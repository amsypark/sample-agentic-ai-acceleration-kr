# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS 계정 레지스트리: model.aws_accounts + model_aliases.account_name (트리거로 사본 유지)

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-14

## 무엇을 추가하나

운영자는 계정을 **한 번** 등록한다(짧은 이름, 표시 이름, 게이트웨이가 assume 할 역할,
ExternalId). 그리고 그 계정을 써야 하는 alias 마다 **이름으로** 고른다. 0043 만으로는 역할
ARN 을 alias 행마다 붙여 넣어야 해서, 한 계정을 쓰는 alias 가 여럿이면 ARN(과 ExternalId)
사본 N 개를 손으로 맞춰야 하고 어떤 계정이 있는지 볼 곳도 없다.

    model.aws_accounts (name PK, display_name, role_arn UNIQUE, external_id, description, ...)
    model.model_aliases.account_name → aws_accounts(name) ON DELETE RESTRICT

## 왜 0043 컬럼을 남기나 — 트리거가 유지하는 사본

``model_aliases.account_role_arn`` / ``account_external_id`` 는 계속 **게이트웨이가 읽는
컬럼**이다. 운영자가 쓰는 것은 ``account_name`` 하나이고, 두 트리거가 사본을 정확히 유지한다:

  * ``trg_alias_account_materialize`` (BEFORE INSERT/UPDATE ON model_aliases):
    account_name 이 있으면 레지스트리 행의 role_arn/external_id 를 복사한다. 선택을
    **해제**하는 UPDATE(이전 값 있음 → NULL)면 사본도 비운다. 선택 없이 사본만 쓰는 쓰기는
    건드리지 않는다 — 아래 CHECK 가 그것을 시끄럽게 거부한다(조용히 버리지 않는다).
  * ``trg_aws_account_propagate`` (AFTER UPDATE OF role_arn, external_id ON aws_accounts):
    그 계정을 고른 모든 alias 에 같은 트랜잭션에서 다시 복사하고 updated_at 을 찍는다
    (그 alias 의 청구 계정이 바뀐 시각이다). 값이 실제로 바뀔 때만 발화한다(WHEN ...
    IS DISTINCT FROM) — 같은 값을 다시 저장해도 updated_at 이 흔들리지 않는다.

그리고 ``ck_model_aliases_account_materialized``: 선택과 사본은 함께 있거나 함께 없다.

반대 설계(게이트웨이가 레지스트리를 조인하고 0043 컬럼을 지운다)는 버전이 섞이는 모든 순간을
청구 위험으로 만든다: 0043 컬럼을 명시 매핑한 이미지는 컬럼이 사라지면 모든 모델 조회가
42703 이고, 롤백에는 장애 없는 순서가 없다. 사본을 유지하면 구·신 이미지가 같은 값을 읽고,
이 마이그레이션은 **게이트웨이를 전혀 바꾸지 않는다.**

## 왜 admin-api 만이 아니라 트리거인가

사본이 곧 청구다. admin-api 도 일관되게 쓰지만, 한 작성자만 유지하는 사본은 누가 다른
방법(장애 중 psql, 미래의 두 번째 작성자)으로 행을 고치는 순간 어긋난다. 트리거가 DB 자체를
보증으로 만든다.

## 왜 UNIQUE(role_arn) 인가

역할 하나 = 계정 항목 하나. 같은 역할에 ExternalId 가 다른 항목 둘이 있으면 "이 역할이 원하는
ExternalId" 가 어느 alias 가 묻느냐에 따라 달라진다 — 게이트웨이 캐시가 (역할, ExternalId)
로 키를 잡는 바로 그 confused-deputy 모호성이다. 백필은 기존 데이터가 그런 모양이면 진행을
거부한다.

## 이름 규칙

``^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$`` (3-64자) 이고 ``default`` / ``gateway`` / ``inherit`` /
``none`` 은 예약어다 — 콘솔에서 "기본값" 과 구별되지 않는 이름을 막는다. 이름은 FK 대상이자
로그·콘솔에 보이는 식별자라 바꿀 수 없다(바꾸려면 새로 등록하고 alias 를 옮긴다).

## 멱등 — init SQL 이 먼저 돈다

