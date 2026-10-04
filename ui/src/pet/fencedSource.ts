/**
 * One input of the pet, read from an authoritative API and kept fresh by live
 * events. Every read is fenced twice:
 *
 * - by key: a result applies only while its key (the bound session, or the
 *   switcher's constant key while it is open) is still current, so switching
 *   from A to B never shows A's state;
 * - by order: starting a read and merging a live event both advance the
 *   generation, and a result applies only if nothing newer happened on this
 *   source since it started. A merge that overtakes an in-flight read schedules
 *   one trailing read, so the source still converges on the server's state.
 *
 * Reads coalesce to at most one in flight plus one trailing.
 */
export type FencedSourceOptions<K, T> = {
  read: (key: K) => Promise<T>;
  apply: (key: K, value: T) => void;
  /** Called only for a fresh read, so a late failure for an old key is dropped. */
  fail?: (key: K, error: unknown) => void;
};

export class FencedSource<K, T> {
  private generation = 0;
  /** The generation of the read in flight for the current key, or 0. */
  private inFlight = 0;
  private trailing = false;
  private key: K | null = null;

  private readonly options: FencedSourceOptions<K, T>;

  constructor(options: FencedSourceOptions<K, T>) {
    this.options = options;
  }

  /** Re-read from the server, coalesced. */
  refresh(): void {
    if (this.inFlight !== 0) {
      this.trailing = true;
      return;
    }
    void this.run();
  }

  /**
   * A live event was merged into this source's state. Any read already in
   * flight started before it and is dropped; one trailing read follows.
   */
  noteLiveMerge(): void {
    this.generation += 1;
    if (this.inFlight !== 0) this.trailing = true;
  }

  /**
   * Point the source at `key` (null: nothing to read). A change drops whatever
   * is in flight or queued for the old key.
   */
  setKey(key: K | null): void {
    if (Object.is(key, this.key)) return;
    this.key = key;
    this.generation += 1;
    this.trailing = false;
    // A read for the old key no longer holds the slot: the new key reads now.
    this.inFlight = 0;
  }

  private async run(): Promise<void> {
    const key = this.key;
    if (key === null) return;
    const generation = ++this.generation;
    this.inFlight = generation;
    try {
      const value = await this.options.read(key);
      if (this.isFresh(key, generation)) this.options.apply(key, value);
    } catch (error) {
      if (this.isFresh(key, generation)) this.options.fail?.(key, error);
    } finally {
      // Only the read that still holds the slot releases it and runs the
      // trailing read; a read abandoned by a key change does neither.
      if (this.inFlight === generation) {
        this.inFlight = 0;
        if (this.trailing) {
          this.trailing = false;
          void this.run();
        }
      }
    }
  }

  private isFresh(key: K, generation: number): boolean {
    return generation === this.generation && Object.is(key, this.key);
  }
}
