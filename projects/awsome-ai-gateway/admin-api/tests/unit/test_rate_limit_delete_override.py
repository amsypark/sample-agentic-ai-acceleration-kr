# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``DELETE /admin/rate-limits/user/{user_id}`` — USER 오버라이드 제거(상속 복귀).

왜 이 경로가 필요한가:
    PUT 은 **전체 교체**다(``RateLimitConfigRepository.upsert``: 기존 활성 행 비활성화 +
    새 행 INSERT). 그래서 "입력을 모두 비우고 저장" 하면 전부 NULL 인 USER 행이 남는다.
    집행상으로는 행이 없는 것과 같지만(전부 None → 스코프 스킵), 트리에서는 ``config`` 가
    채워진 노드로 보이고 ``inherited_from`` 이 사라진다 — 팀 한도가 엄연히 걸려 있는데도
    콘솔은 "이 사용자 무제한" 처럼 읽힌다. 되돌릴 방법이 라우터에 없었다
    (GET tree / GET usage / PUT 3개뿐). ``deactivate_configs`` 는 이미 있었고 사용자
    비활성화 경로만 쓰고 있었다. 실제 행 상태 변화는 실 PG 로
    test_high_console_contract_real_db.py 가 증명한다.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.models.model import RateLimitScope
from app.services.rate_limit_service import RateLimitService

USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


async def delete_override(svc, session, actor, *, deactivated: int = 1):
    with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, patch(
        "app.services.rate_limit_service.audit_logger"
    ) as mock_audit:
        MockRepo.return_value.deactivate_configs = AsyncMock(return_value=deactivated)
        mock_audit.log = AsyncMock()
        result = await svc.delete_user_rate_limit(
            session, user_id=USER_ID, actor=actor, ip_address="10.0.0.1", request_id="req-1"
        )
        return result, MockRepo.return_value.deactivate_configs, mock_audit.log


class TestDeleteUserOverride:
    async def test_deactivates_only_the_user_scope_rows(
        self, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        svc = RateLimitService(cache_mgr=CacheInvalidationManager(mock_redis))
        result, deactivate, _ = await delete_override(svc, mock_session, admin_user)

        deactivate.assert_awaited_once_with(RateLimitScope.USER, USER_ID)
        assert (result.scope, result.scope_id, result.deactivated) == ("USER", str(USER_ID), 1)

    async def test_clears_proxy_snapshot_and_policy_cache(
        self, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        """제거 후에도 gateway-proxy 가 5분 TTL 동안 옛 정책을 서빙하면 제거가 무효다.
        ``ratelimit:config:user:*`` 는 _set_rate_limit 이 심는 스냅샷 키다."""
        cached = [f"rl:config:USER:{USER_ID}:claude-opus"]

        def _scan(*_a, **_kw):
            async def _gen():
                for k in cached:
                    yield k

            return _gen()

        mock_redis.scan_iter = MagicMock(side_effect=_scan)
        svc = RateLimitService(cache_mgr=CacheInvalidationManager(mock_redis))

        await delete_override(svc, mock_session, admin_user)

        deleted = [c.args[0] for c in mock_redis.delete.call_args_list]
        assert f"ratelimit:config:user:{USER_ID}" in deleted
        assert cached[0] in deleted
        match_arg = mock_redis.scan_iter.call_args.kwargs.get("match") or (
            mock_redis.scan_iter.call_args.args[0]
        )
        assert match_arg == f"rl:config:USER:{USER_ID}:*"

    async def test_is_audited(
        self, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        svc = RateLimitService(cache_mgr=CacheInvalidationManager(mock_redis))
        _, _, audit_log = await delete_override(svc, mock_session, admin_user)

        audit_log.assert_awaited_once()
        kwargs = audit_log.await_args.kwargs
        assert kwargs["action"] == "DELETE_RATE_LIMIT"
        assert kwargs["resource_id"] == str(USER_ID)
        assert kwargs["changes"]["after"]["deactivated"] == 1

    async def test_no_existing_override_is_not_an_error(
        self, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        """멱등 — 이미 상속 중인 사용자에게 눌러도 200 + deactivated=0."""
        svc = RateLimitService(cache_mgr=CacheInvalidationManager(mock_redis))
        result, _, _ = await delete_override(svc, mock_session, admin_user, deactivated=0)
        assert result.deactivated == 0

