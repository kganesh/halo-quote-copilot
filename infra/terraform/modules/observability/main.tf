# Where logs go, and when they stop being kept.
#
# The retention is set in the same resource that creates the group, not in a
# follow-up. A CloudWatch log group defaults to "never expire", and the default
# is the expensive one: it bills per GB-month for as long as the account exists,
# long after anyone remembers what was writing to it.
#
# There is no OTLP collector here. Traces go wherever `telemetry.configure` is
# pointed, and a collector is a container that bills continuously — for a
# practice stack, the console exporter and `halo run --trace` answer the same
# question for nothing.

variable "name" { type = string }
variable "retention_days" { type = number }

resource "aws_cloudwatch_log_group" "app" {
  name              = "/halo/${var.name}"
  retention_in_days = var.retention_days
}

output "log_group_arn" { value = aws_cloudwatch_log_group.app.arn }
output "log_group_name" { value = aws_cloudwatch_log_group.app.name }
