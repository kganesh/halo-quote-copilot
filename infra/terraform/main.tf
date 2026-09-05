# The whole stack, and the shape of it is the point.
#
# Five modules, each backing something the code actually calls: Cognito because
# admission maps its claims, a Bedrock guardrail because M4 applies it, a bucket
# because M7 writes to it, a log group because something has to receive logs, and
# a policy because all of that needs permission.
#
# What is absent is as deliberate as what is here.
#
#   No VPC, and therefore no NAT gateway. NAT bills ~$32/month while idle,
#   whether or not a byte moves through it, and this application talks to Bedrock,
#   Cognito and S3 — all public endpoints. A VPC would exist to hold nothing.
#
#   No database. Atlas lives in SQLite next to the CLI. Aurora Serverless v2 has
#   a minimum capacity that bills per ACU-hour, and provisioning one for a corpus
#   of 25 documents would cost more per month than every model call this project
#   has made.
#
#   No container runtime. There is no long-running service: `halo` is a CLI, and
#   anything kept warm for latency bills continuously.
#
# Each of those is a module this stack would grow when there is something to put
# in it. Writing them now would mean infrastructure with no caller, which is the
# expensive kind of unused code.

locals {
  name = "halo-quote-copilot-${var.environment}"
}

data "aws_caller_identity" "current" {}

module "identity" {
  source = "./modules/identity"

  name = local.name
}

module "guardrail" {
  source = "./modules/guardrail"

  name = local.name
}

module "evidence" {
  source = "./modules/evidence"

  name           = local.name
  account_id     = data.aws_caller_identity.current.account_id
  retention_days = var.evidence_retention_days
}

module "observability" {
  source = "./modules/observability"

  name           = local.name
  retention_days = var.log_retention_days
}

module "access" {
  source = "./modules/access"

  name           = local.name
  region         = var.region
  account_id     = data.aws_caller_identity.current.account_id
  evidence_arn   = module.evidence.bucket_arn
  guardrail_arn  = module.guardrail.guardrail_arn
  log_group_arn  = module.observability.log_group_arn
  principal_arn  = var.principal_arn
}

# Free, and set before the first call rather than after the first surprise.
resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email == "" ? 0 : 1

  name         = local.name
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
