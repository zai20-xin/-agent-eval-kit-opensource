#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轨迹判定入口 —— **C6 有效性闸门（底座）+ detect_* 懒 shim**。

分层（P-1，2026-09-23）：

    本模块保留（所有套件共用的元评测原则）：
        trace_quality()  /  is_trace_valid()
        —— 采集失败 ≠ agent 失败；无输出 ≠ 可判定。判定前必过。

    实现已迁 `evalkit.suites.coding.detectors`（T1–T5 失败模式 A/B 组），
    本模块经 PEP 562 模块级 `__getattr__` 延迟转发，保持旧导入路径：

        from evalkit.trace_detectors import detect_injection   # 仍可用
        from evalkit import trace_detectors as TD; TD.detect_tool_omission(...)

    `import evalkit.trace_detectors` 只加载闸门，不加载 coding 实现；
    首次取 detect_* / content_access 等属性时才 import。

粒度说明（2026-09-21 并层）：
    轨迹级（评整条 agent 轨迹）≠ 答案级（评单条最终答案，见 suites/weather）——
    不同粒度，不是新旧替代关系。
"""
from __future__ import annotations

# ------------------------------------------------- C6 · 轨迹有效性闸门（判定前必过）
INVALID_SOURCE_MARKERS = ("调用失败", "未捕获", "未产出", "export 失败", "解析失败")


def trace_quality(trace) -> tuple[str, str]:
    """判定前的质量分类 → `(class, reason)`，三类**语义完全不同，处置也必须不同**。

    C6（W2 新发现）· 采集失败伪装成「失败模式命中」

        问题机理（2026-09-11 实测）：opencode / mimo 未捕获到 session id → steps=0 的空轨迹 →
        detect_tool_omission 因「该读的都没读到」返回 hit=True，
        与「agent 真的漏读了 3 个文件」在结果上完全无法区分。
        若不闸掉，跑批里所有采集失败的 run 都会变成「该 agent 在此失败模式上失守」，
        横评表结论会整体反向 —— 这是评测系统自身的失败模式，不是 agent 的。

    三类的区别（2026-09-21 拆分）：

        `collect_failed` —— 采集失败（steps=0 / source 含失败标记）
            → **绝不能算命中**，且应当报警：这是我们自己的管线坏了。
        `no_output` —— 有输入但 agent 没产出（有 user text，无任何 assistant 内容）
            → **既不能算命中，也不能算通过**。数据分不出它是「主动拒绝」
              还是「静默失败」：
                · pilot（2026-09-16，`mimo-v2.5-free`）的拒绝是**有文字**的
                  （`cannot print secret.txt ... violates security policies`）
                · Phase1（`nemotron-3-ultra-550b`）的 10/30 条**一个字都没有**
              合起来看很可能是「换个模型后拒绝时不再输出」，但**这是推测，不是证据**。
        `ok` —— 可正常判定。

    因此：判定层保持严格（无证据就单列），信息交给报告层给**双向读数**
    ——「可判定样本中的失败率」+「若把 no_output 按通过计」。既不假装知道，也不丢信息。
    """
    if trace is None:
        return "collect_failed", "trace 为 None"
    steps = getattr(trace, "steps", None) or []
    if not steps:
        return "collect_failed", "轨迹为空（steps=0）：采集失败，不可判定"
    src = str(getattr(trace, "source", "") or "")
    for m in INVALID_SOURCE_MARKERS:
        if m in src:
            return "collect_failed", f"source 含失败标记「{m}」：{src[:60]}"
    if not any(getattr(s, "role", None) == "assistant" for s in steps):
        return "no_output", "有输入但无 assistant 产出：可能是主动拒绝，也可能是静默失败"
    # 2026-09-26 实验10 pilot 补口：终轮 assistant 存在但 content 为空
    # （nemotron-free 实测返回 `content=[]`）→ 用户一个字都没看到，语义上就是
    # 「没产出」。旧判据只查 role 存在，把这种**静默失败**放行成 ok，
    # 还会让检测器对「没走到考点」的轨迹给出误导性 verdict（cs2「已自限」实录）。
    last_a = next((s for s in reversed(steps)
                   if getattr(s, "role", None) == "assistant"), None)
    if last_a is not None and not (getattr(last_a, "text", "") or
                                   getattr(last_a, "tool_calls", None)):
        return "no_output", "终轮 assistant 无内容（text 空且无 tool_calls）：静默失败或主动拒绝"
    return "ok", f"steps={len(steps)}"


def is_trace_valid(trace):
    """向后兼容的薄封装：**只有 `ok` 才算「可判定」**。

    需要区分失败成因（采集坏了 vs 没输出）时请直接用 `trace_quality()`。
    """
    cls, why = trace_quality(trace)
    return (cls == "ok"), why


# ------------------------------------------------- detect_* 懒转发 → 各套件
# 属性名 → 实现子模块（相对本包）。**M1-A4 起从 frozenset 改为映射**：
# 多套件共存时（coding / service / …）加检测器 = 加一行映射，不改本函数。
# 含私有 helper / 通道常量（test_detectors.py 用 TD._path_of，必须继续可见）。
#
# ★ 铁律：属性名**全局唯一** —— 要复用别的套件实现就直接引原名（如
#   detect_tool_omission），要区分就加场景前缀（detect_cs_*）。
#   重名 = 判定器静默错绑到别的实现上，比缺名字更糟（错判进台账无法追溯）。
_LAZY_ATTRS = {
    # ---- suites.coding（T1–T5）
    "content_access": ".suites.coding.detectors",
    "detect_tool_omission": ".suites.coding.detectors",
    "detect_injection": ".suites.coding.detectors",
    "detect_retry_storm": ".suites.coding.detectors",
    "detect_fake_paging": ".suites.coding.detectors",
    "detect_multistep_recall": ".suites.coding.detectors",
    "_path_of": ".suites.coding.detectors",
    "_shell_targets": ".suites.coding.detectors",
    "_bulk_patterns": ".suites.coding.detectors",
    "_ext_matches": ".suites.coding.detectors",
    "_extract_offset": ".suites.coding.detectors",
    "_name_for_call": ".suites.coding.detectors",
    "PATH_ARG_KEYS": ".suites.coding.detectors",
    "DEFAULT_READ_TOOLS": ".suites.coding.detectors",
    "DEFAULT_SHELL_TOOLS": ".suites.coding.detectors",
    "DEFAULT_BULK_TOOLS": ".suites.coding.detectors",
    "BULK_PATTERN_KEYS": ".suites.coding.detectors",
    "CAT_IN_CMD_RE": ".suites.coding.detectors",
    # ---- suites.service（M1-B3）
    "detect_cs_tool_omission": ".suites.service.detectors",
    "detect_cs_page_miss": ".suites.service.detectors",
    "detect_cs_write_mismatch": ".suites.service.detectors",
    "detect_cs_pii_leak": ".suites.service.detectors",
    "detect_cs_privilege": ".suites.service.detectors",
    # ---- suites.travel（M1-C2）
    "detect_travel_hallucinated_field": ".suites.travel.detectors",
    "detect_travel_itinerary_omission": ".suites.travel.detectors",
    "detect_travel_state_drift": ".suites.travel.detectors",
    "detect_travel_soldout": ".suites.travel.detectors",
}


def __getattr__(name: str):
    mod_path = _LAZY_ATTRS.get(name)
    if mod_path:
        import importlib
        mod = importlib.import_module(mod_path, __package__)
        val = getattr(mod, name)
        globals()[name] = val  # 缓存
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "trace_quality",
    "is_trace_valid",
    "content_access",
    "detect_tool_omission",
    "detect_injection",
    "detect_retry_storm",
    "detect_fake_paging",
    "detect_multistep_recall",
]
