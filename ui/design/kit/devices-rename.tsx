// Proposed 2026-11-21 (frames only): changing this Avibe's domain under the owner's 11:05 rules, and
// rebinding remote access (ui/design: settings/devices-rename/*). Replaces the rename dialogs of
// `ConnectionPageView` (src/components/devices). The old name is held for a waiting period (one
// number, passed in; 30 now) in which only this account can take it back; afterwards anyone may
// claim it. Renaming clears what it can by itself: an open page clears its own data and moves to
// the new address, and any browser that opens the old address during the waiting period is cleared
// and told it moved. What it cannot reach, it says plainly. Draft copy, inline in both languages.
import { Check, Globe, Laptop, Monitor, RotateCcw, Smartphone, TriangleAlert } from 'lucide-react';

import { ClaimFields } from '@/components/devices/DevicesPageView';
import { SettingsPanel } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { useDraft } from './draft';
import { Para } from './devices-text';

type D = (zh: string, en: string) => string;


export const DOMAIN_SUFFIX = '-app.avibe.bot';

export type RenameDeviceKind = 'host' | 'iphone' | 'desktop' | 'browser' | 'homeScreen';
export type RenameDevice = {
  name: string;
  kind: RenameDeviceKind;
  /** The device this dialog is open on. */
  self?: boolean;
  /** After the rename: whether this device's old data is known to be cleared. */
  cleanup?: 'cleared' | 'unconfirmed';
  /** Shown with `unconfirmed`: when it was last here, already said in the page's words. */
  lastActive?: { zh: string; en: string };
};

/** A name this account gave up and still holds: it can take it back until the hold ends. */
export type HeldName = { name: string; until: Day };

/** A day, already said in each language (「12 月 3 日」, December 3). */
export type Day = { zh: string; en: string };

export type RenameReject =
  | { kind: 'taken' }
  /** Someone else gave this name up and it is still in their waiting period. */
  | { kind: 'held' }
  /** This account already holds the most names it may (`limit`, counting names still waiting);
   *  `released` is when the earliest waiting one is released. */
  | { kind: 'limit'; limit: number; released: Day }
  /** Renaming refused: the account already holds `count` names (renaming may end at the limit plus
   *  the one just left, so this is the limit plus one). */
  | { kind: 'renameLimit'; count: number; released: Day }
  /** Every name it may hold is in use (none waiting): binding another computer needs one deleted. */
  | { kind: 'limitInUse'; limit: number };

export type RenameDialogProps = {
  step: 'form' | 'confirm' | 'done';
  /** The domain now (`form`, `confirm`) or the one it was (`done`). */
  host: string;
  /** The new name typed or picked; for `done`, the new domain's name. */
  name: string;
  /** The real day the old name is released to others: 30 days on (plus a margin), or later when
   *  a direct certificate signed for it runs longer. Never said as a fixed number of days. */
  releaseOn: Day;
  devices: RenameDevice[];
  /** A device of this Avibe has Health connected. */
  health: boolean;
  held?: HeldName[];
  reject?: RenameReject;
  /** `confirm`: seconds before the button can be pressed. */
  countdown?: number;
  /** While refused, fold away what renaming would do (Product compares both). */
  collapseOnReject?: boolean;
};

const deviceIcon = { host: Laptop, iphone: Smartphone, desktop: Monitor, browser: Globe, homeScreen: Globe } as const;

/** How to sign out on that device, said by what it is. Not required: renaming signs every device out. */
const howToSignOut = (d: D, device: RenameDevice, host: string) => {
  if (device.self) return d('就是这台：改名时会自己清理并跳到新地址。', 'This one: it clears itself and moves to the new address when you rename.');
  switch (device.kind) {
    case 'host': return d('运行 Avibe 的电脑：不用做什么。', 'The computer running Avibe: nothing to do.');
    case 'iphone': return d('在 Avibe App 里打开「实例」，点你的账号，选「在此设备退出」。', 'In the Avibe App, open Instances, tap your account, then choose Sign out on this device.');
    case 'desktop': return d('在 Avibe Desktop 里打开「实例」，点「账户」下的账号，选「在此设备退出」。', 'In Avibe Desktop, open Instances, choose your account under Account, then Sign out on this device.');
    case 'homeScreen': return d('打开主屏幕上的 Avibe，在「更多」里选「退出登录」。', 'Open Avibe from the home screen and choose Sign out under More.');
    case 'browser': return d(`在那个浏览器里打开 ${host}，点头像选「退出登录」（手机上在「更多」里）。`, `In that browser, open ${host}, click your avatar and choose Sign out (on a phone, under More).`);
  }
};

