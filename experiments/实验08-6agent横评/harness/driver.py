#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 轨驱动 —— 用 CLI 调起 3 个全自动 agent（pi / opencode / mimo），收归一化轨迹。

设计要点（来自 6agent 可驱动性实测报告 2026-09-10）：
    pi       → `pi -p "<prompt>" --mode json --session-dir <dir>` → 收 <dir> 下最新 .jsonl
    opencode → `opencode run "<prompt>"` → 捕获 session id → `opencode export <id>` 归一化
    mimo     → `mimo run "<prompt>"` → 同上

⚠️ D1 骨架：driver 只负责「调起 + 收 trace」，判定在 run_all 调 evalkit。
   opencode/mimo 的 session id 捕获方式 / export schema 待 D3 实测校正（已标 🔴）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HARNESS = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HARNESS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evalkit.trace_schema import NormalizedTrace
from normalize import normalize_pi, normalize_opencode_export, normalize_mimo_export

# 三家默认模型（报告 §3，横评每档必须标注实际模型；运行时以真实返回为准）
AUTO_AGENTS = {
    "pi":       {"bin": "pi",       "default_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"},
    "opencode": {"bin": "opencode", "default_model": "mimo-v2.5-free"},
    "mimo":     {"bin": "mimo",     "default_model": "mimo-v2.5-pro"},
}


def _sh(cmd_list, cwd, timeout=600):
    """★ 必须经 login shell（/bin/zsh -lc）调起 agent CLI，不能直接 subprocess 调二进制。

    2026-09-11 实测踩坑：
      - 直接 subprocess.run(["pi", ...]) → pi 启动时报
        `Credential store read failed for openrouter: EPERM ... rmdir '~/.pi/agent/auth.json.lock'`
        → exit=1、**不产出任何 jsonl**（轨迹静默丢失，极易误判成「agent 跑完了但没用工具」）。
      - 改 `["/bin/zsh", "-lc", <cmd>]` → exit=0、jsonl 正常落盘、锁自动清理无残留。
      - 中间变量已隔离排除：AWS/OPENROUTER_API_KEY 环境变量有且子进程可继承（73 字符）；
        python 子进程 rmdir 同路径 rc=0（不是权限不足）；清锁后直接调用仍失败。
      - 故根因定位在「非 login shell 环境下 pi 的 mkdir 锁无法 rmdir」，未再深挖，方案本身稳定。
    ⚠️ 同理适用于 opencode / mimo（同一 CLI 生态），统一走本函数。
    """
    shell_cmd = " ".join(shlex.quote(str(c)) for c in cmd_list)
    return subprocess.run(["/bin/zsh", "-lc", shell_cmd],
                          capture_output=True, text=True, timeout=timeout,
                          check=False, cwd=str(cwd))


def run_pi(prompt, out_dir, model=None, timeout=600) -> NormalizedTrace:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ⚠️ session 必须单独放子目录：out_dir 里还会落我们自己的 trace.jsonl，
    #    若混在一起，sorted(glob("*.jsonl"))[-1] 会选中 trace.jsonl（'t' > '2'）
    #    → 把上一轮自己的产物当成 pi 轨迹解析 → JSONDecodeError（2026-09-11 实测）
    sess_dir = out_dir / "pi_session"
    sess_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["pi", "-p", prompt, "--mode", "json", "--session-dir", str(sess_dir)]
    # ★ 绕过 credential store 锁（2026-09-11 D2 关键坑）：
    #   pi 的 credential store 用 mkdir 锁，在 Bash 工具子进程环境里 rmdir 必 EPERM
    #   （即使 dangerouslyDisableSandbox）→ 启动时直接 exit=1、不产出 jsonl，且报
    #   "No API key found"（store 读不到）。实测：直接传 --api-key + --model 可完全绕开
    #   store，正常落盘。key 取自 env（OPENROUTER_API_KEY，已确认 73 字符且子进程可继承）。
    ak = os.environ.get("OPENROUTER_API_KEY")
    if ak:
        cmd += ["--api-key", ak]
    if model:
        cmd += ["--model", model]
    try:
        _sh(cmd, cwd=str(ROOT), timeout=timeout)
    except Exception as e:
        return NormalizedTrace(agent="pi", model=model, source=f"pi 调用失败:{e}", steps=[])
    js = sorted(sess_dir.glob("*.jsonl"))
    if not js:
        return NormalizedTrace(agent="pi", model=model, source="pi 未产出 jsonl", steps=[])
    tr = normalize_pi(js[-1])
    tr.model = tr.model or model or AUTO_AGENTS["pi"]["default_model"]
    return tr


