#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务集契约 —— 用**数据**声明「评什么」，而不是用代码硬编码。

格式（JSON）：

    {
      "name": "phase1_k30",                     // 用于输出文件名
      "meta": {
        "title": "...", "agent": "opencode",
        "sids_dir": "results/opencode/phase1/sids"
      },
      "cases": [
        {
          "id": "T1/n8",                        // 报告里的行标识
          "sids_file": "t1_n8_sids.txt",        // 相对 sids_dir
          "detector": "detect_tool_omission",   // evalkit.trace_detectors 里的函数名
          "detector_params": {"required_paths": ["f1.py", "..."]},
          "label": "漏读",                      // 失败语义，仅用于展示
          "failure_mode": "A1"                  // 失败模式编号（可选）
        }
      ]
    }

三条约束：
    1. `detector` 必须是 `evalkit.trace_detectors` 里真实存在的函数名 —— **load 时就校验**。
       写错名字立刻报错，绝不静默跳过（静默跳过本身就是 C 组失败模式的形态）。
    2. `detector_params` **原样透传**给检测器，不改不补 —— 口径差异留在数据里，一眼可见。
    3. **新增 / 修改任务只改这个 JSON，不改任何代码** —— 这是「用得上」的第一条判据。
"""
from __future__ import annotations

import json
from pathlib import Path


class TaskSetError(ValueError):
    """任务集格式/引用错误 —— 一律显式抛出，不做兜底猜测。"""


def _resolve_detectors():
    """延迟 import：校验时才加载 C6 + coding 检测器（P-1，避免 tasks 成为硬依赖边）。"""
    from . import trace_detectors as TD
    return TD


def load_task_set(path):
    """读取并校验任务集，返回 dict（原样，不做任何补全）。"""
    p = Path(path)
    if not p.exists():
        raise TaskSetError(f"任务集不存在：{p}")
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TaskSetError(f"任务集不是合法 JSON：{e}") from e

    if not isinstance(spec.get("cases"), list):
        raise TaskSetError("任务集缺少 cases 数组")

    TD = _resolve_detectors()
    available = [n for n in dir(TD) if n.startswith("detect_")]
    for i, case in enumerate(spec["cases"]):
        for key in ("id", "sids_file", "detector"):
            if key not in case:
                raise TaskSetError(f"cases[{i}] 缺字段「{key}」")
        if not callable(getattr(TD, case["detector"], None)):
            raise TaskSetError(
                f"cases[{i}]（{case['id']}）引用了不存在的检测器「{case['detector']}」；"
                f"可用的有：{available}"
            )
    return spec
