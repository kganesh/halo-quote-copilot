variable "region" {
  description = "AWS region. us-east-1 has the widest Bedrock model availability."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Suffix for resource names, so two stacks can coexist."
  type        = string
  default     = "practice"
}

variable "log_retention_days" {
  description = <<-EOT
    Days to keep CloudWatch logs. Set here rather than left at "never expire",
    which is the default and the one on this project's cost list: forgotten logs
    bill per GB-month, forever.
  EOT
  type        = number
  default     = 14
}

variable "evidence_retention_days" {
  description = "Days to keep the M7 event record before it expires."
  type        = number
  default     = 400
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget. Alerts; does not block."
  type        = number
  default     = 20
}

variable "budget_alert_email" {
  description = "Where budget alerts go. Empty disables the budget."
  type        = string
  default     = ""
}

variable "principal_arn" {
  description = <<-EOT
    The IAM user or role that runs `halo`. The access policy is attached to it.
    Empty creates the policy without attaching, which is what CI wants when the
    identity is an OIDC role created elsewhere.
  EOT
  type        = string
  default     = ""
}
