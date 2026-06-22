# PII Block Mode Design

## Goal

Add a runtime PII policy mode that can either keep the current reversible masking behavior or block requests containing detected PII before they reach the upstream LLM provider.

## Runtime Modes

`PII_GUARDRAIL_MODE` controls normal policy behavior after successful PII detection:

| Value | Behavior |
| --- | --- |
| `mask` | Default. Preserve existing behavior: mask request text, save Redis mapping, call the provider, and restore placeholders in the response. |
| `block` | Reject requests containing detected PII during pre-call processing. Do not mutate request text, save Redis mappings, or call the provider. |

Invalid values fall back to `mask` and log a warning. This setting is separate from `PII_GUARDRAIL_FAILURE_MODE`, which only controls infrastructure failures such as Analyzer or Redis errors.

## Error Contract

Block mode returns a LiteLLM-compatible client error using HTTP 422 semantics. The error body includes only safe policy metadata:

```json
{
  "error": {
    "message": "Request contains personal data and was blocked by PII policy.",
    "type": "pii_detected",
    "code": "pii_blocked",
    "details": {
      "entities": ["PHONE_NUMBER", "RU_INN"]
    }
  }
}
```

The response must not include raw PII values, source text, offsets, or analyzer spans.

## Observability

Blocked requests increment pre-call metrics with result `blocked`, increment a per-entity block counter, and emit structured JSON logs with request id, entity types, and entity counts. Logs must not contain prompt text or raw PII.

## Tests

Unit tests cover:

- default mode remains `mask`;
- invalid mode falls back to `mask`;
- `block` rejects requests with PII;
- `block` allows clean requests;
- blocked requests do not save Redis mappings or mutate prompt fields;
- block-mode errors and logs do not leak raw PII;
- existing mask-mode behavior remains unchanged.

Documentation and Compose updates expose `PII_GUARDRAIL_MODE=mask` as the default runtime setting.
