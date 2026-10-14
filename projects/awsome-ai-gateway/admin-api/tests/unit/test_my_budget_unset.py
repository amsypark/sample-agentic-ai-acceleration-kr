# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/my/budget`` — 개인 예산 행이 없으면 '미설정' 이다, '$0 하드 차단' 이 아니다.

행이 없을 때 응답은 ``limit_usd=0`` · ``policy="HARD_BLOCK"`` 이다. 지출이 있는 사용자(OIDC 자동 프로비저닝
사용자는 개인 예산 행이 없는 것이 정상)에게 그 값을 그대로 그리면 "한도 $0 / 잔여 $-N / 하드 차단" 이 된다.
게이트웨이는 같은 사용자를 **통과**시킨다(gateway-proxy budget_service: "Q 정책: USER 예산 미설정 →
pass-through"). 그래서 '미설정' 은 더하는 필드 ``configured`` 로 낸다.

⚠️ 그 0 · HARD_BLOCK 은 그대로 둔다(하위 호환). 이전 카드가 그 넷을 ``.toFixed()`` 한다 — null 로 바꾸면
   admin-api 를 먼저 배포한 동안 카드가 죽는다(test_my_budget_wire_fixture.py 가 main 의 실제 응답과 대조한다).

여기서 고정하는 3-state:
  * 설정됨   — configured=true, limit/remaining/usage_pct/policy 는 그 행의 값
  * 미설정   — configured=false, 이전 값(0 · HARD_BLOCK · usage_pct 0 · remaining=-used), used_usd 는 실지출
  * 한도 0   — "미설정" 과 다르다: configured=true 이고 0 은 실재하는 한도다
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


TEAM_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")


def _result(**attrs) -> MagicMock:
    r = MagicMock()
    for name, value in attrs.items():
        getattr(r, name).return_value = value
    return r


TEAM_BUDGET = SimpleNamespace(max_budget_usd=Decimal("5000"), policy=BudgetPolicy.HARD_BLOCK)


def _session(
    config, used: str, *, team_id=TEAM_ID, team_budget=TEAM_BUDGET, team_used: str = "0"
) -> AsyncMock:
    """조회 순서: 개인 예산 행 → 이번 달 지출 → 사용자의 팀 → (팀이 있으면) 팀 활성 예산 행
    → (팀 예산이 있으면) 이번 달 팀 사용액(budget_usages)."""
    results = [
        _result(scalar_one_or_none=config),
        _result(scalar_one=Decimal(used)),
        _result(scalar_one_or_none=team_id),
    ]
    if team_id is not None:
        results.append(_result(scalar_one_or_none=team_budget))
        if team_budget is not None:
            results.append(_result(scalar_one=Decimal(team_used)))
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    return session


def _period() -> str:
    from app.core.usage_filters import current_kst_period

    return current_kst_period()


def _db_request() -> MagicMock:
    request = MagicMock()
    request.app.state.redis = None  # 팀 사용액은 DB 경로(budget_usages)
    return request


async def _my(config, used="12.34", **team) -> dict:
    return await get_my_budget(request=_db_request(), user=_user(), session=_session(config, used, **team))


async def _budget(config, used="12.34") -> dict:
    return (await _my(config, used))["budget"]


async def test_no_budget_row_is_reported_as_unset_not_hard_block():
    b = await _budget(None)
    assert b["configured"] is False, "개인 예산 행이 없는 사용자를 설정됨으로 보냈다"
    # 이전 카드용 값은 예전 그대로다 — 숫자·문자열이어야 이전 카드가 죽지 않는다(하위 호환).
    assert (b["limit_usd"], b["remaining_usd"], b["usage_pct"], b["policy"]) == (0, -12.34, 0, "HARD_BLOCK")
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
        "configured": True,
    }


