# Copyright 2026 © Amazon.com and Affiliates.

"""모델의 대상 AWS 계정 선택(0044)과 레지스트리 요청 검증 — 단위.

실패 모드는 대개 "성공처럼 보이는 저장" 이다: 계정을 고른 저장이 200 인데 게이트웨이는 옛 계정으로
부르거나(캐시), OPENMODEL alias 에 계정이 저장돼 콘솔에는 보이는데 절대 쓰이지 않거나, 해제
(null)가 무시돼 성공 토스트만 뜨는 것. 트리거·실 DB 왕복은
tests/regression/test_high_model_account_selection_real_db.py 에서 본다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.auth import CurrentUser
from app.core.exceptions import ConflictError, ValidationError
from app.models.auth import UserRole
from app.models.model import ApiFormat, ModelAlias, ModelStatus, Provider
from app.schemas.aws_accounts import (
    AwsAccountCreateRequest,
    AwsAccountUpdateRequest,
    account_id_of,
)
from app.schemas.common import ApiFormatEnum, ProviderEnum
from app.schemas.models import ModelCreateRequest, ModelUpdateRequest, StatusPatchRequest
from app.services.aws_account_service import model_cache_keys
from app.services.model_service import ModelService

ROLE = "arn:aws:iam::111122223333:role/in-region-bedrock"
ADMIN = CurrentUser(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    email="admin@example.invalid",
    role=UserRole.ADMIN,
    team_id=None,
)


# ── 요청 검증 ─────────────────────────────────────────────────────────────────────


def _create(**kw):
    base = dict(
        alias="m", provider=ProviderEnum.BEDROCK, provider_model_id="anthropic.x",
        api_format=ApiFormatEnum.BEDROCK_NATIVE,
        input_price_per_1k_tokens=Decimal("0.001"), output_price_per_1k_tokens=Decimal("0.002"),
    )
    base.update(kw)
    return ModelCreateRequest(**base)


@pytest.mark.parametrize("name", ["inregion-seoul", "acct-111122223333-d99be14a", "abc"])
def test_a_registered_account_name_is_accepted(name):
    assert _create(account_name=name).account_name == name
    assert ModelUpdateRequest(account_name=name).account_name == name


@pytest.mark.parametrize(
    "name",
    ["default", "gateway", "inherit", "none", "ab", "-x1", "x1-", "Upper", "under_score", "acct-1\n"],
)
def test_reserved_and_malformed_account_names_are_a_422(name):
    """예약어는 콘솔의 "기본값" 과 구별되지 않는다 — DB CHECK 와 같은 규칙을 여기서 422 로."""
    with pytest.raises(PydanticValidationError):
        _create(account_name=name)
    with pytest.raises(PydanticValidationError):
        ModelUpdateRequest(account_name=name)


@pytest.mark.parametrize("key", ["account_role_arn", "account_external_id"])
def test_the_copy_columns_cannot_be_written_through_the_model_api(key):
    """역할 ARN·ExternalId 는 레지스트리 소유다. 받아서 버리면 200 과 함께 아무 일도 안 일어난다."""
    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        _create(**{key: ROLE})
    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        ModelUpdateRequest(**{key: ROLE})


def test_update_distinguishes_omitting_the_account_from_clearing_it():
    assert "account_name" not in ModelUpdateRequest(description="x").model_fields_set
    cleared = ModelUpdateRequest(account_name=None)
    assert "account_name" in cleared.model_fields_set and cleared.account_name is None


def _acct(**kw):
    base = dict(name="inregion-seoul", display_name="In-Region Seoul", role_arn=ROLE)
    base.update(kw)
    return AwsAccountCreateRequest(**base)


@pytest.mark.parametrize(
    "role",
    [
        "arn:aws:iam::111122223333:user/someone",  # 사용자 ARN — assume 할 수 없다
        "111122223333",
        "arn:aws:iam::12345:role/x",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude",
        "   ",
        # 와일드카드·가짜 파티션·비 ASCII — 같은 문법이 terraform 에서는 IAM 정책 Resource 다.
        "arn:aws:iam::111122223333:role/*",
        "arn:aws:iam::111122223333:role/gw-*",
        "arn:awsfoo:iam::111122223333:role/x",
        "arn:aws:iam::111122223333:role/역할",
        "arn:aws:iam::111122223333:role/a b",
    ],
)
def test_only_iam_role_arns_are_registered(role):
    with pytest.raises(PydanticValidationError):
        _acct(role_arn=role)


def test_partitions_and_paths_are_accepted():
    assert _acct(role_arn="arn:aws-us-gov:iam::111122223333:role/path/to/r").role_arn.endswith("/r")
    assert _acct(role_arn="arn:aws-iso-b:iam::111122223333:role/a+b=c,d.e@f_g-h").role_arn


def test_the_full_sts_external_id_charset_is_accepted():
    assert _acct(external_id="Ab0_+=,.@:/-").external_id == "Ab0_+=,.@:/-"


@pytest.mark.parametrize("ext", ["x", "has space", "", "a" * 1225, "외부아이디", "ext*", "üext"])
def test_external_ids_outside_the_sts_constraint_are_refused(ext):
    with pytest.raises(PydanticValidationError):
        _acct(external_id=ext)


def test_registry_requests_refuse_unknown_keys():
    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        _acct(rolearn=ROLE)
    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        AwsAccountUpdateRequest(name="renamed")  # 이름은 바꿀 수 없다


@pytest.mark.parametrize("field", ["display_name", "role_arn"])
def test_not_null_registry_fields_cannot_be_cleared(field):
    """명시적 null 을 무시하면 일어나지 않은 변경에 성공 토스트가 뜬다."""
    with pytest.raises(PydanticValidationError, match="cannot be cleared"):
        AwsAccountUpdateRequest(**{field: None})
    assert AwsAccountUpdateRequest(external_id=None).model_fields_set == {"external_id"}


def test_account_id_is_derived_from_the_role():
    assert account_id_of(ROLE) == "111122223333"
    assert account_id_of(None) == ""


def test_model_cache_keys_cover_the_alias_every_model_id_and_the_list():
    assert model_cache_keys("a", "pmid-1", "pmid-1", None) == ["model:a", "model:pmid-1", "model:list"]
    assert model_cache_keys("a", "old", "new") == ["model:a", "model:old", "model:new", "model:list"]


# ── ModelService ──────────────────────────────────────────────────────────────────


def _model(provider=Provider.BEDROCK, account_name=None, alias="claude-x", status=ModelStatus.ACTIVE):
    m = MagicMock(spec=ModelAlias)
    m.alias = alias
    m.provider = provider
    m.provider_model_id = "global.anthropic.claude-x"
    m.endpoint_url = None
    m.api_format = ApiFormat.BEDROCK_NATIVE
    m.status = status
    m.description = None
    m.display_name = None
    m.allowed_clients = None
    m.account_name = account_name
    m.account_role_arn = ROLE if account_name else None
    m.account_external_id = None
    m.created_at = datetime.now(UTC)
    m.updated_at = datetime.now(UTC)
    return m


def _session(registered=("inregion-seoul", "dedicated"), siblings=()):
    """``siblings`` = 같은 모델 id 의 다른 ACTIVE alias 조회가 돌려줄 행(advisory lock 문장은
    결과를 읽지 않으므로 같은 결과 객체를 받아도 된다)."""
    s = AsyncMock()
    s.get = AsyncMock(side_effect=lambda cls, key, **kw: object() if key in registered else None)
    s.flush = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(siblings)
    s.execute = AsyncMock(return_value=result)

    async def _refresh(obj, attribute_names=None):
        # 트리거가 DB 에서 한 일을 흉내 낸다: 선택이 있으면 사본을 채우고, 없으면 비운다.
        obj.account_role_arn = ROLE if obj.account_name else None
        obj.account_external_id = None

    s.refresh = AsyncMock(side_effect=_refresh)
    return s


def _svc():
    cache = MagicMock()
    cache.invalidate = AsyncMock()
    return ModelService(cache_mgr=cache), cache


async def test_create_rejects_an_account_on_an_openmodel_alias_before_writing():
    svc, _ = _svc()
    with patch("app.services.model_service.ModelRepository") as Repo:
        Repo.return_value.alias_exists_ci = AsyncMock(return_value=False)
        Repo.return_value.create_model = AsyncMock()
        with pytest.raises(ValidationError, match="OPENMODEL"):
            await svc.create_model(
                _session(), actor=ADMIN,
                data=_create(provider=ProviderEnum.OPENMODEL, api_format=ApiFormatEnum.OPENAI_COMPATIBLE,
                             account_name="inregion-seoul"),
            )
        Repo.return_value.create_model.assert_not_called()


async def test_create_rejects_an_unregistered_account_with_its_name():
    svc, _ = _svc()
    with patch("app.services.model_service.ModelRepository") as Repo:
        Repo.return_value.alias_exists_ci = AsyncMock(return_value=False)
        Repo.return_value.create_model = AsyncMock()
        with pytest.raises(ValidationError, match="'nope-acct' is not registered"):
            await svc.create_model(_session(), actor=ADMIN, data=_create(account_name="nope-acct"))
        Repo.return_value.create_model.assert_not_called()


async def test_create_stores_the_selection_and_answers_with_the_trigger_copy():
    svc, cache = _svc()
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        repo = Repo.return_value
        repo.alias_exists_ci = AsyncMock(return_value=False)

        async def _create_model(m):
            m.created_at = m.updated_at = datetime.now(UTC)
            m.status = ModelStatus.ACTIVE
            return m

        repo.create_model = AsyncMock(side_effect=_create_model)
        repo.create_pricing = AsyncMock()
        out = await svc.create_model(
            _session(), actor=ADMIN, data=_create(alias="claude-x", account_name="inregion-seoul")
        )
    assert (out.response.account_name, out.response.account_role_arn) == ("inregion-seoul", ROLE)
    assert out.cache_keys == ["model:claude-x", "model:anthropic.x", "model:list"]
    # 커밋 전(서비스 안)에는 지우지 않는다 — 라우터가 커밋 뒤에 지운다(ModelMutation).
    cache.invalidate.assert_not_awaited()


async def test_update_without_the_key_keeps_the_selection():
    svc, _ = _svc()
    model = _model(account_name="inregion-seoul")
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        repo = Repo.return_value
        repo.get_by_alias = AsyncMock(return_value=model)
        repo.update_model = AsyncMock(return_value=model)
        repo.get_current_pricing = AsyncMock(return_value=None)
        out = await svc.update_model(
            _session(), alias="claude-x", data=ModelUpdateRequest(description="d"), actor=ADMIN
        )
    assert "account_name" not in repo.update_model.await_args.kwargs
    assert "apply_to_siblings" not in repo.update_model.await_args.kwargs, "컬럼이 아니다"
    assert (out.response.account_name, out.response.account_role_arn) == ("inregion-seoul", ROLE)


async def test_update_with_an_explicit_null_clears_the_selection():
    """해제 = 기본 계정 복귀. 무시되면 성공 토스트만 뜨고 alias 는 옛 계정으로 계속 나간다."""
    svc, cache = _svc()
    model = _model(account_name="inregion-seoul")
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        repo = Repo.return_value
        repo.get_by_alias = AsyncMock(return_value=model)
        repo.update_model = AsyncMock(return_value=model)
        repo.get_current_pricing = AsyncMock(return_value=None)
        out = await svc.update_model(
            _session(), alias="claude-x", data=ModelUpdateRequest(account_name=None), actor=ADMIN
        )
    assert model.account_name is None
    assert (out.response.account_name, out.response.account_role_arn) == (None, None)
    assert audit.log.await_args.kwargs["changes"]["after"]["account_name"] is None
    assert "model:global.anthropic.claude-x" in out.cache_keys
    cache.invalidate.assert_not_awaited()


async def test_update_rejects_an_account_on_an_existing_openmodel_alias():
    svc, _ = _svc()
    model = _model(provider=Provider.OPENMODEL)
    with patch("app.services.model_service.ModelRepository") as Repo:
        repo = Repo.return_value
        repo.get_by_alias = AsyncMock(return_value=model)
        repo.update_model = AsyncMock(return_value=model)
        with pytest.raises(ValidationError, match="OPENMODEL"):
            await svc.update_model(
                _session(), alias="claude-x", data=ModelUpdateRequest(account_name="inregion-seoul"),
                actor=ADMIN,
            )
        repo.update_model.assert_not_called()


async def test_update_invalidates_the_old_and_new_model_id_keys():
    """모델 id 를 바꾸는 저장은 옛 모델 id 키도 지운다 — 거기 남은 항목이 옛 계정으로 300초 산다."""
    svc, cache = _svc()
    model = _model()

    async def _apply(alias, **kw):
        if kw.get("provider_model_id"):
            model.provider_model_id = kw["provider_model_id"]
        return model

    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        repo = Repo.return_value
        repo.get_by_alias = AsyncMock(return_value=model)
        repo.update_model = AsyncMock(side_effect=_apply)
        repo.get_current_pricing = AsyncMock(return_value=None)
        out = await svc.update_model(
            _session(), alias="claude-x", actor=ADMIN,
            data=ModelUpdateRequest(provider_model_id="global.anthropic.claude-y"),
        )
    assert "model:global.anthropic.claude-x" in out.cache_keys
    assert "model:global.anthropic.claude-y" in out.cache_keys


# ── 같은 모델 id 의 형제 alias 와 계정 일치 ───────────────────────────────────────────
#
# 게이트웨이는 alias 정확 매칭이 먼저다. 시드는 모델 id 와 이름이 같은 alias 를 두므로, 한 alias 에만
# 계정을 걸면 모델 id 로 부르는 요청이 그 계정을 건너뛴다(200, 다른 계정 청구). 쓰기에서 막는다.


def _sibling(alias="global.anthropic.claude-x", account_name=None):
    return _model(alias=alias, account_name=account_name)


def _repo_for_update(Repo, model):
    repo = Repo.return_value
    repo.get_by_alias = AsyncMock(return_value=model)
    repo.update_model = AsyncMock(return_value=model)
    repo.get_current_pricing = AsyncMock(return_value=None)
    return repo


async def test_selecting_an_account_that_a_sibling_does_not_share_is_a_409_naming_it():
    svc, _ = _svc()
    model = _model()
    sib = _sibling()
    with patch("app.services.model_service.ModelRepository") as Repo:
        repo = _repo_for_update(Repo, model)
        with pytest.raises(ConflictError) as exc:
            await svc.update_model(
                _session(siblings=[sib]), alias="claude-x", actor=ADMIN,
                data=ModelUpdateRequest(account_name="inregion-seoul"),
            )
    msg = exc.value.message
    assert "global.anthropic.claude-x (no account)" in msg and "apply_to_siblings" in msg
    repo.update_model.assert_not_called()
    assert sib.account_name is None


async def test_apply_to_siblings_moves_them_in_the_same_save_and_drops_their_cache():
    svc, _ = _svc()
    model = _model()
    sib = _sibling()
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        _repo_for_update(Repo, model)
        out = await svc.update_model(
            _session(siblings=[sib]), alias="claude-x", actor=ADMIN,
            data=ModelUpdateRequest(account_name="inregion-seoul", apply_to_siblings=True),
        )
    assert (sib.account_name, sib.account_role_arn) == ("inregion-seoul", ROLE)
    assert out.cache_keys == [
        "model:claude-x", "model:global.anthropic.claude-x", "model:list",
    ], "형제의 alias 키(여기서는 모델 id 와 같은 이름)가 빠지면 형제는 300초 옛 계정으로 나간다"
    after = audit.log.await_args.kwargs["changes"]["after"]
    assert after["account_applied_to_siblings"] == ["global.anthropic.claude-x"]


async def test_create_with_an_account_checks_siblings_too():
    svc, _ = _svc()
    with patch("app.services.model_service.ModelRepository") as Repo:
        Repo.return_value.alias_exists_ci = AsyncMock(return_value=False)
        Repo.return_value.create_model = AsyncMock()
        with pytest.raises(ConflictError, match="other-alias"):
            await svc.create_model(
                _session(siblings=[_sibling(alias="other-alias", account_name="dedicated")]),
                actor=ADMIN, data=_create(account_name="inregion-seoul"),
            )
        Repo.return_value.create_model.assert_not_called()


async def test_an_unrelated_edit_is_not_blocked_by_already_mismatched_siblings():
    """설명만 고치는 저장이 옛 데이터의 불일치 때문에 409 가 되면 운영자는 아무것도 못 고친다."""
    svc, _ = _svc()
    model = _model(account_name="inregion-seoul")
    session = _session(siblings=[_sibling()])
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        _repo_for_update(Repo, model)
        await svc.update_model(
            session, alias="claude-x", actor=ADMIN, data=ModelUpdateRequest(description="d")
        )
    session.execute.assert_not_awaited()


async def test_an_inactive_alias_is_checked_when_it_is_activated_not_when_it_is_edited():
    svc, _ = _svc()
    model = _model(status=ModelStatus.INACTIVE)
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        repo = _repo_for_update(Repo, model)
        repo.patch_status = AsyncMock(return_value=model)
        await svc.update_model(
            _session(siblings=[_sibling()]), alias="claude-x", actor=ADMIN,
            data=ModelUpdateRequest(account_name="inregion-seoul"),
        )
        model.account_name = "inregion-seoul"
        with pytest.raises(ConflictError, match="then activate it"):
            await svc.patch_status(
                _session(siblings=[_sibling()]), alias="claude-x", actor=ADMIN,
                data=StatusPatchRequest(active=True),
            )
        repo.patch_status.assert_not_called()
        # 비활성화는 불일치를 만들 수 없다 — 형제를 보지 않는다.
        model.status = ModelStatus.ACTIVE
        deactivate_session = _session(siblings=[_sibling()])
        await svc.patch_status(
            deactivate_session, alias="claude-x", actor=ADMIN, data=StatusPatchRequest(active=False)
        )
        deactivate_session.execute.assert_not_awaited()


async def test_openmodel_aliases_have_no_account_and_skip_the_sibling_check():
    svc, _ = _svc()
    session = _session(siblings=[_sibling(account_name="inregion-seoul")])
    with patch("app.services.model_service.ModelRepository") as Repo, \
         patch("app.services.model_service.audit_logger") as audit:
        audit.log = AsyncMock()
        repo = Repo.return_value
        repo.alias_exists_ci = AsyncMock(return_value=False)

        async def _create_model(m):
            m.created_at = m.updated_at = datetime.now(UTC)
            m.status = ModelStatus.ACTIVE
            return m

        repo.create_model = AsyncMock(side_effect=_create_model)
        repo.create_pricing = AsyncMock()
        await svc.create_model(
            session, actor=ADMIN,
            data=_create(provider=ProviderEnum.OPENMODEL, api_format=ApiFormatEnum.OPENAI_COMPATIBLE),
        )
    session.execute.assert_not_awaited()


def test_apply_to_siblings_defaults_to_false_and_is_not_a_column():
    assert _create().apply_to_siblings is False
    assert ModelUpdateRequest().apply_to_siblings is False
