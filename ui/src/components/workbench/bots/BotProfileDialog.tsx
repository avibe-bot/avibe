import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Bot as BotIcon, Check, ChevronRight, Loader2, Lock, Pencil } from 'lucide-react';
import clsx from 'clsx';

import { useApi, type VibeAgentBrief } from '../../../context/ApiContext';
import { useBots, type BotProfileRequest } from '../../../context/BotsContext';
import { useToast } from '../../../context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';
import { BOT_HANDLE_RE, botSessionPath, eligibleForBot, type Bot, type BotEmojiAvatarInput } from '@/lib/bots';
import { Button } from '../../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../../ui/dialog';
import { Input } from '../../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../../ui/popover';
import { SegmentedRadio } from '../../ui/segmented';
import { Textarea } from '../../ui/textarea';
import { AgentRoutePicker, type AgentRoutePatch } from '../AgentRoutePicker';
import { BotAvatar } from '../../ui/bot-avatar';
import { BotAvatarPicker, type BotAvatarChoice } from './BotAvatarPicker';


type HandleState =
  | { status: 'idle' }
  | { status: 'generating' }
  /** `forName` is the display name the handle was generated from. */
  | { status: 'ready'; handle: string; forName: string };

/** The server's `normalize_handle`, to show the handle an existing Agent will keep. */
function agentNameHandle(name: string): string {
  const candidate = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/^[^a-z]+/, '')
    .slice(0, 24)
    .replace(/-+$/, '');
  return candidate || name.toLowerCase();
}



/** The add/edit dialog wherever the shell mounts it: open on the provider's request. */
export const BotProfileDialogHost: React.FC = () => {
  const { enabled, profileRequest, closeProfile } = useBots();
  if (!enabled) return null;
  return <BotProfileDialog request={profileRequest} onClose={closeProfile} />;
};

