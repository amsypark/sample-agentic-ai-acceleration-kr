# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.services.router_service import ModelInactiveError, RouterService


@pytest.fixture
def sample_alias_row():
    row = MagicMock()
    row.alias = "claude-sonnet-4-6"
    row.provider = "BEDROCK"
    row.provider_model_id = "global.anthropic.claude-sonnet-4-6"
    row.endpoint_url = None
    row.api_format = "BEDROCK_NATIVE"
    row.status = "ACTIVE"
    row.description = "Claude Sonnet 4.6"
    row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # alias 별 대상 계정(0043) — ORM 행에 있는 컬럼이다. 자동 MagicMock 속성은 _orm_to_schema
    # 가 str | None 필드에 넣을 때 pydantic 검증을 깨뜨린다. None = alias 계정 없음.
    row.account_role_arn = None
    row.account_external_id = None
    return row


@pytest.fixture
def sample_pricing_row():
    row = MagicMock()
    row.input_price_per_1k_tokens = Decimal("0.003000")
    row.output_price_per_1k_tokens = Decimal("0.015000")
    # FR-3.3 Stage 1 cache 단가 — Bedrock Prompt Caching: ×1.25 / ×0.1 multiplier.
    row.cache_creation_5m_price_per_1k_tokens = Decimal("0.003750")
    # Must be a real Decimal: _orm_to_schema reads this into ModelPricingSchema
    # (cache_write_1h_per_1k). Leaving it as an auto-MagicMock attribute caused a
    # pydantic Decimal ValidationError in the cache-miss/DB-rebuild path.
    row.cache_creation_1h_price_per_1k_tokens = Decimal("0.006000")
    row.cache_read_price_per_1k_tokens = Decimal("0.000300")
    # long-context 티어 컬럼(0038). 같은 이유로 실제 값이어야 한다 — 자동 MagicMock 속성은
    # _orm_to_schema 가 ModelPricingSchema 로 넣을 때 pydantic Decimal 검증을 깨뜨린다.
    # None = 티어 없음(이 샘플은 Claude, 밴드 대상 아님).
    row.long_context_threshold_tokens = None
    row.long_context_input_price_per_1k_tokens = None
    row.long_context_output_price_per_1k_tokens = None
    row.long_context_cache_creation_5m_price_per_1k_tokens = None
    row.long_context_cache_creation_1h_price_per_1k_tokens = None
    row.long_context_cache_read_price_per_1k_tokens = None
    return row


