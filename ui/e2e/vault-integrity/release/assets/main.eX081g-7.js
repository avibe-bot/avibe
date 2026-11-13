const Kt = "avibe.vault.crypto";
const ie = {
  sandboxVersion: "0.1.0",
  // Resource hashes are the authority; this source-input identity additionally
  // binds ready/handshake and same-release top-level ceremony pairing.
  buildHash: "2e88ccc707cd0eeeedacf77e87ae80b8e8a14dc769aab6eaf736d7860835e514"
};
class b extends Error {
  code;
  retryable;
  constructor(t, n, r = !1) {
    super(n ?? t), this.code = t, this.retryable = r;
  }
}
function ea(e) {
  let t;
  try {
    t = new URL(e);
  } catch {
    return !1;
  }
  const n = t.hostname.toLowerCase();
  return n === "localhost" || n === "127.0.0.1" || n === "::1" || n === "[::1]" ? t.protocol === "http:" || t.protocol === "https:" : !!(t.protocol === "https:" && (n === "avibe.bot" || n.endsWith(".avibe.bot")));
}
function kf(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return t.channel === Kt && t.version === 2 && typeof t.id == "string" && t.id.length > 0 && typeof t.op == "string";
}
const Ef = /^[A-Za-z0-9_-]{16,128}$/;
function Mc(e) {
  return typeof e == "object" && e !== null;
}
function Sf(e) {
  return Mc(e) ? e.channel === Kt && e.version === 2 && e.kind === "event" && e.event === "confirm.surface" : !1;
}
class Af {
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
        channel: Kt,
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
      channel: Kt,
      version: 2,
      build: ie,
      capabilities: {
        operations: Array.from(this.handlers.keys())
      }
    });
  }
  async onMessage(t) {
    if (!ea(t.origin)) return;
    if (Sf(t.data)) {
      if (t.origin === this.pinnedParentOrigin && t.source === this.pinnedSource) {
        const c = t.data.surface ?? t.data.payload;
        c !== void 0 && this.recordParentSurfaceEvent(t.data, c);
      }
      return;
    }
    if (!kf(t.data)) return;
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
    if (n.authorizationWindow !== void 0 && (typeof n.authorizationWindow != "string" || !Ef.test(n.authorizationWindow))) {
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
        channel: Kt,
        version: 2,
        id: n.id,
        ok: !0,
        result: c
      });
    } catch (c) {
      const u = c instanceof b ? c.code : "internal_error", l = c instanceof b ? c.retryable : !1, d = c instanceof b ? c.message : void 0;
      this.reply(t.source, this.fail(n.id, u, d, l)), r && this.clearPin();
    } finally {
      this.activeSurfaceSlots.delete(n.id);
    }
  }
  validateInitialHandshake(t, n) {
    if (t.op !== "handshake" || !Mc(t.payload)) return null;
    const r = t.payload.parentOrigin, i = t.payload.nonce;
    return typeof r != "string" || r !== n || !ea(r) || typeof i != "string" || i.length < 16 || i.length > 256 ? null : i;
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
    return { channel: Kt, version: 2, id: t, ok: !1, error: { code: n, message: r, retryable: i } };
  }
  reply(t, n) {
    !t || this.pinnedParentOrigin === null || t.postMessage(n, this.pinnedParentOrigin);
  }
  postToParent(t, n) {
    t.postMessage(n, "*");
  }
}
function Lo() {
  return new URLSearchParams((window.location.hash ?? "").slice(1));
}
function Hc(e, t, n, r = {}) {
  const i = new URL(window.location.href);
  return ie.buildHash !== "dev" && (i.pathname = `/v/${ie.buildHash}/index.html`), i.search = "", i.hash = new URLSearchParams({ mode: e, id: t, ...n, ...r }).toString(), i.toString();
}
function dn(e) {
  if (!e || typeof e != "object") return !1;
  const t = e;
  return t.buildHash === ie.buildHash && t.sandboxVersion === ie.sandboxVersion;
}
async function _f() {
  if (ie.buildHash === "dev") return;
  const e = new URL(window.location.href);
  if (e.pathname !== `/v/${ie.buildHash}/index.html` || e.search)
    throw new Error("Sandbox release document URL mismatch");
  const t = document.querySelectorAll('link[rel="stylesheet"]');
  if (t.length !== 1 || !t[0].integrity.startsWith("sha256-") || !t[0].href.startsWith(`${e.origin}/v/${ie.buildHash}/assets/`))
    throw new Error("Sandbox release stylesheet is not pinned");
  const n = t[0];
  n.sheet || await new Promise((r, i) => {
    const o = () => a(new Error("Sandbox release stylesheet failed integrity or loading")), s = () => n.sheet ? a() : o(), a = (u) => {
      clearTimeout(c), n.removeEventListener("load", s), n.removeEventListener("error", o), window.removeEventListener("load", s), window.removeEventListener("pagehide", o), u ? i(u) : r();
    }, c = setTimeout(o, 1e4);
    n.addEventListener("load", s, { once: !0 }), n.addEventListener("error", o, { once: !0 }), window.addEventListener("load", s, { once: !0 }), window.addEventListener("pagehide", o, { once: !0 }), (n.sheet || document.readyState === "complete") && s();
  });
}
class qe extends Error {
  constructor(t) {
    let n;
    t instanceof Error ? n = t.message : typeof t == "string" ? n = t : n = "", super(n), this.name = this.constructor.name;
  }
}
class Xe extends qe {
}
class Cr extends qe {
}
class hr extends qe {
}
class If extends qe {
}
class Pf extends qe {
}
class Tf extends qe {
}
class Bf extends qe {
}
class $f extends qe {
}
class Of extends qe {
}
class Rf extends qe {
}
class Co extends qe {
}
const Lf = {}, Fc = Cf(globalThis, Lf);
function Cf(e, t) {
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
async function zf() {
  if (Fc !== void 0 && globalThis.crypto !== void 0)
    return globalThis.crypto.subtle;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => xf);
    return e.subtle;
  } catch (e) {
    throw new Co(e);
  }
}
class zo {
  constructor() {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    });
  }
  async _setup() {
    this._api === void 0 && (this._api = await zf());
  }
}
const vt = {
  Base: 0,
  Psk: 1,
  Auth: 2,
  AuthPsk: 3
}, ta = {
  DhkemX25519HkdfSha256: 32
}, Kc = {
  HkdfSha256: 1
}, Uo = {
  Aes128Gcm: 1,
  Aes256Gcm: 2,
  ExportOnly: 65535
}, Dr = 8192, Uf = 268435456, na = 32, ve = /* @__PURE__ */ new Uint8Array(0), Je = 0n, nn = 1n, yr = 2n, Nf = /* @__PURE__ */ new Uint8Array([
  75,
  69,
  77,
  0,
  0
]), ra = /* @__PURE__ */ new Uint8Array([
  72,
  80,
  75,
  69,
  45,
  118,
  49
]);
function zr(e) {
  return new Uint8Array(Y(e));
}
function Y(e) {
  return e instanceof ArrayBuffer ? e : ArrayBuffer.isView(e) ? new Uint8Array(e.buffer, e.byteOffset, e.byteLength).slice().buffer : new Uint8Array(e).slice().buffer;
}
class Mf extends zo {
  constructor() {
    super(), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Kc.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: ve
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
    return r.set(ra, 0), r.set(this._suiteId, 7), r.set(t, 7 + this._suiteId.byteLength), r.set(n, 7 + this._suiteId.byteLength + t.byteLength), r;
  }
  buildLabeledInfo(t, n, r) {
    this._checkInit();
    const i = new Uint8Array(9 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return i.set(new Uint8Array([0, r]), 0), i.set(ra, 2), i.set(this._suiteId, 9), i.set(t, 9 + this._suiteId.byteLength), i.set(n, 9 + this._suiteId.byteLength + t.byteLength), i;
  }
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : Y(t);
    if (r.byteLength !== this.hashSize)
      throw new Xe("The salt length must be the same as the hashSize");
    const i = Y(n), o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
  async expand(t, n, r) {
    await this._setup();
    const i = Y(t), o = await this._api.importKey("raw", i, this.algHash, !1, [
      "sign"
    ]), s = new ArrayBuffer(r), a = new Uint8Array(s);
    let c = ve;
    const u = zr(n), l = new Uint8Array(1);
    if (r > 255 * this.hashSize)
      throw new Error("Entropy limit reached");
    const d = new Uint8Array(this.hashSize + u.length + 1);
    for (let h = 1, f = 0; f < a.length; h++)
      l[0] = h, d.set(c, 0), d.set(u, c.length), d.set(l, c.length + u.length), c = new Uint8Array(await this._api.sign("HMAC", o, d.slice(0, c.length + u.length + 1))), a.length - f >= c.length ? (a.set(c, f), f += c.length) : (a.set(c.slice(0, a.length - f), f), f += a.length - f);
    return s;
  }
  async extractAndExpand(t, n, r, i) {
    await this._setup();
    const o = Y(n), s = await this._api.importKey("raw", o, "HKDF", !1, ["deriveBits"]);
    return await this._api.deriveBits({
      name: "HKDF",
      hash: this.algHash.hash,
      salt: Y(t),
      info: Y(r)
    }, s, i * 8);
  }
  async labeledExtract(t, n, r) {
    return await this.extract(t, this.buildLabeledIkm(n, r));
  }
  async labeledExpand(t, n, r, i) {
    return await this.expand(t, this.buildLabeledInfo(n, r, i), i);
  }
  _checkInit() {
    if (this._suiteId === ve)
      throw new Error("Not initialized. Call init()");
  }
}
class Dc extends Mf {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Kc.HkdfSha256
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
const $n = (e) => typeof e == "object" && e !== null && typeof e.privateKey == "object" && typeof e.publicKey == "object";
function qn(e, t) {
  if (t <= 0)
    throw new Error("i2Osp: too small size");
  if (e >= 256 ** t)
    throw new Error("i2Osp: too large integer");
  const n = new Uint8Array(t);
  for (let r = 0; r < t && e; r++)
    n[t - (r + 1)] = e % 256, e = Math.floor(e / 256);
  return n;
}
function wr(e, t) {
  const n = new Uint8Array(e.length + t.length);
  return n.set(e, 0), n.set(t, e.length), n;
}
function ia(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = atob(t), r = new Uint8Array(n.length);
  for (let i = 0; i < n.length; i++)
    r[i] = n.charCodeAt(i);
  return r;
}
async function Hf() {
  if (typeof Fc < "u" && globalThis.crypto !== void 0)
    return globalThis.crypto;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => xf);
    return e;
  } catch {
    throw new Error("failed to load Crypto");
  }
}
function Ff(e, t) {
  if (e.byteLength !== t.byteLength)
    throw new Error("xor: different length inputs");
  const n = new Uint8Array(e.byteLength);
  for (let r = 0; r < e.byteLength; r++)
    n[r] = e[r] ^ t[r];
  return n;
}
const Kf = /* @__PURE__ */ new Uint8Array([
  101,
  97,
  101,
  95,
  112,
  114,
  107
]), Df = /* @__PURE__ */ new Uint8Array([
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
function jf(e, t, n) {
  const r = new Uint8Array(e.length + t.length + n.length);
  return r.set(e, 0), r.set(t, e.length), r.set(n, e.length + t.length), r;
}
class Vf {
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
    const i = new Uint8Array(Nf);
    i.set(qn(this.id, 2), 3), this._kdf.init(i);
  }
  async serializePublicKey(t) {
    return await this._prim.serializePublicKey(t);
  }
  async deserializePublicKey(t) {
    return await this._prim.deserializePublicKey(Y(t));
  }
  async serializePrivateKey(t) {
    return await this._prim.serializePrivateKey(t);
  }
  async deserializePrivateKey(t) {
    return await this._prim.deserializePrivateKey(Y(t));
  }
  async importKey(t, n, r = !0) {
    return await this._prim.importKey(t, n, r);
  }
  async generateKeyPair() {
    return await this._prim.generateKeyPair();
  }
  async deriveKeyPair(t) {
    const n = Y(t);
    if (n.byteLength > Dr)
      throw new Xe("Too long ikm");
    return await this._prim.deriveKeyPair(n);
  }
  async encap(t) {
    let n;
    t.ekm === void 0 ? n = await this.generateKeyPair() : $n(t.ekm) ? n = t.ekm : n = await this.deriveKeyPair(t.ekm);
    const r = await this._prim.serializePublicKey(n.publicKey), i = await this._prim.serializePublicKey(t.recipientPublicKey);
    try {
      let o;
      if (t.senderKey === void 0)
        o = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey));
      else {
        const c = $n(t.senderKey) ? t.senderKey.privateKey : t.senderKey, u = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey)), l = new Uint8Array(await this._prim.dh(c, t.recipientPublicKey));
        o = wr(u, l);
      }
      let s;
      if (t.senderKey === void 0)
        s = wr(new Uint8Array(r), new Uint8Array(i));
      else {
        const c = $n(t.senderKey) ? t.senderKey.publicKey : await this._prim.derivePublicKey(t.senderKey), u = await this._prim.serializePublicKey(c);
        s = jf(new Uint8Array(r), new Uint8Array(i), new Uint8Array(u));
      }
      const a = await this._generateSharedSecret(o, s);
      return {
        enc: r,
        sharedSecret: a
      };
    } catch (o) {
      throw new If(o);
    }
  }
  async decap(t) {
    const n = Y(t.enc), r = await this._prim.deserializePublicKey(n), i = $n(t.recipientKey) ? t.recipientKey.privateKey : t.recipientKey, o = $n(t.recipientKey) ? t.recipientKey.publicKey : await this._prim.derivePublicKey(t.recipientKey), s = await this._prim.serializePublicKey(o);
    try {
      let a;
      if (t.senderPublicKey === void 0)
        a = new Uint8Array(await this._prim.dh(i, r));
      else {
        const u = new Uint8Array(await this._prim.dh(i, r)), l = new Uint8Array(await this._prim.dh(i, t.senderPublicKey));
        a = wr(u, l);
      }
      let c;
      if (t.senderPublicKey === void 0)
        c = wr(new Uint8Array(n), new Uint8Array(s));
      else {
        const u = await this._prim.serializePublicKey(t.senderPublicKey);
        c = new Uint8Array(n.byteLength + s.byteLength + u.byteLength), c.set(new Uint8Array(n), 0), c.set(new Uint8Array(s), n.byteLength), c.set(new Uint8Array(u), n.byteLength + s.byteLength);
      }
      return await this._generateSharedSecret(a, c);
    } catch (a) {
      throw new Pf(a);
    }
  }
  async _generateSharedSecret(t, n) {
    const r = this._kdf.buildLabeledIkm(Kf, t), i = this._kdf.buildLabeledInfo(Df, n, this.secretSize);
    return await this._kdf.extractAndExpand(ve, r, i, this.secretSize);
  }
}
const gr = ["deriveBits"], qf = /* @__PURE__ */ new Uint8Array([
  100,
  107,
  112,
  95,
  112,
  114,
  107
]), Gf = /* @__PURE__ */ new Uint8Array([115, 107]);
class rn {
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
class Wf {
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
      return Promise.reject(new Cr(n));
    }
  }
  async deserializePublicKey(t) {
    try {
      return await this._importRawKey(Y(t), !0);
    } catch (n) {
      throw new hr(n);
    }
  }
  serializePrivateKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new Cr(n));
    }
  }
  async deserializePrivateKey(t) {
    try {
      return await this._importRawKey(Y(t), !1);
    } catch (n) {
      throw new hr(n);
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
      throw new hr(i);
    }
  }
  async generateKeyPair() {
    try {
      const t = await this._curve.utils.randomSecretKey(), n = new rn(this._algName, t, "private", gr);
      return { publicKey: await this.derivePublicKey(n), privateKey: n };
    } catch (t) {
      throw new Co(t);
    }
  }
  async deriveKeyPair(t) {
    try {
      const n = Y(t), r = await this._hkdf.labeledExtract(ve.buffer, qf, new Uint8Array(n)), i = await this._hkdf.labeledExpand(r, Gf, ve, this._nSk), o = new rn(this._algName, new Uint8Array(i), "private", gr);
      return {
        privateKey: o,
        publicKey: await this.derivePublicKey(o)
      };
    } catch (n) {
      throw new Rf(n);
    }
  }
  derivePublicKey(t) {
    try {
      const n = this._curve.getPublicKey(t.key);
      return Promise.resolve(new rn(this._algName, n, "public"));
    } catch (n) {
      return Promise.reject(new hr(n));
    }
  }
  dh(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t.key, n.key).buffer);
    } catch (r) {
      return Promise.reject(new Cr(r));
    }
  }
  _importRawKey(t, n) {
    return new Promise((r, i) => {
      n && t.byteLength !== this._nPk && i(new Error("Invalid length of the key")), !n && t.byteLength !== this._nSk && i(new Error("Invalid length of the key")), r(new rn(this._algName, new Uint8Array(t), n ? "public" : "private", n ? [] : gr));
    });
  }
  _importJWK(t, n) {
    return new Promise((r, i) => {
      t.kty !== "OKP" && i(new Error(`Invalid kty: ${t.kty}`)), t.crv !== this._algName && i(new Error(`Invalid crv: ${t.crv}`)), n ? (typeof t.d < "u" && i(new Error("Invalid key: `d` should not be set")), typeof t.x != "string" && i(new Error("Invalid key: `x` not found")), r(new rn(this._algName, ia(t.x), "public"))) : (typeof t.d != "string" && i(new Error("Invalid key: `d` not found")), r(new rn(this._algName, ia(t.d), "private", gr)));
    });
  }
}
const Zf = ["encrypt", "decrypt"];
function Xf(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Yn(e, t = "") {
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new Error(`${n}expected integer >0, got ${e}`);
  }
}
function pt(e, t, n = "") {
  const r = Xf(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function jr(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Yf(e, t) {
  pt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new Error('"digestInto() output" expected to be of length >=' + n);
}
function Jf(e) {
  if (typeof e == "bigint") {
    if (!Ur(e))
      throw new Error("positive bigint expected, got " + e);
  } else
    Yn(e);
  return e;
}
function Vr(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
const Qf = /* @__PURE__ */ new Uint32Array([287454020]), ep = /* @__PURE__ */ new Uint8Array(Qf.buffer);
ep[0];
function Hi(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Ge(e, t) {
  return e << 32 - t | e >>> t;
}
const jc = /* @ts-ignore: to use toHex */ typeof Uint8Array.from([]).toHex == "function" && // @ts-ignore: to use fromHex
typeof Uint8Array.fromHex == "function", tp = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0")), np = [
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
function rp(e) {
  if (pt(e), jc)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += tp[e[n]];
  return t;
}
const ct = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function fo(e) {
  if (e >= ct._0 && e <= ct._9)
    return e - ct._0;
  if (e >= ct.A && e <= ct.F)
    return e - (ct.A - 10);
  if (e >= ct.a && e <= ct.f)
    return e - (ct.a - 10);
}
function ip(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (jc)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = fo(e.charCodeAt(o)), a = fo(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function op(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  let t = Je;
  for (let n = 0; n < e.length; n++) {
    const r = fo(e.charCodeAt(n));
    if (r === void 0)
      throw new Error('hex string expected, got non-hex character "' + e[n] + '" at index ' + n);
    t = t << 4n | np[r];
  }
  return t;
}
function sp(e) {
  Yn(e, "numberToBigint");
  let t = e, n = Je, r = 1n;
  for (; t > 0; )
    t % 2 === 1 && (n += r), t = Math.floor(t / 2), r <<= 1n;
  return n;
}
function oa(e) {
  return op(rp(po(pt(e)).reverse()));
}
function ap(e, t) {
  Yn(t), e = Jf(e);
  const n = ip(e.toString(16).padStart(t * 2, "0"));
  if (n.length !== t)
    throw new Error("number too large");
  return n;
}
function cp(e, t) {
  return ap(e, t).reverse();
}
function po(e) {
  return Uint8Array.from(e);
}
function Ur(e) {
  return typeof e == "bigint" && Je <= e;
}
function up(e, t, n) {
  return Ur(e) && Ur(t) && Ur(n) && t <= e && e < n;
}
function sa(e, t, n, r) {
  if (!up(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function lp(e, t = {}, n = {}) {
  if (!e || typeof e != "object")
    throw new Error("expected valid options object");
  function r(o, s, a) {
    const c = e[o];
    if (a && c === void 0)
      return;
    const u = typeof c;
    if (u !== s || c === null)
      throw new Error(`param "${o}" is invalid: expected ${s}, got ${u}`);
  }
  const i = (o, s) => Object.entries(o).forEach(([a, c]) => r(a, c, s));
  i(t, !1), i(n, !0);
}
async function dp(e = 32) {
  const t = await Hf(), n = new Uint8Array(e);
  return t.getRandomValues(n), n;
}
function fp(e) {
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
function pp(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash must wrapped by utils.createHasher");
  Yn(e.outputLen), Yn(e.blockLen);
}
function hp(e, t = {}) {
  const n = (o, s) => e(s).update(o).digest(), r = e(void 0), i = Object.assign(n, {
    outputLen: r.outputLen,
    blockLen: r.blockLen,
    create: (o) => e(o),
    ...t
  });
  return Object.freeze(i);
}
let Vc = class {
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
    }), pp(t), pt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), Vr(i);
  }
  update(t) {
    return jr(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    jr(this), pt(t, this.outputLen, "output"), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const qc = (e, t, n) => new Vc(e, t).update(n).digest();
qc.create = (e, t) => new Vc(e, t);
function yp(e, t, n) {
  return e & t ^ ~e & n;
}
function wp(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let gp = class {
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
    }), this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Hi(this.buffer);
  }
  update(t) {
    jr(this), pt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Hi(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    jr(this), Yf(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Vr(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, sp(this.length * 8), o), this.process(r, 0);
    const a = Hi(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen must be aligned to 32bit");
    const u = c / 4, l = this.get();
    if (u > l.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < u; d++)
      a.setUint32(4 * d, l[d], o);
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
const xt = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), mp = /* @__PURE__ */ Uint32Array.from([
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
]), kt = /* @__PURE__ */ new Uint32Array(64);
let bp = class extends gp {
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
      kt[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = kt[d - 15], f = kt[d - 2], g = Ge(h, 7) ^ Ge(h, 18) ^ h >>> 3, m = Ge(f, 17) ^ Ge(f, 19) ^ f >>> 10;
      kt[d] = m + kt[d - 7] + g + kt[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: u, H: l } = this;
    for (let d = 0; d < 64; d++) {
      const h = Ge(a, 6) ^ Ge(a, 11) ^ Ge(a, 25), f = l + h + yp(a, c, u) + mp[d] + kt[d] | 0, m = (Ge(r, 2) ^ Ge(r, 13) ^ Ge(r, 22)) + wp(r, i, o) | 0;
      l = u, u = c, c = a, a = s + f | 0, s = o, o = i, i = r, r = f + m | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, u = u + this.G | 0, l = l + this.H | 0, this.set(r, i, o, s, a, c, u, l);
  }
  roundClean() {
    Vr(kt);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Vr(this.buffer);
  }
}, vp = class extends bp {
  constructor() {
    super(32), Object.defineProperty(this, "A", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[0] | 0
    }), Object.defineProperty(this, "B", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[1] | 0
    }), Object.defineProperty(this, "C", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[2] | 0
    }), Object.defineProperty(this, "D", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[3] | 0
    }), Object.defineProperty(this, "E", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[4] | 0
    }), Object.defineProperty(this, "F", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[5] | 0
    }), Object.defineProperty(this, "G", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[6] | 0
    }), Object.defineProperty(this, "H", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: xt[7] | 0
    });
  }
};
const xp = /* @__PURE__ */ hp(
  () => new vp(),
  /* @__PURE__ */ fp(1)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Gc(e, t) {
  const n = e % t;
  return n >= Je ? n : t + n;
}
function Ce(e, t, n) {
  let r = e;
  for (; t-- > Je; )
    r *= r, r %= n;
  return r;
}
function kp(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Ep(e) {
  return lp(e, {
    adjustScalarBytes: "function",
    powPminus2: "function"
  }), Object.freeze({ ...e });
}
function Sp(e) {
  const t = Ep(e), { P: n, type: r, adjustScalarBytes: i, powPminus2: o, randomBytes: s } = t, a = r === "x25519";
  if (!a && r !== "x448")
    throw new Error("invalid type");
  const c = s || dp, u = a ? 255n : 448n, l = a ? 32 : 56, d = a ? 9n : 5n, h = a ? 121665n : 39081n, f = a ? yr ** 254n : yr ** 447n, g = a ? 8n * yr ** 251n - nn : 4n * yr ** 445n - nn, m = f + g + nn, p = (A) => Gc(A, n), y = v(d);
  function v(A) {
    return cp(p(A), l);
  }
  function x(A) {
    const k = po(pt(A, l, "uCoordinate"));
    return a && (k[31] &= 127), p(oa(k));
  }
  function I(A) {
    return oa(i(po(pt(A, l, "scalar"))));
  }
  function E(A, k) {
    const _ = z(x(k), I(A));
    if (_ === Je)
      throw new Error("invalid private or public key received");
    return v(_);
  }
  function $(A) {
    return E(A, y);
  }
  const T = $, L = E;
  function P(A, k, _) {
    const B = p(A * (k - _));
    return k = p(k - B), _ = p(_ + B), { x_2: k, x_3: _ };
  }
  function z(A, k) {
    sa("u", A, Je, n), sa("scalar", k, f, m);
    const _ = k, B = A;
    let R = nn, O = Je, C = A, N = nn, M = Je;
    for (let H = u - 1n; H >= Je; H--) {
      const ee = _ >> H & nn;
      M ^= ee, { x_2: R, x_3: C } = P(M, R, C), { x_2: O, x_3: N } = P(M, O, N), M = ee;
      const te = R + O, ne = p(te * te), Q = R - O, ye = p(Q * Q), at = ne - ye, Ut = C + N, tn = C - N, Tn = p(tn * te), Bn = p(Ut * Q), Js = Tn + Bn, Qs = Tn - Bn;
      C = p(Js * Js), N = p(B * p(Qs * Qs)), R = p(ne * ye), O = p(at * (ne + p(h * at)));
    }
    ({ x_2: R, x_3: C } = P(M, R, C)), { x_2: O, x_3: N } = P(M, O, N);
    const G = o(O);
    return p(R * G);
  }
  const U = {
    secretKey: l,
    publicKey: l,
    seed: l
  }, w = async (A) => (A === void 0 && (A = await c(l)), pt(A, U.seed, "seed"), A), S = { randomSecretKey: w };
  return Object.freeze({
    keygen: kp(w, T),
    getSharedSecret: L,
    getPublicKey: T,
    scalarMult: E,
    scalarMultBase: $,
    utils: S,
    GuBytes: y.slice(),
    lengths: U
  });
}
class Ap extends zo {
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
    }), this._rawKey = Y(t);
  }
  async seal(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: Y(t),
      additionalData: Y(r)
    };
    return await this._api.encrypt(i, this._key, Y(n));
  }
  async open(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: Y(t),
      additionalData: Y(r)
    };
    return await this._api.decrypt(i, this._key, Y(n));
  }
  async _setupKey() {
    if (this._key !== void 0)
      return;
    await this._setup();
    const t = await this._importKey(this._rawKey);
    new Uint8Array(this._rawKey).fill(0), this._key = t;
  }
  async _importKey(t) {
    return await this._api.importKey("raw", t, { name: "AES-GCM" }, !0, Zf);
  }
}
class _p {
  constructor() {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Uo.Aes128Gcm
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
    return new Ap(t);
  }
}
class Ip extends _p {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Uo.Aes256Gcm
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
function aa() {
  return new Promise((e, t) => {
    t(new Co("Not supported"));
  });
}
const Pp = new Uint8Array([115, 101, 99]);
class No {
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
    return await aa();
  }
  async open(t, n) {
    return await aa();
  }
  async export(t, n) {
    const r = Y(t);
    if (r.byteLength > Dr)
      throw new Xe("Too long exporter context");
    try {
      return await this._kdf.labeledExpand(this.exporterSecret, Pp, new Uint8Array(r), n);
    } catch (i) {
      throw new Tf(i);
    }
  }
}
class Tp extends No {
}
class Bp extends No {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.enc = i;
  }
}
class Wc extends No {
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
    const n = qn(t.seq, t.baseNonce.byteLength);
    return Ff(t.baseNonce, n).buffer;
  }
  incrementSeq(t) {
    if (t.seq > Number.MAX_SAFE_INTEGER)
      throw new Of("Message limit reached");
    t.seq += 1;
  }
}
var $p = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Op = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Nr;
class Zc {
  constructor() {
    Nr.set(this, Promise.resolve());
  }
  async lock() {
    let t;
    const n = new Promise((i) => {
      t = i;
    }), r = $p(this, Nr, "f");
    return Op(this, Nr, n, "f"), await r, t;
  }
}
Nr = /* @__PURE__ */ new WeakMap();
var ca = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Rp = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Hn;
class Lp extends Wc {
  constructor() {
    super(...arguments), Hn.set(this, void 0);
  }
  async open(t, n = ve.buffer) {
    Rp(this, Hn, ca(this, Hn, "f") ?? new Zc(), "f");
    const r = await ca(this, Hn, "f").lock();
    let i;
    try {
      i = await this._ctx.key.open(this.computeNonce(this._ctx), Y(t), Y(n));
    } catch (o) {
      throw new $f(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Hn = /* @__PURE__ */ new WeakMap();
var ua = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, Cp = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Fn;
class zp extends Wc {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Fn.set(this, void 0), this.enc = i;
  }
  async seal(t, n = ve.buffer) {
    Cp(this, Fn, ua(this, Fn, "f") ?? new Zc(), "f");
    const r = await ua(this, Fn, "f").lock();
    let i;
    try {
      i = await this._ctx.key.seal(this.computeNonce(this._ctx), Y(t), Y(n));
    } catch (o) {
      throw new Bf(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Fn = /* @__PURE__ */ new WeakMap();
const Up = new Uint8Array([
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
]), Np = new Uint8Array([101, 120, 112]), Mp = new Uint8Array([
  105,
  110,
  102,
  111,
  95,
  104,
  97,
  115,
  104
]), Hp = new Uint8Array([107, 101, 121]), Fp = new Uint8Array([
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
]), Kp = new Uint8Array([115, 101, 99, 114, 101, 116]), Dp = new Uint8Array([
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
class jp extends zo {
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
      throw new Xe("KemId cannot be used");
    if (this._kem = t.kem, typeof t.kdf == "number")
      throw new Xe("KdfId cannot be used");
    if (this._kdf = t.kdf, typeof t.aead == "number")
      throw new Xe("AeadId cannot be used");
    this._aead = t.aead, this._suiteId = new Uint8Array(Dp), this._suiteId.set(qn(this._kem.id, 2), 4), this._suiteId.set(qn(this._kdf.id, 2), 6), this._suiteId.set(qn(this._aead.id, 2), 8), this._kdf.init(this._suiteId);
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
    return t.psk !== void 0 ? r = t.senderKey !== void 0 ? vt.AuthPsk : vt.Psk : r = t.senderKey !== void 0 ? vt.Auth : vt.Base, await this._keyScheduleS(r, n.sharedSecret, n.enc, t);
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
    return t.psk !== void 0 ? r = t.senderPublicKey !== void 0 ? vt.AuthPsk : vt.Psk : r = t.senderPublicKey !== void 0 ? vt.Auth : vt.Base, await this._keyScheduleR(r, n, t);
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
  async seal(t, n, r = ve.buffer) {
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
  async open(t, n, r = ve.buffer) {
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
    const i = r.psk === void 0 ? ve : zr(r.psk.id), o = await this._kdf.labeledExtract(ve, Fp, i), s = r.info === void 0 ? ve : zr(r.info), a = await this._kdf.labeledExtract(ve, Mp, s), c = new Uint8Array(1 + o.byteLength + a.byteLength);
    c.set(new Uint8Array([t]), 0), c.set(new Uint8Array(o), 1), c.set(new Uint8Array(a), 1 + o.byteLength);
    const u = r.psk === void 0 ? ve : zr(r.psk.key), l = this._kdf.buildLabeledIkm(Kp, u), d = this._kdf.buildLabeledInfo(Np, c, this._kdf.hashSize), h = await this._kdf.extractAndExpand(n, l, d, this._kdf.hashSize);
    if (this._aead.id === Uo.ExportOnly)
      return { aead: this._aead, exporterSecret: h };
    const f = this._kdf.buildLabeledInfo(Hp, c, this._aead.keySize), g = await this._kdf.extractAndExpand(n, l, f, this._aead.keySize), m = this._kdf.buildLabeledInfo(Up, c, this._aead.nonceSize), p = await this._kdf.extractAndExpand(n, l, m, this._aead.nonceSize);
    return {
      aead: this._aead,
      exporterSecret: h,
      key: g,
      baseNonce: new Uint8Array(p),
      seq: 0
    };
  }
  async _keyScheduleS(t, n, r, i) {
    const o = await this._keySchedule(t, n, i);
    return o.key === void 0 ? new Bp(this._api, this._kdf, o.exporterSecret, r) : new zp(this._api, this._kdf, o, r);
  }
  async _keyScheduleR(t, n, r) {
    const i = await this._keySchedule(t, n, r);
    return i.key === void 0 ? new Tp(this._api, this._kdf, i.exporterSecret) : new Lp(this._api, this._kdf, i);
  }
  _validateInputLength(t) {
    if (t.info !== void 0 && t.info.byteLength > Uf)
      throw new Xe("Too long info");
    if (t.psk !== void 0) {
      if (t.psk.key.byteLength < na)
        throw new Xe(`PSK must have at least ${na} bytes`);
      if (t.psk.key.byteLength > Dr)
        throw new Xe("Too long psk.key");
      if (t.psk.id.byteLength > Dr)
        throw new Xe("Too long psk.id");
    }
  }
}
class Vp extends jp {
}
let qp = class extends Dc {
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Gp = 1n, la = 2n, Wp = 3n, Zp = 5n, Xc = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn;
function Xp(e) {
  const t = 10n, n = 20n, r = 40n, i = 80n, o = Xc, a = e * e % o * e % o, c = Ce(a, la, o) * a % o, u = Ce(c, Gp, o) * e % o, l = Ce(u, Zp, o) * u % o, d = Ce(l, t, o) * l % o, h = Ce(d, n, o) * d % o, f = Ce(h, r, o) * h % o, g = Ce(f, i, o) * f % o, m = Ce(g, i, o) * f % o, p = Ce(m, t, o) * l % o;
  return { pow_p_5_8: Ce(p, la, o) * e % o, b2: a };
}
function Yp(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const Jp = /* @__PURE__ */ (() => {
  const e = Xc;
  return Sp({
    P: e,
    type: "x25519",
    powPminus2: (t) => {
      const { pow_p_5_8: n, b2: r } = Xp(t);
      return Gc(Ce(n, Wp, e) * r, e);
    },
    adjustScalarBytes: Yp
  });
})();
class Qp extends Dc {
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : Y(t), i = Y(n);
    if (r.byteLength !== this.hashSize)
      return qc(xp, new Uint8Array(r), new Uint8Array(i)).buffer;
    const o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
}
class eh extends Wf {
  constructor(t) {
    super("X25519", 32, Jp, t);
  }
  derive(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t, n));
    } catch (r) {
      return Promise.reject(new Cr(r));
    }
  }
}
class th extends Vf {
  constructor() {
    const t = new Qp();
    super(ta.DhkemX25519HkdfSha256, new eh(t), t), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: ta.DhkemX25519HkdfSha256
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
function Yc(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array" && "BYTES_PER_ELEMENT" in e && e.BYTES_PER_ELEMENT === 1;
}
function tt(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(`${n}expected number, got ${typeof e}`);
  }
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new RangeError(`${n}expected integer >= 0, got ${e}`);
  }
}
function nt(e, t, n = "") {
  const r = Yc(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`, u = s + "expected Uint8Array" + a + ", got " + c;
    throw r ? new RangeError(u) : new TypeError(u);
  }
  return e;
}
function Jc(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new TypeError("Hash must wrapped by utils.createHasher");
  if (tt(e.outputLen), tt(e.blockLen), e.outputLen < 1)
    throw new Error('"outputLen" must be >= 1');
  if (e.blockLen < 1)
    throw new Error('"blockLen" must be >= 1');
}
function wn(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Mo(e, t) {
  nt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new RangeError('"digestInto() output" expected to be of length >=' + n);
}
function nh(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function rt(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Fi(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function We(e, t) {
  return e << 32 - t | e >>> t;
}
function mr(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const rh = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function ih(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function oh(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = ih(e[t]);
  return e;
}
const da = rh ? (e) => e : oh, Qc = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", sh = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Ho(e) {
  if (nt(e), Qc)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += sh[e[n]];
  return t;
}
const ut = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function fa(e) {
  if (e >= ut._0 && e <= ut._9)
    return e - ut._0;
  if (e >= ut.A && e <= ut.F)
    return e - (ut.A - 10);
  if (e >= ut.a && e <= ut.f)
    return e - (ut.a - 10);
}
function Fo(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  if (Qc)
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
    const s = fa(e.charCodeAt(o)), a = fa(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new RangeError('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function ah(...e) {
  let t = 0;
  for (let r = 0; r < e.length; r++) {
    const i = e[r];
    nt(i), t += i.length;
  }
  const n = new Uint8Array(t);
  for (let r = 0, i = 0; r < e.length; r++) {
    const o = e[r];
    n.set(o, i), i += o.length;
  }
  return n;
}
function bi(e, t = {}) {
  const n = (i, o) => e(o).update(i).digest(), r = e(void 0);
  return n.outputLen = r.outputLen, n.blockLen = r.blockLen, n.canXOF = r.canXOF, n.create = (i) => e(i), Object.assign(n, t), Object.freeze(n);
}
function Ko(e = 32) {
  tt(e, "bytesLength");
  const t = typeof globalThis == "object" ? globalThis.crypto : null;
  if (typeof t?.getRandomValues != "function")
    throw new Error("crypto.getRandomValues must be defined");
  if (e > 65536)
    throw new RangeError(`"bytesLength" expected <= 65536, got ${e}`);
  return t.getRandomValues(new Uint8Array(e));
}
const eu = (e) => ({
  // Current NIST hashAlgs suffixes used here fit in one DER subidentifier octet.
  // Larger suffix values would need base-128 OID encoding and a different length byte.
  oid: Uint8Array.from([6, 9, 96, 134, 72, 1, 101, 3, 4, 2, e])
});
function ch(e, t, n) {
  return e & t ^ ~e & n;
}
function uh(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Do = class {
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
    this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Fi(this.buffer);
  }
  update(t) {
    wn(this), nt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Fi(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    wn(this), Mo(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, rt(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Fi(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen must be aligned to 32bit");
    const u = c / 4, l = this.get();
    if (u > l.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < u; d++)
      a.setUint32(4 * d, l[d], o);
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
const Et = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), me = /* @__PURE__ */ Uint32Array.from([
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
]), br = /* @__PURE__ */ BigInt(2 ** 32 - 1), pa = /* @__PURE__ */ BigInt(32);
function lh(e, t = !1) {
  return t ? { h: Number(e & br), l: Number(e >> pa & br) } : { h: Number(e >> pa & br) | 0, l: Number(e & br) | 0 };
}
function tu(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = lh(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const ha = (e, t, n) => e >>> n, ya = (e, t, n) => e << 32 - n | t >>> n, on = (e, t, n) => e >>> n | t << 32 - n, sn = (e, t, n) => e << 32 - n | t >>> n, vr = (e, t, n) => e << 64 - n | t >>> n - 32, xr = (e, t, n) => e >>> n - 32 | t << 64 - n, dh = (e, t, n) => e << n | t >>> 32 - n, fh = (e, t, n) => t << n | e >>> 32 - n, ph = (e, t, n) => t << n - 32 | e >>> 64 - n, hh = (e, t, n) => e << n - 32 | t >>> 64 - n;
function lt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const yh = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), wh = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, gh = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), mh = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, bh = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), vh = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, xh = /* @__PURE__ */ Uint32Array.from([
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
]), St = /* @__PURE__ */ new Uint32Array(64);
class kh extends Do {
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
      St[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = St[d - 15], f = St[d - 2], g = We(h, 7) ^ We(h, 18) ^ h >>> 3, m = We(f, 17) ^ We(f, 19) ^ f >>> 10;
      St[d] = m + St[d - 7] + g + St[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: u, H: l } = this;
    for (let d = 0; d < 64; d++) {
      const h = We(a, 6) ^ We(a, 11) ^ We(a, 25), f = l + h + ch(a, c, u) + xh[d] + St[d] | 0, m = (We(r, 2) ^ We(r, 13) ^ We(r, 22)) + uh(r, i, o) | 0;
      l = u, u = c, c = a, a = s + f | 0, s = o, o = i, i = r, r = f + m | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, u = u + this.G | 0, l = l + this.H | 0, this.set(r, i, o, s, a, c, u, l);
  }
  roundClean() {
    rt(St);
  }
  destroy() {
    this.destroyed = !0, this.set(0, 0, 0, 0, 0, 0, 0, 0), rt(this.buffer);
  }
}
class Eh extends kh {
  // We cannot use array here since array allows indexing by variable
  // which means optimizer/compiler cannot use registers.
  A = Et[0] | 0;
  B = Et[1] | 0;
  C = Et[2] | 0;
  D = Et[3] | 0;
  E = Et[4] | 0;
  F = Et[5] | 0;
  G = Et[6] | 0;
  H = Et[7] | 0;
  constructor() {
    super(32);
  }
}
const nu = tu([
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
].map((e) => BigInt(e))), Sh = nu[0], Ah = nu[1], At = /* @__PURE__ */ new Uint32Array(80), _t = /* @__PURE__ */ new Uint32Array(80);
class _h extends Do {
  constructor(t) {
    super(128, t, 16, !1);
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: u, El: l, Fh: d, Fl: h, Gh: f, Gl: g, Hh: m, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, u, l, d, h, f, g, m, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, u, l, d, h, f, g, m, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = u | 0, this.El = l | 0, this.Fh = d | 0, this.Fl = h | 0, this.Gh = f | 0, this.Gl = g | 0, this.Hh = m | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let x = 0; x < 16; x++, n += 4)
      At[x] = t.getUint32(n), _t[x] = t.getUint32(n += 4);
    for (let x = 16; x < 80; x++) {
      const I = At[x - 15] | 0, E = _t[x - 15] | 0, $ = on(I, E, 1) ^ on(I, E, 8) ^ ha(I, E, 7), T = sn(I, E, 1) ^ sn(I, E, 8) ^ ya(I, E, 7), L = At[x - 2] | 0, P = _t[x - 2] | 0, z = on(L, P, 19) ^ vr(L, P, 61) ^ ha(L, P, 6), U = sn(L, P, 19) ^ xr(L, P, 61) ^ ya(L, P, 6), w = gh(T, U, _t[x - 7], _t[x - 16]), S = mh(w, $, z, At[x - 7], At[x - 16]);
      At[x] = S | 0, _t[x] = w | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: u, Dl: l, Eh: d, El: h, Fh: f, Fl: g, Gh: m, Gl: p, Hh: y, Hl: v } = this;
    for (let x = 0; x < 80; x++) {
      const I = on(d, h, 14) ^ on(d, h, 18) ^ vr(d, h, 41), E = sn(d, h, 14) ^ sn(d, h, 18) ^ xr(d, h, 41), $ = d & f ^ ~d & m, T = h & g ^ ~h & p, L = bh(v, E, T, Ah[x], _t[x]), P = vh(L, y, I, $, Sh[x], At[x]), z = L | 0, U = on(r, i, 28) ^ vr(r, i, 34) ^ vr(r, i, 39), w = sn(r, i, 28) ^ xr(r, i, 34) ^ xr(r, i, 39), S = r & o ^ r & a ^ o & a, A = i & s ^ i & c ^ s & c;
      y = m | 0, v = p | 0, m = f | 0, p = g | 0, f = d | 0, g = h | 0, { h: d, l: h } = lt(u | 0, l | 0, P | 0, z | 0), u = a | 0, l = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const k = yh(z, w, A);
      r = wh(k, P, U, S), i = k | 0;
    }
    ({ h: r, l: i } = lt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = lt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = lt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: u, l } = lt(this.Dh | 0, this.Dl | 0, u | 0, l | 0), { h: d, l: h } = lt(this.Eh | 0, this.El | 0, d | 0, h | 0), { h: f, l: g } = lt(this.Fh | 0, this.Fl | 0, f | 0, g | 0), { h: m, l: p } = lt(this.Gh | 0, this.Gl | 0, m | 0, p | 0), { h: y, l: v } = lt(this.Hh | 0, this.Hl | 0, y | 0, v | 0), this.set(r, i, o, s, a, c, u, l, d, h, f, g, m, p, y, v);
  }
  roundClean() {
    rt(At, _t);
  }
  destroy() {
    this.destroyed = !0, rt(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
class Ih extends _h {
  Ah = me[0] | 0;
  Al = me[1] | 0;
  Bh = me[2] | 0;
  Bl = me[3] | 0;
  Ch = me[4] | 0;
  Cl = me[5] | 0;
  Dh = me[6] | 0;
  Dl = me[7] | 0;
  Eh = me[8] | 0;
  El = me[9] | 0;
  Fh = me[10] | 0;
  Fl = me[11] | 0;
  Gh = me[12] | 0;
  Gl = me[13] | 0;
  Hh = me[14] | 0;
  Hl = me[15] | 0;
  constructor() {
    super(64);
  }
}
const ue = /* @__PURE__ */ bi(
  () => new Eh(),
  /* @__PURE__ */ eu(1)
), Ph = /* @__PURE__ */ bi(
  () => new Ih(),
  /* @__PURE__ */ eu(3)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const q = (e, t, n) => nt(e, t, n), ru = tt, X = Ho, $e = (...e) => ah(...e), gn = (e) => Fo(e), jo = Yc, Vo = (e) => Ko(e), qr = /* @__PURE__ */ BigInt(0), ho = /* @__PURE__ */ BigInt(1);
function Ct(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function qo(e) {
  if (typeof e == "bigint") {
    if (!Mr(e))
      throw new RangeError("positive bigint expected, got " + e);
  } else
    ru(e);
  return e;
}
function Gr(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected number, got type=" + typeof e);
  }
  if (!Number.isSafeInteger(e)) {
    const n = t && `"${t}" `;
    throw new RangeError(n + "expected safe integer, got " + e);
  }
}
function kr(e) {
  const t = qo(e).toString(16);
  return t.length & 1 ? "0" + t : t;
}
function iu(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  return e === "" ? qr : BigInt("0x" + e);
}
function lr(e) {
  return iu(Ho(e));
}
function mn(e) {
  return iu(Ho(yo(nt(e)).reverse()));
}
function Go(e, t) {
  if (tt(t), t === 0)
    throw new RangeError("zero length");
  e = qo(e);
  const n = e.toString(16);
  if (n.length > t * 2)
    throw new RangeError("number too large");
  return Fo(n.padStart(t * 2, "0"));
}
function ou(e, t) {
  return Go(e, t).reverse();
}
function Th(e, t) {
  if (e = q(e), t = q(t), e.length !== t.length)
    return !1;
  let n = 0;
  for (let r = 0; r < e.length; r++)
    n |= e[r] ^ t[r];
  return n === 0;
}
function yo(e) {
  return Uint8Array.from(q(e));
}
function Bh(e) {
  if (typeof e != "string")
    throw new TypeError("ascii string expected, got " + typeof e);
  return Uint8Array.from(e, (t, n) => {
    const r = t.charCodeAt(0);
    if (t.length !== 1 || r > 127)
      throw new RangeError(`string contains non-ASCII character "${e[n]}" with code ${r} at position ${n}`);
    return r;
  });
}
const Mr = (e) => typeof e == "bigint" && qr <= e;
function $h(e, t, n) {
  return Mr(e) && Mr(t) && Mr(n) && t <= e && e < n;
}
function Wr(e, t, n, r) {
  if (!$h(t, n, r))
    throw new RangeError("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Wo(e) {
  if (e < qr)
    throw new Error("expected non-negative bigint, got " + e);
  let t;
  for (t = 0; e > qr; e >>= ho, t += 1)
    ;
  return t;
}
const Zo = (e) => (ho << BigInt(e)) - ho;
function Oh(e, t, n) {
  if (tt(e, "hashLen"), tt(t, "qByteLen"), typeof n != "function")
    throw new TypeError("hmacFn must be a function");
  const r = (p) => new Uint8Array(p), i = Uint8Array.of(), o = Uint8Array.of(0), s = Uint8Array.of(1), a = 1e3;
  let c = r(e), u = r(e), l = 0;
  const d = () => {
    c.fill(1), u.fill(0), l = 0;
  }, h = (...p) => n(u, $e(c, ...p)), f = (p = i) => {
    u = h(o, p), c = h(), p.length !== 0 && (u = h(s, p), c = h());
  }, g = () => {
    if (l++ >= a)
      throw new Error("drbg: tried max amount of iterations");
    let p = 0;
    const y = [];
    for (; p < t; ) {
      c = h();
      const v = c.slice();
      y.push(v), p += c.length;
    }
    return $e(...y);
  };
  return (p, y) => {
    d(), f(p);
    let v;
    for (; (v = y(g())) === void 0; )
      f();
    return d(), v;
  };
}
function Sn(e, t = {}, n = {}) {
  if (Object.prototype.toString.call(e) !== "[object Object]")
    throw new TypeError("expected valid options object");
  function r(o, s, a) {
    if (!a && s !== "function" && !Object.hasOwn(e, o))
      throw new TypeError(`param "${o}" is invalid: expected own property`);
    const c = e[o];
    if (a && c === void 0)
      return;
    const u = typeof c;
    if (u !== s || c === null)
      throw new TypeError(`param "${o}" is invalid: expected ${s}, got ${u}`);
  }
  const i = (o, s) => Object.entries(o).forEach(([a, c]) => r(a, c, s));
  i(t, !1), i(n, !0);
}
const wa = () => {
  throw new Error("not implemented");
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const be = /* @__PURE__ */ BigInt(0), oe = /* @__PURE__ */ BigInt(1), Dt = /* @__PURE__ */ BigInt(2), su = /* @__PURE__ */ BigInt(3), au = /* @__PURE__ */ BigInt(4), cu = /* @__PURE__ */ BigInt(5), Rh = /* @__PURE__ */ BigInt(7), uu = /* @__PURE__ */ BigInt(8), Lh = /* @__PURE__ */ BigInt(9), lu = /* @__PURE__ */ BigInt(16);
function ce(e, t) {
  if (t <= be)
    throw new Error("mod: expected positive modulus, got " + t);
  const n = e % t;
  return n >= be ? n : t + n;
}
function re(e, t, n) {
  if (t < be)
    throw new Error("pow2: expected non-negative exponent, got " + t);
  let r = e;
  for (; t-- > be; )
    r *= r, r %= n;
  return r;
}
function ga(e, t) {
  if (e === be)
    throw new Error("invert: expected non-zero number");
  if (t <= be)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = ce(e, t), r = t, i = be, o = oe;
  for (; n !== be; ) {
    const a = r / n, c = r - n * a, u = i - o * a;
    r = n, n = c, i = o, o = u;
  }
  if (r !== oe)
    throw new Error("invert: does not exist");
  return ce(i, t);
}
function Xo(e, t, n) {
  const r = e;
  if (!r.eql(r.sqr(t), n))
    throw new Error("Cannot find square root");
}
function du(e, t) {
  const n = e, r = (n.ORDER + oe) / au, i = n.pow(t, r);
  return Xo(n, i, t), i;
}
function Ch(e, t) {
  const n = e, r = (n.ORDER - cu) / uu, i = n.mul(t, Dt), o = n.pow(i, r), s = n.mul(t, o), a = n.mul(n.mul(s, Dt), o), c = n.mul(s, n.sub(a, n.ONE));
  return Xo(n, c, t), c;
}
function zh(e) {
  const t = vi(e), n = fu(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + Rh) / lu;
  return ((a, c) => {
    const u = a;
    let l = u.pow(c, s), d = u.mul(l, r);
    const h = u.mul(l, i), f = u.mul(l, o), g = u.eql(u.sqr(d), c), m = u.eql(u.sqr(h), c);
    l = u.cmov(l, d, g), d = u.cmov(f, h, m);
    const p = u.eql(u.sqr(d), c), y = u.cmov(l, d, p);
    return Xo(u, y, c), y;
  });
}
function fu(e) {
  if (e < su)
    throw new Error("sqrt is not defined for small field");
  let t = e - oe, n = 0;
  for (; t % Dt === be; )
    t /= Dt, n++;
  let r = Dt;
  const i = vi(e);
  for (; ma(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return du;
  let o = i.pow(r, t);
  const s = (t + oe) / Dt;
  return function(c, u) {
    const l = c;
    if (l.is0(u))
      return u;
    if (ma(l, u) !== 1)
      throw new Error("Cannot find square root");
    let d = n, h = l.mul(l.ONE, o), f = l.pow(u, t), g = l.pow(u, s);
    for (; !l.eql(f, l.ONE); ) {
      if (l.is0(f))
        return l.ZERO;
      let m = 1, p = l.sqr(f);
      for (; !l.eql(p, l.ONE); )
        if (m++, p = l.sqr(p), m === d)
          throw new Error("Cannot find square root");
      const y = oe << BigInt(d - m - 1), v = l.pow(h, y);
      d = m, h = l.sqr(v), f = l.mul(f, h), g = l.mul(g, v);
    }
    return g;
  };
}
function Uh(e) {
  return e % au === su ? du : e % uu === cu ? Ch : e % lu === Lh ? zh(e) : fu(e);
}
const Nt = (e, t) => (ce(e, t) & oe) === oe, Nh = [
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
function Mh(e) {
  const t = {
    ORDER: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = Nh.reduce((r, i) => (r[i] = "function", r), t);
  if (Sn(e, n), Gr(e.BYTES, "BYTES"), Gr(e.BITS, "BITS"), e.BYTES < 1 || e.BITS < 1)
    throw new Error("invalid field: expected BYTES/BITS > 0");
  if (e.ORDER <= oe)
    throw new Error("invalid field: expected ORDER > 1, got " + e.ORDER);
  return e;
}
function Hh(e, t, n) {
  const r = e;
  if (n < be)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === be)
    return r.ONE;
  if (n === oe)
    return t;
  let i = r.ONE, o = t;
  for (; n > be; )
    n & oe && (i = r.mul(i, o)), o = r.sqr(o), n >>= oe;
  return i;
}
function pu(e, t, n = !1) {
  const r = e, i = new Array(t.length).fill(n ? r.ZERO : void 0), o = t.reduce((a, c, u) => r.is0(c) ? a : (i[u] = a, r.mul(a, c)), r.ONE), s = r.inv(o);
  return t.reduceRight((a, c, u) => r.is0(c) ? a : (i[u] = r.mul(a, i[u]), r.mul(a, c)), s), i;
}
function ma(e, t) {
  const n = e, r = (n.ORDER - oe) / Dt, i = n.pow(t, r), o = n.eql(i, n.ONE), s = n.eql(i, n.ZERO), a = n.eql(i, n.neg(n.ONE));
  if (!o && !s && !a)
    throw new Error("invalid Legendre symbol result");
  return o ? 1 : s ? 0 : -1;
}
function Fh(e, t) {
  if (t !== void 0 && ru(t), e <= be)
    throw new Error("invalid n length: expected positive n, got " + e);
  if (t !== void 0 && t < 1)
    throw new Error("invalid n length: expected positive bit length, got " + t);
  const n = Wo(e);
  if (t !== void 0 && t < n)
    throw new Error(`invalid n length: expected bit length (${n}) >= n.length (${t})`);
  const r = t !== void 0 ? t : n, i = Math.ceil(r / 8);
  return { nBitLength: r, nByteLength: i };
}
const ba = /* @__PURE__ */ new WeakMap();
class hu {
  ORDER;
  BITS;
  BYTES;
  isLE;
  ZERO = be;
  ONE = oe;
  _lengths;
  _mod;
  constructor(t, n = {}) {
    if (t <= oe)
      throw new Error("invalid field: expected ORDER > 1, got " + t);
    let r;
    this.isLE = !1, n != null && typeof n == "object" && (typeof n.BITS == "number" && (r = n.BITS), typeof n.sqrt == "function" && Object.defineProperty(this, "sqrt", { value: n.sqrt, enumerable: !0 }), typeof n.isLE == "boolean" && (this.isLE = n.isLE), n.allowedLengths && (this._lengths = Object.freeze(n.allowedLengths.slice())), typeof n.modFromBytes == "boolean" && (this._mod = n.modFromBytes));
    const { nBitLength: i, nByteLength: o } = Fh(t, r);
    if (o > 2048)
      throw new Error("invalid field: expected ORDER of <= 2048 bytes");
    this.ORDER = t, this.BITS = i, this.BYTES = o, Object.freeze(this);
  }
  create(t) {
    return ce(t, this.ORDER);
  }
  isValid(t) {
    if (typeof t != "bigint")
      throw new TypeError("invalid field element: expected bigint, got " + typeof t);
    return be <= t && t < this.ORDER;
  }
  is0(t) {
    return t === be;
  }
  // is valid and invertible
  isValidNot0(t) {
    return !this.is0(t) && this.isValid(t);
  }
  isOdd(t) {
    return (t & oe) === oe;
  }
  neg(t) {
    return ce(-t, this.ORDER);
  }
  eql(t, n) {
    return t === n;
  }
  sqr(t) {
    return ce(t * t, this.ORDER);
  }
  add(t, n) {
    return ce(t + n, this.ORDER);
  }
  sub(t, n) {
    return ce(t - n, this.ORDER);
  }
  mul(t, n) {
    return ce(t * n, this.ORDER);
  }
  pow(t, n) {
    return Hh(this, t, n);
  }
  div(t, n) {
    return ce(t * ga(n, this.ORDER), this.ORDER);
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
    return ga(t, this.ORDER);
  }
  sqrt(t) {
    let n = ba.get(this);
    return n || ba.set(this, n = Uh(this.ORDER)), n(this, t);
  }
  toBytes(t) {
    return this.isLE ? ou(t, this.BYTES) : Go(t, this.BYTES);
  }
  fromBytes(t, n = !1) {
    q(t);
    const { _lengths: r, BYTES: i, isLE: o, ORDER: s, _mod: a } = this;
    if (r) {
      if (t.length < 1 || !r.includes(t.length) || t.length > i)
        throw new Error("Field.fromBytes: expected " + r + " bytes, got " + t.length);
      const u = new Uint8Array(i);
      u.set(t, o ? 0 : u.length - t.length), t = u;
    }
    if (t.length !== i)
      throw new Error("Field.fromBytes: expected " + i + " bytes, got " + t.length);
    let c = o ? mn(t) : lr(t);
    if (a && (c = ce(c, s)), !n && !this.isValid(c))
      throw new Error("invalid field element: outside of range 0..ORDER");
    return c;
  }
  // TODO: we don't need it here, move out to separate fn
  invertBatch(t) {
    return pu(this, t);
  }
  // We can't move this out because Fp6, Fp12 implement it
  // and it's unclear what to return in there.
  cmov(t, n, r) {
    return Ct(r, "condition"), r ? n : t;
  }
}
Object.freeze(hu.prototype);
function vi(e, t = {}) {
  return new hu(e, t);
}
function yu(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  if (e <= oe)
    throw new Error("field order must be greater than 1");
  const t = Wo(e - oe);
  return Math.ceil(t / 8);
}
function wu(e) {
  const t = yu(e);
  return t + Math.ceil(t / 2);
}
function gu(e, t, n = !1) {
  q(e);
  const r = e.length, i = yu(t), o = Math.max(wu(t), 16);
  if (r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? mn(e) : lr(e), a = ce(s, t - oe) + oe;
  return n ? ou(a, i) : Go(a, i);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const bn = /* @__PURE__ */ BigInt(0), jt = /* @__PURE__ */ BigInt(1);
function Zr(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function Gn(e, t) {
  const n = pu(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function mu(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function Ki(e, t) {
  mu(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = Zo(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function va(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += jt);
  const u = t * r, l = u + Math.abs(a) - 1, d = a === 0, h = a < 0, f = t % 2 !== 0;
  return { nextN: c, offset: l, isZero: d, isNeg: h, isNegF: f, offsetF: u };
}
const Di = /* @__PURE__ */ new WeakMap(), bu = /* @__PURE__ */ new WeakMap();
function ji(e) {
  return bu.get(e) || 1;
}
function xa(e) {
  if (e !== bn)
    throw new Error("invalid wNAF");
}
class vu {
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
    for (; n > bn; )
      n & jt && (r = r.add(i)), i = i.double(), n >>= jt;
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
    const { windows: r, windowSize: i } = Ki(n, this.bits), o = [];
    let s = t, a = s;
    for (let c = 0; c < r; c++) {
      a = s, o.push(a);
      for (let u = 1; u < i; u++)
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
    const s = Ki(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: u, isZero: l, isNeg: d, isNegF: h, offsetF: f } = va(r, a, s);
      r = c, l ? o = o.add(Zr(h, n[f])) : i = i.add(Zr(d, n[u]));
    }
    return xa(r), { p: i, f: o };
  }
  /**
   * Implements unsafe EC multiplication using precomputed tables
   * and w-ary non-adjacent form.
   * @param acc - accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = Ki(t, this.bits);
    for (let s = 0; s < o.windows && r !== bn; s++) {
      const { nextN: a, offset: c, isZero: u, isNeg: l } = va(r, s, o);
      if (r = a, !u) {
        const d = n[c];
        i = i.add(l ? d.negate() : d);
      }
    }
    return xa(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Di.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Di.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = ji(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = ji(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    mu(n, this.bits), bu.set(t, n), Di.delete(t);
  }
  hasCache(t) {
    return ji(t) !== 1;
  }
}
function Kh(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > bn || r > bn; )
    n & jt && (o = o.add(i)), r & jt && (s = s.add(i)), i = i.double(), n >>= jt, r >>= jt;
  return { p1: o, p2: s };
}
function ka(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return Mh(t), t;
  } else
    return vi(e, { isLE: n });
}
function xu(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const u = t[c];
    if (!(typeof u == "bigint" && u > bn))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = ka(t.p, n.Fp, r), o = ka(t.n, n.Fn, r), a = ["Gx", "Gy", "a", e === "weierstrass" ? "b" : "d"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
function Yo(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const It = /* @__PURE__ */ BigInt(0), pe = /* @__PURE__ */ BigInt(1), Vi = /* @__PURE__ */ BigInt(2), Dh = /* @__PURE__ */ BigInt(8);
function jh(e, t, n, r) {
  const i = e.sqr(n), o = e.sqr(r), s = e.add(e.mul(t.a, i), o), a = e.add(e.ONE, e.mul(t.d, e.mul(i, o)));
  return e.eql(s, a);
}
function Vh(e, t = {}) {
  const n = t, r = xu("edwards", e, n, n.FpFnLE), { Fp: i, Fn: o } = r;
  let s = r.CURVE;
  const { h: a } = s;
  Sn(n, {}, { uvRatio: "function" });
  const c = Vi << BigInt(o.BYTES * 8) - pe, u = (m) => i.create(m), l = n.uvRatio === void 0 ? (m, p) => {
    try {
      return { isValid: !0, value: i.sqrt(i.div(m, p)) };
    } catch {
      return { isValid: !1, value: It };
    }
  } : n.uvRatio;
  if (!jh(i, s, s.Gx, s.Gy))
    throw new Error("bad curve params: generator point");
  function d(m, p, y = !1) {
    const v = y ? pe : It;
    return Wr("coordinate " + m, p, v, c), p;
  }
  function h(m) {
    if (!(m instanceof f))
      throw new Error("EdwardsPoint expected");
  }
  class f {
    // base / generator point
    static BASE = new f(s.Gx, s.Gy, pe, u(s.Gx * s.Gy));
    // zero / infinity / identity point
    static ZERO = new f(It, pe, pe, It);
    // 0, 1, 1, 0
    // math field
    static Fp = i;
    // scalar field
    static Fn = o;
    X;
    Y;
    Z;
    T;
    constructor(p, y, v, x) {
      this.X = d("x", p), this.Y = d("y", y), this.Z = d("z", v, !0), this.T = d("t", x), Object.freeze(this);
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
      if (p instanceof f)
        throw new Error("extended point not allowed");
      const { x: y, y: v } = p || {};
      return d("x", y), d("y", v), new f(y, v, pe, u(y * v));
    }
    // Uses algo from RFC8032 5.1.3.
    static fromBytes(p, y = !1) {
      const v = i.BYTES, { a: x, d: I } = s;
      p = yo(q(p, v, "point")), Ct(y, "zip215");
      const E = yo(p), $ = p[v - 1];
      E[v - 1] = $ & -129;
      const T = mn(E), L = y ? c : i.ORDER;
      Wr("point.y", T, It, L);
      const P = u(T * T), z = u(P - pe), U = u(I * P - x);
      let { isValid: w, value: S } = l(z, U);
      if (!w)
        throw new Error("bad point: invalid y coordinate");
      const A = (S & pe) === pe, k = ($ & 128) !== 0;
      if (!y && S === It && k)
        throw new Error("bad point: x=0 and x_0=1");
      return k !== A && (S = u(-S)), f.fromAffine({ x: S, y: T });
    }
    static fromHex(p, y = !1) {
      return f.fromBytes(gn(p), y);
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    precompute(p = 8, y = !0) {
      return g.createCache(this, p), y || this.multiply(Vi), this;
    }
    // Useful in fromAffine() - not for fromBytes(), which always created valid points.
    assertValidity() {
      const p = this, { a: y, d: v } = s;
      if (p.is0())
        throw new Error("bad point: ZERO");
      const { X: x, Y: I, Z: E, T: $ } = p, T = u(x * x), L = u(I * I), P = u(E * E), z = u(P * P), U = u(T * y), w = u(P * u(U + L)), S = u(z + u(v * u(T * L)));
      if (w !== S)
        throw new Error("bad point: equation left != right (1)");
      const A = u(x * I), k = u(E * $);
      if (A !== k)
        throw new Error("bad point: equation left != right (2)");
    }
    // Compare one point to another.
    equals(p) {
      h(p);
      const { X: y, Y: v, Z: x } = this, { X: I, Y: E, Z: $ } = p, T = u(y * $), L = u(I * x), P = u(v * $), z = u(E * x);
      return T === L && P === z;
    }
    is0() {
      return this.equals(f.ZERO);
    }
    negate() {
      return new f(u(-this.X), this.Y, this.Z, u(-this.T));
    }
    // Fast algo for doubling Extended Point.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#doubling-dbl-2008-hwcd
    // Cost: 4M + 4S + 1*a + 6add + 1*2.
    double() {
      const { a: p } = s, { X: y, Y: v, Z: x } = this, I = u(y * y), E = u(v * v), $ = u(Vi * u(x * x)), T = u(p * I), L = y + v, P = u(u(L * L) - I - E), z = T + E, U = z - $, w = T - E, S = u(P * U), A = u(z * w), k = u(P * w), _ = u(U * z);
      return new f(S, A, _, k);
    }
    // Fast algo for adding 2 Extended Points.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#addition-add-2008-hwcd
    // Cost: 9M + 1*a + 1*d + 7add.
    add(p) {
      h(p);
      const { a: y, d: v } = s, { X: x, Y: I, Z: E, T: $ } = this, { X: T, Y: L, Z: P, T: z } = p, U = u(x * T), w = u(I * L), S = u($ * v * z), A = u(E * P), k = u((x + I) * (T + L) - U - w), _ = A - S, B = A + S, R = u(w - y * U), O = u(k * _), C = u(B * R), N = u(k * R), M = u(_ * B);
      return new f(O, C, M, N);
    }
    subtract(p) {
      return h(p), this.add(p.negate());
    }
    // Constant-time multiplication.
    multiply(p) {
      if (!o.isValidNot0(p))
        throw new RangeError("invalid scalar: expected 1 <= sc < curve.n");
      const { p: y, f: v } = g.cached(this, p, (x) => Gn(f, x));
      return Gn(f, [y, v])[0];
    }
    // Non-constant-time multiplication. Uses double-and-add algorithm.
    // It's faster, but should only be used when you don't care about
    // an exposed private key e.g. sig verification.
    // Keeps the same subgroup-scalar contract: 0 is allowed for public-scalar callers, but
    // n and larger values are rejected instead of being reduced mod n to the identity point.
    multiplyUnsafe(p) {
      if (!o.isValid(p))
        throw new RangeError("invalid scalar: expected 0 <= sc < curve.n");
      return p === It ? f.ZERO : this.is0() || p === pe ? this : g.unsafe(this, p, (y) => Gn(f, y));
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
      return g.unsafe(this, s.n).is0();
    }
    // Converts Extended point to default (x, y) coordinates.
    // Can accept precomputed Z^-1 - for example, from invertBatch.
    toAffine(p) {
      const y = this;
      let v = p;
      const { X: x, Y: I, Z: E } = y, $ = y.is0();
      v == null && (v = $ ? Dh : i.inv(E));
      const T = u(x * v), L = u(I * v), P = i.mul(E, v);
      if ($)
        return { x: It, y: pe };
      if (P !== pe)
        throw new Error("invZ was invalid");
      return { x: T, y: L };
    }
    clearCofactor() {
      return a === pe ? this : this.multiplyUnsafe(a);
    }
    toBytes() {
      const { x: p, y } = this.toAffine(), v = i.toBytes(y);
      return v[v.length - 1] |= p & pe ? 128 : 0, v;
    }
    toHex() {
      return X(this.toBytes());
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const g = new vu(f, o.BITS);
  return o.BITS >= 8 && f.BASE.precompute(8), Object.freeze(f.prototype), Object.freeze(f), f;
}
class qh {
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
    wa();
  }
  static fromHex(t) {
    wa();
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
    return X(this.toBytes());
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
function Gh(e, t, n = {}) {
  if (typeof t != "function")
    throw new Error('"hash" function param is required');
  const r = t, i = n;
  Sn(i, {}, {
    adjustScalarBytes: "function",
    randomBytes: "function",
    domain: "function",
    prehash: "function",
    zip215: "boolean",
    mapToCurve: "function"
  });
  const { prehash: o } = i, { BASE: s, Fp: a, Fn: c } = e, u = r.outputLen, l = 2 * a.BYTES;
  if (u !== void 0 && (Gr(u, "hash.outputLen"), u !== l))
    throw new Error(`hash.outputLen must be ${l}, got ${u}`);
  const d = i.randomBytes === void 0 ? Vo : i.randomBytes, h = i.adjustScalarBytes === void 0 ? (w) => w : i.adjustScalarBytes, f = i.domain === void 0 ? (w, S, A) => {
    if (Ct(A, "phflag"), S.length || A)
      throw new Error("Contexts/pre-hash are not supported");
    return w;
  } : i.domain;
  function g(w) {
    return c.create(mn(w));
  }
  function m(w) {
    const S = T.secretKey;
    q(w, T.secretKey, "secretKey");
    const A = q(r(w), 2 * S, "hashedSecretKey"), k = h(A.slice(0, S)), _ = A.slice(S, 2 * S), B = g(k);
    return { head: k, prefix: _, scalar: B };
  }
  function p(w) {
    const { head: S, prefix: A, scalar: k } = m(w), _ = s.multiply(k), B = _.toBytes();
    return { head: S, prefix: A, scalar: k, point: _, pointBytes: B };
  }
  function y(w) {
    return p(w).pointBytes;
  }
  function v(w = Uint8Array.of(), ...S) {
    const A = $e(...S);
    return g(r(f(A, q(w, void 0, "context"), !!o)));
  }
  function x(w, S, A = {}) {
    w = q(w, void 0, "message"), o && (w = o(w));
    const { prefix: k, scalar: _, pointBytes: B } = p(S), R = v(A.context, k, w), O = s.multiply(R).toBytes(), C = v(A.context, O, B, w), N = c.create(R + C * _);
    if (!c.isValid(N))
      throw new Error("sign failed: invalid s");
    const M = $e(O, c.toBytes(N));
    return q(M, T.signature, "result");
  }
  const I = {
    zip215: i.zip215
  };
  function E(w, S, A, k = I) {
    const { context: _ } = k, B = k.zip215 === void 0 ? !!I.zip215 : k.zip215, R = T.signature;
    w = q(w, R, "signature"), S = q(S, void 0, "message"), A = q(A, T.publicKey, "publicKey"), B !== void 0 && Ct(B, "zip215"), o && (S = o(S));
    const O = R / 2, C = w.subarray(0, O), N = mn(w.subarray(O, R));
    let M, G, H;
    try {
      M = e.fromBytes(A, B), G = e.fromBytes(C, B), H = s.multiplyUnsafe(N);
    } catch {
      return !1;
    }
    if (!B && M.isSmallOrder())
      return !1;
    const ee = v(_, C, A, S);
    return G.add(M.multiplyUnsafe(ee)).subtract(H).clearCofactor().is0();
  }
  const $ = a.BYTES, T = {
    secretKey: $,
    publicKey: $,
    signature: 2 * $,
    seed: $
  };
  function L(w) {
    return w = w === void 0 ? d(T.seed) : w, q(w, T.seed, "seed");
  }
  function P(w) {
    return jo(w) && w.length === T.secretKey;
  }
  function z(w, S) {
    try {
      return !!e.fromBytes(w, S === void 0 ? I.zip215 : S);
    } catch {
      return !1;
    }
  }
  const U = {
    getExtendedPublicKey: p,
    randomSecretKey: L,
    isValidSecretKey: P,
    isValidPublicKey: z,
    /**
     * Converts ed public key to x public key. Uses formula:
     * - ed25519:
     *   - `(u, v) = ((1+y)/(1-y), sqrt(-486664)*u/x)`
     *   - `(x, y) = (sqrt(-486664)*u/v, (u-1)/(u+1))`
     * - ed448:
     *   - `(u, v) = ((y-1)/(y+1), sqrt(156324)*u/x)`
     *   - `(x, y) = (sqrt(156324)*u/v, (1+u)/(1-u))`
     */
    toMontgomery(w) {
      const { y: S } = e.fromBytes(w), A = T.publicKey, k = A === 32;
      if (!k && A !== 57)
        throw new Error("only defined for 25519 and 448");
      const _ = k ? a.div(pe + S, pe - S) : a.div(S - pe, S + pe);
      return a.toBytes(_);
    },
    toMontgomerySecret(w) {
      const S = T.secretKey;
      q(w, S);
      const A = r(w.subarray(0, S));
      return h(A).subarray(0, S);
    }
  };
  return Object.freeze(T), Object.freeze(U), Object.freeze({
    keygen: Yo(L, y),
    getPublicKey: y,
    sign: x,
    verify: E,
    utils: U,
    Point: e,
    lengths: T
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Wh = /* @__PURE__ */ BigInt(0), Wn = /* @__PURE__ */ BigInt(1), Ea = /* @__PURE__ */ BigInt(2), Zh = /* @__PURE__ */ BigInt(5), Xh = /* @__PURE__ */ BigInt(8), Jn = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed"), ku = {
  p: Jn,
  n: BigInt("0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"),
  h: Xh,
  a: BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffec"),
  d: BigInt("0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3"),
  Gx: BigInt("0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a"),
  Gy: BigInt("0x6666666666666666666666666666666666666666666666666666666666666658")
};
function Yh(e) {
  const t = BigInt(10), n = BigInt(20), r = BigInt(40), i = BigInt(80), o = Jn, a = e * e % o * e % o, c = re(a, Ea, o) * a % o, u = re(c, Wn, o) * e % o, l = re(u, Zh, o) * u % o, d = re(l, t, o) * l % o, h = re(d, n, o) * d % o, f = re(h, r, o) * h % o, g = re(f, i, o) * f % o, m = re(g, i, o) * f % o, p = re(m, t, o) * l % o;
  return { pow_p_5_8: re(p, Ea, o) * e % o, b2: a };
}
function Jh(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const wo = /* @__PURE__ */ BigInt("19681161376707505956807079304988542015446066515923890162744021073123829784752");
function Eu(e, t) {
  const n = Jn, r = ce(t * t * t, n), i = ce(r * r * t, n), o = Yh(e * i).pow_p_5_8;
  let s = ce(e * r * o, n);
  const a = ce(t * s * s, n), c = s, u = ce(s * wo, n), l = a === e, d = a === ce(-e, n), h = a === ce(-e * wo, n);
  return l && (s = c), (d || h) && (s = u), Nt(s, n) && (s = ce(-s, n)), { isValid: l || d, value: s };
}
const Vt = /* @__PURE__ */ Vh(ku, { uvRatio: Eu }), Mt = Vt.Fp, Qh = Vt.Fn;
function ey(e) {
  return Gh(Vt, Ph, Object.assign({ adjustScalarBytes: Jh, zip215: !0 }, e));
}
const Ot = /* @__PURE__ */ ey({}), Sa = wo, ty = /* @__PURE__ */ BigInt("54469307008909316920995813868745141605393597292927456921205312896311721017578"), Aa = (e) => Eu(Wn, e), ny = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), ry = (e) => Mt.create(mn(e) & ny);
class Pe extends qh {
  // Do NOT change syntax: the following gymnastics is done,
  // because typescript strips comments, which makes bundlers disable tree-shaking.
  // prettier-ignore
  static BASE = new Pe(Vt.BASE);
  // prettier-ignore
  static ZERO = new Pe(Vt.ZERO);
  // prettier-ignore
  static Fp = Mt;
  // prettier-ignore
  static Fn = Qh;
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
    return new Pe(Vt.fromAffine(t));
  }
  assertSame(t) {
    if (!(t instanceof Pe))
      throw new Error("RistrettoPoint expected");
  }
  init(t) {
    return new Pe(t);
  }
  static fromBytes(t) {
    nt(t, 32);
    const { a: n, d: r } = ku, i = Jn, o = (I) => Mt.create(I), s = ry(t);
    if (!Th(Mt.toBytes(s), t) || Nt(s, i))
      throw new Error("invalid ristretto255 encoding 1");
    const a = o(s * s), c = o(Wn + n * a), u = o(Wn - n * a), l = o(c * c), d = o(u * u), h = o(n * r * l - d), { isValid: f, value: g } = Aa(o(h * d)), m = o(g * u), p = o(g * m * h);
    let y = o((s + s) * m);
    Nt(y, i) && (y = o(-y));
    const v = o(c * p), x = o(y * v);
    if (!f || Nt(x, i) || v === Wh)
      throw new Error("invalid ristretto255 encoding 2");
    return new Pe(new Vt(y, v, Wn, x));
  }
  /**
   * Converts ristretto-encoded string to ristretto point.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-decode).
   * @param hex - Ristretto-encoded 32 bytes. Not every 32-byte string is valid ristretto encoding
   */
  static fromHex(t) {
    return Pe.fromBytes(Fo(t));
  }
  /**
   * Encodes ristretto point to Uint8Array.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-encode).
   */
  toBytes() {
    let { X: t, Y: n, Z: r, T: i } = this.ep;
    const o = Jn, s = (p) => Mt.create(p), a = s(s(r + n) * s(r - n)), c = s(t * n), u = s(c * c), { value: l } = Aa(s(a * u)), d = s(l * a), h = s(l * c), f = s(d * h * i);
    let g;
    if (Nt(i * f, o)) {
      let p = s(n * Sa), y = s(t * Sa);
      t = p, n = y, g = s(d * ty);
    } else
      g = h;
    Nt(t * f, o) && (n = s(-n));
    let m = s((r - n) * g);
    return Nt(m, o) && (m = s(-m)), Mt.toBytes(m);
  }
  /**
   * Compares two Ristretto points.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-equals).
   */
  equals(t) {
    this.assertSame(t);
    const { X: n, Y: r } = this.ep, { X: i, Y: o } = t.ep, s = (u) => Mt.create(u), a = s(n * o) === s(r * i), c = s(r * o) === s(n * i);
    return a || c;
  }
  is0() {
    return this.equals(Pe.ZERO);
  }
}
Object.freeze(Pe.BASE);
Object.freeze(Pe.ZERO);
Object.freeze(Pe.prototype);
Object.freeze(Pe);
class _a {
  oHash;
  iHash;
  blockLen;
  outputLen;
  canXOF = !1;
  finished = !1;
  destroyed = !1;
  constructor(t, n) {
    if (Jc(t), nt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), rt(i);
  }
  update(t) {
    return wn(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    wn(this), Mo(t, this), this.finished = !0;
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
const iy = /* @__PURE__ */ (() => {
  const e = ((t, n, r) => new _a(t, n).update(r).digest());
  return e.create = (t, n) => new _a(t, n), e;
})();
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Ia = (e, t) => (e + (e >= 0 ? t : -t) / Su) / t;
function oy(e, t, n) {
  Wr("scalar", e, Qe, n);
  const [[r, i], [o, s]] = t, a = Ia(s * e, n), c = Ia(-i * e, n);
  let u = e - a * r - c * o, l = -a * i - c * s;
  const d = u < Qe, h = l < Qe;
  d && (u = -u), h && (l = -l);
  const f = Zo(Math.ceil(Wo(n) / 2)) + $t;
  if (u < Qe || u >= f || l < Qe || l >= f)
    throw new Error("splitScalar (endomorphism): failed for k");
  return { k1neg: d, k1: u, k2neg: h, k2: l };
}
function go(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function qi(e, t) {
  Sn(e);
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Ct(n.lowS, "lowS"), Ct(n.prehash, "prehash"), n.format !== void 0 && go(n.format), n;
}
class sy extends Error {
  constructor(t = "") {
    super(t);
  }
}
const Ue = {
  // asn.1 DER encoding utils
  Err: sy,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = Ue;
      if (Gr(e, "tag"), e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (typeof t != "string")
        throw new TypeError('"data" expected string, got type=' + typeof t);
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = kr(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? kr(i.length / 2 | 128) : "";
      return kr(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = Ue;
      t = q(t, void 0, "DER data");
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
        const u = t.subarray(r, r + c);
        if (u.length !== c)
          throw new n("tlv.decode: length bytes not complete");
        if (u[0] === 0)
          throw new n("tlv.decode(long): zero leftmost byte");
        for (const l of u)
          s = s << 8 | l;
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
      const { Err: t } = Ue;
      if (qo(e), e < Qe)
        throw new t("integer: negative integers are not allowed");
      let n = kr(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = Ue;
      if (e.length < 1)
        throw new t("invalid signature integer: empty");
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e.length > 1 && e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return lr(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = Ue, i = q(e, void 0, "signature"), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: u, l } = r.decode(2, c);
    if (l.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(u) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = Ue, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
};
Object.freeze(Ue._tlv);
Object.freeze(Ue._int);
Object.freeze(Ue);
const Qe = /* @__PURE__ */ BigInt(0), $t = /* @__PURE__ */ BigInt(1), Su = /* @__PURE__ */ BigInt(2), Er = /* @__PURE__ */ BigInt(3), ay = /* @__PURE__ */ BigInt(4);
function cy(e, t = {}) {
  const n = xu("weierstrass", e, t), r = n.Fp, i = n.Fn;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  Sn(t, {}, {
    allowInfinityPoint: "boolean",
    clearCofactor: "function",
    isTorsionFree: "function",
    fromBytes: "function",
    toBytes: "function",
    endo: "object"
  });
  const { endo: c, allowInfinityPoint: u } = t;
  if (c && (!r.is0(o.a) || typeof c.beta != "bigint" || !Array.isArray(c.basises)))
    throw new Error('invalid endo: expected "beta": bigint and "basises": array');
  const l = _u(r, i);
  function d() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function h(U, w, S) {
    if (u && w.is0())
      return Uint8Array.of(0);
    const { x: A, y: k } = w.toAffine(), _ = r.toBytes(A);
    if (Ct(S, "isCompressed"), S) {
      d();
      const B = !r.isOdd(k);
      return $e(Au(B), _);
    } else
      return $e(Uint8Array.of(4), _, r.toBytes(k));
  }
  function f(U) {
    q(U, void 0, "Point");
    const { publicKey: w, publicKeyUncompressed: S } = l, A = U.length, k = U[0], _ = U.subarray(1);
    if (u && A === 1 && k === 0)
      return { x: r.ZERO, y: r.ZERO };
    if (A === w && (k === 2 || k === 3)) {
      const B = r.fromBytes(_);
      if (!r.isValid(B))
        throw new Error("bad point: is not on curve, wrong x");
      const R = p(B);
      let O;
      try {
        O = r.sqrt(R);
      } catch (M) {
        const G = M instanceof Error ? ": " + M.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + G);
      }
      d();
      const C = r.isOdd(O);
      return (k & 1) === 1 !== C && (O = r.neg(O)), { x: B, y: O };
    } else if (A === S && k === 4) {
      const B = r.BYTES, R = r.fromBytes(_.subarray(0, B)), O = r.fromBytes(_.subarray(B, B * 2));
      if (!y(R, O))
        throw new Error("bad point: is not on curve");
      return { x: R, y: O };
    } else
      throw new Error(`bad point: got length ${A}, expected compressed=${w} or uncompressed=${S}`);
  }
  const g = t.toBytes === void 0 ? h : t.toBytes, m = t.fromBytes === void 0 ? f : t.fromBytes;
  function p(U) {
    const w = r.sqr(U), S = r.mul(w, U);
    return r.add(r.add(S, r.mul(U, o.a)), o.b);
  }
  function y(U, w) {
    const S = r.sqr(w), A = p(U);
    return r.eql(S, A);
  }
  if (!y(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const v = r.mul(r.pow(o.a, Er), ay), x = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(v, x)))
    throw new Error("bad curve params: a or b");
  function I(U, w, S = !1) {
    if (!r.isValid(w) || S && r.is0(w))
      throw new Error(`bad point coordinate ${U}`);
    return w;
  }
  function E(U) {
    if (!(U instanceof L))
      throw new Error("Weierstrass Point expected");
  }
  function $(U) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return oy(U, c.basises, i.ORDER);
  }
  function T(U, w, S, A, k) {
    return S = new L(r.mul(S.X, U), S.Y, S.Z), w = Zr(A, w), S = Zr(k, S), w.add(S);
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
    constructor(w, S, A) {
      this.X = I("x", w), this.Y = I("y", S, !0), this.Z = I("z", A), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(w) {
      const { x: S, y: A } = w || {};
      if (!w || !r.isValid(S) || !r.isValid(A))
        throw new Error("invalid affine point");
      if (w instanceof L)
        throw new Error("projective point not allowed");
      return r.is0(S) && r.is0(A) ? L.ZERO : new L(S, A, r.ONE);
    }
    static fromBytes(w) {
      const S = L.fromAffine(m(q(w, void 0, "point")));
      return S.assertValidity(), S;
    }
    static fromHex(w) {
      return L.fromBytes(gn(w));
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
    precompute(w = 8, S = !0) {
      return z.createCache(this, w), S || this.multiply(Er), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      const w = this;
      if (w.is0()) {
        if (t.allowInfinityPoint && r.is0(w.X) && r.eql(w.Y, r.ONE) && r.is0(w.Z))
          return;
        throw new Error("bad point: ZERO");
      }
      const { x: S, y: A } = w.toAffine();
      if (!r.isValid(S) || !r.isValid(A))
        throw new Error("bad point: x or y not field elements");
      if (!y(S, A))
        throw new Error("bad point: equation left != right");
      if (!w.isTorsionFree())
        throw new Error("bad point: not in prime-order subgroup");
    }
    hasEvenY() {
      const { y: w } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(w);
    }
    /** Compare one point to another. */
    equals(w) {
      E(w);
      const { X: S, Y: A, Z: k } = this, { X: _, Y: B, Z: R } = w, O = r.eql(r.mul(S, R), r.mul(_, k)), C = r.eql(r.mul(A, R), r.mul(B, k));
      return O && C;
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
      const { a: w, b: S } = o, A = r.mul(S, Er), { X: k, Y: _, Z: B } = this;
      let R = r.ZERO, O = r.ZERO, C = r.ZERO, N = r.mul(k, k), M = r.mul(_, _), G = r.mul(B, B), H = r.mul(k, _);
      return H = r.add(H, H), C = r.mul(k, B), C = r.add(C, C), R = r.mul(w, C), O = r.mul(A, G), O = r.add(R, O), R = r.sub(M, O), O = r.add(M, O), O = r.mul(R, O), R = r.mul(H, R), C = r.mul(A, C), G = r.mul(w, G), H = r.sub(N, G), H = r.mul(w, H), H = r.add(H, C), C = r.add(N, N), N = r.add(C, N), N = r.add(N, G), N = r.mul(N, H), O = r.add(O, N), G = r.mul(_, B), G = r.add(G, G), N = r.mul(G, H), R = r.sub(R, N), C = r.mul(G, M), C = r.add(C, C), C = r.add(C, C), new L(R, O, C);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(w) {
      E(w);
      const { X: S, Y: A, Z: k } = this, { X: _, Y: B, Z: R } = w;
      let O = r.ZERO, C = r.ZERO, N = r.ZERO;
      const M = o.a, G = r.mul(o.b, Er);
      let H = r.mul(S, _), ee = r.mul(A, B), te = r.mul(k, R), ne = r.add(S, A), Q = r.add(_, B);
      ne = r.mul(ne, Q), Q = r.add(H, ee), ne = r.sub(ne, Q), Q = r.add(S, k);
      let ye = r.add(_, R);
      return Q = r.mul(Q, ye), ye = r.add(H, te), Q = r.sub(Q, ye), ye = r.add(A, k), O = r.add(B, R), ye = r.mul(ye, O), O = r.add(ee, te), ye = r.sub(ye, O), N = r.mul(M, Q), O = r.mul(G, te), N = r.add(O, N), O = r.sub(ee, N), N = r.add(ee, N), C = r.mul(O, N), ee = r.add(H, H), ee = r.add(ee, H), te = r.mul(M, te), Q = r.mul(G, Q), ee = r.add(ee, te), te = r.sub(H, te), te = r.mul(M, te), Q = r.add(Q, te), H = r.mul(ee, Q), C = r.add(C, H), H = r.mul(ye, Q), O = r.mul(ne, O), O = r.sub(O, H), H = r.mul(ne, ee), N = r.mul(ye, N), N = r.add(N, H), new L(O, C, N);
    }
    subtract(w) {
      return E(w), this.add(w.negate());
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
    multiply(w) {
      const { endo: S } = t;
      if (!i.isValidNot0(w))
        throw new RangeError("invalid scalar: out of range");
      let A, k;
      const _ = (B) => z.cached(this, B, (R) => Gn(L, R));
      if (S) {
        const { k1neg: B, k1: R, k2neg: O, k2: C } = $(w), { p: N, f: M } = _(R), { p: G, f: H } = _(C);
        k = M.add(H), A = T(S.beta, N, G, B, O);
      } else {
        const { p: B, f: R } = _(w);
        A = B, k = R;
      }
      return Gn(L, [A, k])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(w) {
      const { endo: S } = t, A = this, k = w;
      if (!i.isValid(k))
        throw new RangeError("invalid scalar: out of range");
      if (k === Qe || A.is0())
        return L.ZERO;
      if (k === $t)
        return A;
      if (z.hasCache(this))
        return this.multiply(k);
      if (S) {
        const { k1neg: _, k1: B, k2neg: R, k2: O } = $(k), { p1: C, p2: N } = Kh(L, A, B, O);
        return T(S.beta, C, N, _, R);
      } else
        return z.unsafe(A, k);
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * (X, Y, Z) ∋ (x=X/Z, y=Y/Z).
     * @param invertedZ - Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(w) {
      const S = this;
      let A = w;
      const { X: k, Y: _, Z: B } = S;
      if (r.eql(B, r.ONE))
        return { x: k, y: _ };
      const R = S.is0();
      A == null && (A = R ? r.ONE : r.inv(B));
      const O = r.mul(k, A), C = r.mul(_, A), N = r.mul(B, A);
      if (R)
        return { x: r.ZERO, y: r.ZERO };
      if (!r.eql(N, r.ONE))
        throw new Error("invZ was invalid");
      return { x: O, y: C };
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: w } = t;
      return s === $t ? !0 : w ? w(L, this) : z.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: w } = t;
      return s === $t ? this : w ? w(L, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return s === $t ? this.is0() : this.clearCofactor().is0();
    }
    toBytes(w = !0) {
      return Ct(w, "isCompressed"), this.assertValidity(), g(L, this, w);
    }
    toHex(w = !0) {
      return X(this.toBytes(w));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const P = i.BITS, z = new vu(L, t.endo ? Math.ceil(P / 2) : P);
  return P >= 8 && L.BASE.precompute(8), Object.freeze(L.prototype), Object.freeze(L), L;
}
function Au(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function _u(e, t) {
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
function uy(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes === void 0 ? Vo : t.randomBytes, i = Object.assign(_u(e.Fp, n), {
    seed: Math.max(wu(n.ORDER), 16)
  });
  function o(f) {
    try {
      const g = n.fromBytes(f);
      return n.isValidNot0(g);
    } catch {
      return !1;
    }
  }
  function s(f, g) {
    const { publicKey: m, publicKeyUncompressed: p } = i;
    try {
      const y = f.length;
      return g === !0 && y !== m || g === !1 && y !== p ? !1 : !!e.fromBytes(f);
    } catch {
      return !1;
    }
  }
  function a(f) {
    return f = f === void 0 ? r(i.seed) : f, gu(q(f, i.seed, "seed"), n.ORDER);
  }
  function c(f, g = !0) {
    return e.BASE.multiply(n.fromBytes(f)).toBytes(g);
  }
  function u(f) {
    const { secretKey: g, publicKey: m, publicKeyUncompressed: p } = i, y = n._lengths;
    if (!jo(f))
      return;
    const v = q(f, void 0, "key").length, x = v === m || v === p, I = v === g || !!y?.includes(v);
    if (!(x && I))
      return x;
  }
  function l(f, g, m = !0) {
    if (u(f) === !0)
      throw new Error("first arg must be private key");
    if (u(g) === !1)
      throw new Error("second arg must be public key");
    const p = n.fromBytes(f);
    return e.fromBytes(g).multiply(p).toBytes(m);
  }
  const d = {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a
  }, h = Yo(a, c);
  return Object.freeze(d), Object.freeze(i), Object.freeze({ getPublicKey: c, getSharedSecret: l, keygen: h, Point: e, utils: d, lengths: i });
}
function ly(e, t, n = {}) {
  const r = t;
  Jc(r), Sn(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  }), n = Object.assign({}, n);
  const i = n.randomBytes === void 0 ? Vo : n.randomBytes, o = n.hmac === void 0 ? (k, _) => iy(r, k, _) : n.hmac, { Fp: s, Fn: a } = e, { ORDER: c, BITS: u } = a, { keygen: l, getPublicKey: d, getSharedSecret: h, utils: f, lengths: g } = uy(e, n), m = {
    prehash: !0,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !0,
    format: "compact",
    extraEntropy: !1
  }, p = c * Su + $t < s.ORDER;
  function y(k) {
    const _ = c >> $t;
    return k > _;
  }
  function v(k, _) {
    if (!a.isValidNot0(_))
      throw new Error(`invalid signature ${k}: out of range 1..Point.Fn.ORDER`);
    return _;
  }
  function x() {
    if (p)
      throw new Error('"recovered" sig type is not supported for cofactor >2 curves');
  }
  function I(k, _) {
    go(_);
    const B = g.signature, R = _ === "compact" ? B : _ === "recovered" ? B + 1 : void 0;
    return q(k, R);
  }
  class E {
    r;
    s;
    recovery;
    constructor(_, B, R) {
      if (this.r = v("r", _), this.s = v("s", B), R != null) {
        if (x(), ![0, 1, 2, 3].includes(R))
          throw new Error("invalid recovery id");
        this.recovery = R;
      }
      Object.freeze(this);
    }
    static fromBytes(_, B = m.format) {
      I(_, B);
      let R;
      if (B === "der") {
        const { r: M, s: G } = Ue.toSig(q(_));
        return new E(M, G);
      }
      B === "recovered" && (R = _[0], B = "compact", _ = _.subarray(1));
      const O = g.signature / 2, C = _.subarray(0, O), N = _.subarray(O, O * 2);
      return new E(a.fromBytes(C), a.fromBytes(N), R);
    }
    static fromHex(_, B) {
      return this.fromBytes(gn(_), B);
    }
    assertRecovery() {
      const { recovery: _ } = this;
      if (_ == null)
        throw new Error("invalid recovery id: must be present");
      return _;
    }
    addRecoveryBit(_) {
      return new E(this.r, this.s, _);
    }
    // Unlike the top-level helper below, this method expects a digest that has
    // already been hashed to the curve's message representative.
    recoverPublicKey(_) {
      const { r: B, s: R } = this, O = this.assertRecovery(), C = O === 2 || O === 3 ? B + c : B;
      if (!s.isValid(C))
        throw new Error("invalid recovery id: sig.r+curve.n != R.x");
      const N = s.toBytes(C), M = e.fromBytes($e(Au((O & 1) === 0), N)), G = a.inv(C), H = T(q(_, void 0, "msgHash")), ee = a.create(-H * G), te = a.create(R * G), ne = e.BASE.multiplyUnsafe(ee).add(M.multiplyUnsafe(te));
      if (ne.is0())
        throw new Error("invalid recovery: point at infinify");
      return ne.assertValidity(), ne;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return y(this.s);
    }
    toBytes(_ = m.format) {
      if (go(_), _ === "der")
        return gn(Ue.hexFromSig(this));
      const { r: B, s: R } = this, O = a.toBytes(B), C = a.toBytes(R);
      return _ === "recovered" ? (x(), $e(Uint8Array.of(this.assertRecovery()), O, C)) : $e(O, C);
    }
    toHex(_) {
      return X(this.toBytes(_));
    }
  }
  Object.freeze(E.prototype), Object.freeze(E);
  const $ = n.bits2int === void 0 ? function(_) {
    if (_.length > 8192)
      throw new Error("input is too large");
    const B = lr(_), R = _.length * 8 - u;
    return R > 0 ? B >> BigInt(R) : B;
  } : n.bits2int, T = n.bits2int_modN === void 0 ? function(_) {
    return a.create($(_));
  } : n.bits2int_modN, L = Zo(u);
  function P(k) {
    return Wr("num < 2^" + u, k, Qe, L), a.toBytes(k);
  }
  function z(k, _) {
    return q(k, void 0, "message"), _ ? q(r(k), void 0, "prehashed message") : k;
  }
  function U(k, _, B) {
    const { lowS: R, prehash: O, extraEntropy: C } = qi(B, m);
    k = z(k, O);
    const N = T(k), M = a.fromBytes(_);
    if (!a.isValidNot0(M))
      throw new Error("invalid private key");
    const G = [P(M), P(N)];
    if (C != null && C !== !1) {
      const ne = C === !0 ? i(g.secretKey) : C;
      G.push(q(ne, void 0, "extraEntropy"));
    }
    const H = $e(...G), ee = N;
    function te(ne) {
      const Q = $(ne);
      if (!a.isValidNot0(Q))
        return;
      const ye = a.inv(Q), at = e.BASE.multiply(Q).toAffine(), Ut = a.create(at.x);
      if (Ut === Qe)
        return;
      const tn = a.create(ye * a.create(ee + Ut * M));
      if (tn === Qe)
        return;
      let Tn = (at.x === Ut ? 0 : 2) | Number(at.y & $t), Bn = tn;
      return R && y(tn) && (Bn = a.neg(tn), Tn ^= 1), new E(Ut, Bn, p ? void 0 : Tn);
    }
    return { seed: H, k2sig: te };
  }
  function w(k, _, B = {}) {
    const { seed: R, k2sig: O } = U(k, _, B);
    return Oh(r.outputLen, a.BYTES, o)(R, O).toBytes(B.format);
  }
  function S(k, _, B, R = {}) {
    const { lowS: O, prehash: C, format: N } = qi(R, m);
    if (B = q(B, void 0, "publicKey"), _ = z(_, C), !jo(k)) {
      const M = k instanceof E ? ", use sig.toBytes()" : "";
      throw new Error("verify expects Uint8Array signature" + M);
    }
    I(k, N);
    try {
      const M = E.fromBytes(k, N), G = e.fromBytes(B);
      if (O && M.hasHighS())
        return !1;
      const { r: H, s: ee } = M, te = T(_), ne = a.inv(ee), Q = a.create(te * ne), ye = a.create(H * ne), at = e.BASE.multiplyUnsafe(Q).add(G.multiplyUnsafe(ye));
      return at.is0() ? !1 : a.create(at.x) === H;
    } catch {
      return !1;
    }
  }
  function A(k, _, B = {}) {
    const { prehash: R } = qi(B, m);
    return _ = z(_, R), E.fromBytes(k, "recovered").recoverPublicKey(_).toBytes();
  }
  return Object.freeze({
    keygen: l,
    getPublicKey: d,
    getSharedSecret: h,
    utils: f,
    lengths: g,
    Point: e,
    sign: w,
    verify: S,
    recoverPublicKey: A,
    Signature: E,
    hash: r
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const xi = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, dy = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, fy = /* @__PURE__ */ BigInt(0), mo = /* @__PURE__ */ BigInt(2);
function py(e) {
  const t = xi.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), u = e * e * e % t, l = u * u * e % t, d = re(l, n, t) * l % t, h = re(d, n, t) * l % t, f = re(h, mo, t) * u % t, g = re(f, i, t) * f % t, m = re(g, o, t) * g % t, p = re(m, a, t) * m % t, y = re(p, c, t) * p % t, v = re(y, a, t) * m % t, x = re(v, n, t) * l % t, I = re(x, s, t) * g % t, E = re(I, r, t) * u % t, $ = re(E, mo, t);
  if (!Xr.eql(Xr.sqr($), e))
    throw new Error("Cannot find square root");
  return $;
}
const Xr = vi(xi.p, { sqrt: py }), Jt = /* @__PURE__ */ cy(xi, {
  Fp: Xr,
  endo: dy
}), Se = /* @__PURE__ */ ly(Jt, ue), Pa = {};
function Yr(e, ...t) {
  let n = Pa[e];
  if (n === void 0) {
    const r = ue(Bh(e));
    n = $e(r, r), Pa[e] = n;
  }
  return ue($e(n, ...t));
}
const ki = (e) => e.toBytes(!0).slice(1), Ei = (e) => e % mo === fy;
function Iu(e) {
  const { Fn: t, BASE: n } = Jt, r = t.fromBytes(e), i = n.multiply(r);
  return { scalar: Ei(i.y) ? r : t.neg(r), bytes: ki(i) };
}
function Pu(e) {
  const t = Xr;
  if (!t.isValidNot0(e))
    throw new Error("invalid x: Fail if x ≥ p");
  const n = t.create(e * e), r = t.create(n * e + BigInt(7));
  let i = t.sqrt(r);
  Ei(i) || (i = t.neg(i));
  const o = Jt.fromAffine({ x: e, y: i });
  return o.assertValidity(), o;
}
const pn = lr;
function Tu(...e) {
  return Jt.Fn.create(pn(Yr("BIP0340/challenge", ...e)));
}
function Ta(e) {
  return Iu(e).bytes;
}
function hy(e, t, n = Ko(32)) {
  const { Fn: r, BASE: i } = Jt, o = q(e, void 0, "message"), { bytes: s, scalar: a } = Iu(t), c = q(n, 32, "auxRand"), u = r.toBytes(a ^ pn(Yr("BIP0340/aux", c))), l = Yr("BIP0340/nonce", u, s, o), d = r.create(pn(l));
  if (d === 0n)
    throw new Error("sign failed: k is zero");
  const h = i.multiply(d), f = Ei(h.y) ? d : r.neg(d), g = ki(h), m = Tu(g, s, o), p = new Uint8Array(64);
  if (p.set(g, 0), p.set(r.toBytes(r.create(f + m * a)), 32), !Bu(p, o, s))
    throw new Error("sign: Invalid signature produced");
  return p;
}
function Bu(e, t, n) {
  const { Fp: r, Fn: i, BASE: o } = Jt, s = q(e, 64, "signature"), a = q(t, void 0, "message"), c = q(n, 32, "publicKey");
  try {
    const u = Pu(pn(c)), l = pn(s.subarray(0, 32));
    if (!r.isValidNot0(l))
      return !1;
    const d = pn(s.subarray(32, 64));
    if (!i.isValidNot0(d))
      return !1;
    const h = Tu(i.toBytes(l), ki(u), a), f = o.multiplyUnsafe(d).add(u.multiplyUnsafe(i.neg(h))), { x: g, y: m } = f.toAffine();
    return !(f.is0() || !Ei(m) || g !== l);
  } catch {
    return !1;
  }
}
const Ht = /* @__PURE__ */ (() => {
  const n = (r) => (r = r === void 0 ? Ko(48) : r, gu(r, xi.n));
  return Object.freeze({
    keygen: Yo(n, Ta),
    getPublicKey: Ta,
    sign: hy,
    verify: Bu,
    Point: Jt,
    utils: Object.freeze({
      randomSecretKey: n,
      taggedHash: Yr,
      lift_x: Pu,
      pointToBytes: ki
    }),
    lengths: Object.freeze({
      secretKey: 32,
      publicKey: 32,
      publicKeyHasPrefix: !1,
      signature: 64,
      seed: 48
    })
  });
})(), yy = /* @__PURE__ */ Uint8Array.from([
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
]), $u = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), wy = $u.map((e) => (9 * e + 5) % 16), Ou = /* @__PURE__ */ (() => {
  const n = [[$u], [wy]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => yy[o]));
  return n;
})(), Ru = Ou[0], Lu = Ou[1], Cu = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), gy = /* @__PURE__ */ Ru.map((e, t) => e.map((n) => Cu[t][n])), my = /* @__PURE__ */ Lu.map((e, t) => e.map((n) => Cu[t][n])), by = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), vy = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function Ba(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const Sr = /* @__PURE__ */ new Uint32Array(16);
class xy extends Do {
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
    for (let f = 0; f < 16; f++, n += 4)
      Sr[f] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, u = this.h3 | 0, l = u, d = this.h4 | 0, h = d;
    for (let f = 0; f < 5; f++) {
      const g = 4 - f, m = by[f], p = vy[f], y = Ru[f], v = Lu[f], x = gy[f], I = my[f];
      for (let E = 0; E < 16; E++) {
        const $ = mr(r + Ba(f, o, a, u) + Sr[y[E]] + m, x[E]) + d | 0;
        r = d, d = u, u = mr(a, 10) | 0, a = o, o = $;
      }
      for (let E = 0; E < 16; E++) {
        const $ = mr(i + Ba(g, s, c, l) + Sr[v[E]] + p, I[E]) + h | 0;
        i = h, h = l, l = mr(c, 10) | 0, c = s, s = $;
      }
    }
    this.set(this.h1 + a + l | 0, this.h2 + u + h | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    rt(Sr);
  }
  destroy() {
    this.destroyed = !0, rt(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const zu = /* @__PURE__ */ bi(() => new xy()), ky = BigInt(0), On = BigInt(1), Ey = BigInt(2), Sy = BigInt(7), Ay = BigInt(256), _y = BigInt(113), Uu = [], Nu = [], Mu = [];
for (let e = 0, t = On, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], Uu.push(2 * (5 * r + n)), Nu.push((e + 1) * (e + 2) / 2 % 64);
  let i = ky;
  for (let o = 0; o < 7; o++)
    t = (t << On ^ (t >> Sy) * _y) % Ay, t & Ey && (i ^= On << (On << BigInt(o)) - On);
  Mu.push(i);
}
const Hu = tu(Mu, !0), Iy = Hu[0], Py = Hu[1], $a = (e, t, n) => n > 32 ? ph(e, t, n) : dh(e, t, n), Oa = (e, t, n) => n > 32 ? hh(e, t, n) : fh(e, t, n);
function Ty(e, t = 24) {
  if (tt(t, "rounds"), t < 1 || t > 24)
    throw new Error('"rounds" expected integer 1..24');
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, u = n[c], l = n[c + 1], d = $a(u, l, 1) ^ n[a], h = Oa(u, l, 1) ^ n[a + 1];
      for (let f = 0; f < 50; f += 10)
        e[s + f] ^= d, e[s + f + 1] ^= h;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = Nu[s], c = $a(i, o, a), u = Oa(i, o, a), l = Uu[s];
      i = e[l], o = e[l + 1], e[l] = c, e[l + 1] = u;
    }
    for (let s = 0; s < 50; s += 10) {
      const a = e[s], c = e[s + 1], u = e[s + 2], l = e[s + 3];
      e[s] ^= ~e[s + 2] & e[s + 4], e[s + 1] ^= ~e[s + 3] & e[s + 5], e[s + 2] ^= ~e[s + 4] & e[s + 6], e[s + 3] ^= ~e[s + 5] & e[s + 7], e[s + 4] ^= ~e[s + 6] & e[s + 8], e[s + 5] ^= ~e[s + 7] & e[s + 9], e[s + 6] ^= ~e[s + 8] & a, e[s + 7] ^= ~e[s + 9] & c, e[s + 8] ^= ~a & u, e[s + 9] ^= ~c & l;
    }
    e[0] ^= Iy[r], e[1] ^= Py[r];
  }
  rt(n);
}
let By = class Fu {
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
    if (this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.canXOF = i, this.rounds = o, tt(r, "outputLen"), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = nh(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    da(this.state32), Ty(this.state32, this.rounds), da(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    wn(this), nt(t);
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
    wn(this, !1), nt(t), this.finish();
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
    return tt(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (Mo(t, this), this.finished)
      throw new Error("digest() was already called");
    this.writeInto(t.subarray(0, this.outputLen)), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.outputLen);
    return this.digestInto(t), t;
  }
  destroy() {
    this.destroyed = !0, rt(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t ||= new Fu(n, r, i, s, o), t.blockLen = n, t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.canXOF = this.canXOF, t.destroyed = this.destroyed, t;
  }
};
const $y = (e, t, n, r = {}) => bi(() => new By(t, e, n), r), Ku = /* @__PURE__ */ $y(1, 136, 32), Oy = 1024 * 1024, Du = Object.getPrototypeOf(Uint8Array.prototype), Ry = Object.getOwnPropertyDescriptor(Du, Symbol.toStringTag).get, Ly = Object.getOwnPropertyDescriptor(Du, "byteLength").get, Cy = Uint8Array.prototype.set;
function ju(e) {
  if (!Number.isSafeInteger(e) || e < 0) throw new Error("exact message byte length is invalid");
  if (e > Oy) throw new Error("exact message exceeds the 1 MiB decoded limit");
}
function Vu(e) {
  const t = e.startsWith("0x") ? 2 : 0;
  ju(Math.ceil((e.length - t) / 2));
}
function Jo(e) {
  if (typeof e == "string")
    return Vu(e), gn(e.startsWith("0x") ? e.slice(2) : e);
  if (Ry.call(e) !== "Uint8Array") throw new TypeError("exact message must be hex or Uint8Array");
  const t = Ly.call(e);
  ju(t);
  const n = new Uint8Array(t);
  return Cy.call(n, e), n;
}
const zy = "hpke-x25519-hkdfsha256-aes256gcm-v1", Uy = "avault:blind-box:v1", Ny = "avault:blind-box:aad:v1", Jr = "machine-aesgcm-v1", Si = 1, Ai = "ecdsa-secp256k1-recoverable", Qn = "ed25519", Qo = "ecdsa-secp256k1-der", es = "schnorr-secp256k1-bip340", er = "schnorr-secp256k1-bip341-keypath", se = 32, Qr = 12, _i = 32, My = "avault:protected-vmk:kek-passkey:v1", Hy = /^[A-Za-z_][A-Za-z0-9_]*$/, Fy = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[a-z][a-z0-9_]{0,63})?$/, Ky = "avault:protected-root-meta:v1", Dy = new TextEncoder(), jy = new TextDecoder();
function ts(e) {
  switch (e) {
    case Qn:
    case Ai:
    case Qo:
    case es:
    case er:
      return e;
    default:
      throw new Error("unsupported signing scheme");
  }
}
function An(e) {
  if (e === void 0 || e === "secp256k1") return "secp256k1";
  if (e === "ed25519") return e;
  throw new Error("unsupported signing curve");
}
function ns(e) {
  if (ts(e), e === Qn) throw new Error("Ed25519 requires exact-message signing, not a digest");
}
function ae(e, t = "bytes") {
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
function Te(e) {
  const t = ae(e), n = new ArrayBuffer(t.byteLength);
  return new Uint8Array(n).set(t), t.fill(0), n;
}
function xe(e) {
  return Dy.encode(e);
}
function ei(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(ei).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${ei(t[n])}`).join(",")}}`;
}
function Zt(e) {
  const t = new Uint8Array(e);
  return crypto.getRandomValues(t), t;
}
function J(e) {
  const t = ae(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return t.fill(0), btoa(n);
}
function fe(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = t.padEnd(Math.ceil(t.length / 4) * 4, "=");
  return Uint8Array.from(atob(n), (r) => r.charCodeAt(0));
}
function Oe(e) {
  return gn(e.startsWith("0x") ? e.slice(2) : e);
}
function Ra(e) {
  return /^(0x)?[0-9a-fA-F]+$/.test(e) && (e.startsWith("0x"), e.length % 2 === 0) ? Oe(e) : fe(e);
}
function Vy(e) {
  return X(ae(e));
}
function qu(e, t = "bytes") {
  return ae(e, t);
}
function he(e, t, n) {
  if (e.length !== t)
    throw new Error(`${n} must be ${t} bytes`);
}
function He(e) {
  e && new Uint8Array(e).fill(0);
}
async function ti(e, t, n, r) {
  let i, o, s, a;
  try {
    i = Te(e), o = Te(t), s = Te(n), a = r ? Te(r) : void 0;
    const c = await crypto.subtle.importKey("raw", i, "AES-GCM", !1, ["encrypt"]), u = await crypto.subtle.encrypt(
      {
        name: "AES-GCM",
        iv: o,
        ...a ? { additionalData: a } : {}
      },
      c,
      s
    );
    return new Uint8Array(u);
  } finally {
    He(i), He(o), He(s), He(a);
  }
}
async function Ii(e, t, n, r) {
  let i, o, s, a;
  try {
    i = Te(e), o = Te(t), s = Te(n), a = r ? Te(r) : void 0;
    const c = await crypto.subtle.importKey("raw", i, "AES-GCM", !1, ["decrypt"]), u = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: o,
        ...a ? { additionalData: a } : {}
      },
      c,
      s
    );
    return new Uint8Array(u);
  } finally {
    He(i), He(o), He(s), He(a);
  }
}
async function qy(e, t, n, r) {
  let i, o, s;
  try {
    i = Te(e), o = Te(t), s = Te(n);
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
    He(i), He(o), He(s);
  }
}
async function Gu(e, t) {
  const n = ae(e, "passkey PRF output");
  try {
    const r = ae(t, "passkey PRF salt");
    try {
      if (n.length === 0)
        throw new Error("passkey PRF output is empty");
      return he(r, _i, "passkey PRF salt"), await qy(n, r, xe(My), se);
    } finally {
      r.fill(0);
    }
  } finally {
    n.fill(0);
  }
}
function Gy() {
  return Zt(se);
}
function Wy() {
  return Zt(_i);
}
function Zy(e) {
  const t = ae(e, "passkey PRF salt");
  try {
    return he(t, _i, "passkey PRF salt"), { prf: { eval: { first: Te(t) } } };
  } finally {
    t.fill(0);
  }
}
function ot(e) {
  const t = typeof e == "string" ? JSON.parse(e) : e;
  if (t?.v !== Si || !Array.isArray(t.copies))
    throw new Error("invalid protected wrap_meta");
  return t;
}
async function Xy(e, t) {
  const n = ae(e, "VMK"), r = Zt(Qr);
  let i, o;
  try {
    he(n, se, "VMK"), o = ae(t.prfSalt, "passkey PRF salt"), he(o, _i, "passkey PRF salt"), i = await Gu(t.prfOutput, o);
    const s = await ti(i, r, n);
    return {
      kind: "passkey",
      kdf: "webauthn-prf-hkdf-sha256",
      prf_salt: J(o),
      nonce: J(r),
      wrapped: J(s),
      ...t.credentialId ? { credential_id: t.credentialId } : {}
    };
  } finally {
    i?.fill(0), o?.fill(0), n.fill(0);
  }
}
async function Yy(e, t) {
  const n = ae(e, "VMK");
  try {
    if (he(n, se, "VMK"), t.length === 0)
      throw new Error("at least one protected unlock factor is required");
    const r = [];
    for (const i of t)
      r.push(await Xy(n, i));
    return JSON.stringify({ v: Si, copies: r });
  } finally {
    n.fill(0);
  }
}
async function Jy(e, t) {
  const n = fe(e.prf_salt);
  if (t.prfSalt && J(t.prfSalt) !== e.prf_salt)
    throw n.fill(0), new Error("passkey PRF salt does not match copy");
  const r = await Gu(t.prfOutput, n);
  try {
    return await Ii(r, fe(e.nonce), fe(e.wrapped));
  } finally {
    r.fill(0), n.fill(0);
  }
}
async function Qy(e, t) {
  const n = ot(e);
  for (const r of n.copies)
    try {
      if (r.kind === "passkey") {
        const i = await Jy(r, t);
        try {
          return he(i, se, "VMK"), i;
        } catch (o) {
          throw i.fill(0), o;
        }
      }
    } catch {
    }
  throw new Error("no protected VMK copy could be unwrapped");
}
function rs(e) {
  return ot(e).copies.filter((t) => t.kind === "passkey").map((t) => ({
    prfSalt: fe(t.prf_salt),
    ...t.credential_id ? { credentialId: t.credential_id } : {}
  }));
}
function ew(e, t) {
  const n = ot(e);
  return JSON.stringify({
    ...n,
    rp_id: t.rpId,
    vault_user_handle: t.vaultUserHandle
  });
}
function Wu(e) {
  const t = ot(e);
  return delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, JSON.stringify(t);
}
function is(e) {
  if (!e.kind) {
    if (e.curve !== void 0) throw new Error("protected record curve requires keypair metadata");
    return;
  }
  const t = An(e.curve);
  if (e.kind === "static") {
    if (e.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (e.kind === "keypair") {
    if (typeof e.publicKey != "string" || e.publicKey.length === 0)
      throw new Error("keypair protected record requires a public key");
    return t === "ed25519" && ds(e.publicKey), {
      kind: "keypair",
      public_key: e.publicKey.toLowerCase(),
      ...t === "ed25519" ? { curve: t } : {}
    };
  }
  throw new Error("unsupported protected record kind");
}
function tw(e) {
  if (e === void 0) return;
  if (typeof e != "object" || e === null)
    throw new Error("protected record metadata is invalid");
  const t = e;
  if (t.curve !== void 0 && t.curve !== "ed25519")
    throw new Error("protected record metadata curve is invalid");
  if (t.kind === "static") {
    if (t.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (t.kind === "keypair")
    return is({ kind: "keypair", publicKey: t.public_key, curve: t.curve });
  throw new Error("protected record metadata kind is invalid");
}
function os(e, t) {
  return {
    name: e,
    ...t ? { kind: t.kind } : {},
    ...t?.public_key ? { publicKey: t.public_key } : {},
    ...t?.curve ? { curve: t.curve } : {}
  };
}
function ss(e) {
  return xe(
    ei({
      domain: "avault:protected-record:aad:v2",
      name: e.kind === "keypair" ? el(e.name) : xn(e.name),
      scheme: e.scheme ?? Jr,
      version: e.version ?? Si,
      record: is(e) ?? null
    })
  );
}
async function Zu(e, t, n) {
  const r = ae(t, "VMK"), i = Zt(se);
  let o;
  try {
    he(r, se, "VMK"), o = ae(e, "value");
    const s = ss(n), a = Zt(Qr), c = await ti(i, a, o, s), u = Zt(Qr), l = await ti(r, u, i, s);
    return {
      ciphertext: J(c),
      nonce: J(a),
      dek_nonce: J(u),
      wrapped_dek: J(l)
    };
  } finally {
    i.fill(0), r.fill(0), o?.fill(0);
  }
}
async function Xu(e, t, n) {
  const r = ae(t, "VMK");
  try {
    he(r, se, "VMK");
    const i = await Ii(
      r,
      fe(e.dek_nonce),
      fe(e.wrapped_dek),
      ss(n)
    );
    try {
      return he(i, se, "DEK"), i;
    } catch (o) {
      throw i.fill(0), o;
    }
  } finally {
    r.fill(0);
  }
}
async function as(e, t, n) {
  const r = await Xu(e, t, n);
  try {
    return await Ii(
      r,
      fe(e.nonce),
      fe(e.ciphertext),
      ss(n)
    );
  } finally {
    r.fill(0);
  }
}
function Yu(e, t, n) {
  const r = ot(t);
  if ("dek_nonce" in r || "wrapped_dek" in r)
    throw new Error("vmk wrap_meta must not already carry a wrapped DEK");
  if ("scheme" in r && r.scheme !== Jr)
    throw new Error("protected wrap_meta has unsupported scheme");
  const i = n ? is(n) : void 0;
  return {
    ciphertext: e.ciphertext,
    nonce: e.nonce,
    wrap_meta: JSON.stringify({
      ...r,
      scheme: Jr,
      ...i ? { record_meta: i } : {},
      dek_nonce: e.dek_nonce,
      wrapped_dek: e.wrapped_dek
    })
  };
}
function vn(e) {
  const t = ot(e.wrap_meta), { dek_nonce: n, wrapped_dek: r, record_meta: i, ...o } = t;
  if (typeof n != "string" || typeof r != "string")
    throw new Error("protected record wrap_meta is missing the wrapped DEK");
  const s = tw(i);
  return {
    sealed: { ciphertext: e.ciphertext, nonce: e.nonce, dek_nonce: n, wrapped_dek: r },
    vmkWrapMeta: JSON.stringify(o),
    ...s ? { recordMetadata: s } : {}
  };
}
function Ju(e) {
  const t = { ...ot(e) };
  return delete t.root_meta, delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, delete t.scheme, xe(ei({ domain: Ky, vault: t }));
}
async function nw(e, t, n) {
  const r = Zt(Qr), i = xe(JSON.stringify(t));
  try {
    const o = await ti(e, r, i, Ju(n));
    return { v: 1, nonce: J(r), ciphertext: J(o) };
  } finally {
    i.fill(0);
  }
}
async function cs(e, t) {
  const n = ot(e).root_meta;
  if (!n) return null;
  if (n.v !== 1 || typeof n.nonce != "string" || typeof n.ciphertext != "string")
    throw new Error("invalid protected root metadata");
  const r = await Ii(t, fe(n.nonce), fe(n.ciphertext), Ju(e));
  try {
    const i = JSON.parse(jy.decode(r));
    if (typeof i != "object" || i === null) throw new Error("invalid protected root metadata");
    return i;
  } finally {
    r.fill(0);
  }
}
function rw(e, t) {
  const n = ot(e);
  return JSON.stringify({ ...n, root_meta: t });
}
function iw() {
  return new Vp({
    kem: new th(),
    kdf: new qp(),
    aead: new Ip()
  });
}
function us(e) {
  return fe(typeof e == "string" ? e : e.public_key);
}
async function Qu(e) {
  const t = await crypto.subtle.digest("SHA-256", Te(e));
  return X(new Uint8Array(t));
}
async function ow(e) {
  return Qu(us(e));
}
function sw(e) {
  if (!e.fingerprint)
    throw new Error("protected DEK release requires a pinned avault public key fingerprint");
  return e;
}
async function aw(e) {
  const t = sw(e), n = us(t);
  if (he(n, se, "avault public key"), await Qu(n) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  return t;
}
async function cw(e, t, n) {
  const r = iw(), i = us(t);
  if (he(i, se, "avault public key"), typeof t != "string" && t.fingerprint && await ow(t) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  const o = await r.kem.deserializePublicKey(i), s = typeof e == "string" ? xe(e) : ae(e, "plaintext");
  try {
    const a = await r.seal(
      { recipientPublicKey: o, info: xe(Uy) },
      s,
      ww(n)
    );
    return {
      scheme: zy,
      enc: J(new Uint8Array(a.enc)),
      ct: J(new Uint8Array(a.ct))
    };
  } finally {
    s.fill(0);
  }
}
function el(e) {
  if (typeof e != "string" || !Hy.test(e))
    throw new Error("vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$");
  return e;
}
function xn(e) {
  if (typeof e != "string" || !Fy.test(e))
    throw new Error("vault record name must match ^[A-Za-z_][A-Za-z0-9_]*(\\.[a-z][a-z0-9_]{0,63})?$");
  return e;
}
function La(e, t) {
  if (typeof e != "string" || e.trim() === "")
    throw new Error(`${t} is required`);
  return e;
}
function ls(e) {
  const t = typeof e == "string" ? Oe(e) : ae(e, "digest");
  return he(t, se, "digest"), t;
}
function uw(e) {
  const t = typeof e == "string" ? Oe(e) : ae(e, "private key");
  try {
    if (he(t, se, "private key"), !Se.utils.isValidSecretKey(t))
      throw new Error("invalid secp256k1 private key");
    return t;
  } catch (n) {
    throw t.fill(0), n;
  }
}
function ni(e, t) {
  return typeof e == "string" ? Oe(e) : ae(e, t);
}
function ri(e) {
  const t = ni(e, "operation hash");
  return he(t, se, "operation hash"), t;
}
function ii(e, t) {
  if (typeof e == "number" && (!Number.isSafeInteger(e) || e < 0))
    throw new Error(`${t} out of bounds`);
  const n = typeof e == "bigint" ? e : BigInt(e);
  if (n < 0n || n > 0xffffffffffffffffn)
    throw new Error(`${t} out of bounds`);
  return n;
}
function tl(e, t) {
  const n = ii(e, t), r = new Uint8Array(8);
  return new DataView(r.buffer).setBigUint64(0, n, !1), r;
}
function lw(e) {
  return tl(e, "approval expiry");
}
function Ca(e) {
  const t = ae(e.nonce, "approval nonce");
  if (t.length < 16 || t.length > 128)
    throw new Error("approval nonce must be 16..128 bytes");
  return { approvalNonce: t, approvalExpiresAtUnix: ii(e.expiresAtUnix, "approval expiry") };
}
function Be(e, t) {
  const n = ae(t);
  if (n.length > 4294967295)
    throw new Error("blind-box AAD field is too large");
  e.push(n.length >>> 24 & 255, n.length >>> 16 & 255, n.length >>> 8 & 255, n.length & 255), e.push(...n);
}
function dw(e) {
  switch (e.purpose) {
    case "seal":
      return fw(e.name);
    case "agent-deliver": {
      const t = Ca({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-deliver",
        name: xn(e.name),
        grantId: La(e.grantId, "grant id"),
        ttlSecs: ii(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: ri(e.operationHash)
      };
    }
    case "agent-sign": {
      ns(e.signScheme);
      const t = Ca({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-sign",
        name: el(e.name),
        grantId: La(e.grantId, "grant id"),
        signScheme: e.signScheme,
        digest: ls(e.digest),
        ttlSecs: ii(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: ri(e.operationHash)
      };
    }
    default:
      throw new Error("unsupported blind-box context purpose");
  }
}
function fw(e) {
  return { purpose: "seal", name: xn(e) };
}
async function pw(e) {
  const t = ri(e.operationHash), n = await yw(e.name, e.ttlSecs);
  if (X(t) !== X(n))
    throw new Error("operation hash does not match release operation fields");
}
async function hw(e) {
  const t = [];
  for (const n of e)
    Be(t, typeof n == "string" ? xe(n) : ae(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(t)));
}
async function yw(e, t) {
  return hw(["agent-deliver", xn(e), tl(t, "ttl seconds")]);
}
function ww(e) {
  const t = dw(e), n = [...xe(Ny)];
  return Be(n, xe(t.purpose)), Be(n, xe(t.name)), Be(n, xe(Jr)), Be(n, new Uint8Array([Si])), Be(n, t.purpose === "agent-deliver" || t.purpose === "agent-sign" ? xe(t.grantId) : new Uint8Array()), Be(n, new Uint8Array()), Be(n, t.purpose === "agent-sign" ? xe(t.signScheme) : new Uint8Array()), Be(n, t.purpose === "agent-sign" ? ls(t.digest) : new Uint8Array()), Be(n, t.purpose === "seal" ? new Uint8Array() : ae(t.approvalNonce, "approval nonce")), Be(n, t.purpose === "seal" ? new Uint8Array() : lw(t.approvalExpiresAtUnix)), Be(n, t.purpose === "seal" ? new Uint8Array() : ri(t.operationHash)), new Uint8Array(n);
}
async function gw(e, t, n, r, i) {
  if (r.kind !== void 0 && r.kind !== "static")
    throw new Error("protected DEK release requires a static protected record");
  if (xn(r.name) !== xn(i.name))
    throw new Error("protected DEK release context name does not match record name");
  await pw(i);
  const o = await aw(n), s = await Xu(e, t, r);
  try {
    return await cw(s, o, i);
  } finally {
    s.fill(0);
  }
}
function mw(e = "secp256k1") {
  if (An(e) === "ed25519") {
    const n = Ot.utils.randomSecretKey();
    try {
      return { privateKey: n, publicKey: X(Ot.getPublicKey(n)) };
    } catch (r) {
      throw n.fill(0), r;
    }
  }
  const t = Se.utils.randomSecretKey();
  return { privateKey: t, publicKey: X(Se.getPublicKey(t, !0)) };
}
function za(e) {
  return Oe(e.toString(16).padStart(se * 2, "0"));
}
function bw(e, t, n, r = {}) {
  if (ns(n), !r || typeof r != "object" || Array.isArray(r) || Object.keys(r).some((s) => s !== "schnorrAuxRand"))
    throw new Error("unsupported signing option; script trees are not supported");
  const i = uw(e);
  let o;
  try {
    const s = ls(t);
    if (n === Ai) {
      const a = Se.sign(s, i, { prehash: !1, lowS: !0, format: "recovered" });
      return {
        scheme: n,
        signature: X(a.slice(1)),
        recovery_id: a[0] ?? null
      };
    }
    if (n === Qo)
      return {
        scheme: n,
        signature: X(Se.sign(s, i, { prehash: !1, lowS: !0, format: "der" })),
        recovery_id: null
      };
    if (n === es) {
      o = r.schnorrAuxRand !== void 0 ? ni(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(se)), he(o, se, "schnorr aux randomness");
      const a = Ht.sign(s, i, o);
      if (!Ht.verify(a, s, Ht.getPublicKey(i))) throw new Error("signing self-verification failed");
      return { scheme: n, signature: X(a), recovery_id: null };
    }
    if (n === er) {
      const a = Se.getPublicKey(i, !0), c = fs(a.slice(1));
      let u = BigInt(`0x${X(i)}`);
      a[0] === 3 && (u = Kn - u);
      const l = BigInt(`0x${X(Ht.utils.taggedHash("TapTweak", a.slice(1)))}`);
      if (l >= Kn) throw new Error("invalid taproot tweak");
      let d = (u + l) % Kn;
      if (d === 0n) throw new Error("invalid taproot signing key");
      let h = za(d);
      try {
        c.toBytes(!0)[0] === 3 && (d = Kn - d, h.fill(0), h = za(d)), o = r.schnorrAuxRand !== void 0 ? ni(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(se)), he(o, se, "schnorr aux randomness");
        const f = Ht.sign(s, h, o);
        if (!Ht.verify(f, s, c.toBytes(!0).slice(1))) throw new Error("Taproot signing self-verification failed");
        return { scheme: n, signature: X(f), recovery_id: null };
      } finally {
        h.fill(0);
      }
    }
    throw new Error("unsupported signing scheme");
  } finally {
    o?.fill(0), i.fill(0);
  }
}
async function vw(e, t, n, r, i, o = {}) {
  if (n.kind !== "keypair" || !n.publicKey)
    throw new Error("protected signing requires a keypair protected record");
  if (ns(i), An(n.curve) !== "secp256k1") throw new Error("digest signing requires a secp256k1 protected record");
  const s = await as(e, t, n);
  try {
    if (X(Se.getPublicKey(s, !0)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return bw(s, r, i, o);
  } finally {
    s.fill(0);
  }
}
function xw(e, t) {
  const n = Jo(t), r = ni(e, "Ed25519 seed");
  try {
    he(r, se, "Ed25519 seed");
    const i = Ot.sign(n, r);
    if (!Ot.verify(i, n, Ot.getPublicKey(r), { zip215: !1 }))
      throw new Error("Ed25519 signing self-verification failed");
    return { scheme: Qn, signature: X(i), recovery_id: null };
  } finally {
    r.fill(0);
  }
}
function ds(e) {
  if (!/^[0-9a-fA-F]{64}$/.test(e)) throw new Error("expected a 32-byte Ed25519 public key");
  const t = Oe(e);
  if (!Ot.utils.isValidPublicKey(t, !1)) throw new Error("invalid Ed25519 public key");
  return t;
}
async function kw(e, t, n, r) {
  if (n.kind !== "keypair" || n.curve !== "ed25519" || !n.publicKey)
    throw new Error("exact-message signing requires an Ed25519 keypair protected record");
  const i = Jo(r);
  if (i.length === 0) throw new Error("protected Ed25519 message must not be empty");
  ds(n.publicKey);
  const o = await as(e, t, n);
  try {
    if (he(o, se, "Ed25519 seed"), X(Ot.getPublicKey(o)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return xw(o, i);
  } finally {
    o.fill(0);
  }
}
function Ew(e) {
  return rl(ds(e));
}
function Sw(e) {
  const t = e.rootMetadata?.daemon?.verificationKeys?.find(
    (n) => n.alg === "ed25519" && n.keyId === e.keyId
  );
  if (!t)
    throw new Error("daemon verification key is not pinned");
  return Ot.verify(Ra(e.signature), xe(e.message), Ra(t.publicKey));
}
function nl(e) {
  const t = Oe(e.trim());
  if (t.length === 33 && (t[0] === 2 || t[0] === 3) && Se.utils.isValidPublicKey(t))
    return t;
  if (t.length === 65 && t[0] === 4 && Se.utils.isValidPublicKey(t))
    return Se.Point.fromBytes(t).toBytes(!0);
  throw new Error("expected a valid secp256k1 public key");
}
const Aw = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", _w = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", Iw = 1, Pw = 734539939, Kn = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
function Tw(e) {
  return zu(ue(e));
}
function Bw(e) {
  const t = ue(ue(e)).slice(0, 4), n = new Uint8Array(e.length + t.length);
  return n.set(e), n.set(t, e.length), rl(n);
}
function rl(e) {
  let t = BigInt(`0x${X(e)}`), n = "";
  for (; t > 0n; ) {
    const i = Number(t % 58n);
    t /= 58n, n = Aw[i] + n;
  }
  let r = 0;
  for (const i of e) {
    if (i !== 0) break;
    r += 1;
  }
  return "1".repeat(r) + n;
}
function $w(e) {
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
function Ow(e) {
  return [...e].map((t) => t.charCodeAt(0) >>> 5).concat([0], [...e].map((t) => t.charCodeAt(0) & 31));
}
function Rw(e, t, n) {
  const r = Ow(e).concat(t), i = $w(r.concat([0, 0, 0, 0, 0, 0])) ^ n;
  return [0, 1, 2, 3, 4, 5].map((o) => i >>> 5 * (5 - o) & 31);
}
function Lw(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >>> i & s);
  return i && o.push(r << n - i & s), o;
}
function Ua(e, t, n) {
  const r = t === 0 ? Iw : Pw, i = [t].concat(Lw(n, 8, 5));
  return `${e}1${i.concat(Rw(e, i, r)).map((o) => _w[o]).join("")}`;
}
function Cw(e) {
  const t = e.toLowerCase(), n = X(Ku(xe(t)));
  return [...t].map((r, i) => /[a-f]/.test(r) && Number.parseInt(n[i], 16) >= 8 ? r.toUpperCase() : r).join("");
}
function fs(e) {
  const t = BigInt(`0x${X(Ht.utils.taggedHash("TapTweak", e))}`);
  if (t >= Kn) throw new Error("invalid taproot tweak");
  const n = Se.Point.fromBytes(new Uint8Array([2, ...e])), r = t === 0n ? n : n.add(Se.Point.BASE.multiply(t));
  if (r.equals(Se.Point.ZERO)) throw new Error("invalid taproot output key");
  return r;
}
function zw(e) {
  return fs(nl(e).slice(1)).toBytes(!0).slice(1);
}
function ps(e) {
  const t = nl(e), n = Se.Point.fromBytes(t), r = n.toBytes(!1).slice(1), i = Ku(r), o = Tw(t), s = new Uint8Array(21);
  s[0] = 0, s.set(o, 1);
  const a = n.toBytes(!1).slice(1, 33), c = fs(a);
  return {
    eth: `0x${Cw(X(i.slice(-20)))}`,
    btc_legacy: Bw(s),
    btc_segwit: Ua("bc", 0, o),
    btc_taproot: Ua("bc", 1, c.toBytes(!1).slice(1, 33))
  };
}
const hs = {
  windowSeconds: 600,
  strictApprovals: !1,
  parentValueSealAllowed: !0
};
let bo = { ...hs };
function Uw(e) {
  return e === 300 || e === 600 || e === 1800 ? e : hs.windowSeconds;
}
function Nw(e, t) {
  if (e === 300 || e === 600 || e === 1800) return e;
  throw new b("invalid_payload", `${t}.windowSeconds must be 300, 600, or 1800`);
}
function ys(e, t = "policy") {
  if (typeof e != "object" || e === null) throw new b("invalid_payload", `${t} must be an object`);
  const n = e;
  if (typeof n.strictApprovals != "boolean") throw new b("invalid_payload", `${t}.strictApprovals must be a boolean`);
  if (typeof n.parentValueSealAllowed != "boolean")
    throw new b("invalid_payload", `${t}.parentValueSealAllowed must be a boolean`);
  return {
    windowSeconds: Nw(n.windowSeconds, t),
    strictApprovals: n.strictApprovals,
    parentValueSealAllowed: n.parentValueSealAllowed
  };
}
function Mw(e) {
  if (typeof e != "object" || e === null) return { ...hs };
  const t = e;
  return {
    windowSeconds: Uw(t.windowSeconds),
    strictApprovals: t.strictApprovals === !0,
    parentValueSealAllowed: t.parentValueSealAllowed !== !1
  };
}
function ws(e) {
  return bo = Mw(e), bo;
}
function Ee() {
  return { ...bo };
}
const Hw = "avibe-vault-lock:v1:", j = {
  vmk: null,
  wrapMeta: null,
  freshSetup: !1,
  scopeId: null
};
let wt = null, Zn = null, tr = null, vo = null, gs = 0;
const oi = /* @__PURE__ */ new Set();
let il = null;
function Fw(e) {
  il = e;
}
function Na() {
  return j.vmk ? "unlocked" : j.wrapMeta ? "locked" : "needs-setup";
}
function ol(e) {
  il?.({
    state: Na(),
    ...wt !== null && Na() === "unlocked" ? { expiresAt: wt } : {},
    reason: e
  });
}
function ms() {
  Zn !== null && (clearTimeout(Zn), Zn = null);
}
function si() {
  tr?.close(), tr = null, vo = null;
}
function sl(e) {
  const t = new TextEncoder().encode(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return btoa(n).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function Kw(e) {
  let t = 2166136261;
  for (let n = 0; n < e.length; n += 1)
    t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
  return t.toString(16).padStart(8, "0");
}
function al(e) {
  const t = ot(e);
  return typeof t.vault_user_handle == "string" && t.vault_user_handle.length > 0 ? sl(t.vault_user_handle) : `wrap-${Kw(e)}`;
}
function Dw(e) {
  return sl(e);
}
function cl(e) {
  if (typeof window > "u" || typeof BroadcastChannel > "u" || !e) {
    si();
    return;
  }
  const t = `${Hw}${e}`;
  vo !== t && (si(), vo = t, tr = new BroadcastChannel(t), tr.onmessage = (n) => {
    const r = n.data;
    r?.type === "lock" && r.scopeId === e && Ke({ broadcast: !1, reason: "manual-lock" });
  });
}
function jw() {
  j.scopeId && (cl(j.scopeId), tr?.postMessage({ type: "lock", scopeId: j.scopeId }));
}
function ul() {
  return new Error("vault-operation-aborted");
}
function ll() {
  gs += 1;
  for (const e of oi)
    e.abort(ul());
  oi.clear();
}
function Vw() {
  j.vmk?.fill(0), j.vmk = null, wt = null, ms(), si(), j.freshSetup && (j.wrapMeta = null, j.freshSetup = !1, j.scopeId = null);
}
function qw() {
  return wt !== null && Date.now() >= wt;
}
function dl() {
  return j.vmk && qw() ? (Ke({ broadcast: !1, reason: "auto-lock" }), !1) : j.vmk !== null;
}
function fl(e, t = Ee()) {
  if (!j.vmk)
    throw new Error("vault VMK is not loaded");
  const n = t.windowSeconds * 1e3;
  return wt = Date.now() + n, ms(), Zn = setTimeout(() => {
    Ke({ broadcast: !1, reason: "auto-lock" });
  }, n), ol(e), wt;
}
function Gw() {
  const e = wt;
  if (!j.vmk || e === null) return () => {
  };
  ms();
  let t = !1;
  return () => {
    if (t || !j.vmk) return;
    t = !0;
    const n = e - Date.now();
    if (n <= 0) {
      Ke({ broadcast: !1, reason: "auto-lock" });
      return;
    }
    Zn = setTimeout(() => {
      Ke({ broadcast: !1, reason: "auto-lock" });
    }, n);
  };
}
function Ww(e) {
  if (e instanceof b && e.retryable) return !0;
  const t = e instanceof Error ? e.message : String(e);
  return t === "operation-cancelled" || t === "operation-superseded" || t === "passkey-cancelled" || t === "vault-operation-aborted";
}
function Zw(e) {
  if (e.signal.aborted || e.epoch !== gs || !j.vmk)
    throw ul();
}
async function bs(e, t = {}) {
  if (!dl() || !j.vmk || !j.wrapMeta)
    throw new Error("vault-locked");
  const n = new Uint8Array(j.vmk), r = j.wrapMeta, i = gs, o = new AbortController();
  oi.add(o);
  const s = {
    epoch: i,
    signal: o.signal,
    assertCurrent: () => Zw(s)
  };
  let a = null;
  try {
    s.assertCurrent();
    const c = await e(n, r, s);
    return s.assertCurrent(), t.renewOnSuccess !== !1 && fl("renew"), s.assertCurrent(), t.beforeSuccess && (a = Gw(), await t.beforeSuccess(), s.assertCurrent(), a(), a = null), c;
  } catch (c) {
    throw Ww(c) || Ke({ broadcast: !0, reason: "manual-lock" }), c;
  } finally {
    a?.(), oi.delete(o), n.fill(0);
  }
}
function Xw() {
  return j.vmk !== null && j.freshSetup;
}
function pl(e) {
  return ll(), j.vmk?.fill(0), j.vmk = e.vmk, j.wrapMeta = e.wrapMeta, j.freshSetup = e.freshSetup, j.scopeId = e.scopeId, cl(e.scopeId), { state: "unlocked", expiresAt: fl("unlock", e.policy) };
}
function Ke(e = {}) {
  return e.broadcast && jw(), ll(), Vw(), ol(e.reason ?? "manual-lock"), { state: "locked" };
}
function gt(e) {
  return dl(), j.vmk ? {
    state: "unlocked",
    expiresAt: wt ?? void 0,
    ...j.freshSetup ? { freshSetup: !0 } : {}
  } : (e && (j.wrapMeta = Wu(e), j.scopeId = al(j.wrapMeta)), j.wrapMeta ? { state: "locked" } : { state: "needs-setup" });
}
function vs(e) {
  const t = Wu(e), n = al(t);
  return j.wrapMeta = t, j.scopeId = n, { wrapMeta: t, scopeId: n };
}
function Yw() {
  Ke({ broadcast: !1, reason: "unload" }), si();
}
const Ar = "Avibe Vault", Ma = "avibe-vault";
function Xt(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Rt(e) {
  const t = new ArrayBuffer(e.byteLength);
  return new Uint8Array(t).set(e), t;
}
function hl() {
  return Rt(crypto.getRandomValues(new Uint8Array(32)));
}
function yl(e) {
  try {
    return fe(e);
  } catch {
    return null;
  }
}
function Pi(e, t) {
  if (e instanceof ArrayBuffer || ArrayBuffer.isView(e))
    return Rt(qu(e));
  if (typeof e == "string") {
    const n = yl(e);
    return Rt(n ?? new TextEncoder().encode(e));
  }
  if (Array.isArray(e))
    return Rt(Uint8Array.from(e));
  throw new Error("webauthn bytes are missing");
}
function Gi(e) {
  if (typeof e != "object" || e === null || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null)
    throw new Error("invalid webauthn creation options object");
  return e;
}
function Jw(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 88 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(e) || /[+/]/.test(e) && /[-_]/.test(e))
    throw new Error("invalid webauthn user.id encoding");
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = yl(t);
  if (!n || n.byteLength === 0 || n.byteLength > 64)
    throw new Error("webauthn user.id must be 1-64 bytes");
  const r = J(n);
  if (t !== r && t !== r.replace(/=+$/, ""))
    throw new Error("invalid webauthn user.id encoding");
  return Rt(n);
}
function Qw(e) {
  const t = Pi(e);
  if (t.byteLength === 0 || t.byteLength > 64)
    throw new Error("vault user handle must be 1-64 bytes");
  return t;
}
function ai(e) {
  if (Array.isArray(e))
    return e.map((t) => {
      const n = Xt(t);
      if (!n || n.type !== "public-key")
        throw new Error("invalid WebAuthn credential descriptor");
      return {
        type: "public-key",
        id: Pi(n.id),
        ...Array.isArray(n.transports) ? { transports: n.transports } : {}
      };
    });
}
function Ha(e) {
  return !Array.isArray(e) || e.length === 0 ? [
    { type: "public-key", alg: -7 },
    { type: "public-key", alg: -257 }
  ] : e.map((t) => {
    const n = Xt(t);
    if (!n || n.type !== "public-key" || typeof n.alg != "number")
      throw new Error("invalid WebAuthn pubKeyCredParams");
    return { type: "public-key", alg: n.alg };
  });
}
function Fa(e) {
  return {
    ...Xt(e) ?? {},
    residentKey: "required",
    userVerification: "required"
  };
}
function Ka(e) {
  if (e === "none" || e === "indirect" || e === "direct" || e === "enterprise") return e;
}
function eg(e) {
  const t = Xt(e) ?? {};
  return { ...t, prf: Xt(t.prf) ?? {} };
}
function mt() {
  return window.location.hostname;
}
function tg(e) {
  const t = e.authzCreationOptions === void 0 ? void 0 : Gi(e.authzCreationOptions), n = t && Object.prototype.hasOwnProperty.call(t, "webauthn") ? Gi(t.webauthn) : t, r = typeof t?.challenge_id == "string" ? t.challenge_id : void 0, i = Xt(n?.rp), o = typeof i?.id == "string" ? i.id : void 0;
  if (o && o !== e.rpId)
    throw new Error("webauthn rp_id does not match sandbox host");
  if (!n)
    return {
      options: {
        rp: { name: Ar, id: e.rpId },
        user: {
          id: Qw(e.vaultUserHandle),
          name: e.vaultUserHandle || Ma,
          displayName: e.displayName || Ar
        },
        challenge: hl(),
        pubKeyCredParams: Ha(void 0),
        authenticatorSelection: Fa(void 0),
        extensions: { prf: {} }
      }
    };
  const s = Gi(n.user);
  return {
    options: {
      rp: {
        name: typeof i?.name == "string" ? i.name : Ar,
        id: e.rpId
      },
      user: {
        id: Jw(s.id),
        name: typeof s.name == "string" ? s.name : e.vaultUserHandle || Ma,
        displayName: typeof s.displayName == "string" ? s.displayName : e.displayName || Ar
      },
      challenge: Pi(n.challenge),
      pubKeyCredParams: Ha(n.pubKeyCredParams),
      authenticatorSelection: Fa(n.authenticatorSelection),
      extensions: eg(n.extensions),
      ...ai(n.excludeCredentials) ? { excludeCredentials: ai(n.excludeCredentials) } : {},
      ...typeof n.timeout == "number" ? { timeout: n.timeout } : {},
      ...Ka(n.attestation) ? { attestation: Ka(n.attestation) } : {}
    },
    ...r ? { challengeId: r } : {}
  };
}
function ng(e) {
  if (e.length === 0) throw new Error("passkey-not-configured");
  if (e.length > 1) throw new Error("passkey-multiple-not-supported");
  return e[0];
}
function rg(e, t) {
  const n = ng(e), r = {
    challenge: hl(),
    rpId: t,
    userVerification: "required",
    extensions: Zy(n.prfSalt)
  };
  return n.credentialId && (r.allowCredentials = [
    {
      type: "public-key",
      id: Rt(fe(n.credentialId))
    }
  ]), r;
}
function ig(e, t, n) {
  const r = {
    challenge: Rt(n),
    rpId: t,
    userVerification: "required"
  }, i = e.filter((o) => o.credentialId).map((o) => ({ type: "public-key", id: Rt(fe(o.credentialId)) }));
  return i.length > 0 && (r.allowCredentials = i), r;
}
function og(e, t = mt()) {
  const n = Xt(e);
  if (!n) throw new Error("webauthn assertion options are missing");
  const r = typeof n.rpId == "string" ? n.rpId : typeof n.rp_id == "string" ? n.rp_id : void 0;
  if (r && r !== t)
    throw new Error("webauthn rpId does not match sandbox host");
  return {
    challenge: Pi(n.challenge),
    rpId: t,
    userVerification: "required",
    ...ai(n.allowCredentials) ? { allowCredentials: ai(n.allowCredentials) } : {},
    ...typeof n.timeout == "number" ? { timeout: n.timeout } : {}
  };
}
function wl(e, t) {
  return {
    publicKey: e,
    ...t ? { signal: t } : {}
  };
}
function gl(e) {
  return {
    id: e.id,
    rawId: J(e.rawId),
    type: e.type
  };
}
function sg(e) {
  const t = e.response, n = typeof t.getTransports == "function" ? t.getTransports() : [];
  return {
    ...gl(e),
    response: {
      clientDataJSON: J(t.clientDataJSON),
      attestationObject: J(t.attestationObject),
      transports: n
    }
  };
}
function ag(e) {
  const t = e.response;
  return {
    ...gl(e),
    response: {
      clientDataJSON: J(t.clientDataJSON),
      authenticatorData: J(t.authenticatorData),
      signature: J(t.signature),
      userHandle: t.userHandle ? J(t.userHandle) : null
    }
  };
}
function cg(e) {
  const n = e?.getClientExtensionResults()?.prf?.results?.first;
  if (!n) throw new Error("passkey-prf-unavailable");
  const r = qu(n, "passkey PRF output");
  if (r.byteLength === 0) throw new Error("passkey-prf-unavailable");
  return r;
}
async function xs(e, t, n) {
  const r = await navigator.credentials.get(
    wl(rg(e, t), n)
  );
  if (!r) throw new Error("passkey-cancelled");
  const i = cg(r), o = J(r.rawId), s = e.find((a) => a.credentialId === o);
  return { prfOutput: i, prfSalt: s?.prfSalt ?? e[0].prfSalt, credentialId: o };
}
async function ug(e, t, n, r) {
  if (!await navigator.credentials.get(
    wl(ig(e, t, n), r)
  )) throw new Error("passkey-cancelled");
}
async function lg(e) {
  const t = await navigator.credentials.get({
    publicKey: og(e)
  });
  if (!t) throw new Error("passkey-cancelled");
  return ag(t);
}
const dg = /ancestor|not the same as its|cross-?origin|different origin/i;
function ml(e) {
  return typeof e?.name == "string" ? e.name : "";
}
function fg(e) {
  return e instanceof Error || typeof e?.message == "string" ? e.message : typeof e == "string" ? e : "";
}
function bl(e) {
  const t = ml(e);
  return t === "SecurityError" || t === "NotSupportedError" || dg.test(fg(e));
}
function pg(e) {
  return ml(e) === "NotAllowedError" && !bl(e);
}
function ks(e) {
  const { options: t, challengeId: n } = tg(e);
  return navigator.credentials.create({
    publicKey: t
  }).then((i) => {
    if (!i) throw new Error("passkey-cancelled");
    const o = J(i.rawId), s = sg(i), a = e.authzCreationOptions !== void 0;
    return {
      credentialId: o,
      ...a ? { authzRegistration: n ? { challenge_id: n, credential: s } : s } : {}
    };
  });
}
const xo = {
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
    "sign.confirm": "Confirm sign",
    "review.details": "Details",
    "review.copy": "Copy",
    "review.copied": "Copied",
    "review.copySelected": "Selected; copy with the keyboard",
    "review.kind.solanaSystemTransfer": "Solana · SOL transfer",
    "review.kind.solanaTokenTransfer": "Solana · token transfer",
    "review.kind.solanaTransaction": "Solana transaction",
    "review.kind.evmTransaction": "EVM transaction",
    "review.kind.bitcoinTransfer": "Bitcoin · transfer",
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
    "sign.confirm": "确认签名",
    "review.details": "详情",
    "review.copy": "复制",
    "review.copied": "已复制",
    "review.copySelected": "已选中，请用键盘复制",
    "review.kind.solanaSystemTransfer": "Solana · SOL 转账",
    "review.kind.solanaTokenTransfer": "Solana · 代币转账",
    "review.kind.solanaTransaction": "Solana 交易",
    "review.kind.evmTransaction": "EVM 交易",
    "review.kind.bitcoinTransfer": "Bitcoin · 转账",
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
let ci = "en";
function hg(e) {
  return e === "zh" ? "zh" : "en";
}
function yg(e) {
  const t = hg(e);
  return t === ci ? !1 : (ci = t, !0);
}
function wg() {
  return ci;
}
function gg(e) {
  return typeof e == "string" && Object.hasOwn(xo.en, e);
}
function F(e, t = {}) {
  const n = xo[ci], r = xo.en;
  return (n[e] ?? r[e] ?? e).replace(/\{\{(\w+)\}\}/g, (o, s) => String(t[s] ?? ""));
}
function mg(e) {
  return JSON.stringify(e ?? {});
}
function Da(e) {
  if (!e) return {};
  try {
    const t = JSON.parse(e);
    return typeof t == "object" && t !== null ? t : {};
  } catch {
    return {};
  }
}
function ke(e, t, n) {
  e.dataset.i18nKey = t, e.dataset.i18nParams = mg(n), e.textContent = F(t, n);
}
function Re(e, t) {
  delete e.dataset.i18nKey, delete e.dataset.i18nParams, e.textContent = t;
}
function bg(e = document) {
  e.querySelectorAll("[data-i18n-key]").forEach((t) => {
    const n = t.dataset.i18nKey;
    n && (t.textContent = F(n, Da(t.dataset.i18nParams)));
  }), e.querySelectorAll("[data-i18n-placeholder-key]").forEach((t) => {
    const n = t.dataset.i18nPlaceholderKey;
    n && (t.placeholder = F(n, Da(t.dataset.i18nPlaceholderParams)));
  });
}
const vl = {
  sol: { symbol: "SOL", baseUnit: "lamports", decimals: 9 },
  eth: { symbol: "ETH", baseUnit: "wei", decimals: 18 },
  btc: { symbol: "BTC", baseUnit: "sats", decimals: 8 }
};
function ja(e, t, n) {
  const { symbol: r, baseUnit: i, decimals: o } = vl[t];
  if (!/^(?:0|[1-9][0-9]*)$/.test(e) || n === "base") return `${e} ${i}`;
  const s = e.padStart(o + 1, "0"), a = s.slice(-o).replace(/0+$/, ""), c = `${s.slice(0, -o)}${a ? `.${a}` : ""} ${r}`;
  return n === "symbol" ? c : `${c} (${e} ${i})`;
}
const ui = 8, li = 6, vg = 28;
function Va(e, t) {
  if (!t || e.length <= ui + li + 2) {
    const r = Z("span", "review-mono");
    return Re(r, e), r;
  }
  const n = Z("button", "review-mono review-elided");
  return n.type = "button", n.setAttribute("aria-expanded", "false"), n.title = e, Re(n, `${e.slice(0, ui)}…${e.slice(-li)}`), n.addEventListener("click", () => {
    n.getAttribute("aria-expanded") !== "true" && (n.setAttribute("aria-expanded", "true"), Re(n, e));
  }), n;
}
function Z(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
const xg = "http://www.w3.org/2000/svg";
function _r(e, t) {
  const n = document.createElementNS(xg, e);
  for (const [r, i] of Object.entries(t)) n.setAttribute(r, i);
  return n;
}
function qa() {
  const e = _r("svg", {
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
    _r("path", { d: "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" }),
    _r("path", { d: "M12 9v4.5" }),
    _r("path", { d: "M12 17h.01" })
  ), e;
}
function kg(e) {
  if (!e.notes?.length) return null;
  const t = Z("span", "review-notes");
  for (const n of e.notes) {
    const r = Z("span", "review-note");
    typeof n == "string" ? ke(r, n) : ke(r, n.key, { count: n.count }), t.append(r);
  }
  return t;
}
function Wi(e, t = "review-label") {
  const n = Z(e.heading ? "p" : "span", t), r = {
    ...e.index === void 0 ? {} : { index: e.index },
    ...e.name === void 0 ? {} : { name: e.name }
  };
  return ke(n, e.label, Object.keys(r).length > 0 ? r : void 0), n;
}
function Eg(e, t) {
  const n = Z("button", "review-copy");
  return n.type = "button", ke(n, "review.copy"), n.addEventListener("click", () => {
    const r = () => {
      window.getSelection()?.selectAllChildren(e), ke(n, "review.copySelected");
    };
    let i;
    try {
      i = navigator.clipboard.writeText(t);
    } catch {
      r();
      return;
    }
    i.then(() => ke(n, "review.copied"), r);
  }), n;
}
function Ir(e, t = !1) {
  if (e.heading) return Wi(e, "review-subheading");
  const n = Z("div", e.raw ? "review-field review-field-raw" : "review-field"), r = kg(e);
  if (e.raw) {
    const s = Z("div", "review-raw-head"), a = Z("pre", "review-raw");
    return Re(a, e.value), s.append(Wi(e)), r && s.append(r), s.append(Eg(a, e.value)), n.append(s, a), n;
  }
  const i = Z("div", "review-value"), o = t && e.shownAs ? 0 : e.native ? ja(e.value, e.native, e.unit).length : t && e.elide && e.value.length > ui + li + 2 ? ui + li + 1 : e.value.length;
  if (t && e.to === void 0 && o + (e.secondary?.length ?? 0) <= vg && n.classList.add("review-field-inline"), t && e.shownAs) {
    const s = Z("span", "review-word");
    ke(s, e.shownAs), i.append(s);
  } else if (e.native) {
    const s = Z("span", "review-mono");
    Re(s, ja(e.value, e.native, e.unit)), i.append(s);
  } else
    i.append(Va(e.value, t && e.elide === !0));
  if (e.secondary !== void 0) {
    const s = Z("span", "review-mono review-secondary");
    Re(s, e.secondary), i.append(s);
  }
  if (e.to !== void 0) {
    const s = Z("span", "review-arrow");
    Re(s, "→"), i.append(s, Va(e.to, t));
  }
  return r && i.append(r), n.append(Wi(e), i), n;
}
function Ga(e, t, n) {
  const r = Z("section", "review-group"), i = Z("h2", "review-group-title");
  return ke(i, e, t === void 0 ? void 0 : { index: t }), r.append(i, ...n), r;
}
function Sg(e, t, n = {}) {
  Re(e, "");
  const r = Z("div", "review"), i = Z("div", "review-heading"), o = Z("p", "review-kind");
  if (ke(o, t.kind), i.append(o), n.session) {
    const l = Z("p", "review-session"), d = Z("span", "review-session-label");
    ke(d, "context.session");
    const h = Z("span", "review-session-value");
    Re(h, n.session), l.append(d, h), i.append(l);
  }
  r.append(i);
  for (const l of t.warnings ?? []) {
    const d = Z("p", "review-warning"), h = Z("span", "review-warning-text");
    ke(h, l), d.append(qa(), h), r.append(d);
  }
  if (t.undecoded) {
    const l = Z("p", "review-notice"), d = Z("span", "review-notice-text");
    ke(d, "sign.blindNotice"), l.append(qa(), d), r.append(l);
  }
  const s = Z("div", "review-summary");
  s.append(...t.summary.map((l) => Ir(l, !0)), Ir(t.signer, !0)), r.append(s);
  const a = Z("details", "review-details"), c = Z("summary", "review-details-toggle");
  ke(c, "review.details");
  const u = Z("div", "review-details-body");
  t.details.forEach((l, d) => {
    const h = l.fields.map((f) => Ir(f));
    u.append(Ga(l.title, l.index, d === 0 ? [Ir(t.signer), ...h] : h));
  }), n.rows && u.append(Ga("review.group.request", void 0, [n.rows])), a.append(c, u), r.append(a), e.append(r);
}
function we(e) {
  throw new Error(`signing review is malformed: ${e}`);
}
function di(e, t, n) {
  (typeof e != "object" || e === null || Array.isArray(e)) && we(`${n} must be an object`);
  const r = e;
  for (const i of Object.keys(r)) t.includes(i) || we(`${n}.${i} is not a review field`);
  return r;
}
function qt(e, t, n) {
  return (!gg(e) || !e.startsWith(`${t}.`)) && we(`${n} must be a ${t} translation key`), e;
}
function ko(e, t) {
  if (e !== void 0)
    return (typeof e != "number" || !Number.isSafeInteger(e) || e < 0) && we(`${t} must be a non-negative integer`), e;
}
function Dn(e, t) {
  return Array.isArray(e) || we(`${t} must be a list`), e;
}
const Ag = 4096, _g = /^[A-Za-z_][A-Za-z0-9_]{0,63} \([a-z][a-z0-9[\]]{0,31}\)$/;
function Zi(e, t) {
  const n = di(e, ["label", "index", "name", "value", "native", "unit", "to", "secondary", "elide", "shownAs", "notes", "raw", "heading"], t), r = ko(n.index, `${t}.index`);
  n.name !== void 0 && (typeof n.name != "string" || (n.label === "review.field.typedField" ? n.name.length > Ag : !_g.test(n.name))) && we(`${t}.name is not a valid field name`), typeof n.value != "string" && we(`${t}.value must be a string`), n.native !== void 0 && !Object.hasOwn(vl, n.native) && we(`${t}.native is not a native currency`), n.unit !== void 0 && (n.native === void 0 || n.unit !== "symbol" && n.unit !== "base") && we(`${t}.unit is not a unit of its currency`), n.to !== void 0 && typeof n.to != "string" && we(`${t}.to must be a string`), n.secondary !== void 0 && typeof n.secondary != "string" && we(`${t}.secondary must be a string`), n.elide !== void 0 && typeof n.elide != "boolean" && we(`${t}.elide must be a boolean`);
  const i = n.shownAs === void 0 ? void 0 : qt(n.shownAs, "review.value", `${t}.shownAs`);
  n.raw !== void 0 && typeof n.raw != "boolean" && we(`${t}.raw must be a boolean`), n.heading !== void 0 && typeof n.heading != "boolean" && we(`${t}.heading must be a boolean`), n.heading && (n.value !== "" || n.native !== void 0 || n.notes !== void 0 || n.raw !== void 0 || n.to !== void 0 || n.secondary !== void 0 || n.elide !== void 0 || i !== void 0) && we(`${t} is a heading and cannot carry a value`);
  const o = n.notes === void 0 ? void 0 : Dn(n.notes, `${t}.notes`).map((s, a) => {
    const c = `${t}.notes[${a}]`;
    if (typeof s == "string") return qt(s, "review.note", c);
    const u = di(s, ["key", "count"], c), l = ko(u.count, `${c}.count`);
    return l === void 0 && we(`${c}.count is required`), { key: qt(u.key, "review.note", `${c}.key`), count: l };
  });
  return {
    label: qt(n.label, "review.field", `${t}.label`),
    ...r === void 0 ? {} : { index: r },
    ...n.name === void 0 ? {} : { name: n.name },
    value: n.value,
    ...n.native === void 0 ? {} : { native: n.native },
    ...n.unit === void 0 ? {} : { unit: n.unit },
    ...n.to === void 0 ? {} : { to: n.to },
    ...n.secondary === void 0 ? {} : { secondary: n.secondary },
    ...n.elide === void 0 ? {} : { elide: n.elide },
    ...i === void 0 ? {} : { shownAs: i },
    ...o === void 0 ? {} : { notes: o },
    ...n.raw === void 0 ? {} : { raw: n.raw },
    ...n.heading === void 0 ? {} : { heading: n.heading }
  };
}
function xl(e) {
  const t = di(e, ["kind", "signer", "summary", "undecoded", "warnings", "details"], "review");
  typeof t.undecoded != "boolean" && we("review.undecoded must be a boolean");
  const n = t.warnings === void 0 ? void 0 : Dn(t.warnings, "review.warnings").map((r, i) => qt(r, "review.warning", `review.warnings[${i}]`));
  return {
    kind: qt(t.kind, "review.kind", "review.kind"),
    signer: Zi(t.signer, "review.signer"),
    summary: Dn(t.summary, "review.summary").map((r, i) => Zi(r, `review.summary[${i}]`)),
    undecoded: t.undecoded,
    ...n === void 0 ? {} : { warnings: n },
    details: Dn(t.details, "review.details").map((r, i) => {
      const o = `review.details[${i}]`, s = di(r, ["title", "index", "fields"], o), a = ko(s.index, `${o}.index`);
      return {
        title: qt(s.title, "review.group", `${o}.title`),
        ...a === void 0 ? {} : { index: a },
        fields: Dn(s.fields, `${o}.fields`).map((c, u) => Zi(c, `${o}.fields[${u}]`))
      };
    })
  };
}
let Es = null;
function Ig(e) {
  Es = e;
}
function Pg() {
  Es?.("ui.show");
}
function Tg() {
  Es?.("ui.hide");
}
function Bg() {
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
function kl(e) {
  e.querySelectorAll(".dynamic").forEach((t) => t.remove());
}
function ge(e) {
  return { text: e };
}
function nr(e, t) {
  return { key: e, params: t };
}
function W(e, t) {
  if (typeof t == "string") {
    ke(e, t);
    return;
  }
  if ("text" in t) {
    Re(e, t.text);
    return;
  }
  ke(e, t.key, t.params);
}
function Ss(e, t, n) {
  const r = Bg();
  return document.body.classList.add("interactive"), document.body.classList.remove("confirming"), Pg(), r.card.classList.remove("confirm-card"), W(r.title, e), W(r.subtitle, t), n ? W(r.body, n) : Re(r.body, ""), kl(r.card), r;
}
function As() {
  document.body.classList.remove("interactive"), document.body.classList.remove("confirming");
  const e = document.getElementById("page")?.querySelector(".card");
  e && (e.classList.remove("confirm-card"), kl(e)), Tg();
}
function fn(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic"), (e.querySelector(".card-scroll") ?? e).append(t);
}
function Wa(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic");
  const n = e.querySelector(".card-footer");
  if (!n) throw new Error("sandbox confirmation footer is unavailable");
  n.append(t);
}
function $g(e) {
  const t = document.createElement("div");
  if (t.className = "detail-value", e.variant === "chips") {
    t.classList.add("detail-chips");
    for (const r of e.values) {
      const i = document.createElement("span");
      i.className = `detail-chip${e.tone === "warning" ? " detail-chip-warning" : ""}`, W(i, r), t.append(i);
    }
    return t;
  }
  const n = document.createElement(e.variant === "command" ? "code" : "span");
  return n.className = e.variant === "command" ? "detail-command" : e.variant === "mono" ? "detail-mono" : "detail-text", W(n, e.value), e.variant === "mono" && (n.title = n.textContent ?? ""), t.append(n), t;
}
function El(e) {
  const t = document.createElement("div");
  t.className = "confirmation-details";
  for (const n of e.rows) {
    const r = document.createElement("div");
    r.className = "detail-row";
    const i = document.createElement("span");
    i.className = "detail-label", W(i, n.label), r.append(i, $g(n)), t.append(r);
  }
  return t;
}
function Og(e, t, n) {
  if (Re(e, ""), e.append(El(t)), n) {
    const r = document.createElement("p");
    r.className = "confirmation-note", W(r, n), e.append(r);
  }
}
function rr(e) {
  const t = document.createElement("button");
  return t.type = "button", t.className = "action dynamic", W(t, e), t;
}
function Sl(e = ge("")) {
  const t = document.createElement("p");
  return t.className = "setup-status dynamic", W(t, e), t;
}
let Pr = null;
function _s() {
  return new Error("operation-superseded");
}
function Xn(e) {
  const t = e.reason;
  return t instanceof Error ? t : _s();
}
function Za(e) {
  if (e?.aborted) throw Xn(e);
}
function dr(e) {
  Pr?.abort(_s());
  const t = new AbortController();
  return Pr = t, Promise.resolve().then(() => e(t.signal)).finally(() => {
    Pr === t && (Pr = null);
  });
}
async function Rg(e) {
  return dr(async (t) => {
    Za(t), Za(e.abortSignal);
    const n = new TextDecoder().decode(e.plaintext), r = Ss("plaintext.title", ge(e.name), "plaintext.body"), i = document.createElement("pre");
    i.className = "plaintext dynamic", i.textContent = n;
    const o = rr("common.done"), s = rr("plaintext.copy");
    s.classList.add("secondary");
    const a = Sl("plaintext.copyWarning");
    fn(r.card, i), fn(r.card, a), fn(r.card, s), fn(r.card, o);
    try {
      await new Promise((c, u) => {
        if (t.aborted) {
          u(Xn(t));
          return;
        }
        if (e.abortSignal?.aborted) {
          u(Xn(e.abortSignal));
          return;
        }
        let l = !1, d = () => {
        }, h = null, f = null, g = !e.prepareCopy;
        const m = () => {
          h?.abort(_s());
        }, p = (I) => {
          l || (l = !0, d(), m(), I());
        }, y = (I) => {
          p(() => u(Xn(I)));
        }, v = () => y(t), x = () => {
          e.abortSignal && y(e.abortSignal);
        };
        d = () => {
          t.removeEventListener("abort", v), e.abortSignal?.removeEventListener("abort", x);
        }, t.addEventListener("abort", v, { once: !0 }), e.abortSignal?.addEventListener("abort", x, { once: !0 }), s.addEventListener("click", () => {
          if (f) return;
          if (t.aborted) {
            y(t);
            return;
          }
          if (e.abortSignal?.aborted) {
            y(e.abortSignal);
            return;
          }
          s.disabled = !0;
          const I = new AbortController();
          h = I;
          const E = () => {
            try {
              return Promise.resolve(e.onCopy ? e.onCopy(n, I.signal) : navigator.clipboard.writeText(n));
            } catch (T) {
              return Promise.reject(T);
            }
          }, $ = e.prepareCopy && !g ? Promise.resolve(e.prepareCopy(I.signal)).then(() => {
            I.signal.aborted || t.aborted || e.abortSignal?.aborted || (g = !0, W(a, "plaintext.copyReady"), s.disabled = !1);
          }) : E().then(() => {
            I.signal.aborted || t.aborted || e.abortSignal?.aborted || (e.prepareCopy && (g = !1), W(a, "plaintext.copiedBody"), s.disabled = !1);
          });
          f = $, $.then(
            () => {
              I.signal.aborted;
            },
            (T) => {
              if (!(I.signal.aborted || l)) {
                if (t.aborted) {
                  y(t);
                  return;
                }
                if (e.abortSignal?.aborted) {
                  y(e.abortSignal);
                  return;
                }
                s.disabled = !1, W(a, "plaintext.copyFailed");
              }
            }
          ).finally(() => {
            h === I && (h = null), f === $ && (f = null);
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
      As();
    }
  });
}
function Is(e, t, n, r) {
  const i = Ss(e.title, e.subtitle, e.review ? void 0 : e.body);
  document.body.classList.add("confirming"), i.card.classList.add("confirm-card"), e.review ? Sg(i.body, e.review, {
    session: e.reviewSession,
    rows: e.details && El(e.details)
  }) : e.details && Og(i.body, e.details, e.body);
  const o = rr(e.confirmLabel), s = rr("common.cancel");
  return s.classList.add("secondary"), Wa(i.card, s), Wa(i.card, o), o.disabled = !0, new Promise((a, c) => {
    let u = !1, l;
    const d = setTimeout(() => {
      try {
        l = n?.(i.card);
      } catch (m) {
        f(() => c(m));
        return;
      }
      (l?.ready ?? Promise.resolve()).then(
        () => {
          !u && !t.aborted && (o.disabled = !1);
        },
        (m) => f(() => c(m))
      );
    }, 500), h = () => {
      f(() => c(Xn(t)));
    }, f = (g) => {
      u || (u = !0, clearTimeout(d), l?.dispose(), t.removeEventListener("abort", h), As(), g());
    };
    if (t.aborted) {
      h();
      return;
    }
    t.addEventListener("abort", h, { once: !0 }), o.addEventListener("click", () => {
      if (o.disabled) return;
      o.disabled = !0, s.disabled = !0;
      let g;
      try {
        l?.assertCurrent(), g = r ? r() : Promise.resolve();
      } catch (m) {
        f(() => c(m));
        return;
      }
      g.then(
        () => f(a),
        (m) => f(() => c(m))
      );
    }), s.addEventListener(
      "click",
      () => {
        f(() => c(new Error("operation-cancelled")));
      },
      { once: !0 }
    );
  });
}
function fi(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function Lg(e) {
  return new Promise((t, n) => {
    const r = () => n(fi(e));
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function Xa(e) {
  if (e?.aborted) throw fi(e);
}
async function Al(e) {
  const t = vs(e.wrapMeta), n = rs(t.wrapMeta);
  let r;
  try {
    const i = await Promise.race([
      xs(n, e.currentRpId, e.abortSignal).then(
        (o) => {
          if (e.abortSignal?.aborted)
            throw o.prfOutput.fill(0), fi(e.abortSignal);
          return o;
        },
        (o) => {
          throw e.abortSignal?.aborted ? fi(e.abortSignal) : o;
        }
      ),
      ...e.abortSignal ? [Lg(e.abortSignal)] : []
    ]);
    return r = i.prfOutput, await _l({
      wrapMeta: t.wrapMeta,
      currentRpId: e.currentRpId,
      assertion: i,
      ...e.abortSignal ? { abortSignal: e.abortSignal } : {},
      ...e.policy ? { policy: e.policy } : {}
    });
  } finally {
    r?.fill(0);
  }
}
async function _l(e) {
  const t = vs(e.wrapMeta);
  let n;
  try {
    Xa(e.abortSignal), n = await Qy(t.wrapMeta, {
      kind: "passkey",
      prfOutput: e.assertion.prfOutput,
      prfSalt: e.assertion.prfSalt
    }), Xa(e.abortSignal);
    const r = pl({
      vmk: n,
      wrapMeta: t.wrapMeta,
      freshSetup: !1,
      scopeId: t.scopeId,
      ...e.policy ? { policy: e.policy } : {}
    });
    return n = void 0, { state: r.state, rpId: e.currentRpId, expiresAt: r.expiresAt };
  } finally {
    e.assertion.prfOutput.fill(0), n?.fill(0);
  }
}
function Cg(e) {
  if (typeof e != "object" || e === null)
    throw new b("invalid_payload", "request payload must be an object");
  return e;
}
function zg(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new b("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ya(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new b("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Ug(e) {
  const t = Cg(e), n = t.kind === "keypair" ? "keypair" : t.kind === "static" ? "static" : null;
  if (!n) throw new b("invalid_payload", "kind must be static or keypair");
  const r = Ya(t.name, "name");
  if (t.rootMetadata !== void 0) throw new b("invalid_payload", "seal cannot set vault root metadata");
  const i = zg(t.wrapMeta, "wrapMeta");
  if (n === "static") {
    if (t.curve !== void 0) throw new b("invalid_payload", "static seal cannot carry a curve");
    if (t.inputMode !== "parent-value")
      throw new b("invalid_payload", "static seal inputMode must be parent-value");
    return {
      name: r,
      kind: "static",
      inputMode: "parent-value",
      value: Ya(t.value, "value"),
      wrapMeta: i
    };
  }
  if (t.inputMode !== void 0)
    throw new b("invalid_payload", "keypair seal is generate-only and does not accept inputMode");
  if (t.value !== void 0)
    throw new b("invalid_payload", "keypair seal cannot accept parent-provided private key material");
  let o;
  try {
    o = An(t.curve);
  } catch {
    throw new b("invalid_payload", "unsupported signing curve");
  }
  return {
    name: r,
    kind: "keypair",
    ...t.curve !== void 0 ? { curve: o } : {},
    wrapMeta: i
  };
}
function Ps(e) {
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
const Ng = 512, Il = "avibe-vault-signed-context-replay:v2", Mg = "avibe-vault-signed-context-replay:v2", Hg = "avibe-vault-signed-context-replay:v2", Fe = /* @__PURE__ */ new Map();
let Ja = !1, jn = null;
function ze(e) {
  return typeof e == "object" && e !== null;
}
function ft(e, t) {
  if (typeof e != "string" || e.length === 0) throw new b("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Rn(e, t) {
  if (e !== void 0) {
    if (typeof e != "string") throw new b("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Xi(e, t) {
  if (e !== void 0) {
    if (!Array.isArray(e) || !e.every((n) => typeof n == "string"))
      throw new b("invalid_payload", `${t} must be a string array`);
    return e;
  }
}
function Fg(e, t) {
  if (e !== "static" && e !== "keypair") throw new b("invalid_payload", `${t} must be static or keypair`);
  return e;
}
function Kg(e) {
  if (e !== "agent-deliver" && e !== "sign" && e !== "reveal")
    throw new b("invalid_payload", "context purpose is invalid");
  return e;
}
function Eo(e, t) {
  if (typeof e != "number" || !Number.isSafeInteger(e) || e < 0)
    throw new b("invalid_payload", `${t} must be a non-negative integer`);
  return e;
}
function Dg(e, t) {
  if (!Array.isArray(e) || e.length < 16 || e.length > 128 || !e.every((n) => Number.isInteger(n) && n >= 0 && n <= 255))
    throw new b("invalid_payload", `${t} must contain 16..128 byte integers`);
  return e;
}
function Ts(e, t) {
  if (!ze(e)) throw new b(t, "agent delivery context.release must be an object");
  return {
    name: ft(e.name, "context.release.name"),
    ttlSecs: Eo(e.ttlSecs, "context.release.ttlSecs"),
    approvalNonce: Dg(e.approvalNonce, "context.release.approvalNonce"),
    approvalExpiresAtUnix: Eo(e.approvalExpiresAtUnix, "context.release.approvalExpiresAtUnix"),
    operationHash: ft(e.operationHash, "context.release.operationHash")
  };
}
function it(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(it).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${it(t[n])}`).join(",")}}`;
}
function Bs(e) {
  const t = ze(e) ? e : null;
  if (!t) throw new b("invalid_payload", "context must be an object");
  if (t.v !== 2) throw new b("invalid_payload", "context version must be 2");
  const n = Kg(t.purpose);
  ft(t.requestId, "context.requestId"), t.grantId !== void 0 && Rn(t.grantId, "context.grantId");
  const r = ze(t.display) ? t.display : null;
  if (!r) throw new b("invalid_payload", "context.display must be an object");
  if (!Array.isArray(r.secrets) || r.secrets.length === 0)
    throw new b("invalid_payload", "context.display.secrets must be a non-empty array");
  r.secrets.forEach((c, u) => {
    if (!ze(c)) throw new b("invalid_payload", `context.display.secrets[${u}] must be an object`);
    ft(c.name, `context.display.secrets[${u}].name`), Fg(c.kind, `context.display.secrets[${u}].kind`);
  }), r.sessionLabel !== void 0 && Rn(r.sessionLabel, "context.display.sessionLabel"), r.command !== void 0 && Rn(r.command, "context.display.command"), r.egress !== void 0 && Rn(r.egress, "context.display.egress");
  const i = r.source;
  if (i !== void 0) {
    if (!ze(i)) throw new b("invalid_payload", "context.display.source must be an object");
    i.env !== void 0 && Xi(i.env, "context.display.source.env"), i.tags !== void 0 && Xi(i.tags, "context.display.source.tags"), i.skills !== void 0 && Xi(i.skills, "context.display.source.skills");
  }
  r.grantTtlSeconds !== void 0 && Eo(r.grantTtlSeconds, "context.display.grantTtlSeconds");
  const o = ze(t.signature) ? t.signature : null;
  if (!o) throw new b("invalid_payload", "context.signature must be an object");
  if (o.alg !== "ed25519") throw new b("invalid_payload", "context signature alg must be ed25519");
  ft(o.keyId, "context.signature.keyId"), ft(o.value, "context.signature.value");
  const s = t.agent;
  if (s !== void 0) {
    if (!ze(s)) throw new b("invalid_payload", "context.agent must be an object");
    const c = ze(s.publicKey) ? s.publicKey : null;
    if (!c) throw new b("invalid_payload", "context.agent.publicKey must be an object");
    ft(c.public_key, "context.agent.publicKey.public_key"), c.fingerprint !== void 0 && Rn(c.fingerprint, "context.agent.publicKey.fingerprint"), ft(s.fingerprint, "context.agent.fingerprint");
  }
  const a = t.release;
  if (n === "agent-deliver") {
    const c = Ts(a, "invalid_payload");
    if (r.grantTtlSeconds === void 0)
      throw new b("invalid_payload", "agent delivery context must display its release TTL");
    if (r.grantTtlSeconds !== c.ttlSecs)
      throw new b("invalid_payload", "signed release TTL does not match display TTL");
  } else if (a !== void 0 && !ze(a))
    throw new b("invalid_payload", "context.release must be an object");
  return ft(t.expiresAt, "context.expiresAt"), t;
}
function Ti(e) {
  const { signature: t, ...n } = e;
  return it(n);
}
function So(e) {
  if (e.context.purpose !== e.expectedPurpose)
    throw new b("invalid_context", `${e.expectedPurpose} context has the wrong purpose`);
  if (!e.context.display.secrets.some(
    (n) => n.name === e.secret.name && n.kind === e.secret.kind
  )) {
    const n = e.secret.kind === "keypair" ? "key" : "secret";
    throw new b("invalid_context", `${e.expectedPurpose} context does not cover the requested ${n}`);
  }
}
function pi(e) {
  if (e.expectedPurpose && e.context.purpose !== e.expectedPurpose)
    throw new b("invalid_context", "signed context purpose does not match operation");
  const t = e.now ?? Date.now(), n = Date.parse(e.context.expiresAt);
  if (!Number.isFinite(n) || n <= t)
    throw new b("context_expired", "signed context is expired", !0);
  if (!Sw({
    rootMetadata: e.rootMetadata,
    keyId: e.context.signature.keyId,
    signature: e.context.signature.value,
    message: Ti(e.context)
  })) throw new b("invalid_context_signature", "signed context signature is invalid");
  if (e.context.purpose === "agent-deliver" && Ts(e.context.release, "invalid_context").approvalExpiresAtUnix * 1e3 <= t)
    throw new b("context_expired", "signed release approval is expired", !0);
}
function $s(e) {
  for (const [t, n] of Fe)
    n <= e && Fe.delete(t);
}
function jg() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
function kn() {
  return typeof window < "u";
}
function Os() {
  return new b("context_replay_state_unavailable", "signed context replay state is unavailable", !0);
}
function Pl(e) {
  const t = jg();
  if (!t && e && kn()) throw Os();
  return t;
}
function Tl(e, t) {
  const n = /* @__PURE__ */ new Map();
  if (!ze(e) || e.version !== 2 || !Array.isArray(e.entries)) return n;
  for (const r of e.entries) {
    if (!Array.isArray(r) || r.length !== 2) continue;
    const [i, o] = r;
    typeof i != "string" || i.length === 0 || typeof o != "number" || !Number.isFinite(o) || o > t && n.set(i, Math.max(n.get(i) ?? 0, o));
  }
  return n;
}
function Rs(e = Date.now(), t = !1) {
  $s(e);
  const n = Pl(t);
  if (n)
    try {
      n.setItem(Il, JSON.stringify({ version: 2, entries: [...Fe] }));
    } catch {
      if (t && kn()) throw Os();
    }
}
function Bl(e, t = Date.now(), n = !0) {
  for (const [r, i] of e)
    i > t && Fe.set(r, Math.max(Fe.get(r) ?? 0, i));
  n && Rs(t);
}
function Ls() {
  if (!(jn || typeof BroadcastChannel > "u"))
    try {
      jn = new BroadcastChannel(Mg), jn.addEventListener("message", (e) => {
        const t = ze(e.data) ? e.data : null;
        !t || t.type !== "signed-context-consumed" || Bl(Tl({ version: 2, entries: t.entries }, Date.now()));
      });
    } catch {
      jn = null;
    }
}
function Vg(e = Date.now()) {
  Ja || (Ls(), $l(e, kn()), Ja = !0);
}
function $l(e = Date.now(), t = !1) {
  Ls();
  const n = Pl(t);
  if (n)
    try {
      Bl(Tl(JSON.parse(n.getItem(Il) ?? "null"), e), e, !1), $s(e);
    } catch {
      if (t && kn()) throw Os();
      Rs(e);
    }
}
function qg(e) {
  Ls();
  try {
    jn?.postMessage({ type: "signed-context-consumed", entries: e });
  } catch {
  }
}
function Gg() {
  const t = globalThis.navigator?.locks;
  return !t || typeof t != "object" || typeof t.request != "function" ? null : t;
}
async function Wg(e) {
  const t = Gg();
  if (t) return await t.request(Hg, { mode: "exclusive" }, e);
  if (typeof window < "u")
    throw new b("context_replay_lock_unavailable", "signed context replay lock is unavailable", !0);
  return await e();
}
function Ol(e, t) {
  Vg(t);
  const n = /* @__PURE__ */ new Map();
  for (const r of e) {
    const i = Date.parse(r.expiresAt);
    if (!Number.isFinite(i) || i <= t)
      throw new b("context_expired", "signed context is expired", !0);
    n.set(r.requestId, Math.max(n.get(r.requestId) ?? 0, i));
  }
  $s(t);
  for (const r of n.keys())
    if (Fe.has(r))
      throw new b("context_replayed", "signed context requestId was already used", !0);
  if (Fe.size + n.size > Ng)
    throw new b("context_replay_cache_full", "signed context replay cache is full", !0);
  return n;
}
function hi(e, t = Date.now()) {
  Ol(e, t);
}
async function Bi(e, t = Date.now()) {
  const n = [...e];
  await Wg(() => {
    $l(t, kn());
    const r = Ol(n, t), i = /* @__PURE__ */ new Map();
    for (const o of r.keys()) i.set(o, Fe.get(o));
    for (const [o, s] of r) Fe.set(o, s);
    try {
      Rs(t, kn());
    } catch (o) {
      for (const [s, a] of i)
        a === void 0 ? Fe.delete(s) : Fe.set(s, a);
      throw o;
    }
    qg([...r]);
  });
}
async function Zg(e, t) {
  if (e.purpose !== "agent-deliver") throw new b("invalid_context", "context is not an agent delivery");
  if (!e.grantId) throw new b("invalid_context", "agent delivery context is missing grantId");
  const n = Ts(e.release, "invalid_context");
  if (n.name !== t) throw new b("invalid_context", "signed release name does not match release item");
  if (e.display.grantTtlSeconds === void 0 || e.display.grantTtlSeconds !== n.ttlSecs)
    throw new b("invalid_context", "signed release TTL does not match display TTL");
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
function Xg(e) {
  if (!e.agent) throw new b("invalid_context", "agent delivery context is missing agent key");
  if (e.agent.publicKey.fingerprint && e.agent.publicKey.fingerprint !== e.agent.fingerprint)
    throw new b("invalid_context", "agent public key fingerprint mismatch");
  return { ...e.agent.publicKey, fingerprint: e.agent.fingerprint };
}
async function Yg(e) {
  const t = e.map((n) => Ti(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(it(t))));
}
function Qa(e) {
  return it(e.display);
}
const Jg = "1.2.3";
let Le = class Ao extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof Ao ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof Ao && n.cause.docsPath || n.docsPath, o = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...i ? [`Docs: https://abitype.dev${i}`] : [],
      ...r ? [`Details: ${r}`] : [],
      `Version: abitype@${Jg}`
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
function bt(e, t) {
  return e.exec(t)?.groups;
}
const Rl = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, Ll = /^u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/, Cl = /^\(.+?\).*?$/, ec = /^tuple(?<array>(\[(\d*)\])*)$/;
function _o(e) {
  let t = e.type;
  if (ec.test(e.type) && "components" in e) {
    t = "(";
    const n = e.components.length;
    for (let i = 0; i < n; i++) {
      const o = e.components[i];
      t += _o(o), i < n - 1 && (t += ", ");
    }
    const r = bt(ec, e.type);
    return t += `)${r?.array || ""}`, _o({
      ...e,
      type: t
    });
  }
  return "indexed" in e && e.indexed && (t = `${t} indexed`), e.name ? `${t} ${e.name}` : t;
}
function Ln(e) {
  let t = "";
  const n = e.length;
  for (let r = 0; r < n; r++) {
    const i = e[r];
    t += _o(i), r !== n - 1 && (t += ", ");
  }
  return t;
}
function Qg(e) {
  return e.type === "function" ? `function ${e.name}(${Ln(e.inputs)})${e.stateMutability && e.stateMutability !== "nonpayable" ? ` ${e.stateMutability}` : ""}${e.outputs?.length ? ` returns (${Ln(e.outputs)})` : ""}` : e.type === "event" ? `event ${e.name}(${Ln(e.inputs)})` : e.type === "error" ? `error ${e.name}(${Ln(e.inputs)})` : e.type === "constructor" ? `constructor(${Ln(e.inputs)})${e.stateMutability === "payable" ? " payable" : ""}` : e.type === "fallback" ? `fallback() external${e.stateMutability === "payable" ? " payable" : ""}` : "receive() external payable";
}
const zl = /^error (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function em(e) {
  return zl.test(e);
}
function tm(e) {
  return bt(zl, e);
}
const Ul = /^event (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function nm(e) {
  return Ul.test(e);
}
function rm(e) {
  return bt(Ul, e);
}
const Nl = /^function (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)(?: (?<scope>external|public{1}))?(?: (?<stateMutability>pure|view|nonpayable|payable{1}))?(?: returns\s?\((?<returns>.*?)\))?$/;
function im(e) {
  return Nl.test(e);
}
function om(e) {
  return bt(Nl, e);
}
const Ml = /^struct (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*) \{(?<properties>.*?)\}$/;
function Hl(e) {
  return Ml.test(e);
}
function sm(e) {
  return bt(Ml, e);
}
const Fl = /^constructor\((?<parameters>.*?)\)(?:\s(?<stateMutability>payable{1}))?$/;
function am(e) {
  return Fl.test(e);
}
function cm(e) {
  return bt(Fl, e);
}
const Kl = /^fallback\(\) external(?:\s(?<stateMutability>payable{1}))?$/;
function um(e) {
  return Kl.test(e);
}
function lm(e) {
  return bt(Kl, e);
}
const dm = /^receive\(\) external payable$/;
function fm(e) {
  return dm.test(e);
}
const pm = /* @__PURE__ */ new Set(["indexed"]), Io = /* @__PURE__ */ new Set([
  "calldata",
  "memory",
  "storage"
]);
class hm extends Le {
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
class ym extends Le {
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
class wm extends Le {
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
class gm extends Le {
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
class mm extends Le {
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
class bm extends Le {
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
class vm extends Le {
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
class _n extends Le {
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
class xm extends Le {
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
class km extends Le {
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
class Em extends Le {
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
class Sm extends Le {
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
function Am(e, t, n) {
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
const Yi = /* @__PURE__ */ new Map([
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
function _m(e, t = {}) {
  if (im(e))
    return Im(e, t);
  if (nm(e))
    return Pm(e, t);
  if (em(e))
    return Tm(e, t);
  if (am(e))
    return Bm(e, t);
  if (um(e))
    return $m(e);
  if (fm(e))
    return {
      type: "receive",
      stateMutability: "payable"
    };
  throw new xm({ signature: e });
}
function Im(e, t = {}) {
  const n = om(e);
  if (!n)
    throw new _n({ signature: e, type: "function" });
  const r = Ne(n.parameters), i = [], o = r.length;
  for (let a = 0; a < o; a++)
    i.push(Yt(r[a], {
      modifiers: Io,
      structs: t,
      type: "function"
    }));
  const s = [];
  if (n.returns) {
    const a = Ne(n.returns), c = a.length;
    for (let u = 0; u < c; u++)
      s.push(Yt(a[u], {
        modifiers: Io,
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
function Pm(e, t = {}) {
  const n = rm(e);
  if (!n)
    throw new _n({ signature: e, type: "event" });
  const r = Ne(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(Yt(r[s], {
      modifiers: pm,
      structs: t,
      type: "event"
    }));
  return { name: n.name, type: "event", inputs: i };
}
function Tm(e, t = {}) {
  const n = tm(e);
  if (!n)
    throw new _n({ signature: e, type: "error" });
  const r = Ne(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(Yt(r[s], { structs: t, type: "error" }));
  return { name: n.name, type: "error", inputs: i };
}
function Bm(e, t = {}) {
  const n = cm(e);
  if (!n)
    throw new _n({ signature: e, type: "constructor" });
  const r = Ne(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(Yt(r[s], { structs: t, type: "constructor" }));
  return {
    type: "constructor",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i
  };
}
function $m(e) {
  const t = lm(e);
  if (!t)
    throw new _n({ signature: e, type: "fallback" });
  return {
    type: "fallback",
    stateMutability: t.stateMutability ?? "nonpayable"
  };
}
const Om = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, Rm = /^\((?<type>.+?)\)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, Lm = /^u?int$/;
function Yt(e, t) {
  const n = Am(e, t?.type, t?.structs);
  if (Yi.has(n))
    return Yi.get(n);
  const r = Cl.test(e), i = bt(r ? Rm : Om, e);
  if (!i)
    throw new wm({ param: e });
  if (i.name && zm(i.name))
    throw new gm({ param: e, name: i.name });
  const o = i.name ? { name: i.name } : {}, s = i.modifier === "indexed" ? { indexed: !0 } : {}, a = t?.structs ?? {};
  let c, u = {};
  if (r) {
    c = "tuple";
    const d = Ne(i.type), h = [], f = d.length;
    for (let g = 0; g < f; g++)
      h.push(Yt(d[g], { structs: a }));
    u = { components: h };
  } else if (i.type in a)
    c = "tuple", u = { components: a[i.type] };
  else if (Lm.test(i.type))
    c = `${i.type}256`;
  else if (i.type === "address payable")
    c = "address";
  else if (c = i.type, t?.type !== "struct" && !Dl(c))
    throw new ym({ type: c });
  if (i.modifier) {
    if (!t?.modifiers?.has?.(i.modifier))
      throw new mm({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
    if (Io.has(i.modifier) && !Um(c, !!i.array))
      throw new bm({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
  }
  const l = {
    type: `${c}${i.array ?? ""}`,
    ...o,
    ...s,
    ...u
  };
  return Yi.set(n, l), l;
}
function Ne(e, t = [], n = "", r = 0) {
  const i = e.trim().length;
  for (let o = 0; o < i; o++) {
    const s = e[o], a = e.slice(o + 1);
    switch (s) {
      case ",":
        return r === 0 ? Ne(a, [...t, n.trim()]) : Ne(a, t, `${n}${s}`, r);
      case "(":
        return Ne(a, t, `${n}${s}`, r + 1);
      case ")":
        return Ne(a, t, `${n}${s}`, r - 1);
      default:
        return Ne(a, t, `${n}${s}`, r);
    }
  }
  if (n === "")
    return t;
  if (r !== 0)
    throw new Sm({ current: n, depth: r });
  return t.push(n.trim()), t;
}
function Dl(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || Rl.test(e) || Ll.test(e);
}
const Cm = /^(?:after|alias|anonymous|apply|auto|byte|calldata|case|catch|constant|copyof|default|defined|error|event|external|false|final|function|immutable|implements|in|indexed|inline|internal|let|mapping|match|memory|mutable|null|of|override|partial|private|promise|public|pure|reference|relocatable|return|returns|sizeof|static|storage|struct|super|supports|switch|this|true|try|typedef|typeof|var|view|virtual)$/;
function zm(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || e === "tuple" || Rl.test(e) || Ll.test(e) || Cm.test(e);
}
function Um(e, t) {
  return t || e === "bytes" || e === "string" || e === "tuple";
}
function Nm(e) {
  const t = {}, n = e.length;
  for (let s = 0; s < n; s++) {
    const a = e[s];
    if (!Hl(a))
      continue;
    const c = sm(a);
    if (!c)
      throw new _n({ signature: a, type: "struct" });
    const u = c.properties.split(";"), l = [], d = u.length;
    for (let h = 0; h < d; h++) {
      const g = u[h].trim();
      if (!g)
        continue;
      const m = Yt(g, {
        type: "struct"
      });
      l.push(m);
    }
    if (!l.length)
      throw new km({ signature: a });
    t[c.name] = l;
  }
  const r = {}, i = Object.entries(t), o = i.length;
  for (let s = 0; s < o; s++) {
    const [a, c] = i[s];
    r[a] = jl(c, t);
  }
  return r;
}
const Mm = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*)(?<array>(?:\[\d*?\])+?)?$/;
function jl(e = [], t = {}, n = /* @__PURE__ */ new Set()) {
  const r = [], i = e.length;
  for (let o = 0; o < i; o++) {
    const s = e[o];
    if (Cl.test(s.type))
      r.push(s);
    else {
      const c = bt(Mm, s.type);
      if (!c?.type)
        throw new vm({ abiParameter: s });
      const { array: u, type: l } = c;
      if (l in t) {
        if (n.has(l))
          throw new Em({ type: l });
        r.push({
          ...s,
          type: `tuple${u ?? ""}`,
          components: jl(t[l], t, /* @__PURE__ */ new Set([...n, l]))
        });
      } else if (Dl(l))
        r.push(s);
      else
        throw new hm({ type: l });
    }
  }
  return r;
}
function Hm(e) {
  const t = Nm(e), n = [], r = e.length;
  for (let i = 0; i < r; i++) {
    const o = e[i];
    Hl(o) || n.push(_m(o, t));
  }
  return n;
}
function Vl(e, { includeName: t = !1 } = {}) {
  return e ? e.map((n) => Fm(n, { includeName: t })).join(t ? ", " : ",") : "";
}
function Fm(e, { includeName: t }) {
  return e.type.startsWith("tuple") ? `(${Vl(e.components, { includeName: t })})${e.type.slice(5)}` : e.type + (t && e.name ? ` ${e.name}` : "");
}
function fr(e, { strict: t = !0 } = {}) {
  return !e || typeof e != "string" ? !1 : t ? /^0x[0-9a-fA-F]*$/.test(e) : e.startsWith("0x");
}
function le(e) {
  return fr(e, { strict: !1 }) ? Math.ceil((e.length - 2) / 2) : e.length;
}
const ql = "2.54.6";
let Ji = {
  getDocsUrl: ({ docsBaseUrl: e, docsPath: t = "", docsSlug: n }) => t ? `${e ?? "https://viem.sh"}${t}${n ? `#${n}` : ""}` : void 0,
  version: `viem@${ql}`
};
class D extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof D ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof D && n.cause.docsPath || n.docsPath, o = Ji.getDocsUrl?.({ ...n, docsPath: i }), s = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...o ? [`Docs: ${o}`] : [],
      ...r ? [`Details: ${r}`] : [],
      ...Ji.version ? [`Version: ${Ji.version}`] : []
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
    }), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.name = n.name ?? this.name, this.shortMessage = t, this.version = ql;
  }
  walk(t) {
    return Gl(this, t);
  }
}
function Gl(e, t) {
  return t?.(e) ? e : e && typeof e == "object" && "cause" in e && e.cause !== void 0 ? Gl(e.cause, t) : t ? null : e;
}
class Km extends D {
  constructor({ data: t, params: n, size: r }) {
    super([`Data size of ${r} bytes is too small for given parameters.`].join(`
`), {
      metaMessages: [
        `Params: (${Vl(n, { includeName: !0 })})`,
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
class Dm extends D {
  constructor({ cause: t } = {}) {
    super('Cannot decode zero data ("0x") with ABI parameters.', {
      name: "AbiDecodingZeroDataError",
      cause: t
    });
  }
}
class jm extends D {
  constructor({ expectedLength: t, givenLength: n, type: r }) {
    super([
      `ABI encoding array length mismatch for type ${r}.`,
      `Expected length: ${t}`,
      `Given length: ${n}`
    ].join(`
`), { name: "AbiEncodingArrayLengthMismatchError" });
  }
}
class Vm extends D {
  constructor({ expectedSize: t, value: n }) {
    super(`Size of bytes "${n}" (bytes${le(n)}) does not match expected size (bytes${t}).`, { name: "AbiEncodingBytesSizeMismatchError" });
  }
}
class qm extends D {
  constructor({ expectedLength: t, givenLength: n }) {
    super([
      "ABI encoding params/values length mismatch.",
      `Expected length (params): ${t}`,
      `Given length (values): ${n}`
    ].join(`
`), { name: "AbiEncodingLengthMismatchError" });
  }
}
class Gm extends D {
  constructor({ expectedSize: t, givenSize: n }) {
    super(`Expected bytes${t}, got bytes${n}.`, {
      name: "BytesSizeMismatchError"
    });
  }
}
class Wm extends D {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid encoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiEncodingType" });
  }
}
class Zm extends D {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid decoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiDecodingType" });
  }
}
class Xm extends D {
  constructor(t) {
    super([`Value "${t}" is not a valid array.`].join(`
`), {
      name: "InvalidArrayError"
    });
  }
}
class Wl extends D {
  constructor({ offset: t, position: n, size: r }) {
    super(`Slice ${n === "start" ? "starting" : "ending"} at offset "${t}" is out-of-bounds (size: ${r}).`, { name: "SliceOffsetOutOfBoundsError" });
  }
}
class Zl extends D {
  constructor({ size: t, targetSize: n, type: r }) {
    super(`${r.charAt(0).toUpperCase()}${r.slice(1).toLowerCase()} size (${t}) exceeds padding size (${n}).`, { name: "SizeExceedsPaddingSizeError" });
  }
}
function In(e, { dir: t, size: n = 32 } = {}) {
  return typeof e == "string" ? Lt(e, { dir: t, size: n }) : Ym(e, { dir: t, size: n });
}
function Lt(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  const r = e.replace("0x", "");
  if (r.length > n * 2)
    throw new Zl({
      size: Math.ceil(r.length / 2),
      targetSize: n,
      type: "hex"
    });
  return `0x${r[t === "right" ? "padEnd" : "padStart"](n * 2, "0")}`;
}
function Ym(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  if (e.length > n)
    throw new Zl({
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
class Cs extends D {
  constructor({ max: t, min: n, signed: r, size: i, value: o }) {
    super(`Number "${o}" is not in safe ${i ? `${i * 8}-bit ${r ? "signed" : "unsigned"} ` : ""}integer range ${t ? `(${n} to ${t})` : `(above ${n})`}`, { name: "IntegerOutOfRangeError" });
  }
}
class Jm extends D {
  constructor(t) {
    super(`Bytes value "${t}" is not a valid boolean. The bytes array must contain a single byte of either a 0 or 1 value.`, {
      name: "InvalidBytesBooleanError"
    });
  }
}
class Qm extends D {
  constructor({ givenSize: t, maxSize: n }) {
    super(`Size cannot exceed ${n} bytes. Given size: ${t} bytes.`, { name: "SizeOverflowError" });
  }
}
function ir(e, { dir: t = "left" } = {}) {
  let n = typeof e == "string" ? e.replace("0x", "") : e, r = 0;
  for (let i = 0; i < n.length - 1 && n[t === "left" ? i : n.length - i - 1].toString() === "0"; i++)
    r++;
  return n = t === "left" ? n.slice(r) : n.slice(0, n.length - r), typeof e == "string" ? (n.length === 1 && t === "right" && (n = `${n}0`), `0x${n.length % 2 === 1 ? `0${n}` : n}`) : n;
}
function st(e, { size: t }) {
  if (le(e) > t)
    throw new Qm({
      givenSize: le(e),
      maxSize: t
    });
}
function Xl(e, t = {}) {
  const { signed: n } = t;
  t.size && st(e, { size: t.size });
  const r = BigInt(e);
  if (!n)
    return r;
  const i = (e.length - 2) / 2, o = (1n << BigInt(i) * 8n - 1n) - 1n;
  return r <= o ? r : r - BigInt(`0x${"f".padStart(i * 2, "f")}`) - 1n;
}
function zs(e, t = {}) {
  const n = Xl(e, t), r = Number(n);
  if (!Number.isSafeInteger(r))
    throw new Cs({
      max: `${Number.MAX_SAFE_INTEGER}`,
      min: `${Number.MIN_SAFE_INTEGER}`,
      signed: t.signed,
      size: t.size,
      value: `${n}n`
    });
  return r;
}
const eb = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function or(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? K(e, t) : typeof e == "string" ? Jl(e, t) : typeof e == "boolean" ? Yl(e, t) : _e(e, t);
}
function Yl(e, t = {}) {
  const n = `0x${Number(e)}`;
  return typeof t.size == "number" ? (st(n, { size: t.size }), In(n, { size: t.size })) : n;
}
function _e(e, t = {}) {
  let n = "";
  for (let i = 0; i < e.length; i++)
    n += eb[e[i]];
  const r = `0x${n}`;
  return typeof t.size == "number" ? (st(r, { size: t.size }), In(r, { dir: "right", size: t.size })) : r;
}
function K(e, t = {}) {
  const { signed: n, size: r } = t, i = BigInt(e);
  let o;
  r ? n ? o = (1n << BigInt(r) * 8n - 1n) - 1n : o = 2n ** (BigInt(r) * 8n) - 1n : typeof e == "number" && (o = BigInt(Number.MAX_SAFE_INTEGER));
  const s = typeof o == "bigint" && n ? -o - 1n : 0;
  if (o && i > o || i < s) {
    const c = typeof e == "bigint" ? "n" : "";
    throw new Cs({
      max: o ? `${o}${c}` : void 0,
      min: `${s}${c}`,
      signed: n,
      size: r,
      value: `${e}${c}`
    });
  }
  const a = `0x${(n && i < 0 ? (1n << BigInt(r * 8)) + BigInt(i) : i).toString(16)}`;
  return r ? In(a, { size: r }) : a;
}
const tb = /* @__PURE__ */ new TextEncoder();
function Jl(e, t = {}) {
  const n = tb.encode(e);
  return _e(n, t);
}
const nb = /* @__PURE__ */ new TextEncoder();
function Us(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? ib(e, t) : typeof e == "boolean" ? rb(e, t) : fr(e) ? zt(e, t) : Ql(e, t);
}
function rb(e, t = {}) {
  const n = new Uint8Array(1);
  return n[0] = Number(e), typeof t.size == "number" ? (st(n, { size: t.size }), In(n, { size: t.size })) : n;
}
const dt = {
  zero: 48,
  nine: 57,
  A: 65,
  F: 70,
  a: 97,
  f: 102
};
function tc(e) {
  if (e >= dt.zero && e <= dt.nine)
    return e - dt.zero;
  if (e >= dt.A && e <= dt.F)
    return e - (dt.A - 10);
  if (e >= dt.a && e <= dt.f)
    return e - (dt.a - 10);
}
function zt(e, t = {}) {
  let n = e;
  t.size && (st(n, { size: t.size }), n = In(n, { dir: "right", size: t.size }));
  let r = n.slice(2);
  r.length % 2 && (r = `0${r}`);
  const i = r.length / 2, o = new Uint8Array(i);
  for (let s = 0, a = 0; s < i; s++) {
    const c = tc(r.charCodeAt(a++)), u = tc(r.charCodeAt(a++));
    if (c === void 0 || u === void 0)
      throw new D(`Invalid byte sequence ("${r[a - 2]}${r[a - 1]}" in "${r}").`);
    o[s] = c * 16 + u;
  }
  return o;
}
function ib(e, t) {
  const n = K(e, t);
  return zt(n);
}
function Ql(e, t = {}) {
  const n = nb.encode(e);
  return typeof t.size == "number" ? (st(n, { size: t.size }), In(n, { dir: "right", size: t.size })) : n;
}
const Tr = /* @__PURE__ */ BigInt(2 ** 32 - 1), nc = /* @__PURE__ */ BigInt(32);
function ob(e, t = !1) {
  return t ? { h: Number(e & Tr), l: Number(e >> nc & Tr) } : { h: Number(e >> nc & Tr) | 0, l: Number(e & Tr) | 0 };
}
function sb(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = ob(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const ab = (e, t, n) => e << n | t >>> 32 - n, cb = (e, t, n) => t << n | e >>> 32 - n, ub = (e, t, n) => t << n - 32 | e >>> 64 - n, lb = (e, t, n) => e << n - 32 | t >>> 64 - n;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function db(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function rc(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function sr(e, ...t) {
  if (!db(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function yi(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function ed(e, t) {
  sr(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function fb(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function ar(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Qi(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Ze(e, t) {
  return e << 32 - t | e >>> t;
}
const pb = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function hb(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function yb(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = hb(e[t]);
  return e;
}
const ic = pb ? (e) => e : yb;
function wb(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function Ns(e) {
  return typeof e == "string" && (e = wb(e)), sr(e), e;
}
class td {
}
function nd(e) {
  const t = (r) => e().update(Ns(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
const gb = BigInt(0), Cn = BigInt(1), mb = BigInt(2), bb = BigInt(7), vb = BigInt(256), xb = BigInt(113), rd = [], id = [], od = [];
for (let e = 0, t = Cn, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], rd.push(2 * (5 * r + n)), id.push((e + 1) * (e + 2) / 2 % 64);
  let i = gb;
  for (let o = 0; o < 7; o++)
    t = (t << Cn ^ (t >> bb) * xb) % vb, t & mb && (i ^= Cn << (Cn << /* @__PURE__ */ BigInt(o)) - Cn);
  od.push(i);
}
const sd = sb(od, !0), kb = sd[0], Eb = sd[1], oc = (e, t, n) => n > 32 ? ub(e, t, n) : ab(e, t, n), sc = (e, t, n) => n > 32 ? lb(e, t, n) : cb(e, t, n);
function Sb(e, t = 24) {
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, u = n[c], l = n[c + 1], d = oc(u, l, 1) ^ n[a], h = sc(u, l, 1) ^ n[a + 1];
      for (let f = 0; f < 50; f += 10)
        e[s + f] ^= d, e[s + f + 1] ^= h;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = id[s], c = oc(i, o, a), u = sc(i, o, a), l = rd[s];
      i = e[l], o = e[l + 1], e[l] = c, e[l + 1] = u;
    }
    for (let s = 0; s < 50; s += 10) {
      for (let a = 0; a < 10; a++)
        n[a] = e[s + a];
      for (let a = 0; a < 10; a++)
        e[s + a] ^= ~n[(a + 2) % 10] & n[(a + 4) % 10];
    }
    e[0] ^= kb[r], e[1] ^= Eb[r];
  }
  ar(n);
}
class Ms extends td {
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (super(), this.pos = 0, this.posOut = 0, this.finished = !1, this.destroyed = !1, this.enableXOF = !1, this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.rounds = o, rc(r), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = fb(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    ic(this.state32), Sb(this.state32, this.rounds), ic(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    yi(this), t = Ns(t), sr(t);
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
    yi(this, !1), sr(t), this.finish();
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
    return rc(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (ed(t, this), this.finished)
      throw new Error("digest() was already called");
    return this.writeInto(t), this.destroy(), t;
  }
  digest() {
    return this.digestInto(new Uint8Array(this.outputLen));
  }
  destroy() {
    this.destroyed = !0, ar(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t || (t = new Ms(n, r, i, s, o)), t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.destroyed = this.destroyed, t;
  }
}
const Ab = (e, t, n) => nd(() => new Ms(t, e, n)), _b = Ab(1, 136, 256 / 8);
function et(e, t) {
  const n = t || "hex", r = _b(fr(e, { strict: !1 }) ? Us(e) : e);
  return n === "bytes" ? r : or(r);
}
const Ib = (e) => et(Us(e));
function Pb(e) {
  return Ib(e);
}
function Tb(e) {
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
    throw new D("Unable to normalize signature.");
  return i;
}
const Bb = (e) => {
  const t = typeof e == "string" ? e : Qg(e);
  return Tb(t);
};
function $b(e) {
  return Pb(Bb(e));
}
class Qt extends D {
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
class ad extends Map {
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
const eo = /* @__PURE__ */ new ad(8192);
function cd(e, t) {
  if (eo.has(`${e}.${t}`))
    return eo.get(`${e}.${t}`);
  const n = e.substring(2).toLowerCase(), r = et(Ql(n), "bytes"), i = n.split("");
  for (let s = 0; s < 40; s += 2)
    r[s >> 1] >> 4 >= 8 && i[s] && (i[s] = i[s].toUpperCase()), (r[s >> 1] & 15) >= 8 && i[s + 1] && (i[s + 1] = i[s + 1].toUpperCase());
  const o = `0x${i.join("")}`;
  return eo.set(`${e}.${t}`, o), o;
}
const Ob = /^0x[a-fA-F0-9]{40}$/, to = /* @__PURE__ */ new ad(8192);
function en(e, t) {
  const { strict: n = !0 } = t ?? {}, r = `${e}.${n}`;
  if (to.has(r))
    return to.get(r);
  const i = Ob.test(e) ? e.toLowerCase() === e ? !0 : n ? cd(e) === e : !0 : !1;
  return to.set(r, i), i;
}
function Rb(e) {
  return typeof e[0] == "string" ? De(e) : Lb(e);
}
function Lb(e) {
  let t = 0;
  for (const i of e)
    t += i.length;
  const n = new Uint8Array(t);
  let r = 0;
  for (const i of e)
    n.set(i, r), r += i.length;
  return n;
}
function De(e) {
  return `0x${e.reduce((t, n) => t + n.replace("0x", ""), "")}`;
}
function Hs(e, t, n, { strict: r } = {}) {
  return fr(e, { strict: !1 }) ? fd(e, t, n, {
    strict: r
  }) : dd(e, t, n, {
    strict: r
  });
}
function ud(e, t) {
  if (typeof t == "number" && t > 0 && t > le(e) - 1)
    throw new Wl({
      offset: t,
      position: "start",
      size: le(e)
    });
}
function ld(e, t, n) {
  if (typeof t == "number" && typeof n == "number" && le(e) !== n - t)
    throw new Wl({
      offset: n,
      position: "end",
      size: le(e)
    });
}
function dd(e, t, n, { strict: r } = {}) {
  ud(e, t);
  const i = e.slice(t, n);
  return r && ld(i, t, n), i;
}
function fd(e, t, n, { strict: r } = {}) {
  ud(e, t);
  const i = `0x${e.replace("0x", "").slice((t ?? 0) * 2, (n ?? e.length) * 2)}`;
  return r && ld(i, t, n), i;
}
const Cb = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, pd = /^(u?int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/;
function Fs(e, t) {
  if (e.length !== t.length)
    throw new qm({
      expectedLength: e.length,
      givenLength: t.length
    });
  const n = zb({
    params: e,
    values: t
  });
  return Ds(n);
}
function zb({ params: e, values: t }) {
  const n = [];
  for (let r = 0; r < e.length; r++)
    n.push(Ks({ param: e[r], value: t[r] }));
  return n;
}
function Ks({ param: e, value: t }) {
  const n = $i(e.type);
  if (n) {
    const [r, i] = n;
    return Nb(t, { length: r, param: { ...e, type: i } });
  }
  if (e.type === "tuple")
    return Db(t, {
      param: e
    });
  if (e.type === "address")
    return Ub(t);
  if (e.type === "bool")
    return Hb(t);
  if (e.type.startsWith("uint") || e.type.startsWith("int")) {
    const r = e.type.startsWith("int"), [, , i = "256"] = pd.exec(e.type) ?? [];
    return Fb(t, {
      signed: r,
      size: Number(i)
    });
  }
  if (e.type.startsWith("bytes"))
    return Mb(t, { param: e });
  if (e.type === "string")
    return Kb(t);
  throw new Wm(e.type, {
    docsPath: "/docs/contract/encodeAbiParameters"
  });
}
function Ds(e) {
  let t = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? t += 32 : t += le(a);
  }
  const n = [], r = [];
  let i = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? (n.push(K(t + i, { size: 32 })), r.push(a), i += le(a)) : n.push(a);
  }
  return De([...n, ...r]);
}
function Ub(e) {
  if (!en(e))
    throw new Qt({ address: e });
  return { dynamic: !1, encoded: Lt(e.toLowerCase()) };
}
function Nb(e, { length: t, param: n }) {
  const r = t === null;
  if (!Array.isArray(e))
    throw new Xm(e);
  if (!r && e.length !== t)
    throw new jm({
      expectedLength: t,
      givenLength: e.length,
      type: `${n.type}[${t}]`
    });
  let i = e.length === 0 && Po(n);
  const o = [];
  for (let s = 0; s < e.length; s++) {
    const a = Ks({ param: n, value: e[s] });
    a.dynamic && (i = !0), o.push(a);
  }
  if (r || i) {
    const s = Ds(o);
    if (r) {
      const a = K(o.length, { size: 32 });
      return {
        dynamic: !0,
        encoded: De([a, s])
      };
    }
    if (i)
      return { dynamic: !0, encoded: s };
  }
  return {
    dynamic: !1,
    encoded: De(o.map(({ encoded: s }) => s))
  };
}
function Mb(e, { param: t }) {
  const [, n] = t.type.split("bytes"), r = le(e);
  if (!n) {
    let i = e;
    return r % 32 !== 0 && (i = Lt(i, {
      dir: "right",
      size: Math.ceil((e.length - 2) / 2 / 32) * 32
    })), {
      dynamic: !0,
      encoded: De([
        Lt(K(r, { size: 32 })),
        i
      ])
    };
  }
  if (r !== Number.parseInt(n, 10))
    throw new Vm({
      expectedSize: Number.parseInt(n, 10),
      value: e
    });
  return { dynamic: !1, encoded: Lt(e, { dir: "right" }) };
}
function Hb(e) {
  if (typeof e != "boolean")
    throw new D(`Invalid boolean value: "${e}" (type: ${typeof e}). Expected: \`true\` or \`false\`.`);
  return { dynamic: !1, encoded: Lt(Yl(e)) };
}
function Fb(e, { signed: t, size: n = 256 }) {
  if (typeof n == "number") {
    const r = 2n ** (BigInt(n) - (t ? 1n : 0n)) - 1n, i = t ? -r - 1n : 0n;
    if (e > r || e < i)
      throw new Cs({
        max: r.toString(),
        min: i.toString(),
        signed: t,
        size: n / 8,
        value: e.toString()
      });
  }
  return {
    dynamic: !1,
    encoded: K(e, {
      size: 32,
      signed: t
    })
  };
}
function Kb(e) {
  const t = Jl(e), n = Math.ceil(le(t) / 32), r = [];
  for (let i = 0; i < n; i++)
    r.push(Lt(Hs(t, i * 32, (i + 1) * 32), {
      dir: "right"
    }));
  return {
    dynamic: !0,
    encoded: De([
      Lt(K(le(t), { size: 32 })),
      ...r
    ])
  };
}
function Db(e, { param: t }) {
  let n = !1;
  const r = [];
  for (let i = 0; i < t.components.length; i++) {
    const o = t.components[i], s = Array.isArray(e) ? i : o.name, a = Ks({
      param: o,
      value: e[s]
    });
    r.push(a), a.dynamic && (n = !0);
  }
  return {
    dynamic: n,
    encoded: n ? Ds(r) : De(r.map(({ encoded: i }) => i))
  };
}
function $i(e) {
  const t = e.match(/^(.*)\[(\d+)?\]$/);
  return t ? (
    // Return `null` if the array is dynamic.
    [t[2] ? Number(t[2]) : null, t[1]]
  ) : void 0;
}
function Po(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components.some(Po);
  const n = $i(t);
  return n ? Po({ ...e, type: n[1] }) : !1;
}
const jb = (e) => Hs($b(e), 0, 4);
class ac extends D {
  constructor({ offset: t }) {
    super(`Offset \`${t}\` cannot be negative.`, {
      name: "NegativeOffsetError"
    });
  }
}
class Vb extends D {
  constructor({ length: t, position: n }) {
    super(`Position \`${n}\` is out of bounds (\`0 < position < ${t}\`).`, { name: "PositionOutOfBoundsError" });
  }
}
class qb extends D {
  constructor({ count: t, limit: n }) {
    super(`Recursive read limit of \`${n}\` exceeded (recursive read count: \`${t}\`).`, { name: "RecursiveReadLimitExceededError" });
  }
}
const Gb = {
  bytes: new Uint8Array(),
  dataView: new DataView(new ArrayBuffer(0)),
  position: 0,
  positionReadCount: /* @__PURE__ */ new Map(),
  recursiveReadCount: 0,
  recursiveReadLimit: Number.POSITIVE_INFINITY,
  assertReadLimit() {
    if (this.recursiveReadCount >= this.recursiveReadLimit)
      throw new qb({
        count: this.recursiveReadCount + 1,
        limit: this.recursiveReadLimit
      });
  },
  assertPosition(e) {
    if (e < 0 || e > this.bytes.length - 1)
      throw new Vb({
        length: this.bytes.length,
        position: e
      });
  },
  decrementPosition(e) {
    if (e < 0)
      throw new ac({ offset: e });
    const t = this.position - e;
    this.assertPosition(t), this.position = t;
  },
  getReadCount(e) {
    return this.positionReadCount.get(e || this.position) || 0;
  },
  incrementPosition(e) {
    if (e < 0)
      throw new ac({ offset: e });
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
function js(e, { recursiveReadLimit: t = 8192 } = {}) {
  const n = Object.create(Gb);
  return n.bytes = e, n.dataView = new DataView(e.buffer ?? e, e.byteOffset, e.byteLength), n.positionReadCount = /* @__PURE__ */ new Map(), n.recursiveReadLimit = t, n;
}
function Wb(e, t = {}) {
  typeof t.size < "u" && st(e, { size: t.size });
  const n = _e(e, t);
  return Xl(n, t);
}
function Zb(e, t = {}) {
  let n = e;
  if (typeof t.size < "u" && (st(n, { size: t.size }), n = ir(n)), n.length > 1 || n[0] > 1)
    throw new Jm(n);
  return !!n[0];
}
function ht(e, t = {}) {
  typeof t.size < "u" && st(e, { size: t.size });
  const n = _e(e, t);
  return zs(n, t);
}
function Xb(e, t = {}) {
  let n = e;
  return typeof t.size < "u" && (st(n, { size: t.size }), n = ir(n, { dir: "right" })), new TextDecoder().decode(n);
}
function Yb(e, t) {
  const n = typeof t == "string" ? zt(t) : t, r = js(n);
  if (le(n) === 0 && e.length > 0)
    throw new Dm();
  if (le(t) && le(t) < 32)
    throw new Km({
      data: typeof t == "string" ? t : _e(t),
      params: e,
      size: le(t)
    });
  let i = 0;
  const o = [];
  for (let s = 0; s < e.length; ++s) {
    const a = e[s];
    i < n.length && r.setPosition(i);
    const [c, u] = hn(r, a, {
      staticPosition: 0
    });
    i += u, o.push(c);
  }
  return o;
}
function hn(e, t, { staticPosition: n }) {
  const r = $i(t.type);
  if (r) {
    const [i, o] = r;
    return Qb(e, { ...t, type: o }, { length: i, staticPosition: n });
  }
  if (t.type === "tuple")
    return r0(e, t, { staticPosition: n });
  if (t.type === "address")
    return Jb(e);
  if (t.type === "bool")
    return e0(e);
  if (t.type.startsWith("bytes"))
    return t0(e, t, { staticPosition: n });
  if (t.type.startsWith("uint") || t.type.startsWith("int"))
    return n0(e, t);
  if (t.type === "string")
    return i0(e, { staticPosition: n });
  throw new Zm(t.type, {
    docsPath: "/docs/contract/decodeAbiParameters"
  });
}
const cc = 32, To = 32;
function Jb(e) {
  const t = e.readBytes(32);
  return [cd(_e(dd(t, -20))), 32];
}
function Qb(e, t, { length: n, staticPosition: r }) {
  if (n === null) {
    const s = ht(e.readBytes(To)), a = r + s, c = a + cc;
    e.setPosition(a);
    const u = ht(e.readBytes(cc)), l = cr(t);
    let d = 0;
    const h = [];
    for (let f = 0; f < u; ++f) {
      e.setPosition(c + (l ? f * 32 : d));
      const [g, m] = hn(e, t, {
        staticPosition: c
      });
      d += m, h.push(g), m === 0 && (e.assertReadLimit(), e._touch());
    }
    return e.setPosition(r + 32), [h, 32];
  }
  if (cr(t)) {
    const s = ht(e.readBytes(To)), a = r + s, c = [];
    for (let u = 0; u < n; ++u) {
      e.setPosition(a + u * 32);
      const [l] = hn(e, t, {
        staticPosition: a
      });
      c.push(l);
    }
    return e.setPosition(r + 32), [c, 32];
  }
  let i = 0;
  const o = [];
  for (let s = 0; s < n; ++s) {
    const [a, c] = hn(e, t, {
      staticPosition: r + i
    });
    i += c, o.push(a), c === 0 && (e.assertReadLimit(), e._touch());
  }
  return [o, i];
}
function e0(e) {
  return [Zb(e.readBytes(32), { size: 32 }), 32];
}
function t0(e, t, { staticPosition: n }) {
  const [r, i] = t.type.split("bytes");
  if (!i) {
    const s = ht(e.readBytes(32));
    e.setPosition(n + s);
    const a = ht(e.readBytes(32));
    if (a === 0)
      return e.setPosition(n + 32), ["0x", 32];
    const c = e.readBytes(a);
    return e.setPosition(n + 32), [_e(c), 32];
  }
  return [_e(e.readBytes(Number.parseInt(i, 10), 32)), 32];
}
function n0(e, t) {
  const n = t.type.startsWith("int"), r = Number.parseInt(t.type.split("int")[1] || "256", 10), i = e.readBytes(32);
  return [
    r > 48 ? Wb(i, { signed: n }) : ht(i, { signed: n }),
    32
  ];
}
function r0(e, t, { staticPosition: n }) {
  const r = t.components.length === 0 || t.components.some(({ name: s }) => !s), i = r ? [] : {};
  let o = 0;
  if (cr(t)) {
    const s = ht(e.readBytes(To)), a = n + s;
    for (let c = 0; c < t.components.length; ++c) {
      const u = t.components[c];
      e.setPosition(a + o);
      const [l, d] = hn(e, u, {
        staticPosition: a
      });
      o += d, i[r ? c : u?.name] = l;
    }
    return e.setPosition(n + 32), [i, 32];
  }
  for (let s = 0; s < t.components.length; ++s) {
    const a = t.components[s], [c, u] = hn(e, a, {
      staticPosition: n
    });
    i[r ? s : a?.name] = c, o += u;
  }
  return [i, o];
}
function i0(e, { staticPosition: t }) {
  const n = ht(e.readBytes(32)), r = t + n;
  e.setPosition(r);
  const i = ht(e.readBytes(32));
  if (i === 0)
    return e.setPosition(t + 32), ["", 32];
  const o = e.readBytes(i, 32), s = Xb(ir(o));
  return e.setPosition(t + 32), [s, 32];
}
function cr(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components?.some(cr);
  const n = $i(e.type);
  return !!(n && cr({ ...e, type: n[1] }));
}
const o0 = (e, t, n) => JSON.stringify(e, (r, i) => typeof i == "bigint" ? i.toString() : i, n), s0 = {
  ether: -9,
  wei: 9
};
function a0(e, t) {
  let n = e.toString();
  const r = n.startsWith("-");
  r && (n = n.slice(1)), n = n.padStart(t, "0");
  let [i, o] = [
    n.slice(0, n.length - t),
    n.slice(n.length - t)
  ];
  return o = o.replace(/(0+)$/, ""), `${r ? "-" : ""}${i || "0"}${o ? `.${o}` : ""}`;
}
function Bo(e, t = "wei") {
  return a0(e, s0[t]);
}
function c0(e) {
  const t = Object.entries(e).map(([r, i]) => i === void 0 || i === !1 ? null : [r, i]).filter(Boolean), n = t.reduce((r, [i]) => Math.max(r, i.length), 0);
  return t.map(([r, i]) => `  ${`${r}:`.padEnd(n + 1)}  ${i}`).join(`
`);
}
class u0 extends D {
  constructor({ transaction: t }) {
    super("Cannot infer a transaction type from provided transaction.", {
      metaMessages: [
        "Provided Transaction:",
        "{",
        c0(t),
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
class l0 extends D {
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
class d0 extends D {
  constructor({ storageKey: t }) {
    super(`Size for storage key "${t}" is invalid. Expected 32 bytes. Got ${Math.floor((t.length - 2) / 2)} bytes.`, { name: "InvalidStorageKeySizeError" });
  }
}
function En(e, t = "hex") {
  const n = hd(e), r = js(new Uint8Array(n.length));
  return n.encode(r), t === "hex" ? _e(r.bytes) : r.bytes;
}
function hd(e) {
  return Array.isArray(e) ? f0(e.map((t) => hd(t))) : p0(e);
}
function f0(e) {
  const t = e.reduce((i, o) => i + o.length, 0), n = yd(t);
  return {
    length: t <= 55 ? 1 + t : 1 + n + t,
    encode(i) {
      t <= 55 ? i.pushByte(192 + t) : (i.pushByte(247 + n), n === 1 ? i.pushUint8(t) : n === 2 ? i.pushUint16(t) : n === 3 ? i.pushUint24(t) : i.pushUint32(t));
      for (const { encode: o } of e)
        o(i);
    }
  };
}
function p0(e) {
  const t = typeof e == "string" ? zt(e) : e, n = yd(t.length);
  return {
    length: t.length === 1 && t[0] < 128 ? 1 : t.length <= 55 ? 1 + t.length : 1 + n + t.length,
    encode(i) {
      t.length === 1 && t[0] < 128 ? i.pushBytes(t) : t.length <= 55 ? (i.pushByte(128 + t.length), i.pushBytes(t)) : (i.pushByte(183 + n), n === 1 ? i.pushUint8(t.length) : n === 2 ? i.pushUint16(t.length) : n === 3 ? i.pushUint24(t.length) : i.pushUint32(t.length), i.pushBytes(t));
    }
  };
}
function yd(e) {
  if (e < 2 ** 8)
    return 1;
  if (e < 2 ** 16)
    return 2;
  if (e < 2 ** 24)
    return 3;
  if (e < 2 ** 32)
    return 4;
  throw new D("Length is too large.");
}
class Oi extends D {
  constructor({ cause: t, maxFeePerGas: n } = {}) {
    super(`The fee cap (\`maxFeePerGas\`${n ? ` = ${Bo(n)} gwei` : ""}) cannot be higher than the maximum allowed value (2^256-1).`, {
      cause: t,
      name: "FeeCapTooHighError"
    });
  }
}
Object.defineProperty(Oi, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max fee per gas higher than 2\^256-1|fee cap higher than 2\^256-1/
});
class wd extends D {
  constructor({ cause: t, maxPriorityFeePerGas: n, maxFeePerGas: r } = {}) {
    super([
      `The provided tip (\`maxPriorityFeePerGas\`${n ? ` = ${Bo(n)} gwei` : ""}) cannot be higher than the fee cap (\`maxFeePerGas\`${r ? ` = ${Bo(r)} gwei` : ""}).`
    ].join(`
`), {
      cause: t,
      name: "TipAboveFeeCapError"
    });
  }
}
Object.defineProperty(wd, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max priority fee per gas higher than max fee per gas|tip higher than fee cap/
});
const Vs = 2n ** 256n - 1n;
function gd(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((o) => zt(o)) : e.blobs, i = [];
  for (const o of r)
    i.push(Uint8Array.from(t.blobToKzgCommitment(o)));
  return n === "bytes" ? i : i.map((o) => _e(o));
}
function md(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((s) => zt(s)) : e.blobs, i = typeof e.commitments[0] == "string" ? e.commitments.map((s) => zt(s)) : e.commitments, o = [];
  for (let s = 0; s < r.length; s++) {
    const a = r[s], c = i[s];
    o.push(Uint8Array.from(t.computeBlobKzgProof(a, c)));
  }
  return n === "bytes" ? o : o.map((s) => _e(s));
}
function h0(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, u = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + u, a, r);
}
function y0(e, t, n) {
  return e & t ^ ~e & n;
}
function w0(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
class g0 extends td {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Qi(this.buffer);
  }
  update(t) {
    yi(this), t = Ns(t), sr(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Qi(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    yi(this), ed(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, ar(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    h0(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Qi(t), c = this.outputLen;
    if (c % 4)
      throw new Error("_sha2: outputLen should be aligned to 32bit");
    const u = c / 4, l = this.get();
    if (u > l.length)
      throw new Error("_sha2: outputLen bigger than state");
    for (let d = 0; d < u; d++)
      a.setUint32(4 * d, l[d], o);
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
const Pt = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), m0 = /* @__PURE__ */ Uint32Array.from([
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
]), Tt = /* @__PURE__ */ new Uint32Array(64);
class b0 extends g0 {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = Pt[0] | 0, this.B = Pt[1] | 0, this.C = Pt[2] | 0, this.D = Pt[3] | 0, this.E = Pt[4] | 0, this.F = Pt[5] | 0, this.G = Pt[6] | 0, this.H = Pt[7] | 0;
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
      Tt[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = Tt[d - 15], f = Tt[d - 2], g = Ze(h, 7) ^ Ze(h, 18) ^ h >>> 3, m = Ze(f, 17) ^ Ze(f, 19) ^ f >>> 10;
      Tt[d] = m + Tt[d - 7] + g + Tt[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: u, H: l } = this;
    for (let d = 0; d < 64; d++) {
      const h = Ze(a, 6) ^ Ze(a, 11) ^ Ze(a, 25), f = l + h + y0(a, c, u) + m0[d] + Tt[d] | 0, m = (Ze(r, 2) ^ Ze(r, 13) ^ Ze(r, 22)) + w0(r, i, o) | 0;
      l = u, u = c, c = a, a = s + f | 0, s = o, o = i, i = r, r = f + m | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, u = u + this.G | 0, l = l + this.H | 0, this.set(r, i, o, s, a, c, u, l);
  }
  roundClean() {
    ar(Tt);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), ar(this.buffer);
  }
}
const v0 = /* @__PURE__ */ nd(() => new b0()), x0 = v0;
function k0(e, t) {
  return x0(fr(e, { strict: !1 }) ? Us(e) : e);
}
function E0(e) {
  const { commitment: t, version: n = 1 } = e, r = e.to ?? (typeof t == "string" ? "hex" : "bytes"), i = k0(t);
  return i.set([n], 0), r === "bytes" ? i : _e(i);
}
function S0(e) {
  const { commitments: t, version: n } = e, r = e.to ?? (typeof t[0] == "string" ? "hex" : "bytes"), i = [];
  for (const o of t)
    i.push(E0({
      commitment: o,
      to: r,
      version: n
    }));
  return i;
}
const uc = 6, bd = 32, qs = 4096, vd = bd * qs, lc = vd * uc - // terminator byte (0x80).
1 - // zero byte (0x00) appended to each field element.
1 * qs * uc, xd = 1;
class A0 extends D {
  constructor({ maxSize: t, size: n }) {
    super("Blob size is too large.", {
      metaMessages: [`Max: ${t} bytes`, `Given: ${n} bytes`],
      name: "BlobSizeTooLargeError"
    });
  }
}
class kd extends D {
  constructor() {
    super("Blob data must not be empty.", { name: "EmptyBlobError" });
  }
}
class _0 extends D {
  constructor({ hash: t, size: n }) {
    super(`Versioned hash "${t}" size is invalid.`, {
      metaMessages: ["Expected: 32", `Received: ${n}`],
      name: "InvalidVersionedHashSizeError"
    });
  }
}
class I0 extends D {
  constructor({ hash: t, version: n }) {
    super(`Versioned hash "${t}" version is invalid.`, {
      metaMessages: [
        `Expected: ${xd}`,
        `Received: ${n}`
      ],
      name: "InvalidVersionedHashVersionError"
    });
  }
}
function P0(e) {
  const t = e.to ?? (typeof e.data == "string" ? "hex" : "bytes"), n = typeof e.data == "string" ? zt(e.data) : e.data, r = le(n);
  if (!r)
    throw new kd();
  if (r > lc)
    throw new A0({
      maxSize: lc,
      size: r
    });
  const i = [];
  let o = !0, s = 0;
  for (; o; ) {
    const a = js(new Uint8Array(vd));
    let c = 0;
    for (; c < qs; ) {
      const u = n.slice(s, s + (bd - 1));
      if (a.pushByte(0), a.pushBytes(u), u.length < 31) {
        a.pushByte(128), o = !1;
        break;
      }
      c++, s += 31;
    }
    i.push(a);
  }
  return t === "bytes" ? i.map((a) => a.bytes) : i.map((a) => _e(a.bytes));
}
function T0(e) {
  const { data: t, kzg: n, to: r } = e, i = e.blobs ?? P0({ data: t, to: r }), o = e.commitments ?? gd({ blobs: i, kzg: n, to: r }), s = e.proofs ?? md({ blobs: i, commitments: o, kzg: n, to: r }), a = [];
  for (let c = 0; c < i.length; c++)
    a.push({
      blob: i[c],
      commitment: o[c],
      proof: s[c]
    });
  return a;
}
function B0(e) {
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
  throw new u0({ transaction: e });
}
class Ri extends D {
  constructor({ chainId: t }) {
    super(typeof t == "number" ? `Chain ID "${t}" is invalid.` : "Chain ID is invalid.", { name: "InvalidChainIdError" });
  }
}
function $0(e) {
  const { authorizationList: t } = e;
  if (t)
    for (const n of t) {
      const { chainId: r } = n, i = n.address;
      if (!en(i))
        throw new Qt({ address: i });
      if (r < 0)
        throw new Ri({ chainId: r });
    }
  Gs(e);
}
function O0(e) {
  const { blobVersionedHashes: t } = e;
  if (t) {
    if (t.length === 0)
      throw new kd();
    for (const n of t) {
      const r = le(n), i = zs(Hs(n, 0, 1));
      if (r !== 32)
        throw new _0({ hash: n, size: r });
      if (i !== xd)
        throw new I0({
          hash: n,
          version: i
        });
    }
  }
  Gs(e);
}
function Gs(e) {
  const { chainId: t, maxPriorityFeePerGas: n, maxFeePerGas: r, to: i } = e;
  if (t <= 0)
    throw new Ri({ chainId: t });
  if (i && !en(i))
    throw new Qt({ address: i });
  if (r && r > Vs)
    throw new Oi({ maxFeePerGas: r });
  if (n && r && n > r)
    throw new wd({ maxFeePerGas: r, maxPriorityFeePerGas: n });
}
function R0(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (t <= 0)
    throw new Ri({ chainId: t });
  if (o && !en(o))
    throw new Qt({ address: o });
  if (n || i)
    throw new D("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid EIP-2930 Transaction attribute.");
  if (r && r > Vs)
    throw new Oi({ maxFeePerGas: r });
}
function L0(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (o && !en(o))
    throw new Qt({ address: o });
  if (typeof t < "u" && t <= 0)
    throw new Ri({ chainId: t });
  if (n || i)
    throw new D("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid Legacy Transaction attribute.");
  if (r && r > Vs)
    throw new Oi({ maxFeePerGas: r });
}
function Li(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (let n = 0; n < e.length; n++) {
    const { address: r, storageKeys: i } = e[n];
    for (let o = 0; o < i.length; o++)
      if (i[o].length - 2 !== 64)
        throw new d0({ storageKey: i[o] });
    if (!en(r, { strict: !1 }))
      throw new Qt({ address: r });
    t.push([r, i]);
  }
  return t;
}
function C0(e, t) {
  const n = B0(e);
  return n === "eip1559" ? N0(e, t) : n === "eip2930" ? M0(e, t) : n === "eip4844" ? U0(e, t) : n === "eip7702" ? z0(e, t) : H0(e);
}
function z0(e, t) {
  const { authorizationList: n, chainId: r, gas: i, nonce: o, to: s, value: a, maxFeePerGas: c, maxPriorityFeePerGas: u, accessList: l, data: d } = e;
  $0(e);
  const h = Li(l), f = F0(n);
  return De([
    "0x04",
    En([
      K(r),
      o ? K(o) : "0x",
      u ? K(u) : "0x",
      c ? K(c) : "0x",
      i ? K(i) : "0x",
      s ?? "0x",
      a ? K(a) : "0x",
      d ?? "0x",
      h,
      f,
      ...pr(e, t)
    ])
  ]);
}
function U0(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerBlobGas: a, maxFeePerGas: c, maxPriorityFeePerGas: u, accessList: l, data: d } = e;
  O0(e);
  let h = e.blobVersionedHashes, f = e.sidecars;
  if (e.blobs && (typeof h > "u" || typeof f > "u")) {
    const x = typeof e.blobs[0] == "string" ? e.blobs : e.blobs.map(($) => _e($)), I = e.kzg, E = gd({
      blobs: x,
      kzg: I
    });
    if (typeof h > "u" && (h = S0({
      commitments: E
    })), typeof f > "u") {
      const $ = md({ blobs: x, commitments: E, kzg: I });
      f = T0({ blobs: x, commitments: E, proofs: $ });
    }
  }
  const g = Li(l), m = [
    K(n),
    i ? K(i) : "0x",
    u ? K(u) : "0x",
    c ? K(c) : "0x",
    r ? K(r) : "0x",
    o ?? "0x",
    s ? K(s) : "0x",
    d ?? "0x",
    g,
    a ? K(a) : "0x",
    h ?? [],
    ...pr(e, t)
  ], p = [], y = [], v = [];
  if (f)
    for (let x = 0; x < f.length; x++) {
      const { blob: I, commitment: E, proof: $ } = f[x];
      p.push(I), y.push(E), v.push($);
    }
  return De([
    "0x03",
    // If sidecars are enabled, envelope turns into a "wrapper":
    En(f ? [m, p, y, v] : m)
  ]);
}
function N0(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerGas: a, maxPriorityFeePerGas: c, accessList: u, data: l } = e;
  Gs(e);
  const d = Li(u), h = [
    K(n),
    i ? K(i) : "0x",
    c ? K(c) : "0x",
    a ? K(a) : "0x",
    r ? K(r) : "0x",
    o ?? "0x",
    s ? K(s) : "0x",
    l ?? "0x",
    d,
    ...pr(e, t)
  ];
  return De([
    "0x02",
    En(h)
  ]);
}
function M0(e, t) {
  const { chainId: n, gas: r, data: i, nonce: o, to: s, value: a, accessList: c, gasPrice: u } = e;
  R0(e);
  const l = Li(c), d = [
    K(n),
    o ? K(o) : "0x",
    u ? K(u) : "0x",
    r ? K(r) : "0x",
    s ?? "0x",
    a ? K(a) : "0x",
    i ?? "0x",
    l,
    ...pr(e, t)
  ];
  return De([
    "0x01",
    En(d)
  ]);
}
function H0(e, t) {
  const { chainId: n = 0, gas: r, data: i, nonce: o, to: s, value: a, gasPrice: c } = e;
  L0(e);
  let u = [
    o ? K(o) : "0x",
    c ? K(c) : "0x",
    r ? K(r) : "0x",
    s ?? "0x",
    a ? K(a) : "0x",
    i ?? "0x"
  ];
  return n > 0 && (u = [
    ...u,
    K(n),
    "0x",
    "0x"
  ]), En(u);
}
function pr(e, t) {
  const n = t ?? e, { v: r, yParity: i } = n;
  if (typeof n.r > "u")
    return [];
  if (typeof n.s > "u")
    return [];
  if (typeof r > "u" && typeof i > "u")
    return [];
  const o = ir(n.r), s = ir(n.s);
  return [typeof i == "number" ? i ? K(1) : "0x" : r === 0n ? "0x" : r === 1n ? K(1) : r === 27n ? "0x" : K(1), o === "0x00" ? "0x" : o, s === "0x00" ? "0x" : s];
}
function F0(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (const n of e) {
    const { chainId: r, nonce: i, ...o } = n, s = n.address;
    t.push([
      r ? or(r) : "0x",
      s,
      i ? or(i) : "0x",
      ...pr({}, o)
    ]);
  }
  return t;
}
class K0 extends D {
  constructor({ domain: t }) {
    super(`Invalid domain "${o0(t)}".`, {
      metaMessages: ["Must be a valid EIP-712 domain."]
    });
  }
}
class D0 extends D {
  constructor({ primaryType: t, types: n }) {
    super(`Invalid primary type \`${t}\` must be one of \`${JSON.stringify(Object.keys(n))}\`.`, {
      docsPath: "/api/glossary/Errors#typeddatainvalidprimarytypeerror",
      metaMessages: ["Check that the primary type is a key in `types`."]
    });
  }
}
class j0 extends D {
  constructor({ type: t }) {
    super(`Struct type "${t}" is invalid.`, {
      metaMessages: ["Struct type must not be a Solidity type."],
      name: "InvalidStructTypeError"
    });
  }
}
function V0(e) {
  const { domain: t, message: n, primaryType: r, types: i } = e, o = (s, a) => {
    for (const c of s) {
      const { name: u, type: l } = c, d = a[u], h = l.match(pd);
      if (h && (typeof d == "number" || typeof d == "bigint")) {
        const [m, p, y] = h;
        K(d, {
          signed: p === "int",
          size: Number.parseInt(y, 10) / 8
        });
      }
      if (l === "address" && typeof d == "string" && !en(d))
        throw new Qt({ address: d });
      const f = l.match(Cb);
      if (f) {
        const [m, p] = f;
        if (p && le(d) !== Number.parseInt(p, 10))
          throw new Gm({
            expectedSize: Number.parseInt(p, 10),
            givenSize: le(d)
          });
      }
      const g = i[l];
      g && (q0(l), o(g, d));
    }
  };
  if (i.EIP712Domain && t) {
    if (typeof t != "object")
      throw new K0({ domain: t });
    o(i.EIP712Domain, t);
  }
  if (r !== "EIP712Domain")
    if (i[r])
      o(i[r], n);
    else
      throw new D0({ primaryType: r, types: i });
}
function Ed({ domain: e }) {
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
function q0(e) {
  if (e === "address" || e === "bool" || e === "string" || e.startsWith("bytes") || e.startsWith("uint") || e.startsWith("int"))
    throw new j0({ type: e });
}
function G0(e) {
  const { domain: t = {}, message: n, primaryType: r } = e, i = {
    EIP712Domain: Ed({ domain: t }),
    ...e.types
  };
  V0({
    domain: t,
    message: n,
    primaryType: r,
    types: i
  });
  const o = ["0x1901"];
  return t && o.push(W0({
    domain: t,
    types: i
  })), r !== "EIP712Domain" && o.push(Sd({
    data: n,
    primaryType: r,
    types: i
  })), et(Rb(o));
}
function W0({ domain: e, types: t }) {
  return Sd({
    data: e,
    primaryType: "EIP712Domain",
    types: t
  });
}
function Sd({ data: e, primaryType: t, types: n }) {
  const r = Ad({
    data: e,
    primaryType: t,
    types: n
  });
  return et(r);
}
function Ad({ data: e, primaryType: t, types: n }) {
  const r = [{ type: "bytes32" }], i = [Z0({ primaryType: t, types: n })];
  for (const o of n[t]) {
    const [s, a] = Id({
      types: n,
      name: o.name,
      type: o.type,
      value: e[o.name]
    });
    r.push(s), i.push(a);
  }
  return Fs(r, i);
}
function Z0({ primaryType: e, types: t }) {
  const n = or(X0({ primaryType: e, types: t }));
  return et(n);
}
function X0({ primaryType: e, types: t }) {
  let n = "";
  const r = _d({ primaryType: e, types: t });
  r.delete(e);
  const i = [e, ...Array.from(r).sort()];
  for (const o of i)
    n += `${o}(${t[o].map(({ name: s, type: a }) => `${a} ${s}`).join(",")})`;
  return n;
}
function _d({ primaryType: e, types: t }, n = /* @__PURE__ */ new Set()) {
  const i = e.match(/^\w*/u)?.[0];
  if (n.has(i) || t[i] === void 0)
    return n;
  n.add(i);
  for (const o of t[i])
    _d({ primaryType: o.type, types: t }, n);
  return n;
}
function Id({ types: e, name: t, type: n, value: r }) {
  if (e[n] !== void 0)
    return [
      { type: "bytes32" },
      et(Ad({ data: r, primaryType: n, types: e }))
    ];
  if (n === "bytes")
    return [{ type: "bytes32" }, et(r)];
  if (n === "string")
    return [{ type: "bytes32" }, et(or(r))];
  if (n.lastIndexOf("]") === n.length - 1) {
    const i = n.slice(0, n.lastIndexOf("[")), o = r.map((s) => Id({
      name: t,
      type: i,
      types: e,
      value: s
    }));
    return [
      { type: "bytes32" },
      et(Fs(o.map(([s]) => s), o.map(([, s]) => s)))
    ];
  }
  return [{ type: n }, r];
}
function Y0(e) {
  const t = fd(e, 0, 1);
  if (t === "0x04")
    return "eip7702";
  if (t === "0x03")
    return "eip4844";
  if (t === "0x02")
    return "eip1559";
  if (t === "0x01")
    return "eip2930";
  if (t !== "0x" && zs(t) >= 192)
    return "legacy";
  throw new l0({ serializedType: t });
}
const dc = "bitcoin-taproot-keypath-v1", fc = "bip341-sighash-default-keypath", J0 = ["bitcoin", "testnet", "signet", "regtest"], $o = 21000000n * 100000000n, Pd = 137, Td = /^[0-9a-f]+$/, Q0 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", ev = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", tv = 734539939, nv = 1;
function V(e) {
  throw new b("invalid_payload", e);
}
function pc(e, t) {
  return (!e || typeof e != "object" || Array.isArray(e)) && V(`${t} must be an object`), e;
}
function hc(e, t, n) {
  const r = Object.keys(e).filter((o) => !t.includes(o));
  r.length > 0 && V(`${n} carries unsupported fields: ${r.sort().join(", ")}`);
  const i = t.filter((o) => !(o in e));
  i.length > 0 && V(`${n} is missing required fields: ${i.join(", ")}`);
}
function Hr(e, t, n) {
  return (typeof e != "string" || n !== void 0 && e.length !== n || e.length === 0 || !Td.test(e)) && V(`${t} must be lowercase hexadecimal${n === void 0 ? "" : ` of length ${n}`}`), e;
}
function rv(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < n || e > r) && V(`${t} must be an integer in ${n}..${r}`), e;
}
function Me(...e) {
  const t = new Uint8Array(e.reduce((r, i) => r + i.length, 0));
  let n = 0;
  for (const r of e)
    t.set(r, n), n += r.length;
  return t;
}
function an(e, t, n, r) {
  return t + n > e.length && V(`transaction is truncated while reading ${r}`), [e.slice(t, t + n), t + n];
}
function Br(e, t, n) {
  t >= e.length && V(`transaction is truncated before ${n}`);
  const r = e[t];
  return r >= 253 && V(`${n} must use a single-byte CompactSize`), [r, t + 1];
}
function no(e) {
  return ((e[0] ?? 0) | (e[1] ?? 0) << 8 | (e[2] ?? 0) << 16 | (e[3] ?? 0) << 24) >>> 0;
}
function iv(e) {
  let t = 0n;
  for (let n = 0; n < 8; n += 1) t |= BigInt(e[n] ?? 0) << BigInt(n * 8);
  return t;
}
function zn(e) {
  return new Uint8Array([e & 255, e >>> 8 & 255, e >>> 16 & 255, e >>> 24 & 255]);
}
function yc(e) {
  const t = new Uint8Array(8);
  let n = e;
  for (let r = 0; r < 8; r += 1)
    t[r] = Number(n & 0xffn), n >>= 8n;
  return t;
}
function Bd(e) {
  if (e.length === 25 && e[0] === 118 && e[1] === 169 && e[2] === 20 && e[23] === 136 && e[24] === 172) return "p2pkh";
  if (e.length === 23 && e[0] === 169 && e[1] === 20 && e[22] === 135) return "p2sh";
  if (e.length === 22 && e[0] === 0 && e[1] === 20) return "p2wpkh";
  if (e.length === 34 && e[0] === 0 && e[1] === 32) return "p2wsh";
  if (e.length === 34 && e[0] === 81 && e[1] === 32) return "p2tr";
  V("transaction output uses an unsupported script template");
}
function ov(e) {
  (e.length === 0 || e.length % 2 || e.length > Pd * 2 || !Td.test(e)) && V("unsignedTransaction must be lowercase hex, without 0x or whitespace, and at most 137 bytes");
  const t = Uint8Array.from(e.match(/../g), (T) => Number.parseInt(T, 16));
  (t.length < 4 || t[0] !== 2 || t[1] !== 0 || t[2] !== 0 || t[3] !== 0) && V("transaction version must be exactly 2");
  let n = 4;
  const [r, i] = Br(t, n, "input count");
  n = i, r !== 1 && V("transaction must contain exactly one input");
  const [o, s] = an(t, n, 32, "outpoint hash");
  n = s, o.every((T) => T === 0) && V("outpoint hash must not be null");
  const [a, c] = an(t, n, 4, "outpoint index");
  n = c;
  const u = no(a), [l, d] = Br(t, n, "scriptSig length");
  n = d, l !== 0 && V("scriptSig must be empty");
  const [h, f] = an(t, n, 4, "sequence");
  n = f;
  const g = no(h);
  g !== 4294967293 && g !== 4294967294 && g !== 4294967295 && V("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff");
  const [m, p] = Br(t, n, "output count");
  n = p, m !== 1 && m !== 2 && V("transaction must contain one or two outputs");
  const y = [], v = { p2pkh: 546n, p2sh: 540n, p2wpkh: 294n, p2wsh: 330n, p2tr: 330n };
  for (let T = 0; T < m; T += 1) {
    const [L, P] = an(t, n, 8, `output ${T} amount`);
    n = P;
    const z = iv(L);
    (z < 1n || z > $o) && V(`output ${T} amount is outside the Bitcoin money range`);
    const [U, w] = Br(t, n, `output ${T} script length`);
    n = w;
    const [S, A] = an(t, n, U, `output ${T} script`);
    n = A;
    const k = Bd(S);
    U !== { p2pkh: 25, p2sh: 23, p2wpkh: 22, p2wsh: 34, p2tr: 34 }[k] && V(`output ${T} script length is not canonical for ${k}`), z < v[k] && V(`output ${T} is below the V1 ${k} dust floor`), y.push({ amountSats: z, scriptPubKey: S, kind: k });
  }
  const [x, I] = an(t, n, 4, "locktime");
  n = I;
  const E = no(x);
  E !== 0 && V("locktime must be exactly zero"), n !== t.length && V("transaction has trailing bytes");
  const $ = y.reduce((T, L) => T + L.amountSats, 0n);
  return ($ < 1n || $ > $o) && V("output total is outside the Bitcoin money range"), { raw: t, prevoutHashLe: o, prevoutIndex: u, sequence: g, outputs: y, locktime: E };
}
function sv(e, t) {
  const n = ue(new TextEncoder().encode(e));
  return ue(Me(n, n, t));
}
function av(e, t, n) {
  const r = Me(e.prevoutHashLe, zn(e.prevoutIndex)), i = Me(new Uint8Array([n.length]), n), o = e.outputs.map((a) => Me(yc(a.amountSats), new Uint8Array([a.scriptPubKey.length]), a.scriptPubKey)), s = Me(
    new Uint8Array([0, 0]),
    zn(2),
    zn(e.locktime),
    ue(r),
    ue(yc(t)),
    ue(i),
    ue(zn(e.sequence)),
    ue(Me(...o)),
    new Uint8Array([0]),
    zn(0)
  );
  return sv("TapSighash", s);
}
function cv(e) {
  return BigInt(`0x${X(e)}`);
}
function wc(e) {
  return zu(ue(e));
}
function gc(e, t) {
  const n = Me(new Uint8Array([e]), t), r = ue(ue(n)).slice(0, 4), i = Me(n, r);
  let o = cv(i), s = "";
  for (; o > 0n; ) {
    const a = Number(o % 58n);
    s = Q0[a] + s, o /= 58n;
  }
  for (const a of i) {
    if (a !== 0) break;
    s = `1${s}`;
  }
  return s;
}
function uv(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >> i & s);
  return i > 0 && o.push(r << n - i & s), o;
}
function lv(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1) i >>> o & 1 && (n ^= t[o]);
  }
  return n >>> 0;
}
function ro(e, t, n) {
  const r = e === "bitcoin" ? "bc" : e === "regtest" ? "bcrt" : "tb", i = [t, ...uv(n, 8, 5)], o = [...r].map((u) => u.charCodeAt(0) >> 5).concat([0], [...r].map((u) => u.charCodeAt(0) & 31)), s = t === 0 ? nv : tv, a = lv(o.concat(i, [0, 0, 0, 0, 0, 0])) ^ s, c = Array.from({ length: 6 }, (u, l) => a >>> 5 * (5 - l) & 31);
  return `${r}1${i.concat(c).map((u) => ev[u]).join("")}`;
}
function Oo(e, t) {
  const n = Bd(t), r = e === "bitcoin";
  return n === "p2pkh" ? gc(r ? 0 : 111, t.slice(3, 23)) : n === "p2sh" ? gc(r ? 5 : 196, t.slice(2, 22)) : n === "p2wpkh" || n === "p2wsh" ? ro(e, 0, t.slice(2)) : ro(e, 1, t.slice(2));
}
function dv(e) {
  const t = wc(e), n = Me(new Uint8Array([0, 20]), t);
  return [
    Me(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    n,
    Me(new Uint8Array([169, 20]), wc(n), new Uint8Array([135]))
  ];
}
function io(e, t) {
  return e.length === t.length && e.every((n, r) => n === t[r]);
}
function $r(e) {
  const t = e / 100000000n, n = e % 100000000n;
  return n === 0n ? `${t}.00000000` : `${t}.${n.toString().padStart(8, "0")}`;
}
function $d(e, t) {
  const n = e / BigInt(t), r = e % BigInt(t);
  if (r === 0n) return `${n.toString()} sat/vB`;
  const i = (r * 10000n / BigInt(t)).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n.toString()}.${i} sat/vB`;
}
function Od(e) {
  return X(ue(ue(e.raw)).slice().reverse());
}
function fv(e, t) {
  const n = t.transaction, r = F("bitcoin.network", { network: e.network }), i = [
    F("bitcoin.title"),
    ...t.highFeeWarning ? [F("bitcoin.highFeeWarning"), ""] : [],
    r,
    F("bitcoin.networkWarning"),
    F("bitcoin.input", { txid: X(n.prevoutHashLe.slice().reverse()), vout: n.prevoutIndex }),
    F("bitcoin.inputAmount", { sats: e.prevout.amountSats, btc: $r(BigInt(e.prevout.amountSats)) }),
    F("bitcoin.outputs")
  ];
  return n.outputs.forEach((o, s) => {
    const a = t.sameKeyOutputIndexes.includes(s);
    i.push(F("bitcoin.output", {
      index: s + 1,
      address: Oo(e.network, o.scriptPubKey),
      sats: o.amountSats.toString(),
      btc: $r(o.amountSats),
      role: F(a ? "bitcoin.sameKeyReturn" : "bitcoin.recipient")
    }));
  }), i.push(
    F("bitcoin.fee", { sats: t.feeSats.toString(), btc: $r(t.feeSats), rate: $d(t.feeSats, t.signedVsize) }),
    F("bitcoin.vsize", { vsize: t.signedVsize }),
    F(n.sequence === 4294967293 ? "bitcoin.rbfSignals" : "bitcoin.rbfDoesNotSignal"),
    F("bitcoin.netLeaving", { sats: t.netLeavingSats.toString(), btc: $r(t.netLeavingSats) }),
    F("bitcoin.unsignedTxid", { txid: Od(n) }),
    F("bitcoin.outputKey", { key: X(t.outputKeyXOnly) })
  ), i.join(`
`);
}
function pv(e, t) {
  const n = pc(e, "signingContext");
  hc(n, ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"], "bitcoin signingContext"), n.kind !== dc && V("unsupported Bitcoin signing_context kind"), (typeof n.network != "string" || !J0.includes(n.network)) && V("bitcoin signing_context network is invalid");
  const r = Hr(n.unsignedTransaction, "unsignedTransaction");
  r.length > Pd * 2 && V("unsignedTransaction exceeds the 137-byte V1 limit");
  const i = pc(n.prevout, "signingContext.prevout");
  hc(i, ["amountSats", "scriptPubKey"], "signingContext.prevout");
  const o = rv(i.amountSats, "prevout.amountSats", 1, Number($o)), s = Hr(i.scriptPubKey, "prevout.scriptPubKey", 68);
  s.startsWith("5120") || V("prevout.scriptPubKey must be a lowercase P2TR output key script"), n.digestAlgorithm !== fc && V("unsupported Bitcoin digest algorithm");
  const a = Hr(n.digest, "signingContext.digest", 64);
  return a !== t && V("Bitcoin signing_context digest must match the sign request digest exactly"), {
    kind: dc,
    network: n.network,
    unsignedTransaction: r,
    prevout: { amountSats: o, scriptPubKey: s },
    digestAlgorithm: fc,
    digest: a
  };
}
function Rd(e, t, n) {
  const r = pv(e, t), i = ov(r.unsignedTransaction), o = Uint8Array.from(r.prevout.scriptPubKey.match(/../g), (y) => Number.parseInt(y, 16)), s = o.slice(2);
  let a;
  if (n !== void 0) {
    const y = Hr(n, "signing public key", 66);
    a = Uint8Array.from(y.match(/../g), (x) => Number.parseInt(x, 16)), a[0] !== 2 && a[0] !== 3 && V("signing public key must be compressed secp256k1");
    const v = zw(y);
    io(s, v) || V("prevout.scriptPubKey does not bind to the signing key's BIP86 output key");
  }
  if (a)
    for (const y of i.outputs)
      dv(a).some((v) => io(v, y.scriptPubKey)) && V("transaction sends to a same-key legacy or SegWit-v0 address this workflow cannot spend");
  const c = i.outputs.flatMap((y, v) => io(y.scriptPubKey, o) ? [v] : []);
  c.length > 1 && V("transaction may contain at most one same-key P2TR return output");
  const u = i.outputs.reduce((y, v) => y + v.amountSats, 0n), l = BigInt(r.prevout.amountSats) - u;
  l <= 0n && V("transaction fee must be positive");
  const d = i.raw.length * 4 + 68, h = Math.ceil(d / 4);
  l > 10000n * BigInt(h) && V("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling");
  const f = X(av(i, BigInt(r.prevout.amountSats), o));
  f !== r.digest && V("Bitcoin signing_context digest does not match the independently computed BIP341 digest");
  const g = c.reduce((y, v) => y + i.outputs[v].amountSats, 0n), m = l > 100n * BigInt(h) || l * 100n > BigInt(r.prevout.amountSats) * 2n, p = BigInt(r.prevout.amountSats) - g;
  return {
    digest: f,
    display: fv(r, {
      transaction: i,
      outputKeyXOnly: s,
      feeSats: l,
      signedVsize: h,
      sameKeyOutputIndexes: c,
      highFeeWarning: m,
      netLeavingSats: p
    }),
    facts: {
      network: r.network,
      spendingAddress: Oo(r.network, o),
      inputTxid: X(i.prevoutHashLe.slice().reverse()),
      inputVout: i.prevoutIndex,
      inputAmountSats: BigInt(r.prevout.amountSats),
      outputs: i.outputs.map((y, v) => ({
        address: Oo(r.network, y.scriptPubKey),
        amountSats: y.amountSats,
        sameKey: c.includes(v)
      })),
      feeSats: l,
      feeRate: $d(l, h),
      signedVsize: h,
      netLeavingSats: p,
      highFeeWarning: m,
      sequence: i.sequence,
      signalsRbf: i.sequence === 4294967293,
      locktime: i.locktime,
      unsignedTxid: Od(i),
      ...n !== void 0 ? { internalKeyXOnly: n.slice(2).toLowerCase() } : {},
      outputKeyXOnly: X(s),
      unsignedTransaction: r.unsignedTransaction,
      prevoutScriptPubKey: r.prevout.scriptPubKey
    },
    transaction: i,
    outputKeyXOnly: s
  };
}
const mc = `avault-agent-operation:v1
`;
function cn(e) {
  if (typeof e != "object" || e === null || Array.isArray(e)) throw new Error("signing context is malformed");
  return e;
}
function bc(e) {
  return e.startsWith("0x") ? e.toLowerCase() : `0x${e.toLowerCase()}`;
}
function oo(e, t) {
  const n = bc(e), r = bc(t);
  if (n !== r)
    throw new Error("signing digest does not match verified context");
  return r.slice(2);
}
function Fr(e) {
  return typeof e == "bigint" ? e.toString() : typeof e == "string" || typeof e == "number" || typeof e == "boolean" ? String(e) : e == null ? "" : yn(e);
}
function yn(e) {
  if (typeof e == "bigint") return JSON.stringify(e.toString());
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(yn).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${yn(t[n])}`).join(",")}}`;
}
const hv = Hm([
  "function approve(address spender,uint256 amount)",
  "function transfer(address to,uint256 amount)",
  "function transferFrom(address from,address to,uint256 amount)",
  "function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
  "function setApprovalForAll(address operator,bool approved)",
  "function safeTransferFrom(address from,address to,uint256 tokenId)",
  "function safeTransferFrom(address from,address to,uint256 tokenId,bytes data)"
]), yv = {
  approve: "ERC-20/ERC-721 approve",
  transfer: "ERC-20 transfer",
  transferFrom: "ERC-20/ERC-721 transferFrom",
  permit: "ERC-20 permit",
  setApprovalForAll: "ERC-721 setApprovalForAll",
  safeTransferFrom: "ERC-721 safeTransferFrom",
  safeTransferFromWithData: "ERC-721 safeTransferFrom"
}, wv = new Map(
  hv.map((e) => {
    const t = e.name === "safeTransferFrom" && e.inputs.length === 4 ? "safeTransferFromWithData" : e.name;
    return [jb(e), { call: t, item: e }];
  })
);
function gv(e) {
  return `${e.name}(${e.inputs.map((t) => t.type).join(",")})`;
}
function mv(e) {
  if (e == null || e === "" || e === "0x") return "0x";
  if (typeof e != "string" || !/^0x[0-9a-fA-F]*$/.test(e) || e.length % 2 !== 0)
    throw new Error("EVM transaction calldata is malformed");
  return e.toLowerCase();
}
const bv = /* @__PURE__ */ new Set([
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
]), vv = /* @__PURE__ */ new Set(["domain", "types", "primaryType", "message"]);
function so(e, t, n) {
  const r = Object.keys(e).filter((i) => !t.has(i));
  if (r.length > 0)
    throw new Error(`${n} carries unsupported field: ${r.sort().join(", ")}`);
}
function Ld(e) {
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
function xv(e, t) {
  if (t.chainId === void 0 || t.chainId === null || t.chainId === "")
    throw new Error("EVM transaction chain id is required");
  if (String(t.chainId) !== String(e.chainId))
    throw new Error("EVM transaction chain id mismatch");
}
function kv(e) {
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
function Ev(e) {
  if (e.length < 10) return null;
  const t = wv.get(e.slice(0, 10));
  if (!t) return null;
  let n;
  try {
    if (n = Yb(t.item.inputs, `0x${e.slice(10)}`), Fs(t.item.inputs, n).toLowerCase() !== `0x${e.slice(10)}`) return null;
  } catch {
    return null;
  }
  return {
    call: t.call,
    signature: gv(t.item),
    arguments: t.item.inputs.map((r, i) => ({
      name: r.name ?? `arg${i}`,
      type: r.type,
      value: Fr(n[i])
    }))
  };
}
function Sv(e, t, n) {
  const r = Ld(e), i = mv(e.data), o = (c) => (t[c] === void 0 ? 0n : t[c]).toString(), s = e.to === void 0 || e.to === null || e.to === "" ? null : String(e.to), a = s === null ? null : Ev(i);
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
function Av(e) {
  const t = [F("evm.title", { chainId: e.chainId })];
  if (e.blindSigning && t.push(F("sign.blindNotice")), t.push(
    F("evm.type", { type: e.type }),
    e.to === null ? F("evm.contractCreation") : F("evm.to", { to: e.to }),
    F("evm.value", { value: e.value }),
    F("evm.nonce", { value: e.nonce }),
    F("evm.gas", { value: e.gas })
  ), e.gasPrice !== void 0 && t.push(F("evm.gasPrice", { value: e.gasPrice })), e.maxFeePerGas !== void 0 && t.push(F("evm.maxFeePerGas", { value: e.maxFeePerGas })), e.maxPriorityFeePerGas !== void 0 && t.push(F("evm.maxPriorityFeePerGas", { value: e.maxPriorityFeePerGas })), e.decoded) {
    t.push(F("evm.call", { call: `${yv[e.decoded.call]} ${e.decoded.signature}` }));
    for (const n of e.decoded.arguments) t.push(`  ${n.name} (${n.type}): ${n.value}`);
  }
  return t.push(
    e.selector === null ? F("evm.selectorNone") : F("evm.selector", { selector: e.selector }),
    e.calldata === "0x" ? F("evm.calldataNone") : F("evm.calldata", { bytes: e.calldataBytes, calldata: e.calldata })
  ), t.join(`
`);
}
function _v(e) {
  Ps(e);
  const t = cn(e), n = {
    "evm-transaction": ["kind", "chainId", "unsignedTransaction", "digestAlgorithm", "digest"],
    "eip-712-typed-data": ["kind", "typedData", "digestAlgorithm", "digest"],
    "avault-agent-operation": ["kind", "canonicalPreimage", "digestAlgorithm", "digest"],
    "bitcoin-taproot-keypath-v1": ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"]
  }, r = Object.hasOwn(n, e.kind) ? n[e.kind] : void 0;
  if (!r) throw new Error("unsupported signing context");
  if (so(t, new Set(r), "signing context"), typeof e.digest != "string" || !/^(?:0x)?[0-9a-fA-F]{64}$/.test(e.digest))
    throw new Error("signing context digest must be 32 bytes");
  switch (e.kind) {
    case "evm-transaction": {
      if (e.digestAlgorithm !== "keccak256") throw new Error("unsupported EVM digest algorithm");
      const i = cn(e.unsignedTransaction);
      so(i, bv, "EVM transaction"), xv(e, i), Ld(i);
      const o = kv(i), s = C0(o), a = oo(e.digest, et(s)), c = Sv(i, o, s);
      if (Y0(s) !== c.type) throw new Error("EVM transaction type does not match its serialization");
      return {
        digest: a,
        challenge: Oe(a),
        display: Av(c),
        evm: c
      };
    }
    case "eip-712-typed-data": {
      if (e.digestAlgorithm !== "eip712") throw new Error("unsupported EIP-712 digest algorithm");
      const i = cn(e.typedData);
      so(i, vv, "EIP-712 typed data");
      const o = cn(i.domain);
      if (o.chainId === void 0 || o.verifyingContract === void 0)
        throw new Error("EIP-712 domain must include chainId and verifyingContract");
      const s = oo(e.digest, G0(i));
      return {
        digest: s,
        challenge: Oe(s),
        display: [
          "EIP-712 typed data",
          `domain.chainId: ${Fr(o.chainId)}`,
          `domain.verifyingContract: ${Fr(o.verifyingContract)}`,
          `domain: ${yn(o)}`,
          `types: ${yn(cn(i.types))}`,
          `primaryType: ${Fr(i.primaryType)}`,
          `message: ${yn(cn(i.message))}`
        ].join(`
`),
        typedData: i
      };
    }
    case "avault-agent-operation": {
      if (e.digestAlgorithm !== "avault-operation-hash-v1") throw new Error("unsupported avault digest algorithm");
      if (typeof e.canonicalPreimage != "string" || !e.canonicalPreimage.startsWith(mc) || e.canonicalPreimage.slice(mc.length).trim().length === 0)
        throw new Error("canonical preimage must start with avault-agent-operation:v1\\n followed by operation text; recreate the request");
      const i = oo(e.digest, Vy(ue(new TextEncoder().encode(e.canonicalPreimage))));
      return {
        digest: i,
        challenge: Oe(i),
        display: ["Avault agent operation", e.canonicalPreimage].join(`
`)
      };
    }
    case "bitcoin-taproot-keypath-v1": {
      const i = Rd(e, e.digest);
      return {
        digest: i.digest,
        challenge: Oe(i.digest),
        display: i.display
      };
    }
    default:
      throw new Error("unsupported signing context");
  }
}
function Kr(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) return;
  const t = e.messageHex;
  if (typeof t == "string")
    try {
      Vu(t);
    } catch {
      throw new b("invalid_payload", "signing messageHex exceeds the 1 MiB decoded limit");
    }
}
function Ci(e) {
  if (Kr(e), Ps(e), !e || typeof e != "object" || Array.isArray(e)) throw new b("invalid_payload", "Solana signing context must be an object");
  const t = e;
  if (t.kind !== "solana-transaction-v1" || Object.keys(t).some((n) => n !== "kind" && n !== "messageHex") || typeof t.messageHex != "string" || t.messageHex.length === 0 || t.messageHex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(t.messageHex))
    throw new b("invalid_payload", "Solana context requires only kind and nonempty lowercase even messageHex; no digest alias");
  return { kind: "solana-transaction-v1", messageHex: t.messageHex };
}
function Iv(e, t, n) {
  if (n.kind !== "keypair" || !n.publicKey) throw new Error("protected signing requires a keypair protected record");
  const r = An(n.curve);
  if (e === Qn || t.kind === "solana-transaction-v1" || r === "ed25519") {
    if (e !== Qn || t.kind !== "solana-transaction-v1" || r !== "ed25519")
      throw new b("invalid_payload", "Ed25519 signing requires an Ed25519 record, ed25519 scheme and Solana context together");
    Ci(t);
  }
}
const Cd = "11111111111111111111111111111111", zd = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", Pv = "SysvarRecentB1ockHashes11111111111111111111", Tv = 2, Bv = 4, $v = 3, Ov = 12;
function yt(e) {
  return { decoded: null, undecodedReason: e };
}
function Ud(e, t) {
  let n = 0n;
  for (let r = 7; r >= 0; r -= 1) n = n << 8n | BigInt(e[t + r]);
  return n.toString();
}
function Ws(e) {
  const t = [];
  for (const n of e) {
    if (n.source !== "static") return null;
    t.push(n.address);
  }
  return t;
}
function Rv(e, t) {
  const n = t.length >= 4 ? (t[0] | t[1] << 8 | t[2] << 16 | t[3] << 24) >>> 0 : -1;
  return n === Tv && t.length === 12 ? Lv(e, t) : n === Bv && t.length === 4 ? Cv(e) : yt("unsupported_instruction");
}
function Lv(e, t) {
  if (e.length !== 2) return yt("unsupported_instruction");
  const n = Ws(e);
  if (!n) return yt("unresolved_account");
  const [r, i] = n;
  return { decoded: { kind: "system-transfer", source: r, destination: i, lamports: Ud(t, 4) }, undecodedReason: null };
}
function Cv(e) {
  if (e.length !== 3) return yt("unsupported_instruction");
  const t = Ws(e);
  if (!t) return yt("unresolved_account");
  const [n, r, i] = t;
  return r !== Pv ? yt("unsupported_instruction") : { decoded: { kind: "system-advance-nonce", nonceAccount: n, recentBlockhashesSysvar: r, nonceAuthority: i }, undecodedReason: null };
}
function zv(e, t) {
  const n = t.length === 9 && t[0] === $v && e.length === 3, r = t.length === 10 && t[0] === Ov && e.length === 4;
  if (!n && !r) return yt("unsupported_instruction");
  const i = Ws(e);
  if (!i) return yt("unresolved_account");
  const o = Ud(t, 1);
  if (n) {
    const [l, d, h] = i;
    return { decoded: { kind: "spl-transfer", sourceTokenAccount: l, destinationTokenAccount: d, authority: h, amount: o }, undecodedReason: null };
  }
  const [s, a, c, u] = i;
  return {
    decoded: { kind: "spl-transfer-checked", sourceTokenAccount: s, mint: a, destinationTokenAccount: c, authority: u, amount: o, decimals: t[9] },
    undecodedReason: null
  };
}
function Uv(e, t, n) {
  return e === Cd ? Rv(t, n) : e === zd ? zv(t, n) : yt("unsupported_program");
}
const Nv = 32, ao = 512, Un = 256, vc = 8, un = 3, Nd = /* @__PURE__ */ new Map([
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
]), Mv = /* @__PURE__ */ new Set(["1", "10", "8453", "42161", "11155111", "11155420", "84532", "421614"]);
function Md(e) {
  return Nd.has(e) ? [] : ["review.note.unknownChain"];
}
function Hd(e) {
  const t = Nd.get(e);
  return t === void 0 ? e : `${e} · ${t}`;
}
function wi(e) {
  return typeof e == "object" && e !== null && !Array.isArray(e);
}
function Fd(e) {
  if (typeof e == "bigint") return e;
  if (typeof e != "string" && typeof e != "number" && typeof e != "boolean" && !Array.isArray(e)) return null;
  try {
    return BigInt(e);
  } catch {
    return null;
  }
}
function Ye(e, t) {
  if (/^u?int\d*$/.test(e)) {
    const n = Fd(t);
    return n === null ? JSON.stringify(t) ?? String(t) : n.toString();
  }
  return typeof t == "string" ? t : JSON.stringify(t) ?? String(t);
}
class xc extends Error {
}
function gi(e, t, n, r, i, o) {
  if (i > Nv) {
    o.omitted += 1;
    return;
  }
  const s = e[t];
  if (s !== void 0) {
    if (!Object.hasOwn(e, t) || !Array.isArray(s) || n === null || n === void 0) throw new xc(r);
    const a = Object(n), c = new Set(s.map((u) => u.name));
    for (const u of s) gi(e, u.type, a[u.name], r ? `${r}.${u.name}` : u.name, i + 1, o);
    if (typeof n == "object")
      for (const u of Object.keys(n)) c.has(u) || o.uncovered.push(r ? `${r}.${u}` : u);
    return;
  }
  if (t !== "bytes" && t !== "string" && t.endsWith("]")) {
    if (!Array.isArray(n)) throw new xc(r);
    const a = t.slice(0, t.lastIndexOf("["));
    n.length === 0 && o.covered.push({ path: r, type: t, value: "[]" }), n.forEach((c, u) => gi(e, a, c, `${r}[${u}]`, i + 1, o));
    return;
  }
  o.covered.push({ path: r, type: t, value: Ye(t, n) });
}
function Hv(e, t) {
  const n = /* @__PURE__ */ new Set(), r = (i) => {
    const o = /^\w*/.exec(i)[0];
    if (!(n.has(o) || !e[o])) {
      n.add(o);
      for (const s of e[o]) r(s.type);
    }
  };
  return r(t), n;
}
const Kd = [["token", "address"], ["amount", "uint160"], ["expiration", "uint48"], ["nonce", "uint48"]], Dd = [["token", "address"], ["amount", "uint256"]], Fv = { primaryType: "Permit", types: { Permit: [["owner", "address"], ["spender", "address"], ["value", "uint256"], ["nonce", "uint256"], ["deadline", "uint256"]] } }, Kv = { primaryType: "PermitSingle", types: { PermitSingle: [["details", "PermitDetails"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Kd } }, Dv = { primaryType: "PermitBatch", types: { PermitBatch: [["details", "PermitDetails[]"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Kd } }, jv = {
  primaryType: "PermitTransferFrom",
  types: { PermitTransferFrom: [["permitted", "TokenPermissions"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: Dd }
}, Vv = {
  primaryType: "PermitBatchTransferFrom",
  types: { PermitBatchTransferFrom: [["permitted", "TokenPermissions[]"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: Dd }
};
function Nn(e, t, n) {
  if (t !== n.primaryType) return !1;
  const r = Hv(e, t), i = Object.keys(n.types);
  return r.size !== i.length || !i.every((o) => r.has(o)) ? !1 : i.every((o) => {
    const s = e[o], a = n.types[o];
    return Array.isArray(s) && s.length === a.length && s.every((c, u) => c.name === a[u][0] && c.type === a[u][1]);
  });
}
const kc = (1n << 256n) - 1n, qv = (1n << 160n) - 1n;
function co(e, t, n, r, i) {
  const o = Fd(t), s = o === n;
  return {
    field: {
      label: e,
      ...i === void 0 ? {} : { index: i },
      value: o === null ? Ye("string", t) : o.toString(),
      ...s ? { shownAs: r, notes: ["review.note.exactInDetails"] } : { notes: ["review.note.rawTokenUnits"] }
    },
    isMax: s
  };
}
function Ft(e) {
  return typeof e == "string" ? e : Ye("string", e);
}
function Gv(e, t, n) {
  const r = ["review.warning.tokenAuthorization"];
  if (Nn(e, t, Fv)) {
    const s = co("review.field.permitAmount", n.value, kc, "review.value.unlimited");
    return {
      kind: "review.kind.erc2612Permit",
      warnings: s.isMax ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: [
        { label: "review.field.spender", value: Ft(n.spender), elide: !0 },
        s.field,
        { label: "review.field.permitDeadline", value: Ye("uint256", n.deadline), notes: ["review.note.permitDeadline"] }
      ],
      identity: [
        { label: "review.field.permitOwner", value: Ft(n.owner), elide: !0 },
        { label: "review.field.permitNonce", value: Ye("uint256", n.nonce) }
      ]
    };
  }
  const i = Nn(e, t, Kv);
  if (i || Nn(e, t, Dv)) {
    const s = i ? [n.details] : Array.isArray(n.details) ? n.details : [];
    let a = !1;
    const c = [];
    return s.forEach((u, l) => {
      const d = wi(u) ? u : {}, h = co("review.field.permitTokenAmount", d.amount, qv, "review.value.unlimited", l + 1);
      a ||= h.isMax, l < un && c.push(
        h.field,
        { label: "review.field.permitToken", index: l + 1, value: Ft(d.token), elide: !0 },
        { label: "review.field.permitExpiration", index: l + 1, value: Ye("uint48", d.expiration), notes: ["review.note.permit2Expiration"] }
      );
    }), s.length > un && c.push({ label: "review.field.moreTokens", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: s.length - un }] }), {
      kind: "review.kind.permit2Allowance",
      warnings: a ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: [
        { label: "review.field.spender", value: Ft(n.spender), elide: !0 },
        ...c,
        { label: "review.field.signatureDeadline", value: Ye("uint256", n.sigDeadline), notes: ["review.note.signatureDeadline"] }
      ],
      identity: []
    };
  }
  const o = Nn(e, t, jv);
  if (o || Nn(e, t, Vv)) {
    const s = o ? [n.permitted] : Array.isArray(n.permitted) ? n.permitted : [];
    let a = !1;
    const c = [];
    return s.forEach((u, l) => {
      const d = wi(u) ? u : {}, h = co("review.field.permitTokenAmount", d.amount, kc, "review.value.maximumOneTime", l + 1);
      a ||= h.isMax, l < un && c.push(h.field, { label: "review.field.permitToken", index: l + 1, value: Ft(d.token), elide: !0 });
    }), s.length > un && c.push({ label: "review.field.moreTokens", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: s.length - un }] }), {
      kind: "review.kind.permit2Transfer",
      warnings: a ? ["review.warning.oneTimeTransfer", "review.warning.maximumOneTimeTransfer"] : ["review.warning.oneTimeTransfer"],
      summary: [
        // The Permit2 contract signs msg.sender as the spender; this is the value in the message.
        { label: "review.field.spender", value: Ft(n.spender), elide: !0 },
        ...c,
        { label: "review.field.permitDeadline", value: Ye("uint256", n.deadline), notes: ["review.note.signatureDeadline"] }
      ],
      identity: [{ label: "review.field.permitNonce", value: Ye("uint256", n.nonce) }]
    };
  }
  return null;
}
function jd(e) {
  return e.length <= ao ? { name: e } : { name: `${e.slice(0, ao / 2)} … ${e.slice(-ao / 4)}`, notes: ["review.note.pathShortened"] };
}
function uo(e) {
  const { name: t, notes: n } = jd(`${e.path} (${e.type})`);
  return { label: "review.field.typedField", name: t, value: e.value, ...n ? { notes: n } : {} };
}
function Wv(e, t) {
  const n = e, r = wi(n.domain) ? n.domain : {}, i = n.types, o = {
    EIP712Domain: Ed({ domain: r }),
    ...i
  }, s = ps(t.publicKey).eth;
  if (!s) throw new Error("EIP-712 review requires a secp256k1 signing key");
  const a = { covered: [], uncovered: [], omitted: 0 };
  gi(o, "EIP712Domain", r, "domain", 0, a);
  const c = a.covered, u = a.uncovered, l = new Set(o.EIP712Domain.map((T) => T.name)), d = (T, L) => {
    if (!Object.hasOwn(r, T)) return [];
    const P = l.has(T), z = o.EIP712Domain.find((w) => w.name === T), U = P && z ? Ye(z.type, r[T]) : Ft(r[T]);
    return [{
      label: L,
      value: T === "chainId" ? Hd(U) : U,
      notes: [...T === "chainId" ? Md(U) : [], ...P ? [] : ["review.note.notInTypedHash"]]
    }];
  }, h = n.primaryType !== "EIP712Domain", f = wi(n.message) ? n.message : {}, g = { covered: [], uncovered: u, omitted: 0 };
  h ? gi(o, n.primaryType, n.message, "", 0, g) : n.message !== void 0 && u.push("message");
  const m = g.covered, p = a.omitted + g.omitted;
  let y = null;
  try {
    y = h ? Gv(o, n.primaryType, f) : null;
  } catch {
    y = null;
  }
  const v = m.slice(0, vc).map((T) => T.type === "address" ? { ...uo(T), elide: !0 } : uo(T)), x = Math.max(0, m.length - vc) + p;
  x > 0 && v.push({ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: x }] });
  const I = (T, L = 0) => {
    const P = Math.max(0, T.length - Un) + L;
    return [
      ...T.slice(0, Un).map(uo),
      ...P > 0 ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: P }] }] : []
    ];
  }, E = JSON.stringify(n, null, 2), $ = [
    { title: "review.group.typedDomain", fields: I(c, a.omitted) },
    ...h ? [{ title: "review.group.typedMessage", fields: I(m, g.omitted) }] : [],
    ...u.length > 0 ? [{
      title: "review.group.notInTypedHash",
      fields: [
        ...u.slice(0, Un).map((T) => {
          const L = jd(T);
          return { label: "review.field.typedField", name: L.name, value: "", notes: ["review.note.notInTypedHash", ...L.notes ?? []] };
        }),
        ...u.length > Un ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: u.length - Un }] }] : []
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
        { label: "review.field.typedData", value: E, notes: [{ key: "review.note.bytes", count: new TextEncoder().encode(E).length }], raw: !0 }
      ]
    }
  ];
  return {
    kind: y?.kind ?? "review.kind.eip712",
    signer: { label: "review.field.signingAddress", value: s, elide: !0 },
    // What the signature grants first, then where it applies, then who; the signer last.
    summary: [
      ...y ? y.summary : v,
      ...u.length > 0 ? [{ label: "review.field.notInTypedHash", value: "", notes: [{ key: "review.note.fieldsOutsideHash", count: u.length }] }] : [],
      ...d("chainId", "review.field.chainId"),
      ...d("name", "review.field.domainName"),
      ...d("verifyingContract", "review.field.verifyingContract").map((T) => ({ ...T, elide: !0 })),
      { label: "review.field.primaryType", value: n.primaryType, ...h ? {} : { notes: ["review.note.messageNotHashed"] } },
      ...y ? y.identity : []
    ],
    // Every hashed value is shown; nothing is blind.
    undecoded: !1,
    ...y ? { warnings: y.warnings } : {},
    details: $
  };
}
const Ec = 3, Sc = 3, Zv = 3;
function Vd(e) {
  return e === Cd ? ["review.note.systemProgram"] : e === zd ? ["review.note.tokenProgram"] : [];
}
function zi(e) {
  return e.length / 2;
}
function qd(e) {
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
        {
          label: "review.field.amount",
          value: e.amount,
          notes: ["review.note.baseUnits", { key: "review.note.decimals", count: e.decimals }]
        }
      ];
  }
}
function Xv(e) {
  return e.kind === "spl-transfer-checked" ? "TransferChecked" : "Transfer";
}
function Ac(e) {
  return {
    label: "review.field.undecodedInstruction",
    value: e.programId,
    notes: [{ key: "review.note.dataBytes", count: zi(e.dataHex) }, "review.note.notDecoded"]
  };
}
function Yv(e, t) {
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
        {
          label: "review.field.amount",
          value: e.amount,
          notes: ["review.note.baseUnits", { key: "review.note.decimals", count: e.decimals }]
        },
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount, elide: !0 },
        { label: "review.field.mint", value: e.mint, elide: !0 }
      ];
  }
}
function _c(e) {
  return e === 0 ? [] : [{ label: "review.field.durableNonce", value: "", ...e > 1 ? { notes: [{ key: "review.note.nonceAdvances", count: e }] } : {} }];
}
function Jv(e) {
  const t = qd(e).map((r) => r.label === "review.field.amount" ? r : { ...r, elide: !0 }), n = ["review.field.amount", "review.field.to", "review.field.toTokenAccount", "review.field.mint", "review.field.from", "review.field.fromTokenAccount"];
  return [...t].sort((r, i) => n.indexOf(r.label) - n.indexOf(i.label));
}
const mi = (e) => e.kind !== "system-advance-nonce";
function Qv(e) {
  if (e.instructions.length === 1) {
    const [s] = e.instructions;
    return s.decoded ? mi(s.decoded) ? Jv(s.decoded) : _c(1) : [{ ...Ac(s), elide: !0 }];
  }
  let t = 0, n = 0, r = 0;
  const i = e.instructions.flatMap((s) => {
    const a = s.decoded;
    return a && mi(a) ? (t += 1, t <= Ec ? Yv(a, t) : []) : a ? (n += 1, []) : (r += 1, r <= Sc ? [{ ...Ac(s), elide: !0 }] : []);
  }), o = (s, a, c, u) => c > u ? [{ label: s, value: "", notes: [{ key: a, count: c - u }] }] : [];
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
    ...o("review.field.moreTransfers", "review.note.moreInDetails", t, Ec),
    ...o("review.field.moreUndecoded", "review.note.moreInstructionsInDetails", r, Sc),
    ..._c(n)
  ];
}
function ex(e) {
  return mi(e) ? [
    { label: "review.field.instruction", value: Xv(e) },
    ...qd(e),
    ...e.kind === "system-transfer" ? [] : [{ label: "review.field.authority", value: e.authority }]
  ] : [
    { label: "review.field.instruction", value: "AdvanceNonceAccount" },
    { label: "review.field.nonceAccount", value: e.nonceAccount },
    { label: "review.field.recentBlockhashesSysvar", value: e.recentBlockhashesSysvar },
    { label: "review.field.nonceAuthority", value: e.nonceAuthority }
  ];
}
function tx(e) {
  return e.map((t) => t.index).join(", ");
}
function nx(e) {
  const t = e.decoded, n = {
    label: "review.field.data",
    value: e.dataHex,
    notes: [{ key: "review.note.bytes", count: zi(e.dataHex) }, ...t ? [] : ["review.note.notDecoded"]]
  };
  return {
    title: "review.group.instruction",
    index: e.index + 1,
    fields: [
      { label: "review.field.program", value: e.programId, notes: Vd(e.programId) },
      ...t ? ex(t) : [],
      { label: "review.field.accountIndexes", value: tx(e.accounts) },
      n
    ]
  };
}
function rx(e) {
  const t = e.instructions.length === 1 ? e.instructions[0].decoded : null;
  return {
    kind: !t || !mi(t) ? "review.kind.solanaTransaction" : t.kind === "system-transfer" ? "review.kind.solanaSystemTransfer" : "review.kind.solanaTokenTransfer",
    signer: { label: "review.field.signingKey", value: e.expectedSigner.address, elide: !0 },
    summary: Qv(e),
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
            ...Vd(n.address)
          ]
        }))
      },
      ...e.instructions.map(nx),
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
function Ic(e) {
  return Mv.has(e.chainId) ? { label: "review.field.value", value: e.value, native: "eth" } : { label: "review.field.value", value: e.value, notes: ["review.note.wei"] };
}
function Pc(e) {
  return e.to === null ? { label: "review.field.contract", value: "", notes: ["review.note.contractCreation"] } : { label: "review.field.contract", value: e.to };
}
function Gd(e) {
  return (e.decoded?.arguments ?? []).map((t, n) => ({
    label: "review.field.argument",
    index: n + 1,
    name: `${t.name} (${t.type})`,
    value: t.value
  }));
}
function ix(e) {
  if (e.decoded) {
    const n = { label: "review.field.call", value: e.decoded.signature }, r = Gd(e).map((i) => i.name?.endsWith(" (address)") ? { ...i, elide: !0 } : i);
    return r.length <= Zv ? [n, ...r] : [{ ...n, notes: [{ key: "review.note.argumentsInDetails", count: r.length }] }];
  }
  if (e.calldata === "0x") return [];
  const t = [{ key: "review.note.dataBytes", count: e.calldataBytes }, "review.note.notDecoded"];
  return e.to === null || e.selector === null ? [{ label: "review.field.initCode", value: "", notes: t }] : [{ label: "review.field.selector", value: e.selector, notes: t }];
}
function ox(e, t) {
  const n = ps(t.publicKey).eth;
  if (!n) throw new Error("EVM review requires a secp256k1 signing key");
  const r = (i, o) => o === void 0 ? [] : [{ label: i, value: o, notes: ["review.note.wei"] }];
  return {
    kind: "review.kind.evmTransaction",
    signer: { label: "review.field.signingAddress", value: n, elide: !0 },
    summary: [
      // What it does first, then where: a static label describes the encoded id; it does not prove the network.
      { ...Pc(e), elide: !0 },
      Ic(e),
      ...ix(e),
      { label: "review.field.chainId", value: Hd(e.chainId), notes: Md(e.chainId) }
    ],
    undecoded: e.blindSigning,
    details: [
      {
        title: "review.group.transaction",
        fields: [
          { label: "review.field.publicKey", value: t.publicKey.toLowerCase() },
          { label: "review.field.scheme", value: t.scheme },
          { label: "review.field.type", value: e.type },
          { label: "review.field.chainId", value: e.chainId },
          Pc(e),
          Ic(e),
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
          ...Gd(e)
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
            notes: [{ key: "review.note.bytes", count: zi(e.unsignedTransactionHex) - 1 }],
            raw: !0
          },
          { label: "review.field.digest", value: `0x${t.digest}` }
        ]
      }
    ]
  };
}
function sx(e, t) {
  const n = (s) => s.toString(), r = { label: "review.field.network", value: e.network, notes: ["review.note.requesterNetwork"] }, i = e.outputs.map((s) => s.sameKey ? { label: "review.field.returnToKey", value: n(s.amountSats), native: "btc", unit: "symbol" } : { label: "review.field.send", value: n(s.amountSats), native: "btc", unit: "symbol", to: s.address }), o = e.outputs.filter((s) => !s.sameKey).map((s) => ({ label: "review.field.recipientAddress", value: s.address }));
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
            notes: [{ key: "review.note.bytes", count: zi(e.unsignedTransaction) }],
            raw: !0
          },
          { label: "review.field.prevoutScript", value: e.prevoutScriptPubKey }
        ]
      }
    ]
  };
}
const ax = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function Or(e) {
  let t = 0;
  for (; t < e.length && e[t] === 0; ) t += 1;
  let n = 0n;
  for (const i of e) n = n << 8n | BigInt(i);
  let r = "";
  for (; n > 0n; )
    r = ax[Number(n % 58n)] + r, n /= 58n;
  return "1".repeat(t) + r;
}
class Wd extends Error {
  code;
  constructor(t, n) {
    super(n), this.name = "SolanaMessageError", this.code = t;
  }
}
const Rr = 1232, Tc = 64, Mn = 32, cx = 32, Bc = 128, $c = 256;
function de(e, t) {
  throw new Wd(e, t);
}
function Oc(e) {
  let t = "";
  for (const n of e) t += n.toString(16).padStart(2, "0");
  return t;
}
function ux(e) {
  return e < 128 ? 1 : e < 16384 ? 2 : 3;
}
function lx(e, t, n = "compact-u16") {
  let r = 0;
  for (let i = 0; ; i += 1) {
    t + i >= e.length && de("truncated", `message ends inside ${n}`);
    const o = e[t + i];
    o === 0 && i !== 0 && de("invalid_compact_u16", `${n} is not minimally encoded`);
    const s = (o & 128) === 0;
    if (i === 2 && !s && de("invalid_compact_u16", `${n} is longer than three bytes`), r |= (o & 127) << 7 * i, r > 65535 && de("invalid_compact_u16", `${n} exceeds 65535`), s) return { value: r, length: i + 1 };
  }
}
class dx {
  bytes;
  offset = 0;
  constructor(t) {
    this.bytes = t;
  }
  get remaining() {
    return this.bytes.length - this.offset;
  }
  u8(t) {
    return this.remaining < 1 && de("truncated", `message ends before ${t}`), this.bytes[this.offset++];
  }
  take(t, n) {
    this.remaining < t && de("truncated", `message ends inside ${n}`);
    const r = this.bytes.subarray(this.offset, this.offset + t);
    return this.offset += t, r;
  }
  compactU16(t) {
    const { value: n, length: r } = lx(this.bytes, this.offset, t);
    return this.offset += r, n;
  }
}
function Zd(e) {
  if (typeof e == "object" && e !== null) {
    for (const t of Object.values(e)) Zd(t);
    Object.freeze(e);
  }
  return e;
}
function fx(e, t) {
  e instanceof Uint8Array || de("invalid_input", "message must be a byte array"), (!(t instanceof Uint8Array) || t.length !== Mn) && de("invalid_input", "expected public key must be 32 bytes"), e.length > Rr - 1 - Tc && de("packet_too_large", `message of ${e.length} bytes cannot fit a ${Rr}-byte transaction`);
  const n = new Uint8Array(e), r = Or(new Uint8Array(t)), i = new dx(n), o = i.u8("message prefix");
  let s = "legacy", a = o;
  if ((o & Bc) !== 0) {
    const P = o & ~Bc;
    P !== 0 && de("unsupported_version", `Solana message version ${P} is not supported`), s = "v0", a = i.u8("required signature count");
  }
  const c = i.u8("read-only signed account count"), u = i.u8("read-only unsigned account count"), l = i.compactU16("account key count"), d = i.take(l * Mn, "account keys"), h = Array.from({ length: l }, (P, z) => Or(d.subarray(z * Mn, (z + 1) * Mn))), f = Or(i.take(cx, "recent blockhash")), g = [];
  for (let P = i.compactU16("instruction count"); P > 0; P -= 1) {
    const z = i.u8("instruction program id index"), U = i.take(i.compactU16("instruction account count"), "instruction account indexes"), w = i.take(i.compactU16("instruction data length"), "instruction data");
    g.push({ programIdIndex: z, accountIndexes: U, data: w });
  }
  const m = [];
  if (s === "v0")
    for (let P = i.compactU16("address table lookup count"); P > 0; P -= 1) {
      const z = i.take(Mn, "address table key"), U = i.take(i.compactU16("writable lookup index count"), "writable lookup indexes"), w = i.take(i.compactU16("read-only lookup index count"), "read-only lookup indexes");
      m.push({ accountKey: z, writableIndexes: U, readonlyIndexes: w });
    }
  i.remaining !== 0 && de("trailing_bytes", `${i.remaining} bytes follow the message`), a + u > l && de("invalid_header", "header counts more signed and read-only unsigned accounts than the message carries"), c >= a && de("invalid_header", "header leaves no writable signer to pay the fee");
  let p = 0, y = 0;
  for (const P of m)
    P.writableIndexes.length + P.readonlyIndexes.length === 0 && de("invalid_address_table_lookup", "an address table lookup loads no account"), p += P.writableIndexes.length, y += P.readonlyIndexes.length;
  const v = l + p + y;
  v > $c && de("invalid_address_table_lookup", `message addresses ${v} accounts; the limit is ${$c}`);
  for (const P of g) {
    (P.programIdIndex === 0 || P.programIdIndex >= l) && de("invalid_program_id_index", `instruction program id index ${P.programIdIndex} is not a static non-payer key`);
    for (const z of P.accountIndexes)
      z >= v && de("invalid_account_index", `instruction account index ${z} is outside the message's ${v} accounts`);
  }
  const x = ux(a) + a * Tc + n.length;
  x > Rr && de("packet_too_large", `signed transaction would be ${x} bytes; the limit is ${Rr}`);
  const I = h.indexOf(r);
  (I === -1 || I >= a) && de("signer_not_required", "the expected key is not a required signer of this message");
  const E = h.map((P, z) => ({
    index: z,
    address: P,
    signer: z < a,
    writable: z < a ? z < a - c : z < l - u
  })), $ = m.map((P) => ({
    accountKey: Or(P.accountKey),
    writableIndexes: Array.from(P.writableIndexes),
    readonlyIndexes: Array.from(P.readonlyIndexes)
  })), T = (P) => {
    if (P < l) return { source: "static", ...E[P] };
    const z = P - l < p;
    let U = P - l - (z ? 0 : p);
    for (const [w, S] of $.entries()) {
      const A = z ? S.writableIndexes : S.readonlyIndexes;
      if (U < A.length)
        return { source: "lookup", index: P, lookupIndex: w, accountKey: S.accountKey, tableIndex: A[U], writable: z };
      U -= A.length;
    }
    throw new Error("account index was validated against the lookup tables");
  }, L = g.map((P, z) => {
    const U = h[P.programIdIndex], w = Array.from(P.accountIndexes, T);
    return {
      index: z,
      programIdIndex: P.programIdIndex,
      programId: U,
      accounts: w,
      dataHex: Oc(P.data),
      ...Uv(U, w, P.data)
    };
  });
  return Zd({
    version: s,
    messageHex: Oc(n),
    messageLength: n.length,
    transactionLength: x,
    header: { numRequiredSignatures: a, numReadonlySignedAccounts: c, numReadonlyUnsignedAccounts: u },
    feePayer: h[0],
    expectedSigner: { index: I, address: r, isFeePayer: I === 0 },
    staticAccountKeys: E,
    recentBlockhash: f,
    instructions: L,
    addressTableLookups: $,
    hasUndecodedContent: $.length > 0 || L.some((P) => P.decoded === null)
  });
}
function ln(e, t) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new b("invalid_payload", `${t} must be an object`);
  return e;
}
function Lr(e, t) {
  if (typeof e != "string" || e.length === 0) throw new b("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Vn(e, t, n) {
  if (Object.keys(e).some((r) => !t.includes(r)))
    throw new b("invalid_payload", `${n} carries unsupported fields; script trees are not supported`);
}
function Zs(e) {
  if (e && typeof e == "object") {
    for (const t of Object.values(e)) Zs(t);
    Object.freeze(e);
  }
  return e;
}
function Xs(e, t) {
  if (Kr(e), t && typeof t == "object") {
    const n = t.release;
    Kr(n), n && typeof n == "object" && Kr(n.signingContext);
  }
}
function Xd(e) {
  const t = ln(e, "sign request");
  Xs(t.signingContext, t.context), Ps(t);
  const n = ln(structuredClone(t), "sign request");
  Vn(n, ["material", "scheme", "signingContext", "context"], "sign request");
  const r = ln(n.material, "material");
  Vn(r, ["name", "envelope"], "material");
  const i = ln(r.envelope, "envelope");
  Vn(i, ["ciphertext", "nonce", "wrap_meta"], "envelope");
  const o = Bs(n.context);
  Vn(ln(o, "context"), ["v", "purpose", "requestId", "grantId", "display", "release", "agent", "expiresAt", "signature"], "sign context");
  let s;
  try {
    s = ts(n.scheme);
  } catch {
    throw new b("invalid_payload", "unsupported signing scheme");
  }
  const a = ln(n.signingContext, "signingContext");
  return a.kind === "solana-transaction-v1" && Ci(a), Zs({
    material: {
      name: Lr(r.name, "material.name"),
      envelope: {
        ciphertext: Lr(i.ciphertext, "envelope.ciphertext"),
        nonce: Lr(i.nonce, "envelope.nonce"),
        wrap_meta: Lr(i.wrap_meta, "envelope.wrap_meta")
      }
    },
    scheme: s,
    signingContext: a,
    context: o
  });
}
function Bt() {
  throw new b("invalid_sign_binding", "signing approval is missing or does not match its signed binding; update Avibe and recreate the request");
}
function Yd(e, t, n) {
  Xs(e.signingContext, e.context), So({ context: e.context, expectedPurpose: "sign", secret: { name: e.material.name, kind: "keypair" } });
  const r = e.context.release;
  (!r || Array.isArray(r)) && Bt();
  const i = t.kind === "message";
  if (!i && (t.kind !== "digest" || !/^[0-9a-f]{64}$/.test(t.digest)) && Bt(), Vn(r, ["name", "scheme", i ? "messageHex" : "digest", "publicKey", "signingContext"], "sign context.release"), i) {
    const o = Ci(e.signingContext);
    (t.messageHex !== o.messageHex || r.messageHex !== t.messageHex || e.scheme !== "ed25519" || !/^[0-9a-f]{64}$/.test(n) || r.publicKey !== n) && Bt();
  } else (e.scheme === "ed25519" || e.signingContext.kind === "solana-transaction-v1" || r.digest !== t.digest) && Bt();
  (r.name !== e.material.name || r.scheme !== e.scheme || typeof r.publicKey != "string" || r.publicKey.toLowerCase() !== n.toLowerCase() || !r.signingContext || it(r.signingContext) !== it(e.signingContext)) && Bt();
}
function px(e) {
  return Xs(e.signingContext, e.context), ue(new TextEncoder().encode(Ti(e.context)));
}
function hx(e, t) {
  if (ts(e), e === "ed25519") throw new Error("Ed25519 exact-message signing is reviewed structurally and has no prose description");
  if (!/^(02|03)[0-9a-fA-F]{64}$/.test(t)) throw new Error("signing public key must be compressed secp256k1");
  return e === es ? `BIP340 raw Schnorr (untweaked internal key)
internal key: ${t.slice(2).toLowerCase()}` : e === er ? F("bitcoin.signingIdentity", { key: t.slice(2).toLowerCase() }) : `${e === Ai ? "ECDSA secp256k1 recoverable" : "ECDSA secp256k1 DER"}
public key: ${t.toLowerCase()}`;
}
function yx(e, t) {
  const n = Ci(e.signingContext);
  Yd(e, { kind: "message", messageHex: n.messageHex }, t);
  let r;
  try {
    r = fx(Jo(n.messageHex), Oe(t));
  } catch (i) {
    throw i instanceof Wd ? new b("invalid_payload", `Solana message cannot be signed (${i.code}): ${i.message}`) : i;
  }
  return r.messageHex !== n.messageHex && Bt(), { payload: { kind: "message", messageHex: r.messageHex }, review: rx(r) };
}
function wx(e, t) {
  const n = e.signingContext;
  n.kind === "solana-transaction-v1" && Bt();
  let r = _v(n);
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    if (e.scheme !== er)
      throw new b("invalid_payload", "Bitcoin Taproot contexts require the BIP341 key-path signing scheme");
  } else {
    if (e.scheme === er)
      throw new b("invalid_payload", "the BIP341 key-path signing scheme requires a Bitcoin Taproot context");
    if (n.kind !== "avault-agent-operation" && e.scheme !== Ai && e.scheme !== Qo)
      throw new b("invalid_payload", "EVM signing contexts require an ECDSA scheme");
  }
  let i;
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    const o = Rd(n, r.digest, t);
    r = { digest: o.digest, challenge: r.challenge, display: o.display }, i = o.facts;
  }
  return Yd(e, { kind: "digest", digest: r.digest }, t), {
    payload: { kind: "digest", digest: r.digest },
    display: `${hx(e.scheme, t)}
scheme: ${e.scheme}
${r.display}`,
    ...r.evm ? { review: ox(r.evm, { publicKey: t, scheme: e.scheme, digest: r.digest }) } : {},
    ...i ? { review: sx(i, { scheme: e.scheme, digest: r.digest }) } : {},
    ...gx(r.typedData, { publicKey: t, scheme: e.scheme, digest: r.digest })
  };
}
function gx(e, t) {
  if (e === void 0) return {};
  try {
    const n = Wv(e, t);
    return xl(JSON.parse(JSON.stringify(n))), { review: n };
  } catch {
    return {};
  }
}
function Jd(e) {
  const t = vn(e.material.envelope), n = os(e.material.name, t.recordMetadata);
  Iv(e.scheme, e.signingContext, n);
  const r = n.publicKey, i = e.signingContext.kind === "solana-transaction-v1" ? yx(e, r) : wx(e, r);
  return { unpacked: t, recordContext: n, content: i };
}
function mx(e) {
  const t = Xd(e), { unpacked: n, recordContext: r, content: i } = Jd(t), o = px(t);
  return Zs({ request: t, verified: i, unpacked: n, recordContext: r, challenge: [...o] });
}
async function Qd(e, t, n) {
  const r = Xd(e.request), i = Jd(r);
  (!e.verified || it(e.verified.payload) !== it(i.content.payload)) && Bt();
  const o = await cs(n, t);
  return pi({ context: r.context, rootMetadata: o, expectedPurpose: "sign" }), hi([r.context]), { request: r, ...i };
}
async function bx(e, t, n) {
  return (await Qd(e, t, n)).content;
}
async function vx(e, t, n) {
  const { request: r, content: i, unpacked: o, recordContext: s } = await Qd(e, t, n);
  return i.payload.kind === "message" ? kw(o.sealed, t, s, i.payload.messageHex) : vw(o.sealed, t, s, i.payload.digest, r.scheme);
}
function ef(e, t = e.label) {
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
function xx(e) {
  const { challenge: t, ...n } = e;
  return {
    ...n,
    ...t ? { challenge: J(t) } : {}
  };
}
function Ro(e, t) {
  if (typeof e != "object" || e === null) throw new b("invalid_payload", `${t} must be an object`);
  return e;
}
function Gt(e, t) {
  if (typeof e == "string" && e.length > 0) return e;
  if (typeof e != "object" || e === null) throw new b("invalid_payload", `${t} must be a text spec`);
  const n = e;
  if (typeof n.text == "string") return ge(n.text);
  if (typeof n.key == "string") {
    const r = n.params;
    if (r !== void 0 && (typeof r != "object" || r === null || Array.isArray(r)))
      throw new b("invalid_payload", `${t}.params must be an object`);
    return {
      key: n.key,
      ...r !== void 0 ? { params: r } : {}
    };
  }
  throw new b("invalid_payload", `${t} must be a text spec`);
}
function kx(e) {
  const t = Ro(e, "details");
  if (!Array.isArray(t.rows)) throw new b("invalid_payload", "details must contain rows");
  const n = [];
  for (const [r, i] of t.rows.entries()) {
    const o = Ro(i, `details.rows[${r}]`), s = o.variant;
    if (s !== void 0 && s !== "text" && s !== "command" && s !== "mono" && s !== "chips")
      throw new b("invalid_payload", `details.rows[${r}].variant is invalid`);
    const a = Gt(o.label, `details.rows[${r}].label`);
    if (s === "chips") {
      if (!Array.isArray(o.values)) throw new b("invalid_payload", `details.rows[${r}].values must be an array`);
      const c = o.tone;
      if (c !== void 0 && c !== "default" && c !== "warning")
        throw new b("invalid_payload", `details.rows[${r}].tone is invalid`);
      const u = c === "default" || c === "warning" ? c : void 0;
      n.push({
        label: a,
        values: o.values.map((l, d) => Gt(l, `details.rows[${r}].values[${d}]`)),
        variant: "chips",
        ...u !== void 0 ? { tone: u } : {}
      });
      continue;
    }
    n.push({
      label: a,
      value: Gt(o.value, `details.rows[${r}].value`),
      ...s !== void 0 ? { variant: s } : {}
    });
  }
  return { rows: n };
}
function Ex(e) {
  try {
    return xl(e);
  } catch (t) {
    throw new b("invalid_payload", t instanceof Error ? t.message : "signing review is malformed");
  }
}
function Sx(e) {
  const t = Ro(e, "authorization prompt");
  if (t.challenge !== void 0 && (typeof t.challenge != "string" || t.challenge.length === 0))
    throw new b("invalid_payload", "prompt.challenge must be a non-empty string");
  if (t.reviewSession !== void 0 && typeof t.reviewSession != "string")
    throw new b("invalid_payload", "prompt.reviewSession must be a string");
  const n = t.challenge === void 0 ? void 0 : fe(t.challenge);
  return {
    title: Gt(t.title, "prompt.title"),
    subtitle: Gt(t.subtitle, "prompt.subtitle"),
    ...t.body !== void 0 ? { body: Gt(t.body, "prompt.body") } : {},
    ...t.details !== void 0 ? { details: kx(t.details) } : {},
    ...t.review !== void 0 ? { review: Ex(t.review) } : {},
    ...t.reviewSession !== void 0 ? { reviewSession: t.reviewSession } : {},
    ...n ? { challenge: n } : {},
    label: Gt(t.label, "prompt.label")
  };
}
function tf(e) {
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
async function Ax(e) {
  let t = new TextEncoder().encode(e.value);
  try {
    const n = { name: e.name, kind: "static" }, r = await Zu(t, e.vmk, n);
    return { envelope: Yu(r, e.wrapMeta, n) };
  } finally {
    t?.fill(0), t = void 0;
  }
}
async function _x(e) {
  const t = An(e.curve), n = mw(t);
  let r = n.privateKey;
  try {
    const i = {
      name: e.name,
      kind: "keypair",
      publicKey: n.publicKey,
      ...t === "ed25519" ? { curve: t } : {}
    }, o = await Zu(r, e.vmk, i);
    return {
      envelope: Yu(o, e.wrapMeta, i),
      publicKey: n.publicKey,
      addresses: t === "ed25519" ? { solana: Ew(n.publicKey) } : ps(n.publicKey)
    };
  } finally {
    r?.fill(0), r = void 0;
  }
}
function nf(e) {
  return (e?.kind ?? "static") === "static";
}
function Ix(e, t) {
  if (typeof e != "object" || e === null) throw new b("invalid_payload", "approveRelease item must be an object");
  const n = e;
  return {
    material: t(n.material),
    context: Bs(n.context)
  };
}
function rf(e) {
  const t = e[0]?.context.display;
  if (!t) throw new b("invalid_payload", "approveRelease requires at least one item");
  if (e[0].context.purpose !== "agent-deliver") throw new b("invalid_context", "approveRelease only accepts agent-deliver contexts");
  const n = Qa(e[0].context), r = Rc(e[0].context);
  for (const s of e) {
    if (s.context.purpose !== "agent-deliver") throw new b("invalid_context", "approveRelease only accepts agent-deliver contexts");
    if (Qa(s.context) !== n)
      throw new b("invalid_context", "approveRelease batch contexts must share one signed display block");
    if (Rc(s.context) !== r)
      throw new b("invalid_context", "approveRelease batch contexts must share one signed recipient");
  }
  const i = new Set(t.secrets.map((s) => `${s.name}:${s.kind}`));
  for (const s of e) {
    const { recordMetadata: a } = vn(s.material.envelope), c = a?.kind ?? "static";
    if (!i.has(`${s.material.name}:${c}`))
      throw new b("invalid_context", "signed display block does not cover every release item");
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
function Rc(e) {
  if (!e.grantId) throw new b("invalid_context", "agent delivery context is missing grantId");
  if (!e.agent) throw new b("invalid_context", "agent delivery context is missing agent key");
  return it({ agent: e.agent, grantId: e.grantId });
}
async function Px(e) {
  if (e.items.length === 0) throw new b("invalid_payload", "approveRelease requires at least one item");
  const t = rf(e.items), n = await cs(e.wrapMeta, e.vmk), r = e.items.map((a) => a.context);
  for (const a of e.items)
    pi({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: e.now
    });
  hi(r, e.now);
  const i = await Yg(r);
  await e.confirm({
    ...t,
    challenge: i
  });
  const o = e.now ?? Date.now();
  for (const a of e.items)
    pi({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: o
    });
  e.onApprovalAccepted?.(o), hi(r, o);
  const s = [];
  for (const a of e.items) {
    const { sealed: c, recordMetadata: u } = vn(a.material.envelope);
    if (!nf(u)) throw new b("invalid_payload", "approveRelease requires static protected records");
    s.push(
      await gw(
        c,
        e.vmk,
        Xg(a.context),
        os(a.material.name, u),
        await Zg(a.context, a.material.name)
      )
    );
  }
  return e.consumeReplayIds !== !1 && await Bi(r, o), { blindBoxes: s };
}
const of = `avibe-vault-setup:v1:${ie.buildHash}:`, Tx = `avibe-vault-setup-result:${ie.buildHash}:`, Bx = `avibe-vault-setup-error:${ie.buildHash}:`, $x = 300 * 1e3, Ox = 300 * 1e3, Rx = 30 * 1e3, Lx = 8 * 1e3, Ie = new Af();
let Lc = !1;
function Cx(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return (t.locale === "en" || t.locale === "zh") && (t.theme === "light" || t.theme === "dark");
}
function sf(e) {
  if (!Cx(e))
    throw new b("invalid_payload", "appearance must include locale and theme");
  return e;
}
function zx(e) {
  return e == null ? null : sf(e);
}
function Ui(e) {
  const t = yg(e.locale);
  document.documentElement.setAttribute("data-theme", e.theme), t && bg();
}
function af() {
  const e = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  return { locale: wg(), theme: e };
}
function je(e) {
  if (typeof e != "object" || e === null)
    throw new b("invalid_payload", "request payload must be an object");
  return e;
}
function cf(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new b("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ae(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new b("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function uf(e, t) {
  if (typeof e != "object" || e === null) throw new b("invalid_payload", `${t} must be an object`);
  return e;
}
function Ux(e) {
  const t = uf(e, "envelope");
  return {
    ciphertext: Ae(t.ciphertext, "envelope.ciphertext"),
    nonce: Ae(t.nonce, "envelope.nonce"),
    wrap_meta: Ae(t.wrap_meta, "envelope.wrap_meta")
  };
}
function lf(e) {
  const t = uf(e, "material");
  return { name: Ae(t.name, "material.name"), envelope: Ux(t.envelope) };
}
function Nx(e) {
  const t = e == null ? {} : je(e);
  return { wrapMeta: cf(t.wrapMeta, "wrapMeta") };
}
function df(e) {
  const t = je(e), n = !!t.existingProtectedVault;
  if (n && t.rootMetadata !== void 0)
    throw new b("invalid_payload", "vault root metadata cannot be replaced during setup");
  return {
    vaultUserHandle: Ae(t.vaultUserHandle, "vaultUserHandle"),
    displayName: Ae(t.displayName, "displayName"),
    existingProtectedVault: n,
    authzCreationOptions: t.authzCreationOptions,
    ...t.rootMetadata !== void 0 ? { rootMetadata: t.rootMetadata } : {},
    ...t.policy !== void 0 ? { policy: ys(t.policy) } : {}
  };
}
function Mx(e) {
  const t = je(e);
  return {
    wrapMeta: Ae(t.wrapMeta, "wrapMeta"),
    ...t.policy !== void 0 ? { policy: ys(t.policy) } : {}
  };
}
function Hx(e) {
  const t = je(e);
  return { material: lf(t.material), context: Bs(t.context) };
}
function Fx(e) {
  const t = je(e);
  if (!Array.isArray(t.items) || t.items.length === 0)
    throw new b("invalid_payload", "approveRelease requires a non-empty items array");
  return { items: t.items.map((n) => Ix(n, lf)) };
}
function Kx(e) {
  const t = je(e);
  if (t.operation !== "delete_secret") throw new b("invalid_payload", "deleteAuthzAssertion only supports delete_secret");
  return {
    challengeId: Ae(t.challengeId, "challengeId"),
    operation: "delete_secret",
    secretName: Ae(t.secretName, "secretName"),
    webauthn: t.webauthn
  };
}
function ff() {
  if (typeof crypto.randomUUID == "function") return crypto.randomUUID();
  const e = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(e, (t) => t.toString(16).padStart(2, "0")).join("");
}
async function Cc(e) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(e)));
}
function Dx(e) {
  return new Promise((t, n) => {
    const r = () => {
      n(Pn(e));
    };
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function jx(e) {
  for (const t of e)
    if (t.aborted) return Pn(t);
  return new Error("operation-superseded");
}
function pf(e) {
  const t = new AbortController(), n = [], r = (i) => {
    t.signal.aborted || t.abort(Pn(i));
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
async function hf(e) {
  const t = pf(e.abortSignals);
  try {
    await Promise.race([
      ug(rs(e.wrapMeta), mt(), e.challenge, t.signal).catch((n) => {
        throw t.signal.aborted ? jx(e.abortSignals) : n;
      }),
      ...e.abortSignals.map((n) => Dx(n))
    ]);
  } finally {
    t.cleanup();
  }
}
async function Ys(e) {
  await dr(async (t) => {
    const n = async (i) => {
      const o = pf([t, ...e.abortSignal ? [e.abortSignal] : []]);
      try {
        const s = await Zx(
          {
            prompt: zc(e),
            wrapMeta: e.wrapMeta,
            passkey: e.passkey
          },
          o.signal,
          i
        );
        if (e.passkey === "unlock") {
          if (s.kind !== "unlock") throw new Error("authorization unlock result is missing");
          await _l({
            wrapMeta: e.wrapMeta,
            currentRpId: mt(),
            assertion: {
              prfOutput: fe(s.prfOutput),
              prfSalt: fe(s.prfSalt)
            },
            abortSignal: o.signal,
            ...e.policy ? { policy: e.policy } : {}
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
    await Is(ef(zc(e), "common.continue"), t, void 0, n);
  });
}
function zc(e) {
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
function Vx(e) {
  return {
    ...e,
    prompt: xx(e.prompt)
  };
}
function qx(e) {
  const t = je(e);
  if (!t) throw new b("invalid_payload", "authorization request must be an object");
  const n = t.passkey;
  if (n !== "none" && n !== "unlock" && n !== "uv")
    throw new b("invalid_payload", "authorization passkey requirement is invalid");
  return {
    prompt: Sx(t.prompt),
    wrapMeta: Ae(t.wrapMeta, "wrapMeta"),
    passkey: n
  };
}
function Gx(e) {
  return Hc("authorize", e, af());
}
function Wx(e) {
  const t = je(e);
  if (!t) throw new b("authorization_result_invalid", "authorization result is invalid");
  if (t.kind === "confirmed") return { kind: "confirmed" };
  if (t.kind === "unlock")
    return {
      kind: "unlock",
      prfOutput: Ae(t.prfOutput, "result.prfOutput"),
      prfSalt: Ae(t.prfSalt, "result.prfSalt")
    };
  throw new b("authorization_result_invalid", "authorization result kind is invalid");
}
function Zx(e, t, n) {
  Wt(t);
  const r = n ?? ff(), i = `avibe-vault-authorization-${r}`;
  return new Promise((o, s) => {
    let a = null, c = !1, u = null, l = null, d = null, h = null;
    const f = () => v(() => s(Pn(t))), g = () => v(() => s(new b("authorization_cancelled", "requesting sandbox was unloaded", !0))), m = () => new b("authorization_window_closed", "authorization window was closed", !0), p = (x) => {
      a = x, l = setInterval(() => {
        !x.closed || d || (d = setTimeout(() => v(() => s(m())), 2500));
      }, 500);
    }, y = (x) => {
      if (x.origin !== window.location.origin) return;
      const I = x.data;
      if (!(!I || I.id !== r || !dn(I.build)) && (!a && n && I.type === "authorization-ready" && x.source && (h && clearTimeout(h), h = null, p(x.source)), x.source === a))
        if (I.type === "authorization-ready")
          try {
            a?.postMessage(
              {
                type: "authorization-request",
                build: ie,
                id: r,
                request: Vx(e)
              },
              window.location.origin
            );
          } catch (E) {
            v(() => s(E));
          }
        else if (I.type === "authorization-approved")
          try {
            const E = Wx(I.result);
            v(() => o(E));
          } catch (E) {
            v(() => s(E));
          }
        else I.type === "authorization-error" && v(() => s(new b(I.code, I.message, !!I.retryable)));
    }, v = (x) => {
      if (!c) {
        c = !0, u && clearTimeout(u), l && clearInterval(l), d && clearTimeout(d), h && clearTimeout(h), window.removeEventListener("message", y), window.removeEventListener("pagehide", g), t?.removeEventListener("abort", f);
        try {
          a && !a.closed && a.close();
        } catch {
        }
        x();
      }
    };
    if (window.addEventListener("message", y), window.addEventListener("pagehide", g, { once: !0 }), n)
      h = setTimeout(() => v(() => s(m())), Rx);
    else {
      const x = window.open(Gx(r), i, "popup,width=520,height=760");
      if (!x) {
        v(() => s(new b("authorization_popup_blocked", "authorization window was blocked", !0)));
        return;
      }
      x.focus(), p(x);
    }
    if (u = setTimeout(
      () => v(() => s(new b("authorization_window_timeout", "authorization window timed out", !0))),
      Ox
    ), t) {
      if (t.aborted) {
        f();
        return;
      }
      t.addEventListener("abort", f, { once: !0 });
    }
  });
}
function Xx(e) {
  return e === 0 ? "context.oneTime" : e % 60 === 0 ? nr("context.minutes", { count: e / 60 }) : nr("context.seconds", { count: e });
}
function ur(e, t, n = "access") {
  const r = [];
  e.sessionLabel && r.push({ label: "context.session", value: ge(e.sessionLabel) }), e.command && r.push({ label: "context.command", value: ge(e.command), variant: "command" }), e.egress && r.push({ label: n === "sign" ? "context.signReturns" : "context.egress", value: ge(e.egress) });
  const i = [
    ...e.source?.env?.map((o) => ge(`${F(n === "sign" ? "context.sourceRequestedName" : "context.sourceEnv")}: ${o}`)) ?? [],
    ...e.source?.tags?.map((o) => ge(`${F("context.sourceTag")}: ${o}`)) ?? [],
    ...e.source?.skills?.map((o) => ge(`${F("context.sourceSkill")}: ${o}`)) ?? []
  ];
  return i.length > 0 && r.push({ label: "context.source", values: i, variant: "chips" }), t?.agentFingerprint && r.push({ label: "context.agent", value: ge(t.agentFingerprint), variant: "mono" }), t?.grantId && r.push({ label: "context.grant", value: ge(t.grantId), variant: "mono" }), e.grantTtlSeconds !== void 0 && r.push({ label: "context.agentAccess", value: Xx(e.grantTtlSeconds) }), r.push({
    label: "context.secrets",
    values: e.secrets.map(
      (o) => ge(`${o.name} · ${F(o.kind === "static" ? "context.kindStatic" : "context.kindKeypair")}`)
    ),
    variant: "chips",
    tone: "warning"
  }), { rows: r };
}
function Yx(e) {
  return e instanceof Error && e.message === "vault-locked";
}
function Jx(e) {
  return e instanceof Error && e.message === "vault-operation-aborted";
}
function yf(e, t) {
  return Yx(e) ? !0 : Jx(e) && gt(t).state === "locked";
}
async function Qx(e, t = Ee(), n) {
  await Al({ wrapMeta: e, currentRpId: mt(), policy: t, abortSignal: n });
}
async function e1(e, t = Ee()) {
  await dr((n) => Qx(e, t, n));
}
async function wf(e) {
  try {
    await Ys({ ...e, passkey: "unlock" });
  } catch (t) {
    throw gt(e.wrapMeta).state === "unlocked" && Ke({ broadcast: !0, reason: "manual-lock" }), t;
  }
}
async function t1(e, t) {
  if (gt(e).state !== "unlocked") {
    if (!e) throw new Error("vault-locked");
    await e1(e);
  }
  return await bs(t, { renewOnSuccess: !0 });
}
async function gf(e) {
  const t = gt(e.wrapMeta).state, n = Ee(), r = tf({ tier: e.tier, vaultState: t, policy: n });
  let i = !1;
  const o = async () => {
    await wf({
      ...e.lockedPrompt,
      wrapMeta: e.wrapMeta,
      policy: n,
      parentSurface: e.parentSurface,
      authorizationWindow: e.authorizationWindow
    }), i = !0;
  }, s = async (a, c = !1) => {
    let u = !1;
    try {
      const l = await bs(
        async (d, h, f) => {
          const g = await e.buildPrompt(d, h, f);
          return r.confirm && !c && (await Ys({
            ...g,
            wrapMeta: h,
            passkey: a,
            abortSignal: f.signal,
            parentSurface: e.parentSurface,
            authorizationWindow: e.authorizationWindow
          }), f.assertCurrent()), e.operation(d, h, f);
        },
        { renewOnSuccess: r.renewOnSuccess, beforeSuccess: e.beforeSuccess }
      );
      return u = !0, l;
    } catch (l) {
      throw i && !u && gt().state === "unlocked" && Ke({ broadcast: !0, reason: "manual-lock" }), l;
    }
  };
  try {
    return r.passkey === "unlock" ? (await o(), await s("none", !0)) : await s(r.passkey === "uv" ? "uv" : "none");
  } catch (a) {
    if (r.passkey !== "unlock" && yf(a, e.wrapMeta))
      return await o(), await s("none", !0);
    throw a;
  }
}
function Ve(e, t) {
  if (e instanceof b) return e;
  if (pg(e)) return new b("passkey_cancelled", "passkey-cancelled", !0);
  const n = e instanceof Error ? e.message : t;
  switch (n) {
    case "passkey-cancelled":
      return new b("passkey_cancelled", n, !0);
    case "passkey-prf-unavailable":
      return new b("passkey_prf_unavailable", n);
    case "passkey-not-configured":
      return new b("passkey_not_configured", n);
    case "passkey-multiple-not-supported":
      return new b("passkey_multiple_not_supported", n);
    case "vault-operation-aborted":
      return new b("vault_operation_aborted", n, !0);
    default:
      return new b(t, n);
  }
}
function n1(e) {
  return J(new TextEncoder().encode(JSON.stringify(e))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function r1(e) {
  return df(JSON.parse(new TextDecoder().decode(fe(e))));
}
function i1(e, t) {
  return Hc("setup", e, af(), { req: n1(t) });
}
function lo(e) {
  const t = je(e);
  return {
    credentialId: Ae(t.credentialId, "credentialId"),
    ...t.authzRegistration !== void 0 ? { authzRegistration: t.authzRegistration } : {}
  };
}
function Ni(e) {
  return `${Tx}${e}`;
}
function Mi(e) {
  return `${Bx}${e}`;
}
function Uc(e) {
  try {
    localStorage.removeItem(Ni(e)), localStorage.removeItem(Mi(e));
  } catch {
  }
}
function o1(e, t) {
  try {
    localStorage.setItem(Ni(e), JSON.stringify({ build: ie, result: t })), localStorage.removeItem(Mi(e));
  } catch {
  }
}
function s1(e, t) {
  try {
    localStorage.setItem(
      Mi(e),
      JSON.stringify({ build: ie, code: t.code, message: t.message, retryable: t.retryable })
    ), localStorage.removeItem(Ni(e));
  } catch {
  }
}
function mf(e) {
  if (window.self === window.top)
    return ks({
      rpId: mt(),
      vaultUserHandle: e.vaultUserHandle,
      displayName: e.displayName,
      authzCreationOptions: e.authzCreationOptions
    });
  const t = ff(), n = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${of}${t}`);
  let r = null;
  return Uc(t), new Promise((i, o) => {
    let s = !1, a = null, c = null, u = null;
    const l = () => d(() => o(new b("setup_window_closed", "requesting sandbox was unloaded", !0))), d = (m) => {
      if (!s) {
        s = !0, a && clearTimeout(a), c && clearInterval(c), u && clearTimeout(u), window.removeEventListener("visibilitychange", f), window.removeEventListener("focus", f), window.removeEventListener("storage", f), window.removeEventListener("message", g), window.removeEventListener("pagehide", l), n?.close(), Uc(t);
        try {
          r?.close();
        } catch {
        }
        m();
      }
    }, h = () => {
      let m = null, p = null;
      try {
        m = localStorage.getItem(Ni(t)), p = localStorage.getItem(Mi(t));
      } catch {
        return !1;
      }
      if (m) {
        try {
          const y = je(JSON.parse(m));
          if (!dn(y.build)) throw new b("sandbox_build_mismatch");
          const v = lo(y.result);
          d(() => i(v));
        } catch (y) {
          const v = y instanceof Error ? y.message : "stored setup result is invalid";
          d(() => o(new b("setup_storage_invalid", v)));
        }
        return !0;
      }
      if (p) {
        try {
          const y = je(JSON.parse(p));
          if (!dn(y.build)) throw new b("sandbox_build_mismatch");
          d(
            () => o(new b(Ae(y.code, "code"), cf(y.message, "message"), !!y.retryable))
          );
        } catch (y) {
          const v = y instanceof Error ? y.message : "stored setup error is invalid";
          d(() => o(new b("setup_storage_invalid", v)));
        }
        return !0;
      }
      return !1;
    }, f = () => {
      h();
    }, g = (m) => {
      if (m.origin !== window.location.origin || m.source !== r) return;
      const p = m.data;
      !p || p.id !== t || !dn(p.build) || (p.type === "setup-created" ? d(() => i(lo(p.result))) : p.type === "setup-error" && d(() => o(new b(p.code, p.message, !!p.retryable))));
    };
    if (a = setTimeout(() => {
      h() || d(() => o(new b("setup_window_timeout", "setup window timed out", !0)));
    }, $x), c = setInterval(() => {
      !r?.closed || u || h() || (u = setTimeout(() => {
        h() || d(() => o(new b("setup_window_closed", "setup window was closed", !0)));
      }, 2500));
    }, 500), n && (n.onmessage = (m) => {
      const p = m.data;
      !p || p.id !== t || !dn(p.build) || (p.type === "setup-created" ? d(() => i(lo(p.result))) : p.type === "setup-error" && d(() => o(new b(p.code, p.message, !!p.retryable))));
    }), window.addEventListener("visibilitychange", f), window.addEventListener("focus", f), window.addEventListener("storage", f), window.addEventListener("message", g), window.addEventListener("pagehide", l, { once: !0 }), r = window.open(i1(t, e), "avibe-vault-setup", "popup,width=440,height=620"), !r) {
      d(() => o(new b("setup_popup_blocked", "setup window was blocked", !0)));
      return;
    }
    r.focus();
  });
}
function Pn(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function Wt(e) {
  if (e?.aborted) throw Pn(e);
}
function a1() {
  const e = window.navigator;
  return /iP(?:hone|ad|od)/.test(e.userAgent) || e.platform === "MacIntel" && e.maxTouchPoints > 1;
}
function Nc(e) {
  return a1() && bl(e);
}
async function bf(e, t, n, r, i = Ee()) {
  const o = Wy();
  let s, a;
  try {
    Wt(r), s = (await xs([{ credentialId: n.credentialId, prfSalt: o }], t, r)).prfOutput, Wt(r), a = Gy();
    const u = await Yy(a, [{ kind: "passkey", prfOutput: s, prfSalt: o, credentialId: n.credentialId }]);
    Wt(r);
    let l = ew(u, {
      rpId: t,
      vaultUserHandle: e.vaultUserHandle
    });
    e.rootMetadata && (l = rw(l, await nw(a, e.rootMetadata, l)), Wt(r));
    const d = pl({
      vmk: a,
      wrapMeta: l,
      freshSetup: !e.existingProtectedVault,
      scopeId: Dw(e.vaultUserHandle),
      policy: i
    });
    return a = void 0, {
      wrapMeta: l,
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
function c1(e, t, n) {
  return dr((r) => {
    const i = Ss(
      "setup.title",
      "setup.subtitle",
      "setup.iframeBody"
    ), o = rr("setup.createPasskey"), s = Sl("setup.ready");
    return fn(i.card, o), fn(i.card, s), new Promise((a, c) => {
      let u = !1, l = "create", d = null, h = !1;
      const f = (E) => {
        u || (u = !0, r.removeEventListener("abort", p), As(), E());
      }, g = (E) => {
        f(() => c(E));
      }, m = (E) => {
        u || (l = "create", d = null, W(o, "setup.createPasskey"), o.disabled = !1, W(s, E));
      }, p = () => {
        g(Pn(r));
      }, y = (E) => {
        if (E instanceof b && E.code === "setup_popup_blocked") {
          m("setup.popupBlocked");
          return;
        }
        g(E);
      }, v = () => {
        h = !0, o.disabled = !0, W(s, "setup.popupPrompt");
        let E;
        try {
          E = mf(e);
        } catch ($) {
          y($);
          return;
        }
        E.then(
          ($) => {
            u || (d = $, l = "finish", W(o, "setup.finish"), o.disabled = !1, W(s, "setup.createdFinish"));
          },
          ($) => {
            y($);
          }
        );
      }, x = (E) => {
        o.disabled = !0, W(s, "setup.passkeyPrompt"), bf(e, t, E, r, n).then(
          ($) => f(() => a($)),
          ($) => g($)
        );
      }, I = () => {
        o.disabled = !0, W(s, "setup.passkeyPrompt");
        let E;
        try {
          E = ks({
            rpId: t,
            vaultUserHandle: e.vaultUserHandle,
            displayName: e.displayName,
            authzCreationOptions: e.authzCreationOptions
          });
        } catch ($) {
          if (Nc($)) {
            v();
            return;
          }
          g($);
          return;
        }
        E.then(
          ($) => x($),
          ($) => {
            if (Nc($)) {
              v();
              return;
            }
            g($);
          }
        );
      };
      if (r.aborted) {
        p();
        return;
      }
      r.addEventListener("abort", p, { once: !0 }), o.addEventListener("click", () => {
        if (!(u || o.disabled)) {
          if (l === "finish" && d) {
            x(d);
            return;
          }
          if (h) {
            v();
            return;
          }
          I();
        }
      });
    });
  });
}
async function u1(e) {
  const t = df(e);
  try {
    const n = mt(), r = t.policy ?? Ee(), i = window.self !== window.top ? await c1(t, n, r) : await bf(t, n, await mf(t), void 0, r), o = t.policy ? ws(r) : Ee();
    return { ...i, policy: o };
  } catch (n) {
    throw Ve(n, "setup_failed");
  }
}
async function l1(e) {
  const t = Mx(e);
  try {
    const n = mt(), r = t.policy ?? Ee(), i = await dr(async (s) => {
      let a;
      if (await Is(
        {
          title: "unlock.sessionTitle",
          subtitle: nr("unlock.sessionSubtitle", {
            minutes: r.windowSeconds / 60
          }),
          body: "unlock.sessionBody",
          confirmLabel: "common.continue"
        },
        s,
        void 0,
        async () => {
          a = await Al({ wrapMeta: t.wrapMeta, currentRpId: n, abortSignal: s, policy: r });
        }
      ), !a) throw new Error("vault unlock did not complete");
      return a;
    }), o = t.policy ? ws(r) : Ee();
    return { ...i, policy: o };
  } catch (n) {
    throw Ve(n, "unlock_failed");
  }
}
async function d1(e) {
  const t = Ug(e);
  t.wrapMeta && vs(t.wrapMeta);
  try {
    if (t.kind === "static" && !Ee().parentValueSealAllowed)
      throw new b("parent_value_seal_disabled", "parent-value sealing is disabled by policy");
    return await t1(t.wrapMeta, async (n, r) => ({ ...t.kind === "static" ? await Ax({ name: t.name, value: t.value, vmk: n, wrapMeta: r }) : await _x({ name: t.name, curve: t.curve, vmk: n, wrapMeta: r }), establishingVmk: Xw() }));
  } catch (n) {
    throw Ve(n, "seal_failed");
  }
}
async function f1(e, t) {
  const n = Hx(e);
  try {
    const { sealed: r, vmkWrapMeta: i, recordMetadata: o } = vn(n.material.envelope);
    if (o?.kind === "keypair") throw new Error("keypair protected records cannot be revealed as plaintext");
    return So({
      context: n.context,
      expectedPurpose: "reveal",
      secret: { name: n.material.name, kind: "static" }
    }), await gf({
      tier: "R2",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: {
        title: "reveal.showConfirmTitle",
        subtitle: ge(n.material.name),
        body: "reveal.confirmBody",
        details: ur(n.context.display),
        label: "common.continue"
      },
      buildPrompt: async (s, a) => {
        const c = await cs(a, s);
        return pi({ context: n.context, rootMetadata: c, expectedPurpose: "reveal" }), So({
          context: n.context,
          expectedPurpose: "reveal",
          secret: { name: n.material.name, kind: "static" }
        }), hi([n.context]), {
          title: "reveal.showConfirmTitle",
          subtitle: ge(n.material.name),
          body: "reveal.confirmBody",
          details: ur(n.context.display),
          challenge: await Cc(Ti(n.context)),
          label: "common.continue"
        };
      },
      operation: async (s, a, c) => {
        const u = await as(r, s, os(n.material.name, o));
        try {
          c.assertCurrent(), await Bi([n.context]), await Rg({
            name: n.material.name,
            plaintext: u,
            abortSignal: c.signal,
            prepareCopy: Ee().strictApprovals ? async (l) => {
              await hf({
                wrapMeta: a,
                challenge: await Cc(`reveal-copy:${n.context.requestId}:${n.material.name}`),
                abortSignals: [l, c.signal]
              }), Wt(l), Wt(c.signal);
            } : void 0
          });
        } finally {
          u.fill(0);
        }
        return { completed: !0 };
      }
    });
  } catch (r) {
    throw Ve(r, "reveal_failed");
  }
}
async function p1(e, t) {
  try {
    const n = mx(e), { request: r, unpacked: { vmkWrapMeta: i } } = n, o = (s) => {
      const a = r.context.display.sessionLabel;
      if (!s.review && s.display === void 0) throw new b("invalid_payload", "signing request has no reviewable content");
      return {
        title: "sign.title",
        subtitle: ge(r.material.name),
        ...s.review ? { review: s.review, ...a ? { reviewSession: a } : {} } : { body: ge(s.display ?? "") },
        details: ur(r.context.display, void 0, "sign"),
        label: "sign.confirm"
      };
    };
    return await gf({
      tier: "R3",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: o(n.verified),
      // Shown only after the daemon signature, expiry and replay state are
      // verified with the vault key, from content re-derived at that moment.
      buildPrompt: async (s, a) => ({
        ...o(await bx(n, s, a)),
        challenge: new Uint8Array(n.challenge)
      }),
      operation: (s, a) => vx(n, s, a),
      beforeSuccess: () => Bi([r.context])
    });
  } catch (n) {
    throw Ve(n, "sign_failed");
  }
}
async function h1(e, t) {
  const n = Fx(e);
  try {
    const i = vn(n.items[0].material.envelope).vmkWrapMeta;
    for (const h of n.items) {
      const f = vn(h.material.envelope);
      if (f.vmkWrapMeta !== i) throw new b("invalid_payload", "approveRelease items must share one VMK context");
      if (!nf(f.recordMetadata)) throw new b("invalid_payload", "approveRelease only supports static protected records");
    }
    const o = rf(n.items), s = gt(i).state, a = Ee(), c = tf({ tier: "R2", vaultState: s, policy: a });
    let u = !1;
    const l = async () => {
      await wf({
        title: "release.title",
        subtitle: nr("release.subtitle", { count: n.items.length }),
        details: ur(o.display, o.recipient),
        label: "release.confirm",
        wrapMeta: i,
        policy: a,
        parentSurface: t.latestSurface,
        authorizationWindow: t.takeAuthorizationWindow
      }), u = !0;
    }, d = async (h, f = !1) => {
      let g = !1, m;
      try {
        const p = await bs(
          async (y, v, x) => {
            const I = await Px({
              items: n.items,
              vmk: y,
              wrapMeta: v,
              consumeReplayIds: !1,
              onApprovalAccepted: (E) => {
                m = E;
              },
              confirm: (E) => !c.confirm || f ? Promise.resolve() : Ys({
                title: "release.title",
                subtitle: nr("release.subtitle", { count: n.items.length }),
                details: ur(E.display, E.recipient),
                challenge: E.challenge,
                label: "release.confirm",
                wrapMeta: v,
                passkey: h,
                abortSignal: x.signal,
                parentSurface: t.latestSurface,
                authorizationWindow: t.takeAuthorizationWindow
              })
            });
            return x.assertCurrent(), I;
          },
          {
            renewOnSuccess: c.renewOnSuccess,
            beforeSuccess: () => Bi(n.items.map((y) => y.context), m)
          }
        );
        return g = !0, p;
      } catch (p) {
        throw u && !g && gt().state === "unlocked" && Ke({ broadcast: !0, reason: "manual-lock" }), p;
      }
    };
    try {
      return c.passkey === "unlock" ? (await l(), await d("none", !0)) : await d(c.passkey === "uv" ? "uv" : "none");
    } catch (h) {
      if (c.passkey !== "unlock" && yf(h, i))
        return await l(), await d("none", !0);
      throw h;
    }
  } catch (r) {
    throw Ve(r, "approve_release_failed");
  }
}
async function y1(e) {
  const t = Kx(e);
  try {
    const n = await lg(t.webauthn);
    return { challengeId: t.challengeId, assertion: n };
  } catch (n) {
    throw Ve(n, "delete_authz_failed");
  }
}
function w1() {
  const e = Lo();
  if (e.get("mode") !== "setup") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Ui({ locale: t, theme: n });
  const r = e.get("id"), i = document.getElementById("page"), o = i?.querySelector(".card"), s = i?.querySelector("h1"), a = i?.querySelector(".sub"), c = i?.querySelector(".body");
  if (!r || !o || !s || !a || !c) return;
  document.body.classList.add("setup-mode"), W(s, "setup.title"), W(a, "setup.subtitle"), W(c, "setup.topLevelBody");
  const u = document.createElement("button");
  u.type = "button", u.className = "action", W(u, "setup.createPasskey");
  const l = document.createElement("p");
  l.className = "setup-status", W(l, "setup.readyOnOrigin"), o.append(u, l);
  const d = new URLSearchParams(window.location.hash.slice(1)).get("req");
  if (!d) {
    u.disabled = !0, W(l, "setup.requestMissing");
    return;
  }
  let h;
  try {
    h = r1(d);
  } catch {
    u.disabled = !0, W(l, "setup.requestInvalid");
    return;
  }
  const f = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${of}${r}`), g = (m) => {
    const p = Ve(m, "setup_create_failed"), y = {
      type: "setup-error",
      build: ie,
      id: r,
      code: p.code,
      message: p.message,
      retryable: p.retryable
    };
    s1(r, p), f?.postMessage(y);
    try {
      window.opener?.postMessage(y, window.location.origin);
    } catch {
    }
    u.disabled = !1, W(l, ge(p.message));
  };
  u.addEventListener("click", () => {
    u.disabled = !0, W(l, "setup.passkeyPrompt");
    let m;
    try {
      m = ks({
        rpId: mt(),
        vaultUserHandle: h.vaultUserHandle,
        displayName: h.displayName,
        authzCreationOptions: h.authzCreationOptions
      });
    } catch (p) {
      g(p);
      return;
    }
    m.then((p) => {
      const y = { type: "setup-created", build: ie, id: r, result: p };
      o1(r, p), f?.postMessage(y);
      try {
        window.opener?.postMessage(y, window.location.origin);
      } catch {
      }
      W(l, "setup.created"), setTimeout(() => window.close(), 250);
    }).catch(g);
  });
}
function g1() {
  const e = Lo();
  if (e.get("mode") !== "authorize") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Ui({ locale: t, theme: n });
  const r = e.get("id"), o = document.getElementById("page")?.querySelector(".card"), s = o?.querySelector(".body");
  if (!r || !o || !s || (s.classList.add("waiting"), W(s, window.opener ? "authorize.waiting" : "authorize.waitingLong"), !window.opener)) return;
  const a = () => {
    const p = window.opener;
    if (!p) return [];
    const y = [p];
    try {
      for (let v = 0; v < p.frames.length; v += 1) y.push(p.frames[v]);
    } catch {
    }
    return y;
  };
  let c = null;
  const u = (p) => {
    try {
      c?.postMessage(p, window.location.origin);
    } catch {
    }
  };
  let l = !1, d = null;
  const h = setTimeout(() => W(s, "authorize.waitingLong"), Lx), f = () => {
    if (!l)
      for (const p of a())
        try {
          p.postMessage({ type: "authorization-ready", build: ie, id: r }, window.location.origin);
        } catch {
        }
  }, g = (p) => {
    if (l) return;
    l = !0, d && clearInterval(d), clearTimeout(h), s.classList.remove("waiting"), window.removeEventListener("message", m), document.body.classList.add("setup-mode");
    const y = new AbortController();
    let v = { kind: "confirmed" };
    const x = async () => {
      if (p.passkey === "none") return;
      if (p.passkey === "uv") {
        if (!p.prompt.challenge) throw new b("invalid_payload", "authorization challenge is missing");
        await hf({
          wrapMeta: p.wrapMeta,
          challenge: p.prompt.challenge,
          abortSignals: [y.signal]
        });
        return;
      }
      const I = await xs(rs(p.wrapMeta), mt(), y.signal);
      v = {
        kind: "unlock",
        prfOutput: J(I.prfOutput),
        prfSalt: J(I.prfSalt)
      }, I.prfOutput.fill(0);
    };
    Is(ef(p.prompt), y.signal, void 0, x).then(
      () => {
        u({ type: "authorization-approved", build: ie, id: r, result: v }), setTimeout(() => window.close(), 250);
      },
      (I) => {
        const E = Ve(I, I instanceof Error && I.message === "operation-cancelled" ? "authorization_cancelled" : "authorization_failed");
        u({
          type: "authorization-error",
          build: ie,
          id: r,
          code: E.code,
          message: E.message,
          retryable: E.retryable
        }), setTimeout(() => window.close(), 250);
      }
    );
  }, m = (p) => {
    if (p.origin !== window.location.origin) return;
    const y = p.data;
    if (!y || y.type !== "authorization-request" || y.id !== r || !dn(y.build)) return;
    const v = a().find((x) => x === p.source);
    if (v) {
      c = v;
      try {
        g(qx(y.request));
      } catch (x) {
        const I = Ve(x, "authorization_request_invalid");
        u({
          type: "authorization-error",
          build: ie,
          id: r,
          code: I.code,
          message: I.message,
          retryable: I.retryable
        });
      }
    }
  };
  window.addEventListener("message", m), f(), d = setInterval(f, 500);
}
function m1() {
  if (window.self !== window.top || Lo().has("mode")) return;
  const e = document.querySelector("#page .body");
  e && W(e, "sandbox.intro");
}
Ie.register("handshake", (e) => {
  const t = e ?? {}, n = typeof t.expectedBuildHash == "string" ? t.expectedBuildHash : null;
  if (typeof t.parentOrigin != "string" || typeof t.nonce != "string" || t.nonce.length < 16)
    throw new b("invalid_handshake", "handshake must include parentOrigin and nonce");
  if (n !== null && n !== ie.buildHash)
    throw new b("build_hash_mismatch", "sandbox build hash does not match parent expectation");
  const r = zx(t.appearance);
  r && Ui(r);
  const i = Lc ? Ee() : ws(t.policy === void 0 ? void 0 : ys(t.policy));
  return Lc = !0, {
    accepted: !0,
    channel: Kt,
    version: 2,
    sandboxOrigin: window.location.origin,
    build: ie,
    policy: i
  };
});
Ie.register("set-appearance", (e) => (Ui(sf(e)), { accepted: !0 }));
Ie.register("status", (e) => {
  const t = Nx(e);
  try {
    t.wrapMeta && gt(t.wrapMeta);
    const n = gt(), r = Ee();
    return {
      ...n,
      policy: r,
      session: {
        ...n.expiresAt !== void 0 ? { expiresAt: n.expiresAt } : {},
        strict: r.strictApprovals
      }
    };
  } catch (n) {
    throw Ve(n, "invalid_wrap_meta");
  }
});
Ie.register("setup", u1);
Ie.register("unlock", l1);
Ie.register("lock", () => ({ ...Ke({ broadcast: !0, reason: "manual-lock" }), policy: Ee() }));
Ie.register("seal", d1);
Ie.register("reveal", f1);
Ie.register("sign", p1);
Ie.register("approveRelease", h1);
Ie.register("deleteAuthzAssertion", y1);
Fw((e) => Ie.emit("vault.state", e));
Ig((e) => Ie.emit(e));
await _f();
Ie.start();
window.addEventListener("pagehide", Yw);
w1();
g1();
m1();
window.self !== window.top && document.body.classList.add("embedded");
const vf = globalThis.crypto, b1 = { webcrypto: vf }, xf = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: b1,
  webcrypto: vf
}, Symbol.toStringTag, { value: "Module" }));