/** The one thing renaming cannot reach (Product, 12:32: wording B). Never small print. Afterwards its
 *  last sentence says what can still be done. */
function Residue({ host, releaseOn, after }: { host: string; releaseOn: Day; after: boolean }) {
  const d = useDraft();
  const text = after
    ? d(
      `清不到的：如果某个浏览器在 ${releaseOn.zh}之前没有再打开过 ${host}，它里面的对话记录不会被清掉，以后领取这个名字的人可能读到。担心的话，在那台设备上打开一次 ${host}，或者退出登录。`,
      `Not cleared: a browser that does not open ${host} again before ${releaseOn.en} keeps its conversations, and whoever claims the name later could read them. If that worries you, open ${host} once on that device, or sign out there.`,
    )
    : d(
      `清不到的：如果某个浏览器在 ${releaseOn.zh}之前没有再打开过 ${host}，它里面的对话记录不会被清掉，以后领取这个名字的人可能读到。担心的话，改名前先在那台设备上退出。`,
      `Not cleared: a browser that does not open ${host} again before ${releaseOn.en} keeps its conversations, and whoever claims the name later could read them. If that worries you, sign out on that device before renaming.`,
    );
  return (
    <div role="note" className="flex gap-3 rounded-lg border border-gold/40 bg-gold/10 px-4 py-3">
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <p className="text-[13px] leading-relaxed text-foreground"><Para text={text} /></p>
    </div>
  );
}

/** The devices, one row each: before the rename how to sign out there, after it whether it was cleared. */
function DeviceChecklist({ devices, host, after }: { devices: RenameDevice[]; host: string; after: boolean }) {
  const d = useDraft();
  return (
    <div className="overflow-hidden rounded-lg border border-edge">
      <div className="border-b border-border bg-field/60 px-4 py-2 text-[12px] font-semibold text-foreground">
        {after ? d('你的设备', 'Your devices') : d('你的设备（可以先在上面退出）', 'Your devices (you can sign out on them first)')}
      </div>
      {devices.map((device) => {
        const Icon = deviceIcon[device.kind];
        return (
          <div key={device.name} className="flex items-start gap-3 border-b border-border px-4 py-2.5 last:border-b-0">
            <Icon className="mt-0.5 size-4 shrink-0 text-muted" />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-foreground">{device.name}</div>
              {!after && <div className="text-[11px] leading-relaxed text-muted"><Para text={howToSignOut(d, device, host)} /></div>}
            </div>
            {after && (device.cleanup === 'cleared' ? (
              <Badge variant="success" className="shrink-0"><Check className="size-3" />{d('已清理', 'Cleared')}</Badge>
            ) : (
              <span className="shrink-0 pt-0.5 text-right text-[11px] text-muted">
                {d('未确认', 'Not confirmed')}{device.lastActive ? d(` · 上次活跃于 ${device.lastActive.zh}`, ` · last active ${device.lastActive.en}`) : ''}
              </span>
            ))}
          </div>
        );
      })}
      <p className="border-t border-border px-4 py-2 text-[11px] leading-relaxed text-muted">
        <Para text={d(
          '这里只有现在还登录着的设备。登录早已过期的浏览器、通过分享链接来过的访客不在清单里，清单全部打勾也不代表没有残留。',
          'Only devices signed in now are listed. Browsers whose sign-in expired long ago, and visitors who came through a shared link, are not here, so a fully ticked list does not mean nothing is left.',
        )} />
      </p>
    </div>
  );
}

