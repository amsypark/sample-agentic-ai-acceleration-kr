# Copyright 2026 © Amazon.com and Affiliates
"""Regression: Claude Fable 5 / 5.1 등록(0042).

지키는 것:

1. 단가가 Price List 표와 한 자리도 다르지 않다 — 특히 5.1 의 캐시 읽기(입력의 0.025배)가
   5 의 값(0.1배)으로 복사되지 않았다.
2. effective_from 이 적용 가능한 가장 이른 시각보다 과거다(0041 이 겪은 cost 0 함정).
3. IAM 이 Fable 의 foundation-model 과 프로파일을 **둘 다** 허용한다. 0042 이전에는 IAM 이
   Fable 을 일부러 막았다 — 이 리비전이 그 결정을 뒤집으므로 IaC 도 같이 열려야 한다.
4. 재실행·선등록 가드와 downgrade 정리(0041 과 같은 계약).

``PROOF_DSN`` 이 있으면 실제 PostgreSQL 에서 결과 상태까지 본다.

⚠️ 테스트가 대신해 주지 못하는 것: 계정 데이터 보존 모드(aws_review). 그건 DB·IaC 밖의 계정
   설정이라 여기서 검증할 수 없다 — 0042 docstring 의 PUT/GET 절차로 실행 계정마다 확인할 것.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tests.regression.test_high_model_registry_0041 import _allowed, _allowed_arns
from tests.regression.test_high_model_registry_claude5 import (
    _downgrade_child_tables,
    _module_constants,
    _op_execute_sql,
)

PROOF_DSN = os.environ.get("PROOF_DSN")
_DB = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")

_ROOT = Path(__file__).resolve().parents[3]
_M0042 = _ROOT / "db" / "versions" / "0042_add_claude_fable_aliases.py"
_TF_ENVS = [
    _ROOT / "deployment" / "terraform" / "environments" / env / "variables.tf"
    for env in ("llm-gateway-dev", "llm-gateway-prod")
]

# AWS Price List AmazonBedrockFoundationModels, ap-northeast-2, *_global_standard (per-1M USD,
# publicationDate 2026-10-12). (input, output, cw_5m, cw_1h, cache_read)
SOURCE = {
    "claude-fable-5": (10, 50, "12.5", 20, 1),
    "claude-fable-5-1": (10, 50, "12.5", 20, "0.25"),
}


def _consts() -> dict:
    return _module_constants(_M0042)


def _models() -> dict:
    return {m[0]: m for m in _consts()["MODELS"]}


def test_migration_chain_0042():
    c = _consts()
    assert (c["revision"], c["down_revision"]) == ("0042", "0041")


def test_fable_uses_global_profiles_and_both_names():
    models = _models()
    assert set(models) == set(SOURCE)
    for short, (_s, profile, *_r) in models.items():
        assert profile == f"global.anthropic.{short}"


def test_prices_match_the_price_list_exactly():
    for short, m in _models().items():
        assert tuple(Decimal(x) for x in m[4]) == tuple(
            Decimal(str(v)) / 1000 for v in SOURCE[short]
        ), short


def test_fable_5_1_cache_read_is_not_copied_from_fable_5():
    m = _models()
    r5 = Decimal(m["claude-fable-5"][4][4]) / Decimal(m["claude-fable-5"][4][0])
    r51 = Decimal(m["claude-fable-5-1"][4][4]) / Decimal(m["claude-fable-5-1"][4][0])
    assert (r5, r51) == (Decimal("0.1"), Decimal("0.025"))
    # 나머지 네 칸은 같다 — 다른 건 캐시 읽기 하나뿐이라는 것도 고정한다.
    assert m["claude-fable-5"][4][:4] == m["claude-fable-5-1"][4][:4]


def test_no_single_quote_in_interpolated_strings():
    for m in _consts()["MODELS"]:
        for s in m[:4]:
            assert "'" not in s, s


def test_effective_from_is_before_the_migration_can_run():
    eff = datetime.fromisoformat(_consts()["EFFECTIVE_FROM"].replace("Z", "+00:00"))
    created = re.search(r"Create Date: (\d{4}-\d{2}-\d{2})", _M0042.read_text()).group(1)
    earliest_run = datetime.fromisoformat(created).replace(tzinfo=timezone(timedelta(hours=9)))
    assert eff <= earliest_run


def test_upgrade_guards():
    sql = _op_execute_sql(_M0042, "upgrade")
    assert "ON CONFLICT (alias) DO NOTHING" in sql
    assert re.search(r"NOT EXISTS\s*\(\s*SELECT 1 FROM model\.model_pricings[^)]*effective_until IS NULL", sql)
    assert re.search(r"WHERE EXISTS\s*\(\s*SELECT 1 FROM model\.model_aliases[^)]*provider_model_id", sql)


def test_downgrade_cleans_all_fk_children_and_dangling_defaults():
    assert set(_downgrade_child_tables(_M0042)) == {
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    }
    down = _op_execute_sql(_M0042, "downgrade")
    assert down.index("UPDATE model.routing_profiles") < down.index("DELETE FROM model.model_aliases")


@pytest.mark.parametrize("tf", _TF_ENVS, ids=lambda p: p.parent.name)
def test_iam_allows_fable_on_both_resources(tf):
    pats = _allowed_arns(tf)
    for short, (_s, profile, *_r) in _models().items():
        assert _allowed(f"arn:aws:bedrock:ap-northeast-2:123456789012:inference-profile/{profile}", pats)
        for region in ("", "ap-northeast-2", "us-east-1", "us-west-2", "eu-west-1"):
            fm = f"arn:aws:bedrock:{region}::foundation-model/anthropic.{short}"
            assert _allowed(fm, pats), f"{tf.parent.name}: {fm} 가 막힌다 → AccessDenied"


@pytest.mark.parametrize("tf", _TF_ENVS, ids=lambda p: p.parent.name)
def test_iam_anthropic_line_does_not_open_other_vendors(tf):
    """`anthropic.claude-*` 로 넓혔어도 벤더 경계는 그대로여야 한다(대조군)."""
    pats = _allowed_arns(tf)
    for fm in ("amazon.nova-pro-v1:0", "meta.llama4-maverick-17b-instruct-v1:0", "openai.gpt-9-x"):
        assert not _allowed(f"arn:aws:bedrock:us-east-1::foundation-model/{fm}", pats), fm


@_DB
@pytest.mark.asyncio
async def test_live_0042_state():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    expected = {}
    for short, profile, _d, _desc, prices in _consts()["MODELS"]:
        for alias in (short, profile):
            expected[alias] = (profile, prices)

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            head = (await conn.execute(text("SELECT version_num FROM public.alembic_version"))).scalar()
            if head is None or head < "0042":
                pytest.skip(f"DB head={head} — 0042 미적용")
            rows = (
                await conn.execute(
                    text(
                        "SELECT a.alias, a.provider::text, a.provider_model_id, a.status::text,"
                        "       p.input_price_per_1k_tokens, p.output_price_per_1k_tokens,"
                        "       p.cache_creation_5m_price_per_1k_tokens,"
                        "       p.cache_creation_1h_price_per_1k_tokens,"
                        "       p.cache_read_price_per_1k_tokens, p.long_context_threshold_tokens,"
                        "       (p.effective_from <= now()) AS effective_now"
                        "  FROM model.model_aliases a"
                        "  LEFT JOIN model.model_pricings p"
                        "    ON p.model_alias = a.alias AND p.effective_until IS NULL"
                        " WHERE a.alias = ANY(:aliases)"
                    ),
                    {"aliases": list(expected)},
                )
            ).fetchall()
    finally:
        await engine.dispose()

    got = {r[0]: r for r in rows}
    assert set(got) == set(expected), f"누락: {set(expected) - set(got)}"
    for alias, (profile, prices) in expected.items():
        r = got[alias]
        assert (r[1], r[2], r[3]) == ("BEDROCK", profile, "ACTIVE"), alias
        assert tuple(r[4:9]) == tuple(Decimal(x) for x in prices), alias
        assert r[9] is None, f"{alias}: Price List 에 long_ctx SKU 가 없다 — 티어 없음"
        assert r[10] is True, f"{alias}: 단가가 아직 유효하지 않다 → cost_usd 0"
