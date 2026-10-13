# Copyright 2026 © Amazon.com and Affiliates
"""Regression: Claude 5.5 + GPT-6 등록(0041).

이 테스트가 지키는 것:

1. **단가가 출처 표와 한 자리도 다르지 않다.** 출처(Price List SKU / 모델카드)의 per-1M 값을
   이 파일에 그대로 적어 두고 마이그레이션의 per-1K 문자열과 대조한다. 전사 실수는 에러 없이
   청구를 틀리게 만든다.
2. **배수로 계산하면 틀리는 칸이 고정돼 있다.** Opus/Sonnet 5.5 와 GPT-6.1 Sol 의 캐시 읽기는
   입력의 0.05배다(다른 모델은 0.1배). Haiku 5.5 의 long 은 5배이고 임계는 100K 다.
3. **IAM 이 등록된 모든 모델을 덮는다.** DB 행만으로는 호출되지 않는다(0027 의 Claude 5
   AccessDenied). terraform 의 ``bedrock_allowed_model_arns`` 가 각 모델의 inference-profile
   과 foundation-model ARN 을 **둘 다** 허용하는지, 그리고 fable 은 여전히 막는지 본다.
4. **재실행·선등록에 안전하다.** 단가 INSERT 가 열린 행이 있으면 건너뛰고(0034 부분 유니크),
   alias 가 우리 모델일 때만 붙는다.
5. **downgrade 가 FK 자식까지 정리한다.**

``PROOF_DSN`` 이 있으면 실제 PostgreSQL 에서 결과 상태까지 확인한다.
"""
from __future__ import annotations

import fnmatch
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest

from tests.regression.test_high_model_registry_claude5 import (
    _downgrade_child_tables,
    _module_constants,
    _op_execute_sql,
)

PROOF_DSN = os.environ.get("PROOF_DSN")
_DB = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")

_ROOT = Path(__file__).resolve().parents[3]
_M0041 = _ROOT / "db" / "versions" / "0041_add_claude_55_and_gpt6_aliases.py"
_TF_ENVS = [
    _ROOT / "deployment" / "terraform" / "environments" / env / "variables.tf"
    for env in ("llm-gateway-dev", "llm-gateway-prod")
]

# ── 출처 표 (per-1M USD). 마이그레이션은 이 값 / 1000 이어야 한다. ────────────────
# Claude: AWS Price List AmazonBedrockFoundationModels, ap-northeast-2, *_global_standard
#         (publicationDate 2026-10-12T08:40:08Z). (input, output, cw_5m, cw_1h, cache_read)
CLAUDE_SOURCE = {
    "claude-opus-5-5": ((4, 20, 5, 8, "0.2"), None),
    "claude-sonnet-5-5": ((2, 10, "2.5", 4, "0.1"), None),
    "claude-haiku-5-5": (("0.1", "0.5", "0.125", "0.2", "0.01"),
                         (100000, ("0.5", "2.5", "0.625", 1, "0.05"))),
}
# GPT-6: AWS 모델카드 US Geo CRIS 행 (2026-10-14). (input, cw_30m, cache_read, output)
GPT_SOURCE = {
    "gpt-6-astra": (("11", "13.75", "1.1", "55"), ("22", "27.5", "2.2", "82.5")),
    "gpt-6-sol": (("2.2", "2.75", "0.22", "11"), ("4.4", "5.5", "0.44", "16.5")),
    "gpt-6-luna": (("0.11", "0.1375", "0.011", "0.55"), ("0.22", "0.275", "0.022", "0.825")),
    "gpt-6.1-sol": (("2.2", "2.75", "0.11", "11"), ("4.4", "5.5", "0.22", "16.5")),
}
# 같은 카드의 Global CRIS 행. Geo = Global x 1.1 이어야 한다 — Geo 행을 일관되게 옮겼는지의
# 독립 점검(행을 섞어 옮기면 이 관계가 깨진다).
GPT_GLOBAL = {
    "gpt-6-astra": ("10", "12.5", "1", "50"),
    "gpt-6-sol": ("2", "2.5", "0.2", "10"),
    "gpt-6-luna": ("0.1", "0.125", "0.01", "0.5"),
    "gpt-6.1-sol": ("2", "2.5", "0.1", "10"),
}


