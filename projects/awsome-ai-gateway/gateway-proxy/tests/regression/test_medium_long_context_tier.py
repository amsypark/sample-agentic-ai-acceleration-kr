# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""272K long-context 요금 티어 (명시 요율) + 청구 티어 감사기록.

무엇을 넣나
-----------
GPT-5.6 모델카드는 요금표를 크기축으로 게시한다(OpenAI 문구를 AWS 카드가 인용):
"Prompts with >272K input tokens are priced at 2x input and 1.5x output for the full
request." 한 요청 프롬프트가 272,000 을 넘으면 그 요청 **전체**가 long 요율(계단 하나).

``model_pricings`` 에 크기축이 없어 272K 초과가 절반 요율로 과소청구됐다. 이 변경이
**명시 long 요율 컬럼**(배수 아님 — AWS Price List 자동연동이 달러 요율을 그대로 넣기
위함)과, 청구·감사가 공유하는 단일 판정 ``resolve_context_tier`` 를 넣는다.

⚠️ Claude 는 티어가 없다(threshold=None → 단일 요율). 대조군이 고정한다.

핵심 불변식: **청구 티어 == 기록 티어**. calculate_cost 와 usage_logs.context_tier 가
같은 resolve_context_tier 를 쓴다 — 두 곳이 어긋나면 "청구는 long, 기록은 short" 소명 불가.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.services.cost_recorder import calculate_cost, resolve_context_tier

# terra Geo/In-Region short (per 1k) — 카드값. long = short × {in:2, out:1.5, cache:2}.
_IN, _OUT, _CW, _CR = Decimal("0.0022"), Decimal("0.0132"), Decimal("0.00275"), Decimal("0.00022")
# 1h TTL 캐시-쓰기는 5m 과 **별도 컬럼**이다. 카드상 두 값은 같지만, 코드가 5m 이 아니라
# 1h 컬럼을 읽는지 증명하려면 테스트에서는 5m 과 다른 값을 준다(_CW=0.00275 vs 1h=0.0033).
_CW1H = Decimal("0.0033")
_THRESHOLD = 272000
_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


def _cfg(*, tier: bool, partial_long: bool = False) -> ModelConfigSchema:
    p = ModelPricingSchema(
        input_per_1k=_IN, output_per_1k=_OUT, cache_write_per_1k=_CW,
        cache_write_1h_per_1k=_CW1H, cache_read_per_1k=_CR,
    )
    if tier:
        p.long_context_threshold_tokens = _THRESHOLD
        p.long_input_per_1k = _IN * 2
        p.long_output_per_1k = _OUT * Decimal("1.5")
        p.long_cache_write_per_1k = _CW * 2
        p.long_cache_write_1h_per_1k = _CW1H * 2
        p.long_cache_read_per_1k = _CR * 2
        if partial_long:
            # 특정 버킷 long 단가를 비워 fail-safe(→ short) 를 검증.
            p.long_output_per_1k = None
    return ModelConfigSchema(
        provider_model_id="us.openai.gpt-5.6-terra", alias="gpt-5.6-terra",
        provider=ProviderType.BEDROCK, api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=p, status=ModelStatus.ACTIVE, created_at=datetime(2026, 1, 1),
    )


def _u(*, inp=0, out=0, cw=0, cr=0, ttl_1h=False):
    u = TokenUsage(input_tokens=inp, output_tokens=out,
                   cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
                   cache_ttl_1h=ttl_1h)
    u.total_tokens = inp + out
    return u


# ── resolve_context_tier — 단일 판정점 ──────────────────────────────────────


def test_no_threshold_returns_none():
    assert resolve_context_tier(_u(inp=999_999), _cfg(tier=False)) is None


def test_below_threshold_is_short():
    assert resolve_context_tier(_u(inp=200_000), _cfg(tier=True)) == "short"


def test_above_threshold_is_long():
    assert resolve_context_tier(_u(inp=300_000), _cfg(tier=True)) == "long"


def test_boundary_is_strictly_greater():
    assert resolve_context_tier(_u(inp=272_000), _cfg(tier=True)) == "short"
    assert resolve_context_tier(_u(inp=272_001), _cfg(tier=True)) == "long"


