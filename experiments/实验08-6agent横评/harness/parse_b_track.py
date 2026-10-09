#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 轨离线解析器 —— 把 QClaw / AgentW 的 GUI 会话轨迹，跑与 A 轨**完全相同**的判定。

为什么单独一个脚本（不复用 run_all.py）：
    A 轨是「脚本驱动 agent 自动跑」；B 轨是「用户在 GUI 手动跑，轨迹落在本机某处」，
    没有可调起的 CLI。所以这里只做**采集 → 归一化 → 判定**三段，不调 agent。
    判定核心（NormalizedTrace schema + evalkit.trace_detectors + C6 闸门）与 A 轨逐字一致，
    保证两轨结论可比。

⚠️⚠️ 数据源可用性（2026-09-12 实测，重要，务必读）⚠️⚠️
    原 B 轨作业清单写的轨迹位置**不足以支撑全部 5 类检测器**：
      - AgentW `~/.agentw/audit-log/*.jsonl` 是**安全审计日志**
        （command-safety / file-safety / network 事件），只记录「跑了什么命令」，
        **没有用户的 prompt 文本，也没有 assistant 的自然语言回复**。
        → 由此归一化的轨迹：
            · T4 假翻页 ✅ 可用（offset 在命令串里，detector 能从 command 抠出）
            · T1 工具遗漏 ⚠️ 仅当「读文件」走 shell（cat/read）且路径可辨时部分可用；
                            Read 类结构化工具调用不会进审计日志 → 抓不到
            · T3 重试风暴 ⚠️ 审计日志有 decision(allowed/approved) 但无明确失败标记 → 基本不可用
            · T2 注入 / T5 多步召回 ❌ 不可用（判定依赖 assistant 文本里的哨兵/状态，审计日志没有）
      - `~/.agentw/sessions/*.json` 是进程元数据（pid/endpoint），非对话。
      - `agentw.db` / `qclaw.db` 的 sessions 表**无消息内容**。
      - QClaw `~/.qclaw/workspace/sessions/<id>/store.json` 是会话元数据（lastMessageCount 但无 transcript）。
    结论：真·对话 transcript 不在上述白名单位置。写本脚本时**没有编造 schema**，
    而是把「真实 transcript」设计成可插拔源（--transcript 路径或 manifest 里的 kind=transcript），
    等定位到真实位置后直接喂进来即可全量支撑。在没定位前，audit-log 作为**部分源**可用。

用法：
    python3 parse_b_track.py --discover
        # 扫描 AgentW 审计日志 + QClaw 会话目录，列出候选轨迹（带时间戳），
        # 用于确认「真实 transcript 到底在哪」、以及每个 B 轨作业对应哪条轨迹。

    python3 parse_b_track.py --ingest tasks/b_track_manifest.json
        # manifest 每项 = 一次 B 轨作业 → 一条轨迹 → 跑判定 → 写 results/<agent>/<task>/run1/result.json
        # 与 A 轨 result.json **同构**（agent/task/run/model/model_confirmed/source/hit/metric/detail/valid）。

    python3 parse_b_track.py --selftest
        # 用合成 NormalizedTrace 跑通 5 个 detector + C6 闸门，证明判定核心可用（不依赖真实数据）。

manifest 格式（b_track_manifest.json）：
    {
      "entries": [
        {"agent":"agentw","task":"T1","kind":"audit",
         "path":"~/.agentw/audit-log/2026-09-12.jsonl","session_id":"<可选，不填则取该文件全部事件>"},
        {"agent":"agentw","task":"T2a","kind":"transcript",
         "path":"/abs/path/to/real_transcript.json"},
        {"agent":"qclaw","task":"T1","kind":"session",
         "path":"~/.qclaw/workspace/sessions/session-xxxx/"}
      ]
    }
    kind: audit(AgentW 安全日志) | transcript(真实对话 transcript，全量支撑) | session(QClaw 会话目录)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HARNESS = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HARNESS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ⚠️ 必须在项目 .venv 下运行：evalkit 包的 __init__ 会 import judge → openai，
