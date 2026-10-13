import { describe, it, expect } from 'vitest';
import { formatPer1k } from '@/components/models/ModelsTable';

// 단가 표시가 저장 정밀도(소수 8자리)를 그대로 보여준다.
// 예전 toFixed(5) 는 0.0001375 를 0.00014 로 보여 줘서 화면 검수를 틀리게 만들었다.
describe('formatPer1k', () => {
  it('7자리 요율을 잘라내지 않는다', () => {
    expect(formatPer1k(0.0001375)).toBe('0.0001375');
  });
  it('8자리까지 보여준다', () => {
    expect(formatPer1k(0.00000001)).toBe('0.00000001');
  });
  it('짧은 값은 최소 4자리로 맞춘다', () => {
    expect(formatPer1k(0.003)).toBe('0.0030');
    expect(formatPer1k(0)).toBe('0.0000');
  });
  it('천 단위 구분 기호를 넣지 않는다', () => {
    expect(formatPer1k(1234.5)).toBe('1234.5000');
  });
});
