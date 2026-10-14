# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""성공 회계는 클라이언트가 종결 프레임에서 끊어도 끝까지 돌아야 한다.

이 파일이 고정하는 결함
-----------------------
세 SSE 제너레이터는 usage 가 종결 프레임에만 실리므로 마지막 ``yield`` **뒤에** 회계를 한다.
chunked 응답의 종료 청크는 제너레이터가 리턴해야 나가므로, 종결 이벤트를 읽자마자 닫는
클라이언트(Codex CLI — 매 턴)의 끊김은 회계 도중에 도착하고, Starlette 는 응답 태스크를 취소한다.
``CancelledError`` 는 ``BaseException`` 이라 ``cost_recorder.finalize`` 의 ``except Exception``
을 통과한다 — Redis 차감은 남고 원장 행은 없고 로그도 없다(실 Redis 실측은
``tests/regression/test_critical_stream_disconnect_ledger_gap.py``).

단정이 이렇게 생긴 이유
-----------------------
"on_usage 가 불렸다" 만 보는 테스트는 **고치기 전 코드에서도 통과한다** — 콜백은 진입하고,
끝나지 못할 뿐이다. 그래서 각 테스트는 콜백 안에서 멈춰 세운 뒤 정확히 그 지점에서 소비자를
취소하고, 콜백이 **끝까지 돌았는지**를 단정한다. 빠져 있던 성질이 바로 그것이다.

