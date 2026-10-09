#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 2 · 写一条不会因为换个说法就误杀的断言

背景：
    STEP 1 里你跑出的 5 条回答，语义全部正确，字面全不一样。
    用 assert "不需要带伞" in answer 去卡，只有 3 条能过，2 条正确答案被误杀（40% 假阳性）。

这一脚本做的事：
    方法 1：字面断言（对照组）
    方法 2：LLM-as-a-Judge —— 换一个厂商的模型当裁判，按 rubric 逐条判分
    方法 2 跑 3 轮：看「裁判自己稳不稳」

为什么裁判要用不同厂商的模型：
    同一个模型评自己，会有自我偏好偏差（偏好自己的表达习惯）。这是评测的大忌。
    所以被测用 nvidia，裁判用 z-ai。

跑法：
    export OPENROUTER_API_KEY="sk-or-..."
    .venv/bin/python3 02_judge_baseline.py
"""
import json
import os
import re
import sys
import time
from collections import Counter
from openai import OpenAI

# ---------- 裁判模型 ----------
# 原则：裁判必须和被测不同厂商，否则会有自我偏好偏差（模型偏爱自己的表达习惯）。
# 被测是 nvidia，裁判就避开 nvidia。
# 免费模型限流是常态，所以给一条 fallback 链 + 重试退避。
# （实测 2026-09-04：glm-5.2:free 429 限流；inkling-small 403；lfm/north-mini 返回空；
#   只有 minimax-m3:free 稳定且能正确输出 JSON）
JUDGE_MODELS = [
    "minimax/minimax-m3:free",
    "cohere/north-mini-code:free",
    "inclusionai/ling-3.0-flash-fin:free",
]
JUDGE_ROUNDS = 3
MAX_RETRY = 3

# ---------- STEP 1 你跑出来的 5 条回答，原样搬过来 ----------
ANSWERS = [
    "今天北京天气晴朗，气温约 **32°C**，**没有降雨**。\n**建议**：不需要带伞，出门可以穿轻薄透气的衣物，注意防晒和补水，避免在强烈的阳光下长时间暴晒。祝你今天旅途愉快！",
    "北京今天天气**晴**，气温约 **32 °C**，**不下雨**。\n**建议**：\n- 因为天气晴朗且气温较高，外出请注意防晒（戴帽子、涂抹防晒霜、佩戴太阳镜等）。\n- 由于没有降雨，**不需要带伞**。\n祝您今天玩得愉快！如果还有其他天气相关的问题，随时问我。",
    "今天北京天气**晴**，气温约 **32°C**，没有降雨。\n**结论：不需要带雨伞**。如果您担心晒太阳，可以带帽子或太阳镜，但不用专门为了防雨准备雨伞。祝您出行愉快！",
    "北京今天天气**晴**，气温约**32°C**，目前**没有降雨预报**，**不需要带伞**。不过白天阳光较强，出门记得做好防晒哦！",
    "北京今天天气**晴**，气温**32℃**，**没有降雨**。\n☀️ 建议您外出可以不带伞，但记得注意防晒和补水，避免在强阳光下长时间曝晒。祝您今天玩得开心！",
]

# ---------- 真实工具返回的数据（判分的唯一依据） ----------
GROUND_TRUTH = {"weather": "晴", "temp": 32, "rain": False}
QUESTION = "北京今天天气怎么样？需要带伞吗？"

# ---------- Rubric：三条「可判定」的硬标准 ----------
# 好坏 rubric 的分界线：换一个人照着这条判，能不能得出同样的分数？
#   ✗ 差的："回答得好吗？1-5 分"   → 主观，两个人的分一定不一样
#   ✓ 好的：三条 0/1 硬标准        → 可判定，这是后面算 IAA 的基础
RUBRIC = """你是严格的评测裁判。以下是用户问题、工具返回的真实数据、以及 Agent 的回答。

【用户问题】
{question}

【工具返回的真实数据】
{ground_truth}

【Agent 的回答】
{answer}

【评分标准】—— 逐条判断，每条只给 0 或 1，不要给中间分
1. weather：回答是否明确给出了天气状况为「晴」？（只说"天气不错"不算，必须明确是晴）
2. temp：回答是否给出了气温，且数值约为 32 度？（31-33 都算对）
3. umbrella：回答是否明确给出了「不需要带伞」的结论？（"可以不带伞""不用带伞""不需要带雨伞"都算对）

