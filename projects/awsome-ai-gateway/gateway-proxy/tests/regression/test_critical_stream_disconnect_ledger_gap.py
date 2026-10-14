# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""종결 프레임을 읽자마자 끊는 클라이언트 — Redis 예산 카운터와 원장(cost:stream)이 어긋나는가.

결함
----
스트리밍 회계(``cost_recorder.finalize``)는 마지막 SSE 프레임을 yield **한 뒤에** 돈다(usage 가
종결 프레임에만 실리므로). 그런데 chunked 응답의 종료 청크는 제너레이터가 **리턴해야** 나간다.
그래서 종결 이벤트를 읽자마자 닫는 클라이언트의 끊김은 정확히 회계 도중에 도착한다 — Codex CLI 는
``response.completed`` 를 읽는 순간 리턴하고 스트림을 버린다
(``codex-rs/codex-api/src/sse/responses.rs``: ``if is_completed { return; }``), 매 턴.

uvicorn 은 ASGI spec 2.3 을 보고하므로 Starlette ``StreamingResponse`` 는 task-group 경로를 타고,
끊김에 응답 태스크를 **취소**한다. ``CancelledError`` 는 ``BaseException`` 이라 finalize 의
``except Exception`` 을 전부 통과한다 — 이미 보낸 예산 EVAL 은 Redis 에 남고, 원장 XADD 는
실행되지 않고, 로그는 없다. 증상은 "카운터는 맞고 원장만 짧다" 이고 대사로만 드러난다.

두 겹으로 증명한다(둘 다 실 uvicorn + 실 TCP 소켓)
--------------------------------------------------
1. ASGI 취소 경로 — 원장 자리에 계측용 대역(``_Ledger``)을 둔다. Redis 없이 기본 스위트에서 돈다.
2. 실 대사 — **실제** ``CostRecorder.finalize`` 를 **실 Redis**(Lua budget_deduct + XADD
   cost:stream)에 대고, 운영 대사 식을 그대로 단정한다: 사용자 예산 카운터 == 원장 cost_usd 합계.
   ``REDIS_PROOF_URL`` 이 있을 때만 돈다.

⚠️ 실측(고치기 전, 2 번): Codex 방식 6턴 → Redis 카운터 $0.022386 vs 원장 $0, 원장 행 0/6.
   EOF 까지 읽는 대조군은 같은 하네스에서 일치했다.

⚠️ 2 번의 합성은 RTT 주입 하나뿐이다. 루프백 Redis 왕복은 수백 µs 라 그대로 두면 끊김이 회계 전에
   올지 후에 올지 매번 달라진다. 그래서 실 Redis 명령을 **먼저 실행한 뒤** 응답 지연만 덧붙인다
   (``_RttRedis``) — 취소가 그 지연 안에 떨어지면 "EVAL 은 반영됐고 호출자만 잘렸다" 가 되는데,
   그게 클러스터에서 실제로 일어나는 일이다. 명령 자체를 흉내 내는 것은 없다.

버전은 측정한 스택(starlette 1.0.0 / uvicorn 0.44.0)으로 고정해 단정한다 — 취소 경로를 바꾸는
상향이 원장 공백을 조용히 다시 여는 대신 여기서 실패하게.

실행(2 번 포함):
    REDIS_PROOF_URL=redis://127.0.0.1:56382/0 \\
      pytest tests/regression/test_critical_stream_disconnect_ledger_gap.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import starlette
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.periods import current_kst_period
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
from app.services import bg_tasks
from app.services.cost_recorder import COST_STREAM_KEY, CostRecorder
from app.services.lua_loader import LuaScriptLoader
from app.services.streaming import responses_sse_stream

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 결함의 본질이 'EVAL 은 반영되고 XADD 만 "
        "빠진다' 라서, 두 쓰기를 같은 실 Redis 에서 대사해야만 드러난다."
    ),
)


# ── 공용: 실 uvicorn, Codex 방식 클라이언트 ──────────────────────────────────


def _events() -> list[bytes]:
    return [
        json.dumps({"type": "response.created", "response": {"id": "resp_1"}}).encode(),
        json.dumps({"type": "response.output_text.delta", "delta": "hello"}).encode(),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "usage": {"input_tokens": 1234, "output_tokens": 77, "total_tokens": 1311},
                },
            }
        ).encode(),
    ]


