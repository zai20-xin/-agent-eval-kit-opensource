#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验11 题级合并（B5c 同款口径）：多批 verdicts.jsonl 按题聚合。

用法: .venv/bin/python experiments/实验11-旅游场景/合并题级.py <batch_dir>...
输出: 每题 valid/hit/noin/collect 双批合并 + Wilson CI（可报数门 valid≥5）。
仅当各批 suite_fingerprint 与 task_set.sha256 一致时才合并（不一致直接拒）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evalkit.stats import rate_with_ci  # noqa: E402


def load(batch: str):
    d = Path(batch)
    meta = json.loads((d / "batch.json").read_text(encoding="utf-8"))
    rows = [json.loads(l) for l in
            (d / "verdicts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return meta, rows


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    metas, all_rows = [], []
    for b in sys.argv[1:]:
        m, r = load(b)
        metas.append((b, m))
        all_rows.extend(r)
    fp = {m["evaluator"]["suite_fingerprint"] for _, m in metas}
    sha = {m["task_set"]["sha256"] for _, m in metas}
    if len(fp) > 1 or len(sha) > 1:
        print(f"拒绝合并：指纹不一致 {fp} 或任务集不一致 {sha}")
        return 1

    agg = {}
    for r in all_rows:
        cid = r.get("case")
        a = agg.setdefault(cid, {"valid": 0, "hit": 0, "noin": 0, "cf": 0})
        if r.get("invalid"):
            # report 落盘的分类字段：class ∈ {no_output, collect_failed, ...}
            if r.get("class") == "collect_failed":
                a["cf"] += 1
            else:
                a["noin"] += 1
        else:
            a["valid"] += 1
            a["hit"] += int(r.get("verdict") == 1)
    print(f"指纹={next(iter(fp))} 任务集={next(iter(sha))} 批数={len(metas)}")
    tv = th = 0
    for cid in sorted(agg):
        a = agg[cid]
        gate = "✓报数" if a["valid"] >= 5 else "✗<5"
        pct = rate_with_ci(a["hit"], a["valid"]) if a["valid"] else "—"
        print(f"  {cid}: hit={a['hit']}/{a['valid']} {pct}  {gate} "
              f"| no_output={a['noin']} collect_failed={a['cf']}")
        tv += a["valid"]
        th += a["hit"]
    print(f"  pooled: {th}/{tv} = {th / tv:.1%} (noin={sum(a['noin'] for a in agg.values())}, "
          f"cf={sum(a['cf'] for a in agg.values())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
