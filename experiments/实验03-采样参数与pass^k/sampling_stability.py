#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 11 · 采样参数 ↔ 输出稳定性实验（pass^k 实测）

对应主文档 W1 · D5「非确定性测试范式」，同时兑现 W1 验收线②。

================================================================
三条待验证假设（跑完逐条对照，证伪也算成功）
================================================================
H1  温度↑ → 输出多样性↑      （unique 数↑ / 字面一致率↓）
H2  温度↑ → 单次通过率↓      （pass@1 下降，事实错误变多）
H3  pass^k 比 pass@1 对温度敏感得多
    —— 即使 pass@1 只小幅下降，pass^k 会急剧下降。
    这条如果成立，就是「Agent 评测更该用 pass^k」的实测证据，
    而不是课本上抄来的结论。

================================================================
★ 为什么通过判定不用 LLM judge（本实验最关键的设计）
================================================================
我们要测的是「被测模型的方差」。
若用 LLM judge 判通过，judge 自身的方差会混进测量结果 ——
测出来的就不再是干净的模型方差，而是「模型方差 + 裁判方差」。
→ 所以通过与否全部交给确定性代码：零方差，
  测到的任何波动只可能来自被测模型。
（这也正是 W1 分层铁律的一次实战应用：能用代码判的绝不用 LLM 判）

================================================================
变量控制
================================================================
每组 system / user / 工具返回数据完全相同，唯一变量 = temperature。
刻意隔离工具调用方差（A1 工具调用遗漏）——
本实验只看「拿到数据之后怎么答」这一层的稳定性。

跑法（在本文件所在目录下执行）：
    export OPENROUTER_API_KEY="sk-or-..."
    cd <repo>/experiments/实验03-采样参数与pass^k

    # 全量：4 个温度 × 10 次（约 40 次调用，免费模型偶发挂起，建议后台跑）
    .venv/bin/python3 sampling_stability.py

    # 单温度（推荐逐个跑，一次跑完可能被超时杀掉 —— 实测踩过 exit 137）
    ... sampling_stability.py --temp 0 --n 10
    ... sampling_stability.py --temp 1.0 --n 10

    # 只看汇总（不调用 API，用同目录已有 raw.json 重算）
    ... sampling_stability.py --report
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

# 本文件位于 experiments/实验03-采样参数与pass^k/ 下，需向上三级才能到 agent-eval-kit/
# （evalkit 包所在处）—— 2026-09-06 实验目录重组后由两级改为三级
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from evalkit.detectors import check_temp          # 复用确定性温度判定

from openai import OpenAI

# ============================================================
# 实验配置
# ============================================================
MODEL = "nvidia/nemotron-3.5-lightning:free"
TEMPERATURES = [0.0, 0.3, 0.7, 1.0]
N_PER_TEMP = 10
KS = (1, 3, 5)

GROUND_TRUTH = {"weather": "晴", "temp": 32, "rain": False}
QUESTION = "北京今天天气怎么样？需要带伞吗？"

SYSTEM_PROMPT = """你是一个天气助手。
你会拿到工具返回的真实数据，必须严格基于这些数据回答，不能编造工具没返回的信息。
回答简洁，必须给出三项：天气状况、气温、以及是否需要带伞的明确结论。"""

# 原始数据与本脚本同目录（实验自包含）—— 2026-09-06 重组后调整
RAW_PATH = Path(__file__).resolve().parent / "raw.json"


# ============================================================
# 确定性判定层（零方差）
# ============================================================
# ⚠ 2026-09-05 修订：dry-run 仅用 3 条样本就暴露出两个判定 bug，已修。
#   bug1 umbrella：原正则假设「否定词在『伞』之前」（不需要带伞），
#        但模型实际输出是「是否需要带伞：不需要」—— 否定词在伞【后面】，
#        原正则完全匹配不到 → 3/3 全被误判成「没给结论」。
#        → 改为定位「伞」，在其前后各 20 字的上下文窗口内找否定词，与语序无关。
#   bug2 temp：原判据强制温度必须带单位（°C/℃/度），
#        导致「气温：32」这种没写单位的正确回答被误杀 2/3。
#        → 改为两级：先严格（带单位）；一个带单位的都没提取到时，
#          再退回「气温/温度 + 数字」关键词模式。
#        仍然不认裸数字，所以「风力10级」依旧不会被误当 10 度
#        （那是 07 脚本踩过的坑，不能因为修这个 bug 又踩回去）。

NEG_WORDS = ["不需要", "不用", "不必", "无需", "可以不", "没必要",
             "不必带", "无需带", "不用带", "不需要带"]

# 伞的上下文窗口：前后各 20 个非句末字符。覆盖两种语序：
#   「不需要带伞」（否定在前）  与  「是否需要带伞：不需要」（否定在后）
UMBRELLA_CTX_RE = re.compile(r"([^。！？\n]{0,20})(遮阳伞|太阳伞|伞)([^。！？\n]{0,20})")

