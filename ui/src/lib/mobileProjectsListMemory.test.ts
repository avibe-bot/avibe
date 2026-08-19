import { afterEach, describe, expect, it } from 'vitest';

import {
  MOBILE_SESSION_PAGE_SIZE,
  clearMobileProjectsListSnapshot,
  clearProjectVisibleCount,
  forgetMobileProjectsListUnlessPreserved,
  holdMobileProjectsListForChatReturn,
  isChatRoutePath,
  isMobileProjectsListHeldForChatReturn,
  markMobileProjectsListRestored,
  readMobileProjectsListSnapshot,
  rememberMobileProjectsListOnPageLeave,
  revealMoreVisibleCount,
  visibleSessionCountFor,
  appShellScrollElement,
  readAppShellScrollTop,
  writeAppShellScrollTop,
  writeMobileProjectsListSnapshot,
} from './mobileProjectsListMemory';

afterEach(() => {
  clearMobileProjectsListSnapshot();
});

describe('mobile projects list memory', () => {
  it('treats only /chat/* as a chat leave', () => {
    expect(isChatRoutePath('/chat/ses_1')).toBe(true);
    expect(isChatRoutePath('/projects')).toBe(false);
    expect(isChatRoutePath('/inbox')).toBe(false);
  });

  it('pages visible counts from the compact first window', () => {
    expect(visibleSessionCountFor({}, 'proj_a')).toBe(MOBILE_SESSION_PAGE_SIZE);
    expect(visibleSessionCountFor({ proj_a: 16 }, 'proj_a')).toBe(16);
    expect(revealMoreVisibleCount(8)).toBe(16);
    expect(revealMoreVisibleCount(16)).toBe(24);
  });

  it("collapsing a project forgets only that project's revealed window", () => {
    const counts = { proj_a: 24, proj_b: 16 };
    expect(clearProjectVisibleCount(counts, 'proj_a')).toEqual({ proj_b: 16 });
    expect(clearProjectVisibleCount(counts, 'proj_missing')).toBe(counts);
  });

  it('holds a snapshot across chat, then forgets it after restore plus a non-chat leave', () => {
    holdMobileProjectsListForChatReturn({ visibleCounts: { proj_a: 24 }, scrollTop: 320 });
    expect(isMobileProjectsListHeldForChatReturn()).toBe(true);
    forgetMobileProjectsListUnlessPreserved('/chat/ses_12');
    expect(readMobileProjectsListSnapshot()).toEqual({
      visibleCounts: { proj_a: 24 },
      scrollTop: 320,
    });

    markMobileProjectsListRestored();
    expect(isMobileProjectsListHeldForChatReturn()).toBe(false);
    forgetMobileProjectsListUnlessPreserved('/inbox');
    expect(readMobileProjectsListSnapshot()).toEqual({ visibleCounts: {}, scrollTop: 0 });
  });

  it('forgets a held snapshot once the user leaves chat for a non-projects page', () => {
    holdMobileProjectsListForChatReturn({ visibleCounts: { proj_a: 16 }, scrollTop: 80 });
    forgetMobileProjectsListUnlessPreserved('/inbox');
    expect(readMobileProjectsListSnapshot()).toEqual({ visibleCounts: {}, scrollTop: 0 });
    expect(isMobileProjectsListHeldForChatReturn()).toBe(false);
  });

  it('writes the live window back on a same-page remount instead of dropping it', () => {
    writeMobileProjectsListSnapshot({ visibleCounts: {}, scrollTop: 0 });
    rememberMobileProjectsListOnPageLeave({ visibleCounts: { proj_a: 16 }, scrollTop: 80 });
    expect(readMobileProjectsListSnapshot()).toEqual({
      visibleCounts: { proj_a: 16 },
      scrollTop: 80,
    });
  });

  it('does not overwrite a chat hold with the page-leave snapshot', () => {
    holdMobileProjectsListForChatReturn({ visibleCounts: { proj_a: 16 }, scrollTop: 240 });
    rememberMobileProjectsListOnPageLeave({ visibleCounts: {}, scrollTop: 0 });
    expect(readMobileProjectsListSnapshot()).toEqual({
      visibleCounts: { proj_a: 16 },
      scrollTop: 240,
    });
  });

  it('forgets an unrestored snapshot when leaving a non-chat page', () => {
    writeMobileProjectsListSnapshot({ visibleCounts: { proj_a: 16 }, scrollTop: 80 });
    forgetMobileProjectsListUnlessPreserved('/inbox');
    expect(readMobileProjectsListSnapshot()).toEqual({ visibleCounts: {}, scrollTop: 0 });
  });

  it('rejects a negative or non-finite scroll offset', () => {
    writeMobileProjectsListSnapshot({ visibleCounts: {}, scrollTop: -12 });
    expect(readMobileProjectsListSnapshot().scrollTop).toBe(0);
    writeMobileProjectsListSnapshot({ visibleCounts: {}, scrollTop: Number.NaN });
    expect(readMobileProjectsListSnapshot().scrollTop).toBe(0);
  });

  it('reads and writes the shell scroll owner through a duck-typed node', () => {
    const node = { scrollTop: 12 };
    writeAppShellScrollTop(node, 180);
    expect(readAppShellScrollTop(node)).toBe(180);
    expect(appShellScrollElement({ getElementById: () => node })).toBe(node);
    expect(readAppShellScrollTop(null)).toBe(0);
  });
});