async def test_zero_limit_is_a_real_limit_not_unset():
    """0 원 한도는 '미설정' 과 다른 사실이다 — HARD_BLOCK 의 $0 한도는 모든 요청을 막는다(used >= 0).

    ⚠️ 이전 필드만 보면 미설정과 **같다**(limit 0 · HARD_BLOCK · usage_pct 0). 사용률은 정의되지 않지만 이전
       카드가 숫자를 기대해 0 을 보낸다 — 새 카드는 configured=true · limit 0 을 보고 '--' 와 차단을 그린다."""
    cfg = SimpleNamespace(max_budget_usd=Decimal("0"), policy=BudgetPolicy.HARD_BLOCK)
    b = await _budget(cfg, used="1.00")
    assert b["configured"] is True
    assert (b["limit_usd"], b["usage_pct"], b["remaining_usd"], b["policy"]) == (0, 0, -1.0, "HARD_BLOCK")


# ── 팀 예산 — 게이트웨이는 팀 예산이 없으면 거부한다(C-1, team_budget_unset) ─────────


async def test_teamless_user_is_reported_as_without_a_team():
    """팀 미배정 사용자에게 "팀 예산 적용" 은 거짓이다 — 게이트웨이는 팀 예산이 없으니 거부한다."""
    out = await _my(None, team_id=None)
    assert out["team"] == {"assigned": False, "budget_configured": False, "budget_exhausted": False}


async def test_team_without_a_budget_is_reported():
    out = await _my(None, team_budget=None)
    assert out["team"] == {"assigned": True, "budget_configured": False, "budget_exhausted": False}


async def test_team_with_a_budget_is_reported_as_configured():
    """대조군 — 팀 예산이 있고 남아 있으면 그대로 적용된다."""
    out = await _my(None)
    assert out["team"] == {"assigned": True, "budget_configured": True, "budget_exhausted": False}


# ── 팀 예산 소진 — 게이트웨이의 팀 계층 거부 규칙(redis_scripts/budget_check.lua) 그대로 ──────────
#
# 팀 예산이 **있어도** 게이트웨이가 거부하는 상태가 있다. 카드가 그때 아무 말도 하지 않으면 모든 요청이
# 거부되는 사용자에게 쓸 수 있는 것처럼 보인다. 규칙은 실 Redis 에서 Lua 를 돌려 확인한 그대로다:
#   HARD_BLOCK   used >= limit                 ($0 은 항상 거부)
#   SOFT_WARNING used * 100 >= limit * 110     ($0 은 항상 거부 — Lua. DB 폴백만 limit > 0 이라 통과)
#   THROTTLE     거부하지 않는다(속도만 줄인다) — 소진이어도 false


@pytest.mark.parametrize(
    ("policy", "limit", "team_used", "exhausted"),
    [
        (BudgetPolicy.HARD_BLOCK, "100", "99.99", False),
        (BudgetPolicy.HARD_BLOCK, "100", "100", True),
        (BudgetPolicy.HARD_BLOCK, "0", "0", True),
        (BudgetPolicy.SOFT_WARNING, "100", "109.99", False),
        (BudgetPolicy.SOFT_WARNING, "100", "110", True),
        (BudgetPolicy.SOFT_WARNING, "0", "0", True),
        # float64 경계 — Lua: 0.011*100 = 1.0999999999999999 < 0.01*110 = 1.1 → 통과(십진수로는 '소진' 이 된다)
        (BudgetPolicy.SOFT_WARNING, "0.01", "0.0110", False),
        (BudgetPolicy.SOFT_WARNING, "0.01", "0.0111", True),
        (BudgetPolicy.THROTTLE, "100", "500", False),
        (BudgetPolicy.THROTTLE, "0", "0", False),
    ],
)
async def test_team_budget_exhausted_follows_the_gateway_deny_rule(policy, limit, team_used, exhausted):
    team_budget = SimpleNamespace(max_budget_usd=Decimal(limit), policy=policy)
    out = await _my(None, team_budget=team_budget, team_used=team_used)
    assert out["team"]["budget_configured"] is True, "소진이어도 예산 행은 있다 — '미설정' 이라고 말하면 거짓이다"
    assert out["team"]["budget_exhausted"] is exhausted


