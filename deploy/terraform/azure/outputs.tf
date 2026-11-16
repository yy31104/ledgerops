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

output "assistant_fqdn" {
  description = "Assistant endpoint after explicit initialization/service enablement; null otherwise."
  value       = try(module.assistant[0].fqdn, null)
}

output "assistant_init_job_name" {
  description = "Manual assistant initialization job, never auto-started by Terraform."
  value       = try(module.assistant[0].init_job_name, null)
}
