variable "name" {
  type        = string
  description = "Log Analytics workspace name."
}
variable "resource_group_name" {
  type        = string
  description = "Resource group."
}
variable "location" {
  type        = string
  description = "Region."
}
variable "tags" {
  type        = map(string)
  description = "Tags."
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  # Hard cap on ingestion so a log storm cannot run up the bill.
  daily_quota_gb = 1
  tags           = var.tags
}

output "workspace_id" {
  description = "Log Analytics workspace resource ID."
  value       = azurerm_log_analytics_workspace.this.id
}
