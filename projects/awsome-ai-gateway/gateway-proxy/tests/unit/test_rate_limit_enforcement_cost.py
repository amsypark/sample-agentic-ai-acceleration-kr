# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""FR-4.6 enforcement 레이어의 CPM/CPH pre-reserve 통합 유닛 테스트."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.services.rate_limit_enforcement import _estimate_cost, enforce_rate_limits


def _model(input_per_1k: str = "0.003", output_per_1k: str = "0.015") -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal(input_per_1k),
            output_per_1k=Decimal(output_per_1k),
        ),
        status=ModelStatus.ACTIVE,
    )


def test_estimate_cost_uses_input_and_max_output():
    cost = _estimate_cost(_model(), estimated_input=2000, max_output=1000)
    # input: 2000/1000 * 0.003 = 0.006
    # output: 1000/1000 * 0.015 = 0.015
    assert cost == Decimal("0.021000")


@pytest.mark.asyncio
async def test_enforce_cpm_exceeded_returns_429(monkeypatch, mock_redis):
    """CPM 초과 시 429 JSONResponse (RPM/TPM은 통과 가정)."""
    # RPM/TPM 통과 mock
    async def _rpm_ok(*args, **kwargs):
        from app.schemas.domain import RateLimitResult

        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _tpm_ok(*args, **kwargs):
        from app.schemas.domain import RateLimitResult

        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    # CPM 거부 mock
    async def _cost_fail(*args, **kwargs):
        from app.schemas.domain import CostLimitResult

        return CostLimitResult(
            allowed=False,
            scope="USER",
            limit_type="cpm",
            limit=Decimal("0.5"),
            remaining=Decimal("0"),
            retry_after=60,
            reserved_cost=Decimal("0"),
        )

    from app.services import rate_limit_enforcement as enf

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_fail)

    # config loader — USER CPM=0.5 설정
    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(rpm=1000, tpm=1_000_000, cpm=Decimal("0.5"), cph=None),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}

    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_model(),
        body={"max_tokens": 1000},
        state=state,
        request_id="req-1",
    )

    assert resp is not None
    assert resp.status_code == 429
    body = json.loads(bytes(resp.body).decode())
    assert body["error"]["code"] == "user_cpm_exceeded"
    assert body["error"]["scope"] == "USER"
    assert body["error"]["limit_type"] == "cpm"
    assert resp.headers["Retry-After"] == "60"
    # state에 cost_reserved 주입되지 않음 (거부됨)
    assert "rate_limit_state" not in state


@pytest.mark.asyncio
async def test_enforce_cpm_pass_injects_cost_reserved(monkeypatch, mock_redis):
    """CPM 통과 시 state['rate_limit_state']['cost_reserved']에 예약액 주입."""
    from app.schemas.domain import CostLimitResult, RateLimitResult

    async def _rpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _tpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _cost_ok(*args, **kwargs):
        return CostLimitResult(allowed=True, reserved_cost=Decimal("0.021"))

    from app.services import rate_limit_enforcement as enf

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_ok)

    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(cpm=Decimal("1.0")),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}
    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_model(),
        body={"max_tokens": 500},
        state=state,
        request_id="req-2",
    )

    assert resp is None
    assert state["rate_limit_state"]["cost_reserved"] == Decimal("0.021")


# ── 272K long-context 티어와 pre-reserve 추정의 정합성 (0038) ────────────────────
#
# `_estimate_cost` 의 존재 이유는 **과대 예약**이다 — 게이트가 실제보다 큰 값으로 판정해야
# CPM/CPH 한도가 fail-closed 로 걸린다. long 티어를 무시하면 그 불변식이 깨지고, 깨진 사실은
# 어디에도 로그로 남지 않는다(settle 이 사후에 차액을 맞춰 원장도 정상으로 보인다).
# 그래서 여기서 "예약 ≥ 실제" 를 직접 고정한다.

_THRESHOLD = 272_000