init SQL 이 같은 모양을 만든다. 그래서 여기 모든 문장은 존재 확인·``OR REPLACE`` 다. 트리거는
``CREATE OR REPLACE TRIGGER`` (PostgreSQL 14+, 배포는 Aurora 16.11 / 로컬 pg16)로 만든다 —
``DROP TRIGGER`` 뒤 ``CREATE TRIGGER`` 는 그 사이에 들어온 레지스트리 수정이 alias 로
전파되지 않는 창을 만든다(init SQL 은 문장마다 autocommit 이다). init SQL 은 기존 행이 백필
전이라 materialized CHECK 를 ``NOT VALID`` 로만 만들 수 있고, 백필 뒤 여기서 VALIDATE 한다.

## 배포 순서

스키마 먼저(0043 과 같다). 새 admin-api 가 account_name 을 매핑한다. 이 마이그레이션을 구
이미지보다 먼저 적용하는 것은 안전하다 — 구 이미지는 account_name 을 모르고, 게이트웨이가
읽는 사본은 그대로다.

## 되돌리기

0043 컬럼에 사본이 남으므로 downgrade 후에도 **모든 alias 가 같은 계정으로 호출된다.** 잃는
것은 레지스트리의 이름·표시 이름·설명뿐이다(필요하면 먼저 ``SELECT * FROM model.aws_accounts``).
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

#: 0043 / db/init/02_create_tables.sql 과 글자까지 같다. 콜론이 [:] 인 이유는 0043 참고.
ROLE_ARN_REGEX = "^arn[:]aws(-[a-z]+)*[:]iam[:][:][0-9]{12}[:]role/[A-Za-z0-9+=,.@_/-]+$"
NAME_REGEX = "^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$"
#: sts:AssumeRole ExternalId 문자 집합(ASCII). 길이 2..1224 는 char_length 로 따로 본다 —
#: PostgreSQL 정규식의 반복 상한은 255 라 {2,1224} 를 쓸 수 없다.
EXTERNAL_ID_REGEX = "^[A-Za-z0-9_+=,.@:/-]+$"


