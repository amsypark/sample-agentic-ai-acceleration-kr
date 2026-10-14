# Copyright 2026 © Amazon.com and Affiliates.
"""BedrockOpenAIAdapter — the standard bedrock-runtime plane (SigV4 + CRIS ids).

Sibling of ``test_mantle_openai_adapter.py``. The two adapters speak the same OpenAI
dialect, so the tests that matter here are the ones where they DIFFER:

* the request id — the runtime plane's ``x-amzn-requestid`` is a real AWS id that joins to
  a model-invocation log record, so it must reach the router; Mantle's ``req_...`` joins to
  nothing and is deliberately dropped.
* two wires — ``responses`` re-frames downstream, ``chat`` passes through, and each has a
  different generator. Sharing one would corrupt whichever wire lost.
* the signing region — bound to the endpoint host, because a signature is region-specific.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter, _region_for
from app.schemas.routing import RoutingProfileSchema

ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
MODEL = "us.openai.gpt-5.6-terra"
REQ_ID = "9f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f"


def _profile(region="us-east-2", role_arn=None, external_id=None):
    return RoutingProfileSchema(
        client="codex",
        backend="bedrock-runtime",
        account_role_arn=role_arn,
        region=region,
        default_model="gpt-5.6-terra",
        external_id=external_id,
    )


def _signer():
    s = MagicMock()
    s.sign = AsyncMock(return_value={"Authorization": "AWS4-HMAC-SHA256 ...", "content-type": "application/json"})
    return s


def _adapter(http_client, signer=None):
    return BedrockOpenAIAdapter(http_client=http_client, signer=signer or _signer())


def _responses_body(input_tokens=8, output_tokens=2, cached=0, reasoning=1):
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
        "status": "completed",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _chat_body(prompt=8, completion=2, cached=0):
    return {
        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _resp(status=200, body=None, headers=None, url=ENDPOINT):
    return httpx.Response(
        status,
        json=body if body is not None else {},
        headers=headers or {},
        request=httpx.Request("POST", url),
    )


# ── _region_for ───────────────────────────────────────────────────────────────
def test_region_comes_from_the_endpoint_host():
    assert _region_for(ENDPOINT, _profile(region="us-east-2")) == "us-east-2"


def test_endpoint_host_wins_over_a_disagreeing_profile():
    """The host is authoritative: SigV4 binds the signature to the region it names.

    Honouring the profile instead would sign us-east-1 for a us-east-2 host, which is a
    403 SignatureDoesNotMatch rather than a cross-region call.
    """
    assert _region_for(ENDPOINT, _profile(region="ap-northeast-2")) == "us-east-2"


def test_region_falls_back_to_the_profile_for_a_nonstandard_host():
    # e.g. a VPC endpoint / FIPS host the regex does not match.
    assert _region_for("https://vpce-abc.bedrock-runtime.example", _profile(region="us-west-2")) == "us-west-2"


def test_a_missing_profile_is_survivable_on_a_normal_endpoint():
    """The router treats a routing profile as optional (a failed load must not 500), so the
    host alone has to be enough to sign with."""
    assert _region_for(ENDPOINT, None) == "us-east-2"


def test_undeterminable_region_raises_rather_than_guessing():
    """No profile AND an unrecognised host: raise instead of defaulting to some region.

    A guessed region signs fine locally and fails at AWS with 403 SignatureDoesNotMatch,
    which reads as a credentials problem and sends the operator hunting in the wrong place.
    """
    with pytest.raises(ValueError, match="signing region"):
        _region_for("https://example.invalid", None)


# ── invoke (non-streaming) ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_invoke_responses_posts_to_the_responses_path_and_returns_the_request_id():
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _responses_body(), {"x-amzn-requestid": REQ_ID}))
    status, body, headers, usage = await _adapter(http).invoke(
        b'{"input":"hi"}', MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    assert status == 200
    assert http.post.await_args.args[0] == f"{ENDPOINT}/v1/responses"
    # The join key to usage_logs.bedrock_request_id — the whole reason the runtime plane is
    # auditable and Mantle is not.
    assert headers == {"x-amzn-requestid": REQ_ID}
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (8, 2, 1)
    assert json.loads(body)["status"] == "completed"


@pytest.mark.asyncio
async def test_invoke_chat_posts_to_the_chat_path_and_parses_chat_usage():
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _chat_body(prompt=11, completion=3, cached=4)))
    status, _body, _headers, usage = await _adapter(http).invoke(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat"
    )
    assert status == 200
    assert http.post.await_args.args[0] == f"{ENDPOINT}/v1/chat/completions"
    # Chat dialect keys (prompt_tokens/completion_tokens) — parsing this body with the
    # Responses parser would silently record zero usage, i.e. a free request.
    assert (usage.input_tokens, usage.cache_read_input_tokens, usage.output_tokens) == (7, 4, 3)


@pytest.mark.asyncio
async def test_invoke_sends_the_body_verbatim():
    """The signature covers these exact bytes, so re-serialising would 403."""
    body = b'{"model":"us.openai.gpt-5.6-terra",  "input":"hi"}'
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _responses_body()))
    await _adapter(http).invoke(body, MODEL, profile=_profile(), endpoint=ENDPOINT)
    assert http.post.await_args.kwargs["content"] == body


@pytest.mark.asyncio
async def test_invoke_signs_with_the_host_region_and_profile_role():
    signer = _signer()
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _responses_body()))
    await _adapter(http, signer).invoke(
        b"{}",
        MODEL,
        profile=_profile(role_arn="arn:aws:iam::333344445555:role/x", external_id="ext-1"),
        endpoint=ENDPOINT,
    )
    kw = signer.sign.await_args.kwargs
    assert kw["region"] == "us-east-2"
    assert kw["url"] == f"{ENDPOINT}/v1/responses"
    assert kw["role_arn"] == "arn:aws:iam::333344445555:role/x"
    assert kw["external_id"] == "ext-1"


@pytest.mark.asyncio
async def test_invoke_error_still_returns_the_request_id():
    """An audit that can only correlate successful requests is not an audit."""
    http = MagicMock()
    http.post = AsyncMock(
        return_value=_resp(
            400, {"error": {"message": "The provided model identifier is invalid."}},
            {"x-amzn-requestid": REQ_ID},
        )
    )
    status, body, headers, usage = await _adapter(http).invoke(
        b"{}", "openai.gpt-5.6-terra", profile=_profile(), endpoint=ENDPOINT
    )
    assert status == 400  # the real status, not a flattened 502
    assert headers == {"x-amzn-requestid": REQ_ID}
    assert usage.total_tokens == 0
    assert b"invalid" in body


@pytest.mark.asyncio
async def test_missing_request_id_yields_an_empty_dict_not_a_none_value():
    """``{}`` rather than ``{"x-amzn-requestid": None}``: NULL means "nothing to join to",
    and a key present with None would make any ``in headers`` check lie."""
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _responses_body()))
    _s, _b, headers, _u = await _adapter(http).invoke(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT
    )
    assert headers == {}


@pytest.mark.asyncio
async def test_signing_failure_is_502_and_never_reaches_the_network():
    signer = MagicMock()
    signer.sign = AsyncMock(side_effect=RuntimeError("no IRSA credentials"))
    http = MagicMock()
    http.post = AsyncMock()
    status, body, headers, usage = await _adapter(http, signer).invoke(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT
    )
    assert status == 502
    assert b"signing failed" in body
    assert headers == {}
    http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_failure_is_502_not_an_exception():
    http = MagicMock()
    http.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    status, body, _h, usage = await _adapter(http).invoke(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT
    )
    assert status == 502
    assert b"call failed" in body
    assert usage.total_tokens == 0


@pytest.mark.asyncio
async def test_unknown_wire_is_502_not_a_wrong_url():
    """A bad wire name must not fall through to some default path.

    Silently posting a Chat body to /v1/responses would be a confusing 400 from AWS
    attributed to the model rather than to our own routing.
    """
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _responses_body()))
    status, body, _h, _u = await _adapter(http).invoke(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="completions"
    )
    assert status == 502
    http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_unparseable_success_body_records_zero_usage_not_a_500():
    http = MagicMock()
    resp = httpx.Response(200, content=b"<html>gateway timeout</html>",
                          request=httpx.Request("POST", ENDPOINT))
    http.post = AsyncMock(return_value=resp)
    status, _b, _h, usage = await _adapter(http).invoke(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT
    )
    assert status == 200
    assert usage.total_tokens == 0


@pytest.mark.asyncio
async def test_non_cris_model_id_is_passed_through_with_a_warning_not_blocked():
    """AWS's own ValidationException is clearer than a guess from us, and a future model
    may be ON_DEMAND — so a missing us./global. prefix is logged, not rejected locally."""
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp(200, _responses_body()))
    status, _b, _h, _u = await _adapter(http).invoke(
        b"{}", "openai.gpt-5.6-terra", profile=_profile(), endpoint=ENDPOINT
    )
    assert status == 200
    http.post.assert_awaited_once()


# ── invoke_stream ─────────────────────────────────────────────────────────────
class _FakeStream:
    """Minimal httpx streaming context manager."""

    def __init__(self, status=200, headers=None, lines=None, chunks=None, raise_on_enter=None):
        self._resp = MagicMock()
        self._resp.status_code = status
        self._resp.headers = headers or {}
        self._resp.aread = AsyncMock(return_value=b'{"error":{"message":"nope"}}')
        self._lines = lines or []
        self._chunks = chunks or []
        self._raise = raise_on_enter
        self.exited = False

        async def _aiter_lines():
            for line in self._lines:
                yield line

        async def _aiter_bytes():
            for chunk in self._chunks:
                yield chunk

        self._resp.aiter_lines = _aiter_lines
        self._resp.aiter_bytes = _aiter_bytes

    async def __aenter__(self):
        if self._raise:
            raise self._raise
        return self._resp

    async def __aexit__(self, *exc):
        self.exited = True
        return False


def _streaming_http(stream):
    http = MagicMock()
    http.stream = MagicMock(return_value=stream)
    return http


SSE_LINES = [
    'data: {"type":"response.created"}',
    "",
    'data: {"type":"response.output_text.delta","delta":"OK"}',
    'data: {"type":"response.completed","response":{"usage":{"input_tokens":8,"output_tokens":2}}}',
    "data: [DONE]",
]


@pytest.mark.asyncio
async def test_stream_responses_yields_bare_json_and_drops_done():
    """``responses_sse_stream`` RE-FRAMES, so it wants the payload with ``data:`` stripped.

    Leaving the prefix on would double-frame every event downstream.
    """
    stream = _FakeStream(200, {"x-amzn-requestid": REQ_ID}, lines=SSE_LINES)
    status, gen, headers, req_id = await _adapter(_streaming_http(stream)).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    assert status == 200
    assert req_id == REQ_ID
    assert headers["Content-Type"] == "text/event-stream"
    out = [c async for c in gen]
    assert all(not c.startswith(b"data:") for c in out)
    assert json.loads(out[0])["type"] == "response.created"
    assert b"[DONE]" not in b"".join(out)
    assert len(out) == 3  # created + delta + completed; blank line and [DONE] dropped
    assert stream.exited  # the context manager is always closed


@pytest.mark.asyncio
async def test_stream_chat_passes_bytes_through_untouched():
    """``openai_sse_stream`` PASSES THROUGH and scans for ``data: `` lines itself, so the
    chat wire must not be re-framed — stripping here would break its usage scanner."""
    raw = [b'data: {"choices":[{"delta":{"content":"O"}}]}\n\n', b"data: [DONE]\n\n"]
    stream = _FakeStream(200, {"x-amzn-requestid": REQ_ID}, chunks=raw)
    status, gen, _h, req_id = await _adapter(_streaming_http(stream)).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat"
    )
    assert status == 200
    assert [c async for c in gen] == raw
    assert req_id == REQ_ID


@pytest.mark.asyncio
async def test_stream_non_200_surfaces_the_real_status_before_yielding():
    """Status is read BEFORE returning, so a 400 is a 400 — not a 200 with an error buried
    in the SSE body, which is what a client would otherwise retry against forever."""
    stream = _FakeStream(400, {"x-amzn-requestid": REQ_ID})
    status, gen, headers, req_id = await _adapter(_streaming_http(stream)).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    assert status == 400
    assert req_id == REQ_ID  # AWS answered, so a log record may exist for the rejection
    assert headers == {}
    chunks = [c async for c in gen]
    assert b"HTTP 400" in chunks[0]
    assert stream.exited


@pytest.mark.asyncio
async def test_stream_connect_failure_is_502_with_no_request_id():
    """No response means no id to give — None here is "nothing to join to", not a drop."""
    stream = _FakeStream(raise_on_enter=httpx.ConnectError("boom"))
    status, gen, _h, req_id = await _adapter(_streaming_http(stream)).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    assert status == 502
    assert req_id is None
    assert b"stream failed" in [c async for c in gen][0]


@pytest.mark.asyncio
async def test_stream_signing_failure_frames_the_error_per_wire():
    """A bare-JSON error on the chat wire would land in the client's event stream as
    garbage, so the error frame has to match the wire's downstream contract."""
    signer = MagicMock()
    signer.sign = AsyncMock(side_effect=RuntimeError("no creds"))
    http = MagicMock()
    http.stream = MagicMock()

    status, gen, _h, req_id = await _adapter(http, signer).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    assert status == 502 and req_id is None
    first = [c async for c in gen][0]
    assert not first.startswith(b"data:")  # re-framed downstream

    status, gen, _h, _r = await _adapter(http, signer).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat"
    )
    first = [c async for c in gen][0]
    assert first.startswith(b"data: ") and first.endswith(b"\n\n")  # passthrough
    http.stream.assert_not_called()


