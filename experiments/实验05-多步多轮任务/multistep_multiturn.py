#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实验 05 · 多步多轮任务 —— 一次实验补两个 W1 缺口

缺口①  H3：pass^k 是否比 pass@1 更敏感（实验03 pass@1=pass^3=pass^5=100%，任务太简单没测出来）
缺口②  B10：多轮状态丢失（清单 B 组唯一没实测的核心项）

设计要点
  1) 任务多步：读 3 个文件才能拿全 6 城市数据 → 漏读必错（F1 可检测）
  2) 事实性内容会出错：筛选 rain=false + 按 temp 降序，两个环节可独立失败
  3) 判定 100% 确定性：无 judge、无 LLM 评分，零方差
  4) B10 靠「轮2 不传 tools」逼 agent 依赖上下文 —— 传了工具它会重新读一遍，测不出丢失

用法
    python3 multistep_multiturn.py --tag pilot  --n 10           # 难度校准
    python3 multistep_multiturn.py --tag formal --n 30 --temp 0.7 # 正式跑
    python3 multistep_multiturn.py --tag formal --n 30 --append   # 追加样本（不覆盖）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BARE = HERE.parents[1] / "bare-agent"
sys.path.insert(0, str(BARE))

from openai import OpenAI  # noqa: E402
from bare_agent import Trace  # noqa: E402  （复用轨迹结构，对齐 pi 会话树）

MODEL = os.environ.get("BARE_AGENT_MODEL", "nvidia/nemotron-3.5-lightning:free")
# ───────────────────────── 任务与判定基准（从数据现场算出） ─────────────────────────
# 正解一律由 load_truth() 从数据文件计算，不硬编码 —— 既避免手算出错，
# 换 --data-dir（data=六城市简单版 / data_v2=九城市加码版）也能自动适配。
TRUTH = {"files": [], "expected": [], "avg": 0.0,
         "wet": set(), "dry": set(), "top_city": "", "top_temp": ""}


def load_truth(data_dir) -> dict:
    files = sorted(Path(data_dir).glob("cities_*.json"))
    rows = []
    for p in files:
        rows += json.loads(p.read_text(encoding="utf-8"))
    dry_rows = [r for r in rows if not r["rain"]]
    dry_rows.sort(key=lambda r: -r["temp"])
    return {
        "files": files,
        "expected": [r["city"] for r in dry_rows],
        "avg": round(sum(r["temp"] for r in dry_rows) / len(dry_rows), 1),
        "wet": {r["city"] for r in rows if r["rain"]},
        "dry": {r["city"] for r in dry_rows},
        "top_city": dry_rows[0]["city"],          # 最显眼：最热的不下雨城市
        "top_temp": str(dry_rows[0]["temp"]),
        # 最不起眼：排在正解末位的城市 —— 严苛多轮里追问这个，才测得出真丢失
        "probe_city": dry_rows[-1]["city"],
        "probe_temp": str(dry_rows[-1]["temp"]),
    }

# ★ 实测发现的坑（pilot #000）：模型把城市名输出成英文/拼音
#   （"Xi'an","Beijing","Chengdu"），顺序完全正确却因字面不匹配被判 FAIL。
#   这正是实验03「字面一致率 30% vs 语义 100%」的再现 —— 评测必须语义判定，
#   否则测的是「表述格式」而不是「推理正确性」。故判定前先归一到中文。
ALIASES = {
    "北京": ["北京", "beijing", "peking"],
    "上海": ["上海", "shanghai"],
    "深圳": ["深圳", "shenzhen"],
    "成都": ["成都", "chengdu"],
    "西安": ["西安", "xi'an", "xi’an", "xian"],
    "杭州": ["杭州", "hangzhou"],
    "广州": ["广州", "guangzhou"],
    "武汉": ["武汉", "wuhan"],
    "南京": ["南京", "nanjing"],
    "重庆": ["重庆", "chongqing"],
    "天津": ["天津", "tianjin"],
    "苏州": ["苏州", "suzhou"],
    "青岛": ["青岛", "qingdao"],
    "郑州": ["郑州", "zhengzhou"],
    "长沙": ["长沙", "changsha"],
}
CANON = {a.lower(): c for c, alts in ALIASES.items() for a in alts}
# 长别名优先，避免 "xian" 抢先匹配到 "Xi'an" 里的片段
_ALT_RE = re.compile("|".join(
    re.escape(a.lower()) for alts in ALIASES.values()
    for a in sorted(alts, key=len, reverse=True)), re.I)


