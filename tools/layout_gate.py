#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-eval-kit 落位门禁：每次 git commit 自动跑，结构错就拒绝提交。

用法：
    python3 tools/layout_gate.py                  # 全量查（手工跑）
    python3 tools/layout_gate.py --staged         # 查暂存区（pre-commit 调用）
    python3 tools/layout_gate.py --staged <root>  # 指定项目根

两级：
    FAIL（硬拦 → 退出码 1）—— 只拦「确定是错的」
    WARN（告警 → 只打印）  —— 软质量问题，交给人判断

★ 设计原则：**宁可少拦，也不要误拦**。
  误拦会让人养成 `git commit --no-verify` 的习惯，机制随即全面失效 ——
  这与 AI-OS「靠人记得的机制会空转」是同一类病，只是死法不同。
  所以只有「确定错了」的才 FAIL，「可能有问题」的一律 WARN。
"""
from __future__ import annotations

import py_compile
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

# ─────────────── 配置（跟着项目走，要调整只改这一段）───────────────

# 顶层只允许这些文件 / 目录
# P-1：run_eval.py 已迁入 evalkit/suites/weather/（顶层不再允许场景入口）
# P-0：不新增顶层 tests/ —— 测试放 evalkit/tests/（包内），experiments/ 放实验产物
# P2+ packaging：pyproject.toml 是包元数据（可安装入口），不是散落脚本
# 2026-10-09 开源化：顶层标准件收敛为 README + CHANGELOG（个人工作文档已移除）
TOP_FILES_OK = {"README.md", ".gitignore", "pyproject.toml", "CHANGELOG.md", "LICENSE"}
TOP_DIRS_OK = {"docs", "steps", "experiments", "evalkit", "bare-agent", "tools"}

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".agentw", "archive"}

# pip install -e . 会在仓库根落 `*.egg-info/`（已在 .gitignore）——是构建产物不是顶层条目
EGG_INFO_SUFFIX = ".egg-info"

# experiments/ 下禁止文件名带序号前缀（序号只在 steps/ 有意义）
SEQ_PREFIX_RE = re.compile(r"^\d+[_\-\s]")

# 脚本间硬引用： 常量 = HERE / "xxx.py"
HARD_REF_RE = re.compile(
    r'^\s*[A-Z_][A-Z0-9_]*\s*=\s*[A-Z_][A-Z0-9_]*\s*/\s*["\']([^"\']+\.py)["\']', re.M
)

# 实验目录根层「非脚本文件」数告警阈值
# ★ 2026-09-10 从 12 上调到 30：12 会**误报** —— 按成对命名约定（raw+summary）每轮 2 个文件，
#   一个 3~7 轮的正常实验（实验05 实测 17）必然超过 12，等于**在惩罚遵守约定的行为**。
#   30 ≈「15 轮以上仍未归档」，只作**爆炸兜底**。是否该归档属语义判断（前提：结论不再引用），
#   机器判不了 → 交给 README 的归档规则约束，不靠计数。
ROOT_FILES_WARN_AT = 30

# 暂存区里不该出现的文件
BAD_STAGED_RE = re.compile(r"(\.log$|\.pyc$|__pycache__/|\.DS_Store$|\.log\.bak$)")


class Gate:
    def __init__(self, root: Path, staged_only: bool = False) -> None:
        self.root = root
        self.staged_only = staged_only
        self.fails: list[str] = []
        self.warns: list[str] = []
        self._staged: set[str] | None = None

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def scripts(self):
        for p in sorted(self.root.rglob("*.py")):
            rel = p.relative_to(self.root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            # fixtures/ 下是**被测数据**（测试题附带的 .py），不是项目脚本：
            # 每题一套同名文件（f1.py 在 3 个 N 档里各有一份）是必然形态，
            # 既不需要「跨目录唯一定位」，也不该被当脚本编译 / 查重。
            # 2026-09-21 把 fixture 从 shell 里落盘时被误拦，故排除。
            if "fixtures" in rel.parts:
                continue
            yield p

    def experiment_dirs(self) -> list[Path]:
        base = self.root / "experiments"
        if not base.is_dir():
            return []
        # 跳过下划线开头的辅助目录
        return sorted(d for d in base.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")))

    def staged_paths(self) -> set[str]:
        """暂存区内（新增 / 修改 / 改名）的仓库相对路径。全量模式返回空集。"""
        if not self.staged_only:
            return set()
        if self._staged is None:
            try:
                out = subprocess.run(
                    ["git", "-C", str(self.root), "diff", "--cached", "--name-only",
                     "--diff-filter=ACMR", "-z"],
                    capture_output=True, text=True, check=True,
                ).stdout
                self._staged = {p for p in out.split("\0") if p.strip()}
            except Exception:  # noqa: BLE001
                self._staged = set()
        return self._staged

    def touched(self, d: Path) -> bool:
        """本次提交有没有碰到这个目录。全量模式恒为 True。

        ★ 为什么告警要按「是否碰到」过滤：
          7 个既有实验是先于模板写的，全量告警每次提交刷 20 条 ——
          刷多了人就不看告警了，等于没有告警（与「拦太狠会被 --no-verify 绕过」同源）。
          只报「你这次改到的东西」，才符合 pre-commit 这个时机。
          想全量体检时手工跑 `python3 tools/layout_gate.py`（不带 --staged）。
        """
        if not self.staged_only:
            return True
        prefix = d.relative_to(self.root).as_posix() + "/"
        return any(p.startswith(prefix) for p in self.staged_paths())


# ─────────────────────────── 硬拦检查 ───────────────────────────

def check_top_level(g: Gate) -> None:
    bad = []
    for p in sorted(g.root.iterdir()):
        if p.name in SKIP_DIRS or p.name.startswith("."):
            continue
        if p.is_dir() and p.name.endswith(EGG_INFO_SUFFIX):
            continue
        if p.is_dir() and p.name not in TOP_DIRS_OK:
            bad.append(f"{p.name}/")
        elif p.is_file() and p.name not in TOP_FILES_OK:
            bad.append(p.name)
    if bad:
        g.fail(
            f"顶层出现白名单外的条目：{', '.join(bad)}\n"
            f"      → 落位规则见 README「一、新增产出该放哪」；"
            f"确实需要新增顶层目录时，把它加进 tools/layout_gate.py 的 TOP_DIRS_OK"
        )


# Python 包的标记文件与包内模块：跨目录重名是**必然且无害**的
# （每个包都叫 __init__.py；P-1 后 weather/coding 根 shim 都叫 detectors.py）。
# 不属于「确定是错的」—— 包模块按 import 路径定位，不按裸文件名，
# 与 `HERE / "xxx.py"` 硬引用那类脚本重名不是一回事。
# 按门禁铁律（只拦确定错的，拦太狠会逼人用 --no-verify）排除。
# 2026-09-21 新增 evalkit/adapters/ 子包时被误拦，故加白名单。
# 2026-09-23 P-1 suites/ 落地后 detectors.py ×3 再次触发，改为按父目录有无 __init__.py 判定。
PKG_MARKER_FILES = {"__init__.py"}


def _is_package_module(p: Path) -> bool:
    """父目录含 __init__.py → 包内模块（import 定位），不参与裸文件名唯一性检查。"""
    return (p.parent / "__init__.py").exists()


def check_unique_names(g: Gate) -> None:
    names = [
        p.name
        for p in g.scripts()
        if p.name not in PKG_MARKER_FILES and not _is_package_module(p)
    ]
    dups = {n: c for n, c in Counter(names).items() if c > 1}
    if dups:
        g.fail(f"脚本跨目录重名（无法唯一定位）：{dups}")


def check_compiles(g: Gate) -> None:
    bad = []
    with tempfile.TemporaryDirectory() as td:
        cfile = str(Path(td) / "_chk.pyc")
        for p in g.scripts():
            try:
                py_compile.compile(str(p), doraise=True, cfile=cfile)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{p.relative_to(g.root)}: {e}")
    if bad:
        g.fail("脚本编译失败：\n      - " + "\n      - ".join(bad))


def check_hard_refs(g: Gate) -> None:
    bad = []
    for p in g.scripts():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for target in HARD_REF_RE.findall(text):
            if not (p.parent / target).exists():
                bad.append(f"{p.relative_to(g.root)} -> {target}（目标不存在）")
    if bad:
        g.fail(
            "脚本间硬引用断链（改名时漏改，运行期才会炸）：\n      - "
            + "\n      - ".join(bad)
        )


def check_seq_prefix(g: Gate) -> None:
    bad = []
    for p in sorted((g.root / "experiments").rglob("*.py")):
        rel = p.relative_to(g.root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if SEQ_PREFIX_RE.match(p.name):
            bad.append(str(rel))
    if bad:
        g.fail(
            "experiments/ 下脚本名带了序号前缀（目录已限定范围，序号只会制造跨目录重号）：\n      - "
            + "\n      - ".join(bad)
        )


def check_staged_files(g: Gate) -> None:
    """读暂存区一律走 Gate.staged_paths() —— 单一实现，避免重复踩编码坑。

    ★ 踩坑记录（2026-09-10）：原先这里自己调 `git diff --name-only` 并 splitlines()。
      git 默认会**给非 ASCII 路径加引号并转义**（core.quotepath），拿到的是
      `".../tmp_test.log"` —— 末尾是引号，`\\.log$` 锚点直接失效，该检查形同虚设。
      本项目路径全是中文，所以几乎必然触发。改用 `-z`（NUL 分隔、原样字节）后解决。
    """
    if not g.staged_only:
        return
    bad = [f for f in sorted(g.staged_paths()) if BAD_STAGED_RE.search(f)]
    if bad:
        g.fail(
            "暂存区混入了不该入库的文件（缓存/日志）：\n      - "
            + "\n      - ".join(bad)
            + "\n      → .gitignore 已挡大部分；若确需入库请手工确认后 git add -f"
        )


# ─────────────────────────── 告警检查 ───────────────────────────

def check_exp_root_pileup(g: Gate) -> None:
    for d in g.experiment_dirs():
        if not g.touched(d):
            continue
        n = sum(
            1
            for p in d.iterdir()
            if p.is_file() and p.suffix != ".py"
        )
        if n > ROOT_FILES_WARN_AT:
            g.warn(
                f"{d.name}/ 根层非脚本文件 {n} 个（爆炸兜底阈值 {ROOT_FILES_WARN_AT}）"
                f"→ 按每轮 raw+summary 2 个估约 {n // 2} 轮未归档；"
                f"**结论不再引用**的旧轮次可挪进 archive/"
            )


# ─────────────────────────── 主流程 ───────────────────────────

def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    staged = "--staged" in argv
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent
    if not root.is_dir():
        print(f"不是目录：{root}")
        return 1

    g = Gate(root, staged_only=staged)
    for fn in (
        check_top_level,
        check_unique_names,
        check_compiles,
        check_hard_refs,
        check_seq_prefix,
        check_staged_files,
        check_exp_root_pileup,
    ):
        fn(g)

    mode = "暂存区" if staged else "全量"
    print(f"── 落位门禁 · {root.name} · {mode} ──")
    for w in g.warns:
        print(f"  WARN  {w}")
    for f in g.fails:
        print(f"  FAIL  {f}")

    if g.fails:
        print(f"\n✗ 硬拦 {len(g.fails)} 项 / 告警 {len(g.warns)} 项 —— 拒绝提交")
        print("  确需放行：先修，或在明确知情下用 git commit --no-verify")
        return 1
    if g.warns:
        scope = "仅限本次改动" if staged else "全量"
        print(f"\n✓ 硬拦 0 项 / 告警 {len(g.warns)} 项（{scope}）—— 通过")
        if staged:
            print("  想看全量技术债：python3 tools/layout_gate.py")
    else:
        print("\n✓ 硬拦 0 项 / 告警 0 项 —— 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
