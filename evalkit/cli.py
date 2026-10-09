#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalkit 命令行入口 —— 「一条命令出带区间的报告」。

用法：
    python -m evalkit report --tasks <任务集.json> [--sids <SID 目录>] [--agent opencode] [--out <输出目录>]

它做什么：
    读声明式任务集 → 按 case 里的 `detector` 名取 `evalkit.trace_detectors` 的函数
    → 用 `evalkit.adapters` 取轨迹 → 判定 → 聚合出带 Wilson 95% CI 的报告。

两个刻意的设计取舍：
    1. **「跑 agent」与「出报告」分开**：本子命令只做判定 + 聚合。
       好处是能对**已有数据**重算 —— 回归验证不必等 4 小时跑批。
       （采集侧将来另起 `run` 子命令，复用 evalkit.adapters。）
    2. **所有参数化都走任务集 JSON**：新增任务、甚至改判定口径，都不动这里的代码。
    3. **无效轨迹单列**：`is_trace_valid` 不通过的样本计入 `INVALID`，**不进任何失败率**。
       这是 C6 的处置 —— 采集故障绝不能变成「agent 失守」。
"""
from __future__ import annotations

import argparse
import csv
import importlib
import pkgutil
import shutil
import sys
import tempfile
from pathlib import Path

from . import batch as B
from . import trace_detectors as TD
from .outcome import RunOutcome, case_summary_from_outcome, score_from_failure_rate
from .stats import two_proportion_z, wilson
from .tasks import load_task_set


def discover_adapters() -> dict:
    """自动发现 `adapters/` 下有 `drive` + `fetch_trace` 的模块。

    为什么不写死注册表：**加一个 agent 不该改 CLI 的代码**。
    每个适配器自带「怎么跑它」与「怎么读它」，本模块只负责按名字找出来用。

    发现规则（两条都要满足）：
        · 模块里有 `drive(prompt, cwd, ...)` —— 采集侧
        · 模块里有 `fetch_trace(sid)`        —— 判定侧
    """
    from . import adapters as pkg
    out = {}
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name.startswith("_"):
            continue
        mod = importlib.import_module(f".adapters.{m.name}", __package__)
        if callable(getattr(mod, "drive", None)) and callable(getattr(mod, "fetch_trace", None)):
            out[m.name] = {
                "drive": mod.drive,
                "fetch": mod.fetch_trace,
                "default_db": getattr(mod, "DEFAULT_DB", None),
                "module": f"evalkit.adapters.{m.name}",
            }
    return out


ADAPTERS = discover_adapters()


def _aggregate_case(case, pairs):
    """对某 case 的一批 `(run_idx, trace)` 做判定 + 三类计数。

    返回 `(rows, stats, summary_case, line)`。其中：

        stats["n"] / stats["hits"] —— **只统计可判定样本**
        `no_output`     有输入但 agent 没产出 → 给**双向读数**
                        （可判定样本里的失败率 + 若按「通过」计的失败率）
        `collect_failed` 采集失败 → **是管线问题不是 agent 问题**，单独报警

    这样既不假装知道（无证据的不塞进分子分母），也不丢信息（两种口径都给）。
    """
    fn = getattr(TD, case["detector"])
    rows, hits = [], []
    no_output = collect_failed = 0
    cid = case["id"]

    for run_idx, trace in pairs:
        cls, why = TD.trace_quality(trace)
        if cls != "ok":
            if cls == "no_output":
                no_output += 1
            else:
                collect_failed += 1
            rows.append({"case": cid, "run": run_idx, "class": cls,
                         "invalid": True, "reason": why})
            continue
        res = fn(trace, **case.get("detector_params", {}))
        h = 1 if res.get("hit") else 0
        hits.append(h)
        rows.append({
            "case": cid, "run": run_idx,
            "trace": f"traces/norm/{str(cid).replace('/', '_')}/{run_idx}.json",
            "detector": case["detector"], "params": case.get("detector_params", {}),
            "verdict": h, "invalid": False,
            "detail": res.get("detail", ""), "metric": res.get("metric"),
        })

    n, k = len(hits), sum(hits)
    p, lo, hi = wilson(k, n)
    line = (f"[{cid}] 样本={n} 命中(失败)={k}/{n}={p * 100:.1f}% "
            f"CI[{lo * 100:.1f}%,{hi * 100:.1f}%]  score={score_from_failure_rate(p):.3f}  "
            f"({case.get('label', '')})")
    if no_output:
        tot = n + no_output
        line += (f"  ⚠无产出{no_output} → 若按「通过」计：{k}/{tot}="
                 f"{k / tot * 100:.1f}%")
    if collect_failed:
        line += f"  ❌采集失败{collect_failed}（管线问题，不是 agent 的问题）"

    # 双字段 summary：score/metric/raw/ci + 保留 n/hits/rate（P-0）
    oc = RunOutcome.from_case_rate(
        case_id=cid, rate=p, n=n, hits=k, ci=[lo, hi],
        label=case.get("label"),
        extra={"no_output": no_output, "collect_failed": collect_failed},
    )
    summary_case = case_summary_from_outcome(
        oc, n=n, hits=k, rate=p, ci=[lo, hi],
        no_output=no_output, collect_failed=collect_failed,
        label=case.get("label"),
    )
    stats = {"n": n, "hits": k, "no_output": no_output,
             "collect_failed": collect_failed}
    return rows, stats, summary_case, line


def _report_external(batch_dir: Path, meta: dict) -> int:
    """external 批：**只读内嵌 cases 出报告，绝不重判**（SWE 预留②）。

    轨迹可能为 None、判定已在导入时完成；这里重跑检测器会：
        1) 伪造「可重判」的假象（外部轨迹往往没有）
        2) 用本 kit 口径覆盖外部 reward，破坏 A 桥接语义
    """
    cases = B.embedded_cases(meta)
    if not cases:
        # 兜底：summary.json 里也可能有（new_external_batch 两处都写）
        sp = batch_dir / "summary.json"
        if sp.exists():
            import json as _json
            cases = _json.loads(sp.read_text(encoding="utf-8")).get("cases") or []
    if not cases:
        print(f"external 批无内嵌 cases：{batch_dir}", file=sys.stderr)
        return 2

    print(f"external 批（不重判）｜suite={(meta.get('suite') or {}).get('id') or '—'} "
          f"｜source={(meta.get('source') or {}).get('kind') or '—'}")
    lines = []
    for c in cases:
        score = c.get("score")
        metric = c.get("metric", "?")
        raw = c.get("raw") or {}
        rate = c.get("rate", raw.get("rate"))
        n, hits = c.get("n", raw.get("n")), c.get("hits", raw.get("hits"))
        ci = c.get("ci")
        ci_s = ""
        if ci:
            ci_s = f" CI[{ci[0] * 100:.1f}%,{ci[1] * 100:.1f}%]"
        rate_s = ""
        if rate is not None and n is not None:
            rate_s = f" 命中={hits}/{n}={rate * 100:.1f}%"
        line = (f"[{c.get('id', '?')}] score={score} metric={metric}{rate_s}{ci_s}  "
                f"({c.get('label') or raw.get('label') or ''})")
        lines.append(line)
        print(line)
    print(f"\n读取完成（{len(cases)} cases，未重判）→ {batch_dir}")
    return 0


def _report_from_batch(args) -> int:
    """从**批次目录**出报告。

    · internal：读 `traces/norm` → 判定 → 覆写 verdicts + summary（离线重判）
    · external：只读内嵌 cases，**不重判**（SWE 预留②）

    internal 这条路不碰任何 agent、也不碰它的本机存储 —— 只要批次目录还在，
    换台机器、换个人都能重算。这就是「轨迹必须落盘」换来的东西。
    """
    bd = Path(args.batch)
    meta = B.load_batch(bd)
    if B.is_external(meta):
        return _report_external(bd, meta)

    # task_set.path 可能是相对路径（相对批次目录或 CWD）——按常见锚点解析
    ts = meta.get("task_set") or {}
    tasks_path = ts.get("path")
    spec = None
    if tasks_path:
        candidates = [
            Path(tasks_path),
            bd / tasks_path,
            Path(args.batch).resolve().parent / tasks_path,
        ]
        for cand in candidates:
            if cand.is_file():
                spec = load_task_set(cand)
                break
        if spec is None:
            print(f"找不到任务集文件：{tasks_path}（尝试了 CWD/批次相对路径）",
                  file=sys.stderr)
            return 2
    else:
        print("internal 批缺少 task_set.path", file=sys.stderr)
        return 2

    rows, cases = [], []
    valid = no_output_total = collect_failed_total = 0
    seen_models = []
    for case in spec["cases"]:
        traces = B.read_traces(bd, case["id"])
        if not traces:
            print(f"[{case['id']}] 批次内无轨迹，跳过")
            continue
        seen_models.extend(t.model for _, t in traces if t.model)
        crows, st, scase, line = _aggregate_case(case, traces)
        rows.extend(crows)
        cases.append(scase)
        valid += st["n"]
        no_output_total += st["no_output"]
        collect_failed_total += st["collect_failed"]
        print(line)

    # 轨迹里带出了实测模型而 batch.json 没记（旧批 model=None）→ 回填，不覆盖已有值
    B.update_agent_model(bd, seen_models)

    B.write_verdicts(bd, rows)
    B.write_summary(bd, cases, counts={
        "runs": len(rows), "valid": valid,
        "no_output": no_output_total, "collect_failed": collect_failed_total,
        "invalid": no_output_total + collect_failed_total,
    })
    print(f"\n重判完成 -> {bd}")
    print(f"  runs={len(rows)}｜可判定={valid}｜无产出={no_output_total}"
          f"｜采集失败={collect_failed_total}")
    if collect_failed_total:
        print("  ❌ 采集失败属于管线问题，必须先查管线，不能当成 agent 失守")
    return 0


def cmd_report(args) -> int:
    if args.batch:
        return _report_from_batch(args)
    if not args.tasks:
        print("需要 --tasks 或 --batch 之一", file=sys.stderr)
        return 2

    spec = load_task_set(args.tasks)
    meta = spec.get("meta", {})
    agent = args.agent or meta.get("agent", "opencode")
    ad = ADAPTERS.get(agent)
    if ad is None:
        print(f"未知 agent「{agent}」；已发现：{', '.join(ADAPTERS) or '（无）'}", file=sys.stderr)
        return 2
    fetch = ad["fetch"]

    base = Path(args.tasks).resolve().parent
    sids_dir = Path(args.sids) if args.sids else (base / meta.get("sids_dir", "."))
    out_dir = Path(args.out) if args.out else sids_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    name = spec.get("name", "report")

    lines, rows = [], []
    total_no_output = total_collect_failed = 0
    for case in spec["cases"]:
        if getattr(TD, case["detector"], None) is None:  # load_task_set 已校验，此处仅防御
            print(f"[{case['id']}] 检测器不存在：{case['detector']}", file=sys.stderr)
            return 2
        fp = sids_dir / case["sids_file"]
        if not fp.exists():
            lines.append(f"[{case['id']}] 缺 SID 文件，跳过（{fp.name}）")
            continue

        sids = [ln.strip() for ln in fp.read_text().splitlines() if ln.strip()]
        pairs = [(i, fetch(sid)) for i, sid in enumerate(sids)]
        crows, st, _scase, line = _aggregate_case(case, pairs)
        for r, sid in zip(crows, sids):   # SID 路径的明细多带一列 sid，便于回查
            r["sid"] = sid
        rows.extend(crows)
        total_no_output += st["no_output"]
        total_collect_failed += st["collect_failed"]
        lines.append(line)

    summary = out_dir / f"summary_{name}.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw = out_dir / f"raw_{name}.csv"
    with raw.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case", "sid", "run", "verdict(1=失败/INVALID)", "detail"])
        for r in rows:
            w.writerow([r.get("case"), r.get("sid", ""), r.get("run"),
                        "INVALID" if r.get("invalid") else r.get("verdict"),
                        r.get("reason") or r.get("detail", "")])

    print("\n".join(lines))
    if total_no_output or total_collect_failed:
        print(f"\n⚠ 不可判定 {total_no_output + total_collect_failed} 条已单列，未计入失败率："
              f"无产出 {total_no_output}（可能主动拒绝，也可能静默失败）｜"
              f"采集失败 {total_collect_failed}（这是管线问题，不是 agent 的问题）")
    print(f"\n汇总 -> {summary}")
    print(f"明细 -> {raw}")
    return 0


def cmd_run(args) -> int:
    """采集侧：按任务集驱动 agent，把轨迹落成一个**批次目录**（判定交给 report）。

    与 `report` 分开是刻意的：`run` 只负责"真的去跑"，产物就是轨迹。
    于是「对已有批次重判」不需要重跑 —— 这正是今天能对账的前提。
    """
    import time

    spec = load_task_set(args.tasks)
    meta = spec.get("meta", {})
    agent = args.agent or meta.get("agent", "opencode")
    ad = ADAPTERS.get(agent)
    if ad is None:
        print(f"未知 agent「{agent}」；已发现：{', '.join(ADAPTERS) or '（无）'}", file=sys.stderr)
        return 2

    from . import __version__ as evalkit_version

    # P-0：任务集可声明 suite；默认 coding 轨迹任务挂 coding.t1t5（指纹归套件）
    suite = None
    suite_id = meta.get("suite_id")
    if suite_id is None:
        dets = {c.get("detector", "") for c in spec.get("cases", [])}
        if any(d.startswith("detect_") for d in dets):
            suite_id = "coding.t1t5"
    if suite_id:
        try:
            from .suites import discover_suites
            suite = discover_suites().get(suite_id)
        except Exception:  # noqa: BLE001 — 套件缺失不阻断采集，指纹回退 detectors
            suite = None

    base = Path(args.tasks).resolve().parent
    k = args.k
    batch_id = args.batch_id or (time.strftime("%Y%m%d") +
                                 f"-{agent}-{spec.get('name', 'run')}-k{k}")
    bd = Path(args.out) if args.out else (base.parent / "results" / agent / batch_id)

    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="evalkit-run-"))
    planned = []
    for case in spec["cases"]:
        if args.only and case["id"] not in args.only:
            continue
        cwd = work / str(case["id"]).replace("/", "_")
        cwd.mkdir(parents=True, exist_ok=True)
        fx = case.get("fixture_dir")
        if fx:
            src = base / fx
            if src.is_dir():
                shutil.copytree(src, cwd, dirs_exist_ok=True)
            else:
                print(f"  ⚠ fixture 不存在，跳过拷贝：{src}")
        planned.append((case, cwd, case.get("prompts") or [case["prompt"]]))

    print(f"agent={agent} ｜ k={k} ｜ 工作区={work} ｜ 批次={bd}")
    for case, _, prompts in planned:
        print(f"  {case['id']:8s} 轮数={len(prompts)} "
              f"fixture={case.get('fixture_dir') or '（无）'}")
    if args.dry_run:
        print("\n[dry-run] 只打印计划，未真跑。")
        return 0

    # 批次目录必须在 dry-run 之后才建：dry-run 只打印计划，
    # 不许在 experiments/ 留空骨架（2026-09-26 踩坑：A8 两次 dry-run 残留 2 个空批次目录）
    B.new_batch(bd, batch_id=batch_id, agent=agent, model=None,
                adapter=agent, adapter_version=evalkit_version,
                task_set_path=args.tasks, k=k, suite=suite)

    from .trace_schema import NormalizedTrace

    runs = valid = invalid = 0
    seen_models = []
    for case, cwd, prompts in planned:
        cont = bool(case.get("continuation"))
        for i in range(args.start_index, args.start_index + k):
            sid, is_new = None, False
            for ri, p in enumerate(prompts):
                sid, is_new = ad["drive"](p, cwd, timeout=args.timeout,
                                          continuation=(cont and ri > 0))
                if not is_new:
                    break
            runs += 1
            trace = ad["fetch"](sid) if sid else None
            trace = trace or NormalizedTrace(
                agent=agent, model=None,
                source="驱动未产生新 session（is_new=False）—— 可能是拒绝执行，也可能是驱动故障",
                steps=[])
            if trace.model:
                seen_models.append(trace.model)
            B.write_trace(bd, case["id"], i, trace, raw={"sid": sid, "is_new": is_new})
            ok, _ = TD.is_trace_valid(trace)
            valid += 1 if ok else 0
            invalid += 0 if ok else 1
        print(f"  [{case['id']}] 完成 {k} run")

    # 模型名以轨迹实测为准回填（建批时未知，写的是 None）—— 模型必须可追溯
    B.update_agent_model(bd, seen_models)

    B.write_summary(bd, [], counts={"runs": runs, "valid": valid, "invalid": invalid})
    print(f"\n批次 -> {bd}")
    print(f"  runs={runs}｜valid={valid}｜invalid={invalid}")
    print(f"  下一步：python -m evalkit report --batch {bd}")
    return 0


def cmd_compare(args) -> int:
    """并排对比两个批次 —— 「能不能比」交给 `batch.comparable()` 判，不靠人回忆。

    为什么这是 ④（全局统计）的核心：单批次的率值只回答「这一次是多少」；
    只有两个批次并排，才能回答「**改了什么东西之后变了多少、这个变化算不算数**」。
    """
    import json as _json

    pa, pb = Path(args.batch_a), Path(args.batch_b)
    ma, mb = B.load_batch(pa), B.load_batch(pb)
    sa = _json.loads((pa / "summary.json").read_text(encoding="utf-8")).get("cases", [])
    sb = _json.loads((pb / "summary.json").read_text(encoding="utf-8")).get("cases", [])

    print(f"A = {ma.get('batch_id')}  created={ma.get('created', '?')}")
    print(f"B = {mb.get('batch_id')}  created={mb.get('created', '?')}")
    print()

    ok, reasons = B.comparable(ma, mb)
    if ok:
        print("✓ 可比：题目 / 判定口径 / 底座模型 三者一致")
    else:
        print("✗ 不可比 —— 下列要素不同（数字差异不能直接归因于被测对象）：")
        for r in reasons:
            print(f"    · {r}")
    print()

    da = {c["id"]: c for c in sa}
    db = {c["id"]: c for c in sb}
    print(f"{'case':10s} | {'A 命中/样本':>11s} {'A 率':>7s} | {'B 命中/样本':>11s} {'B 率':>7s} | 差异")
    print("-" * 80)
    for cid in sorted(set(da) | set(db)):
        ca, cb = da.get(cid), db.get(cid)
        ta = f"{ca['hits']}/{ca['n']}" if ca else "—"
        tb = f"{cb['hits']}/{cb['n']}" if cb else "—"
        ra = f"{ca['rate'] * 100:.1f}%" if ca else "—"
        rb = f"{cb['rate'] * 100:.1f}%" if cb else "—"
        note = ""
        if ca and cb and ca["n"] and cb["n"]:
            z, pv = two_proportion_z(ca["hits"], ca["n"], cb["hits"], cb["n"])
            note = f"{(cb['rate'] - ca['rate']) * 100:+.1f}pp  z={z:+.2f} p={pv:.3f}"
            if min(ca["n"], cb["n"]) < 10:
                note += "  ⚠n<10 不看 p"
        print(f"{cid:10s} | {ta:>11s} {ra:>7s} | {tb:>11s} {rb:>7s} | {note}")
    print()
    print("pp = 百分点 ｜ z/p = 两比例检验（差异是否超出抽样噪声）")
    return 0


def cmd_import_tau(args) -> int:
    """τ A 桥接：results.json → external batch（只读 reward，不重判）。

    惰性 import —— 不调本命令不加载 suites.tau（与 discover 一样受懒加载约束）。
    """
    from .suites.tau import import_tau_results

    out = Path(args.out) if args.out else (
        Path("experiments") / "实验09-τ桥接" / "results" /
        (args.batch_id or Path(args.results).resolve().parent.name)
    )
    try:
        bd = import_tau_results(
            args.results,
            out_dir=out,
            domain=args.domain,
            batch_id=args.batch_id,
            agent=args.agent,
            model=args.model,
            suite_id=args.suite_id,
        )
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"已导入 external 批（不重判）→ {bd}")
    print(f"  下一步：python -m evalkit report --batch {bd}")
    return 0


def cmd_import_swe(args) -> int:
    """SWE-Bench A 桥接：resolved/reward 结果 → external batch（不重判）。

    惰性 import —— 不调本命令不加载 suites.swe。
    """
    from .suites.swe import import_swe_results

    out = Path(args.out) if args.out else (
        Path("experiments") / "实验10-SWE桥接" / "results" /
        (args.batch_id or (
            Path(args.results).name if Path(args.results).is_dir()
            else Path(args.results).resolve().parent.name
        ))
    )
    try:
        bd = import_swe_results(
            args.results,
            out_dir=out,
            batch_id=args.batch_id,
            agent=args.agent,
            model=args.model,
            suite_id=args.suite_id,
            dataset=args.dataset,
        )
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"已导入 external 批（不重判）→ {bd}")
    print(f"  下一步：python -m evalkit report --batch {bd}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="evalkit", description="Agent 评测工具包")
    sub = ap.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("compare", help="并排对比两个批次（含可比性判定与显著性检验）")
    cp.add_argument("batch_a", help="批次 A 目录")
    cp.add_argument("batch_b", help="批次 B 目录")
    cp.set_defaults(func=cmd_compare)

    rn = sub.add_parser("run", help="驱动 agent 采集轨迹，落成一个批次目录")
    rn.add_argument("--tasks", required=True, help="任务集 JSON 路径")
    rn.add_argument("--agent", default=None, help="agent 适配器名（默认取 meta.agent）")
    rn.add_argument("--k", type=int, default=1, help="每个 case 跑几次（默认 1）")
    rn.add_argument("--start-index", dest="start_index", type=int, default=0,
                    help="run 序号起始（断点续跑/补样本，不覆盖已有 run；2026-09-26 实验10 补 cs5 样本用）")
    rn.add_argument("--out", default=None, help="批次输出目录")
    rn.add_argument("--batch-id", dest="batch_id", default=None, help="批次 id")
    rn.add_argument("--work", default=None, help="fixture 工作区（默认临时目录）")
    rn.add_argument("--timeout", type=int, default=180, help="单次调用超时秒数")
    rn.add_argument("--only", nargs="*", default=None, help="只跑指定 case id")
    rn.add_argument("--dry-run", dest="dry_run", action="store_true", help="只打印计划，不真跑")
    rn.set_defaults(func=cmd_run)

    r = sub.add_parser("report", help="判定 + 聚合出带 CI 的报告（SID 目录 或 批次目录）")
    r.add_argument("--tasks", default=None, help="任务集 JSON 路径（与 --batch 二选一）")
    r.add_argument("--batch", default=None, help="批次目录：轨迹已在里面，可**离线重判**，不必重跑")
    r.add_argument("--sids", default=None, help="SID 文件目录（默认取任务集 meta.sids_dir）")
    r.add_argument("--agent", default=None, help="轨迹来源 agent（默认取 meta.agent）")
    r.add_argument("--out", default=None, help="输出目录（默认 SID 目录的上一层）")
    r.set_defaults(func=cmd_report)

    it = sub.add_parser(
        "import-tau",
        help="导入 tau2 results.json 为 external 批（只读 reward_info.reward，不重判）",
    )
    it.add_argument("--results", required=True,
                    help="results.json 路径或所在目录")
    it.add_argument("--out", default=None,
                    help="批次输出目录（默认 experiments/实验09-τ桥接/results/…）")
    it.add_argument("--domain", default=None,
                    help="覆盖 domain（默认 environment_info.domain_name）")
    it.add_argument("--batch-id", dest="batch_id", default=None, help="批次 id")
    it.add_argument("--agent", default=None, help="agent 显示名（默认 tau2）")
    it.add_argument("--model", default=None, help="覆盖 model（默认 info.agent_info.llm）")
    it.add_argument("--suite-id", dest="suite_id", default=None,
                    help="覆盖 suite id（默认 tau.<domain>）")
    it.set_defaults(func=cmd_import_tau)

    is_ = sub.add_parser(
        "import-swe",
        help="导入 SWE-Bench 风格 resolved/reward 结果为 external 批（不重判）",
    )
    is_.add_argument("--results", required=True,
                     help="结果文件路径（json/jsonl）或所在目录")
    is_.add_argument("--out", default=None,
                     help="批次输出目录（默认 experiments/实验10-SWE桥接/results/…）")
    is_.add_argument("--batch-id", dest="batch_id", default=None, help="批次 id")
    is_.add_argument("--agent", default=None, help="agent 显示名（默认 swe-harness）")
    is_.add_argument("--model", default=None, help="覆盖 model（默认读结果顶层 meta）")
    is_.add_argument("--suite-id", dest="suite_id", default=None,
                     help="覆盖 suite id（默认 swe.bench）")
    is_.add_argument("--dataset", default=None,
                     help="数据集/split 标签（如 swebench_verified）")
    is_.set_defaults(func=cmd_import_swe)

    args = ap.parse_args(argv)
    return args.func(args)
