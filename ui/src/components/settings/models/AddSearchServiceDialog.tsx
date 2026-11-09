import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import type { CollectionReadAuthority } from './collectionReadAuthority';
import type { SourceCreated } from './modelsApi';
import { keylessEndpoint, searchServicesActive, serviceDefaultEnabled, serviceSettings } from './searchServices';
import { SearchServiceNotes, SearchServicePrice, SearchVendorFacts, SearchVendorGlyph } from './SearchServiceParts';
import type { HostedWebFetchToolStatus, HostedWebSearchToolStatus, Source } from './types';

/** Both entry points use this catalog grid and the existing nonce-safe Source create journey. */
export const AddSearchServiceDialog: React.FC<{
  tool: HostedWebSearchToolStatus;
  fetchTool?: HostedWebFetchToolStatus;
  pending: boolean;
  sourceReads: CollectionReadAuthority<Source[]>;
  onClose: () => void;
  onAdded: (created: SourceCreated) => void;
  onOpenSource: (sourceId: string) => void;
  onViewOrder: () => void;
  onEnableService?: (sourceId: string) => void;
}> = ({ tool, fetchTool, pending, sourceReads, onClose, onAdded, onOpenSource, onViewOrder, onEnableService }) => {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const selected = tool.service_vendors?.find((vendor) => vendor.id === selectedId);
  if (!searchServicesActive(tool)) return null;
  if (selected) return <AddApiKeyDialog key={selected.id} open serviceVendor={selected} sourceReads={sourceReads} onClose={() => setSelectedId(null)} onAdded={onAdded} />;
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent className="max-w-2xl" closeLabel={t('common.close')}>
      <DialogHeader>
        <DialogTitle>{t(fetchTool ? 'settings.models.webFetch.dialogTitle' : 'settings.models.searchServices.add')}</DialogTitle>
        <DialogDescription>{t(fetchTool ? 'settings.models.webFetch.dialogDescription' : 'settings.models.searchServices.dialogDescription')}</DialogDescription>
      </DialogHeader>
      <div className="grid gap-3 sm:grid-cols-2">
        {tool.service_vendors?.map((vendor) => {
          const vendorName = t(`settings.models.searchServices.vendorName.${vendor.id}`, { defaultValue: vendor.name }) as string;
          const builtIn = vendor.facts.built_in === true;
          const added = vendor.source_ids.length > 0;
          const endpoint = keylessEndpoint(vendor, vendor.source_ids[0] ?? '');
          const enabled = vendor.source_ids.some((id) => serviceSettings(tool, vendor, id).enabled);
          const state = vendor.facts.tool_only ? 'added' : enabled ? 'inOrder' : 'addedUnused';
          return <section key={vendor.id} aria-label={vendorName} className="flex min-w-0 flex-col gap-3 rounded-xl border border-edge bg-surface p-4 shadow-elevation-0">
            <div className="flex items-center gap-3"><SearchVendorGlyph vendor={vendor.id} /><h3 className="min-w-0 flex-1 font-semibold text-foreground">{vendorName}</h3></div>
            {fetchTool && <span className="flex gap-2"><span className="model-hub-pill model-hub-accent-pill--cyan border">{t('settings.models.webFetch.searchBadge')}</span>{fetchTool.service_vendors.some((item) => item.id === vendor.id) && <span className="model-hub-pill model-hub-accent-pill--cyan border">{t('settings.models.webFetch.fetchBadge')}</span>}</span>}
            <SearchVendorFacts vendor={vendor} />
            {builtIn && endpoint && <p className="model-hub-tool-note">{t('settings.models.searchServices.keylessQueryDestination', { endpoint })}</p>}
            <SearchServicePrice price={vendor.price} />
            {builtIn ? <>
              <span className="model-hub-pill model-hub-accent-pill--neutral self-start border">{t('settings.models.searchServices.builtIn')}</span>
              {!serviceDefaultEnabled(vendor) && !tool.executor_candidates.some((row) => vendor.source_ids.includes(row.source_id))
                ? <Button variant="outline" size="sm" disabled={pending} onClick={() => onEnableService?.(vendor.source_ids[0])}>{t('settings.models.searchServices.use')}</Button>
                : <Button variant="outline" size="sm" disabled={pending} onClick={onViewOrder}>{t(fetchTool ? 'settings.models.webFetch.viewOrder' : 'settings.models.searchServices.viewOrder')}<ChevronRight aria-hidden /></Button>}
            </> : added ? <>
              <span className="model-hub-pill model-hub-accent-pill--neutral self-start border">{t(`settings.models.searchServices.${state}`)}</span>
              <Button variant="outline" size="sm" disabled={pending} onClick={() => onOpenSource(vendor.source_ids.find((id) => !serviceSettings(tool, vendor, id).enabled) ?? vendor.source_ids[0])}>{t('settings.models.searchServices.openDetail', { vendor: vendorName })}</Button>
            </> : <Button variant="outline" size="sm" disabled={pending} onClick={() => setSelectedId(vendor.id)}>{t('settings.models.searchServices.addVendor', { vendor: vendorName })}<ChevronRight aria-hidden /></Button>}
          </section>;
        })}
        {fetchTool?.service_vendors.filter((vendor) => vendor.facts.built_in && !tool.service_vendors?.some((search) => search.id === vendor.id)).map((vendor) => {
          const name = t(`settings.models.webFetch.vendorName.${vendor.id}`, { defaultValue: vendor.name });
          return <section key={vendor.id} aria-label={name} className="flex min-w-0 flex-col gap-3 rounded-xl border border-edge bg-surface p-4 shadow-elevation-0">
            <div className="flex items-center gap-3"><SearchVendorGlyph vendor={vendor.id} /><h3 className="font-semibold">{name}</h3></div>
            <span className="model-hub-pill model-hub-accent-pill--cyan self-start border">{t('settings.models.webFetch.fetchBadge')}</span>
            <p className="model-hub-tool-note">{t(`settings.models.webFetch.row.${vendor.id}`, { defaultValue: vendor.name })}</p>
            <span className="model-hub-pill model-hub-accent-pill--neutral self-start border">{t('settings.models.searchServices.builtIn')}</span>
            <Button variant="outline" size="sm" disabled={pending} onClick={onViewOrder}>{t('settings.models.webFetch.viewOrder')}<ChevronRight aria-hidden /></Button>
          </section>;
        })}
      </div>
      <SearchServiceNotes />
      <p className="model-hub-tool-note">{t('settings.models.searchServices.priceNote')}</p>
    </DialogContent>
  </Dialog>;
};
