"""LiteLLM guardrails for ru-llm-proxy."""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from litellm_guardrails.pii_guardrail import RuPIIGuardrail

__all__ = ["RuPIIGuardrail", "upstream_stream_adapter"]


def __getattr__(name: str):
    """Preserve the package export without importing the guardrail eagerly."""
    if name == "upstream_stream_adapter":
        # Loading a callback's .py file directly can execute it twice. A package
        # entry point keeps response ownership shared with all guardrail instances.
        from litellm_guardrails.upstream_stream import adapter

        return adapter
    if name != "RuPIIGuardrail":
        raise AttributeError(name)
    from litellm_guardrails.pii_guardrail import RuPIIGuardrail

    return RuPIIGuardrail
