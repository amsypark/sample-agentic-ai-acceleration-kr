# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""단가 열의 소수 자릿수 확대: NUMERIC(10,6) → NUMERIC(12,8)

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-13

## 무엇이 문제였나

단가 열 10개가 모두 ``NUMERIC(10,6)`` 이다 — 소수 6자리. per-1K 요율이 6자리를 넘으면
Postgres 는 **거부하지 않고 반올림한다**. 에러가 없으므로 잘못된 단가가 조용히 저장되고,
그 값으로 비용·예산이 계산된다.

실측 (PostgreSQL 16, 2026-10-13)::

    CREATE TABLE p(c6 NUMERIC(10,6), c8 NUMERIC(12,8));
    INSERT INTO p VALUES (0.0001375, 0.0001375);
    SELECT c6, c8 FROM p;
    --   c6 = 0.000138        ← 반올림됨
    --   c8 = 0.00013750      ← 그대로

Haiku 5.5 의 ``us.`` 5분 캐시 쓰기 요율이 정확히 그 경우다: **0.0001375 / 1K** 는
``0.000138`` 로 저장되어 캐시 쓰기 비용이 **+0.3636%** 과대 청구된다(실측 오차율).
소수 7자리 요율은 앞으로도 나온다 — 캐시 쓰기·읽기는 입력 요율의 1/10 수준이라
모델이 싸질수록 자릿수가 늘어난다.

## ⚠️ 이 마이그레이션은 데이터 입력 **전에** 돌아야 한다

``ALTER TYPE`` 으로 자릿수를 늘려도 **이미 반올림된 값은 복구되지 않는다**(실측)::

    ALTER TABLE p ALTER COLUMN c6 TYPE NUMERIC(12,8);
    SELECT c6 FROM p;   -- 0.00013800  ← 0.0001375 로 돌아오지 않는다

즉 7자리 요율 모델을 등록하기 전에 이 마이그레이션이 선행돼야 한다. 이미
6자리로 반올림되어 들어간 기존 행이 있다면 **열 확대만으로는 교정되지 않고 재입력이
필요하다**. 현재 등록된 모델은 전부 6자리 이내이므로 교정 대상은 없다(등록 시 확인할 것).

## 왜 (12,8) 인가

- 소수 **8자리**: 관측된 최소 자릿수 요구가 7자리(0.0001375)다. 한 자리 여유를 둔다.
- 정수부 **4자리**(precision 12 - scale 8): per-1K 요율이므로 현실적 상한이 수백 달러
  수준이다. 기존 (10,6) 의 정수부도 4자리였으므로 **정수부 범위는 그대로**다 —
  이 변경은 소수 자릿수만 늘린다.

## 범위

``model.model_pricings`` 의 단가 열 10개 전부. 기본 5개와 ``long_context_`` 5개를
같이 올린다. 한쪽만 올리면 같은 요율을 짧은/긴 컨텍스트에서 다르게 반올림한다.

``long_context_threshold_tokens`` 는 INTEGER 라 대상이 아니다.

## 되돌리기

downgrade 는 (10,6) 으로 되돌린다. ⚠️ 그 과정에서 7자리 값은 반올림되어 **데이터가
손실된다**(위와 같은 이유). 되돌릴 일이 있으면 7자리 요율 모델을 먼저 비활성화할 것.
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


#: ``model.model_pricings`` 의 per-1K 단가 열. 기본 5 + long_context 5.
#: long_context 열은 0038 이 ``ADD COLUMN IF NOT EXISTS`` 로 넣었으므로
#: 환경에 따라 없을 수 있다 → 각 ALTER 를 존재 확인 후 수행한다.
_PRICE_COLUMNS: tuple[str, ...] = (
    "input_price_per_1k_tokens",
    "output_price_per_1k_tokens",
    "cache_creation_5m_price_per_1k_tokens",
    "cache_creation_1h_price_per_1k_tokens",
    "cache_read_price_per_1k_tokens",
    "long_context_input_price_per_1k_tokens",
    "long_context_output_price_per_1k_tokens",
    "long_context_cache_creation_5m_price_per_1k_tokens",
    "long_context_cache_creation_1h_price_per_1k_tokens",
    "long_context_cache_read_price_per_1k_tokens",
)


def _alter_scale(precision: int, scale: int) -> None:
    """존재하는 단가 열만 ``NUMERIC(precision, scale)`` 로 바꾼다.

    ``IF EXISTS`` 가 ``ALTER COLUMN ... TYPE`` 에는 없으므로 information_schema 로
    확인한 뒤 실행한다. 0038 의 long_context 열이 환경에 따라 없을 수 있기 때문이다.
    """
    for col in _PRICE_COLUMNS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'model'
                      AND table_name   = 'model_pricings'
                      AND column_name  = '{col}'
                ) THEN
                    ALTER TABLE model.model_pricings
                        ALTER COLUMN {col} TYPE NUMERIC({precision},{scale});
                END IF;
            END $$;
            """
        )


def upgrade() -> None:
    _alter_scale(12, 8)


def downgrade() -> None:
    # ⚠️ 7자리 요율은 여기서 반올림되어 손실된다(docstring 참고).
    _alter_scale(10, 6)
