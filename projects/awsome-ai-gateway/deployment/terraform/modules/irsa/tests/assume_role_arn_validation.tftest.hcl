# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# bedrock_account_assume_role_arns 의 검증만 본다 — mock_provider 라 AWS 를 호출하지 않는다.
# 실행: 이 모듈 디렉터리에서 `tofu init -backend=false && tofu test` (OpenTofu 1.8+ / Terraform 1.7+).
# 와일드카드 ARN 은 아래 IAM statement 의 Resource 가 되어 그 계정의 역할 전부로 AssumeRole 을 연다.

mock_provider "aws" {
  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::111122223333:policy/mock"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::111122223333:role/mock"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  project                    = "proof"
  environment                = "test"
  oidc_provider_arn          = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  bedrock_allowed_model_arns = ["arn:aws:bedrock:*::foundation-model/anthropic.claude-*"]
  mantle_regions             = ["us-east-2"]
}

run "wildcard_role_is_refused" {
  command = plan
  variables {
    bedrock_account_assume_role_arns = ["arn:aws:iam::111122223333:role/*"]
  }
  expect_failures = [var.bedrock_account_assume_role_arns]
}

run "prefix_wildcard_is_refused" {
  command = plan
  variables {
    bedrock_account_assume_role_arns = ["arn:aws:iam::111122223333:role/gw-*"]
  }
  expect_failures = [var.bedrock_account_assume_role_arns]
}

run "fake_partition_is_refused" {
  command = plan
  variables {
    bedrock_account_assume_role_arns = ["arn:awsfoo:iam::111122223333:role/x"]
  }
  expect_failures = [var.bedrock_account_assume_role_arns]
}

run "real_roles_are_accepted" {
  command = plan
  variables {
    bedrock_account_assume_role_arns = [
      "arn:aws:iam::111122223333:role/in-region-bedrock",
      "arn:aws-us-gov:iam::444455556666:role/team/a+b=c,d.e@f_g-h",
    ]
  }
}
