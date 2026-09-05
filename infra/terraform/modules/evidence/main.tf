# The M7 event record: one object per event, partitioned by kind and day.
#
# `force_destroy` is the milestone's whole argument in one argument. Without it,
# `terraform destroy` fails on a bucket with objects in it, someone empties it by
# hand, and "the teardown works" quietly becomes "the teardown works if you
# remember the manual step". A practice account's evidence bucket is not a thing
# to protect from deletion; a production one would set this to false and accept
# that destroy is a two-step operation, which is the honest tradeoff rather than
# a setting to copy.

variable "name" { type = string }
variable "account_id" { type = string }
variable "retention_days" { type = number }

resource "aws_s3_bucket" "evidence" {
  bucket        = "${var.name}-evidence-${var.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# No versioning. Events are append-only by construction — nothing rewrites an
# object — so versions would be storage with no reader, and a versioned bucket
# makes teardown slower for no benefit here.

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "events"
    status = "Enabled"

    filter {
      prefix = "events/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = var.retention_days
    }

    # Uploads that never completed still bill. This is the line item nobody
    # finds, because an incomplete upload is invisible in the console's object
    # listing.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

output "bucket" { value = aws_s3_bucket.evidence.id }
output "bucket_arn" { value = aws_s3_bucket.evidence.arn }
