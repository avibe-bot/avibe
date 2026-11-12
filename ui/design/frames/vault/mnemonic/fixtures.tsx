// Shared scaffolding for the seed-phrase wallet frames (proposal: _tmp/vault-mnemonic/design-v2.md).
// A dialog is drawn open over nothing (as vault/secret-dialog does) or over the shipped Vaults page;
// the wallet itself is listed among the page's other secrets as the keypair the shipped page would
// show, so the page behind a dialog stays the real one.
import type { ReactNode } from 'react';

import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { IMPORTED_ADDRESSES } from '../../../kit/vault-mnemonic/data';
import { DESIGN_NOW } from '../../../shell/clock';
import { consolePage } from '../../workbench/fixtures';
import { secret, vaultApi, vaultFixtures, vaultSecrets } from '../fixtures';

const noop = () => {};

const wallet = secret('AGENT_WALLET', {
  kind: 'keypair',
  signer_kind: 'local',
  description: '给交易和测试 Agent 用的热钱包（公开的测试助记词，切勿充值）',
  tags: ['crypto'],
  signing_addresses: IMPORTED_ADDRESSES[0],
  use_count: 41,
  last_used_at: new Date(DESIGN_NOW - 2 * 3600_000).toISOString(),
});

/** The Vault with the wallet among the household's secrets, no requests or grants. */
export const walletFixtures = vaultFixtures(vaultApi({ secrets: [...vaultSecrets, wallet], grants: [], requests: [] }));

/** An avault new enough for the wallet (0.1.11), or the one before it. */
export const avaultFixtures = (version: string) => vaultFixtures({
  ...vaultApi({ grants: [], requests: [] }),
  '/api/dependencies': { deps: [{ id: 'avault', kind: 'tool', required: true, installed: true, version, status: 'ready' }] },
});

const dialog = (title: string, body: ReactNode, wide: boolean) => (
  <Dialog open onOpenChange={noop}>
    <DialogContent className={wide ? 'max-w-2xl' : undefined}>
      <DialogTitle className="sr-only">{title}</DialogTitle>
      {body}
    </DialogContent>
  </Dialog>
);

/** A dialog on its own, as vault/secret-dialog draws the Add dialog. */
export const alone = (title: string, body: ReactNode, wide = false) => () => dialog(title, body, wide);

/** A dialog over the shipped Vaults page on a desktop. */
export const overPage = (title: string, body: ReactNode, wide = false) =>
  consolePage(<><VaultsPage />{dialog(title, body, wide)}</>);

/** A dialog over the shipped Vaults page on a phone, where it is a bottom sheet. The page is drawn
 *  without AppShell, whose language comes from the config fixture, so the frame follows ?lang=. */
export const overPhone = (title: string, body: ReactNode) =>
  consolePage(<><VaultsPage />{dialog(title, body, false)}</>);

/** A kit surface centred on the canvas, for a part shown on its own. */
export const onCanvas = (body: ReactNode, width: number) => () => (
  <div className="flex min-h-screen items-start justify-center bg-background p-8">
    <div style={{ width }}>{body}</div>
  </div>
);
