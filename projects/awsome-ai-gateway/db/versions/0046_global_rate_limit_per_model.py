# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GLOBAL rate limit 을 모델별로 — model alias 당 활성 GLOBAL 행 1개

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-15

## 무엇이 바뀌나

0024 의 ``uq_rate_limit_configs_active`` 는 ``(scope, COALESCE(scope_id, nil)) WHERE is_active``
였다. GLOBAL 행은 ``scope_id`` 가 NULL 이고 모델 alias 는 ``model_alias`` 컬럼에 있으므로, GLOBAL
전체가 키 하나였다 — **한 번에 한 모델**만 GLOBAL 한도를 가질 수 있었다(admin-api upsert 도 alias 와
무관하게 GLOBAL 활성 행을 전부 비활성화했다). 콘솔 트리는 모델마다 GLOBAL 노드를 보여 주는데, 한
모델에 저장하면 다른 모델의 한도가 풀렸다. Bedrock 쿼터는 모델별이다.

  * ``uq_rate_limit_configs_active`` — USER/TEAM 만 덮는다(``AND scope <> 'GLOBAL'``). 키는 그대로다:
    USER/TEAM 의 upsert 는 여전히 alias 와 무관하게 그 주체의 활성 행을 바꾼다.
  * ``uq_rate_limit_configs_global_active`` — ``(COALESCE(model_alias, '')) WHERE is_active AND
    scope = 'GLOBAL'``. 모델 alias 당 활성 GLOBAL 행 1개. COALESCE 는 0024 와 같은 NULL 규칙이다 —
    유니크 인덱스에서 NULL 은 서로 다르므로, alias 없는 GLOBAL 행(콘솔은 만들지 않지만 SQL 로는
    가능하다)도 한 칸으로 묶지 않으면 아무 제약도 받지 않는다.

## 왜 옛 이름을 그대로 쓰나

