import { SETUP_SCREENS, type SetupScreenId } from './setupFlow';

/** Build-time registration only. Capability, saved intent and runtime never remove a step. */
export const registeredSetupSequence = (registry: Partial<Record<SetupScreenId, true>>) =>
  SETUP_SCREENS.filter((id) => registry[id]);

export const SETUP_REGISTERED_SCREENS = registeredSetupSequence({
  intro: true,
  providers: true,
  assistants: true,
});
