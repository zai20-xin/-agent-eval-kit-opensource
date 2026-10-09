#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 5 · rubric v3 = 你的三条改进 + 一个更省事的解法

你的改动：
  1. 一票否决从公式里抽离出来，做成独立的 pass 判断（CRITICAL 集合）
  2. field 加 few-shot 示例，并把 ground_truth 直接贴给裁判
  3. logic  加 few-shot 示例和反面教材

我补的一条（针对 field / logic 边界模糊）：
  既然同一个缺陷可能被归到 field 或 logic 任一边 —— 那就**两边都纳入一票否决**。
  用冗余对抗分类不确定性，比花力气消除模糊便宜得多，而且立刻生效。

★ 本脚本的额外设计：
  judge 只跑一次，拿到 10 条评分后，用 5 种不同的聚合策略**离线重算**。
  零额外 API 调用，就能看出「公式」本身对结论的影响有多大。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 05_rubric_v3.py
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
KEYS = ("weather", "temp", "umbrella", "field", "logic")

GOOD = [
    "今天北京天气晴朗，气温约 **32°C**，**没有降雨**。\n**建议**：不需要带伞，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！",
    "北京今天天气**晴**，气温约 **32 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。",
    "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：不需要带雨伞**。如果您担心晒太阳，可以带帽子或太阳镜，但不用专门为了防雨准备雨伞。祝您出行愉快！",
    "北京今天天气**晴**，气温约**32°C**，目前**没有降雨预报**，**不需要带伞**。不过白天阳光较强，出门记得做好防晒哦！",
    "北京今天天气**晴**，气温**32℃**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！",
]

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

# ---------- rubric v3：你的 few-shot + ground_truth 直贴 ----------
RUBRIC_V3 = """你是严格的评测裁判。

【用户问题】
{question}

【工具返回的真实数据】（这是判分的唯一依据）
{ground_truth}

【Agent 的回答】
{answer}

【评分标准】逐条判断，每条只给 0 或 1
1. weather：是否明确给出天气状况为「晴」？（只说"天气不错"不算）
2. temp：是否给出气温且约为 32 度？（31-33 都算对）
3. umbrella：是否明确给出「不需要带伞」的结论？
   - "可以不带伞""不用带伞""不需要带雨伞" → 1
   - "带遮阳伞""带太阳伞" → 0（用户问的是防雨，不是防晒）
4. field：是否只包含真实数据里的字段，没有编造事实性数据？
   - 说"注意防晒""穿透气衣物""祝您愉快" → 1（建议和客套不算字段）
   - 说"湿度60%""风力3级"但工具没返回 → 0（编造了事实性数据）
5. logic：回答自身是否存在逻辑或常识错误？
   - 说"风力10级"却建议正常出门 → 0（自相矛盾）
   - 正常的防晒提醒、穿衣建议 → 1

严格要求：只输出 JSON，不要解释文字：
{{"weather": 0或1, "temp": 0或1, "umbrella": 0或1, "field": 0或1, "logic": 0或1, "reason": "一句话说明扣分原因"}}"""