def build_prompt(with_avg: bool = False) -> str:
    """轮1 任务：读 N 文件 → 筛 rain=false → 按 temp 降序（v2 加码再算平均气温）。"""
    files = TRUTH["files"]
    lst = "\n".join(f"{i}. {p}" for i, p in enumerate(files, 1))
    base = (
        f"请读取以下 {len(files)} 个文件，每个文件是若干城市的天气数据"
        "（JSON 数组，字段：city 城市名 / weather 天气 / temp 气温 / rain 是否下雨）：\n"
        f"{lst}\n\n"
        "请找出所有 rain 为 false（不下雨）的城市，按 temp（气温）从高到低排序。\n"
    )
    if with_avg:
        return base + (
            "并计算这些城市的平均气温（保留 1 位小数）。\n"
            "只输出如下 JSON，不要任何解释或多余文字：\n"
            '{"sorted": ["城市A","城市B"], "avg": 12.3}'
        )
    return base + (
        "只输出一个 JSON 数组，城市名请用文件中的原始写法（中文），"
        "不要任何解释或多余文字，例如：[\"城市A\",\"城市B\"]"
    )


def build_rounds(with_avg: bool, multi_turn: bool):
    """生成轮次 [(user_msg, use_tools, expect_temp|None), ...]

    普通模式：任务 → 紧邻追问最显眼的城市（西安，最热）。
    严苛模式：任务 → 两段无关干扰（消耗上下文）→ 追问最不起眼的城市（正解末位）。

    为什么必须插干扰轮：紧跟在任务后追问，答案还在注意力窗口里，几乎不会丢；
    中间塞两段无关生成，才能逼出真正的长程状态丢失。
    """
    rounds = [(build_prompt(with_avg), True, None)]
    if not multi_turn:
        rounds.append((f"刚才你查到的{TRUTH['top_city']}气温是多少摄氏度？"
                       "只回答一个数字，不要解释。", False, TRUTH["top_temp"]))
        return rounds
    rounds.append(("请用一句话解释什么是 RESTful API。", False, None))
    rounds.append(("写一首关于夏天的四行短诗。", False, None))
    rounds.append((f"回到最开始读的那批天气数据：其中{TRUTH['probe_city']}的气温是多少摄氏度？"
                   "只回答一个数字，不要解释。", False, TRUTH["probe_temp"]))
    return rounds


# ───────────────────────── 工具（复用 bare_agent 的确定性实现） ─────────────────────────
def read_file(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return json.dumps({"error": f"file not found: {path}"}, ensure_ascii=False)
    return p.read_text(encoding="utf-8", errors="replace")[:4000]


TOOLS = [{"type": "function", "function": {
    "name": "read_file",
    "description": "读取本地文本文件内容（自动截断到 4000 字符）",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string", "description": "文件绝对路径"}},
                   "required": ["path"]}}}]
REGISTRY = {"read_file": read_file}


def load_key() -> str:
    """优先级：env > ~/.openrouter_key > ~/.zshrc。

    单独支持 ~/.openrouter_key：shell rc 文件常被敏感文件权限保护、自动化长跑读不到；
    单独存一份在 home 下且不在任何 git 仓库内，既可被脚本读取，也不会被误提交。
    """
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    for p in (Path.home() / ".openrouter_key", Path.home() / ".zshrc"):
        if not p.exists():
            continue
        try:
            t = p.read_text(errors="replace")
        except PermissionError:
            continue                      # 被敏感文件保护拦下，换下一个来源
        m = re.search(r'(?:export\s+)?OPENROUTER_API_KEY\s*=\s*["\']?([^"\'\n]+)', t)
        if m:
            return m.group(1).strip()
    raise SystemExit(
        "找不到 OPENROUTER_API_KEY。任选其一：\n"
        "  1) export OPENROUTER_API_KEY=...\n"
        "  2) 写入 ~/.openrouter_key（不含 export 前缀）")


