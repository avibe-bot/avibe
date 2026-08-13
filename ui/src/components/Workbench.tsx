import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Activity, Bot, FolderPlus, Sparkles } from 'lucide-react';

import { useNewSession } from '../lib/useNewSession';
import { NewProjectDialog } from './workbench/NewProjectDialog';
import { Composer } from './workbench/Composer';
import { ProjectPicker } from './workbench/ProjectPicker';
import { AgentRoutePicker } from './workbench/AgentRoutePicker';

// Mirrors design.pen DnkGJ "Workbench" canvas: a centered hero panel +
// suggestion chips with the shared chat Composer below it. The Composer is
// wired to create a new session under the most-recently-active project using
// the default Agent, then routes to /chat/<id> with the typed message
// pre-seeded. No project? The send surfaces the NewProjectDialog so the user
// gets unstuck without bouncing pages. The create flow lives in the shared
// useNewSession hook — one source of truth with the mobile NewSessionSheet.
export const Workbench: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const ns = useNewSession({
    loadErrorText: t('newSession.loadError'),
    createFailedText: t('newSession.createFailed'),
  });
  const [newProjectOpen, setNewProjectOpen] = useState(false);

  // Returns whether the send actually started, so the Composer only clears the
  // box on a real start — a no-project nudge or a transient create error keeps
  // the typed prompt for retry. Navigation stays here (the hook is router-free).
  const send = async (text: string): Promise<boolean> => {
    const result = await ns.send(text);
    if (result) {
      // Hand the typed message to ChatPage as router state; it replays it
      // through the fire-and-forget compose path so the agent turn starts.
      navigate(`/chat/${encodeURIComponent(result.sessionId)}`, { state: { initialMessage: result.initialMessage } });
      return true;
    }
    if (text.trim() && ns.needsProject) setNewProjectOpen(true);
    return false;
  };

  // Quick chips under the hero — three of the most common first moves.
  const suggestions = [
    { key: 'newProject', icon: FolderPlus, onClick: () => setNewProjectOpen(true) },
    { key: 'openAgents', icon: Bot, onClick: () => navigate('/agents') },
    { key: 'openHarness', icon: Activity, onClick: () => navigate('/harness') },
  ] as const;

  return (
    // Desktop centers the hero + Composer as a group (min-h + justify-center). On
    // mobile we DON'T center or force a tall min-height: a tall centered container
    // fights the iOS keyboard (focusing the composer leaves a big gap / pushes it
    // off-screen). Top-aligned normal flow lets iOS scroll the focused composer
    // into view above the keyboard the way it does for ordinary in-flow inputs.
    <div className="flex flex-col items-center gap-5 md:min-h-[calc(100dvh-7rem)] md:justify-center">
      {/* Hero panel — a centered card, not a full-bleed fill. */}
      <div className="flex w-full max-w-[640px] flex-col items-center gap-6 rounded-2xl border border-border bg-surface-2 px-6 py-10">
        <div className="flex size-14 items-center justify-center rounded-2xl border border-mint/40 bg-mint-soft text-mint shadow-[0_0_24px_-6px_rgba(91,255,160,0.6)]">
          <Sparkles className="size-6" />
        </div>
        <div className="flex max-w-[520px] flex-col items-center gap-3 text-center">
          <h1 className="text-[22px] font-semibold text-foreground">{t('workbench.canvas.heroTitle')}</h1>
          <p className="text-[13px] leading-[1.55] text-muted">{t('workbench.canvas.heroBody')}</p>
        </div>
        <div className="flex flex-wrap items-center justify-center gap-2">
          {suggestions.map(({ key, icon: Icon, onClick }) => (
            <button
              key={key}
              type="button"
              onClick={onClick}
              className="group flex items-center gap-2 rounded-full border border-border-strong bg-surface px-3 py-2 text-[12px] text-foreground transition hover:border-mint/40 hover:bg-mint-soft hover:text-mint"
            >
              <Icon className="size-3.5 text-muted group-hover:text-mint" />
              <span>{t(`workbench.canvas.suggestions.${key}`)}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Input — project + agent pickers above the shared chat Composer so the
          user can see/choose where the session lands and which agent runs it. */}
      <div className="flex w-full max-w-[640px] flex-col gap-3">
        {ns.projects.length > 0 && (
          <ProjectPicker
            projects={ns.projects}
            targetId={ns.target?.id}
            onSelect={ns.setSelected}
            onNewProject={() => setNewProjectOpen(true)}
            disabled={ns.sending}
          />
        )}
        <div className="flex min-w-0 flex-col gap-2">
          <div className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t('newSession.agent')}</div>
          <AgentRoutePicker
            value={ns.agentRoute}
            agents={ns.agents}
            onChange={ns.setAgentRoute}
            defaultLabel={ns.effectiveDefaultAgentName ? t('newSession.defaultAgentNamed', { name: ns.effectiveDefaultAgentName }) : t('newSession.defaultAgent')}
            disabled={ns.sending}
            align="start"
            // Fill the column on mobile (≈ full screen); on desktop hug content
            // and cap at 62% so it reads like the compact Chat-header picker
            // instead of a full-width 640px bar. self-start defeats the flex-col
            // stretch that would otherwise force the trigger to the column width.
            triggerClassName="max-w-full sm:max-w-[62%] sm:self-start"
          />
        </div>
        <Composer
          onSend={send}
          placeholder={t('workbench.canvas.inputPlaceholder')}
          disabled={ns.sending}
          className="max-w-[640px]"
        />
        {ns.needsProject && (
          <div className="px-2 text-[10.5px] text-gold">{t('workbench.canvas.noProjectForChat')}</div>
        )}
        {ns.error && (
          <div className="mt-1 rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
            {ns.error}
          </div>
        )}
      </div>

      {newProjectOpen && (
        <NewProjectDialog
          onClose={() => setNewProjectOpen(false)}
          onCreated={(project) => {
            setNewProjectOpen(false);
            // create_project is find-or-create by path: dedup + hoist to top so the
            // "most recent" target reflects the folder just opened.
            ns.upsertSelectProject(project);
          }}
        />
      )}
    </div>
  );
};
