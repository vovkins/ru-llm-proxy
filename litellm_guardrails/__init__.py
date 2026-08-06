"""LiteLLM guardrails for ru-llm-proxy."""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from litellm_guardrails.pii_guardrail import RuPIIGuardrail

__all__ = ["RuPIIGuardrail"]


def __getattr__(name: str):
    """Preserve the package export without importing the guardrail eagerly."""
    if name != "RuPIIGuardrail":
        raise AttributeError(name)
    from litellm_guardrails.pii_guardrail import RuPIIGuardrail

    return RuPIIGuardrail