# ───────────────────────── 多轮 agent 循环（轮2 可断工具） ─────────────────────────
def chat_with_retry(client, kw, tries=3):
    """OpenRouter 免费模型偶发返回非 JSON（pilot 实测 2/10 报 JSONDecodeError）。

    不重试的话错误率会吃掉样本量，还会让 pass^k 的分组成组报废。
    """
    last = None
    for i in range(tries):
        try:
            return client.chat.completions.create(**kw)
        except Exception as e:
            last = e
            time.sleep(1.5 ** i)          # 指数退避
    raise last


def run_turn(client, model, messages, tools, registry, max_turns, trace):
    """跑一轮（可能内含多次 tool 往返）。返回 (answer, stats, messages)。

    tools 传 None/[] 即断掉工具 —— B10 的关键：不让它重新读文件。
    """
    stats = {"llm_calls": 0, "tool_calls": 0, "tool_errors": 0,
             "files_read": [], "arg_parse_errors": 0}
    msgs = list(messages)
    use_tools = tools if tools else None

    for turn in range(max_turns):
        kw = {"model": model, "messages": msgs}
        if use_tools:
            kw["tools"] = use_tools
            kw["tool_choice"] = "auto"
        resp = chat_with_retry(client, kw)
        stats["llm_calls"] += 1
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        trace.assistant(msg.content or "", calls, resp.choices[0].finish_reason, None)

        if not calls:
            return msg.content or "", stats, msgs

        msgs.append({"role": "assistant", "content": msg.content or "",
                     "tool_calls": [{"id": tc.id, "type": "function",
                                     "function": {"name": tc.function.name,
                                                  "arguments": tc.function.arguments}}
                                    for tc in calls]})
        for tc in calls:
            stats["tool_calls"] += 1
            name, raw = tc.function.name, tc.function.arguments
            try:
                args = json.loads(raw) if raw else {}
                fn = REGISTRY.get(name) if registry else None
                out = fn(**args) if fn else json.dumps(
                    {"error": f"Tool not found: {name}"}, ensure_ascii=False)
                is_error = fn is None
                if name == "read_file" and not is_error:
                    stats["files_read"].append(args.get("path", ""))
            except Exception as e:
                out = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                is_error = True
            if is_error:
                stats["tool_errors"] += 1
            trace.tool_result(tc, out, is_error)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})

    return "<max_turns reached>", stats, msgs


# ───────────────────────── 确定性判定（零 judge 方差） ─────────────────────────
def parse_cities(answer: str):
    """提取城市序列（英文/拼音先归一到中文）。优先 JSON 数组，回落别名正则扫描。"""
    m = re.search(r"\[[^\]]*\]", answer)
    if m:
        try:
            arr = json.loads(m.group(0))
            # 归一："Xi'an" -> "西安"，避免语义正确却被字面判错
            seq = [CANON.get(str(c).strip().lower(), str(c).strip())
                   for c in arr if isinstance(c, str)]
            if seq:
                return seq, "json"
        except Exception:
            pass
    seq = []
    for mt in _ALT_RE.finditer(answer):
        c = CANON.get(mt.group(0).lower())
        if c and (not seq or seq[-1] != c):      # 连续重复去重
            seq.append(c)
    if seq:
        return seq, "scan"
    return [], "fail"


