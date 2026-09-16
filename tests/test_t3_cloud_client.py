# -*- coding: utf-8 -*-
"""CloudModelClient 测试：模拟 HTTP 层（不真正发请求）。

覆盖：成功解析（含 usage）、请求头/URL/请求体正确、429/5xx → 可重试、
401/4xx → 不可重试、网络错误 → 可重试、响应结构异常 → 不可重试。
"""
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.providers.model_extractor import (
    CloudModelClient,
    ModelPermanentError,
    ModelTransientError,
)
from src.schemas import ModelConfig


class FakeResp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


def client(**overrides):
    cfg = dict(
        enabled=True,
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        model="deepseek-chat",
        temperature=0.1,
    )
    cfg.update(overrides)
    return CloudModelClient(ModelConfig(**cfg))


def test_cloud_client_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["req"] = req
        return FakeResp(json.dumps({
            "choices": [{"message": {"content": "模型输出文本"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    resp = client().complete("你好", timeout=5)

    assert resp.text == "模型输出文本"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert resp.duration_s >= 0

    # 请求地址、鉴权头、请求体正确
    assert captured["req"].full_url == "https://api.deepseek.com/chat/completions"
    assert captured["req"].headers["Authorization"] == "Bearer sk-test"
    body = json.loads(captured["req"].data)
    assert body["model"] == "deepseek-chat"
    assert body["messages"][0]["content"] == "你好"


def test_cloud_client_429_transient(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", None, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelTransientError):
        client().complete("你好", timeout=5)


def test_cloud_client_5xx_transient(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", None, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelTransientError):
        client().complete("你好", timeout=5)


def test_cloud_client_401_permanent(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelPermanentError):
        client().complete("你好", timeout=5)


def test_cloud_client_network_error_transient(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelTransientError):
        client().complete("你好", timeout=5)


def test_cloud_client_malformed_response_permanent(monkeypatch):
    def fake_urlopen(req, timeout):
        return FakeResp(json.dumps({"unexpected": True}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelPermanentError):
        client().complete("你好", timeout=5)
