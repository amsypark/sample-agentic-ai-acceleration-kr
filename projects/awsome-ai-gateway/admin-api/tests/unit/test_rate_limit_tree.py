# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/rate-limits/tree`` 계약 — 이 트리가 곧 **집행 표면 전체**다.

이 파일이 존재하는 이유:

  1. GLOBAL 루트가 아예 없었다. 라우터에는 ``PUT /admin/rate-limits/global/{model_alias}``
     가 있고 gateway-proxy 는 GLOBAL 한도를 매 요청 집행하는데, 트리는 TEAM/USER 만
     내보냈다. admin-ui 는 노드가 없으면 대상 id 를 만들 수 없으므로 GLOBAL 쓰기 경로가
     콘솔에서 **도달 불가**였고, API 로 건 한도는 콘솔에서 "설정 없음" 으로 보였다.
  2. 노드 id 는 URL 슬롯이다 — ``PUT /admin/rate-limits/{scope}/{node.id}``. 그래서
     GLOBAL 노드의 id 는 UUID 가 아니라 **model alias** 여야 한다.
  3. 팀 미배정 사용자(``auth.users.team_id IS NULL``)가 어떤 루트에도 없었다.
  4. 상속 표식(``inherited_from`` + ``config.scope=="TEAM"``)은 admin-ui 가 "빌려온 값을
     폼에 프리필하지 않는다" 를 판단하는 유일한 근거다.
  5. ``cpm``/``cph`` 는 ``Decimal`` → **JSON 문자열**, 같은 객체의 ``rpm``/``tpm`` 은 숫자다.

와이어 전체는 ``admin-ui/tests/fixtures/rate-limit-tree.json`` 과 **완전 일치**로 고정한다.
admin-ui 컴포넌트 테스트가 같은 파일을 픽스처로 읽으므로, 어느 한쪽이 모양을 바꾸면 두
배포물 중 하나가 반드시 빨개진다(손으로 베낀 픽스처는 드리프트를 못 잡는다).
쿼리 자체(팀 미배정 조회, alias 목록, 활성 GLOBAL 행)는 실 PG 로
test_high_console_contract_real_db.py 가 증명한다.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.model import ModelStatus, RateLimitScope
from app.schemas.rate_limits import RateLimitConfigItem, RateLimitTreeNode
from app.services.rate_limit_service import RateLimitService

# tests/unit/x.py -> tests/unit -> tests -> admin-api -> awsome-ai-gateway
PROJECT = Path(__file__).resolve().parents[3]
UI_FIXTURE = PROJECT / "admin-ui" / "tests" / "fixtures" / "rate-limit-tree.json"

TEAM_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ALICE_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ZOE_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
CAPPED_ALIAS = "claude-opus-4-7"
# glob 메타문자를 품은 실재 alias (db/init/03_seed_data.sql).
BRACKET_ALIAS = "claude-sonnet-4-6[1m]"


def rl_config(scope, *, scope_id=None, model_alias=None, rpm=None, tpm=None, cpm=None, cph=None):
    return SimpleNamespace(
        scope=scope,
        scope_id=scope_id,
        model_alias=model_alias,
        rpm_limit=rpm,
        tpm_limit=tpm,
        cpm_limit_usd=cpm,
        cph_limit_usd=cph,
        is_active=True,
    )


def user_double(user_id: uuid.UUID, name: str, *, active: bool = True):
    return SimpleNamespace(id=user_id, display_name=name, is_active=active)


def team_double(team_id: uuid.UUID, name: str, members: list):
    return SimpleNamespace(id=team_id, name=name, members=members)


async def build_tree(
    *,
    teams=(),
    teamless=(),
    team_configs=(),
    user_configs=(),
    global_configs=(),
    aliases=(),
) -> tuple[list[RateLimitTreeNode], dict]:
    """서비스를 리포지토리 대역과 함께 실행. 반환: (노드, 호출 기록)."""
    svc = RateLimitService(cache_mgr=MagicMock())
    by_scope = {
        RateLimitScope.TEAM: list(team_configs),
        RateLimitScope.USER: list(user_configs),
        RateLimitScope.GLOBAL: list(global_configs),
    }
    calls: dict = {"scopes": [], "alias_status": []}

    async def _by_scope(scope):
        calls["scopes"].append(scope)
        return by_scope[scope]

    async def _aliases(*, status=None):
        calls["alias_status"].append(status)
        return list(aliases)

    with patch("app.services.rate_limit_service.UserRepository") as MockUsers, patch(
        "app.services.rate_limit_service.RateLimitConfigRepository"
    ) as MockRL, patch("app.services.rate_limit_service.ModelRepository") as MockModels:
        MockUsers.return_value.list_all_teams = AsyncMock(return_value=list(teams))
        MockUsers.return_value.list_teamless_users = AsyncMock(return_value=list(teamless))
        MockRL.return_value.list_active_by_scope = AsyncMock(side_effect=_by_scope)
        MockModels.return_value.list_alias_names = AsyncMock(side_effect=_aliases)
        nodes = await svc.get_rate_limit_tree(AsyncMock())
    return nodes, calls