클라우드 마이그레이션 Job 은 매번 ``db/init/*.sql`` 을 alembic **보다 먼저** 돌리고, init SQL 의
``CREATE UNIQUE INDEX IF NOT EXISTS`` 는 **이름만** 본다. USER/TEAM 인덱스에 새 이름을 붙이면, 이
리비전 뒤에 이전 차트의 Job(이전 init SQL)이 한 번이라도 돌 때 옛 정의의
``uq_rate_limit_configs_active`` 가 다시 생긴다 — 활성 GLOBAL 행이 하나뿐이면 GLOBAL 이 조용히 단일
슬롯으로 돌아가고(그 뒤 두 번째 모델 저장은 유니크 위반), 둘 이상이면 그 Job 이 실패한다. 이름이
같으면 그 재실행은 no-op 이다. 대신 이름으로는 옛 정의와 새 정의를 구별할 수 없으므로, 이 리비전은
존재 여부와 무관하게 DROP 후 다시 만든다(한 DB 에서 한 번만 돈다. 테이블은 작다).

## 업그레이드는 기존 데이터를 바꾸지 않는다

0045 까지는 0024 인덱스가 활성 GLOBAL 행을 **전부 합쳐** 최대 1개로 강제했으므로 alias 당 1개는
자동으로 성립하고, USER/TEAM 의 키는 그대로다. 새 인덱스 둘 다 기존 제약에서 따라 나오는 약한
제약이라 생성이 실패할 수 없다(누가 0024 인덱스를 손으로 지웠다면 PostgreSQL 이 중복 키를 이름과
함께 보고하며 실패한다 — 그때는 0024 docstring 의 중복 정리 기준으로 먼저 정리한다).

순서: 새 GLOBAL 인덱스를 먼저 만들고(SHARE — 읽기는 막지 않는다) 옛 인덱스를 지운 뒤 USER/TEAM
인덱스를 만든다. 트랜잭션 하나라 어느 순간에도 제약이 비지 않는다. ``lock_timeout`` 은 0043·0045 와
같다 — 긴 트랜잭션 뒤에서 ACCESS EXCLUSIVE(DROP INDEX)를 기다리는 동안 이 테이블의 모든 읽기(게이트웨이
의 한도 캐시 미스)가 뒤에 줄 서지 않게, 기다리지 말고 실패한다(Job 이 재시도한다).

init SQL(``02_create_tables.sql``)이 같은 두 정의를 갖는다. 클라우드 Job 에서는 init SQL 이 먼저
GLOBAL 인덱스를 만들고(옛 인덱스가 아직 있어 단일 슬롯은 유지된다) 이 리비전이 옛 정의를 바꾼다.

## 되돌리기(downgrade) — GLOBAL 한도를 말없이 버리지 않는다

0045 의 인덱스는 활성 GLOBAL 행을 하나만 허용한다. 여러 모델이 GLOBAL 한도를 갖고 있을 때 0024 처럼
"최신 하나만 남기기" 로 자동 정리하면 나머지 모델의 Bedrock 쿼터 보호가 **아무 신호 없이** 사라진다.
어느 모델의 한도를 남길지는 운영 판단이다. 그래서:

  * 실제 한도(rpm 또는 tpm)를 가진 활성 GLOBAL 행이 2개 이상이면 **실패한다**. 메시지에 모델·한도
    목록과 정리 SQL 을 담는다. 트랜잭션이 롤백되므로 아무것도 바뀌지 않고 ``alembic_version`` 도
    0046 그대로다.
  * rpm·tpm 이 모두 NULL 인 활성 GLOBAL 행(콘솔에서 값을 비우고 저장한 모델)은 아무것도 집행하지
    않는다 — gateway-proxy 는 GLOBAL 의 cpm/cph 를 읽지 않는다. 그런 행은 비활성화한다(삭제가 아니다,
    이력은 남는다). 그래서 콘솔에서 비운 모델이 downgrade 를 막지 않는다.
  * 판정 전에 테이블을 SHARE ROW EXCLUSIVE 로 잠근다 — 판정과 인덱스 생성 사이에 끼어든 저장이 판정을
    무효화하지 못한다(읽기는 막지 않는다).

⚠️ 다운그레이드한 DB 는 **같은 차트의 다음 마이그레이션 Job 이 돌 때까지만** 0045 다. 그 Job 의 init SQL 이
   GLOBAL 인덱스를 다시 만들고 alembic 이 이 리비전을 다시 적용한다. 0045 로 남기려면 다운그레이드 뒤에
   이 리비전이 없는 차트(이미지)로 되돌린다 — 순서와 이미지만 되돌릴 때의 동작은
   deployment/docs/eks-fargate/06-upgrade-rollback.md §4.4.
"""

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

NIL_UUID = "00000000-0000-0000-0000-000000000000"

# 0046 — USER/TEAM: 0024 와 같은 키, GLOBAL 제외.
CREATE_ACTIVE_INDEX = (
    "CREATE UNIQUE INDEX uq_rate_limit_configs_active ON model.rate_limit_configs "
    f"(scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)) "
    "WHERE is_active = true AND scope <> 'GLOBAL'"
)

# 0046 — GLOBAL: model alias 당 활성 행 1개. init SQL 이 먼저 만들었을 수 있다(IF NOT EXISTS).
CREATE_GLOBAL_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_global_active "
    "ON model.rate_limit_configs (COALESCE(model_alias, '')) "
    "WHERE is_active = true AND scope = 'GLOBAL'"
)

# 0045 의 정의(0024) — downgrade 가 되살린다.
CREATE_SINGLE_SLOT_INDEX = (
    "CREATE UNIQUE INDEX uq_rate_limit_configs_active ON model.rate_limit_configs "
    f"(scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)) "
    "WHERE is_active = true"
)

# 아무것도 집행하지 않는 활성 GLOBAL 행(rpm·tpm 모두 NULL) — downgrade 가 비활성화한다.
DEACTIVATE_EMPTY_GLOBAL_ROWS = (
    "UPDATE model.rate_limit_configs SET is_active = false, updated_at = now() "
    "WHERE scope = 'GLOBAL' AND is_active = true "
    "AND rpm_limit IS NULL AND tpm_limit IS NULL"
)

ACTIVE_GLOBAL_ROWS = (
    "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
    "WHERE scope = 'GLOBAL' AND is_active = true "
    "ORDER BY model_alias NULLS FIRST"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(CREATE_GLOBAL_INDEX)
    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_active")
    op.execute(CREATE_ACTIVE_INDEX)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE model.rate_limit_configs IN SHARE ROW EXCLUSIVE MODE")
    conn = op.get_bind()

    emptied = conn.exec_driver_sql(DEACTIVATE_EMPTY_GLOBAL_ROWS).rowcount
    held = conn.exec_driver_sql(ACTIVE_GLOBAL_ROWS).all()
    if len(held) > 1:
        listing = "\n".join(
            f"  {alias if alias is not None else '<no alias>'}: rpm={rpm}, tpm={tpm}"
            for alias, rpm, tpm in held
        )
        raise RuntimeError(
            "[0046] downgrade refused: revision 0045 allows one active GLOBAL rate limit in total, "
            f"but {len(held)} models have one:\n{listing}\n"
            "Downgrading would drop the others' Bedrock quota protection, and which one to keep is "
            "your call. Keep one and clear the rest — in the console (empty RPM and TPM, then save) "
            "or with:\n"
            "  UPDATE model.rate_limit_configs SET is_active = false\n"
            "   WHERE scope = 'GLOBAL' AND is_active\n"
            "     AND model_alias IS DISTINCT FROM '<alias to keep>';\n"
            "then rerun the downgrade. Nothing was changed."
        )

    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_active")
    op.execute(CREATE_SINGLE_SLOT_INDEX)
    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_global_active")
    if emptied:
        print(
            f"[0046] deactivated {emptied} active GLOBAL row(s) with neither rpm nor tpm "
            "(they enforced nothing); the remaining GLOBAL limit, if any, is kept"
        )
