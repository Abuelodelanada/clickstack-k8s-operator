---
name: charm-testing
description: >-
  Use when writing or reviewing tests — unit tests with ops.testing (Scenario),
  conftest fixtures, coverage gates, or integration tests with jubilant on a
  Kubernetes cloud. Covers Model(type='kubernetes'), containers in Scenario
  state, the two-layer mocking strategy, and passing oci-image resources at
  deploy. Load before writing any test file.
metadata:
  verified: "2026-09-15"
  source: >-
    ops.testing/jubilant behaviour carried from the pi-hole repo's charm-testing
    (verified 2026-08) and adapted to K8s; the container-state surface is per
    the ops K8s examples (fetched 2026-09-15). Exact assertion names on
    testing.Container are NOT re-verified — confirm against the ops.testing
    reference when the first test lands.
---

# Testing a Kubernetes charm

Four layers, mirroring the source design. Do not mix them.

| Layer | Tool | What you mock |
|---|---|---|
| Pure decision (`compute`, config mapping) | plain `pytest` | **nothing** |
| State transition (`charm.py`) | `ops.testing` `Context` + `State` | the workload modules — whole (`src.clickhouse` etc.) |
| Workload (the container modules) | plain `pytest` | the Pebble client, the HTTP clients |
| Integration | `jubilant` + `pytest-jubilant` on a K8s cloud | nothing |

## Layer 0 — pure functions need no test infrastructure

This is the payoff of the functional split (see `charm-functional-style`). A
function with the shape `compute(state: ClickStackState, intent: IntentFields)
-> Sequence[ClickStackOutcome]` has no `self`, no IO, and no exceptions used
for control flow. Testing it is construction and `==`, because frozen
dataclasses give you `__eq__`:

```python
def test_absent_containers_yield_wait():
    # GIVEN a pod whose containers have not come up
    state = StackAbsent()

    # WHEN the outcome is computed
    outcomes = compute(state, intent())

    # THEN the only decision is to wait for readiness events
    assert outcomes == (WaitForContainers(),)


def test_unchanged_intent_yields_noop():
    # GIVEN a healthy stack whose applied config already matches intent
    state = stack_ready_with(collector_config=rendered)

    # WHEN the outcome is computed with the same intent
    outcomes = compute(state, intent())

    # THEN nothing happens — this is the "safe to run twice" proof
    assert outcomes == (Noop(),)
```

**Put as much logic as possible in this layer.** Every decision that lives here is
a decision tested without a mock, and mocks are where charm test suites rot.

**If a test at this layer needs `monkeypatch`, the function is not pure** — the
decide/act split is wrong. Fix the code, not the test.

## Layer 1 — state transition tests

The model type defaults to `kubernetes`, which is what this charm runs on —
the *machine*-flavoured trap is inverted here. Say it explicitly in the
fixture anyway so the intent is visible, and so nobody "fixes" it later:

```python
# tests/unit/conftest.py
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from ops import testing

import charm

if TYPE_CHECKING:
    from charm import ClickStackCharm


@pytest.fixture
def mock_workloads(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the four workload classes with mocks.

    The charm imports the classes from the workload modules, so the seam is
    the attribute on the charm module — this is the one place monkeypatch is
    legitimate, because ops offers no constructor injection for the charm.
    """
    mock = MagicMock()
    monkeypatch.setattr(charm, "ClickHouse", lambda *a, **kw: mock.clickhouse)
    monkeypatch.setattr(charm, "OtelCol", lambda *a, **kw: mock.otelcol)
    monkeypatch.setattr(charm, "HyperDX", lambda *a, **kw: mock.hyperdx)
    monkeypatch.setattr(charm, "MongoDB", lambda *a, **kw: mock.mongodb)
    return mock


@pytest.fixture
def ctx() -> testing.Context[ClickStackCharm]:
    return testing.Context(charm.ClickStackCharm)


@pytest.fixture
def base_state() -> testing.State:
    """A Kubernetes model with all four containers connectable."""
    return testing.State(
        model=testing.Model(type="kubernetes"),
        leader=True,
        containers={
            testing.Container(name="clickhouse", can_connect=True),
            testing.Container(name="otelcol", can_connect=True),
            testing.Container(name="hyperdx", can_connect=True),
            testing.Container(name="mongodb", can_connect=True),
        },
    )
```

