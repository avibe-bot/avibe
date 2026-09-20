import { useMemo, useRef, useState } from 'react';
import { Download, Github, Loader2, PackageCheck, PackagePlus, Search, Terminal, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../../context/ApiContext';
import type { SkillDiscovered, SkillScope } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { Button } from '../../ui/button';
import { SegmentedRadio } from '../../ui/segmented';
import { Checkbox } from '../../ui/checkbox';
import { FileDropzone } from './FileDropzone';
import { errorMessage } from '@/lib/errorMessage';

export interface AddSkillDialogProps {
  defaultScope: SkillScope;
  projectId?: string;
  projectName?: string;
  onClose: () => void;
  onInstalled: () => void;
}

type SourceTab = 'github' | 'zip';

/** "Add a skill" modal — import from a GitHub URL/slug or an uploaded .zip. */
export function AddSkillDialog({ defaultScope, projectId, projectName, onClose, onInstalled }: AddSkillDialogProps) {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [source, setSource] = useState<SourceTab>('github');
  const [url, setUrl] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [uploadDir, setUploadDir] = useState<string | null>(null);
  const [discovered, setDiscovered] = useState<SkillDiscovered[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scope, setScope] = useState<SkillScope>(projectId ? defaultScope : 'global');
  const [busy, setBusy] = useState<'fetch' | 'install' | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Monotonic token: a slow preview that resolves after the source changed must
  // not repopulate `discovered` with skills from the previous source.
  const previewReq = useRef(0);

  const baseSource = source === 'github' ? url.trim() : uploadDir;

  const reset = () => {
    previewReq.current += 1;
    setDiscovered(null);
    setSelected(new Set());
    setError(null);
  };

  const onDiscovered = (skills: SkillDiscovered[]) => {
    setDiscovered(skills);
    setSelected(new Set(skills.map((s) => s.name)));
  };

  const fetchGithub = async () => {
    if (!url.trim()) return;
    const reqId = (previewReq.current += 1);
    setBusy('fetch');
    setError(null);
    try {
      const res = await api.previewSkillSource(url.trim(), { projectId });
      if (reqId !== previewReq.current) return; // a newer source superseded this
      if (res.ok && res.skills) onDiscovered(res.skills);
      else setError(res.error?.message ?? t('skills.addDialog.fetchFirst'));
    } catch (err) {
      if (reqId === previewReq.current) setError(errorMessage(err) ?? String(err));
    } finally {
      if (reqId === previewReq.current) setBusy(null);
    }
  };

  const onPickFile = async (picked: File | null) => {
    setFile(picked);
    reset();
    setUploadDir(null);
    if (!picked) return;
    const reqId = (previewReq.current += 1);
    setBusy('fetch');
    try {
      const res = await api.uploadSkillZip(picked, { projectId });
      if (reqId !== previewReq.current) return; // a newer pick/source superseded this
      if (res.ok && res.skills) {
        setUploadDir(res.dir ?? null);
        onDiscovered(res.skills);
      } else {
        setError(res.error?.message ?? t('skills.addDialog.fetchFirst'));
      }
    } catch (err) {
      if (reqId === previewReq.current) setError(errorMessage(err) ?? String(err));
    } finally {
      if (reqId === previewReq.current) setBusy(null);
    }
  };

  const toggleSkill = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const cliPreview = useMemo(() => {
    if (!baseSource) return '';
    const allSelected = discovered && selected.size === discovered.length;
    const selector = allSelected
      ? '--all'
      : selected.size === 1
        ? `--skill ${[...selected][0]}`
        : `--skill … (×${selected.size})`;
    return [
      'askill add',
      baseSource,
      '-a claude-code opencode codex',
      selector,
      scope === 'global' ? '-g' : '',
      '-y',
    ]
      .filter(Boolean)
      .join(' ');
  }, [baseSource, discovered, selected, scope]);

  const install = async () => {
    if (!baseSource || selected.size === 0) return;
    setBusy('install');
    setError(null);
    const targetProject = scope === 'project' ? projectId : undefined;
    const allSelected = discovered != null && selected.size === discovered.length;
    try {
      // askill installs mutate the same .agents/.skill-lock.json + symlink
      // targets, so a multi-skill subset must install sequentially — parallel
      // calls race and can drop part of the selection. (--skill is one name
      // per call; --all is a single call covering everything.)
      let failed: Awaited<ReturnType<typeof api.addSkill>> | null = null;
      if (allSelected) {
        const r = await api.addSkill({ source: baseSource, scope, projectId: targetProject, all: true });
        if (!r.ok) failed = r;
      } else {
        for (const name of selected) {
          // --skill keeps local-dir paths unambiguous (paths can contain '@').
          const r = await api.addSkill({ source: baseSource, skill: name, scope, projectId: targetProject });
          if (!r.ok) {
            failed = r;
            break;
          }
        }
      }
      if (failed) {
        setError(failed.error?.message ?? t('skills.addSkill'));
        return;
      }
      showToast(t('skills.addSuccess', { count: selected.size }), 'success');
      onInstalled();
    } catch (err) {
      setError(errorMessage(err) ?? String(err));
    } finally {
      setBusy(null);
    }
  };

  const scopeOptions = [
    { id: 'global' as const, label: t('skills.scopeGlobal') },
    { id: 'project' as const, label: projectName ? `${t('skills.scopeProject')}: ${projectName}` : t('skills.scopeProject') },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[#05050B]/[0.72] p-10 backdrop-blur-[6px]">
      <div className="flex w-full max-w-[560px] flex-col overflow-hidden rounded-2xl border border-border-strong bg-surface-2 shadow-[0_24px_60px_-12px_rgba(0,0,0,0.7)]">
        <div className="flex items-center gap-3 border-b border-border px-5 py-4">
          <span className="flex size-[34px] shrink-0 items-center justify-center rounded-[9px] border border-mint/40 bg-mint-soft text-mint-ink">
            <PackagePlus className="size-[17px]" />
          </span>
          <div className="flex flex-1 flex-col">
            <div className="text-[16px] font-bold text-foreground">{t('skills.addDialog.title')}</div>
            <div className="text-[11px] text-muted">{t('skills.addDialog.subtitle')}</div>
          </div>
          <button type="button" onClick={onClose} aria-label={t('common.close')} className="flex size-[26px] items-center justify-center rounded-md text-muted transition hover:bg-foreground/[0.06]">
            <X className="size-3.5" />
          </button>
        </div>

        <div className="flex flex-col gap-4 p-5">
          <SegmentedRadio<SourceTab>
            value={source}
            onChange={(next) => {
              setSource(next);
              reset();
            }}
            ariaLabel={t('skills.addDialog.title')}
            options={[
              { id: 'github', label: t('skills.addDialog.githubTab') },
              { id: 'zip', label: t('skills.addDialog.zipTab') },
            ]}
          />

          {source === 'github' ? (
            <label className="flex flex-col gap-1.5">
              <span className="font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{t('skills.addDialog.urlLabel')}</span>
              <div className="flex items-center gap-2 rounded-lg border border-border-strong bg-surface px-2.5 py-2">
                <Github className="size-3.5 shrink-0 text-muted" />
                <input
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value);
                    reset();
                  }}
                  onKeyDown={(e) => e.key === 'Enter' && fetchGithub()}
                  placeholder={t('skills.addDialog.urlPlaceholder')}
                  className="flex-1 bg-transparent font-mono text-[12px] text-foreground outline-none placeholder:text-muted"
                />
                <button
                  type="button"
                  onClick={fetchGithub}
                  disabled={!url.trim() || busy === 'fetch'}
                  className="flex shrink-0 items-center gap-1.5 rounded-md border border-cyan/40 bg-cyan-soft px-2.5 py-1.5 text-[11.5px] font-semibold text-cyan-ink transition hover:brightness-110 disabled:opacity-50"
                >
                  {busy === 'fetch' ? <Loader2 className="size-3 animate-spin" /> : <Search className="size-3" />}
                  {t('skills.addDialog.fetch')}
                </button>
              </div>
            </label>
          ) : (
            <div className="flex flex-col gap-1.5">
              <span className="font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{t('skills.addDialog.package')}</span>
              <FileDropzone
                file={file}
                onFile={onPickFile}
                hint={t('skills.addDialog.dropzoneHint')}
                replaceLabel={t('skills.addDialog.replace')}
                meta={file && discovered ? `${(file.size / 1024).toFixed(0)} KB · ${discovered.length} skills` : undefined}
              />
              <span className="text-[11px] text-muted">{t('skills.addDialog.localNote')}</span>
            </div>
          )}

          {discovered ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-1.5">
                <PackageCheck className="size-3.5 text-mint-ink" />
                <span className="font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-muted">
                  {t('skills.addDialog.found', { count: discovered.length })}
                </span>
              </div>
              {discovered.map((skill) => {
                const on = selected.has(skill.name);
                return (
                  <button
                    key={skill.name}
                    type="button"
                    role="checkbox"
                    aria-checked={on}
                    aria-label={skill.name}
                    onClick={() => toggleSkill(skill.name)}
                    className={clsx(
                      'flex items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition',
                      on ? 'border-mint/40 bg-mint-soft' : 'border-border bg-surface hover:border-border-strong',
                    )}
                  >
                    <Checkbox checked={on} presentational />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="text-[13px] font-semibold text-foreground">{skill.name}</span>
                      {skill.description ? <span className="truncate text-[11px] text-muted">{skill.description}</span> : null}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : null}

          <div className="flex flex-col gap-1.5">
            <span className="font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{t('skills.addDialog.installTo')}</span>
            <SegmentedRadio<SkillScope> value={scope} onChange={setScope} ariaLabel={t('skills.addDialog.installTo')} options={scopeOptions} disabled={!projectId} />
          </div>

          {error ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">{error}</div>
          ) : null}
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-border bg-surface-3 px-5 py-3.5">
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            <Terminal className="size-3 shrink-0 text-muted" />
            <span className="truncate font-mono text-[10.5px] text-muted">{cliPreview || 'askill add …'}</span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button type="button" onClick={onClose} className="rounded-lg border border-border-strong px-3.5 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]">
              {t('skills.addDialog.cancel')}
            </button>
            <Button
              type="button"
              variant="brand"
              size={null}
              onClick={install}
              disabled={!discovered || selected.size === 0 || busy === 'install'}
              className="gap-1.5 px-4 py-2 text-[12px] font-semibold"
            >
              {busy === 'install' ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
              {busy === 'install' ? t('skills.addDialog.installing') : t('skills.addDialog.install', { count: selected.size })}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