def _per_1k(v) -> Decimal:
    return Decimal(str(v)) / 1000


def _consts() -> dict:
    return _module_constants(_M0041)


def _claude():
    return {m[0]: m for m in _consts()["CLAUDE_MODELS"]}


def _gpt():
    return {m[0]: m for m in _consts()["GPT_MODELS"]}


# --- 체인 / 식별자 ----------------------------------------------------------


def test_migration_chain_0041():
    c = _consts()
    assert (c["revision"], c["down_revision"]) == ("0041", "0040"), (
        "0041 은 0040(NUMERIC(12,8)) 뒤여야 한다 — Luna 의 0.0001375 가 6자리 열에서 반올림된다"
    )


def test_claude_55_uses_global_profiles_and_registers_both_names():
    claude = _claude()
    assert set(claude) == set(CLAUDE_SOURCE)
    for short, (alias, profile, *_rest) in claude.items():
        assert profile == f"global.anthropic.{short}", (
            f"{short}: 서울에는 global. 프로파일만 있다(실측) — {profile}"
        )


def test_gpt6_uses_us_cris_on_the_us_east_2_runtime_plane():
    c = _consts()
    assert c["GPT_ENDPOINT"] == "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
    gpt = _gpt()
    assert set(gpt) == set(GPT_SOURCE)
    for alias, (_a, pmid, *_rest) in gpt.items():
        # 접두사 없는 bare id 는 runtime 평면에서 400 이고(INFERENCE_PROFILE 전용), us. 를
        # 다른 리전 엔드포인트로 보내면 "model identifier is invalid" 다(0032 실측).
        assert pmid == f"us.openai.{alias}", pmid


def test_effective_from_is_before_the_migration_can_run():
    """단가 행이 적용 시각보다 미래면 해석기가 무시하고 비용을 0 으로 기록한다(실측).

    마이그레이션이 가장 일찍 돌 수 있는 시각은 Create Date 의 00:00 KST(= 전날 15:00 UTC)다.
    """
    from datetime import datetime, timedelta, timezone

    c = _consts()
    eff = datetime.fromisoformat(c["EFFECTIVE_FROM"].replace("Z", "+00:00"))
    created = re.search(r"Create Date: (\d{4}-\d{2}-\d{2})", _M0041.read_text()).group(1)
    earliest_run = datetime.fromisoformat(created).replace(tzinfo=timezone(timedelta(hours=9)))
    assert eff <= earliest_run, (
        f"EFFECTIVE_FROM {eff.isoformat()} 가 가장 이른 적용 시각 {earliest_run.isoformat()} 보다 늦다 "
        "— 그 사이 요청은 cost_usd=0 으로 기록된다"
    )


def test_fable_is_still_not_registered():
    c = _consts()
    names = [x for m in c["CLAUDE_MODELS"] for x in m[:2]] + [x for m in c["GPT_MODELS"] for x in m[:2]]
    assert not any("fable" in n or "mythos" in n for n in names)


def test_no_single_quote_in_interpolated_strings():
    """설명·표시명은 f-string 으로 SQL 에 들어간다. 따옴표 하나면 마이그레이션이 죽는다."""
    c = _consts()
    for m in c["CLAUDE_MODELS"] + c["GPT_MODELS"]:
        for s in m[:4]:
            assert "'" not in s, s


# --- 단가: 출처와의 일치 ------------------------------------------------------


