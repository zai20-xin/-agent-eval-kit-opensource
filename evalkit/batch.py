#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测批次读写 —— 「一次采集 / 一次导入 = 一个 batch 目录」。

设计与取舍见 `docs/architecture.md` §3。目录结构：

    <batch_dir>/                          # internal（本 kit 采集，可离线重判）
    ├── batch.json                        批次元信息（唯一真源）
    ├── traces/norm/<case>/<run>.json     归一化轨迹 ★ 判定的唯一输入
    ├── traces/raw/<case>/<run>.json      原始投影（审计 / 适配器回归，可选）
    ├── verdicts.jsonl                    每行一条 run 的判定
    └── summary.json                      聚合 + Wilson CI + 双字段 score

    <batch_dir>/                          # external（外部跑完导入，**不重判**）
    ├── batch.json                        含 suite / cases 内嵌结果
    ├── verdicts.jsonl                    导入时写入（可空）
    └── summary.json                      导入时写入
    （无 traces/ —— 轨迹可能为 None，SWE 预留①）

**核心能力**：
    · `comparable()` —— 两批数字能不能直接比（题目 / 判定口径 / 模型）
    · `new_external_batch()` —— τ 等外部套件结果导入；**report 不重判**

P-0 v2（2026-09-23）：
    · schema_version → 2；`mode`: `internal` | `external`
    · `suite` 元数据（id/fingerprint）进 batch.json —— **指纹归套件**
    · external 批 `cases` 内嵌 batch.json（决策⑥：产物进 experiments/，
      不改 TOP_DIRS_OK）
    · 兼容读 v1（无 mode/suite 字段视为 internal + 旧 detectors 指纹）
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Iterable, Optional

from .trace_schema import TRACE_SCHEMA_VERSION, trace_from_dict

SCHEMA_VERSION = 2

# P-1：判定实现迁至 suites/coding；指纹同时覆盖闸门 + 实现（两处任一变更都断指纹）。
# 有意的一次性断裂：v1 存量批次与 P-1 后新批次比对会标「不可比」（已接受）。
# P-0：新批次优先写 suite.fingerprint（归套件）；本函数保留作 v1/无 suite 时的回退。
_DETECTORS_SRC = Path(__file__).resolve().parent / "trace_detectors.py"
_CODING_DETECTIONS_SRC = (
    Path(__file__).resolve().parent / "suites" / "coding" / "detectors.py"
)


def sha256_file(p) -> str:
    h = hashlib.sha256()
    h.update(Path(p).read_bytes())
    return "sha256:" + h.hexdigest()[:16]


def detectors_fingerprint() -> str:
    """判定口径指纹（底座回退）—— 闸门或 coding 检测器任一变更即断。"""
    h = hashlib.sha256()
    h.update(Path(_DETECTORS_SRC).read_bytes())
    h.update(Path(_CODING_DETECTIONS_SRC).read_bytes())
    return "sha256:" + h.hexdigest()[:16]


def _evaluator_block(suite=None, evalkit_version=None) -> dict:
    """evaluator 字段：有 suite 用 suite 指纹（归套件），否则回退 detectors 指纹。"""
    if evalkit_version is None:
        from . import __version__ as evalkit_version
    block = {"evalkit_version": evalkit_version}
    if suite is not None:
        fp = suite.fingerprint() if hasattr(suite, "fingerprint") else None
        sid = getattr(suite, "id", None)
        if sid:
            block["suite_id"] = sid
        if fp:
            block["suite_fingerprint"] = fp
        # 仍写 detectors_sha256，保证与 v1 comparable 逻辑可对话
        block["detectors_sha256"] = detectors_fingerprint()
    else:
        block["detectors_sha256"] = detectors_fingerprint()
    return block


