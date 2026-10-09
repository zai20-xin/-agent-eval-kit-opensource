#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测套件元数据 —— 「什么是 Suite」+ 指纹归套件（SWE 预留）。

Suite 是**内容层的插件边界**：底座（batch/cli/stats/outcome）场景无关，
天气 / coding / 将来的 τ.* 都以 Suite 注册进来。

设计（2026-09-23 P-0 锁定）：
    · 每 domain 一 suite：`weather.w1` / `coding.t1t5` / 将来 `tau.airline` 等
    · **指纹归 suite**（SWE 预留③）：判定口径由套件自己声明参与哈希的源文件，
      底座不再写死「hash 哪个 detectors.py」
    · `kind`：`internal`（本 kit 可重判）| `external`（外部跑完导入，**不重判**）
    · 发现：`suites.discover_suites()` 扫子包模块级 `SUITE`；
      本模块不 import 任何套件实现（保持 `import evalkit.cli` 轻）

铁律：本模块零场景依赖 —— 不 import weather/coding/tau。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence


def sha256_files(paths: Sequence[Path], *, prefix: str = "sha256:") -> str:
    """按序哈希多个文件字节 → `"sha256:<16hex>"`（与 batch.sha256_file 同形）。"""
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).read_bytes())
    return prefix + h.hexdigest()[:16]


@dataclass(frozen=True)
class Suite:
    """套件描述（纯数据 + 可选指纹回调）。"""

    id: str                                 # "coding.t1t5" | "weather.w1" | "tau.airline"
    title: str = ""
    kind: str = "internal"                  # internal | external
    description: str = ""
    # 指纹：二选一 —— 显式 sources（相对套件包根）或自定义 fn
    fingerprint_sources: tuple[str, ...] = ()
    fingerprint_fn: Optional[Callable[[], str]] = None
    # 套件包根目录（discover 时填入；用于解析 fingerprint_sources）
    root: Optional[Path] = field(default=None, compare=False)

    def fingerprint(self) -> Optional[str]:
        """判定口径指纹 —— 归套件所有（SWE 预留③）。无法计算则 None。"""
        if self.fingerprint_fn is not None:
            return self.fingerprint_fn()
        if self.fingerprint_sources and self.root is not None:
            paths = [self.root / s for s in self.fingerprint_sources]
            missing = [p for p in paths if not p.is_file()]
            if missing:
                return None
            return sha256_files(paths)
        return None

    def to_meta(self) -> dict:
        """写入 batch.json 的稳定子集（不含 root / 回调）。"""
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "fingerprint": self.fingerprint(),
        }


def suite_from_module(mod) -> Optional[Suite]:
    """从套件子包读取模块级 `SUITE`（Suite 实例或含 id 的 dict）。

    兼容两种写法：
        SUITE = Suite(...)           # 推荐
        SUITE = {"id": "...", ...}   # 轻量
    """
    raw = getattr(mod, "SUITE", None)
    if raw is None:
        # 兼容 P-1 过渡期的 SUITE_ID 字符串
        sid = getattr(mod, "SUITE_ID", None)
        if isinstance(sid, str) and sid:
            root = Path(getattr(mod, "__file__", "") or ".").parent
            return Suite(id=sid, title=getattr(mod, "__doc__", "") or "",
                         root=root)
        return None
    if isinstance(raw, Suite):
        if raw.root is None:
            root = Path(getattr(mod, "__file__", "") or ".").parent
            raw = Suite(
                id=raw.id, title=raw.title, kind=raw.kind,
                description=raw.description,
                fingerprint_sources=raw.fingerprint_sources,
                fingerprint_fn=raw.fingerprint_fn,
                root=root,
            )
        return raw
    if isinstance(raw, dict) and raw.get("id"):
        root = Path(getattr(mod, "__file__", "") or ".").parent
        return Suite(
            id=str(raw["id"]),
            title=str(raw.get("title", "")),
            kind=str(raw.get("kind", "internal")),
            description=str(raw.get("description", "")),
            fingerprint_sources=tuple(raw.get("fingerprint_sources") or ()),
            root=root,
        )
    return None


__all__ = ["Suite", "sha256_files", "suite_from_module"]