def _build_app(make_on_usage) -> Starlette:
    """요청마다 ``make_on_usage()`` 로 콜백을 받아 실제 ``responses_sse_stream`` 을 돌린다."""

    async def responses(request):
        async def upstream():
            for ev in _events():
                yield ev

        return StreamingResponse(
            responses_sse_stream(
                request, upstream(), on_usage=make_on_usage(), idle_timeout=5.0, drain_timeout=5.0
            ),
            media_type="text/event-stream",
        )

    return Starlette(routes=[Route("/v1/responses", responses, methods=["GET"])])


class _Server:
    """임의 포트의 실 uvicorn. 테스트마다 띄우고 내린다."""

    def __init__(self, app) -> None:
        # lifespan="off": 요청 경로만 본다(앱 기동은 이 결함과 무관).
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
        )
        self._task: asyncio.Task | None = None
        self.port = 0

    async def __aenter__(self) -> _Server:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(500):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - 서버가 뜨지 않음
            raise RuntimeError("uvicorn did not start")
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=10)


async def _codex_turn(port: int, *, close_delay: float = 0.0, hard_reset: bool = False) -> bytes:
    """``response.completed`` 까지만 읽고 바로 닫는다 — Codex CLI 의 종료 방식."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            b"GET /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Accept: text/event-stream\r\n\r\n"
        )
        await writer.drain()
        buf = b""
        while b"response.completed" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:  # pragma: no cover - 서버가 먼저 닫음
                break
            buf += chunk
        if close_delay:
            await asyncio.sleep(close_delay)
        if hard_reset:
            # SO_LINGER 0 → FIN 대신 RST. 가장 거친 끊김이고, 우아한 종료가 우연히 회계를
            # 마무리해 줄 여지가 가장 적다.
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        return buf
    finally:
        writer.close()
        with contextlib.suppress(Exception):  # 상대가 이미 사라졌을 수 있다
            await writer.wait_closed()


async def _eof_turn(port: int) -> bytes:
    """대조군: 종료 청크까지 끝까지 읽는 클라이언트(Claude Code 방식)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            b"GET /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Accept: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        return await asyncio.wait_for(reader.read(-1), timeout=10)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _until(pred, timeout: float = 5.0, interval: float = 0.01) -> None:
    async def _poll():
        while not pred():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


# ── 1. ASGI 취소 경로 — 계측용 원장 대역 ─────────────────────────────────────

#: finalize 의 Redis 왕복 자리. 일부러 넉넉하다: 검증 대상 창은 클러스터 안에서 ~10ms 인데, 이
#: 값이 클라이언트 close 지연보다 작으면 끊김 전에 요청이 끝나 테스트가 공허하게 통과한다.
_ACCOUNTING_DELAY = 0.25


class _Ledger:
    """``deducted`` 는 예산 EVAL 자리, ``recorded`` 는 원장 XADD 자리에서만 붙는다."""

    def __init__(self) -> None:
        self.deducted: list[int] = []
        self.recorded: list[int] = []

    async def on_usage(self, usage, first_token_time) -> None:
        self.deducted.append(usage.output_tokens)
        await asyncio.sleep(_ACCOUNTING_DELAY)
        self.recorded.append(usage.output_tokens)


def test_dependency_versions_match_the_measured_stack():
    """취소 경로는 버전에 민감하다 — 상향 시 여기서 크게 실패한다."""
    assert starlette.__version__ == "1.0.0", (
        f"starlette {starlette.__version__}: 끊김→취소 경로를 다시 검증한 뒤 이 값을 갱신할 것"
    )
    assert uvicorn.__version__ == "0.44.0", (
        f"uvicorn {uvicorn.__version__}: ASGI spec_version 과 listen_for_disconnect 경로를 "
        "다시 검증한 뒤 이 값을 갱신할 것"
    )


