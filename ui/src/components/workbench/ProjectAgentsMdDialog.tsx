import * as React from 'react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import type { WorkbenchProject } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { EditorDialog } from '../ui/editor-dialog';
import { Switch } from '../ui/switch';
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog';

type AgentsMdData = {
  content: string;
  source: 'agents' | 'claude' | 'none';
  symlinked: boolean;
  claude_is_regular_file: boolean;
};

// Edit a project's AGENTS.md from the workbench. AGENTS.md is the canonical
// agent-instructions file; CLAUDE.md is the legacy name, so the editor reads
// AGENTS.md (falling back to CLAUDE.md when it's missing) and offers a "migrate
// CLAUDE.md into AGENTS.md + keep them in sync via a symlink" toggle. Content
// is fetched before the editor opens so EditorDialog seeds its draft from the
// real file rather than an empty string.
export const ProjectAgentsMdDialog: React.FC<{
  project: WorkbenchProject;
  open: boolean;
  onClose: () => void;
}> = ({ project, open, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [data, setData] = useState<AgentsMdData | null>(null);
  const [symlink, setSymlink] = useState(true);

  useEffect(() => {
    if (!open) {
      setData(null);
      return;
    }
    let cancelled = false;
    api.getProjectAgentsMd(project.id).then(
      (loaded) => {
        if (cancelled) return;
        setData(loaded);
        // Default the migration toggle on (recommended), EXCEPT when a separate
        // real CLAUDE.md sits alongside an existing AGENTS.md: there a default-on
        // save would silently replace CLAUDE.md content the editor never showed,
        // so that case is an explicit opt-in.
        const separateRealClaude = loaded.claude_is_regular_file && loaded.source !== 'claude';
        setSymlink(!separateRealClaude);
      },
      () => {
        // The shared apiFetch layer already surfaced the error toast.
        if (cancelled) return;
        onClose();
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, project.id]);

  if (!open) return null;

  // Brief load before the editor opens so its draft seeds from the real file.
  if (!data) {
    return (
      <Dialog open onOpenChange={(next) => !next && onClose()}>
        <DialogContent className="flex max-w-sm items-center gap-3" aria-describedby={undefined}>
          <DialogTitle className="sr-only">{t('agentsMd.title')}</DialogTitle>
          <Loader2 className="size-4 animate-spin text-muted" />
          <span className="text-[13px] text-muted">{t('agentsMd.loading')}</span>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <EditorDialog
      open
      onClose={onClose}
      dismissable={false}
      title={t('agentsMd.title')}
      description={t('agentsMd.description')}
      value={data.content}
      placeholder={t('agentsMd.placeholder')}
      notice={data.source === 'claude' ? t('agentsMd.loadedFromClaude') : undefined}
      footerSlot={
        <div className="flex items-start gap-2.5">
          <Switch
            checked={symlink}
            onCheckedChange={setSymlink}
            label={t('agentsMd.symlinkToggle')}
            className="mt-0.5"
          />
          <span className="text-[12px] leading-relaxed text-foreground">
            {t('agentsMd.symlinkToggle')}
            {symlink && data.claude_is_regular_file && (
              <span className="mt-0.5 block text-[11px] text-gold-ink">
                {t('agentsMd.symlinkMigrateWarning')}
              </span>
            )}
          </span>
        </div>
      }
      onSave={async (content) => {
        // A failed save rejects here: the apiFetch layer shows the error toast,
        // and EditorDialog keeps the dialog open so the in-progress draft isn't lost.
        const res = await api.saveProjectAgentsMd(project.id, { content, symlink });
        showToast(
          res.symlink_error ? t('agentsMd.savedSymlinkFailed') : t('agentsMd.saved'),
          res.symlink_error ? 'warning' : 'success',
        );
      }}
    />
  );
};
