# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN (eks-fargate 모듈 output)"
  type        = string
}

variable "k8s_namespace" {
  description = "Gateway 애플리케이션 네임스페이스"
  type        = string
  default     = "llm-gateway"
}

variable "bedrock_allowed_model_arns" {
  description = "Bedrock InvokeModel 허용 모델 ARN 리스트. *.arn 으로 모델 고정 — 개인 모델 임의 사용 차단"
  type        = list(string)
  # 예시 (환경별 tfvars에서 실제 값 주입):
  # default = [
  #   "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-sonnet-4-*",
  #   "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-haiku-4-*",
  # ]
}

variable "mantle_regions" {
  description = "gateway-proxy IRSA 가 in-account Bedrock Mantle(bedrock-mantle:CreateInference/GetInference) 를 호출할 수 있는 리전 목록. 배포 리전에 맞춰 tfvars 에서 지정 (예: US 단일계정 = [\"us-east-1\"])."
  type        = list(string)
  # ⚠️ 여기에 default 를 두지 않는다 = 호출 root 가 **반드시** 명시해야 한다.
  #    값의 단일 진실원천은 각 env 의 variables.tf 다(dev/prod 둘 다
  #    default = ["ap-northeast-1", "us-east-2"]). env root 는 이 인자를 조건 없이
  #    항상 넘기므로(environments/*/main.tf:69) 모듈 default 는 절대 쓰이지 않는
  #    죽은 코드가 되고, 두 곳의 값이 갈라져도 아무도 눈치채지 못한다.
  #    (실측 2026-09-09: 이 default 를 ["MODULE-DEFAULT-SENTINEL"] 로 바꿔 plan 해도
  #     렌더된 Resource 는 Tokyo+Ohio 그대로 = 영향 0.)
  #    default 를 없애면 인자를 빠뜨린 순간 `tofu validate` 가
  #    "Missing required argument" 로 떨어뜨린다. default 가 있으면 대신 Tokyo+Ohio 로
  #    조용히 폴백해 **잘못된 리전** 정책이 깨끗한 plan 으로 통과한다(실측: validate
  #    Success + Tokyo/Ohio 렌더) — 이 실패를 가장 이른 단계로 끌어오는 것이 목적이다.
  #    nullable = false 는 명시적 null 도 막는다(root 변수의 nullable=false 때문에
  #    실제로는 root default 로 폴백해서 도달한다 — 실측).
  nullable = false

  # 빈 리스트도 막는다. null 과 달리 []는 plan 을 깨끗하게 통과하지만, main.tf 의
  # InAccountMantleInference statement 가 **Resource 원소 없이** 렌더돼 apply 중간에
  # IAM 이 MalformedPolicyDocument("Policy statement must contain resources") 로 거부한다
  # = 런이 반쯤 적용된 상태로 죽는다. (실측: accessanalyzer validate-policy →
  # ERROR / MISSING_RESOURCE.) 그래서 plan 단계에서 떨어뜨린다.
  validation {
    condition     = length(var.mantle_regions) > 0
    error_message = "mantle_regions 는 최소 1개여야 한다. in-account Mantle 을 안 쓰는 배포라도 []로 두지 말고 배포 리전을 넣어라(예: [\"us-east-1\"]). statement 자체를 없애려면 변수가 아니라 main.tf 의 statement 를 조건부로 만들어야 한다."
  }
}

variable "secrets_manager_kms_key_arns" {
  description = "Secrets Manager가 쓰는 KMS 키 ARN (기본 AWS 관리 키 + 사용자 키)"
  type        = list(string)
  default     = ["*"]
}

variable "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN — admin-api sync 기능에 필요"
  type        = string
  default     = ""
}

variable "cowork_role_arn" {
  description = <<-EOT
    Cowork cross-account Mantle role ARN (e.g. arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock).
    gateway-proxy AssumeRole into this to mint Mantle bearer for cowork -> Opus 4.8 (Tokyo).
    Must match model.routing_profiles.account_role_arn for client=cowork. The target role's trust
    policy must allow this account's gateway-proxy IRSA principal + sts:ExternalId=cowork-bedrock.
    Empty = cowork cross-account disabled (no AssumeRole statement rendered).
  EOT
  type        = string
  default     = ""
}

