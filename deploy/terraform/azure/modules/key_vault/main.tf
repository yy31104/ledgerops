variable "name" {
  type        = string
  description = "Key Vault name (globally unique, <= 24 chars)."
}
variable "resource_group_name" {
  type        = string
  description = "Resource group."
}
variable "location" {
  type        = string
  description = "Region."
}
variable "tenant_id" {
  type        = string
  description = "Entra tenant ID."
}
variable "deployer_object_id" {
  type        = string
  description = "Object ID of whoever runs terraform; needs to write secrets."
}
variable "log_analytics_workspace_id" {
  type        = string
  description = "Destination for audit logs."
}
variable "tags" {
  type        = map(string)
  description = "Tags."
}

locals {
  # One launcher profile per component, same shape as ops/linux/bootstrap.py writes.
  # Terraform only creates placeholders; real values are set out-of-band (docs/AZURE.md),
  # so DSNs and tokens never enter Terraform state.
  profiles = ["api-profile", "worker-profile", "provider-profile", "provider-keys", "payout-policy"]
}

resource "azurerm_key_vault" "this" {
  #checkov:skip=CKV_AZURE_189:Cost tradeoff; default_action=Deny limits access to listed IPs. Use a private endpoint for real data.
  #checkov:skip=CKV2_AZURE_32:No VNet in the low-cost profile; see docs/AZURE.md "Hardening".
  name                       = var.name
  resource_group_name        = var.resource_group_name
  location                   = var.location
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 7
  tags                       = var.tags

  network_acls {
    bypass         = "AzureServices"
    default_action = "Deny"
    # Add the deployer's IP here (or use a private endpoint) before writing secrets.
    ip_rules = []
  }
}

resource "azurerm_role_assignment" "deployer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.deployer_object_id
}

resource "azurerm_key_vault_secret" "profile" {
  for_each        = toset(local.profiles)
  name            = each.value
  value           = "PLACEHOLDER-set-with-az-keyvault-secret-set"
  key_vault_id    = azurerm_key_vault.this.id
  content_type    = "application/json"
  expiration_date = "2027-12-31T00:00:00Z"
  depends_on      = [azurerm_role_assignment.deployer]

  lifecycle {
    ignore_changes = [value]
  }
}

resource "azurerm_monitor_diagnostic_setting" "audit" {
  name                       = "audit"
  target_resource_id         = azurerm_key_vault.this.id
  log_analytics_workspace_id = var.log_analytics_workspace_id
  enabled_log {
    category = "AuditEvent"
  }
}

output "id" {
  description = "Key Vault ID."
  value       = azurerm_key_vault.this.id
}
output "uri" {
  description = "Key Vault URI."
  value       = azurerm_key_vault.this.vault_uri
}
output "profile_secret_ids" {
  description = "Versionless secret IDs, keyed by name."
  value       = { for k, s in azurerm_key_vault_secret.profile : k => s.versionless_id }
}
