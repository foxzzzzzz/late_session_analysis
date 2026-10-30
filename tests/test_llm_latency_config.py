"""旁路思考模式和重试配置，不调用真实模型。"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from analysis.llm_client import LLMClient
from orchestration.config import SystemConfig


def response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"summary":"ok","events":[],"conflicts":[],"unknowns":[]}',
        reasoning_content=''), finish_reason='stop')])


@pytest.mark.parametrize('enabled', [False, True])
def test_deepseek_thinking_is_sent_in_extra_body(enabled):
    client = LLMClient(provider='deepseek', model='deepseek-v4-flash', api_key='unit-test',
                       thinking_enabled=enabled)
    with patch('litellm.completion', return_value=response()) as completion:
        assert client.chat('system', 'input', max_retries=0)
    kwargs = completion.call_args.kwargs
    assert kwargs['extra_body'] == {'thinking': {'type': 'enabled' if enabled else 'disabled'}}
    assert kwargs['num_retries'] == 0


@pytest.mark.parametrize('provider,model', [('openai', 'example-model'), ('anthropic', 'example-model')])
def test_other_providers_do_not_receive_deepseek_thinking(provider, model):
    client = LLMClient(provider=provider, model=model, api_key='unit-test', thinking_enabled=False)
    with patch('litellm.completion', return_value=response()) as completion:
        assert client.chat('system', 'input', max_retries=0)
    assert 'extra_body' not in completion.call_args.kwargs


def test_latency_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv('LLM_THINKING_ENABLED', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    config = SystemConfig()
    assert config.llm_thinking_enabled is False
    assert config.llm_max_retries == 0
    monkeypatch.setenv('LLM_THINKING_ENABLED', 'true')
    monkeypatch.setenv('LLM_MAX_RETRIES', '1')
    config = SystemConfig()
    assert config.llm_thinking_enabled is True
    assert config.llm_max_retries == 1
    client = LLMClient(provider='deepseek', model='example', api_key='unit-test')
    assert client.thinking_enabled is True
    assert client.max_retries == 1


def test_default_failure_does_not_retry_or_sleep(monkeypatch):
    monkeypatch.delenv('LLM_THINKING_ENABLED', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    client = LLMClient(provider='deepseek', model='example', api_key='unit-test')
    with patch('litellm.completion', side_effect=TimeoutError('unit timeout')) as completion, \
            patch('analysis.llm_client.time.sleep') as sleep:
        assert client.chat('system', 'input') is None
    assert completion.call_count == 1
    sleep.assert_not_called()


def test_optional_retries_cannot_exceed_remaining_batch_budget():
    client = LLMClient(provider='deepseek', model='example', api_key='unit-test', max_retries=2)
    with patch('litellm.completion', side_effect=TimeoutError('timeout')) as completion, \
            patch('analysis.llm_client.time.monotonic', side_effect=[0, 0.01, 0.02]), \
            patch('analysis.llm_client.time.sleep') as sleep:
        assert client.chat('system', 'input', timeout=0.1) is None
    assert completion.call_count == 1
    sleep.assert_not_called()
