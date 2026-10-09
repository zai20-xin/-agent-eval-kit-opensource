# agent-eval-kit

给 Agent 做评测的工具包。**不是又一个评测平台，是「怎么把评测结果做可信」的一套实现。**

> **覆盖范围**：单步工具调用 + 注入 / 重试 / 多步多轮 / 采样不确定性 + **六 agent 横评**

### ★ 文档总览

| 想知道 | 去哪看 |
|---|---|
| 架构（分层 / 数据流 / 依赖） | [`docs/architecture.md`](docs/architecture.md) |
| 关键决策（ADR） | [`docs/decisions/`](docs/decisions/) |
| 业务场景评测怎么设计（7 步流程 + 指标规范 + 矩阵模板） | [`docs/theory/业务场景评测设计方法论.md`](docs/theory/业务场景评测设计方法论.md) |
| 术语 / 编号看不懂 | [`docs/术语与编号对照.md`](docs/术语与编号对照.md) |
| 新增产出放哪（落位/命名规则） | 本文件第一节（由 `tools/layout_gate.py` 门禁强制） |
| 变更历史 | [`CHANGELOG.md`](CHANGELOG.md) |
| 全部文档清单 | [`docs/README.md`](docs/README.md) |

---

## ★ 一、新增产出该放哪（**写新东西之前先看这张表**）

这个项目会持续加新东西（新场景套件、新检测器、新报告……）。**没有落位规则，顶层必然越堆越乱** —— 下表就是规则。

### 三层边界（先记住这个，再看表）

| 层 | 判据（一句话） | 位置 |
|---|---|---|
| **交付物层** | 会被**引用**的（写进清单/报告） | `docs/` |
| **可复跑层** | 能**重跑**出结果的（实验/脚本） | `experiments/` `steps/` |

### 落位决策表

| 你要新增的东西 | 放哪 | 硬要求 |
|---|---|---|
| 一次新的受控复现实验 | `experiments/实验NN-短名/` | 脚本 + 任务集 + raw/summary 结果同批入库 |
| 某实验迭代 v1/v2/v3 的中间轮 | 该实验目录下 `archive/` | **结论不再引用**的轮次才可归档；根层只留最终轮 |
| 判定器/方法的新一版演进脚本 | `steps/NN_短名.py` | 序号**递增且全局唯一**；单跑 POC |
| 交付物（清单 / 策略 / 结论表） | `docs/` | 长期有效 → 带版本号（`-v1` / `-v2`） |
| 原理 / 设计方法论 | `docs/theory/` | 必须区分【实测】与【推断·待验证】 |
| 被 `import` 的代码模块 | `evalkit/` | 单一职责 |
| 跑批产物 json（`raw_*` / `summary_*`） | 各实验目录内 | 与脚本**同批入库**；`__pycache__` 等缓存已被挡，不要入库 |
| 控制台日志 `.log` | **不落盘** | 内容与 `summary_*.json` 重复，不要入库 |
| 门禁 / 校验 / 安装脚本 | `tools/` | 自包含零依赖，能独立跑；不 import 项目业务代码 |

> **口诀**：会被**引用**的进 `docs/`；能**重跑**的进 `experiments/`。

### 上面这些规则由门禁强制，不靠记

`tools/layout_gate.py` 在每次 `git commit` 时自动跑（pre-commit hook），**结构错直接拒绝提交**。

- 装 hook：`sh tools/install-hooks.sh` —— `.git/hooks/` **不被 git 跟踪**，换机器 / 重新 clone 后要重装一次
  （hook 静默消失是这个机制唯一的死穴：提交照常成功，只是不再检查）
- 手工全量体检：`python3 tools/layout_gate.py`（不带 `--staged` = 查全库技术债）
- 分级原则：**只拦「确定是错的」**（脚本重名 / 编译失败 / 硬引用断链 /
  `experiments/` 脚本带序号前缀 / 顶层越界 / 缓存日志入库）；
  「可能有问题」的一律只告警，且**只报你这次改动碰到的东西**。
- **为什么不全硬拦**：误拦会让人养成 `git commit --no-verify` 的习惯，机制随即全面失效 ——
  所以只有确定错了的才 FAIL，可能有问题的一律 WARN。

### 命名规则（脚本）

| 位置 | 规则 | 例子 |
|---|---|---|
| `steps/` | **保留序号** —— 序号本身表达演进顺序，是有效信息 | `07_temp_by_code.py` |
| `experiments/` | **不带序号，纯语义名** —— 目录已限定范围，序号只会制造「看起来有序其实无序」+ 跨目录重号 | `injection_lab.py` |
| 版本迭代 | `_v1` / `_v2` 后缀 | `retry_storm_v1.py` |
| 脚本间复用 | **不要按文件名直接 import**。用 `importlib` + 顶部常量（如 `SRC = HERE / "xxx.py"`），并在注释里写明 —— 目前有 3 处，**改名时必须同步** | `SRC = HERE / "retry_storm_v2.py"` |

