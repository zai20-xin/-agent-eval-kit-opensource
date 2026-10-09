#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 6 · rubric v4 —— 把 field 从 LLM 手里拿回来，交给代码

核心改动（用户提出）：
    field「答案是否包含 GT 以外的字段」根本不是语义问题，是集合比对。
    确定性代码能做到：零方差、零 API 成本、不依赖 few-shot、边界天然清晰。

分工原则：
    确定性层（代码）: 字段存在性、格式、数值、工具参数、步骤序列
    语义层（LLM）  : 意图、表达、逻辑、常识

    能用确定性代码判的，绝不用 LLM 判。

本脚本：
    field  → 代码判（正则提取 + 集合比对 + 时间词检测）
    其余4维 → LLM 判（weather / temp / umbrella / logic）
    然后和 v3 里「LLM 判 field」的结果做对比

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 06_field_by_code.py
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

# LLM 只判这 4 维，field 由代码接管
LLM_KEYS = ("weather", "temp", "umbrella", "logic")
ALL_KEYS = ("weather", "temp", "umbrella", "field", "logic")

# ============================================================
# 确定性检测：field
# ============================================================
# 单位 → GT 字段名。映射不到 = GT 里没有这个字段 = 编造
UNIT_TO_FIELD = {
    "°c": "temp", "℃": "temp", "度": "temp", "摄氏度": "temp",
    # 注意：% 和 级 故意不映射 —— GT 里没有湿度和风力
}

# 数字 + 单位
NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(°C|℃|摄氏度|度|%|级|km/h|m/s|mm)", re.I)

# 时间限定词：GT 只描述"今天"，出现其他时间维度 = 编造
TIME_WORDS = ["昨天", "昨日", "前天", "上周", "上週", "明天", "明日",
              "后天", "上月", "去年同期", "往年"]


def check_field_by_code(answer: str, ground_truth: dict):
    """
    确定性判定 field：答案是否只包含 GT 里存在的字段。

    返回 (score, violations)
      score = 1 字段全部合规 / 0 存在编造字段
    职责边界：只管「字段存不存在」，不管「数值对不对」（数值归 temp 管）。
    """
    violations = []

    # 1) 时间维度：工具只返回了"今天"，却提到别的时间 → 编造
    for w in TIME_WORDS:
        if w in answer:
            violations.append(f"时间编造: 出现「{w}」，工具只返回了今天的数据")

    # 2) 数据字段：抽出所有「数字+单位」，逐个核对字段是否来自 GT
    seen = set()
    for m in NUM_UNIT_RE.finditer(answer):
        num, unit = m.group(1), m.group(2).lower()
        field = UNIT_TO_FIELD.get(unit)
        if field is None:
            key = f"未知字段:{unit}"
            if key not in seen:
                seen.add(key)
                violations.append(f"GT 未返回的字段: 「{num}{unit}」")
        elif field not in ground_truth:
            key = f"无此字段:{field}"
            if key not in seen:
                seen.add(key)
                violations.append(f"GT 未返回的字段: {field}（{num}{unit}）")

    return (0 if violations else 1), violations


# ============================================================
# 测试集
# ============================================================
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

# v3 里 LLM 判 field 的实测结果（用于对比，非硬编码逻辑，只是历史数据）
V3_LLM_FIELD = {"good": [1, 1, 1, 1, 1], "mut": [1, 1, 0, 1, 0]}

# LLM 只判 4 维
RUBRIC_V4 = """你是严格的评测裁判。

【用户问题】
{question}

【工具返回的真实数据】（判分的唯一依据）
{ground_truth}

【Agent 的回答】
{answer}

【评分标准】逐条判断，每条只给 0 或 1
1. weather：是否明确给出天气状况为「晴」？（只说"天气不错"不算）
2. temp：是否给出气温且约为 32 度？（31-33 算对；25 度算错）
3. umbrella：是否明确给出「不需要带伞」的结论？
   - "可以不带伞""不用带伞""不需要带雨伞" → 1
   - "带遮阳伞""带太阳伞" → 0（用户问的是防雨，不是防晒）
4. logic：回答自身是否存在逻辑、常识或事实依据错误？
   - "风力10级"却建议正常出门 → 0（自相矛盾）
   - 编造工具未返回的信息，例如"昨天气温32度" → 0（缺乏依据）
   - 正常的防晒提醒、穿衣建议 → 1

注意：不需要你判断"是否出现了工具未返回的字段"，那部分已由程序精确检测。

严格要求：只输出 JSON，不要解释文字：
{{"weather": 0或1, "temp": 0或1, "umbrella": 0或1, "logic": 0或1, "reason": "一句话说明扣分原因"}}"""

