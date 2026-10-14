# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_session
from app.core.usage_filters import cost_period_filter, current_kst_period
from app.models.auth import User
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, BudgetUsage
from app.models.usage import UsageLog

router = APIRouter(prefix="/admin/my", tags=["My Usage"])

# 게이트웨이 SOFT_WARNING 의 허용 상한(%) — DB 컬럼이 없어 admin-api 는 config 캐시에 쓰지 않는다.
# budget_check.lua 의 기본값(soft_limit_pct=110)과 gateway-proxy DEFAULT_SOFT_LIMIT_PCT 가 쓰인다.
_GATEWAY_SOFT_LIMIT_PCT = 110


@router.get("/budget")
async def get_my_budget(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    # KST 월(§59) — date.today() 는 pod TZ(UTC)라 매월 1일 첫 9시간에 지난달이 된다.
    period = current_kst_period()

    # ⚠️ `client IS NULL` 이 load-bearing 이다. per-app(앱별) 예산 config 가 **같은
    #    테이블**에 살기 때문에, 이 필터가 없으면 `created_at DESC LIMIT 1` 이 나중에
    #    만들어진 앱별 행을 집는다. 그러면 사용자 화면의 "내 개인 월예산" 칸에 그 앱의
    #    한도(예: $200)가 뜨는데 used_usd 는 **총 사용액**이라, remaining 과 usage_pct
    #    가 둘 다 틀린다 — 사용자는 아직 여유가 있는데 소진됐다고 보거나 그 반대가 된다.
    #    이 레포의 리포지토리 계층은 이미 같은 필터를 걸고 있었다
    #    (repositories/budget_repository.py) — 이 라우터만 빠져 있었다.
    stmt = (
        select(BudgetConfig)
        .where(
            BudgetConfig.scope == BudgetScope.USER,
            BudgetConfig.scope_id == user.user_id,
            BudgetConfig.is_active.is_(True),
            BudgetConfig.client.is_(None),
        )
        .order_by(BudgetConfig.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    config = result.scalar_one_or_none()

    # ⚠️ 개인 예산 행이 없으면 값을 **지어내지 않는다**(3-state: 설정됨 / 미설정 / 지출).
    #    예전엔 limit 0 · policy "HARD_BLOCK" 으로 접어, 지출이 있는 사용자에게
    #    "한도 $0 / 잔여 $-N / 하드 차단" 이라는 실재하지 않는 강제를 보였다. 게이트웨이는
    #    개인 예산이 없으면 통과시킨다(gateway-proxy budget_service "Q 정책: USER 예산 미설정 →
    #    pass-through", 팀 예산은 계속 적용) — 콘솔만 거짓말을 했다. 그래서 limit/remaining/
    #    usage_pct/policy 는 null 로 내보내고 used_usd(진짜 지출)만 채운다.
    if config is not None:
        limit_usd: float | None = float(config.max_budget_usd)
        policy: str | None = config.policy.value
    else:
        limit_usd = None
        policy = None

    usage_stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        UsageLog.user_id == user.user_id,
        cost_period_filter(period),  # §59 SUCCESS + KST
    )
    used_result = await session.execute(usage_stmt)
    used_usd = float(used_result.scalar_one())
    if limit_usd is None:
        remaining: float | None = None
        usage_pct: float | None = None
    else:
        remaining = limit_usd - used_usd
        # ⚠️ $0 한도의 사용률은 **없다**(null). 예전엔 0.0 을 보내 카드가 초록 막대 "0.0%" 로 '아직 안
        #    씀' 처럼 그렸는데, HARD_BLOCK 의 $0 은 모든 요청을 막는다(gateway used >= limit).
        usage_pct = (used_usd / limit_usd * 100) if limit_usd > 0 else None

    # 팀 예산 상태 — 게이트웨이는 **팀 예산이 없으면 거부한다**(gateway-proxy budget_service C-1:
    # TEAM config 미설정 → team_budget_unset, 팀 미배정 사용자도 같다). 개인 예산이 없을 때 카드가
    # "팀 예산 적용" 이라고 말하려면 그게 사실인지 알아야 한다. 팀은 JWT 가 아니라 DB 의 현재 소속이다
    # (게이트웨이도 DB 소속으로 판정한다).
    team_id = (
        await session.execute(select(User.team_id).where(User.id == user.user_id))
    ).scalar_one_or_none()
    team_budget_configured = False
    # ⚠️ 팀 예산 행이 **있어도** 게이트웨이가 거부하는 상태가 있다 — 그 팀 예산이 소진됐을 때다. 행이 있다는
    #    사실(budget_configured)과 따로 낸다: 소진을 "미설정" 으로 접으면 카드가 있는 예산을 없다고 말한다.
    #    규칙은 게이트웨이 팀 계층 판정(redis_scripts/budget_check.lua) 그대로다 — 실 Redis 에서 확인:
    #      HARD_BLOCK   used >= limit               ($0 은 항상 거부)
    #      SOFT_WARNING used * 100 >= limit * 110   ($0 은 항상 거부. Redis 가 죽었을 때의 DB 폴백만
    #                                                `limit > 0` 조건 때문에 $0 을 통과시킨다 — 평소 경로를 따른다)
    #      THROTTLE     거부하지 않는다(RPM 만 줄인다)
    #    used 는 이번 KST 월의 budget_usages TEAM 행이다 — 게이트웨이 DB 폴백이 읽는 행이고, 게이트웨이의 Redis
    #    카운터와 같은 비용 스트림(cost-recorder-worker)으로 쌓인다(그쪽이 flush 주기만큼 앞설 수 있다).
    team_budget_exhausted = False
    if team_id is not None:
        team_cfg = (
            await session.execute(
                select(BudgetConfig)
                .where(
                    BudgetConfig.scope == BudgetScope.TEAM,
                    BudgetConfig.scope_id == team_id,
                    BudgetConfig.is_active.is_(True),
                    BudgetConfig.client.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        team_budget_configured = team_cfg is not None
        if team_cfg is not None:
            team_used = Decimal(
                (
                    await session.execute(
                        select(func.coalesce(func.sum(BudgetUsage.used_usd), 0)).where(
                            BudgetUsage.scope == BudgetScope.TEAM,
                            BudgetUsage.scope_id == team_id,
                            BudgetUsage.period == period,
                            BudgetUsage.client.is_(None),
                        )
                    )
                ).scalar_one()
            )
            team_limit = team_cfg.max_budget_usd
            if team_cfg.policy == BudgetPolicy.HARD_BLOCK:
                team_budget_exhausted = team_used >= team_limit
            elif team_cfg.policy == BudgetPolicy.SOFT_WARNING:
                team_budget_exhausted = team_used * 100 >= team_limit * _GATEWAY_SOFT_LIMIT_PCT

    return {
        "user_id": str(user.user_id),
        "period": period,
        "budget": {
            "limit_usd": round(limit_usd, 2) if limit_usd is not None else None,
            "used_usd": round(used_usd, 4),
            "remaining_usd": round(remaining, 4) if remaining is not None else None,
            "usage_pct": round(usage_pct, 1) if usage_pct is not None else None,
            "policy": policy,
        },
        "team": {
            "assigned": team_id is not None,
            "budget_configured": team_budget_configured,
            "budget_exhausted": team_budget_exhausted,
        },
    }


@router.get("/usage")
async def get_my_usage(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = current_kst_period()  # KST 월 — 아래 _kst_day 집계와 경계 통일

    _kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))
    daily_stmt = (
        select(
            _kst_day.label("day"),
            func.sum(UsageLog.cost_usd).label("cost_usd"),
            func.count().label("requests"),
            func.sum(
                UsageLog.input_tokens
                + UsageLog.output_tokens
                + UsageLog.cache_creation_tokens
                + UsageLog.cache_read_tokens
            ).label("tokens"),
        )
        .where(
            UsageLog.user_id == user.user_id,
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        .group_by(_kst_day)
        .order_by(_kst_day)
    )
    daily_result = await session.execute(daily_stmt)
    daily_usage = [
        {
            "date": str(row.day),
            "cost_usd": round(float(row.cost_usd), 4),
            "requests": row.requests,
            "tokens": row.tokens or 0,
        }
        for row in daily_result.all()
    ]

    model_stmt = (
        select(
            UsageLog.model_alias,
            func.sum(UsageLog.cost_usd).label("cost_usd"),
            func.count().label("requests"),
            func.sum(
                UsageLog.input_tokens
                + UsageLog.output_tokens
                + UsageLog.cache_creation_tokens
                + UsageLog.cache_read_tokens
            ).label("tokens"),
        )
        .where(
            UsageLog.user_id == user.user_id,
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        .group_by(UsageLog.model_alias)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    model_result = await session.execute(model_stmt)
    by_model = [
        {
            "model_alias": row.model_alias,
            "cost_usd": round(float(row.cost_usd), 4),
            "requests": row.requests,
            "tokens": row.tokens or 0,
        }
        for row in model_result.all()
    ]

    return {
        "user_id": str(user.user_id),
        "period": period,
        "daily_usage": daily_usage,
        "by_model": by_model,
    }