# ---------- 一票否决：从公式里抽离出来，做成可配置的策略 ----------
STRATEGIES = [
    ("S0 纯阈值 ≥3，无一票否决", set(), 3),
    ("S1 v2 原版否决(temp,logic)", {"temp", "logic"}, 3),
    ("S2 否决+umbrella", {"temp", "umbrella", "logic"}, 3),
    ("S3 四项全否决（v3 建议）", {"temp", "umbrella", "field", "logic"}, 3),
    ("S4 五项全否决+阈值4", {"weather", "temp", "umbrella", "field", "logic"}, 4),
]


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
    prompt = RUBRIC_V3.format(
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


def make_pass_fn(critical, threshold):
    """一票否决逻辑抽离成一个可替换的策略函数"""
    def _pass(j):
        if any(j[k] == 0 for k in critical):
            return False
        return sum(j[k] for k in KEYS) >= threshold
    return _pass


def score_all(rows_good, rows_mut):
    """用 5 种聚合策略离线重算，零额外 API 调用"""
    out = []
    for label, critical, th in STRATEGIES:
        fn = make_pass_fn(critical, th)
        miss = sum(1 for j in rows_mut if fn(j))
        kill = sum(1 for j in rows_good if not fn(j))
        out.append((label, critical, th, miss, kill))
    return out


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    print("=" * 76)
    print("rubric v3 · few-shot + ground_truth 直贴 · 跑两次(正确组/变异组)")
    print("=" * 76)

    rows_good, rows_mut, failed = [], [], []

    print("\n--- A 组 · 正确回答（期望全通过）---")
    for i, a in enumerate(GOOD, 1):
        j, model, err = judge_once(client, a)
        if j is None:
            failed.append(("正确" + str(i), err))
            print(f"  正确{i}: ⚠ 调用失败（{err}）")
            continue
        rows_good.append(j)
        print(f"  正确{i}: {'/'.join(str(j[k]) for k in KEYS)} = {sum(j[k] for k in KEYS)}/5")
        sys.stdout.flush()

    print("\n--- B 组 · 变异体（期望全不通过）---")
    for i, m in enumerate(MUTANTS, 1):
        j, model, err = judge_once(client, m["answer"])
        if j is None:
            failed.append(("变异" + str(i), err))
            print(f"  变异{i}: ⚠ 调用失败（{err}）")
            continue
        rows_mut.append(j)
        mark = ""
        if j["field"] == 0:
            mark += " [field=0]"
        if j["logic"] == 0:
            mark += " [logic=0]"
        print(f"  变异{i}「{m['name']}」: {'/'.join(str(j[k]) for k in KEYS)}"
              f" = {sum(j[k] for k in KEYS)}/5{mark}")
        sys.stdout.flush()

    if failed:
        print(f"\n⚠ 调用失败 {len(failed)} 次（未计入统计，故障 ≠ 不合格）")

    # ---------- 聚合策略对比：同一批评分，换公式重算 ----------
    print("\n" + "=" * 76)
    print("聚合策略对比（同一批 judge 评分，只换公式，不重新调用模型）")
    print("=" * 76)
    print(f"  {'策略':<28} {'漏检':<8} {'误杀':<8} 评价")
    print("  " + "-" * 70)
    res = score_all(rows_good, rows_mut)
    ng, nm = len(rows_good), len(rows_mut)
    for label, critical, th, miss, kill in res:
        if miss == 0 and kill == 0:
            note = "✓ 完美"
        elif kill > 0:
            note = f"误杀 {kill} 个好答案"
        else:
            note = f"漏掉 {miss} 个坏答案"
        print(f"  {label:<28} {miss}/{nm:<6} {kill}/{ng:<6} {note}")

    best = [r for r in res if r[3] == 0 and r[4] == 0]
    print("\n  " + "-" * 70)
    if best:
        print(f"  → 达到 0 漏检 + 0 误杀的策略：{', '.join(b[0].split()[0] for b in best)}")
    else:
        print("  → 没有策略能同时做到 0 漏检 0 误杀，需要在两者之间取舍")

    print("\n" + "-" * 76)
    print("看明白了吗：judge 一个字没改，光换聚合公式，结论就能从「漏检4个」")
    print("变到「全抓住」。聚合规则不是配角，它跟 rubric 同等重要。")
    print("-" * 76)
    print("\n写进日志：")
    print("  1. 哪几个策略做到了 0 漏检 + 0 误杀")
    print("  2. 最严的 S4 有没有开始误杀好答案 —— 如果有，说明过拟合到这 10 条了")
    print("  3. field 和 logic 是不是又被归到同一边（看上面 [field=0] [logic=0] 标记）")
    print("-" * 76)


if __name__ == "__main__":
    main()
