import { useState } from 'react';
import { Check, ChevronDown, Folder, FolderGit2 } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import type { WorkbenchProject } from '../../../context/ApiContext';
import { Popover, PopoverContent, PopoverTrigger } from '../../ui/popover';

export interface ProjectPickerProps {
  projects: WorkbenchProject[];
  value: string | null;
  onChange: (projectId: string) => void;
}

/** Dropdown that switches which project's skills are being managed. Mirrors the
 *  open-state in design.pen — the project list is the workbench sidebar's. */
export function ProjectPicker({ projects, value, onChange }: ProjectPickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const active = projects.find((p) => p.id === value) ?? null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 rounded-md border border-mint/40 bg-surface px-3 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
        >
          <FolderGit2 className="size-3.5 text-cyan-ink" />
          <span className="max-w-[160px] truncate">{active?.display_name ?? t('skills.scopeProject')}</span>
          <ChevronDown className="size-3 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[280px] p-1.5">
        <div className="px-2 py-1 font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-muted">
          {t('skills.picker.title')}
        </div>
        {projects.map((project) => {
          const on = project.id === value;
          return (
            <button
              key={project.id}
              type="button"
              onClick={() => {
                onChange(project.id);
                setOpen(false);
              }}
              className={clsx(
                'flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-[12.5px] transition',
                on ? 'bg-mint-soft text-foreground' : 'text-foreground hover:bg-foreground/[0.04]',
              )}
            >
              <Folder className={clsx('size-3.5 shrink-0', on ? 'text-mint-ink' : 'text-muted')} />
              <span className="flex-1 truncate">{project.display_name}</span>
              {on ? <Check className="size-3.5 shrink-0 text-mint-ink" /> : null}
            </button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
}