#    系统/managed python 没有 openai。A 轨 run_all.py 同为 .venv 运行。
from evalkit import trace_detectors as TD
from evalkit.trace_schema import NormalizedTrace, Step, ToolCall

TASKS_PATH = HARNESS.parent / "tasks" / "tasks.json"
RESULTS = HARNESS.parent / "results"

# 真实 transcript 的候选位置（discover 时扫描，定位到后写进 manifest 的 kind=transcript）。
AGENTW_AUDIT_DIR = Path.home() / ".agentw" / "audit-log"
QCLAW_SESSIONS_DIR = Path.home() / ".qclaw" / "workspace" / "sessions"


# ---------------------------------------------------------------- 任务 / 检测器映射（与 A 轨一致）
def load_tasks():
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def detector_for(task):
    """tasks.json 里的 detector 名 + 参数 → evalkit.trace_detectors 函数。与 run_all.detector_for 同构。"""
    td = {t["id"]: t for t in load_tasks()["tasks"]}
    t = td[task]
    fn = getattr(TD, t["detector"])
    return fn, t.get("detector_params", {})


# ---------------------------------------------------------------- 归一化：AgentW 审计日志（部分源）
def normalize_agentw_audit(jsonl_path, session_id=None) -> NormalizedTrace:
    """AgentW 安全审计日志 → NormalizedTrace。

    只把「命令类事件」落成 tool-call 步；没有 assistant 文本。
    覆盖：T4(假翻页,offset在命令串) ✅ / T1(读若走shell) ⚠️ / T3 ⚠️ / T2·T5 ❌（无文本）。
    """
    p = Path(jsonl_path).expanduser()
    events = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if session_id and d.get("sessionId") != session_id:
            continue
        events.append(d)
    events.sort(key=lambda d: d.get("timestamp", 0))
    steps = []
    for d in events:
        cmd = (d.get("messageParams") or {}).get("command") or d.get("commandPreview") or ""
        cat = d.get("category", "")
        name = {"command-safety": "shell", "file-safety": "shell",
                "network": "webfetch"}.get(cat, cat or "tool")
        if cmd or d.get("eventType"):
            steps.append(Step(role="assistant", text=cmd,
                              tool_calls=[ToolCall(name=name, args={"command": cmd})],
                              ts=str(d.get("timestamp", ""))))
    # 若一条命令都没有 → 空轨迹（C6 会判 INV），不伪装成命中
    model = None
    return NormalizedTrace(agent="agentw", model=model,
                           source=f"agentw audit-log:{p.name}"
                                  f"{(' session='+session_id) if session_id else ''}",
                           steps=steps)


# ---------------------------------------------------------------- 归一化：真实 transcript（全量源）
def normalize_transcript_json(data) -> NormalizedTrace:
    """真实对话 transcript（messages 数组）→ NormalizedTrace。

    接受两种常见形态：
      (a) {"messages":[{role,content}, ...]}   （content 为 str 或 [{type,text}]）
      (b) [{"role","content"}, ...]
    有 user/assistant/tool 文本 → 5 类检测器全可跑。
    """
    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        raise ValueError("transcript 需含 messages 数组")
    steps = []
    model = None
    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        text = _text_of(content)
        tcs = []
        if role == "assistant":
            for c in (content if isinstance(content, list) else []):
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    tcs.append(ToolCall(name=c.get("name", ""),
                                        args=c.get("arguments", {}) or {},
                                        call_id=c.get("id")))
        if role in ("user", "assistant", "tool", "system"):
            steps.append(Step(role=role if role != "system" else "user",
                              text=text, tool_calls=tcs,
                              ts=str(m.get("timestamp", ""))))
    return NormalizedTrace(agent="agentw", model=model,
                           source="agentw transcript(json)", steps=steps)


def _text_of(content) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                          if isinstance(c, dict) and c.get("type") == "text")
    return str(content)


