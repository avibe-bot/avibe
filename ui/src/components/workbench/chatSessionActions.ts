import type { SessionActionDescriptor } from './sessionActions';

// Touch surfaces do not advertise desktop keyboard chords. Keep the shared
// descriptor model intact and adapt only the mobile presentation.
export const mobileSessionActions = (actions: SessionActionDescriptor[]): SessionActionDescriptor[] =>
  actions.map((action) => (action.id === 'archive' ? { ...action, hint: undefined } : action));

export const mobileChatSessionActions = (actions: SessionActionDescriptor[]): SessionActionDescriptor[] =>
  mobileSessionActions(actions).filter((action) => action.id !== 'rename');
