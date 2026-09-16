# ClickStack K8s charm — design plan

## 1. What we are charming

[ClickStack](https://clickhouse.com/docs/clickstack) is ClickHouse's observability stack. It is
not a single process: it is four cooperating workloads.

| Component      | Role                                                     | Ports                                    |
| -------------- | -------------------------------------------------------- | ---------------------------------------- |
| ClickHouse     | Columnar store for logs/traces/metrics/sessions           | `8123` (HTTP), `9000` (native), `9363` (Prometheus metrics) |
| OTel Collector | Ingestion pipeline, exports to ClickHouse                 | `4317` (OTLP gRPC), `4318` (OTLP HTTP), `13133` (health), `4320` (OpAMP) |
| HyperDX        | UI + API + alerting task runner                           | `8080` (app), `8000` (API)               |
| MongoDB        | HyperDX application state (users, dashboards, alerts)     | `27017`                                  |

Upstream offers six deployment models (see
[Open source deployment options](https://clickhouse.com/docs/clickstack/deployment/oss)). The two
that matter for charming are:

- **All-in-one** — one OCI image, `clickhouse/clickstack-all-in-one`, running all four components
  under a shell entrypoint. Explicitly *not* production-grade upstream: no component isolation,
  shared cgroup limits, no independent scaling.
- **Helm** — each component separate, the production path, supports BYO/ClickHouse Cloud.

This repo starts from all-in-one because it is the shortest path to a working demo, and the
charm's public interface (config options, relation endpoints, ports) is designed so the workload
topology can be swapped underneath it later without breaking users.

### What the all-in-one entrypoint actually does

Extracted from the image (`clickhouse/clickstack-all-in-one:latest`, Alpine 3.24 base with glibc
shims, `Entrypoint: ["sh", "/etc/local/entry.sh"]`, `WorkingDir: /app`):

```
/etc/local/entry.sh          -> sets IS_LOCAL_APP_MODE=REQUIRED_AUTH, sources entry.base.sh
/etc/local/entry.base.sh     -> appends "127.0.0.1 ch-server" and "127.0.0.1 db" to /etc/hosts
                                starts /entrypoint.sh (ClickHouse)   in background
                                starts mongod --dbpath /data/db      in background
                                blocks until curl http://ch-server:8123 succeeds
                                starts /otel-entrypoint.sh (schema migrations, then opampsupervisor)
                                runs node /etc/local/refresh-env.js  (bakes NEXT_PUBLIC_* into the SPA)
                                starts concurrently(API, APP, ALERT-TASK) in background
                                wait -n   # exits as soon as *any* child dies
```

Four facts from this drive the charm implementation:

1. **`working-dir` must be `/app`.** The script uses relative paths
   (`./node_modules/.bin/concurrently`, `cd ./packages/app/packages/app`). Pebble does not inherit
   the image's `WorkingDir`, so it must be set explicitly in the layer or the HyperDX app never
   starts.
2. **`wait -n` makes the script a usable Pebble service.** If any component dies the script exits,
   Pebble notices and restarts the whole thing. Crash-looping is correctly surfaced.
3. **Persistent state lives in three paths**: `/var/lib/clickhouse`, `/data/db`,
   `/var/log/clickhouse-server`. Without volumes, every pod reschedule wipes all telemetry *and*
   all user accounts and dashboards.
4. **Some env vars are unconditionally overwritten** by `entry.base.sh` and therefore cannot be
   exposed as charm config: `HYPERDX_LOG_LEVEL`, `CLICKHOUSE_LOG_LEVEL`, `MONGO_URI`,
   `SERVER_URL`, `EXPRESS_SESSION_SECRET`, `OPAMP_SERVER_URL`. Overridable ones are
   `FRONTEND_URL`/`HYPERDX_APP_URL`, `HYPERDX_APP_PORT`, `HYPERDX_API_PORT`,
   `CLICKHOUSE_ENDPOINT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `DEFAULT_CONNECTIONS`,
   `DEFAULT_SOURCES`, `BETA_CH_OTEL_JSON_SCHEMA_ENABLED`, `CUSTOM_OTELCOL_CONFIG_FILE`,
   `OTLP_AUTH_TOKEN`, `OIDC_ISSUER_URL`/`OIDC_AUDIENCE`.
5. **HyperDX cannot serve from a URL sub-path.** Verified from the running container:
   `/app/packages/app/packages/app/.next/required-server-files.json` has `basePath: ""` and
   `assetPrefix: ""`. In Next.js both are **build-time** settings with no runtime environment
   override, so no charm config can change them. This single fact determines the entire ingress
   design — see §4.2.


## 2. Prerequisites that are out of scope for this task

These are real blockers for anything beyond a demo. Listing them explicitly so nobody mistakes
Stage 1 for a shippable charm.

### 2.1 A rock (blocking for publication)

Stage 1 consumes the upstream Docker Hub image directly. Juju will happily do this — it overrides
the container entrypoint with `/charm/bin/pebble`, and Pebble is a static Go binary so the musl
base is not a problem. But Canonical cannot publish a charm on top of it:

- **Not Ubuntu-based.** Alpine + glibc shims copied in by hand. No Ubuntu Pro / ESM coverage, no
  CVE SLA, no `apt` provenance.
- **No reproducible build.** No `rockcraft.yaml`, no pinned part sources, no SBOM.
- **Unpinnable in practice.** `:latest` moves. We pin by digest in `charmcraft.yaml` and let
  Renovate bump it, which is honest but still means shipping a third-party binary blob.
- **Wrong shape for Juju.** The rock should split the four components into separately-startable
  Pebble services so they can be independently restarted, health-checked and log-forwarded,
  instead of hiding them behind a shell script that Pebble sees as one process.

Out of scope: `clickstack-rock` (or four rocks, or reuse of an existing
`ubuntu/clickhouse` + `ubuntu/opentelemetry-collector` + `ubuntu/mongodb`). Tracked separately.

### 2.2 Decomposition into multiple charms (the real production answer)

Upstream declares the all-in-one image unfit for production. The production Juju topology is
almost certainly:

- `clickhouse-k8s` (or ClickHouse Cloud via config) — a charm in its own right, with its own
  clustering, backup, and `data_integrator`-style relation.
- `mongodb-k8s` — **already exists** on Charmhub, maintained by the Data Platform team. HyperDX's
  `MONGO_URI` is hardcoded in the entrypoint today; consuming the real charm requires either a
  rock that honours `MONGO_URI`, or the Helm-style images.
- `opentelemetry-collector-k8s` — **already exists** in COS. ClickStack's collector is
  otelcol-contrib plus a ClickHouse exporter and an OpAMP supervisor. Long term the COS collector
  should grow a `clickhouse` exporter rather than ClickStack shipping a second collector.
- `hyperdx-k8s` — the only genuinely new charm, requiring `clickhouse` and `mongodb` relations.

Out of scope: that decomposition, and the `clickhouse`/`hyperdx` interface definitions it needs.

### 2.3 Other out-of-scope prerequisites

- **`clickhouse` charm relation interface** — no such interface exists on
  [charm-relation-interfaces](https://github.com/canonical/charm-relation-interfaces) yet.
- **Ingestion API key bootstrap.** Upstream expects a human to create the first account in the UI
  and copy the ingestion key out of it. Juju cannot ask a human. Automating this needs either an
  upstream env var to seed the key, or a charm action that POSTs to the HyperDX API. Until then,
  `receive-otlp` cannot be a functioning relation endpoint, because we have no key to hand out.
  **This is the single biggest functional blocker to making this charm useful in COS.** It needs
  an upstream conversation.
- **Charmhub registration** of the `clickstack-k8s` name, and a `latest/edge` track.
- **CI runners** with enough disk for a ~535 MB workload image.
- **`icon.svg`** — needs a real ClickStack/ClickHouse-derived icon with licensing checked. Stage 1
  ships a placeholder.

## 3. Staged plan

### Stage 1 — minimal demo (implemented in this repo)

Goal: `juju deploy ./clickstack-k8s_*.charm --resource clickstack-image=...` gives you a working
ClickStack UI on port 8080 and working OTLP ingestion on 4317/4318, with persistent storage.

Scope:

- Single `clickstack` container, workload image consumed straight from Docker Hub, pinned by
  index digest.
- One Pebble service, `clickstack`, running the image's own entrypoint (`sh /etc/local/entry.sh`)
  with `working-dir: /app` and the image's env restated explicitly.
- Pebble readiness checks: `http` on `:8123/ping` (ClickHouse), `tcp` on `:27017` (MongoDB) and
  `tcp` on `:8080` (HyperDX). All `level: ready`, deliberately **not** `alive` — first boot runs
  ClickHouse schema migrations and a Next.js cold start, and an `alive` check would kill the
  service mid-startup. The MongoDB check additionally drives
  `on-check-failure: {mongodb-ready: restart}`; see §5 for why that one component needs it.
- Three Juju storages mounted at the three persistence paths.
- Config: `external-url`, `clickhouse-endpoint`, `clickhouse-user`, `clickhouse-credentials`
  (a Juju user secret). Setting an external endpoint also rewrites `DEFAULT_CONNECTIONS` so the
  UI's seeded connection points at the external ClickHouse rather than the ignored local one.
- One relation endpoint: `requires: ingress` (`ingress` interface, `limit: 1`, `optional: true`),
  which feeds the resulting URL into `FRONTEND_URL`. See §4.2 for why this is the only interface
  that works and why the charm blocks on path routing.
- Holistic `_reconcile()` plus `collect-unit-status`, no per-event handlers.
- `set_ports(8080, 4317, 4318, 8123)`.
- Unit tests with `ops.testing` (`Context`/`State`), integration tests with `jubilant`.

Explicitly out of Stage 1: OTLP ingress, TLS, self-monitoring, actions, HA.

### Stage 2 — a real charm on a rock

- Consume `clickstack-rock` with one Pebble service per component; drop the shell entrypoint.
- `provides: receive-otlp` (`otlp` interface, matching `opentelemetry-collector-k8s`) — gated on
  solving the ingestion-key bootstrap (§2.3).
- OTLP ingress. The `ingress` interface used in Stage 1 carries a **single port**, so it only
  covers the UI. Ingressing `4317`/`4318` as well needs the multi-port interfaces —
  `traefik_route` (submit your own Traefik dynamic config, one entrypoint per port) or
  `istio_ingress_route` (declare your own listeners and routes). `opentelemetry-collector-k8s`
  implements both in `src/integrations.py` and is the reference. Note those interfaces return only
  `external_host` + `scheme`, not a URL, so the charm builds URLs itself — and they impose no path
  prefix, which conveniently sidesteps §4.2 entirely.
- TLS: `requires: certificates` + `receive-ca-cert`; render the collector's
  `standalone-auth-config.yaml`/TLS blocks and ClickHouse's TLS config.
- Self-monitoring: `provides: grafana-dashboards-provider`,
  `requires: metrics-endpoint`/`send-loki-logs` so COS can watch ClickStack. ClickHouse already
  exposes Prometheus metrics on `:9363` and the collector on `:8888`.
- `KubernetesComputeResourcesPatch` for CPU/memory limits (needs `--trust`).
- Actions: `get-admin-password` / `create-ingestion-key` / `reset-password`.
- Workload version reporting from the rock's own metadata rather than `printenv CODE_VERSION`.

### Stage 3 — production topology

- Split into `hyperdx-k8s` + relations to `clickhouse-k8s` and the existing `mongodb-k8s` and
  `opentelemetry-collector-k8s` charms.
- Define and land a `clickhouse` interface on charm-relation-interfaces.
- Terraform product module composing the pieces.
- Backup/restore for ClickHouse; scale-out for the collector.
- Multi-unit HA (Stage 1 and 2 are single-unit by construction: MongoDB and ClickHouse are
  pod-local, so `scale > 1` gives you N independent stacks behind one round-robin Service, which
  is broken. The charm should block on `planned_units() > 1` until Stage 3.)

## 4. Stage 1 architecture

```
                        ┌──────────────────────── Juju unit (pod) ─────────────────────────┐
                        │                                                                  │
  juju config  ────────▶│  charm container            clickstack container                 │
                        │  ┌───────────────┐          ┌──────────────────────────────────┐ │
                        │  │  src/charm.py │  pebble  │ pebble service "clickstack"      │ │
                        │  │  _reconcile() ├─────────▶│   sh /etc/local/entry.sh         │ │
                        │  │               │  layer + │   working-dir: /app              │ │
                        │  │  collect-     │  replan  │   ├─ clickhouse   :8123 :9000    │ │
                        │  │  unit-status  │◀─────────┤   ├─ mongod       :27017         │ │
                        │  └───────────────┘  checks  │   ├─ otelcol      :4317 :4318    │ │
                        │                             │   └─ hyperdx      :8080 :8000    │ │
                        │                             └──────────────────────────────────┘ │
                        │                                 │        │        │              │
                        └─────────────────────────────────┼────────┼────────┼──────────────┘
                                                          ▼        ▼        ▼
                                             clickhouse-data  hyperdx-data  clickhouse-logs
                                             /var/lib/         /data/db     /var/log/
                                             clickhouse                     clickhouse-server
```

`_reconcile()` is holistic and idempotent: every observed event runs the same code path, which
computes the desired Pebble layer from config, pushes it with `combine=True`, opens ports, and
replans. Status is never set inline; it is derived in `collect-unit-status` from container
connectivity, Pebble service state, and Pebble check state.

### 4.2 Ingress: why `ingress`, and why subdomain routing is mandatory

**The constraint.** HyperDX is a Next.js app built with `basePath: ""` (§1, fact 5). `basePath` is
build-time, so HyperDX serves only from the root of a host. Neither of the two ways a charm can be
served under a path prefix works:

| | `strip_prefix` | HyperDX sees | Outcome |
| --- | --- | --- | --- |
| Workload serves the sub-path (the `grafana-k8s` design, `GF_SERVER_SERVE_FROM_SUB_PATH`) | `False` | `/mymodel-clickstack/...` | Next.js 404s every request — no `basePath` to match. |
| Ingress strips, workload serves root (the `prometheus-k8s`/`alertmanager-k8s` design) | `True` | `/...` | HTML loads, then the browser requests `/_next/static/...` at the ingress root, which is not routed to us. Blank page. |

Verified in-cluster with Traefik in `routing_mode=path`: `GET /cs-cs/` → `200`, but
`GET /_next/static/chunks/polyfills-*.js` → `404`. Confirmed the second row exactly.

**The decision.** Require the `ingress` interface (`traefik_k8s.v2.ingress.IngressPerAppRequirer`)
and *detect* the unusable case rather than pretending to support it:

- One endpoint serves both providers. `traefik-k8s` and `istio-ingress-k8s` both provide `ingress`,
  so a single requirer works with either. This is why `ingress` was chosen over `traefik_route`,
  which would have bound the charm to Traefik.
- `strip_prefix=True`. A no-op under subdomain routing, and under path routing it at least gets the
  HTML document served so the failure is visible in the browser's network tab rather than being a
  bare 404.
- **Routing mode is not ours to choose.** It is Traefik's model-wide `routing_mode` config
  (`path`|`subdomain`); the `ingress` databag has no field for a requirer to request one. The only
  way to know is to parse the URL we are handed. So `_ingress_path_prefix` does exactly that, and a
  non-empty path yields `BlockedStatus` naming the prefix and the fix. Verified in-cluster: the unit
  goes `blocked` on `routing_mode=path` and back to `active` on `subdomain`.
- `subdomain` mode additionally requires Traefik's `external_hostname` to be a real DNS name, since
  you cannot put a subdomain under an IP address.

**URL precedence** is `ingress.url` → `external-url` config → in-cluster Service FQDN. The relation
wins because it is derived from the ingress actually routing the traffic, whereas the config option
is a human assertion that goes stale silently. This differs from `prometheus-k8s` and
`alertmanager-k8s`, which deprecated their equivalent config option into a no-op; here it is
retained as the supported no-ingress path (port-forward, NodePort), which is a real Stage 1
workflow.

**Library provenance.** `charms.traefik_k8s.v2.ingress` is Charmhub-hosted and has **no PyPI
equivalent** (`charmlibs-interfaces-ingress` does not exist; `charmlibs/interfaces/ingress/` holds
only the interface spec). It is therefore vendored at `lib/charms/traefik_k8s/v2/ingress.py` at
`LIBPATCH 21`, with its `PYDEPS` (`pydantic`) added to `pyproject.toml`. `charmcraft fetch-lib`
now prints a deprecation warning for all Charmhub libraries, so this file is a known piece of debt:
if and when the ingress interface migrates to PyPI, drop `lib/` and add the dependency instead.
By contrast `istio_ingress_route` and `service_mesh` *have* migrated
(`charmlibs-interfaces-istio-ingress-route`, `charmlibs-interfaces-service-mesh`), which is
relevant to the Stage 2 OTLP-ingress work above.

## 5. Known Stage 1 limitations

- `entry.base.sh` appends to `/etc/hosts` on every start, so restarts accumulate duplicate
  `ch-server`/`db` lines. Harmless, but it is a symptom of running a Docker-shaped entrypoint
  under Pebble. Fixed by the rock.
- **Pebble sees one process, and `wait -n` does not cover MongoDB.** `entry.sh` starts `mongod`,
  then blocks in a loop waiting for ClickHouse, and only reaches `wait -n` afterwards. A `mongod`
  that dies inside that window is reaped before anything is watching, so the stack runs
  indefinitely with no database: every port still answers and the UI still serves its static
  pages, but nobody can log in.

  This was observed, not theorised. After a `juju refresh`, `mongod` lost a race against an
  orphaned `mongod` from the previous container generation and exited with
  `Failed to set up listener: SocketException: Address in use` / `code:48`. The unit reported
  `active` for as long as it took to notice by hand.

  Mitigated in Stage 1 by a `mongodb-ready` TCP check plus
  `on-check-failure: {mongodb-ready: restart}` on the service, which makes the failure both
  visible in unit status and self-healing. It is a mitigation, not a fix: restarting the stack to
  recover one component also bounces ClickHouse and the UI. Properly fixed by the rock, with one
  Pebble service per component.
- If ClickHouse or HyperDX dies, `wait -n` does fire, so Pebble restarts the service — but that
  restarts *everything*, including MongoDB and the UI. Also fixed by the rock.
- No log forwarding: components log to files under `/var/log/` inside the container, not stdout,
  so `juju debug-log` and Loki see nothing. Fixed by the rock.
- First-boot is slow (ClickHouse init + schema migrations + Next.js). Expect a few minutes in
  `maintenance` before `active`.
- Single unit only.
- The demo requires manual account creation in the UI, and manual copying of the ingestion API
  key, exactly as the upstream getting-started guide describes.

## 6. Verification

Stage 1 acceptance, mirroring
[the upstream getting-started guide](https://clickhouse.com/docs/clickstack/getting-started/oss):

1. `juju deploy` the charm; unit reaches `active/idle`.
2. `pebble checks` in the workload container shows both checks `up`.
3. Browse to `http://<unit-ip>:8080`, create an account, confirm the four data sources
   (Logs/Traces/Metrics/Sessions) were seeded against the local ClickHouse.
4. Copy the ingestion API key, POST the guide's OTLP log payload to `:4318/v1/logs`, expect
   `200`, and find the event in the Search view.
5. `juju refresh`/`juju remove-unit`-style pod churn: confirm the account and the ingested event
   survive, proving the storage mounts work.

Steps 1–2 are automated in `tests/integration/`. Steps 3–5 are manual at Stage 1 because they
need the API key from the UI.
