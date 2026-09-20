import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Compass, Download, Info, Loader2, Lock, Plus, RefreshCw, Search, Terminal, WandSparkles } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type { SkillBrief, SkillCheckItem, SkillScope, WorkbenchProject } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { Button } from '../ui/button';
import { SegmentedRadio } from '../ui/segmented';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { CapabilityTabs } from './CapabilityTabs';
import { SkillRow } from './skills/SkillRow';
import { SkillDetailPanel } from './skills/SkillDetailPanel';
import { ProjectPicker } from './skills/ProjectPicker';
import { AddSkillDialog } from './skills/AddSkillDialog';
import { BrowseRegistryDialog } from './skills/BrowseRegistryDialog';
import { errorMessage } from '@/lib/errorMessage';
import { Badge } from '../ui/badge';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';

const skillKey = (s: SkillBrief) => `${s.scope}:${s.name}`;

export const SkillsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canManage = capabilities.can_use_skills;

  const [scope, setScope] = useState<SkillScope>('global');
  const [projects, setProjects] = useState<WorkbenchProject[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [skills, setSkills] = useState<SkillBrief[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notInstalled, setNotInstalled] = useState(false);
  const [installingAskill, setInstallingAskill] = useState(false);
  const [projectNoFolder, setProjectNoFolder] = useState(false);
  const [search, setSearch] = useState('');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showBrowse, setShowBrowse] = useState(false);
  const [checkMap, setCheckMap] = useState<Record<string, SkillCheckItem>>({});
  const [updating, setUpdating] = useState(false);

  const activeProject = projects.find((p) => p.id === projectId) ?? null;
  // A folderless project can't hold project-scoped skills (askill needs a real
  // cwd), so the add/browse flows treat it as global-only: the add dialog drops
  // the project-scope option, and browse installs land in global.
  const projectHasFolder = Boolean(activeProject?.capabilities?.has_folder ?? activeProject?.folder_path);
  const projectCanManage = canManage && (scope !== 'project' || Boolean(activeProject?.capabilities?.can_chat));
  const addDialogProjectId = scope === 'project' && projectCanManage && projectHasFolder ? activeProject?.id : undefined;
  const browseScope: SkillScope = scope === 'project' && projectHasFolder ? 'project' : 'global';
  const browseProjectId = browseScope === 'project' ? activeProject?.id : undefined;

  useEffect(() => {
    api
      .listProjects()
      .then((res) => {
        setProjects(res.projects);
        setProjectId((prev) => prev ?? res.projects[0]?.id ?? null);
      })
      .catch(() => undefined);
  }, [api]);

  const listReq = useRef(0);
  const refresh = useCallback(async () => {
    // Global tab → just global skills. Project tab → list everything for the
    // project (cwd) so we can split project-local vs inherited-from-global.
    if (scope === 'project' && !projectId) {
      setSkills([]);
      return;
    }
    // Token guard: a slower list for a previous project/scope must not land
    // after a faster one for the current selection and replace its rows.
    const reqId = (listReq.current += 1);
    setLoading(true);
    setError(null);
    setNotInstalled(false);
    setProjectNoFolder(false);
    try {
      const res = await api.listSkills(
        scope === 'global' ? { scope: 'global' } : { scope: 'all', projectId: projectId ?? undefined },
      );
      if (reqId !== listReq.current) return; // superseded by a newer refresh
      if (res.ok) {
        setSkills(res.skills ?? []);
        setProjectNoFolder(Boolean(res.project_no_folder));
      } else if (res.error?.code === 'askill_not_found') {
        setNotInstalled(true);
        setSkills([]);
      } else {
        setError(res.error?.message ?? 'Failed to list skills');
        setSkills([]);
      }
    } catch (err) {
      if (reqId === listReq.current) setError(errorMessage(err) ?? String(err));
    } finally {
      if (reqId === listReq.current) setLoading(false);
    }
  }, [api, scope, projectId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => api.connectWorkbenchEvents({ onAuthorizationChanged: () => refresh() }), [api, refresh]);

  // Fetch update status (askill check) once the list loads so rows can show an
  // "update available" badge. Best-effort; failures just clear it.
  useEffect(() => {
    if (!canManage || notInstalled || (scope === 'project' && !projectId)) {
      setCheckMap({});
      return;
    }
    let cancelled = false;
    // Project view lists project-local AND inherited-global rows, so check both
    // scopes and merge — otherwise inherited globals never get an update badge.
    const scopes = scope === 'global' ? (['global'] as const) : (['global', 'project'] as const);
    Promise.all(
      scopes.map((s) =>
        api
          .checkSkills({ scope: s, projectId: s === 'project' ? projectId ?? undefined : undefined })
          .catch(() => null),
      ),
    )
      .then((resList) => {
        if (cancelled) return;
        const map: Record<string, SkillCheckItem> = {};
        for (const res of resList) for (const item of res?.skills ?? []) map[`${item.scope}:${item.name}`] = item;
        setCheckMap(map);
      })
      .catch(() => {
        if (!cancelled) setCheckMap({});
      });
    return () => {
      cancelled = true;
    };
  }, [api, scope, projectId, skills, notInstalled, canManage]);

  const matches = useCallback(
    (skill: SkillBrief) => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return skill.name.toLowerCase().includes(q) || (skill.description ?? '').toLowerCase().includes(q);
    },
    [search],
  );

  const filtered = useMemo(() => skills.filter(matches), [skills, matches]);
  const projectLocal = useMemo(() => filtered.filter((s) => s.scope === 'project'), [filtered]);
  const inheritedGlobal = useMemo(() => filtered.filter((s) => s.scope === 'global'), [filtered]);
  const selected = useMemo(() => skills.find((s) => skillKey(s) === selectedKey) ?? null, [skills, selectedKey]);
  // In Project scope `skills` also carries inherited-global rows; only
  // project-local installs should mark a registry result as already installed,
  // so users can still add a project-local copy of a globally installed skill.
  const installedNames = useMemo(
    () => new Set(skills.filter((s) => scope !== 'project' || s.scope === 'project').map((s) => s.name)),
    [skills, scope],
  );

  const onRemove = async () => {
    if (!selected) return;
    if (!window.confirm(t('skills.removeConfirm', { name: selected.name }))) return;
    try {
      const projectArg = selected.scope === 'project' ? projectId ?? undefined : undefined;
      const res = await api.removeSkill(selected.name, { scope: selected.scope, projectId: projectArg });
      if (res.ok) {
        showToast(t('skills.removeSuccess', { name: selected.name }), 'success');
        setSelectedKey(null);
        await refresh();
      } else {
        showToast(res.error?.message ?? selected.name, 'error');
      }
    } catch (err) {
      showToast(errorMessage(err) ?? String(err), 'error');
    }
  };

  const onUpdate = async () => {
    if (!selected) return;
    setUpdating(true);
    try {
      const projectArg = selected.scope === 'project' ? projectId ?? undefined : undefined;
      const res = await api.updateSkill(selected.name, { scope: selected.scope, projectId: projectArg });
      if (res.ok) {
        showToast(t('skills.updateSuccess', { name: selected.name }), 'success');
        await refresh();
      } else {
        showToast(res.error?.message ?? selected.name, 'error');
      }
    } catch (err) {
      showToast(errorMessage(err) ?? String(err), 'error');
    } finally {
      setUpdating(false);
    }
  };

  const afterDialog = () => {
    setShowAdd(false);
    setShowBrowse(false);
    refresh();
  };

  const renderRows = (list: SkillBrief[], inherited?: boolean) =>
    list.map((skill) => (
      <SkillRow
        key={skillKey(skill)}
        skill={skill}
        inherited={inherited}
        updateAvailable={checkMap[skillKey(skill)]?.status === 'update_available'}
        selected={selectedKey === skillKey(skill)}
        onSelect={() => setSelectedKey(skillKey(skill))}
      />
    ));

  const sectionLabel = (icon: React.ReactNode, label: string, hint?: string) => (
    <div className="flex items-center gap-2 px-1">
      {icon}
      <span className="font-mono text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">{label}</span>
      {hint ? <span className="font-mono text-[10px] text-muted">· {hint}</span> : null}
    </div>
  );

  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-5 py-2">
      <CapabilityTabs />
      <WorkbenchPageHeader
        icon={<WandSparkles className="size-5" />}
        title={t('skills.title')}
        subtitle={t('skills.subtitle')}
        actions={
          <Button type="button" variant="outline" size="xs" onClick={() => refresh()} disabled={loading}>
            <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
            {t('common.refresh')}
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2.5">
        <SegmentedRadio<SkillScope>
          value={scope}
          onChange={setScope}
          ariaLabel={t('skills.scopeGlobal')}
          options={[
            { id: 'global', label: t('skills.scopeGlobal') },
            { id: 'project', label: t('skills.scopeProject') },
          ]}
        />
        {scope === 'project' && projects.length > 0 ? (
          <ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />
        ) : null}

        <div className="flex min-w-[220px] flex-1 items-center gap-2 rounded-md border border-border-strong bg-surface px-3 py-2">
          <Search className="size-3.5 shrink-0 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t(scope === 'project' ? 'skills.searchProjectPlaceholder' : 'skills.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
          />
        </div>

        {projectCanManage ? (
          <>
            <Button type="button" variant="outline" size="xs" onClick={() => setShowBrowse(true)}>
              <Compass className="size-3.5 text-cyan-ink" />
              {t('skills.browseRegistry')}
            </Button>
            <Button type="button" variant="brand" size="xs" onClick={() => setShowAdd(true)}>
              <Plus />
              {t('skills.addSkill')}
            </Button>
          </>
        ) : !capabilities.can_use_skills ? (
          <Badge variant="secondary" title={t('skills.remoteReadOnlyHint')}>
            <Lock className="size-3" />
            {t('skills.remoteReadOnly')}
          </Badge>
        ) : null}
      </div>

      {scope === 'project' && activeProject?.folder_path ? (
        <div className="flex items-center gap-2 rounded-[10px] border border-border-strong bg-surface-2 px-3.5 py-2.5">
          <span className="truncate font-mono text-[10.5px] text-muted">{activeProject.folder_path}/.agents/skills</span>
          <div className="flex-1" />
          <Info className="size-3 text-muted" />
          <span className="text-[11px] text-muted">{t('skills.globalAlsoAvailable')}</span>
        </div>
      ) : null}

      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">{error}</div>
      ) : null}

      {notInstalled ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
          <Terminal className="size-7 text-muted" />
          <div className="text-[14px] font-semibold text-foreground">{t('skills.notInstalled')}</div>
          <div className="max-w-md font-mono text-[11.5px] text-muted">{t('skills.notInstalledHint')}</div>
          {projectCanManage ? <Button
            variant="brand"
            size="sm"
            className="mt-1"
            disabled={installingAskill}
            onClick={async () => {
              setInstallingAskill(true);
              try {
                const res = await api.installDependency('askill');
                if (res.ok) {
                  setNotInstalled(false);
                  await refresh();
                } else {
                  showToast(res.message || t('skills.installFailed'), 'error');
                }
              } catch (err) {
                showToast(errorMessage(err) || t('skills.installFailed'), 'error');
              } finally {
                setInstallingAskill(false);
              }
            }}
          >
            {installingAskill ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
            {installingAskill ? t('skills.installing') : t('skills.installAskill')}
          </Button> : null}
        </div>
      ) : (
        // `minmax(0,1fr)` (not bare `1fr`) lets the list column shrink below its
        // content's intrinsic width; otherwise a long row keeps the column from
        // shrinking and pushes the fixed detail card past the viewport edge. The
        // list column itself also needs `min-w-0` for the same reason.
        <div className={clsx('grid gap-5', selected ? 'lg:grid-cols-[minmax(0,1fr)_400px]' : 'grid-cols-1')}>
          <div className={clsx('flex min-w-0 flex-col gap-4', selected && 'max-lg:hidden')}>
            {scope === 'global' ? (
              <div className="flex flex-col gap-2">{renderRows(filtered)}</div>
            ) : (
              <>
                {projectNoFolder ? (
                  <div className="rounded-xl border border-gold/30 bg-gold/[0.06] px-4 py-3 text-[12px] text-muted">
                    {t('skills.projectNoFolder')}
                  </div>
                ) : null}
                <div className="flex flex-col gap-2">
                  {sectionLabel(
                    <WandSparkles className="size-3.5 text-mint-ink" />,
                    t('skills.projectSectionLocal'),
                    t('skills.projectSectionLocalHint'),
                  )}
                  {projectLocal.length > 0 ? (
                    <div className="flex flex-col gap-2">{renderRows(projectLocal)}</div>
                  ) : (
                    <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center text-[12px] text-muted">
                      {t('skills.empty')}
                    </div>
                  )}
                </div>
                {inheritedGlobal.length > 0 ? (
                  <div className="flex flex-col gap-2">
                    {sectionLabel(
                      <Info className="size-3.5 text-muted" />,
                      t('skills.projectSectionGlobal'),
                      t('skills.projectSectionGlobalHint'),
                    )}
                    <div className="flex flex-col gap-2">{renderRows(inheritedGlobal, true)}</div>
                  </div>
                ) : null}
              </>
            )}

            {!loading && filtered.length === 0 && scope === 'global' ? (
              <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-[12px] text-muted">
                {skills.length === 0 ? t('skills.empty') : t('skills.noSearchMatch')}
              </div>
            ) : null}
          </div>

          {selected ? (
            <SkillDetailPanel
              skill={selected}
              projectName={activeProject?.display_name}
              check={checkMap[skillKey(selected)]}
              updating={updating}
              onClose={() => setSelectedKey(null)}
              onUpdate={onUpdate}
              onRemove={onRemove}
              canManage={canManage && (selected.scope !== 'project' || Boolean(activeProject?.capabilities?.can_chat))}
            />
          ) : null}
        </div>
      )}

      {projectCanManage && showAdd ? (
        <AddSkillDialog
          defaultScope={scope}
          projectId={addDialogProjectId}
          projectName={addDialogProjectId ? activeProject?.display_name : undefined}
          onClose={() => setShowAdd(false)}
          onInstalled={afterDialog}
        />
      ) : null}
      {projectCanManage && showBrowse ? (
        <BrowseRegistryDialog
          scope={browseScope}
          projectId={browseProjectId}
          installedNames={installedNames}
          onClose={() => setShowBrowse(false)}
          onInstalled={afterDialog}
        />
      ) : null}
    </div>
  );
};
