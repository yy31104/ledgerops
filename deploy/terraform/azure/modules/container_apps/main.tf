variable "prefix" {
  type        = string
  description = "Name prefix."
}
variable "resource_group_name" {
  type        = string
  description = "Resource group."
}
variable "location" {
  type        = string
  description = "Region."
}
variable "log_analytics_workspace_id" {
  type        = string
  description = "Container Apps environment log destination."
}
variable "key_vault_id" {
  type        = string
  description = "Key Vault holding runtime profiles."
}
variable "secret_ids" {
  type        = map(string)
  description = "Versionless Key Vault secret IDs keyed by name."
}
variable "image" {
  type        = string
  description = "LedgerOps runtime image."
}
variable "tags" {
  type        = map(string)
  description = "Tags."
}

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.prefix}"
  resource_group_name        = var.resource_group_name
  location                   = var.location
  log_analytics_workspace_id = var.log_analytics_workspace_id
  tags                       = var.tags
}

# One identity per app so the API cannot read worker/provider secrets.
resource "azurerm_user_assigned_identity" "app" {
  for_each            = toset(["api", "dispatch"])
  name                = "id-${var.prefix}-${each.key}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

locals {
  grants = {
    api-api           = { app = "api", secret = "api-profile" }
    api-policy        = { app = "api", secret = "payout-policy" }
    dispatch-worker   = { app = "dispatch", secret = "worker-profile" }
    dispatch-provider = { app = "dispatch", secret = "provider-profile" }
    dispatch-keys     = { app = "dispatch", secret = "provider-keys" }
  }
  # The launcher needs UID-owned mode-0600 files; mounted secrets are root-owned, so copy.
  copy = "mkdir -p /private && cp -L /secrets/* /private/ && chmod 0600 /private/*"
}

resource "azurerm_role_assignment" "secret" {
  for_each             = local.grants
  scope                = "${var.key_vault_id}/secrets/${each.value.secret}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app[each.value.app].principal_id
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${var.prefix}-api"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app["api"].id]
  }

  secret {
    name                = "profile-json"
    key_vault_secret_id = var.secret_ids["api-profile"]
    identity            = azurerm_user_assigned_identity.app["api"].id
  }
  secret {
    name                = "payout-policy-json"
    key_vault_secret_id = var.secret_ids["payout-policy"]
    identity            = azurerm_user_assigned_identity.app["api"].id
  }

  ingress {
    external_enabled = true
    target_port      = 8081
    transport        = "http"
    # Envoy terminates TLS, then the sidecar forwards over loopback to Gunicorn.
    allow_insecure_connections = false
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 1
    max_replicas = 3

    init_container {
      name    = "copy-secrets"
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["sh", "-c", replace(local.copy, "/private", "/run/ledgerops")]
      volume_mounts {
        name = "secrets"
        path = "/secrets"
      }
      volume_mounts {
        name = "private"
        path = "/run/ledgerops"
      }
    }

    container {
      name   = "api"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"
      args   = ["service"]
      env {
        name  = "LEDGEROPS_RESTORE_MODE"
        value = "true"
      }
      env {
        name  = "LEDGEROPS_API_WRITES_ENABLED"
        value = "false"
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/health/live"
        header {
          name  = "Host"
          value = "localhost"
        }
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/health/ready"
        header {
          name  = "Host"
          value = "localhost"
        }
      }
      volume_mounts {
        name = "private"
        path = "/run/ledgerops"
      }
    }

    container {
      name    = "https-proxy"
      image   = "caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f"
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["/bin/sh", "-ec"]
      args    = ["printf '%s' \"$CADDYFILE_BASE64\" | base64 -d > /tmp/Caddyfile; exec caddy run --config /tmp/Caddyfile --adapter caddyfile"]
      env {
        name  = "CADDYFILE_BASE64"
        value = filebase64("${path.module}/Caddyfile")
      }
      liveness_probe {
        transport = "TCP"
        port      = 8081
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8081
        path      = "/health/ready"
        header {
          name  = "Host"
          value = "localhost"
        }
      }
    }

    volume {
      name         = "secrets"
      storage_type = "Secret"
    }
    volume {
      name         = "private"
      storage_type = "EmptyDir"
    }
  }
}

# Provider + worker in one app: the simulator binds 127.0.0.1, so they must share a network
# namespace (same reason as deploy/k8s). Provider SQLite is ephemeral here: see limitations.
resource "azurerm_container_app" "dispatch" {
  name                         = "ca-${var.prefix}-dispatch"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app["dispatch"].id]
  }

  secret {
    name                = "worker-profile"
    key_vault_secret_id = var.secret_ids["worker-profile"]
    identity            = azurerm_user_assigned_identity.app["dispatch"].id
  }
  secret {
    name                = "provider-profile"
    key_vault_secret_id = var.secret_ids["provider-profile"]
    identity            = azurerm_user_assigned_identity.app["dispatch"].id
  }
  secret {
    name                = "provider-keys"
    key_vault_secret_id = var.secret_ids["provider-keys"]
    identity            = azurerm_user_assigned_identity.app["dispatch"].id
  }

  template {
    # Exactly one: never two workers or two SQLite writers.
    min_replicas = 1
    max_replicas = 1

    init_container {
      name    = "copy-secrets"
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["sh", "-c", "mkdir -p /private/worker /private/provider && cp -L /secrets/worker-profile /private/worker/profile.json && cp -L /secrets/provider-keys /private/worker/provider-keys.json && cp -L /secrets/provider-profile /private/provider/profile.json && chmod 0600 /private/worker/* /private/provider/*"]
      volume_mounts {
        name = "secrets"
        path = "/secrets"
      }
      volume_mounts {
        name = "private"
        path = "/private"
      }
    }

    container {
      name   = "provider"
      image  = var.image
      cpu    = 0.25
      memory = "0.5Gi"
      args   = ["provider"]
      volume_mounts {
        name     = "private"
        path     = "/run/ledgerops"
        sub_path = "provider"
      }
      volume_mounts {
        name = "provider-data"
        path = "/var/lib/ledgerops-provider"
      }
    }

    container {
      name   = "worker"
      image  = var.image
      cpu    = 0.25
      memory = "0.5Gi"
      args   = ["worker"]
      env {
        name  = "LEDGEROPS_DISPATCH_ENABLED"
        value = "false"
      }
      volume_mounts {
        name     = "private"
        path     = "/run/ledgerops"
        sub_path = "worker"
      }
    }

    volume {
      name         = "secrets"
      storage_type = "Secret"
    }
    volume {
      name         = "private"
      storage_type = "EmptyDir"
    }
    volume {
      name         = "provider-data"
      storage_type = "EmptyDir"
    }
  }
}

output "api_fqdn" {
  description = "API ingress FQDN."
  value       = azurerm_container_app.api.ingress[0].fqdn
}
