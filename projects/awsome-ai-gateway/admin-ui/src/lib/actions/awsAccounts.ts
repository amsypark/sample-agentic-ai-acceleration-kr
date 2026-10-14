'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AWS 계정 레지스트리 서버 액션 (마이그레이션 0044) — admin-api `/admin/aws-accounts`.
 *
 * 계정을 한 번 등록하고 모델에서 이름으로 고른다. 계정의 역할 ARN·ExternalId 를 고치면 그 계정을
 * 고른 **모든** 모델의 호출 계정이 함께 바뀐다(DB 트리거 전파 + admin-api 가 커밋 뒤에 그 모델들의
 * 게이트웨이 캐시를 지운다).
 */

import { revalidatePath } from 'next/cache';
import { adminAPI } from '@/lib/api-client';
import { withRetry, APIError } from '@/lib/utils/retry';
import { AwsAccountSchema } from '@/types/api';
import type { AppAccount, AwsAccount } from '@/types/entities';
import type { ActionResult } from './types';

export interface AwsAccountList {
  items: AwsAccount[];
  app_accounts: AppAccount[];
}

export async function listAwsAccountsAction(): Promise<ActionResult<AwsAccountList>> {
  try {
    const res = await withRetry(() => adminAPI.get<AwsAccountList>('/admin/aws-accounts'));
    return { success: true, data: { items: res.items ?? [], app_accounts: res.app_accounts ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function createAwsAccountAction(formData: unknown): Promise<ActionResult<AwsAccount>> {
  const parsed = AwsAccountSchema.safeParse(formData);
  if (!parsed.success) {
    return { success: false, error: 'Validation failed', fieldErrors: fieldErrorsOf(parsed.error) };
  }
  const d = parsed.data;
  try {
    const res = await withRetry(() =>
      adminAPI.post<AwsAccount>('/admin/aws-accounts', {
        name: d.name,
        display_name: d.display_name,
        role_arn: d.role_arn,
        external_id: d.external_id ?? null,
        description: d.description ?? null,
      }),
    );
    revalidatePath('/models');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

/**
 * 편집. `name` 은 바꿀 수 없다(FK 대상이자 로그·콘솔에 보이는 식별자).
 *
 * 폼은 네 칸을 항상 함께 보여 주고 함께 저장하므로 값을 그대로 보낸다. external_id 의 빈 칸은
 * null(= 해제)이다 — 빈 문자열은 2자 미만이라 거절된다.
 */
export async function updateAwsAccountAction(
  name: string,
  formData: unknown,
): Promise<ActionResult<AwsAccount>> {
  const parsed = AwsAccountSchema.safeParse({ ...(formData as object), name });
  if (!parsed.success) {
    return { success: false, error: 'Validation failed', fieldErrors: fieldErrorsOf(parsed.error) };
  }
  const d = parsed.data;
  try {
    const res = await withRetry(() =>
      adminAPI.put<AwsAccount>(`/admin/aws-accounts/${encodeURIComponent(name)}`, {
        display_name: d.display_name,
        role_arn: d.role_arn,
        external_id: d.external_id ?? null,
        description: d.description ?? null,
      }),
    );
    revalidatePath('/models');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function deleteAwsAccountAction(name: string): Promise<ActionResult> {
  try {
    await withRetry(() => adminAPI.delete(`/admin/aws-accounts/${encodeURIComponent(name)}`));
    revalidatePath('/models');
    return { success: true, data: undefined };
  } catch (err) {
    // 409 = 아직 이 계정을 고른 모델이 있다 — admin-api 메시지가 그 모델들을 나열한다.
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fieldErrorsOf(error: { issues: { path: PropertyKey[]; message: string }[] }) {
  const out: Record<string, string> = {};
  for (const issue of error.issues) {
    const key = String(issue.path[0] ?? 'form');
    if (!out[key]) out[key] = issue.message;
  }
  return out;
}

function toErrorMessage(err: unknown): string {
  if (err instanceof APIError) {
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return 'An unexpected error occurred';
}
