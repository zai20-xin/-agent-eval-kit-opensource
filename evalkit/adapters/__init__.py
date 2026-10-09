#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轨迹适配器 —— 「换一个 agent，只加一个 adapter」。

每个 adapter 只做一件事：把某家 agent 的**原始轨迹**（JSONL / SQLite / 内存对象）
转成 `evalkit.trace_schema.NormalizedTrace`。转换之后，
**所有检测器与判定逻辑一行都不用改** —— 这是「换 agent 不换判定」的实现点。

已实现：
    opencode  → SQLite（`~/.local/share/opencode/opencode.db` 的 `message` + `part` 表）

待补（W2 口径里提到的复用目标）：
    pi        → JSONL 会话树（现有实现在 experiments/实验08-6agent横评/harness/normalize.py）
    mimo      → SQLite，结构与 opencode 同构（默认模型不同）

新增一家 agent 的标准动作：
    1. 在本目录加 `xxx.py`，实现 `fetch_trace(sid) -> NormalizedTrace`
    2. 在下面 __init__ 里导出
    3. 其余任意代码都不动
"""
from .opencode import fetch_trace as fetch_opencode_trace, DEFAULT_DB as OPENCODE_DB

__all__ = ["fetch_opencode_trace", "OPENCODE_DB"]
