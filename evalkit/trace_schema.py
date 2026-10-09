#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归一化轨迹 schema —— W2 起，评测对象从「单条天气答案」升级为「整条 agent 轨迹」。

为什么需要它：
    pi / opencode / mimo / 裸 agent 各自产出不同格式（JSONL 会话树 / SQLite / 内存对象）。
    判定器只认这一种格式 → 各家 trace 先归一化到这里，检测器才能复用、可换 agent 不换判定。

设计铁律：
    判定器只能读这个 schema，绝不直接读原始 JSONL / SQLite。
    这样换一家 agent 只改 normalize，不动 trace_detectors。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)
    call_id: Optional[str] = None


@dataclass
class Step:
    role: str                        # user / assistant / tool
    text: str = ""
    tool_calls: list = field(default_factory=list)   # list[ToolCall]，仅 assistant
    tool_call_id: Optional[str] = None                # 仅 tool 结果
    is_error: bool = False
    ts: Optional[str] = None


@dataclass
class NormalizedTrace:
    agent: str = ""
    model: Optional[str] = None
    source: str = ""                 # 原始来源说明（如 "pi --mode json"）
    steps: list = field(default_factory=list)

    # ---- 便捷迭代 ----
    def tool_steps(self):
        """所有带 tool_calls 的 assistant 步。"""
        for s in self.steps:
            if s.role == "assistant" and s.tool_calls:
                yield s

    def tool_results(self):
        for s in self.steps:
            if s.role == "tool":
                yield s

    def all_text(self) -> str:
        """拼所有 user+assistant+tool 文本，用于注入标记扫描。"""
        return "\n".join(s.text for s in self.steps if s.text)

    def all_calls(self) -> list:
        out = []
        for s in self.tool_steps():
            out.extend(s.tool_calls)
        return out

    def to_dict(self):
        return asdict(self)


# 轨迹 schema 版本 —— 字段增删时递增，并记进批次元信息（batch.json）。
# 换版本之后旧轨迹还能不能重判，必须显式判断，不能靠"试一下"。
TRACE_SCHEMA_VERSION = 1


def trace_from_dict(d) -> NormalizedTrace:
    """把 `to_dict()` 的产物还原成 NormalizedTrace。

    用途：批次目录里的 `traces/norm/<case>/<run>.json` 存的就是 to_dict() 的结果，
    重判时用它读回来 —— 这样即使原始 agent（及其本机 DB）已经不在了，判定仍可复现。
    """
    steps = []
    for s in (d or {}).get("steps", []) or []:
        calls = [
            ToolCall(name=c.get("name", ""), args=c.get("args") or {},
                     call_id=c.get("call_id"))
            for c in (s.get("tool_calls") or [])
        ]
        steps.append(Step(
            role=s.get("role", ""),
            text=s.get("text", "") or "",
            tool_calls=calls,
            tool_call_id=s.get("tool_call_id"),
            is_error=bool(s.get("is_error")),
            ts=s.get("ts"),
        ))
    return NormalizedTrace(agent=(d or {}).get("agent", ""),
                           model=(d or {}).get("model"),
                           source=(d or {}).get("source", ""),
                           steps=steps)
