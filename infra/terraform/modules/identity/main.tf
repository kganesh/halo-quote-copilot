# Cognito: the directory admission reads its claims from.
#
# The two custom attributes are the seller's scope. They live in the directory
# rather than in a table this application owns, because the whole point of
# design rule 03 is that identity arrives with the request instead of being
# looked up by the thing checking it.

variable "name" { type = string }

resource "aws_cognito_user_pool" "this" {
  name = var.name

  # Cognito custom attributes are strings and cannot be lists, so account_ids
  # arrives comma-separated and `admission.py` splits it. Mutable because a
  # seller's book changes and an immutable attribute would need a new user.
  schema {
    name                     = "tenant_id"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 1
      max_length = 32
    }
  }

  schema {
    name                     = "account_ids"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 1
      max_length = 2048
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 3
  }

  # No MFA and no email sending in a practice pool: SES in sandbox is a support
  # ticket, and neither is what this stack exists to demonstrate.
  mfa_configuration = "OFF"

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "cli" {
  name         = "${var.name}-cli"
  user_pool_id = aws_cognito_user_pool.this.id

  explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  generate_secret     = false

  # The ID token is the one that carries the custom attributes, and the one
  # `admission.py` accepts. An access token from this pool is signed by the same
  # key, passes every signature check, and has no tenant on it.
  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 1

  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

# One group per role. `admission.ROLE_BY_GROUP` maps these names, and a group
# with no entry there grants nothing — a typo cannot produce a role.
resource "aws_cognito_user_group" "roles" {
  for_each = toset(["halo-seller", "halo-sales-manager", "halo-operations"])

  name         = each.key
  user_pool_id = aws_cognito_user_pool.this.id
}

output "user_pool_id" { value = aws_cognito_user_pool.this.id }
output "client_id" { value = aws_cognito_user_pool_client.cli.id }
output "issuer" {
  value = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.this.id}"
}

data "aws_region" "current" {}
