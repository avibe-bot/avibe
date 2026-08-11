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

    expect(ownership.epoch()).toBe(0);
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

  it('lets a broad read and a resource read order their overlapping payload', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const bootstrap = ownership.beginRead('projects-bootstrap');
    const project = ownership.beginRead('project:a');

    expect(ownership.isCurrent(bootstrap, ['projects-bootstrap', 'project:a'])).toBe(false);
    expect(ownership.isCurrent(project, ['projects-bootstrap', 'project:a'])).toBe(true);
  });
});
