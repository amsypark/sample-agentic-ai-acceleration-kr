# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""alias 별 대상 AWS 계정(마이그레이션 0043/0044) — 판정 규칙과 공용 타입.

## 왜 있나

Bedrock 호출의 계정은 **서명한 자격증명**으로만 정해진다. ``bedrock-runtime.<region>``
은 리전 공용 호스트이고 SigV4 credential scope(``<key>/<date>/<region>/bedrock``)에도,
foundation-model ARN(``arn:aws:bedrock:<region>::foundation-model/...`` — 계정 칸이 비어
있다)에도 계정이 없다. 그래서 "이 alias 는 다른 계정에서" 는 "그 계정의 역할을 assume 해
그 계정으로서 서명" 이고, 쿼터·청구·모델 액세스 승인이 그 계정을 따른다.

## 우선순위 — 세 경로(plane) 모두 같다

    1. alias 계정  (model_aliases.account_role_arn — 0044 부터 레지스트리 선택의 사본)
    2. 라우팅 프로필 계정 — **경로별 오늘 규칙 그대로**
         Bedrock native (/v1/messages, count_tokens): backend == "invoke" 이고 역할이 있을 때
                        (claude-code 의 cross-account). /model/* 패스스루는 프로필을 보지 않는다.
         bedrock-runtime OpenAI (/v1/chat/completions, /v1/responses): 프로필에 역할이 있으면
         Mantle (/v1/messages Rule A/B, /v1/responses): 프로필에 역할이 있으면
    3. 파드 IRSA

alias 계정이 없는 alias 는 2→3 을 그대로 타므로 0043 이전과 **바이트 단위로 같은** 호출이 된다.
2 단계의 판정은 각 라우터/어댑터의 기존 코드가 계속 한다(그 경로를 이 모듈로 옮기지 않았다).
이 모듈이 정하는 것은 1 단계(:func:`resolve_account_target`)와, 실제로 서빙한 계정의 기록
(:func:`effective_account_id`)뿐이다.

## alias 계정은 바꾸는 것이 서명 주체뿐이다

리전·URL 은 그대로다 — native 는 그 요청의 리전(프로필 cross-account 면 profile.region, 아니면
파드 리전), runtime 은 alias endpoint 호스트, Mantle 은 profile.region. 청구 계정을 바꾸려다
데이터가 다른 지역으로 가는 일이 생기지 않게 한다.

## 실패 — 폴백하지 않는다

alias 계정의 AssumeRole 이 실패하면 세 경로 모두 같은 답을 낸다: HTTP 500 +
:data:`ACCOUNT_UNAVAILABLE_BODY`. 502/503/504 가 아닌 이유: 그 셋은 폴백 루프의 "다른 후보로"
상태이고(fallback_loop._FALLBACK_STATUSES) 502/503 은 모델 id 로 키를 잡은 서킷 브레이커를
올린다 — 계정 하나의 설정 오류가 다른 계정의 같은 모델 alias 까지 끊고, 요청은 다른 계정으로
청구되는 후보가 대신 받는다.

in-account 로 **투명 폴백하지도 않는다.** 그것은 성공하지만 운영자가 고른 계정이 아닌 곳으로
청구되는 응답이고, 어떤 상태 코드도 그것을 드러내지 않는다. 라우팅 프로필 계정(claude-code)의
기존 투명 폴백(``BedrockAdapter._get_client``)은 그대로 둔다 — 그건 앱 단위 가용성 결정이고,
alias 계정이 없는 요청에만 걸린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: RoleSessionName — 경로(transport)마다 **이미 쓰던 이름**을 alias 계정에도 쓴다. 대상 계정의
#: CloudTrail·메트릭 필터·``sts:RoleSessionName`` 조건이 계정 출처(alias/프로필)와 무관하게
#: 경로 하나에 이름 하나로 맞는다.
SESSION_NAME_NATIVE = "gw-bedrock-xacct"  # bedrock_account_client (claude-code 프로필 경로와 같다)
SESSION_NAME_RUNTIME = "gw-bedrock-openai"  # sigv4_signer
SESSION_NAME_MANTLE = "gw-mantle"  # mantle_credentials

#: alias 계정을 쓸 수 없을 때의 단일 응답. 상태 코드가 폴백·서킷 브레이커 집합 밖이어야 한다
#: (모듈 docstring). 본문은 고정이다 — 역할 ARN·STS 오류를 클라이언트에 노출하지 않는다.
ACCOUNT_UNAVAILABLE_STATUS = 500
ACCOUNT_UNAVAILABLE_CODE = "AccountCredentialsUnavailable"
ACCOUNT_UNAVAILABLE_MESSAGE = (
    "The AWS account configured for this model could not be used (AssumeRole failed). "
    "Contact the gateway administrator."
)
ACCOUNT_UNAVAILABLE_BODY = (
    b'{"error":{"type":"api_error","message":"' + ACCOUNT_UNAVAILABLE_MESSAGE.encode() + b'"}}'
)


class AccountCredentialsUnavailable(RuntimeError):
    """대상 계정의 자격증명을 이번 요청에 쓸 수 없다.

    타입이 있어야 세 경로가 같은 응답(:data:`ACCOUNT_UNAVAILABLE_STATUS`)으로 매핑한다.
    RuntimeError 하위라 기존 ``except Exception`` / ``except RuntimeError`` 는 그대로 잡는다 —
    claude-code 프로필 경로의 투명 폴백이 이 타입 때문에 바뀌지 않는다.
    """

    def __init__(self, role_arn: str | None, reason: str) -> None:
        self.role_arn = role_arn
        self.reason = reason
        super().__init__(f"{reason} (role_arn={role_arn})")


@dataclass(frozen=True)
class AccountTarget:
    """한 요청을 서명할 계정. 역할과 ExternalId 를 한 객체로 묶는다.

    느슨한 kwarg 두 개면 호출 지점 하나가 역할만 넘기고 ExternalId 를 떨어뜨릴 수 있다 — 그
    결과는 ExternalId 를 요구하는 역할에서 STS AccessDenied 이고, 배선 버그가 권한 문제처럼
    읽힌다.
    """

    role_arn: str
    #: repr 에서 뺀다 — confused-deputy 를 막는 공유 비밀이고, 이 객체는 로그·예외에 repr 로 섞이기
    #: 쉽다. 동등 비교·해시에는 그대로 들어간다(캐시 키가 ExternalId 로 갈려야 한다).
    external_id: str | None = field(default=None, repr=False)
    #: 로그용 출처. ``"alias"`` 또는 ``"profile"``.
    source: str = "alias"


def resolve_account_target(model_config) -> AccountTarget | None:
    """**alias** 의 대상 계정. ``None`` = alias 계정 없음 → 경로별 오늘 규칙(프로필 → 파드).

    프로필을 받지 않는다. 프로필 계정의 적용 범위는 경로마다 다르고(native 는 backend=invoke
    인 프로필만), 그 판정은 각 경로의 기존 코드가 한다. 여기서 프로필로 폴백하면 그 경로별
    규칙이 한 줄로 뭉개져, 예컨대 Mantle 용 역할을 가진 cowork 프로필이 cowork 의 native 호출까지
    그 계정으로 옮긴다 — 200 이 나고 청구만 다른 계정으로 가는, 어떤 로그에도 안 남는 이동이다
    (이 기능의 이식 원본 첫 버전이 실제로 그랬다).

    ``getattr`` 로 읽는다. 0043 이전 필드가 없는 캐시 payload·테스트 대역이 요청 경로에서
    AttributeError(=500) 가 되면 안 된다 — 그 경우는 그냥 "alias 계정 없음" 이다.
    """
    role = getattr(model_config, "account_role_arn", None)
    if not role:
        return None
    return AccountTarget(
        role_arn=role,
        external_id=getattr(model_config, "account_external_id", None) or None,
        source="alias",
    )


def profile_account_target(profile) -> AccountTarget | None:
    """라우팅 프로필의 계정(역할이 있으면). 적용 여부는 호출하는 경로가 정한다.

    ⚠️ 필드 이름이 alias 와 다르다: routing_profiles 는 ``external_id``, model_aliases 는
       ``account_external_id``. 잘못 읽으면 기존 cross-account 프로필의 ExternalId 가 전부
       빠지고, 그것은 신뢰 정책 문제처럼 보이는 AccessDenied 로 나타난다.
    """
    role = getattr(profile, "account_role_arn", None)
    if not role:
        return None
    return AccountTarget(
        role_arn=role,
        external_id=getattr(profile, "external_id", None) or None,
        source="profile",
    )


#: 계정으로 서명되는 provider. OPENMODEL 은 AWS 호출이 아니다 — 행에 역할이 잘못 남아 있어도
#: 온프렘 호출이 AWS 청구로 기록되면 안 된다.
_AWS_PROVIDERS = frozenset(
    {"BEDROCK", "BEDROCK_MANTLE", "BEDROCK_MANTLE_OPENAI", "BEDROCK_RUNTIME_OPENAI"}
)


def account_id_of(role_arn: str | None) -> str | None:
    """``arn:aws:iam::<12자리>:role/...`` 의 계정 번호. 형식이 아니면 None(쓰레기 대신)."""
    if not role_arn:
        return None
    parts = role_arn.split(":")
    if len(parts) > 4 and len(parts[4]) == 12 and parts[4].isdigit():
        return parts[4]
    return None


_ARN_RE = re.compile(r"arn:aws[^\s\"'\\]*")
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


def redact_account_identifiers(text: str) -> str:
    """클라이언트로 가는 상류 오류 문장에서 ARN 과 12자리 계정 번호만 지운다.

    AccessDenied 메시지에는 서명한 **대상 계정의** assumed-role ARN 이 그대로 들어 있고, 그 본문은
    가상 키를 가진 누구에게나 간다. 메시지를 통째로 일반 문구로 바꾸지 않는 이유: 클라이언트가
    그 문장으로 동작한다(Claude Code 는 ``prompt is too long`` 을 보고 대화를 압축한다). 원문은
    각 어댑터가 서버 로그에 남긴다.
    """
    return _ACCOUNT_ID_RE.sub("<account-id>", _ARN_RE.sub("<arn>", text))


def redact_account_identifiers_bytes(body: bytes) -> bytes:
    """:func:`redact_account_identifiers` 의 바이트판 — 상류 오류 본문 그대로 넘기는 경로용."""
    if b"arn:aws" not in body and not _ACCOUNT_ID_RE.search(body.decode("utf-8", "replace")):
        return body  # 바꿀 것이 없으면 원본 바이트 그대로(디코딩 왕복 없음)
    return redact_account_identifiers(body.decode("utf-8", "replace")).encode()


def effective_account_id(
    model_config, profile_account: AccountTarget | None = None
) -> str | None:
    """요청을 **실제로 서빙한** 12자리 계정 — ``usage_logs.aws_account_id`` (0045).

    서명 규칙과 똑같다: alias 계정이 있으면 그것, 없으면 ``profile_account``, 그것도 없으면
    None(= 게이트웨이 자신의 계정). ``profile_account`` 는 호출한 라우터가 **그 경로가 실제로
    서명에 쓴** 프로필 계정을 넘긴다 — native 는 claude-code 형 cross-account 이고 AssumeRole 이
    성공했을 때만, runtime/Mantle 은 프로필에 역할이 있을 때. 정산은 이 값을 보고 행의
    ``bedrock_request_id`` 를 어느 계정의 invocation log 에서 찾을지 정한다.
    """
    provider = getattr(model_config, "provider", None)
    provider = getattr(provider, "value", provider)  # ProviderType 또는 문자열
    if provider not in _AWS_PROVIDERS:
        return None
    target = resolve_account_target(model_config) or profile_account
    return account_id_of(target.role_arn) if target else None
