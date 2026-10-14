# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""스트리밍 회계를 클라이언트 끊김 취소로부터 떼어 내는 스케줄러.

왜 필요한가
-----------
스트리밍 응답의 회계(``cost_recorder.finalize``)는 마지막 SSE 프레임을 yield **한 뒤에**
돈다 — usage 가 종결 프레임에만 실리기 때문이다. 그런데 chunked 응답의 종료 청크(0 길이)는
제너레이터가 **리턴해야** 나간다. 그래서 종결 이벤트를 읽자마자 연결을 닫는 클라이언트
(Codex CLI 는 ``response.completed`` 를 읽는 즉시 스트림을 버린다 — 매 턴)의 끊김은 정확히
회계 도중에 도착한다.

uvicorn 은 ASGI ``spec_version`` 2.3 을 보고하므로 Starlette ``StreamingResponse`` 는
``listen_for_disconnect`` task-group 경로를 타고, 끊김에 응답 태스크를 **취소**한다(anyio
취소 스코프는 level-triggered 라 ``finally`` 안의 await 도 다시 취소된다).
``asyncio.CancelledError`` 는 ``BaseException`` 이라 finalize 의 ``except Exception`` 을 전부
통과한다: 이미 보낸 예산 EVAL 은 Redis 에 남고, 원장 XADD 는 실행되지 않고, 로그는 없다.

⚠️ 실측(실 Redis + 실 uvicorn): Codex 방식 6턴에서 Redis 예산 카운터 $0.022386 vs 원장 $0,
   원장 행 0/6. "카운터는 맞고 원장만 짧다" — 대사로만 드러나는 모양이다.
   (``tests/regression/test_critical_stream_disconnect_ledger_gap.py``)

그래서 회계를 취소 가능한 태스크 밖으로 뺀다. :func:`spawn_shielded` 는 회계 코루틴을 이
모듈이 강한 참조로 붙드는 태스크에 올리고 :func:`asyncio.shield` 로 기다린다. 호출자는 여전히
``CancelledError`` 를 본다 — 취소를 삼키는 것이 아니라 코루틴의 **수명**만 호출자에게서 분리한다.

대안을 쓰지 않는 이유
---------------------
  * finalize 를 "XADD 먼저, 차감 나중" 으로 바꾸면 유실 방향만 뒤집힌다(원장은 있고 카운터는
    없음 = 예산 fail-open).
  * XADD 주변의 ``except BaseException`` 은 소용이 없다. 취소는 대개 그 **앞**의 Redis 왕복
    (EVAL 2~3회 + 정산)에 떨어지고, 거기서 ``CancelledError`` 를 삼키면 다른 모든 것의 취소
    의미가 깨진다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, NamedTuple

import structlog

logger = structlog.get_logger(__name__)


class _Meta(NamedTuple):
    #: 이 태스크가 회계하는 요청. 셧다운에 남은 태스크를 원장과 대사하는 열쇠다.
    request_id: str | None
    #: 상류를 끝까지 읽는 drain 처럼, 셧다운 대기가 끝나면 **한 번** 취소해도 되는 태스크.
    cancel_on_shutdown: bool


#: 유예 창이 끝난 패스에서 막 취소한 drain 에 주는 1회 대기(초). 그 drain 의 finally 는 보호
#: 태스크로 회계를 넘기고 기다리므로(services/streaming.py) 정상이면 Redis 왕복 몇 번이면 끝난다.
_LATE_CANCEL_WAIT = 0.5

#: 떼어 낸 태스크의 강한 참조(+ 셧다운 drain 이 쓰는 메타데이터).
#:
#: ⚠️ asyncio 는 실행 중 태스크를 **약한 참조**로만 들고 있다. 핸들을 지역 변수로만 둔 태스크는
#:    도중에 GC 될 수 있다("Task was destroyed but it is pending!"). 떼어 낸 회계 태스크는 끝날
#:    때까지 여기 둔다 — 셧다운 :func:`drain` 이 가능한 것도 이 표 덕분이다.
_tasks: dict[asyncio.Task[Any], _Meta] = {}


def request_id_of(request: Any) -> str | None:
    """미들웨어(OTel)가 ``scope["state"]`` 에 넣은 요청 id — 태스크 이름용. 없으면 None."""
    scope = getattr(request, "scope", None)
    state = scope.get("state") if isinstance(scope, dict) else None
    return state.get("request_id") if isinstance(state, dict) else None


def _on_done(task: asyncio.Task[Any]) -> None:
    _tasks.pop(task, None)
    if task.cancelled():
        # 셧다운 drain 이 cancel_on_shutdown 태스크를 끊었거나(그 목록은 drain 이 로그한다)
        # 루프 teardown 이 남은 것을 끊은 경우다(그 전에 drain 이 abandoned 로 남겼다).
        return
    exc = task.exception()
    if exc is not None:
        # 떼어 낸 태스크의 예외는 그대로 두면 GC 시점에 루프 기본 핸들러가 맥락 없이 찍을 뿐이다.
        logger.error(
            "bg_task_failed",
            task_name=task.get_name(),
            error=str(exc),
            exc_info=exc,
        )


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
    request_id: str | None = None,
    cancel_on_shutdown: bool = False,
) -> asyncio.Task[Any]:
    """``coro`` 를 호출자의 취소 스코프와 분리된 태스크로 띄운다(기다리지 않는다).

    fire-and-forget 용(스트림 drain). 정상 경로에서는 호출자가 완료를 기다려야 하는 회계에는
    :func:`spawn_shielded` 를 쓴다.

    ``request_id`` 는 태스크 이름(``<name>:<request_id>``)에 붙는다. ``cancel_on_shutdown`` 은
    상류를 마저 읽는 drain 전용이다 — :func:`drain` 의 대기가 끝나도 남아 있으면 한 번 취소되고,
    그 finally 가 그때까지 읽은 usage 로 회계한다.
    """
    label = ":".join(part for part in (name, request_id) if part) or None
    task = asyncio.create_task(coro, name=label)
    _tasks[task] = _Meta(request_id, cancel_on_shutdown)
    task.add_done_callback(_on_done)
    return task


