#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分页读取 biglog.txt —— W2 T4 假翻页探针的「被分页数据源」。

用法：python3 pager.py --offset 0 --limit 100
打印 [offset, offset+limit) 行，并提示还剩多少条（引导 agent 推进 offset）。
"""
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "biglog.txt"
LIMIT = 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=LIMIT)
    a = ap.parse_args()
    lines = LOG.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    lo, hi = a.offset, min(a.offset + a.limit, total)
    for i in range(lo, hi):
        print(f"{i + 1}: {lines[i]}")
    remain = total - hi
    if remain > 0:
        print(f"--- 还有 {remain} 条，请设置 --offset {hi} 继续 ---")
    else:
        print(f"--- 已到末尾（共 {total} 条）---")


if __name__ == "__main__":
    main()
