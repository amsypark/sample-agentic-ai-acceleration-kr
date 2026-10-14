# Copyright 2026 © Amazon.com and Affiliates.

"""alias 별 대상 AWS 계정(마이그레이션 0043) — 부품 단위.

이 기능의 실패 모드는 예외가 아니다. 계정 B 로 서명했어야 할 요청이 파드(또는 프로필) 자격증명
으로 나가면 **200** 이 온다 — 모델은 답하고, 스트림은 끝나고, 증거는 계정 A 의 청구서와 비어 있는
계정 B 의 CloudTrail 뿐이다. 그래서 여기 테스트는 대부분 "조용히 다른 계정으로 가지 않는다" 를
값으로 단정한다:

  * resolver 는 alias 계정만 본다(프로필로 폴백하지 않는다),
  * 공유 자격증명 캐시의 키·갱신·실패 기억·single-flight,
  * 세 경로의 어댑터가 alias 계정의 자격증명으로 서명하고, 배선이 없으면 파드로 대신하지 않는다,
  * 계정이 없는 호출은 각 경로의 오늘 동작 그대로다(프로필 경로 포함).

요청 처리기 전체를 통과하는 우선순위 검사는 ``test_multi_account_precedence.py``, 서빙 계정 기록은
``test_multi_account_attribution.py`` 에 있다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from app.providers.bedrock_adapter import BedrockAdapter
from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
from app.providers.mantle_adapter import MantleAdapter
from app.providers.mantle_openai_adapter import MantleOpenAIAdapter
from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.schemas.routing import RoutingProfileSchema
from app.services import mantle_credentials as mc
from app.services.account_credentials import (
    ACCOUNT_UNAVAILABLE_BODY,
    ACCOUNT_UNAVAILABLE_STATUS,
    SESSION_NAME_MANTLE,
    SESSION_NAME_NATIVE,
    SESSION_NAME_RUNTIME,
    AccountCredentialsUnavailable,
    AccountTarget,
    profile_account_target,
    resolve_account_target,
)
from app.services.bedrock_account_client import FAILURE_BACKOFF, BedrockAccountClientProvider
from app.services.sigv4_signer import SigV4Signer

pytestmark = pytest.mark.asyncio

ROLE_ALIAS = "arn:aws:iam::111122223333:role/in-region-bedrock"
ROLE_PROFILE = "arn:aws:iam::444455556666:role/app-bedrock"
RUNTIME_ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"


def _cfg(role=None, ext=None, provider=ProviderType.BEDROCK):
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-haiku-5-5",
        alias="a",
        provider=provider,
        api_format=ApiFormat.BEDROCK_NATIVE,
        pricing=ModelPricingSchema(input_per_1k=0, output_per_1k=0),
        status=ModelStatus.ACTIVE,
        account_role_arn=role,
        account_external_id=ext,
    )


def _profile(role=None, ext=None, region="us-west-2", backend="mantle"):
    return RoutingProfileSchema(
        client="cowork",
        backend=backend,
        account_role_arn=role,
        region=region,
        default_model=None,
        external_id=ext,
        enabled=True,
    )


def _sts(*, fail_code=None, expires_in=3600, base=1000.0, delay=0.0, per_role_keys=False):
    """STS 대역. 성공한 호출마다 **다른** 키를 돌려줘 어느 AssumeRole 의 자격증명인지 구별한다."""
    calls: list[dict] = []

    def _assume(**kw):
        calls.append(kw)
        if delay:
            time.sleep(delay)
        if fail_code:
            raise ClientError({"Error": {"Code": fail_code, "Message": "nope"}}, "AssumeRole")
        n = len(calls)
        key = (f"ASIA_{kw['RoleArn'].split(':')[4]}_{n}") if per_role_keys else f"ASIA{n}"
        return {
            "Credentials": {
                "AccessKeyId": key,
                "SecretAccessKey": "s",
                "SessionToken": f"t{n}",
                "Expiration": dt.datetime.fromtimestamp(base + expires_in, dt.UTC),
            }
        }

    sts = MagicMock()
    sts.assume_role = MagicMock(side_effect=_assume)
    return sts, calls


def _provider(sts, now=lambda: 1000.0, config=None):
    return BedrockAccountClientProvider(sts_client=sts, boto_config=config, now=now)


# ── resolver: alias 계정만 ──────────────────────────────────────────────────────────


async def test_resolver_returns_the_alias_account_with_its_external_id():
    assert resolve_account_target(_cfg(ROLE_ALIAS, "alias-ext")) == AccountTarget(
        ROLE_ALIAS, "alias-ext", source="alias"
    )


async def test_no_alias_account_resolves_to_none_whatever_the_profile_says():
    """None 은 "이 계정" 도 "프로필 계정" 도 아니다 — 경로별 오늘 규칙으로 넘어간다는 뜻이다."""
    assert resolve_account_target(_cfg()) is None
    assert resolve_account_target(None) is None


async def test_resolver_tolerates_payloads_that_predate_the_fields():
    """0043 이전 캐시 payload(TTL 300s)·테스트 대역.

    속성 접근이 요청 경로의 500 이 되면 안 된다.
    """

    class Bare:
        pass

    assert resolve_account_target(Bare()) is None


async def test_an_empty_external_id_is_not_sent():
    assert resolve_account_target(_cfg(ROLE_ALIAS, "")).external_id is None


async def test_profile_account_target_reads_the_profile_field_names():
    """routing_profiles 는 ``external_id``, model_aliases 는 ``account_external_id`` 다."""
    assert profile_account_target(_profile(ROLE_PROFILE, "prof-ext")) == AccountTarget(
        ROLE_PROFILE, "prof-ext", source="profile"
    )
    assert profile_account_target(_profile()) is None
    assert profile_account_target(None) is None


async def test_the_resolver_cannot_be_handed_a_profile():
    """구조 고정: 프로필 폴백을 resolver 안에 넣으면 경로별 규칙이 뭉개진다(모듈 docstring)."""
    import inspect

    assert list(inspect.signature(resolve_account_target).parameters) == ["model_config"]


# ── 공유 자격증명 캐시 ──────────────────────────────────────────────────────────────


async def test_assume_role_once_per_target_with_the_external_id_and_session_name():
    sts, calls = _sts()
    prov = _provider(sts)
    t = AccountTarget(ROLE_ALIAS, "ext-1")
    c1 = await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    c2 = await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    assert c1 is c2 and len(calls) == 1
    assert calls[0] == {
        "RoleArn": ROLE_ALIAS,
        "RoleSessionName": "gw-bedrock-openai",
        "DurationSeconds": 3600,
        "ExternalId": "ext-1",
    }


async def test_external_id_is_omitted_when_not_configured():
    """None 이나 "" 로 보내지 않는다 — STS 의 ExternalId 는 최소 2자라 빈 값은 요청 검증에서
    거부된다. 설정하지 않은 값은 키째 뺀다(앱 프로필 경로와 같은 규칙)."""
    sts, calls = _sts()
    await _provider(sts).credentials(AccountTarget(ROLE_ALIAS), session_name=SESSION_NAME_RUNTIME)
    assert "ExternalId" not in calls[0]


async def test_a_target_without_the_external_id_never_reuses_credentials_assumed_with_it():
    """confused-deputy: (역할) 로만 키를 잡으면 ExternalId 없이 지목한 요청이 ExternalId 로 assume
    한 자격증명을 캐시 적중으로 받는다 — 그 검사가 매 적중마다 건너뛰어진다."""
    sts, calls = _sts()
    prov = _provider(sts)
    a = await prov.credentials(
        AccountTarget(ROLE_ALIAS, "secret"), session_name=SESSION_NAME_NATIVE
    )
    b = await prov.credentials(AccountTarget(ROLE_ALIAS, None), session_name=SESSION_NAME_NATIVE)
    assert a is not b and len(calls) == 2
    assert calls[0]["ExternalId"] == "secret" and "ExternalId" not in calls[1]


async def test_each_transport_keeps_its_cloudtrail_session_name():
    """경로마다 쓰던 RoleSessionName 그대로.

    대상 계정의 CloudTrail·``sts:RoleSessionName`` 조건이 계정 출처와 무관하게 맞는다.
    """
    sts, calls = _sts()
    prov = _provider(sts)
    t = AccountTarget(ROLE_ALIAS)
    for name in (SESSION_NAME_NATIVE, SESSION_NAME_RUNTIME, SESSION_NAME_MANTLE):
        await prov.credentials(t, session_name=name)
    assert [c["RoleSessionName"] for c in calls] == [
        "gw-bedrock-xacct",
        "gw-bedrock-openai",
        "gw-mantle",
    ]


async def test_credentials_are_reassumed_exactly_at_the_refresh_skew():
    now = [1000.0]
    sts, calls = _sts(expires_in=3600, base=1000.0)
    prov = _provider(sts, now=lambda: now[0])
    t = AccountTarget(ROLE_ALIAS)
    await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    now[0] = 1000.0 + 3600 - 300 - 1  # 경계 바로 안
    await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    assert len(calls) == 1
    now[0] = 1000.0 + 3600 - 300 + 1  # 경계 바로 밖
    await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    assert len(calls) == 2


async def test_an_alias_assume_failure_is_typed_and_remembered_for_the_backoff():
    now = [1000.0]
    sts, calls = _sts(fail_code="AccessDenied")
    prov = _provider(sts, now=lambda: now[0])
    t = AccountTarget(ROLE_ALIAS, "e")
    with pytest.raises(AccountCredentialsUnavailable, match="AccessDenied"):
        await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    with pytest.raises(AccountCredentialsUnavailable):
        await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    assert len(calls) == 1, "실패하는 역할이 요청마다 STS 왕복을 쓰면 안 된다"
    now[0] += FAILURE_BACKOFF + 1
    with pytest.raises(AccountCredentialsUnavailable):
        await prov.credentials(t, session_name=SESSION_NAME_RUNTIME)
    assert len(calls) == 2, "백오프가 지나면 다시 시도한다(운영자가 IAM 을 고쳤을 수 있다)"


async def test_concurrent_first_requests_share_one_assume_role():
    sts, calls = _sts(delay=0.1)
    prov = _provider(sts)
    results = await asyncio.gather(
        *(
            prov.credentials(AccountTarget(ROLE_ALIAS), session_name=SESSION_NAME_RUNTIME)
            for _ in range(5)
        )
    )
    assert len(calls) == 1 and all(r is results[0] for r in results)


async def test_a_slow_role_does_not_block_a_different_role():
    """전역 lock 하나면 느리거나 실패하는 계정 하나의 STS 왕복 뒤에 모든 계정이 줄을 선다."""

    def _assume(**kw):
        if kw["RoleArn"].endswith("slow"):
            time.sleep(0.4)
        return {
            "Credentials": {
                "AccessKeyId": "A",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
            }
        }

    sts = MagicMock()
    sts.assume_role = MagicMock(side_effect=_assume)
    prov = _provider(sts)
    order: list[str] = []

    async def _get(role, tag):
        await prov.credentials(AccountTarget(role), session_name=SESSION_NAME_RUNTIME)
        order.append(tag)

    await asyncio.gather(
        _get("arn:aws:iam::111122223333:role/slow", "slow"),
        _get("arn:aws:iam::444455556666:role/fast", "fast"),
    )
    assert order == ["fast", "slow"]


async def test_expires_at_reports_the_cached_hard_expiry_only():
    sts, _ = _sts(base=1000.0, expires_in=3600)
    prov = _provider(sts)
    t = AccountTarget(ROLE_ALIAS)
    assert prov.expires_at(t, session_name=SESSION_NAME_MANTLE) is None
    await prov.credentials(t, session_name=SESSION_NAME_MANTLE)
    assert prov.expires_at(t, session_name=SESSION_NAME_MANTLE) == 1000.0 + 3600


# ── 프로필 경로(get_client) 는 오늘 그대로 ──────────────────────────────────────────


async def test_the_profile_path_does_not_remember_failures():
    """claude-code 프로필 경로: 실패한 요청은 in-account 폴백으로 서빙되고, **다음 요청은 다시
    assume 을 시도한다**(오늘 동작). 신뢰 정책을 고치면 바로 그 계정으로 돌아가야 한다."""
    now = [1000.0]
    sts, calls = _sts(fail_code="AccessDenied")
    prov = _provider(sts, now=lambda: now[0])
    for _ in range(3):
        with pytest.raises(Exception):
            await prov.get_client(ROLE_PROFILE, "ap-northeast-2", "claude-code-ext")
    assert len(calls) == 3


async def test_the_profile_path_shares_credentials_with_an_alias_on_the_same_role():
    """같은 (역할, ExternalId) 를 alias 도 고르면 자격증명은 하나다.

    두 캐시가 같은 자격증명의 만료를 각자 들고 어긋나지 않는다.
    """
    sts, calls = _sts()
    prov = _provider(sts)
    with patch("app.services.bedrock_account_client.boto3.client", lambda *a, **k: object()):
        await prov.get_client(ROLE_PROFILE, "ap-northeast-2", "x")
        await prov.get_account_client(AccountTarget(ROLE_PROFILE, "x"), "ap-northeast-2")
    assert len(calls) == 1


# ── 경로 1: Bedrock native (boto3) ─────────────────────────────────────────────────


def _boto_client(request_id="req", region="ap-northeast-2"):
    c = MagicMock()
    c.meta.region_name = region
    body = MagicMock(read=lambda: json.dumps({"usage": {"input_tokens": 1}}).encode())
    c.invoke_model = MagicMock(
        return_value={"body": body, "ResponseMetadata": {"RequestId": request_id}}
    )
    c.count_tokens = MagicMock(return_value={"inputTokens": 7})
    c.invoke_model_with_response_stream = MagicMock(
        return_value={"body": iter([]), "ResponseMetadata": {"RequestId": request_id}}
    )
    return c


async def test_native_with_no_account_uses_the_pod_client_unchanged():
    pod = _boto_client("pod-req")
    sts, calls = _sts()
    ad = BedrockAdapter(pod, account_clients=_provider(sts), account_region="ap-northeast-2")
    with patch("app.services.bedrock_account_client.boto3.client", side_effect=AssertionError):
        _s, _b, hdr, _u = await ad.invoke(b"{}", "m")
    assert hdr == {"x-amzn-requestid": "pod-req"} and pod.invoke_model.call_count == 1
    assert calls == []


async def test_native_with_an_account_signs_with_the_assumed_credentials_in_the_same_region():
    pod = _boto_client("pod-req")
    sts, _ = _sts()
    ad = BedrockAdapter(
        pod, account_clients=_provider(sts, config="POD-CONFIG"), account_region="ap-northeast-2"
    )
    built: list[dict] = []

    def _fake(svc, **kw):
        built.append({"svc": svc, **kw})
        return _boto_client("acct-req", kw["region_name"])

    with patch("app.services.bedrock_account_client.boto3.client", _fake):
        _s, _b, hdr, _u = await ad.invoke(b"{}", "m", account=AccountTarget(ROLE_ALIAS))
    assert hdr == {"x-amzn-requestid": "acct-req"}
    assert pod.invoke_model.call_count == 0, "파드 클라이언트로 갔다"
    assert built[0]["svc"] == "bedrock-runtime" and built[0]["aws_access_key_id"] == "ASIA1"
    # 계정만 바뀌고 리전은 그대로 — 청구 변경이 데이터 지역 이동이 되면 안 된다.
    assert built[0]["region_name"] == "ap-northeast-2"
    # 파드 클라이언트와 같은 BotoConfig(read_timeout·retries·pool).
    assert built[0]["config"] == "POD-CONFIG"


async def test_native_account_client_is_reused_until_the_credential_rotates():
    now = [1000.0]
    sts, _ = _sts(base=1000.0, expires_in=3600)
    ad = BedrockAdapter(
        _boto_client(),
        account_clients=_provider(sts, now=lambda: now[0]),
        account_region="ap-northeast-2",
    )
    built: list = []

    def _fake(svc, **kw):
        built.append(kw)
        return _boto_client()

    t = AccountTarget(ROLE_ALIAS)
    with patch("app.services.bedrock_account_client.boto3.client", _fake):
        await ad.invoke(b"{}", "m", account=t)
        await ad.invoke(b"{}", "m", account=t)
        assert len(built) == 1, "요청마다 boto3 클라이언트를 만들면 안 된다(블로킹 + 비용)"
        now[0] = 1000.0 + 3600  # skew 지남 → 재assume → 새 자격증명 → 재빌드
        await ad.invoke(b"{}", "m", account=t)
    assert len(built) == 2


async def test_native_account_failure_is_a_typed_500_on_every_call_shape():
    pod = _boto_client()
    sts, _ = _sts(fail_code="AccessDenied")
    ad = BedrockAdapter(pod, account_clients=_provider(sts), account_region="ap-northeast-2")
    t = AccountTarget(ROLE_ALIAS, "e")
    st, body, hdr, usage = await ad.invoke(b"{}", "m", account=t)
    assert (st, body, hdr, usage.total_tokens) == (
        ACCOUNT_UNAVAILABLE_STATUS,
        ACCOUNT_UNAVAILABLE_BODY,
        {},
        0,
    )
    st, chunks, _h, rid = await ad.invoke_stream(b"{}", "m", account=t)
    assert st == ACCOUNT_UNAVAILABLE_STATUS and rid is None
    assert [c async for c in chunks] == [ACCOUNT_UNAVAILABLE_BODY]
    assert await ad.count_tokens(b"{}", "m", account=t) == (ACCOUNT_UNAVAILABLE_STATUS, 0)
    pod.invoke_model.assert_not_called()
    pod.count_tokens.assert_not_called()


async def test_native_refuses_rather_than_falling_back_when_nothing_is_wired():
    """배선이 없으면 파드로 대신하지 않는다 — 다른 계정에 청구되는 성공 응답이 된다."""
    pod = _boto_client()
    st, _b, _h, _u = await BedrockAdapter(pod).invoke(b"{}", "m", account=AccountTarget(ROLE_ALIAS))
    assert st == ACCOUNT_UNAVAILABLE_STATUS and pod.invoke_model.call_count == 0


async def test_native_alias_account_never_takes_the_profile_fallback():
    """claude-code 형 요청 단위 어댑터(resolver + in-account 폴백)여도, alias 계정 호출은 그
    resolver 를 부르지 않고 실패해도 in-account 로 폴백하지 않는다."""
    inacct = _boto_client("inacct")
    resolver = AsyncMock(side_effect=AssertionError("프로필 resolver 를 불렀다"))
    sts, _ = _sts(fail_code="AccessDenied")
    ad = BedrockAdapter(
        None,
        client_resolver=resolver,
        fallback_client=inacct,
        account_clients=_provider(sts),
        account_region="ap-northeast-2",
    )
    st, _b, _h, _u = await ad.invoke(b"{}", "m", account=AccountTarget(ROLE_ALIAS))
    assert st == ACCOUNT_UNAVAILABLE_STATUS
    resolver.assert_not_called()
    inacct.invoke_model.assert_not_called()
    assert ad.served_by_fallback is False


async def test_a_client_build_failure_on_the_account_path_is_the_same_typed_500():
    sts, _ = _sts()
    ad = BedrockAdapter(
        _boto_client(), account_clients=_provider(sts), account_region="ap-northeast-2"
    )
    with patch(
        "app.services.bedrock_account_client.boto3.client", side_effect=ValueError("bad endpoint")
    ):
        st, body, _h, _u = await ad.invoke(b"{}", "m", account=AccountTarget(ROLE_ALIAS))
    assert (st, body) == (ACCOUNT_UNAVAILABLE_STATUS, ACCOUNT_UNAVAILABLE_BODY)


async def test_profile_fallback_is_reported_for_attribution():
    """프로필 경로가 in-account 로 폴백하면 그 사실을 남긴다.

    0045 기록이 그 요청을 프로필 계정으로 적지 않게 하려는 것이다.
    """
    inacct = _boto_client("inacct")

    async def _fail():
        raise RuntimeError("AssumeRole AccessDenied")

    ad = BedrockAdapter(None, client_resolver=_fail, fallback_client=inacct)
    st, _b, hdr, _u = await ad.invoke(b"{}", "m")
    assert st == 200 and hdr == {"x-amzn-requestid": "inacct"} and ad.served_by_fallback is True

    async def _ok():
        return _boto_client("xacct")

    ad2 = BedrockAdapter(None, client_resolver=_ok, fallback_client=inacct)
    await ad2.invoke(b"{}", "m")
    assert ad2.served_by_fallback is False


# ── 경로 2: bedrock-runtime OpenAI (SigV4) ─────────────────────────────────────────


class _PodSession:
    def get_credentials(self):
        raw = MagicMock()
        raw.get_frozen_credentials.return_value = Credentials("AKIA_POD", "ps", "pt")
        return raw


def _cred_key(headers):
    return re.search(r"Credential=([^/]+)/", headers["Authorization"]).group(1)


def _scope(headers):
    return re.search(r"Credential=[^/]+/(\S+?),", headers["Authorization"]).group(1)


async def test_sigv4_signs_with_the_alias_account_and_keeps_the_same_scope():
    """이 기능의 근거 사실: 계정은 서명 주체로만 정해진다. 키는 바뀌고 scope 는 그대로다."""
    sts, calls = _sts()
    signer = SigV4Signer(sts_client=None, account_provider=_provider(sts))
    signer._session = _PodSession()
    kw = dict(
        method="POST", url=f"{RUNTIME_ENDPOINT}/v1/responses", body=b'{"a":1}', region="us-east-2"
    )
    pod = await signer.sign(**kw)
    acct = await signer.sign(**kw, account=AccountTarget(ROLE_ALIAS, "ext"))
    assert _cred_key(pod) == "AKIA_POD" and _cred_key(acct) == "ASIA1"
    assert _scope(pod) == _scope(acct) and _scope(acct).endswith("/us-east-2/bedrock/aws4_request")
    assert calls[0]["RoleSessionName"] == "gw-bedrock-openai" and calls[0]["ExternalId"] == "ext"


async def test_sigv4_alias_account_wins_over_the_profile_role():
    sts_profile = MagicMock()
    sts_profile.assume_role = MagicMock(side_effect=AssertionError("프로필 역할로 assume 했다"))
    sts, calls = _sts()
    signer = SigV4Signer(sts_client=sts_profile, account_provider=_provider(sts))
    signer._session = _PodSession()
    h = await signer.sign(
        method="POST",
        url=f"{RUNTIME_ENDPOINT}/v1/responses",
        body=b"{}",
        region="us-east-2",
        role_arn=ROLE_PROFILE,
        external_id="prof",
        account=AccountTarget(ROLE_ALIAS),
    )
    assert _cred_key(h) == "ASIA1" and calls[0]["RoleArn"] == ROLE_ALIAS


async def test_sigv4_refuses_rather_than_signing_with_the_pod_identity():
    signer = SigV4Signer()
    signer._session = _PodSession()
    with pytest.raises(AccountCredentialsUnavailable, match="refusing to sign"):
        await signer.sign(
            method="POST",
            url=f"{RUNTIME_ENDPOINT}/v1/responses",
            body=b"{}",
            region="us-east-2",
            account=AccountTarget(ROLE_ALIAS),
        )


async def test_runtime_adapter_passes_the_account_only_when_there_is_one():
    """계정 없는 alias 의 서명 호출 인자는 이 기능 이전과 같아야 한다(account 키조차 없다)."""
    signer = MagicMock()
    signer.sign = AsyncMock(return_value={"Authorization": "x"})
    http = MagicMock()
    resp = MagicMock(status_code=200, content=b'{"usage":{}}', headers={})
    http.post = AsyncMock(return_value=resp)
    ad = BedrockOpenAIAdapter(http_client=http, signer=signer)
    prof = _profile(ROLE_PROFILE, "prof-ext", region="us-east-2")
    await ad.invoke(b"{}", "us.openai.gpt-5.6-terra", profile=prof, endpoint=RUNTIME_ENDPOINT)
    assert "account" not in signer.sign.await_args.kwargs
    assert signer.sign.await_args.kwargs["role_arn"] == ROLE_PROFILE
    await ad.invoke(
        b"{}",
        "us.openai.gpt-5.6-terra",
        profile=prof,
        endpoint=RUNTIME_ENDPOINT,
        account=AccountTarget(ROLE_ALIAS),
    )
    assert signer.sign.await_args.kwargs["account"] == AccountTarget(ROLE_ALIAS)


async def test_runtime_adapter_answers_an_account_failure_with_500_not_502():
    """502 는 폴백 상태이고 서킷 브레이커 실패다(모델 id 키) — 다른 계정의 후보가 대신 받는다."""
    sts, _ = _sts(fail_code="AccessDenied")
    signer = SigV4Signer(account_provider=_provider(sts))
    signer._session = _PodSession()
    http = MagicMock()
    http.post = AsyncMock(side_effect=AssertionError("네트워크까지 갔다"))
    http.stream = MagicMock(side_effect=AssertionError("네트워크까지 갔다"))
    ad = BedrockOpenAIAdapter(http_client=http, signer=signer)
    kw = dict(
        profile=_profile(region="us-east-2"),
        endpoint=RUNTIME_ENDPOINT,
        account=AccountTarget(ROLE_ALIAS),
    )
    st, body, _h, _u = await ad.invoke(b"{}", "us.openai.gpt-5.6-terra", **kw)
    assert (st, body) == (ACCOUNT_UNAVAILABLE_STATUS, ACCOUNT_UNAVAILABLE_BODY)
    for wire in ("responses", "chat"):
        st, gen, _h, rid = await ad.invoke_stream(b"{}", "us.openai.gpt-5.6-terra", wire=wire, **kw)
        frames = [c async for c in gen]
        assert st == ACCOUNT_UNAVAILABLE_STATUS and rid is None and len(frames) == 1
        # 와이어별 프레이밍: chat 은 그대로 클라이언트로 가므로 SSE data 프레임이어야 한다.
        assert frames[0].startswith(b"data: ") == (wire == "chat")


# ── 경로 3: Mantle (bearer) ────────────────────────────────────────────────────────


@pytest.fixture
def _token_gen(monkeypatch):
    minted: list = []

    class _Gen:
        def get_token(self, creds, region):
            minted.append((creds.access_key, region))
            return f"BEARER-{creds.access_key}-{region}"

    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())
    return minted


async def test_mantle_without_an_account_still_uses_the_profile_role_path(_token_gen):
    """라이브 cowork 경로의 무회귀 가드: account 가 없으면 브로커 자신의 프로필 경로(세션 gw-mantle,
    ExternalId 포함)이고 공유 provider 는 건드리지 않는다."""
    sts_profile, profile_calls = _sts()
    shared_sts, shared_calls = _sts()
    broker = mc.MantleCredentialBroker(
        sts_client=sts_profile, now=lambda: 1000.0, account_provider=_provider(shared_sts)
    )
    await broker.bearer_token(_profile(ROLE_PROFILE, "prof-ext"))
    assert (
        profile_calls[0]["RoleArn"] == ROLE_PROFILE and profile_calls[0]["ExternalId"] == "prof-ext"
    )
    assert profile_calls[0]["RoleSessionName"] == "gw-mantle"
    assert shared_calls == []


async def test_mantle_alias_account_overrides_the_profile_and_caches_separately(_token_gen):
    # ★ 키가 역할에 따라 달라야 한다 — bearer 가 `BEARER-{access_key}-{region}` 이라, 모든 역할에
    #   같은 키를 주는 STS 대역이면 올바르게 분리된 두 bearer 가 같아 보인다.
    sts_profile, _ = _sts(per_role_keys=True)
    shared_sts, shared_calls = _sts(per_role_keys=True)
    broker = mc.MantleCredentialBroker(
        sts_client=sts_profile, now=lambda: 1000.0, account_provider=_provider(shared_sts)
    )
    prof = _profile(ROLE_PROFILE, "prof-ext")
    t_alias = await broker.bearer_token(prof, AccountTarget(ROLE_ALIAS, "alias-ext"))
    assert shared_calls[0]["RoleArn"] == ROLE_ALIAS and shared_calls[0]["ExternalId"] == "alias-ext"
    assert shared_calls[0]["RoleSessionName"] == "gw-mantle"
    assert await broker.bearer_token(prof, AccountTarget(ROLE_ALIAS, "alias-ext")) == t_alias
    assert len(_token_gen) == 1
    t_prof = await broker.bearer_token(prof)
    assert t_prof != t_alias and len(_token_gen) == 2
    assert {k.split("_")[1] for k, _ in _token_gen} == {"111122223333", "444455556666"}


async def test_mantle_alias_bearers_are_keyed_by_external_id_too(_token_gen):
    shared_sts, _ = _sts()
    broker = mc.MantleCredentialBroker(
        sts_client=MagicMock(), now=lambda: 1000.0, account_provider=_provider(shared_sts)
    )
    a = await broker.bearer_token(_profile(), AccountTarget(ROLE_ALIAS, "x"))
    b = await broker.bearer_token(_profile(), AccountTarget(ROLE_ALIAS, None))
    assert a != b


async def test_mantle_alias_bearer_never_outlives_its_credentials(_token_gen):
    shared_sts, _ = _sts(base=1000.0, expires_in=600)
    broker = mc.MantleCredentialBroker(
        sts_client=MagicMock(), now=lambda: 1000.0, account_provider=_provider(shared_sts)
    )
    await broker.bearer_token(_profile(region="us-west-2"), AccountTarget(ROLE_ALIAS))
    entry = broker._bearers[(("account", ROLE_ALIAS, ""), "us-west-2")]
    assert entry.expires_at == 1000.0 + 600 - mc._BEARER_CRED_SKEW
    assert entry.expires_at < 1000.0 + mc._BEARER_TTL


async def test_a_slow_alias_account_never_queues_the_live_profile_path(_token_gen):
    """alias 계정의 STS 왕복은 브로커 전역 lock 밖이다 — 라이브 cowork(프로필) mint 가 그 뒤에
    줄 서지 않는다."""
    sts_profile, _ = _sts()
    shared_sts, _ = _sts(delay=0.4)
    broker = mc.MantleCredentialBroker(
        sts_client=sts_profile, now=lambda: 1000.0, account_provider=_provider(shared_sts)
    )
    order: list[str] = []

    async def _alias():
        await broker.bearer_token(_profile(), AccountTarget(ROLE_ALIAS))
        order.append("alias")

    async def _live():
        await asyncio.sleep(0.05)  # alias 호출이 먼저 시작하게
        await broker.bearer_token(_profile(ROLE_PROFILE, "prof-ext"))
        order.append("profile")

    await asyncio.gather(_alias(), _live())
    assert order == ["profile", "alias"]


async def test_mantle_refuses_without_a_provider_instead_of_minting_from_the_profile(_token_gen):
    sts_profile, profile_calls = _sts()
    broker = mc.MantleCredentialBroker(sts_client=sts_profile, now=lambda: 1000.0)
    with pytest.raises(AccountCredentialsUnavailable):
        await broker.bearer_token(_profile(ROLE_PROFILE), AccountTarget(ROLE_ALIAS))
    assert profile_calls == [] and _token_gen == []


@pytest.mark.parametrize("adapter_cls", [MantleAdapter, MantleOpenAIAdapter])
async def test_mantle_adapters_answer_an_account_failure_with_500(adapter_cls, _token_gen):
    shared_sts, _ = _sts(fail_code="AccessDenied")
    broker = mc.MantleCredentialBroker(
        sts_client=MagicMock(), account_provider=_provider(shared_sts)
    )
    http = MagicMock()
    http.post = AsyncMock(side_effect=AssertionError("네트워크까지 갔다"))
    http.stream = MagicMock(side_effect=AssertionError("네트워크까지 갔다"))
    ad = adapter_cls(http_client=http, broker=broker)
    kw = dict(
        profile=_profile(),
        endpoint="https://bedrock-mantle.us-west-2.api.aws/x",
        account=AccountTarget(ROLE_ALIAS),
    )
    st, body, _h, _u = await ad.invoke(b"{}", "m", **kw)
    assert (st, body) == (ACCOUNT_UNAVAILABLE_STATUS, ACCOUNT_UNAVAILABLE_BODY)
    st, gen, _h, _r = await ad.invoke_stream(b"{}", "m", **kw)
    assert st == ACCOUNT_UNAVAILABLE_STATUS and [c async for c in gen] == [ACCOUNT_UNAVAILABLE_BODY]


@pytest.mark.parametrize("adapter_cls", [MantleAdapter, MantleOpenAIAdapter])
async def test_mantle_adapters_call_the_broker_exactly_as_before_without_an_account(adapter_cls):
    """``bearer_token(profile)`` 한 인자 — 계정 없는 호출의 브로커 인자는 이 기능 이전과 같다."""
    broker = MagicMock()
    broker.bearer_token = AsyncMock(return_value="tok")
    http = MagicMock()
    http.post = AsyncMock(return_value=MagicMock(status_code=200, content=b"{}"))
    ad = adapter_cls(http_client=http, broker=broker)
    prof = _profile()
    await ad.invoke(b"{}", "m", profile=prof, endpoint="https://x")
    assert broker.bearer_token.await_args.args == (prof,)
    await ad.invoke(
        b"{}", "m", profile=prof, endpoint="https://x", account=AccountTarget(ROLE_ALIAS)
    )
    assert broker.bearer_token.await_args.args == (prof, AccountTarget(ROLE_ALIAS))


# ── 폴백·서킷 브레이커 밖 ───────────────────────────────────────────────────────────


async def test_the_account_failure_status_is_outside_the_fallback_and_breaker_sets():
    from app.services import fallback_loop as fl

    assert ACCOUNT_UNAVAILABLE_STATUS not in fl._FALLBACK_STATUSES
    assert ACCOUNT_UNAVAILABLE_STATUS not in fl._CB_FAILURE_STATUSES


async def test_the_fixed_body_names_neither_the_role_nor_the_sts_error():
    """클라이언트에게 역할 ARN·STS 오류를 노출하지 않는다 — 운영자 로그에만 남는다."""
    text = ACCOUNT_UNAVAILABLE_BODY.decode()
    assert json.loads(text)["error"]["type"] == "api_error"
    assert "arn:" not in text and "AccessDenied" not in text
