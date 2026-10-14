# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import structlog
from botocore.credentials import Credentials

from app.schemas.routing import RoutingProfileSchema
from app.services.account_credentials import (
    SESSION_NAME_MANTLE,
    AccountCredentialsUnavailable,
    AccountTarget,
)

logger = structlog.get_logger(__name__)

# Lazy import so the module loads even if the dep is missing in some envs;
# bearer_token() raises clearly if it is truly absent.
try:
    from aws_bedrock_token_generator import BedrockTokenGenerator
except Exception:  # pragma: no cover - exercised only when dep absent
    BedrockTokenGenerator = None  # type: ignore[assignment]

# Refresh assumed creds this many seconds BEFORE their hard expiry.
_CRED_REFRESH_SKEW = 300
# Bearer tokens are minted with a 12h SigV4 expiry by the generator; we re-mint
# more often to stay well within and to follow rotated creds.
_BEARER_TTL = 1800  # 30 min
# A bearer must never outlive the assumed creds it was minted from. Cap its
# expiry at (creds_expiry - this skew) so we never serve a bearer against
# expired creds (would 401).
_BEARER_CRED_SKEW = 60  # 1 min grace before creds hard-expire
_ASSUME_DURATION = 3600  # 1h


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float  # epoch seconds (from STS Expiration)


@dataclass
class _CachedBearer:
    token: str = field(repr=False)  # never expose the bearer in repr/logs
    expires_at: float = 0.0  # epoch seconds from now()


