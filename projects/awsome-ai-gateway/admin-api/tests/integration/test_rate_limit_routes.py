# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""라우트 레벨: rate-limit 콘솔이 의존하는 두 경로가 실제로 배선돼 있는지.

  * ``DELETE /admin/rate-limits/user/{user_id}`` — 오버라이드 제거(새 경로). 관리자 전용,
    UUID 가 아니면 500 이 아니라 422.
  * ``GET /admin/rate-limits/usage/{scope}/{scope_id}`` — 이제 세션을 받아 ``measured`` 를
    판정한다. 라우트가 세션을 넘기지 않으면 서비스는 카운터가 잡힌 경우에만 측정됐다고
    말하므로(=한도가 있는데 0 인 사용자를 '측정 불가' 로 오표시) 배선 자체를 확인한다.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from httpx import AsyncClient

from app.core.db import SESSION_STATE_ATTR, get_db_session

USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


async def _no_keys(*_a, **_kw):
    if False:  # pragma: no cover - 빈 async generator
        yield None


async def test_delete_route_is_admin_only(client: AsyncClient, leader_headers: dict):
    resp = await client.delete(f"/admin/rate-limits/user/{USER_ID}", headers=leader_headers)
    assert resp.status_code == 403, resp.text


async def test_delete_route_rejects_a_non_uuid_with_422_not_500(
    client: AsyncClient, admin_headers: dict
):
    resp = await client.delete("/admin/rate-limits/user/not-a-uuid", headers=admin_headers)
    assert resp.status_code == 422, resp.text