def judge_round1(answer: str, stats: dict, with_avg: bool = False):
    """返回 (passed, primary_fail, all_fails, seq, method, avg_got)。"""
    seq, method = parse_cities(answer)
    files_read = {p for p in stats["files_read"] if p}
    wet, dry, expected = TRUTH["wet"], TRUTH["dry"], TRUTH["expected"]
    fails = []

    if stats["tool_calls"] == 0:
        fails.append("F5")                                   # 不调工具直接编
    if len(files_read) < len(TRUTH["files"]):
        fails.append("F1")                                   # 漏读文件
    if method == "fail":
        fails.append("F6")                                   # 格式/解析失败
    if any(c in wet for c in seq):
        fails.append("F2")                                   # 筛选错：混进下雨城市
    s = set(seq) & dry
    if s and s < dry and not any(c in wet for c in seq):
        fails.append("F4")                                   # 缺项
    if set(seq) == dry and seq != expected:
        fails.append("F3")                                   # 排序错

    # 算术环节（v2 加码）：从答案抓 avg 数值与真值比对，容差 0.05 防浮点噪声
    avg_ok, avg_got = True, None
    if with_avg:
        m = re.search(r'"avg"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', answer)
        avg_got = float(m.group(1)) if m else None
        if avg_got is None or abs(avg_got - TRUTH["avg"]) > 0.05:
            fails.append("F7")                               # 平均气温算错
            avg_ok = False

    # 读全所有文件 + 序列正确 + 算术正确，三者齐备才算过
    passed = (seq == expected) and len(files_read) >= len(TRUTH["files"]) and avg_ok
    if passed:
        fails = []
    return passed, (fails[0] if fails else None), fails, seq, method, avg_got


def judge_round2(answer: str, expect: str):
    """B10：撤掉工具后追问某城市气温 → 答对说明上下文还记着。"""
    nums = re.findall(r"\d+", answer)
    return expect in nums, nums


# ───────────────────────── 单次 run（轮1 + 轮2） ─────────────────────────
_TRACES_READY = False


def one_run(client, model, temp, idx, save_traces, with_avg=False, multi_turn=False):
    trace = Trace(uuid.uuid4().hex[:8], model)
    rec = {"run_id": idx, "model": model, "temp": temp, "error": None,
           "multi_turn": multi_turn}
    try:
        msgs = []
        for i, (msg, use_tools, expect) in enumerate(build_rounds(with_avg, multi_turn)):
            trace.user(msg)
            msgs.append({"role": "user", "content": msg})
            ans, st, msgs = run_turn(
                client, model, msgs,
                TOOLS if use_tools else None,      # 断工具 = 逼它只能依赖上下文
                REGISTRY if use_tools else None,
                8 if use_tools else 2, trace)
            if i == 0:                             # 轮1：多步任务 → 喂 H3
                ok1, f1, allf, seq, method, avg_got = judge_round1(ans, st, with_avg)
                rec["round1"] = {"answer": ans, "passed": ok1, "primary_fail": f1,
                                 "fails": allf, "seq": seq, "parse_method": method,
                                 "avg_got": avg_got,
                                 "avg_truth": TRUTH["avg"] if with_avg else None,
                                 "files_read": sorted({p for p in st["files_read"] if p}),
                                 "tool_calls": st["tool_calls"],
                                 "llm_calls": st["llm_calls"]}
            elif expect is not None:               # 末轮：撤工具追问 → 喂 B10
                ok2, nums = judge_round2(ans, expect)
                rec["round2"] = {"answer": ans, "passed": ok2, "nums": nums,
                                 "expect": expect, "tool_calls": st["tool_calls"]}
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"

    if save_traces:
        global _TRACES_READY
        td = HERE / "traces"
        if not _TRACES_READY:
            # 坑：shim 对「已存在的目录」也会抛 EEXIST（exist_ok 无效）→ 只建一次并吞掉
            try:
                td.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                pass
            _TRACES_READY = True
        try:
            rec["trace_path"] = str(trace.save(td / f"run{idx:03d}.jsonl"))
        except Exception as e:                 # 轨迹存不下不能拖垮主实验
            rec["trace_error"] = f"{type(e).__name__}: {e}"
    return rec


