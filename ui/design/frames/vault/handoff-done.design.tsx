// The line a browser tab shows once the step the desktop app handed to it is done there
// (`from=desktop` on the Vaults URL). It sits above the pending requests on the Vaults page.
import { VaultHandoffDone } from '@/components/ui/vault-handoff-done';
import { frames } from '../../frame';
import { owner } from '../../shell/fixtures';

const fixtures = { bots: [], capabilities: owner };

const designFrames = frames([
  {
    name: 'conversation', title: 'Vault hand-off · done in the browser · back to the conversation',
    width: 760, height: 120, status: 'shipped', fixtures,
    render: () => <div className="p-6"><VaultHandoffDone session="ses_design" /></div>,
  },
  {
    name: 'vaults-page', title: 'Vault hand-off · done in the browser · from the Vaults page (no link)',
    width: 760, height: 120, status: 'shipped', fixtures,
    render: () => <div className="p-6"><VaultHandoffDone session={null} /></div>,
  },
  {
    name: 'phone', title: 'Vault hand-off · done in the browser · narrow window',
    width: 390, height: 160, status: 'shipped', fixtures,
    render: () => <div className="p-4"><VaultHandoffDone session="ses_design" /></div>,
  },
]);

export default designFrames;
