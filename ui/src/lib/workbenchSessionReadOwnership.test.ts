import { describe, expect, it } from 'vitest';

import { createWorkbenchSessionReadOwnership } from './workbenchSessionReadOwnership';

describe('Workbench session read ownership', () => {
  it('accepts only the newest read in the current mutation epoch', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const older = ownership.beginRead();
    const newer = ownership.beginRead();

    expect(ownership.isCurrent(older)).toBe(false);
    expect(ownership.isCurrent(newer)).toBe(true);
  });

  it('invalidates reads issued before an accepted session mutation', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const read = ownership.beginRead();

    expect(ownership.acceptMutation()).toBe(1);
    expect(ownership.isCurrent(read)).toBe(false);
    expect(ownership.isLatestRead(read)).toBe(true);
  });

  it('tracks read generations independently for independent resources', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const projectA = ownership.beginRead('project:a');
    const projectB = ownership.beginRead('project:b');

    expect(ownership.isCurrent(projectA)).toBe(true);
    expect(ownership.isCurrent(projectB)).toBe(true);

    const newerProjectA = ownership.beginRead('project:a');
    expect(ownership.isCurrent(projectA)).toBe(false);
    expect(ownership.isCurrent(newerProjectA)).toBe(true);
    expect(ownership.isCurrent(projectB)).toBe(true);
  });

  it('tracks mutation versions independently for independent resources', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const projectA = ownership.beginRead('project:a');
    const projectB = ownership.beginRead('project:b');

    ownership.acceptMutation('project:a');

    expect(ownership.isCurrent(projectA)).toBe(false);
    expect(ownership.isCurrent(projectB)).toBe(true);
  });

  it('lets a later broad read claim a dynamic row resource', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const targeted = ownership.beginRead('session:a');
    const broad = ownership.beginRead('feed');

    ownership.claimRead(broad, 'session:a');

    expect(ownership.isCurrent(targeted, 'session:a')).toBe(false);
    expect(ownership.isCurrent(broad, 'session:a')).toBe(true);
  });

  it('does not let an older broad read claim over a later targeted read', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const broad = ownership.beginRead('feed');
    const targeted = ownership.beginRead('session:a');

    ownership.claimRead(broad, 'session:a');

    expect(ownership.isCurrent(broad, 'session:a')).toBe(false);
    expect(ownership.isCurrent(targeted, 'session:a')).toBe(true);
  });

  it('lets a broad read and a resource read order their overlapping payload', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const bootstrap = ownership.beginRead('projects-bootstrap');
    const project = ownership.beginRead('project:a');

    expect(ownership.isCurrent(bootstrap, ['projects-bootstrap', 'project:a'])).toBe(false);
    expect(ownership.isCurrent(project, ['projects-bootstrap', 'project:a'])).toBe(true);
  });

  it('exposes the latest generation for operation-specific retry decisions', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const reconcile = ownership.beginRead(['inbox-feed', 'inbox-feed-reconcile']);
    const cursor = ownership.beginRead(['inbox-feed', 'inbox-feed-cursor']);

    expect(ownership.latestGeneration('inbox-feed-cursor')).toBe(cursor.generation);
    expect(ownership.latestGeneration('inbox-feed-reconcile')).toBe(reconcile.generation);
  });
});
