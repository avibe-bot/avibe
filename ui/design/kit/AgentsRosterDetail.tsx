// The panels the proposed Agents page opens beside its list (proposal, 2026-11-02). Each shows only
// what the user can change: a control that cannot apply is left out rather than drawn disabled, and a
// field the user cannot edit reads as text. Bot rules from docs/plans/bots.md D1: a Bot's Agent stays
// enabled, keeps its name, is never the default, and is not deleted; the main Bot is not removed.
import type { ReactNode } from 'react';
import { ChevronRight, Maximize2, MessageSquare, Pencil, Play, Star, Trash2, UserMinus, UserPlus, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { Combobox } from '@/components/ui/combobox';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import type { VibeAgentBrief } from '@/context/ApiContext';
import { useBots } from '@/context/BotsContext';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { eligibleForBot } from '@/lib/bots';

import { AgentGlyph } from './AgentsRoster';
import { useBotRole } from './botRole';
import { useDraft } from './draft';

/** The Agent fields a detail panel draws, beyond the list's brief. */
export interface DetailAgent extends VibeAgentBrief {
  /** Estimated tokens of the system prompt. */
  promptTokens: number;
  /** The efforts the selected model offers. */
  efforts: string[];
}

const noop = () => undefined;

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="font-mono text-xs font-bold uppercase tracking-widest text-muted">{label}</span>
      {children}
    </div>
  );
}

function CloseButton() {
  const { t } = useTranslation();
  return (
    <Button type="button" variant="ghost" size="icon" aria-label={t('common.close')} className="size-7 shrink-0">
      <X className="size-3.5" />
    </Button>
  );
}

/** Model, effort, and system prompt: the shipped fields, unchanged in content. */
function RouteFields({ agent, canEdit }: { agent: DetailAgent; canEdit: boolean }) {
  const { t } = useTranslation();
  return (
    <>
      <Field label={t('agents.detail.model')}>
        <Combobox
          options={agent.model ? [{ value: agent.model, label: agent.model }] : []}
          value={agent.model ?? ''}
          onValueChange={noop}
          placeholder={t('agents.detail.modelPlaceholder')}
          disabled={!canEdit}
        />
      </Field>
      {agent.efforts.length > 0 && (
        <Field label={t('agents.detail.effort')}>
          <div className="grid grid-cols-4 gap-0.5 rounded-lg border border-border-strong bg-surface-2 p-0.5">
            {agent.efforts.map((effort) => (
              <span
                key={effort}
                className={clsx(
                  'truncate rounded-md px-1 py-1.5 text-center text-xs capitalize',
                  effort === agent.reasoning_effort ? 'bg-mint-soft font-bold text-mint-ink' : 'font-medium text-muted',
                )}
              >
                {effort}
              </span>
            ))}
          </div>
        </Field>
      )}
      <div className="flex items-center gap-1.5">
        <div className="flex flex-1 items-center gap-2.5 rounded-lg border border-border bg-foreground/2 px-3 py-2.5">
          <ChevronRight className="size-3 shrink-0 text-muted" />
          <span className="flex-1 text-xs font-semibold text-foreground">{t('agents.detail.systemPrompt')}</span>
          <span className="font-mono text-xs text-muted">{t('agents.detail.systemPromptCount', { count: agent.promptTokens })}</span>
        </div>
        {canEdit && (
          <Button type="button" variant="ghost" size="icon" aria-label={t('agents.detail.systemPromptExpand')} className="size-9 shrink-0 text-muted">
            <Maximize2 className="size-3.5" />
          </Button>
        )}
      </div>
    </>
  );
}

const mintAction = 'border-mint/40 bg-mint-soft text-mint-ink hover:brightness-110';

/** A Bot: its face, name, and role first, the way into its conversation, then its Agent's model and
 *  prompt. Its handle stays in the footer, small; its Agent name is not shown. */
export function BotDetail({ handle, agent, canEdit = true }: { handle: string; agent: DetailAgent; canEdit?: boolean }) {
  const draft = useDraft();
  const { botForHandle, displayName } = useBots();
  const role = useBotRole();
  const bot = botForHandle(handle);
  if (!bot) return null;
  const name = displayName(bot);
  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-start gap-3">
        <BotAvatar avatar={bot.avatar} name={name} size={56} />
        <div className="flex min-w-0 flex-1 flex-col gap-1 pt-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-lg font-bold text-foreground">{name}</span>
            {bot.builtin && (
              <Badge variant="success" className="shrink-0 px-1.5 py-0">
                {draft('主伙伴', 'Main')}
              </Badge>
            )}
          </div>
          <p className="text-xs leading-relaxed text-muted">{role(bot)}</p>
        </div>
        <CloseButton />
      </header>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="xs" className={mintAction}>
          <MessageSquare className="size-3" />
          {draft('进入对话', 'Open conversation')}
        </Button>
        {canEdit && (
          <Button type="button" variant="outline" size="xs">
            <Pencil className="size-3" />
            {draft('编辑资料', 'Edit profile')}
          </Button>
        )}
      </div>
      <div className="h-px bg-border" />
      <RouteFields agent={agent} canEdit={canEdit} />
      {/* The main Bot cannot be removed, and its handle is never typed: it has no footer. */}
      {!bot.builtin && (
        <footer className="flex items-center gap-2 border-t border-border pt-4">
          <span className="font-mono text-xs text-muted" title={draft('代号：命令行用它找到这位伙伴，创建后不变', 'Handle: how the CLI names this Bot; fixed once created')}>
            @{bot.handle}
          </span>
          <div className="flex-1" />
          {canEdit && (
            <Button type="button" variant="destructive-soft" size="xs">
              <UserMinus className="size-3" />
              {draft('移出伙伴', 'Remove from Bots')}
            </Button>
          )}
        </footer>
      )}
    </div>
  );
}

