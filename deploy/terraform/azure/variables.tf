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
