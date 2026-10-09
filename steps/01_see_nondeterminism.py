#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 1 · 亲手看见「非确定性」

做什么：
    用同一个问题，问同一个 Agent 5 次，把 5 次的答案摊开并排看。

为什么这是第一步：
    你没法评测一个你没亲眼见过的东西。Agent 评测的全部难点，
    都源于一个事实——同样的输入，它会给你不一样的输出。
    在看懂这个之前，谈 pass@k、谈轨迹评测都是空中楼阁。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 01_see_nondeterminism.py

看完之后问自己三个问题（写在日志里）：
    1. 5 次里，工具调用的参数有几次是对的？
    2. 5 次里，最终答案有几种不同表述？
    3. 如果这是你负责的功能，你会怎么写断言？—— 写不出来，就对了。
"""
import json
import os
import sys
from collections import Counter
from openai import OpenAI

# ---------- 配置 ----------
MODEL = "nvidia/nemotron-3.5-lightning:free"
RUNS = 5
TEMPERATURE = 0.8          # 故意调高，放大差异，让你一次看个够
QUESTION = "北京今天天气怎么样？需要带伞吗？"

# ---------- 一个假工具（不联网，数据写死，保证变量只有一个：模型本身） ----------
WEATHER_DB = {
    "北京": {"weather": "晴", "temp": 32, "rain": False},
    "上海": {"weather": "小雨", "temp": 22, "rain": True},
    "深圳": {"weather": "雷阵雨", "temp": 28, "rain": True},
}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气情况",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如：北京"}
            },
            "required": ["city"],
        },
    },
}]


def get_weather(city: str) -> dict:
    """工具本体：查表，查不到就明确报未知（不编数据）"""
    city = (city or "").strip()
    return WEATHER_DB.get(city, {"error": f"未知城市：{city}"})


def ask_once(client: OpenAI, question: str, temperature: float) -> dict:
    """跑一次完整的 agent 回合：问 → 模型决定调工具 → 执行 → 回填 → 出结论"""
    messages = [{"role": "user", "content": question}]
    trace = {"tool_called": False, "tool_args": None, "answer": "", "error": None}

    try:
        # 第一轮：模型拿到问题 + 工具清单，决定要不要调工具
        r1 = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS,
            tool_choice="auto", temperature=temperature, timeout=60,
        )
        msg = r1.choices[0].message

        if not msg.tool_calls:
            # 模型选择不调工具 —— 这本身就是一种失败模式，值得记下来
            trace["answer"] = msg.content or ""
            return trace

        # 执行工具调用
        call = msg.tool_calls[0]
        args = json.loads(call.function.arguments)
        trace["tool_called"] = True
        trace["tool_args"] = args

        result = get_weather(**args)

        # 第二轮：把工具结果回填给模型，让它出最终结论
        messages.append(msg)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False),
        })
        r2 = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=temperature, timeout=60,
        )
        trace["answer"] = r2.choices[0].message.content or ""

    except Exception as e:
        trace["error"] = f"{type(e).__name__}: {e}"

    return trace


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )

    print(f"模型: {MODEL}   温度: {TEMPERATURE}   重复: {RUNS} 次")
    print(f"问题: {QUESTION}")
    print("=" * 72)

    results = []
    for i in range(RUNS):
        t = ask_once(client, QUESTION, TEMPERATURE)
        results.append(t)
        print(f"\n【第 {i+1}/{RUNS} 次】")
        if t["error"]:
            print(f"  ✗ 出错: {t['error']}")
        else:
            if t["tool_called"]:
                ok = t["tool_args"].get("city") == "北京"
                print(f"  工具调用: get_weather(city={t['tool_args'].get('city')!r})  "
                      f"{'✓ 参数正确' if ok else '✗ 参数错误'}")
            else:
                print("  工具调用: （没有调用工具，直接回答了）")
            a_ok = '不需要带伞' in t["answer"].strip()[:200]
            print(f"  最终回答: {(t['answer'] or '').strip()[:200]}"  f"{'✓ 回答正确' if a_ok else '✗回答错误'}")
        sys.stdout.flush()

    # ---------- 汇总：这就是最朴素的「评测指标」 ----------
    print("\n" + "=" * 72)
    print("汇总（三个最朴素的指标）")
    print("=" * 72)

    ok_runs = [r for r in results if not r["error"]]
    called = [r for r in ok_runs if r["tool_called"]]
    arg_ok = [r for r in called if r["tool_args"].get("city") == "北京"]
    answers = [r["answer"].strip() for r in ok_runs if r["answer"].strip()]

    def pct(a, b):
        return f"{a}/{b}（{100*a//b if b else 0}%）"

    print(f"1. 工具调用率   : {pct(len(called), len(ok_runs))}      ← 该调的工具调了吗")
    print(f"2. 参数准确率   : {pct(len(arg_ok), len(called)) if called else '—'}      ← 参数传对了吗")
    print(f"3. 答案一致率   : {pct(len(answers) and max(Counter(answers).values()), len(answers))}"
          f"      ← 同一个问题，几次答案一样")

    uniq = Counter(answers)
    print(f"\n   {len(answers)} 次回答里出现了 {len(uniq)} 种不同表述：")
    for a, n in uniq.most_common():
        print(f"   ×{n}  {a[:150]}")

    print("\n" + "-" * 72)
    print("这三个数字，就是 Agent 评测的地基。后面的八周，都是把它们做细、做可信。")
    print("-" * 72)
    print("\n现在把这三件事写下来：")
    print("  1. 上面三个数字分别是多少")
    print("  2. 你看到的、意料之外的差异（哪怕只有一条）")
    print("  3. 如果让你给这个功能写断言，你卡在哪")


if __name__ == "__main__":
    main()
