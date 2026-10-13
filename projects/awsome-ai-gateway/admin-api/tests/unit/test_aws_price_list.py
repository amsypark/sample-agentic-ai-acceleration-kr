# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS Price List usagetype 파서 + preview/sync 대조 (자동연동, fetch ≠ apply).

파서는 실측한 **실제 usagetype 문자열**에 고정한다 — AWS 가 형식을 바꾸면 여기서 깨져야
한다. per-1k 변환(unit '1M tokens' → /1000)과 long-ctx 티어 분리가 핵심.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.services.aws_price_list import (
    LONG_CONTEXT_THRESHOLD_TOKENS,
    AwsModelPrice,
    _price_per_1k,
    fetch_bedrock_prices,
    long_tier_enabled,
    normalize_pmid,
)


def _sku(usagetype: str, usd: str, unit: str = "1M tokens", region: str = "us-gov-west-1") -> str:
    """실측 SKU 모양의 최소 JSON 문자열(get_products PriceList 항목)."""
    return json.dumps(
        {
            "product": {"attributes": {"usagetype": usagetype, "regionCode": region, "service_tier": "standard"}},
            "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}}}},
        }
    )


# 실측 UGW1 terra 세트 (short + long). USD 는 per-1M; 파서가 /1000 해야 per-1k 가 된다.
_REAL_PAGE = {
    "PriceList": [
        _sku("UGW1-openai.gpt-5.6-terra-mantle-input-tokens-standard", "2.6400000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-output-tokens-standard", "15.8400000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-cache-read-tokens-standard", "0.2640000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-cache-write-tokens-30m-standard", "3.3000000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-input-tokens-long-ctx-standard", "5.2800000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-output-tokens-long-ctx-standard", "23.7600000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-cache-read-tokens-long-ctx-standard", "0.5280000000"),
        _sku("UGW1-openai.gpt-5.6-terra-mantle-cache-write-tokens-30m-long-ctx-standard", "6.6000000000"),
        # 비-standard tier 는 무시돼야 한다.
        _sku("UGW1-openai.gpt-5.6-terra-mantle-input-tokens-priority", "10.5600000000"),
        # Guardrail 등 무관 SKU 는 파서 정규식에서 떨어진다.
        _sku("UGW1-Guardrail-ContextualGroundingPolicyUnitsConsumed", "0.1", unit="1K units"),
    ]
}