/** An Agent that is not a Bot. The name is the title (editable in place unless built in), the backend
 *  is its subtitle, and "On" is a switch in the header only where the Agent can be turned off. */
export function AgentDetail({
  agent,
  isDefault,
  canEdit = true,
  canRun = true,
}: {
  agent: DetailAgent;
  isDefault: boolean;
  canEdit?: boolean;
  canRun?: boolean;
}) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { enabled: botsEnabled } = useBots();
  const meta = getBackendUiMeta(agent.backend);
  const system = agent.source === 'builtin' || agent.source === 'system';
  // The built-in Agent of the built-in backend is part of the platform: it stays on.
  const canToggle = canEdit && !(system && meta.builtin);
  const canSetDefault = canEdit && !isDefault && agent.enabled;
  const canSetBot = canEdit && botsEnabled && !isDefault && eligibleForBot(agent);
  const locked = system || !canEdit;
  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-start gap-3">
        <AgentGlyph backend={agent.backend} size="lg" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 pt-0.5">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-lg font-bold text-foreground">{agent.name}</span>
            {!locked && <Pencil className="size-3 shrink-0 text-muted" />}
            {isDefault && (
              <Badge variant="success" className="shrink-0 px-1.5 py-0">
                {t('common.default')}
              </Badge>
            )}
          </div>
          <span className="text-xs text-muted">{meta.label}</span>
        </div>
        {canToggle && (
          <label className="flex shrink-0 items-center gap-2 pt-1.5 text-xs text-muted">
            {draft('启用', 'On')}
            <Switch size="sm" checked={agent.enabled} onCheckedChange={noop} label={t('agents.detail.enabled')} />
          </label>
        )}
        <CloseButton />
      </header>
      <Field label={t('agents.detail.description')}>
        {locked ? (
          <p className="text-sm leading-relaxed text-foreground">{agent.description || '—'}</p>
        ) : (
          <Textarea value={agent.description ?? ''} onChange={noop} rows={2} placeholder={t('agents.detail.descriptionPlaceholder')} className="text-sm" />
        )}
      </Field>
      <RouteFields agent={agent} canEdit={canEdit} />
      {(canRun || canSetDefault || canSetBot || (canEdit && !system)) && (
        <footer className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          {canRun && (
            <Button type="button" variant="outline" size="xs" className={mintAction}>
              <Play className="size-3" />
              {t('agents.detail.run')}
            </Button>
          )}
          {canSetDefault && (
            <Button type="button" variant="ghost" size="xs">
              <Star className="size-3" />
              {t('agents.detail.setDefault')}
            </Button>
          )}
          {canSetBot && (
            <Button type="button" variant="ghost" size="xs">
              <UserPlus className="size-3" />
              {draft('设为伙伴', 'Make a Bot')}
            </Button>
          )}
          <div className="flex-1" />
          {canEdit && !system && (
            <Button type="button" variant="destructive-soft" size="xs">
              <Trash2 className="size-3" />
              {t('common.delete')}
            </Button>
          )}
        </footer>
      )}
    </div>
  );
}

/** Removing a Bot asks once, here and in the sidebar strip alike, and says what stays. */
export function RemoveBotConfirm({ handle }: { handle: string }) {
  const draft = useDraft();
  const { botForHandle, displayName } = useBots();
  const bot = botForHandle(handle);
  if (!bot) return null;
  const name = displayName(bot);
  return (
    <ConfirmDialog
      open
      onOpenChange={noop}
      onConfirm={noop}
      destructive
      title={draft(`把${name}移出伙伴？`, `Remove ${name} from Bots?`)}
      description={draft(
        `对话和记忆都会保留，它回到 Agents 列表里；之后重新设为伙伴，就能接着聊。`,
        `Its conversation and memory are kept, and it returns to the Agents list. Make it a Bot again to pick up where you left off.`,
      )}
      confirmLabel={draft('移出伙伴', 'Remove')}
    />
  );
}
