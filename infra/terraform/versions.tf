terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Local state, deliberately. A remote backend is an S3 bucket and a lock table
  # that outlive `terraform destroy`, which is exactly the thing this milestone
  # is trying to prove does not happen. A team needs one; a practice account
  # being stood up and torn down needs the state file it can delete.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = "halo-agentic-quote-service"
      managed_by  = "terraform"
      environment = var.environment
    }
  }
}
