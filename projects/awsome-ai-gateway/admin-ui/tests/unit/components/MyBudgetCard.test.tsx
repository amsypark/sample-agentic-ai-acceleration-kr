// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * MyBudgetCard — 배포 순서가 섞여도 카드는 게이트웨이가 실제로 하는 일을 말한다.
 *
 * 응답은 손으로 쓰지 않는다. tests/fixtures/my-budget.json 의 **실제 응답**을 쓴다:
 *   * `main` — main(e7a1588)의 admin-api 가 실제로 보내던 응답. 그 라우트 코드로 만들고 실 PostgreSQL 행으로
 *     대조했다. "새 카드 × 이전 API" = admin-ui 가 먼저 배포됐거나 helm 롤 중 두 버전이 섞인 동안이다.
 *   * `head` — 지금 admin-api 의 응답(admin-api tests/unit/test_my_budget_wire_fixture.py 가 완전 일치를 단정).
 *
 * 이전 API 는 개인 예산이 없으면 limit 0 · HARD_BLOCK · usage_pct 0 을 보낸다(기본 상태). 그 모양에서 '$0 → 차단'
 * 을 추론하면 거의 모든 사용자에게 빨간 경고와 꽉 찬 막대를 그린다 — 게이트웨이는 그 사용자를 통과시킨다.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import type { MyBudgetResponse } from '@/lib/actions/my';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import fixture from '../../fixtures/my-budget.json';

const main = fixture.main as unknown as Record<string, MyBudgetResponse>;
const head = fixture.head as unknown as Record<string, MyBudgetResponse>;

function renderCard(data: MyBudgetResponse) {
  return render(<MyBudgetCard data={data} />);
}

/** 개인 예산 미설정 화면 — 지어낸 $0 한도·음수 잔여·하드 차단·빨간 막대가 없다. */
function expectUnset(container: HTMLElement, label: string) {
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.queryByText(/한도: \$/)).toBeNull();
  expect(screen.queryByText('하드 차단')).toBeNull();
  expect(screen.queryByText(/-\$|\$-/)).toBeNull();
  expect(container.querySelector('.bg-red-500')).toBeNull();
}

