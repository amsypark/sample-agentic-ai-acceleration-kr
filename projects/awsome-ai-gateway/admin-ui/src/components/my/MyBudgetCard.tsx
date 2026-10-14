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

  return (
    <div className="glass glass-hover rounded-apple p-6">
      <h2 className="text-base font-semibold mb-4">이번 달 예산</h2>
      <div className="space-y-3">
        <div className="flex items-end justify-between">
          <span className="text-sm text-muted-foreground">사용액</span>
          <span className="text-2xl font-bold">${b.used_usd.toFixed(2)}</span>
        </div>

        {b.limit_usd == null || b.usage_pct == null ? (
          // 개인 예산 행이 없음(미설정). 지어낸 $0 한도 / 음수 잔여 / 하드 차단 대신 중립 상태로
          // 그린다 — 게이트웨이는 개인 예산이 없으면 통과시키고 팀 예산만 적용한다.
          <>
            <div className="w-full h-3 bg-muted rounded-full overflow-hidden" />
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>개인 예산 미설정 (팀 예산 적용)</span>
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
            const pct = Math.min(b.usage_pct, 100);
            return (
              <>
                <div className="w-full h-3 bg-muted rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all ${barColor(pct)}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>

                <div className="flex items-center justify-between text-sm text-muted-foreground">
                  <span>한도: ${b.limit_usd.toFixed(2)}</span>
                  <span className={usageColor(pct)}>{b.usage_pct.toFixed(1)}%</span>
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