# ───────────────────────── pass^k ─────────────────────────
def pass_at_k(oks, k):
    """把 N 次分成 N//k 组，每组 k 次全过才算过。"""
    groups = len(oks) // k
    if groups == 0:
        return None
    return sum(1 for g in range(groups) if all(oks[g * k:(g + 1) * k])) / groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run", help="raw_文件后缀，防覆盖（pilot/formal）")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--append", action="store_true", help="追加到已有 raw_<tag>.json")
    ap.add_argument("--data-dir", default=str(HERE / "data"),
                    help="数据目录：data=六城市简单版 / data_v2=九城市加码版")
    ap.add_argument("--with-avg", action="store_true",
                    help="加码：额外要求算平均气温（配合 data_v2 / data_v3）")
    ap.add_argument("--multi-turn", action="store_true",
                    help="严苛多轮 B10：任务后插两段无关干扰，再追问最不起眼城市")
    ap.add_argument("--mt-first", type=int, default=0,
                    help="只对前 K 条跑完整多轮（B10 抽样），其余只跑轮1；0=全部多轮。"
                         "省时：多轮的 3 个额外轮次对 H3 无用，B10 样本够用即可")
    ap.add_argument("--sleep", type=float, default=0.5, help="run 间隔，防限流")
    a = ap.parse_args()

    TRUTH.update(load_truth(a.data_dir))          # 正解现场算出，先打印出来核对
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=load_key())
    out = HERE / f"raw_{a.tag}.json"

    results = []
    if a.append and out.exists():
        results = json.loads(out.read_text(encoding="utf-8"))

    print(f"[exp05] tag={a.tag} n={a.n} temp={a.temp} model={a.model} "
          f"data={Path(a.data_dir).name} with_avg={a.with_avg} "
          f"multi_turn={a.multi_turn}", flush=True)
    print(f"[exp05] 正解={TRUTH['expected']} avg={TRUTH['avg']} "
          f"files={len(TRUTH['files'])} probe={TRUTH['probe_city']}"
          f"{TRUTH['probe_temp']}℃", flush=True)
    t0 = time.time()
    for i in range(len(results), len(results) + a.n):
        use_mt = a.multi_turn and (a.mt_first == 0 or i < a.mt_first)
        r = one_run(client, a.model, a.temp, i, True, a.with_avg, use_mt)
        results.append(r)
        s1 = r.get("round1", {})
        s2 = r.get("round2", {})
        print(f"  #{i:03d} r1={'PASS' if s1.get('passed') else 'FAIL'}"
              f"({s1.get('primary_fail') or '-'}) seq={s1.get('seq')}"
              f" files={len(s1.get('files_read', []))}"
              + (f" avg={s1.get('avg_got')}/{TRUTH['avg']}" if a.with_avg else "")
              + f" | r2={'PASS' if s2.get('passed') else 'FAIL'} {s2.get('nums')}"
              f" {r.get('error') or ''}", flush=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(a.sleep)

    # 汇总：轮1/轮2 分开统计 —— 中途 API 报错的样本，已成功的那一轮仍要计入，
    # 否则白白丢数据（pilot 里 #006 轮1 明明 PASS 却被整条丢弃）
    done1 = [r for r in results if "round1" in r]
    done2 = [r for r in results if "round2" in r]
    oks1 = [r["round1"]["passed"] for r in done1]
    oks2 = [r["round2"]["passed"] for r in done2]
    n = len(oks1)
    summary = {
        "tag": a.tag, "model": a.model, "temp": a.temp, "N": n,
        "data_dir": Path(a.data_dir).name, "with_avg": a.with_avg,
        "expected": TRUTH["expected"], "avg_truth": TRUTH["avg"] if a.with_avg else None,
        "errors": sum(1 for r in results if r.get("error")),
        "N_round1": len(oks1), "N_round2": len(oks2),
        "elapsed_sec": round(time.time() - t0, 1),
        "pass@1": round(sum(oks1) / n, 4) if n else None,
        "pass^3": round(pass_at_k(oks1, 3), 4) if n >= 3 else None,
        "pass^5": round(pass_at_k(oks1, 5), 4) if n >= 5 else None,
        "B10_状态保持率": round(sum(oks2) / len(oks2), 4) if oks2 else None,
        "fail_modes": {},
        "files_read_dist": {},
        "avg_got_dist": {},
    }
    for r in done1:
        f = r.get("round1", {}).get("primary_fail")
        if f:
            summary["fail_modes"][f] = summary["fail_modes"].get(f, 0) + 1
        k = len(r.get("round1", {}).get("files_read", []))
        summary["files_read_dist"][k] = summary["files_read_dist"].get(k, 0) + 1
        if a.with_avg:
            g = str(r.get("round1", {}).get("avg_got"))
            summary["avg_got_dist"][g] = summary["avg_got_dist"].get(g, 0) + 1

    (HERE / f"summary_{a.tag}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[SUMMARY]", json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
