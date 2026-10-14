# agent-eval-kit

English | [简体中文](README.md)

An evaluation kit for AI agents. **Not another evaluation platform — an implementation of how to make evaluation results trustworthy.**

> **Coverage**: single-step tool calls + injection / retry / multi-step multi-turn / sampling uncertainty + **a six-agent bake-off**

### ★ Documentation Map

| If you want | Go to |
|---|---|
| Architecture (layers / data flow / dependencies) | [`docs/architecture.md`](docs/architecture.md) |
| Key decisions (ADRs) | [`docs/decisions/`](docs/decisions/) |
| How to design evals for business scenarios (7-step process + metric spec + matrix templates) | [`docs/theory/业务场景评测设计方法论.md`](docs/theory/业务场景评测设计方法论.md) (Chinese) |
| Glossary / numbering | [`docs/术语与编号对照.md`](docs/术语与编号对照.md) (Chinese) |
| Where new artifacts go (placement/naming rules) | Section 1 below (enforced by `tools/layout_gate.py`) |
| Change history | [`CHANGELOG.md`](CHANGELOG.md) |
| Full doc index | [`docs/README.md`](docs/README.md) |

---

## ★ 1. Where New Artifacts Go (**read this table before writing anything**)

This project keeps adding new things (new scenario suites, new detectors, new reports...). **Without placement rules, the top level inevitably turns into a junk drawer** — this table is the rule.

### Three-layer boundary (memorize this first)

| Layer | Test (one sentence) | Location |
|---|---|---|
| **Deliverables** | Will be **referenced** (cited in lists/reports) | `docs/` |
| **Reproducible** | Can be **re-run** to reproduce results | `experiments/` `steps/` |

### Placement decision table

| What you're adding | Where | Hard requirement |
|---|---|---|
| A new controlled reproduction experiment | `experiments/实验NN-短名/` | Script + task set + raw/summary results committed in the same batch |
| Intermediate rounds of an experiment iteration (v1/v2/v3) | `archive/` under that experiment | Archive only rounds whose **conclusions are no longer cited**; keep only the final round at the root |
| A new version of a detector/method script | `steps/NN_短名.py` | Sequence numbers **increase and are globally unique**; single-run POCs |
| Deliverables (lists / policies / conclusion tables) | `docs/` | Long-lived → carry a version suffix (`-v1` / `-v2`) |
| Principles / design methodology | `docs/theory/` | Must distinguish [measured] vs [inferred · unverified] |
| Code modules that get `import`ed | `evalkit/` | Single responsibility |
| Batch output json (`raw_*` / `summary_*`) | Inside each experiment directory | Committed **in the same batch as the scripts**; `__pycache__` and other caches are already blocked — don't commit them |
| Console logs `.log` | **Not persisted** | Content duplicates `summary_*.json` — don't commit |

> **Rule of thumb**: things that will be **referenced** go to `docs/`; things that can be **re-run** go to `experiments/`.

### These rules are enforced by a gate, not by memory

`tools/layout_gate.py` runs automatically on every `git commit` (pre-commit hook) and **rejects structural mistakes outright**.

