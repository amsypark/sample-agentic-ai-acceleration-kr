# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS 계정 레지스트리(마이그레이션 0044) — 조회 / 등록 / 수정 / 삭제.

## 계정을 수정하면 무슨 일이 일어나나

항목의 role_arn 이나 external_id 를 바꾸면 그 항목을 고른 **모든** alias 의 호출 계정이 바뀐다:
DB 트리거 ``model.aws_account_propagate`` 가 같은 트랜잭션에서 그 alias 행의 사본을 다시 쓰고,
게이트웨이는 그 사본으로 서명한다. 레지스트리의 목적(한 곳에서 바꾼다)이자, 수정이 영향받는
alias 들의 Redis 키를 돌려주는 이유다 — 라우터가 **커밋 뒤에** 그 키를 지우지 않으면 게이트웨이는
300초 모델 캐시에서 옛 계정을 계속 쓴다.

## 무엇을 거부하나 (각각 500 이 아니라 409 인 이유)

  * 이미 등록된 역할의 두 번째 항목 — UNIQUE(role_arn): 역할 하나 = 항목 하나라야 "이 역할이 원하는
    ExternalId" 가 어느 alias 가 묻느냐에 따라 달라지지 않는다.
  * 앱의 라우팅 프로필 계정과 같은 역할인데 ExternalId 가 다른 항목 — 신뢰 정책이 둘 중 하나만
    받으면 나머지는 간헐적으로 AssumeRole 에 실패한다.
  * alias 가 아직 고르고 있는 항목의 삭제 — FK ON DELETE RESTRICT(SET NULL 이면 그 alias 들의 청구가
    조용히 기본 계정으로 옮겨 가고, CASCADE 면 alias 가 지워진다).

