---
name: charm-cos-integration
description: >-
  Use when wiring observability — in-stack COS relations on Kubernetes
  (metrics-endpoint, logging, grafana-dashboard, tracing), OTLP ingestion as
  a provided interface, alert rules, and dashboards. Load before adding any
  observability endpoint to charmcraft.yaml or creating alert rule files.
metadata:
  verified: "2026-09-15"
  source: >-
    Interface-ecosystem facts carried from the pi-hole repo's skills (verified
    2026-08); the ClickStack-specific direction is decided open — nothing here
    is deployed. Re-check the interface library index before writing code.
---

# COS integration for a Kubernetes charm

## The pattern

On Kubernetes the charm does **not** delegate observability to a subordinate.
The in-stack pattern is direct: the charm itself provides the standard
endpoints and COS (or any compatible consumer) integrates with it.

```
clickstack ──metrics-endpoint──▶ prometheus (scrapes the collector's 8888)
clickstack ──logging──────────▶ loki (the charm forwards its containers' logs)
clickstack ──grafana-dashboard─▶ grafana
clickstack ──tracing (otlp)────▶ tempo / any OTLP receiver
```

There is no `cos-agent` here and **no `grafana_agent.cos_agent` library** —
that is the machine-subordinate pattern. Do not vendor it into this repo.

## The inversion: ClickStack *is* an observability backend

This charm is not a workload that COS observes; it is a charmed observability
stack. Which side of which interface it sits on is therefore a **design
decision, not a default**:

1. **ClickStack as a tracing provider.** The stack's entire purpose is
   ingesting OTLP on 4317/4318. The natural first relation is this charm
   *providing* the `tracing`/OTLP interface, so other Juju workloads ship
   their telemetry to ClickStack the way they would to Tempo. The
   `authorization`-header ingestion key has to fit whatever the interface's
   auth story is — that question alone makes it an ADR, not a default.
2. **ClickStack as a scrape target (self-observability).** The collector
   already exposes Prometheus self-metrics on 8888 — a natural
   `metrics-endpoint` provider with near-zero work.
3. **ClickStack as a log source and dashboard provider.** Standard
   `logging` and `grafana-dashboard` provider roles, same as any charmed
   workload.
4. **ClickStack as a COS *consumer*.** Would ClickStack ingest *from* COS's
   own agents, or be scraped *by* COS? Plausible in both directions; decide
   once, in the ADR.

Nothing is wired yet — zero relations exist. **Do not add any of these
endpoints speculatively**; each lands with an ADR (rule 5: optional by
default, and the charm reaches Active with zero relations).

## Libraries (2026 ecosystem)

- The in-stack interfaces (`prometheus_scrape`, `loki_push_api`,
  `grafana_dashboard`, `tracing`) have long been Charmhub-hosted libraries,
  and **Charmhub-hosted libraries are being phased out** in favour of PyPI
  packages. Before writing any provider/consumer code, re-check the
  [interface libraries index](https://documentation.ubuntu.com/charmlibs/reference/interface-libs/)
  — badges: ✅ recommended, 🚫 deprecated, no badge = neither — and prefer a
  PyPI package where one exists. Whichever library the first relation uses
  gets named in that relation's ADR and declared in `charm-libs:`.
- `charmlibs.interfaces.otlp` exists on PyPI (0.5.0, pre-1.0, verified
  2026-08) for communicating OTLP endpoints — it solves the
  endpoint-discovery problem and may fit the provider side here. **NOT
  VERIFIED** whether it covers the auth-header shape ClickStack needs.
- Note the `uv` plugin does **not** install transitive `PYDEPS` of
  Charmhub libraries — add them to `pyproject.toml` manually.

If a Charmhub library does get vendored, it lands under `lib/charms/...` as
the **one** legitimate vendored directory: never edit it in place, update it
only via `charmcraft fetch-libs`, and never lint it (`python-style` keeps
`lib/` out of the ruff scope deliberately).

## Alert rules

`src/prometheus_alert_rules/*.rules` — one concern per file, in the standard
Prometheus format, arriving only with the relation that consumes them. The
library injects Juju topology labels (`juju_model`, `juju_application`,
`juju_unit`); do not add them yourself.

Alerts worth writing for this workload, given the verified failure modes:

- The collector's exporter failing against ClickHouse (ingestion silently
  dropping while the service stays "running").
- The ingestion path refusing authenticated OTLP — the key rotated but the
  containers did not.
- Query rate collapse to zero — usually means senders silently failed over
  elsewhere.

Write the `description` for a human at 3am. State the user-visible impact and
the first diagnostic step.

## Dashboards

`src/grafana_dashboards/*.json`, exported Grafana JSON, arriving only with
the relation that consumes them. The library rewrites the datasource and
injects topology variables, so leave the datasource as a template variable
rather than hardcoding a UID.

## Do not

- Vendor `grafana_agent.cos_agent` or reach for a subordinate — machine-only
  pattern.
- Add `catalogue`, `probes`, or `datasource_exchange` endpoints. Those are for
  charms that are part of COS itself; decide whether ClickStack is with an
  ADR, not a copy.
- Add any COS endpoint without the ADR that decides its direction — the
  provider/consumer inversion above is exactly the kind of decision that
  gets silently defaulted and then cannot be undone.
