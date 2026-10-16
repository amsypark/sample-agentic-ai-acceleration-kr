# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: Malformed model cache entry must return None, not raise.

Bug: A malformed/legacy cache entry caused ValidationError (permanent 500).
Fix: try/except in _parse_cached_model returning None (treated as cache miss).
"""

from __future__ import annotations

import json

import pytest

from app.services.router_service import _parse_cached_model


def test_invalid_json_returns_none():
    """Completely invalid JSON should return None (not raise)."""
    result = _parse_cached_model("not json at all{{{", model_ref="test-model")
    assert result is None


def test_empty_string_returns_none():
    """Empty string should return None."""
    result = _parse_cached_model("", model_ref="test-model")
    assert result is None


def test_valid_json_wrong_structure_returns_none():
    """Valid JSON but missing required fields should return None."""
    result = _parse_cached_model(
        json.dumps({"foo": "bar", "baz": 123}),
        model_ref="test-model",
    )
    assert result is None


def test_legacy_flat_shape_without_pricing_returns_none():
    """Legacy cache entry without nested pricing should return None."""
    legacy = json.dumps({
        "provider_model_id": "anthropic.claude-v2",
        "provider": "bedrock",
        "api_format": "bedrock_native",
        "endpoint": "us-east-1",
        # Missing "pricing" — legacy shape
    })
    result = _parse_cached_model(legacy, model_ref="test-model")
    assert result is None


def test_valid_cache_entry_returns_model_config():
    """A properly formed cache entry should parse correctly."""
    valid = json.dumps({
        "provider_model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "provider": "BEDROCK",
        "api_format": "BEDROCK_NATIVE",
        "endpoint": "us-east-1",
        "pricing": {
            "input_per_1k": "0.003",
            "output_per_1k": "0.015",
        },
        "status": "ACTIVE",
        # 지금 이미지가 쓰는 payload 는 계정 칸을 항상 싣는다(null 이어도). 칸이 없는 것은 0043 이전
        # 파드의 payload 로 보고 미스다 — test_router_service.py 의 같은 이름 테스트들.
        "account_role_arn": None,
        "account_external_id": None,
    })
    result = _parse_cached_model(valid, model_ref="test-model")
    assert result is not None
    assert result.provider_model_id == "us.anthropic.claude-sonnet-4-20250514-v1:0"


def test_none_input_returns_none():
    """None-like input should not crash."""
    # The function takes a str, but in edge cases...
    try:
        result = _parse_cached_model("{}", model_ref="test-model")
        # Empty dict — missing required fields
        assert result is None
    except Exception:
        # If it raises, the fix is not in place
        pytest.fail("_parse_cached_model raised instead of returning None")
