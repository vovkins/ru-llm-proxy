# Production Egress Templates

These manifests are starting templates for production deployments of `ru-llm-proxy`.
They are not applied by local Docker Compose.

Read the full guide first: `docs/egress-controls.md`.

## Files

| File | Purpose |
| --- | --- |
| `default-deny-egress.yaml` | Namespace-level default deny egress for all pods. |
| `internal-dependencies.networkpolicy.yaml` | Allows `litellm` to reach Analyzer, Redis and PostgreSQL only. |
| `analyzer-no-internet-egress.networkpolicy.yaml` | Documents and enforces no runtime internet egress for Analyzer. |
| `litellm-provider-egress.cilium.yaml` | Cilium FQDN allowlist for current external providers. |

## Before Applying

- Replace namespace `ru-llm-proxy` if your deployment uses another namespace.
- Align `app.kubernetes.io/name` labels with your Helm chart or manifests.
- Align CoreDNS/kube-dns labels in the Cilium policy with your cluster.
- Add or remove provider FQDNs to match `litellm-config.yaml`.
- Validate in staging with positive provider calls and negative non-allowlisted egress
  checks.