@pytest.mark.parametrize("close_delay", [0.0, 0.005, 0.02])
async def test_accounting_completes_when_client_closes_on_terminal_frame(close_delay):
    """종결 프레임 후 0/5/20ms 에 닫는다."""
    ledger = _Ledger()
    async with _Server(_build_app(lambda: ledger.on_usage)) as server:
        body = await _codex_turn(server.port, close_delay=close_delay)
        assert b"event: response.completed" in body, body[-400:]
        # 회계는 이제 떼어 낸 태스크에서 돈다 — 요청 소켓은 이미 없다.
        await _until(lambda: ledger.recorded == [77])

    assert ledger.deducted == [77], "예산 차감은 정확히 한 번"
    assert ledger.recorded == [77], "원장 행은 정확히 한 번"


async def test_accounting_completes_on_hard_tcp_reset():
    """FIN 이 아니라 RST — 우아한 종료가 성공으로 오인될 여지가 없다."""
    ledger = _Ledger()
    async with _Server(_build_app(lambda: ledger.on_usage)) as server:
        await _codex_turn(server.port, hard_reset=True)
        await _until(lambda: ledger.recorded == [77])
    assert ledger.deducted == ledger.recorded == [77]


async def test_well_behaved_client_reading_to_eof_still_accounts_once():
    """대조군: EOF 까지 읽는 클라이언트. 원래 깨진 적 없는 경로 — 그대로 둔다."""
    ledger = _Ledger()
    async with _Server(_build_app(lambda: ledger.on_usage)) as server:
        body = await _eof_turn(server.port)
        await _until(lambda: ledger.recorded == [77])
    assert b"event: response.completed" in body
    assert ledger.deducted == ledger.recorded == [77]


async def test_concurrent_disconnecting_clients_all_land_in_the_ledger():
    """Codex 방식 10턴 동시 — 차감 합계와 원장 합계가 같아야 한다."""
    ledger = _Ledger()
    async with _Server(_build_app(lambda: ledger.on_usage)) as server:
        await asyncio.gather(*(_codex_turn(server.port) for _ in range(10)))
        await _until(lambda: len(ledger.recorded) == 10, timeout=10)

    assert sum(ledger.deducted) == sum(ledger.recorded) == 770
    assert bg_tasks.pending_count() == 0


async def test_sigterm_cuts_in_flight_sse_and_the_cut_turn_is_still_accounted():
    """⚠️ 한계의 특성화 — SIGTERM(uvicorn graceful shutdown)은 진행 중 SSE 를 **즉시** 자른다.

    uvicorn 0.44 의 ``shutdown()`` 은 진행 중 요청의 ``receive()`` 가 ``http.disconnect`` 를
    돌려주게 하고, Starlette 1.0 ``StreamingResponse`` 는 그것을 클라이언트 끊김으로 보고 응답
    태스크를 취소한다. 그래서 남은 terminationGracePeriodSeconds 동안 스트림이 끝나기를
    기다려 주지 않는다 — 스트림이 살아남을 수 있는 창은 preStop sleep 뿐이다(차트의 preStop
    주석 참조). 클라이언트는 종결 이벤트 없는 EOF 를 받는다.

    고칠 수 있는 것은 회계다: 잘린 턴도 drain 이 지금까지 생성된 출력(역산)과 프롬프트(추정,
    prompt_usage_hook)로 기록한다 — 고치기 전에는 프롬프트가 0 이었다.
    """
    from app.schemas.domain import TokenUsage

    seen: list[TokenUsage] = []

    async def on_usage(usage, first_token_time):
        seen.append(usage)

    async def tokenizer_hook(text: str) -> int:
        return len(text)

    async def prompt_hook() -> TokenUsage:
        return TokenUsage(cache_read_input_tokens=1234)

    async def app_route(request):
        async def upstream():  # ~20 초 동안 생성을 계속하는 모델
            for _ in range(200):
                yield json.dumps({"type": "response.output_text.delta", "delta": "x"}).encode()
                await asyncio.sleep(0.1)
            yield _events()[-1]  # pragma: no cover - 셧다운이 먼저 온다

        return StreamingResponse(
            responses_sse_stream(
                request, upstream(), on_usage=on_usage, tokenizer_hook=tokenizer_hook,
                prompt_usage_hook=prompt_hook, idle_timeout=5.0, drain_timeout=30.0,
            ),
            media_type="text/event-stream",
        )

    server = _Server(Starlette(routes=[Route("/v1/responses", app_route, methods=["GET"])]))
    await server.__aenter__()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"GET /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()
    await asyncio.sleep(0.5)  # 몇 개의 델타가 흐른 뒤
    started = time.monotonic()
    await server.__aexit__(None, None, None)  # = SIGTERM → uvicorn graceful shutdown
    body = await asyncio.wait_for(reader.read(-1), timeout=5)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    assert time.monotonic() - started < 5, (
        "셧다운이 스트림을 기다렸다 — 특성이 바뀌었으면 차트 preStop 주석을 갱신할 것"
    )
    assert b"response.output_text.delta" in body
    assert b"response.completed" not in body, "종결 이벤트 없이 잘린다(클라이언트는 EOF)"
    await bg_tasks.drain(timeout=2.0)
    assert len(seen) == 1, f"잘린 턴의 회계가 {len(seen)}회"
    usage = seen[0]
    assert usage.output_tokens > 0 and usage.estimated
    assert usage.cache_read_input_tokens == 1234, "잘린 턴의 프롬프트가 청구되지 않았다"


