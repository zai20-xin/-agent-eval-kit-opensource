#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实验 04 · Function Calling 参数错误率压力测试

要回答的问题：模型生成工具调用**参数**时到底有多不准？哪些参数类型最容易错？

设计要点（沿用本项目的两条铁律）：
1. **判定全部确定性代码**（零 judge 方差）——参数对了就是对了，不需要 LLM 判断。
2. **raw.json 按 case_id 幂等写入**（重跑/被 kill 后重跑不会产生重复样本，
   这是实验03 数据污染事故的直接修复）。

用法：
    export OPENROUTER_API_KEY="sk-or-..."
    cd .../实验04-参数错误率
    <repo>/.venv/bin/python3 param_error_rate.py
    <repo>/.venv/bin/python3 param_error_rate.py --report
    <repo>/.venv/bin/python3 param_error_rate.py --only c01_basic,c03_leading_zero
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BARE = HERE.parent.parent / "bare-agent"
sys.path.insert(0, str(BARE))

from bare_agent import run_agent, read_file, TOOLS, REGISTRY  # noqa: E402

RAW_PATH = HERE / "raw.json"
MODEL = os.environ.get("BARE_AGENT_MODEL", "nvidia/nemotron-3.5-lightning:free")
N = int(os.environ.get("PARAM_N", "5"))

# ───────────────── 压力工具：参数约束写进 description（协议级要求，违反即错） ─────────────────
def create_order(order_id: str, amount: float, currency: str, date: str, note: str = "") -> str:
    return json.dumps({"ok": True, "order_id": order_id, "amount": amount,
                       "currency": currency, "date": date, "note": note}, ensure_ascii=False)


CREATE_ORDER_SCHEMA = {
    "type": "function", "function": {
        "name": "create_order",
        "description": ("创建订单。要求：order_id 必须原样保留（含前导零，不要转成数字）；"
                        "amount 为纯数字（不要带逗号或货币符号）；"
                        "currency 必须是 CNY/USD/EUR 之一且大写；date 格式为 YYYY-MM-DD"),
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "订单号，原样保留，含前导零"},
            "amount": {"type": "number", "description": "金额，纯数字"},
            "currency": {"type": "string", "enum": ["CNY", "USD", "EUR"], "description": "币种，大写三字母"},
            "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD"},
            "note": {"type": "string", "description": "备注，可选"},
        }, "required": ["order_id", "amount", "currency", "date"]},
    }}

TOOLS_X = TOOLS + [CREATE_ORDER_SCHEMA]
REGISTRY_X = {**REGISTRY, "create_order": create_order}

# ───────────────── 12 个 case：每个都有唯一正确参数（GT 可判定） ─────────────────
CASES = [
    dict(id="c01_basic", cat="基准",
         prompt="帮我创建一个订单：订单号 A20260907001，金额 128.5，币种 CNY，日期 2026-09-07",
         tool="create_order",
         expect=dict(order_id="A20260907001", amount=128.5, currency="CNY", date="2026-09-07")),
    dict(id="c02_long_num_id", cat="长数字串",
         prompt="创建订单，订单号 2026090700189234，金额 99.9，币种 CNY，日期 2026-09-08",
         tool="create_order",
         expect=dict(order_id="2026090700189234", amount=99.9, currency="CNY", date="2026-09-08")),
    dict(id="c03_leading_zero", cat="前导零",
         prompt="创建订单，订单号 007，金额 12，币种 USD，日期 2026-09-09",
         tool="create_order",
         expect=dict(order_id="007", amount=12.0, currency="USD", date="2026-09-09")),
    dict(id="c04_amount_comma", cat="金额带逗号",
         prompt="创建订单，订单号 B100，金额 1,280.00，币种 CNY，日期 2026-09-10",
         tool="create_order",
         expect=dict(order_id="B100", amount=1280.0, currency="CNY", date="2026-09-10")),
    dict(id="c05_amount_cn", cat="中文金额",
         prompt="创建订单，订单号 C200，金额一千二百八十元，币种 CNY，日期 2026-09-11",
         tool="create_order",
         expect=dict(order_id="C200", amount=1280.0, currency="CNY", date="2026-09-11")),
    dict(id="c06_date_cn", cat="中文日期",
         prompt="创建订单，订单号 D300，金额 66.6，币种 EUR，日期是 2026年10月1日",
         tool="create_order",
         expect=dict(order_id="D300", amount=66.6, currency="EUR", date="2026-10-01")),
    dict(id="c07_currency_lower", cat="币种小写",
         prompt="创建订单，订单号 E400，金额 88，币种 usd，日期 2026-09-12",
         tool="create_order",
         expect=dict(order_id="E400", amount=88.0, currency="USD", date="2026-09-12")),
    dict(id="c08_path_space", cat="路径含空格",
         prompt="读取文件 /tmp/my file.txt 的内容",
         tool="read_file", expect=dict(path="/tmp/my file.txt")),
    dict(id="c09_path_unicode", cat="路径含中文",
         prompt="读取文件 /tmp/订单 详情.txt 的内容",
         tool="read_file", expect=dict(path="/tmp/订单 详情.txt")),
    dict(id="c10_amount_int", cat="整数金额",
         prompt="创建订单，订单号 F500，金额 100，币种 CNY，日期 2026-09-13",
         tool="create_order",
         expect=dict(order_id="F500", amount=100.0, currency="CNY", date="2026-09-13")),
    dict(id="c11_very_long_id", cat="超长ID",
         prompt="创建订单，订单号 202609070018923456789012，金额 7.25，币种 CNY，日期 2026-09-14",
         tool="create_order",
         expect=dict(order_id="202609070018923456789012", amount=7.25, currency="CNY", date="2026-09-14")),
    dict(id="c12_distractor", cat="干扰信息",
         prompt=("客户编号 8899 联系我们（电话 13800138000 是客服热线），需要创建订单："
                 "订单号 20260907B，金额 45.6，币种 CNY，日期 2026-09-15。"
                 "另外历史订单 20260101A 的金额是 999，不用管。"),
         tool="create_order",
         expect=dict(order_id="20260907B", amount=45.6, currency="CNY", date="2026-09-15")),
]


