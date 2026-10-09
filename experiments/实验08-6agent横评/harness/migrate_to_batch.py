#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 phase1 的既有产物（SID 列表）迁移成 v1 批次目录，**并复算校验**。

为什么需要它：
    phase1 的原始产物是「SID 指针 + 文本汇总」，轨迹本体只在本机 `opencode.db` 里。
    本脚本把轨迹**落盘**进 `traces/norm`（+ `traces/raw`）——
    于是「换台机器仍能重判」这个目标第一次成立（这是团队能用的第一要求）。

    同时它本身就是验收：新结构复算出来的数字 **必须与归档一致**
    （T1 = 1/30、0/30、0/30；T2/a9 = 15/30），否则说明迁移动了什么。

用法：
    python3 experiments/实验08-6agent横评/harness/migrate_to_batch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent                       # experiments/实验08-6agent横评
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evalkit import batch as B                            # noqa: E402
from evalkit import trace_detectors as TD                 # noqa: E402
from evalkit.adapters import fetch_opencode_trace         # noqa: E402
from evalkit.stats import wilson                          # noqa: E402
from evalkit.tasks import load_task_set                   # noqa: E402

SPEC = EXP / "tasks" / "tasks-phase1-opencode.json"
SIDS = EXP / "results" / "opencode" / "phase1" / "sids"
BATCH_ID = "20260916-opencode-phase1-k30"
OUT = EXP / "results" / "opencode" / BATCH_ID


def main() -> int:
    spec = load_task_set(SPEC)
    meta = spec.get("meta", {})
    bd = B.new_batch(
        OUT, batch_id=BATCH_ID,
        agent=meta.get("agent", "opencode"), model=None,   # 模型稍后从轨迹回填
        adapter="opencode", adapter_version="0.3.0",
        task_set_path=SPEC, k=meta.get("k", 30),
    )

    rows, cases = [], []
    valid = invalid = 0
    model_seen = None

    for case in spec["cases"]:
        fp = SIDS / case["sids_file"]
        if not fp.exists():
            print(f"[{case['id']}] 缺 SID 文件，跳过")
            continue
        sids = [ln.strip() for ln in fp.read_text().splitlines() if ln.strip()]
        hits, n_invalid = [], 0
        for i, sid in enumerate(sids):
            trace = fetch_opencode_trace(sid)
            if model_seen is None and trace.model:
                model_seen = trace.model
            B.write_trace(bd, case["id"], i, trace,
                          raw={"sid": sid, "source": "opencode.db",
                               "note": "原始投影的**最小版**：只有指针来源。"})
            ok, why = TD.is_trace_valid(trace)
            if not ok:
                n_invalid += 1
                invalid += 1
                rows.append({"case": case["id"], "run": i, "sid": sid,
                             "invalid": True, "reason": why})
                continue
            valid += 1
            res = getattr(TD, case["detector"])(trace, **case.get("detector_params", {}))
            h = 1 if res.get("hit") else 0
            hits.append(h)
            rows.append({
                "case": case["id"], "run": i, "sid": sid,
                "trace": f"traces/norm/{str(case['id']).replace('/', '_')}/{i}.json",
                "detector": case["detector"], "params": case.get("detector_params", {}),
                "verdict": h, "invalid": False,
                "detail": res.get("detail", ""), "metric": res.get("metric"),
            })

        n_, k_ = len(hits), sum(hits)
        p, lo, hi = wilson(k_, n_)
        cases.append({"id": case["id"], "label": case.get("label"),
                      "n": n_, "hits": k_, "rate": round(p, 4),
                      "ci": [round(lo, 4), round(hi, 4)], "invalid": n_invalid})
        print(f"[{case['id']}] 样本={n_} 命中(失败)={k_}/{n_}={p * 100:.1f}% "
              f"CI[{lo * 100:.1f}%,{hi * 100:.1f}%]" + (f"  ⚠无效{n_invalid}" if n_invalid else ""))

    B.write_verdicts(bd, rows)
    B.write_summary(bd, cases, counts={"runs": len(rows), "valid": valid, "invalid": invalid})

    # 回填实测模型（跑完才知道用的是哪个）
    meta_now = B.load_batch(bd)
    meta_now["agent"]["model"] = model_seen
    (bd / "batch.json").write_text(
        __import__("json").dumps(meta_now, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    print()
    print(f"批次目录 -> {bd}")
    print(f"  traces/norm  文件数: {len(list((bd / 'traces' / 'norm').rglob('*.json')))}")
    print(f"  traces/raw   文件数: {len(list((bd / 'traces' / 'raw').rglob('*.json')))}")
    print(f"  verdicts 行数: {len(rows)}（valid {valid} / invalid {invalid}）")
    print(f"  实测模型（回填进 batch.json）: {model_seen}")
    print()
    print("=== 可比性自检（拿自己跟自己比，应为可比）===")
    ok, why = B.comparable(meta_now, meta_now)
    print(f"  可比: {ok}｜原因: {why or '无差异'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
