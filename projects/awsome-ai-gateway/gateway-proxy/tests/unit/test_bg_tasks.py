# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``services/bg_tasks`` — 회계를 취소 가능한 응답 태스크 밖으로 빼는 원시 연산.

이 결함은 구조적으로 조용하다(잘린 회계는 Redis 차감만 남기고 원장 행 없이 아무것도 로그하지
않는다). 그래서 원시 연산이 지켜야 할 세 성질을 직접 고정한다:

  * 아무도 취소하지 않으면 ``spawn_shielded`` 는 ``await coro`` 와 같다(결과·예외 전달),
  * 호출자가 취소돼도 코루틴은 **끝까지 돌고**, 호출자는 여전히 ``CancelledError`` 를 본다
    (취소를 삼키는 것이 아니라 분리한다),
  * 강한 참조를 쥐므로 실행 중 태스크가 GC 로 사라지지 않는다.

그리고 셧다운 ``drain`` 의 계약 — 모든 회계가 싱크(body_logger·spool·Redis)가 닫히기 **전에**
끝나거나, 끝나지 못한 것은 이름과 요청 id 로 남는다.
"""

from __future__ import annotations

import asyncio
import gc
import json
import weakref

import pytest
import structlog

from app.services import bg_tasks
from app.services.streaming import responses_sse_stream


@pytest.mark.asyncio
async def test_spawn_shielded_awaits_normally_and_returns_the_result():
    order: list[str] = []

    async def work():
        order.append("start")
        await asyncio.sleep(0)
        order.append("end")
        return "done"

    assert await bg_tasks.spawn_shielded(work()) == "done"
    # 호출자가 기다렸다 — 여기 도달했을 때 "end" 가 이미 기록돼 있다.
    assert order == ["start", "end"]
    assert bg_tasks.pending_count() == 0


@pytest.mark.asyncio
async def test_spawn_shielded_propagates_exceptions_when_not_cancelled():
    async def boom():
        raise RuntimeError("upstream exploded")

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await bg_tasks.spawn_shielded(boom())


@pytest.mark.asyncio
async def test_shielded_work_completes_after_caller_is_cancelled():
    """회귀 테스트의 핵심: 호출자가 회계 도중에 죽어도 회계는 끝난다.

    운영 장애의 모양 그대로다 — 응답 태스크가 finalize 의 예산 차감과 원장 XADD 사이에서
    취소된다. shield 가 없으면 ``completed`` 는 영원히 set 되지 않는다.
    """
    started = asyncio.Event()
    completed = asyncio.Event()

    async def accounting():
        started.set()
        await asyncio.sleep(0.05)  # Redis 왕복 자리
        completed.set()

    async def caller():
        await bg_tasks.spawn_shielded(accounting(), name="test_accounting")

    task = asyncio.create_task(caller())
    await started.wait()
    task.cancel()

    # 호출자는 여전히 취소를 본다 — 의미는 보존되고, 삼켜지지 않는다.
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not completed.is_set(), "취소 직후에는 회계가 아직 진행 중이어야 한다"
    await asyncio.wait_for(completed.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_spawned_task_survives_garbage_collection():
    """asyncio 는 실행 중 태스크를 **약한 참조**로만 든다.

    ⚠️ 태스크를 ``asyncio.sleep`` 에 세워 두면 이 테스트는 공허하다 — 루프의 타이머 핸들이 그
       태스크를 강하게 붙들어서 ``bg_tasks`` 없이도 살아남는다(리뷰 프로브로 실측). 그래서 **이
       태스크만** 참조하는 future 에 세운다: 그러면 태스크 ↔ future 의 순환만 남고, 모듈 집합이
       붙들지 않으면 ``gc.collect()`` 가 수거한다("Task was destroyed but it is pending!").
    """

    async def parked():
        await asyncio.get_running_loop().create_future()  # 외부 참조 없는 future

    ref = weakref.ref(bg_tasks.spawn(parked(), name="gc_victim"))  # 핸들을 일부러 버린다
    await asyncio.sleep(0)  # 첫 스텝을 돌려 future 에 세운다(그 전에는 ready 큐가 붙든다)
    gc.collect()
    task = ref()
    assert task is not None, "실행 중 태스크가 GC 로 수거됐다 — 강한 참조가 없다"
    assert task in bg_tasks._tasks and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_drain_waits_for_outstanding_tasks():
    completed: list[int] = []

    async def work(n: int):
        await asyncio.sleep(0.01 * n)
        completed.append(n)

    for n in (1, 2, 3):
        bg_tasks.spawn(work(n))

    done = await bg_tasks.drain(timeout=2.0)
    assert done == 3
    assert sorted(completed) == [1, 2, 3]
    assert bg_tasks.pending_count() == 0


@pytest.mark.asyncio
async def test_drain_reports_zero_when_idle():
    assert await bg_tasks.drain(timeout=0.1) == 0


@pytest.mark.asyncio
async def test_drain_times_out_without_hanging_shutdown():
    """멈춘 태스크가 lifespan 을 영원히 붙잡으면 안 된다 — 로그를 남기고 놔둔다."""

    async def never():
        await asyncio.sleep(30)

    task = bg_tasks.spawn(never(), name="stuck")
    try:
        assert await bg_tasks.drain(timeout=0.05) == 0
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_failed_task_is_logged_not_silent():
    """떼어 낸 태스크의 실패는 GC 시점이 아니라 맥락과 함께 바로 남아야 한다.

    ``caplog`` 가 아니라 ``structlog.testing.capture_logs`` 로 단정한다 — 이 앱은 structlog 를
    stdout 으로 바로 쓰도록 설정해서 stdlib 레코드가 생기지 않고, caplog 단정은 공허하게 통과한다.
    """

    async def boom():
        raise ValueError("accounting failed")

    with structlog.testing.capture_logs() as logs:
        task = bg_tasks.spawn(boom(), name="failing")
        with pytest.raises(ValueError):
            await task
        await asyncio.sleep(0)  # done-callback 이 돌 기회

    entry = next((e for e in logs if e.get("event") == "bg_task_failed"), None)
    assert entry is not None, logs
    assert entry["task_name"] == "failing"
    assert entry["error"] == "accounting failed"


# ── 셧다운 drain(main.py lifespan) — 싱크가 닫히기 전에 끝내기 ───────────────


class _ScopedRequest:
    """미들웨어(OTel)가 scope state 에 요청 id 를 넣은 Starlette 요청의 대역."""

    def __init__(self, request_id: str) -> None:
        self.scope = {"state": {"request_id": request_id}}

    async def is_disconnected(self) -> bool:  # pragma: no cover - 호출되면 안 된다
        raise AssertionError("is_disconnected() 는 스트리밍 응답에서 신뢰할 수 없다")


def _delta(text: str) -> bytes:
    return json.dumps({"type": "response.output_text.delta", "delta": text}).encode()


async def _tokenizer(text: str) -> int:
    return len(text)


@pytest.mark.asyncio
async def test_shutdown_drain_makes_a_still_reading_stream_drain_account_before_sinks_close():
    """main.py 순서 그대로: ``bg_tasks.drain()`` → (body_logger.stop / spool / redis.aclose).

    클라이언트가 ESC 로 끊은 뒤에도 모델은 계속 생성 중이라, 상류를 마저 읽는 drain 은 셧다운
    대기 안에 끝나지 않는다. 고치기 전에는 ``drain()`` 이 그냥 리턴했고, 그 drain 의 finally(회계)는
    asyncio.run 의 teardown 취소에서 — 모든 싱크가 닫힌 **뒤에** 돌았다(리뷰 프로브로 실측).
    """
    fired: list[tuple[int, bool]] = []
    sinks_closed = False

    async def on_usage(usage, first_token_time) -> None:
        await asyncio.sleep(0.01)  # 예산 EVAL + XADD 왕복 자리
        fired.append((usage.output_tokens, sinks_closed))

    async def still_generating():
        yield _delta("hi")
        while True:
            await asyncio.sleep(0.01)
            yield _delta("x")

    gen = responses_sse_stream(
        _ScopedRequest("req-shutdown-1"),
        still_generating(),
        on_usage=on_usage,
        tokenizer_hook=_tokenizer,
        idle_timeout=5.0,
        drain_timeout=30.0,
    )
    await gen.__anext__()  # 프레임 하나가 클라이언트에 갔다
    await gen.aclose()  # 클라이언트가 사라졌다 → 제너레이터가 drain 을 떼어 낸다
    assert bg_tasks.pending_count() == 1

    await bg_tasks.drain(timeout=0.1)
    sinks_closed = True  # body_logger.stop() · spool drain · redis.aclose() · db dispose

    assert fired, "drain 의 회계가 셧다운 drain 안에서 돌지 않았다 — 싱크가 닫힌 뒤로 밀린다"
    tokens, after_close = fired[0]
    assert not after_close
    assert tokens >= len("hi"), "취소 전까지 읽은 텍스트로 역산해야 한다"
    assert bg_tasks.pending_count() == 0
    assert len(fired) == 1, f"회계가 {len(fired)}회 — 정확히 한 번이어야 한다"


@pytest.mark.asyncio
async def test_drain_also_waits_for_tasks_spawned_while_draining():
    """첫 스냅샷 뒤에 생긴 태스크도 기다린다(끝나며 다른 태스크를 띄우는 경우)."""
    order: list[str] = []

    async def spawned_late():
        await asyncio.sleep(0.05)
        order.append("late")

    async def first():
        await asyncio.sleep(0.01)
        bg_tasks.spawn(spawned_late(), name="spawned_late")
        order.append("first")

    bg_tasks.spawn(first(), name="first")
    await bg_tasks.drain(timeout=2.0)
    assert order == ["first", "late"], order
    assert bg_tasks.pending_count() == 0


@pytest.mark.asyncio
async def test_drain_names_abandoned_accounting_by_request_id_and_never_cuts_it():
    """끝나지 않는 회계(Redis 정지 등)는 자르지 않고, 요청 id 와 함께 남긴다.

    finalize 를 취소하면 차감과 XADD 사이가 끊긴다 — 이 모듈이 막으려는 바로 그 결함이다. 그래서
    셧다운 drain 은 상류를 읽는 drain 만 취소하고, 회계 태스크는 이름(요청 id 포함)만 로그한다.
    운영자는 그 요청 id 로 원장을 대사한다.
    """
    entered = asyncio.Event()

    async def stuck_on_usage(usage, first_token_time) -> None:
        entered.set()
        await asyncio.Event().wait()  # 영원히 돌아오지 않는 Redis

    gen = responses_sse_stream(
        _ScopedRequest("req-stuck-7"),
        _aiter([_delta("hi")]),
        on_usage=stuck_on_usage,
        idle_timeout=5.0,
        drain_timeout=5.0,
    )

    async def consume():
        async for _ in gen:
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    consumer.cancel()  # 클라이언트가 종결 프레임에서 끊었다 — 회계는 떼어 낸 태스크에 남는다
    with pytest.raises(asyncio.CancelledError):
        await consumer

    with structlog.testing.capture_logs() as logs:
        await bg_tasks.drain(timeout=0.05)  # 취소할 drain 이 없으니 유예 대기도 없다

    stuck = [t for t in bg_tasks._tasks if not t.done()]
    assert len(stuck) == 1, "회계 태스크를 취소했다 — 차감과 XADD 사이를 자른다"
    assert "req-stuck-7" in stuck[0].get_name()
    entry = next((e for e in logs if e.get("event") == "bg_tasks_drain_abandoned"), None)
    assert entry is not None, logs
    assert entry["request_ids"] == ["req-stuck-7"]
    assert any("req-stuck-7" in n for n in entry["tasks"])
    stuck[0].cancel()


def _aiter(items):
    async def _gen():
        for item in items:
            yield item

    return _gen()
