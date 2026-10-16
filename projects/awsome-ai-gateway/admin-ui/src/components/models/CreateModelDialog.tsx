'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { X } from 'lucide-react';
import type { AwsAccount, ModelListItem } from '@/types/entities';
import { createModelAction, updateModelAction } from '@/lib/actions/models';
// 단가 칸의 `max` — 브라우저가 제출 자체를 막는다(초과 값은 admin-api 에서도 422).
import { MAX_PRICE_PER_1K } from '@/types/api';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface CreateModelDialogProps {
  isOpen: boolean;
  onClose: () => void;
  editModel?: ModelListItem;
  /** 등록된 AWS 계정(0044) — 계정 선택 칸의 선택지. */
  accounts?: AwsAccount[];
  /** 모델 목록 — 같은 모델 id 의 다른 alias(형제)를 찾는다(아래 siblings). */
  models?: ModelListItem[];
}

interface FormState {
  alias: string;
  provider: string;
  model_id: string;
  endpoint_url: string;
  input_price_per_1k: string;
  output_price_per_1k: string;
  cache_creation_5m_price_per_1k: string;
  cache_creation_1h_price_per_1k: string;
  cache_read_price_per_1k: string;
  description: string;
  display_name: string;
  /** 레지스트리 이름. '' = 기본값. */
  account_name: string;
}

function getInitialState(editModel?: ModelListItem): FormState {
  if (editModel) {
    return {
      alias: editModel.alias,
      provider: editModel.provider,
      model_id: editModel.model_id,
      endpoint_url: editModel.endpoint_url ?? '',
      input_price_per_1k: editModel.input_price_per_1k.toString(),
      output_price_per_1k: editModel.output_price_per_1k.toString(),
      cache_creation_5m_price_per_1k: editModel.cache_creation_5m_price_per_1k.toString(),
      cache_creation_1h_price_per_1k: editModel.cache_creation_1h_price_per_1k.toString(),
      cache_read_price_per_1k: editModel.cache_read_price_per_1k.toString(),
      description: editModel.description ?? '',
      display_name: editModel.display_name ?? '',
      // 현재 선택으로 prefill — 아니면 계정 칸을 건드리지 않은 저장이 선택을 지운다.
      account_name: editModel.account_name ?? '',
    };
  }
  return {
    alias: '',
    provider: '',
    model_id: '',
    endpoint_url: '',
    input_price_per_1k: '',
    output_price_per_1k: '',
    cache_creation_5m_price_per_1k: '0',
    cache_creation_1h_price_per_1k: '0',
    cache_read_price_per_1k: '0',
    description: '',
    display_name: '',
    account_name: '',
  };
}

