import socket

import pytest

from sandbox.gateway import inference_request, public_address


def provider(**changes):
    return {'endpoint': 'https://models.example/v1', 'api_key': 'real-secret', 'model': 'code-model', 'package': '@ai-sdk/openai-compatible', **changes}


def test_gateway_uses_fixed_upstream_and_scopes_model():
    url, headers, body = inference_request(provider(), '/api/chat/completions', {'model': 'code-model', 'messages': []})
    assert url == 'https://models.example/v1/chat/completions'
    assert headers['Authorization'] == 'Bearer real-secret'
    with pytest.raises(ValueError):
        inference_request(provider(), '/api/chat/completions', {'model': 'unapproved-model'})


@pytest.mark.parametrize('path', ['/api/files', '/api/../models', '/api/%2e%2e/chat/completions', '/api//evil.example/chat/completions', 'https://evil.example/api/chat/completions', '/api/organizations'])
def test_gateway_rejects_non_inference_paths(path):
    with pytest.raises(ValueError):
        inference_request(provider(), path, {'model': 'code-model'})


def test_provider_authentication_formats_and_token_parameter():
    _, headers, body = inference_request(provider(package='@ai-sdk/anthropic'), '/api/messages', {'model': 'code-model'})
    assert headers['x-api-key'] == 'real-secret'
    _, headers, _ = inference_request(provider(package='@ai-sdk/google'), '/api/models/code-model:streamGenerateContent?key=task-key&alt=sse', {})
    assert headers['x-goog-api-key'] == 'real-secret'
    url, _, body = inference_request(provider(token_param='max_completion_tokens'), '/api/chat/completions', {'model': 'code-model', 'max_tokens': 10})
    assert body['max_completion_tokens'] == 10
    assert 'max_tokens' not in body


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '172.17.0.1', '192.168.1.1', '::1', 'fc00::1', '0.0.0.0', '::ffff:127.0.0.1'])
def test_egress_proxy_cannot_reach_host_private_networks_or_metadata(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(ValueError, match='public'):
        public_address('attacker.example', 443)


def test_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443)) for address in ['8.8.8.8', '127.0.0.1']])
    with pytest.raises(ValueError):
        public_address('changing.example', 443)
