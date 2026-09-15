resource "juju_application" "clickstack" {
  name               = var.app_name
  config             = var.config
  constraints        = var.constraints
  model_uuid         = var.model_uuid
  resources          = var.resources
  storage_directives = var.storage_directives
  units              = 1 # ClickStack cannot be scaled; see DESIGN.md.

  charm {
    base     = var.base
    name     = "clickstack-k8s"
    channel  = var.channel
    revision = var.revision
  }
}

check "storage_directives" {
  assert {
    condition     = contains(keys(var.storage_directives), "clickhouse-data")
    error_message = "clickhouse-data is unset, so it will use the default 1G volume. Size it for your retention before deploying: growing a persistent volume afterwards requires manual steps."
  }
}
