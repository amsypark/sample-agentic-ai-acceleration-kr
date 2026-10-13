# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""API 단가 검증이 DB scale(NUMERIC(12,8))과 같은 정밀도를 받는다.

마이그레이션 0040 이 단가 열을 소수 8자리로 넓혔는데 PricingRequest 가 decimal_places=6 으로
남아 있으면, DB 는 받을 수 있는 7자리 요율을 API 가 422 로 거부한다. 이 파일은 그 불일치가
다시 생기지 않게 막는다 — DB scale 과 API decimal_places 가 함께 움직여야 한다.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import models as m


def _pricing_cls():
    for name in ("PricingRequest", "ModelPricingRequest", "PricingCreate"):
        cls = getattr(m, name, None)
        if cls is not None:
            return cls
    pytest.skip("단가 요청 스키마를 찾지 못했다")


def _payload(**over):
    base = {
        "input_price_per_1k_tokens": Decimal("0.003"),
        "output_price_per_1k_tokens": Decimal("0.015"),
        "effective_from": "2026-10-13T00:00:00Z",
    }
    base.update(over)
    return base


@pytest.mark.unit
def test_seven_decimal_rate_is_accepted():
    cls = _pricing_cls()
    obj = cls(**_payload(cache_creation_5m_price_per_1k_tokens=Decimal("0.0001375")))
    assert obj.cache_creation_5m_price_per_1k_tokens == Decimal("0.0001375")


@pytest.mark.unit
def test_eight_decimal_rate_is_accepted():
    cls = _pricing_cls()
    obj = cls(**_payload(cache_read_price_per_1k_tokens=Decimal("0.00000001")))
    assert obj.cache_read_price_per_1k_tokens == Decimal("0.00000001")


@pytest.mark.unit
def test_nine_decimal_rate_is_rejected():
    # DB 가 9자리를 반올림해 버리므로 API 가 먼저 막는 편이 낫다(조용한 반올림 대신 422).
    cls = _pricing_cls()
    with pytest.raises(ValidationError):
        cls(**_payload(cache_read_price_per_1k_tokens=Decimal("0.000000001")))


@pytest.mark.unit
def test_max_price_matches_numeric_12_8_integer_part():
    assert m.MAX_PRICE_PER_1K == Decimal("9999.99999999")