variable "claude_code_374_role_arn" {
  description = <<-EOT
    Claude Code cross-account Bedrock NATIVE role ARN (e.g. arn:aws:iam::333344445555:role/...).
    gateway-proxy AssumeRole into this to build a 374 bedrock-runtime client (boto3 invoke_model).
    Must match model.routing_profiles.account_role_arn for client=claude-code. The target role's
    trust must allow this account's gateway-proxy IRSA principal + sts:ExternalId=claude-code-bedrock.
    Empty = claude-code stays in-account (123); no AssumeRole statement rendered.
  EOT
  type        = string
  default     = ""
}

variable "bedrock_account_assume_role_arns" {
  description = <<-EOT
    alias 별 대상 AWS 계정(마이그레이션 0043/0044)용으로 gateway-proxy 가 sts:AssumeRole 할 수
    있는 역할 ARN 목록 — 관리 콘솔의 AWS 계정 레지스트리(/admin/aws-accounts)에 등록하는 역할.
    여기 없는 역할을 alias 에 고르면 그 alias 의 호출이 AssumeRole AccessDenied 로 실패한다
    (게이트웨이는 고정 500 으로 답하고 다른 계정으로 대신 호출하지 않는다).
    빈 목록(기본) = statement 를 렌더하지 않는다.

    ⚠️ 와일드카드("*")를 넣지 말 것. 그러면 게이트웨이 역할이 자신을 신뢰하는 임의의 계정 역할로
    전환할 수 있게 되고, 콘솔에 ARN 한 줄을 넣을 수 있는 사람이 곧 그 모든 계정에서 Bedrock 을
    호출할 수 있다. 대상 계정 쪽 신뢰 정책이 2차 방어선이지만 이 모듈이 관리하지 않는다.
  EOT
  type        = list(string)
  default     = []

  validation {
    # 역할 ARN 만. Bedrock ARN·사용자 ARN 도 apply 는 되지만 AssumeRole 이 불가능하고, 그
    # 사실은 그 alias 를 처음 호출할 때까지 드러나지 않는다. 경로·이름은 IAM 문자 집합만 받는다 —
    # "role/.+" 는 role/* · role/gw-* 를 통과시켰고, 그 값은 아래 statement 의 Resource 가 되어 그
    # 계정의 (해당 접두사) 모든 역할로의 AssumeRole 을 연다. 파티션은 aws / aws-<소문자> 만.
    # admin-api(schemas/models.py)·DB CHECK(0043/0044)·admin-ui 와 같은 문법이다.
    condition = alltrue([
      for a in var.bedrock_account_assume_role_arns :
      can(regex("^arn:aws(-[a-z]+)*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", a))
    ])
    error_message = "각 항목은 IAM 역할 ARN 이어야 합니다: arn:aws:iam::<12자리 계정>:role/<이름> (와일드카드 불가)."
  }
}

variable "bedrock_invocation_log_group_arn" {
  description = <<-EOT
    Bedrock model-invocation log group ARN — admin-api 의 감사 대조
    (/admin/audit/invocation-log/*) 가 Logs Insights 로 읽는 대상.

    ⚠️ 리전이 배포 리전과 다르다. invocation log 는 **모델이 실행된 Region** 에 쌓이므로
    GPT-5.6 runtime plane 은 us-east-2 다. 그래서 이름이 아니라 ARN 을 받는다
    (예: arn:aws:logs:us-east-2:<account>:log-group:/aws/bedrock/modelinvocations).
    ARN 끝에 :* 는 붙이지 말 것 — 아래에서 붙인다.

    빈 값 = 감사 기능 미사용, 권한 statement 자체를 렌더하지 않음.
  EOT
  type        = string
  default     = ""
}

variable "body_log_firehose_arn" {
  description = <<-EOT
    요청/응답 본문 로깅 sink 의 Firehose delivery stream ARN
    (modules/body-logging 의 `firehose_stream_arn` output).

    빈 값 = 본문 로깅 미사용 → firehose 쓰기 statement 를 렌더하지 않는다.
    권한은 Put 계열 **쓰기 전용**이다(main.tf 의 이유 주석 참조).
  EOT
  type        = string
  default     = ""
}

variable "body_log_bucket_arn" {
  description = <<-EOT
    본문 로깅 sink 의 S3 버킷 ARN (modules/body-logging 의 `bucket_arn` output).
    Firehose 레코드 상한을 넘는 본문의 직행 fallback 에만 쓴다.

    ⚠️ `/*` 는 붙이지 말 것 — 정책에서 붙인다. 버킷 ARN 자체에 대한 권한(ListBucket)은
    주지 않는다.
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
