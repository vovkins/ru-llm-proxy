"""Static checks for production egress-control documentation and templates."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EGRESS_DIR = ROOT / "deploy" / "kubernetes" / "egress"


def test_production_egress_guide_documents_boundaries_and_allowlist():
    guide = (ROOT / "docs" / "egress-controls.md").read_text()

    for required in (
        "Промышленные ограничения исходящих соединений",
        "PRE_EGRESS_POLICY_MODE",
        "FINAL_PAYLOAD_LEAK_CHECK_MODE",
        "make test-egress-security",
        "Локальная bridge-сеть Docker Compose",
        "не доказывает запрет всех исходящих",
        "DEEPPAVLOV_NER_MODEL_URL",
        "api.z.ai",
        "api.openai.com",
        "api.anthropic.com",
        "presidio-analyzer:5001",
        "redis:6379",
        "db:5432",
        "CiliumNetworkPolicy",
        "toFQDNs",
        "matchName",
        "Kubernetes NetworkPolicy",
    ):
        assert required in guide


def test_kubernetes_egress_templates_are_present_and_specific():
    default_deny = (EGRESS_DIR / "default-deny-egress.yaml").read_text()
    internal = (EGRESS_DIR / "internal-dependencies.networkpolicy.yaml").read_text()
    analyzer = (EGRESS_DIR / "analyzer-no-internet-egress.networkpolicy.yaml").read_text()
    cilium = (EGRESS_DIR / "litellm-provider-egress.cilium.yaml").read_text()
    readme = (EGRESS_DIR / "README.md").read_text()

    assert "policyTypes:\n    - Egress" in default_deny
    assert "egress: []" in default_deny

    for required in (
        "app.kubernetes.io/name: litellm",
        "app.kubernetes.io/name: presidio-analyzer",
        "app.kubernetes.io/name: redis",
        "app.kubernetes.io/name: postgres",
        "port: 5001",
        "port: 6379",
        "port: 5432",
    ):
        assert required in internal

    assert "app.kubernetes.io/name: presidio-analyzer" in analyzer
    assert "egress: []" in analyzer

    for required in (
        "apiVersion: cilium.io/v2",
        "kind: CiliumNetworkPolicy",
        "toFQDNs:",
        "matchName: api.z.ai",
        "matchName: api.openai.com",
        "matchName: api.anthropic.com",
        'port: "443"',
        "k8s:k8s-app: kube-dns",
        'port: "53"',
        "matchPattern: \"*\"",
    ):
        assert required in cilium

    assert "docs/egress-controls.md" in readme


def test_primary_docs_link_production_egress_controls():
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/architecture.md": (ROOT / "docs" / "architecture.md").read_text(),
        "docs/monitoring.md": (ROOT / "docs" / "monitoring.md").read_text(),
        "docs/compliance.md": (ROOT / "docs" / "compliance.md").read_text(),
    }

    for path, text in docs.items():
        assert "docs/egress-controls.md" in text, path
        assert "deploy/kubernetes/egress" in text, path


def test_static_suite_runs_production_egress_controls_regression():
    makefile = (ROOT / "Makefile").read_text()

    assert "tests/test_production_egress_controls_docs.py" in makefile