def test_tier_counts_the_whole_prompt_not_just_non_cached():
    """⚠️ 캐시 히트가 큰 요청. 순입력 100K 만 보면 미달이지만 프롬프트 총량 280K > 272K."""
    assert resolve_context_tier(_u(inp=100_000, cr=180_000), _cfg(tier=True)) == "long"


# ── calculate_cost — 명시 long 요율 ─────────────────────────────────────────


def test_below_threshold_uses_short_rates():
    cost = calculate_cost(_u(inp=100_000, out=500), _cfg(tier=True))
    assert cost == Decimal("0.226600"), cost  # 100×0.0022 + 0.5×0.0132


def test_above_threshold_uses_explicit_long_rates():
    """⚠️ 핵심. 300K → long. long 은 저장된 명시 요율(short×2/1.5)에서 온다."""
    cost = calculate_cost(_u(inp=300_000, out=500), _cfg(tier=True))
    # input 300×0.0044 + output 0.5×0.0198 = 1.32 + 0.0099
    assert cost == Decimal("1.329900"), cost


def test_cache_buckets_rerated_long():
    cost = calculate_cost(_u(inp=280_000, cw=5_000, cr=10_000), _cfg(tier=True))
    # long: 280×0.0044 + cw 5×0.0055 + cr 10×0.00044 = 1.232 + 0.0275 + 0.0044
    assert cost == Decimal("1.263900"), cost


def test_cache_write_1h_bucket_rerated_long():
    """⚠️ 1h TTL 캐시-쓰기의 long 쌍둥이. cache_ttl_1h=True 면 calculate_cost 는 1h long
    컬럼(long_cache_write_1h_per_1k)을 써야 한다 — 5m long 컬럼이 아니라. 5m 과 다른 값
    (1h short 0.0033 vs 5m short 0.00275)으로 어느 컬럼을 읽는지 고정한다. 회귀하면 1h
    캐시-쓰기가 있는 >272K 요청이 과소청구되지만 다른 테스트는 초록으로 남는다."""
    cost = calculate_cost(_u(inp=280_000, cw=5_000, ttl_1h=True), _cfg(tier=True))
    # long: 280×0.0044 + cw(1h) 5×0.0066 = 1.232 + 0.033 = 1.265
    # (5m long 0.0055 를 잘못 쓰면 5×0.0055=0.0275 → 1.2595 로 어긋난다)
    assert cost == Decimal("1.265000"), cost


def test_cache_write_1h_falls_back_to_short_when_long_1h_missing():
    """⚠️ fail-safe(1h). long_cache_write_1h 만 비면 그 항만 1h short, 나머지는 long."""
    cfg = _cfg(tier=True)
    cfg.pricing.long_cache_write_1h_per_1k = None
    cost = calculate_cost(_u(inp=280_000, cw=5_000, ttl_1h=True), cfg)
    # input long 280×0.0044=1.232 ; cw(1h) SHORT 5×0.0033=0.0165 → 1.2485
    assert cost == Decimal("1.248500"), cost


def test_partial_long_rate_falls_back_to_short_per_bucket():
    """⚠️ fail-safe. long_output 만 비면 그 항만 short, 나머지는 long — 크래시 없음."""
    cost = calculate_cost(_u(inp=300_000, out=500), _cfg(tier=True, partial_long=True))
    # input long 300×0.0044=1.32 ; output SHORT 0.5×0.0132=0.0066
    assert cost == Decimal("1.326600"), cost


def test_no_tier_model_never_rerates():
    """⚠️ Claude 대조군. threshold None → 300K 여도 short."""
    cost = calculate_cost(_u(inp=300_000, out=500), _cfg(tier=False))
    assert cost == Decimal("0.666600"), cost


def test_schema_long_fields_default_to_none():
    p = ModelPricingSchema(input_per_1k=Decimal("0.001"), output_per_1k=Decimal("0.002"))
    assert p.long_context_threshold_tokens is None
    assert p.long_input_per_1k is None and p.long_cache_read_per_1k is None


# ── 배선: ORM → 스키마 → 청구 ───────────────────────────────────────────────