- Install the hook: `sh tools/install-hooks.sh` — `.git/hooks/` is **not tracked by git**, so re-install once per machine / fresh clone
  (silent hook disappearance is this mechanism's only weak spot: commits still succeed, just unchecked)
- Full manual check: `python3 tools/layout_gate.py` (without `--staged` = audits the whole repo's tech debt)
- Tiering principle: **only block "definitely wrong"** (duplicate script names / compile failures / broken hard references /
  numbered prefixes in `experiments/` scripts / top-level violations / caches and logs committed);
  "possibly wrong" items only warn, and **only about what this change touched**.
- **Why not block everything hard**: false blocks train people into the `git commit --no-verify` habit and the mechanism dies —
  so only definite errors FAIL; possible issues WARN.

### Naming rules (scripts)

| Location | Rule | Example |
|---|---|---|
| `steps/` | **Keep sequence numbers** — the number itself expresses evolution order, which is real information | `07_temp_by_code.py` |
| `experiments/` | **No numbers, pure semantic names** — the directory already scopes it; numbers only create fake order + cross-directory collisions | `injection_lab.py` |
| Version iterations | `_v1` / `_v2` suffixes | `retry_storm_v1.py` |
| Cross-script reuse | **Never import by filename directly**. Use `importlib` + a top constant (e.g. `SRC = HERE / "xxx.py"`), noted in comments — currently 3 places; **must be synced when renaming** | `SRC = HERE / "retry_storm_v2.py"` |

> **Check command before renaming**: `grep -rn 'HERE / "' --include="*.py" .` — avoids missed hard references.

---

## 2. Core Thesis

The hard part of agent evaluation is not "getting it to run" — it's "daring to trust the results". The most valuable thing in this kit is not the code; it's the **mutation-test-verified verdict logic** — every detector has been fired at defect-injected samples and knows what it catches and what it misses (mutation testing = inject one defect into a correct sample, then verify whether the **detector** catches it).

### Layered architecture (two groups by "granularity")

The two groups evaluate **different granularities**: which one to use depends on whether you're judging a "single answer" or a "whole trajectory".

| Group | What it evaluates | Chain |
|---|---|---|
| **A · Answer-level** (`suites/weather/`) | A single final answer | Deterministic layer `detectors.py` → semantic layer `judge.py` → aggregation layer `aggregate.py` |
| **B · Trajectory-level** (priority for new tasks) | An entire agent trajectory | Data contract `trace_schema.py` (→ NormalizedTrace) → C6 gate `trace_detectors.py` → detectors `suites/coding/` T1~T5 · `suites/service/` (cs_*) · `suites/travel/` (travel_*) |
| **C · General** | Trustworthy rates | `stats.py` Wilson confidence intervals (a rate is only trustworthy with an interval) |

> The full layering diagram (per-layer traits: zero variance / zero API / unit-testable, etc.), data flow, batch schema, and external dependency list
> live in [`docs/architecture.md`](docs/architecture.md).

**Iron rule: whatever deterministic code can judge, never let an LLM judge.**

This is not a preference — it's a measured result. After taking `field` and `temp` back from the LLM:

| Dimension | Code vs LLM judging |
|---|---|
| `temp` | **Zero difference on 10 samples** (the LLM had no edge at all) |
| `field` | **Code wins 2:0** (the LLM missed 1 and false-alarmed 1) |

---

## 3. Results (Measured, Not Estimated)

Same sample batch (5 correct answers + 5 defect-injected mutants):

| Stage | Miss rate | False-kill rate |
|---|---|---|
| Literal assertion `assert "不需要带伞" in answer` | — | **40%** |
| v1 LLM judge (3 criteria + threshold ≥ 2) | **80%** | 0% |
| v5 after deterministic layer takes over | **0%** | **0%** |

Aggregation-strategy sensitivity analysis (offline recalc on the same verdicts, zero extra API):

```
S0 pure threshold ≥ 3                miss 4
S1 veto (temp, logic)                miss 1
S2 veto (temp, umbrella, logic)      miss 0  ✓
S3 veto (+ field)                    miss 0  ✓
S4 all vetoes + threshold 4          miss 0  ✓
```

---

## 4. Running It

```bash
git clone <repo-url> && cd agent-eval-kit
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
VP=.venv/bin/python3

# ★ API key: via environment variable (scripts only read os.environ)
export OPENROUTER_API_KEY="sk-or-..."
$VP -m evalkit.suites.weather.run_eval
```

### Evaluator CLI (since 2026-09-21; packaged 2026-09-24)

```bash
# Install (editable; provides the `evalkit` command, or keep using python -m evalkit)
.venv/bin/pip install -e .
evalkit --help

# Collect: drive the agent; traces are persisted per batch (re-judgeable on another machine)
evalkit run --tasks <tasks.json> --k 30 --out <batch-dir>

# Judge: read a batch directory and produce a report (offline — touches neither the agent nor its local storage)
evalkit report --batch <batch-dir>

# Compare: side-by-side two batches (with three-factor comparability check + two-proportion z test)
evalkit compare <batch-A> <batch-B>

# A-bridging: read-only import of external suite results (no re-judging, no traces/)
evalkit import-tau  --results <results.json> --domain <domain> [--out ...]
evalkit import-swe  --results <results.json|jsonl|dir> [--model ...]

# Self-test (dual mode: pytest or pure script)
.venv/bin/pip install -e ".[dev]"
.venv/bin/python3 -m pytest            # pytest
.venv/bin/python3 -m evalkit.tests     # pure script (works without pytest)

# CI (.github/workflows/ci.yml): runs automatically on push/PR
# layout_gate full + pytest + pure script + lazy hard acceptance × Python 3.12/3.13
```

The corresponding four-layer positioning: **① eval-set management** (declarative task sets + persisted fixtures) |
**② runtime management** (`adapters/` ships "how to run it" + "how to read it" — adding an agent adds one file) |
**③ result management** (batch directory schema: `batch.json` / `traces/` / `verdicts.jsonl` / `summary.json`) |
**④ global statistics** (`compare`). Design and trade-offs in `docs/architecture.md`.

> Experiment 07 runs on **local Ollama** (`127.0.0.1:11434`) — no API key of any kind.

---

## 5. Directory

```
README.md                   ★ this file: placement rules + naming rules + quick start
CHANGELOG.md                change history (Keep a Changelog)
LICENSE                     MIT

docs/                       ◆ deliverables layer (design & architecture)
  ├─ README.md                  doc navigation
  ├─ architecture.md            architecture overview (layers / data flow / directory duties / dependencies)
  ├─ 术语与编号对照.md           glossary / numbering (Chinese)
  ├─ decisions/                 ADR system (template + index + 001/002)
  └─ theory/
      └─ 业务场景评测设计方法论.md  7-step design process + metric spec + path-testpoint matrix (Chinese)

evalkit/                    ◆ reproducible layer: base (judge/stats/batch/tasks/cli/schema + adapters/)
  └─ suites/                scenario suites: weather/ (answer-level) · coding/ (T1–T5 trajectory-level)
                            · service/ (support cs_*) · travel/ (travel_*)
                            + tau.py / swe.py (bridges: read-only import of external results)
                            unified entry: $VP -m evalkit.suites.weather.run_eval

steps/                      ◆ reproducible layer: method-evolution POCs (**numbers are meaningful**, kept on purpose, never deleted)
  └─ 01~07                  evaluation-method evolution: literal assertion → LLM judge → deterministic layer

experiments/                ◆ reproducible layer: reproducible experiments (scripts / task sets / results)
  ├─ 实验01-间接PromptInjection/    injection (indirect + direct)
  ├─ 实验02-retry_storm/            retry storms / pagination misses
  ├─ 实验03-采样参数与pass^k/       literal match rate vs semantic correctness, pass^k
  ├─ 实验04-参数错误率/             parameter value errors
  ├─ 实验05-多步多轮任务/           multi-step recall / difficulty calibrator
  ├─ 实验06-工具调用遗漏/           missed tool calls
  ├─ 实验07-temp0不确定性来源/      sources of uncertainty at temp=0
  ├─ 实验08-6agent横评/             six-agent bake-off (tasks/ + harness/ + results/)
  ├─ 实验09-τ桥接/                  τ-bench read-only bridge
  ├─ 实验10-客服场景/               customer support (tasks/ + path-testpoint matrix + metrics + results/)
  └─ 实验11-旅游场景/               travel planning (same structure)
```

**Two independent numbering axes, namespaces kept separate — do not mix:**

| Axis | Location | Nature | A formal "experiment"? |
|---|---|---|---|
| Script evolution numbers `steps/01~07` | `steps/` | Dev logs / POCs exploring "how to evaluate"; single-run, no archived stdout | **No** |
| Reproduction experiment numbers `实验NN` | `experiments/` | Hypothesis → controlled construction → repeated runs → archive | **Yes** |

> `steps/01~07` are the evolution evidence of "making evaluation trustworthy"; they demonstrate failure modes but are not controlled experiments
> (no pre-written hypotheses, single runs, most raw stdout not preserved). The two numbering systems are unrelated by design.

The 7 scripts in `steps/` are **kept on purpose** — they record the full evolution:
literal assertions failing, the LLM judge missing 80%, few-shot making it worse, and finally the deterministic layer taking over.
**Every version's failure stays there, unembellished.**

---

## 6. Known Limitations

- **Trajectory-level**: T1~T5 covered (with Wilson CIs), but **no OTel instrumentation**
- **Answer-level**: only the "single-step weather query" scenario; the judge uses 1 model with no cross-model comparison, only 10 samples — statistically insignificant
- No judge calibration (agreement / IAA), no CI gating, no drift detection
- **The answer-level "0 misses 0 false-kills" is very likely overfit to those 10 samples** and will probably degrade on a larger sample
- The evaluator skeleton runs end-to-end with one command (`evalkit run` / `report` / `compare`), but **hasn't been scaled yet**:
  K ≥ 10 rates aren't in the ledger; suites cover weather/coding/service/travel, but the latter two are likewise not scaled

---

## 7. Three Hard-Won Lessons (Also in Code Comments)

1. **A call failure ≠ a zero score**. One 429 rate limit can flip a pass rate from 100% to 0% — and the conclusion with it.
2. **Aggregation rules can pass known defects**. The judge scored 0 with a written reason; loosen the threshold and it passes anyway.
3. **Adding few-shot narrows the verdict boundary**. In v3, adding examples turned a catchable defect into a perfect-score miss.

---

## Appendix: Version Management

```bash
git log --oneline          # change history
git status                 # confirm a clean worktree
```