def _tiered_model() -> ModelConfigSchema:
    """``gpt-5.6-terra`` (runtime, Geo CRIS) 의 **시드 단가** — db/init + alembic head 를 마친
    실 PG 의 ``model.model_pricings`` 열린 행에서 그대로 옮겼다(0032 short, 0038 long).

    ★ 값을 손으로 적지 말 것. long 은 0038 이 short 에 카드 배수를 곱해 채운다:
      input   0.0022  → 0.0044  = 2.0x
      output  0.0132  → 0.0198  = 1.5x
      c_write 0.00275 → 0.0055  = 2.0x (5m·1h 동일)
      c_read  0.00022 → 0.00044 = 2.0x
    두 열을 바꿔 적으면(예: output 자리에 cache_write) "long output = short × 1.5" 라는 티어 정의가
    테스트에서 사라지고, 아래 과대예약 검증이 실제보다 약한 조건으로 통과한다 — 아래 배수 고정
    테스트가 그것을 막는다.
    """
    return ModelConfigSchema(
        provider_model_id="us.openai.gpt-5.6-terra",
        alias="gpt-5.6-terra",
        provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
        api_format=ApiFormat.OPENAI_RESPONSES,
        endpoint="",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.0022"),
            output_per_1k=Decimal("0.0132"),
            cache_write_per_1k=Decimal("0.00275"),
            cache_write_1h_per_1k=Decimal("0.00275"),
            cache_read_per_1k=Decimal("0.00022"),
            long_context_threshold_tokens=_THRESHOLD,
            long_input_per_1k=Decimal("0.0044"),
            long_output_per_1k=Decimal("0.0198"),
            long_cache_write_per_1k=Decimal("0.0055"),
            long_cache_write_1h_per_1k=Decimal("0.0055"),
            long_cache_read_per_1k=Decimal("0.00044"),
        ),
        status=ModelStatus.ACTIVE,
    )


def test_tiered_fixture_matches_the_documented_tier_multipliers():
    """픽스처가 티어 정의(입력·캐시 2배, 출력 1.5배)를 실제로 담고 있는지 고정한다."""
    p = _tiered_model().pricing
    assert p.long_input_per_1k == p.input_per_1k * 2
    assert p.long_output_per_1k == p.output_per_1k * Decimal("1.5")
    assert p.long_cache_write_per_1k == p.cache_write_per_1k * 2
    assert p.long_cache_write_1h_per_1k == p.cache_write_1h_per_1k * 2
    assert p.long_cache_read_per_1k == p.cache_read_per_1k * 2


def test_estimate_cost_below_threshold_uses_short_rates():
    cost = _estimate_cost(_tiered_model(), estimated_input=100_000, max_output=1_000)
    # 100000/1000 * 0.0022 + 1000/1000 * 0.0132 = 0.22 + 0.0132
    assert cost == Decimal("0.233200")


def test_estimate_cost_at_threshold_is_still_short():
    # 모델카드의 "272K input tokens or fewer" = short. 경계 자체는 넘지 않은 것이다.
    cost = _estimate_cost(_tiered_model(), estimated_input=_THRESHOLD, max_output=0)
    assert cost == Decimal(_THRESHOLD) / Decimal(1000) * Decimal("0.0022")


def test_estimate_cost_above_threshold_uses_long_rates():
    cost = _estimate_cost(_tiered_model(), estimated_input=_THRESHOLD + 1, max_output=0)
    expected = Decimal(_THRESHOLD + 1) / Decimal(1000) * Decimal("0.0044")
    assert cost == expected.quantize(Decimal("0.000001"))


def test_estimate_cost_above_threshold_covers_actual_billed_cost():
    """예약 ≥ 실제. 깨지면 CPM/CPH 가 $2 남은 사용자가 $2.24 요청을 통과시킨다.

    티어를 무시하던 코드: 500K 프롬프트 + max_output 4,000 → 예약 $1.1528 vs 실제 $2.2396
    (출력 2,000) — **절반**이었다.
    """
    from app.schemas.domain import TokenUsage
    from app.services.cost_recorder import calculate_cost

    model = _tiered_model()
    prompt = 500_000
    reserved = _estimate_cost(model, estimated_input=prompt, max_output=4_000)

    # 실제 청구: 같은 프롬프트, 출력은 상한보다 작게 나온 흔한 경우.
    actual = calculate_cost(
        TokenUsage(input_tokens=prompt, output_tokens=2_000, total_tokens=prompt + 2_000),
        model,
    )
    assert actual == Decimal("2.239600")
    assert reserved >= actual, f"under-reserved: {reserved} < {actual}"


