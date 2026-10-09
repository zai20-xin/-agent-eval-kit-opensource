# 架构总览（Architecture）

> 本文档回答「系统怎么运转」。结构变动时更新；与代码不符时以代码为准并回填。
> 最后更新：2026-10-09（与代码对齐：补齐 service/travel 套件、修正行数等声明）
> **编号 / 黑话看不懂？** 查 [`术语与编号对照.md`](术语与编号对照.md)。

## 1. 系统概览

agent-eval-kit（v0.3.0）是给 Agent 做评测的工具包。核心主张：**评测的难点不在「跑起来」，在「结果敢不敢信」**——最有价值的不是代码，是用变异测试验证过的判定逻辑（每个检测器都拿注入缺陷的样本打过，知道它抓得住什么、抓不住什么）。

四层定位：**① 评测集管理**（声明式任务集 + fixture 落盘）｜**② 运行环境管理**（`adapters/` 自带「怎么跑它」+「怎么读它」）｜**③ 结果管理**（批次目录 schema）｜**④ 全局统计**（`compare`）。

覆盖范围：单步工具调用 + 注入 / 重试 / 多步多轮 / 采样不确定性 + 六 agent 横评 + τ-bench / SWE-bench 外部结果桥接。

## 2. 模块与分层

评测能力按「粒度」分两组（2026-09-21 并层后，不是新旧替代）：

```
【A · 答案级】评单条最终答案（suites/weather/）
确定性层  suites/weather/detectors.py   字段集合比对 / 数值区间 / 工具调用 / 参数 schema
                         零方差 · 零 API · 可单元测试 · 可解释
                         ↓
语义层    judge.py       意图 / 表达 / 逻辑矛盾
                         有方差 · 需校准 · 不可单测
                         ↓
聚合层    aggregate.py   一票否决(CRITICAL) + 总分阈值，可配置
                         （WEATHER_* 策略在 suites/weather/strategies.py）

【B · 轨迹级】评整条 agent 轨迹（新任务优先）
数据契约  trace_schema.py    各家 trace（JSONL / SQLite / 内存）→ NormalizedTrace
                             （铁律：判定器只读它，绝不直接读原始格式）
                         ↓
闸门+shim trace_detectors.py C6 有效性闸门（底座）；detect_* 懒转发 → suites/coding
检测器    suites/coding/     T1~T5 五个检测器
                             T1 用 read / shell-cat / bulk-grep 三通道识别「内容访问」
          suites/service/    客服（service.w1）：新写 cs_* 5 个 + 复用 injection/retry/recall
          suites/travel/     旅游（travel.t1）：新写 travel_* 4 个 + 跨套件复用 cs_page_miss 等

【C · 通用】stats.py   Wilson 置信区间（率值必须带区间才可信）
```

目录树与职责：

```
evalkit/            底座：judge/stats/batch/tasks/cli/trace_schema + adapters/（bare opencode）
                    + suites/（weather/ coding/ service/ travel/ + tau.py swe.py）
bare-agent/         223 行裸 agent（评测 demo 的被测对象；含 11 例离线单测）
steps/              方法演进 POC 01~07（序号有意义，故意保留不删除）
experiments/        可复现实验 01~11（脚本 / 任务集 / 结果）
docs/               交付物层（导航见 docs/README.md）
tools/              layout_gate.py 落位门禁（pre-commit 强制）+ install-hooks.sh
```

## 3. 数据流

```
evalkit run --tasks <任务集.json> --k N --out <批次目录>
    → 驱动 agent → 轨迹随批次落盘（换台机器仍可重判）
    ↓
evalkit report --batch <批次目录>
    → 离线判定（不碰 agent，也不碰它的本机存储）→ verdicts.jsonl / summary.json
    ↓
evalkit compare <批次A> <批次B>
    → 三要素可比性判定 + 两比例 z 检验

A 桥接（外部套件，只读导入）：import-tau / import-swe（不重判、无 traces/）
```

批次目录 schema：`batch.json` / `traces/` / `verdicts.jsonl` / `summary.json`（设计与取舍见本文 §3）。

`agent.model` 建批时未知写 None → 跑完从轨迹实测 modelID 自动回填（跨批可比性的兜底机制之一）。

## 4. 关键设计约定

- **铁律：能用确定性代码判的，绝不用 LLM 判。** 实测依据：`temp` 维度 10 条样本零差异；`field` 维度代码 2:0 完胜（LLM 漏检 1 条、误报 1 条）。演进全过程证据在 `steps/01~07`（字面断言误杀 40% → LLM judge 漏检 80% → 加 few-shot 反而退化 → 确定性层接管 0 漏检 0 误杀）。
- **判定器只读 NormalizedTrace**，绝不直接读原始 trace 格式。
- **两套独立编号，命名空间分离**：`steps/01~07`（POC，不算正式实验）vs `experiments/实验NN`（正式实验），编号互不相干。
- **两层边界**：交付物 `docs/`（会被引用）/ 可复跑 `experiments/`+`steps/`（能重跑）。由 `tools/layout_gate.py` 在 pre-commit 时强制，结构错拒绝提交。
- **脚本间复用不按文件名直接 import**：用 `importlib` + 顶部 `SRC = HERE / "xxx.py"` 常量；改名前跑 `grep -rn 'HERE / "' --include="*.py" .`。
- 聚合策略：一票否决（CRITICAL）+ 总分阈值，可配置（敏感性分析：S2 否决(temp,umbrella,logic) 起漏检归零）。
- 实验归档：结论不再引用的中间轮才可进该实验目录 `archive/`，根层只留最终轮。

## 5. 外部依赖与集成

| 依赖 | 用途 | 约束 |
|---|---|---|
| Python ≥3.12 | 运行时 | 建议用项目自带 `.venv` 安装依赖 |
| openai SDK | judge.py 懒加载调用 LLM | CLI 主路径（report/import-*/compare）不强制联网 |
| OpenRouter | 评测执行默认 LLM 通道 | key 读取：env（`OPENROUTER_API_KEY`），部分脚本降级读 `~/.openrouter_key`；裸脚本只读 `os.environ` |
| 本地 Ollama `127.0.0.1:11434` | 实验07 专用 | 不需要任何 API key |
| opencode CLI | 轨迹来源之一（adapter：`adapters/opencode.py`） | 默认模型会漂移（本机手动切换模型），靠 model 回填 + compare 可比性检查兜底 |
| GitHub Actions（`.github/workflows/ci.yml`） | push/PR 自动跑 | layout_gate 全量 + pytest + 纯脚本 + lazy 硬验收 × Python 3.12/3.13 |
