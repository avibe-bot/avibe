import * as React from 'react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import type { ProjectDefaultAgent, VibeAgentBrief, WorkbenchProject } from '../../context/ApiContext';
import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '../ui/dialog';
import { InfoHint } from '../ui/info-hint';
import { AgentRoutePicker } from './AgentRoutePicker';
import type { AgentRoutePatch } from './AgentRoutePicker';
import { ProjectAgentsMdDialog } from './ProjectAgentsMdDialog';

// Per-project settings. Three sections:
//   1. Working directory (read-only) — where the Agent runs.
//   2. Default Agent (backend + model + effort) — new sessions in this project
//      inherit it; cleared = follow the global default. Reuses the same
//      AgentRoutePicker as the chat header, persisting each pick immediately
//      (no Save button), matching how the chat header patches a session.
//   3. A button that opens the existing "Edit AGENTS.md" editor (the project's
//      guidance prompt), which lives as a file in the project folder.
export const ProjectSettingsDialog: React.FC<{
  project: WorkbenchProject;
  open: boolean;
  onClose: () => void;
}> = ({ project, open, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { setProjectDefaultAgent } = useWorkbenchProjectsTree();
  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [agentsMdOpen, setAgentsMdOpen] = useState(false);

  // Agents feed the picker; fetch once per open (they rarely change).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .listVibeAgents({ includeDisabled: false })
      .then((res) => {
        if (!cancelled) setAgents(res.agents);
      })
      .catch(() => {
        if (!cancelled) setAgents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open, api]);

  // The cache (via the project prop) is the single source of truth for the
  // current default, so the picker reflects a save as soon as it lands.
  const current = project.default_agent ?? null;

  const handleRouteChange = async (patch: AgentRoutePatch) => {
    // The picker emits partial patches (a model-only pick is just {model}), so
    // merge onto the stored default and persist the full 5-field route. A field
    // PRESENT in the patch wins (incl. an explicit null that clears it); an
    // absent field keeps its stored value.
    const merged: ProjectDefaultAgent = {
      agent_backend: null,
      agent_name: 'agent_name' in patch ? patch.agent_name ?? null : current?.agent_name ?? null,
      agent_variant: 'agent_variant' in patch ? patch.agent_variant ?? null : current?.agent_variant ?? null,
      model: 'model' in patch ? patch.model ?? null : current?.model ?? null,
      reasoning_effort:
        'reasoning_effort' in patch ? patch.reasoning_effort ?? null : current?.reasoning_effort ?? null,
    };
    try {
      await setProjectDefaultAgent(project.id, merged);
    } catch {
      // apiFetch already surfaced the error toast; keep the dialog open.
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('projectSettings.title')}</DialogTitle>
            <DialogDescription>{t('projectSettings.description')}</DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-5">
            {/* 1. Working directory (read-only). */}
            <section className="flex flex-col gap-1.5">
              <span className="text-[12px] font-semibold text-foreground">{t('projectSettings.workdir.label')}</span>
              <div className="break-all rounded-lg border border-border bg-surface-2 px-3 py-2 font-mono text-[12px] text-foreground">
                {project.folder_path || t('projectSettings.workdir.unset')}
              </div>
              <span className="text-[11px] leading-relaxed text-muted">{t('projectSettings.workdir.hint')}</span>
            </section>

            {/* 2. Default Agent (backend + model + effort). */}
            <section className="flex flex-col gap-1.5">
              <div className="flex items-center gap-1.5">
                <span className="text-[12px] font-semibold text-foreground">
                  {t('projectSettings.defaultAgent.label')}
                </span>
                <InfoHint
                  label={t('projectSettings.defaultAgent.label')}
                  content={t('projectSettings.defaultAgent.hint')}
                />
              </div>
              <AgentRoutePicker
                value={current ?? {}}
                agents={agents}
                onChange={handleRouteChange}
                defaultLabel={t('projectSettings.defaultAgent.followGlobal')}
                align="start"
                modal
                triggerClassName="w-full"
                onNavigateAway={onClose}
              />
            </section>

            {/* 3. Project guidance prompt → the existing AGENTS.md editor. */}
            <section className="flex flex-col gap-1.5">
              <span className="text-[12px] font-semibold text-foreground">{t('projectSettings.guidance.label')}</span>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="w-full justify-start gap-2"
                disabled={!project.folder_path}
                onClick={() => setAgentsMdOpen(true)}
              >
                <FileText className="size-3.5 text-muted" />
                {t('projectSettings.guidance.button')}
              </Button>
              <span className="text-[11px] leading-relaxed text-muted">
                {project.folder_path ? t('projectSettings.guidance.hint') : t('projectSettings.guidance.noFolder')}
              </span>
            </section>
          </div>
        </DialogContent>
      </Dialog>

      {/* The guidance editor layers on top; the AGENTS.md file lives in the folder. */}
      <ProjectAgentsMdDialog project={project} open={agentsMdOpen} onClose={() => setAgentsMdOpen(false)} />
    </>
  );
};
