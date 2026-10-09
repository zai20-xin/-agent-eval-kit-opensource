#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实验 06 · 工具调用遗漏（A1）—— 遗漏率随步骤数的变化

要补的缺口
    清单 A1「工具调用遗漏」此前只有实验04 的 1/15 单次观察，未复现。

★ v1 设计已证伪（重要，别再踩）
    原假设「模型自以为知道 → 不调工具」。实测 20 次（知名/虚构城市 × 明确/模糊指令）
    **全部调用了工具**，A1 触发 0 次。结论：该模型被训练成「有工具就用」，
    **单次提问骗不出它**。

v2 假设（本版）
    遗漏不发生在「要不要调」，而发生在**多步任务的后续步骤里**——
    需要 N 次独立查询时，查到第 k 个就开始作答。
    （旁证：实验05 v3 的 F1 样本 = 30 条里有 1 只读了 4/5 个文件，那正是 A1 的实例）

设计
    任务：查询 N 个城市的天气，返回其中最热的。工具只接受单个 city → 必须调 N 次。
    变量：N ∈ {2..8}，每档跑 n 次。
    判定（确定性）：实际调用的城市集合 vs 目标集合，缺一个即算遗漏。

用法
    python3 tool_call_omission.py --n 10                  # 7 档 × 10 = 70 次
    python3 tool_call_omission.py --n 10 --ns 3,5,7
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("BARE_AGENT_MODEL", "nvidia/nemotron-3.5-lightning:free")

# 城市按固定顺序取前 N 个，温度刻意打散（不随顺序单调，避免模型靠猜）
CITY_ORDER = ["北京", "上海", "广州", "深圳", "成都", "西安", "杭州", "武汉"]
WEATHER = {"北京": 22, "上海": 28, "广州": 33, "深圳": 31,
           "成都": 25, "西安": 29, "杭州": 27, "武汉": 30}


def get_weather(city: str) -> str:
    if city not in WEATHER:
        return json.dumps({"error": f"unknown city: {city}"}, ensure_ascii=False)
    return json.dumps({"city": city, "temp": WEATHER[city],
                       "weather": "晴" if WEATHER[city] > 28 else "多云"},
                      ensure_ascii=False)


TOOLS = [{"type": "function", "function": {
    "name": "get_weather",
    "description": "查询单个城市的天气，返回 city/temp(摄氏度)/weather 三字段",
    "parameters": {"type": "object",
                   "properties": {"city": {"type": "string", "description": "城市名"}},
                   "required": ["city"]}}}]
REGISTRY = {"get_weather": get_weather}


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
        m = re.search(r'(?:export\s+)?OPENROUTER_API_KEY\s*=\s*["\']?([^"\'\n]+)', t)
        if m:
            return m.group(1).strip()
    raise SystemExit("找不到 OPENROUTER_API_KEY")


def chat_with_retry(client, kw, tries=3):
    last = None
    for i in range(tries):
        try:
            return client.chat.completions.create(**kw)
        except Exception as e:
            last = e
            time.sleep(1.5 ** i)
    raise last


def one_run(client, model, n_cities, idx, max_turns=14):
    """一次 run：让 agent 查 n_cities 个城市并给出最热的。"""
    cities = CITY_ORDER[:n_cities]
    hottest = max(cities, key=lambda c: WEATHER[c])
    prompt = (f"请查询以下 {n_cities} 个城市的天气，然后告诉我其中温度（temp）最高的是哪个城市："
              f"{'、'.join(cities)}。\n只回答城市名，不要解释。")
    rec = {"idx": idx, "n_cities": n_cities, "targets": cities,
           "hottest_truth": hottest, "called": [], "error": None}
    try:
        msgs = [{"role": "user", "content": prompt}]
        for _ in range(max_turns):
            resp = chat_with_retry(client, {"model": model, "messages": msgs,
                                            "tools": TOOLS, "tool_choice": "auto"})
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls:
                rec["answer"] = msg.content or ""
                break
            msgs.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments}}
                                        for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments) if c.function.arguments else {}
                    out = REGISTRY[c.function.name](**args)
                    if args.get("city"):
                        rec["called"].append(args["city"])
                except Exception as e:
                    out = json.dumps({"error": str(e)}, ensure_ascii=False)
                msgs.append({"role": "tool", "tool_call_id": c.id, "content": out})
        else:
            rec["answer"] = "<max_turns reached>"

        got = set(rec["called"])
        rec["missing"] = sorted(set(cities) - got)          # ★ A1 判据：漏查的城市
        rec["extra"] = sorted(got - set(cities))            # 查了不该查的（噪声）
        rec["omission"] = len(rec["missing"]) > 0
        rec["n_calls"] = len(rec["called"])
        rec["dup_calls"] = len(rec["called"]) - len(got)     # 重复调用同一城市
        rec["answer_correct"] = rec["hottest_truth"] in (rec.get("answer") or "")
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="每档样本数")
    ap.add_argument("--ns", default="2,3,4,5,6,7,8", help="城市数档位")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--sleep", type=float, default=0.3)
    a = ap.parse_args()

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=load_key())
    out = HERE / "raw.json"
    results, t0, i = [], time.time(), 0
    ns = [int(x) for x in a.ns.split(",")]
    print(f"[exp06v2] model={a.model} 档位={ns} 每档 n={a.n} 总计 {len(ns) * a.n} 次",
          flush=True)

    for n_cities in ns:
        for _ in range(a.n):
            r = one_run(client, a.model, n_cities, i)
            results.append(r)
            print(f"  #{i:03d} N={n_cities} 调用{len(r.get('called', []))}次 "
                  f"漏={r.get('missing') or '无'} 答案对={r.get('answer_correct')} "
                  f"{r.get('error') or ''}", flush=True)
            out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            time.sleep(a.sleep)
            i += 1

    ok = [r for r in results if not r.get("error")]
    summary = {"model": a.model, "temp": a.temp, "N_total": len(results),
               "errors": len(results) - len(ok),
               "elapsed_sec": round(time.time() - t0, 1),
               "A1_总遗漏率": (round(sum(1 for r in ok if r.get("omission")) / len(ok), 4)
                          if ok else None),
               "按档位": {}}
    for n_cities in ns:
        rows = [r for r in ok if r["n_cities"] == n_cities]
        if not rows:
            continue
        miss = sum(len(r.get("missing", [])) for r in rows)
        summary["按档位"][f"N={n_cities}"] = {
            "N": len(rows),
            "遗漏率": round(sum(1 for r in rows if r.get("omission")) / len(rows), 4),
            "漏查城市总数": miss,
            "平均漏查数": round(miss / len(rows), 3),
            "答案正确率": round(sum(1 for r in rows if r.get("answer_correct")) / len(rows), 4),
            "平均调用次数": round(sum(r.get("n_calls", 0) for r in rows) / len(rows), 2),
        }
    (HERE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print("\n[SUMMARY]", json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
