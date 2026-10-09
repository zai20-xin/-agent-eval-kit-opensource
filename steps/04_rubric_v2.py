#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 4 · 试跑你设计的 rubric v2（5 条标准 + 新公式）

你的改动：
  4. field   : 回答不能包含 GROUND_TRUTH 以外的字段
  5. logic   : 回答自身不能包含逻辑和常识性错误（如"风力10级"却建议出门）
  3. umbrella: 增加反例 —— "太阳伞"不算对
  公式      : total = weather+temp+umbrella+field+logic
              passed = total >= 3 and temp != 0 and logic != 0

★ 本脚本同时跑两组，这点是故意的：
    A 组 = STEP 1 那 5 个正确回答（期望：全部通过）
    B 组 = 5 个变异体（期望：全部不通过）

  只看 B 组会漏掉最重要的事 —— 修漏检的同时，误杀涨了没有。
  评测的每次改动都必须双向验证，这就是 precision / recall 的权衡。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 04_rubric_v2.py
"""
import json
import os
import re
import sys
import time
from openai import OpenAI

JUDGE_MODELS = [
    "minimax/minimax-m3:free",
    "cohere/north-mini-code:free",
    "inclusionai/ling-3.0-flash-fin:free",
]
MAX_RETRY = 3

GROUND_TRUTH = {"weather": "晴", "temp": 32, "rain": False}
QUESTION = "北京今天天气怎么样？需要带伞吗？"

# ---------- A 组：STEP 1 跑出来的 5 个正确回答，期望全部「通过」----------
GOOD = [
    "今天北京天气晴朗，气温约 **32°C**，**没有降雨**。\n**建议**：不需要带伞，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！",
    "北京今天天气**晴**，气温约 **32 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。",
    "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：不需要带雨伞**。如果您担心晒太阳，可以带帽子或太阳镜，但不用专门为了防雨准备雨伞。祝您出行愉快！",
    "北京今天天气**晴**，气温约**32°C**，目前**没有降雨预报**，**不需要带伞**。不过白天阳光较强，出门记得做好防晒哦！",
    "北京今天天气**晴**，气温**32℃**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！",
]

# ---------- B 组：5 个变异体，期望全部「不通过」----------
MUTANTS = [
    {"name": "时间错位+编造依据",
     "answer": "今天北京天气晴朗，昨天气温**32°C**今天和昨日气温一样，**没有降雨**。\n**建议**：不需要考虑下雨问题，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！",
     "defect": "引入工具未返回的信息：昨天气温、今日与昨日的对比"},
    {"name": "事实错误(温度)",
     "answer": "北京今天天气**晴**，气温约 **25 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。",
     "defect": "气温说成 25°C，工具返回 32°C"},
    {"name": "答非所问(遮阳伞)",
     "answer": "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：如果您担心晒太阳，可以带遮阳伞。祝您出行愉快！",
     "defect": "问的是防雨的伞，答的是遮阳伞，没给明确结论"},
    {"name": "完全回避",
     "answer": "建议查看实时天气预报",
     "defect": "零实质信息"},
    {"name": "夹带幻觉数据",
     "answer": "北京今天天气**晴**，气温**32℃**，**适度99%**，**风力10级**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！",
     "defect": "编造「适度99%」「风力10级」"},
]

# ---------- rubric v2：你的版本（JSON 里 field/logic 的 0或1 我补全了）----------
RUBRIC_V2 = """你是严格的评测裁判。以下是用户问题、工具返回的真实数据、以及 Agent 的回答。

【用户问题】
{question}

【工具返回的真实数据】
{ground_truth}

【Agent 的回答】
{answer}

【评分标准】—— 逐条判断，每条只给 0 或 1，不要给中间分
1. weather：回答是否明确给出了天气状况为「晴」？（只说"天气不错"不算，必须明确是晴）
2. temp：回答是否给出了气温，且数值约为 32 度？（31-33 都算对）
3. umbrella：回答是否明确给出了「不需要带伞」的结论？（"可以不带伞""不用带伞""不需要带雨伞"都算对；说"带遮阳伞""带太阳伞"不算对，因为用户问的是防雨）
4. field：回答不能包含 GROUND_TRUTH 以外的字段。
5. logic：回答自身不能包含逻辑和常识性错误（例如"风力10级"却建议正常出门）。

