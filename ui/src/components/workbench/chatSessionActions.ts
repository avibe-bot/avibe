import type { SessionActionDescriptor } from './sessionActions';

export const mobileChatSessionActions = (actions: SessionActionDescriptor[]): SessionActionDescriptor[] =>
  actions.filter((action) => action.id !== 'rename');