> **改名前的检查命令**：`grep -rn 'HERE / "' --include="*.py" .` —— 避免漏改硬引用（2026-09-10 重组时就是靠它抓到一条 `calibrate.py` 里的漏网引用）。

---

## 二、核心主张

Agent 评测的难点不在「跑起来」，在「结果敢不敢信」。这套工具包里最有价值的不是代码，
是**用变异测试验证过的判定逻辑**——每个检测器都拿注入缺陷的样本打过，知道它抓得住什么、抓不住什么（变异测试＝给正确样本人为注入一处缺陷，验证**检测器**抓不抓得住）。

### 分层架构（按「粒度」分两组）

两组评的是**不同粒度**：用哪组取决于你评「单条答案」还是「整条轨迹」。

| 组 | 评什么 | 链路 |
|---|---|---|
| **A · 答案级**（`suites/weather/`） | 单条最终答案 | 确定性层 `detectors.py` → 语义层 `judge.py` → 聚合层 `aggregate.py` |
| **B · 轨迹级**（新任务优先） | 整条 agent 轨迹 | 数据契约 `trace_schema.py`（→ NormalizedTrace）→ C6 闸门 `trace_detectors.py` → 检测器 `suites/coding/` T1~T5 · `suites/service/`（cs_*）· `suites/travel/`（travel_*） |
| **C · 通用** | 率值可信 | `stats.py` Wilson 置信区间（率值必须带区间才可信） |

> 完整分层图（含各层特性：零方差 / 零 API / 可单测等）、数据流、批次 schema、外部依赖清单
> 已下沉到 [`docs/architecture.md`](docs/architecture.md)。

**铁律：能用确定性代码判的，绝不用 LLM 判。**

这不是偏好，是实测结论——把 `field` 和 `temp` 从 LLM 手里拿回来后：

| 维度 | 交给代码后 vs LLM 判定 |
|---|---|
| `temp` | **10 条样本零差异**（LLM 在该维度毫无优势） |
| `field` | **代码 2:0 完胜**（LLM 漏检 1 条、误报 1 条） |

---

## 三、效果（实测，非估算）

同一批样本（5 条正确回答 + 5 条注入缺陷的变异体）：

| 阶段 | 漏检率 | 误杀率 |
|---|---|---|
| 字面断言 `assert "不需要带伞" in answer` | — | **40%** |
| v1 LLM judge（3 标准 + 阈值≥2） | **80%** | 0% |
| v5 确定性层接管后 | **0%** | **0%** |

聚合策略敏感性分析（同一批评分离线重算，零额外 API）：

```
S0 纯阈值≥3                    漏 4
S1 否决(temp,logic)            漏 1
S2 否决(temp,umbrella,logic)   漏 0  ✓
S3 否决(+field)                漏 0  ✓
S4 全否决+阈值4                漏 0  ✓
```

---

## 四、跑起来

```bash
git clone <repo-url> && cd agent-eval-kit
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
VP=.venv/bin/python3

# ★ API key：通过环境变量提供（scripts 只读 os.environ）
export OPENROUTER_API_KEY="sk-or-..."
$VP -m evalkit.suites.weather.run_eval
```

### 评测器 CLI（2026-09-21 起；packaging 2026-09-24）

```bash
# 安装（可编辑；装完有 `evalkit` 命令，也可继续用 python -m evalkit）
.venv/bin/pip install -e .
evalkit --help

# 采集：驱动 agent，轨迹**随批次落盘**（换台机器仍可重判）
evalkit run --tasks <任务集.json> --k 30 --out <批次目录>

# 判定：读批次目录出报告（离线 —— 不碰 agent，也不碰它的本机存储）
evalkit report --batch <批次目录>

# 对比：并排两个批次（含三要素可比性判定 + 两比例 z 检验）
evalkit compare <批次A> <批次B>

# A 桥接：外部套件跑完后**只读导入**结果（不重判、无 traces/）
evalkit import-tau  --results <results.json> --domain <domain> [--out ...]
evalkit import-swe  --results <results.json|jsonl|目录> [--model ...]

# 自测（双模式：pytest 或纯脚本）
.venv/bin/pip install -e ".[dev]"
.venv/bin/python3 -m pytest            # pytest
.venv/bin/python3 -m evalkit.tests     # 纯脚本（无 pytest 也可）

# CI（.github/workflows/ci.yml）：push/PR 自动跑
# layout_gate 全量 + pytest + 纯脚本 + lazy 硬验收 × Python 3.12/3.13
# 本地无 remote 时 CI 不会跑；推 GitHub 后生效
```

对应的四层定位：**① 评测集管理**（声明式任务集 + fixture 落盘）｜
**② 运行环境管理**（`adapters/` 自带「怎么跑它」+「怎么读它」，加 agent 只加一个文件）｜
**③ 结果管理**（批次目录 schema：`batch.json` / `traces/` / `verdicts.jsonl` / `summary.json`）｜
**④ 全局统计**（`compare`）。设计与取舍见 `docs/architecture.md`。

