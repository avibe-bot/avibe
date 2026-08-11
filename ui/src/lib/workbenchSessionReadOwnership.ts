/**
 * Orders asynchronous Workbench reads against accepted session mutations.
 *
 * A response is only allowed to commit when it belongs to the current mutation
 * version and has not been superseded for any resource it owns.
 * Resource-scoped mutation versions and read generations keep independent
 * projects and data slices from invalidating one another.
 */
export type WorkbenchSessionReadStamp = {
  epoch: number;
  generation: number;
  resources: string[];
};

export type WorkbenchSessionReadOwnership = {
  beginRead: (resources?: string | readonly string[]) => WorkbenchSessionReadStamp;
  claimRead: (
    stamp: WorkbenchSessionReadStamp,
    resources: string | readonly string[],
  ) => void;
  acceptMutation: (resources?: string | readonly string[]) => number;
  isMutationCurrent: (
    stamp: WorkbenchSessionReadStamp,
    resources?: string | readonly string[],
  ) => boolean;
  isCurrent: (
    stamp: WorkbenchSessionReadStamp,
    resources?: string | readonly string[],
  ) => boolean;
  isLatestRead: (stamp: WorkbenchSessionReadStamp) => boolean;
  latestGeneration: (resource: string) => number | undefined;
};

export function createWorkbenchSessionReadOwnership(): WorkbenchSessionReadOwnership {
  let mutationEpoch = 0;
  let latestGeneration = 0;
  const latestByResource = new Map<string, number>();
  const mutationByResource = new Map<string, number>();

  return {
    beginRead: (resources = 'default') => {
      const resourceList = typeof resources === 'string' ? [resources] : [...resources];
      if (resourceList.length === 0) throw new Error('A session read must own at least one resource');
      latestGeneration += 1;
      for (const resource of resourceList) latestByResource.set(resource, latestGeneration);
      return {
        epoch: mutationEpoch,
        generation: latestGeneration,
        resources: resourceList,
      };
    },
    claimRead: (stamp, resources) => {
      const resourceList = typeof resources === 'string' ? [resources] : resources;
      if (resourceList.length === 0) throw new Error('A session read must claim at least one resource');
      for (const resource of resourceList) {
        const latest = latestByResource.get(resource) ?? 0;
        if (stamp.generation > latest) latestByResource.set(resource, stamp.generation);
      }
    },
    acceptMutation: (resources = 'default') => {
      const resourceList = typeof resources === 'string' ? [resources] : resources;
      if (resourceList.length === 0) throw new Error('A session mutation must own at least one resource');
      mutationEpoch += 1;
      for (const resource of resourceList) mutationByResource.set(resource, mutationEpoch);
      return mutationEpoch;
    },
    isMutationCurrent: (stamp, resources = stamp.resources) => {
      const resourceList = typeof resources === 'string' ? [resources] : resources;
      return resourceList.every(
        (resource) => (mutationByResource.get(resource) ?? 0) <= stamp.epoch,
      );
    },
    isCurrent: (stamp, resources = stamp.resources) => {
      const resourceList = typeof resources === 'string' ? [resources] : resources;
      return (
        resourceList.every(
          (resource) => (mutationByResource.get(resource) ?? 0) <= stamp.epoch,
        ) &&
        resourceList.every((resource) => {
          const latest = latestByResource.get(resource);
          return latest === undefined || latest <= stamp.generation;
        })
      );
    },
    isLatestRead: (stamp) =>
      stamp.resources.every((resource) => latestByResource.get(resource) === stamp.generation),
    latestGeneration: (resource) => latestByResource.get(resource),
  };
}
