#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
裸 agent · W1 D6-7 —— 最小可跑的 tool-calling 循环（被评测对象）

设计来源：pi-course L03 的 agent-loop（双层循环简化为单层 + max_turns 护栏）。
轨迹落 JSONL，格式对齐 pi 会话树（type/id/parentId），便于 M5 直接加 OTel instrumentation。

用法：
    export OPENROUTER_API_KEY="sk-or-..."
    python3 bare_agent.py "查一下北京天气"                 # 单工具
    python3 bare_agent.py "查深圳天气" --max-turns 3
    python3 bare_agent.py "帮我查订单 8001" --system "你是客服助手……"   # 场景外壳（M1-A1）

作为库用（后续实验/适配器复用）：
    from bare_agent import run_agent, Trace
    r = run_agent("查北京天气"); r["trace"].save(path)
    r = run_agent(system_prompt=CS_PROMPT, messages=old_msgs)   # 外壳 + 多轮续接
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from openai import OpenAI

MODEL = os.environ.get("BARE_AGENT_MODEL", "nvidia/nemotron-3.5-lightning:free")
MAX_TURNS = int(os.environ.get("BARE_AGENT_MAX_TURNS", "6"))
TRACE_DIR = Path(__file__).resolve().parent / "traces"

# ───────────────────────── 工具（确定性假实现，便于评测复现） ─────────────────────────
WEATHER = {
    "北京": {"weather": "晴", "temp": 32, "rain": False},
    "上海": {"weather": "多云", "temp": 28, "rain": True},
    "深圳": {"weather": "雷阵雨", "temp": 30, "rain": True},
}


def get_weather(city: str) -> str:
    d = WEATHER.get(city)
    if d is None:
        return json.dumps({"error": f"unknown city: {city}"}, ensure_ascii=False)
    return json.dumps(d, ensure_ascii=False)


def read_file(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return json.dumps({"error": f"file not found: {path}"}, ensure_ascii=False)
    return p.read_text(encoding="utf-8", errors="replace")[:4000]


TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "查询城市天气，返回 weather/temp/rain 三字段",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string", "description": "城市名，如 北京"}},
                       "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取本地文本文件内容（自动截断到 4000 字符）",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "文件绝对路径"}},
                       "required": ["path"]}}},
]
REGISTRY = {"get_weather": get_weather, "read_file": read_file}


# ───────────────────────── 轨迹（JSONL，对齐 pi 会话树） ─────────────────────────
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_args(tc) -> dict:
    """arguments 是 JSON 字符串（D3-4 已证）；解析失败要留痕，不能静默丢。"""
    try:
        return json.loads(tc.function.arguments) if tc.function.arguments else {}
    except Exception:
        return {"__raw__": tc.function.arguments, "__parse_error__": True}


class Trace:
    def __init__(self, session_id: str, model: str):
        self.lines = [{"type": "session", "id": session_id, "timestamp": _now(),
                       "modelId": model, "cwd": str(Path.cwd())}]
        self.parent = None

    def _emit(self, role: str, **kw):
        mid = "m" + uuid.uuid4().hex[:8]
        self.lines.append({"type": "message", "id": mid, "parentId": self.parent,
                           "timestamp": _now(), "message": {"role": role, **kw}})
        self.parent = mid
        return mid

    def user(self, text):
        return self._emit("user", content=[{"type": "text", "text": text}])

    def system(self, text):
        return self._emit("system", content=[{"type": "text", "text": text}])

    def assistant(self, text, calls, stop_reason, usage):
        content = [{"type": "text", "text": text}] if text else []
        content += [{"type": "toolCall", "id": tc.id, "name": tc.function.name,
                     "arguments": _safe_args(tc)} for tc in calls]
        return self._emit("assistant", content=content, stopReason=stop_reason, usage=usage)

    def tool_result(self, tc, out, is_error):
        return self._emit("toolResult", toolCallId=tc.id, toolName=tc.function.name,
                          content=[{"type": "text", "text": out}], isError=is_error)

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for line in self.lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path


