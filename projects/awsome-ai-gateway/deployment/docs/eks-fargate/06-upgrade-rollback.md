# 06. Upgrade · Rollback

**목적**: 코드 변경을 운영에 반영하는 방법과, 문제 생기면 되돌리는 방법.

---

## 1. 코드만 변경된 경우 (가장 흔한 케이스)

### 1.1 버전 올리기

```bash
cd /path/to/LLM-Gateway-Vanilla
NEW_VERSION="1.0.1"
```

### 1.2 이미지 다시 빌드 + 푸시

```bash
# 변경된 서비스만 빌드
docker build --platform linux/amd64 \
  -t "$ECR_BASE/gateway-proxy:$NEW_VERSION" \
  ./gateway-proxy
docker push "$ECR_BASE/gateway-proxy:$NEW_VERSION"
```

### 1.3 Helm upgrade

> 🔀 **롤백 플래그는 Helm 메이저에 따라 다르다.** 아래 예시는 `--atomic`(v3 기준, v4 에서도
> deprecation warning 만 내고 동작) 으로 적어 뒀다. **Helm 4** 를 쓰면
> `--rollback-on-failure` 가 권장 이름이다. 반대로 v3 에는 `--rollback-on-failure` 가
> 없어 unknown flag 로 죽는다. `install-eks.sh` 는 `helm version` 을 읽어 자동으로
> 고르므로 스크립트 경로에서는 신경 쓰지 않아도 된다 — 손으로 `helm upgrade` 를 칠
> 때만 자기 버전에 맞춰 바꿀 것(`helm version --template '{{.Version}}'`).

방법 A — `install-eks.sh` 재사용 (모든 서비스가 같은 버전 쓸 때):
```bash
# 편집: values-eks-fargate-dev.yaml 에서 image.tag 설정, 또는 --set 직접
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  --namespace llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  --set gatewayProxy.image.tag=$NEW_VERSION \
  --atomic --wait --timeout 15m
```

방법 B — 특정 서비스만 업그레이드:
```bash
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  --reuse-values \
  --set gatewayProxy.image.tag=$NEW_VERSION \
  --namespace llm-gateway \
  --atomic --wait
```

⚠️ `--reuse-values` 주의: 이전 `--set` 값은 유지되지만 **values 파일 변경은 반영 안 됨**. values 파일 수정 시엔 `-f` 로 다시 줘야 함.

### 1.4 롤링 업데이트 진행 확인

```bash
kubectl rollout status deployment/llm-gateway-gateway-proxy \
  -n llm-gateway --timeout=10m
```

✅ `deployment "llm-gateway-gateway-proxy" successfully rolled out`

Pod 교체 진행 상황:
```bash
kubectl get pods -n llm-gateway -l app.kubernetes.io/component=gateway-proxy -w
```

`Terminating` → 새 `Pending` → `Running` 순서로 하나씩 교체.

### 1.5 서비스 간 배포 순서 — admin-api 먼저

admin-api 와 admin-ui 가 함께 바뀌면 **admin-api 를 먼저** 올린다. helm 한 번의 `upgrade` 로 둘을 같이
올려도 롤링 동안 몇 분은 두 버전이 섞인다 — 새 admin-api × 이전 admin-ui, 이전 admin-api × 새 admin-ui 가
모두 생긴다. 그래서 admin-api 응답은 **더하기만** 한다.

- 이전 admin-ui 가 읽는 필드는 이름·타입·의미를 그대로 둔다. 새 의미는 새 필드로 낸다.
- admin-ui 는 새 필드가 없는 응답(이전 admin-api)에서도 동작하고, 없는 필드에서 거부·차단 같은 상태를
  추론하지 않는다.

예: `GET /admin/my/budget` 은 개인 예산이 없어도 예전처럼 `limit_usd: 0` · `policy: "HARD_BLOCK"` ·
`usage_pct: 0` 을 보내고, '미설정' 은 더한 필드 `budget.configured` 로만 알린다. 한때 이 값들을 `null` 로
바꿨더니 admin-api 를 먼저 올린 동안 이전 카드가 `null.toFixed()` 로 죽었고(미설정·$0 사용자), 반대 순서에서는
새 카드가 기본 상태의 모든 사용자에게 "$0 → 차단" 을 그렸다. 두 방향을 테스트가 고정한다.

