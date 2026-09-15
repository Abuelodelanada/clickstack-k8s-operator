#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""A Juju charm for ClickStack on Kubernetes.

Stage 1 of the plan in DESIGN.md: run the upstream all-in-one image under Pebble, with
persistent storage and optional external ClickHouse. No relation endpoints, no TLS, no ingress.

The charm is holistic: :meth:`ClickStackK8sCharm._reconcile` runs on every event, recomputes the
desired Pebble layer from scratch, and applies it. There are deliberately no per-event handlers,
because there is no event whose handling differs. Status is not set inline anywhere; it is
derived at the end of the hook in ``collect-unit-status`` from the observable state of the
container, so it cannot drift out of sync with reality.
"""

import logging
from typing import cast

import ops

import clickstack

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when the charm's configuration cannot be turned into a workload configuration."""


class ClickStackK8sCharm(ops.CharmBase):
    """Charm ClickStack: ClickHouse, OpenTelemetry Collector, HyperDX and MongoDB."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container(clickstack.CONTAINER_NAME)
        self._config_error: str | None = None

        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)
        self._reconcile()

    # --- Properties -------------------------------------------------------------------------

    @property
    def _service_fqdn(self) -> str:
        """DNS name of the Kubernetes Service fronting this application."""
        return f"{self.app.name}.{self.model.name}.svc.cluster.local"

    @property
    def _external_url(self) -> str:
        """URL at which users reach the HyperDX UI in a browser.

        Falls back to the in-cluster Service address, which is only reachable from inside the
        cluster. Users fronting the UI with an ingress must set ``external-url``, because
        HyperDX bakes this value into the links it generates.
        """
        configured = cast(str, self.config.get("external-url") or "").strip().rstrip("/")
        if configured:
            return configured
        return f"http://{self._service_fqdn}:{clickstack.Port.ui.value}"

    # --- Configuration ----------------------------------------------------------------------

    def _clickhouse_connection(self) -> clickstack.ClickHouseConnection | None:
        """Resolve the external ClickHouse to use, if one is configured.

        Returns:
            The external connection, or None to use the ClickHouse bundled in the image.

        Raises:
            ConfigError: If credentials were configured but cannot be read.
        """
        endpoint = cast(str, self.config.get("clickhouse-endpoint") or "").strip()
        if not endpoint:
            return None
        return clickstack.ClickHouseConnection(
            endpoint=endpoint,
            username=cast(str, self.config.get("clickhouse-user") or ""),
            password=self._clickhouse_password(),
        )

    def _clickhouse_password(self) -> str:
        """Read the ClickHouse password out of the configured Juju user secret.

        Returns:
            The password, or an empty string if no secret is configured.

        Raises:
            ConfigError: If the secret is missing, not granted to this application, or does not
                contain a ``password`` key.
        """
        secret_id = cast(str, self.config.get("clickhouse-credentials") or "")
        if not secret_id:
            return ""
        try:
            content = self.model.get_secret(id=secret_id).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError) as e:
            raise ConfigError(
                "cannot read the 'clickhouse-credentials' secret; check that it exists and has "
                f"been granted to {self.app.name}"
            ) from e
        if "password" not in content:
            raise ConfigError(
                "the 'clickhouse-credentials' secret has no 'password' key",
            )
        return content["password"]

    # --- Reconciliation ---------------------------------------------------------------------

    def _reconcile(self) -> None:
        """Recompute and apply the whole desired state of the workload."""
        if not self.container.can_connect():
            logger.debug("Pebble is not up yet in the %s container", clickstack.CONTAINER_NAME)
            return

        try:
            environment = clickstack.environment(
                external_url=self._external_url,
                clickhouse=self._clickhouse_connection(),
            )
        except ConfigError as e:
            # Leave the workload running with its previous configuration rather than tearing it
            # down: a mistyped secret URI should not take the UI offline.
            logger.error("invalid configuration: %s", e)
            self._config_error = str(e)
            return

        self.unit.set_ports(*(port.value for port in clickstack.OPEN_PORTS))
        self.container.add_layer(
            clickstack.SERVICE_NAME,
            clickstack.pebble_layer(environment),
            combine=True,
        )
        # Pebble diffs the layer against the running service, so this is a no-op unless the
        # environment actually changed. Any change restarts the whole stack, which is inherent
        # to running all four components as one Pebble service.
        self.container.replan()

        if version := self._workload_version():
            self.unit.set_workload_version(version)

    def _workload_version(self) -> str | None:
        """Read the ClickStack version out of the workload container.

        The all-in-one image records it in the ``CODE_VERSION`` environment variable, which
        Pebble's exec inherits from the container's OCI configuration. A rock would expose this
        properly; until then this is best-effort.

        Returns:
            The version, or None if it could not be determined.
        """
        try:
            stdout, _ = self.container.exec(["printenv", "CODE_VERSION"]).wait_output()
        except ops.pebble.Error:
            logger.warning("could not determine the ClickStack version", exc_info=True)
            return None
        return stdout.strip() or None

    # --- Status -----------------------------------------------------------------------------

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        """Report unit status, derived from the observable state of the workload."""
        if self._config_error:
            event.add_status(ops.BlockedStatus(self._config_error))

        if self.app.planned_units() > 1:
            # ClickHouse and MongoDB are pod-local in the all-in-one image, so extra units are
            # independent stacks behind one round-robin Service: writes and reads land in
            # different databases. Blocking is the honest response until the workload is split
            # into separate charms (DESIGN.md, stage 3).
            event.add_status(
                ops.BlockedStatus(
                    f"ClickStack cannot be scaled; run: juju scale-application {self.app.name} 1"
                )
            )

        if not self.container.can_connect():
            event.add_status(
                ops.WaitingStatus(
                    f"waiting for Pebble in the {clickstack.CONTAINER_NAME} container"
                )
            )
        elif not self._service_is_running():
            event.add_status(ops.MaintenanceStatus("starting ClickStack"))
        elif pending := self._pending_checks():
            # Expect to sit here for minutes on a first boot: ClickHouse initialises its data
            # directory, the collector runs schema migrations, and HyperDX cold-starts Next.js.
            event.add_status(ops.MaintenanceStatus(f"waiting for {', '.join(pending)}"))

        event.add_status(ops.ActiveStatus())

    def _service_is_running(self) -> bool:
        """Report whether the ClickStack Pebble service is running.

        Returns:
            True if Pebble knows about the service and it is active.
        """
        service = self.container.get_services().get(clickstack.SERVICE_NAME)
        return service is not None and service.is_running()

    def _pending_checks(self) -> list[str]:
        """List the Pebble readiness checks that are not up yet.

        Returns:
            Names of the checks that have not passed, sorted for a stable status message.
        """
        checks = self.container.get_checks(level=ops.pebble.CheckLevel.READY)
        return sorted(
            name for name, check in checks.items() if check.status != ops.pebble.CheckStatus.UP
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(ClickStackK8sCharm)