def _make_db_mock(scalar_results: list):
    """sqlalchemy execute → result.scalar_one_or_none() 모킹.

    scalar_results: 호출 순서대로 반환할 row 리스트 (None 가능).
    """
    db = AsyncMock()
    results = []
    for row in scalar_results:
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=row)
        r.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[row] if row else []))
        )
        results.append(r)
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alias_lookup_success(sample_alias_row, sample_pricing_row):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"
    assert schema.provider_model_id == "global.anthropic.claude-sonnet-4-6"
    assert schema.provider == ProviderType.BEDROCK
    assert schema.api_format == ApiFormat.BEDROCK_NATIVE
    assert schema.pricing.input_per_1k == Decimal("0.003000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_model_id_lookup_success(sample_alias_row, sample_pricing_row):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "global.anthropic.claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_hit_skips_db():
    cached_schema = ModelConfigSchema(
        provider_model_id="global.anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=cached_schema.model_dump_json())
    redis.setex = AsyncMock()
    db = AsyncMock()
    db.execute = AsyncMock()

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"
    db.execute.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_miss_on_an_alias_request_writes_only_the_alias_key(
    sample_alias_row, sample_pricing_row
):
    """alias 로 지목한 해석은 ``model:{alias}`` 만 쓴다.

    예전에는 ``model:{provider_model_id}`` 도 썼다. 그 키는 provider_model_id 와 **이름이
    같은 다른 alias** 의 캐시 칸이기도 해서(시드: claude-opus-4-8 → global.anthropic.
    claude-opus-4-8 이라는 alias 가 따로 있다) TTL 동안 그 alias 가 이 alias 의 설정 — 단가,
    허용 앱, status, alias 계정(0043) — 으로 서빙됐다(router_service._cache_resolved).
    """
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    keys_set = [call.args[0] for call in redis.setex.call_args_list]
    assert keys_set == ["model:claude-sonnet-4-6"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_miss_on_a_model_id_request_writes_both_keys(
    sample_alias_row, sample_pricing_row
):
    """provider_model_id 로 지목한 해석은 두 키 모두 — 다음 같은 요청이 캐시에서 끝난다."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    # 1) alias 정확 매칭 없음 → 2) provider_model_id 형제 스캔 → 3) 단가
    db = _make_db_mock([None, sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "global.anthropic.claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"
    keys_set = [call.args[0] for call in redis.setex.call_args_list]
    assert keys_set == ["model:claude-sonnet-4-6", "model:global.anthropic.claude-sonnet-4-6"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poison_flat_cache_falls_through_to_db(sample_alias_row, sample_pricing_row):
    """P0-④: a malformed/legacy FLAT cache entry (no nested `pricing`) must NOT
    raise (which surfaced as a permanent 500); it is treated as a cache miss and
    rebuilt from the DB with the correct nested shape + TTL (self-heal)."""
    # The exact flat shape the old admin create_model wrote — missing `pricing`.
    poison = json.dumps({
        "alias": "claude-sonnet-4-6",
        "provider": "BEDROCK",
        "provider_model_id": "global.anthropic.claude-sonnet-4-6",
        "endpoint_url": None,
        "api_format": "BEDROCK_NATIVE",
        "status": "ACTIVE",
        "input_price_per_1k": "0.003",
        "output_price_per_1k": "0.015",
    })
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=poison)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    # Did NOT raise; rebuilt from DB with valid nested pricing.
    assert schema.alias == "claude-sonnet-4-6"
    assert schema.pricing.input_per_1k == Decimal("0.003000")
    # DB was consulted (fell through) and cache was rewritten with correct shape+TTL.
    db.execute.assert_awaited()
    keys_set = [call.args[0] for call in redis.setex.call_args_list]
    assert "model:claude-sonnet-4-6" in keys_set


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregistered_alias_raises_lookup_error():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    # Two DB queries on a miss: alias exact-match, then provider_model_id fallback.
    db = _make_db_mock([None, None])

    rs = RouterService()
    with pytest.raises(LookupError, match="not found"):
        await rs.resolve_bedrock_model(redis, db, "claude-fictional")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregistered_full_id_raises_lookup_error():
    """회귀 검증: 기존엔 '.'이 있으면 통과시켰음. 이제는 무조건 LookupError."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    # Two DB queries on a miss: alias exact-match, then provider_model_id fallback.
    db = _make_db_mock([None, None])

    rs = RouterService()
    with pytest.raises(LookupError, match="not found"):
        await rs.resolve_bedrock_model(redis, db, "global.anthropic.claude-fictional")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inactive_alias_raises(sample_alias_row):
    sample_alias_row.status = "INACTIVE"
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    db = _make_db_mock([sample_alias_row])

    rs = RouterService()
    with pytest.raises(LookupError, match="inactive"):
        await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver, model_ref",
    [
        ("resolve_bedrock_model", "claude-sonnet-4-6"),
        ("resolve_codex_model", "codex-gpt"),
    ],
)
async def test_inactive_raises_model_inactive_error_specifically(
    sample_alias_row, resolver, model_ref
):
    """PINS THE SEAM: inactive must raise ModelInactiveError, not a bare LookupError.

    routers/openai_compat._resolve_model tells the two apart on purpose: an unknown
    alias falls back to default_model, an INACTIVE one is an operator kill switch and
    must 404. If this collapses back to a plain LookupError, the kill switch silently
    degrades into "serve default_model and bill the wrong alias" — and every test that
    only asserts LookupError (the base class) would still pass. Hence this assertion on
    the exact type, against the real resolver rather than a fake.
    """
    sample_alias_row.status = "INACTIVE"
    sample_alias_row.provider = (
        "BEDROCK" if resolver == "resolve_bedrock_model" else "BEDROCK_MANTLE_OPENAI"
    )
    sample_alias_row.alias = model_ref
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    db = _make_db_mock([sample_alias_row])

    rs = RouterService()
    with pytest.raises(ModelInactiveError, match="inactive"):
        await getattr(rs, resolver)(redis, db, model_ref)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_none_falls_to_db(sample_alias_row, sample_pricing_row):
    """NFR-2.4 graceful degradation: Redis 다운 시 DB 직접 조회."""
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis=None, db=db, model_ref="claude-sonnet-4-6")
    assert schema.alias == "claude-sonnet-4-6"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_db_exception_propagates():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("DB connection lost"))

    rs = RouterService()
    with pytest.raises(RuntimeError, match="DB connection lost"):
        await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")


