// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 대시보드 Top 팀 × 예산 소진율 조인 — 키는 팀 UUID 다, 팀 이름이 아니다.
 *
 * 예전 page.tsx 는 `new Map(budgetItems.map(i => [i.target_name, pct]))` 로 이름 조인을 했다. 팀 이름은
 * 전역 유일이 아니라(이 스키마에는 팀 이름 유일성 제약이 없다) 동명 팀 두 개가 한 Map 키로 뭉개져
 * 한 팀의 소진율이 다른 팀에 붙었고, React key(id: 이름)도 충돌했다. admin-api top-teams 가 이제
 * team_id 를 싣는다(admin-api tests/unit/test_dashboard_top_teams_team_id.py).
 */

import { describe, it, expect } from 'vitest';
import type { BudgetSummaryItem, TopTeamItem } from '@/lib/actions/dashboard';
import { joinTeamBudgetPct } from '@/lib/utils/teamBudgetJoin';

const team = (team_id: string, name: string, cost_usd: number): TopTeamItem => ({
  team_id,
  name,
  cost_usd,
  call_count: 1,
});

const budget = (target_id: string, target_name: string, usage_pct: string | null): BudgetSummaryItem => ({
  target_type: 'team',
  target_id,
  target_name,
  team_id: null,
  used_usd: '0',
  limit_usd: usage_pct == null ? null : '100',
  usage_pct,
});

describe('joinTeamBudgetPct', () => {
  it('keeps same-named teams apart', () => {
    const rows = joinTeamBudgetPct(
      [team('t-1', 'Platform', 50), team('t-2', 'Platform', 40)],
      [budget('t-1', 'Platform', '91.0'), budget('t-2', 'Platform', '12.5')],
    );
    expect(rows.map((r) => [r.id, r.usagePct])).toEqual([
      ['t-1', 91],
      ['t-2', 12.5],
    ]);
    expect(new Set(rows.map((r) => r.id)).size).toBe(2); // React key 충돌 없음
  });

  it('a team without a budget row has no usage pct (control)', () => {
    const rows = joinTeamBudgetPct([team('t-3', 'Solo', 10)], [budget('t-1', 'Platform', '91.0')]);
    expect(rows).toEqual([{ id: 't-3', name: 'Solo', usedUsd: 10, usagePct: null }]);
  });

  it('ignores user rows that happen to share the id space', () => {
    const userRow = { ...budget('t-1', 'someone', '99.0'), target_type: 'user' };
    const rows = joinTeamBudgetPct([team('t-1', 'Platform', 50)], [userRow]);
    expect(rows[0].usagePct).toBeNull();
  });
});
