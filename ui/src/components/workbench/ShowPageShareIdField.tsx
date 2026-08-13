import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { ApiError, useApi } from '../../context/ApiContext';
import { isValidShareId, SHARE_ID_MAX_LENGTH, type ShowPageLinkInfo } from '../../lib/showPageLinks';

// Editable custom suffix for a public Show Page's /p/<share_id>/ URL. Shared by
// the in-chat share popover and the admin Show Pages page so both surfaces get
// the same validation, error mapping, and save affordance. The caller renders
// its own label (each surface has its own label style) and gates rendering on
// public visibility; this owns the input row + helper/error line.
export const ShowPageShareIdField: React.FC<{
  sessionId: string;
  shareId: string | null;
  // External busy (e.g. a visibility change in flight) disables the field.
  disabled?: boolean;
  // Receives the updated payload so the caller can refresh its link/iframe.
  onSaved: (payload: ShowPageLinkInfo) => void;
}> = ({ sessionId, shareId, disabled, onSaved }) => {
  const { t } = useTranslation();
  const api = useApi();
  const current = shareId ?? '';
  const [draft, setDraft] = useState(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const savedTimer = useRef<number | null>(null);

  // Resync when the saved suffix changes elsewhere (a rotate, or a save from the
  // other surface): the prefilled draft should track the authoritative value.
  useEffect(() => {
    setDraft(shareId ?? '');
    setError(null);
  }, [shareId]);

  useEffect(
    () => () => {
      if (savedTimer.current) window.clearTimeout(savedTimer.current);
    },
    [],
  );

  const trimmed = draft.trim();
  const changed = trimmed !== current;
  const clientValid = isValidShareId(trimmed);
  // Flag a format problem only once the user has typed something new and
  // invalid — never yell at the untouched prefilled value or an empty field.
  const formatError = changed && trimmed.length > 0 && !clientValid;
  const canSave = changed && clientValid && !busy && !disabled;

  const save = async () => {
    if (!canSave) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.setShowPageShareId(sessionId, trimmed);
      onSaved(res);
      setSaved(true);
      if (savedTimer.current) window.clearTimeout(savedTimer.current);
      savedTimer.current = window.setTimeout(() => setSaved(false), 1600);
    } catch (err) {
      // The server is the authority on uniqueness; map its code to a field-level
      // message (a global toast also fires from the shared error handler).
      const code = err instanceof ApiError ? err.code : null;
      setError(
        code === 'share_id_taken'
          ? t('showPages.shareId.errors.taken')
          : code === 'invalid_share_id' || code === 'missing_share_id'
            ? t('showPages.shareId.errors.invalid')
            : t('showPages.shareId.errors.generic'),
      );
    } finally {
      setBusy(false);
    }
  };

  const isError = !!error || formatError;
  const message = error ?? (formatError ? t('showPages.shareId.errors.invalid') : t('showPages.shareId.hint'));

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0 font-mono text-xs text-muted">/p/</span>
        <Input
          value={draft}
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
          maxLength={SHARE_ID_MAX_LENGTH}
          disabled={busy || disabled}
          onChange={(e) => {
            setDraft(e.target.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void save();
            }
          }}
          placeholder={current || t('showPages.shareId.placeholder')}
          aria-label={t('showPages.shareId.label')}
          aria-invalid={isError || undefined}
          className="h-8 flex-1 text-xs"
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 shrink-0"
          disabled={!canSave}
          onClick={() => void save()}
        >
          {busy ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : saved ? (
            <Check className="size-3.5" />
          ) : (
            t('common.save')
          )}
        </Button>
      </div>
      <p className={clsx('text-[11px] leading-snug', isError ? 'text-destructive' : 'text-muted')}>{message}</p>
    </div>
  );
};
