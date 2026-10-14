# 모델 alias별 AWS 계정 지정 설정 가이드

모델 alias마다 호출할 대상 AWS 계정을 지정하는 기능입니다(마이그레이션 0043, 0044, 0045). Bedrock native(InvokeModel/Converse), Bedrock Mantle, OpenAI Bedrock Runtime 세 경로 모두에 적용됩니다.

계정은 **한 번 등록하고 모델에서 이름으로 고릅니다**(0044 계정 레지스트리). 역할 ARN과 External ID는 등록 정보 한 곳에만 있고, 그 계정을 고른 모델이 모두 따라갑니다.

> **관련 문서**
> - 게이트웨이 역할(IRSA) 구성 전반 → [IRSA-SETUP.md](IRSA-SETUP.md)
> - 앱 단위 cross-account(cowork Mantle) → [COWORK-GATEWAY-SETUP.md](COWORK-GATEWAY-SETUP.md)

---

## 1. 먼저 알아야 하는 것

### 1.1 계정은 서명한 자격증명으로 정해진다

Bedrock 호출의 대상 계정은 **요청에 서명한 자격증명**으로 결정됩니다. 엔드포인트로는 결정되지 않습니다.

- `bedrock-runtime.<region>.amazonaws.com`은 모든 계정이 공유하는 리전 단일 호스트입니다
- SigV4 크리덴셜 스코프는 `<access-key>/<date>/<region>/bedrock`이고 계정 칸이 없습니다
- foundation-model ARN은 계정 세그먼트가 비어 있습니다(`arn:aws:bedrock:<region>::foundation-model/...`)

그래서 이 기능은 엔드포인트를 바꾸는 것이 아니라 **대상 계정의 역할로 전환(AssumeRole)해 그 계정으로서 호출**합니다. 쿼터, 과금, 모델 액세스 승인이 모두 지정한 계정 기준이 됩니다. **리전은 바뀌지 않습니다** — Bedrock native는 그 요청이 원래 가던 리전(앱 프로필이 cross-account면 그 프로필의 리전, 아니면 게이트웨이 리전), Runtime OpenAI는 모델의 endpoint 호스트 리전, Mantle은 프로필 리전 그대로입니다.

### 1.2 우선순위 — 세 경로 모두 같다

```
1. 모델(alias)에 고른 계정
2. 앱(라우팅 프로필, model.routing_profiles)의 계정 — 경로별 기존 규칙 그대로
3. 게이트웨이 자신의 계정(gateway-proxy 파드의 IRSA 역할)
```

2단계는 이 기능 이전과 **똑같이** 동작합니다. 모델에 계정을 고르지 않으면(기본값) 그 모델의 호출은 이전과 같은 계정으로 갑니다. 그 계정은 경로에 따라 다릅니다.

| 경로 | 모델 계정이 기본값일 때 |
|---|---|
| Bedrock native `/v1/messages`, `/v1/messages/count_tokens` | 앱 프로필이 `backend = invoke`이고 역할이 있으면 그 계정(claude-code형 cross-account), 아니면 게이트웨이 계정 |
| Bedrock native `/model/{id}/invoke*`, `/model/{id}/converse*` | 게이트웨이 계정(이 경로는 앱 프로필을 보지 않습니다) |
| OpenAI Bedrock Runtime(`/v1/chat/completions`, `/v1/responses`) | 앱 프로필에 역할이 있으면 그 계정, 아니면 게이트웨이 계정 |
| Mantle(`/v1/messages` Mantle 모델, `/v1/responses` Mantle 모델) | 앱 프로필에 역할이 있으면 그 계정, 아니면 게이트웨이 계정 |

모델에 계정을 고르면 그 모델은 **경로와 무관하게** 고른 계정으로 호출되며, 앱 프로필의 계정보다 우선합니다. 폴백 후보(가용성 폴백)는 **그 후보 모델의** 계정으로 호출됩니다.

### 1.3 실패하면 — 다른 계정으로 대신 호출하지 않는다