@pytest.mark.asyncio
async def test_stream_mid_flight_failure_emits_an_error_frame_and_closes():
    class _Boom(_FakeStream):
        def __init__(self):
            super().__init__(200, {"x-amzn-requestid": REQ_ID})

            async def _aiter_lines():
                yield 'data: {"type":"response.created"}'
                raise httpx.ReadError("connection reset")

            self._resp.aiter_lines = _aiter_lines

    stream = _Boom()
    _s, gen, _h, _r = await _adapter(_streaming_http(stream)).invoke_stream(
        b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    out = [c async for c in gen]
    assert json.loads(out[0])["type"] == "response.created"
    assert b"stream failed" in out[-1]
    assert stream.exited


# ── count_tokens ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_count_tokens_returns_zero_rather_than_inventing_a_number():
    """The OpenAI wires have no count endpoint and Bedrock CountTokens rejects these models,
    so 0 keeps the (status, input_tokens) contract without fabricating a count."""
    assert await _adapter(MagicMock()).count_tokens(b"{}", MODEL) == (200, 0)


# ── chat 스트림의 usage: bedrock-runtime 은 요청하지 않으면 usage 를 싣지 않는다 ───────
#
# 측정(2026-10, us-east-2, /openai/v1/chat/completions, stream=true, us.openai.gpt-6-luna):
#   stream_options 없음                 → 청크 4개, usage 어디에도 없음
#   stream_options.include_usage=true   → 청크 5개, 마지막(choices: [])에 usage
# 아래 픽스처는 그 청크 **모양**을 재구성한 것이다(원본 바이트가 아니다). 대역 상류는 측정대로
# 요청 본문에 include_usage 가 있을 때만 usage 청크를 보낸다.

_CHUNK_HEAD = b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,'
_CHAT_ROLE = _CHUNK_HEAD + (
    b'"model":"openai.gpt-6-luna","choices":[{"index":0,'
    b'"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
)
_CHAT_TEXT = _CHUNK_HEAD + (
    b'"model":"openai.gpt-6-luna","choices":[{"index":0,'
    b'"delta":{"content":"Hello there"},"finish_reason":null}]}\n\n'
)
_CHAT_STOP = _CHUNK_HEAD + (
    b'"model":"openai.gpt-6-luna","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
)
_CHAT_USAGE = _CHUNK_HEAD + (
    b'"model":"openai.gpt-6-luna","choices":[],"usage":{"prompt_tokens":12,'
    b'"completion_tokens":6,"total_tokens":18,"prompt_tokens_details":{"cached_tokens":0},'
    b'"completion_tokens_details":{"reasoning_tokens":0}}}\n\n'
)
_CHAT_DONE = b"data: [DONE]\n\n"
CHAT_WITHOUT_USAGE = [_CHAT_ROLE, _CHAT_TEXT, _CHAT_STOP, _CHAT_DONE]
CHAT_WITH_USAGE = [_CHAT_ROLE, _CHAT_TEXT, _CHAT_STOP, _CHAT_USAGE, _CHAT_DONE]


def _measured_runtime_http():
    """요청 본문의 include_usage 에 따라 측정된 청크를 돌려주는 bedrock-runtime 대역."""
    sent: list[bytes] = []

    def _stream(method, url, content=None, headers=None):
        sent.append(content)
        opts = json.loads(content).get("stream_options") or {}
        chunks = CHAT_WITH_USAGE if opts.get("include_usage") else CHAT_WITHOUT_USAGE
        return _FakeStream(200, {"x-amzn-requestid": REQ_ID}, chunks=chunks)

    http = MagicMock()
    http.stream = MagicMock(side_effect=_stream)
    return http, sent


_CHAT_REQ = {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream_options",
    [None, {"include_usage": False}, {"continuous_usage_stats": True}],
    ids=["absent", "false", "other-keys"],
)
async def test_chat_stream_forces_include_usage_before_signing(stream_options):
    """⚠️ 본문은 SigV4 서명 대상이다 — 강제는 서명 **전에**, 서명한 그 바이트를 보낸다."""
    req = dict(_CHAT_REQ)
    if stream_options is not None:
        req["stream_options"] = stream_options
    http, sent = _measured_runtime_http()
    signer = _signer()
    await _adapter(http, signer).invoke_stream(
        json.dumps(req).encode(), MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat"
    )
    signed = signer.sign.await_args.kwargs["body"]
    assert sent == [signed], "서명한 바이트와 보낸 바이트가 다르다(서명 불일치)"
    opts = json.loads(signed)["stream_options"]
    assert opts["include_usage"] is True
    if stream_options and "continuous_usage_stats" in stream_options:
        assert opts["continuous_usage_stats"] is True, "클라이언트의 다른 stream_options 를 지웠다"


@pytest.mark.asyncio
async def test_chat_stream_already_asking_for_usage_is_sent_byte_for_byte():
    raw = json.dumps({**_CHAT_REQ, "stream_options": {"include_usage": True}}).encode()
    http, sent = _measured_runtime_http()
    await _adapter(http).invoke_stream(
        raw, MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat"
    )
    assert sent == [raw]


@pytest.mark.asyncio
async def test_responses_wire_body_is_not_touched():
    raw = json.dumps({"model": MODEL, "stream": True, "input": "hi"}).encode()
    stream = _FakeStream(200, {"x-amzn-requestid": REQ_ID}, lines=SSE_LINES)
    http = _streaming_http(stream)
    await _adapter(http).invoke_stream(
        raw, MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses"
    )
    assert http.stream.call_args.kwargs["content"] == raw


async def _route_chat(monkeypatch, *, client_asks_usage: bool):
    """실제 _handle_openai + 실제 BedrockOpenAIAdapter(대역 HTTP·서명기)로 런타임 chat 스트림."""
    from decimal import Decimal
    from types import SimpleNamespace

    from starlette.datastructures import Headers

    from app.routers import openai_compat
    from app.schemas.domain import (
        ApiFormat,
        AuthContext,
        AuthType,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
        Role,
    )

    cfg = ModelConfigSchema(
        provider_model_id="us.openai.gpt-6-luna",
        alias="gpt-6-luna",
        provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
        api_format=ApiFormat.OPENAI_RESPONSES,
        endpoint=ENDPOINT,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.0002"), output_per_1k=Decimal("0.0012")),
        status=ModelStatus.ACTIVE,
    )

    async def _resolve(redis, db, alias):
        return cfg

    async def _no_limits(**_k):
        return None

    monkeypatch.setattr(openai_compat._router_service, "resolve_openai_model", _resolve)
    monkeypatch.setattr("app.services.rate_limit_enforcement.enforce_rate_limits", _no_limits)
    http, sent = _measured_runtime_http()
    adapter = _adapter(http)
    usages: list = []

    class _Recorder:
        async def finalize(self, redis, auth_context, model_config, usage, *a, **k):
            usages.append(usage)

    async def _load(*_a, **_k):
        return _profile()

    app_state = SimpleNamespace(
        provider_registry=SimpleNamespace(get=lambda _p: adapter),
        cost_recorder=_Recorder(),
        tokenizer=None,
        body_logger=None,
        metrics=None,
        agentcore_mcp_client=None,
        routing_profile_loader=SimpleNamespace(load=_load),
    )
    body = {"model": "gpt-6-luna", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    if client_asks_usage:
        body["stream_options"] = {"include_usage": True}
    auth = AuthContext(user_id="u-1", team_id="t-1", dept_id="d-1", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY)
    state = {"auth_context": auth, "client": "codex", "_redis": None,
             "_session_factory": None, "request_id": "req-chat"}

    class _Req:
        scope = {"state": state}
        app = SimpleNamespace(state=app_state)
        headers = Headers()

        async def body(self) -> bytes:
            return json.dumps(body).encode()

    resp = await openai_compat._handle_openai(_Req(), "/v1/chat/completions")
    client_bytes = b"".join([c async for c in resp.body_iterator])
    from app.services import bg_tasks

    await bg_tasks.drain(timeout=2.0)
    return client_bytes, usages, sent


@pytest.mark.asyncio
async def test_runtime_chat_stream_is_billed_from_real_usage(monkeypatch):
    """⚠️ 고치기 전: 상류가 usage 를 아예 안 보내 실제 usage 없이 청구됐다(여기서는 0)."""
    client_bytes, usages, _sent = await _route_chat(monkeypatch, client_asks_usage=False)
    (usage,) = usages
    assert (usage.input_tokens, usage.output_tokens) == (12, 6), usage
    assert not usage.estimated
    # 클라이언트는 요청하지 않은 usage 청크를 받지 않는다(OpenAI 계약과 같은 모양).
    assert b'"choices":[]' not in client_bytes and b'"usage"' not in client_bytes
    assert b"Hello there" in client_bytes and client_bytes.endswith(_CHAT_DONE)


@pytest.mark.asyncio
async def test_runtime_chat_stream_forwards_usage_the_client_asked_for(monkeypatch):
    client_bytes, usages, _sent = await _route_chat(monkeypatch, client_asks_usage=True)
    (usage,) = usages
    assert (usage.input_tokens, usage.output_tokens) == (12, 6)
    assert _CHAT_USAGE in client_bytes, "클라이언트가 요청한 usage 청크를 지웠다"
