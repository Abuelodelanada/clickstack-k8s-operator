# clickstack-k8s-operator

Juju **Kubernetes charm** that deploys and operates ClickStack — the ClickHouse
observability stack — in a single pod, using the sidecar pattern: the charm
process runs next to four Pebble workload containers.

This is not a machine charm. There are no snaps, no systemd, no `set_ports`,
no `lxd-profile.yaml`, no host to clean up on remove.

## Where we are

**Stage 0 — bootstrap. Nothing under `src/` or `docs/` exists yet.** The first
task is `docs/roadmap.md`; it defines the stages and is the source of truth —
write it before treating any missing feature as a defect rather than as
unstarted work.

Decided on 2026-09-15, recorded here until ADR-0001 lands: **one charm, the
full stack in one pod** — `clickhouse`, `otelcol` (the
`clickstack-otel-collector`), `hyperdx` (the ClickStack UI, HyperDX-derived),
and `mongodb` — deployed from the individual images the ClickStack compose
file uses, **not** the demo all-in-one image. The single pod is a single
failure and scaling domain; that trade-off is accepted for now and bringing
your own ClickHouse is deferred to `docs/BACKLOG.md`.

The public interface is deliberately undesigned: **zero relations, zero
config options, zero actions so far**. Adding the first `provides`/`requires`
— plausibly an OTLP-ingestion endpoint, since that is what ClickStack is
*for* — is a decision, not a detail. The charm owns the ingestion API key
(`HYPERDX_API_KEY` presets it; the UI's Get Started page displays it), which
is a Juju secret the charm mints, exactly like the admin-password pattern
below.

## Non-negotiables

Only rule 3 is machine-checked. **A green `tox -e lint,static,unit` is not
evidence of compliance with 1, 2, 4, 5, 6, 7, or 8.** Rule 5 in particular
looks checkable and is not: Juju ignores `optional`, and no tool reads it.
All of them are audited by `charm-reviewer`. Do not treat a passing gate as
a review.

1. **One reconciler.** Every observed event routes to a single `_reconcile`.
   Every reconcile step must be safe to run twice and safe to never run.
   The test for "deserves its own handler" is objective, from the ops docs:
   *an event that cannot be deferred needs a dedicated handler.* That set is
   exactly: actions, `stop`, `remove`, `secret_rotate`, `secret_remove`,
   `secret_expired`, and the `collect_*_status` lifecycle events. Everything
   deferrable — including `config_changed`, `upgrade_charm`, `secret_changed`,
   `leader_elected`, storage events, every relation event, and every Pebble
   event (`*_pebble_ready`, `pebble_custom_notice`, `pebble_check_failed`,
   `pebble_check_recovered`) — goes through `_reconcile`. The one allowed
   exception is `upgrade_charm` *if* it needs migration logic distinct from
   convergence.
2. **Charm logic and workload logic are separate modules.** `src/charm.py` only
   observes events, maps config to arguments, and reports status. All Pebble,
   HTTP-API and file manipulation lives in the workload modules — one per
   container: `src/clickhouse.py`, `src/otelcol.py`, `src/hyperdx.py`,
   `src/mongodb.py` — none of which ever imports `ops`. `src/clickstack_state.py`
   sits between them as the pure core and imports neither `ops` nor the
   workload. This is what makes unit tests possible — tests mock the module,
   never `ops.pebble` or an HTTP client. No linter checks this; if a test of
   `charm.py` patches `ops.pebble` or a socket, the boundary has already broken.
3. **No imports inside functions.** PEP 8 already says imports go at the top of
   the file; the reason it is restated here as absolute is charm-specific. A Juju
   hook runs once and exits, so an import inside a rarely-taken branch fails in
   production, in that branch, with a venv that differs from the one the tests
   ran against. An import at module top fails on the first hook instead.
   Enforced by `E402` **and `PLC0415`** — `E402` alone only catches late
   module-level imports, not function-level ones. `if TYPE_CHECKING:` blocks are
   at module top and therefore fine; if you need a function-level import to break
   a cycle, the `charm.py`/workload layering has been violated — fix that
   instead.
4. **Relations over config options.** A config option is a permanent public API:
   removing or renaming one breaks every existing deployment, the same class of
   irreversibility as `limit`. So before adding one, check the three alternatives
   in order — does another charm own this data (a relation)? is it network
   placement (a Juju space)? is it deployment shape the operator sets outside the
   charm (constraints, placement, their own deployment tooling)? Config options
   are the residue, not the default.
5. **Optional by default.** Every `requires`/`provides` entry gets
   `optional: true` unless the charm physically cannot reach `ActiveStatus`
   without it. The charm must come up clean with zero relations — which the
   closed-loop single-pod topology makes natural: the stack serves ingestion
   with no integrations at all. Note that Juju does **not** enforce `optional`
   — it is documentation. The guarantee lives in `_reconcile` and
   `collect_unit_status`, so a correct `charmcraft.yaml` is not evidence.
   (`limit`, by contrast, *is* enforced — and adding it later breaks
   `juju refresh` for existing users. See `charm-relations`.)
6. **Never trust a success signal you did not verify.** `Container.replan()` and
   `start_services()` return when Pebble accepted the request, not when the
   process serves; a service `is_running()` long before it answers requests. The
   same shape appears in `ops`: `Secret.set_content` succeeds and the unit errors
   at the *end* of the hook if permission was missing. In every case, read the
   state the operation was supposed to produce: the collector's health endpoint
   on `13133`, ClickHouse's `GET /ping` on `8123`, the UI answering on `8080`,
   the key back from the secret. An accepted Pebble plan is not evidence.
7. **Decide, then act — never both in one function.** A function that performs an
   effect *and* returns a flag describing what it decided cannot be tested without
   running the effect. Pure functions compute an outcome value; impure functions
   consume it. The detection signal is cheap: **if a test needs a mock to reach a
   decision, this rule was broken.** The inverse shape counts too: a boolean
   parameter that decides *whether* the function has effects — `f(generate=True)`
   from one caller, `f(generate=False)` from another — leaves the name unable to
   answer "does this mutate?", and puts the guarantee in an argument instead of in
   the type system. Split it into two named methods and let each name carry the
   answer; `_read_ingestion_key` and `_ensure_ingestion_key` in `src/charm.py` are
   the worked example — the intent is then built from whichever one the caller
   chose. See `charm-functional-style`.
8. **Inheritance only where a framework demands it.** `ops.CharmBase` is the one
   mandatory subclass; charm libraries are instantiated, never extended.
   Everything else is composition — but note the verified constraint:
   **constructor injection into the charm is impossible.** `ops` instantiates it as
   `charm_class(framework)` and `ops.testing.Context` takes a type, not a factory.
   So inject *below* the charm, in `ClickStack`, and do not invent a factory
   indirection to work around it. Pass the narrowest collaborator a function needs,
   never the charm itself.

## Toolchain

`uv` + `tox` + `ruff` + `pyright`. No `pip`, no `poetry`, no `black`/`isort`/`flake8`.

```
tox -e fmt        # ruff format + ruff check --fix
tox -e lint       # ruff check
tox -e static     # pyright
tox -e unit       # pytest tests/unit, coverage fail_under = 90
tox -e integration  # pytest tests/integration (needs a juju K8s model, e.g. microk8s)
tox -e lock       # regenerate uv.lock after changing pyproject.toml
tox -e flaplint   # advisory: relation-databag ordering churn. Not in envlist.
```

`tox.ini` sets `work_dir` outside the tree (override with `TOX_WORK_DIR`). That is
not a preference: tox builds its venvs with symlinks, a working tree on a network
mount may not allow them, and the half-built `.tox/` it leaves behind then lands in
`charmcraft pack`'s build context and breaks the pack. With it, the commands above
run verbatim anywhere.

**A change is not done until `fmt`, `lint`, `static` and `unit` are green.** That
is the floor, not the finish line — reread the non-negotiables above, because none
of those four gates can see rules 1, 2, 4, 5, 6, 7 or 8. Run `flaplint` as well
when the change touches a databag write, a file write, or a hash — a pebble layer
is a rendered file, so it counts.

`uv.lock` is committed. Dependencies go in `pyproject.toml`, never in
`charmcraft.yaml`'s `charm-libs` — that key is only for Charmhub-hosted libraries,
of which this charm needs none yet. When the first relation that requires one is
decided, name it in that ADR, not here.

## Layout

Planned — `src/` does not exist yet, so this is the target the roadmap works
toward, not a description of what is on disk:

```
charmcraft.yaml           # base: ubuntu@26.04, platforms: {amd64:, arm64:}
                          # containers: {clickhouse, otelcol, hyperdx, mongodb}
                          # resources: one oci-image per container
pyproject.toml            # ops, pydantic, tenacity — deps grow only via ADR
uv.lock
tox.ini
docs/
  overview.md             # two-minute map: the pattern, and what each src/ file is for
  pattern.md              # how the charm decides what to do, taught with a small example
  adr/                    # numbered decision records. Load `new-adr` before adding one.
  implementation/         # how an existing module works. One file per module, as it lands.
  roadmap.md              # staged delivery plan — the source of truth
  stack-constraints.md    # what the images cannot do, and the workarounds
  BACKLOG.md
src/
  charm.py                # ClickStackCharm: observe -> _reconcile -> collect_unit_status
  clickstack_state.py     # functional core: intent, state, outcome ADT, fetch/compute
  clickstack_config.py    # pydantic config model + the IntentFields TypedDict
  clickhouse.py           # workload: ClickHouse container + HTTP API client
  otelcol.py              # workload: collector container, OTel config, OTLP endpoints
  hyperdx.py              # workload: ClickStack UI container, ingestion API key
  mongodb.py              # workload: MongoDB container
tests/
  unit/                   # ops.testing, Model(type='kubernetes'), mocks src.<workload>
  integration/            # jubilant + pytest-jubilant on a K8s cloud
```

`src/clickstack_state.py` is the pure core: it holds `ClickStackIntent`, the
`ClickStackState` and `ClickStackOutcome` unions, `fetch`, and `compute`. It
imports neither `ops` nor anything that touches a container — it reaches the
workload only through the `ClickStackFacts` protocol, which is what keeps that
import out. See rule 2.

**`src/` is on `PYTHONPATH`, so imports are flat.** `tox.ini` sets
`PYTHONPATH={tox_root}/lib:{tox_root}/src`, which is what Juju's charm venv also
does. So it is `import clickhouse` and `import charm`, never
`from src.clickhouse import ...` — the latter works nowhere, in the charm or in
the tests.

No `lib/charms/` directory is expected until a decided relation requires a
Charmhub-hosted library; alert-rule and dashboard directories arrive only with
the relation that consumes them.

## Python conventions

Authority is [PEP 8](https://peps.python.org/pep-0008/) and
[PEP 257](https://peps.python.org/pep-0257/), enforced by `ruff`. Details and the
rule-family mapping live in the `python-style` skill.

Machine-checked:

- **PEP 8 with the 99-character exception**, which PEP 8 grants explicitly —
  *provided comments and docstrings stay wrapped at 72*. That proviso is the
  condition, not a suggestion: `E501` and `W505` are both enabled.
- Type annotations everywhere; `pyright` runs `typeCheckingMode = "strict"` over
  **both `src` and `tests`**, so a test helper needs the same annotations as
  production code. They also let `flaplint` resolve cross-object calls, so they buy
  correctness twice.
- Ruff's `select` is `E W F I N UP B C4 SIM RUF ANN D PLC0415`. Two consequences
  worth knowing before you write: **`ANN` makes annotations a lint error, and `D`
  does the same for docstrings** — neither is merely a house preference. No
  `from x import *` (`F403`/`F405`). No bare `except:` (`E722`).
- Python **3.14**, which is what `ubuntu@26.04` ships — and the *only* interpreter
  in that base's archive, so there is no fallback. The charm never runs on anything
  else, so `requires-python = ">=3.14"`, `ruff target-version = "py314"` and
  `pyright pythonVersion = "3.14"`. Do not write code that merely tolerates older
  interpreters. One consequence bites silently: `ruff format` rewrites
  `except (A, B):` into PEP 758's unparenthesized form, which makes `flaplint`
  skip the module without saying so. Give multi-type `except` clauses an
  `as err:` binding — see `python-style`.
- **Type suppressions are checked, and only one spelling works.** `[tool.pyright]`
  sets `enableTypeIgnoreComments = false` and
  `reportUnnecessaryTypeIgnoreComment = "error"`, neither of which `strict` gives
  you. So `# type: ignore[...]` — mypy's spelling — suppresses nothing, and the one
  honoured form, `# pyright: ignore[rule]`, fails the gate once it is no longer
  needed. Before these settings the inherited repo carried seven of the mypy form,
  four of them unnecessary and all of them silently blanket-suppressing their whole
  line.
- **Frozen dataclasses everywhere except exceptions.** `ops` assigns
  `exc.__traceback__` as the event context unwinds, so a frozen exception raises
  `FrozenInstanceError` and buries the real failure. Each exception module carries
  a guard test asserting its exceptions survive being raised.

Not machine-checked — the reviewer's job:

- **Prefer the functional style.** Frozen dataclasses, unions as ADTs, exhaustive
  `match` with `assert_never`, `Mapping`/`Sequence`/`FrozenSet` in signatures.
  Functional core, imperative shell. See `charm-functional-style` — including what
  we deliberately do *not* adopt from `fp-edge-canonical`.
- `pydantic` for anything parsed or serialised: charm config (via
  `self.load_config`), databags (via `Relation.load`/`save`), and the config the
  charm renders into the containers — Pebble layers, the collector's OTel config,
  the env wiring the compose file shows.
- Logging via `logging.getLogger(__name__)`, never `print`. `ops.main` already
  wires this to `juju-log`, so no setup is needed.
- No `except Exception:` where a narrower exception is what actually occurs. Ruff's
  `BLE001` is deliberately not enabled because it cannot tell the difference — this
  one is judgement.
- Tests use `# GIVEN / # WHEN / # THEN` comments and live in `conftest.py`-backed
  fixtures rather than per-file setup boilerplate.
- **Docstrings say what; ADRs say why.** `D` is enforced, so the floor is one
  imperative line plus `Raises:` where the caller must handle it — but design
  rationale belongs in `docs/adr/`. Cite it (`See ADR-0001 section 2`) instead of
  paraphrasing it: a docstring that restates an ADR goes stale and then wins by
  proximity. Keep a rationale inline only where a reader would otherwise plausibly
  "fix" the code, and then as one sentence. A comment explains why *this line*, at
  the line, in two lines or fewer.
- **`# databag-order: ignore` suppresses one `flaplint` finding on one line.** It
  is legitimate only where the nondeterminism is the point and cannot flap: the
  intended first use is on minting the ingestion key, where the value is a fresh
  random token written exactly once. A suppression on a line that runs on every
  reconcile is a defect being silenced — fix the ordering instead.

## Ecosystem facts that bite (2026)

- Charmhub-hosted charm libraries (`charmcraft fetch-lib`, `LIBPATCH`/`LIBAPI`)
  are **being phased out** in favour of PyPI packages. Do not create new
  `lib/charms/...` files for code this repo owns.
- `charmlibs-snap`/`charmlibs-systemd`/`charmlibs-apt` are **machine-charm**
  libraries; nothing in a K8s charm can use them. In-pod workload management is
  `ops`' own Pebble API — `self.unit.get_container(...)`, `ops.pebble` — and
  `lightkube` only for cluster resources beyond the pod that the charm genuinely
  owns. Do not reach for `lightkube` to do something the `Container` API already
  does.
- COS on Kubernetes is **in-stack** (`metrics-endpoint`, `logging`,
  `grafana-dashboard`, `tracing`), not the machine-subordinate `cos_agent` /
  `grafana_agent.cos_agent` pattern — do not vendor that library here. Note the
  inversion: ClickStack *is* an observability backend, so which side of which
  interface it sits on (ingesting from COS agents, being scraped, or both) is an
  open design decision, not a default.
- `bases:` in `charmcraft.yaml` is deprecated. Use `base:` + `platforms:`.
- `Unit.set_ports` is **machine-only** and has no K8s equivalent — do not copy it
  from a machine-charm example. Exposure is a service-mesh / `juju expose`
  concern outside the charm.

## Agents and skills in this repo

Three agents, three jobs. Design decisions go to `charm-architect`,
implementation to `charm-engineer`, and audits to `charm-reviewer` (read-only).
Research delegates to `explore` (this repo) and `general` (the upstream
`references`), both on a cheaper model.

**Load the relevant skill instead of guessing.** Their names and trigger
conditions are already in your system prompt, so this file does not restate
them. They carry verified, sourced, dated detail — so where a skill and this
file disagree, the skill is newer and wins, and this file is the thing to fix.

The agents and skills were inherited from the pi-hole machine charm and
adapted on 2026-09-15. Skill names to expect: `k8s-charm-scaffold`,
`k8s-charm-workload`, `clickstack-stack`, `charm-relations`, `charm-testing`,
`charm-cos-integration`, `charm-functional-style`, `new-adr`, `python-style`.

Decisions live in `docs/adr/`, numbered and dated. `src/charm.py` cites them by
number in comments, so an ADR is not optional documentation — it is where the
reason for a rule lives once the rule is no longer obvious. Load `new-adr`
before adding or revising one.
