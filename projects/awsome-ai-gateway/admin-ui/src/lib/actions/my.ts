'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface MyBudgetResponse {
  user_id: string;
  period: string;
  budget: {
    // ⚠️ 이 다섯 값은 언제나 숫자·문자열이다(하위 호환). 개인 예산 행이 없으면(OIDC 자동 프로비저닝 사용자의
    //    기본 상태) admin-api 는 예전과 같이 limit 0 · "HARD_BLOCK" · usage_pct 0 · remaining = -used 를 보낸다 —
    //    이전 카드가 그대로 `.toFixed()` 하기 때문이다. 그 값으로 '$0 하드 차단' 을 추론하면 안 된다(게이트웨이는
    //    그 사용자를 통과시킨다). 미설정 여부는 configured 로 본다. $0 한도의 usage_pct 도 0 이다.
    limit_usd: number;
    used_usd: number;
    remaining_usd: number;
    usage_pct: number;
    policy: string;
    // 개인 예산 행이 있는가. 이 필드 이전의 admin-api 는 보내지 않는다 — 그때 0 · HARD_BLOCK 은 '미설정' 과
    // '진짜 $0 하드 차단' 을 구분할 수 없다.
    configured?: boolean;
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