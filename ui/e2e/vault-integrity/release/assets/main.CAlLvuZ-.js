const Xn = "avibe.vault.crypto";
const ye = {
  sandboxVersion: "0.1.0",
  // Resource hashes are the authority; this source-input identity additionally
  // binds ready/handshake and same-release top-level ceremony pairing.
  buildHash: "185708641db830f2a02fa2f2d7169eff9aec0d3ec0564e2a4216761300ee3d67"
};
class _ extends Error {
  code;
  retryable;
  constructor(t, n, r = !1) {
    super(n ?? t), this.code = t, this.retryable = r;
  }
}
function Ld(e) {
  let t;
  try {
    t = new URL(e);
  } catch {
    return !1;
  }
  const n = t.hostname.toLowerCase();
  return n === "localhost" || n === "127.0.0.1" || n === "::1" || n === "[::1]" ? t.protocol === "http:" || t.protocol === "https:" : !!(t.protocol === "https:" && (n === "avibe.bot" || n.endsWith(".avibe.bot")));
}
function Om(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return t.channel === Xn && t.version === 2 && typeof t.id == "string" && t.id.length > 0 && typeof t.op == "string";
}
const Cm = /^[A-Za-z0-9_-]{16,128}$/;
function yh(e) {
  return typeof e == "object" && e !== null;
}
function Rm(e) {
  return yh(e) ? e.channel === Xn && e.version === 2 && e.kind === "event" && e.event === "confirm.surface" : !1;
}
class Lm {
  handlers = /* @__PURE__ */ new Map();
  pinnedParentOrigin = null;
  pinnedSource = null;
  handshakeNonce = null;
  activeSurfaceSlots = /* @__PURE__ */ new Map();
  register(t, n) {
    this.handlers.set(t, n);
  }
  emit(t, n) {
    !this.pinnedSource || this.pinnedParentOrigin === null || this.pinnedSource.postMessage(
      {
        channel: Xn,
        version: 2,
        kind: "event",
        event: t,
        ...n !== void 0 ? { payload: n } : {}
      },
      this.pinnedParentOrigin
    );
  }
  /** Start listening + announce readiness to the embedder. */
  start() {
    window.addEventListener("message", (t) => void this.onMessage(t)), window.parent !== window && this.announceReady();
  }
  announceReady() {
    this.postToParent(window.parent, {
      type: "ready",
      channel: Xn,
      version: 2,
      build: ye,
      capabilities: {
        operations: Array.from(this.handlers.keys())
      }
    });
  }
  async onMessage(t) {
    if (window.parent === window || t.source !== window.parent || !Ld(t.origin)) return;
    if (Rm(t.data)) {
      if (t.origin === this.pinnedParentOrigin && t.source === this.pinnedSource) {
        const c = t.data.surface ?? t.data.payload;
        c !== void 0 && this.recordParentSurfaceEvent(t.data, c);
      }
      return;
    }
    if (!Om(t.data)) return;
    const n = t.data, r = this.pinnedParentOrigin === null;
    if (r) {
      const c = this.validateInitialHandshake(n, t.origin);
      if (!c) return;
      this.pinnedParentOrigin = t.origin, this.pinnedSource = t.source, this.handshakeNonce = c;
    } else if (t.origin !== this.pinnedParentOrigin || t.source !== this.pinnedSource || this.handshakeNonce === null)
      return;
    const i = this.handlers.get(n.op);
    if (!i) {
      this.reply(t.source, this.fail(n.id, "unknown_operation")), r && this.clearPin();
      return;
    }
    if (n.authorizationWindow !== void 0 && (typeof n.authorizationWindow != "string" || !Cm.test(n.authorizationWindow))) {
      this.reply(t.source, this.fail(n.id, "invalid_payload", "authorizationWindow is invalid")), r && this.clearPin();
      return;
    }
    let o = n.authorizationWindow;
    const s = {
      ...n.surface !== void 0 ? { surface: this.parentSurface(n.surface) } : {}
    };
    this.activeSurfaceSlots.set(n.id, s);
    const a = {
      ...s.surface ? { surface: s.surface } : {},
      latestSurface: () => s.surface,
      takeAuthorizationWindow: () => {
        const c = o;
        return o = void 0, c;
      }
    };
    try {
      const c = await i(n.payload, a);
      this.reply(t.source, {
        channel: Xn,
        version: 2,
        id: n.id,
        ok: !0,
        result: c
      });
    } catch (c) {
      const l = c instanceof _ ? c.code : "internal_error", d = c instanceof _ ? c.retryable : !1, u = c instanceof _ ? c.message : void 0;
      this.reply(t.source, this.fail(n.id, l, u, d)), r && this.clearPin();
    } finally {
      this.activeSurfaceSlots.delete(n.id);
    }
  }
  validateInitialHandshake(t, n) {
    if (t.op !== "handshake" || !yh(t.payload)) return null;
    const r = t.payload.parentOrigin, i = t.payload.nonce;
    return typeof r != "string" || r !== n || !Ld(r) || typeof i != "string" || i.length < 16 || i.length > 256 ? null : i;
  }
  clearPin() {
    this.pinnedParentOrigin = null, this.pinnedSource = null, this.handshakeNonce = null, this.activeSurfaceSlots.clear();
  }
  parentSurface(t) {
    return { value: t, receivedAt: Date.now() };
  }
  recordParentSurfaceEvent(t, n) {
    const r = typeof t.id == "string" ? t.id : typeof t.requestId == "string" ? t.requestId : null;
    if (r) {
      const o = this.activeSurfaceSlots.get(r);
      o && (o.surface = this.parentSurface(n));
      return;
    }
    if (this.activeSurfaceSlots.size !== 1) return;
    const [i] = this.activeSurfaceSlots.values();
    i && (i.surface = this.parentSurface(n));
  }
  fail(t, n, r, i = !1) {
    return { channel: Xn, version: 2, id: t, ok: !1, error: { code: n, message: r, retryable: i } };
  }
  reply(t, n) {
    !t || this.pinnedParentOrigin === null || t.postMessage(n, this.pinnedParentOrigin);
  }
  postToParent(t, n) {
    t.postMessage(n, "*");
  }
}
function Xc() {
  return new URLSearchParams((window.location.hash ?? "").slice(1));
}
function wh(e, t, n, r = {}) {
  const i = new URL(window.location.href);
  return ye.buildHash !== "dev" && (i.pathname = `/v/${ye.buildHash}/index.html`), i.search = "", i.hash = new URLSearchParams({ mode: e, id: t, ...n, ...r }).toString(), i.toString();
}
function Hr(e) {
  if (!e || typeof e != "object") return !1;
  const t = e;
  return t.buildHash === ye.buildHash && t.sandboxVersion === ye.sandboxVersion;
}
async function Hm() {
  if (ye.buildHash === "dev") return;
  const e = new URL(window.location.href);
  if (e.pathname !== `/v/${ye.buildHash}/index.html` || e.search)
    throw new Error("Sandbox release document URL mismatch");
  const t = document.querySelectorAll('link[rel="stylesheet"]');
  if (t.length !== 1 || !t[0].integrity.startsWith("sha256-") || !t[0].href.startsWith(`${e.origin}/v/${ye.buildHash}/assets/`))
    throw new Error("Sandbox release stylesheet is not pinned");
  const n = t[0];
  n.sheet || await new Promise((r, i) => {
    const o = () => a(new Error("Sandbox release stylesheet failed integrity or loading")), s = () => n.sheet ? a() : o(), a = (l) => {
      clearTimeout(c), n.removeEventListener("load", s), n.removeEventListener("error", o), window.removeEventListener("load", s), window.removeEventListener("pagehide", o), l ? i(l) : r();
    }, c = setTimeout(o, 1e4);
    n.addEventListener("load", s, { once: !0 }), n.addEventListener("error", o, { once: !0 }), window.addEventListener("load", s, { once: !0 }), window.addEventListener("pagehide", o, { once: !0 }), (n.sheet || document.readyState === "complete") && s();
  });
}
class St extends Error {
  constructor(t) {
    let n;
    t instanceof Error ? n = t.message : typeof t == "string" ? n = t : n = "", super(n), this.name = this.constructor.name;
  }
}
class $t extends St {
}
class Mo extends St {
}
class io extends St {
}
class Nm extends St {
}
class Um extends St {
}
class Dm extends St {
}
class zm extends St {
}
class Mm extends St {
}
class Km extends St {
}
class Fm extends St {
}
class Jc extends St {
}
const jm = {}, gh = Vm(globalThis, jm);
function Vm(e, t) {
  return new Proxy(e, {
    get(n, r, i) {
      return r in t ? t[r] : e[r];
    },
    set(n, r, i) {
      return r in t && delete t[r], e[r] = i, !0;
    },
    deleteProperty(n, r) {
      let i = !1;
      return r in t && (delete t[r], i = !0), r in e && (delete e[r], i = !0), i;
    },
    ownKeys(n) {
      const r = Reflect.ownKeys(e), i = Reflect.ownKeys(t), o = new Set(i);
      return [...r.filter((s) => !o.has(s)), ...i];
    },
    defineProperty(n, r, i) {
      return r in t && delete t[r], Reflect.defineProperty(e, r, i), !0;
    },
    getOwnPropertyDescriptor(n, r) {
      return r in t ? Reflect.getOwnPropertyDescriptor(t, r) : Reflect.getOwnPropertyDescriptor(e, r);
    },
    has(n, r) {
      return r in t || r in e;
    }
  });
}
async function qm() {
  if (gh !== void 0 && globalThis.crypto !== void 0)
    return globalThis.crypto.subtle;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Bm);
    return e.subtle;
  } catch (e) {
    throw new Jc(e);
  }
}
class Qc {
  constructor() {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    });
  }
  async _setup() {
    this._api === void 0 && (this._api = await qm());
  }
}
const pn = {
  Base: 0,
  Psk: 1,
  Auth: 2,
  AuthPsk: 3
}, Hd = {
  DhkemX25519HkdfSha256: 32
}, mh = {
  HkdfSha256: 1
}, el = {
  Aes128Gcm: 1,
  Aes256Gcm: 2,
  ExportOnly: 65535
}, Qo = 8192, Wm = 268435456, Nd = 32, Fe = /* @__PURE__ */ new Uint8Array(0), Ot = 0n, Er = 1n, oo = 2n, Gm = /* @__PURE__ */ new Uint8Array([
  75,
  69,
  77,
  0,
  0
]), Ud = /* @__PURE__ */ new Uint8Array([
  72,
  80,
  75,
  69,
  45,
  118,
  49
]);
function Ko(e) {
  return new Uint8Array(de(e));
}
function de(e) {
  return e instanceof ArrayBuffer ? e : ArrayBuffer.isView(e) ? new Uint8Array(e.buffer, e.byteOffset, e.byteLength).slice().buffer : new Uint8Array(e).slice().buffer;
}
class Zm extends Qc {
  constructor() {
    super(), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: mh.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Fe
    }), Object.defineProperty(this, "algHash", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: {
        name: "HMAC",
        hash: "SHA-256",
        length: 256
      }
    });
  }
  init(t) {
    this._suiteId = t;
  }
  buildLabeledIkm(t, n) {
    this._checkInit();
    const r = new Uint8Array(7 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return r.set(Ud, 0), r.set(this._suiteId, 7), r.set(t, 7 + this._suiteId.byteLength), r.set(n, 7 + this._suiteId.byteLength + t.byteLength), r;
  }
  buildLabeledInfo(t, n, r) {
    this._checkInit();
    const i = new Uint8Array(9 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return i.set(new Uint8Array([0, r]), 0), i.set(Ud, 2), i.set(this._suiteId, 9), i.set(t, 9 + this._suiteId.byteLength), i.set(n, 9 + this._suiteId.byteLength + t.byteLength), i;
  }
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : de(t);
    if (r.byteLength !== this.hashSize)
      throw new $t("The salt length must be the same as the hashSize");
    const i = de(n), o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
  async expand(t, n, r) {
    await this._setup();
    const i = de(t), o = await this._api.importKey("raw", i, this.algHash, !1, [
      "sign"
    ]), s = new ArrayBuffer(r), a = new Uint8Array(s);
    let c = Fe;
    const l = Ko(n), d = new Uint8Array(1);
    if (r > 255 * this.hashSize)
      throw new Error("Entropy limit reached");
    const u = new Uint8Array(this.hashSize + l.length + 1);
    for (let f = 1, h = 0; h < a.length; f++)
      d[0] = f, u.set(c, 0), u.set(l, c.length), u.set(d, c.length + l.length), c = new Uint8Array(await this._api.sign("HMAC", o, u.slice(0, c.length + l.length + 1))), a.length - h >= c.length ? (a.set(c, h), h += c.length) : (a.set(c.slice(0, a.length - h), h), h += a.length - h);
    return s;
  }
  async extractAndExpand(t, n, r, i) {
    await this._setup();
    const o = de(n), s = await this._api.importKey("raw", o, "HKDF", !1, ["deriveBits"]);
    return await this._api.deriveBits({
      name: "HKDF",
      hash: this.algHash.hash,
      salt: de(t),
      info: de(r)
    }, s, i * 8);
  }
  async labeledExtract(t, n, r) {
    return await this.extract(t, this.buildLabeledIkm(n, r));
  }
  async labeledExpand(t, n, r, i) {
    return await this.expand(t, this.buildLabeledInfo(n, r, i), i);
  }
  _checkInit() {
    if (this._suiteId === Fe)
      throw new Error("Not initialized. Call init()");
  }
}
class bh extends Zm {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: mh.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 32
    }), Object.defineProperty(this, "algHash", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: {
        name: "HMAC",
        hash: "SHA-256",
        length: 256
      }
    });
  }
}
const pi = (e) => typeof e == "object" && e !== null && typeof e.privateKey == "object" && typeof e.publicKey == "object";
function Ti(e, t) {
  if (t <= 0)
    throw new Error("i2Osp: too small size");
  if (e >= 256 ** t)
    throw new Error("i2Osp: too large integer");
  const n = new Uint8Array(t);
  for (let r = 0; r < t && e; r++)
    n[t - (r + 1)] = e % 256, e = Math.floor(e / 256);
  return n;
}
function so(e, t) {
  const n = new Uint8Array(e.length + t.length);
  return n.set(e, 0), n.set(t, e.length), n;
}
function Dd(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = atob(t), r = new Uint8Array(n.length);
  for (let i = 0; i < n.length; i++)
    r[i] = n.charCodeAt(i);
  return r;
}
async function Ym() {
  if (typeof gh < "u" && globalThis.crypto !== void 0)
    return globalThis.crypto;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Bm);
    return e;
  } catch {
    throw new Error("failed to load Crypto");
  }
}
function Xm(e, t) {
  if (e.byteLength !== t.byteLength)
    throw new Error("xor: different length inputs");
  const n = new Uint8Array(e.byteLength);
  for (let r = 0; r < e.byteLength; r++)
    n[r] = e[r] ^ t[r];
  return n;
}
const Jm = /* @__PURE__ */ new Uint8Array([
  101,
  97,
  101,
  95,
  112,
  114,
  107
]), Qm = /* @__PURE__ */ new Uint8Array([
  115,
  104,
  97,
  114,
  101,
  100,
  95,
  115,
  101,
  99,
  114,
  101,
  116
]);
function eb(e, t, n) {
  const r = new Uint8Array(e.length + t.length + n.length);
  return r.set(e, 0), r.set(t, e.length), r.set(n, e.length + t.length), r;
}
class tb {
  constructor(t, n, r) {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "secretSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "encSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "publicKeySize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "privateKeySize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_prim", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_kdf", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.id = t, this._prim = n, this._kdf = r;
    const i = new Uint8Array(Gm);
    i.set(Ti(this.id, 2), 3), this._kdf.init(i);
  }
  async serializePublicKey(t) {
    return await this._prim.serializePublicKey(t);
  }
  async deserializePublicKey(t) {
    return await this._prim.deserializePublicKey(de(t));
  }
  async serializePrivateKey(t) {
    return await this._prim.serializePrivateKey(t);
  }
  async deserializePrivateKey(t) {
    return await this._prim.deserializePrivateKey(de(t));
  }
  async importKey(t, n, r = !0) {
    return await this._prim.importKey(t, n, r);
  }
  async generateKeyPair() {
    return await this._prim.generateKeyPair();
  }
  async deriveKeyPair(t) {
    const n = de(t);
    if (n.byteLength > Qo)
      throw new $t("Too long ikm");
    return await this._prim.deriveKeyPair(n);
  }
  async encap(t) {
    let n;
    t.ekm === void 0 ? n = await this.generateKeyPair() : pi(t.ekm) ? n = t.ekm : n = await this.deriveKeyPair(t.ekm);
    const r = await this._prim.serializePublicKey(n.publicKey), i = await this._prim.serializePublicKey(t.recipientPublicKey);
    try {
      let o;
      if (t.senderKey === void 0)
        o = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey));
      else {
        const c = pi(t.senderKey) ? t.senderKey.privateKey : t.senderKey, l = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey)), d = new Uint8Array(await this._prim.dh(c, t.recipientPublicKey));
        o = so(l, d);
      }
      let s;
      if (t.senderKey === void 0)
        s = so(new Uint8Array(r), new Uint8Array(i));
      else {
        const c = pi(t.senderKey) ? t.senderKey.publicKey : await this._prim.derivePublicKey(t.senderKey), l = await this._prim.serializePublicKey(c);
        s = eb(new Uint8Array(r), new Uint8Array(i), new Uint8Array(l));
      }
      const a = await this._generateSharedSecret(o, s);
      return {
        enc: r,
        sharedSecret: a
      };
    } catch (o) {
      throw new Nm(o);
    }
  }
  async decap(t) {
    const n = de(t.enc), r = await this._prim.deserializePublicKey(n), i = pi(t.recipientKey) ? t.recipientKey.privateKey : t.recipientKey, o = pi(t.recipientKey) ? t.recipientKey.publicKey : await this._prim.derivePublicKey(t.recipientKey), s = await this._prim.serializePublicKey(o);
    try {
      let a;
      if (t.senderPublicKey === void 0)
        a = new Uint8Array(await this._prim.dh(i, r));
      else {
        const l = new Uint8Array(await this._prim.dh(i, r)), d = new Uint8Array(await this._prim.dh(i, t.senderPublicKey));
        a = so(l, d);
      }
      let c;
      if (t.senderPublicKey === void 0)
        c = so(new Uint8Array(n), new Uint8Array(s));
      else {
        const l = await this._prim.serializePublicKey(t.senderPublicKey);
        c = new Uint8Array(n.byteLength + s.byteLength + l.byteLength), c.set(new Uint8Array(n), 0), c.set(new Uint8Array(s), n.byteLength), c.set(new Uint8Array(l), n.byteLength + s.byteLength);
      }
      return await this._generateSharedSecret(a, c);
    } catch (a) {
      throw new Um(a);
    }
  }
  async _generateSharedSecret(t, n) {
    const r = this._kdf.buildLabeledIkm(Jm, t), i = this._kdf.buildLabeledInfo(Qm, n, this.secretSize);
    return await this._kdf.extractAndExpand(Fe, r, i, this.secretSize);
  }
}
const ao = ["deriveBits"], nb = /* @__PURE__ */ new Uint8Array([
  100,
  107,
  112,
  95,
  112,
  114,
  107
]), rb = /* @__PURE__ */ new Uint8Array([115, 107]);
class Sr {
  constructor(t, n, r, i = []) {
    Object.defineProperty(this, "key", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "type", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "extractable", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: !0
    }), Object.defineProperty(this, "algorithm", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "usages", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.key = n, this.type = r, this.algorithm = { name: t }, this.usages = i, r === "public" && (this.usages = []);
  }
}
class ib {
  constructor(t, n, r, i) {
    Object.defineProperty(this, "_algName", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_curve", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_hkdf", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_nPk", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_nSk", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this._algName = t, this._nPk = n, this._nSk = n, this._curve = r, this._hkdf = i;
  }
  serializePublicKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new Mo(n));
    }
  }
  async deserializePublicKey(t) {
    try {
      return await this._importRawKey(de(t), !0);
    } catch (n) {
      throw new io(n);
    }
  }
  serializePrivateKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new Mo(n));
    }
  }
  async deserializePrivateKey(t) {
    try {
      return await this._importRawKey(de(t), !1);
    } catch (n) {
      throw new io(n);
    }
  }
  async importKey(t, n, r) {
    try {
      if (t === "raw")
        return await this._importRawKey(n, r);
      if (n instanceof ArrayBuffer)
        throw new Error("Invalid jwk key format");
      return await this._importJWK(n, r);
    } catch (i) {
      throw new io(i);
    }
  }
  async generateKeyPair() {
    try {
      const t = await this._curve.utils.randomSecretKey(), n = new Sr(this._algName, t, "private", ao);
      return { publicKey: await this.derivePublicKey(n), privateKey: n };
    } catch (t) {
      throw new Jc(t);
    }
  }
  async deriveKeyPair(t) {
    try {
      const n = de(t), r = await this._hkdf.labeledExtract(Fe.buffer, nb, new Uint8Array(n)), i = await this._hkdf.labeledExpand(r, rb, Fe, this._nSk), o = new Sr(this._algName, new Uint8Array(i), "private", ao);
      return {
        privateKey: o,
        publicKey: await this.derivePublicKey(o)
      };
    } catch (n) {
      throw new Fm(n);
    }
  }
  derivePublicKey(t) {
    try {
      const n = this._curve.getPublicKey(t.key);
      return Promise.resolve(new Sr(this._algName, n, "public"));
    } catch (n) {
      return Promise.reject(new io(n));
    }
  }
  dh(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t.key, n.key).buffer);
    } catch (r) {
      return Promise.reject(new Mo(r));
    }
  }
  _importRawKey(t, n) {
    return new Promise((r, i) => {
      n && t.byteLength !== this._nPk && i(new Error("Invalid length of the key")), !n && t.byteLength !== this._nSk && i(new Error("Invalid length of the key")), r(new Sr(this._algName, new Uint8Array(t), n ? "public" : "private", n ? [] : ao));
    });
  }
  _importJWK(t, n) {
    return new Promise((r, i) => {
      t.kty !== "OKP" && i(new Error(`Invalid kty: ${t.kty}`)), t.crv !== this._algName && i(new Error(`Invalid crv: ${t.crv}`)), n ? (typeof t.d < "u" && i(new Error("Invalid key: `d` should not be set")), typeof t.x != "string" && i(new Error("Invalid key: `x` not found")), r(new Sr(this._algName, Dd(t.x), "public"))) : (typeof t.d != "string" && i(new Error("Invalid key: `d` not found")), r(new Sr(this._algName, Dd(t.d), "private", ao)));
    });
  }
}
const ob = ["encrypt", "decrypt"];
function sb(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Ri(e, t = "") {
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new Error(`${n}expected integer >0, got ${e}`);
  }
}
function on(e, t, n = "") {
  const r = sb(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function es(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function ab(e, t) {
  on(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new Error('"digestInto() output" expected to be of length >=' + n);
}
function cb(e) {
  if (typeof e == "bigint") {
    if (!Fo(e))
      throw new Error("positive bigint expected, got " + e);
  } else
    Ri(e);
  return e;
}
function ts(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
const lb = /* @__PURE__ */ new Uint32Array([287454020]), db = /* @__PURE__ */ new Uint8Array(lb.buffer);
db[0];
function Ta(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function _t(e, t) {
  return e << 32 - t | e >>> t;
}
const vh = /* @ts-ignore: to use toHex */ typeof Uint8Array.from([]).toHex == "function" && // @ts-ignore: to use fromHex
typeof Uint8Array.fromHex == "function", ub = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0")), fb = [
  0n,
  1n,
  2n,
  3n,
  4n,
  5n,
  6n,
  7n,
  8n,
  9n,
  10n,
  11n,
  12n,
  13n,
  14n,
  15n
];
function hb(e) {
  if (on(e), vh)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += ub[e[n]];
  return t;
}
const Vt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function pc(e) {
  if (e >= Vt._0 && e <= Vt._9)
    return e - Vt._0;
  if (e >= Vt.A && e <= Vt.F)
    return e - (Vt.A - 10);
  if (e >= Vt.a && e <= Vt.f)
    return e - (Vt.a - 10);
}
function pb(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (vh)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = pc(e.charCodeAt(o)), a = pc(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function yb(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  let t = Ot;
  for (let n = 0; n < e.length; n++) {
    const r = pc(e.charCodeAt(n));
    if (r === void 0)
      throw new Error('hex string expected, got non-hex character "' + e[n] + '" at index ' + n);
    t = t << 4n | fb[r];
  }
  return t;
}
function wb(e) {
  Ri(e, "numberToBigint");
  let t = e, n = Ot, r = 1n;
  for (; t > 0; )
    t % 2 === 1 && (n += r), t = Math.floor(t / 2), r <<= 1n;
  return n;
}
function zd(e) {
  return yb(hb(yc(on(e)).reverse()));
}
function gb(e, t) {
  Ri(t), e = cb(e);
  const n = pb(e.toString(16).padStart(t * 2, "0"));
  if (n.length !== t)
    throw new Error("number too large");
  return n;
}
function mb(e, t) {
  return gb(e, t).reverse();
}
function yc(e) {
  return Uint8Array.from(e);
}
function Fo(e) {
  return typeof e == "bigint" && Ot <= e;
}
function bb(e, t, n) {
  return Fo(e) && Fo(t) && Fo(n) && t <= e && e < n;
}
function Md(e, t, n, r) {
  if (!bb(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function vb(e, t = {}, n = {}) {
  if (!e || typeof e != "object")
    throw new Error("expected valid options object");
  function r(o, s, a) {
    const c = e[o];
    if (a && c === void 0)
      return;
    const l = typeof c;
    if (l !== s || c === null)
      throw new Error(`param "${o}" is invalid: expected ${s}, got ${l}`);
  }
  const i = (o, s) => Object.entries(o).forEach(([a, c]) => r(a, c, s));
  i(t, !1), i(n, !0);
}
async function xb(e = 32) {
  const t = await Ym(), n = new Uint8Array(e);
  return t.getRandomValues(n), n;
}
function kb(e) {
  return {
    oid: Uint8Array.from([
      6,
      9,
      96,
      134,
      72,
      1,
      101,
      3,
      4,
      2,
      e
    ])
  };
}
function Eb(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash must wrapped by utils.createHasher");
  Ri(e.outputLen), Ri(e.blockLen);
}
function Sb(e, t = {}) {
  const n = (o, s) => e(s).update(o).digest(), r = e(void 0), i = Object.assign(n, {
    outputLen: r.outputLen,
    blockLen: r.blockLen,
    create: (o) => e(o),
    ...t
  });
  return Object.freeze(i);
}
let xh = class {
  constructor(t, n) {
    if (Object.defineProperty(this, "oHash", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "iHash", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "blockLen", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "outputLen", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "finished", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: !1
    }), Object.defineProperty(this, "destroyed", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: !1
    }), Eb(t), on(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), ts(i);
  }
  update(t) {
    return es(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    es(this), on(t, this.outputLen, "output"), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.oHash.outputLen);
    return this.digestInto(t), t;
  }
  _cloneInto(t) {
    t ||= Object.create(Object.getPrototypeOf(this), {});
    const { oHash: n, iHash: r, finished: i, destroyed: o, blockLen: s, outputLen: a } = this;
    return t = t, t.finished = i, t.destroyed = o, t.blockLen = s, t.outputLen = a, t.oHash = n._cloneInto(t.oHash), t.iHash = r._cloneInto(t.iHash), t;
  }
  clone() {
    return this._cloneInto();
  }
  destroy() {
    this.destroyed = !0, this.oHash.destroy(), this.iHash.destroy();
  }
};
const kh = (e, t, n) => new xh(e, t).update(n).digest();
kh.create = (e, t) => new xh(e, t);
function Ab(e, t, n) {
  return e & t ^ ~e & n;
}
function _b(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Ib = class {
  constructor(t, n, r, i) {
    Object.defineProperty(this, "blockLen", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "outputLen", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "padOffset", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "isLE", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "buffer", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "view", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "finished", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: !1
    }), Object.defineProperty(this, "length", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "pos", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "destroyed", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: !1
    }), this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Ta(this.buffer);
  }
  update(t) {
    es(this), on(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Ta(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    es(this), ab(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, ts(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let u = s; u < i; u++)
      n[u] = 0;
    r.setBigUint64(i - 8, wb(this.length * 8), o), this.process(r, 0);
    const a = Ta(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen must be aligned to 32bit");
    const l = c / 4, d = this.get();
    if (l > d.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let u = 0; u < l; u++)
      a.setUint32(4 * u, d[u], o);
  }
  digest() {
    const { buffer: t, outputLen: n } = this;
    this.digestInto(t);
    const r = t.slice(0, n);
    return this.destroy(), r;
  }
  _cloneInto(t) {
    t ||= new this.constructor(), t.set(...this.get());
    const { blockLen: n, buffer: r, length: i, finished: o, destroyed: s, pos: a } = this;
    return t.destroyed = s, t.finished = o, t.length = i, t.pos = a, i % n && t.buffer.set(r), t;
  }
  clone() {
    return this._cloneInto();
  }
};
const yn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Bb = /* @__PURE__ */ Uint32Array.from([
  1116352408,
  1899447441,
  3049323471,
  3921009573,
  961987163,
  1508970993,
  2453635748,
  2870763221,
  3624381080,
  310598401,
  607225278,
  1426881987,
  1925078388,
  2162078206,
  2614888103,
  3248222580,
  3835390401,
  4022224774,
  264347078,
  604807628,
  770255983,
  1249150122,
  1555081692,
  1996064986,
  2554220882,
  2821834349,
  2952996808,
  3210313671,
  3336571891,
  3584528711,
  113926993,
  338241895,
  666307205,
  773529912,
  1294757372,
  1396182291,
  1695183700,
  1986661051,
  2177026350,
  2456956037,
  2730485921,
  2820302411,
  3259730800,
  3345764771,
  3516065817,
  3600352804,
  4094571909,
  275423344,
  430227734,
  506948616,
  659060556,
  883997877,
  958139571,
  1322822218,
  1537002063,
  1747873779,
  1955562222,
  2024104815,
  2227730452,
  2361852424,
  2428436474,
  2756734187,
  3204031479,
  3329325298
]), wn = /* @__PURE__ */ new Uint32Array(64);
let Tb = class extends Ib {
  constructor(t) {
    super(64, t, 8, !1);
  }
  get() {
    const { A: t, B: n, C: r, D: i, E: o, F: s, G: a, H: c } = this;
    return [t, n, r, i, o, s, a, c];
  }
  set(t, n, r, i, o, s, a, c) {
    this.A = t | 0, this.B = n | 0, this.C = r | 0, this.D = i | 0, this.E = o | 0, this.F = s | 0, this.G = a | 0, this.H = c | 0;
  }
  process(t, n) {
    for (let u = 0; u < 16; u++, n += 4)
      wn[u] = t.getUint32(n, !1);
    for (let u = 16; u < 64; u++) {
      const f = wn[u - 15], h = wn[u - 2], y = _t(f, 7) ^ _t(f, 18) ^ f >>> 3, w = _t(h, 17) ^ _t(h, 19) ^ h >>> 10;
      wn[u] = w + wn[u - 7] + y + wn[u - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: d } = this;
    for (let u = 0; u < 64; u++) {
      const f = _t(a, 6) ^ _t(a, 11) ^ _t(a, 25), h = d + f + Ab(a, c, l) + Bb[u] + wn[u] | 0, w = (_t(r, 2) ^ _t(r, 13) ^ _t(r, 22)) + _b(r, i, o) | 0;
      d = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, d = d + this.H | 0, this.set(r, i, o, s, a, c, l, d);
  }
  roundClean() {
    ts(wn);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), ts(this.buffer);
  }
}, Pb = class extends Tb {
  constructor() {
    super(32), Object.defineProperty(this, "A", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[0] | 0
    }), Object.defineProperty(this, "B", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[1] | 0
    }), Object.defineProperty(this, "C", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[2] | 0
    }), Object.defineProperty(this, "D", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[3] | 0
    }), Object.defineProperty(this, "E", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[4] | 0
    }), Object.defineProperty(this, "F", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[5] | 0
    }), Object.defineProperty(this, "G", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[6] | 0
    }), Object.defineProperty(this, "H", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yn[7] | 0
    });
  }
};
const $b = /* @__PURE__ */ Sb(
  () => new Pb(),
  /* @__PURE__ */ kb(1)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Eh(e, t) {
  const n = e % t;
  return n >= Ot ? n : t + n;
}
function pt(e, t, n) {
  let r = e;
  for (; t-- > Ot; )
    r *= r, r %= n;
  return r;
}
function Ob(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Cb(e) {
  return vb(e, {
    adjustScalarBytes: "function",
    powPminus2: "function"
  }), Object.freeze({ ...e });
}
function Rb(e) {
  const t = Cb(e), { P: n, type: r, adjustScalarBytes: i, powPminus2: o, randomBytes: s } = t, a = r === "x25519";
  if (!a && r !== "x448")
    throw new Error("invalid type");
  const c = s || xb, l = a ? 255n : 448n, d = a ? 32 : 56, u = a ? 9n : 5n, f = a ? 121665n : 39081n, h = a ? oo ** 254n : oo ** 447n, y = a ? 8n * oo ** 251n - Er : 4n * oo ** 445n - Er, w = h + y + Er, p = (v) => Eh(v, n), g = k(u);
  function k(v) {
    return mb(p(v), d);
  }
  function b(v) {
    const m = yc(on(v, d, "uCoordinate"));
    return a && (m[31] &= 127), p(zd(m));
  }
  function O(v) {
    return zd(i(yc(on(v, d, "scalar"))));
  }
  function B(v, m) {
    const A = D(b(m), O(v));
    if (A === Ot)
      throw new Error("invalid private or public key received");
    return k(A);
  }
  function R(v) {
    return B(v, g);
  }
  const H = R, L = B;
  function T(v, m, A) {
    const $ = p(v * (m - A));
    return m = p(m - $), A = p(A + $), { x_2: m, x_3: A };
  }
  function D(v, m) {
    Md("u", v, Ot, n), Md("scalar", m, h, w);
    const A = m, $ = v;
    let S = Er, I = Ot, P = v, C = Er, N = Ot;
    for (let U = l - 1n; U >= Ot; U--) {
      const M = A >> U & Er;
      N ^= M, { x_2: S, x_3: P } = T(N, S, P), { x_2: I, x_3: C } = T(N, I, C), N = M;
      const F = S + I, V = p(F * F), q = S - I, W = p(q * q), Y = V - W, pe = P + C, se = P - C, be = p(se * F), Ge = p(pe * q), un = be + Ge, kr = be - Ge;
      P = p(un * un), C = p($ * p(kr * kr)), S = p(V * W), I = p(Y * (V + p(f * Y)));
    }
    ({ x_2: S, x_3: P } = T(N, S, P)), { x_2: I, x_3: C } = T(N, I, C);
    const K = o(I);
    return p(S * K);
  }
  const z = {
    secretKey: d,
    publicKey: d,
    seed: d
  }, E = async (v) => (v === void 0 && (v = await c(d)), on(v, z.seed, "seed"), v), x = { randomSecretKey: E };
  return Object.freeze({
    keygen: Ob(E, H),
    getSharedSecret: L,
    getPublicKey: H,
    scalarMult: B,
    scalarMultBase: R,
    utils: x,
    GuBytes: g.slice(),
    lengths: z
  });
}
class Lb extends Qc {
  constructor(t) {
    super(), Object.defineProperty(this, "_rawKey", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_key", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this._rawKey = de(t);
  }
  async seal(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: de(t),
      additionalData: de(r)
    };
    return await this._api.encrypt(i, this._key, de(n));
  }
  async open(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: de(t),
      additionalData: de(r)
    };
    return await this._api.decrypt(i, this._key, de(n));
  }
  async _setupKey() {
    if (this._key !== void 0)
      return;
    await this._setup();
    const t = await this._importKey(this._rawKey);
    new Uint8Array(this._rawKey).fill(0), this._key = t;
  }
  async _importKey(t) {
    return await this._api.importKey("raw", t, { name: "AES-GCM" }, !0, ob);
  }
}
class Hb {
  constructor() {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: el.Aes128Gcm
    }), Object.defineProperty(this, "keySize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 16
    }), Object.defineProperty(this, "nonceSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 12
    }), Object.defineProperty(this, "tagSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 16
    });
  }
  createEncryptionContext(t) {
    return new Lb(t);
  }
}
class Nb extends Hb {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: el.Aes256Gcm
    }), Object.defineProperty(this, "keySize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 32
    }), Object.defineProperty(this, "nonceSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 12
    }), Object.defineProperty(this, "tagSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 16
    });
  }
}
function Kd() {
  return new Promise((e, t) => {
    t(new Jc("Not supported"));
  });
}
const Ub = new Uint8Array([115, 101, 99]);
class tl {
  constructor(t, n, r) {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "exporterSecret", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_kdf", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this._api = t, this._kdf = n, this.exporterSecret = r;
  }
  async seal(t, n) {
    return await Kd();
  }
  async open(t, n) {
    return await Kd();
  }
  async export(t, n) {
    const r = de(t);
    if (r.byteLength > Qo)
      throw new $t("Too long exporter context");
    try {
      return await this._kdf.labeledExpand(this.exporterSecret, Ub, new Uint8Array(r), n);
    } catch (i) {
      throw new Dm(i);
    }
  }
}
class Db extends tl {
}
class zb extends tl {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.enc = i;
  }
}
class Sh extends tl {
  constructor(t, n, r) {
    if (super(t, n, r.exporterSecret), Object.defineProperty(this, "_aead", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_nK", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_nN", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_nT", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_ctx", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), r.key === void 0 || r.baseNonce === void 0 || r.seq === void 0)
      throw new Error("Required parameters are missing");
    this._aead = r.aead, this._nK = this._aead.keySize, this._nN = this._aead.nonceSize, this._nT = this._aead.tagSize;
    const i = this._aead.createEncryptionContext(r.key);
    this._ctx = {
      key: i,
      baseNonce: r.baseNonce,
      seq: r.seq
    };
  }
  computeNonce(t) {
    const n = Ti(t.seq, t.baseNonce.byteLength);
    return Xm(t.baseNonce, n).buffer;
  }
  incrementSeq(t) {
    if (t.seq > Number.MAX_SAFE_INTEGER)
      throw new Km("Message limit reached");
    t.seq += 1;
  }
}
var Mb = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Kb = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, jo;
class Ah {
  constructor() {
    jo.set(this, Promise.resolve());
  }
  async lock() {
    let t;
    const n = new Promise((i) => {
      t = i;
    }), r = Mb(this, jo, "f");
    return Kb(this, jo, n, "f"), await r, t;
  }
}
jo = /* @__PURE__ */ new WeakMap();
var Fd = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Fb = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Ei;
class jb extends Sh {
  constructor() {
    super(...arguments), Ei.set(this, void 0);
  }
  async open(t, n = Fe.buffer) {
    Fb(this, Ei, Fd(this, Ei, "f") ?? new Ah(), "f");
    const r = await Fd(this, Ei, "f").lock();
    let i;
    try {
      i = await this._ctx.key.open(this.computeNonce(this._ctx), de(t), de(n));
    } catch (o) {
      throw new Mm(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Ei = /* @__PURE__ */ new WeakMap();
var jd = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Vb = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Si;
class qb extends Sh {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Si.set(this, void 0), this.enc = i;
  }
  async seal(t, n = Fe.buffer) {
    Vb(this, Si, jd(this, Si, "f") ?? new Ah(), "f");
    const r = await jd(this, Si, "f").lock();
    let i;
    try {
      i = await this._ctx.key.seal(this.computeNonce(this._ctx), de(t), de(n));
    } catch (o) {
      throw new zm(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Si = /* @__PURE__ */ new WeakMap();
const Wb = new Uint8Array([
  98,
  97,
  115,
  101,
  95,
  110,
  111,
  110,
  99,
  101
]), Gb = new Uint8Array([101, 120, 112]), Zb = new Uint8Array([
  105,
  110,
  102,
  111,
  95,
  104,
  97,
  115,
  104
]), Yb = new Uint8Array([107, 101, 121]), Xb = new Uint8Array([
  112,
  115,
  107,
  95,
  105,
  100,
  95,
  104,
  97,
  115,
  104
]), Jb = new Uint8Array([115, 101, 99, 114, 101, 116]), Qb = new Uint8Array([
  72,
  80,
  75,
  69,
  0,
  0,
  0,
  0,
  0,
  0
]);
class e0 extends Qc {
  /**
   * @param params A set of parameters for building a cipher suite.
   *
   * If the error occurred, throws {@link InvalidParamError}.
   *
   * @throws {@link InvalidParamError}
   */
  constructor(t) {
    if (super(), Object.defineProperty(this, "_kem", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_kdf", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_aead", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), typeof t.kem == "number")
      throw new $t("KemId cannot be used");
    if (this._kem = t.kem, typeof t.kdf == "number")
      throw new $t("KdfId cannot be used");
    if (this._kdf = t.kdf, typeof t.aead == "number")
      throw new $t("AeadId cannot be used");
    this._aead = t.aead, this._suiteId = new Uint8Array(Qb), this._suiteId.set(Ti(this._kem.id, 2), 4), this._suiteId.set(Ti(this._kdf.id, 2), 6), this._suiteId.set(Ti(this._aead.id, 2), 8), this._kdf.init(this._suiteId);
  }
  /**
   * Gets the KEM context of the ciphersuite.
   */
  get kem() {
    return this._kem;
  }
  /**
   * Gets the KDF context of the ciphersuite.
   */
  get kdf() {
    return this._kdf;
  }
  /**
   * Gets the AEAD context of the ciphersuite.
   */
  get aead() {
    return this._aead;
  }
  /**
   * Creates an encryption context for a sender.
   *
   * If the error occurred, throws {@link DecapError} | {@link ValidationError}.
   *
   * @param params A set of parameters for the sender encryption context.
   * @returns A sender encryption context.
   * @throws {@link EncapError}, {@link ValidationError}
   */
  async createSenderContext(t) {
    this._validateInputLength(t), await this._setup();
    const n = await this._kem.encap(t);
    let r;
    return t.psk !== void 0 ? r = t.senderKey !== void 0 ? pn.AuthPsk : pn.Psk : r = t.senderKey !== void 0 ? pn.Auth : pn.Base, await this._keyScheduleS(r, n.sharedSecret, n.enc, t);
  }
  /**
   * Creates an encryption context for a recipient.
   *
   * If the error occurred, throws {@link DecapError}
   * | {@link DeserializeError} | {@link ValidationError}.
   *
   * @param params A set of parameters for the recipient encryption context.
   * @returns A recipient encryption context.
   * @throws {@link DecapError}, {@link DeserializeError}, {@link ValidationError}
   */
  async createRecipientContext(t) {
    this._validateInputLength(t), await this._setup();
    const n = await this._kem.decap(t);
    let r;
    return t.psk !== void 0 ? r = t.senderPublicKey !== void 0 ? pn.AuthPsk : pn.Psk : r = t.senderPublicKey !== void 0 ? pn.Auth : pn.Base, await this._keyScheduleR(r, n, t);
  }
  /**
   * Encrypts a message to a recipient.
   *
   * If the error occurred, throws `EncapError` | `MessageLimitReachedError` | `SealError` | `ValidationError`.
   *
   * @param params A set of parameters for building a sender encryption context.
   * @param pt A plain text as bytes to be encrypted.
   * @param aad Additional authenticated data as bytes fed by an application.
   * @returns A cipher text and an encapsulated key as bytes.
   * @throws {@link EncapError}, {@link MessageLimitReachedError}, {@link SealError}, {@link ValidationError}
   */
  async seal(t, n, r = Fe.buffer) {
    const i = await this.createSenderContext(t);
    return {
      ct: await i.seal(n, r),
      enc: i.enc
    };
  }
  /**
   * Decrypts a message from a sender.
   *
   * If the error occurred, throws `DecapError` | `DeserializeError` | `OpenError` | `ValidationError`.
   *
   * @param params A set of parameters for building a recipient encryption context.
   * @param ct An encrypted text as bytes to be decrypted.
   * @param aad Additional authenticated data as bytes fed by an application.
   * @returns A decrypted plain text as bytes.
   * @throws {@link DecapError}, {@link DeserializeError}, {@link OpenError}, {@link ValidationError}
   */
  async open(t, n, r = Fe.buffer) {
    return await (await this.createRecipientContext(t)).open(n, r);
  }
  // private verifyPskInputs(mode: Mode, params: KeyScheduleParams) {
  //   const gotPsk = (params.psk !== undefined);
  //   const gotPskId = (params.psk !== undefined && params.psk.id.byteLength > 0);
  //   if (gotPsk !== gotPskId) {
  //     throw new Error('Inconsistent PSK inputs');
  //   }
  //   if (gotPsk && (mode === Mode.Base || mode === Mode.Auth)) {
  //     throw new Error('PSK input provided when not needed');
  //   }
  //   if (!gotPsk && (mode === Mode.Psk || mode === Mode.AuthPsk)) {
  //     throw new Error('Missing required PSK input');
  //   }
  //   return;
  // }
  async _keySchedule(t, n, r) {
    const i = r.psk === void 0 ? Fe : Ko(r.psk.id), o = await this._kdf.labeledExtract(Fe, Xb, i), s = r.info === void 0 ? Fe : Ko(r.info), a = await this._kdf.labeledExtract(Fe, Zb, s), c = new Uint8Array(1 + o.byteLength + a.byteLength);
    c.set(new Uint8Array([t]), 0), c.set(new Uint8Array(o), 1), c.set(new Uint8Array(a), 1 + o.byteLength);
    const l = r.psk === void 0 ? Fe : Ko(r.psk.key), d = this._kdf.buildLabeledIkm(Jb, l), u = this._kdf.buildLabeledInfo(Gb, c, this._kdf.hashSize), f = await this._kdf.extractAndExpand(n, d, u, this._kdf.hashSize);
    if (this._aead.id === el.ExportOnly)
      return { aead: this._aead, exporterSecret: f };
    const h = this._kdf.buildLabeledInfo(Yb, c, this._aead.keySize), y = await this._kdf.extractAndExpand(n, d, h, this._aead.keySize), w = this._kdf.buildLabeledInfo(Wb, c, this._aead.nonceSize), p = await this._kdf.extractAndExpand(n, d, w, this._aead.nonceSize);
    return {
      aead: this._aead,
      exporterSecret: f,
      key: y,
      baseNonce: new Uint8Array(p),
      seq: 0
    };
  }
  async _keyScheduleS(t, n, r, i) {
    const o = await this._keySchedule(t, n, i);
    return o.key === void 0 ? new zb(this._api, this._kdf, o.exporterSecret, r) : new qb(this._api, this._kdf, o, r);
  }
  async _keyScheduleR(t, n, r) {
    const i = await this._keySchedule(t, n, r);
    return i.key === void 0 ? new Db(this._api, this._kdf, i.exporterSecret) : new jb(this._api, this._kdf, i);
  }
  _validateInputLength(t) {
    if (t.info !== void 0 && t.info.byteLength > Wm)
      throw new $t("Too long info");
    if (t.psk !== void 0) {
      if (t.psk.key.byteLength < Nd)
        throw new $t(`PSK must have at least ${Nd} bytes`);
      if (t.psk.key.byteLength > Qo)
        throw new $t("Too long psk.key");
      if (t.psk.id.byteLength > Qo)
        throw new $t("Too long psk.id");
    }
  }
}
class t0 extends e0 {
}
let n0 = class extends bh {
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const r0 = 1n, Vd = 2n, i0 = 3n, o0 = 5n, _h = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn;
function s0(e) {
  const t = 10n, n = 20n, r = 40n, i = 80n, o = _h, a = e * e % o * e % o, c = pt(a, Vd, o) * a % o, l = pt(c, r0, o) * e % o, d = pt(l, o0, o) * l % o, u = pt(d, t, o) * d % o, f = pt(u, n, o) * u % o, h = pt(f, r, o) * f % o, y = pt(h, i, o) * h % o, w = pt(y, i, o) * h % o, p = pt(w, t, o) * d % o;
  return { pow_p_5_8: pt(p, Vd, o) * e % o, b2: a };
}
function a0(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const c0 = /* @__PURE__ */ (() => {
  const e = _h;
  return Rb({
    P: e,
    type: "x25519",
    powPminus2: (t) => {
      const { pow_p_5_8: n, b2: r } = s0(t);
      return Eh(pt(n, i0, e) * r, e);
    },
    adjustScalarBytes: a0
  });
})();
class l0 extends bh {
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : de(t), i = de(n);
    if (r.byteLength !== this.hashSize)
      return kh($b, new Uint8Array(r), new Uint8Array(i)).buffer;
    const o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
}
class d0 extends ib {
  constructor(t) {
    super("X25519", 32, c0, t);
  }
  derive(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t, n));
    } catch (r) {
      return Promise.reject(new Mo(r));
    }
  }
}
class u0 extends tb {
  constructor() {
    const t = new l0();
    super(Hd.DhkemX25519HkdfSha256, new d0(t), t), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Hd.DhkemX25519HkdfSha256
    }), Object.defineProperty(this, "secretSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 32
    }), Object.defineProperty(this, "encSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 32
    }), Object.defineProperty(this, "publicKeySize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 32
    }), Object.defineProperty(this, "privateKeySize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 32
    });
  }
}
function Ih(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array" && "BYTES_PER_ELEMENT" in e && e.BYTES_PER_ELEMENT === 1;
}
function Dt(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(`${n}expected number, got ${typeof e}`);
  }
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new RangeError(`${n}expected integer >= 0, got ${e}`);
  }
}
function zt(e, t, n = "") {
  const r = Ih(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`, l = s + "expected Uint8Array" + a + ", got " + c;
    throw r ? new RangeError(l) : new TypeError(l);
  }
  return e;
}
function Bh(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new TypeError("Hash must wrapped by utils.createHasher");
  if (Dt(e.outputLen), Dt(e.blockLen), e.outputLen < 1)
    throw new Error('"outputLen" must be >= 1');
  if (e.blockLen < 1)
    throw new Error('"blockLen" must be >= 1');
}
function Wr(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function nl(e, t) {
  zt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new RangeError('"digestInto() output" expected to be of length >=' + n);
}
function f0(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function Mt(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Pa(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function It(e, t) {
  return e << 32 - t | e >>> t;
}
function co(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const h0 = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function p0(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function y0(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = p0(e[t]);
  return e;
}
const qd = h0 ? (e) => e : y0, Th = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", w0 = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function rl(e) {
  if (zt(e), Th)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += w0[e[n]];
  return t;
}
const qt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function Wd(e) {
  if (e >= qt._0 && e <= qt._9)
    return e - qt._0;
  if (e >= qt.A && e <= qt.F)
    return e - (qt.A - 10);
  if (e >= qt.a && e <= qt.f)
    return e - (qt.a - 10);
}
function il(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  if (Th)
    try {
      return Uint8Array.fromHex(e);
    } catch (i) {
      throw i instanceof SyntaxError ? new RangeError(i.message) : i;
    }
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new RangeError("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = Wd(e.charCodeAt(o)), a = Wd(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new RangeError('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function g0(...e) {
  let t = 0;
  for (let r = 0; r < e.length; r++) {
    const i = e[r];
    zt(i), t += i.length;
  }
  const n = new Uint8Array(t);
  for (let r = 0, i = 0; r < e.length; r++) {
    const o = e[r];
    n.set(o, i), i += o.length;
  }
  return n;
}
function Zs(e, t = {}) {
  const n = (i, o) => e(o).update(i).digest(), r = e(void 0);
  return n.outputLen = r.outputLen, n.blockLen = r.blockLen, n.canXOF = r.canXOF, n.create = (i) => e(i), Object.assign(n, t), Object.freeze(n);
}
function ol(e = 32) {
  Dt(e, "bytesLength");
  const t = typeof globalThis == "object" ? globalThis.crypto : null;
  if (typeof t?.getRandomValues != "function")
    throw new Error("crypto.getRandomValues must be defined");
  if (e > 65536)
    throw new RangeError(`"bytesLength" expected <= 65536, got ${e}`);
  return t.getRandomValues(new Uint8Array(e));
}
const Ph = (e) => ({
  // Current NIST hashAlgs suffixes used here fit in one DER subidentifier octet.
  // Larger suffix values would need base-128 OID encoding and a different length byte.
  oid: Uint8Array.from([6, 9, 96, 134, 72, 1, 101, 3, 4, 2, e])
});
function m0(e, t, n) {
  return e & t ^ ~e & n;
}
function b0(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let sl = class {
  blockLen;
  outputLen;
  canXOF = !1;
  padOffset;
  isLE;
  // For partial updates less than block size
  buffer;
  view;
  finished = !1;
  length = 0;
  pos = 0;
  destroyed = !1;
  constructor(t, n, r, i) {
    this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Pa(this.buffer);
  }
  update(t) {
    Wr(this), zt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Pa(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Wr(this), nl(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Mt(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let u = s; u < i; u++)
      n[u] = 0;
    r.setBigUint64(i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Pa(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen must be aligned to 32bit");
    const l = c / 4, d = this.get();
    if (l > d.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let u = 0; u < l; u++)
      a.setUint32(4 * u, d[u], o);
  }
  digest() {
    const { buffer: t, outputLen: n } = this;
    this.digestInto(t);
    const r = t.slice(0, n);
    return this.destroy(), r;
  }
  _cloneInto(t) {
    t ||= new this.constructor(), t.set(...this.get());
    const { blockLen: n, buffer: r, length: i, finished: o, destroyed: s, pos: a } = this;
    return t.destroyed = s, t.finished = o, t.length = i, t.pos = a, i % n && t.buffer.set(r), t;
  }
  clone() {
    return this._cloneInto();
  }
};
const gn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Re = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  4089235720,
  3144134277,
  2227873595,
  1013904242,
  4271175723,
  2773480762,
  1595750129,
  1359893119,
  2917565137,
  2600822924,
  725511199,
  528734635,
  4215389547,
  1541459225,
  327033209
]), lo = /* @__PURE__ */ BigInt(2 ** 32 - 1), Gd = /* @__PURE__ */ BigInt(32);
function v0(e, t = !1) {
  return t ? { h: Number(e & lo), l: Number(e >> Gd & lo) } : { h: Number(e >> Gd & lo) | 0, l: Number(e & lo) | 0 };
}
function $h(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = v0(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const Zd = (e, t, n) => e >>> n, Yd = (e, t, n) => e << 32 - n | t >>> n, Ar = (e, t, n) => e >>> n | t << 32 - n, _r = (e, t, n) => e << 32 - n | t >>> n, uo = (e, t, n) => e << 64 - n | t >>> n - 32, fo = (e, t, n) => e >>> n - 32 | t << 64 - n, x0 = (e, t, n) => e << n | t >>> 32 - n, k0 = (e, t, n) => t << n | e >>> 32 - n, E0 = (e, t, n) => t << n - 32 | e >>> 64 - n, S0 = (e, t, n) => e << n - 32 | t >>> 64 - n;
function Wt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const A0 = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), _0 = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, I0 = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), B0 = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, T0 = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), P0 = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, $0 = /* @__PURE__ */ Uint32Array.from([
  1116352408,
  1899447441,
  3049323471,
  3921009573,
  961987163,
  1508970993,
  2453635748,
  2870763221,
  3624381080,
  310598401,
  607225278,
  1426881987,
  1925078388,
  2162078206,
  2614888103,
  3248222580,
  3835390401,
  4022224774,
  264347078,
  604807628,
  770255983,
  1249150122,
  1555081692,
  1996064986,
  2554220882,
  2821834349,
  2952996808,
  3210313671,
  3336571891,
  3584528711,
  113926993,
  338241895,
  666307205,
  773529912,
  1294757372,
  1396182291,
  1695183700,
  1986661051,
  2177026350,
  2456956037,
  2730485921,
  2820302411,
  3259730800,
  3345764771,
  3516065817,
  3600352804,
  4094571909,
  275423344,
  430227734,
  506948616,
  659060556,
  883997877,
  958139571,
  1322822218,
  1537002063,
  1747873779,
  1955562222,
  2024104815,
  2227730452,
  2361852424,
  2428436474,
  2756734187,
  3204031479,
  3329325298
]), mn = /* @__PURE__ */ new Uint32Array(64);
class O0 extends sl {
  constructor(t) {
    super(64, t, 8, !1);
  }
  get() {
    const { A: t, B: n, C: r, D: i, E: o, F: s, G: a, H: c } = this;
    return [t, n, r, i, o, s, a, c];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c) {
    this.A = t | 0, this.B = n | 0, this.C = r | 0, this.D = i | 0, this.E = o | 0, this.F = s | 0, this.G = a | 0, this.H = c | 0;
  }
  process(t, n) {
    for (let u = 0; u < 16; u++, n += 4)
      mn[u] = t.getUint32(n, !1);
    for (let u = 16; u < 64; u++) {
      const f = mn[u - 15], h = mn[u - 2], y = It(f, 7) ^ It(f, 18) ^ f >>> 3, w = It(h, 17) ^ It(h, 19) ^ h >>> 10;
      mn[u] = w + mn[u - 7] + y + mn[u - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: d } = this;
    for (let u = 0; u < 64; u++) {
      const f = It(a, 6) ^ It(a, 11) ^ It(a, 25), h = d + f + m0(a, c, l) + $0[u] + mn[u] | 0, w = (It(r, 2) ^ It(r, 13) ^ It(r, 22)) + b0(r, i, o) | 0;
      d = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, d = d + this.H | 0, this.set(r, i, o, s, a, c, l, d);
  }
  roundClean() {
    Mt(mn);
  }
  destroy() {
    this.destroyed = !0, this.set(0, 0, 0, 0, 0, 0, 0, 0), Mt(this.buffer);
  }
}
class C0 extends O0 {
  // We cannot use array here since array allows indexing by variable
  // which means optimizer/compiler cannot use registers.
  A = gn[0] | 0;
  B = gn[1] | 0;
  C = gn[2] | 0;
  D = gn[3] | 0;
  E = gn[4] | 0;
  F = gn[5] | 0;
  G = gn[6] | 0;
  H = gn[7] | 0;
  constructor() {
    super(32);
  }
}
const Oh = $h([
  "0x428a2f98d728ae22",
  "0x7137449123ef65cd",
  "0xb5c0fbcfec4d3b2f",
  "0xe9b5dba58189dbbc",
  "0x3956c25bf348b538",
  "0x59f111f1b605d019",
  "0x923f82a4af194f9b",
  "0xab1c5ed5da6d8118",
  "0xd807aa98a3030242",
  "0x12835b0145706fbe",
  "0x243185be4ee4b28c",
  "0x550c7dc3d5ffb4e2",
  "0x72be5d74f27b896f",
  "0x80deb1fe3b1696b1",
  "0x9bdc06a725c71235",
  "0xc19bf174cf692694",
  "0xe49b69c19ef14ad2",
  "0xefbe4786384f25e3",
  "0x0fc19dc68b8cd5b5",
  "0x240ca1cc77ac9c65",
  "0x2de92c6f592b0275",
  "0x4a7484aa6ea6e483",
  "0x5cb0a9dcbd41fbd4",
  "0x76f988da831153b5",
  "0x983e5152ee66dfab",
  "0xa831c66d2db43210",
  "0xb00327c898fb213f",
  "0xbf597fc7beef0ee4",
  "0xc6e00bf33da88fc2",
  "0xd5a79147930aa725",
  "0x06ca6351e003826f",
  "0x142929670a0e6e70",
  "0x27b70a8546d22ffc",
  "0x2e1b21385c26c926",
  "0x4d2c6dfc5ac42aed",
  "0x53380d139d95b3df",
  "0x650a73548baf63de",
  "0x766a0abb3c77b2a8",
  "0x81c2c92e47edaee6",
  "0x92722c851482353b",
  "0xa2bfe8a14cf10364",
  "0xa81a664bbc423001",
  "0xc24b8b70d0f89791",
  "0xc76c51a30654be30",
  "0xd192e819d6ef5218",
  "0xd69906245565a910",
  "0xf40e35855771202a",
  "0x106aa07032bbd1b8",
  "0x19a4c116b8d2d0c8",
  "0x1e376c085141ab53",
  "0x2748774cdf8eeb99",
  "0x34b0bcb5e19b48a8",
  "0x391c0cb3c5c95a63",
  "0x4ed8aa4ae3418acb",
  "0x5b9cca4f7763e373",
  "0x682e6ff3d6b2b8a3",
  "0x748f82ee5defb2fc",
  "0x78a5636f43172f60",
  "0x84c87814a1f0ab72",
  "0x8cc702081a6439ec",
  "0x90befffa23631e28",
  "0xa4506cebde82bde9",
  "0xbef9a3f7b2c67915",
  "0xc67178f2e372532b",
  "0xca273eceea26619c",
  "0xd186b8c721c0c207",
  "0xeada7dd6cde0eb1e",
  "0xf57d4f7fee6ed178",
  "0x06f067aa72176fba",
  "0x0a637dc5a2c898a6",
  "0x113f9804bef90dae",
  "0x1b710b35131c471b",
  "0x28db77f523047d84",
  "0x32caab7b40c72493",
  "0x3c9ebe0a15c9bebc",
  "0x431d67c49c100d4c",
  "0x4cc5d4becb3e42b6",
  "0x597f299cfc657e2a",
  "0x5fcb6fab3ad6faec",
  "0x6c44198c4a475817"
].map((e) => BigInt(e))), R0 = Oh[0], L0 = Oh[1], bn = /* @__PURE__ */ new Uint32Array(80), vn = /* @__PURE__ */ new Uint32Array(80);
class H0 extends sl {
  constructor(t) {
    super(128, t, 16, !1);
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: d, Fh: u, Fl: f, Gh: h, Gl: y, Hh: w, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, l, d, u, f, h, y, w, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, d, u, f, h, y, w, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = d | 0, this.Fh = u | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = y | 0, this.Hh = w | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let b = 0; b < 16; b++, n += 4)
      bn[b] = t.getUint32(n), vn[b] = t.getUint32(n += 4);
    for (let b = 16; b < 80; b++) {
      const O = bn[b - 15] | 0, B = vn[b - 15] | 0, R = Ar(O, B, 1) ^ Ar(O, B, 8) ^ Zd(O, B, 7), H = _r(O, B, 1) ^ _r(O, B, 8) ^ Yd(O, B, 7), L = bn[b - 2] | 0, T = vn[b - 2] | 0, D = Ar(L, T, 19) ^ uo(L, T, 61) ^ Zd(L, T, 6), z = _r(L, T, 19) ^ fo(L, T, 61) ^ Yd(L, T, 6), E = I0(H, z, vn[b - 7], vn[b - 16]), x = B0(E, R, D, bn[b - 7], bn[b - 16]);
      bn[b] = x | 0, vn[b] = E | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: d, Eh: u, El: f, Fh: h, Fl: y, Gh: w, Gl: p, Hh: g, Hl: k } = this;
    for (let b = 0; b < 80; b++) {
      const O = Ar(u, f, 14) ^ Ar(u, f, 18) ^ uo(u, f, 41), B = _r(u, f, 14) ^ _r(u, f, 18) ^ fo(u, f, 41), R = u & h ^ ~u & w, H = f & y ^ ~f & p, L = T0(k, B, H, L0[b], vn[b]), T = P0(L, g, O, R, R0[b], bn[b]), D = L | 0, z = Ar(r, i, 28) ^ uo(r, i, 34) ^ uo(r, i, 39), E = _r(r, i, 28) ^ fo(r, i, 34) ^ fo(r, i, 39), x = r & o ^ r & a ^ o & a, v = i & s ^ i & c ^ s & c;
      g = w | 0, k = p | 0, w = h | 0, p = y | 0, h = u | 0, y = f | 0, { h: u, l: f } = Wt(l | 0, d | 0, T | 0, D | 0), l = a | 0, d = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const m = A0(D, E, v);
      r = _0(m, T, z, x), i = m | 0;
    }
    ({ h: r, l: i } = Wt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Wt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Wt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: d } = Wt(this.Dh | 0, this.Dl | 0, l | 0, d | 0), { h: u, l: f } = Wt(this.Eh | 0, this.El | 0, u | 0, f | 0), { h, l: y } = Wt(this.Fh | 0, this.Fl | 0, h | 0, y | 0), { h: w, l: p } = Wt(this.Gh | 0, this.Gl | 0, w | 0, p | 0), { h: g, l: k } = Wt(this.Hh | 0, this.Hl | 0, g | 0, k | 0), this.set(r, i, o, s, a, c, l, d, u, f, h, y, w, p, g, k);
  }
  roundClean() {
    Mt(bn, vn);
  }
  destroy() {
    this.destroyed = !0, Mt(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
class N0 extends H0 {
  Ah = Re[0] | 0;
  Al = Re[1] | 0;
  Bh = Re[2] | 0;
  Bl = Re[3] | 0;
  Ch = Re[4] | 0;
  Cl = Re[5] | 0;
  Dh = Re[6] | 0;
  Dl = Re[7] | 0;
  Eh = Re[8] | 0;
  El = Re[9] | 0;
  Fh = Re[10] | 0;
  Fl = Re[11] | 0;
  Gh = Re[12] | 0;
  Gl = Re[13] | 0;
  Hh = Re[14] | 0;
  Hl = Re[15] | 0;
  constructor() {
    super(64);
  }
}
const ue = /* @__PURE__ */ Zs(
  () => new C0(),
  /* @__PURE__ */ Ph(1)
), wc = /* @__PURE__ */ Zs(
  () => new N0(),
  /* @__PURE__ */ Ph(3)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const oe = (e, t, n) => zt(e, t, n), Ch = Dt, G = rl, dt = (...e) => g0(...e), qe = (e) => il(e), al = Ih, cl = (e) => ol(e), ns = /* @__PURE__ */ BigInt(0), gc = /* @__PURE__ */ BigInt(1);
function Kn(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function ll(e) {
  if (typeof e == "bigint") {
    if (!Vo(e))
      throw new RangeError("positive bigint expected, got " + e);
  } else
    Ch(e);
  return e;
}
function rs(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected number, got type=" + typeof e);
  }
  if (!Number.isSafeInteger(e)) {
    const n = t && `"${t}" `;
    throw new RangeError(n + "expected safe integer, got " + e);
  }
}
function ho(e) {
  const t = ll(e).toString(16);
  return t.length & 1 ? "0" + t : t;
}
function Rh(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  return e === "" ? ns : BigInt("0x" + e);
}
function Xi(e) {
  return Rh(rl(e));
}
function Gr(e) {
  return Rh(rl(mc(zt(e)).reverse()));
}
function dl(e, t) {
  if (Dt(t), t === 0)
    throw new RangeError("zero length");
  e = ll(e);
  const n = e.toString(16);
  if (n.length > t * 2)
    throw new RangeError("number too large");
  return il(n.padStart(t * 2, "0"));
}
function Lh(e, t) {
  return dl(e, t).reverse();
}
function U0(e, t) {
  if (e = oe(e), t = oe(t), e.length !== t.length)
    return !1;
  let n = 0;
  for (let r = 0; r < e.length; r++)
    n |= e[r] ^ t[r];
  return n === 0;
}
function mc(e) {
  return Uint8Array.from(oe(e));
}
function D0(e) {
  if (typeof e != "string")
    throw new TypeError("ascii string expected, got " + typeof e);
  return Uint8Array.from(e, (t, n) => {
    const r = t.charCodeAt(0);
    if (t.length !== 1 || r > 127)
      throw new RangeError(`string contains non-ASCII character "${e[n]}" with code ${r} at position ${n}`);
    return r;
  });
}
const Vo = (e) => typeof e == "bigint" && ns <= e;
function z0(e, t, n) {
  return Vo(e) && Vo(t) && Vo(n) && t <= e && e < n;
}
function is(e, t, n, r) {
  if (!z0(t, n, r))
    throw new RangeError("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function ul(e) {
  if (e < ns)
    throw new Error("expected non-negative bigint, got " + e);
  let t;
  for (t = 0; e > ns; e >>= gc, t += 1)
    ;
  return t;
}
const fl = (e) => (gc << BigInt(e)) - gc;
function M0(e, t, n) {
  if (Dt(e, "hashLen"), Dt(t, "qByteLen"), typeof n != "function")
    throw new TypeError("hmacFn must be a function");
  const r = (p) => new Uint8Array(p), i = Uint8Array.of(), o = Uint8Array.of(0), s = Uint8Array.of(1), a = 1e3;
  let c = r(e), l = r(e), d = 0;
  const u = () => {
    c.fill(1), l.fill(0), d = 0;
  }, f = (...p) => n(l, dt(c, ...p)), h = (p = i) => {
    l = f(o, p), c = f(), p.length !== 0 && (l = f(s, p), c = f());
  }, y = () => {
    if (d++ >= a)
      throw new Error("drbg: tried max amount of iterations");
    let p = 0;
    const g = [];
    for (; p < t; ) {
      c = f();
      const k = c.slice();
      g.push(k), p += c.length;
    }
    return dt(...g);
  };
  return (p, g) => {
    u(), h(p);
    let k;
    for (; (k = g(y())) === void 0; )
      h();
    return u(), k;
  };
}
function ii(e, t = {}, n = {}) {
  if (Object.prototype.toString.call(e) !== "[object Object]")
    throw new TypeError("expected valid options object");
  function r(o, s, a) {
    if (!a && s !== "function" && !Object.hasOwn(e, o))
      throw new TypeError(`param "${o}" is invalid: expected own property`);
    const c = e[o];
    if (a && c === void 0)
      return;
    const l = typeof c;
    if (l !== s || c === null)
      throw new TypeError(`param "${o}" is invalid: expected ${s}, got ${l}`);
  }
  const i = (o, s) => Object.entries(o).forEach(([a, c]) => r(a, c, s));
  i(t, !1), i(n, !0);
}
const Xd = () => {
  throw new Error("not implemented");
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const De = /* @__PURE__ */ BigInt(0), xe = /* @__PURE__ */ BigInt(1), Jn = /* @__PURE__ */ BigInt(2), Hh = /* @__PURE__ */ BigInt(3), Nh = /* @__PURE__ */ BigInt(4), Uh = /* @__PURE__ */ BigInt(5), K0 = /* @__PURE__ */ BigInt(7), Dh = /* @__PURE__ */ BigInt(8), F0 = /* @__PURE__ */ BigInt(9), zh = /* @__PURE__ */ BigInt(16);
function Ie(e, t) {
  if (t <= De)
    throw new Error("mod: expected positive modulus, got " + t);
  const n = e % t;
  return n >= De ? n : t + n;
}
function we(e, t, n) {
  if (t < De)
    throw new Error("pow2: expected non-negative exponent, got " + t);
  let r = e;
  for (; t-- > De; )
    r *= r, r %= n;
  return r;
}
function Jd(e, t) {
  if (e === De)
    throw new Error("invert: expected non-zero number");
  if (t <= De)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = Ie(e, t), r = t, i = De, o = xe;
  for (; n !== De; ) {
    const a = r / n, c = r - n * a, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== xe)
    throw new Error("invert: does not exist");
  return Ie(i, t);
}
function hl(e, t, n) {
  const r = e;
  if (!r.eql(r.sqr(t), n))
    throw new Error("Cannot find square root");
}
function Mh(e, t) {
  const n = e, r = (n.ORDER + xe) / Nh, i = n.pow(t, r);
  return hl(n, i, t), i;
}
function j0(e, t) {
  const n = e, r = (n.ORDER - Uh) / Dh, i = n.mul(t, Jn), o = n.pow(i, r), s = n.mul(t, o), a = n.mul(n.mul(s, Jn), o), c = n.mul(s, n.sub(a, n.ONE));
  return hl(n, c, t), c;
}
function V0(e) {
  const t = Ys(e), n = Kh(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + K0) / zh;
  return ((a, c) => {
    const l = a;
    let d = l.pow(c, s), u = l.mul(d, r);
    const f = l.mul(d, i), h = l.mul(d, o), y = l.eql(l.sqr(u), c), w = l.eql(l.sqr(f), c);
    d = l.cmov(d, u, y), u = l.cmov(h, f, w);
    const p = l.eql(l.sqr(u), c), g = l.cmov(d, u, p);
    return hl(l, g, c), g;
  });
}
function Kh(e) {
  if (e < Hh)
    throw new Error("sqrt is not defined for small field");
  let t = e - xe, n = 0;
  for (; t % Jn === De; )
    t /= Jn, n++;
  let r = Jn;
  const i = Ys(e);
  for (; Qd(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return Mh;
  let o = i.pow(r, t);
  const s = (t + xe) / Jn;
  return function(c, l) {
    const d = c;
    if (d.is0(l))
      return l;
    if (Qd(d, l) !== 1)
      throw new Error("Cannot find square root");
    let u = n, f = d.mul(d.ONE, o), h = d.pow(l, t), y = d.pow(l, s);
    for (; !d.eql(h, d.ONE); ) {
      if (d.is0(h))
        return d.ZERO;
      let w = 1, p = d.sqr(h);
      for (; !d.eql(p, d.ONE); )
        if (w++, p = d.sqr(p), w === u)
          throw new Error("Cannot find square root");
      const g = xe << BigInt(u - w - 1), k = d.pow(f, g);
      u = w, f = d.sqr(k), h = d.mul(h, f), y = d.mul(y, k);
    }
    return y;
  };
}
function q0(e) {
  return e % Nh === Hh ? Mh : e % Dh === Uh ? j0 : e % zh === F0 ? V0(e) : Kh(e);
}
const Wn = (e, t) => (Ie(e, t) & xe) === xe, W0 = [
  "create",
  "isValid",
  "is0",
  "neg",
  "inv",
  "sqrt",
  "sqr",
  "eql",
  "add",
  "sub",
  "mul",
  "pow",
  "div",
  "addN",
  "subN",
  "mulN",
  "sqrN"
];
function G0(e) {
  const t = {
    ORDER: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = W0.reduce((r, i) => (r[i] = "function", r), t);
  if (ii(e, n), rs(e.BYTES, "BYTES"), rs(e.BITS, "BITS"), e.BYTES < 1 || e.BITS < 1)
    throw new Error("invalid field: expected BYTES/BITS > 0");
  if (e.ORDER <= xe)
    throw new Error("invalid field: expected ORDER > 1, got " + e.ORDER);
  return e;
}
function Z0(e, t, n) {
  const r = e;
  if (n < De)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === De)
    return r.ONE;
  if (n === xe)
    return t;
  let i = r.ONE, o = t;
  for (; n > De; )
    n & xe && (i = r.mul(i, o)), o = r.sqr(o), n >>= xe;
  return i;
}
function Fh(e, t, n = !1) {
  const r = e, i = new Array(t.length).fill(n ? r.ZERO : void 0), o = t.reduce((a, c, l) => r.is0(c) ? a : (i[l] = a, r.mul(a, c)), r.ONE), s = r.inv(o);
  return t.reduceRight((a, c, l) => r.is0(c) ? a : (i[l] = r.mul(a, i[l]), r.mul(a, c)), s), i;
}
function Qd(e, t) {
  const n = e, r = (n.ORDER - xe) / Jn, i = n.pow(t, r), o = n.eql(i, n.ONE), s = n.eql(i, n.ZERO), a = n.eql(i, n.neg(n.ONE));
  if (!o && !s && !a)
    throw new Error("invalid Legendre symbol result");
  return o ? 1 : s ? 0 : -1;
}
function Y0(e, t) {
  if (t !== void 0 && Ch(t), e <= De)
    throw new Error("invalid n length: expected positive n, got " + e);
  if (t !== void 0 && t < 1)
    throw new Error("invalid n length: expected positive bit length, got " + t);
  const n = ul(e);
  if (t !== void 0 && t < n)
    throw new Error(`invalid n length: expected bit length (${n}) >= n.length (${t})`);
  const r = t !== void 0 ? t : n, i = Math.ceil(r / 8);
  return { nBitLength: r, nByteLength: i };
}
const eu = /* @__PURE__ */ new WeakMap();
class jh {
  ORDER;
  BITS;
  BYTES;
  isLE;
  ZERO = De;
  ONE = xe;
  _lengths;
  _mod;
  constructor(t, n = {}) {
    if (t <= xe)
      throw new Error("invalid field: expected ORDER > 1, got " + t);
    let r;
    this.isLE = !1, n != null && typeof n == "object" && (typeof n.BITS == "number" && (r = n.BITS), typeof n.sqrt == "function" && Object.defineProperty(this, "sqrt", { value: n.sqrt, enumerable: !0 }), typeof n.isLE == "boolean" && (this.isLE = n.isLE), n.allowedLengths && (this._lengths = Object.freeze(n.allowedLengths.slice())), typeof n.modFromBytes == "boolean" && (this._mod = n.modFromBytes));
    const { nBitLength: i, nByteLength: o } = Y0(t, r);
    if (o > 2048)
      throw new Error("invalid field: expected ORDER of <= 2048 bytes");
    this.ORDER = t, this.BITS = i, this.BYTES = o, Object.freeze(this);
  }
  create(t) {
    return Ie(t, this.ORDER);
  }
  isValid(t) {
    if (typeof t != "bigint")
      throw new TypeError("invalid field element: expected bigint, got " + typeof t);
    return De <= t && t < this.ORDER;
  }
  is0(t) {
    return t === De;
  }
  // is valid and invertible
  isValidNot0(t) {
    return !this.is0(t) && this.isValid(t);
  }
  isOdd(t) {
    return (t & xe) === xe;
  }
  neg(t) {
    return Ie(-t, this.ORDER);
  }
  eql(t, n) {
    return t === n;
  }
  sqr(t) {
    return Ie(t * t, this.ORDER);
  }
  add(t, n) {
    return Ie(t + n, this.ORDER);
  }
  sub(t, n) {
    return Ie(t - n, this.ORDER);
  }
  mul(t, n) {
    return Ie(t * n, this.ORDER);
  }
  pow(t, n) {
    return Z0(this, t, n);
  }
  div(t, n) {
    return Ie(t * Jd(n, this.ORDER), this.ORDER);
  }
  // Same as above, but doesn't normalize
  sqrN(t) {
    return t * t;
  }
  addN(t, n) {
    return t + n;
  }
  subN(t, n) {
    return t - n;
  }
  mulN(t, n) {
    return t * n;
  }
  inv(t) {
    return Jd(t, this.ORDER);
  }
  sqrt(t) {
    let n = eu.get(this);
    return n || eu.set(this, n = q0(this.ORDER)), n(this, t);
  }
  toBytes(t) {
    return this.isLE ? Lh(t, this.BYTES) : dl(t, this.BYTES);
  }
  fromBytes(t, n = !1) {
    oe(t);
    const { _lengths: r, BYTES: i, isLE: o, ORDER: s, _mod: a } = this;
    if (r) {
      if (t.length < 1 || !r.includes(t.length) || t.length > i)
        throw new Error("Field.fromBytes: expected " + r + " bytes, got " + t.length);
      const l = new Uint8Array(i);
      l.set(t, o ? 0 : l.length - t.length), t = l;
    }
    if (t.length !== i)
      throw new Error("Field.fromBytes: expected " + i + " bytes, got " + t.length);
    let c = o ? Gr(t) : Xi(t);
    if (a && (c = Ie(c, s)), !n && !this.isValid(c))
      throw new Error("invalid field element: outside of range 0..ORDER");
    return c;
  }
  // TODO: we don't need it here, move out to separate fn
  invertBatch(t) {
    return Fh(this, t);
  }
  // We can't move this out because Fp6, Fp12 implement it
  // and it's unclear what to return in there.
  cmov(t, n, r) {
    return Kn(r, "condition"), r ? n : t;
  }
}
Object.freeze(jh.prototype);
function Ys(e, t = {}) {
  return new jh(e, t);
}
function Vh(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  if (e <= xe)
    throw new Error("field order must be greater than 1");
  const t = ul(e - xe);
  return Math.ceil(t / 8);
}
function qh(e) {
  const t = Vh(e);
  return t + Math.ceil(t / 2);
}
function Wh(e, t, n = !1) {
  oe(e);
  const r = e.length, i = Vh(t), o = Math.max(qh(t), 16);
  if (r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? Gr(e) : Xi(e), a = Ie(s, t - xe) + xe;
  return n ? Lh(a, i) : dl(a, i);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Zr = /* @__PURE__ */ BigInt(0), Qn = /* @__PURE__ */ BigInt(1);
function os(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function Pi(e, t) {
  const n = Fh(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function Gh(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function $a(e, t) {
  Gh(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = fl(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function tu(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += Qn);
  const l = t * r, d = l + Math.abs(a) - 1, u = a === 0, f = a < 0, h = t % 2 !== 0;
  return { nextN: c, offset: d, isZero: u, isNeg: f, isNegF: h, offsetF: l };
}
const Oa = /* @__PURE__ */ new WeakMap(), Zh = /* @__PURE__ */ new WeakMap();
function Ca(e) {
  return Zh.get(e) || 1;
}
function nu(e) {
  if (e !== Zr)
    throw new Error("invalid wNAF");
}
let Yh = class {
  BASE;
  ZERO;
  Fn;
  bits;
  // Parametrized with a given Point class (not individual point)
  constructor(t, n) {
    this.BASE = t.BASE, this.ZERO = t.ZERO, this.Fn = t.Fn, this.bits = n;
  }
  // non-const time multiplication ladder
  _unsafeLadder(t, n, r = this.ZERO) {
    let i = t;
    for (; n > Zr; )
      n & Qn && (r = r.add(i)), i = i.double(), n >>= Qn;
    return r;
  }
  /**
   * Creates a wNAF precomputation window. Used for caching.
   * Default window size is set by `utils.precompute()` and is equal to 8.
   * Number of precomputed points depends on the curve size:
   * 2^(𝑊−1) * (Math.ceil(𝑛 / 𝑊) + 1), where:
   * - 𝑊 is the window size
   * - 𝑛 is the bitlength of the curve order.
   * For a 256-bit curve and window size 8, the number of precomputed points is 128 * 33 = 4224.
   * @param point - Point instance
   * @param W - window size
   * @returns precomputed point tables flattened to a single array
   */
  precomputeWindow(t, n) {
    const { windows: r, windowSize: i } = $a(n, this.bits), o = [];
    let s = t, a = s;
    for (let c = 0; c < r; c++) {
      a = s, o.push(a);
      for (let l = 1; l < i; l++)
        a = a.add(s), o.push(a);
      s = a.double();
    }
    return o;
  }
  /**
   * Implements ec multiplication using precomputed tables and w-ary non-adjacent form.
   * More compact implementation:
   * https://github.com/paulmillr/noble-secp256k1/blob/47cb1669b6e506ad66b35fe7d76132ae97465da2/index.ts#L502-L541
   * @returns real and fake (for const-time) points
   */
  wNAF(t, n, r) {
    if (!this.Fn.isValid(r))
      throw new Error("invalid scalar");
    let i = this.ZERO, o = this.BASE;
    const s = $a(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: d, isNeg: u, isNegF: f, offsetF: h } = tu(r, a, s);
      r = c, d ? o = o.add(os(f, n[h])) : i = i.add(os(u, n[l]));
    }
    return nu(r), { p: i, f: o };
  }
  /**
   * Implements unsafe EC multiplication using precomputed tables
   * and w-ary non-adjacent form.
   * @param acc - accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = $a(t, this.bits);
    for (let s = 0; s < o.windows && r !== Zr; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: d } = tu(r, s, o);
      if (r = a, !l) {
        const u = n[c];
        i = i.add(d ? u.negate() : u);
      }
    }
    return nu(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Oa.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Oa.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = Ca(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = Ca(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    Gh(n, this.bits), Zh.set(t, n), Oa.delete(t);
  }
  hasCache(t) {
    return Ca(t) !== 1;
  }
};
function X0(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > Zr || r > Zr; )
    n & Qn && (o = o.add(i)), r & Qn && (s = s.add(i)), i = i.double(), n >>= Qn, r >>= Qn;
  return { p1: o, p2: s };
}
function ru(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return G0(t), t;
  } else
    return Ys(e, { isLE: n });
}
function Xh(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > Zr))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = ru(t.p, n.Fp, r), o = ru(t.n, n.Fn, r), a = ["Gx", "Gy", "a", e === "weierstrass" ? "b" : "d"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
function pl(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const xn = /* @__PURE__ */ BigInt(0), $e = /* @__PURE__ */ BigInt(1), Ra = /* @__PURE__ */ BigInt(2), J0 = /* @__PURE__ */ BigInt(8);
function Q0(e, t, n, r) {
  const i = e.sqr(n), o = e.sqr(r), s = e.add(e.mul(t.a, i), o), a = e.add(e.ONE, e.mul(t.d, e.mul(i, o)));
  return e.eql(s, a);
}
function ev(e, t = {}) {
  const n = t, r = Xh("edwards", e, n, n.FpFnLE), { Fp: i, Fn: o } = r;
  let s = r.CURVE;
  const { h: a } = s;
  ii(n, {}, { uvRatio: "function" });
  const c = Ra << BigInt(o.BYTES * 8) - $e, l = (w) => i.create(w), d = n.uvRatio === void 0 ? (w, p) => {
    try {
      return { isValid: !0, value: i.sqrt(i.div(w, p)) };
    } catch {
      return { isValid: !1, value: xn };
    }
  } : n.uvRatio;
  if (!Q0(i, s, s.Gx, s.Gy))
    throw new Error("bad curve params: generator point");
  function u(w, p, g = !1) {
    const k = g ? $e : xn;
    return is("coordinate " + w, p, k, c), p;
  }
  function f(w) {
    if (!(w instanceof h))
      throw new Error("EdwardsPoint expected");
  }
  class h {
    // base / generator point
    static BASE = new h(s.Gx, s.Gy, $e, l(s.Gx * s.Gy));
    // zero / infinity / identity point
    static ZERO = new h(xn, $e, $e, xn);
    // 0, 1, 1, 0
    // math field
    static Fp = i;
    // scalar field
    static Fn = o;
    X;
    Y;
    Z;
    T;
    constructor(p, g, k, b) {
      this.X = u("x", p), this.Y = u("y", g), this.Z = u("z", k, !0), this.T = u("t", b), Object.freeze(this);
    }
    static CURVE() {
      return s;
    }
    /**
     * Create one extended Edwards point from affine coordinates.
     * Does NOT validate that the point is on-curve or torsion-free.
     * Use `.assertValidity()` on adversarial inputs.
     */
    static fromAffine(p) {
      if (p instanceof h)
        throw new Error("extended point not allowed");
      const { x: g, y: k } = p || {};
      return u("x", g), u("y", k), new h(g, k, $e, l(g * k));
    }
    // Uses algo from RFC8032 5.1.3.
    static fromBytes(p, g = !1) {
      const k = i.BYTES, { a: b, d: O } = s;
      p = mc(oe(p, k, "point")), Kn(g, "zip215");
      const B = mc(p), R = p[k - 1];
      B[k - 1] = R & -129;
      const H = Gr(B), L = g ? c : i.ORDER;
      is("point.y", H, xn, L);
      const T = l(H * H), D = l(T - $e), z = l(O * T - b);
      let { isValid: E, value: x } = d(D, z);
      if (!E)
        throw new Error("bad point: invalid y coordinate");
      const v = (x & $e) === $e, m = (R & 128) !== 0;
      if (!g && x === xn && m)
        throw new Error("bad point: x=0 and x_0=1");
      return m !== v && (x = l(-x)), h.fromAffine({ x, y: H });
    }
    static fromHex(p, g = !1) {
      return h.fromBytes(qe(p), g);
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    precompute(p = 8, g = !0) {
      return y.createCache(this, p), g || this.multiply(Ra), this;
    }
    // Useful in fromAffine() - not for fromBytes(), which always created valid points.
    assertValidity() {
      const p = this, { a: g, d: k } = s;
      if (p.is0())
        throw new Error("bad point: ZERO");
      const { X: b, Y: O, Z: B, T: R } = p, H = l(b * b), L = l(O * O), T = l(B * B), D = l(T * T), z = l(H * g), E = l(T * l(z + L)), x = l(D + l(k * l(H * L)));
      if (E !== x)
        throw new Error("bad point: equation left != right (1)");
      const v = l(b * O), m = l(B * R);
      if (v !== m)
        throw new Error("bad point: equation left != right (2)");
    }
    // Compare one point to another.
    equals(p) {
      f(p);
      const { X: g, Y: k, Z: b } = this, { X: O, Y: B, Z: R } = p, H = l(g * R), L = l(O * b), T = l(k * R), D = l(B * b);
      return H === L && T === D;
    }
    is0() {
      return this.equals(h.ZERO);
    }
    negate() {
      return new h(l(-this.X), this.Y, this.Z, l(-this.T));
    }
    // Fast algo for doubling Extended Point.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#doubling-dbl-2008-hwcd
    // Cost: 4M + 4S + 1*a + 6add + 1*2.
    double() {
      const { a: p } = s, { X: g, Y: k, Z: b } = this, O = l(g * g), B = l(k * k), R = l(Ra * l(b * b)), H = l(p * O), L = g + k, T = l(l(L * L) - O - B), D = H + B, z = D - R, E = H - B, x = l(T * z), v = l(D * E), m = l(T * E), A = l(z * D);
      return new h(x, v, A, m);
    }
    // Fast algo for adding 2 Extended Points.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#addition-add-2008-hwcd
    // Cost: 9M + 1*a + 1*d + 7add.
    add(p) {
      f(p);
      const { a: g, d: k } = s, { X: b, Y: O, Z: B, T: R } = this, { X: H, Y: L, Z: T, T: D } = p, z = l(b * H), E = l(O * L), x = l(R * k * D), v = l(B * T), m = l((b + O) * (H + L) - z - E), A = v - x, $ = v + x, S = l(E - g * z), I = l(m * A), P = l($ * S), C = l(m * S), N = l(A * $);
      return new h(I, P, N, C);
    }
    subtract(p) {
      return f(p), this.add(p.negate());
    }
    // Constant-time multiplication.
    multiply(p) {
      if (!o.isValidNot0(p))
        throw new RangeError("invalid scalar: expected 1 <= sc < curve.n");
      const { p: g, f: k } = y.cached(this, p, (b) => Pi(h, b));
      return Pi(h, [g, k])[0];
    }
    // Non-constant-time multiplication. Uses double-and-add algorithm.
    // It's faster, but should only be used when you don't care about
    // an exposed private key e.g. sig verification.
    // Keeps the same subgroup-scalar contract: 0 is allowed for public-scalar callers, but
    // n and larger values are rejected instead of being reduced mod n to the identity point.
    multiplyUnsafe(p) {
      if (!o.isValid(p))
        throw new RangeError("invalid scalar: expected 0 <= sc < curve.n");
      return p === xn ? h.ZERO : this.is0() || p === $e ? this : y.unsafe(this, p, (g) => Pi(h, g));
    }
    // Checks if point is of small order.
    // If you add something to small order point, you will have "dirty"
    // point with torsion component.
    // Clears cofactor and checks if the result is 0.
    isSmallOrder() {
      return this.clearCofactor().is0();
    }
    // Multiplies point by curve order and checks if the result is 0.
    // Returns `false` is the point is dirty.
    isTorsionFree() {
      return y.unsafe(this, s.n).is0();
    }
    // Converts Extended point to default (x, y) coordinates.
    // Can accept precomputed Z^-1 - for example, from invertBatch.
    toAffine(p) {
      const g = this;
      let k = p;
      const { X: b, Y: O, Z: B } = g, R = g.is0();
      k == null && (k = R ? J0 : i.inv(B));
      const H = l(b * k), L = l(O * k), T = i.mul(B, k);
      if (R)
        return { x: xn, y: $e };
      if (T !== $e)
        throw new Error("invZ was invalid");
      return { x: H, y: L };
    }
    clearCofactor() {
      return a === $e ? this : this.multiplyUnsafe(a);
    }
    toBytes() {
      const { x: p, y: g } = this.toAffine(), k = i.toBytes(g);
      return k[k.length - 1] |= p & $e ? 128 : 0, k;
    }
    toHex() {
      return G(this.toBytes());
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const y = new Yh(h, o.BITS);
  return o.BITS >= 8 && h.BASE.precompute(8), Object.freeze(h.prototype), Object.freeze(h), h;
}
class tv {
  static BASE;
  static ZERO;
  static Fp;
  static Fn;
  ep;
  /**
   * Wrap one internal Edwards representative directly.
   * This is not a canonical encoding boundary: alternate Edwards
   * representatives may still describe the same abstract wrapper element.
   */
  constructor(t) {
    this.ep = t;
  }
  // Static methods that must be implemented by subclasses
  static fromBytes(t) {
    Xd();
  }
  static fromHex(t) {
    Xd();
  }
  get x() {
    return this.toAffine().x;
  }
  get y() {
    return this.toAffine().y;
  }
  // Common implementations
  clearCofactor() {
    return this;
  }
  assertValidity() {
    this.ep.assertValidity();
  }
  /**
   * Return affine coordinates of the current internal Edwards representative.
   * This is a convenience helper, not a canonical Ristretto/Decaf encoding.
   * Equal abstract elements may expose different `x` / `y`; use
   * `toBytes()` / `fromBytes()` for canonical roundtrips.
   */
  toAffine(t) {
    return this.ep.toAffine(t);
  }
  toHex() {
    return G(this.toBytes());
  }
  toString() {
    return this.toHex();
  }
  isTorsionFree() {
    return !0;
  }
  isSmallOrder() {
    return !1;
  }
  add(t) {
    return this.assertSame(t), this.init(this.ep.add(t.ep));
  }
  subtract(t) {
    return this.assertSame(t), this.init(this.ep.subtract(t.ep));
  }
  multiply(t) {
    return this.init(this.ep.multiply(t));
  }
  multiplyUnsafe(t) {
    return this.init(this.ep.multiplyUnsafe(t));
  }
  double() {
    return this.init(this.ep.double());
  }
  negate() {
    return this.init(this.ep.negate());
  }
  precompute(t, n) {
    return this.ep.precompute(t, n), this;
  }
}
function nv(e, t, n = {}) {
  if (typeof t != "function")
    throw new Error('"hash" function param is required');
  const r = t, i = n;
  ii(i, {}, {
    adjustScalarBytes: "function",
    randomBytes: "function",
    domain: "function",
    prehash: "function",
    zip215: "boolean",
    mapToCurve: "function"
  });
  const { prehash: o } = i, { BASE: s, Fp: a, Fn: c } = e, l = r.outputLen, d = 2 * a.BYTES;
  if (l !== void 0 && (rs(l, "hash.outputLen"), l !== d))
    throw new Error(`hash.outputLen must be ${d}, got ${l}`);
  const u = i.randomBytes === void 0 ? cl : i.randomBytes, f = i.adjustScalarBytes === void 0 ? (E) => E : i.adjustScalarBytes, h = i.domain === void 0 ? (E, x, v) => {
    if (Kn(v, "phflag"), x.length || v)
      throw new Error("Contexts/pre-hash are not supported");
    return E;
  } : i.domain;
  function y(E) {
    return c.create(Gr(E));
  }
  function w(E) {
    const x = H.secretKey;
    oe(E, H.secretKey, "secretKey");
    const v = oe(r(E), 2 * x, "hashedSecretKey"), m = f(v.slice(0, x)), A = v.slice(x, 2 * x), $ = y(m);
    return { head: m, prefix: A, scalar: $ };
  }
  function p(E) {
    const { head: x, prefix: v, scalar: m } = w(E), A = s.multiply(m), $ = A.toBytes();
    return { head: x, prefix: v, scalar: m, point: A, pointBytes: $ };
  }
  function g(E) {
    return p(E).pointBytes;
  }
  function k(E = Uint8Array.of(), ...x) {
    const v = dt(...x);
    return y(r(h(v, oe(E, void 0, "context"), !!o)));
  }
  function b(E, x, v = {}) {
    E = oe(E, void 0, "message"), o && (E = o(E));
    const { prefix: m, scalar: A, pointBytes: $ } = p(x), S = k(v.context, m, E), I = s.multiply(S).toBytes(), P = k(v.context, I, $, E), C = c.create(S + P * A);
    if (!c.isValid(C))
      throw new Error("sign failed: invalid s");
    const N = dt(I, c.toBytes(C));
    return oe(N, H.signature, "result");
  }
  const O = {
    zip215: i.zip215
  };
  function B(E, x, v, m = O) {
    const { context: A } = m, $ = m.zip215 === void 0 ? !!O.zip215 : m.zip215, S = H.signature;
    E = oe(E, S, "signature"), x = oe(x, void 0, "message"), v = oe(v, H.publicKey, "publicKey"), $ !== void 0 && Kn($, "zip215"), o && (x = o(x));
    const I = S / 2, P = E.subarray(0, I), C = Gr(E.subarray(I, S));
    let N, K, U;
    try {
      N = e.fromBytes(v, $), K = e.fromBytes(P, $), U = s.multiplyUnsafe(C);
    } catch {
      return !1;
    }
    if (!$ && N.isSmallOrder())
      return !1;
    const M = k(A, P, v, x);
    return K.add(N.multiplyUnsafe(M)).subtract(U).clearCofactor().is0();
  }
  const R = a.BYTES, H = {
    secretKey: R,
    publicKey: R,
    signature: 2 * R,
    seed: R
  };
  function L(E) {
    return E = E === void 0 ? u(H.seed) : E, oe(E, H.seed, "seed");
  }
  function T(E) {
    return al(E) && E.length === H.secretKey;
  }
  function D(E, x) {
    try {
      return !!e.fromBytes(E, x === void 0 ? O.zip215 : x);
    } catch {
      return !1;
    }
  }
  const z = {
    getExtendedPublicKey: p,
    randomSecretKey: L,
    isValidSecretKey: T,
    isValidPublicKey: D,
    /**
     * Converts ed public key to x public key. Uses formula:
     * - ed25519:
     *   - `(u, v) = ((1+y)/(1-y), sqrt(-486664)*u/x)`
     *   - `(x, y) = (sqrt(-486664)*u/v, (u-1)/(u+1))`
     * - ed448:
     *   - `(u, v) = ((y-1)/(y+1), sqrt(156324)*u/x)`
     *   - `(x, y) = (sqrt(156324)*u/v, (1+u)/(1-u))`
     */
    toMontgomery(E) {
      const { y: x } = e.fromBytes(E), v = H.publicKey, m = v === 32;
      if (!m && v !== 57)
        throw new Error("only defined for 25519 and 448");
      const A = m ? a.div($e + x, $e - x) : a.div(x - $e, x + $e);
      return a.toBytes(A);
    },
    toMontgomerySecret(E) {
      const x = H.secretKey;
      oe(E, x);
      const v = r(E.subarray(0, x));
      return f(v).subarray(0, x);
    }
  };
  return Object.freeze(H), Object.freeze(z), Object.freeze({
    keygen: pl(L, g),
    getPublicKey: g,
    sign: b,
    verify: B,
    utils: z,
    Point: e,
    lengths: H
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const rv = /* @__PURE__ */ BigInt(0), $i = /* @__PURE__ */ BigInt(1), iu = /* @__PURE__ */ BigInt(2), iv = /* @__PURE__ */ BigInt(5), ov = /* @__PURE__ */ BigInt(8), Li = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed"), Jh = {
  p: Li,
  n: BigInt("0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"),
  h: ov,
  a: BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffec"),
  d: BigInt("0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3"),
  Gx: BigInt("0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a"),
  Gy: BigInt("0x6666666666666666666666666666666666666666666666666666666666666658")
};
function sv(e) {
  const t = BigInt(10), n = BigInt(20), r = BigInt(40), i = BigInt(80), o = Li, a = e * e % o * e % o, c = we(a, iu, o) * a % o, l = we(c, $i, o) * e % o, d = we(l, iv, o) * l % o, u = we(d, t, o) * d % o, f = we(u, n, o) * u % o, h = we(f, r, o) * f % o, y = we(h, i, o) * h % o, w = we(y, i, o) * h % o, p = we(w, t, o) * d % o;
  return { pow_p_5_8: we(p, iu, o) * e % o, b2: a };
}
function av(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const bc = /* @__PURE__ */ BigInt("19681161376707505956807079304988542015446066515923890162744021073123829784752");
function Qh(e, t) {
  const n = Li, r = Ie(t * t * t, n), i = Ie(r * r * t, n), o = sv(e * i).pow_p_5_8;
  let s = Ie(e * r * o, n);
  const a = Ie(t * s * s, n), c = s, l = Ie(s * bc, n), d = a === e, u = a === Ie(-e, n), f = a === Ie(-e * bc, n);
  return d && (s = c), (u || f) && (s = l), Wn(s, n) && (s = Ie(-s, n)), { isValid: d || u, value: s };
}
const er = /* @__PURE__ */ ev(Jh, { uvRatio: Qh }), Gn = er.Fp, cv = er.Fn;
function lv(e) {
  return nv(er, wc, Object.assign({ adjustScalarBytes: av, zip215: !0 }, e));
}
const vt = /* @__PURE__ */ lv({}), ou = bc, dv = /* @__PURE__ */ BigInt("54469307008909316920995813868745141605393597292927456921205312896311721017578"), su = (e) => Qh($i, e), uv = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), fv = (e) => Gn.create(Gr(e) & uv);
class Qe extends tv {
  // Do NOT change syntax: the following gymnastics is done,
  // because typescript strips comments, which makes bundlers disable tree-shaking.
  // prettier-ignore
  static BASE = new Qe(er.BASE);
  // prettier-ignore
  static ZERO = new Qe(er.ZERO);
  // prettier-ignore
  static Fp = Gn;
  // prettier-ignore
  static Fn = cv;
  constructor(t) {
    super(t);
  }
  /**
   * Create one Ristretto255 point from affine Edwards coordinates.
   * This wraps the internal Edwards representative directly and is not a
   * canonical ristretto255 decoding path.
   * Use `toBytes()` / `fromBytes()` if canonical ristretto255 bytes matter.
   */
  static fromAffine(t) {
    return new Qe(er.fromAffine(t));
  }
  assertSame(t) {
    if (!(t instanceof Qe))
      throw new Error("RistrettoPoint expected");
  }
  init(t) {
    return new Qe(t);
  }
  static fromBytes(t) {
    zt(t, 32);
    const { a: n, d: r } = Jh, i = Li, o = (O) => Gn.create(O), s = fv(t);
    if (!U0(Gn.toBytes(s), t) || Wn(s, i))
      throw new Error("invalid ristretto255 encoding 1");
    const a = o(s * s), c = o($i + n * a), l = o($i - n * a), d = o(c * c), u = o(l * l), f = o(n * r * d - u), { isValid: h, value: y } = su(o(f * u)), w = o(y * l), p = o(y * w * f);
    let g = o((s + s) * w);
    Wn(g, i) && (g = o(-g));
    const k = o(c * p), b = o(g * k);
    if (!h || Wn(b, i) || k === rv)
      throw new Error("invalid ristretto255 encoding 2");
    return new Qe(new er(g, k, $i, b));
  }
  /**
   * Converts ristretto-encoded string to ristretto point.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-decode).
   * @param hex - Ristretto-encoded 32 bytes. Not every 32-byte string is valid ristretto encoding
   */
  static fromHex(t) {
    return Qe.fromBytes(il(t));
  }
  /**
   * Encodes ristretto point to Uint8Array.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-encode).
   */
  toBytes() {
    let { X: t, Y: n, Z: r, T: i } = this.ep;
    const o = Li, s = (p) => Gn.create(p), a = s(s(r + n) * s(r - n)), c = s(t * n), l = s(c * c), { value: d } = su(s(a * l)), u = s(d * a), f = s(d * c), h = s(u * f * i);
    let y;
    if (Wn(i * h, o)) {
      let p = s(n * ou), g = s(t * ou);
      t = p, n = g, y = s(u * dv);
    } else
      y = f;
    Wn(t * h, o) && (n = s(-n));
    let w = s((r - n) * y);
    return Wn(w, o) && (w = s(-w)), Gn.toBytes(w);
  }
  /**
   * Compares two Ristretto points.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-equals).
   */
  equals(t) {
    this.assertSame(t);
    const { X: n, Y: r } = this.ep, { X: i, Y: o } = t.ep, s = (l) => Gn.create(l), a = s(n * o) === s(r * i), c = s(r * o) === s(n * i);
    return a || c;
  }
  is0() {
    return this.equals(Qe.ZERO);
  }
}
Object.freeze(Qe.BASE);
Object.freeze(Qe.ZERO);
Object.freeze(Qe.prototype);
Object.freeze(Qe);
class au {
  oHash;
  iHash;
  blockLen;
  outputLen;
  canXOF = !1;
  finished = !1;
  destroyed = !1;
  constructor(t, n) {
    if (Bh(t), zt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), Mt(i);
  }
  update(t) {
    return Wr(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    Wr(this), nl(t, this), this.finished = !0;
    const n = t.subarray(0, this.outputLen);
    this.iHash.digestInto(n), this.oHash.update(n), this.oHash.digestInto(n), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.oHash.outputLen);
    return this.digestInto(t), t;
  }
  _cloneInto(t) {
    t ||= Object.create(Object.getPrototypeOf(this), {});
    const { oHash: n, iHash: r, finished: i, destroyed: o, blockLen: s, outputLen: a } = this;
    return t = t, t.finished = i, t.destroyed = o, t.blockLen = s, t.outputLen = a, t.oHash = n._cloneInto(t.oHash), t.iHash = r._cloneInto(t.iHash), t;
  }
  clone() {
    return this._cloneInto();
  }
  destroy() {
    this.destroyed = !0, this.oHash.destroy(), this.iHash.destroy();
  }
}
const vc = /* @__PURE__ */ (() => {
  const e = ((t, n, r) => new au(t, n).update(r).digest());
  return e.create = (t, n) => new au(t, n), e;
})();
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const cu = (e, t) => (e + (e >= 0 ? t : -t) / ep) / t;
function hv(e, t, n) {
  is("scalar", e, Ct, n);
  const [[r, i], [o, s]] = t, a = cu(s * e, n), c = cu(-i * e, n);
  let l = e - a * r - c * o, d = -a * i - c * s;
  const u = l < Ct, f = d < Ct;
  u && (l = -l), f && (d = -d);
  const h = fl(Math.ceil(ul(n) / 2)) + Nn;
  if (l < Ct || l >= h || d < Ct || d >= h)
    throw new Error("splitScalar (endomorphism): failed for k");
  return { k1neg: u, k1: l, k2neg: f, k2: d };
}
function xc(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function La(e, t) {
  ii(e);
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Kn(n.lowS, "lowS"), Kn(n.prehash, "prehash"), n.format !== void 0 && xc(n.format), n;
}
let pv = class extends Error {
  constructor(t = "") {
    super(t);
  }
};
const wt = {
  // asn.1 DER encoding utils
  Err: pv,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = wt;
      if (rs(e, "tag"), e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (typeof t != "string")
        throw new TypeError('"data" expected string, got type=' + typeof t);
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = ho(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? ho(i.length / 2 | 128) : "";
      return ho(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = wt;
      t = oe(t, void 0, "DER data");
      let r = 0;
      if (e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (t.length < 2 || t[r++] !== e)
        throw new n("tlv.decode: wrong tlv");
      const i = t[r++], o = !!(i & 128);
      let s = 0;
      if (!o)
        s = i;
      else {
        const c = i & 127;
        if (!c)
          throw new n("tlv.decode(long): indefinite length not supported");
        if (c > 4)
          throw new n("tlv.decode(long): byte length is too big");
        const l = t.subarray(r, r + c);
        if (l.length !== c)
          throw new n("tlv.decode: length bytes not complete");
        if (l[0] === 0)
          throw new n("tlv.decode(long): zero leftmost byte");
        for (const d of l)
          s = s << 8 | d;
        if (r += c, s < 128)
          throw new n("tlv.decode(long): not minimal encoding");
      }
      const a = t.subarray(r, r + s);
      if (a.length !== s)
        throw new n("tlv.decode: wrong value length");
      return { v: a, l: t.subarray(r + s) };
    }
  },
  // https://crypto.stackexchange.com/a/57734 Leftmost bit of first byte is 'negative' flag,
  // since we always use positive integers here. It must always be empty:
  // - add zero byte if exists
  // - if next byte doesn't have a flag, leading zero is not allowed (minimal encoding)
  _int: {
    encode(e) {
      const { Err: t } = wt;
      if (ll(e), e < Ct)
        throw new t("integer: negative integers are not allowed");
      let n = ho(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = wt;
      if (e.length < 1)
        throw new t("invalid signature integer: empty");
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e.length > 1 && e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return Xi(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = wt, i = oe(e, void 0, "signature"), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: d } = r.decode(2, c);
    if (d.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = wt, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
};
Object.freeze(wt._tlv);
Object.freeze(wt._int);
Object.freeze(wt);
const Ct = /* @__PURE__ */ BigInt(0), Nn = /* @__PURE__ */ BigInt(1), ep = /* @__PURE__ */ BigInt(2), po = /* @__PURE__ */ BigInt(3), yv = /* @__PURE__ */ BigInt(4);
function wv(e, t = {}) {
  const n = Xh("weierstrass", e, t), r = n.Fp, i = n.Fn;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  ii(t, {}, {
    allowInfinityPoint: "boolean",
    clearCofactor: "function",
    isTorsionFree: "function",
    fromBytes: "function",
    toBytes: "function",
    endo: "object"
  });
  const { endo: c, allowInfinityPoint: l } = t;
  if (c && (!r.is0(o.a) || typeof c.beta != "bigint" || !Array.isArray(c.basises)))
    throw new Error('invalid endo: expected "beta": bigint and "basises": array');
  const d = np(r, i);
  function u() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function f(z, E, x) {
    if (l && E.is0())
      return Uint8Array.of(0);
    const { x: v, y: m } = E.toAffine(), A = r.toBytes(v);
    if (Kn(x, "isCompressed"), x) {
      u();
      const $ = !r.isOdd(m);
      return dt(tp($), A);
    } else
      return dt(Uint8Array.of(4), A, r.toBytes(m));
  }
  function h(z) {
    oe(z, void 0, "Point");
    const { publicKey: E, publicKeyUncompressed: x } = d, v = z.length, m = z[0], A = z.subarray(1);
    if (l && v === 1 && m === 0)
      return { x: r.ZERO, y: r.ZERO };
    if (v === E && (m === 2 || m === 3)) {
      const $ = r.fromBytes(A);
      if (!r.isValid($))
        throw new Error("bad point: is not on curve, wrong x");
      const S = p($);
      let I;
      try {
        I = r.sqrt(S);
      } catch (N) {
        const K = N instanceof Error ? ": " + N.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + K);
      }
      u();
      const P = r.isOdd(I);
      return (m & 1) === 1 !== P && (I = r.neg(I)), { x: $, y: I };
    } else if (v === x && m === 4) {
      const $ = r.BYTES, S = r.fromBytes(A.subarray(0, $)), I = r.fromBytes(A.subarray($, $ * 2));
      if (!g(S, I))
        throw new Error("bad point: is not on curve");
      return { x: S, y: I };
    } else
      throw new Error(`bad point: got length ${v}, expected compressed=${E} or uncompressed=${x}`);
  }
  const y = t.toBytes === void 0 ? f : t.toBytes, w = t.fromBytes === void 0 ? h : t.fromBytes;
  function p(z) {
    const E = r.sqr(z), x = r.mul(E, z);
    return r.add(r.add(x, r.mul(z, o.a)), o.b);
  }
  function g(z, E) {
    const x = r.sqr(E), v = p(z);
    return r.eql(x, v);
  }
  if (!g(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const k = r.mul(r.pow(o.a, po), yv), b = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(k, b)))
    throw new Error("bad curve params: a or b");
  function O(z, E, x = !1) {
    if (!r.isValid(E) || x && r.is0(E))
      throw new Error(`bad point coordinate ${z}`);
    return E;
  }
  function B(z) {
    if (!(z instanceof L))
      throw new Error("Weierstrass Point expected");
  }
  function R(z) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return hv(z, c.basises, i.ORDER);
  }
  function H(z, E, x, v, m) {
    return x = new L(r.mul(x.X, z), x.Y, x.Z), E = os(v, E), x = os(m, x), E.add(x);
  }
  class L {
    // base / generator point
    static BASE = new L(o.Gx, o.Gy, r.ONE);
    // zero / infinity / identity point
    static ZERO = new L(r.ZERO, r.ONE, r.ZERO);
    // 0, 1, 0
    // math field
    static Fp = r;
    // scalar field
    static Fn = i;
    X;
    Y;
    Z;
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    constructor(E, x, v) {
      this.X = O("x", E), this.Y = O("y", x, !0), this.Z = O("z", v), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(E) {
      const { x, y: v } = E || {};
      if (!E || !r.isValid(x) || !r.isValid(v))
        throw new Error("invalid affine point");
      if (E instanceof L)
        throw new Error("projective point not allowed");
      return r.is0(x) && r.is0(v) ? L.ZERO : new L(x, v, r.ONE);
    }
    static fromBytes(E) {
      const x = L.fromAffine(w(oe(E, void 0, "point")));
      return x.assertValidity(), x;
    }
    static fromHex(E) {
      return L.fromBytes(qe(E));
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    /**
     *
     * @param windowSize
     * @param isLazy - true will defer table computation until the first multiplication
     * @returns
     */
    precompute(E = 8, x = !0) {
      return D.createCache(this, E), x || this.multiply(po), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      const E = this;
      if (E.is0()) {
        if (t.allowInfinityPoint && r.is0(E.X) && r.eql(E.Y, r.ONE) && r.is0(E.Z))
          return;
        throw new Error("bad point: ZERO");
      }
      const { x, y: v } = E.toAffine();
      if (!r.isValid(x) || !r.isValid(v))
        throw new Error("bad point: x or y not field elements");
      if (!g(x, v))
        throw new Error("bad point: equation left != right");
      if (!E.isTorsionFree())
        throw new Error("bad point: not in prime-order subgroup");
    }
    hasEvenY() {
      const { y: E } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(E);
    }
    /** Compare one point to another. */
    equals(E) {
      B(E);
      const { X: x, Y: v, Z: m } = this, { X: A, Y: $, Z: S } = E, I = r.eql(r.mul(x, S), r.mul(A, m)), P = r.eql(r.mul(v, S), r.mul($, m));
      return I && P;
    }
    /** Flips point to one corresponding to (x, -y) in Affine coordinates. */
    negate() {
      return new L(this.X, r.neg(this.Y), this.Z);
    }
    // Renes-Costello-Batina exception-free doubling formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 3
    // Cost: 8M + 3S + 3*a + 2*b3 + 15add.
    double() {
      const { a: E, b: x } = o, v = r.mul(x, po), { X: m, Y: A, Z: $ } = this;
      let S = r.ZERO, I = r.ZERO, P = r.ZERO, C = r.mul(m, m), N = r.mul(A, A), K = r.mul($, $), U = r.mul(m, A);
      return U = r.add(U, U), P = r.mul(m, $), P = r.add(P, P), S = r.mul(E, P), I = r.mul(v, K), I = r.add(S, I), S = r.sub(N, I), I = r.add(N, I), I = r.mul(S, I), S = r.mul(U, S), P = r.mul(v, P), K = r.mul(E, K), U = r.sub(C, K), U = r.mul(E, U), U = r.add(U, P), P = r.add(C, C), C = r.add(P, C), C = r.add(C, K), C = r.mul(C, U), I = r.add(I, C), K = r.mul(A, $), K = r.add(K, K), C = r.mul(K, U), S = r.sub(S, C), P = r.mul(K, N), P = r.add(P, P), P = r.add(P, P), new L(S, I, P);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(E) {
      B(E);
      const { X: x, Y: v, Z: m } = this, { X: A, Y: $, Z: S } = E;
      let I = r.ZERO, P = r.ZERO, C = r.ZERO;
      const N = o.a, K = r.mul(o.b, po);
      let U = r.mul(x, A), M = r.mul(v, $), F = r.mul(m, S), V = r.add(x, v), q = r.add(A, $);
      V = r.mul(V, q), q = r.add(U, M), V = r.sub(V, q), q = r.add(x, m);
      let W = r.add(A, S);
      return q = r.mul(q, W), W = r.add(U, F), q = r.sub(q, W), W = r.add(v, m), I = r.add($, S), W = r.mul(W, I), I = r.add(M, F), W = r.sub(W, I), C = r.mul(N, q), I = r.mul(K, F), C = r.add(I, C), I = r.sub(M, C), C = r.add(M, C), P = r.mul(I, C), M = r.add(U, U), M = r.add(M, U), F = r.mul(N, F), q = r.mul(K, q), M = r.add(M, F), F = r.sub(U, F), F = r.mul(N, F), q = r.add(q, F), U = r.mul(M, q), P = r.add(P, U), U = r.mul(W, q), I = r.mul(V, I), I = r.sub(I, U), U = r.mul(V, M), C = r.mul(W, C), C = r.add(C, U), new L(I, P, C);
    }
    subtract(E) {
      return B(E), this.add(E.negate());
    }
    is0() {
      return this.equals(L.ZERO);
    }
    /**
     * Constant time multiplication.
     * Uses wNAF method. Windowed method may be 10% faster,
     * but takes 2x longer to generate and consumes 2x memory.
     * Uses precomputes when available.
     * Uses endomorphism for Koblitz curves.
     * @param scalar - by which the point would be multiplied
     * @returns New point
     */
    multiply(E) {
      const { endo: x } = t;
      if (!i.isValidNot0(E))
        throw new RangeError("invalid scalar: out of range");
      let v, m;
      const A = ($) => D.cached(this, $, (S) => Pi(L, S));
      if (x) {
        const { k1neg: $, k1: S, k2neg: I, k2: P } = R(E), { p: C, f: N } = A(S), { p: K, f: U } = A(P);
        m = N.add(U), v = H(x.beta, C, K, $, I);
      } else {
        const { p: $, f: S } = A(E);
        v = $, m = S;
      }
      return Pi(L, [v, m])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(E) {
      const { endo: x } = t, v = this, m = E;
      if (!i.isValid(m))
        throw new RangeError("invalid scalar: out of range");
      if (m === Ct || v.is0())
        return L.ZERO;
      if (m === Nn)
        return v;
      if (D.hasCache(this))
        return this.multiply(m);
      if (x) {
        const { k1neg: A, k1: $, k2neg: S, k2: I } = R(m), { p1: P, p2: C } = X0(L, v, $, I);
        return H(x.beta, P, C, A, S);
      } else
        return D.unsafe(v, m);
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * (X, Y, Z) ∋ (x=X/Z, y=Y/Z).
     * @param invertedZ - Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(E) {
      const x = this;
      let v = E;
      const { X: m, Y: A, Z: $ } = x;
      if (r.eql($, r.ONE))
        return { x: m, y: A };
      const S = x.is0();
      v == null && (v = S ? r.ONE : r.inv($));
      const I = r.mul(m, v), P = r.mul(A, v), C = r.mul($, v);
      if (S)
        return { x: r.ZERO, y: r.ZERO };
      if (!r.eql(C, r.ONE))
        throw new Error("invZ was invalid");
      return { x: I, y: P };
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: E } = t;
      return s === Nn ? !0 : E ? E(L, this) : D.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: E } = t;
      return s === Nn ? this : E ? E(L, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return s === Nn ? this.is0() : this.clearCofactor().is0();
    }
    toBytes(E = !0) {
      return Kn(E, "isCompressed"), this.assertValidity(), y(L, this, E);
    }
    toHex(E = !0) {
      return G(this.toBytes(E));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const T = i.BITS, D = new Yh(L, t.endo ? Math.ceil(T / 2) : T);
  return T >= 8 && L.BASE.precompute(8), Object.freeze(L.prototype), Object.freeze(L), L;
}
function tp(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function np(e, t) {
  return {
    secretKey: t.BYTES,
    publicKey: 1 + e.BYTES,
    publicKeyUncompressed: 1 + 2 * e.BYTES,
    publicKeyHasPrefix: !0,
    // Raw compact `(r || s)` signature width; DER and recovered signatures use
    // different lengths outside this helper.
    signature: 2 * t.BYTES
  };
}
function gv(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes === void 0 ? cl : t.randomBytes, i = Object.assign(np(e.Fp, n), {
    seed: Math.max(qh(n.ORDER), 16)
  });
  function o(h) {
    try {
      const y = n.fromBytes(h);
      return n.isValidNot0(y);
    } catch {
      return !1;
    }
  }
  function s(h, y) {
    const { publicKey: w, publicKeyUncompressed: p } = i;
    try {
      const g = h.length;
      return y === !0 && g !== w || y === !1 && g !== p ? !1 : !!e.fromBytes(h);
    } catch {
      return !1;
    }
  }
  function a(h) {
    return h = h === void 0 ? r(i.seed) : h, Wh(oe(h, i.seed, "seed"), n.ORDER);
  }
  function c(h, y = !0) {
    return e.BASE.multiply(n.fromBytes(h)).toBytes(y);
  }
  function l(h) {
    const { secretKey: y, publicKey: w, publicKeyUncompressed: p } = i, g = n._lengths;
    if (!al(h))
      return;
    const k = oe(h, void 0, "key").length, b = k === w || k === p, O = k === y || !!g?.includes(k);
    if (!(b && O))
      return b;
  }
  function d(h, y, w = !0) {
    if (l(h) === !0)
      throw new Error("first arg must be private key");
    if (l(y) === !1)
      throw new Error("second arg must be public key");
    const p = n.fromBytes(h);
    return e.fromBytes(y).multiply(p).toBytes(w);
  }
  const u = {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a
  }, f = pl(a, c);
  return Object.freeze(u), Object.freeze(i), Object.freeze({ getPublicKey: c, getSharedSecret: d, keygen: f, Point: e, utils: u, lengths: i });
}
function mv(e, t, n = {}) {
  const r = t;
  Bh(r), ii(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  }), n = Object.assign({}, n);
  const i = n.randomBytes === void 0 ? cl : n.randomBytes, o = n.hmac === void 0 ? (m, A) => vc(r, m, A) : n.hmac, { Fp: s, Fn: a } = e, { ORDER: c, BITS: l } = a, { keygen: d, getPublicKey: u, getSharedSecret: f, utils: h, lengths: y } = gv(e, n), w = {
    prehash: !0,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !0,
    format: "compact",
    extraEntropy: !1
  }, p = c * ep + Nn < s.ORDER;
  function g(m) {
    const A = c >> Nn;
    return m > A;
  }
  function k(m, A) {
    if (!a.isValidNot0(A))
      throw new Error(`invalid signature ${m}: out of range 1..Point.Fn.ORDER`);
    return A;
  }
  function b() {
    if (p)
      throw new Error('"recovered" sig type is not supported for cofactor >2 curves');
  }
  function O(m, A) {
    xc(A);
    const $ = y.signature, S = A === "compact" ? $ : A === "recovered" ? $ + 1 : void 0;
    return oe(m, S);
  }
  class B {
    r;
    s;
    recovery;
    constructor(A, $, S) {
      if (this.r = k("r", A), this.s = k("s", $), S != null) {
        if (b(), ![0, 1, 2, 3].includes(S))
          throw new Error("invalid recovery id");
        this.recovery = S;
      }
      Object.freeze(this);
    }
    static fromBytes(A, $ = w.format) {
      O(A, $);
      let S;
      if ($ === "der") {
        const { r: N, s: K } = wt.toSig(oe(A));
        return new B(N, K);
      }
      $ === "recovered" && (S = A[0], $ = "compact", A = A.subarray(1));
      const I = y.signature / 2, P = A.subarray(0, I), C = A.subarray(I, I * 2);
      return new B(a.fromBytes(P), a.fromBytes(C), S);
    }
    static fromHex(A, $) {
      return this.fromBytes(qe(A), $);
    }
    assertRecovery() {
      const { recovery: A } = this;
      if (A == null)
        throw new Error("invalid recovery id: must be present");
      return A;
    }
    addRecoveryBit(A) {
      return new B(this.r, this.s, A);
    }
    // Unlike the top-level helper below, this method expects a digest that has
    // already been hashed to the curve's message representative.
    recoverPublicKey(A) {
      const { r: $, s: S } = this, I = this.assertRecovery(), P = I === 2 || I === 3 ? $ + c : $;
      if (!s.isValid(P))
        throw new Error("invalid recovery id: sig.r+curve.n != R.x");
      const C = s.toBytes(P), N = e.fromBytes(dt(tp((I & 1) === 0), C)), K = a.inv(P), U = H(oe(A, void 0, "msgHash")), M = a.create(-U * K), F = a.create(S * K), V = e.BASE.multiplyUnsafe(M).add(N.multiplyUnsafe(F));
      if (V.is0())
        throw new Error("invalid recovery: point at infinify");
      return V.assertValidity(), V;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return g(this.s);
    }
    toBytes(A = w.format) {
      if (xc(A), A === "der")
        return qe(wt.hexFromSig(this));
      const { r: $, s: S } = this, I = a.toBytes($), P = a.toBytes(S);
      return A === "recovered" ? (b(), dt(Uint8Array.of(this.assertRecovery()), I, P)) : dt(I, P);
    }
    toHex(A) {
      return G(this.toBytes(A));
    }
  }
  Object.freeze(B.prototype), Object.freeze(B);
  const R = n.bits2int === void 0 ? function(A) {
    if (A.length > 8192)
      throw new Error("input is too large");
    const $ = Xi(A), S = A.length * 8 - l;
    return S > 0 ? $ >> BigInt(S) : $;
  } : n.bits2int, H = n.bits2int_modN === void 0 ? function(A) {
    return a.create(R(A));
  } : n.bits2int_modN, L = fl(l);
  function T(m) {
    return is("num < 2^" + l, m, Ct, L), a.toBytes(m);
  }
  function D(m, A) {
    return oe(m, void 0, "message"), A ? oe(r(m), void 0, "prehashed message") : m;
  }
  function z(m, A, $) {
    const { lowS: S, prehash: I, extraEntropy: P } = La($, w);
    m = D(m, I);
    const C = H(m), N = a.fromBytes(A);
    if (!a.isValidNot0(N))
      throw new Error("invalid private key");
    const K = [T(N), T(C)];
    if (P != null && P !== !1) {
      const V = P === !0 ? i(y.secretKey) : P;
      K.push(oe(V, void 0, "extraEntropy"));
    }
    const U = dt(...K), M = C;
    function F(V) {
      const q = R(V);
      if (!a.isValidNot0(q))
        return;
      const W = a.inv(q), Y = e.BASE.multiply(q).toAffine(), pe = a.create(Y.x);
      if (pe === Ct)
        return;
      const se = a.create(W * a.create(M + pe * N));
      if (se === Ct)
        return;
      let be = (Y.x === pe ? 0 : 2) | Number(Y.y & Nn), Ge = se;
      return S && g(se) && (Ge = a.neg(se), be ^= 1), new B(pe, Ge, p ? void 0 : be);
    }
    return { seed: U, k2sig: F };
  }
  function E(m, A, $ = {}) {
    const { seed: S, k2sig: I } = z(m, A, $);
    return M0(r.outputLen, a.BYTES, o)(S, I).toBytes($.format);
  }
  function x(m, A, $, S = {}) {
    const { lowS: I, prehash: P, format: C } = La(S, w);
    if ($ = oe($, void 0, "publicKey"), A = D(A, P), !al(m)) {
      const N = m instanceof B ? ", use sig.toBytes()" : "";
      throw new Error("verify expects Uint8Array signature" + N);
    }
    O(m, C);
    try {
      const N = B.fromBytes(m, C), K = e.fromBytes($);
      if (I && N.hasHighS())
        return !1;
      const { r: U, s: M } = N, F = H(A), V = a.inv(M), q = a.create(F * V), W = a.create(U * V), Y = e.BASE.multiplyUnsafe(q).add(K.multiplyUnsafe(W));
      return Y.is0() ? !1 : a.create(Y.x) === U;
    } catch {
      return !1;
    }
  }
  function v(m, A, $ = {}) {
    const { prehash: S } = La($, w);
    return A = D(A, S), B.fromBytes(m, "recovered").recoverPublicKey(A).toBytes();
  }
  return Object.freeze({
    keygen: d,
    getPublicKey: u,
    getSharedSecret: f,
    utils: h,
    lengths: y,
    Point: e,
    sign: E,
    verify: x,
    recoverPublicKey: v,
    Signature: B,
    hash: r
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Xs = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, bv = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, vv = /* @__PURE__ */ BigInt(0), kc = /* @__PURE__ */ BigInt(2);
function xv(e) {
  const t = Xs.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, d = l * l * e % t, u = we(d, n, t) * d % t, f = we(u, n, t) * d % t, h = we(f, kc, t) * l % t, y = we(h, i, t) * h % t, w = we(y, o, t) * y % t, p = we(w, a, t) * w % t, g = we(p, c, t) * p % t, k = we(g, a, t) * w % t, b = we(k, n, t) * d % t, O = we(b, s, t) * y % t, B = we(O, r, t) * l % t, R = we(B, kc, t);
  if (!ss.eql(ss.sqr(R), e))
    throw new Error("Cannot find square root");
  return R;
}
const ss = Ys(Xs.p, { sqrt: xv }), gr = /* @__PURE__ */ wv(Xs, {
  Fp: ss,
  endo: bv
}), fe = /* @__PURE__ */ mv(gr, ue), lu = {};
function as(e, ...t) {
  let n = lu[e];
  if (n === void 0) {
    const r = ue(D0(e));
    n = dt(r, r), lu[e] = n;
  }
  return ue(dt(n, ...t));
}
const Js = (e) => e.toBytes(!0).slice(1), Qs = (e) => e % kc === vv;
function rp(e) {
  const { Fn: t, BASE: n } = gr, r = t.fromBytes(e), i = n.multiply(r);
  return { scalar: Qs(i.y) ? r : t.neg(r), bytes: Js(i) };
}
function ip(e) {
  const t = ss;
  if (!t.isValidNot0(e))
    throw new Error("invalid x: Fail if x ≥ p");
  const n = t.create(e * e), r = t.create(n * e + BigInt(7));
  let i = t.sqrt(r);
  Qs(i) || (i = t.neg(i));
  const o = gr.fromAffine({ x: e, y: i });
  return o.assertValidity(), o;
}
const Mr = Xi;
function op(...e) {
  return gr.Fn.create(Mr(as("BIP0340/challenge", ...e)));
}
function du(e) {
  return rp(e).bytes;
}
function kv(e, t, n = ol(32)) {
  const { Fn: r, BASE: i } = gr, o = oe(e, void 0, "message"), { bytes: s, scalar: a } = rp(t), c = oe(n, 32, "auxRand"), l = r.toBytes(a ^ Mr(as("BIP0340/aux", c))), d = as("BIP0340/nonce", l, s, o), u = r.create(Mr(d));
  if (u === 0n)
    throw new Error("sign failed: k is zero");
  const f = i.multiply(u), h = Qs(f.y) ? u : r.neg(u), y = Js(f), w = op(y, s, o), p = new Uint8Array(64);
  if (p.set(y, 0), p.set(r.toBytes(r.create(h + w * a)), 32), !sp(p, o, s))
    throw new Error("sign: Invalid signature produced");
  return p;
}
function sp(e, t, n) {
  const { Fp: r, Fn: i, BASE: o } = gr, s = oe(e, 64, "signature"), a = oe(t, void 0, "message"), c = oe(n, 32, "publicKey");
  try {
    const l = ip(Mr(c)), d = Mr(s.subarray(0, 32));
    if (!r.isValidNot0(d))
      return !1;
    const u = Mr(s.subarray(32, 64));
    if (!i.isValidNot0(u))
      return !1;
    const f = op(i.toBytes(d), Js(l), a), h = o.multiplyUnsafe(u).add(l.multiplyUnsafe(i.neg(f))), { x: y, y: w } = h.toAffine();
    return !(h.is0() || !Qs(w) || y !== d);
  } catch {
    return !1;
  }
}
const On = /* @__PURE__ */ (() => {
  const n = (r) => (r = r === void 0 ? ol(48) : r, Wh(r, Xs.n));
  return Object.freeze({
    keygen: pl(n, du),
    getPublicKey: du,
    sign: kv,
    verify: sp,
    Point: gr,
    utils: Object.freeze({
      randomSecretKey: n,
      taggedHash: as,
      lift_x: ip,
      pointToBytes: Js
    }),
    lengths: Object.freeze({
      secretKey: 32,
      publicKey: 32,
      publicKeyHasPrefix: !1,
      signature: 64,
      seed: 48
    })
  });
})(), Ev = /* @__PURE__ */ Uint8Array.from([
  7,
  4,
  13,
  1,
  10,
  6,
  15,
  3,
  12,
  0,
  9,
  5,
  2,
  14,
  11,
  8
]), ap = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), Sv = ap.map((e) => (9 * e + 5) % 16), cp = /* @__PURE__ */ (() => {
  const n = [[ap], [Sv]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => Ev[o]));
  return n;
})(), lp = cp[0], dp = cp[1], up = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), Av = /* @__PURE__ */ lp.map((e, t) => e.map((n) => up[t][n])), _v = /* @__PURE__ */ dp.map((e, t) => e.map((n) => up[t][n])), Iv = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), Bv = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function uu(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const yo = /* @__PURE__ */ new Uint32Array(16);
class Tv extends sl {
  h0 = 1732584193;
  h1 = -271733879;
  h2 = -1732584194;
  h3 = 271733878;
  h4 = -1009589776;
  constructor() {
    super(64, 20, 8, !0);
  }
  get() {
    const { h0: t, h1: n, h2: r, h3: i, h4: o } = this;
    return [t, n, r, i, o];
  }
  set(t, n, r, i, o) {
    this.h0 = t | 0, this.h1 = n | 0, this.h2 = r | 0, this.h3 = i | 0, this.h4 = o | 0;
  }
  process(t, n) {
    for (let h = 0; h < 16; h++, n += 4)
      yo[h] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, d = l, u = this.h4 | 0, f = u;
    for (let h = 0; h < 5; h++) {
      const y = 4 - h, w = Iv[h], p = Bv[h], g = lp[h], k = dp[h], b = Av[h], O = _v[h];
      for (let B = 0; B < 16; B++) {
        const R = co(r + uu(h, o, a, l) + yo[g[B]] + w, b[B]) + u | 0;
        r = u, u = l, l = co(a, 10) | 0, a = o, o = R;
      }
      for (let B = 0; B < 16; B++) {
        const R = co(i + uu(y, s, c, d) + yo[k[B]] + p, O[B]) + f | 0;
        i = f, f = d, d = co(c, 10) | 0, c = s, s = R;
      }
    }
    this.set(this.h1 + a + d | 0, this.h2 + l + f | 0, this.h3 + u + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    Mt(yo);
  }
  destroy() {
    this.destroyed = !0, Mt(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const yl = /* @__PURE__ */ Zs(() => new Tv()), Pv = BigInt(0), yi = BigInt(1), $v = BigInt(2), Ov = BigInt(7), Cv = BigInt(256), Rv = BigInt(113), fp = [], hp = [], pp = [];
for (let e = 0, t = yi, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], fp.push(2 * (5 * r + n)), hp.push((e + 1) * (e + 2) / 2 % 64);
  let i = Pv;
  for (let o = 0; o < 7; o++)
    t = (t << yi ^ (t >> Ov) * Rv) % Cv, t & $v && (i ^= yi << (yi << BigInt(o)) - yi);
  pp.push(i);
}
const yp = $h(pp, !0), Lv = yp[0], Hv = yp[1], fu = (e, t, n) => n > 32 ? E0(e, t, n) : x0(e, t, n), hu = (e, t, n) => n > 32 ? S0(e, t, n) : k0(e, t, n);
function Nv(e, t = 24) {
  if (Dt(t, "rounds"), t < 1 || t > 24)
    throw new Error('"rounds" expected integer 1..24');
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], d = n[c + 1], u = fu(l, d, 1) ^ n[a], f = hu(l, d, 1) ^ n[a + 1];
      for (let h = 0; h < 50; h += 10)
        e[s + h] ^= u, e[s + h + 1] ^= f;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = hp[s], c = fu(i, o, a), l = hu(i, o, a), d = fp[s];
      i = e[d], o = e[d + 1], e[d] = c, e[d + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      const a = e[s], c = e[s + 1], l = e[s + 2], d = e[s + 3];
      e[s] ^= ~e[s + 2] & e[s + 4], e[s + 1] ^= ~e[s + 3] & e[s + 5], e[s + 2] ^= ~e[s + 4] & e[s + 6], e[s + 3] ^= ~e[s + 5] & e[s + 7], e[s + 4] ^= ~e[s + 6] & e[s + 8], e[s + 5] ^= ~e[s + 7] & e[s + 9], e[s + 6] ^= ~e[s + 8] & a, e[s + 7] ^= ~e[s + 9] & c, e[s + 8] ^= ~a & l, e[s + 9] ^= ~c & d;
    }
    e[0] ^= Lv[r], e[1] ^= Hv[r];
  }
  Mt(n);
}
let Uv = class wp {
  state;
  pos = 0;
  posOut = 0;
  finished = !1;
  state32;
  destroyed = !1;
  blockLen;
  suffix;
  outputLen;
  canXOF;
  enableXOF = !1;
  rounds;
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.canXOF = i, this.rounds = o, Dt(r, "outputLen"), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = f0(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    qd(this.state32), Nv(this.state32, this.rounds), qd(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Wr(this), zt(t);
    const { blockLen: n, state: r } = this, i = t.length;
    for (let o = 0; o < i; ) {
      const s = Math.min(n - this.pos, i - o);
      for (let a = 0; a < s; a++)
        r[this.pos++] ^= t[o++];
      this.pos === n && this.keccak();
    }
    return this;
  }
  finish() {
    if (this.finished)
      return;
    this.finished = !0;
    const { state: t, suffix: n, pos: r, blockLen: i } = this;
    t[r] ^= n, (n & 128) !== 0 && r === i - 1 && this.keccak(), t[i - 1] ^= 128, this.keccak();
  }
  writeInto(t) {
    Wr(this, !1), zt(t), this.finish();
    const n = this.state, { blockLen: r } = this;
    for (let i = 0, o = t.length; i < o; ) {
      this.posOut >= r && this.keccak();
      const s = Math.min(r - this.posOut, o - i);
      t.set(n.subarray(this.posOut, this.posOut + s), i), this.posOut += s, i += s;
    }
    return t;
  }
  xofInto(t) {
    if (!this.enableXOF)
      throw new Error("XOF is not possible for this instance");
    return this.writeInto(t);
  }
  xof(t) {
    return Dt(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (nl(t, this), this.finished)
      throw new Error("digest() was already called");
    this.writeInto(t.subarray(0, this.outputLen)), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.outputLen);
    return this.digestInto(t), t;
  }
  destroy() {
    this.destroyed = !0, Mt(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t ||= new wp(n, r, i, s, o), t.blockLen = n, t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.canXOF = this.canXOF, t.destroyed = this.destroyed, t;
  }
};
const Dv = (e, t, n, r = {}) => Zs(() => new Uv(t, e, n), r), cs = /* @__PURE__ */ Dv(1, 136, 32), zv = 1024 * 1024, gp = Object.getPrototypeOf(Uint8Array.prototype), Mv = Object.getOwnPropertyDescriptor(gp, Symbol.toStringTag).get, Kv = Object.getOwnPropertyDescriptor(gp, "byteLength").get, Fv = Uint8Array.prototype.set;
function mp(e) {
  if (!Number.isSafeInteger(e) || e < 0) throw new Error("exact message byte length is invalid");
  if (e > zv) throw new Error("exact message exceeds the 1 MiB decoded limit");
}
function bp(e) {
  const t = e.startsWith("0x") ? 2 : 0;
  mp(Math.ceil((e.length - t) / 2));
}
function wl(e) {
  if (typeof e == "string")
    return bp(e), qe(e.startsWith("0x") ? e.slice(2) : e);
  if (Mv.call(e) !== "Uint8Array") throw new TypeError("exact message must be hex or Uint8Array");
  const t = Kv.call(e);
  mp(t);
  const n = new Uint8Array(t);
  return Fv.call(n, e), n;
}
const Ir = typeof globalThis == "object" && "crypto" in globalThis ? globalThis.crypto : void 0;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function ea(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Ec(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function et(e, ...t) {
  if (!ea(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function vp(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash should be wrapped by utils.createHasher");
  Ec(e.outputLen), Ec(e.blockLen);
}
function ls(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function jv(e, t) {
  et(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function Fn(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Kr(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Bt(e, t) {
  return e << 32 - t | e >>> t;
}
function wo(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const xp = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", Vv = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function dr(e) {
  if (et(e), xp)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += Vv[e[n]];
  return t;
}
const Gt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function pu(e) {
  if (e >= Gt._0 && e <= Gt._9)
    return e - Gt._0;
  if (e >= Gt.A && e <= Gt.F)
    return e - (Gt.A - 10);
  if (e >= Gt.a && e <= Gt.f)
    return e - (Gt.a - 10);
}
function Hi(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (xp)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = pu(e.charCodeAt(o)), a = pu(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function kp(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function gl(e) {
  return typeof e == "string" && (e = kp(e)), et(e), e;
}
function lt(...e) {
  let t = 0;
  for (let r = 0; r < e.length; r++) {
    const i = e[r];
    et(i), t += i.length;
  }
  const n = new Uint8Array(t);
  for (let r = 0, i = 0; r < e.length; r++) {
    const o = e[r];
    n.set(o, i), i += o.length;
  }
  return n;
}
let Ep = class {
};
function ml(e) {
  const t = (r) => e().update(gl(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
function Sp(e = 32) {
  if (Ir && typeof Ir.getRandomValues == "function")
    return Ir.getRandomValues(new Uint8Array(e));
  if (Ir && typeof Ir.randomBytes == "function")
    return Uint8Array.from(Ir.randomBytes(e));
  throw new Error("crypto.getRandomValues must be defined");
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const bl = /* @__PURE__ */ BigInt(0), Sc = /* @__PURE__ */ BigInt(1);
function ds(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}"`;
    throw new Error(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function tr(e, t, n = "") {
  const r = ea(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function go(e) {
  const t = e.toString(16);
  return t.length & 1 ? "0" + t : t;
}
function Ap(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  return e === "" ? bl : BigInt("0x" + e);
}
function ta(e) {
  return Ap(dr(e));
}
function _p(e) {
  return et(e), Ap(dr(Uint8Array.from(e).reverse()));
}
function vl(e, t) {
  return Hi(e.toString(16).padStart(t * 2, "0"));
}
function Ip(e, t) {
  return vl(e, t).reverse();
}
function Ze(e, t, n) {
  let r;
  if (typeof t == "string")
    try {
      r = Hi(t);
    } catch (i) {
      throw new Error(e + " must be hex string or Uint8Array, cause: " + i);
    }
  else if (ea(t))
    r = Uint8Array.from(t);
  else
    throw new Error(e + " must be hex string or Uint8Array");
  return r.length, r;
}
const Ha = (e) => typeof e == "bigint" && bl <= e;
function qv(e, t, n) {
  return Ha(e) && Ha(t) && Ha(n) && t <= e && e < n;
}
function Wv(e, t, n, r) {
  if (!qv(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Bp(e) {
  let t;
  for (t = 0; e > bl; e >>= Sc, t += 1)
    ;
  return t;
}
const Ji = (e) => (Sc << BigInt(e)) - Sc;
function Gv(e, t, n) {
  if (typeof e != "number" || e < 2)
    throw new Error("hashLen must be a number");
  if (typeof t != "number" || t < 2)
    throw new Error("qByteLen must be a number");
  if (typeof n != "function")
    throw new Error("hmacFn must be a function");
  const r = (h) => new Uint8Array(h), i = (h) => Uint8Array.of(h);
  let o = r(e), s = r(e), a = 0;
  const c = () => {
    o.fill(1), s.fill(0), a = 0;
  }, l = (...h) => n(s, o, ...h), d = (h = r(0)) => {
    s = l(i(0), h), o = l(), h.length !== 0 && (s = l(i(1), h), o = l());
  }, u = () => {
    if (a++ >= 1e3)
      throw new Error("drbg: tried 1000 values");
    let h = 0;
    const y = [];
    for (; h < t; ) {
      o = l();
      const w = o.slice();
      y.push(w), h += o.length;
    }
    return lt(...y);
  };
  return (h, y) => {
    c(), d(h);
    let w;
    for (; !(w = y(u())); )
      d();
    return c(), w;
  };
}
function xl(e, t, n = {}) {
  if (!e || typeof e != "object")
    throw new Error("expected valid options object");
  function r(i, o, s) {
    const a = e[i];
    if (s && a === void 0)
      return;
    const c = typeof a;
    if (c !== o || a === null)
      throw new Error(`param "${i}" is invalid: expected ${o}, got ${c}`);
  }
  Object.entries(t).forEach(([i, o]) => r(i, o, !1)), Object.entries(n).forEach(([i, o]) => r(i, o, !0));
}
function yu(e) {
  const t = /* @__PURE__ */ new WeakMap();
  return (n, ...r) => {
    const i = t.get(n);
    if (i !== void 0)
      return i;
    const o = e(n, ...r);
    return t.set(n, o), o;
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Ye = BigInt(0), We = BigInt(1), nr = /* @__PURE__ */ BigInt(2), Tp = /* @__PURE__ */ BigInt(3), Pp = /* @__PURE__ */ BigInt(4), $p = /* @__PURE__ */ BigInt(5), Zv = /* @__PURE__ */ BigInt(7), Op = /* @__PURE__ */ BigInt(8), Yv = /* @__PURE__ */ BigInt(9), Cp = /* @__PURE__ */ BigInt(16);
function ct(e, t) {
  const n = e % t;
  return n >= Ye ? n : t + n;
}
function ot(e, t, n) {
  let r = e;
  for (; t-- > Ye; )
    r *= r, r %= n;
  return r;
}
function wu(e, t) {
  if (e === Ye)
    throw new Error("invert: expected non-zero number");
  if (t <= Ye)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = ct(e, t), r = t, i = Ye, o = We;
  for (; n !== Ye; ) {
    const a = r / n, c = r % n, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== We)
    throw new Error("invert: does not exist");
  return ct(i, t);
}
function kl(e, t, n) {
  if (!e.eql(e.sqr(t), n))
    throw new Error("Cannot find square root");
}
function Rp(e, t) {
  const n = (e.ORDER + We) / Pp, r = e.pow(t, n);
  return kl(e, r, t), r;
}
function Xv(e, t) {
  const n = (e.ORDER - $p) / Op, r = e.mul(t, nr), i = e.pow(r, n), o = e.mul(t, i), s = e.mul(e.mul(o, nr), i), a = e.mul(o, e.sub(s, e.ONE));
  return kl(e, a, t), a;
}
function Jv(e) {
  const t = Qi(e), n = Lp(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + Zv) / Cp;
  return (a, c) => {
    let l = a.pow(c, s), d = a.mul(l, r);
    const u = a.mul(l, i), f = a.mul(l, o), h = a.eql(a.sqr(d), c), y = a.eql(a.sqr(u), c);
    l = a.cmov(l, d, h), d = a.cmov(f, u, y);
    const w = a.eql(a.sqr(d), c), p = a.cmov(l, d, w);
    return kl(a, p, c), p;
  };
}
function Lp(e) {
  if (e < Tp)
    throw new Error("sqrt is not defined for small field");
  let t = e - We, n = 0;
  for (; t % nr === Ye; )
    t /= nr, n++;
  let r = nr;
  const i = Qi(e);
  for (; gu(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return Rp;
  let o = i.pow(r, t);
  const s = (t + We) / nr;
  return function(c, l) {
    if (c.is0(l))
      return l;
    if (gu(c, l) !== 1)
      throw new Error("Cannot find square root");
    let d = n, u = c.mul(c.ONE, o), f = c.pow(l, t), h = c.pow(l, s);
    for (; !c.eql(f, c.ONE); ) {
      if (c.is0(f))
        return c.ZERO;
      let y = 1, w = c.sqr(f);
      for (; !c.eql(w, c.ONE); )
        if (y++, w = c.sqr(w), y === d)
          throw new Error("Cannot find square root");
      const p = We << BigInt(d - y - 1), g = c.pow(u, p);
      d = y, u = c.sqr(g), f = c.mul(f, u), h = c.mul(h, g);
    }
    return h;
  };
}
function Qv(e) {
  return e % Pp === Tp ? Rp : e % Op === $p ? Xv : e % Cp === Yv ? Jv(e) : Lp(e);
}
const e1 = [
  "create",
  "isValid",
  "is0",
  "neg",
  "inv",
  "sqrt",
  "sqr",
  "eql",
  "add",
  "sub",
  "mul",
  "pow",
  "div",
  "addN",
  "subN",
  "mulN",
  "sqrN"
];
function t1(e) {
  const t = {
    ORDER: "bigint",
    MASK: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = e1.reduce((r, i) => (r[i] = "function", r), t);
  return xl(e, n), e;
}
function n1(e, t, n) {
  if (n < Ye)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === Ye)
    return e.ONE;
  if (n === We)
    return t;
  let r = e.ONE, i = t;
  for (; n > Ye; )
    n & We && (r = e.mul(r, i)), i = e.sqr(i), n >>= We;
  return r;
}
function Hp(e, t, n = !1) {
  const r = new Array(t.length).fill(n ? e.ZERO : void 0), i = t.reduce((s, a, c) => e.is0(a) ? s : (r[c] = s, e.mul(s, a)), e.ONE), o = e.inv(i);
  return t.reduceRight((s, a, c) => e.is0(a) ? s : (r[c] = e.mul(s, r[c]), e.mul(s, a)), o), r;
}
function gu(e, t) {
  const n = (e.ORDER - We) / nr, r = e.pow(t, n), i = e.eql(r, e.ONE), o = e.eql(r, e.ZERO), s = e.eql(r, e.neg(e.ONE));
  if (!i && !o && !s)
    throw new Error("invalid Legendre symbol result");
  return i ? 1 : o ? 0 : -1;
}
function Np(e, t) {
  t !== void 0 && Ec(t);
  const n = t !== void 0 ? t : e.toString(2).length, r = Math.ceil(n / 8);
  return { nBitLength: n, nByteLength: r };
}
function Qi(e, t, n = !1, r = {}) {
  if (e <= Ye)
    throw new Error("invalid field: expected ORDER > 0, got " + e);
  let i, o, s = !1, a;
  if (typeof t == "object" && t != null) {
    if (r.sqrt || n)
      throw new Error("cannot specify opts in two arguments");
    const f = t;
    f.BITS && (i = f.BITS), f.sqrt && (o = f.sqrt), typeof f.isLE == "boolean" && (n = f.isLE), typeof f.modFromBytes == "boolean" && (s = f.modFromBytes), a = f.allowedLengths;
  } else
    typeof t == "number" && (i = t), r.sqrt && (o = r.sqrt);
  const { nBitLength: c, nByteLength: l } = Np(e, i);
  if (l > 2048)
    throw new Error("invalid field: expected ORDER of <= 2048 bytes");
  let d;
  const u = Object.freeze({
    ORDER: e,
    isLE: n,
    BITS: c,
    BYTES: l,
    MASK: Ji(c),
    ZERO: Ye,
    ONE: We,
    allowedLengths: a,
    create: (f) => ct(f, e),
    isValid: (f) => {
      if (typeof f != "bigint")
        throw new Error("invalid field element: expected bigint, got " + typeof f);
      return Ye <= f && f < e;
    },
    is0: (f) => f === Ye,
    // is valid and invertible
    isValidNot0: (f) => !u.is0(f) && u.isValid(f),
    isOdd: (f) => (f & We) === We,
    neg: (f) => ct(-f, e),
    eql: (f, h) => f === h,
    sqr: (f) => ct(f * f, e),
    add: (f, h) => ct(f + h, e),
    sub: (f, h) => ct(f - h, e),
    mul: (f, h) => ct(f * h, e),
    pow: (f, h) => n1(u, f, h),
    div: (f, h) => ct(f * wu(h, e), e),
    // Same as above, but doesn't normalize
    sqrN: (f) => f * f,
    addN: (f, h) => f + h,
    subN: (f, h) => f - h,
    mulN: (f, h) => f * h,
    inv: (f) => wu(f, e),
    sqrt: o || ((f) => (d || (d = Qv(e)), d(u, f))),
    toBytes: (f) => n ? Ip(f, l) : vl(f, l),
    fromBytes: (f, h = !0) => {
      if (a) {
        if (!a.includes(f.length) || f.length > l)
          throw new Error("Field.fromBytes: expected " + a + " bytes, got " + f.length);
        const w = new Uint8Array(l);
        w.set(f, n ? 0 : w.length - f.length), f = w;
      }
      if (f.length !== l)
        throw new Error("Field.fromBytes: expected " + l + " bytes, got " + f.length);
      let y = n ? _p(f) : ta(f);
      if (s && (y = ct(y, e)), !h && !u.isValid(y))
        throw new Error("invalid field element: outside of range 0..ORDER");
      return y;
    },
    // TODO: we don't need it here, move out to separate fn
    invertBatch: (f) => Hp(u, f),
    // We can't move this out because Fp6, Fp12 implement it
    // and it's unclear what to return in there.
    cmov: (f, h, y) => y ? h : f
  });
  return Object.freeze(u);
}
function Up(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  const t = e.toString(2).length;
  return Math.ceil(t / 8);
}
function Dp(e) {
  const t = Up(e);
  return t + Math.ceil(t / 2);
}
function r1(e, t, n = !1) {
  const r = e.length, i = Up(t), o = Dp(t);
  if (r < 16 || r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? _p(e) : ta(e), a = ct(s, t - We) + We;
  return n ? Ip(a, i) : vl(a, i);
}
function i1(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function o1(e, t, n) {
  return e & t ^ ~e & n;
}
function s1(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let El = class extends Ep {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Kr(this.buffer);
  }
  update(t) {
    ls(this), t = gl(t), et(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Kr(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    ls(this), jv(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Fn(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let u = s; u < i; u++)
      n[u] = 0;
    i1(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Kr(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const l = c / 4, d = this.get();
    if (l > d.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let u = 0; u < l; u++)
      a.setUint32(4 * u, d[u], o);
  }
  digest() {
    const { buffer: t, outputLen: n } = this;
    this.digestInto(t);
    const r = t.slice(0, n);
    return this.destroy(), r;
  }
  _cloneInto(t) {
    t || (t = new this.constructor()), t.set(...this.get());
    const { blockLen: n, buffer: r, length: i, finished: o, destroyed: s, pos: a } = this;
    return t.destroyed = s, t.finished = o, t.length = i, t.pos = a, i % n && t.buffer.set(r), t;
  }
  clone() {
    return this._cloneInto();
  }
};
const kn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Le = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  4089235720,
  3144134277,
  2227873595,
  1013904242,
  4271175723,
  2773480762,
  1595750129,
  1359893119,
  2917565137,
  2600822924,
  725511199,
  528734635,
  4215389547,
  1541459225,
  327033209
]), mo = /* @__PURE__ */ BigInt(2 ** 32 - 1), mu = /* @__PURE__ */ BigInt(32);
function a1(e, t = !1) {
  return t ? { h: Number(e & mo), l: Number(e >> mu & mo) } : { h: Number(e >> mu & mo) | 0, l: Number(e & mo) | 0 };
}
function c1(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = a1(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const bu = (e, t, n) => e >>> n, vu = (e, t, n) => e << 32 - n | t >>> n, Br = (e, t, n) => e >>> n | t << 32 - n, Tr = (e, t, n) => e << 32 - n | t >>> n, bo = (e, t, n) => e << 64 - n | t >>> n - 32, vo = (e, t, n) => e >>> n - 32 | t << 64 - n;
function Zt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const l1 = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), d1 = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, u1 = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), f1 = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, h1 = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), p1 = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, y1 = /* @__PURE__ */ Uint32Array.from([
  1116352408,
  1899447441,
  3049323471,
  3921009573,
  961987163,
  1508970993,
  2453635748,
  2870763221,
  3624381080,
  310598401,
  607225278,
  1426881987,
  1925078388,
  2162078206,
  2614888103,
  3248222580,
  3835390401,
  4022224774,
  264347078,
  604807628,
  770255983,
  1249150122,
  1555081692,
  1996064986,
  2554220882,
  2821834349,
  2952996808,
  3210313671,
  3336571891,
  3584528711,
  113926993,
  338241895,
  666307205,
  773529912,
  1294757372,
  1396182291,
  1695183700,
  1986661051,
  2177026350,
  2456956037,
  2730485921,
  2820302411,
  3259730800,
  3345764771,
  3516065817,
  3600352804,
  4094571909,
  275423344,
  430227734,
  506948616,
  659060556,
  883997877,
  958139571,
  1322822218,
  1537002063,
  1747873779,
  1955562222,
  2024104815,
  2227730452,
  2361852424,
  2428436474,
  2756734187,
  3204031479,
  3329325298
]), En = /* @__PURE__ */ new Uint32Array(64);
let w1 = class extends El {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = kn[0] | 0, this.B = kn[1] | 0, this.C = kn[2] | 0, this.D = kn[3] | 0, this.E = kn[4] | 0, this.F = kn[5] | 0, this.G = kn[6] | 0, this.H = kn[7] | 0;
  }
  get() {
    const { A: t, B: n, C: r, D: i, E: o, F: s, G: a, H: c } = this;
    return [t, n, r, i, o, s, a, c];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c) {
    this.A = t | 0, this.B = n | 0, this.C = r | 0, this.D = i | 0, this.E = o | 0, this.F = s | 0, this.G = a | 0, this.H = c | 0;
  }
  process(t, n) {
    for (let u = 0; u < 16; u++, n += 4)
      En[u] = t.getUint32(n, !1);
    for (let u = 16; u < 64; u++) {
      const f = En[u - 15], h = En[u - 2], y = Bt(f, 7) ^ Bt(f, 18) ^ f >>> 3, w = Bt(h, 17) ^ Bt(h, 19) ^ h >>> 10;
      En[u] = w + En[u - 7] + y + En[u - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: d } = this;
    for (let u = 0; u < 64; u++) {
      const f = Bt(a, 6) ^ Bt(a, 11) ^ Bt(a, 25), h = d + f + o1(a, c, l) + y1[u] + En[u] | 0, w = (Bt(r, 2) ^ Bt(r, 13) ^ Bt(r, 22)) + s1(r, i, o) | 0;
      d = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, d = d + this.H | 0, this.set(r, i, o, s, a, c, l, d);
  }
  roundClean() {
    Fn(En);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Fn(this.buffer);
  }
};
const zp = c1([
  "0x428a2f98d728ae22",
  "0x7137449123ef65cd",
  "0xb5c0fbcfec4d3b2f",
  "0xe9b5dba58189dbbc",
  "0x3956c25bf348b538",
  "0x59f111f1b605d019",
  "0x923f82a4af194f9b",
  "0xab1c5ed5da6d8118",
  "0xd807aa98a3030242",
  "0x12835b0145706fbe",
  "0x243185be4ee4b28c",
  "0x550c7dc3d5ffb4e2",
  "0x72be5d74f27b896f",
  "0x80deb1fe3b1696b1",
  "0x9bdc06a725c71235",
  "0xc19bf174cf692694",
  "0xe49b69c19ef14ad2",
  "0xefbe4786384f25e3",
  "0x0fc19dc68b8cd5b5",
  "0x240ca1cc77ac9c65",
  "0x2de92c6f592b0275",
  "0x4a7484aa6ea6e483",
  "0x5cb0a9dcbd41fbd4",
  "0x76f988da831153b5",
  "0x983e5152ee66dfab",
  "0xa831c66d2db43210",
  "0xb00327c898fb213f",
  "0xbf597fc7beef0ee4",
  "0xc6e00bf33da88fc2",
  "0xd5a79147930aa725",
  "0x06ca6351e003826f",
  "0x142929670a0e6e70",
  "0x27b70a8546d22ffc",
  "0x2e1b21385c26c926",
  "0x4d2c6dfc5ac42aed",
  "0x53380d139d95b3df",
  "0x650a73548baf63de",
  "0x766a0abb3c77b2a8",
  "0x81c2c92e47edaee6",
  "0x92722c851482353b",
  "0xa2bfe8a14cf10364",
  "0xa81a664bbc423001",
  "0xc24b8b70d0f89791",
  "0xc76c51a30654be30",
  "0xd192e819d6ef5218",
  "0xd69906245565a910",
  "0xf40e35855771202a",
  "0x106aa07032bbd1b8",
  "0x19a4c116b8d2d0c8",
  "0x1e376c085141ab53",
  "0x2748774cdf8eeb99",
  "0x34b0bcb5e19b48a8",
  "0x391c0cb3c5c95a63",
  "0x4ed8aa4ae3418acb",
  "0x5b9cca4f7763e373",
  "0x682e6ff3d6b2b8a3",
  "0x748f82ee5defb2fc",
  "0x78a5636f43172f60",
  "0x84c87814a1f0ab72",
  "0x8cc702081a6439ec",
  "0x90befffa23631e28",
  "0xa4506cebde82bde9",
  "0xbef9a3f7b2c67915",
  "0xc67178f2e372532b",
  "0xca273eceea26619c",
  "0xd186b8c721c0c207",
  "0xeada7dd6cde0eb1e",
  "0xf57d4f7fee6ed178",
  "0x06f067aa72176fba",
  "0x0a637dc5a2c898a6",
  "0x113f9804bef90dae",
  "0x1b710b35131c471b",
  "0x28db77f523047d84",
  "0x32caab7b40c72493",
  "0x3c9ebe0a15c9bebc",
  "0x431d67c49c100d4c",
  "0x4cc5d4becb3e42b6",
  "0x597f299cfc657e2a",
  "0x5fcb6fab3ad6faec",
  "0x6c44198c4a475817"
].map((e) => BigInt(e))), g1 = zp[0], m1 = zp[1], Sn = /* @__PURE__ */ new Uint32Array(80), An = /* @__PURE__ */ new Uint32Array(80);
let b1 = class extends El {
  constructor(t = 64) {
    super(128, t, 16, !1), this.Ah = Le[0] | 0, this.Al = Le[1] | 0, this.Bh = Le[2] | 0, this.Bl = Le[3] | 0, this.Ch = Le[4] | 0, this.Cl = Le[5] | 0, this.Dh = Le[6] | 0, this.Dl = Le[7] | 0, this.Eh = Le[8] | 0, this.El = Le[9] | 0, this.Fh = Le[10] | 0, this.Fl = Le[11] | 0, this.Gh = Le[12] | 0, this.Gl = Le[13] | 0, this.Hh = Le[14] | 0, this.Hl = Le[15] | 0;
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: d, Fh: u, Fl: f, Gh: h, Gl: y, Hh: w, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, l, d, u, f, h, y, w, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, d, u, f, h, y, w, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = d | 0, this.Fh = u | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = y | 0, this.Hh = w | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let b = 0; b < 16; b++, n += 4)
      Sn[b] = t.getUint32(n), An[b] = t.getUint32(n += 4);
    for (let b = 16; b < 80; b++) {
      const O = Sn[b - 15] | 0, B = An[b - 15] | 0, R = Br(O, B, 1) ^ Br(O, B, 8) ^ bu(O, B, 7), H = Tr(O, B, 1) ^ Tr(O, B, 8) ^ vu(O, B, 7), L = Sn[b - 2] | 0, T = An[b - 2] | 0, D = Br(L, T, 19) ^ bo(L, T, 61) ^ bu(L, T, 6), z = Tr(L, T, 19) ^ vo(L, T, 61) ^ vu(L, T, 6), E = u1(H, z, An[b - 7], An[b - 16]), x = f1(E, R, D, Sn[b - 7], Sn[b - 16]);
      Sn[b] = x | 0, An[b] = E | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: d, Eh: u, El: f, Fh: h, Fl: y, Gh: w, Gl: p, Hh: g, Hl: k } = this;
    for (let b = 0; b < 80; b++) {
      const O = Br(u, f, 14) ^ Br(u, f, 18) ^ bo(u, f, 41), B = Tr(u, f, 14) ^ Tr(u, f, 18) ^ vo(u, f, 41), R = u & h ^ ~u & w, H = f & y ^ ~f & p, L = h1(k, B, H, m1[b], An[b]), T = p1(L, g, O, R, g1[b], Sn[b]), D = L | 0, z = Br(r, i, 28) ^ bo(r, i, 34) ^ bo(r, i, 39), E = Tr(r, i, 28) ^ vo(r, i, 34) ^ vo(r, i, 39), x = r & o ^ r & a ^ o & a, v = i & s ^ i & c ^ s & c;
      g = w | 0, k = p | 0, w = h | 0, p = y | 0, h = u | 0, y = f | 0, { h: u, l: f } = Zt(l | 0, d | 0, T | 0, D | 0), l = a | 0, d = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const m = l1(D, E, v);
      r = d1(m, T, z, x), i = m | 0;
    }
    ({ h: r, l: i } = Zt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Zt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Zt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: d } = Zt(this.Dh | 0, this.Dl | 0, l | 0, d | 0), { h: u, l: f } = Zt(this.Eh | 0, this.El | 0, u | 0, f | 0), { h, l: y } = Zt(this.Fh | 0, this.Fl | 0, h | 0, y | 0), { h: w, l: p } = Zt(this.Gh | 0, this.Gl | 0, w | 0, p | 0), { h: g, l: k } = Zt(this.Hh | 0, this.Hl | 0, g | 0, k | 0), this.set(r, i, o, s, a, c, l, d, u, f, h, y, w, p, g, k);
  }
  roundClean() {
    Fn(Sn, An);
  }
  destroy() {
    Fn(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
};
const Sl = /* @__PURE__ */ ml(() => new w1()), xu = /* @__PURE__ */ ml(() => new b1());
let Mp = class extends Ep {
  constructor(t, n) {
    super(), this.finished = !1, this.destroyed = !1, vp(t);
    const r = gl(n);
    if (this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const i = this.blockLen, o = new Uint8Array(i);
    o.set(r.length > i ? t.create().update(r).digest() : r);
    for (let s = 0; s < o.length; s++)
      o[s] ^= 54;
    this.iHash.update(o), this.oHash = t.create();
    for (let s = 0; s < o.length; s++)
      o[s] ^= 106;
    this.oHash.update(o), Fn(o);
  }
  update(t) {
    return ls(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    ls(this), et(t, this.outputLen), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.oHash.outputLen);
    return this.digestInto(t), t;
  }
  _cloneInto(t) {
    t || (t = Object.create(Object.getPrototypeOf(this), {}));
    const { oHash: n, iHash: r, finished: i, destroyed: o, blockLen: s, outputLen: a } = this;
    return t = t, t.finished = i, t.destroyed = o, t.blockLen = s, t.outputLen = a, t.oHash = n._cloneInto(t.oHash), t.iHash = r._cloneInto(t.iHash), t;
  }
  clone() {
    return this._cloneInto();
  }
  destroy() {
    this.destroyed = !0, this.oHash.destroy(), this.iHash.destroy();
  }
};
const us = (e, t, n) => new Mp(e, t).update(n).digest();
us.create = (e, t) => new Mp(e, t);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Yr = BigInt(0), rr = BigInt(1);
function fs(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function Na(e, t) {
  const n = Hp(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function Kp(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function Ua(e, t) {
  Kp(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = Ji(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function ku(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += rr);
  const l = t * r, d = l + Math.abs(a) - 1, u = a === 0, f = a < 0, h = t % 2 !== 0;
  return { nextN: c, offset: d, isZero: u, isNeg: f, isNegF: h, offsetF: l };
}
function v1(e, t) {
  if (!Array.isArray(e))
    throw new Error("array expected");
  e.forEach((n, r) => {
    if (!(n instanceof t))
      throw new Error("invalid point at index " + r);
  });
}
function x1(e, t) {
  if (!Array.isArray(e))
    throw new Error("array of scalars expected");
  e.forEach((n, r) => {
    if (!t.isValid(n))
      throw new Error("invalid scalar at index " + r);
  });
}
const Da = /* @__PURE__ */ new WeakMap(), Fp = /* @__PURE__ */ new WeakMap();
function za(e) {
  return Fp.get(e) || 1;
}
function Eu(e) {
  if (e !== Yr)
    throw new Error("invalid wNAF");
}
class k1 {
  // Parametrized with a given Point class (not individual point)
  constructor(t, n) {
    this.BASE = t.BASE, this.ZERO = t.ZERO, this.Fn = t.Fn, this.bits = n;
  }
  // non-const time multiplication ladder
  _unsafeLadder(t, n, r = this.ZERO) {
    let i = t;
    for (; n > Yr; )
      n & rr && (r = r.add(i)), i = i.double(), n >>= rr;
    return r;
  }
  /**
   * Creates a wNAF precomputation window. Used for caching.
   * Default window size is set by `utils.precompute()` and is equal to 8.
   * Number of precomputed points depends on the curve size:
   * 2^(𝑊−1) * (Math.ceil(𝑛 / 𝑊) + 1), where:
   * - 𝑊 is the window size
   * - 𝑛 is the bitlength of the curve order.
   * For a 256-bit curve and window size 8, the number of precomputed points is 128 * 33 = 4224.
   * @param point Point instance
   * @param W window size
   * @returns precomputed point tables flattened to a single array
   */
  precomputeWindow(t, n) {
    const { windows: r, windowSize: i } = Ua(n, this.bits), o = [];
    let s = t, a = s;
    for (let c = 0; c < r; c++) {
      a = s, o.push(a);
      for (let l = 1; l < i; l++)
        a = a.add(s), o.push(a);
      s = a.double();
    }
    return o;
  }
  /**
   * Implements ec multiplication using precomputed tables and w-ary non-adjacent form.
   * More compact implementation:
   * https://github.com/paulmillr/noble-secp256k1/blob/47cb1669b6e506ad66b35fe7d76132ae97465da2/index.ts#L502-L541
   * @returns real and fake (for const-time) points
   */
  wNAF(t, n, r) {
    if (!this.Fn.isValid(r))
      throw new Error("invalid scalar");
    let i = this.ZERO, o = this.BASE;
    const s = Ua(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: d, isNeg: u, isNegF: f, offsetF: h } = ku(r, a, s);
      r = c, d ? o = o.add(fs(f, n[h])) : i = i.add(fs(u, n[l]));
    }
    return Eu(r), { p: i, f: o };
  }
  /**
   * Implements ec unsafe (non const-time) multiplication using precomputed tables and w-ary non-adjacent form.
   * @param acc accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = Ua(t, this.bits);
    for (let s = 0; s < o.windows && r !== Yr; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: d } = ku(r, s, o);
      if (r = a, !l) {
        const u = n[c];
        i = i.add(d ? u.negate() : u);
      }
    }
    return Eu(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Da.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Da.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = za(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = za(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    Kp(n, this.bits), Fp.set(t, n), Da.delete(t);
  }
  hasCache(t) {
    return za(t) !== 1;
  }
}
function E1(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > Yr || r > Yr; )
    n & rr && (o = o.add(i)), r & rr && (s = s.add(i)), i = i.double(), n >>= rr, r >>= rr;
  return { p1: o, p2: s };
}
function S1(e, t, n, r) {
  v1(n, e), x1(r, t);
  const i = n.length, o = r.length;
  if (i !== o)
    throw new Error("arrays of points and scalars must have equal length");
  const s = e.ZERO, a = Bp(BigInt(i));
  let c = 1;
  a > 12 ? c = a - 3 : a > 4 ? c = a - 2 : a > 0 && (c = 2);
  const l = Ji(c), d = new Array(Number(l) + 1).fill(s), u = Math.floor((t.BITS - 1) / c) * c;
  let f = s;
  for (let h = u; h >= 0; h -= c) {
    d.fill(s);
    for (let w = 0; w < o; w++) {
      const p = r[w], g = Number(p >> BigInt(h) & l);
      d[g] = d[g].add(n[w]);
    }
    let y = s;
    for (let w = d.length - 1, p = s; w > 0; w--)
      p = p.add(d[w]), y = y.add(p);
    if (f = f.add(y), h !== 0)
      for (let w = 0; w < c; w++)
        f = f.double();
  }
  return f;
}
function Su(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return t1(t), t;
  } else
    return Qi(e, { isLE: n });
}
function A1(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > Yr))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = Su(t.p, n.Fp, r), o = Su(t.n, n.Fn, r), a = ["Gx", "Gy", "a", "b"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Au = (e, t) => (e + (e >= 0 ? t : -t) / jp) / t;
function _1(e, t, n) {
  const [[r, i], [o, s]] = t, a = Au(s * e, n), c = Au(-i * e, n);
  let l = e - a * r - c * o, d = -a * i - c * s;
  const u = l < nn, f = d < nn;
  u && (l = -l), f && (d = -d);
  const h = Ji(Math.ceil(Bp(n) / 2)) + Fr;
  if (l < nn || l >= h || d < nn || d >= h)
    throw new Error("splitScalar (endomorphism): failed, k=" + e);
  return { k1neg: u, k1: l, k2neg: f, k2: d };
}
function Ac(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function Ma(e, t) {
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return ds(n.lowS, "lowS"), ds(n.prehash, "prehash"), n.format !== void 0 && Ac(n.format), n;
}
class I1 extends Error {
  constructor(t = "") {
    super(t);
  }
}
const en = {
  // asn.1 DER encoding utils
  Err: I1,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = en;
      if (e < 0 || e > 256)
        throw new n("tlv.encode: wrong tag");
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = go(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? go(i.length / 2 | 128) : "";
      return go(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = en;
      let r = 0;
      if (e < 0 || e > 256)
        throw new n("tlv.encode: wrong tag");
      if (t.length < 2 || t[r++] !== e)
        throw new n("tlv.decode: wrong tlv");
      const i = t[r++], o = !!(i & 128);
      let s = 0;
      if (!o)
        s = i;
      else {
        const c = i & 127;
        if (!c)
          throw new n("tlv.decode(long): indefinite length not supported");
        if (c > 4)
          throw new n("tlv.decode(long): byte length is too big");
        const l = t.subarray(r, r + c);
        if (l.length !== c)
          throw new n("tlv.decode: length bytes not complete");
        if (l[0] === 0)
          throw new n("tlv.decode(long): zero leftmost byte");
        for (const d of l)
          s = s << 8 | d;
        if (r += c, s < 128)
          throw new n("tlv.decode(long): not minimal encoding");
      }
      const a = t.subarray(r, r + s);
      if (a.length !== s)
        throw new n("tlv.decode: wrong value length");
      return { v: a, l: t.subarray(r + s) };
    }
  },
  // https://crypto.stackexchange.com/a/57734 Leftmost bit of first byte is 'negative' flag,
  // since we always use positive integers here. It must always be empty:
  // - add zero byte if exists
  // - if next byte doesn't have a flag, leading zero is not allowed (minimal encoding)
  _int: {
    encode(e) {
      const { Err: t } = en;
      if (e < nn)
        throw new t("integer: negative integers are not allowed");
      let n = go(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = en;
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return ta(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = en, i = Ze("signature", e), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: d } = r.decode(2, c);
    if (d.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = en, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
}, nn = BigInt(0), Fr = BigInt(1), jp = BigInt(2), xo = BigInt(3), B1 = BigInt(4);
function Nr(e, t) {
  const { BYTES: n } = e;
  let r;
  if (typeof t == "bigint")
    r = t;
  else {
    let i = Ze("private key", t);
    try {
      r = e.fromBytes(i);
    } catch {
      throw new Error(`invalid private key: expected ui8a of size ${n}, got ${typeof t}`);
    }
  }
  if (!e.isValidNot0(r))
    throw new Error("invalid private key: out of range [1..N-1]");
  return r;
}
function T1(e, t = {}) {
  const n = A1("weierstrass", e, t), { Fp: r, Fn: i } = n;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  xl(t, {}, {
    allowInfinityPoint: "boolean",
    clearCofactor: "function",
    isTorsionFree: "function",
    fromBytes: "function",
    toBytes: "function",
    endo: "object",
    wrapPrivateKey: "boolean"
  });
  const { endo: c } = t;
  if (c && (!r.is0(o.a) || typeof c.beta != "bigint" || !Array.isArray(c.basises)))
    throw new Error('invalid endo: expected "beta": bigint and "basises": array');
  const l = qp(r, i);
  function d() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function u(E, x, v) {
    const { x: m, y: A } = x.toAffine(), $ = r.toBytes(m);
    if (ds(v, "isCompressed"), v) {
      d();
      const S = !r.isOdd(A);
      return lt(Vp(S), $);
    } else
      return lt(Uint8Array.of(4), $, r.toBytes(A));
  }
  function f(E) {
    tr(E, void 0, "Point");
    const { publicKey: x, publicKeyUncompressed: v } = l, m = E.length, A = E[0], $ = E.subarray(1);
    if (m === x && (A === 2 || A === 3)) {
      const S = r.fromBytes($);
      if (!r.isValid(S))
        throw new Error("bad point: is not on curve, wrong x");
      const I = w(S);
      let P;
      try {
        P = r.sqrt(I);
      } catch (K) {
        const U = K instanceof Error ? ": " + K.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + U);
      }
      d();
      const C = r.isOdd(P);
      return (A & 1) === 1 !== C && (P = r.neg(P)), { x: S, y: P };
    } else if (m === v && A === 4) {
      const S = r.BYTES, I = r.fromBytes($.subarray(0, S)), P = r.fromBytes($.subarray(S, S * 2));
      if (!p(I, P))
        throw new Error("bad point: is not on curve");
      return { x: I, y: P };
    } else
      throw new Error(`bad point: got length ${m}, expected compressed=${x} or uncompressed=${v}`);
  }
  const h = t.toBytes || u, y = t.fromBytes || f;
  function w(E) {
    const x = r.sqr(E), v = r.mul(x, E);
    return r.add(r.add(v, r.mul(E, o.a)), o.b);
  }
  function p(E, x) {
    const v = r.sqr(x), m = w(E);
    return r.eql(v, m);
  }
  if (!p(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const g = r.mul(r.pow(o.a, xo), B1), k = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(g, k)))
    throw new Error("bad curve params: a or b");
  function b(E, x, v = !1) {
    if (!r.isValid(x) || v && r.is0(x))
      throw new Error(`bad point coordinate ${E}`);
    return x;
  }
  function O(E) {
    if (!(E instanceof T))
      throw new Error("ProjectivePoint expected");
  }
  function B(E) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return _1(E, c.basises, i.ORDER);
  }
  const R = yu((E, x) => {
    const { X: v, Y: m, Z: A } = E;
    if (r.eql(A, r.ONE))
      return { x: v, y: m };
    const $ = E.is0();
    x == null && (x = $ ? r.ONE : r.inv(A));
    const S = r.mul(v, x), I = r.mul(m, x), P = r.mul(A, x);
    if ($)
      return { x: r.ZERO, y: r.ZERO };
    if (!r.eql(P, r.ONE))
      throw new Error("invZ was invalid");
    return { x: S, y: I };
  }), H = yu((E) => {
    if (E.is0()) {
      if (t.allowInfinityPoint && !r.is0(E.Y))
        return;
      throw new Error("bad point: ZERO");
    }
    const { x, y: v } = E.toAffine();
    if (!r.isValid(x) || !r.isValid(v))
      throw new Error("bad point: x or y not field elements");
    if (!p(x, v))
      throw new Error("bad point: equation left != right");
    if (!E.isTorsionFree())
      throw new Error("bad point: not in prime-order subgroup");
    return !0;
  });
  function L(E, x, v, m, A) {
    return v = new T(r.mul(v.X, E), v.Y, v.Z), x = fs(m, x), v = fs(A, v), x.add(v);
  }
  class T {
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    constructor(x, v, m) {
      this.X = b("x", x), this.Y = b("y", v, !0), this.Z = b("z", m), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(x) {
      const { x: v, y: m } = x || {};
      if (!x || !r.isValid(v) || !r.isValid(m))
        throw new Error("invalid affine point");
      if (x instanceof T)
        throw new Error("projective point not allowed");
      return r.is0(v) && r.is0(m) ? T.ZERO : new T(v, m, r.ONE);
    }
    static fromBytes(x) {
      const v = T.fromAffine(y(tr(x, void 0, "point")));
      return v.assertValidity(), v;
    }
    static fromHex(x) {
      return T.fromBytes(Ze("pointHex", x));
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    /**
     *
     * @param windowSize
     * @param isLazy true will defer table computation until the first multiplication
     * @returns
     */
    precompute(x = 8, v = !0) {
      return z.createCache(this, x), v || this.multiply(xo), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      H(this);
    }
    hasEvenY() {
      const { y: x } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(x);
    }
    /** Compare one point to another. */
    equals(x) {
      O(x);
      const { X: v, Y: m, Z: A } = this, { X: $, Y: S, Z: I } = x, P = r.eql(r.mul(v, I), r.mul($, A)), C = r.eql(r.mul(m, I), r.mul(S, A));
      return P && C;
    }
    /** Flips point to one corresponding to (x, -y) in Affine coordinates. */
    negate() {
      return new T(this.X, r.neg(this.Y), this.Z);
    }
    // Renes-Costello-Batina exception-free doubling formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 3
    // Cost: 8M + 3S + 3*a + 2*b3 + 15add.
    double() {
      const { a: x, b: v } = o, m = r.mul(v, xo), { X: A, Y: $, Z: S } = this;
      let I = r.ZERO, P = r.ZERO, C = r.ZERO, N = r.mul(A, A), K = r.mul($, $), U = r.mul(S, S), M = r.mul(A, $);
      return M = r.add(M, M), C = r.mul(A, S), C = r.add(C, C), I = r.mul(x, C), P = r.mul(m, U), P = r.add(I, P), I = r.sub(K, P), P = r.add(K, P), P = r.mul(I, P), I = r.mul(M, I), C = r.mul(m, C), U = r.mul(x, U), M = r.sub(N, U), M = r.mul(x, M), M = r.add(M, C), C = r.add(N, N), N = r.add(C, N), N = r.add(N, U), N = r.mul(N, M), P = r.add(P, N), U = r.mul($, S), U = r.add(U, U), N = r.mul(U, M), I = r.sub(I, N), C = r.mul(U, K), C = r.add(C, C), C = r.add(C, C), new T(I, P, C);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(x) {
      O(x);
      const { X: v, Y: m, Z: A } = this, { X: $, Y: S, Z: I } = x;
      let P = r.ZERO, C = r.ZERO, N = r.ZERO;
      const K = o.a, U = r.mul(o.b, xo);
      let M = r.mul(v, $), F = r.mul(m, S), V = r.mul(A, I), q = r.add(v, m), W = r.add($, S);
      q = r.mul(q, W), W = r.add(M, F), q = r.sub(q, W), W = r.add(v, A);
      let Y = r.add($, I);
      return W = r.mul(W, Y), Y = r.add(M, V), W = r.sub(W, Y), Y = r.add(m, A), P = r.add(S, I), Y = r.mul(Y, P), P = r.add(F, V), Y = r.sub(Y, P), N = r.mul(K, W), P = r.mul(U, V), N = r.add(P, N), P = r.sub(F, N), N = r.add(F, N), C = r.mul(P, N), F = r.add(M, M), F = r.add(F, M), V = r.mul(K, V), W = r.mul(U, W), F = r.add(F, V), V = r.sub(M, V), V = r.mul(K, V), W = r.add(W, V), M = r.mul(F, W), C = r.add(C, M), M = r.mul(Y, W), P = r.mul(q, P), P = r.sub(P, M), M = r.mul(q, F), N = r.mul(Y, N), N = r.add(N, M), new T(P, C, N);
    }
    subtract(x) {
      return this.add(x.negate());
    }
    is0() {
      return this.equals(T.ZERO);
    }
    /**
     * Constant time multiplication.
     * Uses wNAF method. Windowed method may be 10% faster,
     * but takes 2x longer to generate and consumes 2x memory.
     * Uses precomputes when available.
     * Uses endomorphism for Koblitz curves.
     * @param scalar by which the point would be multiplied
     * @returns New point
     */
    multiply(x) {
      const { endo: v } = t;
      if (!i.isValidNot0(x))
        throw new Error("invalid scalar: out of range");
      let m, A;
      const $ = (S) => z.cached(this, S, (I) => Na(T, I));
      if (v) {
        const { k1neg: S, k1: I, k2neg: P, k2: C } = B(x), { p: N, f: K } = $(I), { p: U, f: M } = $(C);
        A = K.add(M), m = L(v.beta, N, U, S, P);
      } else {
        const { p: S, f: I } = $(x);
        m = S, A = I;
      }
      return Na(T, [m, A])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(x) {
      const { endo: v } = t, m = this;
      if (!i.isValid(x))
        throw new Error("invalid scalar: out of range");
      if (x === nn || m.is0())
        return T.ZERO;
      if (x === Fr)
        return m;
      if (z.hasCache(this))
        return this.multiply(x);
      if (v) {
        const { k1neg: A, k1: $, k2neg: S, k2: I } = B(x), { p1: P, p2: C } = E1(T, m, $, I);
        return L(v.beta, P, C, A, S);
      } else
        return z.unsafe(m, x);
    }
    multiplyAndAddUnsafe(x, v, m) {
      const A = this.multiplyUnsafe(v).add(x.multiplyUnsafe(m));
      return A.is0() ? void 0 : A;
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * @param invertedZ Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(x) {
      return R(this, x);
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: x } = t;
      return s === Fr ? !0 : x ? x(T, this) : z.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: x } = t;
      return s === Fr ? this : x ? x(T, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return this.multiplyUnsafe(s).is0();
    }
    toBytes(x = !0) {
      return ds(x, "isCompressed"), this.assertValidity(), h(T, this, x);
    }
    toHex(x = !0) {
      return dr(this.toBytes(x));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
    // TODO: remove
    get px() {
      return this.X;
    }
    get py() {
      return this.X;
    }
    get pz() {
      return this.Z;
    }
    toRawBytes(x = !0) {
      return this.toBytes(x);
    }
    _setWindowSize(x) {
      this.precompute(x);
    }
    static normalizeZ(x) {
      return Na(T, x);
    }
    static msm(x, v) {
      return S1(T, i, x, v);
    }
    static fromPrivateKey(x) {
      return T.BASE.multiply(Nr(i, x));
    }
  }
  T.BASE = new T(o.Gx, o.Gy, r.ONE), T.ZERO = new T(r.ZERO, r.ONE, r.ZERO), T.Fp = r, T.Fn = i;
  const D = i.BITS, z = new k1(T, t.endo ? Math.ceil(D / 2) : D);
  return T.BASE.precompute(8), T;
}
function Vp(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function qp(e, t) {
  return {
    secretKey: t.BYTES,
    publicKey: 1 + e.BYTES,
    publicKeyUncompressed: 1 + 2 * e.BYTES,
    publicKeyHasPrefix: !0,
    signature: 2 * t.BYTES
  };
}
function P1(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes || Sp, i = Object.assign(qp(e.Fp, n), { seed: Dp(n.ORDER) });
  function o(h) {
    try {
      return !!Nr(n, h);
    } catch {
      return !1;
    }
  }
  function s(h, y) {
    const { publicKey: w, publicKeyUncompressed: p } = i;
    try {
      const g = h.length;
      return y === !0 && g !== w || y === !1 && g !== p ? !1 : !!e.fromBytes(h);
    } catch {
      return !1;
    }
  }
  function a(h = r(i.seed)) {
    return r1(tr(h, i.seed, "seed"), n.ORDER);
  }
  function c(h, y = !0) {
    return e.BASE.multiply(Nr(n, h)).toBytes(y);
  }
  function l(h) {
    const y = a(h);
    return { secretKey: y, publicKey: c(y) };
  }
  function d(h) {
    if (typeof h == "bigint")
      return !1;
    if (h instanceof e)
      return !0;
    const { secretKey: y, publicKey: w, publicKeyUncompressed: p } = i;
    if (n.allowedLengths || y === w)
      return;
    const g = Ze("key", h).length;
    return g === w || g === p;
  }
  function u(h, y, w = !0) {
    if (d(h) === !0)
      throw new Error("first arg must be private key");
    if (d(y) === !1)
      throw new Error("second arg must be public key");
    const p = Nr(n, h);
    return e.fromHex(y).multiply(p).toBytes(w);
  }
  return Object.freeze({ getPublicKey: c, getSharedSecret: u, keygen: l, Point: e, utils: {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a,
    // TODO: remove
    isValidPrivateKey: o,
    randomPrivateKey: a,
    normPrivateKeyToScalar: (h) => Nr(n, h),
    precompute(h = 8, y = e.BASE) {
      return y.precompute(h, !1);
    }
  }, lengths: i });
}
function $1(e, t, n = {}) {
  vp(t), xl(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  });
  const r = n.randomBytes || Sp, i = n.hmac || ((v, ...m) => us(t, v, lt(...m))), { Fp: o, Fn: s } = e, { ORDER: a, BITS: c } = s, { keygen: l, getPublicKey: d, getSharedSecret: u, utils: f, lengths: h } = P1(e, n), y = {
    prehash: !1,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !1,
    format: void 0,
    //'compact' as ECDSASigFormat,
    extraEntropy: !1
  }, w = "compact";
  function p(v) {
    const m = a >> Fr;
    return v > m;
  }
  function g(v, m) {
    if (!s.isValidNot0(m))
      throw new Error(`invalid signature ${v}: out of range 1..Point.Fn.ORDER`);
    return m;
  }
  function k(v, m) {
    Ac(m);
    const A = h.signature, $ = m === "compact" ? A : m === "recovered" ? A + 1 : void 0;
    return tr(v, $, `${m} signature`);
  }
  class b {
    constructor(m, A, $) {
      this.r = g("r", m), this.s = g("s", A), $ != null && (this.recovery = $), Object.freeze(this);
    }
    static fromBytes(m, A = w) {
      k(m, A);
      let $;
      if (A === "der") {
        const { r: C, s: N } = en.toSig(tr(m));
        return new b(C, N);
      }
      A === "recovered" && ($ = m[0], A = "compact", m = m.subarray(1));
      const S = s.BYTES, I = m.subarray(0, S), P = m.subarray(S, S * 2);
      return new b(s.fromBytes(I), s.fromBytes(P), $);
    }
    static fromHex(m, A) {
      return this.fromBytes(Hi(m), A);
    }
    addRecoveryBit(m) {
      return new b(this.r, this.s, m);
    }
    recoverPublicKey(m) {
      const A = o.ORDER, { r: $, s: S, recovery: I } = this;
      if (I == null || ![0, 1, 2, 3].includes(I))
        throw new Error("recovery id invalid");
      if (a * jp < A && I > 1)
        throw new Error("recovery id is ambiguous for h>1 curve");
      const C = I === 2 || I === 3 ? $ + a : $;
      if (!o.isValid(C))
        throw new Error("recovery id 2 or 3 invalid");
      const N = o.toBytes(C), K = e.fromBytes(lt(Vp((I & 1) === 0), N)), U = s.inv(C), M = B(Ze("msgHash", m)), F = s.create(-M * U), V = s.create(S * U), q = e.BASE.multiplyUnsafe(F).add(K.multiplyUnsafe(V));
      if (q.is0())
        throw new Error("point at infinify");
      return q.assertValidity(), q;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return p(this.s);
    }
    toBytes(m = w) {
      if (Ac(m), m === "der")
        return Hi(en.hexFromSig(this));
      const A = s.toBytes(this.r), $ = s.toBytes(this.s);
      if (m === "recovered") {
        if (this.recovery == null)
          throw new Error("recovery bit must be present");
        return lt(Uint8Array.of(this.recovery), A, $);
      }
      return lt(A, $);
    }
    toHex(m) {
      return dr(this.toBytes(m));
    }
    // TODO: remove
    assertValidity() {
    }
    static fromCompact(m) {
      return b.fromBytes(Ze("sig", m), "compact");
    }
    static fromDER(m) {
      return b.fromBytes(Ze("sig", m), "der");
    }
    normalizeS() {
      return this.hasHighS() ? new b(this.r, s.neg(this.s), this.recovery) : this;
    }
    toDERRawBytes() {
      return this.toBytes("der");
    }
    toDERHex() {
      return dr(this.toBytes("der"));
    }
    toCompactRawBytes() {
      return this.toBytes("compact");
    }
    toCompactHex() {
      return dr(this.toBytes("compact"));
    }
  }
  const O = n.bits2int || function(m) {
    if (m.length > 8192)
      throw new Error("input is too large");
    const A = ta(m), $ = m.length * 8 - c;
    return $ > 0 ? A >> BigInt($) : A;
  }, B = n.bits2int_modN || function(m) {
    return s.create(O(m));
  }, R = Ji(c);
  function H(v) {
    return Wv("num < 2^" + c, v, nn, R), s.toBytes(v);
  }
  function L(v, m) {
    return tr(v, void 0, "message"), m ? tr(t(v), void 0, "prehashed message") : v;
  }
  function T(v, m, A) {
    if (["recovered", "canonical"].some((F) => F in A))
      throw new Error("sign() legacy options not supported");
    const { lowS: $, prehash: S, extraEntropy: I } = Ma(A, y);
    v = L(v, S);
    const P = B(v), C = Nr(s, m), N = [H(C), H(P)];
    if (I != null && I !== !1) {
      const F = I === !0 ? r(h.secretKey) : I;
      N.push(Ze("extraEntropy", F));
    }
    const K = lt(...N), U = P;
    function M(F) {
      const V = O(F);
      if (!s.isValidNot0(V))
        return;
      const q = s.inv(V), W = e.BASE.multiply(V).toAffine(), Y = s.create(W.x);
      if (Y === nn)
        return;
      const pe = s.create(q * s.create(U + Y * C));
      if (pe === nn)
        return;
      let se = (W.x === Y ? 0 : 2) | Number(W.y & Fr), be = pe;
      return $ && p(pe) && (be = s.neg(pe), se ^= 1), new b(Y, be, se);
    }
    return { seed: K, k2sig: M };
  }
  function D(v, m, A = {}) {
    v = Ze("message", v);
    const { seed: $, k2sig: S } = T(v, m, A);
    return Gv(t.outputLen, s.BYTES, i)($, S);
  }
  function z(v) {
    let m;
    const A = typeof v == "string" || ea(v), $ = !A && v !== null && typeof v == "object" && typeof v.r == "bigint" && typeof v.s == "bigint";
    if (!A && !$)
      throw new Error("invalid signature, expected Uint8Array, hex string or Signature instance");
    if ($)
      m = new b(v.r, v.s);
    else if (A) {
      try {
        m = b.fromBytes(Ze("sig", v), "der");
      } catch (S) {
        if (!(S instanceof en.Err))
          throw S;
      }
      if (!m)
        try {
          m = b.fromBytes(Ze("sig", v), "compact");
        } catch {
          return !1;
        }
    }
    return m || !1;
  }
  function E(v, m, A, $ = {}) {
    const { lowS: S, prehash: I, format: P } = Ma($, y);
    if (A = Ze("publicKey", A), m = L(Ze("message", m), I), "strict" in $)
      throw new Error("options.strict was renamed to lowS");
    const C = P === void 0 ? z(v) : b.fromBytes(Ze("sig", v), P);
    if (C === !1)
      return !1;
    try {
      const N = e.fromBytes(A);
      if (S && C.hasHighS())
        return !1;
      const { r: K, s: U } = C, M = B(m), F = s.inv(U), V = s.create(M * F), q = s.create(K * F), W = e.BASE.multiplyUnsafe(V).add(N.multiplyUnsafe(q));
      return W.is0() ? !1 : s.create(W.x) === K;
    } catch {
      return !1;
    }
  }
  function x(v, m, A = {}) {
    const { prehash: $ } = Ma(A, y);
    return m = L(m, $), b.fromBytes(v, "recovered").recoverPublicKey(m).toBytes();
  }
  return Object.freeze({
    keygen: l,
    getPublicKey: d,
    getSharedSecret: u,
    utils: f,
    lengths: h,
    Point: e,
    sign: D,
    verify: E,
    recoverPublicKey: x,
    Signature: b,
    hash: t
  });
}
function O1(e) {
  const t = {
    a: e.a,
    b: e.b,
    p: e.Fp.ORDER,
    n: e.n,
    h: e.h,
    Gx: e.Gx,
    Gy: e.Gy
  }, n = e.Fp;
  let r = e.allowedPrivateKeyLengths ? Array.from(new Set(e.allowedPrivateKeyLengths.map((s) => Math.ceil(s / 2)))) : void 0;
  const i = Qi(t.n, {
    BITS: e.nBitLength,
    allowedLengths: r,
    modFromBytes: e.wrapPrivateKey
  }), o = {
    Fp: n,
    Fn: i,
    allowInfinityPoint: e.allowInfinityPoint,
    endo: e.endo,
    isTorsionFree: e.isTorsionFree,
    clearCofactor: e.clearCofactor,
    fromBytes: e.fromBytes,
    toBytes: e.toBytes
  };
  return { CURVE: t, curveOpts: o };
}
function C1(e) {
  const { CURVE: t, curveOpts: n } = O1(e), r = {
    hmac: e.hmac,
    randomBytes: e.randomBytes,
    lowS: e.lowS,
    bits2int: e.bits2int,
    bits2int_modN: e.bits2int_modN
  };
  return { CURVE: t, curveOpts: n, hash: e.hash, ecdsaOpts: r };
}
function R1(e, t) {
  const n = t.Point;
  return Object.assign({}, t, {
    ProjectivePoint: n,
    CURVE: Object.assign({}, e, Np(n.Fn.ORDER, n.Fn.BITS))
  });
}
function L1(e) {
  const { CURVE: t, curveOpts: n, hash: r, ecdsaOpts: i } = C1(e), o = T1(t, n), s = $1(o, r, i);
  return R1(e, s);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function H1(e, t) {
  const n = (r) => L1({ ...e, hash: r });
  return { ...n(t), create: n };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Al = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, N1 = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, _u = /* @__PURE__ */ BigInt(2);
function U1(e) {
  const t = Al.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, d = l * l * e % t, u = ot(d, n, t) * d % t, f = ot(u, n, t) * d % t, h = ot(f, _u, t) * l % t, y = ot(h, i, t) * h % t, w = ot(y, o, t) * y % t, p = ot(w, a, t) * w % t, g = ot(p, c, t) * p % t, k = ot(g, a, t) * w % t, b = ot(k, n, t) * d % t, O = ot(b, s, t) * y % t, B = ot(O, r, t) * l % t, R = ot(B, _u, t);
  if (!_c.eql(_c.sqr(R), e))
    throw new Error("Cannot find square root");
  return R;
}
const _c = Qi(Al.p, { sqrt: U1 }), Jt = H1({ ...Al, Fp: _c, lowS: !0, endo: N1 }, Sl), D1 = /* @__PURE__ */ Uint8Array.from([
  7,
  4,
  13,
  1,
  10,
  6,
  15,
  3,
  12,
  0,
  9,
  5,
  2,
  14,
  11,
  8
]), Wp = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), z1 = Wp.map((e) => (9 * e + 5) % 16), Gp = /* @__PURE__ */ (() => {
  const n = [[Wp], [z1]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => D1[o]));
  return n;
})(), Zp = Gp[0], Yp = Gp[1], Xp = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), M1 = /* @__PURE__ */ Zp.map((e, t) => e.map((n) => Xp[t][n])), K1 = /* @__PURE__ */ Yp.map((e, t) => e.map((n) => Xp[t][n])), F1 = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), j1 = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function Iu(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const ko = /* @__PURE__ */ new Uint32Array(16);
class V1 extends El {
  constructor() {
    super(64, 20, 8, !0), this.h0 = 1732584193, this.h1 = -271733879, this.h2 = -1732584194, this.h3 = 271733878, this.h4 = -1009589776;
  }
  get() {
    const { h0: t, h1: n, h2: r, h3: i, h4: o } = this;
    return [t, n, r, i, o];
  }
  set(t, n, r, i, o) {
    this.h0 = t | 0, this.h1 = n | 0, this.h2 = r | 0, this.h3 = i | 0, this.h4 = o | 0;
  }
  process(t, n) {
    for (let h = 0; h < 16; h++, n += 4)
      ko[h] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, d = l, u = this.h4 | 0, f = u;
    for (let h = 0; h < 5; h++) {
      const y = 4 - h, w = F1[h], p = j1[h], g = Zp[h], k = Yp[h], b = M1[h], O = K1[h];
      for (let B = 0; B < 16; B++) {
        const R = wo(r + Iu(h, o, a, l) + ko[g[B]] + w, b[B]) + u | 0;
        r = u, u = l, l = wo(a, 10) | 0, a = o, o = R;
      }
      for (let B = 0; B < 16; B++) {
        const R = wo(i + Iu(y, s, c, d) + ko[k[B]] + p, O[B]) + f | 0;
        i = f, f = d, d = wo(c, 10) | 0, c = s, s = R;
      }
    }
    this.set(this.h1 + a + d | 0, this.h2 + l + f | 0, this.h3 + u + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    Fn(ko);
  }
  destroy() {
    this.destroyed = !0, Fn(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const q1 = /* @__PURE__ */ ml(() => new V1());
/*! scure-base - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Ni(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Jp(e, t) {
  return Array.isArray(t) ? t.length === 0 ? !0 : e ? t.every((n) => typeof n == "string") : t.every((n) => Number.isSafeInteger(n)) : !1;
}
function Qp(e) {
  if (typeof e != "function")
    throw new Error("function expected");
  return !0;
}
function Xr(e, t) {
  if (typeof t != "string")
    throw new Error(`${e}: string expected`);
  return !0;
}
function oi(e) {
  if (!Number.isSafeInteger(e))
    throw new Error(`invalid integer: ${e}`);
}
function hs(e) {
  if (!Array.isArray(e))
    throw new Error("array expected");
}
function ps(e, t) {
  if (!Jp(!0, t))
    throw new Error(`${e}: array of strings expected`);
}
function _l(e, t) {
  if (!Jp(!1, t))
    throw new Error(`${e}: array of numbers expected`);
}
// @__NO_SIDE_EFFECTS__
function na(...e) {
  const t = (o) => o, n = (o, s) => (a) => o(s(a)), r = e.map((o) => o.encode).reduceRight(n, t), i = e.map((o) => o.decode).reduce(n, t);
  return { encode: r, decode: i };
}
// @__NO_SIDE_EFFECTS__
function Il(e) {
  const t = typeof e == "string" ? e.split("") : e, n = t.length;
  ps("alphabet", t);
  const r = new Map(t.map((i, o) => [i, o]));
  return {
    encode: (i) => (hs(i), i.map((o) => {
      if (!Number.isSafeInteger(o) || o < 0 || o >= n)
        throw new Error(`alphabet.encode: digit index outside alphabet "${o}". Allowed: ${e}`);
      return t[o];
    })),
    decode: (i) => (hs(i), i.map((o) => {
      Xr("alphabet.decode", o);
      const s = r.get(o);
      if (s === void 0)
        throw new Error(`Unknown letter: "${o}". Allowed: ${e}`);
      return s;
    }))
  };
}
// @__NO_SIDE_EFFECTS__
function Bl(e = "") {
  return Xr("join", e), {
    encode: (t) => (ps("join.decode", t), t.join(e)),
    decode: (t) => (Xr("join.decode", t), t.split(e))
  };
}
// @__NO_SIDE_EFFECTS__
function W1(e, t = "=") {
  return oi(e), Xr("padding", t), {
    encode(n) {
      for (ps("padding.encode", n); n.length * e % 8; )
        n.push(t);
      return n;
    },
    decode(n) {
      ps("padding.decode", n);
      let r = n.length;
      if (r * e % 8)
        throw new Error("padding: invalid, string should have whole number of bytes");
      for (; r > 0 && n[r - 1] === t; r--)
        if ((r - 1) * e % 8 === 0)
          throw new Error("padding: invalid, string has too much padding");
      return n.slice(0, r);
    }
  };
}
function Ic(e, t, n) {
  if (t < 2)
    throw new Error(`convertRadix: invalid from=${t}, base cannot be less than 2`);
  if (n < 2)
    throw new Error(`convertRadix: invalid to=${n}, base cannot be less than 2`);
  if (hs(e), !e.length)
    return [];
  let r = 0;
  const i = [], o = Array.from(e, (a) => {
    if (oi(a), a < 0 || a >= t)
      throw new Error(`invalid integer: ${a}`);
    return a;
  }), s = o.length;
  for (; ; ) {
    let a = 0, c = !0;
    for (let l = r; l < s; l++) {
      const d = o[l], u = t * a, f = u + d;
      if (!Number.isSafeInteger(f) || u / t !== a || f - d !== u)
        throw new Error("convertRadix: carry overflow");
      const h = f / n;
      a = f % n;
      const y = Math.floor(h);
      if (o[l] = y, !Number.isSafeInteger(y) || y * n + a !== f)
        throw new Error("convertRadix: carry overflow");
      if (c)
        y ? c = !1 : r = l;
      else continue;
    }
    if (i.push(a), c)
      break;
  }
  for (let a = 0; a < e.length - 1 && e[a] === 0; a++)
    i.push(0);
  return i.reverse();
}
const ey = (e, t) => t === 0 ? e : ey(t, e % t), ys = /* @__NO_SIDE_EFFECTS__ */ (e, t) => e + (t - ey(e, t)), qo = /* @__PURE__ */ (() => {
  let e = [];
  for (let t = 0; t < 40; t++)
    e.push(2 ** t);
  return e;
})();
function ws(e, t, n, r) {
  if (hs(e), t <= 0 || t > 32)
    throw new Error(`convertRadix2: wrong from=${t}`);
  if (n <= 0 || n > 32)
    throw new Error(`convertRadix2: wrong to=${n}`);
  if (/* @__PURE__ */ ys(t, n) > 32)
    throw new Error(`convertRadix2: carry overflow from=${t} to=${n} carryBits=${/* @__PURE__ */ ys(t, n)}`);
  let i = 0, o = 0;
  const s = qo[t], a = qo[n] - 1, c = [];
  for (const l of e) {
    if (oi(l), l >= s)
      throw new Error(`convertRadix2: invalid data word=${l} from=${t}`);
    if (i = i << t | l, o + t > 32)
      throw new Error(`convertRadix2: carry overflow pos=${o} from=${t}`);
    for (o += t; o >= n; o -= n)
      c.push((i >> o - n & a) >>> 0);
    const d = qo[o];
    if (d === void 0)
      throw new Error("invalid carry");
    i &= d - 1;
  }
  if (i = i << n - o & a, !r && o >= t)
    throw new Error("Excess padding");
  if (!r && i > 0)
    throw new Error(`Non-zero padding: ${i}`);
  return r && o > 0 && c.push(i >>> 0), c;
}
// @__NO_SIDE_EFFECTS__
function ty(e) {
  oi(e);
  const t = 2 ** 8;
  return {
    encode: (n) => {
      if (!Ni(n))
        throw new Error("radix.encode input should be Uint8Array");
      return Ic(Array.from(n), t, e);
    },
    decode: (n) => (_l("radix.decode", n), Uint8Array.from(Ic(n, e, t)))
  };
}
// @__NO_SIDE_EFFECTS__
function ny(e, t = !1) {
  if (oi(e), e <= 0 || e > 32)
    throw new Error("radix2: bits should be in (0..32]");
  if (/* @__PURE__ */ ys(8, e) > 32 || /* @__PURE__ */ ys(e, 8) > 32)
    throw new Error("radix2: carry overflow");
  return {
    encode: (n) => {
      if (!Ni(n))
        throw new Error("radix2.encode input should be Uint8Array");
      return ws(Array.from(n), 8, e, !t);
    },
    decode: (n) => (_l("radix2.decode", n), Uint8Array.from(ws(n, e, 8, t)))
  };
}
function Bu(e) {
  return Qp(e), function(...t) {
    try {
      return e.apply(null, t);
    } catch {
    }
  };
}
function ry(e, t) {
  return oi(e), Qp(t), {
    encode(n) {
      if (!Ni(n))
        throw new Error("checksum.encode: input should be Uint8Array");
      const r = t(n).slice(0, e), i = new Uint8Array(n.length + e);
      return i.set(n), i.set(r, n.length), i;
    },
    decode(n) {
      if (!Ni(n))
        throw new Error("checksum.decode: input should be Uint8Array");
      const r = n.slice(0, -e), i = n.slice(-e), o = t(r).slice(0, e);
      for (let s = 0; s < e; s++)
        if (o[s] !== i[s])
          throw new Error("Invalid checksum");
      return r;
    }
  };
}
const Eo = {
  alphabet: Il,
  chain: na,
  checksum: ry,
  convertRadix: Ic,
  convertRadix2: ws,
  radix: ty,
  radix2: ny,
  join: Bl,
  padding: W1
}, G1 = /* @__NO_SIDE_EFFECTS__ */ (e) => /* @__PURE__ */ na(/* @__PURE__ */ ty(58), /* @__PURE__ */ Il(e), /* @__PURE__ */ Bl("")), iy = /* @__PURE__ */ G1("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"), oy = (e) => /* @__PURE__ */ na(ry(4, (t) => e(e(t))), iy), Z1 = oy, Bc = /* @__PURE__ */ na(/* @__PURE__ */ Il("qpzry9x8gf2tvdw0s3jn54khce6mua7l"), /* @__PURE__ */ Bl("")), Tu = [996825010, 642813549, 513874426, 1027748829, 705979059];
function wi(e) {
  const t = e >> 25;
  let n = (e & 33554431) << 5;
  for (let r = 0; r < Tu.length; r++)
    (t >> r & 1) === 1 && (n ^= Tu[r]);
  return n;
}
function Pu(e, t, n = 1) {
  const r = e.length;
  let i = 1;
  for (let o = 0; o < r; o++) {
    const s = e.charCodeAt(o);
    if (s < 33 || s > 126)
      throw new Error(`Invalid prefix (${e})`);
    i = wi(i) ^ s >> 5;
  }
  i = wi(i);
  for (let o = 0; o < r; o++)
    i = wi(i) ^ e.charCodeAt(o) & 31;
  for (let o of t)
    i = wi(i) ^ o;
  for (let o = 0; o < 6; o++)
    i = wi(i);
  return i ^= n, Bc.encode(ws([i % qo[30]], 30, 5, !1));
}
// @__NO_SIDE_EFFECTS__
function sy(e) {
  const t = e === "bech32" ? 1 : 734539939, n = /* @__PURE__ */ ny(5), r = n.decode, i = n.encode, o = Bu(r);
  function s(u, f, h = 90) {
    Xr("bech32.encode prefix", u), Ni(f) && (f = Array.from(f)), _l("bech32.encode", f);
    const y = u.length;
    if (y === 0)
      throw new TypeError(`Invalid prefix length ${y}`);
    const w = y + 7 + f.length;
    if (h !== !1 && w > h)
      throw new TypeError(`Length ${w} exceeds limit ${h}`);
    const p = u.toLowerCase(), g = Pu(p, f, t);
    return `${p}1${Bc.encode(f)}${g}`;
  }
  function a(u, f = 90) {
    Xr("bech32.decode input", u);
    const h = u.length;
    if (h < 8 || f !== !1 && h > f)
      throw new TypeError(`invalid string length: ${h} (${u}). Expected (8..${f})`);
    const y = u.toLowerCase();
    if (u !== y && u !== u.toUpperCase())
      throw new Error("String must be lowercase or uppercase");
    const w = y.lastIndexOf("1");
    if (w === 0 || w === -1)
      throw new Error('Letter "1" must be present between prefix and data only');
    const p = y.slice(0, w), g = y.slice(w + 1);
    if (g.length < 6)
      throw new Error("Data must be at least 6 characters long");
    const k = Bc.decode(g).slice(0, -6), b = Pu(p, k, t);
    if (!g.endsWith(b))
      throw new Error(`Invalid checksum in ${u}: expected "${b}"`);
    return { prefix: p, words: k };
  }
  const c = Bu(a);
  function l(u) {
    const { prefix: f, words: h } = a(u, !1);
    return { prefix: f, words: h, bytes: r(h) };
  }
  function d(u, f) {
    return s(u, i(f));
  }
  return {
    encode: s,
    decode: a,
    encodeFromBytes: d,
    decodeToBytes: l,
    decodeUnsafe: c,
    fromWords: r,
    fromWordsUnsafe: o,
    toWords: i
  };
}
const $u = /* @__PURE__ */ sy("bech32"), Ou = /* @__PURE__ */ sy("bech32m");
/*! scure-bip32 - MIT License (c) 2022 Patricio Palladino, Paul Miller (paulmillr.com) */
const So = Jt.ProjectivePoint, Ka = oy(Sl);
function Cu(e) {
  et(e);
  const t = e.length === 0 ? "0" : dr(e);
  return BigInt("0x" + t);
}
function Y1(e) {
  if (typeof e != "bigint")
    throw new Error("bigint expected");
  return Hi(e.toString(16).padStart(64, "0"));
}
const X1 = kp("Bitcoin seed"), Fa = { private: 76066276, public: 76067358 }, ja = 2147483648, J1 = (e) => q1(Sl(e)), Q1 = (e) => Kr(e).getUint32(0, !1), Ao = (e) => {
  if (!Number.isSafeInteger(e) || e < 0 || e > 2 ** 32 - 1)
    throw new Error("invalid number, should be from 0 to 2**32-1, got " + e);
  const t = new Uint8Array(4);
  return Kr(t).setUint32(0, e, !1), t;
};
class Rn {
  get fingerprint() {
    if (!this.pubHash)
      throw new Error("No publicKey set!");
    return Q1(this.pubHash);
  }
  get identifier() {
    return this.pubHash;
  }
  get pubKeyHash() {
    return this.pubHash;
  }
  get privateKey() {
    return this.privKeyBytes || null;
  }
  get publicKey() {
    return this.pubKey || null;
  }
  get privateExtendedKey() {
    const t = this.privateKey;
    if (!t)
      throw new Error("No private key");
    return Ka.encode(this.serialize(this.versions.private, lt(new Uint8Array([0]), t)));
  }
  get publicExtendedKey() {
    if (!this.pubKey)
      throw new Error("No public key");
    return Ka.encode(this.serialize(this.versions.public, this.pubKey));
  }
  static fromMasterSeed(t, n = Fa) {
    if (et(t), 8 * t.length < 128 || 8 * t.length > 512)
      throw new Error("HDKey: seed length must be between 128 and 512 bits; 256 bits is advised, got " + t.length);
    const r = us(xu, X1, t);
    return new Rn({
      versions: n,
      chainCode: r.slice(32),
      privateKey: r.slice(0, 32)
    });
  }
  static fromExtendedKey(t, n = Fa) {
    const r = Ka.decode(t), i = Kr(r), o = i.getUint32(0, !1), s = {
      versions: n,
      depth: r[4],
      parentFingerprint: i.getUint32(5, !1),
      index: i.getUint32(9, !1),
      chainCode: r.slice(13, 45)
    }, a = r.slice(45), c = a[0] === 0;
    if (o !== n[c ? "private" : "public"])
      throw new Error("Version mismatch");
    return c ? new Rn({ ...s, privateKey: a.slice(1) }) : new Rn({ ...s, publicKey: a });
  }
  static fromJSON(t) {
    return Rn.fromExtendedKey(t.xpriv);
  }
  constructor(t) {
    if (this.depth = 0, this.index = 0, this.chainCode = null, this.parentFingerprint = 0, !t || typeof t != "object")
      throw new Error("HDKey.constructor must not be called directly");
    if (this.versions = t.versions || Fa, this.depth = t.depth || 0, this.chainCode = t.chainCode || null, this.index = t.index || 0, this.parentFingerprint = t.parentFingerprint || 0, !this.depth && (this.parentFingerprint || this.index))
      throw new Error("HDKey: zero depth with non-zero index/parent fingerprint");
    if (t.publicKey && t.privateKey)
      throw new Error("HDKey: publicKey and privateKey at same time.");
    if (t.privateKey) {
      if (!Jt.utils.isValidPrivateKey(t.privateKey))
        throw new Error("Invalid private key");
      this.privKey = typeof t.privateKey == "bigint" ? t.privateKey : Cu(t.privateKey), this.privKeyBytes = Y1(this.privKey), this.pubKey = Jt.getPublicKey(t.privateKey, !0);
    } else if (t.publicKey)
      this.pubKey = So.fromHex(t.publicKey).toRawBytes(!0);
    else
      throw new Error("HDKey: no public or private key provided");
    this.pubHash = J1(this.pubKey);
  }
  derive(t) {
    if (!/^[mM]'?/.test(t))
      throw new Error('Path must start with "m" or "M"');
    if (/^[mM]'?$/.test(t))
      return this;
    const n = t.replace(/^[mM]'?\//, "").split("/");
    let r = this;
    for (const i of n) {
      const o = /^(\d+)('?)$/.exec(i), s = o && o[1];
      if (!o || o.length !== 3 || typeof s != "string")
        throw new Error("invalid child index: " + i);
      let a = +s;
      if (!Number.isSafeInteger(a) || a >= ja)
        throw new Error("Invalid index");
      o[2] === "'" && (a += ja), r = r.deriveChild(a);
    }
    return r;
  }
  deriveChild(t) {
    if (!this.pubKey || !this.chainCode)
      throw new Error("No publicKey or chainCode set");
    let n = Ao(t);
    if (t >= ja) {
      const a = this.privateKey;
      if (!a)
        throw new Error("Could not derive hardened child key");
      n = lt(new Uint8Array([0]), a, n);
    } else
      n = lt(this.pubKey, n);
    const r = us(xu, this.chainCode, n), i = Cu(r.slice(0, 32)), o = r.slice(32);
    if (!Jt.utils.isValidPrivateKey(i))
      throw new Error("Tweak bigger than curve order");
    const s = {
      versions: this.versions,
      chainCode: o,
      depth: this.depth + 1,
      parentFingerprint: this.fingerprint,
      index: t
    };
    try {
      if (this.privateKey) {
        const a = ct(this.privKey + i, Jt.CURVE.n);
        if (!Jt.utils.isValidPrivateKey(a))
          throw new Error("The tweak was out of range or the resulted private key is invalid");
        s.privateKey = a;
      } else {
        const a = So.fromHex(this.pubKey).add(So.fromPrivateKey(i));
        if (a.equals(So.ZERO))
          throw new Error("The tweak was equal to negative P, which made the result key invalid");
        s.publicKey = a.toRawBytes(!0);
      }
      return new Rn(s);
    } catch {
      return this.deriveChild(t + 1);
    }
  }
  sign(t) {
    if (!this.privateKey)
      throw new Error("No privateKey set!");
    return et(t, 32), Jt.sign(t, this.privKey).toCompactRawBytes();
  }
  verify(t, n) {
    if (et(t, 32), et(n, 64), !this.publicKey)
      throw new Error("No publicKey set!");
    let r;
    try {
      r = Jt.Signature.fromCompact(n);
    } catch {
      return !1;
    }
    return Jt.verify(r, t, this.publicKey);
  }
  wipePrivateData() {
    return this.privKey = void 0, this.privKeyBytes && (this.privKeyBytes.fill(0), this.privKeyBytes = void 0), this;
  }
  toJSON() {
    return {
      xpriv: this.privateExtendedKey,
      xpub: this.publicExtendedKey
    };
  }
  serialize(t, n) {
    if (!this.chainCode)
      throw new Error("No chainCode set");
    return et(n, 33), lt(Ao(t), new Uint8Array([this.depth]), Ao(this.parentFingerprint), Ao(this.index), this.chainCode, n);
  }
}
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function ex(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Oi(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function si(e, ...t) {
  if (!ex(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function ay(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash should be wrapped by utils.createHasher");
  Oi(e.outputLen), Oi(e.blockLen);
}
function gs(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function tx(e, t) {
  si(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function hr(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Wo(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Tt(e, t) {
  return e << 32 - t | e >>> t;
}
function cy(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function Tl(e) {
  return typeof e == "string" && (e = cy(e)), si(e), e;
}
function Ru(e) {
  return typeof e == "string" && (e = cy(e)), si(e), e;
}
function nx(e, t) {
  if (t !== void 0 && {}.toString.call(t) !== "[object Object]")
    throw new Error("options should be object or undefined");
  return Object.assign(e, t);
}
let ly = class {
};
function dy(e) {
  const t = (r) => e().update(Tl(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
class uy extends ly {
  constructor(t, n) {
    super(), this.finished = !1, this.destroyed = !1, ay(t);
    const r = Tl(n);
    if (this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const i = this.blockLen, o = new Uint8Array(i);
    o.set(r.length > i ? t.create().update(r).digest() : r);
    for (let s = 0; s < o.length; s++)
      o[s] ^= 54;
    this.iHash.update(o), this.oHash = t.create();
    for (let s = 0; s < o.length; s++)
      o[s] ^= 106;
    this.oHash.update(o), hr(o);
  }
  update(t) {
    return gs(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    gs(this), si(t, this.outputLen), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.oHash.outputLen);
    return this.digestInto(t), t;
  }
  _cloneInto(t) {
    t || (t = Object.create(Object.getPrototypeOf(this), {}));
    const { oHash: n, iHash: r, finished: i, destroyed: o, blockLen: s, outputLen: a } = this;
    return t = t, t.finished = i, t.destroyed = o, t.blockLen = s, t.outputLen = a, t.oHash = n._cloneInto(t.oHash), t.iHash = r._cloneInto(t.iHash), t;
  }
  clone() {
    return this._cloneInto();
  }
  destroy() {
    this.destroyed = !0, this.oHash.destroy(), this.iHash.destroy();
  }
}
const fy = (e, t, n) => new uy(e, t).update(n).digest();
fy.create = (e, t) => new uy(e, t);
function rx(e, t, n, r) {
  ay(e);
  const i = nx({ dkLen: 32, asyncTick: 10 }, r), { c: o, dkLen: s, asyncTick: a } = i;
  if (Oi(o), Oi(s), Oi(a), o < 1)
    throw new Error("iterations (c) should be >= 1");
  const c = Ru(t), l = Ru(n), d = new Uint8Array(s), u = fy.create(e, c), f = u._cloneInto().update(l);
  return { c: o, dkLen: s, asyncTick: a, DK: d, PRF: u, PRFSalt: f };
}
function ix(e, t, n, r, i) {
  return e.destroy(), t.destroy(), r && r.destroy(), hr(i), n;
}
function ox(e, t, n, r) {
  const { c: i, dkLen: o, DK: s, PRF: a, PRFSalt: c } = rx(e, t, n, r);
  let l;
  const d = new Uint8Array(4), u = Wo(d), f = new Uint8Array(a.outputLen);
  for (let h = 1, y = 0; y < o; h++, y += a.outputLen) {
    const w = s.subarray(y, y + a.outputLen);
    u.setInt32(0, h, !1), (l = c._cloneInto(l)).update(d).digestInto(f), w.set(f.subarray(0, w.length));
    for (let p = 1; p < i; p++) {
      a._cloneInto(l).update(f).digestInto(f);
      for (let g = 0; g < w.length; g++)
        w[g] ^= f[g];
    }
  }
  return ix(a, c, s, l, f);
}
function sx(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function ax(e, t, n) {
  return e & t ^ ~e & n;
}
function cx(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let hy = class extends ly {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Wo(this.buffer);
  }
  update(t) {
    gs(this), t = Tl(t), si(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Wo(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    gs(this), tx(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, hr(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let u = s; u < i; u++)
      n[u] = 0;
    sx(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Wo(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const l = c / 4, d = this.get();
    if (l > d.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let u = 0; u < l; u++)
      a.setUint32(4 * u, d[u], o);
  }
  digest() {
    const { buffer: t, outputLen: n } = this;
    this.digestInto(t);
    const r = t.slice(0, n);
    return this.destroy(), r;
  }
  _cloneInto(t) {
    t || (t = new this.constructor()), t.set(...this.get());
    const { blockLen: n, buffer: r, length: i, finished: o, destroyed: s, pos: a } = this;
    return t.destroyed = s, t.finished = o, t.length = i, t.pos = a, i % n && t.buffer.set(r), t;
  }
  clone() {
    return this._cloneInto();
  }
};
const _n = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), He = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  4089235720,
  3144134277,
  2227873595,
  1013904242,
  4271175723,
  2773480762,
  1595750129,
  1359893119,
  2917565137,
  2600822924,
  725511199,
  528734635,
  4215389547,
  1541459225,
  327033209
]), _o = /* @__PURE__ */ BigInt(2 ** 32 - 1), Lu = /* @__PURE__ */ BigInt(32);
function lx(e, t = !1) {
  return t ? { h: Number(e & _o), l: Number(e >> Lu & _o) } : { h: Number(e >> Lu & _o) | 0, l: Number(e & _o) | 0 };
}
function dx(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = lx(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const Hu = (e, t, n) => e >>> n, Nu = (e, t, n) => e << 32 - n | t >>> n, Pr = (e, t, n) => e >>> n | t << 32 - n, $r = (e, t, n) => e << 32 - n | t >>> n, Io = (e, t, n) => e << 64 - n | t >>> n - 32, Bo = (e, t, n) => e >>> n - 32 | t << 64 - n;
function Yt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const ux = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), fx = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, hx = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), px = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, yx = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), wx = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, gx = /* @__PURE__ */ Uint32Array.from([
  1116352408,
  1899447441,
  3049323471,
  3921009573,
  961987163,
  1508970993,
  2453635748,
  2870763221,
  3624381080,
  310598401,
  607225278,
  1426881987,
  1925078388,
  2162078206,
  2614888103,
  3248222580,
  3835390401,
  4022224774,
  264347078,
  604807628,
  770255983,
  1249150122,
  1555081692,
  1996064986,
  2554220882,
  2821834349,
  2952996808,
  3210313671,
  3336571891,
  3584528711,
  113926993,
  338241895,
  666307205,
  773529912,
  1294757372,
  1396182291,
  1695183700,
  1986661051,
  2177026350,
  2456956037,
  2730485921,
  2820302411,
  3259730800,
  3345764771,
  3516065817,
  3600352804,
  4094571909,
  275423344,
  430227734,
  506948616,
  659060556,
  883997877,
  958139571,
  1322822218,
  1537002063,
  1747873779,
  1955562222,
  2024104815,
  2227730452,
  2361852424,
  2428436474,
  2756734187,
  3204031479,
  3329325298
]), In = /* @__PURE__ */ new Uint32Array(64);
let mx = class extends hy {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = _n[0] | 0, this.B = _n[1] | 0, this.C = _n[2] | 0, this.D = _n[3] | 0, this.E = _n[4] | 0, this.F = _n[5] | 0, this.G = _n[6] | 0, this.H = _n[7] | 0;
  }
  get() {
    const { A: t, B: n, C: r, D: i, E: o, F: s, G: a, H: c } = this;
    return [t, n, r, i, o, s, a, c];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c) {
    this.A = t | 0, this.B = n | 0, this.C = r | 0, this.D = i | 0, this.E = o | 0, this.F = s | 0, this.G = a | 0, this.H = c | 0;
  }
  process(t, n) {
    for (let u = 0; u < 16; u++, n += 4)
      In[u] = t.getUint32(n, !1);
    for (let u = 16; u < 64; u++) {
      const f = In[u - 15], h = In[u - 2], y = Tt(f, 7) ^ Tt(f, 18) ^ f >>> 3, w = Tt(h, 17) ^ Tt(h, 19) ^ h >>> 10;
      In[u] = w + In[u - 7] + y + In[u - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: d } = this;
    for (let u = 0; u < 64; u++) {
      const f = Tt(a, 6) ^ Tt(a, 11) ^ Tt(a, 25), h = d + f + ax(a, c, l) + gx[u] + In[u] | 0, w = (Tt(r, 2) ^ Tt(r, 13) ^ Tt(r, 22)) + cx(r, i, o) | 0;
      d = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, d = d + this.H | 0, this.set(r, i, o, s, a, c, l, d);
  }
  roundClean() {
    hr(In);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), hr(this.buffer);
  }
};
const py = dx([
  "0x428a2f98d728ae22",
  "0x7137449123ef65cd",
  "0xb5c0fbcfec4d3b2f",
  "0xe9b5dba58189dbbc",
  "0x3956c25bf348b538",
  "0x59f111f1b605d019",
  "0x923f82a4af194f9b",
  "0xab1c5ed5da6d8118",
  "0xd807aa98a3030242",
  "0x12835b0145706fbe",
  "0x243185be4ee4b28c",
  "0x550c7dc3d5ffb4e2",
  "0x72be5d74f27b896f",
  "0x80deb1fe3b1696b1",
  "0x9bdc06a725c71235",
  "0xc19bf174cf692694",
  "0xe49b69c19ef14ad2",
  "0xefbe4786384f25e3",
  "0x0fc19dc68b8cd5b5",
  "0x240ca1cc77ac9c65",
  "0x2de92c6f592b0275",
  "0x4a7484aa6ea6e483",
  "0x5cb0a9dcbd41fbd4",
  "0x76f988da831153b5",
  "0x983e5152ee66dfab",
  "0xa831c66d2db43210",
  "0xb00327c898fb213f",
  "0xbf597fc7beef0ee4",
  "0xc6e00bf33da88fc2",
  "0xd5a79147930aa725",
  "0x06ca6351e003826f",
  "0x142929670a0e6e70",
  "0x27b70a8546d22ffc",
  "0x2e1b21385c26c926",
  "0x4d2c6dfc5ac42aed",
  "0x53380d139d95b3df",
  "0x650a73548baf63de",
  "0x766a0abb3c77b2a8",
  "0x81c2c92e47edaee6",
  "0x92722c851482353b",
  "0xa2bfe8a14cf10364",
  "0xa81a664bbc423001",
  "0xc24b8b70d0f89791",
  "0xc76c51a30654be30",
  "0xd192e819d6ef5218",
  "0xd69906245565a910",
  "0xf40e35855771202a",
  "0x106aa07032bbd1b8",
  "0x19a4c116b8d2d0c8",
  "0x1e376c085141ab53",
  "0x2748774cdf8eeb99",
  "0x34b0bcb5e19b48a8",
  "0x391c0cb3c5c95a63",
  "0x4ed8aa4ae3418acb",
  "0x5b9cca4f7763e373",
  "0x682e6ff3d6b2b8a3",
  "0x748f82ee5defb2fc",
  "0x78a5636f43172f60",
  "0x84c87814a1f0ab72",
  "0x8cc702081a6439ec",
  "0x90befffa23631e28",
  "0xa4506cebde82bde9",
  "0xbef9a3f7b2c67915",
  "0xc67178f2e372532b",
  "0xca273eceea26619c",
  "0xd186b8c721c0c207",
  "0xeada7dd6cde0eb1e",
  "0xf57d4f7fee6ed178",
  "0x06f067aa72176fba",
  "0x0a637dc5a2c898a6",
  "0x113f9804bef90dae",
  "0x1b710b35131c471b",
  "0x28db77f523047d84",
  "0x32caab7b40c72493",
  "0x3c9ebe0a15c9bebc",
  "0x431d67c49c100d4c",
  "0x4cc5d4becb3e42b6",
  "0x597f299cfc657e2a",
  "0x5fcb6fab3ad6faec",
  "0x6c44198c4a475817"
].map((e) => BigInt(e))), bx = py[0], vx = py[1], Bn = /* @__PURE__ */ new Uint32Array(80), Tn = /* @__PURE__ */ new Uint32Array(80);
class xx extends hy {
  constructor(t = 64) {
    super(128, t, 16, !1), this.Ah = He[0] | 0, this.Al = He[1] | 0, this.Bh = He[2] | 0, this.Bl = He[3] | 0, this.Ch = He[4] | 0, this.Cl = He[5] | 0, this.Dh = He[6] | 0, this.Dl = He[7] | 0, this.Eh = He[8] | 0, this.El = He[9] | 0, this.Fh = He[10] | 0, this.Fl = He[11] | 0, this.Gh = He[12] | 0, this.Gl = He[13] | 0, this.Hh = He[14] | 0, this.Hl = He[15] | 0;
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: d, Fh: u, Fl: f, Gh: h, Gl: y, Hh: w, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, l, d, u, f, h, y, w, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, d, u, f, h, y, w, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = d | 0, this.Fh = u | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = y | 0, this.Hh = w | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let b = 0; b < 16; b++, n += 4)
      Bn[b] = t.getUint32(n), Tn[b] = t.getUint32(n += 4);
    for (let b = 16; b < 80; b++) {
      const O = Bn[b - 15] | 0, B = Tn[b - 15] | 0, R = Pr(O, B, 1) ^ Pr(O, B, 8) ^ Hu(O, B, 7), H = $r(O, B, 1) ^ $r(O, B, 8) ^ Nu(O, B, 7), L = Bn[b - 2] | 0, T = Tn[b - 2] | 0, D = Pr(L, T, 19) ^ Io(L, T, 61) ^ Hu(L, T, 6), z = $r(L, T, 19) ^ Bo(L, T, 61) ^ Nu(L, T, 6), E = hx(H, z, Tn[b - 7], Tn[b - 16]), x = px(E, R, D, Bn[b - 7], Bn[b - 16]);
      Bn[b] = x | 0, Tn[b] = E | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: d, Eh: u, El: f, Fh: h, Fl: y, Gh: w, Gl: p, Hh: g, Hl: k } = this;
    for (let b = 0; b < 80; b++) {
      const O = Pr(u, f, 14) ^ Pr(u, f, 18) ^ Io(u, f, 41), B = $r(u, f, 14) ^ $r(u, f, 18) ^ Bo(u, f, 41), R = u & h ^ ~u & w, H = f & y ^ ~f & p, L = yx(k, B, H, vx[b], Tn[b]), T = wx(L, g, O, R, bx[b], Bn[b]), D = L | 0, z = Pr(r, i, 28) ^ Io(r, i, 34) ^ Io(r, i, 39), E = $r(r, i, 28) ^ Bo(r, i, 34) ^ Bo(r, i, 39), x = r & o ^ r & a ^ o & a, v = i & s ^ i & c ^ s & c;
      g = w | 0, k = p | 0, w = h | 0, p = y | 0, h = u | 0, y = f | 0, { h: u, l: f } = Yt(l | 0, d | 0, T | 0, D | 0), l = a | 0, d = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const m = ux(D, E, v);
      r = fx(m, T, z, x), i = m | 0;
    }
    ({ h: r, l: i } = Yt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Yt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Yt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: d } = Yt(this.Dh | 0, this.Dl | 0, l | 0, d | 0), { h: u, l: f } = Yt(this.Eh | 0, this.El | 0, u | 0, f | 0), { h, l: y } = Yt(this.Fh | 0, this.Fl | 0, h | 0, y | 0), { h: w, l: p } = Yt(this.Gh | 0, this.Gl | 0, w | 0, p | 0), { h: g, l: k } = Yt(this.Hh | 0, this.Hl | 0, g | 0, k | 0), this.set(r, i, o, s, a, c, l, d, u, f, h, y, w, p, g, k);
  }
  roundClean() {
    hr(Bn, Tn);
  }
  destroy() {
    hr(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
const kx = /* @__PURE__ */ dy(() => new mx()), Ex = /* @__PURE__ */ dy(() => new xx());
/*! scure-bip39 - MIT License (c) 2022 Patricio Palladino, Paul Miller (paulmillr.com) */
const Sx = (e) => e[0] === "あいこくしん";
function yy(e) {
  if (typeof e != "string")
    throw new TypeError("invalid mnemonic type: " + typeof e);
  return e.normalize("NFKD");
}
function wy(e) {
  const t = yy(e), n = t.split(" ");
  if (![12, 15, 18, 21, 24].includes(n.length))
    throw new Error("Invalid mnemonic");
  return { nfkd: t, words: n };
}
function gy(e) {
  si(e, 16, 20, 24, 28, 32);
}
const Ax = (e) => {
  const t = 8 - e.length / 4;
  return new Uint8Array([kx(e)[0] >> t << t]);
};
function my(e) {
  if (!Array.isArray(e) || e.length !== 2048 || typeof e[0] != "string")
    throw new Error("Wordlist: expected array of 2048 strings");
  return e.forEach((t) => {
    if (typeof t != "string")
      throw new Error("wordlist: non-string element: " + t);
  }), Eo.chain(Eo.checksum(1, Ax), Eo.radix2(11, !0), Eo.alphabet(e));
}
function by(e, t) {
  const { words: n } = wy(e), r = my(t).decode(n);
  return gy(r), r;
}
function _x(e, t) {
  return gy(e), my(t).encode(e).join(Sx(t) ? "　" : " ");
}
function Ix(e, t) {
  try {
    by(e, t);
  } catch {
    return !1;
  }
  return !0;
}
const Bx = (e) => yy("mnemonic" + e);
function Tx(e, t = "") {
  return ox(Ex, wy(e).nfkd, Bx(t), { c: 2048, dkLen: 64 });
}
const ai = `abandon
ability
able
about
above
absent
absorb
abstract
absurd
abuse
access
accident
account
accuse
achieve
acid
acoustic
acquire
across
act
action
actor
actress
actual
adapt
add
addict
address
adjust
admit
adult
advance
advice
aerobic
affair
afford
afraid
again
age
agent
agree
ahead
aim
air
airport
aisle
alarm
album
alcohol
alert
alien
all
alley
allow
almost
alone
alpha
already
also
alter
always
amateur
amazing
among
amount
amused
analyst
anchor
ancient
anger
angle
angry
animal
ankle
announce
annual
another
answer
antenna
antique
anxiety
any
apart
apology
appear
apple
approve
april
arch
arctic
area
arena
argue
arm
armed
armor
army
around
arrange
arrest
arrive
arrow
art
artefact
artist
artwork
ask
aspect
assault
asset
assist
assume
asthma
athlete
atom
attack
attend
attitude
attract
auction
audit
august
aunt
author
auto
autumn
average
avocado
avoid
awake
aware
away
awesome
awful
awkward
axis
baby
bachelor
bacon
badge
bag
balance
balcony
ball
bamboo
banana
banner
bar
barely
bargain
barrel
base
basic
basket
battle
beach
bean
beauty
because
become
beef
before
begin
behave
behind
believe
below
belt
bench
benefit
best
betray
better
between
beyond
bicycle
bid
bike
bind
biology
bird
birth
bitter
black
blade
blame
blanket
blast
bleak
bless
blind
blood
blossom
blouse
blue
blur
blush
board
boat
body
boil
bomb
bone
bonus
book
boost
border
boring
borrow
boss
bottom
bounce
box
boy
bracket
brain
brand
brass
brave
bread
breeze
brick
bridge
brief
bright
bring
brisk
broccoli
broken
bronze
broom
brother
brown
brush
bubble
buddy
budget
buffalo
build
bulb
bulk
bullet
bundle
bunker
burden
burger
burst
bus
business
busy
butter
buyer
buzz
cabbage
cabin
cable
cactus
cage
cake
call
calm
camera
camp
can
canal
cancel
candy
cannon
canoe
canvas
canyon
capable
capital
captain
car
carbon
card
cargo
carpet
carry
cart
case
cash
casino
castle
casual
cat
catalog
catch
category
cattle
caught
cause
caution
cave
ceiling
celery
cement
census
century
cereal
certain
chair
chalk
champion
change
chaos
chapter
charge
chase
chat
cheap
check
cheese
chef
cherry
chest
chicken
chief
child
chimney
choice
choose
chronic
chuckle
chunk
churn
cigar
cinnamon
circle
citizen
city
civil
claim
clap
clarify
claw
clay
clean
clerk
clever
click
client
cliff
climb
clinic
clip
clock
clog
close
cloth
cloud
clown
club
clump
cluster
clutch
coach
coast
coconut
code
coffee
coil
coin
collect
color
column
combine
come
comfort
comic
common
company
concert
conduct
confirm
congress
connect
consider
control
convince
cook
cool
copper
copy
coral
core
corn
correct
cost
cotton
couch
country
couple
course
cousin
cover
coyote
crack
cradle
craft
cram
crane
crash
crater
crawl
crazy
cream
credit
creek
crew
cricket
crime
crisp
critic
crop
cross
crouch
crowd
crucial
cruel
cruise
crumble
crunch
crush
cry
crystal
cube
culture
cup
cupboard
curious
current
curtain
curve
cushion
custom
cute
cycle
dad
damage
damp
dance
danger
daring
dash
daughter
dawn
day
deal
debate
debris
decade
december
decide
decline
decorate
decrease
deer
defense
define
defy
degree
delay
deliver
demand
demise
denial
dentist
deny
depart
depend
deposit
depth
deputy
derive
describe
desert
design
desk
despair
destroy
detail
detect
develop
device
devote
diagram
dial
diamond
diary
dice
diesel
diet
differ
digital
dignity
dilemma
dinner
dinosaur
direct
dirt
disagree
discover
disease
dish
dismiss
disorder
display
distance
divert
divide
divorce
dizzy
doctor
document
dog
doll
dolphin
domain
donate
donkey
donor
door
dose
double
dove
draft
dragon
drama
drastic
draw
dream
dress
drift
drill
drink
drip
drive
drop
drum
dry
duck
dumb
dune
during
dust
dutch
duty
dwarf
dynamic
eager
eagle
early
earn
earth
easily
east
easy
echo
ecology
economy
edge
edit
educate
effort
egg
eight
either
elbow
elder
electric
elegant
element
elephant
elevator
elite
else
embark
embody
embrace
emerge
emotion
employ
empower
empty
enable
enact
end
endless
endorse
enemy
energy
enforce
engage
engine
enhance
enjoy
enlist
enough
enrich
enroll
ensure
enter
entire
entry
envelope
episode
equal
equip
era
erase
erode
erosion
error
erupt
escape
essay
essence
estate
eternal
ethics
evidence
evil
evoke
evolve
exact
example
excess
exchange
excite
exclude
excuse
execute
exercise
exhaust
exhibit
exile
exist
exit
exotic
expand
expect
expire
explain
expose
express
extend
extra
eye
eyebrow
fabric
face
faculty
fade
faint
faith
fall
false
fame
family
famous
fan
fancy
fantasy
farm
fashion
fat
fatal
father
fatigue
fault
favorite
feature
february
federal
fee
feed
feel
female
fence
festival
fetch
fever
few
fiber
fiction
field
figure
file
film
filter
final
find
fine
finger
finish
fire
firm
first
fiscal
fish
fit
fitness
fix
flag
flame
flash
flat
flavor
flee
flight
flip
float
flock
floor
flower
fluid
flush
fly
foam
focus
fog
foil
fold
follow
food
foot
force
forest
forget
fork
fortune
forum
forward
fossil
foster
found
fox
fragile
frame
frequent
fresh
friend
fringe
frog
front
frost
frown
frozen
fruit
fuel
fun
funny
furnace
fury
future
gadget
gain
galaxy
gallery
game
gap
garage
garbage
garden
garlic
garment
gas
gasp
gate
gather
gauge
gaze
general
genius
genre
gentle
genuine
gesture
ghost
giant
gift
giggle
ginger
giraffe
girl
give
glad
glance
glare
glass
glide
glimpse
globe
gloom
glory
glove
glow
glue
goat
goddess
gold
good
goose
gorilla
gospel
gossip
govern
gown
grab
grace
grain
grant
grape
grass
gravity
great
green
grid
grief
grit
grocery
group
grow
grunt
guard
guess
guide
guilt
guitar
gun
gym
habit
hair
half
hammer
hamster
hand
happy
harbor
hard
harsh
harvest
hat
have
hawk
hazard
head
health
heart
heavy
hedgehog
height
hello
helmet
help
hen
hero
hidden
high
hill
hint
hip
hire
history
hobby
hockey
hold
hole
holiday
hollow
home
honey
hood
hope
horn
horror
horse
hospital
host
hotel
hour
hover
hub
huge
human
humble
humor
hundred
hungry
hunt
hurdle
hurry
hurt
husband
hybrid
ice
icon
idea
identify
idle
ignore
ill
illegal
illness
image
imitate
immense
immune
impact
impose
improve
impulse
inch
include
income
increase
index
indicate
indoor
industry
infant
inflict
inform
inhale
inherit
initial
inject
injury
inmate
inner
innocent
input
inquiry
insane
insect
inside
inspire
install
intact
interest
into
invest
invite
involve
iron
island
isolate
issue
item
ivory
jacket
jaguar
jar
jazz
jealous
jeans
jelly
jewel
job
join
joke
journey
joy
judge
juice
jump
jungle
junior
junk
just
kangaroo
keen
keep
ketchup
key
kick
kid
kidney
kind
kingdom
kiss
kit
kitchen
kite
kitten
kiwi
knee
knife
knock
know
lab
label
labor
ladder
lady
lake
lamp
language
laptop
large
later
latin
laugh
laundry
lava
law
lawn
lawsuit
layer
lazy
leader
leaf
learn
leave
lecture
left
leg
legal
legend
leisure
lemon
lend
length
lens
leopard
lesson
letter
level
liar
liberty
library
license
life
lift
light
like
limb
limit
link
lion
liquid
list
little
live
lizard
load
loan
lobster
local
lock
logic
lonely
long
loop
lottery
loud
lounge
love
loyal
lucky
luggage
lumber
lunar
lunch
luxury
lyrics
machine
mad
magic
magnet
maid
mail
main
major
make
mammal
man
manage
mandate
mango
mansion
manual
maple
marble
march
margin
marine
market
marriage
mask
mass
master
match
material
math
matrix
matter
maximum
maze
meadow
mean
measure
meat
mechanic
medal
media
melody
melt
member
memory
mention
menu
mercy
merge
merit
merry
mesh
message
metal
method
middle
midnight
milk
million
mimic
mind
minimum
minor
minute
miracle
mirror
misery
miss
mistake
mix
mixed
mixture
mobile
model
modify
mom
moment
monitor
monkey
monster
month
moon
moral
more
morning
mosquito
mother
motion
motor
mountain
mouse
move
movie
much
muffin
mule
multiply
muscle
museum
mushroom
music
must
mutual
myself
mystery
myth
naive
name
napkin
narrow
nasty
nation
nature
near
neck
need
negative
neglect
neither
nephew
nerve
nest
net
network
neutral
never
news
next
nice
night
noble
noise
nominee
noodle
normal
north
nose
notable
note
nothing
notice
novel
now
nuclear
number
nurse
nut
oak
obey
object
oblige
obscure
observe
obtain
obvious
occur
ocean
october
odor
off
offer
office
often
oil
okay
old
olive
olympic
omit
once
one
onion
online
only
open
opera
opinion
oppose
option
orange
orbit
orchard
order
ordinary
organ
orient
original
orphan
ostrich
other
outdoor
outer
output
outside
oval
oven
over
own
owner
oxygen
oyster
ozone
pact
paddle
page
pair
palace
palm
panda
panel
panic
panther
paper
parade
parent
park
parrot
party
pass
patch
path
patient
patrol
pattern
pause
pave
payment
peace
peanut
pear
peasant
pelican
pen
penalty
pencil
people
pepper
perfect
permit
person
pet
phone
photo
phrase
physical
piano
picnic
picture
piece
pig
pigeon
pill
pilot
pink
pioneer
pipe
pistol
pitch
pizza
place
planet
plastic
plate
play
please
pledge
pluck
plug
plunge
poem
poet
point
polar
pole
police
pond
pony
pool
popular
portion
position
possible
post
potato
pottery
poverty
powder
power
practice
praise
predict
prefer
prepare
present
pretty
prevent
price
pride
primary
print
priority
prison
private
prize
problem
process
produce
profit
program
project
promote
proof
property
prosper
protect
proud
provide
public
pudding
pull
pulp
pulse
pumpkin
punch
pupil
puppy
purchase
purity
purpose
purse
push
put
puzzle
pyramid
quality
quantum
quarter
question
quick
quit
quiz
quote
rabbit
raccoon
race
rack
radar
radio
rail
rain
raise
rally
ramp
ranch
random
range
rapid
rare
rate
rather
raven
raw
razor
ready
real
reason
rebel
rebuild
recall
receive
recipe
record
recycle
reduce
reflect
reform
refuse
region
regret
regular
reject
relax
release
relief
rely
remain
remember
remind
remove
render
renew
rent
reopen
repair
repeat
replace
report
require
rescue
resemble
resist
resource
response
result
retire
retreat
return
reunion
reveal
review
reward
rhythm
rib
ribbon
rice
rich
ride
ridge
rifle
right
rigid
ring
riot
ripple
risk
ritual
rival
river
road
roast
robot
robust
rocket
romance
roof
rookie
room
rose
rotate
rough
round
route
royal
rubber
rude
rug
rule
run
runway
rural
sad
saddle
sadness
safe
sail
salad
salmon
salon
salt
salute
same
sample
sand
satisfy
satoshi
sauce
sausage
save
say
scale
scan
scare
scatter
scene
scheme
school
science
scissors
scorpion
scout
scrap
screen
script
scrub
sea
search
season
seat
second
secret
section
security
seed
seek
segment
select
sell
seminar
senior
sense
sentence
series
service
session
settle
setup
seven
shadow
shaft
shallow
share
shed
shell
sheriff
shield
shift
shine
ship
shiver
shock
shoe
shoot
shop
short
shoulder
shove
shrimp
shrug
shuffle
shy
sibling
sick
side
siege
sight
sign
silent
silk
silly
silver
similar
simple
since
sing
siren
sister
situate
six
size
skate
sketch
ski
skill
skin
skirt
skull
slab
slam
sleep
slender
slice
slide
slight
slim
slogan
slot
slow
slush
small
smart
smile
smoke
smooth
snack
snake
snap
sniff
snow
soap
soccer
social
sock
soda
soft
solar
soldier
solid
solution
solve
someone
song
soon
sorry
sort
soul
sound
soup
source
south
space
spare
spatial
spawn
speak
special
speed
spell
spend
sphere
spice
spider
spike
spin
spirit
split
spoil
sponsor
spoon
sport
spot
spray
spread
spring
spy
square
squeeze
squirrel
stable
stadium
staff
stage
stairs
stamp
stand
start
state
stay
steak
steel
stem
step
stereo
stick
still
sting
stock
stomach
stone
stool
story
stove
strategy
street
strike
strong
struggle
student
stuff
stumble
style
subject
submit
subway
success
such
sudden
suffer
sugar
suggest
suit
summer
sun
sunny
sunset
super
supply
supreme
sure
surface
surge
surprise
surround
survey
suspect
sustain
swallow
swamp
swap
swarm
swear
sweet
swift
swim
swing
switch
sword
symbol
symptom
syrup
system
table
tackle
tag
tail
talent
talk
tank
tape
target
task
taste
tattoo
taxi
teach
team
tell
ten
tenant
tennis
tent
term
test
text
thank
that
theme
then
theory
there
they
thing
this
thought
three
thrive
throw
thumb
thunder
ticket
tide
tiger
tilt
timber
time
tiny
tip
tired
tissue
title
toast
tobacco
today
toddler
toe
together
toilet
token
tomato
tomorrow
tone
tongue
tonight
tool
tooth
top
topic
topple
torch
tornado
tortoise
toss
total
tourist
toward
tower
town
toy
track
trade
traffic
tragic
train
transfer
trap
trash
travel
tray
treat
tree
trend
trial
tribe
trick
trigger
trim
trip
trophy
trouble
truck
true
truly
trumpet
trust
truth
try
tube
tuition
tumble
tuna
tunnel
turkey
turn
turtle
twelve
twenty
twice
twin
twist
two
type
typical
ugly
umbrella
unable
unaware
uncle
uncover
under
undo
unfair
unfold
unhappy
uniform
unique
unit
universe
unknown
unlock
until
unusual
unveil
update
upgrade
uphold
upon
upper
upset
urban
urge
usage
use
used
useful
useless
usual
utility
vacant
vacuum
vague
valid
valley
valve
van
vanish
vapor
various
vast
vault
vehicle
velvet
vendor
venture
venue
verb
verify
version
very
vessel
veteran
viable
vibrant
vicious
victory
video
view
village
vintage
violin
virtual
virus
visa
visit
visual
vital
vivid
vocal
voice
void
volcano
volume
vote
voyage
wage
wagon
wait
walk
wall
walnut
want
warfare
warm
warrior
wash
wasp
waste
water
wave
way
wealth
weapon
wear
weasel
weather
web
wedding
weekend
weird
welcome
west
wet
whale
what
wheat
wheel
when
where
whip
whisper
wide
width
wife
wild
will
win
window
wine
wing
wink
winner
winter
wire
wisdom
wise
wish
witness
wolf
woman
wonder
wood
wool
word
work
world
worry
worth
wrap
wreck
wrestle
wrist
write
wrong
yard
year
yellow
you
young
youth
zebra
zero
zone
zoo`.split(`
`), ms = "bitcoin-address-types-v1", Pl = 20, vy = new TextEncoder().encode(`avibe:protected-hd-wallet:v1
`), xy = [16, 20, 24, 28, 32], Px = ["account", "evm", "bitcoin", "bitcoin_p2pkh", "bitcoin_p2wpkh", "solana"];
function xt(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new Error("invalid protected HD object");
  return e;
}
function Ut(e, t) {
  if (Object.keys(e).length !== t.length || t.some((n) => !Object.hasOwn(e, n))) throw new Error("invalid protected HD fields");
}
function $l(e) {
  if (typeof e != "number" || !Number.isInteger(e) || e < 0 || e >= Pl) throw new Error("invalid protected HD account");
  return e;
}
function ky(e) {
  const t = xt(e), n = $l(t.account);
  if (t.chain === "bitcoin") {
    if (Ut(t, ["chain", "account", "address_type"]), t.address_type !== "p2pkh" && t.address_type !== "p2wpkh" && t.address_type !== "p2tr") throw new Error("invalid protected HD Bitcoin address type");
    return { chain: "bitcoin", account: n, address_type: t.address_type };
  }
  if (Ut(t, ["chain", "account"]), t.chain !== "evm" && t.chain !== "solana") throw new Error("invalid protected HD chain");
  return { chain: t.chain, account: n };
}
function bs(e, t, n = "p2tr") {
  if ($l(t), e === "evm") return `m/44'/60'/0'/0/${t}`;
  if (e === "bitcoin") {
    const r = n === "p2pkh" ? 44 : n === "p2wpkh" ? 84 : n === "p2tr" ? 86 : void 0;
    if (r === void 0) throw new Error("invalid protected HD Bitcoin address type");
    return `m/${r}'/0'/0'/0/${t}`;
  }
  if (e === "solana") return `m/44'/501'/${t}'/0'`;
  throw new Error("invalid protected HD chain");
}
function tn(e, t = !1) {
  if (typeof e != "string" || !(t ? /^[0-9a-f]{64}$/ : /^(02|03)[0-9a-f]{64}$/).test(e)) throw new Error("invalid protected HD public key");
  const n = qe(e);
  if (t ? !vt.utils.isValidPublicKey(n, !1) : !fe.utils.isValidPublicKey(n)) throw new Error("invalid protected HD public key");
  return e;
}
function ra(e) {
  const t = xt(e);
  if (Ut(t, ["kind", "profile", "accounts"]), t.kind !== "hd-seed" || t.profile !== ms || !Array.isArray(t.accounts) || t.accounts.length !== Pl) throw new Error("invalid protected HD profile/table");
  return { kind: "hd-seed", profile: ms, accounts: t.accounts.map((n, r) => {
    const i = xt(n);
    if (Ut(i, Px), $l(i.account) !== r) throw new Error("protected HD accounts must be ordered 0..19");
    return { account: r, evm: tn(i.evm), bitcoin: tn(i.bitcoin), solana: tn(i.solana, !0), bitcoin_p2pkh: tn(i.bitcoin_p2pkh), bitcoin_p2wpkh: tn(i.bitcoin_p2wpkh) };
  }) };
}
function $x(e) {
  if (!xy.includes(e.entropy.length) || e.seed.length !== 64 || typeof e.passphraseRequired != "boolean") throw new Error("invalid protected HD payload");
  const t = new Uint8Array(95 + e.entropy.length);
  return t.set(vy), t[29] = e.entropy.length, t.set(e.entropy, 30), t.set(e.seed, 30 + e.entropy.length), t[t.length - 1] = Number(e.passphraseRequired), t;
}
function Ey(e) {
  const t = e[29];
  if (!xy.includes(t) || e.length !== 95 + t || vy.some((n, r) => n !== e[r]) || ![0, 1].includes(e[e.length - 1])) throw new Error("invalid protected HD payload");
  return { entropy: e.slice(30, 30 + t), seed: e.slice(30 + t, 94 + t), passphraseRequired: e[e.length - 1] === 1 };
}
function vs(e) {
  e?.entropy.fill(0), e?.seed.fill(0);
}
function Sy(e) {
  return e.normalize("NFKD").toLowerCase().trim().split(/\s+/u).filter(Boolean).join(" ");
}
function Ox(e) {
  return _x(e, ai);
}
function Ay(e, t) {
  const n = Sy(e), r = by(n, ai);
  try {
    return { entropy: r, seed: Tx(n, t), passphraseRequired: t.normalize("NFKD").length > 0 };
  } catch (i) {
    throw r.fill(0), i;
  }
}
function _y(e, t) {
  let n = vc(wc, new TextEncoder().encode("ed25519 seed"), e);
  try {
    for (const r of [44, 501, t, 0]) {
      const i = new Uint8Array(37);
      i.set(n.subarray(0, 32), 1), new DataView(i.buffer).setUint32(33, 2147483648 + r, !1);
      let o;
      try {
        o = vc(wc, n.subarray(32), i);
      } finally {
        i.fill(0);
      }
      n.fill(0), n = o;
    }
    return n.slice(0, 32);
  } finally {
    n.fill(0);
  }
}
function Cx(e, t, n) {
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  const r = Rn.fromMasterSeed(e);
  let i, o;
  try {
    if (i = r.derive(t), !i.privateKey) throw new Error("protected HD derivation failed");
    return o = new Uint8Array(i.privateKey), n(o);
  } finally {
    o?.fill(0), i?.wipePrivateData(), r.wipePrivateData();
  }
}
function Uu(e, t, n) {
  const r = ky(t);
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  if (r.chain !== "solana") return Cx(e, bs(r.chain, r.account, r.chain === "bitcoin" ? r.address_type : void 0), n);
  const i = _y(e, r.account);
  try {
    return n(i);
  } finally {
    i.fill(0);
  }
}
function Tc(e) {
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  const t = Rn.fromMasterSeed(e);
  try {
    return Array.from({ length: Pl }, (n, r) => {
      const i = (s) => {
        const a = t.derive(s);
        try {
          if (!a.publicKey) throw new Error("protected HD derivation failed");
          return G(a.publicKey);
        } finally {
          a.wipePrivateData();
        }
      }, o = _y(e, r);
      try {
        return { account: r, evm: i(bs("evm", r)), bitcoin: i(bs("bitcoin", r)), solana: G(vt.getPublicKey(o)), bitcoin_p2pkh: i(`m/44'/0'/0'/0/${r}`), bitcoin_p2wpkh: i(`m/84'/0'/0'/0/${r}`) };
      } finally {
        o.fill(0);
      }
    });
  } finally {
    t.wipePrivateData();
  }
}
function Ol(e) {
  const t = qe(tn(e.evm)), n = G(cs(fe.Point.fromBytes(t).toBytes(!1).slice(1)).slice(-20)), r = G(cs(new TextEncoder().encode(n))), i = (l) => yl(ue(qe(tn(l)))), o = qe(tn(e.bitcoin)).slice(1), s = BigInt(`0x${G(On.utils.taggedHash("TapTweak", o))}`);
  if (s >= fe.Point.Fn.ORDER) throw new Error("invalid protected HD Taproot tweak");
  const a = fe.Point.fromBytes(new Uint8Array([2, ...o])), c = s === 0n ? a : a.add(fe.Point.BASE.multiply(s));
  if (c.equals(fe.Point.ZERO)) throw new Error("invalid protected HD Taproot output");
  return {
    eth: "0x" + [...n].map((l, d) => parseInt(r[d], 16) >= 8 ? l.toUpperCase() : l).join(""),
    btc_legacy: Z1(ue).encode(new Uint8Array([0, ...i(e.bitcoin_p2pkh)])),
    btc_segwit: $u.encode("bc", [0, ...$u.toWords(i(e.bitcoin_p2wpkh))]),
    btc_taproot: Ou.encode("bc", [1, ...Ou.toWords(c.toBytes(!0).slice(1))]),
    solana: iy.encode(qe(tn(e.solana, !0)))
  };
}
function Iy(e) {
  const t = JSON.stringify([e.ciphertext, e.nonce, e.wrap_meta]).replace(/[\u007f-\uffff]/g, (n) => "\\u" + n.charCodeAt(0).toString(16).padStart(4, "0"));
  return G(ue(new TextEncoder().encode(t)));
}
function xs(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 65536 || new TextEncoder().encode(e).length > 65536) throw new Error("invalid protected HD wrap_meta size/type");
  let t;
  try {
    t = JSON.parse(e);
  } catch {
    throw new Error("invalid protected HD wrap_meta JSON");
  }
  const n = (i) => {
    for (let o = 0; o < i.length; o++) {
      const s = i.charCodeAt(o);
      if (s >= 55296 && s <= 56319) {
        const a = i.charCodeAt(++o);
        if (!(a >= 56320 && a <= 57343)) throw new Error("invalid protected HD Unicode");
      } else if (s >= 56320 && s <= 57343) throw new Error("invalid protected HD Unicode");
    }
  }, r = (i, o) => {
    if (typeof i == "string") n(i);
    else {
      if (typeof i == "number" && !Number.isFinite(i)) throw new Error("invalid protected HD JSON number");
      if (i && typeof i == "object") {
        if (o > 64) throw new Error("protected HD JSON is too deep");
        for (const [s, a] of Object.entries(i))
          n(s), r(a, o + 1);
      }
    }
  };
  return r(t, 1), xt(t);
}
function ir(e, t) {
  if (typeof e != "string" || e.length > Math.ceil(Math.max(...t) / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) throw new Error("invalid protected HD base64");
  const n = Uint8Array.from(atob(e), (r) => r.charCodeAt(0));
  if (!t.includes(n.length) || btoa(String.fromCharCode(...n)) !== e) throw new Error("invalid protected HD field length/encoding");
  return n;
}
function Rx(e, t) {
  if (t.v !== 1 || t.scheme !== "machine-aesgcm-v1") throw new Error("invalid protected HD envelope version/scheme");
  ir(e.nonce, [12]), ir(e.ciphertext, [127, 131, 135, 139, 143]), ir(t.dek_nonce, [12]), ir(t.wrapped_dek, [48]);
}
const Du = { '"': 34, "\\": 92, "/": 47, b: 8, f: 12, n: 10, r: 13, t: 9 };
function Lx(e) {
  if (typeof e != "string") return !1;
  let t = 0;
  const n = () => {
    for (; t < e.length && /[\x20\t\r\n]/.test(e[t]); ) t += 1;
  }, r = (a) => {
    if (e[t++] !== '"') return !1;
    let c = 0, l = a !== void 0;
    for (; t < e.length; ) {
      let d = e.charCodeAt(t++);
      if (d === 34) return l && c === a.length;
      if (d === 92) {
        if (t >= e.length) return !1;
        const u = e[t++];
        if (u === "u") {
          const f = e.slice(t, t + 4);
          if (f.length !== 4 || !/^[0-9a-fA-F]{4}$/.test(f)) return !1;
          d = Number.parseInt(f, 16), t += 4;
        } else {
          if (!Object.hasOwn(Du, u)) return !1;
          d = Du[u];
        }
      }
      (a === void 0 || c >= a.length || d !== a.charCodeAt(c)) && (l = !1), c += 1;
    }
    return !1;
  }, i = () => {
    if (n(), e[t] === '"') {
      r();
      return;
    }
    if (e[t] !== "{" && e[t] !== "[") {
      for (; t < e.length && !/[,}\]\x20\t\r\n]/.test(e[t]); ) t += 1;
      return;
    }
    let a = 0;
    for (; t < e.length; ) {
      const c = e[t];
      if (c === '"') {
        r();
        continue;
      }
      if (t += 1, (c === "{" || c === "[") && (a += 1), (c === "}" || c === "]") && (a -= 1, a === 0))
        return;
    }
  }, o = () => {
    if (n(), e[t] !== "{")
      return i(), !1;
    t += 1;
    let a = !1;
    for (; ; ) {
      if (n(), e[t] === "}")
        return t += 1, a;
      if (e[t] !== '"') return !1;
      const c = r("kind");
      if (n(), e[t++] !== ":") return !1;
      if (n(), c && e[t] === '"' ? a = r("hd-seed") : (i(), c && (a = !1)), n(), e[t] === "}")
        return t += 1, a;
      if (e[t++] !== ",") return !1;
    }
  };
  if (n(), e[t++] !== "{") return !1;
  let s = !1;
  for (; ; ) {
    if (n(), e[t] === "}") return s;
    if (e[t] !== '"') return !1;
    const a = r("record_meta");
    if (n(), e[t++] !== ":") return !1;
    if (a ? s = o() : i(), n(), e[t] === "}") return s;
    if (e[t++] !== ",") return !1;
  }
}
const Hx = "hpke-x25519-hkdfsha256-aes256gcm-v1", Nx = "avault:blind-box:v1", Ux = "avault:blind-box:aad:v1", ks = "machine-aesgcm-v1", ia = 1, oa = "ecdsa-secp256k1-recoverable", Ui = "ed25519", Es = "ecdsa-secp256k1-der", Cl = "schnorr-secp256k1-bip340", Di = "schnorr-secp256k1-bip341-keypath", ke = 32, Ss = 12, sa = 32, Dx = "avault:protected-vmk:kek-passkey:v1", zx = /^[A-Za-z_][A-Za-z0-9_]*$/, Mx = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[a-z][a-z0-9_]{0,63})?$/, Kx = "avault:protected-root-meta:v1", Fx = new TextEncoder(), jx = new TextDecoder();
function Rl(e) {
  switch (e) {
    case Ui:
    case oa:
    case Es:
    case Cl:
    case Di:
      return e;
    default:
      throw new Error("unsupported signing scheme");
  }
}
function ci(e) {
  if (e === void 0 || e === "secp256k1") return "secp256k1";
  if (e === "ed25519") return e;
  throw new Error("unsupported signing curve");
}
function Ll(e) {
  if (Rl(e), e === Ui) throw new Error("Ed25519 requires exact-message signing, not a digest");
}
function Se(e, t = "bytes") {
  if (e instanceof Uint8Array)
    return new Uint8Array(e);
  if (e instanceof ArrayBuffer)
    return new Uint8Array(e.slice(0));
  if (ArrayBuffer.isView(e))
    return new Uint8Array(e.buffer.slice(e.byteOffset, e.byteOffset + e.byteLength));
  if (typeof e == "object" && e !== null && typeof e.length == "number")
    return Uint8Array.from(e);
  throw new TypeError(`${t} must be bytes`);
}
function rt(e) {
  const t = Se(e), n = new ArrayBuffer(t.byteLength);
  return new Uint8Array(n).set(t), t.fill(0), n;
}
function je(e) {
  return Fx.encode(e);
}
function As(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(As).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${As(t[n])}`).join(",")}}`;
}
function ur(e) {
  const t = new Uint8Array(e);
  return crypto.getRandomValues(t), t;
}
function le(e) {
  const t = Se(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return t.fill(0), btoa(n);
}
function Ee(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = t.padEnd(Math.ceil(t.length / 4) * 4, "=");
  return Uint8Array.from(atob(n), (r) => r.charCodeAt(0));
}
function ut(e) {
  return qe(e.startsWith("0x") ? e.slice(2) : e);
}
function zu(e) {
  return /^(0x)?[0-9a-fA-F]+$/.test(e) && (e.startsWith("0x"), e.length % 2 === 0) ? ut(e) : Ee(e);
}
function Vx(e) {
  return G(Se(e));
}
function By(e, t = "bytes") {
  return Se(e, t);
}
function Oe(e, t, n) {
  if (e.length !== t)
    throw new Error(`${n} must be ${t} bytes`);
}
function mt(e) {
  e && new Uint8Array(e).fill(0);
}
async function _s(e, t, n, r) {
  let i, o, s, a;
  try {
    i = rt(e), o = rt(t), s = rt(n), a = r ? rt(r) : void 0;
    const c = await crypto.subtle.importKey("raw", i, "AES-GCM", !1, ["encrypt"]), l = await crypto.subtle.encrypt(
      {
        name: "AES-GCM",
        iv: o,
        ...a ? { additionalData: a } : {}
      },
      c,
      s
    );
    return new Uint8Array(l);
  } finally {
    mt(i), mt(o), mt(s), mt(a);
  }
}
async function aa(e, t, n, r) {
  let i, o, s, a;
  try {
    i = rt(e), o = rt(t), s = rt(n), a = r ? rt(r) : void 0;
    const c = await crypto.subtle.importKey("raw", i, "AES-GCM", !1, ["decrypt"]), l = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: o,
        ...a ? { additionalData: a } : {}
      },
      c,
      s
    );
    return new Uint8Array(l);
  } finally {
    mt(i), mt(o), mt(s), mt(a);
  }
}
async function qx(e, t, n, r) {
  let i, o, s;
  try {
    i = rt(e), o = rt(t), s = rt(n);
    const a = await crypto.subtle.importKey("raw", i, "HKDF", !1, ["deriveBits"]), c = await crypto.subtle.deriveBits(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt: o,
        info: s
      },
      a,
      r * 8
    );
    return new Uint8Array(c);
  } finally {
    mt(i), mt(o), mt(s);
  }
}
async function Ty(e, t) {
  const n = Se(e, "passkey PRF output");
  try {
    const r = Se(t, "passkey PRF salt");
    try {
      if (n.length === 0)
        throw new Error("passkey PRF output is empty");
      return Oe(r, sa, "passkey PRF salt"), await qx(n, r, je(Dx), ke);
    } finally {
      r.fill(0);
    }
  } finally {
    n.fill(0);
  }
}
function Wx() {
  return ur(ke);
}
function Gx() {
  return ur(sa);
}
function Zx(e) {
  const t = Se(e, "passkey PRF salt");
  try {
    return Oe(t, sa, "passkey PRF salt"), { prf: { eval: { first: rt(t) } } };
  } finally {
    t.fill(0);
  }
}
function At(e) {
  const t = typeof e == "string" ? JSON.parse(e) : e;
  if (t?.v !== ia || !Array.isArray(t.copies))
    throw new Error("invalid protected wrap_meta");
  return t;
}
async function Yx(e, t) {
  const n = Se(e, "VMK"), r = ur(Ss);
  let i, o;
  try {
    Oe(n, ke, "VMK"), o = Se(t.prfSalt, "passkey PRF salt"), Oe(o, sa, "passkey PRF salt"), i = await Ty(t.prfOutput, o);
    const s = await _s(i, r, n);
    return {
      kind: "passkey",
      kdf: "webauthn-prf-hkdf-sha256",
      prf_salt: le(o),
      nonce: le(r),
      wrapped: le(s),
      ...t.credentialId ? { credential_id: t.credentialId } : {}
    };
  } finally {
    i?.fill(0), o?.fill(0), n.fill(0);
  }
}
async function Xx(e, t) {
  const n = Se(e, "VMK");
  try {
    if (Oe(n, ke, "VMK"), t.length === 0)
      throw new Error("at least one protected unlock factor is required");
    const r = [];
    for (const i of t)
      r.push(await Yx(n, i));
    return JSON.stringify({ v: ia, copies: r });
  } finally {
    n.fill(0);
  }
}
async function Jx(e, t) {
  const n = Ee(e.prf_salt);
  if (t.prfSalt && le(t.prfSalt) !== e.prf_salt)
    throw n.fill(0), new Error("passkey PRF salt does not match copy");
  const r = await Ty(t.prfOutput, n);
  try {
    return await aa(r, Ee(e.nonce), Ee(e.wrapped));
  } finally {
    r.fill(0), n.fill(0);
  }
}
async function Py(e, t) {
  const n = At(e);
  for (const r of n.copies)
    try {
      if (r.kind === "passkey") {
        const i = await Jx(r, t);
        try {
          return Oe(i, ke, "VMK"), i;
        } catch (o) {
          throw i.fill(0), o;
        }
      }
    } catch {
    }
  throw new Error("no protected VMK copy could be unwrapped");
}
function ca(e) {
  return At(e).copies.filter((t) => t.kind === "passkey").map((t) => ({
    prfSalt: Ee(t.prf_salt),
    ...t.credential_id ? { credentialId: t.credential_id } : {}
  }));
}
function Qx(e, t) {
  const n = At(e);
  return JSON.stringify({
    ...n,
    rp_id: t.rpId,
    vault_user_handle: t.vaultUserHandle
  });
}
function $y(e) {
  const t = At(e);
  return delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, JSON.stringify(t);
}
function Hl(e) {
  if (e.kind === "hd-seed") {
    if (e.publicKey !== void 0 || e.curve !== void 0 || !e.hd) throw new Error("invalid protected HD context");
    return ra(e.hd);
  }
  if (e.hd !== void 0) throw new Error("protected HD table requires HD metadata");
  if (!e.kind) {
    if (e.curve !== void 0) throw new Error("protected record curve requires keypair metadata");
    return;
  }
  const t = ci(e.curve);
  if (e.kind === "static") {
    if (e.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (e.kind === "keypair") {
    if (typeof e.publicKey != "string" || e.publicKey.length === 0)
      throw new Error("keypair protected record requires a public key");
    return t === "ed25519" && Fl(e.publicKey), {
      kind: "keypair",
      public_key: e.publicKey.toLowerCase(),
      ...t === "ed25519" ? { curve: t } : {}
    };
  }
  throw new Error("unsupported protected record kind");
}
function e2(e) {
  if (e === void 0) return;
  if (typeof e != "object" || e === null)
    throw new Error("protected record metadata is invalid");
  const t = e;
  if (t.kind === "hd-seed") return ra(t);
  if (t.curve !== void 0 && t.curve !== "ed25519")
    throw new Error("protected record metadata curve is invalid");
  if (t.kind === "static") {
    if (t.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (t.kind === "keypair")
    return Hl({ kind: "keypair", publicKey: t.public_key, curve: t.curve });
  throw new Error("protected record metadata kind is invalid");
}
function la(e, t) {
  return t?.kind === "hd-seed" ? { name: e, kind: "hd-seed", hd: ra(t) } : {
    name: e,
    ...t ? { kind: t.kind } : {},
    ...t?.public_key ? { publicKey: t.public_key } : {},
    ...t?.curve ? { curve: t.curve } : {}
  };
}
function Nl(e) {
  return je(
    As({
      domain: "avault:protected-record:aad:v2",
      name: e.kind === "keypair" || e.kind === "hd-seed" ? Ly(e.name) : Jr(e.name),
      scheme: e.scheme ?? ks,
      version: e.version ?? ia,
      record: Hl(e) ?? null
    })
  );
}
async function Ul(e, t, n) {
  const r = Se(t, "VMK"), i = ur(ke);
  let o;
  try {
    Oe(r, ke, "VMK"), o = Se(e, "value");
    const s = Nl(n), a = ur(Ss), c = await _s(i, a, o, s), l = ur(Ss), d = await _s(r, l, i, s);
    return {
      ciphertext: le(c),
      nonce: le(a),
      dek_nonce: le(l),
      wrapped_dek: le(d)
    };
  } finally {
    i.fill(0), r.fill(0), o?.fill(0);
  }
}
async function Oy(e, t, n) {
  const r = Se(t, "VMK");
  try {
    Oe(r, ke, "VMK");
    const i = await aa(
      r,
      Ee(e.dek_nonce),
      Ee(e.wrapped_dek),
      Nl(n)
    );
    try {
      return Oe(i, ke, "DEK"), i;
    } catch (o) {
      throw i.fill(0), o;
    }
  } finally {
    r.fill(0);
  }
}
async function li(e, t, n) {
  const r = await Oy(e, t, n);
  try {
    return await aa(
      r,
      Ee(e.nonce),
      Ee(e.ciphertext),
      Nl(n)
    );
  } finally {
    r.fill(0);
  }
}
function Dl(e, t, n) {
  const r = At(t);
  if ("dek_nonce" in r || "wrapped_dek" in r)
    throw new Error("vmk wrap_meta must not already carry a wrapped DEK");
  if ("scheme" in r && r.scheme !== ks)
    throw new Error("protected wrap_meta has unsupported scheme");
  const i = n ? Hl(n) : void 0;
  return {
    ciphertext: e.ciphertext,
    nonce: e.nonce,
    wrap_meta: JSON.stringify({
      ...r,
      scheme: ks,
      ...i ? { record_meta: i } : {},
      dek_nonce: e.dek_nonce,
      wrapped_dek: e.wrapped_dek
    })
  };
}
function jn(e) {
  const t = Lx(e.wrap_meta), n = At(t ? xs(e.wrap_meta) : e.wrap_meta);
  if (n.record_meta?.kind === "hd-seed") {
    const c = t ? n : xs(e.wrap_meta);
    Rx(e, c);
  }
  const { dek_nonce: r, wrapped_dek: i, record_meta: o, ...s } = n;
  if (typeof r != "string" || typeof i != "string")
    throw new Error("protected record wrap_meta is missing the wrapped DEK");
  const a = e2(o);
  return {
    sealed: { ciphertext: e.ciphertext, nonce: e.nonce, dek_nonce: r, wrapped_dek: i },
    vmkWrapMeta: JSON.stringify(s),
    ...a ? { recordMetadata: a } : {}
  };
}
function Cy(e) {
  const t = { ...At(e) };
  return delete t.root_meta, delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, delete t.scheme, je(As({ domain: Kx, vault: t }));
}
async function t2(e, t, n) {
  const r = ur(Ss), i = je(JSON.stringify(t));
  try {
    const o = await _s(e, r, i, Cy(n));
    return { v: 1, nonce: le(r), ciphertext: le(o) };
  } finally {
    i.fill(0);
  }
}
async function Is(e, t) {
  const n = At(e).root_meta;
  if (!n) return null;
  if (n.v !== 1 || typeof n.nonce != "string" || typeof n.ciphertext != "string")
    throw new Error("invalid protected root metadata");
  const r = await aa(t, Ee(n.nonce), Ee(n.ciphertext), Cy(e));
  try {
    const i = JSON.parse(jx.decode(r));
    if (typeof i != "object" || i === null) throw new Error("invalid protected root metadata");
    return i;
  } finally {
    r.fill(0);
  }
}
function n2(e, t) {
  const n = At(e);
  return JSON.stringify({ ...n, root_meta: t });
}
function r2() {
  return new t0({
    kem: new u0(),
    kdf: new n0(),
    aead: new Nb()
  });
}
function zl(e) {
  return Ee(typeof e == "string" ? e : e.public_key);
}
async function Ry(e) {
  const t = await crypto.subtle.digest("SHA-256", rt(e));
  return G(new Uint8Array(t));
}
async function i2(e) {
  return Ry(zl(e));
}
function o2(e) {
  if (!e.fingerprint)
    throw new Error("protected DEK release requires a pinned avault public key fingerprint");
  return e;
}
async function s2(e) {
  const t = o2(e), n = zl(t);
  if (Oe(n, ke, "avault public key"), await Ry(n) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  return t;
}
async function a2(e, t, n) {
  const r = r2(), i = zl(t);
  if (Oe(i, ke, "avault public key"), typeof t != "string" && t.fingerprint && await i2(t) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  const o = await r.kem.deserializePublicKey(i), s = typeof e == "string" ? je(e) : Se(e, "plaintext");
  try {
    const a = await r.seal(
      { recipientPublicKey: o, info: je(Nx) },
      s,
      y2(n)
    );
    return {
      scheme: Hx,
      enc: le(new Uint8Array(a.enc)),
      ct: le(new Uint8Array(a.ct))
    };
  } finally {
    s.fill(0);
  }
}
function Ly(e) {
  if (typeof e != "string" || !zx.test(e))
    throw new Error("vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$");
  return e;
}
function Jr(e) {
  if (typeof e != "string" || !Mx.test(e))
    throw new Error("vault record name must match ^[A-Za-z_][A-Za-z0-9_]*(\\.[a-z][a-z0-9_]{0,63})?$");
  return e;
}
function Mu(e, t) {
  if (typeof e != "string" || e.trim() === "")
    throw new Error(`${t} is required`);
  return e;
}
function Ml(e) {
  const t = typeof e == "string" ? ut(e) : Se(e, "digest");
  return Oe(t, ke, "digest"), t;
}
function c2(e) {
  const t = typeof e == "string" ? ut(e) : Se(e, "private key");
  try {
    if (Oe(t, ke, "private key"), !fe.utils.isValidSecretKey(t))
      throw new Error("invalid secp256k1 private key");
    return t;
  } catch (n) {
    throw t.fill(0), n;
  }
}
function Bs(e, t) {
  return typeof e == "string" ? ut(e) : Se(e, t);
}
function Ts(e) {
  const t = Bs(e, "operation hash");
  return Oe(t, ke, "operation hash"), t;
}
function Ps(e, t) {
  if (typeof e == "number" && (!Number.isSafeInteger(e) || e < 0))
    throw new Error(`${t} out of bounds`);
  const n = typeof e == "bigint" ? e : BigInt(e);
  if (n < 0n || n > 0xffffffffffffffffn)
    throw new Error(`${t} out of bounds`);
  return n;
}
function Hy(e, t) {
  const n = Ps(e, t), r = new Uint8Array(8);
  return new DataView(r.buffer).setBigUint64(0, n, !1), r;
}
function l2(e) {
  return Hy(e, "approval expiry");
}
function Ku(e) {
  const t = Se(e.nonce, "approval nonce");
  if (t.length < 16 || t.length > 128)
    throw new Error("approval nonce must be 16..128 bytes");
  return { approvalNonce: t, approvalExpiresAtUnix: Ps(e.expiresAtUnix, "approval expiry") };
}
function at(e, t) {
  const n = Se(t);
  if (n.length > 4294967295)
    throw new Error("blind-box AAD field is too large");
  e.push(n.length >>> 24 & 255, n.length >>> 16 & 255, n.length >>> 8 & 255, n.length & 255), e.push(...n);
}
function d2(e) {
  switch (e.purpose) {
    case "seal":
      return u2(e.name);
    case "agent-deliver": {
      const t = Ku({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-deliver",
        name: Jr(e.name),
        grantId: Mu(e.grantId, "grant id"),
        ttlSecs: Ps(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: Ts(e.operationHash)
      };
    }
    case "agent-sign": {
      Ll(e.signScheme);
      const t = Ku({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-sign",
        name: Ly(e.name),
        grantId: Mu(e.grantId, "grant id"),
        signScheme: e.signScheme,
        digest: Ml(e.digest),
        ttlSecs: Ps(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: Ts(e.operationHash)
      };
    }
    default:
      throw new Error("unsupported blind-box context purpose");
  }
}
function u2(e) {
  return { purpose: "seal", name: Jr(e) };
}
async function f2(e) {
  const t = Ts(e.operationHash), n = await p2(e.name, e.ttlSecs);
  if (G(t) !== G(n))
    throw new Error("operation hash does not match release operation fields");
}
async function h2(e) {
  const t = [];
  for (const n of e)
    at(t, typeof n == "string" ? je(n) : Se(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(t)));
}
async function p2(e, t) {
  return h2(["agent-deliver", Jr(e), Hy(t, "ttl seconds")]);
}
function y2(e) {
  const t = d2(e), n = [...je(Ux)];
  return at(n, je(t.purpose)), at(n, je(t.name)), at(n, je(ks)), at(n, new Uint8Array([ia])), at(n, t.purpose === "agent-deliver" || t.purpose === "agent-sign" ? je(t.grantId) : new Uint8Array()), at(n, new Uint8Array()), at(n, t.purpose === "agent-sign" ? je(t.signScheme) : new Uint8Array()), at(n, t.purpose === "agent-sign" ? Ml(t.digest) : new Uint8Array()), at(n, t.purpose === "seal" ? new Uint8Array() : Se(t.approvalNonce, "approval nonce")), at(n, t.purpose === "seal" ? new Uint8Array() : l2(t.approvalExpiresAtUnix)), at(n, t.purpose === "seal" ? new Uint8Array() : Ts(t.operationHash)), new Uint8Array(n);
}
async function w2(e, t, n, r, i) {
  if (r.kind !== void 0 && r.kind !== "static")
    throw new Error("protected DEK release requires a static protected record");
  if (Jr(r.name) !== Jr(i.name))
    throw new Error("protected DEK release context name does not match record name");
  await f2(i);
  const o = await s2(n), s = await Oy(e, t, r);
  try {
    return await a2(s, o, i);
  } finally {
    s.fill(0);
  }
}
function g2(e = "secp256k1") {
  if (ci(e) === "ed25519") {
    const n = vt.utils.randomSecretKey();
    try {
      return { privateKey: n, publicKey: G(vt.getPublicKey(n)) };
    } catch (r) {
      throw n.fill(0), r;
    }
  }
  const t = fe.utils.randomSecretKey();
  return { privateKey: t, publicKey: G(fe.getPublicKey(t, !0)) };
}
function Fu(e) {
  return ut(e.toString(16).padStart(ke * 2, "0"));
}
function Kl(e, t, n, r = {}) {
  if (Ll(n), !r || typeof r != "object" || Array.isArray(r) || Object.keys(r).some((s) => s !== "schnorrAuxRand"))
    throw new Error("unsupported signing option; script trees are not supported");
  const i = c2(e);
  let o;
  try {
    const s = Ml(t);
    if (n === oa) {
      const a = fe.sign(s, i, { prehash: !1, lowS: !0, format: "recovered" });
      return {
        scheme: n,
        signature: G(a.slice(1)),
        recovery_id: a[0] ?? null
      };
    }
    if (n === Es)
      return {
        scheme: n,
        signature: G(fe.sign(s, i, { prehash: !1, lowS: !0, format: "der" })),
        recovery_id: null
      };
    if (n === Cl) {
      o = r.schnorrAuxRand !== void 0 ? Bs(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(ke)), Oe(o, ke, "schnorr aux randomness");
      const a = On.sign(s, i, o);
      if (!On.verify(a, s, On.getPublicKey(i))) throw new Error("signing self-verification failed");
      return { scheme: n, signature: G(a), recovery_id: null };
    }
    if (n === Di) {
      const a = fe.getPublicKey(i, !0), c = jl(a.slice(1));
      let l = BigInt(`0x${G(i)}`);
      a[0] === 3 && (l = Ai - l);
      const d = BigInt(`0x${G(On.utils.taggedHash("TapTweak", a.slice(1)))}`);
      if (d >= Ai) throw new Error("invalid taproot tweak");
      let u = (l + d) % Ai;
      if (u === 0n) throw new Error("invalid taproot signing key");
      let f = Fu(u);
      try {
        c.toBytes(!0)[0] === 3 && (u = Ai - u, f.fill(0), f = Fu(u)), o = r.schnorrAuxRand !== void 0 ? Bs(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(ke)), Oe(o, ke, "schnorr aux randomness");
        const h = On.sign(s, f, o);
        if (!On.verify(h, s, c.toBytes(!0).slice(1))) throw new Error("Taproot signing self-verification failed");
        return { scheme: n, signature: G(h), recovery_id: null };
      } finally {
        f.fill(0);
      }
    }
    throw new Error("unsupported signing scheme");
  } finally {
    o?.fill(0), i.fill(0);
  }
}
async function m2(e, t, n, r, i, o = {}) {
  if (n.kind !== "keypair" || !n.publicKey)
    throw new Error("protected signing requires a keypair protected record");
  if (Ll(i), ci(n.curve) !== "secp256k1") throw new Error("digest signing requires a secp256k1 protected record");
  const s = await li(e, t, n);
  try {
    if (G(fe.getPublicKey(s, !0)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return Kl(s, r, i, o);
  } finally {
    s.fill(0);
  }
}
function Ny(e, t) {
  const n = wl(t), r = Bs(e, "Ed25519 seed");
  try {
    Oe(r, ke, "Ed25519 seed");
    const i = vt.sign(n, r);
    if (!vt.verify(i, n, vt.getPublicKey(r), { zip215: !1 }))
      throw new Error("Ed25519 signing self-verification failed");
    return { scheme: Ui, signature: G(i), recovery_id: null };
  } finally {
    r.fill(0);
  }
}
function Fl(e) {
  if (!/^[0-9a-fA-F]{64}$/.test(e)) throw new Error("expected a 32-byte Ed25519 public key");
  const t = ut(e);
  if (!vt.utils.isValidPublicKey(t, !1)) throw new Error("invalid Ed25519 public key");
  return t;
}
async function b2(e, t, n, r) {
  if (n.kind !== "keypair" || n.curve !== "ed25519" || !n.publicKey)
    throw new Error("exact-message signing requires an Ed25519 keypair protected record");
  const i = wl(r);
  if (i.length === 0) throw new Error("protected Ed25519 message must not be empty");
  Fl(n.publicKey);
  const o = await li(e, t, n);
  try {
    if (Oe(o, ke, "Ed25519 seed"), G(vt.getPublicKey(o)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return Ny(o, i);
  } finally {
    o.fill(0);
  }
}
function v2(e) {
  return zy(Fl(e));
}
function x2(e) {
  const t = e.rootMetadata?.daemon?.verificationKeys?.find(
    (n) => n.alg === "ed25519" && n.keyId === e.keyId
  );
  if (!t)
    throw new Error("daemon verification key is not pinned");
  return vt.verify(zu(e.signature), je(e.message), zu(t.publicKey));
}
function Uy(e) {
  const t = ut(e.trim());
  if (t.length === 33 && (t[0] === 2 || t[0] === 3) && fe.utils.isValidPublicKey(t))
    return t;
  if (t.length === 65 && t[0] === 4 && fe.utils.isValidPublicKey(t))
    return fe.Point.fromBytes(t).toBytes(!0);
  throw new Error("expected a valid secp256k1 public key");
}
const k2 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", E2 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", S2 = 1, A2 = 734539939, Ai = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
function _2(e) {
  return yl(ue(e));
}
function Dy(e) {
  const t = ue(ue(e)).slice(0, 4), n = new Uint8Array(e.length + t.length);
  return n.set(e), n.set(t, e.length), zy(n);
}
function zy(e) {
  let t = BigInt(`0x${G(e)}`), n = "";
  for (; t > 0n; ) {
    const i = Number(t % 58n);
    t /= 58n, n = k2[i] + n;
  }
  let r = 0;
  for (const i of e) {
    if (i !== 0) break;
    r += 1;
  }
  return "1".repeat(r) + n;
}
function I2(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1)
      i >>> o & 1 && (n ^= t[o]);
  }
  return n;
}
function B2(e) {
  return [...e].map((t) => t.charCodeAt(0) >>> 5).concat([0], [...e].map((t) => t.charCodeAt(0) & 31));
}
function T2(e, t, n) {
  const r = B2(e).concat(t), i = I2(r.concat([0, 0, 0, 0, 0, 0])) ^ n;
  return [0, 1, 2, 3, 4, 5].map((o) => i >>> 5 * (5 - o) & 31);
}
function P2(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >>> i & s);
  return i && o.push(r << n - i & s), o;
}
function Pc(e, t, n) {
  const r = t === 0 ? S2 : A2, i = [t].concat(P2(n, 8, 5));
  return `${e}1${i.concat(T2(e, i, r)).map((o) => E2[o]).join("")}`;
}
function My(e) {
  const t = e.toLowerCase(), n = G(cs(je(t)));
  return [...t].map((r, i) => /[a-f]/.test(r) && Number.parseInt(n[i], 16) >= 8 ? r.toUpperCase() : r).join("");
}
function jl(e) {
  const t = BigInt(`0x${G(On.utils.taggedHash("TapTweak", e))}`);
  if (t >= Ai) throw new Error("invalid taproot tweak");
  const n = fe.Point.fromBytes(new Uint8Array([2, ...e])), r = t === 0n ? n : n.add(fe.Point.BASE.multiply(t));
  if (r.equals(fe.Point.ZERO)) throw new Error("invalid taproot output key");
  return r;
}
function Ky(e) {
  return jl(Uy(e).slice(1)).toBytes(!0).slice(1);
}
function da(e) {
  const t = Uy(e), n = fe.Point.fromBytes(t), r = n.toBytes(!1).slice(1), i = cs(r), o = _2(t), s = new Uint8Array(21);
  s[0] = 0, s.set(o, 1);
  const a = n.toBytes(!1).slice(1, 33), c = jl(a);
  return {
    eth: `0x${My(G(i.slice(-20)))}`,
    btc_legacy: Dy(s),
    btc_segwit: Pc("bc", 0, o),
    btc_taproot: Pc("bc", 1, c.toBytes(!1).slice(1, 33))
  };
}
const Vl = {
  windowSeconds: 600,
  strictApprovals: !1,
  parentValueSealAllowed: !0
};
let $c = { ...Vl };
function $2(e) {
  return e === 300 || e === 600 || e === 1800 ? e : Vl.windowSeconds;
}
function O2(e, t) {
  if (e === 300 || e === 600 || e === 1800) return e;
  throw new _("invalid_payload", `${t}.windowSeconds must be 300, 600, or 1800`);
}
function ql(e, t = "policy") {
  if (typeof e != "object" || e === null) throw new _("invalid_payload", `${t} must be an object`);
  const n = e;
  if (typeof n.strictApprovals != "boolean") throw new _("invalid_payload", `${t}.strictApprovals must be a boolean`);
  if (typeof n.parentValueSealAllowed != "boolean")
    throw new _("invalid_payload", `${t}.parentValueSealAllowed must be a boolean`);
  return {
    windowSeconds: O2(n.windowSeconds, t),
    strictApprovals: n.strictApprovals,
    parentValueSealAllowed: n.parentValueSealAllowed
  };
}
function C2(e) {
  if (typeof e != "object" || e === null) return { ...Vl };
  const t = e;
  return {
    windowSeconds: $2(t.windowSeconds),
    strictApprovals: t.strictApprovals === !0,
    parentValueSealAllowed: t.parentValueSealAllowed !== !1
  };
}
function Wl(e) {
  return $c = C2(e), $c;
}
function ze() {
  return { ...$c };
}
const R2 = "avibe-vault-lock:v1:", j = {
  vmk: null,
  wrapMeta: null,
  freshSetup: !1,
  scopeId: null
};
let sn = null, Qr = null, fr = null, zi = null, Oc = null, ln = 0;
const $s = /* @__PURE__ */ new Set();
let Ur = 0, Fy = null;
function L2(e) {
  Fy = e;
}
function ju() {
  return j.vmk ? "unlocked" : j.wrapMeta ? "locked" : "needs-setup";
}
function jy(e) {
  Fy?.({
    state: ju(),
    ...sn !== null && ju() === "unlocked" ? { expiresAt: sn } : {},
    reason: e
  });
}
function Gl() {
  fr !== null && (clearTimeout(fr), fr = null);
}
function Mi() {
  zi?.close(), zi = null, Oc = null;
}
function Vy(e) {
  const t = new TextEncoder().encode(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return btoa(n).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function H2(e) {
  let t = 2166136261;
  for (let n = 0; n < e.length; n += 1)
    t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
  return t.toString(16).padStart(8, "0");
}
function qy(e) {
  const t = At(e);
  return typeof t.vault_user_handle == "string" && t.vault_user_handle.length > 0 ? Vy(t.vault_user_handle) : `wrap-${H2(e)}`;
}
function N2(e) {
  return Vy(e);
}
function Zl(e) {
  if (typeof window > "u" || typeof BroadcastChannel > "u" || !e) {
    Mi();
    return;
  }
  const t = `${R2}${e}`;
  Oc !== t && (Mi(), Oc = t, zi = new BroadcastChannel(t), zi.onmessage = (n) => {
    const r = n.data;
    r?.type === "lock" && r.scopeId === e && ft({ broadcast: !1, reason: "manual-lock" });
  });
}
function U2() {
  j.scopeId && (Zl(j.scopeId), zi?.postMessage({ type: "lock", scopeId: j.scopeId }));
}
function Rt() {
  return new Error("vault-operation-aborted");
}
function Wy(e = {}) {
  ln += 1;
  for (const t of $s)
    t.abort(Rt());
  $s.clear();
  for (const [t, n] of nt)
    e.preserveAutoLockOperations && n.surviveAutoLock || (t.abort(Rt()), nt.delete(t));
}
function tt(e) {
  const t = { ...At(e) };
  for (const n of ["scheme", "record_meta", "dek_nonce", "wrapped_dek"]) delete t[n];
  return t;
}
function rn(e, t) {
  const n = [[e, t]];
  for (; n.length; ) {
    const [r, i] = n.pop();
    if (r === i) continue;
    if (r === null || i === null || typeof r != "object" || typeof i != "object" || Array.isArray(r) !== Array.isArray(i)) return !1;
    const o = Object.keys(r);
    if (o.length !== Object.keys(i).length) return !1;
    for (const s of o) {
      if (!Object.hasOwn(i, s)) return !1;
      n.push([r[s], i[s]]);
    }
  }
  return !0;
}
const nt = /* @__PURE__ */ new Map();
function Gy(e) {
  if (nt.size === 0) return;
  const t = tt(e);
  for (const [n, r] of nt)
    rn(r.root, t) || n.abort(Rt());
}
function ua(e, t = {}) {
  Os();
  const n = tt(e);
  if (j.wrapMeta && !rn(tt(j.wrapMeta), n) || (kt(e), !j.wrapMeta || !rn(tt(j.wrapMeta), n))) throw Rt();
  for (const l of nt.keys()) l.abort(Rt());
  nt.clear();
  const r = ++Ur, i = ln, o = t.surviveAutoLock === !0, s = o ? Vu() : Cc(), a = Vu(), c = new AbortController();
  return nt.set(c, { root: n, surviveAutoLock: o }), Zl(j.scopeId), {
    freshSetup: s,
    signal: c.signal,
    assertCurrent: () => {
      if (Os(), c.signal.aborted || r !== Ur || !o && i !== ln || !o && s !== Cc() || !j.wrapMeta || !rn(tt(j.wrapMeta), n)) throw Rt();
    },
    lockIfCurrent: (l = {}) => c.signal.aborted || r !== Ur ? !1 : Zy(i, n, l),
    release: (l = {}) => {
      const d = !l.completed && !c.signal.aborted && r === Ur && a && !j.vmk && j.freshSetup && j.wrapMeta !== null && rn(tt(j.wrapMeta), n);
      nt.delete(c), c.abort(Rt()), d && Yy(), !j.vmk && nt.size === 0 && Mi();
    }
  };
}
function Zy(e, t, n = {}) {
  return e !== ln || !j.vmk || !j.wrapMeta || !rn(tt(j.wrapMeta), t) ? !1 : (ft(n), !0);
}
function Yy() {
  j.wrapMeta = null, j.freshSetup = !1, j.scopeId = null;
}
function D2(e = {}) {
  j.vmk?.fill(0), j.vmk = null, sn = null, Qr = null, Gl(), e.preserveLockChannel || Mi(), j.freshSetup && !e.preserveFreshSetupRoot && Yy();
}
function z2() {
  return sn !== null && Date.now() >= sn;
}
function Os() {
  return j.vmk && z2() ? (ft({ broadcast: !1, reason: "auto-lock" }), !1) : j.vmk !== null;
}
function Xy(e) {
  const t = setTimeout(() => {
    fr !== t || Qr !== e || e.epoch !== ln || !j.vmk || (fr = null, ft({ broadcast: !1, reason: "auto-lock" }));
  }, Math.max(0, e.expiresAt - Date.now()));
  fr = t;
}
function Jy(e, t = ze()) {
  if (!j.vmk)
    throw new Error("vault VMK is not loaded");
  const n = t.windowSeconds * 1e3;
  return sn = Date.now() + n, Qr = { epoch: ln, expiresAt: sn, pendingCommits: 0 }, Gl(), Xy(Qr), jy(e), sn;
}
function M2() {
  const e = Qr;
  if (!j.vmk || !e) return () => {
  };
  e.pendingCommits += 1, Gl();
  let t = !1;
  return () => {
    if (!t && (t = !0, e.pendingCommits -= 1, !(Qr !== e || e.epoch !== ln || !j.vmk || e.pendingCommits !== 0 || fr !== null))) {
      if (e.expiresAt <= Date.now()) {
        ft({ broadcast: !1, reason: "auto-lock" });
        return;
      }
      Xy(e);
    }
  };
}
function K2(e) {
  if (e instanceof _ && e.retryable) return !0;
  const t = e instanceof Error ? e.message : String(e);
  return t === "operation-cancelled" || t === "operation-superseded" || t === "passkey-cancelled" || t === "vault-operation-aborted";
}
function F2(e) {
  if (e.signal.aborted || e.epoch !== ln || !j.vmk)
    throw Rt();
}
async function fa(e, t = {}) {
  if (!Os() || !j.vmk || !j.wrapMeta)
    throw new Error("vault-locked");
  const n = j.wrapMeta, r = t.expectedWrapMeta === void 0 ? void 0 : tt(t.expectedWrapMeta);
  if (r !== void 0 && !rn(tt(n), r)) throw Rt();
  const i = new Uint8Array(j.vmk), o = ln, s = Ur, a = new AbortController();
  $s.add(a), r !== void 0 && nt.set(a, { root: r, surviveAutoLock: !1 });
  const c = {
    epoch: o,
    signal: a.signal,
    assertCurrent: () => F2(c)
  };
  let l = null;
  try {
    c.assertCurrent();
    const d = await e(i, n, c);
    return c.assertCurrent(), t.renewOnSuccess !== !1 && Jy("renew"), c.assertCurrent(), t.beforeSuccess && (l = M2(), await t.beforeSuccess(), c.assertCurrent(), l(), l = null), d;
  } catch (d) {
    throw K2(d) || (r !== void 0 ? !a.signal.aborted && s === Ur && Zy(o, r, { broadcast: !0, reason: "manual-lock" }) : ft({ broadcast: !0, reason: "manual-lock" })), d;
  } finally {
    l?.(), $s.delete(a), nt.delete(a), i.fill(0);
  }
}
function Cc() {
  return j.vmk !== null && j.freshSetup;
}
function Vu() {
  return j.freshSetup && j.wrapMeta !== null;
}
function Qy(e) {
  Wy(), j.vmk?.fill(0), j.vmk = e.vmk, j.wrapMeta = e.wrapMeta, j.freshSetup = e.freshSetup, j.scopeId = e.scopeId, Zl(e.scopeId);
  const t = { state: "unlocked", expiresAt: Jy("unlock", e.policy) };
  if (e.onCommitted) {
    const n = ua(e.wrapMeta);
    try {
      e.onCommitted(n);
    } catch (r) {
      throw n.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), n.release(), r;
    }
  }
  return t;
}
function ft(e = {}) {
  e.broadcast && U2();
  const t = e.reason === "auto-lock", n = t && j.freshSetup && [...nt.values()].some((r) => r.surviveAutoLock);
  return Wy({ preserveAutoLockOperations: t }), D2({
    preserveFreshSetupRoot: n,
    preserveLockChannel: t && nt.size > 0
  }), jy(e.reason ?? "manual-lock"), { state: "locked" };
}
function kt(e) {
  Os();
  const t = e ? $y(e) : void 0;
  if (t && Gy(t), j.vmk)
    return {
      state: "unlocked",
      expiresAt: sn ?? void 0,
      ...j.freshSetup ? { freshSetup: !0 } : {}
    };
  if (t) {
    const n = j.wrapMeta !== null && rn(tt(j.wrapMeta), tt(t));
    j.freshSetup && !n && (j.freshSetup = !1), j.wrapMeta = t, j.scopeId = qy(t);
  }
  return j.wrapMeta ? { state: "locked" } : { state: "needs-setup" };
}
function Yl(e) {
  const t = $y(e), n = qy(t), r = j.wrapMeta !== null && rn(tt(j.wrapMeta), tt(t));
  return Gy(t), j.freshSetup && !r && (j.freshSetup = !1), j.vmk && !r && ft({ broadcast: !1, reason: "manual-lock" }), j.wrapMeta = t, j.scopeId = n, { wrapMeta: t, scopeId: n };
}
function j2() {
  ft({ broadcast: !1, reason: "unload" }), Mi();
}
const To = "Avibe Vault", qu = "avibe-vault";
function pr(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Un(e) {
  const t = new ArrayBuffer(e.byteLength);
  return new Uint8Array(t).set(e), t;
}
function ew() {
  return Un(crypto.getRandomValues(new Uint8Array(32)));
}
function tw(e) {
  try {
    return Ee(e);
  } catch {
    return null;
  }
}
function ha(e, t) {
  if (e instanceof ArrayBuffer || ArrayBuffer.isView(e))
    return Un(By(e));
  if (typeof e == "string") {
    const n = tw(e);
    return Un(n ?? new TextEncoder().encode(e));
  }
  if (Array.isArray(e))
    return Un(Uint8Array.from(e));
  throw new Error("webauthn bytes are missing");
}
function Va(e) {
  if (typeof e != "object" || e === null || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null)
    throw new Error("invalid webauthn creation options object");
  return e;
}
function V2(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 88 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(e) || /[+/]/.test(e) && /[-_]/.test(e))
    throw new Error("invalid webauthn user.id encoding");
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = tw(t);
  if (!n || n.byteLength === 0 || n.byteLength > 64)
    throw new Error("webauthn user.id must be 1-64 bytes");
  const r = le(n);
  if (t !== r && t !== r.replace(/=+$/, ""))
    throw new Error("invalid webauthn user.id encoding");
  return Un(n);
}
function q2(e) {
  const t = ha(e);
  if (t.byteLength === 0 || t.byteLength > 64)
    throw new Error("vault user handle must be 1-64 bytes");
  return t;
}
function Cs(e) {
  if (Array.isArray(e))
    return e.map((t) => {
      const n = pr(t);
      if (!n || n.type !== "public-key")
        throw new Error("invalid WebAuthn credential descriptor");
      return {
        type: "public-key",
        id: ha(n.id),
        ...Array.isArray(n.transports) ? { transports: n.transports } : {}
      };
    });
}
function Wu(e) {
  return !Array.isArray(e) || e.length === 0 ? [
    { type: "public-key", alg: -7 },
    { type: "public-key", alg: -257 }
  ] : e.map((t) => {
    const n = pr(t);
    if (!n || n.type !== "public-key" || typeof n.alg != "number")
      throw new Error("invalid WebAuthn pubKeyCredParams");
    return { type: "public-key", alg: n.alg };
  });
}
function Gu(e) {
  return {
    ...pr(e) ?? {},
    residentKey: "required",
    userVerification: "required"
  };
}
function Zu(e) {
  if (e === "none" || e === "indirect" || e === "direct" || e === "enterprise") return e;
}
function W2(e) {
  const t = pr(e) ?? {};
  return { ...t, prf: pr(t.prf) ?? {} };
}
function Ft() {
  return window.location.hostname;
}
function G2(e) {
  const t = e.authzCreationOptions === void 0 ? void 0 : Va(e.authzCreationOptions), n = t && Object.prototype.hasOwnProperty.call(t, "webauthn") ? Va(t.webauthn) : t, r = typeof t?.challenge_id == "string" ? t.challenge_id : void 0, i = pr(n?.rp), o = typeof i?.id == "string" ? i.id : void 0;
  if (o && o !== e.rpId)
    throw new Error("webauthn rp_id does not match sandbox host");
  if (!n)
    return {
      options: {
        rp: { name: To, id: e.rpId },
        user: {
          id: q2(e.vaultUserHandle),
          name: e.vaultUserHandle || qu,
          displayName: e.displayName || To
        },
        challenge: ew(),
        pubKeyCredParams: Wu(void 0),
        authenticatorSelection: Gu(void 0),
        extensions: { prf: {} }
      }
    };
  const s = Va(n.user);
  return {
    options: {
      rp: {
        name: typeof i?.name == "string" ? i.name : To,
        id: e.rpId
      },
      user: {
        id: V2(s.id),
        name: typeof s.name == "string" ? s.name : e.vaultUserHandle || qu,
        displayName: typeof s.displayName == "string" ? s.displayName : e.displayName || To
      },
      challenge: ha(n.challenge),
      pubKeyCredParams: Wu(n.pubKeyCredParams),
      authenticatorSelection: Gu(n.authenticatorSelection),
      extensions: W2(n.extensions),
      ...Cs(n.excludeCredentials) ? { excludeCredentials: Cs(n.excludeCredentials) } : {},
      ...typeof n.timeout == "number" ? { timeout: n.timeout } : {},
      ...Zu(n.attestation) ? { attestation: Zu(n.attestation) } : {}
    },
    ...r ? { challengeId: r } : {}
  };
}
function Z2(e) {
  if (e.length === 0) throw new Error("passkey-not-configured");
  if (e.length > 1) throw new Error("passkey-multiple-not-supported");
  return e[0];
}
function Y2(e, t) {
  const n = Z2(e), r = {
    challenge: ew(),
    rpId: t,
    userVerification: "required",
    extensions: Zx(n.prfSalt)
  };
  return n.credentialId && (r.allowCredentials = [
    {
      type: "public-key",
      id: Un(Ee(n.credentialId))
    }
  ]), r;
}
function X2(e, t, n) {
  const r = {
    challenge: Un(n),
    rpId: t,
    userVerification: "required"
  }, i = e.filter((o) => o.credentialId).map((o) => ({ type: "public-key", id: Un(Ee(o.credentialId)) }));
  return i.length > 0 && (r.allowCredentials = i), r;
}
function J2(e, t = Ft()) {
  const n = pr(e);
  if (!n) throw new Error("webauthn assertion options are missing");
  const r = typeof n.rpId == "string" ? n.rpId : typeof n.rp_id == "string" ? n.rp_id : void 0;
  if (r && r !== t)
    throw new Error("webauthn rpId does not match sandbox host");
  return {
    challenge: ha(n.challenge),
    rpId: t,
    userVerification: "required",
    ...Cs(n.allowCredentials) ? { allowCredentials: Cs(n.allowCredentials) } : {},
    ...typeof n.timeout == "number" ? { timeout: n.timeout } : {}
  };
}
function nw(e, t) {
  return {
    publicKey: e,
    ...t ? { signal: t } : {}
  };
}
function rw(e) {
  return {
    id: e.id,
    rawId: le(e.rawId),
    type: e.type
  };
}
function Q2(e) {
  const t = e.response, n = typeof t.getTransports == "function" ? t.getTransports() : [];
  return {
    ...rw(e),
    response: {
      clientDataJSON: le(t.clientDataJSON),
      attestationObject: le(t.attestationObject),
      transports: n
    }
  };
}
function iw(e) {
  const t = e.response;
  return {
    ...rw(e),
    response: {
      clientDataJSON: le(t.clientDataJSON),
      authenticatorData: le(t.authenticatorData),
      signature: le(t.signature),
      userHandle: t.userHandle ? le(t.userHandle) : null
    }
  };
}
function ek(e) {
  const n = e?.getClientExtensionResults()?.prf?.results?.first;
  if (!n) throw new Error("passkey-prf-unavailable");
  const r = By(n, "passkey PRF output");
  if (r.byteLength === 0) throw new Error("passkey-prf-unavailable");
  return r;
}
async function pa(e, t, n) {
  const r = await navigator.credentials.get(
    nw(Y2(e, t), n)
  );
  if (!r) throw new Error("passkey-cancelled");
  const i = ek(r), o = le(r.rawId), s = e.find((a) => a.credentialId === o);
  return { prfOutput: i, prfSalt: s?.prfSalt ?? e[0].prfSalt, credentialId: o };
}
async function tk(e, t, n, r) {
  if (!await navigator.credentials.get(
    nw(X2(e, t, n), r)
  )) throw new Error("passkey-cancelled");
}
async function nk(e) {
  const t = await navigator.credentials.get({
    publicKey: J2(e)
  });
  if (!t) throw new Error("passkey-cancelled");
  return iw(t);
}
const rk = /ancestor|not the same as its|cross-?origin|different origin/i;
function ow(e) {
  return typeof e?.name == "string" ? e.name : "";
}
function ik(e) {
  return e instanceof Error || typeof e?.message == "string" ? e.message : typeof e == "string" ? e : "";
}
function sw(e) {
  const t = ow(e);
  return t === "SecurityError" || t === "NotSupportedError" || rk.test(ik(e));
}
function aw(e) {
  return ow(e) === "NotAllowedError" && !sw(e);
}
function Xl(e) {
  const { options: t, challengeId: n } = G2(e);
  return navigator.credentials.create({
    publicKey: t
  }).then((i) => {
    if (!i) throw new Error("passkey-cancelled");
    const o = le(i.rawId), s = Q2(i), a = e.authzCreationOptions !== void 0;
    return {
      credentialId: o,
      ...a ? { authzRegistration: n ? { challenge_id: n, credential: s } : s } : {}
    };
  });
}
const Rc = {
  en: {
    "common.cancel": "Cancel",
    "common.continue": "Continue",
    "common.done": "Done",
    "sandbox.intro": "Your protected vault keys are created and used here, isolated from the rest of Avibe. This runs entirely in your browser — Avibe's servers only serve this code, never your keys.",
    "authorize.waiting": "Preparing your approval…",
    "authorize.waitingLong": "Still waiting for Avibe. If nothing appears, close this window and approve again in Avibe.",
    "setup.title": "Create vault passkey",
    "setup.subtitle": "Protected vault setup",
    "setup.iframeBody": "Create a passkey in this sandbox frame. Your vault key stays on this origin.",
    "setup.topLevelBody": "Confirm to create a resident passkey on this sandbox origin. Your vault key stays in this browser.",
    "setup.createPasskey": "Create passkey",
    "setup.finish": "Finish setup",
    "setup.ready": "Ready.",
    "setup.readyOnOrigin": "Ready on this origin.",
    "setup.popupBlocked": "Popup was blocked. Tap Create passkey again.",
    "setup.popupPrompt": "Create the passkey in the new window.",
    "setup.createdFinish": "Passkey created. Tap Finish setup to continue.",
    "setup.passkeyPrompt": "Follow your browser passkey prompt...",
    "setup.requestMissing": "Setup request is missing.",
    "setup.requestInvalid": "Setup request is invalid.",
    "setup.created": "Passkey created.",
    "unlock.sessionTitle": "Unlock vault",
    "unlock.sessionSubtitle": "Available in this browser for {{minutes}} min",
    "unlock.sessionBody": "Use your passkey once. While the vault stays unlocked, access requests only need confirmation; signing still verifies your passkey every time.",
    "plaintext.copyTitle": "Copy protected value",
    "plaintext.title": "Protected value",
    "plaintext.body": "The plaintext is visible only in this sandbox frame.",
    "plaintext.copy": "Copy",
    "plaintext.copyWarning": "Copying puts plaintext on the system clipboard, where other apps or pages may read it.",
    "plaintext.copyReady": "Passkey confirmed. Click Copy again to write to clipboard.",
    "plaintext.copyFailed": "Copy was not completed. The value remains visible here.",
    "plaintext.copiedBody": "Copied from the sandbox frame. The plaintext was not returned to Avibe.",
    "reveal.showConfirmTitle": "Show protected value",
    "reveal.confirmBody": "Confirm in this sandbox to display the value here. The plaintext will not be returned to Avibe.",
    "sign.title": "Sign protected operation",
    "hd.stepsLabel": "Steps",
    "hd.stepSetup": "Set up",
    "hd.stepBackup": "Write it down",
    "hd.stepCheck": "Check",
    "hd.generateHint": "Next you get 12 words; write them down, then tap 3 of them to check.",
    "hd.generateCta": "Generate the words",
    "hd.importHint": "Each word is checked as you paste, and account #0’s addresses show so you can compare before creating.",
    "hd.importCta": "Import wallet",
    "hd.createCta": "Create wallet",
    "hd.passphraseTitle": "Add a passphrase (optional)",
    "hd.passphraseFolded": "Most people don’t need one",
    "hd.passphraseSet": "Set",
    "hd.passphraseBody": "The passphrase and the words together decide the wallet’s addresses. Recovering it needs the words plus this exact passphrase; forget it and the wallet can’t be recovered.",
    "hd.passphraseLabel": "Passphrase",
    "hd.passphraseAgain": "Enter it again",
    "hd.passphraseMismatch": "The two entries don’t match.",
    "hd.passphraseMatch": "They match. Case and spaces count.",
    "hd.passphraseHint": "Case and spaces count and are kept as typed. Leave both empty for none.",
    "hd.passphraseBackupReminder": "This wallet also has a passphrase. Write it down separately from the words: recovering needs both, and the passphrase must be exact.",
    "hd.backupTitle": "Write these {{count}} words down, in order",
    "hd.backupBody": "Avibe won’t show them again after saving. If this computer fails or you move wallets, this backup is the only way back.",
    "hd.backupShow": "Show the {{count}} words",
    "hd.backupScreen": "Make sure no one can see your screen",
    "hd.backupCopy": "Copy",
    "hd.backupCopied": "Copied",
    "hd.backupHide": "Hide",
    "hd.backupClipboard": "Other apps can read the clipboard; paper is safer.",
    "hd.backupAgentNote": "Agents never need these words to sign; they are only for recovery. Never send them to an Agent or a chat.",
    "hd.backupDone": "I wrote them down",
    "hd.hiddenWord": "•••••",
    "hd.checkTitle": "Using your paper, pick these 3 words",
    "hd.checkBody": "Three taps to prove the backup works.",
    "hd.checkWord": "Word #{{position}}",
    "hd.checkWrong": "Word #{{position}} doesn’t match.",
    "hd.checkWrongHint": "Tap “See the words” and compare word #{{position}} on your paper; it may be miscopied.",
    "hd.checkAgain": "See the words",
    "hd.importLabel": "Paste the phrase",
    "hd.importPlaceholder": "12 or 24 English words separated by spaces. Each word is checked as you paste.",
    "hd.importLocal": "The phrase stays on this computer and never reaches a chat or a model.",
    "hd.importOk": "{{count}} words · checksum OK",
    "hd.importUnknown": "Word #{{position}}, “{{word}}”, is not a seed phrase word.",
    "hd.importDidYouMean": "Did you mean “{{word}}”?",
    "hd.importUse": "Use {{word}}",
    "hd.importChecksum": "All {{count}} words are valid, but together they fail the checksum.",
    "hd.importChecksumHint": "Usually one word was copied as another valid word, or two are swapped. Compare each word with your original.",
    "hd.importCount": "A phrase has 12, 15, 18, 21 or 24 words; this one has {{count}}.",
    "hd.importPassphrase": "BIP39 passphrase (optional)",
    "hd.importPassphraseFolded": "Most wallets don’t use one",
    "hd.importPassphraseHint": "Only if your old wallet used one. A wrong passphrase gives no error, just different addresses, so check them below.",
    "hd.importPreviewTitle": "Compare with your old wallet’s first account",
    "hd.importPreviewHint": "MetaMask’s Account 1 and Phantom’s first account show the same EVM and Solana addresses. The Taproot address shows only in wallets that support Taproot, such as Phantom.",
    "hd.btcTaprootInfo": "This is a Taproot address (starts with bc1p). Phantom and some other wallets show a SegWit address (bc1q) by default; switch them to Taproot before comparing.",
    "hd.accountIndex": "#{{account}}",
    "hd.back": "Back",
    "hd.showValue": "Show value",
    "hd.hideValue": "Hide value",
    "hd.title": "Seed phrase wallet",
    "hd.reviewEvm": "ETH",
    "hd.reviewBitcoin": "BTC · Taproot",
    "hd.reviewSolana": "Solana",
    "hd.passkeyNote": "Saving is confirmed with your passkey; the secure window then reopens the encrypted result to check it before handing the encrypted result to Avibe.",
    "hd.working": "Saving and checking…",
    "hd.unlockTitle": "Unlock protected wallet",
    "hd.unlockBody": "First unlock with your passkey. Then review the actual account and derivation path and use your passkey again to approve this signature.",
    "review.field.walletAccount": "Wallet account",
    "review.group.hdAccount": "Protected wallet account",
    "sign.confirm": "Confirm sign",
    "review.details": "Details",
    "review.copy": "Copy",
    "review.copied": "Copied",
    "review.copySelected": "Selected; copy with the keyboard",
    "review.kind.solanaSystemTransfer": "Solana · SOL transfer",
    "review.kind.solanaTokenTransfer": "Solana · token transfer",
    "review.kind.solanaTransaction": "Solana transaction",
    "review.kind.evmTransaction": "EVM transaction",
    "review.kind.bitcoinTransfer": "Bitcoin · Taproot (P2TR) transfer",
    "review.kind.eip712": "Ethereum · typed data (EIP-712)",
    "review.kind.erc2612Permit": "Token authorization · ERC-2612 permit",
    "review.kind.permit2Allowance": "Token authorization · Permit2 allowance",
    "review.kind.permit2Transfer": "Token transfer authorization · Permit2 (one-time)",
    "review.field.typedField": "{{name}}",
    "review.field.domainName": "Domain name",
    "review.field.verifyingContract": "Verifying contract",
    "review.field.primaryType": "Primary type",
    "review.field.spender": "Spender",
    "review.field.permitAmount": "Allowance amount",
    "review.field.permitOwner": "Owner (in message)",
    "review.field.permitDeadline": "Signature deadline",
    "review.field.permitNonce": "Permit nonce",
    "review.field.permitToken": "Token {{index}}",
    "review.field.permitTokenAmount": "Token {{index}} amount",
    "review.field.permitExpiration": "Token {{index}} allowance expires",
    "review.field.moreTokens": "More tokens",
    "review.field.signatureDeadline": "Signature deadline",
    "review.field.moreFields": "More fields",
    "review.field.notInTypedHash": "Outside the typed-data hash",
    "review.field.typedTypes": "Types",
    "review.field.typedData": "Complete typed data",
    "review.note.unknownChain": "unrecognized chain ID",
    "review.note.notInTypedHash": "not in the typed-data hash",
    "review.note.messageNotHashed": "the message is not hashed",
    "review.note.fieldsOutsideHash": "{{count}} values present but not signed by the typed data; see Details",
    "review.note.rawTokenUnits": "raw token units",
    "review.note.permitDeadline": "the permit must be used by this time; not how long the allowance lasts",
    "review.note.permit2Expiration": "allowance expiry; 0 means it expires in the block it is used",
    "review.note.signatureDeadline": "the signature must be used by this time",
    "review.note.moreInRawData": "and {{count}} more in the complete typed data",
    "review.note.pathShortened": "path shortened; complete in the typed data",
    "review.warning.tokenAuthorization": "This signature lets the spender move your tokens.",
    "review.warning.unlimitedAllowance": "Unlimited amount: the allowance is the largest value the field can hold.",
    "review.warning.oneTimeTransfer": "This signature lets the spender make one transfer of your tokens.",
    "review.warning.maximumOneTimeTransfer": "The permitted amount is the largest value the field can hold, for that one transfer.",
    "review.group.typedDomain": "Domain",
    "review.group.typedMessage": "Message",
    "review.group.notInTypedHash": "Outside the typed-data hash",
    "review.field.network": "Network",
    "review.field.returnToKey": "Returning to this key",
    "review.field.returnAmount": "Return amount",
    "review.field.fee": "Fee",
    "review.field.feeRate": "Fee rate",
    "review.field.netLeaving": "Net leaving this key",
    "review.field.send": "Send",
    "review.field.recipientAddress": "Recipient address",
    "review.value.unlimited": "Unlimited",
    "review.value.maximumOneTime": "Maximum, for one transfer only",
    "review.note.exactInDetails": "exact value in Details",
    "review.kind.erc20Transfer": "Token transfer (ERC-20 format)",
    "review.kind.tokenApprove": "Token authorization · approve",
    "review.kind.tokenIncreaseAllowance": "Token authorization · increaseAllowance",
    "review.field.transferTo": "To",
    "review.field.tokenContract": "Token contract",
    "review.field.approvalValue": "Allowance or NFT token ID",
    "review.field.allowanceIncrease": "Allowance increase",
    "review.field.nativeTransfer": "Transfer",
    "review.field.maxEntries": "Entries at the maximum",
    "review.value.maximum": "Maximum",
    "review.value.maximumIncrease": "Maximum increase",
    "review.note.addedToAllowance": "added to the current allowance; the resulting total is not in this transaction",
    "review.note.moreEntriesInDetails": "{{count}} more, see Details",
    "review.note.ofEntries": "of {{count}} entries",
    "review.warning.maximumApproval": "This value is the largest uint256. If it is an ERC-20 allowance, that usually means unlimited; it may also be an NFT token ID.",
    "review.amount.baseUnitsDecimals": "({{value}} base units, decimals {{count}})",
    "review.time.raw": "({{value}})",
    "review.time.noDate": "(no calendar date for this value)",
    "review.field.internalKey": "Internal key",
    "review.field.outputKey": "Taproot output key",
    "review.field.input": "Input",
    "review.field.inputAmount": "Input amount",
    "review.field.outputIndexed": "Output {{index}}",
    "review.field.outputAmountIndexed": "Output {{index}} amount",
    "review.field.signedVsize": "Signed size",
    "review.field.sequence": "Sequence",
    "review.field.locktime": "Locktime",
    "review.field.unsignedTxid": "Unsigned transaction ID",
    "review.field.prevoutScript": "Spent output script",
    "review.note.requesterNetwork": "selected by the requester, not proven on chain",
    "review.note.networkNotChainProof": "selected by the requester; not chain proof. Verify the destination and funding independently, and do not treat testnet or Signet as safer.",
    "review.note.recipient": "recipient",
    "review.note.sameKeyReturn": "returns to this key",
    "review.note.vbytes": "vB",
    "review.note.rbfSignals": "signals RBF",
    "review.note.rbfDoesNotSignal": "does not signal RBF",
    "review.warning.highFee": "This fee is above the V1 review threshold (100 sat/vB or 2% of the input).",
    "review.kind.bitcoinLegacyTransfer": "Bitcoin · Legacy (P2PKH) transfer",
    "review.kind.bitcoinNativeTransfer": "Bitcoin · Native SegWit (P2WPKH) transfer",
    "review.field.returnToAccount": "Returning to this wallet account",
    "review.field.netLeavingAccount": "Net leaving this wallet account",
    "review.field.addressType": "Address type",
    "review.field.derivationPath": "Derivation path",
    "review.field.sighashType": "Signature hash type",
    "review.field.digestAlgorithm": "Digest algorithm",
    "review.field.scriptCode": "Script code (BIP143)",
    "review.field.vsizeRange": "Signed size",
    "review.field.feeRateRange": "Fee rate",
    "review.field.unsignedTemplateHash": "Unsigned template hash",
    "review.field.parentTransaction": "Funding transaction (complete)",
    "review.field.parentTxid": "Funding transaction ID",
    "review.field.outputScriptIndexed": "Output {{index}} script",
    "review.note.sameAccountReturn": "returns to this wallet account",
    "review.note.scriptP2pkh": "P2PKH (Legacy)",
    "review.note.scriptP2sh": "P2SH",
    "review.note.scriptP2wpkh": "P2WPKH (Native SegWit)",
    "review.note.scriptP2wsh": "P2WSH",
    "review.note.scriptP2tr": "P2TR (Taproot)",
    "review.note.legacyAmountFromParent": "matches the complete funding transaction in Details; not proof that it is confirmed or unspent",
    "review.note.legacyNoAmountField": "a Legacy signature does not commit to this amount directly; the verified funding transaction binds it indirectly",
    "review.note.nativeAmountSupplied": "supplied by the requester and committed by this signature; not independently verified on chain",
    "review.note.nativeFormOnly": "requested native form only; check the actual output and assembly before broadcasting",
    "review.field.requestedPrevoutScript": "Requested spent-output script",
    "review.note.requestedNativeScript": "this key's Native SegWit script, as requested; the signature does not prove the spent output uses it",
    "review.note.scriptFromParent": "matches the selected output of the funding transaction",
    "review.note.notFinalTxid": "not the final transaction ID: the Legacy signature changes it",
    "review.note.finalTxidUnchanged": "also the final transaction ID; the witness changes only the wtxid",
    "review.note.sizeRange": "the exact size depends on the signature; the fee limit uses the smallest size",
    "review.field.signingKey": "Signing key",
    "review.field.signingAddress": "Signing address",
    "review.field.publicKey": "Public key",
    "review.field.from": "From",
    "review.field.to": "To",
    "review.field.fromTokenAccount": "From token account",
    "review.field.toTokenAccount": "To token account",
    "review.field.mint": "Mint",
    "review.field.amount": "Amount",
    "review.field.program": "Program",
    "review.field.undecodedInstruction": "Undecoded instruction",
    "review.field.durableNonce": "Includes a durable-nonce advance instruction",
    "review.field.nonceAccount": "Nonce account",
    "review.field.nonceAuthority": "Nonce authority (as encoded)",
    "review.field.recentBlockhashesSysvar": "RecentBlockhashes sysvar",
    "review.field.instruction": "Instruction",
    "review.field.instructions": "Instructions",
    "review.field.transferSol": "{{index}} · SOL transfer",
    "review.field.transferToken": "{{index}} · Token transfer",
    "review.field.transferTokenNoMint": "{{index}} · Token transfer · mint not in instruction",
    "review.field.moreTransfers": "More transfers",
    "review.field.moreUndecoded": "More undecoded instructions",
    "review.field.authority": "Authority",
    "review.field.accountIndexes": "Account indexes",
    "review.field.data": "Data",
    "review.field.messageFormat": "Message format",
    "review.field.feePayer": "Fee payer",
    "review.field.blockhashOrNonce": "Blockhash or nonce value",
    "review.field.scheme": "Signature scheme",
    "review.field.account": "Account {{index}}",
    "review.field.message": "Complete message",
    "review.field.chainId": "Chain ID",
    "review.field.contract": "To",
    "review.field.value": "Value",
    "review.field.selector": "Selector",
    "review.field.type": "Type",
    "review.field.nonce": "Nonce",
    "review.field.gas": "Gas limit",
    "review.field.maxFeePerGas": "Max fee per gas",
    "review.field.gasPrice": "Gas price",
    "review.field.maxPriorityFeePerGas": "Max priority fee per gas",
    "review.field.call": "Call",
    "review.field.argument": "Argument {{index}} · {{name}}",
    "review.field.initCode": "Contract init code",
    "review.field.calldata": "Complete calldata",
    "review.field.unsignedTransaction": "Unsigned transaction",
    "review.field.digest": "Signing digest",
    "review.note.wei": "wei",
    "review.note.baseUnits": "base units",
    "review.note.decimals": "decimals {{count}}",
    "review.note.bytes": "{{count}} bytes",
    "review.note.dataBytes": "{{count}} bytes of data",
    "review.note.notDecoded": "not decoded",
    "review.note.transfersDecoded": "{{count}} decoded transfers",
    "review.note.instructionsNotDecoded": "{{count}} not decoded",
    "review.note.nonceAdvances": "{{count}} nonce advances",
    "review.note.moreInDetails": "and {{count}} more, see Details",
    "review.note.moreInstructionsInDetails": "and {{count}} more, see Details",
    "review.note.argumentsInDetails": "{{count}} arguments in Details",
    "review.note.contractCreation": "contract creation",
    "review.note.signer": "signer",
    "review.note.writable": "writable",
    "review.note.readOnly": "read-only",
    "review.note.systemProgram": "System Program",
    "review.note.tokenProgram": "SPL Token Program",
    "review.group.request": "Request",
    "review.group.transaction": "Transaction",
    "review.group.accounts": "Accounts in message",
    "review.group.instruction": "Instruction {{index}}",
    "review.group.call": "Decoded call",
    "review.group.exact": "Exact content to sign",
    "bitcoin.signingIdentity": `BIP341 Taproot key-path Schnorr (BIP86 empty tree)
Internal key: {{key}}`,
    "bitcoin.title": "Review Bitcoin Taproot payment",
    "bitcoin.network": "Network (requester's selection): {{network}}",
    "bitcoin.networkWarning": "This network label is not chain proof. Verify the destination and funding independently; do not treat testnet or Signet as safer.",
    "bitcoin.input": "Input: {{txid}}:{{vout}}",
    "bitcoin.inputAmount": "Input amount: {{sats}} sats ({{btc}} BTC)",
    "bitcoin.outputs": "Outputs",
    "bitcoin.output": "Output {{index}}: {{address}} · {{sats}} sats ({{btc}} BTC) · {{role}}",
    "bitcoin.sameKeyReturn": "same-key return",
    "bitcoin.recipient": "recipient",
    "bitcoin.fee": "Fee: {{sats}} sats ({{btc}} BTC) · {{rate}}",
    "bitcoin.vsize": "Signed vsize: {{vsize}} vB",
    "bitcoin.rbfSignals": "This transaction signals RBF.",
    "bitcoin.rbfDoesNotSignal": "This transaction does not signal RBF.",
    "bitcoin.netLeaving": "Net value leaving this key: {{sats}} sats ({{btc}} BTC)",
    "bitcoin.unsignedTxid": "Unsigned transaction ID: {{txid}}",
    "bitcoin.outputKey": "Taproot output key Q: {{key}}",
    "bitcoin.highFeeWarning": "Warning: this fee is above the V1 review threshold (100 sat/vB or 2% of input).",
    "sign.blindNotice": "Some content is not decoded; approving it is blind signing.",
    "evm.title": "EVM transaction on chain {{chainId}}",
    "evm.type": "type: {{type}}",
    "evm.to": "to: {{to}}",
    "evm.contractCreation": "to: (contract creation)",
    "evm.value": "value: {{value}} wei",
    "evm.nonce": "nonce: {{value}}",
    "evm.gas": "gas: {{value}}",
    "evm.gasPrice": "gasPrice: {{value}} wei",
    "evm.maxFeePerGas": "maxFeePerGas: {{value}} wei",
    "evm.maxPriorityFeePerGas": "maxPriorityFeePerGas: {{value}} wei",
    "evm.call": "call: {{call}}",
    "evm.selector": "selector: {{selector}}",
    "evm.selectorNone": "selector: none",
    "evm.calldata": "calldata ({{bytes}} bytes, complete): {{calldata}}",
    "evm.calldataNone": "calldata: none",
    "release.title": "Release protected access",
    "release.subtitle": "{{count}} protected item(s)",
    "release.confirm": "Confirm release",
    "policyDowngrade.title": 'Turn off "Ask before signing"',
    "policyDowngrade.confirm": "Turn off confirmation",
    "policyDowngrade.standardWarning": "After this, Agents can sign with it without asking you. This confirmation only guards against mistakes; to protect funds, use the protected tier.",
    "policyDowngrade.protectedNotice": "Protected keys still need your Passkey approval in the browser for every signature.",
    "policyDowngrade.setting": "Setting",
    "policyDowngrade.settingValue": "Ask before signing: On → Off",
    "policyDowngrade.key": "Key",
    "policyDowngrade.tier": "Tier",
    "policyDowngrade.standard": "Standard",
    "policyDowngrade.protected": "Protected",
    "policyDowngrade.account": "Account",
    "policyDowngrade.accountNamed": "#{{index}} {{name}}",
    "policyDowngrade.accountUnnamed": "#{{index}} (no name)",
    "policyDowngrade.appliesTo": "Applies to",
    "policyDowngrade.appliesToValue": "This account's signing on ETH, BTC and Solana",
    "policyDowngrade.onlyPolicy": "This only changes the setting. It doesn't sign anything or touch the key.",
    "policyDowngrade.details": "Request details",
    "policyDowngrade.serverFacts": "Server-asserted target facts",
    "policyDowngrade.eth": "EVM address",
    "policyDowngrade.btcLegacy": "Bitcoin legacy address",
    "policyDowngrade.btcSegwit": "Bitcoin SegWit address",
    "policyDowngrade.btcTaproot": "Bitcoin Taproot address",
    "policyDowngrade.solana": "Solana address",
    "policyDowngrade.recordId": "Record ID",
    "policyDowngrade.identity": "Identity digest",
    "policyDowngrade.revision": "Record revision",
    "policyDowngrade.expires": "Approval expires",
    "policyDowngrade.challengeId": "Challenge ID",
    "policyDowngrade.notUsed": "Not used for this signer",
    "context.session": "Session",
    "context.command": "Command",
    "context.egress": "Egress",
    "context.source": "Source",
    "context.sourceEnv": "env",
    "context.signReturns": "Returns",
    "context.sourceRequestedName": "requested as",
    "context.sourceTag": "tag",
    "context.sourceSkill": "skill",
    "context.agent": "Agent",
    "context.grant": "Grant",
    "context.agentAccess": "Agent access",
    "context.secrets": "Secrets",
    "context.kindStatic": "static",
    "context.kindKeypair": "keypair",
    "context.oneTime": "one-time",
    "context.minutes": "{{count}} min",
    "context.seconds": "{{count}} sec"
  },
  zh: {
    "common.cancel": "取消",
    "common.continue": "继续",
    "common.done": "完成",
    "sandbox.intro": "受保护的保险库密钥在这里创建和使用，与 Avibe 的其他部分隔离。一切都在你的浏览器里完成——Avibe 的服务器只提供这段代码，从不接触你的密钥。",
    "authorize.waiting": "正在准备授权…",
    "authorize.waitingLong": "仍在等待 Avibe。如果一直没有出现，请关闭这个窗口，回到 Avibe 重新批准。",
    "setup.title": "创建保险库 passkey",
    "setup.subtitle": "保护档设置",
    "setup.iframeBody": "在这个沙箱窗口里创建 passkey。保险库密钥只留在这里。",
    "setup.topLevelBody": "确认在沙箱域名上创建 passkey。保险库密钥只留在这个浏览器里。",
    "setup.createPasskey": "创建 passkey",
    "setup.finish": "完成设置",
    "setup.ready": "准备好了。",
    "setup.readyOnOrigin": "当前域名已准备好。",
    "setup.popupBlocked": "弹窗被拦截了。再点一次创建 passkey。",
    "setup.popupPrompt": "请在新窗口里创建 passkey。",
    "setup.createdFinish": "passkey 已创建。点「完成设置」继续。",
    "setup.passkeyPrompt": "请按浏览器里的 passkey 提示操作…",
    "setup.requestMissing": "缺少设置请求。",
    "setup.requestInvalid": "设置请求无效。",
    "setup.created": "passkey 已创建。",
    "unlock.sessionTitle": "解锁保险库",
    "unlock.sessionSubtitle": "本浏览器内保持 {{minutes}} 分钟",
    "unlock.sessionBody": "使用一次 Passkey 解锁。解锁期间，普通授权只需确认；签名仍会每次验证 Passkey。",
    "plaintext.copyTitle": "复制保护档值",
    "plaintext.title": "保护档值",
    "plaintext.body": "明文只会在这个沙箱窗口里显示。",
    "plaintext.copy": "复制",
    "plaintext.copyWarning": "复制会把明文放到系统剪贴板，其他应用或页面可能读取。",
    "plaintext.copyReady": "passkey 已确认。再次点击「复制」写入剪贴板。",
    "plaintext.copyFailed": "复制未完成。明文仍只显示在这里。",
    "plaintext.copiedBody": "已从沙箱窗口复制。明文没有交给 Avibe。",
    "reveal.showConfirmTitle": "显示保护档值",
    "reveal.confirmBody": "在沙箱里确认后，只在这里显示明文。明文不会交给 Avibe。",
    "sign.title": "签名保护档操作",
    "hd.stepsLabel": "步骤",
    "hd.stepSetup": "设置",
    "hd.stepBackup": "抄下助记词",
    "hd.stepCheck": "核对",
    "hd.generateHint": "下一步生成 12 个词，抄下来后点 3 个词核对。",
    "hd.generateCta": "生成助记词",
    "hd.importHint": "粘贴后逐词检查，并显示账户 #0 的地址，可以先核对再创建。",
    "hd.importCta": "导入钱包",
    "hd.createCta": "创建钱包",
    "hd.passphraseTitle": "添加 Passphrase（可选）",
    "hd.passphraseFolded": "大多数人不需要",
    "hd.passphraseSet": "已设置",
    "hd.passphraseBody": "Passphrase 和这些词一起决定钱包的地址。恢复时需要词，加上一字不差的 Passphrase；忘了它，钱包就找不回来。",
    "hd.passphraseLabel": "Passphrase",
    "hd.passphraseAgain": "再输入一次",
    "hd.passphraseMismatch": "两次输入不一样。",
    "hd.passphraseMatch": "两次输入一致。区分大小写和空格。",
    "hd.passphraseHint": "区分大小写和空格，原样保留。两栏都空着就是不设置。",
    "hd.passphraseBackupReminder": "这个钱包还设置了 Passphrase。把它和这些词分开记下：恢复时两样都要，Passphrase 要一字不差。",
    "hd.backupTitle": "按顺序抄下这 {{count}} 个词",
    "hd.backupBody": "保存后 Avibe 不会再显示它们。这台电脑坏了或要换到别的钱包时，只能靠这份备份。",
    "hd.backupShow": "显示 {{count}} 个词",
    "hd.backupScreen": "先确认没人能看到你的屏幕",
    "hd.backupCopy": "复制",
    "hd.backupCopied": "已复制",
    "hd.backupHide": "隐藏",
    "hd.backupClipboard": "复制后其他应用可能读到剪贴板，写在纸上更安全。",
    "hd.backupAgentNote": "Agent 签名不需要这些词，它们只用来恢复。不要发给任何 Agent 或聊天。",
    "hd.backupDone": "我已抄好",
    "hd.hiddenWord": "•••••",
    "hd.checkTitle": "看着你的纸，选出这 3 个词",
    "hd.checkBody": "只用点 3 下，确认备份能用。",
    "hd.checkWord": "第 {{position}} 个词",
    "hd.checkWrong": "第 {{position}} 个词对不上。",
    "hd.checkWrongHint": "点「再看一遍」，对照纸上的第 {{position}} 个词，可能抄错了。",
    "hd.checkAgain": "再看一遍",
    "hd.importLabel": "粘贴助记词",
    "hd.importPlaceholder": "12 或 24 个英文单词，用空格分开。粘贴后会逐个检查。",
    "hd.importLocal": "助记词只在这台电脑上处理，不会进入聊天或模型。",
    "hd.importOk": "{{count}} 个词 · 校验通过",
    "hd.importUnknown": "第 {{position}} 个词“{{word}}”不在助记词词表里。",
    "hd.importDidYouMean": "是不是“{{word}}”？",
    "hd.importUse": "改成 {{word}}",
    "hd.importChecksum": "{{count}} 个词都对得上词表，但合在一起校验不通过。",
    "hd.importChecksumHint": "通常是某个词抄成了另一个有效的词，或两个词顺序颠倒。请逐个对照原件。",
    "hd.importCount": "助记词应是 12、15、18、21 或 24 个词，现在是 {{count}} 个。",
    "hd.importPassphrase": "BIP39 密码（passphrase，可选）",
    "hd.importPassphraseFolded": "大多数钱包没有设置",
    "hd.importPassphraseHint": "只有原钱包设置过才填。填错不会报错，只会得到另一组地址，所以请对照下面的地址。",
    "hd.importPreviewTitle": "和原钱包的第一个账户核对",
    "hd.importPreviewHint": "MetaMask 的 Account 1、Phantom 的第一个账户应显示相同的 EVM 和 Solana 地址。Taproot 地址只在 Phantom 等支持 Taproot 的钱包里看得到。",
    "hd.btcTaprootInfo": "这是 Taproot 地址（bc1p 开头）。Phantom 等钱包默认显示 SegWit 地址（bc1q 开头），核对时请先在钱包里切换到 Taproot。",
    "hd.accountIndex": "#{{account}}",
    "hd.back": "上一步",
    "hd.showValue": "显示值",
    "hd.hideValue": "隐藏值",
    "hd.title": "助记词钱包",
    "hd.reviewEvm": "ETH",
    "hd.reviewBitcoin": "BTC · Taproot",
    "hd.reviewSolana": "Solana",
    "hd.passkeyNote": "保存时用 Passkey 确认；随后在安全窗口里重新打开加密结果核对，再把加密结果交给 Avibe。",
    "hd.working": "正在保存并核对…",
    "hd.unlockTitle": "解锁保护档钱包",
    "hd.unlockBody": "先使用 Passkey 解锁，再核对实际账户和派生路径，并再次使用 Passkey 批准本次签名。",
    "review.field.walletAccount": "钱包账户",
    "review.group.hdAccount": "保护档钱包账户",
    "sign.confirm": "确认签名",
    "review.details": "详情",
    "review.copy": "复制",
    "review.copied": "已复制",
    "review.copySelected": "已选中，请用键盘复制",
    "review.kind.solanaSystemTransfer": "Solana · SOL 转账",
    "review.kind.solanaTokenTransfer": "Solana · 代币转账",
    "review.kind.solanaTransaction": "Solana 交易",
    "review.kind.evmTransaction": "EVM 交易",
    "review.kind.bitcoinTransfer": "Bitcoin · Taproot（P2TR）转账",
    "review.kind.eip712": "以太坊 · 结构化签名（EIP-712）",
    "review.kind.erc2612Permit": "代币授权 · ERC-2612 Permit",
    "review.kind.permit2Allowance": "代币授权 · Permit2 额度",
    "review.kind.permit2Transfer": "代币转账授权 · Permit2（一次性）",
    "review.field.typedField": "{{name}}",
    "review.field.domainName": "域名称（domain.name）",
    "review.field.verifyingContract": "验证合约",
    "review.field.primaryType": "主类型",
    "review.field.spender": "被授权方（spender）",
    "review.field.permitAmount": "授权额度",
    "review.field.permitOwner": "所有者（消息中）",
    "review.field.permitDeadline": "签名截止时间",
    "review.field.permitNonce": "Permit nonce",
    "review.field.permitToken": "代币 {{index}}",
    "review.field.permitTokenAmount": "代币 {{index}} 数额",
    "review.field.permitExpiration": "代币 {{index}} 额度到期",
    "review.field.moreTokens": "其余代币",
    "review.field.signatureDeadline": "签名截止时间",
    "review.field.moreFields": "其余字段",
    "review.field.notInTypedHash": "未纳入结构化签名哈希",
    "review.field.typedTypes": "类型定义",
    "review.field.typedData": "完整结构化数据",
    "review.note.unknownChain": "未识别的链 ID",
    "review.note.notInTypedHash": "未纳入结构化签名哈希",
    "review.note.messageNotHashed": "消息未被哈希",
    "review.note.fieldsOutsideHash": "有 {{count}} 个值未被结构化签名覆盖，见详情",
    "review.note.rawTokenUnits": "代币最小单位",
    "review.note.permitDeadline": "须在此时间前使用该授权；不是额度的有效期",
    "review.note.permit2Expiration": "额度到期时间；0 表示在使用的区块内到期",
    "review.note.signatureDeadline": "须在此时间前使用该签名",
    "review.note.moreInRawData": "另有 {{count}} 项，见完整结构化数据",
    "review.note.pathShortened": "路径已缩略，完整路径见结构化数据",
    "review.warning.tokenAuthorization": "代币授权：此签名允许被授权方动用你的代币。",
    "review.warning.unlimitedAllowance": "无限授权：额度为该字段能表示的最大值。",
    "review.warning.oneTimeTransfer": "代币转账授权：此签名允许被授权方转走你的代币一次。",
    "review.warning.maximumOneTimeTransfer": "许可数额为该字段能表示的最大值，仅限这一次转账。",
    "review.group.typedDomain": "域（domain）",
    "review.group.typedMessage": "消息（message）",
    "review.group.notInTypedHash": "未纳入结构化签名哈希",
    "review.field.network": "网络",
    "review.field.returnToKey": "退回此密钥",
    "review.field.returnAmount": "退回金额",
    "review.field.fee": "手续费",
    "review.field.feeRate": "费率",
    "review.field.netLeaving": "实际离开此密钥",
    "review.field.send": "转出",
    "review.field.recipientAddress": "收款地址",
    "review.value.unlimited": "无限",
    "review.value.maximumOneTime": "最大值，仅限这一次转账",
    "review.note.exactInDetails": "原始数值见详情",
    "review.kind.erc20Transfer": "代币转账（ERC-20 格式）",
    "review.kind.tokenApprove": "代币授权 · approve",
    "review.kind.tokenIncreaseAllowance": "代币授权 · 增加额度（increaseAllowance）",
    "review.field.transferTo": "转给",
    "review.field.tokenContract": "代币合约",
    "review.field.approvalValue": "额度或 NFT 编号",
    "review.field.allowanceIncrease": "增加额度",
    "review.field.nativeTransfer": "转账",
    "review.field.maxEntries": "最大值条目",
    "review.value.maximum": "最大值",
    "review.value.maximumIncrease": "最大增量",
    "review.note.addedToAllowance": "在现有额度上增加；结果总额不在此交易中",
    "review.note.moreEntriesInDetails": "另外 {{count}} 项，见详情",
    "review.note.ofEntries": "共 {{count}} 项",
    "review.warning.maximumApproval": "该值为 uint256 最大值：若为 ERC-20 额度，通常表示无限授权；也可能是 NFT 编号。",
    "review.amount.baseUnitsDecimals": "（{{value}} 最小单位，小数位 {{count}}）",
    "review.time.raw": "（{{value}}）",
    "review.time.noDate": "（该数值无法换算为日期）",
    "review.field.internalKey": "内部公钥",
    "review.field.outputKey": "Taproot 输出公钥",
    "review.field.input": "输入",
    "review.field.inputAmount": "输入金额",
    "review.field.outputIndexed": "输出 {{index}}",
    "review.field.outputAmountIndexed": "输出 {{index}} 金额",
    "review.field.signedVsize": "签名后大小",
    "review.field.sequence": "Sequence",
    "review.field.locktime": "Locktime",
    "review.field.unsignedTxid": "未签名交易 ID",
    "review.field.prevoutScript": "被花费输出脚本",
    "review.note.requesterNetwork": "请求方标注，未经链上证明",
    "review.note.networkNotChainProof": "由请求方标注，不是链上证明。请独立核对收款地址和资金来源；不要因为是测试网或 Signet 就降低警惕。",
    "review.note.recipient": "收款方",
    "review.note.sameKeyReturn": "退回此密钥",
    "review.note.vbytes": "vB",
    "review.note.rbfSignals": "发出 RBF 信号",
    "review.note.rbfDoesNotSignal": "不发出 RBF 信号",
    "review.warning.highFee": "手续费高于 V1 审核阈值（100 sat/vB 或输入金额的 2%）。",
    "review.kind.bitcoinLegacyTransfer": "Bitcoin · Legacy（P2PKH）转账",
    "review.kind.bitcoinNativeTransfer": "Bitcoin · Native SegWit（P2WPKH）转账",
    "review.field.returnToAccount": "退回此钱包账户",
    "review.field.netLeavingAccount": "实际离开此钱包账户",
    "review.field.addressType": "地址类型",
    "review.field.derivationPath": "派生路径",
    "review.field.sighashType": "签名哈希类型",
    "review.field.digestAlgorithm": "摘要算法",
    "review.field.scriptCode": "Script code（BIP143）",
    "review.field.vsizeRange": "签名后大小",
    "review.field.feeRateRange": "费率",
    "review.field.unsignedTemplateHash": "未签名模板哈希",
    "review.field.parentTransaction": "资金来源交易（完整）",
    "review.field.parentTxid": "资金来源交易 ID",
    "review.field.outputScriptIndexed": "输出 {{index}} 脚本",
    "review.note.sameAccountReturn": "退回此钱包账户",
    "review.note.scriptP2pkh": "P2PKH（Legacy）",
    "review.note.scriptP2sh": "P2SH",
    "review.note.scriptP2wpkh": "P2WPKH（Native SegWit）",
    "review.note.scriptP2wsh": "P2WSH",
    "review.note.scriptP2tr": "P2TR（Taproot）",
    "review.note.legacyAmountFromParent": "与详情中的完整资金来源交易一致；不证明它已确认或未被花费",
    "review.note.legacyNoAmountField": "Legacy 签名不直接承诺此金额；由已校验的资金来源交易间接绑定",
    "review.note.nativeAmountSupplied": "由请求方提供并由本签名承诺；未经链上独立验证",
    "review.note.nativeFormOnly": "仅为请求的 Native 形式；广播前请核对实际输出和组装方式",
    "review.field.requestedPrevoutScript": "请求的被花费输出脚本",
    "review.note.requestedNativeScript": "按请求为此密钥的 Native SegWit 脚本；签名不能证明被花费的输出用的就是它",
    "review.note.scriptFromParent": "与资金交易中被选中的输出一致",
    "review.note.notFinalTxid": "不是最终交易 ID：Legacy 签名会改变它",
    "review.note.finalTxidUnchanged": "也是最终交易 ID；见证数据只改变 wtxid",
    "review.note.sizeRange": "具体大小取决于签名；手续费上限按最小大小计算",
    "review.field.signingKey": "签名密钥",
    "review.field.signingAddress": "签名地址",
    "review.field.publicKey": "公钥",
    "review.field.from": "转出账户",
    "review.field.to": "转入账户",
    "review.field.fromTokenAccount": "转出代币账户",
    "review.field.toTokenAccount": "转入代币账户",
    "review.field.mint": "代币 Mint",
    "review.field.amount": "数量",
    "review.field.program": "程序",
    "review.field.undecodedInstruction": "未解析指令",
    "review.field.durableNonce": "包含持久 nonce 推进指令",
    "review.field.nonceAccount": "Nonce 账户",
    "review.field.nonceAuthority": "Nonce 授权账户（指令所列）",
    "review.field.recentBlockhashesSysvar": "RecentBlockhashes 系统变量",
    "review.field.instruction": "指令",
    "review.field.instructions": "指令数",
    "review.field.transferSol": "{{index}} · SOL 转账",
    "review.field.transferToken": "{{index}} · 代币转账",
    "review.field.transferTokenNoMint": "{{index}} · 代币转账 · 指令未包含 mint",
    "review.field.moreTransfers": "其余转账",
    "review.field.moreUndecoded": "其余未解析指令",
    "review.field.authority": "授权账户",
    "review.field.accountIndexes": "账户索引",
    "review.field.data": "数据",
    "review.field.messageFormat": "消息格式",
    "review.field.feePayer": "手续费支付方",
    "review.field.blockhashOrNonce": "区块哈希或 nonce 值",
    "review.field.scheme": "签名方案",
    "review.field.account": "账户 {{index}}",
    "review.field.message": "完整消息",
    "review.field.chainId": "链 ID",
    "review.field.contract": "目标地址",
    "review.field.value": "转账金额",
    "review.field.selector": "函数选择器",
    "review.field.type": "类型",
    "review.field.nonce": "Nonce",
    "review.field.gas": "Gas 上限",
    "review.field.maxFeePerGas": "每单位 Gas 最高费用",
    "review.field.gasPrice": "Gas 价格",
    "review.field.maxPriorityFeePerGas": "每单位 Gas 最高优先费",
    "review.field.call": "调用",
    "review.field.argument": "参数 {{index}} · {{name}}",
    "review.field.initCode": "合约初始化代码",
    "review.field.calldata": "完整 calldata",
    "review.field.unsignedTransaction": "未签名交易",
    "review.field.digest": "签名摘要",
    "review.note.wei": "wei",
    "review.note.baseUnits": "最小单位",
    "review.note.decimals": "小数位 {{count}}",
    "review.note.bytes": "{{count}} 字节",
    "review.note.dataBytes": "数据 {{count}} 字节",
    "review.note.notDecoded": "未解析",
    "review.note.transfersDecoded": "{{count}} 笔已解析转账",
    "review.note.instructionsNotDecoded": "{{count}} 条未解析",
    "review.note.nonceAdvances": "共 {{count}} 次 nonce 推进",
    "review.note.moreInDetails": "还有 {{count}} 笔，见详情",
    "review.note.moreInstructionsInDetails": "还有 {{count}} 条，见详情",
    "review.note.argumentsInDetails": "{{count}} 个参数见详情",
    "review.note.contractCreation": "创建合约",
    "review.note.signer": "签名者",
    "review.note.writable": "可写",
    "review.note.readOnly": "只读",
    "review.note.systemProgram": "System Program",
    "review.note.tokenProgram": "SPL Token Program",
    "review.group.request": "请求来源",
    "review.group.transaction": "交易",
    "review.group.accounts": "消息内账户",
    "review.group.instruction": "指令 {{index}}",
    "review.group.call": "已解析调用",
    "review.group.exact": "待签名的完整内容",
    "bitcoin.signingIdentity": `BIP341 Taproot 密钥路径 Schnorr（BIP86 空脚本树）
内部公钥：{{key}}`,
    "bitcoin.title": "审核 Bitcoin Taproot 支付",
    "bitcoin.network": "网络（请求方选择）：{{network}}",
    "bitcoin.networkWarning": "这个网络标签不是链上证明。请独立核对收款地址和资金来源；不要因为是测试网或 Signet 就降低警惕。",
    "bitcoin.input": "输入：{{txid}}:{{vout}}",
    "bitcoin.inputAmount": "输入金额：{{sats}} sats（{{btc}} BTC）",
    "bitcoin.outputs": "输出",
    "bitcoin.output": "输出 {{index}}：{{address}} · {{sats}} sats（{{btc}} BTC）· {{role}}",
    "bitcoin.sameKeyReturn": "同一密钥找零",
    "bitcoin.recipient": "收款方",
    "bitcoin.fee": "手续费：{{sats}} sats（{{btc}} BTC）· {{rate}}",
    "bitcoin.vsize": "签名后 vsize：{{vsize}} vB",
    "bitcoin.rbfSignals": "此交易会发出 RBF 信号。",
    "bitcoin.rbfDoesNotSignal": "此交易不会发出 RBF 信号。",
    "bitcoin.netLeaving": "离开此密钥的净金额：{{sats}} sats（{{btc}} BTC）",
    "bitcoin.unsignedTxid": "未签名交易 ID：{{txid}}",
    "bitcoin.outputKey": "Taproot 输出密钥 Q：{{key}}",
    "bitcoin.highFeeWarning": "警告：手续费高于 V1 审核阈值（100 sat/vB 或输入金额的 2%）。",
    "sign.blindNotice": "部分内容未解析，批准即为盲签。",
    "evm.title": "EVM 交易，链 ID {{chainId}}",
    "evm.type": "类型：{{type}}",
    "evm.to": "目标地址：{{to}}",
    "evm.contractCreation": "目标地址：（创建合约）",
    "evm.value": "转账金额：{{value}} wei",
    "evm.nonce": "nonce：{{value}}",
    "evm.gas": "gas：{{value}}",
    "evm.gasPrice": "gasPrice：{{value}} wei",
    "evm.maxFeePerGas": "maxFeePerGas：{{value}} wei",
    "evm.maxPriorityFeePerGas": "maxPriorityFeePerGas：{{value}} wei",
    "evm.call": "调用：{{call}}",
    "evm.selector": "函数选择器：{{selector}}",
    "evm.selectorNone": "函数选择器：无",
    "evm.calldata": "calldata（{{bytes}} 字节，完整）：{{calldata}}",
    "evm.calldataNone": "calldata：无",
    "release.title": "批准保护档访问",
    "release.subtitle": "{{count}} 个保护档项目",
    "release.confirm": "确认批准",
    "policyDowngrade.title": "关闭「签名前问我」",
    "policyDowngrade.confirm": "关闭确认",
    "policyDowngrade.standardWarning": "关闭后，Agent 用它签名时不再问你。这个确认只防误操作；要保护资金，请用保护档。",
    "policyDowngrade.protectedNotice": "保护档密钥每次签名仍要在浏览器里用 Passkey 批准。",
    "policyDowngrade.setting": "设置",
    "policyDowngrade.settingValue": "签名前问我：开 → 关",
    "policyDowngrade.key": "密钥",
    "policyDowngrade.tier": "档位",
    "policyDowngrade.standard": "标准档",
    "policyDowngrade.protected": "保护档",
    "policyDowngrade.account": "账户",
    "policyDowngrade.accountNamed": "#{{index}} {{name}}",
    "policyDowngrade.accountUnnamed": "#{{index}}（未命名）",
    "policyDowngrade.appliesTo": "适用于",
    "policyDowngrade.appliesToValue": "这个账户在 ETH、BTC、Solana 上的签名",
    "policyDowngrade.onlyPolicy": "这一步只改这个设置，不会签名，也不会动密钥。",
    "policyDowngrade.details": "请求信息",
    "policyDowngrade.serverFacts": "服务器断言的目标信息",
    "policyDowngrade.eth": "EVM 地址",
    "policyDowngrade.btcLegacy": "Bitcoin Legacy 地址",
    "policyDowngrade.btcSegwit": "Bitcoin SegWit 地址",
    "policyDowngrade.btcTaproot": "Bitcoin Taproot 地址",
    "policyDowngrade.solana": "Solana 地址",
    "policyDowngrade.recordId": "记录 ID",
    "policyDowngrade.identity": "身份摘要",
    "policyDowngrade.revision": "记录修订号",
    "policyDowngrade.expires": "授权过期时间",
    "policyDowngrade.challengeId": "挑战 ID",
    "policyDowngrade.notUsed": "此签名者未使用",
    "context.session": "会话",
    "context.command": "命令",
    "context.egress": "外联",
    "context.source": "来源",
    "context.sourceEnv": "环境变量",
    "context.signReturns": "返回内容",
    "context.sourceRequestedName": "请求名称",
    "context.sourceTag": "标签",
    "context.sourceSkill": "技能",
    "context.agent": "Agent",
    "context.grant": "授权",
    "context.agentAccess": "Agent 授权时长",
    "context.secrets": "保护档",
    "context.kindStatic": "静态值",
    "context.kindKeypair": "签名密钥",
    "context.oneTime": "一次性",
    "context.minutes": "{{count}} 分钟",
    "context.seconds": "{{count}} 秒"
  }
};
let Rs = "en";
function ok(e) {
  return e === "zh" ? "zh" : "en";
}
function sk(e) {
  const t = ok(e);
  return t === Rs ? !1 : (Rs = t, !0);
}
function ak() {
  return Rs;
}
function ck(e) {
  return typeof e == "string" && Object.hasOwn(Rc.en, e);
}
function J(e, t = {}) {
  const n = Rc[Rs], r = Rc.en;
  return (n[e] ?? r[e] ?? e).replace(/\{\{(\w+)\}\}/g, (o, s) => String(t[s] ?? ""));
}
function lk(e) {
  return JSON.stringify(e ?? {});
}
function Yu(e) {
  if (!e) return {};
  try {
    const t = JSON.parse(e);
    return typeof t == "object" && t !== null ? t : {};
  } catch {
    return {};
  }
}
function ge(e, t, n) {
  e.dataset.i18nKey = t, e.dataset.i18nParams = lk(n), e.textContent = J(t, n);
}
function Ue(e, t) {
  delete e.dataset.i18nKey, delete e.dataset.i18nParams, e.textContent = t;
}
function dk(e = document) {
  e.querySelectorAll("[data-i18n-key]").forEach((t) => {
    const n = t.dataset.i18nKey;
    n && (t.textContent = J(n, Yu(t.dataset.i18nParams)));
  }), e.querySelectorAll("[data-i18n-placeholder-key]").forEach((t) => {
    const n = t.dataset.i18nPlaceholderKey;
    n && (t.placeholder = J(n, Yu(t.dataset.i18nPlaceholderParams)));
  });
}
function cw(e) {
  return {
    title: "policyDowngrade.title",
    subtitle: { text: e.target.name },
    confirmLabel: "policyDowngrade.confirm",
    policyDowngrade: e
  };
}
function Lt(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
function st(e, t, n = "mono") {
  const r = Lt("div", "review-field"), i = Lt("span", "review-label");
  ge(i, e);
  const o = Lt("div", "review-value"), s = Lt("span", n === "word" ? "review-word" : "review-mono");
  return typeof t == "string" ? Ue(s, t) : ge(s, t.key, t.params), o.append(s), r.append(i, o), r;
}
function Po(e, t) {
  const n = Lt("p", t);
  return ge(n, e), n;
}
function uk(e, t) {
  Ue(e, "");
  const { target: n } = t, r = Lt("div", "review policy-downgrade-review");
  r.append(n.protection === "standard" ? Po("policyDowngrade.standardWarning", "review-warning") : Po("policyDowngrade.protectedNotice", "review-info"));
  const i = Lt("div", "review-summary");
  i.append(
    st("policyDowngrade.setting", { key: "policyDowngrade.settingValue" }, "word"),
    st("policyDowngrade.key", n.name),
    st("policyDowngrade.tier", { key: n.protection === "standard" ? "policyDowngrade.standard" : "policyDowngrade.protected" }, "word")
  ), n.account !== null && i.append(
    st("policyDowngrade.account", n.accountName !== null ? { key: "policyDowngrade.accountNamed", params: { index: n.account, name: n.accountName } } : { key: "policyDowngrade.accountUnnamed", params: { index: n.account } }, "word"),
    st("policyDowngrade.appliesTo", { key: "policyDowngrade.appliesToValue" }, "word")
  ), r.append(i, Po("policyDowngrade.onlyPolicy", "confirmation-note"));
  const o = Lt("details", "review-details"), s = Lt("summary", "review-details-toggle");
  ge(s, "policyDowngrade.details");
  const a = Lt("div", "review-details-body"), c = [
    ["eth", "policyDowngrade.eth"],
    ["btc_legacy", "policyDowngrade.btcLegacy"],
    ["btc_segwit", "policyDowngrade.btcSegwit"],
    ["btc_taproot", "policyDowngrade.btcTaproot"],
    ["solana", "policyDowngrade.solana"]
  ];
  a.append(Po("policyDowngrade.serverFacts", "confirmation-note"));
  for (const [l, d] of c) {
    const u = n.addresses[l];
    u !== null ? a.append(st(d, u)) : n.signerKind === "hd-seed" && a.append(st(d, { key: "policyDowngrade.notUsed" }));
  }
  a.append(
    st("policyDowngrade.recordId", n.secretId),
    st("policyDowngrade.identity", n.identitySha256),
    st("policyDowngrade.revision", t.change.revision),
    st("policyDowngrade.expires", t.expiresAt),
    st("policyDowngrade.challengeId", t.challengeId)
  ), o.append(s, a), r.append(o), e.append(r);
}
const lw = {
  sol: { symbol: "SOL", baseUnit: "lamports", decimals: 9 },
  eth: { symbol: "ETH", baseUnit: "wei", decimals: 18 },
  btc: { symbol: "BTC", baseUnit: "sats", decimals: 8 }
}, Jl = /^(?:0|[1-9][0-9]*)$/;
function dw(e, t) {
  if (t === 0) return e;
  const n = e.padStart(t + 1, "0"), r = n.slice(-t).replace(/0+$/, "");
  return `${n.slice(0, -t)}${r ? `.${r}` : ""}`;
}
function Xu(e, t, n) {
  const { symbol: r, baseUnit: i, decimals: o } = lw[t];
  if (!Jl.test(e) || n === "base") return `${e} ${i}`;
  const s = `${dw(e, o)} ${r}`;
  return n === "symbol" ? s : `${s} (${e} ${i})`;
}
const fk = 253402300799n;
function hk(e) {
  if (!Jl.test(e) || e.length > 12 || BigInt(e) > fk) return null;
  const t = new Date(Number(e) * 1e3).toISOString(), n = t.slice(17, 19);
  return `${t.slice(0, 10)} ${t.slice(11, 16)}${n === "00" ? "" : `:${n}`} UTC`;
}
const Ls = 8, Hs = 6, Ju = 28;
function Qu(e, t) {
  if (!t || e.length <= Ls + Hs + 2) {
    const r = ie("span", "review-mono");
    return Ue(r, e), r;
  }
  const n = ie("button", "review-mono review-elided");
  return n.type = "button", n.setAttribute("aria-expanded", "false"), n.title = e, Ue(n, `${e.slice(0, Ls)}…${e.slice(-Hs)}`), n.addEventListener("click", () => {
    n.getAttribute("aria-expanded") !== "true" && (n.setAttribute("aria-expanded", "true"), Ue(n, e));
  }), n;
}
function ie(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
const pk = "http://www.w3.org/2000/svg";
function $o(e, t) {
  const n = document.createElementNS(pk, e);
  for (const [r, i] of Object.entries(t)) n.setAttribute(r, i);
  return n;
}
function ef() {
  const e = $o("svg", {
    class: "review-notice-icon",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true"
  });
  return e.append(
    $o("path", { d: "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" }),
    $o("path", { d: "M12 9v4.5" }),
    $o("path", { d: "M12 17h.01" })
  ), e;
}
function yk(e) {
  if (!e.notes?.length) return null;
  const t = ie("span", "review-notes");
  for (const n of e.notes) {
    const r = ie("span", "review-note");
    typeof n == "string" ? ge(r, n) : ge(r, n.key, { count: n.count }), t.append(r);
  }
  return t;
}
function qa(e, t = "review-label") {
  const n = ie(e.heading ? "p" : "span", t), r = {
    ...e.index === void 0 ? {} : { index: e.index },
    ...e.name === void 0 ? {} : { name: e.name }
  };
  return ge(n, e.label, Object.keys(r).length > 0 ? r : void 0), n;
}
function wk(e, t) {
  const n = ie("button", "review-copy");
  return n.type = "button", ge(n, "review.copy"), n.addEventListener("click", () => {
    const r = () => {
      window.getSelection()?.selectAllChildren(e), ge(n, "review.copySelected");
    };
    let i;
    try {
      i = navigator.clipboard.writeText(t);
    } catch {
      r();
      return;
    }
    i.then(() => ge(n, "review.copied"), r);
  }), n;
}
function Oo(e, t = !1) {
  if (e.heading) return qa(e, "review-subheading");
  const n = ie("div", e.raw ? "review-field review-field-raw" : "review-field"), r = yk(e);
  if (e.raw) {
    const c = ie("div", "review-raw-head"), l = ie("pre", "review-raw");
    return Ue(l, e.value), c.append(qa(e)), r && c.append(r), c.append(wk(l, e.value)), n.append(c, l), n;
  }
  const i = ie("div", "review-value"), o = e.decimals !== void 0 && Jl.test(e.value) ? dw(e.value, e.decimals) : null, s = e.time ? hk(e.value) : null, a = t && e.shownAs ? 0 : e.native ? Xu(e.value, e.native, e.unit).length : o !== null || e.time ? Ju + 1 : t && e.elide && e.value.length > Ls + Hs + 2 ? Ls + Hs + 1 : e.value.length;
  if (t && e.to === void 0 && a + (e.secondary?.length ?? 0) <= Ju && n.classList.add("review-field-inline"), t && e.shownAs) {
    const c = ie("span", "review-word");
    ge(c, e.shownAs), i.append(c);
  } else if (e.native) {
    const c = ie("span", "review-mono");
    Ue(c, Xu(e.value, e.native, e.unit)), i.append(c);
  } else if (o !== null) {
    const c = ie("span", "review-mono");
    Ue(c, o);
    const l = ie("span", "review-exact");
    ge(l, "review.amount.baseUnitsDecimals", { value: e.value, count: e.decimals }), i.append(c, l);
  } else if (e.time) {
    const c = ie("span", "review-mono");
    Ue(c, s ?? e.value);
    const l = ie("span", "review-exact");
    s === null ? ge(l, "review.time.noDate") : ge(l, "review.time.raw", { value: e.value }), i.append(c, l);
  } else
    i.append(Qu(e.value, t && e.elide === !0));
  if (e.secondary !== void 0) {
    const c = ie("span", "review-mono review-secondary");
    Ue(c, e.secondary), i.append(c);
  }
  if (e.to !== void 0) {
    const c = ie("span", "review-arrow");
    Ue(c, "→"), i.append(c, Qu(e.to, t));
  }
  return r && i.append(r), n.append(qa(e), i), n;
}
function tf(e, t, n) {
  const r = ie("section", "review-group"), i = ie("h2", "review-group-title");
  return ge(i, e, t === void 0 ? void 0 : { index: t }), r.append(i, ...n), r;
}
function gk(e, t, n = {}) {
  Ue(e, "");
  const r = ie("div", "review"), i = ie("div", "review-heading"), o = ie("p", "review-kind");
  if (ge(o, t.kind), i.append(o), n.session) {
    const d = ie("p", "review-session"), u = ie("span", "review-session-label");
    ge(u, "context.session");
    const f = ie("span", "review-session-value");
    Ue(f, n.session), d.append(u, f), i.append(d);
  }
  r.append(i);
  for (const d of t.warnings ?? []) {
    const u = ie("p", "review-warning"), f = ie("span", "review-warning-text");
    ge(f, d), u.append(ef(), f), r.append(u);
  }
  if (t.undecoded) {
    const d = ie("p", "review-notice"), u = ie("span", "review-notice-text");
    ge(u, "sign.blindNotice"), d.append(ef(), u), r.append(d);
  }
  const s = ie("div", "review-summary");
  s.append(...t.summary.map((d) => Oo(d, !0)), Oo(t.signer, !0)), r.append(s);
  const a = ie("details", "review-details"), c = ie("summary", "review-details-toggle");
  ge(c, "review.details");
  const l = ie("div", "review-details-body");
  t.details.forEach((d, u) => {
    const f = d.fields.map((h) => Oo(h));
    l.append(tf(d.title, d.index, u === 0 ? [Oo(t.signer), ...f] : f));
  }), n.rows && l.append(tf("review.group.request", void 0, [n.rows])), a.append(c, l), r.append(a), e.append(r);
}
function Te(e) {
  throw new Error(`signing review is malformed: ${e}`);
}
function Ns(e, t, n) {
  (typeof e != "object" || e === null || Array.isArray(e)) && Te(`${n} must be an object`);
  const r = e;
  for (const i of Object.keys(r)) t.includes(i) || Te(`${n}.${i} is not a review field`);
  return r;
}
function or(e, t, n) {
  return (!ck(e) || !e.startsWith(`${t}.`)) && Te(`${n} must be a ${t} translation key`), e;
}
function Lc(e, t) {
  if (e !== void 0)
    return (typeof e != "number" || !Number.isSafeInteger(e) || e < 0) && Te(`${t} must be a non-negative integer`), e;
}
function _i(e, t) {
  return Array.isArray(e) || Te(`${t} must be a list`), e;
}
const mk = 4096, bk = /^[A-Za-z_][A-Za-z0-9_]{0,63} \([a-z][a-z0-9[\]]{0,31}\)$/;
function Wa(e, t) {
  const n = Ns(e, ["label", "index", "name", "value", "native", "unit", "decimals", "time", "to", "secondary", "elide", "shownAs", "notes", "raw", "heading"], t), r = Lc(n.index, `${t}.index`);
  n.name !== void 0 && (typeof n.name != "string" || (n.label === "review.field.typedField" ? n.name.length > mk : !bk.test(n.name))) && Te(`${t}.name is not a valid field name`), typeof n.value != "string" && Te(`${t}.value must be a string`), n.native !== void 0 && !Object.hasOwn(lw, n.native) && Te(`${t}.native is not a native currency`), n.unit !== void 0 && (n.native === void 0 || n.unit !== "symbol" && n.unit !== "base") && Te(`${t}.unit is not a unit of its currency`), n.decimals !== void 0 && (typeof n.decimals != "number" || !Number.isInteger(n.decimals) || n.decimals < 0 || n.decimals > 255 || n.native !== void 0) && Te(`${t}.decimals must be an encoded decimal count`), n.time !== void 0 && (n.time !== "unix-seconds" || n.native !== void 0 || n.decimals !== void 0) && Te(`${t}.time is not a time form`), n.to !== void 0 && typeof n.to != "string" && Te(`${t}.to must be a string`), n.secondary !== void 0 && typeof n.secondary != "string" && Te(`${t}.secondary must be a string`), n.elide !== void 0 && typeof n.elide != "boolean" && Te(`${t}.elide must be a boolean`);
  const i = n.shownAs === void 0 ? void 0 : or(n.shownAs, "review.value", `${t}.shownAs`);
  n.raw !== void 0 && typeof n.raw != "boolean" && Te(`${t}.raw must be a boolean`), n.heading !== void 0 && typeof n.heading != "boolean" && Te(`${t}.heading must be a boolean`), n.heading && (n.value !== "" || n.native !== void 0 || n.decimals !== void 0 || n.time !== void 0 || n.notes !== void 0 || n.raw !== void 0 || n.to !== void 0 || n.secondary !== void 0 || n.elide !== void 0 || i !== void 0) && Te(`${t} is a heading and cannot carry a value`);
  const o = n.notes === void 0 ? void 0 : _i(n.notes, `${t}.notes`).map((s, a) => {
    const c = `${t}.notes[${a}]`;
    if (typeof s == "string") return or(s, "review.note", c);
    const l = Ns(s, ["key", "count"], c), d = Lc(l.count, `${c}.count`);
    return d === void 0 && Te(`${c}.count is required`), { key: or(l.key, "review.note", `${c}.key`), count: d };
  });
  return {
    label: or(n.label, "review.field", `${t}.label`),
    ...r === void 0 ? {} : { index: r },
    ...n.name === void 0 ? {} : { name: n.name },
    value: n.value,
    ...n.native === void 0 ? {} : { native: n.native },
    ...n.unit === void 0 ? {} : { unit: n.unit },
    ...n.decimals === void 0 ? {} : { decimals: n.decimals },
    ...n.time === void 0 ? {} : { time: "unix-seconds" },
    ...n.to === void 0 ? {} : { to: n.to },
    ...n.secondary === void 0 ? {} : { secondary: n.secondary },
    ...n.elide === void 0 ? {} : { elide: n.elide },
    ...i === void 0 ? {} : { shownAs: i },
    ...o === void 0 ? {} : { notes: o },
    ...n.raw === void 0 ? {} : { raw: n.raw },
    ...n.heading === void 0 ? {} : { heading: n.heading }
  };
}
function uw(e) {
  const t = Ns(e, ["kind", "signer", "summary", "undecoded", "warnings", "details"], "review");
  typeof t.undecoded != "boolean" && Te("review.undecoded must be a boolean");
  const n = t.warnings === void 0 ? void 0 : _i(t.warnings, "review.warnings").map((r, i) => or(r, "review.warning", `review.warnings[${i}]`));
  return {
    kind: or(t.kind, "review.kind", "review.kind"),
    signer: Wa(t.signer, "review.signer"),
    summary: _i(t.summary, "review.summary").map((r, i) => Wa(r, `review.summary[${i}]`)),
    undecoded: t.undecoded,
    ...n === void 0 ? {} : { warnings: n },
    details: _i(t.details, "review.details").map((r, i) => {
      const o = `review.details[${i}]`, s = Ns(r, ["title", "index", "fields"], o), a = Lc(s.index, `${o}.index`);
      return {
        title: or(s.title, "review.group", `${o}.title`),
        ...a === void 0 ? {} : { index: a },
        fields: _i(s.fields, `${o}.fields`).map((c, l) => Wa(c, `${o}.fields[${l}]`))
      };
    })
  };
}
let Ql = null, ed = 0;
function vk(e) {
  Ql = e;
}
function fw() {
  return Date.now() < ed;
}
function xk() {
  ed = Date.now() + 150, Ql?.("ui.show");
}
function kk() {
  ed = 0, Ql?.("ui.hide");
}
function Ek() {
  const e = document.getElementById("page"), t = e?.querySelector(".card"), n = e?.querySelector("h1"), r = e?.querySelector(".sub"), i = e?.querySelector(".body"), o = e?.querySelector(".card-scroll"), s = e?.querySelector(".card-footer");
  if (!t || !n || !r || !i || !o || !s)
    throw new Error("sandbox UI is unavailable");
  return {
    card: t,
    title: n,
    subtitle: r,
    body: i,
    scroll: o,
    footer: s
  };
}
function hw(e) {
  e.querySelectorAll(".dynamic").forEach((t) => t.remove());
}
function _e(e) {
  return { text: e };
}
function Ce(e, t) {
  return { key: e, params: t };
}
function X(e, t) {
  if (typeof t == "string") {
    ge(e, t);
    return;
  }
  if ("text" in t) {
    Ue(e, t.text);
    return;
  }
  ge(e, t.key, t.params);
}
function ya(e, t, n) {
  const r = Ek();
  return document.body.classList.add("interactive"), document.body.classList.remove("confirming"), xk(), r.card.classList.remove("confirm-card"), X(r.title, e), X(r.subtitle, t), n ? X(r.body, n) : Ue(r.body, ""), hw(r.card), r;
}
function wa() {
  document.body.classList.remove("interactive"), document.body.classList.remove("confirming");
  const e = document.getElementById("page")?.querySelector(".card");
  e && (e.classList.remove("confirm-card"), hw(e)), kk();
}
function Q(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic"), (e.querySelector(".card-scroll") ?? e).append(t);
}
function nf(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic");
  const n = e.querySelector(".card-footer");
  if (!n) throw new Error("sandbox confirmation footer is unavailable");
  n.append(t);
}
function Sk(e) {
  const t = document.createElement("div");
  if (t.className = "detail-value", e.variant === "chips") {
    t.classList.add("detail-chips");
    for (const r of e.values) {
      const i = document.createElement("span");
      i.className = `detail-chip${e.tone === "warning" ? " detail-chip-warning" : ""}`, X(i, r), t.append(i);
    }
    return t;
  }
  const n = document.createElement(e.variant === "command" ? "code" : "span");
  return n.className = e.variant === "command" ? "detail-command" : e.variant === "mono" ? "detail-mono" : "detail-text", X(n, e.value), e.variant === "mono" && (n.title = n.textContent ?? ""), t.append(n), t;
}
function pw(e) {
  const t = document.createElement("div");
  t.className = "confirmation-details";
  for (const n of e.rows) {
    const r = document.createElement("div");
    r.className = "detail-row";
    const i = document.createElement("span");
    i.className = "detail-label", X(i, n.label), r.append(i, Sk(n)), t.append(r);
  }
  return t;
}
function Ak(e, t, n) {
  if (Ue(e, ""), e.append(pw(t)), n) {
    const r = document.createElement("p");
    r.className = "confirmation-note", X(r, n), e.append(r);
  }
}
function Ht(e) {
  const t = document.createElement("button");
  return t.type = "button", t.className = "action dynamic", X(t, e), t;
}
function Us(e = _e("")) {
  const t = document.createElement("p");
  return t.className = "setup-status dynamic", X(t, e), t;
}
let Co = null;
function td() {
  return new Error("operation-superseded");
}
function Ci(e) {
  const t = e.reason;
  return t instanceof Error ? t : td();
}
function rf(e) {
  if (e?.aborted) throw Ci(e);
}
function mr(e) {
  Co?.abort(td());
  const t = new AbortController();
  return Co = t, Promise.resolve().then(() => e(t.signal)).finally(() => {
    Co === t && (Co = null);
  });
}
async function _k(e) {
  return mr(async (t) => {
    rf(t), rf(e.abortSignal);
    const n = new TextDecoder().decode(e.plaintext), r = ya("plaintext.title", _e(e.name), "plaintext.body"), i = document.createElement("pre");
    i.className = "plaintext dynamic", i.textContent = n;
    const o = Ht("common.done"), s = Ht("plaintext.copy");
    s.classList.add("secondary");
    const a = Us("plaintext.copyWarning");
    Q(r.card, i), Q(r.card, a), Q(r.card, s), Q(r.card, o);
    try {
      await new Promise((c, l) => {
        if (t.aborted) {
          l(Ci(t));
          return;
        }
        if (e.abortSignal?.aborted) {
          l(Ci(e.abortSignal));
          return;
        }
        let d = !1, u = () => {
        }, f = null, h = null, y = !e.prepareCopy;
        const w = () => {
          f?.abort(td());
        }, p = (O) => {
          d || (d = !0, u(), w(), O());
        }, g = (O) => {
          p(() => l(Ci(O)));
        }, k = () => g(t), b = () => {
          e.abortSignal && g(e.abortSignal);
        };
        u = () => {
          t.removeEventListener("abort", k), e.abortSignal?.removeEventListener("abort", b);
        }, t.addEventListener("abort", k, { once: !0 }), e.abortSignal?.addEventListener("abort", b, { once: !0 }), s.addEventListener("click", () => {
          if (h) return;
          if (t.aborted) {
            g(t);
            return;
          }
          if (e.abortSignal?.aborted) {
            g(e.abortSignal);
            return;
          }
          s.disabled = !0;
          const O = new AbortController();
          f = O;
          const B = () => {
            try {
              return Promise.resolve(e.onCopy ? e.onCopy(n, O.signal) : navigator.clipboard.writeText(n));
            } catch (H) {
              return Promise.reject(H);
            }
          }, R = e.prepareCopy && !y ? Promise.resolve(e.prepareCopy(O.signal)).then(() => {
            O.signal.aborted || t.aborted || e.abortSignal?.aborted || (y = !0, X(a, "plaintext.copyReady"), s.disabled = !1);
          }) : B().then(() => {
            O.signal.aborted || t.aborted || e.abortSignal?.aborted || (e.prepareCopy && (y = !1), X(a, "plaintext.copiedBody"), s.disabled = !1);
          });
          h = R, R.then(
            () => {
              O.signal.aborted;
            },
            (H) => {
              if (!(O.signal.aborted || d)) {
                if (t.aborted) {
                  g(t);
                  return;
                }
                if (e.abortSignal?.aborted) {
                  g(e.abortSignal);
                  return;
                }
                s.disabled = !1, X(a, "plaintext.copyFailed");
              }
            }
          ).finally(() => {
            f === O && (f = null), h === R && (h = null);
          });
        }), o.addEventListener(
          "click",
          () => {
            p(c);
          },
          { once: !0 }
        );
      });
    } finally {
      wa();
    }
  });
}
function Ki(e, t, n, r) {
  const i = ya(e.title, e.subtitle, e.review ? void 0 : e.body);
  document.body.classList.add("confirming"), i.card.classList.add("confirm-card"), e.policyDowngrade ? uk(i.body, e.policyDowngrade) : e.review ? gk(i.body, e.review, {
    session: e.reviewSession,
    rows: e.details && pw(e.details)
  }) : e.details && Ak(i.body, e.details, e.body);
  const o = Ht(e.confirmLabel), s = Ht("common.cancel");
  return s.classList.add("secondary"), nf(i.card, s), nf(i.card, o), o.disabled = !0, new Promise((a, c) => {
    let l = !1, d;
    const u = setTimeout(() => {
      try {
        d = n?.(i.card);
      } catch (w) {
        h(() => c(w));
        return;
      }
      (d?.ready ?? Promise.resolve()).then(
        () => {
          !l && !t.aborted && (o.disabled = !1);
        },
        (w) => h(() => c(w))
      );
    }, 500), f = () => {
      h(() => c(Ci(t)));
    }, h = (y) => {
      l || (l = !0, clearTimeout(u), d?.dispose(), t.removeEventListener("abort", f), wa(), y());
    };
    if (t.aborted) {
      f();
      return;
    }
    t.addEventListener("abort", f, { once: !0 }), o.addEventListener("click", () => {
      if (o.disabled) return;
      o.disabled = !0, s.disabled = !0;
      let y;
      try {
        d?.assertCurrent(), y = r ? r() : Promise.resolve();
      } catch (w) {
        h(() => c(w));
        return;
      }
      y.then(
        () => h(a),
        (w) => h(() => c(w))
      );
    }), s.addEventListener(
      "click",
      () => {
        h(() => c(new Error("operation-cancelled")));
      },
      { once: !0 }
    );
  });
}
function Ds(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function Ik(e) {
  return new Promise((t, n) => {
    const r = () => n(Ds(e));
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function of(e) {
  if (e?.aborted) throw Ds(e);
}
async function yw(e) {
  const t = Yl(e.wrapMeta), n = ca(t.wrapMeta);
  let r;
  try {
    const i = await Promise.race([
      pa(n, e.currentRpId, e.abortSignal).then(
        (o) => {
          if (e.abortSignal?.aborted)
            throw o.prfOutput.fill(0), Ds(e.abortSignal);
          return o;
        },
        (o) => {
          throw e.abortSignal?.aborted ? Ds(e.abortSignal) : o;
        }
      ),
      ...e.abortSignal ? [Ik(e.abortSignal)] : []
    ]);
    return r = i.prfOutput, await ww({
      wrapMeta: t.wrapMeta,
      currentRpId: e.currentRpId,
      assertion: i,
      ...e.abortSignal ? { abortSignal: e.abortSignal } : {},
      ...e.policy ? { policy: e.policy } : {},
      ...e.onCommitted ? { onCommitted: e.onCommitted } : {}
    });
  } finally {
    r?.fill(0);
  }
}
async function ww(e) {
  const t = Yl(e.wrapMeta);
  let n;
  try {
    of(e.abortSignal), n = await Py(t.wrapMeta, {
      kind: "passkey",
      prfOutput: e.assertion.prfOutput,
      prfSalt: e.assertion.prfSalt
    }), of(e.abortSignal);
    const r = Qy({
      vmk: n,
      wrapMeta: t.wrapMeta,
      freshSetup: !1,
      scopeId: t.scopeId,
      ...e.policy ? { policy: e.policy } : {},
      ...e.onCommitted ? { onCommitted: e.onCommitted } : {}
    });
    return n = void 0, { state: r.state, rpId: e.currentRpId, expiresAt: r.expiresAt };
  } finally {
    e.assertion.prfOutput.fill(0), n?.fill(0);
  }
}
function Bk(e) {
  if (typeof e != "object" || e === null)
    throw new _("invalid_payload", "request payload must be an object");
  return e;
}
function Tk(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new _("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function sf(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new _("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Pk(e) {
  const t = Bk(e), n = t.kind === "keypair" ? "keypair" : t.kind === "static" ? "static" : null;
  if (!n) throw new _("invalid_payload", "kind must be static or keypair");
  const r = sf(t.name, "name");
  if (t.rootMetadata !== void 0) throw new _("invalid_payload", "seal cannot set vault root metadata");
  const i = Tk(t.wrapMeta, "wrapMeta");
  if (n === "static") {
    if (t.curve !== void 0) throw new _("invalid_payload", "static seal cannot carry a curve");
    if (t.inputMode !== "parent-value")
      throw new _("invalid_payload", "static seal inputMode must be parent-value");
    return {
      name: r,
      kind: "static",
      inputMode: "parent-value",
      value: sf(t.value, "value"),
      wrapMeta: i
    };
  }
  if (t.inputMode !== void 0)
    throw new _("invalid_payload", "keypair seal is generate-only and does not accept inputMode");
  if (t.value !== void 0)
    throw new _("invalid_payload", "keypair seal cannot accept parent-provided private key material");
  let o;
  try {
    o = ci(t.curve);
  } catch {
    throw new _("invalid_payload", "unsupported signing curve");
  }
  return {
    name: r,
    kind: "keypair",
    ...t.curve !== void 0 ? { curve: o } : {},
    wrapMeta: i
  };
}
function di(e) {
  const t = /* @__PURE__ */ new Set();
  function n(i) {
    for (let o = 0; o < i.length; o += 1) {
      const s = i.charCodeAt(o);
      if (s >= 55296 && s <= 56319) {
        const a = i.charCodeAt(++o);
        if (!(a >= 56320 && a <= 57343)) throw new Error("signing context contains an unpaired UTF-16 surrogate");
      } else if (s >= 56320 && s <= 57343)
        throw new Error("signing context contains an unpaired UTF-16 surrogate");
    }
  }
  function r(i) {
    if (!(i === null || typeof i == "boolean")) {
      if (typeof i == "string") {
        n(i);
        return;
      }
      if (typeof i == "number") {
        if (!Number.isSafeInteger(i)) throw new Error("signing context numbers must be safe integers; use strings for large quantities");
        return;
      }
      if (typeof i != "object") throw new Error("signing context must contain only JSON values");
      if (!Array.isArray(i) && Object.getPrototypeOf(i) !== Object.prototype && Object.getPrototypeOf(i) !== null)
        throw new Error("signing context must contain only plain JSON objects");
      if (t.has(i)) throw new Error("signing context must not contain cycles");
      if (t.add(i), Array.isArray(i))
        for (const o of i) r(o);
      else
        for (const [o, s] of Object.entries(i))
          n(o), r(s);
      t.delete(i);
    }
  }
  r(e);
}
const $k = 512, gw = "avibe-vault-signed-context-replay:v2", Ok = "avibe-vault-signed-context-replay:v2", Ck = "avibe-vault-signed-context-replay:v2", bt = /* @__PURE__ */ new Map();
let af = !1, Ii = null;
function yt(e) {
  return typeof e == "object" && e !== null;
}
function Qt(e, t) {
  if (typeof e != "string" || e.length === 0) throw new _("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function gi(e, t) {
  if (e !== void 0) {
    if (typeof e != "string") throw new _("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ga(e, t) {
  if (e !== void 0) {
    if (!Array.isArray(e) || !e.every((n) => typeof n == "string"))
      throw new _("invalid_payload", `${t} must be a string array`);
    return e;
  }
}
function Rk(e, t) {
  if (e !== "static" && e !== "keypair") throw new _("invalid_payload", `${t} must be static or keypair`);
  return e;
}
function Lk(e) {
  if (e !== "agent-deliver" && e !== "sign" && e !== "reveal")
    throw new _("invalid_payload", "context purpose is invalid");
  return e;
}
function Hc(e, t) {
  if (typeof e != "number" || !Number.isSafeInteger(e) || e < 0)
    throw new _("invalid_payload", `${t} must be a non-negative integer`);
  return e;
}
function Hk(e, t) {
  if (!Array.isArray(e) || e.length < 16 || e.length > 128 || !e.every((n) => Number.isInteger(n) && n >= 0 && n <= 255))
    throw new _("invalid_payload", `${t} must contain 16..128 byte integers`);
  return e;
}
function nd(e, t) {
  if (!yt(e)) throw new _(t, "agent delivery context.release must be an object");
  return {
    name: Qt(e.name, "context.release.name"),
    ttlSecs: Hc(e.ttlSecs, "context.release.ttlSecs"),
    approvalNonce: Hk(e.approvalNonce, "context.release.approvalNonce"),
    approvalExpiresAtUnix: Hc(e.approvalExpiresAtUnix, "context.release.approvalExpiresAtUnix"),
    operationHash: Qt(e.operationHash, "context.release.operationHash")
  };
}
function me(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(me).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${me(t[n])}`).join(",")}}`;
}
function rd(e) {
  const t = yt(e) ? e : null;
  if (!t) throw new _("invalid_payload", "context must be an object");
  if (t.v !== 2) throw new _("invalid_payload", "context version must be 2");
  const n = Lk(t.purpose);
  Qt(t.requestId, "context.requestId"), t.grantId !== void 0 && gi(t.grantId, "context.grantId");
  const r = yt(t.display) ? t.display : null;
  if (!r) throw new _("invalid_payload", "context.display must be an object");
  if (!Array.isArray(r.secrets) || r.secrets.length === 0)
    throw new _("invalid_payload", "context.display.secrets must be a non-empty array");
  r.secrets.forEach((c, l) => {
    if (!yt(c)) throw new _("invalid_payload", `context.display.secrets[${l}] must be an object`);
    Qt(c.name, `context.display.secrets[${l}].name`), Rk(c.kind, `context.display.secrets[${l}].kind`);
  }), r.sessionLabel !== void 0 && gi(r.sessionLabel, "context.display.sessionLabel"), r.command !== void 0 && gi(r.command, "context.display.command"), r.egress !== void 0 && gi(r.egress, "context.display.egress");
  const i = r.source;
  if (i !== void 0) {
    if (!yt(i)) throw new _("invalid_payload", "context.display.source must be an object");
    i.env !== void 0 && Ga(i.env, "context.display.source.env"), i.tags !== void 0 && Ga(i.tags, "context.display.source.tags"), i.skills !== void 0 && Ga(i.skills, "context.display.source.skills");
  }
  r.grantTtlSeconds !== void 0 && Hc(r.grantTtlSeconds, "context.display.grantTtlSeconds");
  const o = yt(t.signature) ? t.signature : null;
  if (!o) throw new _("invalid_payload", "context.signature must be an object");
  if (o.alg !== "ed25519") throw new _("invalid_payload", "context signature alg must be ed25519");
  Qt(o.keyId, "context.signature.keyId"), Qt(o.value, "context.signature.value");
  const s = t.agent;
  if (s !== void 0) {
    if (!yt(s)) throw new _("invalid_payload", "context.agent must be an object");
    const c = yt(s.publicKey) ? s.publicKey : null;
    if (!c) throw new _("invalid_payload", "context.agent.publicKey must be an object");
    Qt(c.public_key, "context.agent.publicKey.public_key"), c.fingerprint !== void 0 && gi(c.fingerprint, "context.agent.publicKey.fingerprint"), Qt(s.fingerprint, "context.agent.fingerprint");
  }
  const a = t.release;
  if (n === "agent-deliver") {
    const c = nd(a, "invalid_payload");
    if (r.grantTtlSeconds === void 0)
      throw new _("invalid_payload", "agent delivery context must display its release TTL");
    if (r.grantTtlSeconds !== c.ttlSecs)
      throw new _("invalid_payload", "signed release TTL does not match display TTL");
  } else if (a !== void 0 && !yt(a))
    throw new _("invalid_payload", "context.release must be an object");
  return Qt(t.expiresAt, "context.expiresAt"), t;
}
function ga(e) {
  const { signature: t, ...n } = e;
  return me(n);
}
function zs(e) {
  if (e.context.purpose !== e.expectedPurpose)
    throw new _("invalid_context", `${e.expectedPurpose} context has the wrong purpose`);
  if (!e.context.display.secrets.some(
    (n) => n.name === e.secret.name && n.kind === e.secret.kind
  )) {
    const n = e.secret.kind === "keypair" ? "key" : "secret";
    throw new _("invalid_context", `${e.expectedPurpose} context does not cover the requested ${n}`);
  }
}
function sr(e) {
  if (e.expectedPurpose && e.context.purpose !== e.expectedPurpose)
    throw new _("invalid_context", "signed context purpose does not match operation");
  const t = e.now ?? Date.now(), n = Date.parse(e.context.expiresAt);
  if (!Number.isFinite(n) || n <= t)
    throw new _("context_expired", "signed context is expired", !0);
  if (!x2({
    rootMetadata: e.rootMetadata,
    keyId: e.context.signature.keyId,
    signature: e.context.signature.value,
    message: ga(e.context)
  })) throw new _("invalid_context_signature", "signed context signature is invalid");
  if (e.context.purpose === "agent-deliver" && nd(e.context.release, "invalid_context").approvalExpiresAtUnix * 1e3 <= t)
    throw new _("context_expired", "signed release approval is expired", !0);
}
function id(e) {
  for (const [t, n] of bt)
    n <= e && bt.delete(t);
}
function Nk() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
function ei() {
  return typeof window < "u";
}
function od() {
  return new _("context_replay_state_unavailable", "signed context replay state is unavailable", !0);
}
function mw(e) {
  const t = Nk();
  if (!t && e && ei()) throw od();
  return t;
}
function bw(e, t) {
  const n = /* @__PURE__ */ new Map();
  if (!yt(e) || e.version !== 2 || !Array.isArray(e.entries)) return n;
  for (const r of e.entries) {
    if (!Array.isArray(r) || r.length !== 2) continue;
    const [i, o] = r;
    typeof i != "string" || i.length === 0 || typeof o != "number" || !Number.isFinite(o) || o > t && n.set(i, Math.max(n.get(i) ?? 0, o));
  }
  return n;
}
function sd(e = Date.now(), t = !1) {
  id(e);
  const n = mw(t);
  if (n)
    try {
      n.setItem(gw, JSON.stringify({ version: 2, entries: [...bt] }));
    } catch {
      if (t && ei()) throw od();
    }
}
function vw(e, t = Date.now(), n = !0) {
  for (const [r, i] of e)
    i > t && bt.set(r, Math.max(bt.get(r) ?? 0, i));
  n && sd(t);
}
function ad() {
  if (!(Ii || typeof BroadcastChannel > "u"))
    try {
      Ii = new BroadcastChannel(Ok), Ii.addEventListener("message", (e) => {
        const t = yt(e.data) ? e.data : null;
        !t || t.type !== "signed-context-consumed" || vw(bw({ version: 2, entries: t.entries }, Date.now()));
      });
    } catch {
      Ii = null;
    }
}
function Uk(e = Date.now()) {
  af || (ad(), xw(e, ei()), af = !0);
}
function xw(e = Date.now(), t = !1) {
  ad();
  const n = mw(t);
  if (n)
    try {
      vw(bw(JSON.parse(n.getItem(gw) ?? "null"), e), e, !1), id(e);
    } catch {
      if (t && ei()) throw od();
      sd(e);
    }
}
function Dk(e) {
  ad();
  try {
    Ii?.postMessage({ type: "signed-context-consumed", entries: e });
  } catch {
  }
}
function zk() {
  const t = globalThis.navigator?.locks;
  return !t || typeof t != "object" || typeof t.request != "function" ? null : t;
}
async function Mk(e) {
  const t = zk();
  if (t) return await t.request(Ck, { mode: "exclusive" }, e);
  if (typeof window < "u")
    throw new _("context_replay_lock_unavailable", "signed context replay lock is unavailable", !0);
  return await e();
}
function kw(e, t) {
  Uk(t);
  const n = /* @__PURE__ */ new Map();
  for (const r of e) {
    const i = Date.parse(r.expiresAt);
    if (!Number.isFinite(i) || i <= t)
      throw new _("context_expired", "signed context is expired", !0);
    n.set(r.requestId, Math.max(n.get(r.requestId) ?? 0, i));
  }
  id(t);
  for (const r of n.keys())
    if (bt.has(r))
      throw new _("context_replayed", "signed context requestId was already used", !0);
  if (bt.size + n.size > $k)
    throw new _("context_replay_cache_full", "signed context replay cache is full", !0);
  return n;
}
function ti(e, t = Date.now()) {
  kw(e, t);
}
async function Fi(e, t = Date.now()) {
  const n = [...e];
  await Mk(() => {
    xw(t, ei());
    const r = kw(n, t), i = /* @__PURE__ */ new Map();
    for (const o of r.keys()) i.set(o, bt.get(o));
    for (const [o, s] of r) bt.set(o, s);
    try {
      sd(t, ei());
    } catch (o) {
      for (const [s, a] of i)
        a === void 0 ? bt.delete(s) : bt.set(s, a);
      throw o;
    }
    Dk([...r]);
  });
}
async function Kk(e, t) {
  if (e.purpose !== "agent-deliver") throw new _("invalid_context", "context is not an agent delivery");
  if (!e.grantId) throw new _("invalid_context", "agent delivery context is missing grantId");
  const n = nd(e.release, "invalid_context");
  if (n.name !== t) throw new _("invalid_context", "signed release name does not match release item");
  if (e.display.grantTtlSeconds === void 0 || e.display.grantTtlSeconds !== n.ttlSecs)
    throw new _("invalid_context", "signed release TTL does not match display TTL");
  return {
    purpose: "agent-deliver",
    name: n.name,
    grantId: e.grantId,
    ttlSecs: n.ttlSecs,
    approvalNonce: new Uint8Array(n.approvalNonce),
    approvalExpiresAtUnix: n.approvalExpiresAtUnix,
    operationHash: n.operationHash
  };
}
function Fk(e) {
  if (!e.agent) throw new _("invalid_context", "agent delivery context is missing agent key");
  if (e.agent.publicKey.fingerprint && e.agent.publicKey.fingerprint !== e.agent.fingerprint)
    throw new _("invalid_context", "agent public key fingerprint mismatch");
  return { ...e.agent.publicKey, fingerprint: e.agent.fingerprint };
}
async function jk(e) {
  const t = e.map((n) => ga(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(me(t))));
}
function cf(e) {
  return me(e.display);
}
const Vk = "1.2.3";
let ht = class Nc extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof Nc ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof Nc && n.cause.docsPath || n.docsPath, o = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...i ? [`Docs: https://abitype.dev${i}`] : [],
      ...r ? [`Details: ${r}`] : [],
      `Version: abitype@${Vk}`
    ].join(`
`);
    super(o), Object.defineProperty(this, "details", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "docsPath", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "metaMessages", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "shortMessage", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "AbiTypeError"
    }), n.cause && (this.cause = n.cause), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.shortMessage = t;
  }
};
function dn(e, t) {
  return e.exec(t)?.groups;
}
const Ew = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, Sw = /^u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/, Aw = /^\(.+?\).*?$/, lf = /^tuple(?<array>(\[(\d*)\])*)$/;
function Uc(e) {
  let t = e.type;
  if (lf.test(e.type) && "components" in e) {
    t = "(";
    const n = e.components.length;
    for (let i = 0; i < n; i++) {
      const o = e.components[i];
      t += Uc(o), i < n - 1 && (t += ", ");
    }
    const r = dn(lf, e.type);
    return t += `)${r?.array || ""}`, Uc({
      ...e,
      type: t
    });
  }
  return "indexed" in e && e.indexed && (t = `${t} indexed`), e.name ? `${t} ${e.name}` : t;
}
function mi(e) {
  let t = "";
  const n = e.length;
  for (let r = 0; r < n; r++) {
    const i = e[r];
    t += Uc(i), r !== n - 1 && (t += ", ");
  }
  return t;
}
function qk(e) {
  return e.type === "function" ? `function ${e.name}(${mi(e.inputs)})${e.stateMutability && e.stateMutability !== "nonpayable" ? ` ${e.stateMutability}` : ""}${e.outputs?.length ? ` returns (${mi(e.outputs)})` : ""}` : e.type === "event" ? `event ${e.name}(${mi(e.inputs)})` : e.type === "error" ? `error ${e.name}(${mi(e.inputs)})` : e.type === "constructor" ? `constructor(${mi(e.inputs)})${e.stateMutability === "payable" ? " payable" : ""}` : e.type === "fallback" ? `fallback() external${e.stateMutability === "payable" ? " payable" : ""}` : "receive() external payable";
}
const _w = /^error (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Wk(e) {
  return _w.test(e);
}
function Gk(e) {
  return dn(_w, e);
}
const Iw = /^event (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Zk(e) {
  return Iw.test(e);
}
function Yk(e) {
  return dn(Iw, e);
}
const Bw = /^function (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)(?: (?<scope>external|public{1}))?(?: (?<stateMutability>pure|view|nonpayable|payable{1}))?(?: returns\s?\((?<returns>.*?)\))?$/;
function Xk(e) {
  return Bw.test(e);
}
function Jk(e) {
  return dn(Bw, e);
}
const Tw = /^struct (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*) \{(?<properties>.*?)\}$/;
function Pw(e) {
  return Tw.test(e);
}
function Qk(e) {
  return dn(Tw, e);
}
const $w = /^constructor\((?<parameters>.*?)\)(?:\s(?<stateMutability>payable{1}))?$/;
function eE(e) {
  return $w.test(e);
}
function tE(e) {
  return dn($w, e);
}
const Ow = /^fallback\(\) external(?:\s(?<stateMutability>payable{1}))?$/;
function nE(e) {
  return Ow.test(e);
}
function rE(e) {
  return dn(Ow, e);
}
const iE = /^receive\(\) external payable$/;
function oE(e) {
  return iE.test(e);
}
const sE = /* @__PURE__ */ new Set(["indexed"]), Dc = /* @__PURE__ */ new Set([
  "calldata",
  "memory",
  "storage"
]);
class aE extends ht {
  constructor({ type: t }) {
    super("Unknown type.", {
      metaMessages: [
        `Type "${t}" is not a valid ABI type. Perhaps you forgot to include a struct signature?`
      ]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "UnknownTypeError"
    });
  }
}
class cE extends ht {
  constructor({ type: t }) {
    super("Unknown type.", {
      metaMessages: [`Type "${t}" is not a valid ABI type.`]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "UnknownSolidityTypeError"
    });
  }
}
class lE extends ht {
  constructor({ param: t }) {
    super("Invalid ABI parameter.", {
      details: t
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidParameterError"
    });
  }
}
class dE extends ht {
  constructor({ param: t, name: n }) {
    super("Invalid ABI parameter.", {
      details: t,
      metaMessages: [
        `"${n}" is a protected Solidity keyword. More info: https://docs.soliditylang.org/en/latest/cheatsheet.html`
      ]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "SolidityProtectedKeywordError"
    });
  }
}
class uE extends ht {
  constructor({ param: t, type: n, modifier: r }) {
    super("Invalid ABI parameter.", {
      details: t,
      metaMessages: [
        `Modifier "${r}" not allowed${n ? ` in "${n}" type` : ""}.`
      ]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidModifierError"
    });
  }
}
class fE extends ht {
  constructor({ param: t, type: n, modifier: r }) {
    super("Invalid ABI parameter.", {
      details: t,
      metaMessages: [
        `Modifier "${r}" not allowed${n ? ` in "${n}" type` : ""}.`,
        `Data location can only be specified for array, struct, or mapping types, but "${r}" was given.`
      ]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidFunctionModifierError"
    });
  }
}
class hE extends ht {
  constructor({ abiParameter: t }) {
    super("Invalid ABI parameter.", {
      details: JSON.stringify(t, null, 2),
      metaMessages: ["ABI parameter type is invalid."]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidAbiTypeParameterError"
    });
  }
}
class ui extends ht {
  constructor({ signature: t, type: n }) {
    super(`Invalid ${n} signature.`, {
      details: t
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidSignatureError"
    });
  }
}
class pE extends ht {
  constructor({ signature: t }) {
    super("Unknown signature.", {
      details: t
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "UnknownSignatureError"
    });
  }
}
class yE extends ht {
  constructor({ signature: t }) {
    super("Invalid struct signature.", {
      details: t,
      metaMessages: ["No properties exist."]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidStructSignatureError"
    });
  }
}
class wE extends ht {
  constructor({ type: t }) {
    super("Circular reference detected.", {
      metaMessages: [`Struct "${t}" is a circular reference.`]
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "CircularReferenceError"
    });
  }
}
class gE extends ht {
  constructor({ current: t, depth: n }) {
    super("Unbalanced parentheses.", {
      metaMessages: [
        `"${t.trim()}" has too many ${n > 0 ? "opening" : "closing"} parentheses.`
      ],
      details: `Depth "${n}"`
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "InvalidParenthesisError"
    });
  }
}
function mE(e, t, n) {
  let r = "";
  if (n)
    for (const i of Object.entries(n)) {
      if (!i)
        continue;
      let o = "";
      for (const s of i[1])
        o += `[${s.type}${s.name ? `:${s.name}` : ""}]`;
      r += `(${i[0]}{${o}})`;
    }
  return t ? `${t}:${e}${r}` : `${e}${r}`;
}
const Za = /* @__PURE__ */ new Map([
  // Unnamed
  ["address", { type: "address" }],
  ["bool", { type: "bool" }],
  ["bytes", { type: "bytes" }],
  ["bytes32", { type: "bytes32" }],
  ["int", { type: "int256" }],
  ["int256", { type: "int256" }],
  ["string", { type: "string" }],
  ["uint", { type: "uint256" }],
  ["uint8", { type: "uint8" }],
  ["uint16", { type: "uint16" }],
  ["uint24", { type: "uint24" }],
  ["uint32", { type: "uint32" }],
  ["uint64", { type: "uint64" }],
  ["uint96", { type: "uint96" }],
  ["uint112", { type: "uint112" }],
  ["uint160", { type: "uint160" }],
  ["uint192", { type: "uint192" }],
  ["uint256", { type: "uint256" }],
  // Named
  ["address owner", { type: "address", name: "owner" }],
  ["address to", { type: "address", name: "to" }],
  ["bool approved", { type: "bool", name: "approved" }],
  ["bytes _data", { type: "bytes", name: "_data" }],
  ["bytes data", { type: "bytes", name: "data" }],
  ["bytes signature", { type: "bytes", name: "signature" }],
  ["bytes32 hash", { type: "bytes32", name: "hash" }],
  ["bytes32 r", { type: "bytes32", name: "r" }],
  ["bytes32 root", { type: "bytes32", name: "root" }],
  ["bytes32 s", { type: "bytes32", name: "s" }],
  ["string name", { type: "string", name: "name" }],
  ["string symbol", { type: "string", name: "symbol" }],
  ["string tokenURI", { type: "string", name: "tokenURI" }],
  ["uint tokenId", { type: "uint256", name: "tokenId" }],
  ["uint8 v", { type: "uint8", name: "v" }],
  ["uint256 balance", { type: "uint256", name: "balance" }],
  ["uint256 tokenId", { type: "uint256", name: "tokenId" }],
  ["uint256 value", { type: "uint256", name: "value" }],
  // Indexed
  [
    "event:address indexed from",
    { type: "address", name: "from", indexed: !0 }
  ],
  ["event:address indexed to", { type: "address", name: "to", indexed: !0 }],
  [
    "event:uint indexed tokenId",
    { type: "uint256", name: "tokenId", indexed: !0 }
  ],
  [
    "event:uint256 indexed tokenId",
    { type: "uint256", name: "tokenId", indexed: !0 }
  ]
]);
function bE(e, t = {}) {
  if (Xk(e))
    return vE(e, t);
  if (Zk(e))
    return xE(e, t);
  if (Wk(e))
    return kE(e, t);
  if (eE(e))
    return EE(e, t);
  if (nE(e))
    return SE(e);
  if (oE(e))
    return {
      type: "receive",
      stateMutability: "payable"
    };
  throw new pE({ signature: e });
}
function vE(e, t = {}) {
  const n = Jk(e);
  if (!n)
    throw new ui({ signature: e, type: "function" });
  const r = gt(n.parameters), i = [], o = r.length;
  for (let a = 0; a < o; a++)
    i.push(yr(r[a], {
      modifiers: Dc,
      structs: t,
      type: "function"
    }));
  const s = [];
  if (n.returns) {
    const a = gt(n.returns), c = a.length;
    for (let l = 0; l < c; l++)
      s.push(yr(a[l], {
        modifiers: Dc,
        structs: t,
        type: "function"
      }));
  }
  return {
    name: n.name,
    type: "function",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i,
    outputs: s
  };
}
function xE(e, t = {}) {
  const n = Yk(e);
  if (!n)
    throw new ui({ signature: e, type: "event" });
  const r = gt(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(yr(r[s], {
      modifiers: sE,
      structs: t,
      type: "event"
    }));
  return { name: n.name, type: "event", inputs: i };
}
function kE(e, t = {}) {
  const n = Gk(e);
  if (!n)
    throw new ui({ signature: e, type: "error" });
  const r = gt(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(yr(r[s], { structs: t, type: "error" }));
  return { name: n.name, type: "error", inputs: i };
}
function EE(e, t = {}) {
  const n = tE(e);
  if (!n)
    throw new ui({ signature: e, type: "constructor" });
  const r = gt(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(yr(r[s], { structs: t, type: "constructor" }));
  return {
    type: "constructor",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i
  };
}
function SE(e) {
  const t = rE(e);
  if (!t)
    throw new ui({ signature: e, type: "fallback" });
  return {
    type: "fallback",
    stateMutability: t.stateMutability ?? "nonpayable"
  };
}
const AE = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, _E = /^\((?<type>.+?)\)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, IE = /^u?int$/;
function yr(e, t) {
  const n = mE(e, t?.type, t?.structs);
  if (Za.has(n))
    return Za.get(n);
  const r = Aw.test(e), i = dn(r ? _E : AE, e);
  if (!i)
    throw new lE({ param: e });
  if (i.name && TE(i.name))
    throw new dE({ param: e, name: i.name });
  const o = i.name ? { name: i.name } : {}, s = i.modifier === "indexed" ? { indexed: !0 } : {}, a = t?.structs ?? {};
  let c, l = {};
  if (r) {
    c = "tuple";
    const u = gt(i.type), f = [], h = u.length;
    for (let y = 0; y < h; y++)
      f.push(yr(u[y], { structs: a }));
    l = { components: f };
  } else if (i.type in a)
    c = "tuple", l = { components: a[i.type] };
  else if (IE.test(i.type))
    c = `${i.type}256`;
  else if (i.type === "address payable")
    c = "address";
  else if (c = i.type, t?.type !== "struct" && !Cw(c))
    throw new cE({ type: c });
  if (i.modifier) {
    if (!t?.modifiers?.has?.(i.modifier))
      throw new uE({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
    if (Dc.has(i.modifier) && !PE(c, !!i.array))
      throw new fE({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
  }
  const d = {
    type: `${c}${i.array ?? ""}`,
    ...o,
    ...s,
    ...l
  };
  return Za.set(n, d), d;
}
function gt(e, t = [], n = "", r = 0) {
  const i = e.trim().length;
  for (let o = 0; o < i; o++) {
    const s = e[o], a = e.slice(o + 1);
    switch (s) {
      case ",":
        return r === 0 ? gt(a, [...t, n.trim()]) : gt(a, t, `${n}${s}`, r);
      case "(":
        return gt(a, t, `${n}${s}`, r + 1);
      case ")":
        return gt(a, t, `${n}${s}`, r - 1);
      default:
        return gt(a, t, `${n}${s}`, r);
    }
  }
  if (n === "")
    return t;
  if (r !== 0)
    throw new gE({ current: n, depth: r });
  return t.push(n.trim()), t;
}
function Cw(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || Ew.test(e) || Sw.test(e);
}
const BE = /^(?:after|alias|anonymous|apply|auto|byte|calldata|case|catch|constant|copyof|default|defined|error|event|external|false|final|function|immutable|implements|in|indexed|inline|internal|let|mapping|match|memory|mutable|null|of|override|partial|private|promise|public|pure|reference|relocatable|return|returns|sizeof|static|storage|struct|super|supports|switch|this|true|try|typedef|typeof|var|view|virtual)$/;
function TE(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || e === "tuple" || Ew.test(e) || Sw.test(e) || BE.test(e);
}
function PE(e, t) {
  return t || e === "bytes" || e === "string" || e === "tuple";
}
function $E(e) {
  const t = {}, n = e.length;
  for (let s = 0; s < n; s++) {
    const a = e[s];
    if (!Pw(a))
      continue;
    const c = Qk(a);
    if (!c)
      throw new ui({ signature: a, type: "struct" });
    const l = c.properties.split(";"), d = [], u = l.length;
    for (let f = 0; f < u; f++) {
      const y = l[f].trim();
      if (!y)
        continue;
      const w = yr(y, {
        type: "struct"
      });
      d.push(w);
    }
    if (!d.length)
      throw new yE({ signature: a });
    t[c.name] = d;
  }
  const r = {}, i = Object.entries(t), o = i.length;
  for (let s = 0; s < o; s++) {
    const [a, c] = i[s];
    r[a] = Rw(c, t);
  }
  return r;
}
const OE = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*)(?<array>(?:\[\d*?\])+?)?$/;
function Rw(e = [], t = {}, n = /* @__PURE__ */ new Set()) {
  const r = [], i = e.length;
  for (let o = 0; o < i; o++) {
    const s = e[o];
    if (Aw.test(s.type))
      r.push(s);
    else {
      const c = dn(OE, s.type);
      if (!c?.type)
        throw new hE({ abiParameter: s });
      const { array: l, type: d } = c;
      if (d in t) {
        if (n.has(d))
          throw new wE({ type: d });
        r.push({
          ...s,
          type: `tuple${l ?? ""}`,
          components: Rw(t[d], t, /* @__PURE__ */ new Set([...n, d]))
        });
      } else if (Cw(d))
        r.push(s);
      else
        throw new aE({ type: d });
    }
  }
  return r;
}
function CE(e) {
  const t = $E(e), n = [], r = e.length;
  for (let i = 0; i < r; i++) {
    const o = e[i];
    Pw(o) || n.push(bE(o, t));
  }
  return n;
}
function Lw(e, { includeName: t = !1 } = {}) {
  return e ? e.map((n) => RE(n, { includeName: t })).join(t ? ", " : ",") : "";
}
function RE(e, { includeName: t }) {
  return e.type.startsWith("tuple") ? `(${Lw(e.components, { includeName: t })})${e.type.slice(5)}` : e.type + (t && e.name ? ` ${e.name}` : "");
}
function eo(e, { strict: t = !0 } = {}) {
  return !e || typeof e != "string" ? !1 : t ? /^0x[0-9a-fA-F]*$/.test(e) : e.startsWith("0x");
}
function Be(e) {
  return eo(e, { strict: !1 }) ? Math.ceil((e.length - 2) / 2) : e.length;
}
const Hw = "2.54.6";
let Ya = {
  getDocsUrl: ({ docsBaseUrl: e, docsPath: t = "", docsSlug: n }) => t ? `${e ?? "https://viem.sh"}${t}${n ? `#${n}` : ""}` : void 0,
  version: `viem@${Hw}`
};
class te extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof te ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof te && n.cause.docsPath || n.docsPath, o = Ya.getDocsUrl?.({ ...n, docsPath: i }), s = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...o ? [`Docs: ${o}`] : [],
      ...r ? [`Details: ${r}`] : [],
      ...Ya.version ? [`Version: ${Ya.version}`] : []
    ].join(`
`);
    super(s, n.cause ? { cause: n.cause } : void 0), Object.defineProperty(this, "details", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "docsPath", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "metaMessages", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "shortMessage", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "version", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "name", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: "BaseError"
    }), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.name = n.name ?? this.name, this.shortMessage = t, this.version = Hw;
  }
  walk(t) {
    return Nw(this, t);
  }
}
function Nw(e, t) {
  return t?.(e) ? e : e && typeof e == "object" && "cause" in e && e.cause !== void 0 ? Nw(e.cause, t) : t ? null : e;
}
class LE extends te {
  constructor({ data: t, params: n, size: r }) {
    super([`Data size of ${r} bytes is too small for given parameters.`].join(`
`), {
      metaMessages: [
        `Params: (${Lw(n, { includeName: !0 })})`,
        `Data:   ${t} (${r} bytes)`
      ],
      name: "AbiDecodingDataSizeTooSmallError"
    }), Object.defineProperty(this, "data", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "params", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Object.defineProperty(this, "size", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.data = t, this.params = n, this.size = r;
  }
}
class HE extends te {
  constructor({ cause: t } = {}) {
    super('Cannot decode zero data ("0x") with ABI parameters.', {
      name: "AbiDecodingZeroDataError",
      cause: t
    });
  }
}
class NE extends te {
  constructor({ expectedLength: t, givenLength: n, type: r }) {
    super([
      `ABI encoding array length mismatch for type ${r}.`,
      `Expected length: ${t}`,
      `Given length: ${n}`
    ].join(`
`), { name: "AbiEncodingArrayLengthMismatchError" });
  }
}
class UE extends te {
  constructor({ expectedSize: t, value: n }) {
    super(`Size of bytes "${n}" (bytes${Be(n)}) does not match expected size (bytes${t}).`, { name: "AbiEncodingBytesSizeMismatchError" });
  }
}
class DE extends te {
  constructor({ expectedLength: t, givenLength: n }) {
    super([
      "ABI encoding params/values length mismatch.",
      `Expected length (params): ${t}`,
      `Given length (values): ${n}`
    ].join(`
`), { name: "AbiEncodingLengthMismatchError" });
  }
}
class zE extends te {
  constructor({ expectedSize: t, givenSize: n }) {
    super(`Expected bytes${t}, got bytes${n}.`, {
      name: "BytesSizeMismatchError"
    });
  }
}
class ME extends te {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid encoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiEncodingType" });
  }
}
class KE extends te {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid decoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiDecodingType" });
  }
}
class FE extends te {
  constructor(t) {
    super([`Value "${t}" is not a valid array.`].join(`
`), {
      name: "InvalidArrayError"
    });
  }
}
class Uw extends te {
  constructor({ offset: t, position: n, size: r }) {
    super(`Slice ${n === "start" ? "starting" : "ending"} at offset "${t}" is out-of-bounds (size: ${r}).`, { name: "SliceOffsetOutOfBoundsError" });
  }
}
class Dw extends te {
  constructor({ size: t, targetSize: n, type: r }) {
    super(`${r.charAt(0).toUpperCase()}${r.slice(1).toLowerCase()} size (${t}) exceeds padding size (${n}).`, { name: "SizeExceedsPaddingSizeError" });
  }
}
function fi(e, { dir: t, size: n = 32 } = {}) {
  return typeof e == "string" ? Dn(e, { dir: t, size: n }) : jE(e, { dir: t, size: n });
}
function Dn(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  const r = e.replace("0x", "");
  if (r.length > n * 2)
    throw new Dw({
      size: Math.ceil(r.length / 2),
      targetSize: n,
      type: "hex"
    });
  return `0x${r[t === "right" ? "padEnd" : "padStart"](n * 2, "0")}`;
}
function jE(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  if (e.length > n)
    throw new Dw({
      size: e.length,
      targetSize: n,
      type: "bytes"
    });
  const r = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const o = t === "right";
    r[o ? i : n - i - 1] = e[o ? i : e.length - i - 1];
  }
  return r;
}
class cd extends te {
  constructor({ max: t, min: n, signed: r, size: i, value: o }) {
    super(`Number "${o}" is not in safe ${i ? `${i * 8}-bit ${r ? "signed" : "unsigned"} ` : ""}integer range ${t ? `(${n} to ${t})` : `(above ${n})`}`, { name: "IntegerOutOfRangeError" });
  }
}
class VE extends te {
  constructor(t) {
    super(`Bytes value "${t}" is not a valid boolean. The bytes array must contain a single byte of either a 0 or 1 value.`, {
      name: "InvalidBytesBooleanError"
    });
  }
}
class qE extends te {
  constructor({ givenSize: t, maxSize: n }) {
    super(`Size cannot exceed ${n} bytes. Given size: ${t} bytes.`, { name: "SizeOverflowError" });
  }
}
function ji(e, { dir: t = "left" } = {}) {
  let n = typeof e == "string" ? e.replace("0x", "") : e, r = 0;
  for (let i = 0; i < n.length - 1 && n[t === "left" ? i : n.length - i - 1].toString() === "0"; i++)
    r++;
  return n = t === "left" ? n.slice(r) : n.slice(0, n.length - r), typeof e == "string" ? (n.length === 1 && t === "right" && (n = `${n}0`), `0x${n.length % 2 === 1 ? `0${n}` : n}`) : n;
}
function jt(e, { size: t }) {
  if (Be(e) > t)
    throw new qE({
      givenSize: Be(e),
      maxSize: t
    });
}
function zw(e, t = {}) {
  const { signed: n } = t;
  t.size && jt(e, { size: t.size });
  const r = BigInt(e);
  if (!n)
    return r;
  const i = (e.length - 2) / 2, o = (1n << BigInt(i) * 8n - 1n) - 1n;
  return r <= o ? r : r - BigInt(`0x${"f".padStart(i * 2, "f")}`) - 1n;
}
function ld(e, t = {}) {
  const n = zw(e, t), r = Number(n);
  if (!Number.isSafeInteger(r))
    throw new cd({
      max: `${Number.MAX_SAFE_INTEGER}`,
      min: `${Number.MIN_SAFE_INTEGER}`,
      signed: t.signed,
      size: t.size,
      value: `${n}n`
    });
  return r;
}
const WE = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Vi(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? ee(e, t) : typeof e == "string" ? Kw(e, t) : typeof e == "boolean" ? Mw(e, t) : Je(e, t);
}
function Mw(e, t = {}) {
  const n = `0x${Number(e)}`;
  return typeof t.size == "number" ? (jt(n, { size: t.size }), fi(n, { size: t.size })) : n;
}
function Je(e, t = {}) {
  let n = "";
  for (let i = 0; i < e.length; i++)
    n += WE[e[i]];
  const r = `0x${n}`;
  return typeof t.size == "number" ? (jt(r, { size: t.size }), fi(r, { dir: "right", size: t.size })) : r;
}
function ee(e, t = {}) {
  const { signed: n, size: r } = t, i = BigInt(e);
  let o;
  r ? n ? o = (1n << BigInt(r) * 8n - 1n) - 1n : o = 2n ** (BigInt(r) * 8n) - 1n : typeof e == "number" && (o = BigInt(Number.MAX_SAFE_INTEGER));
  const s = typeof o == "bigint" && n ? -o - 1n : 0;
  if (o && i > o || i < s) {
    const c = typeof e == "bigint" ? "n" : "";
    throw new cd({
      max: o ? `${o}${c}` : void 0,
      min: `${s}${c}`,
      signed: n,
      size: r,
      value: `${e}${c}`
    });
  }
  const a = `0x${(n && i < 0 ? (1n << BigInt(r * 8)) + BigInt(i) : i).toString(16)}`;
  return r ? fi(a, { size: r }) : a;
}
const GE = /* @__PURE__ */ new TextEncoder();
function Kw(e, t = {}) {
  const n = GE.encode(e);
  return Je(n, t);
}
const ZE = /* @__PURE__ */ new TextEncoder();
function dd(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? XE(e, t) : typeof e == "boolean" ? YE(e, t) : eo(e) ? Vn(e, t) : Fw(e, t);
}
function YE(e, t = {}) {
  const n = new Uint8Array(1);
  return n[0] = Number(e), typeof t.size == "number" ? (jt(n, { size: t.size }), fi(n, { size: t.size })) : n;
}
const Xt = {
  zero: 48,
  nine: 57,
  A: 65,
  F: 70,
  a: 97,
  f: 102
};
function df(e) {
  if (e >= Xt.zero && e <= Xt.nine)
    return e - Xt.zero;
  if (e >= Xt.A && e <= Xt.F)
    return e - (Xt.A - 10);
  if (e >= Xt.a && e <= Xt.f)
    return e - (Xt.a - 10);
}
function Vn(e, t = {}) {
  let n = e;
  t.size && (jt(n, { size: t.size }), n = fi(n, { dir: "right", size: t.size }));
  let r = n.slice(2);
  r.length % 2 && (r = `0${r}`);
  const i = r.length / 2, o = new Uint8Array(i);
  for (let s = 0, a = 0; s < i; s++) {
    const c = df(r.charCodeAt(a++)), l = df(r.charCodeAt(a++));
    if (c === void 0 || l === void 0)
      throw new te(`Invalid byte sequence ("${r[a - 2]}${r[a - 1]}" in "${r}").`);
    o[s] = c * 16 + l;
  }
  return o;
}
function XE(e, t) {
  const n = ee(e, t);
  return Vn(n);
}
function Fw(e, t = {}) {
  const n = ZE.encode(e);
  return typeof t.size == "number" ? (jt(n, { size: t.size }), fi(n, { dir: "right", size: t.size })) : n;
}
const Ro = /* @__PURE__ */ BigInt(2 ** 32 - 1), uf = /* @__PURE__ */ BigInt(32);
function JE(e, t = !1) {
  return t ? { h: Number(e & Ro), l: Number(e >> uf & Ro) } : { h: Number(e >> uf & Ro) | 0, l: Number(e & Ro) | 0 };
}
function QE(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = JE(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const eS = (e, t, n) => e << n | t >>> 32 - n, tS = (e, t, n) => t << n | e >>> 32 - n, nS = (e, t, n) => t << n - 32 | e >>> 64 - n, rS = (e, t, n) => e << n - 32 | t >>> 64 - n;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function iS(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function ff(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function qi(e, ...t) {
  if (!iS(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function Ms(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function jw(e, t) {
  qi(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function oS(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function Wi(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Xa(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Pt(e, t) {
  return e << 32 - t | e >>> t;
}
const sS = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function aS(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function cS(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = aS(e[t]);
  return e;
}
const hf = sS ? (e) => e : cS;
function lS(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function ud(e) {
  return typeof e == "string" && (e = lS(e)), qi(e), e;
}
class Vw {
}
function qw(e) {
  const t = (r) => e().update(ud(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
const dS = BigInt(0), bi = BigInt(1), uS = BigInt(2), fS = BigInt(7), hS = BigInt(256), pS = BigInt(113), Ww = [], Gw = [], Zw = [];
for (let e = 0, t = bi, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], Ww.push(2 * (5 * r + n)), Gw.push((e + 1) * (e + 2) / 2 % 64);
  let i = dS;
  for (let o = 0; o < 7; o++)
    t = (t << bi ^ (t >> fS) * pS) % hS, t & uS && (i ^= bi << (bi << /* @__PURE__ */ BigInt(o)) - bi);
  Zw.push(i);
}
const Yw = QE(Zw, !0), yS = Yw[0], wS = Yw[1], pf = (e, t, n) => n > 32 ? nS(e, t, n) : eS(e, t, n), yf = (e, t, n) => n > 32 ? rS(e, t, n) : tS(e, t, n);
function gS(e, t = 24) {
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], d = n[c + 1], u = pf(l, d, 1) ^ n[a], f = yf(l, d, 1) ^ n[a + 1];
      for (let h = 0; h < 50; h += 10)
        e[s + h] ^= u, e[s + h + 1] ^= f;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = Gw[s], c = pf(i, o, a), l = yf(i, o, a), d = Ww[s];
      i = e[d], o = e[d + 1], e[d] = c, e[d + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      for (let a = 0; a < 10; a++)
        n[a] = e[s + a];
      for (let a = 0; a < 10; a++)
        e[s + a] ^= ~n[(a + 2) % 10] & n[(a + 4) % 10];
    }
    e[0] ^= yS[r], e[1] ^= wS[r];
  }
  Wi(n);
}
class fd extends Vw {
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (super(), this.pos = 0, this.posOut = 0, this.finished = !1, this.destroyed = !1, this.enableXOF = !1, this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.rounds = o, ff(r), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = oS(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    hf(this.state32), gS(this.state32, this.rounds), hf(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Ms(this), t = ud(t), qi(t);
    const { blockLen: n, state: r } = this, i = t.length;
    for (let o = 0; o < i; ) {
      const s = Math.min(n - this.pos, i - o);
      for (let a = 0; a < s; a++)
        r[this.pos++] ^= t[o++];
      this.pos === n && this.keccak();
    }
    return this;
  }
  finish() {
    if (this.finished)
      return;
    this.finished = !0;
    const { state: t, suffix: n, pos: r, blockLen: i } = this;
    t[r] ^= n, (n & 128) !== 0 && r === i - 1 && this.keccak(), t[i - 1] ^= 128, this.keccak();
  }
  writeInto(t) {
    Ms(this, !1), qi(t), this.finish();
    const n = this.state, { blockLen: r } = this;
    for (let i = 0, o = t.length; i < o; ) {
      this.posOut >= r && this.keccak();
      const s = Math.min(r - this.posOut, o - i);
      t.set(n.subarray(this.posOut, this.posOut + s), i), this.posOut += s, i += s;
    }
    return t;
  }
  xofInto(t) {
    if (!this.enableXOF)
      throw new Error("XOF is not possible for this instance");
    return this.writeInto(t);
  }
  xof(t) {
    return ff(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (jw(t, this), this.finished)
      throw new Error("digest() was already called");
    return this.writeInto(t), this.destroy(), t;
  }
  digest() {
    return this.digestInto(new Uint8Array(this.outputLen));
  }
  destroy() {
    this.destroyed = !0, Wi(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t || (t = new fd(n, r, i, s, o)), t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.destroyed = this.destroyed, t;
  }
}
const mS = (e, t, n) => qw(() => new fd(t, e, n)), bS = mS(1, 136, 256 / 8);
function Nt(e, t) {
  const n = t || "hex", r = bS(eo(e, { strict: !1 }) ? dd(e) : e);
  return n === "bytes" ? r : Vi(r);
}
const vS = (e) => Nt(dd(e));
function xS(e) {
  return vS(e);
}
function kS(e) {
  let t = !0, n = "", r = 0, i = "", o = !1;
  for (let s = 0; s < e.length; s++) {
    const a = e[s];
    if (["(", ")", ","].includes(a) && (t = !0), a === "(" && r++, a === ")" && r--, !!t) {
      if (r === 0) {
        if (a === " " && ["event", "function", ""].includes(i))
          i = "";
        else if (i += a, a === ")") {
          o = !0;
          break;
        }
        continue;
      }
      if (a === " ") {
        e[s - 1] !== "," && n !== "," && n !== ",(" && (n = "", t = !1);
        continue;
      }
      i += a, n += a;
    }
  }
  if (!o)
    throw new te("Unable to normalize signature.");
  return i;
}
const ES = (e) => {
  const t = typeof e == "string" ? e : qk(e);
  return kS(t);
};
function SS(e) {
  return xS(ES(e));
}
class br extends te {
  constructor({ address: t }) {
    super(`Address "${t}" is invalid.`, {
      metaMessages: [
        "- Address must be a hex value of 20 bytes (40 hex characters).",
        "- Address must match its checksum counterpart."
      ],
      name: "InvalidAddressError"
    });
  }
}
class Xw extends Map {
  constructor(t) {
    super(), Object.defineProperty(this, "maxSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.maxSize = t;
  }
  get(t) {
    const n = super.get(t);
    return super.has(t) && (super.delete(t), super.set(t, n)), n;
  }
  set(t, n) {
    if (super.has(t) && super.delete(t), super.set(t, n), this.maxSize && this.size > this.maxSize) {
      const r = super.keys().next().value;
      r !== void 0 && super.delete(r);
    }
    return this;
  }
}
const Ja = /* @__PURE__ */ new Xw(8192);
function Jw(e, t) {
  if (Ja.has(`${e}.${t}`))
    return Ja.get(`${e}.${t}`);
  const n = e.substring(2).toLowerCase(), r = Nt(Fw(n), "bytes"), i = n.split("");
  for (let s = 0; s < 40; s += 2)
    r[s >> 1] >> 4 >= 8 && i[s] && (i[s] = i[s].toUpperCase()), (r[s >> 1] & 15) >= 8 && i[s + 1] && (i[s + 1] = i[s + 1].toUpperCase());
  const o = `0x${i.join("")}`;
  return Ja.set(`${e}.${t}`, o), o;
}
const AS = /^0x[a-fA-F0-9]{40}$/, Qa = /* @__PURE__ */ new Xw(8192);
function vr(e, t) {
  const { strict: n = !0 } = t ?? {}, r = `${e}.${n}`;
  if (Qa.has(r))
    return Qa.get(r);
  const i = AS.test(e) ? e.toLowerCase() === e ? !0 : n ? Jw(e) === e : !0 : !1;
  return Qa.set(r, i), i;
}
function _S(e) {
  return typeof e[0] == "string" ? Et(e) : IS(e);
}
function IS(e) {
  let t = 0;
  for (const i of e)
    t += i.length;
  const n = new Uint8Array(t);
  let r = 0;
  for (const i of e)
    n.set(i, r), r += i.length;
  return n;
}
function Et(e) {
  return `0x${e.reduce((t, n) => t + n.replace("0x", ""), "")}`;
}
function hd(e, t, n, { strict: r } = {}) {
  return eo(e, { strict: !1 }) ? ng(e, t, n, {
    strict: r
  }) : tg(e, t, n, {
    strict: r
  });
}
function Qw(e, t) {
  if (typeof t == "number" && t > 0 && t > Be(e) - 1)
    throw new Uw({
      offset: t,
      position: "start",
      size: Be(e)
    });
}
function eg(e, t, n) {
  if (typeof t == "number" && typeof n == "number" && Be(e) !== n - t)
    throw new Uw({
      offset: n,
      position: "end",
      size: Be(e)
    });
}
function tg(e, t, n, { strict: r } = {}) {
  Qw(e, t);
  const i = e.slice(t, n);
  return r && eg(i, t, n), i;
}
function ng(e, t, n, { strict: r } = {}) {
  Qw(e, t);
  const i = `0x${e.replace("0x", "").slice((t ?? 0) * 2, (n ?? e.length) * 2)}`;
  return r && eg(i, t, n), i;
}
const BS = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, rg = /^(u?int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/;
function pd(e, t) {
  if (e.length !== t.length)
    throw new DE({
      expectedLength: e.length,
      givenLength: t.length
    });
  const n = TS({
    params: e,
    values: t
  });
  return wd(n);
}
function TS({ params: e, values: t }) {
  const n = [];
  for (let r = 0; r < e.length; r++)
    n.push(yd({ param: e[r], value: t[r] }));
  return n;
}
function yd({ param: e, value: t }) {
  const n = ma(e.type);
  if (n) {
    const [r, i] = n;
    return $S(t, { length: r, param: { ...e, type: i } });
  }
  if (e.type === "tuple")
    return HS(t, {
      param: e
    });
  if (e.type === "address")
    return PS(t);
  if (e.type === "bool")
    return CS(t);
  if (e.type.startsWith("uint") || e.type.startsWith("int")) {
    const r = e.type.startsWith("int"), [, , i = "256"] = rg.exec(e.type) ?? [];
    return RS(t, {
      signed: r,
      size: Number(i)
    });
  }
  if (e.type.startsWith("bytes"))
    return OS(t, { param: e });
  if (e.type === "string")
    return LS(t);
  throw new ME(e.type, {
    docsPath: "/docs/contract/encodeAbiParameters"
  });
}
function wd(e) {
  let t = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? t += 32 : t += Be(a);
  }
  const n = [], r = [];
  let i = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? (n.push(ee(t + i, { size: 32 })), r.push(a), i += Be(a)) : n.push(a);
  }
  return Et([...n, ...r]);
}
function PS(e) {
  if (!vr(e))
    throw new br({ address: e });
  return { dynamic: !1, encoded: Dn(e.toLowerCase()) };
}
function $S(e, { length: t, param: n }) {
  const r = t === null;
  if (!Array.isArray(e))
    throw new FE(e);
  if (!r && e.length !== t)
    throw new NE({
      expectedLength: t,
      givenLength: e.length,
      type: `${n.type}[${t}]`
    });
  let i = e.length === 0 && zc(n);
  const o = [];
  for (let s = 0; s < e.length; s++) {
    const a = yd({ param: n, value: e[s] });
    a.dynamic && (i = !0), o.push(a);
  }
  if (r || i) {
    const s = wd(o);
    if (r) {
      const a = ee(o.length, { size: 32 });
      return {
        dynamic: !0,
        encoded: Et([a, s])
      };
    }
    if (i)
      return { dynamic: !0, encoded: s };
  }
  return {
    dynamic: !1,
    encoded: Et(o.map(({ encoded: s }) => s))
  };
}
function OS(e, { param: t }) {
  const [, n] = t.type.split("bytes"), r = Be(e);
  if (!n) {
    let i = e;
    return r % 32 !== 0 && (i = Dn(i, {
      dir: "right",
      size: Math.ceil((e.length - 2) / 2 / 32) * 32
    })), {
      dynamic: !0,
      encoded: Et([
        Dn(ee(r, { size: 32 })),
        i
      ])
    };
  }
  if (r !== Number.parseInt(n, 10))
    throw new UE({
      expectedSize: Number.parseInt(n, 10),
      value: e
    });
  return { dynamic: !1, encoded: Dn(e, { dir: "right" }) };
}
function CS(e) {
  if (typeof e != "boolean")
    throw new te(`Invalid boolean value: "${e}" (type: ${typeof e}). Expected: \`true\` or \`false\`.`);
  return { dynamic: !1, encoded: Dn(Mw(e)) };
}
function RS(e, { signed: t, size: n = 256 }) {
  if (typeof n == "number") {
    const r = 2n ** (BigInt(n) - (t ? 1n : 0n)) - 1n, i = t ? -r - 1n : 0n;
    if (e > r || e < i)
      throw new cd({
        max: r.toString(),
        min: i.toString(),
        signed: t,
        size: n / 8,
        value: e.toString()
      });
  }
  return {
    dynamic: !1,
    encoded: ee(e, {
      size: 32,
      signed: t
    })
  };
}
function LS(e) {
  const t = Kw(e), n = Math.ceil(Be(t) / 32), r = [];
  for (let i = 0; i < n; i++)
    r.push(Dn(hd(t, i * 32, (i + 1) * 32), {
      dir: "right"
    }));
  return {
    dynamic: !0,
    encoded: Et([
      Dn(ee(Be(t), { size: 32 })),
      ...r
    ])
  };
}
function HS(e, { param: t }) {
  let n = !1;
  const r = [];
  for (let i = 0; i < t.components.length; i++) {
    const o = t.components[i], s = Array.isArray(e) ? i : o.name, a = yd({
      param: o,
      value: e[s]
    });
    r.push(a), a.dynamic && (n = !0);
  }
  return {
    dynamic: n,
    encoded: n ? wd(r) : Et(r.map(({ encoded: i }) => i))
  };
}
function ma(e) {
  const t = e.match(/^(.*)\[(\d+)?\]$/);
  return t ? (
    // Return `null` if the array is dynamic.
    [t[2] ? Number(t[2]) : null, t[1]]
  ) : void 0;
}
function zc(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components.some(zc);
  const n = ma(t);
  return n ? zc({ ...e, type: n[1] }) : !1;
}
const NS = (e) => hd(SS(e), 0, 4);
class wf extends te {
  constructor({ offset: t }) {
    super(`Offset \`${t}\` cannot be negative.`, {
      name: "NegativeOffsetError"
    });
  }
}
class US extends te {
  constructor({ length: t, position: n }) {
    super(`Position \`${n}\` is out of bounds (\`0 < position < ${t}\`).`, { name: "PositionOutOfBoundsError" });
  }
}
class DS extends te {
  constructor({ count: t, limit: n }) {
    super(`Recursive read limit of \`${n}\` exceeded (recursive read count: \`${t}\`).`, { name: "RecursiveReadLimitExceededError" });
  }
}
const zS = {
  bytes: new Uint8Array(),
  dataView: new DataView(new ArrayBuffer(0)),
  position: 0,
  positionReadCount: /* @__PURE__ */ new Map(),
  recursiveReadCount: 0,
  recursiveReadLimit: Number.POSITIVE_INFINITY,
  assertReadLimit() {
    if (this.recursiveReadCount >= this.recursiveReadLimit)
      throw new DS({
        count: this.recursiveReadCount + 1,
        limit: this.recursiveReadLimit
      });
  },
  assertPosition(e) {
    if (e < 0 || e > this.bytes.length - 1)
      throw new US({
        length: this.bytes.length,
        position: e
      });
  },
  decrementPosition(e) {
    if (e < 0)
      throw new wf({ offset: e });
    const t = this.position - e;
    this.assertPosition(t), this.position = t;
  },
  getReadCount(e) {
    return this.positionReadCount.get(e || this.position) || 0;
  },
  incrementPosition(e) {
    if (e < 0)
      throw new wf({ offset: e });
    const t = this.position + e;
    this.assertPosition(t), this.position = t;
  },
  inspectByte(e) {
    const t = e ?? this.position;
    return this.assertPosition(t), this.bytes[t];
  },
  inspectBytes(e, t) {
    const n = t ?? this.position;
    return this.assertPosition(n + e - 1), this.bytes.subarray(n, n + e);
  },
  inspectUint8(e) {
    const t = e ?? this.position;
    return this.assertPosition(t), this.bytes[t];
  },
  inspectUint16(e) {
    const t = e ?? this.position;
    return this.assertPosition(t + 1), this.dataView.getUint16(t);
  },
  inspectUint24(e) {
    const t = e ?? this.position;
    return this.assertPosition(t + 2), (this.dataView.getUint16(t) << 8) + this.dataView.getUint8(t + 2);
  },
  inspectUint32(e) {
    const t = e ?? this.position;
    return this.assertPosition(t + 3), this.dataView.getUint32(t);
  },
  pushByte(e) {
    this.assertPosition(this.position), this.bytes[this.position] = e, this.position++;
  },
  pushBytes(e) {
    this.assertPosition(this.position + e.length - 1), this.bytes.set(e, this.position), this.position += e.length;
  },
  pushUint8(e) {
    this.assertPosition(this.position), this.bytes[this.position] = e, this.position++;
  },
  pushUint16(e) {
    this.assertPosition(this.position + 1), this.dataView.setUint16(this.position, e), this.position += 2;
  },
  pushUint24(e) {
    this.assertPosition(this.position + 2), this.dataView.setUint16(this.position, e >> 8), this.dataView.setUint8(this.position + 2, e & 255), this.position += 3;
  },
  pushUint32(e) {
    this.assertPosition(this.position + 3), this.dataView.setUint32(this.position, e), this.position += 4;
  },
  readByte() {
    this.assertReadLimit(), this._touch();
    const e = this.inspectByte();
    return this.position++, e;
  },
  readBytes(e, t) {
    this.assertReadLimit(), this._touch();
    const n = this.inspectBytes(e);
    return this.position += t ?? e, n;
  },
  readUint8() {
    this.assertReadLimit(), this._touch();
    const e = this.inspectUint8();
    return this.position += 1, e;
  },
  readUint16() {
    this.assertReadLimit(), this._touch();
    const e = this.inspectUint16();
    return this.position += 2, e;
  },
  readUint24() {
    this.assertReadLimit(), this._touch();
    const e = this.inspectUint24();
    return this.position += 3, e;
  },
  readUint32() {
    this.assertReadLimit(), this._touch();
    const e = this.inspectUint32();
    return this.position += 4, e;
  },
  get remaining() {
    return this.bytes.length - this.position;
  },
  setPosition(e) {
    const t = this.position;
    return this.assertPosition(e), this.position = e, () => this.position = t;
  },
  _touch() {
    if (this.recursiveReadLimit === Number.POSITIVE_INFINITY)
      return;
    const e = this.getReadCount();
    this.positionReadCount.set(this.position, e + 1), e > 0 && this.recursiveReadCount++;
  }
};
function gd(e, { recursiveReadLimit: t = 8192 } = {}) {
  const n = Object.create(zS);
  return n.bytes = e, n.dataView = new DataView(e.buffer ?? e, e.byteOffset, e.byteLength), n.positionReadCount = /* @__PURE__ */ new Map(), n.recursiveReadLimit = t, n;
}
function MS(e, t = {}) {
  typeof t.size < "u" && jt(e, { size: t.size });
  const n = Je(e, t);
  return zw(n, t);
}
function KS(e, t = {}) {
  let n = e;
  if (typeof t.size < "u" && (jt(n, { size: t.size }), n = ji(n)), n.length > 1 || n[0] > 1)
    throw new VE(n);
  return !!n[0];
}
function an(e, t = {}) {
  typeof t.size < "u" && jt(e, { size: t.size });
  const n = Je(e, t);
  return ld(n, t);
}
function FS(e, t = {}) {
  let n = e;
  return typeof t.size < "u" && (jt(n, { size: t.size }), n = ji(n, { dir: "right" })), new TextDecoder().decode(n);
}
function jS(e, t) {
  const n = typeof t == "string" ? Vn(t) : t, r = gd(n);
  if (Be(n) === 0 && e.length > 0)
    throw new HE();
  if (Be(t) && Be(t) < 32)
    throw new LE({
      data: typeof t == "string" ? t : Je(t),
      params: e,
      size: Be(t)
    });
  let i = 0;
  const o = [];
  for (let s = 0; s < e.length; ++s) {
    const a = e[s];
    i < n.length && r.setPosition(i);
    const [c, l] = jr(r, a, {
      staticPosition: 0
    });
    i += l, o.push(c);
  }
  return o;
}
function jr(e, t, { staticPosition: n }) {
  const r = ma(t.type);
  if (r) {
    const [i, o] = r;
    return qS(e, { ...t, type: o }, { length: i, staticPosition: n });
  }
  if (t.type === "tuple")
    return YS(e, t, { staticPosition: n });
  if (t.type === "address")
    return VS(e);
  if (t.type === "bool")
    return WS(e);
  if (t.type.startsWith("bytes"))
    return GS(e, t, { staticPosition: n });
  if (t.type.startsWith("uint") || t.type.startsWith("int"))
    return ZS(e, t);
  if (t.type === "string")
    return XS(e, { staticPosition: n });
  throw new KE(t.type, {
    docsPath: "/docs/contract/decodeAbiParameters"
  });
}
const gf = 32, Mc = 32;
function VS(e) {
  const t = e.readBytes(32);
  return [Jw(Je(tg(t, -20))), 32];
}
function qS(e, t, { length: n, staticPosition: r }) {
  if (n === null) {
    const s = an(e.readBytes(Mc)), a = r + s, c = a + gf;
    e.setPosition(a);
    const l = an(e.readBytes(gf)), d = Gi(t);
    let u = 0;
    const f = [];
    for (let h = 0; h < l; ++h) {
      e.setPosition(c + (d ? h * 32 : u));
      const [y, w] = jr(e, t, {
        staticPosition: c
      });
      u += w, f.push(y), w === 0 && (e.assertReadLimit(), e._touch());
    }
    return e.setPosition(r + 32), [f, 32];
  }
  if (Gi(t)) {
    const s = an(e.readBytes(Mc)), a = r + s, c = [];
    for (let l = 0; l < n; ++l) {
      e.setPosition(a + l * 32);
      const [d] = jr(e, t, {
        staticPosition: a
      });
      c.push(d);
    }
    return e.setPosition(r + 32), [c, 32];
  }
  let i = 0;
  const o = [];
  for (let s = 0; s < n; ++s) {
    const [a, c] = jr(e, t, {
      staticPosition: r + i
    });
    i += c, o.push(a), c === 0 && (e.assertReadLimit(), e._touch());
  }
  return [o, i];
}
function WS(e) {
  return [KS(e.readBytes(32), { size: 32 }), 32];
}
function GS(e, t, { staticPosition: n }) {
  const [r, i] = t.type.split("bytes");
  if (!i) {
    const s = an(e.readBytes(32));
    e.setPosition(n + s);
    const a = an(e.readBytes(32));
    if (a === 0)
      return e.setPosition(n + 32), ["0x", 32];
    const c = e.readBytes(a);
    return e.setPosition(n + 32), [Je(c), 32];
  }
  return [Je(e.readBytes(Number.parseInt(i, 10), 32)), 32];
}
function ZS(e, t) {
  const n = t.type.startsWith("int"), r = Number.parseInt(t.type.split("int")[1] || "256", 10), i = e.readBytes(32);
  return [
    r > 48 ? MS(i, { signed: n }) : an(i, { signed: n }),
    32
  ];
}
function YS(e, t, { staticPosition: n }) {
  const r = t.components.length === 0 || t.components.some(({ name: s }) => !s), i = r ? [] : {};
  let o = 0;
  if (Gi(t)) {
    const s = an(e.readBytes(Mc)), a = n + s;
    for (let c = 0; c < t.components.length; ++c) {
      const l = t.components[c];
      e.setPosition(a + o);
      const [d, u] = jr(e, l, {
        staticPosition: a
      });
      o += u, i[r ? c : l?.name] = d;
    }
    return e.setPosition(n + 32), [i, 32];
  }
  for (let s = 0; s < t.components.length; ++s) {
    const a = t.components[s], [c, l] = jr(e, a, {
      staticPosition: n
    });
    i[r ? s : a?.name] = c, o += l;
  }
  return [i, o];
}
function XS(e, { staticPosition: t }) {
  const n = an(e.readBytes(32)), r = t + n;
  e.setPosition(r);
  const i = an(e.readBytes(32));
  if (i === 0)
    return e.setPosition(t + 32), ["", 32];
  const o = e.readBytes(i, 32), s = FS(ji(o));
  return e.setPosition(t + 32), [s, 32];
}
function Gi(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components?.some(Gi);
  const n = ma(e.type);
  return !!(n && Gi({ ...e, type: n[1] }));
}
const JS = (e, t, n) => JSON.stringify(e, (r, i) => typeof i == "bigint" ? i.toString() : i, n), QS = {
  ether: -9,
  wei: 9
};
function eA(e, t) {
  let n = e.toString();
  const r = n.startsWith("-");
  r && (n = n.slice(1)), n = n.padStart(t, "0");
  let [i, o] = [
    n.slice(0, n.length - t),
    n.slice(n.length - t)
  ];
  return o = o.replace(/(0+)$/, ""), `${r ? "-" : ""}${i || "0"}${o ? `.${o}` : ""}`;
}
function Kc(e, t = "wei") {
  return eA(e, QS[t]);
}
function tA(e) {
  const t = Object.entries(e).map(([r, i]) => i === void 0 || i === !1 ? null : [r, i]).filter(Boolean), n = t.reduce((r, [i]) => Math.max(r, i.length), 0);
  return t.map(([r, i]) => `  ${`${r}:`.padEnd(n + 1)}  ${i}`).join(`
`);
}
class nA extends te {
  constructor({ transaction: t }) {
    super("Cannot infer a transaction type from provided transaction.", {
      metaMessages: [
        "Provided Transaction:",
        "{",
        tA(t),
        "}",
        "",
        "To infer the type, either provide:",
        "- a `type` to the Transaction, or",
        "- an EIP-1559 Transaction with `maxFeePerGas`, or",
        "- an EIP-2930 Transaction with `gasPrice` & `accessList`, or",
        "- an EIP-4844 Transaction with `blobs`, `blobVersionedHashes`, `sidecars`, or",
        "- an EIP-7702 Transaction with `authorizationList`, or",
        "- a Legacy Transaction with `gasPrice`"
      ],
      name: "InvalidSerializableTransactionError"
    });
  }
}
class rA extends te {
  constructor({ serializedType: t }) {
    super(`Serialized transaction type "${t}" is invalid.`, {
      name: "InvalidSerializedTransactionType"
    }), Object.defineProperty(this, "serializedType", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.serializedType = t;
  }
}
class iA extends te {
  constructor({ storageKey: t }) {
    super(`Size for storage key "${t}" is invalid. Expected 32 bytes. Got ${Math.floor((t.length - 2) / 2)} bytes.`, { name: "InvalidStorageKeySizeError" });
  }
}
function ni(e, t = "hex") {
  const n = ig(e), r = gd(new Uint8Array(n.length));
  return n.encode(r), t === "hex" ? Je(r.bytes) : r.bytes;
}
function ig(e) {
  return Array.isArray(e) ? oA(e.map((t) => ig(t))) : sA(e);
}
function oA(e) {
  const t = e.reduce((i, o) => i + o.length, 0), n = og(t);
  return {
    length: t <= 55 ? 1 + t : 1 + n + t,
    encode(i) {
      t <= 55 ? i.pushByte(192 + t) : (i.pushByte(247 + n), n === 1 ? i.pushUint8(t) : n === 2 ? i.pushUint16(t) : n === 3 ? i.pushUint24(t) : i.pushUint32(t));
      for (const { encode: o } of e)
        o(i);
    }
  };
}
function sA(e) {
  const t = typeof e == "string" ? Vn(e) : e, n = og(t.length);
  return {
    length: t.length === 1 && t[0] < 128 ? 1 : t.length <= 55 ? 1 + t.length : 1 + n + t.length,
    encode(i) {
      t.length === 1 && t[0] < 128 ? i.pushBytes(t) : t.length <= 55 ? (i.pushByte(128 + t.length), i.pushBytes(t)) : (i.pushByte(183 + n), n === 1 ? i.pushUint8(t.length) : n === 2 ? i.pushUint16(t.length) : n === 3 ? i.pushUint24(t.length) : i.pushUint32(t.length), i.pushBytes(t));
    }
  };
}
function og(e) {
  if (e < 2 ** 8)
    return 1;
  if (e < 2 ** 16)
    return 2;
  if (e < 2 ** 24)
    return 3;
  if (e < 2 ** 32)
    return 4;
  throw new te("Length is too large.");
}
class ba extends te {
  constructor({ cause: t, maxFeePerGas: n } = {}) {
    super(`The fee cap (\`maxFeePerGas\`${n ? ` = ${Kc(n)} gwei` : ""}) cannot be higher than the maximum allowed value (2^256-1).`, {
      cause: t,
      name: "FeeCapTooHighError"
    });
  }
}
Object.defineProperty(ba, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max fee per gas higher than 2\^256-1|fee cap higher than 2\^256-1/
});
class sg extends te {
  constructor({ cause: t, maxPriorityFeePerGas: n, maxFeePerGas: r } = {}) {
    super([
      `The provided tip (\`maxPriorityFeePerGas\`${n ? ` = ${Kc(n)} gwei` : ""}) cannot be higher than the fee cap (\`maxFeePerGas\`${r ? ` = ${Kc(r)} gwei` : ""}).`
    ].join(`
`), {
      cause: t,
      name: "TipAboveFeeCapError"
    });
  }
}
Object.defineProperty(sg, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max priority fee per gas higher than max fee per gas|tip higher than fee cap/
});
const md = 2n ** 256n - 1n;
function ag(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((o) => Vn(o)) : e.blobs, i = [];
  for (const o of r)
    i.push(Uint8Array.from(t.blobToKzgCommitment(o)));
  return n === "bytes" ? i : i.map((o) => Je(o));
}
function cg(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((s) => Vn(s)) : e.blobs, i = typeof e.commitments[0] == "string" ? e.commitments.map((s) => Vn(s)) : e.commitments, o = [];
  for (let s = 0; s < r.length; s++) {
    const a = r[s], c = i[s];
    o.push(Uint8Array.from(t.computeBlobKzgProof(a, c)));
  }
  return n === "bytes" ? o : o.map((s) => Je(s));
}
function aA(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function cA(e, t, n) {
  return e & t ^ ~e & n;
}
function lA(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
class dA extends Vw {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Xa(this.buffer);
  }
  update(t) {
    Ms(this), t = ud(t), qi(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Xa(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Ms(this), jw(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Wi(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let u = s; u < i; u++)
      n[u] = 0;
    aA(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Xa(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const l = c / 4, d = this.get();
    if (l > d.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let u = 0; u < l; u++)
      a.setUint32(4 * u, d[u], o);
  }
  digest() {
    const { buffer: t, outputLen: n } = this;
    this.digestInto(t);
    const r = t.slice(0, n);
    return this.destroy(), r;
  }
  _cloneInto(t) {
    t || (t = new this.constructor()), t.set(...this.get());
    const { blockLen: n, buffer: r, length: i, finished: o, destroyed: s, pos: a } = this;
    return t.destroyed = s, t.finished = o, t.length = i, t.pos = a, i % n && t.buffer.set(r), t;
  }
  clone() {
    return this._cloneInto();
  }
}
const Pn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), uA = /* @__PURE__ */ Uint32Array.from([
  1116352408,
  1899447441,
  3049323471,
  3921009573,
  961987163,
  1508970993,
  2453635748,
  2870763221,
  3624381080,
  310598401,
  607225278,
  1426881987,
  1925078388,
  2162078206,
  2614888103,
  3248222580,
  3835390401,
  4022224774,
  264347078,
  604807628,
  770255983,
  1249150122,
  1555081692,
  1996064986,
  2554220882,
  2821834349,
  2952996808,
  3210313671,
  3336571891,
  3584528711,
  113926993,
  338241895,
  666307205,
  773529912,
  1294757372,
  1396182291,
  1695183700,
  1986661051,
  2177026350,
  2456956037,
  2730485921,
  2820302411,
  3259730800,
  3345764771,
  3516065817,
  3600352804,
  4094571909,
  275423344,
  430227734,
  506948616,
  659060556,
  883997877,
  958139571,
  1322822218,
  1537002063,
  1747873779,
  1955562222,
  2024104815,
  2227730452,
  2361852424,
  2428436474,
  2756734187,
  3204031479,
  3329325298
]), $n = /* @__PURE__ */ new Uint32Array(64);
class fA extends dA {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = Pn[0] | 0, this.B = Pn[1] | 0, this.C = Pn[2] | 0, this.D = Pn[3] | 0, this.E = Pn[4] | 0, this.F = Pn[5] | 0, this.G = Pn[6] | 0, this.H = Pn[7] | 0;
  }
  get() {
    const { A: t, B: n, C: r, D: i, E: o, F: s, G: a, H: c } = this;
    return [t, n, r, i, o, s, a, c];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c) {
    this.A = t | 0, this.B = n | 0, this.C = r | 0, this.D = i | 0, this.E = o | 0, this.F = s | 0, this.G = a | 0, this.H = c | 0;
  }
  process(t, n) {
    for (let u = 0; u < 16; u++, n += 4)
      $n[u] = t.getUint32(n, !1);
    for (let u = 16; u < 64; u++) {
      const f = $n[u - 15], h = $n[u - 2], y = Pt(f, 7) ^ Pt(f, 18) ^ f >>> 3, w = Pt(h, 17) ^ Pt(h, 19) ^ h >>> 10;
      $n[u] = w + $n[u - 7] + y + $n[u - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: d } = this;
    for (let u = 0; u < 64; u++) {
      const f = Pt(a, 6) ^ Pt(a, 11) ^ Pt(a, 25), h = d + f + cA(a, c, l) + uA[u] + $n[u] | 0, w = (Pt(r, 2) ^ Pt(r, 13) ^ Pt(r, 22)) + lA(r, i, o) | 0;
      d = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, d = d + this.H | 0, this.set(r, i, o, s, a, c, l, d);
  }
  roundClean() {
    Wi($n);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Wi(this.buffer);
  }
}
const hA = /* @__PURE__ */ qw(() => new fA()), pA = hA;
function yA(e, t) {
  return pA(eo(e, { strict: !1 }) ? dd(e) : e);
}
function wA(e) {
  const { commitment: t, version: n = 1 } = e, r = e.to ?? (typeof t == "string" ? "hex" : "bytes"), i = yA(t);
  return i.set([n], 0), r === "bytes" ? i : Je(i);
}
function gA(e) {
  const { commitments: t, version: n } = e, r = e.to ?? (typeof t[0] == "string" ? "hex" : "bytes"), i = [];
  for (const o of t)
    i.push(wA({
      commitment: o,
      to: r,
      version: n
    }));
  return i;
}
const mf = 6, lg = 32, bd = 4096, dg = lg * bd, bf = dg * mf - // terminator byte (0x80).
1 - // zero byte (0x00) appended to each field element.
1 * bd * mf, ug = 1;
class mA extends te {
  constructor({ maxSize: t, size: n }) {
    super("Blob size is too large.", {
      metaMessages: [`Max: ${t} bytes`, `Given: ${n} bytes`],
      name: "BlobSizeTooLargeError"
    });
  }
}
class fg extends te {
  constructor() {
    super("Blob data must not be empty.", { name: "EmptyBlobError" });
  }
}
class bA extends te {
  constructor({ hash: t, size: n }) {
    super(`Versioned hash "${t}" size is invalid.`, {
      metaMessages: ["Expected: 32", `Received: ${n}`],
      name: "InvalidVersionedHashSizeError"
    });
  }
}
class vA extends te {
  constructor({ hash: t, version: n }) {
    super(`Versioned hash "${t}" version is invalid.`, {
      metaMessages: [
        `Expected: ${ug}`,
        `Received: ${n}`
      ],
      name: "InvalidVersionedHashVersionError"
    });
  }
}
function xA(e) {
  const t = e.to ?? (typeof e.data == "string" ? "hex" : "bytes"), n = typeof e.data == "string" ? Vn(e.data) : e.data, r = Be(n);
  if (!r)
    throw new fg();
  if (r > bf)
    throw new mA({
      maxSize: bf,
      size: r
    });
  const i = [];
  let o = !0, s = 0;
  for (; o; ) {
    const a = gd(new Uint8Array(dg));
    let c = 0;
    for (; c < bd; ) {
      const l = n.slice(s, s + (lg - 1));
      if (a.pushByte(0), a.pushBytes(l), l.length < 31) {
        a.pushByte(128), o = !1;
        break;
      }
      c++, s += 31;
    }
    i.push(a);
  }
  return t === "bytes" ? i.map((a) => a.bytes) : i.map((a) => Je(a.bytes));
}
function kA(e) {
  const { data: t, kzg: n, to: r } = e, i = e.blobs ?? xA({ data: t, to: r }), o = e.commitments ?? ag({ blobs: i, kzg: n, to: r }), s = e.proofs ?? cg({ blobs: i, commitments: o, kzg: n, to: r }), a = [];
  for (let c = 0; c < i.length; c++)
    a.push({
      blob: i[c],
      commitment: o[c],
      proof: s[c]
    });
  return a;
}
function EA(e) {
  if (e.type)
    return e.type;
  if (typeof e.authorizationList < "u")
    return "eip7702";
  if (typeof e.blobs < "u" || typeof e.blobVersionedHashes < "u" || typeof e.maxFeePerBlobGas < "u" || typeof e.sidecars < "u")
    return "eip4844";
  if (typeof e.maxFeePerGas < "u" || typeof e.maxPriorityFeePerGas < "u")
    return "eip1559";
  if (typeof e.gasPrice < "u")
    return typeof e.accessList < "u" ? "eip2930" : "legacy";
  throw new nA({ transaction: e });
}
class va extends te {
  constructor({ chainId: t }) {
    super(typeof t == "number" ? `Chain ID "${t}" is invalid.` : "Chain ID is invalid.", { name: "InvalidChainIdError" });
  }
}
function SA(e) {
  const { authorizationList: t } = e;
  if (t)
    for (const n of t) {
      const { chainId: r } = n, i = n.address;
      if (!vr(i))
        throw new br({ address: i });
      if (r < 0)
        throw new va({ chainId: r });
    }
  vd(e);
}
function AA(e) {
  const { blobVersionedHashes: t } = e;
  if (t) {
    if (t.length === 0)
      throw new fg();
    for (const n of t) {
      const r = Be(n), i = ld(hd(n, 0, 1));
      if (r !== 32)
        throw new bA({ hash: n, size: r });
      if (i !== ug)
        throw new vA({
          hash: n,
          version: i
        });
    }
  }
  vd(e);
}
function vd(e) {
  const { chainId: t, maxPriorityFeePerGas: n, maxFeePerGas: r, to: i } = e;
  if (t <= 0)
    throw new va({ chainId: t });
  if (i && !vr(i))
    throw new br({ address: i });
  if (r && r > md)
    throw new ba({ maxFeePerGas: r });
  if (n && r && n > r)
    throw new sg({ maxFeePerGas: r, maxPriorityFeePerGas: n });
}
function _A(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (t <= 0)
    throw new va({ chainId: t });
  if (o && !vr(o))
    throw new br({ address: o });
  if (n || i)
    throw new te("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid EIP-2930 Transaction attribute.");
  if (r && r > md)
    throw new ba({ maxFeePerGas: r });
}
function IA(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (o && !vr(o))
    throw new br({ address: o });
  if (typeof t < "u" && t <= 0)
    throw new va({ chainId: t });
  if (n || i)
    throw new te("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid Legacy Transaction attribute.");
  if (r && r > md)
    throw new ba({ maxFeePerGas: r });
}
function xa(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (let n = 0; n < e.length; n++) {
    const { address: r, storageKeys: i } = e[n];
    for (let o = 0; o < i.length; o++)
      if (i[o].length - 2 !== 64)
        throw new iA({ storageKey: i[o] });
    if (!vr(r, { strict: !1 }))
      throw new br({ address: r });
    t.push([r, i]);
  }
  return t;
}
function BA(e, t) {
  const n = EA(e);
  return n === "eip1559" ? $A(e, t) : n === "eip2930" ? OA(e, t) : n === "eip4844" ? PA(e, t) : n === "eip7702" ? TA(e, t) : CA(e);
}
function TA(e, t) {
  const { authorizationList: n, chainId: r, gas: i, nonce: o, to: s, value: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: d, data: u } = e;
  SA(e);
  const f = xa(d), h = RA(n);
  return Et([
    "0x04",
    ni([
      ee(r),
      o ? ee(o) : "0x",
      l ? ee(l) : "0x",
      c ? ee(c) : "0x",
      i ? ee(i) : "0x",
      s ?? "0x",
      a ? ee(a) : "0x",
      u ?? "0x",
      f,
      h,
      ...to(e, t)
    ])
  ]);
}
function PA(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerBlobGas: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: d, data: u } = e;
  AA(e);
  let f = e.blobVersionedHashes, h = e.sidecars;
  if (e.blobs && (typeof f > "u" || typeof h > "u")) {
    const b = typeof e.blobs[0] == "string" ? e.blobs : e.blobs.map((R) => Je(R)), O = e.kzg, B = ag({
      blobs: b,
      kzg: O
    });
    if (typeof f > "u" && (f = gA({
      commitments: B
    })), typeof h > "u") {
      const R = cg({ blobs: b, commitments: B, kzg: O });
      h = kA({ blobs: b, commitments: B, proofs: R });
    }
  }
  const y = xa(d), w = [
    ee(n),
    i ? ee(i) : "0x",
    l ? ee(l) : "0x",
    c ? ee(c) : "0x",
    r ? ee(r) : "0x",
    o ?? "0x",
    s ? ee(s) : "0x",
    u ?? "0x",
    y,
    a ? ee(a) : "0x",
    f ?? [],
    ...to(e, t)
  ], p = [], g = [], k = [];
  if (h)
    for (let b = 0; b < h.length; b++) {
      const { blob: O, commitment: B, proof: R } = h[b];
      p.push(O), g.push(B), k.push(R);
    }
  return Et([
    "0x03",
    // If sidecars are enabled, envelope turns into a "wrapper":
    ni(h ? [w, p, g, k] : w)
  ]);
}
function $A(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerGas: a, maxPriorityFeePerGas: c, accessList: l, data: d } = e;
  vd(e);
  const u = xa(l), f = [
    ee(n),
    i ? ee(i) : "0x",
    c ? ee(c) : "0x",
    a ? ee(a) : "0x",
    r ? ee(r) : "0x",
    o ?? "0x",
    s ? ee(s) : "0x",
    d ?? "0x",
    u,
    ...to(e, t)
  ];
  return Et([
    "0x02",
    ni(f)
  ]);
}
function OA(e, t) {
  const { chainId: n, gas: r, data: i, nonce: o, to: s, value: a, accessList: c, gasPrice: l } = e;
  _A(e);
  const d = xa(c), u = [
    ee(n),
    o ? ee(o) : "0x",
    l ? ee(l) : "0x",
    r ? ee(r) : "0x",
    s ?? "0x",
    a ? ee(a) : "0x",
    i ?? "0x",
    d,
    ...to(e, t)
  ];
  return Et([
    "0x01",
    ni(u)
  ]);
}
function CA(e, t) {
  const { chainId: n = 0, gas: r, data: i, nonce: o, to: s, value: a, gasPrice: c } = e;
  IA(e);
  let l = [
    o ? ee(o) : "0x",
    c ? ee(c) : "0x",
    r ? ee(r) : "0x",
    s ?? "0x",
    a ? ee(a) : "0x",
    i ?? "0x"
  ];
  return n > 0 && (l = [
    ...l,
    ee(n),
    "0x",
    "0x"
  ]), ni(l);
}
function to(e, t) {
  const n = t ?? e, { v: r, yParity: i } = n;
  if (typeof n.r > "u")
    return [];
  if (typeof n.s > "u")
    return [];
  if (typeof r > "u" && typeof i > "u")
    return [];
  const o = ji(n.r), s = ji(n.s);
  return [typeof i == "number" ? i ? ee(1) : "0x" : r === 0n ? "0x" : r === 1n ? ee(1) : r === 27n ? "0x" : ee(1), o === "0x00" ? "0x" : o, s === "0x00" ? "0x" : s];
}
function RA(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (const n of e) {
    const { chainId: r, nonce: i, ...o } = n, s = n.address;
    t.push([
      r ? Vi(r) : "0x",
      s,
      i ? Vi(i) : "0x",
      ...to({}, o)
    ]);
  }
  return t;
}
class LA extends te {
  constructor({ domain: t }) {
    super(`Invalid domain "${JS(t)}".`, {
      metaMessages: ["Must be a valid EIP-712 domain."]
    });
  }
}
class HA extends te {
  constructor({ primaryType: t, types: n }) {
    super(`Invalid primary type \`${t}\` must be one of \`${JSON.stringify(Object.keys(n))}\`.`, {
      docsPath: "/api/glossary/Errors#typeddatainvalidprimarytypeerror",
      metaMessages: ["Check that the primary type is a key in `types`."]
    });
  }
}
class NA extends te {
  constructor({ type: t }) {
    super(`Struct type "${t}" is invalid.`, {
      metaMessages: ["Struct type must not be a Solidity type."],
      name: "InvalidStructTypeError"
    });
  }
}
function UA(e) {
  const { domain: t, message: n, primaryType: r, types: i } = e, o = (s, a) => {
    for (const c of s) {
      const { name: l, type: d } = c, u = a[l], f = d.match(rg);
      if (f && (typeof u == "number" || typeof u == "bigint")) {
        const [w, p, g] = f;
        ee(u, {
          signed: p === "int",
          size: Number.parseInt(g, 10) / 8
        });
      }
      if (d === "address" && typeof u == "string" && !vr(u))
        throw new br({ address: u });
      const h = d.match(BS);
      if (h) {
        const [w, p] = h;
        if (p && Be(u) !== Number.parseInt(p, 10))
          throw new zE({
            expectedSize: Number.parseInt(p, 10),
            givenSize: Be(u)
          });
      }
      const y = i[d];
      y && (DA(d), o(y, u));
    }
  };
  if (i.EIP712Domain && t) {
    if (typeof t != "object")
      throw new LA({ domain: t });
    o(i.EIP712Domain, t);
  }
  if (r !== "EIP712Domain")
    if (i[r])
      o(i[r], n);
    else
      throw new HA({ primaryType: r, types: i });
}
function hg({ domain: e }) {
  return [
    typeof e?.name == "string" && { name: "name", type: "string" },
    e?.version && { name: "version", type: "string" },
    (typeof e?.chainId == "number" || typeof e?.chainId == "bigint") && {
      name: "chainId",
      type: "uint256"
    },
    e?.verifyingContract && {
      name: "verifyingContract",
      type: "address"
    },
    e?.salt && { name: "salt", type: "bytes32" }
  ].filter(Boolean);
}
function DA(e) {
  if (e === "address" || e === "bool" || e === "string" || e.startsWith("bytes") || e.startsWith("uint") || e.startsWith("int"))
    throw new NA({ type: e });
}
function zA(e) {
  const { domain: t = {}, message: n, primaryType: r } = e, i = {
    EIP712Domain: hg({ domain: t }),
    ...e.types
  };
  UA({
    domain: t,
    message: n,
    primaryType: r,
    types: i
  });
  const o = ["0x1901"];
  return t && o.push(MA({
    domain: t,
    types: i
  })), r !== "EIP712Domain" && o.push(pg({
    data: n,
    primaryType: r,
    types: i
  })), Nt(_S(o));
}
function MA({ domain: e, types: t }) {
  return pg({
    data: e,
    primaryType: "EIP712Domain",
    types: t
  });
}
function pg({ data: e, primaryType: t, types: n }) {
  const r = yg({
    data: e,
    primaryType: t,
    types: n
  });
  return Nt(r);
}
function yg({ data: e, primaryType: t, types: n }) {
  const r = [{ type: "bytes32" }], i = [KA({ primaryType: t, types: n })];
  for (const o of n[t]) {
    const [s, a] = gg({
      types: n,
      name: o.name,
      type: o.type,
      value: e[o.name]
    });
    r.push(s), i.push(a);
  }
  return pd(r, i);
}
function KA({ primaryType: e, types: t }) {
  const n = Vi(FA({ primaryType: e, types: t }));
  return Nt(n);
}
function FA({ primaryType: e, types: t }) {
  let n = "";
  const r = wg({ primaryType: e, types: t });
  r.delete(e);
  const i = [e, ...Array.from(r).sort()];
  for (const o of i)
    n += `${o}(${t[o].map(({ name: s, type: a }) => `${a} ${s}`).join(",")})`;
  return n;
}
function wg({ primaryType: e, types: t }, n = /* @__PURE__ */ new Set()) {
  const i = e.match(/^\w*/u)?.[0];
  if (n.has(i) || t[i] === void 0)
    return n;
  n.add(i);
  for (const o of t[i])
    wg({ primaryType: o.type, types: t }, n);
  return n;
}
function gg({ types: e, name: t, type: n, value: r }) {
  if (e[n] !== void 0)
    return [
      { type: "bytes32" },
      Nt(yg({ data: r, primaryType: n, types: e }))
    ];
  if (n === "bytes")
    return [{ type: "bytes32" }, Nt(r)];
  if (n === "string")
    return [{ type: "bytes32" }, Nt(Vi(r))];
  if (n.lastIndexOf("]") === n.length - 1) {
    const i = n.slice(0, n.lastIndexOf("[")), o = r.map((s) => gg({
      name: t,
      type: i,
      types: e,
      value: s
    }));
    return [
      { type: "bytes32" },
      Nt(pd(o.map(([s]) => s), o.map(([, s]) => s)))
    ];
  }
  return [{ type: n }, r];
}
function jA(e) {
  const t = ng(e, 0, 1);
  if (t === "0x04")
    return "eip7702";
  if (t === "0x03")
    return "eip4844";
  if (t === "0x02")
    return "eip1559";
  if (t === "0x01")
    return "eip2930";
  if (t !== "0x" && ld(t) >= 192)
    return "legacy";
  throw new rA({ serializedType: t });
}
const vf = "bitcoin-taproot-keypath-v1", xf = "bip341-sighash-default-keypath", mg = ["bitcoin", "testnet", "signet", "regtest"], ri = 21000000n * 100000000n, bg = 137, vg = /^[0-9a-f]+$/, VA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", qA = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", WA = 734539939, GA = 1;
function re(e) {
  throw new _("invalid_payload", e);
}
function kf(e, t) {
  return (!e || typeof e != "object" || Array.isArray(e)) && re(`${t} must be an object`), e;
}
function Ef(e, t, n) {
  const r = Object.keys(e).filter((o) => !t.includes(o));
  r.length > 0 && re(`${n} carries unsupported fields: ${r.sort().join(", ")}`);
  const i = t.filter((o) => !(o in e));
  i.length > 0 && re(`${n} is missing required fields: ${i.join(", ")}`);
}
function Go(e, t, n) {
  return (typeof e != "string" || n !== void 0 && e.length !== n || e.length === 0 || !vg.test(e)) && re(`${t} must be lowercase hexadecimal${n === void 0 ? "" : ` of length ${n}`}`), e;
}
function ZA(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < n || e > r) && re(`${t} must be an integer in ${n}..${r}`), e;
}
function he(...e) {
  const t = new Uint8Array(e.reduce((r, i) => r + i.length, 0));
  let n = 0;
  for (const r of e)
    t.set(r, n), n += r.length;
  return t;
}
function Or(e, t, n, r) {
  return t + n > e.length && re(`transaction is truncated while reading ${r}`), [e.slice(t, t + n), t + n];
}
function Lo(e, t, n) {
  t >= e.length && re(`transaction is truncated before ${n}`);
  const r = e[t];
  return r >= 253 && re(`${n} must use a single-byte CompactSize`), [r, t + 1];
}
function ec(e) {
  return ((e[0] ?? 0) | (e[1] ?? 0) << 8 | (e[2] ?? 0) << 16 | (e[3] ?? 0) << 24) >>> 0;
}
function YA(e) {
  let t = 0n;
  for (let n = 0; n < 8; n += 1) t |= BigInt(e[n] ?? 0) << BigInt(n * 8);
  return t;
}
function Ke(e) {
  return new Uint8Array([e & 255, e >>> 8 & 255, e >>> 16 & 255, e >>> 24 & 255]);
}
function Ks(e) {
  const t = new Uint8Array(8);
  let n = e;
  for (let r = 0; r < 8; r += 1)
    t[r] = Number(n & 0xffn), n >>= 8n;
  return t;
}
function xg(e) {
  if (e.length === 25 && e[0] === 118 && e[1] === 169 && e[2] === 20 && e[23] === 136 && e[24] === 172) return "p2pkh";
  if (e.length === 23 && e[0] === 169 && e[1] === 20 && e[22] === 135) return "p2sh";
  if (e.length === 22 && e[0] === 0 && e[1] === 20) return "p2wpkh";
  if (e.length === 34 && e[0] === 0 && e[1] === 32) return "p2wsh";
  if (e.length === 34 && e[0] === 81 && e[1] === 32) return "p2tr";
  re("transaction output uses an unsupported script template");
}
function kg(e) {
  (e.length === 0 || e.length % 2 || e.length > bg * 2 || !vg.test(e)) && re("unsignedTransaction must be lowercase hex, without 0x or whitespace, and at most 137 bytes");
  const t = Uint8Array.from(e.match(/../g), (H) => Number.parseInt(H, 16));
  (t.length < 4 || t[0] !== 2 || t[1] !== 0 || t[2] !== 0 || t[3] !== 0) && re("transaction version must be exactly 2");
  let n = 4;
  const [r, i] = Lo(t, n, "input count");
  n = i, r !== 1 && re("transaction must contain exactly one input");
  const [o, s] = Or(t, n, 32, "outpoint hash");
  n = s, o.every((H) => H === 0) && re("outpoint hash must not be null");
  const [a, c] = Or(t, n, 4, "outpoint index");
  n = c;
  const l = ec(a), [d, u] = Lo(t, n, "scriptSig length");
  n = u, d !== 0 && re("scriptSig must be empty");
  const [f, h] = Or(t, n, 4, "sequence");
  n = h;
  const y = ec(f);
  y !== 4294967293 && y !== 4294967294 && y !== 4294967295 && re("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff");
  const [w, p] = Lo(t, n, "output count");
  n = p, w !== 1 && w !== 2 && re("transaction must contain one or two outputs");
  const g = [], k = { p2pkh: 546n, p2sh: 540n, p2wpkh: 294n, p2wsh: 330n, p2tr: 330n };
  for (let H = 0; H < w; H += 1) {
    const [L, T] = Or(t, n, 8, `output ${H} amount`);
    n = T;
    const D = YA(L);
    (D < 1n || D > ri) && re(`output ${H} amount is outside the Bitcoin money range`);
    const [z, E] = Lo(t, n, `output ${H} script length`);
    n = E;
    const [x, v] = Or(t, n, z, `output ${H} script`);
    n = v;
    const m = xg(x);
    z !== { p2pkh: 25, p2sh: 23, p2wpkh: 22, p2wsh: 34, p2tr: 34 }[m] && re(`output ${H} script length is not canonical for ${m}`), D < k[m] && re(`output ${H} is below the V1 ${m} dust floor`), g.push({ amountSats: D, scriptPubKey: x, kind: m });
  }
  const [b, O] = Or(t, n, 4, "locktime");
  n = O;
  const B = ec(b);
  B !== 0 && re("locktime must be exactly zero"), n !== t.length && re("transaction has trailing bytes");
  const R = g.reduce((H, L) => H + L.amountSats, 0n);
  return (R < 1n || R > ri) && re("output total is outside the Bitcoin money range"), { raw: t, prevoutHashLe: o, prevoutIndex: l, sequence: y, outputs: g, locktime: B };
}
function XA(e, t) {
  const n = ue(new TextEncoder().encode(e));
  return ue(he(n, n, t));
}
function JA(e, t, n) {
  const r = he(e.prevoutHashLe, Ke(e.prevoutIndex)), i = he(new Uint8Array([n.length]), n), o = e.outputs.map((a) => he(Ks(a.amountSats), new Uint8Array([a.scriptPubKey.length]), a.scriptPubKey)), s = he(
    new Uint8Array([0, 0]),
    Ke(2),
    Ke(e.locktime),
    ue(r),
    ue(Ks(t)),
    ue(i),
    ue(Ke(e.sequence)),
    ue(he(...o)),
    new Uint8Array([0]),
    Ke(0)
  );
  return XA("TapSighash", s);
}
function QA(e) {
  return BigInt(`0x${G(e)}`);
}
function Fc(e) {
  return yl(ue(e));
}
function Sf(e, t) {
  const n = he(new Uint8Array([e]), t), r = ue(ue(n)).slice(0, 4), i = he(n, r);
  let o = QA(i), s = "";
  for (; o > 0n; ) {
    const a = Number(o % 58n);
    s = VA[a] + s, o /= 58n;
  }
  for (const a of i) {
    if (a !== 0) break;
    s = `1${s}`;
  }
  return s;
}
function e_(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >> i & s);
  return i > 0 && o.push(r << n - i & s), o;
}
function t_(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1) i >>> o & 1 && (n ^= t[o]);
  }
  return n >>> 0;
}
function tc(e, t, n) {
  const r = e === "bitcoin" ? "bc" : e === "regtest" ? "bcrt" : "tb", i = [t, ...e_(n, 8, 5)], o = [...r].map((l) => l.charCodeAt(0) >> 5).concat([0], [...r].map((l) => l.charCodeAt(0) & 31)), s = t === 0 ? GA : WA, a = t_(o.concat(i, [0, 0, 0, 0, 0, 0])) ^ s, c = Array.from({ length: 6 }, (l, d) => a >>> 5 * (5 - d) & 31);
  return `${r}1${i.concat(c).map((l) => qA[l]).join("")}`;
}
function Zi(e, t) {
  const n = xg(t), r = e === "bitcoin";
  return n === "p2pkh" ? Sf(r ? 0 : 111, t.slice(3, 23)) : n === "p2sh" ? Sf(r ? 5 : 196, t.slice(2, 22)) : n === "p2wpkh" || n === "p2wsh" ? tc(e, 0, t.slice(2)) : tc(e, 1, t.slice(2));
}
function n_(e) {
  const t = Fc(e), n = he(new Uint8Array([0, 20]), t);
  return [
    he(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    n,
    he(new Uint8Array([169, 20]), Fc(n), new Uint8Array([135]))
  ];
}
function Vr(e, t) {
  return e.length === t.length && e.every((n, r) => n === t[r]);
}
function Ho(e) {
  const t = e / 100000000n, n = e % 100000000n;
  return n === 0n ? `${t}.00000000` : `${t}.${n.toString().padStart(8, "0")}`;
}
function Eg(e, t) {
  const n = e / BigInt(t), r = e % BigInt(t);
  if (r === 0n) return `${n.toString()} sat/vB`;
  const i = (r * 10000n / BigInt(t)).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n.toString()}.${i} sat/vB`;
}
function Sg(e) {
  return G(ue(ue(e.raw)).slice().reverse());
}
function r_(e, t) {
  const n = t.transaction, r = J("bitcoin.network", { network: e.network }), i = [
    J("bitcoin.title"),
    ...t.highFeeWarning ? [J("bitcoin.highFeeWarning"), ""] : [],
    r,
    J("bitcoin.networkWarning"),
    J("bitcoin.input", { txid: G(n.prevoutHashLe.slice().reverse()), vout: n.prevoutIndex }),
    J("bitcoin.inputAmount", { sats: e.prevout.amountSats, btc: Ho(BigInt(e.prevout.amountSats)) }),
    J("bitcoin.outputs")
  ];
  return n.outputs.forEach((o, s) => {
    const a = t.sameKeyOutputIndexes.includes(s);
    i.push(J("bitcoin.output", {
      index: s + 1,
      address: Zi(e.network, o.scriptPubKey),
      sats: o.amountSats.toString(),
      btc: Ho(o.amountSats),
      role: J(a ? "bitcoin.sameKeyReturn" : "bitcoin.recipient")
    }));
  }), i.push(
    J("bitcoin.fee", { sats: t.feeSats.toString(), btc: Ho(t.feeSats), rate: Eg(t.feeSats, t.signedVsize) }),
    J("bitcoin.vsize", { vsize: t.signedVsize }),
    J(n.sequence === 4294967293 ? "bitcoin.rbfSignals" : "bitcoin.rbfDoesNotSignal"),
    J("bitcoin.netLeaving", { sats: t.netLeavingSats.toString(), btc: Ho(t.netLeavingSats) }),
    J("bitcoin.unsignedTxid", { txid: Sg(n) }),
    J("bitcoin.outputKey", { key: G(t.outputKeyXOnly) })
  ), i.join(`
`);
}
function i_(e, t) {
  const n = kf(e, "signingContext");
  Ef(n, ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"], "bitcoin signingContext"), n.kind !== vf && re("unsupported Bitcoin signing_context kind"), (typeof n.network != "string" || !mg.includes(n.network)) && re("bitcoin signing_context network is invalid");
  const r = Go(n.unsignedTransaction, "unsignedTransaction");
  r.length > bg * 2 && re("unsignedTransaction exceeds the 137-byte V1 limit");
  const i = kf(n.prevout, "signingContext.prevout");
  Ef(i, ["amountSats", "scriptPubKey"], "signingContext.prevout");
  const o = ZA(i.amountSats, "prevout.amountSats", 1, Number(ri)), s = Go(i.scriptPubKey, "prevout.scriptPubKey", 68);
  s.startsWith("5120") || re("prevout.scriptPubKey must be a lowercase P2TR output key script"), n.digestAlgorithm !== xf && re("unsupported Bitcoin digest algorithm");
  const a = Go(n.digest, "signingContext.digest", 64);
  return a !== t && re("Bitcoin signing_context digest must match the sign request digest exactly"), {
    kind: vf,
    network: n.network,
    unsignedTransaction: r,
    prevout: { amountSats: o, scriptPubKey: s },
    digestAlgorithm: xf,
    digest: a
  };
}
function Ag(e, t, n) {
  const r = i_(e, t), i = kg(r.unsignedTransaction), o = Uint8Array.from(r.prevout.scriptPubKey.match(/../g), (g) => Number.parseInt(g, 16)), s = o.slice(2);
  let a;
  if (n !== void 0) {
    const g = Go(n, "signing public key", 66);
    a = Uint8Array.from(g.match(/../g), (b) => Number.parseInt(b, 16)), a[0] !== 2 && a[0] !== 3 && re("signing public key must be compressed secp256k1");
    const k = Ky(g);
    Vr(s, k) || re("prevout.scriptPubKey does not bind to the signing key's BIP86 output key");
  }
  if (a)
    for (const g of i.outputs)
      n_(a).some((k) => Vr(k, g.scriptPubKey)) && re("transaction sends to a same-key legacy or SegWit-v0 address this workflow cannot spend");
  const c = i.outputs.flatMap((g, k) => Vr(g.scriptPubKey, o) ? [k] : []);
  c.length > 1 && re("transaction may contain at most one same-key P2TR return output");
  const l = i.outputs.reduce((g, k) => g + k.amountSats, 0n), d = BigInt(r.prevout.amountSats) - l;
  d <= 0n && re("transaction fee must be positive");
  const u = i.raw.length * 4 + 68, f = Math.ceil(u / 4);
  d > 10000n * BigInt(f) && re("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling");
  const h = G(JA(i, BigInt(r.prevout.amountSats), o));
  h !== r.digest && re("Bitcoin signing_context digest does not match the independently computed BIP341 digest");
  const y = c.reduce((g, k) => g + i.outputs[k].amountSats, 0n), w = d > 100n * BigInt(f) || d * 100n > BigInt(r.prevout.amountSats) * 2n, p = BigInt(r.prevout.amountSats) - y;
  return {
    digest: h,
    display: r_(r, {
      transaction: i,
      outputKeyXOnly: s,
      feeSats: d,
      signedVsize: f,
      sameKeyOutputIndexes: c,
      highFeeWarning: w,
      netLeavingSats: p
    }),
    facts: {
      network: r.network,
      spendingAddress: Zi(r.network, o),
      inputTxid: G(i.prevoutHashLe.slice().reverse()),
      inputVout: i.prevoutIndex,
      inputAmountSats: BigInt(r.prevout.amountSats),
      outputs: i.outputs.map((g, k) => ({
        address: Zi(r.network, g.scriptPubKey),
        amountSats: g.amountSats,
        sameKey: c.includes(k)
      })),
      feeSats: d,
      feeRate: Eg(d, f),
      signedVsize: f,
      netLeavingSats: p,
      highFeeWarning: w,
      sequence: i.sequence,
      signalsRbf: i.sequence === 4294967293,
      locktime: i.locktime,
      unsignedTxid: Sg(i),
      ...n !== void 0 ? { internalKeyXOnly: n.slice(2).toLowerCase() } : {},
      outputKeyXOnly: G(s),
      unsignedTransaction: r.unsignedTransaction,
      prevoutScriptPubKey: r.prevout.scriptPubKey
    },
    transaction: i,
    outputKeyXOnly: s
  };
}
const Af = `avault-agent-operation:v1
`;
function Cr(e) {
  if (typeof e != "object" || e === null || Array.isArray(e)) throw new Error("signing context is malformed");
  return e;
}
function _f(e) {
  return e.startsWith("0x") ? e.toLowerCase() : `0x${e.toLowerCase()}`;
}
function nc(e, t) {
  const n = _f(e), r = _f(t);
  if (n !== r)
    throw new Error("signing digest does not match verified context");
  return r.slice(2);
}
function Zo(e) {
  return typeof e == "bigint" ? e.toString() : typeof e == "string" || typeof e == "number" || typeof e == "boolean" ? String(e) : e == null ? "" : qr(e);
}
function qr(e) {
  if (typeof e == "bigint") return JSON.stringify(e.toString());
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(qr).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${qr(t[n])}`).join(",")}}`;
}
const o_ = CE([
  "function approve(address spender,uint256 amount)",
  // A common extension, not an EIP-20 method: the amount is added to the current allowance.
  "function increaseAllowance(address spender,uint256 addedValue)",
  "function transfer(address to,uint256 amount)",
  "function transferFrom(address from,address to,uint256 amount)",
  "function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
  "function setApprovalForAll(address operator,bool approved)",
  "function safeTransferFrom(address from,address to,uint256 tokenId)",
  "function safeTransferFrom(address from,address to,uint256 tokenId,bytes data)"
]), s_ = {
  approve: "ERC-20/ERC-721 approve",
  increaseAllowance: "ERC-20 increaseAllowance (extension)",
  transfer: "ERC-20 transfer",
  transferFrom: "ERC-20/ERC-721 transferFrom",
  permit: "ERC-20 permit",
  setApprovalForAll: "ERC-721 setApprovalForAll",
  safeTransferFrom: "ERC-721 safeTransferFrom",
  safeTransferFromWithData: "ERC-721 safeTransferFrom"
}, a_ = new Map(
  o_.map((e) => {
    const t = e.name === "safeTransferFrom" && e.inputs.length === 4 ? "safeTransferFromWithData" : e.name;
    return [NS(e), { call: t, item: e }];
  })
);
function c_(e) {
  return `${e.name}(${e.inputs.map((t) => t.type).join(",")})`;
}
function l_(e) {
  if (e == null || e === "" || e === "0x") return "0x";
  if (typeof e != "string" || !/^0x[0-9a-fA-F]*$/.test(e) || e.length % 2 !== 0)
    throw new Error("EVM transaction calldata is malformed");
  return e.toLowerCase();
}
const d_ = /* @__PURE__ */ new Set([
  "chainId",
  "type",
  "to",
  "value",
  "nonce",
  "gas",
  "gasPrice",
  "maxFeePerGas",
  "maxPriorityFeePerGas",
  "data"
]), u_ = /* @__PURE__ */ new Set(["domain", "types", "primaryType", "message"]);
function rc(e, t, n) {
  const r = Object.keys(e).filter((i) => !t.has(i));
  if (r.length > 0)
    throw new Error(`${n} carries unsupported field: ${r.sort().join(", ")}`);
}
function _g(e) {
  if (e.type !== void 0 && e.type !== "legacy" && e.type !== "eip1559")
    throw new Error("unsupported EVM transaction type");
  const t = e.maxFeePerGas !== void 0 || e.maxPriorityFeePerGas !== void 0;
  if (e.type === "legacy" && t)
    throw new Error("EVM legacy transaction cannot carry maxFeePerGas or maxPriorityFeePerGas");
  if (e.type === "eip1559" || t) {
    if (e.gasPrice !== void 0) throw new Error("EVM EIP-1559 transaction cannot carry gasPrice");
    return "eip1559";
  }
  return "legacy";
}
function f_(e, t) {
  if (t.chainId === void 0 || t.chainId === null || t.chainId === "")
    throw new Error("EVM transaction chain id is required");
  if (String(t.chainId) !== String(e.chainId))
    throw new Error("EVM transaction chain id mismatch");
}
function h_(e) {
  const t = { ...e };
  for (const n of ["chainId", "nonce", "value", "gas", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas"]) {
    const r = e[n];
    if (r === void 0) continue;
    if (!(typeof r == "number" && Number.isSafeInteger(r) && r >= 0 || typeof r == "string" && /^(?:[0-9]+|0x[0-9a-fA-F]+)$/.test(r)))
      throw new Error(`EVM transaction ${n} must be an unsigned integer or integer string`);
    const i = BigInt(r);
    if (i >= 1n << 256n) throw new Error(`EVM transaction ${n} is out of range`);
    t[n] = i;
  }
  return t;
}
function p_(e) {
  if (e.length < 10) return null;
  const t = a_.get(e.slice(0, 10));
  if (!t) return null;
  let n;
  try {
    if (n = jS(t.item.inputs, `0x${e.slice(10)}`), pd(t.item.inputs, n).toLowerCase() !== `0x${e.slice(10)}`) return null;
  } catch {
    return null;
  }
  return {
    call: t.call,
    signature: c_(t.item),
    arguments: t.item.inputs.map((r, i) => ({
      name: r.name ?? `arg${i}`,
      type: r.type,
      value: Zo(n[i])
    }))
  };
}
function y_(e, t, n) {
  const r = _g(e), i = l_(e.data), o = (c) => (t[c] === void 0 ? 0n : t[c]).toString(), s = e.to === void 0 || e.to === null || e.to === "" ? null : String(e.to), a = s === null ? null : p_(i);
  return {
    chainId: o("chainId"),
    type: r,
    to: s,
    value: o("value"),
    nonce: o("nonce"),
    gas: o("gas"),
    ...r === "legacy" ? { gasPrice: o("gasPrice") } : { maxFeePerGas: o("maxFeePerGas"), maxPriorityFeePerGas: o("maxPriorityFeePerGas") },
    selector: i === "0x" ? null : i.slice(0, 10),
    unsignedTransactionHex: n,
    calldata: i,
    calldataBytes: (i.length - 2) / 2,
    decoded: a,
    blindSigning: i !== "0x" && a === null
  };
}
function w_(e) {
  const t = [J("evm.title", { chainId: e.chainId })];
  if (e.blindSigning && t.push(J("sign.blindNotice")), t.push(
    J("evm.type", { type: e.type }),
    e.to === null ? J("evm.contractCreation") : J("evm.to", { to: e.to }),
    J("evm.value", { value: e.value }),
    J("evm.nonce", { value: e.nonce }),
    J("evm.gas", { value: e.gas })
  ), e.gasPrice !== void 0 && t.push(J("evm.gasPrice", { value: e.gasPrice })), e.maxFeePerGas !== void 0 && t.push(J("evm.maxFeePerGas", { value: e.maxFeePerGas })), e.maxPriorityFeePerGas !== void 0 && t.push(J("evm.maxPriorityFeePerGas", { value: e.maxPriorityFeePerGas })), e.decoded) {
    t.push(J("evm.call", { call: `${s_[e.decoded.call]} ${e.decoded.signature}` }));
    for (const n of e.decoded.arguments) t.push(`  ${n.name} (${n.type}): ${n.value}`);
  }
  return t.push(
    e.selector === null ? J("evm.selectorNone") : J("evm.selector", { selector: e.selector }),
    e.calldata === "0x" ? J("evm.calldataNone") : J("evm.calldata", { bytes: e.calldataBytes, calldata: e.calldata })
  ), t.join(`
`);
}
function g_(e) {
  di(e);
  const t = Cr(e), n = {
    "evm-transaction": ["kind", "chainId", "unsignedTransaction", "digestAlgorithm", "digest"],
    "eip-712-typed-data": ["kind", "typedData", "digestAlgorithm", "digest"],
    "avault-agent-operation": ["kind", "canonicalPreimage", "digestAlgorithm", "digest"],
    "bitcoin-taproot-keypath-v1": ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"]
  }, r = Object.hasOwn(n, e.kind) ? n[e.kind] : void 0;
  if (!r) throw new Error("unsupported signing context");
  if (rc(t, new Set(r), "signing context"), typeof e.digest != "string" || !/^(?:0x)?[0-9a-fA-F]{64}$/.test(e.digest))
    throw new Error("signing context digest must be 32 bytes");
  switch (e.kind) {
    case "evm-transaction": {
      if (e.digestAlgorithm !== "keccak256") throw new Error("unsupported EVM digest algorithm");
      const i = Cr(e.unsignedTransaction);
      rc(i, d_, "EVM transaction"), f_(e, i), _g(i);
      const o = h_(i), s = BA(o), a = nc(e.digest, Nt(s)), c = y_(i, o, s);
      if (jA(s) !== c.type) throw new Error("EVM transaction type does not match its serialization");
      return {
        digest: a,
        challenge: ut(a),
        display: w_(c),
        evm: c
      };
    }
    case "eip-712-typed-data": {
      if (e.digestAlgorithm !== "eip712") throw new Error("unsupported EIP-712 digest algorithm");
      const i = Cr(e.typedData);
      rc(i, u_, "EIP-712 typed data");
      const o = Cr(i.domain);
      if (o.chainId === void 0 || o.verifyingContract === void 0)
        throw new Error("EIP-712 domain must include chainId and verifyingContract");
      const s = nc(e.digest, zA(i));
      return {
        digest: s,
        challenge: ut(s),
        display: [
          "EIP-712 typed data",
          `domain.chainId: ${Zo(o.chainId)}`,
          `domain.verifyingContract: ${Zo(o.verifyingContract)}`,
          `domain: ${qr(o)}`,
          `types: ${qr(Cr(i.types))}`,
          `primaryType: ${Zo(i.primaryType)}`,
          `message: ${qr(Cr(i.message))}`
        ].join(`
`),
        typedData: i
      };
    }
    case "avault-agent-operation": {
      if (e.digestAlgorithm !== "avault-operation-hash-v1") throw new Error("unsupported avault digest algorithm");
      if (typeof e.canonicalPreimage != "string" || !e.canonicalPreimage.startsWith(Af) || e.canonicalPreimage.slice(Af.length).trim().length === 0)
        throw new Error("canonical preimage must start with avault-agent-operation:v1\\n followed by operation text; recreate the request");
      const i = nc(e.digest, Vx(ue(new TextEncoder().encode(e.canonicalPreimage))));
      return {
        digest: i,
        challenge: ut(i),
        display: ["Avault agent operation", e.canonicalPreimage].join(`
`)
      };
    }
    case "bitcoin-taproot-keypath-v1": {
      const i = Ag(e, e.digest);
      return {
        digest: i.digest,
        challenge: ut(i.digest),
        display: i.display
      };
    }
    default:
      throw new Error("unsupported signing context");
  }
}
function Yo(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) return;
  const t = e.messageHex;
  if (typeof t == "string")
    try {
      bp(t);
    } catch {
      throw new _("invalid_payload", "signing messageHex exceeds the 1 MiB decoded limit");
    }
}
function ka(e) {
  if (Yo(e), di(e), !e || typeof e != "object" || Array.isArray(e)) throw new _("invalid_payload", "Solana signing context must be an object");
  const t = e;
  if (t.kind !== "solana-transaction-v1" || Object.keys(t).some((n) => n !== "kind" && n !== "messageHex") || typeof t.messageHex != "string" || t.messageHex.length === 0 || t.messageHex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(t.messageHex))
    throw new _("invalid_payload", "Solana context requires only kind and nonempty lowercase even messageHex; no digest alias");
  return { kind: "solana-transaction-v1", messageHex: t.messageHex };
}
function m_(e, t, n) {
  if (n.kind !== "keypair" || !n.publicKey) throw new Error("protected signing requires a keypair protected record");
  const r = ci(n.curve);
  if (e === Ui || t.kind === "solana-transaction-v1" || r === "ed25519") {
    if (e !== Ui || t.kind !== "solana-transaction-v1" || r !== "ed25519")
      throw new _("invalid_payload", "Ed25519 signing requires an Ed25519 record, ed25519 scheme and Solana context together");
    ka(t);
  }
}
const no = 1048576, b_ = ["bitcoin-p2pkh-v1", "bitcoin-p2wpkh-v1"];
function Ve(e, t, n, r) {
  if (typeof e != "string" || e.length === 0 || e.length > t * 2 || e.length % 2 || r !== void 0 && e.length !== r * 2) throw new _("invalid_payload", `${n} has an invalid hexadecimal size`);
  for (let i = 0; i < e.length; i++) {
    const o = e.charCodeAt(i);
    if (!(o >= 48 && o <= 57) && !(o >= 97 && o <= 102)) throw new _("invalid_payload", `${n} must be lowercase hexadecimal`);
  }
  return e;
}
const If = 524288, v_ = 4718592, x_ = 2162688;
function ve(e) {
  throw new _("invalid_payload", `Bitcoin resource limit: ${e}`);
}
function ce(e, t) {
  if (!(!e || typeof e != "object"))
    return Object.getOwnPropertyDescriptor(e, t)?.value;
}
function Fs(e, t) {
  return !!e && typeof e == "object" && Object.hasOwn(e, t);
}
function k_(e) {
  return b_.includes(ce(e, "kind")) || Fs(ce(e, "prevout"), "parentTransaction");
}
function Ig(e, t) {
  if (!e || typeof e != "object") return;
  const n = ce(e, "prevout");
  Fs(n, "parentTransaction") && (Ve(ce(n, "parentTransaction"), no, "parentTransaction"), t.set(n, "parentTransaction"));
  const r = ce(e, "unsignedTransaction");
  r !== void 0 && Ve(r, 137, "unsignedTransaction");
  const i = ce(e, "digest");
  i !== void 0 && Ve(i, 32, "digest", 32);
  const o = ce(n, "txid");
  o !== void 0 && Ve(o, 32, "parent txid", 32);
  const s = ce(n, "scriptPubKey");
  s !== void 0 && Ve(s, 25, "prevout script");
}
function xd(e, t, n, r = 2) {
  let i = 0, o = 0, s = 0, a = 0, c = 0;
  const l = /* @__PURE__ */ new Set();
  function d(h, y = !1) {
    i += h, y || (o += h), (i > n || o > If) && ve("canonical byte budget exceeded");
  }
  function u(h, y = !1) {
    h.length > (y ? no * 2 : If) && ve("string size exceeded"), d(2);
    for (let w = 0; w < h.length; w++) {
      const p = h.charCodeAt(w);
      let g;
      if (p === 34 || p === 92 || p === 8 || p === 9 || p === 10 || p === 12 || p === 13) g = 2;
      else if (p < 32) g = 6;
      else if (p < 128) g = 1;
      else if (p < 2048) g = 2;
      else if (p >= 55296 && p <= 56319) {
        const k = h.charCodeAt(++w);
        k >= 56320 && k <= 57343 || ve("unpaired Unicode surrogate"), g = 4;
      } else
        p >= 56320 && p <= 57343 && ve("unpaired Unicode surrogate"), g = 3;
      d(g, y);
    }
  }
  const f = [{ value: e, depth: 0 }];
  for (; f.length; ) {
    const h = f.pop();
    if ("exit" in h) {
      l.delete(h.exit);
      continue;
    }
    (++s > 4096 || h.depth > 16) && ve("node/depth budget exceeded");
    const y = h.value;
    if (y === null) {
      d(4);
      continue;
    }
    if (typeof y == "boolean") {
      d(y ? 4 : 5);
      continue;
    }
    if (typeof y == "number") {
      Number.isSafeInteger(y) || ve("numbers must be safe integers"), d(String(y).length);
      continue;
    }
    if (typeof y == "string") {
      h.proof && (++a > r || (c += y.length) > 4194304) && ve("proof-copy budget exceeded"), u(y, h.proof);
      continue;
    }
    (!y || typeof y != "object") && ve("only JSON values are permitted"), l.has(y) && ve("cyclic structure");
    const w = Array.isArray(y);
    !w && Object.getPrototypeOf(y) !== Object.prototype && Object.getPrototypeOf(y) !== null && ve("only plain JSON objects are permitted"), w && y.length > 2048 && ve("array size/shape exceeded"), l.add(y), f.push({ exit: y });
    const p = Reflect.ownKeys(y);
    if (w) {
      p.length !== y.length + 1 && ve("array size/shape exceeded"), d(2 + Math.max(0, y.length - 1));
      for (let g = y.length - 1; g >= 0; g--) {
        const k = Object.getOwnPropertyDescriptor(y, String(g));
        (!k || !("value" in k) || !k.enumerable) && ve("array accessors/holes are not JSON"), f.push({ value: k.value, depth: h.depth + 1 });
      }
    } else {
      p.length > 128 && ve("object field budget exceeded");
      const g = [];
      for (const k of p) {
        typeof k != "string" && ve("symbol fields are not JSON");
        const b = Object.getOwnPropertyDescriptor(y, k);
        (!("value" in b) || !b.enumerable) && ve("accessor/nonenumerable fields are not JSON"), g.push([k, b.value]);
      }
      d(2 + Math.max(0, g.length - 1));
      for (const [k, b] of g) {
        u(k), d(1), k === "parentTransaction" && t.get(y) !== k && ve("additional embedded proof copy");
        const O = t.get(y) === k;
        O && typeof b != "string" && ve("proof must be a string"), f.push({ value: b, depth: h.depth + 1, proof: O });
      }
    }
  }
}
function Bg(e) {
  const t = /* @__PURE__ */ new Map();
  Ig(e, t), xd(e, t, ce(e, "kind") === "bitcoin-p2wpkh-v1" ? 4096 : x_);
}
function E_(e) {
  const t = ce(ce(e, "context"), "release"), n = ce(e, "delivery"), r = ce(ce(n, "operation_context"), "release"), i = [ce(e, "signingContext"), ce(e, "signing_context"), ce(t, "signingContext"), ce(n, "signing_context"), ce(r, "signingContext")];
  if (!i.some(k_)) return;
  Fs(e, "signingContext") && Fs(e, "signing_context") && ve("multiple direct context aliases");
  const o = /* @__PURE__ */ new Map();
  for (const a of i) Ig(a, o);
  const s = ce(ce(e, "material"), "envelope");
  if (s && typeof s == "object") {
    const a = Reflect.ownKeys(s);
    a.length > 128 && ve("material envelope field budget exceeded");
    for (const c of a) {
      const l = Object.getOwnPropertyDescriptor(s, c)?.value;
      typeof l == "string" && l.length > 262144 && ve("material envelope string exceeded");
    }
  }
  xd(e, o, v_);
  for (const a of i) a !== void 0 && Bg(a);
}
const S_ = ["review.kind.bitcoinLegacyTransfer", "review.kind.bitcoinNativeTransfer"], A_ = 2621440;
function Tg(e) {
  const t = ce(e, "review");
  if (!S_.includes(ce(t, "kind"))) return;
  const n = ce(t, "details"), r = /* @__PURE__ */ new Map();
  if (Array.isArray(n) && n.length <= 16)
    for (let i = 0; i < n.length; i++) {
      const o = ce(ce(n, String(i)), "fields");
      if (!(!Array.isArray(o) || o.length > 512))
        for (let s = 0; s < o.length; s++) {
          const a = ce(o, String(s));
          ce(a, "label") === "review.field.parentTransaction" && (Ve(ce(a, "value"), no, "parentTransaction"), r.set(a, "value"));
        }
    }
  xd(e, r, A_, 1);
}
function qn(e) {
  if (!e || typeof e != "object") return !1;
  const t = e.kind;
  return t === "bitcoin-p2pkh-v1" || t === "bitcoin-p2wpkh-v1";
}
function ae(e) {
  throw new _("invalid_payload", e);
}
function Bf(e, t) {
  return (!e || typeof e != "object" || Array.isArray(e)) && ae(`${t} must be an object`), e;
}
function Tf(e, t, n) {
  (Object.keys(e).length !== t.length || t.some((r) => !Object.hasOwn(e, r))) && ae(`${n} has unsupported or missing fields`);
}
function Pf(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < t || e > n) && ae(`${r} is outside its integer range`), e;
}
function Dr(e) {
  return ue(ue(e));
}
function jc(e) {
  return G(Dr(e).reverse());
}
function kd(e) {
  Ve(e, 33, "selected public key", 33), /^(02|03)/.test(e) || ae("selected public key must be compressed secp256k1");
  const t = qe(e);
  try {
    fe.Point.fromBytes(t).assertValidity();
  } catch {
    ae("selected public key is not a secp256k1 point");
  }
  return t;
}
function No(e) {
  const t = Fc(kd(e));
  return {
    p2pkh: he(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    p2wpkh: he(new Uint8Array([0, 20]), t),
    p2tr: he(new Uint8Array([81, 32]), Ky(e))
  };
}
function __(e) {
  const t = Ve(e, no, "parentTransaction"), n = qe(t);
  let r = 0;
  function i(f) {
    (!Number.isSafeInteger(f) || f < 0 || f > n.length - r) && ae("funding transaction is truncated");
    const h = n.subarray(r, r + f);
    return r += f, h;
  }
  function o(f) {
    const h = i(f);
    let y = 0n;
    for (let w = 0; w < f; w++) y |= BigInt(h[w]) << BigInt(w * 8);
    return y;
  }
  function s(f, h) {
    const y = i(1)[0], w = y < 253 ? BigInt(y) : o(y === 253 ? 2 : y === 254 ? 4 : 8);
    return (y === 253 && w < 253n || y === 254 && w < 65536n || y === 255 && w < 4294967296n) && ae(`funding ${h} has a nonminimal CompactSize`), (w > BigInt(Number.MAX_SAFE_INTEGER) || w > BigInt(f)) && ae(`funding ${h} exceeds its bound`), Number(w);
  }
  i(4);
  const a = s(128, "input count");
  a === 0 && ae("funding transaction must have 1..128 inputs, without witness serialization");
  for (let f = 0; f < a; f++)
    i(36), i(s(1e4, "input script length")), i(4);
  const c = s(128, "output count");
  c === 0 && ae("funding transaction must have 1..128 outputs");
  const l = [];
  let d = 0n;
  for (let f = 0; f < c; f++) {
    const h = o(8);
    d += h, (h > ri || d > ri) && ae("funding output amount/total exceeds the Bitcoin money range");
    const y = i(s(1e4, "output script length"));
    l.push({ amountSats: h, scriptPubKey: y });
  }
  i(4), r !== n.length && ae("funding transaction has trailing bytes or witnesses");
  const u = Dr(n);
  return { raw: n, outputs: l, hash: u, txid: G(u.slice().reverse()) };
}
function I_(e, t) {
  Bg(e);
  const n = Bf(e, "Bitcoin signingContext"), r = Bf(n.prevout, "Bitcoin prevout");
  Object.hasOwn(r, "parentTransaction") && Ve(r.parentTransaction, no, "parentTransaction"), Tf(n, ["kind", "addressType", "network", "unsignedTransaction", "prevout", "sighashType", "digestAlgorithm", "digest"], "Bitcoin signingContext");
  const i = n.kind === "bitcoin-p2pkh-v1";
  return (!i && n.kind !== "bitcoin-p2wpkh-v1" || n.addressType !== (i ? "p2pkh" : "p2wpkh") || n.digestAlgorithm !== (i ? "bitcoin-legacy-sighash-all" : "bitcoin-bip143-sighash-all") || n.sighashType !== 1) && ae("Bitcoin context kind/type/algorithm/SIGHASH_ALL mismatch"), mg.includes(n.network) || ae("Bitcoin network is invalid"), Ve(n.unsignedTransaction, 137, "unsignedTransaction"), Ve(n.digest, 32, "digest", 32), n.digest !== t && ae("Bitcoin context digest differs from request digest"), Tf(r, i ? ["parentTransaction", "txid", "vout", "amountSats", "scriptPubKey"] : ["amountSats", "scriptPubKey"], "Bitcoin prevout"), Pf(r.amountSats, 1, Number(ri), "prevout amount"), Ve(r.scriptPubKey, i ? 25 : 22, "prevout script", i ? 25 : 22), i && (Ve(r.txid, 32, "parent txid", 32), Pf(r.vout, 0, 4294967295, "prevout vout")), n;
}
function B_(e) {
  return he(...e.outputs.map((t) => he(Ks(t.amountSats), new Uint8Array([t.scriptPubKey.length]), t.scriptPubKey)));
}
function T_(e, t, n, r) {
  t !== "p2pkh" && t !== "p2wpkh" && ae("unsupported ECDSA Bitcoin type");
  const i = he(e.prevoutHashLe, Ke(e.prevoutIndex)), o = he(new Uint8Array([r.length]), r), s = B_(e), a = t === "p2pkh" ? he(Ke(2), new Uint8Array([1]), i, o, Ke(e.sequence), new Uint8Array([e.outputs.length]), s, Ke(e.locktime), Ke(1)) : he(Ke(2), Dr(i), Dr(Ke(e.sequence)), i, o, Ks(n), Ke(e.sequence), Dr(s), Ke(e.locktime), Ke(1));
  return G(Dr(a));
}
function P_(e, t) {
  return e === "p2pkh" ? { min: t + 44, max: t + 107 } : { min: Math.ceil((4 * t + 47) / 4), max: Math.ceil((4 * t + 110) / 4) };
}
function Pg(e, t, n) {
  const r = 100n * e / BigInt(n), i = (100n * e + BigInt(t) - 1n) / BigInt(t), o = (s) => `${s / 100n}.${(s % 100n).toString().padStart(2, "0")}`;
  return { lo100: r, hi100: i, lower: o(r), upper: o(i) };
}
function $g(e, t, n) {
  return e <= 0n && ae("transaction fee must be positive"), e > 10000n * BigInt(n) && ae("transaction fee exceeds the conservative 10,000 sat/vB ceiling"), e > 100n * BigInt(n) || 100n * e > 2n * t;
}
function Og(e, t, n, r) {
  const i = I_(e, t), o = kg(i.unsignedTransaction), s = No(n), a = s[i.addressType];
  let c;
  if (i.kind === "bitcoin-p2pkh-v1") {
    const k = i.prevout;
    c = __(k.parentTransaction), (!Vr(o.prevoutHashLe, c.hash) || k.txid !== c.txid || k.vout !== o.prevoutIndex || k.vout >= c.outputs.length) && ae("Legacy parent txid/outpoint/vout mismatch");
    const b = c.outputs[k.vout];
    (b.amountSats <= 0n || b.amountSats !== BigInt(k.amountSats) || G(b.scriptPubKey) !== k.scriptPubKey) && ae("Legacy selected parent amount/script mismatch");
  }
  i.prevout.scriptPubKey !== G(a) && ae("prevout script does not match the selected key/type");
  const l = T_(o, i.addressType, BigInt(i.prevout.amountSats), s.p2pkh);
  l !== i.digest && ae("Bitcoin digest differs from independently computed SIGHASH_ALL");
  const d = r ?? { p2pkh: n, p2wpkh: n, p2tr: n };
  d[i.addressType] !== n && ae("selected BTC purpose point differs from selected key");
  const u = r ? [No(d.p2pkh).p2pkh, No(d.p2wpkh).p2wpkh, No(d.p2tr).p2tr] : Object.values(s), f = o.outputs.flatMap((k, b) => u.some((O) => Vr(O, k.scriptPubKey)) ? [b] : []);
  f.length > 1 && ae("transaction must not have more than one same-key/account return OUTPUT");
  const h = BigInt(i.prevout.amountSats), y = h - o.outputs.reduce((k, b) => k + b.amountSats, 0n), w = P_(i.addressType, o.raw.length), p = $g(y, h, w.min), g = f.length ? o.outputs[f[0]].amountSats : 0n;
  return {
    context: i,
    transaction: o,
    digest: l,
    publicKey: n,
    facts: {
      network: i.network,
      addressType: i.addressType,
      spendingAddress: Zi(i.network, a),
      inputTxid: G(o.prevoutHashLe.slice().reverse()),
      inputVout: o.prevoutIndex,
      inputAmountSats: h,
      prevoutScriptPubKey: i.prevout.scriptPubKey,
      scriptCode: G(s.p2pkh),
      serializedScriptCode: `19${G(s.p2pkh)}`,
      outputs: o.outputs.map((k, b) => ({ index: b, address: Zi(i.network, k.scriptPubKey), kind: k.kind, amountSats: k.amountSats, scriptPubKey: G(k.scriptPubKey), returned: f.includes(b) })),
      feeSats: y,
      vsize: w,
      rate: Pg(y, w.min, w.max),
      highFeeWarning: p,
      returnedSats: g,
      netLeavingSats: h - g,
      sequence: o.sequence,
      signalsRbf: o.sequence === 4294967293,
      locktime: o.locktime,
      unsignedTransaction: i.unsignedTransaction,
      unsignedTemplateHash: jc(o.raw),
      ...c ? { parent: { transaction: i.kind === "bitcoin-p2pkh-v1" ? i.prevout.parentTransaction : "", txid: c.txid, byteLength: c.raw.length } } : {}
    }
  };
}
function $_(e, t, n) {
  const r = Ve(e, 71, "DER signature");
  r.length < 16 && ae("DER signature must be 8..71 bytes"), Ve(t, 32, "digest", 32);
  const i = kd(n), o = qe(r);
  try {
    const s = fe.Signature.fromBytes(o, "der");
    (s.r <= 0n || s.s <= 0n || s.hasHighS() || !Vr(s.toBytes("der"), o) || !fe.verify(o, qe(t), i, { prehash: !1, lowS: !0, format: "der" })) && ae("invalid strict low-S DER signature for selected point/digest");
  } catch {
    ae("invalid strict low-S DER signature for selected point/digest");
  }
  return o;
}
function O_(e, t, n, r, i) {
  const o = Og(e, t, n, i), s = $_(r, o.digest, n), a = o.transaction, c = kd(n), l = he(s, new Uint8Array([1]));
  let d, u;
  if (o.context.addressType === "p2pkh") {
    const y = he(new Uint8Array([l.length]), l, new Uint8Array([33]), c);
    d = he(a.raw.subarray(0, 41), new Uint8Array([y.length]), y, a.raw.subarray(42)), u = 4 * d.length, d.length !== a.raw.length + s.length + 36 && ae("Legacy signed size closure failed");
  } else {
    const y = he(new Uint8Array([2, l.length]), l, new Uint8Array([33]), c);
    d = he(a.raw.subarray(0, 4), new Uint8Array([0, 1]), a.raw.subarray(4, a.raw.length - 4), y, a.raw.subarray(a.raw.length - 4)), u = 3 * a.raw.length + d.length, u !== 4 * a.raw.length + s.length + 39 && ae("Native signed weight closure failed");
  }
  const f = Math.ceil(u / 4);
  (f < o.facts.vsize.min || f > o.facts.vsize.max) && ae("signed vsize is outside its conservative range");
  const h = $g(o.facts.feeSats, o.facts.inputAmountSats, f);
  return {
    signature: G(s),
    signedTransaction: G(d),
    weight: u,
    vsize: f,
    txid: jc(o.context.addressType === "p2wpkh" ? a.raw : d),
    wtxid: jc(d),
    feeSats: o.facts.feeSats,
    rate: Pg(o.facts.feeSats, f, f),
    highFeeWarning: h || o.facts.highFeeWarning
  };
}
const Xo = Object.freeze({
  // The source now includes authenticated fresh-UV/key proof and one-sign local
  // closure for BOTH new kinds. Release/pin registry enablement remains separate.
  ordinaryInputTypes: Object.freeze(["p2pkh", "p2wpkh", "p2tr"]),
  hdInputTypes: Object.freeze(["p2pkh", "p2wpkh", "p2tr"]),
  ecdsaReturnTypes: Object.freeze(["p2pkh", "p2wpkh", "p2tr"])
});
function C_(e = Xo) {
  return ["p2pkh", "p2wpkh", "p2tr"].every((t) => e.ordinaryInputTypes.includes(t));
}
const Cg = "11111111111111111111111111111111", Rg = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", R_ = "SysvarRecentB1ockHashes11111111111111111111", L_ = 2, H_ = 4, N_ = 3, U_ = 12;
function cn(e) {
  return { decoded: null, undecodedReason: e };
}
function Lg(e, t) {
  let n = 0n;
  for (let r = 7; r >= 0; r -= 1) n = n << 8n | BigInt(e[t + r]);
  return n.toString();
}
function Ed(e) {
  const t = [];
  for (const n of e) {
    if (n.source !== "static") return null;
    t.push(n.address);
  }
  return t;
}
function D_(e, t) {
  const n = t.length >= 4 ? (t[0] | t[1] << 8 | t[2] << 16 | t[3] << 24) >>> 0 : -1;
  return n === L_ && t.length === 12 ? z_(e, t) : n === H_ && t.length === 4 ? M_(e) : cn("unsupported_instruction");
}
function z_(e, t) {
  if (e.length !== 2) return cn("unsupported_instruction");
  const n = Ed(e);
  if (!n) return cn("unresolved_account");
  const [r, i] = n;
  return { decoded: { kind: "system-transfer", source: r, destination: i, lamports: Lg(t, 4) }, undecodedReason: null };
}
function M_(e) {
  if (e.length !== 3) return cn("unsupported_instruction");
  const t = Ed(e);
  if (!t) return cn("unresolved_account");
  const [n, r, i] = t;
  return r !== R_ ? cn("unsupported_instruction") : { decoded: { kind: "system-advance-nonce", nonceAccount: n, recentBlockhashesSysvar: r, nonceAuthority: i }, undecodedReason: null };
}
function K_(e, t) {
  const n = t.length === 9 && t[0] === N_ && e.length === 3, r = t.length === 10 && t[0] === U_ && e.length === 4;
  if (!n && !r) return cn("unsupported_instruction");
  const i = Ed(e);
  if (!i) return cn("unresolved_account");
  const o = Lg(t, 1);
  if (n) {
    const [d, u, f] = i;
    return { decoded: { kind: "spl-transfer", sourceTokenAccount: d, destinationTokenAccount: u, authority: f, amount: o }, undecodedReason: null };
  }
  const [s, a, c, l] = i;
  return {
    decoded: { kind: "spl-transfer-checked", sourceTokenAccount: s, mint: a, destinationTokenAccount: c, authority: l, amount: o, decimals: t[9] },
    undecodedReason: null
  };
}
function F_(e, t, n) {
  return e === Cg ? D_(t, n) : e === Rg ? K_(t, n) : cn("unsupported_program");
}
const j_ = 32, ic = 512, vi = 256, $f = 8, Hg = 3, Ng = /* @__PURE__ */ new Map([
  ["1", "Ethereum"],
  ["10", "OP Mainnet"],
  ["56", "BNB Smart Chain"],
  ["137", "Polygon"],
  ["8453", "Base"],
  ["42161", "Arbitrum One"],
  ["11155111", "Sepolia"],
  ["11155420", "OP Sepolia"],
  ["84532", "Base Sepolia"],
  ["421614", "Arbitrum Sepolia"]
]), Ug = /* @__PURE__ */ new Set(["1", "10", "8453", "42161", "11155111", "11155420", "84532", "421614"]);
function Dg(e) {
  return Ng.has(e) ? [] : ["review.note.unknownChain"];
}
function zg(e) {
  const t = Ng.get(e);
  return t === void 0 ? e : `${e} · ${t}`;
}
function js(e) {
  return typeof e == "object" && e !== null && !Array.isArray(e);
}
function Mg(e) {
  if (typeof e == "bigint") return e;
  if (typeof e != "string" && typeof e != "number" && typeof e != "boolean" && !Array.isArray(e)) return null;
  try {
    return BigInt(e);
  } catch {
    return null;
  }
}
function zn(e, t) {
  if (/^u?int\d*$/.test(e)) {
    const n = Mg(t);
    return n === null ? JSON.stringify(t) ?? String(t) : n.toString();
  }
  return typeof t == "string" ? t : JSON.stringify(t) ?? String(t);
}
class Of extends Error {
}
function Vs(e, t, n, r, i, o) {
  if (i > j_) {
    o.omitted += 1;
    return;
  }
  const s = e[t];
  if (s !== void 0) {
    if (!Object.hasOwn(e, t) || !Array.isArray(s) || n === null || n === void 0) throw new Of(r);
    const a = Object(n), c = new Set(s.map((l) => l.name));
    for (const l of s) Vs(e, l.type, a[l.name], r ? `${r}.${l.name}` : l.name, i + 1, o);
    if (typeof n == "object")
      for (const l of Object.keys(n)) c.has(l) || o.uncovered.push(r ? `${r}.${l}` : l);
    return;
  }
  if (t !== "bytes" && t !== "string" && t.endsWith("]")) {
    if (!Array.isArray(n)) throw new Of(r);
    const a = t.slice(0, t.lastIndexOf("["));
    n.length === 0 && o.covered.push({ path: r, type: t, value: "[]" }), n.forEach((c, l) => Vs(e, a, c, `${r}[${l}]`, i + 1, o));
    return;
  }
  o.covered.push({ path: r, type: t, value: zn(t, n) });
}
function V_(e, t) {
  const n = /* @__PURE__ */ new Set(), r = (i) => {
    const o = /^\w*/.exec(i)[0];
    if (!(n.has(o) || !e[o])) {
      n.add(o);
      for (const s of e[o]) r(s.type);
    }
  };
  return r(t), n;
}
const Kg = [["token", "address"], ["amount", "uint160"], ["expiration", "uint48"], ["nonce", "uint48"]], Fg = [["token", "address"], ["amount", "uint256"]], q_ = { primaryType: "Permit", types: { Permit: [["owner", "address"], ["spender", "address"], ["value", "uint256"], ["nonce", "uint256"], ["deadline", "uint256"]] } }, W_ = { primaryType: "PermitSingle", types: { PermitSingle: [["details", "PermitDetails"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Kg } }, G_ = { primaryType: "PermitBatch", types: { PermitBatch: [["details", "PermitDetails[]"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Kg } }, Z_ = {
  primaryType: "PermitTransferFrom",
  types: { PermitTransferFrom: [["permitted", "TokenPermissions"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: Fg }
}, Y_ = {
  primaryType: "PermitBatchTransferFrom",
  types: { PermitBatchTransferFrom: [["permitted", "TokenPermissions[]"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: Fg }
};
function xi(e, t, n) {
  if (t !== n.primaryType) return !1;
  const r = V_(e, t), i = Object.keys(n.types);
  return r.size !== i.length || !i.every((o) => r.has(o)) ? !1 : i.every((o) => {
    const s = e[o], a = n.types[o];
    return Array.isArray(s) && s.length === a.length && s.every((c, l) => c.name === a[l][0] && c.type === a[l][1]);
  });
}
const Cf = (1n << 256n) - 1n, X_ = (1n << 160n) - 1n;
function oc(e, t, n, r, i) {
  const o = Mg(t), s = o === n;
  return {
    field: {
      label: e,
      ...i === void 0 ? {} : { index: i },
      value: o === null ? zn("string", t) : o.toString(),
      ...s ? { shownAs: r, notes: ["review.note.exactInDetails"] } : { notes: ["review.note.rawTokenUnits"] }
    },
    isMax: s
  };
}
function Zn(e) {
  return typeof e == "string" ? e : zn("string", e);
}
function sc(e, t, n) {
  return { label: e, value: zn("uint256", t), time: "unix-seconds", notes: [n] };
}
function Rf(e) {
  const t = e.filter((o) => o.isMax), n = e.filter((o) => !o.isMax), r = n.slice(0, Math.max(0, Hg - t.length)), i = n.length - r.length;
  return {
    rows: [
      ...[...t, ...r].flatMap((o) => o.rows()),
      ...i > 0 ? [{ label: "review.field.moreTokens", value: "", notes: [{ key: "review.note.moreEntriesInDetails", count: i }] }] : []
    ],
    maxima: t.length
  };
}
function Lf(e, t, n, r) {
  return t.maxima <= Hg ? [e, ...t.rows, r] : [
    e,
    { label: "review.field.maxEntries", value: String(t.maxima), notes: [{ key: "review.note.ofEntries", count: n }] },
    r,
    ...t.rows
  ];
}
function J_(e, t, n) {
  const r = ["review.warning.tokenAuthorization"];
  if (xi(e, t, q_)) {
    const s = oc("review.field.permitAmount", n.value, Cf, "review.value.unlimited");
    return {
      kind: "review.kind.erc2612Permit",
      warnings: s.isMax ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: [
        { label: "review.field.spender", value: Zn(n.spender), elide: !0 },
        s.field,
        sc("review.field.permitDeadline", n.deadline, "review.note.permitDeadline")
      ],
      identity: [
        { label: "review.field.permitOwner", value: Zn(n.owner), elide: !0 },
        { label: "review.field.permitNonce", value: zn("uint256", n.nonce) }
      ]
    };
  }
  const i = xi(e, t, W_);
  if (i || xi(e, t, G_)) {
    const s = i ? [n.details] : Array.isArray(n.details) ? n.details : [], a = Rf(s.map((c, l) => {
      const d = js(c) ? c : {}, u = oc("review.field.permitTokenAmount", d.amount, X_, "review.value.unlimited", l + 1);
      return {
        isMax: u.isMax,
        rows: () => [
          u.field,
          { label: "review.field.permitToken", index: l + 1, value: Zn(d.token), elide: !0 },
          { label: "review.field.permitExpiration", index: l + 1, value: zn("uint48", d.expiration), notes: ["review.note.permit2Expiration"] }
        ]
      };
    }));
    return {
      kind: "review.kind.permit2Allowance",
      warnings: a.maxima > 0 ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: Lf(
        { label: "review.field.spender", value: Zn(n.spender), elide: !0 },
        a,
        s.length,
        sc("review.field.signatureDeadline", n.sigDeadline, "review.note.signatureDeadline")
      ),
      identity: []
    };
  }
  const o = xi(e, t, Z_);
  if (o || xi(e, t, Y_)) {
    const s = o ? [n.permitted] : Array.isArray(n.permitted) ? n.permitted : [], a = Rf(s.map((c, l) => {
      const d = js(c) ? c : {}, u = oc("review.field.permitTokenAmount", d.amount, Cf, "review.value.maximumOneTime", l + 1);
      return {
        isMax: u.isMax,
        rows: () => [u.field, { label: "review.field.permitToken", index: l + 1, value: Zn(d.token), elide: !0 }]
      };
    }));
    return {
      kind: "review.kind.permit2Transfer",
      warnings: a.maxima > 0 ? ["review.warning.oneTimeTransfer", "review.warning.maximumOneTimeTransfer"] : ["review.warning.oneTimeTransfer"],
      summary: Lf(
        // The Permit2 contract signs msg.sender as the spender; this is the value in the message.
        { label: "review.field.spender", value: Zn(n.spender), elide: !0 },
        a,
        s.length,
        sc("review.field.permitDeadline", n.deadline, "review.note.signatureDeadline")
      ),
      identity: [{ label: "review.field.permitNonce", value: zn("uint256", n.nonce) }]
    };
  }
  return null;
}
function jg(e) {
  return e.length <= ic ? { name: e } : { name: `${e.slice(0, ic / 2)} … ${e.slice(-ic / 4)}`, notes: ["review.note.pathShortened"] };
}
function ac(e) {
  const { name: t, notes: n } = jg(`${e.path} (${e.type})`);
  return { label: "review.field.typedField", name: t, value: e.value, ...n ? { notes: n } : {} };
}
function Q_(e, t) {
  const n = e, r = js(n.domain) ? n.domain : {}, i = n.types, o = {
    EIP712Domain: hg({ domain: r }),
    ...i
  }, s = da(t.publicKey).eth;
  if (!s) throw new Error("EIP-712 review requires a secp256k1 signing key");
  const a = { covered: [], uncovered: [], omitted: 0 };
  Vs(o, "EIP712Domain", r, "domain", 0, a);
  const c = a.covered, l = a.uncovered, d = new Set(o.EIP712Domain.map((H) => H.name)), u = (H, L) => {
    if (!Object.hasOwn(r, H)) return [];
    const T = d.has(H), D = o.EIP712Domain.find((E) => E.name === H), z = T && D ? zn(D.type, r[H]) : Zn(r[H]);
    return [{
      label: L,
      value: H === "chainId" ? zg(z) : z,
      notes: [...H === "chainId" ? Dg(z) : [], ...T ? [] : ["review.note.notInTypedHash"]]
    }];
  }, f = n.primaryType !== "EIP712Domain", h = js(n.message) ? n.message : {}, y = { covered: [], uncovered: l, omitted: 0 };
  f ? Vs(o, n.primaryType, n.message, "", 0, y) : n.message !== void 0 && l.push("message");
  const w = y.covered, p = a.omitted + y.omitted;
  let g = null;
  try {
    g = f ? J_(o, n.primaryType, h) : null;
  } catch {
    g = null;
  }
  const k = w.slice(0, $f).map((H) => H.type === "address" ? { ...ac(H), elide: !0 } : ac(H)), b = Math.max(0, w.length - $f) + p;
  b > 0 && k.push({ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: b }] });
  const O = (H, L = 0) => {
    const T = Math.max(0, H.length - vi) + L;
    return [
      ...H.slice(0, vi).map(ac),
      ...T > 0 ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: T }] }] : []
    ];
  }, B = JSON.stringify(n, null, 2), R = [
    { title: "review.group.typedDomain", fields: O(c, a.omitted) },
    ...f ? [{ title: "review.group.typedMessage", fields: O(w, y.omitted) }] : [],
    ...l.length > 0 ? [{
      title: "review.group.notInTypedHash",
      fields: [
        ...l.slice(0, vi).map((H) => {
          const L = jg(H);
          return { label: "review.field.typedField", name: L.name, value: "", notes: ["review.note.notInTypedHash", ...L.notes ?? []] };
        }),
        ...l.length > vi ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: l.length - vi }] }] : []
      ]
    }] : [],
    {
      title: "review.group.transaction",
      fields: [
        { label: "review.field.publicKey", value: t.publicKey.toLowerCase() },
        { label: "review.field.scheme", value: t.scheme },
        { label: "review.field.primaryType", value: n.primaryType },
        { label: "review.field.digest", value: `0x${t.digest}` }
      ]
    },
    {
      title: "review.group.exact",
      fields: [
        { label: "review.field.typedTypes", value: JSON.stringify(i, null, 2), raw: !0 },
        { label: "review.field.typedData", value: B, notes: [{ key: "review.note.bytes", count: new TextEncoder().encode(B).length }], raw: !0 }
      ]
    }
  ];
  return {
    kind: g?.kind ?? "review.kind.eip712",
    signer: { label: "review.field.signingAddress", value: s, elide: !0 },
    // What the signature grants first, then where it applies, then who; the signer last.
    summary: [
      ...g ? g.summary : k,
      ...l.length > 0 ? [{ label: "review.field.notInTypedHash", value: "", notes: [{ key: "review.note.fieldsOutsideHash", count: l.length }] }] : [],
      ...u("chainId", "review.field.chainId"),
      ...u("name", "review.field.domainName"),
      ...u("verifyingContract", "review.field.verifyingContract").map((H) => ({ ...H, elide: !0 })),
      { label: "review.field.primaryType", value: n.primaryType, ...f ? {} : { notes: ["review.note.messageNotHashed"] } },
      ...g ? g.identity : []
    ],
    // Every hashed value is shown; nothing is blind.
    undecoded: !1,
    ...g ? { warnings: g.warnings } : {},
    details: R
  };
}
const Hf = 3, Nf = 3, e3 = 3;
function Vg(e) {
  return e === Cg ? ["review.note.systemProgram"] : e === Rg ? ["review.note.tokenProgram"] : [];
}
function ro(e) {
  return e.length / 2;
}
function qg(e) {
  switch (e.kind) {
    case "system-transfer":
      return [
        { label: "review.field.from", value: e.source },
        { label: "review.field.to", value: e.destination },
        { label: "review.field.amount", value: e.lamports, native: "sol" }
      ];
    case "spl-transfer":
      return [
        { label: "review.field.fromTokenAccount", value: e.sourceTokenAccount },
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount },
        { label: "review.field.amount", value: e.amount, notes: ["review.note.baseUnits"] }
      ];
    case "spl-transfer-checked":
      return [
        { label: "review.field.fromTokenAccount", value: e.sourceTokenAccount },
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount },
        { label: "review.field.mint", value: e.mint },
        // The instruction encodes its decimals: the exact decimal amount, then the integer and the count.
        { label: "review.field.amount", value: e.amount, decimals: e.decimals }
      ];
  }
}
function t3(e) {
  return e.kind === "spl-transfer-checked" ? "TransferChecked" : "Transfer";
}
function Uf(e) {
  return {
    label: "review.field.undecodedInstruction",
    value: e.programId,
    notes: [{ key: "review.note.dataBytes", count: ro(e.dataHex) }, "review.note.notDecoded"]
  };
}
function n3(e, t) {
  const n = (r) => ({ label: r, index: t, value: "", heading: !0 });
  switch (e.kind) {
    case "system-transfer":
      return [
        n("review.field.transferSol"),
        { label: "review.field.amount", value: e.lamports, native: "sol" },
        { label: "review.field.to", value: e.destination, elide: !0 }
      ];
    case "spl-transfer":
      return [
        n("review.field.transferTokenNoMint"),
        { label: "review.field.amount", value: e.amount, notes: ["review.note.baseUnits"] },
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount, elide: !0 }
      ];
    case "spl-transfer-checked":
      return [
        n("review.field.transferToken"),
        { label: "review.field.amount", value: e.amount, decimals: e.decimals },
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount, elide: !0 },
        { label: "review.field.mint", value: e.mint, elide: !0 }
      ];
  }
}
function Df(e) {
  return e === 0 ? [] : [{ label: "review.field.durableNonce", value: "", ...e > 1 ? { notes: [{ key: "review.note.nonceAdvances", count: e }] } : {} }];
}
function r3(e) {
  const t = qg(e).map((r) => r.label === "review.field.amount" ? r : { ...r, elide: !0 }), n = ["review.field.amount", "review.field.to", "review.field.toTokenAccount", "review.field.mint", "review.field.from", "review.field.fromTokenAccount"];
  return [...t].sort((r, i) => n.indexOf(r.label) - n.indexOf(i.label));
}
const qs = (e) => e.kind !== "system-advance-nonce";
function i3(e) {
  if (e.instructions.length === 1) {
    const [s] = e.instructions;
    return s.decoded ? qs(s.decoded) ? r3(s.decoded) : Df(1) : [{ ...Uf(s), elide: !0 }];
  }
  let t = 0, n = 0, r = 0;
  const i = e.instructions.flatMap((s) => {
    const a = s.decoded;
    return a && qs(a) ? (t += 1, t <= Hf ? n3(a, t) : []) : a ? (n += 1, []) : (r += 1, r <= Nf ? [{ ...Uf(s), elide: !0 }] : []);
  }), o = (s, a, c, l) => c > l ? [{ label: s, value: "", notes: [{ key: a, count: c - l }] }] : [];
  return [
    {
      label: "review.field.instructions",
      value: String(e.instructions.length),
      notes: [
        ...t > 0 ? [{ key: "review.note.transfersDecoded", count: t }] : [],
        ...r > 0 ? [{ key: "review.note.instructionsNotDecoded", count: r }] : []
      ]
    },
    ...i,
    ...o("review.field.moreTransfers", "review.note.moreInDetails", t, Hf),
    ...o("review.field.moreUndecoded", "review.note.moreInstructionsInDetails", r, Nf),
    ...Df(n)
  ];
}
function o3(e) {
  return qs(e) ? [
    { label: "review.field.instruction", value: t3(e) },
    ...qg(e),
    ...e.kind === "system-transfer" ? [] : [{ label: "review.field.authority", value: e.authority }]
  ] : [
    { label: "review.field.instruction", value: "AdvanceNonceAccount" },
    { label: "review.field.nonceAccount", value: e.nonceAccount },
    { label: "review.field.recentBlockhashesSysvar", value: e.recentBlockhashesSysvar },
    { label: "review.field.nonceAuthority", value: e.nonceAuthority }
  ];
}
function s3(e) {
  return e.map((t) => t.index).join(", ");
}
function a3(e) {
  const t = e.decoded, n = {
    label: "review.field.data",
    value: e.dataHex,
    notes: [{ key: "review.note.bytes", count: ro(e.dataHex) }, ...t ? [] : ["review.note.notDecoded"]]
  };
  return {
    title: "review.group.instruction",
    index: e.index + 1,
    fields: [
      { label: "review.field.program", value: e.programId, notes: Vg(e.programId) },
      ...t ? o3(t) : [],
      { label: "review.field.accountIndexes", value: s3(e.accounts) },
      n
    ]
  };
}
function c3(e) {
  const t = e.instructions.length === 1 ? e.instructions[0].decoded : null;
  return {
    kind: !t || !qs(t) ? "review.kind.solanaTransaction" : t.kind === "system-transfer" ? "review.kind.solanaSystemTransfer" : "review.kind.solanaTokenTransfer",
    signer: { label: "review.field.signingKey", value: e.expectedSigner.address, elide: !0 },
    summary: i3(e),
    // Any undecoded instruction, and any account loaded through an address table.
    undecoded: e.hasUndecodedContent,
    details: [
      {
        title: "review.group.transaction",
        fields: [
          { label: "review.field.messageFormat", value: e.version },
          { label: "review.field.scheme", value: "ed25519" },
          { label: "review.field.feePayer", value: e.feePayer },
          { label: "review.field.blockhashOrNonce", value: e.recentBlockhash }
        ]
      },
      {
        title: "review.group.accounts",
        fields: e.staticAccountKeys.map((n) => ({
          label: "review.field.account",
          index: n.index,
          value: n.address,
          notes: [
            ...n.signer ? ["review.note.signer"] : [],
            n.writable ? "review.note.writable" : "review.note.readOnly",
            ...Vg(n.address)
          ]
        }))
      },
      ...e.instructions.map(a3),
      {
        title: "review.group.exact",
        fields: [{
          label: "review.field.message",
          value: e.messageHex,
          notes: [{ key: "review.note.bytes", count: e.messageLength }],
          raw: !0
        }]
      }
    ]
  };
}
function Vc(e) {
  return Ug.has(e.chainId) ? { label: "review.field.value", value: e.value, native: "eth" } : { label: "review.field.value", value: e.value, notes: ["review.note.wei"] };
}
function Wg(e) {
  return e.to === null ? { label: "review.field.contract", value: "", notes: ["review.note.contractCreation"] } : { label: "review.field.contract", value: e.to };
}
function Gg(e) {
  return (e.decoded?.arguments ?? []).map((t, n) => ({
    label: "review.field.argument",
    index: n + 1,
    name: `${t.name} (${t.type})`,
    value: t.value
  }));
}
function l3(e) {
  if (e.decoded) {
    const n = { label: "review.field.call", value: e.decoded.signature }, r = Gg(e).map((i) => i.name?.endsWith(" (address)") ? { ...i, elide: !0 } : i);
    return r.length <= e3 ? [n, ...r] : [{ ...n, notes: [{ key: "review.note.argumentsInDetails", count: r.length }] }];
  }
  if (e.calldata === "0x") return [];
  const t = [{ key: "review.note.dataBytes", count: e.calldataBytes }, "review.note.notDecoded"];
  return e.to === null || e.selector === null ? [{ label: "review.field.initCode", value: "", notes: t }] : [{ label: "review.field.selector", value: e.selector, notes: t }];
}
const zf = ((1n << 256n) - 1n).toString();
function d3(e) {
  const t = e.decoded;
  if (!t || e.to === null) return null;
  const [n, r] = t.arguments;
  if (t.call === "transfer")
    return {
      kind: "review.kind.erc20Transfer",
      lead: [
        { label: "review.field.transferTo", value: n.value, elide: !0 },
        { label: "review.field.amount", value: r.value, notes: ["review.note.rawTokenUnits"] }
      ]
    };
  if (t.call === "approve") {
    const i = r.value === zf;
    return {
      kind: "review.kind.tokenApprove",
      warnings: i ? ["review.warning.tokenAuthorization", "review.warning.maximumApproval"] : ["review.warning.tokenAuthorization"],
      lead: [
        { label: "review.field.spender", value: n.value, elide: !0 },
        i ? { label: "review.field.approvalValue", value: r.value, shownAs: "review.value.maximum", notes: ["review.note.exactInDetails"] } : { label: "review.field.approvalValue", value: r.value }
      ]
    };
  }
  if (t.call === "increaseAllowance") {
    const i = r.value === zf;
    return {
      kind: "review.kind.tokenIncreaseAllowance",
      warnings: ["review.warning.tokenAuthorization"],
      lead: [
        { label: "review.field.spender", value: n.value, elide: !0 },
        i ? { label: "review.field.allowanceIncrease", value: r.value, shownAs: "review.value.maximumIncrease", notes: ["review.note.exactInDetails", "review.note.addedToAllowance"] } : { label: "review.field.allowanceIncrease", value: r.value, notes: ["review.note.rawTokenUnits", "review.note.addedToAllowance"] }
      ]
    };
  }
  return null;
}
function u3(e) {
  const t = { label: "review.field.chainId", value: zg(e.chainId), notes: Dg(e.chainId) };
  if (e.to !== null && e.calldata === "0x")
    return { kind: "review.kind.evmTransaction", summary: [Ug.has(e.chainId) ? { label: "review.field.nativeTransfer", value: e.value, native: "eth", unit: "symbol", to: e.to } : { label: "review.field.nativeTransfer", value: e.value, notes: ["review.note.wei"], to: e.to }, t] };
  const n = d3(e);
  return n ? {
    kind: n.kind,
    ...n.warnings ? { warnings: n.warnings } : {},
    // The native value the transaction carries, as encoded, never assumed to be zero.
    summary: [...n.lead, { label: "review.field.tokenContract", value: e.to, elide: !0 }, Vc(e), t]
  } : { kind: "review.kind.evmTransaction", summary: [{ ...Wg(e), elide: !0 }, Vc(e), ...l3(e), t] };
}
function f3(e, t) {
  const n = da(t.publicKey).eth;
  if (!n) throw new Error("EVM review requires a secp256k1 signing key");
  const r = (a, c) => c === void 0 ? [] : [{ label: a, value: c, notes: ["review.note.wei"] }], { kind: i, summary: o, warnings: s } = u3(e);
  return {
    kind: i,
    signer: { label: "review.field.signingAddress", value: n, elide: !0 },
    summary: o,
    undecoded: e.blindSigning,
    ...s ? { warnings: s } : {},
    details: [
      {
        title: "review.group.transaction",
        fields: [
          { label: "review.field.publicKey", value: t.publicKey.toLowerCase() },
          { label: "review.field.scheme", value: t.scheme },
          { label: "review.field.type", value: e.type },
          { label: "review.field.chainId", value: e.chainId },
          Wg(e),
          Vc(e),
          { label: "review.field.nonce", value: e.nonce },
          { label: "review.field.gas", value: e.gas },
          ...r("review.field.gasPrice", e.gasPrice),
          ...r("review.field.maxFeePerGas", e.maxFeePerGas),
          ...r("review.field.maxPriorityFeePerGas", e.maxPriorityFeePerGas)
        ]
      },
      ...e.decoded ? [{
        title: "review.group.call",
        fields: [
          { label: "review.field.call", value: e.decoded.signature },
          ...e.selector === null ? [] : [{ label: "review.field.selector", value: e.selector }],
          ...Gg(e)
        ]
      }] : [],
      {
        title: "review.group.exact",
        fields: [
          {
            label: "review.field.calldata",
            value: e.calldata,
            notes: [{ key: "review.note.bytes", count: e.calldataBytes }, ...e.blindSigning ? ["review.note.notDecoded"] : []],
            raw: e.calldata !== "0x"
          },
          {
            label: "review.field.unsignedTransaction",
            value: e.unsignedTransactionHex,
            notes: [{ key: "review.note.bytes", count: ro(e.unsignedTransactionHex) - 1 }],
            raw: !0
          },
          { label: "review.field.digest", value: `0x${t.digest}` }
        ]
      }
    ]
  };
}
function Zg(e, t) {
  return [...e.filter((n) => !t(n)), ...e.filter(t)];
}
function h3(e, t) {
  const n = (s) => s.toString(), r = { label: "review.field.network", value: e.network, notes: ["review.note.requesterNetwork"] }, i = Zg(e.outputs, (s) => s.sameKey).map((s) => s.sameKey ? { label: "review.field.returnToKey", value: n(s.amountSats), native: "btc", unit: "symbol" } : { label: "review.field.send", value: n(s.amountSats), native: "btc", unit: "symbol", to: s.address }), o = e.outputs.filter((s) => !s.sameKey).map((s) => ({ label: "review.field.recipientAddress", value: s.address }));
  return {
    kind: "review.kind.bitcoinTransfer",
    signer: { label: "review.field.signingAddress", value: e.spendingAddress, elide: !0 },
    summary: [
      ...i,
      { label: "review.field.fee", value: n(e.feeSats), native: "btc", unit: "base", secondary: e.feeRate },
      { label: "review.field.netLeaving", value: n(e.netLeavingSats), native: "btc", unit: "symbol" },
      ...o,
      r
    ],
    // Every byte is parsed by the bounded verifier; nothing here is blind.
    undecoded: !1,
    ...e.highFeeWarning ? { warnings: ["review.warning.highFee"] } : {},
    details: [
      {
        title: "review.group.transaction",
        fields: [
          { label: "review.field.scheme", value: t.scheme },
          ...e.internalKeyXOnly ? [{ label: "review.field.internalKey", value: e.internalKeyXOnly }] : [],
          { label: "review.field.outputKey", value: e.outputKeyXOnly },
          { label: "review.field.network", value: e.network, notes: ["review.note.networkNotChainProof"] },
          { label: "review.field.input", value: `${e.inputTxid}:${e.inputVout}` },
          { label: "review.field.inputAmount", value: n(e.inputAmountSats), native: "btc" },
          ...e.outputs.flatMap((s, a) => [
            { label: "review.field.outputIndexed", index: a + 1, value: s.address, notes: [s.sameKey ? "review.note.sameKeyReturn" : "review.note.recipient"] },
            { label: "review.field.outputAmountIndexed", index: a + 1, value: n(s.amountSats), native: "btc" }
          ]),
          { label: "review.field.fee", value: n(e.feeSats), native: "btc" },
          { label: "review.field.feeRate", value: e.feeRate },
          { label: "review.field.signedVsize", value: String(e.signedVsize), notes: ["review.note.vbytes"] },
          { label: "review.field.netLeaving", value: n(e.netLeavingSats), native: "btc" },
          {
            label: "review.field.sequence",
            value: `0x${e.sequence.toString(16).padStart(8, "0")}`,
            notes: [e.signalsRbf ? "review.note.rbfSignals" : "review.note.rbfDoesNotSignal"]
          },
          { label: "review.field.locktime", value: String(e.locktime) },
          { label: "review.field.unsignedTxid", value: e.unsignedTxid },
          { label: "review.field.digest", value: t.digest }
        ]
      },
      {
        title: "review.group.exact",
        fields: [
          {
            label: "review.field.unsignedTransaction",
            value: e.unsignedTransaction,
            notes: [{ key: "review.note.bytes", count: ro(e.unsignedTransaction) }],
            raw: !0
          },
          { label: "review.field.prevoutScript", value: e.prevoutScriptPubKey }
        ]
      }
    ]
  };
}
const Mf = {
  p2pkh: "review.note.scriptP2pkh",
  p2sh: "review.note.scriptP2sh",
  p2wpkh: "review.note.scriptP2wpkh",
  p2wsh: "review.note.scriptP2wsh",
  p2tr: "review.note.scriptP2tr"
};
function p3(e, t) {
  const n = (f) => f.toString(), r = t.boundary.kind === "account", i = e.addressType === "p2pkh", o = `${e.rate.lower} – ${e.rate.upper} sat/vB`, s = r ? "review.field.returnToAccount" : "review.field.returnToKey", a = r ? "review.field.netLeavingAccount" : "review.field.netLeaving", c = r ? "review.note.sameAccountReturn" : "review.note.sameKeyReturn", l = Zg(e.outputs, (f) => f.returned).map((f) => f.returned ? { label: s, value: n(f.amountSats), native: "btc", unit: "symbol" } : { label: "review.field.send", value: n(f.amountSats), native: "btc", unit: "symbol", to: f.address }), d = e.outputs.filter((f) => !f.returned).map((f) => ({ label: "review.field.recipientAddress", value: f.address })), u = i ? ["review.note.legacyAmountFromParent", "review.note.legacyNoAmountField"] : ["review.note.nativeAmountSupplied", "review.note.nativeFormOnly"];
  return {
    kind: i ? "review.kind.bitcoinLegacyTransfer" : "review.kind.bitcoinNativeTransfer",
    signer: { label: "review.field.signingAddress", value: e.spendingAddress, elide: !0 },
    summary: [
      ...l,
      { label: "review.field.fee", value: n(e.feeSats), native: "btc", unit: "base", secondary: o },
      { label: a, value: n(e.netLeavingSats), native: "btc", unit: "symbol" },
      // Which shared receiving index "this wallet account" is, on the first screen,
      // in the same "#i · path" form as the other protected seed-wallet reviews.
      ...t.boundary.kind === "account" ? [{ label: "review.field.walletAccount", value: `#${t.boundary.index} · ${t.boundary.path}` }] : [],
      ...d,
      { label: "review.field.network", value: e.network, notes: ["review.note.requesterNetwork"] }
    ],
    // Every byte, including the Legacy funding transaction, is parsed by the bounded verifier.
    undecoded: !1,
    ...e.highFeeWarning ? { warnings: ["review.warning.highFee"] } : {},
    details: [
      {
        title: "review.group.transaction",
        fields: [
          { label: "review.field.addressType", value: e.addressType, notes: [Mf[e.addressType]] },
          { label: "review.field.publicKey", value: t.publicKey.toLowerCase() },
          ...t.boundary.kind === "account" ? [
            { label: "review.field.walletAccount", value: `#${t.boundary.index}` },
            { label: "review.field.derivationPath", value: t.boundary.path }
          ] : [],
          { label: "review.field.scheme", value: t.scheme },
          { label: "review.field.network", value: e.network, notes: ["review.note.networkNotChainProof"] },
          { label: "review.field.input", value: `${e.inputTxid}:${e.inputVout}` },
          { label: "review.field.inputAmount", value: n(e.inputAmountSats), native: "btc", notes: u },
          ...e.outputs.flatMap((f) => [
            {
              label: "review.field.outputIndexed",
              index: f.index + 1,
              value: f.address,
              notes: [Mf[f.kind], f.returned ? c : "review.note.recipient"]
            },
            { label: "review.field.outputAmountIndexed", index: f.index + 1, value: n(f.amountSats), native: "btc" }
          ]),
          { label: "review.field.fee", value: n(e.feeSats), native: "btc" },
          { label: "review.field.feeRateRange", value: o },
          { label: "review.field.vsizeRange", value: `${e.vsize.min} – ${e.vsize.max}`, notes: ["review.note.vbytes", "review.note.sizeRange"] },
          { label: "review.field.returnAmount", value: n(e.returnedSats), native: "btc" },
          { label: a, value: n(e.netLeavingSats), native: "btc" },
          {
            label: "review.field.sequence",
            value: `0x${e.sequence.toString(16).padStart(8, "0")}`,
            notes: [e.signalsRbf ? "review.note.rbfSignals" : "review.note.rbfDoesNotSignal"]
          },
          { label: "review.field.locktime", value: String(e.locktime) },
          { label: "review.field.sighashType", value: "SIGHASH_ALL (0x01)" },
          { label: "review.field.digestAlgorithm", value: i ? "bitcoin-legacy-sighash-all" : "bitcoin-bip143-sighash-all" },
          { label: "review.field.digest", value: t.digest },
          {
            label: "review.field.unsignedTemplateHash",
            value: e.unsignedTemplateHash,
            notes: [i ? "review.note.notFinalTxid" : "review.note.finalTxidUnchanged"]
          },
          ...e.parent ? [{ label: "review.field.parentTxid", value: e.parent.txid }] : []
        ]
      },
      {
        title: "review.group.exact",
        fields: [
          {
            label: "review.field.unsignedTransaction",
            value: e.unsignedTransaction,
            notes: [{ key: "review.note.bytes", count: ro(e.unsignedTransaction) }],
            raw: !0
          },
          // Legacy's script is checked against the funding transaction; Native's is only the requested form.
          i ? { label: "review.field.prevoutScript", value: e.prevoutScriptPubKey, notes: ["review.note.scriptFromParent"] } : { label: "review.field.requestedPrevoutScript", value: e.prevoutScriptPubKey, notes: ["review.note.requestedNativeScript"] },
          ...i ? [] : [{ label: "review.field.scriptCode", value: e.serializedScriptCode }],
          ...e.outputs.map((f) => ({ label: "review.field.outputScriptIndexed", index: f.index + 1, value: f.scriptPubKey })),
          ...e.parent ? [{
            label: "review.field.parentTransaction",
            value: e.parent.transaction,
            notes: [{ key: "review.note.bytes", count: e.parent.byteLength }],
            raw: !0
          }] : []
        ]
      }
    ]
  };
}
function Sd(e, t) {
  try {
    const n = ky(e.context.release?.derivation), r = t.accounts[n.account], i = n.chain === "bitcoin" ? n.address_type : void 0, o = i === "p2pkh" ? r.bitcoin_p2pkh : i === "p2wpkh" ? r.bitcoin_p2wpkh : r[n.chain], s = e.signingContext.kind, a = i === "p2tr" ? e.scheme === "schnorr-secp256k1-bip341-keypath" && s === "bitcoin-taproot-keypath-v1" : e.scheme === "ecdsa-secp256k1-der" && s === (i === "p2pkh" ? "bitcoin-p2pkh-v1" : "bitcoin-p2wpkh-v1");
    if (n.chain === "bitcoin" ? !a : n.chain === "solana" ? e.scheme !== "ed25519" || s !== "solana-transaction-v1" : e.scheme !== "ecdsa-secp256k1-recoverable" || s !== "evm-transaction" && s !== "eip-712-typed-data") throw new Error("protected HD chain/scheme/context mismatch");
    const c = e.context.release?.signingKey;
    if (!c || typeof c != "object" || Array.isArray(c)) throw new Error("missing protected HD signing key binding");
    const l = c;
    if (Object.keys(l).length !== 3 || typeof l.secret_id != "string" || l.secret_id.length === 0 || l.public_key !== o || e.context.release?.publicKey !== o || typeof l.envelope_sha256 != "string" || !/^[0-9a-f]{64}$/.test(l.envelope_sha256) || l.envelope_sha256 !== Iy(e.material.envelope)) throw new Error("protected HD signing identity mismatch");
    return {
      selection: n,
      publicKey: o,
      path: bs(n.chain, n.account, i),
      ...i && i !== "p2tr" ? { bitcoinReturnKeys: { p2pkh: r.bitcoin_p2pkh, p2wpkh: r.bitcoin_p2wpkh, p2tr: r.bitcoin } } : {}
    };
  } catch {
    throw new _("invalid_sign_binding", "protected HD signing requires the exact signed selection/key/envelope binding");
  }
}
async function Ad(e, t) {
  if (e.context.kind !== "hd-seed") throw new Error("protected HD custody requires HD metadata");
  const n = await li(e.sealed, e.vmk, e.context);
  let r;
  try {
    return r = Ey(n), Uu(r.seed, e.identity.selection, (i) => {
      const o = G(e.identity.selection.chain === "solana" ? vt.getPublicKey(i) : fe.getPublicKey(i, !0));
      if (o !== e.identity.publicKey) throw new _("invalid_sign_binding", "protected HD seed does not match the selected authenticated public key");
      const s = e.identity.selection, a = e.identity.bitcoinReturnKeys;
      if (s.chain === "bitcoin" && s.address_type !== "p2tr") {
        if (!a || a[s.address_type] !== o) throw new _("invalid_sign_binding", "protected HD Bitcoin return identity is missing");
        for (const c of ["p2pkh", "p2wpkh", "p2tr"])
          c !== s.address_type && Uu(r.seed, { chain: "bitcoin", account: s.account, address_type: c }, (l) => {
            if (G(fe.getPublicKey(l, !0)) !== a[c]) throw new _("invalid_sign_binding", "protected HD seed does not match an authenticated Bitcoin sibling point");
          });
      }
      return t(i);
    });
  } finally {
    n.fill(0), vs(r);
  }
}
async function y3(e) {
  return Ad(e, (t) => e.payload.kind === "message" ? Ny(t, e.payload.messageHex) : Kl(t, e.payload.digest, e.request.scheme));
}
const w3 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function Bi(e) {
  let t = 0;
  for (; t < e.length && e[t] === 0; ) t += 1;
  let n = 0n;
  for (const i of e) n = n << 8n | BigInt(i);
  let r = "";
  for (; n > 0n; )
    r = w3[Number(n % 58n)] + r, n /= 58n;
  return "1".repeat(t) + r;
}
class Yg extends Error {
  code;
  constructor(t, n) {
    super(n), this.name = "SolanaMessageError", this.code = t;
  }
}
const Uo = 1232, Kf = 64, ki = 32, g3 = 32, Ff = 128, jf = 256;
function Pe(e, t) {
  throw new Yg(e, t);
}
function Vf(e) {
  let t = "";
  for (const n of e) t += n.toString(16).padStart(2, "0");
  return t;
}
function m3(e) {
  return e < 128 ? 1 : e < 16384 ? 2 : 3;
}
function b3(e, t, n = "compact-u16") {
  let r = 0;
  for (let i = 0; ; i += 1) {
    t + i >= e.length && Pe("truncated", `message ends inside ${n}`);
    const o = e[t + i];
    o === 0 && i !== 0 && Pe("invalid_compact_u16", `${n} is not minimally encoded`);
    const s = (o & 128) === 0;
    if (i === 2 && !s && Pe("invalid_compact_u16", `${n} is longer than three bytes`), r |= (o & 127) << 7 * i, r > 65535 && Pe("invalid_compact_u16", `${n} exceeds 65535`), s) return { value: r, length: i + 1 };
  }
}
class v3 {
  bytes;
  offset = 0;
  constructor(t) {
    this.bytes = t;
  }
  get remaining() {
    return this.bytes.length - this.offset;
  }
  u8(t) {
    return this.remaining < 1 && Pe("truncated", `message ends before ${t}`), this.bytes[this.offset++];
  }
  take(t, n) {
    this.remaining < t && Pe("truncated", `message ends inside ${n}`);
    const r = this.bytes.subarray(this.offset, this.offset + t);
    return this.offset += t, r;
  }
  compactU16(t) {
    const { value: n, length: r } = b3(this.bytes, this.offset, t);
    return this.offset += r, n;
  }
}
function Xg(e) {
  if (typeof e == "object" && e !== null) {
    for (const t of Object.values(e)) Xg(t);
    Object.freeze(e);
  }
  return e;
}
function x3(e, t) {
  e instanceof Uint8Array || Pe("invalid_input", "message must be a byte array"), (!(t instanceof Uint8Array) || t.length !== ki) && Pe("invalid_input", "expected public key must be 32 bytes"), e.length > Uo - 1 - Kf && Pe("packet_too_large", `message of ${e.length} bytes cannot fit a ${Uo}-byte transaction`);
  const n = new Uint8Array(e), r = Bi(new Uint8Array(t)), i = new v3(n), o = i.u8("message prefix");
  let s = "legacy", a = o;
  if ((o & Ff) !== 0) {
    const T = o & ~Ff;
    T !== 0 && Pe("unsupported_version", `Solana message version ${T} is not supported`), s = "v0", a = i.u8("required signature count");
  }
  const c = i.u8("read-only signed account count"), l = i.u8("read-only unsigned account count"), d = i.compactU16("account key count"), u = i.take(d * ki, "account keys"), f = Array.from({ length: d }, (T, D) => Bi(u.subarray(D * ki, (D + 1) * ki))), h = Bi(i.take(g3, "recent blockhash")), y = [];
  for (let T = i.compactU16("instruction count"); T > 0; T -= 1) {
    const D = i.u8("instruction program id index"), z = i.take(i.compactU16("instruction account count"), "instruction account indexes"), E = i.take(i.compactU16("instruction data length"), "instruction data");
    y.push({ programIdIndex: D, accountIndexes: z, data: E });
  }
  const w = [];
  if (s === "v0")
    for (let T = i.compactU16("address table lookup count"); T > 0; T -= 1) {
      const D = i.take(ki, "address table key"), z = i.take(i.compactU16("writable lookup index count"), "writable lookup indexes"), E = i.take(i.compactU16("read-only lookup index count"), "read-only lookup indexes");
      w.push({ accountKey: D, writableIndexes: z, readonlyIndexes: E });
    }
  i.remaining !== 0 && Pe("trailing_bytes", `${i.remaining} bytes follow the message`), a + l > d && Pe("invalid_header", "header counts more signed and read-only unsigned accounts than the message carries"), c >= a && Pe("invalid_header", "header leaves no writable signer to pay the fee");
  let p = 0, g = 0;
  for (const T of w)
    T.writableIndexes.length + T.readonlyIndexes.length === 0 && Pe("invalid_address_table_lookup", "an address table lookup loads no account"), p += T.writableIndexes.length, g += T.readonlyIndexes.length;
  const k = d + p + g;
  k > jf && Pe("invalid_address_table_lookup", `message addresses ${k} accounts; the limit is ${jf}`);
  for (const T of y) {
    (T.programIdIndex === 0 || T.programIdIndex >= d) && Pe("invalid_program_id_index", `instruction program id index ${T.programIdIndex} is not a static non-payer key`);
    for (const D of T.accountIndexes)
      D >= k && Pe("invalid_account_index", `instruction account index ${D} is outside the message's ${k} accounts`);
  }
  const b = m3(a) + a * Kf + n.length;
  b > Uo && Pe("packet_too_large", `signed transaction would be ${b} bytes; the limit is ${Uo}`);
  const O = f.indexOf(r);
  (O === -1 || O >= a) && Pe("signer_not_required", "the expected key is not a required signer of this message");
  const B = f.map((T, D) => ({
    index: D,
    address: T,
    signer: D < a,
    writable: D < a ? D < a - c : D < d - l
  })), R = w.map((T) => ({
    accountKey: Bi(T.accountKey),
    writableIndexes: Array.from(T.writableIndexes),
    readonlyIndexes: Array.from(T.readonlyIndexes)
  })), H = (T) => {
    if (T < d) return { source: "static", ...B[T] };
    const D = T - d < p;
    let z = T - d - (D ? 0 : p);
    for (const [E, x] of R.entries()) {
      const v = D ? x.writableIndexes : x.readonlyIndexes;
      if (z < v.length)
        return { source: "lookup", index: T, lookupIndex: E, accountKey: x.accountKey, tableIndex: v[z], writable: D };
      z -= v.length;
    }
    throw new Error("account index was validated against the lookup tables");
  }, L = y.map((T, D) => {
    const z = f[T.programIdIndex], E = Array.from(T.accountIndexes, H);
    return {
      index: D,
      programIdIndex: T.programIdIndex,
      programId: z,
      accounts: E,
      dataHex: Vf(T.data),
      ...F_(z, E, T.data)
    };
  });
  return Xg({
    version: s,
    messageHex: Vf(n),
    messageLength: n.length,
    transactionLength: b,
    header: { numRequiredSignatures: a, numReadonlySignedAccounts: c, numReadonlyUnsignedAccounts: l },
    feePayer: f[0],
    expectedSigner: { index: O, address: r, isFeePayer: O === 0 },
    staticAccountKeys: B,
    recentBlockhash: h,
    instructions: L,
    addressTableLookups: R,
    hasUndecodedContent: R.length > 0 || L.some((T) => T.decoded === null)
  });
}
function Cn(e, t) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new _("invalid_payload", `${t} must be an object`);
  return e;
}
function Do(e, t) {
  if (typeof e != "string" || e.length === 0) throw new _("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function ar(e, t, n) {
  if (Object.keys(e).some((r) => !t.includes(r)))
    throw new _("invalid_payload", `${n} carries unsupported fields; script trees are not supported`);
}
function _d(e) {
  if (e && typeof e == "object") {
    for (const t of Object.values(e)) _d(t);
    Object.freeze(e);
  }
  return e;
}
function Id(e, t) {
  if (Yo(e), t && typeof t == "object") {
    const n = t.release;
    Yo(n), n && typeof n == "object" && Yo(n.signingContext);
  }
}
function Jg(e) {
  const t = Cn(e, "sign request");
  E_(t), Id(t.signingContext, t.context), di(t);
  const n = Cn(structuredClone(t), "sign request");
  ar(n, ["material", "scheme", "signingContext", "context"], "sign request");
  const r = Cn(n.material, "material");
  ar(r, ["name", "envelope"], "material");
  const i = Cn(r.envelope, "envelope");
  ar(i, ["ciphertext", "nonce", "wrap_meta"], "envelope");
  const o = rd(n.context);
  ar(Cn(o, "context"), ["v", "purpose", "requestId", "grantId", "display", "release", "agent", "expiresAt", "signature"], "sign context");
  let s;
  try {
    s = Rl(n.scheme);
  } catch {
    throw new _("invalid_payload", "unsupported signing scheme");
  }
  const a = Cn(n.signingContext, "signingContext");
  return a.kind === "solana-transaction-v1" && ka(a), _d({
    material: {
      name: Do(r.name, "material.name"),
      envelope: {
        ciphertext: Do(i.ciphertext, "envelope.ciphertext"),
        nonce: Do(i.nonce, "envelope.nonce"),
        wrap_meta: Do(i.wrap_meta, "envelope.wrap_meta")
      }
    },
    scheme: s,
    signingContext: a,
    context: o
  });
}
function Ne() {
  throw new _("invalid_sign_binding", "signing approval is missing or does not match its signed binding; update Avibe and recreate the request");
}
function qc(e, t, n) {
  Id(e.signingContext, e.context), zs({ context: e.context, expectedPurpose: "sign", secret: { name: e.material.name, kind: "keypair" } });
  const r = e.context.release;
  (!r || Array.isArray(r)) && Ne();
  const i = t.kind === "message";
  !i && (t.kind !== "digest" || !/^[0-9a-f]{64}$/.test(t.digest)) && Ne();
  const o = jn(e.material.envelope).recordMetadata, s = o?.kind === "hd-seed" ? Sd(e, o) : void 0;
  s && s.publicKey !== n && Ne();
  const a = qn(e.signingContext);
  if (ar(r, ["name", "scheme", i ? "messageHex" : "digest", "publicKey", "signingContext", ...s || a ? ["signingKey"] : [], ...s ? ["derivation"] : [], ...a ? ["sandboxCohort"] : []], "sign context.release"), a && qn(e.signingContext)) {
    const c = s ? Xo.hdInputTypes : Xo.ordinaryInputTypes;
    ["p2pkh", "p2wpkh", "p2tr"].every((f) => c.includes(f) && Xo.ecdsaReturnTypes.includes(f)) || Ne();
    const l = Cn(r.signingKey, "sign context.release.signingKey");
    ar(l, ["secret_id", "public_key", "envelope_sha256"], "sign context.release.signingKey");
    const d = Cn(r.sandboxCohort, "sign context.release.sandboxCohort");
    ar(d, ["buildHash", "manifestHash"], "sign context.release.sandboxCohort"), (Object.keys(l).length !== 3 || typeof l.secret_id != "string" || l.secret_id.length === 0 || !/^(02|03)[0-9a-f]{64}$/.test(n) || l.public_key !== n || r.publicKey !== n || l.envelope_sha256 !== Iy(e.material.envelope) || Object.keys(d).length !== 2 || d.buildHash !== ye.buildHash || typeof d.manifestHash != "string" || !/^sha256-[A-Za-z0-9+/]{43}=$/.test(d.manifestHash)) && Ne();
    const u = d.manifestHash.slice(7);
    le(Ee(u)) !== u && Ne(), s && (s.selection.chain !== "bitcoin" || s.selection.address_type !== e.signingContext.addressType) && Ne();
  }
  if (i) {
    const c = ka(e.signingContext);
    (t.messageHex !== c.messageHex || r.messageHex !== t.messageHex || e.scheme !== "ed25519" || !/^[0-9a-f]{64}$/.test(n) || r.publicKey !== n) && Ne();
  } else (e.scheme === "ed25519" || e.signingContext.kind === "solana-transaction-v1" || r.digest !== t.digest) && Ne();
  (r.name !== e.material.name || r.scheme !== e.scheme || typeof r.publicKey != "string" || r.publicKey.toLowerCase() !== n.toLowerCase() || !r.signingContext || me(r.signingContext) !== me(e.signingContext)) && Ne();
}
function k3(e) {
  return Id(e.signingContext, e.context), ue(new TextEncoder().encode(ga(e.context)));
}
function qf(e, t) {
  if (Rl(e), e === "ed25519") throw new Error("Ed25519 exact-message signing is reviewed structurally and has no prose description");
  if (!/^(02|03)[0-9a-fA-F]{64}$/.test(t)) throw new Error("signing public key must be compressed secp256k1");
  return e === Cl ? `BIP340 raw Schnorr (untweaked internal key)
internal key: ${t.slice(2).toLowerCase()}` : e === Di ? J("bitcoin.signingIdentity", { key: t.slice(2).toLowerCase() }) : `${e === oa ? "ECDSA secp256k1 recoverable" : "ECDSA secp256k1 DER"}
public key: ${t.toLowerCase()}`;
}
function E3(e, t) {
  const n = ka(e.signingContext);
  qc(e, { kind: "message", messageHex: n.messageHex }, t);
  let r;
  try {
    r = x3(wl(n.messageHex), ut(t));
  } catch (i) {
    throw i instanceof Yg ? new _("invalid_payload", `Solana message cannot be signed (${i.code}): ${i.message}`) : i;
  }
  return r.messageHex !== n.messageHex && Ne(), { payload: { kind: "message", messageHex: r.messageHex }, review: c3(r) };
}
function S3(e, t, n) {
  const r = e.signingContext;
  if (r.kind === "solana-transaction-v1" && Ne(), qn(r)) {
    if (e.scheme !== Es) throw new _("invalid_payload", "Legacy/Native Bitcoin contexts require the DER ECDSA scheme");
    const s = Og(r, r.digest, t, n?.bitcoinReturnKeys);
    return qc(e, { kind: "digest", digest: s.digest }, t), {
      payload: { kind: "digest", digest: s.digest },
      display: qf(e.scheme, t),
      review: p3(s.facts, {
        scheme: e.scheme,
        digest: s.digest,
        publicKey: t,
        boundary: n ? { kind: "account", index: n.selection.account, path: n.path } : { kind: "key" }
      })
    };
  }
  let i = g_(r);
  if (r.kind === "bitcoin-taproot-keypath-v1") {
    if (e.scheme !== Di)
      throw new _("invalid_payload", "Bitcoin Taproot contexts require the BIP341 key-path signing scheme");
  } else {
    if (e.scheme === Di)
      throw new _("invalid_payload", "the BIP341 key-path signing scheme requires a Bitcoin Taproot context");
    if (r.kind !== "avault-agent-operation" && e.scheme !== oa && e.scheme !== Es)
      throw new _("invalid_payload", "EVM signing contexts require an ECDSA scheme");
  }
  let o;
  if (r.kind === "bitcoin-taproot-keypath-v1") {
    const s = Ag(r, i.digest, t);
    i = { digest: s.digest, challenge: i.challenge, display: s.display }, o = s.facts;
  }
  return qc(e, { kind: "digest", digest: i.digest }, t), {
    payload: { kind: "digest", digest: i.digest },
    display: `${qf(e.scheme, t)}
scheme: ${e.scheme}
${i.display}`,
    ...i.evm ? { review: f3(i.evm, { publicKey: t, scheme: e.scheme, digest: i.digest }) } : {},
    ...o ? { review: h3(o, { scheme: e.scheme, digest: i.digest }) } : {},
    ...A3(i.typedData, { publicKey: t, scheme: e.scheme, digest: i.digest })
  };
}
function A3(e, t) {
  if (e === void 0) return {};
  try {
    const n = Q_(e, t);
    return uw(JSON.parse(JSON.stringify(n))), { review: n };
  } catch {
    return {};
  }
}
function Qg(e) {
  const t = jn(e.material.envelope), n = la(e.material.name, t.recordMetadata), r = t.recordMetadata?.kind === "hd-seed" ? Sd(e, t.recordMetadata) : void 0, i = r ? {
    name: e.material.name,
    kind: "keypair",
    publicKey: r.publicKey,
    ...r.selection.chain === "solana" ? { curve: "ed25519" } : {}
  } : n;
  m_(e.scheme, e.signingContext, i);
  const o = i.publicKey, s = e.signingContext.kind === "solana-transaction-v1" ? E3(e, o) : S3(e, o, r);
  if (r && !qn(e.signingContext)) {
    const a = `#${r.selection.account} · ${r.path}${r.selection.chain === "bitcoin" ? " · Taproot" : ""}`;
    if (s.review) {
      const c = [{ label: "review.field.walletAccount", value: a }];
      s.review = { ...s.review, summary: [...s.review.summary, ...c], details: [{ title: "review.group.hdAccount", fields: c }, ...s.review.details] };
    } else if (typeof s.display == "string") {
      const c = da(r.publicKey).eth;
      (!c || r.selection.chain !== "evm") && Ne(), s.display = `${J("review.field.walletAccount")}: ${a}
${J("review.field.signingAddress")}: ${c}

${s.display}`;
    }
  }
  return { unpacked: t, recordContext: n, content: s, ...r ? { hd: r } : {} };
}
function _3(e) {
  const t = Jg(e), { unpacked: n, recordContext: r, content: i } = Qg(t), o = k3(t);
  return _d({ request: t, verified: i, unpacked: n, recordContext: r, challenge: [...o], ...n.recordMetadata?.kind === "hd-seed" ? { hd: Sd(t, n.recordMetadata) } : {} });
}
async function em(e, t, n, r = !0) {
  const i = Jg(e.request), o = Qg(i);
  (!e.verified || me(e.verified.payload) !== me(o.content.payload)) && Ne();
  const s = await Is(n, t);
  return sr({ context: i.context, rootMetadata: s, expectedPurpose: "sign" }), ti([i.context]), r && (o.hd ? await Ad({ sealed: o.unpacked.sealed, vmk: t, context: o.recordContext, identity: o.hd }, () => {
  }) : qn(i.signingContext) && await tm(o, t, () => {
  })), { request: i, ...o };
}
async function Wf(e, t, n) {
  return (await em(e, t, n)).content;
}
async function tm(e, t, n) {
  const { recordContext: r } = e;
  (r.kind !== "keypair" || !r.publicKey || r.curve === "ed25519") && Ne();
  const i = await li(e.unpacked.sealed, t, r);
  try {
    return G(fe.getPublicKey(i, !0)) !== r.publicKey && Ne(), n(i);
  } finally {
    i.fill(0);
  }
}
async function Gf(e, t, n, r) {
  const i = await em(e, t, n, !qn(e.request.signingContext)), { request: o, content: s, unpacked: a, recordContext: c, hd: l } = i;
  if (qn(o.signingContext)) {
    if (!r || s.payload.kind !== "digest") throw new _("invalid_sign_binding", "new Bitcoin signing requires the current scoped authorization lease");
    const d = l?.publicKey ?? c.publicKey, u = s.payload.digest, f = (h) => {
      r(), ti([o.context]);
      const y = Kl(h, u, o.scheme);
      return O_(o.signingContext, u, d, y.signature, l?.bitcoinReturnKeys), r(), y;
    };
    return l ? Ad({ sealed: a.sealed, vmk: t, context: c, identity: l }, f) : tm(i, t, f);
  }
  return l ? y3({ request: o, sealed: a.sealed, vmk: t, context: c, identity: l, payload: s.payload }) : s.payload.kind === "message" ? b2(a.sealed, t, c, s.payload.messageHex) : m2(a.sealed, t, c, s.payload.digest, o.scheme);
}
function nm(e, t = e.label) {
  return {
    title: e.title,
    subtitle: e.subtitle,
    ...e.body !== void 0 ? { body: e.body } : {},
    ...e.details ? { details: e.details } : {},
    ...e.review ? { review: e.review } : {},
    ...e.reviewSession !== void 0 ? { reviewSession: e.reviewSession } : {},
    confirmLabel: t
  };
}
function I3(e) {
  const { challenge: t, ...n } = e;
  return {
    ...n,
    ...t ? { challenge: le(t) } : {}
  };
}
function Wc(e, t) {
  if (typeof e != "object" || e === null) throw new _("invalid_payload", `${t} must be an object`);
  return e;
}
function cr(e, t) {
  if (typeof e == "string" && e.length > 0) return e;
  if (typeof e != "object" || e === null) throw new _("invalid_payload", `${t} must be a text spec`);
  const n = e;
  if (typeof n.text == "string") return _e(n.text);
  if (typeof n.key == "string") {
    const r = n.params;
    if (r !== void 0 && (typeof r != "object" || r === null || Array.isArray(r)))
      throw new _("invalid_payload", `${t}.params must be an object`);
    return {
      key: n.key,
      ...r !== void 0 ? { params: r } : {}
    };
  }
  throw new _("invalid_payload", `${t} must be a text spec`);
}
function B3(e) {
  const t = Wc(e, "details");
  if (!Array.isArray(t.rows)) throw new _("invalid_payload", "details must contain rows");
  const n = [];
  for (const [r, i] of t.rows.entries()) {
    const o = Wc(i, `details.rows[${r}]`), s = o.variant;
    if (s !== void 0 && s !== "text" && s !== "command" && s !== "mono" && s !== "chips")
      throw new _("invalid_payload", `details.rows[${r}].variant is invalid`);
    const a = cr(o.label, `details.rows[${r}].label`);
    if (s === "chips") {
      if (!Array.isArray(o.values)) throw new _("invalid_payload", `details.rows[${r}].values must be an array`);
      const c = o.tone;
      if (c !== void 0 && c !== "default" && c !== "warning")
        throw new _("invalid_payload", `details.rows[${r}].tone is invalid`);
      const l = c === "default" || c === "warning" ? c : void 0;
      n.push({
        label: a,
        values: o.values.map((d, u) => cr(d, `details.rows[${r}].values[${u}]`)),
        variant: "chips",
        ...l !== void 0 ? { tone: l } : {}
      });
      continue;
    }
    n.push({
      label: a,
      value: cr(o.value, `details.rows[${r}].value`),
      ...s !== void 0 ? { variant: s } : {}
    });
  }
  return { rows: n };
}
function T3(e) {
  try {
    return uw(e);
  } catch (t) {
    throw new _("invalid_payload", t instanceof Error ? t.message : "signing review is malformed");
  }
}
function P3(e) {
  const t = Wc(e, "authorization prompt");
  if (t.challenge !== void 0 && (typeof t.challenge != "string" || t.challenge.length === 0))
    throw new _("invalid_payload", "prompt.challenge must be a non-empty string");
  if (t.reviewSession !== void 0 && typeof t.reviewSession != "string")
    throw new _("invalid_payload", "prompt.reviewSession must be a string");
  const n = t.challenge === void 0 ? void 0 : Ee(t.challenge);
  return {
    title: cr(t.title, "prompt.title"),
    subtitle: cr(t.subtitle, "prompt.subtitle"),
    ...t.body !== void 0 ? { body: cr(t.body, "prompt.body") } : {},
    ...t.details !== void 0 ? { details: B3(t.details) } : {},
    ...t.review !== void 0 ? { review: T3(t.review) } : {},
    ...t.reviewSession !== void 0 ? { reviewSession: t.reviewSession } : {},
    ...n ? { challenge: n } : {},
    label: cr(t.label, "prompt.label")
  };
}
const Zf = 320, Yf = 220, $3 = 6e4, O3 = 1e3;
function C3(e, t = {}) {
  if (e.uiShowPending) return { ok: !1, code: "sandbox_not_visible", detail: "ui show is still pending" };
  if (!e.documentVisible) return { ok: !1, code: "sandbox_not_visible", detail: "document is not visible" };
  if ((t.requireFocus ?? !0) && !e.documentFocused)
    return { ok: !1, code: "sandbox_not_visible", detail: "document is not focused" };
  if (e.frameWidth < Zf || e.frameHeight < Yf)
    return { ok: !1, code: "sandbox_not_visible", detail: "sandbox frame is too small" };
  if (!e.visibleByIntersectionObserver || e.intersectionRatio < 0.99)
    return { ok: !1, code: "sandbox_not_visible", detail: "sandbox frame is not fully visible" };
  const n = [];
  if (e.embedded) {
    const r = e.parent;
    r ? (r.ageMs > $3 && n.push("parent frame visibility is stale"), (r.frameWidth < Zf || r.frameHeight < Yf) && n.push("parent frame is too small"), (!r.visibleByIntersectionObserver || r.intersectionRatio < 0.99) && n.push("parent frame is not fully visible"), (r.opacity < 0.99 || !r.pointerEvents) && n.push("parent frame is visually occluded")) : n.push("parent frame visibility is not attested");
  }
  return n.length > 0 ? { ok: !0, warnings: n } : { ok: !0 };
}
function Xf(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Yn(e) {
  if (typeof e == "number") return Number.isFinite(e) ? e : void 0;
  if (typeof e != "string" || e.trim() === "") return;
  const t = Number(e);
  return Number.isFinite(t) ? t : void 0;
}
function Rr(e) {
  const t = Yn(e);
  if (t !== void 0) return t;
  if (typeof e != "string" || e.trim() === "") return;
  const n = Date.parse(e);
  return Number.isFinite(n) ? n : void 0;
}
function cc(e) {
  return typeof e == "boolean" ? e : void 0;
}
function R3(e) {
  if (typeof e == "boolean") return e;
  if (typeof e == "string")
    return e.toLowerCase() !== "none";
}
function L3(e) {
  if (!e) return;
  const t = Xf(e.value);
  if (!t) return;
  const n = Xf(t.frame) ?? t, r = Yn(n.frameWidth) ?? Yn(n.width), i = Yn(n.frameHeight) ?? Yn(n.height), o = Yn(n.intersectionRatio), s = cc(n.visibleByIntersectionObserver) ?? cc(n.isVisible) ?? cc(n.visible), a = Yn(n.opacity), c = R3(n.pointerEvents), l = Rr(t.sampledAt) ?? Rr(t.measuredAt) ?? Rr(t.timestamp) ?? Rr(n.sampledAt) ?? Rr(n.measuredAt) ?? Rr(n.timestamp);
  if (r === void 0 || i === void 0 || o === void 0 || s === void 0 || a === void 0 || c === void 0 || l === void 0)
    return;
  const d = Date.now();
  if (!(l > d + O3))
    return {
      frameWidth: r,
      frameHeight: i,
      intersectionRatio: o,
      visibleByIntersectionObserver: s,
      opacity: a,
      pointerEvents: c,
      ageMs: Math.max(0, d - l)
    };
}
function rm(e) {
  const t = e.visibilityTarget ?? document.body ?? document.documentElement;
  let n = { ratio: 0, visible: !1 }, r = !1, i = !1, o, s;
  const a = () => ({
    documentVisible: document.visibilityState === "visible",
    documentFocused: typeof document.hasFocus == "function" ? document.hasFocus() : !1,
    frameWidth: window.innerWidth,
    frameHeight: window.innerHeight,
    intersectionRatio: n.ratio,
    visibleByIntersectionObserver: n.visible,
    uiShowPending: e.uiShowPending(),
    embedded: window.parent !== window,
    parent: L3(e.parentSurface?.())
  }), c = (y) => {
    const w = a(), p = C3(w, { requireFocus: y });
    if (!p.ok) throw new _(p.code, p.detail, !0);
    if (e.requireParentSurface && w.embedded && (p.warnings?.length ?? 0) > 0)
      throw new _("sandbox_not_visible", p.warnings[0], !0);
    for (const g of p.warnings ?? []) console.warn(`[vault-sandbox] Confirm surface advisory: ${g}`);
  }, l = () => c(!0), d = new Promise((y, w) => {
    o = y, s = w;
  }), u = () => {
    if (!(i || r)) {
      i = !0;
      try {
        c(!1), o();
      } catch (y) {
        s(y);
      }
    }
  }, f = typeof IntersectionObserver > "u" ? null : new IntersectionObserver(
    (y) => {
      const w = y[0], p = w?.isVisible;
      n = w ? {
        ratio: w.intersectionRatio,
        visible: p === void 0 ? w.intersectionRatio >= 0.99 : p === !0
      } : { ratio: 0, visible: !1 }, u();
    },
    {
      threshold: [0, 0.99, 1],
      trackVisibility: !0,
      delay: 100
    }
  );
  f?.observe(t);
  const h = setTimeout(u, 250);
  return {
    ready: d,
    assertCurrent: l,
    dispose: () => {
      r || (r = !0, clearTimeout(h), f?.disconnect());
    }
  };
}
function im(e) {
  const t = e.tier === "R2" && e.policy.strictApprovals ? "R3" : e.tier, n = e.vaultState !== "unlocked";
  return t === "R1" ? {
    tier: e.tier,
    effectiveTier: t,
    confirm: !1,
    passkey: n ? "unlock" : "none",
    renewOnSuccess: !0
  } : n ? {
    tier: e.tier,
    effectiveTier: t,
    confirm: !0,
    passkey: "unlock",
    renewOnSuccess: t !== "R3"
  } : {
    tier: e.tier,
    effectiveTier: t,
    confirm: !0,
    passkey: t === "R3" ? "uv" : "none",
    renewOnSuccess: t !== "R3"
  };
}
async function H3(e) {
  let t = new TextEncoder().encode(e.value);
  try {
    const n = { name: e.name, kind: "static" }, r = await Ul(t, e.vmk, n);
    return { envelope: Dl(r, e.wrapMeta, n) };
  } finally {
    t?.fill(0), t = void 0;
  }
}
async function N3(e) {
  const t = ci(e.curve), n = g2(t);
  let r = n.privateKey;
  try {
    const i = {
      name: e.name,
      kind: "keypair",
      publicKey: n.publicKey,
      ...t === "ed25519" ? { curve: t } : {}
    }, o = await Ul(r, e.vmk, i);
    return {
      envelope: Dl(o, e.wrapMeta, i),
      publicKey: n.publicKey,
      addresses: t === "ed25519" ? { solana: v2(n.publicKey) } : da(n.publicKey)
    };
  } finally {
    r?.fill(0), r = void 0;
  }
}
function Bd(e) {
  return (e?.kind ?? "static") === "static";
}
function U3(e, t) {
  if (typeof e != "object" || e === null) throw new _("invalid_payload", "approveRelease item must be an object");
  const n = e;
  return {
    material: t(n.material),
    context: rd(n.context)
  };
}
function om(e) {
  const t = e[0]?.context.display;
  if (!t) throw new _("invalid_payload", "approveRelease requires at least one item");
  if (e[0].context.purpose !== "agent-deliver") throw new _("invalid_context", "approveRelease only accepts agent-deliver contexts");
  const n = cf(e[0].context), r = Jf(e[0].context);
  for (const s of e) {
    if (s.context.purpose !== "agent-deliver") throw new _("invalid_context", "approveRelease only accepts agent-deliver contexts");
    if (cf(s.context) !== n)
      throw new _("invalid_context", "approveRelease batch contexts must share one signed display block");
    if (Jf(s.context) !== r)
      throw new _("invalid_context", "approveRelease batch contexts must share one signed recipient");
  }
  const i = new Set(t.secrets.map((s) => `${s.name}:${s.kind}`));
  for (const s of e) {
    const { recordMetadata: a } = jn(s.material.envelope), c = a?.kind ?? "static";
    if (!i.has(`${s.material.name}:${c}`))
      throw new _("invalid_context", "signed display block does not cover every release item");
  }
  const o = e[0].context;
  return {
    display: t,
    recipient: {
      agentFingerprint: o.agent?.fingerprint,
      grantId: o.grantId
    }
  };
}
function Jf(e) {
  if (!e.grantId) throw new _("invalid_context", "agent delivery context is missing grantId");
  if (!e.agent) throw new _("invalid_context", "agent delivery context is missing agent key");
  return me({ agent: e.agent, grantId: e.grantId });
}
async function D3(e) {
  if (e.items.length === 0) throw new _("invalid_payload", "approveRelease requires at least one item");
  const t = om(e.items), n = await Is(e.wrapMeta, e.vmk), r = e.items.map((a) => a.context);
  for (const a of e.items)
    sr({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: e.now
    });
  ti(r, e.now);
  const i = await jk(r);
  await e.confirm({
    ...t,
    challenge: i
  });
  const o = e.now ?? Date.now();
  for (const a of e.items)
    sr({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: o
    });
  e.onApprovalAccepted?.(o), ti(r, o);
  const s = [];
  for (const a of e.items) {
    const { sealed: c, recordMetadata: l } = jn(a.material.envelope);
    if (!Bd(l)) throw new _("invalid_payload", "approveRelease requires static protected records");
    s.push(
      await w2(
        c,
        e.vmk,
        Fk(a.context),
        la(a.material.name, l),
        await Kk(a.context, a.material.name)
      )
    );
  }
  return e.consumeReplayIds !== !1 && await Fi(r, o), { blindBoxes: s };
}
const Qf = "vault-signing-policy-downgrade", z3 = `avibe:vault:signing-policy-downgrade:v1
`, zr = 65536, M3 = /^[A-Za-z0-9_-]{1,80}$/, eh = ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"];
function ne(e) {
  throw new _("policy_context_invalid", e);
}
function Ln(e, t, n) {
  (e === null || typeof e != "object" || Array.isArray(e) || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null) && ne(`${n} must be an object`);
  const r = e;
  return (Object.keys(r).length !== t.length || !t.every((i) => Object.hasOwn(r, i))) && ne(`${n} has missing or unknown fields`), r;
}
function Gc(e, t) {
  return (typeof e != "string" || e.length === 0) && ne(`${t} must be a nonempty string`), e;
}
function lc(e, t) {
  return (typeof e != "string" || !M3.test(e)) && ne(`${t} is not a valid ID`), e;
}
function Hn(e, t, n) {
  (typeof e != "string" || e.length > 4 * Math.ceil(n / 3) || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) && ne("noncanonical policy base64");
  const r = Ee(e);
  return (r.length < t || r.length > n || le(r) !== e) && ne("policy base64 length or padding is invalid"), r;
}
const K3 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function F3(e) {
  let t = 0n;
  for (const i of e) {
    const o = K3.indexOf(i);
    o < 0 && ne("invalid Base58 address"), t = t * 58n + BigInt(o);
  }
  const n = [];
  for (; t > 0n; )
    n.push(Number(t & 255n)), t >>= 8n;
  const r = /^1*/.exec(e)[0].length;
  return Uint8Array.from([...new Array(r).fill(0), ...n.reverse()]);
}
const th = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function nh(e, t, n) {
  (!e.startsWith(`bc1${th[t]}`) || e.length !== (n === 20 ? 42 : 62)) && ne("invalid mainnet witness address");
  const r = [...e.slice(4, -6)].map((c) => {
    const l = th.indexOf(c);
    return l < 0 && ne("invalid Bech32 address character"), l;
  });
  let i = 0, o = 0;
  const s = [];
  for (const c of r)
    i = (i << 5 | c) & 65535, o += 5, o >= 8 && (o -= 8, s.push(i >>> o & 255));
  (o >= 5 || (i & (1 << o) - 1) !== 0 || s.length !== n) && ne("noncanonical witness address padding");
  const a = Uint8Array.from(s);
  return Pc("bc", t, a) !== e && ne("invalid witness address checksum"), a;
}
function j3(e, t) {
  if (e === null) return null;
  const n = Gc(e, `addresses.${t}`);
  if (n.length > 128 && ne("policy address is too long"), t === "eth")
    (!/^0x[0-9a-fA-F]{40}$/.test(n) || `0x${My(n.slice(2))}` !== n) && ne("noncanonical EIP-55 address");
  else if (t === "btc_segwit") nh(n, 0, 20);
  else if (t === "btc_taproot") nh(n, 1, 32);
  else {
    const r = F3(n);
    t === "solana" ? (r.length !== 32 || Bi(r) !== n) && ne("noncanonical Solana address") : (r.length !== 25 || r[0] !== 0 || Dy(r.slice(0, 21)) !== n) && ne("invalid mainnet P2PKH address");
  }
  return n;
}
function V3(e) {
  return z3 + me(e);
}
function sm(e) {
  return ue(new TextEncoder().encode(V3(e)));
}
function Mn(e, t = Date.now()) {
  const n = Date.parse(e.expiresAt) - t;
  if (Number.isFinite(n) || ne("invalid policy expiry"), n <= 0) throw new _("policy_context_expired", "policy context has expired", !0);
  if (n > 15e4) throw new _("policy_context_clock_skew", "check this device's clock and retry", !0);
  return n;
}
function am(e, t, n = Date.now(), r) {
  try {
    const i = JSON.stringify(e);
    (i === void 0 || i.length > zr) && ne("policy context exceeds its size bound"), di(e);
    const o = Ln(e, ["v", "purpose", "challengeId", "nonce", "expiresAt", "rpId", "origin", "target", "change", "factors"], "context");
    (o.v !== 1 || o.purpose !== Qf) && ne("unsupported policy context version or purpose"), (o.origin !== t.origin || o.rpId !== t.hostname) && ne("policy origin or RP does not match this sandbox");
    const s = Gc(o.expiresAt, "expiresAt"), a = Date.parse(s);
    (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(s) || !Number.isFinite(a) || new Date(a).toISOString() !== s) && ne("policy expiry must be an exact UTC millisecond date"), Hn(o.nonce, 32, 32);
    const c = Ln(o.target, ["secretId", "name", "protection", "signerKind", "account", "accountName", "addresses", "identitySha256"], "target");
    c.protection !== "standard" && c.protection !== "protected" && ne("invalid protection tier"), c.signerKind !== "local" && c.signerKind !== "hd-seed" && ne("unsupported signer kind"), (typeof c.identitySha256 != "string" || !/^[0-9a-f]{64}$/.test(c.identitySha256)) && ne("invalid identity digest");
    const l = Ln(c.addresses, eh, "addresses"), d = Object.fromEntries(eh.map((p) => [p, j3(l[p], p)]));
    if (c.signerKind === "hd-seed")
      (c.protection !== "standard" || typeof c.account != "number" || !Number.isSafeInteger(c.account) || c.account < 0 || c.account > 19) && ne("invalid shared HD account"), c.accountName !== null && (typeof c.accountName != "string" || [...c.accountName].length < 1 || [...c.accountName].length > 80) && ne("invalid HD account name"), (d.eth === null || d.btc_taproot === null || d.solana === null || d.btc_legacy !== null || d.btc_segwit !== null) && ne("invalid shared HD address set");
    else {
      (c.account !== null || c.accountName !== null) && ne("ordinary keys cannot carry an HD account");
      const p = d.eth !== null && d.btc_legacy !== null && d.btc_segwit !== null && d.btc_taproot !== null && d.solana === null, g = d.solana !== null && d.eth === null && d.btc_legacy === null && d.btc_segwit === null && d.btc_taproot === null;
      !p && !g && ne("invalid ordinary key address set"), p && c.protection === "protected" && !C_(r) && ne("this sandbox build does not support every protected Bitcoin address type");
    }
    const u = Ln(o.change, ["field", "from", "to", "revision"], "change");
    (u.field !== "always_ask" || u.from !== !0 || u.to !== !1 || typeof u.revision != "string" || !/^(?:legacy|[0-9a-f]{32})$/.test(u.revision)) && ne("only true-to-false always_ask changes are supported"), (!Array.isArray(o.factors) || o.factors.length === 0) && ne("policy factors must be nonempty");
    const f = /* @__PURE__ */ new Set();
    let h = "";
    const y = o.factors.map((p) => {
      const g = Ln(p, ["factorId", "credentialId"], "factor"), k = lc(g.factorId, "factorId");
      Hn(g.credentialId, 1, 1023);
      const b = g.credentialId;
      return (k <= h || f.has(b)) && ne("policy factors must have unique credentials and sorted unique IDs"), h = k, f.add(b), { factorId: k, credentialId: b };
    }), w = {
      v: 1,
      purpose: Qf,
      challengeId: lc(o.challengeId, "challengeId"),
      nonce: o.nonce,
      expiresAt: s,
      rpId: o.rpId,
      origin: o.origin,
      target: {
        secretId: lc(c.secretId, "secretId"),
        name: Gc(c.name, "name"),
        protection: c.protection,
        signerKind: c.signerKind,
        account: c.account,
        accountName: c.accountName,
        addresses: d,
        identitySha256: c.identitySha256
      },
      change: { field: "always_ask", from: !0, to: !1, revision: u.revision },
      factors: y
    };
    return new TextEncoder().encode(me(w)).length > zr && ne("policy context exceeds its UTF-8 size bound"), Mn(w, n), w;
  } catch (i) {
    if (i instanceof _) throw i;
    ne("policy context is not canonical JSON");
  }
}
function q3(e, t) {
  return am(Ln(e, ["context"], "payload").context, t);
}
function W3(e) {
  const t = Mn(e), n = Math.min(12e4, Math.floor(t));
  if (n <= 0) throw new _("policy_context_expired", "policy context has expired", !0);
  return {
    challenge: new Uint8Array(sm(e)).buffer,
    rpId: e.rpId,
    userVerification: "required",
    timeout: n,
    allowCredentials: e.factors.map((r) => ({ type: "public-key", id: new Uint8Array(Hn(r.credentialId, 1, 1023)).buffer }))
  };
}
function rh(e) {
  return le(e).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function Zc(e, t) {
  try {
    const n = Ln(e, ["id", "rawId", "type", "response"], "assertion"), r = Hn(n.rawId, 1, 1023);
    (n.type !== "public-key" || n.id !== rh(r) || !t.factors.some((l) => l.credentialId === n.rawId)) && ne("assertion does not identify a frozen factor");
    const i = Ln(n.response, ["clientDataJSON", "authenticatorData", "signature", "userHandle"], "assertion.response"), o = Hn(i.clientDataJSON, 1, zr), s = JSON.parse(new TextDecoder("utf-8", { fatal: !0 }).decode(o));
    (s.type !== "webauthn.get" || s.challenge !== rh(sm(t)) || s.origin !== t.origin || s.crossOrigin !== void 0 && s.crossOrigin !== !1) && ne("assertion client data does not bind this policy context");
    const a = Hn(i.authenticatorData, 37, zr), c = ue(new TextEncoder().encode(t.rpId));
    return ((a[32] & 5) !== 5 || !a.slice(0, 32).every((l, d) => l === c[d])) && ne("assertion RP or UP/UV flags are invalid"), Hn(i.signature, 1, zr), i.userHandle !== null && Hn(i.userHandle, 0, zr), Mn(t), { id: n.id, rawId: n.rawId, type: "public-key", response: { ...i } };
  } catch (n) {
    throw n instanceof _ && n.code !== "policy_context_invalid" ? n : new _("authorization_result_invalid", "invalid policy assertion");
  }
}
async function G3(e, t) {
  if (t.aborted) throw t.reason;
  const n = await navigator.credentials.get({ publicKey: W3(e), signal: t });
  if (t.aborted) throw t.reason;
  if (!n) throw new Error("passkey-cancelled");
  return Zc(iw(n), e);
}
function dc(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new _("invalid_payload", "reveal material must be an object");
  return e;
}
function zo(e) {
  if (typeof e != "string" || e.length === 0) throw new _("invalid_payload", "reveal material requires nonempty strings");
  return e;
}
function cm(e) {
  return e && typeof e == "object" && (Object.values(e).forEach(cm), Object.freeze(e)), e;
}
function Z3(e) {
  const t = { ciphertext: e.ciphertext, nonce: e.nonce, wrap_meta: e.wrap_meta };
  return di(t), G(ue(new TextEncoder().encode(me(t))));
}
function Jo(e) {
  zs({ context: e.context, expectedPurpose: "reveal", secret: { name: e.material.name, kind: "static" } });
  const t = e.context.release, n = t?.envelopeHash;
  if (!t || t.name !== e.material.name || !n || typeof n != "object" || Array.isArray(n))
    throw new _("invalid_reveal_binding", "reveal approval does not bind this envelope");
  const r = n;
  if (Object.keys(r).length !== 2 || r.alg !== "sha256" || typeof r.digest != "string" || !/^[0-9a-f]{64}$/.test(r.digest) || r.digest !== Z3(e.material.envelope))
    throw new _("invalid_reveal_binding", "reveal approval does not bind this envelope");
}
function Y3(e) {
  di(e);
  const t = dc(structuredClone(e)), n = dc(t.material), r = dc(n.envelope), i = cm({
    material: { name: zo(n.name), envelope: {
      ciphertext: zo(r.ciphertext),
      nonce: zo(r.nonce),
      wrap_meta: zo(r.wrap_meta)
    } },
    context: rd(t.context)
  });
  return Jo(i), i;
}
function lm(e) {
  const t = xs(e);
  if (t.v !== 1 || !Array.isArray(t.copies) || t.copies.length === 0 || ["record_meta", "dek_nonce", "wrapped_dek"].some((n) => Object.hasOwn(t, n)) || t.scheme !== void 0 && t.scheme !== "machine-aesgcm-v1") throw new Error("protected HD requires an existing base/root wrap");
  for (const n of t.copies) {
    const r = xt(n);
    if (r.kind !== "passkey" || r.kdf !== "webauthn-prf-hkdf-sha256" || r.credential_id !== void 0 && (typeof r.credential_id != "string" || r.credential_id.length === 0)) throw new Error("invalid protected HD passkey copy");
    ir(r.prf_salt, [32]), ir(r.nonce, [12]), ir(r.wrapped, [48]);
  }
  return e;
}
function ih(e) {
  const t = xs(e);
  for (const n of ["scheme", "record_meta", "dek_nonce", "wrapped_dek"]) delete t[n];
  return me(t);
}
function dm(e, t, n) {
  const r = xt(e);
  Ut(r, ["envelope", "accounts", "expectedAccount0"]);
  const i = xt(r.envelope);
  if (Ut(i, ["ciphertext", "nonce", "wrap_meta"]), typeof i.ciphertext != "string" || typeof i.nonce != "string" || typeof i.wrap_meta != "string") throw new Error("invalid protected HD envelope");
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(t)) throw new Error("invalid protected HD name");
  const o = i, s = jn(o);
  if (s.recordMetadata?.kind !== "hd-seed" || ih(o.wrap_meta) !== ih(n)) throw new Error("protected HD result root mismatch");
  const a = ra({ kind: "hd-seed", profile: ms, accounts: r.accounts });
  if (me(a) !== me(s.recordMetadata)) throw new Error("protected HD result table mismatch");
  const c = xt(r.expectedAccount0);
  Ut(c, ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"]);
  const l = Ol(a.accounts[0]);
  if (me(c) !== me(l)) throw new Error("protected HD result account0 mismatch");
  return { envelope: { ...o }, accounts: a.accounts, expectedAccount0: l };
}
async function X3(e) {
  e.assertCurrent(), lm(e.wrapMeta);
  const t = Ay(e.words, e.passphrase);
  let n, r, i;
  try {
    const o = Tc(t.seed);
    e.assertCurrent();
    const s = { kind: "hd-seed", profile: ms, accounts: o }, a = la(e.name, s);
    n = $x(t);
    const c = await Ul(n, e.vmk, a);
    e.assertCurrent();
    const d = { envelope: Dl(c, e.wrapMeta, a), accounts: o, expectedAccount0: Ol(o[0]) };
    r = await li(c, e.vmk, a), e.assertCurrent(), i = Ey(r);
    const u = (h, y) => h.length === y.length && h.every((w, p) => w === y[p]);
    if (!u(i.entropy, t.entropy) || !u(i.seed, t.seed) || i.passphraseRequired !== t.passphraseRequired || me(Tc(i.seed)) !== me(o)) throw new Error("protected HD creation reopen mismatch");
    const f = dm(d, e.name, e.wrapMeta);
    return e.assertCurrent(), f;
  } finally {
    n?.fill(0), r?.fill(0), vs(i), vs(t);
  }
}
const um = 21e5;
function fm(e) {
  try {
    const t = xt(e);
    if (Ut(t, ["name", "mode", "wrapMeta"]), typeof t.name != "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(t.name) || t.mode !== "create" && t.mode !== "import") throw new Error("invalid protected HD name/mode");
    return { name: t.name, mode: t.mode, wrapMeta: lm(t.wrapMeta) };
  } catch {
    throw new _("invalid_payload", "sealHdWallet requires only name, create/import mode and an existing base root");
  }
}
function wr(e) {
  const t = e.deadlineAt - Date.now();
  if (!Number.isSafeInteger(e.deadlineAt) || t <= 0 || t > um) throw new _("authorization_window_timeout", "protected HD window expired", !0);
  return t;
}
function J3(e) {
  const t = xt(e);
  if (Ut(t, ["kind", "name", "mode", "wrapMeta", "deadlineAt"]), t.kind !== "protected-hd-wallet" || typeof t.deadlineAt != "number") throw new _("invalid_payload", "invalid protected HD window request");
  const r = { ...fm({ name: t.name, mode: t.mode, wrapMeta: t.wrapMeta }), kind: "protected-hd-wallet", deadlineAt: t.deadlineAt };
  return wr(r), Object.freeze(r);
}
function Ws(e, t) {
  try {
    const n = xt(e);
    if (Ut(n, ["kind", "envelope", "accounts", "expectedAccount0"]), n.kind !== "sealed-hd-wallet") throw new Error("invalid result kind");
    return wr(t), { kind: "sealed-hd-wallet", ...dm({ envelope: n.envelope, accounts: n.accounts, expectedAccount0: n.expectedAccount0 }, t.name, t.wrapMeta) };
  } catch (n) {
    throw n instanceof _ ? n : new _("authorization_result_invalid", "protected HD result is invalid");
  }
}
async function Q3(e, t, n) {
  const r = new AbortController(), i = () => t.reason instanceof Error ? t.reason : new _("authorization_cancelled", "protected HD operation cancelled", !0), o = () => r.abort(i());
  t.addEventListener("abort", o, { once: !0 }), t.aborted && o();
  const s = () => {
    if (r.signal.aborted) throw r.signal.reason;
    wr(e);
  };
  let a, c;
  const l = (d) => c ? Promise.reject(new _("authorization_result_invalid", "protected HD wallet is already being sealed")) : (c = (async () => {
    let u, f, h, y = [];
    try {
      s(), y = ca(e.wrapMeta);
      const w = await pa(y, Ft(), r.signal);
      u = w.prfOutput, f = w.prfSalt, s(), h = await Py(e.wrapMeta, { kind: "passkey", prfOutput: u, prfSalt: f }), s();
      const p = await X3({ name: e.name, words: d.words, passphrase: d.passphrase, vmk: h, wrapMeta: e.wrapMeta, assertCurrent: s });
      if (s(), me(p.accounts) !== me(d.reviewedAccounts)) throw new _("authorization_result_invalid", "protected HD reviewed account table changed");
      a = Ws({ kind: "sealed-hd-wallet", ...p }, e);
    } finally {
      u?.fill(0), f?.fill(0), h?.fill(0), y.forEach((w) => w.prfSalt.fill(0)), d.words = "", d.passphrase = "", d.reviewedAccounts = [];
    }
  })(), c);
  try {
    if (s(), await n(e, r.signal, l), c && await c, s(), !a) throw new _("authorization_result_invalid", "protected HD wallet was not sealed");
    return Ws(a, e);
  } catch (d) {
    throw r.abort(d), c && await c.catch(() => {
    }), d;
  } finally {
    t.removeEventListener("abort", o);
  }
}
const e6 = [12, 15, 18, 21, 24];
function Yc(e) {
  return e.normalize("NFKD").toLowerCase().trim().split(/\s+/u).filter(Boolean);
}
const t6 = new Set(ai);
function n6(e, t = 3) {
  for (let n = Math.min(4, e.length); n >= 1; n -= 1) {
    const r = e.slice(0, n), i = ai.filter((o) => o.startsWith(r));
    if (i.length > 0) return i.slice(0, t);
  }
  return [];
}
function oh(e) {
  const t = Yc(e);
  if (t.length === 0) return { ok: !1, reason: "empty" };
  const n = t.findIndex((r) => !t6.has(r));
  return n >= 0 ? { ok: !1, reason: "unknown-word", position: n + 1, word: t[n], suggestions: n6(t[n]) } : e6.includes(t.length) ? Ix(t.join(" "), ai) ? { ok: !0, words: t } : { ok: !1, reason: "checksum" } : { ok: !1, reason: "count", count: t.length };
}
function r6(e, t = Math.random) {
  const n = /* @__PURE__ */ new Set();
  for (; n.size < 3; ) n.add(1 + Math.floor(t() * e.length));
  const r = [...n].sort((c, l) => c - l), i = new Set(r.map((c) => e[c - 1])), o = new Set(i), s = (c) => {
    const l = c.filter((u) => !o.has(u));
    if (l.length === 0) return null;
    const d = l[Math.floor(t() * l.length)];
    return o.add(d), d;
  }, a = [...new Set(e)];
  return r.map((c) => {
    const l = e[c - 1], d = [];
    for (; d.length < 2; ) d.push(s(a) ?? s(ai));
    const u = [l, ...d];
    for (let f = u.length - 1; f > 0; f -= 1) {
      const h = Math.floor(t() * (f + 1));
      [u[f], u[h]] = [u[h], u[f]];
    }
    return { position: c, options: u, answer: l };
  });
}
const sh = (e, t) => e.every((n) => t[n.position] === n.answer);
async function i6(e, t) {
  if (!t()) return !1;
  try {
    if (navigator.clipboard?.writeText)
      return await navigator.clipboard.writeText(e), !0;
  } catch {
  }
  if (!t()) return !1;
  const n = document.createElement("textarea");
  n.value = e, n.setAttribute("readonly", ""), n.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
  const r = document.activeElement instanceof HTMLElement ? document.activeElement : null, i = document.getSelection?.(), o = i && i.rangeCount > 0 ? i.getRangeAt(0) : null;
  try {
    return document.body.append(n), n.focus(), n.select(), n.setSelectionRange(0, n.value.length), document.execCommand("copy") === !0;
  } catch {
    return !1;
  } finally {
    if (n.value = "", n.remove(), t())
      try {
        i?.removeAllRanges(), o && i?.addRange(o), r?.focus();
      } catch {
      }
  }
}
function o6() {
  const e = new Uint32Array(1);
  return crypto.getRandomValues(e), e[0] / 2 ** 32;
}
function s6(e, t = o6) {
  return r6(e, t);
}
function Z(e, t, n) {
  const r = document.createElement(e);
  return r.className = `${t} dynamic`, n !== void 0 && X(r, n), r;
}
function hm(e) {
  const t = document.createElement(e);
  return t.className = e === "textarea" ? "field textarea" : "field", t.setAttribute("autocomplete", "off"), t.setAttribute("autocapitalize", "off"), t.setAttribute("autocorrect", "off"), t.setAttribute("spellcheck", "false"), t instanceof HTMLInputElement && (t.type = "password"), t;
}
const Gs = "http://www.w3.org/2000/svg", a6 = ["M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0"], c6 = [
  "M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49",
  "M14.084 14.158a3 3 0 0 1-4.242-4.242",
  "M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143",
  "m2 2 20 20"
];
function l6(e) {
  const t = document.createElementNS(Gs, "svg"), n = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true"
  };
  for (const [r, i] of Object.entries(n)) t.setAttribute(r, i);
  for (const r of e ? c6 : a6) {
    const i = document.createElementNS(Gs, "path");
    i.setAttribute("d", r), t.append(i);
  }
  return t;
}
function d6(e) {
  const t = document.createElementNS(Gs, "svg");
  for (const [r, i] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" })) t.setAttribute(r, i);
  const n = document.createElementNS(Gs, "path");
  return n.setAttribute("d", e ? "m6 9 6 6 6-6" : "m9 18 6-6-6-6"), t.append(n), t;
}
let u6 = 0;
function uc(e) {
  const t = Z("div", "hd-field"), n = `hd-passphrase-${++u6}`, r = Z("label", "hd-field-label", e);
  r.htmlFor = n;
  const i = Z("div", "hd-secret-row"), o = hm("input");
  o.id = n;
  const s = document.createElement("button");
  s.type = "button", s.className = "hd-reveal", s.setAttribute("aria-controls", n);
  const a = Z("span", "hd-reveal-label"), c = (l) => {
    o.type = l ? "text" : "password", s.setAttribute("aria-pressed", String(l)), X(a, l ? "hd.hideValue" : "hd.showValue"), s.textContent = "", s.append(l6(l), a);
  };
  return s.addEventListener("click", () => c(o.type === "password")), c(!1), i.append(o, s), t.append(r, i), { wrap: t, input: o, toggle: s, hide: () => c(!1) };
}
function Lr(e, ...t) {
  const n = Z("div", `hd-notice hd-notice-${e}`);
  return n.setAttribute("role", e === "error" ? "alert" : "note"), t.forEach((r, i) => n.append(Z("span", i === 0 && t.length > 1 ? "hd-notice-lead" : "hd-notice-text", r))), n;
}
function fc(e) {
  const t = Z("ol", "hd-stepper");
  return pm(t, "hd.stepsLabel"), ["hd.stepSetup", "hd.stepBackup", "hd.stepCheck"].forEach((n, r) => {
    const i = r + 1, o = document.createElement("li");
    o.className = i < e ? "done" : i === e ? "current" : "", i === e && o.setAttribute("aria-current", "step");
    const s = document.createElement("span");
    s.className = "hd-step-dot", s.textContent = i < e ? "✓" : String(i);
    const a = document.createElement("span");
    a.className = "hd-step-name", X(a, n), o.append(s, a), t.append(o);
  }), t;
}
function pm(e, t) {
  const n = document.createElement("span");
  X(n, t), e.setAttribute("aria-label", n.textContent ?? "");
}
function ah(e, t, n) {
  const r = Z("div", "hd-fold"), i = document.createElement("button");
  i.type = "button", i.className = "hd-fold-header";
  const o = Z("span", "hd-fold-chevron");
  o.setAttribute("aria-hidden", "true");
  const s = Z("span", "hd-fold-title", e), a = Z("span", "hd-fold-hint");
  i.append(o, s, a);
  const c = Z("div", "hd-fold-body"), l = () => {
    i.setAttribute("aria-expanded", String(n)), o.textContent = "", o.append(d6(n)), c.hidden = !n, a.hidden = n, X(a, n ? _e("") : t());
  };
  return i.addEventListener("click", () => {
    n = !n, l();
  }), l(), r.append(i, c), { wrap: r, body: c, refresh: l };
}
function ch(e, t = {}) {
  const n = Z("ol", "hd-words");
  return pm(n, "hd.stepBackup"), t.hidden && (n.classList.add("hd-words-hidden"), n.setAttribute("aria-hidden", "true")), e.forEach((r, i) => {
    const o = document.createElement("li");
    t.bad === i && (o.className = "bad");
    const s = document.createElement("span");
    s.className = "hd-word-index", s.textContent = String(i + 1);
    const a = document.createElement("span");
    t.hidden ? X(a, "hd.hiddenWord") : a.textContent = r, o.append(s, a), n.append(o);
  }), n;
}
function f6(e) {
  const t = Ol(e), n = Z("div", "hd-addresses"), r = [
    ["hd.reviewEvm", t.eth],
    ["hd.reviewBitcoin", t.btc_taproot],
    ["hd.reviewSolana", t.solana]
  ];
  for (const [i, o] of r) {
    const s = document.createElement("div");
    s.className = "hd-address";
    const a = document.createElement("span");
    a.className = "hd-address-label", X(a, i);
    const c = document.createElement("span");
    c.className = "hd-address-value", c.textContent = o, s.append(a, c), n.append(s);
  }
  return n;
}
function lh(e, t) {
  const n = Ay(e, t);
  try {
    return Tc(n.seed);
  } finally {
    vs(n);
  }
}
const h6 = 250, p6 = (e, t, n) => new Promise((r, i) => {
  const o = [];
  let s = [], a = "", c = "", l = "", d = [], u = {}, f = [], h = !1, y = !1, w = 0;
  const p = () => {
    for (const S of o.splice(0)) S();
  }, g = (S, I) => (p(), w += 1, ya(S, I)), k = (S) => {
    if (!h) {
      h = !0, t.removeEventListener("abort", b), p(), s = [], a = "", c = "", l = "";
      for (const I of d)
        I.answer = "", I.options.fill("");
      d = [], u = {}, f = [], wa(), S();
    }
  }, b = () => k(() => i(t.reason instanceof Error ? t.reason : new Error("operation-cancelled")));
  if (t.aborted) {
    b();
    return;
  }
  t.addEventListener("abort", b, { once: !0 });
  const O = "hd.title", B = _e(e.name), R = (S, I, P = !1, C = !1) => {
    const N = w;
    S.addEventListener("click", () => {
      if (!(h || N !== w || y && !C))
        try {
          I();
        } catch (K) {
          k(() => i(K));
        }
    }, { once: P });
  }, H = (S, I) => {
    const P = w;
    S.addEventListener("input", () => {
      if (!(h || P !== w || y))
        try {
          I();
        } catch (C) {
          k(() => i(C));
        }
    });
  }, L = (S) => {
    const I = Ht(S);
    return I.classList.add("secondary"), I;
  }, T = () => {
    const S = L("common.cancel");
    return R(S, () => k(() => i(new Error("operation-cancelled"))), !0, !0), S;
  }, D = (S) => {
    o.push(() => {
      S.value = "";
    });
  }, z = (S) => {
    o.push(() => {
      S.textContent = "";
    });
  }, E = (S, I, P, C) => {
    if (y) return;
    y = !0;
    for (const K of ["input", "textarea", "button"])
      S.querySelectorAll(K).forEach((U) => {
        U !== I && (U.disabled = !0);
      });
    X(P, "hd.working"), n(C).then(() => k(r), (K) => k(() => i(K)));
  }, x = () => {
    const S = () => !!(a || c), I = ah("hd.passphraseTitle", () => S() ? "hd.passphraseSet" : "hd.passphraseFolded", S()), P = uc("hd.passphraseLabel"), C = uc("hd.passphraseAgain");
    P.input.value = a, C.input.value = c;
    for (const U of [P, C])
      D(U.input), o.push(U.hide);
    const N = Z("p", "hd-hint"), K = () => {
      const U = S() && a !== c;
      N.className = `hd-hint dynamic${U ? " hd-error" : S() ? " hd-ok" : ""}`, N.setAttribute("role", U ? "alert" : "status"), C.input.setAttribute("aria-invalid", String(U)), X(N, U ? "hd.passphraseMismatch" : S() ? "hd.passphraseMatch" : "hd.passphraseHint");
    };
    return I.body.append(Z("p", "hd-hint", "hd.passphraseBody"), P.wrap, C.wrap, N), { wrap: I.wrap, fields: [P, C], refresh: K };
  }, v = () => {
    const S = g(O, B);
    Q(S.card, fc(1)), Q(S.card, Z("p", "hd-hint", "hd.generateHint"));
    const I = x(), P = Ht("hd.generateCta"), C = () => {
      a = I.fields[0].input.value, c = I.fields[1].input.value, I.refresh(), P.disabled = a !== c;
    };
    I.fields.forEach((N) => H(N.input, C)), I.refresh(), P.disabled = a !== c, R(P, () => {
      if (a === c) {
        if (s.length === 0) {
          const N = new Uint8Array(16);
          try {
            crypto.getRandomValues(N), s = Ox(N).split(" ");
          } finally {
            N.fill(0);
          }
        }
        d.length === 0 && (d = s6(s)), m();
      }
    }), Q(S.card, I.wrap), Q(S.card, P), Q(S.card, T());
  }, m = () => {
    const S = g(O, B);
    Q(S.card, fc(2)), Q(S.card, Z("p", "hd-step-title", Ce("hd.backupTitle", { count: s.length }))), Q(S.card, Z("p", "hd-hint", "hd.backupBody"));
    const I = Z("div", "hd-backup");
    z(I);
    let P = !1;
    const C = (U) => {
      I.textContent = "";
      const M = Z("div", "hd-words-frame");
      if (M.append(ch(s, { hidden: U })), U) {
        const Y = Z("div", "hd-reveal-overlay"), pe = L(Ce("hd.backupShow", { count: s.length }));
        pe.classList.add("hd-inline"), R(pe, () => C(!1)), Y.append(pe, Z("span", "hd-reveal-note", "hd.backupScreen")), M.append(Y), I.append(M);
        return;
      }
      const F = Z("div", "hd-word-tools"), V = L(P ? "hd.backupCopied" : "hd.backupCopy");
      V.classList.add("hd-inline");
      const q = w;
      R(V, () => {
        i6(s.join(" "), () => !h && q === w).then((Y) => {
          !Y || h || q !== w || (P = !0, X(V, "hd.backupCopied"));
        }, () => {
        });
      });
      const W = L("hd.backupHide");
      W.classList.add("hd-inline"), R(W, () => C(!0)), F.append(V, W, Z("span", "hd-hint", "hd.backupClipboard")), I.append(M, F);
    };
    C(!0), Q(S.card, I), a && Q(S.card, Lr("warning", "hd.passphraseBackupReminder")), Q(S.card, Lr("note", "hd.backupAgentNote"));
    const N = L("hd.back");
    R(N, () => {
      u = {}, v();
    }, !0);
    const K = Ht("hd.backupDone");
    R(K, () => A(), !0), Q(S.card, K), Q(S.card, N), Q(S.card, T());
  }, A = () => {
    const S = g(O, B);
    f = lh(s.join(" "), a), Q(S.card, fc(3)), Q(S.card, Z("p", "hd-step-title", "hd.checkTitle")), Q(S.card, Z("p", "hd-hint", "hd.checkBody"));
    const I = Z("div", "hd-problem"), P = Ht("hd.createCta");
    P.classList.add("hd-gated");
    const C = () => {
      const M = d.find((F) => u[F.position] && u[F.position] !== F.answer);
      I.textContent = "", M && I.append(Lr("error", Ce("hd.checkWrong", { position: M.position }), Ce("hd.checkWrongHint", { position: M.position }))), P.disabled = y || !sh(d, u);
    };
    for (const M of d) {
      const F = Z("div", "hd-quiz");
      F.setAttribute("role", "radiogroup");
      const V = Z("span", "hd-field-label", Ce("hd.checkWord", { position: M.position }));
      V.id = `hd-quiz-${M.position}`, F.setAttribute("aria-labelledby", V.id), F.append(V);
      const q = M.options.map((W) => {
        const Y = document.createElement("button");
        return Y.type = "button", Y.className = "hd-option", Y.setAttribute("role", "radio"), Y.setAttribute("aria-checked", "false"), Y.textContent = W, R(Y, () => {
          u = { ...u, [M.position]: W }, q.forEach((pe) => {
            const se = pe === Y;
            pe.setAttribute("aria-checked", String(se)), pe.classList.remove("selected", "wrong"), se && pe.classList.add(W === M.answer ? "selected" : "wrong");
          }), C();
        }), Y;
      });
      F.append(...q), z(F), Q(S.card, F);
    }
    C();
    const N = Us(), K = L("hd.checkAgain"), U = T();
    R(K, () => {
      u = {}, f = [], m();
    }, !0), R(P, () => {
      P.disabled || !sh(d, u) || E(S.card, U, N, { words: s.join(" "), passphrase: a, reviewedAccounts: f });
    }), Q(S.card, I), Q(S.card, Z("p", "hd-hint", "hd.passkeyNote")), Q(S.card, N), Q(S.card, P), Q(S.card, K), Q(S.card, U);
  }, $ = () => {
    const S = g(O, B);
    Q(S.card, Z("p", "hd-hint", "hd.importHint"));
    const I = Z("label", "hd-field"), P = Z("span", "hd-field-label", "hd.importLabel"), C = hm("textarea");
    C.rows = 3;
    const N = document.createElement("span");
    X(N, "hd.importPlaceholder"), C.placeholder = N.textContent ?? "", C.value = l, D(C), I.append(P, C);
    const K = Z("div", "hd-import-words"), U = Z("div", "hd-import-verdict"), M = Z("div", "hd-import-passphrase"), F = Z("div", "hd-preview");
    for (const Ae of [K, U, F]) z(Ae);
    const V = ah("hd.importPassphrase", () => "hd.importPassphraseFolded", !!a), q = uc("hd.passphraseLabel");
    q.input.value = a, D(q.input), o.push(q.hide), V.body.append(q.wrap, Z("p", "hd-hint", "hd.importPassphraseHint")), M.append(V.wrap);
    const W = Us(), Y = Ht("hd.importCta"), pe = T();
    Y.classList.add("hd-gated");
    let se = oh(l), be = null, Ge, un = 0;
    o.push(() => {
      Ge !== void 0 && clearTimeout(Ge), Ge = void 0, un += 1, be = null;
    });
    const kr = () => se.ok ? { words: Sy(se.words.join(" ")), passphrase: a } : null, Tm = () => {
      U.textContent = "";
      const Ae = Yc(l);
      if (K.textContent = "", Ae.length > 0 ? K.append(ch(Ae, { bad: !se.ok && se.reason === "unknown-word" ? se.position - 1 : void 0 })) : K.append(Z("p", "hd-hint", "hd.importLocal")), se.ok)
        U.append(Z("p", "hd-hint hd-ok", Ce("hd.importOk", { count: se.words.length })));
      else if (se.reason === "unknown-word") {
        const fn = Lr(
          "error",
          Ce("hd.importUnknown", { position: se.position, word: se.word }),
          ...se.suggestions[0] ? [Ce("hd.importDidYouMean", { word: se.suggestions[0] })] : []
        );
        if (se.suggestions.length > 0) {
          const hn = Z("div", "hd-suggestions"), hi = se.position;
          for (const Rd of se.suggestions) {
            const Ba = L(Ce("hd.importUse", { word: Rd }));
            Ba.classList.add("hd-inline"), R(Ba, () => {
              l = Yc(l).map((Pm, $m) => $m === hi - 1 ? Rd : Pm).join(" "), C.value = l, Ia();
            }), hn.append(Ba);
          }
          fn.append(hn);
        }
        U.append(fn);
      } else se.reason === "checksum" ? U.append(Lr("error", Ce("hd.importChecksum", { count: Ae.length }), "hd.importChecksumHint")) : se.reason === "count" && U.append(Lr("error", Ce("hd.importCount", { count: se.count })));
      M.hidden = !se.ok;
    }, Od = () => {
      F.textContent = "";
      const Ae = kr();
      if (Y.disabled = y || !be || !Ae || be.words !== Ae.words || be.passphrase !== Ae.passphrase, !be || Y.disabled) return;
      const fn = Z("div", "hd-preview-box"), hn = Z("p", "hd-preview-title");
      hn.append(Z("span", "hd-account-index", Ce("hd.accountIndex", { account: 0 })), Z("span", "", "hd.importPreviewTitle")), fn.append(hn, f6(be.accounts[0]), Z("p", "hd-hint", "hd.btcTaprootInfo"), Z("p", "hd-hint", "hd.importPreviewHint")), F.append(fn);
    }, Cd = () => {
      Ge !== void 0 && clearTimeout(Ge), Ge = void 0, be = null, un += 1, Od();
      const Ae = kr();
      if (!Ae) return;
      const fn = un, hn = w;
      Ge = setTimeout(() => {
        if (Ge = void 0, !(h || hn !== w || fn !== un))
          try {
            const hi = lh(Ae.words, Ae.passphrase);
            if (h || hn !== w || fn !== un) return;
            be = { ...Ae, accounts: hi }, Od();
          } catch (hi) {
            k(() => i(hi));
          }
      }, h6);
    }, Ia = () => {
      se = oh(l), Tm(), Cd();
    };
    H(C, () => {
      l = C.value, Ia();
    }), H(q.input, () => {
      a = q.input.value, Cd();
    }), R(Y, () => {
      const Ae = kr();
      Y.disabled || !Ae || !be || be.words !== Ae.words || be.passphrase !== Ae.passphrase || E(S.card, pe, W, { words: Ae.words, passphrase: Ae.passphrase, reviewedAccounts: be.accounts });
    }), Ia(), Q(S.card, I), Q(S.card, K), Q(S.card, U), Q(S.card, M), Q(S.card, F), Q(S.card, Z("p", "hd-hint", "hd.passkeyNote")), Q(S.card, W), Q(S.card, Y), Q(S.card, pe);
  };
  try {
    e.mode === "create" ? v() : $();
  } catch (S) {
    k(() => i(S));
  }
}), ym = `avibe-vault-setup:v1:${ye.buildHash}:`, y6 = `avibe-vault-setup-result:${ye.buildHash}:`, w6 = `avibe-vault-setup-error:${ye.buildHash}:`, g6 = 300 * 1e3, m6 = 300 * 1e3, b6 = 30 * 1e3, v6 = 8 * 1e3, Me = new Lm();
let dh = !1;
function x6(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return (t.locale === "en" || t.locale === "zh") && (t.theme === "light" || t.theme === "dark");
}
function wm(e) {
  if (!x6(e))
    throw new _("invalid_payload", "appearance must include locale and theme");
  return e;
}
function k6(e) {
  return e == null ? null : wm(e);
}
function Ea(e) {
  const t = sk(e.locale);
  document.documentElement.setAttribute("data-theme", e.theme), t && dk();
}
function gm() {
  const e = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  return { locale: ak(), theme: e };
}
function Kt(e) {
  if (typeof e != "object" || e === null)
    throw new _("invalid_payload", "request payload must be an object");
  return e;
}
function mm(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new _("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Xe(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new _("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function bm(e, t) {
  if (typeof e != "object" || e === null) throw new _("invalid_payload", `${t} must be an object`);
  return e;
}
function E6(e) {
  const t = bm(e, "envelope");
  return {
    ciphertext: Xe(t.ciphertext, "envelope.ciphertext"),
    nonce: Xe(t.nonce, "envelope.nonce"),
    wrap_meta: Xe(t.wrap_meta, "envelope.wrap_meta")
  };
}
function S6(e) {
  const t = bm(e, "material");
  return { name: Xe(t.name, "material.name"), envelope: E6(t.envelope) };
}
function A6(e) {
  const t = e == null ? {} : Kt(e);
  return { wrapMeta: mm(t.wrapMeta, "wrapMeta") };
}
function vm(e) {
  const t = Kt(e), n = !!t.existingProtectedVault;
  if (n && t.rootMetadata !== void 0)
    throw new _("invalid_payload", "vault root metadata cannot be replaced during setup");
  return {
    vaultUserHandle: Xe(t.vaultUserHandle, "vaultUserHandle"),
    displayName: Xe(t.displayName, "displayName"),
    existingProtectedVault: n,
    authzCreationOptions: t.authzCreationOptions,
    ...t.rootMetadata !== void 0 ? { rootMetadata: t.rootMetadata } : {},
    ...t.policy !== void 0 ? { policy: ql(t.policy) } : {}
  };
}
function _6(e) {
  const t = Kt(e);
  return {
    wrapMeta: Xe(t.wrapMeta, "wrapMeta"),
    ...t.policy !== void 0 ? { policy: ql(t.policy) } : {}
  };
}
function I6(e) {
  const t = Kt(e);
  if (!Array.isArray(t.items) || t.items.length === 0)
    throw new _("invalid_payload", "approveRelease requires a non-empty items array");
  return { items: t.items.map((n) => U3(n, S6)) };
}
function B6(e) {
  const t = Kt(e);
  if (t.operation !== "delete_secret") throw new _("invalid_payload", "deleteAuthzAssertion only supports delete_secret");
  return {
    challengeId: Xe(t.challengeId, "challengeId"),
    operation: "delete_secret",
    secretName: Xe(t.secretName, "secretName"),
    webauthn: t.webauthn
  };
}
function xm() {
  if (typeof crypto.randomUUID == "function") return crypto.randomUUID();
  const e = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(e, (t) => t.toString(16).padStart(2, "0")).join("");
}
async function uh(e) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(e)));
}
function T6(e) {
  return new Promise((t, n) => {
    const r = () => {
      n(xr(e));
    };
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function P6(e) {
  for (const t of e)
    if (t.aborted) return xr(t);
  return new Error("operation-superseded");
}
function Td(e) {
  const t = new AbortController(), n = [], r = (i) => {
    t.signal.aborted || t.abort(xr(i));
  };
  for (const i of e) {
    if (i.aborted) {
      r(i);
      continue;
    }
    const o = () => r(i);
    i.addEventListener("abort", o, { once: !0 }), n.push(() => i.removeEventListener("abort", o));
  }
  return { signal: t.signal, cleanup: () => n.forEach((i) => i()) };
}
async function km(e) {
  const t = Td(e.abortSignals);
  try {
    await Promise.race([
      tk(ca(e.wrapMeta), Ft(), e.challenge, t.signal).catch((n) => {
        throw t.signal.aborted ? P6(e.abortSignals) : n;
      }),
      ...e.abortSignals.map((n) => T6(n))
    ]);
  } finally {
    t.cleanup();
  }
}
async function Sa(e) {
  await mr(async (t) => {
    const n = async (i) => {
      const o = Td([t, ...e.abortSignal ? [e.abortSignal] : []]);
      try {
        const s = await Pd(
          {
            prompt: fh(e),
            wrapMeta: e.wrapMeta,
            passkey: e.passkey
          },
          o.signal,
          i
        );
        if (e.passkey === "unlock") {
          if (s.kind !== "unlock") throw new Error("authorization unlock result is missing");
          await ww({
            wrapMeta: e.wrapMeta,
            currentRpId: Ft(),
            assertion: {
              prfOutput: Ee(s.prfOutput),
              prfSalt: Ee(s.prfSalt)
            },
            abortSignal: o.signal,
            ...e.policy ? { policy: e.policy } : {},
            ...e.onUnlocked ? { onCommitted: e.onUnlocked } : {}
          });
        } else if (s.kind !== "confirmed")
          throw new Error("authorization result is invalid");
      } finally {
        o.cleanup();
      }
    }, r = e.authorizationWindow?.();
    if (r) {
      await n(r);
      return;
    }
    await Ki(nm(fh(e), "common.continue"), t, void 0, n);
  });
}
function fh(e) {
  return {
    title: e.title,
    subtitle: e.subtitle,
    ...e.body ? { body: e.body } : {},
    ...e.details ? { details: e.details } : {},
    ...e.review ? { review: e.review } : {},
    ...e.reviewSession !== void 0 ? { reviewSession: e.reviewSession } : {},
    ...e.challenge ? { challenge: e.challenge } : {},
    label: e.label
  };
}
function $6(e) {
  if ("kind" in e) return e;
  const t = I3(e.prompt);
  return Tg(t), { ...e, prompt: t };
}
function O6(e) {
  const t = Kt(e);
  if (!t) throw new _("invalid_payload", "authorization request must be an object");
  if (t.kind === "protected-hd-wallet") return J3(t);
  if (t.kind === "signing-policy-downgrade") {
    if (Object.keys(t).length !== 2 || !Object.hasOwn(t, "context"))
      throw new _("invalid_payload", "policy authorization request has unknown fields");
    return {
      kind: "signing-policy-downgrade",
      context: am(t.context, {
        origin: window.location.origin,
        hostname: window.location.hostname
      })
    };
  }
  if (t.kind !== void 0) throw new _("invalid_payload", "authorization request kind is invalid");
  const n = t.passkey;
  if (n !== "none" && n !== "unlock" && n !== "uv")
    throw new _("invalid_payload", "authorization passkey requirement is invalid");
  return Tg(t.prompt), {
    prompt: P3(t.prompt),
    wrapMeta: Xe(t.wrapMeta, "wrapMeta"),
    passkey: n
  };
}
function C6(e) {
  return wh("authorize", e, gm());
}
function R6(e, t) {
  const n = Kt(e);
  if (!n) throw new _("authorization_result_invalid", "authorization result is invalid");
  if ("kind" in t) {
    if (t.kind === "protected-hd-wallet") return Ws(n, t);
    if (t.kind !== "signing-policy-downgrade")
      throw new _("authorization_result_invalid", "authorization request kind is invalid");
    if (n.kind !== "assertion" || !Object.hasOwn(n, "assertion"))
      throw new _("authorization_result_invalid", "policy authorization result kind is invalid");
    return { kind: "assertion", assertion: n.assertion };
  }
  if (n.kind === "confirmed") return { kind: "confirmed" };
  if (n.kind === "unlock")
    return {
      kind: "unlock",
      prfOutput: Xe(n.prfOutput, "result.prfOutput"),
      prfSalt: Xe(n.prfSalt, "result.prfSalt")
    };
  throw new _("authorization_result_invalid", "authorization result kind is invalid");
}
function Pd(e, t, n) {
  lr(t);
  const r = "kind" in e && e.kind === "protected-hd-wallet" ? e : void 0;
  r && wr(r);
  const i = n ?? xm(), o = `avibe-vault-authorization-${i}`;
  return new Promise((s, a) => {
    let c = null, l = !1, d = null, u = null, f = null, h = null;
    const y = () => b(() => a(xr(t))), w = () => b(() => a(new _("authorization_cancelled", "requesting sandbox was unloaded", !0))), p = () => new _("authorization_window_closed", "authorization window was closed", !0), g = (O) => {
      c = O, u = setInterval(() => {
        !O.closed || f || (f = setTimeout(() => b(() => a(p())), 2500));
      }, 500);
    }, k = (O) => {
      if (O.origin !== window.location.origin) return;
      const B = O.data;
      if (!(!B || B.id !== i || !Hr(B.build)) && (!c && n && B.type === "authorization-ready" && O.source && (h && clearTimeout(h), h = null, g(O.source)), O.source === c))
        if (B.type === "authorization-ready")
          try {
            c?.postMessage(
              {
                type: "authorization-request",
                build: ye,
                id: i,
                request: $6(e)
              },
              window.location.origin
            );
          } catch (R) {
            b(() => a(R));
          }
        else if (B.type === "authorization-approved")
          try {
            const R = R6(B.result, e);
            b(() => s(R));
          } catch (R) {
            b(() => a(R));
          }
        else B.type === "authorization-error" && b(() => a(new _(B.code, B.message, !!B.retryable)));
    }, b = (O) => {
      if (!l) {
        l = !0, d && clearTimeout(d), u && clearInterval(u), f && clearTimeout(f), h && clearTimeout(h), window.removeEventListener("message", k), window.removeEventListener("pagehide", w), t?.removeEventListener("abort", y);
        try {
          c && !c.closed && c.close();
        } catch {
        }
        O();
      }
    };
    if (window.addEventListener("message", k), window.addEventListener("pagehide", w, { once: !0 }), n)
      h = setTimeout(() => b(() => a(p())), b6);
    else {
      const O = window.open(C6(i), o, "popup,width=520,height=760");
      if (!O) {
        b(() => a(new _("authorization_popup_blocked", "authorization window was blocked", !0)));
        return;
      }
      O.focus(), g(O);
    }
    if (d = setTimeout(
      () => b(() => a(new _("authorization_window_timeout", "authorization window timed out", !0))),
      r ? wr(r) : m6
    ), t) {
      if (t.aborted) {
        y();
        return;
      }
      t.addEventListener("abort", y, { once: !0 });
    }
  });
}
function L6(e) {
  return e === 0 ? "context.oneTime" : e % 60 === 0 ? Ce("context.minutes", { count: e / 60 }) : Ce("context.seconds", { count: e });
}
function Yi(e, t, n = "access") {
  const r = [];
  e.sessionLabel && r.push({ label: "context.session", value: _e(e.sessionLabel) }), e.command && r.push({ label: "context.command", value: _e(e.command), variant: "command" }), e.egress && r.push({ label: n === "sign" ? "context.signReturns" : "context.egress", value: _e(e.egress) });
  const i = [
    ...e.source?.env?.map((o) => _e(`${J(n === "sign" ? "context.sourceRequestedName" : "context.sourceEnv")}: ${o}`)) ?? [],
    ...e.source?.tags?.map((o) => _e(`${J("context.sourceTag")}: ${o}`)) ?? [],
    ...e.source?.skills?.map((o) => _e(`${J("context.sourceSkill")}: ${o}`)) ?? []
  ];
  return i.length > 0 && r.push({ label: "context.source", values: i, variant: "chips" }), t?.agentFingerprint && r.push({ label: "context.agent", value: _e(t.agentFingerprint), variant: "mono" }), t?.grantId && r.push({ label: "context.grant", value: _e(t.grantId), variant: "mono" }), e.grantTtlSeconds !== void 0 && r.push({ label: "context.agentAccess", value: L6(e.grantTtlSeconds) }), r.push({
    label: "context.secrets",
    values: e.secrets.map(
      (o) => _e(`${o.name} · ${J(o.kind === "static" ? "context.kindStatic" : "context.kindKeypair")}`)
    ),
    variant: "chips",
    tone: "warning"
  }), { rows: r };
}
function H6(e) {
  return e instanceof Error && e.message === "vault-locked";
}
function N6(e) {
  return e instanceof Error && e.message === "vault-operation-aborted";
}
function Em(e, t) {
  return H6(e) ? !0 : N6(e) && kt(t).state === "locked";
}
async function U6(e, t = ze(), n) {
  await yw({ wrapMeta: e, currentRpId: Ft(), policy: t, abortSignal: n });
}
async function D6(e, t = ze()) {
  await mr((n) => U6(e, t, n));
}
async function $d(e) {
  let t;
  try {
    await Sa({
      ...e,
      passkey: "unlock",
      ...e.abortSignal ? { onUnlocked: (n) => {
        t = n, e.onUnlocked?.(n);
      } } : {}
    });
  } catch (n) {
    throw e.abortSignal ? (t?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), t?.release()) : kt(e.wrapMeta).state === "unlocked" && ft({ broadcast: !0, reason: "manual-lock" }), n;
  } finally {
    e.onUnlocked || t?.release();
  }
}
async function z6(e, t) {
  if (kt(e).state !== "unlocked") {
    if (!e) throw new Error("vault-locked");
    await D6(e);
  }
  return await fa(t, { renewOnSuccess: !0, ...e ? { expectedWrapMeta: e } : {} });
}
async function Sm(e) {
  const t = kt(e.bindRoot ? void 0 : e.wrapMeta).state, n = ze(), r = im({ tier: e.tier, vaultState: t, policy: n });
  let i = !1, o;
  const s = async () => {
    const c = e.bindRoot ? ua(e.wrapMeta) : void 0;
    try {
      await $d({
        ...e.lockedPrompt,
        wrapMeta: e.wrapMeta,
        policy: n,
        ...c ? { abortSignal: c.signal, onUnlocked: (l) => {
          o = l;
        } } : {},
        parentSurface: e.parentSurface,
        authorizationWindow: e.authorizationWindow
      }), i = !0;
    } finally {
      c?.release();
    }
  }, a = async (c, l = !1) => {
    let d = !1;
    try {
      o?.assertCurrent();
      const u = await fa(
        async (f, h, y) => {
          const w = await e.buildPrompt(f, h, y);
          return r.confirm && !l && (await Sa({
            ...w,
            wrapMeta: h,
            passkey: c,
            abortSignal: y.signal,
            parentSurface: e.parentSurface,
            authorizationWindow: e.authorizationWindow
          }), y.assertCurrent()), e.operation(f, h, y);
        },
        {
          renewOnSuccess: r.renewOnSuccess,
          beforeSuccess: e.beforeSuccess,
          ...e.bindRoot ? { expectedWrapMeta: e.wrapMeta } : {}
        }
      );
      return d = !0, u;
    } catch (u) {
      throw !e.bindRoot && i && !d && kt().state === "unlocked" && ft({ broadcast: !0, reason: "manual-lock" }), u;
    }
  };
  try {
    return r.passkey === "unlock" ? (await s(), await a("none", !0)) : await a(r.passkey === "uv" ? "uv" : "none");
  } catch (c) {
    if (!e.bindRoot && r.passkey !== "unlock" && Em(c, e.wrapMeta))
      return await s(), await a("none", !0);
    throw e.bindRoot && o?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), c;
  } finally {
    o?.release();
  }
}
function it(e, t) {
  if (e instanceof _) return e;
  if (aw(e)) return new _("passkey_cancelled", "passkey-cancelled", !0);
  const n = e instanceof Error ? e.message : t;
  switch (n) {
    case "passkey-cancelled":
      return new _("passkey_cancelled", n, !0);
    case "passkey-prf-unavailable":
      return new _("passkey_prf_unavailable", n);
    case "passkey-not-configured":
      return new _("passkey_not_configured", n);
    case "passkey-multiple-not-supported":
      return new _("passkey_multiple_not_supported", n);
    case "vault-operation-aborted":
      return new _("vault_operation_aborted", n, !0);
    default:
      return new _(t, n);
  }
}
function M6(e) {
  return le(new TextEncoder().encode(JSON.stringify(e))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function K6(e) {
  return vm(JSON.parse(new TextDecoder().decode(Ee(e))));
}
function F6(e, t) {
  return wh("setup", e, gm(), { req: M6(t) });
}
function hc(e) {
  const t = Kt(e);
  return {
    credentialId: Xe(t.credentialId, "credentialId"),
    ...t.authzRegistration !== void 0 ? { authzRegistration: t.authzRegistration } : {}
  };
}
function Aa(e) {
  return `${y6}${e}`;
}
function _a(e) {
  return `${w6}${e}`;
}
function hh(e) {
  try {
    localStorage.removeItem(Aa(e)), localStorage.removeItem(_a(e));
  } catch {
  }
}
function j6(e, t) {
  try {
    localStorage.setItem(Aa(e), JSON.stringify({ build: ye, result: t })), localStorage.removeItem(_a(e));
  } catch {
  }
}
function V6(e, t) {
  try {
    localStorage.setItem(
      _a(e),
      JSON.stringify({ build: ye, code: t.code, message: t.message, retryable: t.retryable })
    ), localStorage.removeItem(Aa(e));
  } catch {
  }
}
function Am(e) {
  if (window.self === window.top)
    return Xl({
      rpId: Ft(),
      vaultUserHandle: e.vaultUserHandle,
      displayName: e.displayName,
      authzCreationOptions: e.authzCreationOptions
    });
  const t = xm(), n = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${ym}${t}`);
  let r = null;
  return hh(t), new Promise((i, o) => {
    let s = !1, a = null, c = null, l = null;
    const d = () => u(() => o(new _("setup_window_closed", "requesting sandbox was unloaded", !0))), u = (w) => {
      if (!s) {
        s = !0, a && clearTimeout(a), c && clearInterval(c), l && clearTimeout(l), window.removeEventListener("visibilitychange", h), window.removeEventListener("focus", h), window.removeEventListener("storage", h), window.removeEventListener("message", y), window.removeEventListener("pagehide", d), n?.close(), hh(t);
        try {
          r?.close();
        } catch {
        }
        w();
      }
    }, f = () => {
      let w = null, p = null;
      try {
        w = localStorage.getItem(Aa(t)), p = localStorage.getItem(_a(t));
      } catch {
        return !1;
      }
      if (w) {
        try {
          const g = Kt(JSON.parse(w));
          if (!Hr(g.build)) throw new _("sandbox_build_mismatch");
          const k = hc(g.result);
          u(() => i(k));
        } catch (g) {
          const k = g instanceof Error ? g.message : "stored setup result is invalid";
          u(() => o(new _("setup_storage_invalid", k)));
        }
        return !0;
      }
      if (p) {
        try {
          const g = Kt(JSON.parse(p));
          if (!Hr(g.build)) throw new _("sandbox_build_mismatch");
          u(
            () => o(new _(Xe(g.code, "code"), mm(g.message, "message"), !!g.retryable))
          );
        } catch (g) {
          const k = g instanceof Error ? g.message : "stored setup error is invalid";
          u(() => o(new _("setup_storage_invalid", k)));
        }
        return !0;
      }
      return !1;
    }, h = () => {
      f();
    }, y = (w) => {
      if (w.origin !== window.location.origin || w.source !== r) return;
      const p = w.data;
      !p || p.id !== t || !Hr(p.build) || (p.type === "setup-created" ? u(() => i(hc(p.result))) : p.type === "setup-error" && u(() => o(new _(p.code, p.message, !!p.retryable))));
    };
    if (a = setTimeout(() => {
      f() || u(() => o(new _("setup_window_timeout", "setup window timed out", !0)));
    }, g6), c = setInterval(() => {
      !r?.closed || l || f() || (l = setTimeout(() => {
        f() || u(() => o(new _("setup_window_closed", "setup window was closed", !0)));
      }, 2500));
    }, 500), n && (n.onmessage = (w) => {
      const p = w.data;
      !p || p.id !== t || !Hr(p.build) || (p.type === "setup-created" ? u(() => i(hc(p.result))) : p.type === "setup-error" && u(() => o(new _(p.code, p.message, !!p.retryable))));
    }), window.addEventListener("visibilitychange", h), window.addEventListener("focus", h), window.addEventListener("storage", h), window.addEventListener("message", y), window.addEventListener("pagehide", d, { once: !0 }), r = window.open(F6(t, e), "avibe-vault-setup", "popup,width=440,height=620"), !r) {
      u(() => o(new _("setup_popup_blocked", "setup window was blocked", !0)));
      return;
    }
    r.focus();
  });
}
function xr(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function lr(e) {
  if (e?.aborted) throw xr(e);
}
function q6() {
  const e = window.navigator;
  return /iP(?:hone|ad|od)/.test(e.userAgent) || e.platform === "MacIntel" && e.maxTouchPoints > 1;
}
function ph(e) {
  return q6() && sw(e);
}
async function _m(e, t, n, r, i = ze()) {
  const o = Gx();
  let s, a;
  try {
    lr(r), s = (await pa([{ credentialId: n.credentialId, prfSalt: o }], t, r)).prfOutput, lr(r), a = Wx();
    const l = await Xx(a, [{ kind: "passkey", prfOutput: s, prfSalt: o, credentialId: n.credentialId }]);
    lr(r);
    let d = Qx(l, {
      rpId: t,
      vaultUserHandle: e.vaultUserHandle
    });
    e.rootMetadata && (d = n2(d, await t2(a, e.rootMetadata, d)), lr(r));
    const u = Qy({
      vmk: a,
      wrapMeta: d,
      freshSetup: !e.existingProtectedVault,
      scopeId: N2(e.vaultUserHandle),
      policy: i
    });
    return a = void 0, {
      wrapMeta: d,
      rpId: t,
      credentialId: n.credentialId,
      state: u.state,
      expiresAt: u.expiresAt,
      ...n.authzRegistration ? { authzRegistration: n.authzRegistration } : {}
    };
  } finally {
    s?.fill(0), o.fill(0), a?.fill(0);
  }
}
function W6(e, t, n) {
  return mr((r) => {
    const i = ya(
      "setup.title",
      "setup.subtitle",
      "setup.iframeBody"
    ), o = Ht("setup.createPasskey"), s = Us("setup.ready");
    return Q(i.card, o), Q(i.card, s), new Promise((a, c) => {
      let l = !1, d = "create", u = null, f = !1;
      const h = (B) => {
        l || (l = !0, r.removeEventListener("abort", p), wa(), B());
      }, y = (B) => {
        h(() => c(B));
      }, w = (B) => {
        l || (d = "create", u = null, X(o, "setup.createPasskey"), o.disabled = !1, X(s, B));
      }, p = () => {
        y(xr(r));
      }, g = (B) => {
        if (B instanceof _ && B.code === "setup_popup_blocked") {
          w("setup.popupBlocked");
          return;
        }
        y(B);
      }, k = () => {
        f = !0, o.disabled = !0, X(s, "setup.popupPrompt");
        let B;
        try {
          B = Am(e);
        } catch (R) {
          g(R);
          return;
        }
        B.then(
          (R) => {
            l || (u = R, d = "finish", X(o, "setup.finish"), o.disabled = !1, X(s, "setup.createdFinish"));
          },
          (R) => {
            g(R);
          }
        );
      }, b = (B) => {
        o.disabled = !0, X(s, "setup.passkeyPrompt"), _m(e, t, B, r, n).then(
          (R) => h(() => a(R)),
          (R) => y(R)
        );
      }, O = () => {
        o.disabled = !0, X(s, "setup.passkeyPrompt");
        let B;
        try {
          B = Xl({
            rpId: t,
            vaultUserHandle: e.vaultUserHandle,
            displayName: e.displayName,
            authzCreationOptions: e.authzCreationOptions
          });
        } catch (R) {
          if (ph(R)) {
            k();
            return;
          }
          y(R);
          return;
        }
        B.then(
          (R) => b(R),
          (R) => {
            if (ph(R)) {
              k();
              return;
            }
            y(R);
          }
        );
      };
      if (r.aborted) {
        p();
        return;
      }
      r.addEventListener("abort", p, { once: !0 }), o.addEventListener("click", () => {
        if (!(l || o.disabled)) {
          if (d === "finish" && u) {
            b(u);
            return;
          }
          if (f) {
            k();
            return;
          }
          O();
        }
      });
    });
  });
}
async function G6(e) {
  const t = vm(e);
  try {
    const n = Ft(), r = t.policy ?? ze(), i = window.self !== window.top ? await W6(t, n, r) : await _m(t, n, await Am(t), void 0, r), o = t.policy ? Wl(r) : ze();
    return { ...i, policy: o };
  } catch (n) {
    throw it(n, "setup_failed");
  }
}
async function Z6(e) {
  const t = _6(e);
  try {
    const n = Ft(), r = t.policy ?? ze(), i = await mr(async (s) => {
      let a;
      if (await Ki(
        {
          title: "unlock.sessionTitle",
          subtitle: Ce("unlock.sessionSubtitle", {
            minutes: r.windowSeconds / 60
          }),
          body: "unlock.sessionBody",
          confirmLabel: "common.continue"
        },
        s,
        void 0,
        async () => {
          a = await yw({ wrapMeta: t.wrapMeta, currentRpId: n, abortSignal: s, policy: r });
        }
      ), !a) throw new Error("vault unlock did not complete");
      return a;
    }), o = t.policy ? Wl(r) : ze();
    return { ...i, policy: o };
  } catch (n) {
    throw it(n, "unlock_failed");
  }
}
async function Y6(e) {
  const t = Pk(e);
  t.wrapMeta && Yl(t.wrapMeta);
  try {
    if (t.kind === "static" && !ze().parentValueSealAllowed)
      throw new _("parent_value_seal_disabled", "parent-value sealing is disabled by policy");
    return await z6(t.wrapMeta, async (n, r) => ({ ...t.kind === "static" ? await H3({ name: t.name, value: t.value, vmk: n, wrapMeta: r }) : await N3({ name: t.name, curve: t.curve, vmk: n, wrapMeta: r }), establishingVmk: Cc() }));
  } catch (n) {
    throw it(n, "seal_failed");
  }
}
async function X6(e, t) {
  try {
    const n = fm(e), r = t.takeAuthorizationWindow();
    if (!r) throw new _("invalid_payload", "sealHdWallet requires a parent-opened authorization window");
    const i = ua(n.wrapMeta, { surviveAutoLock: !0 });
    let o = !1;
    const s = { ...n, kind: "protected-hd-wallet", deadlineAt: Date.now() + um };
    try {
      const a = await mr(async (c) => {
        const l = Td([c, i.signal]);
        try {
          i.assertCurrent();
          const d = await Pd(s, l.signal, r);
          i.assertCurrent();
          const u = Ws(d, s);
          return i.assertCurrent(), { envelope: u.envelope, accounts: u.accounts, expectedAccount0: u.expectedAccount0, establishingVmk: i.freshSetup };
        } finally {
          l.cleanup();
        }
      });
      return o = !0, a;
    } finally {
      i.release({ completed: o });
    }
  } catch (n) {
    throw it(n, "seal_hd_failed");
  }
}
async function J6(e, t) {
  const n = Y3(e);
  try {
    const { sealed: r, vmkWrapMeta: i, recordMetadata: o } = jn(n.material.envelope);
    if (!Bd(o)) throw new Error("only static protected records can be revealed as plaintext");
    return zs({
      context: n.context,
      expectedPurpose: "reveal",
      secret: { name: n.material.name, kind: "static" }
    }), await Sm({
      tier: "R2",
      wrapMeta: i,
      bindRoot: !0,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: {
        title: "reveal.showConfirmTitle",
        subtitle: _e(n.material.name),
        body: "reveal.confirmBody",
        details: Yi(n.context.display),
        label: "common.continue"
      },
      buildPrompt: async (s, a) => {
        const c = await Is(a, s);
        return sr({ context: n.context, rootMetadata: c, expectedPurpose: "reveal" }), Jo(n), zs({
          context: n.context,
          expectedPurpose: "reveal",
          secret: { name: n.material.name, kind: "static" }
        }), ti([n.context]), {
          title: "reveal.showConfirmTitle",
          subtitle: _e(n.material.name),
          body: "reveal.confirmBody",
          details: Yi(n.context.display),
          challenge: await uh(ga(n.context)),
          label: "common.continue"
        };
      },
      operation: async (s, a, c) => {
        const l = await Is(a, s);
        c.assertCurrent(), sr({ context: n.context, rootMetadata: l, expectedPurpose: "reveal" }), Jo(n), ti([n.context]);
        const d = await li(r, s, la(n.material.name, o));
        try {
          c.assertCurrent(), sr({ context: n.context, rootMetadata: l, expectedPurpose: "reveal" }), Jo(n), await Fi([n.context]), c.assertCurrent(), sr({ context: n.context, rootMetadata: l, expectedPurpose: "reveal" }), await _k({
            name: n.material.name,
            plaintext: d,
            abortSignal: c.signal,
            prepareCopy: ze().strictApprovals ? async (u) => {
              await km({
                wrapMeta: a,
                challenge: await uh(`reveal-copy:${n.context.requestId}:${n.material.name}`),
                abortSignals: [u, c.signal]
              }), lr(u), lr(c.signal);
            } : void 0
          });
        } finally {
          d.fill(0);
        }
        return { completed: !0 };
      }
    });
  } catch (r) {
    throw it(r, "reveal_failed");
  }
}
async function Q6(e, t) {
  try {
    const n = _3(e), { request: r, unpacked: { vmkWrapMeta: i } } = n, o = (s) => {
      const a = r.context.display.sessionLabel;
      if (!s.review && s.display === void 0) throw new _("invalid_payload", "signing request has no reviewable content");
      return {
        title: "sign.title",
        subtitle: _e(r.material.name),
        ...s.review ? { review: s.review, ...a ? { reviewSession: a } : {} } : { body: _e(s.display ?? "") },
        details: Yi(r.context.display, void 0, "sign"),
        label: "sign.confirm"
      };
    };
    if (n.hd || qn(r.signingContext)) {
      let s;
      try {
        if (kt().state !== "unlocked") {
          const a = ua(i);
          try {
            await $d({
              title: n.hd ? "hd.unlockTitle" : "unlock.sessionTitle",
              subtitle: _e(r.material.name),
              body: n.hd ? "hd.unlockBody" : "unlock.sessionBody",
              label: "common.continue",
              wrapMeta: i,
              policy: ze(),
              abortSignal: a.signal,
              onUnlocked: (c) => {
                s = c;
              },
              parentSurface: t.latestSurface,
              authorizationWindow: t.takeAuthorizationWindow
            });
          } finally {
            a.release();
          }
        }
        return s?.assertCurrent(), await fa(async (a, c, l) => {
          const d = await Wf(n, a, c);
          return l.assertCurrent(), await Sa({
            ...o(d),
            wrapMeta: c,
            passkey: "uv",
            challenge: new Uint8Array(n.challenge),
            abortSignal: l.signal,
            parentSurface: t.latestSurface,
            authorizationWindow: t.takeAuthorizationWindow
          }), l.assertCurrent(), Gf(n, a, c, l.assertCurrent);
        }, { renewOnSuccess: !1, expectedWrapMeta: i, beforeSuccess: () => Fi([r.context]) });
      } catch (a) {
        throw s?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), a;
      } finally {
        s?.release();
      }
    }
    return await Sm({
      tier: "R3",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: o(n.verified),
      // Shown only after the daemon signature, expiry and replay state are
      // verified with the vault key, from content re-derived at that moment.
      buildPrompt: async (s, a) => ({
        ...o(await Wf(n, s, a)),
        challenge: new Uint8Array(n.challenge)
      }),
      operation: (s, a) => Gf(n, s, a),
      beforeSuccess: () => Fi([r.context])
    });
  } catch (n) {
    throw it(n, "sign_failed");
  }
}
async function e5(e, t) {
  const n = I6(e);
  try {
    const i = jn(n.items[0].material.envelope).vmkWrapMeta;
    for (const f of n.items) {
      const h = jn(f.material.envelope);
      if (h.vmkWrapMeta !== i) throw new _("invalid_payload", "approveRelease items must share one VMK context");
      if (!Bd(h.recordMetadata)) throw new _("invalid_payload", "approveRelease only supports static protected records");
    }
    const o = om(n.items), s = kt(i).state, a = ze(), c = im({ tier: "R2", vaultState: s, policy: a });
    let l = !1;
    const d = async () => {
      await $d({
        title: "release.title",
        subtitle: Ce("release.subtitle", { count: n.items.length }),
        details: Yi(o.display, o.recipient),
        label: "release.confirm",
        wrapMeta: i,
        policy: a,
        parentSurface: t.latestSurface,
        authorizationWindow: t.takeAuthorizationWindow
      }), l = !0;
    }, u = async (f, h = !1) => {
      let y = !1, w;
      try {
        const p = await fa(
          async (g, k, b) => {
            const O = await D3({
              items: n.items,
              vmk: g,
              wrapMeta: k,
              consumeReplayIds: !1,
              onApprovalAccepted: (B) => {
                w = B;
              },
              confirm: (B) => !c.confirm || h ? Promise.resolve() : Sa({
                title: "release.title",
                subtitle: Ce("release.subtitle", { count: n.items.length }),
                details: Yi(B.display, B.recipient),
                challenge: B.challenge,
                label: "release.confirm",
                wrapMeta: k,
                passkey: f,
                abortSignal: b.signal,
                parentSurface: t.latestSurface,
                authorizationWindow: t.takeAuthorizationWindow
              })
            });
            return b.assertCurrent(), O;
          },
          {
            renewOnSuccess: c.renewOnSuccess,
            beforeSuccess: () => Fi(n.items.map((g) => g.context), w)
          }
        );
        return y = !0, p;
      } catch (p) {
        throw l && !y && kt().state === "unlocked" && ft({ broadcast: !0, reason: "manual-lock" }), p;
      }
    };
    try {
      return c.passkey === "unlock" ? (await d(), await u("none", !0)) : await u(c.passkey === "uv" ? "uv" : "none");
    } catch (f) {
      if (c.passkey !== "unlock" && Em(f, i))
        return await d(), await u("none", !0);
      throw f;
    }
  } catch (r) {
    throw it(r, "approve_release_failed");
  }
}
async function t5(e, t) {
  let n;
  if (await mr(async (r) => {
    Mn(e);
    const i = { kind: "signing-policy-downgrade", context: e }, o = async (c) => {
      Mn(e);
      const l = await Pd(i, r, c);
      if (l.kind !== "assertion") throw new _("authorization_result_invalid", "policy assertion is missing");
      n = Zc(l.assertion, e);
    }, s = t.takeAuthorizationWindow();
    if (s) {
      await o(s);
      return;
    }
    Mn(e);
    const a = cw(e);
    await Ki(
      a,
      r,
      (c) => rm({
        uiShowPending: fw,
        parentSurface: () => t.latestSurface(),
        visibilityTarget: c,
        requireParentSurface: !0
      }),
      () => o()
    );
  }), !n) throw new _("authorization_result_invalid", "policy assertion is missing");
  return Zc(n, e);
}
async function n5(e, t) {
  try {
    const n = q3(e, {
      origin: window.location.origin,
      hostname: window.location.hostname
    }), r = await t5(n, t);
    return { challengeId: n.challengeId, assertion: r };
  } catch (n) {
    throw it(n, "policy_authorization_failed");
  }
}
async function r5(e) {
  const t = B6(e);
  try {
    const n = await nk(t.webauthn);
    return { challengeId: t.challengeId, assertion: n };
  } catch (n) {
    throw it(n, "delete_authz_failed");
  }
}
function i5() {
  const e = Xc();
  if (e.get("mode") !== "setup") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Ea({ locale: t, theme: n });
  const r = e.get("id"), i = document.getElementById("page"), o = i?.querySelector(".card"), s = i?.querySelector("h1"), a = i?.querySelector(".sub"), c = i?.querySelector(".body");
  if (!r || !o || !s || !a || !c) return;
  document.body.classList.add("setup-mode"), X(s, "setup.title"), X(a, "setup.subtitle"), X(c, "setup.topLevelBody");
  const l = document.createElement("button");
  l.type = "button", l.className = "action", X(l, "setup.createPasskey");
  const d = document.createElement("p");
  d.className = "setup-status", X(d, "setup.readyOnOrigin"), o.append(l, d);
  const u = new URLSearchParams(window.location.hash.slice(1)).get("req");
  if (!u) {
    l.disabled = !0, X(d, "setup.requestMissing");
    return;
  }
  let f;
  try {
    f = K6(u);
  } catch {
    l.disabled = !0, X(d, "setup.requestInvalid");
    return;
  }
  const h = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${ym}${r}`), y = (w) => {
    const p = it(w, "setup_create_failed"), g = {
      type: "setup-error",
      build: ye,
      id: r,
      code: p.code,
      message: p.message,
      retryable: p.retryable
    };
    V6(r, p), h?.postMessage(g);
    try {
      window.opener?.postMessage(g, window.location.origin);
    } catch {
    }
    l.disabled = !1, X(d, _e(p.message));
  };
  l.addEventListener("click", () => {
    l.disabled = !0, X(d, "setup.passkeyPrompt");
    let w;
    try {
      w = Xl({
        rpId: Ft(),
        vaultUserHandle: f.vaultUserHandle,
        displayName: f.displayName,
        authzCreationOptions: f.authzCreationOptions
      });
    } catch (p) {
      y(p);
      return;
    }
    w.then((p) => {
      const g = { type: "setup-created", build: ye, id: r, result: p };
      j6(r, p), h?.postMessage(g);
      try {
        window.opener?.postMessage(g, window.location.origin);
      } catch {
      }
      X(d, "setup.created"), setTimeout(() => window.close(), 250);
    }).catch(y);
  });
}
function o5() {
  const e = Xc();
  if (e.get("mode") !== "authorize") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Ea({ locale: t, theme: n });
  const r = e.get("id"), o = document.getElementById("page")?.querySelector(".card"), s = o?.querySelector(".body");
  if (!r || !o || !s || (s.classList.add("waiting"), X(s, window.opener ? "authorize.waiting" : "authorize.waitingLong"), !window.opener)) return;
  const a = () => {
    const p = window.opener;
    if (!p) return [];
    const g = [p];
    try {
      for (let k = 0; k < p.frames.length; k += 1) g.push(p.frames[k]);
    } catch {
    }
    return g;
  };
  let c = null;
  const l = (p) => {
    try {
      c?.postMessage(p, window.location.origin);
    } catch {
    }
  };
  let d = !1, u = null;
  const f = setTimeout(() => X(s, "authorize.waitingLong"), v6), h = () => {
    if (!d)
      for (const p of a())
        try {
          p.postMessage({ type: "authorization-ready", build: ye, id: r }, window.location.origin);
        } catch {
        }
  }, y = (p) => {
    if (d) return;
    d = !0, u && clearInterval(u), clearTimeout(f), s.classList.remove("waiting"), window.removeEventListener("message", w), document.body.classList.add("setup-mode");
    const g = new AbortController(), k = (R) => {
      const H = it(R, R instanceof Error && R.message === "operation-cancelled" ? "authorization_cancelled" : "authorization_failed");
      l({
        type: "authorization-error",
        build: ye,
        id: r,
        code: H.code,
        message: H.message,
        retryable: H.retryable
      }), setTimeout(() => window.close(), 250);
    }, b = (R, H) => {
      R.then(
        () => {
          try {
            l({ type: "authorization-approved", build: ye, id: r, result: H() }), setTimeout(() => window.close(), 250);
          } catch (L) {
            k(L);
          }
        },
        k
      );
    };
    if ("kind" in p) {
      if (p.kind === "protected-hd-wallet") {
        let L;
        const T = () => g.abort(new _("authorization_cancelled", "protected HD window closed", !0)), D = setTimeout(() => g.abort(new _("authorization_window_timeout", "protected HD window expired", !0)), wr(p));
        window.addEventListener("pagehide", T, { once: !0 });
        const z = Q3(p, g.signal, p6).then((E) => {
          L = E;
        }).catch((E) => {
          throw aw(E) || E instanceof Error && ["operation-cancelled", "passkey-cancelled"].includes(E.message) ? new _("authorization_cancelled", "protected HD operation cancelled", !0) : E;
        }).finally(() => {
          clearTimeout(D), window.removeEventListener("pagehide", T);
        });
        b(z, () => {
          if (g.signal.aborted) throw xr(g.signal);
          if (wr(p), !L) throw new _("authorization_result_invalid", "protected HD result is missing");
          return L;
        });
        return;
      }
      if (p.kind !== "signing-policy-downgrade") {
        k(new _("authorization_request_invalid", "authorization request kind is invalid"));
        return;
      }
      try {
        Mn(p.context);
      } catch (L) {
        k(L);
        return;
      }
      let R;
      const H = cw(p.context);
      b(
        Ki(
          H,
          g.signal,
          (L) => rm({
            uiShowPending: fw,
            visibilityTarget: L,
            requireParentSurface: !0
          }),
          async () => {
            Mn(p.context), R = await G3(p.context, g.signal);
          }
        ),
        () => {
          if (!R) throw new _("authorization_result_invalid", "policy assertion is missing");
          return { kind: "assertion", assertion: R };
        }
      );
      return;
    }
    let O = { kind: "confirmed" };
    const B = async () => {
      if (p.passkey === "none") return;
      if (p.passkey === "uv") {
        if (!p.prompt.challenge) throw new _("invalid_payload", "authorization challenge is missing");
        await km({
          wrapMeta: p.wrapMeta,
          challenge: p.prompt.challenge,
          abortSignals: [g.signal]
        });
        return;
      }
      const R = await pa(ca(p.wrapMeta), Ft(), g.signal);
      O = {
        kind: "unlock",
        prfOutput: le(R.prfOutput),
        prfSalt: le(R.prfSalt)
      }, R.prfOutput.fill(0);
    };
    b(Ki(nm(p.prompt), g.signal, void 0, B), () => O);
  }, w = (p) => {
    if (p.origin !== window.location.origin) return;
    const g = p.data;
    if (!g || g.type !== "authorization-request" || g.id !== r || !Hr(g.build)) return;
    const k = a().find((b) => b === p.source);
    if (k) {
      c = k;
      try {
        y(O6(g.request));
      } catch (b) {
        const O = it(b, "authorization_request_invalid");
        l({
          type: "authorization-error",
          build: ye,
          id: r,
          code: O.code,
          message: O.message,
          retryable: O.retryable
        });
      }
    }
  };
  window.addEventListener("message", w), h(), u = setInterval(h, 500);
}
function s5() {
  if (window.self !== window.top || Xc().has("mode")) return;
  const e = document.querySelector("#page .body");
  e && X(e, "sandbox.intro");
}
Me.register("handshake", (e) => {
  const t = e ?? {}, n = typeof t.expectedBuildHash == "string" ? t.expectedBuildHash : null;
  if (typeof t.parentOrigin != "string" || typeof t.nonce != "string" || t.nonce.length < 16)
    throw new _("invalid_handshake", "handshake must include parentOrigin and nonce");
  if (n !== null && n !== ye.buildHash)
    throw new _("build_hash_mismatch", "sandbox build hash does not match parent expectation");
  const r = k6(t.appearance);
  r && Ea(r);
  const i = dh ? ze() : Wl(t.policy === void 0 ? void 0 : ql(t.policy));
  return dh = !0, {
    accepted: !0,
    channel: Xn,
    version: 2,
    sandboxOrigin: window.location.origin,
    build: ye,
    policy: i
  };
});
Me.register("set-appearance", (e) => (Ea(wm(e)), { accepted: !0 }));
Me.register("status", (e) => {
  const t = A6(e);
  try {
    t.wrapMeta && kt(t.wrapMeta);
    const n = kt(), r = ze();
    return {
      ...n,
      policy: r,
      session: {
        ...n.expiresAt !== void 0 ? { expiresAt: n.expiresAt } : {},
        strict: r.strictApprovals
      }
    };
  } catch (n) {
    throw it(n, "invalid_wrap_meta");
  }
});
Me.register("setup", G6);
Me.register("unlock", Z6);
Me.register("lock", () => ({ ...ft({ broadcast: !0, reason: "manual-lock" }), policy: ze() }));
Me.register("seal", Y6);
Me.register("sealHdWallet", X6);
Me.register("reveal", J6);
Me.register("sign", Q6);
Me.register("approveRelease", e5);
Me.register("deleteAuthzAssertion", r5);
Me.register("authorizeSigningPolicyDowngrade", n5);
L2((e) => Me.emit("vault.state", e));
vk((e) => Me.emit(e));
await Hm();
Me.start();
window.addEventListener("pagehide", j2);
i5();
o5();
s5();
window.self !== window.top && document.body.classList.add("embedded");
const Im = globalThis.crypto, a5 = { webcrypto: Im }, Bm = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: a5,
  webcrypto: Im
}, Symbol.toStringTag, { value: "Module" }));
