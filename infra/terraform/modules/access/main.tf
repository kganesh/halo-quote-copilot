# Exactly what `halo` needs, and nothing that would let it read the bucket back.
#
# The application writes evidence and never reads it: an investigation uses
# Athena over the partitions, as a person, with their own permissions. Granting
# s3:GetObject here would mean the process that redacts on the way in can also
# read everything it wrote, which is the shape of an exfiltration path rather
# than a feature anything uses.

variable "name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "evidence_arn" { type = string }
variable "guardrail_arn" { type = string }
variable "log_group_arn" { type = string }
variable "principal_arn" { type = string }

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "InvokeClaudeAndTitan"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Inference profiles and foundation models are separate ARN shapes, and a
    # policy naming only one of them fails at the surface the client happens to
    # pick. Both are listed rather than a wildcard on bedrock:*.
    resources = [
      "arn:aws:bedrock:${var.region}::foundation-model/*",
      "arn:aws:bedrock:${var.region}:${var.account_id}:inference-profile/*",
      "arn:aws:bedrock:*:${var.account_id}:inference-profile/*",
    ]
  }

  statement {
    sid       = "ApplyTheGuardrail"
    effect    = "Allow"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [var.guardrail_arn]
  }

  statement {
    sid       = "WriteEvidenceOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.evidence_arn}/events/*"]
  }

  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${var.log_group_arn}:*"]
  }

  statement {
    sid    = "ReadOwnSpend"
    effect = "Allow"
    # Cost Explorer has no resource-level permissions; this is the documented
    # shape. It is what `halo teardown --live` reads to prove the account went
    # back to zero.
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "app" {
  name        = "${var.name}-access"
  description = "What halo-quote-copilot needs: invoke, guard, write evidence, read own spend"
  policy      = data.aws_iam_policy_document.app.json
}

# Attached only when a principal was named. CI's identity is usually an OIDC role
# created elsewhere, and a stack that insisted on creating it would be a stack
# that cannot be applied twice in one account.
resource "aws_iam_role_policy_attachment" "app" {
  count = var.principal_arn == "" ? 0 : 1

  role       = element(split("/", var.principal_arn), length(split("/", var.principal_arn)) - 1)
  policy_arn = aws_iam_policy.app.arn
}

output "policy_arn" { value = aws_iam_policy.app.arn }