async def full_tree() -> list[RateLimitTreeNode]:
    """픽스처와 같은 시나리오: GLOBAL 2개(1개만 한도), 팀 1개(자기 설정 + 상속 멤버),
    팀 미배정 사용자 1명."""
    alice = user_double(ALICE_ID, "Alice")
    bob = user_double(BOB_ID, "Bob")
    zoe = user_double(ZOE_ID, "Zoe")
    nodes, _ = await build_tree(
        teams=[team_double(TEAM_ID, "Platform", [alice, bob])],
        teamless=[zoe],
        team_configs=[
            rl_config(
                RateLimitScope.TEAM,
                scope_id=TEAM_ID,
                rpm=100,
                tpm=9000,
                cpm=Decimal("0.5000"),
                cph=Decimal("12.3456"),
            )
        ],
        user_configs=[
            rl_config(
                RateLimitScope.USER,
                scope_id=ALICE_ID,
                rpm=60,
                tpm=1000,
                cpm=Decimal("0.5000"),
                cph=Decimal("12.3456"),
            )
        ],
        # Bedrock 쿼터 모양: TPM 만 걸린 GLOBAL 행(rpm=NULL).
        global_configs=[
            rl_config(RateLimitScope.GLOBAL, model_alias=CAPPED_ALIAS, rpm=None, tpm=200000)
        ],
        aliases=[CAPPED_ALIAS, BRACKET_ALIAS],
    )
    return nodes


class TestGlobalRoots:
    async def test_global_node_emitted_for_each_alias(self):
        nodes = await full_tree()
        globals_ = [n for n in nodes if n.scope == "GLOBAL"]

        assert [n.id for n in globals_] == sorted([CAPPED_ALIAS, BRACKET_ALIAS])
        capped = next(n for n in globals_ if n.id == CAPPED_ALIAS)
        assert capped.config is not None
        # TPM 만 걸린 쿼터가 정상이다 — rpm=None 은 '한도 없음' 이 아니다.
        assert (capped.config.rpm, capped.config.tpm) == (None, 200000)
        # 한도가 없는 모델도 노드로 나온다 — 그래야 새 GLOBAL 한도를 걸 수 있다.
        assert next(n for n in globals_ if n.id == BRACKET_ALIAS).config is None

    async def test_global_node_id_is_alias_not_uuid(self):
        """id 는 PUT /admin/rate-limits/global/{model_alias} 의 URL 슬롯으로 들어간다."""
        nodes = await full_tree()
        for node in [n for n in nodes if n.scope == "GLOBAL"]:
            with pytest.raises(ValueError):
                uuid.UUID(node.id)
            assert node.config is None or node.config.target_id == node.id

    async def test_all_three_scopes_are_reachable(self):
        nodes = await full_tree()
        scopes = {n.scope for n in nodes} | {c.scope for n in nodes for c in n.children}
        assert scopes == {s.value for s in RateLimitScope}

    async def test_global_configs_are_actually_queried(self):
        """서비스가 GLOBAL 스코프를 **조회조차** 하지 않던 것이 원래 결함이었다."""
        _, calls = await build_tree(aliases=[CAPPED_ALIAS])
        assert set(calls["scopes"]) == set(RateLimitScope)
        # 활성 모델만 GLOBAL 루트 후보(비활성 모델은 한도가 걸린 경우에만 합집합으로 등장)
        assert calls["alias_status"] == [ModelStatus.ACTIVE]

    async def test_inactive_alias_with_live_cap_still_listed(self):
        """모델이 비활성이어도 GLOBAL 한도 행은 남아 있다 → 화면에서 사라지면 안 된다."""
        nodes, _ = await build_tree(
            global_configs=[rl_config(RateLimitScope.GLOBAL, model_alias="retired-model", tpm=5)],
            aliases=[],
        )
        retired = next(n for n in nodes if n.id == "retired-model")
        assert retired.scope == "GLOBAL"
        assert retired.config is not None and retired.config.tpm == 5

    async def test_globals_come_before_teams_and_teamless_users_last(self):
        nodes = await full_tree()
        assert [n.scope for n in nodes] == ["GLOBAL", "GLOBAL", "TEAM", "USER"]


