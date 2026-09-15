---
name: k8s-charm-scaffold
description: >-
  Use when creating or editing charmcraft.yaml, pyproject.toml, tox.ini,
  .jujuignore, or deciding the repo layout for this Kubernetes charm. Covers
  base/platforms syntax, containers and oci-image resources, the uv part
  plugin, config options, actions, and where dependencies belong. Load before
  running charmcraft init or hand-writing any of those files.
metadata:
  verified: "2026-09-15"
  source: >-
    canonical/operator examples/k8s-1-minimal .. k8s-5-observe (fetched
    2026-09-15); charmcraft/uv behaviours carried from the pi-hole repo's
    machine-charm-scaffold (verified 2026-08, NOT re-verified here).
---

# Kubernetes charm scaffolding

Reference implementation: `canonical/operator` →
`examples/k8s-1-minimal/` (minimal), `k8s-2-configurable/` (config),
`k8s-3-postgresql/` (relations), `k8s-4-action/`, `k8s-5-observe/` (COS),
available as the `ops` reference. Read one before inventing structure.

## `charmcraft.yaml`

`bases:` is **deprecated**. Use `base:` plus `platforms:`.
([charmcraft.yaml reference](https://canonical.com/juju/docs/charmcraft/stable/reference/files/charmcraft-yaml-file/))

```yaml
type: charm
name: clickstack
title: ClickStack
summary: ClickHouse observability stack in a single pod.   # <= 78 chars
description: |
  ClickStack is the ClickHouse observability stack: the HyperDX-derived UI,
  an OpenTelemetry collector for OTLP ingestion, MongoDB, and ClickHouse
  itself, deployed as four Pebble containers in one pod.

  Key features:
  - OTLP log, trace and metric ingestion over gRPC and HTTP
  - Search and dashboards over ingested telemetry
  - Charm-owned ingestion API key, minted as a Juju secret

base: ubuntu@26.04
platforms:
  amd64:
  arm64:

assumes:
  - juju >= 3.6
  - k8s-api

parts:
  charm:
    plugin: uv
    source: .
    build-snaps:
      - astral-uv

config:
  options: {}

containers:
  clickhouse:
    resource: clickhouse-image
  otelcol:
    resource: otelcol-image
  hyperdx:
    resource: hyperdx-image
  mongodb:
    resource: mongodb-image

resources:
  clickhouse-image:
    type: oci-image
    description: ClickHouse server, as used by the ClickStack compose file
  otelcol-image:
    type: oci-image
    description: ClickStack OTel collector (clickstack-otel-collector)
  hyperdx-image:
    type: oci-image
    description: ClickStack UI (HyperDX-derived)
  mongodb-image:
    type: oci-image
    description: MongoDB, as used by the ClickStack compose file
```

Notes:

- Every `containers:` entry must point at a `resources:` entry of
  `type: oci-image`. Juju injects the image into the pod; Pebble runs inside
  it. One image per container — the individual images the ClickStack compose
  file uses, **not** the demo all-in-one image (topology decided, ADR-0001
  pending).
- The exact image references (`upstream-source:`) are a pinning decision —
  record them in the ADR that owns it, not here. The compose file's image
  variables are in `clickstack-stack`.
- `upstream-source:` is ignored by Charmcraft and Juju at deploy time — the
  operator passes `--resource` at deploy, and integration tests must do the
  same. It exists for tooling and documentation.
- The `platforms:` shorthand (`amd64:` with a null value) expands to
  `build-on: [amd64], build-for: [amd64]`.
  ([platforms reference](https://canonical.com/juju/docs/charmcraft/stable/reference/platforms/))

### Why 26.04

The pi-hole repo this set was inherited from stayed on `ubuntu@24.04` because
a *machine subordinate* (`opentelemetry-collector`) had no 26.04 revisions.
That blocker does not exist here: this charm has no subordinates and no
base-compatibility coupling to other charms. Everything else was verified
ready (2026-08, pi-hole repo): Juju supports 26.04 since 3.6.17/4.0.6,
charmcraft accepts `base: ubuntu@26.04` without `build-base`, Python is
3.14 — the only interpreter in that base's archive — and `pydantic-core`
ships cp314 manylinux wheels for both target arches.

Consequences: `requires-python = ">=3.14"`, `ruff target-version = "py314"`,
`pyright pythonVersion = "3.14"`. Do not write code that merely tolerates
older interpreters, and do not pin 3.12/3.13 "for safety" — the charm never
runs on them.

Carried-over rules that stay load-bearing:

- **Never put `/` in a part name.** Forbidden on 26.04 and later bases.
- **Never switch to the `charm` plugin.** It does not exist on the 26.04
  base; `uv` is the only forward-compatible choice.
- **`parts:` is not optional in practice.** Omit it and charmcraft applies
  the legacy `charm` plugin, which builds from `requirements.txt`.
- `UV_FROZEN` defaults to `true` in the `uv` plugin, so `uv.lock` **must**
  exist and is the only source of truth. `UV_PYTHON_DOWNLOADS=never` and
  `UV_PYTHON_PREFERENCE=only-system` mean the build uses the base's Python.
- The `description` is rendered on Charmhub: lead with what the workload
  does, then bullet the charm's features. `summary` must be 78 characters or
  fewer.
- `charm-libs:` is **only** for Charmhub-hosted libraries — the reference
  says so verbatim. Regular PyPI packages go in `pyproject.toml`. This charm
  needs none yet; when the first in-stack COS or tracing relation is decided,
  name the library in that ADR. **The `uv` plugin does not install transitive
  `PYDEPS` of Charmhub libraries** — add them to `pyproject.toml` manually.

## `pyproject.toml`

```toml
[project]
name = "clickstack-k8s-operator"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "ops>=3.8,<4",
    "pydantic>=2,<3",
    "tenacity>=9,<10",
]

[dependency-groups]
dev = [
    "ops[testing]",
    "pytest",
    "pytest-cov",
    "coverage[toml]",
    "jubilant>=1.12,<2",      # NOT >=2: jubilant 2.x does not exist, latest is 1.12.0
    "pytest-jubilant>=2.2,<3",
    "ruff",
    "pyright",
]

[tool.ruff]
line-length = 99
target-version = "py314"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "RUF", "ANN", "D", "PLC0415"]
# E501 is deliberately NOT ignored: PEP 8 only permits 99 chars on the condition
# that prose stays at 72, so both limits have to be enforced or neither is.
# PLC0415 is what actually enforces "no imports inside functions" — E402 only
# catches late module-level imports and lets `def f(): import x` through.
ignore = ["D105", "D107"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 72   # enables W505, the PEP 8 proviso for comments/docstrings

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["ANN"]

[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.14"
pythonPlatform = "Linux"
typeCheckingMode = "strict"
enableTypeIgnoreComments = false
reportUnnecessaryTypeIgnoreComment = "error"

[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
fail_under = 90
show_missing = true
```

`uv.lock` is committed. Regenerate with `uv lock`, never edit by hand.

**Why `ops>=3.8,<4` and not `~=3.8`.** ops ships a minor version roughly
monthly, and the support policy is explicit: *"To receive bug and security
fixes within a major version, charms must update to the latest minor release
within that major version."* `>=3.8,<4` documents the floor and the ceiling.

Extras: `ops[testing]` pulls `ops-scenario` (dev group), `ops[tracing]` pulls
`ops-tracing` (runtime, only if you want charm traces). `ops[harness]` exists
only to ease migration — `Harness` is legacy, do not use it in new code.

**No `charmlibs-*` dependencies.** `charmlibs-snap`/`-systemd`/`-apt` are
machine-charm libraries; nothing in a pod can use them. In-pod workload
management is `ops`' own Pebble API. `lightkube` goes in only when an ADR
decides the charm owns a cluster resource beyond the pod.

## `tox.ini`

```ini
[tox]
no_package = True
skip_missing_interpreters = True
env_list = fmt, lint, static, unit

[vars]
src_path = {tox_root}/src
tests_path = {tox_root}/tests

[testenv]
runner = uv-venv-lock-runner
set_env =
    PYTHONPATH = {tox_root}/lib:{tox_root}/src
    PYTHONBREAKPOINT = pdb.set_trace
pass_env = PYTHONPATH, CHARM_PATH, JUJU_*

[testenv:fmt]
description = Apply coding style standards
dependency_groups = dev
commands =
    ruff format {[vars]src_path} {[vars]tests_path}
    ruff check --fix {[vars]src_path} {[vars]tests_path}

[testenv:lint]
description = Check code against coding style standards
dependency_groups = dev
commands =
    ruff check {[vars]src_path} {[vars]tests_path}
    ruff format --check --diff {[vars]src_path} {[vars]tests_path}

[testenv:static]
description = Run static type checks
dependency_groups = dev
commands = pyright {posargs}

[testenv:unit]
description = Run unit tests
dependency_groups = dev
commands =
    coverage run --module pytest {[vars]tests_path}/unit {posargs}
    coverage report

[testenv:integration]
description = Run integration tests against a juju K8s model
dependency_groups = dev
commands = pytest --exitfirst {[vars]tests_path}/integration {posargs}

[testenv:flaplint]
description = Detect relation-databag ordering churn (advisory, not in env_list)
skip_install = true
allowlist_externals = uvx
commands =
    uvx --python 3.14 --from git+https://github.com/michaeldmitry/flaplint@v1.1.0 \
        flaplint {tox_root}/src --own-only --min-confidence high

[testenv:lock]
description = Update uv.lock
commands = uv lock --upgrade
```

`PYTHONPATH` includes `lib` so a future Charmhub library is importable without
configuration; `src` is what makes imports flat (`import charm`, not
`from src import charm`).

**flaplint under 3.14 is NOT VERIFIED.** Its CI matrix was 3.10–3.13
(verified 2026-08 in the pi-hole repo) and it parses source with the
*running* interpreter's `ast`, so it must run on 3.14 to read this repo's
py314 syntax — including the PEP 758 `except` clauses `ruff format` emits.
The first `tox -e flaplint` run on real code is the verification; until then
treat a clean run as unproven. It stays **outside** `env_list` for the same
reasons as before: six-week-old single-maintainer project, no PyPI release,
no LICENSE file — see `python-style` for the full risk assessment and the
f-string blind spot.

## Layout

Planned — `src/` does not exist yet:

```
charmcraft.yaml
pyproject.toml
uv.lock
tox.ini
.jujuignore
icon.svg
README.md
CONTRIBUTING.md
src/
  charm.py            # events -> _reconcile -> collect_unit_status. No pebble calls.
  clickhouse.py       # ClickHouse container + HTTP API client. No ops imports.
  otelcol.py         # collector container, OTel config. No ops imports.
  hyperdx.py          # ClickStack UI container, ingestion API key. No ops imports.
  mongodb.py          # MongoDB container. No ops imports.
  clickstack_state.py # pure core: intent, state/outcome ADTs, fetch, compute
  clickstack_config.py# pydantic model of the config options
tests/
  unit/
    conftest.py
    test_charm.py
    test_clickstack_state.py
    test_clickhouse.py
    test_otelcol.py
    test_hyperdx.py
    test_mongodb.py
  integration/
    conftest.py
    test_deploy.py
```

The `src/charm.py` / workload-module split is mandatory. See
`k8s-charm-workload`.

## `.jujuignore`

```
/venv
/.venv
*.py[cod]
/.tox
/.git
/tests
/.opencode
__pycache__
.coverage
.ruff_cache
```

## Config options

Every option needs a `description` and, where meaningful, a `default`. Types:
`string`, `int`, `float`, `boolean`, `secret`.

The charm has **zero config options today**, and that is deliberate — see
AGENTS.md rule 4. Before adding one, apply the test: does another charm own
this data (→ relation), is it network placement (→ a Juju space), or is it
deployment shape the operator sets outside the charm? What survives all three
belongs in an ADR *and* a `Field(description=...)` that agrees with the
`charmcraft.yaml` copy — three copies of one vocabulary, and they rot apart.

For `type: secret`, the value is a secret URI; the charm calls
`self.model.get_secret(id=...)` and must observe `secret_changed`.

## Actions

Actions are the escape hatch for imperative operations that do not belong in
`_reconcile`. **Always set `additionalProperties` explicitly** — the default
differs between Juju 3 (`true`) and Juju 4 (`false`), so omitting it means the
behaviour changes under you.

```yaml
# Illustrative — none of these exist yet; each lands with an ADR.
actions:
  get-ingestion-key:
    description: Retrieve the OTLP ingestion API key.
    additionalProperties: false
  rotate-ingestion-key:
    description: Mint a new OTLP ingestion API key and rotate the secret.
    additionalProperties: false
```

Other valid action keys the reference lists: `parallel` (boolean) and
`execution-group` (string). `required` is a list of **parameter names** — the
example in the `charmcraft.yaml` reference page is buggy (it lists a
filename); the `actions.yaml` page has it right.

Action handlers are the one place per-event handlers are correct, and actions
are non-deferrable, which is the official test for "deserves its own
handler".

## Keys that do not apply to a Kubernetes charm

| Key / file | Why |
|---|---|
| `lxd-profile.yaml` | machine-charm-only, applies an LXD profile to the host container |
| `extra-bindings`-driven port logic, `Unit.set_ports` | `set_ports` is machine-only; K8s exposure is a Service / `juju expose` concern outside the charm |
| `subordinate:` | this charm is a principal; subordinates need `scope: container` requires |
| `charmlibs-snap` / `-systemd` / `-apt` | machine libraries; nothing in a pod can use them |

**Keys that do apply and are easy to forget**: `storage:` (`type: filesystem`
or `block` — this charm will need it for ClickHouse data and MongoDB before it
is production-usable), `peers:`, `links` (`contact`, `documentation`,
`issues`, `source`, `website`), `assumes: [k8s-api]` — recommended for K8s
charms per the reference.

For `links.documentation`, the reference is explicit: link the *charm's*
docs, not the application's. Do not point it at clickhouse.com/docs.

## `charmcraft` linters run during pack

`charmcraft pack` runs analyzers implicitly and a linter in error state
**blocks the pack** unless `--force`. Two attributes matter here — and both
were **verified FAILING for a `plugin: uv` charm** with charmcraft 4.3.1
(2026-08-08, pi-hole repo; NOT re-verified here — re-check when this repo
first packs):

1. **`entrypoint` / `language`.** `charmcraft/dispatch.py` emits
   `exec "${python_path}" "${dispatch_path}/src/charm.py"`, but
   `linters.py::get_entrypoint_from_dispatch` joins it to the base directory
   **without expanding the shell variable**, then looks for a literal
   `${dispatch_path}/src/charm.py` and fails. Affects every charm packed with
   charmcraft 4.x's generated dispatch.
2. **`framework`.** `_check_operator` requires `basedir/venv/ops` to be a
   *directory*. The `uv` plugin builds a real virtualenv, so `ops` lands at
   `venv/lib/pythonX.Y/site-packages/ops`. There is no `venv/ops`.

Consequences: `manifest.yaml` records `language: unknown` and
`framework: unknown` — this affects what Charmhub sees. **`charmcraft pack`
is not blocked** — the analysers are advisory unless a linter is in error
state.

Do **not** vendor a hand-written `dispatch` to work around #1 without reading
the `uv` plugin first: it deletes `venv/bin/python*` during build, so the
template's `ln -s $(which python3)` and `LD_LIBRARY_PATH` setup are
load-bearing at runtime. #2 cannot be fixed from inside a charm repo at all.

One thing that *is* ours: the entrypoint must be executable
(`chmod +x src/charm.py`), because `check_dispatch_with_python_entrypoint`
calls `os.access(entrypoint, os.X_OK)`.

Linters can be silenced via `analysis: {ignore: {attributes: [...],
linters: [...]}}`.