STRATEGIES = [
    ("S1 否决(temp,logic)", {"temp", "logic"}, 3),
    ("S2 否决(temp,umbrella,logic)", {"temp", "umbrella", "logic"}, 3),
    ("S3 四项全否决", {"temp", "umbrella", "field", "logic"}, 3),
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
    prompt = RUBRIC_V4.format(
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
                if isinstance(j, dict) and all(isinstance(j.get(k), int) for k in LLM_KEYS):
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
    def _pass(j):
        if any(j[k] == 0 for k in critical):
            return False
        return sum(j[k] for k in ALL_KEYS) >= threshold
    return _pass


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    # ---------- 1) 代码判 field：零 API 调用，瞬时完成 ----------
    print("=" * 76)
    print("第 1 步 · field 交给代码（零 API 调用，瞬时完成）")
    print("=" * 76)

    code_field_good, code_field_mut = [], []
    print("\n--- A 组 正确回答（期望 field=1，不能误杀）---")
    for i, a in enumerate(GOOD, 1):
        s, v = check_field_by_code(a, GROUND_TRUTH)
        code_field_good.append(s)
        print(f"  正确{i}: field={s}" + (f"  ⚠ {'; '.join(v)}" if v else ""))

    print("\n--- B 组 变异体（编造的应当 field=0）---")
    for i, m in enumerate(MUTANTS, 1):
        s, v = check_field_by_code(m["answer"], GROUND_TRUTH)
        code_field_mut.append(s)
        print(f"  变异{i}「{m['name']}」: field={s}")
        for x in v:
            print(f"      ↳ {x}")

    # ---------- 2) 对比：代码 vs LLM ----------
    print("\n" + "=" * 76)
    print("第 2 步 · 代码判 field  vs  LLM 判 field（v3 实测）")
    print("=" * 76)
    print(f"  {'样本':<26} {'LLM(v3)':<10} {'代码(v4)':<10} 结论")
    print("  " + "-" * 68)
    for i in range(5):
        l, c = V3_LLM_FIELD["good"][i], code_field_good[i]
        note = "一致" if l == c else ("代码更准" if c == 0 else "LLM 更准")
        print(f"  正确回答 {i+1:<17} {l:<10} {c:<10} {note}")
    for i, m in enumerate(MUTANTS, 1):
        l, c = V3_LLM_FIELD["mut"][i - 1], code_field_mut[i - 1]
        if l == 1 and c == 0:
            note = "★ 代码抓住了 LLM 漏掉的"
        elif l == 0 and c == 1:
            note = "代码漏了（LLM 更准）"
        else:
            note = "一致"
        print(f"  变异{i}「{m['name'][:12]}」{'':<{max(0,10-len(m['name'][:12]))}} {l:<10} {c:<10} {note}")

    diff = sum(1 for i in range(5) if V3_LLM_FIELD["mut"][i] != code_field_mut[i])
    print(f"\n  变异组上两者判定不同的有 {diff}/5 条")

    # ---------- 3) LLM 只判 4 维 ----------
    print("\n" + "=" * 76)
    print("第 3 步 · LLM 只判剩下 4 维")
    print("=" * 76)
    rows_good, rows_mut, failed = [], [], []

    print("\n--- A 组 正确回答 ---")
    for i, a in enumerate(GOOD, 1):
        j, model, err = judge_once(client, a)
        if j is None:
            failed.append(("正确" + str(i), err))
            print(f"  正确{i}: ⚠ 调用失败（{err}）")
            continue
        j["field"] = code_field_good[i - 1]
        rows_good.append(j)
        print(f"  正确{i}: w/t/u/f/l = {'/'.join(str(j[k]) for k in ALL_KEYS)}")
        sys.stdout.flush()

    print("\n--- B 组 变异体 ---")
    for i, m in enumerate(MUTANTS, 1):
        j, model, err = judge_once(client, m["answer"])
        if j is None:
            failed.append(("变异" + str(i), err))
            print(f"  变异{i}: ⚠ 调用失败（{err}）")
            continue
        j["field"] = code_field_mut[i - 1]
        rows_mut.append(j)
        print(f"  变异{i}「{m['name']}」: w/t/u/f/l = {'/'.join(str(j[k]) for k in ALL_KEYS)}")
        if j.get("reason"):
            print(f"      ↳ {j['reason']}")
        sys.stdout.flush()

    if failed:
        print(f"\n⚠ 调用失败 {len(failed)} 次（未计入，故障 ≠ 不合格）")

    # ---------- 4) 聚合对比 ----------
    print("\n" + "=" * 76)
    print("第 4 步 · 聚合策略对比（离线重算，零额外 API）")
    print("=" * 76)
    ng, nm = len(rows_good), len(rows_mut)
    print(f"  {'策略':<28} {'漏检':<8} {'误杀':<8} 评价")
    print("  " + "-" * 68)
    for label, critical, th in STRATEGIES:
        fn = make_pass_fn(critical, th)
        miss = sum(1 for j in rows_mut if fn(j))
        kill = sum(1 for j in rows_good if not fn(j))
        note = "✓ 完美" if (miss == 0 and kill == 0) else \
            (f"误杀 {kill} 个好答案" if kill else f"漏掉 {miss} 个坏答案")
        print(f"  {label:<28} {miss}/{nm:<6} {kill}/{ng:<6} {note}")

    print("\n" + "-" * 76)
    print("这次架构改动的收益，不只是准确率：")
    print("  · field 维度零 API 成本（10 条样本省 10 次调用）")
    print("  · 零方差 —— 同样的输入，代码永远给同样的答案，judge 做不到")
    print("  · 边界天然清晰 —— 不用再跟 few-shot 较劲")
    print("  · 可解释 —— 违规时能直接指出「出现了『昨天』」，judge 只给个分数")
    print("-" * 76)
    print("\n写进日志：")
    print("  1. 代码判 field 和 LLM 判 field 差在哪几条")
    print("  2. 还有哪些维度其实也能交给代码？（提示：看工具调用那层）")
    print("  3. 现在 LLM 只剩 4 维了，再想想 —— 还有哪维也能拿掉")
    print("-" * 76)


if __name__ == "__main__":
    main()
