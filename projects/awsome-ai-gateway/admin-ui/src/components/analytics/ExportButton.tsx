'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useRef, useEffect } from 'react';
import { Download, ChevronDown } from 'lucide-react';
import { useTranslations } from 'next-intl';
import type { AnalyticsFilterForm } from '@/types/api';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';
import { redirectToLoginIfUnauthorized } from '@/lib/utils/unauthorized';

interface ExportButtonProps {
  filter: AnalyticsFilterForm;
  /** 화면 차트와 동일하게 환산된 적용 월 (YYYY-MM). */
  latestMonth?: string;
}

type ExportFormat = 'csv' | 'json';

export function ExportButton({ filter, latestMonth }: ExportButtonProps) {
  const t = useTranslations('common');
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // 메뉴 외부 클릭 시 닫기
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  async function handleExport(format: ExportFormat) {
    setIsOpen(false);
    setIsLoading(true);

    try {
      // 내보내기 질의는 **화면 질의와 같은 빌더**로 만든다(buildAnalyticsQuery). 예전에는 여기서
      // 손으로 조립했고 두 질의가 갈렸다 — 한쪽 날짜만 고른 상태에서 start_date 하나만 보내
      // (서버 400) 내보내기가 실패했고, 서버는 scope/범위를 선언조차 안 해 범위 화면에서 월 전체
      // CSV 가 내려갔다. 파일은 "화면에 보이는 것" 이어야 하므로 질의 출처가 하나여야 한다.
      const params = new URLSearchParams(
        Object.entries(buildAnalyticsQuery(filter, latestMonth))
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)] as [string, string])
          .concat([['format', format]]),
      ).toString();

      const response = await fetch(`/api/analytics-export?${params}`);
      // ⚠️ 401 을 아래 throw 로 흘리면 console.error 만 남고 화면엔 아무 변화가 없다 —
      //    사용자는 버튼이 그냥 안 먹는다고 생각한다. 세션이 죽었으면 로그인으로 보낸다.
      //    403(권한 부족)은 여기 걸리지 않는다.
      if (redirectToLoginIfUnauthorized(response)) {
        return;
      }
      if (!response.ok) {
        throw new Error(`내보내기 실패: ${response.status}`);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      // 파일명은 서버가 붙인 Content-Disposition 을 따른다(월 또는 시작일_종료일). blob 다운로드는
      // `a.download` 가 이기므로 고정 `analytics.csv` 를 쓰면 그 기간 정보가 파일에서 사라진다.
      const disposition = response.headers.get('content-disposition') ?? '';
      a.download = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? `analytics.${format}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Export error:', err);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setIsOpen((prev) => !prev)}
        disabled={isLoading}
        aria-haspopup="true"
        aria-expanded={isOpen}
        className={[
          'inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition-colors',
          'border border-border bg-background hover:bg-muted',
          'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
          'disabled:pointer-events-none disabled:opacity-50',
        ].join(' ')}
      >
        <Download size={14} aria-hidden="true" />
        {isLoading ? t('loading') : t('export')}
        <ChevronDown
          size={14}
          aria-hidden="true"
          className={`transition-transform ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {isOpen && (
        <div
          role="menu"
          className="absolute right-0 mt-1 w-36 rounded-md border border-border bg-background shadow-md z-10"
        >
          {(['csv', 'json'] as ExportFormat[]).map((fmt) => (
            <button
              key={fmt}
              role="menuitem"
              onClick={() => handleExport(fmt)}
              className="block w-full px-4 py-2 text-left text-sm hover:bg-muted transition-colors first:rounded-t-md last:rounded-b-md"
            >
              {fmt.toUpperCase()}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}