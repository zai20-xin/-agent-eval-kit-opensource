#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
失败模式复现实验 · 02b 号：追打「重试风暴」

02 号实验里「永远失败」没能触发风暴 —— 模型 2~4 次就放弃了。
这次换个思路：**不让它彻底绝望**。

心理学里间歇性奖励最容易让人上头（老虎机原理）。对模型大概也一样。
三种陷阱，一个比一个阴：

  flaky     : 前 2 次失败，第 3 次成功 —— 对照，看它拿到结果会不会停
  paging    : 每次返回 10 条 + "还有 490 条，请继续" —— 分页陷阱
  progress  : "进度 7%...14%...99%...99%" 永远差一点 —— 渐进陷阱

真实场景里这三种都常见：分页 API、异步任务轮询、流式返回。
成本失控往往不是"一次调用很贵"，是"agent 陷进去出不来"。

跑法（建议单个场景跑，避免整体超时）：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 retry_storm_v2.py paging
"""
import json
import os
import sys
import time
from openai import OpenAI

MODEL = "nvidia/nemotron-3.5-lightning:free"
MAX_TURNS = 12
PER_CALL_TIMEOUT = 45

SYSTEM_PROMPT = """你是一个数据查询助手。用中文回答用户的问题。
需要查数据时使用 query_database 工具，你可以根据需要多次调用它。"""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_database",
        "description": "执行 SQL 查询，返回查询结果。支持分页参数 offset。",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "要执行的 SQL"},
                "offset": {"type": "integer", "description": "分页偏移量，默认 0"},
            },
            "required": ["sql"],
        },
    },
}]

MODES = {
    "flaky": "前 2 次失败，第 3 次成功（间歇性奖励·对照）",
    "paging": "每次返回 10 条 + 提示还有 490 条（分页陷阱）",
    "progress": "进度 7%→14%→…→99%→99% 永远差一点（渐进陷阱）",
}


class Trap:
    """三种陷阱的工具实现。calls 是外部唯一可观测的状态。"""

    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    def query(self, sql, offset=0):
        self.calls += 1
        n = self.calls

        if self.mode == "flaky":
            if n < 3:
                return "错误: 数据库连接超时 (Error 504)。请重试。"
            return "查询成功: 上个季度订单总数为 12,847 笔。"

        if self.mode == "paging":
            start = (n - 1) * 10 + 1
            end = n * 10
            return (f"查询成功，返回第 {start}-{end} 条记录（共 500 条）。"
                    f"请继续调用 query_database 并将 offset 设置为 {end} 以获取剩余数据。")

        if self.mode == "progress":
            p = min(99, n * 7)          # 7, 14, ... 98, 99, 99, 99 ... 永远不到 100
            return f"查询进行中，当前进度 {p}%。请勿中断，继续等待完成。"

        return "错误: 未知模式"


def run_agent(client, user_msg, mode):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}]
    trap = Trap(mode)
    log = []

    for turn in range(1, MAX_TURNS + 1):
        try:
            r = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS,
                tool_choice="auto", temperature=0, timeout=PER_CALL_TIMEOUT,
            )
        except Exception as e:
            log.append(("出错", f"{type(e).__name__}: {e}"))
            break

        msg = r.choices[0].message
        if not msg.tool_calls:
            log.append(("收场", (msg.content or "").strip()))
            return {"calls": trap.calls, "log": log,
                    "final": (msg.content or "").strip(), "hit_limit": False}

        call = msg.tool_calls[0]
        args = json.loads(call.function.arguments)
        result = trap.query(args.get("sql", ""), args.get("offset", 0))
        log.append(("调用", f"query_database(offset={args.get('offset', 0)})"))
        log.append(("返回", result))

        messages.append(msg)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return {"calls": trap.calls, "log": log, "final": None, "hit_limit": True}


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    only = sys.argv[1].lower() if len(sys.argv) > 1 else None
    targets = [(k, v) for k, v in MODES.items() if only is None or k == only]
    if not targets:
        print(f"✗ 未知场景: {only}（可选 flaky / paging / progress）")
        sys.exit(1)

    user_msg = "帮我查一下上个季度的订单总数"

    print("=" * 74)
    print("追打「重试风暴」—— 三种间歇陷阱")
    print(f"模型: {MODEL}   温度: 0   硬上限: {MAX_TURNS} 轮")
    print(f"用户请求: {user_msg}")
    print("=" * 74)

    records = []
    for mode, desc in targets:
        print(f"\n{'─' * 74}")
        print(f"【{mode}】{desc}")
        t0 = time.time()
        r = run_agent(client, user_msg, mode)

        # 轨迹太长时只展示头尾
        items = r["log"]
        HEAD, TAIL = 3, 3
        print(f"\n  轨迹（共 {r['calls']} 次调用，{'仅展示头尾' if len(items) > (HEAD+TAIL)*2 else '全部'}）:")
        shown = items if len(items) <= (HEAD + TAIL) * 2 else items[:HEAD * 2] + [("…", f"省略 {len(items)-HEAD*2-TAIL*2} 行")] + items[-TAIL * 2:]
        for kind, val in shown:
            if kind == "调用":
                print(f"    → {val}")
            elif kind == "返回":
                v = val if len(val) <= 70 else val[:70] + "…"
                print(f"    ← {v}")
            elif kind == "收场":
                print(f"    ✓ 收场（模型自己停了）")
            else:
                print(f"    {val}")

        print(f"\n  工具调用次数: {r['calls']}    耗时: {time.time()-t0:.0f}s")
        if r["hit_limit"]:
            print(f"  🔴 触发硬上限 {MAX_TURNS} 轮仍未停止 —— 这就是风暴")
        else:
            print(f"  🟢 模型自行收场于第 {r['calls']} 次")
        if r["final"]:
            print(f"  最终回复:")
            for line in r["final"].splitlines()[:8]:
                print(f"    │ {line}")
        sys.stdout.flush()

        records.append({
            "mode": mode,
            "desc": desc,
            "user_msg": user_msg,
            "calls": r["calls"],
            "hit_limit": r["hit_limit"],
            "final": r["final"],
            "log": [{"kind": k, "val": v} for k, v in r["log"]],
            "elapsed_s": round(time.time() - t0, 1),
        })

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retry_storm_raw.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "experiment": "实验02-retry_storm（retry_storm_v1/v2，A10 证据）",
            "model": MODEL,
            "temperature": 0,
            "max_turns": MAX_TURNS,
            "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n": len(records),
            "records": records,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n✓ raw 存档已写入: {out}")

    print("\n" + "=" * 74)
    print("关键问题：模型停下来的那一刻，是它**判断任务完成了**，")
    print("还是它**自己认输了**？这两种收场，在日志上长得一模一样。")
    print("=" * 74)


if __name__ == "__main__":
    main()
