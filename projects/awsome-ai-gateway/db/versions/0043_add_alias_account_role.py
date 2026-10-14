# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""alias 별 대상 AWS 계정: model_aliases.account_role_arn / account_external_id

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-14

## 무엇을 추가하나

``model.model_aliases`` 에 두 컬럼:

    account_role_arn     VARCHAR(2048)  이 alias 를 서명할 대상 계정의 IAM 역할
    account_external_id  VARCHAR(1224)  그 AssumeRole 의 ExternalId

Bedrock 호출의 계정은 **서명한 자격증명**으로만 정해진다. ``bedrock-runtime.<region>``
엔드포인트는 리전 공용 호스트이고, SigV4 credential scope(``<key>/<date>/<region>/bedrock``)
에도 foundation-model ARN(계정 칸이 비어 있다)에도 계정이 없다. 그래서 "다른 계정에서
호출" 은 "그 계정의 역할을 assume 해 그 계정으로서 호출" 을 뜻하고, 쿼터·청구·모델 액세스
승인이 모두 그 계정 기준이 된다.

## 왜 alias 인가 — routing_profiles 와의 관계

계정 선택은 이미 ``model.routing_profiles.account_role_arn`` 에 있다. 그런데 그 테이블의
PK 는 ``client`` 라서 "앱 1개 = 계정 1개" 다. "같은 앱에서 이 alias 만 다른 계정" 은 그
모양으로 표현할 수 없고, 이 컬럼이 그 칸을 채운다.

``routing_profiles.account_role_arn`` 은 **지우지 않고 의미도 바꾸지 않는다.** 라이브 배포가
그 값에 의존한다(claude-code 는 Bedrock native 를, cowork 는 Mantle 을 앱 계정으로 보낸다).
게이트웨이의 우선순위는 세 경로 모두 같다:

    alias 계정(이 컬럼)  →  라우팅 프로필 계정(오늘 동작 그대로)  →  파드 IRSA

가운데 단계는 **경로별 오늘 규칙을 그대로** 따른다(Bedrock native 는 backend=invoke 인
프로필만, runtime/Mantle 은 프로필 역할이 있으면). 판정은
``gateway-proxy/src/app/services/account_credentials.py`` 한 곳에 있다.

## 왜 NULL 기본값인가

NULL = "alias 계정 없음" = 위 우선순위의 다음 단계 = **오늘의 동작 그대로**다. 기존 모든
alias 가 이 값으로 시작하므로 이 마이그레이션은 어떤 호출의 계정도 바꾸지 않는다. 보편
기본값은 양쪽으로 틀린다 — 모든 alias 에 맞는 역할은 없고, 파드 자신의 계정을 적으면 암묵적
in-account 호출이 자기 자신에 대한 AssumeRole 이 된다(파드 역할에 그 신뢰 정책이 없다).

## 왜 ExternalId 가 별도 컬럼인가

``sts:AssumeRole`` 이 ``ExternalId`` 를 따로 받고, 대상 계정을 남이 소유할 때 confused-deputy
를 막는 장치다. ARN 문자열에 끼워 넣으면 호출마다 다시 파싱해야 하고, 리소스 식별자로 로그에
찍히는 칸에 공유 비밀을 넣게 된다.

