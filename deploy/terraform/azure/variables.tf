variable "subscription_id" {
  description = "Azure subscription ID. Not needed for fmt/validate."
  type        = string
  default     = null
}

variable "prefix" {
  description = "Short lowercase name prefix for all resources."
  type        = string
  default     = "ledgerops"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.prefix))
    error_message = "prefix must be 3-12 lowercase letters/digits."
  }
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"
}

variable "image" {
  description = "Runtime image built from ops/linux/Dockerfile (target runtime), pushed to a registry you control."
  type        = string
}

variable "budget_amount" {
  description = "Monthly budget in the subscription currency."
  type        = number
  default     = 60
}

variable "budget_contact_emails" {
  description = "Who receives budget alerts."
  type        = list(string)
}

variable "budget_start_date" {
  description = "First day of the budget period, RFC3339 (must be the first of a month)."
  type        = string
  default     = "2026-12-01T00:00:00Z"
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = { project = "ledgerops", environment = "synthetic" }
}

variable "assistant_image" {
  description = "Optional digest-pinned assistant image; null creates no assistant resources."
  type        = string
  default     = null
  validation {
    condition     = var.assistant_image == null ? true : can(regex("@sha256:[0-9a-f]{64}$", var.assistant_image))
    error_message = "assistant_image must be null or pinned by sha256 digest."
  }
}

variable "assistant_service_enabled" {
  description = "Enable only after the manual initialization job succeeds."
  type        = bool
  default     = false
}

variable "assistant_external_ingress" {
  description = "Keep false until caller authentication and the public trial are approved."
  type        = bool
  default     = false
}

variable "key_vault_deployer_ipv4_cidrs" {
  description = "Explicit deployer /32 addresses allowed to populate Key Vault; never a broad network."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for address in var.key_vault_deployer_ipv4_cidrs : can(cidrnetmask(address)) && endswith(address, "/32")])
    error_message = "Key Vault deployer addresses must be individual IPv4 /32 CIDRs."
  }
}
