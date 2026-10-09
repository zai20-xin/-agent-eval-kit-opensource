#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bare 轨迹适配器：JSONL 会话树 → NormalizedTrace（in-process 驱动）。

与 opencode 适配器（子进程 + SQLite）的三点不同，都是因为 bare_agent 就在本仓库里：
    · **in-process 调用**：A1 已把 system_prompt / messages / tools / registry 做成参数，
      直接 import 调用比再包一层子进程少两次序列化，且测试可注入假 client（不碰网络）
    · **原始 JSONL 落 `cwd/traces/`（工作区）**，批次里持久化的是 norm —— 与 opencode
      同构：raw 在库外，norm 进批（引用原始轨迹去实验目录抄，B6 落档时负责）
    · **session 文件 = `cwd/.bare_session`**（存最近一次 trace 绝对路径），
      多轮续接靠它找上一轮 —— 替代 opencode 的「DB 里取最新 session id」

场景选择：env `BARE_SCENE`（`customer_service` | `default`），**每次 drive 时读取**
    · customer_service（默认）：三段式外壳 + 4 客服工具（M1-A2）
    · default：裸 agent 原样（无 system prompt + weather/read_file 工具）

⚠ `timeout` 参数被**忽略**（in-process 无硬超时）：护栏 = `max_turns` + 客户端调用超时。
  这是与 opencode 子进程驱动的已知行为差异，写在这里防止被当成 bug 修。

C6 纪律（同 opencode 适配器）：
    · fetch_trace 取不到文件 → `steps=[]` 的空轨迹 → `trace_quality` 判 collect_failed
      → **绝不能算「失败模式命中」**
    · drive 失败：fresh 失败返回 `(None, False)`（本 run 记采集失败，**不借上一 run 的轨迹**）；
      续接失败返回 `(prev_sid, False)`（上一轮是真跑出来的，按部分历史判）
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from ..trace_schema import NormalizedTrace, Step, ToolCall

BARE_DIR = Path(__file__).resolve().parent.parent.parent / "bare-agent"
SESSION_FILE = ".bare_session"
DEFAULT_SCENE = "customer_service"
_SCENE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

if str(BARE_DIR) not in sys.path:
    sys.path.insert(0, str(BARE_DIR))


def _bare():
    import bare_agent
    return bare_agent


def _load_scene(name: str):
    """场景 → (system_prompt, tools, registry)；非法名/缺模块 → ValueError（drive 兜成失败）。"""
    if not _SCENE_RE.match(name or ""):
        raise ValueError(f"非法场景名: {name!r}")
    ba = _bare()
    if name == "default":
        return None, ba.TOOLS, ba.REGISTRY
    mod = importlib.import_module(f"scenes.{name}")
    return mod.SYSTEM_PROMPT, mod.TOOLS, mod.REGISTRY