def upgrade() -> None:
    # 기다리는 DDL 뒤에는 이후의 읽기·쓰기가 모두 줄을 선다. 5초 상한으로 멈춘 락을 걸린 배포가
    # 아니라 실패한 Job 으로 바꾼다. transaction_per_migration 이라 SET LOCAL 은 이 리비전에만 걸린다.
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model.aws_accounts (
            name          VARCHAR(64)   PRIMARY KEY,
            display_name  VARCHAR(128)  NOT NULL,
            role_arn      VARCHAR(2048) NOT NULL,
            external_id   VARCHAR(1224),
            description   TEXT,
            created_by    UUID          NOT NULL REFERENCES auth.users(id),
            created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
        )
        """
    )
    # 제약은 이름으로 하나씩 확인해 붙인다 — init SQL 이 테이블을 먼저 만들었으면
    # CREATE TABLE IF NOT EXISTS 가 통째로 건너뛰어 인라인 제약이 생기지 않는다.
    for name, ddl in (
        (
            "ck_aws_accounts_name",
            f"CHECK (name ~ '{NAME_REGEX}' "
            "AND name NOT IN ('default', 'gateway', 'inherit', 'none'))",
        ),
        ("ck_aws_accounts_role_arn", f"CHECK (role_arn ~ '{ROLE_ARN_REGEX}')"),
        (
            "ck_aws_accounts_external_id",
            "CHECK (external_id IS NULL OR (char_length(external_id) BETWEEN 2 AND 1224 "
            f"AND external_id ~ '{EXTERNAL_ID_REGEX}'))",
        ),
        ("uq_aws_accounts_role_arn", "UNIQUE (role_arn)"),
    ):
        op.execute(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = '{name}'
                       AND conrelid = 'model.aws_accounts'::regclass
                ) THEN
                    ALTER TABLE model.aws_accounts ADD CONSTRAINT {name} {ddl};
                END IF;
            END $$
            """
        )

    # ADD COLUMN IF NOT EXISTS 는 no-op 이어도 ACCESS EXCLUSIVE 를 잡는다(0043 docstring, 실 PG16
    # 측정). 그 락은 커밋까지 유지되므로 아래 백필 내내 게이트웨이의 읽기까지 막는다 — 있으면 건너뛴다.
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'model' AND table_name = 'model_aliases'
                   AND column_name = 'account_name'
            ) THEN
                ALTER TABLE model.model_aliases ADD COLUMN account_name VARCHAR(64);
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'fk_model_aliases_account_name'
                   AND conrelid = 'model.model_aliases'::regclass
            ) THEN
                -- RESTRICT: SET NULL 은 그 alias 들의 청구를 조용히 기본 계정으로 옮기고,
                -- CASCADE 는 alias 를 지운다. 둘 다 계정 삭제 한 번의 부작용으로 너무 크다.
                ALTER TABLE model.model_aliases
                    ADD CONSTRAINT fk_model_aliases_account_name
                    FOREIGN KEY (account_name) REFERENCES model.aws_accounts(name)
                    ON DELETE RESTRICT;
            END IF;
        END $$
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_aliases_account_name "
        "ON model.model_aliases (account_name) WHERE account_name IS NOT NULL"
    )

    # ── 백필: 0043 인라인 값 → 레지스트리 항목 ───────────────────────────────────────
    # 백필이 도는 동안 admin 쓰기가 들어오면 백필이 못 본 인라인 ARN 이 생길 수 있다 — 쓰기만
    # 막는다. SHARE ROW EXCLUSIVE 는 읽기와 충돌하지 않는다(실 PG16 측정). 단 이 리비전이
    # account_name 을 실제로 추가한 경우(init SQL 없이 alembic 만 도는 경로)에는 그 ADD COLUMN 의
    # ACCESS EXCLUSIVE 가 커밋까지 남아 읽기도 그동안 기다린다. 클라우드 Job 은 init SQL 이 컬럼을
    # 먼저 만들므로 이 리비전에서 잡는 락은 SHARE ROW EXCLUSIVE · SHARE(인덱스 확인) ·
    # SHARE UPDATE EXCLUSIVE(VALIDATE) 뿐이고, 셋 다 읽기를 막지 않는다.
    op.execute("LOCK TABLE model.model_aliases IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        DECLARE
            conflict text;
        BEGIN
            -- 역할 하나에 ExternalId 둘: 하나를 고르지 않고 거부한다. UNIQUE(role_arn) 이
            -- 성립할 수 없고, 고르면 일부 alias 가 STS 에 내미는 ExternalId 가 조용히 바뀐다.
            SELECT string_agg(account_role_arn, ', ') INTO conflict FROM (
                SELECT account_role_arn FROM model.model_aliases
                 WHERE account_role_arn IS NOT NULL
                 GROUP BY account_role_arn
                HAVING count(DISTINCT coalesce(account_external_id, '')) > 1
            ) c;
            IF conflict IS NOT NULL THEN
                RAISE EXCEPTION
                    '0044: role(s) used with more than one ExternalId across aliases: %. '
                    'Make each role use one ExternalId, then re-run the migration.', conflict;
            END IF;

            INSERT INTO model.aws_accounts
                   (name, display_name, role_arn, external_id, description, created_by)
            SELECT DISTINCT ON (a.account_role_arn)
                   'acct-' || split_part(a.account_role_arn, ':', 5)
                           || '-' || left(md5(a.account_role_arn), 8),
                   left(substring(a.account_role_arn from '[^/]+$')
                        || ' (' || split_part(a.account_role_arn, ':', 5) || ')', 128),
                   a.account_role_arn,
                   a.account_external_id,
                   'Created by migration 0044 from model_aliases.account_role_arn',
                   a.created_by
              FROM model.model_aliases a
             WHERE a.account_role_arn IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM model.aws_accounts r WHERE r.role_arn = a.account_role_arn
               )
             ORDER BY a.account_role_arn, a.created_at;

            UPDATE model.model_aliases a
               SET account_name = r.name
              FROM model.aws_accounts r
             WHERE a.account_role_arn = r.role_arn
               AND a.account_external_id IS NOT DISTINCT FROM r.external_id
               AND a.account_name IS NULL;

            -- 사후 조건. 선택 없이 인라인 ARN 만 남은 alias 는 오늘은 맞게 청구되지만 첫
            -- 콘솔 편집에서 계정이 사라진다 — 그 함정을 남기느니 거부한다.
            SELECT string_agg(alias, ', ') INTO conflict FROM model.model_aliases
             WHERE account_role_arn IS NOT NULL AND account_name IS NULL;
            IF conflict IS NOT NULL THEN
                RAISE EXCEPTION '0044: alias(es) not matched to a registry entry: %', conflict;
            END IF;
        END $$
        """
    )

    # ── 사본 유지 트리거 ───────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION model.alias_account_materialize() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            r model.aws_accounts%ROWTYPE;
        BEGIN
            IF NEW.account_name IS NOT NULL THEN
                -- FOR SHARE: 이 alias 가 커밋될 때까지 그 계정 행의 수정을 막고, 이미 진행 중인
                -- 수정이 있으면 그 커밋을 기다려 새 값을 읽는다. 락 없이 읽으면 선택과 레지스트리
                -- 수정이 겹칠 때 사본이 옛 값으로 영구히 남는다 — 실 PG16 에서 네 가지 순서 중
                -- 셋(선택 먼저·external_id 수정 / 수정 먼저·external_id / 수정 먼저·role_arn)이
                -- 그랬다. FK 검사(FOR KEY SHARE)는 role_arn 수정만, 그것도 선택이 먼저일 때만
                -- 직렬화한다. FOR SHARE 는 aws_accounts 의 UPDATE 권한이 필요한데, model_aliases
                -- 를 쓰는 롤(admin_api_user, 배포의 단일 gateway 유저)은 model 스키마 CRUD 를 갖는다.
                SELECT * INTO r FROM model.aws_accounts WHERE name = NEW.account_name FOR SHARE;
                IF NOT FOUND THEN
                    -- FK 도 거부하지만 문장 끝에서야 한다. 사본이 반쯤 채워진 행이 생기기 전에
                    -- 이름을 넣어 여기서 말한다.
                    RAISE EXCEPTION 'aws account "%" does not exist', NEW.account_name
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
                NEW.account_role_arn := r.role_arn;
                NEW.account_external_id := r.external_id;
            ELSIF TG_OP = 'UPDATE' AND OLD.account_name IS NOT NULL THEN
                -- 선택 해제: 사본도 함께 비워 alias 를 기본 계정으로 되돌린다. 이 전이에서만
                -- 그렇게 한다 — 선택 없이 사본만 쓰는 쓰기는 CHECK 가 시끄럽게 거부하게 둔다.
                NEW.account_role_arn := NULL;
                NEW.account_external_id := NULL;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE TRIGGER trg_alias_account_materialize
            BEFORE INSERT OR UPDATE ON model.model_aliases
            FOR EACH ROW EXECUTE FUNCTION model.alias_account_materialize()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION model.aws_account_propagate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- updated_at 도 올린다: 이 alias 의 청구 계정이 바뀐 시각이다.
            UPDATE model.model_aliases
               SET account_role_arn = NEW.role_arn,
                   account_external_id = NEW.external_id,
                   updated_at = now()
             WHERE account_name = NEW.name;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE TRIGGER trg_aws_account_propagate
            AFTER UPDATE OF role_arn, external_id ON model.aws_accounts
            FOR EACH ROW
            -- 값이 실제로 바뀐 때만: 같은 값을 다시 저장해도 alias 의 updated_at 이 흔들리지 않게.
            WHEN (OLD.role_arn IS DISTINCT FROM NEW.role_arn
                  OR OLD.external_id IS DISTINCT FROM NEW.external_id)
            EXECUTE FUNCTION model.aws_account_propagate()
        """
    )

    # 선택과 사본은 함께 있거나 함께 없다. init SQL 은 백필 전 행 때문에 NOT VALID 로만
    # 만들 수 있으므로 여기서 백필 뒤 검증한다.
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'ck_model_aliases_account_materialized'
                   AND conrelid = 'model.model_aliases'::regclass
            ) THEN
                ALTER TABLE model.model_aliases
                    ADD CONSTRAINT ck_model_aliases_account_materialized
                    CHECK ((account_name IS NULL) = (account_role_arn IS NULL)) NOT VALID;
            END IF;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE model.model_aliases VALIDATE CONSTRAINT ck_model_aliases_account_materialized"
    )


def downgrade() -> None:
    # 0043 컬럼에 사본이 남으므로 모든 alias 가 같은 계정으로 계속 호출된다. 잃는 것은
    # 레지스트리의 이름·표시 이름·설명뿐이다.
    op.execute(
        "ALTER TABLE model.model_aliases "
        "DROP CONSTRAINT IF EXISTS ck_model_aliases_account_materialized"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_aws_account_propagate ON model.aws_accounts")
    op.execute("DROP TRIGGER IF EXISTS trg_alias_account_materialize ON model.model_aliases")
    op.execute("DROP FUNCTION IF EXISTS model.aws_account_propagate()")
    op.execute("DROP FUNCTION IF EXISTS model.alias_account_materialize()")
    op.execute(
        "ALTER TABLE model.model_aliases DROP CONSTRAINT IF EXISTS fk_model_aliases_account_name"
    )
    op.execute("DROP INDEX IF EXISTS model.idx_model_aliases_account_name")
    op.execute("ALTER TABLE model.model_aliases DROP COLUMN IF EXISTS account_name")
    op.execute("DROP TABLE IF EXISTS model.aws_accounts")