A `pebble_ready` test is the same state plus the event, fired per container:

```python
def test_pebble_ready_converges(ctx, base_state, mock_workloads):
    # GIVEN a pod where clickhouse's Pebble just came up
    container = testing.Container(name="clickhouse", can_connect=True)

    # WHEN the ready event fires
    state_out = ctx.run(ctx.on.clickhouse_pebble_ready(container), base_state)

    # THEN the reconciler ran and the unit reports a non-error status
    mock_workloads.clickhouse.ensure_services.assert_called_once()
    assert state_out.unit_status == testing.ActiveStatus()
```

Test both directions of the non-negotiable: **what breaks if it runs twice**
and **what breaks if it never runs** (assert the status is
`Maintenance`/`Blocked`, not `Active`, when a container has not come up).
Run the same event twice to prove the first property — `ctx.run(ctx.on
.config_changed(), state)` twice and assert nothing regressed.

**Patching `ops.pebble`, an HTTP client, or a socket at this layer is the
finding, not the technique.** The workload modules exist so that mocking them
whole is enough. If a charm test needs to fake Pebble itself, rule 2 has
broken.

## Layer 2 — workload module tests

Patch what the container module actually calls — the `Container` handed to it
and the HTTP helpers:

```python
def test_ping_is_the_readiness_signal():
    # GIVEN a container whose Pebble says the service runs
    # but whose HTTP /ping refuses
    container = MagicMock()
    container.get_service.return_value.is_running.return_value = True

    # WHEN readiness is evaluated
    clickhouse = ClickHouse(container)

    # THEN the health endpoint, not the service state, decides
    assert clickhouse.is_ready() is False
```

Every Pebble/HTTP interaction in the workload modules deserves a test
asserting that acceptance is not trusted — this is the rule-6 read-back
encoded as a test.

## Testing actions and status

**`ctx.run_action` does not exist.** It was removed and raises
`AttributeError` with the replacement in the message. Actions go through
`ctx.run` like everything else:

```python
def test_get_ingestion_key_returns_secret(ctx, base_state, mock_workloads):
    # GIVEN a charm-owned secret with a known key
    # WHEN the action runs
    ctx.run(ctx.on.action("get-ingestion-key"), base_state)

    # THEN the key is returned to the operator
    assert ctx.action_results == {"key": "hd_xxx"}
```

- `ctx.on.action(name, params=...)` — `name` uses **dashes**, as in the metadata.
- `ctx.action_results` is `None` if the charm never called `set_results`.
- `ctx.action_logs` is a list of strings.
- If the charm calls `event.fail(...)`, `ctx.run` raises `testing.ActionFailed`.

**Testing `collect_unit_status`: prefer the indirect route.** The framework emits
it after every hook, so the resolved status is already in the output state. That
exercises the reconciler and the status handler together, with their real
interaction:

```python
state_out = ctx.run(ctx.on.config_changed(), state_in)
assert state_out.unit_status == testing.BlockedStatus(
    "clickhouse-image resource not supplied; ..."
)
```

`ctx.run(ctx.on.collect_unit_status(), state)` exists for isolated parametric
tables, but do not make it the primary way you test status.

`ctx.unit_status_history` gives the full sequence of intermediate statuses — useful
for asserting the unit passes through `MaintenanceStatus` during bootstrap rather
than jumping straight to Active.

## Treat warnings as errors

Run pytest with `-W error`. `ops` itself does this in its own unit tests, and the
official how-to recommends it. It is how you find an ops deprecation while it is
still a warning instead of after it becomes a breakage.

## Coverage

`fail_under = 90` in `[tool.coverage.report]`. Coverage measures `src/` only.
Do not chase the number by testing getters; chase it by covering failure paths,
which is where this charm's risk lives.

If coverage is hard to reach, that is usually a design signal rather than a
testing problem: logic that is expensive to cover is logic sitting on the wrong
side of the decide/act boundary. Move the decision into a pure function and the
coverage follows for free.

## Determinism

Tests must not depend on iteration order. If an assertion compares a serialised
collection, sort at construction (`tuple(sorted(...))`), not in the assertion —
otherwise the test passes while the production code still flaps. This is the same
defect `flaplint` looks for; see `python-style`. It matters doubly here: a Pebble
layer and the collector's OTel config are rendered files.