모델에 고른 계정으로 전환하지 못하면(신뢰 정책, 게이트웨이 권한, External ID 불일치 등) 그 요청은 **HTTP 500**과 고정 메시지(`The AWS account configured for this model could not be used (AssumeRole failed)...`)로 끝납니다. 이때:

- 게이트웨이 계정이나 앱 프로필 계정으로 **대신 호출하지 않습니다.** 그렇게 하면 성공 응답이 운영자가 고르지 않은 계정에 청구되고, 어떤 상태 코드도 그 사실을 드러내지 않습니다.
- 가용성 폴백(다른 모델로)을 하지 않고 서킷 브레이커도 올리지 않습니다. 계정 설정 오류가 같은 모델 ID를 쓰는 다른 계정의 모델까지 끊지 않게 하기 위해서입니다.
- 실패는 30초 동안 기억합니다(그 사이 STS를 반복 호출하지 않습니다). 원인을 고치면 최대 30초 안에 정상화됩니다.
- 이미 받은 자격증명의 **갱신**(만료 5분 전부터)이 실패한 것은 실패로 치지 않습니다. 남은 유효 시간이 1분을 넘으면 그 자격증명으로 계속 서명하고 경고(`account_credentials_refresh_failed_serving_cached`)만 남기며, 갱신은 30초 간격으로 다시 시도합니다.
- 상류가 502/503을 줄 때 올라가는 서킷 브레이커는 모델에 계정을 고른 경우 **그 계정 단위**입니다(키 `cb:<model-id>@<계정 번호>:*`). 계정을 고르지 않은 모델의 키는 이전과 같습니다(`cb:<model-id>:*`). 한 계정의 장애·쿼터가 같은 모델 ID를 다른 계정으로 부르는 모델을 막지 않습니다. Bedrock native에서 계정 쿼터 초과(`ServiceQuotaExceededException`)는 429로 답하며 브레이커와 폴백 대상이 아닙니다.
- 클라이언트로 가는 Bedrock·Mantle 오류 본문과 스트림 도중의 오류 이벤트에서는 ARN과 12자리 계정 번호를 `<arn>`·`<account-id>`로 지웁니다(AccessDenied 문장에 대상 계정의 역할 ARN이 들어 있습니다). 나머지 문장은 그대로이고(클라이언트가 그 문장으로 동작합니다 — 예: `prompt is too long`), 원문은 게이트웨이 로그에 남습니다.

반면 **앱 프로필의 계정**(예: claude-code의 cross-account)은 이 기능 이전과 같이, 전환에 실패하면 게이트웨이 계정으로 투명하게 폴백합니다. 이 동작은 모델에 계정을 고르지 않은 요청에만 적용됩니다.

---

## 2. 설정 순서

아래 순서를 지켜야 합니다. 특히 2.3과 2.4의 순서가 바뀌면 게이트웨이 전체에 장애가 발생합니다.

### 2.1 대상 계정에 역할 만들기 (대상 계정)

**신뢰 정책** — 게이트웨이 역할(`<project>-<env>-gateway-proxy-bedrock`)이 이 역할을 맡을 수 있어야 합니다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::111122223333:role/llm-gateway-prod-gateway-proxy-bedrock" },
      "Action": "sts:AssumeRole",
      "Condition": { "StringEquals": { "sts:ExternalId": "<합의한-값>" } }
    }
  ]
}
```

대상 계정을 다른 조직이 소유하면 ExternalId 조건을 두는 것을 권장합니다. 두었다면 같은 값을 콘솔 등록 정보의 External ID에 입력합니다. 두지 않았다면 `Condition`을 빼고 External ID를 비워 둡니다(비워 두면 게이트웨이는 ExternalId를 아예 보내지 않습니다).

**권한 정책** — 그 역할이 Bedrock을 호출할 수 있어야 합니다. 리소스 범위는 사용할 모델에 맞춰 좁힙니다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInference",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:CountTokens"
      ],
      "Resource": [
        "arn:aws:bedrock:<region>::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:<region>:444455556666:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-*"
      ]
    }
  ]
}
```

