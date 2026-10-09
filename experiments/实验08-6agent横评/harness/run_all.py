#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 轨编排 —— agent × task × N 跑批，落 results/<agent>/<task>/<run>/。

用法：
    python3 run_all.py --dry-run            # 只打印计划，不调 agent（D1 验证用）
    python3 run_all.py                      # 真跑（需 API key + 各 agent 已装）
    python3 run_all.py --agents pi --tasks T1 --n 2

每个 run 落盘：
    results/<agent>/<task>/<run>/trace.jsonl   # 归一化轨迹（可重跑判定）
    results/<agent>/<task>/<run>/result.json   # 判定输出（hit / metric / detail）

⚠️ 只跑 A 轨（pi / opencode / mimo）。B 轨（QClaw / AgentW）由用户在 GUI 执行，
   离线解析脚本在 D3 补（parse_b_track.py）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HARNESS = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HARNESS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evalkit import trace_detectors as TD
import normalize  # noqa: F401  (normalize 经 driver 间接用到，显式 import 保路径)
import driver

TASKS_PATH = HARNESS.parent / "tasks" / "tasks.json"
RESULTS = HARNESS.parent / "results"
FIXTURES = HARNESS.parent / "tasks" / "fixtures"


def reset_fixtures():
    """每个 run 之前把 fixtures 恢复到 git HEAD 的干净状态。

    ⚠️ 必要性（2026-09-11 D2 发现，属实验设计缺陷）：
       T2b（按 spec 生成函数）、T5（建 config.py + 改 README.md）都会让 agent
       **真的落盘改文件**。跑 N 次时第 2 次起，agent 看到的是上一轮留下的结果 ——
       于是「多步状态保持」「注入是否中招」的判定全部失真，且第 1 次与第 2/3 次
       根本不是同一道题。副作用任务必须每轮重置，否则 N 次不是重复采样而是三个不同的实验。

    安全边界：只针对 tasks/fixtures/ 这一个目录，不动仓库其他文件；
    fixtures 已在 D1（commit db298f0）提交，checkout 不会吞掉任何未提交的工作成果。
    """
    subprocess.run(["git", "-C", str(ROOT), "checkout", "--", str(FIXTURES)],
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(ROOT), "clean", "-fdq", str(FIXTURES)],
                   capture_output=True, text=True)


def load_tasks():
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def detector_for(task):
    """tasks.json 里的 detector 名 + 参数 → evalkit.trace_detectors 函数。"""
    fn = getattr(TD, task["detector"])
    return fn, task.get("detector_params", {})


def run_once(agent, task, run_id):
    reset_fixtures()  # 副作用任务（T2b/T5）必须每轮复位，否则 N 次不是重复采样
    out = RESULTS / agent / task["id"] / run_id
    out.mkdir(parents=True, exist_ok=True)
    model = driver.AUTO_AGENTS[agent]["default_model"]
    tr = driver.run_agent(agent, task["prompt"], str(out), model=model)
    fn, params = detector_for(task)
    valid, why = TD.is_trace_valid(tr)
    if valid:
        res = fn(tr, **params)
    else:
        # C6：无效轨迹不判定，避免「采集失败」被记成「失败模式命中」
        res = {"hit": None, "metric": {}, "detail": f"无效轨迹，未判定（{why}）"}
    res = {**res, "valid": valid, "valid_reason": why}
    (out / "trace.jsonl").write_text(
        json.dumps(tr.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "result.json").write_text(
        json.dumps({"agent": agent, "task": task["id"], "run": run_id,
                    "model": tr.model,
                    # 硬要求：模型必须是轨迹里读到的真实值，不是登记值
                    "model_confirmed": bool(tr.model),
                    "source": tr.source, **res},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def plan(agents, tasks, n):
    # ⚠️ 必须回传 task 字典本身，不能只回 t["id"]：
    #    run_once() 需要 task["detector"] / ["detector_params"]。
    #    2026-09-11 实测：D1 只跑 --dry-run（不进 run_once）所以没暴露这个断链。
    return [(a, t, i) for a in agents for t in tasks for i in range(1, n + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="pi,opencode,mimo")
    ap.add_argument("--tasks", default=None, help="逗号分隔 task id；默认全跑")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()

    agents = [x.strip() for x in a.agents.split(",") if x.strip()]
    tasks = load_tasks()["tasks"]
    if a.tasks:
        want = set(a.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in want]

    rows = plan(agents, tasks, a.n)
    print(f"=== A 轨跑批计划：{len(agents)} agent × {len(tasks)} task × {a.n} = {len(rows)} runs ===")
    for a_, t_, i_ in rows:
        print(f"  {a_:9s} {t_['id']:8s} run{i_}")

    if a.dry_run:
        print("\n[--dry-run] 未实际调用 agent。去掉该参数即真跑（D3-D4 用）。")
        return

    print("\n开始跑批…")
    for a_, t_, i_ in rows:
        rid = f"run{i_}"
        t0 = time.time()
        try:
            res = run_once(a_, t_, rid)
            dt = time.time() - t0
            # INV = 无效轨迹（采集失败），必须与「真的没命中」区分开
            flag = "HIT " if res["hit"] else ("INV " if not res.get("valid") else "ok  ")
            print(f"  {flag} {a_:9s} {t_['id']:8s} {rid}  {dt:5.1f}s  {res['detail']}")
        except Exception as e:
            # ★ 不吞异常：跑批 silent failure 是最贵的排障成本（见 driver._sh 注释里的同类坑）
            import traceback
            print(f"  ERR  {a_:9s} {t_['id']:8s} {rid}  {type(e).__name__}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
