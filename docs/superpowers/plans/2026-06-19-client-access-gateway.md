# Client Access Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class client access for Codex, Claude Code, OpenCode, and Kilo Code through a multi-provider LiteLLM gateway.

**Architecture:** LiteLLM remains the only public LLM boundary. Server-funded requests authenticate to the proxy with LiteLLM virtual keys and use upstream provider keys stored on the proxy. Subscription/BYOK passthrough requests authenticate to LiteLLM with `x-litellm-api-key` and reserve provider auth headers such as `Authorization` or `x-api-key` for Codex/Claude upstream auth. The repo documents and smoke-tests OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages API surfaces.

**Tech Stack:** LiteLLM proxy YAML, Bash helper scripts, Makefile targets, curl/jq live smokes, Markdown documentation.

---

## Files

- Modify `litellm-config.yaml`: add provider-prefixed model aliases for Z.AI, OpenAI, and Anthropic; enable header forwarding required for subscription/BYOK passthrough.
- Create `scripts/create_virtual_key.sh`: admin helper for `/key/generate`.
- Create `tests/e2e/test_client_auth.sh`: live auth/protocol smoke tests.
- Modify `Makefile`: add `virtual-key-create` and `client-auth-smoke`; keep existing admin diagnostics on master key.
- Create `docs/clients/codex.md`: Codex CLI/App local task setup.
- Create `docs/clients/claude-code.md`: Claude Code setup.
- Create `docs/clients/opencode.md`: OpenCode CLI/Desktop setup.
- Create `docs/clients/kilo-code.md`: Kilo Code VS Code/CLI setup.
- Create `docs/clients/jwt.md`: JWT/OIDC proxy auth deployment guidance.
- Modify `README.md` and `docs/examples.md`: make virtual keys the user-facing examples.

### Task 1: Multi-Provider LiteLLM Config

**Files:**
- Modify: `litellm-config.yaml`

- [ ] **Step 1: Expand model aliases**

Replace the single `model_list` entry with provider-prefixed aliases while preserving legacy `glm-5.1`:

```yaml
model_list:
  - model_name: zai-glm-5.1
    litellm_params:
      model: openai/glm-5.1
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      access_groups: ["zai", "standard"]
  - model_name: glm-5.1
    litellm_params:
      model: openai/glm-5.1
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      access_groups: ["zai", "standard"]
  - model_name: openai-gpt-5.4-mini
    litellm_params:
      model: openai/gpt-5.4-mini
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      access_groups: ["openai", "standard"]
  - model_name: openai-gpt-5.5
    litellm_params:
      model: openai/gpt-5.5
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      access_groups: ["openai", "premium"]
  - model_name: claude-opus-4.8
    litellm_params:
      model: anthropic/claude-opus-4-8
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      access_groups: ["anthropic", "premium"]
  - model_name: claude-sonnet-4.6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      access_groups: ["anthropic", "standard"]
  - model_name: claude-haiku-4.5
    litellm_params:
      model: anthropic/claude-haiku-4-5
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      access_groups: ["anthropic", "standard"]
```

- [ ] **Step 2: Validate YAML**

Run:

```bash
python3 - <<'PY'
import yaml
from pathlib import Path
yaml.safe_load(Path("litellm-config.yaml").read_text())
print("litellm-config.yaml ok")
PY
```

Expected: `litellm-config.yaml ok`.

- [ ] **Step 3: Commit**

```bash
git add litellm-config.yaml
git commit -m "config: expose client-facing model aliases"
```

### Task 2: Virtual Key Helper And Smoke Script

**Files:**
- Create: `scripts/create_virtual_key.sh`
- Create: `tests/e2e/test_client_auth.sh`
- Modify: `Makefile`

- [ ] **Step 1: Add `scripts/create_virtual_key.sh`**

The script must load `.env`, require `LITELLM_MASTER_KEY`, call `/key/generate`, and print only the generated client key plus non-secret metadata.

- [ ] **Step 2: Add `tests/e2e/test_client_auth.sh`**

The smoke script must check:

