import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Loader2, Play, X } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import type { VibeAgentFull, WorkbenchProject } from '../../context/ApiContext';
import { Button } from '../ui/button';
import { Select } from '../ui/select';

interface RunAgentDialogProps {
  agent: VibeAgentFull;
  onClose: () => void;
}

// Lightweight project picker that spins up a session under the picked
// project bound to the given Agent, then routes the user straight into
// the Chat page. Mirrors the Harness create-via-chat flow but skips the
// seed-prompt step — Run means "start a clean chat using this agent",
// not "ask the agent to set itself up".
export const RunAgentDialog: React.FC<RunAgentDialogProps> = ({ agent, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<WorkbenchProject[] | null>(null);
  const [projectId, setProjectId] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listProjects()
      .then((result) => {
        if (cancelled) return;
        const chatProjects = result.projects.filter((project) => project.capabilities.can_chat);
        setProjects(chatProjects);
        if (chatProjects.length > 0) {
          const sorted = [...chatProjects].sort((a, b) => {
            const aTs = a.last_active_at || a.created_at;
            const bTs = b.last_active_at || b.created_at;
            return bTs.localeCompare(aTs);
          });
          setProjectId(sorted[0].id);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const open = async () => {
    if (!projectId || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const session = await api.createSession({
        project_id: projectId,
        agent_backend: agent.backend,
        agent_name: agent.name,
        agent_id: agent.id,
        model: agent.model || undefined,
        reasoning_effort: agent.reasoning_effort || undefined,
      });
      navigate(`/chat/${encodeURIComponent(session.id)}`);
    } catch (err: any) {
      setError(err?.message ?? String(err));
      setSubmitting(false);
    }
  };

  const noProjects = projects !== null && projects.length === 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      role="dialog"
      aria-modal="true"
      aria-label={t('agents.runDialog.title')}
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-md flex-col gap-4 rounded-2xl border border-border-strong bg-surface p-5 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <div className="flex size-9 items-center justify-center rounded-lg border border-mint/30 bg-mint-soft text-mint">
            <Play className="size-4" />
          </div>
          <div className="flex flex-1 flex-col gap-0.5">
            <div className="text-[14px] font-bold text-foreground">{t('agents.runDialog.title')}</div>
            <div className="text-[11.5px] leading-relaxed text-muted">
              {t('agents.runDialog.description')}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
            className="text-muted transition hover:text-foreground"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="flex flex-col gap-1.5">
          <label className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-muted">
            {t('agents.runDialog.projectLabel')}
          </label>
          {projects === null ? (
            <div className="flex items-center gap-2 rounded-md border border-border-strong bg-surface-2 px-3 py-2 text-[12px] text-muted">
              <Loader2 className="size-3 animate-spin" />
              {t('common.loading')}
            </div>
          ) : noProjects ? (
            <div className="rounded-md border border-dashed border-border bg-foreground/[0.02] px-3 py-2 text-[12px] text-muted">
              {t('agents.runDialog.noProject')}
            </div>
          ) : (
            <Select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="text-[12.5px]"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name}
                </option>
              ))}
            </Select>
          )}
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
            {error}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={submitting}>
            {t('agents.runDialog.cancel')}
          </Button>
          <Button
            type="button"
            variant="brand"
            size="sm"
            onClick={open}
            disabled={!projectId || submitting || noProjects}
          >
            {submitting ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                {t('agents.runDialog.opening')}
              </>
            ) : (
              <>
                {t('agents.runDialog.open')}
                <ArrowRight className="size-3.5" />
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
};
