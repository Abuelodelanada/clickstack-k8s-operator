---
name: clickstack-stack
description: >-
  Use for anything touching the ClickStack workload itself — which container
  needs which environment, the ports each component listens on, the OTLP
  ingestion endpoints, the ingestion API key, first-boot bootstrap, the
  ClickHouse schema, and persistence. Read this BEFORE writing any code that
  drives a container, its API, or its config, because the wiring is not
  guessable from the image names.
metadata:
  verified: "2026-09-15"
  method: >-
    ClickHouse/ClickStack repo (docker-compose.yml, .env, docker/ tree) and
    the official ClickStack docs, fetched 2026-09-15. Container behaviour is
    NOT VERIFIED at runtime — no deployment exists yet. Facts gain runtime
    verification stage by stage; until then treat this file as the documented
    contract, not as measured truth.
---

# ClickStack — the stack this charm operates

Everything here is from the upstream sources. Where something was not
verified it says **NOT VERIFIED**. Treat that distinction as load-bearing:
upstream docs describe the all-in-one and compose flows, and this charm runs
the same components as four Pebble containers, which nobody has documented.

Source repo is available as the `clickstack` reference
(`ClickHouse/ClickStack`). Read `docker-compose.yml`, `.env`,
`docker/clickhouse/local/{config.xml,users.xml}` when this file is not
specific enough.

## What ClickStack is

The ClickHouse observability stack: an OTLP-ingesting collector, ClickHouse
for storage, and a HyperDX-derived UI for search/dashboards. Upstream ships
it as an all-in-one demo image, a Helm chart, and a docker-compose file
composed of individual images. This charm deploys the **individual images**
— the all-in-one image is explicitly *"not recommended for production"*.

## The four containers

From `docker-compose.yml` (fetched 2026-09-15):

| Service | Image (compose variable) | Role |
|---|---|---|
| `ch-server` | `clickhouse/clickhouse-server:26.1-alpine` | storage for logs/traces/metrics/sessions |
| `otel-collector` | `${CH_IMAGE_REPO}/${NEXT_OTEL_COLLECTOR_IMAGE_NAME_DOCKERHUB}:${IMAGE_VERSION}` = `docker.clickhouse.com/clickhouse/clickstack-otel-collector:2` (Docker Hub: `clickhouse/clickstack-otel-collector`) | OTLP/fluentd ingestion, writes to ClickHouse |
| `app` | `${HDX_IMAGE_REPO}/${IMAGE_NAME_DOCKERHUB}:${IMAGE_VERSION}` = `docker.hyperdx.io/hyperdx/hyperdx:2` | ClickStack UI + API server (HyperDX-derived) |
| `db` | `mongo:5.0.32-focal` | persistence for the UI's own state |

`.env` defaults: `HDX_IMAGE_REPO=docker.hyperdx.io`,
`CH_IMAGE_REPO=docker.clickhouse.com`, `CODE_VERSION=2.19.0`,
`IMAGE_VERSION=2`.

- The compose uses a **legacy** (`hyperdx/*`) and a **next** (`clickhouse/*`)
  image-naming pair; `NEXT_OTEL_COLLECTOR_IMAGE_NAME_DOCKERHUB` is the one
  wired for the collector. Which registry and tag the charm pins is an ADR
  decision, not a fact of nature. **NOT VERIFIED**: whether the `hyperdx/*`
  images have an equivalent on Docker Hub or ghcr.
- `mongo:5.0` is a 2020 release line pinned by upstream; whether the charm
  follows it or tracks a newer line is part of the same pinning ADR.
- The `app` service references `MINER_API_URL: http://miner:5123`, but the
  compose file **contains no `miner` service** — a stale/optional reference.
  **NOT VERIFIED** whether omitting it is safe. Do not wire it until proven.

## Ports

| Port | Component | Use |
|---|---|---|
| 8080 | app | UI (the port users browse; `HYPERDX_APP_PORT`) |
| 8000 | app | API server (`HYPERDX_API_PORT`) |
| 4320 | app | OpAMP server (`HYPERDX_OPAMP_PORT`) — the collector connects to it |
| 4317 | otel-collector | OTLP gRPC receiver (ingestion) |
| 4318 | otel-collector | OTLP HTTP receiver (ingestion) |
| 24225 | otel-collector | fluentd receiver (log ingestion) |
| 13133 | otel-collector | `health_check` extension — the readiness signal |
| 8888 | otel-collector | Prometheus self-metrics for the collector |
| 8123 | clickhouse | HTTP interface (`/ping` answers "Ok." — the readiness signal) |
| 9000 | clickhouse | native TCP protocol (what the collector writes through) |
| 27017 | mongodb | internal only in compose; the app reaches it in-pod |

Ports 4317/4318/13133/8888/24225 are *host-published* in compose; 8123/9000/
27017 are deliberately **not** — they are in-pod only. Whether the charm
exposes any of them beyond the pod is a `juju expose`/Ingress concern, not
the charm's (see `k8s-charm-workload`).

## Data flow

```
OTLP SDKs ──4317/4318──▶ otel-collector ──9000 (native)──▶ ClickHouse
                              │
                              └──OpAMP 4320──▶ app ◀──8123 (HTTP)── ClickHouse
                                                 │
                                                 ▼
                                             mongodb (27017)
```

The app itself also *ships* OTLP to the collector
(`OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318`,
`OTEL_SERVICE_NAME: hdx-oss-app`) — self-telemetry. That makes a natural
in-charm end-to-end ingestion test: the app's own traces are in ClickHouse.

## Environment wiring (the compose contract)

**otel-collector:**

