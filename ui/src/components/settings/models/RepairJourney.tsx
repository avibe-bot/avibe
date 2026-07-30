// The page-level host for the two repairs that need more than a menu click: a
// native/hub subscription re-login and an API-key replacement.
//
// Why the page hosts them rather than the row: both open a MODAL over the whole
// page, and a modal owned by a list item dies when the list re-renders under it —
// which is exactly what happens here, because a repair's first act is to change
// the row that started it (`mark_native_irreversible_start` writes 需处理 before
// the login page even loads).
//
// That is also why `target.source` is a SNAPSHOT, never a live lookup: the dialog
// must keep naming the source the user clicked, with the display name it had, for
// the whole journey. The page holds the object it was handed; it does not re-find
// it in the refreshed list.
//
// The re-auth confirm is UNCONDITIONAL (AC-13): the server refuses the route
// without `acknowledge_irreversible`, and the reason is that the old login stops
// working the moment the flow starts — whether or not this one ends in a success.
// So the question is asked before anything is sent, every time, with no
// remember-my-choice and no skip for a source that is already broken.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { ReplaceKeyDialog } from './ReplaceKeyDialog';
import type { RaisedRepair } from './SourceRowMenu';
import type { Source } from './types';

export type RepairTarget = { source: Source; kind: RaisedRepair };

export const RepairJourney: React.FC<{
  /** The row + remedy the user picked. A snapshot — see the note above. Null closes everything. */
  target: RepairTarget | null;
  onClose: () => void;
  /** Something committed server-side: re-fetch sources + agents. */
  onChanged: () => void;
}> = ({ target, onClose, onChanged }) => {
  const { t } = useTranslation();
  // The confirm and the flow are two dialogs over one target, so which of them is
  // showing is this component's only state.
  const [acknowledged, setAcknowledged] = React.useState(false);

  const reauthTarget = target?.kind === 'reauth' ? target.source : null;
  const replaceTarget = target?.kind === 'replace_key' ? target.source : null;

  // A new target always starts at the question again.
  React.useEffect(() => {
    setAcknowledged(false);
  }, [target?.source.id, target?.kind]);

  const closeAll = () => {
    setAcknowledged(false);
    onClose();
  };

  return (
    <>
      <ConfirmDialog
        open={reauthTarget !== null && !acknowledged}
        onOpenChange={(v) => !v && closeAll()}
        title={t('settings.models.repair.reauthTitle', { name: reauthTarget?.display_name ?? '' })}
        description={t('settings.models.repair.reauthBody')}
        confirmLabel={t('settings.models.repair.reauthConfirm') as string}
        onConfirm={() => setAcknowledged(true)}
      />

      <OAuthConnectDialog
        open={reauthTarget !== null && acknowledged}
        // The source's own vendor: a re-login goes back to the account it has, and
        // the flow's presentation comes from the runtime either way.
        vendor={reauthTarget?.vendor ?? 'anthropic'}
        reauth={reauthTarget}
        // Refresh on the way out WHATEVER happened: the reauth route has already
        // written 需处理 and cleared the discovered models by the time this dialog
        // is on screen, so a cancelled or failed login leaves the page stale in a
        // way the user needs to see.
        onClose={() => {
          onChanged();
          closeAll();
        }}
        onConnected={onChanged}
      />

      <ReplaceKeyDialog source={replaceTarget} onClose={closeAll} onReplaced={onChanged} />
    </>
  );
};

export default RepairJourney;
