# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Claude Fable 5 / 5.1 등록 — 계정 데이터 보존 모드가 aws_review 여야 호출된다

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-14

## 무엇을 추가하나

    claude-fable-5    / global.anthropic.claude-fable-5      (BEDROCK, ap-northeast-2, Global CRIS)
    claude-fable-5-1  / global.anthropic.claude-fable-5-1

0027 의 Claude 5 와 같이 짧은 alias + full ID 를 둘 다 넣고, 각 alias 에 단가 행을 붙인다.

## 0027 이 Fable 을 뺐던 이유와 지금

0027 은 "ap-northeast-2 에 프로파일이 없고, 호출하면 data retention mode 'default' is not
available 로 거부된다" 는 이유로 Fable 을 뺐다. 2026-10-14 기준:

  * 프로파일 — 서울에 ``global.anthropic.claude-fable-5`` / ``-5-1`` 이 ACTIVE 다
    (``aws bedrock list-inference-profiles``).
  * 거부의 정체 — 모델 요구사항이다. 모델카드: *"To use this model, you must opt in to AWS
    review by setting your data retention mode to aws_review via the Data Retention API."*
    Fable 의 ``allowed_modes`` 는 ``["aws_review", "provider_data_share"]`` 뿐이라, 계정의
    실효 모드가 ``none``/``default``(신규 계정의 ``inherit`` → 모델 기본값)이면
    bedrock-runtime 이 ValidationException 을 돌려준다.

## ⚠️ 이 리비전만으로는 호출되지 않는다 — 계정 설정이 필요하다

데이터 보존 모드는 **계정 단위·리전별** 설정이다(bedrock-runtime 에는 project 개념이 없다).
요청을 **실행하는 계정**의 **요청 리전**에서 켜야 한다:

    PUT https://bedrock.<region>.amazonaws.com/data-retention   {"mode": "aws_review"}
    GET 같은 URL 로 확인 — {"mode":"aws_review", ...}

게이트웨이는 클라이언트별로 다른 계정에서 실행할 수 있다(routing_profiles.account_role_arn).
cross-account 를 쓰는 클라이언트라면 **그 계정**도 켜야 한다 — 안 켜면 그 클라이언트만
400 이 나고, 그건 AccessDenied 가 아니라서 in-account 폴백도 타지 않는다.

되돌리기는 같은 API 로 ``{"mode": "inherit"}``.

### 켜면 무엇이 달라지나 (AWS 문서 — Data retention / Amazon Bedrock abuse detection)

  * Fable 트래픽: 최대 30일 보존(자동 오프라인 남용 탐지), 분류기에 걸린 요청은 AWS 가
    사람 검토할 수 있다. 모델 공급자와는 공유되지 않는다. Global CRIS 이므로 보존 데이터는
    **실제로 처리된 리전**(해외일 수 있음)에 저장된다.
  * 다른 모델: 바뀌지 않는다. *"Configuring your account or project to aws_review does not
    mean all models will start retaining your content for review"* — 모델마다 자기
    ``allowed_modes`` 로 처리가 정해지고, ZDR 가능 모델은 여전히 아무것도 저장하지 않는다.

이 정책 변경은 데이터 처리 결정이라 운영자가 승인한 뒤에 켤 것.

## 단가 — AWS Price List (0041 과 같은 방식)

``AmazonBedrockFoundationModels``, regionCode ap-northeast-2, ``APN2_*_global_standard-Units``,
unit ``1M tokens``, publicationDate 2026-10-12T08:40:08Z, effectiveDate 2026-09-01. 같은 조회의
대조군 Opus 5.5 가 0041 값(4/20/5/8/0.2)과 일치했다.

    $/1M          input  output  cw-5m  cw-1h  cache-read
    Fable 5       10.00  50.00   12.50  20.00  1.00
    Fable 5.1     10.00  50.00   12.50  20.00  0.25

⚠️ Fable 5.1 의 캐시 읽기는 입력의 0.025배다(5 는 0.1배). 나머지 네 칸은 같다 — 5 의 행을
   복사하면 5.1 캐시 읽기가 4배 과대청구된다.

Price List 에 long_ctx SKU 가 없다 → threshold NULL(단일 요율, 0038 의 Anthropic 기본).

## thinking

