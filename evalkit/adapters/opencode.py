#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode 轨迹适配器：SQLite → NormalizedTrace。

数据位置（2026-09-21 实测确认）：

    ~/.local/share/opencode/opencode.db
      · `message` 表 data 字段
            role     = "user" / "assistant"
            modelID  = 实际使用的模型名（如 mimo-v2.5-free）
            tokens / cost / parentID
      · `part` 表 data 字段，type ∈ {text, tool, reasoning, step-start, step-finish}
            text   → data.text            ⚠️ 本身**没有 role**，必须 JOIN message 才知道是谁说的
            tool   → data.tool（工具名）+ data.callID + data.state.input / .output / .status
            reasoning / step-start / step-finish → 流程与思维链，判定不需要

为什么直接读 DB，而不是走 `opencode export`：
    export 要额外起一次 CLI 进程，且其输出与 DB 字段之间还隔一层映射；
    直接读 DB 的字段语义已在 phase1 的 230 条跑批里被验证过（判定脚本用的就是这些字段）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess

from ..trace_schema import NormalizedTrace, Step, ToolCall

DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")

_TEXT, _TOOL, _REASONING = "text", "tool", "reasoning"

_QUERY = (
    "SELECT p.time_created, p.data, m.data "
    "FROM part p JOIN message m ON p.message_id = m.id "
    "WHERE p.session_id = ? ORDER BY p.time_created"
)


def _load(raw):
    try:
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001  单条脏数据不该毁掉整条轨迹
        return {}


def fetch_trace(sid, db_path=None, agent="opencode") -> NormalizedTrace:
    """按 session id 取一条轨迹，归一化为 NormalizedTrace。

    ⚠️ 取不到（DB 不存在 / session 无 part）时返回 `steps=[]` 的 trace ——
       交给 `is_trace_valid()` 判成「不可判定」，**绝不能当成「失败模式命中」**。
       这是 C6（评测脚本自身故障被记到 agent 头上）的处置方式。
    """
    db = db_path or DEFAULT_DB
    if not os.path.exists(db):
        return NormalizedTrace(agent=agent, model=None,
                               source=f"opencode db 不存在: {db}", steps=[])
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(_QUERY, (sid,)).fetchall()
    finally:
        conn.close()

    steps, model = [], None
    for ts, p_raw, m_raw in rows:
        p = _load(p_raw)
        m = _load(m_raw)
        role = m.get("role") or "assistant"
        if model is None and m.get("modelID"):
            model = m.get("modelID")

        ptype = p.get("type")
        if ptype == _TEXT:
            steps.append(Step(role=role, text=p.get("text") or "", ts=str(ts)))
        elif ptype == _TOOL:
            call_id = p.get("callID")
            state = p.get("state") or {}
            steps.append(Step(role="assistant", ts=str(ts), tool_calls=[
                ToolCall(name=p.get("tool") or "",
                         args=state.get("input") or {},
                         call_id=call_id)]))
            out = state.get("output")
            if out is not None:
                steps.append(Step(role="tool", text=str(out), tool_call_id=call_id,
                                  is_error=(state.get("status") == "error")))
        elif ptype == _REASONING:
            # 思维链不进 text 扫描面：否则推理里「复述」到注入串会被误判成中招
            continue

    return NormalizedTrace(agent=agent, model=model,
                           source=f"opencode db parts={len(rows)} sid={sid}",
                           steps=steps)


# ---------------------------------------------------------------- 驱动（采集侧）
# 设计：**一个 agent = 一个文件**，里面同时具备「怎么跑它」与「怎么读它」。
# 于是「换一个 agent」就是只加这一个文件 —— CLI 侧一行都不用改。
CLI = "opencode"
RUN_ARGS = ("run", "--auto")
CONT_FLAG = "-c"                       # `opencode run -c --auto` = 续上一个 session
_LATEST_SID_SQL = "SELECT id FROM session ORDER BY time_created DESC LIMIT 1"


def latest_sid(db_path=None):
    """库里最新的 session id（`phase1_drive.sh` 的 `run_and_capture` 用的是同一思路）。"""
    db = db_path or DEFAULT_DB
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(_LATEST_SID_SQL).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def drive(prompt, cwd, timeout=180, continuation=False, db_path=None):
    """跑一次 agent，返回 `(sid, is_new)`。

    「这次是否跑出了新 session」＝跑前跑后各取一次最新 SID 做差 —— 与 `phase1_drive.sh` 同法。

    ⚠️ `is_new=False` 有两种成因，调用方**必须区分**、不能混为一谈：
        · agent 主动拒绝了请求（根本不建 session）—— b7 实测有 10/30 是这种
        · 驱动自身失败（CLI 不在 PATH / 超时 / 免费额度耗尽）
       两者都不该被当成「失败模式命中」（C6 的处置），但也都**不该静默算作「通过」**。

    超时**不抛异常**：agent 可能已经把 session 建好了（原脚本用 `alarm` 也是这个行为）。
    """
    before = latest_sid(db_path)
    cmd = [CLI, *RUN_ARGS]
    if continuation:
        cmd.insert(2, CONT_FLAG)
    cmd.append(prompt)
    try:
        subprocess.run(cmd, cwd=str(cwd), timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    after = latest_sid(db_path)
    return after, (after is not None and after != before)
