# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Claude 5.5 (Opus/Sonnet/Haiku) 와 GPT-6 (Astra/Sol/Luna, 6.1 Sol) 등록

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-14

## 무엇을 추가하나

    Claude 5.5 (BEDROCK, ap-northeast-2, Global CRIS) — 0027 과 같이 짧은 alias + full ID
      claude-opus-5-5    / global.anthropic.claude-opus-5-5
      claude-sonnet-5-5  / global.anthropic.claude-sonnet-5-5
      claude-haiku-5-5   / global.anthropic.claude-haiku-5-5

    GPT-6 (BEDROCK_RUNTIME_OPENAI, us-east-2, US Geo CRIS) — 0032 와 같은 접두사 없는 이름
      gpt-6-astra   -> us.openai.gpt-6-astra
      gpt-6-sol     -> us.openai.gpt-6-sol
      gpt-6-luna    -> us.openai.gpt-6-luna
      gpt-6.1-sol   -> us.openai.gpt-6.1-sol

모든 alias 에 단가 행을 같이 넣는다. 단가 없는 alias 는 cost_usd 0 으로 기록돼 예산이
조용히 무력화된다(test_high_model_registry_claude5 의 orphan 검사가 막는 결함).

## 호출 실측 (2026-10-14, in-account IAM user SigV4, AWS_BEARER_TOKEN_BEDROCK unset)

    ap-northeast-2  bedrock-runtime InvokeModel  global.anthropic.claude-{opus,sonnet,haiku}-5-5  3/3 200
    us-east-2       /openai/v1/responses         us.openai.gpt-{6-astra,6-sol,6-luna,6.1-sol}     4/4 200
    (대조군 us.openai.gpt-5.6-sol 200)

서울에는 Claude 5.5 의 ``global.`` 프로파일만 있다(0027 의 Claude 5 와 같다). GPT-6 는
0032 와 같은 이유로 us-east-2 + ``us.`` 다 — 미국 안으로 좁힌 데이터 경계이고, 모델카드
기준 runtime 평면에는 In-Region 이 없어 CRIS 가 필수다.

## 단가 출처 — Claude 는 Price List, GPT 는 모델카드

**Claude 5.5** — AWS Price List ``AmazonBedrockFoundationModels``, regionCode
ap-northeast-2, usagetype ``APN2_*_global_standard-Units``, unit ``1M tokens``,
publicationDate 2026-10-12T08:40:08Z. 같은 조회에서 **대조군 두 개가 DB 와 마지막 자리까지
일치**했다: Sonnet 5 (2.00/10.00/2.50/4.00/0.20, 0030 의 정정값)와 Opus 5
(5.00/25.00/6.25/10.00/0.50). 그래서 같은 SKU 형식으로 읽은 5.5 값을 그대로 쓴다.

    $/1M          input  output  cw-5m  cw-1h  cache-read
    Opus 5.5      4.00   20.00   5.00   8.00   0.20
    Sonnet 5.5    2.00   10.00   2.50   4.00   0.10
    Haiku 5.5     0.10    0.50   0.125  0.20   0.01
     └ long-ctx   0.50    2.50   0.625  1.00   0.05     (프롬프트 > 100,000 토큰)

