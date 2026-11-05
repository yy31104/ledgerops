variable "name" {
  type        = string
  description = "Server name."
}
variable "resource_group_name" {
  type        = string
  description = "Resource group."
}
variable "location" {
  type        = string
  description = "Region."
}
variable "key_vault_id" {
  type        = string
  description = "Key Vault that stores the administrator password."
}
variable "log_analytics_workspace_id" {
  type        = string
  description = "Destination for server logs."
}
variable "tags" {
  type        = map(string)
  description = "Tags."
}

# The admin password is generated here and therefore lives in Terraform state:
# keep state in an encrypted remote backend with restricted access (docs/AZURE.md).
resource "random_password" "admin" {
  length  = 32
  special = false
}

resource "azurerm_postgresql_flexible_server" "this" {
  name                          = var.name
  resource_group_name           = var.resource_group_name
  location                      = var.location
  version                       = "17"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  backup_retention_days         = 7
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = true
  administrator_login           = "ledgeradmin"
  administrator_password        = random_password.admin.result
  zone                          = "1"
  tags                          = var.tags

  authentication {
    password_auth_enabled = true
  }
}

resource "azurerm_postgresql_flexible_server_database" "ledgerops" {
  name      = "ledgerops"
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Matches ops/linux: TLS required, connection/lock logging on.
resource "azurerm_postgresql_flexible_server_configuration" "settings" {
  for_each = {
    require_secure_transport = "on"
    log_connections          = "on"
    log_disconnections       = "on"
    log_checkpoints          = "on"
    connection_throttling    = "on"
    max_connections          = "50"
  }
  name      = each.key
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = each.value
}

# Only Azure-internal sources (Container Apps egress). 0.0.0.0-0.0.0.0 is Azure's special
# "allow Azure services" rule, not the internet. Use VNet integration for production.
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_key_vault_secret" "admin" {
  name            = "postgres-admin-password"
  value           = random_password.admin.result
  key_vault_id    = var.key_vault_id
  content_type    = "text/plain"
  expiration_date = "2027-12-31T00:00:00Z"
}

resource "azurerm_monitor_diagnostic_setting" "logs" {
  name                       = "postgres-logs"
  target_resource_id         = azurerm_postgresql_flexible_server.this.id
  log_analytics_workspace_id = var.log_analytics_workspace_id
  enabled_log {
    category = "PostgreSQLLogs"
  }
  enabled_metric {
    category = "AllMetrics"
  }
}

output "fqdn" {
  description = "Server FQDN."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}
