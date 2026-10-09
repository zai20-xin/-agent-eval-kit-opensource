# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Added

- 初始开源版本：`evalkit` 底座（run / report / compare / import-tau / import-swe CLI）
- 场景套件：weather（答案级）、coding（轨迹级 T1–T5）、service（客服）、travel（旅游）与 tau / swe 桥接
- `bare-agent`：223 行确定性裸 agent（评测 demo 的被测对象）
- `experiments/实验01~11`：可复跑实验（脚本 / 任务集 / 结果）与场景设计矩阵
- `steps/01~07`：评测方法演进 POC（字面断言 → LLM judge → 确定性层）
- `tools/layout_gate.py`：落位门禁（pre-commit 强制）
- 文档：架构总览 / ADR / 术语表 / 业务场景评测设计方法论
- 许可：MIT