export const BotProfileDialog: React.FC<{ request: BotProfileRequest | null; onClose: () => void }> = ({
  request,
  onClose,
}) => {
  // A fresh form per request: the state below is the form's own, not the last one's.
  const key = request ? (request.kind === 'edit' ? `edit:${request.handle}` : `create:${request.tab ?? 'new'}:${request.agentName ?? ''}`) : 'closed';
  return (
    <Dialog open={request !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      {request && <BotProfileForm key={key} request={request} onClose={onClose} />}
    </Dialog>
  );
};

const BotProfileForm: React.FC<{ request: BotProfileRequest; onClose: () => void }> = ({ request, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const { mainBot, botForHandle, displayName, refresh } = useBots();
  const editing = request.kind === 'edit' ? botForHandle(request.handle) ?? null : null;
  const [tab, setTab] = useState<'new' | 'agent'>(request.kind === 'create' ? request.tab ?? 'new' : 'new');
  const [agents, setAgents] = useState<VibeAgentBrief[] | null>(null);
  const [agentName, setAgentName] = useState<string | null>(request.kind === 'create' ? request.agentName ?? null : null);
  const [name, setName] = useState(editing?.display_name ?? '');
  const [role, setRole] = useState(editing?.role ?? '');
  const [avatar, setAvatar] = useState<BotAvatarChoice>(editing ? { kind: 'keep' } : { kind: 'default' });
  const [pickerOpen, setPickerOpen] = useState(false);
  const [handle, setHandle] = useState<HandleState>({ status: 'idle' });
  const [handleDraft, setHandleDraft] = useState<string | null>(null);
  const [handleEdited, setHandleEdited] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [route, setRoute] = useState<{ model: string | null; reasoning_effort: string | null } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const suggestionRef = useRef<{ controller: AbortController; promise: Promise<string | null> } | null>(null);

  const mainName = mainBot ? displayName(mainBot) : t('bots.mainDefaultName');
  const mainAgent = useMemo(
    () => (agents && mainBot ? agents.find((agent) => agent.name === mainBot.agent_name) ?? null : null),
    [agents, mainBot],
  );
  const eligibleAgents = useMemo(() => (agents ?? []).filter(eligibleForBot), [agents]);
  const pickedAgent = eligibleAgents.find((agent) => agent.name === agentName) ?? null;

  useEffect(() => {
    let live = true;
    api
      .listVibeAgents({ cache: false })
      .then((result) => {
        if (live) setAgents(result.agents ?? []);
      })
      .catch(() => {
        if (live) setAgents([]);
      });
    return () => {
      live = false;
    };
  }, [api]);

  // The existing-Agent tab's display name starts as the Agent's name until the user types one.
  const nameTouchedRef = useRef(Boolean(editing));
  useEffect(() => {
    if (tab === 'agent' && pickedAgent && !nameTouchedRef.current) setName(pickedAgent.name);
  }, [tab, pickedAgent]);

  // An unsaved upload's preview URL is released when it is replaced or the form closes.
  const previewUrl = avatar.kind === 'image' ? avatar.previewUrl : null;
  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  }, [previewUrl]);

  // D2: the handle is generated in the background once a name is entered — the
  // server normalizes an ASCII name and asks the main Bot's model for any other.
  // A handle the user edited is theirs; the name no longer drives it.
  const suggest = useCallback(
    (displayNameValue: string, roleValue: string): Promise<string | null> => {
      suggestionRef.current?.controller.abort();
      const trimmed = displayNameValue.trim();
      if (!trimmed) {
        suggestionRef.current = null;
        setHandle({ status: 'idle' });
        return Promise.resolve(null);
      }
      const controller = new AbortController();
      setHandle({ status: 'generating' });
      const promise = api
        .suggestBotHandle({ display_name: trimmed, role: roleValue.trim() }, { signal: controller.signal })
        .then((result) => {
          if (controller.signal.aborted) return null;
          setHandle(result.handle ? { status: 'ready', handle: result.handle, forName: trimmed } : { status: 'idle' });
          return result.handle || null;
        })
        .catch(() => {
          if (!controller.signal.aborted) setHandle({ status: 'idle' });
          return null;
        });
      suggestionRef.current = { controller, promise };
      return promise;
    },
    [api],
  );
  const creatingNew = request.kind === 'create' && tab === 'new';
  // The role refines a model's codename but does not ask for a new one per keystroke.
  const roleRef = useRef(role);
  useEffect(() => {
    roleRef.current = role;
  }, [role]);
  useEffect(() => {
    if (!creatingNew || handleEdited) return;
    const timer = window.setTimeout(() => void suggest(name, roleRef.current), 600);
    return () => window.clearTimeout(timer);
  }, [creatingNew, handleEdited, name, suggest]);
  // Closing the form cancels what it was doing: a suggestion in flight, and a
  // create still waiting for its handle, which then never sends.
  const closedRef = useRef(false);
  const submitControllerRef = useRef<AbortController | null>(null);
  useEffect(() => () => {
    closedRef.current = true;
    suggestionRef.current?.controller.abort();
    submitControllerRef.current?.abort();
  }, []);

  const handleError = handleDraft !== null && handleDraft !== '' && !BOT_HANDLE_RE.test(handleDraft)
    ? t('bots.dialog.handleInvalid')
    : null;

  const emojiInput: BotEmojiAvatarInput | null = avatar.kind === 'emoji' ? { emoji: avatar.emoji, hue: avatar.hue } : null;

  const uploadAvatar = async (bot: Bot) => {
    if (avatar.kind !== 'image') return bot;
    try {
      return await api.uploadBotAvatar(bot.handle, avatar.file);
    } catch (err) {
      // The Bot exists; only its picture did not land. Say so and keep going.
      showToast(errorMessage(err) || t('bots.dialog.avatarUploadFailed'), 'error');
      return bot;
    }
  };

  const submit = async () => {
    if (submitting) return;
    setError(null);
    const trimmedName = name.trim();
    if (!trimmedName && !(editing?.builtin)) {
      setError(t('bots.dialog.nameRequired'));
      return;
    }
    if (handleError) return;
    setSubmitting(true);
    try {
      if (request.kind === 'edit') {
        if (!editing) return;
        const patch: { display_name?: string; avatar?: BotEmojiAvatarInput | null } = {};
        if (trimmedName !== editing.display_name) patch.display_name = trimmedName;
        if (avatar.kind === 'emoji') patch.avatar = emojiInput;
        if (avatar.kind === 'default') patch.avatar = null;
        const bot = Object.keys(patch).length > 0 ? await api.updateBot(editing.handle, patch) : editing;
        await uploadAvatar(bot);
        if (!editing.builtin && role.trim() !== editing.role.trim()) {
          await api.updateVibeAgent(editing.agent_name, { description: role.trim() || null });
        }
        await refresh();
        onClose();
        return;
      }
      if (tab === 'agent') {
        if (!pickedAgent) {
          setError(t('bots.dialog.pickAgent'));
          return;
        }
        let bot = await api.setBot({ agent_name: pickedAgent.name, display_name: trimmedName, avatar: emojiInput });
        bot = await uploadAvatar(bot);
        await refresh();
        onClose();
        navigate(botSessionPath(bot));
        return;
      }
      // The handle shown for this exact name, else a fresh one: a suggestion is
      // worth its few seconds, since without it the server falls back to
      // transliteration, which reads worse for a CJK name.
      let chosenHandle: string | null = handleEdited ? handleDraft?.trim() || null : null;
      if (!handleEdited) {
        if (handle.status === 'ready' && handle.forName === trimmedName) {
          chosenHandle = handle.handle;
        } else {
          // Its own request, not the field's: a later keystroke's suggestion must not
          // cancel the create, and only closing the form may.
          const controller = new AbortController();
          submitControllerRef.current = controller;
          try {
            const suggestion = await api.suggestBotHandle(
              { display_name: trimmedName, role: role.trim() },
              { signal: controller.signal },
            );
            chosenHandle = suggestion.handle || null;
          } catch {
            // A failed suggestion leaves the handle to the server.
            chosenHandle = null;
          }
          if (closedRef.current || controller.signal.aborted) return;
        }
      }
      let bot = await api.createBot({
        display_name: trimmedName,
        handle: chosenHandle,
        role: role.trim() || null,
        avatar: emojiInput,
        ...(route ? { model: route.model, reasoning_effort: route.reasoning_effort } : {}),
      });
      bot = await uploadAvatar(bot);
      await refresh();
      onClose();
      navigate(botSessionPath(bot));
    } catch (err) {
      setError(errorMessage(err) || t('bots.dialog.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  // The avatar shown: the pick, else what the Bot has, else the name's default.
  const shownAvatar = avatar.kind === 'emoji'
    ? { kind: 'emoji' as const, emoji: avatar.emoji, hue: avatar.hue }
    : avatar.kind === 'keep'
      ? editing?.avatar ?? null
      : avatar.kind === 'default' && editing?.builtin
        ? { kind: 'mascot' as const }
        : null;
  const avatarName = name.trim() || (editing ? displayName(editing) : '') || (tab === 'agent' ? pickedAgent?.name ?? '' : '');
  const hasNonDefaultAvatar = avatar.kind === 'emoji' || avatar.kind === 'image'
    || (avatar.kind === 'keep' && Boolean(editing?.avatar) && editing?.avatar?.kind !== 'mascot');

  const title = request.kind === 'edit' ? t('bots.dialog.editTitle') : t('bots.dialog.addTitle');
  const primary = request.kind === 'edit'
    ? t('bots.dialog.save')
    : tab === 'agent'
      ? t('bots.dialog.set')
      : t('bots.dialog.create');
  const footerHint = request.kind === 'edit'
    ? null
    : tab === 'agent'
      ? t('bots.dialog.fromAgentHint')
      : t('bots.dialog.chatHint', { name: mainName });

  const avatarButton = (
    <Popover open={pickerOpen} onOpenChange={setPickerOpen} modal>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={t('bots.dialog.avatar')}
          className="relative shrink-0 rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60"
        >
          <BotAvatar avatar={shownAvatar} name={avatarName} size={64} previewUrl={previewUrl} />
          <span className="absolute -bottom-0.5 -right-0.5 grid size-6 place-items-center rounded-full border border-border-strong bg-surface-2 text-foreground">
            <Pencil className="size-3" />
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[300px] rounded-2xl border-border-strong bg-surface-2 p-3.5">
        <BotAvatarPicker
          current={avatar.kind === 'emoji'
            ? { emoji: avatar.emoji, hue: avatar.hue }
            : shownAvatar?.kind === 'emoji'
              ? { emoji: shownAvatar.emoji, hue: shownAvatar.hue }
              : null}
          canUseDefault={hasNonDefaultAvatar}
          onPick={(choice) => {
            setAvatar(choice);
            if (choice.kind !== 'emoji') setPickerOpen(false);
          }}
          onError={(message) => setError(message)}
        />
      </PopoverContent>
    </Popover>
  );

  const nameField = (label: string) => (
    <label className="flex min-w-0 flex-col gap-1.5">
      <span className="text-[12px] font-semibold text-foreground">{label}</span>
      <Input
        value={name}
        autoFocus
        maxLength={64}
        onChange={(event) => {
          nameTouchedRef.current = true;
          const next = event.target.value;
          setName(next);
          // A handle made for another name is stale at once; a new one follows (D2).
          if (creatingNew && !handleEdited) {
            setHandle((current) => (current.status === 'ready' && current.forName === next.trim()
              ? current
              : next.trim() ? { status: 'generating' } : { status: 'idle' }));
          }
        }}
        placeholder={editing?.builtin ? t('bots.mainDefaultName') : t('bots.dialog.namePlaceholder')}
        className="text-[13px]"
      />
    </label>
  );

  const roleField = (
    <label className="flex flex-col gap-1.5">
      <span className="text-[12px] font-semibold text-foreground">{t('bots.dialog.role')}</span>
      <Textarea
        value={role}
        onChange={(event) => setRole(event.target.value)}
        rows={3}
        placeholder={t('bots.dialog.rolePlaceholder')}
        className="min-h-[60px] text-[13px]"
      />
      <span className="text-[11px] leading-relaxed text-muted">{t('bots.dialog.roleHint')}</span>
    </label>
  );

  return (
    <DialogContent
      className="max-w-[460px] gap-0 p-0 max-md:p-0"
      closeLabel={t('common.close')}
      onOpenAutoFocus={(event) => {
        // The name field asks for focus itself; on the Agent tab the list leads.
        if (tab === 'agent') event.preventDefault();
      }}
    >
      <div className="flex flex-col gap-1 px-5 pt-5">
        <DialogTitle className="text-[16px] font-bold">{title}</DialogTitle>
        <DialogDescription className="text-[12px] leading-relaxed">{t('bots.dialog.description')}</DialogDescription>
      </div>

      {request.kind === 'create' && (
        <div className="px-5 pt-4">
          <SegmentedRadio
            value={tab}
            onChange={(next) => {
              setTab(next);
              setError(null);
            }}
            ariaLabel={title}
            options={[
              { id: 'new', label: t('bots.dialog.tabNew') },
              { id: 'agent', label: t('bots.dialog.tabFromAgent') },
            ]}
          />
        </div>
      )}

      <div className="flex flex-col gap-4 px-5 py-4">
        {request.kind === 'create' && tab === 'agent' && (
          agents === null ? (
            <div className="flex items-center gap-2 text-[12px] text-muted">
              <Loader2 className="size-3.5 animate-spin" />
              {t('common.loading')}
            </div>
          ) : eligibleAgents.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border px-3 py-4 text-center text-[12px] leading-relaxed text-muted">
              {t('bots.dialog.noEligibleAgents')}
            </div>
          ) : (
            <div className="flex max-h-[180px] flex-col gap-1.5 overflow-y-auto" role="radiogroup" aria-label={t('bots.dialog.tabFromAgent')}>
              {eligibleAgents.map((agent) => {
                const picked = agent.name === agentName;
                return (
                  <button
                    key={agent.id}
                    type="button"
                    role="radio"
                    aria-checked={picked}
                    onClick={() => setAgentName(agent.name)}
                    className={clsx(
                      'flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition',
                      picked ? 'border-mint bg-mint-soft' : 'border-border hover:bg-foreground/[0.03]',
                    )}
                  >
                    <span className={clsx('grid size-4 shrink-0 place-items-center rounded-full border', picked ? 'border-mint' : 'border-border-strong')}>
                      {picked && <span className="size-2 rounded-full bg-mint" />}
                    </span>
                    <BotIcon className="size-4 shrink-0 text-muted" />
                    <span className="min-w-0 flex-1 leading-tight">
                      <span className="block truncate font-mono text-[12px] font-semibold text-foreground">{agent.name}</span>
                      {agent.description && <span className="block truncate text-[11px] text-muted">{agent.description}</span>}
                    </span>
                  </button>
                );
              })}
            </div>
          )
        )}

        {(request.kind === 'edit' || tab === 'new' || pickedAgent) && (
          <div className="flex items-start gap-4">
            {avatarButton}
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              {nameField(tab === 'agent' || editing ? t('bots.dialog.displayName') : t('bots.dialog.name'))}
              {request.kind === 'edit' && editing ? (
                <HandleLine handle={editing.handle} note={t('bots.dialog.handleFrozen')} locked />
              ) : tab === 'agent' && pickedAgent ? (
                <HandleLine handle={agentNameHandle(pickedAgent.name)} note={t('bots.dialog.handleLocked')} locked />
              ) : handleEdited || handleDraft !== null ? (
                <div className="flex flex-col gap-1">
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-[12px] font-semibold text-muted">{t('bots.handlePrefix')}</span>
                    <Input
                      value={handleDraft ?? ''}
                      autoFocus
                      aria-label={t('bots.dialog.handleEdit')}
                      aria-invalid={handleError ? true : undefined}
                      onChange={(event) => {
                        setHandleEdited(true);
                        setHandleDraft(event.target.value.toLowerCase());
                      }}
                      className="h-7 flex-1 px-2 font-mono text-[12px]"
                    />
                  </div>
                  <span className={clsx('text-[11px]', handleError ? 'text-destructive-ink' : 'text-muted')}>
                    {handleError ?? t('bots.dialog.handleHint')}
                  </span>
                </div>
              ) : handle.status === 'generating' ? (
                <div className="flex items-center gap-1.5 text-[12px] text-muted" role="status">
                  <Loader2 className="size-3.5 animate-spin" />
                  {t('bots.dialog.handleGenerating')}
                </div>
              ) : handle.status === 'ready' ? (
                <HandleLine
                  handle={handle.handle}
                  note={t('bots.dialog.handleHint')}
                  onEdit={() => setHandleDraft(handle.handle)}
                  editLabel={t('bots.dialog.handleEdit')}
                />
              ) : null}
            </div>
          </div>
        )}

        {(request.kind === 'edit' ? !editing?.builtin : tab === 'new') && roleField}
        {request.kind === 'edit' && editing?.builtin && editing.role && (
          <div className="flex flex-col gap-1.5">
            <span className="text-[12px] font-semibold text-foreground">{t('bots.dialog.role')}</span>
            <p className="text-[12px] leading-relaxed text-muted">{editing.role}</p>
          </div>
        )}

        {request.kind === 'create' && tab === 'new' && mainAgent && (
          <div className="flex flex-col gap-2">
            <button
              type="button"
              aria-expanded={advancedOpen}
              onClick={() => setAdvancedOpen((open) => !open)}
              className="flex items-center gap-1.5 self-start text-[12px] text-muted transition hover:text-foreground"
            >
              <ChevronRight className={clsx('size-3.5 transition-transform', advancedOpen && 'rotate-90')} />
              {route
                ? t('bots.dialog.advancedModel', { model: [route.model, route.reasoning_effort].filter(Boolean).join(' · ') })
                : t('bots.dialog.advancedFollows', {
                  name: mainName,
                  model: [mainAgent.model, mainAgent.reasoning_effort].filter(Boolean).join(' · ') || t('newSession.defaultAgent'),
                })}
            </button>
            {advancedOpen && (
              <div className="flex flex-col gap-1.5 pl-5">
                <AgentRoutePicker
                  presentation="compact"
                  modal
                  align="start"
                  value={{
                    agent_name: mainAgent.name,
                    agent_backend: mainAgent.backend,
                    agent_variant: mainAgent.backend,
                    model: route ? route.model : mainAgent.model,
                    reasoning_effort: route ? route.reasoning_effort : mainAgent.reasoning_effort,
                  }}
                  agents={[mainAgent]}
                  agentLocked
                  // Any exit the picker offers leaves the dialog behind it closed.
                  onNavigateAway={onClose}
                  onChange={(patch: AgentRoutePatch) => {
                    setRoute({
                      model: patch.model ?? (route ? route.model : mainAgent.model),
                      reasoning_effort: patch.reasoning_effort !== undefined
                        ? patch.reasoning_effort
                        : route ? route.reasoning_effort : mainAgent.reasoning_effort,
                    });
                  }}
                  triggerClassName="max-w-full"
                />
                {route && (
                  <button
                    type="button"
                    onClick={() => setRoute(null)}
                    className="self-start text-[11px] font-medium text-muted transition hover:text-foreground"
                  >
                    {t('bots.dialog.followMain', { name: mainName })}
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {error && (
          <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
            {error}
          </p>
        )}
      </div>

      <div className="flex items-center gap-2 border-t border-border px-5 py-3">
        <p className="min-w-0 flex-1 text-[11px] leading-relaxed text-muted">{footerHint}</p>
        <Button type="button" variant="outline" size="sm" onClick={onClose}>
          {t('common.cancel')}
        </Button>
        <Button
          type="button"
          size="sm"
          onClick={() => void submit()}
          disabled={submitting || (request.kind === 'create' && tab === 'agent' && !pickedAgent) || Boolean(handleError)}
        >
          {submitting ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
          {primary}
        </Button>
      </div>
    </DialogContent>
  );
};

const HandleLine: React.FC<{
  handle: string;
  note: string;
  locked?: boolean;
  onEdit?: () => void;
  editLabel?: string;
}> = ({ handle, note, locked, onEdit, editLabel }) => {
  const { t } = useTranslation();
  return (
  <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[12px] text-muted">
    <span className="whitespace-nowrap font-mono font-semibold text-foreground">{t('bots.handle', { handle })}</span>
    <span className="min-w-0 flex-1">{note}</span>
    {locked ? (
      <Lock className="size-3.5 shrink-0" aria-hidden="true" />
    ) : (
      <button
        type="button"
        onClick={onEdit}
        aria-label={editLabel}
        title={editLabel}
        className="grid size-5 shrink-0 place-items-center rounded transition hover:bg-foreground/[0.06] hover:text-foreground"
      >
        <Pencil className="size-3.5" />
      </button>
    )}
  </div>
  );
};
