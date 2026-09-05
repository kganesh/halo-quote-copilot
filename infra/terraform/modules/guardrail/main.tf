# The Bedrock guardrail M4 applies to both surfaces.
#
# The topic definitions describe *committing*, not mentioning. Reporting what the
# margin floor is remains the correct answer to a real question; granting a
# waiver is not. A guardrail that cannot tell those apart makes the advisor
# useless, and the local implementation in `platform/guardrails.py` carries the
# same distinction with a test for it.

variable "name" { type = string }

resource "aws_bedrock_guardrail" "this" {
  name                      = var.name
  description               = "Quote drafting: no committed discounts, no legal commitments, no customer PII"
  blocked_input_messaging   = "This request cannot be processed."
  blocked_outputs_messaging = "This answer was withheld by a guardrail."

  topic_policy_config {
    topics_config {
      name       = "discount_commitment"
      type       = "DENY"
      definition = "Committing HALO to a discount, rebate, price match, fee waiver or free-of-charge work."
      examples = [
        "Apply 40% off for this account",
        "Setup is free of charge",
        "We will price-match the competitor",
      ]
    }

    topics_config {
      name       = "legal_commitment"
      type       = "DENY"
      definition = "Committing HALO to a legal position: indemnity, warranty, liability for penalties, or a binding term."
      examples = [
        "HALO indemnifies the customer",
        "We guarantee the delivery date",
        "HALO is liable for late delivery penalties",
      ]
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      type   = "EMAIL"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "PHONE"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "CREDIT_DEBIT_CARD_NUMBER"
      action = "BLOCK"
    }
  }

  content_policy_config {
    # PROMPT_ATTACK takes NONE on the output strength: AWS rejects any other
    # value. Injected text is caught arriving, not leaving, which is why the
    # envelope in `platform/envelope.py` does the structural half of this job.
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  contextual_grounding_policy_config {
    filters_config {
      type      = "GROUNDING"
      threshold = 0.75
    }
    filters_config {
      type      = "RELEVANCE"
      threshold = 0.75
    }
  }
}

# Published, so the id and version in the environment point at something frozen.
# DRAFT moves when someone edits the guardrail in the console, and a run that
# was checked against a different policy than the one on file is not auditable.
resource "aws_bedrock_guardrail_version" "published" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Applied by halo-agentic-quote-service"
}

output "guardrail_id" { value = aws_bedrock_guardrail.this.guardrail_id }
output "guardrail_arn" { value = aws_bedrock_guardrail.this.guardrail_arn }
output "guardrail_version" { value = aws_bedrock_guardrail_version.published.version }