# ── FR-2.6 check_key_scope ──


def _schema(alias: str, provider_model_id: str) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id=provider_model_id,
        alias=alias,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0"), output_per_1k=Decimal("0")),
        status=ModelStatus.ACTIVE,
    )


def _auth(allowed: list[str] | None):
    from app.schemas.domain import AuthContext, AuthType, Role

    return AuthContext(
        user_id="u1",
        team_id="t1",
        dept_id="",
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
        allowed_models=allowed,
    )


def test_check_key_scope_none_allows_everything():
    RouterService().check_key_scope(_auth(None), _schema("claude-opus", "bedrock-id"))


def test_check_key_scope_schema_matches_alias():
    """allowed_models에 alias 있으면 provider_model_id가 달라도 통과."""
    RouterService().check_key_scope(
        _auth(["claude-haiku"]),
        _schema("claude-haiku", "anthropic.claude-3-haiku-20240307-v1:0"),
    )


def test_check_key_scope_schema_matches_provider_model_id():
    """allowed_models에 provider_model_id만 있어도 통과 (하위 호환)."""
    RouterService().check_key_scope(
        _auth(["anthropic.claude-3-haiku-20240307-v1:0"]),
        _schema("claude-haiku", "anthropic.claude-3-haiku-20240307-v1:0"),
    )


def test_check_key_scope_schema_rejects_non_allowed():
    with pytest.raises(PermissionError):
        RouterService().check_key_scope(
            _auth(["claude-haiku"]),
            _schema("claude-opus", "anthropic.claude-3-opus"),
        )


def test_check_key_scope_legacy_str_signature_still_works():
    RouterService().check_key_scope(_auth(["claude-haiku"]), "claude-haiku")
    with pytest.raises(PermissionError):
        RouterService().check_key_scope(_auth(["claude-haiku"]), "claude-opus")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cached_payload_from_before_alias_accounts_is_a_miss(
    sample_alias_row, sample_pricing_row
):
    """롤링 배포: 0043 이전 이미지의 파드가 쓴 ``model:{alias}`` 에는 계정 칸이 **없다**. 새 파드가
    그것을 그대로 받으면 스키마 기본값 None = "alias 계정 없음" 으로 읽어, 그 alias 를 TTL(300초)
    동안 파드/프로필 계정으로 서명한다 — 다른 계정에 청구되는 200 이다. 칸이 없는 payload 는
    미스로 보고 DB 에서 다시 읽는다(명시적 null 은 정상 payload 다)."""
    legacy = ModelConfigSchema(
        provider_model_id="global.anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    ).model_dump(mode="json")
    legacy.pop("account_role_arn")
    legacy.pop("account_external_id")
    sample_alias_row.account_role_arn = "arn:aws:iam::111122223333:role/in-region-bedrock"
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps(legacy))
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    schema = await RouterService().resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    assert schema.account_role_arn == "arn:aws:iam::111122223333:role/in-region-bedrock"
    db.execute.assert_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cached_payload_with_an_explicit_null_account_is_a_hit():
    cached = ModelConfigSchema(
        provider_model_id="global.anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    ).model_dump_json()
    assert '"account_role_arn":null' in cached
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=cached)
    db = AsyncMock()
    db.execute = AsyncMock()

    schema = await RouterService().resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    assert schema.account_role_arn is None
    db.execute.assert_not_called()
