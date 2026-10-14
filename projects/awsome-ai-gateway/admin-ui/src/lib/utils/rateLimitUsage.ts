// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 실시간 RPM 사용량(§60.9) 클라이언트 폴링 — RateLimitConfigPanel 에서 사용.
// gateway-proxy 가 Redis 에 적재하는 sliding-window 카운터를 /api/rate-limits/usage
// 프록시 경유로 읽는다. fail-soft: 실패 시 available:false.

export interface RateLimitUsage {
  available: boolean;
  scope?: string;
  scope_id?: string;
  window_sec: number;
  // 이 스코프에 카운터가 **존재할 수 있는가**. gateway-proxy 는 자기 rpm 한도가 양수인 스코프에만
  // ZSET 을 만든다 — 한도가 없는 주체는 요청을 아무리 보내도 키가 없다. measured=false 인데
  // rpm_used_total=0 을 "요청 없음" 으로 표시하면 거짓이다.
  measured: boolean;
  limit_rpm: number | null; // 이 스코프 **자신의** 활성 rpm 한도(상속분 아님)
  rpm_used_total: number;
  by_model: { model_alias: string; rpm_used: number }[];
  reason?: string;
}

const UNAVAILABLE: RateLimitUsage = {
  available: false,
  measured: false,
  limit_rpm: null,
  window_sec: 60,
  rpm_used_total: 0,
  by_model: [],
};

export async function fetchRateLimitUsage(scope: string, scopeId: string): Promise<RateLimitUsage> {
  const params = new URLSearchParams({ scope, scope_id: scopeId });
  try {
    const res = await fetch(`/api/rate-limits/usage?${params}`, { cache: 'no-store' });
    const data = await res.json();
    return {
      available: !!data.available,
      scope: data.scope,
      scope_id: data.scope_id,
      window_sec: data.window_sec ?? 60,
      // 필드가 없는 응답(구버전 API·프록시 오류 본문)은 false 로 떨어뜨린다 — true 로 기본값을
      // 주면 "요청 없음" 오표시가 조용히 되살아난다.
      measured: !!data.measured,
      limit_rpm: typeof data.limit_rpm === 'number' ? data.limit_rpm : null,
      rpm_used_total: data.rpm_used_total ?? 0,
      by_model: Array.isArray(data.by_model) ? data.by_model : [],
      reason: data.reason,
    };
  } catch {
    return { ...UNAVAILABLE };
  }
}