async def test_delete_route_returns_the_delete_envelope(
    client: AsyncClient, admin_headers: dict, test_app
):
    # 캐시 무효화는 SCAN 을 돈다 — conftest 의 AsyncMock 은 async iterator 가 아니므로 빈 스캔을 준다.
    test_app.state.redis.scan_iter = MagicMock(side_effect=lambda *a, **kw: _no_keys())
    with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, patch(
        "app.services.rate_limit_service.audit_logger"
    ) as mock_audit:
        MockRepo.return_value.get_active = AsyncMock(return_value=None)
        MockRepo.return_value.deactivate_configs = AsyncMock(return_value=1)
        mock_audit.log = AsyncMock()
        resp = await client.delete(f"/admin/rate-limits/user/{USER_ID}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"scope": "USER", "scope_id": str(USER_ID), "deactivated": 1}
    MockRepo.return_value.deactivate_configs.assert_awaited_once()


async def test_usage_route_passes_the_session_for_the_measured_flag(
    client: AsyncClient, admin_headers: dict, test_app
):
    """한도 행이 있으면(세션으로 읽힌다) 카운터가 0 이어도 measured=true 여야 한다."""
    test_app.state.redis.scan_iter = MagicMock(side_effect=lambda *a, **kw: _no_keys())
    row = MagicMock(rpm_limit=60, model_alias=None)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [row]

    from app.core.db import get_db_session

    async def _session_with_limit():
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        yield session

    test_app.dependency_overrides[get_db_session] = _session_with_limit
    resp = await client.get(f"/admin/rate-limits/usage/USER/{USER_ID}", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["available"], body["measured"], body["limit_rpm"]) == (True, True, 60)
    assert body["rpm_used_total"] == 0


# ── 커밋 뒤에 무효화 ──────────────────────────────────────────────────────────
#
# 예전: 서비스가 upsert/비활성화 직후(커밋 전) rl:config:* 를 지웠고 커밋은 CommittingRoute 가
# 응답 직전에 했다. 그 사이 게이트웨이 요청의 캐시 미스는 **커밋된(=옛)** 행을 읽어 300초 다시
# 캐시한다 — 해제한 GLOBAL 한도·지운 개인 한도가 콘솔과 달리 5분 더 집행된다(실 PG+Redis 재현:
# tests/regression/test_high_rate_limit_invalidate_after_commit_real.py).

TEAM_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")


def _recording_session(events: list[str]):
    async def _override(request: Request):
        session = AsyncMock()
        session.info = {}
        session.add = MagicMock()
        session.scalar = AsyncMock(return_value="claude-opus-4-7")  # GLOBAL alias 존재 확인
        open_tx = {"value": True}
        session.in_transaction = MagicMock(side_effect=lambda: open_tx["value"])

        async def _commit():
            events.append("commit")
            open_tx["value"] = False

        session.commit = AsyncMock(side_effect=_commit)
        session.rollback = AsyncMock()
        setattr(request.state, SESSION_STATE_ATTR, session)
        yield session

    return _override


@pytest.mark.parametrize(
    "method, path, body, pattern",
    [
        ("PUT", f"/admin/rate-limits/user/{USER_ID}", {"rpm": 5}, f"rl:config:USER:{USER_ID}:*"),
        ("PUT", f"/admin/rate-limits/team/{TEAM_ID}", {"rpm": 5}, f"rl:config:TEAM:{TEAM_ID}:*"),
        ("PUT", "/admin/rate-limits/global/claude-opus-4-7", {"rpm": 5}, "rl:config:GLOBAL:NULL:*"),
        ("DELETE", f"/admin/rate-limits/user/{USER_ID}", None, f"rl:config:USER:{USER_ID}:*"),
    ],
)
async def test_mutations_invalidate_the_proxy_cache_only_after_commit(
    client: AsyncClient, admin_headers: dict, test_app, method, path, body, pattern
):
    events: list[str] = []
    test_app.dependency_overrides[get_db_session] = _recording_session(events)

    def _scan(*_a, **kw):
        events.append(f"scan:{kw.get('match') or _a[0]}")
        return _no_keys()

    test_app.state.redis.scan_iter = MagicMock(side_effect=_scan)
    test_app.state.redis.delete = AsyncMock(side_effect=lambda key: events.append(f"del:{key}"))

    with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, patch(
        "app.services.rate_limit_service.audit_logger"
    ) as mock_audit:
        MockRepo.return_value.upsert = AsyncMock()
        MockRepo.return_value.deactivate_configs = AsyncMock(return_value=1)
        MockRepo.return_value.get_active = AsyncMock(return_value=None)
        mock_audit.log = AsyncMock()
        resp = await client.request(method, path, json=body, headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert f"scan:{pattern}" in events, events  # 대조군 — 무효화 자체는 일어난다
    first_invalidation = min(i for i, e in enumerate(events) if e.startswith(("scan:", "del:")))
    assert "commit" in events[:first_invalidation], f"커밋 전에 캐시를 지웠다: {events}"


# ── GLOBAL 슬롯 해제는 명시적 동의가 필요하다 ────────────────────────────────


async def _put_global(client, headers, holder, query=""):
    with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, patch(
        "app.services.rate_limit_service.audit_logger"
    ) as mock_audit:
        MockRepo.return_value.upsert = AsyncMock()
        MockRepo.return_value.get_active = AsyncMock(return_value=holder)
        mock_audit.log = AsyncMock()
        resp = await client.put(
            f"/admin/rate-limits/global/claude-opus-4-7{query}", json={"rpm": 5}, headers=headers
        )
    return resp, MockRepo.return_value.upsert


async def test_global_save_that_would_release_another_model_is_409(
    client: AsyncClient, admin_headers: dict, test_app
):
    from app.core.db import get_db_session

    test_app.dependency_overrides[get_db_session] = _recording_session([])
    holder = MagicMock(model_alias="claude-sonnet-4-6", rpm_limit=None, tpm_limit=200000)

    resp, upsert = await _put_global(client, admin_headers, holder)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"
    assert "claude-sonnet-4-6" in resp.json()["error"]["message"]
    upsert.assert_not_awaited()

    test_app.state.redis.scan_iter = MagicMock(side_effect=lambda *a, **kw: _no_keys())
    resp, upsert = await _put_global(client, admin_headers, holder, "?release_alias=claude-sonnet-4-6")
    assert resp.status_code == 200, resp.text  # 대조군 — 주인을 명시하면 저장된다
    upsert.assert_awaited_once()
