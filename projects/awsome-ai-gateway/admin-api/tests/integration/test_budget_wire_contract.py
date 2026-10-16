# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""예산 화면이 읽고 쓰는 와이어 — HTTP 레이어를 통과시켜 고정한다.

  * ``GET /admin/my/budget`` — 개인 예산 행이 없으면 ``configured`` 가 **false** 이고, 이전 카드가 읽는
    limit/remaining/usage_pct/policy 는 예전 값(0 / 음수 / 0 / "HARD_BLOCK") 그대로다(하위 호환 — null 이면
    이전 카드가 `.toFixed()` 에서 죽는다). 콘솔의 MyBudgetCard 는 configured 로 '미설정' 을 그린다.
  * ``PUT /admin/budgets/user/{id}`` — ``max_budget_usd`` 가 NUMERIC(12,4) 를 넘으면 **422**
    (예전: 검증 통과 → asyncpg numeric overflow → 500). 컬럼이 실제로 그 값을 거부한다는 것은
    실 PG 로 test_high_console_contract_real_db.py 가 증명한다.
  * ``GET /admin/budgets/team/{id}/allocation`` — ``*_usd`` 는 pydantic Decimal 이라 JSON
    **문자열**이다. admin-ui TeamAllocationView 가 이를 number 로 믿고 ``.toFixed()`` 를 불러
    렌더가 죽었다(타입을 문자열로 정정하고 소비 지점에서 Number() 로 바꾼다).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from app.schemas.budgets import AllocationEntry, TeamBudgetAllocation

TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
MEMBER_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")


async def test_my_budget_without_a_row_is_unset_and_keeps_the_legacy_numbers(client: AsyncClient, dev_headers: dict):
    # conftest 의 mock DB: scalar_one_or_none → None(예산 행 없음), scalar_one → 0(지출)
    resp = await client.get("/admin/my/budget", headers=dev_headers)
    assert resp.status_code == 200, resp.text
    b = resp.json()["budget"]
    assert b["configured"] is False
    # 와이어(JSON)에서도 이전 카드가 `.toFixed()` 하는 넷은 숫자다 — null 이 아니다.
    assert (b["limit_usd"], b["remaining_usd"], b["usage_pct"], b["policy"]) == (0, 0, 0, "HARD_BLOCK")
    assert b["used_usd"] == 0  # 실지출은 숫자로 남는다
    # conftest 의 mock DB 에서 사용자의 팀도 None — 팀 미배정은 게이트웨이가 거부하는 상태다.
    assert resp.json()["team"] == {"assigned": False, "budget_configured": False, "budget_exhausted": False}


async def test_budget_over_the_numeric_column_is_a_422(client: AsyncClient, admin_headers: dict):
    resp = await client.put(
        f"/admin/budgets/user/{MEMBER_ID}",
        json={"max_budget_usd": "100000000", "policy": "HARD_BLOCK"},
        headers=admin_headers,
    )
    assert resp.status_code == 422, resp.text
    assert "max_budget_usd" in resp.text  # 어느 필드가 문제인지 나와야 한다


async def test_budget_at_the_column_ceiling_passes_validation(client: AsyncClient, admin_headers: dict):
    """대조군 — 상한(99,999,999.9999)은 스키마를 통과한다(그 뒤 서비스가 사용자 부재로 404)."""
    resp = await client.put(
        f"/admin/budgets/user/{MEMBER_ID}",
        json={"max_budget_usd": "99999999.9999", "policy": "HARD_BLOCK"},
        headers=admin_headers,
    )
    assert resp.status_code != 422, resp.text


async def test_allocation_decimals_are_json_strings(client: AsyncClient, admin_headers: dict):
    allocation = TeamBudgetAllocation(
        team_id=str(TEAM_ID),
        team_name="Platform",
        total_budget_usd=Decimal("100.0000"),
        entries=[
            AllocationEntry(
                target_id=str(TEAM_ID),
                target_name="Platform",
                target_type="TEAM",
                allocated_usd=Decimal("100.0000"),
                used_usd=Decimal("12.5000"),
                remaining_usd=Decimal("87.5000"),
                alert_level="NORMAL",
            ),
            AllocationEntry(
                target_id=str(MEMBER_ID),
                target_name="Alice",
                target_type="USER",
                allocated_usd=Decimal("60.0000"),
                used_usd=Decimal("10.0000"),
                remaining_usd=Decimal("50.0000"),
                alert_level="NORMAL",
            ),
        ],
    )
    with patch(
        "app.services.budget_service.BudgetService.get_team_allocation",
        AsyncMock(return_value=allocation),
    ):
        resp = await client.get(f"/admin/budgets/team/{TEAM_ID}/allocation", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_budget_usd"] == "100.0000"
    for entry in body["entries"]:
        for field in ("allocated_usd", "used_usd", "remaining_usd"):
            assert isinstance(entry[field], str), (field, entry)
    # 읽기는 target_id 이고 쓰기(PUT /allocate)는 user_id 다 — 콘솔이 이름을 바꿔 보낸다
    # (admin-ui lib/actions/budgets.ts). TEAM 합계 행이 첫 줄에 온다는 것도 콘솔의 전제다.
    assert body["entries"][0]["target_type"] == "TEAM"