def test_orm_to_schema_carries_explicit_long_columns():
    """⚠️ ORM 컬럼명(long_context_*_price_per_1k_tokens) → 스키마 필드(long_*_per_1k) 매핑.

    끊기면 DB 에 티어를 켜도 청구가 안 바뀐다(allowed_clients 사고와 같은 부류).
    """
    from app.services.router_service import _orm_to_schema

    class _Alias:
        provider_model_id = "us.openai.gpt-5.6-terra"
        alias = "gpt-5.6-terra"
        provider = "BEDROCK"
        api_format = "ANTHROPIC_MESSAGES"
        endpoint_url = ""
        status = "ACTIVE"
        created_at = datetime(2026, 1, 1)
        description = None
        allowed_clients = None

    class _Row:
        input_price_per_1k_tokens = _IN
        output_price_per_1k_tokens = _OUT
        cache_creation_5m_price_per_1k_tokens = _CW
        cache_creation_1h_price_per_1k_tokens = Decimal("0")
        cache_read_price_per_1k_tokens = _CR
        long_context_threshold_tokens = _THRESHOLD
        long_context_input_price_per_1k_tokens = _IN * 2
        long_context_output_price_per_1k_tokens = _OUT * Decimal("1.5")
        long_context_cache_creation_5m_price_per_1k_tokens = _CW * 2
        long_context_cache_creation_1h_price_per_1k_tokens = Decimal("0")
        long_context_cache_read_price_per_1k_tokens = _CR * 2

    cfg = _orm_to_schema(_Alias(), _Row())
    assert cfg.pricing.long_context_threshold_tokens == _THRESHOLD
    assert calculate_cost(_u(inp=300_000, out=0), cfg) == Decimal("1.320000")


def test_band_survives_redis_cache_round_trip():
    cfg = _cfg(tier=True)
    restored = ModelConfigSchema.model_validate_json(cfg.model_dump_json())
    assert restored.pricing.long_context_threshold_tokens == _THRESHOLD
    assert calculate_cost(_u(inp=300_000, out=0), restored) == Decimal("1.320000")


# ── context_tier 감사: 청구 티어 == 기록 티어 ───────────────────────────────


async def test_finalize_records_the_tier_it_billed():
    """⚠️ cost:stream 으로 나가는 엔트리의 context_tier 가 청구에 쓴 판정과 같아야 한다."""
    from app.services.cost_recorder import CostRecorder

    class _Cap:
        def __init__(self):
            self.entries = []
        async def xadd(self, key, fields, **kw):
            self.entries.append(json.loads(fields["payload"]))
            return "1-1"
        def __getattr__(self, n):
            async def _f(*a, **k): return None
            return _f

    class _Auth:
        user_id = "11111111-1111-1111-1111-111111111111"
        team_id = "22222222-2222-2222-2222-222222222222"
        dept_id = "33333333-3333-3333-3333-333333333333"
        sso_subject = "s"

    r = _Cap()
    rec = CostRecorder()
    cfg = _cfg(tier=True)
    # long 요청
    await rec.finalize(r, _Auth(), cfg, _u(inp=300_000, out=100), f"lc-{uuid.uuid4()}",
                       False, 100, rate_limit_state=None, client="codex")
    assert r.entries[-1]["context_tier"] == "long", r.entries[-1].get("context_tier")
    # short 요청
    await rec.finalize(r, _Auth(), cfg, _u(inp=100_000, out=100), f"sc-{uuid.uuid4()}",
                       False, 100, rate_limit_state=None, client="codex")
    assert r.entries[-1]["context_tier"] == "short"
    # 티어 없는 모델 → None
    await rec.finalize(r, _Auth(), _cfg(tier=False), _u(inp=300_000, out=100),
                       f"nc-{uuid.uuid4()}", False, 100, rate_limit_state=None, client="codex")
    assert r.entries[-1]["context_tier"] is None


# ── 실 PostgreSQL — 마이그레이션한 행이 청구까지 흐르고 티어가 usage_logs 에 남는가 ──

PROOF_DSN = os.environ.get("PROOF_DSN")
_pg = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG 필요(0038/0039 적용 head)."
)


