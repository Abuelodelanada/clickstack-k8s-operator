# ClickStack Kubernetes charm

[![CharmHub Badge](https://charmhub.io/clickstack-k8s/badge.svg)](https://charmhub.io/clickstack-k8s)

A [Juju](https://juju.is) charm for [ClickStack](https://clickhouse.com/docs/clickstack),
ClickHouse's open-source observability stack, on Kubernetes.

ClickStack is four workloads:

- **ClickHouse** — stores logs, traces, metrics and sessions.
- **OpenTelemetry Collector** — OTLP ingestion on `4317` (gRPC) and `4318` (HTTP).
- **HyperDX** — the UI, its API, and the alerting task runner, on `8080`.
- **MongoDB** — HyperDX application state: users, dashboards, alerts.

> [!WARNING]
> This is a **demo-grade charm**. It runs the upstream
> [all-in-one image](https://clickhouse.com/docs/clickstack/deployment/all-in-one), which upstream
> explicitly does not recommend for production, on a third-party Docker Hub image rather than a
> rock. It is single-unit only and has no relation endpoints. Read [DESIGN.md](./DESIGN.md) for
> what is missing and why.

## Quickstart

```shell
charmcraft pack
juju deploy ./clickstack-k8s_amd64.charm \
  --resource clickstack-image="$(yq '.resources.clickstack-image.upstream-source' charmcraft.yaml)"
juju status --watch 2s
```

First boot takes a few minutes: ClickHouse initialises its data directory, the collector runs its
ClickHouse schema migrations, and HyperDX cold-starts Next.js. The unit reports
`maintenance/waiting for hyperdx-ready` while that happens, then goes `active`.

Then, following the
[upstream getting-started guide](https://clickhouse.com/docs/clickstack/getting-started/oss):

```shell
# Reach the UI. In production you would put an ingress in front of this instead.
juju ssh --container clickstack clickstack-k8s/0 'echo ok'   # confirm the container is up
kubectl -n <model> port-forward svc/clickstack-k8s 8080:8080 4318:4318
```

1. Open <http://localhost:8080> and create an account.
2. Under **Get Started → Add Data**, copy the **Ingestion API Key**.
3. Send a test log:

   ```shell
   export CLICKSTACK_API_KEY=<your_ingestion_api_key>
   NOW_NANO="$(date +%s)000000000"
   curl -i http://localhost:4318/v1/logs \
     -H "Content-Type: application/json" \
     -H "authorization: ${CLICKSTACK_API_KEY}" \
     --data-binary @- <<EOF
   {"resourceLogs":[{"resource":{"attributes":[{"key":"service.name",
     "value":{"stringValue":"clickstack-charm-test"}}]},"scopeLogs":[{
     "scope":{"name":"clickstack-charm-test"},"logRecords":[{
     "timeUnixNano":"${NOW_NANO}","severityText":"INFO",
     "body":{"stringValue":"ClickStack ingestion test"}}]}]}]}
   EOF
   ```

4. Search for `ClickStack ingestion test` in the **Search** view.

## Accessing the UI from outside the cluster

Relate to an ingress. Both `traefik-k8s` and `istio-ingress-k8s` provide the `ingress` interface
this charm requires:

```shell
juju deploy traefik-k8s traefik --trust
juju config traefik routing_mode=subdomain external_hostname=example.com
juju integrate clickstack-k8s:ingress traefik:ingress
```

The charm feeds the URL it gets back into HyperDX's `FRONTEND_URL`, which is what stops the UI
redirecting you to its in-cluster address after login.

> [!IMPORTANT]
> **The ingress must use subdomain (host-based) routing.** HyperDX is a Next.js application built
> with `basePath: ""`, and `basePath` is a *build-time* setting — so HyperDX can only ever serve
> from the root of a host. Under path routing the HTML document loads but every `/_next/...` asset
> 404s, giving you a blank page with no explanation. The charm detects this and blocks:
>
> ```
> ingress uses path routing (/mymodel-clickstack); the UI cannot serve from a
> sub-path. Set routing_mode=subdomain and external_hostname on the ingress
> ```
>
> Note `routing_mode` is a property of the *ingress* charm, not of this one, and `subdomain` mode
> requires `external_hostname` to be a real DNS name rather than an IP address.

Traefik then serves the app at `<model>-<app>.<external_hostname>`. Without real DNS you can test
with `nip.io`, which resolves `*.<ip>.nip.io` to `<ip>`:

```shell
juju config traefik external_hostname=10.212.157.240.nip.io   # traefik's LoadBalancer IP
curl -i http://mymodel-clickstack-k8s.10.212.157.240.nip.io/
```

### Without an ingress

Set `external-url` by hand. Precedence is: `ingress` relation, then `external-url`, then the
in-cluster Service address.

```shell
kubectl -n <model> port-forward --address 0.0.0.0 svc/clickstack-k8s 8080:8080 4318:4318
juju config clickstack-k8s external-url=http://<vm-ip>:8080
```

`--address 0.0.0.0` matters if your cluster is in a VM and you are browsing from the host;
`port-forward` binds to `127.0.0.1` by default.

## Configuration


| Option                   | Type   | Default   | Purpose                                                        |
| ------------------------ | ------ | --------- | -------------------------------------------------------------- |
| `external-url`           | string | *(unset)* | Browser-reachable URL of the UI, used when there is no `ingress` relation. HyperDX bakes it into generated links. Overridden by the `ingress` relation. |
| `clickhouse-endpoint`    | string | *(unset)* | HTTP(S) endpoint (including port) of an external ClickHouse, e.g. ClickHouse Cloud. When unset, the bundled ClickHouse is used. |
| `clickhouse-user`        | string | `default` | Username for the external ClickHouse.                           |
| `clickhouse-credentials` | secret | *(unset)* | Juju user secret with a `password` key, for the external ClickHouse. |

## Relations

| Endpoint  | Interface | Role     | Optional | Purpose                                     |
| --------- | --------- | -------- | -------- | ------------------------------------------- |
| `ingress` | `ingress` | requires | yes      | External access to the HyperDX UI on `8080`. |

Note that only the **UI** is ingressed. The OTLP endpoints (`4317`/`4318`) are not, because the
`ingress` interface carries a single port. Ingressing OTLP as well needs the multi-port
`traefik_route` / `istio_ingress_route` interfaces — see [DESIGN.md](./DESIGN.md) stage 2.


Using ClickHouse Cloud:

```shell
juju add-secret ch-creds password=<password>
# note the returned secret URI
juju grant-secret ch-creds clickstack-k8s
juju config clickstack-k8s \
  clickhouse-endpoint=https://abc123.us-east-2.aws.clickhouse.com:8443 \
  clickhouse-user=default \
  clickhouse-credentials=<secret-uri>
```

The bundled ClickHouse still runs and is ignored — a limitation of the all-in-one image, not of
the charm.

## Storage

Three volumes, because without them a pod reschedule loses every ingested signal *and* every user
account and dashboard:

| Storage           | Mount                        | Holds                                        |
| ----------------- | ---------------------------- | -------------------------------------------- |
| `clickhouse-data` | `/var/lib/clickhouse`        | All ingested telemetry.                      |
| `clickhouse-logs` | `/var/log/clickhouse-server` | ClickHouse server logs.                      |
| `hyperdx-data`    | `/data/db`                   | Users, teams, API keys, dashboards, alerts.  |

They default to 1 GB each. Size `clickhouse-data` for your retention **at deploy time**; growing
a persistent volume afterwards needs manual intervention:

```shell
juju deploy ./clickstack-k8s_amd64.charm --storage clickhouse-data=50G ...
```

## Scaling

Not supported. ClickHouse and MongoDB are pod-local in the all-in-one image, so a second unit is
a second independent stack behind the same round-robin Service. The charm blocks on
`planned_units() > 1` rather than pretending otherwise.

## Development

```shell
tox -e format   # ruff format + autofix
tox -e lint     # codespell, ruff, pyright
charmcraft pack
```

Tests are not yet written; `tox -e unit` and `tox -e integration` are wired up and waiting for
`tests/`.

## Other resources

- [ClickStack documentation](https://clickhouse.com/docs/clickstack)
- [DESIGN.md](./DESIGN.md) — the staged plan, and the prerequisites that are out of scope
- [Contributing](./CONTRIBUTING.md)
