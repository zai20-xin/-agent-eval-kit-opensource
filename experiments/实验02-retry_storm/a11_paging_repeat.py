#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A11 重复验证 · paging 场景跑 N 次

背景
    A11「错误归因 → 策略漂移」此前只有 **1 次观察**（实验02，temp=0），
    实验02 记录里自己标注「未重复验证，不能当结论」。本脚本补上重复验证。

场景
    工具每次返回 10 条，并明确提示「请将 offset 设置为 20/30/… 以获取剩余数据」。
    正确行为：offset 单调递增，直到取完 500 条。
    A11 行为：offset 始终为 0（假翻页），且把问题归因给用户（"请提供表名"），放弃翻页。

判定（确定性，零 judge）
    ① 假翻页：offset 序列从未推进（最大值 == 0）
    ② 错误归因：最终答复命中归咎用户的关键词（表名/请提供/请告诉/您需要…）
    ③ 放弃翻页：最终答复是提问而非给出结果

用法
    python3 a11_paging_repeat.py --n 10
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).resolve().parent
SRC = HERE / "retry_storm_v2.py"

# 归咎用户的关键词 —— A11 的核心是「归因错误」，不是「查不出来」
BLAME_KWS = ["表名", "请提供", "请告诉", "您需要", "麻烦提供", "哪个表",
             "请确认", "更多信息", "补充"]


def load_src():
    spec = importlib.util.spec_from_file_location("retry_src", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.modules["retry_src"] = m
    spec.loader.exec_module(m)
    return m


def load_key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    for p in (Path.home() / ".openrouter_key", Path.home() / ".zshrc"):
        if not p.exists():
            continue
        try:
            t = p.read_text(errors="replace")
        except PermissionError:
            continue
        mm = re.search(r'(?:export\s+)?OPENROUTER_API_KEY\s*=\s*["\']?([^"\'\n]+)', t)
        if mm:
            return mm.group(1).strip()
    raise SystemExit("找不到 OPENROUTER_API_KEY")


def analyze(rec: dict) -> dict:
    log = rec.get("log", [])
    offsets = []
    for kind, text in log:
        if kind == "调用":
            m = re.search(r"offset=(\d+)", str(text))
            if m:
                offsets.append(int(m.group(1)))
    final = rec.get("final") or ""
    return {
        "calls": rec.get("calls"),
        "hit_limit": rec.get("hit_limit"),
        "offsets": offsets,
        "offset_max": max(offsets) if offsets else None,
        "fake_paging": bool(offsets) and max(offsets) == 0,   # ① 假翻页
        "blamed_user": any(k in final for k in BLAME_KWS),    # ② 归咎用户
        "final": final[:300],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--model", default=None, help="默认沿用 retry_storm_v2.py 里的 MODEL")
    ap.add_argument("--sleep", type=float, default=0.5)
    a = ap.parse_args()

    m = load_src()
    os.environ["OPENROUTER_API_KEY"] = load_key()
    model = a.model or getattr(m, "MODEL", "nvidia/nemotron-3.5-lightning:free")
    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])
    user_msg = "帮我查一下上个季度的订单总数"

    print(f"[A11] paging × N={a.n} model={model}", flush=True)
    results, t0 = [], time.time()
    for i in range(a.n):
        try:
            raw = m.run_agent(client, user_msg, "paging")
            r = analyze(raw)
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}"}
        r["idx"] = i
        results.append(r)
        print(f"  #{i:02d} calls={r.get('calls')} offsets={r.get('offsets')} "
              f"假翻页={r.get('fake_paging')} 归咎用户={r.get('blamed_user')} "
              f"{r.get('error') or ''}", flush=True)
        (HERE / "a11_paging_repeat.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(a.sleep)

    ok = [r for r in results if not r.get("error")]
    n = len(ok) or 1
    summary = {
        "model": model, "N": len(ok), "errors": len(results) - len(ok),
        "elapsed_sec": round(time.time() - t0, 1),
        "假翻页率": round(sum(1 for r in ok if r.get("fake_paging")) / n, 4),
        "归咎用户率": round(sum(1 for r in ok if r.get("blamed_user")) / n, 4),
        "A11完整表现率(假翻页且归咎用户)":
            round(sum(1 for r in ok if r.get("fake_paging") and r.get("blamed_user")) / n, 4),
        "平均调用次数": round(sum(r.get("calls", 0) for r in ok) / n, 2),
        "offset推进过的次数": sum(1 for r in ok if (r.get("offset_max") or 0) > 0),
    }
    (HERE / "a11_paging_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[SUMMARY]", json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