- no token rejected on `/v1/chat/completions`;
- invalid token rejected on `/v1/chat/completions`;
- virtual key works on `/v1/chat/completions`;
- virtual key works on `/v1/responses`;
- virtual key works on `/v1/messages`;
- restricted key cannot call a disallowed model.

The script must skip provider-specific protocol calls with a clear message if the needed env key is not configured.

- [ ] **Step 3: Add Make targets**

Add targets:

```make
virtual-key-create:
	bash scripts/create_virtual_key.sh

client-auth-smoke:
	bash tests/e2e/test_client_auth.sh
```

Update `.PHONY` and `help`.

- [ ] **Step 4: Verify shell syntax**

Run:

```bash
bash -n scripts/create_virtual_key.sh
bash -n tests/e2e/test_client_auth.sh
make -n virtual-key-create
make -n client-auth-smoke
```

Expected: all commands complete without syntax errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/create_virtual_key.sh tests/e2e/test_client_auth.sh Makefile
git commit -m "feat: add virtual key helper and client auth smoke"
```

### Task 3: Client Documentation

**Files:**
- Create: `docs/clients/codex.md`
- Create: `docs/clients/claude-code.md`
- Create: `docs/clients/opencode.md`
- Create: `docs/clients/kilo-code.md`

- [ ] **Step 1: Document Codex**

Create a Codex guide that shows `~/.codex/config.toml` with:

```toml
model_provider = "ru_llm_proxy"
model = "openai-gpt-5.4-mini"

[model_providers.ru_llm_proxy]
name = "ru-llm-proxy"
base_url = "http://localhost:4000/v1"
env_key = "RU_LLM_PROXY_TOKEN"
wire_api = "responses"
```

Mention Codex App support is for local tasks and that upstream OpenAI Platform credentials stay on the proxy in server-funded mode.

Also document ChatGPT subscription passthrough using OpenAI authentication against the proxy:

```toml
model_provider = "ru_llm_proxy_chatgpt"
model = "openai-gpt-5.4-mini"

[model_providers.ru_llm_proxy_chatgpt]
name = "ru-llm-proxy via ChatGPT auth"
base_url = "http://localhost:4000/v1"
wire_api = "responses"
requires_openai_auth = true
env_http_headers = { "x-litellm-api-key" = "RU_LLM_PROXY_TOKEN" }
```

Call out that this path must be live-validated because standard LiteLLM header forwarding normally strips `Authorization`; if normal routing fails, the implementation must use pass-through or a sidecar.

- [ ] **Step 2: Document Claude Code**

Create a Claude Code guide that shows:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"
export ANTHROPIC_MODEL="claude-sonnet-4.6"
```

Mention `apiKeyHelper` as CLI-only dynamic auth.

Also document Claude subscription passthrough:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_MODEL="claude-sonnet-4.6"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"
claude
```

Call out that the Claude account auth stays in Claude Code on the client machine and must be live-validated against the pinned LiteLLM image.

- [ ] **Step 3: Document OpenCode**

Create an OpenCode guide with `opencode.json` using `@ai-sdk/openai-compatible`, `baseURL`, `apiKey`, and models `zai-glm-5.1`, `openai-gpt-5.4-mini`, and `openai-gpt-5.5`.

- [ ] **Step 4: Document Kilo Code**

Create a Kilo Code guide with OpenAI-compatible provider config for VS Code and CLI, sourced from `RU_LLM_PROXY_TOKEN`.

- [ ] **Step 5: Commit**

```bash
git add docs/clients
git commit -m "docs: add coding client setup guides"
```

### Task 4: README, Examples, And JWT Cleanup

**Files:**
- Modify: `README.md`
- Modify: `docs/examples.md`
- Modify: `.env.example`
- Create: `docs/clients/jwt.md`

- [ ] **Step 1: Make user examples virtual-key-first**

Replace normal client calls that use `LITELLM_MASTER_KEY` with `RU_LLM_PROXY_TOKEN`. Keep master key examples only for admin operations such as key generation or guardrail diagnostics.

- [ ] **Step 2: Add client access index**

Link the client guides from README, including JWT/OIDC proxy auth.

- [ ] **Step 3: Add JWT/OIDC guidance**

Add `docs/clients/jwt.md` for LiteLLM JWT/OIDC and JWT to virtual key mapping. Keep it disabled in default config because it needs an IdP/JWKS and LiteLLM Enterprise.

- [ ] **Step 4: Limit upstream examples to the supported providers**

Remove unused provider key placeholders from `.env.example` so the documented upstream set is `ZAI_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY`.

- [ ] **Step 5: Add docs guard**

Verify remaining `LITELLM_MASTER_KEY` references are admin-only:

```bash
rg -n "LITELLM_MASTER_KEY" README.md docs Makefile tests scripts
```

- [ ] **Step 6: Commit**

```bash
git add README.md docs/examples.md .env.example docs/clients/jwt.md
git commit -m "docs: make client examples virtual-key first"
```

### Task 5: Final Verification

**Files:**
- No required edits unless verification exposes issues.

- [ ] **Step 1: Run static checks**

```bash
bash -n scripts/create_virtual_key.sh
bash -n tests/e2e/test_client_auth.sh
python3 - <<'PY'
import yaml
from pathlib import Path
for path in ["litellm-config.yaml", "docker-compose.yml", ".github/workflows/baseline.yml"]:
    if Path(path).exists():
        yaml.safe_load(Path(path).read_text())
        print(f"{path} ok")