표면: 세 SSE 제너레이터, ``/model/*`` 패스스루(``routers/bedrock.py``), 웹서치 스티처 두 개와
그 본문 로그 래퍼. 일부는 Starlette 와 같은 anyio 취소 스코프로 돌린다 — 평범한
``task.cancel()`` 은 한 번만 던져서 ``finally`` 안의 await 를 다시 자르지 못하므로, 그 경로의
운영 결함은 anyio 스코프에서만 재현된다.
"""

from __future__ import annotations

import asyncio
import json
import time

import anyio
import pytest

from app.schemas.domain import TokenUsage
from app.services import web_search_loop as wsl
from app.services.body_log_records import wrap_stream_for_body_log
from app.services.streaming import (
    bedrock_anthropic_sse_stream,
    openai_sse_stream,
    responses_sse_stream,
)

_TIMEOUTS = {"idle_timeout": 5.0, "drain_timeout": 5.0}


class _FakeRequest:
    async def is_disconnected(self) -> bool:  # pragma: no cover - 호출되면 안 된다
        raise AssertionError("is_disconnected() 는 스트리밍 응답에서 신뢰할 수 없다")


def _aiter(items):
    async def _gen():
        for item in items:
            yield item

    return _gen()


class _Accounting:
    """회계 도중에 멈춰 서는 ``on_usage`` — 테스트가 정확히 그 지점에서 취소할 수 있게.

    ``entered`` 는 첫 Redis 차감 자리에서, ``finished`` 는 원장 XADD 자리까지 살아남았을 때만 선다.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls: list[TokenUsage] = []

    async def __call__(self, usage: TokenUsage, first_token_time: float | None) -> None:
        self.entered.set()
        # finalize 의 Redis 왕복(차감 EVAL 2~3회 + 정산) 자리. 테스트가 여기 세워 두고 취소한다.
        await self.release.wait()
        self.calls.append(usage)
        self.finished.set()


async def _until(pred, interval: float = 0.005) -> None:
    while not pred():
        await asyncio.sleep(interval)


async def _cancel_during_accounting(gen, acct: _Accounting) -> None:
    """``gen`` 을 소비하다 회계가 시작되는 순간 소비자를 취소하고, 회계가 끝나는지 단정한다."""

    async def consume():
        async for _ in gen:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(acct.entered.wait(), timeout=2.0)

    # 클라이언트가 바로 여기서 사라진다 — 종결 프레임과 원장 사이.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not acct.finished.is_set(), "이 시점에는 회계가 아직 멈춰 있어야 한다"
    acct.release.set()
    await asyncio.wait_for(acct.finished.wait(), timeout=2.0)
    assert len(acct.calls) == 1, "회계는 정확히 한 번 — 두 번이면 이중 청구다"


# ── Codex 경로(/v1/responses) — 유실이 측정된 곳 ──────────────────────────────


def _responses_events() -> list[bytes]:
    return [
        json.dumps({"type": "response.output_text.delta", "delta": "hi"}).encode(),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}
                },
            }
        ).encode(),
    ]


@pytest.mark.asyncio
async def test_responses_accounting_survives_disconnect_on_terminal_frame():
    acct = _Accounting()
    gen = responses_sse_stream(
        _FakeRequest(), _aiter(_responses_events()), on_usage=acct, **_TIMEOUTS
    )
    await _cancel_during_accounting(gen, acct)
    usage = acct.calls[0]
    # 0 으로 채운 자리표시가 아니라 실제 usage 가 넘어갔다.
    assert usage.output_tokens == 50
    assert usage.input_tokens + usage.cache_read_input_tokens == 1000


@pytest.mark.asyncio
async def test_responses_body_log_also_survives_disconnect():
    """usage 와 본문 로그는 한 태스크다 — 앞의 것만 보호하면 뒤의 것은 도달하지 못한다.

    ``shield`` 는 호출자 쪽에 ``CancelledError`` 를 다시 던지므로, shield 뒤의 두 번째 ``await``
    는 실행되지 않는다.
    """
    acct = _Accounting()
    completed: list[str] = []

    async def on_complete(text: str, status: str) -> None:
        completed.append(status)

    gen = responses_sse_stream(
        _FakeRequest(),
        _aiter(_responses_events()),
        on_usage=acct,
        on_complete=on_complete,
        **_TIMEOUTS,
    )
    await _cancel_during_accounting(gen, acct)
    await asyncio.wait_for(_until(lambda: completed == ["success"]), timeout=2.0)


# ── Claude 경로(/v1/messages) — 빈도는 낮고 결함은 같다 ─────────────────────────


def _bedrock_events() -> list[bytes]:
    return [
        b'{"type":"message_start","message":{"usage":{"input_tokens":500,'
        b'"cache_read_input_tokens":200}}}',
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}',
        b'{"type":"message_delta","usage":{"output_tokens":42}}',
    ]


@pytest.mark.asyncio
async def test_bedrock_accounting_survives_disconnect_on_terminal_frame():
    acct = _Accounting()
    gen = bedrock_anthropic_sse_stream(
        _FakeRequest(), _aiter(_bedrock_events()), on_usage=acct, **_TIMEOUTS
    )
    await _cancel_during_accounting(gen, acct)
    usage = acct.calls[0]
    assert usage.output_tokens == 42
    assert usage.cache_read_input_tokens == 200


# ── OpenAI chat-completions 경로 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_accounting_survives_disconnect_on_terminal_frame():
    acct = _Accounting()
    completed: list[str] = []

    async def on_complete(text: str, status: str) -> None:
        completed.append(status)

    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,'
        b'"completion_tokens":9,"total_tokens":16}}\n\n',
    ]
    gen = openai_sse_stream(
        _FakeRequest(), _aiter(chunks), on_usage=acct, on_complete=on_complete, **_TIMEOUTS
    )
    await _cancel_during_accounting(gen, acct)
    assert acct.calls[0].output_tokens == 9
    # 이 경로도 본문 로그 훅이 있다 — 같은 태스크에 실려 함께 살아남아야 한다.
    await asyncio.wait_for(_until(lambda: completed == ["success"]), timeout=2.0)


# ── timeout 경로: 회계가 오류 프레임 yield **앞**, try 안에 있다 ─────────────────


@pytest.mark.asyncio
async def test_timeout_path_survives_cancel_and_the_drain_does_not_bill_twice():
    """이 트리에서는 timeout/예외 경로가 회계를 오류 프레임 yield **앞**에서 확정한다.

    timeout 경로는 try 안이라, 그 회계 도중의 취소는 취소 핸들러로 빠져 drain 을 띄운다.
    그래서 여기가 이중 청구 경합이 실제로 성립하는 곳이다 — 클레임이 동기로 먼저 잡혀 있어야
    drain 이 같은 요청을 또 청구하지 않는다. 고치기 전에는 회계가 잘려 ``finished`` 가 서지 않았다.
    """
    acct = _Accounting()
    completed: list[str] = []

    async def on_complete(text: str, status: str) -> None:
        completed.append(status)

    async def stalls_after_first_event():
        yield b'{"type":"message_start","message":{"usage":{"input_tokens":10}}}'
        yield b'{"type":"message_delta","usage":{"output_tokens":5}}'
        await asyncio.sleep(30)  # idle timeout 초과
        yield b'{"type":"never"}'  # pragma: no cover

    gen = bedrock_anthropic_sse_stream(
        _FakeRequest(),
        stalls_after_first_event(),
        on_usage=acct,
        on_complete=on_complete,
        idle_timeout=0.05,
        drain_timeout=1.0,
    )
    await _cancel_during_accounting(gen, acct)
    assert (acct.calls[0].input_tokens, acct.calls[0].output_tokens) == (10, 5)
    await asyncio.wait_for(_until(lambda: completed == ["partial"]), timeout=2.0)
    await asyncio.sleep(0.05)  # drain 이 늦게라도 두 번째로 청구하는지 볼 여유
    assert len(acct.calls) == 1, f"이중 청구: {len(acct.calls)}회"
    assert completed == ["partial"], f"본문 로그 중복: {completed}"


# ── 스트림 도중 끊김(ESC) — 원래 맞던 경로는 그대로여야 한다 ───────────────────


@pytest.mark.asyncio
async def test_midstream_disconnect_still_accounts_via_drain():
    """원래 옳았던 경로의 회귀 가드.

    생성 도중 ESC 는 취소 핸들러의 drain 이 처리한다 — 나머지 턴을 읽어 실제 usage 를 기록한다.
    보호 작업이 이를 건드리면 안 되고, drain 과 꼬리가 둘 다 발화해서도 안 된다.

    ⚠️ 끊김을 ``yield`` 지점(버려진 제너레이터의 aclose → GeneratorExit)에서 넣는다. 상류를
       읽는 도중의 취소는 ``wait_for`` 가 상류의 ``__anext__`` 까지 함께 취소해 상류
       제너레이터를 닫으므로, 이 트리의 drain 은 그 진행 중 읽기를 이어 받지 못한다(별도 펌프
       태스크가 없다) — 기존 한계이고 이 변경의 범위가 아니다.
    """
    calls: list[int] = []

    async def on_usage(usage: TokenUsage, first_token_time: float | None) -> None:
        calls.append(usage.output_tokens)

    async def upstream():
        yield json.dumps({"type": "response.output_text.delta", "delta": "hi"}).encode()
        yield json.dumps(
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 3, "output_tokens": 11, "total_tokens": 14}},
            }
        ).encode()

    gen = responses_sse_stream(_FakeRequest(), upstream(), on_usage=on_usage, **_TIMEOUTS)
    await gen.__anext__()  # 프레임 하나가 클라이언트에 갔다
    await gen.aclose()  # 클라이언트가 사라졌다 — 버려진 제너레이터가 yield 에서 닫힌다

    await asyncio.wait_for(_until(lambda: calls), timeout=2.0)
    await asyncio.sleep(0.05)
    # drain 이 종결 프레임을 읽었으므로 **실제** usage 가 기록됐다 — 정확히 한 번.
    assert calls == [11], calls


# ── /model/* 패스스루(routers/bedrock.py) — 회계가 finally 안에 있다 ───────────


class _ParkedRecorder:
    """``cost_recorder.finalize`` 자리 — 차감과 XADD 사이에서 멈춰 선다.

    ``rtt`` 를 주면 멈추는 대신 그만큼 잔다(Redis 왕복 = 취소된 스코프 안의 체크포인트).
    """

    def __init__(self, *, rtt: float | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls: list[TokenUsage] = []
        self._rtt = rtt

    async def finalize(self, redis, auth_context, model_config, usage, *args, **kwargs):
        self.entered.set()
        if self._rtt is None:
            await self.release.wait()
        else:
            await asyncio.sleep(self._rtt)
        self.calls.append(usage)
        self.finished.set()


async def _passthrough_stream(monkeypatch, recorder: _ParkedRecorder, upstream):
    """실제 ``_handle_bedrock`` 을 타서 ``stream_with_cost`` 제너레이터를 받는다.

    라우트 레벨 픽스처가 없으므로 해석·인가·예약 게이트만 대역으로 바꾼다 — 검증 대상인
    ``stream_with_cost`` 의 finally 는 실제 코드다.
    """
    from decimal import Decimal
    from types import SimpleNamespace

    from app.routers import bedrock
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
    from app.services import rate_limit_enforcement

    model = ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )

    async def _resolve(*_a, **_k):
        return model

    async def _no_limit(**_k):
        return None

    monkeypatch.setattr(bedrock._router_service, "resolve_bedrock_model", _resolve)
    monkeypatch.setattr(bedrock._router_service, "check_key_scope", lambda *a, **k: None)
    monkeypatch.setattr(bedrock._router_service, "check_client_model_scope", lambda *a, **k: None)
    monkeypatch.setattr(rate_limit_enforcement, "enforce_rate_limits", _no_limit)

    class _Adapter:
        async def invoke_stream(self, body, model_id, path_suffix=None):
            return 200, upstream, {"Content-Type": "application/octet-stream"}, "rid"

    class _Request:
        def __init__(self) -> None:
            self.scope = {
                "state": {
                    "auth_context": AuthContext(
                        user_id="u-1",
                        team_id="t-1",
                        dept_id="d-1",
                        roles=[Role.USER],
                        auth_type=AuthType.VIRTUAL_KEY,
                    ),
                    "_redis": object(),
                    "request_id": "req-1",
                }
            }
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    provider_registry=SimpleNamespace(get=lambda _p: _Adapter()),
                    cost_recorder=recorder,
                    metrics=None,
                )
            )

        async def body(self) -> bytes:
            return b'{"max_tokens": 16}'

    resp = await bedrock._handle_bedrock(
        _Request(), "claude-sonnet-4-6", "invoke-with-response-stream", stream=True
    )
    return resp.body_iterator


_PASSTHROUGH_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
    b'data: {"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}\n\n',
]


@pytest.mark.asyncio
async def test_bedrock_passthrough_finalize_survives_disconnect_on_terminal_chunk(monkeypatch):
    recorder = _ParkedRecorder()
    gen = await _passthrough_stream(monkeypatch, recorder, _aiter(_PASSTHROUGH_CHUNKS))

    async def consume():
        async for _ in gen:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(recorder.entered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not recorder.finished.is_set()
    recorder.release.set()
    await asyncio.wait_for(recorder.finished.wait(), timeout=2.0)
    assert [(u.input_tokens, u.output_tokens) for u in recorder.calls] == [(5, 7)]


@pytest.mark.asyncio
async def test_bedrock_passthrough_midstream_cancel_under_anyio_still_finalizes(monkeypatch):
    """Starlette 와 같은 anyio 스코프에서 생성 도중 끊는다 — finally 의 await 가 다시 취소된다.

    평범한 ``task.cancel()`` 로는 재현되지 않는다(한 번만 던지므로 finally 가 그대로 끝난다).
    """
    recorder = _ParkedRecorder(rtt=0.01)
    stalled = asyncio.Event()

    async def stalls():
        yield _PASSTHROUGH_CHUNKS[1]  # usage 는 이미 받았다
        stalled.set()
        await asyncio.sleep(30)
        yield b""  # pragma: no cover

    gen = await _passthrough_stream(monkeypatch, recorder, stalls())

    async def consume():
        async for _ in gen:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await stalled.wait()
        tg.cancel_scope.cancel()  # = StreamingResponse 가 http.disconnect 에 하는 일

    await asyncio.wait_for(recorder.finished.wait(), timeout=2.0)
    assert [(u.input_tokens, u.output_tokens) for u in recorder.calls] == [(5, 7)]


# ── 웹서치 스티처(services/web_search_loop.py) — usage 를 finally 에서 쏜다 ─────
#
# 웹서치는 라우팅 프로파일별 opt-in 이라 꺼진 환경에서는 증상이 없다. 켜는 순간 같은 유실이
# 시작되고, 잘리면 여러 턴의 토큰과 검색 횟수(web_search_count)가 한꺼번에 사라진다. 본문 로그는
# 스티처가 아니라 바깥 래퍼(body_log_records.wrap_stream_for_body_log)가 쏘므로 따로 견뎌야 한다.


def _anthropic_final_turn() -> list[bytes]:
    """검색 없이 바로 답하는 한 턴 — 성공(terminal) 경로로 빠진다."""
    return [
        json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1000}}}
        ).encode(),
        json.dumps(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
        ).encode(),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer"},
            }
        ).encode(),
        json.dumps({"type": "content_block_stop", "index": 0}).encode(),
        json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 42},
            }
        ).encode(),
        json.dumps({"type": "message_stop"}).encode(),
    ]


def _responses_final_turn() -> list[bytes]:
    return [
        json.dumps({"type": "response.created", "response": {"id": "resp_1"}}).encode(),
        json.dumps(
            {"type": "response.output_text.delta", "output_index": 0, "delta": "answer"}
        ).encode(),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "output": [],
                    "usage": {"input_tokens": 900, "output_tokens": 30, "total_tokens": 930},
                },
            }
        ).encode(),
    ]


class _WsAccounting:
    """회계 도중에 멈춰 서는 1-인자 ``on_usage`` (웹서치 루프의 계약)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls: list[tuple[int, int]] = []

    async def on_usage(self, usage) -> None:
        self.entered.set()
        await self.release.wait()  # finalize 의 Redis 왕복 자리
        self.calls.append((usage.input_tokens, usage.output_tokens))
        self.finished.set()


def _stitcher(dialect: str, upstream, on_usage):
    async def invoke_stream(body):
        return 200, upstream, {}, None

    stitch = wsl._anthropic_stream if dialect == "anthropic" else wsl._responses_stream
    base = (
        {"messages": [{"role": "user", "content": "hi"}]}
        if dialect == "anthropic"
        else {"input": "hi"}
    )
    return stitch(
        invoke_stream=invoke_stream,
        base_body=base,
        mcp_client=None,
        request=_FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
    )


async def _cancel_during_ws_accounting(gen, acct: _WsAccounting) -> None:
    async def consume():
        async for _ in gen:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(acct.entered.wait(), timeout=2.0)
    task.cancel()  # 클라이언트가 종결 프레임만 받고 사라지는 순간
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not acct.finished.is_set(), "회계가 아직 멈춰 있어야 이 시점이 취약 구간이다"
    acct.release.set()
    await asyncio.wait_for(acct.finished.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_anthropic_stitcher_accounting_survives_disconnect():
    acct = _WsAccounting()
    gen = _stitcher("anthropic", _aiter(_anthropic_final_turn()), acct.on_usage)
    await _cancel_during_ws_accounting(gen, acct)
    assert acct.calls == [(1000, 42)], acct.calls  # 실제 usage 로, 정확히 한 번


@pytest.mark.asyncio
async def test_responses_stitcher_accounting_survives_disconnect():
    """Codex 경로(웹서치 켠 /v1/responses) — 유실이 측정된 클라이언트가 쓰는 스티처."""
    acct = _WsAccounting()
    gen = _stitcher("responses", _aiter(_responses_final_turn()), acct.on_usage)
    await _cancel_during_ws_accounting(gen, acct)
    assert acct.calls == [(900, 30)], acct.calls


@pytest.mark.asyncio
async def test_web_search_body_log_survives_disconnect():
    """usage 를 보호해도 ``CancelledError`` 는 래퍼까지 올라온다 — 래퍼가 견뎌야 한다.

    종결 프레임이 이미 래퍼에 누적됐으므로 본문 로그는 success 로 남아야 한다.
    """
    acct = _WsAccounting()
    logged: list[str] = []

    async def on_complete(text: str, status: str) -> None:
        logged.append(status)

    gen = wrap_stream_for_body_log(
        _stitcher("anthropic", _aiter(_anthropic_final_turn()), acct.on_usage),
        dialect="anthropic",
        on_complete=on_complete,
    )
    await _cancel_during_ws_accounting(gen, acct)
    await asyncio.wait_for(_until(lambda: logged), timeout=2.0)
    await asyncio.sleep(0.02)
    assert logged == ["success"], logged


@pytest.mark.asyncio
async def test_web_search_midstream_cancel_under_anyio_still_records_what_accrued():
    """Starlette 와 같은 anyio 스코프에서 생성 도중 끊는다 — finally 의 첫 체크포인트가 다시
    잘리므로, 보호가 없으면 이미 쓴 입력 토큰이 원장에 남지 않는다."""
    recorded: list[int] = []
    stalled = asyncio.Event()

    async def on_usage(usage) -> None:
        await asyncio.sleep(0.01)  # Redis 왕복 — 취소된 스코프 안의 체크포인트
        recorded.append(usage.input_tokens)

    async def stalls_mid_turn():
        yield json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1000}}}
        ).encode()
        stalled.set()
        await asyncio.sleep(30)
        yield b"{}"  # pragma: no cover

    gen = _stitcher("anthropic", stalls_mid_turn(), on_usage)

    async def consume():
        async for _ in gen:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await stalled.wait()
        tg.cancel_scope.cancel()  # = StreamingResponse 가 http.disconnect 에 하는 일

    await asyncio.wait_for(_until(lambda: recorded), timeout=2.0)
    assert recorded == [1000], recorded


# ── 잘린 스트림의 프롬프트 — usage 이벤트 없이 끝나면 입력이 0 으로 청구됐다 ─────────
#
# Responses / chat 방언은 usage 를 **종결 이벤트에만** 싣는다. 그 전에 끊기면(클라이언트 ESC,
# SIGTERM 셧다운, 상류 절단) 출력은 KI-08 역산으로 남지만 입력은 0 이었다 — AWS 는 이미 처리한
# 프롬프트를 청구하는데 우리 원장에는 프롬프트가 없다. 추정은 **하한**으로 청구한다: 모델이 캐시
# 읽기 단가를 매기면 그 버킷(어떤 프롬프트 토큰도 그보다 싸게 청구되지 않는다), 아니면 입력.


class _CharTokenizer:
    """결정적 토크나이저 대역 — 글자 수를 토큰 수로 본다."""

    async def estimate_output_tokens(self, text, *, provider, model_id):
        return len(text)


def _gpt_model(*, cache_read: str = "0.00022"):
    from decimal import Decimal

    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="us.openai.gpt-5.6-terra",
        alias="gpt-5.6-terra",
        provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
        api_format=ApiFormat.OPENAI_RESPONSES,
        endpoint="https://runtime.example.invalid/openai",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.0022"),
            output_per_1k=Decimal("0.0132"),
            cache_read_per_1k=Decimal(cache_read),
        ),
        status=ModelStatus.ACTIVE,
    )


def test_prompt_text_keeps_text_and_drops_base64_media():
    from app.services.tokenizer import prompt_text

    body = {
        "model": "gpt-5.6-terra",
        "max_output_tokens": 999,
        "instructions": "be brief",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe"},
                    {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 5000},
                    {"type": "input_file", "file_data": "data:application/pdf;base64,QUJD"},
                ],
            }
        ],
        "tools": [{"type": "function", "name": "lookup", "description": "find a thing"}],
    }
    text = prompt_text(body)
    for kept in ("be brief", "describe", "lookup", "find a thing"):
        assert kept in text
    assert "AAAA" not in text and "QUJD" not in text, "base64 미디어가 토크나이저에 들어간다"
    assert "gpt-5.6-terra" not in text and "999" not in text, "프롬프트가 아닌 최상위 필드"


@pytest.mark.asyncio
async def test_prompt_estimate_bills_the_lower_bound_bucket():
    from app.services.tokenizer import estimate_prompt_usage, prompt_text

    body = {"messages": [{"role": "user", "content": "hello there"}]}
    n = len(prompt_text(body))
    cached = await estimate_prompt_usage(_CharTokenizer(), body, _gpt_model())
    assert (cached.cache_read_input_tokens, cached.input_tokens) == (n, 0)
    uncached = await estimate_prompt_usage(_CharTokenizer(), body, _gpt_model(cache_read="0"))
    assert (uncached.cache_read_input_tokens, uncached.input_tokens) == (0, n)
    assert await estimate_prompt_usage(None, body, _gpt_model()) is None


async def _stalling(frames: list[bytes]):
    for f in frames:
        yield f
    await asyncio.sleep(30)  # 상류가 더 생성 중 — 여기서 끊긴다
    yield b""  # pragma: no cover


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["responses", "chat"])
async def test_helper_uses_the_prompt_hook_only_when_no_usage_event_arrived(dialect):
    """usage 이벤트 없이 끊겼고 생성된 텍스트가 있을 때만 — 실제 usage 는 절대 덮지 않는다."""
    from app.schemas.domain import TokenUsage

    seen: list[TokenUsage] = []

    async def on_usage(usage, first_token_time):
        seen.append(usage)

    async def prompt_hook():
        return TokenUsage(cache_read_input_tokens=777)

    async def tok(text):
        return len(text)

    if dialect == "responses":
        frames = [_responses_events()[0]]  # delta 만 — 종결 이벤트 없음
        helper = responses_sse_stream
    else:
        frames = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n']
        helper = openai_sse_stream
    gen = helper(
        _FakeRequest(), _stalling(frames), on_usage=on_usage, tokenizer_hook=tok,
        prompt_usage_hook=prompt_hook, **_TIMEOUTS,
    )
    consumer = asyncio.create_task(_consume(gen))
    await asyncio.sleep(0.05)
    consumer.cancel()  # 상류를 읽는 도중 끊긴다 → drain 이 받을 것이 없다
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.wait_for(_until(lambda: seen), timeout=2.0)
    (usage,) = seen
    assert usage.cache_read_input_tokens == 777 and usage.estimated
    assert usage.output_tokens == len("hi")
    assert usage.total_tokens == 777 + len("hi")

    # 실제 usage 가 왔으면 추정은 쓰지 않는다.
    seen.clear()
    full = (
        _responses_events()
        if dialect == "responses"
        else [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,'
            b'"completion_tokens":9,"total_tokens":16}}\n\n',
        ]
    )
    async for _ in helper(
        _FakeRequest(), _aiter(full), on_usage=on_usage, tokenizer_hook=tok,
        prompt_usage_hook=prompt_hook, **_TIMEOUTS,
    ):
        pass
    await asyncio.wait_for(_until(lambda: seen), timeout=2.0)
    assert seen[0].cache_read_input_tokens != 777 and not seen[0].estimated


async def _consume(gen):
    async for _ in gen:
        pass


class _CapturingRecorder:
    def __init__(self) -> None:
        self.usages: list = []

    async def finalize(self, redis, auth_context, model_config, usage, *args, **kwargs):
        self.usages.append(usage)


async def _cut_route_stream(monkeypatch, route: str):
    """실제 라우터(_handle_openai / _handle_responses)로 200 스트림을 받다가 상류 도중 끊는다."""
    from types import SimpleNamespace

    from app.routers import openai_compat
    from app.schemas.domain import AuthContext, AuthType, Role
    from app.schemas.routing import RoutingProfileSchema

    cfg = _gpt_model()

    async def _resolve(redis, db, alias):
        return cfg

    async def _no_limits(**_k):
        return None

    monkeypatch.setattr(openai_compat._router_service, "resolve_openai_model", _resolve)
    monkeypatch.setattr(openai_compat._router_service, "resolve_codex_model", _resolve)
    monkeypatch.setattr("app.services.rate_limit_enforcement.enforce_rate_limits", _no_limits)

    if route == "responses":
        frames = [
            json.dumps({"type": "response.created", "response": {"id": "r1"}}).encode(),
            json.dumps({"type": "response.output_text.delta", "delta": "partial"}).encode(),
        ]
        body = {"model": cfg.alias, "stream": True, "input": "summarize this repository"}
    else:
        frames = [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n']
        body = {
            "model": cfg.alias,
            "stream": True,
            "messages": [{"role": "user", "content": "summarize this repository"}],
        }

    async def _invoke_stream(*_a, **_k):
        return 200, _stalling(frames), {}, "rid-1"

    recorder = _CapturingRecorder()
    profile = RoutingProfileSchema(
        client="codex", backend="bedrock_openai", account_role_arn=None,
        region="us-east-2", default_model=cfg.alias, external_id=None,
    )

    async def _load(*_a, **_k):
        return profile

    app_state = SimpleNamespace(
        provider_registry=SimpleNamespace(
            get=lambda _p: SimpleNamespace(invoke_stream=_invoke_stream)
        ),
        cost_recorder=recorder,
        tokenizer=_CharTokenizer(),
        body_logger=None,
        metrics=None,
        agentcore_mcp_client=None,
        routing_profile_loader=SimpleNamespace(load=_load),
    )
    auth = AuthContext(
        user_id="u-1", team_id="t-1", dept_id="d-1", roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state = {"auth_context": auth, "client": "codex", "_redis": None,
             "_session_factory": None, "request_id": f"req-cut-{route}"}

    class _Req:
        def __init__(self) -> None:
            from starlette.datastructures import Headers

            self.scope = {"state": state}
            self.app = SimpleNamespace(state=app_state)
            self.headers = Headers()

        async def body(self) -> bytes:
            return json.dumps(body).encode()

        async def is_disconnected(self) -> bool:  # pragma: no cover
            return False

    if route == "responses":
        resp = await openai_compat._handle_responses(_Req())
    else:
        resp = await openai_compat._handle_openai(_Req(), "/v1/chat/completions")
    consumer = asyncio.create_task(_consume(resp.body_iterator))
    await asyncio.sleep(0.05)
    consumer.cancel()  # 클라이언트 ESC / SIGTERM 셧다운 — 상류를 읽는 도중
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.wait_for(_until(lambda: recorder.usages), timeout=2.0)
    return recorder.usages, body


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "chat"])
async def test_cut_stream_bills_the_prompt_instead_of_zero(monkeypatch, route):
    """⚠️ 고치기 전: 출력만 역산되고 입력은 0 — 프롬프트가 원장에 없었다."""
    usages, body = await _cut_route_stream(monkeypatch, route)
    (usage,) = usages
    prompt = usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
    assert prompt > 0, f"프롬프트가 청구되지 않았다(입력 0): {usage}"

    from app.services.tokenizer import prompt_text

    assert prompt == len(prompt_text(body))
    assert usage.cache_read_input_tokens == prompt, "캐시 단가가 있으면 하한 버킷으로"
    assert usage.output_tokens == len("partial") and usage.estimated
