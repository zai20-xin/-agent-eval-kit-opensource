#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轨迹归一化 —— 把各家 agent 的原始 trace 转成 evalkit.trace_schema.NormalizedTrace。

现状（2026-09-10 可驱动性实测报告）：
    pi           → JSONL 会话树，格式与 bare-agent 对齐 → normalize_pi 已实测可用
    opencode/mimo → SQLite（opencode.db / mimocode.db）+ export 子命令 → 格式待确认（🔴）
                   本文件给最佳实现，D3 跑批时以真实 export 输出校正
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evalkit.trace_schema import NormalizedTrace, Step, ToolCall


def _text_of(content) -> str:
    """从 pi/bare-agent 的 content（str 或 [{type,text/name}]）抽出纯文本。"""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "toolCall":
                    parts.append(f"[toolCall {c.get('name')}]")
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(content)


def _normalize_session_lines(lines) -> NormalizedTrace:
    """pi / bare-agent 同构的 JSONL 会话树 → NormalizedTrace。"""
    steps = []
    agent = None
    model = None
    for line in lines:
        if not isinstance(line, dict):
            continue
        if line.get("type") == "session":
            agent = line.get("modelId") and "pi" or agent
            model = line.get("modelId")
            continue
        msg = line.get("message", {})
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            steps.append(Step(role="user", text=_text_of(content), ts=line.get("timestamp")))
        elif role == "assistant":
            tcs = []
            for c in (content or []):
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    tcs.append(ToolCall(name=c.get("name", ""),
                                        args=c.get("arguments", {}) or {},
                                        call_id=c.get("id")))
            steps.append(Step(role="assistant", text=_text_of(content),
                              tool_calls=tcs, ts=line.get("timestamp")))
        elif role == "tool":
            steps.append(Step(role="tool", text=_text_of(content),
                              tool_call_id=line.get("toolCallId"),
                              is_error=bool(line.get("isError")), ts=line.get("timestamp")))
    return NormalizedTrace(agent=agent or "pi", model=model,
                           source="pi/bare-agent jsonl", steps=steps)


def normalize_pi(jsonl_path) -> NormalizedTrace:
    """pi JSONL（type/id/parentId 会话树，与 bare_agent.Trace 同构）。"""
    p = Path(jsonl_path)
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return _normalize_session_lines(lines)


def _extract_model(data: dict):
    """从 export JSON 抽模型 id，兼容两套 schema（2026-09-11 实测校正）：

    - opencode：`info.model.id` → "mimo-v2.5-free"
    - mimo：   `info.model` 为 null，真实模型在 `messages[i].info.model`
              = {providerID:"openrouter", modelID:"nvidia/nemotron-3-ultra-550b-a55b:free"}
              取 modelID（裸 OpenRouter id，与 pi 同格式，不带头 provider 前缀）。
    两处都缺则返回 None（上层标 model_confirmed=False，不进结论）。
    """
    info = data.get("info") or {}
    top = (info.get("model") or {}).get("id")
    if top:
        return top
    for m in data.get("messages") or []:
        mm = (m.get("info") or {}).get("model") or {}
        mid = mm.get("modelID")
        if mid:
            return mid
    return None


def normalize_opencode_export(data: dict, agent_name="opencode") -> NormalizedTrace:
    """opencode / mimo 的 `export <sessionID>` JSON → NormalizedTrace。

    真实 schema（2026-09-11 实测校正，原 D1 的 🔴 已消）：
        { "info": { "id": "ses_xxx", "model": {"id": "mimo-v2.5-free"},
                    "tokens": {...}, "cost": 0, ... },
          "messages": [ { "info": {"role": "user"|"assistant", "time": {...}},
                          "parts": [ {"type": "text", "text": ...},
                                     {"type": "reasoning", "text": ...},
                                     {"type": "step-start"|"step-finish"},
                                     {"type": "tool", "tool": "read", "callID": ...,
                                      "state": {"status","input": {"filePath": ...},
                                                "output": "...", "time": {...}}} ] } ] }

    映射规则：
        - 一条 message → 一个 Step（role 取 info.role）
        - text / reasoning → Step.text（reasoning 也要留，注入证据可能在思维链里）
        - tool → ToolCall(name=part.tool, args=state.input, call_id=callID)
                 并把 state.output 并入 text，保证 all_text() 覆盖工具返回
                 （detect_injection 靠 all_text 扫间接注入带出的痕迹）
    """
    info = data.get("info") or {}
    model = _extract_model(data)
    steps = []
    for m in data.get("messages") or []:
        mi = m.get("info") or {}
        texts, calls = [], []
        for p in m.get("parts") or []:
            t = p.get("type")
            if t in ("text", "reasoning"):
                if p.get("text"):
                    texts.append(str(p["text"]))
            elif t == "tool":
                st = p.get("state") or {}
                calls.append(ToolCall(name=p.get("tool", ""),
                                      args=st.get("input") or {},
                                      call_id=p.get("callID")))
                if st.get("output") is not None:
                    texts.append(str(st["output"]))
        steps.append(Step(role=mi.get("role") or "assistant",
                          text="\n".join(texts),
                          tool_calls=calls,
                          ts=str((mi.get("time") or {}).get("created") or "")))
    return NormalizedTrace(agent=agent_name, model=model,
                           source=f"{agent_name} export", steps=steps)


def normalize_mimo_export(data: dict) -> NormalizedTrace:
    return normalize_opencode_export(data, agent_name="mimo")
