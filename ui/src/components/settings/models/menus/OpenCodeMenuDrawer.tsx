// 模型菜单 · OpenCode — the open-menu drawer (frame 05r). Models are grouped BY
// provider prefix (one taxonomy); the full identifier is visible by construction
// (group prefix + mono row id), friendly name secondary. 精选/全量 toggle,
// checkbox = appears in OpenCode's picker, colored dots = supplying sources,
// custom rows carry a 自定义 badge + edit. Commits via PUT .../menu on 完成.
import * as React from 'react';
import { Pencil, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { SegmentedRadio } from '@/components/ui/segmented';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from '../modelsApi';
import { backendVisual } from '../vendorMeta';
import type { AgentMenu, AgentSupply, Source } from '../types';
import { MenuDrawer } from './MenuDrawer';
import { AddCustomModelDialog } from './AddCustomModelDialog';
import { buildMenuGroups, isSourceEligible, type MenuModelRow } from './identifiers';
import { SupplyDots } from './supplyBits';

type EditTarget = { sourceId: string; modelId: string; displayName: string | null } | null;

const sameSet = (a: Set<string>, b: string[]): boolean =>
  a.size === b.length && b.every((x) => a.has(x));

const ModelRow: React.FC<{
  row: MenuModelRow;
  checked: boolean;
  onToggle: () => void;
  onEdit: () => void;
}> = ({ row, checked, onToggle, onEdit }) => {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border px-3.5 py-3">
      <Checkbox checked={checked} onCheckedChange={onToggle} label={row.identifier} />
      <button type="button" onClick={onToggle} className="flex min-w-0 flex-1 items-center gap-2.5 text-left">
        <span className="font-mono text-[14px] font-medium text-foreground">{row.modelId}</span>
        {row.displayName && <span className="truncate text-[13px] text-muted">{row.displayName}</span>}
        {row.isCustom && (
          <Badge className="shrink-0 rounded-md bg-violet-soft px-2 py-0.5 text-[10px] font-medium text-violet">
            {t('settings.models.menus.opencode.customBadge')}
          </Badge>
        )}
      </button>
      <SupplyDots accents={row.accents} className="flex shrink-0 items-center gap-1" />
      {row.isCustom && (
        <button
          type="button"
          aria-label={t('common.edit') as string}
          onClick={onEdit}
          className="shrink-0 rounded-md p-1 text-muted transition-colors hover:text-foreground"
        >
          <Pencil className="size-3.5" />
        </button>
      )}
    </div>
  );
};

