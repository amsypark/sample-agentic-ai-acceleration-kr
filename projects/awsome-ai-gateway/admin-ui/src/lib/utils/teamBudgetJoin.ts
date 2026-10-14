// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { TopSpendRow } from '@/components/dashboard/TopSpendTable';
import type { BudgetSummaryItem, TopTeamItem } from '@/lib/actions/dashboard';

/**
 * Top-teams(실비용) 행에 예산 소진율(%)을 보강한다.
 *
 * ★ 조인키는 **팀 UUID**(team_id ↔ budgets/summary target_id)다. 팀 이름은 전역 유일이 아니라,
 *   이름으로 조인하면 동명 팀 두 행이 한 Map 키로 뭉개져 한 팀의 소진율이 다른 팀에 붙는다
 *   (React key 도 이름이라 충돌했다). 예산이 없는 팀은 null.
 *
 * async 서버 컴포넌트 안의 인라인 로직이면 테스트할 수 없어 이 결함이 숨었다 — 순수 함수로 뺀다.
 */
export function joinTeamBudgetPct(
  topTeams: TopTeamItem[],
  budgetItems: BudgetSummaryItem[],
): TopSpendRow[] {
  const teamPctById = new Map<string, number | null>(
    budgetItems
      .filter((i) => i.target_type === 'team')
      .map((i) => [i.target_id, i.usage_pct != null ? parseFloat(i.usage_pct) : null]),
  );

  return topTeams.map((team) => ({
    id: team.team_id,
    name: team.name,
    usedUsd: team.cost_usd,
    usagePct: teamPctById.get(team.team_id) ?? null,
  }));
}
