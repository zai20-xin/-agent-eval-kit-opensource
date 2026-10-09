#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""τ-bench 外部套件桥接（A 桥接：外部跑完，结果导入本 kit）。

决策（2026-09-23 锁定）：
    · 每 domain 一 suite：`tau.airline` / `tau.retail` / `tau.telecom` /
      `tau.mock` / `tau.zh_cs`
    · **只读** `reward_info.reward`（决策⑧）—— 不在 kit 内复算 τ 判定
    · 导入走 `batch.new_external_batch` —— **不重判**、cases 内嵌、无 traces/
    · 产物进 `experiments/`（决策⑥），不改 layout TOP_DIRS_OK

results.json 形状（tau2）：
    {info, tasks[], simulations[{task_id, trial, reward_info:{reward,...}}]}

铁律：本模块是**导入器**，不是评测器 —— 不调 τ 的 evaluator、不读消息轨迹判分。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional, Sequence

from ..batch import new_external_batch
from ..outcome import RunOutcome, case_summary_from_outcome
from ..suite import Suite

# 每 domain 一 suite（决策：不设聚合的 `tau` 伞形套件）
DOMAINS: tuple[str, ...] = (
    "airline", "retail", "telecom", "mock", "zh_cs",
)

# 指纹：归套件（SWE 预留③）—— 只哈希本导入器；口径=「如何把 reward 落成 case」
_FINGERPRINT_SOURCES = ("tau.py",)
_SUITE_ROOT = Path(__file__).resolve().parent


def suite_for(domain: str) -> Suite:
    """`tau.<domain>` 元数据；未知 domain 也返回（id 仍规范），kind=external。"""
    d = str(domain).strip() or "unknown"
    return Suite(
        id=f"tau.{d}",
        title=f"τ-bench {d}（外部 reward 导入）",
        kind="external",
        description=(
            "A 桥接：外部 tau2 跑完 → 只读 reward_info.reward → "
            "new_external_batch；report 不重判。"
        ),
        fingerprint_sources=_FINGERPRINT_SOURCES,
        root=_SUITE_ROOT,
    )


# discover_suites 读取：一个模块导出多个 Suite（P-1 τ）
SUITES: dict[str, Suite] = {suite_for(d).id: suite_for(d) for d in DOMAINS}


def _tasks_sha256(tasks: Sequence[dict]) -> str:
    """题目指纹：只哈希任务定义，**不含** simulations/reward。

    同 domain 换模型重跑 → tasks 不变 → task_set.sha256 不变 → 可比；
    改任务/判定标准 → hash 变 → 不可比（可比性三要素之一）。
    """
    blob = json.dumps(list(tasks), sort_keys=True, ensure_ascii=False,
                      default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:16]


