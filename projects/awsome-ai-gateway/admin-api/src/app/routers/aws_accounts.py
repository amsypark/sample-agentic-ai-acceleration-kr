# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS 계정 레지스트리 관리 라우터 (마이그레이션 0044).

``GET    /admin/aws-accounts``          → 항목 + 각 항목을 쓰는 alias·앱, 앱 단위 계정 목록
``POST   /admin/aws-accounts``          → 등록
``PUT    /admin/aws-accounts/{name}``   → 수정(name 은 불변)
``DELETE /admin/aws-accounts/{name}``   → 삭제. alias 가 고르고 있으면 409

쓰기 경로는 명시적으로 커밋한 **뒤에** 수정이 옮긴 모든 alias 의 모델 캐시 키를 지운다
(routers/routing.py 와 같은 순서). 커밋 전에 지우면 그 창에 들어온 게이트웨이 캐시 미스가 수정 전
행을 다시 읽어 **옛 계정**을 300초 더 캐시한다 — 아무것도 바꾸지 못한 200 이고, 청구는 운영자가 막
옮겨 가려던 계정으로 간다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.schemas.aws_accounts import (
    AwsAccountCreateRequest,
    AwsAccountListResponse,
    AwsAccountResponse,
    AwsAccountUpdateRequest,
)
from app.services.aws_account_service import AwsAccountService

router = APIRouter(prefix="/admin/aws-accounts", tags=["AWS Accounts"])


def _ctx(request: Request) -> dict:
    return {
        "ip_address": request.client.host if request.client else "0.0.0.0",
        "request_id": request.headers.get("x-request-id", ""),
    }


@router.get("", response_model=AwsAccountListResponse)
async def list_aws_accounts(
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    return await AwsAccountService(session).list_accounts()


@router.post("", response_model=AwsAccountResponse, status_code=201)
async def create_aws_account(
    request: Request,
    body: AwsAccountCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    outcome = await AwsAccountService(session).create(body, actor=admin, **_ctx(request))
    await session.commit()
    return outcome.response


@router.put("/{name}", response_model=AwsAccountResponse)
async def update_aws_account(
    request: Request,
    name: str,
    body: AwsAccountUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    outcome = await AwsAccountService(session).update(name, body, actor=admin, **_ctx(request))
    await session.commit()
    # 커밋 **뒤에**(모듈 docstring). best effort — Redis 실패면 캐시 TTL 에 반영되고, 실패는
    # CacheInvalidationManager 가 기록해 재시도할 수 있다.
    await request.app.state.cache_mgr.invalidate(outcome.cache_keys, session=session)
    return outcome.response


@router.delete("/{name}", status_code=204)
async def delete_aws_account(
    request: Request,
    name: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    await AwsAccountService(session).delete(name, actor=admin, **_ctx(request))
    await session.commit()
    return Response(status_code=204)
