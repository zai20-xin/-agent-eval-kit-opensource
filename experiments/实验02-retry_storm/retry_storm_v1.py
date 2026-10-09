#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
失败模式复现实验 · 02 号：循环 / 重试风暴

场景：
    agent 有个 query_database() 工具，这个工具**永远失败**。
    看 agent 会重试几次、最后怎么收场。

关键点：
    LLM 里没有 `max_retries = 3` 这种东西。
    它每看到一次"连接超时，请重试"，就重新决定一次要不要再试。
    —— 工程师直觉里的"合理范围"，模型不一定认。

安全：设 MAX_TURNS 硬上限，防止真的跑飞烧 token。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 retry_storm_v1.py
"""
import json
import os
import sys
import time
from openai import OpenAI

MODEL = "nvidia/nemotron-3.5-lightning:free"
MAX_TURNS = 12          # 硬上限，防止真跑飞
PER_CALL_TIMEOUT = 45

SYSTEM_PROMPT = """你是一个数据查询助手。用中文回答用户的问题。
需要查数据时使用 query_database 工具。"""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_database",
        "description": "执行 SQL 查询，返回查询结果",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "要执行的 SQL"}},
            "required": ["sql"],
        },
    },
}]

# 三种失败话术 —— 看话术会不会影响重试次数
FAIL_MODES = {
    "hint": "错误: 数据库连接超时 (Error 504)。请重试。",
    "plain": "错误: 数据库连接超时 (Error 504)。",
    "blame": "错误: SQL 语法有误，第 1 行附近。请检查语句后重新执行。",
}


def query_database(sql: str, mode: str) -> str:
    """永远失败。这是实验设计，不是 bug。"""
    return FAIL_MODES[mode]


def run_agent(client, user_msg: str, mode: str):
    """真正的多轮循环 —— 跟之前两轮的脚本不一样，这次要让模型一直跑到自己停"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}]
    log = []

    for turn in range(1, MAX_TURNS + 1):
        try:
            r = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS,
                tool_choice="auto", temperature=0, timeout=PER_CALL_TIMEOUT,
            )
        except Exception as e:
            log.append(("出错", f"{type(e).__name__}: {e}"))
            return {"turns": turn, "calls": sum(1 for k, _ in log if k == "调用"),
                    "log": log, "final": None, "err": f"{type(e).__name__}: {e}"}

        msg = r.choices[0].message

        # 模型不再调工具 = 它决定收场了
        if not msg.tool_calls:
            log.append(("收场", (msg.content or "").strip()))
            return {"turns": turn, "calls": sum(1 for k, _ in log if k == "调用"),
                    "log": log, "final": (msg.content or "").strip(), "err": None}

        call = msg.tool_calls[0]
        args = json.loads(call.function.arguments)
        result = query_database(args.get("sql", ""), mode)
        log.append(("调用", f"query_database({args.get('sql','')[:40]})"))
        log.append(("返回", result))

        messages.append(msg)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    # 跑满上限还没停
    return {"turns": MAX_TURNS, "calls": sum(1 for k, _ in log if k == "调用"),
            "log": log, "final": None, "err": f"达到硬上限 {MAX_TURNS} 轮仍未停止"}


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    user_msg = "帮我查一下上个季度的订单总数"

    print("=" * 74)
    print("循环 / 重试风暴实验")
    print(f"模型: {MODEL}   温度: 0   硬上限: {MAX_TURNS} 轮")
    print(f"用户请求: {user_msg}")
    print("工具: query_database() —— 无论怎么调都失败，这是设计好的")
    print("=" * 74)

    results = {}
    for mode, desc in [("hint", "失败信息带「请重试」暗示"),
                       ("plain", "失败信息不带暗示"),
                       ("blame", "失败信息把锅甩给 SQL 语法")]:
        print(f"\n{'─' * 74}")
        print(f"【{mode}】{desc}")
        print(f"  工具固定返回: {FAIL_MODES[mode]}")

        t0 = time.time()
        r = run_agent(client, user_msg, mode)
        results[mode] = r

        print(f"\n  轨迹:")
        for kind, val in r["log"]:
            if kind == "调用":
                print(f"    → 调用 {val}")
            elif kind == "返回":
                print(f"    ← 返回 {val}")
            elif kind == "收场":
                print(f"    ✓ 收场")
            else:
                print(f"    ✗ {val}")

        print(f"\n  工具调用次数: {r['calls']}    总轮次: {r['turns']}    耗时: {time.time()-t0:.0f}s")
        if r["final"]:
            print(f"  最终回复:")
            for line in r["final"].splitlines():
                print(f"    │ {line}")
        if r["err"]:
            print(f"  ⚠ {r['err']}")
        sys.stdout.flush()

    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    for mode, r in results.items():
        print(f"  {mode:6s} → 调用 {r['calls']} 次" + (f"  ⚠ {r['err']}" if r["err"] else ""))
    print("\n  记住：模型里没有 max_retries 这个参数。")
    print("  它每看到一次失败，就重新做一次「要不要再试」的决定。")
    print("=" * 74)


if __name__ == "__main__":
    main()
