variable "name" {
  type        = string
  description = "Budget name."
}
variable "resource_group_id" {
  type        = string
  description = "Scope of the budget."
}
variable "amount" {
  type        = number
  description = "Monthly amount."
}
variable "start_date" {
  type        = string
  description = "First day of a month, RFC3339."
}
variable "contact_emails" {
  type        = list(string)
  description = "Alert recipients."
}

resource "azurerm_consumption_budget_resource_group" "this" {
  name              = var.name
  resource_group_id = var.resource_group_id
  amount            = var.amount
  time_grain        = "Monthly"

  time_period {
    start_date = var.start_date
  }

  # 50 % and 90 % of actual spend, plus 100 % of forecast.
  notification {
    enabled        = true
    threshold      = 50
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Actual"
    contact_emails = var.contact_emails
  }
  notification {
    enabled        = true
    threshold      = 90
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Actual"
    contact_emails = var.contact_emails
  }
  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Forecasted"
    contact_emails = var.contact_emails
  }
}
