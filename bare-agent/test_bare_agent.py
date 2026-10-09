#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1-A5 · bare_agent 单测（全离线：假 client 注入，不碰网络、不花额度）。

覆盖外壳验收清单：工具假实现 / 参数解析留痕 / 错误不炸 / max_turns 终止 /
messages 续接 / 外壳生效。附 BARE_AGENT 环境不依赖（OPENROUTER_API_KEY 用假值）。

假 client 手法与 `evalkit.adapters.bare` 的冒烟一致：替换模块级 `OpenAI` 符号
（in-process，run_agent 每次调用时才实例化）。
"""
from __future__ import annotations

import copy
import json
import os
import types

import pytest

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import bare_agent as ba  # noqa: E402  （pytest 已把本目录放进 sys.path）


# ───────────────────────── 假 client ─────────────────────────
class _FakeTC:
    def __init__(self, name, args, cid="c1"):
        self.id = cid
        self.function = types.SimpleNamespace(
            name=name, arguments=args if isinstance(args, str) else json.dumps(args))


class _FakeClient:
    """replies 里每项是一次 create 的返回：str → 纯文本收工；list[_FakeTC] → 工具调用。"""

    def __init__(self):
        self.replies = []
        self.seen = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, model, messages, tools, tool_choice):
        self.seen.append(copy.deepcopy(messages))
        if not self.replies:
            raise AssertionError("假 client 的回复用完了（循环轮数超预期）")
        item = self.replies.pop(0)
        if isinstance(item, str):
            msg = types.SimpleNamespace(content=item, tool_calls=None)
            finish = "stop"
        else:
            msg = types.SimpleNamespace(content="", tool_calls=item)
            finish = "tool_calls"
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg, finish_reason=finish)],
            usage=None)


@pytest.fixture
def fake(monkeypatch):
    fc = _FakeClient()
    monkeypatch.setattr(ba, "OpenAI", lambda **kw: fc)
    return fc


# ───────────────────────── 1 · 工具假实现（确定性） ─────────────────────────
def test_weather_tool_deterministic():
    d = json.loads(ba.get_weather("北京"))
    assert (d["weather"], d["temp"], d["rain"]) == ("晴", 32, False)
    assert json.loads(ba.get_weather("火星"))["error"].startswith("unknown city")


def test_tool_execution_in_loop(fake):
    fake.replies = [[_FakeTC("get_weather", {"city": "北京"})], "北京晴，32 度"]
    r = ba.run_agent("北京天气")
    assert r["stats"]["tool_calls"] == 1
    # 第二轮 messages 里：user → assistant(tool_calls) → tool 结果
    second = fake.seen[1]
    assert second[1]["role"] == "assistant"
    assert second[1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert second[2]["role"] == "tool"
    assert json.loads(second[2]["content"])["temp"] == 32
    assert r["answer"] == "北京晴，32 度"


# ───────────────────────── 2 · 参数解析留痕 ─────────────────────────
def test_bad_arguments_leaves_trace(fake):
    fake.replies = [[_FakeTC("get_weather", "{bad json")], "查不了"]
    r = ba.run_agent("北京天气")
    # 轨迹里 assistant 的 toolCall.arguments 带 __parse_error__ 留痕（不静默丢）
    tc_lines = [c for s in r["trace"].lines for c in
                (s.get("message", {}).get("content") or [])
                if isinstance(c, dict) and c.get("type") == "toolCall"]
    assert tc_lines[0]["arguments"]["__parse_error__"] is True
    assert tc_lines[0]["arguments"]["__raw__"] == "{bad json"
    # 解析失败走错误工具结果，循环不炸（注：stats.arg_parse_errors 为历史死分支，
    # 解析失败计入 tool_errors —— 判定不依赖前者，此断言钉住现状防静默变）
    assert r["stats"]["tool_errors"] == 1
    assert r["stats"]["arg_parse_errors"] == 0


# ───────────────────────── 3 · 错误不炸 ─────────────────────────
def test_unknown_tool_error_result(fake):
    fake.replies = [[_FakeTC("no_such_tool", {})], "抱歉"]
    r = ba.run_agent("试试")
    assert r["stats"]["tool_errors"] == 1
    assert "Tool not found" in fake.seen[1][2]["content"]
    assert r["answer"] == "抱歉"          # 错误后仍能收工


def test_tool_exception_does_not_crash(fake):
    def boom(**kw):
        raise RuntimeError("工具内部炸了")
    fake.replies = [[_FakeTC("boom", {})], "没能完成"]
    r = ba.run_agent("炸一个", registry={**ba.REGISTRY, "boom": boom})
    assert r["stats"]["tool_errors"] == 1
    assert "RuntimeError" in fake.seen[1][2]["content"]
    assert r["hit_max_turns"] is False


# ───────────────────────── 4 · max_turns 终止 ─────────────────────────
def test_max_turns_stops_loop(fake):
    # 每轮都要调工具，永远不收工
    fake.replies = [[_FakeTC("get_weather", {"city": "北京"})] for _ in range(5)]
    r = ba.run_agent("一直查", max_turns=3)
    assert r["hit_max_turns"] is True
    assert r["turns"] == 3
    assert r["answer"] == "<max_turns reached>"
    assert len(fake.seen) == 3                # 恰好 3 次调用，不多不少


# ───────────────────────── 5 · messages 续接 ─────────────────────────
def test_messages_continuation_no_side_effect(fake):
    old = [{"role": "system", "content": "S"},
           {"role": "user", "content": "早前问题"},
           {"role": "assistant", "content": "早前回答"}]
    snap = copy.deepcopy(old)
    fake.replies = ["续接回答"]
    r = ba.run_agent(prompt=None, messages=old)
    assert old == snap                         # 调用方列表不被改
    assert fake.seen[0] == old                 # 历史原样入请求
    assert r["answer"] == "续接回答"


def test_messages_with_new_prompt_appends(fake):
    old = [{"role": "user", "content": "早前问题"}]
    fake.replies = ["新回答"]
    ba.run_agent("新问题", messages=old)
    sent = fake.seen[0]
    assert sent == old + [{"role": "user", "content": "新问题"}]


def test_both_none_raises():
    with pytest.raises(ValueError):
        ba.run_agent()


# ───────────────────────── 6 · 外壳生效 ─────────────────────────
def test_system_prompt_injected(fake):
    fake.replies = ["收到"]
    r = ba.run_agent("处理订单", system_prompt="你是客服助手")
    assert fake.seen[0][0] == {"role": "system", "content": "你是客服助手"}
    assert fake.seen[0][1] == {"role": "user", "content": "处理订单"}
    roles = [l["message"]["role"] for l in r["trace"].lines[1:]]
    assert roles == ["system", "user", "assistant"]   # 轨迹留痕


def test_default_run_unchanged(fake):
    """旧调用零变化：无 system、消息就一条 user（A1 兼容性钉子）。"""
    fake.replies = ["ok"]
    r = ba.run_agent("查北京天气")
    assert fake.seen[0] == [{"role": "user", "content": "查北京天气"}]
    assert [l["message"]["role"] for l in r["trace"].lines[1:]] == ["user", "assistant"]
