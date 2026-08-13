import { useTranslation } from 'react-i18next';
import { Folder, FolderOpen, FolderPlus } from 'lucide-react';
import clsx from 'clsx';

import type { WorkbenchProject } from '../../context/ApiContext';
import { Button } from '../ui/button';

interface ProjectPickerProps {
  projects: WorkbenchProject[];
  /** The resolved create target's id — the chip to highlight. */
  targetId: string | undefined;
  onSelect: (id: string) => void;
  onNewProject: () => void;
  disabled?: boolean;
}

// Shared project chip picker for the create surfaces (Workbench home +
// NewSessionSheet) so the user can see + choose where a new session lands. A
// horizontal scroll row of EVERY project (a target beyond the first few stays
// reachable) plus a New Project chip; the active chip is the resolved target.
export const ProjectPicker: React.FC<ProjectPickerProps> = ({ projects, targetId, onSelect, onNewProject, disabled }) => {
  const { t } = useTranslation();
  return (
    // min-w-0 lets this shrink inside the sheet's CSS grid so the chip row
    // scrolls horizontally instead of stretching the whole sheet wide.
    <div className="flex min-w-0 flex-col gap-2">
      <div className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-muted">
        {t('newSession.project')}
      </div>
      <div className="-mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
        {projects.map((project) => {
          const active = project.id === targetId;
          return (
            <Button
              key={project.id}
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onSelect(project.id)}
              disabled={disabled}
              className={clsx(
                'h-auto shrink-0 gap-1.5 rounded-full px-3 py-1.5 text-[12.5px] font-medium',
                active ? 'border-mint/40 bg-mint-soft text-mint hover:bg-mint-soft hover:text-mint' : 'text-foreground',
              )}
            >
              {active ? <FolderOpen className="size-3.5" /> : <Folder className="size-3.5" />}
              <span className="max-w-[140px] truncate">{project.display_name}</span>
            </Button>
          );
        })}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onNewProject}
          disabled={disabled}
          className="h-auto shrink-0 gap-1.5 rounded-full px-3 py-1.5 text-[12.5px] font-medium text-muted"
        >
          <FolderPlus className="size-3.5" />
          {t('newSession.newProject')}
        </Button>
      </div>
    </div>
  );
};