describe('새 카드 × 이전 admin-api (main 의 실제 응답)', () => {
  it('main 의 응답에는 configured 도 team 도 없다 (전제)', () => {
    expect(main.no_budget.budget.configured).toBeUndefined();
    expect(main.no_budget.team).toBeUndefined();
    expect(main.no_budget.budget).toMatchObject({ limit_usd: 0, policy: 'HARD_BLOCK', usage_pct: 0 });
  });

  it('개인 예산이 없는 기본 상태를 $0 차단으로 그리지 않는다', () => {
    const { container } = renderCard(main.no_budget);
    expect(screen.getByText('$12.34')).toBeInTheDocument(); // 실지출은 그대로
    expect(screen.queryByRole('alert')).toBeNull();
    expectUnset(container, '개인 예산 미설정 (팀 예산 적용)');
  });

  it('$0 하드 차단은 이전 응답에서 미설정과 구분되지 않는다 — 거부를 추론하지 않는다', () => {
    // main 의 응답은 두 상태가 같은 모양이다(값만 다르다). 거의 모두가 미설정이고 게이트웨이는 그들을 통과시킨다.
    const { container } = renderCard(main.zero_hard_block);
    expect(screen.queryByRole('alert')).toBeNull();
    expectUnset(container, '개인 예산 미설정 (팀 예산 적용)');
  });

  it('$0 이라도 정책이 HARD_BLOCK 이 아니면 실재하는 한도다 — 이전 응답으로도 판정한다', () => {
    renderCard(main.zero_soft_warning);
    expect(screen.getByRole('alert')).toHaveTextContent('한도가 $0 이라 게이트웨이가 요청을 차단합니다');
  });

  it('$0 쓰로틀링은 막지도 늦추지도 않는다', () => {
    renderCard(main.zero_throttle);
    expect(screen.getByText(/막거나 늦추지 않습니다/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('일반 예산과 초과 사용은 그대로 그린다 (대조군)', () => {
    const { unmount } = renderCard(main.normal);
    expect(screen.getByText('한도: $100.00')).toBeInTheDocument();
    expect(screen.getByText('25.0%')).toBeInTheDocument();
    expect(screen.getByText('$75.00')).toBeInTheDocument();
    expect(screen.getByText('경고만')).toBeInTheDocument();
    unmount();

    renderCard(main.over_limit);
    expect(screen.getByText('한도: $50.00')).toBeInTheDocument();
    expect(screen.getByText('120.0%')).toBeInTheDocument();
    expect(screen.getByText('$-10.00')).toBeInTheDocument();
    expect(screen.getByText('하드 차단')).toBeInTheDocument();
  });
});

describe('새 카드 × 새 admin-api (head 의 실제 응답)', () => {
  it('개인 예산이 없으면 미설정이다 — configured 로 안다', () => {
    const { container } = renderCard(head.no_budget);
    expect(head.no_budget.budget.configured).toBe(false);
    expect(screen.queryByRole('alert')).toBeNull();
    expectUnset(container, '개인 예산 미설정 (팀 예산 적용)');
  });

  it('$0 하드 차단은 차단이다 — 0.0% 가 아니라 "--" 와 꽉 찬 막대', () => {
    const { container } = renderCard(head.zero_hard_block);
    expect(screen.getByRole('alert')).toHaveTextContent('한도가 $0 이라 게이트웨이가 요청을 차단합니다');
    expect(screen.getByText('한도: $0.00')).toBeInTheDocument();
    expect(screen.getByText('하드 차단')).toBeInTheDocument();
    expect(screen.getByText('--')).toBeInTheDocument();
    expect(screen.queryByText('0.0%')).toBeNull();
    expect(container.querySelector('.bg-red-500')).not.toBeNull();
  });

  it('$0 SOFT_WARNING 도 차단이다 (Lua: used*100 >= 0*110)', () => {
    renderCard(head.zero_soft_warning);
    expect(screen.getByRole('alert')).toHaveTextContent('한도가 $0 이라 게이트웨이가 요청을 차단합니다');
  });

  it('$0 쓰로틀링은 빨간 경고가 아니다', () => {
    renderCard(head.zero_throttle);
    expect(screen.getByText(/막거나 늦추지 않습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/속도가 제한/)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('일반 예산은 한도·사용률·잔여·정책을 그린다 (대조군)', () => {
    renderCard(head.normal);
    expect(screen.getByText('한도: $100.00')).toBeInTheDocument();
    expect(screen.getByText('25.0%')).toBeInTheDocument();
    expect(screen.getByText('$75.00')).toBeInTheDocument();
    expect(screen.getByText('경고만')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('팀 미배정이면 거부된다고 말한다 — "팀 예산 적용" 이 아니다', () => {
    renderCard(head.teamless);
    expect(screen.getByRole('alert')).toHaveTextContent('팀에 배정되지 않아 게이트웨이가 요청을 거부합니다');
    expect(screen.getByText('개인 예산 미설정')).toBeInTheDocument();
    expect(screen.queryByText('개인 예산 미설정 (팀 예산 적용)')).toBeNull();
  });

  it('팀 예산이 없거나 소진됐으면 거부된다고 말한다', () => {
    const { unmount } = renderCard(head.team_without_budget);
    expect(screen.getByRole('alert')).toHaveTextContent('팀 예산이 설정되지 않아 게이트웨이가 요청을 거부합니다');
    unmount();

    renderCard(head.team_exhausted);
    expect(screen.getByRole('alert')).toHaveTextContent('팀 예산이 소진되어 게이트웨이가 요청을 거부합니다');
    expect(screen.queryByText('개인 예산 미설정 (팀 예산 적용)')).toBeNull();
  });
});
