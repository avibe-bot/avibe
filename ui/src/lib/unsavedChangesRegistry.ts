export type UnsavedChangesRegistrationId = string;

export type UnsavedChangesRegistry = Map<UnsavedChangesRegistrationId, string>;

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
