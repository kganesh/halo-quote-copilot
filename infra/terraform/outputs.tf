output "environment" {
  description = "Everything `halo` needs in one place, ready to export."
  value = {
    HALO_GUARDRAIL_ID      = module.guardrail.guardrail_id
    HALO_GUARDRAIL_VERSION = module.guardrail.guardrail_version
    HALO_EVENTS_BUCKET     = module.evidence.bucket
    AWS_REGION             = var.region
  }
}

output "cognito" {
  description = "Issuer and audience for the API Gateway JWT authorizer."
  value = {
    user_pool_id = module.identity.user_pool_id
    client_id    = module.identity.client_id
    issuer       = module.identity.issuer
  }
}

output "access_policy_arn" {
  description = "Attach this to whoever runs the CLI, if principal_arn was empty."
  value       = module.access.policy_arn
}
