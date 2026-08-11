/**
 * Orders asynchronous Workbench reads against accepted session mutations.
 *
 * A response is only allowed to commit when it belongs to the current
 * mutation epoch and is the newest read issued by this data owner. This keeps
 * HTTP completion order from deciding whether a stale snapshot wins over a
 * realtime event or a newer read.
 */
export type WorkbenchSessionReadStamp = {
  epoch: number;
  generation: number;
};

export type WorkbenchSessionReadOwnership = {
  beginRead: () => WorkbenchSessionReadStamp;
  acceptMutation: () => number;
  isCurrent: (stamp: WorkbenchSessionReadStamp) => boolean;
  isLatestRead: (stamp: WorkbenchSessionReadStamp) => boolean;
  epoch: () => number;
};

export function createWorkbenchSessionReadOwnership(): WorkbenchSessionReadOwnership {
  let mutationEpoch = 0;
  let latestGeneration = 0;

  return {
    beginRead: () => {
      latestGeneration += 1;
      return { epoch: mutationEpoch, generation: latestGeneration };
    },
    acceptMutation: () => {
      mutationEpoch += 1;
      return mutationEpoch;
    },
    isCurrent: (stamp) =>
      stamp.epoch === mutationEpoch && stamp.generation === latestGeneration,
    isLatestRead: (stamp) => stamp.generation === latestGeneration,
    epoch: () => mutationEpoch,
  };
}
