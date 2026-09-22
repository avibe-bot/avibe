import { SETUP_SCREENS, type SetupScreenId } from './setupFlow';

/** Build-time registration only. Capability, saved intent and runtime never remove a step. */
export const registeredSetupSequence = (registry: Partial<Record<SetupScreenId, true>>) =>
  SETUP_SCREENS.filter((id) => registry[id]);

// L2 adds providers when its implementation ships. There is no empty interim slot.
export const SETUP_REGISTERED_SCREENS = registeredSetupSequence({ intro: true, assistants: true });