## Integration tests

`jubilant` supports K8s clouds as a first-class case. Environment setup with
Concierge (`sudo concierge prepare -p k8s` — profile name NOT VERIFIED) or a
manually bootstrapped microk8s controller.

```python
# tests/integration/conftest.py
import os
import pathlib

import pytest


@pytest.fixture(scope="module")
def charm_path() -> pathlib.Path:
    """Pack once, reuse across every test file in the run."""
    if path := os.environ.get("CHARM_PATH"):
        return pathlib.Path(path)
    pytest.skip("CHARM_PATH not set; pack the charm first")
```

```python
# tests/integration/test_deploy.py
def test_deploy_reaches_active(juju, charm_path):
    # GIVEN a fresh K8s model
    # WHEN the charm is deployed with its oci-image resources
    juju.deploy(
        charm_path,
        "clickstack",
        resources={
            "clickhouse-image": "clickhouse/clickhouse-server:26.1-alpine",
            "otelcol-image": "clickhouse/clickstack-otel-collector:2",
            "hyperdx-image": "hyperdx/hyperdx:2",
            "mongodb-image": "mongo:5.0.32-focal",
        },
    )

    # THEN it converges without any relations
    juju.wait(jubilant.all_active, timeout=1800)   # first-boot pulls four images


def test_ingestion_round_trip(juju):
    # GIVEN an active stack
    # WHEN an OTLP log is posted to the collector with the charm's key
    # THEN it is searchable in the UI's API — the docs' own test, via juju exec
    ...
```

- **Pass the resources explicitly.** A deploy without the oci-image resources
  cannot start, and the failure surfaces as an indefinite wait rather than a
  clear error — so make the missing-resource case a fast, explicit assertion,
  not a timeout.
- The getting-started flow (send an OTLP log with the ingestion key in the
  `authorization` header, then find it in the UI) is the natural end-to-end
  acceptance test. The app's own self-telemetry (`hdx-oss-app`) is a
  shortcut worth asserting too.
- Generous timeouts: four images pull, ClickHouse bootstraps schema, the UI
  does first-boot work.

### Kubernetes behaviour that differs from machines

- `Juju.remove_unit()` takes the **app name plus `num_units`** on K8s —
  *"individual units are not named"* — not unit names.
- `Juju.exec(..., container=...)` targets a workload container on K8s; the
  `machine=` parameter is machine-only. Exact flag semantics NOT VERIFIED —
  confirm with `juju help exec` before relying on them.
- `Status.machines` does not exist; the pod view comes through the K8s
  cloud's own tooling (`kubectl`) outside the test.

### Juju CLI names changed in 3.x — do not copy 2.9 examples

The rename is a trap because the same string means different things:

| Intent | Juju 2.9 | **Juju 3.6** |
|---|---|---|
| run an arbitrary command | `juju run` | **`juju exec`** |
| run a charm action | `juju run-action` | **`juju run`** |
| pick a series/base | `--series` | **`--base`** |

For verifying real container state from a test (non-negotiable #6), `juju
exec --unit clickstack/0 --container otelcol ...` reaches inside a workload
container (flag NOT VERIFIED — see above).

### `update-status` interval

Default is **5m**, changed with `juju model-config
update-status-hook-interval=30s`. Lowering it in the test model fixture makes any
test that depends on `update-status` converge much faster.

But note the design signal: **if the charm only reaches `ActiveStatus` via
`update-status`, some event that should trigger a reconcile is not observed.**
In this charm the containers announce themselves with `*_pebble_ready` — a
stack that only converges on `update-status` is missing one of those
observations. Lowering the interval to make a test pass hides that bug rather
than fixing it.

### Practical notes for this charm

- Never pack inside the test. Pack once by hand (`charmcraft pack`), export
  `CHARM_PATH`, and reuse it. Packing is slow and every test file would repeat it.
- **Name relation endpoints explicitly** in any integration test (when the
  first relation lands): `juju integrate clickstack:otlp other:endpoint`,
  never bare names — the implicit `juju-info` endpoint may win.
- Debugging tools worth knowing when a reconcile appears to hang:
  `juju show-status-log <unit>` (the full status transition history —
  invaluable for a flapping reconciler), `juju debug-log`, and `kubectl
  describe pod` for the image-pull side that Juju surfaces only as waiting.
