// Whether Setup offers its optional remote-access step after the last screen (ui/design:
// setup/remote-access/*). Only where turning it on can work from here: a browser on this computer
// or an admitted private address, on an instance that is not bound yet and is not an
// organization's. The Desktop App and the Avibe App bring their own account sign-in; a remote
// session is already bound.
export function offersRemoteAccessStep(facts: {
  /** The session comes from elsewhere (a bound instance's remote sign-in). */
  remote: boolean;
  desktopShell: boolean;
  nativeShell: boolean;
  /** `/api/remote-access/status`: bound or not; null when it could not be read. */
  paired: boolean | null;
  /** This computer's own record says the Avibe is an organization's: a person's claim is not its way. */
  organization?: boolean;
}): boolean {
  return !facts.remote && !facts.desktopShell && !facts.nativeShell && !facts.organization && facts.paired === false;
}
