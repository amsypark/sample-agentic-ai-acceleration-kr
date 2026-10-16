# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/dashboard/top-teams`` 는 행마다 ``team_id`` 를 싣는다.

콘솔 대시보드는 이 목록을 ``/admin/budgets/summary`` 의 소진율과 조인한다. id 가 없던 동안 조인키가
**팀 이름**이었다 — 팀 이름은 전역 유일이 아니어서(부서가 달라도, 이 스키마에선 같은 부서여도 같은
이름이 가능하다) 동명 팀의 소진율이 서로 뒤바뀌었다. 동명 팀이 실제로 두 행으로 나오는지는 실 PG 로
test_high_console_contract_real_db.py 가 증명한다.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.routers.dashboard import top_teams

TEAM_A = uuid.UUID("aaaa1111-0000-4000-a000-000000000001")
TEAM_B = uuid.UUID("bbbb1111-0000-4000-a000-000000000002")


async def _call(rows):
    result = MagicMock()
    result.all.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    out = await top_teams(period="2026-09", limit=5, client=None, _admin=MagicMock(), session=session)
    return out, session.execute.await_args.args[0]


async def test_rows_carry_the_team_uuid():
    rows = [
        SimpleNamespace(team_id=TEAM_A, name="Platform", cost_usd=10, call_count=3),
        SimpleNamespace(team_id=TEAM_B, name="Platform", cost_usd=5, call_count=1),  # 동명 팀
    ]
    out, _ = await _call(rows)
    assert [t["team_id"] for t in out["teams"]] == [str(TEAM_A), str(TEAM_B)]
    assert [t["name"] for t in out["teams"]] == ["Platform", "Platform"]


async def test_ties_are_broken_by_team_id():
    """같은 비용일 때 LIMIT 경계에서 잘리는 팀이 요청마다 바뀌지 않게 정렬을 고정한다."""
    _, stmt = await _call([])
    order = str(stmt).split("ORDER BY", 1)[1]
    assert "auth.teams.id" in order, order