- admin-api `tests/unit/test_my_budget_wire_fixture.py` — 이전 카드가 `.toFixed()` 하는 값이 모든 상태에서
  숫자이고, main 이 알던 상태에서는 main 과 같은 값이다.
- admin-ui `tests/unit/components/MyBudgetCard.test.tsx` — 새 카드를 main 의 실제 응답
  (`tests/fixtures/my-budget.json` 의 `main`)으로 그린다.

---

## 2. values/config 변경 (코드 동일)

예: HPA 최대 replica 증설, 리소스 requests 조정, 환경변수 추가.

### 2.1 values 파일 편집

```bash
vim deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml
```

### 2.2 Helm upgrade

```bash
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  --namespace llm-gateway \
  --atomic --wait
```

⚠️ **Secret/ConfigMap 변경** → Pod 자동 재시작됨 (template에 `checksum/config` annotation 주입으로).

---

## 3. 인프라 변경 (Terraform)

Aurora 인스턴스 클래스 변경, VPC CIDR 확장 등 **인프라 구조 변경**은 Terraform을 거칩니다.

### 3.1 Terraform plan 으로 변경사항 확인

```bash
cd deployment/terraform/environments/$ENV
vim terraform.tfvars  # 값 수정
terraform plan
```

⚠️ 출력의 **`destroy` 필드** 반드시 확인:
- `aws_rds_cluster_instance` 가 replace되면 **짧은 다운타임**
- `aws_vpc` 나 `aws_subnet` 이 replace되면 **전체 재생성** → ❌ 절대 바로 apply 금지

### 3.2 terraform apply

```bash
terraform apply
```

---

## 4. 롤백

### 4.1 Helm 롤백 (가장 빠름, 추천)

```bash
# 릴리즈 이력 확인
helm history llm-gateway -n llm-gateway
```

```
REVISION   UPDATED                   STATUS       CHART                  APP VERSION   DESCRIPTION
1          Mon Apr 22 15:30:00 2026  superseded   llm-gateway-0.1.0   1.0.0         Install complete
2          Tue Apr 23 10:15:00 2026  deployed     llm-gateway-0.1.0   1.0.1         Upgrade complete   ← 문제 있음
```

revision 1 로 되돌리기:

```bash
helm rollback llm-gateway 1 -n llm-gateway --wait --timeout 10m
```

✅ 1~2분 내 Pod가 이전 이미지로 교체됨.

### 4.2 롤백이 안전하지 않은 경우

- **DB 스키마가 바뀐 경우**: 구 이미지가 새 스키마를 이해 못해 CrashLoop
  - **해결**: 마이그레이션은 반드시 **backward-compatible** 하게. 컬럼 추가는 nullable, 삭제는 2단계 (코드에서 use 중단 → 다음 릴리즈에서 drop)
- **Secret 스키마가 바뀐 경우**: 구 이미지가 parse 못 함
  - **해결**: Secret 키는 추가만 하고 제거는 2단계
- **API 응답 모양이 바뀐 경우**: admin-api 만 되돌리면 새 admin-ui × 이전 admin-api 가 된다
  - **해결**: 응답은 더하기만 하고, admin-ui 는 새 필드가 없는 응답에서도 동작하게 만든다(위 1.5)

### 4.3 강제 롤백 (긴급)

⚠️ **다운타임 발생** — 모든 Pod가 삭제 후 재생성됩니다. 4.1 롤백이 불가할 때만 사용.

⚠️ **DB 가 이전 차트보다 앞서 있으면(이전 차트에 없는 마이그레이션이 적용돼 있으면) 이전 차트를 uninstall →
install 하지 않는다.** install 의 pre-install 훅(마이그레이션 Job)이 `alembic upgrade head` 에서
`Can't locate revision identified by '<지금 리비전>'` 으로 실패하고, 훅이 실패하면 Deployment 가 하나도 만들어지지
않는다 — uninstall 로 이미 다 지웠으므로 복구될 때까지 전면 중단이다. 0043~0046 모두 해당한다. **먼저 DB 를
이전 차트의 alembic head 로 내린 뒤**(4.4 의 "다운그레이드 Job") 이 절차를 쓴다.

```bash
# 현재 릴리즈 바로 삭제 + 재설치 — DB 가 <old-chart-version> 의 alembic head 에 있을 때만
helm uninstall llm-gateway -n llm-gateway
# Secret/PVC 는 "helm.sh/resource-policy: keep" 때문에 보존됨
helm install llm-gateway ./deployment/charts/llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  ... --version <old-chart-version>
```

