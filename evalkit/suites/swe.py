#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SWE-Bench 外部套件桥接（A 桥接：外部跑完，结果导入本 kit）。

决策（对齐 τ 桥接，2026-09-24 P2+）：
    · 一 suite：`swe.bench`（不按 repo 再拆 —— 题目指纹已含 instance_id）
    · **只读**外部 `resolved` / `reward` / `score` —— 不在 kit 内跑测试、不复算判定
    · 导入走 `batch.new_external_batch` —— **不重判**、cases 内嵌、无 traces/
    · 产物进 `experiments/`（决策⑥），不改 layout TOP_DIRS_OK
    · SWE 四条预留：trace 可 None · 不重判 · 指纹归 suite · evidence 可 refs

接受的输入形状（常见 harness / leaderboard 导出，多选一）：
    1. 列表：`[{instance_id, resolved|reward|score|pass}, ...]`
    2. 对象含列表：`{"results"|"instances"|"records"|"predictions": [...]}`
    3. 汇总 id 列表：`{"resolved_ids": [...], "unresolved_ids"?: [...],
                        "total_ids"?: int}`
    4. JSONL：每行一个对象（同 1）
    5. 目录：在目录内找 `results.json` / `report.json` / `results.jsonl`

铁律：本模块是**导入器**，不是评测器 —— 不执行补丁、不跑测试、不读 diff 判分。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from ..batch import new_external_batch
from ..outcome import RunOutcome, case_summary_from_outcome
from ..suite import Suite

# 指纹：归套件（SWE 预留③）—— 只哈希本导入器；口径=「如何把 resolved 落成 case」
_FINGERPRINT_SOURCES = ("swe.py",)
_SUITE_ROOT = Path(__file__).resolve().parent

SUITE = Suite(
    id="swe.bench",
    title="SWE-Bench（外部 resolved/reward 导入）",
    kind="external",
    description=(
        "A 桥接：外部 harness 跑完 → 只读 resolved/reward → "
        "new_external_batch；report 不重判。trace 可 None（预留①）。"
    ),
    fingerprint_sources=_FINGERPRINT_SOURCES,
    root=_SUITE_ROOT,
)

SUITES: dict[str, Suite] = {SUITE.id: SUITE}

# 目录内默认查找的文件名（按序）
_DIR_CANDIDATES = ("results.json", "report.json", "results.jsonl",
                   "predictions.json", "predictions.jsonl")

# 单条记录里「得分」字段的优先序（bool → 0/1；float → [0,1]）
_SCORE_KEYS = ("resolved", "reward", "score", "pass", "passed", "success",
               "correct", "ok")


def _tasks_sha256(tasks: Sequence[dict]) -> str:
    """题目指纹：只哈希 instance 定义（id 为主），**不含** resolved/reward。

    同题目换模型重跑 → hash 不变 → 可比；增删改题 → hash 变 → 不可比。
    """
    blob = json.dumps(list(tasks), sort_keys=True, ensure_ascii=False,
                      default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:16]


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def parse_results(path: Path | str) -> Any:
    """读 SWE 结果文件（json / jsonl / 目录）→ 原始对象（不在此判分）。"""
    p = Path(path)
    if p.is_dir():
        for name in _DIR_CANDIDATES:
            cand = p / name
            if cand.is_file():
                p = cand
                break
        else:
            raise FileNotFoundError(
                f"目录内无 {'/'.join(_DIR_CANDIDATES)}：{p}")
    if not p.is_file():
        raise FileNotFoundError(f"结果文件不存在：{p}")

    text = _read_text(p)
    if p.suffix == ".jsonl" or "\n" in text.strip() and text.lstrip().startswith("{"):
        # jsonl：逐行；单对象 JSON 走 json.loads 分支更好，这里先试整体
        try:
            data = json.loads(text)
            return data
        except json.JSONDecodeError:
            rows = []
            for i, ln in enumerate(text.splitlines(), 1):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError as e:
                    raise ValueError(f"{p} 第 {i} 行不是合法 JSON：{e}") from e
            if not rows:
                raise ValueError(f"空结果文件：{p}")
            return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"不是合法 JSON：{p} —— {e}") from e


