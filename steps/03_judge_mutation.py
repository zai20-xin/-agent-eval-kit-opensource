#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 3 · 给你的 Judge 做「变异测试」

背景：
    STEP 2 里 judge 判了 5 条全对，三轮一致、零判错。
    但那只证明它「能把对的判成对」—— 一个只会输出满分的傻子模型也能做到。
    真正的检验是：喂错的，它抓不抓得出来。

这就是测试领域的老朋友 —— 变异测试（mutation testing）：
    测试用例的有效性不靠「通过率」证明，靠「能不能杀死变异体」证明。
    用在 LLM judge 上，就是往答案里注入已知缺陷，看 judge 会不会翻红。

本脚本的输入 = 用户亲手设计的 5 个变异体（每个都标了注入的缺陷）。
期望：全部判「不通过」。判「通过」的 = 漏检（假阴性）。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 03_judge_mutation.py
"""
import json
import os
import re
import sys
import time
from openai import OpenAI

# ---------- 裁判（沿用 STEP 2 的选型结论）----------
JUDGE_MODELS = [
    "minimax/minimax-m3:free",
    "cohere/north-mini-code:free",
    "inclusionai/ling-3.0-flash-fin:free",
]
MAX_RETRY = 3

GROUND_TRUTH = {"weather": "晴", "temp": 32, "rain": False, "wind": "10级"}
QUESTION = "北京今天天气怎么样？需要带伞吗？"

# ---------- 变异体：每条都明确标注「注入了什么缺陷」----------
# should_pass 全是 False —— 它们都该被判不合格。
# 判成通过的就是漏检，而漏检比误杀危险：坏答案上线了，用户看到的是错的。
MUTANTS = [
    {
        "name": "时间错位 + 编造依据",
        "answer": "今天北京天气晴朗，昨天气温**32°C**今天和昨日气温一样，**没有降雨**。\n**建议**：不需要考虑下雨问题，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！",
        "defect": "引入工具未返回的信息：昨天的气温、今天与昨天的对比",
        "why_tricky": "结论数字是对的（32度），但支撑过程是编的",
    },
    {
        "name": "事实错误（温度）",
        "answer": "北京今天天气**晴**，气温约 **25 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。",
        "defect": "气温说成 25°C，工具明明返回 32°C",
        "why_tricky": "其余全对，只有一个数字错",
    },
    {
        "name": "答非所问 + 结论回避",
        "answer": "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：如果您担心晒太阳，可以带遮阳伞。祝您出行愉快！",
        "defect": "问的是要不要带伞（防雨），答的是遮阳伞（防晒），没给明确结论",
        "why_tricky": "天气和温度都对，只有结论被偷换了概念",
    },
    {
        "name": "完全回避作答",
        "answer": "建议查看实时天气预报",
        "defect": "零实质信息，什么都没答",
        "why_tricky": "不假，但完全没用 —— 这是 agent 最常见的失败形态",
    },
    {
        "name": "夹带幻觉数据",
        "answer": "北京今天天气**晴**，气温**32℃**，**湿度99%**，**风力10级**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！",
        "defect": "编造了「湿度99%」—— 工具根本没返回这两个字段",
        "why_tricky": "关键信息全对，幻觉只是附加的 —— 最危险的一种",
    },
]

# ---------- Rubric：STEP 2 那三条，一条没改 ----------
RUBRIC = """你是严格的评测裁判。以下是用户问题、工具返回的真实数据、以及 Agent 的回答。

【用户问题】
{question}

【工具返回的真实数据】
{ground_truth}

【Agent 的回答】
{answer}

【评分标准】—— 逐条判断，每条只给 0 或 1，不要给中间分
1. weather：回答是否明确给出了天气状况为「晴」？（只说"天气不错"不算，必须明确是晴）
2. temp：回答是否给出了气温，且明确是今天，且数值约为 32 度？（31-33 都算对）
3. umbrella：回答是否明确给出了「不需要带伞」的结论？（"可以不带伞""不用带伞""不需要带雨伞"都算对;"太阳伞"不对）
4. field：回答不能包含GROUND_TRUTH以外的字段
5. logic: 回答自身不能包含逻辑和常识性错误（"风力大于6级"不适合出门）

