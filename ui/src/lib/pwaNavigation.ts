function normalizeHostname(hostname: string): string {
  return hostname.trim().toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = normalizeHostname(hostname);
  if (normalized === 'localhost' || normalized.endsWith('.localhost') || normalized === '::1') {
    return true;
  }

  const octets = normalized.split('.');
  return (
    octets.length === 4 &&
    octets[0] === '127' &&
    octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255)
  );
}

export function shouldBlockPwaLoopbackLink(href: string, currentHref: string): boolean {
  try {
    const current = new URL(currentHref);
    const target = new URL(href, current);
    if (target.protocol !== 'http:' && target.protocol !== 'https:') return false;

    // A loopback link is valid when the app itself is being used on loopback.
    // From a remote iPhone PWA it points at the phone, not the Avibe host.
    return !isLoopbackHostname(current.hostname) && isLoopbackHostname(target.hostname);
  } catch {
    return false;
  }
}
