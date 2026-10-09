#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weather/run_eval.py —— 天气套件统一入口（W1 答案级三层）

一次跑完三层：
    1. 确定性层（代码）: field 集合比对 + temp 数值区间    ← 零方差、零 API
    2. 语义层（LLM）   : weather + umbrella + logic        ← 有方差，需校准
    3. 聚合层          : 一票否决 + 阈值，并做敏感性分析      ← 离线重算，零额外 API

跑法（P-1 后根目录不再有 run_eval.py）：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 -m evalkit.suites.weather.run_eval

P-1 剥离：自根目录 run_eval.py 迁入。
"""
import json
import sys

from evalkit.judge import get_client, judge
from evalkit.aggregate import sensitivity
from evalkit.suites.weather import (
    check_field, check_temp,
    WEATHER_KEYS, WEATHER_STRATEGIES,
)

# ---------------------------------------------------------------- 场景配置
GROUND_TRUTH = {"weather": "晴", "temp": 32, "rain": False}
QUESTION = "北京今天天气怎么样？需要带伞吗？"

# LLM 只判这 3 维
LLM_KEYS = ("weather", "umbrella", "logic")

RUBRIC = """你是严格的评测裁判。

【用户问题】
{question}

【工具返回的真实数据】（判分的唯一依据）
{ground_truth}

【Agent 的回答】
{answer}

【评分标准】逐条判断，每条只给 0 或 1
1. weather：是否明确给出天气状况为「晴」？（只说"天气不错""阳光很好"不算，必须明确是晴）
2. umbrella：是否明确给出「不需要带伞」的结论？
   - "可以不带伞""不用带伞""不需要带雨伞" → 1
   - "带遮阳伞""带太阳伞" → 0（用户问的是防雨，不是防晒）
   - 完全没提要不要带伞 → 0
3. logic：回答自身是否存在逻辑、常识或事实依据错误？
   - "风力10级"却建议正常出门 → 0（自相矛盾）
   - 编造工具未返回的信息，例如"昨天气温32度" → 0（缺乏依据）
   - 正常的防晒提醒、穿衣建议 → 1

注意：气温数值与字段合规已由程序精确检测，你不需要判断。

严格要求：只输出 JSON，不要解释文字：
{{"weather": 0或1, "umbrella": 0或1, "logic": 0或1, "reason": "一句话说明扣分原因"}}"""

# ---------------------------------------------------------------- 测试集
GOOD = [
    "今天北京天气晴朗，气温约 **32°C**，**没有降雨**。\n**建议**：不需要带伞，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！",
    "北京今天天气**晴**，气温约 **32 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。",
    "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：不需要带雨伞**。如果您担心晒太阳，可以带帽子或太阳镜，但不用专门为了防雨准备雨伞。祝您出行愉快！",
    "北京今天天气**晴**，气温约**32°C**，目前**没有降雨预报**，**不需要带伞**。不过白天阳光较强，出门记得做好防晒哦！",
    "北京今天天气**晴**，气温**32℃**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！",
]

MUTANTS = [
    {"name": "A5 编造依据",
     "answer": "今天北京天气晴朗，昨天气温**32°C**今天和昨日气温一样，**没有降雨**。\n**建议**：不需要考虑下雨问题，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！"},
    {"name": "A4 事实错误(温度)",
     "answer": "北京今天天气**晴**，气温约 **25 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。"},
    {"name": "A7 答非所问(遮阳伞)",
     "answer": "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：如果您担心晒太阳，可以带遮阳伞。祝您出行愉快！"},
    {"name": "A8 完全回避",
     "answer": "建议查看实时天气预报"},
    {"name": "A6 夹带幻觉字段",
     "answer": "北京今天天气**晴**，气温**32℃**，**适度99%**，**风力10级**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！"},
]


def evaluate(client, answer):
    """对一条答案跑完整三层，返回 (scores, notes)"""
    notes = []

    # 1) 确定性层
    f_score, f_viol = check_field(answer, GROUND_TRUTH)
    t_score, t_vals, t_diag = check_temp(answer, GROUND_TRUTH["temp"])
    for v in f_viol:
        notes.append(f"[field] {v}")
    if t_diag:
        notes.append(f"[temp] {t_diag}")

    # 2) 语义层
    prompt = RUBRIC.format(question=QUESTION,
                           ground_truth=json.dumps(GROUND_TRUTH, ensure_ascii=False),
                           answer=answer)
    j, model, err = judge(client, prompt, LLM_KEYS)
    if j is None:
        return None, [f"[judge] 调用失败：{err}（不计入统计，故障 ≠ 不合格）"]

    scores = dict(j)
    scores["field"] = f_score
    scores["temp"] = t_score
    if j.get("reason"):
        notes.append(f"[judge] {j['reason']}")
    return scores, notes


def main():
    try:
        client = get_client()
    except RuntimeError as e:
        print(f"✗ {e}")
        sys.exit(1)

    print("=" * 74)
    print("evalkit  ·  确定性层(代码) + 语义层(LLM) + 聚合层")
    print("=" * 74)

    def run_group(items, expect_pass, label):
        print(f"\n--- {label}（期望全部{'通过' if expect_pass else '不通过'}）---")
        rows, failed = [], 0
        for i, it in enumerate(items, 1):
            ans = it if isinstance(it, str) else it["answer"]
            name = f"正确{i}" if isinstance(it, str) else f"变异{i}「{it['name']}」"
            scores, notes = evaluate(client, ans)
            if scores is None:
                failed += 1
                print(f"  {name}: ⚠ 调用失败")
                for n in notes:
                    print(f"      {n}")
                continue
            rows.append(scores)
            total = sum(scores[k] for k in WEATHER_KEYS)
            bits = "/".join(str(scores[k]) for k in WEATHER_KEYS)
            print(f"  {name}: w/t/u/f/l = {bits}  总分 {total}/5")
            for n in notes:
                print(f"      ↳ {n}")
        return rows, failed

    good, f1 = run_group(GOOD, True, "A 组 正确回答")
    print()
    mut, f2 = run_group(MUTANTS, False, "B 组 变异体")
    if f1 + f2:
        print(f"\n⚠ 调用失败 {f1+f2} 次，已排除、未计入分母")

    # 3) 聚合 + 敏感性分析
    print("\n" + "=" * 74)
    print("聚合层：敏感性分析（同一批评分，换公式离线重算）")
    print("=" * 74)
    res = sensitivity(good, mut, WEATHER_STRATEGIES, WEATHER_KEYS)

    perfect = [r for r in res if r[1] == 0 and r[2] == 0]
    print()
    if perfect:
        print(f"  达到 0 漏检 + 0 误杀的策略: {', '.join(r[0].split()[0] for r in perfect)}")
    else:
        print("  没有策略同时做到 0 漏检 0 误杀，需要在两者之间取舍")

    print("\n" + "-" * 74)
    print("  确定性层承担了 2/5 维度（field、temp），零 API、零方差、可单测。")
    print("  语义层只剩 3 维 —— 维度越少，judge 越不容易跑偏，但也越容易")
    print("  变成兜底口袋（失败模式 C4：维度渗透）。")
    print("-" * 74)


if __name__ == "__main__":
    main()