class MantleCredentialBroker:
    """Mint short-lived Mantle bearer tokens, supporting two credential paths.

    - Cross-account (Cowork / 905): ``account_role_arn`` is set → AssumeRole via
      STS to obtain temporary creds, then mint bearer.
    - In-account (Claude Code / 374): ``account_role_arn`` is None → use the
      pod's own IRSA credentials directly (no AssumeRole needed).

    Holds NO long-lived account keys. Caches assumed creds (~1h) and bearer
    tokens (30 min) to avoid per-request STS calls and token minting.

    alias 별 대상 계정(0043): ``bearer_token(profile, account=AccountTarget)`` 이면 프로필
    계정보다 **우선해** 그 계정으로 mint 한다. 자격증명은 세 경로가 공유하는
    ``BedrockAccountClientProvider`` 에서 받고(세션 이름은 이 경로가 쓰던 ``gw-mantle``),
    bearer 는 ((역할, ExternalId), region) 으로 따로 캐시한다. ``account`` 가 없으면 위 두
    경로가 바이트 단위로 그대로다.
    """

    def __init__(
        self,
        sts_client,
        now: Callable[[], float] = time.time,
        account_provider=None,
    ) -> None:
        self._sts = sts_client
        self._now = now
        # BedrockAccountClientProvider. alias 계정 mint 에만 쓴다. None 이면 account 가 온 요청은
        # 프로필/파드 자격증명으로 대신하지 않고 거부한다.
        self._account_provider = account_provider
        self._creds: dict[str, _CachedCreds] = {}
        # Bearer cache is keyed by (cred_key, region): a bearer is region-bound
        # (SigV4 binds to the regional endpoint), so two profiles sharing a role
        # but differing region must not cross-serve tokens.
        # alias 계정 bearer(0043)는 첫 칸이 ("account", 역할, ExternalId) 튜플이다(_account_bearer).
        self._bearers: dict[tuple, _CachedBearer] = {}
        self._lock = asyncio.Lock()

    def _in_account_creds(self) -> Credentials:
        """Pod's own IRSA credentials (no AssumeRole) for in-account Mantle (333)."""
        import boto3
        raw = boto3.Session().get_credentials()
        if raw is None:
            raise RuntimeError(
                "No AWS credentials available for in-account Mantle path. "
                "Ensure IRSA is configured on the pod."
            )
        frozen = raw.get_frozen_credentials()
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)

    async def bearer_token(
        self, profile: RoutingProfileSchema, account: AccountTarget | None = None
    ) -> str:
        if BedrockTokenGenerator is None:
            raise RuntimeError("aws-bedrock-token-generator not installed")
        if account is not None:
            return await self._account_bearer(profile, account)

        cred_key = profile.account_role_arn or "in-account"
        bearer_key = (cred_key, profile.region)
        async with self._lock:
            now = self._now()
            cached_bearer = self._bearers.get(bearer_key)
            if cached_bearer and cached_bearer.expires_at > now:
                return cached_bearer.token

            if profile.account_role_arn:
                creds = await self._get_creds(profile.account_role_arn, profile.external_id, now)
                cred_expiry = self._creds[profile.account_role_arn].expires_at
            else:
                creds = await asyncio.get_running_loop().run_in_executor(None, self._in_account_creds)
                cred_expiry = now + _ASSUME_DURATION

            token = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: BedrockTokenGenerator().get_token(creds, profile.region),
            )
            # Never let the bearer outlive the underlying creds it was minted from.
            bearer_expiry = min(now + _BEARER_TTL, cred_expiry - _BEARER_CRED_SKEW)
            self._bearers[bearer_key] = _CachedBearer(token=token, expires_at=bearer_expiry)
            return token

    async def _account_bearer(self, profile: RoutingProfileSchema, account: AccountTarget) -> str:
        """alias 계정으로 mint 한 bearer. region 은 프로필 그대로(bearer 는 region-bound).

        캐시 키의 첫 칸이 튜플이라 프로필 경로의 키(역할 문자열 / ``"in-account"``)와 겹치지 않고,
        프로필 경로의 재assume 무효화(``k[0] != role_arn``)에도 걸리지 않는다 — 이 bearer 는 그
        자격증명에서 나오지 않았다. ExternalId 가 키에 있어야, 같은 역할을 ExternalId 없이 지목한
        alias 가 ExternalId 로 assume 한 bearer 를 받지 않는다.
        """
        if self._account_provider is None:
            raise AccountCredentialsUnavailable(
                account.role_arn,
                "alias requests a target AWS account but no account provider is wired into "
                "MantleCredentialBroker; refusing to mint with the profile's or pod's credentials",
            )
        bearer_key = (("account", account.role_arn, account.external_id or ""), profile.region)
        cached_bearer = self._bearers.get(bearer_key)
        if cached_bearer and cached_bearer.expires_at > self._now():
            return cached_bearer.token
        # STS 왕복은 브로커의 전역 lock **밖에서** 한다(provider 가 키별 lock 을 갖는다). 안에서
        # 하면 느리거나 실패하는 alias 계정 하나가 라이브 프로필 경로(cowork)의 bearer mint 까지
        # 그 왕복 뒤에 줄 세운다.
        creds = await self._account_provider.credentials(account, session_name=SESSION_NAME_MANTLE)
        async with self._lock:
            now = self._now()
            cached_bearer = self._bearers.get(bearer_key)
            if cached_bearer and cached_bearer.expires_at > now:
                return cached_bearer.token
            cred_expiry = self._account_provider.expires_at(
                account, session_name=SESSION_NAME_MANTLE
            ) or (now + _ASSUME_DURATION)
            token = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: BedrockTokenGenerator().get_token(creds, profile.region),
            )
            # 프로필 경로와 같은 상한: bearer 가 그 자격증명보다 오래 살지 않는다.
            bearer_expiry = min(now + _BEARER_TTL, cred_expiry - _BEARER_CRED_SKEW)
            self._bearers[bearer_key] = _CachedBearer(token=token, expires_at=bearer_expiry)
            return token

    async def _get_creds(
        self, role_arn: str, external_id: Optional[str], now: float
    ) -> Credentials:
        cached = self._creds.get(role_arn)
        if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
            return cached.creds

        kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-mantle",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id

        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._sts.assume_role(**kwargs)
        )
        c = resp["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        expires_at = c["Expiration"].timestamp()
        self._creds[role_arn] = _CachedCreds(creds=creds, expires_at=expires_at)
        # invalidate any stale bearer (all regions) minted from older creds
        self._bearers = {k: v for k, v in self._bearers.items() if k[0] != role_arn}
        return creds
