import type { SessionActionDescriptor } from './sessionActions';

// Touch surfaces do not advertise desktop keyboard chords. Keep the shared
// descriptor model intact and adapt only the mobile presentation.
export const mobileSessionActions = (actions: SessionActionDescriptor[]): SessionActionDescriptor[] =>
  actions.map((action) => (action.id === 'archive' ? { ...action, hint: undefined } : action));

// The chat header's title is already click-to-edit, so its menu never repeats
// Rename on any viewport.
export const chatHeaderSessionActions = (actions: SessionActionDescriptor[]): SessionActionDescriptor[] =>
  actions.filter((action) => action.id !== 'rename');

export const mobileChatSessionActions = (actions: SessionActionDescriptor[]): SessionActionDescriptor[] =>
  chatHeaderSessionActions(mobileSessionActions(actions));
