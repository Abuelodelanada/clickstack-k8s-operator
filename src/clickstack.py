#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Workload-specific knowledge about ClickStack, with no charming concerns.

Everything in here describes *what* the ClickStack all-in-one image needs in order to run. The
charm in ``charm.py`` is responsible for working out the inputs from Juju and applying the
result to the container.

The all-in-one image bundles four components behind a single shell entrypoint:

* ``clickhouse``      - the telemetry store
* ``mongod``          - HyperDX application state (users, dashboards, alerts)
* ``opampsupervisor`` - supervises the OpenTelemetry Collector
* ``hyperdx``         - the UI, its API, and the alerting task runner

``/etc/local/entry.sh`` starts all four in the background and then blocks on ``wait -n``, so it
exits as soon as any one of them dies. That makes it usable as a single Pebble service: Pebble
notices the exit and restarts the stack. It is not a *good* arrangement, and replacing it with
one Pebble service per component is the main reason we need a rock. See DESIGN.md.
"""

import enum
import json
from dataclasses import dataclass

from ops.pebble import CheckDict, HttpDict, LayerDict, TcpDict

CONTAINER_NAME = "clickstack"
"""Name of the workload container, as declared in charmcraft.yaml."""

SERVICE_NAME = "clickstack"
"""Name of the single Pebble service that runs the whole stack."""

ENTRYPOINT = "sh /etc/local/entry.sh"
"""The image's own ``Entrypoint``.

Juju replaces the container's entrypoint with Pebble, so we have to reinstate it ourselves.
"""

WORKING_DIR = "/app"
"""The image's own ``WorkingDir``.

Pebble does not inherit it, and ``entry.sh`` resolves several paths relative to it (for example
``./node_modules/.bin/concurrently``), so omitting this stops HyperDX from ever starting.
"""


class Port(enum.IntEnum):
    """Ports used by the components inside the workload container."""

    ui = 8080
    """HyperDX web UI."""
    api = 8000
    """HyperDX API. Only reached by the UI, so not opened on the unit."""
    otlp_grpc = 4317
    otlp_http = 4318
    clickhouse_http = 8123
    clickhouse_native = 9000
    """ClickHouse native protocol. Not opened on the unit: nothing outside needs it yet."""
    opamp = 4320
    """OpAMP server the collector's supervisor talks to. Container-local."""


OPEN_PORTS = (Port.ui, Port.otlp_grpc, Port.otlp_http, Port.clickhouse_http)
"""Ports opened on the unit, matching what the upstream getting-started guide publishes."""

IMAGE_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "en_US.UTF-8",
    "LANGUAGE": "en_US:en",
    "LC_ALL": "en_US.UTF-8",
    "TZ": "UTC",
    "CLICKHOUSE_CONFIG": "/etc/clickhouse-server/config.xml",
    "NODE_ENV": "production",
}
"""Environment baked into the image's OCI config.

Restated explicitly rather than relying on Pebble inheriting the daemon's environment, so that
the service's environment is fully described by the layer and does not silently change if the
image or Pebble's inheritance behaviour does.
"""


@dataclass(frozen=True)
class ClickHouseConnection:
    """An external ClickHouse to use instead of the bundled one."""

    endpoint: str
    """HTTP(S) endpoint including the port, e.g. ``https://abc.clickhouse.cloud:8443``."""
    username: str
    password: str

    @property
    def default_connections(self) -> str:
        """The ``DEFAULT_CONNECTIONS`` value that seeds this connection in the HyperDX UI.

        Without this, the UI is seeded with a connection to the bundled ClickHouse on
        ``localhost:8123`` while the collector writes to the external one, so the UI would
        show no data.
        """
        return json.dumps(
            [
                {
                    "name": "External ClickHouse",
                    "host": self.endpoint,
                    "username": self.username,
                    "password": self.password,
                }
            ]
        )


def environment(
    *,
    external_url: str,
    clickhouse: ClickHouseConnection | None = None,
) -> dict[str, str]:
    """Build the environment for the ClickStack Pebble service.

    Only variables that ``entry.base.sh`` actually honours are set here. It unconditionally
    overwrites ``HYPERDX_LOG_LEVEL``, ``CLICKHOUSE_LOG_LEVEL``, ``MONGO_URI``, ``SERVER_URL``
    and ``OPAMP_SERVER_URL``, so those cannot be configured from the charm.

    Args:
        external_url: URL at which users reach the HyperDX UI in a browser. HyperDX bakes this
            into generated links, so it has to be the externally reachable address.
        clickhouse: External ClickHouse to write to, or None to use the bundled instance.

    Returns:
        The environment to put in the Pebble layer.
    """
    env = {
        **IMAGE_ENVIRONMENT,
        "FRONTEND_URL": external_url,
        "HYPERDX_APP_PORT": str(Port.ui.value),
        "HYPERDX_API_PORT": str(Port.api.value),
    }
    if clickhouse is not None:
        env.update(
            {
                "CLICKHOUSE_ENDPOINT": clickhouse.endpoint,
                "CLICKHOUSE_USER": clickhouse.username,
                "CLICKHOUSE_PASSWORD": clickhouse.password,
                "DEFAULT_CONNECTIONS": clickhouse.default_connections,
            }
        )
    return env


def pebble_layer(environment: dict[str, str]) -> LayerDict:
    """Build the Pebble layer that runs ClickStack.

    Args:
        environment: Environment for the service, as returned by :func:`environment`.

    Returns:
        A Pebble layer definition.
    """
    return LayerDict(
        summary="clickstack layer",
        description="Runs the ClickStack all-in-one entrypoint",
        services={
            SERVICE_NAME: {
                "override": "replace",
                "summary": "ClickHouse, MongoDB, OpenTelemetry Collector and HyperDX",
                "command": ENTRYPOINT,
                "working-dir": WORKING_DIR,
                "startup": "enabled",
                "environment": environment,
            }
        },
        checks=pebble_checks(),
    )


def pebble_checks() -> dict[str, CheckDict]:
    """Build the Pebble checks for the workload container.

    Both checks are deliberately ``ready`` and not ``alive``. A first boot runs ClickHouse
    initialisation, the collector's ClickHouse schema migrations and a Next.js cold start, which
    together take minutes; an ``alive`` check would kill and restart the service part-way
    through and never converge. ``ready`` lets the charm report ``maintenance`` while that
    happens, and leaves crash detection to Pebble's own service restart logic, which works
    because ``entry.sh`` exits when any component dies.

    Returns:
        Pebble check definitions, keyed by check name.
    """
    return {
        # ClickHouse answers "Ok." on /ping once it has finished initialising its data
        # directory, which is the gate the entrypoint itself waits on.
        "clickhouse-ready": CheckDict(
            override="replace",
            level="ready",
            startup="enabled",
            period="30s",
            timeout="10s",
            threshold=3,
            http=HttpDict(url=f"http://localhost:{Port.clickhouse_http.value}/ping"),
        ),
        # The UI root redirects to /login, so an HTTP check would see a 302 and fail. A TCP
        # check is enough to know the Next.js server finished starting.
        "hyperdx-ready": CheckDict(
            override="replace",
            level="ready",
            startup="enabled",
            period="30s",
            timeout="10s",
            threshold=3,
            tcp=TcpDict(port=Port.ui.value),
        ),
    }
