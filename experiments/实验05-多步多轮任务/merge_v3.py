#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并 v3 的 pilot(10) + formal(20) = N=30 样本，产出最终统计。

★ 为什么必须单独写这个文件：
  formal 用了 --mt-first 10，即只有前 10 条跑「严苛多轮」（4 轮 + 干扰 + 追问天津 25℃），
  后 10 条走的是「普通模式」（2 轮 + 追问西安 35℃）。两者难度完全不同，
  混在一起算 B10 保持率会污染结论 —— 必须按 multi_turn 标记分开统计。

输出：summary_merged_v3.json
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(tag: str) -> list:
    p = HERE / f"raw_{tag}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def pass_at_k(oks: list, k: int):
    """把 N 次分成 N//k 组，每组 k 次全过才算过。"""
    g = len(oks) // k
    if g == 0:
        return None
    return sum(1 for i in range(g) if all(oks[i * k:(i + 1) * k])) / g


def main():
    pilot, formal = load("pilot_v3"), load("formal_v3")
    allr = pilot + formal                      # 保持时间顺序，pass^k 分组才成立

    oks1 = [r["round1"]["passed"] for r in allr if "round1" in r]

    mt = [r for r in allr if r.get("multi_turn") and "round2" in r]
    nm = [r for r in allr if not r.get("multi_turn") and "round2" in r]
    oks_mt = [r["round2"]["passed"] for r in mt]
    oks_nm = [r["round2"]["passed"] for r in nm]

    fails, files_dist, avg_dist = {}, {}, {}
    for r in allr:
        r1 = r.get("round1", {})
        f = r1.get("primary_fail")
        if f:
            fails[f] = fails.get(f, 0) + 1
        k = len(r1.get("files_read", []))
        files_dist[k] = files_dist.get(k, 0) + 1
        g = str(r1.get("avg_got"))
        avg_dist[g] = avg_dist.get(g, 0) + 1

    out = {
        "说明": "pilot_v3(10) + formal_v3(20) 合并；B10 按 multi_turn 分离口径",
        "model": "nvidia/nemotron-3.5-lightning:free", "temp": 0.7,
        "expected": ["西安", "南京", "北京", "郑州", "成都", "青岛", "武汉", "重庆", "天津"],
        "avg_truth": 29.4,
        "N_round1": len(oks1),
        "errors": sum(1 for r in allr if r.get("error")),
        "pass@1": round(sum(oks1) / len(oks1), 4),
        "pass^3": round(pass_at_k(oks1, 3), 4),
        "pass^5": round(pass_at_k(oks1, 5), 4),
        "B10_严苛多轮(4轮+干扰+追问天津25)": {
            "N": len(oks_mt),
            "保持率": round(sum(oks_mt) / len(oks_mt), 4) if oks_mt else None},
        "B10_普通对照(2轮+追问西安35)": {
            "N": len(oks_nm),
            "保持率": round(sum(oks_nm) / len(oks_nm), 4) if oks_nm else None},
        "fail_modes": fails,
        "files_read_dist": files_dist,
        "avg_got_dist": avg_dist,
    }
    (HERE / "summary_merged_v3.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
