# Шаблоны production egress

Эти манифесты — стартовые шаблоны сетевых ограничений для production-развертывания `ru-llm-proxy`. Они не применяются локальным Docker Compose и не являются полноценным Kubernetes deployment проекта.

Перед применением прочитайте основной гайд: [docs/egress-controls.md](../../../docs/egress-controls.md).

## Файлы

| Файл | Назначение |
| --- | --- |
| `default-deny-egress.yaml` | Namespace-level default deny egress для всех pod. |
| `internal-dependencies.networkpolicy.yaml` | Разрешает `litellm` доступ только к Analyzer, Redis и PostgreSQL. |
| `analyzer-no-internet-egress.networkpolicy.yaml` | Фиксирует и enforcing runtime-запрет интернет-доступа для Analyzer. |
| `litellm-provider-egress.cilium.yaml` | Cilium FQDN allowlist для текущих внешних провайдеров. |

## Перед применением

- Замените namespace `ru-llm-proxy`, если в вашем кластере используется другой namespace.
- Согласуйте labels `app.kubernetes.io/name` с Helm chart или манифестами вашей установки.
- Проверьте labels CoreDNS/kube-dns в Cilium policy.
- Добавьте или удалите provider FQDNs в соответствии с `litellm-config.yaml`.
- Проверьте изменения в staging: положительные provider calls должны проходить, отрицательные non-allowlisted egress checks должны блокироваться.

Полный production-дизайн Kubernetes отслеживается отдельно в issue #59.
