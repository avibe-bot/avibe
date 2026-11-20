// Proposed 2026-11-20 (frames only, a draft to discuss): Setup's optional last step, remote access
// (ui/design: setup/remote-access/*). The same block as Settings · Devices while remote access is
// off (kit/devices.tsx `ClaimFields`), in Setup's own shell: one behaviour everywhere. One tap
// skips it. In the Desktop App it takes the place of the 「本机已准备好」 screen that follows Setup
// today. Draft copy, inline in both languages.
import { ArrowRight } from 'lucide-react';

import { SetupHeader } from '@/components/Wizard';
import { Button } from '@/components/ui/button';
import { ClaimFields } from './devices';
import { useDraft } from './draft';

export type SetupRemoteAccessProps =
  /** A plain browser on a standalone Runtime: the first claim passes avibe.bot once. */
  | { surface: 'browser' }
  /** The Desktop App: its own sign-in, then the name here. */
  | { surface: 'desktop'; account: string | null; name: string };

export function SetupRemoteAccess(props: SetupRemoteAccessProps) {
  const d = useDraft();
  const signedIn = props.surface === 'desktop' && props.account !== null;
  const action = props.surface === 'browser' || signedIn ? d('开启远程访问', 'Turn on remote access') : d('登录并开启', 'Sign in and turn on');
  return (
    <div className="onboarding-shell">
      <SetupHeader />
      <main className="onboarding-shell-content">
        <div className="onboarding-step" data-setup-screen="remote-access">
          <div className="onboarding-screens">
            <div data-setup-screen-root="remote-access">
              <div className="onboarding-setup">
                <header className="onboarding-heading">
                  <h1 tabIndex={-1}>{d('在手机和其他电脑上打开它', 'Open it from your phone and other computers')}</h1>
                  <p>{d('可选。领一个专属域名就能远程访问；数据和 Agent 仍然留在这台电脑上。', 'Optional. A domain of your own gives you remote access; your data and Agents stay on this computer.')}</p>
                </header>
                <div className="mx-auto mb-10 w-full max-w-[560px] overflow-hidden rounded-xl border border-edge bg-surface text-left shadow-elevation-0">
                  {props.surface === 'desktop' && signedIn ? (
                    <ClaimFields account={props.account ?? ''} name={props.name} nameState="free" />
                  ) : (
                    <p className="px-5 py-4 text-[13px] leading-relaxed text-muted">
                      {props.surface === 'browser'
                        ? d(
                            '只有这一次：去 avibe.bot 起个名字、领取域名，然后自动回来，直接进入工作台。',
                            'Only this once: name it and get its domain on avibe.bot, then come back by itself, straight into the workbench.',
                          )
                        : d(
                            '用这个 App 的 Avibe 账号登录一次，回到这里起个名字就能开启。',
                            'Sign in to this App with your Avibe account once, then name this computer here.',
                          )}
                    </p>
                  )}
                </div>
              </div>
            </div>
          </div>
          <div className="onboarding-setup-footer">
            <Button type="button" variant="brand" className="group onboarding-action-w onboarding-primary-action">
              {action}<ArrowRight size={16} />
            </Button>
            <Button type="button" variant="ghost" className="onboarding-action-w onboarding-back-action">
              {d('以后再说，进入工作台', 'Later, go to the workbench')}
            </Button>
          </div>
        </div>
      </main>
    </div>
  );
}
