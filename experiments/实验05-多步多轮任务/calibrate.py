#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动难度校准器 · 对抗 benchmark saturation

为什么需要它
    实验05 手工校准难度花了 3 轮约 70 分钟，其中两轮得到 pass@1 = 1.0 纯属浪费。
    根因：pass^k 的敏感性 = f(任务难度, 失败随机性)。任务太简单时
    pass^1 = pass^3 = pass^5 = 1.0，跑 k 次毫无信息量。
    模型每进化一代，难度曲线整体右移 → 今天校准好的数据明天就饱和。
    所以该沉淀的不是「某套数据」，而是「针对任意模型快速校准出有区分度评测集」的能力。

做什么
    生成数据 → 跑 pilot → 读 pass@1 → 调难度旋钮 → 再跑，直到落入目标区间。

用法
    python3 calibrate.py --target 0.75 --tol 0.15 --pilot-n 8 --max-iter 5
    python3 calibrate.py --model openai/gpt-oss-120b:free --target 0.7

产出
    data_calibrated/      可直接喂给 multistep_multiturn.py --data-dir
    calibration_report.json  每轮 (难度参数, pass@1, 失败模式) —— 本身即模型能力画像

实现说明
    ★ 只负责「生成数据 + 调度 pilot + 读结果」，判定逻辑完全复用 multistep_multiturn.py。
      两者靠 --data-dir 解耦，所以换数据不需要改判定器一行代码。
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "multistep_multiturn.py"
OUT_DATA = HERE / "data_calibrated"
REPORT = HERE / "calibration_report.json"

# 城市名池（生成数据时按需取用）
CITY_POOL = ["北京", "上海", "广州", "深圳", "成都", "西安", "杭州", "武汉", "南京",
             "重庆", "天津", "苏州", "青岛", "郑州", "长沙", "沈阳", "大连", "宁波",
             "无锡", "厦门", "福州", "济南", "合肥", "昆明", "南宁"]

WEATHER_DRY = ["晴", "多云", "阴"]
WEATHER_WET = ["小雨", "雷阵雨", "中雨", "阵雨"]


# ───────────────────────── 数据生成 ─────────────────────────
def gen_data(out_dir: Path, n_files: int, n_dry: int, n_wet: int,
             gap: int, seed: int) -> dict:
    """生成 n_files 个城市天气文件。

    关键约束：
      1) 正解（rain=false）城市必须**分散在不同文件**，漏读任一文件必错
      2) 干扰（rain=true）城市里要有几个温度高于多数入选城市 —— 筛错必然连锁错排序
    """
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("cities_*.json"):
        p.unlink()

    if n_dry + n_wet > len(CITY_POOL):
        raise SystemExit(f"城市数超出名字池（{len(CITY_POOL)}）")

    names = rng.sample(CITY_POOL, n_dry + n_wet)
    dry_names, wet_names = names[:n_dry], names[n_dry:]

    # 入选城市温度：从 35 起，按 gap 递减 → gap 越小排序越难
    dry = [{"city": c, "weather": rng.choice(WEATHER_DRY),
            "temp": 35 - i * gap, "rain": False}
           for i, c in enumerate(dry_names)]
    # 干扰城市温度：集中在入选区间的中上部，制造「下雨但更热」的陷阱
    lo, hi = min(d["temp"] for d in dry), max(d["temp"] for d in dry)
    wet = [{"city": c, "weather": rng.choice(WEATHER_WET),
            "temp": rng.randint(lo, hi + 2), "rain": True}
           for c in wet_names]

    # 分配：先给每个文件塞 1 个入选城市（保证分散），剩余的轮询均分
    buckets: list[list] = [[] for _ in range(n_files)]
    for i, d in enumerate(dry):
        buckets[i % n_files].append(d)
    rest = list(wet)
    rng.shuffle(rest)
    for i, w in enumerate(rest):
        buckets[i % n_files].append(w)
    for b in buckets:
        rng.shuffle(b)

    files = []
    for i, b in enumerate(buckets):
        p = out_dir / f"cities_{chr(ord('a') + i)}.json"
        p.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(p)

    dry_sorted = sorted(dry, key=lambda r: -r["temp"])
    return {
        "n_files": n_files, "n_dry": n_dry, "n_wet": n_wet, "gap": gap, "seed": seed,
        "expected": [r["city"] for r in dry_sorted],
        "avg": round(sum(r["temp"] for r in dry_sorted) / len(dry_sorted), 1),
        "probe_city": dry_sorted[-1]["city"], "probe_temp": dry_sorted[-1]["temp"],
        "files": [p.name for p in files],
    }


