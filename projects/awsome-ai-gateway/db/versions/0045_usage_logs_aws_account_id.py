# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""usage.usage_logs.aws_account_id — 요청을 실제로 서빙한 AWS 계정 기록

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-14

## 왜 필요한가

0043/0044 이후 한 게이트웨이의 호출이 여러 계정에서 청구된다(alias 계정, 앱 단위 라우팅
프로필 계정, 게이트웨이 자신의 계정). usage_logs 한 행만 보고는 어느 계정의 청구서에
올라갔는지 알 수 없다. 정산은 행의 ``bedrock_request_id`` 를 **그 계정의** invocation log /
CloudTrail 에서 찾아야 하는데, 계정을 모르면 다른 계정이 서빙한 호출이 전부 "미매칭" 으로
보인다 — invocation logging 이 꺼진 것과 구별되지 않는다. alias 의 현재 설정으로 역산하는
것도 안 된다: 설정은 나중에 바뀐다.

## 무엇을 넣나

``aws_account_id VARCHAR(12) NULL``

  * 12자리 = 그 계정의 역할로 서명해 보낸 호출(alias 계정, 또는 alias 계정이 없을 때 오늘
    규칙으로 쓰이는 라우팅 프로필 계정).
  * NULL  = 게이트웨이 자신의 계정(파드 자격증명), OPENMODEL, 그리고 0045 이전의 모든 행.

판정은 gateway-proxy ``account_credentials.effective_account_id`` 에서 서명과 같은 규칙으로
하고, cost stream(``CostStreamEntry.aws_account_id``)을 거쳐 cost-recorder-worker 가 쓴다.
Bedrock native 의 프로필 계정이 AssumeRole 실패로 in-account 로 투명 폴백한 요청은 NULL 이다
— 실제로 서명한 쪽이 파드이기 때문이다.

## CHECK 를 걸지 않는 이유

usage_logs 는 원장이다. 형식이 어긋난 값 하나로 INSERT 가 실패하면 그 배치 전체가 기록되지
않고(XACK 안 됨) 같은 배치가 계속 재시도된다. 감사용 부가 정보 하나 때문에 과금 기록이 멈추는
것은 맞바꿀 거래가 아니다. 대신 worker 스키마가 12자리 숫자가 아닌 값을 None 으로 떨군다.

## ⚠️ 배포 순서 — DB 먼저

0039(context_tier)와 같은 위험이다. worker INSERT 가 이 컬럼을 명시하므로, 컬럼 없는 DB 에
새 worker 가 붙으면 매 flush 가 42703 으로 죽어 사용량 기록이 멈춘다(요청 자체는 성공하므로
눈에 띄지 않는다). 순서: (1) 0045 또는 같은 내용의 init SQL, (2) 이미지. init SQL 이
alembic 보다 먼저 돌므로 클라우드 Job 은 자동으로 이 순서를 지킨다.

gateway 와 worker 사이는 순서 무관하다: 새 gateway + 구 worker 면 worker 가 모르는 필드를
버리고(pydantic 기본 extra="ignore"), 구 gateway + 새 worker 면 필드 부재 → None.

NULL 허용 + 기본값 없음 → 테이블 재작성 없음. 백필 없음(과거 행의 계정은 재구성하지 않는다 —
그 시점의 alias 설정을 모른다).

## 되돌리기

worker 이미지를 먼저 되돌린 뒤(이 컬럼을 INSERT 하지 않게) 컬럼을 지운다.
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # init SQL 이 먼저 만든 환경(클라우드 모드)에서는 건너뛴다. ADD COLUMN IF NOT EXISTS 로 쓰지
    # 않는다 — no-op 이어도 ACCESS EXCLUSIVE 를 잡아(0043 docstring, 실 PG16 측정) 그동안
    # 게이트웨이의 usage_logs INSERT 가 기다린다.
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'usage' AND table_name = 'usage_logs'
                   AND column_name = 'aws_account_id'
            ) THEN
                ALTER TABLE usage.usage_logs ADD COLUMN aws_account_id VARCHAR(12);
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    # 이미지를 먼저 되돌린 뒤 지울 것(위 배포 순서의 역순).
    op.execute("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS aws_account_id")
