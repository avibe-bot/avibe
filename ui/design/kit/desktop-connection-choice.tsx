// Proposed 2026-11-21 (frames only; ui/design: desktop/connection-choice/*): where the Desktop App
// keeps its own choice of path, 「自动」 or 「仅 Cloudflare 隧道」. The choice is this computer's, for
// every instance it opens; it is kept by the app itself, and no page can read or change it.
//
// A. In the app's own menus (the tray menu on macOS and Windows, and the macOS application menu),
//    next to 「切换实例」: a 「连接方式」 item whose submenu holds the two choices, the current one
//    ticked. The system draws these menus; `NativeMenu` is a stand-in for them, as
//    `kit/desktop-native-show-window.tsx` is for the title bar. The other items are the shell's
//    shipped ones, in the shell's order (`install_native_tray` in `desktop/src-tauri/src/lib.rs`).
// B. On the app's instances page, a 「连接方式」 section under the account, drawn as the Devices
//    page draws its 「这台设备」 group.
//
// All three strings are the Devices page's shipped copy (`devices.connection.title`, `.automatic`,
// `.tunnelOnly`, and for B the two hints), read by key, so the menu and that page cannot drift apart.
import type { ReactNode } from 'react';
import { Check, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useDraft } from './draft';

export type ConnectionChoice = 'automatic' | 'tunnel';

type Item = { label: string; ticked?: boolean; disabled?: boolean; shortcut?: string; open?: boolean } | 'separator';

function MenuPanel({ items, system, className = '' }: { items: Item[]; system: 'macos' | 'windows'; className?: string }) {
  const mac = system === 'macos';
  return (
    <div className={`w-max border border-edge bg-popover py-1 text-popover-foreground shadow-elevation-3 ${mac ? 'rounded-lg px-1' : 'rounded-sm'} ${className}`}>
      {items.map((item, index) => item === 'separator'
        ? <div key={index} className="my-1 border-t border-border" />
        : (
          <div
            key={index}
            data-menu-item={item.open ? 'open' : undefined}
            className={`flex items-center gap-2 py-1 pr-2 text-titlebar ${mac ? 'rounded-md pl-1' : 'pl-2'} ${item.open ? 'bg-field' : ''} ${item.disabled ? 'text-muted' : ''}`}
          >
            <span className="grid size-4 shrink-0 place-items-center">{item.ticked && <Check className="size-3.5" strokeWidth={2.5} />}</span>
            <span className="flex-1 whitespace-nowrap">{item.label}</span>
            {item.shortcut && <span className="pl-6 text-muted">{item.shortcut}</span>}
            {item.open !== undefined && <ChevronRight className="size-3.5 text-muted" />}
          </div>
        ))}
    </div>
  );
}

/** The tray menu while the app shows another computer's instance, with 「连接方式」 open. */
export function ConnectionChoiceMenu({ choice, system }: { choice: ConnectionChoice; system: 'macos' | 'windows' }) {
  const { t } = useTranslation();
  const mac = system === 'macos';
  const items: Item[] = [
    { label: t('desktopBootstrap.tray.open') },
    { label: t('desktopBootstrap.tray.serving', { address: '127.0.0.1:5123' }), disabled: true },
    'separator',
    { label: t('desktopBootstrap.computerUse.ready'), ticked: true },
    { label: t('desktopBootstrap.tray.settings'), shortcut: mac ? '⌘,' : 'Ctrl+,' },
    { label: t('desktopBootstrap.updater.menu') },
    { label: t('desktopBootstrap.updater.testChannel') },
    { label: t('desktopBootstrap.tray.login'), ticked: true },
    { label: t('desktopBootstrap.notifications.toggle'), ticked: true },
    { label: t('desktopBootstrap.pet.toggle') },
    { label: t('desktopBootstrap.pet.shortcutSettings') },
    'separator',
    { label: t('desktopBootstrap.tray.quit'), shortcut: mac ? '⌘Q' : 'Ctrl+Q' },
    { label: t('desktopClient.accountService') },
    { label: t('desktopClient.switchInstance') },
    { label: t('devices.connection.title'), open: true },
    { label: t('desktopClient.signOut') },
  ];
  const choices: Item[] = [
    { label: t('devices.connection.automatic'), ticked: choice === 'automatic' },
    { label: t('devices.connection.tunnelOnly'), ticked: choice === 'tunnel' },
  ];
  return (
    <div className={`flex h-full w-full bg-background p-6 text-foreground ${mac ? 'items-start' : 'items-end'}`}>
      <div className="flex items-end gap-1">
        <MenuPanel items={items} system={system} />
        {/* The submenu opens beside its item, the last but one of the menu. */}
        <MenuPanel items={choices} system={system} className="mb-6" />
      </div>
    </div>
  );
}

/** B: the same choice as a section of the app's instances page, under the account. */
export function ConnectionChoiceSection({ choice }: { choice: ConnectionChoice }) {
  const { t } = useTranslation();
  const d = useDraft();
  const options: [ConnectionChoice, string, string][] = [
    ['automatic', t('devices.connection.automatic'), t('devices.connection.automaticHint')],
    ['tunnel', t('devices.connection.tunnelOnly'), t('devices.connection.tunnelOnlyHint')],
  ];
  return (
    <section aria-label={t('devices.connection.title')} className="mx-auto mt-6 w-full max-w-2xl">
      <h2 className="mb-2 text-sm font-medium text-muted">{t('devices.connection.title')}</h2>
      <div role="radiogroup" aria-label={t('devices.connection.title')} className="divide-y divide-border overflow-hidden rounded-xl border border-edge bg-surface">
        {options.map(([id, label, hint]) => (
          <div key={id} role="radio" aria-checked={choice === id} className="flex items-center gap-3 p-4">
            <span className={`flex size-4 shrink-0 items-center justify-center rounded-full border ${choice === id ? 'border-mint bg-mint' : 'border-border-strong'}`}>
              {choice === id && <span className="size-1.5 rounded-full bg-background" />}
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-semibold">{label}</span>
              <span className="mt-1 block text-xs leading-5 text-muted">{hint}</span>
            </span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-xs leading-5 text-muted">
        {d('这个选择只管这台电脑，对它打开的每个实例都生效。', 'This choice is for this computer only, for every instance it opens.')}
      </p>
    </section>
  );
}

/** A page of the app's own account surface, as `desktop/instances` frames lay it out. */
export function AccountPage({ children }: { children: ReactNode }) {
  return <div className="h-full overflow-y-auto bg-background px-10 py-5 text-foreground">{children}</div>;
}