# ── 2. 실 대사 — 실제 finalize + 실 Redis ────────────────────────────────────

#: 명령 하나의 응답 지연. finalize 는 이 경로에서 EVAL 2회 + XADD 1회라 회계 창이 약 150ms —
#: 루프백 close 가 도착하는 시간(1ms 미만)보다 충분히 길다.
_RTT = 0.05
_TURNS = 6
#: 1234 × 0.0022/1k + 77 × 0.0132/1k = 0.0037312 → 0.003731 (COST_PRECISION)
_PER_TURN = Decimal("0.003731")


class _RttRedis:
    """실 Redis 명령을 그대로 실행하고, **응답 쪽**에만 왕복 지연을 덧붙인다.

    지연을 명령 앞에 두면 취소가 전송 전에 떨어져 "아무것도 안 쓰였다" 가 되므로 실제 장애(차감은
    반영, 원장은 누락)를 재현하지 못한다. 그래서 뒤에 둔다.
    """

    def __init__(self, inner, rtt: float) -> None:
        self._inner = inner
        self._rtt = rtt

    async def eval(self, *args, **kwargs):
        result = await self._inner.eval(*args, **kwargs)
        await asyncio.sleep(self._rtt)
        return result

    async def xadd(self, *args, **kwargs):
        result = await self._inner.xadd(*args, **kwargs)
        await asyncio.sleep(self._rtt)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _terra() -> ModelConfigSchema:
    # 0032 가 시드하는 gpt-5.6-terra(runtime, Geo CRIS short) 단가.
    return ModelConfigSchema(
        provider_model_id="us.openai.gpt-5.6-terra",
        alias="gpt-5.6-terra",
        provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
        api_format=ApiFormat.OPENAI_RESPONSES,
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.0022"),
            output_per_1k=Decimal("0.0132"),
            cache_write_per_1k=Decimal("0.00275"),
            cache_read_per_1k=Decimal("0.00022"),
        ),
        status=ModelStatus.ACTIVE,
    )


def _recorder_app(redis, auth: AuthContext) -> Starlette:
    recorder = CostRecorder()
    model = _terra()

    def make_on_usage():
        request_id = f"req-{uuid.uuid4()}"
        started = time.monotonic()

        async def _record(usage, first_token_time):
            # routers/openai_compat.py 의 _record 와 같은 호출. 예약 상태는 없다 — 정산 단계를
            # 빼고 차감 2회(user/team) + XADD 1회만 남긴다.
            duration_ms = int((time.monotonic() - started) * 1000)
            await recorder.finalize(
                redis, auth, model, usage, request_id, True, duration_ms, ttft_ms=duration_ms
            )

        return _record

    return _build_app(make_on_usage)