export const OpenCodeMenuDrawer: React.FC<{
  open: boolean;
  agent: AgentSupply;
  sources: Source[];
  onClose: () => void;
  onSaved: () => void;
  /** Re-fetch sources after a custom model is added/edited. */
  onRefresh: () => void;
}> = ({ open, agent, sources, onClose, onSaved, onRefresh }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { Icon, accent } = backendVisual('opencode');

  // Standard OpenCode vendor prefixes, server-populated (agent-supply v1.2) so
  // identifiers byte-match the backend's opencode_model_id — never hand-mirrored.
  const standardVendors = React.useMemo(() => new Set(agent.standard_vendors ?? []), [agent.standard_vendors]);

  // Only OpenCode-eligible sources materialize as providers (isSourceEligible):
  // API-key sources only — subscriptions (native_cli AND hub-held experimental)
  // are excluded, matching the backend predicate, so we never offer a row the
  // live `set_opencode_menu` would reject.
  const eligibleSources = React.useMemo(() => sources.filter((s) => isSourceEligible(s, 'opencode')), [sources]);
  const groups = React.useMemo(() => buildMenuGroups(eligibleSources, standardVendors), [eligibleSources, standardVendors]);
  const allRows = React.useMemo(() => groups.flatMap((g) => g.rows), [groups]);

  const [view, setView] = React.useState<'featured' | 'full'>(agent.menu?.view ?? 'featured');
  const [checked, setChecked] = React.useState<Set<string>>(() => new Set(agent.menu?.checked ?? []));
  const initialRef = React.useRef<AgentMenu>({ view, checked: [...checked] });
  const [saving, setSaving] = React.useState(false);
  const [customOpen, setCustomOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<EditTarget>(null);

  React.useEffect(() => {
    if (!open) return;
    const v = agent.menu?.view ?? 'featured';
    const c = agent.menu?.checked ?? [];
    setView(v);
    setChecked(new Set(c));
    initialRef.current = { view: v, checked: [...c] };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const featuredCount = allRows.filter((r) => checked.has(r.identifier)).length;
  const fullCount = allRows.length;

  const toggle = (identifier: string) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(identifier)) next.delete(identifier);
      else next.add(identifier);
      return next;
    });

  const dirty = () => view !== initialRef.current.view || !sameSet(checked, initialRef.current.checked);

  const commitAndClose = async () => {
    if (saving) return;
    if (!dirty()) {
      onClose();
      return;
    }
    setSaving(true);
    try {
      await modelsApi.putMenu({ view, checked: [...checked] });
      onSaved();
      onClose();
    } catch {
      showToast(t('settings.models.menus.saveFailed') as string, 'error');
    } finally {
      setSaving(false);
    }
  };

  const providerAnnotation = (provider: string) =>
    t(`settings.models.menus.opencode.provider.${provider}`, { defaultValue: '' }) as string;

  const visibleGroups = groups
    .map((g) => ({
      ...g,
      rows: view === 'featured' ? g.rows.filter((r) => checked.has(r.identifier)) : g.rows,
    }))
    .filter((g) => g.rows.length > 0);

  return (
    <>
      <MenuDrawer
        open={open}
        onClose={() => void commitAndClose()}
        Icon={Icon}
        accent={accent}
        title={t('settings.models.menus.opencode.title') as string}
        subtitle={t('settings.models.menus.opencode.subtitle') as string}
        footer={
          <>
            <span />
            <Button variant="brand" size="sm" onClick={() => void commitAndClose()} disabled={saving}>
              {t('settings.models.menus.done')}
            </Button>
          </>
        }
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="w-[220px]">
            <SegmentedRadio
              value={view}
              onChange={setView}
              ariaLabel={t('settings.models.menus.opencode.viewLabel') as string}
              tone="muted"
              options={[
                { id: 'featured', label: t('settings.models.menus.opencode.featured', { count: featuredCount }) as string },
                { id: 'full', label: t('settings.models.menus.opencode.full', { count: fullCount }) as string },
              ]}
            />
          </div>
          <Button
            variant="outline"
            size="sm"
            className="border-violet/40 text-violet hover:border-violet/60 hover:text-violet"
            onClick={() => {
              setEditTarget(null);
              setCustomOpen(true);
            }}
          >
            <Plus className="size-4" />
            {t('settings.models.menus.opencode.addCustom')}
          </Button>
        </div>

        {visibleGroups.length === 0 ? (
          <p className="px-1 py-8 text-center text-[13px] text-muted">{t('settings.models.menus.opencode.empty')}</p>
        ) : (
          <div className="flex flex-col gap-5">
            {visibleGroups.map((group) => {
              const annotation = providerAnnotation(group.provider);
              return (
                <div key={group.provider} className="flex flex-col gap-2">
                  <div className="flex items-baseline gap-2 px-1">
                    <span className="font-mono text-[13px] font-bold text-violet">{group.provider}/</span>
                    {annotation && <span className="text-[12px] text-muted">{annotation}</span>}
                  </div>
                  <div className="flex flex-col gap-2">
                    {group.rows.map((row) => (
                      <ModelRow
                        key={row.identifier}
                        row={row}
                        checked={checked.has(row.identifier)}
                        onToggle={() => toggle(row.identifier)}
                        onEdit={() => {
                          // Pull the display name from the CHOSEN manual model, not the
                          // aggregate row label (which may come from a discovered source),
                          // so saving unchanged can't overwrite it with another source's name.
                          const custom = row.sources.find((s) => s.models.some((m) => m.id === row.modelId && m.provenance === 'manual'));
                          const manual = custom?.models.find((m) => m.id === row.modelId && m.provenance === 'manual');
                          setEditTarget({ sourceId: (custom ?? row.sources[0]).id, modelId: row.modelId, displayName: manual?.display_name ?? null });
                          setCustomOpen(true);
                        }}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <p className="mt-5 px-1 text-[12px] leading-relaxed text-muted">
          {t('settings.models.menus.opencode.footnote')}
        </p>
      </MenuDrawer>

      <AddCustomModelDialog
        open={customOpen}
        sources={eligibleSources}
        standardVendors={standardVendors}
        edit={editTarget}
        onClose={() => setCustomOpen(false)}
        onSaved={(identifier) => {
          // Auto-check only a NEWLY added model (so it shows in 精选). Editing an
          // existing entry's metadata must not flip its menu-selection state — a
          // display-name edit on an unchecked model would otherwise re-add it.
          if (!editTarget) setChecked((prev) => new Set(prev).add(identifier));
          onRefresh();
        }}
        onDeleted={(identifier) => {
          // Drop the removed model from the menu selection so 完成 doesn't try to
          // re-check a now-nonexistent identifier (set_opencode_menu would reject).
          setChecked((prev) => {
            const next = new Set(prev);
            next.delete(identifier);
            return next;
          });
          onRefresh();
        }}
      />
    </>
  );
};
