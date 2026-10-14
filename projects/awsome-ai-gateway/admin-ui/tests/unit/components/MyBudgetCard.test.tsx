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

function data(
  budget: Partial<MyBudgetResponse['budget']>,
  team: MyBudgetResponse['team'] = { assigned: true, budget_configured: true },
): MyBudgetResponse {
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
    team,
  };
}

const UNSET = { limit_usd: null, remaining_usd: null, usage_pct: null, policy: null, used_usd: 12.34 };

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

  it('0 원 한도는 미설정이 아니라 실재하는 한도다 — 0.0% 가 아니라 "차단" 으로 그린다', () => {
    // admin-api 는 $0 한도의 사용률을 null 로 보낸다. 예전 카드는 초록 막대 "0.0%" 로 '아직 안 씀' 처럼
    // 그렸는데, HARD_BLOCK 의 $0 은 모든 요청을 막는다(gateway used >= limit).
    render(
      <MyBudgetCard
        data={data({ limit_usd: 0, usage_pct: null, remaining_usd: -1, used_usd: 1, policy: 'HARD_BLOCK' })}
      />,
    );
    expect(screen.getByText('한도: $0.00')).toBeInTheDocument();
    expect(screen.getByText('하드 차단')).toBeInTheDocument();
    expect(screen.queryByText('0.0%')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('한도가 $0 이라 게이트웨이가 요청을 차단합니다');
  });

  it('0 원 THROTTLE 은 차단이 아니라 속도 제한이다', () => {
    render(
      <MyBudgetCard
        data={data({ limit_usd: 0, usage_pct: null, remaining_usd: 0, used_usd: 0, policy: 'THROTTLE' })}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('요청 속도가 제한됩니다');
    expect(screen.getByRole('alert')).not.toHaveTextContent('차단');
  });

  it('팀 미배정 사용자에게 "팀 예산 적용" 이라고 말하지 않는다 — 게이트웨이가 거부한다', () => {
    render(<MyBudgetCard data={data(UNSET, { assigned: false, budget_configured: false })} />);
    expect(screen.queryByText('개인 예산 미설정 (팀 예산 적용)')).toBeNull();
    expect(screen.getByText('개인 예산 미설정')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('팀에 배정되지 않아 게이트웨이가 요청을 거부합니다');
  });

  it('팀에 예산이 없으면 그 사실을 알린다 — 개인 예산이 있어도', () => {
    render(<MyBudgetCard data={data({}, { assigned: true, budget_configured: false })} />);
    expect(screen.getByText('한도: $100.00')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('팀 예산이 설정되지 않아 게이트웨이가 요청을 거부합니다');
  });

  it('팀 예산이 있으면 경고하지 않는다 (대조군)', () => {
    render(<MyBudgetCard data={data(UNSET)} />);
    expect(screen.getByText('개인 예산 미설정 (팀 예산 적용)')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('MyBudgetCard — team 을 아직 보내지 않는 admin-api (롤링 배포)', () => {
  // admin-ui 가 admin-api 보다 먼저 배포되면 `/admin/my/budget` 응답에 `team` 이 없다. 예전 카드는
  // `data.team.assigned` 를 바로 읽어 TypeError 로 카드 전체가 죽었다. 팀 정보가 없으면 team 이전과
  // 같게 그린다(거부를 지어내지 않는다).
  function legacy(budget: Partial<MyBudgetResponse['budget']>): MyBudgetResponse {
    const out: Partial<MyBudgetResponse> = data(budget);
    delete out.team;
    return out as MyBudgetResponse;
  }

  it('개인 예산이 없으면 team 이전처럼 "팀 예산 적용" 으로 그린다', () => {
    render(<MyBudgetCard data={legacy(UNSET)} />);
    expect(screen.getByText('$12.34')).toBeInTheDocument();
    expect(screen.getByText('개인 예산 미설정 (팀 예산 적용)')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('개인 예산이 있으면 한도·사용률을 그대로 그린다', () => {
    render(<MyBudgetCard data={legacy({})} />);
    expect(screen.getByText('한도: $100.00')).toBeInTheDocument();
    expect(screen.getByText('25.0%')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
