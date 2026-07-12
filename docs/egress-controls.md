# Production Egress Controls

Этот документ описывает production network layer для `ru-llm-proxy`: deny-by-default
egress, allowlist LLM providers и внутренние зависимости. Это defense-in-depth слой,
который дополняет, но не заменяет `PRE_EGRESS_POLICY_MODE`,
`FINAL_PAYLOAD_LEAK_CHECK_MODE`, PII mask/block и `make test-egress-security`.
Переменные окружения для этих policy layers и build-time загрузок описаны в
[configuration.md](configuration.md).

## Security Goal

Production окружение должно отвечать на простой вопрос: даже если в приложении
появился sanitizer miss, ошибка маршрутизации или неожиданная зависимость, сможет ли
контейнер отправить данные в неразрешенный внешний адрес? Целевое состояние:

- `litellm` может ходить только во внутренние сервисы проекта и к явно разрешенным
  LLM provider endpoints;
- `presidio-analyzer` не имеет внешнего runtime egress;
- `redis` и `db` не имеют internet egress;
- DNS и provider egress логируются на инфраструктурном уровне;
- изменение `litellm-config.yaml` или provider endpoint требует обновления allowlist.

## Scope Boundaries

Local Docker Compose bridge network не является production egress enforcement. Compose
удобен для разработки и smoke-тестов, но сам по себе не доказывает deny-all outbound
egress. Для production нужен отдельный слой: Kubernetes NetworkPolicy с CNI, egress
gateway/proxy, cloud firewall или host firewall.

`make test-egress-security` доказывает application-level свойство через mock provider
capture: raw тестовые значения не доходят до provider-bound payload, а blocked-запросы
не создают provider request. Этот gate не заменяет production firewall/CNI allowlist,
потому что внешний провайдер в live окружении не дает проекту полный network capture.

Build-time загрузки отделены от runtime egress. Analyzer image может скачивать spaCy и
DeepPavlov `ner_rus_bert` во время сборки, включая `DEEPPAVLOV_NER_MODEL_URL`. После
сборки `presidio-analyzer` в runtime не должен обращаться в интернет для обработки
запросов.

## Runtime Allowlist

Минимальная allowlist для текущего `litellm-config.yaml`:

| Service | Allowed destinations | Purpose |
| --- | --- | --- |
| `litellm` | `presidio-analyzer:5001` | PII detection via `POST /api/v1/analyze` |
| `litellm` | `redis:6379` | PII placeholder mappings and LiteLLM deployment affinity |
| `litellm` | `db:5432` | LiteLLM persistence |
| `litellm` | `api.z.ai:443` | Z.AI GLM provider (`api_base: https://api.z.ai/api/coding/paas/v4`) |
| `litellm` | `api.openai.com:443` | OpenAI aliases when `api_base` is not overridden |
| `litellm` | `api.anthropic.com:443` | Anthropic aliases when `api_base` is not overridden |
| `presidio-analyzer` | none | Runtime analysis is local after image build |
| `redis` | none | Internal dependency only |
| `db` | none | Internal dependency only |

If you add a provider deployment with explicit `api_base`, add that FQDN to the
production allowlist before enabling the deployment. If the provider uses regional
hosts, private endpoints or an enterprise gateway, allowlist the concrete host used by
that deployment instead of a broad wildcard.

## Kubernetes Pattern

Recommended baseline:

1. Put runtime services in a dedicated namespace such as `ru-llm-proxy`.
2. Apply namespace-level default deny egress.
3. Allow `litellm` to reach only `presidio-analyzer`, `redis`, `db` and approved
   provider FQDNs over `443/TCP`.
4. Keep `presidio-analyzer`, `redis` and `db` without internet egress.
5. Monitor DNS queries and denied flows with the CNI, egress gateway or firewall used
   by your platform.

Vanilla Kubernetes `NetworkPolicy` can express default-deny and internal service
traffic, but it does not provide portable FQDN allowlisting for dynamic provider
hosts. For provider FQDNs use a CNI or gateway that supports DNS-aware policies. The
template in `deploy/kubernetes/egress/litellm-provider-egress.cilium.yaml` uses
Cilium `CiliumNetworkPolicy`, `toFQDNs`, `matchName` and DNS proxy rules.

Templates:

- `deploy/kubernetes/egress/default-deny-egress.yaml`
- `deploy/kubernetes/egress/internal-dependencies.networkpolicy.yaml`
- `deploy/kubernetes/egress/analyzer-no-internet-egress.networkpolicy.yaml`
- `deploy/kubernetes/egress/litellm-provider-egress.cilium.yaml`

These files are intentionally templates. Before applying them, align namespace names,
pod labels, CoreDNS labels and provider endpoints with your cluster.

## Validation Checklist

Use a staging namespace before production:

1. Apply default-deny and internal dependency policies.
2. Apply provider FQDN policy for the configured providers.
3. Confirm `make health` equivalent probes pass through the service mesh/CNI path.
4. Send a clean request through each enabled provider alias and confirm success.
5. From the `litellm` pod, verify that a request to a non-allowlisted external host is
   denied by the network layer.
6. Confirm `presidio-analyzer`, `redis` and `db` cannot reach arbitrary internet hosts.
7. Run `make test-egress-security` in the application test environment to keep the
   application-level no-raw-provider-egress gate green.
8. Check CNI/egress logs for denied flows and unexpected DNS queries.

## Operational Checklist

- Review provider hosts whenever `litellm-config.yaml` changes.
- Review provider hosts during LiteLLM upgrades and provider SDK/API migrations.
- Keep `model_info.id` stable for sticky routing; egress allowlist should be tied to
  deployment endpoints, not to user-facing model aliases.
- Alert on denied outbound connections from `litellm`, especially new external FQDNs.
- Alert on any external outbound attempt from `presidio-analyzer`, `redis` or `db`.
- Store egress policy manifests together with production deployment manifests and run
  policy validation as part of release review.

## References

- [Kubernetes Network Policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [Cilium DNS-based policies](https://docs.cilium.io/en/stable/security/dns/)