async def _reconcile(raw, auth: AuthContext, want_rows: int, timeout: float = 5.0):
    """(사용자 예산 카운터, 원장 행) — 원장이 기대 행 수에 닿거나 timeout 까지 기다린다.

    회계가 떼어 낸 태스크에서 마무리되므로 즉시 읽으면 이르다. 반대로 고치기 전에는 잘린 회계가
    다시 돌지 않으므로 timeout 뒤의 값이 곧 최종값이다.
    """
    counter_key = f"budget:user:{{{auth.user_id}}}:{current_kst_period()}"
    deadline = time.monotonic() + timeout
    while True:
        value = await raw.get(counter_key)
        counter = Decimal(value) if value is not None else Decimal("0")
        rows = [
            json.loads(fields["payload"])
            for _, fields in await raw.xrange(COST_STREAM_KEY, "-", "+")
            if json.loads(fields["payload"]).get("user_id") == auth.user_id
        ]
        if len(rows) >= want_rows or time.monotonic() > deadline:
            return counter, rows
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _real_redis():
    import redis.asyncio as aioredis

    LuaScriptLoader.load_all(_SRC / "redis_scripts")
    raw = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    # ⚠️ 풀을 미리 데운다(동시 턴 수만큼). 운영 풀은 늘 데워져 있다. 차가운 풀이면 끊김이 TCP 접속
    #    수립 중에 떨어져 EVAL 자체가 나가지 않고 "둘 다 0" 이 된다 — 실측으로 그랬다. 그건 이
    #    결함의 운영 모양("카운터만 오른다")이 아니다.
    await asyncio.gather(*(raw.ping() for _ in range(_TURNS)))
    auth = AuthContext(
        user_id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
        dept_id=str(uuid.uuid4()),
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    period = current_kst_period()
    try:
        yield raw, auth
    finally:
        own = [
            entry_id
            for entry_id, fields in await raw.xrange(COST_STREAM_KEY, "-", "+")
            if json.loads(fields["payload"]).get("user_id") == auth.user_id
        ]
        if own:
            await raw.xdel(COST_STREAM_KEY, *own)
        await raw.delete(
            f"budget:user:{{{auth.user_id}}}:{period}",
            f"budget:team:{{{auth.team_id}}}:{period}",
        )
        await raw.aclose()


@_redis_required
async def test_codex_style_disconnect_keeps_counter_and_ledger_equal():
    """⚠️ 핵심 — 대사 식 "Redis 증가분 − 원장 합계 = 0".

    고치기 전에는 카운터만 오르고 원장 행이 0 개였다(EVAL 은 반영, XADD 는 취소).
    """
    async with _real_redis() as (raw, auth):
        async with _Server(_recorder_app(_RttRedis(raw, _RTT), auth)) as server:
            bodies = await asyncio.gather(*(_codex_turn(server.port) for _ in range(_TURNS)))
            assert all(b"event: response.completed" in b for b in bodies)
            counter, rows = await _reconcile(raw, auth, want_rows=_TURNS)

        ledger = sum((Decimal(r["cost_usd"]) for r in rows), Decimal("0"))
        assert len(rows) == _TURNS, (
            f"원장 행 {len(rows)}/{_TURNS} — Redis 카운터 ${counter} vs 원장 합계 ${ledger}. "
            "종결 프레임 직후의 끊김이 finalize 를 차감과 XADD 사이에서 잘랐다."
        )
        assert abs(counter - ledger) < Decimal("0.000001"), f"대사 불일치: ${counter} vs ${ledger}"
        # 정확히 한 번씩 — 과소청구를 고치면서 이중 차감을 만들지 않았는지.
        assert abs(counter - _PER_TURN * _TURNS) < Decimal("0.000001"), counter
        assert len({r["request_id"] for r in rows}) == _TURNS


@_redis_required
async def test_real_redis_client_reading_to_eof_is_the_control():
    """대조군 — 끝까지 읽는 클라이언트는 고치기 전에도 일치해야 한다.

    이게 깨지면 하네스(RTT 주입·대사 쿼리)가 틀린 것이고, 위 테스트의 실패를 끊김 탓으로 돌릴
    근거가 사라진다.
    """
    async with _real_redis() as (raw, auth):
        async with _Server(_recorder_app(_RttRedis(raw, _RTT), auth)) as server:
            assert b"event: response.completed" in await _eof_turn(server.port)
            counter, rows = await _reconcile(raw, auth, want_rows=1)

        assert len(rows) == 1
        assert Decimal(rows[0]["cost_usd"]) == _PER_TURN
        assert abs(counter - _PER_TURN) < Decimal("0.000001")