## CHECK 두 개

  * ``ck_model_aliases_account_role_arn`` — IAM **역할** ARN 문법. 오타가 그 alias 의 첫
    호출에서야 STS 오류로 드러나면, 드물게 쓰는 alias 는 편집 후 한참 뒤에야 발견된다.
    문법만 본다(역할이 존재하는지·assume 가능한지는 CHECK 가 알 수 없는 런타임 사실이다).
    파티션은 ``aws`` / ``aws-<소문자>`` 만(``arn:awsfoo`` 거부), 역할 경로·이름은 IAM 문자
    집합 ASCII ``[A-Za-z0-9+=,.@_/-]`` 만 받는다 — ``role/*`` · ``role/gw-*`` 같은 와일드카드는
    STS 에는 실패할 뿐이지만 같은 값이 IAM 정책 Resource 로 가면 그 계정의 모든 역할을 연다.
    admin-api(``schemas/models.py``) · terraform irsa 변수 · admin-ui 와 같은 문법이다.
  * ``ck_model_aliases_account_external_id_needs_role`` — 역할 없이 ExternalId 만 있는
    조합은 붙일 AssumeRole 이 없어 아무 효과가 없다.

★ 정규식의 콜론은 문자 클래스 ``[:]`` 로 쓴다. alembic 의 ``op.execute(str)`` 는 문자열을
``text()`` 로 감싸고, SQLAlchemy 는 ``:name`` 꼴을 바인드 파라미터로 훑는다. ARN 문법에는
``:role`` 이 들어가므로 리터럴 콜론은 ``A value is required for bind parameter 'role'`` 로
죽는다(이 저장소의 실 PG 증명에서 재현). ``[:]`` 는 Postgres 에는 같은 뜻이고 바인드
스캔에는 걸리지 않는다. ``\\:`` 이스케이프보다 안전하다 — 역슬래시는 누가 "정리" 하면
사라지고, 그때는 에러가 아니라 **조용히 틀린 CHECK** 가 남는다.

## 멱등 — init SQL 이 먼저 돈다

클라우드 모드의 ``run_migration.sh`` 는 ``db/init/*.sql`` 을 alembic **전에** 돌리고, init
SQL 은 이 컬럼과 CHECK 를 미러링한다. 그래서 기존 DB 에서는 이 마이그레이션이 돌 때 이미
다 있다. 컬럼도 제약도 존재를 먼저 보고 없을 때만 추가한다 — 맨 ``ADD CONSTRAINT`` 는 그
순서에서 DuplicateObject 로 Job 을 실패시킨다. 컬럼에 ``ADD COLUMN IF NOT EXISTS`` 를 쓰지
않는 이유: 컬럼이 이미 있어 no-op 이어도 ACCESS EXCLUSIVE 를 잡는다(실 PG16 측정 — 그 트랜잭션
동안 다른 연결의 ``SELECT count(*)`` 가 lock_timeout 으로 실패). 게이트웨이의 모든 모델 조회가
이 테이블을 읽으므로, 할 일이 없는 재실행이 읽기를 막을 이유가 없다.

## 배포 순서 — 스키마 먼저

두 ORM(gateway-proxy, admin-api)이 이 컬럼을 명시 열거하므로, 컬럼 없는 DB 에 새 이미지가
붙으면 모델 조회마다 42703 UndefinedColumn = 게이트웨이 전면 장애다. 이 마이그레이션(또는
같은 내용의 init SQL)을 이미지보다 먼저 적용한다. 반대 순서(이 마이그레이션 후 구 이미지)는
안전하다 — 컬럼이 nullable 이고 구 이미지는 읽지 않는다.

## 되돌리기

컬럼을 지우면 모든 alias 가 우선순위의 다음 단계(프로필 → 파드)로 돌아간다. 0043 이전
동작이므로 롤백으로서는 맞지만 **청구 계정이 바뀌는 변경**이고, upgrade 를 다시 돌려도 값은
돌아오지 않는다. 필요하면 먼저 기록한다::

    SELECT alias, account_role_arn, account_external_id
      FROM model.model_aliases WHERE account_role_arn IS NOT NULL;
"""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


#: 0044 / db/init/02_create_tables.sql 과 글자까지 같다. 콜론이 [:] 인 이유는 위 docstring.
ROLE_ARN_REGEX = "^arn[:]aws(-[a-z]+)*[:]iam[:][:][0-9]{12}[:]role/[A-Za-z0-9+=,.@_/-]+$"


def _add_column_if_missing(column: str, ddl_type: str) -> None:
    # information_schema 만 읽으면 테이블 락이 없다(실 PG16 측정: pg_locks 에 0건).
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'model' AND table_name = 'model_aliases'
                   AND column_name = '{column}'
            ) THEN
                ALTER TABLE model.model_aliases ADD COLUMN {column} {ddl_type};
            END IF;
        END $$
        """
    )


def upgrade() -> None:
    # 컬럼을 실제로 추가할 때는 ACCESS EXCLUSIVE 가 필요하다. 그 락을 기다리는 동안 뒤에 오는
    # 게이트웨이 읽기가 전부 줄을 서므로, 5초 상한으로 멈춘 락을 걸린 배포가 아니라 실패한 Job
    # 으로 바꾼다. transaction_per_migration 이라 SET LOCAL 은 이 리비전에만 걸린다.
    op.execute("SET LOCAL lock_timeout = '5s'")
    # VARCHAR(2048): 역할 ARN 은 그보다 훨씬 짧지만 role path 는 운영자가 정한다.
    # routing_profiles 의 512 는 깊은 path + 긴 역할 이름에 여유가 없다.
    _add_column_if_missing("account_role_arn", "VARCHAR(2048)")
    # 1224 = sts:AssumeRole ExternalId 의 상한.
    _add_column_if_missing("account_external_id", "VARCHAR(1224)")
    # 제약 이름은 테이블 단위로 유일하므로 conrelid 로 범위를 좁혀 확인한다.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'ck_model_aliases_account_role_arn'
                   AND conrelid = 'model.model_aliases'::regclass
            ) THEN
                ALTER TABLE model.model_aliases
                    ADD CONSTRAINT ck_model_aliases_account_role_arn
                    CHECK (
                        account_role_arn IS NULL
                        OR account_role_arn ~ '{ROLE_ARN_REGEX}'
                    );
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'ck_model_aliases_account_external_id_needs_role'
                   AND conrelid = 'model.model_aliases'::regclass
            ) THEN
                ALTER TABLE model.model_aliases
                    ADD CONSTRAINT ck_model_aliases_account_external_id_needs_role
                    CHECK (account_external_id IS NULL OR account_role_arn IS NOT NULL);
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    # ⚠️ 청구 계정 변경이다(docstring 참고). 값은 upgrade 로 복구되지 않는다.
    op.execute(
        "ALTER TABLE model.model_aliases "
        "DROP CONSTRAINT IF EXISTS ck_model_aliases_account_external_id_needs_role"
    )
    op.execute(
        "ALTER TABLE model.model_aliases "
        "DROP CONSTRAINT IF EXISTS ck_model_aliases_account_role_arn"
    )
    op.execute("ALTER TABLE model.model_aliases DROP COLUMN IF EXISTS account_external_id")
    op.execute("ALTER TABLE model.model_aliases DROP COLUMN IF EXISTS account_role_arn")