- `bedrock:CountTokens`가 필요합니다. `/v1/messages/count_tokens`도 고른 계정으로 호출하므로, 빠지면 추론은 되는데 토큰 카운트만 403이 됩니다.
- In-Region 모델(접두사 없는 model ID)은 첫 번째 항목으로 충분합니다. Geo 추론 프로필(`us.`, `apac.` 등)은 요청이 도착 리전으로 라우팅되므로 도착 리전의 foundation-model ARN(세 번째 항목)이 필요합니다. Global 추론 프로필(`global.`)은 inference-profile ARN, 리전 foundation-model ARN, **리전 없는** foundation-model ARN(네 번째 항목)이 모두 필요합니다.
- OpenAI Bedrock Runtime 모델을 이 계정으로 보내려면 `openai.*` 모델의 foundation-model·inference-profile ARN과 `arn:aws:bedrock:<region>:444455556666:project/default`도 추가합니다(게이트웨이 IRSA 정책의 같은 항목 참고: `deployment/terraform/environments/*/variables.tf`의 `bedrock_allowed_model_arns`).
- Mantle 모델이라면 `bedrock-mantle:CreateInference`, `bedrock-mantle:GetInference`, `bedrock-mantle:CallWithBearerToken`이 추가로 필요합니다.

**모델 액세스** — 대상 계정의 Bedrock에서 사용할 모델의 액세스가 활성화되어 있어야 합니다. IAM과 별개이며, 없으면 IAM이 맞아도 `AccessDeniedException`입니다. 데이터 보존 모드가 필요한 모델(예: 0042의 Claude Fable)은 **대상 계정의 요청 리전**에서 켜야 합니다.

### 2.2 게이트웨이 역할에 AssumeRole 권한 (게이트웨이 계정)

gateway-proxy IRSA 정책에 대상 역할에 대한 `sts:AssumeRole`이 필요합니다. Terraform 모듈 `deployment/terraform/modules/irsa`의 `bedrock_account_assume_role_arns` 변수가 이 statement(`AssumeTargetBedrockAccounts`)를 만듭니다. 빈 목록(기본)이면 statement가 렌더되지 않습니다. 환경의 모듈 호출(`deployment/terraform/environments/llm-gateway-<env>/main.tf`의 `module "irsa"`)에 목록을 넘깁니다.

```hcl
module "irsa" {
  # ...
  bedrock_account_assume_role_arns = [
    "arn:aws:iam::444455556666:role/llm-gateway-bedrock",
  ]
}
```

- 와일드카드(`*`)는 변수 검증에서 거부됩니다. 게이트웨이 역할이 자신을 신뢰하는 임의의 계정 역할로 전환할 수 있게 되면, 콘솔에 ARN을 입력할 수 있는 사람이 그 모든 계정에서 Bedrock을 호출할 수 있습니다.
- 앱 프로필용 기존 statement(`AssumeCoworkMantle`, `AssumeClaudeCode374Bedrock`)와 같은 역할이면 한 곳에만 있어도 됩니다.
- `terraform plan`에서 바뀌는 것이 이 statement 하나인지 확인한 뒤 적용합니다.

### 2.3 마이그레이션 적용 (반드시 이미지 교체보다 먼저)

차트(`deployment/charts/llm-gateway`)의 마이그레이션 Job(helm pre-install/pre-upgrade 훅, `db/run_migration.sh`)이 `db/init/*.sql`을 먼저 실행한 뒤 `alembic upgrade head`를 실행합니다. init SQL이 0043~0045와 같은 스키마를 멱등으로 만들고, alembic이 0043(컬럼·CHECK), 0044(레지스트리·트리거·백필·CHECK 검증), 0045(`usage_logs.aws_account_id`)를 적용합니다. 모두 다시 실행해도 안전합니다.

적용 후 확인:

```sql
SELECT version_num FROM alembic_version;
-- 0045

SELECT column_name FROM information_schema.columns
 WHERE table_schema='model' AND table_name='model_aliases' AND column_name LIKE 'account%';
-- account_external_id, account_name, account_role_arn

SELECT tgname FROM pg_trigger
 WHERE tgname IN ('trg_alias_account_materialize', 'trg_aws_account_propagate');
-- 2건 (레지스트리 선택을 모델 행에 복사·전파하는 트리거)

SELECT conname, convalidated FROM pg_constraint
 WHERE conname LIKE 'ck_model_aliases_account%' OR conname LIKE '%aws_accounts%';
-- ck_model_aliases_account_materialized 의 convalidated 가 t

SELECT count(*) FROM information_schema.columns
 WHERE table_schema='usage' AND table_name='usage_logs' AND column_name='aws_account_id';
-- 1
```