# ───────────────── 确定性判定 ─────────────────
def value_match(expect, actual) -> bool:
    if isinstance(expect, (int, float)) and not isinstance(expect, bool):
        try:
            return abs(float(expect) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False
    return expect == actual


def judge(call, case):
    """返回 (是否全对, 错误明细 list)"""
    if call is None:
        return False, ["no_tool_call"]
    errs = []
    if call.get("name") != case["tool"]:
        errs.append(f"wrong_tool:{call.get('name')}")
    args = call.get("arguments") or {}
    if isinstance(args, dict) and args.get("__parse_error__"):
        return False, ["arg_parse_error"] + errs
    for k, want in case["expect"].items():
        if k not in args:
            errs.append(f"missing:{k}")
        elif not value_match(want, args[k]):
            errs.append(f"wrong:{k}(want={want!r},got={args.get(k)!r})")
    return (len(errs) == 0), errs


def first_tool_call(trace):
    for line in trace.lines:
        m = line.get("message") or {}
        if m.get("role") != "assistant":
            continue
        for p in m.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "toolCall":
                return {"id": p.get("id"), "name": p.get("name"), "arguments": p.get("arguments")}
    return None


def load_raw():
    return json.load(open(RAW_PATH, encoding="utf-8")) if RAW_PATH.exists() else {}


def run(only=None):
    data = load_raw()
    cases = [c for c in CASES if (not only or c["id"] in only)]
    for c in cases:
        results = []
        for i in range(N):
            try:
                r = run_agent(c["prompt"], model=MODEL, tools=TOOLS_X, registry=REGISTRY_X)
                call = first_tool_call(r["trace"])
            except Exception as e:
                results.append({"i": i, "ok": False, "errs": [f"run_exception:{type(e).__name__}"],
                                "call": None})
                continue
            ok, errs = judge(call, c)
            results.append({"i": i, "ok": ok, "errs": errs, "call": call,
                            "answer": (r.get("answer") or "")[:300],
                            "turns": r["turns"], "stats": r["stats"]})
            time.sleep(0.4)
        data[c["id"]] = {"case": c, "results": results}      # 幂等：整例替换，不追加
        json.dump(data, open(RAW_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        npass = sum(1 for x in results if x["ok"])
        print(f"  {c['id']:22s} {c['cat']:8s} ok {npass}/{len(results)}"
              + ("" if npass == len(results) else
                 f"   错例: {results[[x['ok'] for x in results].index(False)]['errs'][:2]}"), flush=True)
    return data


def report():
    data = load_raw()
    if not data:
        print("无数据"); return
    print(f"\n{'case':24s}{'类别':10s}{'通过':>8s}{'错误率':>9s}   主要错误")
    print("-" * 88)
    tot = ok_tot = 0
    cat_stat = {}
    for cid, d in data.items():
        rs = d["results"]; n = len(rs); k = sum(1 for x in rs if x["ok"])
        tot += n; ok_tot += k
        c = d["case"]
        cat_stat.setdefault(c["cat"], [0, 0])
        cat_stat[c["cat"]][0] += n; cat_stat[c["cat"]][1] += k
        errs = [e for x in rs if not x["ok"] for e in x["errs"]]
        top = max(set(errs), key=errs.count) if errs else "-"
        print(f"{cid:24s}{c['cat']:10s}{k:>4d}/{n:<3d}{100*(n-k)/n:>8.0f}%   {top[:40]}")
    print("-" * 88)
    print(f"{'合计':24s}{'':10s}{ok_tot:>4d}/{tot:<3d}{100*(tot-ok_tot)/tot:>8.0f}%")
    print("\n按类别：")
    for cat, (n, k) in sorted(cat_stat.items(), key=lambda x: (x[1][1] / x[1][0])):
        print(f"  {cat:10s} {k}/{n}  错误率 {100*(n-k)/n:.0f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--only", default="", help="逗号分隔的 case id，只跑这些")
    a = ap.parse_args()
    if a.report:
        report()
    else:
        only = set(a.only.split(",")) if a.only else None
        print(f"实验04 参数错误率  模型={MODEL}  N={N}  cases={len(CASES) if not only else len(only)}")
        run(only)
        report()
