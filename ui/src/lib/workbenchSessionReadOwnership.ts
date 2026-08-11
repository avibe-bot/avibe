/**
 * Orders asynchronous Workbench reads against accepted session mutations.
 *
 * A response is only allowed to commit when it belongs to the current mutation
 * epoch and has not been superseded for any resource it owns. The shared epoch
 * orders realtime mutations, while resource generations keep independent reads
 * from invalidating one another merely because they completed in another order.
 */
export type WorkbenchSessionReadStamp = {
  epoch: number;
  generation: number;
  resources: string[];
};

export type WorkbenchSessionReadOwnership = {
  beginRead: (resources?: string | readonly string[]) => WorkbenchSessionReadStamp;
  acceptMutation: () => number;
  isCurrent: (
    stamp: WorkbenchSessionReadStamp,
    resources?: string | readonly string[],
  ) => boolean;
  isLatestRead: (stamp: WorkbenchSessionReadStamp) => boolean;
  latestGeneration: (resource: string) => number | undefined;
  epoch: () => number;
};

export function createWorkbenchSessionReadOwnership(): WorkbenchSessionReadOwnership {
  let mutationEpoch = 0;
  let latestGeneration = 0;
  const latestByResource = new Map<string, number>();

  return {
    beginRead: (resources = 'default') => {
      const resourceList = typeof resources === 'string' ? [resources] : [...resources];
      if (resourceList.length === 0) throw new Error('A session read must own at least one resource');
      latestGeneration += 1;
      for (const resource of resourceList) latestByResource.set(resource, latestGeneration);
      return { epoch: mutationEpoch, generation: latestGeneration, resources: resourceList };
    },
    acceptMutation: () => {
      mutationEpoch += 1;
      return mutationEpoch;
    },
    isCurrent: (stamp, resources = stamp.resources) => {
      const resourceList = typeof resources === 'string' ? [resources] : resources;
      return (
        stamp.epoch === mutationEpoch &&
        resourceList.every((resource) => {
          const latest = latestByResource.get(resource);
          return latest === undefined || latest <= stamp.generation;
        })
      );
    },
    isLatestRead: (stamp) =>
      stamp.resources.every((resource) => latestByResource.get(resource) === stamp.generation),
    latestGeneration: (resource) => latestByResource.get(resource),
    epoch: () => mutationEpoch,
  };
}