마이그레이션 Job 이 다시 돈다. DB 가 그 차트의 head 에 있으면 init SQL 은 멱등이고 `alembic upgrade head` 는
no-op 이다. DB 가 앞서 있으면 위처럼 실패한다.

### 4.4 Migration 0046 — GLOBAL rate limit 을 모델별로

0046 은 활성 GLOBAL rate limit 행의 유일성을 "전체에서 1개" 에서 "model alias 당 1개" 로 바꾼다
(USER/TEAM 은 그대로 주체당 1개). 데이터는 바꾸지 않는다 — 0045 까지 활성 GLOBAL 행은 많아야 1개였으므로
새 제약이 이미 성립한다. 이후 한 모델의 GLOBAL 저장은 다른 모델의 GLOBAL 한도를 해제하지 않는다.

함께 들어가는 지연 제약 트리거 `rl_global_release_guard` 는 **같은 모델의 대체 행 없이 활성 GLOBAL 행을
비활성화하는 커밋**을 거부한다(`check_violation`). 이 릴리스의 저장·비우기는 대체 행을 함께 넣으므로 영향이
없다. 막는 대상은 0046 DB 에 붙은 이전 admin-api 다 — 그 upsert 는 alias 와 무관하게 활성 GLOBAL 행을 전부
비활성화하므로, 모델 N 개 중 N−1 개의 한도를 감사 기록도 없이 해제했다. 의도된 해제는 그 트랜잭션에서만
`SET LOCAL rl.allow_global_release = 'on'` 으로 우회한다(세션 단위 `SET` 은 쓰지 않는다).

0046 은 `model.rate_limit_configs` 에 ACCESS EXCLUSIVE 를 처음에 잡는다(양방향). 그 대기(`lock_timeout` 5s 가
상한)와 DDL 동안 이 테이블의 읽기도 줄을 선다 — gateway-proxy 의 한도 캐시 미스와 rate-limit 화면이다. 5초 안에
못 잡으면 마이그레이션이 아무것도 바꾸지 않고 실패하고 Job 이 재시도한다.

**업그레이드 순서.** 평소의 `helm upgrade` 그대로다. 마이그레이션 Job(pre-upgrade 훅)이 init SQL →
`alembic upgrade head` 를 마친 뒤 Deployment 가 롤링된다.

- gateway-proxy 는 순서 제약이 없다. 이전 게이트웨이도 GLOBAL 한도를 요청의 alias 로 읽고 카운터를 alias 별로
  두므로(`{GLOBAL:*:<alias>}`), 활성 GLOBAL 행이 여러 개인 DB 에서도 모델마다 자기 행만 집행한다. 실 PG + 실
  Redis 증명은 gateway-proxy `tests/regression/test_high_global_rate_limit_per_model_real.py`, Redis Cluster 는
  `tests/integration/test_rate_limit_cluster.py`.
- **롤링이 끝날 때까지 GLOBAL 한도를 편집하지 않는다.** 그동안 섞이는 조합:
  - 이전 admin-api 파드의 GLOBAL 저장은 다른 모델의 활성 GLOBAL 행이 하나라도 있으면 **500** 이다 — 트리거가
    커밋을 거부해 `rate_limit_configs` 는 아무것도 바뀌지 않는다(예전에는 다른 모델의 한도를 말없이 해제했다).
    트리거는 Job 의 init SQL 이 만들므로 alembic 보다도 먼저, 즉 이 Job 이 돈 순간부터 걸린다. 다른 모델의 활성
    GLOBAL 행이 없을 때의 저장은 그대로 된다. ⚠️ 그 파드의 감사 로그에는 거부된 저장도 `SET_RATE_LIMIT` 로 남을
    수 있다 — 감사 항목은 커밋 전에 큐에 들어간다.
  - 이전 콘솔은 다른 모델이 한도를 가진 동안 "그 한도가 해제된다" 고 확인받은 뒤 `release_alias` 를 보낸다.
    새 admin-api 는 이를 **409** 로 거절하고 아무것도 바꾸지 않는다. 콘솔을 새로 고친다.
  - 이전·새 admin-api 의 GLOBAL 저장은 같은 advisory lock 키로 직렬화되므로 서로 끼어들어 유니크 위반을 내지 않는다.

