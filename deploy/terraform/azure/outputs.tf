output "api_fqdn" {
  description = "Public HTTPS endpoint of the API container app."
  value       = module.container_apps.api_fqdn
}

output "postgres_fqdn" {
  description = "PostgreSQL Flexible Server FQDN."
  value       = module.postgres.fqdn
}

output "key_vault_uri" {
  description = "Key Vault URI holding the runtime profiles."
  value       = module.key_vault.uri
}
