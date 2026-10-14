// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 등록/편집 폼의 상한 = admin-api 스키마 = DB 컬럼 폭.
 *
 *   alias              VARCHAR(128)   — 콘솔은 64 로 막았다: 서버가 받는 65~128자 alias 를 콘솔이
 *                                       거부하는 막다른 길이었고, 이 스키마는 편집에도 쓰이므로 그런
 *                                       alias 는 단가만 고치는 저장조차 불가능했다.
 *   provider_model_id  VARCHAR(512)   — 콘솔 상한이 없었다(초과 시 서버 왕복 후 422).
 *   단가               NUMERIC(12,8)  — 정수부 4자리. admin-api MAX_PRICE_PER_1K 와 같은 값.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { MAX_PRICE_PER_1K, ModelCreateSchema } from '@/types/api';

const base = {
  alias: 'claude-x',
  provider: 'BEDROCK',
  model_id: 'anthropic.claude-x-v1:0',
  endpoint_url: '',
  input_price_per_1k: 0.003,
  output_price_per_1k: 0.015,
  cache_creation_5m_price_per_1k: 0,
  cache_creation_1h_price_per_1k: 0,
  cache_read_price_per_1k: 0,
};

describe('ModelCreateSchema bounds', () => {
  it('accepts an alias up to the server column width (was capped at 64)', () => {
    expect(ModelCreateSchema.safeParse({ ...base, alias: 'a'.repeat(100) }).success).toBe(true);
    expect(ModelCreateSchema.safeParse({ ...base, alias: 'a'.repeat(128) }).success).toBe(true);
    expect(ModelCreateSchema.safeParse({ ...base, alias: 'a'.repeat(129) }).success).toBe(false);
  });

  it('caps the provider model id at 512', () => {
    expect(ModelCreateSchema.safeParse({ ...base, model_id: 'm'.repeat(512) }).success).toBe(true);
    expect(ModelCreateSchema.safeParse({ ...base, model_id: 'm'.repeat(513) }).success).toBe(false);
  });

  it('caps every price at the NUMERIC(12,8) ceiling', () => {
    expect(MAX_PRICE_PER_1K).toBe(9999.99999999);
    for (const field of [
      'input_price_per_1k',
      'output_price_per_1k',
      'cache_creation_5m_price_per_1k',
      'cache_creation_1h_price_per_1k',
      'cache_read_price_per_1k',
    ] as const) {
      expect(ModelCreateSchema.safeParse({ ...base, [field]: MAX_PRICE_PER_1K }).success, field).toBe(true);
      expect(ModelCreateSchema.safeParse({ ...base, [field]: 10000 }).success, field).toBe(false);
    }
  });

  it('matches the admin-api constant (both sides must move together)', () => {
    // admin-api 는 별개 배포물이라 import 할 수 없다 — 소스의 상수 리터럴을 대조한다.
    const py = readFileSync(resolve(process.cwd(), '../admin-api/src/app/schemas/models.py'), 'utf8');
    const m = /MAX_PRICE_PER_1K\s*=\s*Decimal\("([0-9.]+)"\)/.exec(py);
    expect(m, 'admin-api 의 MAX_PRICE_PER_1K 를 찾지 못했다').not.toBeNull();
    expect(Number(m![1])).toBe(MAX_PRICE_PER_1K);
  });
});

describe('CreateModelDialog input attributes', () => {
  // 브라우저가 제출 자체를 막도록 입력 속성도 같은 상한을 갖는다.
  const src = readFileSync(resolve(process.cwd(), 'src/components/models/CreateModelDialog.tsx'), 'utf8');

  it('limits the alias and model id inputs', () => {
    expect(src).toMatch(/id="alias"[\s\S]{0,200}maxLength=\{128\}/);
    expect(src).toMatch(/id="model_id"[\s\S]{0,200}maxLength=\{512\}/);
  });

  it('puts max on every price input', () => {
    const priceInputs = src.match(/step=\{0\.00000001\}/g) ?? [];
    const withMax = src.match(/max=\{MAX_PRICE_PER_1K\}\s*step=\{0\.00000001\}/g) ?? [];
    expect(priceInputs.length).toBeGreaterThan(0);
    expect(withMax.length).toBe(priceInputs.length);
  });
});
