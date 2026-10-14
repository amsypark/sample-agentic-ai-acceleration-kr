'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface MyBudgetResponse {
  user_id: string;
  period: string;
  budget: {
    // 개인 예산 행이 없으면(OIDC 자동 프로비저닝 사용자의 기본 상태) 서버가 null 을 보낸다.
    // 예전엔 0 / "HARD_BLOCK" 으로 접혀 "한도 $0 / 잔여 $-N / 하드 차단" 이라는 실재하지 않는
    // 강제를 렌더했다(게이트웨이는 그 사용자를 통과시킨다). used_usd(실지출)만 항상 숫자다.
    limit_usd: number | null;
    used_usd: number;
    remaining_usd: number | null;
    // $0 한도면 null 이다(0 으로 나눈 비율은 없다 — 0.0% 는 '아직 안 씀' 으로 읽힌다).
    usage_pct: number | null;
    policy: string | null;
  };
  // 게이트웨이는 팀 예산이 없으면(팀 미배정 포함) 개인 예산과 무관하게 거부한다(team_budget_unset).
  // optional — 이 필드를 보내기 전의 admin-api 와 함께 도는 롤링 배포 동안에는 없다.
  team?: {
    assigned: boolean;
    budget_configured: boolean;
    // 팀 예산 행이 있어도 게이트웨이가 거부하는 상태(HARD_BLOCK 소진 · SOFT_WARNING 110% · $0). THROTTLE 은
    // 거부하지 않으므로 항상 false. optional — 이 필드 이전의 admin-api 는 보내지 않는다.
    budget_exhausted?: boolean;
  };
}

export interface DailyUsage {
  date: string;
  cost_usd: number;
  requests: number;
  tokens: number;
}

export interface ModelUsage {
  model_alias: string;
  cost_usd: number;
  requests: number;
  tokens: number;
}

export interface MyUsageResponse {
  user_id: string;
  period: string;
  daily_usage: DailyUsage[];
  by_model: ModelUsage[];
}

export async function fetchMyBudget(): Promise<MyBudgetResponse> {
  return withRetry(() => adminAPI.get<MyBudgetResponse>('/admin/my/budget'));
}

export async function fetchMyUsage(period?: string): Promise<MyUsageResponse> {
  return withRetry(() =>
    adminAPI.get<MyUsageResponse>('/admin/my/usage', period ? { period } : undefined)
  );
}