function Consequences({ host, releaseOn, health }: { host: string; releaseOn: Day; health: boolean }) {
  const d = useDraft();
  const items: Array<string | null> = [
    d('所有设备都会退出，要用新地址重新登录。', 'Every device is signed out and signs in again at the new address.'),
    d('已经发出去的展示页和文件链接会失效。', 'Show Page and file links already sent stop working.'),
    d('通知会自己恢复。', 'Notifications come back by themselves.'),
    health ? d('已同步的健康数据会被删除，要在 iPhone 上重新连接健康。', 'Synced Health data is deleted; connect Health again on the iPhone.') : null,
    d(`${releaseOn.zh}之前只有你的账号能拿回 ${host}；之后别人可以领取，不会还给你。`, `Until ${releaseOn.en} only your account can take ${host} back; after that someone else can claim it, and you do not get it back.`),
  ].filter(Boolean);
  return (
    <div className="rounded-lg bg-field px-4 py-3">
      <div className="text-[12px] font-semibold text-foreground">{d('改名后', 'After renaming')}</div>
      <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-[12px] leading-relaxed text-muted">
        {items.map((item, index) => <li key={index}><Para text={item ?? ''} /></li>)}
      </ul>
    </div>
  );
}

function RejectLine({ reject }: { reject: RenameReject }) {
  const d = useDraft();
  const text = reject.kind === 'taken'
    ? d('已被占用，换一个名称。', 'Taken. Choose another name.')
    : reject.kind === 'held'
      ? d('这个名字还在原来主人的等待期里，暂时不能取。换一个名称。', 'This name is still in its previous owner’s waiting period and cannot be taken yet. Choose another name.')
      : reject.kind === 'renameLimit'
        ? d(
          `这个账号已持有 ${reject.count} 个名字（包括冷却中的），已到上限。最早的一个在 ${reject.released.zh}释放，之后可以再改。`,
          `This account already holds ${reject.count} names, including those still waiting, which is the limit. The earliest is released on ${reject.released.en}; you can rename after that.`,
        )
      : reject.kind === 'limitInUse'
        ? d(
          `这个账号已有 ${reject.limit} 个在用的名字，已到上限。要再绑定一台，先删除一个实例，它的名字释放后可以再取。`,
          `This account already has ${reject.limit} names in use, which is the limit. To bind another computer, delete an instance first; you can take a new name once its name is released.`,
        )
        : d(
        `这个账号已持有 ${reject.limit} 个名字（包括冷却中的）。最早的一个在 ${reject.released.zh}释放，之后可以再取。`,
        `This account already holds ${reject.limit} names, including those still waiting. The earliest is released on ${reject.released.en}; you can take another after that.`,
      );
  return <span role="alert" className="text-[11px] leading-relaxed text-destructive-ink"><Para text={text} /></span>;
}

