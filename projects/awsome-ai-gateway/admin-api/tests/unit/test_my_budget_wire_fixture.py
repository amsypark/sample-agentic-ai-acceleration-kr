# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/my/budget`` — 배포 순서가 섞여도 두 카드가 모두 동작하게 하는 와이어 계약.

이 응답을 읽는 것은 admin-ui 의 내 예산 카드 하나다(gateway-cli 상태줄은 gateway-proxy 의 /v1/usage/me 를
읽는다). 그 카드가 두 버전 공존한다 — admin-api 를 먼저 배포하는 동안에도, helm 한 번의 롤에서도 몇 분은
이전 카드 × 새 API 와 새 카드 × 이전 API 가 섞인다.

  * **이전 카드 × 새 API**(목표 순서: admin-api → admin-ui). 이전 카드는 budget 의 limit_usd·used_usd·
    remaining_usd·usage_pct 를 그대로 ``.toFixed()`` 하고 policy 로 라벨을 찾는다. 한때 개인 예산이 없거나 $0
    이면 이 값들을 null 로 보냈고, 이전 카드가 TypeError 로 통째로 죽었다. → 모든 상태에서 숫자·문자열이고,
    이전 API(main)가 알던 상태에서는 **main 과 같은 값**이어야 한다. 새 의미는 더하는 필드로만 낸다.
  * **새 카드 × 이전 API**. admin-ui 쪽 테스트가 같은 픽스처의 ``main`` 응답으로 검증한다.

픽스처 ``admin-ui/tests/fixtures/my-budget.json``:
  * ``main`` — main(e7a1588)의 admin-api 가 **실제로 보내던** 응답. 그 라우트 코드를 그대로 돌려 만들었고, 같은
    상태를 실 PostgreSQL 행으로 돌린 결과와 같았다. main 의 코드는 이 브랜치에 없으므로 이 파일은 고정 기록이다.
  * ``head`` — 지금 이 라우트의 응답. 아래 테스트가 **완전 일치**를 단정한다 — admin-ui 테스트가 손으로 베낀
    모양이 아니라 서버가 실제로 내보내는 모양을 쓰게 하려는 것이다(rate-limit 트리 픽스처와 같은 규약).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.models.budget import BudgetPolicy
from app.routers.my import get_my_budget

PROJECT = Path(__file__).resolve().parents[3]
UI_FIXTURE = PROJECT / "admin-ui" / "tests" / "fixtures" / "my-budget.json"
PERIOD = "2026-10"
TEAM = uuid.UUID("77777777-7777-4777-8777-777777777777")

# 상태 → 라우트가 DB 에서 읽는 값. (한도, 정책) 은 활성 예산 행, None 은 행 없음.
_TEAM_OK = {"team_id": TEAM, "team_budget": ("5000", "HARD_BLOCK"), "team_used": "0"}
STATES: dict[str, dict] = {
    "no_budget": {"config": None, "used": "12.34", **_TEAM_OK},
    "zero_hard_block": {"config": ("0", "HARD_BLOCK"), "used": "1.00", **_TEAM_OK},
    "zero_throttle": {"config": ("0", "THROTTLE"), "used": "0", **_TEAM_OK},
    "zero_soft_warning": {"config": ("0", "SOFT_WARNING"), "used": "0", **_TEAM_OK},
    "normal": {"config": ("100", "SOFT_WARNING"), "used": "25.00", **_TEAM_OK},
    "over_limit": {"config": ("50", "HARD_BLOCK"), "used": "60.00", **_TEAM_OK},
    "teamless": {"config": None, "used": "3.21", "team_id": None, "team_budget": None, "team_used": None},
    "team_without_budget": {"config": None, "used": "0", "team_id": TEAM, "team_budget": None, "team_used": None},
    "team_exhausted": {"config": None, "used": "0", "team_id": TEAM, "team_budget": ("100", "HARD_BLOCK"),
                       "team_used": "100"},
}
# main 의 응답에는 팀 정보가 없다 — main 이 구분하던 것은 개인 예산 상태뿐이다.
MAIN_STATES = ("no_budget", "zero_hard_block", "zero_throttle", "zero_soft_warning", "normal", "over_limit")
# 이전 카드가 그대로 `.toFixed()` 하는 값들.
DEREFERENCED_NUMBERS = ("limit_usd", "used_usd", "remaining_usd", "usage_pct")


