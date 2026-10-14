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

from httpx import AsyncClient

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