# ───────────────────────── 核心循环（约 45 行） ─────────────────────────
def run_agent(prompt=None, model=MODEL, max_turns=MAX_TURNS, trace=None,
              tools=None, registry=None, system_prompt=None, messages=None):
    """一次完整 run：user → (assistant → tool_calls → tool_results)* → 终局 assistant。

    tools / registry 可注入（默认用内置两个），供实验挂自己的压力工具，不改本文件的默认行为。
    system_prompt / messages 默认 None —— **不传时行为与旧版逐字节一致**（M1-A1）：
      - system_prompt：注入消息列表最前（场景外壳用），并留痕进轨迹
      - messages：既有会话作为起始消息（多轮续接用）；prompt 非 None 时仍追加为一条新 user 消息。
        续接时若原会话已含 system，勿再传 system_prompt（会重复注入）。
    """
    if prompt is None and messages is None:
        raise ValueError("prompt 与 messages 至少给一个")
    tools = tools if tools is not None else TOOLS
    registry = registry if registry is not None else REGISTRY
    # 传输超时（采集侧健壮性，2026-09-26 pilot 踩坑）：free 端点排队时单次调用
    # 可挂 7 分钟以上，无超时会拖死整批。超时 → 异常 → adapter 记 collect_failed（C6），
    # 这是管线问题不冒充 agent 行为；不影响被测行为（模型/提示词/工具都没动）。
    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"],
                    timeout=float(os.environ.get("BARE_AGENT_TIMEOUT", "120")),
                    max_retries=2)
    trace = trace or Trace(uuid.uuid4().hex[:8], model)
    if messages is None:
        messages = [{"role": "user", "content": prompt}]
    else:
        messages = list(messages)                      # 拷贝，不改调用方列表
        if prompt is not None:
            messages.append({"role": "user", "content": prompt})
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages
        trace.system(system_prompt)
    if prompt is not None:
        trace.user(prompt)
    stats = {"llm_calls": 0, "tool_calls": 0, "tool_errors": 0, "arg_parse_errors": 0}

    for turn in range(max_turns):
        resp = client.chat.completions.create(model=model, messages=messages,
                                             tools=tools, tool_choice="auto")
        stats["llm_calls"] += 1
        choice = resp.choices[0]
        msg, finish = choice.message, choice.finish_reason
        usage = getattr(resp, "usage", None)
        usage = {"input": usage.prompt_tokens, "output": usage.completion_tokens} if usage else None
        calls = msg.tool_calls or []
        trace.assistant(msg.content or "", calls, finish, usage)

        if not calls:                                    # 无工具调用 → agent 收工
            return {"answer": msg.content or "", "turns": turn + 1,
                    "stats": stats, "trace": trace, "hit_max_turns": False}

        # assistant 消息必须带 tool_calls 入列，随后 tool 结果按 tool_call_id 配对回填
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name,
                                                      "arguments": tc.function.arguments}}
                                        for tc in calls]})
        for tc in calls:
            stats["tool_calls"] += 1
            name, raw = tc.function.name, tc.function.arguments
            try:
                args = json.loads(raw) if raw else {}
                if _safe_args(tc).get("__parse_error__"):
                    stats["arg_parse_errors"] += 1
                fn = registry.get(name)
                out = fn(**args) if fn else json.dumps({"error": f"Tool not found: {name}"},
                                                       ensure_ascii=False)
                is_error = fn is None
            except Exception as e:                        # 工具错误不炸循环（L03 防线3）
                out = json.dumps({"error": f"{type(e).__name__}: {e}", "raw_arguments": raw},
                                 ensure_ascii=False)
                is_error = True
            if is_error:
                stats["tool_errors"] += 1
            trace.tool_result(tc, out, is_error)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

    return {"answer": "<max_turns reached>", "turns": max_turns,
            "stats": stats, "trace": trace, "hit_max_turns": True}


def main():
    ap = argparse.ArgumentParser(description="裸 agent：最小 tool-calling 循环")
    ap.add_argument("prompt", nargs="?", default="查一下北京天气")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--system", default=None,
                    help="注入 system prompt（场景外壳）；不传 = 旧行为")
    a = ap.parse_args()

    r = run_agent(a.prompt, model=a.model, max_turns=a.max_turns, system_prompt=a.system)
    path = r["trace"].save(TRACE_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}.jsonl")
    print(json.dumps({"answer": r["answer"], "turns": r["turns"],
                      "stats": r["stats"], "hit_max_turns": r["hit_max_turns"],
                      "trace": str(path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
