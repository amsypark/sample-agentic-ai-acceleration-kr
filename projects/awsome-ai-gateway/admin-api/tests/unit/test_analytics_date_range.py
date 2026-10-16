# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`GET /admin/analytics` 의 start_date/end_date 는 **모든** 집계에 적용돼야 한다.

UI 의 `직접 입력`(시작일/종료일) 선택기는 start_date/end_date 를 계속 URL 에 실었지만
서버 라우트가 그 파라미터를 선언하지 않아 FastAPI 가 422 없이 버렸고, 화면에는 그 달
**전체** 숫자가 5일치라고 표시됐다(선택기는 고른 날짜를 그대로 보여준다).

여기서 못박는 것(집계 결과 자체는 실 PG 로 test_high_console_contract_real_db.py 가 증명):
  1. 범위가 repository 집계 5개 전부에 전달되는가 (요약 카드 + 모델별 요청 수)
  2. by_team / by_user / trends 의 WHERE 가 월 경계가 아니라 범위 경계인가
  3. 반영할 수 없는 입력(한쪽만/역순/형식·달력 오류)은 조용히 무시가 아니라 예외인가
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.exceptions import ValidationError
from app.models.auth import UserRole
from app.services.analytics_service import AnalyticsService

REPO_AGGREGATES = (
    "sum_usage_by_model",
    "count_requests_by_model",
    "count_active_users",
    "total_requests",
    "total_tokens",
)


def _admin() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        email="admin@example.com",
        role=UserRole.ADMIN,
        team_id=None,
    )


def _result(rows: list):
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    result.__iter__ = MagicMock(return_value=iter(rows))
    return result


def _session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda *a, **k: _result([]))
    return session


def _repo(MockRepo):
    repo = MockRepo.return_value
    repo.sum_usage_by_model = AsyncMock(return_value={"claude-sonnet": Decimal("30.00")})
    repo.count_requests_by_model = AsyncMock(return_value={"claude-sonnet": 12})
    repo.count_active_users = AsyncMock(return_value=3)
    repo.total_requests = AsyncMock(return_value=12)
    repo.total_tokens = AsyncMock(return_value=45_000)
    return repo


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


async def _run(session=None, **kwargs):
    with patch("app.services.analytics_service.AnalyticsRepository") as MockRepo:
        repo = _repo(MockRepo)
        resp = await AnalyticsService().get_analytics(
            session or _session(), period="2026-09", scope="all", actor=_admin(), **kwargs
        )
    return resp, repo


async def test_range_is_forwarded_to_every_repository_aggregate():
    resp, repo = await _run(start_date="2026-09-01", end_date="2026-09-05")

    for name in REPO_AGGREGATES:
        got = getattr(repo, name).await_args.kwargs.get("date_range")
        assert got == ("2026-09-01", "2026-09-05"), f"{name} 에 범위가 전달되지 않았다"
    # 서버가 반영한 구간을 응답에 싣는다 — 클라이언트가 검증할 수 있어야 한다.
    assert (resp.start_date, resp.end_date) == ("2026-09-01", "2026-09-05")


async def test_month_request_keeps_no_range():
    """대조군 — 범위가 없으면 예전과 같이 월 집계(date_range=None)다."""
    resp, repo = await _run()
    for name in REPO_AGGREGATES:
        assert getattr(repo, name).await_args.kwargs.get("date_range") is None, name
    assert resp.start_date is None and resp.end_date is None


async def test_by_team_and_trends_use_range_bounds_not_month_bounds():
    """KST 09-01 00:00 ~ 09-06 00:00 → UTC 08-31 15:00 ~ 09-05 15:00 (반개구간)."""
    session = _session()
    await _run(session, group_by="team", start_date="2026-09-01", end_date="2026-09-05")

    # 호출 순서: by_team → trends (group_by='team' 은 by_user 를 만들지 않는다)
    by_team_sql, trends_sql = (_sql(c.args[0]) for c in session.execute.await_args_list)
    for sql in (by_team_sql, trends_sql):
        assert "2026-08-31 15:00:00" in sql  # start (KST 09-01 00:00)
        assert "2026-09-05 15:00:00" in sql  # end   (KST 09-06 00:00)
        # 월 전체 경계(KST 10-01 00:00 = UTC 09-30 15:00)는 나오면 안 된다.
        assert "2026-09-30 15:00:00" not in sql
        # SUCCESS 필터가 범위 모드에서도 살아 있다(§59).
        assert "SUCCESS" in sql


async def test_month_mode_by_team_uses_month_bounds():
    """음성 대조 — 범위가 없으면 같은 질의가 월 경계를 쓴다(위 단정이 공허하지 않음을 보인다)."""
    session = _session()
    await _run(session, group_by="team")
    by_team_sql = _sql(session.execute.await_args_list[0].args[0])
    assert "2026-08-31 15:00:00" in by_team_sql
    assert "2026-09-30 15:00:00" in by_team_sql


