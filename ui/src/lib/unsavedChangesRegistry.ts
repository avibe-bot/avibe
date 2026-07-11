export type UnsavedChangesRegistrationId = string;

export type UnsavedChangesRegistry = Map<UnsavedChangesRegistrationId, string>;

export interface UnsavedChangesActionAuthorization {
  /** Run one synchronous router navigation without prompting again after the action was confirmed. */
  runNavigation: (navigation: () => void) => void;
}

export interface UnsavedChangesNavigationGate {
  authorize: (
    message: string | null,
    confirm: (message: string) => boolean,
  ) => UnsavedChangesActionAuthorization | null;
  shouldBlock: (hasUnsavedChanges: boolean) => boolean;
}

export function createUnsavedChangesNavigationGate(): UnsavedChangesNavigationGate {
  let authorizedNavigation: symbol | null = null;

  return {
    authorize(message, confirm) {
      if (message !== null && !confirm(message)) return null;

      const token = message === null ? null : Symbol('unsaved-changes-route-action');
      return {
        runNavigation(navigation) {
          if (token !== null) authorizedNavigation = token;
          try {
            navigation();
          } finally {
            // Never leave a bypass behind if the callback did not issue a synchronous navigation.
            if (token !== null && authorizedNavigation === token) authorizedNavigation = null;
          }
        },
      };
    },
    shouldBlock(hasUnsavedChanges) {
      if (authorizedNavigation !== null) {
        authorizedNavigation = null;
        return false;
      }
      return hasUnsavedChanges;
    },
  };
}

export function setUnsavedChangesRegistration(
  registry: UnsavedChangesRegistry,
  id: UnsavedChangesRegistrationId,
  message: string | null,
): void {
  if (message === null) {
    registry.delete(id);
    return;
  }
  registry.set(id, message);
}

export function getUnsavedChangesMessage(registry: UnsavedChangesRegistry): string | null {
  let active: string | null = null;
  for (const message of registry.values()) active = message;
  return active;
}
