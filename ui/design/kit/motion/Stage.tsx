// The stage a moment plays on: the shipped page in each of its states, stacked. A moment is the change
// between two states the product already draws, so the loop moves from the page before it to the page after
// it: the after page starts where the before page was, and glides to where it is.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';
import clsx from 'clsx';

import { ChatPage } from '@/components/workbench/ChatPage';
import { InboxPage } from '@/components/workbench/InboxPage';

import { InboxStateProvider } from '../../shell/DesignShell';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { DockProvider } from '@/context/DockProvider';
import { ShowPageDragProvider } from '@/context/ShowPageDragProvider';

/** The Dock and the Show Page drag the chat header reads (AppShell serves them in the app). */
const withChatProviders = (node: ReactNode) => (
  <DockProvider enabled>
    <ShowPageDragProvider>{node}</ShowPageDragProvider>
  </DockProvider>
);

/** The shipped chat page for one Session, whatever the frame's route. */
export function ChatAt({ session }: { session: string }) {
  return (
    <Routes location={`/chat/${session}`}>
      <Route path="/chat/:sessionId" element={<ChatPage />} />
    </Routes>
  );
}

/** One state of a moment, filling the screen. `opaque` paints the page background under it. A layer
 *  stacked over another is `seeThrough`: the phone chat draws itself on an opaque sheet, and over the
 *  layer below, which draws that same background, the sheet would only hide what the two states share,
 *  so the parts that differ (the composer) could not cross-fade. Each layer isolates its page, so a
 *  page's own stacking (the phone chat is a fixed, raised sheet) stays inside it and the layers stack in
 *  the order they are written. */
export function Layer({ name, opaque = false, seeThrough = false, children }: { name: string; opaque?: boolean; seeThrough?: boolean; children: ReactNode }) {
  return (
    <div
      data-motion-layer={name}
      className={clsx(
        'absolute inset-0 isolate',
        opaque && 'bg-background page-glow-console',
        seeThrough && '[&_.fixed.z-40.bg-background]:bg-transparent',
      )}
    >
      {children}
    </div>
  );
}

/** A phone screen holding stacked states, as `PhoneConversation` draws a chat full screen. */
export function PhoneStage({ children }: { children: ReactNode }) {
  return <div className="relative h-full overflow-hidden bg-background text-foreground">{withChatProviders(children)}</div>;
}


/** The shipped Inbox page on a phone screen, reading the rows of one of the frame's `inboxStates`. */
export function InboxAt({ state }: { state: string }) {
  return (
    <InboxStateProvider state={state}>
      <PhoneScreen>
        <InboxPage onOpenSearch={() => {}} />
      </PhoneScreen>
    </InboxStateProvider>
  );
}

/** The desktop Workbench (the sidebar and the content column) with stacked states in the column. Each
 *  layer covers the whole column and pads itself as the column does, since the chat page reaches out
 *  over that padding. */
export function DesktopStage({ children }: { children: ReactNode }) {
  return withChatProviders(
    <DesktopScreen page={<div className="absolute inset-0 [&>[data-motion-layer]]:px-10 [&>[data-motion-layer]]:py-8">{children}</div>} />,
  );
}
