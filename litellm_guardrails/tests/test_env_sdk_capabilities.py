"""LiteLLM inspects the registered leaf class instead of inherited hooks."""
import litellm
import pytest
from litellm.proxy.utils import ProxyLogging
from litellm_guardrails.env_assignment_masking import RuEnvAssignmentGuardrail


@pytest.mark.parametrize('capability',['pre_call','stream_iterator'])
def test_sdk_discovers_env_guardrail_hooks_without_an_unrelated_base_callback(monkeypatch,capability):
    guard=RuEnvAssignmentGuardrail(dictionary_substitutions_enabled=False)
    monkeypatch.setattr(litellm,'callbacks',[guard])
    caps=ProxyLogging._callback_capabilities()
    if capability=='pre_call':
        assert caps.has_pre_call_override
    else:
        assert caps.has_iterator_override
        assert any(callback is guard and kind=='override' for callback,kind in caps.iterator_overrides)
