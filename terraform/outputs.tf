output "app_name" {
  value = juju_application.clickstack.name
}

# ClickStack provides no relation endpoints yet; see DESIGN.md stage 2.
output "provides" {
  value = {}
}

output "requires" {
  value = {
    ingress = "ingress",
  }
}
