// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 같은 오리진으로 되돌아가는 리다이렉트 — **상대 Location** 으로만 낸다.
 *
 * 왜 `NextResponse.redirect(...)` 을 안 쓰나: 그 API 는 절대 URL 을 요구하고, 컨테이너
 * 안에서 절대 URL 을 만들 근거가 둘 다 신뢰할 수 없다.
 *
 *   * `request.url` / `request.nextUrl` — 컨테이너의 바인드 주소로 풀려 `0.0.0.0:3000`
 *     이 된다. 그 URL 로 리다이렉트하면 브라우저는 갈 곳이 없다.
 *   * `Host` 헤더 — 위보다 낫지만 CloudFront→ALB 구성에서는 **ALB 의 DNS 이름**이
 *     들어온다(CloudFront 가 viewer Host 를 전달하도록 명시 설정하지 않으면 오리진
 *     호스트를 보낸다). 그러면 로그인 직후 브라우저가 내부 ALB 호스트로 이동해
 *     CloudFront 오리진을 잃는다 — 내부 호스트에는 dev-login 이 열려 있을 수 있으므로
 *     단순한 UX 문제가 아니다.
 *
 * 상대 Location 은 이 문제 자체를 없앤다. RFC 7231 §7.1.2 는 Location 에 상대 참조를
 * 허용하며, 브라우저는 **자신이 실제로 요청한 URL** 을 기준으로 해석한다 — 즉 사용자가
 * 보고 있는 오리진(CloudFront 도메인)이 그대로 유지된다. 서버는 자기 외부 주소를
 * 알 필요가 없어진다.
 *
 * ⚠️ 외부 오리진으로 나가는 리다이렉트에는 쓸 수 없다(IdP authorize URL 등) — 그건
 *    절대 URL 이어야 한다. 그래서 이 함수는 `/` 로 시작하는 경로만 받는다.
 *
 * ⚠️ **라우트 핸들러 전용이다 — middleware.ts 에서 쓰지 말 것.** Next 14 의 미들웨어
 *    어댑터는 응답 Location 을 base 없이 `new NextURL(...)` 으로 다시 파싱해서, 상대 경로면
 *    `TypeError: Invalid URL` 로 그 요청이 500 이 된다(2026-10-14 실측 — 로그인 리다이렉트를
 *    타는 모든 페이지). 미들웨어는 절대 URL 로 리다이렉트하고, 같은 오리진이면 Next 가
 *    보내기 전에 상대 경로로 바꾼다(middleware.ts 의 redirectSameOrigin).
 */

import { NextResponse } from 'next/server';

/** 기본 303 — POST 이후에도 GET 으로 이동시킨다(로그아웃/폼 제출 경로). */
const DEFAULT_STATUS = 303;

export function redirectRelative(
  path: string,
  init?: { status?: number; headers?: Record<string, string> },
): NextResponse {
  // ⚠️ 오픈 리다이렉트 방지. `//evil.com` 은 **스킴 상대 URL** 이라 브라우저가 외부
  //    호스트로 간다 — "슬래시로 시작하니 내부" 라는 판정은 틀렸다. `/\evil.com` 도
  //    일부 브라우저가 같게 다룬다. 이 함수의 인자는 전부 코드 상수이므로 지금은
  //    도달 불가한 경로지만, 나중에 쿼리에서 온 값을 넘기는 순간 취약점이 된다.
  if (!path.startsWith('/') || path.startsWith('//') || path.startsWith('/\\')) {
    throw new Error(`redirectRelative: same-origin path expected, got ${path}`);
  }

  return new NextResponse(null, {
    status: init?.status ?? DEFAULT_STATUS,
    headers: { ...(init?.headers ?? {}), Location: path },
  });
}