def _patched_client():
    """boto3.client('pricing').get_paginator('get_products').paginate() → 실측 페이지 1장."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [_REAL_PAGE]
    client.get_paginator.return_value = paginator
    return client


def test_normalize_pmid_strips_cris_prefix():
    assert normalize_pmid("global.openai.gpt-5.6-terra") == "openai.gpt-5.6-terra"
    assert normalize_pmid("us.openai.gpt-5.6-sol") == "openai.gpt-5.6-sol"
    assert normalize_pmid("openai.gpt-5.6-luna") == "openai.gpt-5.6-luna"  # 이미 bare


def test_parses_short_and_long_per_1k_and_ignores_non_standard():
    with patch("app.services.aws_price_list.boto3.client", return_value=_patched_client()):
        out = fetch_bedrock_prices("us-gov-west-1")

    assert set(out) == {"openai.gpt-5.6-terra"}
    e = out["openai.gpt-5.6-terra"]
    assert e.endpoint == "mantle"
    assert e.region_code == "us-gov-west-1"
    # per-1M → per-1k: 2.64/1000 = 0.00264
    assert e.short["input"] == Decimal("0.00264")
    assert e.short["output"] == Decimal("0.015840")
    assert e.short["cache_read"] == Decimal("0.000264")
    assert e.short["cache_write"] == Decimal("0.0033")
    # long tier
    assert e.long["input"] == Decimal("0.00528")
    assert e.long["output"] == Decimal("0.023760")
    assert e.long["cache_read"] == Decimal("0.000528")
    assert e.long["cache_write"] == Decimal("0.0066")


def test_long_is_exactly_2x_short_input_and_1_5x_output():
    """실측 배수 관계 — 파서가 short/long 을 뒤바꾸면 깨진다."""
    with patch("app.services.aws_price_list.boto3.client", return_value=_patched_client()):
        e = fetch_bedrock_prices("us-gov-west-1")["openai.gpt-5.6-terra"]
    assert e.long["input"] == e.short["input"] * 2
    assert e.long["output"] == e.short["output"] * Decimal("1.5")


def test_threshold_constant_is_272000():
    assert LONG_CONTEXT_THRESHOLD_TOKENS == 272_000


def test_per_1k_quantized_to_storage_scale_so_sync_never_422s():
    """per-1M/1000 을 저장 형식(NUMERIC(12,8), PricingRequest decimal_places=8)에 맞춘다.

    $0.5551/1M → /1000 = 0.0005551 (7dp). 저장 형식이 소수 8자리이므로 **그대로 보존**된다.
    ⚠️ 이 테스트는 예전에 0.000555(6dp)를 단정했다 — 저장 형식에 맞춘다는 의도는 같지만, 그때는
       형식 자체가 6자리여서 정밀도 손실을 정답으로 고정하고 있었다. 마이그레이션 0040 이 열을
       넓힌 뒤로는 손실 없이 저장할 수 있으므로 단정을 바꾼다.
    """
    page = {"PriceList": [
        _sku("USE1-openai.gpt-5.6-mini-mantle-input-tokens-standard", "0.5551000000", region="us-east-1"),
    ]}
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [page]
    client.get_paginator.return_value = paginator
    with patch("app.services.aws_price_list.boto3.client", return_value=client):
        out = fetch_bedrock_prices("us-east-1")
    val = out["openai.gpt-5.6-mini"].short["input"]
    assert val == Decimal("0.0005551")
    assert val.as_tuple().exponent == -8  # 저장형식(NUMERIC 12,8)과 정확히 일치


# ── 단위 파싱은 substring 이 아니라 수량 파싱 (1000000 오독 방지) ──

def test_price_per_1k_bare_millions_unit_not_misread_as_1k():
    """'1000000 tokens'(bare per-1M)를 1K 로 오독하면 1000배 과소청구 — /1000 되어야 한다."""
    assert _price_per_1k("1000000 tokens", "2.6400000000") == Decimal("0.002640")
    assert _price_per_1k("1M tokens", "2.6400000000") == Decimal("0.002640")
    assert _price_per_1k("1,000,000 tokens", "2.6400000000") == Decimal("0.002640")
    assert _price_per_1k("1K tokens", "0.5000000000") == Decimal("0.500000")
    assert _price_per_1k("1000 tokens", "0.5000000000") == Decimal("0.500000")


def test_price_per_1k_rejects_unparseable_unit():
    with pytest.raises(ValueError):
        _price_per_1k("per request", "1.0")


# ── SKU 하나가 깨져도 전체 fetch 를 죽이지 않는다 ──

def test_one_broken_sku_is_skipped_not_fatal():
    good = _sku("USE1-openai.gpt-5.6-terra-mantle-input-tokens-standard", "2.64", region="us-east-1")
    bad = _sku("USE1-openai.gpt-5.6-terra-mantle-output-tokens-standard", "12.0",
               unit="per potato", region="us-east-1")  # 파싱 불가 단위
    page = {"PriceList": [good, bad]}
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [page]
    client.get_paginator.return_value = paginator
    with patch("app.services.aws_price_list.boto3.client", return_value=client):
        out = fetch_bedrock_prices("us-east-1")
    # 깨진 output SKU 는 건너뛰고 input 은 정상 반영 — 전체가 죽지 않는다
    assert out["openai.gpt-5.6-terra"].short["input"] == Decimal("0.002640")
    assert "output" not in out["openai.gpt-5.6-terra"].short


# ── long_tier_enabled 는 input+output 둘 다 있을 때만 True (preview/sync 공유 술어) ──

def test_long_tier_enabled_requires_input_and_output():
    full = AwsModelPrice("openai.gpt-5.6-terra", "us-east-1", "mantle",
                         short={"input": Decimal("0.002")},
                         long={"input": Decimal("0.004"), "output": Decimal("0.018")})
    assert long_tier_enabled(full) is True
    # cache long 만 있고 input/output 없음 → 티어 아님(preview 가 켠다고 오보하지 않게)
    cache_only = AwsModelPrice("openai.gpt-5.6-terra", "us-east-1", "mantle",
                               short={}, long={"cache_read": Decimal("0.0004")})
    assert long_tier_enabled(cache_only) is False
    assert long_tier_enabled(AwsModelPrice("x", "us-east-1", "mantle", short={}, long={})) is False


# ---------------------------------------------------------------------------
# 7자리 단가가 동기화에서 깎이지 않는다 (마이그레이션 0040 과 한 쌍)
# ---------------------------------------------------------------------------
# 0040 이 단가 열을 NUMERIC(12,8) 로 넓혔다. 여기 양자화가 6자리로 남아 있으면 DB 에 닿기
# 전에 앱이 먼저 반올림해 0040 이 무의미해진다. Haiku 5.5 의 `us.` 5분 캐시 쓰기
# $0.1375/1M 이 실제 사례다(per-1K 로 0.0001375, 소수 7자리).


def test_seven_decimal_rate_survives_quantization():
    val = _price_per_1k("1M tokens", "0.1375")
    assert val == Decimal("0.0001375")
    # 6자리 반올림이었다면 0.000138 이 된다 — 그 값이 아님을 직접 단정한다.
    assert val != Decimal("0.000138")


def test_quantization_is_eight_decimals():
    # 8자리까지는 그대로, 9자리째는 ROUND_HALF_UP.
    assert _price_per_1k("1M tokens", "0.01") == Decimal("0.00001")
    assert _price_per_1k("1M tokens", "0.000015") == Decimal("0.00000002")  # 0.000000015 → 반올림
    assert _price_per_1k("1M tokens", "0.1375").as_tuple().exponent == -8
