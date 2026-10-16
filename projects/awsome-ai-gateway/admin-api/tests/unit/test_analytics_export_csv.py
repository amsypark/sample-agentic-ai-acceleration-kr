# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`/admin/analytics/export` — CSV 는 화면과 **같은 축**이어야 하고 scope/범위를 존중해야 한다.

예전 `_to_csv` 는 group_by 를 인자로 받지도 않고 늘 `period,model,cost_usd` 헤더에
`by_model` 만 썼다. '팀별'/'사용자별' 화면에서 내보내면 서비스가 by_team/by_user 를
계산해 놓고 한 줄 뒤에서 버렸다. export_analytics 는 scope 를 `"all"` 로 하드코딩했다
(라우트는 scope/start_date/end_date 를 선언조차 하지 않았다).

⚠️ 권한 관점의 정정: TEAM_LEADER 가 "전사 CSV" 를 받은 적은 **없다** — scope='all' 이어도
   get_analytics 가 TEAM_LEADER 를 자기 팀으로 좁힌다. 깨져 있던 것은 화면↔파일 일치다
   (ADMIN 의 `scope=team:X` 화면이 전사 CSV 를, 범위 화면이 월 전체 CSV 를 내려받았다).
   scope 를 실제로 넘기게 되면서 남의 팀 scope 는 403 이 되는지를 아래에서 같이 고정한다.
