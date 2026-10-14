// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * MyBudgetCard — 개인 예산 행이 없는 사용자에게 '$0 한도 / 음수 잔여 / 하드 차단' 을 그리지 않는다.
 *
 * admin-api `/admin/my/budget` 은 이제 행이 없으면 limit/remaining/usage_pct/policy 를 null 로
 * 보낸다(예전: 0 / 음수 / "HARD_BLOCK"). 게이트웨이는 그 사용자를 통과시키므로(개인 예산 미설정 →
 * 팀 예산만 적용) 화면만 실재하지 않는 강제를 보였다. 예전 카드는 null 에 `.toFixed()` 를 불러
 * 렌더 자체가 죽는다 — 그래서 3-state 를 같이 고정한다.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import type { MyBudgetResponse } from '@/lib/actions/my';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';

function data(budget: Partial<MyBudgetResponse['budget']>): MyBudgetResponse {
  return {
    user_id: 'u1',
    period: '2026-09',
    budget: {
      limit_usd: 100,
      used_usd: 25,
      remaining_usd: 75,
      usage_pct: 25,
      policy: 'SOFT_WARNING',
      ...budget,
    },
  };
}

describe('MyBudgetCard', () => {
  it('예산 행이 없으면 미설정으로 그린다 — $0 한도·음수 잔여·하드 차단을 지어내지 않는다', () => {
    render(
      <MyBudgetCard
        data={data({ limit_usd: null, remaining_usd: null, usage_pct: null, policy: null, used_usd: 12.34 })}
      />,
    );

    expect(screen.getByText('$12.34')).toBeInTheDocument(); // 실지출은 그대로
    expect(screen.getByText('개인 예산 미설정 (팀 예산 적용)')).toBeInTheDocument();
    expect(screen.queryByText(/한도: \$0\.00/)).toBeNull();
    expect(screen.queryByText('하드 차단')).toBeNull();
    expect(screen.queryByText(/-\$|\$-/)).toBeNull();
  });

  it('예산이 있으면 한도·사용률·정책을 그린다 (대조군)', () => {
    render(<MyBudgetCard data={data({})} />);
    expect(screen.getByText('한도: $100.00')).toBeInTheDocument();
    expect(screen.getByText('25.0%')).toBeInTheDocument();
    expect(screen.getByText('$75.00')).toBeInTheDocument();
    expect(screen.getByText('경고만')).toBeInTheDocument();
    expect(screen.queryByText('개인 예산 미설정 (팀 예산 적용)')).toBeNull();
  });

  it('0 원 한도는 미설정이 아니라 실재하는 한도다', () => {
    render(
      <MyBudgetCard
        data={data({ limit_usd: 0, usage_pct: 0, remaining_usd: -1, used_usd: 1, policy: 'HARD_BLOCK' })}
      />,
    );
    expect(screen.getByText('한도: $0.00')).toBeInTheDocument();
    expect(screen.getByText('하드 차단')).toBeInTheDocument();
  });
});
