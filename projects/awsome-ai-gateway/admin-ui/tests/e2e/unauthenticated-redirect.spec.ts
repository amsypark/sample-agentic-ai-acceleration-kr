// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 미인증 페이지 요청은 로그인 진입점으로 **리다이렉트**된다 — 500 이 아니다.
 *
 * 이 파일이 있는 이유: 다른 e2e 스펙은 전부 beforeEach 에서 dev-login 으로 쿠키를 먼저
 * 굽는다. 그래서 middleware 의 "쿠키 없음 → 리다이렉트" 분기를 한 번도 지나지 않았고,
 * 그 분기가 실서버에서 모든 페이지를 500 으로 만들던 동안(상대 Location 이 Next 미들웨어
 * 어댑터의 `new NextURL(location)` 에서 Invalid URL 로 던졌다 — middleware.ts 의
 * redirectSameOrigin 주석) 전부 통과했다. 단위 테스트도 middleware() 를 직접 불러 그
 * 어댑터 단계를 건너뛰었다. 실제 Next 런타임을 타는 것은 여기뿐이다.
 *
 * 쿠키가 없는 새 컨텍스트로 `maxRedirects: 0` 요청을 보내 첫 응답 그 자체를 본다.
 */

import { test, expect } from '@playwright/test';

test.describe('Unauthenticated page requests', () => {
  for (const path of ['/', '/budgets?q=secret']) {
    test(`${path} → 307 to /api/auth/login (not 500)`, async ({ playwright, baseURL }) => {
      const ctx = await playwright.request.newContext({ baseURL });
      try {
        const res = await ctx.get(path, { maxRedirects: 0 });
        expect(res.status()).toBe(307);
        // 브라우저가 받는 Location 은 상대 경로다 — 같은 오리진 리다이렉트를 Next 가
        // 상대로 바꿔 보낸다. 내부 호스트(0.0.0.0, ALB 이름)도, 원래 쿼리도 싣지 않는다.
        expect(res.headers()['location']).toBe('/api/auth/login');
      } finally {
        await ctx.dispose();
      }
    });
  }

  test('/api/auth/login itself is reachable without a cookie (no redirect loop)', async ({
    playwright,
    baseURL,
  }) => {
    const ctx = await playwright.request.newContext({ baseURL });
    try {
      const res = await ctx.get('/api/auth/login', { maxRedirects: 0 });
      // dev 폼(307 → dev-login), OIDC authorize(307 → IdP), 미설정(503 + 본문) 중 하나.
      expect([307, 503]).toContain(res.status());
      expect(res.headers()['location'] ?? '').not.toContain('/api/auth/login');
    } finally {
      await ctx.dispose();
    }
  });
});
