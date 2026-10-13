# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""service_tier gate (services/service_tier_gate.py) on both OpenAI-dialect routes.

What is pinned here:

  * a tier the gateway has no price for is refused with a 400 that names the field,
    on BOTH Bedrock OpenAI planes and on BOTH routes (/v1/chat/completions, /v1/responses);
  * the refusal happens BEFORE ``enforce_rate_limits`` — a 400 after the pre-reservation
    would hold TPM/cost the request never used;
  * Standard-tier values (absent, null, 'auto', 'default') pass and are forwarded
    unchanged — the gate refuses, it never rewrites the body;
  * other providers are untouched (the Anthropic dialect drops the field itself, vLLM is
    not billed per tier).
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import JSONResponse

from app.routers.openai_compat import _handle_openai, _handle_responses
from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.schemas.routing import RoutingProfileSchema
from app.services.service_tier_gate import reject_unpriced_service_tier

RUNTIME_ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
MANTLE_ENDPOINT = "https://bedrock-mantle.us-east-2.api.aws/openai"

GATED = [ProviderType.BEDROCK_RUNTIME_OPENAI, ProviderType.BEDROCK_MANTLE_OPENAI]
STANDARD_VALUES = [None, "auto", "default"]
# 'ultrafast' is the one Bedrock accepts on GPT-6 Astra / 6.1 Sol (billed 6x); the rest
# are what a client could send. Case matters upstream, so 'Default' is not Standard.
UNPRICED_VALUES = ["ultrafast", "priority", "flex", "scale", "Default", 123, ["ultrafast"],
                   {"tier": "ultrafast"}]


# ── the pure function ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider", GATED)
@pytest.mark.parametrize("tier", STANDARD_VALUES)
def test_standard_tier_values_pass(provider, tier):
    body = {"model": "x", "input": "hi"}
    if tier is not None:
        body["service_tier"] = tier
    assert reject_unpriced_service_tier(body, provider) is None
    # explicit JSON null is the same as absent
    assert reject_unpriced_service_tier({"service_tier": None}, provider) is None


@pytest.mark.parametrize("provider", GATED)
@pytest.mark.parametrize("tier", UNPRICED_VALUES)
def test_unpriced_tier_is_refused_with_a_named_400(provider, tier):
    resp = reject_unpriced_service_tier({"model": "x", "service_tier": tier}, provider)
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    err = json.loads(resp.body)["error"]
    assert err["type"] == "invalid_request_error"
    assert err["param"] == "service_tier"
    assert err["code"] == "unsupported_service_tier"


@pytest.mark.parametrize(
    "provider",
    [ProviderType.BEDROCK, ProviderType.BEDROCK_MANTLE, ProviderType.OPENMODEL],
)
def test_other_providers_are_not_gated(provider):
    assert reject_unpriced_service_tier({"service_tier": "ultrafast"}, provider) is None


def test_string_provider_value_is_gated_too():
    # ProviderType is a str Enum; a row that carries the raw string must not slip past.
    resp = reject_unpriced_service_tier({"service_tier": "ultrafast"}, "BEDROCK_RUNTIME_OPENAI")
    assert resp is not None and resp.status_code == 400


def test_non_dict_body_is_left_to_the_route():
    body = ["service_tier"]
    assert reject_unpriced_service_tier(body, ProviderType.BEDROCK_RUNTIME_OPENAI) is None


def test_message_is_bounded_for_a_huge_value():
    resp = reject_unpriced_service_tier(
        {"service_tier": "u" * 10_000}, ProviderType.BEDROCK_RUNTIME_OPENAI
    )
    assert len(json.loads(resp.body)["error"]["message"]) < 400


# ── route fakes ───────────────────────────────────────────────────────────────


def _model_config(provider: ProviderType, alias: str, pmid: str) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id=pmid,
        alias=alias,
        provider=provider,
        api_format=ApiFormat.OPENAI_RESPONSES,
        endpoint=RUNTIME_ENDPOINT if provider == ProviderType.BEDROCK_RUNTIME_OPENAI
        else MANTLE_ENDPOINT,
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.011"), output_per_1k=Decimal("0.055")
        ),
        status=ModelStatus.ACTIVE,
    )


class _FakeRequest:
    def __init__(self, body: dict, state: dict, app_state) -> None:
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = MagicMock()
        self.app.state = app_state

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


def _adapter():
    adapter = MagicMock()
    adapter.invoke = AsyncMock(
        return_value=(
            200,
            json.dumps({"status": "completed",
                        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}}
                       ).encode(),
            {},
            TokenUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        )
    )
    adapter.invoke_stream = AsyncMock()
    return adapter


def _app_state(adapter, profile=None):
    app_state = MagicMock()
    app_state.provider_registry.get = MagicMock(return_value=adapter)
    app_state.cost_recorder.finalize = AsyncMock()
    loader = MagicMock()
    loader.load = AsyncMock(return_value=profile)
    app_state.routing_profile_loader = loader
    app_state.agentcore_mcp_client = None
    app_state.body_logger = None  # see test_responses_model_selection: MagicMock is truthy
    app_state.tokenizer = None
    return app_state


def _state(auth_context):
    return {
        "auth_context": auth_context,
        "client": "codex",
        "_redis": None,
        "_session_factory": None,
        "request_id": "req-tier",
    }


