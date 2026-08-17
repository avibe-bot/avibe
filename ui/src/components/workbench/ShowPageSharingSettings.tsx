import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import { Loader2, Plus, RefreshCw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { useApi } from '@/context/ApiContext';
import {
  normalizeShowAccessEmail,
  showAccessDraftChanged,
  showAccessTargetEmails,
  type ShowAccess,
  type ShowAccessMode,
} from '@/lib/showPageAccess';
import { isValidShareId, SHARE_ID_MAX_LENGTH } from '@/lib/showPageLinks';

type Gate = 'idle' | 'loading' | 'ready' | 'conflict' | 'share_id_taken' | 'invalid' | 'error';

const normalizedSet = (emails: string[]): string[] => [...new Set(emails)].sort();

const requiresNarrowingConfirmation = (
  saved: ShowAccess,
  mode: ShowAccessMode,
  emails: string[],
): boolean => {
  if (saved.access_mode === 'public' && mode !== 'public') return true;
  if (saved.access_mode !== 'limited') return false;
  if (mode === 'private') return true;
  if (mode !== 'limited') return false;
  const next = new Set(emails);
  return saved.normalized_emails.some((email) => !next.has(email));
};

export function ShowPageSharingSettings({
  active,
  canManage,
  sessionId,
  onApplied,
  onConfirmationOpenChange,
  ownerWindowId,
}: {
  active: boolean;
  canManage: boolean;
  sessionId: string;
  onApplied?: (showAccess: ShowAccess) => void;
  onConfirmationOpenChange?: (open: boolean) => void;
  ownerWindowId?: string;
}) {
  const { t } = useTranslation();
  const api = useApi();
  const [gate, setGate] = useState<Gate>('idle');
  const [saved, setSaved] = useState<ShowAccess | null>(null);
  const [mode, setMode] = useState<ShowAccessMode>('private');
  const [shareId, setShareId] = useState('');
  const [emails, setEmails] = useState<string[]>([]);
  const [emailDraft, setEmailDraft] = useState('');
  const [emailInvalid, setEmailInvalid] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const generationRef = useRef(0);

  const setConfirmationOpen = useCallback((next: boolean) => {
    setConfirmNarrowing(next);
    onConfirmationOpenChange?.(next);
  }, [onConfirmationOpenChange]);

  const adopt = useCallback((showAccess: ShowAccess) => {
    if (showAccess.page_id !== sessionId) throw new Error('ShowAccess page identity mismatch');
    setSaved(showAccess);
    setMode(showAccess.access_mode);
    setShareId(showAccess.share_id ?? '');
    setEmails(showAccess.normalized_emails);
    setEmailDraft('');
    setEmailInvalid(false);
  }, [sessionId]);

  const load = useCallback(async (settledGate: Gate = 'ready') => {
    const generation = ++generationRef.current;
    setGate('loading');
    try {
      const result = await api.getShowAccessSettings(sessionId);
      if (generation !== generationRef.current) return;
      adopt(result.show_access);
      setGate(settledGate);
    } catch {
      if (generation !== generationRef.current) return;
      setGate('error');
    }
  }, [adopt, api, sessionId]);

  useEffect(() => {
    generationRef.current += 1;
    setSaved(null);
    setMode('private');
    setShareId('');
    setEmails([]);
    setEmailDraft('');
    setEmailInvalid(false);
    setSaving(false);
    setConfirmationOpen(false);
    setGate('idle');
    if (active && canManage) void load();
    return () => {
      generationRef.current += 1;
    };
  }, [active, canManage, load, sessionId, setConfirmationOpen]);

  const normalizedShareId = shareId.trim() || null;
  const sharedMode = mode === 'limited' || mode === 'public';
  const shareIdInvalid = sharedMode && (!normalizedShareId || !isValidShareId(normalizedShareId));
  const targetEmails = useMemo(
    () => showAccessTargetEmails(mode, emails),
    [emails, mode],
  );
  const dirty = Boolean(
    saved && showAccessDraftChanged(saved, mode, normalizedShareId, emails),
  );
  const draftBlocked = Boolean(emailDraft.replace(/^[\t\n\f\r\v ]+|[\t\n\f\r\v ]+$/g, ''));
  const invalid = shareIdInvalid || (mode === 'limited' && targetEmails.length === 0);
  const editable = canManage && gate !== 'loading' && !saving;

  const addEmail = () => {
    const normalized = normalizeShowAccessEmail(emailDraft);
    if (!normalized) {
      setEmailInvalid(true);
      return;
    }
    setEmails((current) => normalizedSet([...current, normalized]));
    setEmailDraft('');
    setEmailInvalid(false);
  };

  const commit = async () => {
    if (!saved || !dirty || invalid || draftBlocked || !editable) return;
    setSaving(true);
    setConfirmationOpen(false);
    try {
      const result = await api.applyShowAccess(sessionId, {
        expected_revision: saved.revision,
        target_access_mode: mode,
        target_share_id: normalizedShareId,
        target_emails: targetEmails,
      });
      if (result.show_access.page_id !== sessionId) throw new Error('ShowAccess page identity mismatch');
      if (result.status === 'conflict') {
        await load('conflict');
        return;
      }
      if (result.status === 'share_id_taken' || result.status === 'invalid') {
        setGate(result.status);
        return;
      }
      adopt(result.show_access);
      setGate('ready');
      onApplied?.(result.show_access);
    } catch {
      setGate('error');
    } finally {
      setSaving(false);
    }
  };

  const save = () => {
    if (!saved || !dirty || invalid || draftBlocked || !editable) return;
    if (requiresNarrowingConfirmation(saved, mode, targetEmails)) {
      setConfirmationOpen(true);
      return;
    }
    void commit();
  };

  if (!canManage) return null;

  return (
    <>
      <section className="space-y-2.5" aria-label={t('chat.showPage.sharingAccess')}>
        <div>
          <div className="text-sm font-medium">{t('chat.showPage.sharingAccess')}</div>
          <p className="mt-0.5 text-[11px] leading-snug text-muted">
            {t('chat.showPage.sharingAccessDesc')}
          </p>
        </div>

        {gate === 'loading' || gate === 'idle' ? (
          <div className="flex h-9 items-center gap-1.5 text-[11px] text-muted">
            <Loader2 className="size-3.5 animate-spin" />
            {t('chat.showPage.loadingSharingAccess')}
          </div>
        ) : saved ? (
          <>
            <SegmentedRadio<ShowAccessMode>
              value={mode}
              onChange={(next) => {
                if (editable) setMode(next);
              }}
              disabled={!editable}
              ariaLabel={t('chat.showPage.sharingAccess')}
              options={[
                { id: 'private', label: t('chat.showPage.sharingModes.private') },
                { id: 'limited', label: t('chat.showPage.sharingModes.limited') },
                { id: 'public', label: t('chat.showPage.sharingModes.public') },
              ]}
            />
            <p className="text-[11px] leading-snug text-muted">
              {t(`chat.showPage.sharingHelp.${mode}`)}
            </p>

            {mode === 'limited' ? (
              <div className="space-y-2">
                <div className="flex items-start gap-1.5">
                  <div className="min-w-0 flex-1">
                    <Input
                      type="email"
                      value={emailDraft}
                      disabled={!editable}
                      onChange={(event) => {
                        setEmailDraft(event.target.value);
                        setEmailInvalid(false);
                      }}
                      onKeyDown={(event) => {
                        if (event.key !== 'Enter') return;
                        event.preventDefault();
                        addEmail();
                      }}
                      placeholder={t('chat.showPage.emailPlaceholder')}
                      aria-label={t('chat.showPage.limitedEmails')}
                      aria-invalid={emailInvalid || undefined}
                      className={clsx('h-8 text-xs', emailInvalid && 'border-destructive')}
                    />
                    {emailInvalid ? (
                      <p className="mt-1 text-[11px] text-destructive-ink">
                        {t('chat.showPage.emailInvalid')}
                      </p>
                    ) : null}
                  </div>
                  <Button
                    type="button"
                    size="icon"
                    variant="outline"
                    className="size-8 shrink-0"
                    disabled={!editable || !emailDraft}
                    onClick={addEmail}
                    aria-label={t('chat.showPage.addEmail')}
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
                          disabled={!editable}
                          onClick={() => setEmails((current) => current.filter((value) => value !== email))}
                          aria-label={t('chat.showPage.removeEmail', { email })}
                        >
                          <X className="size-3" />
                        </Button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-[11px] text-destructive-ink">
                    {t('chat.showPage.limitedEmailRequired')}
                  </p>
                )}
              </div>
            ) : null}

            {sharedMode ? (
              <div className="space-y-1">
                <div className="flex items-center gap-1.5">
                  <span className="shrink-0 font-mono text-xs text-muted">/p/</span>
                  <Input
                    value={shareId}
                    spellCheck={false}
                    autoCapitalize="none"
                    autoCorrect="off"
                    maxLength={SHARE_ID_MAX_LENGTH}
                    disabled={!editable}
                    onChange={(event) => {
                      setShareId(event.target.value);
                      if (gate === 'share_id_taken') setGate('ready');
                    }}
                    aria-label={t('showPages.shareId.label')}
                    aria-invalid={shareIdInvalid || gate === 'share_id_taken' || undefined}
                    className={clsx(
                      'h-8 min-w-0 flex-1 font-mono text-xs',
                      (shareIdInvalid || gate === 'share_id_taken') && 'border-destructive',
                    )}
                  />
                </div>
                {shareIdInvalid || gate === 'share_id_taken' ? (
                  <p className="text-[11px] text-destructive-ink">
                    {gate === 'share_id_taken'
                      ? t('showPages.shareId.errors.taken')
                      : t('showPages.shareId.errors.invalid')}
                  </p>
                ) : null}
              </div>
            ) : null}

            {['conflict', 'invalid', 'error'].includes(gate) ? (
              <div className="flex items-center justify-between gap-2 border-t border-border pt-2">
                <span className="text-[11px] leading-snug text-destructive-ink">
                  {t(`chat.showPage.sharingErrors.${gate}`)}
                </span>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="size-7 shrink-0"
                  onClick={() => void load()}
                  aria-label={t('common.retry')}
                >
                  <RefreshCw className="size-3.5" />
                </Button>
              </div>
            ) : null}

            <div className="flex justify-end">
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7"
                disabled={!dirty || invalid || draftBlocked || !editable}
                onClick={save}
              >
                {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
                {t('chat.showPage.applySharingAccess')}
              </Button>
            </div>
          </>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-destructive-ink">
              {t('chat.showPage.sharingErrors.error')}
            </span>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7"
              onClick={() => void load()}
              aria-label={t('common.retry')}
            >
              <RefreshCw className="size-3.5" />
            </Button>
          </div>
        )}
      </section>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmationOpen}
        title={t('chat.showPage.narrowSharingTitle')}
        description={t('chat.showPage.narrowSharingBody')}
        confirmLabel={t('chat.showPage.applySharingAccess')}
        onConfirm={commit}
        windowOwnerId={ownerWindowId}
      />
    </>
  );
}
