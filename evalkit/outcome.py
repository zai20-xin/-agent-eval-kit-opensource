#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行结果契约 —— 单 run / 单 case 的**双字段**结果模型。

为什么是双字段（2026-09-23 P-0 锁定决策③）：

    · `score ∈ [0,1]` **高=好** —— 跨套件可比的归一化轴
      （coding 的 failure_rate 要翻成 pass；τ 的 reward 天然高=好）
    · `metric` + `raw` + `ci` —— **保留套件原生叙述**
      （coding 继续讲 hits/rate；τ 继续讲 reward / reward_breakdown）

    只留 score 会丢掉「2/30 到底是什么」；只留 rate 又没法跨套件并排。
    两者都存，且 score 的换算规则写在 raw 里，可审计。

SWE-Bench 兼容预留（已认可，进 P-0 验收）：
    · `RunOutcome.trace` **可为 None** —— 外部套件可能只导入结果、不带归一化轨迹
    · `RunOutcome.evidence` 允许 **refs / 路径** —— 不强制内嵌正文
    · external 批**不重判** —— 见 batch.new_external_batch / cli 外部报告路径

铁律：本模块只定义数据形状与换算，不 import 任何 suites 实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def clamp01(x: float) -> float:
    """把任意有限值夹到 [0,1]；NaN → 0.0（不可判定不要伪装成满分）。"""
    if x != x:  # NaN
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def score_from_failure_rate(rate: float) -> float:
    """失败模式命中率 → 高=好的 score（coding 等「越低越好」指标的统一翻转）。"""
    return clamp01(1.0 - clamp01(rate))


@dataclass
class RunOutcome:
    """单条 run 的判定/导入结果。

    必填：case_id / run / score / metric
    score：**永远** ∈ [0,1] 且高=好；原生指标（可能高=坏）放 raw。
    """

    case_id: str
    run: int
    score: float
    metric: str                      # 原生指标名："failure_rate" | "reward" | ...
    raw: dict = field(default_factory=dict)   # 原生数字（hits/n/rate/reward/...）
    ci: Optional[list] = None        # 原生 rate 的 CI [lo,hi]；无区间则 None

    # ---- 轨迹 / 证据（SWE 预留：都可空、evidence 可为 refs）----
    trace: Any = None                # NormalizedTrace | None（None 合法）
    evidence: Optional[list] = None  # [{ref|path|url, note?}, ...] 或内嵌 dict 列表

    # ---- 无效 / 元信息 ----
    invalid: bool = False            # C6：采集失败/无产出 → 不进率值
    reason: Optional[str] = None
    detail: str = ""
    detector: Optional[str] = None
    params: Optional[dict] = None
    suite_id: Optional[str] = None
    trace_path: Optional[str] = None  # 批次内相对路径（有轨迹时）

    def __post_init__(self):
        self.score = clamp01(float(self.score))
        if self.invalid:
            # 无效 run 不参与聚合时由调用方过滤；score 保留但标 invalid
            pass

    # ---- 构造 helpers ----
    @classmethod
    def from_failure_hit(cls, *, case_id: str, run: int, hit: bool,
                         n: Optional[int] = None, rate: Optional[float] = None,
                         hits: Optional[int] = None, ci: Optional[list] = None,
                         detail: str = "", metric_extra: Optional[dict] = None,
                         **kw) -> "RunOutcome":
        """coding 轨迹检测器：hit=True 表示失败模式命中 → score = 1-rate 或 0/1。

        run 级默认：hit → score=0，未 hit → score=1（单次伯努利）。
        case 级聚合请用 from_case_rate。
        """
        raw = dict(metric_extra or {})
        if n is not None:
            raw["n"] = n
        if hits is not None:
            raw["hits"] = hits
        if rate is not None:
            raw["rate"] = rate
        raw["hit"] = bool(hit)
        return cls(
            case_id=case_id, run=run,
            score=0.0 if hit else 1.0,
            metric="failure_rate",
            raw=raw, ci=ci, detail=detail, **kw,
        )

    @classmethod
    def from_case_rate(cls, *, case_id: str, rate: float, n: int, hits: int,
                       ci: Optional[list] = None, label: Optional[str] = None,
                       metric: str = "failure_rate", extra: Optional[dict] = None,
                       **kw) -> "RunOutcome":
        """case 聚合：score = 1 - rate（高=好）；raw 保留 rate/hits/n 原叙述。"""
        raw = {"n": n, "hits": hits, "rate": rate}
        if extra:
            raw.update(extra)
        if label is not None:
            raw["label"] = label
        return cls(
            case_id=case_id, run=-1,          # run=-1 表示 case 聚合，不是单 run
            score=score_from_failure_rate(rate),
            metric=metric, raw=raw, ci=ci, **kw,
        )

    @classmethod
    def from_reward(cls, *, case_id: str, run: int, reward: float,
                    breakdown: Optional[dict] = None, **kw) -> "RunOutcome":
        """τ-bench 桥接：reward_info.reward 天然 ∈[0,1] 高=好（决策⑧只读 reward）。"""
        raw: dict = {"reward": reward}
        if breakdown is not None:
            raw["reward_breakdown"] = breakdown
        return cls(
            case_id=case_id, run=run,
            score=clamp01(reward), metric="reward", raw=raw, **kw,
        )

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        d = asdict(self)
        # trace 若是 NormalizedTrace，交给批次层再落盘；此处只保留标记
        if self.trace is not None and hasattr(self.trace, "to_dict"):
            d["trace"] = None
            d["has_trace"] = True
        else:
            d["has_trace"] = self.trace is not None
            if not isinstance(self.trace, (dict, type(None))):
                d["trace"] = None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RunOutcome":
        d = dict(d or {})
        d.pop("has_trace", None)
        # trace 不从 dict 恢复（轨迹走 batch.traces/norm；外部批可无）
        d["trace"] = None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# case 级 summary 形状（双字段 + 保留 coding 原叙述键）
def case_summary_from_outcome(oc: RunOutcome, *,
                              n: Optional[int] = None,
                              hits: Optional[int] = None,
                              rate: Optional[float] = None,
                              ci: Optional[list] = None,
                              no_output: int = 0,
                              collect_failed: int = 0,
                              label: Optional[str] = None) -> dict:
    """把 case 级 RunOutcome 渲染成 summary.cases[] 元素。

    双字段：score/metric/raw/ci + **保留** n/hits/rate（compare 与旧读者不破）。
    """
    raw = dict(oc.raw)
    if label is not None:
        raw.setdefault("label", label)
    # 优先从 oc.raw 取，允许显式覆盖
    n = n if n is not None else raw.get("n", 0)
    hits = hits if hits is not None else raw.get("hits", 0)
    rate = rate if rate is not None else raw.get("rate", 0.0)
    ci = ci if ci is not None else oc.ci

    out = {
        "id": oc.case_id,
        "label": label if label is not None else raw.get("label"),
        # ---- 双字段（P-0）----
        "score": round(oc.score, 4),
        "metric": oc.metric,
        "raw": raw,
        "ci": [round(x, 4) for x in ci] if ci else None,
        # ---- 原叙述（coding / compare 兼容）----
        "n": n,
        "hits": hits,
        "rate": round(rate, 4) if isinstance(rate, float) else rate,
        "no_output": no_output,
        "collect_failed": collect_failed,
    }
    if oc.ci is not None and ci is None:
        out["ci"] = [round(x, 4) for x in oc.ci]
    return out


__all__ = [
    "RunOutcome",
    "clamp01",
    "score_from_failure_rate",
    "case_summary_from_outcome",
]
