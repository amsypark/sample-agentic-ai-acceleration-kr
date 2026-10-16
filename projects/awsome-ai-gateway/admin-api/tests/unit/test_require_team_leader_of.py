# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``require_team_leader_of`` — 팀 없는 리더는 어떤 팀의 리더도 아니다.

검사가 ``user.team_id == team_id`` 뿐이었다. 팀 없는 리더(team_id None)를 ``None`` 팀에 대해 물으면
None == None → True 라 통과했다(예산 가드에서 고친 것과 같은 함정). 지금 이 의존성을 쓰는 라우트는 없지만,
쓰기 시작하는 순간 그 구멍이 열린다.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.core.auth import CurrentUser, require_team_leader_of
from app.models.auth import UserRole

TEAM_A = uuid.UUID("00000000-0000-4000-a000-0000000000a1")
TEAM_B = uuid.UUID("00000000-0000-4000-a000-0000000000b1")


def _user(role: UserRole, team_id: uuid.UUID | None) -> CurrentUser:
    return CurrentUser(user_id=uuid.uuid4(), email="user@example.com", role=role, team_id=team_id)


async def _check(team_id, user: CurrentUser) -> CurrentUser:
    return await require_team_leader_of(team_id)(user=user)


@pytest.mark.parametrize("team_id", [None, TEAM_A])
async def test_teamless_leader_is_never_a_teams_leader(team_id):
    with pytest.raises(HTTPException) as exc:
        await _check(team_id, _user(UserRole.TEAM_LEADER, None))
    assert exc.value.status_code == 403


async def test_leader_of_another_team_is_refused():
    with pytest.raises(HTTPException) as exc:
        await _check(TEAM_B, _user(UserRole.TEAM_LEADER, TEAM_A))
    assert exc.value.status_code == 403


async def test_developer_is_refused_even_for_the_own_team():
    with pytest.raises(HTTPException):
        await _check(TEAM_A, _user(UserRole.DEVELOPER, TEAM_A))


async def test_own_team_leader_and_admin_pass():
    """대조군 — 자기 팀의 리더와 관리자는 통과한다."""
    leader = _user(UserRole.TEAM_LEADER, TEAM_A)
    admin = _user(UserRole.ADMIN, None)
    assert await _check(TEAM_A, leader) is leader
    assert await _check(TEAM_B, admin) is admin
