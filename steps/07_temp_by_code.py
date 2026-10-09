#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 7 · rubric v5 —— temp 也交给代码

你的判断：temp 是纯数值范围判断（32±1），连语义都不涉及，代码判更合适。

现在代码接管 2 维：
    field  → 集合比对（GT 外字段 / 时间词）
    temp   → 数值区间（gt-1 <= t <= gt+1）
LLM 只剩 3 维：weather / umbrella / logic

规则：
    数字必须带温度单位才算气温（°C / ℃ / 摄氏度 / 度），
    否则 "风力10级" 里的 10 会被误当温度。
    多个温度值全部要在区间内 —— 编造一个错的也算错。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 07_temp_by_code.py
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

# LLM 只剩这 3 维
LLM_KEYS = ("weather", "umbrella", "logic")
ALL_KEYS = ("weather", "temp", "umbrella", "field", "logic")

# ============================================================
# 确定性层 1：field（沿用 v4）
# ============================================================
UNIT_TO_FIELD = {"°c": "temp", "℃": "temp", "度": "temp", "摄氏度": "temp"}
NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(°C|℃|摄氏度|度|%|级|km/h|m/s|mm)", re.I)
TIME_WORDS = ["昨天", "昨日", "前天", "上周", "上週", "明天", "明日",
              "后天", "上月", "去年同期", "往年"]


def check_field_by_code(answer: str, ground_truth: dict):
    violations = []
    for w in TIME_WORDS:
        if w in answer:
            violations.append(f"时间编造: 出现「{w}」，工具只返回了今天的数据")
    seen = set()
    for m in NUM_UNIT_RE.finditer(answer):
        num, unit = m.group(1), m.group(2).lower()
        field = UNIT_TO_FIELD.get(unit)
        if field is None or field not in ground_truth:
            key = f"{unit}"
            if key not in seen:
                seen.add(key)
                violations.append(f"GT 未返回的字段: 「{num}{unit}」")
    return (0 if violations else 1), violations


# ============================================================
# 确定性层 2：temp（本次新增）
# ============================================================
TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°C|℃|摄氏度|度)")


def check_temp_by_code(answer: str, gt_temp: float, tol: float = 1.0):
    """
    判定 temp：答案里出现的每个温度值，都必须落在 [gt-tol, gt+tol]。

    返回 (score, 提取到的温度列表, 诊断)
    必须带温度单位 —— 否则 "风力10级" 的 10 会被当成温度。
    """
    vals = [float(m.group(1)) for m in TEMP_RE.finditer(answer)]
    if not vals:
        return 0, [], "未提及气温"
    bad = [v for v in vals if abs(v - gt_temp) > tol]
    if bad:
        shown = [int(v) if v.is_integer() else v for v in bad]
        return 0, vals, f"气温 {shown} 超出 {int(gt_temp)}±{int(tol)} 的允许范围"
    return 1, vals, ""


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

# v4 里 LLM 判 temp 的实测结果（历史数据，用于对比）
V4_LLM_TEMP = {"good": [1, 1, 1, 1, 1], "mut": [1, 0, 1, 0, 1]}