严格要求：只输出一个 JSON 对象，不要任何解释文字，格式如下：
{{"weather": 0或1, "temp": 0或1, "umbrella": 0或1, "field": 0或1, "logic": 0或1, "reason": "一句话说明扣分原因"}}"""

KEYS = ("weather", "temp", "umbrella", "field", "logic")


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
    prompt = RUBRIC_V2.format(
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
                if isinstance(j, dict) and all(isinstance(j.get(k), int) for k in KEYS):
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


def passed_of(j: dict) -> bool:
    """你的公式（忠实实现）"""
    total = sum(j[k] for k in KEYS)
    return total >= 3 and j["temp"] != 0 and j["logic"] != 0


def run_group(client, items, expect_pass, label):
    print("=" * 74)
    print(f"{label}   期望：全部{'通过' if expect_pass else '不通过'}")
    print("=" * 74)
    wrong, ok, failed = [], [], []
    for i, it in enumerate(items, 1):
        answer = it if isinstance(it, str) else it["answer"]
        name = f"正确回答 {i}" if isinstance(it, str) else f"变异 {i}「{it['name']}」"
        j, model, err = judge_once(client, answer)
        if j is None:
            failed.append((name, err))
            print(f"\n{name}\n  ⚠ 调用失败（{err}）→ 不计入统计")
            continue
        p = passed_of(j)
        total = sum(j[k] for k in KEYS)
        good = (p == expect_pass)
        (ok if good else wrong).append((name, it, j, total, p))
        mark = "✓" if good else ("✗ 误杀" if expect_pass else "✗ 漏检")
        bits = "/".join(str(j[k]) for k in KEYS)
        print(f"\n{name}")
        print(f"  weather/temp/umbrella/field/logic = {bits}   总分 {total}/5   → "
              f"{'通过' if p else '不通过'}   {mark}")
        if j.get("reason"):
            print(f"  理由: {j['reason']}")
        sys.stdout.flush()
    return ok, wrong, failed


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    ok_g, wrong_g, fail_g = run_group(client, GOOD, True, "A 组 · 正确回答（看有没有被误杀）")
    print()
    ok_m, wrong_m, fail_m = run_group(client, MUTANTS, False, "B 组 · 变异体（看有没有漏检）")

    # ---------- 双向汇总 ----------
    print("\n" + "=" * 74)
    print("双向汇总")
    print("=" * 74)
    ng = len(ok_g) + len(wrong_g)
    nm = len(ok_m) + len(wrong_m)
    if ng:
        print(f"  A 组 正确回答 : 误杀 {len(wrong_g)}/{ng}  （误杀率 {100*len(wrong_g)//ng}%）← 越低越好")
    if nm:
        print(f"  B 组 变异体   : 漏检 {len(wrong_m)}/{nm}  （漏检率 {100*len(wrong_m)//nm}%）← 越低越好")
    print(f"  调用失败      : {len(fail_g)+len(fail_m)} 次（未计入，故障 ≠ 不合格）")

    print("\n  对照 v1（3 条标准 + 阈值≥2）：")
    print("    漏检率 80%   误杀率 40%（字面断言）/ 0%（judge v1）")

    if wrong_g:
        print("\n" + "-" * 74)
        print("被误杀的正确答案 —— 这是新标准带来的代价")
        print("-" * 74)
        for name, it, j, total, p in wrong_g:
            print(f"\n  {name}: {bits_of(j)} = {total}/5 → 不通过")
            print(f"    理由: {j.get('reason','')}")
        print("\n  → 重点看 field 那一列。把「建议」「客套」「推理」也算成 GT 外字段，")
        print("    会把好答案一起杀掉。")

    if wrong_m:
        print("\n" + "-" * 74)
        print("仍然漏检的变异体 —— 新问题")
        print("-" * 74)
        for name, it, j, total, p in wrong_m:
            d = it.get("defect", "") if isinstance(it, dict) else ""
            print(f"\n  {name}: {bits_of(j)} = {total}/5 → 通过")
            print(f"    缺陷: {d}")
            print(f"    理由: {j.get('reason','')}")

    print("\n" + "-" * 74)
    print("写进日志：")
    print("  1. 误杀率和漏检率各是多少，跟 v1 比是变好还是变差")
    print("  2. 被误杀的正确答案，是死在哪条标准上")
    print("  3. 仍漏检的变异，是标准没覆盖，还是公式放过了")
    print("-" * 74)


def bits_of(j):
    return "/".join(str(j[k]) for k in KEYS)


if __name__ == "__main__":
    main()
