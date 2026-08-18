import { useCallback, useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import {
  Check,
  ChevronDown,
  Globe2,
  Loader2,
  LockKeyhole,
  Plus,
  RefreshCw,
  Users,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { useApi } from '@/context/ApiContext';
import {
  normalizeShowAccessEmail,
  showAccessDraftChanged,
  showAccessTargetEmails,
  SHOW_ACCESS_EMAIL_MAX_COUNT,
  type ShowAccess,
  type ShowAccessMode,
} from '@/lib/showPageAccess';
import { isValidShareId, SHARE_ID_MAX_LENGTH } from '@/lib/showPageLinks';

type Gate = 'idle' | 'loading' | 'ready' | 'conflict' | 'share_id_taken' | 'invalid' | 'error';

const normalizedSet = (emails: string[]): string[] => [...new Set(emails)].sort();

const MODE_ICONS = {
  private: LockKeyhole,
  limited: Users,
  public: Globe2,
} satisfies Record<ShowAccessMode, typeof LockKeyhole>;

function AccessModeSelect({
  value,
  disabled,
  onChange,
  ownerWindowId,
}: {
  value: ShowAccessMode;
  disabled: boolean;
  onChange: (value: ShowAccessMode) => void;
  ownerWindowId?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const CurrentIcon = MODE_ICONS[value];
  const modes = (['private', 'limited', 'public'] as const).map((mode) => ({
    mode,
    label: t(`chat.showPage.sharingModes.${mode}`),
    description: t(`chat.showPage.sharingHelp.${mode}`),
  }));

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-haspopup="listbox"
          aria-label={t('chat.showPage.sharingModeCurrent', {
            mode: t(`chat.showPage.sharingModes.${value}`),
          })}
          className="flex h-9 w-40 items-center gap-2 rounded-md border border-border bg-background px-3 text-left text-[13px] font-medium text-foreground outline-none transition hover:border-border-strong focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          <CurrentIcon data-access-icon={value} className="size-4 shrink-0 text-cyan-ink" />
          <span className="min-w-0 flex-1 truncate">{t(`chat.showPage.sharingModes.${value}`)}</span>
          <ChevronDown className="size-4 shrink-0 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={4}
        role="listbox"
        aria-label={t('chat.showPage.sharingAccess')}
        data-window-owner-id={ownerWindowId}
        className="w-64 space-y-0.5 p-1"
      >
        {modes.map(({ mode, label, description }) => {
          const Icon = MODE_ICONS[mode];
          const selected = value === mode;
          return (
            <button
              key={mode}
              type="button"
              role="option"
              aria-selected={selected}
              disabled={disabled}
              onClick={() => {
                setOpen(false);
                onChange(mode);
              }}
              className={clsx(
                'flex w-full items-start gap-2.5 rounded-sm px-2.5 py-2 text-left transition-colors',
                selected ? 'bg-mint/[0.1]' : 'hover:bg-foreground/[0.05]',
              )}
            >
              <Icon
                data-access-icon={mode}
                className={clsx(
                  'mt-0.5 size-4 shrink-0',
                  selected ? 'text-mint-ink' : 'text-muted',
                )}
              />
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] font-medium text-foreground">{label}</span>
                <span className="mt-0.5 block text-[11px] leading-snug text-muted">{description}</span>
              </span>
              {selected ? <Check className="mt-0.5 size-4 shrink-0 text-mint-ink" /> : null}
            </button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
}

export function ShowPageSharingSettings({
  active,
  canManage,
  sessionId,
  onApplied,
  ownerWindowId,
  showCustomLink = true,
}: {
  active: boolean;
  canManage: boolean;
  sessionId: string;
  onApplied?: (showAccess: ShowAccess) => void;
  ownerWindowId?: string;
  showCustomLink?: boolean;
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
  const generationRef = useRef(0);
  const savingRef = useRef(false);
  const savedRef = useRef<ShowAccess | null>(null);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  const adopt = useCallback(
    (showAccess: ShowAccess, preserveShareIdDraft = false) => {
      if (showAccess.page_id !== sessionId) throw new Error('ShowAccess page identity mismatch');
      savedRef.current = showAccess;
      setSaved(showAccess);
      setMode(showAccess.access_mode);
      if (!preserveShareIdDraft) setShareId(showAccess.share_id ?? '');
      setEmails(showAccess.normalized_emails);
      setEmailDraft('');
      setEmailInvalid(false);
    },
    [sessionId],
  );

  const load = useCallback(async (
    settledGate: Gate = 'ready',
    preserveShareIdDraft = false,
  ) => {
    const generation = ++generationRef.current;
    setGate('loading');
    try {
      const result = await api.getShowAccessSettings(sessionId);
      if (generation !== generationRef.current) return;
      adopt(result.show_access, preserveShareIdDraft);
      setGate(settledGate);
    } catch {
      if (generation !== generationRef.current) return;
      setGate('error');
    }
  }, [adopt, api, sessionId]);

  useEffect(() => {
    generationRef.current += 1;
    savingRef.current = false;
    savedRef.current = null;
    setSaved(null);
    setMode('private');
    setShareId('');
    setEmails([]);
    setEmailDraft('');
    setEmailInvalid(false);
    setSaving(false);
    setGate('idle');
    if (active && canManage) void load();
    return () => {
      generationRef.current += 1;
      savingRef.current = false;
    };
  }, [active, canManage, load, sessionId]);

  const normalizedShareId = shareId.trim() || null;
  const sharedMode = mode === 'limited' || mode === 'public';
  const shareIdInvalid = sharedMode && (!normalizedShareId || !isValidShareId(normalizedShareId));
  const accessDirty = Boolean(
    saved && showAccessDraftChanged(saved, mode, saved.share_id, emails),
  );
  const shareIdDirty = Boolean(saved && normalizedShareId !== saved.share_id);
  const emailLimitReached = emails.length >= SHOW_ACCESS_EMAIL_MAX_COUNT;
  const editable = canManage && gate !== 'loading' && !saving;

  const commit = async (
    nextMode: ShowAccessMode,
    nextShareId: string,
    nextEmails: string[],
    preserveShareIdDraft = false,
  ) => {
    const current = savedRef.current;
    const targetShareId = nextShareId.trim() || null;
    const nextTargetEmails = showAccessTargetEmails(nextMode, nextEmails);
    const nextInvalid =
      (nextMode !== 'private' && (!targetShareId || !isValidShareId(targetShareId)))
      || (nextMode === 'limited' && nextTargetEmails.length === 0);
    if (
      !current
      || savingRef.current
      || nextInvalid
      || !canManage
      || gate === 'loading'
      || !showAccessDraftChanged(current, nextMode, targetShareId, nextEmails)
    ) {
      return;
    }
    const generation = generationRef.current;
    const requestSessionId = sessionId;
    const isCurrent = () =>
      generation === generationRef.current && requestSessionId === sessionIdRef.current;
    savingRef.current = true;
    setSaving(true);
    try {
      const result = await api.applyShowAccess(requestSessionId, {
        expected_revision: current.revision,
        target_access_mode: nextMode,
        target_share_id: targetShareId,
        target_emails: nextTargetEmails,
      });
      if (result.show_access.page_id !== requestSessionId) {
        throw new Error('ShowAccess page identity mismatch');
      }
      if (!isCurrent()) {
        // Collapsing the row unmounts this editor, but the successful write still
        // has to reconcile the shared inventory so its copied link is not stale.
        // A real session change updates sessionIdRef and remains discarded.
        if (result.status === 'applied' && requestSessionId === sessionIdRef.current) {
          onApplied?.(result.show_access);
        }
        return;
      }
      if (result.status === 'conflict') {
        savingRef.current = false;
        setSaving(false);
        await load('conflict', preserveShareIdDraft);
        return;
      }
      if (result.status === 'share_id_taken' || result.status === 'invalid') {
        setGate(result.status);
        return;
      }
      adopt(result.show_access, preserveShareIdDraft);
      setGate('ready');
      onApplied?.(result.show_access);
    } catch {
      if (!isCurrent()) return;
      setGate('error');
    } finally {
      if (isCurrent()) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  };

  const changeMode = (nextMode: ShowAccessMode) => {
    if (!editable || nextMode === mode) return;
    setMode(nextMode);
    setEmailInvalid(false);
    if (nextMode === 'limited' && emails.length === 0) return;
    void commit(nextMode, savedRef.current?.share_id ?? shareId, emails, shareIdDirty);
  };

  const addEmail = () => {
    if (!editable || emailLimitReached) return;
    const normalized = normalizeShowAccessEmail(emailDraft);
    if (!normalized) {
      setEmailInvalid(true);
      return;
    }
    const nextEmails = normalizedSet([...emails, normalized]);
    setEmails(nextEmails);
    setEmailDraft('');
    setEmailInvalid(false);
    if (mode === 'limited') {
      void commit(mode, savedRef.current?.share_id ?? shareId, nextEmails, shareIdDirty);
    }
  };

  const removeEmail = (email: string) => {
    if (!editable || (mode === 'limited' && emails.length <= 1)) return;
    const nextEmails = emails.filter((value) => value !== email);
    setEmails(nextEmails);
    if (mode === 'limited') {
      void commit(mode, savedRef.current?.share_id ?? shareId, nextEmails, shareIdDirty);
    }
  };

  const saveShareId = () => {
    if (!editable || shareIdInvalid) return;
    void commit(mode, shareId, emails, true);
  };

  if (!canManage) return null;

  return (
    <div className="space-y-2.5">
      {gate === 'loading' || gate === 'idle' ? (
        <div className="flex min-h-9 items-center justify-between gap-3">
          <div className="text-sm font-medium">{t('chat.showPage.sharingAccess')}</div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <Loader2 className="size-3.5 animate-spin" />
            {t('chat.showPage.loadingSharingAccess')}
          </div>
        </div>
      ) : saved ? (
        <>
          {showCustomLink && sharedMode ? (
            <section
              className="space-y-1.5"
              aria-label={t('showPages.shareId.label')}
            >
              <div className="text-sm font-medium">{t('showPages.shareId.label')}</div>
              <div className="grid w-full max-w-[17.5rem] grid-cols-[auto_minmax(0,1fr)_2rem] items-center gap-1.5">
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
                    'h-8 min-w-0 w-full font-mono text-xs',
                    (shareIdInvalid || gate === 'share_id_taken') && 'border-destructive',
                  )}
                />
                <div className="size-8 shrink-0">
                  {shareIdDirty ? (
                    <Button
                      type="button"
                      size="icon"
                      variant="outline"
                      className="size-8"
                      disabled={
                        !editable
                        || shareIdInvalid
                        || (mode === 'limited' && emails.length === 0)
                      }
                      onClick={saveShareId}
                      aria-label={t('common.save')}
                      title={t('common.save')}
                    >
                      <Check className="size-3.5" />
                    </Button>
                  ) : null}
                </div>
              </div>
              {shareIdInvalid || gate === 'share_id_taken' ? (
                <p className="text-[11px] text-destructive-ink">
                  {gate === 'share_id_taken'
                    ? t('showPages.shareId.errors.taken')
                    : t('showPages.shareId.errors.invalid')}
                </p>
              ) : null}
            </section>
          ) : null}

          <section
            aria-label={t('chat.showPage.sharingAccess')}
            className={clsx(
              'space-y-2.5',
              showCustomLink && sharedMode && 'border-t border-border pt-3',
            )}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium">{t('chat.showPage.sharingAccess')}</div>
                <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted" role="status">
                  {saving ? (
                    <>
                      <Loader2 className="size-3 animate-spin" />
                      {t('common.saving')}
                    </>
                  ) : (
                    <>
                      <Check className="size-3 text-mint-ink" />
                      {t('chat.showPage.sharingAutoSave')}
                    </>
                  )}
                </div>
              </div>
              <AccessModeSelect
                value={mode}
                disabled={!editable}
                onChange={changeMode}
                ownerWindowId={ownerWindowId}
              />
            </div>

            {mode === 'limited' ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-foreground">
                    {t('chat.showPage.limitedEmails')}
                  </span>
                  {saved.access_mode === 'limited' && !accessDirty && !saving ? (
                    <span className="flex items-center gap-1 text-[11px] text-mint-ink">
                      <Check className="size-3" />
                      {t('common.saved')}
                    </span>
                  ) : null}
                </div>
                <div className="flex w-full max-w-[17.5rem] items-start gap-1.5">
                  <div className="min-w-0 flex-1">
                    <Input
                      type="email"
                      value={emailDraft}
                      disabled={!editable || emailLimitReached}
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
                    disabled={!editable || !emailDraft || emailLimitReached}
                    onClick={addEmail}
                    aria-label={t('chat.showPage.addEmail')}
                  >
                    <Plus className="size-3.5" />
                  </Button>
                </div>
                {emails.length ? (
                  <div className="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto">
                    {emails.map((email) => {
                      const lastLimitedEmail = mode === 'limited' && emails.length === 1;
                      return (
                        <span
                          key={email}
                          className="inline-flex h-7 max-w-full items-center gap-1 rounded-md border border-border bg-foreground/[0.04] pl-2.5 pr-1 text-[11px] text-foreground"
                        >
                          <span className="min-w-0 truncate font-mono">{email}</span>
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            className="size-5 shrink-0"
                            disabled={!editable || lastLimitedEmail}
                            onClick={() => removeEmail(email)}
                            aria-label={t('chat.showPage.removeEmail', { email })}
                            title={lastLimitedEmail
                              ? t('chat.showPage.keepOneLimitedEmail')
                              : t('chat.showPage.removeEmail', { email })}
                          >
                            <X className="size-3" />
                          </Button>
                        </span>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-[11px] text-destructive-ink">
                    {t('chat.showPage.limitedEmailRequired')}
                  </p>
                )}
                <p className="text-[11px] text-muted">
                  {t('chat.showPage.limitedEmailHint', { count: SHOW_ACCESS_EMAIL_MAX_COUNT })}
                </p>
              </div>
            ) : null}
          </section>

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
                onClick={() => void load('ready', shareIdDirty)}
                aria-label={t('common.retry')}
              >
                <RefreshCw className="size-3.5" />
              </Button>
            </div>
          ) : null}
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
            onClick={() => void load('ready', shareIdDirty)}
            aria-label={t('common.retry')}
          >
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
      )}
    </div>
  );
}
