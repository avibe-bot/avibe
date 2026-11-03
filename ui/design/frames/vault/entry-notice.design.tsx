import { VaultEntryNotice } from '@/components/ui/vault-entry-notice';
import { frames } from '../../frame';
import { owner } from '../../shell/fixtures';

const designFrames = frames([
  {
    name: 'owner', title: 'Protected Vault · unsupported address · owner',
    width: 560, height: 300, status: 'shipped',
    fixtures: { bots: [], capabilities: owner },
    render: () => <div className="p-6"><VaultEntryNotice issue="address" /></div>,
  },
  {
    name: 'owner-phone', title: 'Protected Vault · unsupported address · phone',
    width: 390, height: 360, status: 'shipped',
    fixtures: { bots: [], capabilities: owner },
    render: () => <div className="p-4"><VaultEntryNotice issue="address" /></div>,
  },
  {
    name: 'member-phone', title: 'Protected Vault · unsupported address · member',
    width: 390, height: 360, status: 'shipped',
    fixtures: { bots: [], capabilities: { ...owner, can_manage_instance: false } },
    render: () => <div className="p-4"><VaultEntryNotice issue="address" /></div>,
  },
  {
    name: 'form-phone', title: 'Protected Vault · form navigation disclosure',
    width: 390, height: 420, status: 'shipped',
    fixtures: { bots: [], capabilities: owner },
    render: () => <div className="p-4"><VaultEntryNotice issue="address" warnDraftDiscard /></div>,
  },
  {
    name: 'browser', title: 'Protected Vault · unavailable browser context',
    width: 560, height: 300, status: 'shipped',
    fixtures: { bots: [], capabilities: owner },
    render: () => <div className="p-6"><VaultEntryNotice issue="browser" /></div>,
  },
]);

export default designFrames;