**이미지만 되돌릴 때.** `helm rollback`(그리고 `helm upgrade --atomic` 의 자동 롤백)은 마이그레이션 훅을 돌리지
않는다(훅은 pre-install·pre-upgrade 뿐이다). DB 는 0046 에 남는다. 이전 gateway-proxy 는 위와 같이 모델별로
집행하므로 안전하다. 이전 admin-api 는 위 "섞이는 조합" 의 이전 파드처럼, 다른 모델의 GLOBAL 행이 있는 동안 GLOBAL
저장이 500 이다. 이전 동작으로 돌아가려면 **이미지를 되돌리기 전에** 아래 다운그레이드를 한다.

**다운그레이드 대상 = 이전 차트의 alembic head.** 0045 가 아니다. 되돌릴 차트의 마이그레이션 이미지로 확인한다.

```bash
docker run --rm <이전 차트의 마이그레이션 이미지> alembic heads   # 예: main 차트 → 0042 (head)
```

prod 의 이전 차트(main)는 head 가 0042 다. 0045 까지만 내리고 그 차트로 돌아가면 그 차트의 Job 이
`Can't locate revision identified by '0045'` 로 실패한다(exit 255). 0042 로 내리면 0043~0045(alias 별 AWS
계정) 도 함께 내려간다 — 무엇을 잃는지는 [멀티 계정 가이드 §5](../../../docs/guides/multi-account-bedrock-setup.md#5-롤백).

1. **GLOBAL 한도를 하나로 줄인다.** 다운그레이드는 실제 한도(rpm 또는 tpm)를 가진, alias 있는 활성 GLOBAL 행이
   2개 이상이면 아무것도 바꾸지 않고 실패한다 — 자동으로 하나만 남기면 나머지 모델의 Bedrock 쿼터 보호가 신호
   없이 사라지고, 어느 모델을 남길지는 운영 판단이기 때문이다. 콘솔에서 나머지 모델의 RPM·TPM 을 비우고 저장하거나
   (감사 로그가 남는다), SQL 로 한 트랜잭션에서 우회와 감사 기록을 함께 남긴다. `<운영자 user id>` 는 본인의
   `auth.users.id` 다.

   ```sql
   SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs
    WHERE scope = 'GLOBAL' AND is_active ORDER BY model_alias;
   BEGIN;
   SET LOCAL rl.allow_global_release = 'on';
   WITH released AS (
       UPDATE model.rate_limit_configs SET is_active = false, updated_at = now()
        WHERE scope = 'GLOBAL' AND is_active AND model_alias IS DISTINCT FROM '<남길 alias>'
       RETURNING id, model_alias, rpm_limit, tpm_limit
   )
   INSERT INTO audit.audit_logs
       (actor_user_id, actor_role, action, resource_type, resource_id, changes,
        result, ip_address, request_id)
   SELECT '<운영자 user id>'::uuid, 'ADMIN', 'DELETE_RATE_LIMIT', 'RateLimitConfig', id::text,
          jsonb_build_object(
              'before', jsonb_build_object(
                  'scope', 'GLOBAL', 'model_alias', model_alias,
                  'rpm', rpm_limit, 'tpm', tpm_limit),
              'after', jsonb_build_object('scope', 'GLOBAL', 'deactivated', 1),
              'reason', 'manual cleanup before alembic downgrade below 0046'),
          'SUCCESS', '0.0.0.0', 'manual-0046-downgrade'
     FROM released
   RETURNING resource_id, changes -> 'before';
   COMMIT;
   ```

   `SET LOCAL` 없이 같은 UPDATE 를 커밋하면 트리거가 거부한다. rpm·tpm 이 모두 비어 있는 행과 alias 없는 행은
   아무것도 집행하지 않으므로 다운그레이드가 비활성화하고 행마다 `DELETE_RATE_LIMIT` 감사 기록을 남긴다.

2. **다운그레이드 Job 을 한 번 돌린다.** 훅 Job 은 성공하면 지워지므로 `kubectl create job --from` 은 쓸 수 없다.
   지금 배포된(0046 이 있는) 차트 디렉터리에서, 배포된 값으로 마이그레이션 Job 을 렌더링하고 이름과 명령만 바꾼다.
   이미지·env·Secret 은 훅 Job 과 같다. `helm template` 과 `kubectl kustomize` 는 클러스터에 접속하지 않는다.

   ```bash
   NS=llm-gateway
   TARGET=0042                       # 이전 차트의 alembic head
   mkdir downgrade-job && cd downgrade-job
   helm get values llm-gateway -n "$NS" -o yaml > deployed-values.yaml
   helm template llm-gateway ../deployment/charts/llm-gateway -n "$NS" -f deployed-values.yaml \
     --show-only templates/jobs/migration.yaml > migration.yaml
   cat > kustomization.yaml <<EOF
   resources:
     - migration.yaml
   patches:
     - target:
         annotationSelector: helm.sh/hook
       patch: |-
         - op: remove
           path: /metadata/annotations
     - target:
         kind: Job
       patch: |-
         - op: replace
           path: /metadata/name
           value: llm-gateway-migration-downgrade
         - op: add
           path: /spec/template/spec/containers/0/command
           value: ["alembic", "downgrade", "$TARGET"]
   EOF
   kubectl kustomize . > downgrade-job.yaml
   grep -A3 'command:' downgrade-job.yaml        # alembic downgrade <TARGET> 인지 확인
   kubectl apply -n "$NS" -f downgrade-job.yaml
   kubectl wait -n "$NS" --for=condition=complete job/llm-gateway-migration-downgrade --timeout=10m
   kubectl logs -n "$NS" job/llm-gateway-migration-downgrade   # Job 은 끝나고 10분 뒤 사라진다
   kubectl delete -n "$NS" -f downgrade-job.yaml
   ```

   렌더링된 ServiceAccount·Role·RoleBinding 은 훅이 쓰는 것과 이름이 같다(initContainer 가 Secret 을 기다리는 데
   필요하다). 마지막 `kubectl delete` 로 함께 지운다. Job 이 실패하면(`kubectl wait` 가 시간 초과) 로그에 이유가
   있다 — 0046 의 거부라면 1 로 돌아간다.

3. **차트를 되돌린다.** `helm rollback llm-gateway <이전 리비전> -n llm-gateway`. 롤백은 Job 을 돌리지 않는다.
   다음 `helm upgrade` 의 Job 은 DB 가 그 차트의 head 에 있으므로 정상이다.

**다운그레이드한 DB 와 다음 Job.** 다운그레이드한 뒤 0046 이 있는 차트로 Job 이 다시 돌면 init SQL 이 GLOBAL
인덱스와 트리거를 다시 만들고 alembic 이 0043~0046 을 다시 적용한다 — 그래서 3 을 바로 이어서 한다.

**다운그레이드 없이 이전 차트로 `helm upgrade` 하면** 그 Job 은 실패한다. 이전 init SQL 은 같은 이름의 인덱스가
있어 no-op 이라 모델별 GLOBAL 은 그대로지만, 이어지는 `alembic upgrade head` 가
`Can't locate revision identified by '0046'` 로 끝난다. 먼저 위 다운그레이드를 한다.

---

## 5. 카나리/블루그린 (추가 구성 필요)

기본 chart는 **Rolling Update** 만 지원합니다. 카나리/블루그린을 원하면:
- **Argo Rollouts** 설치 후 `Rollout` 리소스로 전환
- 또는 **Flagger** + Istio/Linkerd 조합

이 가이드 범위 밖이며, 필요 시 별도 구성이 필요합니다.

---

## 6. 일반적 시나리오

### 6.1 긴급 패치 절차

```bash
# 1. 핫픽스 브랜치 생성
git checkout -b hotfix/critical-bug

# 2. 수정 + 빌드 + 푸시
docker build ... -t $ECR_BASE/gateway-proxy:1.0.1-hotfix ./gateway-proxy
docker push ...

# 3. dev 에서 먼저 배포 + 검증
ENV=dev helm upgrade ... --set gatewayProxy.image.tag=1.0.1-hotfix ...
./deployment/scripts/smoke-test.sh --env dev

# 4. prod 배포
ENV=prod helm upgrade ... --set gatewayProxy.image.tag=1.0.1-hotfix ...

# 5. 문제 시 즉시 롤백
helm rollback llm-gateway <prev-revision> -n llm-gateway
```

### 6.2 단계적 릴리즈 (수동 카나리)

replicas 2 → 1개씩 새 버전, 1개는 구 버전으로 유지하고 싶다면:
→ 기본 Helm 으로는 어렵습니다. 위 Argo Rollouts 사용 권장.

임시 방편: `replicaCount=4` 로 올리고 2개는 신규, 2개는 구버전. (서로 다른 Deployment 2개로 운영)

---

---

[👈 05-smoke-test.md](./05-smoke-test.md) | [07-cognito-onboarding.md 👉](./07-cognito-onboarding.md)
