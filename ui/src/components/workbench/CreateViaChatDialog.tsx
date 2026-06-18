import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Loader2, Sparkles, X } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import type { WorkbenchProject } from '../../context/ApiContext';
import { Button } from '../ui/button';
import { Select } from '../ui/select';

export type CreateViaChatKind = 'task' | 'watch';

interface CreateViaChatDialogProps {
  kind: CreateViaChatKind;
  onClose: () => void;
}

// Mirrors design.pen AbIUE — single 500px-wide violet-glow card that
// invites the user to describe a task/watch in natural language. The
// "Open chat" button creates a fresh session under the selected project
// and seeds it with an intent-shaped first user message; the agent
// finishes the job by calling `vibe task add` / `vibe watch add`.
export const CreateViaChatDialog: React.FC<CreateViaChatDialogProps> = ({ kind, onClose }) => {
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
        setProjects(result.projects);
        if (result.projects.length > 0) {
          // Default to the most recently active project so the user can
          // usually just hit "Open chat" without touching the picker.
          const sorted = [...result.projects].sort((a, b) => {
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
      const titleKey = kind === 'task' ? 'workbench.createDialog.kindTask' : 'workbench.createDialog.kindWatch';
      // Omit agent_backend so the server routes through the default Agent
      // rather than a hard-coded backend.
      const session = await api.createSession({
        project_id: projectId,
        title: t(`harness.createDialog.${kind === 'task' ? 'kindTask' : 'kindWatch'}`, {
          // titleKey is only here so the translation linter doesn't drop it
          defaultValue: t(titleKey, { defaultValue: 'Background work' }),
        }),
      });
      const prompt = t(
        kind === 'task' ? 'harness.createDialog.promptTask' : 'harness.createDialog.promptWatch',
      );
      // Hand the seed prompt to ChatPage as router state; it replays the
      // message through the fire-and-forget compose path (plain POST →
      // dispatch_async) so the agent turn actually starts and the reply arrives
      // over the session stream — otherwise the task/watch creation would sit
      // with a persisted prompt that no dispatch ever picks up.
      navigate(`/chat/${encodeURIComponent(session.id)}`, {
        state: { initialMessage: prompt },
      });
    } catch (err: any) {
      setError(err?.message ?? String(err));
      setSubmitting(false);
    }
  };

  const noProjects = projects !== null && projects.length === 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[#080812F0] px-4"
      role="dialog"
      aria-modal="true"
      aria-label={t('harness.createDialog.title')}
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-[500px] flex-col items-center gap-5 rounded-2xl border border-violet/30 bg-surface p-7 shadow-[0_24px_48px_-6px_rgba(0,0,0,0.8),0_0_32px_-12px_rgba(124,91,255,0.55)]"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          aria-label={t('harness.createDialog.cancel')}
          onClick={onClose}
          className="absolute right-4 top-4 text-muted transition hover:text-foreground"
        >
          <X className="size-4" />
        </button>

        {/* Hero icon — 64x64 violet-soft tile with glow */}
        <div className="flex size-16 items-center justify-center rounded-2xl border border-violet/30 bg-violet-soft text-violet shadow-[0_0_28px_-6px_rgba(124,91,255,0.6)]">
          <Sparkles className="size-[30px]" />
        </div>

        <div className="flex flex-col gap-1.5 text-center">
          <div className="text-[20px] font-bold text-foreground">{t('harness.createDialog.title')}</div>
          <div className="text-[12px] italic text-muted">{t('harness.createDialog.subtitle')}</div>
        </div>

        <div className="flex w-full flex-col gap-2.5 rounded-xl border border-border bg-foreground/[0.015] px-[18px] py-4">
          <p className="text-[12.5px] leading-[1.65] text-foreground">
            {t('harness.createDialog.body')}
          </p>
          <div className="h-px w-full bg-border" />
          <p className="text-[11.5px] leading-[1.65] text-muted">{t('harness.createDialog.example')}</p>
        </div>

        <div className="flex w-full flex-col gap-1.5">
          <label className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-muted">
            {t('harness.createDialog.projectLabel')}
          </label>
          {projects === null ? (
            <div className="flex items-center gap-2 rounded-md border border-border-strong bg-surface-2 px-3 py-2 text-[12px] text-muted">
              <Loader2 className="size-3 animate-spin" />
              {t('common.loading')}
            </div>
          ) : noProjects ? (
            <div className="rounded-md border border-dashed border-border bg-foreground/[0.02] px-3 py-2 text-[12px] text-muted">
              {t('harness.createDialog.noProject')}
            </div>
          ) : (
            <Select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="text-[12.5px]"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name} · {p.folder_path}
                </option>
              ))}
            </Select>
          )}
        </div>

        {error && (
          <div className="w-full rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
            {error}
          </div>
        )}

        <div className="flex w-full items-center justify-end gap-2.5">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="rounded-lg border border-border-strong px-4 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04] disabled:opacity-50"
          >
            {t('harness.createDialog.cancel')}
          </button>
          <Button
            type="button"
            variant="brand-violet"
            size="sm"
            onClick={open}
            disabled={submitting || noProjects || !projectId}
          >
            {submitting ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                {t('harness.createDialog.opening')}
              </>
            ) : (
              <>
                {t('harness.createDialog.open')}
                <ArrowRight className="size-3.5" />
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
};