각각 미리 확인해 충돌하는 이름을 알려 준다. DB 제약은 경합에 대한 마지막 그물이고, 거기서 나온
IntegrityError 도 409 로 바꾼다.
"""

from __future__ import annotations

from typing import NamedTuple

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.exceptions import ConflictError, NotFoundError
from app.models.model import AwsAccount, ModelAlias
from app.models.routing import RoutingProfile
from app.schemas.aws_accounts import (
    AppAccountItem,
    AwsAccountCreateRequest,
    AwsAccountListResponse,
    AwsAccountResponse,
    AwsAccountUpdateRequest,
    account_id_of,
)

logger = structlog.get_logger(__name__)


def model_cache_keys(alias: str, *provider_model_ids: str | None) -> list[str]:
    """모델 하나가 바뀔 때 지울 Redis 키: alias 키 + 각 provider_model_id 키 + 목록.

    provider_model_id 키도 지운다 — 게이트웨이는 모델 id 로 지목한 요청의 해석을 그 키에도
    캐시하므로(router_service._cache_resolved), 그 키에 옛 계정이 300초 남는다.
    """
    keys = [f"model:{alias}"]
    keys.extend(f"model:{pmid}" for pmid in provider_model_ids if pmid)
    keys.append("model:list")
    return list(dict.fromkeys(keys))


class AccountMutation(NamedTuple):
    response: AwsAccountResponse | None
    cache_keys: list[str]


def _external_id_audit(before: str | None, after: str | None) -> dict:
    # ExternalId 는 AWS 정의상 비밀은 아니지만, 콘솔보다 더 많은 사람이 읽는 감사 기록에 복사할
    # 이유도 없다 — 설정 여부와 변경 여부만 남긴다.
    return {"external_id_set": after is not None, "external_id_changed": before != after}


class AwsAccountService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ── 조회 ────────────────────────────────────────────────────────────────────────

    async def _aliases_by_account(self) -> dict[str, list[ModelAlias]]:
        rows = (
            await self._s.execute(select(ModelAlias).where(ModelAlias.account_name.is_not(None)))
        ).scalars().all()
        out: dict[str, list[ModelAlias]] = {}
        for r in rows:
            out.setdefault(r.account_name, []).append(r)
        return out

    async def _app_profiles(self) -> list[RoutingProfile]:
        return list(
            (
                await self._s.execute(
                    select(RoutingProfile).where(RoutingProfile.account_role_arn.is_not(None))
                )
            ).scalars().all()
        )

    async def list_accounts(self) -> AwsAccountListResponse:
        accounts = (
            await self._s.execute(select(AwsAccount).order_by(AwsAccount.name))
        ).scalars().all()
        by_account = await self._aliases_by_account()
        profiles = await self._app_profiles()
        by_role = {a.role_arn: a.name for a in accounts}
        items = [
            AwsAccountResponse.build(
                a,
                aliases=[m.alias for m in by_account.get(a.name, [])],
                app_profiles=[p.client for p in profiles if p.account_role_arn == a.role_arn],
            )
            for a in accounts
        ]
        app_accounts = [
            AppAccountItem(
                client=p.client,
                backend=p.backend,
                role_arn=p.account_role_arn,
                account_id=account_id_of(p.account_role_arn),
                registered_as=by_role.get(p.account_role_arn),
            )
            for p in sorted(profiles, key=lambda p: p.client)
        ]
        return AwsAccountListResponse(items=items, app_accounts=app_accounts)

    async def _get(self, name: str) -> AwsAccount:
        row = await self._s.get(AwsAccount, name)
        if row is None:
            raise NotFoundError("AwsAccount", name)
        return row

    # ── 가드 ────────────────────────────────────────────────────────────────────────

    async def _assert_role_free(self, role_arn: str, *, except_name: str | None = None) -> None:
        other = (
            await self._s.execute(select(AwsAccount).where(AwsAccount.role_arn == role_arn))
        ).scalar_one_or_none()
        if other is not None and other.name != except_name:
            raise ConflictError(
                f"role {role_arn} is already registered as '{other.name}'. One role is one "
                "account entry — select that entry on the alias instead."
            )

    async def _assert_matches_app_profiles(self, role_arn: str, external_id: str | None) -> None:
        for p in await self._app_profiles():
            if p.account_role_arn == role_arn and (p.external_id or None) != (external_id or None):
                raise ConflictError(
                    f"role {role_arn} is also the account of app '{p.client}' (routing profile), "
                    "with a different ExternalId. The role's trust policy may accept only one of "
                    "them, so the other would fail at AssumeRole. Use the same ExternalId."
                )

    # ── 쓰기 ────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        data: AwsAccountCreateRequest,
        *,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AccountMutation:
        if await self._s.get(AwsAccount, data.name) is not None:
            raise ConflictError(f"AWS account '{data.name}' already exists")
        await self._assert_role_free(data.role_arn)
        await self._assert_matches_app_profiles(data.role_arn, data.external_id)
        row = AwsAccount(
            name=data.name,
            display_name=data.display_name,
            role_arn=data.role_arn,
            external_id=data.external_id,
            description=data.description,
            created_by=actor.user_id,
        )
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError as exc:  # 같은 이름·역할의 동시 등록
            raise ConflictError(
                f"AWS account '{data.name}' conflicts with an existing entry"
            ) from exc
        await self._s.refresh(row)
        await audit_logger.log(
            self._s,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CREATE_AWS_ACCOUNT",
            resource_type="AwsAccount",
            resource_id=row.name,
            changes={
                "after": {
                    "name": row.name,
                    "role_arn": row.role_arn,
                    **_external_id_audit(None, row.external_id),
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        # 새 항목은 아직 어떤 alias 도 고르지 않았다 — 낡을 수 있는 캐시가 없다.
        return AccountMutation(AwsAccountResponse.build(row, aliases=[], app_profiles=[]), [])

    async def update(
        self,
        name: str,
        data: AwsAccountUpdateRequest,
        *,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AccountMutation:
        row = await self._get(name)
        fields = data.model_fields_set
        before = (row.role_arn, row.external_id)

        new_role = data.role_arn if "role_arn" in fields else row.role_arn
        new_ext = data.external_id if "external_id" in fields else row.external_id
        if new_role != row.role_arn:
            await self._assert_role_free(new_role, except_name=name)
        if (new_role, new_ext) != before:
            await self._assert_matches_app_profiles(new_role, new_ext)

        if "display_name" in fields:
            row.display_name = data.display_name
        if "description" in fields:
            row.description = data.description
        row.role_arn = new_role
        row.external_id = new_ext
        try:
            await self._s.flush()  # 역할/ExternalId 가 바뀌었으면 trg_aws_account_propagate 발화
        except IntegrityError as exc:
            raise ConflictError(f"role {new_role} conflicts with an existing entry") from exc
        await self._s.refresh(row)

        aliases = (await self._aliases_by_account()).get(name, [])
        identity_changed = (row.role_arn, row.external_id) != before
        keys: list[str] = []
        if identity_changed:
            for m in aliases:
                keys.extend(model_cache_keys(m.alias, m.provider_model_id))
            keys = list(dict.fromkeys(keys))  # model:list 가 alias 마다 한 번씩 들어 있다
        profiles = [
            p.client for p in await self._app_profiles() if p.account_role_arn == row.role_arn
        ]
        await audit_logger.log(
            self._s,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="UPDATE_AWS_ACCOUNT",
            resource_type="AwsAccount",
            resource_id=name,
            changes={
                "before": {"role_arn": before[0]},
                "after": {
                    "role_arn": row.role_arn,
                    **_external_id_audit(before[1], row.external_id),
                    # 청구 결과를, 그것을 일으킨 수정과 함께 남긴다.
                    "aliases_repointed": sorted(m.alias for m in aliases) if identity_changed else [],
                },
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        return AccountMutation(
            AwsAccountResponse.build(row, aliases=[m.alias for m in aliases], app_profiles=profiles),
            keys,
        )

    async def delete(
        self,
        name: str,
        *,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AccountMutation:
        row = await self._get(name)
        users = (await self._aliases_by_account()).get(name, [])
        if users:
            raise ConflictError(
                f"AWS account '{name}' is selected by {len(users)} alias(es): "
                f"{', '.join(sorted(m.alias for m in users))}. Point them at another account "
                "(or the default) first."
            )
        role_arn = row.role_arn
        await self._s.delete(row)
        try:
            await self._s.flush()
        except IntegrityError as exc:  # 그 사이 alias 가 이 항목을 골랐다
            raise ConflictError(f"AWS account '{name}' is now selected by an alias") from exc
        await audit_logger.log(
            self._s,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_AWS_ACCOUNT",
            resource_type="AwsAccount",
            resource_id=name,
            changes={"before": {"name": name, "role_arn": role_arn}},
            ip_address=ip_address,
            request_id=request_id,
        )
        return AccountMutation(None, [])