export function RenameDialog({ step, host, name, releaseOn, devices, health, held = [], reject, countdown = 0, collapseOnReject = true }: RenameDialogProps) {
  const d = useDraft();
  const newHost = `${name}${DOMAIN_SUFFIX}`;
  const reclaiming = held.some((each) => each.name === name);
  if (step === 'confirm') {
    return (
      <Dialog open onOpenChange={() => undefined}>
        <DialogContent className="max-w-md" closeLabel={d('关闭', 'Close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{d(`改成 ${newHost}？`, `Change to ${newHost}?`)}</DialogTitle>
            <DialogDescription>
              <Para text={d(
                `这不能撤销。${host} 立即停用，所有设备都会退出，包括你现在用的这一台。${releaseOn.zh}之前只有你的账号能把它拿回来。`,
                `This cannot be undone. ${host} stops working at once and every device is signed out, this one included. Until ${releaseOn.en} only your account can take it back.`,
              )} />
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost">{d('取消', 'Cancel')}</Button>
            <Button variant="destructive" disabled={countdown > 0}>
              {countdown > 0 ? d(`更改域名（${countdown}）`, `Change the domain (${countdown})`) : d('更改域名', 'Change the domain')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  if (step === 'done') {
    const allCleared = devices.every((each) => each.cleanup === 'cleared');
    return (
      <Dialog open onOpenChange={() => undefined}>
        <DialogContent className="max-w-[520px]" mobileSheetHeight="tall" closeLabel={d('关闭', 'Close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{d(`域名已改为 ${newHost}`, `The domain is now ${newHost}`)}</DialogTitle>
            <DialogDescription>
              <Para text={allCleared
                ? d('清单里的设备都已清理，并退出了登录。', 'Every listed device has been cleared and signed out.')
                : d('设备再打开旧地址时会自己清理，前提是运行 Avibe 的这台电脑开着。还没确认的，打开过一次就会变成「已清理」。', 'A device clears itself the next time it opens the old address, as long as the computer running Avibe is on. Those not confirmed yet turn to Cleared once they have opened it.')} />
            </DialogDescription>
          </DialogHeader>
          <DeviceChecklist devices={devices} host={host} after />
          <Residue host={host} releaseOn={releaseOn} after />
          <DialogFooter>
            <Button variant="default">{d('完成', 'Done')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  return (
    <Dialog open onOpenChange={() => undefined}>
      <DialogContent className="max-w-[560px]" mobileSheetHeight="tall" closeLabel={d('关闭', 'Close')}>
        <DialogHeader>
          <DialogTitle>{d('更改域名', 'Change the domain')}</DialogTitle>
          <DialogDescription>{d(`现在是 ${host}。`, `It is ${host} now.`)}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Input className="w-48 text-[13px]" value={name} readOnly aria-label={d('新名称', 'New name')} aria-invalid={Boolean(reject) || undefined} />
            <span className="truncate font-mono text-[12px] text-muted">{DOMAIN_SUFFIX}</span>
          </div>
          {reject && <RejectLine reject={reject} />}
          {reclaiming && !reject && (
            <span className="text-[11px] leading-relaxed text-mint-ink">{d('这是你用过的名字，可以直接用回来。', 'You used this name before. You can take it back.')}</span>
          )}
          {held.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11px] text-muted">{d('可以用回你自己的名字：', 'You can take back a name of your own:')}</span>
              <div className="flex flex-wrap gap-2">
                {held.map((each) => (
                  <button
                    key={each.name}
                    type="button"
                    aria-pressed={each.name === name}
                    className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] ${each.name === name ? 'border-mint bg-mint-soft text-mint-ink' : 'border-edge text-foreground'}`}
                  >
                    <RotateCcw className="size-3" />
                    <span className="font-mono">{each.name}</span>
                    <span className="text-muted">{d(`· ${each.until.zh}之前`, `· until ${each.until.en}`)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        {!(collapseOnReject && reject) && (
          <>
            <Consequences host={host} releaseOn={releaseOn} health={health} />
            <Residue host={host} releaseOn={releaseOn} after={false} />
            <DeviceChecklist devices={devices} host={host} after={false} />
          </>
        )}
        <DialogFooter>
          <Button variant="ghost">{d('取消', 'Cancel')}</Button>
          <Button variant="default" disabled={Boolean(reject) || !name}>{reclaiming ? d('拿回这个名字', 'Take this name back') : d('继续', 'Continue')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** What any browser sees when it opens the old address during the waiting period, after it cleared
 *  what it kept for it: one sentence and a link to avibe.bot. Not in Settings. */
export function MovedPage() {
  const d = useDraft();
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-md text-center">
        <div className="mx-auto mb-6 grid size-12 place-items-center rounded-2xl border border-edge bg-field"><Globe className="size-5 text-muted" /></div>
        <p className="text-balance text-[15px] leading-relaxed text-foreground">
          <Para text={d('这个 Avibe 换了新地址。能访问它的人，在 avibe.bot 登录后可以打开。', 'This Avibe has a new address. People who can access it can open it after signing in at avibe.bot.')} />
        </p>
        <a href="https://avibe.bot" className="mt-5 inline-flex items-center gap-1 text-[14px] font-medium text-cyan-ink hover:underline">avibe.bot</a>
      </div>
    </main>
  );
}

/** The instance learned its address was changed somewhere else, with its device key: say so, loudly. */
export function UnrecognizedChangeBanner({ host }: { host: string }) {
  const d = useDraft();
  return (
    <div role="alert" className="flex flex-col gap-3 rounded-xl border border-destructive/40 bg-destructive/10 px-5 py-4 md:flex-row md:items-center">
      <TriangleAlert className="size-5 shrink-0 text-destructive-ink max-md:hidden" />
      <div className="min-w-0 flex-1">
        <div className="text-balance text-[14px] font-semibold text-foreground">{d('这个 Avibe 的地址在别处被改了', 'This Avibe’s address was changed somewhere else')}</div>
        <p className="mt-1 text-[12px] leading-relaxed text-foreground">
          <Para text={d(
            `现在的 ${host} 不是在这台电脑上改的，可能有人拿到了它的设备密钥。请先重新绑定远程访问，再在「更改域名」里把原来的名字拿回来。`,
            `${host} was not set from this computer; someone may have its device key. Bind remote access again first, then take your old name back under Change the domain.`,
          )} />
        </p>
      </div>
      <Button variant="destructive" size="sm" className="shrink-0">{d('去重新绑定', 'Go to Bind again')}</Button>
    </div>
  );
}

/** At the computer that runs Avibe, in the domain's panel under 「更改域名」: bind remote access again.
 *  Rarely needed, so a quiet row (Product, 12:32). */
export function RebindRow() {
  const d = useDraft();
  return (
    <div className="flex items-center gap-3 border-t border-border px-5 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-foreground">{d('重新绑定远程访问', 'Bind remote access again')}</div>
        <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
          <Para text={d('怀疑有人动过这台电脑的远程访问时用：旧的设备密钥会作废。', 'For when you suspect someone changed this computer’s remote access: the old device key stops working.')} />
        </div>
      </div>
      <Button variant="ghost" size="compact" className="shrink-0">{d('重新绑定', 'Bind again')}</Button>
    </div>
  );
}

export function RebindDialog({ host, account }: { host: string; account: string }) {
  const d = useDraft();
  return (
    <Dialog open onOpenChange={() => undefined}>
      <DialogContent className="max-w-md" closeLabel={d('关闭', 'Close')}>
        <DialogHeader>
          <DialogTitle className="text-balance">{d('重新绑定远程访问？', 'Bind remote access again?')}</DialogTitle>
          <DialogDescription>
            <Para text={d(
              `这台电脑会用 ${account} 重新向 avibe.bot 登记。旧的设备密钥立即作废。所有设备都会退出，要重新登录；这台电脑上的页面不受影响。${host} 不变；之后可以在「更改域名」里拿回你等待期内的旧名字。`,
              `This computer registers with avibe.bot again as ${account}. The old device key stops working at once. Every device is signed out and signs in again; pages on this computer are not affected. ${host} stays; afterwards you can take back an old name still in its waiting period under Change the domain.`,
            )} />
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost">{d('取消', 'Cancel')}</Button>
          <Button variant="destructive">{d('重新绑定', 'Bind again')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The claim on the Devices page (or Setup) refused because this account holds the most names it may. */
export function ClaimLimitCard({ account, name, reject }: { account: string; name: string; reject: RenameReject }) {
  const d = useDraft();
  return (
    <SettingsPanel
      title={d('远程访问', 'Remote access')}
      description={d('在手机和其他电脑上打开这个 Avibe。数据和 Agent 仍然留在这台电脑上。', 'Open this Avibe from your phone and other computers. Your data and Agents stay on this computer.')}
      actions={<Badge variant="secondary">{d('未开启', 'Off')}</Badge>}
    >
      <ClaimFields account={account} name={name} />
      <div className="px-5 pb-1 md:pl-[116px]"><RejectLine reject={reject} /></div>
      <div className="border-t border-border px-5 py-4">
        <Button variant="brand" disabled>{d('开启远程访问', 'Turn on remote access')}</Button>
      </div>
    </SettingsPanel>
  );
}