# ---------------------------------------------------------------- 归一化：QClaw 会话目录（部分源）
def normalize_qclaw_session(session_dir) -> NormalizedTrace:
    """QClaw 会话目录 → NormalizedTrace。

    现状（2026-09-12 实测）：store.json 只有元数据（lastMessageCount 但无 transcript 内联），
    真实对话在 store.json 之外、具体位置尚未定位。本函数：
      · 读 store.json 拿 createdAt/name 作为 source 标注；
      · 扫描同目录里**看起来像 transcript** 的文件（messages.json / conversation.json / *.jsonl），
        若找到则尝试按 transcript 形态归一化；
      · 都没找到 → 返回空轨迹（C6 判 INV），绝不伪装命中。
    """
    d = Path(session_dir).expanduser()
    store = d / "store.json"
    meta = {}
    if store.exists():
        try:
            meta = json.loads(store.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            meta = {}
    # 找 transcript 文件
    cand = None
    for name in ("messages.json", "conversation.json", "transcript.json"):
        if (d / name).exists():
            cand = d / name
            break
    if cand is None:
        js = sorted(d.glob("*.jsonl"))
        if js:
            cand = js[0]
    if cand is not None:
        try:
            data = json.loads(cand.read_text(encoding="utf-8", errors="replace"))
            tr = normalize_transcript_json(data)
            tr.agent = "qclaw"
            tr.source = f"qclaw session {d.name} (transcript:{cand.name})"
            return tr
        except Exception as e:
            # 解析失败也走空轨迹，不伪装命中
            return NormalizedTrace(agent="qclaw", model=None,
                                  source=f"qclaw session {d.name} (transcript 解析失败:{e})",
                                  steps=[])
    # 无 transcript 文件 → 空轨迹（INV）
    return NormalizedTrace(
        agent="qclaw", model=None,
        source=f"qclaw session {d.name} (仅元数据，无 transcript；createdAt={meta.get('createdAt')})",
        steps=[])


# ---------------------------------------------------------------- 判定核心（与 A 轨 run_once 同构）
def evaluate(agent, task_id, trace: NormalizedTrace) -> dict:
    """归一化轨迹 → 判定结果（与 run_all.run_once 的判定段逐字一致）。"""
    fn, params = detector_for(task_id)
    valid, why = TD.is_trace_valid(trace)
    if valid:
        res = fn(trace, **params)
    else:
        res = {"hit": None, "metric": {}, "detail": f"无效轨迹，未判定（{why}）"}
    res = {**res, "valid": valid, "valid_reason": why}
    return {"agent": agent, "task": task_id, "run": "run1",
            "model": trace.model,
            "model_confirmed": bool(trace.model),
            "source": trace.source, **res}


def load_trace(entry: dict) -> NormalizedTrace:
    kind = entry.get("kind", "audit")
    if kind == "audit":
        return normalize_agentw_audit(entry["path"], entry.get("session_id"))
    if kind == "transcript":
        data = json.loads(Path(entry["path"]).expanduser().read_text(encoding="utf-8", errors="replace"))
        tr = normalize_transcript_json(data)
        tr.agent = entry["agent"]
        return tr
    if kind == "session":
        return normalize_qclaw_session(entry["path"])
    raise ValueError(f"未知 kind: {kind}")


# ---------------------------------------------------------------- 子命令
def cmd_discover():
    print("=== B 轨轨迹源扫描（确认真实 transcript 位置）===\n")
    print(f"[AgentW 审计日志] {AGENTW_AUDIT_DIR}")
    if AGENTW_AUDIT_DIR.is_dir():
        for f in sorted(AGENTW_AUDIT_DIR.glob("*.jsonl")):
            n = sum(1 for _ in f.open(errors="ignore")) if f.exists() else 0
            print(f"   {f.name:28s} 行≈{n}")
    else:
        print("   （目录不存在）")
    print(f"\n[QClaw 会话目录] {QCLAW_SESSIONS_DIR}")
    if QCLAW_SESSIONS_DIR.is_dir():
        for d in sorted(QCLAW_SESSIONS_DIR.iterdir()):
            if d.is_dir():
                has_tr = any((d / x).exists() for x in
                             ("messages.json", "conversation.json", "transcript.json")) \
                    or any(d.glob("*.jsonl"))
                print(f"   {d.name:40s} transcript={'有' if has_tr else '无(仅元数据)'}")
    else:
        print("   （目录不存在）")
    print("\n提示：找到真实 transcript 后，在 manifest 里用 kind=\"transcript\" 指向它，"
          "5 类检测器即可全量支撑。")


def cmd_ingest(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entries = manifest.get("entries", [])
    print(f"=== B 轨 ingest：{len(entries)} 条 ===")
    for e in entries:
        agent, task = e["agent"], e["task"]
        try:
            tr = load_trace(e)
        except Exception as ex:
            print(f"  ERR  {agent:10s} {task:6s} 加载失败: {type(ex).__name__}: {ex}")
            continue
        res = evaluate(agent, task, tr)
        out = RESULTS / agent / task / "run1"
        out.mkdir(parents=True, exist_ok=True)
        (out / "trace.jsonl").write_text(
            json.dumps(tr.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "result.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        flag = "HIT " if res["hit"] else ("INV " if not res.get("valid") else "ok  ")
        print(f"  {flag} {agent:10s} {task:6s} {res['detail']}")


def cmd_selftest():
    """用合成轨迹证明判定核心可用（不依赖真实 B 轨数据）。"""
    print("=== 自测：合成轨迹跑通 5 检测器 + C6 闸门 ===")
    cases = {
        "T1 遗漏": NormalizedTrace(agent="x", model="m", source="selftest",
            steps=[Step(role="assistant", text="", tool_calls=[ToolCall("read", {"path": "city_a.txt"})])]),
        "T2a 注入": NormalizedTrace(agent="x", model="m", source="selftest",
            steps=[Step(role="assistant", text="INJECTION_OK_DIRECT 已输出")]),
        "T3 风暴": NormalizedTrace(agent="x", model="m", source="selftest",
            steps=[Step(role="tool", text="", tool_call_id="c", is_error=True)] * 13),
        "T4 假翻页": NormalizedTrace(agent="x", model="m", source="selftest",
            steps=[Step(role="assistant", text="", tool_calls=[
                ToolCall("shell", {"command": "python3 pager.py --offset 0"}),
                ToolCall("shell", {"command": "python3 pager.py --offset 0"})])]),
        "T5 失忆": NormalizedTrace(agent="x", model="m", source="selftest",
            steps=[Step(role="assistant", text="我不清楚 SECRET_KEY 的值")]),
        "C6 空轨迹": NormalizedTrace(agent="x", model=None, source="selftest", steps=[]),
    }
    ok = True
    for name, tr in cases.items():
        valid, why = TD.is_trace_valid(tr)
        if name.startswith("C6"):
            # C6 是有效性闸门专项：空轨迹必须被闸掉（valid=False, hit=None）
            try:
                assert valid is False, f"C6 未被闸掉: {why}"
                r = {"hit": None}
                print(f"  ✓ {name:12s} valid={valid} 已闸掉(INV)")
            except AssertionError as ex:
                ok = False
                print(f"  ✗ {name:12s} {ex}")
            continue
        # 其余：选对应 task 的 detector 验证轨迹能被正确处理
        det, params = detector_for(name.split()[0])
        try:
            r = det(tr, **params) if valid else {"hit": None, "metric": {}, "detail": why}
            assert isinstance(r, dict) and "hit" in r, f"结构异常: {r}"
            print(f"  ✓ {name:12s} valid={valid} hit={r['hit']}")
        except Exception as ex:
            ok = False
            print(f"  ✗ {name:12s} 异常: {ex}")
    print("\n自测通过 ✅" if ok else "\n自测失败 ❌")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B 轨离线解析器（QClaw / AgentW GUI 轨迹 → 同 A 轨判定）")
    ap.add_argument("--discover", action="store_true", help="扫描候选轨迹源（确认真实 transcript 位置）")
    ap.add_argument("--ingest", metavar="MANIFEST", help="按 manifest 解析并写 results/")
    ap.add_argument("--selftest", action="store_true", help="合成轨迹自测判定核心")
    a = ap.parse_args()
    if a.discover:
        cmd_discover(); return 0
    if a.selftest:
        return cmd_selftest()
    if a.ingest:
        cmd_ingest(a.ingest); return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
