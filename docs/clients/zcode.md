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
- Direct ZCode account login or upstream Z.AI credentials in ZCode when requests should go through ru-llm-proxy.

## Credentials

Use a LiteLLM virtual key as the ZCode client token:

```bash
export RU_LLM_PROXY_TOKEN="sk-..."
```

The real `ZAI_API_KEY`, `ZAI_API_KEY_2`, GLM Coding Plan keys, optional provider keys, and `LITELLM_MASTER_KEY` stay only on the proxy host. ZCode receives only the proxy token. Do not put `ZAI_API_KEY` or `LITELLM_MASTER_KEY` into ZCode.

All environment variables used in this guide are documented in [../configuration.md](../configuration.md).

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
| Model | `glm-5.2` | `glm-5.2` or another allowed proxy alias |

The additional alias `glm-5.1` is also available in the current repository config for installations that need the previous model generation. Prefer `glm-5.2` for new setup and smoke tests.

Example local configuration:

```text
Provider: OpenAI Compatible / Custom
OpenAI Base URL: http://localhost:4000/v1
API Key: $RU_LLM_PROXY_TOKEN
Model: glm-5.2
```

With this setup, ZCode requests go through the same proxy path as other clients: virtual-key auth, guardrails, PII masking and restoration, dictionary substitutions, routing, budgets, audit logs, and metrics.

## Proxy-Side Z.AI Configuration

The upstream Z.AI credential belongs in the proxy environment:

```env
ZAI_API_KEY=...
ZAI_API_KEY_2=...
```

The default Z.AI entries in `litellm-config.yaml` use the GLM Coding Plan OpenAI-compatible endpoint:

```yaml
model_list:
  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY
    model_info:
      id: glm-5-2-zai-coding-primary
  - model_name: glm-5.2
    litellm_params:
      model: openai/glm-5.2
      api_base: https://api.z.ai/api/coding/paas/v4
      api_key: os.environ/ZAI_API_KEY_2
    model_info:
      id: glm-5-2-zai-coding-secondary
```

Do not configure this upstream endpoint or upstream API key directly in ZCode when ZCode is supposed to use ru-llm-proxy. ZCode should call the proxy `/v1` endpoint; the proxy then calls Z.AI.

## Additional GLM Models

`glm-5.1` is available as an additional proxy alias. Use it only when a client or installation intentionally needs the previous model generation:

```bash
CHAT_MODEL=glm-5.1 make client-auth-smoke
```

For new setup, keep `glm-5.2` as the default.

## Validation

When the proxy is running and a virtual key exists, validate the OpenAI-compatible chat path:

```bash
CHAT_MODEL=glm-5.2 make client-auth-smoke
```

For guardrail behavior:

```bash
CHAT_MODEL=glm-5.2 make guardrails-smoke
```

Common failures:

- `401` or `403`: the LiteLLM virtual key is missing, expired, or not allowed to use the selected model group.
- Provider authentication error: `ZAI_API_KEY` or `ZAI_API_KEY_2` is missing or invalid on the proxy host.
- Connection or model-not-found error: the ZCode Base URL does not end with `/v1`, or the model alias is not present in `litellm-config.yaml`.

## References

- ZCode configuration: https://zcode.z.ai/en/docs/configuration
- Z.AI API authentication and OpenAI SDK style access: https://docs.z.ai/api-reference/introduction
- Z.AI GLM Coding Plan tooling endpoint: https://docs.z.ai/devpack/tool/crush
- LiteLLM virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
- LiteLLM OpenAI-compatible provider: https://docs.litellm.ai/docs/providers/openai_compatible
