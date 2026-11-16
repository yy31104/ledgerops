variable "prefix" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "container_app_environment_id" { type = string }
variable "image" { type = string }
variable "api_origin" { type = string }
variable "secret_ids" { type = map(string) }
variable "key_vault_id" { type = string }
variable "tags" { type = map(string) }
variable "service_enabled" { type = bool }
variable "external_ingress" { type = bool }

resource "azurerm_user_assigned_identity" "runtime" {
  name                = "id-${var.prefix}-assistant"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "initializer" {
  name                = "id-${var.prefix}-assistant-init"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

locals {
  grants = {
    runtime  = { secret = "assistant-runtime-database-url", identity = azurerm_user_assigned_identity.runtime.principal_id }
    owner    = { secret = "assistant-admin-database-url", identity = azurerm_user_assigned_identity.initializer.principal_id }
    password = { secret = "assistant-runtime-password", identity = azurerm_user_assigned_identity.initializer.principal_id }
  }
}

resource "azurerm_role_assignment" "secret" {
  for_each             = local.grants
  scope                = "${var.key_vault_id}/secrets/${each.value.secret}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value.identity
}

# Manual, one-shot bootstrap. The runtime identity cannot read owner credentials.
resource "azurerm_container_app_job" "initialize" {
  name                         = "job-${var.prefix}-assistant-init"
  location                     = var.location
  resource_group_name          = var.resource_group_name
  container_app_environment_id = var.container_app_environment_id
  replica_timeout_in_seconds   = 1800
  replica_retry_limit          = 0
  tags                         = var.tags
  depends_on                   = [azurerm_role_assignment.secret]
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.initializer.id]
  }
  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }
  dynamic "secret" {
    for_each = toset(["assistant-admin-database-url", "assistant-runtime-password"])
    content {
      name                = secret.value
      key_vault_secret_id = var.secret_ids[secret.value]
      identity            = azurerm_user_assigned_identity.initializer.id
    }
  }
  template {
    container {
      name    = "initialize"
      image   = var.image
      cpu     = 1
      memory  = "2Gi"
      command = ["python", "-m", "ledgerops_assistant.runtime", "init"]
      env {
        name        = "LEDGEROPS_ASSISTANT_DATABASE_URL"
        secret_name = "assistant-admin-database-url"
      }
      env {
        name        = "ASSISTANT_RUNTIME_PASSWORD"
        secret_name = "assistant-runtime-password"
      }
      env {
        name  = "HF_HOME"
        value = "/tmp/hf"
      }
      env {
        name  = "LEDGEROPS_ASSISTANT_EMBEDDER"
        value = "bge"
      }
    }
  }
}

# Provision only after successful initialization, avoiding an unindexed startup race.
resource "azurerm_container_app" "runtime" {
  count                        = var.service_enabled ? 1 : 0
  name                         = "ca-${var.prefix}-assistant"
  container_app_environment_id = var.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags
  depends_on                   = [azurerm_role_assignment.secret]
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }
  secret {
    name                = "assistant-runtime-database-url"
    key_vault_secret_id = var.secret_ids["assistant-runtime-database-url"]
    identity            = azurerm_user_assigned_identity.runtime.id
  }
  ingress {
    external_enabled           = var.external_ingress
    allow_insecure_connections = false
    target_port                = 8088
    transport                  = "http"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 0
    max_replicas = 1
    container {
      name   = "assistant"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"
      env {
        name        = "LEDGEROPS_ASSISTANT_DATABASE_URL"
        secret_name = "assistant-runtime-database-url"
      }
      env {
        name  = "LEDGEROPS_ASSISTANT_API_ORIGIN"
        value = var.api_origin
      }
      env {
        name  = "LEDGEROPS_ASSISTANT_CONSOLE_URL"
        value = var.api_origin
      }
      env {
        name  = "LEDGEROPS_ASSISTANT_PROVIDERS"
        value = "fake"
      }
      env {
        name  = "LEDGEROPS_ASSISTANT_EMBEDDER"
        value = "bge"
      }
      env {
        name  = "HF_HOME"
        value = "/tmp/hf"
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8088
        path      = "/health/live"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8088
        path      = "/health/ready"
      }
      startup_probe {
        transport               = "TCP"
        port                    = 8088
        interval_seconds        = 10
        failure_count_threshold = 60
      }
    }
  }
}

output "fqdn" {
  value       = try(azurerm_container_app.runtime[0].ingress[0].fqdn, null)
  description = "Endpoint after the explicit second-stage service enablement."
}
output "init_job_name" {
  value       = azurerm_container_app_job.initialize.name
  description = "Manual initialization job."
}