def test_claude_prices_match_the_price_list_exactly():
    for short, m in _claude().items():
        short_src, long_src = CLAUDE_SOURCE[short]
        assert tuple(Decimal(x) for x in m[4]) == tuple(_per_1k(v) for v in short_src), short
        if long_src is None:
            assert m[5] is None, f"{short}: Price List 에 long_ctx SKU 가 없다 — 티어를 켜면 안 된다"
        else:
            thr, long_vals = long_src
            assert m[5][0] == thr
            assert tuple(Decimal(x) for x in m[5][1:]) == tuple(_per_1k(v) for v in long_vals)


def test_gpt_prices_match_the_model_cards_exactly():
    for alias, m in _gpt().items():
        (s_in, s_cw, s_cr, s_out), (l_in, l_cw, l_cr, l_out) = GPT_SOURCE[alias]
        p_in, p_out, p_5m, p_1h, p_cr = (Decimal(x) for x in m[4])
        assert (p_in, p_out, p_5m, p_cr) == tuple(_per_1k(v) for v in (s_in, s_out, s_cw, s_cr)), alias
        # OpenAI 모델은 쓰기 요율이 하나(30m) — 5m·1h 에 같은 값(0032 관례).
        assert p_5m == p_1h, alias
        thr, *lv = m[5]
        assert thr == 272000, alias
        l_in_, l_out_, l_5m, l_1h, l_cr_ = (Decimal(x) for x in lv)
        assert (l_in_, l_out_, l_5m, l_cr_) == tuple(_per_1k(v) for v in (l_in, l_out, l_cw, l_cr)), alias
        assert l_5m == l_1h, alias


def test_gpt_geo_row_is_global_plus_ten_percent():
    for alias, m in _gpt().items():
        p_in, p_out, p_5m, _p_1h, p_cr = (Decimal(x) for x in m[4])
        g_in, g_cw, g_cr, g_out = (_per_1k(v) for v in GPT_GLOBAL[alias])
        assert (p_in, p_5m, p_cr, p_out) == tuple(v * Decimal("1.1") for v in (g_in, g_cw, g_cr, g_out)), (
            f"{alias}: Geo CRIS 가 아닌 행을 옮겼다(us. 는 Geo CRIS)"
        )


def test_every_price_fits_numeric_12_8_without_rounding():
    c = _consts()
    for m in c["CLAUDE_MODELS"] + c["GPT_MODELS"]:
        vals = list(m[4]) + (list(m[5][1:]) if m[5] else [])
        for v in vals:
            d = Decimal(v)
            assert d == d.quantize(Decimal("0.00000001")), f"{m[0]}: {v} 는 8자리를 넘는다"
            assert d > 0, f"{m[0]}: 0 단가는 예산을 조용히 무력화한다"


# --- 배수로 계산하면 틀리는 칸 (각각 실제로 다른 값이라는 것을 고정) ------------------


def test_cache_read_ratio_is_not_a_constant():
    claude, gpt = _claude(), _gpt()

    def ratio(m):
        return Decimal(m[4][4]) / Decimal(m[4][0])

    assert ratio(claude["claude-opus-5-5"]) == Decimal("0.05")
    assert ratio(claude["claude-sonnet-5-5"]) == Decimal("0.05")
    assert ratio(claude["claude-haiku-5-5"]) == Decimal("0.1")
    assert ratio(gpt["gpt-6.1-sol"]) == Decimal("0.05")
    assert ratio(gpt["gpt-6-sol"]) == Decimal("0.1")


def test_haiku_55_long_tier_is_5x_at_100k():
    m = _claude()["claude-haiku-5-5"]
    thr, *long_vals = m[5]
    assert thr == 100000, "카드: 100,000 토큰 초과 프롬프트가 long 요율 (GPT 의 272K 가 아니다)"
    for short_v, long_v in zip(m[4], long_vals):
        assert Decimal(long_v) == Decimal(short_v) * 5


# --- SQL 가드 ----------------------------------------------------------------


