import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import type { CollectionReadAuthority } from './collectionReadAuthority';
import type { SourceCreated } from './modelsApi';
import { searchServicesActive, serviceSettings } from './searchServices';
import { SearchServiceNotes, SearchServicePrice, SearchVendorFacts, SearchVendorGlyph } from './SearchServiceParts';
import type { HostedWebSearchToolStatus, Source } from './types';

/** Both entry points use this catalog grid and the existing nonce-safe Source create journey. */
export const AddSearchServiceDialog: React.FC<{
  tool: HostedWebSearchToolStatus;
  pending: boolean;
  sourceReads: CollectionReadAuthority<Source[]>;
  onClose: () => void;
  onAdded: (created: SourceCreated) => void;
  onOpenSource: (sourceId: string) => void;
}> = ({ tool, pending, sourceReads, onClose, onAdded, onOpenSource }) => {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const selected = tool.service_vendors?.find((vendor) => vendor.id === selectedId);
  if (!searchServicesActive(tool)) return null;
  if (selected) return <AddApiKeyDialog key={selected.id} open serviceVendor={selected} sourceReads={sourceReads} onClose={() => setSelectedId(null)} onAdded={onAdded} />;
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent className="max-w-2xl border-border-strong bg-surface" closeLabel={t('common.close')}>
      <DialogHeader>
        <DialogTitle>{t('settings.models.searchServices.add')}</DialogTitle>
        <DialogDescription>{t('settings.models.searchServices.dialogDescription')}</DialogDescription>
      </DialogHeader>
      <div className="grid gap-3 sm:grid-cols-2">
        {tool.service_vendors?.map((vendor) => {
          const vendorName = t(`settings.models.searchServices.vendorName.${vendor.id}`, { defaultValue: vendor.name }) as string;
          const builtIn = vendor.facts.built_in === true;
          const added = vendor.source_ids.length > 0;
          const enabled = vendor.source_ids.some((id) => serviceSettings(tool, vendor, id).enabled);
          const state = vendor.facts.tool_only ? 'added' : enabled ? 'inOrder' : 'addedUnused';
          return <section key={vendor.id} aria-label={vendorName} className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-background p-4">
            <div className="flex items-center gap-3"><SearchVendorGlyph vendor={vendor.id} /><h3 className="min-w-0 flex-1 font-semibold text-foreground">{vendorName}</h3></div>
            <SearchVendorFacts vendor={vendor} />
            <SearchServicePrice price={vendor.price} />
            {builtIn ? <>
              <span className="model-hub-pill model-hub-accent-pill--neutral self-start border">{t('settings.models.searchServices.builtIn')}</span>
              <Button variant="outline" size="sm" disabled={pending} onClick={() => onOpenSource(vendor.source_ids[0])}>{t('settings.models.searchServices.viewOrder')}<ChevronRight aria-hidden /></Button>
            </> : added ? <>
              <span className="model-hub-pill model-hub-accent-pill--neutral self-start border">{t(`settings.models.searchServices.${state}`)}</span>
              <Button variant="outline" size="sm" disabled={pending} onClick={() => onOpenSource(vendor.source_ids.find((id) => !serviceSettings(tool, vendor, id).enabled) ?? vendor.source_ids[0])}>{t('settings.models.searchServices.openDetail', { vendor: vendorName })}</Button>
            </> : <Button variant="outline" size="sm" disabled={pending} onClick={() => setSelectedId(vendor.id)}>{t('settings.models.searchServices.addVendor', { vendor: vendorName })}<ChevronRight aria-hidden /></Button>}
          </section>;
        })}
      </div>
      <SearchServiceNotes />
      <p className="model-hub-tool-note">{t('settings.models.searchServices.priceNote')}</p>
    </DialogContent>
  </Dialog>;
};
