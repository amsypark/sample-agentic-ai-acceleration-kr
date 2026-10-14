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
  // ⚠️ 게이트웨이는 **팀 예산이 없으면 거부한다**(gateway-proxy C-1: team_budget_unset — 팀 미배정
  //    사용자도 같다). 개인 예산이 있든 없든 그렇다. 예전 카드는 개인 예산이 없으면 무조건 "팀 예산
  //    적용" 이라고 말해, 실제로는 모든 요청이 거부되는 사용자에게 쓸 수 있는 것처럼 보였다.
  //    `team` 은 optional 이다 — admin-ui 가 admin-api 보다 먼저 배포되면(롤링) 응답에 없다. 그때는 알 수
  //    없으므로 team 이전과 같게 그린다(거부를 지어내지 않는다). 예전엔 `data.team.assigned` 가 TypeError.
  const teamAssigned = data.team?.assigned ?? true;
  const teamBudgetConfigured = data.team?.budget_configured ?? true;
  const teamDenial = !teamAssigned
    ? '팀에 배정되지 않아 게이트웨이가 요청을 거부합니다(팀 예산 필요). 관리자에게 문의하세요.'
    : !teamBudgetConfigured
      ? '팀 예산이 설정되지 않아 게이트웨이가 요청을 거부합니다. 관리자에게 문의하세요.'
      : null;
  // ⚠️ $0 한도는 사용률이 없다(서버가 null). HARD_BLOCK/SOFT_WARNING 은 막고(used >= 0) THROTTLE 은
  //    속도만 줄인다 — 예전엔 초록 막대 "0.0%" 로 '아직 안 씀' 처럼 그렸다.
  const zeroLimitNote =
    b.limit_usd === 0
      ? b.policy === 'THROTTLE'
        ? '한도가 $0 이라 요청 속도가 제한됩니다.'
        : '한도가 $0 이라 게이트웨이가 요청을 차단합니다.'
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

        {b.limit_usd == null ? (
          // 개인 예산 행이 없음(미설정). 지어낸 $0 한도 / 음수 잔여 / 하드 차단 대신 중립 상태로
          // 그린다 — 게이트웨이는 개인 예산이 없으면 그 계층을 통과시킨다(팀 예산은 계속 적용).
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
            // $0 한도는 정의상 소진 상태다(사용률 null) — 막대는 가득 채운다.
            const pct = limit === 0 ? 100 : Math.min(b.usage_pct ?? 0, 100);
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
                    {b.usage_pct != null ? `${b.usage_pct.toFixed(1)}%` : '--'}
                  </span>
                </div>

                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">잔여</span>
                  <span className="font-medium">
                    {b.remaining_usd != null ? `$${b.remaining_usd.toFixed(2)}` : '--'}
                  </span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">초과 정책</span>
                  <span className="font-medium">
                    {b.policy ? (POLICY_LABELS[b.policy] ?? b.policy) : '--'}
                  </span>
                </div>
              </>
            );
          })()
        )}
      </div>
    </div>
  );
}