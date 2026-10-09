#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实验 07 · 坐实「temp=0 仍不确定」的方差来源

背景
    「为什么 temp=0 仍不确定」长期只有【推断·待验证】的归因，四种候选机理无法区分，
    并明确写了「坐实之前不得对外声称」。本实验做坐实。

原计划 vs 本实验的升级
    原计划：换「直连端点」（非 OpenRouter 路由）重跑 temp=0 × N≥30。
    ★ 局限：换端点往往也换模型（OpenRouter 上的免费 nemotron 本地跑不了），
      于是「路径」和「模型」两个变量缠在一起，结论不干净。

    本实验改为**完全本地、单实例、零路由**，把路径变量压到 0，然后只动一个东西：

        B1：同一 prompt  × 同一模型 × temp=0 × **串行** N=30
        B2：同一 prompt  × 同一模型 × temp=0 × **并发 5** N=30

    → 两者唯一差别是**请求是否被并行批处理**。
      若 B2 的一致性显著低于 B1，则直接坐实「**批处理变化 → 浮点非结合性 → 输出翻转**」（机理 2）。

判定（全确定性代码，零 judge）
    字面一致率 = 最高频回答的占比 ｜ unique = N 次里出现几种不同回答
    语义正确率 = 是否含 晴 / 32 / 不带伞 三项（对齐 ground truth）

用法
    python3 temp0_source.py            # 跑 B1 + B2
    python3 temp0_source.py --n 30     # 指定样本数

结论边界（不许外推）
    本实验只验证**批处理**这一条机理。机理 1（供应商路由）仍需跨 provider 对照，
    不在本实验射程内 —— 不得据此宣称「路由不是原因」，只能说「批处理是一个已坐实的原因」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).resolve().parent

# ── Ollama 本地直连（零路由、单实例、无网络）─────────────────────────
BASE_URL = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct-q4_K_M"

# ── prompt 完全复用实验03，保证与 A 组（历史 OpenRouter 数据）可比 ──────
SYSTEM_PROMPT = """你是一个天气助手。
你会拿到工具返回的真实数据，必须严格基于这些数据回答，不能编造工具没返回的信息。
回答简洁，必须给出三项：天气状况、气温、以及是否需要带伞的明确结论。"""
QUESTION = "北京今天天气怎么样？需要带伞吗？"
GROUND_TRUTH = {"weather": "晴", "temp": 32, "rain": False}

USER_MSG = (f"{QUESTION}\n\n【工具返回的真实数据】\n"
            f"{json.dumps(GROUND_TRUTH, ensure_ascii=False)}")


def ask(client: OpenAI, temperature: float = 0.0) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": USER_MSG}],
        temperature=temperature,
        timeout=180,
    )
    return (r.choices[0].message.content or "").strip()


def semantic_ok(t: str) -> bool:
    """确定性语义判定：三个事实点是否都在。

    ★ 判定器自测记录（2026-09-10）：首版只认「不需要带伞」这种连写形态，
      而模型实际输出是「是否需要带伞：不需要」（冒号 + 拆开），
      导致**完全正确的答案被判 0%** —— 与 C 组「判定器必须先自测」是同一条教训。
      现将「不带伞」的判据改为短语族匹配，任一命中即可。
    """
    has_weather = "晴" in t
    has_temp = "32" in t
    no_umbrella = any(k in t for k in [
        "不需要带伞", "不用带伞", "无需带伞", "不必带伞", "不需要伞", "不用伞",
        "不需要", "不用带", "无需带", "不必带",   # 兼容「是否需要带伞：不需要」这类拆写
    ])
    # 反向保护：若明确说了要带伞，则不算通过
    if any(k in t for k in ["需要带伞：是", "建议带伞", "需要携带雨伞", "需要带伞哦"]):
        return False
    return has_weather and has_temp and no_umbrella