``services/thinking_normalizer.py`` 의 adaptive 전용 접두사에 ``anthropic.claude-fable-5`` 가
이미 있어 5.1 도 그 접두사로 잡힌다. 코드 변경은 없다.

## IAM

``bedrock_allowed_model_arns`` 의 Anthropic foundation-model 줄을 ``anthropic.claude-*`` 한
줄로 합친다(이 리비전과 같은 PR). 세대·계열별로 적던 이유가 "fable 을 IAM 에서도 막는다"
였는데 그 이유가 없어졌고, 그 방식은 신모델마다 "IaC 를 안 고치면 조용히 AccessDenied"
(0027 의 Claude 5, 0041 의 Haiku 5.5)를 반복했다. 프로파일 줄은 이미 ``global.anthropic.claude-*`` 다.

## Redis 캐시 / 되돌리기

0041 과 같다. 새 alias 라 단건 해석 캐시는 stale 이 없고 ``model:list`` 만 최대 300s 늦다.
downgrade 는 FK 자식 6개를 먼저 지우고 alias 를 지운다.
"""
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"
# 적용 시각보다 반드시 과거(0041 참고 — 미래 행은 해석기가 무시해 cost 0 이 된다).
# Price List SKU 의 effectiveDate 다.
EFFECTIVE_FROM = "2026-09-01T00:00:00Z"

# (short_alias, profile_id, display_name, description,
#  (input, output, cache_write_5m, cache_write_1h, cache_read) per-1K)
MODELS = [
    (
        "claude-fable-5",
        "global.anthropic.claude-fable-5",
        "Claude Fable 5",
        "Claude Fable 5 (Global inference profile). Requires the account data retention mode "
        "aws_review in the request Region. Pricing = AWS Price List, ap-northeast-2 global "
        "standard SKU (published 2026-10-12).",
        ("0.01000000", "0.05000000", "0.01250000", "0.02000000", "0.00100000"),
    ),
    (
        "claude-fable-5-1",
        "global.anthropic.claude-fable-5-1",
        "Claude Fable 5.1",
        "Claude Fable 5.1 (Global inference profile). Requires the account data retention mode "
        "aws_review in the request Region. Pricing = AWS Price List, ap-northeast-2 global "
        "standard SKU (published 2026-10-12); cache read is 0.025x input.",
        ("0.01000000", "0.05000000", "0.01250000", "0.02000000", "0.00025000"),
    ),
]


def upgrade() -> None:
    for short, profile, display, desc, (p_in, p_out, p_5m, p_1h, p_cr) in MODELS:
        for alias in (short, profile):
            suffix = "" if alias == short else " (full ID)"
            op.execute(
                f"""
                INSERT INTO model.model_aliases
                    (alias, provider, provider_model_id, endpoint_url, api_format, status,
                     description, display_name, created_by)
                VALUES
                    ('{alias}', 'BEDROCK', '{profile}', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
                     '{desc}{suffix}', '{display}', '{SYSTEM_USER}')
                ON CONFLICT (alias) DO NOTHING
                """
            )
            # 0041 과 같은 두 가드: 열린 단가 행이 있으면 건너뛰고(0034 부분 유니크),
            # alias 가 우리가 넣은 모델일 때만 붙인다.
            op.execute(
                f"""
                INSERT INTO model.model_pricings
                    (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
                     cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                     cache_read_price_per_1k_tokens, effective_from, created_by)
                SELECT gen_random_uuid(), '{alias}',
                       {p_in}, {p_out}, {p_5m}, {p_1h}, {p_cr},
                       '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
                WHERE EXISTS (
                    SELECT 1 FROM model.model_aliases
                     WHERE alias = '{alias}' AND provider_model_id = '{profile}'
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM model.model_pricings
                     WHERE model_alias = '{alias}' AND effective_until IS NULL
                )
                """
            )


def downgrade() -> None:
    """0027/0041 과 같다 — FK 자식(전부 ON DELETE NO ACTION)부터 지우고 alias 를 지운다."""
    aliases = ", ".join(f"'{a}'" for m in MODELS for a in (m[0], m[1]))

    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = NULL
         WHERE default_model IN ({aliases})
        """
    )
    for table, column in (
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    ):
        op.execute(f"DELETE FROM {table} WHERE {column} IN ({aliases})")

    op.execute(f"DELETE FROM model.model_aliases WHERE alias IN ({aliases})")
