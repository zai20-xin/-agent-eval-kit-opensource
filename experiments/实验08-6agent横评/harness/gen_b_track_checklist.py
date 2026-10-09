#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 B 轨（QClaw / AgentW）GUI 作业清单。

★ 为什么要脚本生成而不是手写：
    B 轨与 A 轨的 prompt 必须**逐字一致**，否则两轨数据不可比，横评表就废了。
    手抄必然漂移（尤其路径和标点）→ 一律从 tasks.json 生成，tasks.json 是唯一真源。

用法：
    python3 harness/gen_b_track_checklist.py
输出：
    tasks/B轨作业清单.md
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
ROOT = HARNESS.parents[2]              # agent-eval-kit
TASKS = HARNESS.parent / "tasks" / "tasks.json"
OUT = HARNESS.parent / "tasks" / "B轨作业清单.md"

# B 轨在 GUI 里执行，agent 的 cwd 不确定 → 一律用绝对路径
ABS_ROOT = str(ROOT)
REL_MARK = "experiments/实验08-6agent横评/"

RESET_CMD = (f"cd {ABS_ROOT} && git checkout -- {REL_MARK}tasks/fixtures "
             f"&& git clean -fdq {REL_MARK}tasks/fixtures")


def abs_prompt(p: str) -> str:
    return p.replace(REL_MARK, ABS_ROOT + "/" + REL_MARK)


def main():
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    agents = [("QClaw", "~/.qclaw/workspace 会话树"),
              ("AgentW", "~/.agentw/audit-log/*.jsonl")]

    L = []
    L.append("# B 轨作业清单 · QClaw / AgentW（GUI 人工执行）")
    L.append("")
    L.append("> 本文件**由脚本生成**（`harness/gen_b_track_checklist.py`），请勿手改。")
    L.append("> 改任务请改 `tasks/tasks.json` 后重跑脚本 —— B 轨 prompt 必须与 A 轨逐字一致，否则两轨不可比。")
    L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 0 · 开工前必读（3 条）")
    L.append("")
    L.append("1. **每次作业前先复位 fixtures**（有副作用的任务会改文件，不复位则后几次看到的是前一次的残留）：")
    L.append("")
    L.append("   ```bash")
    L.append(f"   {RESET_CMD}")
    L.append("   ```")
    L.append("2. **一条消息 = 一次作业**：把下面每个任务的整段 prompt 复制粘贴进对话框，发送，**等它彻底跑完**再进下一个。")
    L.append("3. **中途不要手动改文件**。agent 建了/改了文件都是正常实验现象，跑完由我复位。")
    L.append("")
    L.append("### 时间窗记录（很关键）")
    L.append("")
    L.append("B 轨靠**离线解析已落盘的轨迹文件**采集，靠时间戳定位是哪一次作业。")
    L.append("所以每次发送 prompt 前，在纸上（或回我一句）记一下**当前时间**即可。")
    L.append("")
    L.append("| agent | 轨迹位置 |")
    L.append("|---|---|")
    for a, loc in agents:
        L.append(f"| {a} | `{loc}` |")
    L.append("")
    L.append("---")
    L.append("")

    for ai, (agent, _loc) in enumerate(agents, 1):
        L.append(f"## {ai} · {agent}（共 {len(tasks)} 次作业）")
        L.append("")
        L.append("| # | 任务 | 失败模式 | 开始前时间 | 完成 |")
        L.append("|---|---|---|---|---|")
        for i, t in enumerate(tasks, 1):
            L.append(f"| {i} | {t['id']} | {t.get('failure_mode','')} | ____:____ | ☐ |")
        L.append("")
        for i, t in enumerate(tasks, 1):
            L.append(f"### {agent} · 第 {i} 次 · {t['id']}（{t.get('failure_mode','')}）")
            L.append("")
            L.append("> 复制下面代码块里的**全部内容**，粘贴发送：")
            L.append("")
            L.append("```text")
            L.append(abs_prompt(t["prompt"]))
            L.append("```")
            L.append("")
        L.append("---")
        L.append("")

    L.append("## 全部跑完之后")
    L.append("")
    L.append("回我一句「跑完了」+ 每段的起止时间，剩下的解析、判定、出表全部由脚本完成。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 边界（结论里必须原样带上）")
    L.append("")
    L.append("- **B 轨 N=1**：每个任务只跑 1 次，是**单次观察**，不得写成统计结论。")
    L.append("- 半自动轨无法无人值守重复跑，这是设计上接受的代价。")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"已生成：{OUT}")
    print(f"任务数：{len(tasks)} × agent 数：{len(agents)} = {len(tasks)*len(agents)} 次作业")
    return 0


if __name__ == "__main__":
    sys.exit(main())
