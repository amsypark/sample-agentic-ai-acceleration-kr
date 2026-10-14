# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
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

# 팀 카운터 Redis 읽기의 상한(초). ⚠️ admin-api 의 Redis 클라이언트에는 소켓 타임아웃이 없다
# (core/redis_client.py) — 연결은 받고 응답하지 않는 Redis 앞에서 이 조회가 끝없이 기다리면, 팀 예산이 있는
# 모든 사용자의 /my 카드가 멈춘다. 상한을 넘기면 'Redis 를 쓸 수 없음' 으로 보고 DB 로 판정한다.
_REDIS_READ_TIMEOUT_S = 0.5


def _gateway_denies_team(policy: BudgetPolicy, limit: Decimal, used: Decimal) -> bool:
    """budget_check.lua 의 팀 계층 거부 판정 — **같은 식, 같은 피연산자, float64**.

    Lua 의 숫자는 double 이다. limit 은 config 캐시의 문자열(str(Decimal))을, used 는 카운터 문자열을
    ``tonumber`` 한 값이고, ``float()`` 는 같은 십진 문자열에서 같은 double 을 만든다.
      hard_block    ``used >= limit``
      soft_warning  ``used * 100 >= limit * soft_limit_pct``  (soft_limit_pct 는 config 에 없어 Lua 기본값 110)
      throttle      거부하지 않는다(RPM 만 줄인다)
    ⚠️ 십진수(Decimal)로 계산하면 경계에서 Lua 와 갈린다. SOFT_WARNING · 한도 $0.01 · 사용 $0.0110 은 Lua 에서
       0.011*100 = 1.0999999999999999 < 0.01*110 = 1.1 이라 **통과**인데, Decimal 로는 1.1000 >= 1.10 이라 '소진'
       이었다(실 Redis 의 Lua 와 그리드로 대조 — tests/regression/test_high_my_budget_gateway_parity_real.py).
    """
    limit_f, used_f = float(limit), float(used)
    if policy == BudgetPolicy.HARD_BLOCK:
        return used_f >= limit_f
    if policy == BudgetPolicy.SOFT_WARNING:
        return used_f * 100 >= limit_f * _GATEWAY_SOFT_LIMIT_PCT
    return False


async def _team_used_usd(redis, session: AsyncSession, team_id: uuid.UUID, period: str) -> Decimal:
    """게이트웨이가 팀 계층을 판정할 때 보는 이번 달 팀 사용액 — 원천과 해석이 게이트웨이와 같아야 한다.

    * Redis 카운터 ``budget:team:{<team_id>}:<period>`` 가 원천이다. gateway-proxy 가 요청을 끝낼 때마다 직접
      INCRBYFLOAT 하고(services/cost_recorder.py → budget_deduct.lua) budget_check.lua 가 그 값으로 거부한다.
      ⚠️ 예전엔 budget_usages 만 읽었다. 그 행은 cost-recorder-worker 가 배치로 쌓아 카운터보다 늦다 — 이미
      거부되는 팀을 '여유 있음' 으로 보였다.
    * 키가 없으면 **0** 이다. budget_check.lua 는 ``GET … or '0'`` 으로 읽고, 게이트웨이는 팀 카운터를 DB 에서
      복원하지 않는다 — 카운터가 사라진 직후(장애·재시작) 게이트웨이는 실제로 그 팀을 통과시킨다.
    * Redis 를 쓸 수 없을 때만 budget_usages 다(게이트웨이도 그때 DB 폴백으로 같은 행을 읽는다). 모델은
      budget_service.get_budget_summary 의 Redis 우선 조회다.
    """
    if redis is not None:
        try:
            raw = await asyncio.wait_for(
                redis.get(f"budget:team:{{{team_id}}}:{period}"), timeout=_REDIS_READ_TIMEOUT_S
            )
            return Decimal(raw.decode() if isinstance(raw, bytes) else raw) if raw is not None else Decimal("0")
        except Exception:  # noqa: BLE001 - 연결 실패·응답 없음(TimeoutError)·이상한 값이면 DB 로 판정한다
            pass
    return Decimal(
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

    # 개인 예산 행이 있는가(3-state: 설정됨 / 미설정 / 지출). 게이트웨이는 행이 없으면 그 계층을 통과시킨다
    # (gateway-proxy budget_service "Q 정책: USER 예산 미설정 → pass-through", 팀 예산은 계속 적용).
    #
    # ⚠️ 하위 호환 — `budget` 의 limit_usd · remaining_usd · usage_pct · policy 는 **이전 admin-ui 카드가 그대로
    #    `.toFixed()` 한다**. 행이 없을 때도 예전과 같은 값(limit 0 · "HARD_BLOCK" · usage_pct 0 · remaining =
    #    -used)을 계속 보낸다. 한때 이 넷을 null 로 바꿨는데, admin-api 를 먼저 배포하는 동안(admin-api → admin-ui
    #    가 배포 순서이고, helm 한 번의 롤에서도 두 버전이 몇 분 섞인다) 이전 카드가 TypeError 로 통째로 죽었다.
    #    '미설정' 이라는 새 의미는 **더하는 필드** `configured` 로만 낸다 — 새 카드는 그것을 보고 미설정을 그린다.
    configured = config is not None
    limit_usd = float(config.max_budget_usd) if configured else 0
    policy = config.policy.value if configured else "HARD_BLOCK"

    usage_stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        UsageLog.user_id == user.user_id,
        cost_period_filter(period),  # §59 SUCCESS + KST
    )
    used_result = await session.execute(usage_stmt)
    used_usd = float(used_result.scalar_one())
    remaining = limit_usd - used_usd
    # $0 한도의 사용률은 정의되지 않지만 이전 카드가 숫자를 기대하므로 예전처럼 0 을 보낸다. 새 카드는
    # configured 이고 limit 이 0 이면 사용률 대신 '--' 와 정책별 $0 판정을 그린다(0.0% 는 '아직 안 씀' 으로 읽힌다).
    usage_pct = (used_usd / limit_usd * 100) if limit_usd > 0 else 0

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
    #    규칙은 게이트웨이 팀 계층 판정(redis_scripts/budget_check.lua) 그대로다(_gateway_denies_team, float64):
    #      HARD_BLOCK   used >= limit               ($0 은 항상 거부)
    #      SOFT_WARNING used * 100 >= limit * 110   ($0 은 항상 거부. Redis 가 죽었을 때의 DB 폴백만
    #                                                `limit > 0` 조건 때문에 $0 을 통과시킨다 — 평소 경로를 따른다)
    #      THROTTLE     거부하지 않는다(RPM 만 줄인다)
    #    used 는 게이트웨이가 판정에 쓰는 Redis 팀 카운터다(gateway-proxy 가 직접 올린다). Redis 를 쓸 수 없을 때만
    #    budget_usages(게이트웨이 DB 폴백이 읽는 행 — cost-recorder-worker 가 배치로 쌓아 카운터보다 늦다) — _team_used_usd.
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
            team_used = await _team_used_usd(request.app.state.redis, session, team_id, period)
            team_budget_exhausted = _gateway_denies_team(team_cfg.policy, team_cfg.max_budget_usd, team_used)

    return {
        "user_id": str(user.user_id),
        "period": period,
        "budget": {
            "limit_usd": round(limit_usd, 2),
            "used_usd": round(used_usd, 4),
            "remaining_usd": round(remaining, 4),
            "usage_pct": round(usage_pct, 1),
            "policy": policy,
            "configured": configured,
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
