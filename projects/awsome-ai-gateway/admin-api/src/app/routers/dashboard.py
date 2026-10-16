# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.core.usage_filters import (
    client_coalesce_expr,
    client_filter,
    cost_period_filter,
    current_kst_period,
    kst_month_expr,
)
from app.models.auth import KeyStatus, Team, User, VirtualKey
from app.models.budget import BudgetConfig, BudgetScope
from app.models.model import ModelAlias, ModelStatus
from app.models.usage import UsageLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/dashboard", tags=["Dashboard"])

#: 대시보드 응답 캐시 TTL(초). 30초를 고른 이유: 관리자가 예산/모델/키를 바꾼 뒤 화면에
#: 반영되기까지의 최대 지연이 그만큼이라는 뜻이므로, 체감상 "즉시" 로 남는 상한이다.
#: 더 늘리면 편집 후 화면이 낡아 보여 운영자가 새로고침을 반복하게 된다.
_DASHBOARD_CACHE_TTL = 30


def _cache_key(name: str, **parts: object) -> str:
    """`dashboard:<name>:<k=v>...` 형태의 캐시 키.

    ⚠️ 값 정규화가 중요하다. 이 라우터의 필터는 미지정이 `None` 인데 admin-ui 는 전체를
    뜻할 때 `'all'` 을 보낸다. 정규화하지 않으면 같은 질의가 두 키에 나뉘어 캐시 적중률이
    반토막 나고, 더 나쁘게는 한쪽만 무효화되어 두 값이 갈린다.
    """
    norm = "&".join(
        f"{k}={'all' if v in (None, '', 'all') else v}" for k, v in sorted(parts.items())
    )
    return f"dashboard:{name}:{norm}"


async def _cache_get(request: Request, key: str):
    """캐시 조회. **어떤 실패도 삼킨다**(fail-open).

    ⚠️ Redis 장애가 대시보드를 죽여서는 안 된다 — 캐시는 성능 장치일 뿐 정합성의
    근거가 아니다. 그래서 예외를 올리지 않고 miss 로 취급한다. 다만 조용히 넘기면
    "캐시가 영원히 안 맞는" 상태를 아무도 모르므로 debug 로 흔적은 남긴다.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 — 캐시 실패는 요청 실패가 아니다
        logger.debug("dashboard cache get failed key=%s err=%s", key, exc)
        return None


async def _cache_set(request: Request, key: str, value: object) -> None:
    """캐시 저장. 조회와 같은 이유로 실패를 삼킨다."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.setex(key, _DASHBOARD_CACHE_TTL, json.dumps(value, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.debug("dashboard cache set failed key=%s err=%s", key, exc)