@_pg
async def test_migrated_row_bills_long_through_the_orm():
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.model import ModelAlias, ModelPricing
    from app.services.router_service import _orm_to_schema

    engine = create_async_engine(PROOF_DSN)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            alias = (await db.execute(
                select(ModelAlias).where(ModelAlias.alias == "gpt-5.6-terra")
            )).scalar_one_or_none()
            if alias is None:
                pytest.skip("gpt-5.6-terra 없음 — 0025/0032 미적용")
            row = (await db.execute(
                select(ModelPricing).where(ModelPricing.model_alias == "gpt-5.6-terra")
                .where(ModelPricing.effective_until.is_(None))
            )).scalar_one()
        assert row.long_context_threshold_tokens == 272000
        cfg = _orm_to_schema(alias, row)
        long_cost = calculate_cost(_u(inp=300_000, out=1000), cfg)
        short_in = (Decimal(300_000) / 1000) * cfg.pricing.input_per_1k
        short_out = (Decimal(1000) / 1000) * cfg.pricing.output_per_1k
        expected = (short_in * 2 + short_out * Decimal("1.5")).quantize(Decimal("0.000001"))
        assert long_cost == expected, (long_cost, expected)
        assert calculate_cost(_u(inp=200_000, out=0), cfg) == (
            (Decimal(200_000) / 1000) * cfg.pricing.input_per_1k
        ).quantize(Decimal("0.000001"))
    finally:
        await engine.dispose()


# AWS 가 long-context 요율을 **게시한** 모델만 티어를 가진다(마이그레이션 0038·0041).
#   openai.gpt-5.6-*            272K  (0038)
#   openai.gpt-6-* / gpt-6.1-*  272K  (0041)
#   anthropic.claude-haiku-5-5  100K  (0041 — Anthropic 모델 중 처음으로 long SKU 가 있다)
# 그 밖의 Claude(4.x, 5, Opus/Sonnet 5.5)는 1M 컨텍스트를 표준 요율로 청구한다(Price List 에 long
# SKU 0건). 거기에 티어가 켜지면 긴 요청이 과대청구된다 — 그게 이 대조군이 막는 것이다.
_PUBLISHED_LONG_TIER = (
    "%openai.gpt-5.6-%",
    "%openai.gpt-6-%",
    "%openai.gpt-6.1-%",
    "%anthropic.claude-haiku-5-5%",
)