def summarize(replies: list[str]) -> dict:
    c = Counter(replies)
    top, cnt = c.most_common(1)[0]
    ok = sum(1 for r in replies if semantic_ok(r))
    return {
        "n": len(replies),
        "unique": len(c),
        "top_share": round(cnt / len(replies), 3),
        "top_reply": top[:80],
        "semantic_pass": ok,
        "semantic_rate": round(ok / len(replies), 3),
        "dist": [{"reply": k[:60], "count": v} for k, v in c.most_common(6)],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--only", choices=["b1", "b2"], default=None)
    a = ap.parse_args()

    client = OpenAI(base_url=BASE_URL, api_key="ollama")

    # 预热 + 连通性检查（避免把冷启动差异算进方差）
    print(f"[exp07] 端点={BASE_URL} 模型={MODEL} temp=0 N={a.n}", flush=True)
    try:
        warm = ask(client)
        print(f"[exp07] 预热 OK（{len(warm)} 字）\n", flush=True)
    except Exception as e:
        print(f"✗ 预热失败：{type(e).__name__}: {e}")
        print("  检查：ollama 是否在跑（ollama list / curl 127.0.0.1:11434）")
        sys.exit(1)

    results = {}

    # ── B1 串行 ──────────────────────────────────────────────
    if a.only in (None, "b1"):
        print("=" * 70)
        print("B1 · 串行（每次请求单独发，无并发批处理）")
        print("=" * 70)
        t0 = time.time()
        reps = []
        for i in range(1, a.n + 1):
            try:
                t = ask(client)
            except Exception as e:
                print(f"  #{i:02d} ✗ {type(e).__name__}: {e}")
                continue
            reps.append(t)
            print(f"  #{i:02d} [{len(t):3d}字] {t[:58].replace(chr(10), ' ')}")
            sys.stdout.flush()
        s1 = summarize(reps)
        s1["elapsed_s"] = round(time.time() - t0, 1)
        s1["replies"] = reps
        results["B1_serial"] = s1
        print(f"\n  → unique={s1['unique']}/{s1['n']}  最高频占比={s1['top_share']:.0%}  "
              f"语义正确率={s1['semantic_rate']:.0%}  耗时={s1['elapsed_s']}s\n")

    # ── B2 并发 ──────────────────────────────────────────────
    if a.only in (None, "b2"):
        print("=" * 70)
        print(f"B2 · 并发 {a.concurrency}（同时发，触发批处理）")
        print("=" * 70)
        t0 = time.time()
        reps2: list[str] = []
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = [ex.submit(ask, client) for _ in range(a.n)]
            for i, f in enumerate(futs, 1):
                try:
                    t = f.result()
                except Exception as e:
                    print(f"  #{i:02d} ✗ {type(e).__name__}: {e}")
                    continue
                reps2.append(t)
                print(f"  #{i:02d} [{len(t):3d}字] {t[:58].replace(chr(10), ' ')}")
                sys.stdout.flush()
        s2 = summarize(reps2)
        s2["elapsed_s"] = round(time.time() - t0, 1)
        s2["replies"] = reps2
        results["B2_concurrent"] = s2
        print(f"\n  → unique={s2['unique']}/{s2['n']}  最高频占比={s2['top_share']:.0%}  "
              f"语义正确率={s2['semantic_rate']:.0%}  耗时={s2['elapsed_s']}s\n")

    # ── 对比 ────────────────────────────────────────────────
    print("=" * 70)
    print("汇总对照")
    print("=" * 70)
    print(f"{'组':<16}{'unique':>8}{'最高频占比':>12}{'语义正确率':>12}{'耗时':>9}")
    for k, v in results.items():
        print(f"{k:<16}{v['unique']:>4}/{v['n']:<3}{v['top_share']:>11.0%}"
              f"{v['semantic_rate']:>12.0%}{v['elapsed_s']:>8}s")
    print("\n历史 A 组（实验03 · OpenRouter 路由 + nemotron-3.5-lightning, N=10）："
          "\n  unique=7/10，最高频占比 40% —— 跨后端路由的高方差参考点")

    if "B1_serial" in results and "B2_concurrent" in results:
        d = results["B1_serial"]["top_share"] - results["B2_concurrent"]["top_share"]
        print(f"\n串行 vs 并发 一致率差：{d:+.0%}")
        if d > 0.1:
            print("  → 并发一致性明显更低 = **批处理是一个已坐实的方差来源**（机理 2）")
        elif abs(d) <= 0.1:
            print("  → 两者接近：本模型上批处理影响不显著（不代表无影响，可能被量化/规模掩盖）")
        else:
            print("  → 并发反而更一致：异常方向，需重复验证，先不要下结论")

    out = HERE / "raw.json"
    out.write_text(json.dumps({
        "experiment": "实验07-temp=0不确定性来源",
        "endpoint": BASE_URL, "model": MODEL, "temperature": 0,
        "n": a.n, "concurrency": a.concurrency,
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "零路由单实例本地直连；prompt 复用实验03 保证可比",
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ raw 存档已写入: {out}")


if __name__ == "__main__":
    main()