0044는 0043의 두 컬럼(`account_role_arn`, `account_external_id`)을 지우지 않고 **레지스트리 선택의 사본**으로 유지합니다. 게이트웨이는 그 사본으로 서명하고 레지스트리를 직접 읽지 않으므로, 0044는 게이트웨이 동작을 바꾸지 않습니다. 0043만으로 이미 역할을 넣어 둔 모델이 있으면 0044가 역할마다 등록 정보를 하나씩 만들어 연결합니다. 같은 역할이 모델마다 다른 External ID로 들어가 있으면 어느 쪽이 맞는지 정할 수 없으므로 실패하고 롤백합니다(메시지에 해당 역할이 나옵니다).

### 2.4 이미지 교체

gateway-proxy, admin-api, admin-ui, cost-recorder-worker를 함께 롤링합니다.

- **스키마가 먼저인 이유**: gateway-proxy와 admin-api의 ORM이 모델 조회 때 컬럼을 명시적으로 열거합니다. 0043이 없는 데이터베이스에 새 이미지가 붙으면 모든 모델 조회가 실패합니다(Postgres 42703) — 모델 하나가 아니라 게이트웨이 전체입니다. 반대 순서(스키마 적용 후 구 이미지)는 안전합니다.
- cost-recorder-worker도 같은 이유로 0045가 먼저입니다. 새 worker는 `aws_account_id`를 명시해 저장하므로, 컬럼이 없으면 사용 기록 저장이 전부 실패합니다(요청 자체는 성공해 눈에 띄지 않습니다). 게이트웨이와 worker 사이의 순서는 상관없습니다.
- **admin-api만 먼저 올리지 마십시오.** 그 상태에서 계정을 고르면 구 게이트웨이는 그 값을 모르므로, 호출은 성공하지만 이전 계정으로 나갑니다.
- **admin-ui보다 admin-api가 먼저입니다.** 구 admin-api는 모르는 키를 422로 거부하는데(`extra="forbid"`), 새 콘솔은 모델 **생성과 수정 모두**에 `account_name`을 항상 보내고 형제 적용 시 `apply_to_siblings`도 보냅니다. 새 admin-ui + 구 admin-api 조합에서는 계정을 고르지 않아도 모델 생성·수정이 전부 422입니다.
- **계정 선택은 롤아웃이 끝나고 5분(모델 캐시 TTL) 뒤에 합니다.** 롤링 중에는 구 파드가 남아 있고, 구 파드는 계정을 모르므로 그 파드로 간 요청은 이전 계정으로 나갑니다. 구 파드가 쓴 모델 캐시 항목에는 계정 칸이 없는데, 새 파드는 그런 항목을 캐시 미스로 보고 DB에서 다시 읽습니다(그 항목을 "계정 없음"으로 읽지 않습니다).

### 2.5 콘솔에서 계정 등록 후 선택

1. 관리 콘솔 **모델** 화면의 **AWS 계정** 패널에서 **계정 등록**을 누르고 이름, 표시 이름, 역할 ARN, (필요하면) External ID를 입력합니다. 이름은 영소문자·숫자·하이픈 3-64자이고 등록 후 바꿀 수 없습니다(`default`, `gateway`, `inherit`, `none`은 예약어).
2. 모델을 편집해 **AWS 계정**에서 등록한 계정을 고릅니다. "기본값"을 고르면 1.2의 표대로 돌아갑니다. 같은 model ID를 쓰는 다른 활성 모델이 다른 계정을 부르면 다이얼로그가 그 모델들을 보여 주고 **"이 N개 alias 에도 같은 계정을 적용"**을 고르게 합니다(아래 "같은 model ID" 참고).
3. 모델 표의 **AWS 계정** 열에 표시 이름과 계정 번호가 보입니다.

