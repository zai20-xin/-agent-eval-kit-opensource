#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对账：新检测器（三通道口径） vs 已归档判定（旧 read-only 口径）。

为什么必须做这一步：
    2026-09-21 并层时，把 detect_tool_omission 的「内容访问」识别从 **1 条通道**
    （只认 read 工具的路径参数）补成 **3 条**（read / shell-cat / bulk-grep）。
    但归档结果 `results/opencode/phase1/summary_phase1_k30.txt` 是用**旧口径**的
    `phase1_judge.judge_t1` 算出来的。

    两者若不逐条一致，说明「并层」实质上改写了结论 —— 那就必须先查清根因、
    并决定以哪边为准，绝不能把差异糊过去。
    （这正是 09-14 那批「假拐点」的教训：判定口径悄悄变了却没人对账。）

用法：
    python3 experiments/实验08-6agent横评/harness/reconcile_detectors.py
退出码 0 = 逐条一致；1 = 存在差异（明细会打印，不掩盖）。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from evalkit.adapters import fetch_opencode_trace          # noqa: E402
from evalkit.trace_detectors import detect_tool_omission   # noqa: E402
import phase1_judge as PJ                                  # noqa: E402

SIDS = HERE.parent / "results" / "opencode" / "phase1" / "sids"
CASES = [(6, "t1_n6_sids.txt"), (7, "t1_n7_sids.txt"), (8, "t1_n8_sids.txt")]

same, diff, invalid = 0, [], 0

for N, fname in CASES:
    fp = SIDS / fname
    if not fp.exists():
        print(f"[跳过] 缺 SID 文件 {fp}")
        continue
    sids = [ln.strip() for ln in fp.read_text().splitlines() if ln.strip()]
    required = [f"f{i}.py" for i in range(1, N + 1)]
    n_new = n_old = 0
    for sid in sids:
        trace = fetch_opencode_trace(sid)
        if not trace.steps:
            invalid += 1
            print(f"  [无效轨迹] {sid}（parts=0，不可判定）")
            continue
        new_hit = bool(detect_tool_omission(trace, required)["hit"])
        old_hit = bool(PJ.judge_t1(sid, N)[0])
        n_new += new_hit
        n_old += old_hit
        if new_hit == old_hit:
            same += 1
        else:
            diff.append((N, sid, new_hit, old_hit))
    print(f"[T1/N={N}] 新口径命中 {n_new}/{len(sids)} ｜ 旧口径命中 {n_old}/{len(sids)}")

print()
print(f"=== 逐条一致 {same} 条 ｜ 差异 {len(diff)} 条 ｜ 无效轨迹 {invalid} 条 ===")
if diff:
    print("差异明细（N, sid, 新hit, 旧hit）：")
    for row in diff:
        print("  ", row)
sys.exit(1 if diff else 0)