export function CreateModelDialog({
  isOpen,
  onClose,
  editModel,
  accounts = [],
  models = [],
}: CreateModelDialogProps) {
  const t = useTranslations('models');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [form, setForm] = useState<FormState>(() => getInitialState(editModel));
  const [applyToSiblings, setApplyToSiblings] = useState(false);

  const isEditMode = !!editModel;
  // OPENMODEL 은 AWS 가 서빙하지 않는다 — 계정을 고를 수 없다(admin-api 도 400 으로 거부한다).
  const isOpenModel = form.provider.toUpperCase() === 'OPENMODEL';
  // 선택지에 현재 값이 없으면(목록 조회 실패 등) 그 값을 그대로 한 줄 넣는다 — 안 그러면 select 가
  // 첫 선택지(기본값)를 보여 주고, 저장하면 운영자가 고르지 않은 해제가 일어난다.
  const accountOptions =
    form.account_name && !accounts.some((a) => a.name === form.account_name)
      ? [...accounts, { name: form.account_name, display_name: form.account_name, account_id: '' } as AwsAccount]
      : accounts;
  // 같은 모델 id 의 다른 ACTIVE alias 중 다른 계정을 부르는 것. 게이트웨이는 요청의 모델 이름을
  // alias 정확 매칭으로 먼저 찾으므로(시드는 모델 id 와 이름이 같은 alias 를 둔다), 여기서 고른 계정은
  // 모델 id 나 그 alias 로 부른 요청에는 쓰이지 않는다. admin-api 는 그런 저장을 409 로 막고
  // apply_to_siblings 로 함께 옮기게 한다(services/model_service._align_account_siblings).
  const selfAlias = editModel?.alias ?? form.alias;
  const siblings =
    isOpenModel || !form.model_id
      ? []
      : models.filter(
          (m) =>
            m.alias !== selfAlias &&
            m.model_id === form.model_id &&
            m.is_active &&
            m.provider.toUpperCase() !== 'OPENMODEL' &&
            (m.account_name ?? '') !== form.account_name,
        );

  useEffect(() => {
    if (editModel) {
      setForm(getInitialState(editModel));
      setApplyToSiblings(false);
    }
  }, [editModel]);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>
  ) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    if (fieldErrors[name]) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});

    const payload = {
      alias: form.alias,
      provider: form.provider,
      model_id: form.model_id,
      endpoint_url: form.endpoint_url,
      input_price_per_1k: parseFloat(form.input_price_per_1k),
      output_price_per_1k: parseFloat(form.output_price_per_1k),
      cache_creation_5m_price_per_1k: parseFloat(form.cache_creation_5m_price_per_1k || '0'),
      cache_creation_1h_price_per_1k: parseFloat(form.cache_creation_1h_price_per_1k || '0'),
      cache_read_price_per_1k: parseFloat(form.cache_read_price_per_1k || '0'),
      description: form.description || undefined,
      display_name: form.display_name || undefined,
      // '' = 기본값 → null. OPENMODEL 이면 언제나 기본값이다.
      account_name: isOpenModel ? null : form.account_name || null,
      // 운영자가 고른 경우에만 보낸다 — 구 admin-api 는 모르는 키를 422 로 거부한다.
      ...(siblings.length > 0 && applyToSiblings ? { apply_to_siblings: true } : {}),
    };

    startTransition(async () => {
      const result = isEditMode
        ? await updateModelAction(editModel.alias, payload)
        : await createModelAction(payload);

      if (result.success) {
        toast({
          type: 'success',
          message: isEditMode
            ? t('editSuccess', { alias: form.alias })
            : t('createSuccess', { alias: form.alias }),
          auto_dismiss_ms: 3000,
        });
        onClose();
      } else {
        // 필드 에러 중 이 폼에 대응 입력란이 없는 키는 아래 JSX 로 렌더될 수 없어 그냥 사라진다.
        // 실제로 그래서 max_tokens/context_window 스키마 드리프트가 원인 없는 "Validation failed"
        // 로만 보였다. 렌더 불가한 키는 상단 에러 메시지에 합쳐 항상 화면에 노출한다.
        const fe = (!result.success && result.fieldErrors) || {};
        const orphanKeys = Object.keys(fe).filter((k) => !(k in form));
        setError(
          orphanKeys.length
            ? `${result.error}: ${orphanKeys.map((k) => `${k} (${fe[k]})`).join(', ')}`
            : result.error
        );
        setFieldErrors(fe);
      }
    });
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-background rounded-lg p-6 w-full max-w-lg shadow-xl border border-border max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">
            {isEditMode ? t('editModel') : t('createModel')}
          </h2>
          <button
            onClick={onClose}
            className="rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-opacity"
            aria-label={tCommon('close')}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Alias */}
          <div className="space-y-1">
            <label htmlFor="alias" className="text-sm font-medium">
              Alias <span className="text-destructive">*</span>
            </label>
            <input
              id="alias"
              name="alias"
              type="text"
              // 128 = `model_aliases.alias VARCHAR(128)` (= admin-api max_length).
              maxLength={128}
              value={form.alias}
              onChange={handleChange}
              required
              disabled={isEditMode}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
              placeholder="e.g. claude-3-5-sonnet"
            />
            {isEditMode && (
              <p className="text-xs text-muted-foreground">
                {t('aliasReadonly')}
              </p>
            )}
            {fieldErrors.alias && <FormError error={fieldErrors.alias} />}
          </div>

          {/* Provider — ⚠️ 편집 모드에서는 alias 와 마찬가지로 읽기 전용이다.
              PUT /admin/models/{alias} 의 ModelUpdateRequest 에는 provider/api_format 이
              없고(admin-api/src/app/schemas/models.py) update 서비스·리포지토리에도 변경
              경로가 없어 admin API 로는 바꿀 수 없는 불변 필드다. 예전엔 여기서 드롭다운을
              바꿀 수 있었고 updateModelAction 은 provider 를 아예 보내지도 않으므로
              (lib/actions/models.ts:95~) 성공 토스트만 뜨고 값은 반영되지 않았다 —
              화면과 DB 가 갈라지는 조용한 실패. 지금은 스키마가 422 로 거부하기도 하지만,
              애초에 저장 못 하는 컨트롤을 열어 두지 않는다. */}
          <div className="space-y-1">
            <label htmlFor="provider" className="text-sm font-medium">
              Provider <span className="text-destructive">*</span>
            </label>
            <select
              id="provider"
              name="provider"
              value={form.provider}
              onChange={handleChange}
              required
              disabled={isEditMode}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
            >
              <option value="">{t('selectProvider')}</option>
              <option value="BEDROCK">BEDROCK</option>
              <option value="OPENMODEL">OPENMODEL</option>
              {/* Mantle 계열은 endpoint_url·api_format 이 필요해 보통 마이그레이션으로 시드되지만,
                  기존 cowork-opus / codex-gpt 모델 편집(단가 등) 시 provider 드롭다운이 값과
                  매칭되도록 옵션을 노출한다. */}
              <option value="BEDROCK_MANTLE">BEDROCK_MANTLE (Cowork · Opus)</option>
              <option value="BEDROCK_MANTLE_OPENAI">BEDROCK_MANTLE_OPENAI (Codex · GPT)</option>
              {/* 표준 bedrock-runtime plane (SigV4 + CRIS). Mantle 과 달리 endpoint_url 이
                  **필수**다 — 어댑터가 endpoint 호스트에서 서명 리전을 뽑아내므로
                  (bedrock-runtime.{region}.amazonaws.com) 비워 두면 서명할 수 없다.
                  provider_model_id 는 us./global. 접두사가 붙은 추론 프로파일 ID 여야 한다. */}
              <option value="BEDROCK_RUNTIME_OPENAI">BEDROCK_RUNTIME_OPENAI (GPT-5.6 · CRIS)</option>
            </select>
            {isEditMode && (
              <p className="text-xs text-muted-foreground">
                {t('providerReadonly')}
              </p>
            )}
            {fieldErrors.provider && <FormError error={fieldErrors.provider} />}
          </div>

          {/* Model ID */}
          <div className="space-y-1">
            <label htmlFor="model_id" className="text-sm font-medium">
              Model ID <span className="text-destructive">*</span>
            </label>
            <input
              id="model_id"
              name="model_id"
              type="text"
              // 512 = `model_aliases.provider_model_id VARCHAR(512)`.
              maxLength={512}
              value={form.model_id}
              onChange={handleChange}
              required
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder="e.g. anthropic.claude-3-5-sonnet-20241022-v2:0"
            />
            {fieldErrors.model_id && <FormError error={fieldErrors.model_id} />}
          </div>

          {/* Endpoint URL — OPENMODEL(vLLM) 등 커스텀 엔드포인트 모델에서만 의미있음 */}
          <div className="space-y-1">
            <label htmlFor="endpoint_url" className="text-sm font-medium">
              Endpoint URL <span className="text-muted-foreground text-xs">(선택)</span>
            </label>
            <input
              id="endpoint_url"
              name="endpoint_url"
              type="text"
              value={form.endpoint_url}
              onChange={handleChange}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder="예: http://vllm.internal:8000/v1 (Bedrock/Mantle은 비워둠)"
            />
            {fieldErrors.endpoint_url && <FormError error={fieldErrors.endpoint_url} />}
          </div>

          {/* Price fields — 수직 일렬 배치 */}
          <div className="space-y-3">
            <span className="text-sm font-medium">{t('priceLabel')}</span>

            <div className="space-y-1">
              <label htmlFor="input_price_per_1k" className="text-xs text-muted-foreground">{t('priceInput')}</label>
              <input
                id="input_price_per_1k"
                name="input_price_per_1k"
                type="number"
                min={0}
                max={MAX_PRICE_PER_1K}
                step={0.00000001}
                value={form.input_price_per_1k}
                onChange={handleChange}
                required
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.input_price_per_1k && <FormError error={fieldErrors.input_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="output_price_per_1k" className="text-xs text-muted-foreground">{t('priceOutput')}</label>
              <input
                id="output_price_per_1k"
                name="output_price_per_1k"
                type="number"
                min={0}
                max={MAX_PRICE_PER_1K}
                step={0.00000001}
                value={form.output_price_per_1k}
                onChange={handleChange}
                required
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.output_price_per_1k && <FormError error={fieldErrors.output_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_creation_5m_price_per_1k" className="text-xs text-muted-foreground">{t('priceCacheCreate5m')}</label>
              <input
                id="cache_creation_5m_price_per_1k"
                name="cache_creation_5m_price_per_1k"
                type="number"
                min={0}
                max={MAX_PRICE_PER_1K}
                step={0.00000001}
                value={form.cache_creation_5m_price_per_1k}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.cache_creation_5m_price_per_1k && <FormError error={fieldErrors.cache_creation_5m_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_creation_1h_price_per_1k" className="text-xs text-muted-foreground">{t('priceCacheCreate1h')}</label>
              <input
                id="cache_creation_1h_price_per_1k"
                name="cache_creation_1h_price_per_1k"
                type="number"
                min={0}
                max={MAX_PRICE_PER_1K}
                step={0.00000001}
                value={form.cache_creation_1h_price_per_1k}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.cache_creation_1h_price_per_1k && <FormError error={fieldErrors.cache_creation_1h_price_per_1k} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_read_price_per_1k" className="text-xs text-muted-foreground">{t('priceCacheRead')}</label>
              <input
                id="cache_read_price_per_1k"
                name="cache_read_price_per_1k"
                type="number"
                min={0}
                max={MAX_PRICE_PER_1K}
                step={0.00000001}
                value={form.cache_read_price_per_1k}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0.000000"
              />
              {fieldErrors.cache_read_price_per_1k && <FormError error={fieldErrors.cache_read_price_per_1k} />}
            </div>
          </div>

          {/* Description */}
          <div className="space-y-1">
            <label htmlFor="description" className="text-sm font-medium">
              {t('descriptionLabel')} <span className="text-muted-foreground text-xs">({t('descriptionOptional')})</span>
            </label>
            <textarea
              id="description"
              name="description"
              value={form.description}
              onChange={handleChange}
              rows={3}
              maxLength={512}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring resize-none"
              placeholder={t('descriptionPlaceholder')}
            />
            {fieldErrors.description && <FormError error={fieldErrors.description} />}
          </div>

          {/* Display Name */}
          <div className="space-y-1">
            <label htmlFor="display_name" className="text-sm font-medium">
              {t('displayNameLabel')}
            </label>
            <input
              id="display_name"
              name="display_name"
              type="text"
              maxLength={128}
              value={form.display_name}
              onChange={handleChange}
              placeholder={t('displayNamePlaceholder')}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            {fieldErrors.display_name && <FormError error={fieldErrors.display_name} />}
          </div>

          {/* 대상 AWS 계정 (0044) — 레지스트리에서 이름으로 고른다. 역할 ARN·ExternalId 는
              아래 'AWS 계정' 패널이 관리한다. */}
          <div className="space-y-1 rounded-md border border-border p-3">
            <span className="text-sm font-medium">{t('accountSectionTitle')}</span>
            <p className="text-xs text-muted-foreground">{t('accountSectionNote')}</p>
            <label htmlFor="account_name" className="text-xs text-muted-foreground">
              {t('accountSelectLabel')}
            </label>
            <select
              id="account_name"
              name="account_name"
              value={isOpenModel ? '' : form.account_name}
              onChange={handleChange}
              disabled={isOpenModel}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
            >
              <option value="">{t('accountDefaultOption')}</option>
              {accountOptions.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.account_id ? `${a.display_name} (${a.account_id}) — ${a.name}` : a.name}
                </option>
              ))}
            </select>
            {isOpenModel ? (
              <p className="text-xs text-muted-foreground">{t('accountOpenmodelNote')}</p>
            ) : (
              accounts.length === 0 && (
                <p className="text-xs text-muted-foreground">{t('accountNoneRegistered')}</p>
              )
            )}
            {siblings.length > 0 && (
              <div
                role="alert"
                className="mt-2 space-y-1 rounded-md border border-amber-500/50 bg-amber-500/10 p-2"
              >
                <p className="text-xs">{t('accountSiblingsWarning', { count: siblings.length })}</p>
                <ul className="list-disc pl-4 text-xs">
                  {siblings.map((m) => (
                    <li key={m.alias}>
                      {m.alias} ({m.account_name || t('accountColumnDefault')})
                    </li>
                  ))}
                </ul>
                <label className="flex items-center gap-2 text-xs font-medium">
                  <input
                    type="checkbox"
                    checked={applyToSiblings}
                    onChange={(e) => setApplyToSiblings(e.target.checked)}
                  />
                  {t('accountApplyToSiblings', { count: siblings.length })}
                </label>
              </div>
            )}
            {fieldErrors.account_name && <FormError error={fieldErrors.account_name} />}
          </div>

          <FormError error={error} />

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isPending}
              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            >
              {tCommon('cancel')}
            </button>
            <SpinnerButton type="submit" isLoading={isPending}>
              {isEditMode ? t('edit') : t('register')}
            </SpinnerButton>
          </div>
        </form>
      </div>
    </div>
  );
}