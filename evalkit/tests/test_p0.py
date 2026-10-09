#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalkit 自测（双模式：pytest 或纯脚本）。

用法：
    .venv/bin/python3 -m pytest evalkit/tests -q     # pytest（dev extra）
    .venv/bin/python3 -m evalkit.tests               # 纯脚本（无 pytest 也可跑）

退出码 0 = 全过。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许 `python3 evalkit/tests/test_p0.py` 直跑
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# pytest 下 check 失败即 AssertionError（逐条定位）；
# 纯脚本下继续收集，最后统一汇总 PASS/FAIL。
_IN_PYTEST = "pytest" in sys.modules

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if not _IN_PYTEST:
        (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    tail = f"  — {extra}" if extra and not cond else ""
    print(f"  {mark} {name}{tail}")
    if not cond and _IN_PYTEST:
        msg = f"{name} — {extra}" if extra else name
        raise AssertionError(msg)


def section(title: str) -> None:
    print(f"\n【{title}】")


def main() -> int:
    test_outcome()
    test_suite_discover()
    test_batch_external()
    test_swe_reservations()
    test_lazy_imports()
    test_tau_bridge()
    test_swe_bridge()

    print(f"\n=== 通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


# ---------------------------------------------------------------- outcome
def test_outcome() -> None:
    section("outcome · 双字段")
    from evalkit.outcome import (
        RunOutcome, clamp01, score_from_failure_rate, case_summary_from_outcome,
    )

    check("clamp01 夹取", clamp01(1.5) == 1.0 and clamp01(-0.2) == 0.0)
    check("clamp01 NaN→0", clamp01(float("nan")) == 0.0)
    check("score=1-rate", abs(score_from_failure_rate(0.0667) - 0.9333) < 1e-3)

    # run 级：hit → score 0
    oc = RunOutcome.from_failure_hit(case_id="T1/n6", run=0, hit=True,
                                     detail="遗漏 f7.py")
    check("failure_hit score=0", oc.score == 0.0 and oc.metric == "failure_rate")
    check("failure_hit 保留 raw.hit", oc.raw.get("hit") is True)

    # case 级
    oc2 = RunOutcome.from_case_rate(case_id="T1/n6", rate=0.0667, n=30, hits=2,
                                    ci=[0.0185, 0.2132], label="漏读")
    check("case score=1-rate", abs(oc2.score - 0.9333) < 1e-3)
    s = case_summary_from_outcome(oc2, label="漏读")
    check("summary 双字段齐全",
          {"score", "metric", "raw", "ci", "n", "hits", "rate"} <= set(s),
          str(sorted(s)))
    check("summary 保留 coding 叙述 n/hits/rate",
          s["n"] == 30 and s["hits"] == 2 and abs(s["rate"] - 0.0667) < 1e-4)
    check("summary score 高=好", abs(s["score"] - 0.9333) < 1e-3)

    # τ reward
    oc3 = RunOutcome.from_reward(case_id="create_ticket", run=0, reward=0.7,
                                 breakdown={"reward": 0.7, "db_check": 1.0})
    check("reward score=reward", oc3.score == 0.7 and oc3.metric == "reward")
    check("reward_breakdown 进 raw", "reward_breakdown" in oc3.raw)

    # 序列化
    d = oc2.to_dict()
    check("to_dict 无真 trace 对象", d.get("trace") is None)
    back = RunOutcome.from_dict(d)
    check("from_dict 往返 score/metric",
          back.score == oc2.score and back.metric == oc2.metric)


# ---------------------------------------------------------------- suite
def test_suite_discover() -> None:
    section("suite · discover_suites")
    from evalkit.suites import discover_suites
    from evalkit.suite import Suite

    suites = discover_suites()
    check("发现 ≥2 套件", len(suites) >= 2, str(list(suites)))
    check("含 coding.t1t5", "coding.t1t5" in suites, str(list(suites)))
    check("含 weather.w1", "weather.w1" in suites, str(list(suites)))

    coding = suites.get("coding.t1t5")
    check("coding 是 Suite", isinstance(coding, Suite))
    if coding:
        check("coding kind=internal", coding.kind == "internal")
        fp = coding.fingerprint()
        check("coding 指纹非空且 sha256 形", bool(fp) and fp.startswith("sha256:"),
              str(fp))
        # 指纹稳定性：再算一次应相同
        check("coding 指纹稳定", fp == coding.fingerprint())
        meta = coding.to_meta()
        check("to_meta 含 fingerprint", meta.get("fingerprint") == fp)

    weather = suites.get("weather.w1")
    if weather:
        check("weather 指纹非空", bool(weather.fingerprint()),
              str(weather.fingerprint()))


# ---------------------------------------------------------------- batch external
def test_batch_external() -> None:
    section("batch v2 · external 不重判 + cases 内嵌")
    import tempfile
    import shutil
    from evalkit import batch as B
    from evalkit.outcome import RunOutcome, case_summary_from_outcome

    td = Path(tempfile.mkdtemp(prefix="evalkit-p0-"))
    try:
        cases = []
        for cid, rate, n, hits in [
            ("T1/n6", 0.0667, 30, 2),
            ("T2/a9", 0.5, 30, 15),
        ]:
            oc = RunOutcome.from_case_rate(
                case_id=cid, rate=rate, n=n, hits=hits,
                ci=[0.0, 0.2], label="x")
            cases.append(case_summary_from_outcome(oc, label="x"))

        bd = B.new_external_batch(
            td / "ext1",
            batch_id="p0-ext-demo",
            agent="tau2",
            model="gpt-4.1",
            suite_id="tau.zh_cs",
            cases=cases,
            source={"kind": "tau2", "results_path": "results.json"},
        )
        meta = B.load_batch(bd)
        check("mode=external", B.is_external(meta))
        check("cases 内嵌 batch.json", len(B.embedded_cases(meta)) == 2)
        check("schema_version=2", meta.get("schema_version") == 2)
        check("summary.json 已写", (bd / "summary.json").is_file())
        check("无 traces/ 目录（外部不落轨迹）",
              not (bd / "traces").exists())

        # report external 路径：不重判
        from evalkit.cli import main as cli_main
        rc = cli_main(["report", "--batch", str(bd)])
        check("report external 返回 0", rc == 0)
        # 内嵌 cases 未被清掉
        meta2 = B.load_batch(bd)
        check("report 后 cases 仍在（未重判覆盖）",
              len(B.embedded_cases(meta2)) == 2)

        # new_batch 拒绝 external mode
        try:
            B.new_batch(td / "bad", batch_id="x", agent="a", model=None,
                        adapter="a", adapter_version="0",
                        task_set_path=__file__, k=1, mode="external")
            check("new_batch(mode=external) 应 raise", False)
        except ValueError:
            check("new_batch(mode=external) 应 raise", True)

        # comparable：双侧 external 无 task_set hash → 不因缺失判不可比
        ok, reasons = B.comparable(meta, meta)
        check("同批自比可比", ok, str(reasons))

        # 指纹不同 → 不可比
        m2 = dict(meta)
        m2["evaluator"] = dict(meta.get("evaluator") or {})
        m2["evaluator"]["suite_fingerprint"] = "sha256:deadbeefdeadbeef"
        ok2, reasons2 = B.comparable(meta, m2)
        check("指纹不同 → 不可比", (not ok2) and any("指纹" in r for r in reasons2),
              str(reasons2))
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------- SWE 预留
def test_swe_reservations() -> None:
    section("SWE-Bench 四条预留（进 P-0 验收）")

    # ① RunOutcome.trace 可为 None
    from evalkit.outcome import RunOutcome
    oc = RunOutcome(case_id="c", run=0, score=1.0, metric="reward", trace=None)
    check("① trace=None 合法", oc.trace is None and oc.score == 1.0)
    d = oc.to_dict()
    check("① to_dict 后 trace 仍可缺", d.get("trace") is None)

    # ② external 不重判 —— 在 test_batch_external 已验（report 后 cases 完好）
    #    这里补：CLI 不会去 load_task_set / 读 traces
    import inspect
    from evalkit import cli as cli_mod
    src = inspect.getsource(cli_mod._report_external)
    check("② external 分支无 load_task_set", "load_task_set" not in src)
    check("② external 分支无 read_traces", "read_traces" not in src)
    check("② external 分支无 _aggregate_case", "_aggregate_case" not in src)

    # ③ fingerprint 归 suite
    from evalkit.suites import discover_suites
    suites = discover_suites()
    coding = suites.get("coding.t1t5")
    check("③ Suite.fingerprint() 可调用", coding is not None and
          callable(getattr(coding, "fingerprint", None)))
    fp = coding.fingerprint() if coding else None
    check("③ 指纹来自套件源文件", bool(fp))
    # 改 suite 源文件指纹应变 —— 不真改文件，验证 sources 指向存在文件
    if coding and coding.fingerprint_sources:
        root = coding.root
        all_exist = all((root / s).is_file() for s in coding.fingerprint_sources)
        check("③ fingerprint_sources 均存在", all_exist,
              str(coding.fingerprint_sources))

    # ④ evidence 允许 refs/路径
    oc2 = RunOutcome(case_id="c", run=0, score=1.0, metric="reward",
                     evidence=[{"ref": "trajectories/rollout-1.json"},
                               {"path": "/tmp/foo/bar.txt", "note": "外部路径"}])
    check("④ evidence refs 合法", oc2.evidence is not None and
          oc2.evidence[0].get("ref") is not None)
    check("④ evidence 路径合合法", oc2.evidence[1].get("path") is not None)


# ---------------------------------------------------------------- tau bridge
def test_tau_bridge() -> None:
    section("tau · A 桥接（reward 导入 · 不重判）")
    import tempfile
    import shutil
    import json as _json

    from evalkit.suites import discover_suites
    from evalkit.suites.tau import (
        SUITES, suite_for, detect_domain, cases_from_results, import_tau_results,
    )

    # 每 domain 一 suite
    check("SUITES 含 5 个 tau.*",
          set(SUITES) >= {"tau.airline", "tau.retail", "tau.telecom",
                          "tau.mock", "tau.zh_cs"},
          str(sorted(SUITES)))
    suites = discover_suites()
    check("discover 含 tau.zh_cs", "tau.zh_cs" in suites, str(sorted(suites)))
    s = suite_for("zh_cs")
    check("suite kind=external", s.kind == "external")
    check("suite 指纹非空", bool(s.fingerprint()), str(s.fingerprint()))

    # 合成 results.json（最小 tau2 形状）
    fixture = {
        "timestamp": "2026-09-23T00:00:00",
        "info": {
            "num_trials": 2,
            "seed": 1,
            "git_commit": "test",
            "agent_info": {"implementation": "llm_agent",
                           "llm": "openrouter/test-model"},
            "user_info": {"llm": "openrouter/test-user"},
            "environment_info": {"domain_name": "zh_cs"},
        },
        "tasks": [
            {"id": "t_ok", "ticket": "全对",
             "description": {"purpose": "ok case"}},
            {"id": "t_partial", "ticket": "半对",
             "description": {"purpose": "partial"}},
            {"id": "t_fail", "ticket": "全错",
             "description": {"purpose": "fail"}},
        ],
        "simulations": [
            {"task_id": "t_ok", "trial": 0,
             "reward_info": {"reward": 1.0, "reward_basis": ["DB"],
                             "reward_breakdown": {"DB": 1.0}}},
            {"task_id": "t_ok", "trial": 1,
             "reward_info": {"reward": 1.0, "reward_basis": ["DB"],
                             "reward_breakdown": {"DB": 1.0}}},
            {"task_id": "t_partial", "trial": 0,
             "reward_info": {"reward": 0.5, "reward_basis": ["DB"]}},
            {"task_id": "t_partial", "trial": 1,
             "reward_info": {"reward": 0.5, "reward_basis": ["DB"]}},
            {"task_id": "t_fail", "trial": 0,
             "reward_info": {"reward": 0.0, "reward_basis": ["DB"]}},
            {"task_id": "t_fail", "trial": 1, "reward_info": None},
        ],
    }

    dom = detect_domain(fixture, path="20260923_zh_cs_llm_agent_x")
    check("detect_domain 环境优先", dom == "zh_cs", dom)
    dom2 = detect_domain({"info": {}}, path="20260923_mock_llm_agent_x")
    check("detect_domain 目录名回退", dom2 == "mock", dom2)

    cases, counts = cases_from_results(fixture, domain="zh_cs")
    check("cases 数=3", len(cases) == 3, str([c["id"] for c in cases]))
    by = {c["id"]: c for c in cases}
    check("t_ok score=1 metric=reward",
          by["t_ok"]["score"] == 1.0 and by["t_ok"]["metric"] == "reward")
    check("t_partial score=0.5（mean reward，非满奖率）",
          by["t_partial"]["score"] == 0.5, str(by["t_partial"]))
    # 满奖率：0 次 reward>=1 → hits=0 rate=0，但 score=0.5
    check("t_partial hits=0 rate=0（compare 率值=满奖率）",
          by["t_partial"]["hits"] == 0 and by["t_partial"]["rate"] == 0.0,
          str({k: by["t_partial"][k] for k in ("n", "hits", "rate", "score")}))
    check("t_ok raw 含 rewards 明细", by["t_ok"]["raw"].get("rewards") == [1.0, 1.0])
    check("counts runs=6 valid=5 invalid=1",
          counts["runs"] == 6 and counts["valid"] == 5 and counts["invalid"] == 1,
          str(counts))

    td = Path(tempfile.mkdtemp(prefix="evalkit-tau-"))
    try:
        rp = td / "results.json"
        rp.write_text(_json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        # 写进兄弟目录，domain 靠 environment_info
        bd = import_tau_results(rp, out_dir=td / "batch",
                                batch_id="p0-tau-demo")
        from evalkit import batch as B
        meta = B.load_batch(bd)
        check("import → mode=external", B.is_external(meta))
        check("suite id=tau.zh_cs",
              (meta.get("suite") or {}).get("id") == "tau.zh_cs",
              str(meta.get("suite")))
        check("evaluator.suite_fingerprint 存在",
              bool((meta.get("evaluator") or {}).get("suite_fingerprint")))
        check("task_set.sha256 只含题目",
              (meta.get("task_set") or {}).get("sha256", "").startswith("sha256:"))
        check("无 traces/", not (bd / "traces").exists())
        check("cases 内嵌 n=3", len(B.embedded_cases(meta)) == 3)
        check("model 取 agent_info.llm",
              (meta.get("agent") or {}).get("model") == "openrouter/test-model",
              str(meta.get("agent")))

        # report 不重判
        from evalkit.cli import main as cli_main
        rc = cli_main(["report", "--batch", str(bd)])
        check("report external 返回 0", rc == 0)
        meta2 = B.load_batch(bd)
        check("report 后 cases 仍在", len(B.embedded_cases(meta2)) == 3)

        # 同 domain 同 tasks 第二次导入 → 可比（model 相同）
        bd2 = import_tau_results(rp, out_dir=td / "batch2",
                                 batch_id="p0-tau-demo2")
        ok, reasons = B.comparable(B.load_batch(bd), B.load_batch(bd2))
        check("同题同模型两批可比", ok, str(reasons))

        # 换 model → 不可比
        bd3 = import_tau_results(rp, out_dir=td / "batch3",
                                 batch_id="p0-tau-demo3", model="other-model")
        ok3, r3 = B.comparable(B.load_batch(bd), B.load_batch(bd3))
        check("换 model → 不可比", (not ok3) and any("底座模型" in x for x in r3),
              str(r3))
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------- SWE bridge
def test_swe_bridge() -> None:
    section("swe · A 桥接（resolved 导入 · 不重判 · trace=None）")
    import tempfile
    import shutil
    import json as _json

    from evalkit.suites import discover_suites
    from evalkit.suites.swe import (
        SUITE, parse_results, cases_from_records, import_swe_results,
    )

    check("suite id=swe.bench", SUITE.id == "swe.bench")
    check("suite kind=external", SUITE.kind == "external")
    check("suite 指纹非空", bool(SUITE.fingerprint()), str(SUITE.fingerprint()))
    suites = discover_suites()
    check("discover 含 swe.bench", "swe.bench" in suites, str(sorted(suites)))

    # 形状 1：记录列表（resolved bool + reward 部分）
    recs1 = [
        {"instance_id": "repo__a-1", "resolved": True, "repo": "repo"},
        {"instance_id": "repo__a-2", "resolved": False, "repo": "repo"},
        {"instance_id": "repo__a-3", "reward": 0.5},
        {"instance_id": "repo__a-3", "reward": 0.5},  # 多 trial
        {"instance_id": "repo__a-4"},                 # 缺 score
    ]
    cases, counts, tasks = cases_from_records(recs1)
    check("cases 数=4（同 id 聚合）", len(cases) == 4,
          str([c["id"] for c in cases]))
    by = {c["id"]: c for c in cases}
    check("resolved=True → score=1", by["repo__a-1"]["score"] == 1.0)
    check("resolved=False → score=0", by["repo__a-2"]["score"] == 0.0)
    check("reward=0.5 → score=0.5（mean，非满分解率）",
          by["repo__a-3"]["score"] == 0.5, str(by["repo__a-3"]))
    check("a-3 hits=0 rate=0（率值=满分解）",
          by["repo__a-3"]["hits"] == 0 and by["repo__a-3"]["rate"] == 0.0)
    check("缺 score → n=0 collect_failed",
          by["repo__a-4"]["n"] == 0 and by["repo__a-4"]["collect_failed"] == 1,
          str(by["repo__a-4"]))
    check("counts runs=5 valid=4 invalid=1",
          counts["runs"] == 5 and counts["valid"] == 4 and counts["invalid"] == 1,
          str(counts))
    check("题目 hash 不含 resolved", "resolved" not in _json.dumps(tasks))

    # 形状 3：resolved_ids + unresolved_ids
    data3 = {"resolved_ids": ["x-1", "x-2"], "unresolved_ids": ["x-3"],
             "total_ids": 3, "model": "test-model"}
    from evalkit.suites.swe import _as_records
    recs3 = _as_records(data3)
    check("resolved_ids → 3 records", len(recs3) == 3)
    c3, k3, t3 = cases_from_records(recs3)
    by3 = {c["id"]: c for c in c3}
    check("x-1 score=1 / x-3 score=0",
          by3["x-1"]["score"] == 1.0 and by3["x-3"]["score"] == 0.0)

    # 形状 2：{"results": [...]}
    recs2 = _as_records({"results": [{"id": "y-1", "pass": True}]})
    check("results[] 识别", len(recs2) == 1 and recs2[0]["id"] == "y-1")

    # 裸字符串列表应拒绝
    try:
        _as_records(["a", "b"])
        check("裸字符串列表拒绝", False)
    except ValueError:
        check("裸字符串列表拒绝", True)

    td = Path(tempfile.mkdtemp(prefix="evalkit-swe-"))
    try:
        rp = td / "results.json"
        payload = {
            "model": "openrouter/test-model",
            "dataset": "swebench_verified",
            "results": recs1,
        }
        rp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        bd = import_swe_results(rp, out_dir=td / "batch",
                                batch_id="p0-swe-demo")
        from evalkit import batch as B
        meta = B.load_batch(bd)
        check("import → mode=external", B.is_external(meta))
        check("suite id=swe.bench",
              (meta.get("suite") or {}).get("id") == "swe.bench",
              str(meta.get("suite")))
        check("evaluator.suite_fingerprint 存在",
              bool((meta.get("evaluator") or {}).get("suite_fingerprint")))
        check("task_set.sha256 只含题目",
              (meta.get("task_set") or {}).get("sha256", "").startswith("sha256:"))
        check("无 traces/", not (bd / "traces").exists())
        check("cases 内嵌 n=4", len(B.embedded_cases(meta)) == 4)
        check("model 取顶层 meta",
              (meta.get("agent") or {}).get("model") == "openrouter/test-model",
              str(meta.get("agent")))
        check("source.kind=swebench",
              (meta.get("source") or {}).get("kind") == "swebench")

        # trace 全 None（SWE 预留① 在 case 级）
        check("cases 无 trace 字段强制（external 只存 summary）",
              all("trace" not in c or c.get("trace") is None
                  for c in B.embedded_cases(meta)))

        # report 不重判
        from evalkit.cli import main as cli_main
        rc = cli_main(["report", "--batch", str(bd)])
        check("report external 返回 0", rc == 0)
        meta2 = B.load_batch(bd)
        check("report 后 cases 仍在", len(B.embedded_cases(meta2)) == 4)

        # 同题同模型两批可比
        bd2 = import_swe_results(rp, out_dir=td / "batch2",
                                 batch_id="p0-swe-demo2")
        ok, reasons = B.comparable(B.load_batch(bd), B.load_batch(bd2))
        check("同题同模型两批可比", ok, str(reasons))

        # 换 model → 不可比
        bd3 = import_swe_results(rp, out_dir=td / "batch3",
                                 batch_id="p0-swe-demo3", model="other")
        ok3, r3 = B.comparable(B.load_batch(bd), B.load_batch(bd3))
        check("换 model → 不可比", (not ok3) and any("底座模型" in x for x in r3),
              str(r3))

        # CLI import-swe 惰性路径
        rc2 = cli_main(["import-swe", "--results", str(rp),
                        "--out", str(td / "cli-batch"),
                        "--batch-id", "p0-swe-cli"])
        check("CLI import-swe 返回 0", rc2 == 0)
        meta4 = B.load_batch(td / "cli-batch")
        check("CLI 批 mode=external", B.is_external(meta4))

        # 解析错误
        try:
            parse_results(td / "missing.json")
            check("缺文件 FileNotFoundError", False)
        except FileNotFoundError:
            check("缺文件 FileNotFoundError", True)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------- lazy
def test_lazy_imports() -> None:
    section("懒加载 · import evalkit.cli 不载 suites 实现")
    # 注意：本进程可能已因 discover_suites 载入 suites —— 用子进程验证
    import subprocess
    import sys as _sys

    code = (
        "import sys\n"
        "import evalkit.cli\n"
        "bad = [m for m in sys.modules if m.startswith('evalkit.suites.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run([_sys.executable, "-c", code],
                       capture_output=True, text=True, cwd=str(_ROOT))
    check("子进程 import evalkit.cli 零 suites",
          r.returncode == 0 and "OK" in r.stdout,
          r.stderr or r.stdout)

    # outcome/suite 是底座；cli 顶层只 eager import outcome（suite 仅 discover 时加载）
    code2 = (
        "import sys\n"
        "import evalkit.cli\n"
        "assert 'evalkit.outcome' in sys.modules, 'cli should load outcome'\n"
        "assert 'evalkit.suites' not in sys.modules\n"
        "assert not [m for m in sys.modules if m.startswith('evalkit.suites.')]\n"
        "print('OK')\n"
    )
    r2 = subprocess.run([_sys.executable, "-c", code2],
                        capture_output=True, text=True, cwd=str(_ROOT))
    check("子进程 cli 载 outcome 但不载 suites 包",
          r2.returncode == 0, r2.stderr or r2.stdout)

    # suite 可独立加载（底座，零场景实现）
    code3 = (
        "import sys\n"
        "import evalkit.suite\n"
        "assert 'evalkit.suites' not in sys.modules\n"
        "print('OK')\n"
    )
    r3 = subprocess.run([_sys.executable, "-c", code3],
                        capture_output=True, text=True, cwd=str(_ROOT))
    check("子进程 import evalkit.suite 不载 suites 包",
          r3.returncode == 0, r3.stderr or r3.stdout)


if __name__ == "__main__":
    sys.exit(main())


def test_trace_quality_empty_final_assistant() -> None:
    """2026-09-26 实验10 pilot 口径补口：终轮 assistant 存在但无内容 = no_output。

    nemotron-free 实测返回 content=[]，用户一个字没看到；
    旧判据只查 role 存在会把它放行成 ok，检测器随之给出误导 verdict。
    """
    from evalkit.trace_detectors import trace_quality
    from evalkit.trace_schema import NormalizedTrace, Step, ToolCall

    base = [Step(role="user", text="帮我退款"),
            Step(role="assistant", tool_calls=[ToolCall(name="get_order",
                                                        args={"order_id": "8003"}, call_id="c1")]),
            Step(role="tool", tool_call_id="c1", text="ok")]
    # 终轮空 content → no_output
    tr = NormalizedTrace(agent="bare", steps=base + [Step(role="assistant", text="")])
    assert trace_quality(tr)[0] == "no_output"
    # 终轮有正文 → ok
    tr2 = NormalizedTrace(agent="bare", steps=base + [Step(role="assistant", text="已查询")])
    assert trace_quality(tr2)[0] == "ok"
    # 终轮停在 tool_call（max_turns 截断）→ 有行为内容，仍 ok（P9 单列观察处理）
    tr3 = NormalizedTrace(agent="bare", steps=base + [
        Step(role="assistant", tool_calls=[ToolCall(name="process_refund",
                                                    args={"order_id": "8003"}, call_id="c2")])])
    assert trace_quality(tr3)[0] == "ok"