# ── 팀 사용액의 원천 — 게이트웨이가 판정에 쓰는 Redis 카운터(gateway-proxy 가 요청마다 INCRBYFLOAT) ──────────
#
# budget_usages 는 cost-recorder-worker 가 배치로 쌓아 카운터보다 늦다. 게이트웨이는 카운터로 거부하므로, DB 만
# 보면 이미 거부되는 팀을 '여유 있음' 으로 보인다. 키가 없으면 게이트웨이는 0 으로 읽고(팀 카운터는 DB 에서
# 복원하지 않는다) 통과시킨다. Redis 를 쓸 수 없을 때만 budget_usages 로 판정한다(게이트웨이의 DB 폴백과 같은 행).

TEAM_100 = SimpleNamespace(max_budget_usd=Decimal("100"), policy=BudgetPolicy.HARD_BLOCK)


class _FakeRedis:
    def __init__(self, values: dict[str, str] | None = None, *, down: bool = False):
        self.values = values or {}
        self.down = down
        self.keys: list[str] = []

    async def get(self, key: str):
        self.keys.append(key)
        if self.down:
            raise ConnectionError("redis is down")
        return self.values.get(key)


def _redis_session(*, team_used_in_db: str | None) -> AsyncMock:
    """팀 사용액 SELECT 가 나가면(DB 경로) team_used_in_db 를 돌려준다 — Redis 로 판정하면 그 결과는 안 쓰인다."""
    results = [
        _result(scalar_one_or_none=None),
        _result(scalar_one=Decimal("0")),
        _result(scalar_one_or_none=TEAM_ID),
        _result(scalar_one_or_none=TEAM_100),
    ]
    if team_used_in_db is not None:
        results.append(_result(scalar_one=Decimal(team_used_in_db)))
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    return session


async def _team_with(redis, *, team_used_in_db: str | None) -> dict:
    request = MagicMock()
    request.app.state.redis = redis
    out = await get_my_budget(request=request, user=_user(), session=_redis_session(team_used_in_db=team_used_in_db))
    return out["team"]


async def test_worker_lag_the_gateway_counter_is_ahead_of_budget_usages():
    """카운터 100(게이트웨이는 이미 거부) · budget_usages 50(워커가 아직 안 쌓음) → 소진이다."""
    redis = _FakeRedis({f"budget:team:{{{TEAM_ID}}}:" + _period(): "100"})
    team = await _team_with(redis, team_used_in_db="50")
    assert team["budget_exhausted"] is True, "워커가 늦게 쌓은 budget_usages 로 판정했다"
    assert redis.keys == [f"budget:team:{{{TEAM_ID}}}:" + _period()]


async def test_a_missing_counter_reads_as_zero_like_the_gateway():
    """키가 없으면 게이트웨이는 0 으로 읽고 통과시킨다 — DB 가 소진이라고 해도 카드는 게이트웨이를 따른다."""
    team = await _team_with(_FakeRedis({}), team_used_in_db="150")
    assert team["budget_exhausted"] is False, "게이트웨이는 0 으로 읽는 팀을 budget_usages 로 소진이라고 했다"


async def test_redis_down_falls_back_to_budget_usages():
    """Redis 를 쓸 수 없으면 게이트웨이처럼 budget_usages 로 판정한다."""
    assert (await _team_with(_FakeRedis(down=True), team_used_in_db="100"))["budget_exhausted"] is True
    assert (await _team_with(_FakeRedis(down=True), team_used_in_db="50"))["budget_exhausted"] is False


async def test_no_redis_client_uses_budget_usages():
    """대조군 — Redis 가 없는 배포(app.state.redis None)는 DB 경로다."""
    assert (await _team_with(None, team_used_in_db="100"))["budget_exhausted"] is True