> 实验 07 走**本地 Ollama**（`127.0.0.1:11434`），不需要任何 API key。

---

## 五、目录

```
README.md                   ★ 本文件：落位规则 + 命名规则 + 快速开始
CHANGELOG.md                变更历史（Keep a Changelog）
LICENSE                     MIT

docs/                       ◆ 交付物层（设计与架构）
  ├─ README.md                  文档导航
  ├─ architecture.md            架构总览（分层 / 数据流 / 目录职责 / 依赖）
  ├─ 术语与编号对照.md           编号 / 术语释义（撞车编号消歧）
  ├─ decisions/                 ADR 体系（template + 索引 + 001/002）
  └─ theory/
      └─ 业务场景评测设计方法论.md  7 步设计流程 + 指标定义规范 + 路径-测试点矩阵模板

evalkit/                    ◆ 可复跑层：底座（judge/stats/batch/tasks/cli/schema + adapters/）
  └─ suites/                场景套件：weather/（答案级）· coding/（T1–T5 轨迹级）
                            · service/（客服 cs_*）· travel/（旅游 travel_*）
                            + tau.py / swe.py（桥接：外部结果只读导入）
                            统一入口：$VP -m evalkit.suites.weather.run_eval

steps/                      ◆ 可复跑层：方法演进 POC（**序号有意义**，故意保留、不删除）
  └─ 01~07                  评测方法演进：字面断言 → LLM judge → 确定性层

experiments/                ◆ 可复跑层：可复跑实验（脚本 / 任务集 / 结果）
  ├─ 实验01-间接PromptInjection/    注入（间接 + 直接）
  ├─ 实验02-retry_storm/            重试风暴 / 翻页遗漏
  ├─ 实验03-采样参数与pass^k/       字面一致率 vs 语义正确率、pass^k
  ├─ 实验04-参数错误率/             参数值错误
  ├─ 实验05-多步多轮任务/           多步召回 / 难度校准器
  ├─ 实验06-工具调用遗漏/           工具调用遗漏
  ├─ 实验07-temp0不确定性来源/      temp=0 的不确定性来源
  ├─ 实验08-6agent横评/             六 agent 横评（tasks/ + harness/ + results/）
  ├─ 实验09-τ桥接/                  τ-bench 只读桥接
  ├─ 实验10-客服场景/               智能客服（tasks/ + 处理路径-测试点矩阵 + 指标表 + results/）
  └─ 实验11-旅游场景/               智能旅游（同上结构）

bare-agent/                 223 行裸 agent（评测 demo 的被测对象）
  └─ traces/                原始轨迹样例
```

**两套独立编号，命名空间已分离，不要混用：**

| 轴 | 位置 | 性质 | 是否「实验」 |
|---|---|---|---|
| 脚本演进序号 `steps/01~07` | `steps/` | 探索「怎么评测」的开发日志 / POC，单跑、无归档 stdout | **不算正式实验** |
| 复现实验编号 `实验NN` | `experiments/` | 提出假设→受控构造→重复跑→归档的实验 | **是正式实验** |

> `steps/01~07` 是「把评测做可信」这件事的演进证据，它们演示了失败模式但不是受控实验
> （没提前写假设、单跑、原始 stdout 多数未留存）。两者编号互不相干，避免混淆。

`steps/` 里那 7 个脚本是**故意保留的**，它们记录了完整的演进过程：
从字面断言失效，到 LLM judge 漏检 80%，到加 few-shot 反而退化，再到确定性层接管。
**每个版本的失败都留在那儿，不粉饰。**

---

## 六、已知不足

- **轨迹级**：已覆盖 T1~T5（带 Wilson CI），但**没有 OTel instrumentation**
- **答案级**：只覆盖「单步天气查询」一种场景；judge 只用 1 个模型，未跨模型对比，样本仅 10 条，统计上不显著
- 无 judge 校准（agreement / IAA）、无 CI 门禁、无 drift 检测
- **答案级的「0 漏检 0 误杀」很可能过拟合到这 10 条样本**，扩大样本后大概率会退化
- 评测器骨架已能一条命令跑（`evalkit run` / `report` / `compare`），但**尚未上量**：
  K≥10 率值未入台账；套件已覆盖 weather/coding/service/travel，但后两者同样未上量

---

## 七、三条踩过的坑（写在代码注释里）

1. **调用失败 ≠ 判 0 分**。一次 429 限流就能让通过率从 100% 掉到 0%，结论整个翻转。
2. **聚合规则放过已识别的缺陷**。judge 判了 0 分、理由写明原因，阈值一松照样通过。
3. **加 few-shot 会收窄判定边界**。v3 加了示例后，一个原本能抓住的缺陷变成了满分漏检。

---

## 附：版本管理

```bash
git log --oneline          # 看变更历史
git status                 # 确认工作区干净
```