def test_upgrade_guards():
    sql = _op_execute_sql(_M0041, "upgrade")
    assert "ON CONFLICT (alias) DO NOTHING" in sql
    # 열린 행이 있으면 건너뛴다 — 재실행 멱등 + 0034 부분 유니크 위반 방지
    assert re.search(r"NOT EXISTS\s*\(\s*SELECT 1 FROM model\.model_pricings[^)]*effective_until IS NULL", sql)
    # 우리 모델을 가리키는 alias 에만 붙인다
    assert re.search(r"WHERE EXISTS\s*\(\s*SELECT 1 FROM model\.model_aliases[^)]*provider_model_id", sql)
    assert "long_context_threshold_tokens" in sql


def test_downgrade_cleans_all_fk_children_and_dangling_defaults():
    assert set(_downgrade_child_tables(_M0041)) == {
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    }
    down = _op_execute_sql(_M0041, "downgrade")
    assert down.index("UPDATE model.routing_profiles") < down.index("DELETE FROM model.model_aliases")


# --- IAM 교차 검증 -----------------------------------------------------------


def _allowed_arns(tf: Path) -> list[str]:
    text = tf.read_text()
    block = re.search(r'variable "bedrock_allowed_model_arns"\s*\{(.*?)\n\}', text, re.S)
    assert block, f"{tf}: bedrock_allowed_model_arns 가 없다"
    body = re.sub(r"#[^\n]*", "", block.group(1))  # 주석 속 ARN 예시는 정책이 아니다
    return re.findall(r'"(arn:aws:bedrock:[^"]+)"', body)


def _allowed(arn: str, patterns: list[str]) -> bool:
    # IAM 와일드카드: * = 0자 이상, ? = 1자. ARN 에는 [ ] 가 없어 fnmatch 와 의미가 같다.
    return any(fnmatch.fnmatchcase(arn, p) for p in patterns)


_CLAUDE_FM_REGIONS = ["", "ap-northeast-2", "us-east-1", "us-west-2", "eu-west-1"]  # "" = global FM ARN
_GPT_US_REGIONS = ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]


@pytest.mark.parametrize("tf", _TF_ENVS, ids=lambda p: p.parent.name)
def test_iam_allows_every_0041_model_on_both_resources(tf):
    pats = _allowed_arns(tf)
    for short, (_a, profile, *_r) in _claude().items():
        assert _allowed(f"arn:aws:bedrock:ap-northeast-2:123456789012:inference-profile/{profile}", pats), profile
        for r in _CLAUDE_FM_REGIONS:
            fm = f"arn:aws:bedrock:{r}::foundation-model/anthropic.{short}"
            assert _allowed(fm, pats), f"{tf.parent.name}: {fm} 가 막힌다 → 프로파일은 통과해도 AccessDenied"
    for alias, (_a, pmid, *_r) in _gpt().items():
        assert _allowed(f"arn:aws:bedrock:us-east-2:123456789012:inference-profile/{pmid}", pats), pmid
        for r in _GPT_US_REGIONS:
            fm = f"arn:aws:bedrock:{r}::foundation-model/openai.{alias}"
            assert _allowed(fm, pats), f"{tf.parent.name}: {fm} 가 막힌다"
    # OpenAI 호환 엔드포인트는 project 리소스도 검사한다(실측: 없으면 gpt-5.6 까지 401).
    project = "arn:aws:bedrock:us-east-2:123456789012:project/default"
    assert _allowed(project, pats), f"{tf.parent.name}: {project} 가 없다 → runtime 평면 전부 401"


@pytest.mark.parametrize("tf", _TF_ENVS, ids=lambda p: p.parent.name)
def test_iam_still_blocks_fable(tf):
    pats = _allowed_arns(tf)
    for fm in ("anthropic.claude-fable-5", "anthropic.claude-fable-5-1"):
        for r in _CLAUDE_FM_REGIONS:
            arn = f"arn:aws:bedrock:{r}::foundation-model/{fm}"
            assert not _allowed(arn, pats), f"{tf.parent.name}: {arn} 가 열렸다"


# --- 실제 PostgreSQL 상태 증명 -----------------------------------------------