def user_for(state: str) -> CurrentUser:
    index = list(STATES).index(state) + 1
    return CurrentUser(
        user_id=uuid.UUID(f"44444444-4444-4444-8444-{index:012x}"),
        email="me@example.com",
        role=UserRole.DEVELOPER,
        team_id=None,
    )


def _row(pair):
    if pair is None:
        return None
    limit, policy = pair
    return SimpleNamespace(max_budget_usd=Decimal(limit), policy=BudgetPolicy[policy])


def _result(**attrs) -> MagicMock:
    r = MagicMock()
    for name, value in attrs.items():
        getattr(r, name).return_value = value
    return r


def head_session(state: str) -> AsyncMock:
    """지금 라우트의 조회 순서: 개인 예산 행 → 이번 달 지출 → 사용자의 팀 → 팀 예산 행 → 팀 사용액(DB 경로)."""
    s = STATES[state]
    results = [
        _result(scalar_one_or_none=_row(s["config"])),
        _result(scalar_one=Decimal(s["used"])),
        _result(scalar_one_or_none=s["team_id"]),
    ]
    if s["team_id"] is not None:
        results.append(_result(scalar_one_or_none=_row(s["team_budget"])))
        if s["team_budget"] is not None:
            results.append(_result(scalar_one=Decimal(s["team_used"])))
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    return session


def no_redis_request() -> MagicMock:
    request = MagicMock()
    request.app.state.redis = None  # 팀 사용액은 DB 경로(budget_usages)로 — 픽스처는 Redis 상태와 무관하다
    return request


async def head_response(state: str) -> dict:
    with patch("app.routers.my.current_kst_period", return_value=PERIOD):
        out = await get_my_budget(request=no_redis_request(), user=user_for(state), session=head_session(state))
    return json.loads(json.dumps(out))  # 와이어로 나가는 JSON 그대로


def _fixture() -> dict:
    if not UI_FIXTURE.exists():
        pytest.skip(f"sibling admin-ui not in this checkout: {UI_FIXTURE}")
    return json.loads(UI_FIXTURE.read_text(encoding="utf-8"))


async def test_wire_matches_admin_ui_fixture():
    expected = _fixture()["head"]
    assert set(expected) == set(STATES), "픽스처와 상태 목록이 어긋난다"
    for state in STATES:
        assert await head_response(state) == expected[state], (
            f"{state}: 와이어 모양이 바뀌었다. admin-ui 는 이 JSON 을 픽스처로 그대로 읽는다 — 서버를 되돌리거나 "
            f"{UI_FIXTURE.relative_to(PROJECT)} 를 갱신하고 admin-ui 테스트를 함께 통과시켜라."
        )


@pytest.mark.parametrize("state", list(STATES))
async def test_fields_the_previous_card_dereferences_are_never_null(state):
    """이전 카드는 이 넷을 `.toFixed()` 한다 — 하나라도 null 이면 admin-api 를 먼저 배포한 동안 카드가 죽는다."""
    budget = (await head_response(state))["budget"]
    for field in DEREFERENCED_NUMBERS:
        value = budget[field]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{state}.{field} = {value!r}"
    assert isinstance(budget["policy"], str) and budget["policy"], f"{state}.policy = {budget['policy']!r}"


@pytest.mark.parametrize("state", MAIN_STATES)
async def test_legacy_fields_keep_mains_values(state):
    """main 이 알던 상태에서 이전 필드는 main 과 **같은 값**이다 — 새 의미(configured, team)는 더하는 필드로만."""
    main = _fixture()["main"][state]
    head = await head_response(state)
    assert set(main) <= set(head), f"main 의 최상위 키가 사라졌다: {set(main) - set(head)}"
    assert {k: head["budget"][k] for k in main["budget"]} == main["budget"], state
    assert {k: head[k] for k in main if k != "budget"} == {k: v for k, v in main.items() if k != "budget"}


async def test_unset_is_carried_by_the_additive_field():
    """미설정과 진짜 $0 하드 차단은 이전 필드로는 같아 보인다 — 그 차이는 configured 만 안다."""
    unset = (await head_response("no_budget"))["budget"]
    zero = (await head_response("zero_hard_block"))["budget"]
    assert (unset["limit_usd"], unset["policy"], unset["usage_pct"]) == (0, "HARD_BLOCK", 0)
    assert (zero["limit_usd"], zero["policy"], zero["usage_pct"]) == (0, "HARD_BLOCK", 0)
    assert (unset["configured"], zero["configured"]) == (False, True)