def test_estimate_cost_tier_decision_matches_the_billing_path():
    """추정기와 청구가 **같은 판정점**을 쓴다 — 경계 양쪽에서 교차 확인."""
    from app.schemas.domain import TokenUsage
    from app.services.cost_recorder import calculate_cost

    model = _tiered_model()
    for prompt, expected_rate in ((_THRESHOLD, "0.0022"), (_THRESHOLD + 1, "0.0044")):
        reserved = _estimate_cost(model, estimated_input=prompt, max_output=0)
        billed = calculate_cost(
            TokenUsage(input_tokens=prompt, output_tokens=0, total_tokens=prompt), model
        )
        want = (Decimal(prompt) / Decimal(1000) * Decimal(expected_rate)).quantize(
            Decimal("0.000001")
        )
        assert reserved == want, f"estimator used the wrong rate at {prompt}"
        assert billed == want, f"biller used the wrong rate at {prompt}"


def test_estimate_cost_untiered_model_unaffected():
    """threshold 가 없는 모델(Claude 등)은 프롬프트가 아무리 커도 단일 요율이다."""
    cost = _estimate_cost(_model(), estimated_input=1_000_000, max_output=0)
    assert cost == Decimal(1_000_000) / Decimal(1000) * Decimal("0.003")


def test_estimate_cost_falls_back_to_short_when_long_rate_missing():
    """long 단가가 비면 그 항만 short 로 폴백 — calculate_cost 와 같은 fail-safe.

    admin-api 스키마 검증이 막는 조합이지만 DB 직접 수정으로 도달할 수 있다. 여기서 크래시하면
    예약 단계가 500 이 되어 **모든** 요청이 막힌다.
    """
    model = _tiered_model()
    model.pricing.long_input_per_1k = None
    cost = _estimate_cost(model, estimated_input=_THRESHOLD + 1, max_output=0)
    expected = Decimal(_THRESHOLD + 1) / Decimal(1000) * Decimal("0.0022")
    assert cost == expected.quantize(Decimal("0.000001"))


# ── 티어 판정에서 base64 이미지·오디오 바이트 제외 ──────────────────────────────
#
# 예약량(TPM·비용 크기)은 body 전체의 bytes/4 그대로 둔다. 바뀌는 것은 **티어 판정**뿐이다:
# 이미지·오디오의 base64 바이트는 프롬프트 토큰이 아니라서(AWS 는 해상도·길이 기반 토큰으로
# 센다) ~1.1MB 짜리 data URL 하나가 요청을 long 티어로 밀어 CPM/CPH 를 2배로 잡고, 멀티모달
# 요청에 거짓 429 를 낸다.
#
# ⚠️ 문서(PDF 등)와 텍스트는 빼면 안 된다 — 그 **내용**이 프롬프트 토큰으로 청구된다. 처음 구현은
#    MIME 을 보지 않고 data URL·``bytes``·``{"type":"base64","data"}`` 를 전부 뺐고, 1MB PDF 가 33
#    티어 토큰이 되어 short 예약·long 청구로 "예약 ≥ 청구" 가 깨졌다(리뷰 프로브로 실측).

_MEDIA = "A" * 1_100_000  # base64 1.1MB ≈ bytes/4 로 275K "토큰" — 임계(272K) 초과


def _responses_body(content: list) -> dict:
    return {
        "model": "gpt-5.6-terra",
        "max_output_tokens": 1000,
        "input": [{"role": "user", "content": content}],
    }


_MEDIA_BODIES = {
    "responses_input_image": _responses_body(
        [
            {"type": "input_text", "text": "describe this"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{_MEDIA}"},
        ]
    ),
    "chat_image_url": {
        "model": "gpt-5.6-terra",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_MEDIA}"}},
                ],
            }
        ],
    },
    "chat_input_audio": {
        "model": "gpt-5.6-terra",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": _MEDIA, "format": "wav"}}
                ],
            }
        ],
    },
    "anthropic_image_base64": {
        "model": "gpt-5.6-terra",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": _MEDIA},
                    },
                ],
            }
        ],
    },
}