# 回退用：明确写了「气温/温度」关键词但没带单位时（如「气温：32」）
TEMP_KEYWORD_RE = re.compile(r"(?:气温|温度)\s*[:：是为约]?\s*(-?\d+(?:\.\d+)?)")


def check_weather(answer: str, gt_weather: str = "晴") -> bool:
    """天气判对没：GT 是「晴」，回答里必须明确出现「晴」。"""
    return gt_weather in answer


def check_umbrella(answer: str) -> bool:
    """
    带伞结论判对没：GT 不下雨 → 必须明确给出「不需要带伞」类结论。
    「可以带遮阳伞」不算对 —— 问的是防雨，答的是防晒（A7）。
    """
    for m in UMBRELLA_CTX_RE.finditer(answer):
        if m.group(2) in ("遮阳伞", "太阳伞"):
            continue                      # 说的是「带遮阳伞」，不是「不带雨伞」
        ctx = m.group(1) + m.group(2) + m.group(3)
        if any(neg in ctx for neg in NEG_WORDS):
            return True
    return False


def check_temp_local(answer: str, gt_temp: float, tol: float = 1.0):
    """
    两级温度判定：先严格（必须带单位），严格模式一个都没提到时再退回关键词模式。
    返回 (score, values, diagnosis)
    """
    score, vals, diag = check_temp(answer, gt_temp, tol)
    if score == 1:
        return 1, vals, diag
    if vals:
        # 带单位的温度提到了，但超出允许区间 —— 确实是错，不回退
        return 0, vals, diag
    kw_vals = [float(m.group(1)) for m in TEMP_KEYWORD_RE.finditer(answer)]
    if not kw_vals:
        return 0, [], "未提及气温"
    bad = [v for v in kw_vals if abs(v - gt_temp) > tol]
    if bad:
        shown = [int(v) if float(v).is_integer() else v for v in bad]
        lo, hi = int(gt_temp - tol), int(gt_temp + tol)
        return 0, kw_vals, f"气温 {shown} 超出允许区间 [{lo}, {hi}]"
    return 1, kw_vals, ""


def judge_by_code(answer: str):
    """返回 (weather_ok, temp_ok, umbrella_ok, passed, diag)"""
    w = check_weather(answer)
    t_score, vals, diag = check_temp_local(answer, GROUND_TRUTH["temp"], tol=1.0)
    t = (t_score == 1)
    u = check_umbrella(answer)
    return w, t, u, (w and t and u), ("" if t else diag)


# ============================================================
# 采样
# ============================================================
def generate(client, temperature: float, n: int):
    """同一输入重复采样 n 次。调用失败不计入样本（故障 ≠ 不合格，C1）。"""
    outs, fails = [], 0
    for i in range(n):
        got = False
        for attempt in range(3):
            try:
                r = client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content":
                            f"{QUESTION}\n\n【工具返回的真实数据】\n"
                            f"{json.dumps(GROUND_TRUTH, ensure_ascii=False)}"},
                    ],
                    temperature=temperature,
                    timeout=90,
                )
                outs.append(r.choices[0].message.content or "")
                got = True
                break
            except Exception as e:
                if attempt == 2:
                    fails += 1
                    print(f"    ⚠ 第 {i+1} 次调用失败（{type(e).__name__}）→ 不计入样本")
                else:
                    time.sleep(2 * (attempt + 1))
        if not got:
            continue
        time.sleep(0.4)
        sys.stdout.flush()
    return outs, fails


# ============================================================
# 统计
# ============================================================
def empirical_pass_k(passed_list, k):
    """
    经验 pass^k：把 N 次结果按顺序切成若干组「连续 k 次」的试验，
    统计「k 次全部通过」的试验占比。

    不用 bootstrap 有放回抽样 —— 那会破坏「连续独立重复运行」的语义，
    而且 bootstrap 抽出来的全通过概率恒等于 p̂^k，等于什么也没多测。
    """
    groups = [passed_list[i:i + k] for i in range(0, len(passed_list) - k + 1, k)]
    if not groups:
        return None, 0
    ok = sum(1 for g in groups if all(g))
    return ok / len(groups), len(groups)


def summarize(answers):
    """对某个温度下的 n 条回答做统计"""
    n = len(answers)
    uniq = len(set(answers))
    mode_n = Counter(answers).most_common(1)[0][1] if answers else 0
    literal_rate = mode_n / n if n else 0

    judged = [judge_by_code(a) for a in answers]
    passed = [j[3] for j in judged]
    p1 = sum(passed) / n if n else 0

    pk = {}
    for k in KS:
        emp, gcnt = empirical_pass_k(passed, k)
        pk[k] = {"emp": emp, "groups": gcnt, "theo": p1 ** k}

    fails = {
        "weather": sum(1 for j in judged if not j[0]),
        "temp": sum(1 for j in judged if not j[1]),
        "umbrella": sum(1 for j in judged if not j[2]),
    }
    return {
        "n": n, "unique": uniq, "literal_rate": literal_rate,
        "pass_at_1": p1, "pass_k": pk, "fail_breakdown": fails,
        "passed": passed,
    }