"""
from __future__ import annotations

import csv
import io
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError
from app.models.auth import UserRole
from app.schemas.analytics import (
    AnalyticsResponse,
    CostSummary,
    ModelBreakdown,
    TeamBreakdown,
    UserBreakdown,
)
from app.services.analytics_service import AnalyticsService

TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _actor(role: UserRole = UserRole.ADMIN, team_id: uuid.UUID | None = TEAM_ID) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        email="a@example.com",
        role=role,
        team_id=team_id,
    )


def _response(**overrides) -> AnalyticsResponse:
    payload = dict(
        period="2026-08",
        cost_summary=CostSummary(),
        by_model=[ModelBreakdown(model="claude-sonnet-4", requests=3, cost_usd=Decimal("9.00"))],
        by_team=[
            TeamBreakdown(team="Team A", team_id=str(TEAM_ID), cost_usd=Decimal("5.00"), active_users=2),
            TeamBreakdown(team="Team B", team_id=str(OTHER_TEAM_ID), cost_usd=Decimal("4.00"), active_users=1),
        ],
        by_user=[
            UserBreakdown(user="alice", email="alice@example.com", cost_usd=Decimal("6.00"), requests=4),
            UserBreakdown(user="bob", email="bob@example.com", cost_usd=Decimal("3.00"), requests=2),
        ],
    )
    payload.update(overrides)
    return AnalyticsResponse(**payload)


def _rows(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


def test_csv_team_grouping_emits_team_rows():
    rows = _rows(AnalyticsService._to_csv(_response(), "team"))
    assert rows[0] == ["period", "team", "team_id", "cost_usd", "active_users"]
    assert "model" not in rows[0]  # 회귀 고정: 팀 축인데 모델 표가 나오면 실패
    assert [r[1] for r in rows[1:]] == ["Team A", "Team B"]
    assert [r[3] for r in rows[1:]] == ["5.00", "4.00"]


def test_csv_user_grouping_emits_user_rows():
    rows = _rows(AnalyticsService._to_csv(_response(), "user"))
    assert rows[0] == ["period", "user", "email", "cost_usd", "requests"]
    assert [r[1] for r in rows[1:]] == ["alice", "bob"]
    assert [r[2] for r in rows[1:]] == ["alice@example.com", "bob@example.com"]


def test_csv_model_grouping_keeps_the_legacy_header():
    """대조군 — 모델 축(기본값)은 기존 3열 그대로다(기존 소비자 보호)."""
    rows = _rows(AnalyticsService._to_csv(_response(), "model"))
    assert rows[0] == ["period", "model", "cost_usd"]
    assert rows[1] == ["2026-08", "claude-sonnet-4", "9.00"]
    assert _rows(AnalyticsService._to_csv(_response())) == rows  # 기본 인자 = model


def test_csv_empty_team_breakdown_is_header_only():
    """TEAM_LEADER 는 by_team 이 비어 있다(fail-closed) — 헤더만 나오는 것이 정답이다."""
    rows = _rows(AnalyticsService._to_csv(_response(by_team=[]), "team"))
    assert rows == [["period", "team", "team_id", "cost_usd", "active_users"]]


def test_csv_period_column_states_the_actual_range():
    rows = _rows(
        AnalyticsService._to_csv(_response(start_date="2026-08-01", end_date="2026-08-05"), "model")
    )
    assert rows[1][0] == "2026-08-01~2026-08-05"


async def test_export_forwards_scope_and_range_to_get_analytics():
    svc = AnalyticsService()
    with patch.object(svc, "get_analytics", AsyncMock(return_value=_response())) as spy:
        content, ctype = await svc.export_analytics(
            AsyncMock(),
            format="csv",
            period="2026-08",
            group_by="team",
            scope=f"team:{TEAM_ID}",
            start_date="2026-08-01",
            end_date="2026-08-05",
            actor=_actor(),
        )

    kwargs = spy.await_args.kwargs
    assert kwargs["scope"] == f"team:{TEAM_ID}", "export 가 scope 를 다시 하드코딩했다"
    assert (kwargs["start_date"], kwargs["end_date"]) == ("2026-08-01", "2026-08-05")
    assert kwargs["group_by"] == "team"
    assert ctype == "text/csv"
    assert _rows(content)[0][1] == "team"  # group_by 가 직렬화까지 도달했다


async def test_export_defaults_to_global_scope():
    """대조군 — 인자를 안 주면 예전과 같은 scope='all' 이다(기존 호출자 호환)."""
    svc = AnalyticsService()
    with patch.object(svc, "get_analytics", AsyncMock(return_value=_response())) as spy:
        await svc.export_analytics(
            AsyncMock(), format="csv", period="2026-08", group_by="model", actor=_actor()
        )
    assert spy.await_args.kwargs["scope"] == "all"
    assert spy.await_args.kwargs["start_date"] is None


async def test_export_cannot_cross_team_boundary():
    """scope 를 실제로 넘기므로, 리더가 남의 팀을 내보내려면 403 이어야 한다."""
    result = MagicMock()
    result.all = MagicMock(return_value=[])
    result.__iter__ = MagicMock(return_value=iter([]))
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(ForbiddenError):
        await AnalyticsService().export_analytics(
            session,
            format="csv",
            period="2026-08",
            group_by="team",
            scope=f"team:{OTHER_TEAM_ID}",
            actor=_actor(UserRole.TEAM_LEADER),
        )
    session.execute.assert_not_awaited()  # 막기 전에 아무것도 읽지 않았다


async def test_json_export_carries_the_range():
    svc = AnalyticsService()
    resp = _response(start_date="2026-08-01", end_date="2026-08-05")
    with patch.object(svc, "get_analytics", AsyncMock(return_value=resp)):
        content, ctype = await svc.export_analytics(
            AsyncMock(),
            format="json",
            period="2026-08",
            group_by="model",
            start_date="2026-08-01",
            end_date="2026-08-05",
            actor=_actor(),
        )
    assert ctype == "application/json"
    assert '"start_date": "2026-08-01"' in content


# ── CSV 수식 주입 ─────────────────────────────────────────────────────────────

INJECTIONS = ["=HYPERLINK(\"http://x\")", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1:A2)", "\tTAB", "\rCR"]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_user_and_email_cells_cannot_start_a_formula(payload):
    resp = _response(by_user=[UserBreakdown(user=payload, email=payload, cost_usd=Decimal("1.00"), requests=1)])
    row = _rows(AnalyticsService._to_csv(resp, "user"))[1]
    assert row[1] == "'" + payload and row[2] == "'" + payload, row


@pytest.mark.parametrize("payload", INJECTIONS)
def test_team_and_model_cells_cannot_start_a_formula(payload):
    resp = _response(
        by_team=[TeamBreakdown(team=payload, team_id=str(TEAM_ID), cost_usd=Decimal("1"), active_users=1)],
        by_model=[ModelBreakdown(model=payload, requests=1, cost_usd=Decimal("1"))],
    )
    assert _rows(AnalyticsService._to_csv(resp, "team"))[1][1] == "'" + payload
    assert _rows(AnalyticsService._to_csv(resp, "model"))[1][1] == "'" + payload


def test_ordinary_text_and_numbers_are_untouched():
    """대조군 — 평범한 이름·이메일과 숫자 셀(우리가 만든 값)은 그대로다."""
    resp = _response(by_user=[UserBreakdown(user="alice", email="alice@example.com", cost_usd=Decimal("6.00"), requests=4)])
    row = _rows(AnalyticsService._to_csv(resp, "user"))[1]
    assert row[1:] == ["alice", "alice@example.com", "6.00", "4"]