#: 대조군 — 내용이 프롬프트 토큰으로 청구되므로 **빼면 안 되는** 것들(전부 long 으로 예약돼야 한다).
_COUNTED_BODIES = {
    "responses_pdf_input_file": _responses_body(
        [
            {"type": "input_text", "text": "summarize"},
            {
                "type": "input_file",
                "filename": "spec.pdf",
                "file_data": f"data:application/pdf;base64,{_MEDIA}",
            },
        ]
    ),
    "anthropic_pdf_document": {
        "model": "gpt-5.6-terra",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": _MEDIA,
                        },
                    },
                ],
            }
        ],
    },
    "text_under_a_bytes_key": _responses_body(
        [{"type": "input_text", "text": "call the tool"}]
    )
    | {"tools": [{"type": "function", "name": "w", "parameters": {"bytes": "x " * 550_000}}]},
    "text_as_untyped_base64_dict": _responses_body(
        [{"type": "x", "content": {"type": "base64", "data": "x " * 550_000}}]
    ),
}


async def _reserved_cost_for(monkeypatch, mock_redis, body: dict) -> Decimal:
    """``enforce_rate_limits`` 가 reserve_cost 에 넘기는 예약액을 그대로 잡는다."""
    from app.schemas.domain import CostLimitResult, RateLimitResult
    from app.services import rate_limit_enforcement as enf
    from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

    seen: list[Decimal] = []

    async def _ok(*_a, **_k):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _reserve(*_a, **kwargs):
        seen.append(kwargs["estimated_cost"])
        return CostLimitResult(allowed=True, reserved_cost=kwargs["estimated_cost"])

    async def _limits(**_k):
        return AllScopeLimits(
            user=ScopeLimits(cpm=Decimal("100")), team=ScopeLimits(), global_=ScopeLimits()
        )

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _reserve)
    monkeypatch.setattr(enf, "load_all_scope_limits", _limits)

    auth = AuthContext(
        user_id="u-1", team_id="t-1", dept_id="d-1", roles=["USER"], auth_type=AuthType.VIRTUAL_KEY
    )
    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_tiered_model(),
        body=body,
        state={},
        request_id="req-media",
    )
    assert resp is None and len(seen) == 1
    return seen[0]


def _expected(estimated_input: int, max_output: int, *, long: bool) -> Decimal:
    in_rate, out_rate = ("0.0044", "0.0198") if long else ("0.0022", "0.0132")
    return (
        Decimal(estimated_input) / Decimal(1000) * Decimal(in_rate)
        + Decimal(max_output) / Decimal(1000) * Decimal(out_rate)
    ).quantize(Decimal("0.000001"))


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_MEDIA_BODIES))
async def test_base64_media_does_not_push_a_request_into_the_long_tier(
    monkeypatch, mock_redis, shape
):
    """⚠️ 고치기 전: data URL 하나로 long 단가 예약(약 2배) → 멀티모달에 거짓 CPM/CPH 429."""
    from app.services.rate_limit_enforcement import _estimate_input_tokens

    body = _MEDIA_BODIES[shape]
    estimated_input = _estimate_input_tokens(body)
    assert estimated_input > _THRESHOLD, "전제: bytes/4 로는 임계를 넘는 body 다"
    reserved = await _reserved_cost_for(monkeypatch, mock_redis, body)
    # 예약 크기는 그대로(body 전체 bytes/4), 단가만 short.
    assert reserved == _expected(estimated_input, 1000, long=False), reserved


@pytest.mark.asyncio
async def test_text_over_the_threshold_still_reserves_at_long_rates(monkeypatch, mock_redis):
    """대조군 — 같은 크기의 **텍스트**는 여전히 long 이다.

    미디어만 빠졌는지, 티어 판정 자체가 꺼졌는지를 이것으로 구분한다.
    """
    from app.services.rate_limit_enforcement import _estimate_input_tokens

    body = _responses_body([{"type": "input_text", "text": "x" * 1_100_000}])
    estimated_input = _estimate_input_tokens(body)
    reserved = await _reserved_cost_for(monkeypatch, mock_redis, body)
    assert reserved == _expected(estimated_input, 1000, long=True), reserved


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_COUNTED_BODIES))
async def test_documents_and_text_still_count_for_the_tier(monkeypatch, mock_redis, shape):
    """⚠️ 대조군 — 문서·텍스트는 티어 판정에서 빠지면 안 된다(short 예약·long 청구가 된다)."""
    from app.services.rate_limit_enforcement import _estimate_input_tokens

    body = _COUNTED_BODIES[shape]
    estimated_input = _estimate_input_tokens(body)
    assert estimated_input > _THRESHOLD, "전제: bytes/4 로 임계를 넘는 body 다"
    reserved = await _reserved_cost_for(monkeypatch, mock_redis, body)
    assert reserved == _expected(estimated_input, 1000, long=True), reserved
