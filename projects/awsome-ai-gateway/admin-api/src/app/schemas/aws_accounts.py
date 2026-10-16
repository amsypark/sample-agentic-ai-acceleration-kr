# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS 계정 레지스트리(마이그레이션 0044) — 요청·응답 모델.

계정은 한 번 등록하고 alias 에서 ``name`` 으로 고른다. 검증은 DB CHECK(ck_aws_accounts_name /
_role_arn / _external_id)와 같다 — 오타는 필드 이름이 붙은 422 여야 하고, 운영자에게
"Request failed" 로 보이는 IntegrityError 여서는 안 된다.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.models import _validate_account_name, _validate_external_id, _validate_role_arn


def account_id_of(role_arn: str | None) -> str:
    """``arn:aws:iam::<account>:role/<name>`` 의 12자리 계정 번호(형식 검증된 값 기준)."""
    parts = (role_arn or "").split(":")
    return parts[4] if len(parts) > 4 else ""


class AwsAccountCreateRequest(BaseModel):
    # extra="forbid": 오타 키가 201 과 함께 조용히 버려지면 안 된다(models.py 의 같은 근거).
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    role_arn: str = Field(max_length=2048)
    external_id: str | None = Field(default=None, max_length=1224)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        out = _validate_account_name(v)
        assert out is not None
        return out

    @field_validator("role_arn")
    @classmethod
    def _v_role(cls, v: str) -> str:
        out = _validate_role_arn(v)
        assert out is not None
        return out

    _v_ext = field_validator("external_id")(_validate_external_id)


class AwsAccountUpdateRequest(BaseModel):
    """부분 수정. ``name`` 은 바꿀 수 없다(FK 대상이자 로그에 보이는 식별자) — 이름을 바꾸려면
    새로 등록하고 alias 를 옮긴다.

    이 API 의 다른 곳과 같은 세 상태: 키 없음 = 유지, 명시적 null = 지움(nullable 인
    ``external_id`` / ``description`` 만), 값 = 설정.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role_arn: str | None = Field(default=None, max_length=2048)
    external_id: str | None = Field(default=None, max_length=1224)
    description: str | None = None

    _v_role = field_validator("role_arn")(_validate_role_arn)
    _v_ext = field_validator("external_id")(_validate_external_id)

    @model_validator(mode="after")
    def _not_null_fields(self):
        # display_name / role_arn 은 NOT NULL 이다. 명시적 null 을 무시하면 일어나지 않은 변경에
        # 성공 토스트가 뜬다.
        for f in ("display_name", "role_arn"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be cleared; omit it to keep the current value")
        return self


class AwsAccountResponse(BaseModel):
    name: str
    display_name: str
    role_arn: str
    #: role_arn 에서 유도 — 콘솔 표와 모델 표에 보인다.
    account_id: str
    external_id: str | None = None
    description: str | None = None
    #: 이 계정을 고른 alias — 그 호출이 이 계정으로 서명된다.
    aliases: list[str] = Field(default_factory=list)
    #: 같은 역할을 앱 단위 계정(routing_profiles.account_role_arn)으로도 쓰는 앱. 정보용 —
    #: 그 앱 단위 설정은 이 레지스트리가 관리하지 않는다.
    app_profiles: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def build(cls, row, *, aliases: list[str], app_profiles: list[str]) -> AwsAccountResponse:
        return cls(
            name=row.name,
            display_name=row.display_name,
            role_arn=row.role_arn,
            account_id=account_id_of(row.role_arn),
            external_id=row.external_id,
            description=row.description,
            aliases=sorted(aliases),
            app_profiles=sorted(app_profiles),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AppAccountItem(BaseModel):
    """앱 단위(라우팅 프로필) 계정. 콘솔이 "alias 계정을 비우면 무엇인가" 를 설명하는 데 쓴다.

    어느 경로에 적용되는지는 오늘 규칙 그대로다: ``backend == "invoke"`` 면 그 앱의 Bedrock
    native 호출(claude-code 형), 그 밖에는 그 앱의 Mantle·runtime(OpenAI) 호출. ``/model/*``
    패스스루는 프로필을 보지 않는다.
    """

    client: str
    backend: str
    role_arn: str
    account_id: str
    #: 같은 역할의 레지스트리 항목이 있으면 그 이름.
    registered_as: str | None = None


class AwsAccountListResponse(BaseModel):
    items: list[AwsAccountResponse]
    app_accounts: list[AppAccountItem] = Field(default_factory=list)