@_DB
@pytest.mark.asyncio
async def test_live_0041_state():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    c = _consts()
    expected = {}
    for short, profile, _d, _desc, s, lng in c["CLAUDE_MODELS"]:
        for alias in (short, profile):
            expected[alias] = ("BEDROCK", profile, None, "BEDROCK_NATIVE", s, lng)
    for alias, pmid, _d, _desc, s, lng in c["GPT_MODELS"]:
        expected[alias] = (
            "BEDROCK_RUNTIME_OPENAI", pmid, c["GPT_ENDPOINT"], "OPENAI_RESPONSES", s, lng
        )

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            head = (await conn.execute(text("SELECT version_num FROM public.alembic_version"))).scalar()
            if head is None or head < "0041":
                pytest.skip(f"DB head={head} — 0041 미적용")
            rows = (
                await conn.execute(
                    text(
                        "SELECT a.alias, a.provider::text, a.provider_model_id, a.endpoint_url,"
                        "       a.api_format::text, a.status::text, a.allowed_clients,"
                        "       p.input_price_per_1k_tokens, p.output_price_per_1k_tokens,"
                        "       p.cache_creation_5m_price_per_1k_tokens,"
                        "       p.cache_creation_1h_price_per_1k_tokens,"
                        "       p.cache_read_price_per_1k_tokens,"
                        "       p.long_context_threshold_tokens,"
                        "       p.long_context_input_price_per_1k_tokens,"
                        "       p.long_context_output_price_per_1k_tokens,"
                        "       p.long_context_cache_creation_5m_price_per_1k_tokens,"
                        "       p.long_context_cache_creation_1h_price_per_1k_tokens,"
                        "       p.long_context_cache_read_price_per_1k_tokens,"
                        "       (SELECT count(*) FROM model.model_pricings q"
                        "         WHERE q.model_alias = a.alias AND q.effective_until IS NULL) AS n_open"
                        "  FROM model.model_aliases a"
                        "  LEFT JOIN model.model_pricings p"
                        "    ON p.model_alias = a.alias AND p.effective_until IS NULL"
                        " WHERE a.alias = ANY(:aliases)"
                    ),
                    {"aliases": list(expected)},
                )
            ).fetchall()
        async with engine.connect() as conn:
            stale = [r[0] for r in (await conn.execute(text(
                "SELECT a.alias FROM model.model_aliases a WHERE a.status = 'ACTIVE' AND NOT EXISTS ("
                "  SELECT 1 FROM model.model_pricings p WHERE p.model_alias = a.alias"
                "   AND p.effective_from <= now()"
                "   AND (p.effective_until IS NULL OR p.effective_until > now()))"
            ))).fetchall()]
    finally:
        await engine.dispose()

    got = {r[0]: r for r in rows}
    assert set(got) == set(expected), f"누락: {set(expected) - set(got)}"
    # 해석기와 **같은 조건**으로 "지금 유효한" 단가가 있는지. 행이 있어도 effective_from 이
    # 미래면 gateway 는 단가 없음 → cost 0 으로 기록한다(2026-10-14 dev 실측).
    assert not stale, f"지금 유효한 단가가 없는 ACTIVE alias: {stale} → cost_usd 0 으로 기록된다"
    for alias, (provider, pmid, endpoint, api_format, short, long) in expected.items():
        r = got[alias]
        assert (r[1], r[2], r[3], r[4], r[5]) == (provider, pmid, endpoint, api_format, "ACTIVE"), alias
        assert r[6] is None, f"{alias}: allowed_clients 는 NULL(제한 없음)이어야 한다 — '{{}}' 는 전면 차단"
        assert r[18] == 1, f"{alias}: 열린 단가 행이 {r[18]}개"
        assert tuple(r[7:12]) == tuple(Decimal(x) for x in short), alias
        if long is None:
            assert tuple(r[12:18]) == (None,) * 6, alias
        else:
            assert r[12] == long[0], alias
            assert tuple(r[13:18]) == tuple(Decimal(x) for x in long[1:]), alias