def _read_lines(path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:  # noqa: BLE001  单条脏行不该毁掉整条轨迹
                    continue
    return out


def _msg_text(content) -> str:
    """content 是 [{type,text}] 列表（bare 轨迹格式）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _messages_from_lines(lines: list[dict]) -> list[dict]:
    """JSONL 历史 → OpenAI messages（续接用）。tool_calls 的 arguments 从 dict 还原成 JSON 串。"""
    msgs = []
    for ln in lines:
        if ln.get("type") != "message":
            continue
        m = ln.get("message") or {}
        role = m.get("role")
        if role == "system":
            msgs.append({"role": "system", "content": _msg_text(m.get("content"))})
        elif role == "user":
            msgs.append({"role": "user", "content": _msg_text(m.get("content"))})
        elif role == "assistant":
            content = m.get("content") or []
            calls = [c for c in content
                     if isinstance(c, dict) and c.get("type") == "toolCall"]
            if calls:
                msgs.append({"role": "assistant",
                             "content": _msg_text(content),
                             "tool_calls": [{
                                 "id": c.get("id"), "type": "function",
                                 "function": {"name": c.get("name", ""),
                                              "arguments": json.dumps(
                                                  c.get("arguments") or {},
                                                  ensure_ascii=False)}} for c in calls]})
            else:
                msgs.append({"role": "assistant", "content": _msg_text(content)})
        elif role == "toolResult":
            msgs.append({"role": "tool", "tool_call_id": m.get("toolCallId"),
                         "content": _msg_text(m.get("content"))})
    return msgs


# ---------------------------------------------------------------- 驱动（采集侧）
def drive(prompt, cwd, timeout=None, continuation=False):
    """跑一次 bare agent，返回 `(sid, is_new)`；sid = 原始 JSONL 绝对路径。

    续接（continuation=True）：读 `cwd/.bare_session` 找上一轮 → 重建 messages →
    **历史行整份并入新 trace**（B10 状态保持判定要看全 history，不只最后一轮）。
    """
    cwd = Path(cwd)
    prev_sid = None
    sess = cwd / SESSION_FILE
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        if sess.exists():
            prev_sid = sess.read_text(encoding="utf-8").strip() or None
        scene = os.environ.get("BARE_SCENE", DEFAULT_SCENE)
        system_prompt, tools, registry = _load_scene(scene)
        ba = _bare()
    except Exception:  # noqa: BLE001  场景/环境故障 = 驱动侧问题，不是 agent 失败
        return None, False

    use_prev = bool(continuation and prev_sid and Path(prev_sid).exists())
    if continuation and not use_prev:
        # 续接却找不到上一轮 → 降级成单轮会「看起来有效但缺上文」，会污染 B10 判定
        return None, False
    old_cwd = Path.cwd()
    try:
        os.chdir(cwd)                            # Trace 的 cwd 字段 + 工具相对路径（串行调用下安全）
        if use_prev:
            lines = _read_lines(prev_sid)
            msgs = _messages_from_lines(lines)
            sid_model = next((l.get("modelId") for l in lines
                              if l.get("type") == "session"), None)
            trace = ba.Trace(uuid.uuid4().hex[:8], sid_model)
            trace.lines = list(lines)            # 历史整份并入
            trace.parent = next((l["id"] for l in reversed(lines)
                                 if l.get("type") == "message"), None)
            # 历史里已有 system（场景外壳），勿再传 system_prompt（会重复注入）
            result = ba.run_agent(prompt=prompt, trace=trace, messages=msgs,
                                  system_prompt=None, tools=tools, registry=registry)
        else:
            result = ba.run_agent(prompt=prompt, system_prompt=system_prompt,
                                  tools=tools, registry=registry)
        sid = cwd / "traces" / f"trace-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.jsonl"
        result["trace"].save(sid)
        sess.write_text(str(sid), encoding="utf-8")
        return str(sid), True
    except Exception:  # noqa: BLE001  API/环境故障 → 采集失败（C6），不冒充 agent 行为
        if use_prev:
            return str(prev_sid), False          # 上一轮是真跑出来的，按部分历史判
        return None, False
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass


# ---------------------------------------------------------------- 判定侧
def fetch_trace(sid, agent="bare") -> NormalizedTrace:
    """sid（原始 JSONL 绝对路径）→ NormalizedTrace。

    **system 行不进归一化**：同 opencode 排除 reasoning 的理由 ——
    system prompt 原文进 `all_text()` 会把「系统提示词泄露」类检测器自伤成永远命中。
    外壳内容需要时回 `scenes.<scene>` 读，不走轨迹。
    """
    if not sid or not Path(sid).exists():
        return NormalizedTrace(agent=agent, model=None,
                               source=f"bare trace 不存在: {sid}", steps=[])

    lines = _read_lines(sid)
    steps, model = [], None
    for ln in lines:
        if ln.get("type") == "session":
            model = model or ln.get("modelId")
            continue
        if ln.get("type") != "message":
            continue
        m = ln.get("message") or {}
        role, content = m.get("role"), m.get("content")
        if role == "system":
            continue
        if role == "user":
            steps.append(Step(role="user", text=_msg_text(content),
                              ts=ln.get("timestamp")))
        elif role == "assistant":
            if not isinstance(content, list):
                steps.append(Step(role="assistant", text=_msg_text(content),
                                  ts=ln.get("timestamp")))
                continue
            calls = [ToolCall(name=c.get("name", ""), args=c.get("arguments") or {},
                              call_id=c.get("id"))
                     for c in content if isinstance(c, dict) and c.get("type") == "toolCall"]
            steps.append(Step(role="assistant", text=_msg_text(content),
                              tool_calls=calls, ts=ln.get("timestamp")))
        elif role == "toolResult":
            steps.append(Step(role="tool", text=_msg_text(content),
                              tool_call_id=m.get("toolCallId"),
                              is_error=bool(m.get("isError")),
                              ts=ln.get("timestamp")))

    return NormalizedTrace(agent=agent, model=model,
                           source=f"bare jsonl lines={len(lines)} sid={sid}",
                           steps=steps)
