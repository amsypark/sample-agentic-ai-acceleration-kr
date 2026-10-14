# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Cross-account Bedrock credential + client broker.

원래 역할(claude-code → 다른 계정 native): Bedrock NATIVE(boto3 invoke_model)는 Mantle 과 달리
cross-account 를 미지원했다(startup 에 in-account 클라이언트 고정). 이 브로커는 대상 계정
role 을 STS AssumeRole 하여 그 계정의 bedrock-runtime 클라이언트를 빌드·캐시한다.

alias 별 대상 계정(0043/0044) 이후 이 클래스는 **alias 계정 자격증명의 단일 캐시**이기도 하다:

  * ``get_client(role, region, external_id)`` — 라우팅 프로필 경로(claude-code). 동작은 그대로다:
    같은 STS 인자(RoleSessionName ``gw-bedrock-xacct``, 1h, ExternalId), 만료 300s 전 재빌드,
    실패는 raise 해 호출자(BedrockAdapter._get_client)가 in-account 로 투명 폴백한다.
    실패를 기억하지 않는다 — 신뢰 정책을 고치면 다음 요청이 바로 그 계정으로 간다(오늘과 같다).
  * ``get_account_client(target, region)`` — alias 계정의 native 호출.
  * ``credentials(target, session_name=...)`` — alias 계정의 runtime(SigV4)·Mantle(bearer) 서명.
    세 경로가 같은 (역할, ExternalId, 세션 이름) 키를 공유하므로 AssumeRole 이 경로마다
    따로 일어나지 않고, 경로마다 만료 시각을 따로 들고 어긋나지도 않는다.
    실패는 :class:`AccountCredentialsUnavailable` 이고 ``FAILURE_BACKOFF`` 초 동안 기억한다.

- 자격증명 캐시 키 = (role_arn, external_id, session_name). ExternalId 가 키에 없으면 같은 역할을
  ExternalId 없이 지목한 요청이 ExternalId 로 assume 한 자격증명을 캐시 적중으로 받는다 —
  confused-deputy 검사가 매 적중마다 건너뛰어진다.
- 클라이언트 캐시 키 = (role_arn, region, external_id). region-bound 클라이언트 cross-serve 방지.
  자격증명 객체가 바뀌면(=재assume) 재빌드한다 — static creds 는 botocore 가 갱신하지 않는다.
- 키별 single-flight lock. 전역 lock 하나는 느리거나 실패하는 한 계정의 STS 왕복 뒤에 다른
  모든 계정을 줄 세웠다.
- assume/build 는 blocking → run_in_executor.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from botocore.credentials import Credentials

from app.services.account_credentials import (
    SESSION_NAME_NATIVE,
    AccountCredentialsUnavailable,
    AccountTarget,
)

logger = structlog.get_logger(__name__)

_ASSUME_DURATION = 3600          # 1h STS 세션 (Mantle 브로커와 동일)
_CLIENT_REFRESH_SKEW = 300       # creds 하드만료 이 초 전에 재assume → 클라이언트 재빌드
#: 실패한 AssumeRole 을 기억하는 시간(alias 계정만). 없으면 틀린 ARN/ExternalId 의 바쁜 alias 가
#: 요청마다 STS 왕복을 하나씩 쓰고 그 키의 lock 뒤에 줄을 선다. 짧게 둔다 — 신뢰 정책을 고친
#: 운영자는 재시작 없이 30초 안에 동작을 봐야 한다.
FAILURE_BACKOFF = 30


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float            # epoch sec (STS Expiration)


@dataclass
class _CachedClient:
    client: Any
    creds: Credentials           # 이 클라이언트를 빌드한 자격증명 — 바뀌면 재빌드
    expires_at: float


_CredKey = tuple  # (role_arn, external_id or "", session_name)


