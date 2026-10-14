# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""BatchFlusher 단위 테스트.

배치 flush 로직의 핵심 경로 검증:
- 빈 entries → no-op
- user+team 스코프별 period 집계 합산
- threshold_triggered 있는 entry → notifications:budget publish
- daily counter pipeline 호출
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.batch_flusher import BatchFlusher
from worker.schemas.cost_stream import CostStreamEntry


def _make_entry(
    request_id: str = "req-1",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    team_id: str = "00000000-0000-0000-0000-000000000002",
    cost: str = "0.01",
    threshold: int | None = None,
    aws_account_id: str | None = None,
) -> CostStreamEntry:
    return CostStreamEntry(
        request_id=request_id,
        user_id=user_id,
        team_id=team_id,
        dept_id="00000000-0000-0000-0000-000000000003",
        model_alias="claude-sonnet-4-6",
        provider="BEDROCK",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=200,
        is_streaming=False,
        estimated_usage=False,
        requested_at="2026-04-21T10:00:00+00:00",
        completed_at="2026-04-21T10:00:01+00:00",
        period="2026-04",
        date="2026-04-21",
        threshold_triggered=threshold,
        aws_account_id=aws_account_id,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_empty_batch_is_noop():
    session_factory = MagicMock()
    redis = MagicMock()
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([])
    session_factory.assert_not_called()
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_flush_inserts_usage_and_upserts_budgets():
    """entries 3개, 같은 user/team/period 이면 budget_usages UPSERT 는 집계 1건씩."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id=f"req-{i}", cost="0.10") for i in range(3)
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # 4 session.execute 호출: 재처리 필터 SELECT + INSERT usage_logs
    # + UPSERT user budget + UPSERT team budget
    #
    # ⚠️ 첫 호출이 재처리 필터여야 한다. budget_usages 는 **가산** UPSERT 라, 재처리된
    #    entry 를 걸러내지 않으면 사용자의 기록 사용액이 영구히 두 배가 된다
    #    (batch_flusher._filter_replays 주석 참조). 개수만 세면 그 SELECT 가 뒤로 밀려
    #    무의미해진 것을 잡지 못하므로 순서를 함께 못 박는다.
    assert session.execute.await_count == 4
    assert session.commit.await_count == 1
    replay_sql = str(session.execute.await_args_list[0].args[0]).lower()
    assert "from usage.usage_logs" in replay_sql and "request_id" in replay_sql, (
        f"첫 호출이 재처리 필터 SELECT 가 아니다: {replay_sql[:120]}"
    )

    # 3번째 호출 = user UPSERT — 합산된 cost (0.30)
    user_call = session.execute.await_args_list[2]
    user_params = user_call.args[1]
    assert len(user_params) == 1  # 단일 (user, period) 그룹
    assert user_params[0]["scope"] == "USER"
    assert user_params[0]["cost"] == "0.30"

    # 4번째 호출 = team UPSERT
    team_call = session.execute.await_args_list[3]
    team_params = team_call.args[1]
    assert team_params[0]["scope"] == "TEAM"
    assert team_params[0]["cost"] == "0.30"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_triggered_publishes_notification():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id="req-normal"),  # no threshold
        _make_entry(request_id="req-80pct", threshold=80),
        _make_entry(request_id="req-100pct", threshold=100),
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # publish는 threshold != None 인 2건만.
    assert redis.publish.await_count == 2
    channels = [call.args[0] for call in redis.publish.await_args_list]
    assert all(c == "notifications:budget" for c in channels)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_daily_counter_pipeline_uses_hash_tag():
    """Redis Cluster hash tag {<user_id>} 가 daily counter 키에 포함."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entry = _make_entry(user_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([entry])

    # incrbyfloat 첫 호출의 key 확인 — hash tag 포함.
    keys_seen = [call.args[0] for call in pipe.incrbyfloat.call_args_list]
    assert any("{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}" in k for k in keys_seen)


# ── 서빙 계정 (마이그레이션 0045) ─────────────────────────────────────────────────


def test_insert_names_the_serving_account_column():
    """usage_logs INSERT 가 aws_account_id 컬럼과 바인드를 둘 다 가져야 한다 — 하나만 있으면
    값이 조용히 유실되거나(컬럼 없음) 매 flush 가 죽는다(바인드 없음)."""
    from worker.batch_flusher import _INSERT_USAGE_LOGS

    sql = str(_INSERT_USAGE_LOGS)
    assert ", aws_account_id" in sql and ":aws_account_id" in sql


@pytest.mark.asyncio
@pytest.mark.unit
async def test_insert_params_carry_the_serving_account():
    session = AsyncMock()
    session.execute = AsyncMock()
    flusher = BatchFlusher(session_factory=MagicMock(), redis=MagicMock())

    await flusher._insert_usage_logs(session, [_make_entry(aws_account_id="111122223333")])
    assert session.execute.await_args.args[1][0]["aws_account_id"] == "111122223333"

    # None = 게이트웨이 자신의 계정 — 그대로 NULL 로 간다.
    session.execute.reset_mock()
    await flusher._insert_usage_logs(session, [_make_entry()])
    assert session.execute.await_args.args[1][0]["aws_account_id"] is None


@pytest.mark.parametrize("bad", ["12345", "1111222233334", "ABCDEFGHIJKL", "", 111122223333])
def test_a_malformed_account_is_dropped_rather_than_failing_the_batch(bad):
    """VARCHAR(12) 원장 컬럼: 형식이 어긋난 값 하나가 INSERT 를 실패시키면 배치 전체가 기록되지
    않는다(XACK 안 됨 → 재시도 반복). 감사 정보만 비우고 과금 행은 살린다."""
    assert _make_entry(aws_account_id=bad).aws_account_id is None


def test_an_entry_from_a_pre_0045_gateway_parses():
    """롤아웃 순서 무관: 필드가 없는 엔트리도 그대로 읽힌다."""
    raw = _make_entry().model_dump(mode="json")
    raw.pop("aws_account_id")
    assert CostStreamEntry.model_validate(raw).aws_account_id is None
