"""Keep the isolated Analyzer and guardrail images on the same wire contract."""
from pathlib import Path


def test_boundary_contract_copies_match():
    root = Path(__file__).resolve().parents[1]
    assert (root / "presidio/boundary_contract.py").read_bytes() == (
        root / "litellm_guardrails/boundary_contract.py"
    ).read_bytes()