@router.get("/summary")
async def dashboard_summary(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = current_kst_period()

    # ⚠️ 캐시가 안전한 근거: 이 라우터의 모든 핸들러가 `require_admin` 이므로 응답이
    #    **행위자에 따라 달라지지 않는다**. 그래서 키에 actor 를 넣지 않아도 된다.
    #    (대조: /admin/analytics 는 require_admin_or_team_leader 라 같은 파라미터가
    #     ADMIN 에겐 전사·TEAM_LEADER 에겐 팀 범위를 뜻한다 — 거기서 actor 없는 키를
    #     쓰면 TEAM_LEADER 가 전사 데이터를 받는다. 그 캐시는 role 을 키에 넣는다.)
    cache_key = _cache_key("summary", period=period, client=client)
    if (cached := await _cache_get(request, cache_key)) is not None:
        return cached

    # 선택적 앱(client) 필터 — 'all'/None 이면 전체.
    where_clauses = [cost_period_filter(period)]
    if (cf := client_filter(client)) is not None:
        where_clauses.append(cf)

    stmt = select(
        func.count().label("total_requests"),
        func.coalesce(
            func.sum(
                UsageLog.input_tokens
                + UsageLog.output_tokens
                + UsageLog.cache_creation_tokens
                + UsageLog.cache_read_tokens
            ),
            0,
        ).label("total_tokens"),
        func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost_usd"),
        func.count(distinct(UsageLog.user_id)).label("active_users"),
    ).where(*where_clauses)
    row = (await session.execute(stmt)).one()

    total_requests = row.total_requests or 0
    total_tokens = int(row.total_tokens or 0)
    total_cost = Decimal(row.total_cost_usd or 0)
    active_users = row.active_users or 0
    cost_per_user = (total_cost / active_users) if active_users > 0 else Decimal(0)

    payload = {
        "period": period,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_cost_usd": round(float(total_cost), 4),
        "active_users": active_users,
        "cost_per_user_usd": round(float(cost_per_user), 4),
    }
    await _cache_set(request, cache_key, payload)
    return payload


@router.get("/model-share")
async def model_share(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    team_id: str = Query(default="all", description="UUID 또는 'all'"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = current_kst_period()

    # 응답을 바꾸는 파라미터 **전부**를 키에 넣는다 — 하나라도 빠지면 다른 질의의
    # 결과가 반환된다(team_id 를 빼면 A팀 화면에 B팀 점유율이 뜨는 식).
    cache_key = _cache_key("model-share", period=period, team_id=team_id, client=client)
    if (cached := await _cache_get(request, cache_key)) is not None:
        return cached

    where_clauses = [
        # §59 비용 집계 표준: SUCCESS 만 + KST 월 경계.
        cost_period_filter(period),
    ]
    if (cf := client_filter(client)) is not None:
        where_clauses.append(cf)

    team_filter: str = "all"
    if team_id and team_id != "all":
        try:
            team_uuid = uuid.UUID(team_id)
        except ValueError:
            return {
                "period": period,
                "team_id": team_id,
                "total_cost_usd": 0.0,
                "models": [],
                "error": "invalid_team_id",
            }
        where_clauses.append(UsageLog.team_id == team_uuid)
        team_filter = str(team_uuid)

    # display_name 을 위해 model_aliases 와 LEFT OUTER JOIN (없으면 NULL → UI 가 alias fallback).
    stmt = (
        select(
            UsageLog.model_alias,
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.max(ModelAlias.display_name).label("display_name"),
        )
        .select_from(UsageLog)
        .outerjoin(ModelAlias, ModelAlias.alias == UsageLog.model_alias)
        .where(*where_clauses)
        .group_by(UsageLog.model_alias)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = (await session.execute(stmt)).all()

    total = sum(float(r.cost_usd or 0) for r in rows)
    models = []
    for r in rows:
        cost = float(r.cost_usd or 0)
        share = (cost / total * 100) if total > 0 else 0.0
        models.append({
            "model_alias": r.model_alias,
            "display_name": r.display_name,
            "cost_usd": round(cost, 4),
            "share_pct": round(share, 2),
        })

    payload = {
        "period": period,
        "team_id": team_filter,
        "total_cost_usd": round(total, 4),
        "models": models,
    }
    await _cache_set(request, cache_key, payload)
    return payload


@router.get("/client-share")
async def client_share(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """앱별(client) 비용 점유율 — claude-code / cowork / codex / other(legacy NULL 포함).

    client_coalesce_expr() 로 NULL(레거시 미식별) 행을 'other' 로 접어 GROUP BY.
    §59 비용 집계 표준(SUCCESS + KST 월 경계) 동일 적용. admin-ui ClientShareResponse 형태.
    """
    if not period:
        period = current_kst_period()

    client_col = client_coalesce_expr().label("client")
    stmt = (
        select(
            client_col,
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().label("call_count"),
            # Web search calls per client (attribution metric; not a cost/token). Lets the
            # dashboard show which apps use AgentCore WebSearch and how much.
            func.coalesce(func.sum(UsageLog.web_search_count), 0).label("web_search_count"),
        )
        .where(cost_period_filter(period))
        .group_by(client_col)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = (await session.execute(stmt)).all()

    total = sum(float(r.cost_usd or 0) for r in rows)
    clients = []
    for r in rows:
        cost = float(r.cost_usd or 0)
        share = (cost / total * 100) if total > 0 else 0.0
        clients.append({
            "client": r.client,
            "cost_usd": round(cost, 4),
            "share_pct": round(share, 2),
            "call_count": int(r.call_count or 0),
            "web_search_count": int(r.web_search_count or 0),
        })

    return {
        "period": period,
        "total_cost_usd": round(total, 4),
        "clients": clients,
    }


@router.get("/top-users")
async def top_users(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    limit: int = Query(default=5, ge=1, le=50),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """실제 비용 기준 상위 사용자(§60.8) — usage_logs 를 SUCCESS+KST 로 집계해 cost 내림차순.

    ⚠️ 기존 대시보드 'Top 사용자 by 비용' 위젯은 budgets/summary(예산설정된 사용자만)를
    써서 예산 없는 헤비유저를 누락했다(라벨='비용'인데 실제론 예산설정자 중 사용액).
    이 엔드포인트는 챗(text2SQL)과 동일하게 usage_logs 전체에서 진짜 top spender 를 낸다.
    PII 금지: sso_subject 미노출(display_name·email 만).
    """
    if not period:
        period = current_kst_period()

    stmt = (
        select(
            User.display_name.label("name"),
            User.email.label("email"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().label("call_count"),
        )
        .join(User, User.id == UsageLog.user_id)
        .where(
            cost_period_filter(period),  # §59 SUCCESS + KST (대시보드 단일 진실원)
            *([cf] if (cf := client_filter(client)) is not None else []),
        )
        .group_by(User.id, User.display_name, User.email)
        .order_by(func.sum(UsageLog.cost_usd).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    return {
        "period": period,
        "users": [
            {
                "name": r.name,
                "email": r.email,
                "cost_usd": round(float(r.cost_usd or 0), 4),
                "call_count": int(r.call_count or 0),
            }
            for r in rows
        ],
    }


@router.get("/top-teams")
async def top_teams(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    limit: int = Query(default=5, ge=1, le=50),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """실제 비용 기준 상위 팀(§60.9) — usage_logs 를 SUCCESS+KST 로 집계해 cost 내림차순.

    기존 대시보드 'Top 팀 by 비용'은 budgets/summary(예산설정 팀만)를 써서 예산 없는
    팀을 누락했다(§60.8 의 top-users 와 동형 버그 — 팀은 미수정이었음). top-users 와
    동일하게 usage_logs 전체에서 집계한다. **팀 귀속은 usage_logs.team_id 직접**
    (users.team_id 경유 금지 — 팀 이동 사용자의 과거 비용 오귀속 방지).
    """
    if not period:
        period = current_kst_period()

    stmt = (
        select(
            # ⚠️ team_id 를 내보낸다. 콘솔은 이 목록을 /admin/budgets/summary 의 소진율과 조인하는데,
            #    id 가 없던 동안 **팀 이름**으로 조인했다. 팀 이름은 전역 유일이 아니라(부서마다 같은
            #    이름이 있을 수 있다) 동명 팀의 소진율이 서로 뒤바뀌어 표시됐다. Team.id 는 이미
            #    GROUP BY 에 있어 질의 비용은 그대로다.
            Team.id.label("team_id"),
            Team.name.label("name"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().label("call_count"),
        )
        .join(Team, Team.id == UsageLog.team_id)
        .where(
            cost_period_filter(period),  # §59 SUCCESS + KST (대시보드 단일 진실원)
            *([cf] if (cf := client_filter(client)) is not None else []),
        )
        .group_by(Team.id, Team.name)
        # 동점(같은 비용)일 때 순서를 고정 — LIMIT 경계에서 어느 팀이 잘리는지가 요청마다 바뀌지 않게.
        .order_by(func.sum(UsageLog.cost_usd).desc(), Team.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    return {
        "period": period,
        "teams": [
            {
                "team_id": str(r.team_id),
                "name": r.name,
                "cost_usd": round(float(r.cost_usd or 0), 4),
                "call_count": int(r.call_count or 0),
            }
            for r in rows
        ],
    }


@router.get("/kpi")
async def dashboard_kpi(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """대시보드 상단 KPI 카드 일괄 — 화면 1개당 API 1개.

    ## 왜 필요한가

    예전 화면은 카드를 채우려고 4개 API 를 동시에 불렀고, 그중
    `GET /admin/budgets/summary` 가 병목이었다. 그 핸들러는 활성 예산 config 를 훑으며
    **config 하나당 Redis GET + usage_logs SUM 을 순차로** 수행한다
    (`budget_service.get_budget_summary` → `_resolve_used`). 사용자가 수백~수천 명 규모면
    라운드트립이 그만큼 쌓여 대시보드 첫 로드가 수십 초까지 늘어난다.
    KPI 카드에 필요한 건 **합계 두 개**(사용액·한도)뿐이라, 그걸 SQL 집계 한 번으로 낸다.

    ## 예산 사용률을 이렇게 계산하는 이유

    분모(한도)는 **TEAM 예산 + 팀이 없는 USER 예산**만 더한다. 팀에 속한 USER 예산을
    함께 더하면 같은 지출 한도를 팀 축과 개인 축에서 **이중계상**한다(팀 예산이 이미 그
    멤버들을 포함한다). 이건 기존 화면이 프론트에서 하던 계산과 정확히 같은 의미다
    (admin-ui/src/app/page.tsx 의 teamItems + teamlessUsers) — 값이 움직이지 않는다.

    ⚠️ 분자(사용액)는 `budget.budget_usages` 에서 읽지 **않는다.** 그 테이블은
    cost-recorder-worker 가 플러시하는 모든 항목을 status 구분 없이 누적하는
    monotonic accumulator 라(ERROR/TIMEOUT 비용 포함, 사후 정정 없음) §59 의
    "SUCCESS 만" 기준과 어긋난다. 같은 화면의 다른 카드는 전부 usage_logs 의 SUCCESS
    합계를 쓰므로, 여기서만 다른 소스를 쓰면 **같은 화면 안에서 숫자가 서로 안 맞는다.**
    그래서 분자도 `cost_period_filter`(SUCCESS + KST 월) 로 usage_logs 에서 집계한다.

    ## 실패 시 의미

    카드별로 `null` 을 돌려준다. 프론트는 null 을 '—' 로 렌더해야 한다 — 0 으로 접으면
    "활성 키 0개" 처럼 **거짓 사실**을 표시하게 된다(page.tsx 의 기존 관례).
    """
    if not period:
        period = current_kst_period()

    cache_key = _cache_key("kpi", period=period, client=client)
    if (cached := await _cache_get(request, cache_key)) is not None:
        return cached

    # ── 1) 비용/요청/사용자: /summary 와 동일한 집계(같은 필터를 공유해 값이 갈리지 않게)
    where_clauses = [cost_period_filter(period)]
    if (cf := client_filter(client)) is not None:
        where_clauses.append(cf)

    usage_row = (
        await session.execute(
            select(
                func.count().label("total_requests"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
                func.count(distinct(UsageLog.user_id)).label("active_users"),
                # /summary 와 **같은 식**을 쓴다 — 두 엔드포인트가 같은 화면에 쓰이므로
                # 토큰 정의가 갈리면 같은 기간에 다른 숫자가 보인다.
                func.coalesce(
                    func.sum(
                        UsageLog.input_tokens
                        + UsageLog.output_tokens
                        + UsageLog.cache_creation_tokens
                        + UsageLog.cache_read_tokens
                    ),
                    0,
                ).label("total_tokens"),
            ).where(*where_clauses)
        )
    ).one()

    total_cost = Decimal(str(usage_row.total_cost or 0))
    active_users = usage_row.active_users or 0

    # ── 2) 예산 한도(분모): TEAM + 팀 없는 USER 의 활성 config 합계.
    #    LEFT JOIN 후 team_id IS NULL 조건으로 "팀 없는 USER" 를 고른다. USER config 의
    #    scope_id 가 auth.users 에 없는 고아 행이면 조인이 NULL 이 되어 포함되는데,
    #    그건 팀 소속을 확인할 수 없는 예산이므로 합산 대상으로 두는 편이 안전하다
    #    (누락시 사용률이 과대평가된다).
    limit_row = (
        await session.execute(
            select(func.coalesce(func.sum(BudgetConfig.max_budget_usd), 0).label("total_limit"))
            .select_from(BudgetConfig)
            .outerjoin(
                User,
                and_(
                    BudgetConfig.scope == BudgetScope.USER,
                    User.id == BudgetConfig.scope_id,
                ),
            )
            .where(
                BudgetConfig.is_active.is_(True),
                or_(
                    BudgetConfig.scope == BudgetScope.TEAM,
                    and_(
                        BudgetConfig.scope == BudgetScope.USER,
                        User.team_id.is_(None),
                    ),
                ),
            )
        )
    ).one()
    total_limit = Decimal(str(limit_row.total_limit or 0))

    # ── 3) 예산 사용액(분자): usage_logs SUCCESS 합계.
    #    한도가 TEAM+팀없는USER 를 덮으므로 사용액도 전사 합계와 같다(모든 사용자는
    #    팀에 속하거나 속하지 않는다 — 두 집합의 합집합이 전체다). client 필터는
    #    적용하지 않는다: 예산은 client 축과 무관한 전체 한도이므로 분자도 전체여야
    #    비율이 의미를 갖는다.
    budget_used_row = (
        await session.execute(
            select(func.coalesce(func.sum(UsageLog.cost_usd), 0).label("used"))
            .where(cost_period_filter(period))
        )
    ).one()
    budget_used = Decimal(str(budget_used_row.used or 0))
    utilization = (
        float(budget_used / total_limit * 100) if total_limit > 0 else None
    )

    # ── 4) 활성 키 / 활성 모델
    active_keys = (
        await session.execute(
            select(func.count()).select_from(VirtualKey).where(
                VirtualKey.status == KeyStatus.ACTIVE
            )
        )
    ).scalar()
    active_models = (
        await session.execute(
            select(func.count()).select_from(ModelAlias).where(
                ModelAlias.status == ModelStatus.ACTIVE
            )
        )
    ).scalar()

    payload = {
        "period": period,
        "total_requests": usage_row.total_requests or 0,
        "total_tokens": int(usage_row.total_tokens or 0),
        "total_cost_usd": round(float(total_cost), 4),
        "active_users": active_users,
        "cost_per_user_usd": round(float(total_cost / active_users), 4) if active_users else 0.0,
        "budget_used_usd": round(float(budget_used), 4),
        "budget_limit_usd": round(float(total_limit), 4),
        # 한도가 0 이면 비율이 정의되지 않는다 — 0% 로 접으면 "예산을 안 썼다" 는
        # 거짓 사실이 된다. null 로 두고 프론트가 '—' 로 렌더한다.
        "budget_utilization_pct": round(utilization, 2) if utilization is not None else None,
        "active_keys": active_keys or 0,
        "active_models": active_models or 0,
    }
    await _cache_set(request, cache_key, payload)
    return payload


@router.get("/periods")
async def dashboard_periods(
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """사용량 데이터가 실제로 존재하는 월(YYYY-MM) 목록 — 최신순.

    admin-ui 의 기간 선택기가 이걸로 옵션을 채우고, 기본값을
    "데이터 있는 가장 최근 월"(periods[0])로 잡아 현재 달력월이 비어도
    빈 화면을 피한다. status 필터 안 함 — 에러만 있는 월도 노출.
    """
    # 월 binning 을 명시적 KST 로(§59) — requested_at 은 timestamptz 라 to_char 가
    # 세션 타임존을 타므로, timezone('Asia/Seoul', ...) 로 고정해 /summary·budget·
    # chat 과 동일 기준(KST) 보장. status 필터 안 함 — 에러만 있는 월도 옵션에 노출.
    period_expr = kst_month_expr()
    stmt = select(distinct(period_expr).label("period")).order_by(period_expr.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return {"periods": [p for p in rows if p]}