def _as_records(data: Any) -> list[dict]:
    """把多种输入形状收成 `[{instance_id?, score_fields...}, ...]`。"""
    if isinstance(data, list):
        out = [r for r in data if isinstance(r, dict)]
        if out:
            return out
        # 纯 id 字符串列表 → 当作 resolved_ids 不明确，拒绝（需对象形状）
        if data and all(isinstance(x, str) for x in data):
            raise ValueError(
                "裸字符串列表无法区分 resolved/unresolved；"
                "请用 {\"resolved_ids\": [...]} 或 [{\"instance_id\":..., \"resolved\":...}]")
        raise ValueError("列表里没有 dict 记录")

    if isinstance(data, dict):
        # 形状 3：resolved_ids 汇总
        if "resolved_ids" in data or "unresolved_ids" in data:
            resolved = [str(x) for x in (data.get("resolved_ids") or [])]
            unresolved = [str(x) for x in (data.get("unresolved_ids") or [])]
            records = [{"instance_id": i, "resolved": True} for i in resolved]
            records += [{"instance_id": i, "resolved": False} for i in unresolved]
            # total 大于已知列表时，未知 id 视为 unresolved 不可靠 → 用 note
            total = data.get("total_ids") or data.get("total_instances")
            if isinstance(total, int) and total > len(records):
                # 无 id 的未提交样本单列不了 case；记进 meta 由调用方放 source
                pass
            if records:
                return records
            raise ValueError("resolved_ids/unresolved_ids 均为空")

        for key in ("results", "instances", "records", "predictions",
                    "samples", "rows"):
            v = data.get(key)
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return list(v)

        # 单条对象本身带 instance_id
        if _instance_id_of(data) is not None:
            return [data]

    raise ValueError(
        "无法识别的 SWE 结果形状；支持：记录列表 / results[] / "
        "resolved_ids+unresolved_ids / JSONL")


def _instance_id_of(rec: dict) -> Optional[str]:
    for k in ("instance_id", "id", "task_id", "name", "instance"):
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v)
    return None


def _score_of(rec: dict) -> Optional[float]:
    """读单条记录得分 → float ∈[0,1]；缺失 → None。

    resolved/pass 类 bool/int 0/1 → 0.0/1.0；
    reward/score 类 float 原样（假定已 ∈[0,1]，与 τ reward 同约定）。
    """
    for k in _SCORE_KEYS:
        if k not in rec:
            continue
        v = rec[k]
        if v is None:
            continue
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            # 避免 1 当成 100% 制 —— >1 且 ≤100 视为百分制
            fv = float(v)
            if fv > 1.0 and fv <= 100.0:
                return max(0.0, min(1.0, fv / 100.0))
            return max(0.0, min(1.0, fv))
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("true", "yes", "y", "pass", "passed", "resolved", "1"):
                return 1.0
            if t in ("false", "no", "n", "fail", "failed", "unresolved", "0"):
                return 0.0
            try:
                fv = float(t)
                return max(0.0, min(1.0, fv / 100.0 if fv > 1.0 else fv))
            except ValueError:
                continue
    return None


