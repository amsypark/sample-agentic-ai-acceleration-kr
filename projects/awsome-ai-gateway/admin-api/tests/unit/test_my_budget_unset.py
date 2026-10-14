# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/my/budget`` — 개인 예산 행이 없으면 '미설정' 이다, '$0 하드 차단' 이 아니다.

예전에는 행이 없을 때 ``limit_usd=0`` · ``policy="HARD_BLOCK"`` 으로 접었다. 그래서 지출이
있는 사용자(OIDC 자동 프로비저닝 사용자는 개인 예산 행이 없는 것이 정상)에게 콘솔이
"한도 $0 / 잔여 $-N / 하드 차단" 을 보였다. 게이트웨이는 같은 사용자를 **통과**시킨다
(gateway-proxy budget_service: "Q 정책: USER 예산 미설정 → pass-through") — 콘솔만 거짓말을 했다.

여기서 고정하는 3-state:
  * 설정됨   — limit/remaining/usage_pct/policy 가 숫자·문자열
  * 미설정   — 그 넷이 null, used_usd 는 여전히 숫자(실지출)
  * 한도 0   — "미설정" 과 다르다: 0 은 실재하는 한도라 숫자로 나간다
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.models.budget import BudgetPolicy
from app.routers.my import get_my_budget


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        email="dev@example.com",
        role=UserRole.DEVELOPER,
        team_id=None,
    )


def _session(config, used: str) -> AsyncMock:
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = config
    used_result = MagicMock()
    used_result.scalar_one.return_value = Decimal(used)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[cfg_result, used_result])
    return session


async def _budget(config, used="12.34") -> dict:
    out = await get_my_budget(request=MagicMock(), user=_user(), session=_session(config, used))
    return out["budget"]


async def test_no_budget_row_is_reported_as_unset_not_hard_block():
    b = await _budget(None)
    assert b["limit_usd"] is None, "없는 한도를 $0 으로 지어냈다"
    assert b["remaining_usd"] is None, "없는 한도로 음수 잔여를 계산했다"
    assert b["usage_pct"] is None
    assert b["policy"] is None, "게이트웨이가 통과시키는 사용자를 HARD_BLOCK 으로 표시했다"
    # 실지출은 미설정이어도 사실이다 — 숫자로 남는다.
    assert b["used_usd"] == 12.34


async def test_configured_budget_is_reported_as_numbers():
    """대조군 — 행이 있으면 예전과 같은 숫자다."""
    cfg = SimpleNamespace(max_budget_usd=Decimal("100.00"), policy=BudgetPolicy.SOFT_WARNING)
    b = await _budget(cfg, used="25.00")
    assert b == {
        "limit_usd": 100.0,
        "used_usd": 25.0,
        "remaining_usd": 75.0,
        "usage_pct": 25.0,
        "policy": "SOFT_WARNING",
    }


async def test_zero_limit_is_a_real_limit_not_unset():
    """0 원 한도는 '미설정' 과 다른 사실이다 — null 로 접으면 실재하는 차단을 숨긴다."""
    cfg = SimpleNamespace(max_budget_usd=Decimal("0"), policy=BudgetPolicy.HARD_BLOCK)
    b = await _budget(cfg, used="1.00")
    assert b["limit_usd"] == 0
    assert b["usage_pct"] == 0.0
    assert b["remaining_usd"] == -1.0
    assert b["policy"] == "HARD_BLOCK"