@pytest.fixture
def rate_limits(monkeypatch):
    """Record whether the pre-reservation ran; answer with a sentinel 429 when it does.

    The sentinel lets a Standard-tier request prove it got PAST the gate and reached
    enforce_rate_limits, without having to fake the whole upstream call.
    """
    calls: list[dict] = []

    async def _enforce(**kwargs):
        calls.append(kwargs)
        return JSONResponse(status_code=429, content={"error": {"type": "sentinel"}})

    monkeypatch.setattr(
        "app.services.rate_limit_enforcement.enforce_rate_limits", _enforce
    )
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.check_key_scope", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.check_client_model_scope",
        lambda *a, **k: None,
    )
    return calls


# ── /v1/chat/completions (runtime plane only) ─────────────────────────────────


def _chat_request(body: dict, auth_context):
    adapter = _adapter()
    request = _FakeRequest(body, _state(auth_context), _app_state(adapter))
    return request, adapter


@pytest.mark.asyncio
async def test_chat_refuses_ultrafast_before_reserving(monkeypatch, rate_limits):
    cfg = _model_config(ProviderType.BEDROCK_RUNTIME_OPENAI, "gpt-6-astra", "us.openai.gpt-6-astra")

    async def _resolve(redis, db, alias):
        return cfg

    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_openai_model", _resolve
    )
    request, adapter = _chat_request(
        {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}],
         "service_tier": "ultrafast"},
        auth_context=MagicMock(),
    )

    resp = await _handle_openai(request, "/v1/chat/completions")

    assert resp.status_code == 400
    assert json.loads(resp.body)["error"]["param"] == "service_tier"
    assert rate_limits == [], "the gate must run before enforce_rate_limits (reservation leak)"
    adapter.invoke.assert_not_awaited()
    adapter.invoke_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_standard_tier_reaches_rate_limits(monkeypatch, rate_limits):
    cfg = _model_config(ProviderType.BEDROCK_RUNTIME_OPENAI, "gpt-6-astra", "us.openai.gpt-6-astra")

    async def _resolve(redis, db, alias):
        return cfg

    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_openai_model", _resolve
    )
    request, _ = _chat_request(
        {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}],
         "service_tier": "default"},
        auth_context=MagicMock(),
    )

    resp = await _handle_openai(request, "/v1/chat/completions")

    assert resp.status_code == 429, "Standard tier must pass the gate and hit the sentinel"
    assert len(rate_limits) == 1


# ── /v1/responses (both planes) ───────────────────────────────────────────────


def _profile(default_model: str, backend: str) -> RoutingProfileSchema:
    return RoutingProfileSchema(
        client="codex",
        backend=backend,
        account_role_arn=None,
        region="us-east-2",
        default_model=default_model,
        external_id=None,
    )


PLANES = [
    (ProviderType.BEDROCK_RUNTIME_OPENAI, "gpt-6-astra", "us.openai.gpt-6-astra", "bedrock_openai"),
    (ProviderType.BEDROCK_MANTLE_OPENAI, "codex-gpt-5.6-terra", "openai.gpt-5.6-terra", "mantle"),
]


def _responses_request(monkeypatch, plane, body: dict, auth_context):
    provider, alias, pmid, backend = plane
    cfg = _model_config(provider, alias, pmid)

    async def _resolve(redis, db, model_ref):
        if model_ref == alias:
            return cfg
        raise LookupError(model_ref)

    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", _resolve
    )
    adapter = _adapter()
    request = _FakeRequest(
        body, _state(auth_context), _app_state(adapter, _profile(alias, backend))
    )
    return request, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("plane", PLANES, ids=lambda p: p[0].value)
async def test_responses_refuses_ultrafast_before_reserving(monkeypatch, rate_limits, plane):
    request, adapter = _responses_request(
        monkeypatch, plane,
        {"model": plane[1], "input": "hi", "service_tier": "ultrafast"},
        auth_context=MagicMock(),
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 400
    assert json.loads(resp.body)["error"]["code"] == "unsupported_service_tier"
    assert rate_limits == [], "the gate must run before enforce_rate_limits (reservation leak)"
    adapter.invoke.assert_not_awaited()
    adapter.invoke_stream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("plane", PLANES, ids=lambda p: p[0].value)
async def test_responses_standard_tier_reaches_rate_limits(monkeypatch, rate_limits, plane):
    request, _ = _responses_request(
        monkeypatch, plane,
        {"model": plane[1], "input": "hi", "service_tier": "auto"},
        auth_context=MagicMock(),
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 429
    assert len(rate_limits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("plane", PLANES, ids=lambda p: p[0].value)
async def test_responses_without_auth_context_still_gated(monkeypatch, plane):
    """The gate is about billing, not entitlement: it does not hide behind auth_context."""
    request, adapter = _responses_request(
        monkeypatch, plane,
        {"model": plane[1], "input": "hi", "service_tier": "priority"},
        auth_context=None,
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 400
    adapter.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_responses_standard_tier_is_forwarded_unchanged(monkeypatch):
    """The gate refuses or passes; it never rewrites. 'default' reaches the upstream body."""
    request, adapter = _responses_request(
        monkeypatch, PLANES[0],
        {"model": "gpt-6-astra", "input": "hi", "service_tier": "default"},
        auth_context=None,
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    sent = json.loads(adapter.invoke.await_args.args[0])
    assert sent["service_tier"] == "default"
    assert sent["model"] == "us.openai.gpt-6-astra"
