"""Install the local React card into the pinned, offline Next.js static export.

Only the expanded EvaluationCard is customized. Keep all routing, Auth, React
and GenericGuardrailResponse from the release. Refuse unknown release assets
instead of silently shipping a stock or incompatible dashboard.
"""
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path


def install(out):
    chunk = out / "_next/static/chunks/2cje6va15a1ws.js"
    original = chunk.read_bytes()
    if sha256(original).hexdigest() != "6052f97471baf375d6080941fbf79f8d398f6665083fc4fff4e127052a7c98fa":
        raise ValueError("Unexpected GuardrailViewer bundle; revalidate the LiteLLM version")
    source = original.decode()
    # Insert in the same module scope as the JSX runtime and raw-response component.
    before = "ey=({response:e})=>"
    mask = 'o>0&&(0,s.jsxs)("div",{className:"mt-3",children:[(0,s.jsx)("h5",{className:"text-sm font-medium text-gray-700 mb-2",children:"Masked Entities"})'
    raw = 'u&&!en.has(u)&&p&&(0,s.jsx)(ey,{response:p})'
    card = Path(__file__).with_name("ru_guardrail_card.js").read_text()
    for old, new in [
        (before, f"ruCard=({card}),{before}"),
        (mask, '"ru-llm-proxy"===u?ruCard(e,s,ey):' + mask),
        (raw, '"ru-llm-proxy"!==u&&' + raw),
    ]:
        if source.count(old) != 1:
            raise ValueError("GuardrailViewer integration point is missing or ambiguous")
        source = source.replace(old, new)
    updated = chunk.with_name(f"ru-card-{sha256(source.encode()).hexdigest()[:16]}.js")
    updated.write_text(source)
    # Next references this shared chunk from HTML and flight payloads for
    # Logs, Guardrails Monitor and Tool Policies. Use a new cache key in all.
    for ext in ("*.html", "*.txt"):
        for path in out.rglob(ext):
            data = path.read_bytes()
            if chunk.name.encode() in data:
                path.write_bytes(data.replace(chunk.name.encode(), updated.name.encode()))


if __name__ == "__main__":
    install(Path(find_spec("litellm").origin).parent / "proxy/_experimental/out")