async def test_mid_month_range_by_user_uses_range_and_skips_the_seed_queries():
    session = _session()
    await _run(session, group_by="user", start_date="2026-09-02", end_date="2026-09-03")

    sqls = [_sql(c.args[0]) for c in session.execute.await_args_list]
    # 호출 순서: by_team → by_user → trends. 중간 범위라 seed 질의(budget_usages)는 없다.
    assert len(sqls) == 3, sqls
    assert not any("budget.budget_usages" in q for q in sqls), "중간 범위에 월 seed 를 섞었다"
    by_user_sql = sqls[1]
    assert "auth.users" in by_user_sql
    assert "2026-09-01 15:00:00" in by_user_sql  # KST 09-02 00:00
    assert "2026-09-03 15:00:00" in by_user_sql  # KST 09-04 00:00


async def test_month_to_date_range_still_reads_the_seed():
    """월 초 시작 범위는 `/usage/by-user` 와 같은 규약으로 seed 를 더한다(질의가 나가야 한다)."""
    session = _session()
    await _run(session, group_by="user", start_date="2026-09-01", end_date="2026-09-10")
    sqls = [_sql(c.args[0]) for c in session.execute.await_args_list]
    assert any("budget.budget_usages" in q for q in sqls)


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-05", None),
        (None, "2026-09-05"),
        ("2026-09-05", "2026-09-01"),  # 역순
        ("2026-09-31", "2026-09-30"),  # 존재하지 않는 날짜
        ("2026-9-1", "2026-09-05"),  # 형식 오류
    ],
)
async def test_unhonorable_range_is_rejected_loudly(start, end):
    """반영할 수 없는 범위는 조용히 월 전체로 떨어지지 않고 예외(400)가 된다."""
    with pytest.raises(ValidationError):
        await _run(start_date=start, end_date=end)


async def test_period_is_still_validated_in_range_mode():
    """범위 모드는 월 필터를 타지 않는다 — 그래도 잘못된 period 가 200 으로 나가면 안 된다."""
    with patch("app.services.analytics_service.AnalyticsRepository") as MockRepo:
        _repo(MockRepo)
        with pytest.raises(ValidationError):
            await AnalyticsService().get_analytics(
                _session(),
                period="garbage",
                scope="all",
                start_date="2026-09-01",
                end_date="2026-09-05",
                actor=_admin(),
            )


async def test_single_day_range_is_allowed():
    resp, _ = await _run(start_date="2026-09-03", end_date="2026-09-03")
    assert (resp.start_date, resp.end_date) == ("2026-09-03", "2026-09-03")


# ── 범위 상한 / period 정합 ────────────────────────────────────────────────────


async def test_range_longer_than_the_cap_is_rejected():
    """2000-01-01~2100-12-31 같은 범위는 캐시되지 않는 요청 하나로 usage_logs 전체 집계를 6~9번 돌린다."""
    from app.services.analytics_service import MAX_ANALYTICS_RANGE_DAYS

    assert MAX_ANALYTICS_RANGE_DAYS == 93
    with pytest.raises(ValidationError, match="at most 93 days"):
        await _run(start_date="2026-09-01", end_date="2026-12-03")  # 양끝 포함 94일
    with patch("app.services.analytics_service.AnalyticsRepository") as MockRepo:
        _repo(MockRepo)
        with pytest.raises(ValidationError):
            await AnalyticsService().get_analytics(
                _session(), period="2000-01", scope="all", start_date="2000-01-01",
                end_date="2100-12-31", actor=_admin(),
            )


async def test_range_at_the_cap_is_allowed():
    """대조군 — 정확히 93일(양끝 포함)은 받는다."""
    resp, _ = await _run(start_date="2026-09-01", end_date="2026-12-02")
    assert (resp.start_date, resp.end_date) == ("2026-09-01", "2026-12-02")


async def test_period_must_be_the_month_of_start_date():
    """period 는 응답 라벨·캐시 키·seed 판정에 쓰인다 — 범위와 다른 달이면 400."""
    with pytest.raises(ValidationError, match="month of start_date"):
        await _run(start_date="2026-08-25", end_date="2026-09-05")  # _run 은 period=2026-09

    with patch("app.services.analytics_service.AnalyticsRepository") as MockRepo:
        _repo(MockRepo)
        resp = await AnalyticsService().get_analytics(  # 대조군 — start 의 달이면 달을 넘어도 된다
            _session(), period="2026-08", scope="all", start_date="2026-08-25",
            end_date="2026-09-05", actor=_admin(),
        )
    assert resp.start_date == "2026-08-25"
