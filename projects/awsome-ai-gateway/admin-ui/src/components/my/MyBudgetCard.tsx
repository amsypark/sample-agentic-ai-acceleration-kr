'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import type { MyBudgetResponse } from '@/lib/actions/my';

const POLICY_LABELS: Record<string, string> = {
  HARD_BLOCK: '하드 차단',
  SOFT_WARNING: '경고만',
  THROTTLE: '쓰로틀링',
};

function usageColor(pct: number) {
  if (pct >= 90) return 'text-red-600';
  if (pct >= 70) return 'text-yellow-600';
  return 'text-green-600';
}

function barColor(pct: number) {
  if (pct >= 90) return 'bg-red-500';
  if (pct >= 70) return 'bg-yellow-500';
  return 'bg-green-500';
}

export function MyBudgetCard({ data }: { data: MyBudgetResponse }) {
  const b = data.budget;
  // 개인 예산 행이 있는가 — 새 admin-api 는 `configured` 로 직접 알려 준다. 이전 admin-api 는 보내지 않고, 행이
  // 없으면 limit 0 · HARD_BLOCK 으로 접는다(기본 상태라 대부분의 사용자가 이 모양이다).
  // ⚠️ 그 모양에서 **$0 거부를 추론하지 않는다** — '미설정' 과 '진짜 $0 하드 차단' 을 구분할 수 없고, 게이트웨이는
  //    미설정 사용자를 통과시킨다. admin-ui 가 먼저 배포된 동안 이 카드는 기본 상태의 모든 사용자에게 빨간
  //    "$0 → 차단" 과 꽉 찬 막대를 그렸다. 미설정으로 그린다(진짜 $0 하드 차단이면 그 짧은 동안만 덜 경고한다).
  const configured = b.configured ?? !(b.limit_usd === 0 && b.policy === 'HARD_BLOCK');
  // ⚠️ 게이트웨이는 **팀 예산이 없으면 거부한다**(gateway-proxy C-1: team_budget_unset — 팀 미배정
  //    사용자도 같다). 개인 예산이 있든 없든 그렇다. 예전 카드는 개인 예산이 없으면 무조건 "팀 예산
  //    적용" 이라고 말해, 실제로는 모든 요청이 거부되는 사용자에게 쓸 수 있는 것처럼 보였다.
  //    `team` 은 optional 이다 — admin-ui 가 admin-api 보다 먼저 배포되면(롤링) 응답에 없다. 그때는 알 수
  //    없으므로 team 이전과 같게 그린다(거부를 지어내지 않는다). 예전엔 `data.team.assigned` 가 TypeError.
  const teamAssigned = data.team?.assigned ?? true;
  const teamBudgetConfigured = data.team?.budget_configured ?? true;
  // 팀 예산 행이 있어도 소진됐으면 게이트웨이가 거부한다(HARD_BLOCK used >= limit, SOFT_WARNING 110%).
  const teamBudgetExhausted = data.team?.budget_exhausted ?? false;
  const teamDenial = !teamAssigned
    ? '팀에 배정되지 않아 게이트웨이가 요청을 거부합니다(팀 예산 필요). 관리자에게 문의하세요.'
    : !teamBudgetConfigured
      ? '팀 예산이 설정되지 않아 게이트웨이가 요청을 거부합니다. 관리자에게 문의하세요.'
      : teamBudgetExhausted
        ? '팀 예산이 소진되어 게이트웨이가 요청을 거부합니다. 관리자에게 문의하세요.'
        : null;
  // ⚠️ $0 한도는 사용률이 없다(서버가 null) — 예전엔 초록 막대 "0.0%" 로 '아직 안 씀' 처럼 그렸다.
  //    게이트웨이(redis_scripts/budget_check.lua)의 $0 판정은 정책마다 다르다(실 Redis 로 확인):
  //      HARD_BLOCK   used >= 0          → 항상 차단
  //      SOFT_WARNING used*100 >= 0*110  → 항상 차단(Redis 가 죽었을 때의 DB 폴백만 limit > 0 조건으로 통과)
  //      THROTTLE     사용률을 limit > 0 일 때만 계산 → 0 으로 남아 모든 임계값(1~100) 아래 → 막지도 늦추지도 않는다
  //    예전 THROTTLE 문구 "요청 속도가 제한됩니다" 는 거짓이었다 — 거부가 아니므로 빨간 경고로도 그리지 않는다.
  const zeroLimit = configured && b.limit_usd === 0;
  const zeroLimitNote =
    zeroLimit && b.policy !== 'THROTTLE' ? '한도가 $0 이라 게이트웨이가 요청을 차단합니다.' : null;
  const zeroThrottleNote =
    zeroLimit && b.policy === 'THROTTLE'
      ? '한도가 $0 이지만 쓰로틀링 정책은 이 한도로 요청을 막거나 늦추지 않습니다.'
      : null;

  return (
    <div className="glass glass-hover rounded-apple p-6">
      <h2 className="text-base font-semibold mb-4">이번 달 예산</h2>
      <div className="space-y-3">
        <div className="flex items-end justify-between">
          <span className="text-sm text-muted-foreground">사용액</span>
          <span className="text-2xl font-bold">${b.used_usd.toFixed(2)}</span>
        </div>

        {(teamDenial || zeroLimitNote) && (
          <div role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
            {teamDenial && <p>{teamDenial}</p>}
            {zeroLimitNote && <p>{zeroLimitNote}</p>}
          </div>
        )}
        {zeroThrottleNote && <p className="text-sm text-muted-foreground">{zeroThrottleNote}</p>}

        {!configured ? (
          // 개인 예산 행이 없음(미설정). 서버가 보낸 $0 한도 / 음수 잔여 / 하드 차단(하위 호환 값) 대신 중립
          // 상태로 그린다 — 게이트웨이는 개인 예산이 없으면 그 계층을 통과시킨다(팀 예산은 계속 적용).
          <>
            <div className="w-full h-3 bg-muted rounded-full overflow-hidden" />
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>{teamDenial ? '개인 예산 미설정' : '개인 예산 미설정 (팀 예산 적용)'}</span>
              <span>--</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">잔여</span>
              <span className="font-medium">--</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">초과 정책</span>
              <span className="font-medium">미설정</span>
            </div>
          </>
        ) : (
          (() => {
            const limit = b.limit_usd;
            // $0 한도는 정의상 소진 상태다(사용률이 없다 — 서버는 하위 호환으로 0 을 보낸다) — 막대는 가득 채운다.
            const pct = zeroLimit ? 100 : Math.min(b.usage_pct, 100);
            return (
              <>
                <div className="w-full h-3 bg-muted rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all ${barColor(pct)}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>

                <div className="flex items-center justify-between text-sm text-muted-foreground">
                  <span>한도: ${limit.toFixed(2)}</span>
                  <span className={usageColor(pct)}>
                    {zeroLimit ? '--' : `${b.usage_pct.toFixed(1)}%`}
                  </span>
                </div>

                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">잔여</span>
                  <span className="font-medium">${b.remaining_usd.toFixed(2)}</span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">초과 정책</span>
                  <span className="font-medium">{POLICY_LABELS[b.policy] ?? b.policy}</span>
                </div>
              </>
            );
          })()
        )}
      </div>
    </div>
  );
}