class BedrockAccountClientProvider:
    """대상 계정 role 을 assume 하여 자격증명/bedrock-runtime 클라이언트를 vend·캐시."""

    def __init__(self, sts_client, boto_config, now: Callable[[], float] = time.time) -> None:
        self._sts = sts_client
        self._boto_config = boto_config
        self._now = now
        self._creds: dict[_CredKey, _CachedCreds] = {}
        self._clients: dict[tuple[str, str, str], _CachedClient] = {}
        self._failures: dict[_CredKey, tuple[float, str]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    # ── 자격증명 ────────────────────────────────────────────────────────────────────

    async def credentials(
        self,
        target: AccountTarget,
        *,
        session_name: str,
        remember_failure: bool = True,
    ) -> Credentials:
        """``target`` 계정의 자격증명. 실패는 :class:`AccountCredentialsUnavailable`.

        파드 자신의 계정은 여기 오지 않는다 — 그 경로는 각 transport 의 기존 코드(파드 boto3
        클라이언트, SigV4Signer 의 세션, Mantle 브로커의 매 mint 새 세션)가 그대로 맡는다.
        """
        key: _CredKey = (target.role_arn, target.external_id or "", session_name)
        async with self._locks.setdefault(("creds", *key), asyncio.Lock()):
            now = self._now()
            cached = self._creds.get(key)
            if cached and cached.expires_at - _CLIENT_REFRESH_SKEW > now:
                return cached.creds
            failed = self._failures.get(key)
            if remember_failure and failed and failed[0] > now:
                raise AccountCredentialsUnavailable(target.role_arn, failed[1])
            return await self._assume(key, target, now)

    def expires_at(self, target: AccountTarget, *, session_name: str) -> float | None:
        """캐시된 자격증명의 하드 만료(epoch). 없으면 None.

        스스로 갱신할 수 없는 파생물(Mantle bearer)을 만드는 호출자가 자기 TTL 을 이 값에
        맞춰 자르라고 있다 — 그러지 않으면 만료된 자격증명 위의 bearer 가 401 이 된다.
        """
        cached = self._creds.get((target.role_arn, target.external_id or "", session_name))
        return cached.expires_at if cached else None

    async def _assume(self, key: _CredKey, target: AccountTarget, now: float) -> Credentials:
        """호출자가 그 키의 lock 을 잡고 있어야 한다."""
        kwargs: dict[str, Any] = {
            "RoleArn": target.role_arn,
            "RoleSessionName": key[2],
            "DurationSeconds": _ASSUME_DURATION,
        }
        if target.external_id:
            kwargs["ExternalId"] = target.external_id
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._sts.assume_role(**kwargs)
            )
        except Exception as exc:  # botocore ClientError, EndpointConnectionError, ...
            err = getattr(exc, "response", None)
            code = err.get("Error", {}).get("Code") if isinstance(err, dict) else None
            reason = f"AssumeRole failed: {code or type(exc).__name__}"
            self._failures[key] = (now + FAILURE_BACKOFF, reason)
            logger.warning(
                "account_credentials_assume_failed",
                role_arn=target.role_arn,
                source=target.source,
                session_name=key[2],
                with_external_id=bool(target.external_id),
                error_code=code or type(exc).__name__,
            )
            raise AccountCredentialsUnavailable(target.role_arn, reason) from exc
        c = resp["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        self._creds[key] = _CachedCreds(creds=creds, expires_at=c["Expiration"].timestamp())
        self._failures.pop(key, None)
        logger.info(
            "account_credentials_assumed",
            role_arn=target.role_arn,
            source=target.source,
            session_name=key[2],
            with_external_id=bool(target.external_id),
        )
        return creds

    # ── bedrock-runtime 클라이언트 (native) ─────────────────────────────────────────

    async def get_client(self, role_arn: str, region: str, external_id: str | None = None):
        """라우팅 프로필 경로(claude-code cross-account). 실패는 raise → 호출자가 in-account 폴백.

        실패를 기억하지 않는다(``remember_failure=False``) — 오늘 동작 그대로 매 요청이 다시
        assume 을 시도하고, 실패한 요청은 in-account 로 서빙된다.
        """
        target = AccountTarget(role_arn=role_arn, external_id=external_id, source="profile")
        return await self._client(target, region, remember_failure=False)

    async def get_account_client(self, target: AccountTarget, region: str):
        """alias 계정의 native 클라이언트. 실패는 :class:`AccountCredentialsUnavailable`."""
        return await self._client(target, region, remember_failure=True)

    async def _client(self, target: AccountTarget, region: str, *, remember_failure: bool):
        creds = await self.credentials(
            target, session_name=SESSION_NAME_NATIVE, remember_failure=remember_failure
        )
        # 캐시 키에 external_id 포함: external_id 회전/수정 시 stale 클라이언트 재사용을 막는다
        # (external_id 는 assume 자격의 일부라 값이 바뀌면 다른 세션으로 취급해야 함).
        ckey = (target.role_arn, region, target.external_id or "")
        async with self._locks.setdefault(("client", *ckey), asyncio.Lock()):
            cached = self._clients.get(ckey)
            # 자격증명 객체가 같으면 재사용 — 재assume 은 새 객체를 돌려주므로 그게 곧 갱신 신호다.
            if cached is not None and cached.creds is creds:
                return cached.client
            client = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._build_client(region, creds)
            )
            expires_at = self.expires_at(target, session_name=SESSION_NAME_NATIVE) or 0.0
            self._clients[ckey] = _CachedClient(client=client, creds=creds, expires_at=expires_at)
            logger.info(
                "bedrock_xacct_client_built",
                role_arn=target.role_arn,
                region=region,
                source=target.source,
            )
            return client

    def _build_client(self, region: str, creds: Credentials):
        # in-account 클라이언트와 같은 BotoConfig(main.py) — read_timeout·retries·pool 이 같아야
        # 계정만 바뀌고 나머지 동작은 그대로다.
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=self._boto_config,
            aws_access_key_id=creds.access_key,
            aws_secret_access_key=creds.secret_key,
            aws_session_token=creds.token,
        )