class TestUserCoverage:
    async def test_every_user_addressable_exactly_once(self):
        nodes = await full_tree()
        ids = [n.id for n in nodes if n.scope == "USER"] + [
            c.id for n in nodes for c in n.children
        ]
        assert sorted(ids) == sorted(str(u) for u in (ALICE_ID, BOB_ID, ZOE_ID))
        assert len(ids) == len(set(ids))

    async def test_teamless_user_is_a_root_without_inheritance(self):
        nodes = await full_tree()
        zoe = next(n for n in nodes if n.id == str(ZOE_ID))
        assert (zoe.scope, zoe.inherited_from, zoe.config) == ("USER", None, None)

    async def test_teamless_user_with_own_limit_shows_it(self):
        """팀이 없어도 USER 행은 집행된다 — 화면에서도 보여야 한다."""
        nodes, _ = await build_tree(
            teamless=[user_double(ZOE_ID, "Zoe")],
            user_configs=[rl_config(RateLimitScope.USER, scope_id=ZOE_ID, rpm=7)],
        )
        zoe = next(n for n in nodes if n.id == str(ZOE_ID))
        assert zoe.config is not None and zoe.config.rpm == 7 and zoe.inherited_from is None

    async def test_inheriting_member_carries_team_marker(self):
        """admin-ui 가 프리필을 억제하는 근거. 세 값이 모두 있어야 한다."""
        nodes = await full_tree()
        team = next(n for n in nodes if n.scope == "TEAM")
        bob = next(c for c in team.children if c.id == str(BOB_ID))

        assert bob.inherited_from == str(TEAM_ID)
        assert bob.config is not None
        assert bob.config.scope == "TEAM"
        assert bob.config.target_id == str(TEAM_ID)
        assert bob.config.rpm == 100

    async def test_own_config_has_no_inheritance_marker(self):
        nodes = await full_tree()
        team = next(n for n in nodes if n.scope == "TEAM")
        alice = next(c for c in team.children if c.id == str(ALICE_ID))

        assert alice.inherited_from is None
        assert alice.config is not None
        assert alice.config.scope == "USER"
        assert alice.config.target_id == str(ALICE_ID)
        assert alice.config.rpm == 60


class TestWireShape:
    """Decimal 은 JSON 문자열, int 는 JSON 숫자 — 한 객체의 두 절반이 다르다."""

    def test_fastapi_response_model_emits_decimal_as_string(self):
        """실제 FastAPI 직렬화 경로(라우터는 response_model=list[RateLimitTreeNode])."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        node = RateLimitTreeNode(
            id=str(ALICE_ID),
            label="Alice",
            scope="USER",
            config=RateLimitConfigItem(
                target_id=str(ALICE_ID),
                scope="USER",
                rpm=60,
                tpm=1000,
                cpm=Decimal("0.5000"),
                cph=Decimal("12.3456"),
            ),
        )
        app = FastAPI()

        @app.get("/tree", response_model=list[RateLimitTreeNode])
        async def _tree():  # pragma: no cover - 직렬화 경로만 필요
            return [node]

        body = TestClient(app).get("/tree").json()

        assert body[0]["config"]["cpm"] == "0.5000"
        assert isinstance(body[0]["config"]["cph"], str)
        assert isinstance(body[0]["config"]["rpm"], int)
        assert isinstance(body[0]["config"]["tpm"], int)

    async def test_wire_matches_admin_ui_fixture(self):
        if not UI_FIXTURE.exists():
            pytest.skip(f"sibling admin-ui not in this checkout: {UI_FIXTURE}")
        nodes = await full_tree()
        wire = [n.model_dump(mode="json") for n in nodes]
        expected = json.loads(UI_FIXTURE.read_text(encoding="utf-8"))
        assert wire == expected, (
            "트리 와이어 모양이 바뀌었다. admin-ui 는 이 JSON 을 픽스처로 그대로 읽는다 — "
            f"서버를 되돌리거나 {UI_FIXTURE.relative_to(PROJECT)} 를 갱신하고 "
            "admin-ui 테스트(npm test)를 함께 통과시켜라."
        )
