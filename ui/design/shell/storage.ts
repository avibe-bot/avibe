// Product components keep preferences (collapsed sidebar sections, the last-used
// Agent) in Web Storage, which every frame and Show Page on one origin shares. A
// frame's state must come from its fixtures alone, so each design document gets
// fresh in-memory storage before any product module loads (main.tsx imports this
// first); a frame that draws a remembered preference seeds it as a fixture.

class MemoryStorage implements Storage {
  private items = new Map<string, string>();
  get length() {
    return this.items.size;
  }
  clear() {
    this.items.clear();
  }
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  key(index: number) {
    return [...this.items.keys()][index] ?? null;
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
  setItem(key: string, value: string) {
    this.items.set(key, String(value));
  }
}

const local = new MemoryStorage();
for (const [name, storage] of [['localStorage', local], ['sessionStorage', new MemoryStorage()]] as const) {
  Object.defineProperty(window, name, { configurable: true, get: () => storage });
}

export const designStorage = {
  seed(entries: Record<string, string> = {}) {
    for (const [key, value] of Object.entries(entries)) local.setItem(key, value);
  },
};