# ───────────────────────── 跑 pilot ─────────────────────────
def run_pilot(data_dir: Path, n: int, model: str, temp: float, tag: str) -> dict:
    """调用 multistep_multiturn.py 跑一轮 pilot，只读轮1（不开多轮，省时）。返回 summary。

    ★ 09-10 修两处（此前「API 全流程未跑通」却查不出原因）：
      1) 子进程输出改为落 logs_pilot_<tag>.log，不再 DEVNULL 吞掉 ——
         失败时能看到真实报错，而不是只有一句「summary 未生成」。
      2) 跑之前删掉 summary_<tag>.json 残留 ——
         否则本轮若失败，会把上一轮结果读进来当成本轮成绩 → 假收敛。
    """
    cmd = [sys.executable, str(RUNNER), "--tag", tag, "--n", str(n),
           "--data-dir", str(data_dir), "--with-avg", "--model", model,
           "--temp", str(temp)]

    sfile = HERE / f"summary_{tag}.json"
    logf = HERE / f"logs_pilot_{tag}.log"
    sfile.unlink(missing_ok=True)          # ★ 防读到残留

    with open(logf, "w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(HERE), check=False,
                              stdout=fh, stderr=subprocess.STDOUT)

    if not sfile.exists():
        tail = logf.read_text(encoding="utf-8", errors="replace")[-1000:]
        return {"pass@1": None,
                "error": f"summary 未生成（退出码 {proc.returncode}）",
                "log": logf.name, "log_tail": tail}
    return json.loads(sfile.read_text(encoding="utf-8"))


# ───────────────────────── 校准循环 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=0.75, help="目标 pass@1")
    ap.add_argument("--tol", type=float, default=0.15, help="容差，|p-target|<=tol 即收敛")
    ap.add_argument("--pilot-n", type=int, default=8, help="每轮 pilot 样本数（粗调，别太大）")
    ap.add_argument("--max-iter", type=int, default=5)
    ap.add_argument("--model", default="nvidia/nemotron-3.5-lightning:free")
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    # 难度旋钮：n_dry（入选城市数 = 排序链长度，最有效）、gap（温度间距）、n_files（步骤数）
    n_dry, gap, n_files, n_wet = 5, 2, 3, 4
    history, best = [], None
    t0 = time.time()

    print(f"[calib] target={a.target}±{a.tol} pilot_n={a.pilot_n} model={a.model}", flush=True)

    for it in range(1, a.max_iter + 1):
        cfg = gen_data(OUT_DATA, n_files, n_dry, n_wet, gap, a.seed + it)
        tag = f"calib{it}"
        s = run_pilot(OUT_DATA, a.pilot_n, a.model, a.temp, tag)
        p = s.get("pass@1")
        rec = {"iter": it, "n_files": n_files, "n_dry": n_dry, "n_wet": n_wet,
               "gap": gap, "pass@1": p, "expected": cfg["expected"],
               "avg": cfg["avg"], "fail_modes": s.get("fail_modes", {}),
               "N": s.get("N"), "errors": s.get("errors"),
               "error": s.get("error"), "log": s.get("log")}
        history.append(rec)

        dist = abs(p - a.target) if p is not None else 9
        if best is None or dist < best[0]:
            best = (dist, it)
        print(f"  轮{it}: files={n_files} dry={n_dry} gap={gap} "
              f"→ pass@1={p} (N={s.get('N')}) fails={s.get('fail_modes', {})}", flush=True)

        if p is None:
            print(f"        ✗ {s.get('error')}", flush=True)
            print(f"        日志 → {s.get('log')}", flush=True)
            tail = (s.get("log_tail") or "").strip()
            if tail:
                print("        ── 日志尾部（最后 12 行）──", flush=True)
                for line in tail.splitlines()[-12:]:
                    print(f"        │ {line}", flush=True)

        if p is not None and abs(p - a.target) <= a.tol:
            print(f"\n[calib] ✅ 收敛：难度档 files={n_files} dry={n_dry} gap={gap} "
                  f"→ pass@1={p}", flush=True)
            break

        # 调难度：太简单 → 加长排序链 + 缩小温度间距（gap 最小 1）
        if p is None or p > a.target + a.tol:
            n_dry += 2
            gap = max(1, gap - 1)
            n_files = min(6, 3 + (n_dry - 5) // 3)
            n_wet = max(4, n_dry - 1)
            why = "太简单 → 加长排序链/缩小间距/加文件"
        else:
            n_dry = max(3, n_dry - 2)
            gap += 1
            n_files = max(2, n_files - 1)
            n_wet = max(3, n_dry - 1)
            why = "太难 → 缩短排序链/拉大间距/减文件"
        print(f"        {why}", flush=True)
    else:
        print(f"\n[calib] ⚠ {a.max_iter} 轮内未收敛，取最接近的一档（轮{best[1]}）", flush=True)
        for r in history:
            if r["iter"] == best[1]:
                n_files, n_dry, gap, n_wet = r["n_files"], r["n_dry"], r["n_wet"], r["gap"]
        cfg = gen_data(OUT_DATA, n_files, n_dry, n_wet, gap, a.seed + 999)

    report = {"target": a.target, "tol": a.tol, "model": a.model, "temp": a.temp,
              "pilot_n": a.pilot_n, "elapsed_sec": round(time.time() - t0, 1),
              "final": {"n_files": n_files, "n_dry": n_dry, "n_wet": n_wet, "gap": gap},
              "history": history}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[calib] 报告 → {REPORT}")
    print(f"[calib] 数据 → {OUT_DATA}（可直接 --data-dir data_calibrated 开正式跑）")


if __name__ == "__main__":
    main()