def print_report(all_res):
    print("\n" + "=" * 88)
    print("汇总 · 采样参数 ↔ 输出稳定性（判定全部由确定性代码完成，零 judge 方差）")
    print("=" * 88)
    hdr = (f"  {'temp':<6} {'N':<4} {'unique':<7} {'字面一致率':<11} "
           f"{'pass@1':<8} {'pass^3':<16} {'pass^5':<16}")
    print(hdr)
    print("  " + "-" * 84)
    for t in TEMPERATURES:
        key = str(t)
        if key not in all_res or not all_res[key].get("answers"):
            print(f"  {key:<6} {'(未跑)':<4}")
            continue
        s = summarize(all_res[key]["answers"])
        p3, p5 = s["pass_k"][3], s["pass_k"][5]
        p3s = f"{p3['emp']:.0%}({p3['groups']}组)" if p3["emp"] is not None else "—"
        p5s = f"{p5['emp']:.0%}({p5['groups']}组)" if p5["emp"] is not None else "—"
        print(f"  {key:<6} {s['n']:<4} {s['unique']:<7} {s['literal_rate']:<11.0%} "
              f"{s['pass_at_1']:<8.0%} {p3s:<16} {p5s:<16}")

    print("\n  pass^k 对照（经验值 vs 理论值 p^k）：")
    print(f"  {'temp':<8} {'k':<4} {'经验':<14} {'理论 p^k':<12} 差异")
    print("  " + "-" * 60)
    for t in TEMPERATURES:
        key = str(t)
        if key not in all_res or not all_res[key].get("answers"):
            continue
        s = summarize(all_res[key]["answers"])
        for k in KS:
            pk = s["pass_k"][k]
            if pk["emp"] is None:
                continue
            diff = pk["emp"] - pk["theo"]
            print(f"  {key:<8} {k:<4} {pk['emp']:<14.0%} {pk['theo']:<12.0%} {diff:+.0%}")

    print("\n  失败归因（各维度不通过的次数）：")
    print(f"  {'temp':<8} {'weather':<9} {'temp':<9} {'umbrella':<9}")
    print("  " + "-" * 40)
    for t in TEMPERATURES:
        key = str(t)
        if key not in all_res or not all_res[key].get("answers"):
            continue
        s = summarize(all_res[key]["answers"])
        fb = s["fail_breakdown"]
        print(f"  {key:<8} {fb['weather']:<9} {fb['temp']:<9} {fb['umbrella']:<9}")

    print("\n" + "=" * 88)
    print("假设对照（跑完自己填，证伪也是结论）")
    print("=" * 88)
    print("  H1 温度↑ → unique↑ / 字面一致率↓        → 看上面前两列")
    print("  H2 温度↑ → pass@1↓                      → 看 pass@1 列")
    print("  H3 pass^k 比 pass@1 对温度更敏感        → 比较 pass@1 与 pass^5 的下降幅度")
    print("\n  ⚠ 统计诚实声明：每组仅 10 次采样，pass^5 只有 2 组试验，")
    print("     置信区间极宽。本实验用于「看到趋势」，不足以给出精确的数值结论。")


# ============================================================
# main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--temp", type=float, action="append",
                    help="只跑指定温度，可多次传入")
    ap.add_argument("--n", type=int, default=N_PER_TEMP, help="每个温度采样次数")
    ap.add_argument("--report", action="store_true", help="只汇总已有结果，不调 API")
    args = ap.parse_args()

    temps = args.temp if args.temp else TEMPERATURES

    # 读已有结果
    all_res = {}
    if RAW_PATH.exists():
        all_res = json.loads(RAW_PATH.read_text(encoding="utf-8"))

    if args.report:
        print_report(all_res)
        return

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("✗ 没读到 OPENROUTER_API_KEY")
        print('  先跑：export OPENROUTER_API_KEY="sk-or-..."')
        sys.exit(1)

    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])

    print("=" * 88)
    print(f"采样参数 ↔ 输出稳定性实验   模型 {MODEL}")
    print(f"温度 {temps}   每组 {args.n} 次   通过判定：确定性代码（零 judge 方差）")
    print("=" * 88)

    for t in temps:
        print(f"\n【temperature = {t}】采样 {args.n} 次 …")
        answers, fails = generate(client, t, args.n)
        key = str(t)
        prev = all_res.get(key, {}).get("answers", [])
        # 同温度重复跑 = 累加样本（不是覆盖），便于分次跑避开超时
        merged = prev + answers
        all_res[key] = {"answers": merged, "fails": all_res.get(key, {}).get("fails", 0) + fails}
        print(f"  → 新增 {len(answers)} 条（调用失败 {fails} 次），该温度累计 {len(merged)} 条")

        # 实时打一条样例，方便肉眼感受温度带来的差异
        if answers:
            print(f"  样例：{answers[0][:70].replace(chr(10), ' ')}…")

        RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_PATH.write_text(json.dumps(all_res, ensure_ascii=False, indent=2), encoding="utf-8")

    print_report(all_res)
    print(f"\n原始结果已存：{RAW_PATH}")


if __name__ == "__main__":
    main()
