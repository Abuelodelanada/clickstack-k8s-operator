---
name: k8s-charm-workload
description: >-
  Use when writing or reviewing src/charm.py, the container workload modules,
  or any code that drives Pebble layers, services, files, or the containers'
  HTTP APIs. Covers the reconciler skeleton, status collection, readiness
  verification, and the layering that makes the charm testable. Load before
  writing an event handler or touching ops.pebble.
metadata:
  verified: "2026-09-15"
  source: >-
    ops.testing/pebble APIs per canonical/operator k8s examples (fetched
    2026-09-15); status semantics and ops behaviour carried from the pi-hole
    repo's machine-charm-workload (verified 2026-08; the ops docstrings and
    discourse thread are charm-model-agnostic).
---

# Kubernetes charm workload management

## The layer design

This is the single most important structural decision, and it comes straight
from the official guidance
([run workloads with a charm](https://canonical.com/juju/docs/ops/latest/howto/run-workloads-with-a-charm/)):
charming concerns (event handlers, status, config parsing) stay in
`src/charm.py`; workload logic lives in separate modules. In this repo that is
one module per container — `src/clickhouse.py`, `src/otelcol.py`,
`src/hyperdx.py`, `src/mongodb.py` — with `src/clickstack_state.py` as the
pure core between them.

**Workload-module rules** (all four of them):

- Never import `ops` at runtime. The charm hands them the `ops.Container`;
  type-only references under `if TYPE_CHECKING:` at module top are fine.
- Take plain arguments (a container client, plain values), return plain
  values, or raise module-specific exceptions.
- Own every Pebble call, every HTTP request to a container's API, every
  rendered config file.
- Know nothing about relations, config options, or Juju statuses.

```python
# src/clickhouse.py — the shape, not the implementation.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ops


class ClickHouseError(Exception):
    """A ClickHouse operation failed or verified false."""


class ClickHouse:
    """Own every effect on the clickhouse container. Knows nothing about ops."""

    def __init__(self, container: ops.Container) -> None:
        self._container = container

    def ping(self) -> bool:
        """Readiness from the HTTP API: GET /ping on 8123 must answer Ok."""
```

**`src/charm.py` rules:**

- Never calls `add_layer`, opens a socket, or renders a container config
  directly — it calls the workload modules.
- Translates charm config into arguments for the workload modules.
- Translates workload return values into Juju statuses.

If you find yourself wanting to patch `ops.pebble` or an HTTP client in a
test of `charm.py`, the split is wrong.

## Pebble essentials

The container client is `self.unit.get_container(name)`. **It is unusable
until the container's Pebble socket exists** — which is exactly what the
`<name>_pebble_ready` event announces. `container.can_connect()` is the
cheap guard; a call on a missing socket raises `ops.pebble.ConnectionError`.

**Layers.** A layer is a declarative service description; `combine=True`
merges it into the plan:

```python
layer = ops.pebble.Layer(
    {
        "summary": "clickstack collector",
        "services": {
            "otelcol": {
                "override": "replace",
                "summary": "ClickStack OTel collector",
                "command": "/otelcol",   # what the image actually runs
                "startup": "enabled",
                "environment": {"CLICKHOUSE_ENDPOINT": "..."},
            }
        },
    }
)
container.add_layer("clickstack", layer, combine=True)
```

Use a **stable layer name** on every reconcile: adding the same-named layer
with the same content is a no-op, and `replan()` only restarts services whose
definition changed. A fresh name per render (or `replace=True` on the plan)
restarts the stack on every hook.

**`replan()` vs `start_services()`.** `replan()` reconciles the running
services with the plan (start changed/stopped-autostart, stop removed);
`start_services(names)` starts exactly what you name. Both return when
Pebble **accepted** the request — not when the process serves. That gap is
non-negotiable #6.

**Read-back.** `get_service(name)` gives current state (`is_running()`),
`get_services()` the whole set, `get_plan()` the merged plan the containers
actually run. The plan is also where env vars are visible — see the reviewer's
note on secret material in layer env.

**Files.** `container.push(path, source, make_dirs=True)` writes config into
the container's filesystem (`pull` reads back); `push_path`/`pull_path` move
trees. The collector's OTel config is a rendered file — determinism rules
(`sorted()` at creation) apply, and a pebble layer is a rendered file too, so
`flaplint` territory starts here.

**Exec.** `container.exec(command, ...)` runs a one-shot process in the
container and returns an `ExecProcess` with `wait_output()`. It is the escape
hatch for things with no API (`mongosh` ping, schema tooling) — every exec
still gets a read-back like any other effect.

**Checks and notices.** Pebble supports HTTP/TCP/exec checks
(`container.add_check(...)`, surfaced as `pebble_check_failed` /
`pebble_check_recovered` events) and custom notices
(`pebble_custom_notice`). Both are deferrable → `_reconcile`; a failed check
is a natural `MaintenanceStatus` or degraded-Active source, not a new handler.

## Reconciler skeleton

`src/charm.py` does not exist yet — this is the shape the roadmap converges
to, so do not treat field names as settled:

```python
import logging

import ops

from clickhouse import ClickHouse
from clickstack_config import ClickStackConfig
from clickstack_state import ClickStackIntent, fetch
from hyperdx import HyperDX
from mongodb import MongoDB
from otelcol import OtelCol

logger = logging.getLogger(__name__)


class ClickStackCharm(ops.CharmBase):
    """Charm the ClickStack containers in this pod."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        # Status collection is registered first and only reports.
        framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # Non-deferrable: actions land here when the first one is decided.
        # (remove/stop need no host cleanup on K8s — the pod goes away.)

        # Everything else converges through one reconciler, and the Pebble
        # readiness of all four containers is part of "everything else".
        for event in (
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
            self.on.clickhouse_pebble_ready,
            self.on.otelcol_pebble_ready,
            self.on.hyperdx_pebble_ready,
            self.on.mongodb_pebble_ready,
        ):
            framework.observe(event, self._reconcile)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the pod toward the desired state.

        Every step must be safe to run twice and safe to never run.
        """
        config = self.load_config(ClickStackConfig, errors="blocked")
        if not self._all_containers_connectable():
            return  # a pebble_ready will re-trigger us

        clickhouse = ClickHouse(self.unit.get_container("clickhouse"))
        otelcol = OtelCol(self.unit.get_container("otelcol"))
        hyperdx = HyperDX(self.unit.get_container("hyperdx"))
        mongodb = MongoDB(self.unit.get_container("mongodb"))

        intent = ClickStackIntent(config=config, ingestion_key=self._ensure_ingestion_key())

        # Order matters: ClickHouse and MongoDB before the collector and UI.
        clickhouse.ensure_services(intent)
        mongodb.ensure_services(intent)
        otelcol.ensure_services(intent)
        hyperdx.ensure_services(intent)

        if version := clickhouse.workload_version():
            self.unit.set_workload_version(version)

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        """Report status. Must not mutate anything."""
        ...  # pull statuses: health of each container, config validity


if __name__ == "__main__":  # pragma: nocover
    ops.main(ClickStackCharm)
```

`ops.main(ClickStackCharm)` is the current entrypoint — `from ops.main
import main` is deprecated since 2.16.0.

## Reconciler rules

1. **One `_reconcile`.** The only legitimate separate handlers are
   `collect_unit_status`, actions, `stop`/`remove` (no-ops here unless a
   decided feature needs them), `secret_rotate`/`secret_expired` when secrets
   get owners, and `upgrade_charm` when it needs migration logic distinct
   from convergence. The official test: *"if an event cannot be deferred, it
   needs a dedicated handler."* Everything — including every
   `*_pebble_ready` — routes to `_reconcile`.
2. **Idempotent steps.** Same-named layer with unchanged content: no-op.
   `replan()` restarts only what changed. `push` of identical bytes: no-op.
   Lean on that.
3. **Order matters where the stack demands it.** The collector needs
   ClickHouse before its exporter stops erroring; the app needs MongoDB and
   ClickHouse before its first page. Encode the ordering in the reconcile
   sequence and gate each step on the previous one's *verified* health, not
   on the event graph.
4. **`collect_unit_status` never mutates.** It reads cheap, pullable state:
   `can_connect()`, service state, health endpoints, config validity.
5. **Never report `ActiveStatus` from `get_services()` alone.** A service is
   `is_running()` before it answers requests; the collector is "running"
   while its exporter still errors against ClickHouse.
6. **Do not use `defer()`.** Deferring while waiting for other parts of the
   configuration is the documented antipattern — it queues handlers that redo
   the same expensive work. Set a status and return; the next event
   reconciles. A container that is not ready yet will announce itself with
   `pebble_ready`.
7. **Do not use `ops.StoredState`.** Caching a value that already exists in
   Pebble's plan, the containers' responses, and Juju's secrets doubles the
   possible states without adding a correct one. Read the real state on every
   reconcile — which non-negotiable #6 requires anyway.

## Status semantics — get this right, it is counterintuitive

Each settable status answers a different question. From the ops docstrings:

| Status | ops says | The question it answers | Settable |
|---|---|---|---|
| `ActiveStatus` | *"correctly offering all the services it has been asked to offer"* — and *"if the unit is operational but some feature is in a degraded state, set active with an appropriate message"* | is the workload doing its job right now? | yes |
| `MaintenanceStatus` | *"performing an operation such as `apt install`, or waiting for something under its control"* | is **this unit** busy, and will it clear on its own? | yes |
| `WaitingStatus` | *"waiting on a charm it's integrated with"* | am I blocked on **another application**? | yes |
| `BlockedStatus` | *"an administrator has to manually intervene to unblock the charm to let it proceed"* | can a human do something about it? | yes |
| `ErrorStatus` | *"the unit-agent has encountered an error"* — **read-only**, `add_status` raises `InvalidStatusError` | — | no |
| `UnknownStatus` | the state before the first `status-set` — **read-only** | — | no |

**"Waiting for the collector's ClickHouse schema" is `MaintenanceStatus`,
not `WaitingStatus`.** The schema is this unit's own workload, inside its own
pod. `WaitingStatus` is reserved for waiting on a *related* application —
which, today, this charm has none of.

### `BlockedStatus` is the one that directs a human

The test is a question, not a severity: **can an administrator do something
about it?** If yes, Blocked. If it clears on its own, Maintenance. If it
depends on another app, Waiting. If the service works but something is
degraded, Active with a message.

For this charm:

| Condition | Status | Why |
|---|---|---|
| invalid config (a future option, a bad secret URI) | **Blocked** | only a human can fix the config |
| an oci-image resource the operator never supplied / cannot be pulled | **Blocked** | the operator must pass a valid `--resource` |
| containers still pulling/starting | **Maintenance** | clears on its own; the next `pebble_ready` reconciles |
| ClickHouse schema bootstrapping, first-boot seeding | **Maintenance** | this unit's own asynchronous work |
| UI answers and ingestion works, one component unhealthy | **Active with a message** | the workload *is* offering its service, just degraded |
| a required relation absent (none exist yet) | **Waiting** | not an administrator problem |

Because a Blocked message exists to direct a human, it must name the action:

```python
# Bad: states the problem, not the remedy.
ops.BlockedStatus("clickhouse container missing")

# Good.
ops.BlockedStatus(
    "clickhouse-image resource not supplied; deploy with "
    "juju deploy clickstack --resource clickhouse-image=clickhouse/clickhouse-server:26.1-alpine"
)
```

### `raise` versus `BlockedStatus` — and the real cost of error state

This is contested territory. The reference discussion is
[*"It's probably ok for a unit to go into error state"*](https://discourse.charmhub.io/t/its-probably-ok-for-a-unit-to-go-into-error-state/13022),
worth reading in full because the ops docstrings do not capture the
trade-off. The consensus that emerges:

**Where the thread agrees with a plain "let it raise":**

- An operation that should structurally succeed (a hook precondition)
  failing is a genuine error. Ben Hoyt: *"I agree we should probably error
  more than we do."*
- Error is honest signalling: John Meinel frames it as *"something is
  fundamentally off, such that I cannot progress the model of the world"*.

**But the costs are concrete, not cosmetic:**

1. **`juju remove` does not work** on a unit in error without `--force`, and
   `--force` skips the `remove` handler. On K8s the pod goes away anyway, so
   this costs less than on a machine — but the charm still mints Juju
   secrets (the ingestion key) and any future in-cluster resource, and
   cleanup-by-force remains undefined behaviour.
2. **Model migration is blocked, full stop**, and so are some Juju
   upgrades. Paul Goins: *"if there are 'expected' cases where something is
   going to go into an error state, Juju migrations and at least some Juju
   upgrades are blocked, full stop."*
3. **Error detaches the charm from managing the workload.** It stops reacting
   to config and relation changes entirely.

**And the retry you were counting on is not guaranteed.**
`automatically-retry-hooks` is *model* config. It defaults to `true`, but
Tom Haddon notes it is *"not set that way everywhere"*, and Meinel confirms
OpenStack CI runs with it **disabled**. So "Juju will retry" is not a
property you can design on.

**The trap that decides the config case.** Error preserves the *original*
hook context. Leon: *"correcting a config option or relation data would not
help resolving the charm, because juju will continue to retry the hook with
the old context."* The operator has to run `juju resolve --no-retry <unit>`
after making the correction. So raising on bad config produces a unit that
retries forever against the value the operator already fixed. Meinel: *"Things
that need normal human intervention (bad config, missing relation) should
certainly not be errors, and that is what Blocked is for."*

**The middle ground is retry inside the hook, not raise.** Both Hoyt and
Meinel land here: *"If the API is flakey, do say 3 retries of that operation
in a simple loop (or use a retry decorator)"*. The local flaky surface is a
container's HTTP endpoint during startup:

```python
@tenacity.retry(
    # The narrow exception: a container's health endpoint refusing or
    # resetting during boot. Not ConnectionError on the Pebble socket —
    # that means pebble_ready has not fired and no amount of retrying
    # helps; return and let the event re-trigger.
    retry=tenacity.retry_if_exception_type(urllib.error.URLError),
    wait=tenacity.wait_fixed(2) + tenacity.wait_random(0, 5),
    stop=tenacity.stop_after_attempt(3),
    reraise=True,
)
def wait_healthy(self) -> None: ...
```

### The decision table for this charm

| Situation | Do this | Why |
|---|---|---|
| invalid config | `BlockedStatus` | retry cannot help, and error would retry the *stale* config forever |
| missing/unpullable oci-image resource | `BlockedStatus` | the operator can act; name the exact `--resource` |
| a container's Pebble socket missing | return; wait for `pebble_ready` | retrying does not create a socket |
| container HTTP flake during boot | retry ~3× in-hook, then Maintenance/raise | genuinely transient; if it persists something is wrong |
| health read-back mismatch after an apply | `BlockedStatus` via the reconciler | see the push-status problem below — a retry produces the same silent failure |
| first-boot bootstrap running | `MaintenanceStatus` | clears itself |
| a bug in our own code | let it raise | it *is* an error; the traceback is the point |

### The sharper Blocked criterion

Dylan Stephano-Shachter, in the same thread, on why people get this wrong:

> People see "manual intervention" and think *the charm can't recover
> automatically, so a human needs to debug the issue, thus blocked state.* My
> understanding is that it is not actually for the situation above, but for a
> situation where **the charm can tell the human what needs to be done.**

So the test is not just "can a human act?" but "**can we name the action?**"
If the charm cannot say what to do, Blocked is the wrong status — that is an
error or a `MaintenanceStatus`, depending on whether it is our bug or
transient.

Worth knowing the taxonomy has a genuinely ambiguous corner, acknowledged by
Tony Meyer: a missing relation — Blocked (a human must run `integrate`) or
Waiting (Juju is setting up an integration already requested)? There is no
clean answer. It is moot for this charm today, which reaches Active with
zero relations.

### The push/pull status problem — a real hole to close

Leon's framing, which the thread converges on:

- **Pull statuses** can be queried at any time. `collect_unit_status` was
  designed for exactly these.
- **Push statuses** are only knowable by *attempting* a mutating operation.
  They cannot be re-derived in the status handler.

Tony Meyer names the resulting race precisely: a check *looks* pullable,
*"but that introduces a race where your main handler failed and your
collect status handler succeeded."*

**This charm has that race.** Consider: `_reconcile` pushes the collector
config and replans, and the verification read-back fails, and it raises
`OtelColError`. Meanwhile `_on_collect_status` independently reads
`get_services()` and the health endpoint — which may well succeed, because
Pebble is running the service and only the new config never took. **The
unit reports `ActiveStatus` while the requested config was never applied.**
That is precisely the "model departure" the thread warns about, and it is
the exact defect non-negotiable #6 exists to prevent.

**The fix, and it needs no `StoredState`.** `ops/_main.py` runs
`_emit_charm_event()` and then `_evaluate_status()` in the same method, in
the same process, on the same charm instance. So a plain instance attribute
carries a push status from the reconciler to the status handler:

```python
def __init__(self, framework: ops.Framework):
    super().__init__(framework)
    self._reconcile_failure: ops.StatusBase | None = None
    ...

def _reconcile(self, _: ops.EventBase) -> None:
    try:
        ...
        otelcol.ensure_services(intent)
    except OtelColError as err:
        # A push status: collect_unit_status cannot re-derive this, because
        # the service is running and only the apply silently failed.
        self._reconcile_failure = ops.BlockedStatus(str(err))

def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
    if self._reconcile_failure is not None:
        event.add_status(self._reconcile_failure)
    # ... then the pull statuses
```

This is state, but it lives for one hook execution and is gone. It does not
violate the "no `StoredState`" rule, which is about caching across hooks.

**Prefer converting push to pull where you can.** Many apparent push statuses
can be written as pull statuses: "does `get_plan()` contain the environment
we rendered?" *is* pullable. Do that where it is cheap, and fall back to the
instance attribute only where attempting the operation is the only way to
know.

### Precedence

When several statuses are added (`StatusBase._get_highest_priority`):

```
blocked  >  maintenance  >  waiting  >  active
```

Ties go to the first one added. **One spurious Blocked masks every other
status the handler adds**, which is another reason to reserve it for genuine
operator problems.

`add_status` can be called many times, and the docs say *"each code path in
a collect-status handler should call `add_status` at least once"*.

`collect_app_status` also exists and the framework only emits it on the
leader, so you do **not** need an `is_leader()` guard in that handler. It
will matter the day this charm has peers or publishes anything app-wide.

## Networking — what a K8s charm actually has

**`Unit.set_ports` is machine-only.** There is no `open-port` on K8s; do not
copy it from a machine-charm example. Exposure is a Service / Ingress /
service-mesh concern that the operator (or a future decided relation)
handles outside the charm.

What the charm *does* own:

- Ports the containers bind are part of the layer/command/env, and their
  documentation lives in `charmcraft.yaml`'s description and
  `docs/stack-constraints.md`.
- `model.get_binding(endpoint_or_relation).network` still works on K8s and is
  the answer to "which address should we advertise in relation data?" —
  `bind_address` for binding, `ingress_address` for telling other apps. Use
  it rather than inventing a config option (rule 4).

In tests, `testing.Network(...)` with `testing.BindAddress`/`testing.Address`
populates it.

## Other APIs worth knowing

- **`self.unit.set_workload_version(str)`** — shows the workload's (not the
  charm's) version in `juju status`. Call it in the reconciler once a
  container answers. Raises `TypeError` if not given a `str`.
- **`self.unit.reboot(...)`** — machine charms only; raises on K8s. Never
  appears in this repo.
- **`self.app.planned_units()`** — how many units Juju *intends* to have.
  The way to distinguish a deliberate scale-down from a failed unit in
  `remove`.
- **`ops.hookcmds`** — a typed escape hatch for hook tools the model does
  not expose (`goal_state()`). Public and documented; mostly machine-flavoured,
  so expect little use here.
- **Logging needs no setup.** `ops.main` wires `logging` to `juju-log`, so
  `logging.getLogger(__name__)` already reaches `juju debug-log`.

## Typed config and params — prefer the native pydantic support

`ops` has first-class pydantic integration. Since this repo mandates pydantic
anyway, use it instead of hand-parsing `self.config`:

```python
config = self.load_config(ClickStackConfig, errors="blocked")
```

`errors="blocked"` sets `BlockedStatus` with a useful message and exits 0, so
Juju does not retry a hook that can only fail again. Dashes in option names
map to underscores, and `pydantic.Field(alias=...)` is respected. A
`type: secret` option arrives as an `ops.Secret` object rather than a URI
string. **Call it bare** — wrapping it in `try`/`except Exception` swallows
`ops._main._Abort` and the operator is told nothing.

The action equivalent:

```python
params = event.load_params(SomeParams, errors="fail")
```

## Secrets

`Model.get_secret` is **keyword-only**:

```python
secret = self.model.get_secret(id=uri)      # or label=...
```

`get_secret("secret:abc")` positionally is a `TypeError`. The ops docstring
for `Relation.load` gets this wrong — do not copy from it.

| Method | Use |
|---|---|
| `get_content()` | cached on the object |
| `get_content(refresh=True)` | **only** in a `secret_changed` handler — this is what tells Juju to start tracking the new revision |
| `peek_content()` | latest revision without changing tracking or caching |
| `set_content(...)` | creates a new revision; a no-op since Juju 3.6 if the content is identical |

`set_content` is another instance of non-negotiable #6: if the charm lacks
permission or the secret is gone, **the method succeeds** and the unit errors
at the *end* of the hook. The ingestion key this charm mints is a
charm-owned secret — read it back.

## Events that do not exist or should not be observed

- `pre_series_upgrade` / `post_series_upgrade` — **removed in Juju 4.0.** Do
  not observe them.
- `leader_settings_changed` — deprecated since ops 2.4.0.
- `collect_metrics` — removed in Juju 3.6.11.
- Every `*_pebble_ready` **does** exist and **must** be observed — one per
  declared container — and routed to `_reconcile` like everything deferrable.

## Verification pattern

Because Pebble accepting a request is not the process serving, every apply
gets a read-back at the layer *and* the endpoint:

```python
def ensure_services(self, intent: IntentFields) -> None:
    """Apply the collector layer and verify the service is really serving.

    Raises:
        OtelColError: Pebble accepted the plan but the collector did not
            become healthy on its health endpoint.
    """
    self._container.add_layer("clickstack", self._render_layer(intent), combine=True)
    self._container.replan()

    info = self._container.get_service("otelcol")
    if not info.is_running():
        raise OtelColError("otelcol accepted the plan but is not running")

    # The evidence that counts: the health extension answering on 13133.
    if not self._healthy():
        raise OtelColError("otelcol is running but its health endpoint does not answer")
```

And for the files the charm renders — the collector's OTel config — the
read-back is `pull()` the bytes and compare, or the workload's own view of
the config (its health/metrics endpoint), before declaring the step done.