严格要求：只输出一个 JSON 对象，不要任何解释文字，格式如下：
{{"weather": 0或1, "temp": 0或1, "umbrella": 0或1, "field","logic","reason": "一句话说明扣分原因"}}"""


def parse_json_loose(text: str):
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def judge_once(client: OpenAI, answer: str):
    """返回 (评分dict|None, 模型, 错误|None)。调用失败返回 None，绝不当成 0 分。"""
    prompt = RUBRIC.format(
        question=QUESTION,
        ground_truth=json.dumps(GROUND_TRUTH, ensure_ascii=False),
        answer=answer,
    )
    last_err = None
    for model in JUDGE_MODELS:
        for attempt in range(MAX_RETRY):
            try:
                r = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                    temperature=0, timeout=90,
                )
                j = parse_json_loose(r.choices[0].message.content)
                if isinstance(j, dict) and all(
                    isinstance(j.get(k), int) for k in ("weather", "temp", "umbrella")
                ):
                    return j, model, None
                last_err = f"{model} 返回 JSON 不完整"
            except Exception as e:
                last_err = f"{model}: {type(e).__name__}"
                if "RateLimit" in type(e).__name__ or "429" in str(e):
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            time.sleep(1)
    return None, None, last_err


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    print("=" * 74)
    print("变异测试：5 个变异体，全部应当判「不通过」")
    print("判成「通过」的 = 漏检（假阴性）")
    print("=" * 74)

    caught, missed, failed = [], [], []

    for i, m in enumerate(MUTANTS, 1):
        j, model, err = judge_once(client, m["answer"])

        if j is None:
            failed.append((i, err))
            print(f"\n【变异 {i}】{m['name']}")
            print(f"  ⚠ 调用失败（{err}）→ 不计入统计")
            continue

        total = j["weather"] + j["temp"] + j["umbrella"] + j["field"] + j["logic"]
        passed = total >=3 and j["temp"] != 0 and j["logic"] != 0
        (missed if passed else caught).append((i, m, total, j))

        verdict = "判为【通过】← ⚠ 漏检！" if passed else "判为【不通过】✓ 抓住了"
        print(f"\n【变异 {i}】{m['name']}")
        print(f"  注入的缺陷 : {m['defect']}")
        print(f"  judge 打分 : {j['weather']}/{j['temp']}/{j['umbrella']} = {total} 分   {verdict}")
        if j.get("reason"):
            print(f"  judge 理由 : {j['reason']}")
        sys.stdout.flush()

    # ---------- 汇总 ----------
    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    n_valid = len(caught) + len(missed)
    if n_valid:
        print(f"  有效评判 : {n_valid}/5")
        print(f"  抓住     : {len(caught)} 个")
        print(f"  漏检     : {len(missed)} 个  →  漏检率 {100*len(missed)//n_valid}%")
    if failed:
        print(f"  调用失败 : {len(failed)} 次（未计入，故障 ≠ 合格）")

    if missed:
        print("\n" + "-" * 74)
        print("漏检的变异体 —— 这些就是你 judge 的能力边界")
        print("-" * 74)
        for i, m, total, j in missed:
            print(f"\n  变异 {i}「{m['name']}」得了 {total}/3 分")
            print(f"    缺陷: {m['defect']}")
            print(f"    难点: {m['why_tricky']}")
            print(f"    → rubric 为什么没抓住：三条标准全在问「该说的说了没有」，")
            print(f"      没有一条在问「不该说的说了没有」。")

    print("\n" + "-" * 74)
    print("这一步的产出（写进日志，也写进失败模式清单）：")
    print("  1. 漏检了哪几条，为什么 rubric 抓不住")
    print("  2. 如果让你改 rubric，你会加第几条标准？（先别看答案，自己想）")
    print("-" * 74)


if __name__ == "__main__":
    main()