⚠️ 캐시 읽기를 "입력의 0.1배" 로 계산하면 안 된다. Opus 5.5 와 Sonnet 5.5 는 0.05배다
   (Bedrock pricing 페이지: "Starting October 7, 2026, cache read pricing for Claude
   Sonnet 5.5 is reduced by 50%"). Haiku 5.5 만 0.1배다. SKU 값을 그대로 쓴 이유다.
⚠️ Haiku 5.5 가 4.5($1/$5)보다 10배 싸다. 이상해 보이지만 SKU 설명이 단위를 명시한다
   ("Input Tokens - Standard, Global", unit=1M tokens, 원값 0.1000000000). What's New
   (2026-10-07)는 "around 75% less than Claude Haiku 4.5 for most tasks" 라고 한다 —
   토큰 단가가 아니라 작업당 비용이라 방향만 일치한다.

**GPT-6** — AWS 모델카드(2026-10-14 조회)
``https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-{astra,sol,luna}.html``,
``.../model-card-openai-gpt-6-1-sol.html``. 0032 와 같은 원칙이다: Sol/Luna/6.1 Sol 은
상용 리전 Price List SKU 가 **0건**이고(0032 가 기록한 5.6 Sol 과 같다), 카드에는 날짜가
박힌 요율표가 있다. Astra 는 두 출처가 있고 서로 일치한다(Price List us-east-2 regional
SKU 11.00/55.00/1.10/13.75 = 카드의 Geo CRIS 행).

    $/1M, US Geo CRIS   input  cw-30m  cache-read  output   long: input  cw-30m  cread  output
    gpt-6-astra         11.00  13.75   1.10        55.00          22.00  27.50   2.20   82.50
    gpt-6-sol            2.20   2.75   0.22        11.00           4.40   5.50   0.44   16.50
    gpt-6-luna           0.11   0.1375 0.011        0.55           0.22   0.275  0.022   0.825
    gpt-6.1-sol          2.20   2.75   0.11        11.00           4.40   5.50   0.22   16.50

``us.`` 는 Geo CRIS 라 카드의 Geo 행(Global 대비 +10%)을 넣는다. ``global.`` 로 바꾸는 것은
**가격 변경**이기도 하다(0032 의 MOVING REGIONS 참고).
⚠️ 6.1 Sol 의 캐시 읽기는 입력의 0.05배다(카드: "cache-read tokens are billed at 0.05×").
   6 Sol 과 입력·출력은 같지만 캐시 읽기만 다르다 — 배수로 복사하면 2배 과대청구다.
⚠️ Luna 의 cw-30m 0.1375/1M = 0.0001375/1K 는 7자리다. 0040 이 단가 열을 NUMERIC(12,8) 로
   넓히기 전이었다면 0.000138 로 반올림됐다. 이 리비전이 0040 **뒤에** 있어야 하는 이유다.

cache write 는 0032 와 같이 5m·1h 두 열에 같은 값을 넣는다. Bedrock 은 OpenAI 모델에
쓰기 요율을 하나(30m)만 게시하고, 1h 열은 Anthropic 방언에서만 쓰인다.

## long-context 티어 — 명시 요율 (0038 의 열)

    Haiku 5.5   threshold 100000   카드: "Prompts that exceed 100,000 tokens are billed at
                                   higher rates for input, output, and cache read and write"
    GPT-6 4종   threshold 272000   카드: "Long-context rates apply to the full request when
                                   input exceeds 272,000 tokens."

둘 다 **요청 전체**가 long 요율로 바뀌는 계단이다(초과분 누진 아님). cost_recorder 의
``resolve_context_tier`` 가 정확히 그 의미다(프롬프트 총량 = 비캐시 입력 + 캐시 읽기 +
캐시 쓰기 > threshold → 전부 long). 이 비교는 provider 와 무관해서 Anthropic 방언인
Haiku 5.5 에도 그대로 맞는다.

Opus 5.5 / Sonnet 5.5 는 Price List 에 long_ctx SKU 가 없다 → threshold NULL(단일 요율).
0038 이 기록한 "Anthropic 4.6+ 는 1M 컨텍스트를 표준 단가로 청구" 와 같다. **Haiku 5.5 가
Anthropic 모델 중 처음으로 long 티어를 갖는다.**

0038 처럼 short × 배수로 계산하지 않고 카드/SKU 의 달러 값을 그대로 넣는다 — Haiku 5.5 는
5배라 0038 의 배수(2/1.5/2)와 다르고, 명시 값이면 배수 가정을 검증할 필요가 없다.

## ⚠️ Ultrafast(6배)는 이 단가로 청구되지 않는다 — 게이트웨이가 막는다

Astra 와 6.1 Sol 은 ``service_tier: "ultrafast"`` 를 받는다(실측 200, 응답
``service_tier='ultrafast'``). 카드 기준 Standard 의 6배다. 이 리비전이 넣는 것은 Standard
요율뿐이므로, 그 요청이 상류로 가면 AWS 는 6배를 청구하고 게이트웨이는 1배로 기록·차감한다.
같은 PR 의 ``gateway-proxy/src/app/services/service_tier_gate.py`` 가 단가를 모르는 티어를
상류 호출 전에 400 으로 거절한다. **그 게이트 없이 이 리비전만 적용하면 예산 상한이
6배로 뚫린다.**

## ⚠️ IAM 도 같이 늘려야 호출된다

DB 행만으로는 호출되지 않는다(0027 의 Claude 5 AccessDenied 사고). gateway-proxy IRSA 의
``bedrock_allowed_model_arns`` 에 foundation-model 과 inference-profile 양쪽이 필요하다:

    foundation-model/anthropic.claude-haiku-5*        (opus-5*, sonnet-5* 는 이미 5.5 를 덮는다)
    foundation-model/openai.gpt-6-*,   openai.gpt-6.1-*
    inference-profile/us.openai.gpt-6-*,     us.openai.gpt-6.1-*
    inference-profile/global.openai.gpt-6-*, global.openai.gpt-6.1-*

(deployment/terraform/environments/llm-gateway-{dev,prod}/variables.tf)

## Fable 5 / 5.1 은 여전히 미등록

0027 과 README 의 방침 그대로다. 서울에서 호출이 거부되고(data retention mode), IAM 도
fable 을 계속 막아 둔다. 이 리비전은 그 결정을 바꾸지 않는다.

## Redis 캐시

새 alias 는 새 키라 단건 해석(``model:{alias}``)에는 stale 캐시가 없다. 부정 캐시도 없다.
``model:list`` 만 최대 300s 늦게 반영된다(``redis-cli DEL model:list`` 로 즉시 반영).

## 되돌리기

0027/0032 와 같다 — FK 자식 6개(전부 ON DELETE NO ACTION)를 먼저 지우고 alias 를 지운다.
routing_profiles.default_model 이 이 alias 를 가리키면 Claude 는 NULL(pass-through),
GPT 는 ACTIVE 인 ``codex-gpt-5.6-terra`` 로 되돌린다(없으면 손대지 않는다).
"""
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"
GPT_ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
# Pinned (not now()) for cross-environment parity in price audits, like 0025/0027/0032.
EFFECTIVE_FROM = "2026-10-14T00:00:00Z"
GPT_FALLBACK_DEFAULT = "codex-gpt-5.6-terra"

# 단가는 전부 per-1K, 소수 8자리(0040 의 NUMERIC(12,8)). 출처 값(per-1M) / 1000.
# short = (input, output, cache_write_5m, cache_write_1h, cache_read)
# long  = None 이면 티어 없음, 아니면 (threshold, input, output, cw_5m, cw_1h, cache_read)

# (short_alias, profile_id, display_name, description, short, long)
CLAUDE_MODELS = [
    (
        "claude-opus-5-5",
        "global.anthropic.claude-opus-5-5",
        "Claude Opus 5.5",
        "Claude Opus 5.5 (Global inference profile). Pricing = AWS Price List, "
        "ap-northeast-2 global standard SKU (published 2026-10-12).",
        ("0.00400000", "0.02000000", "0.00500000", "0.00800000", "0.00020000"),
        None,
    ),
    (
        "claude-sonnet-5-5",
        "global.anthropic.claude-sonnet-5-5",
        "Claude Sonnet 5.5",
        "Claude Sonnet 5.5 (Global inference profile). Pricing = AWS Price List, "
        "ap-northeast-2 global standard SKU (published 2026-10-12; cache read cut 50% on 2026-10-07).",
        ("0.00200000", "0.01000000", "0.00250000", "0.00400000", "0.00010000"),
        None,
    ),
    (
        "claude-haiku-5-5",
        "global.anthropic.claude-haiku-5-5",
        "Claude Haiku 5.5",
        "Claude Haiku 5.5 (Global inference profile). Pricing = AWS Price List, "
        "ap-northeast-2 global standard SKU (published 2026-10-12); prompts over 100K tokens "
        "are billed at the long-context rates for the whole request.",
        ("0.00010000", "0.00050000", "0.00012500", "0.00020000", "0.00001000"),
        (100000, "0.00050000", "0.00250000", "0.00062500", "0.00100000", "0.00005000"),
    ),
]

# (alias, provider_model_id, display_name, description, short, long)
GPT_MODELS = [
    (
        "gpt-6-astra",
        "us.openai.gpt-6-astra",
        "GPT-6 Astra (Bedrock runtime)",
        "bedrock-runtime GPT-6 Astra (us-east-2, US CRIS; Responses + Chat). Pricing = AWS "
        "model card, Geo CRIS Standard (= Price List). Ultrafast is refused by the gateway.",
        ("0.01100000", "0.05500000", "0.01375000", "0.01375000", "0.00110000"),
        (272000, "0.02200000", "0.08250000", "0.02750000", "0.02750000", "0.00220000"),
    ),
    (
        "gpt-6-sol",
        "us.openai.gpt-6-sol",
        "GPT-6 Sol (Bedrock runtime)",
        "bedrock-runtime GPT-6 Sol (us-east-2, US CRIS; Responses + Chat). Pricing = AWS "
        "model card, Geo CRIS Standard.",
        ("0.00220000", "0.01100000", "0.00275000", "0.00275000", "0.00022000"),
        (272000, "0.00440000", "0.01650000", "0.00550000", "0.00550000", "0.00044000"),
    ),
    (
        "gpt-6-luna",
        "us.openai.gpt-6-luna",
        "GPT-6 Luna (Bedrock runtime)",
        "bedrock-runtime GPT-6 Luna (us-east-2, US CRIS; Responses + Chat). Pricing = AWS "
        "model card, Geo CRIS Standard.",
        ("0.00011000", "0.00055000", "0.00013750", "0.00013750", "0.00001100"),
        (272000, "0.00022000", "0.00082500", "0.00027500", "0.00027500", "0.00002200"),
    ),
    (
        "gpt-6.1-sol",
        "us.openai.gpt-6.1-sol",
        "GPT-6.1 Sol (Bedrock runtime)",
        "bedrock-runtime GPT-6.1 Sol (us-east-2, US CRIS; Responses + Chat). Pricing = AWS "
        "model card, Geo CRIS Standard (cache read is 0.05x input). Ultrafast is refused by "
        "the gateway.",
        ("0.00220000", "0.01100000", "0.00275000", "0.00275000", "0.00011000"),
        (272000, "0.00440000", "0.01650000", "0.00550000", "0.00550000", "0.00022000"),
    ),
]


def _rows():
    """(alias, provider, provider_model_id, endpoint, api_format, display, desc, short, long)."""
    for short_alias, profile, display, desc, short, long in CLAUDE_MODELS:
        for alias in (short_alias, profile):
            suffix = "" if alias == short_alias else " (full ID)"
            yield (alias, "BEDROCK", profile, None, "BEDROCK_NATIVE",
                   display, desc + suffix, short, long)
    for alias, model_id, display, desc, short, long in GPT_MODELS:
        yield (alias, "BEDROCK_RUNTIME_OPENAI", model_id, GPT_ENDPOINT, "OPENAI_RESPONSES",
               display, desc, short, long)


def upgrade() -> None:
    for alias, provider, model_id, endpoint, api_format, display, desc, short, long in _rows():
        endpoint_sql = "NULL" if endpoint is None else f"'{endpoint}'"
        op.execute(
            f"""
            INSERT INTO model.model_aliases
                (alias, provider, provider_model_id, endpoint_url, api_format, status,
                 description, display_name, created_by)
            VALUES
                ('{alias}', '{provider}', '{model_id}', {endpoint_sql}, '{api_format}', 'ACTIVE',
                 '{desc}', '{display}', '{SYSTEM_USER}')
            ON CONFLICT (alias) DO NOTHING
            """
        )

        p_in, p_out, p_5m, p_1h, p_cr = short
        if long is None:
            thr, l_in, l_out, l_5m, l_1h, l_cr = ("NULL",) * 6
        else:
            thr, l_in, l_out, l_5m, l_1h, l_cr = (str(v) for v in long)

        # 두 가드:
        #  · 열린 단가 행이 이미 있으면 넣지 않는다 — 재실행 멱등이고, 0034 의 부분 유니크
        #    (model_alias) WHERE effective_until IS NULL 을 어기지 않는다(운영자가 콘솔로 먼저
        #    등록·가격 설정한 alias 에서 마이그레이션이 죽지 않는다).
        #  · alias 행이 **우리가 넣은 모델**일 때만 넣는다 — ON CONFLICT 로 운영자의 alias 가
        #    남았는데 그게 다른 모델을 가리키면, 이 요율을 거기 붙이면 틀린 가격이다.
        op.execute(
            f"""
            INSERT INTO model.model_pricings
                (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
                 cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                 cache_read_price_per_1k_tokens,
                 long_context_threshold_tokens,
                 long_context_input_price_per_1k_tokens,
                 long_context_output_price_per_1k_tokens,
                 long_context_cache_creation_5m_price_per_1k_tokens,
                 long_context_cache_creation_1h_price_per_1k_tokens,
                 long_context_cache_read_price_per_1k_tokens,
                 effective_from, created_by)
            SELECT gen_random_uuid(), '{alias}',
                   {p_in}, {p_out}, {p_5m}, {p_1h}, {p_cr},
                   {thr}, {l_in}, {l_out}, {l_5m}, {l_1h}, {l_cr},
                   '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
            WHERE EXISTS (
                SELECT 1 FROM model.model_aliases
                 WHERE alias = '{alias}' AND provider_model_id = '{model_id}'
            )
              AND NOT EXISTS (
                SELECT 1 FROM model.model_pricings
                 WHERE model_alias = '{alias}' AND effective_until IS NULL
            )
            """
        )


def downgrade() -> None:
    """alias 를 참조하는 자식 행까지 children-before-parent 순으로 제거 (0027/0032 와 같다).

    FK 6개가 전부 ON DELETE NO ACTION 이라, 운영자가 이 모델을 팀/사용자에게 허용한 뒤
    alias 만 지우면 ForeignKeyViolationError 로 죽고 transaction_per_migration 때문에 DB 가
    이 리비전에 갇힌다. routing_profiles.default_model 은 FK 가 없는 평범한 열이라 따로
    되돌린다 — 지운 alias 를 가리키면 그 클라이언트의 모든 요청이 404 가 된다.
    """
    claude = ", ".join(f"'{a}'" for m in CLAUDE_MODELS for a in (m[0], m[1]))
    gpt = ", ".join(f"'{m[0]}'" for m in GPT_MODELS)

    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = NULL
         WHERE default_model IN ({claude})
        """
    )
    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = '{GPT_FALLBACK_DEFAULT}'
         WHERE default_model IN ({gpt})
           AND EXISTS (
                 SELECT 1 FROM model.model_aliases
                  WHERE alias = '{GPT_FALLBACK_DEFAULT}' AND status = 'ACTIVE'
           )
        """
    )

    aliases = f"{claude}, {gpt}"
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
