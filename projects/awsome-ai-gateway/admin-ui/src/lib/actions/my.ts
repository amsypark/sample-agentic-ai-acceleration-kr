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
    usage_pct: number | null;
    policy: string | null;
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