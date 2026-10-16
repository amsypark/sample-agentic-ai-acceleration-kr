'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface MonitoringOverviewResponse {
  timestamp: string;
  active_models: number;
  last_1h: {
    total_requests: number;
    error_count: number;
    error_rate_pct: number;
    avg_latency_ms: number;
    p95_latency_ms: number;
    total_cost_usd: number;
  };
}

/**
 * `GET /admin/monitoring/models` 의 상태 열. `string` 이 아니라 닫힌 유니온이어야 한다 — 서버가 새
 * 값을 추가하면 타입 검사에서 걸려 배지 톤 표를 함께 늘리게 된다. `string` 이던 동안 이 열은 서버가
 * 리터럴 `"ACTIVE"` 를 박아 보내는 것을 아무도 눈치채지 못한 자리였다(항상 초록 = 정보량 0).
 *
 *  - ACTIVE / INACTIVE : `model.model_aliases.status` 의 실제 값
 *  - UNREGISTERED      : 등록부에 없는 alias 인데 최근 1시간 트래픽에 있음
 *                        (usage_logs.model_alias 는 FK 없는 free-text 컬럼)
 */
export type MonitoringModelStatus = 'ACTIVE' | 'INACTIVE' | 'UNREGISTERED';

export interface MonitoringModelItem {
  alias: string;
  status: MonitoringModelStatus;
  last_1h_requests: number;
  avg_latency_ms: number;
  error_rate_pct: number;
  last_request_at: string | null;
}

export interface MonitoringModelsResponse {
  models: MonitoringModelItem[];
}

export type MonitoringEventTypeFilter =
  | 'all'
  | 'success'
  | 'error'
  | 'timeout'
  | 'slow'
  | 'abnormal';

export interface MonitoringEvent {
  timestamp: string;
  user_id: string;
  model_alias: string;
  downgraded_from?: string | null;
  event_type: string;
  ttft_ms?: number | null;
  latency_ms?: number;
  detail: string;
}

export interface MonitoringEventsResponse {
  events: MonitoringEvent[];
}

export interface MonitoringUserItem {
  user_id: string;
  email: string;
  display_name: string;
  requests: number;
  tokens: number;
  cost_usd: number;
  error_rate_pct: number;
  last_request_at: string | null;
}

export interface MonitoringUsersResponse {
  users: MonitoringUserItem[];
}

export async function fetchMonitoringOverview(): Promise<MonitoringOverviewResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringOverviewResponse>('/admin/monitoring/overview')
  );
}

export async function fetchMonitoringModels(): Promise<MonitoringModelsResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringModelsResponse>('/admin/monitoring/models')
  );
}

export async function fetchMonitoringEvents(
  limit = 50,
  eventType: MonitoringEventTypeFilter = 'all',
): Promise<MonitoringEventsResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringEventsResponse>('/admin/monitoring/events', {
      limit,
      event_type: eventType,
    })
  );
}

export async function fetchMonitoringUsers(limit = 10): Promise<MonitoringUsersResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringUsersResponse>('/admin/monitoring/users', { limit })
  );
}