@_pg
async def test_only_published_long_tier_models_are_tiered_in_the_migrated_db():
    """⚠️ 대조군(실 PG). 패턴이 다른 Claude 까지 티어를 켜면 1M 요청이 과대청구된다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT a.alias, a.provider_model_id FROM model.model_pricings p "
                "JOIN model.model_aliases a ON a.alias=p.model_alias "
                "WHERE p.effective_until IS NULL AND p.long_context_threshold_tokens IS NOT NULL "
                "AND NOT (a.provider_model_id LIKE ANY (:patterns))"),
                {"patterns": list(_PUBLISHED_LONG_TIER)})).fetchall()
        assert not rows, f"long 요율이 게시되지 않은 모델에 티어가 켜졌다: {rows}"
    finally:
        await engine.dispose()


# ── 웹서치 루프: 임계는 **상류 호출 단위**다 ─────────────────────────────────────
#
# 웹서치 루프는 N 번의 상류 호출(검색 턴들 + 최종 턴)을 한 usage 로 합쳐 usage_logs 한 행으로
# 남긴다. 그런데 272K 임계는 **호출마다** 적용된다 — AWS 는 각 호출을 자기 프롬프트 크기로
# 청구한다. 합계를 임계와 비교하면 각자 short 인 턴들이 long 으로 청구된다: 100K × 3 턴이
# $0.6996 대신 $1.3794 로, context_tier=long 으로 기록됐다(리뷰 프로브로 실측).

_PER_SHORT_TURN = Decimal("0.2332")  # 100K × 0.0022/1k + 1K × 0.0132/1k


def _merged(turns):
    from app.services.web_search_loop import _merge_usage

    merged = TokenUsage()
    for t in turns:
        _merge_usage(merged, t)
    return merged


def test_merged_web_search_usage_bills_each_turn_at_its_own_tier():
    cfg = _cfg(tier=True)
    merged = _merged([_u(inp=100_000, out=1_000) for _ in range(3)])
    assert merged.input_tokens == 300_000, "토큰 합계는 그대로 한 행에 남는다"
    assert resolve_context_tier(merged, cfg) == "short"
    assert calculate_cost(merged, cfg) == _PER_SHORT_TURN * 3  # 0.6996, not 1.3794


def test_one_long_turn_bills_only_that_turn_long_and_records_long():
    cfg = _cfg(tier=True)
    merged = _merged([_u(inp=100_000, out=1_000), _u(inp=300_000, out=1_000)])
    # 0.2332 + (300 × 0.0044 + 1 × 0.0198)
    assert calculate_cost(merged, cfg) == Decimal("1.573000")
    assert resolve_context_tier(merged, cfg) == "long", "한 턴이라도 long 이면 감사 기록은 long"


def test_turn_breakdown_bills_with_the_request_cache_ttl():
    """``cache_ttl_1h`` 는 라우터가 **루프가 끝난 뒤** 합계 usage 에 세운다(_ws_record).

    턴별로 나눠 청구할 때 그 값이 턴에 전달되지 않으면 1h 캐시 쓰기가 5m 단가로 청구된다.
    """
    cfg = _cfg(tier=True)
    merged = _merged([_u(inp=1_000, cw=10_000), _u(inp=1_000, cw=10_000)])
    merged.cache_ttl_1h = True
    # 턴마다 1 × 0.0022 + 10 × 0.0033(1h) = 0.0352
    assert calculate_cost(merged, cfg) == Decimal("0.070400")


def test_untiered_model_is_unchanged_by_the_breakdown():
    cfg = _cfg(tier=False)
    merged = _merged([_u(inp=200_000, out=1_000) for _ in range(3)])
    assert resolve_context_tier(merged, cfg) is None
    assert calculate_cost(merged, cfg) == calculate_cost(_u(inp=600_000, out=3_000), cfg)


class _Search:
    async def search(self, query, max_results):
        from types import SimpleNamespace

        return SimpleNamespace(raw_text="result")


def _anthropic_turn(*, search: bool, idx: int) -> list[bytes]:
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 100_000}}}]
    if search:
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": f"tu_{idx}", "name": "web_search"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"query": "q"}'}},
            {"type": "content_block_stop", "index": 0},
        ]
        stop = "tool_use"
    else:
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "answer"}},
            {"type": "content_block_stop", "index": 0},
        ]
        stop = "end_turn"
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop},
         "usage": {"output_tokens": 1_000}},
        {"type": "message_stop"},
    ]
    return [json.dumps(e).encode() for e in events]


def _responses_turn(*, search: bool, idx: int) -> list[bytes]:
    events = [{"type": "response.created", "response": {"id": "resp_1"}}]
    if search:
        item = {"type": "function_call", "name": "web_search", "call_id": f"c_{idx}"}
        events += [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.function_call_arguments.delta", "output_index": 0,
             "delta": '{"query": "q"}'},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {**item, "arguments": '{"query": "q"}'}},
        ]
    else:
        events.append({"type": "response.output_text.delta", "output_index": 0, "delta": "answer"})
    events.append({
        "type": "response.completed",
        "response": {"id": "resp_1", "output": [],
                     "usage": {"input_tokens": 100_000, "output_tokens": 1_000,
                               "total_tokens": 101_000}},
    })
    return [json.dumps(e).encode() for e in events]


def _aiter(items):
    async def _gen():
        for item in items:
            yield item

    return _gen()


def _base_body(dialect: str) -> dict:
    if dialect == "anthropic":
        return {"messages": [{"role": "user", "content": "hi"}]}
    return {"input": "hi"}


class _NoRequest:
    async def is_disconnected(self) -> bool:  # pragma: no cover - 호출되면 안 된다
        raise AssertionError


@pytest.mark.parametrize("dialect", ["anthropic", "responses"])
async def test_stream_stitchers_bill_each_upstream_call_at_its_own_tier(dialect):
    """실제 스티처로 검색 2턴 + 최종 1턴(각 100K)을 돌린다 — 각 호출은 AWS 에서 short 다."""
    import time

    from app.services import web_search_loop as wsl

    make = _anthropic_turn if dialect == "anthropic" else _responses_turn
    turns = iter([make(search=True, idx=1), make(search=True, idx=2), make(search=False, idx=3)])
    seen = []

    async def invoke_stream(body):
        return 200, _aiter(next(turns)), {}, None

    async def on_usage(usage):
        seen.append(usage)

    stitch = wsl._anthropic_stream if dialect == "anthropic" else wsl._responses_stream
    base = _base_body(dialect)
    async for _ in stitch(
        invoke_stream=invoke_stream, base_body=base, mcp_client=_Search(), request=_NoRequest(),
        on_usage=on_usage, max_iterations=5, deadline=time.monotonic() + 60,
        default_max_results=10,
    ):
        pass

    (merged,) = seen
    cfg = _cfg(tier=True)
    assert (merged.input_tokens, merged.web_search_count) == (300_000, 2)
    assert resolve_context_tier(merged, cfg) == "short"
    assert calculate_cost(merged, cfg) == _PER_SHORT_TURN * 3


@pytest.mark.parametrize("dialect", ["anthropic", "responses"])
async def test_nonstream_loops_bill_each_upstream_call_at_its_own_tier(dialect):
    import time

    from app.services import web_search_loop as wsl

    def _body(search: bool, idx: int) -> bytes:
        if dialect == "anthropic":
            content = (
                [{"type": "tool_use", "id": f"tu_{idx}", "name": "web_search",
                  "input": {"query": "q"}}]
                if search else [{"type": "text", "text": "answer"}]
            )
            return json.dumps({"content": content, "usage": {"input_tokens": 1, "output_tokens": 1},
                               "stop_reason": "tool_use" if search else "end_turn"}).encode()
        output = (
            [{"type": "function_call", "name": "web_search", "call_id": f"c_{idx}",
              "arguments": '{"query": "q"}'}]
            if search else [{"type": "message", "content": [{"type": "output_text", "text": "a"}]}]
        )
        usage = {"input_tokens": 1, "output_tokens": 1}
        return json.dumps({"output": output, "usage": usage}).encode()

    plan = iter([(True, 1), (True, 2), (False, 3)])
    seen = []

    async def invoke(body):
        search, idx = next(plan)
        return 200, _body(search, idx), {}, _u(inp=100_000, out=1_000)

    async def on_usage(usage):
        seen.append(usage)

    loop = wsl._anthropic_nonstream if dialect == "anthropic" else wsl._responses_nonstream
    base = _base_body(dialect)
    await loop(
        invoke=invoke, base_body=base, mcp_client=_Search(), on_usage=on_usage,
        max_iterations=5, deadline=time.monotonic() + 60, default_max_results=10,
    )
    (merged,) = seen
    cfg = _cfg(tier=True)
    assert merged.input_tokens == 300_000
    assert resolve_context_tier(merged, cfg) == "short"
    assert calculate_cost(merged, cfg) == _PER_SHORT_TURN * 3


async def test_finalize_records_one_row_with_the_per_turn_cost_and_tier():
    """usage_logs 는 여전히 **한 행**이다 — 토큰은 합계, 비용은 턴별 합, 티어는 턴 판정."""
    from unittest.mock import AsyncMock

    from app.schemas.domain import AuthContext, AuthType, Role
    from app.services.cost_recorder import CostRecorder
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader.load_all(_SRC / "redis_scripts")
    redis = AsyncMock()
    redis.eval = AsyncMock(return_value='{"threshold_triggered": null}')
    redis.xadd = AsyncMock()
    auth = AuthContext(user_id="u-1", team_id="t-1", dept_id="d-1", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY)
    merged = _merged([_u(inp=100_000, out=1_000) for _ in range(3)])
    cost = await CostRecorder().finalize(redis, auth, _cfg(tier=True), merged, "req-ws", True, 10)

    assert cost == _PER_SHORT_TURN * 3
    assert redis.xadd.await_count == 1, "웹서치 요청 하나는 원장 한 행"
    payload = json.loads(redis.xadd.await_args.args[1]["payload"])
    assert (payload["input_tokens"], payload["output_tokens"]) == (300_000, 3_000)
    assert Decimal(payload["cost_usd"]) == _PER_SHORT_TURN * 3
    assert payload["context_tier"] == "short"