严格要求：只输出一个 JSON 对象，不要任何解释文字，格式如下：
{{"weather": 0或1, "temp": 0或1, "umbrella": 0或1, "reason": "一句话说明扣分原因"}}"""


def parse_json_loose(text: str):
    """免费模型爱加 markdown 代码块和废话，这里做宽容解析"""
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
    """
    返回 (评分dict | None, 使用的模型, 错误信息 | None)

    ★ 这里有个必须守住的边界：调用失败 ≠ 判 0 分。
      限流、超时、模型返回空，都是「基础设施故障」；
      被测对象答错了才是「不合格」。
      混为一谈的话，一次限流就能让通过率从 100% 掉到 0%，结论整个翻转。
      所以失败返回 None，由调用方单独统计，不计入通过率的分母。
    """
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
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,          # 裁判要尽量稳定，温度归零
                    timeout=90,
                )
                j = parse_json_loose(r.choices[0].message.content)
                # 必须三条标准都拿到了才算一次有效评分，否则视为这次调用失败
                if isinstance(j, dict) and all(
                    isinstance(j.get(k), int) for k in ("weather", "temp", "umbrella")
                ):
                    return j, model, None
                last_err = f"{model} 返回的 JSON 不完整或无效"
            except Exception as e:
                last_err = f"{model}: {type(e).__name__}"
                if "RateLimit" in type(e).__name__ or "429" in str(e):
                    time.sleep(2 * (attempt + 1))   # 退避后重试
                    continue
                break
            time.sleep(1)
    return None, None, last_err


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )

    # ========== 方法 1：字面断言 ==========
    print("=" * 72)
    print("方法 1 · 字面断言  assert \"不需要带伞\" in answer")
    print("=" * 72)
    literal = []
    for i, a in enumerate(ANSWERS, 1):
        ok = "不需要带伞" in a
        literal.append(ok)
        flag = "✓ 通过" if ok else "✗ 被误杀（其实答对了）"
        print(f"  回答 {i}: {flag}")
    n_pass = sum(literal)
    print(f"\n  → 通过 {n_pass}/5，误杀 {5 - n_pass}/5，假阳性率 {100*(5-n_pass)//5}%")
    print("     被误杀的两条其实说的是「不需要带雨伞」和「可以不带伞」—— 都对。")

    # ========== 方法 2：LLM-as-a-Judge ==========
    print("\n" + "=" * 72)
    print(f"方法 2 · LLM-as-a-Judge   裁判: {' → '.join(JUDGE_MODELS)}（限流自动降级）")
    print(f"         rubric: 3 条 0/1 硬标准，满分 3 分，2 分及以上算通过")
    print(f"         跑 {JUDGE_ROUNDS} 轮 —— 重点看裁判自己稳不稳")
    print("=" * 72)

    rounds, used_models, failed = [], [], []
    for rnd in range(JUDGE_ROUNDS):
        print(f"\n  【第 {rnd+1}/{JUDGE_ROUNDS} 轮裁判】")
        scores = []
        for i, a in enumerate(ANSWERS, 1):
            j, model, err = judge_once(client, a)
            if j is None:
                # 调用失败：不判分，不进分母，单独记账
                scores.append(None)
                failed.append((rnd + 1, i, err))
                print(f"    回答 {i}: ⚠ 调用失败（{err}）→ 不计入通过率")
            else:
                used_models.append(model)
                w, t, u = j["weather"], j["temp"], j["umbrella"]
                total = w + t + u
                scores.append(total)
                passed = "通过" if total >= 2 else "不通过"
                extra = f"  ({j.get('reason','')})" if j.get("reason") and total < 3 else ""
                print(f"    回答 {i}: {w}/{t}/{u} = {total} 分  [{passed}]{extra}")
            sys.stdout.flush()
        rounds.append(scores)
        valid = [s for s in scores if s is not None]
        if valid:
            print(f"    本轮通过: {sum(1 for s in valid if s >= 2)}/{len(valid)}（有效评分）")

    # ---------- 裁判稳定性 ----------
    print("\n" + "=" * 72)
    print("裁判稳定性（同一个裁判，跑 3 轮，结果一样吗？）")
    print("=" * 72)
    sigs = Counter(tuple(r) for r in rounds)
    print(f"  {JUDGE_ROUNDS} 轮共出现 {len(sigs)} 种评分结果：")
    for sig, n in sigs.most_common():
        print(f"    ×{n}  {list(sig)}")

    if len(sigs) == 1:
        print("\n  ✓ 裁判这 3 轮完全一致")
    else:
        print(f"\n  ⚠ 裁判自己摇摆了 —— 同样的输入、同样的 rubric，{JUDGE_ROUNDS} 轮给出 {len(sigs)} 种结果")
        print("    这就是 W5 要解决的：judge 不是装上就能信，它需要校准和一致性检验。")

    # ---------- 对比 ----------
    print("\n" + "=" * 72)
    print("两种方法对比")
    print("=" * 72)
    per_round = [sum(1 for s in r if s is not None and s >= 2) for r in rounds]
    n_valid = [sum(1 for s in r if s is not None) for r in rounds]
    best, worst = (max(per_round), min(per_round)) if per_round else (0, 0)

    print(f"  字面断言      : {n_pass}/5 通过   （误杀 {5-n_pass} 个正确答案）")
    if any(n_valid):
        print(f"  LLM-as-Judge  : {worst}~{best}/{max(n_valid)} 通过   （{JUDGE_ROUNDS} 轮区间，分母是有效评分数）")
    else:
        print("  LLM-as-Judge  : 全部调用失败，无法评分")

    if failed:
        print(f"\n  ⚠ 有 {len(failed)} 次裁判调用失败（限流/超时/返回空），已排除、未计入分母：")
        for rnd, i, err in failed[:6]:
            print(f"    第{rnd}轮 回答{i}: {err}")
        print("  → 记住这条：故障不是不合格。真做评测平台时，这类失败要触发告警而不是静默记 0 分。")

    print("\n" + "-" * 72)
    print("你刚刚亲手走完了 Agent 评测最重要的一次升级：")
    print("  断言「字面相等」 → 判定「语义正确」")
    print("代价是：裁判自己也是个模型，也会摇摆、会限流 —— 所以才需要 W5 的校准。")
    print("-" * 72)
    print("\n写进日志：")
    print("  1. 字面断言通过几条、误杀几条")
    print("  2. Judge 三轮结果是否一致（不一致的话，差在哪条）")
    print("  3. Judge 有没有判错？比如把某条明明对的判成不通过 —— 如果有，那是最值钱的发现")
    print("  4. 有没有出现调用失败？如果有，你刚才的代码是怎么处理它的")


if __name__ == "__main__":
    main()
