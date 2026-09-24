import path from 'node:path';

/**
 * A repository-relative path in POSIX form.
 *
 * Callers that compare a host path against something written in POSIX — a
 * Dockerfile's ``COPY`` sources, a ``path.posix`` join, a ``startsWith(`${dir}/`)``
 * prefix test — need the separator shed at the one place the host path becomes
 * repository-relative, because ``path.relative`` answers in the host's separator.
 * On Windows that is ``\``, so an un-normalized target compares unequal to every
 * POSIX form of itself and the comparison fails against correct input.
 *
 * ``platform`` selects the path flavour and exists so the Windows behaviour is
 * reachable from a test on any host: Node binds ``path`` to the running platform,
 * so ``path.win32`` is the only way to exercise the separator that breaks.
 */
export const repoRelativePosix = (from, to, platform = path) =>
  platform.relative(from, to).split(platform.sep).join('/');
