# ZCode

ZCode connects to this proxy as an OpenAI-compatible client in API Key mode.

Supported scope:

- ZCode configured with `Use API Key`.
- ZCode custom/OpenAI-compatible provider settings that can set an OpenAI Base URL and API key.
- Server-funded Z.AI/GLM access through ru-llm-proxy.

Not covered here:

- ZCode account login through `Continue with Z.ai` or `Continue with BigModel`.
- Storing shared ZCode account/session credentials on the proxy.
- Subscription or account-auth passthrough for ZCode.
- Claiming `glm-5.2` support before the proxy config and upstream model ID are live-validated.

## Credentials

Use a LiteLLM virtual key as the ZCode client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `ZAI_API_KEY`, GLM Coding Plan API key, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` stay only on the proxy host. ZCode receives only the proxy token. Do not put `ZAI_API_KEY` or `LITELLM_MASTER_KEY` into ZCode.

Create routine user/client keys in LiteLLM Admin UI. The CLI helper is only an optional DevOps/CI/bootstrap path from the proxy host:

```bash
scripts/create_virtual_key.sh --alias zcode-local --models standard,zai --duration 30d
```

The helper prints `RU_LLM_PROXY_TOKEN=...`; that value goes into ZCode's API Key field.

## ZCode Setup

On first launch, choose `Use API Key`. If ZCode is already configured, open the model/provider settings and add an OpenAI-compatible custom provider.

Use these values:

| ZCode field | Local value | Deployed value |
| --- | --- | --- |
| OpenAI Base URL | `http://localhost:4000/v1` | `https://<proxy-host>/v1` |
| API Key | `$RU_LLM_PROXY_TOKEN` | LiteLLM virtual key issued by the proxy |
| Model | `zai-glm-5.1` | `zai-glm-5.1` or another allowed proxy alias |

The legacy alias `glm-5.1` is also available in the current repository config. Prefer `zai-glm-5.1` in new ZCode setup because it makes the provider boundary explicit.

Example local configuration:

```text
Provider: OpenAI Compatible / Custom
OpenAI Base URL: http://localhost:4000/v1
API Key: $RU_LLM_PROXY_TOKEN
Model: zai-glm-5.1
```

With this setup, ZCode requests go through the same proxy path as other clients: virtual-key auth, guardrails, PII masking and restoration, dictionary substitutions, routing, budgets, audit logs, and metrics.

## Proxy-Side Z.AI Configuration

The upstream Z.AI credential belongs in the proxy environment:

```env
ZAI_API_KEY=...
```

The default Z.AI entries in `litellm-config.yaml` use the GLM Coding Plan OpenAI-compatible endpoint:

```yaml
model_list:
  - model_name: zai-glm-5.1
    litellm_params:
      model: openai/glm-5.1
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
```

Do not configure this upstream endpoint or upstream API key directly in ZCode when ZCode is supposed to use ru-llm-proxy. ZCode should call the proxy `/v1` endpoint; the proxy then calls Z.AI.

## Adding GLM 5.2 Later

Do not type `glm-5.2` into ZCode until the proxy exposes a matching alias.

To add GLM 5.2 later:

1. Confirm the exact upstream Z.AI model ID and endpoint behavior.
2. Add a new `model_list` entry or alias in `litellm-config.yaml`.
3. Give every deployment a stable `model_info.id` so sticky routing and metrics remain meaningful.
4. Restart LiteLLM with `make restart`.
5. Run a live client smoke against the new alias:

```bash
CHAT_MODEL=zai-glm-5.2 make client-auth-smoke
```

Only after that should the new alias be documented as a supported ZCode model.

## Validation

When the proxy is running and a virtual key exists, validate the OpenAI-compatible chat path:

```bash
CHAT_MODEL=zai-glm-5.1 make client-auth-smoke
```

For guardrail behavior:

```bash
CHAT_MODEL=zai-glm-5.1 make guardrails-smoke
```

Common failures:

- `401` or `403`: the LiteLLM virtual key is missing, expired, or not allowed to use the selected model group.
- Provider authentication error: `ZAI_API_KEY` is missing or invalid on the proxy host.
- Connection or model-not-found error: the ZCode Base URL does not end with `/v1`, or the model alias is not present in `litellm-config.yaml`.

## References

- ZCode configuration: https://zcode.z.ai/en/docs/configuration
- Z.AI API authentication and OpenAI SDK style access: https://docs.z.ai/api-reference/introduction
- Z.AI GLM Coding Plan tooling endpoint: https://docs.z.ai/devpack/tool/crush
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
- LiteLLM OpenAI-compatible provider: https://docs.litellm.ai/docs/providers/openai_compatible