| Env | Compose value | Meaning |
|---|---|---|
| `CLICKHOUSE_ENDPOINT` | `tcp://ch-server:9000?dial_timeout=10s` | where the exporter writes; note it is the **native** protocol, `tcp://`, not http |
| `HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE` | `default` | target database for OTel tables |
| `HYPERDX_OTEL_EXPORTER_CREATE_LEGACY_SCHEMA` | `"true"` | creates the legacy (otel_* ) schema on boot |
| `OPAMP_SERVER_URL` | `http://app:4320` | the collector's OpAMP management link |
| `HYPERDX_LOG_LEVEL` | `debug` | shared log level |

**app:**

| Env | Compose value | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://db:27017/hyperdx` | the UI's own persistence |
| `FRONTEND_URL` / `SERVER_URL` / `HYPERDX_APP_URL` | `http://localhost` etc. | URLs the UI renders/advertises — must match how users actually reach 8080 |
| `HYPERDX_API_PORT` / `HYPERDX_APP_PORT` / `OPAMP_PORT` | 8000 / 8080 / 4320 | listen ports |
| `HYPERDX_API_KEY` | unset in compose | **presets the ingestion API key** — the charm-owned secret hooks in here |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME` | `http://otel-collector:4318` / `hdx-oss-app` | the app's own telemetry |
| `DEFAULT_CONNECTIONS` | JSON: `{"name":"Local ClickHouse","host":"http://ch-server:8123","username":"default","password":""}` | pre-seeds the ClickHouse connection the UI queries through |
| `DEFAULT_SOURCES` | JSON: logs=otel_logs, traces=otel_traces, metrics=otel_metrics_{gauge,histogram,sum}, sessions=hyperdx_sessions | pre-seeds what the UI shows |
| `USAGE_STATS_ENABLED` | `true` | upstream usage reporting — an operator-visibility decision |
| `MINER_API_URL` | `http://miner:5123` | see the miner caveat above |

**ch-server:** `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: 1` (the default user
can manage access), plus mounted `config.xml` / `users.xml` from
`docker/clickhouse/local/`. **The charm must understand what those files
change before shipping its own** — NOT VERIFIED; read them in the reference
before rendering config.

**db:** no env; a plain `mongo` with a data volume.

In-pod names in this charm replace compose's `ch-server`/`db`/`app` hostnames
with Juju's container DNS names — everything above is a template, the
endpoints get rewritten per pod. **NOT VERIFIED**: that the app/collector
resolve sibling containers by these names in a Juju pod rather than compose's
network aliases; verify on first deploy.

## Ingestion auth and the API key

- OTLP requests to 4317/4318 must carry the **ingestion API key** in the
  `authorization` header. Upstream's tutorial has the user copy it from the
  UI's Get Started page after creating an account.
- `HYPERDX_API_KEY` on the app **presets** it instead. That is the seam this
  charm owns: mint a Juju secret, pass it into the app container's env, and
  the key is known to the operator without any UI scrape. This is the
  admin-password pattern from the pi-hole charm, transplanted.
- **Open design question (roadmap/ADR, not settled here): the first-boot
  admin user.** Upstream flow has a human create an account in the UI on
  first visit; the docs say ClickStack then creates data sources
  automatically. Whether the charm should auto-bootstrap an admin (seeded
  user/password as secrets), and whether `DEFAULT_CONNECTIONS`/`DEFAULT_SOURCES`
  make the manual step unnecessary, is NOT VERIFIED and must be settled
  empirically before any `ActiveStatus` claims to be done.

## Schema: Map vs JSON

ClickStack stores attributes as `Map(LowCardinality(String), String)` by
default — recommended for observability workloads. A `JSON`-typed schema
exists in beta and is **not recommended** as the default. The compose sets
`HYPERDX_OTEL_EXPORTER_CREATE_LEGACY_SCHEMA: "true"` ("TODO: use new schema"
upstream). Whether the charm exposes any of this is a config-surface decision
(rule 4) — the default is upstream's default.

## Persistence

Compose mounts: ClickHouse data `/var/lib/clickhouse`, logs
`/var/log/clickhouse-server`, MongoDB `/data/db`, and config files. Without
volumes, removing the deployment destroys the telemetry. For the charm this
maps to **Juju storage** (`storage:` in `charmcraft.yaml`, `type: filesystem`)
attached per container — until that lands, the charm is demo-grade, and the
roadmap must say so rather than implying durability.

## Upstream deployment shapes (for context)

| Shape | Upstream's word | This charm |
|---|---|---|
| All-in-one image | demos/PoC only, *"not recommended for production"* | rejected (topology decision) |
| Helm chart | production on K8s | the reference for what production topology looks like |
| Docker Compose | single-server production / BYO ClickHouse | the per-component contract this charm ports |
| HyperDX-only | BYO ClickHouse and schema | deferred to `docs/BACKLOG.md` (external ClickHouse) |

## What this means for the charm

- **Startup order is real**: the collector needs ClickHouse's native port, and
  the app needs both databases before it serves. Encode it in the reconcile
  sequence with health-gated steps (`k8s-charm-workload`).
- **Readiness signals exist per container**: collector 13133, ClickHouse
  `/ping` 8123, app on 8080, mongodb via `exec` ping. Use them; Pebble
  service state is not evidence (non-negotiable #6).
- **Everything is env-first.** Upstream configures this stack through env
  and two mounted XML files, not through APIs. The charm's config surface is
  therefore mostly *what it renders into container env and files* — and each
  knob it exposes is a rule-4 decision, not a default.
- **The interesting charm value-add** is exactly what compose does not do:
  minting and rotating the ingestion key, health-gated startup, verified
  upgrades, Juju-native relations (OTLP ingestion first, plausibly), storage,
  and status.