def parse_results(path: Path | str) -> dict:
    """读 tau2 `results.json` → 规范化 dict（不在此判分）。"""
    p = Path(path)
    if p.is_dir():
        p = p / "results.json"
    if not p.is_file():
        raise FileNotFoundError(f"results.json 不存在：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "simulations" not in data:
        raise ValueError(f"不是 tau2 results.json 形状：{p}")
    return data


def detect_domain(data: dict, *, path: Path | str | None = None,
                  explicit: Optional[str] = None) -> str:
    """domain 解析优先级：显式参数 > environment_info > 目录名 token > unknown。"""
    if explicit:
        return str(explicit).strip()
    env = (data.get("info") or {}).get("environment_info") or {}
    name = env.get("domain_name")
    if name:
        return str(name)
    known = set(DOMAINS)
    # 目录名形如 20260923_200545_zh_cs_llm_agent_...
    text = str(path or "")
    for tok in text.replace("-", "_").split("_"):
        if tok in known:
            return tok
    # 连续 token：zh + cs
    for a in known:
        if a in text:
            return a
    return "unknown"


def _reward_of(sim: dict) -> Optional[float]:
    """只读 reward_info.reward（决策⑧）；兼容 reward_info 为 JSON 字符串。"""
    ri = sim.get("reward_info")
    if isinstance(ri, str):
        try:
            ri = json.loads(ri)
        except json.JSONDecodeError:
            ri = None
    if not isinstance(ri, dict):
        # 极少数导出把 reward 提到顶层
        r = sim.get("reward")
        return float(r) if isinstance(r, (int, float)) else None
    r = ri.get("reward")
    if r is None:
        return None
    try:
        return float(r)
    except (TypeError, ValueError):
        return None


def _reward_breakdown(sim: dict) -> Optional[dict]:
    ri = sim.get("reward_info")
    if isinstance(ri, str):
        try:
            ri = json.loads(ri)
        except json.JSONDecodeError:
            return None
    if isinstance(ri, dict):
        rb = ri.get("reward_breakdown")
        return rb if isinstance(rb, dict) else None
    return None


def _reward_basis(sim: dict) -> Optional[list]:
    ri = sim.get("reward_info")
    if isinstance(ri, str):
        try:
            ri = json.loads(ri)
        except json.JSONDecodeError:
            return None
    if isinstance(ri, dict):
        rb = ri.get("reward_basis")
        return list(rb) if isinstance(rb, (list, tuple)) else None
    return None


def cases_from_results(data: dict, *, domain: str = "") -> tuple[list[dict], dict]:
    """simulations → case 级双字段 summary（按 task_id 聚合）+ counts。

    score = mean(reward) ∈[0,1] 高=好（reward 天然如此）；
    raw 保留全部 trial rewards 与 reward_breakdown 供审计；
    n/hits/rate：n=trial 数，hits=满奖励次数(reward>=1)，rate=满奖率
    （compare 的两比例 z 检验用它；score 与 rate 在有部分奖励时可以不同）。
    """
    tasks = list(data.get("tasks") or [])
    task_ids: list[str] = [t["id"] for t in tasks if isinstance(t, dict) and t.get("id")]
    labels = {
        t["id"]: (t.get("ticket") or t.get("description") or "")
        if isinstance(t.get("description"), str)
        else ((t.get("description") or {}).get("purpose") or t.get("ticket") or "")
        for t in tasks if isinstance(t, dict) and t.get("id")
    }

    sims = list(data.get("simulations") or [])
    by_task: dict[str, list] = {}
    for sim in sims:
        if not isinstance(sim, dict):
            continue
        tid = str(sim.get("task_id") or "_unknown")
        by_task.setdefault(tid, []).append(sim)

    # 保序：tasks 顺序，再补 simulations 里多出的 task
    order = [t for t in task_ids if t in by_task]
    order += [t for t in by_task if t not in order]

    cases: list[dict] = []
    n_runs = valid = invalid = 0
    full_pass_total = 0

    for tid in order:
        group = by_task[tid]
        rewards = [_reward_of(s) for s in group]
        valid_rs = [r for r in rewards if r is not None]
        n_runs += len(group)
        invalid += len(group) - len(valid_rs)
        valid += len(valid_rs)
        if not valid_rs:
            # 全部缺失 reward —— 单列 case，不伪装成 0 分任务失败
            oc = RunOutcome(
                case_id=tid, run=-1, score=0.0, metric="reward",
                raw={"rewards": [], "n_trials": len(group),
                     "note": "missing reward_info.reward"},
                suite_id=f"tau.{domain}" if domain else None,
            )
            summary = {
                "id": tid, "label": labels.get(tid) or tid,
                "score": 0.0, "metric": "reward",
                "raw": oc.raw, "ci": None,
                "n": 0, "hits": 0, "rate": 0.0,
                "no_output": 0, "collect_failed": len(group),
            }
            cases.append(summary)
            continue

        mean_r = sum(valid_rs) / len(valid_rs)
        hits = sum(1 for r in valid_rs if r >= 1.0)
        full_pass_total += hits
        breakdowns = [b for b in (_reward_breakdown(s) for s in group) if b]
        basis = next((b for b in (_reward_basis(s) for s in group) if b), None)

        oc = RunOutcome.from_reward(
            case_id=tid, run=-1, reward=mean_r,
            breakdown=breakdowns[-1] if breakdowns else None,
            suite_id=f"tau.{domain}" if domain else None,
        )
        # from_reward 已填 raw.reward；补 trial 级明细
        oc.raw.update({
            "rewards": valid_rs,
            "n_trials": len(group),
            "n_valid": len(valid_rs),
            "mean": mean_r,
            "full_passes": hits,
            "reward_basis": basis,
            "reward_breakdowns": breakdowns or None,
        })
        # case 级：score=mean reward；率值叙述用满奖率（compare 用）
        rate = hits / len(valid_rs)
        summary = case_summary_from_outcome(
            oc, n=len(valid_rs), hits=hits, rate=rate, ci=None,
            label=labels.get(tid) or tid,
            no_output=0, collect_failed=len(group) - len(valid_rs),
        )
        # case_summary 会写 metric=reward + raw；率值键对齐 compare
        summary["raw"] = oc.raw
        cases.append(summary)

    counts = {
        "runs": n_runs,
        "valid": valid,
        "invalid": invalid,
        "full_passes": full_pass_total,
    }
    return cases, counts


def import_tau_results(
    results_path: Path | str,
    *,
    out_dir: Path | str,
    domain: Optional[str] = None,
    batch_id: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    suite_id: Optional[str] = None,
) -> Path:
    """τ results.json → external batch 目录（**只写结果，不重判**）。

    out_dir 建议：`experiments/实验09-τ桥接/results/<batch_id>/`（决策⑥）。
    返回批次目录 Path。
    """
    p = Path(results_path)
    data = parse_results(p)
    if p.is_dir():
        results_file = p / "results.json"
    else:
        results_file = p

    dom = detect_domain(data, path=results_file, explicit=domain)
    sid = suite_id or f"tau.{dom}"
    suite = suite_for(dom) if suite_id is None else None
    if suite_id is not None:
        # 自定义 suite id：仍走 external + 指纹（指纹源同 tau.py）
        suite = Suite(
            id=suite_id, title=f"τ-bench {suite_id}", kind="external",
            fingerprint_sources=_FINGERPRINT_SOURCES, root=_SUITE_ROOT,
        )

    info = data.get("info") or {}
    agent_info = info.get("agent_info") or {}
    agent_name = agent or "tau2"
    model_name = model or agent_info.get("llm")
    tasks = list(data.get("tasks") or [])
    cases, counts = cases_from_results(data, domain=dom)

    if batch_id is None:
        stem = results_file.parent.name if results_file.parent.name else "tau"
        batch_id = f"import-{stem}"

    meta_agent = (info.get("agent_info") or {})
    source = {
        "kind": "tau2",
        "results_path": str(results_file),
        "domain": dom,
        "num_trials": info.get("num_trials"),
        "seed": info.get("seed"),
        "git_commit": info.get("git_commit"),
        "timestamp": data.get("timestamp"),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_implementation": meta_agent.get("implementation"),
        "user_llm": (info.get("user_info") or {}).get("llm"),
    }

    task_set = {
        "path": str(results_file),
        "sha256": _tasks_sha256(tasks),
        "name": f"tau.{dom}",
        "n_tasks": len(tasks),
    }

    bd = new_external_batch(
        Path(out_dir),
        batch_id=batch_id,
        agent=agent_name,
        model=model_name,
        suite=suite,
        suite_id=sid,
        cases=cases,
        k=int(info.get("num_trials") or counts["runs"] or 1),
        source=source,
        task_set=task_set,
        counts=counts,
    )
    return bd


__all__ = [
    "DOMAINS",
    "SUITES",
    "suite_for",
    "parse_results",
    "detect_domain",
    "cases_from_results",
    "import_tau_results",
]