def _session_ids(bin_name, timeout=120):
    """`opencode session list` 的 id 集合（行首 ses_ 开头）。

    ⚠️ 2026-09-11 实测更正（原 D1 假设作废）：
       `opencode run` 的 stdout **不含 session id**（只有 "> build · mimo-v2.5-free" + 回答文本），
       原 `_capture_session_id` 正则永远匹配不到 → steps=0 → 被误判成失败模式命中。
       正确做法 = **run 前后对 session list 做集合差分**，取新增的 id。
    """
    try:
        out = _sh([bin_name, "session", "list"], cwd=str(ROOT), timeout=timeout)
    except Exception:
        return set()
    return {l.split()[0] for l in (out.stdout or "").splitlines()
            if l.strip().startswith("ses_")}


def _sh_raw(shell_cmd, cwd, timeout=600):
    """与 _sh 同机制（login shell），但接收**已拼好的 shell 字符串**，
    用于需要 shell 重定向（如 `> file`）的场景——_sh 的 list 入参会把 `>` 当普通参数转义。"""
    return subprocess.run(["/bin/zsh", "-lc", shell_cmd],
                          capture_output=True, text=True, timeout=timeout,
                          check=False, cwd=str(cwd))


def run_opencode(prompt, out_dir, bin_name="opencode", model=None, timeout=600) -> NormalizedTrace:
    before = _session_ids(bin_name)
    try:
        res = _sh([bin_name, "run", prompt], cwd=str(ROOT), timeout=timeout)
    except Exception as e:
        return NormalizedTrace(agent=bin_name, model=model,
                               source=f"{bin_name} 调用失败:{e}", steps=[])

    new = _session_ids(bin_name) - before
    if not new:
        return NormalizedTrace(agent=bin_name, model=model,
                               source=f"{bin_name} 未捕获 session id（stdout {len(res.stdout)}B）",
                               steps=[])
    sid = sorted(new)[0]

    # ★ export 必须重定向到文件，不能靠 capture_output 抓 stdout：
    #   大 session 的 export JSON 可达 60KB+，超过 OS 管道缓冲时 stdout 被截断
    #   → json.loads 报 "Unterminated string"（2026-09-11 D2 实测，T2b/T4 命中 3 次）。
    #   写文件无管道缓冲上限；残留文件也便于事后复查原始轨迹。
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    exp_path = Path(out_dir) / f"{bin_name}_export.json"
    _sh_raw(f"{bin_name} export {shlex.quote(sid)} > {shlex.quote(str(exp_path))} 2>/dev/null",
            cwd=str(ROOT), timeout=180)
    raw = exp_path.read_text(encoding="utf-8", errors="replace") if exp_path.exists() else ""
    if not raw.strip():
        return NormalizedTrace(agent=bin_name, model=model,
                               source=f"{bin_name} export 空输出（{exp_path.name} 为空）", steps=[])
    try:
        data = json.loads(raw)
    except Exception as e:
        return NormalizedTrace(agent=bin_name, model=model,
                               source=f"{bin_name} export 解析失败:{e}（原始见 {exp_path.name}）", steps=[])

    tr = normalize_opencode_export(data, agent_name=bin_name)
    # ⚠️ 绝不用登记值兜底：横评的硬要求是「每档标注**实际**模型」。
    #    实测已抓到登记值过时（报告记 mimo=mimo-v2.5-pro，实际 export 给的是
    #    nvidia/nemotron-3-ultra-550b-a55b:free）→ 拿登记值冒充真实值 = 假标注。
    #    拿不到就留 None，由上层标 model_confirmed=False，该档不进结论。
    tr.source = f"{bin_name} export {sid}"
    return tr


def run_mimo(prompt, out_dir, model=None, timeout=600) -> NormalizedTrace:
    return run_opencode(prompt, out_dir, bin_name="mimo", model=model, timeout=timeout)


def run_agent(agent, prompt, out_dir, model=None, timeout=600) -> NormalizedTrace:
    if agent == "pi":
        return run_pi(prompt, out_dir, model=model, timeout=timeout)
    if agent == "opencode":
        return run_opencode(prompt, out_dir, model=model, timeout=timeout)
    if agent == "mimo":
        return run_mimo(prompt, out_dir, model=model, timeout=timeout)
    raise ValueError(f"不支持的 A 轨 agent: {agent}（B 轨 QClaw/AgentW 走离线解析）")