def new_batch(out_dir, *, batch_id, agent, model, adapter, adapter_version,
              task_set_path, k, evalkit_version=None, suite=None,
              mode: str = "internal") -> Path:
    """建批次目录并写 `batch.json`，返回批次目录路径。

    suite : Suite | None —— 提供则写 suite_id + suite_fingerprint（P-0 归套件）
    mode  : "internal"（默认，采集+可重判）| "external"（导入+不重判）
    """
    if mode == "external":
        raise ValueError(
            "external 批请用 new_external_batch() —— 它会内嵌 cases 且不建 traces/"
        )
    bd = Path(out_dir)
    (bd / "traces" / "norm").mkdir(parents=True, exist_ok=True)
    (bd / "traces" / "raw").mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "batch_id": batch_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent": {"name": agent, "model": model, "adapter": adapter,
                  "adapter_version": adapter_version},
        "task_set": {"path": str(task_set_path), "sha256": sha256_file(task_set_path)},
        "evaluator": _evaluator_block(suite, evalkit_version),
        "k": k,
        "counts": {"runs": 0, "valid": 0, "invalid": 0},
    }
    if suite is not None and hasattr(suite, "to_meta"):
        meta["suite"] = suite.to_meta()
    (bd / "batch.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bd


def new_external_batch(out_dir, *, batch_id, agent, model=None,
                       suite=None, suite_id: Optional[str] = None,
                       cases: Iterable[dict] = (),
                       k: int = 1, evalkit_version=None,
                       source: Optional[dict] = None,
                       task_set: Optional[dict] = None,
                       counts: Optional[dict] = None) -> Path:
    """外部套件结果导入（A 桥接）—— **只写结果，不建 traces，report 不重判**。

    cases : 可迭代的 case 级 dict（summary.cases 元素 / RunOutcome.to_dict 均可）
            内嵌进 batch.json（决策⑥）；同时可另写 summary.json
    suite : Suite | None —— 有则写 suite 元数据 + 指纹
    source: 外部来源说明，如 {"kind":"tau2","results_path":"...","reward_basis":...}
    task_set: 外部任务集元数据 {path?, sha256?, name?}；无文件时只存 name/sha256

    产物目录建议放 `experiments/.../results/`（TOP_DIRS_OK 已含 experiments）。
    """
    if evalkit_version is None:
        from . import __version__ as evalkit_version
    bd = Path(out_dir)
    bd.mkdir(parents=True, exist_ok=True)

    cases_list = [dict(c) for c in cases]
    sid = suite_id or (getattr(suite, "id", None) if suite else None)
    kind = getattr(suite, "kind", "external") if suite is not None else "external"

    meta = {
        "schema_version": SCHEMA_VERSION,
        "mode": "external",
        "batch_id": batch_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent": {"name": agent, "model": model, "adapter": None,
                  "adapter_version": evalkit_version},
        "evaluator": _evaluator_block(suite, evalkit_version),
        "k": k,
        "counts": counts or {"runs": sum(int(c.get("n", 0) or 0) for c in cases_list),
                              "valid": sum(int(c.get("n", 0) or 0) for c in cases_list),
                              "invalid": 0},
        "source": source or {"kind": kind},
        # 决策⑥：cases 内嵌 —— 无外部任务集文件也可读
        "cases": cases_list,
    }
    if suite is not None and hasattr(suite, "to_meta"):
        meta["suite"] = suite.to_meta()
    elif sid:
        meta["suite"] = {"id": sid, "kind": "external",
                         "fingerprint": (suite.fingerprint()
                                         if suite is not None and hasattr(suite, "fingerprint")
                                         else None)}
    if task_set is not None:
        meta["task_set"] = task_set
    elif sid:
        # 无独立任务集时给占位，避免 comparable 读路径炸
        meta["task_set"] = {"path": None, "sha256": None, "name": sid}

    (bd / "batch.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # summary.json 同步内嵌（report/compare 直接读它；external 不重判）
    payload = {
        "batch_id": batch_id,
        "mode": "external",
        "cases": cases_list,
        "counts": meta["counts"],
        "evaluator": meta["evaluator"],
        "task_set": meta.get("task_set"),
        "agent": meta["agent"],
        "suite": meta.get("suite"),
        "source": meta.get("source"),
    }
    (bd / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bd


def _case_dir(batch_dir, kind, case_id) -> Path:
    d = Path(batch_dir) / "traces" / kind / str(case_id).replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_trace(batch_dir, case_id, run_idx, trace, raw=None) -> dict:
    """落一条轨迹：`norm`（必需，判定输入）+ `raw`（可选，审计 / 适配器回归）。"""
    np_ = _case_dir(batch_dir, "norm", case_id) / f"{run_idx}.json"
    np_.write_text(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
    out = {"norm": str(np_.relative_to(batch_dir))}
    if raw is not None:
        rp = _case_dir(batch_dir, "raw", case_id) / f"{run_idx}.json"
        rp.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
        out["raw"] = str(rp.relative_to(batch_dir))
    return out


def read_traces(batch_dir, case_id):
    """读回某 case 的全部轨迹，按 run 序号排序 → [(run_idx, NormalizedTrace), ...]。"""
    d = Path(batch_dir) / "traces" / "norm" / str(case_id).replace("/", "_")
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"),
                    key=lambda x: int(x.stem) if x.stem.isdigit() else 0):
        out.append((int(p.stem) if p.stem.isdigit() else 0,
                    trace_from_dict(json.loads(p.read_text(encoding="utf-8")))))
    return out


def write_verdicts(batch_dir, rows):
    """写 `verdicts.jsonl`（每行一条 run）。

    `verdict` 语义**全局统一**：**1 = 该失败模式命中**（T5 也统一成 hit=丢失）。
    统一之后跨 case 聚合、跨批次对比才不需要逐个特判。
    """
    p = Path(batch_dir) / "verdicts.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def read_verdicts(batch_dir):
    p = Path(batch_dir) / "verdicts.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write_summary(batch_dir, cases, counts=None):
    """写 `summary.json`；顺带把 counts 回填进 batch.json。"""
    bd = Path(batch_dir)
    meta = load_batch(bd)
    if counts:
        meta["counts"] = counts
        (bd / "batch.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload = {
        "batch_id": meta.get("batch_id"),
        "mode": meta.get("mode", "internal"),
        "cases": cases,
        "counts": meta.get("counts"),
        "evaluator": meta.get("evaluator"),
        "task_set": meta.get("task_set"),
        "agent": meta.get("agent"),
        "suite": meta.get("suite"),
    }
    p = bd / "summary.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def update_agent_model(batch_dir, models) -> Optional[str]:
    """把**实测**模型名回填进 batch.json / summary.json 的 agent.model。

    为什么建批时不写、事后回填：`cmd_run` 建 batch 时模型未知（写 None），
    模型名要等轨迹落盘后从 agent 自己的存储读出来（如 opencode 的 message.modelID）。
    与其让人手填（2026-09-21 就发生过登记模型名与实测不符的勘误），不如让管线
    自己把「实际跑的时候用的模型」写进结果 —— 模型必须可追溯，这是可比性的三要素之一。

    规则（不假装知道）：
        · 无非空 model       → 不动（没证据不编造）
        · 单一模型           → 回填
        · 多个模型           → **不回填**，打印警告 —— 混模型批次没有单一「底座模型」，
          该写什么应留给人工判断；不覆盖已有值（人工标注优先于自动回填）。
    """
    import sys as _sys

    batch_dir = Path(batch_dir)
    uniq = sorted({m for m in models if m})
    if not uniq:
        return None
    if len(uniq) > 1:
        print(f"⚠ 同批出现多个模型 {uniq} —— 不自动回填 agent.model，"
              "请人工确认（混模型批次没有单一底座模型）", file=_sys.stderr)
        return None
    model = uniq[0]
    meta = load_batch(batch_dir)
    agent = dict(meta.get("agent") or {})
    if agent.get("model"):
        return agent["model"]
    agent["model"] = model
    meta["agent"] = agent
    (batch_dir / "batch.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sp = batch_dir / "summary.json"   # 已存在则同步（report/compare 读它）
    if sp.exists():
        payload = json.loads(sp.read_text(encoding="utf-8"))
        payload["agent"] = agent
        sp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return model


def load_batch(batch_dir):
    return json.loads((Path(batch_dir) / "batch.json").read_text(encoding="utf-8"))


def is_external(batch_meta: dict) -> bool:
    """是否外部导入批（v1 无 mode 字段 → False）。"""
    return str(batch_meta.get("mode", "internal")) == "external"


def embedded_cases(batch_meta: dict) -> list:
    """external 批内嵌的 cases（internal 返回 []）。"""
    if not is_external(batch_meta):
        return []
    return list(batch_meta.get("cases") or [])


# ---------------------------------------------------------------- 可比性
def _fingerprint_of(batch_meta: dict):
    """判定口径：优先 suite_fingerprint（归套件），回退 detectors_sha256。"""
    ev = batch_meta.get("evaluator") or {}
    return ev.get("suite_fingerprint") or ev.get("detectors_sha256")


def _compare_keys(batch_a: dict, batch_b: dict) -> list:
    """按两侧字段动态生成比较项 —— external 可能无 task_set 文件路径。"""
    keys = []
    ta = (batch_a.get("task_set") or {}).get("sha256")
    tb = (batch_b.get("task_set") or {}).get("sha256")
    if ta is not None or tb is not None:
        keys.append(("题目（任务集 hash）", ("task_set", "sha256")))
    keys.append(("判定口径（套件/检测器指纹）", ("__fingerprint__",)))
    keys.append(("底座模型", ("agent", "model")))
    return keys


def _dig(d, path):
    if path == ("__fingerprint__",) or path == ["__fingerprint__"]:
        return _fingerprint_of(d)
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def comparable(batch_a, batch_b):
    """两个批次的数字能否直接比较 → (ok: bool, reasons: list[str])。

    三要素任一不同即不可比，并指出是**哪一项**、**各是什么值** ——
    这样「为什么这次不能跟上次比」是机器给出的答案，不用人回忆。
    """
    reasons = []
    for label, path in _compare_keys(batch_a, batch_b):
        va, vb = _dig(batch_a, path), _dig(batch_b, path)
        # 双侧都缺（如 external 无 task_set）→ 不因缺失判不可比
        if va is None and vb is None:
            continue
        if va != vb:
            reasons.append(f"{label}不同：{va} ≠ {vb}")
    return (not reasons), reasons