# LLM 只判 3 维
RUBRIC_V5 = """你是严格的评测裁判。

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

注意：以下两项已由程序精确检测，你不需要判断：
  - 气温数值是否正确（程序按 32±1 判定）
  - 是否出现了工具未返回的字段（程序做集合比对）

严格要求：只输出 JSON，不要解释文字：
{{"weather": 0或1, "umbrella": 0或1, "logic": 0或1, "reason": "一句话说明扣分原因"}}"""

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
    prompt = RUBRIC_V5.format(
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

    # ---------- 代码判 temp + field ----------
    print("=" * 76)
    print("第 1 步 · 代码判 temp（数值区间 32±1）与 field（集合比对）")
    print("=" * 76)

    code_temp_good, code_temp_mut = [], []
    code_field_good, code_field_mut = [], []

    print("\n--- A 组 正确回答 ---")
    for i, a in enumerate(GOOD, 1):
        ts, vals, diag = check_temp_by_code(a, GROUND_TRUTH["temp"])
        fs, fv = check_field_by_code(a, GROUND_TRUTH)
        code_temp_good.append(ts)
        code_field_good.append(fs)
        v = [int(x) if float(x).is_integer() else x for x in vals]
        print(f"  正确{i}: temp={ts}(提取到{v}) field={fs}" + (f"  ⚠ {diag}" if diag else ""))

    print("\n--- B 组 变异体 ---")
    for i, m in enumerate(MUTANTS, 1):
        ts, vals, diag = check_temp_by_code(m["answer"], GROUND_TRUTH["temp"])
        fs, fv = check_field_by_code(m["answer"], GROUND_TRUTH)
        code_temp_mut.append(ts)
        code_field_mut.append(fs)
        v = [int(x) if float(x).is_integer() else x for x in vals]
        print(f"  变异{i}「{m['name']}」: temp={ts}(提取到{v}) field={fs}")
        if diag:
            print(f"      ↳ temp: {diag}")
        for x in fv:
            print(f"      ↳ field: {x}")

    # ---------- 对比：代码 temp vs LLM temp ----------
    print("\n" + "=" * 76)
    print("第 2 步 · 代码判 temp  vs  LLM 判 temp（v4 实测）")
    print("=" * 76)
    print(f"  {'样本':<26} {'LLM(v4)':<10} {'代码(v5)':<10} 结论")
    print("  " + "-" * 68)
    diff = 0
    for i in range(5):
        l, c = V4_LLM_TEMP["good"][i], code_temp_good[i]
        note = "一致" if l == c else "★ 不一致"
        if l != c:
            diff += 1
        print(f"  正确回答 {i+1:<17} {l:<10} {c:<10} {note}")
    for i, m in enumerate(MUTANTS, 1):
        l, c = V4_LLM_TEMP["mut"][i - 1], code_temp_mut[i - 1]
        note = "一致" if l == c else "★ 不一致"
        if l != c:
            diff += 1
        name = m["name"][:12]
        pad = " " * max(0, 10 - len(name) * 2)
        print(f"  变异{i}「{name}」{pad} {l:<10} {c:<10} {note}")
    print(f"\n  两者判定不同的: {diff}/10 条")
    if diff == 0:
        print("  → 零差异。这意味着 LLM 在这个维度上**没有任何优势**，")
        print("    却带来了方差、成本和 few-shot 维护负担。拿掉它是对的。")

    # ---------- LLM 只判 3 维 ----------
    print("\n" + "=" * 76)
    print("第 3 步 · LLM 只判剩下 3 维（weather / umbrella / logic）")
    print("=" * 76)
    rows_good, rows_mut, failed = [], [], []

    print("\n--- A 组 正确回答 ---")
    for i, a in enumerate(GOOD, 1):
        j, model, err = judge_once(client, a)
        if j is None:
            failed.append(("正确" + str(i), err))
            print(f"  正确{i}: ⚠ 调用失败（{err}）")
            continue
        j["temp"] = code_temp_good[i - 1]
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
        j["temp"] = code_temp_mut[i - 1]
        j["field"] = code_field_mut[i - 1]
        rows_mut.append(j)
        print(f"  变异{i}「{m['name']}」: w/t/u/f/l = {'/'.join(str(j[k]) for k in ALL_KEYS)}")
        if j.get("reason"):
            print(f"      ↳ {j['reason']}")
        sys.stdout.flush()

    if failed:
        print(f"\n⚠ 调用失败 {len(failed)} 次（未计入，故障 ≠ 不合格）")

    # ---------- 聚合对比 ----------
    print("\n" + "=" * 76)
    print("第 4 步 · 聚合策略对比")
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

    # ---------- 诚实算账 ----------
    print("\n" + "=" * 76)
    print("算笔账：把维度拿掉到底省了什么")
    print("=" * 76)
    print("  诚实地说 —— API 调用次数没变（一次调用判多维，5维和3维都是 1 次）。")
    print("  真正省下的是三样：")
    print("    · 方差    : temp/field 现在零方差，同样的输入永远同样的输出")
    print("    · 提示词  : rubric 从 5 条标准缩到 3 条，token 更少，judge 更不容易跑偏")
    print("    · 维护    : 不再需要为这两个维度写 few-shot、调措辞（v3 的退化就是这么来的）")
    print("  多出来的：")
    print("    · 可解释  : 违规能指出「气温 25 超出 32±1」，judge 只给个 0")
    print("    · 可测试  : 这段判定逻辑可以写单元测试，judge 不行")
    print("-" * 76)
    print("\n写进日志：")
    print("  1. 代码判 temp 与 LLM 判 temp 有无差异")
    print("  2. 现在 LLM 只剩 3 维：weather / umbrella / logic，还有哪维能拿掉")
    print("  3. 提示：umbrella 可以用「关键词+否定」做快筛，但「遮阳伞」是个坑，想想怎么处理")
    print("-" * 76)


if __name__ == "__main__":
    main()
