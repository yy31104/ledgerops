resource "azurerm_resource_group" "this" {
  name     = "rg-${var.prefix}"
  location = var.location
  tags     = var.tags
}

data "azurerm_client_config" "current" {}

module "logging" {
  source              = "./modules/logging"
  name                = "log-${var.prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = var.tags
}

module "key_vault" {
  source                     = "./modules/key_vault"
  name                       = "kv-${var.prefix}-${substr(md5(azurerm_resource_group.this.id), 0, 6)}"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  deployer_object_id         = data.azurerm_client_config.current.object_id
  log_analytics_workspace_id = module.logging.workspace_id
  tags                       = var.tags
}

module "postgres" {
  source                     = "./modules/postgres"
  name                       = "psql-${var.prefix}-${substr(md5(azurerm_resource_group.this.id), 0, 6)}"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  key_vault_id               = module.key_vault.id
  log_analytics_workspace_id = module.logging.workspace_id
  tags                       = var.tags
}

module "container_apps" {
  source                     = "./modules/container_apps"
  prefix                     = var.prefix
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = module.logging.workspace_id
  key_vault_id               = module.key_vault.id
  secret_ids                 = module.key_vault.profile_secret_ids
  image                      = var.image
  tags                       = var.tags
}

module "budget" {
  source            = "./modules/budget"
  name              = "budget-${var.prefix}"
  resource_group_id = azurerm_resource_group.this.id
  amount            = var.budget_amount
  start_date        = var.budget_start_date
  contact_emails    = var.budget_contact_emails
}