async def spawn_shielded(
    coro: Coroutine[Any, Any, Any], *, name: str | None = None, request_id: str | None = None
) -> Any:
    """호출자가 취소돼도 ``coro`` 는 끝까지 돈다.

    취소가 없으면 ``await coro`` 와 같다 — 호출자가 기다리고, 결과와 예외가 그대로 전달된다.
    호출자가 취소되면 호출자는 평소대로 ``CancelledError`` 를 받고, ``coro`` 는 자기 태스크에서
    계속 돈다. (셧다운에도 취소하지 않는다 — 회계를 자르면 차감과 XADD 사이가 끊긴다.)
    """
    return await asyncio.shield(spawn(coro, name=name, request_id=request_id))


def pending_count() -> int:
    """아직 실행 중인 떼어 낸 태스크 수. 테스트와 셧다운 로그용."""
    return sum(1 for t in _tasks if not t.done())


async def drain(timeout: float = 10.0, *, cancel_grace: float = 2.0) -> int:
    """셧다운: 떼어 낸 태스크를 끝낸다. 끝난(완료·실패·취소) 개수를 돌려준다.

    lifespan 셧다운에서, 회계가 쓰는 싱크(body_logger·spool·Redis)를 닫기 **전에** 부른다.
    shield 는 **요청** 취소만 막는다 — 여기서 끝내지 못한 태스크는 asyncio.run 의 teardown 취소가
    싱크가 닫힌 **뒤에** 그 finally 를 돌린다(리뷰 프로브로 실측: 차감·XADD 가 sinks_closed 뒤).

    1. ``timeout`` 까지 기다린다 — 매번 다시 스냅샷해서, 기다리는 동안 생긴 태스크도 기다린다.
    2. 남은 ``cancel_on_shutdown`` 태스크(상류를 마저 읽는 drain)를 **한 번** 취소한다 — finally 가
       그때까지 읽은 usage 로 회계한다. 회계 태스크는 취소하지 않는다(자르면 그게 바로 결함이다).
    3. ``cancel_grace`` 동안 다시 기다린다(그 사이 생긴 drain 도 한 번 취소한다).
    4. 그래도 남은 것은 이름과 요청 id 를 로그한다 — 그 요청들은 원장 대사가 필요하다.
    """
    loop = asyncio.get_running_loop()
    #: 스냅샷 시점의 메타. 끝난 태스크는 _tasks 에서 빠지므로 로그용으로 따로 쥔다.
    meta: dict[asyncio.Task[Any], _Meta] = {}
    cancelled: set[asyncio.Task[Any]] = set()

    def _pending() -> set[asyncio.Task[Any]]:
        live = {t: m for t, m in _tasks.items() if not t.done()}
        meta.update(live)
        return set(live)

    def _describe(tasks: set[asyncio.Task[Any]]) -> dict[str, list]:
        ordered = sorted(tasks, key=lambda t: t.get_name())
        return {
            "tasks": [t.get_name() for t in ordered],
            "request_ids": [meta[t].request_id for t in ordered if meta[t].request_id],
        }

    async def _wait(deadline: float, *, cancel: bool) -> set[asyncio.Task[Any]]:
        while True:
            pending = _pending()
            batch = {t for t in pending - cancelled if cancel and meta[t].cancel_on_shutdown}
            if batch:
                for t in batch:
                    t.cancel()
                cancelled.update(batch)
                logger.warning("bg_tasks_drain_cancelled", **_describe(batch))
            remaining = deadline - loop.time()
            if not pending:
                return pending
            if remaining <= 0:
                if batch:
                    # ⚠️ 마지막 패스에서 처음 본 drain 은 방금 취소됐을 뿐 finally 를 돌 기회가
                    #    없었다. 그대로 리턴하면 회계 없이 abandoned 가 된다 — 그 묶음만 한 번
                    #    짧게 기다린다.
                    await asyncio.wait(batch, timeout=min(_LATE_CANCEL_WAIT, cancel_grace))
                    return _pending()
                return pending
            await asyncio.wait(pending, timeout=remaining)

    if not _pending():
        return 0
    logger.info("bg_tasks_drain_start", pending=len(meta), timeout=timeout)
    pending = await _wait(loop.time() + timeout, cancel=False)
    if any(meta[t].cancel_on_shutdown for t in pending):
        pending = await _wait(loop.time() + cancel_grace, cancel=True)
    if pending:
        logger.warning("bg_tasks_drain_abandoned", timeout=timeout, **_describe(pending))
    return sum(1 for t in meta if t.done())
