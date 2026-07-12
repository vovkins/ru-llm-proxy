"""Static checks for high-level repository status and client docs."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_status_does_not_list_merged_work_as_pending():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status_section = readme.split("## Статус проекта", 1)[1].split(
        "## Что защищает прокси",
        1,
    )[0]

    assert "В работе / не слито" not in status_section
    assert "PR #22" not in status_section
    assert "PR #23" not in status_section
    assert "#15" not in status_section
    assert "#17" not in status_section
    assert "PR #15" not in status_section
    assert "issue #15" not in status_section
    assert "PR #17" not in status_section
    assert "issue #17" not in status_section
    assert "Reused Redis/httpx guardrail clients" in status_section
    assert "Calibrated Russian recognizer thresholds" in status_section


def test_readme_describes_current_inn_policy():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "до слияния #15/#23" not in readme
    assert "PRESIDIO_ANALYZER_DETECT_BARE_INN_BY_CHECKSUM=true" in readme
    assert "checksum-valid bare ИНН без контекстного слова" in readme
    assert "голый ИНН требует контекст" in readme


def test_readme_status_does_not_overstate_anthropic_messages_coverage():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status_section = readme.split("## Статус проекта", 1)[1].split(
        "## Что защищает прокси",
        1,
    )[0]

    assert "Anthropic-compatible `messages[].content`" not in status_section
    assert "базовых Anthropic Messages `content` string/text blocks" in status_section


def test_claude_code_docs_do_not_overstate_basic_auth_smoke():
    docs = (ROOT / "docs" / "clients" / "claude-code.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    examples = (ROOT / "docs" / "examples.md").read_text(encoding="utf-8")

    assert "полноценный Claude Code gateway пока не считается полностью валидированным" in docs
    assert "Что сейчас валидируется" in docs
    assert "POST /v1/messages?beta=true" in docs
    assert "streaming" in docs
    assert "SSE responses" in docs
    assert "anthropic-version" in docs
    assert "anthropic-beta" in docs
    assert "basic Anthropic Messages auth smoke" in docs
    assert "не проверяет Claude Code `?beta=true` query" in docs
    assert "Добавьте отдельный Claude Code" in docs
    assert "gateway smoke" in docs
    assert "базовые `/v1` protocol smokes" in readme
    assert "Опциональный базовый Anthropic Messages API" in examples
    assert "Полный Claude Code gateway contract строже этого примера" in examples
