import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import { CloudOff, Loader2, Mail, Plus, RefreshCw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Input } from '@/components/ui/input';
import { ApiError, useApi } from '@/context/ApiContext';
import {
  normalizeShowPageAuthorizedEmail,
  requiresShowPageEmailRevocationConfirmation,
} from '@/lib/showPageAccess';

type EmailAccessGate = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error';

const normalizedSet = (emails: string[]): string[] => [...new Set(emails)].sort();

const emailAccessUnavailable = (error: unknown): boolean => error instanceof ApiError
  && [
    'show_page_email_access_not_configured',
    'show_page_email_access_unavailable',
  ].includes(error.code ?? '');

export function ShowPageEmailAccessEditor({
  active,
  canManage,
  sessionId,
}: {
  active: boolean;
  canManage: boolean;
  sessionId: string;
}) {
  const { t } = useTranslation();
  const api = useApi();
  const [gate, setGate] = useState<EmailAccessGate>('idle');
  const [emails, setEmails] = useState<string[]>([]);
  const [savedEmails, setSavedEmails] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [draftInvalid, setDraftInvalid] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const generationRef = useRef(0);

  const load = useCallback(async () => {
    const generation = ++generationRef.current;
    setGate('loading');
    try {
      const result = await api.getShowPageAuthorizedEmails(sessionId);
      if (generation !== generationRef.current) return;
      const next = normalizedSet(result.emails);
      setEmails(next);
      setSavedEmails(next);
      setGate('ready');
    } catch (error) {
      if (generation !== generationRef.current) return;
      setGate(emailAccessUnavailable(error) ? 'unavailable' : 'error');
    }
  }, [api, sessionId]);

  useEffect(() => {
    generationRef.current += 1;
    setGate('idle');
    setEmails([]);
    setSavedEmails([]);
    setDraft('');
    setDraftInvalid(false);
    setSaving(false);
    setConfirmNarrowing(false);
    if (active && canManage) void load();
    return () => {
      generationRef.current += 1;
    };
  }, [active, canManage, load, sessionId]);

  const dirty = useMemo(
    () => emails.join('\u0000') !== savedEmails.join('\u0000'),
    [emails, savedEmails],
  );

  if (!canManage) return null;

  const addDraft = () => {
    const normalized = normalizeShowPageAuthorizedEmail(draft);
    if (!normalized) {
      setDraftInvalid(true);
      return;
    }
    setEmails((current) => normalizedSet([...current, normalized]));
    setDraft('');
    setDraftInvalid(false);
  };

  const commit = async () => {
    if (!dirty || saving || draft.trim()) return;
    setConfirmNarrowing(false);
    setSaving(true);
    try {
      const result = await api.replaceShowPageAuthorizedEmails(sessionId, emails);
      const next = normalizedSet(result.emails);
      setEmails(next);
      setSavedEmails(next);
      setGate('ready');
    } catch (error) {
      setGate(emailAccessUnavailable(error) ? 'unavailable' : 'error');
    } finally {
      setSaving(false);
    }
  };

  const save = () => {
    if (!dirty || saving || draft.trim()) return;
    if (requiresShowPageEmailRevocationConfirmation(savedEmails, emails)) {
      setConfirmNarrowing(true);
      return;
    }
    void commit();
  };

  return (
    <>
      <div className="space-y-2.5 border-t border-border pt-2.5">
        <div className="flex items-start gap-2">
          <Mail className="mt-0.5 size-3.5 shrink-0 text-muted" />
          <div className="min-w-0">
            <div className="text-xs font-medium">{t('chat.showPage.emailAccess')}</div>
            <p className="mt-0.5 text-[11px] leading-snug text-muted">
              {t('chat.showPage.emailAccessDesc')}
            </p>
          </div>
        </div>

        {gate === 'loading' || gate === 'idle' ? (
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <Loader2 className="size-3.5 animate-spin" />
            {t('chat.showPage.loadingEmailAccess')}
          </div>
        ) : null}

        {gate === 'unavailable' ? (
          <div className="flex items-start gap-1.5 text-[11px] leading-snug text-muted">
            <CloudOff className="mt-0.5 size-3.5 shrink-0" />
            {t('chat.showPage.emailAccessUnavailable')}
          </div>
        ) : null}

        {gate === 'error' ? (
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] leading-snug text-destructive">
              {t('chat.showPage.emailAccessError')}
            </span>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0"
              onClick={() => void load()}
              aria-label={t('common.retry')}
              title={t('common.retry')}
            >
              <RefreshCw className="size-3.5" />
            </Button>
          </div>
        ) : null}

        {gate === 'ready' ? (
          <>
            <div className="flex items-start gap-1.5">
              <div className="min-w-0 flex-1">
                <Input
                  type="email"
                  value={draft}
                  disabled={saving || emails.length >= 64}
                  onChange={(event) => {
                    setDraft(event.target.value);
                    if (draftInvalid) setDraftInvalid(false);
                  }}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter') return;
                    event.preventDefault();
                    addDraft();
                  }}
                  placeholder={t('chat.showPage.emailPlaceholder')}
                  aria-label={t('chat.showPage.emailAccess')}
                  aria-invalid={draftInvalid || undefined}
                  className={clsx('h-8 text-xs', draftInvalid && 'border-destructive')}
                />
                {draftInvalid ? (
                  <p className="mt-1 text-[11px] text-destructive">
                    {t('chat.showPage.emailInvalid')}
                  </p>
                ) : null}
              </div>
              <Button
                type="button"
                size="icon"
                variant="outline"
                className="size-8 shrink-0"
                disabled={saving || !draft.trim() || emails.length >= 64}
                onClick={addDraft}
                aria-label={t('chat.showPage.addEmail')}
                title={t('chat.showPage.addEmail')}
              >
                <Plus className="size-3.5" />
              </Button>
            </div>

            {emails.length ? (
              <div className="max-h-32 divide-y divide-border overflow-y-auto rounded-md border border-border">
                {emails.map((email) => (
                  <div key={email} className="flex min-h-8 items-center gap-2 px-2">
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px]">{email}</span>
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      className="size-6 shrink-0"
                      disabled={saving}
                      onClick={() => setEmails((current) => current.filter((value) => value !== email))}
                      aria-label={t('chat.showPage.removeEmail', { email })}
                      title={t('chat.showPage.removeEmail', { email })}
                    >
                      <X className="size-3" />
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-[11px] leading-snug text-muted">
                {t('chat.showPage.emailAccessEmpty')}
              </p>
            )}

            <div className="flex items-center justify-between gap-3">
              <span className="text-[10px] text-muted">{t('chat.showPage.emailAccessLimit')}</span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7"
                disabled={!dirty || saving || Boolean(draft.trim())}
                onClick={save}
              >
                {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
                {t('chat.showPage.applyEmailAccess')}
              </Button>
            </div>
          </>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmNarrowing}
        title={t('organization.resources.narrowTitle')}
        description={t('organization.resources.narrowBody')}
        confirmLabel={t('organization.actions.saveChanges')}
        onConfirm={commit}
      />
    </>
  );
}
