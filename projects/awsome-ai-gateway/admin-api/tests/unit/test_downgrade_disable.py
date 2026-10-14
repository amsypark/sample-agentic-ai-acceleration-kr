# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``PUT /admin/budgets/{scope}/{id}/downgrade`` 의 ``enabled: false`` 는 실제로 끈다.

예전에는 ``enabled`` 를 감사 로그 after 에만 적고 규칙을 전부 ``is_active=True`` 로 다시 썼다.
admin-ui(AutoDowngradeConfig)는 체크를 끈 상태에서도 규칙과 함께 PUT 하므로, 운영자는 200 + 성공
토스트를 받았지만 gateway-proxy 는 임계값에서 계속 모델을 바꿔치기했다. 행 상태는 실 PG 로
test_high_console_contract_real_db.py 가 증명한다.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.models.budget import BudgetScope
from app.schemas.budgets import AutoDowngradeConfigRequest, AutoDowngradeConfigResponse
from app.services.budget_service import BudgetService

TEAM_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
ADMIN = CurrentUser(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
    email="admin@example.com",
    role=UserRole.ADMIN,
    team_id=None,
)
RULES = [{"from_model_alias": "does-not-exist", "to_model_alias": "also-missing", "threshold_pct": 80}]


def _svc():
    cache = MagicMock()
    cache.invalidate = AsyncMock()
    return BudgetService(cache_mgr=cache), cache


async def _set(enabled: bool):
    svc, cache = _svc()
    off = AutoDowngradeConfigResponse(scope="TEAM", scope_id=str(TEAM_ID), enabled=False, rules=[])
    with patch("app.services.budget_service.DowngradePolicyRepository") as Rules, patch(
        "app.services.budget_service.audit_logger"
    ) as audit, patch.object(svc, "get_downgrade_config", AsyncMock(return_value=off)):
        Rules.return_value.get_rules = AsyncMock(return_value=[
            MagicMock(from_model_alias="claude-opus-4-7", to_model_alias="claude-sonnet-4-6", threshold_pct=80),
            MagicMock(from_model_alias="claude-sonnet-4-6", to_model_alias="claude-haiku-4-5", threshold_pct=95),
        ])
        Rules.return_value.delete_rules = AsyncMock(return_value=2)
        Rules.return_value.set_rules = AsyncMock()
        audit.log = AsyncMock()
        try:
            out = await svc.set_downgrade_config(
                AsyncMock(),
                scope=BudgetScope.TEAM,
                scope_id=TEAM_ID,
                data=AutoDowngradeConfigRequest(enabled=enabled, rules=RULES),
                actor=ADMIN,
            )
        except Exception as exc:  # noqa: BLE001 - 대조군이 검증 예외를 본다
            out = exc
    return out, Rules.return_value, audit.log, cache


async def test_disable_deactivates_the_rules_instead_of_rewriting_them_active():
    out, rules, audit_log, cache = await _set(enabled=False)
    rules.delete_rules.assert_awaited_once_with(BudgetScope.TEAM, TEAM_ID)
    rules.set_rules.assert_not_awaited()
    assert out.enabled is False
    # gateway-proxy 가 캐시된 규칙으로 계속 바꿔치기하지 않게 같은 키를 지운다.
    assert cache.invalidate.await_args.args[0] == [f"budget:downgrade:team:{TEAM_ID}"]
    after = audit_log.await_args.kwargs["changes"]["after"]
    assert after == {"enabled": False, "rules": [], "deactivated": 2}, "감사 기록이 DB 와 다른 상태를 주장한다"


async def test_disable_audits_the_rules_it_removed():
    """끈 뒤에는 규칙이 DB 에 활성으로 남지 않는다 — 감사 기록이 무엇을 껐는지 담아야 되돌릴 수 있다."""
    _, _, audit_log, _ = await _set(enabled=False)
    before = audit_log.await_args.kwargs["changes"]["before"]
    assert before == {"rules": [
        {"from_model_alias": "claude-opus-4-7", "to_model_alias": "claude-sonnet-4-6", "threshold_pct": 80},
        {"from_model_alias": "claude-sonnet-4-6", "to_model_alias": "claude-haiku-4-5", "threshold_pct": 95},
    ]}


async def test_disable_works_even_when_saved_aliases_went_invalid():
    """끄는 경로는 alias 검증을 하지 않는다 — 위 RULES 의 alias 는 존재하지 않는다."""
    out, _, _, _ = await _set(enabled=False)
    assert not isinstance(out, Exception), out


async def test_enable_still_validates_and_sets_rules():
    """대조군 — enabled=true 경로는 그대로 alias 를 검증한다(존재하지 않으면 NotFound)."""
    from app.core.exceptions import NotFoundError

    with patch("app.repositories.model_repository.ModelRepository") as Models:
        Models.return_value.get_by_alias = AsyncMock(return_value=None)
        out, rules, _, _ = await _set(enabled=True)
    assert isinstance(out, NotFoundError), out
    rules.delete_rules.assert_not_awaited()