알아 둘 점:

- **등록 정보를 고치면 그 계정을 고른 모델 전부의 호출·청구 계정이 함께 바뀝니다.** 역할 ARN이나 External ID를 바꾸는 저장은 영향받는 모델 목록을 보여 주는 확인을 거치고, 저장이 커밋된 뒤 그 모델들의 게이트웨이 캐시를 지웁니다.
- 한 역할은 한 번만 등록할 수 있습니다. 앱 프로필 계정과 같은 역할을 등록할 때는 External ID도 같아야 합니다.
- **같은 model ID를 쓰는 활성 모델들은 같은 계정이어야 합니다.** 게이트웨이는 요청의 모델 이름을 alias 이름과 먼저 맞추고, 같은 이름의 alias가 없을 때만 model ID로 찾습니다. 시드는 공유 model ID 9개 중 8개에 model ID와 **이름이 같은** alias를 둡니다(예: alias `claude-opus-5`와 alias `global.anthropic.claude-opus-5`가 같은 model ID). 그래서 `claude-opus-5`에만 계정을 고르면 model ID로 부르는 클라이언트는 이름이 같은 다른 alias로 해석되어 **고른 계정을 건너뜁니다**(200, 다른 계정에 청구). admin-api는 그런 저장(생성, 활성 모델의 계정·model ID 변경, 활성화)을 409로 거부하고 해당 모델과 계정을 메시지에 적습니다. 같은 저장에서 맞추려면 콘솔의 "모두 적용"(API는 `apply_to_siblings: true`)을 씁니다. 계정·model ID를 바꾸지 않는 편집(설명 등)은 막지 않습니다. OPENMODEL과 비활성 모델은 대상이 아니며, 비활성 모델은 활성화할 때 확인합니다.
- 모델이 고르고 있는 계정은 삭제할 수 없습니다(메시지에 그 모델들이 나옵니다).
- AWS 계정 패널 아래에 **앱 단위 계정**(라우팅 프로필)이 backend와 함께 표시됩니다. 여기서 관리하지 않는 앱 설정이며, 기본값인 모델이 그 경로에서 쓰는 계정입니다.
- OPENMODEL 모델에는 계정을 지정할 수 없습니다(AWS가 서빙하지 않습니다).
- 콘솔 저장은 게이트웨이의 모델 캐시를 지웁니다. 데이터베이스를 직접 고친 경우에는 최대 5분(캐시 TTL) 뒤에 반영됩니다.

---

## 3. 적용 확인

계정을 고른 모델로 요청을 보낸 뒤 **대상 계정**에서 확인합니다.

- CloudTrail에 `AssumeRole`. `RoleSessionName`은 경로별로 `gw-bedrock-xacct`(Bedrock native), `gw-bedrock-openai`(Runtime OpenAI), `gw-mantle`(Mantle)입니다 — 앱 프로필 경로가 쓰던 이름과 같습니다.
- CloudTrail에 `InvokeModel` / `InvokeModelWithResponseStream`(또는 해당 OpenAI·Mantle 이벤트).
- 게이트웨이 로그에 `account_credentials_assumed`(`source=alias`, `role_arn`, `session_name`, `with_external_id`). Bedrock native는 이어서 `bedrock_xacct_client_built`(`source=alias`).

게이트웨이 계정 쪽에는 그 호출의 Bedrock 이벤트가 **나타나지 않아야** 합니다.

### 3.1 요청별 서빙 계정 (정산용)

`usage.usage_logs.aws_account_id`에 그 요청을 실제로 서빙한 계정 번호가 남습니다(0045). 판정은 서명과 같습니다: 모델에 고른 계정, 없으면 그 경로가 실제로 서명에 쓴 앱 프로필 계정, 그 외에는 NULL(= 게이트웨이 자신의 계정). 앱 프로필 계정으로 전환하지 못해 게이트웨이 계정으로 폴백한 요청은 NULL입니다. OPENMODEL은 항상 NULL입니다.

