#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Judge —— 只负责确定性代码判不了的部分

三条必须守住的边界（都是踩过的坑）：
    1. 调用失败 ≠ 判 0 分。限流/超时/返回空，一律返回 None，由调用方单独统计。
       否则一次 429 就能让通过率从 100% 掉到 0%（失败模式 C1）。
    2. 裁判要和被测不同厂商，避免自我偏好偏差。
    3. 免费模型限流是常态，必须有 fallback 链 + 退避重试 + JSON 完整性校验。
"""
import json
import os
import re
import time

from openai import OpenAI

DEFAULT_JUDGE_MODELS = [
    "minimax/minimax-m3:free",
    "cohere/north-mini-code:free",
    "inclusionai/ling-3.0-flash-fin:free",
]
MAX_RETRY = 3


def get_client(api_key=None):
    """api_key 为空时从环境读；OpenRouter 兼容 OpenAI 接口"""
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "没读到 OPENROUTER_API_KEY。"
            '先跑：export OPENROUTER_API_KEY="sk-or-..."'
        )
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)


def parse_json_loose(text: str):
    """免费模型爱加 markdown 代码块和废话，宽容解析"""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def judge(client, prompt, required_keys, models=None):
    """
    跑一次 LLM 判定。

    返回 (评分dict | None, 使用的模型, 错误信息 | None)
    None 表示调用失败 —— 绝不当成 0 分。
    """
    models = models or DEFAULT_JUDGE_MODELS
    last_err = None

    for model in models:
        for attempt in range(MAX_RETRY):
            try:
                r = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,      # 裁判要尽量稳定
                    timeout=90,
                )
                j = parse_json_loose(r.choices[0].message.content)
                # 必须拿到全部维度才算一次有效评分，残缺 = 这次调用失败
                if isinstance(j, dict) and all(isinstance(j.get(k), int) for k in required_keys):
                    return j, model, None
                last_err = f"{model} 返回 JSON 不完整"
            except Exception as e:
                last_err = f"{model}: {type(e).__name__}"
                if "RateLimit" in type(e).__name__ or "429" in str(e):
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            time.sleep(1)

    return None, None, last_err
