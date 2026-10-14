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
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: 떼어 낸 태스크의 강한 참조.
#:
#: ⚠️ asyncio 는 실행 중 태스크를 **약한 참조**로만 들고 있다. 핸들을 지역 변수로만 둔 태스크는
#:    도중에 GC 될 수 있다("Task was destroyed but it is pending!"). 떼어 낸 회계 태스크는 끝날
#:    때까지 여기 둔다 — 셧다운 :func:`drain` 이 가능한 것도 이 집합 덕분이다.
_tasks: set[asyncio.Task[Any]] = set()


def _on_done(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        # 루프 종료나 drain() 타임아웃에서만 도달한다 — 둘 다 자체 로그가 있다.
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


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """``coro`` 를 호출자의 취소 스코프와 분리된 태스크로 띄운다(기다리지 않는다).

    fire-and-forget 용(스트림 drain). 정상 경로에서는 호출자가 완료를 기다려야 하는 회계에는
    :func:`spawn_shielded` 를 쓴다.
    """
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


async def spawn_shielded(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> Any:
    """호출자가 취소돼도 ``coro`` 는 끝까지 돈다.

    취소가 없으면 ``await coro`` 와 같다 — 호출자가 기다리고, 결과와 예외가 그대로 전달된다.
    호출자가 취소되면 호출자는 평소대로 ``CancelledError`` 를 받고, ``coro`` 는 자기 태스크에서
    계속 돈다.
    """
    return await asyncio.shield(spawn(coro, name=name))


def pending_count() -> int:
    """아직 실행 중인 떼어 낸 태스크 수. 테스트와 셧다운 로그용."""
    return sum(1 for t in _tasks if not t.done())


async def drain(timeout: float = 10.0) -> int:
    """남은 떼어 낸 태스크를 기다린다. 완료된 개수를 돌려준다.

    lifespan 셧다운에서 부른다. shield 는 **요청** 취소만 막는다 — 이 drain 이 없으면 회계는
    여전히 **프로세스** 셧다운(롤링 배포마다)에 노출된다. 창은 작아도 같은 조용한 유실이다.
    """
    pending = {t for t in _tasks if not t.done()}
    if not pending:
        return 0
    logger.info("bg_tasks_drain_start", pending=len(pending), timeout=timeout)
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    if still_pending:
        logger.warning("bg_tasks_drain_timeout", pending=len(still_pending), timeout=timeout)
    return len(done)