PY
git diff --check
make -n virtual-key-create
make -n client-auth-smoke
```

- [ ] **Step 2: Run available tests**

Run unit or dry-run checks available in the current environment. If Docker is unavailable, report that live smokes were not executed.

- [ ] **Step 3: Commit fixes if needed**

If verification required edits:

```bash
git add -A
git commit -m "fix: address client access verification issues"
```

### Task 6: Subscription/BYOK Passthrough Delta

**Files:**
- Modify: `litellm-config.yaml`
- Modify: `docs/clients/codex.md`
- Modify: `docs/clients/claude-code.md`
- Modify: `README.md`
- Modify: `docs/examples.md`
- Modify: `tests/e2e/test_client_auth.sh`
- Modify: `docs/superpowers/specs/2026-06-19-client-access-gateway-design.md`

- [ ] **Step 1: Enable LiteLLM header forwarding**

Add the required forwarding settings:

```yaml
general_settings:
  forward_client_headers_to_llm_api: true
  forward_llm_provider_auth_headers: true
```

Keep `LITELLM_MASTER_KEY` admin-only and keep existing server-funded provider API key routes working.

- [ ] **Step 2: Add x-litellm proxy auth smoke coverage**

Extend `tests/e2e/test_client_auth.sh` so it verifies `x-litellm-api-key` works as proxy auth when `Authorization` is occupied by a non-proxy bearer token.

- [ ] **Step 3: Update Codex docs**

Document two Codex modes:

- server-funded mode: `env_key = "RU_LLM_PROXY_TOKEN"`;
- ChatGPT subscription passthrough mode: `requires_openai_auth = true` plus `env_http_headers = { "x-litellm-api-key" = "RU_LLM_PROXY_TOKEN" }`.

Mark the ChatGPT passthrough path as requiring live validation on the current LiteLLM image. If `/v1/responses` strips `Authorization`, do not claim support until a pass-through route or sidecar is implemented.

- [ ] **Step 4: Update Claude Code docs**

Document two Claude Code modes:

- server-funded mode: `ANTHROPIC_AUTH_TOKEN="$RU_LLM_PROXY_TOKEN"`;
- Claude subscription passthrough mode: client-side Claude login plus `ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $RU_LLM_PROXY_TOKEN"`.

- [ ] **Step 5: Update README and examples**

Explain the two auth layers:

- `x-litellm-api-key` authenticates the caller to the proxy;
- `Authorization`, `x-api-key`, or provider-specific headers authenticate the caller to the upstream provider in passthrough/BYOK mode.

- [ ] **Step 6: Verify**

Run:

```bash
bash -n tests/e2e/test_client_auth.sh
python3 - <<'PY'
import yaml
from pathlib import Path
yaml.safe_load(Path("litellm-config.yaml").read_text())
print("litellm-config.yaml ok")
PY
git diff --check
make -n client-auth-smoke
```

Live validation must be reported separately because it requires running services and real Codex/Claude client auth.