def _label_of(rec: dict) -> Optional[str]:
    for k in ("repo", "problem_statement", "title", "description", "note"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            return s if len(s) <= 80 else s[:77] + "..."
    return None


def cases_from_records(records: Sequence[dict], *,
                       suite_id: str = "swe.bench"
                       ) -> tuple[list[dict], dict, list[dict]]:
    """records → case 级双字段 summary + counts + 题目定义（供 hash）。

    · 每 instance 一条 case；同 instance 多条（多 trial）→ mean(score)
    · score ∈[0,1] 高=好（resolved 天然 0/1；reward 原样）
    · n/hits/rate：n=有效 trial，hits=score≥1 次数，rate=满分解（compare 用）
    · 缺 score 的记录 → collect_failed，不进分母（C6 同源处置）
    · trace 恒 None（SWE 预留①）；evidence 可填外部 patch/test 路径 ref
    """
    by_id: dict[str, list[tuple[dict, Optional[float]]]] = {}
    order: list[str] = []
    orphan = 0
    for rec in records:
        iid = _instance_id_of(rec)
        if iid is None:
            orphan += 1
            continue
        if iid not in by_id:
            by_id[iid] = []
            order.append(iid)
        by_id[iid].append((rec, _score_of(rec)))

    # 题目定义：只有 id（+ 可选 repo/base_commit 若有）—— 不含得分
    tasks: list[dict] = []
    for iid in order:
        recs = [r for r, _ in by_id[iid]]
        task: dict = {"id": iid}
        for k in ("repo", "base_commit", "version", "environment_setup_commit"):
            if recs and recs[0].get(k) is not None:
                task[k] = recs[0].get(k)
        tasks.append(task)

    cases: list[dict] = []
    n_runs = valid = invalid = full_pass = 0

    for iid in order:
        group = by_id[iid]
        scores = [s for _, s in group if s is not None]
        n_runs += len(group)
        invalid += len(group) - len(scores)
        valid += len(scores)
        label = _label_of(group[0][0]) or iid

        if not scores:
            summary = {
                "id": iid, "label": label,
                "score": 0.0, "metric": "resolved",
                "raw": {"scores": [], "n_trials": len(group),
                        "note": "missing resolved/reward/score"},
                "ci": None,
                "n": 0, "hits": 0, "rate": 0.0,
                "no_output": 0, "collect_failed": len(group),
            }
            cases.append(summary)
            continue

        mean_s = sum(scores) / len(scores)
        hits = sum(1 for s in scores if s >= 1.0)
        full_pass += hits

        # evidence：外部轨迹/补丁路径 ref（不内嵌正文，SWE 预留④）
        evidence = []
        for k in ("patch", "model_patch", "test_output", "logs",
                  "trajectory", "report"):
            v = group[0][0].get(k)
            if isinstance(v, str) and v and len(v) <= 200 and ("/" in v or "\\" in v):
                evidence.append({"path": v, "note": k})
            elif isinstance(v, str) and group[0][0].get(f"{k}_path"):
                evidence.append({"path": group[0][0][f"{k}_path"], "note": k})

        oc = RunOutcome(
            case_id=iid, run=-1, score=mean_s, metric="resolved",
            raw={"scores": scores, "n_trials": len(group),
                 "n_valid": len(scores), "mean": mean_s,
                 "full_passes": hits},
            suite_id=suite_id,
            trace=None,  # SWE 预留①
            evidence=evidence or None,
        )
        rate = hits / len(scores)
        summary = case_summary_from_outcome(
            oc, n=len(scores), hits=hits, rate=rate, ci=None,
            label=label,
            no_output=0, collect_failed=len(group) - len(scores),
        )
        summary["raw"] = oc.raw
        cases.append(summary)

    counts = {
        "runs": n_runs,
        "valid": valid,
        "invalid": invalid,
        "full_passes": full_pass,
        "orphan_records": orphan,
    }
    return cases, counts, tasks


def import_swe_results(
    results_path: Path | str,
    *,
    out_dir: Path | str,
    batch_id: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    suite_id: Optional[str] = None,
    dataset: Optional[str] = None,
) -> Path:
    """SWE 结果 → external batch 目录（**只写结果，不重判**）。

    out_dir 建议：`experiments/<实验>/results/<batch_id>/`（决策⑥）。
    返回批次目录 Path。
    """
    p = Path(results_path)
    data = parse_results(p)
    if p.is_dir():
        # parse_results 已解析到具体文件，但目录时再定位一次供 source
        results_file = next(
            (p / n for n in _DIR_CANDIDATES if (p / n).is_file()), p)
    else:
        results_file = p

    sid = suite_id or SUITE.id
    if suite_id is None or suite_id == SUITE.id:
        suite = SUITE
    else:
        suite = Suite(
            id=suite_id, title=f"SWE {suite_id}", kind="external",
            fingerprint_sources=_FINGERPRINT_SOURCES, root=_SUITE_ROOT,
        )

    records = _as_records(data)
    cases, counts, tasks = cases_from_records(records, suite_id=sid)

    if batch_id is None:
        stem = results_file.parent.name if results_file.parent.name else "swe"
        batch_id = f"import-{stem}"

    # model / agent：SWE 导出常在顶层 meta；没有则靠 CLI 覆盖
    info = data if isinstance(data, dict) else {}
    model_name = model
    agent_name = agent or "swe-harness"
    if model_name is None:
        for key in ("model", "model_name", "agent_model"):
            if isinstance(info.get(key), str) and info[key]:
                model_name = info[key]
                break
        else:
            agent_info = info.get("agent_info") or info.get("agent") or {}
            if isinstance(agent_info, dict):
                model_name = agent_info.get("model") or agent_info.get("llm")

    source = {
        "kind": "swebench",
        "results_path": str(results_file),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataset": dataset or info.get("dataset") or info.get("split"),
        "n_records": len(records),
    }
    # 汇总形状可能带的元数据
    if isinstance(info, dict):
        for k in ("total_instances", "resolved_instances", "submitted_instances",
                  "error_instances", "completions"):
            if k in info:
                source[k] = info[k]

    task_set = {
        "path": str(results_file),
        "sha256": _tasks_sha256(tasks),
        "name": sid,
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
        k=max(1, counts.get("runs") // max(1, len(tasks)) or 1),
        source=source,
        task_set=task_set,
        counts={k: counts[k] for k in
                ("runs", "valid", "invalid", "full_passes")
                if k in counts},
    )
    return bd


__all__ = [
    "SUITE",
    "SUITES",
    "parse_results",
    "cases_from_records",
    "import_swe_results",
]
