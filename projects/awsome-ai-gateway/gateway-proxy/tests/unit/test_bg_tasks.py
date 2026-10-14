# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``services/bg_tasks`` — 회계를 취소 가능한 응답 태스크 밖으로 빼는 원시 연산.

이 결함은 구조적으로 조용하다(잘린 회계는 Redis 차감만 남기고 원장 행 없이 아무것도 로그하지
않는다). 그래서 원시 연산이 지켜야 할 세 성질을 직접 고정한다:

  * 아무도 취소하지 않으면 ``spawn_shielded`` 는 ``await coro`` 와 같다(결과·예외 전달),
  * 호출자가 취소돼도 코루틴은 **끝까지 돌고**, 호출자는 여전히 ``CancelledError`` 를 본다
    (취소를 삼키는 것이 아니라 분리한다),
  * 강한 참조를 쥐므로 실행 중 태스크가 GC 로 사라지지 않는다.
"""

from __future__ import annotations

import asyncio
import gc

import pytest
import structlog

from app.services import bg_tasks


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

    핸들을 버린 bare ``asyncio.create_task`` 는 도중에 수거될 수 있다("Task was destroyed but
    it is pending!"). ``spawn`` 은 모듈 집합에 태스크를 붙들어 GC 가 회계를 버리지 못하게 한다.
    """
    completed = asyncio.Event()

    async def work():
        await asyncio.sleep(0.02)
        completed.set()

    bg_tasks.spawn(work(), name="gc_victim")  # 핸들을 일부러 버린다
    gc.collect()
    await asyncio.wait_for(completed.wait(), timeout=1.0)


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
