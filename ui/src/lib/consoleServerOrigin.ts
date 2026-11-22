// Where a page is after the console server's host or port was saved (Settings › Service).

const stripBrackets = (host: string) =>
  host.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host;
const isLoopback = (host: string) => host === '127.0.0.1' || host === '::1' || host === 'localhost';
/** A name rather than an address: the page came through a tunnel or a proxy, not to the bind itself. */
const isName = (host: string) => host !== 'localhost' && !host.includes(':') && !/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host);

export type ConsoleOriginAfterSave = {
  /** Where the console answers this browser now. Always plain HTTP: the server binds no TLS. */
  origin: string;
  /**
   * `stay`: the page is already there, or reaches Avibe by a name that the bind does not move.
   * `go`: move the page there. `tell`: the address is on the computer that runs Avibe and this
   * browser is not, so it is only shown.
   */
  action: 'stay' | 'go' | 'tell';
};

/**
 * A browser on the computer that runs Avibe stays on a loopback address when the host becomes a
 * network address: the server keeps listening on the loopback address of the same family, and on
 * an Avibe bound to an account the network address no longer opens Avibe at all.
 */
export function consoleOriginAfterSave(
  current: { hostname: string; port: string },
  saved: { host: string; port: number },
): ConsoleOriginAfterSave {
  const currentHostname = stripBrackets(current.hostname);
  const savedHost = stripBrackets(saved.host);
  const port = String(saved.port);
  if (isName(currentHostname)) {
    return { origin: `http://${current.hostname}${current.port ? `:${current.port}` : ''}`, action: 'stay' };
  }
  const onThisComputer = isLoopback(currentHostname);
  const bindsAll = savedHost === '0.0.0.0' || savedHost === '::' || savedHost === '';
  let hostname = savedHost;
  if (bindsAll) hostname = currentHostname;
  else if (onThisComputer && !isLoopback(savedHost)) {
    hostname = currentHostname === 'localhost' ? 'localhost' : savedHost.includes(':') ? '::1' : '127.0.0.1';
  }
  const origin = `http://${hostname.includes(':') ? `[${hostname}]` : hostname}:${port}`;
  if (hostname === currentHostname && port === current.port) return { origin, action: 'stay' };
  // A loopback address is only reachable from the computer itself.
  return { origin, action: !isLoopback(hostname) || onThisComputer ? 'go' : 'tell' };
}

/**
 * Whether the server can listen on `host`: an IP address (IPv6 with or without brackets), `*`, or a
 * well-formed host name. The rule of `POST /api/ui/reload`, checked before anything is saved, so that
 * a mistyped address is never written to the configuration.
 */
export function isListenableHost(value: string): boolean {
  const host = value.trim();
  if (host === '*' || host.toLowerCase() === 'localhost') return true;
  if (!host || host.includes('%')) return false;
  if (/^[\d.]+$/.test(host)) {
    const parts = host.split('.');
    return parts.length === 4 && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
  }
  try {
    if (host.includes(':')) {
      new URL(`http://[${stripBrackets(host)}]/`);
      return true;
    }
    // The URL parser gives the ASCII form of a name in another script.
    const name = new URL(`http://${host}/`).hostname.replace(/\.$/, '');
    const labels = name.split('.');
    return name.length <= 253
      && !/^\d+$/.test(labels[labels.length - 1])
      && labels.every((label) => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label));
  } catch {
    return false;
  }
}

/** `host:port` as an address is written, an IPv6 host in brackets. */
export function formatListenAddress(host: string, port: number): string {
  const bare = stripBrackets(host.trim());
  return `${bare.includes(':') ? `[${bare}]` : bare}:${port}`;
}