```sql
SELECT requested_at, model_alias, client, aws_account_id, bedrock_request_id
  FROM usage.usage_logs
 ORDER BY requested_at DESC LIMIT 20;
```

Bedrock invocation log나 CloudTrail과 대조할 때는 `bedrock_request_id`를 `aws_account_id` 계정에서 찾습니다. 0045 이전 기록은 모두 NULL이며 소급해서 채우지 않습니다(그 시점의 설정을 알 수 없습니다).

---

## 4. 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| 계정 등록이 422, 역할 ARN 칸에 오류 | ARN 형식 | `arn:aws:iam::<12자리>:role/<이름>`이어야 합니다(경로·이름은 `A-Z a-z 0-9 + = , . @ _ / -`만, 와일드카드 불가). 사용자 ARN이나 Bedrock ARN은 AssumeRole 대상이 아닙니다 |
| 모델 저장이 409, `other active aliases of model id` | 같은 model ID를 쓰는 다른 활성 모델이 다른 계정을 부름 | 메시지의 모델에도 같은 계정을 적용합니다(콘솔 "모두 적용", API `apply_to_siblings: true`). 2.5의 "같은 model ID" 참고 |
| 모델 활성화가 409, `then activate it` | 활성화하려는 모델의 계정이 같은 model ID의 활성 모델과 다름 | 그 모델을 먼저 편집해 계정을 맞춘 뒤 활성화합니다 |
| 해당 모델 요청이 500, 본문에 `AssumeRole failed` | 게이트웨이가 대상 역할로 전환하지 못함(2.1 신뢰 정책, 2.2 권한, External ID 불일치 중 하나) | 게이트웨이 로그 `account_credentials_assume_failed`에 역할 ARN과 오류 코드가 남습니다. 고치면 최대 30초 안에 다음 요청부터 정상화됩니다 |
| Bedrock `AccessDeniedException`(403) | 대상 역할의 권한 정책 또는 모델 액세스 누락 | 2.1을 확인합니다 |
| `/v1/messages/count_tokens`만 403 | 대상 역할에 `bedrock:CountTokens` 누락 | 2.1 권한 정책에 추가합니다 |
| 고른 계정이 아니라 이전 계정으로 호출됨 | 이미지가 구버전이거나(2.4) 모델 캐시가 아직 갱신되지 않음 | 네 이미지가 모두 롤링됐는지 확인합니다. DB를 직접 고쳤다면 최대 5분 기다립니다 |
| 모든 모델 조회가 실패(42703) | 0043 미적용 상태로 새 이미지 기동 | 2.3을 먼저 적용합니다 |
| 계정 삭제가 409 | 아직 그 계정을 고른 모델이 있음 | 메시지의 모델을 다른 계정이나 기본값으로 바꾼 뒤 삭제합니다 |
| 계정 등록이 409, `already registered as` | 같은 역할이 다른 이름으로 이미 등록됨 | 기존 등록을 고릅니다(한 역할 = 한 등록) |
| 계정 등록이 409, `account of app` | 앱 프로필 계정과 같은 역할인데 External ID가 다름 | 앱 프로필과 같은 External ID를 씁니다 |
| 모델 저장이 400, `OPENMODEL` | OPENMODEL 모델에 계정 지정 | 기본값으로 둡니다 |
| model ID로 부른 요청이 404, 메시지에 `call different AWS accounts` | 같은 model ID를 공유하는 활성 모델들의 계정이 서로 다름(콘솔을 거치지 않고 DB를 고쳤거나, 이 규칙 이전에 만든 데이터) | 공유하는 모델들의 계정을 같게 맞추거나 alias 이름으로 요청합니다. 어느 계정으로 갈지 정할 수 없는 요청은 모든 경로에서 거절합니다 — `/v1/responses`도 기본 모델로 바꿔 서빙하지 않습니다. 해당 모델 이름은 클라이언트 응답이 아니라 게이트웨이 로그(`provider_model_id_siblings_disagree_on_account`)에 있습니다 |

---

## 5. 롤백

모델 하나만 되돌리려면 모델 편집에서 계정을 "기본값"으로 저장합니다.

