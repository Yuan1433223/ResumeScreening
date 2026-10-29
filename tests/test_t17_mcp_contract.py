"""MCP 仍复用 API 的审批身份契约及可选登录，不触发真实网络。"""
import io
import json
import urllib.error

import mcp_server as mcp
from src import config


def test_mcp_approval_forwards_the_reviewed_snapshot(monkeypatch):
    calls = []
    def call(method, path, body):
        calls.append((method, path, body))
        return body
    monkeypatch.setattr(mcp, '_call', call)
    result = mcp.approve('doc', 'approve', 'assessment', 'v1', 'request', reason='synthetic')
    assert result['assessment_id'] == 'assessment'
    assert result['expected_version'] == 'v1'
    assert result['request_id'] == 'request'
    assert calls[0][:2] == ('POST', '/api/documents/doc/approve')


def test_mcp_authenticates_once_after_401(monkeypatch):
    monkeypatch.setattr(config, 'AUTH_ENABLED', True)
    monkeypatch.setattr(config, 'AUTH_USERNAME', 'synthetic_user')
    monkeypatch.setattr(config, 'AUTH_PASSWORD', 'synthetic_password')
    calls = []
    class Opener:
        def open(self, request, data=None, timeout=30):
            calls.append((request.full_url, data))
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO())
            return io.BytesIO(b'{"ok":true}')
    monkeypatch.setattr(mcp, '_OPENER', Opener())
    assert mcp._call('GET', '/api/resumes') == {'ok': True}
    assert calls[0][0] == calls[2][0]
    assert calls[1][0].endswith('/api/auth/login')
    assert json.loads(calls[1][1]) == {'username': 'synthetic_user', 'password': 'synthetic_password'}
