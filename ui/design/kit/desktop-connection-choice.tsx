// Approved 2026-11-21 (Product; ui/design: desktop/connection-choice/*): where the Desktop App keeps
// this computer's choice of path to an instance: 「自动」, 「仅 Cloudflare 隧道」, and since
// 2026-11-24 (Product, option A) 「仅直连和中继」. As in the iPhone App, the choice is per account
// and instance, kept by the app itself; the instance's Devices › 连接方式 page shows and changes the
// same choice.
//
// It lives in the app's own menus (the tray menu on macOS and Windows, and the macOS application
// menu), right after 「切换实例」: a 「连接方式」 item whose submenu holds the choices, the one for
// the instance on show ticked. While this computer's own instance is on show there is nothing to
// choose and the item is greyed. 「仅直连和中继」 is in the submenu only for an instance that could
// be reached on a path that is not the tunnel (`direct`); for any other it is absent, not greyed. The system draws these menus; `MenuPanel` is a stand-in for
// them, as `kit/desktop-native-show-window.tsx` is for the title bar. The other items are the
// shell's shipped ones, in the shell's order (`install_native_tray` in `desktop/src-tauri/src/lib.rs`).
//
// Every string is the Devices page's shipped copy (`devices.connection.title`, `.automatic`,
// `.tunnelOnly`, `.directOnly`), read by key (`ConnectionMenu` in
// `desktop/src-tauri/src/direct/shell.rs`), so the menu and that page cannot drift apart.
import { Check, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';


export type ConnectionChoice = 'automatic' | 'tunnel' | 'direct';

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

/** The tray menu with 「连接方式」: open on the choice of the instance on show, or greyed (`choice`
 *  null) while this computer's own instance is. `direct`: the instance on show could be routed, so
 *  「仅直连和中继」 is offered. */
export function ConnectionChoiceMenu({ choice, system, direct = true }: { choice: ConnectionChoice | null; system: 'macos' | 'windows'; direct?: boolean }) {
  const { t } = useTranslation();
  const mac = system === 'macos';
  const remote = choice !== null;
  const items: Item[] = [
    { label: t('desktopBootstrap.tray.open') },
    { label: t('desktopBootstrap.tray.serving', { address: '127.0.0.1:5123' }), disabled: true },
    'separator',
    { label: t('desktopBootstrap.computerUse.ready'), ticked: true },
    { label: t('desktopBootstrap.tray.settings'), shortcut: mac ? '⌘,' : 'Ctrl+,' },
    { label: t('desktopBootstrap.updater.menu') },
    { label: t('desktopBootstrap.updater.testChannel') },
    { label: t('desktopBootstrap.tray.login'), ticked: true },
    // Off while another computer's instance is on show (`refresh_native_controls`).
    { label: t('desktopBootstrap.notifications.toggle'), ticked: true, disabled: remote },
    { label: t('desktopBootstrap.pet.toggle'), disabled: remote },
    { label: t('desktopBootstrap.pet.shortcutSettings') },
    'separator',
    { label: t('desktopBootstrap.tray.quit'), shortcut: mac ? '⌘Q' : 'Ctrl+Q' },
    { label: t('desktopClient.accountService') },
    { label: t('desktopClient.switchInstance') },
    choice ? { label: t('devices.connection.title'), open: true } : { label: t('devices.connection.title'), open: false, disabled: true },
    { label: t('desktopClient.signOut') },
    ...(remote ? [{ label: t('desktopClient.auxiliaryIncomplete'), disabled: true }] : []),
  ];
  const choices: Item[] = [
    { label: t('devices.connection.automatic'), ticked: choice === 'automatic' },
    { label: t('devices.connection.tunnelOnly'), ticked: choice === 'tunnel' },
    ...(direct ? [{ label: t('devices.connection.directOnly'), ticked: choice === 'direct' }] : []),
  ];
  return (
    <div className={`flex h-full w-full bg-background p-6 text-foreground ${mac ? 'items-start' : 'items-end'}`}>
      <div className="flex items-end gap-1">
        <MenuPanel items={items} system={system} />
        {/* The submenu opens beside its item, the last but one of the menu. */}
        {choice && <MenuPanel items={choices} system={system} className="mb-12" />}
      </div>
    </div>
  );
}