- `alembic downgrade 0044` — `usage_logs.aws_account_id`가 제거됩니다. cost-recorder-worker 이미지를 먼저 되돌린 뒤 실행합니다. 호출 계정에는 영향이 없습니다.
- `alembic downgrade 0043` — 레지스트리 테이블·트리거가 제거되지만 **모델별 호출 계정은 유지됩니다**(사본 컬럼이 남습니다). 잃는 것은 등록 이름·표시 이름·설명뿐입니다. 단, 이 상태는 **같은 이미지의 다음 마이그레이션 Job이 돌 때까지만** 유지됩니다(아래 "부분 다운그레이드").
- `alembic downgrade 0042` — 사본 컬럼까지 제거되어 모든 모델이 기본값(1.2의 표)으로 돌아갑니다. 롤백으로서는 정상이지만 **과금 대상이 바뀌는 변경**이고, upgrade를 다시 해도 값은 돌아오지 않습니다. 먼저 기록해 둡니다.

```sql
SELECT alias, account_name, account_role_arn, account_external_id
  FROM model.model_aliases WHERE account_role_arn IS NOT NULL;
SELECT * FROM model.aws_accounts;
```

다운그레이드 전에 이미지를 먼저 되돌립니다(2.4의 역순).

**부분 다운그레이드(0043까지만)와 다음 마이그레이션 Job.** Job(클라우드 모드의 `db/run_migration.sh`)은 매번 `db/init/*.sql`을 alembic보다 먼저 실행하고, 이 버전의 init SQL에는 0044의 스키마가 들어 있습니다. 그래서 0043으로 내린 뒤 0044 이후 차트(이미지)로 Job이 다시 돌면, 실 PostgreSQL 16에서 다음이 일어납니다.

1. init SQL이 빈 레지스트리 테이블, `account_name` 컬럼(모두 NULL), 두 트리거, `ck_model_aliases_account_materialized`(NOT VALID)를 다시 만듭니다. 사본 컬럼에 역할이 남아 있는 모델은 이때부터 **편집이 실패합니다**(`violates check constraint "ck_model_aliases_account_materialized"` — 선택 없이 사본만 있는 행이기 때문입니다). 계정이 없는 모델은 영향이 없고, 게이트웨이는 사본으로 서명하므로 호출 계정도 그대로입니다.
2. 이어지는 `alembic upgrade head`가 0044를 다시 적용하고, 백필이 사본의 역할마다 등록 정보를 **생성된 이름**으로 만듭니다 — 이름 `acct-<계정 번호>-<8자리 hex>`, 표시 이름 `<역할 이름> (<계정 번호>)`, 설명 `Created by migration 0044 from model_aliases.account_role_arn`. 모델은 그 등록에 다시 연결되고 호출 계정은 그대로지만, 원래의 등록 이름·표시 이름·설명은 돌아오지 않습니다.

0044를 실제로 내려 둔 상태로 유지하려면 init SQL에 0044가 없는 차트(이미지)로 되돌린 뒤 내립니다. 다시 올라간 뒤에는 표시 이름·설명을 콘솔에서 고칠 수 있지만(내리기 전에 위 `SELECT * FROM model.aws_accounts`로 기록해 두십시오) 이름은 바꿀 수 없고, 같은 역할은 한 번만 등록되므로 원래 이름으로 새로 등록할 수도 없습니다.

---

## 6. 범위

- 계정 지정은 **모델(alias) 단위**입니다. "같은 모델을 앱마다 다른 계정으로"는 앱 프로필(1.2의 2단계)로, "사용자마다 다른 계정으로"는 아직 표현할 수 없습니다.
- 같은 model ID를 공유하는 활성 모델들에는 서로 다른 계정을 지정할 수 없습니다(2.5의 "같은 model ID"). DB를 직접 고쳐 어긋난 경우 model ID로 부르는 요청은 거절됩니다(4장).
- 스트림이 끊겼을 때 출력 토큰을 역산하는 내부 추정(Bedrock CountTokens)은 게이트웨이 계정으로 호출합니다. 실패하면 근사치(tiktoken)로 대신하며, 과금되지 않는 호출입니다.
