const Jn = "avibe.vault.crypto";
const he = {
  sandboxVersion: "0.1.0",
  // Resource hashes are the authority; this source-input identity additionally
  // binds ready/handshake and same-release top-level ceremony pairing.
  buildHash: "31abc4f68a989990b27423d7121fd22cbf0068276ed5ff3afaf3a2b5296888c4"
};
class S extends Error {
  code;
  retryable;
  constructor(t, n, r = !1) {
    super(n ?? t), this.code = t, this.retryable = r;
  }
}
function Hd(e) {
  let t;
  try {
    t = new URL(e);
  } catch {
    return !1;
  }
  const n = t.hostname.toLowerCase();
  return n === "localhost" || n === "127.0.0.1" || n === "::1" || n === "[::1]" ? t.protocol === "http:" || t.protocol === "https:" : !!(t.protocol === "https:" && (n === "avibe.bot" || n.endsWith(".avibe.bot")));
}
function Hm(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return t.channel === Jn && t.version === 2 && typeof t.id == "string" && t.id.length > 0 && typeof t.op == "string";
}
const Nm = /^[A-Za-z0-9_-]{16,128}$/;
function gh(e) {
  return typeof e == "object" && e !== null;
}
function Um(e) {
  return gh(e) ? e.channel === Jn && e.version === 2 && e.kind === "event" && e.event === "confirm.surface" : !1;
}
class Dm {
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
        channel: Jn,
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
      channel: Jn,
      version: 2,
      build: he,
      capabilities: {
        operations: Array.from(this.handlers.keys())
      }
    });
  }
  async onMessage(t) {
    if (window.parent === window || t.source !== window.parent || !Hd(t.origin)) return;
    if (Um(t.data)) {
      if (t.origin === this.pinnedParentOrigin && t.source === this.pinnedSource) {
        const c = t.data.surface ?? t.data.payload;
        c !== void 0 && this.recordParentSurfaceEvent(t.data, c);
      }
      return;
    }
    if (!Hm(t.data)) return;
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
    if (n.authorizationWindow !== void 0 && (typeof n.authorizationWindow != "string" || !Nm.test(n.authorizationWindow))) {
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
        channel: Jn,
        version: 2,
        id: n.id,
        ok: !0,
        result: c
      });
    } catch (c) {
      const l = c instanceof S ? c.code : "internal_error", u = c instanceof S ? c.retryable : !1, d = c instanceof S ? c.message : void 0;
      this.reply(t.source, this.fail(n.id, l, d, u)), r && this.clearPin();
    } finally {
      this.activeSurfaceSlots.delete(n.id);
    }
  }
  validateInitialHandshake(t, n) {
    if (t.op !== "handshake" || !gh(t.payload)) return null;
    const r = t.payload.parentOrigin, i = t.payload.nonce;
    return typeof r != "string" || r !== n || !Hd(r) || typeof i != "string" || i.length < 16 || i.length > 256 ? null : i;
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
    return { channel: Jn, version: 2, id: t, ok: !1, error: { code: n, message: r, retryable: i } };
  }
  reply(t, n) {
    !t || this.pinnedParentOrigin === null || t.postMessage(n, this.pinnedParentOrigin);
  }
  postToParent(t, n) {
    t.postMessage(n, "*");
  }
}
function Qc() {
  return new URLSearchParams((window.location.hash ?? "").slice(1));
}
function mh(e, t, n, r = {}) {
  const i = new URL(window.location.href);
  return he.buildHash !== "dev" && (i.pathname = `/v/${he.buildHash}/index.html`), i.search = "", i.hash = new URLSearchParams({ mode: e, id: t, ...n, ...r }).toString(), i.toString();
}
function Lr(e) {
  if (!e || typeof e != "object") return !1;
  const t = e;
  return t.buildHash === he.buildHash && t.sandboxVersion === he.sandboxVersion;
}
async function zm() {
  if (he.buildHash === "dev") return;
  const e = new URL(window.location.href);
  if (e.pathname !== `/v/${he.buildHash}/index.html` || e.search)
    throw new Error("Sandbox release document URL mismatch");
  const t = document.querySelectorAll('link[rel="stylesheet"]');
  if (t.length !== 1 || !t[0].integrity.startsWith("sha256-") || !t[0].href.startsWith(`${e.origin}/v/${he.buildHash}/assets/`))
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
class Ot extends St {
}
class Fo extends St {
}
class oo extends St {
}
class Mm extends St {
}
class Km extends St {
}
class Fm extends St {
}
class jm extends St {
}
class Vm extends St {
}
class qm extends St {
}
class Wm extends St {
}
class el extends St {
}
const Gm = {}, bh = Zm(globalThis, Gm);
function Zm(e, t) {
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
async function Ym() {
  if (bh !== void 0 && globalThis.crypto !== void 0)
    return globalThis.crypto.subtle;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Om);
    return e.subtle;
  } catch (e) {
    throw new el(e);
  }
}
class tl {
  constructor() {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    });
  }
  async _setup() {
    this._api === void 0 && (this._api = await Ym());
  }
}
const wn = {
  Base: 0,
  Psk: 1,
  Auth: 2,
  AuthPsk: 3
}, Nd = {
  DhkemX25519HkdfSha256: 32
}, vh = {
  HkdfSha256: 1
}, nl = {
  Aes128Gcm: 1,
  Aes256Gcm: 2,
  ExportOnly: 65535
}, ts = 8192, Xm = 268435456, Ud = 32, je = /* @__PURE__ */ new Uint8Array(0), Ct = 0n, kr = 1n, so = 2n, Jm = /* @__PURE__ */ new Uint8Array([
  75,
  69,
  77,
  0,
  0
]), Dd = /* @__PURE__ */ new Uint8Array([
  72,
  80,
  75,
  69,
  45,
  118,
  49
]);
function jo(e) {
  return new Uint8Array(de(e));
}
function de(e) {
  return e instanceof ArrayBuffer ? e : ArrayBuffer.isView(e) ? new Uint8Array(e.buffer, e.byteOffset, e.byteLength).slice().buffer : new Uint8Array(e).slice().buffer;
}
class Qm extends tl {
  constructor() {
    super(), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: vh.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: je
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
    return r.set(Dd, 0), r.set(this._suiteId, 7), r.set(t, 7 + this._suiteId.byteLength), r.set(n, 7 + this._suiteId.byteLength + t.byteLength), r;
  }
  buildLabeledInfo(t, n, r) {
    this._checkInit();
    const i = new Uint8Array(9 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return i.set(new Uint8Array([0, r]), 0), i.set(Dd, 2), i.set(this._suiteId, 9), i.set(t, 9 + this._suiteId.byteLength), i.set(n, 9 + this._suiteId.byteLength + t.byteLength), i;
  }
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : de(t);
    if (r.byteLength !== this.hashSize)
      throw new Ot("The salt length must be the same as the hashSize");
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
    let c = je;
    const l = jo(n), u = new Uint8Array(1);
    if (r > 255 * this.hashSize)
      throw new Error("Entropy limit reached");
    const d = new Uint8Array(this.hashSize + l.length + 1);
    for (let f = 1, h = 0; h < a.length; f++)
      u[0] = f, d.set(c, 0), d.set(l, c.length), d.set(u, c.length + l.length), c = new Uint8Array(await this._api.sign("HMAC", o, d.slice(0, c.length + l.length + 1))), a.length - h >= c.length ? (a.set(c, h), h += c.length) : (a.set(c.slice(0, a.length - h), h), h += a.length - h);
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
    if (this._suiteId === je)
      throw new Error("Not initialized. Call init()");
  }
}
class xh extends Qm {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: vh.HkdfSha256
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
const hi = (e) => typeof e == "object" && e !== null && typeof e.privateKey == "object" && typeof e.publicKey == "object";
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
function ao(e, t) {
  const n = new Uint8Array(e.length + t.length);
  return n.set(e, 0), n.set(t, e.length), n;
}
function zd(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = atob(t), r = new Uint8Array(n.length);
  for (let i = 0; i < n.length; i++)
    r[i] = n.charCodeAt(i);
  return r;
}
async function eb() {
  if (typeof bh < "u" && globalThis.crypto !== void 0)
    return globalThis.crypto;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Om);
    return e;
  } catch {
    throw new Error("failed to load Crypto");
  }
}
function tb(e, t) {
  if (e.byteLength !== t.byteLength)
    throw new Error("xor: different length inputs");
  const n = new Uint8Array(e.byteLength);
  for (let r = 0; r < e.byteLength; r++)
    n[r] = e[r] ^ t[r];
  return n;
}
const nb = /* @__PURE__ */ new Uint8Array([
  101,
  97,
  101,
  95,
  112,
  114,
  107
]), rb = /* @__PURE__ */ new Uint8Array([
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
function ib(e, t, n) {
  const r = new Uint8Array(e.length + t.length + n.length);
  return r.set(e, 0), r.set(t, e.length), r.set(n, e.length + t.length), r;
}
class ob {
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
    const i = new Uint8Array(Jm);
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
    if (n.byteLength > ts)
      throw new Ot("Too long ikm");
    return await this._prim.deriveKeyPair(n);
  }
  async encap(t) {
    let n;
    t.ekm === void 0 ? n = await this.generateKeyPair() : hi(t.ekm) ? n = t.ekm : n = await this.deriveKeyPair(t.ekm);
    const r = await this._prim.serializePublicKey(n.publicKey), i = await this._prim.serializePublicKey(t.recipientPublicKey);
    try {
      let o;
      if (t.senderKey === void 0)
        o = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey));
      else {
        const c = hi(t.senderKey) ? t.senderKey.privateKey : t.senderKey, l = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey)), u = new Uint8Array(await this._prim.dh(c, t.recipientPublicKey));
        o = ao(l, u);
      }
      let s;
      if (t.senderKey === void 0)
        s = ao(new Uint8Array(r), new Uint8Array(i));
      else {
        const c = hi(t.senderKey) ? t.senderKey.publicKey : await this._prim.derivePublicKey(t.senderKey), l = await this._prim.serializePublicKey(c);
        s = ib(new Uint8Array(r), new Uint8Array(i), new Uint8Array(l));
      }
      const a = await this._generateSharedSecret(o, s);
      return {
        enc: r,
        sharedSecret: a
      };
    } catch (o) {
      throw new Mm(o);
    }
  }
  async decap(t) {
    const n = de(t.enc), r = await this._prim.deserializePublicKey(n), i = hi(t.recipientKey) ? t.recipientKey.privateKey : t.recipientKey, o = hi(t.recipientKey) ? t.recipientKey.publicKey : await this._prim.derivePublicKey(t.recipientKey), s = await this._prim.serializePublicKey(o);
    try {
      let a;
      if (t.senderPublicKey === void 0)
        a = new Uint8Array(await this._prim.dh(i, r));
      else {
        const l = new Uint8Array(await this._prim.dh(i, r)), u = new Uint8Array(await this._prim.dh(i, t.senderPublicKey));
        a = ao(l, u);
      }
      let c;
      if (t.senderPublicKey === void 0)
        c = ao(new Uint8Array(n), new Uint8Array(s));
      else {
        const l = await this._prim.serializePublicKey(t.senderPublicKey);
        c = new Uint8Array(n.byteLength + s.byteLength + l.byteLength), c.set(new Uint8Array(n), 0), c.set(new Uint8Array(s), n.byteLength), c.set(new Uint8Array(l), n.byteLength + s.byteLength);
      }
      return await this._generateSharedSecret(a, c);
    } catch (a) {
      throw new Km(a);
    }
  }
  async _generateSharedSecret(t, n) {
    const r = this._kdf.buildLabeledIkm(nb, t), i = this._kdf.buildLabeledInfo(rb, n, this.secretSize);
    return await this._kdf.extractAndExpand(je, r, i, this.secretSize);
  }
}
const co = ["deriveBits"], sb = /* @__PURE__ */ new Uint8Array([
  100,
  107,
  112,
  95,
  112,
  114,
  107
]), ab = /* @__PURE__ */ new Uint8Array([115, 107]);
class Er {
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
class cb {
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
      return Promise.reject(new Fo(n));
    }
  }
  async deserializePublicKey(t) {
    try {
      return await this._importRawKey(de(t), !0);
    } catch (n) {
      throw new oo(n);
    }
  }
  serializePrivateKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new Fo(n));
    }
  }
  async deserializePrivateKey(t) {
    try {
      return await this._importRawKey(de(t), !1);
    } catch (n) {
      throw new oo(n);
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
      throw new oo(i);
    }
  }
  async generateKeyPair() {
    try {
      const t = await this._curve.utils.randomSecretKey(), n = new Er(this._algName, t, "private", co);
      return { publicKey: await this.derivePublicKey(n), privateKey: n };
    } catch (t) {
      throw new el(t);
    }
  }
  async deriveKeyPair(t) {
    try {
      const n = de(t), r = await this._hkdf.labeledExtract(je.buffer, sb, new Uint8Array(n)), i = await this._hkdf.labeledExpand(r, ab, je, this._nSk), o = new Er(this._algName, new Uint8Array(i), "private", co);
      return {
        privateKey: o,
        publicKey: await this.derivePublicKey(o)
      };
    } catch (n) {
      throw new Wm(n);
    }
  }
  derivePublicKey(t) {
    try {
      const n = this._curve.getPublicKey(t.key);
      return Promise.resolve(new Er(this._algName, n, "public"));
    } catch (n) {
      return Promise.reject(new oo(n));
    }
  }
  dh(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t.key, n.key).buffer);
    } catch (r) {
      return Promise.reject(new Fo(r));
    }
  }
  _importRawKey(t, n) {
    return new Promise((r, i) => {
      n && t.byteLength !== this._nPk && i(new Error("Invalid length of the key")), !n && t.byteLength !== this._nSk && i(new Error("Invalid length of the key")), r(new Er(this._algName, new Uint8Array(t), n ? "public" : "private", n ? [] : co));
    });
  }
  _importJWK(t, n) {
    return new Promise((r, i) => {
      t.kty !== "OKP" && i(new Error(`Invalid kty: ${t.kty}`)), t.crv !== this._algName && i(new Error(`Invalid crv: ${t.crv}`)), n ? (typeof t.d < "u" && i(new Error("Invalid key: `d` should not be set")), typeof t.x != "string" && i(new Error("Invalid key: `x` not found")), r(new Er(this._algName, zd(t.x), "public"))) : (typeof t.d != "string" && i(new Error("Invalid key: `d` not found")), r(new Er(this._algName, zd(t.d), "private", co)));
    });
  }
}
const lb = ["encrypt", "decrypt"];
function db(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Ci(e, t = "") {
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new Error(`${n}expected integer >0, got ${e}`);
  }
}
function on(e, t, n = "") {
  const r = db(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function ns(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function ub(e, t) {
  on(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new Error('"digestInto() output" expected to be of length >=' + n);
}
function fb(e) {
  if (typeof e == "bigint") {
    if (!Vo(e))
      throw new Error("positive bigint expected, got " + e);
  } else
    Ci(e);
  return e;
}
function rs(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
const hb = /* @__PURE__ */ new Uint32Array([287454020]), pb = /* @__PURE__ */ new Uint8Array(hb.buffer);
pb[0];
function $a(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function It(e, t) {
  return e << 32 - t | e >>> t;
}
const kh = /* @ts-ignore: to use toHex */ typeof Uint8Array.from([]).toHex == "function" && // @ts-ignore: to use fromHex
typeof Uint8Array.fromHex == "function", wb = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0")), yb = [
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
function gb(e) {
  if (on(e), kh)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += wb[e[n]];
  return t;
}
const Vt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function wc(e) {
  if (e >= Vt._0 && e <= Vt._9)
    return e - Vt._0;
  if (e >= Vt.A && e <= Vt.F)
    return e - (Vt.A - 10);
  if (e >= Vt.a && e <= Vt.f)
    return e - (Vt.a - 10);
}
function mb(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (kh)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = wc(e.charCodeAt(o)), a = wc(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function bb(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  let t = Ct;
  for (let n = 0; n < e.length; n++) {
    const r = wc(e.charCodeAt(n));
    if (r === void 0)
      throw new Error('hex string expected, got non-hex character "' + e[n] + '" at index ' + n);
    t = t << 4n | yb[r];
  }
  return t;
}
function vb(e) {
  Ci(e, "numberToBigint");
  let t = e, n = Ct, r = 1n;
  for (; t > 0; )
    t % 2 === 1 && (n += r), t = Math.floor(t / 2), r <<= 1n;
  return n;
}
function Md(e) {
  return bb(gb(yc(on(e)).reverse()));
}
function xb(e, t) {
  Ci(t), e = fb(e);
  const n = mb(e.toString(16).padStart(t * 2, "0"));
  if (n.length !== t)
    throw new Error("number too large");
  return n;
}
function kb(e, t) {
  return xb(e, t).reverse();
}
function yc(e) {
  return Uint8Array.from(e);
}
function Vo(e) {
  return typeof e == "bigint" && Ct <= e;
}
function Eb(e, t, n) {
  return Vo(e) && Vo(t) && Vo(n) && t <= e && e < n;
}
function Kd(e, t, n, r) {
  if (!Eb(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Ab(e, t = {}, n = {}) {
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
async function Sb(e = 32) {
  const t = await eb(), n = new Uint8Array(e);
  return t.getRandomValues(n), n;
}
function _b(e) {
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
function Ib(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash must wrapped by utils.createHasher");
  Ci(e.outputLen), Ci(e.blockLen);
}
function Bb(e, t = {}) {
  const n = (o, s) => e(s).update(o).digest(), r = e(void 0), i = Object.assign(n, {
    outputLen: r.outputLen,
    blockLen: r.blockLen,
    create: (o) => e(o),
    ...t
  });
  return Object.freeze(i);
}
let Eh = class {
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
    }), Ib(t), on(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), rs(i);
  }
  update(t) {
    return ns(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    ns(this), on(t, this.outputLen, "output"), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const Ah = (e, t, n) => new Eh(e, t).update(n).digest();
Ah.create = (e, t) => new Eh(e, t);
function Tb(e, t, n) {
  return e & t ^ ~e & n;
}
function Pb(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let $b = class {
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
    }), this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = $a(this.buffer);
  }
  update(t) {
    ns(this), on(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = $a(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    ns(this), ub(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, rs(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, vb(this.length * 8), o), this.process(r, 0);
    const a = $a(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen must be aligned to 32bit");
    const l = c / 4, u = this.get();
    if (l > u.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < l; d++)
      a.setUint32(4 * d, u[d], o);
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
]), Ob = /* @__PURE__ */ Uint32Array.from([
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
]), gn = /* @__PURE__ */ new Uint32Array(64);
let Cb = class extends $b {
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
    for (let d = 0; d < 16; d++, n += 4)
      gn[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = gn[d - 15], h = gn[d - 2], p = It(f, 7) ^ It(f, 18) ^ f >>> 3, y = It(h, 17) ^ It(h, 19) ^ h >>> 10;
      gn[d] = y + gn[d - 7] + p + gn[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = It(a, 6) ^ It(a, 11) ^ It(a, 25), h = u + f + Tb(a, c, l) + Ob[d] + gn[d] | 0, y = (It(r, 2) ^ It(r, 13) ^ It(r, 22)) + Pb(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    rs(gn);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), rs(this.buffer);
  }
}, Rb = class extends Cb {
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
const Lb = /* @__PURE__ */ Bb(
  () => new Rb(),
  /* @__PURE__ */ _b(1)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Sh(e, t) {
  const n = e % t;
  return n >= Ct ? n : t + n;
}
function wt(e, t, n) {
  let r = e;
  for (; t-- > Ct; )
    r *= r, r %= n;
  return r;
}
function Hb(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Nb(e) {
  return Ab(e, {
    adjustScalarBytes: "function",
    powPminus2: "function"
  }), Object.freeze({ ...e });
}
function Ub(e) {
  const t = Nb(e), { P: n, type: r, adjustScalarBytes: i, powPminus2: o, randomBytes: s } = t, a = r === "x25519";
  if (!a && r !== "x448")
    throw new Error("invalid type");
  const c = s || Sb, l = a ? 255n : 448n, u = a ? 32 : 56, d = a ? 9n : 5n, f = a ? 121665n : 39081n, h = a ? so ** 254n : so ** 447n, p = a ? 8n * so ** 251n - kr : 4n * so ** 445n - kr, y = h + p + kr, w = (x) => Sh(x, n), m = g(d);
  function g(x) {
    return kb(w(x), u);
  }
  function v(x) {
    const b = yc(on(x, u, "uCoordinate"));
    return a && (b[31] &= 127), w(Md(b));
  }
  function L(x) {
    return Md(i(yc(on(x, u, "scalar"))));
  }
  function T(x, b) {
    const _ = U(v(b), L(x));
    if (_ === Ct)
      throw new Error("invalid private or public key received");
    return g(_);
  }
  function C(x) {
    return T(x, m);
  }
  const H = C, O = T;
  function I(x, b, _) {
    const $ = w(x * (b - _));
    return b = w(b - $), _ = w(_ + $), { x_2: b, x_3: _ };
  }
  function U(x, b) {
    Kd("u", x, Ct, n), Kd("scalar", b, h, y);
    const _ = b, $ = x;
    let A = kr, B = Ct, P = x, R = kr, N = Ct;
    for (let D = l - 1n; D >= Ct; D--) {
      const M = _ >> D & kr;
      N ^= M, { x_2: A, x_3: P } = I(N, A, P), { x_2: B, x_3: R } = I(N, B, R), N = M;
      const F = A + B, q = w(F * F), j = A - B, W = w(j * j), X = q - W, we = P + R, se = P - R, xe = w(se * F), Ze = w(we * j), fn = xe + Ze, xr = xe - Ze;
      P = w(fn * fn), R = w($ * w(xr * xr)), A = w(q * W), B = w(X * (q + w(f * X)));
    }
    ({ x_2: A, x_3: P } = I(N, A, P)), { x_2: B, x_3: R } = I(N, B, R);
    const K = o(B);
    return w(A * K);
  }
  const z = {
    secretKey: u,
    publicKey: u,
    seed: u
  }, E = async (x) => (x === void 0 && (x = await c(u)), on(x, z.seed, "seed"), x), k = { randomSecretKey: E };
  return Object.freeze({
    keygen: Hb(E, H),
    getSharedSecret: O,
    getPublicKey: H,
    scalarMult: T,
    scalarMultBase: C,
    utils: k,
    GuBytes: m.slice(),
    lengths: z
  });
}
class Db extends tl {
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
    return await this._api.importKey("raw", t, { name: "AES-GCM" }, !0, lb);
  }
}
class zb {
  constructor() {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: nl.Aes128Gcm
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
    return new Db(t);
  }
}
class Mb extends zb {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: nl.Aes256Gcm
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
function Fd() {
  return new Promise((e, t) => {
    t(new el("Not supported"));
  });
}
const Kb = new Uint8Array([115, 101, 99]);
class rl {
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
    return await Fd();
  }
  async open(t, n) {
    return await Fd();
  }
  async export(t, n) {
    const r = de(t);
    if (r.byteLength > ts)
      throw new Ot("Too long exporter context");
    try {
      return await this._kdf.labeledExpand(this.exporterSecret, Kb, new Uint8Array(r), n);
    } catch (i) {
      throw new Fm(i);
    }
  }
}
class Fb extends rl {
}
class jb extends rl {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.enc = i;
  }
}
class _h extends rl {
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
    return tb(t.baseNonce, n).buffer;
  }
  incrementSeq(t) {
    if (t.seq > Number.MAX_SAFE_INTEGER)
      throw new qm("Message limit reached");
    t.seq += 1;
  }
}
var Vb = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, qb = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, qo;
class Ih {
  constructor() {
    qo.set(this, Promise.resolve());
  }
  async lock() {
    let t;
    const n = new Promise((i) => {
      t = i;
    }), r = Vb(this, qo, "f");
    return qb(this, qo, n, "f"), await r, t;
  }
}
qo = /* @__PURE__ */ new WeakMap();
var jd = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Wb = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, ki;
class Gb extends _h {
  constructor() {
    super(...arguments), ki.set(this, void 0);
  }
  async open(t, n = je.buffer) {
    Wb(this, ki, jd(this, ki, "f") ?? new Ih(), "f");
    const r = await jd(this, ki, "f").lock();
    let i;
    try {
      i = await this._ctx.key.open(this.computeNonce(this._ctx), de(t), de(n));
    } catch (o) {
      throw new Vm(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
ki = /* @__PURE__ */ new WeakMap();
var Vd = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Zb = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Ei;
class Yb extends _h {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Ei.set(this, void 0), this.enc = i;
  }
  async seal(t, n = je.buffer) {
    Zb(this, Ei, Vd(this, Ei, "f") ?? new Ih(), "f");
    const r = await Vd(this, Ei, "f").lock();
    let i;
    try {
      i = await this._ctx.key.seal(this.computeNonce(this._ctx), de(t), de(n));
    } catch (o) {
      throw new jm(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Ei = /* @__PURE__ */ new WeakMap();
const Xb = new Uint8Array([
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
]), Jb = new Uint8Array([101, 120, 112]), Qb = new Uint8Array([
  105,
  110,
  102,
  111,
  95,
  104,
  97,
  115,
  104
]), e0 = new Uint8Array([107, 101, 121]), t0 = new Uint8Array([
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
]), n0 = new Uint8Array([115, 101, 99, 114, 101, 116]), r0 = new Uint8Array([
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
class i0 extends tl {
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
      throw new Ot("KemId cannot be used");
    if (this._kem = t.kem, typeof t.kdf == "number")
      throw new Ot("KdfId cannot be used");
    if (this._kdf = t.kdf, typeof t.aead == "number")
      throw new Ot("AeadId cannot be used");
    this._aead = t.aead, this._suiteId = new Uint8Array(r0), this._suiteId.set(Ti(this._kem.id, 2), 4), this._suiteId.set(Ti(this._kdf.id, 2), 6), this._suiteId.set(Ti(this._aead.id, 2), 8), this._kdf.init(this._suiteId);
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
    return t.psk !== void 0 ? r = t.senderKey !== void 0 ? wn.AuthPsk : wn.Psk : r = t.senderKey !== void 0 ? wn.Auth : wn.Base, await this._keyScheduleS(r, n.sharedSecret, n.enc, t);
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
    return t.psk !== void 0 ? r = t.senderPublicKey !== void 0 ? wn.AuthPsk : wn.Psk : r = t.senderPublicKey !== void 0 ? wn.Auth : wn.Base, await this._keyScheduleR(r, n, t);
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
  async seal(t, n, r = je.buffer) {
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
  async open(t, n, r = je.buffer) {
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
    const i = r.psk === void 0 ? je : jo(r.psk.id), o = await this._kdf.labeledExtract(je, t0, i), s = r.info === void 0 ? je : jo(r.info), a = await this._kdf.labeledExtract(je, Qb, s), c = new Uint8Array(1 + o.byteLength + a.byteLength);
    c.set(new Uint8Array([t]), 0), c.set(new Uint8Array(o), 1), c.set(new Uint8Array(a), 1 + o.byteLength);
    const l = r.psk === void 0 ? je : jo(r.psk.key), u = this._kdf.buildLabeledIkm(n0, l), d = this._kdf.buildLabeledInfo(Jb, c, this._kdf.hashSize), f = await this._kdf.extractAndExpand(n, u, d, this._kdf.hashSize);
    if (this._aead.id === nl.ExportOnly)
      return { aead: this._aead, exporterSecret: f };
    const h = this._kdf.buildLabeledInfo(e0, c, this._aead.keySize), p = await this._kdf.extractAndExpand(n, u, h, this._aead.keySize), y = this._kdf.buildLabeledInfo(Xb, c, this._aead.nonceSize), w = await this._kdf.extractAndExpand(n, u, y, this._aead.nonceSize);
    return {
      aead: this._aead,
      exporterSecret: f,
      key: p,
      baseNonce: new Uint8Array(w),
      seq: 0
    };
  }
  async _keyScheduleS(t, n, r, i) {
    const o = await this._keySchedule(t, n, i);
    return o.key === void 0 ? new jb(this._api, this._kdf, o.exporterSecret, r) : new Yb(this._api, this._kdf, o, r);
  }
  async _keyScheduleR(t, n, r) {
    const i = await this._keySchedule(t, n, r);
    return i.key === void 0 ? new Fb(this._api, this._kdf, i.exporterSecret) : new Gb(this._api, this._kdf, i);
  }
  _validateInputLength(t) {
    if (t.info !== void 0 && t.info.byteLength > Xm)
      throw new Ot("Too long info");
    if (t.psk !== void 0) {
      if (t.psk.key.byteLength < Ud)
        throw new Ot(`PSK must have at least ${Ud} bytes`);
      if (t.psk.key.byteLength > ts)
        throw new Ot("Too long psk.key");
      if (t.psk.id.byteLength > ts)
        throw new Ot("Too long psk.id");
    }
  }
}
class o0 extends i0 {
}
let s0 = class extends xh {
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const a0 = 1n, qd = 2n, c0 = 3n, l0 = 5n, Bh = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn;
function d0(e) {
  const t = 10n, n = 20n, r = 40n, i = 80n, o = Bh, a = e * e % o * e % o, c = wt(a, qd, o) * a % o, l = wt(c, a0, o) * e % o, u = wt(l, l0, o) * l % o, d = wt(u, t, o) * u % o, f = wt(d, n, o) * d % o, h = wt(f, r, o) * f % o, p = wt(h, i, o) * h % o, y = wt(p, i, o) * h % o, w = wt(y, t, o) * u % o;
  return { pow_p_5_8: wt(w, qd, o) * e % o, b2: a };
}
function u0(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const f0 = /* @__PURE__ */ (() => {
  const e = Bh;
  return Ub({
    P: e,
    type: "x25519",
    powPminus2: (t) => {
      const { pow_p_5_8: n, b2: r } = d0(t);
      return Sh(wt(n, c0, e) * r, e);
    },
    adjustScalarBytes: u0
  });
})();
class h0 extends xh {
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : de(t), i = de(n);
    if (r.byteLength !== this.hashSize)
      return Ah(Lb, new Uint8Array(r), new Uint8Array(i)).buffer;
    const o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
}
class p0 extends cb {
  constructor(t) {
    super("X25519", 32, f0, t);
  }
  derive(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t, n));
    } catch (r) {
      return Promise.reject(new Fo(r));
    }
  }
}
class w0 extends ob {
  constructor() {
    const t = new h0();
    super(Nd.DhkemX25519HkdfSha256, new p0(t), t), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Nd.DhkemX25519HkdfSha256
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
function Th(e) {
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
  const r = Th(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`, l = s + "expected Uint8Array" + a + ", got " + c;
    throw r ? new RangeError(l) : new TypeError(l);
  }
  return e;
}
function Ph(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new TypeError("Hash must wrapped by utils.createHasher");
  if (Dt(e.outputLen), Dt(e.blockLen), e.outputLen < 1)
    throw new Error('"outputLen" must be >= 1');
  if (e.blockLen < 1)
    throw new Error('"blockLen" must be >= 1');
}
function qr(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function il(e, t) {
  zt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new RangeError('"digestInto() output" expected to be of length >=' + n);
}
function y0(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function Mt(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Oa(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Bt(e, t) {
  return e << 32 - t | e >>> t;
}
function lo(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const g0 = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function m0(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function b0(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = m0(e[t]);
  return e;
}
const Wd = g0 ? (e) => e : b0, $h = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", v0 = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function ol(e) {
  if (zt(e), $h)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += v0[e[n]];
  return t;
}
const qt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function Gd(e) {
  if (e >= qt._0 && e <= qt._9)
    return e - qt._0;
  if (e >= qt.A && e <= qt.F)
    return e - (qt.A - 10);
  if (e >= qt.a && e <= qt.f)
    return e - (qt.a - 10);
}
function sl(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  if ($h)
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
    const s = Gd(e.charCodeAt(o)), a = Gd(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new RangeError('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function x0(...e) {
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
function Js(e, t = {}) {
  const n = (i, o) => e(o).update(i).digest(), r = e(void 0);
  return n.outputLen = r.outputLen, n.blockLen = r.blockLen, n.canXOF = r.canXOF, n.create = (i) => e(i), Object.assign(n, t), Object.freeze(n);
}
function al(e = 32) {
  Dt(e, "bytesLength");
  const t = typeof globalThis == "object" ? globalThis.crypto : null;
  if (typeof t?.getRandomValues != "function")
    throw new Error("crypto.getRandomValues must be defined");
  if (e > 65536)
    throw new RangeError(`"bytesLength" expected <= 65536, got ${e}`);
  return t.getRandomValues(new Uint8Array(e));
}
const Oh = (e) => ({
  // Current NIST hashAlgs suffixes used here fit in one DER subidentifier octet.
  // Larger suffix values would need base-128 OID encoding and a different length byte.
  oid: Uint8Array.from([6, 9, 96, 134, 72, 1, 101, 3, 4, 2, e])
});
function k0(e, t, n) {
  return e & t ^ ~e & n;
}
function E0(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let cl = class {
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
    this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Oa(this.buffer);
  }
  update(t) {
    qr(this), zt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Oa(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    qr(this), il(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Mt(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Oa(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen must be aligned to 32bit");
    const l = c / 4, u = this.get();
    if (l > u.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < l; d++)
      a.setUint32(4 * d, u[d], o);
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
const mn = /* @__PURE__ */ Uint32Array.from([
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
]), uo = /* @__PURE__ */ BigInt(2 ** 32 - 1), Zd = /* @__PURE__ */ BigInt(32);
function A0(e, t = !1) {
  return t ? { h: Number(e & uo), l: Number(e >> Zd & uo) } : { h: Number(e >> Zd & uo) | 0, l: Number(e & uo) | 0 };
}
function Ch(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = A0(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const Yd = (e, t, n) => e >>> n, Xd = (e, t, n) => e << 32 - n | t >>> n, Ar = (e, t, n) => e >>> n | t << 32 - n, Sr = (e, t, n) => e << 32 - n | t >>> n, fo = (e, t, n) => e << 64 - n | t >>> n - 32, ho = (e, t, n) => e >>> n - 32 | t << 64 - n, S0 = (e, t, n) => e << n | t >>> 32 - n, _0 = (e, t, n) => t << n | e >>> 32 - n, I0 = (e, t, n) => t << n - 32 | e >>> 64 - n, B0 = (e, t, n) => e << n - 32 | t >>> 64 - n;
function Wt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const T0 = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), P0 = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, $0 = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), O0 = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, C0 = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), R0 = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, L0 = /* @__PURE__ */ Uint32Array.from([
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
]), bn = /* @__PURE__ */ new Uint32Array(64);
class H0 extends cl {
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
    for (let d = 0; d < 16; d++, n += 4)
      bn[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = bn[d - 15], h = bn[d - 2], p = Bt(f, 7) ^ Bt(f, 18) ^ f >>> 3, y = Bt(h, 17) ^ Bt(h, 19) ^ h >>> 10;
      bn[d] = y + bn[d - 7] + p + bn[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = Bt(a, 6) ^ Bt(a, 11) ^ Bt(a, 25), h = u + f + k0(a, c, l) + L0[d] + bn[d] | 0, y = (Bt(r, 2) ^ Bt(r, 13) ^ Bt(r, 22)) + E0(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    Mt(bn);
  }
  destroy() {
    this.destroyed = !0, this.set(0, 0, 0, 0, 0, 0, 0, 0), Mt(this.buffer);
  }
}
class N0 extends H0 {
  // We cannot use array here since array allows indexing by variable
  // which means optimizer/compiler cannot use registers.
  A = mn[0] | 0;
  B = mn[1] | 0;
  C = mn[2] | 0;
  D = mn[3] | 0;
  E = mn[4] | 0;
  F = mn[5] | 0;
  G = mn[6] | 0;
  H = mn[7] | 0;
  constructor() {
    super(32);
  }
}
const Rh = Ch([
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
].map((e) => BigInt(e))), U0 = Rh[0], D0 = Rh[1], vn = /* @__PURE__ */ new Uint32Array(80), xn = /* @__PURE__ */ new Uint32Array(80);
class z0 extends cl {
  constructor(t) {
    super(128, t, 16, !1);
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: f, Gh: h, Gl: p, Hh: y, Hl: w } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, f, h, p, y, w];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, f, h, p, y, w) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = p | 0, this.Hh = y | 0, this.Hl = w | 0;
  }
  process(t, n) {
    for (let v = 0; v < 16; v++, n += 4)
      vn[v] = t.getUint32(n), xn[v] = t.getUint32(n += 4);
    for (let v = 16; v < 80; v++) {
      const L = vn[v - 15] | 0, T = xn[v - 15] | 0, C = Ar(L, T, 1) ^ Ar(L, T, 8) ^ Yd(L, T, 7), H = Sr(L, T, 1) ^ Sr(L, T, 8) ^ Xd(L, T, 7), O = vn[v - 2] | 0, I = xn[v - 2] | 0, U = Ar(O, I, 19) ^ fo(O, I, 61) ^ Yd(O, I, 6), z = Sr(O, I, 19) ^ ho(O, I, 61) ^ Xd(O, I, 6), E = $0(H, z, xn[v - 7], xn[v - 16]), k = O0(E, C, U, vn[v - 7], vn[v - 16]);
      vn[v] = k | 0, xn[v] = E | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: f, Fh: h, Fl: p, Gh: y, Gl: w, Hh: m, Hl: g } = this;
    for (let v = 0; v < 80; v++) {
      const L = Ar(d, f, 14) ^ Ar(d, f, 18) ^ fo(d, f, 41), T = Sr(d, f, 14) ^ Sr(d, f, 18) ^ ho(d, f, 41), C = d & h ^ ~d & y, H = f & p ^ ~f & w, O = C0(g, T, H, D0[v], xn[v]), I = R0(O, m, L, C, U0[v], vn[v]), U = O | 0, z = Ar(r, i, 28) ^ fo(r, i, 34) ^ fo(r, i, 39), E = Sr(r, i, 28) ^ ho(r, i, 34) ^ ho(r, i, 39), k = r & o ^ r & a ^ o & a, x = i & s ^ i & c ^ s & c;
      m = y | 0, g = w | 0, y = h | 0, w = p | 0, h = d | 0, p = f | 0, { h: d, l: f } = Wt(l | 0, u | 0, I | 0, U | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const b = T0(U, E, x);
      r = P0(b, I, z, k), i = b | 0;
    }
    ({ h: r, l: i } = Wt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Wt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Wt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = Wt(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: f } = Wt(this.Eh | 0, this.El | 0, d | 0, f | 0), { h, l: p } = Wt(this.Fh | 0, this.Fl | 0, h | 0, p | 0), { h: y, l: w } = Wt(this.Gh | 0, this.Gl | 0, y | 0, w | 0), { h: m, l: g } = Wt(this.Hh | 0, this.Hl | 0, m | 0, g | 0), this.set(r, i, o, s, a, c, l, u, d, f, h, p, y, w, m, g);
  }
  roundClean() {
    Mt(vn, xn);
  }
  destroy() {
    this.destroyed = !0, Mt(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
class M0 extends z0 {
  Ah = He[0] | 0;
  Al = He[1] | 0;
  Bh = He[2] | 0;
  Bl = He[3] | 0;
  Ch = He[4] | 0;
  Cl = He[5] | 0;
  Dh = He[6] | 0;
  Dl = He[7] | 0;
  Eh = He[8] | 0;
  El = He[9] | 0;
  Fh = He[10] | 0;
  Fl = He[11] | 0;
  Gh = He[12] | 0;
  Gl = He[13] | 0;
  Hh = He[14] | 0;
  Hl = He[15] | 0;
  constructor() {
    super(64);
  }
}
const ue = /* @__PURE__ */ Js(
  () => new N0(),
  /* @__PURE__ */ Oh(1)
), gc = /* @__PURE__ */ Js(
  () => new M0(),
  /* @__PURE__ */ Oh(3)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const oe = (e, t, n) => zt(e, t, n), Lh = Dt, G = ol, dt = (...e) => x0(...e), We = (e) => sl(e), ll = Th, dl = (e) => al(e), is = /* @__PURE__ */ BigInt(0), mc = /* @__PURE__ */ BigInt(1);
function Fn(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function ul(e) {
  if (typeof e == "bigint") {
    if (!Wo(e))
      throw new RangeError("positive bigint expected, got " + e);
  } else
    Lh(e);
  return e;
}
function os(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected number, got type=" + typeof e);
  }
  if (!Number.isSafeInteger(e)) {
    const n = t && `"${t}" `;
    throw new RangeError(n + "expected safe integer, got " + e);
  }
}
function po(e) {
  const t = ul(e).toString(16);
  return t.length & 1 ? "0" + t : t;
}
function Hh(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  return e === "" ? is : BigInt("0x" + e);
}
function Xi(e) {
  return Hh(ol(e));
}
function Wr(e) {
  return Hh(ol(bc(zt(e)).reverse()));
}
function fl(e, t) {
  if (Dt(t), t === 0)
    throw new RangeError("zero length");
  e = ul(e);
  const n = e.toString(16);
  if (n.length > t * 2)
    throw new RangeError("number too large");
  return sl(n.padStart(t * 2, "0"));
}
function Nh(e, t) {
  return fl(e, t).reverse();
}
function K0(e, t) {
  if (e = oe(e), t = oe(t), e.length !== t.length)
    return !1;
  let n = 0;
  for (let r = 0; r < e.length; r++)
    n |= e[r] ^ t[r];
  return n === 0;
}
function bc(e) {
  return Uint8Array.from(oe(e));
}
function F0(e) {
  if (typeof e != "string")
    throw new TypeError("ascii string expected, got " + typeof e);
  return Uint8Array.from(e, (t, n) => {
    const r = t.charCodeAt(0);
    if (t.length !== 1 || r > 127)
      throw new RangeError(`string contains non-ASCII character "${e[n]}" with code ${r} at position ${n}`);
    return r;
  });
}
const Wo = (e) => typeof e == "bigint" && is <= e;
function j0(e, t, n) {
  return Wo(e) && Wo(t) && Wo(n) && t <= e && e < n;
}
function ss(e, t, n, r) {
  if (!j0(t, n, r))
    throw new RangeError("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function hl(e) {
  if (e < is)
    throw new Error("expected non-negative bigint, got " + e);
  let t;
  for (t = 0; e > is; e >>= mc, t += 1)
    ;
  return t;
}
const pl = (e) => (mc << BigInt(e)) - mc;
function V0(e, t, n) {
  if (Dt(e, "hashLen"), Dt(t, "qByteLen"), typeof n != "function")
    throw new TypeError("hmacFn must be a function");
  const r = (w) => new Uint8Array(w), i = Uint8Array.of(), o = Uint8Array.of(0), s = Uint8Array.of(1), a = 1e3;
  let c = r(e), l = r(e), u = 0;
  const d = () => {
    c.fill(1), l.fill(0), u = 0;
  }, f = (...w) => n(l, dt(c, ...w)), h = (w = i) => {
    l = f(o, w), c = f(), w.length !== 0 && (l = f(s, w), c = f());
  }, p = () => {
    if (u++ >= a)
      throw new Error("drbg: tried max amount of iterations");
    let w = 0;
    const m = [];
    for (; w < t; ) {
      c = f();
      const g = c.slice();
      m.push(g), w += c.length;
    }
    return dt(...m);
  };
  return (w, m) => {
    d(), h(w);
    let g;
    for (; (g = m(p())) === void 0; )
      h();
    return d(), g;
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
const Jd = () => {
  throw new Error("not implemented");
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Me = /* @__PURE__ */ BigInt(0), Ee = /* @__PURE__ */ BigInt(1), Qn = /* @__PURE__ */ BigInt(2), Uh = /* @__PURE__ */ BigInt(3), Dh = /* @__PURE__ */ BigInt(4), zh = /* @__PURE__ */ BigInt(5), q0 = /* @__PURE__ */ BigInt(7), Mh = /* @__PURE__ */ BigInt(8), W0 = /* @__PURE__ */ BigInt(9), Kh = /* @__PURE__ */ BigInt(16);
function Ie(e, t) {
  if (t <= Me)
    throw new Error("mod: expected positive modulus, got " + t);
  const n = e % t;
  return n >= Me ? n : t + n;
}
function ge(e, t, n) {
  if (t < Me)
    throw new Error("pow2: expected non-negative exponent, got " + t);
  let r = e;
  for (; t-- > Me; )
    r *= r, r %= n;
  return r;
}
function Qd(e, t) {
  if (e === Me)
    throw new Error("invert: expected non-zero number");
  if (t <= Me)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = Ie(e, t), r = t, i = Me, o = Ee;
  for (; n !== Me; ) {
    const a = r / n, c = r - n * a, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== Ee)
    throw new Error("invert: does not exist");
  return Ie(i, t);
}
function wl(e, t, n) {
  const r = e;
  if (!r.eql(r.sqr(t), n))
    throw new Error("Cannot find square root");
}
function Fh(e, t) {
  const n = e, r = (n.ORDER + Ee) / Dh, i = n.pow(t, r);
  return wl(n, i, t), i;
}
function G0(e, t) {
  const n = e, r = (n.ORDER - zh) / Mh, i = n.mul(t, Qn), o = n.pow(i, r), s = n.mul(t, o), a = n.mul(n.mul(s, Qn), o), c = n.mul(s, n.sub(a, n.ONE));
  return wl(n, c, t), c;
}
function Z0(e) {
  const t = Qs(e), n = jh(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + q0) / Kh;
  return ((a, c) => {
    const l = a;
    let u = l.pow(c, s), d = l.mul(u, r);
    const f = l.mul(u, i), h = l.mul(u, o), p = l.eql(l.sqr(d), c), y = l.eql(l.sqr(f), c);
    u = l.cmov(u, d, p), d = l.cmov(h, f, y);
    const w = l.eql(l.sqr(d), c), m = l.cmov(u, d, w);
    return wl(l, m, c), m;
  });
}
function jh(e) {
  if (e < Uh)
    throw new Error("sqrt is not defined for small field");
  let t = e - Ee, n = 0;
  for (; t % Qn === Me; )
    t /= Qn, n++;
  let r = Qn;
  const i = Qs(e);
  for (; eu(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return Fh;
  let o = i.pow(r, t);
  const s = (t + Ee) / Qn;
  return function(c, l) {
    const u = c;
    if (u.is0(l))
      return l;
    if (eu(u, l) !== 1)
      throw new Error("Cannot find square root");
    let d = n, f = u.mul(u.ONE, o), h = u.pow(l, t), p = u.pow(l, s);
    for (; !u.eql(h, u.ONE); ) {
      if (u.is0(h))
        return u.ZERO;
      let y = 1, w = u.sqr(h);
      for (; !u.eql(w, u.ONE); )
        if (y++, w = u.sqr(w), y === d)
          throw new Error("Cannot find square root");
      const m = Ee << BigInt(d - y - 1), g = u.pow(f, m);
      d = y, f = u.sqr(g), h = u.mul(h, f), p = u.mul(p, g);
    }
    return p;
  };
}
function Y0(e) {
  return e % Dh === Uh ? Fh : e % Mh === zh ? G0 : e % Kh === W0 ? Z0(e) : jh(e);
}
const Gn = (e, t) => (Ie(e, t) & Ee) === Ee, X0 = [
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
function J0(e) {
  const t = {
    ORDER: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = X0.reduce((r, i) => (r[i] = "function", r), t);
  if (ii(e, n), os(e.BYTES, "BYTES"), os(e.BITS, "BITS"), e.BYTES < 1 || e.BITS < 1)
    throw new Error("invalid field: expected BYTES/BITS > 0");
  if (e.ORDER <= Ee)
    throw new Error("invalid field: expected ORDER > 1, got " + e.ORDER);
  return e;
}
function Q0(e, t, n) {
  const r = e;
  if (n < Me)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === Me)
    return r.ONE;
  if (n === Ee)
    return t;
  let i = r.ONE, o = t;
  for (; n > Me; )
    n & Ee && (i = r.mul(i, o)), o = r.sqr(o), n >>= Ee;
  return i;
}
function Vh(e, t, n = !1) {
  const r = e, i = new Array(t.length).fill(n ? r.ZERO : void 0), o = t.reduce((a, c, l) => r.is0(c) ? a : (i[l] = a, r.mul(a, c)), r.ONE), s = r.inv(o);
  return t.reduceRight((a, c, l) => r.is0(c) ? a : (i[l] = r.mul(a, i[l]), r.mul(a, c)), s), i;
}
function eu(e, t) {
  const n = e, r = (n.ORDER - Ee) / Qn, i = n.pow(t, r), o = n.eql(i, n.ONE), s = n.eql(i, n.ZERO), a = n.eql(i, n.neg(n.ONE));
  if (!o && !s && !a)
    throw new Error("invalid Legendre symbol result");
  return o ? 1 : s ? 0 : -1;
}
function ev(e, t) {
  if (t !== void 0 && Lh(t), e <= Me)
    throw new Error("invalid n length: expected positive n, got " + e);
  if (t !== void 0 && t < 1)
    throw new Error("invalid n length: expected positive bit length, got " + t);
  const n = hl(e);
  if (t !== void 0 && t < n)
    throw new Error(`invalid n length: expected bit length (${n}) >= n.length (${t})`);
  const r = t !== void 0 ? t : n, i = Math.ceil(r / 8);
  return { nBitLength: r, nByteLength: i };
}
const tu = /* @__PURE__ */ new WeakMap();
class qh {
  ORDER;
  BITS;
  BYTES;
  isLE;
  ZERO = Me;
  ONE = Ee;
  _lengths;
  _mod;
  constructor(t, n = {}) {
    if (t <= Ee)
      throw new Error("invalid field: expected ORDER > 1, got " + t);
    let r;
    this.isLE = !1, n != null && typeof n == "object" && (typeof n.BITS == "number" && (r = n.BITS), typeof n.sqrt == "function" && Object.defineProperty(this, "sqrt", { value: n.sqrt, enumerable: !0 }), typeof n.isLE == "boolean" && (this.isLE = n.isLE), n.allowedLengths && (this._lengths = Object.freeze(n.allowedLengths.slice())), typeof n.modFromBytes == "boolean" && (this._mod = n.modFromBytes));
    const { nBitLength: i, nByteLength: o } = ev(t, r);
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
    return Me <= t && t < this.ORDER;
  }
  is0(t) {
    return t === Me;
  }
  // is valid and invertible
  isValidNot0(t) {
    return !this.is0(t) && this.isValid(t);
  }
  isOdd(t) {
    return (t & Ee) === Ee;
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
    return Q0(this, t, n);
  }
  div(t, n) {
    return Ie(t * Qd(n, this.ORDER), this.ORDER);
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
    return Qd(t, this.ORDER);
  }
  sqrt(t) {
    let n = tu.get(this);
    return n || tu.set(this, n = Y0(this.ORDER)), n(this, t);
  }
  toBytes(t) {
    return this.isLE ? Nh(t, this.BYTES) : fl(t, this.BYTES);
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
    let c = o ? Wr(t) : Xi(t);
    if (a && (c = Ie(c, s)), !n && !this.isValid(c))
      throw new Error("invalid field element: outside of range 0..ORDER");
    return c;
  }
  // TODO: we don't need it here, move out to separate fn
  invertBatch(t) {
    return Vh(this, t);
  }
  // We can't move this out because Fp6, Fp12 implement it
  // and it's unclear what to return in there.
  cmov(t, n, r) {
    return Fn(r, "condition"), r ? n : t;
  }
}
Object.freeze(qh.prototype);
function Qs(e, t = {}) {
  return new qh(e, t);
}
function Wh(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  if (e <= Ee)
    throw new Error("field order must be greater than 1");
  const t = hl(e - Ee);
  return Math.ceil(t / 8);
}
function Gh(e) {
  const t = Wh(e);
  return t + Math.ceil(t / 2);
}
function Zh(e, t, n = !1) {
  oe(e);
  const r = e.length, i = Wh(t), o = Math.max(Gh(t), 16);
  if (r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? Wr(e) : Xi(e), a = Ie(s, t - Ee) + Ee;
  return n ? Nh(a, i) : fl(a, i);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Gr = /* @__PURE__ */ BigInt(0), er = /* @__PURE__ */ BigInt(1);
function as(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function Pi(e, t) {
  const n = Vh(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function Yh(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function Ca(e, t) {
  Yh(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = pl(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function nu(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += er);
  const l = t * r, u = l + Math.abs(a) - 1, d = a === 0, f = a < 0, h = t % 2 !== 0;
  return { nextN: c, offset: u, isZero: d, isNeg: f, isNegF: h, offsetF: l };
}
const Ra = /* @__PURE__ */ new WeakMap(), Xh = /* @__PURE__ */ new WeakMap();
function La(e) {
  return Xh.get(e) || 1;
}
function ru(e) {
  if (e !== Gr)
    throw new Error("invalid wNAF");
}
let Jh = class {
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
    for (; n > Gr; )
      n & er && (r = r.add(i)), i = i.double(), n >>= er;
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
    const { windows: r, windowSize: i } = Ca(n, this.bits), o = [];
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
    const s = Ca(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: u, isNeg: d, isNegF: f, offsetF: h } = nu(r, a, s);
      r = c, u ? o = o.add(as(f, n[h])) : i = i.add(as(d, n[l]));
    }
    return ru(r), { p: i, f: o };
  }
  /**
   * Implements unsafe EC multiplication using precomputed tables
   * and w-ary non-adjacent form.
   * @param acc - accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = Ca(t, this.bits);
    for (let s = 0; s < o.windows && r !== Gr; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: u } = nu(r, s, o);
      if (r = a, !l) {
        const d = n[c];
        i = i.add(u ? d.negate() : d);
      }
    }
    return ru(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Ra.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Ra.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = La(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = La(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    Yh(n, this.bits), Xh.set(t, n), Ra.delete(t);
  }
  hasCache(t) {
    return La(t) !== 1;
  }
};
function tv(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > Gr || r > Gr; )
    n & er && (o = o.add(i)), r & er && (s = s.add(i)), i = i.double(), n >>= er, r >>= er;
  return { p1: o, p2: s };
}
function iu(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return J0(t), t;
  } else
    return Qs(e, { isLE: n });
}
function Qh(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > Gr))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = iu(t.p, n.Fp, r), o = iu(t.n, n.Fn, r), a = ["Gx", "Gy", "a", e === "weierstrass" ? "b" : "d"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
function yl(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const kn = /* @__PURE__ */ BigInt(0), $e = /* @__PURE__ */ BigInt(1), Ha = /* @__PURE__ */ BigInt(2), nv = /* @__PURE__ */ BigInt(8);
function rv(e, t, n, r) {
  const i = e.sqr(n), o = e.sqr(r), s = e.add(e.mul(t.a, i), o), a = e.add(e.ONE, e.mul(t.d, e.mul(i, o)));
  return e.eql(s, a);
}
function iv(e, t = {}) {
  const n = t, r = Qh("edwards", e, n, n.FpFnLE), { Fp: i, Fn: o } = r;
  let s = r.CURVE;
  const { h: a } = s;
  ii(n, {}, { uvRatio: "function" });
  const c = Ha << BigInt(o.BYTES * 8) - $e, l = (y) => i.create(y), u = n.uvRatio === void 0 ? (y, w) => {
    try {
      return { isValid: !0, value: i.sqrt(i.div(y, w)) };
    } catch {
      return { isValid: !1, value: kn };
    }
  } : n.uvRatio;
  if (!rv(i, s, s.Gx, s.Gy))
    throw new Error("bad curve params: generator point");
  function d(y, w, m = !1) {
    const g = m ? $e : kn;
    return ss("coordinate " + y, w, g, c), w;
  }
  function f(y) {
    if (!(y instanceof h))
      throw new Error("EdwardsPoint expected");
  }
  class h {
    // base / generator point
    static BASE = new h(s.Gx, s.Gy, $e, l(s.Gx * s.Gy));
    // zero / infinity / identity point
    static ZERO = new h(kn, $e, $e, kn);
    // 0, 1, 1, 0
    // math field
    static Fp = i;
    // scalar field
    static Fn = o;
    X;
    Y;
    Z;
    T;
    constructor(w, m, g, v) {
      this.X = d("x", w), this.Y = d("y", m), this.Z = d("z", g, !0), this.T = d("t", v), Object.freeze(this);
    }
    static CURVE() {
      return s;
    }
    /**
     * Create one extended Edwards point from affine coordinates.
     * Does NOT validate that the point is on-curve or torsion-free.
     * Use `.assertValidity()` on adversarial inputs.
     */
    static fromAffine(w) {
      if (w instanceof h)
        throw new Error("extended point not allowed");
      const { x: m, y: g } = w || {};
      return d("x", m), d("y", g), new h(m, g, $e, l(m * g));
    }
    // Uses algo from RFC8032 5.1.3.
    static fromBytes(w, m = !1) {
      const g = i.BYTES, { a: v, d: L } = s;
      w = bc(oe(w, g, "point")), Fn(m, "zip215");
      const T = bc(w), C = w[g - 1];
      T[g - 1] = C & -129;
      const H = Wr(T), O = m ? c : i.ORDER;
      ss("point.y", H, kn, O);
      const I = l(H * H), U = l(I - $e), z = l(L * I - v);
      let { isValid: E, value: k } = u(U, z);
      if (!E)
        throw new Error("bad point: invalid y coordinate");
      const x = (k & $e) === $e, b = (C & 128) !== 0;
      if (!m && k === kn && b)
        throw new Error("bad point: x=0 and x_0=1");
      return b !== x && (k = l(-k)), h.fromAffine({ x: k, y: H });
    }
    static fromHex(w, m = !1) {
      return h.fromBytes(We(w), m);
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    precompute(w = 8, m = !0) {
      return p.createCache(this, w), m || this.multiply(Ha), this;
    }
    // Useful in fromAffine() - not for fromBytes(), which always created valid points.
    assertValidity() {
      const w = this, { a: m, d: g } = s;
      if (w.is0())
        throw new Error("bad point: ZERO");
      const { X: v, Y: L, Z: T, T: C } = w, H = l(v * v), O = l(L * L), I = l(T * T), U = l(I * I), z = l(H * m), E = l(I * l(z + O)), k = l(U + l(g * l(H * O)));
      if (E !== k)
        throw new Error("bad point: equation left != right (1)");
      const x = l(v * L), b = l(T * C);
      if (x !== b)
        throw new Error("bad point: equation left != right (2)");
    }
    // Compare one point to another.
    equals(w) {
      f(w);
      const { X: m, Y: g, Z: v } = this, { X: L, Y: T, Z: C } = w, H = l(m * C), O = l(L * v), I = l(g * C), U = l(T * v);
      return H === O && I === U;
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
      const { a: w } = s, { X: m, Y: g, Z: v } = this, L = l(m * m), T = l(g * g), C = l(Ha * l(v * v)), H = l(w * L), O = m + g, I = l(l(O * O) - L - T), U = H + T, z = U - C, E = H - T, k = l(I * z), x = l(U * E), b = l(I * E), _ = l(z * U);
      return new h(k, x, _, b);
    }
    // Fast algo for adding 2 Extended Points.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#addition-add-2008-hwcd
    // Cost: 9M + 1*a + 1*d + 7add.
    add(w) {
      f(w);
      const { a: m, d: g } = s, { X: v, Y: L, Z: T, T: C } = this, { X: H, Y: O, Z: I, T: U } = w, z = l(v * H), E = l(L * O), k = l(C * g * U), x = l(T * I), b = l((v + L) * (H + O) - z - E), _ = x - k, $ = x + k, A = l(E - m * z), B = l(b * _), P = l($ * A), R = l(b * A), N = l(_ * $);
      return new h(B, P, N, R);
    }
    subtract(w) {
      return f(w), this.add(w.negate());
    }
    // Constant-time multiplication.
    multiply(w) {
      if (!o.isValidNot0(w))
        throw new RangeError("invalid scalar: expected 1 <= sc < curve.n");
      const { p: m, f: g } = p.cached(this, w, (v) => Pi(h, v));
      return Pi(h, [m, g])[0];
    }
    // Non-constant-time multiplication. Uses double-and-add algorithm.
    // It's faster, but should only be used when you don't care about
    // an exposed private key e.g. sig verification.
    // Keeps the same subgroup-scalar contract: 0 is allowed for public-scalar callers, but
    // n and larger values are rejected instead of being reduced mod n to the identity point.
    multiplyUnsafe(w) {
      if (!o.isValid(w))
        throw new RangeError("invalid scalar: expected 0 <= sc < curve.n");
      return w === kn ? h.ZERO : this.is0() || w === $e ? this : p.unsafe(this, w, (m) => Pi(h, m));
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
      return p.unsafe(this, s.n).is0();
    }
    // Converts Extended point to default (x, y) coordinates.
    // Can accept precomputed Z^-1 - for example, from invertBatch.
    toAffine(w) {
      const m = this;
      let g = w;
      const { X: v, Y: L, Z: T } = m, C = m.is0();
      g == null && (g = C ? nv : i.inv(T));
      const H = l(v * g), O = l(L * g), I = i.mul(T, g);
      if (C)
        return { x: kn, y: $e };
      if (I !== $e)
        throw new Error("invZ was invalid");
      return { x: H, y: O };
    }
    clearCofactor() {
      return a === $e ? this : this.multiplyUnsafe(a);
    }
    toBytes() {
      const { x: w, y: m } = this.toAffine(), g = i.toBytes(m);
      return g[g.length - 1] |= w & $e ? 128 : 0, g;
    }
    toHex() {
      return G(this.toBytes());
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const p = new Jh(h, o.BITS);
  return o.BITS >= 8 && h.BASE.precompute(8), Object.freeze(h.prototype), Object.freeze(h), h;
}
class ov {
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
    Jd();
  }
  static fromHex(t) {
    Jd();
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
function sv(e, t, n = {}) {
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
  const { prehash: o } = i, { BASE: s, Fp: a, Fn: c } = e, l = r.outputLen, u = 2 * a.BYTES;
  if (l !== void 0 && (os(l, "hash.outputLen"), l !== u))
    throw new Error(`hash.outputLen must be ${u}, got ${l}`);
  const d = i.randomBytes === void 0 ? dl : i.randomBytes, f = i.adjustScalarBytes === void 0 ? (E) => E : i.adjustScalarBytes, h = i.domain === void 0 ? (E, k, x) => {
    if (Fn(x, "phflag"), k.length || x)
      throw new Error("Contexts/pre-hash are not supported");
    return E;
  } : i.domain;
  function p(E) {
    return c.create(Wr(E));
  }
  function y(E) {
    const k = H.secretKey;
    oe(E, H.secretKey, "secretKey");
    const x = oe(r(E), 2 * k, "hashedSecretKey"), b = f(x.slice(0, k)), _ = x.slice(k, 2 * k), $ = p(b);
    return { head: b, prefix: _, scalar: $ };
  }
  function w(E) {
    const { head: k, prefix: x, scalar: b } = y(E), _ = s.multiply(b), $ = _.toBytes();
    return { head: k, prefix: x, scalar: b, point: _, pointBytes: $ };
  }
  function m(E) {
    return w(E).pointBytes;
  }
  function g(E = Uint8Array.of(), ...k) {
    const x = dt(...k);
    return p(r(h(x, oe(E, void 0, "context"), !!o)));
  }
  function v(E, k, x = {}) {
    E = oe(E, void 0, "message"), o && (E = o(E));
    const { prefix: b, scalar: _, pointBytes: $ } = w(k), A = g(x.context, b, E), B = s.multiply(A).toBytes(), P = g(x.context, B, $, E), R = c.create(A + P * _);
    if (!c.isValid(R))
      throw new Error("sign failed: invalid s");
    const N = dt(B, c.toBytes(R));
    return oe(N, H.signature, "result");
  }
  const L = {
    zip215: i.zip215
  };
  function T(E, k, x, b = L) {
    const { context: _ } = b, $ = b.zip215 === void 0 ? !!L.zip215 : b.zip215, A = H.signature;
    E = oe(E, A, "signature"), k = oe(k, void 0, "message"), x = oe(x, H.publicKey, "publicKey"), $ !== void 0 && Fn($, "zip215"), o && (k = o(k));
    const B = A / 2, P = E.subarray(0, B), R = Wr(E.subarray(B, A));
    let N, K, D;
    try {
      N = e.fromBytes(x, $), K = e.fromBytes(P, $), D = s.multiplyUnsafe(R);
    } catch {
      return !1;
    }
    if (!$ && N.isSmallOrder())
      return !1;
    const M = g(_, P, x, k);
    return K.add(N.multiplyUnsafe(M)).subtract(D).clearCofactor().is0();
  }
  const C = a.BYTES, H = {
    secretKey: C,
    publicKey: C,
    signature: 2 * C,
    seed: C
  };
  function O(E) {
    return E = E === void 0 ? d(H.seed) : E, oe(E, H.seed, "seed");
  }
  function I(E) {
    return ll(E) && E.length === H.secretKey;
  }
  function U(E, k) {
    try {
      return !!e.fromBytes(E, k === void 0 ? L.zip215 : k);
    } catch {
      return !1;
    }
  }
  const z = {
    getExtendedPublicKey: w,
    randomSecretKey: O,
    isValidSecretKey: I,
    isValidPublicKey: U,
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
      const { y: k } = e.fromBytes(E), x = H.publicKey, b = x === 32;
      if (!b && x !== 57)
        throw new Error("only defined for 25519 and 448");
      const _ = b ? a.div($e + k, $e - k) : a.div(k - $e, k + $e);
      return a.toBytes(_);
    },
    toMontgomerySecret(E) {
      const k = H.secretKey;
      oe(E, k);
      const x = r(E.subarray(0, k));
      return f(x).subarray(0, k);
    }
  };
  return Object.freeze(H), Object.freeze(z), Object.freeze({
    keygen: yl(O, m),
    getPublicKey: m,
    sign: v,
    verify: T,
    utils: z,
    Point: e,
    lengths: H
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const av = /* @__PURE__ */ BigInt(0), $i = /* @__PURE__ */ BigInt(1), ou = /* @__PURE__ */ BigInt(2), cv = /* @__PURE__ */ BigInt(5), lv = /* @__PURE__ */ BigInt(8), Ri = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed"), ep = {
  p: Ri,
  n: BigInt("0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"),
  h: lv,
  a: BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffec"),
  d: BigInt("0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3"),
  Gx: BigInt("0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a"),
  Gy: BigInt("0x6666666666666666666666666666666666666666666666666666666666666658")
};
function dv(e) {
  const t = BigInt(10), n = BigInt(20), r = BigInt(40), i = BigInt(80), o = Ri, a = e * e % o * e % o, c = ge(a, ou, o) * a % o, l = ge(c, $i, o) * e % o, u = ge(l, cv, o) * l % o, d = ge(u, t, o) * u % o, f = ge(d, n, o) * d % o, h = ge(f, r, o) * f % o, p = ge(h, i, o) * h % o, y = ge(p, i, o) * h % o, w = ge(y, t, o) * u % o;
  return { pow_p_5_8: ge(w, ou, o) * e % o, b2: a };
}
function uv(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const vc = /* @__PURE__ */ BigInt("19681161376707505956807079304988542015446066515923890162744021073123829784752");
function tp(e, t) {
  const n = Ri, r = Ie(t * t * t, n), i = Ie(r * r * t, n), o = dv(e * i).pow_p_5_8;
  let s = Ie(e * r * o, n);
  const a = Ie(t * s * s, n), c = s, l = Ie(s * vc, n), u = a === e, d = a === Ie(-e, n), f = a === Ie(-e * vc, n);
  return u && (s = c), (d || f) && (s = l), Gn(s, n) && (s = Ie(-s, n)), { isValid: u || d, value: s };
}
const tr = /* @__PURE__ */ iv(ep, { uvRatio: tp }), Zn = tr.Fp, fv = tr.Fn;
function hv(e) {
  return sv(tr, gc, Object.assign({ adjustScalarBytes: uv, zip215: !0 }, e));
}
const kt = /* @__PURE__ */ hv({}), su = vc, pv = /* @__PURE__ */ BigInt("54469307008909316920995813868745141605393597292927456921205312896311721017578"), au = (e) => tp($i, e), wv = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), yv = (e) => Zn.create(Wr(e) & wv);
class Qe extends ov {
  // Do NOT change syntax: the following gymnastics is done,
  // because typescript strips comments, which makes bundlers disable tree-shaking.
  // prettier-ignore
  static BASE = new Qe(tr.BASE);
  // prettier-ignore
  static ZERO = new Qe(tr.ZERO);
  // prettier-ignore
  static Fp = Zn;
  // prettier-ignore
  static Fn = fv;
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
    return new Qe(tr.fromAffine(t));
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
    const { a: n, d: r } = ep, i = Ri, o = (L) => Zn.create(L), s = yv(t);
    if (!K0(Zn.toBytes(s), t) || Gn(s, i))
      throw new Error("invalid ristretto255 encoding 1");
    const a = o(s * s), c = o($i + n * a), l = o($i - n * a), u = o(c * c), d = o(l * l), f = o(n * r * u - d), { isValid: h, value: p } = au(o(f * d)), y = o(p * l), w = o(p * y * f);
    let m = o((s + s) * y);
    Gn(m, i) && (m = o(-m));
    const g = o(c * w), v = o(m * g);
    if (!h || Gn(v, i) || g === av)
      throw new Error("invalid ristretto255 encoding 2");
    return new Qe(new tr(m, g, $i, v));
  }
  /**
   * Converts ristretto-encoded string to ristretto point.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-decode).
   * @param hex - Ristretto-encoded 32 bytes. Not every 32-byte string is valid ristretto encoding
   */
  static fromHex(t) {
    return Qe.fromBytes(sl(t));
  }
  /**
   * Encodes ristretto point to Uint8Array.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-encode).
   */
  toBytes() {
    let { X: t, Y: n, Z: r, T: i } = this.ep;
    const o = Ri, s = (w) => Zn.create(w), a = s(s(r + n) * s(r - n)), c = s(t * n), l = s(c * c), { value: u } = au(s(a * l)), d = s(u * a), f = s(u * c), h = s(d * f * i);
    let p;
    if (Gn(i * h, o)) {
      let w = s(n * su), m = s(t * su);
      t = w, n = m, p = s(d * pv);
    } else
      p = f;
    Gn(t * h, o) && (n = s(-n));
    let y = s((r - n) * p);
    return Gn(y, o) && (y = s(-y)), Zn.toBytes(y);
  }
  /**
   * Compares two Ristretto points.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-equals).
   */
  equals(t) {
    this.assertSame(t);
    const { X: n, Y: r } = this.ep, { X: i, Y: o } = t.ep, s = (l) => Zn.create(l), a = s(n * o) === s(r * i), c = s(r * o) === s(n * i);
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
class cu {
  oHash;
  iHash;
  blockLen;
  outputLen;
  canXOF = !1;
  finished = !1;
  destroyed = !1;
  constructor(t, n) {
    if (Ph(t), zt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
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
    return qr(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    qr(this), il(t, this), this.finished = !0;
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
const xc = /* @__PURE__ */ (() => {
  const e = ((t, n, r) => new cu(t, n).update(r).digest());
  return e.create = (t, n) => new cu(t, n), e;
})();
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const lu = (e, t) => (e + (e >= 0 ? t : -t) / np) / t;
function gv(e, t, n) {
  ss("scalar", e, Rt, n);
  const [[r, i], [o, s]] = t, a = lu(s * e, n), c = lu(-i * e, n);
  let l = e - a * r - c * o, u = -a * i - c * s;
  const d = l < Rt, f = u < Rt;
  d && (l = -l), f && (u = -u);
  const h = pl(Math.ceil(hl(n) / 2)) + Un;
  if (l < Rt || l >= h || u < Rt || u >= h)
    throw new Error("splitScalar (endomorphism): failed for k");
  return { k1neg: d, k1: l, k2neg: f, k2: u };
}
function kc(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function Na(e, t) {
  ii(e);
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Fn(n.lowS, "lowS"), Fn(n.prehash, "prehash"), n.format !== void 0 && kc(n.format), n;
}
let mv = class extends Error {
  constructor(t = "") {
    super(t);
  }
};
const gt = {
  // asn.1 DER encoding utils
  Err: mv,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = gt;
      if (os(e, "tag"), e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (typeof t != "string")
        throw new TypeError('"data" expected string, got type=' + typeof t);
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = po(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? po(i.length / 2 | 128) : "";
      return po(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = gt;
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
        for (const u of l)
          s = s << 8 | u;
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
      const { Err: t } = gt;
      if (ul(e), e < Rt)
        throw new t("integer: negative integers are not allowed");
      let n = po(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = gt;
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
    const { Err: t, _int: n, _tlv: r } = gt, i = oe(e, void 0, "signature"), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: u } = r.decode(2, c);
    if (u.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = gt, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
};
Object.freeze(gt._tlv);
Object.freeze(gt._int);
Object.freeze(gt);
const Rt = /* @__PURE__ */ BigInt(0), Un = /* @__PURE__ */ BigInt(1), np = /* @__PURE__ */ BigInt(2), wo = /* @__PURE__ */ BigInt(3), bv = /* @__PURE__ */ BigInt(4);
function vv(e, t = {}) {
  const n = Qh("weierstrass", e, t), r = n.Fp, i = n.Fn;
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
  const u = ip(r, i);
  function d() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function f(z, E, k) {
    if (l && E.is0())
      return Uint8Array.of(0);
    const { x, y: b } = E.toAffine(), _ = r.toBytes(x);
    if (Fn(k, "isCompressed"), k) {
      d();
      const $ = !r.isOdd(b);
      return dt(rp($), _);
    } else
      return dt(Uint8Array.of(4), _, r.toBytes(b));
  }
  function h(z) {
    oe(z, void 0, "Point");
    const { publicKey: E, publicKeyUncompressed: k } = u, x = z.length, b = z[0], _ = z.subarray(1);
    if (l && x === 1 && b === 0)
      return { x: r.ZERO, y: r.ZERO };
    if (x === E && (b === 2 || b === 3)) {
      const $ = r.fromBytes(_);
      if (!r.isValid($))
        throw new Error("bad point: is not on curve, wrong x");
      const A = w($);
      let B;
      try {
        B = r.sqrt(A);
      } catch (N) {
        const K = N instanceof Error ? ": " + N.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + K);
      }
      d();
      const P = r.isOdd(B);
      return (b & 1) === 1 !== P && (B = r.neg(B)), { x: $, y: B };
    } else if (x === k && b === 4) {
      const $ = r.BYTES, A = r.fromBytes(_.subarray(0, $)), B = r.fromBytes(_.subarray($, $ * 2));
      if (!m(A, B))
        throw new Error("bad point: is not on curve");
      return { x: A, y: B };
    } else
      throw new Error(`bad point: got length ${x}, expected compressed=${E} or uncompressed=${k}`);
  }
  const p = t.toBytes === void 0 ? f : t.toBytes, y = t.fromBytes === void 0 ? h : t.fromBytes;
  function w(z) {
    const E = r.sqr(z), k = r.mul(E, z);
    return r.add(r.add(k, r.mul(z, o.a)), o.b);
  }
  function m(z, E) {
    const k = r.sqr(E), x = w(z);
    return r.eql(k, x);
  }
  if (!m(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const g = r.mul(r.pow(o.a, wo), bv), v = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(g, v)))
    throw new Error("bad curve params: a or b");
  function L(z, E, k = !1) {
    if (!r.isValid(E) || k && r.is0(E))
      throw new Error(`bad point coordinate ${z}`);
    return E;
  }
  function T(z) {
    if (!(z instanceof O))
      throw new Error("Weierstrass Point expected");
  }
  function C(z) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return gv(z, c.basises, i.ORDER);
  }
  function H(z, E, k, x, b) {
    return k = new O(r.mul(k.X, z), k.Y, k.Z), E = as(x, E), k = as(b, k), E.add(k);
  }
  class O {
    // base / generator point
    static BASE = new O(o.Gx, o.Gy, r.ONE);
    // zero / infinity / identity point
    static ZERO = new O(r.ZERO, r.ONE, r.ZERO);
    // 0, 1, 0
    // math field
    static Fp = r;
    // scalar field
    static Fn = i;
    X;
    Y;
    Z;
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    constructor(E, k, x) {
      this.X = L("x", E), this.Y = L("y", k, !0), this.Z = L("z", x), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(E) {
      const { x: k, y: x } = E || {};
      if (!E || !r.isValid(k) || !r.isValid(x))
        throw new Error("invalid affine point");
      if (E instanceof O)
        throw new Error("projective point not allowed");
      return r.is0(k) && r.is0(x) ? O.ZERO : new O(k, x, r.ONE);
    }
    static fromBytes(E) {
      const k = O.fromAffine(y(oe(E, void 0, "point")));
      return k.assertValidity(), k;
    }
    static fromHex(E) {
      return O.fromBytes(We(E));
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
    precompute(E = 8, k = !0) {
      return U.createCache(this, E), k || this.multiply(wo), this;
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
      const { x: k, y: x } = E.toAffine();
      if (!r.isValid(k) || !r.isValid(x))
        throw new Error("bad point: x or y not field elements");
      if (!m(k, x))
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
      T(E);
      const { X: k, Y: x, Z: b } = this, { X: _, Y: $, Z: A } = E, B = r.eql(r.mul(k, A), r.mul(_, b)), P = r.eql(r.mul(x, A), r.mul($, b));
      return B && P;
    }
    /** Flips point to one corresponding to (x, -y) in Affine coordinates. */
    negate() {
      return new O(this.X, r.neg(this.Y), this.Z);
    }
    // Renes-Costello-Batina exception-free doubling formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 3
    // Cost: 8M + 3S + 3*a + 2*b3 + 15add.
    double() {
      const { a: E, b: k } = o, x = r.mul(k, wo), { X: b, Y: _, Z: $ } = this;
      let A = r.ZERO, B = r.ZERO, P = r.ZERO, R = r.mul(b, b), N = r.mul(_, _), K = r.mul($, $), D = r.mul(b, _);
      return D = r.add(D, D), P = r.mul(b, $), P = r.add(P, P), A = r.mul(E, P), B = r.mul(x, K), B = r.add(A, B), A = r.sub(N, B), B = r.add(N, B), B = r.mul(A, B), A = r.mul(D, A), P = r.mul(x, P), K = r.mul(E, K), D = r.sub(R, K), D = r.mul(E, D), D = r.add(D, P), P = r.add(R, R), R = r.add(P, R), R = r.add(R, K), R = r.mul(R, D), B = r.add(B, R), K = r.mul(_, $), K = r.add(K, K), R = r.mul(K, D), A = r.sub(A, R), P = r.mul(K, N), P = r.add(P, P), P = r.add(P, P), new O(A, B, P);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(E) {
      T(E);
      const { X: k, Y: x, Z: b } = this, { X: _, Y: $, Z: A } = E;
      let B = r.ZERO, P = r.ZERO, R = r.ZERO;
      const N = o.a, K = r.mul(o.b, wo);
      let D = r.mul(k, _), M = r.mul(x, $), F = r.mul(b, A), q = r.add(k, x), j = r.add(_, $);
      q = r.mul(q, j), j = r.add(D, M), q = r.sub(q, j), j = r.add(k, b);
      let W = r.add(_, A);
      return j = r.mul(j, W), W = r.add(D, F), j = r.sub(j, W), W = r.add(x, b), B = r.add($, A), W = r.mul(W, B), B = r.add(M, F), W = r.sub(W, B), R = r.mul(N, j), B = r.mul(K, F), R = r.add(B, R), B = r.sub(M, R), R = r.add(M, R), P = r.mul(B, R), M = r.add(D, D), M = r.add(M, D), F = r.mul(N, F), j = r.mul(K, j), M = r.add(M, F), F = r.sub(D, F), F = r.mul(N, F), j = r.add(j, F), D = r.mul(M, j), P = r.add(P, D), D = r.mul(W, j), B = r.mul(q, B), B = r.sub(B, D), D = r.mul(q, M), R = r.mul(W, R), R = r.add(R, D), new O(B, P, R);
    }
    subtract(E) {
      return T(E), this.add(E.negate());
    }
    is0() {
      return this.equals(O.ZERO);
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
      const { endo: k } = t;
      if (!i.isValidNot0(E))
        throw new RangeError("invalid scalar: out of range");
      let x, b;
      const _ = ($) => U.cached(this, $, (A) => Pi(O, A));
      if (k) {
        const { k1neg: $, k1: A, k2neg: B, k2: P } = C(E), { p: R, f: N } = _(A), { p: K, f: D } = _(P);
        b = N.add(D), x = H(k.beta, R, K, $, B);
      } else {
        const { p: $, f: A } = _(E);
        x = $, b = A;
      }
      return Pi(O, [x, b])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(E) {
      const { endo: k } = t, x = this, b = E;
      if (!i.isValid(b))
        throw new RangeError("invalid scalar: out of range");
      if (b === Rt || x.is0())
        return O.ZERO;
      if (b === Un)
        return x;
      if (U.hasCache(this))
        return this.multiply(b);
      if (k) {
        const { k1neg: _, k1: $, k2neg: A, k2: B } = C(b), { p1: P, p2: R } = tv(O, x, $, B);
        return H(k.beta, P, R, _, A);
      } else
        return U.unsafe(x, b);
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * (X, Y, Z) ∋ (x=X/Z, y=Y/Z).
     * @param invertedZ - Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(E) {
      const k = this;
      let x = E;
      const { X: b, Y: _, Z: $ } = k;
      if (r.eql($, r.ONE))
        return { x: b, y: _ };
      const A = k.is0();
      x == null && (x = A ? r.ONE : r.inv($));
      const B = r.mul(b, x), P = r.mul(_, x), R = r.mul($, x);
      if (A)
        return { x: r.ZERO, y: r.ZERO };
      if (!r.eql(R, r.ONE))
        throw new Error("invZ was invalid");
      return { x: B, y: P };
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: E } = t;
      return s === Un ? !0 : E ? E(O, this) : U.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: E } = t;
      return s === Un ? this : E ? E(O, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return s === Un ? this.is0() : this.clearCofactor().is0();
    }
    toBytes(E = !0) {
      return Fn(E, "isCompressed"), this.assertValidity(), p(O, this, E);
    }
    toHex(E = !0) {
      return G(this.toBytes(E));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const I = i.BITS, U = new Jh(O, t.endo ? Math.ceil(I / 2) : I);
  return I >= 8 && O.BASE.precompute(8), Object.freeze(O.prototype), Object.freeze(O), O;
}
function rp(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function ip(e, t) {
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
function xv(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes === void 0 ? dl : t.randomBytes, i = Object.assign(ip(e.Fp, n), {
    seed: Math.max(Gh(n.ORDER), 16)
  });
  function o(h) {
    try {
      const p = n.fromBytes(h);
      return n.isValidNot0(p);
    } catch {
      return !1;
    }
  }
  function s(h, p) {
    const { publicKey: y, publicKeyUncompressed: w } = i;
    try {
      const m = h.length;
      return p === !0 && m !== y || p === !1 && m !== w ? !1 : !!e.fromBytes(h);
    } catch {
      return !1;
    }
  }
  function a(h) {
    return h = h === void 0 ? r(i.seed) : h, Zh(oe(h, i.seed, "seed"), n.ORDER);
  }
  function c(h, p = !0) {
    return e.BASE.multiply(n.fromBytes(h)).toBytes(p);
  }
  function l(h) {
    const { secretKey: p, publicKey: y, publicKeyUncompressed: w } = i, m = n._lengths;
    if (!ll(h))
      return;
    const g = oe(h, void 0, "key").length, v = g === y || g === w, L = g === p || !!m?.includes(g);
    if (!(v && L))
      return v;
  }
  function u(h, p, y = !0) {
    if (l(h) === !0)
      throw new Error("first arg must be private key");
    if (l(p) === !1)
      throw new Error("second arg must be public key");
    const w = n.fromBytes(h);
    return e.fromBytes(p).multiply(w).toBytes(y);
  }
  const d = {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a
  }, f = yl(a, c);
  return Object.freeze(d), Object.freeze(i), Object.freeze({ getPublicKey: c, getSharedSecret: u, keygen: f, Point: e, utils: d, lengths: i });
}
function kv(e, t, n = {}) {
  const r = t;
  Ph(r), ii(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  }), n = Object.assign({}, n);
  const i = n.randomBytes === void 0 ? dl : n.randomBytes, o = n.hmac === void 0 ? (b, _) => xc(r, b, _) : n.hmac, { Fp: s, Fn: a } = e, { ORDER: c, BITS: l } = a, { keygen: u, getPublicKey: d, getSharedSecret: f, utils: h, lengths: p } = xv(e, n), y = {
    prehash: !0,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !0,
    format: "compact",
    extraEntropy: !1
  }, w = c * np + Un < s.ORDER;
  function m(b) {
    const _ = c >> Un;
    return b > _;
  }
  function g(b, _) {
    if (!a.isValidNot0(_))
      throw new Error(`invalid signature ${b}: out of range 1..Point.Fn.ORDER`);
    return _;
  }
  function v() {
    if (w)
      throw new Error('"recovered" sig type is not supported for cofactor >2 curves');
  }
  function L(b, _) {
    kc(_);
    const $ = p.signature, A = _ === "compact" ? $ : _ === "recovered" ? $ + 1 : void 0;
    return oe(b, A);
  }
  class T {
    r;
    s;
    recovery;
    constructor(_, $, A) {
      if (this.r = g("r", _), this.s = g("s", $), A != null) {
        if (v(), ![0, 1, 2, 3].includes(A))
          throw new Error("invalid recovery id");
        this.recovery = A;
      }
      Object.freeze(this);
    }
    static fromBytes(_, $ = y.format) {
      L(_, $);
      let A;
      if ($ === "der") {
        const { r: N, s: K } = gt.toSig(oe(_));
        return new T(N, K);
      }
      $ === "recovered" && (A = _[0], $ = "compact", _ = _.subarray(1));
      const B = p.signature / 2, P = _.subarray(0, B), R = _.subarray(B, B * 2);
      return new T(a.fromBytes(P), a.fromBytes(R), A);
    }
    static fromHex(_, $) {
      return this.fromBytes(We(_), $);
    }
    assertRecovery() {
      const { recovery: _ } = this;
      if (_ == null)
        throw new Error("invalid recovery id: must be present");
      return _;
    }
    addRecoveryBit(_) {
      return new T(this.r, this.s, _);
    }
    // Unlike the top-level helper below, this method expects a digest that has
    // already been hashed to the curve's message representative.
    recoverPublicKey(_) {
      const { r: $, s: A } = this, B = this.assertRecovery(), P = B === 2 || B === 3 ? $ + c : $;
      if (!s.isValid(P))
        throw new Error("invalid recovery id: sig.r+curve.n != R.x");
      const R = s.toBytes(P), N = e.fromBytes(dt(rp((B & 1) === 0), R)), K = a.inv(P), D = H(oe(_, void 0, "msgHash")), M = a.create(-D * K), F = a.create(A * K), q = e.BASE.multiplyUnsafe(M).add(N.multiplyUnsafe(F));
      if (q.is0())
        throw new Error("invalid recovery: point at infinify");
      return q.assertValidity(), q;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return m(this.s);
    }
    toBytes(_ = y.format) {
      if (kc(_), _ === "der")
        return We(gt.hexFromSig(this));
      const { r: $, s: A } = this, B = a.toBytes($), P = a.toBytes(A);
      return _ === "recovered" ? (v(), dt(Uint8Array.of(this.assertRecovery()), B, P)) : dt(B, P);
    }
    toHex(_) {
      return G(this.toBytes(_));
    }
  }
  Object.freeze(T.prototype), Object.freeze(T);
  const C = n.bits2int === void 0 ? function(_) {
    if (_.length > 8192)
      throw new Error("input is too large");
    const $ = Xi(_), A = _.length * 8 - l;
    return A > 0 ? $ >> BigInt(A) : $;
  } : n.bits2int, H = n.bits2int_modN === void 0 ? function(_) {
    return a.create(C(_));
  } : n.bits2int_modN, O = pl(l);
  function I(b) {
    return ss("num < 2^" + l, b, Rt, O), a.toBytes(b);
  }
  function U(b, _) {
    return oe(b, void 0, "message"), _ ? oe(r(b), void 0, "prehashed message") : b;
  }
  function z(b, _, $) {
    const { lowS: A, prehash: B, extraEntropy: P } = Na($, y);
    b = U(b, B);
    const R = H(b), N = a.fromBytes(_);
    if (!a.isValidNot0(N))
      throw new Error("invalid private key");
    const K = [I(N), I(R)];
    if (P != null && P !== !1) {
      const q = P === !0 ? i(p.secretKey) : P;
      K.push(oe(q, void 0, "extraEntropy"));
    }
    const D = dt(...K), M = R;
    function F(q) {
      const j = C(q);
      if (!a.isValidNot0(j))
        return;
      const W = a.inv(j), X = e.BASE.multiply(j).toAffine(), we = a.create(X.x);
      if (we === Rt)
        return;
      const se = a.create(W * a.create(M + we * N));
      if (se === Rt)
        return;
      let xe = (X.x === we ? 0 : 2) | Number(X.y & Un), Ze = se;
      return A && m(se) && (Ze = a.neg(se), xe ^= 1), new T(we, Ze, w ? void 0 : xe);
    }
    return { seed: D, k2sig: F };
  }
  function E(b, _, $ = {}) {
    const { seed: A, k2sig: B } = z(b, _, $);
    return V0(r.outputLen, a.BYTES, o)(A, B).toBytes($.format);
  }
  function k(b, _, $, A = {}) {
    const { lowS: B, prehash: P, format: R } = Na(A, y);
    if ($ = oe($, void 0, "publicKey"), _ = U(_, P), !ll(b)) {
      const N = b instanceof T ? ", use sig.toBytes()" : "";
      throw new Error("verify expects Uint8Array signature" + N);
    }
    L(b, R);
    try {
      const N = T.fromBytes(b, R), K = e.fromBytes($);
      if (B && N.hasHighS())
        return !1;
      const { r: D, s: M } = N, F = H(_), q = a.inv(M), j = a.create(F * q), W = a.create(D * q), X = e.BASE.multiplyUnsafe(j).add(K.multiplyUnsafe(W));
      return X.is0() ? !1 : a.create(X.x) === D;
    } catch {
      return !1;
    }
  }
  function x(b, _, $ = {}) {
    const { prehash: A } = Na($, y);
    return _ = U(_, A), T.fromBytes(b, "recovered").recoverPublicKey(_).toBytes();
  }
  return Object.freeze({
    keygen: u,
    getPublicKey: d,
    getSharedSecret: f,
    utils: h,
    lengths: p,
    Point: e,
    sign: E,
    verify: k,
    recoverPublicKey: x,
    Signature: T,
    hash: r
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const ea = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, Ev = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, Av = /* @__PURE__ */ BigInt(0), Ec = /* @__PURE__ */ BigInt(2);
function Sv(e) {
  const t = ea.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, u = l * l * e % t, d = ge(u, n, t) * u % t, f = ge(d, n, t) * u % t, h = ge(f, Ec, t) * l % t, p = ge(h, i, t) * h % t, y = ge(p, o, t) * p % t, w = ge(y, a, t) * y % t, m = ge(w, c, t) * w % t, g = ge(m, a, t) * y % t, v = ge(g, n, t) * u % t, L = ge(v, s, t) * p % t, T = ge(L, r, t) * l % t, C = ge(T, Ec, t);
  if (!cs.eql(cs.sqr(C), e))
    throw new Error("Cannot find square root");
  return C;
}
const cs = Qs(ea.p, { sqrt: Sv }), yr = /* @__PURE__ */ vv(ea, {
  Fp: cs,
  endo: Ev
}), fe = /* @__PURE__ */ kv(yr, ue), du = {};
function ls(e, ...t) {
  let n = du[e];
  if (n === void 0) {
    const r = ue(F0(e));
    n = dt(r, r), du[e] = n;
  }
  return ue(dt(n, ...t));
}
const ta = (e) => e.toBytes(!0).slice(1), na = (e) => e % Ec === Av;
function op(e) {
  const { Fn: t, BASE: n } = yr, r = t.fromBytes(e), i = n.multiply(r);
  return { scalar: na(i.y) ? r : t.neg(r), bytes: ta(i) };
}
function sp(e) {
  const t = cs;
  if (!t.isValidNot0(e))
    throw new Error("invalid x: Fail if x ≥ p");
  const n = t.create(e * e), r = t.create(n * e + BigInt(7));
  let i = t.sqrt(r);
  na(i) || (i = t.neg(i));
  const o = yr.fromAffine({ x: e, y: i });
  return o.assertValidity(), o;
}
const zr = Xi;
function ap(...e) {
  return yr.Fn.create(zr(ls("BIP0340/challenge", ...e)));
}
function uu(e) {
  return op(e).bytes;
}
function _v(e, t, n = al(32)) {
  const { Fn: r, BASE: i } = yr, o = oe(e, void 0, "message"), { bytes: s, scalar: a } = op(t), c = oe(n, 32, "auxRand"), l = r.toBytes(a ^ zr(ls("BIP0340/aux", c))), u = ls("BIP0340/nonce", l, s, o), d = r.create(zr(u));
  if (d === 0n)
    throw new Error("sign failed: k is zero");
  const f = i.multiply(d), h = na(f.y) ? d : r.neg(d), p = ta(f), y = ap(p, s, o), w = new Uint8Array(64);
  if (w.set(p, 0), w.set(r.toBytes(r.create(h + y * a)), 32), !cp(w, o, s))
    throw new Error("sign: Invalid signature produced");
  return w;
}
function cp(e, t, n) {
  const { Fp: r, Fn: i, BASE: o } = yr, s = oe(e, 64, "signature"), a = oe(t, void 0, "message"), c = oe(n, 32, "publicKey");
  try {
    const l = sp(zr(c)), u = zr(s.subarray(0, 32));
    if (!r.isValidNot0(u))
      return !1;
    const d = zr(s.subarray(32, 64));
    if (!i.isValidNot0(d))
      return !1;
    const f = ap(i.toBytes(u), ta(l), a), h = o.multiplyUnsafe(d).add(l.multiplyUnsafe(i.neg(f))), { x: p, y } = h.toAffine();
    return !(h.is0() || !na(y) || p !== u);
  } catch {
    return !1;
  }
}
const Cn = /* @__PURE__ */ (() => {
  const n = (r) => (r = r === void 0 ? al(48) : r, Zh(r, ea.n));
  return Object.freeze({
    keygen: yl(n, uu),
    getPublicKey: uu,
    sign: _v,
    verify: cp,
    Point: yr,
    utils: Object.freeze({
      randomSecretKey: n,
      taggedHash: ls,
      lift_x: sp,
      pointToBytes: ta
    }),
    lengths: Object.freeze({
      secretKey: 32,
      publicKey: 32,
      publicKeyHasPrefix: !1,
      signature: 64,
      seed: 48
    })
  });
})(), Iv = /* @__PURE__ */ Uint8Array.from([
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
]), lp = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), Bv = lp.map((e) => (9 * e + 5) % 16), dp = /* @__PURE__ */ (() => {
  const n = [[lp], [Bv]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => Iv[o]));
  return n;
})(), up = dp[0], fp = dp[1], hp = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), Tv = /* @__PURE__ */ up.map((e, t) => e.map((n) => hp[t][n])), Pv = /* @__PURE__ */ fp.map((e, t) => e.map((n) => hp[t][n])), $v = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), Ov = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function fu(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const yo = /* @__PURE__ */ new Uint32Array(16);
class Cv extends cl {
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
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, u = l, d = this.h4 | 0, f = d;
    for (let h = 0; h < 5; h++) {
      const p = 4 - h, y = $v[h], w = Ov[h], m = up[h], g = fp[h], v = Tv[h], L = Pv[h];
      for (let T = 0; T < 16; T++) {
        const C = lo(r + fu(h, o, a, l) + yo[m[T]] + y, v[T]) + d | 0;
        r = d, d = l, l = lo(a, 10) | 0, a = o, o = C;
      }
      for (let T = 0; T < 16; T++) {
        const C = lo(i + fu(p, s, c, u) + yo[g[T]] + w, L[T]) + f | 0;
        i = f, f = u, u = lo(c, 10) | 0, c = s, s = C;
      }
    }
    this.set(this.h1 + a + u | 0, this.h2 + l + f | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    Mt(yo);
  }
  destroy() {
    this.destroyed = !0, Mt(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const gl = /* @__PURE__ */ Js(() => new Cv()), Rv = BigInt(0), pi = BigInt(1), Lv = BigInt(2), Hv = BigInt(7), Nv = BigInt(256), Uv = BigInt(113), pp = [], wp = [], yp = [];
for (let e = 0, t = pi, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], pp.push(2 * (5 * r + n)), wp.push((e + 1) * (e + 2) / 2 % 64);
  let i = Rv;
  for (let o = 0; o < 7; o++)
    t = (t << pi ^ (t >> Hv) * Uv) % Nv, t & Lv && (i ^= pi << (pi << BigInt(o)) - pi);
  yp.push(i);
}
const gp = Ch(yp, !0), Dv = gp[0], zv = gp[1], hu = (e, t, n) => n > 32 ? I0(e, t, n) : S0(e, t, n), pu = (e, t, n) => n > 32 ? B0(e, t, n) : _0(e, t, n);
function Mv(e, t = 24) {
  if (Dt(t, "rounds"), t < 1 || t > 24)
    throw new Error('"rounds" expected integer 1..24');
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], u = n[c + 1], d = hu(l, u, 1) ^ n[a], f = pu(l, u, 1) ^ n[a + 1];
      for (let h = 0; h < 50; h += 10)
        e[s + h] ^= d, e[s + h + 1] ^= f;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = wp[s], c = hu(i, o, a), l = pu(i, o, a), u = pp[s];
      i = e[u], o = e[u + 1], e[u] = c, e[u + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      const a = e[s], c = e[s + 1], l = e[s + 2], u = e[s + 3];
      e[s] ^= ~e[s + 2] & e[s + 4], e[s + 1] ^= ~e[s + 3] & e[s + 5], e[s + 2] ^= ~e[s + 4] & e[s + 6], e[s + 3] ^= ~e[s + 5] & e[s + 7], e[s + 4] ^= ~e[s + 6] & e[s + 8], e[s + 5] ^= ~e[s + 7] & e[s + 9], e[s + 6] ^= ~e[s + 8] & a, e[s + 7] ^= ~e[s + 9] & c, e[s + 8] ^= ~a & l, e[s + 9] ^= ~c & u;
    }
    e[0] ^= Dv[r], e[1] ^= zv[r];
  }
  Mt(n);
}
let Kv = class mp {
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
    this.state = new Uint8Array(200), this.state32 = y0(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    Wd(this.state32), Mv(this.state32, this.rounds), Wd(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    qr(this), zt(t);
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
    qr(this, !1), zt(t), this.finish();
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
    if (il(t, this), this.finished)
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
    return t ||= new mp(n, r, i, s, o), t.blockLen = n, t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.canXOF = this.canXOF, t.destroyed = this.destroyed, t;
  }
};
const Fv = (e, t, n, r = {}) => Js(() => new Kv(t, e, n), r), ds = /* @__PURE__ */ Fv(1, 136, 32), jv = 1024 * 1024, bp = Object.getPrototypeOf(Uint8Array.prototype), Vv = Object.getOwnPropertyDescriptor(bp, Symbol.toStringTag).get, qv = Object.getOwnPropertyDescriptor(bp, "byteLength").get, Wv = Uint8Array.prototype.set;
function vp(e) {
  if (!Number.isSafeInteger(e) || e < 0) throw new Error("exact message byte length is invalid");
  if (e > jv) throw new Error("exact message exceeds the 1 MiB decoded limit");
}
function xp(e) {
  const t = e.startsWith("0x") ? 2 : 0;
  vp(Math.ceil((e.length - t) / 2));
}
function ml(e) {
  if (typeof e == "string")
    return xp(e), We(e.startsWith("0x") ? e.slice(2) : e);
  if (Vv.call(e) !== "Uint8Array") throw new TypeError("exact message must be hex or Uint8Array");
  const t = qv.call(e);
  vp(t);
  const n = new Uint8Array(t);
  return Wv.call(n, e), n;
}
const _r = typeof globalThis == "object" && "crypto" in globalThis ? globalThis.crypto : void 0;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function ra(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Ac(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function et(e, ...t) {
  if (!ra(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function kp(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash should be wrapped by utils.createHasher");
  Ac(e.outputLen), Ac(e.blockLen);
}
function us(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Gv(e, t) {
  et(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function jn(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Mr(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Tt(e, t) {
  return e << 32 - t | e >>> t;
}
function go(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const Ep = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", Zv = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function lr(e) {
  if (et(e), Ep)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += Zv[e[n]];
  return t;
}
const Gt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function wu(e) {
  if (e >= Gt._0 && e <= Gt._9)
    return e - Gt._0;
  if (e >= Gt.A && e <= Gt.F)
    return e - (Gt.A - 10);
  if (e >= Gt.a && e <= Gt.f)
    return e - (Gt.a - 10);
}
function Li(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (Ep)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = wu(e.charCodeAt(o)), a = wu(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function Ap(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function bl(e) {
  return typeof e == "string" && (e = Ap(e)), et(e), e;
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
let Sp = class {
};
function vl(e) {
  const t = (r) => e().update(bl(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
function _p(e = 32) {
  if (_r && typeof _r.getRandomValues == "function")
    return _r.getRandomValues(new Uint8Array(e));
  if (_r && typeof _r.randomBytes == "function")
    return Uint8Array.from(_r.randomBytes(e));
  throw new Error("crypto.getRandomValues must be defined");
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const xl = /* @__PURE__ */ BigInt(0), Sc = /* @__PURE__ */ BigInt(1);
function fs(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}"`;
    throw new Error(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function nr(e, t, n = "") {
  const r = ra(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function mo(e) {
  const t = e.toString(16);
  return t.length & 1 ? "0" + t : t;
}
function Ip(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  return e === "" ? xl : BigInt("0x" + e);
}
function ia(e) {
  return Ip(lr(e));
}
function Bp(e) {
  return et(e), Ip(lr(Uint8Array.from(e).reverse()));
}
function kl(e, t) {
  return Li(e.toString(16).padStart(t * 2, "0"));
}
function Tp(e, t) {
  return kl(e, t).reverse();
}
function Ye(e, t, n) {
  let r;
  if (typeof t == "string")
    try {
      r = Li(t);
    } catch (i) {
      throw new Error(e + " must be hex string or Uint8Array, cause: " + i);
    }
  else if (ra(t))
    r = Uint8Array.from(t);
  else
    throw new Error(e + " must be hex string or Uint8Array");
  return r.length, r;
}
const Ua = (e) => typeof e == "bigint" && xl <= e;
function Yv(e, t, n) {
  return Ua(e) && Ua(t) && Ua(n) && t <= e && e < n;
}
function Xv(e, t, n, r) {
  if (!Yv(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Pp(e) {
  let t;
  for (t = 0; e > xl; e >>= Sc, t += 1)
    ;
  return t;
}
const Ji = (e) => (Sc << BigInt(e)) - Sc;
function Jv(e, t, n) {
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
  }, l = (...h) => n(s, o, ...h), u = (h = r(0)) => {
    s = l(i(0), h), o = l(), h.length !== 0 && (s = l(i(1), h), o = l());
  }, d = () => {
    if (a++ >= 1e3)
      throw new Error("drbg: tried 1000 values");
    let h = 0;
    const p = [];
    for (; h < t; ) {
      o = l();
      const y = o.slice();
      p.push(y), h += o.length;
    }
    return lt(...p);
  };
  return (h, p) => {
    c(), u(h);
    let y;
    for (; !(y = p(d())); )
      u();
    return c(), y;
  };
}
function El(e, t, n = {}) {
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
const Xe = BigInt(0), Ge = BigInt(1), rr = /* @__PURE__ */ BigInt(2), $p = /* @__PURE__ */ BigInt(3), Op = /* @__PURE__ */ BigInt(4), Cp = /* @__PURE__ */ BigInt(5), Qv = /* @__PURE__ */ BigInt(7), Rp = /* @__PURE__ */ BigInt(8), e1 = /* @__PURE__ */ BigInt(9), Lp = /* @__PURE__ */ BigInt(16);
function ct(e, t) {
  const n = e % t;
  return n >= Xe ? n : t + n;
}
function ot(e, t, n) {
  let r = e;
  for (; t-- > Xe; )
    r *= r, r %= n;
  return r;
}
function gu(e, t) {
  if (e === Xe)
    throw new Error("invert: expected non-zero number");
  if (t <= Xe)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = ct(e, t), r = t, i = Xe, o = Ge;
  for (; n !== Xe; ) {
    const a = r / n, c = r % n, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== Ge)
    throw new Error("invert: does not exist");
  return ct(i, t);
}
function Al(e, t, n) {
  if (!e.eql(e.sqr(t), n))
    throw new Error("Cannot find square root");
}
function Hp(e, t) {
  const n = (e.ORDER + Ge) / Op, r = e.pow(t, n);
  return Al(e, r, t), r;
}
function t1(e, t) {
  const n = (e.ORDER - Cp) / Rp, r = e.mul(t, rr), i = e.pow(r, n), o = e.mul(t, i), s = e.mul(e.mul(o, rr), i), a = e.mul(o, e.sub(s, e.ONE));
  return Al(e, a, t), a;
}
function n1(e) {
  const t = Qi(e), n = Np(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + Qv) / Lp;
  return (a, c) => {
    let l = a.pow(c, s), u = a.mul(l, r);
    const d = a.mul(l, i), f = a.mul(l, o), h = a.eql(a.sqr(u), c), p = a.eql(a.sqr(d), c);
    l = a.cmov(l, u, h), u = a.cmov(f, d, p);
    const y = a.eql(a.sqr(u), c), w = a.cmov(l, u, y);
    return Al(a, w, c), w;
  };
}
function Np(e) {
  if (e < $p)
    throw new Error("sqrt is not defined for small field");
  let t = e - Ge, n = 0;
  for (; t % rr === Xe; )
    t /= rr, n++;
  let r = rr;
  const i = Qi(e);
  for (; mu(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return Hp;
  let o = i.pow(r, t);
  const s = (t + Ge) / rr;
  return function(c, l) {
    if (c.is0(l))
      return l;
    if (mu(c, l) !== 1)
      throw new Error("Cannot find square root");
    let u = n, d = c.mul(c.ONE, o), f = c.pow(l, t), h = c.pow(l, s);
    for (; !c.eql(f, c.ONE); ) {
      if (c.is0(f))
        return c.ZERO;
      let p = 1, y = c.sqr(f);
      for (; !c.eql(y, c.ONE); )
        if (p++, y = c.sqr(y), p === u)
          throw new Error("Cannot find square root");
      const w = Ge << BigInt(u - p - 1), m = c.pow(d, w);
      u = p, d = c.sqr(m), f = c.mul(f, d), h = c.mul(h, m);
    }
    return h;
  };
}
function r1(e) {
  return e % Op === $p ? Hp : e % Rp === Cp ? t1 : e % Lp === e1 ? n1(e) : Np(e);
}
const i1 = [
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
function o1(e) {
  const t = {
    ORDER: "bigint",
    MASK: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = i1.reduce((r, i) => (r[i] = "function", r), t);
  return El(e, n), e;
}
function s1(e, t, n) {
  if (n < Xe)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === Xe)
    return e.ONE;
  if (n === Ge)
    return t;
  let r = e.ONE, i = t;
  for (; n > Xe; )
    n & Ge && (r = e.mul(r, i)), i = e.sqr(i), n >>= Ge;
  return r;
}
function Up(e, t, n = !1) {
  const r = new Array(t.length).fill(n ? e.ZERO : void 0), i = t.reduce((s, a, c) => e.is0(a) ? s : (r[c] = s, e.mul(s, a)), e.ONE), o = e.inv(i);
  return t.reduceRight((s, a, c) => e.is0(a) ? s : (r[c] = e.mul(s, r[c]), e.mul(s, a)), o), r;
}
function mu(e, t) {
  const n = (e.ORDER - Ge) / rr, r = e.pow(t, n), i = e.eql(r, e.ONE), o = e.eql(r, e.ZERO), s = e.eql(r, e.neg(e.ONE));
  if (!i && !o && !s)
    throw new Error("invalid Legendre symbol result");
  return i ? 1 : o ? 0 : -1;
}
function Dp(e, t) {
  t !== void 0 && Ac(t);
  const n = t !== void 0 ? t : e.toString(2).length, r = Math.ceil(n / 8);
  return { nBitLength: n, nByteLength: r };
}
function Qi(e, t, n = !1, r = {}) {
  if (e <= Xe)
    throw new Error("invalid field: expected ORDER > 0, got " + e);
  let i, o, s = !1, a;
  if (typeof t == "object" && t != null) {
    if (r.sqrt || n)
      throw new Error("cannot specify opts in two arguments");
    const f = t;
    f.BITS && (i = f.BITS), f.sqrt && (o = f.sqrt), typeof f.isLE == "boolean" && (n = f.isLE), typeof f.modFromBytes == "boolean" && (s = f.modFromBytes), a = f.allowedLengths;
  } else
    typeof t == "number" && (i = t), r.sqrt && (o = r.sqrt);
  const { nBitLength: c, nByteLength: l } = Dp(e, i);
  if (l > 2048)
    throw new Error("invalid field: expected ORDER of <= 2048 bytes");
  let u;
  const d = Object.freeze({
    ORDER: e,
    isLE: n,
    BITS: c,
    BYTES: l,
    MASK: Ji(c),
    ZERO: Xe,
    ONE: Ge,
    allowedLengths: a,
    create: (f) => ct(f, e),
    isValid: (f) => {
      if (typeof f != "bigint")
        throw new Error("invalid field element: expected bigint, got " + typeof f);
      return Xe <= f && f < e;
    },
    is0: (f) => f === Xe,
    // is valid and invertible
    isValidNot0: (f) => !d.is0(f) && d.isValid(f),
    isOdd: (f) => (f & Ge) === Ge,
    neg: (f) => ct(-f, e),
    eql: (f, h) => f === h,
    sqr: (f) => ct(f * f, e),
    add: (f, h) => ct(f + h, e),
    sub: (f, h) => ct(f - h, e),
    mul: (f, h) => ct(f * h, e),
    pow: (f, h) => s1(d, f, h),
    div: (f, h) => ct(f * gu(h, e), e),
    // Same as above, but doesn't normalize
    sqrN: (f) => f * f,
    addN: (f, h) => f + h,
    subN: (f, h) => f - h,
    mulN: (f, h) => f * h,
    inv: (f) => gu(f, e),
    sqrt: o || ((f) => (u || (u = r1(e)), u(d, f))),
    toBytes: (f) => n ? Tp(f, l) : kl(f, l),
    fromBytes: (f, h = !0) => {
      if (a) {
        if (!a.includes(f.length) || f.length > l)
          throw new Error("Field.fromBytes: expected " + a + " bytes, got " + f.length);
        const y = new Uint8Array(l);
        y.set(f, n ? 0 : y.length - f.length), f = y;
      }
      if (f.length !== l)
        throw new Error("Field.fromBytes: expected " + l + " bytes, got " + f.length);
      let p = n ? Bp(f) : ia(f);
      if (s && (p = ct(p, e)), !h && !d.isValid(p))
        throw new Error("invalid field element: outside of range 0..ORDER");
      return p;
    },
    // TODO: we don't need it here, move out to separate fn
    invertBatch: (f) => Up(d, f),
    // We can't move this out because Fp6, Fp12 implement it
    // and it's unclear what to return in there.
    cmov: (f, h, p) => p ? h : f
  });
  return Object.freeze(d);
}
function zp(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  const t = e.toString(2).length;
  return Math.ceil(t / 8);
}
function Mp(e) {
  const t = zp(e);
  return t + Math.ceil(t / 2);
}
function a1(e, t, n = !1) {
  const r = e.length, i = zp(t), o = Mp(t);
  if (r < 16 || r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? Bp(e) : ia(e), a = ct(s, t - Ge) + Ge;
  return n ? Tp(a, i) : kl(a, i);
}
function c1(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function l1(e, t, n) {
  return e & t ^ ~e & n;
}
function d1(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Sl = class extends Sp {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Mr(this.buffer);
  }
  update(t) {
    us(this), t = bl(t), et(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Mr(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    us(this), Gv(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, jn(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    c1(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Mr(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const l = c / 4, u = this.get();
    if (l > u.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < l; d++)
      a.setUint32(4 * d, u[d], o);
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
const En = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Ne = /* @__PURE__ */ Uint32Array.from([
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
]), bo = /* @__PURE__ */ BigInt(2 ** 32 - 1), bu = /* @__PURE__ */ BigInt(32);
function u1(e, t = !1) {
  return t ? { h: Number(e & bo), l: Number(e >> bu & bo) } : { h: Number(e >> bu & bo) | 0, l: Number(e & bo) | 0 };
}
function f1(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = u1(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const vu = (e, t, n) => e >>> n, xu = (e, t, n) => e << 32 - n | t >>> n, Ir = (e, t, n) => e >>> n | t << 32 - n, Br = (e, t, n) => e << 32 - n | t >>> n, vo = (e, t, n) => e << 64 - n | t >>> n - 32, xo = (e, t, n) => e >>> n - 32 | t << 64 - n;
function Zt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const h1 = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), p1 = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, w1 = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), y1 = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, g1 = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), m1 = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, b1 = /* @__PURE__ */ Uint32Array.from([
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
]), An = /* @__PURE__ */ new Uint32Array(64);
let v1 = class extends Sl {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = En[0] | 0, this.B = En[1] | 0, this.C = En[2] | 0, this.D = En[3] | 0, this.E = En[4] | 0, this.F = En[5] | 0, this.G = En[6] | 0, this.H = En[7] | 0;
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
    for (let d = 0; d < 16; d++, n += 4)
      An[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = An[d - 15], h = An[d - 2], p = Tt(f, 7) ^ Tt(f, 18) ^ f >>> 3, y = Tt(h, 17) ^ Tt(h, 19) ^ h >>> 10;
      An[d] = y + An[d - 7] + p + An[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = Tt(a, 6) ^ Tt(a, 11) ^ Tt(a, 25), h = u + f + l1(a, c, l) + b1[d] + An[d] | 0, y = (Tt(r, 2) ^ Tt(r, 13) ^ Tt(r, 22)) + d1(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    jn(An);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), jn(this.buffer);
  }
};
const Kp = f1([
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
].map((e) => BigInt(e))), x1 = Kp[0], k1 = Kp[1], Sn = /* @__PURE__ */ new Uint32Array(80), _n = /* @__PURE__ */ new Uint32Array(80);
let E1 = class extends Sl {
  constructor(t = 64) {
    super(128, t, 16, !1), this.Ah = Ne[0] | 0, this.Al = Ne[1] | 0, this.Bh = Ne[2] | 0, this.Bl = Ne[3] | 0, this.Ch = Ne[4] | 0, this.Cl = Ne[5] | 0, this.Dh = Ne[6] | 0, this.Dl = Ne[7] | 0, this.Eh = Ne[8] | 0, this.El = Ne[9] | 0, this.Fh = Ne[10] | 0, this.Fl = Ne[11] | 0, this.Gh = Ne[12] | 0, this.Gl = Ne[13] | 0, this.Hh = Ne[14] | 0, this.Hl = Ne[15] | 0;
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: f, Gh: h, Gl: p, Hh: y, Hl: w } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, f, h, p, y, w];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, f, h, p, y, w) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = p | 0, this.Hh = y | 0, this.Hl = w | 0;
  }
  process(t, n) {
    for (let v = 0; v < 16; v++, n += 4)
      Sn[v] = t.getUint32(n), _n[v] = t.getUint32(n += 4);
    for (let v = 16; v < 80; v++) {
      const L = Sn[v - 15] | 0, T = _n[v - 15] | 0, C = Ir(L, T, 1) ^ Ir(L, T, 8) ^ vu(L, T, 7), H = Br(L, T, 1) ^ Br(L, T, 8) ^ xu(L, T, 7), O = Sn[v - 2] | 0, I = _n[v - 2] | 0, U = Ir(O, I, 19) ^ vo(O, I, 61) ^ vu(O, I, 6), z = Br(O, I, 19) ^ xo(O, I, 61) ^ xu(O, I, 6), E = w1(H, z, _n[v - 7], _n[v - 16]), k = y1(E, C, U, Sn[v - 7], Sn[v - 16]);
      Sn[v] = k | 0, _n[v] = E | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: f, Fh: h, Fl: p, Gh: y, Gl: w, Hh: m, Hl: g } = this;
    for (let v = 0; v < 80; v++) {
      const L = Ir(d, f, 14) ^ Ir(d, f, 18) ^ vo(d, f, 41), T = Br(d, f, 14) ^ Br(d, f, 18) ^ xo(d, f, 41), C = d & h ^ ~d & y, H = f & p ^ ~f & w, O = g1(g, T, H, k1[v], _n[v]), I = m1(O, m, L, C, x1[v], Sn[v]), U = O | 0, z = Ir(r, i, 28) ^ vo(r, i, 34) ^ vo(r, i, 39), E = Br(r, i, 28) ^ xo(r, i, 34) ^ xo(r, i, 39), k = r & o ^ r & a ^ o & a, x = i & s ^ i & c ^ s & c;
      m = y | 0, g = w | 0, y = h | 0, w = p | 0, h = d | 0, p = f | 0, { h: d, l: f } = Zt(l | 0, u | 0, I | 0, U | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const b = h1(U, E, x);
      r = p1(b, I, z, k), i = b | 0;
    }
    ({ h: r, l: i } = Zt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Zt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Zt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = Zt(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: f } = Zt(this.Eh | 0, this.El | 0, d | 0, f | 0), { h, l: p } = Zt(this.Fh | 0, this.Fl | 0, h | 0, p | 0), { h: y, l: w } = Zt(this.Gh | 0, this.Gl | 0, y | 0, w | 0), { h: m, l: g } = Zt(this.Hh | 0, this.Hl | 0, m | 0, g | 0), this.set(r, i, o, s, a, c, l, u, d, f, h, p, y, w, m, g);
  }
  roundClean() {
    jn(Sn, _n);
  }
  destroy() {
    jn(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
};
const _l = /* @__PURE__ */ vl(() => new v1()), ku = /* @__PURE__ */ vl(() => new E1());
let Fp = class extends Sp {
  constructor(t, n) {
    super(), this.finished = !1, this.destroyed = !1, kp(t);
    const r = bl(n);
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
    this.oHash.update(o), jn(o);
  }
  update(t) {
    return us(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    us(this), et(t, this.outputLen), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const hs = (e, t, n) => new Fp(e, t).update(n).digest();
hs.create = (e, t) => new Fp(e, t);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Zr = BigInt(0), ir = BigInt(1);
function ps(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function Da(e, t) {
  const n = Up(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function jp(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function za(e, t) {
  jp(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = Ji(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function Eu(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += ir);
  const l = t * r, u = l + Math.abs(a) - 1, d = a === 0, f = a < 0, h = t % 2 !== 0;
  return { nextN: c, offset: u, isZero: d, isNeg: f, isNegF: h, offsetF: l };
}
function A1(e, t) {
  if (!Array.isArray(e))
    throw new Error("array expected");
  e.forEach((n, r) => {
    if (!(n instanceof t))
      throw new Error("invalid point at index " + r);
  });
}
function S1(e, t) {
  if (!Array.isArray(e))
    throw new Error("array of scalars expected");
  e.forEach((n, r) => {
    if (!t.isValid(n))
      throw new Error("invalid scalar at index " + r);
  });
}
const Ma = /* @__PURE__ */ new WeakMap(), Vp = /* @__PURE__ */ new WeakMap();
function Ka(e) {
  return Vp.get(e) || 1;
}
function Au(e) {
  if (e !== Zr)
    throw new Error("invalid wNAF");
}
class _1 {
  // Parametrized with a given Point class (not individual point)
  constructor(t, n) {
    this.BASE = t.BASE, this.ZERO = t.ZERO, this.Fn = t.Fn, this.bits = n;
  }
  // non-const time multiplication ladder
  _unsafeLadder(t, n, r = this.ZERO) {
    let i = t;
    for (; n > Zr; )
      n & ir && (r = r.add(i)), i = i.double(), n >>= ir;
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
    const { windows: r, windowSize: i } = za(n, this.bits), o = [];
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
    const s = za(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: u, isNeg: d, isNegF: f, offsetF: h } = Eu(r, a, s);
      r = c, u ? o = o.add(ps(f, n[h])) : i = i.add(ps(d, n[l]));
    }
    return Au(r), { p: i, f: o };
  }
  /**
   * Implements ec unsafe (non const-time) multiplication using precomputed tables and w-ary non-adjacent form.
   * @param acc accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = za(t, this.bits);
    for (let s = 0; s < o.windows && r !== Zr; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: u } = Eu(r, s, o);
      if (r = a, !l) {
        const d = n[c];
        i = i.add(u ? d.negate() : d);
      }
    }
    return Au(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Ma.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Ma.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = Ka(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = Ka(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    jp(n, this.bits), Vp.set(t, n), Ma.delete(t);
  }
  hasCache(t) {
    return Ka(t) !== 1;
  }
}
function I1(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > Zr || r > Zr; )
    n & ir && (o = o.add(i)), r & ir && (s = s.add(i)), i = i.double(), n >>= ir, r >>= ir;
  return { p1: o, p2: s };
}
function B1(e, t, n, r) {
  A1(n, e), S1(r, t);
  const i = n.length, o = r.length;
  if (i !== o)
    throw new Error("arrays of points and scalars must have equal length");
  const s = e.ZERO, a = Pp(BigInt(i));
  let c = 1;
  a > 12 ? c = a - 3 : a > 4 ? c = a - 2 : a > 0 && (c = 2);
  const l = Ji(c), u = new Array(Number(l) + 1).fill(s), d = Math.floor((t.BITS - 1) / c) * c;
  let f = s;
  for (let h = d; h >= 0; h -= c) {
    u.fill(s);
    for (let y = 0; y < o; y++) {
      const w = r[y], m = Number(w >> BigInt(h) & l);
      u[m] = u[m].add(n[y]);
    }
    let p = s;
    for (let y = u.length - 1, w = s; y > 0; y--)
      w = w.add(u[y]), p = p.add(w);
    if (f = f.add(p), h !== 0)
      for (let y = 0; y < c; y++)
        f = f.double();
  }
  return f;
}
function Su(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return o1(t), t;
  } else
    return Qi(e, { isLE: n });
}
function T1(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > Zr))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = Su(t.p, n.Fp, r), o = Su(t.n, n.Fn, r), a = ["Gx", "Gy", "a", "b"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const _u = (e, t) => (e + (e >= 0 ? t : -t) / qp) / t;
function P1(e, t, n) {
  const [[r, i], [o, s]] = t, a = _u(s * e, n), c = _u(-i * e, n);
  let l = e - a * r - c * o, u = -a * i - c * s;
  const d = l < nn, f = u < nn;
  d && (l = -l), f && (u = -u);
  const h = Ji(Math.ceil(Pp(n) / 2)) + Kr;
  if (l < nn || l >= h || u < nn || u >= h)
    throw new Error("splitScalar (endomorphism): failed, k=" + e);
  return { k1neg: d, k1: l, k2neg: f, k2: u };
}
function _c(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function Fa(e, t) {
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return fs(n.lowS, "lowS"), fs(n.prehash, "prehash"), n.format !== void 0 && _c(n.format), n;
}
class $1 extends Error {
  constructor(t = "") {
    super(t);
  }
}
const en = {
  // asn.1 DER encoding utils
  Err: $1,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = en;
      if (e < 0 || e > 256)
        throw new n("tlv.encode: wrong tag");
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = mo(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? mo(i.length / 2 | 128) : "";
      return mo(e) + o + i + t;
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
        for (const u of l)
          s = s << 8 | u;
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
      let n = mo(e);
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
      return ia(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = en, i = Ye("signature", e), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: u } = r.decode(2, c);
    if (u.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = en, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
}, nn = BigInt(0), Kr = BigInt(1), qp = BigInt(2), ko = BigInt(3), O1 = BigInt(4);
function Hr(e, t) {
  const { BYTES: n } = e;
  let r;
  if (typeof t == "bigint")
    r = t;
  else {
    let i = Ye("private key", t);
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
function C1(e, t = {}) {
  const n = T1("weierstrass", e, t), { Fp: r, Fn: i } = n;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  El(t, {}, {
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
  const l = Gp(r, i);
  function u() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function d(E, k, x) {
    const { x: b, y: _ } = k.toAffine(), $ = r.toBytes(b);
    if (fs(x, "isCompressed"), x) {
      u();
      const A = !r.isOdd(_);
      return lt(Wp(A), $);
    } else
      return lt(Uint8Array.of(4), $, r.toBytes(_));
  }
  function f(E) {
    nr(E, void 0, "Point");
    const { publicKey: k, publicKeyUncompressed: x } = l, b = E.length, _ = E[0], $ = E.subarray(1);
    if (b === k && (_ === 2 || _ === 3)) {
      const A = r.fromBytes($);
      if (!r.isValid(A))
        throw new Error("bad point: is not on curve, wrong x");
      const B = y(A);
      let P;
      try {
        P = r.sqrt(B);
      } catch (K) {
        const D = K instanceof Error ? ": " + K.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + D);
      }
      u();
      const R = r.isOdd(P);
      return (_ & 1) === 1 !== R && (P = r.neg(P)), { x: A, y: P };
    } else if (b === x && _ === 4) {
      const A = r.BYTES, B = r.fromBytes($.subarray(0, A)), P = r.fromBytes($.subarray(A, A * 2));
      if (!w(B, P))
        throw new Error("bad point: is not on curve");
      return { x: B, y: P };
    } else
      throw new Error(`bad point: got length ${b}, expected compressed=${k} or uncompressed=${x}`);
  }
  const h = t.toBytes || d, p = t.fromBytes || f;
  function y(E) {
    const k = r.sqr(E), x = r.mul(k, E);
    return r.add(r.add(x, r.mul(E, o.a)), o.b);
  }
  function w(E, k) {
    const x = r.sqr(k), b = y(E);
    return r.eql(x, b);
  }
  if (!w(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const m = r.mul(r.pow(o.a, ko), O1), g = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(m, g)))
    throw new Error("bad curve params: a or b");
  function v(E, k, x = !1) {
    if (!r.isValid(k) || x && r.is0(k))
      throw new Error(`bad point coordinate ${E}`);
    return k;
  }
  function L(E) {
    if (!(E instanceof I))
      throw new Error("ProjectivePoint expected");
  }
  function T(E) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return P1(E, c.basises, i.ORDER);
  }
  const C = yu((E, k) => {
    const { X: x, Y: b, Z: _ } = E;
    if (r.eql(_, r.ONE))
      return { x, y: b };
    const $ = E.is0();
    k == null && (k = $ ? r.ONE : r.inv(_));
    const A = r.mul(x, k), B = r.mul(b, k), P = r.mul(_, k);
    if ($)
      return { x: r.ZERO, y: r.ZERO };
    if (!r.eql(P, r.ONE))
      throw new Error("invZ was invalid");
    return { x: A, y: B };
  }), H = yu((E) => {
    if (E.is0()) {
      if (t.allowInfinityPoint && !r.is0(E.Y))
        return;
      throw new Error("bad point: ZERO");
    }
    const { x: k, y: x } = E.toAffine();
    if (!r.isValid(k) || !r.isValid(x))
      throw new Error("bad point: x or y not field elements");
    if (!w(k, x))
      throw new Error("bad point: equation left != right");
    if (!E.isTorsionFree())
      throw new Error("bad point: not in prime-order subgroup");
    return !0;
  });
  function O(E, k, x, b, _) {
    return x = new I(r.mul(x.X, E), x.Y, x.Z), k = ps(b, k), x = ps(_, x), k.add(x);
  }
  class I {
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    constructor(k, x, b) {
      this.X = v("x", k), this.Y = v("y", x, !0), this.Z = v("z", b), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(k) {
      const { x, y: b } = k || {};
      if (!k || !r.isValid(x) || !r.isValid(b))
        throw new Error("invalid affine point");
      if (k instanceof I)
        throw new Error("projective point not allowed");
      return r.is0(x) && r.is0(b) ? I.ZERO : new I(x, b, r.ONE);
    }
    static fromBytes(k) {
      const x = I.fromAffine(p(nr(k, void 0, "point")));
      return x.assertValidity(), x;
    }
    static fromHex(k) {
      return I.fromBytes(Ye("pointHex", k));
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
    precompute(k = 8, x = !0) {
      return z.createCache(this, k), x || this.multiply(ko), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      H(this);
    }
    hasEvenY() {
      const { y: k } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(k);
    }
    /** Compare one point to another. */
    equals(k) {
      L(k);
      const { X: x, Y: b, Z: _ } = this, { X: $, Y: A, Z: B } = k, P = r.eql(r.mul(x, B), r.mul($, _)), R = r.eql(r.mul(b, B), r.mul(A, _));
      return P && R;
    }
    /** Flips point to one corresponding to (x, -y) in Affine coordinates. */
    negate() {
      return new I(this.X, r.neg(this.Y), this.Z);
    }
    // Renes-Costello-Batina exception-free doubling formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 3
    // Cost: 8M + 3S + 3*a + 2*b3 + 15add.
    double() {
      const { a: k, b: x } = o, b = r.mul(x, ko), { X: _, Y: $, Z: A } = this;
      let B = r.ZERO, P = r.ZERO, R = r.ZERO, N = r.mul(_, _), K = r.mul($, $), D = r.mul(A, A), M = r.mul(_, $);
      return M = r.add(M, M), R = r.mul(_, A), R = r.add(R, R), B = r.mul(k, R), P = r.mul(b, D), P = r.add(B, P), B = r.sub(K, P), P = r.add(K, P), P = r.mul(B, P), B = r.mul(M, B), R = r.mul(b, R), D = r.mul(k, D), M = r.sub(N, D), M = r.mul(k, M), M = r.add(M, R), R = r.add(N, N), N = r.add(R, N), N = r.add(N, D), N = r.mul(N, M), P = r.add(P, N), D = r.mul($, A), D = r.add(D, D), N = r.mul(D, M), B = r.sub(B, N), R = r.mul(D, K), R = r.add(R, R), R = r.add(R, R), new I(B, P, R);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(k) {
      L(k);
      const { X: x, Y: b, Z: _ } = this, { X: $, Y: A, Z: B } = k;
      let P = r.ZERO, R = r.ZERO, N = r.ZERO;
      const K = o.a, D = r.mul(o.b, ko);
      let M = r.mul(x, $), F = r.mul(b, A), q = r.mul(_, B), j = r.add(x, b), W = r.add($, A);
      j = r.mul(j, W), W = r.add(M, F), j = r.sub(j, W), W = r.add(x, _);
      let X = r.add($, B);
      return W = r.mul(W, X), X = r.add(M, q), W = r.sub(W, X), X = r.add(b, _), P = r.add(A, B), X = r.mul(X, P), P = r.add(F, q), X = r.sub(X, P), N = r.mul(K, W), P = r.mul(D, q), N = r.add(P, N), P = r.sub(F, N), N = r.add(F, N), R = r.mul(P, N), F = r.add(M, M), F = r.add(F, M), q = r.mul(K, q), W = r.mul(D, W), F = r.add(F, q), q = r.sub(M, q), q = r.mul(K, q), W = r.add(W, q), M = r.mul(F, W), R = r.add(R, M), M = r.mul(X, W), P = r.mul(j, P), P = r.sub(P, M), M = r.mul(j, F), N = r.mul(X, N), N = r.add(N, M), new I(P, R, N);
    }
    subtract(k) {
      return this.add(k.negate());
    }
    is0() {
      return this.equals(I.ZERO);
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
    multiply(k) {
      const { endo: x } = t;
      if (!i.isValidNot0(k))
        throw new Error("invalid scalar: out of range");
      let b, _;
      const $ = (A) => z.cached(this, A, (B) => Da(I, B));
      if (x) {
        const { k1neg: A, k1: B, k2neg: P, k2: R } = T(k), { p: N, f: K } = $(B), { p: D, f: M } = $(R);
        _ = K.add(M), b = O(x.beta, N, D, A, P);
      } else {
        const { p: A, f: B } = $(k);
        b = A, _ = B;
      }
      return Da(I, [b, _])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(k) {
      const { endo: x } = t, b = this;
      if (!i.isValid(k))
        throw new Error("invalid scalar: out of range");
      if (k === nn || b.is0())
        return I.ZERO;
      if (k === Kr)
        return b;
      if (z.hasCache(this))
        return this.multiply(k);
      if (x) {
        const { k1neg: _, k1: $, k2neg: A, k2: B } = T(k), { p1: P, p2: R } = I1(I, b, $, B);
        return O(x.beta, P, R, _, A);
      } else
        return z.unsafe(b, k);
    }
    multiplyAndAddUnsafe(k, x, b) {
      const _ = this.multiplyUnsafe(x).add(k.multiplyUnsafe(b));
      return _.is0() ? void 0 : _;
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * @param invertedZ Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(k) {
      return C(this, k);
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: k } = t;
      return s === Kr ? !0 : k ? k(I, this) : z.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: k } = t;
      return s === Kr ? this : k ? k(I, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return this.multiplyUnsafe(s).is0();
    }
    toBytes(k = !0) {
      return fs(k, "isCompressed"), this.assertValidity(), h(I, this, k);
    }
    toHex(k = !0) {
      return lr(this.toBytes(k));
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
    toRawBytes(k = !0) {
      return this.toBytes(k);
    }
    _setWindowSize(k) {
      this.precompute(k);
    }
    static normalizeZ(k) {
      return Da(I, k);
    }
    static msm(k, x) {
      return B1(I, i, k, x);
    }
    static fromPrivateKey(k) {
      return I.BASE.multiply(Hr(i, k));
    }
  }
  I.BASE = new I(o.Gx, o.Gy, r.ONE), I.ZERO = new I(r.ZERO, r.ONE, r.ZERO), I.Fp = r, I.Fn = i;
  const U = i.BITS, z = new _1(I, t.endo ? Math.ceil(U / 2) : U);
  return I.BASE.precompute(8), I;
}
function Wp(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function Gp(e, t) {
  return {
    secretKey: t.BYTES,
    publicKey: 1 + e.BYTES,
    publicKeyUncompressed: 1 + 2 * e.BYTES,
    publicKeyHasPrefix: !0,
    signature: 2 * t.BYTES
  };
}
function R1(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes || _p, i = Object.assign(Gp(e.Fp, n), { seed: Mp(n.ORDER) });
  function o(h) {
    try {
      return !!Hr(n, h);
    } catch {
      return !1;
    }
  }
  function s(h, p) {
    const { publicKey: y, publicKeyUncompressed: w } = i;
    try {
      const m = h.length;
      return p === !0 && m !== y || p === !1 && m !== w ? !1 : !!e.fromBytes(h);
    } catch {
      return !1;
    }
  }
  function a(h = r(i.seed)) {
    return a1(nr(h, i.seed, "seed"), n.ORDER);
  }
  function c(h, p = !0) {
    return e.BASE.multiply(Hr(n, h)).toBytes(p);
  }
  function l(h) {
    const p = a(h);
    return { secretKey: p, publicKey: c(p) };
  }
  function u(h) {
    if (typeof h == "bigint")
      return !1;
    if (h instanceof e)
      return !0;
    const { secretKey: p, publicKey: y, publicKeyUncompressed: w } = i;
    if (n.allowedLengths || p === y)
      return;
    const m = Ye("key", h).length;
    return m === y || m === w;
  }
  function d(h, p, y = !0) {
    if (u(h) === !0)
      throw new Error("first arg must be private key");
    if (u(p) === !1)
      throw new Error("second arg must be public key");
    const w = Hr(n, h);
    return e.fromHex(p).multiply(w).toBytes(y);
  }
  return Object.freeze({ getPublicKey: c, getSharedSecret: d, keygen: l, Point: e, utils: {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a,
    // TODO: remove
    isValidPrivateKey: o,
    randomPrivateKey: a,
    normPrivateKeyToScalar: (h) => Hr(n, h),
    precompute(h = 8, p = e.BASE) {
      return p.precompute(h, !1);
    }
  }, lengths: i });
}
function L1(e, t, n = {}) {
  kp(t), El(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  });
  const r = n.randomBytes || _p, i = n.hmac || ((x, ...b) => hs(t, x, lt(...b))), { Fp: o, Fn: s } = e, { ORDER: a, BITS: c } = s, { keygen: l, getPublicKey: u, getSharedSecret: d, utils: f, lengths: h } = R1(e, n), p = {
    prehash: !1,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !1,
    format: void 0,
    //'compact' as ECDSASigFormat,
    extraEntropy: !1
  }, y = "compact";
  function w(x) {
    const b = a >> Kr;
    return x > b;
  }
  function m(x, b) {
    if (!s.isValidNot0(b))
      throw new Error(`invalid signature ${x}: out of range 1..Point.Fn.ORDER`);
    return b;
  }
  function g(x, b) {
    _c(b);
    const _ = h.signature, $ = b === "compact" ? _ : b === "recovered" ? _ + 1 : void 0;
    return nr(x, $, `${b} signature`);
  }
  class v {
    constructor(b, _, $) {
      this.r = m("r", b), this.s = m("s", _), $ != null && (this.recovery = $), Object.freeze(this);
    }
    static fromBytes(b, _ = y) {
      g(b, _);
      let $;
      if (_ === "der") {
        const { r: R, s: N } = en.toSig(nr(b));
        return new v(R, N);
      }
      _ === "recovered" && ($ = b[0], _ = "compact", b = b.subarray(1));
      const A = s.BYTES, B = b.subarray(0, A), P = b.subarray(A, A * 2);
      return new v(s.fromBytes(B), s.fromBytes(P), $);
    }
    static fromHex(b, _) {
      return this.fromBytes(Li(b), _);
    }
    addRecoveryBit(b) {
      return new v(this.r, this.s, b);
    }
    recoverPublicKey(b) {
      const _ = o.ORDER, { r: $, s: A, recovery: B } = this;
      if (B == null || ![0, 1, 2, 3].includes(B))
        throw new Error("recovery id invalid");
      if (a * qp < _ && B > 1)
        throw new Error("recovery id is ambiguous for h>1 curve");
      const R = B === 2 || B === 3 ? $ + a : $;
      if (!o.isValid(R))
        throw new Error("recovery id 2 or 3 invalid");
      const N = o.toBytes(R), K = e.fromBytes(lt(Wp((B & 1) === 0), N)), D = s.inv(R), M = T(Ye("msgHash", b)), F = s.create(-M * D), q = s.create(A * D), j = e.BASE.multiplyUnsafe(F).add(K.multiplyUnsafe(q));
      if (j.is0())
        throw new Error("point at infinify");
      return j.assertValidity(), j;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return w(this.s);
    }
    toBytes(b = y) {
      if (_c(b), b === "der")
        return Li(en.hexFromSig(this));
      const _ = s.toBytes(this.r), $ = s.toBytes(this.s);
      if (b === "recovered") {
        if (this.recovery == null)
          throw new Error("recovery bit must be present");
        return lt(Uint8Array.of(this.recovery), _, $);
      }
      return lt(_, $);
    }
    toHex(b) {
      return lr(this.toBytes(b));
    }
    // TODO: remove
    assertValidity() {
    }
    static fromCompact(b) {
      return v.fromBytes(Ye("sig", b), "compact");
    }
    static fromDER(b) {
      return v.fromBytes(Ye("sig", b), "der");
    }
    normalizeS() {
      return this.hasHighS() ? new v(this.r, s.neg(this.s), this.recovery) : this;
    }
    toDERRawBytes() {
      return this.toBytes("der");
    }
    toDERHex() {
      return lr(this.toBytes("der"));
    }
    toCompactRawBytes() {
      return this.toBytes("compact");
    }
    toCompactHex() {
      return lr(this.toBytes("compact"));
    }
  }
  const L = n.bits2int || function(b) {
    if (b.length > 8192)
      throw new Error("input is too large");
    const _ = ia(b), $ = b.length * 8 - c;
    return $ > 0 ? _ >> BigInt($) : _;
  }, T = n.bits2int_modN || function(b) {
    return s.create(L(b));
  }, C = Ji(c);
  function H(x) {
    return Xv("num < 2^" + c, x, nn, C), s.toBytes(x);
  }
  function O(x, b) {
    return nr(x, void 0, "message"), b ? nr(t(x), void 0, "prehashed message") : x;
  }
  function I(x, b, _) {
    if (["recovered", "canonical"].some((F) => F in _))
      throw new Error("sign() legacy options not supported");
    const { lowS: $, prehash: A, extraEntropy: B } = Fa(_, p);
    x = O(x, A);
    const P = T(x), R = Hr(s, b), N = [H(R), H(P)];
    if (B != null && B !== !1) {
      const F = B === !0 ? r(h.secretKey) : B;
      N.push(Ye("extraEntropy", F));
    }
    const K = lt(...N), D = P;
    function M(F) {
      const q = L(F);
      if (!s.isValidNot0(q))
        return;
      const j = s.inv(q), W = e.BASE.multiply(q).toAffine(), X = s.create(W.x);
      if (X === nn)
        return;
      const we = s.create(j * s.create(D + X * R));
      if (we === nn)
        return;
      let se = (W.x === X ? 0 : 2) | Number(W.y & Kr), xe = we;
      return $ && w(we) && (xe = s.neg(we), se ^= 1), new v(X, xe, se);
    }
    return { seed: K, k2sig: M };
  }
  function U(x, b, _ = {}) {
    x = Ye("message", x);
    const { seed: $, k2sig: A } = I(x, b, _);
    return Jv(t.outputLen, s.BYTES, i)($, A);
  }
  function z(x) {
    let b;
    const _ = typeof x == "string" || ra(x), $ = !_ && x !== null && typeof x == "object" && typeof x.r == "bigint" && typeof x.s == "bigint";
    if (!_ && !$)
      throw new Error("invalid signature, expected Uint8Array, hex string or Signature instance");
    if ($)
      b = new v(x.r, x.s);
    else if (_) {
      try {
        b = v.fromBytes(Ye("sig", x), "der");
      } catch (A) {
        if (!(A instanceof en.Err))
          throw A;
      }
      if (!b)
        try {
          b = v.fromBytes(Ye("sig", x), "compact");
        } catch {
          return !1;
        }
    }
    return b || !1;
  }
  function E(x, b, _, $ = {}) {
    const { lowS: A, prehash: B, format: P } = Fa($, p);
    if (_ = Ye("publicKey", _), b = O(Ye("message", b), B), "strict" in $)
      throw new Error("options.strict was renamed to lowS");
    const R = P === void 0 ? z(x) : v.fromBytes(Ye("sig", x), P);
    if (R === !1)
      return !1;
    try {
      const N = e.fromBytes(_);
      if (A && R.hasHighS())
        return !1;
      const { r: K, s: D } = R, M = T(b), F = s.inv(D), q = s.create(M * F), j = s.create(K * F), W = e.BASE.multiplyUnsafe(q).add(N.multiplyUnsafe(j));
      return W.is0() ? !1 : s.create(W.x) === K;
    } catch {
      return !1;
    }
  }
  function k(x, b, _ = {}) {
    const { prehash: $ } = Fa(_, p);
    return b = O(b, $), v.fromBytes(x, "recovered").recoverPublicKey(b).toBytes();
  }
  return Object.freeze({
    keygen: l,
    getPublicKey: u,
    getSharedSecret: d,
    utils: f,
    lengths: h,
    Point: e,
    sign: U,
    verify: E,
    recoverPublicKey: k,
    Signature: v,
    hash: t
  });
}
function H1(e) {
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
function N1(e) {
  const { CURVE: t, curveOpts: n } = H1(e), r = {
    hmac: e.hmac,
    randomBytes: e.randomBytes,
    lowS: e.lowS,
    bits2int: e.bits2int,
    bits2int_modN: e.bits2int_modN
  };
  return { CURVE: t, curveOpts: n, hash: e.hash, ecdsaOpts: r };
}
function U1(e, t) {
  const n = t.Point;
  return Object.assign({}, t, {
    ProjectivePoint: n,
    CURVE: Object.assign({}, e, Dp(n.Fn.ORDER, n.Fn.BITS))
  });
}
function D1(e) {
  const { CURVE: t, curveOpts: n, hash: r, ecdsaOpts: i } = N1(e), o = C1(t, n), s = L1(o, r, i);
  return U1(e, s);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function z1(e, t) {
  const n = (r) => D1({ ...e, hash: r });
  return { ...n(t), create: n };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Il = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, M1 = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, Iu = /* @__PURE__ */ BigInt(2);
function K1(e) {
  const t = Il.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, u = l * l * e % t, d = ot(u, n, t) * u % t, f = ot(d, n, t) * u % t, h = ot(f, Iu, t) * l % t, p = ot(h, i, t) * h % t, y = ot(p, o, t) * p % t, w = ot(y, a, t) * y % t, m = ot(w, c, t) * w % t, g = ot(m, a, t) * y % t, v = ot(g, n, t) * u % t, L = ot(v, s, t) * p % t, T = ot(L, r, t) * l % t, C = ot(T, Iu, t);
  if (!Ic.eql(Ic.sqr(C), e))
    throw new Error("Cannot find square root");
  return C;
}
const Ic = Qi(Il.p, { sqrt: K1 }), Jt = z1({ ...Il, Fp: Ic, lowS: !0, endo: M1 }, _l), F1 = /* @__PURE__ */ Uint8Array.from([
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
]), Zp = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), j1 = Zp.map((e) => (9 * e + 5) % 16), Yp = /* @__PURE__ */ (() => {
  const n = [[Zp], [j1]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => F1[o]));
  return n;
})(), Xp = Yp[0], Jp = Yp[1], Qp = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), V1 = /* @__PURE__ */ Xp.map((e, t) => e.map((n) => Qp[t][n])), q1 = /* @__PURE__ */ Jp.map((e, t) => e.map((n) => Qp[t][n])), W1 = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), G1 = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function Bu(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const Eo = /* @__PURE__ */ new Uint32Array(16);
class Z1 extends Sl {
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
      Eo[h] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, u = l, d = this.h4 | 0, f = d;
    for (let h = 0; h < 5; h++) {
      const p = 4 - h, y = W1[h], w = G1[h], m = Xp[h], g = Jp[h], v = V1[h], L = q1[h];
      for (let T = 0; T < 16; T++) {
        const C = go(r + Bu(h, o, a, l) + Eo[m[T]] + y, v[T]) + d | 0;
        r = d, d = l, l = go(a, 10) | 0, a = o, o = C;
      }
      for (let T = 0; T < 16; T++) {
        const C = go(i + Bu(p, s, c, u) + Eo[g[T]] + w, L[T]) + f | 0;
        i = f, f = u, u = go(c, 10) | 0, c = s, s = C;
      }
    }
    this.set(this.h1 + a + u | 0, this.h2 + l + f | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    jn(Eo);
  }
  destroy() {
    this.destroyed = !0, jn(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const Y1 = /* @__PURE__ */ vl(() => new Z1());
/*! scure-base - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Hi(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function ew(e, t) {
  return Array.isArray(t) ? t.length === 0 ? !0 : e ? t.every((n) => typeof n == "string") : t.every((n) => Number.isSafeInteger(n)) : !1;
}
function tw(e) {
  if (typeof e != "function")
    throw new Error("function expected");
  return !0;
}
function Yr(e, t) {
  if (typeof t != "string")
    throw new Error(`${e}: string expected`);
  return !0;
}
function oi(e) {
  if (!Number.isSafeInteger(e))
    throw new Error(`invalid integer: ${e}`);
}
function ws(e) {
  if (!Array.isArray(e))
    throw new Error("array expected");
}
function ys(e, t) {
  if (!ew(!0, t))
    throw new Error(`${e}: array of strings expected`);
}
function Bl(e, t) {
  if (!ew(!1, t))
    throw new Error(`${e}: array of numbers expected`);
}
// @__NO_SIDE_EFFECTS__
function oa(...e) {
  const t = (o) => o, n = (o, s) => (a) => o(s(a)), r = e.map((o) => o.encode).reduceRight(n, t), i = e.map((o) => o.decode).reduce(n, t);
  return { encode: r, decode: i };
}
// @__NO_SIDE_EFFECTS__
function Tl(e) {
  const t = typeof e == "string" ? e.split("") : e, n = t.length;
  ys("alphabet", t);
  const r = new Map(t.map((i, o) => [i, o]));
  return {
    encode: (i) => (ws(i), i.map((o) => {
      if (!Number.isSafeInteger(o) || o < 0 || o >= n)
        throw new Error(`alphabet.encode: digit index outside alphabet "${o}". Allowed: ${e}`);
      return t[o];
    })),
    decode: (i) => (ws(i), i.map((o) => {
      Yr("alphabet.decode", o);
      const s = r.get(o);
      if (s === void 0)
        throw new Error(`Unknown letter: "${o}". Allowed: ${e}`);
      return s;
    }))
  };
}
// @__NO_SIDE_EFFECTS__
function Pl(e = "") {
  return Yr("join", e), {
    encode: (t) => (ys("join.decode", t), t.join(e)),
    decode: (t) => (Yr("join.decode", t), t.split(e))
  };
}
// @__NO_SIDE_EFFECTS__
function X1(e, t = "=") {
  return oi(e), Yr("padding", t), {
    encode(n) {
      for (ys("padding.encode", n); n.length * e % 8; )
        n.push(t);
      return n;
    },
    decode(n) {
      ys("padding.decode", n);
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
function Bc(e, t, n) {
  if (t < 2)
    throw new Error(`convertRadix: invalid from=${t}, base cannot be less than 2`);
  if (n < 2)
    throw new Error(`convertRadix: invalid to=${n}, base cannot be less than 2`);
  if (ws(e), !e.length)
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
      const u = o[l], d = t * a, f = d + u;
      if (!Number.isSafeInteger(f) || d / t !== a || f - u !== d)
        throw new Error("convertRadix: carry overflow");
      const h = f / n;
      a = f % n;
      const p = Math.floor(h);
      if (o[l] = p, !Number.isSafeInteger(p) || p * n + a !== f)
        throw new Error("convertRadix: carry overflow");
      if (c)
        p ? c = !1 : r = l;
      else continue;
    }
    if (i.push(a), c)
      break;
  }
  for (let a = 0; a < e.length - 1 && e[a] === 0; a++)
    i.push(0);
  return i.reverse();
}
const nw = (e, t) => t === 0 ? e : nw(t, e % t), gs = /* @__NO_SIDE_EFFECTS__ */ (e, t) => e + (t - nw(e, t)), Go = /* @__PURE__ */ (() => {
  let e = [];
  for (let t = 0; t < 40; t++)
    e.push(2 ** t);
  return e;
})();
function ms(e, t, n, r) {
  if (ws(e), t <= 0 || t > 32)
    throw new Error(`convertRadix2: wrong from=${t}`);
  if (n <= 0 || n > 32)
    throw new Error(`convertRadix2: wrong to=${n}`);
  if (/* @__PURE__ */ gs(t, n) > 32)
    throw new Error(`convertRadix2: carry overflow from=${t} to=${n} carryBits=${/* @__PURE__ */ gs(t, n)}`);
  let i = 0, o = 0;
  const s = Go[t], a = Go[n] - 1, c = [];
  for (const l of e) {
    if (oi(l), l >= s)
      throw new Error(`convertRadix2: invalid data word=${l} from=${t}`);
    if (i = i << t | l, o + t > 32)
      throw new Error(`convertRadix2: carry overflow pos=${o} from=${t}`);
    for (o += t; o >= n; o -= n)
      c.push((i >> o - n & a) >>> 0);
    const u = Go[o];
    if (u === void 0)
      throw new Error("invalid carry");
    i &= u - 1;
  }
  if (i = i << n - o & a, !r && o >= t)
    throw new Error("Excess padding");
  if (!r && i > 0)
    throw new Error(`Non-zero padding: ${i}`);
  return r && o > 0 && c.push(i >>> 0), c;
}
// @__NO_SIDE_EFFECTS__
function rw(e) {
  oi(e);
  const t = 2 ** 8;
  return {
    encode: (n) => {
      if (!Hi(n))
        throw new Error("radix.encode input should be Uint8Array");
      return Bc(Array.from(n), t, e);
    },
    decode: (n) => (Bl("radix.decode", n), Uint8Array.from(Bc(n, e, t)))
  };
}
// @__NO_SIDE_EFFECTS__
function iw(e, t = !1) {
  if (oi(e), e <= 0 || e > 32)
    throw new Error("radix2: bits should be in (0..32]");
  if (/* @__PURE__ */ gs(8, e) > 32 || /* @__PURE__ */ gs(e, 8) > 32)
    throw new Error("radix2: carry overflow");
  return {
    encode: (n) => {
      if (!Hi(n))
        throw new Error("radix2.encode input should be Uint8Array");
      return ms(Array.from(n), 8, e, !t);
    },
    decode: (n) => (Bl("radix2.decode", n), Uint8Array.from(ms(n, e, 8, t)))
  };
}
function Tu(e) {
  return tw(e), function(...t) {
    try {
      return e.apply(null, t);
    } catch {
    }
  };
}
function ow(e, t) {
  return oi(e), tw(t), {
    encode(n) {
      if (!Hi(n))
        throw new Error("checksum.encode: input should be Uint8Array");
      const r = t(n).slice(0, e), i = new Uint8Array(n.length + e);
      return i.set(n), i.set(r, n.length), i;
    },
    decode(n) {
      if (!Hi(n))
        throw new Error("checksum.decode: input should be Uint8Array");
      const r = n.slice(0, -e), i = n.slice(-e), o = t(r).slice(0, e);
      for (let s = 0; s < e; s++)
        if (o[s] !== i[s])
          throw new Error("Invalid checksum");
      return r;
    }
  };
}
const Ao = {
  alphabet: Tl,
  chain: oa,
  checksum: ow,
  convertRadix: Bc,
  convertRadix2: ms,
  radix: rw,
  radix2: iw,
  join: Pl,
  padding: X1
}, J1 = /* @__NO_SIDE_EFFECTS__ */ (e) => /* @__PURE__ */ oa(/* @__PURE__ */ rw(58), /* @__PURE__ */ Tl(e), /* @__PURE__ */ Pl("")), sw = /* @__PURE__ */ J1("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"), aw = (e) => /* @__PURE__ */ oa(ow(4, (t) => e(e(t))), sw), Q1 = aw, Tc = /* @__PURE__ */ oa(/* @__PURE__ */ Tl("qpzry9x8gf2tvdw0s3jn54khce6mua7l"), /* @__PURE__ */ Pl("")), Pu = [996825010, 642813549, 513874426, 1027748829, 705979059];
function wi(e) {
  const t = e >> 25;
  let n = (e & 33554431) << 5;
  for (let r = 0; r < Pu.length; r++)
    (t >> r & 1) === 1 && (n ^= Pu[r]);
  return n;
}
function $u(e, t, n = 1) {
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
  return i ^= n, Tc.encode(ms([i % Go[30]], 30, 5, !1));
}
// @__NO_SIDE_EFFECTS__
function cw(e) {
  const t = e === "bech32" ? 1 : 734539939, n = /* @__PURE__ */ iw(5), r = n.decode, i = n.encode, o = Tu(r);
  function s(d, f, h = 90) {
    Yr("bech32.encode prefix", d), Hi(f) && (f = Array.from(f)), Bl("bech32.encode", f);
    const p = d.length;
    if (p === 0)
      throw new TypeError(`Invalid prefix length ${p}`);
    const y = p + 7 + f.length;
    if (h !== !1 && y > h)
      throw new TypeError(`Length ${y} exceeds limit ${h}`);
    const w = d.toLowerCase(), m = $u(w, f, t);
    return `${w}1${Tc.encode(f)}${m}`;
  }
  function a(d, f = 90) {
    Yr("bech32.decode input", d);
    const h = d.length;
    if (h < 8 || f !== !1 && h > f)
      throw new TypeError(`invalid string length: ${h} (${d}). Expected (8..${f})`);
    const p = d.toLowerCase();
    if (d !== p && d !== d.toUpperCase())
      throw new Error("String must be lowercase or uppercase");
    const y = p.lastIndexOf("1");
    if (y === 0 || y === -1)
      throw new Error('Letter "1" must be present between prefix and data only');
    const w = p.slice(0, y), m = p.slice(y + 1);
    if (m.length < 6)
      throw new Error("Data must be at least 6 characters long");
    const g = Tc.decode(m).slice(0, -6), v = $u(w, g, t);
    if (!m.endsWith(v))
      throw new Error(`Invalid checksum in ${d}: expected "${v}"`);
    return { prefix: w, words: g };
  }
  const c = Tu(a);
  function l(d) {
    const { prefix: f, words: h } = a(d, !1);
    return { prefix: f, words: h, bytes: r(h) };
  }
  function u(d, f) {
    return s(d, i(f));
  }
  return {
    encode: s,
    decode: a,
    encodeFromBytes: u,
    decodeToBytes: l,
    decodeUnsafe: c,
    fromWords: r,
    fromWordsUnsafe: o,
    toWords: i
  };
}
const Ou = /* @__PURE__ */ cw("bech32"), Cu = /* @__PURE__ */ cw("bech32m");
/*! scure-bip32 - MIT License (c) 2022 Patricio Palladino, Paul Miller (paulmillr.com) */
const So = Jt.ProjectivePoint, ja = aw(_l);
function Ru(e) {
  et(e);
  const t = e.length === 0 ? "0" : lr(e);
  return BigInt("0x" + t);
}
function ex(e) {
  if (typeof e != "bigint")
    throw new Error("bigint expected");
  return Li(e.toString(16).padStart(64, "0"));
}
const tx = Ap("Bitcoin seed"), Va = { private: 76066276, public: 76067358 }, qa = 2147483648, nx = (e) => Y1(_l(e)), rx = (e) => Mr(e).getUint32(0, !1), _o = (e) => {
  if (!Number.isSafeInteger(e) || e < 0 || e > 2 ** 32 - 1)
    throw new Error("invalid number, should be from 0 to 2**32-1, got " + e);
  const t = new Uint8Array(4);
  return Mr(t).setUint32(0, e, !1), t;
};
class Ln {
  get fingerprint() {
    if (!this.pubHash)
      throw new Error("No publicKey set!");
    return rx(this.pubHash);
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
    return ja.encode(this.serialize(this.versions.private, lt(new Uint8Array([0]), t)));
  }
  get publicExtendedKey() {
    if (!this.pubKey)
      throw new Error("No public key");
    return ja.encode(this.serialize(this.versions.public, this.pubKey));
  }
  static fromMasterSeed(t, n = Va) {
    if (et(t), 8 * t.length < 128 || 8 * t.length > 512)
      throw new Error("HDKey: seed length must be between 128 and 512 bits; 256 bits is advised, got " + t.length);
    const r = hs(ku, tx, t);
    return new Ln({
      versions: n,
      chainCode: r.slice(32),
      privateKey: r.slice(0, 32)
    });
  }
  static fromExtendedKey(t, n = Va) {
    const r = ja.decode(t), i = Mr(r), o = i.getUint32(0, !1), s = {
      versions: n,
      depth: r[4],
      parentFingerprint: i.getUint32(5, !1),
      index: i.getUint32(9, !1),
      chainCode: r.slice(13, 45)
    }, a = r.slice(45), c = a[0] === 0;
    if (o !== n[c ? "private" : "public"])
      throw new Error("Version mismatch");
    return c ? new Ln({ ...s, privateKey: a.slice(1) }) : new Ln({ ...s, publicKey: a });
  }
  static fromJSON(t) {
    return Ln.fromExtendedKey(t.xpriv);
  }
  constructor(t) {
    if (this.depth = 0, this.index = 0, this.chainCode = null, this.parentFingerprint = 0, !t || typeof t != "object")
      throw new Error("HDKey.constructor must not be called directly");
    if (this.versions = t.versions || Va, this.depth = t.depth || 0, this.chainCode = t.chainCode || null, this.index = t.index || 0, this.parentFingerprint = t.parentFingerprint || 0, !this.depth && (this.parentFingerprint || this.index))
      throw new Error("HDKey: zero depth with non-zero index/parent fingerprint");
    if (t.publicKey && t.privateKey)
      throw new Error("HDKey: publicKey and privateKey at same time.");
    if (t.privateKey) {
      if (!Jt.utils.isValidPrivateKey(t.privateKey))
        throw new Error("Invalid private key");
      this.privKey = typeof t.privateKey == "bigint" ? t.privateKey : Ru(t.privateKey), this.privKeyBytes = ex(this.privKey), this.pubKey = Jt.getPublicKey(t.privateKey, !0);
    } else if (t.publicKey)
      this.pubKey = So.fromHex(t.publicKey).toRawBytes(!0);
    else
      throw new Error("HDKey: no public or private key provided");
    this.pubHash = nx(this.pubKey);
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
      if (!Number.isSafeInteger(a) || a >= qa)
        throw new Error("Invalid index");
      o[2] === "'" && (a += qa), r = r.deriveChild(a);
    }
    return r;
  }
  deriveChild(t) {
    if (!this.pubKey || !this.chainCode)
      throw new Error("No publicKey or chainCode set");
    let n = _o(t);
    if (t >= qa) {
      const a = this.privateKey;
      if (!a)
        throw new Error("Could not derive hardened child key");
      n = lt(new Uint8Array([0]), a, n);
    } else
      n = lt(this.pubKey, n);
    const r = hs(ku, this.chainCode, n), i = Ru(r.slice(0, 32)), o = r.slice(32);
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
      return new Ln(s);
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
    return et(n, 33), lt(_o(t), new Uint8Array([this.depth]), _o(this.parentFingerprint), _o(this.index), this.chainCode, n);
  }
}
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function ix(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Oi(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function si(e, ...t) {
  if (!ix(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function lw(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash should be wrapped by utils.createHasher");
  Oi(e.outputLen), Oi(e.blockLen);
}
function bs(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function ox(e, t) {
  si(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function fr(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Zo(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Pt(e, t) {
  return e << 32 - t | e >>> t;
}
function dw(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function $l(e) {
  return typeof e == "string" && (e = dw(e)), si(e), e;
}
function Lu(e) {
  return typeof e == "string" && (e = dw(e)), si(e), e;
}
function sx(e, t) {
  if (t !== void 0 && {}.toString.call(t) !== "[object Object]")
    throw new Error("options should be object or undefined");
  return Object.assign(e, t);
}
let uw = class {
};
function fw(e) {
  const t = (r) => e().update($l(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
class hw extends uw {
  constructor(t, n) {
    super(), this.finished = !1, this.destroyed = !1, lw(t);
    const r = $l(n);
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
    this.oHash.update(o), fr(o);
  }
  update(t) {
    return bs(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    bs(this), si(t, this.outputLen), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const pw = (e, t, n) => new hw(e, t).update(n).digest();
pw.create = (e, t) => new hw(e, t);
function ax(e, t, n, r) {
  lw(e);
  const i = sx({ dkLen: 32, asyncTick: 10 }, r), { c: o, dkLen: s, asyncTick: a } = i;
  if (Oi(o), Oi(s), Oi(a), o < 1)
    throw new Error("iterations (c) should be >= 1");
  const c = Lu(t), l = Lu(n), u = new Uint8Array(s), d = pw.create(e, c), f = d._cloneInto().update(l);
  return { c: o, dkLen: s, asyncTick: a, DK: u, PRF: d, PRFSalt: f };
}
function cx(e, t, n, r, i) {
  return e.destroy(), t.destroy(), r && r.destroy(), fr(i), n;
}
function lx(e, t, n, r) {
  const { c: i, dkLen: o, DK: s, PRF: a, PRFSalt: c } = ax(e, t, n, r);
  let l;
  const u = new Uint8Array(4), d = Zo(u), f = new Uint8Array(a.outputLen);
  for (let h = 1, p = 0; p < o; h++, p += a.outputLen) {
    const y = s.subarray(p, p + a.outputLen);
    d.setInt32(0, h, !1), (l = c._cloneInto(l)).update(u).digestInto(f), y.set(f.subarray(0, y.length));
    for (let w = 1; w < i; w++) {
      a._cloneInto(l).update(f).digestInto(f);
      for (let m = 0; m < y.length; m++)
        y[m] ^= f[m];
    }
  }
  return cx(a, c, s, l, f);
}
function dx(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function ux(e, t, n) {
  return e & t ^ ~e & n;
}
function fx(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let ww = class extends uw {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Zo(this.buffer);
  }
  update(t) {
    bs(this), t = $l(t), si(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Zo(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    bs(this), ox(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, fr(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    dx(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Zo(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const l = c / 4, u = this.get();
    if (l > u.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < l; d++)
      a.setUint32(4 * d, u[d], o);
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
const In = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Ue = /* @__PURE__ */ Uint32Array.from([
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
]), Io = /* @__PURE__ */ BigInt(2 ** 32 - 1), Hu = /* @__PURE__ */ BigInt(32);
function hx(e, t = !1) {
  return t ? { h: Number(e & Io), l: Number(e >> Hu & Io) } : { h: Number(e >> Hu & Io) | 0, l: Number(e & Io) | 0 };
}
function px(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = hx(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const Nu = (e, t, n) => e >>> n, Uu = (e, t, n) => e << 32 - n | t >>> n, Tr = (e, t, n) => e >>> n | t << 32 - n, Pr = (e, t, n) => e << 32 - n | t >>> n, Bo = (e, t, n) => e << 64 - n | t >>> n - 32, To = (e, t, n) => e >>> n - 32 | t << 64 - n;
function Yt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const wx = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), yx = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, gx = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), mx = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, bx = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), vx = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, xx = /* @__PURE__ */ Uint32Array.from([
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
]), Bn = /* @__PURE__ */ new Uint32Array(64);
let kx = class extends ww {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = In[0] | 0, this.B = In[1] | 0, this.C = In[2] | 0, this.D = In[3] | 0, this.E = In[4] | 0, this.F = In[5] | 0, this.G = In[6] | 0, this.H = In[7] | 0;
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
    for (let d = 0; d < 16; d++, n += 4)
      Bn[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = Bn[d - 15], h = Bn[d - 2], p = Pt(f, 7) ^ Pt(f, 18) ^ f >>> 3, y = Pt(h, 17) ^ Pt(h, 19) ^ h >>> 10;
      Bn[d] = y + Bn[d - 7] + p + Bn[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = Pt(a, 6) ^ Pt(a, 11) ^ Pt(a, 25), h = u + f + ux(a, c, l) + xx[d] + Bn[d] | 0, y = (Pt(r, 2) ^ Pt(r, 13) ^ Pt(r, 22)) + fx(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    fr(Bn);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), fr(this.buffer);
  }
};
const yw = px([
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
].map((e) => BigInt(e))), Ex = yw[0], Ax = yw[1], Tn = /* @__PURE__ */ new Uint32Array(80), Pn = /* @__PURE__ */ new Uint32Array(80);
class Sx extends ww {
  constructor(t = 64) {
    super(128, t, 16, !1), this.Ah = Ue[0] | 0, this.Al = Ue[1] | 0, this.Bh = Ue[2] | 0, this.Bl = Ue[3] | 0, this.Ch = Ue[4] | 0, this.Cl = Ue[5] | 0, this.Dh = Ue[6] | 0, this.Dl = Ue[7] | 0, this.Eh = Ue[8] | 0, this.El = Ue[9] | 0, this.Fh = Ue[10] | 0, this.Fl = Ue[11] | 0, this.Gh = Ue[12] | 0, this.Gl = Ue[13] | 0, this.Hh = Ue[14] | 0, this.Hl = Ue[15] | 0;
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: f, Gh: h, Gl: p, Hh: y, Hl: w } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, f, h, p, y, w];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, f, h, p, y, w) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = p | 0, this.Hh = y | 0, this.Hl = w | 0;
  }
  process(t, n) {
    for (let v = 0; v < 16; v++, n += 4)
      Tn[v] = t.getUint32(n), Pn[v] = t.getUint32(n += 4);
    for (let v = 16; v < 80; v++) {
      const L = Tn[v - 15] | 0, T = Pn[v - 15] | 0, C = Tr(L, T, 1) ^ Tr(L, T, 8) ^ Nu(L, T, 7), H = Pr(L, T, 1) ^ Pr(L, T, 8) ^ Uu(L, T, 7), O = Tn[v - 2] | 0, I = Pn[v - 2] | 0, U = Tr(O, I, 19) ^ Bo(O, I, 61) ^ Nu(O, I, 6), z = Pr(O, I, 19) ^ To(O, I, 61) ^ Uu(O, I, 6), E = gx(H, z, Pn[v - 7], Pn[v - 16]), k = mx(E, C, U, Tn[v - 7], Tn[v - 16]);
      Tn[v] = k | 0, Pn[v] = E | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: f, Fh: h, Fl: p, Gh: y, Gl: w, Hh: m, Hl: g } = this;
    for (let v = 0; v < 80; v++) {
      const L = Tr(d, f, 14) ^ Tr(d, f, 18) ^ Bo(d, f, 41), T = Pr(d, f, 14) ^ Pr(d, f, 18) ^ To(d, f, 41), C = d & h ^ ~d & y, H = f & p ^ ~f & w, O = bx(g, T, H, Ax[v], Pn[v]), I = vx(O, m, L, C, Ex[v], Tn[v]), U = O | 0, z = Tr(r, i, 28) ^ Bo(r, i, 34) ^ Bo(r, i, 39), E = Pr(r, i, 28) ^ To(r, i, 34) ^ To(r, i, 39), k = r & o ^ r & a ^ o & a, x = i & s ^ i & c ^ s & c;
      m = y | 0, g = w | 0, y = h | 0, w = p | 0, h = d | 0, p = f | 0, { h: d, l: f } = Yt(l | 0, u | 0, I | 0, U | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const b = wx(U, E, x);
      r = yx(b, I, z, k), i = b | 0;
    }
    ({ h: r, l: i } = Yt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Yt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Yt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = Yt(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: f } = Yt(this.Eh | 0, this.El | 0, d | 0, f | 0), { h, l: p } = Yt(this.Fh | 0, this.Fl | 0, h | 0, p | 0), { h: y, l: w } = Yt(this.Gh | 0, this.Gl | 0, y | 0, w | 0), { h: m, l: g } = Yt(this.Hh | 0, this.Hl | 0, m | 0, g | 0), this.set(r, i, o, s, a, c, l, u, d, f, h, p, y, w, m, g);
  }
  roundClean() {
    fr(Tn, Pn);
  }
  destroy() {
    fr(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
const _x = /* @__PURE__ */ fw(() => new kx()), Ix = /* @__PURE__ */ fw(() => new Sx());
/*! scure-bip39 - MIT License (c) 2022 Patricio Palladino, Paul Miller (paulmillr.com) */
const Bx = (e) => e[0] === "あいこくしん";
function gw(e) {
  if (typeof e != "string")
    throw new TypeError("invalid mnemonic type: " + typeof e);
  return e.normalize("NFKD");
}
function mw(e) {
  const t = gw(e), n = t.split(" ");
  if (![12, 15, 18, 21, 24].includes(n.length))
    throw new Error("Invalid mnemonic");
  return { nfkd: t, words: n };
}
function bw(e) {
  si(e, 16, 20, 24, 28, 32);
}
const Tx = (e) => {
  const t = 8 - e.length / 4;
  return new Uint8Array([_x(e)[0] >> t << t]);
};
function vw(e) {
  if (!Array.isArray(e) || e.length !== 2048 || typeof e[0] != "string")
    throw new Error("Wordlist: expected array of 2048 strings");
  return e.forEach((t) => {
    if (typeof t != "string")
      throw new Error("wordlist: non-string element: " + t);
  }), Ao.chain(Ao.checksum(1, Tx), Ao.radix2(11, !0), Ao.alphabet(e));
}
function xw(e, t) {
  const { words: n } = mw(e), r = vw(t).decode(n);
  return bw(r), r;
}
function Px(e, t) {
  return bw(e), vw(t).encode(e).join(Bx(t) ? "　" : " ");
}
function $x(e, t) {
  try {
    xw(e, t);
  } catch {
    return !1;
  }
  return !0;
}
const Ox = (e) => gw("mnemonic" + e);
function Cx(e, t = "") {
  return lx(Ix, mw(e).nfkd, Ox(t), { c: 2048, dkLen: 64 });
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
`), vs = "bitcoin-address-types-v1", Ol = 20, kw = new TextEncoder().encode(`avibe:protected-hd-wallet:v1
`), Ew = [16, 20, 24, 28, 32], Rx = ["account", "evm", "bitcoin", "bitcoin_p2pkh", "bitcoin_p2wpkh", "solana"];
function Et(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new Error("invalid protected HD object");
  return e;
}
function Ut(e, t) {
  if (Object.keys(e).length !== t.length || t.some((n) => !Object.hasOwn(e, n))) throw new Error("invalid protected HD fields");
}
function Cl(e) {
  if (typeof e != "number" || !Number.isInteger(e) || e < 0 || e >= Ol) throw new Error("invalid protected HD account");
  return e;
}
function Aw(e) {
  const t = Et(e), n = Cl(t.account);
  if (t.chain === "bitcoin") {
    if (Ut(t, ["chain", "account", "address_type"]), t.address_type !== "p2pkh" && t.address_type !== "p2wpkh" && t.address_type !== "p2tr") throw new Error("invalid protected HD Bitcoin address type");
    return { chain: "bitcoin", account: n, address_type: t.address_type };
  }
  if (Ut(t, ["chain", "account"]), t.chain !== "evm" && t.chain !== "solana") throw new Error("invalid protected HD chain");
  return { chain: t.chain, account: n };
}
function xs(e, t, n = "p2tr") {
  if (Cl(t), e === "evm") return `m/44'/60'/0'/0/${t}`;
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
  const n = We(e);
  if (t ? !kt.utils.isValidPublicKey(n, !1) : !fe.utils.isValidPublicKey(n)) throw new Error("invalid protected HD public key");
  return e;
}
function sa(e) {
  const t = Et(e);
  if (Ut(t, ["kind", "profile", "accounts"]), t.kind !== "hd-seed" || t.profile !== vs || !Array.isArray(t.accounts) || t.accounts.length !== Ol) throw new Error("invalid protected HD profile/table");
  return { kind: "hd-seed", profile: vs, accounts: t.accounts.map((n, r) => {
    const i = Et(n);
    if (Ut(i, Rx), Cl(i.account) !== r) throw new Error("protected HD accounts must be ordered 0..19");
    return { account: r, evm: tn(i.evm), bitcoin: tn(i.bitcoin), solana: tn(i.solana, !0), bitcoin_p2pkh: tn(i.bitcoin_p2pkh), bitcoin_p2wpkh: tn(i.bitcoin_p2wpkh) };
  }) };
}
function Lx(e) {
  if (!Ew.includes(e.entropy.length) || e.seed.length !== 64 || typeof e.passphraseRequired != "boolean") throw new Error("invalid protected HD payload");
  const t = new Uint8Array(95 + e.entropy.length);
  return t.set(kw), t[29] = e.entropy.length, t.set(e.entropy, 30), t.set(e.seed, 30 + e.entropy.length), t[t.length - 1] = Number(e.passphraseRequired), t;
}
function Sw(e) {
  const t = e[29];
  if (!Ew.includes(t) || e.length !== 95 + t || kw.some((n, r) => n !== e[r]) || ![0, 1].includes(e[e.length - 1])) throw new Error("invalid protected HD payload");
  return { entropy: e.slice(30, 30 + t), seed: e.slice(30 + t, 94 + t), passphraseRequired: e[e.length - 1] === 1 };
}
function ks(e) {
  e?.entropy.fill(0), e?.seed.fill(0);
}
function _w(e) {
  return e.normalize("NFKD").toLowerCase().trim().split(/\s+/u).filter(Boolean).join(" ");
}
function Hx(e) {
  return Px(e, ai);
}
function Iw(e, t) {
  const n = _w(e), r = xw(n, ai);
  try {
    return { entropy: r, seed: Cx(n, t), passphraseRequired: t.normalize("NFKD").length > 0 };
  } catch (i) {
    throw r.fill(0), i;
  }
}
function Bw(e, t) {
  let n = xc(gc, new TextEncoder().encode("ed25519 seed"), e);
  try {
    for (const r of [44, 501, t, 0]) {
      const i = new Uint8Array(37);
      i.set(n.subarray(0, 32), 1), new DataView(i.buffer).setUint32(33, 2147483648 + r, !1);
      let o;
      try {
        o = xc(gc, n.subarray(32), i);
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
function Nx(e, t, n) {
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  const r = Ln.fromMasterSeed(e);
  let i, o;
  try {
    if (i = r.derive(t), !i.privateKey) throw new Error("protected HD derivation failed");
    return o = new Uint8Array(i.privateKey), n(o);
  } finally {
    o?.fill(0), i?.wipePrivateData(), r.wipePrivateData();
  }
}
function Du(e, t, n) {
  const r = Aw(t);
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  if (r.chain !== "solana") return Nx(e, xs(r.chain, r.account, r.chain === "bitcoin" ? r.address_type : void 0), n);
  const i = Bw(e, r.account);
  try {
    return n(i);
  } finally {
    i.fill(0);
  }
}
function Pc(e) {
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  const t = Ln.fromMasterSeed(e);
  try {
    return Array.from({ length: Ol }, (n, r) => {
      const i = (s) => {
        const a = t.derive(s);
        try {
          if (!a.publicKey) throw new Error("protected HD derivation failed");
          return G(a.publicKey);
        } finally {
          a.wipePrivateData();
        }
      }, o = Bw(e, r);
      try {
        return { account: r, evm: i(xs("evm", r)), bitcoin: i(xs("bitcoin", r)), solana: G(kt.getPublicKey(o)), bitcoin_p2pkh: i(`m/44'/0'/0'/0/${r}`), bitcoin_p2wpkh: i(`m/84'/0'/0'/0/${r}`) };
      } finally {
        o.fill(0);
      }
    });
  } finally {
    t.wipePrivateData();
  }
}
function Rl(e) {
  const t = We(tn(e.evm)), n = G(ds(fe.Point.fromBytes(t).toBytes(!1).slice(1)).slice(-20)), r = G(ds(new TextEncoder().encode(n))), i = (l) => gl(ue(We(tn(l)))), o = We(tn(e.bitcoin)).slice(1), s = BigInt(`0x${G(Cn.utils.taggedHash("TapTweak", o))}`);
  if (s >= fe.Point.Fn.ORDER) throw new Error("invalid protected HD Taproot tweak");
  const a = fe.Point.fromBytes(new Uint8Array([2, ...o])), c = s === 0n ? a : a.add(fe.Point.BASE.multiply(s));
  if (c.equals(fe.Point.ZERO)) throw new Error("invalid protected HD Taproot output");
  return {
    eth: "0x" + [...n].map((l, u) => parseInt(r[u], 16) >= 8 ? l.toUpperCase() : l).join(""),
    btc_legacy: Q1(ue).encode(new Uint8Array([0, ...i(e.bitcoin_p2pkh)])),
    btc_segwit: Ou.encode("bc", [0, ...Ou.toWords(i(e.bitcoin_p2wpkh))]),
    btc_taproot: Cu.encode("bc", [1, ...Cu.toWords(c.toBytes(!0).slice(1))]),
    solana: sw.encode(We(tn(e.solana, !0)))
  };
}
function Tw(e) {
  const t = JSON.stringify([e.ciphertext, e.nonce, e.wrap_meta]).replace(/[\u007f-\uffff]/g, (n) => "\\u" + n.charCodeAt(0).toString(16).padStart(4, "0"));
  return G(ue(new TextEncoder().encode(t)));
}
function Es(e) {
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
  return r(t, 1), Et(t);
}
function or(e, t) {
  if (typeof e != "string" || e.length > Math.ceil(Math.max(...t) / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) throw new Error("invalid protected HD base64");
  const n = Uint8Array.from(atob(e), (r) => r.charCodeAt(0));
  if (!t.includes(n.length) || btoa(String.fromCharCode(...n)) !== e) throw new Error("invalid protected HD field length/encoding");
  return n;
}
function Ux(e, t) {
  if (t.v !== 1 || t.scheme !== "machine-aesgcm-v1") throw new Error("invalid protected HD envelope version/scheme");
  or(e.nonce, [12]), or(e.ciphertext, [127, 131, 135, 139, 143]), or(t.dek_nonce, [12]), or(t.wrapped_dek, [48]);
}
const zu = { '"': 34, "\\": 92, "/": 47, b: 8, f: 12, n: 10, r: 13, t: 9 };
function Dx(e) {
  if (typeof e != "string") return !1;
  let t = 0;
  const n = () => {
    for (; t < e.length && /[\x20\t\r\n]/.test(e[t]); ) t += 1;
  }, r = (a) => {
    if (e[t++] !== '"') return !1;
    let c = 0, l = a !== void 0;
    for (; t < e.length; ) {
      let u = e.charCodeAt(t++);
      if (u === 34) return l && c === a.length;
      if (u === 92) {
        if (t >= e.length) return !1;
        const d = e[t++];
        if (d === "u") {
          const f = e.slice(t, t + 4);
          if (f.length !== 4 || !/^[0-9a-fA-F]{4}$/.test(f)) return !1;
          u = Number.parseInt(f, 16), t += 4;
        } else {
          if (!Object.hasOwn(zu, d)) return !1;
          u = zu[d];
        }
      }
      (a === void 0 || c >= a.length || u !== a.charCodeAt(c)) && (l = !1), c += 1;
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
const zx = "hpke-x25519-hkdfsha256-aes256gcm-v1", Mx = "avault:blind-box:v1", Kx = "avault:blind-box:aad:v1", As = "machine-aesgcm-v1", aa = 1, ca = "ecdsa-secp256k1-recoverable", Ni = "ed25519", Ss = "ecdsa-secp256k1-der", Ll = "schnorr-secp256k1-bip340", Ui = "schnorr-secp256k1-bip341-keypath", Ae = 32, _s = 12, la = 32, Fx = "avault:protected-vmk:kek-passkey:v1", jx = /^[A-Za-z_][A-Za-z0-9_]*$/, Vx = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[a-z][a-z0-9_]{0,63})?$/, qx = "avault:protected-root-meta:v1", Wx = new TextEncoder(), Gx = new TextDecoder();
function Hl(e) {
  switch (e) {
    case Ni:
    case ca:
    case Ss:
    case Ll:
    case Ui:
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
function Nl(e) {
  if (Hl(e), e === Ni) throw new Error("Ed25519 requires exact-message signing, not a digest");
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
function Ve(e) {
  return Wx.encode(e);
}
function Is(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(Is).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${Is(t[n])}`).join(",")}}`;
}
function dr(e) {
  const t = new Uint8Array(e);
  return crypto.getRandomValues(t), t;
}
function ce(e) {
  const t = Se(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return t.fill(0), btoa(n);
}
function ve(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = t.padEnd(Math.ceil(t.length / 4) * 4, "=");
  return Uint8Array.from(atob(n), (r) => r.charCodeAt(0));
}
function ut(e) {
  return We(e.startsWith("0x") ? e.slice(2) : e);
}
function Mu(e) {
  return /^(0x)?[0-9a-fA-F]+$/.test(e) && (e.startsWith("0x"), e.length % 2 === 0) ? ut(e) : ve(e);
}
function Zx(e) {
  return G(Se(e));
}
function Pw(e, t = "bytes") {
  return Se(e, t);
}
function Re(e, t, n) {
  if (e.length !== t)
    throw new Error(`${n} must be ${t} bytes`);
}
function vt(e) {
  e && new Uint8Array(e).fill(0);
}
async function Bs(e, t, n, r) {
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
    vt(i), vt(o), vt(s), vt(a);
  }
}
async function da(e, t, n, r) {
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
    vt(i), vt(o), vt(s), vt(a);
  }
}
async function Yx(e, t, n, r) {
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
    vt(i), vt(o), vt(s);
  }
}
async function $w(e, t) {
  const n = Se(e, "passkey PRF output");
  try {
    const r = Se(t, "passkey PRF salt");
    try {
      if (n.length === 0)
        throw new Error("passkey PRF output is empty");
      return Re(r, la, "passkey PRF salt"), await Yx(n, r, Ve(Fx), Ae);
    } finally {
      r.fill(0);
    }
  } finally {
    n.fill(0);
  }
}
function Xx() {
  return dr(Ae);
}
function Jx() {
  return dr(la);
}
function Qx(e) {
  const t = Se(e, "passkey PRF salt");
  try {
    return Re(t, la, "passkey PRF salt"), { prf: { eval: { first: rt(t) } } };
  } finally {
    t.fill(0);
  }
}
function _t(e) {
  const t = typeof e == "string" ? JSON.parse(e) : e;
  if (t?.v !== aa || !Array.isArray(t.copies))
    throw new Error("invalid protected wrap_meta");
  return t;
}
async function e2(e, t) {
  const n = Se(e, "VMK"), r = dr(_s);
  let i, o;
  try {
    Re(n, Ae, "VMK"), o = Se(t.prfSalt, "passkey PRF salt"), Re(o, la, "passkey PRF salt"), i = await $w(t.prfOutput, o);
    const s = await Bs(i, r, n);
    return {
      kind: "passkey",
      kdf: "webauthn-prf-hkdf-sha256",
      prf_salt: ce(o),
      nonce: ce(r),
      wrapped: ce(s),
      ...t.credentialId ? { credential_id: t.credentialId } : {}
    };
  } finally {
    i?.fill(0), o?.fill(0), n.fill(0);
  }
}
async function t2(e, t) {
  const n = Se(e, "VMK");
  try {
    if (Re(n, Ae, "VMK"), t.length === 0)
      throw new Error("at least one protected unlock factor is required");
    const r = [];
    for (const i of t)
      r.push(await e2(n, i));
    return JSON.stringify({ v: aa, copies: r });
  } finally {
    n.fill(0);
  }
}
async function n2(e, t) {
  const n = ve(e.prf_salt);
  if (t.prfSalt && ce(t.prfSalt) !== e.prf_salt)
    throw n.fill(0), new Error("passkey PRF salt does not match copy");
  const r = await $w(t.prfOutput, n);
  try {
    return await da(r, ve(e.nonce), ve(e.wrapped));
  } finally {
    r.fill(0), n.fill(0);
  }
}
async function Ow(e, t) {
  const n = _t(e);
  for (const r of n.copies)
    try {
      if (r.kind === "passkey") {
        const i = await n2(r, t);
        try {
          return Re(i, Ae, "VMK"), i;
        } catch (o) {
          throw i.fill(0), o;
        }
      }
    } catch {
    }
  throw new Error("no protected VMK copy could be unwrapped");
}
function ua(e) {
  return _t(e).copies.filter((t) => t.kind === "passkey").map((t) => ({
    prfSalt: ve(t.prf_salt),
    ...t.credential_id ? { credentialId: t.credential_id } : {}
  }));
}
function r2(e, t) {
  const n = _t(e);
  return JSON.stringify({
    ...n,
    rp_id: t.rpId,
    vault_user_handle: t.vaultUserHandle
  });
}
function Cw(e) {
  const t = _t(e);
  return delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, JSON.stringify(t);
}
function Ul(e) {
  if (e.kind === "hd-seed") {
    if (e.publicKey !== void 0 || e.curve !== void 0 || !e.hd) throw new Error("invalid protected HD context");
    return sa(e.hd);
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
    return t === "ed25519" && Vl(e.publicKey), {
      kind: "keypair",
      public_key: e.publicKey.toLowerCase(),
      ...t === "ed25519" ? { curve: t } : {}
    };
  }
  throw new Error("unsupported protected record kind");
}
function i2(e) {
  if (e === void 0) return;
  if (typeof e != "object" || e === null)
    throw new Error("protected record metadata is invalid");
  const t = e;
  if (t.kind === "hd-seed") return sa(t);
  if (t.curve !== void 0 && t.curve !== "ed25519")
    throw new Error("protected record metadata curve is invalid");
  if (t.kind === "static") {
    if (t.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (t.kind === "keypair")
    return Ul({ kind: "keypair", publicKey: t.public_key, curve: t.curve });
  throw new Error("protected record metadata kind is invalid");
}
function fa(e, t) {
  return t?.kind === "hd-seed" ? { name: e, kind: "hd-seed", hd: sa(t) } : {
    name: e,
    ...t ? { kind: t.kind } : {},
    ...t?.public_key ? { publicKey: t.public_key } : {},
    ...t?.curve ? { curve: t.curve } : {}
  };
}
function Dl(e) {
  return Ve(
    Is({
      domain: "avault:protected-record:aad:v2",
      name: e.kind === "keypair" || e.kind === "hd-seed" ? Nw(e.name) : Xr(e.name),
      scheme: e.scheme ?? As,
      version: e.version ?? aa,
      record: Ul(e) ?? null
    })
  );
}
async function zl(e, t, n) {
  const r = Se(t, "VMK"), i = dr(Ae);
  let o;
  try {
    Re(r, Ae, "VMK"), o = Se(e, "value");
    const s = Dl(n), a = dr(_s), c = await Bs(i, a, o, s), l = dr(_s), u = await Bs(r, l, i, s);
    return {
      ciphertext: ce(c),
      nonce: ce(a),
      dek_nonce: ce(l),
      wrapped_dek: ce(u)
    };
  } finally {
    i.fill(0), r.fill(0), o?.fill(0);
  }
}
async function Rw(e, t, n) {
  const r = Se(t, "VMK");
  try {
    Re(r, Ae, "VMK");
    const i = await da(
      r,
      ve(e.dek_nonce),
      ve(e.wrapped_dek),
      Dl(n)
    );
    try {
      return Re(i, Ae, "DEK"), i;
    } catch (o) {
      throw i.fill(0), o;
    }
  } finally {
    r.fill(0);
  }
}
async function li(e, t, n) {
  const r = await Rw(e, t, n);
  try {
    return await da(
      r,
      ve(e.nonce),
      ve(e.ciphertext),
      Dl(n)
    );
  } finally {
    r.fill(0);
  }
}
function Ml(e, t, n) {
  const r = _t(t);
  if ("dek_nonce" in r || "wrapped_dek" in r)
    throw new Error("vmk wrap_meta must not already carry a wrapped DEK");
  if ("scheme" in r && r.scheme !== As)
    throw new Error("protected wrap_meta has unsupported scheme");
  const i = n ? Ul(n) : void 0;
  return {
    ciphertext: e.ciphertext,
    nonce: e.nonce,
    wrap_meta: JSON.stringify({
      ...r,
      scheme: As,
      ...i ? { record_meta: i } : {},
      dek_nonce: e.dek_nonce,
      wrapped_dek: e.wrapped_dek
    })
  };
}
function Vn(e) {
  const t = Dx(e.wrap_meta), n = _t(t ? Es(e.wrap_meta) : e.wrap_meta);
  if (n.record_meta?.kind === "hd-seed") {
    const c = t ? n : Es(e.wrap_meta);
    Ux(e, c);
  }
  const { dek_nonce: r, wrapped_dek: i, record_meta: o, ...s } = n;
  if (typeof r != "string" || typeof i != "string")
    throw new Error("protected record wrap_meta is missing the wrapped DEK");
  const a = i2(o);
  return {
    sealed: { ciphertext: e.ciphertext, nonce: e.nonce, dek_nonce: r, wrapped_dek: i },
    vmkWrapMeta: JSON.stringify(s),
    ...a ? { recordMetadata: a } : {}
  };
}
function Lw(e) {
  const t = { ..._t(e) };
  return delete t.root_meta, delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, delete t.scheme, Ve(Is({ domain: qx, vault: t }));
}
async function o2(e, t, n) {
  const r = dr(_s), i = Ve(JSON.stringify(t));
  try {
    const o = await Bs(e, r, i, Lw(n));
    return { v: 1, nonce: ce(r), ciphertext: ce(o) };
  } finally {
    i.fill(0);
  }
}
async function Ts(e, t) {
  const n = _t(e).root_meta;
  if (!n) return null;
  if (n.v !== 1 || typeof n.nonce != "string" || typeof n.ciphertext != "string")
    throw new Error("invalid protected root metadata");
  const r = await da(t, ve(n.nonce), ve(n.ciphertext), Lw(e));
  try {
    const i = JSON.parse(Gx.decode(r));
    if (typeof i != "object" || i === null) throw new Error("invalid protected root metadata");
    return i;
  } finally {
    r.fill(0);
  }
}
function s2(e, t) {
  const n = _t(e);
  return JSON.stringify({ ...n, root_meta: t });
}
function a2() {
  return new o0({
    kem: new w0(),
    kdf: new s0(),
    aead: new Mb()
  });
}
function Kl(e) {
  return ve(typeof e == "string" ? e : e.public_key);
}
async function Hw(e) {
  const t = await crypto.subtle.digest("SHA-256", rt(e));
  return G(new Uint8Array(t));
}
async function c2(e) {
  return Hw(Kl(e));
}
function l2(e) {
  if (!e.fingerprint)
    throw new Error("protected DEK release requires a pinned avault public key fingerprint");
  return e;
}
async function d2(e) {
  const t = l2(e), n = Kl(t);
  if (Re(n, Ae, "avault public key"), await Hw(n) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  return t;
}
async function u2(e, t, n) {
  const r = a2(), i = Kl(t);
  if (Re(i, Ae, "avault public key"), typeof t != "string" && t.fingerprint && await c2(t) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  const o = await r.kem.deserializePublicKey(i), s = typeof e == "string" ? Ve(e) : Se(e, "plaintext");
  try {
    const a = await r.seal(
      { recipientPublicKey: o, info: Ve(Mx) },
      s,
      b2(n)
    );
    return {
      scheme: zx,
      enc: ce(new Uint8Array(a.enc)),
      ct: ce(new Uint8Array(a.ct))
    };
  } finally {
    s.fill(0);
  }
}
function Nw(e) {
  if (typeof e != "string" || !jx.test(e))
    throw new Error("vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$");
  return e;
}
function Xr(e) {
  if (typeof e != "string" || !Vx.test(e))
    throw new Error("vault record name must match ^[A-Za-z_][A-Za-z0-9_]*(\\.[a-z][a-z0-9_]{0,63})?$");
  return e;
}
function Ku(e, t) {
  if (typeof e != "string" || e.trim() === "")
    throw new Error(`${t} is required`);
  return e;
}
function Fl(e) {
  const t = typeof e == "string" ? ut(e) : Se(e, "digest");
  return Re(t, Ae, "digest"), t;
}
function f2(e) {
  const t = typeof e == "string" ? ut(e) : Se(e, "private key");
  try {
    if (Re(t, Ae, "private key"), !fe.utils.isValidSecretKey(t))
      throw new Error("invalid secp256k1 private key");
    return t;
  } catch (n) {
    throw t.fill(0), n;
  }
}
function Ps(e, t) {
  return typeof e == "string" ? ut(e) : Se(e, t);
}
function $s(e) {
  const t = Ps(e, "operation hash");
  return Re(t, Ae, "operation hash"), t;
}
function Os(e, t) {
  if (typeof e == "number" && (!Number.isSafeInteger(e) || e < 0))
    throw new Error(`${t} out of bounds`);
  const n = typeof e == "bigint" ? e : BigInt(e);
  if (n < 0n || n > 0xffffffffffffffffn)
    throw new Error(`${t} out of bounds`);
  return n;
}
function Uw(e, t) {
  const n = Os(e, t), r = new Uint8Array(8);
  return new DataView(r.buffer).setBigUint64(0, n, !1), r;
}
function h2(e) {
  return Uw(e, "approval expiry");
}
function Fu(e) {
  const t = Se(e.nonce, "approval nonce");
  if (t.length < 16 || t.length > 128)
    throw new Error("approval nonce must be 16..128 bytes");
  return { approvalNonce: t, approvalExpiresAtUnix: Os(e.expiresAtUnix, "approval expiry") };
}
function at(e, t) {
  const n = Se(t);
  if (n.length > 4294967295)
    throw new Error("blind-box AAD field is too large");
  e.push(n.length >>> 24 & 255, n.length >>> 16 & 255, n.length >>> 8 & 255, n.length & 255), e.push(...n);
}
function p2(e) {
  switch (e.purpose) {
    case "seal":
      return w2(e.name);
    case "agent-deliver": {
      const t = Fu({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-deliver",
        name: Xr(e.name),
        grantId: Ku(e.grantId, "grant id"),
        ttlSecs: Os(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: $s(e.operationHash)
      };
    }
    case "agent-sign": {
      Nl(e.signScheme);
      const t = Fu({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-sign",
        name: Nw(e.name),
        grantId: Ku(e.grantId, "grant id"),
        signScheme: e.signScheme,
        digest: Fl(e.digest),
        ttlSecs: Os(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: $s(e.operationHash)
      };
    }
    default:
      throw new Error("unsupported blind-box context purpose");
  }
}
function w2(e) {
  return { purpose: "seal", name: Xr(e) };
}
async function y2(e) {
  const t = $s(e.operationHash), n = await m2(e.name, e.ttlSecs);
  if (G(t) !== G(n))
    throw new Error("operation hash does not match release operation fields");
}
async function g2(e) {
  const t = [];
  for (const n of e)
    at(t, typeof n == "string" ? Ve(n) : Se(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(t)));
}
async function m2(e, t) {
  return g2(["agent-deliver", Xr(e), Uw(t, "ttl seconds")]);
}
function b2(e) {
  const t = p2(e), n = [...Ve(Kx)];
  return at(n, Ve(t.purpose)), at(n, Ve(t.name)), at(n, Ve(As)), at(n, new Uint8Array([aa])), at(n, t.purpose === "agent-deliver" || t.purpose === "agent-sign" ? Ve(t.grantId) : new Uint8Array()), at(n, new Uint8Array()), at(n, t.purpose === "agent-sign" ? Ve(t.signScheme) : new Uint8Array()), at(n, t.purpose === "agent-sign" ? Fl(t.digest) : new Uint8Array()), at(n, t.purpose === "seal" ? new Uint8Array() : Se(t.approvalNonce, "approval nonce")), at(n, t.purpose === "seal" ? new Uint8Array() : h2(t.approvalExpiresAtUnix)), at(n, t.purpose === "seal" ? new Uint8Array() : $s(t.operationHash)), new Uint8Array(n);
}
async function v2(e, t, n, r, i) {
  if (r.kind !== void 0 && r.kind !== "static")
    throw new Error("protected DEK release requires a static protected record");
  if (Xr(r.name) !== Xr(i.name))
    throw new Error("protected DEK release context name does not match record name");
  await y2(i);
  const o = await d2(n), s = await Rw(e, t, r);
  try {
    return await u2(s, o, i);
  } finally {
    s.fill(0);
  }
}
function x2(e = "secp256k1") {
  if (ci(e) === "ed25519") {
    const n = kt.utils.randomSecretKey();
    try {
      return { privateKey: n, publicKey: G(kt.getPublicKey(n)) };
    } catch (r) {
      throw n.fill(0), r;
    }
  }
  const t = fe.utils.randomSecretKey();
  return { privateKey: t, publicKey: G(fe.getPublicKey(t, !0)) };
}
function ju(e) {
  return ut(e.toString(16).padStart(Ae * 2, "0"));
}
function jl(e, t, n, r = {}) {
  if (Nl(n), !r || typeof r != "object" || Array.isArray(r) || Object.keys(r).some((s) => s !== "schnorrAuxRand"))
    throw new Error("unsupported signing option; script trees are not supported");
  const i = f2(e);
  let o;
  try {
    const s = Fl(t);
    if (n === ca) {
      const a = fe.sign(s, i, { prehash: !1, lowS: !0, format: "recovered" });
      return {
        scheme: n,
        signature: G(a.slice(1)),
        recovery_id: a[0] ?? null
      };
    }
    if (n === Ss)
      return {
        scheme: n,
        signature: G(fe.sign(s, i, { prehash: !1, lowS: !0, format: "der" })),
        recovery_id: null
      };
    if (n === Ll) {
      o = r.schnorrAuxRand !== void 0 ? Ps(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(Ae)), Re(o, Ae, "schnorr aux randomness");
      const a = Cn.sign(s, i, o);
      if (!Cn.verify(a, s, Cn.getPublicKey(i))) throw new Error("signing self-verification failed");
      return { scheme: n, signature: G(a), recovery_id: null };
    }
    if (n === Ui) {
      const a = fe.getPublicKey(i, !0), c = ql(a.slice(1));
      let l = BigInt(`0x${G(i)}`);
      a[0] === 3 && (l = Ai - l);
      const u = BigInt(`0x${G(Cn.utils.taggedHash("TapTweak", a.slice(1)))}`);
      if (u >= Ai) throw new Error("invalid taproot tweak");
      let d = (l + u) % Ai;
      if (d === 0n) throw new Error("invalid taproot signing key");
      let f = ju(d);
      try {
        c.toBytes(!0)[0] === 3 && (d = Ai - d, f.fill(0), f = ju(d)), o = r.schnorrAuxRand !== void 0 ? Ps(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(Ae)), Re(o, Ae, "schnorr aux randomness");
        const h = Cn.sign(s, f, o);
        if (!Cn.verify(h, s, c.toBytes(!0).slice(1))) throw new Error("Taproot signing self-verification failed");
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
async function k2(e, t, n, r, i, o = {}) {
  if (n.kind !== "keypair" || !n.publicKey)
    throw new Error("protected signing requires a keypair protected record");
  if (Nl(i), ci(n.curve) !== "secp256k1") throw new Error("digest signing requires a secp256k1 protected record");
  const s = await li(e, t, n);
  try {
    if (G(fe.getPublicKey(s, !0)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return jl(s, r, i, o);
  } finally {
    s.fill(0);
  }
}
function Dw(e, t) {
  const n = ml(t), r = Ps(e, "Ed25519 seed");
  try {
    Re(r, Ae, "Ed25519 seed");
    const i = kt.sign(n, r);
    if (!kt.verify(i, n, kt.getPublicKey(r), { zip215: !1 }))
      throw new Error("Ed25519 signing self-verification failed");
    return { scheme: Ni, signature: G(i), recovery_id: null };
  } finally {
    r.fill(0);
  }
}
function Vl(e) {
  if (!/^[0-9a-fA-F]{64}$/.test(e)) throw new Error("expected a 32-byte Ed25519 public key");
  const t = ut(e);
  if (!kt.utils.isValidPublicKey(t, !1)) throw new Error("invalid Ed25519 public key");
  return t;
}
async function E2(e, t, n, r) {
  if (n.kind !== "keypair" || n.curve !== "ed25519" || !n.publicKey)
    throw new Error("exact-message signing requires an Ed25519 keypair protected record");
  const i = ml(r);
  if (i.length === 0) throw new Error("protected Ed25519 message must not be empty");
  Vl(n.publicKey);
  const o = await li(e, t, n);
  try {
    if (Re(o, Ae, "Ed25519 seed"), G(kt.getPublicKey(o)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return Dw(o, i);
  } finally {
    o.fill(0);
  }
}
function A2(e) {
  return Kw(Vl(e));
}
function S2(e) {
  const t = e.rootMetadata?.daemon?.verificationKeys?.find(
    (n) => n.alg === "ed25519" && n.keyId === e.keyId
  );
  if (!t)
    throw new Error("daemon verification key is not pinned");
  return kt.verify(Mu(e.signature), Ve(e.message), Mu(t.publicKey));
}
function zw(e) {
  const t = ut(e.trim());
  if (t.length === 33 && (t[0] === 2 || t[0] === 3) && fe.utils.isValidPublicKey(t))
    return t;
  if (t.length === 65 && t[0] === 4 && fe.utils.isValidPublicKey(t))
    return fe.Point.fromBytes(t).toBytes(!0);
  throw new Error("expected a valid secp256k1 public key");
}
const _2 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", I2 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", B2 = 1, T2 = 734539939, Ai = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
function P2(e) {
  return gl(ue(e));
}
function Mw(e) {
  const t = ue(ue(e)).slice(0, 4), n = new Uint8Array(e.length + t.length);
  return n.set(e), n.set(t, e.length), Kw(n);
}
function Kw(e) {
  let t = BigInt(`0x${G(e)}`), n = "";
  for (; t > 0n; ) {
    const i = Number(t % 58n);
    t /= 58n, n = _2[i] + n;
  }
  let r = 0;
  for (const i of e) {
    if (i !== 0) break;
    r += 1;
  }
  return "1".repeat(r) + n;
}
function $2(e) {
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
function O2(e) {
  return [...e].map((t) => t.charCodeAt(0) >>> 5).concat([0], [...e].map((t) => t.charCodeAt(0) & 31));
}
function C2(e, t, n) {
  const r = O2(e).concat(t), i = $2(r.concat([0, 0, 0, 0, 0, 0])) ^ n;
  return [0, 1, 2, 3, 4, 5].map((o) => i >>> 5 * (5 - o) & 31);
}
function R2(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >>> i & s);
  return i && o.push(r << n - i & s), o;
}
function $c(e, t, n) {
  const r = t === 0 ? B2 : T2, i = [t].concat(R2(n, 8, 5));
  return `${e}1${i.concat(C2(e, i, r)).map((o) => I2[o]).join("")}`;
}
function Fw(e) {
  const t = e.toLowerCase(), n = G(ds(Ve(t)));
  return [...t].map((r, i) => /[a-f]/.test(r) && Number.parseInt(n[i], 16) >= 8 ? r.toUpperCase() : r).join("");
}
function ql(e) {
  const t = BigInt(`0x${G(Cn.utils.taggedHash("TapTweak", e))}`);
  if (t >= Ai) throw new Error("invalid taproot tweak");
  const n = fe.Point.fromBytes(new Uint8Array([2, ...e])), r = t === 0n ? n : n.add(fe.Point.BASE.multiply(t));
  if (r.equals(fe.Point.ZERO)) throw new Error("invalid taproot output key");
  return r;
}
function jw(e) {
  return ql(zw(e).slice(1)).toBytes(!0).slice(1);
}
function ha(e) {
  const t = zw(e), n = fe.Point.fromBytes(t), r = n.toBytes(!1).slice(1), i = ds(r), o = P2(t), s = new Uint8Array(21);
  s[0] = 0, s.set(o, 1);
  const a = n.toBytes(!1).slice(1, 33), c = ql(a);
  return {
    eth: `0x${Fw(G(i.slice(-20)))}`,
    btc_legacy: Mw(s),
    btc_segwit: $c("bc", 0, o),
    btc_taproot: $c("bc", 1, c.toBytes(!1).slice(1, 33))
  };
}
const Wl = {
  windowSeconds: 600,
  strictApprovals: !1,
  parentValueSealAllowed: !0
};
let Oc = { ...Wl };
function L2(e) {
  return e === 300 || e === 600 || e === 1800 ? e : Wl.windowSeconds;
}
function H2(e, t) {
  if (e === 300 || e === 600 || e === 1800) return e;
  throw new S("invalid_payload", `${t}.windowSeconds must be 300, 600, or 1800`);
}
function Gl(e, t = "policy") {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be an object`);
  const n = e;
  if (typeof n.strictApprovals != "boolean") throw new S("invalid_payload", `${t}.strictApprovals must be a boolean`);
  if (typeof n.parentValueSealAllowed != "boolean")
    throw new S("invalid_payload", `${t}.parentValueSealAllowed must be a boolean`);
  return {
    windowSeconds: H2(n.windowSeconds, t),
    strictApprovals: n.strictApprovals,
    parentValueSealAllowed: n.parentValueSealAllowed
  };
}
function N2(e) {
  if (typeof e != "object" || e === null) return { ...Wl };
  const t = e;
  return {
    windowSeconds: L2(t.windowSeconds),
    strictApprovals: t.strictApprovals === !0,
    parentValueSealAllowed: t.parentValueSealAllowed !== !1
  };
}
function Zl(e) {
  return Oc = N2(e), Oc;
}
function Le() {
  return { ...Oc };
}
const U2 = "avibe-vault-lock:v1:", V = {
  vmk: null,
  wrapMeta: null,
  freshSetup: !1,
  scopeId: null
};
let sn = null, Jr = null, ur = null, Di = null, Cc = null, ln = 0;
const Cs = /* @__PURE__ */ new Set();
let Nr = 0, Vw = null;
function D2(e) {
  Vw = e;
}
function Vu() {
  return V.vmk ? "unlocked" : V.wrapMeta ? "locked" : "needs-setup";
}
function qw(e) {
  Vw?.({
    state: Vu(),
    ...sn !== null && Vu() === "unlocked" ? { expiresAt: sn } : {},
    reason: e
  });
}
function Yl() {
  ur !== null && (clearTimeout(ur), ur = null);
}
function zi() {
  Di?.close(), Di = null, Cc = null;
}
function Ww(e) {
  const t = new TextEncoder().encode(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return btoa(n).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function z2(e) {
  let t = 2166136261;
  for (let n = 0; n < e.length; n += 1)
    t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
  return t.toString(16).padStart(8, "0");
}
function Gw(e) {
  const t = _t(e);
  return typeof t.vault_user_handle == "string" && t.vault_user_handle.length > 0 ? Ww(t.vault_user_handle) : `wrap-${z2(e)}`;
}
function M2(e) {
  return Ww(e);
}
function Xl(e) {
  if (typeof window > "u" || typeof BroadcastChannel > "u" || !e) {
    zi();
    return;
  }
  const t = `${U2}${e}`;
  Cc !== t && (zi(), Cc = t, Di = new BroadcastChannel(t), Di.onmessage = (n) => {
    const r = n.data;
    r?.type === "lock" && r.scopeId === e && ht({ broadcast: !1, reason: "manual-lock" });
  });
}
function K2() {
  V.scopeId && (Xl(V.scopeId), Di?.postMessage({ type: "lock", scopeId: V.scopeId }));
}
function Lt() {
  return new Error("vault-operation-aborted");
}
function Zw(e = {}) {
  ln += 1;
  for (const t of Cs)
    t.abort(Lt());
  Cs.clear();
  for (const [t, n] of nt)
    e.preserveAutoLockOperations && n.surviveAutoLock || (t.abort(Lt()), nt.delete(t));
}
function tt(e) {
  const t = { ..._t(e) };
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
function Yw(e) {
  if (nt.size === 0) return;
  const t = tt(e);
  for (const [n, r] of nt)
    rn(r.root, t) || n.abort(Lt());
}
function pa(e, t = {}) {
  Rs();
  const n = tt(e);
  if (V.wrapMeta && !rn(tt(V.wrapMeta), n) || (ft(e), !V.wrapMeta || !rn(tt(V.wrapMeta), n))) throw Lt();
  for (const l of nt.keys()) l.abort(Lt());
  nt.clear();
  const r = ++Nr, i = ln, o = t.surviveAutoLock === !0, s = o ? qu() : Rc(), a = qu(), c = new AbortController();
  return nt.set(c, { root: n, surviveAutoLock: o }), Xl(V.scopeId), {
    freshSetup: s,
    signal: c.signal,
    assertCurrent: () => {
      if (Rs(), c.signal.aborted || r !== Nr || !o && i !== ln || !o && s !== Rc() || !V.wrapMeta || !rn(tt(V.wrapMeta), n)) throw Lt();
    },
    lockIfCurrent: (l = {}) => c.signal.aborted || r !== Nr ? !1 : Xw(i, n, l),
    release: (l = {}) => {
      const u = !l.completed && !c.signal.aborted && r === Nr && a && !V.vmk && V.freshSetup && V.wrapMeta !== null && rn(tt(V.wrapMeta), n);
      nt.delete(c), c.abort(Lt()), u && Jw(), !V.vmk && nt.size === 0 && zi();
    }
  };
}
function Xw(e, t, n = {}) {
  return e !== ln || !V.vmk || !V.wrapMeta || !rn(tt(V.wrapMeta), t) ? !1 : (ht(n), !0);
}
function Jw() {
  V.wrapMeta = null, V.freshSetup = !1, V.scopeId = null;
}
function F2(e = {}) {
  V.vmk?.fill(0), V.vmk = null, sn = null, Jr = null, Yl(), e.preserveLockChannel || zi(), V.freshSetup && !e.preserveFreshSetupRoot && Jw();
}
function j2() {
  return sn !== null && Date.now() >= sn;
}
function Rs() {
  return V.vmk && j2() ? (ht({ broadcast: !1, reason: "auto-lock" }), !1) : V.vmk !== null;
}
function Qw(e) {
  const t = setTimeout(() => {
    ur !== t || Jr !== e || e.epoch !== ln || !V.vmk || (ur = null, ht({ broadcast: !1, reason: "auto-lock" }));
  }, Math.max(0, e.expiresAt - Date.now()));
  ur = t;
}
function ey(e, t = Le()) {
  if (!V.vmk)
    throw new Error("vault VMK is not loaded");
  const n = t.windowSeconds * 1e3;
  return sn = Date.now() + n, Jr = { epoch: ln, expiresAt: sn, pendingCommits: 0 }, Yl(), Qw(Jr), qw(e), sn;
}
function V2() {
  const e = Jr;
  if (!V.vmk || !e) return () => {
  };
  e.pendingCommits += 1, Yl();
  let t = !1;
  return () => {
    if (!t && (t = !0, e.pendingCommits -= 1, !(Jr !== e || e.epoch !== ln || !V.vmk || e.pendingCommits !== 0 || ur !== null))) {
      if (e.expiresAt <= Date.now()) {
        ht({ broadcast: !1, reason: "auto-lock" });
        return;
      }
      Qw(e);
    }
  };
}
function q2(e) {
  if (e instanceof S && e.retryable) return !0;
  const t = e instanceof Error ? e.message : String(e);
  return t === "operation-cancelled" || t === "operation-superseded" || t === "passkey-cancelled" || t === "vault-operation-aborted";
}
function W2(e) {
  if (e.signal.aborted || e.epoch !== ln || !V.vmk)
    throw Lt();
}
async function wa(e, t = {}) {
  if (!Rs() || !V.vmk || !V.wrapMeta)
    throw new Error("vault-locked");
  const n = V.wrapMeta, r = t.expectedWrapMeta === void 0 ? void 0 : tt(t.expectedWrapMeta);
  if (r !== void 0 && !rn(tt(n), r)) throw Lt();
  const i = new Uint8Array(V.vmk), o = ln, s = Nr, a = new AbortController();
  Cs.add(a), r !== void 0 && nt.set(a, { root: r, surviveAutoLock: !1 });
  const c = {
    epoch: o,
    signal: a.signal,
    assertCurrent: () => W2(c)
  };
  let l = null;
  try {
    c.assertCurrent();
    const u = await e(i, n, c);
    return c.assertCurrent(), t.renewOnSuccess !== !1 && ey("renew"), c.assertCurrent(), t.beforeSuccess && (l = V2(), await t.beforeSuccess(), c.assertCurrent(), l(), l = null), u;
  } catch (u) {
    throw q2(u) || (r !== void 0 ? !a.signal.aborted && s === Nr && Xw(o, r, { broadcast: !0, reason: "manual-lock" }) : ht({ broadcast: !0, reason: "manual-lock" })), u;
  } finally {
    l?.(), Cs.delete(a), nt.delete(a), i.fill(0);
  }
}
function Rc() {
  return V.vmk !== null && V.freshSetup;
}
function qu() {
  return V.freshSetup && V.wrapMeta !== null;
}
function ty(e) {
  Zw(), V.vmk?.fill(0), V.vmk = e.vmk, V.wrapMeta = e.wrapMeta, V.freshSetup = e.freshSetup, V.scopeId = e.scopeId, Xl(e.scopeId);
  const t = { state: "unlocked", expiresAt: ey("unlock", e.policy) };
  if (e.onCommitted) {
    const n = pa(e.wrapMeta);
    try {
      e.onCommitted(n);
    } catch (r) {
      throw n.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), n.release(), r;
    }
  }
  return t;
}
function ht(e = {}) {
  e.broadcast && K2();
  const t = e.reason === "auto-lock", n = t && V.freshSetup && [...nt.values()].some((r) => r.surviveAutoLock);
  return Zw({ preserveAutoLockOperations: t }), F2({
    preserveFreshSetupRoot: n,
    preserveLockChannel: t && nt.size > 0
  }), qw(e.reason ?? "manual-lock"), { state: "locked" };
}
function ft(e) {
  Rs();
  const t = e ? Cw(e) : void 0;
  if (t && Yw(t), V.vmk)
    return {
      state: "unlocked",
      expiresAt: sn ?? void 0,
      ...V.freshSetup ? { freshSetup: !0 } : {}
    };
  if (t) {
    const n = V.wrapMeta !== null && rn(tt(V.wrapMeta), tt(t));
    V.freshSetup && !n && (V.freshSetup = !1), V.wrapMeta = t, V.scopeId = Gw(t);
  }
  return V.wrapMeta ? { state: "locked" } : { state: "needs-setup" };
}
function Jl(e) {
  const t = Cw(e), n = Gw(t), r = V.wrapMeta !== null && rn(tt(V.wrapMeta), tt(t));
  return Yw(t), V.freshSetup && !r && (V.freshSetup = !1), V.vmk && !r && ht({ broadcast: !1, reason: "manual-lock" }), V.wrapMeta = t, V.scopeId = n, { wrapMeta: t, scopeId: n };
}
function G2() {
  ht({ broadcast: !1, reason: "unload" }), zi();
}
const Po = "Avibe Vault", Wu = "avibe-vault";
function hr(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Dn(e) {
  const t = new ArrayBuffer(e.byteLength);
  return new Uint8Array(t).set(e), t;
}
function ny() {
  return Dn(crypto.getRandomValues(new Uint8Array(32)));
}
function ry(e) {
  try {
    return ve(e);
  } catch {
    return null;
  }
}
function ya(e, t) {
  if (e instanceof ArrayBuffer || ArrayBuffer.isView(e))
    return Dn(Pw(e));
  if (typeof e == "string") {
    const n = ry(e);
    return Dn(n ?? new TextEncoder().encode(e));
  }
  if (Array.isArray(e))
    return Dn(Uint8Array.from(e));
  throw new Error("webauthn bytes are missing");
}
function Wa(e) {
  if (typeof e != "object" || e === null || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null)
    throw new Error("invalid webauthn creation options object");
  return e;
}
function Z2(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 88 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(e) || /[+/]/.test(e) && /[-_]/.test(e))
    throw new Error("invalid webauthn user.id encoding");
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = ry(t);
  if (!n || n.byteLength === 0 || n.byteLength > 64)
    throw new Error("webauthn user.id must be 1-64 bytes");
  const r = ce(n);
  if (t !== r && t !== r.replace(/=+$/, ""))
    throw new Error("invalid webauthn user.id encoding");
  return Dn(n);
}
function Y2(e) {
  const t = ya(e);
  if (t.byteLength === 0 || t.byteLength > 64)
    throw new Error("vault user handle must be 1-64 bytes");
  return t;
}
function Ls(e) {
  if (Array.isArray(e))
    return e.map((t) => {
      const n = hr(t);
      if (!n || n.type !== "public-key")
        throw new Error("invalid WebAuthn credential descriptor");
      return {
        type: "public-key",
        id: ya(n.id),
        ...Array.isArray(n.transports) ? { transports: n.transports } : {}
      };
    });
}
function Gu(e) {
  return !Array.isArray(e) || e.length === 0 ? [
    { type: "public-key", alg: -7 },
    { type: "public-key", alg: -257 }
  ] : e.map((t) => {
    const n = hr(t);
    if (!n || n.type !== "public-key" || typeof n.alg != "number")
      throw new Error("invalid WebAuthn pubKeyCredParams");
    return { type: "public-key", alg: n.alg };
  });
}
function Zu(e) {
  return {
    ...hr(e) ?? {},
    residentKey: "required",
    userVerification: "required"
  };
}
function Yu(e) {
  if (e === "none" || e === "indirect" || e === "direct" || e === "enterprise") return e;
}
function X2(e) {
  const t = hr(e) ?? {};
  return { ...t, prf: hr(t.prf) ?? {} };
}
function Ft() {
  return window.location.hostname;
}
function J2(e) {
  const t = e.authzCreationOptions === void 0 ? void 0 : Wa(e.authzCreationOptions), n = t && Object.prototype.hasOwnProperty.call(t, "webauthn") ? Wa(t.webauthn) : t, r = typeof t?.challenge_id == "string" ? t.challenge_id : void 0, i = hr(n?.rp), o = typeof i?.id == "string" ? i.id : void 0;
  if (o && o !== e.rpId)
    throw new Error("webauthn rp_id does not match sandbox host");
  if (!n)
    return {
      options: {
        rp: { name: Po, id: e.rpId },
        user: {
          id: Y2(e.vaultUserHandle),
          name: e.vaultUserHandle || Wu,
          displayName: e.displayName || Po
        },
        challenge: ny(),
        pubKeyCredParams: Gu(void 0),
        authenticatorSelection: Zu(void 0),
        extensions: { prf: {} }
      }
    };
  const s = Wa(n.user);
  return {
    options: {
      rp: {
        name: typeof i?.name == "string" ? i.name : Po,
        id: e.rpId
      },
      user: {
        id: Z2(s.id),
        name: typeof s.name == "string" ? s.name : e.vaultUserHandle || Wu,
        displayName: typeof s.displayName == "string" ? s.displayName : e.displayName || Po
      },
      challenge: ya(n.challenge),
      pubKeyCredParams: Gu(n.pubKeyCredParams),
      authenticatorSelection: Zu(n.authenticatorSelection),
      extensions: X2(n.extensions),
      ...Ls(n.excludeCredentials) ? { excludeCredentials: Ls(n.excludeCredentials) } : {},
      ...typeof n.timeout == "number" ? { timeout: n.timeout } : {},
      ...Yu(n.attestation) ? { attestation: Yu(n.attestation) } : {}
    },
    ...r ? { challengeId: r } : {}
  };
}
function Q2(e) {
  if (e.length === 0) throw new Error("passkey-not-configured");
  if (e.length > 1) throw new Error("passkey-multiple-not-supported");
  return e[0];
}
function ek(e, t) {
  const n = Q2(e), r = {
    challenge: ny(),
    rpId: t,
    userVerification: "required",
    extensions: Qx(n.prfSalt)
  };
  return n.credentialId && (r.allowCredentials = [
    {
      type: "public-key",
      id: Dn(ve(n.credentialId))
    }
  ]), r;
}
function tk(e, t, n) {
  const r = {
    challenge: Dn(n),
    rpId: t,
    userVerification: "required"
  }, i = e.filter((o) => o.credentialId).map((o) => ({ type: "public-key", id: Dn(ve(o.credentialId)) }));
  return i.length > 0 && (r.allowCredentials = i), r;
}
function nk(e, t = Ft()) {
  const n = hr(e);
  if (!n) throw new Error("webauthn assertion options are missing");
  const r = typeof n.rpId == "string" ? n.rpId : typeof n.rp_id == "string" ? n.rp_id : void 0;
  if (r && r !== t)
    throw new Error("webauthn rpId does not match sandbox host");
  return {
    challenge: ya(n.challenge),
    rpId: t,
    userVerification: "required",
    ...Ls(n.allowCredentials) ? { allowCredentials: Ls(n.allowCredentials) } : {},
    ...typeof n.timeout == "number" ? { timeout: n.timeout } : {}
  };
}
function iy(e, t) {
  return {
    publicKey: e,
    ...t ? { signal: t } : {}
  };
}
function oy(e) {
  return {
    id: e.id,
    rawId: ce(e.rawId),
    type: e.type
  };
}
function rk(e) {
  const t = e.response, n = typeof t.getTransports == "function" ? t.getTransports() : [];
  return {
    ...oy(e),
    response: {
      clientDataJSON: ce(t.clientDataJSON),
      attestationObject: ce(t.attestationObject),
      transports: n
    }
  };
}
function sy(e) {
  const t = e.response;
  return {
    ...oy(e),
    response: {
      clientDataJSON: ce(t.clientDataJSON),
      authenticatorData: ce(t.authenticatorData),
      signature: ce(t.signature),
      userHandle: t.userHandle ? ce(t.userHandle) : null
    }
  };
}
function ik(e) {
  const n = e?.getClientExtensionResults()?.prf?.results?.first;
  if (!n) throw new Error("passkey-prf-unavailable");
  const r = Pw(n, "passkey PRF output");
  if (r.byteLength === 0) throw new Error("passkey-prf-unavailable");
  return r;
}
async function ga(e, t, n) {
  const r = await navigator.credentials.get(
    iy(ek(e, t), n)
  );
  if (!r) throw new Error("passkey-cancelled");
  const i = ik(r), o = ce(r.rawId), s = e.find((a) => a.credentialId === o);
  return { prfOutput: i, prfSalt: s?.prfSalt ?? e[0].prfSalt, credentialId: o };
}
async function ok(e, t, n, r) {
  if (!await navigator.credentials.get(
    iy(tk(e, t, n), r)
  )) throw new Error("passkey-cancelled");
}
async function sk(e) {
  const t = await navigator.credentials.get({
    publicKey: nk(e)
  });
  if (!t) throw new Error("passkey-cancelled");
  return sy(t);
}
const ak = /ancestor|not the same as its|cross-?origin|different origin/i;
function ay(e) {
  return typeof e?.name == "string" ? e.name : "";
}
function ck(e) {
  return e instanceof Error || typeof e?.message == "string" ? e.message : typeof e == "string" ? e : "";
}
function cy(e) {
  const t = ay(e);
  return t === "SecurityError" || t === "NotSupportedError" || ak.test(ck(e));
}
function ly(e) {
  return ay(e) === "NotAllowedError" && !cy(e);
}
function Ql(e) {
  const { options: t, challengeId: n } = J2(e);
  return navigator.credentials.create({
    publicKey: t
  }).then((i) => {
    if (!i) throw new Error("passkey-cancelled");
    const o = ce(i.rawId), s = rk(i), a = e.authzCreationOptions !== void 0;
    return {
      credentialId: o,
      ...a ? { authzRegistration: n ? { challenge_id: n, credential: s } : s } : {}
    };
  });
}
const Lc = {
  en: {
    "common.cancel": "Cancel",
    "common.continue": "Continue",
    "common.done": "Done",
    "sandbox.intro": "Your protected vault keys are created and used here, isolated from the rest of Avibe. This runs entirely in your browser — Avibe's servers only serve this code, never your keys.",
    "authorize.waiting": "Preparing your approval…",
    "authorize.waitingLong": "Still waiting for Avibe. If nothing appears, close this window and approve again in Avibe.",
    "authorize.continuingTitle": "One moment",
    "authorize.continuing": "Checking the request. Keep this window open.",
    "authorize.continueInAvibe": "Continue in Avibe.",
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
    "plaintext.title": "View secret",
    "plaintext.valueLabel": "Value",
    "plaintext.show": "Show",
    "plaintext.hide": "Hide",
    "plaintext.copied": "Copied",
    "plaintext.copyRow": "Copy {{label}}",
    "plaintext.copiedRow": "Copied {{label}}",
    "plaintext.showRow": "Show {{label}}",
    "plaintext.hideRow": "Hide {{label}}",
    "plaintext.hiddenValue": "Hidden",
    "plaintext.copyReadyRow": "Passkey confirmed for {{label}}. Click its Copy again to write it to the clipboard.",
    "plaintext.body": "Values are shown only in this secure window.",
    "plaintext.copy": "Copy",
    "plaintext.copyReady": "Passkey confirmed. Click Copy again to write to clipboard.",
    "plaintext.copyFailed": "Couldn't copy. Try again.",
    "reveal.showConfirmTitle": "View secret",
    "reveal.showSecretTitle": "View secret",
    "reveal.confirmSecretBody": "Continue to show this secret's values in this window. The plaintext will not be returned to Avibe.",
    "reveal.openWindowTitle": "Approved",
    "reveal.openWindowBody": "The values open in the secure window.",
    "reveal.openWindowBodyOne": "The value opens in the secure window.",
    "reveal.openWindow": "Open window",
    "reveal.confirmBody": "Continue to show the value in this window. The plaintext will not be returned to Avibe.",
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
    "hd.passphraseConfirmPending": "Enter the passphrase again to continue.",
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
    "hd.unlockBody": "Unlock with your passkey. After checking the account and address, click Confirm sign to complete this signature.",
    "hd.unlockBodyStrict": "First unlock with your passkey. Then review the actual account and derivation path and use your passkey again to approve this signature.",
    "sign.unlockBody": "Unlock with your passkey. After checking this signature, click Confirm sign.",
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
    "authorize.continuingTitle": "请稍候",
    "authorize.continuing": "正在核对请求，请保持这个窗口打开。",
    "authorize.continueInAvibe": "请回到 Avibe 继续。",
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
    "plaintext.title": "查看密钥",
    "plaintext.valueLabel": "值",
    "plaintext.show": "显示",
    "plaintext.hide": "隐藏",
    "plaintext.copied": "已复制",
    "plaintext.copyRow": "复制 {{label}}",
    "plaintext.copiedRow": "已复制 {{label}}",
    "plaintext.showRow": "显示 {{label}}",
    "plaintext.hideRow": "隐藏 {{label}}",
    "plaintext.hiddenValue": "已隐藏",
    "plaintext.copyReadyRow": "{{label}} 的 Passkey 已确认。再次点击它的「复制」写入剪贴板。",
    "plaintext.body": "值只在这个安全窗口里显示。",
    "plaintext.copy": "复制",
    "plaintext.copyReady": "passkey 已确认。再次点击「复制」写入剪贴板。",
    "plaintext.copyFailed": "没有复制成功，请再试一次。",
    "reveal.showConfirmTitle": "查看密钥",
    "reveal.showSecretTitle": "查看密钥",
    "reveal.confirmSecretBody": "继续后，这个密钥的值会在这个窗口里显示，不会交给 Avibe。",
    "reveal.openWindowTitle": "已批准",
    "reveal.openWindowBody": "值会在安全窗口里显示。",
    "reveal.openWindowBodyOne": "值会在安全窗口里显示。",
    "reveal.openWindow": "打开窗口",
    "reveal.confirmBody": "继续后，明文会在这个窗口里显示，不会交给 Avibe。",
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
    "hd.passphraseConfirmPending": "请再输入一次 Passphrase，再继续。",
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
    "hd.unlockBody": "先用 Passkey 解锁，核对账户和地址后，点「确认签名」完成这次签名。",
    "hd.unlockBodyStrict": "先使用 Passkey 解锁，再核对实际账户和派生路径，并再次使用 Passkey 批准本次签名。",
    "sign.unlockBody": "先用 Passkey 解锁，核对这次签名后，点「确认签名」。",
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
let Hs = "en";
function lk(e) {
  return e === "zh" ? "zh" : "en";
}
function dk(e) {
  const t = lk(e);
  return t === Hs ? !1 : (Hs = t, !0);
}
function uk() {
  return Hs;
}
function fk(e) {
  return typeof e == "string" && Object.hasOwn(Lc.en, e);
}
function Y(e, t = {}) {
  const n = Lc[Hs], r = Lc.en;
  return (n[e] ?? r[e] ?? e).replace(/\{\{(\w+)\}\}/g, (o, s) => String(t[s] ?? ""));
}
function hk(e) {
  return JSON.stringify(e ?? {});
}
function Xu(e) {
  if (!e) return {};
  try {
    const t = JSON.parse(e);
    return typeof t == "object" && t !== null ? t : {};
  } catch {
    return {};
  }
}
function be(e, t, n) {
  e.dataset.i18nKey = t, e.dataset.i18nParams = hk(n), e.textContent = Y(t, n);
}
function ze(e, t) {
  delete e.dataset.i18nKey, delete e.dataset.i18nParams, e.textContent = t;
}
function pk(e = document) {
  e.querySelectorAll("[data-i18n-key]").forEach((t) => {
    const n = t.dataset.i18nKey;
    n && (t.textContent = Y(n, Xu(t.dataset.i18nParams)));
  }), e.querySelectorAll("[data-i18n-placeholder-key]").forEach((t) => {
    const n = t.dataset.i18nPlaceholderKey;
    n && (t.placeholder = Y(n, Xu(t.dataset.i18nPlaceholderParams)));
  });
}
function dy(e) {
  return {
    title: "policyDowngrade.title",
    subtitle: { text: e.target.name },
    confirmLabel: "policyDowngrade.confirm",
    policyDowngrade: e
  };
}
function Ht(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
function st(e, t, n = "mono") {
  const r = Ht("div", "review-field"), i = Ht("span", "review-label");
  be(i, e);
  const o = Ht("div", "review-value"), s = Ht("span", n === "word" ? "review-word" : "review-mono");
  return typeof t == "string" ? ze(s, t) : be(s, t.key, t.params), o.append(s), r.append(i, o), r;
}
function $o(e, t) {
  const n = Ht("p", t);
  return be(n, e), n;
}
function wk(e, t) {
  ze(e, "");
  const { target: n } = t, r = Ht("div", "review policy-downgrade-review");
  r.append(n.protection === "standard" ? $o("policyDowngrade.standardWarning", "review-warning") : $o("policyDowngrade.protectedNotice", "review-info"));
  const i = Ht("div", "review-summary");
  i.append(
    st("policyDowngrade.setting", { key: "policyDowngrade.settingValue" }, "word"),
    st("policyDowngrade.key", n.name),
    st("policyDowngrade.tier", { key: n.protection === "standard" ? "policyDowngrade.standard" : "policyDowngrade.protected" }, "word")
  ), n.account !== null && i.append(
    st("policyDowngrade.account", n.accountName !== null ? { key: "policyDowngrade.accountNamed", params: { index: n.account, name: n.accountName } } : { key: "policyDowngrade.accountUnnamed", params: { index: n.account } }, "word"),
    st("policyDowngrade.appliesTo", { key: "policyDowngrade.appliesToValue" }, "word")
  ), r.append(i, $o("policyDowngrade.onlyPolicy", "confirmation-note"));
  const o = Ht("details", "review-details"), s = Ht("summary", "review-details-toggle");
  be(s, "policyDowngrade.details");
  const a = Ht("div", "review-details-body"), c = [
    ["eth", "policyDowngrade.eth"],
    ["btc_legacy", "policyDowngrade.btcLegacy"],
    ["btc_segwit", "policyDowngrade.btcSegwit"],
    ["btc_taproot", "policyDowngrade.btcTaproot"],
    ["solana", "policyDowngrade.solana"]
  ];
  a.append($o("policyDowngrade.serverFacts", "confirmation-note"));
  for (const [l, u] of c) {
    const d = n.addresses[l];
    d !== null ? a.append(st(u, d)) : n.signerKind === "hd-seed" && a.append(st(u, { key: "policyDowngrade.notUsed" }));
  }
  a.append(
    st("policyDowngrade.recordId", n.secretId),
    st("policyDowngrade.identity", n.identitySha256),
    st("policyDowngrade.revision", t.change.revision),
    st("policyDowngrade.expires", t.expiresAt),
    st("policyDowngrade.challengeId", t.challengeId)
  ), o.append(s, a), r.append(o), e.append(r);
}
const uy = {
  sol: { symbol: "SOL", baseUnit: "lamports", decimals: 9 },
  eth: { symbol: "ETH", baseUnit: "wei", decimals: 18 },
  btc: { symbol: "BTC", baseUnit: "sats", decimals: 8 }
}, ed = /^(?:0|[1-9][0-9]*)$/;
function fy(e, t) {
  if (t === 0) return e;
  const n = e.padStart(t + 1, "0"), r = n.slice(-t).replace(/0+$/, "");
  return `${n.slice(0, -t)}${r ? `.${r}` : ""}`;
}
function Ju(e, t, n) {
  const { symbol: r, baseUnit: i, decimals: o } = uy[t];
  if (!ed.test(e) || n === "base") return `${e} ${i}`;
  const s = `${fy(e, o)} ${r}`;
  return n === "symbol" ? s : `${s} (${e} ${i})`;
}
const yk = 253402300799n;
function gk(e) {
  if (!ed.test(e) || e.length > 12 || BigInt(e) > yk) return null;
  const t = new Date(Number(e) * 1e3).toISOString(), n = t.slice(17, 19);
  return `${t.slice(0, 10)} ${t.slice(11, 16)}${n === "00" ? "" : `:${n}`} UTC`;
}
const Ns = 8, Us = 6, Qu = 28;
function ef(e, t) {
  if (!t || e.length <= Ns + Us + 2) {
    const r = ie("span", "review-mono");
    return ze(r, e), r;
  }
  const n = ie("button", "review-mono review-elided");
  return n.type = "button", n.setAttribute("aria-expanded", "false"), n.title = e, ze(n, `${e.slice(0, Ns)}…${e.slice(-Us)}`), n.addEventListener("click", () => {
    n.getAttribute("aria-expanded") !== "true" && (n.setAttribute("aria-expanded", "true"), ze(n, e));
  }), n;
}
function ie(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
const mk = "http://www.w3.org/2000/svg";
function Oo(e, t) {
  const n = document.createElementNS(mk, e);
  for (const [r, i] of Object.entries(t)) n.setAttribute(r, i);
  return n;
}
function tf() {
  const e = Oo("svg", {
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
    Oo("path", { d: "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" }),
    Oo("path", { d: "M12 9v4.5" }),
    Oo("path", { d: "M12 17h.01" })
  ), e;
}
function bk(e) {
  if (!e.notes?.length) return null;
  const t = ie("span", "review-notes");
  for (const n of e.notes) {
    const r = ie("span", "review-note");
    typeof n == "string" ? be(r, n) : be(r, n.key, { count: n.count }), t.append(r);
  }
  return t;
}
function Ga(e, t = "review-label") {
  const n = ie(e.heading ? "p" : "span", t), r = {
    ...e.index === void 0 ? {} : { index: e.index },
    ...e.name === void 0 ? {} : { name: e.name }
  };
  return be(n, e.label, Object.keys(r).length > 0 ? r : void 0), n;
}
function vk(e, t) {
  const n = ie("button", "review-copy");
  return n.type = "button", be(n, "review.copy"), n.addEventListener("click", () => {
    const r = () => {
      window.getSelection()?.selectAllChildren(e), be(n, "review.copySelected");
    };
    let i;
    try {
      i = navigator.clipboard.writeText(t);
    } catch {
      r();
      return;
    }
    i.then(() => be(n, "review.copied"), r);
  }), n;
}
function Co(e, t = !1) {
  if (e.heading) return Ga(e, "review-subheading");
  const n = ie("div", e.raw ? "review-field review-field-raw" : "review-field"), r = bk(e);
  if (e.raw) {
    const c = ie("div", "review-raw-head"), l = ie("pre", "review-raw");
    return ze(l, e.value), c.append(Ga(e)), r && c.append(r), c.append(vk(l, e.value)), n.append(c, l), n;
  }
  const i = ie("div", "review-value"), o = e.decimals !== void 0 && ed.test(e.value) ? fy(e.value, e.decimals) : null, s = e.time ? gk(e.value) : null, a = t && e.shownAs ? 0 : e.native ? Ju(e.value, e.native, e.unit).length : o !== null || e.time ? Qu + 1 : t && e.elide && e.value.length > Ns + Us + 2 ? Ns + Us + 1 : e.value.length;
  if (t && e.to === void 0 && a + (e.secondary?.length ?? 0) <= Qu && n.classList.add("review-field-inline"), t && e.shownAs) {
    const c = ie("span", "review-word");
    be(c, e.shownAs), i.append(c);
  } else if (e.native) {
    const c = ie("span", "review-mono");
    ze(c, Ju(e.value, e.native, e.unit)), i.append(c);
  } else if (o !== null) {
    const c = ie("span", "review-mono");
    ze(c, o);
    const l = ie("span", "review-exact");
    be(l, "review.amount.baseUnitsDecimals", { value: e.value, count: e.decimals }), i.append(c, l);
  } else if (e.time) {
    const c = ie("span", "review-mono");
    ze(c, s ?? e.value);
    const l = ie("span", "review-exact");
    s === null ? be(l, "review.time.noDate") : be(l, "review.time.raw", { value: e.value }), i.append(c, l);
  } else
    i.append(ef(e.value, t && e.elide === !0));
  if (e.secondary !== void 0) {
    const c = ie("span", "review-mono review-secondary");
    ze(c, e.secondary), i.append(c);
  }
  if (e.to !== void 0) {
    const c = ie("span", "review-arrow");
    ze(c, "→"), i.append(c, ef(e.to, t));
  }
  return r && i.append(r), n.append(Ga(e), i), n;
}
function nf(e, t, n) {
  const r = ie("section", "review-group"), i = ie("h2", "review-group-title");
  return be(i, e, t === void 0 ? void 0 : { index: t }), r.append(i, ...n), r;
}
function xk(e, t, n = {}) {
  ze(e, "");
  const r = ie("div", "review"), i = ie("div", "review-heading"), o = ie("p", "review-kind");
  if (be(o, t.kind), i.append(o), n.session) {
    const u = ie("p", "review-session"), d = ie("span", "review-session-label");
    be(d, "context.session");
    const f = ie("span", "review-session-value");
    ze(f, n.session), u.append(d, f), i.append(u);
  }
  r.append(i);
  for (const u of t.warnings ?? []) {
    const d = ie("p", "review-warning"), f = ie("span", "review-warning-text");
    be(f, u), d.append(tf(), f), r.append(d);
  }
  if (t.undecoded) {
    const u = ie("p", "review-notice"), d = ie("span", "review-notice-text");
    be(d, "sign.blindNotice"), u.append(tf(), d), r.append(u);
  }
  const s = ie("div", "review-summary");
  s.append(...t.summary.map((u) => Co(u, !0)), Co(t.signer, !0)), r.append(s);
  const a = ie("details", "review-details"), c = ie("summary", "review-details-toggle");
  be(c, "review.details");
  const l = ie("div", "review-details-body");
  t.details.forEach((u, d) => {
    const f = u.fields.map((h) => Co(h));
    l.append(nf(u.title, u.index, d === 0 ? [Co(t.signer), ...f] : f));
  }), n.rows && l.append(nf("review.group.request", void 0, [n.rows])), a.append(c, l), r.append(a), e.append(r);
}
function Te(e) {
  throw new Error(`signing review is malformed: ${e}`);
}
function Ds(e, t, n) {
  (typeof e != "object" || e === null || Array.isArray(e)) && Te(`${n} must be an object`);
  const r = e;
  for (const i of Object.keys(r)) t.includes(i) || Te(`${n}.${i} is not a review field`);
  return r;
}
function sr(e, t, n) {
  return (!fk(e) || !e.startsWith(`${t}.`)) && Te(`${n} must be a ${t} translation key`), e;
}
function Hc(e, t) {
  if (e !== void 0)
    return (typeof e != "number" || !Number.isSafeInteger(e) || e < 0) && Te(`${t} must be a non-negative integer`), e;
}
function Si(e, t) {
  return Array.isArray(e) || Te(`${t} must be a list`), e;
}
const kk = 4096, Ek = /^[A-Za-z_][A-Za-z0-9_]{0,63} \([a-z][a-z0-9[\]]{0,31}\)$/;
function Za(e, t) {
  const n = Ds(e, ["label", "index", "name", "value", "native", "unit", "decimals", "time", "to", "secondary", "elide", "shownAs", "notes", "raw", "heading"], t), r = Hc(n.index, `${t}.index`);
  n.name !== void 0 && (typeof n.name != "string" || (n.label === "review.field.typedField" ? n.name.length > kk : !Ek.test(n.name))) && Te(`${t}.name is not a valid field name`), typeof n.value != "string" && Te(`${t}.value must be a string`), n.native !== void 0 && !Object.hasOwn(uy, n.native) && Te(`${t}.native is not a native currency`), n.unit !== void 0 && (n.native === void 0 || n.unit !== "symbol" && n.unit !== "base") && Te(`${t}.unit is not a unit of its currency`), n.decimals !== void 0 && (typeof n.decimals != "number" || !Number.isInteger(n.decimals) || n.decimals < 0 || n.decimals > 255 || n.native !== void 0) && Te(`${t}.decimals must be an encoded decimal count`), n.time !== void 0 && (n.time !== "unix-seconds" || n.native !== void 0 || n.decimals !== void 0) && Te(`${t}.time is not a time form`), n.to !== void 0 && typeof n.to != "string" && Te(`${t}.to must be a string`), n.secondary !== void 0 && typeof n.secondary != "string" && Te(`${t}.secondary must be a string`), n.elide !== void 0 && typeof n.elide != "boolean" && Te(`${t}.elide must be a boolean`);
  const i = n.shownAs === void 0 ? void 0 : sr(n.shownAs, "review.value", `${t}.shownAs`);
  n.raw !== void 0 && typeof n.raw != "boolean" && Te(`${t}.raw must be a boolean`), n.heading !== void 0 && typeof n.heading != "boolean" && Te(`${t}.heading must be a boolean`), n.heading && (n.value !== "" || n.native !== void 0 || n.decimals !== void 0 || n.time !== void 0 || n.notes !== void 0 || n.raw !== void 0 || n.to !== void 0 || n.secondary !== void 0 || n.elide !== void 0 || i !== void 0) && Te(`${t} is a heading and cannot carry a value`);
  const o = n.notes === void 0 ? void 0 : Si(n.notes, `${t}.notes`).map((s, a) => {
    const c = `${t}.notes[${a}]`;
    if (typeof s == "string") return sr(s, "review.note", c);
    const l = Ds(s, ["key", "count"], c), u = Hc(l.count, `${c}.count`);
    return u === void 0 && Te(`${c}.count is required`), { key: sr(l.key, "review.note", `${c}.key`), count: u };
  });
  return {
    label: sr(n.label, "review.field", `${t}.label`),
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
function hy(e) {
  const t = Ds(e, ["kind", "signer", "summary", "undecoded", "warnings", "details"], "review");
  typeof t.undecoded != "boolean" && Te("review.undecoded must be a boolean");
  const n = t.warnings === void 0 ? void 0 : Si(t.warnings, "review.warnings").map((r, i) => sr(r, "review.warning", `review.warnings[${i}]`));
  return {
    kind: sr(t.kind, "review.kind", "review.kind"),
    signer: Za(t.signer, "review.signer"),
    summary: Si(t.summary, "review.summary").map((r, i) => Za(r, `review.summary[${i}]`)),
    undecoded: t.undecoded,
    ...n === void 0 ? {} : { warnings: n },
    details: Si(t.details, "review.details").map((r, i) => {
      const o = `review.details[${i}]`, s = Ds(r, ["title", "index", "fields"], o), a = Hc(s.index, `${o}.index`);
      return {
        title: sr(s.title, "review.group", `${o}.title`),
        ...a === void 0 ? {} : { index: a },
        fields: Si(s.fields, `${o}.fields`).map((c, l) => Za(c, `${o}.fields[${l}]`))
      };
    })
  };
}
let td = null, nd = 0;
function Ak(e) {
  td = e;
}
function py() {
  return Date.now() < nd;
}
function Sk() {
  nd = Date.now() + 150, td?.("ui.show");
}
function _k() {
  nd = 0, td?.("ui.hide");
}
function Ik() {
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
function wy(e) {
  e.querySelectorAll(".dynamic").forEach((t) => t.remove());
}
function me(e) {
  return { text: e };
}
function Oe(e, t) {
  return { key: e, params: t };
}
function J(e, t) {
  if (typeof t == "string") {
    be(e, t);
    return;
  }
  if ("text" in t) {
    ze(e, t.text);
    return;
  }
  be(e, t.key, t.params);
}
function eo(e, t, n) {
  const r = Ik();
  return document.body.classList.add("interactive"), document.body.classList.remove("confirming"), Sk(), r.card.classList.remove("confirm-card"), J(r.title, e), J(r.subtitle, t), n ? J(r.body, n) : ze(r.body, ""), wy(r.card), r;
}
function ma() {
  document.body.classList.remove("interactive"), document.body.classList.remove("confirming");
  const e = document.getElementById("page")?.querySelector(".card");
  e && (e.classList.remove("confirm-card"), wy(e)), _k();
}
function te(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic"), (e.querySelector(".card-scroll") ?? e).append(t);
}
function rf(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic");
  const n = e.querySelector(".card-footer");
  if (!n) throw new Error("sandbox confirmation footer is unavailable");
  n.append(t);
}
function Bk(e) {
  const t = document.createElement("div");
  if (t.className = "detail-value", e.variant === "chips") {
    t.classList.add("detail-chips");
    for (const r of e.values) {
      const i = document.createElement("span");
      i.className = `detail-chip${e.tone === "warning" ? " detail-chip-warning" : ""}`, J(i, r), t.append(i);
    }
    return t;
  }
  const n = document.createElement(e.variant === "command" ? "code" : "span");
  return n.className = e.variant === "command" ? "detail-command" : e.variant === "mono" ? "detail-mono" : "detail-text", J(n, e.value), e.variant === "mono" && (n.title = n.textContent ?? ""), t.append(n), t;
}
function yy(e) {
  const t = document.createElement("div");
  t.className = "confirmation-details";
  for (const n of e.rows) {
    const r = document.createElement("div");
    r.className = "detail-row";
    const i = document.createElement("span");
    i.className = "detail-label", J(i, n.label), r.append(i, Bk(n)), t.append(r);
  }
  return t;
}
function Tk(e, t, n) {
  if (ze(e, ""), e.append(yy(t)), n) {
    const r = document.createElement("p");
    r.className = "confirmation-note", J(r, n), e.append(r);
  }
}
function mt(e) {
  const t = document.createElement("button");
  return t.type = "button", t.className = "action dynamic", J(t, e), t;
}
function zs(e = me("")) {
  const t = document.createElement("p");
  return t.className = "setup-status dynamic", J(t, e), t;
}
let Ro = null;
function gy() {
  return new Error("operation-superseded");
}
function Pk(e) {
  const t = e.reason;
  return t instanceof Error ? t : gy();
}
function dn(e) {
  Ro?.abort(gy());
  const t = new AbortController();
  return Ro = t, Promise.resolve().then(() => e(t.signal)).finally(() => {
    Ro === t && (Ro = null);
  });
}
function Qr(e, t, n, r) {
  const i = eo(e.title, e.subtitle, e.review ? void 0 : e.body);
  document.body.classList.add("confirming"), i.card.classList.add("confirm-card"), e.policyDowngrade ? wk(i.body, e.policyDowngrade) : e.review ? xk(i.body, e.review, {
    session: e.reviewSession,
    rows: e.details && yy(e.details)
  }) : e.details && Tk(i.body, e.details, e.body);
  const o = mt(e.confirmLabel), s = mt("common.cancel");
  return s.classList.add("secondary"), rf(i.card, s), rf(i.card, o), o.disabled = !0, new Promise((a, c) => {
    let l = !1, u;
    const d = setTimeout(() => {
      try {
        u = n?.(i.card);
      } catch (y) {
        h(() => c(y));
        return;
      }
      (u?.ready ?? Promise.resolve()).then(
        () => {
          !l && !t.aborted && (o.disabled = !1);
        },
        (y) => h(() => c(y))
      );
    }, 500), f = () => {
      h(() => c(Pk(t)));
    }, h = (p) => {
      l || (l = !0, clearTimeout(d), u?.dispose(), t.removeEventListener("abort", f), ma(), p());
    };
    if (t.aborted) {
      f();
      return;
    }
    t.addEventListener("abort", f, { once: !0 }), o.addEventListener("click", () => {
      if (o.disabled) return;
      o.disabled = !0, s.disabled = !0;
      let p;
      try {
        u?.assertCurrent(), p = r ? r() : Promise.resolve();
      } catch (y) {
        h(() => c(y));
        return;
      }
      p.then(
        () => h(a),
        (y) => h(() => c(y))
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
function Ms(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function $k(e) {
  return new Promise((t, n) => {
    const r = () => n(Ms(e));
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function of(e) {
  if (e?.aborted) throw Ms(e);
}
async function my(e) {
  const t = Jl(e.wrapMeta), n = ua(t.wrapMeta);
  let r;
  try {
    const i = await Promise.race([
      ga(n, e.currentRpId, e.abortSignal).then(
        (o) => {
          if (e.abortSignal?.aborted)
            throw o.prfOutput.fill(0), Ms(e.abortSignal);
          return o;
        },
        (o) => {
          throw e.abortSignal?.aborted ? Ms(e.abortSignal) : o;
        }
      ),
      ...e.abortSignal ? [$k(e.abortSignal)] : []
    ]);
    return r = i.prfOutput, await by({
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
async function by(e) {
  const t = Jl(e.wrapMeta);
  let n;
  try {
    of(e.abortSignal), n = await Ow(t.wrapMeta, {
      kind: "passkey",
      prfOutput: e.assertion.prfOutput,
      prfSalt: e.assertion.prfSalt
    }), of(e.abortSignal);
    const r = ty({
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
function Ok(e) {
  if (typeof e != "object" || e === null)
    throw new S("invalid_payload", "request payload must be an object");
  return e;
}
function Ck(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new S("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function sf(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Rk(e) {
  const t = Ok(e), n = t.kind === "keypair" ? "keypair" : t.kind === "static" ? "static" : null;
  if (!n) throw new S("invalid_payload", "kind must be static or keypair");
  const r = sf(t.name, "name");
  if (t.rootMetadata !== void 0) throw new S("invalid_payload", "seal cannot set vault root metadata");
  const i = Ck(t.wrapMeta, "wrapMeta");
  if (n === "static") {
    if (t.curve !== void 0) throw new S("invalid_payload", "static seal cannot carry a curve");
    if (t.inputMode !== "parent-value")
      throw new S("invalid_payload", "static seal inputMode must be parent-value");
    return {
      name: r,
      kind: "static",
      inputMode: "parent-value",
      value: sf(t.value, "value"),
      wrapMeta: i
    };
  }
  if (t.inputMode !== void 0)
    throw new S("invalid_payload", "keypair seal is generate-only and does not accept inputMode");
  if (t.value !== void 0)
    throw new S("invalid_payload", "keypair seal cannot accept parent-provided private key material");
  let o;
  try {
    o = ci(t.curve);
  } catch {
    throw new S("invalid_payload", "unsupported signing curve");
  }
  return {
    name: r,
    kind: "keypair",
    ...t.curve !== void 0 ? { curve: o } : {},
    wrapMeta: i
  };
}
function gr(e) {
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
const Lk = 512, vy = "avibe-vault-signed-context-replay:v2", Hk = "avibe-vault-signed-context-replay:v2", Nk = "avibe-vault-signed-context-replay:v2", xt = /* @__PURE__ */ new Map();
let af = !1, _i = null;
function yt(e) {
  return typeof e == "object" && e !== null;
}
function Qt(e, t) {
  if (typeof e != "string" || e.length === 0) throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function yi(e, t) {
  if (e !== void 0) {
    if (typeof e != "string") throw new S("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ya(e, t) {
  if (e !== void 0) {
    if (!Array.isArray(e) || !e.every((n) => typeof n == "string"))
      throw new S("invalid_payload", `${t} must be a string array`);
    return e;
  }
}
function Uk(e, t) {
  if (e !== "static" && e !== "keypair") throw new S("invalid_payload", `${t} must be static or keypair`);
  return e;
}
function Dk(e) {
  if (e !== "agent-deliver" && e !== "sign" && e !== "reveal")
    throw new S("invalid_payload", "context purpose is invalid");
  return e;
}
function Nc(e, t) {
  if (typeof e != "number" || !Number.isSafeInteger(e) || e < 0)
    throw new S("invalid_payload", `${t} must be a non-negative integer`);
  return e;
}
function zk(e, t) {
  if (!Array.isArray(e) || e.length < 16 || e.length > 128 || !e.every((n) => Number.isInteger(n) && n >= 0 && n <= 255))
    throw new S("invalid_payload", `${t} must contain 16..128 byte integers`);
  return e;
}
function rd(e, t) {
  if (!yt(e)) throw new S(t, "agent delivery context.release must be an object");
  return {
    name: Qt(e.name, "context.release.name"),
    ttlSecs: Nc(e.ttlSecs, "context.release.ttlSecs"),
    approvalNonce: zk(e.approvalNonce, "context.release.approvalNonce"),
    approvalExpiresAtUnix: Nc(e.approvalExpiresAtUnix, "context.release.approvalExpiresAtUnix"),
    operationHash: Qt(e.operationHash, "context.release.operationHash")
  };
}
function ye(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(ye).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${ye(t[n])}`).join(",")}}`;
}
function id(e) {
  const t = yt(e) ? e : null;
  if (!t) throw new S("invalid_payload", "context must be an object");
  if (t.v !== 2) throw new S("invalid_payload", "context version must be 2");
  const n = Dk(t.purpose);
  Qt(t.requestId, "context.requestId"), t.grantId !== void 0 && yi(t.grantId, "context.grantId");
  const r = yt(t.display) ? t.display : null;
  if (!r) throw new S("invalid_payload", "context.display must be an object");
  if (!Array.isArray(r.secrets) || r.secrets.length === 0)
    throw new S("invalid_payload", "context.display.secrets must be a non-empty array");
  r.secrets.forEach((c, l) => {
    if (!yt(c)) throw new S("invalid_payload", `context.display.secrets[${l}] must be an object`);
    Qt(c.name, `context.display.secrets[${l}].name`), Uk(c.kind, `context.display.secrets[${l}].kind`);
  }), r.sessionLabel !== void 0 && yi(r.sessionLabel, "context.display.sessionLabel"), r.command !== void 0 && yi(r.command, "context.display.command"), r.egress !== void 0 && yi(r.egress, "context.display.egress");
  const i = r.source;
  if (i !== void 0) {
    if (!yt(i)) throw new S("invalid_payload", "context.display.source must be an object");
    i.env !== void 0 && Ya(i.env, "context.display.source.env"), i.tags !== void 0 && Ya(i.tags, "context.display.source.tags"), i.skills !== void 0 && Ya(i.skills, "context.display.source.skills");
  }
  r.grantTtlSeconds !== void 0 && Nc(r.grantTtlSeconds, "context.display.grantTtlSeconds");
  const o = yt(t.signature) ? t.signature : null;
  if (!o) throw new S("invalid_payload", "context.signature must be an object");
  if (o.alg !== "ed25519") throw new S("invalid_payload", "context signature alg must be ed25519");
  Qt(o.keyId, "context.signature.keyId"), Qt(o.value, "context.signature.value");
  const s = t.agent;
  if (s !== void 0) {
    if (!yt(s)) throw new S("invalid_payload", "context.agent must be an object");
    const c = yt(s.publicKey) ? s.publicKey : null;
    if (!c) throw new S("invalid_payload", "context.agent.publicKey must be an object");
    Qt(c.public_key, "context.agent.publicKey.public_key"), c.fingerprint !== void 0 && yi(c.fingerprint, "context.agent.publicKey.fingerprint"), Qt(s.fingerprint, "context.agent.fingerprint");
  }
  const a = t.release;
  if (n === "agent-deliver") {
    const c = rd(a, "invalid_payload");
    if (r.grantTtlSeconds === void 0)
      throw new S("invalid_payload", "agent delivery context must display its release TTL");
    if (r.grantTtlSeconds !== c.ttlSecs)
      throw new S("invalid_payload", "signed release TTL does not match display TTL");
  } else if (a !== void 0 && !yt(a))
    throw new S("invalid_payload", "context.release must be an object");
  return Qt(t.expiresAt, "context.expiresAt"), t;
}
function ba(e) {
  const { signature: t, ...n } = e;
  return ye(n);
}
function od(e) {
  if (e.context.purpose !== e.expectedPurpose)
    throw new S("invalid_context", `${e.expectedPurpose} context has the wrong purpose`);
  if (!e.context.display.secrets.some(
    (n) => n.name === e.secret.name && n.kind === e.secret.kind
  )) {
    const n = e.secret.kind === "keypair" ? "key" : "secret";
    throw new S("invalid_context", `${e.expectedPurpose} context does not cover the requested ${n}`);
  }
}
function Ks(e) {
  if (e.expectedPurpose && e.context.purpose !== e.expectedPurpose)
    throw new S("invalid_context", "signed context purpose does not match operation");
  const t = e.now ?? Date.now(), n = Date.parse(e.context.expiresAt);
  if (!Number.isFinite(n) || n <= t)
    throw new S("context_expired", "signed context is expired", !0);
  if (!S2({
    rootMetadata: e.rootMetadata,
    keyId: e.context.signature.keyId,
    signature: e.context.signature.value,
    message: ba(e.context)
  })) throw new S("invalid_context_signature", "signed context signature is invalid");
  if (e.context.purpose === "agent-deliver" && rd(e.context.release, "invalid_context").approvalExpiresAtUnix * 1e3 <= t)
    throw new S("context_expired", "signed release approval is expired", !0);
}
function sd(e) {
  for (const [t, n] of xt)
    n <= e && xt.delete(t);
}
function Mk() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
function ei() {
  return typeof window < "u";
}
function ad() {
  return new S("context_replay_state_unavailable", "signed context replay state is unavailable", !0);
}
function xy(e) {
  const t = Mk();
  if (!t && e && ei()) throw ad();
  return t;
}
function ky(e, t) {
  const n = /* @__PURE__ */ new Map();
  if (!yt(e) || e.version !== 2 || !Array.isArray(e.entries)) return n;
  for (const r of e.entries) {
    if (!Array.isArray(r) || r.length !== 2) continue;
    const [i, o] = r;
    typeof i != "string" || i.length === 0 || typeof o != "number" || !Number.isFinite(o) || o > t && n.set(i, Math.max(n.get(i) ?? 0, o));
  }
  return n;
}
function cd(e = Date.now(), t = !1) {
  sd(e);
  const n = xy(t);
  if (n)
    try {
      n.setItem(vy, JSON.stringify({ version: 2, entries: [...xt] }));
    } catch {
      if (t && ei()) throw ad();
    }
}
function Ey(e, t = Date.now(), n = !0) {
  for (const [r, i] of e)
    i > t && xt.set(r, Math.max(xt.get(r) ?? 0, i));
  n && cd(t);
}
function ld() {
  if (!(_i || typeof BroadcastChannel > "u"))
    try {
      _i = new BroadcastChannel(Hk), _i.addEventListener("message", (e) => {
        const t = yt(e.data) ? e.data : null;
        !t || t.type !== "signed-context-consumed" || Ey(ky({ version: 2, entries: t.entries }, Date.now()));
      });
    } catch {
      _i = null;
    }
}
function Kk(e = Date.now()) {
  af || (ld(), Ay(e, ei()), af = !0);
}
function Ay(e = Date.now(), t = !1) {
  ld();
  const n = xy(t);
  if (n)
    try {
      Ey(ky(JSON.parse(n.getItem(vy) ?? "null"), e), e, !1), sd(e);
    } catch {
      if (t && ei()) throw ad();
      cd(e);
    }
}
function Fk(e) {
  ld();
  try {
    _i?.postMessage({ type: "signed-context-consumed", entries: e });
  } catch {
  }
}
function jk() {
  const t = globalThis.navigator?.locks;
  return !t || typeof t != "object" || typeof t.request != "function" ? null : t;
}
async function Vk(e) {
  const t = jk();
  if (t) return await t.request(Nk, { mode: "exclusive" }, e);
  if (typeof window < "u")
    throw new S("context_replay_lock_unavailable", "signed context replay lock is unavailable", !0);
  return await e();
}
function Sy(e, t) {
  Kk(t);
  const n = /* @__PURE__ */ new Map();
  for (const r of e) {
    const i = Date.parse(r.expiresAt);
    if (!Number.isFinite(i) || i <= t)
      throw new S("context_expired", "signed context is expired", !0);
    n.set(r.requestId, Math.max(n.get(r.requestId) ?? 0, i));
  }
  sd(t);
  for (const r of n.keys())
    if (xt.has(r))
      throw new S("context_replayed", "signed context requestId was already used", !0);
  if (xt.size + n.size > Lk)
    throw new S("context_replay_cache_full", "signed context replay cache is full", !0);
  return n;
}
function ti(e, t = Date.now()) {
  Sy(e, t);
}
async function Mi(e, t = Date.now()) {
  const n = [...e];
  await Vk(() => {
    Ay(t, ei());
    const r = Sy(n, t), i = /* @__PURE__ */ new Map();
    for (const o of r.keys()) i.set(o, xt.get(o));
    for (const [o, s] of r) xt.set(o, s);
    try {
      cd(t, ei());
    } catch (o) {
      for (const [s, a] of i)
        a === void 0 ? xt.delete(s) : xt.set(s, a);
      throw o;
    }
    Fk([...r]);
  });
}
async function qk(e, t) {
  if (e.purpose !== "agent-deliver") throw new S("invalid_context", "context is not an agent delivery");
  if (!e.grantId) throw new S("invalid_context", "agent delivery context is missing grantId");
  const n = rd(e.release, "invalid_context");
  if (n.name !== t) throw new S("invalid_context", "signed release name does not match release item");
  if (e.display.grantTtlSeconds === void 0 || e.display.grantTtlSeconds !== n.ttlSecs)
    throw new S("invalid_context", "signed release TTL does not match display TTL");
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
function Wk(e) {
  if (!e.agent) throw new S("invalid_context", "agent delivery context is missing agent key");
  if (e.agent.publicKey.fingerprint && e.agent.publicKey.fingerprint !== e.agent.fingerprint)
    throw new S("invalid_context", "agent public key fingerprint mismatch");
  return { ...e.agent.publicKey, fingerprint: e.agent.fingerprint };
}
async function Gk(e) {
  const t = e.map((n) => ba(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ye(t))));
}
function cf(e) {
  return ye(e.display);
}
const Zk = "1.2.3";
let pt = class Uc extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof Uc ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof Uc && n.cause.docsPath || n.docsPath, o = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...i ? [`Docs: https://abitype.dev${i}`] : [],
      ...r ? [`Details: ${r}`] : [],
      `Version: abitype@${Zk}`
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
function un(e, t) {
  return e.exec(t)?.groups;
}
const _y = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, Iy = /^u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/, By = /^\(.+?\).*?$/, lf = /^tuple(?<array>(\[(\d*)\])*)$/;
function Dc(e) {
  let t = e.type;
  if (lf.test(e.type) && "components" in e) {
    t = "(";
    const n = e.components.length;
    for (let i = 0; i < n; i++) {
      const o = e.components[i];
      t += Dc(o), i < n - 1 && (t += ", ");
    }
    const r = un(lf, e.type);
    return t += `)${r?.array || ""}`, Dc({
      ...e,
      type: t
    });
  }
  return "indexed" in e && e.indexed && (t = `${t} indexed`), e.name ? `${t} ${e.name}` : t;
}
function gi(e) {
  let t = "";
  const n = e.length;
  for (let r = 0; r < n; r++) {
    const i = e[r];
    t += Dc(i), r !== n - 1 && (t += ", ");
  }
  return t;
}
function Yk(e) {
  return e.type === "function" ? `function ${e.name}(${gi(e.inputs)})${e.stateMutability && e.stateMutability !== "nonpayable" ? ` ${e.stateMutability}` : ""}${e.outputs?.length ? ` returns (${gi(e.outputs)})` : ""}` : e.type === "event" ? `event ${e.name}(${gi(e.inputs)})` : e.type === "error" ? `error ${e.name}(${gi(e.inputs)})` : e.type === "constructor" ? `constructor(${gi(e.inputs)})${e.stateMutability === "payable" ? " payable" : ""}` : e.type === "fallback" ? `fallback() external${e.stateMutability === "payable" ? " payable" : ""}` : "receive() external payable";
}
const Ty = /^error (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Xk(e) {
  return Ty.test(e);
}
function Jk(e) {
  return un(Ty, e);
}
const Py = /^event (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Qk(e) {
  return Py.test(e);
}
function eE(e) {
  return un(Py, e);
}
const $y = /^function (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)(?: (?<scope>external|public{1}))?(?: (?<stateMutability>pure|view|nonpayable|payable{1}))?(?: returns\s?\((?<returns>.*?)\))?$/;
function tE(e) {
  return $y.test(e);
}
function nE(e) {
  return un($y, e);
}
const Oy = /^struct (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*) \{(?<properties>.*?)\}$/;
function Cy(e) {
  return Oy.test(e);
}
function rE(e) {
  return un(Oy, e);
}
const Ry = /^constructor\((?<parameters>.*?)\)(?:\s(?<stateMutability>payable{1}))?$/;
function iE(e) {
  return Ry.test(e);
}
function oE(e) {
  return un(Ry, e);
}
const Ly = /^fallback\(\) external(?:\s(?<stateMutability>payable{1}))?$/;
function sE(e) {
  return Ly.test(e);
}
function aE(e) {
  return un(Ly, e);
}
const cE = /^receive\(\) external payable$/;
function lE(e) {
  return cE.test(e);
}
const dE = /* @__PURE__ */ new Set(["indexed"]), zc = /* @__PURE__ */ new Set([
  "calldata",
  "memory",
  "storage"
]);
class uE extends pt {
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
class fE extends pt {
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
class hE extends pt {
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
class pE extends pt {
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
class wE extends pt {
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
class yE extends pt {
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
class gE extends pt {
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
class di extends pt {
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
class mE extends pt {
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
class bE extends pt {
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
class vE extends pt {
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
class xE extends pt {
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
function kE(e, t, n) {
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
const Xa = /* @__PURE__ */ new Map([
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
function EE(e, t = {}) {
  if (tE(e))
    return AE(e, t);
  if (Qk(e))
    return SE(e, t);
  if (Xk(e))
    return _E(e, t);
  if (iE(e))
    return IE(e, t);
  if (sE(e))
    return BE(e);
  if (lE(e))
    return {
      type: "receive",
      stateMutability: "payable"
    };
  throw new mE({ signature: e });
}
function AE(e, t = {}) {
  const n = nE(e);
  if (!n)
    throw new di({ signature: e, type: "function" });
  const r = bt(n.parameters), i = [], o = r.length;
  for (let a = 0; a < o; a++)
    i.push(pr(r[a], {
      modifiers: zc,
      structs: t,
      type: "function"
    }));
  const s = [];
  if (n.returns) {
    const a = bt(n.returns), c = a.length;
    for (let l = 0; l < c; l++)
      s.push(pr(a[l], {
        modifiers: zc,
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
function SE(e, t = {}) {
  const n = eE(e);
  if (!n)
    throw new di({ signature: e, type: "event" });
  const r = bt(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(pr(r[s], {
      modifiers: dE,
      structs: t,
      type: "event"
    }));
  return { name: n.name, type: "event", inputs: i };
}
function _E(e, t = {}) {
  const n = Jk(e);
  if (!n)
    throw new di({ signature: e, type: "error" });
  const r = bt(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(pr(r[s], { structs: t, type: "error" }));
  return { name: n.name, type: "error", inputs: i };
}
function IE(e, t = {}) {
  const n = oE(e);
  if (!n)
    throw new di({ signature: e, type: "constructor" });
  const r = bt(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(pr(r[s], { structs: t, type: "constructor" }));
  return {
    type: "constructor",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i
  };
}
function BE(e) {
  const t = aE(e);
  if (!t)
    throw new di({ signature: e, type: "fallback" });
  return {
    type: "fallback",
    stateMutability: t.stateMutability ?? "nonpayable"
  };
}
const TE = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, PE = /^\((?<type>.+?)\)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, $E = /^u?int$/;
function pr(e, t) {
  const n = kE(e, t?.type, t?.structs);
  if (Xa.has(n))
    return Xa.get(n);
  const r = By.test(e), i = un(r ? PE : TE, e);
  if (!i)
    throw new hE({ param: e });
  if (i.name && CE(i.name))
    throw new pE({ param: e, name: i.name });
  const o = i.name ? { name: i.name } : {}, s = i.modifier === "indexed" ? { indexed: !0 } : {}, a = t?.structs ?? {};
  let c, l = {};
  if (r) {
    c = "tuple";
    const d = bt(i.type), f = [], h = d.length;
    for (let p = 0; p < h; p++)
      f.push(pr(d[p], { structs: a }));
    l = { components: f };
  } else if (i.type in a)
    c = "tuple", l = { components: a[i.type] };
  else if ($E.test(i.type))
    c = `${i.type}256`;
  else if (i.type === "address payable")
    c = "address";
  else if (c = i.type, t?.type !== "struct" && !Hy(c))
    throw new fE({ type: c });
  if (i.modifier) {
    if (!t?.modifiers?.has?.(i.modifier))
      throw new wE({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
    if (zc.has(i.modifier) && !RE(c, !!i.array))
      throw new yE({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
  }
  const u = {
    type: `${c}${i.array ?? ""}`,
    ...o,
    ...s,
    ...l
  };
  return Xa.set(n, u), u;
}
function bt(e, t = [], n = "", r = 0) {
  const i = e.trim().length;
  for (let o = 0; o < i; o++) {
    const s = e[o], a = e.slice(o + 1);
    switch (s) {
      case ",":
        return r === 0 ? bt(a, [...t, n.trim()]) : bt(a, t, `${n}${s}`, r);
      case "(":
        return bt(a, t, `${n}${s}`, r + 1);
      case ")":
        return bt(a, t, `${n}${s}`, r - 1);
      default:
        return bt(a, t, `${n}${s}`, r);
    }
  }
  if (n === "")
    return t;
  if (r !== 0)
    throw new xE({ current: n, depth: r });
  return t.push(n.trim()), t;
}
function Hy(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || _y.test(e) || Iy.test(e);
}
const OE = /^(?:after|alias|anonymous|apply|auto|byte|calldata|case|catch|constant|copyof|default|defined|error|event|external|false|final|function|immutable|implements|in|indexed|inline|internal|let|mapping|match|memory|mutable|null|of|override|partial|private|promise|public|pure|reference|relocatable|return|returns|sizeof|static|storage|struct|super|supports|switch|this|true|try|typedef|typeof|var|view|virtual)$/;
function CE(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || e === "tuple" || _y.test(e) || Iy.test(e) || OE.test(e);
}
function RE(e, t) {
  return t || e === "bytes" || e === "string" || e === "tuple";
}
function LE(e) {
  const t = {}, n = e.length;
  for (let s = 0; s < n; s++) {
    const a = e[s];
    if (!Cy(a))
      continue;
    const c = rE(a);
    if (!c)
      throw new di({ signature: a, type: "struct" });
    const l = c.properties.split(";"), u = [], d = l.length;
    for (let f = 0; f < d; f++) {
      const p = l[f].trim();
      if (!p)
        continue;
      const y = pr(p, {
        type: "struct"
      });
      u.push(y);
    }
    if (!u.length)
      throw new bE({ signature: a });
    t[c.name] = u;
  }
  const r = {}, i = Object.entries(t), o = i.length;
  for (let s = 0; s < o; s++) {
    const [a, c] = i[s];
    r[a] = Ny(c, t);
  }
  return r;
}
const HE = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*)(?<array>(?:\[\d*?\])+?)?$/;
function Ny(e = [], t = {}, n = /* @__PURE__ */ new Set()) {
  const r = [], i = e.length;
  for (let o = 0; o < i; o++) {
    const s = e[o];
    if (By.test(s.type))
      r.push(s);
    else {
      const c = un(HE, s.type);
      if (!c?.type)
        throw new gE({ abiParameter: s });
      const { array: l, type: u } = c;
      if (u in t) {
        if (n.has(u))
          throw new vE({ type: u });
        r.push({
          ...s,
          type: `tuple${l ?? ""}`,
          components: Ny(t[u], t, /* @__PURE__ */ new Set([...n, u]))
        });
      } else if (Hy(u))
        r.push(s);
      else
        throw new uE({ type: u });
    }
  }
  return r;
}
function NE(e) {
  const t = LE(e), n = [], r = e.length;
  for (let i = 0; i < r; i++) {
    const o = e[i];
    Cy(o) || n.push(EE(o, t));
  }
  return n;
}
function Uy(e, { includeName: t = !1 } = {}) {
  return e ? e.map((n) => UE(n, { includeName: t })).join(t ? ", " : ",") : "";
}
function UE(e, { includeName: t }) {
  return e.type.startsWith("tuple") ? `(${Uy(e.components, { includeName: t })})${e.type.slice(5)}` : e.type + (t && e.name ? ` ${e.name}` : "");
}
function to(e, { strict: t = !0 } = {}) {
  return !e || typeof e != "string" ? !1 : t ? /^0x[0-9a-fA-F]*$/.test(e) : e.startsWith("0x");
}
function Be(e) {
  return to(e, { strict: !1 }) ? Math.ceil((e.length - 2) / 2) : e.length;
}
const Dy = "2.54.6";
let Ja = {
  getDocsUrl: ({ docsBaseUrl: e, docsPath: t = "", docsSlug: n }) => t ? `${e ?? "https://viem.sh"}${t}${n ? `#${n}` : ""}` : void 0,
  version: `viem@${Dy}`
};
class ee extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof ee ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof ee && n.cause.docsPath || n.docsPath, o = Ja.getDocsUrl?.({ ...n, docsPath: i }), s = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...o ? [`Docs: ${o}`] : [],
      ...r ? [`Details: ${r}`] : [],
      ...Ja.version ? [`Version: ${Ja.version}`] : []
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
    }), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.name = n.name ?? this.name, this.shortMessage = t, this.version = Dy;
  }
  walk(t) {
    return zy(this, t);
  }
}
function zy(e, t) {
  return t?.(e) ? e : e && typeof e == "object" && "cause" in e && e.cause !== void 0 ? zy(e.cause, t) : t ? null : e;
}
class DE extends ee {
  constructor({ data: t, params: n, size: r }) {
    super([`Data size of ${r} bytes is too small for given parameters.`].join(`
`), {
      metaMessages: [
        `Params: (${Uy(n, { includeName: !0 })})`,
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
class zE extends ee {
  constructor({ cause: t } = {}) {
    super('Cannot decode zero data ("0x") with ABI parameters.', {
      name: "AbiDecodingZeroDataError",
      cause: t
    });
  }
}
class ME extends ee {
  constructor({ expectedLength: t, givenLength: n, type: r }) {
    super([
      `ABI encoding array length mismatch for type ${r}.`,
      `Expected length: ${t}`,
      `Given length: ${n}`
    ].join(`
`), { name: "AbiEncodingArrayLengthMismatchError" });
  }
}
class KE extends ee {
  constructor({ expectedSize: t, value: n }) {
    super(`Size of bytes "${n}" (bytes${Be(n)}) does not match expected size (bytes${t}).`, { name: "AbiEncodingBytesSizeMismatchError" });
  }
}
class FE extends ee {
  constructor({ expectedLength: t, givenLength: n }) {
    super([
      "ABI encoding params/values length mismatch.",
      `Expected length (params): ${t}`,
      `Given length (values): ${n}`
    ].join(`
`), { name: "AbiEncodingLengthMismatchError" });
  }
}
class jE extends ee {
  constructor({ expectedSize: t, givenSize: n }) {
    super(`Expected bytes${t}, got bytes${n}.`, {
      name: "BytesSizeMismatchError"
    });
  }
}
class VE extends ee {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid encoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiEncodingType" });
  }
}
class qE extends ee {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid decoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiDecodingType" });
  }
}
class WE extends ee {
  constructor(t) {
    super([`Value "${t}" is not a valid array.`].join(`
`), {
      name: "InvalidArrayError"
    });
  }
}
class My extends ee {
  constructor({ offset: t, position: n, size: r }) {
    super(`Slice ${n === "start" ? "starting" : "ending"} at offset "${t}" is out-of-bounds (size: ${r}).`, { name: "SliceOffsetOutOfBoundsError" });
  }
}
class Ky extends ee {
  constructor({ size: t, targetSize: n, type: r }) {
    super(`${r.charAt(0).toUpperCase()}${r.slice(1).toLowerCase()} size (${t}) exceeds padding size (${n}).`, { name: "SizeExceedsPaddingSizeError" });
  }
}
function ui(e, { dir: t, size: n = 32 } = {}) {
  return typeof e == "string" ? zn(e, { dir: t, size: n }) : GE(e, { dir: t, size: n });
}
function zn(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  const r = e.replace("0x", "");
  if (r.length > n * 2)
    throw new Ky({
      size: Math.ceil(r.length / 2),
      targetSize: n,
      type: "hex"
    });
  return `0x${r[t === "right" ? "padEnd" : "padStart"](n * 2, "0")}`;
}
function GE(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  if (e.length > n)
    throw new Ky({
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
class dd extends ee {
  constructor({ max: t, min: n, signed: r, size: i, value: o }) {
    super(`Number "${o}" is not in safe ${i ? `${i * 8}-bit ${r ? "signed" : "unsigned"} ` : ""}integer range ${t ? `(${n} to ${t})` : `(above ${n})`}`, { name: "IntegerOutOfRangeError" });
  }
}
class ZE extends ee {
  constructor(t) {
    super(`Bytes value "${t}" is not a valid boolean. The bytes array must contain a single byte of either a 0 or 1 value.`, {
      name: "InvalidBytesBooleanError"
    });
  }
}
class YE extends ee {
  constructor({ givenSize: t, maxSize: n }) {
    super(`Size cannot exceed ${n} bytes. Given size: ${t} bytes.`, { name: "SizeOverflowError" });
  }
}
function Ki(e, { dir: t = "left" } = {}) {
  let n = typeof e == "string" ? e.replace("0x", "") : e, r = 0;
  for (let i = 0; i < n.length - 1 && n[t === "left" ? i : n.length - i - 1].toString() === "0"; i++)
    r++;
  return n = t === "left" ? n.slice(r) : n.slice(0, n.length - r), typeof e == "string" ? (n.length === 1 && t === "right" && (n = `${n}0`), `0x${n.length % 2 === 1 ? `0${n}` : n}`) : n;
}
function jt(e, { size: t }) {
  if (Be(e) > t)
    throw new YE({
      givenSize: Be(e),
      maxSize: t
    });
}
function Fy(e, t = {}) {
  const { signed: n } = t;
  t.size && jt(e, { size: t.size });
  const r = BigInt(e);
  if (!n)
    return r;
  const i = (e.length - 2) / 2, o = (1n << BigInt(i) * 8n - 1n) - 1n;
  return r <= o ? r : r - BigInt(`0x${"f".padStart(i * 2, "f")}`) - 1n;
}
function ud(e, t = {}) {
  const n = Fy(e, t), r = Number(n);
  if (!Number.isSafeInteger(r))
    throw new dd({
      max: `${Number.MAX_SAFE_INTEGER}`,
      min: `${Number.MIN_SAFE_INTEGER}`,
      signed: t.signed,
      size: t.size,
      value: `${n}n`
    });
  return r;
}
const XE = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Fi(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? Q(e, t) : typeof e == "string" ? Vy(e, t) : typeof e == "boolean" ? jy(e, t) : Je(e, t);
}
function jy(e, t = {}) {
  const n = `0x${Number(e)}`;
  return typeof t.size == "number" ? (jt(n, { size: t.size }), ui(n, { size: t.size })) : n;
}
function Je(e, t = {}) {
  let n = "";
  for (let i = 0; i < e.length; i++)
    n += XE[e[i]];
  const r = `0x${n}`;
  return typeof t.size == "number" ? (jt(r, { size: t.size }), ui(r, { dir: "right", size: t.size })) : r;
}
function Q(e, t = {}) {
  const { signed: n, size: r } = t, i = BigInt(e);
  let o;
  r ? n ? o = (1n << BigInt(r) * 8n - 1n) - 1n : o = 2n ** (BigInt(r) * 8n) - 1n : typeof e == "number" && (o = BigInt(Number.MAX_SAFE_INTEGER));
  const s = typeof o == "bigint" && n ? -o - 1n : 0;
  if (o && i > o || i < s) {
    const c = typeof e == "bigint" ? "n" : "";
    throw new dd({
      max: o ? `${o}${c}` : void 0,
      min: `${s}${c}`,
      signed: n,
      size: r,
      value: `${e}${c}`
    });
  }
  const a = `0x${(n && i < 0 ? (1n << BigInt(r * 8)) + BigInt(i) : i).toString(16)}`;
  return r ? ui(a, { size: r }) : a;
}
const JE = /* @__PURE__ */ new TextEncoder();
function Vy(e, t = {}) {
  const n = JE.encode(e);
  return Je(n, t);
}
const QE = /* @__PURE__ */ new TextEncoder();
function fd(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? tA(e, t) : typeof e == "boolean" ? eA(e, t) : to(e) ? qn(e, t) : qy(e, t);
}
function eA(e, t = {}) {
  const n = new Uint8Array(1);
  return n[0] = Number(e), typeof t.size == "number" ? (jt(n, { size: t.size }), ui(n, { size: t.size })) : n;
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
function qn(e, t = {}) {
  let n = e;
  t.size && (jt(n, { size: t.size }), n = ui(n, { dir: "right", size: t.size }));
  let r = n.slice(2);
  r.length % 2 && (r = `0${r}`);
  const i = r.length / 2, o = new Uint8Array(i);
  for (let s = 0, a = 0; s < i; s++) {
    const c = df(r.charCodeAt(a++)), l = df(r.charCodeAt(a++));
    if (c === void 0 || l === void 0)
      throw new ee(`Invalid byte sequence ("${r[a - 2]}${r[a - 1]}" in "${r}").`);
    o[s] = c * 16 + l;
  }
  return o;
}
function tA(e, t) {
  const n = Q(e, t);
  return qn(n);
}
function qy(e, t = {}) {
  const n = QE.encode(e);
  return typeof t.size == "number" ? (jt(n, { size: t.size }), ui(n, { dir: "right", size: t.size })) : n;
}
const Lo = /* @__PURE__ */ BigInt(2 ** 32 - 1), uf = /* @__PURE__ */ BigInt(32);
function nA(e, t = !1) {
  return t ? { h: Number(e & Lo), l: Number(e >> uf & Lo) } : { h: Number(e >> uf & Lo) | 0, l: Number(e & Lo) | 0 };
}
function rA(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = nA(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const iA = (e, t, n) => e << n | t >>> 32 - n, oA = (e, t, n) => t << n | e >>> 32 - n, sA = (e, t, n) => t << n - 32 | e >>> 64 - n, aA = (e, t, n) => e << n - 32 | t >>> 64 - n;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function cA(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function ff(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function ji(e, ...t) {
  if (!cA(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function Fs(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Wy(e, t) {
  ji(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function lA(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function Vi(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Qa(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function $t(e, t) {
  return e << 32 - t | e >>> t;
}
const dA = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function uA(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function fA(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = uA(e[t]);
  return e;
}
const hf = dA ? (e) => e : fA;
function hA(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function hd(e) {
  return typeof e == "string" && (e = hA(e)), ji(e), e;
}
class Gy {
}
function Zy(e) {
  const t = (r) => e().update(hd(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
const pA = BigInt(0), mi = BigInt(1), wA = BigInt(2), yA = BigInt(7), gA = BigInt(256), mA = BigInt(113), Yy = [], Xy = [], Jy = [];
for (let e = 0, t = mi, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], Yy.push(2 * (5 * r + n)), Xy.push((e + 1) * (e + 2) / 2 % 64);
  let i = pA;
  for (let o = 0; o < 7; o++)
    t = (t << mi ^ (t >> yA) * mA) % gA, t & wA && (i ^= mi << (mi << /* @__PURE__ */ BigInt(o)) - mi);
  Jy.push(i);
}
const Qy = rA(Jy, !0), bA = Qy[0], vA = Qy[1], pf = (e, t, n) => n > 32 ? sA(e, t, n) : iA(e, t, n), wf = (e, t, n) => n > 32 ? aA(e, t, n) : oA(e, t, n);
function xA(e, t = 24) {
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], u = n[c + 1], d = pf(l, u, 1) ^ n[a], f = wf(l, u, 1) ^ n[a + 1];
      for (let h = 0; h < 50; h += 10)
        e[s + h] ^= d, e[s + h + 1] ^= f;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = Xy[s], c = pf(i, o, a), l = wf(i, o, a), u = Yy[s];
      i = e[u], o = e[u + 1], e[u] = c, e[u + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      for (let a = 0; a < 10; a++)
        n[a] = e[s + a];
      for (let a = 0; a < 10; a++)
        e[s + a] ^= ~n[(a + 2) % 10] & n[(a + 4) % 10];
    }
    e[0] ^= bA[r], e[1] ^= vA[r];
  }
  Vi(n);
}
class pd extends Gy {
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (super(), this.pos = 0, this.posOut = 0, this.finished = !1, this.destroyed = !1, this.enableXOF = !1, this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.rounds = o, ff(r), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = lA(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    hf(this.state32), xA(this.state32, this.rounds), hf(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Fs(this), t = hd(t), ji(t);
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
    Fs(this, !1), ji(t), this.finish();
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
    if (Wy(t, this), this.finished)
      throw new Error("digest() was already called");
    return this.writeInto(t), this.destroy(), t;
  }
  digest() {
    return this.digestInto(new Uint8Array(this.outputLen));
  }
  destroy() {
    this.destroyed = !0, Vi(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t || (t = new pd(n, r, i, s, o)), t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.destroyed = this.destroyed, t;
  }
}
const kA = (e, t, n) => Zy(() => new pd(t, e, n)), EA = kA(1, 136, 256 / 8);
function Nt(e, t) {
  const n = t || "hex", r = EA(to(e, { strict: !1 }) ? fd(e) : e);
  return n === "bytes" ? r : Fi(r);
}
const AA = (e) => Nt(fd(e));
function SA(e) {
  return AA(e);
}
function _A(e) {
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
    throw new ee("Unable to normalize signature.");
  return i;
}
const IA = (e) => {
  const t = typeof e == "string" ? e : Yk(e);
  return _A(t);
};
function BA(e) {
  return SA(IA(e));
}
class mr extends ee {
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
class eg extends Map {
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
const ec = /* @__PURE__ */ new eg(8192);
function tg(e, t) {
  if (ec.has(`${e}.${t}`))
    return ec.get(`${e}.${t}`);
  const n = e.substring(2).toLowerCase(), r = Nt(qy(n), "bytes"), i = n.split("");
  for (let s = 0; s < 40; s += 2)
    r[s >> 1] >> 4 >= 8 && i[s] && (i[s] = i[s].toUpperCase()), (r[s >> 1] & 15) >= 8 && i[s + 1] && (i[s + 1] = i[s + 1].toUpperCase());
  const o = `0x${i.join("")}`;
  return ec.set(`${e}.${t}`, o), o;
}
const TA = /^0x[a-fA-F0-9]{40}$/, tc = /* @__PURE__ */ new eg(8192);
function br(e, t) {
  const { strict: n = !0 } = t ?? {}, r = `${e}.${n}`;
  if (tc.has(r))
    return tc.get(r);
  const i = TA.test(e) ? e.toLowerCase() === e ? !0 : n ? tg(e) === e : !0 : !1;
  return tc.set(r, i), i;
}
function PA(e) {
  return typeof e[0] == "string" ? At(e) : $A(e);
}
function $A(e) {
  let t = 0;
  for (const i of e)
    t += i.length;
  const n = new Uint8Array(t);
  let r = 0;
  for (const i of e)
    n.set(i, r), r += i.length;
  return n;
}
function At(e) {
  return `0x${e.reduce((t, n) => t + n.replace("0x", ""), "")}`;
}
function wd(e, t, n, { strict: r } = {}) {
  return to(e, { strict: !1 }) ? og(e, t, n, {
    strict: r
  }) : ig(e, t, n, {
    strict: r
  });
}
function ng(e, t) {
  if (typeof t == "number" && t > 0 && t > Be(e) - 1)
    throw new My({
      offset: t,
      position: "start",
      size: Be(e)
    });
}
function rg(e, t, n) {
  if (typeof t == "number" && typeof n == "number" && Be(e) !== n - t)
    throw new My({
      offset: n,
      position: "end",
      size: Be(e)
    });
}
function ig(e, t, n, { strict: r } = {}) {
  ng(e, t);
  const i = e.slice(t, n);
  return r && rg(i, t, n), i;
}
function og(e, t, n, { strict: r } = {}) {
  ng(e, t);
  const i = `0x${e.replace("0x", "").slice((t ?? 0) * 2, (n ?? e.length) * 2)}`;
  return r && rg(i, t, n), i;
}
const OA = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, sg = /^(u?int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/;
function yd(e, t) {
  if (e.length !== t.length)
    throw new FE({
      expectedLength: e.length,
      givenLength: t.length
    });
  const n = CA({
    params: e,
    values: t
  });
  return md(n);
}
function CA({ params: e, values: t }) {
  const n = [];
  for (let r = 0; r < e.length; r++)
    n.push(gd({ param: e[r], value: t[r] }));
  return n;
}
function gd({ param: e, value: t }) {
  const n = va(e.type);
  if (n) {
    const [r, i] = n;
    return LA(t, { length: r, param: { ...e, type: i } });
  }
  if (e.type === "tuple")
    return zA(t, {
      param: e
    });
  if (e.type === "address")
    return RA(t);
  if (e.type === "bool")
    return NA(t);
  if (e.type.startsWith("uint") || e.type.startsWith("int")) {
    const r = e.type.startsWith("int"), [, , i = "256"] = sg.exec(e.type) ?? [];
    return UA(t, {
      signed: r,
      size: Number(i)
    });
  }
  if (e.type.startsWith("bytes"))
    return HA(t, { param: e });
  if (e.type === "string")
    return DA(t);
  throw new VE(e.type, {
    docsPath: "/docs/contract/encodeAbiParameters"
  });
}
function md(e) {
  let t = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? t += 32 : t += Be(a);
  }
  const n = [], r = [];
  let i = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? (n.push(Q(t + i, { size: 32 })), r.push(a), i += Be(a)) : n.push(a);
  }
  return At([...n, ...r]);
}
function RA(e) {
  if (!br(e))
    throw new mr({ address: e });
  return { dynamic: !1, encoded: zn(e.toLowerCase()) };
}
function LA(e, { length: t, param: n }) {
  const r = t === null;
  if (!Array.isArray(e))
    throw new WE(e);
  if (!r && e.length !== t)
    throw new ME({
      expectedLength: t,
      givenLength: e.length,
      type: `${n.type}[${t}]`
    });
  let i = e.length === 0 && Mc(n);
  const o = [];
  for (let s = 0; s < e.length; s++) {
    const a = gd({ param: n, value: e[s] });
    a.dynamic && (i = !0), o.push(a);
  }
  if (r || i) {
    const s = md(o);
    if (r) {
      const a = Q(o.length, { size: 32 });
      return {
        dynamic: !0,
        encoded: At([a, s])
      };
    }
    if (i)
      return { dynamic: !0, encoded: s };
  }
  return {
    dynamic: !1,
    encoded: At(o.map(({ encoded: s }) => s))
  };
}
function HA(e, { param: t }) {
  const [, n] = t.type.split("bytes"), r = Be(e);
  if (!n) {
    let i = e;
    return r % 32 !== 0 && (i = zn(i, {
      dir: "right",
      size: Math.ceil((e.length - 2) / 2 / 32) * 32
    })), {
      dynamic: !0,
      encoded: At([
        zn(Q(r, { size: 32 })),
        i
      ])
    };
  }
  if (r !== Number.parseInt(n, 10))
    throw new KE({
      expectedSize: Number.parseInt(n, 10),
      value: e
    });
  return { dynamic: !1, encoded: zn(e, { dir: "right" }) };
}
function NA(e) {
  if (typeof e != "boolean")
    throw new ee(`Invalid boolean value: "${e}" (type: ${typeof e}). Expected: \`true\` or \`false\`.`);
  return { dynamic: !1, encoded: zn(jy(e)) };
}
function UA(e, { signed: t, size: n = 256 }) {
  if (typeof n == "number") {
    const r = 2n ** (BigInt(n) - (t ? 1n : 0n)) - 1n, i = t ? -r - 1n : 0n;
    if (e > r || e < i)
      throw new dd({
        max: r.toString(),
        min: i.toString(),
        signed: t,
        size: n / 8,
        value: e.toString()
      });
  }
  return {
    dynamic: !1,
    encoded: Q(e, {
      size: 32,
      signed: t
    })
  };
}
function DA(e) {
  const t = Vy(e), n = Math.ceil(Be(t) / 32), r = [];
  for (let i = 0; i < n; i++)
    r.push(zn(wd(t, i * 32, (i + 1) * 32), {
      dir: "right"
    }));
  return {
    dynamic: !0,
    encoded: At([
      zn(Q(Be(t), { size: 32 })),
      ...r
    ])
  };
}
function zA(e, { param: t }) {
  let n = !1;
  const r = [];
  for (let i = 0; i < t.components.length; i++) {
    const o = t.components[i], s = Array.isArray(e) ? i : o.name, a = gd({
      param: o,
      value: e[s]
    });
    r.push(a), a.dynamic && (n = !0);
  }
  return {
    dynamic: n,
    encoded: n ? md(r) : At(r.map(({ encoded: i }) => i))
  };
}
function va(e) {
  const t = e.match(/^(.*)\[(\d+)?\]$/);
  return t ? (
    // Return `null` if the array is dynamic.
    [t[2] ? Number(t[2]) : null, t[1]]
  ) : void 0;
}
function Mc(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components.some(Mc);
  const n = va(t);
  return n ? Mc({ ...e, type: n[1] }) : !1;
}
const MA = (e) => wd(BA(e), 0, 4);
class yf extends ee {
  constructor({ offset: t }) {
    super(`Offset \`${t}\` cannot be negative.`, {
      name: "NegativeOffsetError"
    });
  }
}
class KA extends ee {
  constructor({ length: t, position: n }) {
    super(`Position \`${n}\` is out of bounds (\`0 < position < ${t}\`).`, { name: "PositionOutOfBoundsError" });
  }
}
class FA extends ee {
  constructor({ count: t, limit: n }) {
    super(`Recursive read limit of \`${n}\` exceeded (recursive read count: \`${t}\`).`, { name: "RecursiveReadLimitExceededError" });
  }
}
const jA = {
  bytes: new Uint8Array(),
  dataView: new DataView(new ArrayBuffer(0)),
  position: 0,
  positionReadCount: /* @__PURE__ */ new Map(),
  recursiveReadCount: 0,
  recursiveReadLimit: Number.POSITIVE_INFINITY,
  assertReadLimit() {
    if (this.recursiveReadCount >= this.recursiveReadLimit)
      throw new FA({
        count: this.recursiveReadCount + 1,
        limit: this.recursiveReadLimit
      });
  },
  assertPosition(e) {
    if (e < 0 || e > this.bytes.length - 1)
      throw new KA({
        length: this.bytes.length,
        position: e
      });
  },
  decrementPosition(e) {
    if (e < 0)
      throw new yf({ offset: e });
    const t = this.position - e;
    this.assertPosition(t), this.position = t;
  },
  getReadCount(e) {
    return this.positionReadCount.get(e || this.position) || 0;
  },
  incrementPosition(e) {
    if (e < 0)
      throw new yf({ offset: e });
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
function bd(e, { recursiveReadLimit: t = 8192 } = {}) {
  const n = Object.create(jA);
  return n.bytes = e, n.dataView = new DataView(e.buffer ?? e, e.byteOffset, e.byteLength), n.positionReadCount = /* @__PURE__ */ new Map(), n.recursiveReadLimit = t, n;
}
function VA(e, t = {}) {
  typeof t.size < "u" && jt(e, { size: t.size });
  const n = Je(e, t);
  return Fy(n, t);
}
function qA(e, t = {}) {
  let n = e;
  if (typeof t.size < "u" && (jt(n, { size: t.size }), n = Ki(n)), n.length > 1 || n[0] > 1)
    throw new ZE(n);
  return !!n[0];
}
function an(e, t = {}) {
  typeof t.size < "u" && jt(e, { size: t.size });
  const n = Je(e, t);
  return ud(n, t);
}
function WA(e, t = {}) {
  let n = e;
  return typeof t.size < "u" && (jt(n, { size: t.size }), n = Ki(n, { dir: "right" })), new TextDecoder().decode(n);
}
function GA(e, t) {
  const n = typeof t == "string" ? qn(t) : t, r = bd(n);
  if (Be(n) === 0 && e.length > 0)
    throw new zE();
  if (Be(t) && Be(t) < 32)
    throw new DE({
      data: typeof t == "string" ? t : Je(t),
      params: e,
      size: Be(t)
    });
  let i = 0;
  const o = [];
  for (let s = 0; s < e.length; ++s) {
    const a = e[s];
    i < n.length && r.setPosition(i);
    const [c, l] = Fr(r, a, {
      staticPosition: 0
    });
    i += l, o.push(c);
  }
  return o;
}
function Fr(e, t, { staticPosition: n }) {
  const r = va(t.type);
  if (r) {
    const [i, o] = r;
    return YA(e, { ...t, type: o }, { length: i, staticPosition: n });
  }
  if (t.type === "tuple")
    return eS(e, t, { staticPosition: n });
  if (t.type === "address")
    return ZA(e);
  if (t.type === "bool")
    return XA(e);
  if (t.type.startsWith("bytes"))
    return JA(e, t, { staticPosition: n });
  if (t.type.startsWith("uint") || t.type.startsWith("int"))
    return QA(e, t);
  if (t.type === "string")
    return tS(e, { staticPosition: n });
  throw new qE(t.type, {
    docsPath: "/docs/contract/decodeAbiParameters"
  });
}
const gf = 32, Kc = 32;
function ZA(e) {
  const t = e.readBytes(32);
  return [tg(Je(ig(t, -20))), 32];
}
function YA(e, t, { length: n, staticPosition: r }) {
  if (n === null) {
    const s = an(e.readBytes(Kc)), a = r + s, c = a + gf;
    e.setPosition(a);
    const l = an(e.readBytes(gf)), u = qi(t);
    let d = 0;
    const f = [];
    for (let h = 0; h < l; ++h) {
      e.setPosition(c + (u ? h * 32 : d));
      const [p, y] = Fr(e, t, {
        staticPosition: c
      });
      d += y, f.push(p), y === 0 && (e.assertReadLimit(), e._touch());
    }
    return e.setPosition(r + 32), [f, 32];
  }
  if (qi(t)) {
    const s = an(e.readBytes(Kc)), a = r + s, c = [];
    for (let l = 0; l < n; ++l) {
      e.setPosition(a + l * 32);
      const [u] = Fr(e, t, {
        staticPosition: a
      });
      c.push(u);
    }
    return e.setPosition(r + 32), [c, 32];
  }
  let i = 0;
  const o = [];
  for (let s = 0; s < n; ++s) {
    const [a, c] = Fr(e, t, {
      staticPosition: r + i
    });
    i += c, o.push(a), c === 0 && (e.assertReadLimit(), e._touch());
  }
  return [o, i];
}
function XA(e) {
  return [qA(e.readBytes(32), { size: 32 }), 32];
}
function JA(e, t, { staticPosition: n }) {
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
function QA(e, t) {
  const n = t.type.startsWith("int"), r = Number.parseInt(t.type.split("int")[1] || "256", 10), i = e.readBytes(32);
  return [
    r > 48 ? VA(i, { signed: n }) : an(i, { signed: n }),
    32
  ];
}
function eS(e, t, { staticPosition: n }) {
  const r = t.components.length === 0 || t.components.some(({ name: s }) => !s), i = r ? [] : {};
  let o = 0;
  if (qi(t)) {
    const s = an(e.readBytes(Kc)), a = n + s;
    for (let c = 0; c < t.components.length; ++c) {
      const l = t.components[c];
      e.setPosition(a + o);
      const [u, d] = Fr(e, l, {
        staticPosition: a
      });
      o += d, i[r ? c : l?.name] = u;
    }
    return e.setPosition(n + 32), [i, 32];
  }
  for (let s = 0; s < t.components.length; ++s) {
    const a = t.components[s], [c, l] = Fr(e, a, {
      staticPosition: n
    });
    i[r ? s : a?.name] = c, o += l;
  }
  return [i, o];
}
function tS(e, { staticPosition: t }) {
  const n = an(e.readBytes(32)), r = t + n;
  e.setPosition(r);
  const i = an(e.readBytes(32));
  if (i === 0)
    return e.setPosition(t + 32), ["", 32];
  const o = e.readBytes(i, 32), s = WA(Ki(o));
  return e.setPosition(t + 32), [s, 32];
}
function qi(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components?.some(qi);
  const n = va(e.type);
  return !!(n && qi({ ...e, type: n[1] }));
}
const nS = (e, t, n) => JSON.stringify(e, (r, i) => typeof i == "bigint" ? i.toString() : i, n), rS = {
  ether: -9,
  wei: 9
};
function iS(e, t) {
  let n = e.toString();
  const r = n.startsWith("-");
  r && (n = n.slice(1)), n = n.padStart(t, "0");
  let [i, o] = [
    n.slice(0, n.length - t),
    n.slice(n.length - t)
  ];
  return o = o.replace(/(0+)$/, ""), `${r ? "-" : ""}${i || "0"}${o ? `.${o}` : ""}`;
}
function Fc(e, t = "wei") {
  return iS(e, rS[t]);
}
function oS(e) {
  const t = Object.entries(e).map(([r, i]) => i === void 0 || i === !1 ? null : [r, i]).filter(Boolean), n = t.reduce((r, [i]) => Math.max(r, i.length), 0);
  return t.map(([r, i]) => `  ${`${r}:`.padEnd(n + 1)}  ${i}`).join(`
`);
}
class sS extends ee {
  constructor({ transaction: t }) {
    super("Cannot infer a transaction type from provided transaction.", {
      metaMessages: [
        "Provided Transaction:",
        "{",
        oS(t),
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
class aS extends ee {
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
class cS extends ee {
  constructor({ storageKey: t }) {
    super(`Size for storage key "${t}" is invalid. Expected 32 bytes. Got ${Math.floor((t.length - 2) / 2)} bytes.`, { name: "InvalidStorageKeySizeError" });
  }
}
function ni(e, t = "hex") {
  const n = ag(e), r = bd(new Uint8Array(n.length));
  return n.encode(r), t === "hex" ? Je(r.bytes) : r.bytes;
}
function ag(e) {
  return Array.isArray(e) ? lS(e.map((t) => ag(t))) : dS(e);
}
function lS(e) {
  const t = e.reduce((i, o) => i + o.length, 0), n = cg(t);
  return {
    length: t <= 55 ? 1 + t : 1 + n + t,
    encode(i) {
      t <= 55 ? i.pushByte(192 + t) : (i.pushByte(247 + n), n === 1 ? i.pushUint8(t) : n === 2 ? i.pushUint16(t) : n === 3 ? i.pushUint24(t) : i.pushUint32(t));
      for (const { encode: o } of e)
        o(i);
    }
  };
}
function dS(e) {
  const t = typeof e == "string" ? qn(e) : e, n = cg(t.length);
  return {
    length: t.length === 1 && t[0] < 128 ? 1 : t.length <= 55 ? 1 + t.length : 1 + n + t.length,
    encode(i) {
      t.length === 1 && t[0] < 128 ? i.pushBytes(t) : t.length <= 55 ? (i.pushByte(128 + t.length), i.pushBytes(t)) : (i.pushByte(183 + n), n === 1 ? i.pushUint8(t.length) : n === 2 ? i.pushUint16(t.length) : n === 3 ? i.pushUint24(t.length) : i.pushUint32(t.length), i.pushBytes(t));
    }
  };
}
function cg(e) {
  if (e < 2 ** 8)
    return 1;
  if (e < 2 ** 16)
    return 2;
  if (e < 2 ** 24)
    return 3;
  if (e < 2 ** 32)
    return 4;
  throw new ee("Length is too large.");
}
class xa extends ee {
  constructor({ cause: t, maxFeePerGas: n } = {}) {
    super(`The fee cap (\`maxFeePerGas\`${n ? ` = ${Fc(n)} gwei` : ""}) cannot be higher than the maximum allowed value (2^256-1).`, {
      cause: t,
      name: "FeeCapTooHighError"
    });
  }
}
Object.defineProperty(xa, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max fee per gas higher than 2\^256-1|fee cap higher than 2\^256-1/
});
class lg extends ee {
  constructor({ cause: t, maxPriorityFeePerGas: n, maxFeePerGas: r } = {}) {
    super([
      `The provided tip (\`maxPriorityFeePerGas\`${n ? ` = ${Fc(n)} gwei` : ""}) cannot be higher than the fee cap (\`maxFeePerGas\`${r ? ` = ${Fc(r)} gwei` : ""}).`
    ].join(`
`), {
      cause: t,
      name: "TipAboveFeeCapError"
    });
  }
}
Object.defineProperty(lg, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max priority fee per gas higher than max fee per gas|tip higher than fee cap/
});
const vd = 2n ** 256n - 1n;
function dg(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((o) => qn(o)) : e.blobs, i = [];
  for (const o of r)
    i.push(Uint8Array.from(t.blobToKzgCommitment(o)));
  return n === "bytes" ? i : i.map((o) => Je(o));
}
function ug(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((s) => qn(s)) : e.blobs, i = typeof e.commitments[0] == "string" ? e.commitments.map((s) => qn(s)) : e.commitments, o = [];
  for (let s = 0; s < r.length; s++) {
    const a = r[s], c = i[s];
    o.push(Uint8Array.from(t.computeBlobKzgProof(a, c)));
  }
  return n === "bytes" ? o : o.map((s) => Je(s));
}
function uS(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function fS(e, t, n) {
  return e & t ^ ~e & n;
}
function hS(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
class pS extends Gy {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Qa(this.buffer);
  }
  update(t) {
    Fs(this), t = hd(t), ji(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Qa(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Fs(this), Wy(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Vi(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    uS(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Qa(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const l = c / 4, u = this.get();
    if (l > u.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < l; d++)
      a.setUint32(4 * d, u[d], o);
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
const $n = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), wS = /* @__PURE__ */ Uint32Array.from([
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
]), On = /* @__PURE__ */ new Uint32Array(64);
class yS extends pS {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = $n[0] | 0, this.B = $n[1] | 0, this.C = $n[2] | 0, this.D = $n[3] | 0, this.E = $n[4] | 0, this.F = $n[5] | 0, this.G = $n[6] | 0, this.H = $n[7] | 0;
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
    for (let d = 0; d < 16; d++, n += 4)
      On[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = On[d - 15], h = On[d - 2], p = $t(f, 7) ^ $t(f, 18) ^ f >>> 3, y = $t(h, 17) ^ $t(h, 19) ^ h >>> 10;
      On[d] = y + On[d - 7] + p + On[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = $t(a, 6) ^ $t(a, 11) ^ $t(a, 25), h = u + f + fS(a, c, l) + wS[d] + On[d] | 0, y = ($t(r, 2) ^ $t(r, 13) ^ $t(r, 22)) + hS(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    Vi(On);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Vi(this.buffer);
  }
}
const gS = /* @__PURE__ */ Zy(() => new yS()), mS = gS;
function bS(e, t) {
  return mS(to(e, { strict: !1 }) ? fd(e) : e);
}
function vS(e) {
  const { commitment: t, version: n = 1 } = e, r = e.to ?? (typeof t == "string" ? "hex" : "bytes"), i = bS(t);
  return i.set([n], 0), r === "bytes" ? i : Je(i);
}
function xS(e) {
  const { commitments: t, version: n } = e, r = e.to ?? (typeof t[0] == "string" ? "hex" : "bytes"), i = [];
  for (const o of t)
    i.push(vS({
      commitment: o,
      to: r,
      version: n
    }));
  return i;
}
const mf = 6, fg = 32, xd = 4096, hg = fg * xd, bf = hg * mf - // terminator byte (0x80).
1 - // zero byte (0x00) appended to each field element.
1 * xd * mf, pg = 1;
class kS extends ee {
  constructor({ maxSize: t, size: n }) {
    super("Blob size is too large.", {
      metaMessages: [`Max: ${t} bytes`, `Given: ${n} bytes`],
      name: "BlobSizeTooLargeError"
    });
  }
}
class wg extends ee {
  constructor() {
    super("Blob data must not be empty.", { name: "EmptyBlobError" });
  }
}
class ES extends ee {
  constructor({ hash: t, size: n }) {
    super(`Versioned hash "${t}" size is invalid.`, {
      metaMessages: ["Expected: 32", `Received: ${n}`],
      name: "InvalidVersionedHashSizeError"
    });
  }
}
class AS extends ee {
  constructor({ hash: t, version: n }) {
    super(`Versioned hash "${t}" version is invalid.`, {
      metaMessages: [
        `Expected: ${pg}`,
        `Received: ${n}`
      ],
      name: "InvalidVersionedHashVersionError"
    });
  }
}
function SS(e) {
  const t = e.to ?? (typeof e.data == "string" ? "hex" : "bytes"), n = typeof e.data == "string" ? qn(e.data) : e.data, r = Be(n);
  if (!r)
    throw new wg();
  if (r > bf)
    throw new kS({
      maxSize: bf,
      size: r
    });
  const i = [];
  let o = !0, s = 0;
  for (; o; ) {
    const a = bd(new Uint8Array(hg));
    let c = 0;
    for (; c < xd; ) {
      const l = n.slice(s, s + (fg - 1));
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
function _S(e) {
  const { data: t, kzg: n, to: r } = e, i = e.blobs ?? SS({ data: t, to: r }), o = e.commitments ?? dg({ blobs: i, kzg: n, to: r }), s = e.proofs ?? ug({ blobs: i, commitments: o, kzg: n, to: r }), a = [];
  for (let c = 0; c < i.length; c++)
    a.push({
      blob: i[c],
      commitment: o[c],
      proof: s[c]
    });
  return a;
}
function IS(e) {
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
  throw new sS({ transaction: e });
}
class ka extends ee {
  constructor({ chainId: t }) {
    super(typeof t == "number" ? `Chain ID "${t}" is invalid.` : "Chain ID is invalid.", { name: "InvalidChainIdError" });
  }
}
function BS(e) {
  const { authorizationList: t } = e;
  if (t)
    for (const n of t) {
      const { chainId: r } = n, i = n.address;
      if (!br(i))
        throw new mr({ address: i });
      if (r < 0)
        throw new ka({ chainId: r });
    }
  kd(e);
}
function TS(e) {
  const { blobVersionedHashes: t } = e;
  if (t) {
    if (t.length === 0)
      throw new wg();
    for (const n of t) {
      const r = Be(n), i = ud(wd(n, 0, 1));
      if (r !== 32)
        throw new ES({ hash: n, size: r });
      if (i !== pg)
        throw new AS({
          hash: n,
          version: i
        });
    }
  }
  kd(e);
}
function kd(e) {
  const { chainId: t, maxPriorityFeePerGas: n, maxFeePerGas: r, to: i } = e;
  if (t <= 0)
    throw new ka({ chainId: t });
  if (i && !br(i))
    throw new mr({ address: i });
  if (r && r > vd)
    throw new xa({ maxFeePerGas: r });
  if (n && r && n > r)
    throw new lg({ maxFeePerGas: r, maxPriorityFeePerGas: n });
}
function PS(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (t <= 0)
    throw new ka({ chainId: t });
  if (o && !br(o))
    throw new mr({ address: o });
  if (n || i)
    throw new ee("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid EIP-2930 Transaction attribute.");
  if (r && r > vd)
    throw new xa({ maxFeePerGas: r });
}
function $S(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (o && !br(o))
    throw new mr({ address: o });
  if (typeof t < "u" && t <= 0)
    throw new ka({ chainId: t });
  if (n || i)
    throw new ee("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid Legacy Transaction attribute.");
  if (r && r > vd)
    throw new xa({ maxFeePerGas: r });
}
function Ea(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (let n = 0; n < e.length; n++) {
    const { address: r, storageKeys: i } = e[n];
    for (let o = 0; o < i.length; o++)
      if (i[o].length - 2 !== 64)
        throw new cS({ storageKey: i[o] });
    if (!br(r, { strict: !1 }))
      throw new mr({ address: r });
    t.push([r, i]);
  }
  return t;
}
function OS(e, t) {
  const n = IS(e);
  return n === "eip1559" ? LS(e, t) : n === "eip2930" ? HS(e, t) : n === "eip4844" ? RS(e, t) : n === "eip7702" ? CS(e, t) : NS(e);
}
function CS(e, t) {
  const { authorizationList: n, chainId: r, gas: i, nonce: o, to: s, value: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: u, data: d } = e;
  BS(e);
  const f = Ea(u), h = US(n);
  return At([
    "0x04",
    ni([
      Q(r),
      o ? Q(o) : "0x",
      l ? Q(l) : "0x",
      c ? Q(c) : "0x",
      i ? Q(i) : "0x",
      s ?? "0x",
      a ? Q(a) : "0x",
      d ?? "0x",
      f,
      h,
      ...no(e, t)
    ])
  ]);
}
function RS(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerBlobGas: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: u, data: d } = e;
  TS(e);
  let f = e.blobVersionedHashes, h = e.sidecars;
  if (e.blobs && (typeof f > "u" || typeof h > "u")) {
    const v = typeof e.blobs[0] == "string" ? e.blobs : e.blobs.map((C) => Je(C)), L = e.kzg, T = dg({
      blobs: v,
      kzg: L
    });
    if (typeof f > "u" && (f = xS({
      commitments: T
    })), typeof h > "u") {
      const C = ug({ blobs: v, commitments: T, kzg: L });
      h = _S({ blobs: v, commitments: T, proofs: C });
    }
  }
  const p = Ea(u), y = [
    Q(n),
    i ? Q(i) : "0x",
    l ? Q(l) : "0x",
    c ? Q(c) : "0x",
    r ? Q(r) : "0x",
    o ?? "0x",
    s ? Q(s) : "0x",
    d ?? "0x",
    p,
    a ? Q(a) : "0x",
    f ?? [],
    ...no(e, t)
  ], w = [], m = [], g = [];
  if (h)
    for (let v = 0; v < h.length; v++) {
      const { blob: L, commitment: T, proof: C } = h[v];
      w.push(L), m.push(T), g.push(C);
    }
  return At([
    "0x03",
    // If sidecars are enabled, envelope turns into a "wrapper":
    ni(h ? [y, w, m, g] : y)
  ]);
}
function LS(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerGas: a, maxPriorityFeePerGas: c, accessList: l, data: u } = e;
  kd(e);
  const d = Ea(l), f = [
    Q(n),
    i ? Q(i) : "0x",
    c ? Q(c) : "0x",
    a ? Q(a) : "0x",
    r ? Q(r) : "0x",
    o ?? "0x",
    s ? Q(s) : "0x",
    u ?? "0x",
    d,
    ...no(e, t)
  ];
  return At([
    "0x02",
    ni(f)
  ]);
}
function HS(e, t) {
  const { chainId: n, gas: r, data: i, nonce: o, to: s, value: a, accessList: c, gasPrice: l } = e;
  PS(e);
  const u = Ea(c), d = [
    Q(n),
    o ? Q(o) : "0x",
    l ? Q(l) : "0x",
    r ? Q(r) : "0x",
    s ?? "0x",
    a ? Q(a) : "0x",
    i ?? "0x",
    u,
    ...no(e, t)
  ];
  return At([
    "0x01",
    ni(d)
  ]);
}
function NS(e, t) {
  const { chainId: n = 0, gas: r, data: i, nonce: o, to: s, value: a, gasPrice: c } = e;
  $S(e);
  let l = [
    o ? Q(o) : "0x",
    c ? Q(c) : "0x",
    r ? Q(r) : "0x",
    s ?? "0x",
    a ? Q(a) : "0x",
    i ?? "0x"
  ];
  return n > 0 && (l = [
    ...l,
    Q(n),
    "0x",
    "0x"
  ]), ni(l);
}
function no(e, t) {
  const n = t ?? e, { v: r, yParity: i } = n;
  if (typeof n.r > "u")
    return [];
  if (typeof n.s > "u")
    return [];
  if (typeof r > "u" && typeof i > "u")
    return [];
  const o = Ki(n.r), s = Ki(n.s);
  return [typeof i == "number" ? i ? Q(1) : "0x" : r === 0n ? "0x" : r === 1n ? Q(1) : r === 27n ? "0x" : Q(1), o === "0x00" ? "0x" : o, s === "0x00" ? "0x" : s];
}
function US(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (const n of e) {
    const { chainId: r, nonce: i, ...o } = n, s = n.address;
    t.push([
      r ? Fi(r) : "0x",
      s,
      i ? Fi(i) : "0x",
      ...no({}, o)
    ]);
  }
  return t;
}
class DS extends ee {
  constructor({ domain: t }) {
    super(`Invalid domain "${nS(t)}".`, {
      metaMessages: ["Must be a valid EIP-712 domain."]
    });
  }
}
class zS extends ee {
  constructor({ primaryType: t, types: n }) {
    super(`Invalid primary type \`${t}\` must be one of \`${JSON.stringify(Object.keys(n))}\`.`, {
      docsPath: "/api/glossary/Errors#typeddatainvalidprimarytypeerror",
      metaMessages: ["Check that the primary type is a key in `types`."]
    });
  }
}
class MS extends ee {
  constructor({ type: t }) {
    super(`Struct type "${t}" is invalid.`, {
      metaMessages: ["Struct type must not be a Solidity type."],
      name: "InvalidStructTypeError"
    });
  }
}
function KS(e) {
  const { domain: t, message: n, primaryType: r, types: i } = e, o = (s, a) => {
    for (const c of s) {
      const { name: l, type: u } = c, d = a[l], f = u.match(sg);
      if (f && (typeof d == "number" || typeof d == "bigint")) {
        const [y, w, m] = f;
        Q(d, {
          signed: w === "int",
          size: Number.parseInt(m, 10) / 8
        });
      }
      if (u === "address" && typeof d == "string" && !br(d))
        throw new mr({ address: d });
      const h = u.match(OA);
      if (h) {
        const [y, w] = h;
        if (w && Be(d) !== Number.parseInt(w, 10))
          throw new jE({
            expectedSize: Number.parseInt(w, 10),
            givenSize: Be(d)
          });
      }
      const p = i[u];
      p && (FS(u), o(p, d));
    }
  };
  if (i.EIP712Domain && t) {
    if (typeof t != "object")
      throw new DS({ domain: t });
    o(i.EIP712Domain, t);
  }
  if (r !== "EIP712Domain")
    if (i[r])
      o(i[r], n);
    else
      throw new zS({ primaryType: r, types: i });
}
function yg({ domain: e }) {
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
function FS(e) {
  if (e === "address" || e === "bool" || e === "string" || e.startsWith("bytes") || e.startsWith("uint") || e.startsWith("int"))
    throw new MS({ type: e });
}
function jS(e) {
  const { domain: t = {}, message: n, primaryType: r } = e, i = {
    EIP712Domain: yg({ domain: t }),
    ...e.types
  };
  KS({
    domain: t,
    message: n,
    primaryType: r,
    types: i
  });
  const o = ["0x1901"];
  return t && o.push(VS({
    domain: t,
    types: i
  })), r !== "EIP712Domain" && o.push(gg({
    data: n,
    primaryType: r,
    types: i
  })), Nt(PA(o));
}
function VS({ domain: e, types: t }) {
  return gg({
    data: e,
    primaryType: "EIP712Domain",
    types: t
  });
}
function gg({ data: e, primaryType: t, types: n }) {
  const r = mg({
    data: e,
    primaryType: t,
    types: n
  });
  return Nt(r);
}
function mg({ data: e, primaryType: t, types: n }) {
  const r = [{ type: "bytes32" }], i = [qS({ primaryType: t, types: n })];
  for (const o of n[t]) {
    const [s, a] = vg({
      types: n,
      name: o.name,
      type: o.type,
      value: e[o.name]
    });
    r.push(s), i.push(a);
  }
  return yd(r, i);
}
function qS({ primaryType: e, types: t }) {
  const n = Fi(WS({ primaryType: e, types: t }));
  return Nt(n);
}
function WS({ primaryType: e, types: t }) {
  let n = "";
  const r = bg({ primaryType: e, types: t });
  r.delete(e);
  const i = [e, ...Array.from(r).sort()];
  for (const o of i)
    n += `${o}(${t[o].map(({ name: s, type: a }) => `${a} ${s}`).join(",")})`;
  return n;
}
function bg({ primaryType: e, types: t }, n = /* @__PURE__ */ new Set()) {
  const i = e.match(/^\w*/u)?.[0];
  if (n.has(i) || t[i] === void 0)
    return n;
  n.add(i);
  for (const o of t[i])
    bg({ primaryType: o.type, types: t }, n);
  return n;
}
function vg({ types: e, name: t, type: n, value: r }) {
  if (e[n] !== void 0)
    return [
      { type: "bytes32" },
      Nt(mg({ data: r, primaryType: n, types: e }))
    ];
  if (n === "bytes")
    return [{ type: "bytes32" }, Nt(r)];
  if (n === "string")
    return [{ type: "bytes32" }, Nt(Fi(r))];
  if (n.lastIndexOf("]") === n.length - 1) {
    const i = n.slice(0, n.lastIndexOf("[")), o = r.map((s) => vg({
      name: t,
      type: i,
      types: e,
      value: s
    }));
    return [
      { type: "bytes32" },
      Nt(yd(o.map(([s]) => s), o.map(([, s]) => s)))
    ];
  }
  return [{ type: n }, r];
}
function GS(e) {
  const t = og(e, 0, 1);
  if (t === "0x04")
    return "eip7702";
  if (t === "0x03")
    return "eip4844";
  if (t === "0x02")
    return "eip1559";
  if (t === "0x01")
    return "eip2930";
  if (t !== "0x" && ud(t) >= 192)
    return "legacy";
  throw new aS({ serializedType: t });
}
const vf = "bitcoin-taproot-keypath-v1", xf = "bip341-sighash-default-keypath", xg = ["bitcoin", "testnet", "signet", "regtest"], ri = 21000000n * 100000000n, kg = 137, Eg = /^[0-9a-f]+$/, ZS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", YS = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", XS = 734539939, JS = 1;
function re(e) {
  throw new S("invalid_payload", e);
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
function Yo(e, t, n) {
  return (typeof e != "string" || n !== void 0 && e.length !== n || e.length === 0 || !Eg.test(e)) && re(`${t} must be lowercase hexadecimal${n === void 0 ? "" : ` of length ${n}`}`), e;
}
function QS(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < n || e > r) && re(`${t} must be an integer in ${n}..${r}`), e;
}
function pe(...e) {
  const t = new Uint8Array(e.reduce((r, i) => r + i.length, 0));
  let n = 0;
  for (const r of e)
    t.set(r, n), n += r.length;
  return t;
}
function $r(e, t, n, r) {
  return t + n > e.length && re(`transaction is truncated while reading ${r}`), [e.slice(t, t + n), t + n];
}
function Ho(e, t, n) {
  t >= e.length && re(`transaction is truncated before ${n}`);
  const r = e[t];
  return r >= 253 && re(`${n} must use a single-byte CompactSize`), [r, t + 1];
}
function nc(e) {
  return ((e[0] ?? 0) | (e[1] ?? 0) << 8 | (e[2] ?? 0) << 16 | (e[3] ?? 0) << 24) >>> 0;
}
function e_(e) {
  let t = 0n;
  for (let n = 0; n < 8; n += 1) t |= BigInt(e[n] ?? 0) << BigInt(n * 8);
  return t;
}
function Fe(e) {
  return new Uint8Array([e & 255, e >>> 8 & 255, e >>> 16 & 255, e >>> 24 & 255]);
}
function js(e) {
  const t = new Uint8Array(8);
  let n = e;
  for (let r = 0; r < 8; r += 1)
    t[r] = Number(n & 0xffn), n >>= 8n;
  return t;
}
function Ag(e) {
  if (e.length === 25 && e[0] === 118 && e[1] === 169 && e[2] === 20 && e[23] === 136 && e[24] === 172) return "p2pkh";
  if (e.length === 23 && e[0] === 169 && e[1] === 20 && e[22] === 135) return "p2sh";
  if (e.length === 22 && e[0] === 0 && e[1] === 20) return "p2wpkh";
  if (e.length === 34 && e[0] === 0 && e[1] === 32) return "p2wsh";
  if (e.length === 34 && e[0] === 81 && e[1] === 32) return "p2tr";
  re("transaction output uses an unsupported script template");
}
function Sg(e) {
  (e.length === 0 || e.length % 2 || e.length > kg * 2 || !Eg.test(e)) && re("unsignedTransaction must be lowercase hex, without 0x or whitespace, and at most 137 bytes");
  const t = Uint8Array.from(e.match(/../g), (H) => Number.parseInt(H, 16));
  (t.length < 4 || t[0] !== 2 || t[1] !== 0 || t[2] !== 0 || t[3] !== 0) && re("transaction version must be exactly 2");
  let n = 4;
  const [r, i] = Ho(t, n, "input count");
  n = i, r !== 1 && re("transaction must contain exactly one input");
  const [o, s] = $r(t, n, 32, "outpoint hash");
  n = s, o.every((H) => H === 0) && re("outpoint hash must not be null");
  const [a, c] = $r(t, n, 4, "outpoint index");
  n = c;
  const l = nc(a), [u, d] = Ho(t, n, "scriptSig length");
  n = d, u !== 0 && re("scriptSig must be empty");
  const [f, h] = $r(t, n, 4, "sequence");
  n = h;
  const p = nc(f);
  p !== 4294967293 && p !== 4294967294 && p !== 4294967295 && re("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff");
  const [y, w] = Ho(t, n, "output count");
  n = w, y !== 1 && y !== 2 && re("transaction must contain one or two outputs");
  const m = [], g = { p2pkh: 546n, p2sh: 540n, p2wpkh: 294n, p2wsh: 330n, p2tr: 330n };
  for (let H = 0; H < y; H += 1) {
    const [O, I] = $r(t, n, 8, `output ${H} amount`);
    n = I;
    const U = e_(O);
    (U < 1n || U > ri) && re(`output ${H} amount is outside the Bitcoin money range`);
    const [z, E] = Ho(t, n, `output ${H} script length`);
    n = E;
    const [k, x] = $r(t, n, z, `output ${H} script`);
    n = x;
    const b = Ag(k);
    z !== { p2pkh: 25, p2sh: 23, p2wpkh: 22, p2wsh: 34, p2tr: 34 }[b] && re(`output ${H} script length is not canonical for ${b}`), U < g[b] && re(`output ${H} is below the V1 ${b} dust floor`), m.push({ amountSats: U, scriptPubKey: k, kind: b });
  }
  const [v, L] = $r(t, n, 4, "locktime");
  n = L;
  const T = nc(v);
  T !== 0 && re("locktime must be exactly zero"), n !== t.length && re("transaction has trailing bytes");
  const C = m.reduce((H, O) => H + O.amountSats, 0n);
  return (C < 1n || C > ri) && re("output total is outside the Bitcoin money range"), { raw: t, prevoutHashLe: o, prevoutIndex: l, sequence: p, outputs: m, locktime: T };
}
function t_(e, t) {
  const n = ue(new TextEncoder().encode(e));
  return ue(pe(n, n, t));
}
function n_(e, t, n) {
  const r = pe(e.prevoutHashLe, Fe(e.prevoutIndex)), i = pe(new Uint8Array([n.length]), n), o = e.outputs.map((a) => pe(js(a.amountSats), new Uint8Array([a.scriptPubKey.length]), a.scriptPubKey)), s = pe(
    new Uint8Array([0, 0]),
    Fe(2),
    Fe(e.locktime),
    ue(r),
    ue(js(t)),
    ue(i),
    ue(Fe(e.sequence)),
    ue(pe(...o)),
    new Uint8Array([0]),
    Fe(0)
  );
  return t_("TapSighash", s);
}
function r_(e) {
  return BigInt(`0x${G(e)}`);
}
function jc(e) {
  return gl(ue(e));
}
function Af(e, t) {
  const n = pe(new Uint8Array([e]), t), r = ue(ue(n)).slice(0, 4), i = pe(n, r);
  let o = r_(i), s = "";
  for (; o > 0n; ) {
    const a = Number(o % 58n);
    s = ZS[a] + s, o /= 58n;
  }
  for (const a of i) {
    if (a !== 0) break;
    s = `1${s}`;
  }
  return s;
}
function i_(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >> i & s);
  return i > 0 && o.push(r << n - i & s), o;
}
function o_(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1) i >>> o & 1 && (n ^= t[o]);
  }
  return n >>> 0;
}
function rc(e, t, n) {
  const r = e === "bitcoin" ? "bc" : e === "regtest" ? "bcrt" : "tb", i = [t, ...i_(n, 8, 5)], o = [...r].map((l) => l.charCodeAt(0) >> 5).concat([0], [...r].map((l) => l.charCodeAt(0) & 31)), s = t === 0 ? JS : XS, a = o_(o.concat(i, [0, 0, 0, 0, 0, 0])) ^ s, c = Array.from({ length: 6 }, (l, u) => a >>> 5 * (5 - u) & 31);
  return `${r}1${i.concat(c).map((l) => YS[l]).join("")}`;
}
function Wi(e, t) {
  const n = Ag(t), r = e === "bitcoin";
  return n === "p2pkh" ? Af(r ? 0 : 111, t.slice(3, 23)) : n === "p2sh" ? Af(r ? 5 : 196, t.slice(2, 22)) : n === "p2wpkh" || n === "p2wsh" ? rc(e, 0, t.slice(2)) : rc(e, 1, t.slice(2));
}
function s_(e) {
  const t = jc(e), n = pe(new Uint8Array([0, 20]), t);
  return [
    pe(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    n,
    pe(new Uint8Array([169, 20]), jc(n), new Uint8Array([135]))
  ];
}
function jr(e, t) {
  return e.length === t.length && e.every((n, r) => n === t[r]);
}
function No(e) {
  const t = e / 100000000n, n = e % 100000000n;
  return n === 0n ? `${t}.00000000` : `${t}.${n.toString().padStart(8, "0")}`;
}
function _g(e, t) {
  const n = e / BigInt(t), r = e % BigInt(t);
  if (r === 0n) return `${n.toString()} sat/vB`;
  const i = (r * 10000n / BigInt(t)).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n.toString()}.${i} sat/vB`;
}
function Ig(e) {
  return G(ue(ue(e.raw)).slice().reverse());
}
function a_(e, t) {
  const n = t.transaction, r = Y("bitcoin.network", { network: e.network }), i = [
    Y("bitcoin.title"),
    ...t.highFeeWarning ? [Y("bitcoin.highFeeWarning"), ""] : [],
    r,
    Y("bitcoin.networkWarning"),
    Y("bitcoin.input", { txid: G(n.prevoutHashLe.slice().reverse()), vout: n.prevoutIndex }),
    Y("bitcoin.inputAmount", { sats: e.prevout.amountSats, btc: No(BigInt(e.prevout.amountSats)) }),
    Y("bitcoin.outputs")
  ];
  return n.outputs.forEach((o, s) => {
    const a = t.sameKeyOutputIndexes.includes(s);
    i.push(Y("bitcoin.output", {
      index: s + 1,
      address: Wi(e.network, o.scriptPubKey),
      sats: o.amountSats.toString(),
      btc: No(o.amountSats),
      role: Y(a ? "bitcoin.sameKeyReturn" : "bitcoin.recipient")
    }));
  }), i.push(
    Y("bitcoin.fee", { sats: t.feeSats.toString(), btc: No(t.feeSats), rate: _g(t.feeSats, t.signedVsize) }),
    Y("bitcoin.vsize", { vsize: t.signedVsize }),
    Y(n.sequence === 4294967293 ? "bitcoin.rbfSignals" : "bitcoin.rbfDoesNotSignal"),
    Y("bitcoin.netLeaving", { sats: t.netLeavingSats.toString(), btc: No(t.netLeavingSats) }),
    Y("bitcoin.unsignedTxid", { txid: Ig(n) }),
    Y("bitcoin.outputKey", { key: G(t.outputKeyXOnly) })
  ), i.join(`
`);
}
function c_(e, t) {
  const n = kf(e, "signingContext");
  Ef(n, ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"], "bitcoin signingContext"), n.kind !== vf && re("unsupported Bitcoin signing_context kind"), (typeof n.network != "string" || !xg.includes(n.network)) && re("bitcoin signing_context network is invalid");
  const r = Yo(n.unsignedTransaction, "unsignedTransaction");
  r.length > kg * 2 && re("unsignedTransaction exceeds the 137-byte V1 limit");
  const i = kf(n.prevout, "signingContext.prevout");
  Ef(i, ["amountSats", "scriptPubKey"], "signingContext.prevout");
  const o = QS(i.amountSats, "prevout.amountSats", 1, Number(ri)), s = Yo(i.scriptPubKey, "prevout.scriptPubKey", 68);
  s.startsWith("5120") || re("prevout.scriptPubKey must be a lowercase P2TR output key script"), n.digestAlgorithm !== xf && re("unsupported Bitcoin digest algorithm");
  const a = Yo(n.digest, "signingContext.digest", 64);
  return a !== t && re("Bitcoin signing_context digest must match the sign request digest exactly"), {
    kind: vf,
    network: n.network,
    unsignedTransaction: r,
    prevout: { amountSats: o, scriptPubKey: s },
    digestAlgorithm: xf,
    digest: a
  };
}
function Bg(e, t, n) {
  const r = c_(e, t), i = Sg(r.unsignedTransaction), o = Uint8Array.from(r.prevout.scriptPubKey.match(/../g), (m) => Number.parseInt(m, 16)), s = o.slice(2);
  let a;
  if (n !== void 0) {
    const m = Yo(n, "signing public key", 66);
    a = Uint8Array.from(m.match(/../g), (v) => Number.parseInt(v, 16)), a[0] !== 2 && a[0] !== 3 && re("signing public key must be compressed secp256k1");
    const g = jw(m);
    jr(s, g) || re("prevout.scriptPubKey does not bind to the signing key's BIP86 output key");
  }
  if (a)
    for (const m of i.outputs)
      s_(a).some((g) => jr(g, m.scriptPubKey)) && re("transaction sends to a same-key legacy or SegWit-v0 address this workflow cannot spend");
  const c = i.outputs.flatMap((m, g) => jr(m.scriptPubKey, o) ? [g] : []);
  c.length > 1 && re("transaction may contain at most one same-key P2TR return output");
  const l = i.outputs.reduce((m, g) => m + g.amountSats, 0n), u = BigInt(r.prevout.amountSats) - l;
  u <= 0n && re("transaction fee must be positive");
  const d = i.raw.length * 4 + 68, f = Math.ceil(d / 4);
  u > 10000n * BigInt(f) && re("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling");
  const h = G(n_(i, BigInt(r.prevout.amountSats), o));
  h !== r.digest && re("Bitcoin signing_context digest does not match the independently computed BIP341 digest");
  const p = c.reduce((m, g) => m + i.outputs[g].amountSats, 0n), y = u > 100n * BigInt(f) || u * 100n > BigInt(r.prevout.amountSats) * 2n, w = BigInt(r.prevout.amountSats) - p;
  return {
    digest: h,
    display: a_(r, {
      transaction: i,
      outputKeyXOnly: s,
      feeSats: u,
      signedVsize: f,
      sameKeyOutputIndexes: c,
      highFeeWarning: y,
      netLeavingSats: w
    }),
    facts: {
      network: r.network,
      spendingAddress: Wi(r.network, o),
      inputTxid: G(i.prevoutHashLe.slice().reverse()),
      inputVout: i.prevoutIndex,
      inputAmountSats: BigInt(r.prevout.amountSats),
      outputs: i.outputs.map((m, g) => ({
        address: Wi(r.network, m.scriptPubKey),
        amountSats: m.amountSats,
        sameKey: c.includes(g)
      })),
      feeSats: u,
      feeRate: _g(u, f),
      signedVsize: f,
      netLeavingSats: w,
      highFeeWarning: y,
      sequence: i.sequence,
      signalsRbf: i.sequence === 4294967293,
      locktime: i.locktime,
      unsignedTxid: Ig(i),
      ...n !== void 0 ? { internalKeyXOnly: n.slice(2).toLowerCase() } : {},
      outputKeyXOnly: G(s),
      unsignedTransaction: r.unsignedTransaction,
      prevoutScriptPubKey: r.prevout.scriptPubKey
    },
    transaction: i,
    outputKeyXOnly: s
  };
}
const Sf = `avault-agent-operation:v1
`;
function Or(e) {
  if (typeof e != "object" || e === null || Array.isArray(e)) throw new Error("signing context is malformed");
  return e;
}
function _f(e) {
  return e.startsWith("0x") ? e.toLowerCase() : `0x${e.toLowerCase()}`;
}
function ic(e, t) {
  const n = _f(e), r = _f(t);
  if (n !== r)
    throw new Error("signing digest does not match verified context");
  return r.slice(2);
}
function Xo(e) {
  return typeof e == "bigint" ? e.toString() : typeof e == "string" || typeof e == "number" || typeof e == "boolean" ? String(e) : e == null ? "" : Vr(e);
}
function Vr(e) {
  if (typeof e == "bigint") return JSON.stringify(e.toString());
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(Vr).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${Vr(t[n])}`).join(",")}}`;
}
const l_ = NE([
  "function approve(address spender,uint256 amount)",
  // A common extension, not an EIP-20 method: the amount is added to the current allowance.
  "function increaseAllowance(address spender,uint256 addedValue)",
  "function transfer(address to,uint256 amount)",
  "function transferFrom(address from,address to,uint256 amount)",
  "function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
  "function setApprovalForAll(address operator,bool approved)",
  "function safeTransferFrom(address from,address to,uint256 tokenId)",
  "function safeTransferFrom(address from,address to,uint256 tokenId,bytes data)"
]), d_ = {
  approve: "ERC-20/ERC-721 approve",
  increaseAllowance: "ERC-20 increaseAllowance (extension)",
  transfer: "ERC-20 transfer",
  transferFrom: "ERC-20/ERC-721 transferFrom",
  permit: "ERC-20 permit",
  setApprovalForAll: "ERC-721 setApprovalForAll",
  safeTransferFrom: "ERC-721 safeTransferFrom",
  safeTransferFromWithData: "ERC-721 safeTransferFrom"
}, u_ = new Map(
  l_.map((e) => {
    const t = e.name === "safeTransferFrom" && e.inputs.length === 4 ? "safeTransferFromWithData" : e.name;
    return [MA(e), { call: t, item: e }];
  })
);
function f_(e) {
  return `${e.name}(${e.inputs.map((t) => t.type).join(",")})`;
}
function h_(e) {
  if (e == null || e === "" || e === "0x") return "0x";
  if (typeof e != "string" || !/^0x[0-9a-fA-F]*$/.test(e) || e.length % 2 !== 0)
    throw new Error("EVM transaction calldata is malformed");
  return e.toLowerCase();
}
const p_ = /* @__PURE__ */ new Set([
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
]), w_ = /* @__PURE__ */ new Set(["domain", "types", "primaryType", "message"]);
function oc(e, t, n) {
  const r = Object.keys(e).filter((i) => !t.has(i));
  if (r.length > 0)
    throw new Error(`${n} carries unsupported field: ${r.sort().join(", ")}`);
}
function Tg(e) {
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
function y_(e, t) {
  if (t.chainId === void 0 || t.chainId === null || t.chainId === "")
    throw new Error("EVM transaction chain id is required");
  if (String(t.chainId) !== String(e.chainId))
    throw new Error("EVM transaction chain id mismatch");
}
function g_(e) {
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
function m_(e) {
  if (e.length < 10) return null;
  const t = u_.get(e.slice(0, 10));
  if (!t) return null;
  let n;
  try {
    if (n = GA(t.item.inputs, `0x${e.slice(10)}`), yd(t.item.inputs, n).toLowerCase() !== `0x${e.slice(10)}`) return null;
  } catch {
    return null;
  }
  return {
    call: t.call,
    signature: f_(t.item),
    arguments: t.item.inputs.map((r, i) => ({
      name: r.name ?? `arg${i}`,
      type: r.type,
      value: Xo(n[i])
    }))
  };
}
function b_(e, t, n) {
  const r = Tg(e), i = h_(e.data), o = (c) => (t[c] === void 0 ? 0n : t[c]).toString(), s = e.to === void 0 || e.to === null || e.to === "" ? null : String(e.to), a = s === null ? null : m_(i);
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
function v_(e) {
  const t = [Y("evm.title", { chainId: e.chainId })];
  if (e.blindSigning && t.push(Y("sign.blindNotice")), t.push(
    Y("evm.type", { type: e.type }),
    e.to === null ? Y("evm.contractCreation") : Y("evm.to", { to: e.to }),
    Y("evm.value", { value: e.value }),
    Y("evm.nonce", { value: e.nonce }),
    Y("evm.gas", { value: e.gas })
  ), e.gasPrice !== void 0 && t.push(Y("evm.gasPrice", { value: e.gasPrice })), e.maxFeePerGas !== void 0 && t.push(Y("evm.maxFeePerGas", { value: e.maxFeePerGas })), e.maxPriorityFeePerGas !== void 0 && t.push(Y("evm.maxPriorityFeePerGas", { value: e.maxPriorityFeePerGas })), e.decoded) {
    t.push(Y("evm.call", { call: `${d_[e.decoded.call]} ${e.decoded.signature}` }));
    for (const n of e.decoded.arguments) t.push(`  ${n.name} (${n.type}): ${n.value}`);
  }
  return t.push(
    e.selector === null ? Y("evm.selectorNone") : Y("evm.selector", { selector: e.selector }),
    e.calldata === "0x" ? Y("evm.calldataNone") : Y("evm.calldata", { bytes: e.calldataBytes, calldata: e.calldata })
  ), t.join(`
`);
}
function x_(e) {
  gr(e);
  const t = Or(e), n = {
    "evm-transaction": ["kind", "chainId", "unsignedTransaction", "digestAlgorithm", "digest"],
    "eip-712-typed-data": ["kind", "typedData", "digestAlgorithm", "digest"],
    "avault-agent-operation": ["kind", "canonicalPreimage", "digestAlgorithm", "digest"],
    "bitcoin-taproot-keypath-v1": ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"]
  }, r = Object.hasOwn(n, e.kind) ? n[e.kind] : void 0;
  if (!r) throw new Error("unsupported signing context");
  if (oc(t, new Set(r), "signing context"), typeof e.digest != "string" || !/^(?:0x)?[0-9a-fA-F]{64}$/.test(e.digest))
    throw new Error("signing context digest must be 32 bytes");
  switch (e.kind) {
    case "evm-transaction": {
      if (e.digestAlgorithm !== "keccak256") throw new Error("unsupported EVM digest algorithm");
      const i = Or(e.unsignedTransaction);
      oc(i, p_, "EVM transaction"), y_(e, i), Tg(i);
      const o = g_(i), s = OS(o), a = ic(e.digest, Nt(s)), c = b_(i, o, s);
      if (GS(s) !== c.type) throw new Error("EVM transaction type does not match its serialization");
      return {
        digest: a,
        challenge: ut(a),
        display: v_(c),
        evm: c
      };
    }
    case "eip-712-typed-data": {
      if (e.digestAlgorithm !== "eip712") throw new Error("unsupported EIP-712 digest algorithm");
      const i = Or(e.typedData);
      oc(i, w_, "EIP-712 typed data");
      const o = Or(i.domain);
      if (o.chainId === void 0 || o.verifyingContract === void 0)
        throw new Error("EIP-712 domain must include chainId and verifyingContract");
      const s = ic(e.digest, jS(i));
      return {
        digest: s,
        challenge: ut(s),
        display: [
          "EIP-712 typed data",
          `domain.chainId: ${Xo(o.chainId)}`,
          `domain.verifyingContract: ${Xo(o.verifyingContract)}`,
          `domain: ${Vr(o)}`,
          `types: ${Vr(Or(i.types))}`,
          `primaryType: ${Xo(i.primaryType)}`,
          `message: ${Vr(Or(i.message))}`
        ].join(`
`),
        typedData: i
      };
    }
    case "avault-agent-operation": {
      if (e.digestAlgorithm !== "avault-operation-hash-v1") throw new Error("unsupported avault digest algorithm");
      if (typeof e.canonicalPreimage != "string" || !e.canonicalPreimage.startsWith(Sf) || e.canonicalPreimage.slice(Sf.length).trim().length === 0)
        throw new Error("canonical preimage must start with avault-agent-operation:v1\\n followed by operation text; recreate the request");
      const i = ic(e.digest, Zx(ue(new TextEncoder().encode(e.canonicalPreimage))));
      return {
        digest: i,
        challenge: ut(i),
        display: ["Avault agent operation", e.canonicalPreimage].join(`
`)
      };
    }
    case "bitcoin-taproot-keypath-v1": {
      const i = Bg(e, e.digest);
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
function Jo(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) return;
  const t = e.messageHex;
  if (typeof t == "string")
    try {
      xp(t);
    } catch {
      throw new S("invalid_payload", "signing messageHex exceeds the 1 MiB decoded limit");
    }
}
function Aa(e) {
  if (Jo(e), gr(e), !e || typeof e != "object" || Array.isArray(e)) throw new S("invalid_payload", "Solana signing context must be an object");
  const t = e;
  if (t.kind !== "solana-transaction-v1" || Object.keys(t).some((n) => n !== "kind" && n !== "messageHex") || typeof t.messageHex != "string" || t.messageHex.length === 0 || t.messageHex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(t.messageHex))
    throw new S("invalid_payload", "Solana context requires only kind and nonempty lowercase even messageHex; no digest alias");
  return { kind: "solana-transaction-v1", messageHex: t.messageHex };
}
function k_(e, t, n) {
  if (n.kind !== "keypair" || !n.publicKey) throw new Error("protected signing requires a keypair protected record");
  const r = ci(n.curve);
  if (e === Ni || t.kind === "solana-transaction-v1" || r === "ed25519") {
    if (e !== Ni || t.kind !== "solana-transaction-v1" || r !== "ed25519")
      throw new S("invalid_payload", "Ed25519 signing requires an Ed25519 record, ed25519 scheme and Solana context together");
    Aa(t);
  }
}
const ro = 1048576, E_ = ["bitcoin-p2pkh-v1", "bitcoin-p2wpkh-v1"];
function qe(e, t, n, r) {
  if (typeof e != "string" || e.length === 0 || e.length > t * 2 || e.length % 2 || r !== void 0 && e.length !== r * 2) throw new S("invalid_payload", `${n} has an invalid hexadecimal size`);
  for (let i = 0; i < e.length; i++) {
    const o = e.charCodeAt(i);
    if (!(o >= 48 && o <= 57) && !(o >= 97 && o <= 102)) throw new S("invalid_payload", `${n} must be lowercase hexadecimal`);
  }
  return e;
}
const If = 524288, A_ = 4718592, S_ = 2162688;
function ke(e) {
  throw new S("invalid_payload", `Bitcoin resource limit: ${e}`);
}
function le(e, t) {
  if (!(!e || typeof e != "object"))
    return Object.getOwnPropertyDescriptor(e, t)?.value;
}
function Vs(e, t) {
  return !!e && typeof e == "object" && Object.hasOwn(e, t);
}
function __(e) {
  return E_.includes(le(e, "kind")) || Vs(le(e, "prevout"), "parentTransaction");
}
function Pg(e, t) {
  if (!e || typeof e != "object") return;
  const n = le(e, "prevout");
  Vs(n, "parentTransaction") && (qe(le(n, "parentTransaction"), ro, "parentTransaction"), t.set(n, "parentTransaction"));
  const r = le(e, "unsignedTransaction");
  r !== void 0 && qe(r, 137, "unsignedTransaction");
  const i = le(e, "digest");
  i !== void 0 && qe(i, 32, "digest", 32);
  const o = le(n, "txid");
  o !== void 0 && qe(o, 32, "parent txid", 32);
  const s = le(n, "scriptPubKey");
  s !== void 0 && qe(s, 25, "prevout script");
}
function Ed(e, t, n, r = 2) {
  let i = 0, o = 0, s = 0, a = 0, c = 0;
  const l = /* @__PURE__ */ new Set();
  function u(h, p = !1) {
    i += h, p || (o += h), (i > n || o > If) && ke("canonical byte budget exceeded");
  }
  function d(h, p = !1) {
    h.length > (p ? ro * 2 : If) && ke("string size exceeded"), u(2);
    for (let y = 0; y < h.length; y++) {
      const w = h.charCodeAt(y);
      let m;
      if (w === 34 || w === 92 || w === 8 || w === 9 || w === 10 || w === 12 || w === 13) m = 2;
      else if (w < 32) m = 6;
      else if (w < 128) m = 1;
      else if (w < 2048) m = 2;
      else if (w >= 55296 && w <= 56319) {
        const g = h.charCodeAt(++y);
        g >= 56320 && g <= 57343 || ke("unpaired Unicode surrogate"), m = 4;
      } else
        w >= 56320 && w <= 57343 && ke("unpaired Unicode surrogate"), m = 3;
      u(m, p);
    }
  }
  const f = [{ value: e, depth: 0 }];
  for (; f.length; ) {
    const h = f.pop();
    if ("exit" in h) {
      l.delete(h.exit);
      continue;
    }
    (++s > 4096 || h.depth > 16) && ke("node/depth budget exceeded");
    const p = h.value;
    if (p === null) {
      u(4);
      continue;
    }
    if (typeof p == "boolean") {
      u(p ? 4 : 5);
      continue;
    }
    if (typeof p == "number") {
      Number.isSafeInteger(p) || ke("numbers must be safe integers"), u(String(p).length);
      continue;
    }
    if (typeof p == "string") {
      h.proof && (++a > r || (c += p.length) > 4194304) && ke("proof-copy budget exceeded"), d(p, h.proof);
      continue;
    }
    (!p || typeof p != "object") && ke("only JSON values are permitted"), l.has(p) && ke("cyclic structure");
    const y = Array.isArray(p);
    !y && Object.getPrototypeOf(p) !== Object.prototype && Object.getPrototypeOf(p) !== null && ke("only plain JSON objects are permitted"), y && p.length > 2048 && ke("array size/shape exceeded"), l.add(p), f.push({ exit: p });
    const w = Reflect.ownKeys(p);
    if (y) {
      w.length !== p.length + 1 && ke("array size/shape exceeded"), u(2 + Math.max(0, p.length - 1));
      for (let m = p.length - 1; m >= 0; m--) {
        const g = Object.getOwnPropertyDescriptor(p, String(m));
        (!g || !("value" in g) || !g.enumerable) && ke("array accessors/holes are not JSON"), f.push({ value: g.value, depth: h.depth + 1 });
      }
    } else {
      w.length > 128 && ke("object field budget exceeded");
      const m = [];
      for (const g of w) {
        typeof g != "string" && ke("symbol fields are not JSON");
        const v = Object.getOwnPropertyDescriptor(p, g);
        (!("value" in v) || !v.enumerable) && ke("accessor/nonenumerable fields are not JSON"), m.push([g, v.value]);
      }
      u(2 + Math.max(0, m.length - 1));
      for (const [g, v] of m) {
        d(g), u(1), g === "parentTransaction" && t.get(p) !== g && ke("additional embedded proof copy");
        const L = t.get(p) === g;
        L && typeof v != "string" && ke("proof must be a string"), f.push({ value: v, depth: h.depth + 1, proof: L });
      }
    }
  }
}
function $g(e) {
  const t = /* @__PURE__ */ new Map();
  Pg(e, t), Ed(e, t, le(e, "kind") === "bitcoin-p2wpkh-v1" ? 4096 : S_);
}
function I_(e) {
  const t = le(le(e, "context"), "release"), n = le(e, "delivery"), r = le(le(n, "operation_context"), "release"), i = [le(e, "signingContext"), le(e, "signing_context"), le(t, "signingContext"), le(n, "signing_context"), le(r, "signingContext")];
  if (!i.some(__)) return;
  Vs(e, "signingContext") && Vs(e, "signing_context") && ke("multiple direct context aliases");
  const o = /* @__PURE__ */ new Map();
  for (const a of i) Pg(a, o);
  const s = le(le(e, "material"), "envelope");
  if (s && typeof s == "object") {
    const a = Reflect.ownKeys(s);
    a.length > 128 && ke("material envelope field budget exceeded");
    for (const c of a) {
      const l = Object.getOwnPropertyDescriptor(s, c)?.value;
      typeof l == "string" && l.length > 262144 && ke("material envelope string exceeded");
    }
  }
  Ed(e, o, A_);
  for (const a of i) a !== void 0 && $g(a);
}
const B_ = ["review.kind.bitcoinLegacyTransfer", "review.kind.bitcoinNativeTransfer"], T_ = 2621440;
function Og(e) {
  const t = le(e, "review");
  if (!B_.includes(le(t, "kind"))) return;
  const n = le(t, "details"), r = /* @__PURE__ */ new Map();
  if (Array.isArray(n) && n.length <= 16)
    for (let i = 0; i < n.length; i++) {
      const o = le(le(n, String(i)), "fields");
      if (!(!Array.isArray(o) || o.length > 512))
        for (let s = 0; s < o.length; s++) {
          const a = le(o, String(s));
          le(a, "label") === "review.field.parentTransaction" && (qe(le(a, "value"), ro, "parentTransaction"), r.set(a, "value"));
        }
    }
  Ed(e, r, T_, 1);
}
function Wn(e) {
  if (!e || typeof e != "object") return !1;
  const t = e.kind;
  return t === "bitcoin-p2pkh-v1" || t === "bitcoin-p2wpkh-v1";
}
function ae(e) {
  throw new S("invalid_payload", e);
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
function Ur(e) {
  return ue(ue(e));
}
function Vc(e) {
  return G(Ur(e).reverse());
}
function Ad(e) {
  qe(e, 33, "selected public key", 33), /^(02|03)/.test(e) || ae("selected public key must be compressed secp256k1");
  const t = We(e);
  try {
    fe.Point.fromBytes(t).assertValidity();
  } catch {
    ae("selected public key is not a secp256k1 point");
  }
  return t;
}
function Uo(e) {
  const t = jc(Ad(e));
  return {
    p2pkh: pe(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    p2wpkh: pe(new Uint8Array([0, 20]), t),
    p2tr: pe(new Uint8Array([81, 32]), jw(e))
  };
}
function P_(e) {
  const t = qe(e, ro, "parentTransaction"), n = We(t);
  let r = 0;
  function i(f) {
    (!Number.isSafeInteger(f) || f < 0 || f > n.length - r) && ae("funding transaction is truncated");
    const h = n.subarray(r, r + f);
    return r += f, h;
  }
  function o(f) {
    const h = i(f);
    let p = 0n;
    for (let y = 0; y < f; y++) p |= BigInt(h[y]) << BigInt(y * 8);
    return p;
  }
  function s(f, h) {
    const p = i(1)[0], y = p < 253 ? BigInt(p) : o(p === 253 ? 2 : p === 254 ? 4 : 8);
    return (p === 253 && y < 253n || p === 254 && y < 65536n || p === 255 && y < 4294967296n) && ae(`funding ${h} has a nonminimal CompactSize`), (y > BigInt(Number.MAX_SAFE_INTEGER) || y > BigInt(f)) && ae(`funding ${h} exceeds its bound`), Number(y);
  }
  i(4);
  const a = s(128, "input count");
  a === 0 && ae("funding transaction must have 1..128 inputs, without witness serialization");
  for (let f = 0; f < a; f++)
    i(36), i(s(1e4, "input script length")), i(4);
  const c = s(128, "output count");
  c === 0 && ae("funding transaction must have 1..128 outputs");
  const l = [];
  let u = 0n;
  for (let f = 0; f < c; f++) {
    const h = o(8);
    u += h, (h > ri || u > ri) && ae("funding output amount/total exceeds the Bitcoin money range");
    const p = i(s(1e4, "output script length"));
    l.push({ amountSats: h, scriptPubKey: p });
  }
  i(4), r !== n.length && ae("funding transaction has trailing bytes or witnesses");
  const d = Ur(n);
  return { raw: n, outputs: l, hash: d, txid: G(d.slice().reverse()) };
}
function $_(e, t) {
  $g(e);
  const n = Bf(e, "Bitcoin signingContext"), r = Bf(n.prevout, "Bitcoin prevout");
  Object.hasOwn(r, "parentTransaction") && qe(r.parentTransaction, ro, "parentTransaction"), Tf(n, ["kind", "addressType", "network", "unsignedTransaction", "prevout", "sighashType", "digestAlgorithm", "digest"], "Bitcoin signingContext");
  const i = n.kind === "bitcoin-p2pkh-v1";
  return (!i && n.kind !== "bitcoin-p2wpkh-v1" || n.addressType !== (i ? "p2pkh" : "p2wpkh") || n.digestAlgorithm !== (i ? "bitcoin-legacy-sighash-all" : "bitcoin-bip143-sighash-all") || n.sighashType !== 1) && ae("Bitcoin context kind/type/algorithm/SIGHASH_ALL mismatch"), xg.includes(n.network) || ae("Bitcoin network is invalid"), qe(n.unsignedTransaction, 137, "unsignedTransaction"), qe(n.digest, 32, "digest", 32), n.digest !== t && ae("Bitcoin context digest differs from request digest"), Tf(r, i ? ["parentTransaction", "txid", "vout", "amountSats", "scriptPubKey"] : ["amountSats", "scriptPubKey"], "Bitcoin prevout"), Pf(r.amountSats, 1, Number(ri), "prevout amount"), qe(r.scriptPubKey, i ? 25 : 22, "prevout script", i ? 25 : 22), i && (qe(r.txid, 32, "parent txid", 32), Pf(r.vout, 0, 4294967295, "prevout vout")), n;
}
function O_(e) {
  return pe(...e.outputs.map((t) => pe(js(t.amountSats), new Uint8Array([t.scriptPubKey.length]), t.scriptPubKey)));
}
function C_(e, t, n, r) {
  t !== "p2pkh" && t !== "p2wpkh" && ae("unsupported ECDSA Bitcoin type");
  const i = pe(e.prevoutHashLe, Fe(e.prevoutIndex)), o = pe(new Uint8Array([r.length]), r), s = O_(e), a = t === "p2pkh" ? pe(Fe(2), new Uint8Array([1]), i, o, Fe(e.sequence), new Uint8Array([e.outputs.length]), s, Fe(e.locktime), Fe(1)) : pe(Fe(2), Ur(i), Ur(Fe(e.sequence)), i, o, js(n), Fe(e.sequence), Ur(s), Fe(e.locktime), Fe(1));
  return G(Ur(a));
}
function R_(e, t) {
  return e === "p2pkh" ? { min: t + 44, max: t + 107 } : { min: Math.ceil((4 * t + 47) / 4), max: Math.ceil((4 * t + 110) / 4) };
}
function Cg(e, t, n) {
  const r = 100n * e / BigInt(n), i = (100n * e + BigInt(t) - 1n) / BigInt(t), o = (s) => `${s / 100n}.${(s % 100n).toString().padStart(2, "0")}`;
  return { lo100: r, hi100: i, lower: o(r), upper: o(i) };
}
function Rg(e, t, n) {
  return e <= 0n && ae("transaction fee must be positive"), e > 10000n * BigInt(n) && ae("transaction fee exceeds the conservative 10,000 sat/vB ceiling"), e > 100n * BigInt(n) || 100n * e > 2n * t;
}
function Lg(e, t, n, r) {
  const i = $_(e, t), o = Sg(i.unsignedTransaction), s = Uo(n), a = s[i.addressType];
  let c;
  if (i.kind === "bitcoin-p2pkh-v1") {
    const g = i.prevout;
    c = P_(g.parentTransaction), (!jr(o.prevoutHashLe, c.hash) || g.txid !== c.txid || g.vout !== o.prevoutIndex || g.vout >= c.outputs.length) && ae("Legacy parent txid/outpoint/vout mismatch");
    const v = c.outputs[g.vout];
    (v.amountSats <= 0n || v.amountSats !== BigInt(g.amountSats) || G(v.scriptPubKey) !== g.scriptPubKey) && ae("Legacy selected parent amount/script mismatch");
  }
  i.prevout.scriptPubKey !== G(a) && ae("prevout script does not match the selected key/type");
  const l = C_(o, i.addressType, BigInt(i.prevout.amountSats), s.p2pkh);
  l !== i.digest && ae("Bitcoin digest differs from independently computed SIGHASH_ALL");
  const u = r ?? { p2pkh: n, p2wpkh: n, p2tr: n };
  u[i.addressType] !== n && ae("selected BTC purpose point differs from selected key");
  const d = r ? [Uo(u.p2pkh).p2pkh, Uo(u.p2wpkh).p2wpkh, Uo(u.p2tr).p2tr] : Object.values(s), f = o.outputs.flatMap((g, v) => d.some((L) => jr(L, g.scriptPubKey)) ? [v] : []);
  f.length > 1 && ae("transaction must not have more than one same-key/account return OUTPUT");
  const h = BigInt(i.prevout.amountSats), p = h - o.outputs.reduce((g, v) => g + v.amountSats, 0n), y = R_(i.addressType, o.raw.length), w = Rg(p, h, y.min), m = f.length ? o.outputs[f[0]].amountSats : 0n;
  return {
    context: i,
    transaction: o,
    digest: l,
    publicKey: n,
    facts: {
      network: i.network,
      addressType: i.addressType,
      spendingAddress: Wi(i.network, a),
      inputTxid: G(o.prevoutHashLe.slice().reverse()),
      inputVout: o.prevoutIndex,
      inputAmountSats: h,
      prevoutScriptPubKey: i.prevout.scriptPubKey,
      scriptCode: G(s.p2pkh),
      serializedScriptCode: `19${G(s.p2pkh)}`,
      outputs: o.outputs.map((g, v) => ({ index: v, address: Wi(i.network, g.scriptPubKey), kind: g.kind, amountSats: g.amountSats, scriptPubKey: G(g.scriptPubKey), returned: f.includes(v) })),
      feeSats: p,
      vsize: y,
      rate: Cg(p, y.min, y.max),
      highFeeWarning: w,
      returnedSats: m,
      netLeavingSats: h - m,
      sequence: o.sequence,
      signalsRbf: o.sequence === 4294967293,
      locktime: o.locktime,
      unsignedTransaction: i.unsignedTransaction,
      unsignedTemplateHash: Vc(o.raw),
      ...c ? { parent: { transaction: i.kind === "bitcoin-p2pkh-v1" ? i.prevout.parentTransaction : "", txid: c.txid, byteLength: c.raw.length } } : {}
    }
  };
}
function L_(e, t, n) {
  const r = qe(e, 71, "DER signature");
  r.length < 16 && ae("DER signature must be 8..71 bytes"), qe(t, 32, "digest", 32);
  const i = Ad(n), o = We(r);
  try {
    const s = fe.Signature.fromBytes(o, "der");
    (s.r <= 0n || s.s <= 0n || s.hasHighS() || !jr(s.toBytes("der"), o) || !fe.verify(o, We(t), i, { prehash: !1, lowS: !0, format: "der" })) && ae("invalid strict low-S DER signature for selected point/digest");
  } catch {
    ae("invalid strict low-S DER signature for selected point/digest");
  }
  return o;
}
function H_(e, t, n, r, i) {
  const o = Lg(e, t, n, i), s = L_(r, o.digest, n), a = o.transaction, c = Ad(n), l = pe(s, new Uint8Array([1]));
  let u, d;
  if (o.context.addressType === "p2pkh") {
    const p = pe(new Uint8Array([l.length]), l, new Uint8Array([33]), c);
    u = pe(a.raw.subarray(0, 41), new Uint8Array([p.length]), p, a.raw.subarray(42)), d = 4 * u.length, u.length !== a.raw.length + s.length + 36 && ae("Legacy signed size closure failed");
  } else {
    const p = pe(new Uint8Array([2, l.length]), l, new Uint8Array([33]), c);
    u = pe(a.raw.subarray(0, 4), new Uint8Array([0, 1]), a.raw.subarray(4, a.raw.length - 4), p, a.raw.subarray(a.raw.length - 4)), d = 3 * a.raw.length + u.length, d !== 4 * a.raw.length + s.length + 39 && ae("Native signed weight closure failed");
  }
  const f = Math.ceil(d / 4);
  (f < o.facts.vsize.min || f > o.facts.vsize.max) && ae("signed vsize is outside its conservative range");
  const h = Rg(o.facts.feeSats, o.facts.inputAmountSats, f);
  return {
    signature: G(s),
    signedTransaction: G(u),
    weight: d,
    vsize: f,
    txid: Vc(o.context.addressType === "p2wpkh" ? a.raw : u),
    wtxid: Vc(u),
    feeSats: o.facts.feeSats,
    rate: Cg(o.facts.feeSats, f, f),
    highFeeWarning: h || o.facts.highFeeWarning
  };
}
const Qo = Object.freeze({
  // The source now includes authenticated fresh-UV/key proof and one-sign local
  // closure for BOTH new kinds. Release/pin registry enablement remains separate.
  ordinaryInputTypes: Object.freeze(["p2pkh", "p2wpkh", "p2tr"]),
  hdInputTypes: Object.freeze(["p2pkh", "p2wpkh", "p2tr"]),
  ecdsaReturnTypes: Object.freeze(["p2pkh", "p2wpkh", "p2tr"])
});
function N_(e = Qo) {
  return ["p2pkh", "p2wpkh", "p2tr"].every((t) => e.ordinaryInputTypes.includes(t));
}
const Hg = "11111111111111111111111111111111", Ng = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", U_ = "SysvarRecentB1ockHashes11111111111111111111", D_ = 2, z_ = 4, M_ = 3, K_ = 12;
function cn(e) {
  return { decoded: null, undecodedReason: e };
}
function Ug(e, t) {
  let n = 0n;
  for (let r = 7; r >= 0; r -= 1) n = n << 8n | BigInt(e[t + r]);
  return n.toString();
}
function Sd(e) {
  const t = [];
  for (const n of e) {
    if (n.source !== "static") return null;
    t.push(n.address);
  }
  return t;
}
function F_(e, t) {
  const n = t.length >= 4 ? (t[0] | t[1] << 8 | t[2] << 16 | t[3] << 24) >>> 0 : -1;
  return n === D_ && t.length === 12 ? j_(e, t) : n === z_ && t.length === 4 ? V_(e) : cn("unsupported_instruction");
}
function j_(e, t) {
  if (e.length !== 2) return cn("unsupported_instruction");
  const n = Sd(e);
  if (!n) return cn("unresolved_account");
  const [r, i] = n;
  return { decoded: { kind: "system-transfer", source: r, destination: i, lamports: Ug(t, 4) }, undecodedReason: null };
}
function V_(e) {
  if (e.length !== 3) return cn("unsupported_instruction");
  const t = Sd(e);
  if (!t) return cn("unresolved_account");
  const [n, r, i] = t;
  return r !== U_ ? cn("unsupported_instruction") : { decoded: { kind: "system-advance-nonce", nonceAccount: n, recentBlockhashesSysvar: r, nonceAuthority: i }, undecodedReason: null };
}
function q_(e, t) {
  const n = t.length === 9 && t[0] === M_ && e.length === 3, r = t.length === 10 && t[0] === K_ && e.length === 4;
  if (!n && !r) return cn("unsupported_instruction");
  const i = Sd(e);
  if (!i) return cn("unresolved_account");
  const o = Ug(t, 1);
  if (n) {
    const [u, d, f] = i;
    return { decoded: { kind: "spl-transfer", sourceTokenAccount: u, destinationTokenAccount: d, authority: f, amount: o }, undecodedReason: null };
  }
  const [s, a, c, l] = i;
  return {
    decoded: { kind: "spl-transfer-checked", sourceTokenAccount: s, mint: a, destinationTokenAccount: c, authority: l, amount: o, decimals: t[9] },
    undecodedReason: null
  };
}
function W_(e, t, n) {
  return e === Hg ? F_(t, n) : e === Ng ? q_(t, n) : cn("unsupported_program");
}
const G_ = 32, sc = 512, bi = 256, $f = 8, Dg = 3, zg = /* @__PURE__ */ new Map([
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
]), Mg = /* @__PURE__ */ new Set(["1", "10", "8453", "42161", "11155111", "11155420", "84532", "421614"]);
function Kg(e) {
  return zg.has(e) ? [] : ["review.note.unknownChain"];
}
function Fg(e) {
  const t = zg.get(e);
  return t === void 0 ? e : `${e} · ${t}`;
}
function qs(e) {
  return typeof e == "object" && e !== null && !Array.isArray(e);
}
function jg(e) {
  if (typeof e == "bigint") return e;
  if (typeof e != "string" && typeof e != "number" && typeof e != "boolean" && !Array.isArray(e)) return null;
  try {
    return BigInt(e);
  } catch {
    return null;
  }
}
function Mn(e, t) {
  if (/^u?int\d*$/.test(e)) {
    const n = jg(t);
    return n === null ? JSON.stringify(t) ?? String(t) : n.toString();
  }
  return typeof t == "string" ? t : JSON.stringify(t) ?? String(t);
}
class Of extends Error {
}
function Ws(e, t, n, r, i, o) {
  if (i > G_) {
    o.omitted += 1;
    return;
  }
  const s = e[t];
  if (s !== void 0) {
    if (!Object.hasOwn(e, t) || !Array.isArray(s) || n === null || n === void 0) throw new Of(r);
    const a = Object(n), c = new Set(s.map((l) => l.name));
    for (const l of s) Ws(e, l.type, a[l.name], r ? `${r}.${l.name}` : l.name, i + 1, o);
    if (typeof n == "object")
      for (const l of Object.keys(n)) c.has(l) || o.uncovered.push(r ? `${r}.${l}` : l);
    return;
  }
  if (t !== "bytes" && t !== "string" && t.endsWith("]")) {
    if (!Array.isArray(n)) throw new Of(r);
    const a = t.slice(0, t.lastIndexOf("["));
    n.length === 0 && o.covered.push({ path: r, type: t, value: "[]" }), n.forEach((c, l) => Ws(e, a, c, `${r}[${l}]`, i + 1, o));
    return;
  }
  o.covered.push({ path: r, type: t, value: Mn(t, n) });
}
function Z_(e, t) {
  const n = /* @__PURE__ */ new Set(), r = (i) => {
    const o = /^\w*/.exec(i)[0];
    if (!(n.has(o) || !e[o])) {
      n.add(o);
      for (const s of e[o]) r(s.type);
    }
  };
  return r(t), n;
}
const Vg = [["token", "address"], ["amount", "uint160"], ["expiration", "uint48"], ["nonce", "uint48"]], qg = [["token", "address"], ["amount", "uint256"]], Y_ = { primaryType: "Permit", types: { Permit: [["owner", "address"], ["spender", "address"], ["value", "uint256"], ["nonce", "uint256"], ["deadline", "uint256"]] } }, X_ = { primaryType: "PermitSingle", types: { PermitSingle: [["details", "PermitDetails"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Vg } }, J_ = { primaryType: "PermitBatch", types: { PermitBatch: [["details", "PermitDetails[]"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Vg } }, Q_ = {
  primaryType: "PermitTransferFrom",
  types: { PermitTransferFrom: [["permitted", "TokenPermissions"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: qg }
}, e3 = {
  primaryType: "PermitBatchTransferFrom",
  types: { PermitBatchTransferFrom: [["permitted", "TokenPermissions[]"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: qg }
};
function vi(e, t, n) {
  if (t !== n.primaryType) return !1;
  const r = Z_(e, t), i = Object.keys(n.types);
  return r.size !== i.length || !i.every((o) => r.has(o)) ? !1 : i.every((o) => {
    const s = e[o], a = n.types[o];
    return Array.isArray(s) && s.length === a.length && s.every((c, l) => c.name === a[l][0] && c.type === a[l][1]);
  });
}
const Cf = (1n << 256n) - 1n, t3 = (1n << 160n) - 1n;
function ac(e, t, n, r, i) {
  const o = jg(t), s = o === n;
  return {
    field: {
      label: e,
      ...i === void 0 ? {} : { index: i },
      value: o === null ? Mn("string", t) : o.toString(),
      ...s ? { shownAs: r, notes: ["review.note.exactInDetails"] } : { notes: ["review.note.rawTokenUnits"] }
    },
    isMax: s
  };
}
function Yn(e) {
  return typeof e == "string" ? e : Mn("string", e);
}
function cc(e, t, n) {
  return { label: e, value: Mn("uint256", t), time: "unix-seconds", notes: [n] };
}
function Rf(e) {
  const t = e.filter((o) => o.isMax), n = e.filter((o) => !o.isMax), r = n.slice(0, Math.max(0, Dg - t.length)), i = n.length - r.length;
  return {
    rows: [
      ...[...t, ...r].flatMap((o) => o.rows()),
      ...i > 0 ? [{ label: "review.field.moreTokens", value: "", notes: [{ key: "review.note.moreEntriesInDetails", count: i }] }] : []
    ],
    maxima: t.length
  };
}
function Lf(e, t, n, r) {
  return t.maxima <= Dg ? [e, ...t.rows, r] : [
    e,
    { label: "review.field.maxEntries", value: String(t.maxima), notes: [{ key: "review.note.ofEntries", count: n }] },
    r,
    ...t.rows
  ];
}
function n3(e, t, n) {
  const r = ["review.warning.tokenAuthorization"];
  if (vi(e, t, Y_)) {
    const s = ac("review.field.permitAmount", n.value, Cf, "review.value.unlimited");
    return {
      kind: "review.kind.erc2612Permit",
      warnings: s.isMax ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: [
        { label: "review.field.spender", value: Yn(n.spender), elide: !0 },
        s.field,
        cc("review.field.permitDeadline", n.deadline, "review.note.permitDeadline")
      ],
      identity: [
        { label: "review.field.permitOwner", value: Yn(n.owner), elide: !0 },
        { label: "review.field.permitNonce", value: Mn("uint256", n.nonce) }
      ]
    };
  }
  const i = vi(e, t, X_);
  if (i || vi(e, t, J_)) {
    const s = i ? [n.details] : Array.isArray(n.details) ? n.details : [], a = Rf(s.map((c, l) => {
      const u = qs(c) ? c : {}, d = ac("review.field.permitTokenAmount", u.amount, t3, "review.value.unlimited", l + 1);
      return {
        isMax: d.isMax,
        rows: () => [
          d.field,
          { label: "review.field.permitToken", index: l + 1, value: Yn(u.token), elide: !0 },
          { label: "review.field.permitExpiration", index: l + 1, value: Mn("uint48", u.expiration), notes: ["review.note.permit2Expiration"] }
        ]
      };
    }));
    return {
      kind: "review.kind.permit2Allowance",
      warnings: a.maxima > 0 ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: Lf(
        { label: "review.field.spender", value: Yn(n.spender), elide: !0 },
        a,
        s.length,
        cc("review.field.signatureDeadline", n.sigDeadline, "review.note.signatureDeadline")
      ),
      identity: []
    };
  }
  const o = vi(e, t, Q_);
  if (o || vi(e, t, e3)) {
    const s = o ? [n.permitted] : Array.isArray(n.permitted) ? n.permitted : [], a = Rf(s.map((c, l) => {
      const u = qs(c) ? c : {}, d = ac("review.field.permitTokenAmount", u.amount, Cf, "review.value.maximumOneTime", l + 1);
      return {
        isMax: d.isMax,
        rows: () => [d.field, { label: "review.field.permitToken", index: l + 1, value: Yn(u.token), elide: !0 }]
      };
    }));
    return {
      kind: "review.kind.permit2Transfer",
      warnings: a.maxima > 0 ? ["review.warning.oneTimeTransfer", "review.warning.maximumOneTimeTransfer"] : ["review.warning.oneTimeTransfer"],
      summary: Lf(
        // The Permit2 contract signs msg.sender as the spender; this is the value in the message.
        { label: "review.field.spender", value: Yn(n.spender), elide: !0 },
        a,
        s.length,
        cc("review.field.permitDeadline", n.deadline, "review.note.signatureDeadline")
      ),
      identity: [{ label: "review.field.permitNonce", value: Mn("uint256", n.nonce) }]
    };
  }
  return null;
}
function Wg(e) {
  return e.length <= sc ? { name: e } : { name: `${e.slice(0, sc / 2)} … ${e.slice(-sc / 4)}`, notes: ["review.note.pathShortened"] };
}
function lc(e) {
  const { name: t, notes: n } = Wg(`${e.path} (${e.type})`);
  return { label: "review.field.typedField", name: t, value: e.value, ...n ? { notes: n } : {} };
}
function r3(e, t) {
  const n = e, r = qs(n.domain) ? n.domain : {}, i = n.types, o = {
    EIP712Domain: yg({ domain: r }),
    ...i
  }, s = ha(t.publicKey).eth;
  if (!s) throw new Error("EIP-712 review requires a secp256k1 signing key");
  const a = { covered: [], uncovered: [], omitted: 0 };
  Ws(o, "EIP712Domain", r, "domain", 0, a);
  const c = a.covered, l = a.uncovered, u = new Set(o.EIP712Domain.map((H) => H.name)), d = (H, O) => {
    if (!Object.hasOwn(r, H)) return [];
    const I = u.has(H), U = o.EIP712Domain.find((E) => E.name === H), z = I && U ? Mn(U.type, r[H]) : Yn(r[H]);
    return [{
      label: O,
      value: H === "chainId" ? Fg(z) : z,
      notes: [...H === "chainId" ? Kg(z) : [], ...I ? [] : ["review.note.notInTypedHash"]]
    }];
  }, f = n.primaryType !== "EIP712Domain", h = qs(n.message) ? n.message : {}, p = { covered: [], uncovered: l, omitted: 0 };
  f ? Ws(o, n.primaryType, n.message, "", 0, p) : n.message !== void 0 && l.push("message");
  const y = p.covered, w = a.omitted + p.omitted;
  let m = null;
  try {
    m = f ? n3(o, n.primaryType, h) : null;
  } catch {
    m = null;
  }
  const g = y.slice(0, $f).map((H) => H.type === "address" ? { ...lc(H), elide: !0 } : lc(H)), v = Math.max(0, y.length - $f) + w;
  v > 0 && g.push({ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: v }] });
  const L = (H, O = 0) => {
    const I = Math.max(0, H.length - bi) + O;
    return [
      ...H.slice(0, bi).map(lc),
      ...I > 0 ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: I }] }] : []
    ];
  }, T = JSON.stringify(n, null, 2), C = [
    { title: "review.group.typedDomain", fields: L(c, a.omitted) },
    ...f ? [{ title: "review.group.typedMessage", fields: L(y, p.omitted) }] : [],
    ...l.length > 0 ? [{
      title: "review.group.notInTypedHash",
      fields: [
        ...l.slice(0, bi).map((H) => {
          const O = Wg(H);
          return { label: "review.field.typedField", name: O.name, value: "", notes: ["review.note.notInTypedHash", ...O.notes ?? []] };
        }),
        ...l.length > bi ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: l.length - bi }] }] : []
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
        { label: "review.field.typedData", value: T, notes: [{ key: "review.note.bytes", count: new TextEncoder().encode(T).length }], raw: !0 }
      ]
    }
  ];
  return {
    kind: m?.kind ?? "review.kind.eip712",
    signer: { label: "review.field.signingAddress", value: s, elide: !0 },
    // What the signature grants first, then where it applies, then who; the signer last.
    summary: [
      ...m ? m.summary : g,
      ...l.length > 0 ? [{ label: "review.field.notInTypedHash", value: "", notes: [{ key: "review.note.fieldsOutsideHash", count: l.length }] }] : [],
      ...d("chainId", "review.field.chainId"),
      ...d("name", "review.field.domainName"),
      ...d("verifyingContract", "review.field.verifyingContract").map((H) => ({ ...H, elide: !0 })),
      { label: "review.field.primaryType", value: n.primaryType, ...f ? {} : { notes: ["review.note.messageNotHashed"] } },
      ...m ? m.identity : []
    ],
    // Every hashed value is shown; nothing is blind.
    undecoded: !1,
    ...m ? { warnings: m.warnings } : {},
    details: C
  };
}
const Hf = 3, Nf = 3, i3 = 3;
function Gg(e) {
  return e === Hg ? ["review.note.systemProgram"] : e === Ng ? ["review.note.tokenProgram"] : [];
}
function io(e) {
  return e.length / 2;
}
function Zg(e) {
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
function o3(e) {
  return e.kind === "spl-transfer-checked" ? "TransferChecked" : "Transfer";
}
function Uf(e) {
  return {
    label: "review.field.undecodedInstruction",
    value: e.programId,
    notes: [{ key: "review.note.dataBytes", count: io(e.dataHex) }, "review.note.notDecoded"]
  };
}
function s3(e, t) {
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
function a3(e) {
  const t = Zg(e).map((r) => r.label === "review.field.amount" ? r : { ...r, elide: !0 }), n = ["review.field.amount", "review.field.to", "review.field.toTokenAccount", "review.field.mint", "review.field.from", "review.field.fromTokenAccount"];
  return [...t].sort((r, i) => n.indexOf(r.label) - n.indexOf(i.label));
}
const Gs = (e) => e.kind !== "system-advance-nonce";
function c3(e) {
  if (e.instructions.length === 1) {
    const [s] = e.instructions;
    return s.decoded ? Gs(s.decoded) ? a3(s.decoded) : Df(1) : [{ ...Uf(s), elide: !0 }];
  }
  let t = 0, n = 0, r = 0;
  const i = e.instructions.flatMap((s) => {
    const a = s.decoded;
    return a && Gs(a) ? (t += 1, t <= Hf ? s3(a, t) : []) : a ? (n += 1, []) : (r += 1, r <= Nf ? [{ ...Uf(s), elide: !0 }] : []);
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
function l3(e) {
  return Gs(e) ? [
    { label: "review.field.instruction", value: o3(e) },
    ...Zg(e),
    ...e.kind === "system-transfer" ? [] : [{ label: "review.field.authority", value: e.authority }]
  ] : [
    { label: "review.field.instruction", value: "AdvanceNonceAccount" },
    { label: "review.field.nonceAccount", value: e.nonceAccount },
    { label: "review.field.recentBlockhashesSysvar", value: e.recentBlockhashesSysvar },
    { label: "review.field.nonceAuthority", value: e.nonceAuthority }
  ];
}
function d3(e) {
  return e.map((t) => t.index).join(", ");
}
function u3(e) {
  const t = e.decoded, n = {
    label: "review.field.data",
    value: e.dataHex,
    notes: [{ key: "review.note.bytes", count: io(e.dataHex) }, ...t ? [] : ["review.note.notDecoded"]]
  };
  return {
    title: "review.group.instruction",
    index: e.index + 1,
    fields: [
      { label: "review.field.program", value: e.programId, notes: Gg(e.programId) },
      ...t ? l3(t) : [],
      { label: "review.field.accountIndexes", value: d3(e.accounts) },
      n
    ]
  };
}
function f3(e) {
  const t = e.instructions.length === 1 ? e.instructions[0].decoded : null;
  return {
    kind: !t || !Gs(t) ? "review.kind.solanaTransaction" : t.kind === "system-transfer" ? "review.kind.solanaSystemTransfer" : "review.kind.solanaTokenTransfer",
    signer: { label: "review.field.signingKey", value: e.expectedSigner.address, elide: !0 },
    summary: c3(e),
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
            ...Gg(n.address)
          ]
        }))
      },
      ...e.instructions.map(u3),
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
function qc(e) {
  return Mg.has(e.chainId) ? { label: "review.field.value", value: e.value, native: "eth" } : { label: "review.field.value", value: e.value, notes: ["review.note.wei"] };
}
function Yg(e) {
  return e.to === null ? { label: "review.field.contract", value: "", notes: ["review.note.contractCreation"] } : { label: "review.field.contract", value: e.to };
}
function Xg(e) {
  return (e.decoded?.arguments ?? []).map((t, n) => ({
    label: "review.field.argument",
    index: n + 1,
    name: `${t.name} (${t.type})`,
    value: t.value
  }));
}
function h3(e) {
  if (e.decoded) {
    const n = { label: "review.field.call", value: e.decoded.signature }, r = Xg(e).map((i) => i.name?.endsWith(" (address)") ? { ...i, elide: !0 } : i);
    return r.length <= i3 ? [n, ...r] : [{ ...n, notes: [{ key: "review.note.argumentsInDetails", count: r.length }] }];
  }
  if (e.calldata === "0x") return [];
  const t = [{ key: "review.note.dataBytes", count: e.calldataBytes }, "review.note.notDecoded"];
  return e.to === null || e.selector === null ? [{ label: "review.field.initCode", value: "", notes: t }] : [{ label: "review.field.selector", value: e.selector, notes: t }];
}
const zf = ((1n << 256n) - 1n).toString();
function p3(e) {
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
function w3(e) {
  const t = { label: "review.field.chainId", value: Fg(e.chainId), notes: Kg(e.chainId) };
  if (e.to !== null && e.calldata === "0x")
    return { kind: "review.kind.evmTransaction", summary: [Mg.has(e.chainId) ? { label: "review.field.nativeTransfer", value: e.value, native: "eth", unit: "symbol", to: e.to } : { label: "review.field.nativeTransfer", value: e.value, notes: ["review.note.wei"], to: e.to }, t] };
  const n = p3(e);
  return n ? {
    kind: n.kind,
    ...n.warnings ? { warnings: n.warnings } : {},
    // The native value the transaction carries, as encoded, never assumed to be zero.
    summary: [...n.lead, { label: "review.field.tokenContract", value: e.to, elide: !0 }, qc(e), t]
  } : { kind: "review.kind.evmTransaction", summary: [{ ...Yg(e), elide: !0 }, qc(e), ...h3(e), t] };
}
function y3(e, t) {
  const n = ha(t.publicKey).eth;
  if (!n) throw new Error("EVM review requires a secp256k1 signing key");
  const r = (a, c) => c === void 0 ? [] : [{ label: a, value: c, notes: ["review.note.wei"] }], { kind: i, summary: o, warnings: s } = w3(e);
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
          Yg(e),
          qc(e),
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
          ...Xg(e)
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
            notes: [{ key: "review.note.bytes", count: io(e.unsignedTransactionHex) - 1 }],
            raw: !0
          },
          { label: "review.field.digest", value: `0x${t.digest}` }
        ]
      }
    ]
  };
}
function Jg(e, t) {
  return [...e.filter((n) => !t(n)), ...e.filter(t)];
}
function g3(e, t) {
  const n = (s) => s.toString(), r = { label: "review.field.network", value: e.network, notes: ["review.note.requesterNetwork"] }, i = Jg(e.outputs, (s) => s.sameKey).map((s) => s.sameKey ? { label: "review.field.returnToKey", value: n(s.amountSats), native: "btc", unit: "symbol" } : { label: "review.field.send", value: n(s.amountSats), native: "btc", unit: "symbol", to: s.address }), o = e.outputs.filter((s) => !s.sameKey).map((s) => ({ label: "review.field.recipientAddress", value: s.address }));
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
            notes: [{ key: "review.note.bytes", count: io(e.unsignedTransaction) }],
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
function m3(e, t) {
  const n = (f) => f.toString(), r = t.boundary.kind === "account", i = e.addressType === "p2pkh", o = `${e.rate.lower} – ${e.rate.upper} sat/vB`, s = r ? "review.field.returnToAccount" : "review.field.returnToKey", a = r ? "review.field.netLeavingAccount" : "review.field.netLeaving", c = r ? "review.note.sameAccountReturn" : "review.note.sameKeyReturn", l = Jg(e.outputs, (f) => f.returned).map((f) => f.returned ? { label: s, value: n(f.amountSats), native: "btc", unit: "symbol" } : { label: "review.field.send", value: n(f.amountSats), native: "btc", unit: "symbol", to: f.address }), u = e.outputs.filter((f) => !f.returned).map((f) => ({ label: "review.field.recipientAddress", value: f.address })), d = i ? ["review.note.legacyAmountFromParent", "review.note.legacyNoAmountField"] : ["review.note.nativeAmountSupplied", "review.note.nativeFormOnly"];
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
      ...u,
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
          { label: "review.field.inputAmount", value: n(e.inputAmountSats), native: "btc", notes: d },
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
            notes: [{ key: "review.note.bytes", count: io(e.unsignedTransaction) }],
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
function _d(e, t) {
  try {
    const n = Aw(e.context.release?.derivation), r = t.accounts[n.account], i = n.chain === "bitcoin" ? n.address_type : void 0, o = i === "p2pkh" ? r.bitcoin_p2pkh : i === "p2wpkh" ? r.bitcoin_p2wpkh : r[n.chain], s = e.signingContext.kind, a = i === "p2tr" ? e.scheme === "schnorr-secp256k1-bip341-keypath" && s === "bitcoin-taproot-keypath-v1" : e.scheme === "ecdsa-secp256k1-der" && s === (i === "p2pkh" ? "bitcoin-p2pkh-v1" : "bitcoin-p2wpkh-v1");
    if (n.chain === "bitcoin" ? !a : n.chain === "solana" ? e.scheme !== "ed25519" || s !== "solana-transaction-v1" : e.scheme !== "ecdsa-secp256k1-recoverable" || s !== "evm-transaction" && s !== "eip-712-typed-data") throw new Error("protected HD chain/scheme/context mismatch");
    const c = e.context.release?.signingKey;
    if (!c || typeof c != "object" || Array.isArray(c)) throw new Error("missing protected HD signing key binding");
    const l = c;
    if (Object.keys(l).length !== 3 || typeof l.secret_id != "string" || l.secret_id.length === 0 || l.public_key !== o || e.context.release?.publicKey !== o || typeof l.envelope_sha256 != "string" || !/^[0-9a-f]{64}$/.test(l.envelope_sha256) || l.envelope_sha256 !== Tw(e.material.envelope)) throw new Error("protected HD signing identity mismatch");
    return {
      selection: n,
      publicKey: o,
      path: xs(n.chain, n.account, i),
      ...i && i !== "p2tr" ? { bitcoinReturnKeys: { p2pkh: r.bitcoin_p2pkh, p2wpkh: r.bitcoin_p2wpkh, p2tr: r.bitcoin } } : {}
    };
  } catch {
    throw new S("invalid_sign_binding", "protected HD signing requires the exact signed selection/key/envelope binding");
  }
}
async function Id(e, t) {
  if (e.context.kind !== "hd-seed") throw new Error("protected HD custody requires HD metadata");
  const n = await li(e.sealed, e.vmk, e.context);
  let r;
  try {
    return r = Sw(n), Du(r.seed, e.identity.selection, (i) => {
      const o = G(e.identity.selection.chain === "solana" ? kt.getPublicKey(i) : fe.getPublicKey(i, !0));
      if (o !== e.identity.publicKey) throw new S("invalid_sign_binding", "protected HD seed does not match the selected authenticated public key");
      const s = e.identity.selection, a = e.identity.bitcoinReturnKeys;
      if (s.chain === "bitcoin" && s.address_type !== "p2tr") {
        if (!a || a[s.address_type] !== o) throw new S("invalid_sign_binding", "protected HD Bitcoin return identity is missing");
        for (const c of ["p2pkh", "p2wpkh", "p2tr"])
          c !== s.address_type && Du(r.seed, { chain: "bitcoin", account: s.account, address_type: c }, (l) => {
            if (G(fe.getPublicKey(l, !0)) !== a[c]) throw new S("invalid_sign_binding", "protected HD seed does not match an authenticated Bitcoin sibling point");
          });
      }
      return t(i);
    });
  } finally {
    n.fill(0), ks(r);
  }
}
async function b3(e) {
  return Id(e, (t) => e.payload.kind === "message" ? Dw(t, e.payload.messageHex) : jl(t, e.payload.digest, e.request.scheme));
}
const v3 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function Ii(e) {
  let t = 0;
  for (; t < e.length && e[t] === 0; ) t += 1;
  let n = 0n;
  for (const i of e) n = n << 8n | BigInt(i);
  let r = "";
  for (; n > 0n; )
    r = v3[Number(n % 58n)] + r, n /= 58n;
  return "1".repeat(t) + r;
}
class Qg extends Error {
  code;
  constructor(t, n) {
    super(n), this.name = "SolanaMessageError", this.code = t;
  }
}
const Do = 1232, Kf = 64, xi = 32, x3 = 32, Ff = 128, jf = 256;
function Pe(e, t) {
  throw new Qg(e, t);
}
function Vf(e) {
  let t = "";
  for (const n of e) t += n.toString(16).padStart(2, "0");
  return t;
}
function k3(e) {
  return e < 128 ? 1 : e < 16384 ? 2 : 3;
}
function E3(e, t, n = "compact-u16") {
  let r = 0;
  for (let i = 0; ; i += 1) {
    t + i >= e.length && Pe("truncated", `message ends inside ${n}`);
    const o = e[t + i];
    o === 0 && i !== 0 && Pe("invalid_compact_u16", `${n} is not minimally encoded`);
    const s = (o & 128) === 0;
    if (i === 2 && !s && Pe("invalid_compact_u16", `${n} is longer than three bytes`), r |= (o & 127) << 7 * i, r > 65535 && Pe("invalid_compact_u16", `${n} exceeds 65535`), s) return { value: r, length: i + 1 };
  }
}
class A3 {
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
    const { value: n, length: r } = E3(this.bytes, this.offset, t);
    return this.offset += r, n;
  }
}
function em(e) {
  if (typeof e == "object" && e !== null) {
    for (const t of Object.values(e)) em(t);
    Object.freeze(e);
  }
  return e;
}
function S3(e, t) {
  e instanceof Uint8Array || Pe("invalid_input", "message must be a byte array"), (!(t instanceof Uint8Array) || t.length !== xi) && Pe("invalid_input", "expected public key must be 32 bytes"), e.length > Do - 1 - Kf && Pe("packet_too_large", `message of ${e.length} bytes cannot fit a ${Do}-byte transaction`);
  const n = new Uint8Array(e), r = Ii(new Uint8Array(t)), i = new A3(n), o = i.u8("message prefix");
  let s = "legacy", a = o;
  if ((o & Ff) !== 0) {
    const I = o & ~Ff;
    I !== 0 && Pe("unsupported_version", `Solana message version ${I} is not supported`), s = "v0", a = i.u8("required signature count");
  }
  const c = i.u8("read-only signed account count"), l = i.u8("read-only unsigned account count"), u = i.compactU16("account key count"), d = i.take(u * xi, "account keys"), f = Array.from({ length: u }, (I, U) => Ii(d.subarray(U * xi, (U + 1) * xi))), h = Ii(i.take(x3, "recent blockhash")), p = [];
  for (let I = i.compactU16("instruction count"); I > 0; I -= 1) {
    const U = i.u8("instruction program id index"), z = i.take(i.compactU16("instruction account count"), "instruction account indexes"), E = i.take(i.compactU16("instruction data length"), "instruction data");
    p.push({ programIdIndex: U, accountIndexes: z, data: E });
  }
  const y = [];
  if (s === "v0")
    for (let I = i.compactU16("address table lookup count"); I > 0; I -= 1) {
      const U = i.take(xi, "address table key"), z = i.take(i.compactU16("writable lookup index count"), "writable lookup indexes"), E = i.take(i.compactU16("read-only lookup index count"), "read-only lookup indexes");
      y.push({ accountKey: U, writableIndexes: z, readonlyIndexes: E });
    }
  i.remaining !== 0 && Pe("trailing_bytes", `${i.remaining} bytes follow the message`), a + l > u && Pe("invalid_header", "header counts more signed and read-only unsigned accounts than the message carries"), c >= a && Pe("invalid_header", "header leaves no writable signer to pay the fee");
  let w = 0, m = 0;
  for (const I of y)
    I.writableIndexes.length + I.readonlyIndexes.length === 0 && Pe("invalid_address_table_lookup", "an address table lookup loads no account"), w += I.writableIndexes.length, m += I.readonlyIndexes.length;
  const g = u + w + m;
  g > jf && Pe("invalid_address_table_lookup", `message addresses ${g} accounts; the limit is ${jf}`);
  for (const I of p) {
    (I.programIdIndex === 0 || I.programIdIndex >= u) && Pe("invalid_program_id_index", `instruction program id index ${I.programIdIndex} is not a static non-payer key`);
    for (const U of I.accountIndexes)
      U >= g && Pe("invalid_account_index", `instruction account index ${U} is outside the message's ${g} accounts`);
  }
  const v = k3(a) + a * Kf + n.length;
  v > Do && Pe("packet_too_large", `signed transaction would be ${v} bytes; the limit is ${Do}`);
  const L = f.indexOf(r);
  (L === -1 || L >= a) && Pe("signer_not_required", "the expected key is not a required signer of this message");
  const T = f.map((I, U) => ({
    index: U,
    address: I,
    signer: U < a,
    writable: U < a ? U < a - c : U < u - l
  })), C = y.map((I) => ({
    accountKey: Ii(I.accountKey),
    writableIndexes: Array.from(I.writableIndexes),
    readonlyIndexes: Array.from(I.readonlyIndexes)
  })), H = (I) => {
    if (I < u) return { source: "static", ...T[I] };
    const U = I - u < w;
    let z = I - u - (U ? 0 : w);
    for (const [E, k] of C.entries()) {
      const x = U ? k.writableIndexes : k.readonlyIndexes;
      if (z < x.length)
        return { source: "lookup", index: I, lookupIndex: E, accountKey: k.accountKey, tableIndex: x[z], writable: U };
      z -= x.length;
    }
    throw new Error("account index was validated against the lookup tables");
  }, O = p.map((I, U) => {
    const z = f[I.programIdIndex], E = Array.from(I.accountIndexes, H);
    return {
      index: U,
      programIdIndex: I.programIdIndex,
      programId: z,
      accounts: E,
      dataHex: Vf(I.data),
      ...W_(z, E, I.data)
    };
  });
  return em({
    version: s,
    messageHex: Vf(n),
    messageLength: n.length,
    transactionLength: v,
    header: { numRequiredSignatures: a, numReadonlySignedAccounts: c, numReadonlyUnsignedAccounts: l },
    feePayer: f[0],
    expectedSigner: { index: L, address: r, isFeePayer: L === 0 },
    staticAccountKeys: T,
    recentBlockhash: h,
    instructions: O,
    addressTableLookups: C,
    hasUndecodedContent: C.length > 0 || O.some((I) => I.decoded === null)
  });
}
function Rn(e, t) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new S("invalid_payload", `${t} must be an object`);
  return e;
}
function zo(e, t) {
  if (typeof e != "string" || e.length === 0) throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function ar(e, t, n) {
  if (Object.keys(e).some((r) => !t.includes(r)))
    throw new S("invalid_payload", `${n} carries unsupported fields; script trees are not supported`);
}
function Bd(e) {
  if (e && typeof e == "object") {
    for (const t of Object.values(e)) Bd(t);
    Object.freeze(e);
  }
  return e;
}
function Td(e, t) {
  if (Jo(e), t && typeof t == "object") {
    const n = t.release;
    Jo(n), n && typeof n == "object" && Jo(n.signingContext);
  }
}
function tm(e) {
  const t = Rn(e, "sign request");
  I_(t), Td(t.signingContext, t.context), gr(t);
  const n = Rn(structuredClone(t), "sign request");
  ar(n, ["material", "scheme", "signingContext", "context"], "sign request");
  const r = Rn(n.material, "material");
  ar(r, ["name", "envelope"], "material");
  const i = Rn(r.envelope, "envelope");
  ar(i, ["ciphertext", "nonce", "wrap_meta"], "envelope");
  const o = id(n.context);
  ar(Rn(o, "context"), ["v", "purpose", "requestId", "grantId", "display", "release", "agent", "expiresAt", "signature"], "sign context");
  let s;
  try {
    s = Hl(n.scheme);
  } catch {
    throw new S("invalid_payload", "unsupported signing scheme");
  }
  const a = Rn(n.signingContext, "signingContext");
  return a.kind === "solana-transaction-v1" && Aa(a), Bd({
    material: {
      name: zo(r.name, "material.name"),
      envelope: {
        ciphertext: zo(i.ciphertext, "envelope.ciphertext"),
        nonce: zo(i.nonce, "envelope.nonce"),
        wrap_meta: zo(i.wrap_meta, "envelope.wrap_meta")
      }
    },
    scheme: s,
    signingContext: a,
    context: o
  });
}
function De() {
  throw new S("invalid_sign_binding", "signing approval is missing or does not match its signed binding; update Avibe and recreate the request");
}
function Wc(e, t, n) {
  Td(e.signingContext, e.context), od({ context: e.context, expectedPurpose: "sign", secret: { name: e.material.name, kind: "keypair" } });
  const r = e.context.release;
  (!r || Array.isArray(r)) && De();
  const i = t.kind === "message";
  !i && (t.kind !== "digest" || !/^[0-9a-f]{64}$/.test(t.digest)) && De();
  const o = Vn(e.material.envelope).recordMetadata, s = o?.kind === "hd-seed" ? _d(e, o) : void 0;
  s && s.publicKey !== n && De();
  const a = Wn(e.signingContext);
  if (ar(r, ["name", "scheme", i ? "messageHex" : "digest", "publicKey", "signingContext", ...s || a ? ["signingKey"] : [], ...s ? ["derivation"] : [], ...a ? ["sandboxCohort"] : []], "sign context.release"), a && Wn(e.signingContext)) {
    const c = s ? Qo.hdInputTypes : Qo.ordinaryInputTypes;
    ["p2pkh", "p2wpkh", "p2tr"].every((f) => c.includes(f) && Qo.ecdsaReturnTypes.includes(f)) || De();
    const l = Rn(r.signingKey, "sign context.release.signingKey");
    ar(l, ["secret_id", "public_key", "envelope_sha256"], "sign context.release.signingKey");
    const u = Rn(r.sandboxCohort, "sign context.release.sandboxCohort");
    ar(u, ["buildHash", "manifestHash"], "sign context.release.sandboxCohort"), (Object.keys(l).length !== 3 || typeof l.secret_id != "string" || l.secret_id.length === 0 || !/^(02|03)[0-9a-f]{64}$/.test(n) || l.public_key !== n || r.publicKey !== n || l.envelope_sha256 !== Tw(e.material.envelope) || Object.keys(u).length !== 2 || u.buildHash !== he.buildHash || typeof u.manifestHash != "string" || !/^sha256-[A-Za-z0-9+/]{43}=$/.test(u.manifestHash)) && De();
    const d = u.manifestHash.slice(7);
    ce(ve(d)) !== d && De(), s && (s.selection.chain !== "bitcoin" || s.selection.address_type !== e.signingContext.addressType) && De();
  }
  if (i) {
    const c = Aa(e.signingContext);
    (t.messageHex !== c.messageHex || r.messageHex !== t.messageHex || e.scheme !== "ed25519" || !/^[0-9a-f]{64}$/.test(n) || r.publicKey !== n) && De();
  } else (e.scheme === "ed25519" || e.signingContext.kind === "solana-transaction-v1" || r.digest !== t.digest) && De();
  (r.name !== e.material.name || r.scheme !== e.scheme || typeof r.publicKey != "string" || r.publicKey.toLowerCase() !== n.toLowerCase() || !r.signingContext || ye(r.signingContext) !== ye(e.signingContext)) && De();
}
function _3(e) {
  return Td(e.signingContext, e.context), ue(new TextEncoder().encode(ba(e.context)));
}
function qf(e, t) {
  if (Hl(e), e === "ed25519") throw new Error("Ed25519 exact-message signing is reviewed structurally and has no prose description");
  if (!/^(02|03)[0-9a-fA-F]{64}$/.test(t)) throw new Error("signing public key must be compressed secp256k1");
  return e === Ll ? `BIP340 raw Schnorr (untweaked internal key)
internal key: ${t.slice(2).toLowerCase()}` : e === Ui ? Y("bitcoin.signingIdentity", { key: t.slice(2).toLowerCase() }) : `${e === ca ? "ECDSA secp256k1 recoverable" : "ECDSA secp256k1 DER"}
public key: ${t.toLowerCase()}`;
}
function I3(e, t) {
  const n = Aa(e.signingContext);
  Wc(e, { kind: "message", messageHex: n.messageHex }, t);
  let r;
  try {
    r = S3(ml(n.messageHex), ut(t));
  } catch (i) {
    throw i instanceof Qg ? new S("invalid_payload", `Solana message cannot be signed (${i.code}): ${i.message}`) : i;
  }
  return r.messageHex !== n.messageHex && De(), { payload: { kind: "message", messageHex: r.messageHex }, review: f3(r) };
}
function B3(e, t, n) {
  const r = e.signingContext;
  if (r.kind === "solana-transaction-v1" && De(), Wn(r)) {
    if (e.scheme !== Ss) throw new S("invalid_payload", "Legacy/Native Bitcoin contexts require the DER ECDSA scheme");
    const s = Lg(r, r.digest, t, n?.bitcoinReturnKeys);
    return Wc(e, { kind: "digest", digest: s.digest }, t), {
      payload: { kind: "digest", digest: s.digest },
      display: qf(e.scheme, t),
      review: m3(s.facts, {
        scheme: e.scheme,
        digest: s.digest,
        publicKey: t,
        boundary: n ? { kind: "account", index: n.selection.account, path: n.path } : { kind: "key" }
      })
    };
  }
  let i = x_(r);
  if (r.kind === "bitcoin-taproot-keypath-v1") {
    if (e.scheme !== Ui)
      throw new S("invalid_payload", "Bitcoin Taproot contexts require the BIP341 key-path signing scheme");
  } else {
    if (e.scheme === Ui)
      throw new S("invalid_payload", "the BIP341 key-path signing scheme requires a Bitcoin Taproot context");
    if (r.kind !== "avault-agent-operation" && e.scheme !== ca && e.scheme !== Ss)
      throw new S("invalid_payload", "EVM signing contexts require an ECDSA scheme");
  }
  let o;
  if (r.kind === "bitcoin-taproot-keypath-v1") {
    const s = Bg(r, i.digest, t);
    i = { digest: s.digest, challenge: i.challenge, display: s.display }, o = s.facts;
  }
  return Wc(e, { kind: "digest", digest: i.digest }, t), {
    payload: { kind: "digest", digest: i.digest },
    display: `${qf(e.scheme, t)}
scheme: ${e.scheme}
${i.display}`,
    ...i.evm ? { review: y3(i.evm, { publicKey: t, scheme: e.scheme, digest: i.digest }) } : {},
    ...o ? { review: g3(o, { scheme: e.scheme, digest: i.digest }) } : {},
    ...T3(i.typedData, { publicKey: t, scheme: e.scheme, digest: i.digest })
  };
}
function T3(e, t) {
  if (e === void 0) return {};
  try {
    const n = r3(e, t);
    return hy(JSON.parse(JSON.stringify(n))), { review: n };
  } catch {
    return {};
  }
}
function nm(e) {
  const t = Vn(e.material.envelope), n = fa(e.material.name, t.recordMetadata), r = t.recordMetadata?.kind === "hd-seed" ? _d(e, t.recordMetadata) : void 0, i = r ? {
    name: e.material.name,
    kind: "keypair",
    publicKey: r.publicKey,
    ...r.selection.chain === "solana" ? { curve: "ed25519" } : {}
  } : n;
  k_(e.scheme, e.signingContext, i);
  const o = i.publicKey, s = e.signingContext.kind === "solana-transaction-v1" ? I3(e, o) : B3(e, o, r);
  if (r && !Wn(e.signingContext)) {
    const a = `#${r.selection.account} · ${r.path}${r.selection.chain === "bitcoin" ? " · Taproot" : ""}`;
    if (s.review) {
      const c = [{ label: "review.field.walletAccount", value: a }];
      s.review = { ...s.review, summary: [...s.review.summary, ...c], details: [{ title: "review.group.hdAccount", fields: c }, ...s.review.details] };
    } else if (typeof s.display == "string") {
      const c = ha(r.publicKey).eth;
      (!c || r.selection.chain !== "evm") && De(), s.display = `${Y("review.field.walletAccount")}: ${a}
${Y("review.field.signingAddress")}: ${c}

${s.display}`;
    }
  }
  return { unpacked: t, recordContext: n, content: s, ...r ? { hd: r } : {} };
}
function P3(e) {
  const t = tm(e), { unpacked: n, recordContext: r, content: i } = nm(t), o = _3(t);
  return Bd({ request: t, verified: i, unpacked: n, recordContext: r, challenge: [...o], ...n.recordMetadata?.kind === "hd-seed" ? { hd: _d(t, n.recordMetadata) } : {} });
}
async function rm(e, t, n, r = !0) {
  const i = tm(e.request), o = nm(i);
  (!e.verified || ye(e.verified.payload) !== ye(o.content.payload)) && De();
  const s = await Ts(n, t);
  return Ks({ context: i.context, rootMetadata: s, expectedPurpose: "sign" }), ti([i.context]), r && (o.hd ? await Id({ sealed: o.unpacked.sealed, vmk: t, context: o.recordContext, identity: o.hd }, () => {
  }) : Wn(i.signingContext) && await im(o, t, () => {
  })), { request: i, ...o };
}
async function Wf(e, t, n) {
  return (await rm(e, t, n)).content;
}
async function im(e, t, n) {
  const { recordContext: r } = e;
  (r.kind !== "keypair" || !r.publicKey || r.curve === "ed25519") && De();
  const i = await li(e.unpacked.sealed, t, r);
  try {
    return G(fe.getPublicKey(i, !0)) !== r.publicKey && De(), n(i);
  } finally {
    i.fill(0);
  }
}
async function Gf(e, t, n, r) {
  const i = await rm(e, t, n, !Wn(e.request.signingContext)), { request: o, content: s, unpacked: a, recordContext: c, hd: l } = i;
  if (Wn(o.signingContext)) {
    if (!r || s.payload.kind !== "digest") throw new S("invalid_sign_binding", "new Bitcoin signing requires the current scoped authorization lease");
    const u = l?.publicKey ?? c.publicKey, d = s.payload.digest, f = (h) => {
      r(), ti([o.context]);
      const p = jl(h, d, o.scheme);
      return H_(o.signingContext, d, u, p.signature, l?.bitcoinReturnKeys), r(), p;
    };
    return l ? Id({ sealed: a.sealed, vmk: t, context: c, identity: l }, f) : im(i, t, f);
  }
  return l ? b3({ request: o, sealed: a.sealed, vmk: t, context: c, identity: l, payload: s.payload }) : s.payload.kind === "message" ? E2(a.sealed, t, c, s.payload.messageHex) : k2(a.sealed, t, c, s.payload.digest, o.scheme);
}
function om(e, t = e.label) {
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
function $3(e) {
  const { challenge: t, ...n } = e;
  return {
    ...n,
    ...t ? { challenge: ce(t) } : {}
  };
}
function Gc(e, t) {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be an object`);
  return e;
}
function cr(e, t) {
  if (typeof e == "string" && e.length > 0) return e;
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be a text spec`);
  const n = e;
  if (typeof n.text == "string") return me(n.text);
  if (typeof n.key == "string") {
    const r = n.params;
    if (r !== void 0 && (typeof r != "object" || r === null || Array.isArray(r)))
      throw new S("invalid_payload", `${t}.params must be an object`);
    return {
      key: n.key,
      ...r !== void 0 ? { params: r } : {}
    };
  }
  throw new S("invalid_payload", `${t} must be a text spec`);
}
function O3(e) {
  const t = Gc(e, "details");
  if (!Array.isArray(t.rows)) throw new S("invalid_payload", "details must contain rows");
  const n = [];
  for (const [r, i] of t.rows.entries()) {
    const o = Gc(i, `details.rows[${r}]`), s = o.variant;
    if (s !== void 0 && s !== "text" && s !== "command" && s !== "mono" && s !== "chips")
      throw new S("invalid_payload", `details.rows[${r}].variant is invalid`);
    const a = cr(o.label, `details.rows[${r}].label`);
    if (s === "chips") {
      if (!Array.isArray(o.values)) throw new S("invalid_payload", `details.rows[${r}].values must be an array`);
      const c = o.tone;
      if (c !== void 0 && c !== "default" && c !== "warning")
        throw new S("invalid_payload", `details.rows[${r}].tone is invalid`);
      const l = c === "default" || c === "warning" ? c : void 0;
      n.push({
        label: a,
        values: o.values.map((u, d) => cr(u, `details.rows[${r}].values[${d}]`)),
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
function C3(e) {
  try {
    return hy(e);
  } catch (t) {
    throw new S("invalid_payload", t instanceof Error ? t.message : "signing review is malformed");
  }
}
function R3(e) {
  const t = Gc(e, "authorization prompt");
  if (t.challenge !== void 0 && (typeof t.challenge != "string" || t.challenge.length === 0))
    throw new S("invalid_payload", "prompt.challenge must be a non-empty string");
  if (t.reviewSession !== void 0 && typeof t.reviewSession != "string")
    throw new S("invalid_payload", "prompt.reviewSession must be a string");
  const n = t.challenge === void 0 ? void 0 : ve(t.challenge);
  return {
    title: cr(t.title, "prompt.title"),
    subtitle: cr(t.subtitle, "prompt.subtitle"),
    ...t.body !== void 0 ? { body: cr(t.body, "prompt.body") } : {},
    ...t.details !== void 0 ? { details: O3(t.details) } : {},
    ...t.review !== void 0 ? { review: C3(t.review) } : {},
    ...t.reviewSession !== void 0 ? { reviewSession: t.reviewSession } : {},
    ...n ? { challenge: n } : {},
    label: cr(t.label, "prompt.label")
  };
}
const Zf = 320, Yf = 220, L3 = 6e4, H3 = 1e3;
function N3(e, t = {}) {
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
    r ? (r.ageMs > L3 && n.push("parent frame visibility is stale"), (r.frameWidth < Zf || r.frameHeight < Yf) && n.push("parent frame is too small"), (!r.visibleByIntersectionObserver || r.intersectionRatio < 0.99) && n.push("parent frame is not fully visible"), (r.opacity < 0.99 || !r.pointerEvents) && n.push("parent frame is visually occluded")) : n.push("parent frame visibility is not attested");
  }
  return n.length > 0 ? { ok: !0, warnings: n } : { ok: !0 };
}
function Xf(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Xn(e) {
  if (typeof e == "number") return Number.isFinite(e) ? e : void 0;
  if (typeof e != "string" || e.trim() === "") return;
  const t = Number(e);
  return Number.isFinite(t) ? t : void 0;
}
function Cr(e) {
  const t = Xn(e);
  if (t !== void 0) return t;
  if (typeof e != "string" || e.trim() === "") return;
  const n = Date.parse(e);
  return Number.isFinite(n) ? n : void 0;
}
function dc(e) {
  return typeof e == "boolean" ? e : void 0;
}
function U3(e) {
  if (typeof e == "boolean") return e;
  if (typeof e == "string")
    return e.toLowerCase() !== "none";
}
function D3(e) {
  if (!e) return;
  const t = Xf(e.value);
  if (!t) return;
  const n = Xf(t.frame) ?? t, r = Xn(n.frameWidth) ?? Xn(n.width), i = Xn(n.frameHeight) ?? Xn(n.height), o = Xn(n.intersectionRatio), s = dc(n.visibleByIntersectionObserver) ?? dc(n.isVisible) ?? dc(n.visible), a = Xn(n.opacity), c = U3(n.pointerEvents), l = Cr(t.sampledAt) ?? Cr(t.measuredAt) ?? Cr(t.timestamp) ?? Cr(n.sampledAt) ?? Cr(n.measuredAt) ?? Cr(n.timestamp);
  if (r === void 0 || i === void 0 || o === void 0 || s === void 0 || a === void 0 || c === void 0 || l === void 0)
    return;
  const u = Date.now();
  if (!(l > u + H3))
    return {
      frameWidth: r,
      frameHeight: i,
      intersectionRatio: o,
      visibleByIntersectionObserver: s,
      opacity: a,
      pointerEvents: c,
      ageMs: Math.max(0, u - l)
    };
}
function sm(e) {
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
    parent: D3(e.parentSurface?.())
  }), c = (p) => {
    const y = a(), w = N3(y, { requireFocus: p });
    if (!w.ok) throw new S(w.code, w.detail, !0);
    if (e.requireParentSurface && y.embedded && (w.warnings?.length ?? 0) > 0)
      throw new S("sandbox_not_visible", w.warnings[0], !0);
    for (const m of w.warnings ?? []) console.warn(`[vault-sandbox] Confirm surface advisory: ${m}`);
  }, l = () => c(!0), u = new Promise((p, y) => {
    o = p, s = y;
  }), d = () => {
    if (!(i || r)) {
      i = !0;
      try {
        c(!1), o();
      } catch (p) {
        s(p);
      }
    }
  }, f = typeof IntersectionObserver > "u" ? null : new IntersectionObserver(
    (p) => {
      const y = p[0], w = y?.isVisible;
      n = y ? {
        ratio: y.intersectionRatio,
        visible: w === void 0 ? y.intersectionRatio >= 0.99 : w === !0
      } : { ratio: 0, visible: !1 }, d();
    },
    {
      threshold: [0, 0.99, 1],
      trackVisibility: !0,
      delay: 100
    }
  );
  f?.observe(t);
  const h = setTimeout(d, 250);
  return {
    ready: u,
    assertCurrent: l,
    dispose: () => {
      r || (r = !0, clearTimeout(h), f?.disconnect());
    }
  };
}
function am(e) {
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
async function z3(e) {
  let t = new TextEncoder().encode(e.value);
  try {
    const n = { name: e.name, kind: "static" }, r = await zl(t, e.vmk, n);
    return { envelope: Ml(r, e.wrapMeta, n) };
  } finally {
    t?.fill(0), t = void 0;
  }
}
async function M3(e) {
  const t = ci(e.curve), n = x2(t);
  let r = n.privateKey;
  try {
    const i = {
      name: e.name,
      kind: "keypair",
      publicKey: n.publicKey,
      ...t === "ed25519" ? { curve: t } : {}
    }, o = await zl(r, e.vmk, i);
    return {
      envelope: Ml(o, e.wrapMeta, i),
      publicKey: n.publicKey,
      addresses: t === "ed25519" ? { solana: A2(n.publicKey) } : ha(n.publicKey)
    };
  } finally {
    r?.fill(0), r = void 0;
  }
}
function Pd(e) {
  return (e?.kind ?? "static") === "static";
}
function K3(e, t) {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", "approveRelease item must be an object");
  const n = e;
  return {
    material: t(n.material),
    context: id(n.context)
  };
}
function cm(e) {
  const t = e[0]?.context.display;
  if (!t) throw new S("invalid_payload", "approveRelease requires at least one item");
  if (e[0].context.purpose !== "agent-deliver") throw new S("invalid_context", "approveRelease only accepts agent-deliver contexts");
  const n = cf(e[0].context), r = Jf(e[0].context);
  for (const s of e) {
    if (s.context.purpose !== "agent-deliver") throw new S("invalid_context", "approveRelease only accepts agent-deliver contexts");
    if (cf(s.context) !== n)
      throw new S("invalid_context", "approveRelease batch contexts must share one signed display block");
    if (Jf(s.context) !== r)
      throw new S("invalid_context", "approveRelease batch contexts must share one signed recipient");
  }
  const i = new Set(t.secrets.map((s) => `${s.name}:${s.kind}`));
  for (const s of e) {
    const { recordMetadata: a } = Vn(s.material.envelope), c = a?.kind ?? "static";
    if (!i.has(`${s.material.name}:${c}`))
      throw new S("invalid_context", "signed display block does not cover every release item");
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
  if (!e.grantId) throw new S("invalid_context", "agent delivery context is missing grantId");
  if (!e.agent) throw new S("invalid_context", "agent delivery context is missing agent key");
  return ye({ agent: e.agent, grantId: e.grantId });
}
async function F3(e) {
  if (e.items.length === 0) throw new S("invalid_payload", "approveRelease requires at least one item");
  const t = cm(e.items), n = await Ts(e.wrapMeta, e.vmk), r = e.items.map((a) => a.context);
  for (const a of e.items)
    Ks({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: e.now
    });
  ti(r, e.now);
  const i = await Gk(r);
  await e.confirm({
    ...t,
    challenge: i
  });
  const o = e.now ?? Date.now();
  for (const a of e.items)
    Ks({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: o
    });
  e.onApprovalAccepted?.(o), ti(r, o);
  const s = [];
  for (const a of e.items) {
    const { sealed: c, recordMetadata: l } = Vn(a.material.envelope);
    if (!Pd(l)) throw new S("invalid_payload", "approveRelease requires static protected records");
    s.push(
      await v2(
        c,
        e.vmk,
        Wk(a.context),
        fa(a.material.name, l),
        await qk(a.context, a.material.name)
      )
    );
  }
  return e.consumeReplayIds !== !1 && await Mi(r, o), { blindBoxes: s };
}
const Qf = "vault-signing-policy-downgrade", j3 = `avibe:vault:signing-policy-downgrade:v1
`, Dr = 65536, V3 = /^[A-Za-z0-9_-]{1,80}$/, eh = ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"];
function ne(e) {
  throw new S("policy_context_invalid", e);
}
function Hn(e, t, n) {
  (e === null || typeof e != "object" || Array.isArray(e) || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null) && ne(`${n} must be an object`);
  const r = e;
  return (Object.keys(r).length !== t.length || !t.every((i) => Object.hasOwn(r, i))) && ne(`${n} has missing or unknown fields`), r;
}
function Zc(e, t) {
  return (typeof e != "string" || e.length === 0) && ne(`${t} must be a nonempty string`), e;
}
function uc(e, t) {
  return (typeof e != "string" || !V3.test(e)) && ne(`${t} is not a valid ID`), e;
}
function Nn(e, t, n) {
  (typeof e != "string" || e.length > 4 * Math.ceil(n / 3) || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) && ne("noncanonical policy base64");
  const r = ve(e);
  return (r.length < t || r.length > n || ce(r) !== e) && ne("policy base64 length or padding is invalid"), r;
}
const q3 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function W3(e) {
  let t = 0n;
  for (const i of e) {
    const o = q3.indexOf(i);
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
  return $c("bc", t, a) !== e && ne("invalid witness address checksum"), a;
}
function G3(e, t) {
  if (e === null) return null;
  const n = Zc(e, `addresses.${t}`);
  if (n.length > 128 && ne("policy address is too long"), t === "eth")
    (!/^0x[0-9a-fA-F]{40}$/.test(n) || `0x${Fw(n.slice(2))}` !== n) && ne("noncanonical EIP-55 address");
  else if (t === "btc_segwit") nh(n, 0, 20);
  else if (t === "btc_taproot") nh(n, 1, 32);
  else {
    const r = W3(n);
    t === "solana" ? (r.length !== 32 || Ii(r) !== n) && ne("noncanonical Solana address") : (r.length !== 25 || r[0] !== 0 || Mw(r.slice(0, 21)) !== n) && ne("invalid mainnet P2PKH address");
  }
  return n;
}
function Z3(e) {
  return j3 + ye(e);
}
function lm(e) {
  return ue(new TextEncoder().encode(Z3(e)));
}
function Kn(e, t = Date.now()) {
  const n = Date.parse(e.expiresAt) - t;
  if (Number.isFinite(n) || ne("invalid policy expiry"), n <= 0) throw new S("policy_context_expired", "policy context has expired", !0);
  if (n > 15e4) throw new S("policy_context_clock_skew", "check this device's clock and retry", !0);
  return n;
}
function dm(e, t, n = Date.now(), r) {
  try {
    const i = JSON.stringify(e);
    (i === void 0 || i.length > Dr) && ne("policy context exceeds its size bound"), gr(e);
    const o = Hn(e, ["v", "purpose", "challengeId", "nonce", "expiresAt", "rpId", "origin", "target", "change", "factors"], "context");
    (o.v !== 1 || o.purpose !== Qf) && ne("unsupported policy context version or purpose"), (o.origin !== t.origin || o.rpId !== t.hostname) && ne("policy origin or RP does not match this sandbox");
    const s = Zc(o.expiresAt, "expiresAt"), a = Date.parse(s);
    (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(s) || !Number.isFinite(a) || new Date(a).toISOString() !== s) && ne("policy expiry must be an exact UTC millisecond date"), Nn(o.nonce, 32, 32);
    const c = Hn(o.target, ["secretId", "name", "protection", "signerKind", "account", "accountName", "addresses", "identitySha256"], "target");
    c.protection !== "standard" && c.protection !== "protected" && ne("invalid protection tier"), c.signerKind !== "local" && c.signerKind !== "hd-seed" && ne("unsupported signer kind"), (typeof c.identitySha256 != "string" || !/^[0-9a-f]{64}$/.test(c.identitySha256)) && ne("invalid identity digest");
    const l = Hn(c.addresses, eh, "addresses"), u = Object.fromEntries(eh.map((w) => [w, G3(l[w], w)]));
    if (c.signerKind === "hd-seed")
      (c.protection !== "standard" || typeof c.account != "number" || !Number.isSafeInteger(c.account) || c.account < 0 || c.account > 19) && ne("invalid shared HD account"), c.accountName !== null && (typeof c.accountName != "string" || [...c.accountName].length < 1 || [...c.accountName].length > 80) && ne("invalid HD account name"), (u.eth === null || u.btc_taproot === null || u.solana === null || u.btc_legacy !== null || u.btc_segwit !== null) && ne("invalid shared HD address set");
    else {
      (c.account !== null || c.accountName !== null) && ne("ordinary keys cannot carry an HD account");
      const w = u.eth !== null && u.btc_legacy !== null && u.btc_segwit !== null && u.btc_taproot !== null && u.solana === null, m = u.solana !== null && u.eth === null && u.btc_legacy === null && u.btc_segwit === null && u.btc_taproot === null;
      !w && !m && ne("invalid ordinary key address set"), w && c.protection === "protected" && !N_(r) && ne("this sandbox build does not support every protected Bitcoin address type");
    }
    const d = Hn(o.change, ["field", "from", "to", "revision"], "change");
    (d.field !== "always_ask" || d.from !== !0 || d.to !== !1 || typeof d.revision != "string" || !/^(?:legacy|[0-9a-f]{32})$/.test(d.revision)) && ne("only true-to-false always_ask changes are supported"), (!Array.isArray(o.factors) || o.factors.length === 0) && ne("policy factors must be nonempty");
    const f = /* @__PURE__ */ new Set();
    let h = "";
    const p = o.factors.map((w) => {
      const m = Hn(w, ["factorId", "credentialId"], "factor"), g = uc(m.factorId, "factorId");
      Nn(m.credentialId, 1, 1023);
      const v = m.credentialId;
      return (g <= h || f.has(v)) && ne("policy factors must have unique credentials and sorted unique IDs"), h = g, f.add(v), { factorId: g, credentialId: v };
    }), y = {
      v: 1,
      purpose: Qf,
      challengeId: uc(o.challengeId, "challengeId"),
      nonce: o.nonce,
      expiresAt: s,
      rpId: o.rpId,
      origin: o.origin,
      target: {
        secretId: uc(c.secretId, "secretId"),
        name: Zc(c.name, "name"),
        protection: c.protection,
        signerKind: c.signerKind,
        account: c.account,
        accountName: c.accountName,
        addresses: u,
        identitySha256: c.identitySha256
      },
      change: { field: "always_ask", from: !0, to: !1, revision: d.revision },
      factors: p
    };
    return new TextEncoder().encode(ye(y)).length > Dr && ne("policy context exceeds its UTF-8 size bound"), Kn(y, n), y;
  } catch (i) {
    if (i instanceof S) throw i;
    ne("policy context is not canonical JSON");
  }
}
function Y3(e, t) {
  return dm(Hn(e, ["context"], "payload").context, t);
}
function X3(e) {
  const t = Kn(e), n = Math.min(12e4, Math.floor(t));
  if (n <= 0) throw new S("policy_context_expired", "policy context has expired", !0);
  return {
    challenge: new Uint8Array(lm(e)).buffer,
    rpId: e.rpId,
    userVerification: "required",
    timeout: n,
    allowCredentials: e.factors.map((r) => ({ type: "public-key", id: new Uint8Array(Nn(r.credentialId, 1, 1023)).buffer }))
  };
}
function rh(e) {
  return ce(e).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function Yc(e, t) {
  try {
    const n = Hn(e, ["id", "rawId", "type", "response"], "assertion"), r = Nn(n.rawId, 1, 1023);
    (n.type !== "public-key" || n.id !== rh(r) || !t.factors.some((l) => l.credentialId === n.rawId)) && ne("assertion does not identify a frozen factor");
    const i = Hn(n.response, ["clientDataJSON", "authenticatorData", "signature", "userHandle"], "assertion.response"), o = Nn(i.clientDataJSON, 1, Dr), s = JSON.parse(new TextDecoder("utf-8", { fatal: !0 }).decode(o));
    (s.type !== "webauthn.get" || s.challenge !== rh(lm(t)) || s.origin !== t.origin || s.crossOrigin !== void 0 && s.crossOrigin !== !1) && ne("assertion client data does not bind this policy context");
    const a = Nn(i.authenticatorData, 37, Dr), c = ue(new TextEncoder().encode(t.rpId));
    return ((a[32] & 5) !== 5 || !a.slice(0, 32).every((l, u) => l === c[u])) && ne("assertion RP or UP/UV flags are invalid"), Nn(i.signature, 1, Dr), i.userHandle !== null && Nn(i.userHandle, 0, Dr), Kn(t), { id: n.id, rawId: n.rawId, type: "public-key", response: { ...i } };
  } catch (n) {
    throw n instanceof S && n.code !== "policy_context_invalid" ? n : new S("authorization_result_invalid", "invalid policy assertion");
  }
}
async function J3(e, t) {
  if (t.aborted) throw t.reason;
  const n = await navigator.credentials.get({ publicKey: X3(e), signal: t });
  if (t.aborted) throw t.reason;
  if (!n) throw new Error("passkey-cancelled");
  return Yc(sy(n), e);
}
function es(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new S("invalid_payload", "reveal material must be an object");
  return e;
}
function Mo(e) {
  if (typeof e != "string" || e.length === 0) throw new S("invalid_payload", "reveal material requires nonempty strings");
  return e;
}
function Zs(e) {
  return e && typeof e == "object" && (Object.values(e).forEach(Zs), Object.freeze(e)), e;
}
function Q3(e) {
  const t = { ciphertext: e.ciphertext, nonce: e.nonce, wrap_meta: e.wrap_meta };
  return gr(t), G(ue(new TextEncoder().encode(ye(t))));
}
function um(e) {
  od({ context: e.context, expectedPurpose: "reveal", secret: { name: e.material.name, kind: "static" } });
  const t = e.context.release, n = t?.envelopeHash;
  if (!t || t.name !== e.material.name || !n || typeof n != "object" || Array.isArray(n))
    throw new S("invalid_reveal_binding", "reveal approval does not bind this envelope");
  const r = n;
  if (Object.keys(r).length !== 2 || r.alg !== "sha256" || typeof r.digest != "string" || !/^[0-9a-f]{64}$/.test(r.digest) || r.digest !== Q3(e.material.envelope))
    throw new S("invalid_reveal_binding", "reveal approval does not bind this envelope");
}
function e5(e) {
  gr(e);
  const t = es(structuredClone(e)), n = es(t.material), r = es(n.envelope), i = Zs({
    material: { name: Mo(n.name), envelope: {
      ciphertext: Mo(r.ciphertext),
      nonce: Mo(r.nonce),
      wrap_meta: Mo(r.wrap_meta)
    } },
    context: id(t.context)
  });
  return um(i), i;
}
const Xc = 32, t5 = /^[A-Za-z_][A-Za-z0-9_]*$/, fm = /^[a-z][a-z0-9_]{0,63}$/;
function n5(e) {
  const t = e.indexOf("."), n = t < 0 ? e : e.slice(0, t), r = t < 0 ? null : e.slice(t + 1);
  if (!t5.test(n) || r !== null && !fm.test(r))
    throw new S("invalid_payload", "reveal record name is invalid");
  return { secret: n, field: r };
}
function r5(e) {
  gr(e);
  const t = es(e), n = !Object.hasOwn(t, "items");
  if (!n) {
    for (const c of Object.keys(t))
      if (c !== "items" && c !== "defaultFieldLabel") throw new S("invalid_payload", "reveal request has unknown fields");
  }
  const r = n ? [t] : t.items;
  if (!Array.isArray(r) || r.length < 1 || r.length > Xc)
    throw new S("invalid_payload", `reveal items must hold 1 to ${Xc} fields`);
  const i = n ? void 0 : t.defaultFieldLabel;
  if (i !== void 0 && (typeof i != "string" || !fm.test(i)))
    throw new S("invalid_payload", "reveal defaultFieldLabel is invalid");
  const o = /* @__PURE__ */ new Set();
  let s;
  const a = r.map((c) => {
    const l = e5(c), u = n5(l.material.name);
    if (s !== void 0 && u.secret !== s) throw new S("invalid_payload", "reveal items must belong to one secret");
    if (o.has(l.material.name)) throw new S("invalid_payload", "reveal items must be distinct fields");
    return s = u.secret, o.add(l.material.name), Zs({ ...l, field: u.field });
  });
  return Zs({ secret: s, items: a, ...i !== void 0 ? { defaultFieldLabel: i } : {} });
}
const i5 = "••••••••";
function Ko(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function o5(e) {
  return dn(async (t) => {
    try {
      if (t.aborted) throw Ko(t);
      if (e.abortSignal?.aborted) throw Ko(e.abortSignal);
      const n = e.rows.length === 1, r = eo("plaintext.title", me(e.name), "plaintext.body"), i = document.createElement("div");
      i.className = "secret-rows";
      const o = zs();
      o.classList.add("secret-status"), o.setAttribute("role", "status");
      const s = (p) => {
        if (p === null) {
          o.textContent = "";
          return;
        }
        J(o, p);
      }, a = e.clipboard ?? ((p) => navigator.clipboard.writeText(p)), c = [];
      let l = !1, u = !1;
      const d = /* @__PURE__ */ new Set(), f = (p) => new TextDecoder().decode(p.value);
      for (const p of e.rows) {
        const y = document.createElement("div");
        y.className = "secret-row";
        const w = p.label ?? Y("plaintext.valueLabel"), m = document.createElement("span");
        m.className = "secret-row-label", m.textContent = w;
        const g = document.createElement("pre");
        g.className = "secret-row-value plaintext";
        let v = n;
        const L = () => {
          if (g.textContent = "", v) {
            g.textContent = f(p), g.classList.remove("secret-row-hidden");
            return;
          }
          const O = document.createElement("span");
          O.setAttribute("aria-hidden", "true"), O.textContent = i5;
          const I = document.createElement("span");
          I.className = "secret-row-spoken", I.textContent = Y("plaintext.hiddenValue"), g.append(O, I), g.classList.add("secret-row-hidden");
        };
        L();
        const T = document.createElement("div");
        if (T.className = "secret-row-actions", !n) {
          const O = mt("plaintext.show");
          O.classList.add("secondary", "secret-row-button"), O.setAttribute("aria-label", Y("plaintext.showRow", { label: w })), O.addEventListener("click", () => {
            l || (v = !v, L(), J(O, v ? "plaintext.hide" : "plaintext.show"), O.setAttribute("aria-label", Y(v ? "plaintext.hideRow" : "plaintext.showRow", { label: w })));
          }), T.append(O);
        }
        const C = mt("plaintext.copy");
        C.classList.add("secondary", "secret-row-button"), C.setAttribute("aria-label", Y("plaintext.copyRow", { label: w }));
        let H = !p.prepareCopy;
        c.push({ node: C, reset: () => {
          J(C, "plaintext.copy"), C.setAttribute("aria-label", Y("plaintext.copyRow", { label: w }));
        } }), C.addEventListener("click", () => {
          if (l || u) return;
          u = !0, C.disabled = !0;
          const O = new AbortController();
          d.add(O), (p.prepareCopy && !H ? p.prepareCopy(O.signal).then(() => {
            O.signal.aborted || l || (H = !0, s(n ? "plaintext.copyReady" : Oe("plaintext.copyReadyRow", { label: w })));
          }) : a(f(p)).then(() => {
            if (!(O.signal.aborted || l)) {
              p.prepareCopy && (H = !1);
              for (const U of c) U.reset();
              J(C, "plaintext.copied"), C.setAttribute("aria-label", Y("plaintext.copiedRow", { label: w })), s(null);
            }
          })).then(
            () => {
            },
            () => {
              !O.signal.aborted && !l && s("plaintext.copyFailed");
            }
          ).finally(() => {
            d.delete(O), u = !1, l || (C.disabled = !1);
          });
        }), T.append(C), y.append(m, g, T), i.append(y);
      }
      const h = mt("common.done");
      te(r.card, i), te(r.card, o), te(r.card, h), await new Promise((p, y) => {
        const w = (v) => {
          if (!l) {
            l = !0;
            for (const L of d) L.abort(new Error("operation-superseded"));
            t.removeEventListener("abort", m), e.abortSignal?.removeEventListener("abort", g), v();
          }
        }, m = () => w(() => y(Ko(t))), g = () => w(() => y(Ko(e.abortSignal)));
        t.addEventListener("abort", m, { once: !0 }), e.abortSignal?.addEventListener("abort", g, { once: !0 }), h.addEventListener("click", () => w(p), { once: !0 });
      });
    } finally {
      for (const n of e.rows) n.value.fill(0);
      ma();
    }
  });
}
function hm(e) {
  const t = Es(e);
  if (t.v !== 1 || !Array.isArray(t.copies) || t.copies.length === 0 || ["record_meta", "dek_nonce", "wrapped_dek"].some((n) => Object.hasOwn(t, n)) || t.scheme !== void 0 && t.scheme !== "machine-aesgcm-v1") throw new Error("protected HD requires an existing base/root wrap");
  for (const n of t.copies) {
    const r = Et(n);
    if (r.kind !== "passkey" || r.kdf !== "webauthn-prf-hkdf-sha256" || r.credential_id !== void 0 && (typeof r.credential_id != "string" || r.credential_id.length === 0)) throw new Error("invalid protected HD passkey copy");
    or(r.prf_salt, [32]), or(r.nonce, [12]), or(r.wrapped, [48]);
  }
  return e;
}
function ih(e) {
  const t = Es(e);
  for (const n of ["scheme", "record_meta", "dek_nonce", "wrapped_dek"]) delete t[n];
  return ye(t);
}
function pm(e, t, n) {
  const r = Et(e);
  Ut(r, ["envelope", "accounts", "expectedAccount0"]);
  const i = Et(r.envelope);
  if (Ut(i, ["ciphertext", "nonce", "wrap_meta"]), typeof i.ciphertext != "string" || typeof i.nonce != "string" || typeof i.wrap_meta != "string") throw new Error("invalid protected HD envelope");
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(t)) throw new Error("invalid protected HD name");
  const o = i, s = Vn(o);
  if (s.recordMetadata?.kind !== "hd-seed" || ih(o.wrap_meta) !== ih(n)) throw new Error("protected HD result root mismatch");
  const a = sa({ kind: "hd-seed", profile: vs, accounts: r.accounts });
  if (ye(a) !== ye(s.recordMetadata)) throw new Error("protected HD result table mismatch");
  const c = Et(r.expectedAccount0);
  Ut(c, ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"]);
  const l = Rl(a.accounts[0]);
  if (ye(c) !== ye(l)) throw new Error("protected HD result account0 mismatch");
  return { envelope: { ...o }, accounts: a.accounts, expectedAccount0: l };
}
async function s5(e) {
  e.assertCurrent(), hm(e.wrapMeta);
  const t = Iw(e.words, e.passphrase);
  let n, r, i;
  try {
    const o = Pc(t.seed);
    e.assertCurrent();
    const s = { kind: "hd-seed", profile: vs, accounts: o }, a = fa(e.name, s);
    n = Lx(t);
    const c = await zl(n, e.vmk, a);
    e.assertCurrent();
    const u = { envelope: Ml(c, e.wrapMeta, a), accounts: o, expectedAccount0: Rl(o[0]) };
    r = await li(c, e.vmk, a), e.assertCurrent(), i = Sw(r);
    const d = (h, p) => h.length === p.length && h.every((y, w) => y === p[w]);
    if (!d(i.entropy, t.entropy) || !d(i.seed, t.seed) || i.passphraseRequired !== t.passphraseRequired || ye(Pc(i.seed)) !== ye(o)) throw new Error("protected HD creation reopen mismatch");
    const f = pm(u, e.name, e.wrapMeta);
    return e.assertCurrent(), f;
  } finally {
    n?.fill(0), r?.fill(0), ks(i), ks(t);
  }
}
const wm = 21e5;
function ym(e) {
  try {
    const t = Et(e);
    if (Ut(t, ["name", "mode", "wrapMeta"]), typeof t.name != "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(t.name) || t.mode !== "create" && t.mode !== "import") throw new Error("invalid protected HD name/mode");
    return { name: t.name, mode: t.mode, wrapMeta: hm(t.wrapMeta) };
  } catch {
    throw new S("invalid_payload", "sealHdWallet requires only name, create/import mode and an existing base root");
  }
}
function wr(e) {
  const t = e.deadlineAt - Date.now();
  if (!Number.isSafeInteger(e.deadlineAt) || t <= 0 || t > wm) throw new S("authorization_window_timeout", "protected HD window expired", !0);
  return t;
}
function a5(e) {
  const t = Et(e);
  if (Ut(t, ["kind", "name", "mode", "wrapMeta", "deadlineAt"]), t.kind !== "protected-hd-wallet" || typeof t.deadlineAt != "number") throw new S("invalid_payload", "invalid protected HD window request");
  const r = { ...ym({ name: t.name, mode: t.mode, wrapMeta: t.wrapMeta }), kind: "protected-hd-wallet", deadlineAt: t.deadlineAt };
  return wr(r), Object.freeze(r);
}
function Ys(e, t) {
  try {
    const n = Et(e);
    if (Ut(n, ["kind", "envelope", "accounts", "expectedAccount0"]), n.kind !== "sealed-hd-wallet") throw new Error("invalid result kind");
    return wr(t), { kind: "sealed-hd-wallet", ...pm({ envelope: n.envelope, accounts: n.accounts, expectedAccount0: n.expectedAccount0 }, t.name, t.wrapMeta) };
  } catch (n) {
    throw n instanceof S ? n : new S("authorization_result_invalid", "protected HD result is invalid");
  }
}
async function c5(e, t, n) {
  const r = new AbortController(), i = () => t.reason instanceof Error ? t.reason : new S("authorization_cancelled", "protected HD operation cancelled", !0), o = () => r.abort(i());
  t.addEventListener("abort", o, { once: !0 }), t.aborted && o();
  const s = () => {
    if (r.signal.aborted) throw r.signal.reason;
    wr(e);
  };
  let a, c;
  const l = (u) => c ? Promise.reject(new S("authorization_result_invalid", "protected HD wallet is already being sealed")) : (c = (async () => {
    let d, f, h, p = [];
    try {
      s(), p = ua(e.wrapMeta);
      const y = await ga(p, Ft(), r.signal);
      d = y.prfOutput, f = y.prfSalt, s(), h = await Ow(e.wrapMeta, { kind: "passkey", prfOutput: d, prfSalt: f }), s();
      const w = await s5({ name: e.name, words: u.words, passphrase: u.passphrase, vmk: h, wrapMeta: e.wrapMeta, assertCurrent: s });
      if (s(), ye(w.accounts) !== ye(u.reviewedAccounts)) throw new S("authorization_result_invalid", "protected HD reviewed account table changed");
      a = Ys({ kind: "sealed-hd-wallet", ...w }, e);
    } finally {
      d?.fill(0), f?.fill(0), h?.fill(0), p.forEach((y) => y.prfSalt.fill(0)), u.words = "", u.passphrase = "", u.reviewedAccounts = [];
    }
  })(), c);
  try {
    if (s(), await n(e, r.signal, l), c && await c, s(), !a) throw new S("authorization_result_invalid", "protected HD wallet was not sealed");
    return Ys(a, e);
  } catch (u) {
    throw r.abort(u), c && await c.catch(() => {
    }), u;
  } finally {
    t.removeEventListener("abort", o);
  }
}
const l5 = [12, 15, 18, 21, 24];
function Jc(e) {
  return e.normalize("NFKD").toLowerCase().trim().split(/\s+/u).filter(Boolean);
}
const d5 = new Set(ai);
function u5(e, t = 3) {
  for (let n = Math.min(4, e.length); n >= 1; n -= 1) {
    const r = e.slice(0, n), i = ai.filter((o) => o.startsWith(r));
    if (i.length > 0) return i.slice(0, t);
  }
  return [];
}
function oh(e) {
  const t = Jc(e);
  if (t.length === 0) return { ok: !1, reason: "empty" };
  const n = t.findIndex((r) => !d5.has(r));
  return n >= 0 ? { ok: !1, reason: "unknown-word", position: n + 1, word: t[n], suggestions: u5(t[n]) } : l5.includes(t.length) ? $x(t.join(" "), ai) ? { ok: !0, words: t } : { ok: !1, reason: "checksum" } : { ok: !1, reason: "count", count: t.length };
}
function f5(e, t = Math.random) {
  const n = /* @__PURE__ */ new Set();
  for (; n.size < 3; ) n.add(1 + Math.floor(t() * e.length));
  const r = [...n].sort((c, l) => c - l), i = new Set(r.map((c) => e[c - 1])), o = new Set(i), s = (c) => {
    const l = c.filter((d) => !o.has(d));
    if (l.length === 0) return null;
    const u = l[Math.floor(t() * l.length)];
    return o.add(u), u;
  }, a = [...new Set(e)];
  return r.map((c) => {
    const l = e[c - 1], u = [];
    for (; u.length < 2; ) u.push(s(a) ?? s(ai));
    const d = [l, ...u];
    for (let f = d.length - 1; f > 0; f -= 1) {
      const h = Math.floor(t() * (f + 1));
      [d[f], d[h]] = [d[h], d[f]];
    }
    return { position: c, options: d, answer: l };
  });
}
const sh = (e, t) => e.every((n) => t[n.position] === n.answer);
async function h5(e, t) {
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
function p5() {
  const e = new Uint32Array(1);
  return crypto.getRandomValues(e), e[0] / 2 ** 32;
}
function w5(e, t = p5) {
  return f5(e, t);
}
function Z(e, t, n) {
  const r = document.createElement(e);
  return r.className = `${t} dynamic`, n !== void 0 && J(r, n), r;
}
function gm(e) {
  const t = document.createElement(e);
  return t.className = e === "textarea" ? "field textarea" : "field", t.setAttribute("autocomplete", "off"), t.setAttribute("autocapitalize", "off"), t.setAttribute("autocorrect", "off"), t.setAttribute("spellcheck", "false"), t instanceof HTMLInputElement && (t.type = "password"), t;
}
const Xs = "http://www.w3.org/2000/svg", y5 = ["M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0"], g5 = [
  "M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49",
  "M14.084 14.158a3 3 0 0 1-4.242-4.242",
  "M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143",
  "m2 2 20 20"
];
function m5(e) {
  const t = document.createElementNS(Xs, "svg"), n = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true"
  };
  for (const [r, i] of Object.entries(n)) t.setAttribute(r, i);
  for (const r of e ? g5 : y5) {
    const i = document.createElementNS(Xs, "path");
    i.setAttribute("d", r), t.append(i);
  }
  return t;
}
function b5(e) {
  const t = document.createElementNS(Xs, "svg");
  for (const [r, i] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" })) t.setAttribute(r, i);
  const n = document.createElementNS(Xs, "path");
  return n.setAttribute("d", e ? "m6 9 6 6 6-6" : "m9 18 6-6-6-6"), t.append(n), t;
}
let v5 = 0;
function fc(e) {
  const t = Z("div", "hd-field"), n = `hd-passphrase-${++v5}`, r = Z("label", "hd-field-label", e);
  r.htmlFor = n;
  const i = Z("div", "hd-secret-row"), o = gm("input");
  o.id = n;
  const s = document.createElement("button");
  s.type = "button", s.className = "hd-reveal", s.setAttribute("aria-controls", n);
  const a = Z("span", "hd-reveal-label"), c = (l) => {
    o.type = l ? "text" : "password", s.setAttribute("aria-pressed", String(l)), J(a, l ? "hd.hideValue" : "hd.showValue"), s.textContent = "", s.append(m5(l), a);
  };
  return s.addEventListener("click", () => c(o.type === "password")), c(!1), i.append(o, s), t.append(r, i), { wrap: t, input: o, toggle: s, hide: () => c(!1) };
}
function x5(e, t, n) {
  return t === "" || !n && t.length < e.length ? "hint" : e === t ? "match" : "mismatch";
}
function Rr(e, ...t) {
  const n = Z("div", `hd-notice hd-notice-${e}`);
  return n.setAttribute("role", e === "error" ? "alert" : "note"), t.forEach((r, i) => n.append(Z("span", i === 0 && t.length > 1 ? "hd-notice-lead" : "hd-notice-text", r))), n;
}
function hc(e) {
  const t = Z("ol", "hd-stepper");
  return mm(t, "hd.stepsLabel"), ["hd.stepSetup", "hd.stepBackup", "hd.stepCheck"].forEach((n, r) => {
    const i = r + 1, o = document.createElement("li");
    o.className = i < e ? "done" : i === e ? "current" : "", i === e && o.setAttribute("aria-current", "step");
    const s = document.createElement("span");
    s.className = "hd-step-dot", s.textContent = i < e ? "✓" : String(i);
    const a = document.createElement("span");
    a.className = "hd-step-name", J(a, n), o.append(s, a), t.append(o);
  }), t;
}
function mm(e, t) {
  const n = document.createElement("span");
  J(n, t), e.setAttribute("aria-label", n.textContent ?? "");
}
function ah(e, t, n) {
  const r = Z("div", "hd-fold"), i = document.createElement("button");
  i.type = "button", i.className = "hd-fold-header";
  const o = Z("span", "hd-fold-chevron");
  o.setAttribute("aria-hidden", "true");
  const s = Z("span", "hd-fold-title", e), a = Z("span", "hd-fold-hint");
  i.append(o, s, a);
  const c = Z("div", "hd-fold-body"), l = () => {
    i.setAttribute("aria-expanded", String(n)), o.textContent = "", o.append(b5(n)), c.hidden = !n, a.hidden = n, J(a, n ? me("") : t());
  };
  return i.addEventListener("click", () => {
    n = !n, l();
  }), l(), r.append(i, c), { wrap: r, body: c, refresh: l };
}
function ch(e, t = {}) {
  const n = Z("ol", "hd-words");
  return mm(n, "hd.stepBackup"), t.hidden && (n.classList.add("hd-words-hidden"), n.setAttribute("aria-hidden", "true")), e.forEach((r, i) => {
    const o = document.createElement("li");
    t.bad === i && (o.className = "bad");
    const s = document.createElement("span");
    s.className = "hd-word-index", s.textContent = String(i + 1);
    const a = document.createElement("span");
    t.hidden ? J(a, "hd.hiddenWord") : a.textContent = r, o.append(s, a), n.append(o);
  }), n;
}
function k5(e) {
  const t = Rl(e), n = Z("div", "hd-addresses"), r = [
    ["hd.reviewEvm", t.eth],
    ["hd.reviewBitcoin", t.btc_taproot],
    ["hd.reviewSolana", t.solana]
  ];
  for (const [i, o] of r) {
    const s = document.createElement("div");
    s.className = "hd-address";
    const a = document.createElement("span");
    a.className = "hd-address-label", J(a, i);
    const c = document.createElement("span");
    c.className = "hd-address-value", c.textContent = o, s.append(a, c), n.append(s);
  }
  return n;
}
function lh(e, t) {
  const n = Iw(e, t);
  try {
    return Pc(n.seed);
  } finally {
    ks(n);
  }
}
const E5 = 250, A5 = (e, t, n) => new Promise((r, i) => {
  const o = [];
  let s = [], a = "", c = "", l = "", u = [], d = {}, f = [], h = !1, p = !1, y = 0;
  const w = () => {
    for (const A of o.splice(0)) A();
  }, m = (A, B) => (w(), y += 1, eo(A, B)), g = (A) => {
    if (!h) {
      h = !0, t.removeEventListener("abort", v), w(), s = [], a = "", c = "", l = "";
      for (const B of u)
        B.answer = "", B.options.fill("");
      u = [], d = {}, f = [], ma(), A();
    }
  }, v = () => g(() => i(t.reason instanceof Error ? t.reason : new Error("operation-cancelled")));
  if (t.aborted) {
    v();
    return;
  }
  t.addEventListener("abort", v, { once: !0 });
  const L = "hd.title", T = me(e.name), C = (A, B, P = !1, R = !1) => {
    const N = y;
    A.addEventListener("click", () => {
      if (!(h || N !== y || p && !R))
        try {
          B();
        } catch (K) {
          g(() => i(K));
        }
    }, { once: P });
  }, H = (A, B, P = "input") => {
    const R = y;
    A.addEventListener(P, (N) => {
      if (!(h || R !== y || p))
        try {
          B(N);
        } catch (K) {
          g(() => i(K));
        }
    });
  }, O = (A) => {
    const B = mt(A);
    return B.classList.add("secondary"), B;
  }, I = () => {
    const A = O("common.cancel");
    return C(A, () => g(() => i(new Error("operation-cancelled"))), !0, !0), A;
  }, U = (A) => {
    o.push(() => {
      A.value = "";
    });
  }, z = (A) => {
    o.push(() => {
      A.textContent = "";
    });
  }, E = (A, B, P, R) => {
    if (p) return;
    p = !0;
    for (const K of ["input", "textarea", "button"])
      A.querySelectorAll(K).forEach((D) => {
        D !== B && (D.disabled = !0);
      });
    J(P, "hd.working"), n(R).then(() => g(r), (K) => g(() => i(K)));
  }, k = () => {
    const A = () => !!(a || c);
    let B = !1;
    const P = () => x5(a, c, B), R = () => {
      const j = P();
      return j === "mismatch" ? "hd.passphraseMismatch" : j === "match" ? "hd.passphraseMatch" : a !== c ? "hd.passphraseConfirmPending" : "hd.passphraseHint";
    }, N = ah("hd.passphraseTitle", () => a !== c ? R() : A() ? "hd.passphraseSet" : "hd.passphraseFolded", A()), K = fc("hd.passphraseLabel"), D = fc("hd.passphraseAgain");
    K.input.value = a, D.input.value = c;
    for (const j of [K, D])
      U(j.input), o.push(j.hide);
    const M = Z("p", "hd-hint"), F = () => {
      c === "" && (B = !1);
      const j = P() === "mismatch";
      M.className = `hd-hint dynamic${j ? " hd-error" : P() === "match" ? " hd-ok" : ""}`, M.setAttribute("role", j ? "alert" : "status"), D.input.setAttribute("aria-invalid", String(j)), J(M, R());
    }, q = () => {
      B = !0, F();
    };
    return H(D.input, q, "blur"), H(D.input, (j) => {
      j.key === "Enter" && q();
    }, "keydown"), C(D.toggle, q), H(D.input, () => {
      B = !1, F();
    }, "focus"), N.body.append(Z("p", "hd-hint", "hd.passphraseBody"), K.wrap, D.wrap, M), { wrap: N.wrap, fields: [K, D], refresh: F };
  }, x = () => {
    const A = m(L, T);
    te(A.card, hc(1)), te(A.card, Z("p", "hd-hint", "hd.generateHint"));
    const B = k(), P = mt("hd.generateCta"), R = () => {
      a = B.fields[0].input.value, c = B.fields[1].input.value, B.refresh(), P.disabled = a !== c;
    };
    B.fields.forEach((N) => H(N.input, R)), B.refresh(), P.disabled = a !== c, C(P, () => {
      if (a === c) {
        if (s.length === 0) {
          const N = new Uint8Array(16);
          try {
            crypto.getRandomValues(N), s = Hx(N).split(" ");
          } finally {
            N.fill(0);
          }
        }
        u.length === 0 && (u = w5(s)), b();
      }
    }), te(A.card, B.wrap), te(A.card, P), te(A.card, I());
  }, b = () => {
    const A = m(L, T);
    te(A.card, hc(2)), te(A.card, Z("p", "hd-step-title", Oe("hd.backupTitle", { count: s.length }))), te(A.card, Z("p", "hd-hint", "hd.backupBody"));
    const B = Z("div", "hd-backup");
    z(B);
    let P = !1;
    const R = (D) => {
      B.textContent = "";
      const M = Z("div", "hd-words-frame");
      if (M.append(ch(s, { hidden: D })), D) {
        const X = Z("div", "hd-reveal-overlay"), we = O(Oe("hd.backupShow", { count: s.length }));
        we.classList.add("hd-inline"), C(we, () => R(!1)), X.append(we, Z("span", "hd-reveal-note", "hd.backupScreen")), M.append(X), B.append(M);
        return;
      }
      const F = Z("div", "hd-word-tools"), q = O(P ? "hd.backupCopied" : "hd.backupCopy");
      q.classList.add("hd-inline");
      const j = y;
      C(q, () => {
        h5(s.join(" "), () => !h && j === y).then((X) => {
          !X || h || j !== y || (P = !0, J(q, "hd.backupCopied"));
        }, () => {
        });
      });
      const W = O("hd.backupHide");
      W.classList.add("hd-inline"), C(W, () => R(!0)), F.append(q, W, Z("span", "hd-hint", "hd.backupClipboard")), B.append(M, F);
    };
    R(!0), te(A.card, B), a && te(A.card, Rr("warning", "hd.passphraseBackupReminder")), te(A.card, Rr("note", "hd.backupAgentNote"));
    const N = O("hd.back");
    C(N, () => {
      d = {}, x();
    }, !0);
    const K = mt("hd.backupDone");
    C(K, () => _(), !0), te(A.card, K), te(A.card, N), te(A.card, I());
  }, _ = () => {
    const A = m(L, T);
    f = lh(s.join(" "), a), te(A.card, hc(3)), te(A.card, Z("p", "hd-step-title", "hd.checkTitle")), te(A.card, Z("p", "hd-hint", "hd.checkBody"));
    const B = Z("div", "hd-problem"), P = mt("hd.createCta");
    P.classList.add("hd-gated");
    const R = () => {
      const M = u.find((F) => d[F.position] && d[F.position] !== F.answer);
      B.textContent = "", M && B.append(Rr("error", Oe("hd.checkWrong", { position: M.position }), Oe("hd.checkWrongHint", { position: M.position }))), P.disabled = p || !sh(u, d);
    };
    for (const M of u) {
      const F = Z("div", "hd-quiz");
      F.setAttribute("role", "radiogroup");
      const q = Z("span", "hd-field-label", Oe("hd.checkWord", { position: M.position }));
      q.id = `hd-quiz-${M.position}`, F.setAttribute("aria-labelledby", q.id), F.append(q);
      const j = M.options.map((W) => {
        const X = document.createElement("button");
        return X.type = "button", X.className = "hd-option", X.setAttribute("role", "radio"), X.setAttribute("aria-checked", "false"), X.textContent = W, C(X, () => {
          d = { ...d, [M.position]: W }, j.forEach((we) => {
            const se = we === X;
            we.setAttribute("aria-checked", String(se)), we.classList.remove("selected", "wrong"), se && we.classList.add(W === M.answer ? "selected" : "wrong");
          }), R();
        }), X;
      });
      F.append(...j), z(F), te(A.card, F);
    }
    R();
    const N = zs(), K = O("hd.checkAgain"), D = I();
    C(K, () => {
      d = {}, f = [], b();
    }, !0), C(P, () => {
      P.disabled || !sh(u, d) || E(A.card, D, N, { words: s.join(" "), passphrase: a, reviewedAccounts: f });
    }), te(A.card, B), te(A.card, Z("p", "hd-hint", "hd.passkeyNote")), te(A.card, N), te(A.card, P), te(A.card, K), te(A.card, D);
  }, $ = () => {
    const A = m(L, T);
    te(A.card, Z("p", "hd-hint", "hd.importHint"));
    const B = Z("label", "hd-field"), P = Z("span", "hd-field-label", "hd.importLabel"), R = gm("textarea");
    R.rows = 3;
    const N = document.createElement("span");
    J(N, "hd.importPlaceholder"), R.placeholder = N.textContent ?? "", R.value = l, U(R), B.append(P, R);
    const K = Z("div", "hd-import-words"), D = Z("div", "hd-import-verdict"), M = Z("div", "hd-import-passphrase"), F = Z("div", "hd-preview");
    for (const _e of [K, D, F]) z(_e);
    const q = ah("hd.importPassphrase", () => "hd.importPassphraseFolded", !!a), j = fc("hd.passphraseLabel");
    j.input.value = a, U(j.input), o.push(j.hide), q.body.append(j.wrap, Z("p", "hd-hint", "hd.importPassphraseHint")), M.append(q.wrap);
    const W = zs(), X = mt("hd.importCta"), we = I();
    X.classList.add("hd-gated");
    let se = oh(l), xe = null, Ze, fn = 0;
    o.push(() => {
      Ze !== void 0 && clearTimeout(Ze), Ze = void 0, fn += 1, xe = null;
    });
    const xr = () => se.ok ? { words: _w(se.words.join(" ")), passphrase: a } : null, Cm = () => {
      D.textContent = "";
      const _e = Jc(l);
      if (K.textContent = "", _e.length > 0 ? K.append(ch(_e, { bad: !se.ok && se.reason === "unknown-word" ? se.position - 1 : void 0 })) : K.append(Z("p", "hd-hint", "hd.importLocal")), se.ok)
        D.append(Z("p", "hd-hint hd-ok", Oe("hd.importOk", { count: se.words.length })));
      else if (se.reason === "unknown-word") {
        const hn = Rr(
          "error",
          Oe("hd.importUnknown", { position: se.position, word: se.word }),
          ...se.suggestions[0] ? [Oe("hd.importDidYouMean", { word: se.suggestions[0] })] : []
        );
        if (se.suggestions.length > 0) {
          const pn = Z("div", "hd-suggestions"), fi = se.position;
          for (const Ld of se.suggestions) {
            const Pa = O(Oe("hd.importUse", { word: Ld }));
            Pa.classList.add("hd-inline"), C(Pa, () => {
              l = Jc(l).map((Rm, Lm) => Lm === fi - 1 ? Ld : Rm).join(" "), R.value = l, Ta();
            }), pn.append(Pa);
          }
          hn.append(pn);
        }
        D.append(hn);
      } else se.reason === "checksum" ? D.append(Rr("error", Oe("hd.importChecksum", { count: _e.length }), "hd.importChecksumHint")) : se.reason === "count" && D.append(Rr("error", Oe("hd.importCount", { count: se.count })));
      M.hidden = !se.ok;
    }, Cd = () => {
      F.textContent = "";
      const _e = xr();
      if (X.disabled = p || !xe || !_e || xe.words !== _e.words || xe.passphrase !== _e.passphrase, !xe || X.disabled) return;
      const hn = Z("div", "hd-preview-box"), pn = Z("p", "hd-preview-title");
      pn.append(Z("span", "hd-account-index", Oe("hd.accountIndex", { account: 0 })), Z("span", "", "hd.importPreviewTitle")), hn.append(pn, k5(xe.accounts[0]), Z("p", "hd-hint", "hd.btcTaprootInfo"), Z("p", "hd-hint", "hd.importPreviewHint")), F.append(hn);
    }, Rd = () => {
      Ze !== void 0 && clearTimeout(Ze), Ze = void 0, xe = null, fn += 1, Cd();
      const _e = xr();
      if (!_e) return;
      const hn = fn, pn = y;
      Ze = setTimeout(() => {
        if (Ze = void 0, !(h || pn !== y || hn !== fn))
          try {
            const fi = lh(_e.words, _e.passphrase);
            if (h || pn !== y || hn !== fn) return;
            xe = { ..._e, accounts: fi }, Cd();
          } catch (fi) {
            g(() => i(fi));
          }
      }, E5);
    }, Ta = () => {
      se = oh(l), Cm(), Rd();
    };
    H(R, () => {
      l = R.value, Ta();
    }), H(j.input, () => {
      a = j.input.value, Rd();
    }), C(X, () => {
      const _e = xr();
      X.disabled || !_e || !xe || xe.words !== _e.words || xe.passphrase !== _e.passphrase || E(A.card, we, W, { words: _e.words, passphrase: _e.passphrase, reviewedAccounts: xe.accounts });
    }), Ta(), te(A.card, B), te(A.card, K), te(A.card, D), te(A.card, M), te(A.card, F), te(A.card, Z("p", "hd-hint", "hd.passkeyNote")), te(A.card, W), te(A.card, X), te(A.card, we);
  };
  try {
    e.mode === "create" ? x() : $();
  } catch (A) {
    g(() => i(A));
  }
}), bm = `avibe-vault-setup:v1:${he.buildHash}:`, S5 = `avibe-vault-setup-result:${he.buildHash}:`, _5 = `avibe-vault-setup-error:${he.buildHash}:`, I5 = 300 * 1e3, vm = 300 * 1e3, dh = 30 * 1e3, B5 = 8 * 1e3, T5 = 5 * 1e3, P5 = 1500, Ke = new Dm();
let uh = !1;
function $5(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return (t.locale === "en" || t.locale === "zh") && (t.theme === "light" || t.theme === "dark");
}
function xm(e) {
  if (!$5(e))
    throw new S("invalid_payload", "appearance must include locale and theme");
  return e;
}
function O5(e) {
  return e == null ? null : xm(e);
}
function Sa(e) {
  const t = dk(e.locale);
  document.documentElement.setAttribute("data-theme", e.theme), t && pk();
}
function km() {
  const e = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  return { locale: uk(), theme: e };
}
function Kt(e) {
  if (typeof e != "object" || e === null)
    throw new S("invalid_payload", "request payload must be an object");
  return e;
}
function Em(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new S("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ce(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function $d(e, t) {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be an object`);
  return e;
}
function C5(e) {
  const t = $d(e, "envelope");
  return {
    ciphertext: Ce(t.ciphertext, "envelope.ciphertext"),
    nonce: Ce(t.nonce, "envelope.nonce"),
    wrap_meta: Ce(t.wrap_meta, "envelope.wrap_meta")
  };
}
function R5(e) {
  const t = $d(e, "material");
  return { name: Ce(t.name, "material.name"), envelope: C5(t.envelope) };
}
function L5(e) {
  const t = e == null ? {} : Kt(e);
  return { wrapMeta: Em(t.wrapMeta, "wrapMeta") };
}
function Am(e) {
  const t = Kt(e), n = !!t.existingProtectedVault;
  if (n && t.rootMetadata !== void 0)
    throw new S("invalid_payload", "vault root metadata cannot be replaced during setup");
  return {
    vaultUserHandle: Ce(t.vaultUserHandle, "vaultUserHandle"),
    displayName: Ce(t.displayName, "displayName"),
    existingProtectedVault: n,
    authzCreationOptions: t.authzCreationOptions,
    ...t.rootMetadata !== void 0 ? { rootMetadata: t.rootMetadata } : {},
    ...t.policy !== void 0 ? { policy: Gl(t.policy) } : {}
  };
}
function H5(e) {
  const t = Kt(e);
  return {
    wrapMeta: Ce(t.wrapMeta, "wrapMeta"),
    ...t.policy !== void 0 ? { policy: Gl(t.policy) } : {}
  };
}
function N5(e) {
  const t = Kt(e);
  if (!Array.isArray(t.items) || t.items.length === 0)
    throw new S("invalid_payload", "approveRelease requires a non-empty items array");
  return { items: t.items.map((n) => K3(n, R5)) };
}
function U5(e) {
  const t = Kt(e);
  if (t.operation !== "delete_secret") throw new S("invalid_payload", "deleteAuthzAssertion only supports delete_secret");
  return {
    challengeId: Ce(t.challengeId, "challengeId"),
    operation: "delete_secret",
    secretName: Ce(t.secretName, "secretName"),
    webauthn: t.webauthn
  };
}
function Sm() {
  if (typeof crypto.randomUUID == "function") return crypto.randomUUID();
  const e = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(e, (t) => t.toString(16).padStart(2, "0")).join("");
}
async function fh(e) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(e)));
}
function D5(e) {
  return new Promise((t, n) => {
    const r = () => {
      n(vr(e));
    };
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function z5(e) {
  for (const t of e)
    if (t.aborted) return vr(t);
  return new Error("operation-superseded");
}
function Gi(e) {
  const t = new AbortController(), n = [], r = (i) => {
    t.signal.aborted || t.abort(vr(i));
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
async function hh(e) {
  const t = Gi(e.abortSignals);
  try {
    await Promise.race([
      ok(ua(e.wrapMeta), Ft(), e.challenge, t.signal).catch((n) => {
        throw t.signal.aborted ? z5(e.abortSignals) : n;
      }),
      ...e.abortSignals.map((n) => D5(n))
    ]);
  } finally {
    t.cleanup();
  }
}
async function _a(e) {
  await dn(async (t) => {
    const n = async (o, s) => {
      const a = Gi([t, ...e.abortSignal ? [e.abortSignal] : []]);
      try {
        const c = await Zi(
          {
            prompt: ph(e),
            wrapMeta: e.wrapMeta,
            passkey: e.passkey
          },
          a.signal,
          o,
          { onKeepOpen: e.keepOpen, knownTarget: s }
        );
        if (e.passkey === "unlock") {
          if (c.kind !== "unlock") throw new Error("authorization unlock result is missing");
          await by({
            wrapMeta: e.wrapMeta,
            currentRpId: Ft(),
            assertion: {
              prfOutput: ve(c.prfOutput),
              prfSalt: ve(c.prfSalt)
            },
            abortSignal: a.signal,
            ...e.policy ? { policy: e.policy } : {},
            ...e.onUnlocked ? { onCommitted: e.onUnlocked } : {}
          });
        } else if (c.kind !== "confirmed")
          throw new Error("authorization result is invalid");
      } finally {
        a.cleanup();
      }
    }, r = e.keptWindow;
    if (r?.isOpen())
      try {
        await n(r.id, r.target);
        return;
      } catch (o) {
        if (!_m(o)) throw o;
      }
    const i = e.authorizationWindow?.();
    if (i) {
      await n(i);
      return;
    }
    await Qr(om(ph(e), "common.continue"), t, void 0, n);
  });
}
function ph(e) {
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
function M5(e) {
  if ("kind" in e && e.kind === "protected-display")
    return {
      ...e,
      rows: e.rows.map(({ copyChallenge: n, ...r }) => ({ ...r, ...n ? { copyChallenge: ce(n) } : {} }))
    };
  if ("kind" in e) return e;
  const t = $3(e.prompt);
  return Og(t), { ...e, prompt: t };
}
function K5(e) {
  const t = Kt(e);
  if (!t) throw new S("invalid_payload", "authorization request must be an object");
  if (t.kind === "protected-hd-wallet") return a5(t);
  if (t.kind === "protected-display") {
    for (const r of Object.keys(t))
      if (!["kind", "name", "rows", "wrapMeta", "notAfter"].includes(r)) throw new S("invalid_payload", "display request has unknown fields");
    if (!Array.isArray(t.rows) || t.rows.length < 1 || t.rows.length > Xc)
      throw new S("invalid_payload", "display rows are invalid");
    if (t.notAfter !== void 0 && (typeof t.notAfter != "number" || !Number.isFinite(t.notAfter)))
      throw new S("invalid_payload", "display notAfter is invalid");
    return {
      kind: "protected-display",
      name: Ce(t.name, "name"),
      wrapMeta: Ce(t.wrapMeta, "wrapMeta"),
      ...t.notAfter !== void 0 ? { notAfter: t.notAfter } : {},
      rows: t.rows.map((r) => {
        const i = $d(r, "row");
        for (const o of Object.keys(i))
          if (!["label", "value", "copyChallenge"].includes(o)) throw new S("invalid_payload", "display row has unknown fields");
        if (typeof i.value != "string") throw new S("invalid_payload", "display value must be a string");
        return {
          ...i.label !== void 0 ? { label: Ce(i.label, "row.label") } : {},
          value: i.value,
          ...i.copyChallenge !== void 0 ? { copyChallenge: ve(Ce(i.copyChallenge, "row.copyChallenge")) } : {}
        };
      })
    };
  }
  if (t.kind === "signing-policy-downgrade") {
    if (Object.keys(t).length !== 2 || !Object.hasOwn(t, "context"))
      throw new S("invalid_payload", "policy authorization request has unknown fields");
    return {
      kind: "signing-policy-downgrade",
      context: dm(t.context, {
        origin: window.location.origin,
        hostname: window.location.hostname
      })
    };
  }
  if (t.kind !== void 0) throw new S("invalid_payload", "authorization request kind is invalid");
  const n = t.passkey;
  if (n !== "none" && n !== "unlock" && n !== "uv")
    throw new S("invalid_payload", "authorization passkey requirement is invalid");
  if (t.keepOpen !== void 0 && t.keepOpen !== !0) throw new S("invalid_payload", "authorization keepOpen is invalid");
  return Og(t.prompt), {
    prompt: R3(t.prompt),
    wrapMeta: Ce(t.wrapMeta, "wrapMeta"),
    passkey: n,
    ...t.keepOpen === !0 ? { keepOpen: !0 } : {}
  };
}
function F5(e) {
  return mh("authorize", e, km());
}
function j5(e, t) {
  const n = Kt(e);
  if (!n) throw new S("authorization_result_invalid", "authorization result is invalid");
  if ("kind" in t) {
    if (t.kind === "protected-hd-wallet") return Ys(n, t);
    if (t.kind === "protected-display") {
      if (n.kind !== "displayed" || Object.keys(n).length !== 1) throw new S("authorization_result_invalid", "display result is invalid");
      return { kind: "displayed" };
    }
    if (t.kind !== "signing-policy-downgrade")
      throw new S("authorization_result_invalid", "authorization request kind is invalid");
    if (n.kind !== "assertion" || !Object.hasOwn(n, "assertion"))
      throw new S("authorization_result_invalid", "policy authorization result kind is invalid");
    return { kind: "assertion", assertion: n.assertion };
  }
  if (n.kind === "confirmed") return { kind: "confirmed" };
  if (n.kind === "unlock")
    return {
      kind: "unlock",
      prfOutput: Ce(n.prfOutput, "result.prfOutput"),
      prfSalt: Ce(n.prfSalt, "result.prfSalt")
    };
  throw new S("authorization_result_invalid", "authorization result kind is invalid");
}
function Zi(e, t, n, r = {}) {
  const { onKeepOpen: i, knownTarget: o, onPosted: s } = r;
  Bi(t);
  const a = !!i && !("kind" in e), c = "kind" in e && e.kind === "protected-hd-wallet" ? e : void 0;
  c && wr(c);
  const l = n ?? Sm(), u = `avibe-vault-authorization-${l}`;
  return new Promise((d, f) => {
    let h = null, p = !1, y = null, w = null, m = null, g = null;
    const v = () => O(() => f(vr(t))), L = () => O(() => f(new S("authorization_cancelled", "requesting sandbox was unloaded", !0))), T = () => new S("authorization_window_closed", "authorization window was closed", !0), C = (I) => {
      h = I, w = setInterval(() => {
        !I.closed || m || (m = setTimeout(() => O(() => f(T())), 2500));
      }, 500);
    }, H = (I) => {
      if (I.origin !== window.location.origin) return;
      const U = I.data;
      if (!(!U || U.id !== l || !Lr(U.build)) && (!h && n && U.type === "authorization-ready" && I.source && (g && clearTimeout(g), g = null, C(I.source)), I.source === h))
        if (U.type === "authorization-ready") {
          g && clearTimeout(g), g = null;
          try {
            h?.postMessage(
              {
                type: "authorization-request",
                build: he,
                id: l,
                request: M5(a && !("kind" in e) ? { ...e, keepOpen: !0 } : e)
              },
              window.location.origin
            ), s?.();
          } catch (z) {
            O(() => f(z));
          }
        } else if (U.type === "authorization-approved")
          try {
            const z = j5(U.result, e);
            if (a && i) {
              const E = h, k = {
                id: l,
                target: E,
                isOpen: () => !E.closed,
                dismiss: () => {
                  try {
                    E.closed || E.postMessage({ type: "authorization-dismiss", build: he, id: l }, window.location.origin);
                  } catch {
                  }
                }
              };
              O(() => {
                i(k), d(z);
              }, !0);
            } else O(() => d(z));
          } catch (z) {
            O(() => f(z));
          }
        else U.type === "authorization-error" && O(() => f(new S(U.code, U.message, !!U.retryable)));
    }, O = (I, U = !1) => {
      if (!p) {
        p = !0, y && clearTimeout(y), w && clearInterval(w), m && clearTimeout(m), g && clearTimeout(g), window.removeEventListener("message", H), window.removeEventListener("pagehide", L), t?.removeEventListener("abort", v);
        try {
          !U && h && !h.closed && h.close();
        } catch {
        }
        I();
      }
    };
    if (window.addEventListener("message", H), window.addEventListener("pagehide", L, { once: !0 }), n && o) {
      if (o.closed) {
        O(() => f(T()));
        return;
      }
      C(o), g = setTimeout(() => O(() => f(T())), dh);
    } else if (n)
      g = setTimeout(() => O(() => f(T())), dh);
    else {
      const I = window.open(F5(l), u, "popup,width=520,height=760");
      if (!I) {
        O(() => f(new S("authorization_popup_blocked", "authorization window was blocked", !0)));
        return;
      }
      I.focus(), C(I);
    }
    if (y = setTimeout(
      () => O(() => f(new S("authorization_window_timeout", "authorization window timed out", !0))),
      c ? wr(c) : vm
    ), t) {
      if (t.aborted) {
        v();
        return;
      }
      t.addEventListener("abort", v, { once: !0 });
    }
  });
}
function V5(e) {
  return e === 0 ? "context.oneTime" : e % 60 === 0 ? Oe("context.minutes", { count: e / 60 }) : Oe("context.seconds", { count: e });
}
function Yi(e, t, n = "access") {
  const r = [];
  e.sessionLabel && r.push({ label: "context.session", value: me(e.sessionLabel) }), e.command && r.push({ label: "context.command", value: me(e.command), variant: "command" }), e.egress && r.push({ label: n === "sign" ? "context.signReturns" : "context.egress", value: me(e.egress) });
  const i = [
    ...e.source?.env?.map((o) => me(`${Y(n === "sign" ? "context.sourceRequestedName" : "context.sourceEnv")}: ${o}`)) ?? [],
    ...e.source?.tags?.map((o) => me(`${Y("context.sourceTag")}: ${o}`)) ?? [],
    ...e.source?.skills?.map((o) => me(`${Y("context.sourceSkill")}: ${o}`)) ?? []
  ];
  return i.length > 0 && r.push({ label: "context.source", values: i, variant: "chips" }), t?.agentFingerprint && r.push({ label: "context.agent", value: me(t.agentFingerprint), variant: "mono" }), t?.grantId && r.push({ label: "context.grant", value: me(t.grantId), variant: "mono" }), e.grantTtlSeconds !== void 0 && r.push({ label: "context.agentAccess", value: V5(e.grantTtlSeconds) }), r.push({
    label: "context.secrets",
    values: e.secrets.map(
      (o) => me(`${o.name} · ${Y(o.kind === "static" ? "context.kindStatic" : "context.kindKeypair")}`)
    ),
    variant: "chips",
    tone: "warning"
  }), { rows: r };
}
function _m(e) {
  return e instanceof S && e.code === "authorization_window_closed";
}
async function q5(e, t, n, r) {
  let i = !1;
  const o = () => {
    i = !0, r();
  }, s = (a) => i && (a instanceof S && (a.code === "authorization_window_closed" || a.code === "authorization_window_timeout") || a instanceof Error && a.message === "operation-superseded");
  if (!(t?.isOpen() && await dn(async (c) => {
    const l = Gi([c, n]);
    try {
      return await Zi(e, l.signal, t.id, { knownTarget: t.target, onPosted: o }), !0;
    } catch (u) {
      if (s(u)) return !0;
      if (i || !_m(u)) throw u;
      return !1;
    } finally {
      l.cleanup();
    }
  })))
    try {
      await dn(async (a) => {
        const c = Gi([a, n]);
        try {
          await Qr(
            { title: "reveal.openWindowTitle", subtitle: me(e.name), body: e.rows.length === 1 ? "reveal.openWindowBodyOne" : "reveal.openWindowBody", confirmLabel: "reveal.openWindow" },
            c.signal,
            void 0,
            async () => {
              try {
                await Zi(e, c.signal, void 0, { onPosted: o });
              } catch (l) {
                if (!s(l)) throw l;
              }
            }
          );
        } finally {
          c.cleanup();
        }
      });
    } catch (a) {
      if (!s(a)) throw a;
    }
}
function W5(e) {
  return e instanceof Error && e.message === "vault-locked";
}
function G5(e) {
  return e instanceof Error && e.message === "vault-operation-aborted";
}
function Im(e, t) {
  return W5(e) ? !0 : G5(e) && ft(t).state === "locked";
}
async function Z5(e, t = Le(), n) {
  await my({ wrapMeta: e, currentRpId: Ft(), policy: t, abortSignal: n });
}
async function Y5(e, t = Le()) {
  await dn((n) => Z5(e, t, n));
}
async function Od(e) {
  let t;
  try {
    await _a({
      ...e,
      passkey: "unlock",
      ...e.abortSignal ? { onUnlocked: (n) => {
        t = n, e.onUnlocked?.(n);
      } } : {}
    });
  } catch (n) {
    throw e.abortSignal ? (t?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), t?.release()) : ft(e.wrapMeta).state === "unlocked" && ht({ broadcast: !0, reason: "manual-lock" }), n;
  } finally {
    e.onUnlocked || t?.release();
  }
}
async function X5(e, t) {
  if (ft(e).state !== "unlocked") {
    if (!e) throw new Error("vault-locked");
    await Y5(e);
  }
  return await wa(t, { renewOnSuccess: !0, ...e ? { expectedWrapMeta: e } : {} });
}
async function Bm(e) {
  const t = ft(e.bindRoot ? void 0 : e.wrapMeta).state, n = Le(), r = am({ tier: e.tier, vaultState: t, policy: n });
  let i = !1, o;
  const s = async () => {
    const c = e.bindRoot ? pa(e.wrapMeta) : void 0;
    try {
      await Od({
        ...e.lockedPrompt,
        wrapMeta: e.wrapMeta,
        policy: n,
        ...c ? { abortSignal: c.signal, onUnlocked: (l) => {
          o = l;
        } } : {},
        ...e.keepOpen ? { keepOpen: e.keepOpen } : {},
        parentSurface: e.parentSurface,
        authorizationWindow: e.authorizationWindow
      }), i = !0;
    } finally {
      c?.release();
    }
  }, a = async (c, l = !1) => {
    let u = !1;
    try {
      o?.assertCurrent();
      const d = await wa(
        async (f, h, p) => {
          const y = await e.buildPrompt(f, h, p);
          return r.confirm && !l && (await _a({
            ...y,
            wrapMeta: h,
            passkey: c,
            abortSignal: p.signal,
            ...e.keepOpen ? { keepOpen: e.keepOpen } : {},
            parentSurface: e.parentSurface,
            authorizationWindow: e.authorizationWindow
          }), p.assertCurrent()), e.operation(f, h, p);
        },
        {
          renewOnSuccess: r.renewOnSuccess,
          beforeSuccess: e.beforeSuccess,
          ...e.bindRoot ? { expectedWrapMeta: e.wrapMeta } : {}
        }
      );
      return u = !0, d;
    } catch (d) {
      throw !e.bindRoot && i && !u && ft().state === "unlocked" && ht({ broadcast: !0, reason: "manual-lock" }), d;
    }
  };
  try {
    return r.passkey === "unlock" ? (await s(), await a("none", !0)) : await a(r.passkey === "uv" ? "uv" : "none");
  } catch (c) {
    if (!e.bindRoot && r.passkey !== "unlock" && Im(c, e.wrapMeta))
      return await s(), await a("none", !0);
    throw e.bindRoot && o?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), c;
  } finally {
    o?.release();
  }
}
function it(e, t) {
  if (e instanceof S) return e;
  if (ly(e)) return new S("passkey_cancelled", "passkey-cancelled", !0);
  const n = e instanceof Error ? e.message : t;
  switch (n) {
    case "passkey-cancelled":
      return new S("passkey_cancelled", n, !0);
    case "passkey-prf-unavailable":
      return new S("passkey_prf_unavailable", n);
    case "passkey-not-configured":
      return new S("passkey_not_configured", n);
    case "passkey-multiple-not-supported":
      return new S("passkey_multiple_not_supported", n);
    case "vault-operation-aborted":
      return new S("vault_operation_aborted", n, !0);
    default:
      return new S(t, n);
  }
}
function J5(e) {
  return ce(new TextEncoder().encode(JSON.stringify(e))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function Q5(e) {
  return Am(JSON.parse(new TextDecoder().decode(ve(e))));
}
function e6(e, t) {
  return mh("setup", e, km(), { req: J5(t) });
}
function pc(e) {
  const t = Kt(e);
  return {
    credentialId: Ce(t.credentialId, "credentialId"),
    ...t.authzRegistration !== void 0 ? { authzRegistration: t.authzRegistration } : {}
  };
}
function Ia(e) {
  return `${S5}${e}`;
}
function Ba(e) {
  return `${_5}${e}`;
}
function wh(e) {
  try {
    localStorage.removeItem(Ia(e)), localStorage.removeItem(Ba(e));
  } catch {
  }
}
function t6(e, t) {
  try {
    localStorage.setItem(Ia(e), JSON.stringify({ build: he, result: t })), localStorage.removeItem(Ba(e));
  } catch {
  }
}
function n6(e, t) {
  try {
    localStorage.setItem(
      Ba(e),
      JSON.stringify({ build: he, code: t.code, message: t.message, retryable: t.retryable })
    ), localStorage.removeItem(Ia(e));
  } catch {
  }
}
function Tm(e) {
  if (window.self === window.top)
    return Ql({
      rpId: Ft(),
      vaultUserHandle: e.vaultUserHandle,
      displayName: e.displayName,
      authzCreationOptions: e.authzCreationOptions
    });
  const t = Sm(), n = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${bm}${t}`);
  let r = null;
  return wh(t), new Promise((i, o) => {
    let s = !1, a = null, c = null, l = null;
    const u = () => d(() => o(new S("setup_window_closed", "requesting sandbox was unloaded", !0))), d = (y) => {
      if (!s) {
        s = !0, a && clearTimeout(a), c && clearInterval(c), l && clearTimeout(l), window.removeEventListener("visibilitychange", h), window.removeEventListener("focus", h), window.removeEventListener("storage", h), window.removeEventListener("message", p), window.removeEventListener("pagehide", u), n?.close(), wh(t);
        try {
          r?.close();
        } catch {
        }
        y();
      }
    }, f = () => {
      let y = null, w = null;
      try {
        y = localStorage.getItem(Ia(t)), w = localStorage.getItem(Ba(t));
      } catch {
        return !1;
      }
      if (y) {
        try {
          const m = Kt(JSON.parse(y));
          if (!Lr(m.build)) throw new S("sandbox_build_mismatch");
          const g = pc(m.result);
          d(() => i(g));
        } catch (m) {
          const g = m instanceof Error ? m.message : "stored setup result is invalid";
          d(() => o(new S("setup_storage_invalid", g)));
        }
        return !0;
      }
      if (w) {
        try {
          const m = Kt(JSON.parse(w));
          if (!Lr(m.build)) throw new S("sandbox_build_mismatch");
          d(
            () => o(new S(Ce(m.code, "code"), Em(m.message, "message"), !!m.retryable))
          );
        } catch (m) {
          const g = m instanceof Error ? m.message : "stored setup error is invalid";
          d(() => o(new S("setup_storage_invalid", g)));
        }
        return !0;
      }
      return !1;
    }, h = () => {
      f();
    }, p = (y) => {
      if (y.origin !== window.location.origin || y.source !== r) return;
      const w = y.data;
      !w || w.id !== t || !Lr(w.build) || (w.type === "setup-created" ? d(() => i(pc(w.result))) : w.type === "setup-error" && d(() => o(new S(w.code, w.message, !!w.retryable))));
    };
    if (a = setTimeout(() => {
      f() || d(() => o(new S("setup_window_timeout", "setup window timed out", !0)));
    }, I5), c = setInterval(() => {
      !r?.closed || l || f() || (l = setTimeout(() => {
        f() || d(() => o(new S("setup_window_closed", "setup window was closed", !0)));
      }, 2500));
    }, 500), n && (n.onmessage = (y) => {
      const w = y.data;
      !w || w.id !== t || !Lr(w.build) || (w.type === "setup-created" ? d(() => i(pc(w.result))) : w.type === "setup-error" && d(() => o(new S(w.code, w.message, !!w.retryable))));
    }), window.addEventListener("visibilitychange", h), window.addEventListener("focus", h), window.addEventListener("storage", h), window.addEventListener("message", p), window.addEventListener("pagehide", u, { once: !0 }), r = window.open(e6(t, e), "avibe-vault-setup", "popup,width=440,height=620"), !r) {
      d(() => o(new S("setup_popup_blocked", "setup window was blocked", !0)));
      return;
    }
    r.focus();
  });
}
function vr(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function Bi(e) {
  if (e?.aborted) throw vr(e);
}
function r6() {
  const e = window.navigator;
  return /iP(?:hone|ad|od)/.test(e.userAgent) || e.platform === "MacIntel" && e.maxTouchPoints > 1;
}
function yh(e) {
  return r6() && cy(e);
}
async function Pm(e, t, n, r, i = Le()) {
  const o = Jx();
  let s, a;
  try {
    Bi(r), s = (await ga([{ credentialId: n.credentialId, prfSalt: o }], t, r)).prfOutput, Bi(r), a = Xx();
    const l = await t2(a, [{ kind: "passkey", prfOutput: s, prfSalt: o, credentialId: n.credentialId }]);
    Bi(r);
    let u = r2(l, {
      rpId: t,
      vaultUserHandle: e.vaultUserHandle
    });
    e.rootMetadata && (u = s2(u, await o2(a, e.rootMetadata, u)), Bi(r));
    const d = ty({
      vmk: a,
      wrapMeta: u,
      freshSetup: !e.existingProtectedVault,
      scopeId: M2(e.vaultUserHandle),
      policy: i
    });
    return a = void 0, {
      wrapMeta: u,
      rpId: t,
      credentialId: n.credentialId,
      state: d.state,
      expiresAt: d.expiresAt,
      ...n.authzRegistration ? { authzRegistration: n.authzRegistration } : {}
    };
  } finally {
    s?.fill(0), o.fill(0), a?.fill(0);
  }
}
function i6(e, t, n) {
  return dn((r) => {
    const i = eo(
      "setup.title",
      "setup.subtitle",
      "setup.iframeBody"
    ), o = mt("setup.createPasskey"), s = zs("setup.ready");
    return te(i.card, o), te(i.card, s), new Promise((a, c) => {
      let l = !1, u = "create", d = null, f = !1;
      const h = (T) => {
        l || (l = !0, r.removeEventListener("abort", w), ma(), T());
      }, p = (T) => {
        h(() => c(T));
      }, y = (T) => {
        l || (u = "create", d = null, J(o, "setup.createPasskey"), o.disabled = !1, J(s, T));
      }, w = () => {
        p(vr(r));
      }, m = (T) => {
        if (T instanceof S && T.code === "setup_popup_blocked") {
          y("setup.popupBlocked");
          return;
        }
        p(T);
      }, g = () => {
        f = !0, o.disabled = !0, J(s, "setup.popupPrompt");
        let T;
        try {
          T = Tm(e);
        } catch (C) {
          m(C);
          return;
        }
        T.then(
          (C) => {
            l || (d = C, u = "finish", J(o, "setup.finish"), o.disabled = !1, J(s, "setup.createdFinish"));
          },
          (C) => {
            m(C);
          }
        );
      }, v = (T) => {
        o.disabled = !0, J(s, "setup.passkeyPrompt"), Pm(e, t, T, r, n).then(
          (C) => h(() => a(C)),
          (C) => p(C)
        );
      }, L = () => {
        o.disabled = !0, J(s, "setup.passkeyPrompt");
        let T;
        try {
          T = Ql({
            rpId: t,
            vaultUserHandle: e.vaultUserHandle,
            displayName: e.displayName,
            authzCreationOptions: e.authzCreationOptions
          });
        } catch (C) {
          if (yh(C)) {
            g();
            return;
          }
          p(C);
          return;
        }
        T.then(
          (C) => v(C),
          (C) => {
            if (yh(C)) {
              g();
              return;
            }
            p(C);
          }
        );
      };
      if (r.aborted) {
        w();
        return;
      }
      r.addEventListener("abort", w, { once: !0 }), o.addEventListener("click", () => {
        if (!(l || o.disabled)) {
          if (u === "finish" && d) {
            v(d);
            return;
          }
          if (f) {
            g();
            return;
          }
          L();
        }
      });
    });
  });
}
async function o6(e) {
  const t = Am(e);
  try {
    const n = Ft(), r = t.policy ?? Le(), i = window.self !== window.top ? await i6(t, n, r) : await Pm(t, n, await Tm(t), void 0, r), o = t.policy ? Zl(r) : Le();
    return { ...i, policy: o };
  } catch (n) {
    throw it(n, "setup_failed");
  }
}
async function s6(e) {
  const t = H5(e);
  try {
    const n = Ft(), r = t.policy ?? Le(), i = await dn(async (s) => {
      let a;
      if (await Qr(
        {
          title: "unlock.sessionTitle",
          subtitle: Oe("unlock.sessionSubtitle", {
            minutes: r.windowSeconds / 60
          }),
          body: "unlock.sessionBody",
          confirmLabel: "common.continue"
        },
        s,
        void 0,
        async () => {
          a = await my({ wrapMeta: t.wrapMeta, currentRpId: n, abortSignal: s, policy: r });
        }
      ), !a) throw new Error("vault unlock did not complete");
      return a;
    }), o = t.policy ? Zl(r) : Le();
    return { ...i, policy: o };
  } catch (n) {
    throw it(n, "unlock_failed");
  }
}
async function a6(e) {
  const t = Rk(e);
  t.wrapMeta && Jl(t.wrapMeta);
  try {
    if (t.kind === "static" && !Le().parentValueSealAllowed)
      throw new S("parent_value_seal_disabled", "parent-value sealing is disabled by policy");
    return await X5(t.wrapMeta, async (n, r) => ({ ...t.kind === "static" ? await z3({ name: t.name, value: t.value, vmk: n, wrapMeta: r }) : await M3({ name: t.name, curve: t.curve, vmk: n, wrapMeta: r }), establishingVmk: Rc() }));
  } catch (n) {
    throw it(n, "seal_failed");
  }
}
async function c6(e, t) {
  try {
    const n = ym(e), r = t.takeAuthorizationWindow();
    if (!r) throw new S("invalid_payload", "sealHdWallet requires a parent-opened authorization window");
    const i = pa(n.wrapMeta, { surviveAutoLock: !0 });
    let o = !1;
    const s = { ...n, kind: "protected-hd-wallet", deadlineAt: Date.now() + wm };
    try {
      const a = await dn(async (c) => {
        const l = Gi([c, i.signal]);
        try {
          i.assertCurrent();
          const u = await Zi(s, l.signal, r);
          i.assertCurrent();
          const d = Ys(u, s);
          return i.assertCurrent(), { envelope: d.envelope, accounts: d.accounts, expectedAccount0: d.expectedAccount0, establishingVmk: i.freshSetup };
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
async function l6(e, t) {
  const n = r5(e);
  let r, i = !1;
  try {
    const o = n.items.map((d) => {
      const f = Vn(d.material.envelope);
      if (!Pd(f.recordMetadata)) throw new Error("only static protected records can be revealed as plaintext");
      return od({ context: d.context, expectedPurpose: "reveal", secret: { name: d.material.name, kind: "static" } }), { item: d, ...f };
    }), s = o[0].vmkWrapMeta;
    if (o.some((d) => d.vmkWrapMeta !== s)) throw new S("invalid_payload", "reveal items must share one VMK context");
    const a = n.items.map((d) => d.context), c = (d) => {
      const { secrets: f, ...h } = d.display;
      return ye(h);
    };
    if (a.some((d) => c(d) !== c(a[0])))
      throw new S("invalid_payload", "reveal items must share one request display");
    const l = { ...a[0].display, secrets: a.flatMap((d) => d.display.secrets) }, u = (d) => {
      for (const f of n.items)
        Ks({ context: f.context, rootMetadata: d, expectedPurpose: "reveal" }), um(f);
    };
    return await Bm({
      tier: "R2",
      wrapMeta: s,
      bindRoot: !0,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      keepOpen: (d) => {
        r?.dismiss(), r = d;
      },
      lockedPrompt: {
        title: n.items.length > 1 ? "reveal.showSecretTitle" : "reveal.showConfirmTitle",
        subtitle: me(n.secret),
        body: n.items.length > 1 ? "reveal.confirmSecretBody" : "reveal.confirmBody",
        details: Yi(l),
        label: "common.continue"
      },
      buildPrompt: async (d, f) => (u(await Ts(f, d)), ti(a), {
        title: n.items.length > 1 ? "reveal.showSecretTitle" : "reveal.showConfirmTitle",
        subtitle: me(n.secret),
        body: n.items.length > 1 ? "reveal.confirmSecretBody" : "reveal.confirmBody",
        details: Yi(l),
        challenge: await fh(a.map((h) => ba(h)).join(`
`)),
        label: "common.continue"
      }),
      operation: async (d, f, h) => {
        const p = await Ts(f, d);
        h.assertCurrent(), u(p), ti(a);
        const y = [];
        try {
          for (const L of o)
            y.push(await li(L.sealed, d, fa(L.item.material.name, L.recordMetadata))), h.assertCurrent();
          u(p), await Mi(a), h.assertCurrent(), u(p);
          const w = r;
          r = void 0;
          const m = Le().strictApprovals, g = [];
          for (const [L, T] of o.entries()) {
            const C = T.item.field ?? n.defaultFieldLabel;
            g.push({
              ...C !== void 0 ? { label: C } : {},
              value: new TextDecoder().decode(y[L]),
              ...m ? { copyChallenge: await fh(`reveal-copy:${T.item.context.requestId}:${T.item.material.name}`) } : {}
            });
          }
          const v = ft().expiresAt;
          await q5({ kind: "protected-display", name: n.secret, rows: g, wrapMeta: f, ...v !== void 0 ? { notAfter: v } : {} }, w, h.signal, () => {
            i = !0;
          });
        } finally {
          for (const w of y) w.fill(0);
        }
        return { completed: !0 };
      }
    });
  } catch (o) {
    if (i && o instanceof Error && ["vault-operation-aborted", "operation-superseded"].includes(o.message)) return { completed: !0 };
    throw it(o, "reveal_failed");
  } finally {
    r?.dismiss();
  }
}
async function d6(e, t) {
  try {
    const n = P3(e), { request: r, unpacked: { vmkWrapMeta: i } } = n, o = (s) => {
      const a = r.context.display.sessionLabel;
      if (!s.review && s.display === void 0) throw new S("invalid_payload", "signing request has no reviewable content");
      return {
        title: "sign.title",
        subtitle: me(r.material.name),
        ...s.review ? { review: s.review, ...a ? { reviewSession: a } : {} } : { body: me(s.display ?? "") },
        details: Yi(r.context.display, void 0, "sign"),
        label: "sign.confirm"
      };
    };
    if (n.hd || Wn(r.signingContext)) {
      const s = !Le().strictApprovals;
      let a, c;
      try {
        if (ft().state !== "unlocked") {
          const l = pa(i);
          try {
            await Od({
              title: n.hd ? "hd.unlockTitle" : "unlock.sessionTitle",
              subtitle: me(r.material.name),
              body: s ? n.hd ? "hd.unlockBody" : "sign.unlockBody" : n.hd ? "hd.unlockBodyStrict" : "unlock.sessionBody",
              label: "common.continue",
              wrapMeta: i,
              policy: Le(),
              abortSignal: l.signal,
              onUnlocked: (u) => {
                a = u;
              },
              ...s ? { keepOpen: (u) => {
                c = u;
              } } : {},
              parentSurface: t.latestSurface,
              authorizationWindow: t.takeAuthorizationWindow
            });
          } finally {
            l.release();
          }
        }
        return a?.assertCurrent(), await wa(async (l, u, d) => {
          const f = await Wf(n, l, u);
          d.assertCurrent();
          const h = s && a !== void 0 && c !== void 0, p = c;
          return c = void 0, await _a({
            ...o(f),
            wrapMeta: u,
            ...h && p ? { passkey: "none", keptWindow: p } : { passkey: "uv", challenge: new Uint8Array(n.challenge), authorizationWindow: t.takeAuthorizationWindow },
            abortSignal: d.signal,
            parentSurface: t.latestSurface
          }), h && a.assertCurrent(), d.assertCurrent(), Gf(n, l, u, d.assertCurrent);
        }, { renewOnSuccess: !1, expectedWrapMeta: i, beforeSuccess: () => Mi([r.context]) });
      } catch (l) {
        throw a?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), l;
      } finally {
        c?.dismiss(), a?.release();
      }
    }
    return await Bm({
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
      beforeSuccess: () => Mi([r.context])
    });
  } catch (n) {
    throw it(n, "sign_failed");
  }
}
async function u6(e, t) {
  const n = N5(e);
  try {
    const i = Vn(n.items[0].material.envelope).vmkWrapMeta;
    for (const f of n.items) {
      const h = Vn(f.material.envelope);
      if (h.vmkWrapMeta !== i) throw new S("invalid_payload", "approveRelease items must share one VMK context");
      if (!Pd(h.recordMetadata)) throw new S("invalid_payload", "approveRelease only supports static protected records");
    }
    const o = cm(n.items), s = ft(i).state, a = Le(), c = am({ tier: "R2", vaultState: s, policy: a });
    let l = !1;
    const u = async () => {
      await Od({
        title: "release.title",
        subtitle: Oe("release.subtitle", { count: n.items.length }),
        details: Yi(o.display, o.recipient),
        label: "release.confirm",
        wrapMeta: i,
        policy: a,
        parentSurface: t.latestSurface,
        authorizationWindow: t.takeAuthorizationWindow
      }), l = !0;
    }, d = async (f, h = !1) => {
      let p = !1, y;
      try {
        const w = await wa(
          async (m, g, v) => {
            const L = await F3({
              items: n.items,
              vmk: m,
              wrapMeta: g,
              consumeReplayIds: !1,
              onApprovalAccepted: (T) => {
                y = T;
              },
              confirm: (T) => !c.confirm || h ? Promise.resolve() : _a({
                title: "release.title",
                subtitle: Oe("release.subtitle", { count: n.items.length }),
                details: Yi(T.display, T.recipient),
                challenge: T.challenge,
                label: "release.confirm",
                wrapMeta: g,
                passkey: f,
                abortSignal: v.signal,
                parentSurface: t.latestSurface,
                authorizationWindow: t.takeAuthorizationWindow
              })
            });
            return v.assertCurrent(), L;
          },
          {
            renewOnSuccess: c.renewOnSuccess,
            beforeSuccess: () => Mi(n.items.map((m) => m.context), y)
          }
        );
        return p = !0, w;
      } catch (w) {
        throw l && !p && ft().state === "unlocked" && ht({ broadcast: !0, reason: "manual-lock" }), w;
      }
    };
    try {
      return c.passkey === "unlock" ? (await u(), await d("none", !0)) : await d(c.passkey === "uv" ? "uv" : "none");
    } catch (f) {
      if (c.passkey !== "unlock" && Im(f, i))
        return await u(), await d("none", !0);
      throw f;
    }
  } catch (r) {
    throw it(r, "approve_release_failed");
  }
}
async function f6(e, t) {
  let n;
  if (await dn(async (r) => {
    Kn(e);
    const i = { kind: "signing-policy-downgrade", context: e }, o = async (c) => {
      Kn(e);
      const l = await Zi(i, r, c);
      if (l.kind !== "assertion") throw new S("authorization_result_invalid", "policy assertion is missing");
      n = Yc(l.assertion, e);
    }, s = t.takeAuthorizationWindow();
    if (s) {
      await o(s);
      return;
    }
    Kn(e);
    const a = dy(e);
    await Qr(
      a,
      r,
      (c) => sm({
        uiShowPending: py,
        parentSurface: () => t.latestSurface(),
        visibilityTarget: c,
        requireParentSurface: !0
      }),
      () => o()
    );
  }), !n) throw new S("authorization_result_invalid", "policy assertion is missing");
  return Yc(n, e);
}
async function h6(e, t) {
  try {
    const n = Y3(e, {
      origin: window.location.origin,
      hostname: window.location.hostname
    }), r = await f6(n, t);
    return { challengeId: n.challengeId, assertion: r };
  } catch (n) {
    throw it(n, "policy_authorization_failed");
  }
}
async function p6(e) {
  const t = U5(e);
  try {
    const n = await sk(t.webauthn);
    return { challengeId: t.challengeId, assertion: n };
  } catch (n) {
    throw it(n, "delete_authz_failed");
  }
}
function w6() {
  const e = Qc();
  if (e.get("mode") !== "setup") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Sa({ locale: t, theme: n });
  const r = e.get("id"), i = document.getElementById("page"), o = i?.querySelector(".card"), s = i?.querySelector("h1"), a = i?.querySelector(".sub"), c = i?.querySelector(".body");
  if (!r || !o || !s || !a || !c) return;
  document.body.classList.add("setup-mode"), J(s, "setup.title"), J(a, "setup.subtitle"), J(c, "setup.topLevelBody");
  const l = document.createElement("button");
  l.type = "button", l.className = "action", J(l, "setup.createPasskey");
  const u = document.createElement("p");
  u.className = "setup-status", J(u, "setup.readyOnOrigin"), o.append(l, u);
  const d = new URLSearchParams(window.location.hash.slice(1)).get("req");
  if (!d) {
    l.disabled = !0, J(u, "setup.requestMissing");
    return;
  }
  let f;
  try {
    f = Q5(d);
  } catch {
    l.disabled = !0, J(u, "setup.requestInvalid");
    return;
  }
  const h = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${bm}${r}`), p = (y) => {
    const w = it(y, "setup_create_failed"), m = {
      type: "setup-error",
      build: he,
      id: r,
      code: w.code,
      message: w.message,
      retryable: w.retryable
    };
    n6(r, w), h?.postMessage(m);
    try {
      window.opener?.postMessage(m, window.location.origin);
    } catch {
    }
    l.disabled = !1, J(u, me(w.message));
  };
  l.addEventListener("click", () => {
    l.disabled = !0, J(u, "setup.passkeyPrompt");
    let y;
    try {
      y = Ql({
        rpId: Ft(),
        vaultUserHandle: f.vaultUserHandle,
        displayName: f.displayName,
        authzCreationOptions: f.authzCreationOptions
      });
    } catch (w) {
      p(w);
      return;
    }
    y.then((w) => {
      const m = { type: "setup-created", build: he, id: r, result: w };
      t6(r, w), h?.postMessage(m);
      try {
        window.opener?.postMessage(m, window.location.origin);
      } catch {
      }
      J(u, "setup.created"), setTimeout(() => window.close(), 250);
    }).catch(p);
  });
}
function y6() {
  const e = Qc();
  if (e.get("mode") !== "authorize") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Sa({ locale: t, theme: n });
  const r = e.get("id"), o = document.getElementById("page")?.querySelector(".card"), s = o?.querySelector(".body");
  if (!r || !o || !s || (s.classList.add("waiting"), J(s, window.opener ? "authorize.waiting" : "authorize.waitingLong"), !window.opener)) return;
  const a = () => {
    const g = window.opener;
    if (!g) return [];
    const v = [g];
    try {
      for (let L = 0; L < g.frames.length; L += 1) v.push(g.frames[L]);
    } catch {
    }
    return v;
  };
  let c = null;
  const l = (g) => {
    try {
      c?.postMessage(g, window.location.origin);
    } catch {
    }
  };
  let u = !1, d = !1, f = null, h = null;
  const p = setTimeout(() => J(s, "authorize.waitingLong"), B5), y = () => {
    if (!u)
      for (const g of d ? c ? [c] : [] : a())
        try {
          g.postMessage({ type: "authorization-ready", build: he, id: r }, window.location.origin);
        } catch {
        }
  }, w = (g) => {
    if (u) return;
    u = !0, h && clearInterval(h), f && clearTimeout(f), clearTimeout(p), s.classList.remove("waiting"), document.body.classList.add("setup-mode");
    const v = new AbortController(), L = d;
    if (!(!L || ("kind" in g ? g.kind === "protected-display" : g.passkey === "none" && !g.keepOpen))) {
      l({ type: "authorization-error", build: he, id: r, code: "authorization_request_invalid", message: "authorization follow-up is not allowed", retryable: !1 }), setTimeout(() => window.close(), 250);
      return;
    }
    const C = !L && !("kind" in g) && g.keepOpen === !0;
    C || window.removeEventListener("message", m);
    const H = (E) => {
      const k = it(E, E instanceof Error && E.message === "operation-cancelled" ? "authorization_cancelled" : "authorization_failed");
      l({
        type: "authorization-error",
        build: he,
        id: r,
        code: k.code,
        message: k.message,
        retryable: k.retryable
      }), setTimeout(() => window.close(), 250);
    }, O = (E, k) => {
      E.then(
        () => {
          try {
            l({ type: "authorization-approved", build: he, id: r, result: k() }), C ? I() : setTimeout(() => window.close(), 250);
          } catch (x) {
            H(x);
          }
        },
        H
      );
    }, I = () => {
      d = !0, u = !1;
      const E = eo("authorize.continuingTitle", "kind" in g ? me("") : g.prompt.subtitle);
      E.body.classList.add("waiting"), J(E.body, "authorize.continuing"), f = setTimeout(() => {
        u = !0, h && clearInterval(h), window.removeEventListener("message", m), E.body.classList.remove("waiting"), J(E.body, "authorize.continueInAvibe"), setTimeout(() => window.close(), P5);
      }, T5), y(), h = setInterval(y, 500);
    };
    if ("kind" in g && g.kind === "protected-display") {
      const E = Math.min(Date.now() + vm, g.notAfter ?? Number.POSITIVE_INFINITY);
      if (E <= Date.now()) {
        H(new S("authorization_window_timeout", "the unlocked session already ended", !0));
        return;
      }
      const k = () => v.abort(new S("authorization_window_timeout", "secure window display expired", !0)), x = () => {
        Date.now() >= E && k();
      }, b = () => v.abort(new S("authorization_window_closed", "secure window was closed", !0)), _ = setTimeout(k, Math.max(0, E - Date.now()));
      window.addEventListener("pagehide", b, { once: !0 });
      for (const A of ["pageshow", "focus", "visibilitychange"]) window.addEventListener(A, x);
      const $ = o5({
        name: g.name,
        abortSignal: v.signal,
        rows: g.rows.map((A) => {
          const B = A.copyChallenge;
          return {
            label: A.label,
            value: new TextEncoder().encode(A.value),
            ...B ? {
              prepareCopy: async (P) => {
                await hh({ wrapMeta: g.wrapMeta, challenge: B, abortSignals: [P, v.signal] });
              }
            } : {}
          };
        })
      }).finally(() => {
        clearTimeout(_), window.removeEventListener("pagehide", b);
        for (const A of ["pageshow", "focus", "visibilitychange"]) window.removeEventListener(A, x);
      });
      O($, () => ({ kind: "displayed" }));
      return;
    }
    if ("kind" in g) {
      if (g.kind === "protected-hd-wallet") {
        let x;
        const b = () => v.abort(new S("authorization_cancelled", "protected HD window closed", !0)), _ = setTimeout(() => v.abort(new S("authorization_window_timeout", "protected HD window expired", !0)), wr(g));
        window.addEventListener("pagehide", b, { once: !0 });
        const $ = c5(g, v.signal, A5).then((A) => {
          x = A;
        }).catch((A) => {
          throw ly(A) || A instanceof Error && ["operation-cancelled", "passkey-cancelled"].includes(A.message) ? new S("authorization_cancelled", "protected HD operation cancelled", !0) : A;
        }).finally(() => {
          clearTimeout(_), window.removeEventListener("pagehide", b);
        });
        O($, () => {
          if (v.signal.aborted) throw vr(v.signal);
          if (wr(g), !x) throw new S("authorization_result_invalid", "protected HD result is missing");
          return x;
        });
        return;
      }
      if (g.kind !== "signing-policy-downgrade") {
        H(new S("authorization_request_invalid", "authorization request kind is invalid"));
        return;
      }
      try {
        Kn(g.context);
      } catch (x) {
        H(x);
        return;
      }
      let E;
      const k = dy(g.context);
      O(
        Qr(
          k,
          v.signal,
          (x) => sm({
            uiShowPending: py,
            visibilityTarget: x,
            requireParentSurface: !0
          }),
          async () => {
            Kn(g.context), E = await J3(g.context, v.signal);
          }
        ),
        () => {
          if (!E) throw new S("authorization_result_invalid", "policy assertion is missing");
          return { kind: "assertion", assertion: E };
        }
      );
      return;
    }
    let U = { kind: "confirmed" };
    const z = async () => {
      if (g.passkey === "none") return;
      if (g.passkey === "uv") {
        if (!g.prompt.challenge) throw new S("invalid_payload", "authorization challenge is missing");
        await hh({
          wrapMeta: g.wrapMeta,
          challenge: g.prompt.challenge,
          abortSignals: [v.signal]
        });
        return;
      }
      const E = await ga(ua(g.wrapMeta), Ft(), v.signal);
      U = {
        kind: "unlock",
        prfOutput: ce(E.prfOutput),
        prfSalt: ce(E.prfSalt)
      }, E.prfOutput.fill(0);
    };
    O(Qr(om(g.prompt), v.signal, void 0, z), () => U);
  }, m = (g) => {
    if (g.origin !== window.location.origin) return;
    const v = g.data;
    if (!v || v.id !== r || !Lr(v.build)) return;
    if (v.type === "authorization-dismiss") {
      d && !u && g.source === c && window.close();
      return;
    }
    if (v.type !== "authorization-request" || u) return;
    const L = d ? g.source === c ? c : void 0 : a().find((T) => T === g.source);
    if (L) {
      c = L;
      try {
        w(K5(v.request));
      } catch (T) {
        const C = it(T, "authorization_request_invalid");
        l({
          type: "authorization-error",
          build: he,
          id: r,
          code: C.code,
          message: C.message,
          retryable: C.retryable
        });
      }
    }
  };
  window.addEventListener("message", m), y(), h = setInterval(y, 500);
}
function g6() {
  if (window.self !== window.top || Qc().has("mode")) return;
  const e = document.querySelector("#page .body");
  e && J(e, "sandbox.intro");
}
Ke.register("handshake", (e) => {
  const t = e ?? {}, n = typeof t.expectedBuildHash == "string" ? t.expectedBuildHash : null;
  if (typeof t.parentOrigin != "string" || typeof t.nonce != "string" || t.nonce.length < 16)
    throw new S("invalid_handshake", "handshake must include parentOrigin and nonce");
  if (n !== null && n !== he.buildHash)
    throw new S("build_hash_mismatch", "sandbox build hash does not match parent expectation");
  const r = O5(t.appearance);
  r && Sa(r);
  const i = uh ? Le() : Zl(t.policy === void 0 ? void 0 : Gl(t.policy));
  return uh = !0, {
    accepted: !0,
    channel: Jn,
    version: 2,
    sandboxOrigin: window.location.origin,
    build: he,
    policy: i
  };
});
Ke.register("set-appearance", (e) => (Sa(xm(e)), { accepted: !0 }));
Ke.register("status", (e) => {
  const t = L5(e);
  try {
    t.wrapMeta && ft(t.wrapMeta);
    const n = ft(), r = Le();
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
Ke.register("setup", o6);
Ke.register("unlock", s6);
Ke.register("lock", () => ({ ...ht({ broadcast: !0, reason: "manual-lock" }), policy: Le() }));
Ke.register("seal", a6);
Ke.register("sealHdWallet", c6);
Ke.register("reveal", l6);
Ke.register("sign", d6);
Ke.register("approveRelease", u6);
Ke.register("deleteAuthzAssertion", p6);
Ke.register("authorizeSigningPolicyDowngrade", h6);
D2((e) => Ke.emit("vault.state", e));
Ak((e) => Ke.emit(e));
await zm();
Ke.start();
window.addEventListener("pagehide", G2);
w6();
y6();
g6();
window.self !== window.top && document.body.classList.add("embedded");
const $m = globalThis.crypto, m6 = { webcrypto: $m }, Om = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: m6,
  webcrypto: $m
}, Symbol.toStringTag, { value: "Module" }));
