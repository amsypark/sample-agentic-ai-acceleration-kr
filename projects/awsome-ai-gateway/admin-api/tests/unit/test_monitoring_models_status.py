# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/monitoring/models`` 의 status 는 **등록부의 실제 값**이다(예전: 리터럴 "ACTIVE").

리터럴이던 동안 콘솔 상태 열은 항상 초록 ACTIVE 였다 — 방금 INACTIVE 로 내려 gateway-proxy 가
전면 거부하는 alias 조차 healthy 로 보였다. 등록부에 없는 alias(usage_logs.model_alias 는 FK 없는
free-text)는 UNREGISTERED 로 따로 보인다. 조인·GROUP BY 가 실제 스키마에서 맞게 도는지는 실 PG 로
test_high_console_contract_real_db.py 가 증명한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.routers.monitoring import UNREGISTERED_MODEL_STATUS, monitoring_models


def _row(alias: str, status: str | None):
    return SimpleNamespace(
        model_alias=alias,
        requests=10,
        avg_latency_ms=120.0,
        error_count=1,
        last_request_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        registry_status=status,
    )


async def _call(rows):
    result = MagicMock()
    result.all.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    out = await monitoring_models(request=MagicMock(), admin=MagicMock(), session=session)
    return out, session.execute.await_args.args[0]


async def test_status_comes_from_the_registry_not_a_literal():
    out, _ = await _call([_row("live", "ACTIVE"), _row("retired", "INACTIVE"), _row("ghost", None)])
    assert {m["alias"]: m["status"] for m in out["models"]} == {
        "live": "ACTIVE",
        "retired": "INACTIVE",
        "ghost": UNREGISTERED_MODEL_STATUS,
    }


async def test_join_is_outer_so_unregistered_traffic_is_not_dropped():
    _, stmt = await _call([])
    sql = str(stmt)
    assert "LEFT OUTER JOIN model.model_aliases" in sql, sql
    # enum 이 아니라 텍스트로 읽는다 — 이미지가 모르는 새 enum 값에도 죽지 않는다.
    assert "CAST(model.model_aliases.status AS TEXT)" in sql


async def test_other_columns_are_unchanged():
    """대조군 — 상태 열만 바뀌고 나머지 계산은 그대로다."""
    out, _ = await _call([_row("live", "ACTIVE")])
    m = out["models"][0]
    assert (m["last_1h_requests"], m["avg_latency_ms"], m["error_rate_pct"]) == (10, 120, 10.0)
