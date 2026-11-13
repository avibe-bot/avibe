const Zt = "avibe.vault.crypto";
const ae = {
  sandboxVersion: "0.1.0",
  // Resource hashes are the authority; this source-input identity additionally
  // binds ready/handshake and same-release top-level ceremony pairing.
  buildHash: "fc013b755a005b6b48dc750c429c46e9ee1401532d944c671207f85e371e7b7a"
};
class m extends Error {
  code;
  retryable;
  constructor(t, n, r = !1) {
    super(n ?? t), this.code = t, this.retryable = r;
  }
}
function ma(e) {
  let t;
  try {
    t = new URL(e);
  } catch {
    return !1;
  }
  const n = t.hostname.toLowerCase();
  return n === "localhost" || n === "127.0.0.1" || n === "::1" || n === "[::1]" ? t.protocol === "http:" || t.protocol === "https:" : !!(t.protocol === "https:" && (n === "avibe.bot" || n.endsWith(".avibe.bot")));
}
function cp(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return t.channel === Zt && t.version === 2 && typeof t.id == "string" && t.id.length > 0 && typeof t.op == "string";
}
const lp = /^[A-Za-z0-9_-]{16,128}$/;
function fl(e) {
  return typeof e == "object" && e !== null;
}
function up(e) {
  return fl(e) ? e.channel === Zt && e.version === 2 && e.kind === "event" && e.event === "confirm.surface" : !1;
}
class dp {
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
        channel: Zt,
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
      channel: Zt,
      version: 2,
      build: ae,
      capabilities: {
        operations: Array.from(this.handlers.keys())
      }
    });
  }
  async onMessage(t) {
    if (!ma(t.origin)) return;
    if (up(t.data)) {
      if (t.origin === this.pinnedParentOrigin && t.source === this.pinnedSource) {
        const c = t.data.surface ?? t.data.payload;
        c !== void 0 && this.recordParentSurfaceEvent(t.data, c);
      }
      return;
    }
    if (!cp(t.data)) return;
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
    if (n.authorizationWindow !== void 0 && (typeof n.authorizationWindow != "string" || !lp.test(n.authorizationWindow))) {
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
        channel: Zt,
        version: 2,
        id: n.id,
        ok: !0,
        result: c
      });
    } catch (c) {
      const l = c instanceof m ? c.code : "internal_error", u = c instanceof m ? c.retryable : !1, d = c instanceof m ? c.message : void 0;
      this.reply(t.source, this.fail(n.id, l, d, u)), r && this.clearPin();
    } finally {
      this.activeSurfaceSlots.delete(n.id);
    }
  }
  validateInitialHandshake(t, n) {
    if (t.op !== "handshake" || !fl(t.payload)) return null;
    const r = t.payload.parentOrigin, i = t.payload.nonce;
    return typeof r != "string" || r !== n || !ma(r) || typeof i != "string" || i.length < 16 || i.length > 256 ? null : i;
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
    return { channel: Zt, version: 2, id: t, ok: !1, error: { code: n, message: r, retryable: i } };
  }
  reply(t, n) {
    !t || this.pinnedParentOrigin === null || t.postMessage(n, this.pinnedParentOrigin);
  }
  postToParent(t, n) {
    t.postMessage(n, "*");
  }
}
function Jo() {
  return new URLSearchParams((window.location.hash ?? "").slice(1));
}
function pl(e, t, n, r = {}) {
  const i = new URL(window.location.href);
  return ae.buildHash !== "dev" && (i.pathname = `/v/${ae.buildHash}/index.html`), i.search = "", i.hash = new URLSearchParams({ mode: e, id: t, ...n, ...r }).toString(), i.toString();
}
function mn(e) {
  if (!e || typeof e != "object") return !1;
  const t = e;
  return t.buildHash === ae.buildHash && t.sandboxVersion === ae.sandboxVersion;
}
async function fp() {
  if (ae.buildHash === "dev") return;
  const e = new URL(window.location.href);
  if (e.pathname !== `/v/${ae.buildHash}/index.html` || e.search)
    throw new Error("Sandbox release document URL mismatch");
  const t = document.querySelectorAll('link[rel="stylesheet"]');
  if (t.length !== 1 || !t[0].integrity.startsWith("sha256-") || !t[0].href.startsWith(`${e.origin}/v/${ae.buildHash}/assets/`))
    throw new Error("Sandbox release stylesheet is not pinned");
  const n = t[0];
  n.sheet || await new Promise((r, i) => {
    const o = () => a(new Error("Sandbox release stylesheet failed integrity or loading")), s = () => n.sheet ? a() : o(), a = (l) => {
      clearTimeout(c), n.removeEventListener("load", s), n.removeEventListener("error", o), window.removeEventListener("load", s), window.removeEventListener("pagehide", o), l ? i(l) : r();
    }, c = setTimeout(o, 1e4);
    n.addEventListener("load", s, { once: !0 }), n.addEventListener("error", o, { once: !0 }), window.addEventListener("load", s, { once: !0 }), window.addEventListener("pagehide", o, { once: !0 }), (n.sheet || document.readyState === "complete") && s();
  });
}
class Ze extends Error {
  constructor(t) {
    let n;
    t instanceof Error ? n = t.message : typeof t == "string" ? n = t : n = "", super(n), this.name = this.constructor.name;
  }
}
class Qe extends Ze {
}
class Vr extends Ze {
}
class Sr extends Ze {
}
class pp extends Ze {
}
class hp extends Ze {
}
class yp extends Ze {
}
class wp extends Ze {
}
class gp extends Ze {
}
class mp extends Ze {
}
class bp extends Ze {
}
class Qo extends Ze {
}
const vp = {}, hl = xp(globalThis, vp);
function xp(e, t) {
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
async function kp() {
  if (hl !== void 0 && globalThis.crypto !== void 0)
    return globalThis.crypto.subtle;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => ap);
    return e.subtle;
  } catch (e) {
    throw new Qo(e);
  }
}
class es {
  constructor() {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    });
  }
  async _setup() {
    this._api === void 0 && (this._api = await kp());
  }
}
const kt = {
  Base: 0,
  Psk: 1,
  Auth: 2,
  AuthPsk: 3
}, ba = {
  DhkemX25519HkdfSha256: 32
}, yl = {
  HkdfSha256: 1
}, ts = {
  Aes128Gcm: 1,
  Aes256Gcm: 2,
  ExportOnly: 65535
}, Qr = 8192, Ep = 268435456, va = 32, Ee = /* @__PURE__ */ new Uint8Array(0), et = 0n, un = 1n, Ar = 2n, Sp = /* @__PURE__ */ new Uint8Array([
  75,
  69,
  77,
  0,
  0
]), xa = /* @__PURE__ */ new Uint8Array([
  72,
  80,
  75,
  69,
  45,
  118,
  49
]);
function qr(e) {
  return new Uint8Array(J(e));
}
function J(e) {
  return e instanceof ArrayBuffer ? e : ArrayBuffer.isView(e) ? new Uint8Array(e.buffer, e.byteOffset, e.byteLength).slice().buffer : new Uint8Array(e).slice().buffer;
}
class Ap extends es {
  constructor() {
    super(), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yl.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Ee
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
    return r.set(xa, 0), r.set(this._suiteId, 7), r.set(t, 7 + this._suiteId.byteLength), r.set(n, 7 + this._suiteId.byteLength + t.byteLength), r;
  }
  buildLabeledInfo(t, n, r) {
    this._checkInit();
    const i = new Uint8Array(9 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return i.set(new Uint8Array([0, r]), 0), i.set(xa, 2), i.set(this._suiteId, 9), i.set(t, 9 + this._suiteId.byteLength), i.set(n, 9 + this._suiteId.byteLength + t.byteLength), i;
  }
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : J(t);
    if (r.byteLength !== this.hashSize)
      throw new Qe("The salt length must be the same as the hashSize");
    const i = J(n), o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
  async expand(t, n, r) {
    await this._setup();
    const i = J(t), o = await this._api.importKey("raw", i, this.algHash, !1, [
      "sign"
    ]), s = new ArrayBuffer(r), a = new Uint8Array(s);
    let c = Ee;
    const l = qr(n), u = new Uint8Array(1);
    if (r > 255 * this.hashSize)
      throw new Error("Entropy limit reached");
    const d = new Uint8Array(this.hashSize + l.length + 1);
    for (let h = 1, p = 0; p < a.length; h++)
      u[0] = h, d.set(c, 0), d.set(l, c.length), d.set(u, c.length + l.length), c = new Uint8Array(await this._api.sign("HMAC", o, d.slice(0, c.length + l.length + 1))), a.length - p >= c.length ? (a.set(c, p), p += c.length) : (a.set(c.slice(0, a.length - p), p), p += a.length - p);
    return s;
  }
  async extractAndExpand(t, n, r, i) {
    await this._setup();
    const o = J(n), s = await this._api.importKey("raw", o, "HKDF", !1, ["deriveBits"]);
    return await this._api.deriveBits({
      name: "HKDF",
      hash: this.algHash.hash,
      salt: J(t),
      info: J(r)
    }, s, i * 8);
  }
  async labeledExtract(t, n, r) {
    return await this.extract(t, this.buildLabeledIkm(n, r));
  }
  async labeledExpand(t, n, r, i) {
    return await this.expand(t, this.buildLabeledInfo(n, r, i), i);
  }
  _checkInit() {
    if (this._suiteId === Ee)
      throw new Error("Not initialized. Call init()");
  }
}
class wl extends Ap {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: yl.HkdfSha256
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
const Mn = (e) => typeof e == "object" && e !== null && typeof e.privateKey == "object" && typeof e.publicKey == "object";
function nr(e, t) {
  if (t <= 0)
    throw new Error("i2Osp: too small size");
  if (e >= 256 ** t)
    throw new Error("i2Osp: too large integer");
  const n = new Uint8Array(t);
  for (let r = 0; r < t && e; r++)
    n[t - (r + 1)] = e % 256, e = Math.floor(e / 256);
  return n;
}
function _r(e, t) {
  const n = new Uint8Array(e.length + t.length);
  return n.set(e, 0), n.set(t, e.length), n;
}
function ka(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = atob(t), r = new Uint8Array(n.length);
  for (let i = 0; i < n.length; i++)
    r[i] = n.charCodeAt(i);
  return r;
}
async function _p() {
  if (typeof hl < "u" && globalThis.crypto !== void 0)
    return globalThis.crypto;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => ap);
    return e;
  } catch {
    throw new Error("failed to load Crypto");
  }
}
function Ip(e, t) {
  if (e.byteLength !== t.byteLength)
    throw new Error("xor: different length inputs");
  const n = new Uint8Array(e.byteLength);
  for (let r = 0; r < e.byteLength; r++)
    n[r] = e[r] ^ t[r];
  return n;
}
const Pp = /* @__PURE__ */ new Uint8Array([
  101,
  97,
  101,
  95,
  112,
  114,
  107
]), Tp = /* @__PURE__ */ new Uint8Array([
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
function Bp(e, t, n) {
  const r = new Uint8Array(e.length + t.length + n.length);
  return r.set(e, 0), r.set(t, e.length), r.set(n, e.length + t.length), r;
}
class $p {
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
    const i = new Uint8Array(Sp);
    i.set(nr(this.id, 2), 3), this._kdf.init(i);
  }
  async serializePublicKey(t) {
    return await this._prim.serializePublicKey(t);
  }
  async deserializePublicKey(t) {
    return await this._prim.deserializePublicKey(J(t));
  }
  async serializePrivateKey(t) {
    return await this._prim.serializePrivateKey(t);
  }
  async deserializePrivateKey(t) {
    return await this._prim.deserializePrivateKey(J(t));
  }
  async importKey(t, n, r = !0) {
    return await this._prim.importKey(t, n, r);
  }
  async generateKeyPair() {
    return await this._prim.generateKeyPair();
  }
  async deriveKeyPair(t) {
    const n = J(t);
    if (n.byteLength > Qr)
      throw new Qe("Too long ikm");
    return await this._prim.deriveKeyPair(n);
  }
  async encap(t) {
    let n;
    t.ekm === void 0 ? n = await this.generateKeyPair() : Mn(t.ekm) ? n = t.ekm : n = await this.deriveKeyPair(t.ekm);
    const r = await this._prim.serializePublicKey(n.publicKey), i = await this._prim.serializePublicKey(t.recipientPublicKey);
    try {
      let o;
      if (t.senderKey === void 0)
        o = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey));
      else {
        const c = Mn(t.senderKey) ? t.senderKey.privateKey : t.senderKey, l = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey)), u = new Uint8Array(await this._prim.dh(c, t.recipientPublicKey));
        o = _r(l, u);
      }
      let s;
      if (t.senderKey === void 0)
        s = _r(new Uint8Array(r), new Uint8Array(i));
      else {
        const c = Mn(t.senderKey) ? t.senderKey.publicKey : await this._prim.derivePublicKey(t.senderKey), l = await this._prim.serializePublicKey(c);
        s = Bp(new Uint8Array(r), new Uint8Array(i), new Uint8Array(l));
      }
      const a = await this._generateSharedSecret(o, s);
      return {
        enc: r,
        sharedSecret: a
      };
    } catch (o) {
      throw new pp(o);
    }
  }
  async decap(t) {
    const n = J(t.enc), r = await this._prim.deserializePublicKey(n), i = Mn(t.recipientKey) ? t.recipientKey.privateKey : t.recipientKey, o = Mn(t.recipientKey) ? t.recipientKey.publicKey : await this._prim.derivePublicKey(t.recipientKey), s = await this._prim.serializePublicKey(o);
    try {
      let a;
      if (t.senderPublicKey === void 0)
        a = new Uint8Array(await this._prim.dh(i, r));
      else {
        const l = new Uint8Array(await this._prim.dh(i, r)), u = new Uint8Array(await this._prim.dh(i, t.senderPublicKey));
        a = _r(l, u);
      }
      let c;
      if (t.senderPublicKey === void 0)
        c = _r(new Uint8Array(n), new Uint8Array(s));
      else {
        const l = await this._prim.serializePublicKey(t.senderPublicKey);
        c = new Uint8Array(n.byteLength + s.byteLength + l.byteLength), c.set(new Uint8Array(n), 0), c.set(new Uint8Array(s), n.byteLength), c.set(new Uint8Array(l), n.byteLength + s.byteLength);
      }
      return await this._generateSharedSecret(a, c);
    } catch (a) {
      throw new hp(a);
    }
  }
  async _generateSharedSecret(t, n) {
    const r = this._kdf.buildLabeledIkm(Pp, t), i = this._kdf.buildLabeledInfo(Tp, n, this.secretSize);
    return await this._kdf.extractAndExpand(Ee, r, i, this.secretSize);
  }
}
const Ir = ["deriveBits"], Op = /* @__PURE__ */ new Uint8Array([
  100,
  107,
  112,
  95,
  112,
  114,
  107
]), Rp = /* @__PURE__ */ new Uint8Array([115, 107]);
class dn {
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
class Lp {
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
      return Promise.reject(new Vr(n));
    }
  }
  async deserializePublicKey(t) {
    try {
      return await this._importRawKey(J(t), !0);
    } catch (n) {
      throw new Sr(n);
    }
  }
  serializePrivateKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new Vr(n));
    }
  }
  async deserializePrivateKey(t) {
    try {
      return await this._importRawKey(J(t), !1);
    } catch (n) {
      throw new Sr(n);
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
      throw new Sr(i);
    }
  }
  async generateKeyPair() {
    try {
      const t = await this._curve.utils.randomSecretKey(), n = new dn(this._algName, t, "private", Ir);
      return { publicKey: await this.derivePublicKey(n), privateKey: n };
    } catch (t) {
      throw new Qo(t);
    }
  }
  async deriveKeyPair(t) {
    try {
      const n = J(t), r = await this._hkdf.labeledExtract(Ee.buffer, Op, new Uint8Array(n)), i = await this._hkdf.labeledExpand(r, Rp, Ee, this._nSk), o = new dn(this._algName, new Uint8Array(i), "private", Ir);
      return {
        privateKey: o,
        publicKey: await this.derivePublicKey(o)
      };
    } catch (n) {
      throw new bp(n);
    }
  }
  derivePublicKey(t) {
    try {
      const n = this._curve.getPublicKey(t.key);
      return Promise.resolve(new dn(this._algName, n, "public"));
    } catch (n) {
      return Promise.reject(new Sr(n));
    }
  }
  dh(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t.key, n.key).buffer);
    } catch (r) {
      return Promise.reject(new Vr(r));
    }
  }
  _importRawKey(t, n) {
    return new Promise((r, i) => {
      n && t.byteLength !== this._nPk && i(new Error("Invalid length of the key")), !n && t.byteLength !== this._nSk && i(new Error("Invalid length of the key")), r(new dn(this._algName, new Uint8Array(t), n ? "public" : "private", n ? [] : Ir));
    });
  }
  _importJWK(t, n) {
    return new Promise((r, i) => {
      t.kty !== "OKP" && i(new Error(`Invalid kty: ${t.kty}`)), t.crv !== this._algName && i(new Error(`Invalid crv: ${t.crv}`)), n ? (typeof t.d < "u" && i(new Error("Invalid key: `d` should not be set")), typeof t.x != "string" && i(new Error("Invalid key: `x` not found")), r(new dn(this._algName, ka(t.x), "public"))) : (typeof t.d != "string" && i(new Error("Invalid key: `d` not found")), r(new dn(this._algName, ka(t.d), "private", Ir)));
    });
  }
}
const Cp = ["encrypt", "decrypt"];
function Np(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function ar(e, t = "") {
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new Error(`${n}expected integer >0, got ${e}`);
  }
}
function yt(e, t, n = "") {
  const r = Np(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function ei(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function zp(e, t) {
  yt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new Error('"digestInto() output" expected to be of length >=' + n);
}
function Dp(e) {
  if (typeof e == "bigint") {
    if (!Gr(e))
      throw new Error("positive bigint expected, got " + e);
  } else
    ar(e);
  return e;
}
function ti(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
const Up = /* @__PURE__ */ new Uint32Array([287454020]), Mp = /* @__PURE__ */ new Uint8Array(Up.buffer);
Mp[0];
function Yi(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Xe(e, t) {
  return e << 32 - t | e >>> t;
}
const gl = /* @ts-ignore: to use toHex */ typeof Uint8Array.from([]).toHex == "function" && // @ts-ignore: to use fromHex
typeof Uint8Array.fromHex == "function", Hp = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0")), Fp = [
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
function Kp(e) {
  if (yt(e), gl)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += Hp[e[n]];
  return t;
}
const ut = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function _o(e) {
  if (e >= ut._0 && e <= ut._9)
    return e - ut._0;
  if (e >= ut.A && e <= ut.F)
    return e - (ut.A - 10);
  if (e >= ut.a && e <= ut.f)
    return e - (ut.a - 10);
}
function jp(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (gl)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = _o(e.charCodeAt(o)), a = _o(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function Vp(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  let t = et;
  for (let n = 0; n < e.length; n++) {
    const r = _o(e.charCodeAt(n));
    if (r === void 0)
      throw new Error('hex string expected, got non-hex character "' + e[n] + '" at index ' + n);
    t = t << 4n | Fp[r];
  }
  return t;
}
function qp(e) {
  ar(e, "numberToBigint");
  let t = e, n = et, r = 1n;
  for (; t > 0; )
    t % 2 === 1 && (n += r), t = Math.floor(t / 2), r <<= 1n;
  return n;
}
function Ea(e) {
  return Vp(Kp(Io(yt(e)).reverse()));
}
function Gp(e, t) {
  ar(t), e = Dp(e);
  const n = jp(e.toString(16).padStart(t * 2, "0"));
  if (n.length !== t)
    throw new Error("number too large");
  return n;
}
function Wp(e, t) {
  return Gp(e, t).reverse();
}
function Io(e) {
  return Uint8Array.from(e);
}
function Gr(e) {
  return typeof e == "bigint" && et <= e;
}
function Zp(e, t, n) {
  return Gr(e) && Gr(t) && Gr(n) && t <= e && e < n;
}
function Sa(e, t, n, r) {
  if (!Zp(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Xp(e, t = {}, n = {}) {
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
async function Yp(e = 32) {
  const t = await _p(), n = new Uint8Array(e);
  return t.getRandomValues(n), n;
}
function Jp(e) {
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
function Qp(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash must wrapped by utils.createHasher");
  ar(e.outputLen), ar(e.blockLen);
}
function eh(e, t = {}) {
  const n = (o, s) => e(s).update(o).digest(), r = e(void 0), i = Object.assign(n, {
    outputLen: r.outputLen,
    blockLen: r.blockLen,
    create: (o) => e(o),
    ...t
  });
  return Object.freeze(i);
}
let ml = class {
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
    }), Qp(t), yt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), ti(i);
  }
  update(t) {
    return ei(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    ei(this), yt(t, this.outputLen, "output"), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const bl = (e, t, n) => new ml(e, t).update(n).digest();
bl.create = (e, t) => new ml(e, t);
function th(e, t, n) {
  return e & t ^ ~e & n;
}
function nh(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let rh = class {
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
    }), this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Yi(this.buffer);
  }
  update(t) {
    ei(this), yt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Yi(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    ei(this), zp(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, ti(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, qp(this.length * 8), o), this.process(r, 0);
    const a = Yi(t), c = this.outputLen;
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
const Et = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), ih = /* @__PURE__ */ Uint32Array.from([
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
let oh = class extends rh {
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
      St[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = St[d - 15], p = St[d - 2], g = Xe(h, 7) ^ Xe(h, 18) ^ h >>> 3, w = Xe(p, 17) ^ Xe(p, 19) ^ p >>> 10;
      St[d] = w + St[d - 7] + g + St[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const h = Xe(a, 6) ^ Xe(a, 11) ^ Xe(a, 25), p = u + h + th(a, c, l) + ih[d] + St[d] | 0, w = (Xe(r, 2) ^ Xe(r, 13) ^ Xe(r, 22)) + nh(r, i, o) | 0;
      u = l, l = c, c = a, a = s + p | 0, s = o, o = i, i = r, r = p + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    ti(St);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), ti(this.buffer);
  }
}, sh = class extends oh {
  constructor() {
    super(32), Object.defineProperty(this, "A", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[0] | 0
    }), Object.defineProperty(this, "B", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[1] | 0
    }), Object.defineProperty(this, "C", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[2] | 0
    }), Object.defineProperty(this, "D", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[3] | 0
    }), Object.defineProperty(this, "E", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[4] | 0
    }), Object.defineProperty(this, "F", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[5] | 0
    }), Object.defineProperty(this, "G", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[6] | 0
    }), Object.defineProperty(this, "H", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Et[7] | 0
    });
  }
};
const ah = /* @__PURE__ */ eh(
  () => new sh(),
  /* @__PURE__ */ Jp(1)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function vl(e, t) {
  const n = e % t;
  return n >= et ? n : t + n;
}
function Ue(e, t, n) {
  let r = e;
  for (; t-- > et; )
    r *= r, r %= n;
  return r;
}
function ch(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function lh(e) {
  return Xp(e, {
    adjustScalarBytes: "function",
    powPminus2: "function"
  }), Object.freeze({ ...e });
}
function uh(e) {
  const t = lh(e), { P: n, type: r, adjustScalarBytes: i, powPminus2: o, randomBytes: s } = t, a = r === "x25519";
  if (!a && r !== "x448")
    throw new Error("invalid type");
  const c = s || Yp, l = a ? 255n : 448n, u = a ? 32 : 56, d = a ? 9n : 5n, h = a ? 121665n : 39081n, p = a ? Ar ** 254n : Ar ** 447n, g = a ? 8n * Ar ** 251n - un : 4n * Ar ** 445n - un, w = p + g + un, f = (A) => vl(A, n), y = v(d);
  function v(A) {
    return Wp(f(A), u);
  }
  function x(A) {
    const k = Io(yt(A, u, "uCoordinate"));
    return a && (k[31] &= 127), f(Ea(k));
  }
  function I(A) {
    return Ea(i(Io(yt(A, u, "scalar"))));
  }
  function E(A, k) {
    const _ = N(x(k), I(A));
    if (_ === et)
      throw new Error("invalid private or public key received");
    return v(_);
  }
  function T(A) {
    return E(A, y);
  }
  const P = T, O = E;
  function B(A, k, _) {
    const $ = f(A * (k - _));
    return k = f(k - $), _ = f(_ + $), { x_2: k, x_3: _ };
  }
  function N(A, k) {
    Sa("u", A, et, n), Sa("scalar", k, p, w);
    const _ = k, $ = A;
    let L = un, R = et, C = A, D = un, U = et;
    for (let M = l - 1n; M >= et; M--) {
      const te = _ >> M & un;
      U ^= te, { x_2: L, x_3: C } = B(U, L, C), { x_2: R, x_3: D } = B(U, R, D), U = te;
      const ne = L + R, re = f(ne * ne), ee = L - R, me = f(ee * ee), lt = re - me, Kt = C + D, ln = C - D, Dn = f(ln * ne), Un = f(Kt * ee), wa = Dn + Un, ga = Dn - Un;
      C = f(wa * wa), D = f($ * f(ga * ga)), L = f(re * me), R = f(lt * (re + f(h * lt)));
    }
    ({ x_2: L, x_3: C } = B(U, L, C)), { x_2: R, x_3: D } = B(U, R, D);
    const Z = o(R);
    return f(L * Z);
  }
  const z = {
    secretKey: u,
    publicKey: u,
    seed: u
  }, b = async (A) => (A === void 0 && (A = await c(u)), yt(A, z.seed, "seed"), A), S = { randomSecretKey: b };
  return Object.freeze({
    keygen: ch(b, P),
    getSharedSecret: O,
    getPublicKey: P,
    scalarMult: E,
    scalarMultBase: T,
    utils: S,
    GuBytes: y.slice(),
    lengths: z
  });
}
class dh extends es {
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
    }), this._rawKey = J(t);
  }
  async seal(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: J(t),
      additionalData: J(r)
    };
    return await this._api.encrypt(i, this._key, J(n));
  }
  async open(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: J(t),
      additionalData: J(r)
    };
    return await this._api.decrypt(i, this._key, J(n));
  }
  async _setupKey() {
    if (this._key !== void 0)
      return;
    await this._setup();
    const t = await this._importKey(this._rawKey);
    new Uint8Array(this._rawKey).fill(0), this._key = t;
  }
  async _importKey(t) {
    return await this._api.importKey("raw", t, { name: "AES-GCM" }, !0, Cp);
  }
}
class fh {
  constructor() {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: ts.Aes128Gcm
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
    return new dh(t);
  }
}
class ph extends fh {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: ts.Aes256Gcm
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
function Aa() {
  return new Promise((e, t) => {
    t(new Qo("Not supported"));
  });
}
const hh = new Uint8Array([115, 101, 99]);
class ns {
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
    return await Aa();
  }
  async open(t, n) {
    return await Aa();
  }
  async export(t, n) {
    const r = J(t);
    if (r.byteLength > Qr)
      throw new Qe("Too long exporter context");
    try {
      return await this._kdf.labeledExpand(this.exporterSecret, hh, new Uint8Array(r), n);
    } catch (i) {
      throw new yp(i);
    }
  }
}
class yh extends ns {
}
class wh extends ns {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.enc = i;
  }
}
class xl extends ns {
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
    const n = nr(t.seq, t.baseNonce.byteLength);
    return Ip(t.baseNonce, n).buffer;
  }
  incrementSeq(t) {
    if (t.seq > Number.MAX_SAFE_INTEGER)
      throw new mp("Message limit reached");
    t.seq += 1;
  }
}
var gh = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, mh = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Wr;
class kl {
  constructor() {
    Wr.set(this, Promise.resolve());
  }
  async lock() {
    let t;
    const n = new Promise((i) => {
      t = i;
    }), r = gh(this, Wr, "f");
    return mh(this, Wr, n, "f"), await r, t;
  }
}
Wr = /* @__PURE__ */ new WeakMap();
var _a = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, bh = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Zn;
class vh extends xl {
  constructor() {
    super(...arguments), Zn.set(this, void 0);
  }
  async open(t, n = Ee.buffer) {
    bh(this, Zn, _a(this, Zn, "f") ?? new kl(), "f");
    const r = await _a(this, Zn, "f").lock();
    let i;
    try {
      i = await this._ctx.key.open(this.computeNonce(this._ctx), J(t), J(n));
    } catch (o) {
      throw new gp(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Zn = /* @__PURE__ */ new WeakMap();
var Ia = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, xh = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Xn;
class kh extends xl {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Xn.set(this, void 0), this.enc = i;
  }
  async seal(t, n = Ee.buffer) {
    xh(this, Xn, Ia(this, Xn, "f") ?? new kl(), "f");
    const r = await Ia(this, Xn, "f").lock();
    let i;
    try {
      i = await this._ctx.key.seal(this.computeNonce(this._ctx), J(t), J(n));
    } catch (o) {
      throw new wp(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Xn = /* @__PURE__ */ new WeakMap();
const Eh = new Uint8Array([
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
]), Sh = new Uint8Array([101, 120, 112]), Ah = new Uint8Array([
  105,
  110,
  102,
  111,
  95,
  104,
  97,
  115,
  104
]), _h = new Uint8Array([107, 101, 121]), Ih = new Uint8Array([
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
]), Ph = new Uint8Array([115, 101, 99, 114, 101, 116]), Th = new Uint8Array([
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
class Bh extends es {
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
      throw new Qe("KemId cannot be used");
    if (this._kem = t.kem, typeof t.kdf == "number")
      throw new Qe("KdfId cannot be used");
    if (this._kdf = t.kdf, typeof t.aead == "number")
      throw new Qe("AeadId cannot be used");
    this._aead = t.aead, this._suiteId = new Uint8Array(Th), this._suiteId.set(nr(this._kem.id, 2), 4), this._suiteId.set(nr(this._kdf.id, 2), 6), this._suiteId.set(nr(this._aead.id, 2), 8), this._kdf.init(this._suiteId);
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
    return t.psk !== void 0 ? r = t.senderKey !== void 0 ? kt.AuthPsk : kt.Psk : r = t.senderKey !== void 0 ? kt.Auth : kt.Base, await this._keyScheduleS(r, n.sharedSecret, n.enc, t);
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
    return t.psk !== void 0 ? r = t.senderPublicKey !== void 0 ? kt.AuthPsk : kt.Psk : r = t.senderPublicKey !== void 0 ? kt.Auth : kt.Base, await this._keyScheduleR(r, n, t);
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
  async seal(t, n, r = Ee.buffer) {
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
  async open(t, n, r = Ee.buffer) {
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
    const i = r.psk === void 0 ? Ee : qr(r.psk.id), o = await this._kdf.labeledExtract(Ee, Ih, i), s = r.info === void 0 ? Ee : qr(r.info), a = await this._kdf.labeledExtract(Ee, Ah, s), c = new Uint8Array(1 + o.byteLength + a.byteLength);
    c.set(new Uint8Array([t]), 0), c.set(new Uint8Array(o), 1), c.set(new Uint8Array(a), 1 + o.byteLength);
    const l = r.psk === void 0 ? Ee : qr(r.psk.key), u = this._kdf.buildLabeledIkm(Ph, l), d = this._kdf.buildLabeledInfo(Sh, c, this._kdf.hashSize), h = await this._kdf.extractAndExpand(n, u, d, this._kdf.hashSize);
    if (this._aead.id === ts.ExportOnly)
      return { aead: this._aead, exporterSecret: h };
    const p = this._kdf.buildLabeledInfo(_h, c, this._aead.keySize), g = await this._kdf.extractAndExpand(n, u, p, this._aead.keySize), w = this._kdf.buildLabeledInfo(Eh, c, this._aead.nonceSize), f = await this._kdf.extractAndExpand(n, u, w, this._aead.nonceSize);
    return {
      aead: this._aead,
      exporterSecret: h,
      key: g,
      baseNonce: new Uint8Array(f),
      seq: 0
    };
  }
  async _keyScheduleS(t, n, r, i) {
    const o = await this._keySchedule(t, n, i);
    return o.key === void 0 ? new wh(this._api, this._kdf, o.exporterSecret, r) : new kh(this._api, this._kdf, o, r);
  }
  async _keyScheduleR(t, n, r) {
    const i = await this._keySchedule(t, n, r);
    return i.key === void 0 ? new yh(this._api, this._kdf, i.exporterSecret) : new vh(this._api, this._kdf, i);
  }
  _validateInputLength(t) {
    if (t.info !== void 0 && t.info.byteLength > Ep)
      throw new Qe("Too long info");
    if (t.psk !== void 0) {
      if (t.psk.key.byteLength < va)
        throw new Qe(`PSK must have at least ${va} bytes`);
      if (t.psk.key.byteLength > Qr)
        throw new Qe("Too long psk.key");
      if (t.psk.id.byteLength > Qr)
        throw new Qe("Too long psk.id");
    }
  }
}
class $h extends Bh {
}
let Oh = class extends wl {
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Rh = 1n, Pa = 2n, Lh = 3n, Ch = 5n, El = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn;
function Nh(e) {
  const t = 10n, n = 20n, r = 40n, i = 80n, o = El, a = e * e % o * e % o, c = Ue(a, Pa, o) * a % o, l = Ue(c, Rh, o) * e % o, u = Ue(l, Ch, o) * l % o, d = Ue(u, t, o) * u % o, h = Ue(d, n, o) * d % o, p = Ue(h, r, o) * h % o, g = Ue(p, i, o) * p % o, w = Ue(g, i, o) * p % o, f = Ue(w, t, o) * u % o;
  return { pow_p_5_8: Ue(f, Pa, o) * e % o, b2: a };
}
function zh(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const Dh = /* @__PURE__ */ (() => {
  const e = El;
  return uh({
    P: e,
    type: "x25519",
    powPminus2: (t) => {
      const { pow_p_5_8: n, b2: r } = Nh(t);
      return vl(Ue(n, Lh, e) * r, e);
    },
    adjustScalarBytes: zh
  });
})();
class Uh extends wl {
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : J(t), i = J(n);
    if (r.byteLength !== this.hashSize)
      return bl(ah, new Uint8Array(r), new Uint8Array(i)).buffer;
    const o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
}
class Mh extends Lp {
  constructor(t) {
    super("X25519", 32, Dh, t);
  }
  derive(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t, n));
    } catch (r) {
      return Promise.reject(new Vr(r));
    }
  }
}
class Hh extends $p {
  constructor() {
    const t = new Uh();
    super(ba.DhkemX25519HkdfSha256, new Mh(t), t), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: ba.DhkemX25519HkdfSha256
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
function Sl(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array" && "BYTES_PER_ELEMENT" in e && e.BYTES_PER_ELEMENT === 1;
}
function it(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(`${n}expected number, got ${typeof e}`);
  }
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new RangeError(`${n}expected integer >= 0, got ${e}`);
  }
}
function ot(e, t, n = "") {
  const r = Sl(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`, l = s + "expected Uint8Array" + a + ", got " + c;
    throw r ? new RangeError(l) : new TypeError(l);
  }
  return e;
}
function Al(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new TypeError("Hash must wrapped by utils.createHasher");
  if (it(e.outputLen), it(e.blockLen), e.outputLen < 1)
    throw new Error('"outputLen" must be >= 1');
  if (e.blockLen < 1)
    throw new Error('"blockLen" must be >= 1');
}
function Sn(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function rs(e, t) {
  ot(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new RangeError('"digestInto() output" expected to be of length >=' + n);
}
function Fh(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function st(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Ji(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Ye(e, t) {
  return e << 32 - t | e >>> t;
}
function Pr(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const Kh = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function jh(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function Vh(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = jh(e[t]);
  return e;
}
const Ta = Kh ? (e) => e : Vh, _l = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", qh = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function is(e) {
  if (ot(e), _l)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += qh[e[n]];
  return t;
}
const dt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function Ba(e) {
  if (e >= dt._0 && e <= dt._9)
    return e - dt._0;
  if (e >= dt.A && e <= dt.F)
    return e - (dt.A - 10);
  if (e >= dt.a && e <= dt.f)
    return e - (dt.a - 10);
}
function os(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  if (_l)
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
    const s = Ba(e.charCodeAt(o)), a = Ba(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new RangeError('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function Gh(...e) {
  let t = 0;
  for (let r = 0; r < e.length; r++) {
    const i = e[r];
    ot(i), t += i.length;
  }
  const n = new Uint8Array(t);
  for (let r = 0, i = 0; r < e.length; r++) {
    const o = e[r];
    n.set(o, i), i += o.length;
  }
  return n;
}
function Ti(e, t = {}) {
  const n = (i, o) => e(o).update(i).digest(), r = e(void 0);
  return n.outputLen = r.outputLen, n.blockLen = r.blockLen, n.canXOF = r.canXOF, n.create = (i) => e(i), Object.assign(n, t), Object.freeze(n);
}
function ss(e = 32) {
  it(e, "bytesLength");
  const t = typeof globalThis == "object" ? globalThis.crypto : null;
  if (typeof t?.getRandomValues != "function")
    throw new Error("crypto.getRandomValues must be defined");
  if (e > 65536)
    throw new RangeError(`"bytesLength" expected <= 65536, got ${e}`);
  return t.getRandomValues(new Uint8Array(e));
}
const Il = (e) => ({
  // Current NIST hashAlgs suffixes used here fit in one DER subidentifier octet.
  // Larger suffix values would need base-128 OID encoding and a different length byte.
  oid: Uint8Array.from([6, 9, 96, 134, 72, 1, 101, 3, 4, 2, e])
});
function Wh(e, t, n) {
  return e & t ^ ~e & n;
}
function Zh(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let as = class {
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
    this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Ji(this.buffer);
  }
  update(t) {
    Sn(this), ot(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Ji(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Sn(this), rs(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, st(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Ji(t), c = this.outputLen;
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
const At = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), ve = /* @__PURE__ */ Uint32Array.from([
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
]), Tr = /* @__PURE__ */ BigInt(2 ** 32 - 1), $a = /* @__PURE__ */ BigInt(32);
function Xh(e, t = !1) {
  return t ? { h: Number(e & Tr), l: Number(e >> $a & Tr) } : { h: Number(e >> $a & Tr) | 0, l: Number(e & Tr) | 0 };
}
function Pl(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = Xh(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const Oa = (e, t, n) => e >>> n, Ra = (e, t, n) => e << 32 - n | t >>> n, fn = (e, t, n) => e >>> n | t << 32 - n, pn = (e, t, n) => e << 32 - n | t >>> n, Br = (e, t, n) => e << 64 - n | t >>> n - 32, $r = (e, t, n) => e >>> n - 32 | t << 64 - n, Yh = (e, t, n) => e << n | t >>> 32 - n, Jh = (e, t, n) => t << n | e >>> 32 - n, Qh = (e, t, n) => t << n - 32 | e >>> 64 - n, ey = (e, t, n) => e << n - 32 | t >>> 64 - n;
function ft(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const ty = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), ny = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, ry = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), iy = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, oy = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), sy = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, ay = /* @__PURE__ */ Uint32Array.from([
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
]), _t = /* @__PURE__ */ new Uint32Array(64);
class cy extends as {
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
      _t[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = _t[d - 15], p = _t[d - 2], g = Ye(h, 7) ^ Ye(h, 18) ^ h >>> 3, w = Ye(p, 17) ^ Ye(p, 19) ^ p >>> 10;
      _t[d] = w + _t[d - 7] + g + _t[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const h = Ye(a, 6) ^ Ye(a, 11) ^ Ye(a, 25), p = u + h + Wh(a, c, l) + ay[d] + _t[d] | 0, w = (Ye(r, 2) ^ Ye(r, 13) ^ Ye(r, 22)) + Zh(r, i, o) | 0;
      u = l, l = c, c = a, a = s + p | 0, s = o, o = i, i = r, r = p + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    st(_t);
  }
  destroy() {
    this.destroyed = !0, this.set(0, 0, 0, 0, 0, 0, 0, 0), st(this.buffer);
  }
}
class ly extends cy {
  // We cannot use array here since array allows indexing by variable
  // which means optimizer/compiler cannot use registers.
  A = At[0] | 0;
  B = At[1] | 0;
  C = At[2] | 0;
  D = At[3] | 0;
  E = At[4] | 0;
  F = At[5] | 0;
  G = At[6] | 0;
  H = At[7] | 0;
  constructor() {
    super(32);
  }
}
const Tl = Pl([
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
].map((e) => BigInt(e))), uy = Tl[0], dy = Tl[1], It = /* @__PURE__ */ new Uint32Array(80), Pt = /* @__PURE__ */ new Uint32Array(80);
class fy extends as {
  constructor(t) {
    super(128, t, 16, !1);
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: h, Gh: p, Gl: g, Hh: w, Hl: f } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, h, p, g, w, f];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, h, p, g, w, f) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = h | 0, this.Gh = p | 0, this.Gl = g | 0, this.Hh = w | 0, this.Hl = f | 0;
  }
  process(t, n) {
    for (let x = 0; x < 16; x++, n += 4)
      It[x] = t.getUint32(n), Pt[x] = t.getUint32(n += 4);
    for (let x = 16; x < 80; x++) {
      const I = It[x - 15] | 0, E = Pt[x - 15] | 0, T = fn(I, E, 1) ^ fn(I, E, 8) ^ Oa(I, E, 7), P = pn(I, E, 1) ^ pn(I, E, 8) ^ Ra(I, E, 7), O = It[x - 2] | 0, B = Pt[x - 2] | 0, N = fn(O, B, 19) ^ Br(O, B, 61) ^ Oa(O, B, 6), z = pn(O, B, 19) ^ $r(O, B, 61) ^ Ra(O, B, 6), b = ry(P, z, Pt[x - 7], Pt[x - 16]), S = iy(b, T, N, It[x - 7], It[x - 16]);
      It[x] = S | 0, Pt[x] = b | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: h, Fh: p, Fl: g, Gh: w, Gl: f, Hh: y, Hl: v } = this;
    for (let x = 0; x < 80; x++) {
      const I = fn(d, h, 14) ^ fn(d, h, 18) ^ Br(d, h, 41), E = pn(d, h, 14) ^ pn(d, h, 18) ^ $r(d, h, 41), T = d & p ^ ~d & w, P = h & g ^ ~h & f, O = oy(v, E, P, dy[x], Pt[x]), B = sy(O, y, I, T, uy[x], It[x]), N = O | 0, z = fn(r, i, 28) ^ Br(r, i, 34) ^ Br(r, i, 39), b = pn(r, i, 28) ^ $r(r, i, 34) ^ $r(r, i, 39), S = r & o ^ r & a ^ o & a, A = i & s ^ i & c ^ s & c;
      y = w | 0, v = f | 0, w = p | 0, f = g | 0, p = d | 0, g = h | 0, { h: d, l: h } = ft(l | 0, u | 0, B | 0, N | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const k = ty(N, b, A);
      r = ny(k, B, z, S), i = k | 0;
    }
    ({ h: r, l: i } = ft(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = ft(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = ft(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = ft(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: h } = ft(this.Eh | 0, this.El | 0, d | 0, h | 0), { h: p, l: g } = ft(this.Fh | 0, this.Fl | 0, p | 0, g | 0), { h: w, l: f } = ft(this.Gh | 0, this.Gl | 0, w | 0, f | 0), { h: y, l: v } = ft(this.Hh | 0, this.Hl | 0, y | 0, v | 0), this.set(r, i, o, s, a, c, l, u, d, h, p, g, w, f, y, v);
  }
  roundClean() {
    st(It, Pt);
  }
  destroy() {
    this.destroyed = !0, st(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
class py extends fy {
  Ah = ve[0] | 0;
  Al = ve[1] | 0;
  Bh = ve[2] | 0;
  Bl = ve[3] | 0;
  Ch = ve[4] | 0;
  Cl = ve[5] | 0;
  Dh = ve[6] | 0;
  Dl = ve[7] | 0;
  Eh = ve[8] | 0;
  El = ve[9] | 0;
  Fh = ve[10] | 0;
  Fl = ve[11] | 0;
  Gh = ve[12] | 0;
  Gl = ve[13] | 0;
  Hh = ve[14] | 0;
  Hl = ve[15] | 0;
  constructor() {
    super(64);
  }
}
const oe = /* @__PURE__ */ Ti(
  () => new ly(),
  /* @__PURE__ */ Il(1)
), hy = /* @__PURE__ */ Ti(
  () => new py(),
  /* @__PURE__ */ Il(3)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const W = (e, t, n) => ot(e, t, n), Bl = it, Y = is, Le = (...e) => Gh(...e), An = (e) => os(e), cs = Sl, ls = (e) => ss(e), ni = /* @__PURE__ */ BigInt(0), Po = /* @__PURE__ */ BigInt(1);
function Ht(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function us(e) {
  if (typeof e == "bigint") {
    if (!Zr(e))
      throw new RangeError("positive bigint expected, got " + e);
  } else
    Bl(e);
  return e;
}
function ri(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected number, got type=" + typeof e);
  }
  if (!Number.isSafeInteger(e)) {
    const n = t && `"${t}" `;
    throw new RangeError(n + "expected safe integer, got " + e);
  }
}
function Or(e) {
  const t = us(e).toString(16);
  return t.length & 1 ? "0" + t : t;
}
function $l(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  return e === "" ? ni : BigInt("0x" + e);
}
function xr(e) {
  return $l(is(e));
}
function _n(e) {
  return $l(is(To(ot(e)).reverse()));
}
function ds(e, t) {
  if (it(t), t === 0)
    throw new RangeError("zero length");
  e = us(e);
  const n = e.toString(16);
  if (n.length > t * 2)
    throw new RangeError("number too large");
  return os(n.padStart(t * 2, "0"));
}
function Ol(e, t) {
  return ds(e, t).reverse();
}
function yy(e, t) {
  if (e = W(e), t = W(t), e.length !== t.length)
    return !1;
  let n = 0;
  for (let r = 0; r < e.length; r++)
    n |= e[r] ^ t[r];
  return n === 0;
}
function To(e) {
  return Uint8Array.from(W(e));
}
function wy(e) {
  if (typeof e != "string")
    throw new TypeError("ascii string expected, got " + typeof e);
  return Uint8Array.from(e, (t, n) => {
    const r = t.charCodeAt(0);
    if (t.length !== 1 || r > 127)
      throw new RangeError(`string contains non-ASCII character "${e[n]}" with code ${r} at position ${n}`);
    return r;
  });
}
const Zr = (e) => typeof e == "bigint" && ni <= e;
function gy(e, t, n) {
  return Zr(e) && Zr(t) && Zr(n) && t <= e && e < n;
}
function ii(e, t, n, r) {
  if (!gy(t, n, r))
    throw new RangeError("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function fs(e) {
  if (e < ni)
    throw new Error("expected non-negative bigint, got " + e);
  let t;
  for (t = 0; e > ni; e >>= Po, t += 1)
    ;
  return t;
}
const ps = (e) => (Po << BigInt(e)) - Po;
function my(e, t, n) {
  if (it(e, "hashLen"), it(t, "qByteLen"), typeof n != "function")
    throw new TypeError("hmacFn must be a function");
  const r = (f) => new Uint8Array(f), i = Uint8Array.of(), o = Uint8Array.of(0), s = Uint8Array.of(1), a = 1e3;
  let c = r(e), l = r(e), u = 0;
  const d = () => {
    c.fill(1), l.fill(0), u = 0;
  }, h = (...f) => n(l, Le(c, ...f)), p = (f = i) => {
    l = h(o, f), c = h(), f.length !== 0 && (l = h(s, f), c = h());
  }, g = () => {
    if (u++ >= a)
      throw new Error("drbg: tried max amount of iterations");
    let f = 0;
    const y = [];
    for (; f < t; ) {
      c = h();
      const v = c.slice();
      y.push(v), f += c.length;
    }
    return Le(...y);
  };
  return (f, y) => {
    d(), p(f);
    let v;
    for (; (v = y(g())) === void 0; )
      p();
    return d(), v;
  };
}
function On(e, t = {}, n = {}) {
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
const La = () => {
  throw new Error("not implemented");
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const ke = /* @__PURE__ */ BigInt(0), ce = /* @__PURE__ */ BigInt(1), Xt = /* @__PURE__ */ BigInt(2), Rl = /* @__PURE__ */ BigInt(3), Ll = /* @__PURE__ */ BigInt(4), Cl = /* @__PURE__ */ BigInt(5), by = /* @__PURE__ */ BigInt(7), Nl = /* @__PURE__ */ BigInt(8), vy = /* @__PURE__ */ BigInt(9), zl = /* @__PURE__ */ BigInt(16);
function de(e, t) {
  if (t <= ke)
    throw new Error("mod: expected positive modulus, got " + t);
  const n = e % t;
  return n >= ke ? n : t + n;
}
function ie(e, t, n) {
  if (t < ke)
    throw new Error("pow2: expected non-negative exponent, got " + t);
  let r = e;
  for (; t-- > ke; )
    r *= r, r %= n;
  return r;
}
function Ca(e, t) {
  if (e === ke)
    throw new Error("invert: expected non-zero number");
  if (t <= ke)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = de(e, t), r = t, i = ke, o = ce;
  for (; n !== ke; ) {
    const a = r / n, c = r - n * a, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== ce)
    throw new Error("invert: does not exist");
  return de(i, t);
}
function hs(e, t, n) {
  const r = e;
  if (!r.eql(r.sqr(t), n))
    throw new Error("Cannot find square root");
}
function Dl(e, t) {
  const n = e, r = (n.ORDER + ce) / Ll, i = n.pow(t, r);
  return hs(n, i, t), i;
}
function xy(e, t) {
  const n = e, r = (n.ORDER - Cl) / Nl, i = n.mul(t, Xt), o = n.pow(i, r), s = n.mul(t, o), a = n.mul(n.mul(s, Xt), o), c = n.mul(s, n.sub(a, n.ONE));
  return hs(n, c, t), c;
}
function ky(e) {
  const t = Bi(e), n = Ul(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + by) / zl;
  return ((a, c) => {
    const l = a;
    let u = l.pow(c, s), d = l.mul(u, r);
    const h = l.mul(u, i), p = l.mul(u, o), g = l.eql(l.sqr(d), c), w = l.eql(l.sqr(h), c);
    u = l.cmov(u, d, g), d = l.cmov(p, h, w);
    const f = l.eql(l.sqr(d), c), y = l.cmov(u, d, f);
    return hs(l, y, c), y;
  });
}
function Ul(e) {
  if (e < Rl)
    throw new Error("sqrt is not defined for small field");
  let t = e - ce, n = 0;
  for (; t % Xt === ke; )
    t /= Xt, n++;
  let r = Xt;
  const i = Bi(e);
  for (; Na(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return Dl;
  let o = i.pow(r, t);
  const s = (t + ce) / Xt;
  return function(c, l) {
    const u = c;
    if (u.is0(l))
      return l;
    if (Na(u, l) !== 1)
      throw new Error("Cannot find square root");
    let d = n, h = u.mul(u.ONE, o), p = u.pow(l, t), g = u.pow(l, s);
    for (; !u.eql(p, u.ONE); ) {
      if (u.is0(p))
        return u.ZERO;
      let w = 1, f = u.sqr(p);
      for (; !u.eql(f, u.ONE); )
        if (w++, f = u.sqr(f), w === d)
          throw new Error("Cannot find square root");
      const y = ce << BigInt(d - w - 1), v = u.pow(h, y);
      d = w, h = u.sqr(v), p = u.mul(p, h), g = u.mul(g, v);
    }
    return g;
  };
}
function Ey(e) {
  return e % Ll === Rl ? Dl : e % Nl === Cl ? xy : e % zl === vy ? ky(e) : Ul(e);
}
const jt = (e, t) => (de(e, t) & ce) === ce, Sy = [
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
function Ay(e) {
  const t = {
    ORDER: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = Sy.reduce((r, i) => (r[i] = "function", r), t);
  if (On(e, n), ri(e.BYTES, "BYTES"), ri(e.BITS, "BITS"), e.BYTES < 1 || e.BITS < 1)
    throw new Error("invalid field: expected BYTES/BITS > 0");
  if (e.ORDER <= ce)
    throw new Error("invalid field: expected ORDER > 1, got " + e.ORDER);
  return e;
}
function _y(e, t, n) {
  const r = e;
  if (n < ke)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === ke)
    return r.ONE;
  if (n === ce)
    return t;
  let i = r.ONE, o = t;
  for (; n > ke; )
    n & ce && (i = r.mul(i, o)), o = r.sqr(o), n >>= ce;
  return i;
}
function Ml(e, t, n = !1) {
  const r = e, i = new Array(t.length).fill(n ? r.ZERO : void 0), o = t.reduce((a, c, l) => r.is0(c) ? a : (i[l] = a, r.mul(a, c)), r.ONE), s = r.inv(o);
  return t.reduceRight((a, c, l) => r.is0(c) ? a : (i[l] = r.mul(a, i[l]), r.mul(a, c)), s), i;
}
function Na(e, t) {
  const n = e, r = (n.ORDER - ce) / Xt, i = n.pow(t, r), o = n.eql(i, n.ONE), s = n.eql(i, n.ZERO), a = n.eql(i, n.neg(n.ONE));
  if (!o && !s && !a)
    throw new Error("invalid Legendre symbol result");
  return o ? 1 : s ? 0 : -1;
}
function Iy(e, t) {
  if (t !== void 0 && Bl(t), e <= ke)
    throw new Error("invalid n length: expected positive n, got " + e);
  if (t !== void 0 && t < 1)
    throw new Error("invalid n length: expected positive bit length, got " + t);
  const n = fs(e);
  if (t !== void 0 && t < n)
    throw new Error(`invalid n length: expected bit length (${n}) >= n.length (${t})`);
  const r = t !== void 0 ? t : n, i = Math.ceil(r / 8);
  return { nBitLength: r, nByteLength: i };
}
const za = /* @__PURE__ */ new WeakMap();
class Hl {
  ORDER;
  BITS;
  BYTES;
  isLE;
  ZERO = ke;
  ONE = ce;
  _lengths;
  _mod;
  constructor(t, n = {}) {
    if (t <= ce)
      throw new Error("invalid field: expected ORDER > 1, got " + t);
    let r;
    this.isLE = !1, n != null && typeof n == "object" && (typeof n.BITS == "number" && (r = n.BITS), typeof n.sqrt == "function" && Object.defineProperty(this, "sqrt", { value: n.sqrt, enumerable: !0 }), typeof n.isLE == "boolean" && (this.isLE = n.isLE), n.allowedLengths && (this._lengths = Object.freeze(n.allowedLengths.slice())), typeof n.modFromBytes == "boolean" && (this._mod = n.modFromBytes));
    const { nBitLength: i, nByteLength: o } = Iy(t, r);
    if (o > 2048)
      throw new Error("invalid field: expected ORDER of <= 2048 bytes");
    this.ORDER = t, this.BITS = i, this.BYTES = o, Object.freeze(this);
  }
  create(t) {
    return de(t, this.ORDER);
  }
  isValid(t) {
    if (typeof t != "bigint")
      throw new TypeError("invalid field element: expected bigint, got " + typeof t);
    return ke <= t && t < this.ORDER;
  }
  is0(t) {
    return t === ke;
  }
  // is valid and invertible
  isValidNot0(t) {
    return !this.is0(t) && this.isValid(t);
  }
  isOdd(t) {
    return (t & ce) === ce;
  }
  neg(t) {
    return de(-t, this.ORDER);
  }
  eql(t, n) {
    return t === n;
  }
  sqr(t) {
    return de(t * t, this.ORDER);
  }
  add(t, n) {
    return de(t + n, this.ORDER);
  }
  sub(t, n) {
    return de(t - n, this.ORDER);
  }
  mul(t, n) {
    return de(t * n, this.ORDER);
  }
  pow(t, n) {
    return _y(this, t, n);
  }
  div(t, n) {
    return de(t * Ca(n, this.ORDER), this.ORDER);
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
    return Ca(t, this.ORDER);
  }
  sqrt(t) {
    let n = za.get(this);
    return n || za.set(this, n = Ey(this.ORDER)), n(this, t);
  }
  toBytes(t) {
    return this.isLE ? Ol(t, this.BYTES) : ds(t, this.BYTES);
  }
  fromBytes(t, n = !1) {
    W(t);
    const { _lengths: r, BYTES: i, isLE: o, ORDER: s, _mod: a } = this;
    if (r) {
      if (t.length < 1 || !r.includes(t.length) || t.length > i)
        throw new Error("Field.fromBytes: expected " + r + " bytes, got " + t.length);
      const l = new Uint8Array(i);
      l.set(t, o ? 0 : l.length - t.length), t = l;
    }
    if (t.length !== i)
      throw new Error("Field.fromBytes: expected " + i + " bytes, got " + t.length);
    let c = o ? _n(t) : xr(t);
    if (a && (c = de(c, s)), !n && !this.isValid(c))
      throw new Error("invalid field element: outside of range 0..ORDER");
    return c;
  }
  // TODO: we don't need it here, move out to separate fn
  invertBatch(t) {
    return Ml(this, t);
  }
  // We can't move this out because Fp6, Fp12 implement it
  // and it's unclear what to return in there.
  cmov(t, n, r) {
    return Ht(r, "condition"), r ? n : t;
  }
}
Object.freeze(Hl.prototype);
function Bi(e, t = {}) {
  return new Hl(e, t);
}
function Fl(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  if (e <= ce)
    throw new Error("field order must be greater than 1");
  const t = fs(e - ce);
  return Math.ceil(t / 8);
}
function Kl(e) {
  const t = Fl(e);
  return t + Math.ceil(t / 2);
}
function jl(e, t, n = !1) {
  W(e);
  const r = e.length, i = Fl(t), o = Math.max(Kl(t), 16);
  if (r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? _n(e) : xr(e), a = de(s, t - ce) + ce;
  return n ? Ol(a, i) : ds(a, i);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const In = /* @__PURE__ */ BigInt(0), Yt = /* @__PURE__ */ BigInt(1);
function oi(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function rr(e, t) {
  const n = Ml(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function Vl(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function Qi(e, t) {
  Vl(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = ps(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function Da(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += Yt);
  const l = t * r, u = l + Math.abs(a) - 1, d = a === 0, h = a < 0, p = t % 2 !== 0;
  return { nextN: c, offset: u, isZero: d, isNeg: h, isNegF: p, offsetF: l };
}
const eo = /* @__PURE__ */ new WeakMap(), ql = /* @__PURE__ */ new WeakMap();
function to(e) {
  return ql.get(e) || 1;
}
function Ua(e) {
  if (e !== In)
    throw new Error("invalid wNAF");
}
class Gl {
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
    for (; n > In; )
      n & Yt && (r = r.add(i)), i = i.double(), n >>= Yt;
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
    const { windows: r, windowSize: i } = Qi(n, this.bits), o = [];
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
    const s = Qi(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: u, isNeg: d, isNegF: h, offsetF: p } = Da(r, a, s);
      r = c, u ? o = o.add(oi(h, n[p])) : i = i.add(oi(d, n[l]));
    }
    return Ua(r), { p: i, f: o };
  }
  /**
   * Implements unsafe EC multiplication using precomputed tables
   * and w-ary non-adjacent form.
   * @param acc - accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = Qi(t, this.bits);
    for (let s = 0; s < o.windows && r !== In; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: u } = Da(r, s, o);
      if (r = a, !l) {
        const d = n[c];
        i = i.add(u ? d.negate() : d);
      }
    }
    return Ua(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = eo.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), eo.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = to(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = to(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    Vl(n, this.bits), ql.set(t, n), eo.delete(t);
  }
  hasCache(t) {
    return to(t) !== 1;
  }
}
function Py(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > In || r > In; )
    n & Yt && (o = o.add(i)), r & Yt && (s = s.add(i)), i = i.double(), n >>= Yt, r >>= Yt;
  return { p1: o, p2: s };
}
function Ma(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return Ay(t), t;
  } else
    return Bi(e, { isLE: n });
}
function Wl(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > In))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = Ma(t.p, n.Fp, r), o = Ma(t.n, n.Fn, r), a = ["Gx", "Gy", "a", e === "weierstrass" ? "b" : "d"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
function ys(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Tt = /* @__PURE__ */ BigInt(0), we = /* @__PURE__ */ BigInt(1), no = /* @__PURE__ */ BigInt(2), Ty = /* @__PURE__ */ BigInt(8);
function By(e, t, n, r) {
  const i = e.sqr(n), o = e.sqr(r), s = e.add(e.mul(t.a, i), o), a = e.add(e.ONE, e.mul(t.d, e.mul(i, o)));
  return e.eql(s, a);
}
function $y(e, t = {}) {
  const n = t, r = Wl("edwards", e, n, n.FpFnLE), { Fp: i, Fn: o } = r;
  let s = r.CURVE;
  const { h: a } = s;
  On(n, {}, { uvRatio: "function" });
  const c = no << BigInt(o.BYTES * 8) - we, l = (w) => i.create(w), u = n.uvRatio === void 0 ? (w, f) => {
    try {
      return { isValid: !0, value: i.sqrt(i.div(w, f)) };
    } catch {
      return { isValid: !1, value: Tt };
    }
  } : n.uvRatio;
  if (!By(i, s, s.Gx, s.Gy))
    throw new Error("bad curve params: generator point");
  function d(w, f, y = !1) {
    const v = y ? we : Tt;
    return ii("coordinate " + w, f, v, c), f;
  }
  function h(w) {
    if (!(w instanceof p))
      throw new Error("EdwardsPoint expected");
  }
  class p {
    // base / generator point
    static BASE = new p(s.Gx, s.Gy, we, l(s.Gx * s.Gy));
    // zero / infinity / identity point
    static ZERO = new p(Tt, we, we, Tt);
    // 0, 1, 1, 0
    // math field
    static Fp = i;
    // scalar field
    static Fn = o;
    X;
    Y;
    Z;
    T;
    constructor(f, y, v, x) {
      this.X = d("x", f), this.Y = d("y", y), this.Z = d("z", v, !0), this.T = d("t", x), Object.freeze(this);
    }
    static CURVE() {
      return s;
    }
    /**
     * Create one extended Edwards point from affine coordinates.
     * Does NOT validate that the point is on-curve or torsion-free.
     * Use `.assertValidity()` on adversarial inputs.
     */
    static fromAffine(f) {
      if (f instanceof p)
        throw new Error("extended point not allowed");
      const { x: y, y: v } = f || {};
      return d("x", y), d("y", v), new p(y, v, we, l(y * v));
    }
    // Uses algo from RFC8032 5.1.3.
    static fromBytes(f, y = !1) {
      const v = i.BYTES, { a: x, d: I } = s;
      f = To(W(f, v, "point")), Ht(y, "zip215");
      const E = To(f), T = f[v - 1];
      E[v - 1] = T & -129;
      const P = _n(E), O = y ? c : i.ORDER;
      ii("point.y", P, Tt, O);
      const B = l(P * P), N = l(B - we), z = l(I * B - x);
      let { isValid: b, value: S } = u(N, z);
      if (!b)
        throw new Error("bad point: invalid y coordinate");
      const A = (S & we) === we, k = (T & 128) !== 0;
      if (!y && S === Tt && k)
        throw new Error("bad point: x=0 and x_0=1");
      return k !== A && (S = l(-S)), p.fromAffine({ x: S, y: P });
    }
    static fromHex(f, y = !1) {
      return p.fromBytes(An(f), y);
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    precompute(f = 8, y = !0) {
      return g.createCache(this, f), y || this.multiply(no), this;
    }
    // Useful in fromAffine() - not for fromBytes(), which always created valid points.
    assertValidity() {
      const f = this, { a: y, d: v } = s;
      if (f.is0())
        throw new Error("bad point: ZERO");
      const { X: x, Y: I, Z: E, T } = f, P = l(x * x), O = l(I * I), B = l(E * E), N = l(B * B), z = l(P * y), b = l(B * l(z + O)), S = l(N + l(v * l(P * O)));
      if (b !== S)
        throw new Error("bad point: equation left != right (1)");
      const A = l(x * I), k = l(E * T);
      if (A !== k)
        throw new Error("bad point: equation left != right (2)");
    }
    // Compare one point to another.
    equals(f) {
      h(f);
      const { X: y, Y: v, Z: x } = this, { X: I, Y: E, Z: T } = f, P = l(y * T), O = l(I * x), B = l(v * T), N = l(E * x);
      return P === O && B === N;
    }
    is0() {
      return this.equals(p.ZERO);
    }
    negate() {
      return new p(l(-this.X), this.Y, this.Z, l(-this.T));
    }
    // Fast algo for doubling Extended Point.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#doubling-dbl-2008-hwcd
    // Cost: 4M + 4S + 1*a + 6add + 1*2.
    double() {
      const { a: f } = s, { X: y, Y: v, Z: x } = this, I = l(y * y), E = l(v * v), T = l(no * l(x * x)), P = l(f * I), O = y + v, B = l(l(O * O) - I - E), N = P + E, z = N - T, b = P - E, S = l(B * z), A = l(N * b), k = l(B * b), _ = l(z * N);
      return new p(S, A, _, k);
    }
    // Fast algo for adding 2 Extended Points.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#addition-add-2008-hwcd
    // Cost: 9M + 1*a + 1*d + 7add.
    add(f) {
      h(f);
      const { a: y, d: v } = s, { X: x, Y: I, Z: E, T } = this, { X: P, Y: O, Z: B, T: N } = f, z = l(x * P), b = l(I * O), S = l(T * v * N), A = l(E * B), k = l((x + I) * (P + O) - z - b), _ = A - S, $ = A + S, L = l(b - y * z), R = l(k * _), C = l($ * L), D = l(k * L), U = l(_ * $);
      return new p(R, C, U, D);
    }
    subtract(f) {
      return h(f), this.add(f.negate());
    }
    // Constant-time multiplication.
    multiply(f) {
      if (!o.isValidNot0(f))
        throw new RangeError("invalid scalar: expected 1 <= sc < curve.n");
      const { p: y, f: v } = g.cached(this, f, (x) => rr(p, x));
      return rr(p, [y, v])[0];
    }
    // Non-constant-time multiplication. Uses double-and-add algorithm.
    // It's faster, but should only be used when you don't care about
    // an exposed private key e.g. sig verification.
    // Keeps the same subgroup-scalar contract: 0 is allowed for public-scalar callers, but
    // n and larger values are rejected instead of being reduced mod n to the identity point.
    multiplyUnsafe(f) {
      if (!o.isValid(f))
        throw new RangeError("invalid scalar: expected 0 <= sc < curve.n");
      return f === Tt ? p.ZERO : this.is0() || f === we ? this : g.unsafe(this, f, (y) => rr(p, y));
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
    toAffine(f) {
      const y = this;
      let v = f;
      const { X: x, Y: I, Z: E } = y, T = y.is0();
      v == null && (v = T ? Ty : i.inv(E));
      const P = l(x * v), O = l(I * v), B = i.mul(E, v);
      if (T)
        return { x: Tt, y: we };
      if (B !== we)
        throw new Error("invZ was invalid");
      return { x: P, y: O };
    }
    clearCofactor() {
      return a === we ? this : this.multiplyUnsafe(a);
    }
    toBytes() {
      const { x: f, y } = this.toAffine(), v = i.toBytes(y);
      return v[v.length - 1] |= f & we ? 128 : 0, v;
    }
    toHex() {
      return Y(this.toBytes());
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const g = new Gl(p, o.BITS);
  return o.BITS >= 8 && p.BASE.precompute(8), Object.freeze(p.prototype), Object.freeze(p), p;
}
class Oy {
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
    La();
  }
  static fromHex(t) {
    La();
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
    return Y(this.toBytes());
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
function Ry(e, t, n = {}) {
  if (typeof t != "function")
    throw new Error('"hash" function param is required');
  const r = t, i = n;
  On(i, {}, {
    adjustScalarBytes: "function",
    randomBytes: "function",
    domain: "function",
    prehash: "function",
    zip215: "boolean",
    mapToCurve: "function"
  });
  const { prehash: o } = i, { BASE: s, Fp: a, Fn: c } = e, l = r.outputLen, u = 2 * a.BYTES;
  if (l !== void 0 && (ri(l, "hash.outputLen"), l !== u))
    throw new Error(`hash.outputLen must be ${u}, got ${l}`);
  const d = i.randomBytes === void 0 ? ls : i.randomBytes, h = i.adjustScalarBytes === void 0 ? (b) => b : i.adjustScalarBytes, p = i.domain === void 0 ? (b, S, A) => {
    if (Ht(A, "phflag"), S.length || A)
      throw new Error("Contexts/pre-hash are not supported");
    return b;
  } : i.domain;
  function g(b) {
    return c.create(_n(b));
  }
  function w(b) {
    const S = P.secretKey;
    W(b, P.secretKey, "secretKey");
    const A = W(r(b), 2 * S, "hashedSecretKey"), k = h(A.slice(0, S)), _ = A.slice(S, 2 * S), $ = g(k);
    return { head: k, prefix: _, scalar: $ };
  }
  function f(b) {
    const { head: S, prefix: A, scalar: k } = w(b), _ = s.multiply(k), $ = _.toBytes();
    return { head: S, prefix: A, scalar: k, point: _, pointBytes: $ };
  }
  function y(b) {
    return f(b).pointBytes;
  }
  function v(b = Uint8Array.of(), ...S) {
    const A = Le(...S);
    return g(r(p(A, W(b, void 0, "context"), !!o)));
  }
  function x(b, S, A = {}) {
    b = W(b, void 0, "message"), o && (b = o(b));
    const { prefix: k, scalar: _, pointBytes: $ } = f(S), L = v(A.context, k, b), R = s.multiply(L).toBytes(), C = v(A.context, R, $, b), D = c.create(L + C * _);
    if (!c.isValid(D))
      throw new Error("sign failed: invalid s");
    const U = Le(R, c.toBytes(D));
    return W(U, P.signature, "result");
  }
  const I = {
    zip215: i.zip215
  };
  function E(b, S, A, k = I) {
    const { context: _ } = k, $ = k.zip215 === void 0 ? !!I.zip215 : k.zip215, L = P.signature;
    b = W(b, L, "signature"), S = W(S, void 0, "message"), A = W(A, P.publicKey, "publicKey"), $ !== void 0 && Ht($, "zip215"), o && (S = o(S));
    const R = L / 2, C = b.subarray(0, R), D = _n(b.subarray(R, L));
    let U, Z, M;
    try {
      U = e.fromBytes(A, $), Z = e.fromBytes(C, $), M = s.multiplyUnsafe(D);
    } catch {
      return !1;
    }
    if (!$ && U.isSmallOrder())
      return !1;
    const te = v(_, C, A, S);
    return Z.add(U.multiplyUnsafe(te)).subtract(M).clearCofactor().is0();
  }
  const T = a.BYTES, P = {
    secretKey: T,
    publicKey: T,
    signature: 2 * T,
    seed: T
  };
  function O(b) {
    return b = b === void 0 ? d(P.seed) : b, W(b, P.seed, "seed");
  }
  function B(b) {
    return cs(b) && b.length === P.secretKey;
  }
  function N(b, S) {
    try {
      return !!e.fromBytes(b, S === void 0 ? I.zip215 : S);
    } catch {
      return !1;
    }
  }
  const z = {
    getExtendedPublicKey: f,
    randomSecretKey: O,
    isValidSecretKey: B,
    isValidPublicKey: N,
    /**
     * Converts ed public key to x public key. Uses formula:
     * - ed25519:
     *   - `(u, v) = ((1+y)/(1-y), sqrt(-486664)*u/x)`
     *   - `(x, y) = (sqrt(-486664)*u/v, (u-1)/(u+1))`
     * - ed448:
     *   - `(u, v) = ((y-1)/(y+1), sqrt(156324)*u/x)`
     *   - `(x, y) = (sqrt(156324)*u/v, (1+u)/(1-u))`
     */
    toMontgomery(b) {
      const { y: S } = e.fromBytes(b), A = P.publicKey, k = A === 32;
      if (!k && A !== 57)
        throw new Error("only defined for 25519 and 448");
      const _ = k ? a.div(we + S, we - S) : a.div(S - we, S + we);
      return a.toBytes(_);
    },
    toMontgomerySecret(b) {
      const S = P.secretKey;
      W(b, S);
      const A = r(b.subarray(0, S));
      return h(A).subarray(0, S);
    }
  };
  return Object.freeze(P), Object.freeze(z), Object.freeze({
    keygen: ys(O, y),
    getPublicKey: y,
    sign: x,
    verify: E,
    utils: z,
    Point: e,
    lengths: P
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Ly = /* @__PURE__ */ BigInt(0), ir = /* @__PURE__ */ BigInt(1), Ha = /* @__PURE__ */ BigInt(2), Cy = /* @__PURE__ */ BigInt(5), Ny = /* @__PURE__ */ BigInt(8), cr = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed"), Zl = {
  p: cr,
  n: BigInt("0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"),
  h: Ny,
  a: BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffec"),
  d: BigInt("0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3"),
  Gx: BigInt("0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a"),
  Gy: BigInt("0x6666666666666666666666666666666666666666666666666666666666666658")
};
function zy(e) {
  const t = BigInt(10), n = BigInt(20), r = BigInt(40), i = BigInt(80), o = cr, a = e * e % o * e % o, c = ie(a, Ha, o) * a % o, l = ie(c, ir, o) * e % o, u = ie(l, Cy, o) * l % o, d = ie(u, t, o) * u % o, h = ie(d, n, o) * d % o, p = ie(h, r, o) * h % o, g = ie(p, i, o) * p % o, w = ie(g, i, o) * p % o, f = ie(w, t, o) * u % o;
  return { pow_p_5_8: ie(f, Ha, o) * e % o, b2: a };
}
function Dy(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const Bo = /* @__PURE__ */ BigInt("19681161376707505956807079304988542015446066515923890162744021073123829784752");
function Xl(e, t) {
  const n = cr, r = de(t * t * t, n), i = de(r * r * t, n), o = zy(e * i).pow_p_5_8;
  let s = de(e * r * o, n);
  const a = de(t * s * s, n), c = s, l = de(s * Bo, n), u = a === e, d = a === de(-e, n), h = a === de(-e * Bo, n);
  return u && (s = c), (d || h) && (s = l), jt(s, n) && (s = de(-s, n)), { isValid: u || d, value: s };
}
const Jt = /* @__PURE__ */ $y(Zl, { uvRatio: Xl }), Vt = Jt.Fp, Uy = Jt.Fn;
function My(e) {
  return Ry(Jt, hy, Object.assign({ adjustScalarBytes: Dy, zip215: !0 }, e));
}
const Nt = /* @__PURE__ */ My({}), Fa = Bo, Hy = /* @__PURE__ */ BigInt("54469307008909316920995813868745141605393597292927456921205312896311721017578"), Ka = (e) => Xl(ir, e), Fy = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), Ky = (e) => Vt.create(_n(e) & Fy);
class Be extends Oy {
  // Do NOT change syntax: the following gymnastics is done,
  // because typescript strips comments, which makes bundlers disable tree-shaking.
  // prettier-ignore
  static BASE = new Be(Jt.BASE);
  // prettier-ignore
  static ZERO = new Be(Jt.ZERO);
  // prettier-ignore
  static Fp = Vt;
  // prettier-ignore
  static Fn = Uy;
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
    return new Be(Jt.fromAffine(t));
  }
  assertSame(t) {
    if (!(t instanceof Be))
      throw new Error("RistrettoPoint expected");
  }
  init(t) {
    return new Be(t);
  }
  static fromBytes(t) {
    ot(t, 32);
    const { a: n, d: r } = Zl, i = cr, o = (I) => Vt.create(I), s = Ky(t);
    if (!yy(Vt.toBytes(s), t) || jt(s, i))
      throw new Error("invalid ristretto255 encoding 1");
    const a = o(s * s), c = o(ir + n * a), l = o(ir - n * a), u = o(c * c), d = o(l * l), h = o(n * r * u - d), { isValid: p, value: g } = Ka(o(h * d)), w = o(g * l), f = o(g * w * h);
    let y = o((s + s) * w);
    jt(y, i) && (y = o(-y));
    const v = o(c * f), x = o(y * v);
    if (!p || jt(x, i) || v === Ly)
      throw new Error("invalid ristretto255 encoding 2");
    return new Be(new Jt(y, v, ir, x));
  }
  /**
   * Converts ristretto-encoded string to ristretto point.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-decode).
   * @param hex - Ristretto-encoded 32 bytes. Not every 32-byte string is valid ristretto encoding
   */
  static fromHex(t) {
    return Be.fromBytes(os(t));
  }
  /**
   * Encodes ristretto point to Uint8Array.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-encode).
   */
  toBytes() {
    let { X: t, Y: n, Z: r, T: i } = this.ep;
    const o = cr, s = (f) => Vt.create(f), a = s(s(r + n) * s(r - n)), c = s(t * n), l = s(c * c), { value: u } = Ka(s(a * l)), d = s(u * a), h = s(u * c), p = s(d * h * i);
    let g;
    if (jt(i * p, o)) {
      let f = s(n * Fa), y = s(t * Fa);
      t = f, n = y, g = s(d * Hy);
    } else
      g = h;
    jt(t * p, o) && (n = s(-n));
    let w = s((r - n) * g);
    return jt(w, o) && (w = s(-w)), Vt.toBytes(w);
  }
  /**
   * Compares two Ristretto points.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-equals).
   */
  equals(t) {
    this.assertSame(t);
    const { X: n, Y: r } = this.ep, { X: i, Y: o } = t.ep, s = (l) => Vt.create(l), a = s(n * o) === s(r * i), c = s(r * o) === s(n * i);
    return a || c;
  }
  is0() {
    return this.equals(Be.ZERO);
  }
}
Object.freeze(Be.BASE);
Object.freeze(Be.ZERO);
Object.freeze(Be.prototype);
Object.freeze(Be);
class ja {
  oHash;
  iHash;
  blockLen;
  outputLen;
  canXOF = !1;
  finished = !1;
  destroyed = !1;
  constructor(t, n) {
    if (Al(t), ot(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), st(i);
  }
  update(t) {
    return Sn(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    Sn(this), rs(t, this), this.finished = !0;
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
const jy = /* @__PURE__ */ (() => {
  const e = ((t, n, r) => new ja(t, n).update(r).digest());
  return e.create = (t, n) => new ja(t, n), e;
})();
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Va = (e, t) => (e + (e >= 0 ? t : -t) / Yl) / t;
function Vy(e, t, n) {
  ii("scalar", e, tt, n);
  const [[r, i], [o, s]] = t, a = Va(s * e, n), c = Va(-i * e, n);
  let l = e - a * r - c * o, u = -a * i - c * s;
  const d = l < tt, h = u < tt;
  d && (l = -l), h && (u = -u);
  const p = ps(Math.ceil(fs(n) / 2)) + Ct;
  if (l < tt || l >= p || u < tt || u >= p)
    throw new Error("splitScalar (endomorphism): failed for k");
  return { k1neg: d, k1: l, k2neg: h, k2: u };
}
function $o(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function ro(e, t) {
  On(e);
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Ht(n.lowS, "lowS"), Ht(n.prehash, "prehash"), n.format !== void 0 && $o(n.format), n;
}
class qy extends Error {
  constructor(t = "") {
    super(t);
  }
}
const He = {
  // asn.1 DER encoding utils
  Err: qy,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = He;
      if (ri(e, "tag"), e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (typeof t != "string")
        throw new TypeError('"data" expected string, got type=' + typeof t);
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = Or(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? Or(i.length / 2 | 128) : "";
      return Or(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = He;
      t = W(t, void 0, "DER data");
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
      const { Err: t } = He;
      if (us(e), e < tt)
        throw new t("integer: negative integers are not allowed");
      let n = Or(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = He;
      if (e.length < 1)
        throw new t("invalid signature integer: empty");
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e.length > 1 && e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return xr(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = He, i = W(e, void 0, "signature"), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: u } = r.decode(2, c);
    if (u.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = He, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
};
Object.freeze(He._tlv);
Object.freeze(He._int);
Object.freeze(He);
const tt = /* @__PURE__ */ BigInt(0), Ct = /* @__PURE__ */ BigInt(1), Yl = /* @__PURE__ */ BigInt(2), Rr = /* @__PURE__ */ BigInt(3), Gy = /* @__PURE__ */ BigInt(4);
function Wy(e, t = {}) {
  const n = Wl("weierstrass", e, t), r = n.Fp, i = n.Fn;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  On(t, {}, {
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
  const u = Ql(r, i);
  function d() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function h(z, b, S) {
    if (l && b.is0())
      return Uint8Array.of(0);
    const { x: A, y: k } = b.toAffine(), _ = r.toBytes(A);
    if (Ht(S, "isCompressed"), S) {
      d();
      const $ = !r.isOdd(k);
      return Le(Jl($), _);
    } else
      return Le(Uint8Array.of(4), _, r.toBytes(k));
  }
  function p(z) {
    W(z, void 0, "Point");
    const { publicKey: b, publicKeyUncompressed: S } = u, A = z.length, k = z[0], _ = z.subarray(1);
    if (l && A === 1 && k === 0)
      return { x: r.ZERO, y: r.ZERO };
    if (A === b && (k === 2 || k === 3)) {
      const $ = r.fromBytes(_);
      if (!r.isValid($))
        throw new Error("bad point: is not on curve, wrong x");
      const L = f($);
      let R;
      try {
        R = r.sqrt(L);
      } catch (U) {
        const Z = U instanceof Error ? ": " + U.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + Z);
      }
      d();
      const C = r.isOdd(R);
      return (k & 1) === 1 !== C && (R = r.neg(R)), { x: $, y: R };
    } else if (A === S && k === 4) {
      const $ = r.BYTES, L = r.fromBytes(_.subarray(0, $)), R = r.fromBytes(_.subarray($, $ * 2));
      if (!y(L, R))
        throw new Error("bad point: is not on curve");
      return { x: L, y: R };
    } else
      throw new Error(`bad point: got length ${A}, expected compressed=${b} or uncompressed=${S}`);
  }
  const g = t.toBytes === void 0 ? h : t.toBytes, w = t.fromBytes === void 0 ? p : t.fromBytes;
  function f(z) {
    const b = r.sqr(z), S = r.mul(b, z);
    return r.add(r.add(S, r.mul(z, o.a)), o.b);
  }
  function y(z, b) {
    const S = r.sqr(b), A = f(z);
    return r.eql(S, A);
  }
  if (!y(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const v = r.mul(r.pow(o.a, Rr), Gy), x = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(v, x)))
    throw new Error("bad curve params: a or b");
  function I(z, b, S = !1) {
    if (!r.isValid(b) || S && r.is0(b))
      throw new Error(`bad point coordinate ${z}`);
    return b;
  }
  function E(z) {
    if (!(z instanceof O))
      throw new Error("Weierstrass Point expected");
  }
  function T(z) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return Vy(z, c.basises, i.ORDER);
  }
  function P(z, b, S, A, k) {
    return S = new O(r.mul(S.X, z), S.Y, S.Z), b = oi(A, b), S = oi(k, S), b.add(S);
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
    constructor(b, S, A) {
      this.X = I("x", b), this.Y = I("y", S, !0), this.Z = I("z", A), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(b) {
      const { x: S, y: A } = b || {};
      if (!b || !r.isValid(S) || !r.isValid(A))
        throw new Error("invalid affine point");
      if (b instanceof O)
        throw new Error("projective point not allowed");
      return r.is0(S) && r.is0(A) ? O.ZERO : new O(S, A, r.ONE);
    }
    static fromBytes(b) {
      const S = O.fromAffine(w(W(b, void 0, "point")));
      return S.assertValidity(), S;
    }
    static fromHex(b) {
      return O.fromBytes(An(b));
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
    precompute(b = 8, S = !0) {
      return N.createCache(this, b), S || this.multiply(Rr), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      const b = this;
      if (b.is0()) {
        if (t.allowInfinityPoint && r.is0(b.X) && r.eql(b.Y, r.ONE) && r.is0(b.Z))
          return;
        throw new Error("bad point: ZERO");
      }
      const { x: S, y: A } = b.toAffine();
      if (!r.isValid(S) || !r.isValid(A))
        throw new Error("bad point: x or y not field elements");
      if (!y(S, A))
        throw new Error("bad point: equation left != right");
      if (!b.isTorsionFree())
        throw new Error("bad point: not in prime-order subgroup");
    }
    hasEvenY() {
      const { y: b } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(b);
    }
    /** Compare one point to another. */
    equals(b) {
      E(b);
      const { X: S, Y: A, Z: k } = this, { X: _, Y: $, Z: L } = b, R = r.eql(r.mul(S, L), r.mul(_, k)), C = r.eql(r.mul(A, L), r.mul($, k));
      return R && C;
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
      const { a: b, b: S } = o, A = r.mul(S, Rr), { X: k, Y: _, Z: $ } = this;
      let L = r.ZERO, R = r.ZERO, C = r.ZERO, D = r.mul(k, k), U = r.mul(_, _), Z = r.mul($, $), M = r.mul(k, _);
      return M = r.add(M, M), C = r.mul(k, $), C = r.add(C, C), L = r.mul(b, C), R = r.mul(A, Z), R = r.add(L, R), L = r.sub(U, R), R = r.add(U, R), R = r.mul(L, R), L = r.mul(M, L), C = r.mul(A, C), Z = r.mul(b, Z), M = r.sub(D, Z), M = r.mul(b, M), M = r.add(M, C), C = r.add(D, D), D = r.add(C, D), D = r.add(D, Z), D = r.mul(D, M), R = r.add(R, D), Z = r.mul(_, $), Z = r.add(Z, Z), D = r.mul(Z, M), L = r.sub(L, D), C = r.mul(Z, U), C = r.add(C, C), C = r.add(C, C), new O(L, R, C);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(b) {
      E(b);
      const { X: S, Y: A, Z: k } = this, { X: _, Y: $, Z: L } = b;
      let R = r.ZERO, C = r.ZERO, D = r.ZERO;
      const U = o.a, Z = r.mul(o.b, Rr);
      let M = r.mul(S, _), te = r.mul(A, $), ne = r.mul(k, L), re = r.add(S, A), ee = r.add(_, $);
      re = r.mul(re, ee), ee = r.add(M, te), re = r.sub(re, ee), ee = r.add(S, k);
      let me = r.add(_, L);
      return ee = r.mul(ee, me), me = r.add(M, ne), ee = r.sub(ee, me), me = r.add(A, k), R = r.add($, L), me = r.mul(me, R), R = r.add(te, ne), me = r.sub(me, R), D = r.mul(U, ee), R = r.mul(Z, ne), D = r.add(R, D), R = r.sub(te, D), D = r.add(te, D), C = r.mul(R, D), te = r.add(M, M), te = r.add(te, M), ne = r.mul(U, ne), ee = r.mul(Z, ee), te = r.add(te, ne), ne = r.sub(M, ne), ne = r.mul(U, ne), ee = r.add(ee, ne), M = r.mul(te, ee), C = r.add(C, M), M = r.mul(me, ee), R = r.mul(re, R), R = r.sub(R, M), M = r.mul(re, te), D = r.mul(me, D), D = r.add(D, M), new O(R, C, D);
    }
    subtract(b) {
      return E(b), this.add(b.negate());
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
    multiply(b) {
      const { endo: S } = t;
      if (!i.isValidNot0(b))
        throw new RangeError("invalid scalar: out of range");
      let A, k;
      const _ = ($) => N.cached(this, $, (L) => rr(O, L));
      if (S) {
        const { k1neg: $, k1: L, k2neg: R, k2: C } = T(b), { p: D, f: U } = _(L), { p: Z, f: M } = _(C);
        k = U.add(M), A = P(S.beta, D, Z, $, R);
      } else {
        const { p: $, f: L } = _(b);
        A = $, k = L;
      }
      return rr(O, [A, k])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(b) {
      const { endo: S } = t, A = this, k = b;
      if (!i.isValid(k))
        throw new RangeError("invalid scalar: out of range");
      if (k === tt || A.is0())
        return O.ZERO;
      if (k === Ct)
        return A;
      if (N.hasCache(this))
        return this.multiply(k);
      if (S) {
        const { k1neg: _, k1: $, k2neg: L, k2: R } = T(k), { p1: C, p2: D } = Py(O, A, $, R);
        return P(S.beta, C, D, _, L);
      } else
        return N.unsafe(A, k);
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * (X, Y, Z) ∋ (x=X/Z, y=Y/Z).
     * @param invertedZ - Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(b) {
      const S = this;
      let A = b;
      const { X: k, Y: _, Z: $ } = S;
      if (r.eql($, r.ONE))
        return { x: k, y: _ };
      const L = S.is0();
      A == null && (A = L ? r.ONE : r.inv($));
      const R = r.mul(k, A), C = r.mul(_, A), D = r.mul($, A);
      if (L)
        return { x: r.ZERO, y: r.ZERO };
      if (!r.eql(D, r.ONE))
        throw new Error("invZ was invalid");
      return { x: R, y: C };
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: b } = t;
      return s === Ct ? !0 : b ? b(O, this) : N.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: b } = t;
      return s === Ct ? this : b ? b(O, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return s === Ct ? this.is0() : this.clearCofactor().is0();
    }
    toBytes(b = !0) {
      return Ht(b, "isCompressed"), this.assertValidity(), g(O, this, b);
    }
    toHex(b = !0) {
      return Y(this.toBytes(b));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const B = i.BITS, N = new Gl(O, t.endo ? Math.ceil(B / 2) : B);
  return B >= 8 && O.BASE.precompute(8), Object.freeze(O.prototype), Object.freeze(O), O;
}
function Jl(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function Ql(e, t) {
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
function Zy(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes === void 0 ? ls : t.randomBytes, i = Object.assign(Ql(e.Fp, n), {
    seed: Math.max(Kl(n.ORDER), 16)
  });
  function o(p) {
    try {
      const g = n.fromBytes(p);
      return n.isValidNot0(g);
    } catch {
      return !1;
    }
  }
  function s(p, g) {
    const { publicKey: w, publicKeyUncompressed: f } = i;
    try {
      const y = p.length;
      return g === !0 && y !== w || g === !1 && y !== f ? !1 : !!e.fromBytes(p);
    } catch {
      return !1;
    }
  }
  function a(p) {
    return p = p === void 0 ? r(i.seed) : p, jl(W(p, i.seed, "seed"), n.ORDER);
  }
  function c(p, g = !0) {
    return e.BASE.multiply(n.fromBytes(p)).toBytes(g);
  }
  function l(p) {
    const { secretKey: g, publicKey: w, publicKeyUncompressed: f } = i, y = n._lengths;
    if (!cs(p))
      return;
    const v = W(p, void 0, "key").length, x = v === w || v === f, I = v === g || !!y?.includes(v);
    if (!(x && I))
      return x;
  }
  function u(p, g, w = !0) {
    if (l(p) === !0)
      throw new Error("first arg must be private key");
    if (l(g) === !1)
      throw new Error("second arg must be public key");
    const f = n.fromBytes(p);
    return e.fromBytes(g).multiply(f).toBytes(w);
  }
  const d = {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a
  }, h = ys(a, c);
  return Object.freeze(d), Object.freeze(i), Object.freeze({ getPublicKey: c, getSharedSecret: u, keygen: h, Point: e, utils: d, lengths: i });
}
function Xy(e, t, n = {}) {
  const r = t;
  Al(r), On(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  }), n = Object.assign({}, n);
  const i = n.randomBytes === void 0 ? ls : n.randomBytes, o = n.hmac === void 0 ? (k, _) => jy(r, k, _) : n.hmac, { Fp: s, Fn: a } = e, { ORDER: c, BITS: l } = a, { keygen: u, getPublicKey: d, getSharedSecret: h, utils: p, lengths: g } = Zy(e, n), w = {
    prehash: !0,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !0,
    format: "compact",
    extraEntropy: !1
  }, f = c * Yl + Ct < s.ORDER;
  function y(k) {
    const _ = c >> Ct;
    return k > _;
  }
  function v(k, _) {
    if (!a.isValidNot0(_))
      throw new Error(`invalid signature ${k}: out of range 1..Point.Fn.ORDER`);
    return _;
  }
  function x() {
    if (f)
      throw new Error('"recovered" sig type is not supported for cofactor >2 curves');
  }
  function I(k, _) {
    $o(_);
    const $ = g.signature, L = _ === "compact" ? $ : _ === "recovered" ? $ + 1 : void 0;
    return W(k, L);
  }
  class E {
    r;
    s;
    recovery;
    constructor(_, $, L) {
      if (this.r = v("r", _), this.s = v("s", $), L != null) {
        if (x(), ![0, 1, 2, 3].includes(L))
          throw new Error("invalid recovery id");
        this.recovery = L;
      }
      Object.freeze(this);
    }
    static fromBytes(_, $ = w.format) {
      I(_, $);
      let L;
      if ($ === "der") {
        const { r: U, s: Z } = He.toSig(W(_));
        return new E(U, Z);
      }
      $ === "recovered" && (L = _[0], $ = "compact", _ = _.subarray(1));
      const R = g.signature / 2, C = _.subarray(0, R), D = _.subarray(R, R * 2);
      return new E(a.fromBytes(C), a.fromBytes(D), L);
    }
    static fromHex(_, $) {
      return this.fromBytes(An(_), $);
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
      const { r: $, s: L } = this, R = this.assertRecovery(), C = R === 2 || R === 3 ? $ + c : $;
      if (!s.isValid(C))
        throw new Error("invalid recovery id: sig.r+curve.n != R.x");
      const D = s.toBytes(C), U = e.fromBytes(Le(Jl((R & 1) === 0), D)), Z = a.inv(C), M = P(W(_, void 0, "msgHash")), te = a.create(-M * Z), ne = a.create(L * Z), re = e.BASE.multiplyUnsafe(te).add(U.multiplyUnsafe(ne));
      if (re.is0())
        throw new Error("invalid recovery: point at infinify");
      return re.assertValidity(), re;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return y(this.s);
    }
    toBytes(_ = w.format) {
      if ($o(_), _ === "der")
        return An(He.hexFromSig(this));
      const { r: $, s: L } = this, R = a.toBytes($), C = a.toBytes(L);
      return _ === "recovered" ? (x(), Le(Uint8Array.of(this.assertRecovery()), R, C)) : Le(R, C);
    }
    toHex(_) {
      return Y(this.toBytes(_));
    }
  }
  Object.freeze(E.prototype), Object.freeze(E);
  const T = n.bits2int === void 0 ? function(_) {
    if (_.length > 8192)
      throw new Error("input is too large");
    const $ = xr(_), L = _.length * 8 - l;
    return L > 0 ? $ >> BigInt(L) : $;
  } : n.bits2int, P = n.bits2int_modN === void 0 ? function(_) {
    return a.create(T(_));
  } : n.bits2int_modN, O = ps(l);
  function B(k) {
    return ii("num < 2^" + l, k, tt, O), a.toBytes(k);
  }
  function N(k, _) {
    return W(k, void 0, "message"), _ ? W(r(k), void 0, "prehashed message") : k;
  }
  function z(k, _, $) {
    const { lowS: L, prehash: R, extraEntropy: C } = ro($, w);
    k = N(k, R);
    const D = P(k), U = a.fromBytes(_);
    if (!a.isValidNot0(U))
      throw new Error("invalid private key");
    const Z = [B(U), B(D)];
    if (C != null && C !== !1) {
      const re = C === !0 ? i(g.secretKey) : C;
      Z.push(W(re, void 0, "extraEntropy"));
    }
    const M = Le(...Z), te = D;
    function ne(re) {
      const ee = T(re);
      if (!a.isValidNot0(ee))
        return;
      const me = a.inv(ee), lt = e.BASE.multiply(ee).toAffine(), Kt = a.create(lt.x);
      if (Kt === tt)
        return;
      const ln = a.create(me * a.create(te + Kt * U));
      if (ln === tt)
        return;
      let Dn = (lt.x === Kt ? 0 : 2) | Number(lt.y & Ct), Un = ln;
      return L && y(ln) && (Un = a.neg(ln), Dn ^= 1), new E(Kt, Un, f ? void 0 : Dn);
    }
    return { seed: M, k2sig: ne };
  }
  function b(k, _, $ = {}) {
    const { seed: L, k2sig: R } = z(k, _, $);
    return my(r.outputLen, a.BYTES, o)(L, R).toBytes($.format);
  }
  function S(k, _, $, L = {}) {
    const { lowS: R, prehash: C, format: D } = ro(L, w);
    if ($ = W($, void 0, "publicKey"), _ = N(_, C), !cs(k)) {
      const U = k instanceof E ? ", use sig.toBytes()" : "";
      throw new Error("verify expects Uint8Array signature" + U);
    }
    I(k, D);
    try {
      const U = E.fromBytes(k, D), Z = e.fromBytes($);
      if (R && U.hasHighS())
        return !1;
      const { r: M, s: te } = U, ne = P(_), re = a.inv(te), ee = a.create(ne * re), me = a.create(M * re), lt = e.BASE.multiplyUnsafe(ee).add(Z.multiplyUnsafe(me));
      return lt.is0() ? !1 : a.create(lt.x) === M;
    } catch {
      return !1;
    }
  }
  function A(k, _, $ = {}) {
    const { prehash: L } = ro($, w);
    return _ = N(_, L), E.fromBytes(k, "recovered").recoverPublicKey(_).toBytes();
  }
  return Object.freeze({
    keygen: u,
    getPublicKey: d,
    getSharedSecret: h,
    utils: p,
    lengths: g,
    Point: e,
    sign: b,
    verify: S,
    recoverPublicKey: A,
    Signature: E,
    hash: r
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const $i = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, Yy = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, Jy = /* @__PURE__ */ BigInt(0), Oo = /* @__PURE__ */ BigInt(2);
function Qy(e) {
  const t = $i.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, u = l * l * e % t, d = ie(u, n, t) * u % t, h = ie(d, n, t) * u % t, p = ie(h, Oo, t) * l % t, g = ie(p, i, t) * p % t, w = ie(g, o, t) * g % t, f = ie(w, a, t) * w % t, y = ie(f, c, t) * f % t, v = ie(y, a, t) * w % t, x = ie(v, n, t) * u % t, I = ie(x, s, t) * g % t, E = ie(I, r, t) * l % t, T = ie(E, Oo, t);
  if (!si.eql(si.sqr(T), e))
    throw new Error("Cannot find square root");
  return T;
}
const si = Bi($i.p, { sqrt: Qy }), sn = /* @__PURE__ */ Wy($i, {
  Fp: si,
  endo: Yy
}), Ie = /* @__PURE__ */ Xy(sn, oe), qa = {};
function ai(e, ...t) {
  let n = qa[e];
  if (n === void 0) {
    const r = oe(wy(e));
    n = Le(r, r), qa[e] = n;
  }
  return oe(Le(n, ...t));
}
const Oi = (e) => e.toBytes(!0).slice(1), Ri = (e) => e % Oo === Jy;
function eu(e) {
  const { Fn: t, BASE: n } = sn, r = t.fromBytes(e), i = n.multiply(r);
  return { scalar: Ri(i.y) ? r : t.neg(r), bytes: Oi(i) };
}
function tu(e) {
  const t = si;
  if (!t.isValidNot0(e))
    throw new Error("invalid x: Fail if x ≥ p");
  const n = t.create(e * e), r = t.create(n * e + BigInt(7));
  let i = t.sqrt(r);
  Ri(i) || (i = t.neg(i));
  const o = sn.fromAffine({ x: e, y: i });
  return o.assertValidity(), o;
}
const xn = xr;
function nu(...e) {
  return sn.Fn.create(xn(ai("BIP0340/challenge", ...e)));
}
function Ga(e) {
  return eu(e).bytes;
}
function ew(e, t, n = ss(32)) {
  const { Fn: r, BASE: i } = sn, o = W(e, void 0, "message"), { bytes: s, scalar: a } = eu(t), c = W(n, 32, "auxRand"), l = r.toBytes(a ^ xn(ai("BIP0340/aux", c))), u = ai("BIP0340/nonce", l, s, o), d = r.create(xn(u));
  if (d === 0n)
    throw new Error("sign failed: k is zero");
  const h = i.multiply(d), p = Ri(h.y) ? d : r.neg(d), g = Oi(h), w = nu(g, s, o), f = new Uint8Array(64);
  if (f.set(g, 0), f.set(r.toBytes(r.create(p + w * a)), 32), !ru(f, o, s))
    throw new Error("sign: Invalid signature produced");
  return f;
}
function ru(e, t, n) {
  const { Fp: r, Fn: i, BASE: o } = sn, s = W(e, 64, "signature"), a = W(t, void 0, "message"), c = W(n, 32, "publicKey");
  try {
    const l = tu(xn(c)), u = xn(s.subarray(0, 32));
    if (!r.isValidNot0(u))
      return !1;
    const d = xn(s.subarray(32, 64));
    if (!i.isValidNot0(d))
      return !1;
    const h = nu(i.toBytes(u), Oi(l), a), p = o.multiplyUnsafe(d).add(l.multiplyUnsafe(i.neg(h))), { x: g, y: w } = p.toAffine();
    return !(p.is0() || !Ri(w) || g !== u);
  } catch {
    return !1;
  }
}
const qt = /* @__PURE__ */ (() => {
  const n = (r) => (r = r === void 0 ? ss(48) : r, jl(r, $i.n));
  return Object.freeze({
    keygen: ys(n, Ga),
    getPublicKey: Ga,
    sign: ew,
    verify: ru,
    Point: sn,
    utils: Object.freeze({
      randomSecretKey: n,
      taggedHash: ai,
      lift_x: tu,
      pointToBytes: Oi
    }),
    lengths: Object.freeze({
      secretKey: 32,
      publicKey: 32,
      publicKeyHasPrefix: !1,
      signature: 64,
      seed: 48
    })
  });
})(), tw = /* @__PURE__ */ Uint8Array.from([
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
]), iu = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), nw = iu.map((e) => (9 * e + 5) % 16), ou = /* @__PURE__ */ (() => {
  const n = [[iu], [nw]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => tw[o]));
  return n;
})(), su = ou[0], au = ou[1], cu = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), rw = /* @__PURE__ */ su.map((e, t) => e.map((n) => cu[t][n])), iw = /* @__PURE__ */ au.map((e, t) => e.map((n) => cu[t][n])), ow = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), sw = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function Wa(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const Lr = /* @__PURE__ */ new Uint32Array(16);
class aw extends as {
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
    for (let p = 0; p < 16; p++, n += 4)
      Lr[p] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, u = l, d = this.h4 | 0, h = d;
    for (let p = 0; p < 5; p++) {
      const g = 4 - p, w = ow[p], f = sw[p], y = su[p], v = au[p], x = rw[p], I = iw[p];
      for (let E = 0; E < 16; E++) {
        const T = Pr(r + Wa(p, o, a, l) + Lr[y[E]] + w, x[E]) + d | 0;
        r = d, d = l, l = Pr(a, 10) | 0, a = o, o = T;
      }
      for (let E = 0; E < 16; E++) {
        const T = Pr(i + Wa(g, s, c, u) + Lr[v[E]] + f, I[E]) + h | 0;
        i = h, h = u, u = Pr(c, 10) | 0, c = s, s = T;
      }
    }
    this.set(this.h1 + a + u | 0, this.h2 + l + h | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    st(Lr);
  }
  destroy() {
    this.destroyed = !0, st(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const lu = /* @__PURE__ */ Ti(() => new aw()), cw = BigInt(0), Hn = BigInt(1), lw = BigInt(2), uw = BigInt(7), dw = BigInt(256), fw = BigInt(113), uu = [], du = [], fu = [];
for (let e = 0, t = Hn, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], uu.push(2 * (5 * r + n)), du.push((e + 1) * (e + 2) / 2 % 64);
  let i = cw;
  for (let o = 0; o < 7; o++)
    t = (t << Hn ^ (t >> uw) * fw) % dw, t & lw && (i ^= Hn << (Hn << BigInt(o)) - Hn);
  fu.push(i);
}
const pu = Pl(fu, !0), pw = pu[0], hw = pu[1], Za = (e, t, n) => n > 32 ? Qh(e, t, n) : Yh(e, t, n), Xa = (e, t, n) => n > 32 ? ey(e, t, n) : Jh(e, t, n);
function yw(e, t = 24) {
  if (it(t, "rounds"), t < 1 || t > 24)
    throw new Error('"rounds" expected integer 1..24');
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], u = n[c + 1], d = Za(l, u, 1) ^ n[a], h = Xa(l, u, 1) ^ n[a + 1];
      for (let p = 0; p < 50; p += 10)
        e[s + p] ^= d, e[s + p + 1] ^= h;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = du[s], c = Za(i, o, a), l = Xa(i, o, a), u = uu[s];
      i = e[u], o = e[u + 1], e[u] = c, e[u + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      const a = e[s], c = e[s + 1], l = e[s + 2], u = e[s + 3];
      e[s] ^= ~e[s + 2] & e[s + 4], e[s + 1] ^= ~e[s + 3] & e[s + 5], e[s + 2] ^= ~e[s + 4] & e[s + 6], e[s + 3] ^= ~e[s + 5] & e[s + 7], e[s + 4] ^= ~e[s + 6] & e[s + 8], e[s + 5] ^= ~e[s + 7] & e[s + 9], e[s + 6] ^= ~e[s + 8] & a, e[s + 7] ^= ~e[s + 9] & c, e[s + 8] ^= ~a & l, e[s + 9] ^= ~c & u;
    }
    e[0] ^= pw[r], e[1] ^= hw[r];
  }
  st(n);
}
let ww = class hu {
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
    if (this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.canXOF = i, this.rounds = o, it(r, "outputLen"), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = Fh(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    Ta(this.state32), yw(this.state32, this.rounds), Ta(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Sn(this), ot(t);
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
    Sn(this, !1), ot(t), this.finish();
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
    return it(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (rs(t, this), this.finished)
      throw new Error("digest() was already called");
    this.writeInto(t.subarray(0, this.outputLen)), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.outputLen);
    return this.digestInto(t), t;
  }
  destroy() {
    this.destroyed = !0, st(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t ||= new hu(n, r, i, s, o), t.blockLen = n, t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.canXOF = this.canXOF, t.destroyed = this.destroyed, t;
  }
};
const gw = (e, t, n, r = {}) => Ti(() => new ww(t, e, n), r), yu = /* @__PURE__ */ gw(1, 136, 32), mw = 1024 * 1024, wu = Object.getPrototypeOf(Uint8Array.prototype), bw = Object.getOwnPropertyDescriptor(wu, Symbol.toStringTag).get, vw = Object.getOwnPropertyDescriptor(wu, "byteLength").get, xw = Uint8Array.prototype.set;
function gu(e) {
  if (!Number.isSafeInteger(e) || e < 0) throw new Error("exact message byte length is invalid");
  if (e > mw) throw new Error("exact message exceeds the 1 MiB decoded limit");
}
function mu(e) {
  const t = e.startsWith("0x") ? 2 : 0;
  gu(Math.ceil((e.length - t) / 2));
}
function ws(e) {
  if (typeof e == "string")
    return mu(e), An(e.startsWith("0x") ? e.slice(2) : e);
  if (bw.call(e) !== "Uint8Array") throw new TypeError("exact message must be hex or Uint8Array");
  const t = vw.call(e);
  gu(t);
  const n = new Uint8Array(t);
  return xw.call(n, e), n;
}
const kw = "hpke-x25519-hkdfsha256-aes256gcm-v1", Ew = "avault:blind-box:v1", Sw = "avault:blind-box:aad:v1", ci = "machine-aesgcm-v1", Li = 1, Ci = "ecdsa-secp256k1-recoverable", lr = "ed25519", gs = "ecdsa-secp256k1-der", ms = "schnorr-secp256k1-bip340", ur = "schnorr-secp256k1-bip341-keypath", le = 32, li = 12, Ni = 32, Aw = "avault:protected-vmk:kek-passkey:v1", _w = /^[A-Za-z_][A-Za-z0-9_]*$/, Iw = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[a-z][a-z0-9_]{0,63})?$/, Pw = "avault:protected-root-meta:v1", Tw = new TextEncoder(), Bw = new TextDecoder();
function bs(e) {
  switch (e) {
    case lr:
    case Ci:
    case gs:
    case ms:
    case ur:
      return e;
    default:
      throw new Error("unsupported signing scheme");
  }
}
function Rn(e) {
  if (e === void 0 || e === "secp256k1") return "secp256k1";
  if (e === "ed25519") return e;
  throw new Error("unsupported signing curve");
}
function vs(e) {
  if (bs(e), e === lr) throw new Error("Ed25519 requires exact-message signing, not a digest");
}
function ue(e, t = "bytes") {
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
function $e(e) {
  const t = ue(e), n = new ArrayBuffer(t.byteLength);
  return new Uint8Array(n).set(t), t.fill(0), n;
}
function Se(e) {
  return Tw.encode(e);
}
function ui(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(ui).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${ui(t[n])}`).join(",")}}`;
}
function nn(e) {
  const t = new Uint8Array(e);
  return crypto.getRandomValues(t), t;
}
function Q(e) {
  const t = ue(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return t.fill(0), btoa(n);
}
function pe(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = t.padEnd(Math.ceil(t.length / 4) * 4, "=");
  return Uint8Array.from(atob(n), (r) => r.charCodeAt(0));
}
function Ce(e) {
  return An(e.startsWith("0x") ? e.slice(2) : e);
}
function Ya(e) {
  return /^(0x)?[0-9a-fA-F]+$/.test(e) && (e.startsWith("0x"), e.length % 2 === 0) ? Ce(e) : pe(e);
}
function $w(e) {
  return Y(ue(e));
}
function bu(e, t = "bytes") {
  return ue(e, t);
}
function ge(e, t, n) {
  if (e.length !== t)
    throw new Error(`${n} must be ${t} bytes`);
}
function je(e) {
  e && new Uint8Array(e).fill(0);
}
async function di(e, t, n, r) {
  let i, o, s, a;
  try {
    i = $e(e), o = $e(t), s = $e(n), a = r ? $e(r) : void 0;
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
    je(i), je(o), je(s), je(a);
  }
}
async function zi(e, t, n, r) {
  let i, o, s, a;
  try {
    i = $e(e), o = $e(t), s = $e(n), a = r ? $e(r) : void 0;
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
    je(i), je(o), je(s), je(a);
  }
}
async function Ow(e, t, n, r) {
  let i, o, s;
  try {
    i = $e(e), o = $e(t), s = $e(n);
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
    je(i), je(o), je(s);
  }
}
async function vu(e, t) {
  const n = ue(e, "passkey PRF output");
  try {
    const r = ue(t, "passkey PRF salt");
    try {
      if (n.length === 0)
        throw new Error("passkey PRF output is empty");
      return ge(r, Ni, "passkey PRF salt"), await Ow(n, r, Se(Aw), le);
    } finally {
      r.fill(0);
    }
  } finally {
    n.fill(0);
  }
}
function Rw() {
  return nn(le);
}
function Lw() {
  return nn(Ni);
}
function Cw(e) {
  const t = ue(e, "passkey PRF salt");
  try {
    return ge(t, Ni, "passkey PRF salt"), { prf: { eval: { first: $e(t) } } };
  } finally {
    t.fill(0);
  }
}
function at(e) {
  const t = typeof e == "string" ? JSON.parse(e) : e;
  if (t?.v !== Li || !Array.isArray(t.copies))
    throw new Error("invalid protected wrap_meta");
  return t;
}
async function Nw(e, t) {
  const n = ue(e, "VMK"), r = nn(li);
  let i, o;
  try {
    ge(n, le, "VMK"), o = ue(t.prfSalt, "passkey PRF salt"), ge(o, Ni, "passkey PRF salt"), i = await vu(t.prfOutput, o);
    const s = await di(i, r, n);
    return {
      kind: "passkey",
      kdf: "webauthn-prf-hkdf-sha256",
      prf_salt: Q(o),
      nonce: Q(r),
      wrapped: Q(s),
      ...t.credentialId ? { credential_id: t.credentialId } : {}
    };
  } finally {
    i?.fill(0), o?.fill(0), n.fill(0);
  }
}
async function zw(e, t) {
  const n = ue(e, "VMK");
  try {
    if (ge(n, le, "VMK"), t.length === 0)
      throw new Error("at least one protected unlock factor is required");
    const r = [];
    for (const i of t)
      r.push(await Nw(n, i));
    return JSON.stringify({ v: Li, copies: r });
  } finally {
    n.fill(0);
  }
}
async function Dw(e, t) {
  const n = pe(e.prf_salt);
  if (t.prfSalt && Q(t.prfSalt) !== e.prf_salt)
    throw n.fill(0), new Error("passkey PRF salt does not match copy");
  const r = await vu(t.prfOutput, n);
  try {
    return await zi(r, pe(e.nonce), pe(e.wrapped));
  } finally {
    r.fill(0), n.fill(0);
  }
}
async function Uw(e, t) {
  const n = at(e);
  for (const r of n.copies)
    try {
      if (r.kind === "passkey") {
        const i = await Dw(r, t);
        try {
          return ge(i, le, "VMK"), i;
        } catch (o) {
          throw i.fill(0), o;
        }
      }
    } catch {
    }
  throw new Error("no protected VMK copy could be unwrapped");
}
function xs(e) {
  return at(e).copies.filter((t) => t.kind === "passkey").map((t) => ({
    prfSalt: pe(t.prf_salt),
    ...t.credential_id ? { credentialId: t.credential_id } : {}
  }));
}
function Mw(e, t) {
  const n = at(e);
  return JSON.stringify({
    ...n,
    rp_id: t.rpId,
    vault_user_handle: t.vaultUserHandle
  });
}
function xu(e) {
  const t = at(e);
  return delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, JSON.stringify(t);
}
function ks(e) {
  if (!e.kind) {
    if (e.curve !== void 0) throw new Error("protected record curve requires keypair metadata");
    return;
  }
  const t = Rn(e.curve);
  if (e.kind === "static") {
    if (e.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (e.kind === "keypair") {
    if (typeof e.publicKey != "string" || e.publicKey.length === 0)
      throw new Error("keypair protected record requires a public key");
    return t === "ed25519" && Ts(e.publicKey), {
      kind: "keypair",
      public_key: e.publicKey.toLowerCase(),
      ...t === "ed25519" ? { curve: t } : {}
    };
  }
  throw new Error("unsupported protected record kind");
}
function Hw(e) {
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
    return ks({ kind: "keypair", publicKey: t.public_key, curve: t.curve });
  throw new Error("protected record metadata kind is invalid");
}
function Es(e, t) {
  return {
    name: e,
    ...t ? { kind: t.kind } : {},
    ...t?.public_key ? { publicKey: t.public_key } : {},
    ...t?.curve ? { curve: t.curve } : {}
  };
}
function Ss(e) {
  return Se(
    ui({
      domain: "avault:protected-record:aad:v2",
      name: e.kind === "keypair" ? Iu(e.name) : Tn(e.name),
      scheme: e.scheme ?? ci,
      version: e.version ?? Li,
      record: ks(e) ?? null
    })
  );
}
async function ku(e, t, n) {
  const r = ue(t, "VMK"), i = nn(le);
  let o;
  try {
    ge(r, le, "VMK"), o = ue(e, "value");
    const s = Ss(n), a = nn(li), c = await di(i, a, o, s), l = nn(li), u = await di(r, l, i, s);
    return {
      ciphertext: Q(c),
      nonce: Q(a),
      dek_nonce: Q(l),
      wrapped_dek: Q(u)
    };
  } finally {
    i.fill(0), r.fill(0), o?.fill(0);
  }
}
async function Eu(e, t, n) {
  const r = ue(t, "VMK");
  try {
    ge(r, le, "VMK");
    const i = await zi(
      r,
      pe(e.dek_nonce),
      pe(e.wrapped_dek),
      Ss(n)
    );
    try {
      return ge(i, le, "DEK"), i;
    } catch (o) {
      throw i.fill(0), o;
    }
  } finally {
    r.fill(0);
  }
}
async function As(e, t, n) {
  const r = await Eu(e, t, n);
  try {
    return await zi(
      r,
      pe(e.nonce),
      pe(e.ciphertext),
      Ss(n)
    );
  } finally {
    r.fill(0);
  }
}
function Su(e, t, n) {
  const r = at(t);
  if ("dek_nonce" in r || "wrapped_dek" in r)
    throw new Error("vmk wrap_meta must not already carry a wrapped DEK");
  if ("scheme" in r && r.scheme !== ci)
    throw new Error("protected wrap_meta has unsupported scheme");
  const i = n ? ks(n) : void 0;
  return {
    ciphertext: e.ciphertext,
    nonce: e.nonce,
    wrap_meta: JSON.stringify({
      ...r,
      scheme: ci,
      ...i ? { record_meta: i } : {},
      dek_nonce: e.dek_nonce,
      wrapped_dek: e.wrapped_dek
    })
  };
}
function Pn(e) {
  const t = at(e.wrap_meta), { dek_nonce: n, wrapped_dek: r, record_meta: i, ...o } = t;
  if (typeof n != "string" || typeof r != "string")
    throw new Error("protected record wrap_meta is missing the wrapped DEK");
  const s = Hw(i);
  return {
    sealed: { ciphertext: e.ciphertext, nonce: e.nonce, dek_nonce: n, wrapped_dek: r },
    vmkWrapMeta: JSON.stringify(o),
    ...s ? { recordMetadata: s } : {}
  };
}
function Au(e) {
  const t = { ...at(e) };
  return delete t.root_meta, delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, delete t.scheme, Se(ui({ domain: Pw, vault: t }));
}
async function Fw(e, t, n) {
  const r = nn(li), i = Se(JSON.stringify(t));
  try {
    const o = await di(e, r, i, Au(n));
    return { v: 1, nonce: Q(r), ciphertext: Q(o) };
  } finally {
    i.fill(0);
  }
}
async function _s(e, t) {
  const n = at(e).root_meta;
  if (!n) return null;
  if (n.v !== 1 || typeof n.nonce != "string" || typeof n.ciphertext != "string")
    throw new Error("invalid protected root metadata");
  const r = await zi(t, pe(n.nonce), pe(n.ciphertext), Au(e));
  try {
    const i = JSON.parse(Bw.decode(r));
    if (typeof i != "object" || i === null) throw new Error("invalid protected root metadata");
    return i;
  } finally {
    r.fill(0);
  }
}
function Kw(e, t) {
  const n = at(e);
  return JSON.stringify({ ...n, root_meta: t });
}
function jw() {
  return new $h({
    kem: new Hh(),
    kdf: new Oh(),
    aead: new ph()
  });
}
function Is(e) {
  return pe(typeof e == "string" ? e : e.public_key);
}
async function _u(e) {
  const t = await crypto.subtle.digest("SHA-256", $e(e));
  return Y(new Uint8Array(t));
}
async function Vw(e) {
  return _u(Is(e));
}
function qw(e) {
  if (!e.fingerprint)
    throw new Error("protected DEK release requires a pinned avault public key fingerprint");
  return e;
}
async function Gw(e) {
  const t = qw(e), n = Is(t);
  if (ge(n, le, "avault public key"), await _u(n) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  return t;
}
async function Ww(e, t, n) {
  const r = jw(), i = Is(t);
  if (ge(i, le, "avault public key"), typeof t != "string" && t.fingerprint && await Vw(t) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  const o = await r.kem.deserializePublicKey(i), s = typeof e == "string" ? Se(e) : ue(e, "plaintext");
  try {
    const a = await r.seal(
      { recipientPublicKey: o, info: Se(Ew) },
      s,
      ng(n)
    );
    return {
      scheme: kw,
      enc: Q(new Uint8Array(a.enc)),
      ct: Q(new Uint8Array(a.ct))
    };
  } finally {
    s.fill(0);
  }
}
function Iu(e) {
  if (typeof e != "string" || !_w.test(e))
    throw new Error("vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$");
  return e;
}
function Tn(e) {
  if (typeof e != "string" || !Iw.test(e))
    throw new Error("vault record name must match ^[A-Za-z_][A-Za-z0-9_]*(\\.[a-z][a-z0-9_]{0,63})?$");
  return e;
}
function Ja(e, t) {
  if (typeof e != "string" || e.trim() === "")
    throw new Error(`${t} is required`);
  return e;
}
function Ps(e) {
  const t = typeof e == "string" ? Ce(e) : ue(e, "digest");
  return ge(t, le, "digest"), t;
}
function Zw(e) {
  const t = typeof e == "string" ? Ce(e) : ue(e, "private key");
  try {
    if (ge(t, le, "private key"), !Ie.utils.isValidSecretKey(t))
      throw new Error("invalid secp256k1 private key");
    return t;
  } catch (n) {
    throw t.fill(0), n;
  }
}
function fi(e, t) {
  return typeof e == "string" ? Ce(e) : ue(e, t);
}
function pi(e) {
  const t = fi(e, "operation hash");
  return ge(t, le, "operation hash"), t;
}
function hi(e, t) {
  if (typeof e == "number" && (!Number.isSafeInteger(e) || e < 0))
    throw new Error(`${t} out of bounds`);
  const n = typeof e == "bigint" ? e : BigInt(e);
  if (n < 0n || n > 0xffffffffffffffffn)
    throw new Error(`${t} out of bounds`);
  return n;
}
function Pu(e, t) {
  const n = hi(e, t), r = new Uint8Array(8);
  return new DataView(r.buffer).setBigUint64(0, n, !1), r;
}
function Xw(e) {
  return Pu(e, "approval expiry");
}
function Qa(e) {
  const t = ue(e.nonce, "approval nonce");
  if (t.length < 16 || t.length > 128)
    throw new Error("approval nonce must be 16..128 bytes");
  return { approvalNonce: t, approvalExpiresAtUnix: hi(e.expiresAtUnix, "approval expiry") };
}
function Re(e, t) {
  const n = ue(t);
  if (n.length > 4294967295)
    throw new Error("blind-box AAD field is too large");
  e.push(n.length >>> 24 & 255, n.length >>> 16 & 255, n.length >>> 8 & 255, n.length & 255), e.push(...n);
}
function Yw(e) {
  switch (e.purpose) {
    case "seal":
      return Jw(e.name);
    case "agent-deliver": {
      const t = Qa({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-deliver",
        name: Tn(e.name),
        grantId: Ja(e.grantId, "grant id"),
        ttlSecs: hi(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: pi(e.operationHash)
      };
    }
    case "agent-sign": {
      vs(e.signScheme);
      const t = Qa({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-sign",
        name: Iu(e.name),
        grantId: Ja(e.grantId, "grant id"),
        signScheme: e.signScheme,
        digest: Ps(e.digest),
        ttlSecs: hi(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: pi(e.operationHash)
      };
    }
    default:
      throw new Error("unsupported blind-box context purpose");
  }
}
function Jw(e) {
  return { purpose: "seal", name: Tn(e) };
}
async function Qw(e) {
  const t = pi(e.operationHash), n = await tg(e.name, e.ttlSecs);
  if (Y(t) !== Y(n))
    throw new Error("operation hash does not match release operation fields");
}
async function eg(e) {
  const t = [];
  for (const n of e)
    Re(t, typeof n == "string" ? Se(n) : ue(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(t)));
}
async function tg(e, t) {
  return eg(["agent-deliver", Tn(e), Pu(t, "ttl seconds")]);
}
function ng(e) {
  const t = Yw(e), n = [...Se(Sw)];
  return Re(n, Se(t.purpose)), Re(n, Se(t.name)), Re(n, Se(ci)), Re(n, new Uint8Array([Li])), Re(n, t.purpose === "agent-deliver" || t.purpose === "agent-sign" ? Se(t.grantId) : new Uint8Array()), Re(n, new Uint8Array()), Re(n, t.purpose === "agent-sign" ? Se(t.signScheme) : new Uint8Array()), Re(n, t.purpose === "agent-sign" ? Ps(t.digest) : new Uint8Array()), Re(n, t.purpose === "seal" ? new Uint8Array() : ue(t.approvalNonce, "approval nonce")), Re(n, t.purpose === "seal" ? new Uint8Array() : Xw(t.approvalExpiresAtUnix)), Re(n, t.purpose === "seal" ? new Uint8Array() : pi(t.operationHash)), new Uint8Array(n);
}
async function rg(e, t, n, r, i) {
  if (r.kind !== void 0 && r.kind !== "static")
    throw new Error("protected DEK release requires a static protected record");
  if (Tn(r.name) !== Tn(i.name))
    throw new Error("protected DEK release context name does not match record name");
  await Qw(i);
  const o = await Gw(n), s = await Eu(e, t, r);
  try {
    return await Ww(s, o, i);
  } finally {
    s.fill(0);
  }
}
function ig(e = "secp256k1") {
  if (Rn(e) === "ed25519") {
    const n = Nt.utils.randomSecretKey();
    try {
      return { privateKey: n, publicKey: Y(Nt.getPublicKey(n)) };
    } catch (r) {
      throw n.fill(0), r;
    }
  }
  const t = Ie.utils.randomSecretKey();
  return { privateKey: t, publicKey: Y(Ie.getPublicKey(t, !0)) };
}
function ec(e) {
  return Ce(e.toString(16).padStart(le * 2, "0"));
}
function og(e, t, n, r = {}) {
  if (vs(n), !r || typeof r != "object" || Array.isArray(r) || Object.keys(r).some((s) => s !== "schnorrAuxRand"))
    throw new Error("unsupported signing option; script trees are not supported");
  const i = Zw(e);
  let o;
  try {
    const s = Ps(t);
    if (n === Ci) {
      const a = Ie.sign(s, i, { prehash: !1, lowS: !0, format: "recovered" });
      return {
        scheme: n,
        signature: Y(a.slice(1)),
        recovery_id: a[0] ?? null
      };
    }
    if (n === gs)
      return {
        scheme: n,
        signature: Y(Ie.sign(s, i, { prehash: !1, lowS: !0, format: "der" })),
        recovery_id: null
      };
    if (n === ms) {
      o = r.schnorrAuxRand !== void 0 ? fi(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(le)), ge(o, le, "schnorr aux randomness");
      const a = qt.sign(s, i, o);
      if (!qt.verify(a, s, qt.getPublicKey(i))) throw new Error("signing self-verification failed");
      return { scheme: n, signature: Y(a), recovery_id: null };
    }
    if (n === ur) {
      const a = Ie.getPublicKey(i, !0), c = Bs(a.slice(1));
      let l = BigInt(`0x${Y(i)}`);
      a[0] === 3 && (l = Yn - l);
      const u = BigInt(`0x${Y(qt.utils.taggedHash("TapTweak", a.slice(1)))}`);
      if (u >= Yn) throw new Error("invalid taproot tweak");
      let d = (l + u) % Yn;
      if (d === 0n) throw new Error("invalid taproot signing key");
      let h = ec(d);
      try {
        c.toBytes(!0)[0] === 3 && (d = Yn - d, h.fill(0), h = ec(d)), o = r.schnorrAuxRand !== void 0 ? fi(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(le)), ge(o, le, "schnorr aux randomness");
        const p = qt.sign(s, h, o);
        if (!qt.verify(p, s, c.toBytes(!0).slice(1))) throw new Error("Taproot signing self-verification failed");
        return { scheme: n, signature: Y(p), recovery_id: null };
      } finally {
        h.fill(0);
      }
    }
    throw new Error("unsupported signing scheme");
  } finally {
    o?.fill(0), i.fill(0);
  }
}
async function sg(e, t, n, r, i, o = {}) {
  if (n.kind !== "keypair" || !n.publicKey)
    throw new Error("protected signing requires a keypair protected record");
  if (vs(i), Rn(n.curve) !== "secp256k1") throw new Error("digest signing requires a secp256k1 protected record");
  const s = await As(e, t, n);
  try {
    if (Y(Ie.getPublicKey(s, !0)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return og(s, r, i, o);
  } finally {
    s.fill(0);
  }
}
function ag(e, t) {
  const n = ws(t), r = fi(e, "Ed25519 seed");
  try {
    ge(r, le, "Ed25519 seed");
    const i = Nt.sign(n, r);
    if (!Nt.verify(i, n, Nt.getPublicKey(r), { zip215: !1 }))
      throw new Error("Ed25519 signing self-verification failed");
    return { scheme: lr, signature: Y(i), recovery_id: null };
  } finally {
    r.fill(0);
  }
}
function Ts(e) {
  if (!/^[0-9a-fA-F]{64}$/.test(e)) throw new Error("expected a 32-byte Ed25519 public key");
  const t = Ce(e);
  if (!Nt.utils.isValidPublicKey(t, !1)) throw new Error("invalid Ed25519 public key");
  return t;
}
async function cg(e, t, n, r) {
  if (n.kind !== "keypair" || n.curve !== "ed25519" || !n.publicKey)
    throw new Error("exact-message signing requires an Ed25519 keypair protected record");
  const i = ws(r);
  if (i.length === 0) throw new Error("protected Ed25519 message must not be empty");
  Ts(n.publicKey);
  const o = await As(e, t, n);
  try {
    if (ge(o, le, "Ed25519 seed"), Y(Nt.getPublicKey(o)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return ag(o, i);
  } finally {
    o.fill(0);
  }
}
function lg(e) {
  return $u(Ts(e));
}
function ug(e) {
  const t = e.rootMetadata?.daemon?.verificationKeys?.find(
    (n) => n.alg === "ed25519" && n.keyId === e.keyId
  );
  if (!t)
    throw new Error("daemon verification key is not pinned");
  return Nt.verify(Ya(e.signature), Se(e.message), Ya(t.publicKey));
}
function Tu(e) {
  const t = Ce(e.trim());
  if (t.length === 33 && (t[0] === 2 || t[0] === 3) && Ie.utils.isValidPublicKey(t))
    return t;
  if (t.length === 65 && t[0] === 4 && Ie.utils.isValidPublicKey(t))
    return Ie.Point.fromBytes(t).toBytes(!0);
  throw new Error("expected a valid secp256k1 public key");
}
const dg = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", fg = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", pg = 1, hg = 734539939, Yn = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
function yg(e) {
  return lu(oe(e));
}
function Bu(e) {
  const t = oe(oe(e)).slice(0, 4), n = new Uint8Array(e.length + t.length);
  return n.set(e), n.set(t, e.length), $u(n);
}
function $u(e) {
  let t = BigInt(`0x${Y(e)}`), n = "";
  for (; t > 0n; ) {
    const i = Number(t % 58n);
    t /= 58n, n = dg[i] + n;
  }
  let r = 0;
  for (const i of e) {
    if (i !== 0) break;
    r += 1;
  }
  return "1".repeat(r) + n;
}
function wg(e) {
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
function gg(e) {
  return [...e].map((t) => t.charCodeAt(0) >>> 5).concat([0], [...e].map((t) => t.charCodeAt(0) & 31));
}
function mg(e, t, n) {
  const r = gg(e).concat(t), i = wg(r.concat([0, 0, 0, 0, 0, 0])) ^ n;
  return [0, 1, 2, 3, 4, 5].map((o) => i >>> 5 * (5 - o) & 31);
}
function bg(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >>> i & s);
  return i && o.push(r << n - i & s), o;
}
function Ro(e, t, n) {
  const r = t === 0 ? pg : hg, i = [t].concat(bg(n, 8, 5));
  return `${e}1${i.concat(mg(e, i, r)).map((o) => fg[o]).join("")}`;
}
function Ou(e) {
  const t = e.toLowerCase(), n = Y(yu(Se(t)));
  return [...t].map((r, i) => /[a-f]/.test(r) && Number.parseInt(n[i], 16) >= 8 ? r.toUpperCase() : r).join("");
}
function Bs(e) {
  const t = BigInt(`0x${Y(qt.utils.taggedHash("TapTweak", e))}`);
  if (t >= Yn) throw new Error("invalid taproot tweak");
  const n = Ie.Point.fromBytes(new Uint8Array([2, ...e])), r = t === 0n ? n : n.add(Ie.Point.BASE.multiply(t));
  if (r.equals(Ie.Point.ZERO)) throw new Error("invalid taproot output key");
  return r;
}
function vg(e) {
  return Bs(Tu(e).slice(1)).toBytes(!0).slice(1);
}
function $s(e) {
  const t = Tu(e), n = Ie.Point.fromBytes(t), r = n.toBytes(!1).slice(1), i = yu(r), o = yg(t), s = new Uint8Array(21);
  s[0] = 0, s.set(o, 1);
  const a = n.toBytes(!1).slice(1, 33), c = Bs(a);
  return {
    eth: `0x${Ou(Y(i.slice(-20)))}`,
    btc_legacy: Bu(s),
    btc_segwit: Ro("bc", 0, o),
    btc_taproot: Ro("bc", 1, c.toBytes(!1).slice(1, 33))
  };
}
const Os = {
  windowSeconds: 600,
  strictApprovals: !1,
  parentValueSealAllowed: !0
};
let Lo = { ...Os };
function xg(e) {
  return e === 300 || e === 600 || e === 1800 ? e : Os.windowSeconds;
}
function kg(e, t) {
  if (e === 300 || e === 600 || e === 1800) return e;
  throw new m("invalid_payload", `${t}.windowSeconds must be 300, 600, or 1800`);
}
function Rs(e, t = "policy") {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be an object`);
  const n = e;
  if (typeof n.strictApprovals != "boolean") throw new m("invalid_payload", `${t}.strictApprovals must be a boolean`);
  if (typeof n.parentValueSealAllowed != "boolean")
    throw new m("invalid_payload", `${t}.parentValueSealAllowed must be a boolean`);
  return {
    windowSeconds: kg(n.windowSeconds, t),
    strictApprovals: n.strictApprovals,
    parentValueSealAllowed: n.parentValueSealAllowed
  };
}
function Eg(e) {
  if (typeof e != "object" || e === null) return { ...Os };
  const t = e;
  return {
    windowSeconds: xg(t.windowSeconds),
    strictApprovals: t.strictApprovals === !0,
    parentValueSealAllowed: t.parentValueSealAllowed !== !1
  };
}
function Ls(e) {
  return Lo = Eg(e), Lo;
}
function Ae() {
  return { ...Lo };
}
const Sg = "avibe-vault-lock:v1:", V = {
  vmk: null,
  wrapMeta: null,
  freshSetup: !1,
  scopeId: null
};
let mt = null, or = null, dr = null, Co = null, Cs = 0;
const yi = /* @__PURE__ */ new Set();
let Ru = null;
function Ag(e) {
  Ru = e;
}
function tc() {
  return V.vmk ? "unlocked" : V.wrapMeta ? "locked" : "needs-setup";
}
function Lu(e) {
  Ru?.({
    state: tc(),
    ...mt !== null && tc() === "unlocked" ? { expiresAt: mt } : {},
    reason: e
  });
}
function Ns() {
  or !== null && (clearTimeout(or), or = null);
}
function wi() {
  dr?.close(), dr = null, Co = null;
}
function Cu(e) {
  const t = new TextEncoder().encode(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return btoa(n).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function _g(e) {
  let t = 2166136261;
  for (let n = 0; n < e.length; n += 1)
    t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
  return t.toString(16).padStart(8, "0");
}
function Nu(e) {
  const t = at(e);
  return typeof t.vault_user_handle == "string" && t.vault_user_handle.length > 0 ? Cu(t.vault_user_handle) : `wrap-${_g(e)}`;
}
function Ig(e) {
  return Cu(e);
}
function zu(e) {
  if (typeof window > "u" || typeof BroadcastChannel > "u" || !e) {
    wi();
    return;
  }
  const t = `${Sg}${e}`;
  Co !== t && (wi(), Co = t, dr = new BroadcastChannel(t), dr.onmessage = (n) => {
    const r = n.data;
    r?.type === "lock" && r.scopeId === e && qe({ broadcast: !1, reason: "manual-lock" });
  });
}
function Pg() {
  V.scopeId && (zu(V.scopeId), dr?.postMessage({ type: "lock", scopeId: V.scopeId }));
}
function Du() {
  return new Error("vault-operation-aborted");
}
function Uu() {
  Cs += 1;
  for (const e of yi)
    e.abort(Du());
  yi.clear();
}
function Tg() {
  V.vmk?.fill(0), V.vmk = null, mt = null, Ns(), wi(), V.freshSetup && (V.wrapMeta = null, V.freshSetup = !1, V.scopeId = null);
}
function Bg() {
  return mt !== null && Date.now() >= mt;
}
function Mu() {
  return V.vmk && Bg() ? (qe({ broadcast: !1, reason: "auto-lock" }), !1) : V.vmk !== null;
}
function Hu(e, t = Ae()) {
  if (!V.vmk)
    throw new Error("vault VMK is not loaded");
  const n = t.windowSeconds * 1e3;
  return mt = Date.now() + n, Ns(), or = setTimeout(() => {
    qe({ broadcast: !1, reason: "auto-lock" });
  }, n), Lu(e), mt;
}
function $g() {
  const e = mt;
  if (!V.vmk || e === null) return () => {
  };
  Ns();
  let t = !1;
  return () => {
    if (t || !V.vmk) return;
    t = !0;
    const n = e - Date.now();
    if (n <= 0) {
      qe({ broadcast: !1, reason: "auto-lock" });
      return;
    }
    or = setTimeout(() => {
      qe({ broadcast: !1, reason: "auto-lock" });
    }, n);
  };
}
function Og(e) {
  if (e instanceof m && e.retryable) return !0;
  const t = e instanceof Error ? e.message : String(e);
  return t === "operation-cancelled" || t === "operation-superseded" || t === "passkey-cancelled" || t === "vault-operation-aborted";
}
function Rg(e) {
  if (e.signal.aborted || e.epoch !== Cs || !V.vmk)
    throw Du();
}
async function zs(e, t = {}) {
  if (!Mu() || !V.vmk || !V.wrapMeta)
    throw new Error("vault-locked");
  const n = new Uint8Array(V.vmk), r = V.wrapMeta, i = Cs, o = new AbortController();
  yi.add(o);
  const s = {
    epoch: i,
    signal: o.signal,
    assertCurrent: () => Rg(s)
  };
  let a = null;
  try {
    s.assertCurrent();
    const c = await e(n, r, s);
    return s.assertCurrent(), t.renewOnSuccess !== !1 && Hu("renew"), s.assertCurrent(), t.beforeSuccess && (a = $g(), await t.beforeSuccess(), s.assertCurrent(), a(), a = null), c;
  } catch (c) {
    throw Og(c) || qe({ broadcast: !0, reason: "manual-lock" }), c;
  } finally {
    a?.(), yi.delete(o), n.fill(0);
  }
}
function Lg() {
  return V.vmk !== null && V.freshSetup;
}
function Fu(e) {
  return Uu(), V.vmk?.fill(0), V.vmk = e.vmk, V.wrapMeta = e.wrapMeta, V.freshSetup = e.freshSetup, V.scopeId = e.scopeId, zu(e.scopeId), { state: "unlocked", expiresAt: Hu("unlock", e.policy) };
}
function qe(e = {}) {
  return e.broadcast && Pg(), Uu(), Tg(), Lu(e.reason ?? "manual-lock"), { state: "locked" };
}
function bt(e) {
  return Mu(), V.vmk ? {
    state: "unlocked",
    expiresAt: mt ?? void 0,
    ...V.freshSetup ? { freshSetup: !0 } : {}
  } : (e && (V.wrapMeta = xu(e), V.scopeId = Nu(V.wrapMeta)), V.wrapMeta ? { state: "locked" } : { state: "needs-setup" });
}
function Ds(e) {
  const t = xu(e), n = Nu(t);
  return V.wrapMeta = t, V.scopeId = n, { wrapMeta: t, scopeId: n };
}
function Cg() {
  qe({ broadcast: !1, reason: "unload" }), wi();
}
const Cr = "Avibe Vault", nc = "avibe-vault";
function rn(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function zt(e) {
  const t = new ArrayBuffer(e.byteLength);
  return new Uint8Array(t).set(e), t;
}
function Ku() {
  return zt(crypto.getRandomValues(new Uint8Array(32)));
}
function ju(e) {
  try {
    return pe(e);
  } catch {
    return null;
  }
}
function Di(e, t) {
  if (e instanceof ArrayBuffer || ArrayBuffer.isView(e))
    return zt(bu(e));
  if (typeof e == "string") {
    const n = ju(e);
    return zt(n ?? new TextEncoder().encode(e));
  }
  if (Array.isArray(e))
    return zt(Uint8Array.from(e));
  throw new Error("webauthn bytes are missing");
}
function io(e) {
  if (typeof e != "object" || e === null || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null)
    throw new Error("invalid webauthn creation options object");
  return e;
}
function Ng(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 88 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(e) || /[+/]/.test(e) && /[-_]/.test(e))
    throw new Error("invalid webauthn user.id encoding");
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = ju(t);
  if (!n || n.byteLength === 0 || n.byteLength > 64)
    throw new Error("webauthn user.id must be 1-64 bytes");
  const r = Q(n);
  if (t !== r && t !== r.replace(/=+$/, ""))
    throw new Error("invalid webauthn user.id encoding");
  return zt(n);
}
function zg(e) {
  const t = Di(e);
  if (t.byteLength === 0 || t.byteLength > 64)
    throw new Error("vault user handle must be 1-64 bytes");
  return t;
}
function gi(e) {
  if (Array.isArray(e))
    return e.map((t) => {
      const n = rn(t);
      if (!n || n.type !== "public-key")
        throw new Error("invalid WebAuthn credential descriptor");
      return {
        type: "public-key",
        id: Di(n.id),
        ...Array.isArray(n.transports) ? { transports: n.transports } : {}
      };
    });
}
function rc(e) {
  return !Array.isArray(e) || e.length === 0 ? [
    { type: "public-key", alg: -7 },
    { type: "public-key", alg: -257 }
  ] : e.map((t) => {
    const n = rn(t);
    if (!n || n.type !== "public-key" || typeof n.alg != "number")
      throw new Error("invalid WebAuthn pubKeyCredParams");
    return { type: "public-key", alg: n.alg };
  });
}
function ic(e) {
  return {
    ...rn(e) ?? {},
    residentKey: "required",
    userVerification: "required"
  };
}
function oc(e) {
  if (e === "none" || e === "indirect" || e === "direct" || e === "enterprise") return e;
}
function Dg(e) {
  const t = rn(e) ?? {};
  return { ...t, prf: rn(t.prf) ?? {} };
}
function vt() {
  return window.location.hostname;
}
function Ug(e) {
  const t = e.authzCreationOptions === void 0 ? void 0 : io(e.authzCreationOptions), n = t && Object.prototype.hasOwnProperty.call(t, "webauthn") ? io(t.webauthn) : t, r = typeof t?.challenge_id == "string" ? t.challenge_id : void 0, i = rn(n?.rp), o = typeof i?.id == "string" ? i.id : void 0;
  if (o && o !== e.rpId)
    throw new Error("webauthn rp_id does not match sandbox host");
  if (!n)
    return {
      options: {
        rp: { name: Cr, id: e.rpId },
        user: {
          id: zg(e.vaultUserHandle),
          name: e.vaultUserHandle || nc,
          displayName: e.displayName || Cr
        },
        challenge: Ku(),
        pubKeyCredParams: rc(void 0),
        authenticatorSelection: ic(void 0),
        extensions: { prf: {} }
      }
    };
  const s = io(n.user);
  return {
    options: {
      rp: {
        name: typeof i?.name == "string" ? i.name : Cr,
        id: e.rpId
      },
      user: {
        id: Ng(s.id),
        name: typeof s.name == "string" ? s.name : e.vaultUserHandle || nc,
        displayName: typeof s.displayName == "string" ? s.displayName : e.displayName || Cr
      },
      challenge: Di(n.challenge),
      pubKeyCredParams: rc(n.pubKeyCredParams),
      authenticatorSelection: ic(n.authenticatorSelection),
      extensions: Dg(n.extensions),
      ...gi(n.excludeCredentials) ? { excludeCredentials: gi(n.excludeCredentials) } : {},
      ...typeof n.timeout == "number" ? { timeout: n.timeout } : {},
      ...oc(n.attestation) ? { attestation: oc(n.attestation) } : {}
    },
    ...r ? { challengeId: r } : {}
  };
}
function Mg(e) {
  if (e.length === 0) throw new Error("passkey-not-configured");
  if (e.length > 1) throw new Error("passkey-multiple-not-supported");
  return e[0];
}
function Hg(e, t) {
  const n = Mg(e), r = {
    challenge: Ku(),
    rpId: t,
    userVerification: "required",
    extensions: Cw(n.prfSalt)
  };
  return n.credentialId && (r.allowCredentials = [
    {
      type: "public-key",
      id: zt(pe(n.credentialId))
    }
  ]), r;
}
function Fg(e, t, n) {
  const r = {
    challenge: zt(n),
    rpId: t,
    userVerification: "required"
  }, i = e.filter((o) => o.credentialId).map((o) => ({ type: "public-key", id: zt(pe(o.credentialId)) }));
  return i.length > 0 && (r.allowCredentials = i), r;
}
function Kg(e, t = vt()) {
  const n = rn(e);
  if (!n) throw new Error("webauthn assertion options are missing");
  const r = typeof n.rpId == "string" ? n.rpId : typeof n.rp_id == "string" ? n.rp_id : void 0;
  if (r && r !== t)
    throw new Error("webauthn rpId does not match sandbox host");
  return {
    challenge: Di(n.challenge),
    rpId: t,
    userVerification: "required",
    ...gi(n.allowCredentials) ? { allowCredentials: gi(n.allowCredentials) } : {},
    ...typeof n.timeout == "number" ? { timeout: n.timeout } : {}
  };
}
function Vu(e, t) {
  return {
    publicKey: e,
    ...t ? { signal: t } : {}
  };
}
function qu(e) {
  return {
    id: e.id,
    rawId: Q(e.rawId),
    type: e.type
  };
}
function jg(e) {
  const t = e.response, n = typeof t.getTransports == "function" ? t.getTransports() : [];
  return {
    ...qu(e),
    response: {
      clientDataJSON: Q(t.clientDataJSON),
      attestationObject: Q(t.attestationObject),
      transports: n
    }
  };
}
function Gu(e) {
  const t = e.response;
  return {
    ...qu(e),
    response: {
      clientDataJSON: Q(t.clientDataJSON),
      authenticatorData: Q(t.authenticatorData),
      signature: Q(t.signature),
      userHandle: t.userHandle ? Q(t.userHandle) : null
    }
  };
}
function Vg(e) {
  const n = e?.getClientExtensionResults()?.prf?.results?.first;
  if (!n) throw new Error("passkey-prf-unavailable");
  const r = bu(n, "passkey PRF output");
  if (r.byteLength === 0) throw new Error("passkey-prf-unavailable");
  return r;
}
async function Us(e, t, n) {
  const r = await navigator.credentials.get(
    Vu(Hg(e, t), n)
  );
  if (!r) throw new Error("passkey-cancelled");
  const i = Vg(r), o = Q(r.rawId), s = e.find((a) => a.credentialId === o);
  return { prfOutput: i, prfSalt: s?.prfSalt ?? e[0].prfSalt, credentialId: o };
}
async function qg(e, t, n, r) {
  if (!await navigator.credentials.get(
    Vu(Fg(e, t, n), r)
  )) throw new Error("passkey-cancelled");
}
async function Gg(e) {
  const t = await navigator.credentials.get({
    publicKey: Kg(e)
  });
  if (!t) throw new Error("passkey-cancelled");
  return Gu(t);
}
const Wg = /ancestor|not the same as its|cross-?origin|different origin/i;
function Wu(e) {
  return typeof e?.name == "string" ? e.name : "";
}
function Zg(e) {
  return e instanceof Error || typeof e?.message == "string" ? e.message : typeof e == "string" ? e : "";
}
function Zu(e) {
  const t = Wu(e);
  return t === "SecurityError" || t === "NotSupportedError" || Wg.test(Zg(e));
}
function Xg(e) {
  return Wu(e) === "NotAllowedError" && !Zu(e);
}
function Ms(e) {
  const { options: t, challengeId: n } = Ug(e);
  return navigator.credentials.create({
    publicKey: t
  }).then((i) => {
    if (!i) throw new Error("passkey-cancelled");
    const o = Q(i.rawId), s = jg(i), a = e.authzCreationOptions !== void 0;
    return {
      credentialId: o,
      ...a ? { authzRegistration: n ? { challenge_id: n, credential: s } : s } : {}
    };
  });
}
const No = {
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
    "policyDowngrade.account": "账号",
    "policyDowngrade.accountNamed": "#{{index}} {{name}}",
    "policyDowngrade.accountUnnamed": "#{{index}}（未命名）",
    "policyDowngrade.appliesTo": "适用于",
    "policyDowngrade.appliesToValue": "这个账号在 ETH、BTC、Solana 上的签名",
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
let mi = "en";
function Yg(e) {
  return e === "zh" ? "zh" : "en";
}
function Jg(e) {
  const t = Yg(e);
  return t === mi ? !1 : (mi = t, !0);
}
function Qg() {
  return mi;
}
function em(e) {
  return typeof e == "string" && Object.hasOwn(No.en, e);
}
function H(e, t = {}) {
  const n = No[mi], r = No.en;
  return (n[e] ?? r[e] ?? e).replace(/\{\{(\w+)\}\}/g, (o, s) => String(t[s] ?? ""));
}
function tm(e) {
  return JSON.stringify(e ?? {});
}
function sc(e) {
  if (!e) return {};
  try {
    const t = JSON.parse(e);
    return typeof t == "object" && t !== null ? t : {};
  } catch {
    return {};
  }
}
function se(e, t, n) {
  e.dataset.i18nKey = t, e.dataset.i18nParams = tm(n), e.textContent = H(t, n);
}
function xe(e, t) {
  delete e.dataset.i18nKey, delete e.dataset.i18nParams, e.textContent = t;
}
function nm(e = document) {
  e.querySelectorAll("[data-i18n-key]").forEach((t) => {
    const n = t.dataset.i18nKey;
    n && (t.textContent = H(n, sc(t.dataset.i18nParams)));
  }), e.querySelectorAll("[data-i18n-placeholder-key]").forEach((t) => {
    const n = t.dataset.i18nPlaceholderKey;
    n && (t.placeholder = H(n, sc(t.dataset.i18nPlaceholderParams)));
  });
}
function Xu(e) {
  return {
    title: "policyDowngrade.title",
    subtitle: { text: e.target.name },
    confirmLabel: "policyDowngrade.confirm",
    policyDowngrade: e
  };
}
function nt(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
function Oe(e, t, n = "mono") {
  const r = nt("div", "review-field"), i = nt("span", "review-label");
  se(i, e);
  const o = nt("div", "review-value"), s = nt("span", n === "word" ? "review-word" : "review-mono");
  return typeof t == "string" ? xe(s, t) : se(s, t.key, t.params), o.append(s), r.append(i, o), r;
}
function Nr(e, t) {
  const n = nt("p", t);
  return se(n, e), n;
}
function rm(e, t) {
  xe(e, "");
  const { target: n } = t, r = nt("div", "review policy-downgrade-review");
  r.append(n.protection === "standard" ? Nr("policyDowngrade.standardWarning", "review-warning") : Nr("policyDowngrade.protectedNotice", "review-info"));
  const i = nt("div", "review-summary");
  i.append(
    Oe("policyDowngrade.setting", { key: "policyDowngrade.settingValue" }, "word"),
    Oe("policyDowngrade.key", n.name),
    Oe("policyDowngrade.tier", { key: n.protection === "standard" ? "policyDowngrade.standard" : "policyDowngrade.protected" }, "word")
  ), n.account !== null && i.append(
    Oe("policyDowngrade.account", n.accountName !== null ? { key: "policyDowngrade.accountNamed", params: { index: n.account, name: n.accountName } } : { key: "policyDowngrade.accountUnnamed", params: { index: n.account } }, "word"),
    Oe("policyDowngrade.appliesTo", { key: "policyDowngrade.appliesToValue" }, "word")
  ), r.append(i, Nr("policyDowngrade.onlyPolicy", "confirmation-note"));
  const o = nt("details", "review-details"), s = nt("summary", "review-details-toggle");
  se(s, "policyDowngrade.details");
  const a = nt("div", "review-details-body"), c = [
    ["eth", "policyDowngrade.eth"],
    ["btc_legacy", "policyDowngrade.btcLegacy"],
    ["btc_segwit", "policyDowngrade.btcSegwit"],
    ["btc_taproot", "policyDowngrade.btcTaproot"],
    ["solana", "policyDowngrade.solana"]
  ];
  a.append(Nr("policyDowngrade.serverFacts", "confirmation-note"));
  for (const [l, u] of c) {
    const d = n.addresses[l];
    d !== null ? a.append(Oe(u, d)) : n.signerKind === "hd-seed" && a.append(Oe(u, { key: "policyDowngrade.notUsed" }));
  }
  a.append(
    Oe("policyDowngrade.recordId", n.secretId),
    Oe("policyDowngrade.identity", n.identitySha256),
    Oe("policyDowngrade.revision", t.change.revision),
    Oe("policyDowngrade.expires", t.expiresAt),
    Oe("policyDowngrade.challengeId", t.challengeId)
  ), o.append(s, a), r.append(o), e.append(r);
}
const Yu = {
  sol: { symbol: "SOL", baseUnit: "lamports", decimals: 9 },
  eth: { symbol: "ETH", baseUnit: "wei", decimals: 18 },
  btc: { symbol: "BTC", baseUnit: "sats", decimals: 8 }
}, Hs = /^(?:0|[1-9][0-9]*)$/;
function Ju(e, t) {
  if (t === 0) return e;
  const n = e.padStart(t + 1, "0"), r = n.slice(-t).replace(/0+$/, "");
  return `${n.slice(0, -t)}${r ? `.${r}` : ""}`;
}
function ac(e, t, n) {
  const { symbol: r, baseUnit: i, decimals: o } = Yu[t];
  if (!Hs.test(e) || n === "base") return `${e} ${i}`;
  const s = `${Ju(e, o)} ${r}`;
  return n === "symbol" ? s : `${s} (${e} ${i})`;
}
const im = 253402300799n;
function om(e) {
  if (!Hs.test(e) || e.length > 12 || BigInt(e) > im) return null;
  const t = new Date(Number(e) * 1e3).toISOString(), n = t.slice(17, 19);
  return `${t.slice(0, 10)} ${t.slice(11, 16)}${n === "00" ? "" : `:${n}`} UTC`;
}
const bi = 8, vi = 6, cc = 28;
function lc(e, t) {
  if (!t || e.length <= bi + vi + 2) {
    const r = G("span", "review-mono");
    return xe(r, e), r;
  }
  const n = G("button", "review-mono review-elided");
  return n.type = "button", n.setAttribute("aria-expanded", "false"), n.title = e, xe(n, `${e.slice(0, bi)}…${e.slice(-vi)}`), n.addEventListener("click", () => {
    n.getAttribute("aria-expanded") !== "true" && (n.setAttribute("aria-expanded", "true"), xe(n, e));
  }), n;
}
function G(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
const sm = "http://www.w3.org/2000/svg";
function zr(e, t) {
  const n = document.createElementNS(sm, e);
  for (const [r, i] of Object.entries(t)) n.setAttribute(r, i);
  return n;
}
function uc() {
  const e = zr("svg", {
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
    zr("path", { d: "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" }),
    zr("path", { d: "M12 9v4.5" }),
    zr("path", { d: "M12 17h.01" })
  ), e;
}
function am(e) {
  if (!e.notes?.length) return null;
  const t = G("span", "review-notes");
  for (const n of e.notes) {
    const r = G("span", "review-note");
    typeof n == "string" ? se(r, n) : se(r, n.key, { count: n.count }), t.append(r);
  }
  return t;
}
function oo(e, t = "review-label") {
  const n = G(e.heading ? "p" : "span", t), r = {
    ...e.index === void 0 ? {} : { index: e.index },
    ...e.name === void 0 ? {} : { name: e.name }
  };
  return se(n, e.label, Object.keys(r).length > 0 ? r : void 0), n;
}
function cm(e, t) {
  const n = G("button", "review-copy");
  return n.type = "button", se(n, "review.copy"), n.addEventListener("click", () => {
    const r = () => {
      window.getSelection()?.selectAllChildren(e), se(n, "review.copySelected");
    };
    let i;
    try {
      i = navigator.clipboard.writeText(t);
    } catch {
      r();
      return;
    }
    i.then(() => se(n, "review.copied"), r);
  }), n;
}
function Dr(e, t = !1) {
  if (e.heading) return oo(e, "review-subheading");
  const n = G("div", e.raw ? "review-field review-field-raw" : "review-field"), r = am(e);
  if (e.raw) {
    const c = G("div", "review-raw-head"), l = G("pre", "review-raw");
    return xe(l, e.value), c.append(oo(e)), r && c.append(r), c.append(cm(l, e.value)), n.append(c, l), n;
  }
  const i = G("div", "review-value"), o = e.decimals !== void 0 && Hs.test(e.value) ? Ju(e.value, e.decimals) : null, s = e.time ? om(e.value) : null, a = t && e.shownAs ? 0 : e.native ? ac(e.value, e.native, e.unit).length : o !== null || e.time ? cc + 1 : t && e.elide && e.value.length > bi + vi + 2 ? bi + vi + 1 : e.value.length;
  if (t && e.to === void 0 && a + (e.secondary?.length ?? 0) <= cc && n.classList.add("review-field-inline"), t && e.shownAs) {
    const c = G("span", "review-word");
    se(c, e.shownAs), i.append(c);
  } else if (e.native) {
    const c = G("span", "review-mono");
    xe(c, ac(e.value, e.native, e.unit)), i.append(c);
  } else if (o !== null) {
    const c = G("span", "review-mono");
    xe(c, o);
    const l = G("span", "review-exact");
    se(l, "review.amount.baseUnitsDecimals", { value: e.value, count: e.decimals }), i.append(c, l);
  } else if (e.time) {
    const c = G("span", "review-mono");
    xe(c, s ?? e.value);
    const l = G("span", "review-exact");
    s === null ? se(l, "review.time.noDate") : se(l, "review.time.raw", { value: e.value }), i.append(c, l);
  } else
    i.append(lc(e.value, t && e.elide === !0));
  if (e.secondary !== void 0) {
    const c = G("span", "review-mono review-secondary");
    xe(c, e.secondary), i.append(c);
  }
  if (e.to !== void 0) {
    const c = G("span", "review-arrow");
    xe(c, "→"), i.append(c, lc(e.to, t));
  }
  return r && i.append(r), n.append(oo(e), i), n;
}
function dc(e, t, n) {
  const r = G("section", "review-group"), i = G("h2", "review-group-title");
  return se(i, e, t === void 0 ? void 0 : { index: t }), r.append(i, ...n), r;
}
function lm(e, t, n = {}) {
  xe(e, "");
  const r = G("div", "review"), i = G("div", "review-heading"), o = G("p", "review-kind");
  if (se(o, t.kind), i.append(o), n.session) {
    const u = G("p", "review-session"), d = G("span", "review-session-label");
    se(d, "context.session");
    const h = G("span", "review-session-value");
    xe(h, n.session), u.append(d, h), i.append(u);
  }
  r.append(i);
  for (const u of t.warnings ?? []) {
    const d = G("p", "review-warning"), h = G("span", "review-warning-text");
    se(h, u), d.append(uc(), h), r.append(d);
  }
  if (t.undecoded) {
    const u = G("p", "review-notice"), d = G("span", "review-notice-text");
    se(d, "sign.blindNotice"), u.append(uc(), d), r.append(u);
  }
  const s = G("div", "review-summary");
  s.append(...t.summary.map((u) => Dr(u, !0)), Dr(t.signer, !0)), r.append(s);
  const a = G("details", "review-details"), c = G("summary", "review-details-toggle");
  se(c, "review.details");
  const l = G("div", "review-details-body");
  t.details.forEach((u, d) => {
    const h = u.fields.map((p) => Dr(p));
    l.append(dc(u.title, u.index, d === 0 ? [Dr(t.signer), ...h] : h));
  }), n.rows && l.append(dc("review.group.request", void 0, [n.rows])), a.append(c, l), r.append(a), e.append(r);
}
function he(e) {
  throw new Error(`signing review is malformed: ${e}`);
}
function xi(e, t, n) {
  (typeof e != "object" || e === null || Array.isArray(e)) && he(`${n} must be an object`);
  const r = e;
  for (const i of Object.keys(r)) t.includes(i) || he(`${n}.${i} is not a review field`);
  return r;
}
function Qt(e, t, n) {
  return (!em(e) || !e.startsWith(`${t}.`)) && he(`${n} must be a ${t} translation key`), e;
}
function zo(e, t) {
  if (e !== void 0)
    return (typeof e != "number" || !Number.isSafeInteger(e) || e < 0) && he(`${t} must be a non-negative integer`), e;
}
function Jn(e, t) {
  return Array.isArray(e) || he(`${t} must be a list`), e;
}
const um = 4096, dm = /^[A-Za-z_][A-Za-z0-9_]{0,63} \([a-z][a-z0-9[\]]{0,31}\)$/;
function so(e, t) {
  const n = xi(e, ["label", "index", "name", "value", "native", "unit", "decimals", "time", "to", "secondary", "elide", "shownAs", "notes", "raw", "heading"], t), r = zo(n.index, `${t}.index`);
  n.name !== void 0 && (typeof n.name != "string" || (n.label === "review.field.typedField" ? n.name.length > um : !dm.test(n.name))) && he(`${t}.name is not a valid field name`), typeof n.value != "string" && he(`${t}.value must be a string`), n.native !== void 0 && !Object.hasOwn(Yu, n.native) && he(`${t}.native is not a native currency`), n.unit !== void 0 && (n.native === void 0 || n.unit !== "symbol" && n.unit !== "base") && he(`${t}.unit is not a unit of its currency`), n.decimals !== void 0 && (typeof n.decimals != "number" || !Number.isInteger(n.decimals) || n.decimals < 0 || n.decimals > 255 || n.native !== void 0) && he(`${t}.decimals must be an encoded decimal count`), n.time !== void 0 && (n.time !== "unix-seconds" || n.native !== void 0 || n.decimals !== void 0) && he(`${t}.time is not a time form`), n.to !== void 0 && typeof n.to != "string" && he(`${t}.to must be a string`), n.secondary !== void 0 && typeof n.secondary != "string" && he(`${t}.secondary must be a string`), n.elide !== void 0 && typeof n.elide != "boolean" && he(`${t}.elide must be a boolean`);
  const i = n.shownAs === void 0 ? void 0 : Qt(n.shownAs, "review.value", `${t}.shownAs`);
  n.raw !== void 0 && typeof n.raw != "boolean" && he(`${t}.raw must be a boolean`), n.heading !== void 0 && typeof n.heading != "boolean" && he(`${t}.heading must be a boolean`), n.heading && (n.value !== "" || n.native !== void 0 || n.decimals !== void 0 || n.time !== void 0 || n.notes !== void 0 || n.raw !== void 0 || n.to !== void 0 || n.secondary !== void 0 || n.elide !== void 0 || i !== void 0) && he(`${t} is a heading and cannot carry a value`);
  const o = n.notes === void 0 ? void 0 : Jn(n.notes, `${t}.notes`).map((s, a) => {
    const c = `${t}.notes[${a}]`;
    if (typeof s == "string") return Qt(s, "review.note", c);
    const l = xi(s, ["key", "count"], c), u = zo(l.count, `${c}.count`);
    return u === void 0 && he(`${c}.count is required`), { key: Qt(l.key, "review.note", `${c}.key`), count: u };
  });
  return {
    label: Qt(n.label, "review.field", `${t}.label`),
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
function Qu(e) {
  const t = xi(e, ["kind", "signer", "summary", "undecoded", "warnings", "details"], "review");
  typeof t.undecoded != "boolean" && he("review.undecoded must be a boolean");
  const n = t.warnings === void 0 ? void 0 : Jn(t.warnings, "review.warnings").map((r, i) => Qt(r, "review.warning", `review.warnings[${i}]`));
  return {
    kind: Qt(t.kind, "review.kind", "review.kind"),
    signer: so(t.signer, "review.signer"),
    summary: Jn(t.summary, "review.summary").map((r, i) => so(r, `review.summary[${i}]`)),
    undecoded: t.undecoded,
    ...n === void 0 ? {} : { warnings: n },
    details: Jn(t.details, "review.details").map((r, i) => {
      const o = `review.details[${i}]`, s = xi(r, ["title", "index", "fields"], o), a = zo(s.index, `${o}.index`);
      return {
        title: Qt(s.title, "review.group", `${o}.title`),
        ...a === void 0 ? {} : { index: a },
        fields: Jn(s.fields, `${o}.fields`).map((c, l) => so(c, `${o}.fields[${l}]`))
      };
    })
  };
}
let Fs = null, Ks = 0;
function fm(e) {
  Fs = e;
}
function ed() {
  return Date.now() < Ks;
}
function pm() {
  Ks = Date.now() + 150, Fs?.("ui.show");
}
function hm() {
  Ks = 0, Fs?.("ui.hide");
}
function ym() {
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
function td(e) {
  e.querySelectorAll(".dynamic").forEach((t) => t.remove());
}
function be(e) {
  return { text: e };
}
function fr(e, t) {
  return { key: e, params: t };
}
function X(e, t) {
  if (typeof t == "string") {
    se(e, t);
    return;
  }
  if ("text" in t) {
    xe(e, t.text);
    return;
  }
  se(e, t.key, t.params);
}
function js(e, t, n) {
  const r = ym();
  return document.body.classList.add("interactive"), document.body.classList.remove("confirming"), pm(), r.card.classList.remove("confirm-card"), X(r.title, e), X(r.subtitle, t), n ? X(r.body, n) : xe(r.body, ""), td(r.card), r;
}
function Vs() {
  document.body.classList.remove("interactive"), document.body.classList.remove("confirming");
  const e = document.getElementById("page")?.querySelector(".card");
  e && (e.classList.remove("confirm-card"), td(e)), hm();
}
function bn(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic"), (e.querySelector(".card-scroll") ?? e).append(t);
}
function fc(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic");
  const n = e.querySelector(".card-footer");
  if (!n) throw new Error("sandbox confirmation footer is unavailable");
  n.append(t);
}
function wm(e) {
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
function nd(e) {
  const t = document.createElement("div");
  t.className = "confirmation-details";
  for (const n of e.rows) {
    const r = document.createElement("div");
    r.className = "detail-row";
    const i = document.createElement("span");
    i.className = "detail-label", X(i, n.label), r.append(i, wm(n)), t.append(r);
  }
  return t;
}
function gm(e, t, n) {
  if (xe(e, ""), e.append(nd(t)), n) {
    const r = document.createElement("p");
    r.className = "confirmation-note", X(r, n), e.append(r);
  }
}
function pr(e) {
  const t = document.createElement("button");
  return t.type = "button", t.className = "action dynamic", X(t, e), t;
}
function rd(e = be("")) {
  const t = document.createElement("p");
  return t.className = "setup-status dynamic", X(t, e), t;
}
let Ur = null;
function qs() {
  return new Error("operation-superseded");
}
function sr(e) {
  const t = e.reason;
  return t instanceof Error ? t : qs();
}
function pc(e) {
  if (e?.aborted) throw sr(e);
}
function Ln(e) {
  Ur?.abort(qs());
  const t = new AbortController();
  return Ur = t, Promise.resolve().then(() => e(t.signal)).finally(() => {
    Ur === t && (Ur = null);
  });
}
async function mm(e) {
  return Ln(async (t) => {
    pc(t), pc(e.abortSignal);
    const n = new TextDecoder().decode(e.plaintext), r = js("plaintext.title", be(e.name), "plaintext.body"), i = document.createElement("pre");
    i.className = "plaintext dynamic", i.textContent = n;
    const o = pr("common.done"), s = pr("plaintext.copy");
    s.classList.add("secondary");
    const a = rd("plaintext.copyWarning");
    bn(r.card, i), bn(r.card, a), bn(r.card, s), bn(r.card, o);
    try {
      await new Promise((c, l) => {
        if (t.aborted) {
          l(sr(t));
          return;
        }
        if (e.abortSignal?.aborted) {
          l(sr(e.abortSignal));
          return;
        }
        let u = !1, d = () => {
        }, h = null, p = null, g = !e.prepareCopy;
        const w = () => {
          h?.abort(qs());
        }, f = (I) => {
          u || (u = !0, d(), w(), I());
        }, y = (I) => {
          f(() => l(sr(I)));
        }, v = () => y(t), x = () => {
          e.abortSignal && y(e.abortSignal);
        };
        d = () => {
          t.removeEventListener("abort", v), e.abortSignal?.removeEventListener("abort", x);
        }, t.addEventListener("abort", v, { once: !0 }), e.abortSignal?.addEventListener("abort", x, { once: !0 }), s.addEventListener("click", () => {
          if (p) return;
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
            } catch (P) {
              return Promise.reject(P);
            }
          }, T = e.prepareCopy && !g ? Promise.resolve(e.prepareCopy(I.signal)).then(() => {
            I.signal.aborted || t.aborted || e.abortSignal?.aborted || (g = !0, X(a, "plaintext.copyReady"), s.disabled = !1);
          }) : E().then(() => {
            I.signal.aborted || t.aborted || e.abortSignal?.aborted || (e.prepareCopy && (g = !1), X(a, "plaintext.copiedBody"), s.disabled = !1);
          });
          p = T, T.then(
            () => {
              I.signal.aborted;
            },
            (P) => {
              if (!(I.signal.aborted || u)) {
                if (t.aborted) {
                  y(t);
                  return;
                }
                if (e.abortSignal?.aborted) {
                  y(e.abortSignal);
                  return;
                }
                s.disabled = !1, X(a, "plaintext.copyFailed");
              }
            }
          ).finally(() => {
            h === I && (h = null), p === T && (p = null);
          });
        }), o.addEventListener(
          "click",
          () => {
            f(c);
          },
          { once: !0 }
        );
      });
    } finally {
      Vs();
    }
  });
}
function hr(e, t, n, r) {
  const i = js(e.title, e.subtitle, e.review ? void 0 : e.body);
  document.body.classList.add("confirming"), i.card.classList.add("confirm-card"), e.policyDowngrade ? rm(i.body, e.policyDowngrade) : e.review ? lm(i.body, e.review, {
    session: e.reviewSession,
    rows: e.details && nd(e.details)
  }) : e.details && gm(i.body, e.details, e.body);
  const o = pr(e.confirmLabel), s = pr("common.cancel");
  return s.classList.add("secondary"), fc(i.card, s), fc(i.card, o), o.disabled = !0, new Promise((a, c) => {
    let l = !1, u;
    const d = setTimeout(() => {
      try {
        u = n?.(i.card);
      } catch (w) {
        p(() => c(w));
        return;
      }
      (u?.ready ?? Promise.resolve()).then(
        () => {
          !l && !t.aborted && (o.disabled = !1);
        },
        (w) => p(() => c(w))
      );
    }, 500), h = () => {
      p(() => c(sr(t)));
    }, p = (g) => {
      l || (l = !0, clearTimeout(d), u?.dispose(), t.removeEventListener("abort", h), Vs(), g());
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
        u?.assertCurrent(), g = r ? r() : Promise.resolve();
      } catch (w) {
        p(() => c(w));
        return;
      }
      g.then(
        () => p(a),
        (w) => p(() => c(w))
      );
    }), s.addEventListener(
      "click",
      () => {
        p(() => c(new Error("operation-cancelled")));
      },
      { once: !0 }
    );
  });
}
function ki(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function bm(e) {
  return new Promise((t, n) => {
    const r = () => n(ki(e));
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function hc(e) {
  if (e?.aborted) throw ki(e);
}
async function id(e) {
  const t = Ds(e.wrapMeta), n = xs(t.wrapMeta);
  let r;
  try {
    const i = await Promise.race([
      Us(n, e.currentRpId, e.abortSignal).then(
        (o) => {
          if (e.abortSignal?.aborted)
            throw o.prfOutput.fill(0), ki(e.abortSignal);
          return o;
        },
        (o) => {
          throw e.abortSignal?.aborted ? ki(e.abortSignal) : o;
        }
      ),
      ...e.abortSignal ? [bm(e.abortSignal)] : []
    ]);
    return r = i.prfOutput, await od({
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
async function od(e) {
  const t = Ds(e.wrapMeta);
  let n;
  try {
    hc(e.abortSignal), n = await Uw(t.wrapMeta, {
      kind: "passkey",
      prfOutput: e.assertion.prfOutput,
      prfSalt: e.assertion.prfSalt
    }), hc(e.abortSignal);
    const r = Fu({
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
function vm(e) {
  if (typeof e != "object" || e === null)
    throw new m("invalid_payload", "request payload must be an object");
  return e;
}
function xm(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new m("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function yc(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function km(e) {
  const t = vm(e), n = t.kind === "keypair" ? "keypair" : t.kind === "static" ? "static" : null;
  if (!n) throw new m("invalid_payload", "kind must be static or keypair");
  const r = yc(t.name, "name");
  if (t.rootMetadata !== void 0) throw new m("invalid_payload", "seal cannot set vault root metadata");
  const i = xm(t.wrapMeta, "wrapMeta");
  if (n === "static") {
    if (t.curve !== void 0) throw new m("invalid_payload", "static seal cannot carry a curve");
    if (t.inputMode !== "parent-value")
      throw new m("invalid_payload", "static seal inputMode must be parent-value");
    return {
      name: r,
      kind: "static",
      inputMode: "parent-value",
      value: yc(t.value, "value"),
      wrapMeta: i
    };
  }
  if (t.inputMode !== void 0)
    throw new m("invalid_payload", "keypair seal is generate-only and does not accept inputMode");
  if (t.value !== void 0)
    throw new m("invalid_payload", "keypair seal cannot accept parent-provided private key material");
  let o;
  try {
    o = Rn(t.curve);
  } catch {
    throw new m("invalid_payload", "unsupported signing curve");
  }
  return {
    name: r,
    kind: "keypair",
    ...t.curve !== void 0 ? { curve: o } : {},
    wrapMeta: i
  };
}
function Ui(e) {
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
const Em = 512, sd = "avibe-vault-signed-context-replay:v2", Sm = "avibe-vault-signed-context-replay:v2", Am = "avibe-vault-signed-context-replay:v2", Ve = /* @__PURE__ */ new Map();
let wc = !1, Qn = null;
function Me(e) {
  return typeof e == "object" && e !== null;
}
function ht(e, t) {
  if (typeof e != "string" || e.length === 0) throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Fn(e, t) {
  if (e !== void 0) {
    if (typeof e != "string") throw new m("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function ao(e, t) {
  if (e !== void 0) {
    if (!Array.isArray(e) || !e.every((n) => typeof n == "string"))
      throw new m("invalid_payload", `${t} must be a string array`);
    return e;
  }
}
function _m(e, t) {
  if (e !== "static" && e !== "keypair") throw new m("invalid_payload", `${t} must be static or keypair`);
  return e;
}
function Im(e) {
  if (e !== "agent-deliver" && e !== "sign" && e !== "reveal")
    throw new m("invalid_payload", "context purpose is invalid");
  return e;
}
function Do(e, t) {
  if (typeof e != "number" || !Number.isSafeInteger(e) || e < 0)
    throw new m("invalid_payload", `${t} must be a non-negative integer`);
  return e;
}
function Pm(e, t) {
  if (!Array.isArray(e) || e.length < 16 || e.length > 128 || !e.every((n) => Number.isInteger(n) && n >= 0 && n <= 255))
    throw new m("invalid_payload", `${t} must contain 16..128 byte integers`);
  return e;
}
function Gs(e, t) {
  if (!Me(e)) throw new m(t, "agent delivery context.release must be an object");
  return {
    name: ht(e.name, "context.release.name"),
    ttlSecs: Do(e.ttlSecs, "context.release.ttlSecs"),
    approvalNonce: Pm(e.approvalNonce, "context.release.approvalNonce"),
    approvalExpiresAtUnix: Do(e.approvalExpiresAtUnix, "context.release.approvalExpiresAtUnix"),
    operationHash: ht(e.operationHash, "context.release.operationHash")
  };
}
function Ne(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(Ne).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${Ne(t[n])}`).join(",")}}`;
}
function Ws(e) {
  const t = Me(e) ? e : null;
  if (!t) throw new m("invalid_payload", "context must be an object");
  if (t.v !== 2) throw new m("invalid_payload", "context version must be 2");
  const n = Im(t.purpose);
  ht(t.requestId, "context.requestId"), t.grantId !== void 0 && Fn(t.grantId, "context.grantId");
  const r = Me(t.display) ? t.display : null;
  if (!r) throw new m("invalid_payload", "context.display must be an object");
  if (!Array.isArray(r.secrets) || r.secrets.length === 0)
    throw new m("invalid_payload", "context.display.secrets must be a non-empty array");
  r.secrets.forEach((c, l) => {
    if (!Me(c)) throw new m("invalid_payload", `context.display.secrets[${l}] must be an object`);
    ht(c.name, `context.display.secrets[${l}].name`), _m(c.kind, `context.display.secrets[${l}].kind`);
  }), r.sessionLabel !== void 0 && Fn(r.sessionLabel, "context.display.sessionLabel"), r.command !== void 0 && Fn(r.command, "context.display.command"), r.egress !== void 0 && Fn(r.egress, "context.display.egress");
  const i = r.source;
  if (i !== void 0) {
    if (!Me(i)) throw new m("invalid_payload", "context.display.source must be an object");
    i.env !== void 0 && ao(i.env, "context.display.source.env"), i.tags !== void 0 && ao(i.tags, "context.display.source.tags"), i.skills !== void 0 && ao(i.skills, "context.display.source.skills");
  }
  r.grantTtlSeconds !== void 0 && Do(r.grantTtlSeconds, "context.display.grantTtlSeconds");
  const o = Me(t.signature) ? t.signature : null;
  if (!o) throw new m("invalid_payload", "context.signature must be an object");
  if (o.alg !== "ed25519") throw new m("invalid_payload", "context signature alg must be ed25519");
  ht(o.keyId, "context.signature.keyId"), ht(o.value, "context.signature.value");
  const s = t.agent;
  if (s !== void 0) {
    if (!Me(s)) throw new m("invalid_payload", "context.agent must be an object");
    const c = Me(s.publicKey) ? s.publicKey : null;
    if (!c) throw new m("invalid_payload", "context.agent.publicKey must be an object");
    ht(c.public_key, "context.agent.publicKey.public_key"), c.fingerprint !== void 0 && Fn(c.fingerprint, "context.agent.publicKey.fingerprint"), ht(s.fingerprint, "context.agent.fingerprint");
  }
  const a = t.release;
  if (n === "agent-deliver") {
    const c = Gs(a, "invalid_payload");
    if (r.grantTtlSeconds === void 0)
      throw new m("invalid_payload", "agent delivery context must display its release TTL");
    if (r.grantTtlSeconds !== c.ttlSecs)
      throw new m("invalid_payload", "signed release TTL does not match display TTL");
  } else if (a !== void 0 && !Me(a))
    throw new m("invalid_payload", "context.release must be an object");
  return ht(t.expiresAt, "context.expiresAt"), t;
}
function Mi(e) {
  const { signature: t, ...n } = e;
  return Ne(n);
}
function Uo(e) {
  if (e.context.purpose !== e.expectedPurpose)
    throw new m("invalid_context", `${e.expectedPurpose} context has the wrong purpose`);
  if (!e.context.display.secrets.some(
    (n) => n.name === e.secret.name && n.kind === e.secret.kind
  )) {
    const n = e.secret.kind === "keypair" ? "key" : "secret";
    throw new m("invalid_context", `${e.expectedPurpose} context does not cover the requested ${n}`);
  }
}
function Ei(e) {
  if (e.expectedPurpose && e.context.purpose !== e.expectedPurpose)
    throw new m("invalid_context", "signed context purpose does not match operation");
  const t = e.now ?? Date.now(), n = Date.parse(e.context.expiresAt);
  if (!Number.isFinite(n) || n <= t)
    throw new m("context_expired", "signed context is expired", !0);
  if (!ug({
    rootMetadata: e.rootMetadata,
    keyId: e.context.signature.keyId,
    signature: e.context.signature.value,
    message: Mi(e.context)
  })) throw new m("invalid_context_signature", "signed context signature is invalid");
  if (e.context.purpose === "agent-deliver" && Gs(e.context.release, "invalid_context").approvalExpiresAtUnix * 1e3 <= t)
    throw new m("context_expired", "signed release approval is expired", !0);
}
function Zs(e) {
  for (const [t, n] of Ve)
    n <= e && Ve.delete(t);
}
function Tm() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
function Bn() {
  return typeof window < "u";
}
function Xs() {
  return new m("context_replay_state_unavailable", "signed context replay state is unavailable", !0);
}
function ad(e) {
  const t = Tm();
  if (!t && e && Bn()) throw Xs();
  return t;
}
function cd(e, t) {
  const n = /* @__PURE__ */ new Map();
  if (!Me(e) || e.version !== 2 || !Array.isArray(e.entries)) return n;
  for (const r of e.entries) {
    if (!Array.isArray(r) || r.length !== 2) continue;
    const [i, o] = r;
    typeof i != "string" || i.length === 0 || typeof o != "number" || !Number.isFinite(o) || o > t && n.set(i, Math.max(n.get(i) ?? 0, o));
  }
  return n;
}
function Ys(e = Date.now(), t = !1) {
  Zs(e);
  const n = ad(t);
  if (n)
    try {
      n.setItem(sd, JSON.stringify({ version: 2, entries: [...Ve] }));
    } catch {
      if (t && Bn()) throw Xs();
    }
}
function ld(e, t = Date.now(), n = !0) {
  for (const [r, i] of e)
    i > t && Ve.set(r, Math.max(Ve.get(r) ?? 0, i));
  n && Ys(t);
}
function Js() {
  if (!(Qn || typeof BroadcastChannel > "u"))
    try {
      Qn = new BroadcastChannel(Sm), Qn.addEventListener("message", (e) => {
        const t = Me(e.data) ? e.data : null;
        !t || t.type !== "signed-context-consumed" || ld(cd({ version: 2, entries: t.entries }, Date.now()));
      });
    } catch {
      Qn = null;
    }
}
function Bm(e = Date.now()) {
  wc || (Js(), ud(e, Bn()), wc = !0);
}
function ud(e = Date.now(), t = !1) {
  Js();
  const n = ad(t);
  if (n)
    try {
      ld(cd(JSON.parse(n.getItem(sd) ?? "null"), e), e, !1), Zs(e);
    } catch {
      if (t && Bn()) throw Xs();
      Ys(e);
    }
}
function $m(e) {
  Js();
  try {
    Qn?.postMessage({ type: "signed-context-consumed", entries: e });
  } catch {
  }
}
function Om() {
  const t = globalThis.navigator?.locks;
  return !t || typeof t != "object" || typeof t.request != "function" ? null : t;
}
async function Rm(e) {
  const t = Om();
  if (t) return await t.request(Am, { mode: "exclusive" }, e);
  if (typeof window < "u")
    throw new m("context_replay_lock_unavailable", "signed context replay lock is unavailable", !0);
  return await e();
}
function dd(e, t) {
  Bm(t);
  const n = /* @__PURE__ */ new Map();
  for (const r of e) {
    const i = Date.parse(r.expiresAt);
    if (!Number.isFinite(i) || i <= t)
      throw new m("context_expired", "signed context is expired", !0);
    n.set(r.requestId, Math.max(n.get(r.requestId) ?? 0, i));
  }
  Zs(t);
  for (const r of n.keys())
    if (Ve.has(r))
      throw new m("context_replayed", "signed context requestId was already used", !0);
  if (Ve.size + n.size > Em)
    throw new m("context_replay_cache_full", "signed context replay cache is full", !0);
  return n;
}
function Si(e, t = Date.now()) {
  dd(e, t);
}
async function Hi(e, t = Date.now()) {
  const n = [...e];
  await Rm(() => {
    ud(t, Bn());
    const r = dd(n, t), i = /* @__PURE__ */ new Map();
    for (const o of r.keys()) i.set(o, Ve.get(o));
    for (const [o, s] of r) Ve.set(o, s);
    try {
      Ys(t, Bn());
    } catch (o) {
      for (const [s, a] of i)
        a === void 0 ? Ve.delete(s) : Ve.set(s, a);
      throw o;
    }
    $m([...r]);
  });
}
async function Lm(e, t) {
  if (e.purpose !== "agent-deliver") throw new m("invalid_context", "context is not an agent delivery");
  if (!e.grantId) throw new m("invalid_context", "agent delivery context is missing grantId");
  const n = Gs(e.release, "invalid_context");
  if (n.name !== t) throw new m("invalid_context", "signed release name does not match release item");
  if (e.display.grantTtlSeconds === void 0 || e.display.grantTtlSeconds !== n.ttlSecs)
    throw new m("invalid_context", "signed release TTL does not match display TTL");
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
function Cm(e) {
  if (!e.agent) throw new m("invalid_context", "agent delivery context is missing agent key");
  if (e.agent.publicKey.fingerprint && e.agent.publicKey.fingerprint !== e.agent.fingerprint)
    throw new m("invalid_context", "agent public key fingerprint mismatch");
  return { ...e.agent.publicKey, fingerprint: e.agent.fingerprint };
}
async function Nm(e) {
  const t = e.map((n) => Mi(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(Ne(t))));
}
function gc(e) {
  return Ne(e.display);
}
const zm = "1.2.3";
let De = class Mo extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof Mo ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof Mo && n.cause.docsPath || n.docsPath, o = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...i ? [`Docs: https://abitype.dev${i}`] : [],
      ...r ? [`Details: ${r}`] : [],
      `Version: abitype@${zm}`
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
function xt(e, t) {
  return e.exec(t)?.groups;
}
const fd = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, pd = /^u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/, hd = /^\(.+?\).*?$/, mc = /^tuple(?<array>(\[(\d*)\])*)$/;
function Ho(e) {
  let t = e.type;
  if (mc.test(e.type) && "components" in e) {
    t = "(";
    const n = e.components.length;
    for (let i = 0; i < n; i++) {
      const o = e.components[i];
      t += Ho(o), i < n - 1 && (t += ", ");
    }
    const r = xt(mc, e.type);
    return t += `)${r?.array || ""}`, Ho({
      ...e,
      type: t
    });
  }
  return "indexed" in e && e.indexed && (t = `${t} indexed`), e.name ? `${t} ${e.name}` : t;
}
function Kn(e) {
  let t = "";
  const n = e.length;
  for (let r = 0; r < n; r++) {
    const i = e[r];
    t += Ho(i), r !== n - 1 && (t += ", ");
  }
  return t;
}
function Dm(e) {
  return e.type === "function" ? `function ${e.name}(${Kn(e.inputs)})${e.stateMutability && e.stateMutability !== "nonpayable" ? ` ${e.stateMutability}` : ""}${e.outputs?.length ? ` returns (${Kn(e.outputs)})` : ""}` : e.type === "event" ? `event ${e.name}(${Kn(e.inputs)})` : e.type === "error" ? `error ${e.name}(${Kn(e.inputs)})` : e.type === "constructor" ? `constructor(${Kn(e.inputs)})${e.stateMutability === "payable" ? " payable" : ""}` : e.type === "fallback" ? `fallback() external${e.stateMutability === "payable" ? " payable" : ""}` : "receive() external payable";
}
const yd = /^error (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Um(e) {
  return yd.test(e);
}
function Mm(e) {
  return xt(yd, e);
}
const wd = /^event (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Hm(e) {
  return wd.test(e);
}
function Fm(e) {
  return xt(wd, e);
}
const gd = /^function (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)(?: (?<scope>external|public{1}))?(?: (?<stateMutability>pure|view|nonpayable|payable{1}))?(?: returns\s?\((?<returns>.*?)\))?$/;
function Km(e) {
  return gd.test(e);
}
function jm(e) {
  return xt(gd, e);
}
const md = /^struct (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*) \{(?<properties>.*?)\}$/;
function bd(e) {
  return md.test(e);
}
function Vm(e) {
  return xt(md, e);
}
const vd = /^constructor\((?<parameters>.*?)\)(?:\s(?<stateMutability>payable{1}))?$/;
function qm(e) {
  return vd.test(e);
}
function Gm(e) {
  return xt(vd, e);
}
const xd = /^fallback\(\) external(?:\s(?<stateMutability>payable{1}))?$/;
function Wm(e) {
  return xd.test(e);
}
function Zm(e) {
  return xt(xd, e);
}
const Xm = /^receive\(\) external payable$/;
function Ym(e) {
  return Xm.test(e);
}
const Jm = /* @__PURE__ */ new Set(["indexed"]), Fo = /* @__PURE__ */ new Set([
  "calldata",
  "memory",
  "storage"
]);
class Qm extends De {
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
class eb extends De {
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
class tb extends De {
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
class nb extends De {
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
class rb extends De {
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
class ib extends De {
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
class ob extends De {
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
class Cn extends De {
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
class sb extends De {
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
class ab extends De {
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
class cb extends De {
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
class lb extends De {
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
function ub(e, t, n) {
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
const co = /* @__PURE__ */ new Map([
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
function db(e, t = {}) {
  if (Km(e))
    return fb(e, t);
  if (Hm(e))
    return pb(e, t);
  if (Um(e))
    return hb(e, t);
  if (qm(e))
    return yb(e, t);
  if (Wm(e))
    return wb(e);
  if (Ym(e))
    return {
      type: "receive",
      stateMutability: "payable"
    };
  throw new sb({ signature: e });
}
function fb(e, t = {}) {
  const n = jm(e);
  if (!n)
    throw new Cn({ signature: e, type: "function" });
  const r = Fe(n.parameters), i = [], o = r.length;
  for (let a = 0; a < o; a++)
    i.push(on(r[a], {
      modifiers: Fo,
      structs: t,
      type: "function"
    }));
  const s = [];
  if (n.returns) {
    const a = Fe(n.returns), c = a.length;
    for (let l = 0; l < c; l++)
      s.push(on(a[l], {
        modifiers: Fo,
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
function pb(e, t = {}) {
  const n = Fm(e);
  if (!n)
    throw new Cn({ signature: e, type: "event" });
  const r = Fe(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(on(r[s], {
      modifiers: Jm,
      structs: t,
      type: "event"
    }));
  return { name: n.name, type: "event", inputs: i };
}
function hb(e, t = {}) {
  const n = Mm(e);
  if (!n)
    throw new Cn({ signature: e, type: "error" });
  const r = Fe(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(on(r[s], { structs: t, type: "error" }));
  return { name: n.name, type: "error", inputs: i };
}
function yb(e, t = {}) {
  const n = Gm(e);
  if (!n)
    throw new Cn({ signature: e, type: "constructor" });
  const r = Fe(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(on(r[s], { structs: t, type: "constructor" }));
  return {
    type: "constructor",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i
  };
}
function wb(e) {
  const t = Zm(e);
  if (!t)
    throw new Cn({ signature: e, type: "fallback" });
  return {
    type: "fallback",
    stateMutability: t.stateMutability ?? "nonpayable"
  };
}
const gb = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, mb = /^\((?<type>.+?)\)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, bb = /^u?int$/;
function on(e, t) {
  const n = ub(e, t?.type, t?.structs);
  if (co.has(n))
    return co.get(n);
  const r = hd.test(e), i = xt(r ? mb : gb, e);
  if (!i)
    throw new tb({ param: e });
  if (i.name && xb(i.name))
    throw new nb({ param: e, name: i.name });
  const o = i.name ? { name: i.name } : {}, s = i.modifier === "indexed" ? { indexed: !0 } : {}, a = t?.structs ?? {};
  let c, l = {};
  if (r) {
    c = "tuple";
    const d = Fe(i.type), h = [], p = d.length;
    for (let g = 0; g < p; g++)
      h.push(on(d[g], { structs: a }));
    l = { components: h };
  } else if (i.type in a)
    c = "tuple", l = { components: a[i.type] };
  else if (bb.test(i.type))
    c = `${i.type}256`;
  else if (i.type === "address payable")
    c = "address";
  else if (c = i.type, t?.type !== "struct" && !kd(c))
    throw new eb({ type: c });
  if (i.modifier) {
    if (!t?.modifiers?.has?.(i.modifier))
      throw new rb({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
    if (Fo.has(i.modifier) && !kb(c, !!i.array))
      throw new ib({
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
  return co.set(n, u), u;
}
function Fe(e, t = [], n = "", r = 0) {
  const i = e.trim().length;
  for (let o = 0; o < i; o++) {
    const s = e[o], a = e.slice(o + 1);
    switch (s) {
      case ",":
        return r === 0 ? Fe(a, [...t, n.trim()]) : Fe(a, t, `${n}${s}`, r);
      case "(":
        return Fe(a, t, `${n}${s}`, r + 1);
      case ")":
        return Fe(a, t, `${n}${s}`, r - 1);
      default:
        return Fe(a, t, `${n}${s}`, r);
    }
  }
  if (n === "")
    return t;
  if (r !== 0)
    throw new lb({ current: n, depth: r });
  return t.push(n.trim()), t;
}
function kd(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || fd.test(e) || pd.test(e);
}
const vb = /^(?:after|alias|anonymous|apply|auto|byte|calldata|case|catch|constant|copyof|default|defined|error|event|external|false|final|function|immutable|implements|in|indexed|inline|internal|let|mapping|match|memory|mutable|null|of|override|partial|private|promise|public|pure|reference|relocatable|return|returns|sizeof|static|storage|struct|super|supports|switch|this|true|try|typedef|typeof|var|view|virtual)$/;
function xb(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || e === "tuple" || fd.test(e) || pd.test(e) || vb.test(e);
}
function kb(e, t) {
  return t || e === "bytes" || e === "string" || e === "tuple";
}
function Eb(e) {
  const t = {}, n = e.length;
  for (let s = 0; s < n; s++) {
    const a = e[s];
    if (!bd(a))
      continue;
    const c = Vm(a);
    if (!c)
      throw new Cn({ signature: a, type: "struct" });
    const l = c.properties.split(";"), u = [], d = l.length;
    for (let h = 0; h < d; h++) {
      const g = l[h].trim();
      if (!g)
        continue;
      const w = on(g, {
        type: "struct"
      });
      u.push(w);
    }
    if (!u.length)
      throw new ab({ signature: a });
    t[c.name] = u;
  }
  const r = {}, i = Object.entries(t), o = i.length;
  for (let s = 0; s < o; s++) {
    const [a, c] = i[s];
    r[a] = Ed(c, t);
  }
  return r;
}
const Sb = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*)(?<array>(?:\[\d*?\])+?)?$/;
function Ed(e = [], t = {}, n = /* @__PURE__ */ new Set()) {
  const r = [], i = e.length;
  for (let o = 0; o < i; o++) {
    const s = e[o];
    if (hd.test(s.type))
      r.push(s);
    else {
      const c = xt(Sb, s.type);
      if (!c?.type)
        throw new ob({ abiParameter: s });
      const { array: l, type: u } = c;
      if (u in t) {
        if (n.has(u))
          throw new cb({ type: u });
        r.push({
          ...s,
          type: `tuple${l ?? ""}`,
          components: Ed(t[u], t, /* @__PURE__ */ new Set([...n, u]))
        });
      } else if (kd(u))
        r.push(s);
      else
        throw new Qm({ type: u });
    }
  }
  return r;
}
function Ab(e) {
  const t = Eb(e), n = [], r = e.length;
  for (let i = 0; i < r; i++) {
    const o = e[i];
    bd(o) || n.push(db(o, t));
  }
  return n;
}
function Sd(e, { includeName: t = !1 } = {}) {
  return e ? e.map((n) => _b(n, { includeName: t })).join(t ? ", " : ",") : "";
}
function _b(e, { includeName: t }) {
  return e.type.startsWith("tuple") ? `(${Sd(e.components, { includeName: t })})${e.type.slice(5)}` : e.type + (t && e.name ? ` ${e.name}` : "");
}
function kr(e, { strict: t = !0 } = {}) {
  return !e || typeof e != "string" ? !1 : t ? /^0x[0-9a-fA-F]*$/.test(e) : e.startsWith("0x");
}
function fe(e) {
  return kr(e, { strict: !1 }) ? Math.ceil((e.length - 2) / 2) : e.length;
}
const Ad = "2.54.6";
let lo = {
  getDocsUrl: ({ docsBaseUrl: e, docsPath: t = "", docsSlug: n }) => t ? `${e ?? "https://viem.sh"}${t}${n ? `#${n}` : ""}` : void 0,
  version: `viem@${Ad}`
};
class K extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof K ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof K && n.cause.docsPath || n.docsPath, o = lo.getDocsUrl?.({ ...n, docsPath: i }), s = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...o ? [`Docs: ${o}`] : [],
      ...r ? [`Details: ${r}`] : [],
      ...lo.version ? [`Version: ${lo.version}`] : []
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
    }), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.name = n.name ?? this.name, this.shortMessage = t, this.version = Ad;
  }
  walk(t) {
    return _d(this, t);
  }
}
function _d(e, t) {
  return t?.(e) ? e : e && typeof e == "object" && "cause" in e && e.cause !== void 0 ? _d(e.cause, t) : t ? null : e;
}
class Ib extends K {
  constructor({ data: t, params: n, size: r }) {
    super([`Data size of ${r} bytes is too small for given parameters.`].join(`
`), {
      metaMessages: [
        `Params: (${Sd(n, { includeName: !0 })})`,
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
class Pb extends K {
  constructor({ cause: t } = {}) {
    super('Cannot decode zero data ("0x") with ABI parameters.', {
      name: "AbiDecodingZeroDataError",
      cause: t
    });
  }
}
class Tb extends K {
  constructor({ expectedLength: t, givenLength: n, type: r }) {
    super([
      `ABI encoding array length mismatch for type ${r}.`,
      `Expected length: ${t}`,
      `Given length: ${n}`
    ].join(`
`), { name: "AbiEncodingArrayLengthMismatchError" });
  }
}
class Bb extends K {
  constructor({ expectedSize: t, value: n }) {
    super(`Size of bytes "${n}" (bytes${fe(n)}) does not match expected size (bytes${t}).`, { name: "AbiEncodingBytesSizeMismatchError" });
  }
}
class $b extends K {
  constructor({ expectedLength: t, givenLength: n }) {
    super([
      "ABI encoding params/values length mismatch.",
      `Expected length (params): ${t}`,
      `Given length (values): ${n}`
    ].join(`
`), { name: "AbiEncodingLengthMismatchError" });
  }
}
class Ob extends K {
  constructor({ expectedSize: t, givenSize: n }) {
    super(`Expected bytes${t}, got bytes${n}.`, {
      name: "BytesSizeMismatchError"
    });
  }
}
class Rb extends K {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid encoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiEncodingType" });
  }
}
class Lb extends K {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid decoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiDecodingType" });
  }
}
class Cb extends K {
  constructor(t) {
    super([`Value "${t}" is not a valid array.`].join(`
`), {
      name: "InvalidArrayError"
    });
  }
}
class Id extends K {
  constructor({ offset: t, position: n, size: r }) {
    super(`Slice ${n === "start" ? "starting" : "ending"} at offset "${t}" is out-of-bounds (size: ${r}).`, { name: "SliceOffsetOutOfBoundsError" });
  }
}
class Pd extends K {
  constructor({ size: t, targetSize: n, type: r }) {
    super(`${r.charAt(0).toUpperCase()}${r.slice(1).toLowerCase()} size (${t}) exceeds padding size (${n}).`, { name: "SizeExceedsPaddingSizeError" });
  }
}
function Nn(e, { dir: t, size: n = 32 } = {}) {
  return typeof e == "string" ? Dt(e, { dir: t, size: n }) : Nb(e, { dir: t, size: n });
}
function Dt(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  const r = e.replace("0x", "");
  if (r.length > n * 2)
    throw new Pd({
      size: Math.ceil(r.length / 2),
      targetSize: n,
      type: "hex"
    });
  return `0x${r[t === "right" ? "padEnd" : "padStart"](n * 2, "0")}`;
}
function Nb(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  if (e.length > n)
    throw new Pd({
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
class Qs extends K {
  constructor({ max: t, min: n, signed: r, size: i, value: o }) {
    super(`Number "${o}" is not in safe ${i ? `${i * 8}-bit ${r ? "signed" : "unsigned"} ` : ""}integer range ${t ? `(${n} to ${t})` : `(above ${n})`}`, { name: "IntegerOutOfRangeError" });
  }
}
class zb extends K {
  constructor(t) {
    super(`Bytes value "${t}" is not a valid boolean. The bytes array must contain a single byte of either a 0 or 1 value.`, {
      name: "InvalidBytesBooleanError"
    });
  }
}
class Db extends K {
  constructor({ givenSize: t, maxSize: n }) {
    super(`Size cannot exceed ${n} bytes. Given size: ${t} bytes.`, { name: "SizeOverflowError" });
  }
}
function yr(e, { dir: t = "left" } = {}) {
  let n = typeof e == "string" ? e.replace("0x", "") : e, r = 0;
  for (let i = 0; i < n.length - 1 && n[t === "left" ? i : n.length - i - 1].toString() === "0"; i++)
    r++;
  return n = t === "left" ? n.slice(r) : n.slice(0, n.length - r), typeof e == "string" ? (n.length === 1 && t === "right" && (n = `${n}0`), `0x${n.length % 2 === 1 ? `0${n}` : n}`) : n;
}
function ct(e, { size: t }) {
  if (fe(e) > t)
    throw new Db({
      givenSize: fe(e),
      maxSize: t
    });
}
function Td(e, t = {}) {
  const { signed: n } = t;
  t.size && ct(e, { size: t.size });
  const r = BigInt(e);
  if (!n)
    return r;
  const i = (e.length - 2) / 2, o = (1n << BigInt(i) * 8n - 1n) - 1n;
  return r <= o ? r : r - BigInt(`0x${"f".padStart(i * 2, "f")}`) - 1n;
}
function ea(e, t = {}) {
  const n = Td(e, t), r = Number(n);
  if (!Number.isSafeInteger(r))
    throw new Qs({
      max: `${Number.MAX_SAFE_INTEGER}`,
      min: `${Number.MIN_SAFE_INTEGER}`,
      signed: t.signed,
      size: t.size,
      value: `${n}n`
    });
  return r;
}
const Ub = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function wr(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? F(e, t) : typeof e == "string" ? $d(e, t) : typeof e == "boolean" ? Bd(e, t) : Te(e, t);
}
function Bd(e, t = {}) {
  const n = `0x${Number(e)}`;
  return typeof t.size == "number" ? (ct(n, { size: t.size }), Nn(n, { size: t.size })) : n;
}
function Te(e, t = {}) {
  let n = "";
  for (let i = 0; i < e.length; i++)
    n += Ub[e[i]];
  const r = `0x${n}`;
  return typeof t.size == "number" ? (ct(r, { size: t.size }), Nn(r, { dir: "right", size: t.size })) : r;
}
function F(e, t = {}) {
  const { signed: n, size: r } = t, i = BigInt(e);
  let o;
  r ? n ? o = (1n << BigInt(r) * 8n - 1n) - 1n : o = 2n ** (BigInt(r) * 8n) - 1n : typeof e == "number" && (o = BigInt(Number.MAX_SAFE_INTEGER));
  const s = typeof o == "bigint" && n ? -o - 1n : 0;
  if (o && i > o || i < s) {
    const c = typeof e == "bigint" ? "n" : "";
    throw new Qs({
      max: o ? `${o}${c}` : void 0,
      min: `${s}${c}`,
      signed: n,
      size: r,
      value: `${e}${c}`
    });
  }
  const a = `0x${(n && i < 0 ? (1n << BigInt(r * 8)) + BigInt(i) : i).toString(16)}`;
  return r ? Nn(a, { size: r }) : a;
}
const Mb = /* @__PURE__ */ new TextEncoder();
function $d(e, t = {}) {
  const n = Mb.encode(e);
  return Te(n, t);
}
const Hb = /* @__PURE__ */ new TextEncoder();
function ta(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? Kb(e, t) : typeof e == "boolean" ? Fb(e, t) : kr(e) ? Ft(e, t) : Od(e, t);
}
function Fb(e, t = {}) {
  const n = new Uint8Array(1);
  return n[0] = Number(e), typeof t.size == "number" ? (ct(n, { size: t.size }), Nn(n, { size: t.size })) : n;
}
const pt = {
  zero: 48,
  nine: 57,
  A: 65,
  F: 70,
  a: 97,
  f: 102
};
function bc(e) {
  if (e >= pt.zero && e <= pt.nine)
    return e - pt.zero;
  if (e >= pt.A && e <= pt.F)
    return e - (pt.A - 10);
  if (e >= pt.a && e <= pt.f)
    return e - (pt.a - 10);
}
function Ft(e, t = {}) {
  let n = e;
  t.size && (ct(n, { size: t.size }), n = Nn(n, { dir: "right", size: t.size }));
  let r = n.slice(2);
  r.length % 2 && (r = `0${r}`);
  const i = r.length / 2, o = new Uint8Array(i);
  for (let s = 0, a = 0; s < i; s++) {
    const c = bc(r.charCodeAt(a++)), l = bc(r.charCodeAt(a++));
    if (c === void 0 || l === void 0)
      throw new K(`Invalid byte sequence ("${r[a - 2]}${r[a - 1]}" in "${r}").`);
    o[s] = c * 16 + l;
  }
  return o;
}
function Kb(e, t) {
  const n = F(e, t);
  return Ft(n);
}
function Od(e, t = {}) {
  const n = Hb.encode(e);
  return typeof t.size == "number" ? (ct(n, { size: t.size }), Nn(n, { dir: "right", size: t.size })) : n;
}
const Mr = /* @__PURE__ */ BigInt(2 ** 32 - 1), vc = /* @__PURE__ */ BigInt(32);
function jb(e, t = !1) {
  return t ? { h: Number(e & Mr), l: Number(e >> vc & Mr) } : { h: Number(e >> vc & Mr) | 0, l: Number(e & Mr) | 0 };
}
function Vb(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = jb(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const qb = (e, t, n) => e << n | t >>> 32 - n, Gb = (e, t, n) => t << n | e >>> 32 - n, Wb = (e, t, n) => t << n - 32 | e >>> 64 - n, Zb = (e, t, n) => e << n - 32 | t >>> 64 - n;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Xb(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function xc(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function gr(e, ...t) {
  if (!Xb(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function Ai(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Rd(e, t) {
  gr(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function Yb(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function mr(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function uo(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Je(e, t) {
  return e << 32 - t | e >>> t;
}
const Jb = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function Qb(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function e0(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = Qb(e[t]);
  return e;
}
const kc = Jb ? (e) => e : e0;
function t0(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function na(e) {
  return typeof e == "string" && (e = t0(e)), gr(e), e;
}
class Ld {
}
function Cd(e) {
  const t = (r) => e().update(na(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
const n0 = BigInt(0), jn = BigInt(1), r0 = BigInt(2), i0 = BigInt(7), o0 = BigInt(256), s0 = BigInt(113), Nd = [], zd = [], Dd = [];
for (let e = 0, t = jn, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], Nd.push(2 * (5 * r + n)), zd.push((e + 1) * (e + 2) / 2 % 64);
  let i = n0;
  for (let o = 0; o < 7; o++)
    t = (t << jn ^ (t >> i0) * s0) % o0, t & r0 && (i ^= jn << (jn << /* @__PURE__ */ BigInt(o)) - jn);
  Dd.push(i);
}
const Ud = Vb(Dd, !0), a0 = Ud[0], c0 = Ud[1], Ec = (e, t, n) => n > 32 ? Wb(e, t, n) : qb(e, t, n), Sc = (e, t, n) => n > 32 ? Zb(e, t, n) : Gb(e, t, n);
function l0(e, t = 24) {
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], u = n[c + 1], d = Ec(l, u, 1) ^ n[a], h = Sc(l, u, 1) ^ n[a + 1];
      for (let p = 0; p < 50; p += 10)
        e[s + p] ^= d, e[s + p + 1] ^= h;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = zd[s], c = Ec(i, o, a), l = Sc(i, o, a), u = Nd[s];
      i = e[u], o = e[u + 1], e[u] = c, e[u + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      for (let a = 0; a < 10; a++)
        n[a] = e[s + a];
      for (let a = 0; a < 10; a++)
        e[s + a] ^= ~n[(a + 2) % 10] & n[(a + 4) % 10];
    }
    e[0] ^= a0[r], e[1] ^= c0[r];
  }
  mr(n);
}
class ra extends Ld {
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (super(), this.pos = 0, this.posOut = 0, this.finished = !1, this.destroyed = !1, this.enableXOF = !1, this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.rounds = o, xc(r), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = Yb(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    kc(this.state32), l0(this.state32, this.rounds), kc(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Ai(this), t = na(t), gr(t);
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
    Ai(this, !1), gr(t), this.finish();
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
    return xc(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (Rd(t, this), this.finished)
      throw new Error("digest() was already called");
    return this.writeInto(t), this.destroy(), t;
  }
  digest() {
    return this.digestInto(new Uint8Array(this.outputLen));
  }
  destroy() {
    this.destroyed = !0, mr(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t || (t = new ra(n, r, i, s, o)), t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.destroyed = this.destroyed, t;
  }
}
const u0 = (e, t, n) => Cd(() => new ra(t, e, n)), d0 = u0(1, 136, 256 / 8);
function rt(e, t) {
  const n = t || "hex", r = d0(kr(e, { strict: !1 }) ? ta(e) : e);
  return n === "bytes" ? r : wr(r);
}
const f0 = (e) => rt(ta(e));
function p0(e) {
  return f0(e);
}
function h0(e) {
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
    throw new K("Unable to normalize signature.");
  return i;
}
const y0 = (e) => {
  const t = typeof e == "string" ? e : Dm(e);
  return h0(t);
};
function w0(e) {
  return p0(y0(e));
}
class an extends K {
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
class Md extends Map {
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
const fo = /* @__PURE__ */ new Md(8192);
function Hd(e, t) {
  if (fo.has(`${e}.${t}`))
    return fo.get(`${e}.${t}`);
  const n = e.substring(2).toLowerCase(), r = rt(Od(n), "bytes"), i = n.split("");
  for (let s = 0; s < 40; s += 2)
    r[s >> 1] >> 4 >= 8 && i[s] && (i[s] = i[s].toUpperCase()), (r[s >> 1] & 15) >= 8 && i[s + 1] && (i[s + 1] = i[s + 1].toUpperCase());
  const o = `0x${i.join("")}`;
  return fo.set(`${e}.${t}`, o), o;
}
const g0 = /^0x[a-fA-F0-9]{40}$/, po = /* @__PURE__ */ new Md(8192);
function cn(e, t) {
  const { strict: n = !0 } = t ?? {}, r = `${e}.${n}`;
  if (po.has(r))
    return po.get(r);
  const i = g0.test(e) ? e.toLowerCase() === e ? !0 : n ? Hd(e) === e : !0 : !1;
  return po.set(r, i), i;
}
function m0(e) {
  return typeof e[0] == "string" ? Ge(e) : b0(e);
}
function b0(e) {
  let t = 0;
  for (const i of e)
    t += i.length;
  const n = new Uint8Array(t);
  let r = 0;
  for (const i of e)
    n.set(i, r), r += i.length;
  return n;
}
function Ge(e) {
  return `0x${e.reduce((t, n) => t + n.replace("0x", ""), "")}`;
}
function ia(e, t, n, { strict: r } = {}) {
  return kr(e, { strict: !1 }) ? Vd(e, t, n, {
    strict: r
  }) : jd(e, t, n, {
    strict: r
  });
}
function Fd(e, t) {
  if (typeof t == "number" && t > 0 && t > fe(e) - 1)
    throw new Id({
      offset: t,
      position: "start",
      size: fe(e)
    });
}
function Kd(e, t, n) {
  if (typeof t == "number" && typeof n == "number" && fe(e) !== n - t)
    throw new Id({
      offset: n,
      position: "end",
      size: fe(e)
    });
}
function jd(e, t, n, { strict: r } = {}) {
  Fd(e, t);
  const i = e.slice(t, n);
  return r && Kd(i, t, n), i;
}
function Vd(e, t, n, { strict: r } = {}) {
  Fd(e, t);
  const i = `0x${e.replace("0x", "").slice((t ?? 0) * 2, (n ?? e.length) * 2)}`;
  return r && Kd(i, t, n), i;
}
const v0 = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, qd = /^(u?int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/;
function oa(e, t) {
  if (e.length !== t.length)
    throw new $b({
      expectedLength: e.length,
      givenLength: t.length
    });
  const n = x0({
    params: e,
    values: t
  });
  return aa(n);
}
function x0({ params: e, values: t }) {
  const n = [];
  for (let r = 0; r < e.length; r++)
    n.push(sa({ param: e[r], value: t[r] }));
  return n;
}
function sa({ param: e, value: t }) {
  const n = Fi(e.type);
  if (n) {
    const [r, i] = n;
    return E0(t, { length: r, param: { ...e, type: i } });
  }
  if (e.type === "tuple")
    return P0(t, {
      param: e
    });
  if (e.type === "address")
    return k0(t);
  if (e.type === "bool")
    return A0(t);
  if (e.type.startsWith("uint") || e.type.startsWith("int")) {
    const r = e.type.startsWith("int"), [, , i = "256"] = qd.exec(e.type) ?? [];
    return _0(t, {
      signed: r,
      size: Number(i)
    });
  }
  if (e.type.startsWith("bytes"))
    return S0(t, { param: e });
  if (e.type === "string")
    return I0(t);
  throw new Rb(e.type, {
    docsPath: "/docs/contract/encodeAbiParameters"
  });
}
function aa(e) {
  let t = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? t += 32 : t += fe(a);
  }
  const n = [], r = [];
  let i = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? (n.push(F(t + i, { size: 32 })), r.push(a), i += fe(a)) : n.push(a);
  }
  return Ge([...n, ...r]);
}
function k0(e) {
  if (!cn(e))
    throw new an({ address: e });
  return { dynamic: !1, encoded: Dt(e.toLowerCase()) };
}
function E0(e, { length: t, param: n }) {
  const r = t === null;
  if (!Array.isArray(e))
    throw new Cb(e);
  if (!r && e.length !== t)
    throw new Tb({
      expectedLength: t,
      givenLength: e.length,
      type: `${n.type}[${t}]`
    });
  let i = e.length === 0 && Ko(n);
  const o = [];
  for (let s = 0; s < e.length; s++) {
    const a = sa({ param: n, value: e[s] });
    a.dynamic && (i = !0), o.push(a);
  }
  if (r || i) {
    const s = aa(o);
    if (r) {
      const a = F(o.length, { size: 32 });
      return {
        dynamic: !0,
        encoded: Ge([a, s])
      };
    }
    if (i)
      return { dynamic: !0, encoded: s };
  }
  return {
    dynamic: !1,
    encoded: Ge(o.map(({ encoded: s }) => s))
  };
}
function S0(e, { param: t }) {
  const [, n] = t.type.split("bytes"), r = fe(e);
  if (!n) {
    let i = e;
    return r % 32 !== 0 && (i = Dt(i, {
      dir: "right",
      size: Math.ceil((e.length - 2) / 2 / 32) * 32
    })), {
      dynamic: !0,
      encoded: Ge([
        Dt(F(r, { size: 32 })),
        i
      ])
    };
  }
  if (r !== Number.parseInt(n, 10))
    throw new Bb({
      expectedSize: Number.parseInt(n, 10),
      value: e
    });
  return { dynamic: !1, encoded: Dt(e, { dir: "right" }) };
}
function A0(e) {
  if (typeof e != "boolean")
    throw new K(`Invalid boolean value: "${e}" (type: ${typeof e}). Expected: \`true\` or \`false\`.`);
  return { dynamic: !1, encoded: Dt(Bd(e)) };
}
function _0(e, { signed: t, size: n = 256 }) {
  if (typeof n == "number") {
    const r = 2n ** (BigInt(n) - (t ? 1n : 0n)) - 1n, i = t ? -r - 1n : 0n;
    if (e > r || e < i)
      throw new Qs({
        max: r.toString(),
        min: i.toString(),
        signed: t,
        size: n / 8,
        value: e.toString()
      });
  }
  return {
    dynamic: !1,
    encoded: F(e, {
      size: 32,
      signed: t
    })
  };
}
function I0(e) {
  const t = $d(e), n = Math.ceil(fe(t) / 32), r = [];
  for (let i = 0; i < n; i++)
    r.push(Dt(ia(t, i * 32, (i + 1) * 32), {
      dir: "right"
    }));
  return {
    dynamic: !0,
    encoded: Ge([
      Dt(F(fe(t), { size: 32 })),
      ...r
    ])
  };
}
function P0(e, { param: t }) {
  let n = !1;
  const r = [];
  for (let i = 0; i < t.components.length; i++) {
    const o = t.components[i], s = Array.isArray(e) ? i : o.name, a = sa({
      param: o,
      value: e[s]
    });
    r.push(a), a.dynamic && (n = !0);
  }
  return {
    dynamic: n,
    encoded: n ? aa(r) : Ge(r.map(({ encoded: i }) => i))
  };
}
function Fi(e) {
  const t = e.match(/^(.*)\[(\d+)?\]$/);
  return t ? (
    // Return `null` if the array is dynamic.
    [t[2] ? Number(t[2]) : null, t[1]]
  ) : void 0;
}
function Ko(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components.some(Ko);
  const n = Fi(t);
  return n ? Ko({ ...e, type: n[1] }) : !1;
}
const T0 = (e) => ia(w0(e), 0, 4);
class Ac extends K {
  constructor({ offset: t }) {
    super(`Offset \`${t}\` cannot be negative.`, {
      name: "NegativeOffsetError"
    });
  }
}
class B0 extends K {
  constructor({ length: t, position: n }) {
    super(`Position \`${n}\` is out of bounds (\`0 < position < ${t}\`).`, { name: "PositionOutOfBoundsError" });
  }
}
class $0 extends K {
  constructor({ count: t, limit: n }) {
    super(`Recursive read limit of \`${n}\` exceeded (recursive read count: \`${t}\`).`, { name: "RecursiveReadLimitExceededError" });
  }
}
const O0 = {
  bytes: new Uint8Array(),
  dataView: new DataView(new ArrayBuffer(0)),
  position: 0,
  positionReadCount: /* @__PURE__ */ new Map(),
  recursiveReadCount: 0,
  recursiveReadLimit: Number.POSITIVE_INFINITY,
  assertReadLimit() {
    if (this.recursiveReadCount >= this.recursiveReadLimit)
      throw new $0({
        count: this.recursiveReadCount + 1,
        limit: this.recursiveReadLimit
      });
  },
  assertPosition(e) {
    if (e < 0 || e > this.bytes.length - 1)
      throw new B0({
        length: this.bytes.length,
        position: e
      });
  },
  decrementPosition(e) {
    if (e < 0)
      throw new Ac({ offset: e });
    const t = this.position - e;
    this.assertPosition(t), this.position = t;
  },
  getReadCount(e) {
    return this.positionReadCount.get(e || this.position) || 0;
  },
  incrementPosition(e) {
    if (e < 0)
      throw new Ac({ offset: e });
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
function ca(e, { recursiveReadLimit: t = 8192 } = {}) {
  const n = Object.create(O0);
  return n.bytes = e, n.dataView = new DataView(e.buffer ?? e, e.byteOffset, e.byteLength), n.positionReadCount = /* @__PURE__ */ new Map(), n.recursiveReadLimit = t, n;
}
function R0(e, t = {}) {
  typeof t.size < "u" && ct(e, { size: t.size });
  const n = Te(e, t);
  return Td(n, t);
}
function L0(e, t = {}) {
  let n = e;
  if (typeof t.size < "u" && (ct(n, { size: t.size }), n = yr(n)), n.length > 1 || n[0] > 1)
    throw new zb(n);
  return !!n[0];
}
function wt(e, t = {}) {
  typeof t.size < "u" && ct(e, { size: t.size });
  const n = Te(e, t);
  return ea(n, t);
}
function C0(e, t = {}) {
  let n = e;
  return typeof t.size < "u" && (ct(n, { size: t.size }), n = yr(n, { dir: "right" })), new TextDecoder().decode(n);
}
function N0(e, t) {
  const n = typeof t == "string" ? Ft(t) : t, r = ca(n);
  if (fe(n) === 0 && e.length > 0)
    throw new Pb();
  if (fe(t) && fe(t) < 32)
    throw new Ib({
      data: typeof t == "string" ? t : Te(t),
      params: e,
      size: fe(t)
    });
  let i = 0;
  const o = [];
  for (let s = 0; s < e.length; ++s) {
    const a = e[s];
    i < n.length && r.setPosition(i);
    const [c, l] = kn(r, a, {
      staticPosition: 0
    });
    i += l, o.push(c);
  }
  return o;
}
function kn(e, t, { staticPosition: n }) {
  const r = Fi(t.type);
  if (r) {
    const [i, o] = r;
    return D0(e, { ...t, type: o }, { length: i, staticPosition: n });
  }
  if (t.type === "tuple")
    return F0(e, t, { staticPosition: n });
  if (t.type === "address")
    return z0(e);
  if (t.type === "bool")
    return U0(e);
  if (t.type.startsWith("bytes"))
    return M0(e, t, { staticPosition: n });
  if (t.type.startsWith("uint") || t.type.startsWith("int"))
    return H0(e, t);
  if (t.type === "string")
    return K0(e, { staticPosition: n });
  throw new Lb(t.type, {
    docsPath: "/docs/contract/decodeAbiParameters"
  });
}
const _c = 32, jo = 32;
function z0(e) {
  const t = e.readBytes(32);
  return [Hd(Te(jd(t, -20))), 32];
}
function D0(e, t, { length: n, staticPosition: r }) {
  if (n === null) {
    const s = wt(e.readBytes(jo)), a = r + s, c = a + _c;
    e.setPosition(a);
    const l = wt(e.readBytes(_c)), u = br(t);
    let d = 0;
    const h = [];
    for (let p = 0; p < l; ++p) {
      e.setPosition(c + (u ? p * 32 : d));
      const [g, w] = kn(e, t, {
        staticPosition: c
      });
      d += w, h.push(g), w === 0 && (e.assertReadLimit(), e._touch());
    }
    return e.setPosition(r + 32), [h, 32];
  }
  if (br(t)) {
    const s = wt(e.readBytes(jo)), a = r + s, c = [];
    for (let l = 0; l < n; ++l) {
      e.setPosition(a + l * 32);
      const [u] = kn(e, t, {
        staticPosition: a
      });
      c.push(u);
    }
    return e.setPosition(r + 32), [c, 32];
  }
  let i = 0;
  const o = [];
  for (let s = 0; s < n; ++s) {
    const [a, c] = kn(e, t, {
      staticPosition: r + i
    });
    i += c, o.push(a), c === 0 && (e.assertReadLimit(), e._touch());
  }
  return [o, i];
}
function U0(e) {
  return [L0(e.readBytes(32), { size: 32 }), 32];
}
function M0(e, t, { staticPosition: n }) {
  const [r, i] = t.type.split("bytes");
  if (!i) {
    const s = wt(e.readBytes(32));
    e.setPosition(n + s);
    const a = wt(e.readBytes(32));
    if (a === 0)
      return e.setPosition(n + 32), ["0x", 32];
    const c = e.readBytes(a);
    return e.setPosition(n + 32), [Te(c), 32];
  }
  return [Te(e.readBytes(Number.parseInt(i, 10), 32)), 32];
}
function H0(e, t) {
  const n = t.type.startsWith("int"), r = Number.parseInt(t.type.split("int")[1] || "256", 10), i = e.readBytes(32);
  return [
    r > 48 ? R0(i, { signed: n }) : wt(i, { signed: n }),
    32
  ];
}
function F0(e, t, { staticPosition: n }) {
  const r = t.components.length === 0 || t.components.some(({ name: s }) => !s), i = r ? [] : {};
  let o = 0;
  if (br(t)) {
    const s = wt(e.readBytes(jo)), a = n + s;
    for (let c = 0; c < t.components.length; ++c) {
      const l = t.components[c];
      e.setPosition(a + o);
      const [u, d] = kn(e, l, {
        staticPosition: a
      });
      o += d, i[r ? c : l?.name] = u;
    }
    return e.setPosition(n + 32), [i, 32];
  }
  for (let s = 0; s < t.components.length; ++s) {
    const a = t.components[s], [c, l] = kn(e, a, {
      staticPosition: n
    });
    i[r ? s : a?.name] = c, o += l;
  }
  return [i, o];
}
function K0(e, { staticPosition: t }) {
  const n = wt(e.readBytes(32)), r = t + n;
  e.setPosition(r);
  const i = wt(e.readBytes(32));
  if (i === 0)
    return e.setPosition(t + 32), ["", 32];
  const o = e.readBytes(i, 32), s = C0(yr(o));
  return e.setPosition(t + 32), [s, 32];
}
function br(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components?.some(br);
  const n = Fi(e.type);
  return !!(n && br({ ...e, type: n[1] }));
}
const j0 = (e, t, n) => JSON.stringify(e, (r, i) => typeof i == "bigint" ? i.toString() : i, n), V0 = {
  ether: -9,
  wei: 9
};
function q0(e, t) {
  let n = e.toString();
  const r = n.startsWith("-");
  r && (n = n.slice(1)), n = n.padStart(t, "0");
  let [i, o] = [
    n.slice(0, n.length - t),
    n.slice(n.length - t)
  ];
  return o = o.replace(/(0+)$/, ""), `${r ? "-" : ""}${i || "0"}${o ? `.${o}` : ""}`;
}
function Vo(e, t = "wei") {
  return q0(e, V0[t]);
}
function G0(e) {
  const t = Object.entries(e).map(([r, i]) => i === void 0 || i === !1 ? null : [r, i]).filter(Boolean), n = t.reduce((r, [i]) => Math.max(r, i.length), 0);
  return t.map(([r, i]) => `  ${`${r}:`.padEnd(n + 1)}  ${i}`).join(`
`);
}
class W0 extends K {
  constructor({ transaction: t }) {
    super("Cannot infer a transaction type from provided transaction.", {
      metaMessages: [
        "Provided Transaction:",
        "{",
        G0(t),
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
class Z0 extends K {
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
class X0 extends K {
  constructor({ storageKey: t }) {
    super(`Size for storage key "${t}" is invalid. Expected 32 bytes. Got ${Math.floor((t.length - 2) / 2)} bytes.`, { name: "InvalidStorageKeySizeError" });
  }
}
function $n(e, t = "hex") {
  const n = Gd(e), r = ca(new Uint8Array(n.length));
  return n.encode(r), t === "hex" ? Te(r.bytes) : r.bytes;
}
function Gd(e) {
  return Array.isArray(e) ? Y0(e.map((t) => Gd(t))) : J0(e);
}
function Y0(e) {
  const t = e.reduce((i, o) => i + o.length, 0), n = Wd(t);
  return {
    length: t <= 55 ? 1 + t : 1 + n + t,
    encode(i) {
      t <= 55 ? i.pushByte(192 + t) : (i.pushByte(247 + n), n === 1 ? i.pushUint8(t) : n === 2 ? i.pushUint16(t) : n === 3 ? i.pushUint24(t) : i.pushUint32(t));
      for (const { encode: o } of e)
        o(i);
    }
  };
}
function J0(e) {
  const t = typeof e == "string" ? Ft(e) : e, n = Wd(t.length);
  return {
    length: t.length === 1 && t[0] < 128 ? 1 : t.length <= 55 ? 1 + t.length : 1 + n + t.length,
    encode(i) {
      t.length === 1 && t[0] < 128 ? i.pushBytes(t) : t.length <= 55 ? (i.pushByte(128 + t.length), i.pushBytes(t)) : (i.pushByte(183 + n), n === 1 ? i.pushUint8(t.length) : n === 2 ? i.pushUint16(t.length) : n === 3 ? i.pushUint24(t.length) : i.pushUint32(t.length), i.pushBytes(t));
    }
  };
}
function Wd(e) {
  if (e < 2 ** 8)
    return 1;
  if (e < 2 ** 16)
    return 2;
  if (e < 2 ** 24)
    return 3;
  if (e < 2 ** 32)
    return 4;
  throw new K("Length is too large.");
}
class Ki extends K {
  constructor({ cause: t, maxFeePerGas: n } = {}) {
    super(`The fee cap (\`maxFeePerGas\`${n ? ` = ${Vo(n)} gwei` : ""}) cannot be higher than the maximum allowed value (2^256-1).`, {
      cause: t,
      name: "FeeCapTooHighError"
    });
  }
}
Object.defineProperty(Ki, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max fee per gas higher than 2\^256-1|fee cap higher than 2\^256-1/
});
class Zd extends K {
  constructor({ cause: t, maxPriorityFeePerGas: n, maxFeePerGas: r } = {}) {
    super([
      `The provided tip (\`maxPriorityFeePerGas\`${n ? ` = ${Vo(n)} gwei` : ""}) cannot be higher than the fee cap (\`maxFeePerGas\`${r ? ` = ${Vo(r)} gwei` : ""}).`
    ].join(`
`), {
      cause: t,
      name: "TipAboveFeeCapError"
    });
  }
}
Object.defineProperty(Zd, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max priority fee per gas higher than max fee per gas|tip higher than fee cap/
});
const la = 2n ** 256n - 1n;
function Xd(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((o) => Ft(o)) : e.blobs, i = [];
  for (const o of r)
    i.push(Uint8Array.from(t.blobToKzgCommitment(o)));
  return n === "bytes" ? i : i.map((o) => Te(o));
}
function Yd(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((s) => Ft(s)) : e.blobs, i = typeof e.commitments[0] == "string" ? e.commitments.map((s) => Ft(s)) : e.commitments, o = [];
  for (let s = 0; s < r.length; s++) {
    const a = r[s], c = i[s];
    o.push(Uint8Array.from(t.computeBlobKzgProof(a, c)));
  }
  return n === "bytes" ? o : o.map((s) => Te(s));
}
function Q0(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function ev(e, t, n) {
  return e & t ^ ~e & n;
}
function tv(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
class nv extends Ld {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = uo(this.buffer);
  }
  update(t) {
    Ai(this), t = na(t), gr(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = uo(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Ai(this), Rd(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, mr(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    Q0(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = uo(t), c = this.outputLen;
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
const Bt = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), rv = /* @__PURE__ */ Uint32Array.from([
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
]), $t = /* @__PURE__ */ new Uint32Array(64);
class iv extends nv {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = Bt[0] | 0, this.B = Bt[1] | 0, this.C = Bt[2] | 0, this.D = Bt[3] | 0, this.E = Bt[4] | 0, this.F = Bt[5] | 0, this.G = Bt[6] | 0, this.H = Bt[7] | 0;
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
      $t[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = $t[d - 15], p = $t[d - 2], g = Je(h, 7) ^ Je(h, 18) ^ h >>> 3, w = Je(p, 17) ^ Je(p, 19) ^ p >>> 10;
      $t[d] = w + $t[d - 7] + g + $t[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const h = Je(a, 6) ^ Je(a, 11) ^ Je(a, 25), p = u + h + ev(a, c, l) + rv[d] + $t[d] | 0, w = (Je(r, 2) ^ Je(r, 13) ^ Je(r, 22)) + tv(r, i, o) | 0;
      u = l, l = c, c = a, a = s + p | 0, s = o, o = i, i = r, r = p + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    mr($t);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), mr(this.buffer);
  }
}
const ov = /* @__PURE__ */ Cd(() => new iv()), sv = ov;
function av(e, t) {
  return sv(kr(e, { strict: !1 }) ? ta(e) : e);
}
function cv(e) {
  const { commitment: t, version: n = 1 } = e, r = e.to ?? (typeof t == "string" ? "hex" : "bytes"), i = av(t);
  return i.set([n], 0), r === "bytes" ? i : Te(i);
}
function lv(e) {
  const { commitments: t, version: n } = e, r = e.to ?? (typeof t[0] == "string" ? "hex" : "bytes"), i = [];
  for (const o of t)
    i.push(cv({
      commitment: o,
      to: r,
      version: n
    }));
  return i;
}
const Ic = 6, Jd = 32, ua = 4096, Qd = Jd * ua, Pc = Qd * Ic - // terminator byte (0x80).
1 - // zero byte (0x00) appended to each field element.
1 * ua * Ic, ef = 1;
class uv extends K {
  constructor({ maxSize: t, size: n }) {
    super("Blob size is too large.", {
      metaMessages: [`Max: ${t} bytes`, `Given: ${n} bytes`],
      name: "BlobSizeTooLargeError"
    });
  }
}
class tf extends K {
  constructor() {
    super("Blob data must not be empty.", { name: "EmptyBlobError" });
  }
}
class dv extends K {
  constructor({ hash: t, size: n }) {
    super(`Versioned hash "${t}" size is invalid.`, {
      metaMessages: ["Expected: 32", `Received: ${n}`],
      name: "InvalidVersionedHashSizeError"
    });
  }
}
class fv extends K {
  constructor({ hash: t, version: n }) {
    super(`Versioned hash "${t}" version is invalid.`, {
      metaMessages: [
        `Expected: ${ef}`,
        `Received: ${n}`
      ],
      name: "InvalidVersionedHashVersionError"
    });
  }
}
function pv(e) {
  const t = e.to ?? (typeof e.data == "string" ? "hex" : "bytes"), n = typeof e.data == "string" ? Ft(e.data) : e.data, r = fe(n);
  if (!r)
    throw new tf();
  if (r > Pc)
    throw new uv({
      maxSize: Pc,
      size: r
    });
  const i = [];
  let o = !0, s = 0;
  for (; o; ) {
    const a = ca(new Uint8Array(Qd));
    let c = 0;
    for (; c < ua; ) {
      const l = n.slice(s, s + (Jd - 1));
      if (a.pushByte(0), a.pushBytes(l), l.length < 31) {
        a.pushByte(128), o = !1;
        break;
      }
      c++, s += 31;
    }
    i.push(a);
  }
  return t === "bytes" ? i.map((a) => a.bytes) : i.map((a) => Te(a.bytes));
}
function hv(e) {
  const { data: t, kzg: n, to: r } = e, i = e.blobs ?? pv({ data: t, to: r }), o = e.commitments ?? Xd({ blobs: i, kzg: n, to: r }), s = e.proofs ?? Yd({ blobs: i, commitments: o, kzg: n, to: r }), a = [];
  for (let c = 0; c < i.length; c++)
    a.push({
      blob: i[c],
      commitment: o[c],
      proof: s[c]
    });
  return a;
}
function yv(e) {
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
  throw new W0({ transaction: e });
}
class ji extends K {
  constructor({ chainId: t }) {
    super(typeof t == "number" ? `Chain ID "${t}" is invalid.` : "Chain ID is invalid.", { name: "InvalidChainIdError" });
  }
}
function wv(e) {
  const { authorizationList: t } = e;
  if (t)
    for (const n of t) {
      const { chainId: r } = n, i = n.address;
      if (!cn(i))
        throw new an({ address: i });
      if (r < 0)
        throw new ji({ chainId: r });
    }
  da(e);
}
function gv(e) {
  const { blobVersionedHashes: t } = e;
  if (t) {
    if (t.length === 0)
      throw new tf();
    for (const n of t) {
      const r = fe(n), i = ea(ia(n, 0, 1));
      if (r !== 32)
        throw new dv({ hash: n, size: r });
      if (i !== ef)
        throw new fv({
          hash: n,
          version: i
        });
    }
  }
  da(e);
}
function da(e) {
  const { chainId: t, maxPriorityFeePerGas: n, maxFeePerGas: r, to: i } = e;
  if (t <= 0)
    throw new ji({ chainId: t });
  if (i && !cn(i))
    throw new an({ address: i });
  if (r && r > la)
    throw new Ki({ maxFeePerGas: r });
  if (n && r && n > r)
    throw new Zd({ maxFeePerGas: r, maxPriorityFeePerGas: n });
}
function mv(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (t <= 0)
    throw new ji({ chainId: t });
  if (o && !cn(o))
    throw new an({ address: o });
  if (n || i)
    throw new K("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid EIP-2930 Transaction attribute.");
  if (r && r > la)
    throw new Ki({ maxFeePerGas: r });
}
function bv(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (o && !cn(o))
    throw new an({ address: o });
  if (typeof t < "u" && t <= 0)
    throw new ji({ chainId: t });
  if (n || i)
    throw new K("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid Legacy Transaction attribute.");
  if (r && r > la)
    throw new Ki({ maxFeePerGas: r });
}
function Vi(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (let n = 0; n < e.length; n++) {
    const { address: r, storageKeys: i } = e[n];
    for (let o = 0; o < i.length; o++)
      if (i[o].length - 2 !== 64)
        throw new X0({ storageKey: i[o] });
    if (!cn(r, { strict: !1 }))
      throw new an({ address: r });
    t.push([r, i]);
  }
  return t;
}
function vv(e, t) {
  const n = yv(e);
  return n === "eip1559" ? Ev(e, t) : n === "eip2930" ? Sv(e, t) : n === "eip4844" ? kv(e, t) : n === "eip7702" ? xv(e, t) : Av(e);
}
function xv(e, t) {
  const { authorizationList: n, chainId: r, gas: i, nonce: o, to: s, value: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: u, data: d } = e;
  wv(e);
  const h = Vi(u), p = _v(n);
  return Ge([
    "0x04",
    $n([
      F(r),
      o ? F(o) : "0x",
      l ? F(l) : "0x",
      c ? F(c) : "0x",
      i ? F(i) : "0x",
      s ?? "0x",
      a ? F(a) : "0x",
      d ?? "0x",
      h,
      p,
      ...Er(e, t)
    ])
  ]);
}
function kv(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerBlobGas: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: u, data: d } = e;
  gv(e);
  let h = e.blobVersionedHashes, p = e.sidecars;
  if (e.blobs && (typeof h > "u" || typeof p > "u")) {
    const x = typeof e.blobs[0] == "string" ? e.blobs : e.blobs.map((T) => Te(T)), I = e.kzg, E = Xd({
      blobs: x,
      kzg: I
    });
    if (typeof h > "u" && (h = lv({
      commitments: E
    })), typeof p > "u") {
      const T = Yd({ blobs: x, commitments: E, kzg: I });
      p = hv({ blobs: x, commitments: E, proofs: T });
    }
  }
  const g = Vi(u), w = [
    F(n),
    i ? F(i) : "0x",
    l ? F(l) : "0x",
    c ? F(c) : "0x",
    r ? F(r) : "0x",
    o ?? "0x",
    s ? F(s) : "0x",
    d ?? "0x",
    g,
    a ? F(a) : "0x",
    h ?? [],
    ...Er(e, t)
  ], f = [], y = [], v = [];
  if (p)
    for (let x = 0; x < p.length; x++) {
      const { blob: I, commitment: E, proof: T } = p[x];
      f.push(I), y.push(E), v.push(T);
    }
  return Ge([
    "0x03",
    // If sidecars are enabled, envelope turns into a "wrapper":
    $n(p ? [w, f, y, v] : w)
  ]);
}
function Ev(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerGas: a, maxPriorityFeePerGas: c, accessList: l, data: u } = e;
  da(e);
  const d = Vi(l), h = [
    F(n),
    i ? F(i) : "0x",
    c ? F(c) : "0x",
    a ? F(a) : "0x",
    r ? F(r) : "0x",
    o ?? "0x",
    s ? F(s) : "0x",
    u ?? "0x",
    d,
    ...Er(e, t)
  ];
  return Ge([
    "0x02",
    $n(h)
  ]);
}
function Sv(e, t) {
  const { chainId: n, gas: r, data: i, nonce: o, to: s, value: a, accessList: c, gasPrice: l } = e;
  mv(e);
  const u = Vi(c), d = [
    F(n),
    o ? F(o) : "0x",
    l ? F(l) : "0x",
    r ? F(r) : "0x",
    s ?? "0x",
    a ? F(a) : "0x",
    i ?? "0x",
    u,
    ...Er(e, t)
  ];
  return Ge([
    "0x01",
    $n(d)
  ]);
}
function Av(e, t) {
  const { chainId: n = 0, gas: r, data: i, nonce: o, to: s, value: a, gasPrice: c } = e;
  bv(e);
  let l = [
    o ? F(o) : "0x",
    c ? F(c) : "0x",
    r ? F(r) : "0x",
    s ?? "0x",
    a ? F(a) : "0x",
    i ?? "0x"
  ];
  return n > 0 && (l = [
    ...l,
    F(n),
    "0x",
    "0x"
  ]), $n(l);
}
function Er(e, t) {
  const n = t ?? e, { v: r, yParity: i } = n;
  if (typeof n.r > "u")
    return [];
  if (typeof n.s > "u")
    return [];
  if (typeof r > "u" && typeof i > "u")
    return [];
  const o = yr(n.r), s = yr(n.s);
  return [typeof i == "number" ? i ? F(1) : "0x" : r === 0n ? "0x" : r === 1n ? F(1) : r === 27n ? "0x" : F(1), o === "0x00" ? "0x" : o, s === "0x00" ? "0x" : s];
}
function _v(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (const n of e) {
    const { chainId: r, nonce: i, ...o } = n, s = n.address;
    t.push([
      r ? wr(r) : "0x",
      s,
      i ? wr(i) : "0x",
      ...Er({}, o)
    ]);
  }
  return t;
}
class Iv extends K {
  constructor({ domain: t }) {
    super(`Invalid domain "${j0(t)}".`, {
      metaMessages: ["Must be a valid EIP-712 domain."]
    });
  }
}
class Pv extends K {
  constructor({ primaryType: t, types: n }) {
    super(`Invalid primary type \`${t}\` must be one of \`${JSON.stringify(Object.keys(n))}\`.`, {
      docsPath: "/api/glossary/Errors#typeddatainvalidprimarytypeerror",
      metaMessages: ["Check that the primary type is a key in `types`."]
    });
  }
}
class Tv extends K {
  constructor({ type: t }) {
    super(`Struct type "${t}" is invalid.`, {
      metaMessages: ["Struct type must not be a Solidity type."],
      name: "InvalidStructTypeError"
    });
  }
}
function Bv(e) {
  const { domain: t, message: n, primaryType: r, types: i } = e, o = (s, a) => {
    for (const c of s) {
      const { name: l, type: u } = c, d = a[l], h = u.match(qd);
      if (h && (typeof d == "number" || typeof d == "bigint")) {
        const [w, f, y] = h;
        F(d, {
          signed: f === "int",
          size: Number.parseInt(y, 10) / 8
        });
      }
      if (u === "address" && typeof d == "string" && !cn(d))
        throw new an({ address: d });
      const p = u.match(v0);
      if (p) {
        const [w, f] = p;
        if (f && fe(d) !== Number.parseInt(f, 10))
          throw new Ob({
            expectedSize: Number.parseInt(f, 10),
            givenSize: fe(d)
          });
      }
      const g = i[u];
      g && ($v(u), o(g, d));
    }
  };
  if (i.EIP712Domain && t) {
    if (typeof t != "object")
      throw new Iv({ domain: t });
    o(i.EIP712Domain, t);
  }
  if (r !== "EIP712Domain")
    if (i[r])
      o(i[r], n);
    else
      throw new Pv({ primaryType: r, types: i });
}
function nf({ domain: e }) {
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
function $v(e) {
  if (e === "address" || e === "bool" || e === "string" || e.startsWith("bytes") || e.startsWith("uint") || e.startsWith("int"))
    throw new Tv({ type: e });
}
function Ov(e) {
  const { domain: t = {}, message: n, primaryType: r } = e, i = {
    EIP712Domain: nf({ domain: t }),
    ...e.types
  };
  Bv({
    domain: t,
    message: n,
    primaryType: r,
    types: i
  });
  const o = ["0x1901"];
  return t && o.push(Rv({
    domain: t,
    types: i
  })), r !== "EIP712Domain" && o.push(rf({
    data: n,
    primaryType: r,
    types: i
  })), rt(m0(o));
}
function Rv({ domain: e, types: t }) {
  return rf({
    data: e,
    primaryType: "EIP712Domain",
    types: t
  });
}
function rf({ data: e, primaryType: t, types: n }) {
  const r = of({
    data: e,
    primaryType: t,
    types: n
  });
  return rt(r);
}
function of({ data: e, primaryType: t, types: n }) {
  const r = [{ type: "bytes32" }], i = [Lv({ primaryType: t, types: n })];
  for (const o of n[t]) {
    const [s, a] = af({
      types: n,
      name: o.name,
      type: o.type,
      value: e[o.name]
    });
    r.push(s), i.push(a);
  }
  return oa(r, i);
}
function Lv({ primaryType: e, types: t }) {
  const n = wr(Cv({ primaryType: e, types: t }));
  return rt(n);
}
function Cv({ primaryType: e, types: t }) {
  let n = "";
  const r = sf({ primaryType: e, types: t });
  r.delete(e);
  const i = [e, ...Array.from(r).sort()];
  for (const o of i)
    n += `${o}(${t[o].map(({ name: s, type: a }) => `${a} ${s}`).join(",")})`;
  return n;
}
function sf({ primaryType: e, types: t }, n = /* @__PURE__ */ new Set()) {
  const i = e.match(/^\w*/u)?.[0];
  if (n.has(i) || t[i] === void 0)
    return n;
  n.add(i);
  for (const o of t[i])
    sf({ primaryType: o.type, types: t }, n);
  return n;
}
function af({ types: e, name: t, type: n, value: r }) {
  if (e[n] !== void 0)
    return [
      { type: "bytes32" },
      rt(of({ data: r, primaryType: n, types: e }))
    ];
  if (n === "bytes")
    return [{ type: "bytes32" }, rt(r)];
  if (n === "string")
    return [{ type: "bytes32" }, rt(wr(r))];
  if (n.lastIndexOf("]") === n.length - 1) {
    const i = n.slice(0, n.lastIndexOf("[")), o = r.map((s) => af({
      name: t,
      type: i,
      types: e,
      value: s
    }));
    return [
      { type: "bytes32" },
      rt(oa(o.map(([s]) => s), o.map(([, s]) => s)))
    ];
  }
  return [{ type: n }, r];
}
function Nv(e) {
  const t = Vd(e, 0, 1);
  if (t === "0x04")
    return "eip7702";
  if (t === "0x03")
    return "eip4844";
  if (t === "0x02")
    return "eip1559";
  if (t === "0x01")
    return "eip2930";
  if (t !== "0x" && ea(t) >= 192)
    return "legacy";
  throw new Z0({ serializedType: t });
}
const Tc = "bitcoin-taproot-keypath-v1", Bc = "bip341-sighash-default-keypath", zv = ["bitcoin", "testnet", "signet", "regtest"], qo = 21000000n * 100000000n, cf = 137, lf = /^[0-9a-f]+$/, Dv = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", Uv = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", Mv = 734539939, Hv = 1;
function q(e) {
  throw new m("invalid_payload", e);
}
function $c(e, t) {
  return (!e || typeof e != "object" || Array.isArray(e)) && q(`${t} must be an object`), e;
}
function Oc(e, t, n) {
  const r = Object.keys(e).filter((o) => !t.includes(o));
  r.length > 0 && q(`${n} carries unsupported fields: ${r.sort().join(", ")}`);
  const i = t.filter((o) => !(o in e));
  i.length > 0 && q(`${n} is missing required fields: ${i.join(", ")}`);
}
function Xr(e, t, n) {
  return (typeof e != "string" || n !== void 0 && e.length !== n || e.length === 0 || !lf.test(e)) && q(`${t} must be lowercase hexadecimal${n === void 0 ? "" : ` of length ${n}`}`), e;
}
function Fv(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < n || e > r) && q(`${t} must be an integer in ${n}..${r}`), e;
}
function Ke(...e) {
  const t = new Uint8Array(e.reduce((r, i) => r + i.length, 0));
  let n = 0;
  for (const r of e)
    t.set(r, n), n += r.length;
  return t;
}
function hn(e, t, n, r) {
  return t + n > e.length && q(`transaction is truncated while reading ${r}`), [e.slice(t, t + n), t + n];
}
function Hr(e, t, n) {
  t >= e.length && q(`transaction is truncated before ${n}`);
  const r = e[t];
  return r >= 253 && q(`${n} must use a single-byte CompactSize`), [r, t + 1];
}
function ho(e) {
  return ((e[0] ?? 0) | (e[1] ?? 0) << 8 | (e[2] ?? 0) << 16 | (e[3] ?? 0) << 24) >>> 0;
}
function Kv(e) {
  let t = 0n;
  for (let n = 0; n < 8; n += 1) t |= BigInt(e[n] ?? 0) << BigInt(n * 8);
  return t;
}
function Vn(e) {
  return new Uint8Array([e & 255, e >>> 8 & 255, e >>> 16 & 255, e >>> 24 & 255]);
}
function Rc(e) {
  const t = new Uint8Array(8);
  let n = e;
  for (let r = 0; r < 8; r += 1)
    t[r] = Number(n & 0xffn), n >>= 8n;
  return t;
}
function uf(e) {
  if (e.length === 25 && e[0] === 118 && e[1] === 169 && e[2] === 20 && e[23] === 136 && e[24] === 172) return "p2pkh";
  if (e.length === 23 && e[0] === 169 && e[1] === 20 && e[22] === 135) return "p2sh";
  if (e.length === 22 && e[0] === 0 && e[1] === 20) return "p2wpkh";
  if (e.length === 34 && e[0] === 0 && e[1] === 32) return "p2wsh";
  if (e.length === 34 && e[0] === 81 && e[1] === 32) return "p2tr";
  q("transaction output uses an unsupported script template");
}
function jv(e) {
  (e.length === 0 || e.length % 2 || e.length > cf * 2 || !lf.test(e)) && q("unsignedTransaction must be lowercase hex, without 0x or whitespace, and at most 137 bytes");
  const t = Uint8Array.from(e.match(/../g), (P) => Number.parseInt(P, 16));
  (t.length < 4 || t[0] !== 2 || t[1] !== 0 || t[2] !== 0 || t[3] !== 0) && q("transaction version must be exactly 2");
  let n = 4;
  const [r, i] = Hr(t, n, "input count");
  n = i, r !== 1 && q("transaction must contain exactly one input");
  const [o, s] = hn(t, n, 32, "outpoint hash");
  n = s, o.every((P) => P === 0) && q("outpoint hash must not be null");
  const [a, c] = hn(t, n, 4, "outpoint index");
  n = c;
  const l = ho(a), [u, d] = Hr(t, n, "scriptSig length");
  n = d, u !== 0 && q("scriptSig must be empty");
  const [h, p] = hn(t, n, 4, "sequence");
  n = p;
  const g = ho(h);
  g !== 4294967293 && g !== 4294967294 && g !== 4294967295 && q("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff");
  const [w, f] = Hr(t, n, "output count");
  n = f, w !== 1 && w !== 2 && q("transaction must contain one or two outputs");
  const y = [], v = { p2pkh: 546n, p2sh: 540n, p2wpkh: 294n, p2wsh: 330n, p2tr: 330n };
  for (let P = 0; P < w; P += 1) {
    const [O, B] = hn(t, n, 8, `output ${P} amount`);
    n = B;
    const N = Kv(O);
    (N < 1n || N > qo) && q(`output ${P} amount is outside the Bitcoin money range`);
    const [z, b] = Hr(t, n, `output ${P} script length`);
    n = b;
    const [S, A] = hn(t, n, z, `output ${P} script`);
    n = A;
    const k = uf(S);
    z !== { p2pkh: 25, p2sh: 23, p2wpkh: 22, p2wsh: 34, p2tr: 34 }[k] && q(`output ${P} script length is not canonical for ${k}`), N < v[k] && q(`output ${P} is below the V1 ${k} dust floor`), y.push({ amountSats: N, scriptPubKey: S, kind: k });
  }
  const [x, I] = hn(t, n, 4, "locktime");
  n = I;
  const E = ho(x);
  E !== 0 && q("locktime must be exactly zero"), n !== t.length && q("transaction has trailing bytes");
  const T = y.reduce((P, O) => P + O.amountSats, 0n);
  return (T < 1n || T > qo) && q("output total is outside the Bitcoin money range"), { raw: t, prevoutHashLe: o, prevoutIndex: l, sequence: g, outputs: y, locktime: E };
}
function Vv(e, t) {
  const n = oe(new TextEncoder().encode(e));
  return oe(Ke(n, n, t));
}
function qv(e, t, n) {
  const r = Ke(e.prevoutHashLe, Vn(e.prevoutIndex)), i = Ke(new Uint8Array([n.length]), n), o = e.outputs.map((a) => Ke(Rc(a.amountSats), new Uint8Array([a.scriptPubKey.length]), a.scriptPubKey)), s = Ke(
    new Uint8Array([0, 0]),
    Vn(2),
    Vn(e.locktime),
    oe(r),
    oe(Rc(t)),
    oe(i),
    oe(Vn(e.sequence)),
    oe(Ke(...o)),
    new Uint8Array([0]),
    Vn(0)
  );
  return Vv("TapSighash", s);
}
function Gv(e) {
  return BigInt(`0x${Y(e)}`);
}
function Lc(e) {
  return lu(oe(e));
}
function Cc(e, t) {
  const n = Ke(new Uint8Array([e]), t), r = oe(oe(n)).slice(0, 4), i = Ke(n, r);
  let o = Gv(i), s = "";
  for (; o > 0n; ) {
    const a = Number(o % 58n);
    s = Dv[a] + s, o /= 58n;
  }
  for (const a of i) {
    if (a !== 0) break;
    s = `1${s}`;
  }
  return s;
}
function Wv(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >> i & s);
  return i > 0 && o.push(r << n - i & s), o;
}
function Zv(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1) i >>> o & 1 && (n ^= t[o]);
  }
  return n >>> 0;
}
function yo(e, t, n) {
  const r = e === "bitcoin" ? "bc" : e === "regtest" ? "bcrt" : "tb", i = [t, ...Wv(n, 8, 5)], o = [...r].map((l) => l.charCodeAt(0) >> 5).concat([0], [...r].map((l) => l.charCodeAt(0) & 31)), s = t === 0 ? Hv : Mv, a = Zv(o.concat(i, [0, 0, 0, 0, 0, 0])) ^ s, c = Array.from({ length: 6 }, (l, u) => a >>> 5 * (5 - u) & 31);
  return `${r}1${i.concat(c).map((l) => Uv[l]).join("")}`;
}
function Go(e, t) {
  const n = uf(t), r = e === "bitcoin";
  return n === "p2pkh" ? Cc(r ? 0 : 111, t.slice(3, 23)) : n === "p2sh" ? Cc(r ? 5 : 196, t.slice(2, 22)) : n === "p2wpkh" || n === "p2wsh" ? yo(e, 0, t.slice(2)) : yo(e, 1, t.slice(2));
}
function Xv(e) {
  const t = Lc(e), n = Ke(new Uint8Array([0, 20]), t);
  return [
    Ke(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    n,
    Ke(new Uint8Array([169, 20]), Lc(n), new Uint8Array([135]))
  ];
}
function wo(e, t) {
  return e.length === t.length && e.every((n, r) => n === t[r]);
}
function Fr(e) {
  const t = e / 100000000n, n = e % 100000000n;
  return n === 0n ? `${t}.00000000` : `${t}.${n.toString().padStart(8, "0")}`;
}
function df(e, t) {
  const n = e / BigInt(t), r = e % BigInt(t);
  if (r === 0n) return `${n.toString()} sat/vB`;
  const i = (r * 10000n / BigInt(t)).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n.toString()}.${i} sat/vB`;
}
function ff(e) {
  return Y(oe(oe(e.raw)).slice().reverse());
}
function Yv(e, t) {
  const n = t.transaction, r = H("bitcoin.network", { network: e.network }), i = [
    H("bitcoin.title"),
    ...t.highFeeWarning ? [H("bitcoin.highFeeWarning"), ""] : [],
    r,
    H("bitcoin.networkWarning"),
    H("bitcoin.input", { txid: Y(n.prevoutHashLe.slice().reverse()), vout: n.prevoutIndex }),
    H("bitcoin.inputAmount", { sats: e.prevout.amountSats, btc: Fr(BigInt(e.prevout.amountSats)) }),
    H("bitcoin.outputs")
  ];
  return n.outputs.forEach((o, s) => {
    const a = t.sameKeyOutputIndexes.includes(s);
    i.push(H("bitcoin.output", {
      index: s + 1,
      address: Go(e.network, o.scriptPubKey),
      sats: o.amountSats.toString(),
      btc: Fr(o.amountSats),
      role: H(a ? "bitcoin.sameKeyReturn" : "bitcoin.recipient")
    }));
  }), i.push(
    H("bitcoin.fee", { sats: t.feeSats.toString(), btc: Fr(t.feeSats), rate: df(t.feeSats, t.signedVsize) }),
    H("bitcoin.vsize", { vsize: t.signedVsize }),
    H(n.sequence === 4294967293 ? "bitcoin.rbfSignals" : "bitcoin.rbfDoesNotSignal"),
    H("bitcoin.netLeaving", { sats: t.netLeavingSats.toString(), btc: Fr(t.netLeavingSats) }),
    H("bitcoin.unsignedTxid", { txid: ff(n) }),
    H("bitcoin.outputKey", { key: Y(t.outputKeyXOnly) })
  ), i.join(`
`);
}
function Jv(e, t) {
  const n = $c(e, "signingContext");
  Oc(n, ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"], "bitcoin signingContext"), n.kind !== Tc && q("unsupported Bitcoin signing_context kind"), (typeof n.network != "string" || !zv.includes(n.network)) && q("bitcoin signing_context network is invalid");
  const r = Xr(n.unsignedTransaction, "unsignedTransaction");
  r.length > cf * 2 && q("unsignedTransaction exceeds the 137-byte V1 limit");
  const i = $c(n.prevout, "signingContext.prevout");
  Oc(i, ["amountSats", "scriptPubKey"], "signingContext.prevout");
  const o = Fv(i.amountSats, "prevout.amountSats", 1, Number(qo)), s = Xr(i.scriptPubKey, "prevout.scriptPubKey", 68);
  s.startsWith("5120") || q("prevout.scriptPubKey must be a lowercase P2TR output key script"), n.digestAlgorithm !== Bc && q("unsupported Bitcoin digest algorithm");
  const a = Xr(n.digest, "signingContext.digest", 64);
  return a !== t && q("Bitcoin signing_context digest must match the sign request digest exactly"), {
    kind: Tc,
    network: n.network,
    unsignedTransaction: r,
    prevout: { amountSats: o, scriptPubKey: s },
    digestAlgorithm: Bc,
    digest: a
  };
}
function pf(e, t, n) {
  const r = Jv(e, t), i = jv(r.unsignedTransaction), o = Uint8Array.from(r.prevout.scriptPubKey.match(/../g), (y) => Number.parseInt(y, 16)), s = o.slice(2);
  let a;
  if (n !== void 0) {
    const y = Xr(n, "signing public key", 66);
    a = Uint8Array.from(y.match(/../g), (x) => Number.parseInt(x, 16)), a[0] !== 2 && a[0] !== 3 && q("signing public key must be compressed secp256k1");
    const v = vg(y);
    wo(s, v) || q("prevout.scriptPubKey does not bind to the signing key's BIP86 output key");
  }
  if (a)
    for (const y of i.outputs)
      Xv(a).some((v) => wo(v, y.scriptPubKey)) && q("transaction sends to a same-key legacy or SegWit-v0 address this workflow cannot spend");
  const c = i.outputs.flatMap((y, v) => wo(y.scriptPubKey, o) ? [v] : []);
  c.length > 1 && q("transaction may contain at most one same-key P2TR return output");
  const l = i.outputs.reduce((y, v) => y + v.amountSats, 0n), u = BigInt(r.prevout.amountSats) - l;
  u <= 0n && q("transaction fee must be positive");
  const d = i.raw.length * 4 + 68, h = Math.ceil(d / 4);
  u > 10000n * BigInt(h) && q("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling");
  const p = Y(qv(i, BigInt(r.prevout.amountSats), o));
  p !== r.digest && q("Bitcoin signing_context digest does not match the independently computed BIP341 digest");
  const g = c.reduce((y, v) => y + i.outputs[v].amountSats, 0n), w = u > 100n * BigInt(h) || u * 100n > BigInt(r.prevout.amountSats) * 2n, f = BigInt(r.prevout.amountSats) - g;
  return {
    digest: p,
    display: Yv(r, {
      transaction: i,
      outputKeyXOnly: s,
      feeSats: u,
      signedVsize: h,
      sameKeyOutputIndexes: c,
      highFeeWarning: w,
      netLeavingSats: f
    }),
    facts: {
      network: r.network,
      spendingAddress: Go(r.network, o),
      inputTxid: Y(i.prevoutHashLe.slice().reverse()),
      inputVout: i.prevoutIndex,
      inputAmountSats: BigInt(r.prevout.amountSats),
      outputs: i.outputs.map((y, v) => ({
        address: Go(r.network, y.scriptPubKey),
        amountSats: y.amountSats,
        sameKey: c.includes(v)
      })),
      feeSats: u,
      feeRate: df(u, h),
      signedVsize: h,
      netLeavingSats: f,
      highFeeWarning: w,
      sequence: i.sequence,
      signalsRbf: i.sequence === 4294967293,
      locktime: i.locktime,
      unsignedTxid: ff(i),
      ...n !== void 0 ? { internalKeyXOnly: n.slice(2).toLowerCase() } : {},
      outputKeyXOnly: Y(s),
      unsignedTransaction: r.unsignedTransaction,
      prevoutScriptPubKey: r.prevout.scriptPubKey
    },
    transaction: i,
    outputKeyXOnly: s
  };
}
const Nc = `avault-agent-operation:v1
`;
function yn(e) {
  if (typeof e != "object" || e === null || Array.isArray(e)) throw new Error("signing context is malformed");
  return e;
}
function zc(e) {
  return e.startsWith("0x") ? e.toLowerCase() : `0x${e.toLowerCase()}`;
}
function go(e, t) {
  const n = zc(e), r = zc(t);
  if (n !== r)
    throw new Error("signing digest does not match verified context");
  return r.slice(2);
}
function Yr(e) {
  return typeof e == "bigint" ? e.toString() : typeof e == "string" || typeof e == "number" || typeof e == "boolean" ? String(e) : e == null ? "" : En(e);
}
function En(e) {
  if (typeof e == "bigint") return JSON.stringify(e.toString());
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(En).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${En(t[n])}`).join(",")}}`;
}
const Qv = Ab([
  "function approve(address spender,uint256 amount)",
  // A common extension, not an EIP-20 method: the amount is added to the current allowance.
  "function increaseAllowance(address spender,uint256 addedValue)",
  "function transfer(address to,uint256 amount)",
  "function transferFrom(address from,address to,uint256 amount)",
  "function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
  "function setApprovalForAll(address operator,bool approved)",
  "function safeTransferFrom(address from,address to,uint256 tokenId)",
  "function safeTransferFrom(address from,address to,uint256 tokenId,bytes data)"
]), ex = {
  approve: "ERC-20/ERC-721 approve",
  increaseAllowance: "ERC-20 increaseAllowance (extension)",
  transfer: "ERC-20 transfer",
  transferFrom: "ERC-20/ERC-721 transferFrom",
  permit: "ERC-20 permit",
  setApprovalForAll: "ERC-721 setApprovalForAll",
  safeTransferFrom: "ERC-721 safeTransferFrom",
  safeTransferFromWithData: "ERC-721 safeTransferFrom"
}, tx = new Map(
  Qv.map((e) => {
    const t = e.name === "safeTransferFrom" && e.inputs.length === 4 ? "safeTransferFromWithData" : e.name;
    return [T0(e), { call: t, item: e }];
  })
);
function nx(e) {
  return `${e.name}(${e.inputs.map((t) => t.type).join(",")})`;
}
function rx(e) {
  if (e == null || e === "" || e === "0x") return "0x";
  if (typeof e != "string" || !/^0x[0-9a-fA-F]*$/.test(e) || e.length % 2 !== 0)
    throw new Error("EVM transaction calldata is malformed");
  return e.toLowerCase();
}
const ix = /* @__PURE__ */ new Set([
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
]), ox = /* @__PURE__ */ new Set(["domain", "types", "primaryType", "message"]);
function mo(e, t, n) {
  const r = Object.keys(e).filter((i) => !t.has(i));
  if (r.length > 0)
    throw new Error(`${n} carries unsupported field: ${r.sort().join(", ")}`);
}
function hf(e) {
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
function sx(e, t) {
  if (t.chainId === void 0 || t.chainId === null || t.chainId === "")
    throw new Error("EVM transaction chain id is required");
  if (String(t.chainId) !== String(e.chainId))
    throw new Error("EVM transaction chain id mismatch");
}
function ax(e) {
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
function cx(e) {
  if (e.length < 10) return null;
  const t = tx.get(e.slice(0, 10));
  if (!t) return null;
  let n;
  try {
    if (n = N0(t.item.inputs, `0x${e.slice(10)}`), oa(t.item.inputs, n).toLowerCase() !== `0x${e.slice(10)}`) return null;
  } catch {
    return null;
  }
  return {
    call: t.call,
    signature: nx(t.item),
    arguments: t.item.inputs.map((r, i) => ({
      name: r.name ?? `arg${i}`,
      type: r.type,
      value: Yr(n[i])
    }))
  };
}
function lx(e, t, n) {
  const r = hf(e), i = rx(e.data), o = (c) => (t[c] === void 0 ? 0n : t[c]).toString(), s = e.to === void 0 || e.to === null || e.to === "" ? null : String(e.to), a = s === null ? null : cx(i);
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
function ux(e) {
  const t = [H("evm.title", { chainId: e.chainId })];
  if (e.blindSigning && t.push(H("sign.blindNotice")), t.push(
    H("evm.type", { type: e.type }),
    e.to === null ? H("evm.contractCreation") : H("evm.to", { to: e.to }),
    H("evm.value", { value: e.value }),
    H("evm.nonce", { value: e.nonce }),
    H("evm.gas", { value: e.gas })
  ), e.gasPrice !== void 0 && t.push(H("evm.gasPrice", { value: e.gasPrice })), e.maxFeePerGas !== void 0 && t.push(H("evm.maxFeePerGas", { value: e.maxFeePerGas })), e.maxPriorityFeePerGas !== void 0 && t.push(H("evm.maxPriorityFeePerGas", { value: e.maxPriorityFeePerGas })), e.decoded) {
    t.push(H("evm.call", { call: `${ex[e.decoded.call]} ${e.decoded.signature}` }));
    for (const n of e.decoded.arguments) t.push(`  ${n.name} (${n.type}): ${n.value}`);
  }
  return t.push(
    e.selector === null ? H("evm.selectorNone") : H("evm.selector", { selector: e.selector }),
    e.calldata === "0x" ? H("evm.calldataNone") : H("evm.calldata", { bytes: e.calldataBytes, calldata: e.calldata })
  ), t.join(`
`);
}
function dx(e) {
  Ui(e);
  const t = yn(e), n = {
    "evm-transaction": ["kind", "chainId", "unsignedTransaction", "digestAlgorithm", "digest"],
    "eip-712-typed-data": ["kind", "typedData", "digestAlgorithm", "digest"],
    "avault-agent-operation": ["kind", "canonicalPreimage", "digestAlgorithm", "digest"],
    "bitcoin-taproot-keypath-v1": ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"]
  }, r = Object.hasOwn(n, e.kind) ? n[e.kind] : void 0;
  if (!r) throw new Error("unsupported signing context");
  if (mo(t, new Set(r), "signing context"), typeof e.digest != "string" || !/^(?:0x)?[0-9a-fA-F]{64}$/.test(e.digest))
    throw new Error("signing context digest must be 32 bytes");
  switch (e.kind) {
    case "evm-transaction": {
      if (e.digestAlgorithm !== "keccak256") throw new Error("unsupported EVM digest algorithm");
      const i = yn(e.unsignedTransaction);
      mo(i, ix, "EVM transaction"), sx(e, i), hf(i);
      const o = ax(i), s = vv(o), a = go(e.digest, rt(s)), c = lx(i, o, s);
      if (Nv(s) !== c.type) throw new Error("EVM transaction type does not match its serialization");
      return {
        digest: a,
        challenge: Ce(a),
        display: ux(c),
        evm: c
      };
    }
    case "eip-712-typed-data": {
      if (e.digestAlgorithm !== "eip712") throw new Error("unsupported EIP-712 digest algorithm");
      const i = yn(e.typedData);
      mo(i, ox, "EIP-712 typed data");
      const o = yn(i.domain);
      if (o.chainId === void 0 || o.verifyingContract === void 0)
        throw new Error("EIP-712 domain must include chainId and verifyingContract");
      const s = go(e.digest, Ov(i));
      return {
        digest: s,
        challenge: Ce(s),
        display: [
          "EIP-712 typed data",
          `domain.chainId: ${Yr(o.chainId)}`,
          `domain.verifyingContract: ${Yr(o.verifyingContract)}`,
          `domain: ${En(o)}`,
          `types: ${En(yn(i.types))}`,
          `primaryType: ${Yr(i.primaryType)}`,
          `message: ${En(yn(i.message))}`
        ].join(`
`),
        typedData: i
      };
    }
    case "avault-agent-operation": {
      if (e.digestAlgorithm !== "avault-operation-hash-v1") throw new Error("unsupported avault digest algorithm");
      if (typeof e.canonicalPreimage != "string" || !e.canonicalPreimage.startsWith(Nc) || e.canonicalPreimage.slice(Nc.length).trim().length === 0)
        throw new Error("canonical preimage must start with avault-agent-operation:v1\\n followed by operation text; recreate the request");
      const i = go(e.digest, $w(oe(new TextEncoder().encode(e.canonicalPreimage))));
      return {
        digest: i,
        challenge: Ce(i),
        display: ["Avault agent operation", e.canonicalPreimage].join(`
`)
      };
    }
    case "bitcoin-taproot-keypath-v1": {
      const i = pf(e, e.digest);
      return {
        digest: i.digest,
        challenge: Ce(i.digest),
        display: i.display
      };
    }
    default:
      throw new Error("unsupported signing context");
  }
}
function Jr(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) return;
  const t = e.messageHex;
  if (typeof t == "string")
    try {
      mu(t);
    } catch {
      throw new m("invalid_payload", "signing messageHex exceeds the 1 MiB decoded limit");
    }
}
function qi(e) {
  if (Jr(e), Ui(e), !e || typeof e != "object" || Array.isArray(e)) throw new m("invalid_payload", "Solana signing context must be an object");
  const t = e;
  if (t.kind !== "solana-transaction-v1" || Object.keys(t).some((n) => n !== "kind" && n !== "messageHex") || typeof t.messageHex != "string" || t.messageHex.length === 0 || t.messageHex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(t.messageHex))
    throw new m("invalid_payload", "Solana context requires only kind and nonempty lowercase even messageHex; no digest alias");
  return { kind: "solana-transaction-v1", messageHex: t.messageHex };
}
function fx(e, t, n) {
  if (n.kind !== "keypair" || !n.publicKey) throw new Error("protected signing requires a keypair protected record");
  const r = Rn(n.curve);
  if (e === lr || t.kind === "solana-transaction-v1" || r === "ed25519") {
    if (e !== lr || t.kind !== "solana-transaction-v1" || r !== "ed25519")
      throw new m("invalid_payload", "Ed25519 signing requires an Ed25519 record, ed25519 scheme and Solana context together");
    qi(t);
  }
}
const yf = "11111111111111111111111111111111", wf = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", px = "SysvarRecentB1ockHashes11111111111111111111", hx = 2, yx = 4, wx = 3, gx = 12;
function gt(e) {
  return { decoded: null, undecodedReason: e };
}
function gf(e, t) {
  let n = 0n;
  for (let r = 7; r >= 0; r -= 1) n = n << 8n | BigInt(e[t + r]);
  return n.toString();
}
function fa(e) {
  const t = [];
  for (const n of e) {
    if (n.source !== "static") return null;
    t.push(n.address);
  }
  return t;
}
function mx(e, t) {
  const n = t.length >= 4 ? (t[0] | t[1] << 8 | t[2] << 16 | t[3] << 24) >>> 0 : -1;
  return n === hx && t.length === 12 ? bx(e, t) : n === yx && t.length === 4 ? vx(e) : gt("unsupported_instruction");
}
function bx(e, t) {
  if (e.length !== 2) return gt("unsupported_instruction");
  const n = fa(e);
  if (!n) return gt("unresolved_account");
  const [r, i] = n;
  return { decoded: { kind: "system-transfer", source: r, destination: i, lamports: gf(t, 4) }, undecodedReason: null };
}
function vx(e) {
  if (e.length !== 3) return gt("unsupported_instruction");
  const t = fa(e);
  if (!t) return gt("unresolved_account");
  const [n, r, i] = t;
  return r !== px ? gt("unsupported_instruction") : { decoded: { kind: "system-advance-nonce", nonceAccount: n, recentBlockhashesSysvar: r, nonceAuthority: i }, undecodedReason: null };
}
function xx(e, t) {
  const n = t.length === 9 && t[0] === wx && e.length === 3, r = t.length === 10 && t[0] === gx && e.length === 4;
  if (!n && !r) return gt("unsupported_instruction");
  const i = fa(e);
  if (!i) return gt("unresolved_account");
  const o = gf(t, 1);
  if (n) {
    const [u, d, h] = i;
    return { decoded: { kind: "spl-transfer", sourceTokenAccount: u, destinationTokenAccount: d, authority: h, amount: o }, undecodedReason: null };
  }
  const [s, a, c, l] = i;
  return {
    decoded: { kind: "spl-transfer-checked", sourceTokenAccount: s, mint: a, destinationTokenAccount: c, authority: l, amount: o, decimals: t[9] },
    undecodedReason: null
  };
}
function kx(e, t, n) {
  return e === yf ? mx(t, n) : e === wf ? xx(t, n) : gt("unsupported_program");
}
const Ex = 32, bo = 512, qn = 256, Dc = 8, mf = 3, bf = /* @__PURE__ */ new Map([
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
]), vf = /* @__PURE__ */ new Set(["1", "10", "8453", "42161", "11155111", "11155420", "84532", "421614"]);
function xf(e) {
  return bf.has(e) ? [] : ["review.note.unknownChain"];
}
function kf(e) {
  const t = bf.get(e);
  return t === void 0 ? e : `${e} · ${t}`;
}
function _i(e) {
  return typeof e == "object" && e !== null && !Array.isArray(e);
}
function Ef(e) {
  if (typeof e == "bigint") return e;
  if (typeof e != "string" && typeof e != "number" && typeof e != "boolean" && !Array.isArray(e)) return null;
  try {
    return BigInt(e);
  } catch {
    return null;
  }
}
function Ut(e, t) {
  if (/^u?int\d*$/.test(e)) {
    const n = Ef(t);
    return n === null ? JSON.stringify(t) ?? String(t) : n.toString();
  }
  return typeof t == "string" ? t : JSON.stringify(t) ?? String(t);
}
class Uc extends Error {
}
function Ii(e, t, n, r, i, o) {
  if (i > Ex) {
    o.omitted += 1;
    return;
  }
  const s = e[t];
  if (s !== void 0) {
    if (!Object.hasOwn(e, t) || !Array.isArray(s) || n === null || n === void 0) throw new Uc(r);
    const a = Object(n), c = new Set(s.map((l) => l.name));
    for (const l of s) Ii(e, l.type, a[l.name], r ? `${r}.${l.name}` : l.name, i + 1, o);
    if (typeof n == "object")
      for (const l of Object.keys(n)) c.has(l) || o.uncovered.push(r ? `${r}.${l}` : l);
    return;
  }
  if (t !== "bytes" && t !== "string" && t.endsWith("]")) {
    if (!Array.isArray(n)) throw new Uc(r);
    const a = t.slice(0, t.lastIndexOf("["));
    n.length === 0 && o.covered.push({ path: r, type: t, value: "[]" }), n.forEach((c, l) => Ii(e, a, c, `${r}[${l}]`, i + 1, o));
    return;
  }
  o.covered.push({ path: r, type: t, value: Ut(t, n) });
}
function Sx(e, t) {
  const n = /* @__PURE__ */ new Set(), r = (i) => {
    const o = /^\w*/.exec(i)[0];
    if (!(n.has(o) || !e[o])) {
      n.add(o);
      for (const s of e[o]) r(s.type);
    }
  };
  return r(t), n;
}
const Sf = [["token", "address"], ["amount", "uint160"], ["expiration", "uint48"], ["nonce", "uint48"]], Af = [["token", "address"], ["amount", "uint256"]], Ax = { primaryType: "Permit", types: { Permit: [["owner", "address"], ["spender", "address"], ["value", "uint256"], ["nonce", "uint256"], ["deadline", "uint256"]] } }, _x = { primaryType: "PermitSingle", types: { PermitSingle: [["details", "PermitDetails"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Sf } }, Ix = { primaryType: "PermitBatch", types: { PermitBatch: [["details", "PermitDetails[]"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: Sf } }, Px = {
  primaryType: "PermitTransferFrom",
  types: { PermitTransferFrom: [["permitted", "TokenPermissions"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: Af }
}, Tx = {
  primaryType: "PermitBatchTransferFrom",
  types: { PermitBatchTransferFrom: [["permitted", "TokenPermissions[]"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: Af }
};
function Gn(e, t, n) {
  if (t !== n.primaryType) return !1;
  const r = Sx(e, t), i = Object.keys(n.types);
  return r.size !== i.length || !i.every((o) => r.has(o)) ? !1 : i.every((o) => {
    const s = e[o], a = n.types[o];
    return Array.isArray(s) && s.length === a.length && s.every((c, l) => c.name === a[l][0] && c.type === a[l][1]);
  });
}
const Mc = (1n << 256n) - 1n, Bx = (1n << 160n) - 1n;
function vo(e, t, n, r, i) {
  const o = Ef(t), s = o === n;
  return {
    field: {
      label: e,
      ...i === void 0 ? {} : { index: i },
      value: o === null ? Ut("string", t) : o.toString(),
      ...s ? { shownAs: r, notes: ["review.note.exactInDetails"] } : { notes: ["review.note.rawTokenUnits"] }
    },
    isMax: s
  };
}
function Gt(e) {
  return typeof e == "string" ? e : Ut("string", e);
}
function xo(e, t, n) {
  return { label: e, value: Ut("uint256", t), time: "unix-seconds", notes: [n] };
}
function Hc(e) {
  const t = e.filter((o) => o.isMax), n = e.filter((o) => !o.isMax), r = n.slice(0, Math.max(0, mf - t.length)), i = n.length - r.length;
  return {
    rows: [
      ...[...t, ...r].flatMap((o) => o.rows()),
      ...i > 0 ? [{ label: "review.field.moreTokens", value: "", notes: [{ key: "review.note.moreEntriesInDetails", count: i }] }] : []
    ],
    maxima: t.length
  };
}
function Fc(e, t, n, r) {
  return t.maxima <= mf ? [e, ...t.rows, r] : [
    e,
    { label: "review.field.maxEntries", value: String(t.maxima), notes: [{ key: "review.note.ofEntries", count: n }] },
    r,
    ...t.rows
  ];
}
function $x(e, t, n) {
  const r = ["review.warning.tokenAuthorization"];
  if (Gn(e, t, Ax)) {
    const s = vo("review.field.permitAmount", n.value, Mc, "review.value.unlimited");
    return {
      kind: "review.kind.erc2612Permit",
      warnings: s.isMax ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: [
        { label: "review.field.spender", value: Gt(n.spender), elide: !0 },
        s.field,
        xo("review.field.permitDeadline", n.deadline, "review.note.permitDeadline")
      ],
      identity: [
        { label: "review.field.permitOwner", value: Gt(n.owner), elide: !0 },
        { label: "review.field.permitNonce", value: Ut("uint256", n.nonce) }
      ]
    };
  }
  const i = Gn(e, t, _x);
  if (i || Gn(e, t, Ix)) {
    const s = i ? [n.details] : Array.isArray(n.details) ? n.details : [], a = Hc(s.map((c, l) => {
      const u = _i(c) ? c : {}, d = vo("review.field.permitTokenAmount", u.amount, Bx, "review.value.unlimited", l + 1);
      return {
        isMax: d.isMax,
        rows: () => [
          d.field,
          { label: "review.field.permitToken", index: l + 1, value: Gt(u.token), elide: !0 },
          { label: "review.field.permitExpiration", index: l + 1, value: Ut("uint48", u.expiration), notes: ["review.note.permit2Expiration"] }
        ]
      };
    }));
    return {
      kind: "review.kind.permit2Allowance",
      warnings: a.maxima > 0 ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: Fc(
        { label: "review.field.spender", value: Gt(n.spender), elide: !0 },
        a,
        s.length,
        xo("review.field.signatureDeadline", n.sigDeadline, "review.note.signatureDeadline")
      ),
      identity: []
    };
  }
  const o = Gn(e, t, Px);
  if (o || Gn(e, t, Tx)) {
    const s = o ? [n.permitted] : Array.isArray(n.permitted) ? n.permitted : [], a = Hc(s.map((c, l) => {
      const u = _i(c) ? c : {}, d = vo("review.field.permitTokenAmount", u.amount, Mc, "review.value.maximumOneTime", l + 1);
      return {
        isMax: d.isMax,
        rows: () => [d.field, { label: "review.field.permitToken", index: l + 1, value: Gt(u.token), elide: !0 }]
      };
    }));
    return {
      kind: "review.kind.permit2Transfer",
      warnings: a.maxima > 0 ? ["review.warning.oneTimeTransfer", "review.warning.maximumOneTimeTransfer"] : ["review.warning.oneTimeTransfer"],
      summary: Fc(
        // The Permit2 contract signs msg.sender as the spender; this is the value in the message.
        { label: "review.field.spender", value: Gt(n.spender), elide: !0 },
        a,
        s.length,
        xo("review.field.permitDeadline", n.deadline, "review.note.signatureDeadline")
      ),
      identity: [{ label: "review.field.permitNonce", value: Ut("uint256", n.nonce) }]
    };
  }
  return null;
}
function _f(e) {
  return e.length <= bo ? { name: e } : { name: `${e.slice(0, bo / 2)} … ${e.slice(-bo / 4)}`, notes: ["review.note.pathShortened"] };
}
function ko(e) {
  const { name: t, notes: n } = _f(`${e.path} (${e.type})`);
  return { label: "review.field.typedField", name: t, value: e.value, ...n ? { notes: n } : {} };
}
function Ox(e, t) {
  const n = e, r = _i(n.domain) ? n.domain : {}, i = n.types, o = {
    EIP712Domain: nf({ domain: r }),
    ...i
  }, s = $s(t.publicKey).eth;
  if (!s) throw new Error("EIP-712 review requires a secp256k1 signing key");
  const a = { covered: [], uncovered: [], omitted: 0 };
  Ii(o, "EIP712Domain", r, "domain", 0, a);
  const c = a.covered, l = a.uncovered, u = new Set(o.EIP712Domain.map((P) => P.name)), d = (P, O) => {
    if (!Object.hasOwn(r, P)) return [];
    const B = u.has(P), N = o.EIP712Domain.find((b) => b.name === P), z = B && N ? Ut(N.type, r[P]) : Gt(r[P]);
    return [{
      label: O,
      value: P === "chainId" ? kf(z) : z,
      notes: [...P === "chainId" ? xf(z) : [], ...B ? [] : ["review.note.notInTypedHash"]]
    }];
  }, h = n.primaryType !== "EIP712Domain", p = _i(n.message) ? n.message : {}, g = { covered: [], uncovered: l, omitted: 0 };
  h ? Ii(o, n.primaryType, n.message, "", 0, g) : n.message !== void 0 && l.push("message");
  const w = g.covered, f = a.omitted + g.omitted;
  let y = null;
  try {
    y = h ? $x(o, n.primaryType, p) : null;
  } catch {
    y = null;
  }
  const v = w.slice(0, Dc).map((P) => P.type === "address" ? { ...ko(P), elide: !0 } : ko(P)), x = Math.max(0, w.length - Dc) + f;
  x > 0 && v.push({ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: x }] });
  const I = (P, O = 0) => {
    const B = Math.max(0, P.length - qn) + O;
    return [
      ...P.slice(0, qn).map(ko),
      ...B > 0 ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: B }] }] : []
    ];
  }, E = JSON.stringify(n, null, 2), T = [
    { title: "review.group.typedDomain", fields: I(c, a.omitted) },
    ...h ? [{ title: "review.group.typedMessage", fields: I(w, g.omitted) }] : [],
    ...l.length > 0 ? [{
      title: "review.group.notInTypedHash",
      fields: [
        ...l.slice(0, qn).map((P) => {
          const O = _f(P);
          return { label: "review.field.typedField", name: O.name, value: "", notes: ["review.note.notInTypedHash", ...O.notes ?? []] };
        }),
        ...l.length > qn ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: l.length - qn }] }] : []
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
      ...l.length > 0 ? [{ label: "review.field.notInTypedHash", value: "", notes: [{ key: "review.note.fieldsOutsideHash", count: l.length }] }] : [],
      ...d("chainId", "review.field.chainId"),
      ...d("name", "review.field.domainName"),
      ...d("verifyingContract", "review.field.verifyingContract").map((P) => ({ ...P, elide: !0 })),
      { label: "review.field.primaryType", value: n.primaryType, ...h ? {} : { notes: ["review.note.messageNotHashed"] } },
      ...y ? y.identity : []
    ],
    // Every hashed value is shown; nothing is blind.
    undecoded: !1,
    ...y ? { warnings: y.warnings } : {},
    details: T
  };
}
const Kc = 3, jc = 3, Rx = 3;
function If(e) {
  return e === yf ? ["review.note.systemProgram"] : e === wf ? ["review.note.tokenProgram"] : [];
}
function Gi(e) {
  return e.length / 2;
}
function Pf(e) {
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
function Lx(e) {
  return e.kind === "spl-transfer-checked" ? "TransferChecked" : "Transfer";
}
function Vc(e) {
  return {
    label: "review.field.undecodedInstruction",
    value: e.programId,
    notes: [{ key: "review.note.dataBytes", count: Gi(e.dataHex) }, "review.note.notDecoded"]
  };
}
function Cx(e, t) {
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
function qc(e) {
  return e === 0 ? [] : [{ label: "review.field.durableNonce", value: "", ...e > 1 ? { notes: [{ key: "review.note.nonceAdvances", count: e }] } : {} }];
}
function Nx(e) {
  const t = Pf(e).map((r) => r.label === "review.field.amount" ? r : { ...r, elide: !0 }), n = ["review.field.amount", "review.field.to", "review.field.toTokenAccount", "review.field.mint", "review.field.from", "review.field.fromTokenAccount"];
  return [...t].sort((r, i) => n.indexOf(r.label) - n.indexOf(i.label));
}
const Pi = (e) => e.kind !== "system-advance-nonce";
function zx(e) {
  if (e.instructions.length === 1) {
    const [s] = e.instructions;
    return s.decoded ? Pi(s.decoded) ? Nx(s.decoded) : qc(1) : [{ ...Vc(s), elide: !0 }];
  }
  let t = 0, n = 0, r = 0;
  const i = e.instructions.flatMap((s) => {
    const a = s.decoded;
    return a && Pi(a) ? (t += 1, t <= Kc ? Cx(a, t) : []) : a ? (n += 1, []) : (r += 1, r <= jc ? [{ ...Vc(s), elide: !0 }] : []);
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
    ...o("review.field.moreTransfers", "review.note.moreInDetails", t, Kc),
    ...o("review.field.moreUndecoded", "review.note.moreInstructionsInDetails", r, jc),
    ...qc(n)
  ];
}
function Dx(e) {
  return Pi(e) ? [
    { label: "review.field.instruction", value: Lx(e) },
    ...Pf(e),
    ...e.kind === "system-transfer" ? [] : [{ label: "review.field.authority", value: e.authority }]
  ] : [
    { label: "review.field.instruction", value: "AdvanceNonceAccount" },
    { label: "review.field.nonceAccount", value: e.nonceAccount },
    { label: "review.field.recentBlockhashesSysvar", value: e.recentBlockhashesSysvar },
    { label: "review.field.nonceAuthority", value: e.nonceAuthority }
  ];
}
function Ux(e) {
  return e.map((t) => t.index).join(", ");
}
function Mx(e) {
  const t = e.decoded, n = {
    label: "review.field.data",
    value: e.dataHex,
    notes: [{ key: "review.note.bytes", count: Gi(e.dataHex) }, ...t ? [] : ["review.note.notDecoded"]]
  };
  return {
    title: "review.group.instruction",
    index: e.index + 1,
    fields: [
      { label: "review.field.program", value: e.programId, notes: If(e.programId) },
      ...t ? Dx(t) : [],
      { label: "review.field.accountIndexes", value: Ux(e.accounts) },
      n
    ]
  };
}
function Hx(e) {
  const t = e.instructions.length === 1 ? e.instructions[0].decoded : null;
  return {
    kind: !t || !Pi(t) ? "review.kind.solanaTransaction" : t.kind === "system-transfer" ? "review.kind.solanaSystemTransfer" : "review.kind.solanaTokenTransfer",
    signer: { label: "review.field.signingKey", value: e.expectedSigner.address, elide: !0 },
    summary: zx(e),
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
            ...If(n.address)
          ]
        }))
      },
      ...e.instructions.map(Mx),
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
function Wo(e) {
  return vf.has(e.chainId) ? { label: "review.field.value", value: e.value, native: "eth" } : { label: "review.field.value", value: e.value, notes: ["review.note.wei"] };
}
function Tf(e) {
  return e.to === null ? { label: "review.field.contract", value: "", notes: ["review.note.contractCreation"] } : { label: "review.field.contract", value: e.to };
}
function Bf(e) {
  return (e.decoded?.arguments ?? []).map((t, n) => ({
    label: "review.field.argument",
    index: n + 1,
    name: `${t.name} (${t.type})`,
    value: t.value
  }));
}
function Fx(e) {
  if (e.decoded) {
    const n = { label: "review.field.call", value: e.decoded.signature }, r = Bf(e).map((i) => i.name?.endsWith(" (address)") ? { ...i, elide: !0 } : i);
    return r.length <= Rx ? [n, ...r] : [{ ...n, notes: [{ key: "review.note.argumentsInDetails", count: r.length }] }];
  }
  if (e.calldata === "0x") return [];
  const t = [{ key: "review.note.dataBytes", count: e.calldataBytes }, "review.note.notDecoded"];
  return e.to === null || e.selector === null ? [{ label: "review.field.initCode", value: "", notes: t }] : [{ label: "review.field.selector", value: e.selector, notes: t }];
}
const Gc = ((1n << 256n) - 1n).toString();
function Kx(e) {
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
    const i = r.value === Gc;
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
    const i = r.value === Gc;
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
function jx(e) {
  const t = { label: "review.field.chainId", value: kf(e.chainId), notes: xf(e.chainId) };
  if (e.to !== null && e.calldata === "0x")
    return { kind: "review.kind.evmTransaction", summary: [vf.has(e.chainId) ? { label: "review.field.nativeTransfer", value: e.value, native: "eth", unit: "symbol", to: e.to } : { label: "review.field.nativeTransfer", value: e.value, notes: ["review.note.wei"], to: e.to }, t] };
  const n = Kx(e);
  return n ? {
    kind: n.kind,
    ...n.warnings ? { warnings: n.warnings } : {},
    // The native value the transaction carries, as encoded, never assumed to be zero.
    summary: [...n.lead, { label: "review.field.tokenContract", value: e.to, elide: !0 }, Wo(e), t]
  } : { kind: "review.kind.evmTransaction", summary: [{ ...Tf(e), elide: !0 }, Wo(e), ...Fx(e), t] };
}
function Vx(e, t) {
  const n = $s(t.publicKey).eth;
  if (!n) throw new Error("EVM review requires a secp256k1 signing key");
  const r = (a, c) => c === void 0 ? [] : [{ label: a, value: c, notes: ["review.note.wei"] }], { kind: i, summary: o, warnings: s } = jx(e);
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
          Tf(e),
          Wo(e),
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
          ...Bf(e)
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
            notes: [{ key: "review.note.bytes", count: Gi(e.unsignedTransactionHex) - 1 }],
            raw: !0
          },
          { label: "review.field.digest", value: `0x${t.digest}` }
        ]
      }
    ]
  };
}
function qx(e, t) {
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
            notes: [{ key: "review.note.bytes", count: Gi(e.unsignedTransaction) }],
            raw: !0
          },
          { label: "review.field.prevoutScript", value: e.prevoutScriptPubKey }
        ]
      }
    ]
  };
}
const Gx = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function er(e) {
  let t = 0;
  for (; t < e.length && e[t] === 0; ) t += 1;
  let n = 0n;
  for (const i of e) n = n << 8n | BigInt(i);
  let r = "";
  for (; n > 0n; )
    r = Gx[Number(n % 58n)] + r, n /= 58n;
  return "1".repeat(t) + r;
}
class $f extends Error {
  code;
  constructor(t, n) {
    super(n), this.name = "SolanaMessageError", this.code = t;
  }
}
const Kr = 1232, Wc = 64, Wn = 32, Wx = 32, Zc = 128, Xc = 256;
function ye(e, t) {
  throw new $f(e, t);
}
function Yc(e) {
  let t = "";
  for (const n of e) t += n.toString(16).padStart(2, "0");
  return t;
}
function Zx(e) {
  return e < 128 ? 1 : e < 16384 ? 2 : 3;
}
function Xx(e, t, n = "compact-u16") {
  let r = 0;
  for (let i = 0; ; i += 1) {
    t + i >= e.length && ye("truncated", `message ends inside ${n}`);
    const o = e[t + i];
    o === 0 && i !== 0 && ye("invalid_compact_u16", `${n} is not minimally encoded`);
    const s = (o & 128) === 0;
    if (i === 2 && !s && ye("invalid_compact_u16", `${n} is longer than three bytes`), r |= (o & 127) << 7 * i, r > 65535 && ye("invalid_compact_u16", `${n} exceeds 65535`), s) return { value: r, length: i + 1 };
  }
}
class Yx {
  bytes;
  offset = 0;
  constructor(t) {
    this.bytes = t;
  }
  get remaining() {
    return this.bytes.length - this.offset;
  }
  u8(t) {
    return this.remaining < 1 && ye("truncated", `message ends before ${t}`), this.bytes[this.offset++];
  }
  take(t, n) {
    this.remaining < t && ye("truncated", `message ends inside ${n}`);
    const r = this.bytes.subarray(this.offset, this.offset + t);
    return this.offset += t, r;
  }
  compactU16(t) {
    const { value: n, length: r } = Xx(this.bytes, this.offset, t);
    return this.offset += r, n;
  }
}
function Of(e) {
  if (typeof e == "object" && e !== null) {
    for (const t of Object.values(e)) Of(t);
    Object.freeze(e);
  }
  return e;
}
function Jx(e, t) {
  e instanceof Uint8Array || ye("invalid_input", "message must be a byte array"), (!(t instanceof Uint8Array) || t.length !== Wn) && ye("invalid_input", "expected public key must be 32 bytes"), e.length > Kr - 1 - Wc && ye("packet_too_large", `message of ${e.length} bytes cannot fit a ${Kr}-byte transaction`);
  const n = new Uint8Array(e), r = er(new Uint8Array(t)), i = new Yx(n), o = i.u8("message prefix");
  let s = "legacy", a = o;
  if ((o & Zc) !== 0) {
    const B = o & ~Zc;
    B !== 0 && ye("unsupported_version", `Solana message version ${B} is not supported`), s = "v0", a = i.u8("required signature count");
  }
  const c = i.u8("read-only signed account count"), l = i.u8("read-only unsigned account count"), u = i.compactU16("account key count"), d = i.take(u * Wn, "account keys"), h = Array.from({ length: u }, (B, N) => er(d.subarray(N * Wn, (N + 1) * Wn))), p = er(i.take(Wx, "recent blockhash")), g = [];
  for (let B = i.compactU16("instruction count"); B > 0; B -= 1) {
    const N = i.u8("instruction program id index"), z = i.take(i.compactU16("instruction account count"), "instruction account indexes"), b = i.take(i.compactU16("instruction data length"), "instruction data");
    g.push({ programIdIndex: N, accountIndexes: z, data: b });
  }
  const w = [];
  if (s === "v0")
    for (let B = i.compactU16("address table lookup count"); B > 0; B -= 1) {
      const N = i.take(Wn, "address table key"), z = i.take(i.compactU16("writable lookup index count"), "writable lookup indexes"), b = i.take(i.compactU16("read-only lookup index count"), "read-only lookup indexes");
      w.push({ accountKey: N, writableIndexes: z, readonlyIndexes: b });
    }
  i.remaining !== 0 && ye("trailing_bytes", `${i.remaining} bytes follow the message`), a + l > u && ye("invalid_header", "header counts more signed and read-only unsigned accounts than the message carries"), c >= a && ye("invalid_header", "header leaves no writable signer to pay the fee");
  let f = 0, y = 0;
  for (const B of w)
    B.writableIndexes.length + B.readonlyIndexes.length === 0 && ye("invalid_address_table_lookup", "an address table lookup loads no account"), f += B.writableIndexes.length, y += B.readonlyIndexes.length;
  const v = u + f + y;
  v > Xc && ye("invalid_address_table_lookup", `message addresses ${v} accounts; the limit is ${Xc}`);
  for (const B of g) {
    (B.programIdIndex === 0 || B.programIdIndex >= u) && ye("invalid_program_id_index", `instruction program id index ${B.programIdIndex} is not a static non-payer key`);
    for (const N of B.accountIndexes)
      N >= v && ye("invalid_account_index", `instruction account index ${N} is outside the message's ${v} accounts`);
  }
  const x = Zx(a) + a * Wc + n.length;
  x > Kr && ye("packet_too_large", `signed transaction would be ${x} bytes; the limit is ${Kr}`);
  const I = h.indexOf(r);
  (I === -1 || I >= a) && ye("signer_not_required", "the expected key is not a required signer of this message");
  const E = h.map((B, N) => ({
    index: N,
    address: B,
    signer: N < a,
    writable: N < a ? N < a - c : N < u - l
  })), T = w.map((B) => ({
    accountKey: er(B.accountKey),
    writableIndexes: Array.from(B.writableIndexes),
    readonlyIndexes: Array.from(B.readonlyIndexes)
  })), P = (B) => {
    if (B < u) return { source: "static", ...E[B] };
    const N = B - u < f;
    let z = B - u - (N ? 0 : f);
    for (const [b, S] of T.entries()) {
      const A = N ? S.writableIndexes : S.readonlyIndexes;
      if (z < A.length)
        return { source: "lookup", index: B, lookupIndex: b, accountKey: S.accountKey, tableIndex: A[z], writable: N };
      z -= A.length;
    }
    throw new Error("account index was validated against the lookup tables");
  }, O = g.map((B, N) => {
    const z = h[B.programIdIndex], b = Array.from(B.accountIndexes, P);
    return {
      index: N,
      programIdIndex: B.programIdIndex,
      programId: z,
      accounts: b,
      dataHex: Yc(B.data),
      ...kx(z, b, B.data)
    };
  });
  return Of({
    version: s,
    messageHex: Yc(n),
    messageLength: n.length,
    transactionLength: x,
    header: { numRequiredSignatures: a, numReadonlySignedAccounts: c, numReadonlyUnsignedAccounts: l },
    feePayer: h[0],
    expectedSigner: { index: I, address: r, isFeePayer: I === 0 },
    staticAccountKeys: E,
    recentBlockhash: p,
    instructions: O,
    addressTableLookups: T,
    hasUndecodedContent: T.length > 0 || O.some((B) => B.decoded === null)
  });
}
function wn(e, t) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new m("invalid_payload", `${t} must be an object`);
  return e;
}
function jr(e, t) {
  if (typeof e != "string" || e.length === 0) throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function tr(e, t, n) {
  if (Object.keys(e).some((r) => !t.includes(r)))
    throw new m("invalid_payload", `${n} carries unsupported fields; script trees are not supported`);
}
function pa(e) {
  if (e && typeof e == "object") {
    for (const t of Object.values(e)) pa(t);
    Object.freeze(e);
  }
  return e;
}
function ha(e, t) {
  if (Jr(e), t && typeof t == "object") {
    const n = t.release;
    Jr(n), n && typeof n == "object" && Jr(n.signingContext);
  }
}
function Rf(e) {
  const t = wn(e, "sign request");
  ha(t.signingContext, t.context), Ui(t);
  const n = wn(structuredClone(t), "sign request");
  tr(n, ["material", "scheme", "signingContext", "context"], "sign request");
  const r = wn(n.material, "material");
  tr(r, ["name", "envelope"], "material");
  const i = wn(r.envelope, "envelope");
  tr(i, ["ciphertext", "nonce", "wrap_meta"], "envelope");
  const o = Ws(n.context);
  tr(wn(o, "context"), ["v", "purpose", "requestId", "grantId", "display", "release", "agent", "expiresAt", "signature"], "sign context");
  let s;
  try {
    s = bs(n.scheme);
  } catch {
    throw new m("invalid_payload", "unsupported signing scheme");
  }
  const a = wn(n.signingContext, "signingContext");
  return a.kind === "solana-transaction-v1" && qi(a), pa({
    material: {
      name: jr(r.name, "material.name"),
      envelope: {
        ciphertext: jr(i.ciphertext, "envelope.ciphertext"),
        nonce: jr(i.nonce, "envelope.nonce"),
        wrap_meta: jr(i.wrap_meta, "envelope.wrap_meta")
      }
    },
    scheme: s,
    signingContext: a,
    context: o
  });
}
function Ot() {
  throw new m("invalid_sign_binding", "signing approval is missing or does not match its signed binding; update Avibe and recreate the request");
}
function Lf(e, t, n) {
  ha(e.signingContext, e.context), Uo({ context: e.context, expectedPurpose: "sign", secret: { name: e.material.name, kind: "keypair" } });
  const r = e.context.release;
  (!r || Array.isArray(r)) && Ot();
  const i = t.kind === "message";
  if (!i && (t.kind !== "digest" || !/^[0-9a-f]{64}$/.test(t.digest)) && Ot(), tr(r, ["name", "scheme", i ? "messageHex" : "digest", "publicKey", "signingContext"], "sign context.release"), i) {
    const o = qi(e.signingContext);
    (t.messageHex !== o.messageHex || r.messageHex !== t.messageHex || e.scheme !== "ed25519" || !/^[0-9a-f]{64}$/.test(n) || r.publicKey !== n) && Ot();
  } else (e.scheme === "ed25519" || e.signingContext.kind === "solana-transaction-v1" || r.digest !== t.digest) && Ot();
  (r.name !== e.material.name || r.scheme !== e.scheme || typeof r.publicKey != "string" || r.publicKey.toLowerCase() !== n.toLowerCase() || !r.signingContext || Ne(r.signingContext) !== Ne(e.signingContext)) && Ot();
}
function Qx(e) {
  return ha(e.signingContext, e.context), oe(new TextEncoder().encode(Mi(e.context)));
}
function e1(e, t) {
  if (bs(e), e === "ed25519") throw new Error("Ed25519 exact-message signing is reviewed structurally and has no prose description");
  if (!/^(02|03)[0-9a-fA-F]{64}$/.test(t)) throw new Error("signing public key must be compressed secp256k1");
  return e === ms ? `BIP340 raw Schnorr (untweaked internal key)
internal key: ${t.slice(2).toLowerCase()}` : e === ur ? H("bitcoin.signingIdentity", { key: t.slice(2).toLowerCase() }) : `${e === Ci ? "ECDSA secp256k1 recoverable" : "ECDSA secp256k1 DER"}
public key: ${t.toLowerCase()}`;
}
function t1(e, t) {
  const n = qi(e.signingContext);
  Lf(e, { kind: "message", messageHex: n.messageHex }, t);
  let r;
  try {
    r = Jx(ws(n.messageHex), Ce(t));
  } catch (i) {
    throw i instanceof $f ? new m("invalid_payload", `Solana message cannot be signed (${i.code}): ${i.message}`) : i;
  }
  return r.messageHex !== n.messageHex && Ot(), { payload: { kind: "message", messageHex: r.messageHex }, review: Hx(r) };
}
function n1(e, t) {
  const n = e.signingContext;
  n.kind === "solana-transaction-v1" && Ot();
  let r = dx(n);
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    if (e.scheme !== ur)
      throw new m("invalid_payload", "Bitcoin Taproot contexts require the BIP341 key-path signing scheme");
  } else {
    if (e.scheme === ur)
      throw new m("invalid_payload", "the BIP341 key-path signing scheme requires a Bitcoin Taproot context");
    if (n.kind !== "avault-agent-operation" && e.scheme !== Ci && e.scheme !== gs)
      throw new m("invalid_payload", "EVM signing contexts require an ECDSA scheme");
  }
  let i;
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    const o = pf(n, r.digest, t);
    r = { digest: o.digest, challenge: r.challenge, display: o.display }, i = o.facts;
  }
  return Lf(e, { kind: "digest", digest: r.digest }, t), {
    payload: { kind: "digest", digest: r.digest },
    display: `${e1(e.scheme, t)}
scheme: ${e.scheme}
${r.display}`,
    ...r.evm ? { review: Vx(r.evm, { publicKey: t, scheme: e.scheme, digest: r.digest }) } : {},
    ...i ? { review: qx(i, { scheme: e.scheme, digest: r.digest }) } : {},
    ...r1(r.typedData, { publicKey: t, scheme: e.scheme, digest: r.digest })
  };
}
function r1(e, t) {
  if (e === void 0) return {};
  try {
    const n = Ox(e, t);
    return Qu(JSON.parse(JSON.stringify(n))), { review: n };
  } catch {
    return {};
  }
}
function Cf(e) {
  const t = Pn(e.material.envelope), n = Es(e.material.name, t.recordMetadata);
  fx(e.scheme, e.signingContext, n);
  const r = n.publicKey, i = e.signingContext.kind === "solana-transaction-v1" ? t1(e, r) : n1(e, r);
  return { unpacked: t, recordContext: n, content: i };
}
function i1(e) {
  const t = Rf(e), { unpacked: n, recordContext: r, content: i } = Cf(t), o = Qx(t);
  return pa({ request: t, verified: i, unpacked: n, recordContext: r, challenge: [...o] });
}
async function Nf(e, t, n) {
  const r = Rf(e.request), i = Cf(r);
  (!e.verified || Ne(e.verified.payload) !== Ne(i.content.payload)) && Ot();
  const o = await _s(n, t);
  return Ei({ context: r.context, rootMetadata: o, expectedPurpose: "sign" }), Si([r.context]), { request: r, ...i };
}
async function o1(e, t, n) {
  return (await Nf(e, t, n)).content;
}
async function s1(e, t, n) {
  const { request: r, content: i, unpacked: o, recordContext: s } = await Nf(e, t, n);
  return i.payload.kind === "message" ? cg(o.sealed, t, s, i.payload.messageHex) : sg(o.sealed, t, s, i.payload.digest, r.scheme);
}
function zf(e, t = e.label) {
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
function a1(e) {
  const { challenge: t, ...n } = e;
  return {
    ...n,
    ...t ? { challenge: Q(t) } : {}
  };
}
function Zo(e, t) {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be an object`);
  return e;
}
function en(e, t) {
  if (typeof e == "string" && e.length > 0) return e;
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be a text spec`);
  const n = e;
  if (typeof n.text == "string") return be(n.text);
  if (typeof n.key == "string") {
    const r = n.params;
    if (r !== void 0 && (typeof r != "object" || r === null || Array.isArray(r)))
      throw new m("invalid_payload", `${t}.params must be an object`);
    return {
      key: n.key,
      ...r !== void 0 ? { params: r } : {}
    };
  }
  throw new m("invalid_payload", `${t} must be a text spec`);
}
function c1(e) {
  const t = Zo(e, "details");
  if (!Array.isArray(t.rows)) throw new m("invalid_payload", "details must contain rows");
  const n = [];
  for (const [r, i] of t.rows.entries()) {
    const o = Zo(i, `details.rows[${r}]`), s = o.variant;
    if (s !== void 0 && s !== "text" && s !== "command" && s !== "mono" && s !== "chips")
      throw new m("invalid_payload", `details.rows[${r}].variant is invalid`);
    const a = en(o.label, `details.rows[${r}].label`);
    if (s === "chips") {
      if (!Array.isArray(o.values)) throw new m("invalid_payload", `details.rows[${r}].values must be an array`);
      const c = o.tone;
      if (c !== void 0 && c !== "default" && c !== "warning")
        throw new m("invalid_payload", `details.rows[${r}].tone is invalid`);
      const l = c === "default" || c === "warning" ? c : void 0;
      n.push({
        label: a,
        values: o.values.map((u, d) => en(u, `details.rows[${r}].values[${d}]`)),
        variant: "chips",
        ...l !== void 0 ? { tone: l } : {}
      });
      continue;
    }
    n.push({
      label: a,
      value: en(o.value, `details.rows[${r}].value`),
      ...s !== void 0 ? { variant: s } : {}
    });
  }
  return { rows: n };
}
function l1(e) {
  try {
    return Qu(e);
  } catch (t) {
    throw new m("invalid_payload", t instanceof Error ? t.message : "signing review is malformed");
  }
}
function u1(e) {
  const t = Zo(e, "authorization prompt");
  if (t.challenge !== void 0 && (typeof t.challenge != "string" || t.challenge.length === 0))
    throw new m("invalid_payload", "prompt.challenge must be a non-empty string");
  if (t.reviewSession !== void 0 && typeof t.reviewSession != "string")
    throw new m("invalid_payload", "prompt.reviewSession must be a string");
  const n = t.challenge === void 0 ? void 0 : pe(t.challenge);
  return {
    title: en(t.title, "prompt.title"),
    subtitle: en(t.subtitle, "prompt.subtitle"),
    ...t.body !== void 0 ? { body: en(t.body, "prompt.body") } : {},
    ...t.details !== void 0 ? { details: c1(t.details) } : {},
    ...t.review !== void 0 ? { review: l1(t.review) } : {},
    ...t.reviewSession !== void 0 ? { reviewSession: t.reviewSession } : {},
    ...n ? { challenge: n } : {},
    label: en(t.label, "prompt.label")
  };
}
const Jc = 320, Qc = 220, d1 = 6e4, f1 = 1e3;
function p1(e, t = {}) {
  if (e.uiShowPending) return { ok: !1, code: "sandbox_not_visible", detail: "ui show is still pending" };
  if (!e.documentVisible) return { ok: !1, code: "sandbox_not_visible", detail: "document is not visible" };
  if ((t.requireFocus ?? !0) && !e.documentFocused)
    return { ok: !1, code: "sandbox_not_visible", detail: "document is not focused" };
  if (e.frameWidth < Jc || e.frameHeight < Qc)
    return { ok: !1, code: "sandbox_not_visible", detail: "sandbox frame is too small" };
  if (!e.visibleByIntersectionObserver || e.intersectionRatio < 0.99)
    return { ok: !1, code: "sandbox_not_visible", detail: "sandbox frame is not fully visible" };
  const n = [];
  if (e.embedded) {
    const r = e.parent;
    r ? (r.ageMs > d1 && n.push("parent frame visibility is stale"), (r.frameWidth < Jc || r.frameHeight < Qc) && n.push("parent frame is too small"), (!r.visibleByIntersectionObserver || r.intersectionRatio < 0.99) && n.push("parent frame is not fully visible"), (r.opacity < 0.99 || !r.pointerEvents) && n.push("parent frame is visually occluded")) : n.push("parent frame visibility is not attested");
  }
  return n.length > 0 ? { ok: !0, warnings: n } : { ok: !0 };
}
function el(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Wt(e) {
  if (typeof e == "number") return Number.isFinite(e) ? e : void 0;
  if (typeof e != "string" || e.trim() === "") return;
  const t = Number(e);
  return Number.isFinite(t) ? t : void 0;
}
function gn(e) {
  const t = Wt(e);
  if (t !== void 0) return t;
  if (typeof e != "string" || e.trim() === "") return;
  const n = Date.parse(e);
  return Number.isFinite(n) ? n : void 0;
}
function Eo(e) {
  return typeof e == "boolean" ? e : void 0;
}
function h1(e) {
  if (typeof e == "boolean") return e;
  if (typeof e == "string")
    return e.toLowerCase() !== "none";
}
function y1(e) {
  if (!e) return;
  const t = el(e.value);
  if (!t) return;
  const n = el(t.frame) ?? t, r = Wt(n.frameWidth) ?? Wt(n.width), i = Wt(n.frameHeight) ?? Wt(n.height), o = Wt(n.intersectionRatio), s = Eo(n.visibleByIntersectionObserver) ?? Eo(n.isVisible) ?? Eo(n.visible), a = Wt(n.opacity), c = h1(n.pointerEvents), l = gn(t.sampledAt) ?? gn(t.measuredAt) ?? gn(t.timestamp) ?? gn(n.sampledAt) ?? gn(n.measuredAt) ?? gn(n.timestamp);
  if (r === void 0 || i === void 0 || o === void 0 || s === void 0 || a === void 0 || c === void 0 || l === void 0)
    return;
  const u = Date.now();
  if (!(l > u + f1))
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
function Df(e) {
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
    parent: y1(e.parentSurface?.())
  }), c = (g) => {
    const w = a(), f = p1(w, { requireFocus: g });
    if (!f.ok) throw new m(f.code, f.detail, !0);
    if (e.requireParentSurface && w.embedded && (f.warnings?.length ?? 0) > 0)
      throw new m("sandbox_not_visible", f.warnings[0], !0);
    for (const y of f.warnings ?? []) console.warn(`[vault-sandbox] Confirm surface advisory: ${y}`);
  }, l = () => c(!0), u = new Promise((g, w) => {
    o = g, s = w;
  }), d = () => {
    if (!(i || r)) {
      i = !0;
      try {
        c(!1), o();
      } catch (g) {
        s(g);
      }
    }
  }, h = typeof IntersectionObserver > "u" ? null : new IntersectionObserver(
    (g) => {
      const w = g[0], f = w?.isVisible;
      n = w ? {
        ratio: w.intersectionRatio,
        visible: f === void 0 ? w.intersectionRatio >= 0.99 : f === !0
      } : { ratio: 0, visible: !1 }, d();
    },
    {
      threshold: [0, 0.99, 1],
      trackVisibility: !0,
      delay: 100
    }
  );
  h?.observe(t);
  const p = setTimeout(d, 250);
  return {
    ready: u,
    assertCurrent: l,
    dispose: () => {
      r || (r = !0, clearTimeout(p), h?.disconnect());
    }
  };
}
function Uf(e) {
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
async function w1(e) {
  let t = new TextEncoder().encode(e.value);
  try {
    const n = { name: e.name, kind: "static" }, r = await ku(t, e.vmk, n);
    return { envelope: Su(r, e.wrapMeta, n) };
  } finally {
    t?.fill(0), t = void 0;
  }
}
async function g1(e) {
  const t = Rn(e.curve), n = ig(t);
  let r = n.privateKey;
  try {
    const i = {
      name: e.name,
      kind: "keypair",
      publicKey: n.publicKey,
      ...t === "ed25519" ? { curve: t } : {}
    }, o = await ku(r, e.vmk, i);
    return {
      envelope: Su(o, e.wrapMeta, i),
      publicKey: n.publicKey,
      addresses: t === "ed25519" ? { solana: lg(n.publicKey) } : $s(n.publicKey)
    };
  } finally {
    r?.fill(0), r = void 0;
  }
}
function Mf(e) {
  return (e?.kind ?? "static") === "static";
}
function m1(e, t) {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", "approveRelease item must be an object");
  const n = e;
  return {
    material: t(n.material),
    context: Ws(n.context)
  };
}
function Hf(e) {
  const t = e[0]?.context.display;
  if (!t) throw new m("invalid_payload", "approveRelease requires at least one item");
  if (e[0].context.purpose !== "agent-deliver") throw new m("invalid_context", "approveRelease only accepts agent-deliver contexts");
  const n = gc(e[0].context), r = tl(e[0].context);
  for (const s of e) {
    if (s.context.purpose !== "agent-deliver") throw new m("invalid_context", "approveRelease only accepts agent-deliver contexts");
    if (gc(s.context) !== n)
      throw new m("invalid_context", "approveRelease batch contexts must share one signed display block");
    if (tl(s.context) !== r)
      throw new m("invalid_context", "approveRelease batch contexts must share one signed recipient");
  }
  const i = new Set(t.secrets.map((s) => `${s.name}:${s.kind}`));
  for (const s of e) {
    const { recordMetadata: a } = Pn(s.material.envelope), c = a?.kind ?? "static";
    if (!i.has(`${s.material.name}:${c}`))
      throw new m("invalid_context", "signed display block does not cover every release item");
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
function tl(e) {
  if (!e.grantId) throw new m("invalid_context", "agent delivery context is missing grantId");
  if (!e.agent) throw new m("invalid_context", "agent delivery context is missing agent key");
  return Ne({ agent: e.agent, grantId: e.grantId });
}
async function b1(e) {
  if (e.items.length === 0) throw new m("invalid_payload", "approveRelease requires at least one item");
  const t = Hf(e.items), n = await _s(e.wrapMeta, e.vmk), r = e.items.map((a) => a.context);
  for (const a of e.items)
    Ei({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: e.now
    });
  Si(r, e.now);
  const i = await Nm(r);
  await e.confirm({
    ...t,
    challenge: i
  });
  const o = e.now ?? Date.now();
  for (const a of e.items)
    Ei({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: o
    });
  e.onApprovalAccepted?.(o), Si(r, o);
  const s = [];
  for (const a of e.items) {
    const { sealed: c, recordMetadata: l } = Pn(a.material.envelope);
    if (!Mf(l)) throw new m("invalid_payload", "approveRelease requires static protected records");
    s.push(
      await rg(
        c,
        e.vmk,
        Cm(a.context),
        Es(a.material.name, l),
        await Lm(a.context, a.material.name)
      )
    );
  }
  return e.consumeReplayIds !== !1 && await Hi(r, o), { blindBoxes: s };
}
const nl = "vault-signing-policy-downgrade", v1 = `avibe:vault:signing-policy-downgrade:v1
`, vn = 65536, x1 = /^[A-Za-z0-9_-]{1,80}$/, rl = ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"];
function j(e) {
  throw new m("policy_context_invalid", e);
}
function Rt(e, t, n) {
  (e === null || typeof e != "object" || Array.isArray(e) || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null) && j(`${n} must be an object`);
  const r = e;
  return (Object.keys(r).length !== t.length || !t.every((i) => Object.hasOwn(r, i))) && j(`${n} has missing or unknown fields`), r;
}
function Xo(e, t) {
  return (typeof e != "string" || e.length === 0) && j(`${t} must be a nonempty string`), e;
}
function So(e, t) {
  return (typeof e != "string" || !x1.test(e)) && j(`${t} is not a valid ID`), e;
}
function Lt(e, t, n) {
  (typeof e != "string" || e.length > 4 * Math.ceil(n / 3) || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) && j("noncanonical policy base64");
  const r = pe(e);
  return (r.length < t || r.length > n || Q(r) !== e) && j("policy base64 length or padding is invalid"), r;
}
const k1 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function E1(e) {
  let t = 0n;
  for (const i of e) {
    const o = k1.indexOf(i);
    o < 0 && j("invalid Base58 address"), t = t * 58n + BigInt(o);
  }
  const n = [];
  for (; t > 0n; )
    n.push(Number(t & 255n)), t >>= 8n;
  const r = /^1*/.exec(e)[0].length;
  return Uint8Array.from([...new Array(r).fill(0), ...n.reverse()]);
}
const il = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function ol(e, t, n) {
  (!e.startsWith(`bc1${il[t]}`) || e.length !== (n === 20 ? 42 : 62)) && j("invalid mainnet witness address");
  const r = [...e.slice(4, -6)].map((c) => {
    const l = il.indexOf(c);
    return l < 0 && j("invalid Bech32 address character"), l;
  });
  let i = 0, o = 0;
  const s = [];
  for (const c of r)
    i = (i << 5 | c) & 65535, o += 5, o >= 8 && (o -= 8, s.push(i >>> o & 255));
  (o >= 5 || (i & (1 << o) - 1) !== 0 || s.length !== n) && j("noncanonical witness address padding");
  const a = Uint8Array.from(s);
  return Ro("bc", t, a) !== e && j("invalid witness address checksum"), a;
}
function S1(e, t) {
  if (e === null) return null;
  const n = Xo(e, `addresses.${t}`);
  if (n.length > 128 && j("policy address is too long"), t === "eth")
    (!/^0x[0-9a-fA-F]{40}$/.test(n) || `0x${Ou(n.slice(2))}` !== n) && j("noncanonical EIP-55 address");
  else if (t === "btc_segwit") ol(n, 0, 20);
  else if (t === "btc_taproot") ol(n, 1, 32);
  else {
    const r = E1(n);
    t === "solana" ? (r.length !== 32 || er(r) !== n) && j("noncanonical Solana address") : (r.length !== 25 || r[0] !== 0 || Bu(r.slice(0, 21)) !== n) && j("invalid mainnet P2PKH address");
  }
  return n;
}
function A1(e) {
  return v1 + Ne(e);
}
function Ff(e) {
  return oe(new TextEncoder().encode(A1(e)));
}
function Mt(e, t = Date.now()) {
  const n = Date.parse(e.expiresAt) - t;
  if (Number.isFinite(n) || j("invalid policy expiry"), n <= 0) throw new m("policy_context_expired", "policy context has expired", !0);
  if (n > 15e4) throw new m("policy_context_clock_skew", "check this device's clock and retry", !0);
  return n;
}
function Kf(e, t, n = Date.now()) {
  try {
    const r = JSON.stringify(e);
    (r === void 0 || r.length > vn) && j("policy context exceeds its size bound"), Ui(e);
    const i = Rt(e, ["v", "purpose", "challengeId", "nonce", "expiresAt", "rpId", "origin", "target", "change", "factors"], "context");
    (i.v !== 1 || i.purpose !== nl) && j("unsupported policy context version or purpose"), (i.origin !== t.origin || i.rpId !== t.hostname) && j("policy origin or RP does not match this sandbox");
    const o = Xo(i.expiresAt, "expiresAt"), s = Date.parse(o);
    (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(o) || !Number.isFinite(s) || new Date(s).toISOString() !== o) && j("policy expiry must be an exact UTC millisecond date"), Lt(i.nonce, 32, 32);
    const a = Rt(i.target, ["secretId", "name", "protection", "signerKind", "account", "accountName", "addresses", "identitySha256"], "target");
    a.protection !== "standard" && a.protection !== "protected" && j("invalid protection tier"), a.signerKind !== "local" && a.signerKind !== "hd-seed" && j("unsupported signer kind"), (typeof a.identitySha256 != "string" || !/^[0-9a-f]{64}$/.test(a.identitySha256)) && j("invalid identity digest");
    const c = Rt(a.addresses, rl, "addresses"), l = Object.fromEntries(rl.map((w) => [w, S1(c[w], w)]));
    if (a.signerKind === "hd-seed")
      (a.protection !== "standard" || typeof a.account != "number" || !Number.isSafeInteger(a.account) || a.account < 0 || a.account > 19) && j("invalid shared HD account"), a.accountName !== null && (typeof a.accountName != "string" || [...a.accountName].length < 1 || [...a.accountName].length > 80) && j("invalid HD account name"), (l.eth === null || l.btc_taproot === null || l.solana === null || l.btc_legacy !== null || l.btc_segwit !== null) && j("invalid shared HD address set");
    else {
      (a.account !== null || a.accountName !== null) && j("ordinary keys cannot carry an HD account");
      const w = l.eth !== null && l.btc_legacy !== null && l.btc_segwit !== null && l.btc_taproot !== null && l.solana === null, f = l.solana !== null && l.eth === null && l.btc_legacy === null && l.btc_segwit === null && l.btc_taproot === null;
      !w && !f && j("invalid ordinary key address set");
    }
    const u = Rt(i.change, ["field", "from", "to", "revision"], "change");
    (u.field !== "always_ask" || u.from !== !0 || u.to !== !1 || typeof u.revision != "string" || !/^(?:legacy|[0-9a-f]{32})$/.test(u.revision)) && j("only true-to-false always_ask changes are supported"), (!Array.isArray(i.factors) || i.factors.length === 0) && j("policy factors must be nonempty");
    const d = /* @__PURE__ */ new Set();
    let h = "";
    const p = i.factors.map((w) => {
      const f = Rt(w, ["factorId", "credentialId"], "factor"), y = So(f.factorId, "factorId");
      Lt(f.credentialId, 1, 1023);
      const v = f.credentialId;
      return (y <= h || d.has(v)) && j("policy factors must have unique credentials and sorted unique IDs"), h = y, d.add(v), { factorId: y, credentialId: v };
    }), g = {
      v: 1,
      purpose: nl,
      challengeId: So(i.challengeId, "challengeId"),
      nonce: i.nonce,
      expiresAt: o,
      rpId: i.rpId,
      origin: i.origin,
      target: {
        secretId: So(a.secretId, "secretId"),
        name: Xo(a.name, "name"),
        protection: a.protection,
        signerKind: a.signerKind,
        account: a.account,
        accountName: a.accountName,
        addresses: l,
        identitySha256: a.identitySha256
      },
      change: { field: "always_ask", from: !0, to: !1, revision: u.revision },
      factors: p
    };
    return new TextEncoder().encode(Ne(g)).length > vn && j("policy context exceeds its UTF-8 size bound"), Mt(g, n), g;
  } catch (r) {
    if (r instanceof m) throw r;
    j("policy context is not canonical JSON");
  }
}
function _1(e, t) {
  return Kf(Rt(e, ["context"], "payload").context, t);
}
function I1(e) {
  const t = Mt(e), n = Math.min(12e4, Math.floor(t));
  if (n <= 0) throw new m("policy_context_expired", "policy context has expired", !0);
  return {
    challenge: new Uint8Array(Ff(e)).buffer,
    rpId: e.rpId,
    userVerification: "required",
    timeout: n,
    allowCredentials: e.factors.map((r) => ({ type: "public-key", id: new Uint8Array(Lt(r.credentialId, 1, 1023)).buffer }))
  };
}
function sl(e) {
  return Q(e).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function Yo(e, t) {
  try {
    const n = Rt(e, ["id", "rawId", "type", "response"], "assertion"), r = Lt(n.rawId, 1, 1023);
    (n.type !== "public-key" || n.id !== sl(r) || !t.factors.some((l) => l.credentialId === n.rawId)) && j("assertion does not identify a frozen factor");
    const i = Rt(n.response, ["clientDataJSON", "authenticatorData", "signature", "userHandle"], "assertion.response"), o = Lt(i.clientDataJSON, 1, vn), s = JSON.parse(new TextDecoder("utf-8", { fatal: !0 }).decode(o));
    (s.type !== "webauthn.get" || s.challenge !== sl(Ff(t)) || s.origin !== t.origin || s.crossOrigin !== void 0 && s.crossOrigin !== !1) && j("assertion client data does not bind this policy context");
    const a = Lt(i.authenticatorData, 37, vn), c = oe(new TextEncoder().encode(t.rpId));
    return ((a[32] & 5) !== 5 || !a.slice(0, 32).every((l, u) => l === c[u])) && j("assertion RP or UP/UV flags are invalid"), Lt(i.signature, 1, vn), i.userHandle !== null && Lt(i.userHandle, 0, vn), Mt(t), { id: n.id, rawId: n.rawId, type: "public-key", response: { ...i } };
  } catch (n) {
    throw n instanceof m && n.code !== "policy_context_invalid" ? n : new m("authorization_result_invalid", "invalid policy assertion");
  }
}
async function P1(e, t) {
  if (t.aborted) throw t.reason;
  const n = await navigator.credentials.get({ publicKey: I1(e), signal: t });
  if (t.aborted) throw t.reason;
  if (!n) throw new Error("passkey-cancelled");
  return Yo(Gu(n), e);
}
const jf = `avibe-vault-setup:v1:${ae.buildHash}:`, T1 = `avibe-vault-setup-result:${ae.buildHash}:`, B1 = `avibe-vault-setup-error:${ae.buildHash}:`, $1 = 300 * 1e3, O1 = 300 * 1e3, R1 = 30 * 1e3, L1 = 8 * 1e3, _e = new dp();
let al = !1;
function C1(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return (t.locale === "en" || t.locale === "zh") && (t.theme === "light" || t.theme === "dark");
}
function Vf(e) {
  if (!C1(e))
    throw new m("invalid_payload", "appearance must include locale and theme");
  return e;
}
function N1(e) {
  return e == null ? null : Vf(e);
}
function Wi(e) {
  const t = Jg(e.locale);
  document.documentElement.setAttribute("data-theme", e.theme), t && nm();
}
function qf() {
  const e = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  return { locale: Qg(), theme: e };
}
function We(e) {
  if (typeof e != "object" || e === null)
    throw new m("invalid_payload", "request payload must be an object");
  return e;
}
function Gf(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new m("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Pe(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Wf(e, t) {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be an object`);
  return e;
}
function z1(e) {
  const t = Wf(e, "envelope");
  return {
    ciphertext: Pe(t.ciphertext, "envelope.ciphertext"),
    nonce: Pe(t.nonce, "envelope.nonce"),
    wrap_meta: Pe(t.wrap_meta, "envelope.wrap_meta")
  };
}
function Zf(e) {
  const t = Wf(e, "material");
  return { name: Pe(t.name, "material.name"), envelope: z1(t.envelope) };
}
function D1(e) {
  const t = e == null ? {} : We(e);
  return { wrapMeta: Gf(t.wrapMeta, "wrapMeta") };
}
function Xf(e) {
  const t = We(e), n = !!t.existingProtectedVault;
  if (n && t.rootMetadata !== void 0)
    throw new m("invalid_payload", "vault root metadata cannot be replaced during setup");
  return {
    vaultUserHandle: Pe(t.vaultUserHandle, "vaultUserHandle"),
    displayName: Pe(t.displayName, "displayName"),
    existingProtectedVault: n,
    authzCreationOptions: t.authzCreationOptions,
    ...t.rootMetadata !== void 0 ? { rootMetadata: t.rootMetadata } : {},
    ...t.policy !== void 0 ? { policy: Rs(t.policy) } : {}
  };
}
function U1(e) {
  const t = We(e);
  return {
    wrapMeta: Pe(t.wrapMeta, "wrapMeta"),
    ...t.policy !== void 0 ? { policy: Rs(t.policy) } : {}
  };
}
function M1(e) {
  const t = We(e);
  return { material: Zf(t.material), context: Ws(t.context) };
}
function H1(e) {
  const t = We(e);
  if (!Array.isArray(t.items) || t.items.length === 0)
    throw new m("invalid_payload", "approveRelease requires a non-empty items array");
  return { items: t.items.map((n) => m1(n, Zf)) };
}
function F1(e) {
  const t = We(e);
  if (t.operation !== "delete_secret") throw new m("invalid_payload", "deleteAuthzAssertion only supports delete_secret");
  return {
    challengeId: Pe(t.challengeId, "challengeId"),
    operation: "delete_secret",
    secretName: Pe(t.secretName, "secretName"),
    webauthn: t.webauthn
  };
}
function Yf() {
  if (typeof crypto.randomUUID == "function") return crypto.randomUUID();
  const e = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(e, (t) => t.toString(16).padStart(2, "0")).join("");
}
async function cl(e) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(e)));
}
function K1(e) {
  return new Promise((t, n) => {
    const r = () => {
      n(zn(e));
    };
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function j1(e) {
  for (const t of e)
    if (t.aborted) return zn(t);
  return new Error("operation-superseded");
}
function Jf(e) {
  const t = new AbortController(), n = [], r = (i) => {
    t.signal.aborted || t.abort(zn(i));
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
async function Qf(e) {
  const t = Jf(e.abortSignals);
  try {
    await Promise.race([
      qg(xs(e.wrapMeta), vt(), e.challenge, t.signal).catch((n) => {
        throw t.signal.aborted ? j1(e.abortSignals) : n;
      }),
      ...e.abortSignals.map((n) => K1(n))
    ]);
  } finally {
    t.cleanup();
  }
}
async function ya(e) {
  await Ln(async (t) => {
    const n = async (i) => {
      const o = Jf([t, ...e.abortSignal ? [e.abortSignal] : []]);
      try {
        const s = await ep(
          {
            prompt: ll(e),
            wrapMeta: e.wrapMeta,
            passkey: e.passkey
          },
          o.signal,
          i
        );
        if (e.passkey === "unlock") {
          if (s.kind !== "unlock") throw new Error("authorization unlock result is missing");
          await od({
            wrapMeta: e.wrapMeta,
            currentRpId: vt(),
            assertion: {
              prfOutput: pe(s.prfOutput),
              prfSalt: pe(s.prfSalt)
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
    await hr(zf(ll(e), "common.continue"), t, void 0, n);
  });
}
function ll(e) {
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
function V1(e) {
  return "kind" in e ? e : {
    ...e,
    prompt: a1(e.prompt)
  };
}
function q1(e) {
  const t = We(e);
  if (!t) throw new m("invalid_payload", "authorization request must be an object");
  if (t.kind === "signing-policy-downgrade") {
    if (Object.keys(t).length !== 2 || !Object.hasOwn(t, "context"))
      throw new m("invalid_payload", "policy authorization request has unknown fields");
    return {
      kind: "signing-policy-downgrade",
      context: Kf(t.context, {
        origin: window.location.origin,
        hostname: window.location.hostname
      })
    };
  }
  if (t.kind !== void 0) throw new m("invalid_payload", "authorization request kind is invalid");
  const n = t.passkey;
  if (n !== "none" && n !== "unlock" && n !== "uv")
    throw new m("invalid_payload", "authorization passkey requirement is invalid");
  return {
    prompt: u1(t.prompt),
    wrapMeta: Pe(t.wrapMeta, "wrapMeta"),
    passkey: n
  };
}
function G1(e) {
  return pl("authorize", e, qf());
}
function W1(e, t) {
  const n = We(e);
  if (!n) throw new m("authorization_result_invalid", "authorization result is invalid");
  if ("kind" in t) {
    if (t.kind !== "signing-policy-downgrade")
      throw new m("authorization_result_invalid", "authorization request kind is invalid");
    if (n.kind !== "assertion" || !Object.hasOwn(n, "assertion"))
      throw new m("authorization_result_invalid", "policy authorization result kind is invalid");
    return { kind: "assertion", assertion: n.assertion };
  }
  if (n.kind === "confirmed") return { kind: "confirmed" };
  if (n.kind === "unlock")
    return {
      kind: "unlock",
      prfOutput: Pe(n.prfOutput, "result.prfOutput"),
      prfSalt: Pe(n.prfSalt, "result.prfSalt")
    };
  throw new m("authorization_result_invalid", "authorization result kind is invalid");
}
function ep(e, t, n) {
  tn(t);
  const r = n ?? Yf(), i = `avibe-vault-authorization-${r}`;
  return new Promise((o, s) => {
    let a = null, c = !1, l = null, u = null, d = null, h = null;
    const p = () => v(() => s(zn(t))), g = () => v(() => s(new m("authorization_cancelled", "requesting sandbox was unloaded", !0))), w = () => new m("authorization_window_closed", "authorization window was closed", !0), f = (x) => {
      a = x, u = setInterval(() => {
        !x.closed || d || (d = setTimeout(() => v(() => s(w())), 2500));
      }, 500);
    }, y = (x) => {
      if (x.origin !== window.location.origin) return;
      const I = x.data;
      if (!(!I || I.id !== r || !mn(I.build)) && (!a && n && I.type === "authorization-ready" && x.source && (h && clearTimeout(h), h = null, f(x.source)), x.source === a))
        if (I.type === "authorization-ready")
          try {
            a?.postMessage(
              {
                type: "authorization-request",
                build: ae,
                id: r,
                request: V1(e)
              },
              window.location.origin
            );
          } catch (E) {
            v(() => s(E));
          }
        else if (I.type === "authorization-approved")
          try {
            const E = W1(I.result, e);
            v(() => o(E));
          } catch (E) {
            v(() => s(E));
          }
        else I.type === "authorization-error" && v(() => s(new m(I.code, I.message, !!I.retryable)));
    }, v = (x) => {
      if (!c) {
        c = !0, l && clearTimeout(l), u && clearInterval(u), d && clearTimeout(d), h && clearTimeout(h), window.removeEventListener("message", y), window.removeEventListener("pagehide", g), t?.removeEventListener("abort", p);
        try {
          a && !a.closed && a.close();
        } catch {
        }
        x();
      }
    };
    if (window.addEventListener("message", y), window.addEventListener("pagehide", g, { once: !0 }), n)
      h = setTimeout(() => v(() => s(w())), R1);
    else {
      const x = window.open(G1(r), i, "popup,width=520,height=760");
      if (!x) {
        v(() => s(new m("authorization_popup_blocked", "authorization window was blocked", !0)));
        return;
      }
      x.focus(), f(x);
    }
    if (l = setTimeout(
      () => v(() => s(new m("authorization_window_timeout", "authorization window timed out", !0))),
      O1
    ), t) {
      if (t.aborted) {
        p();
        return;
      }
      t.addEventListener("abort", p, { once: !0 });
    }
  });
}
function Z1(e) {
  return e === 0 ? "context.oneTime" : e % 60 === 0 ? fr("context.minutes", { count: e / 60 }) : fr("context.seconds", { count: e });
}
function vr(e, t, n = "access") {
  const r = [];
  e.sessionLabel && r.push({ label: "context.session", value: be(e.sessionLabel) }), e.command && r.push({ label: "context.command", value: be(e.command), variant: "command" }), e.egress && r.push({ label: n === "sign" ? "context.signReturns" : "context.egress", value: be(e.egress) });
  const i = [
    ...e.source?.env?.map((o) => be(`${H(n === "sign" ? "context.sourceRequestedName" : "context.sourceEnv")}: ${o}`)) ?? [],
    ...e.source?.tags?.map((o) => be(`${H("context.sourceTag")}: ${o}`)) ?? [],
    ...e.source?.skills?.map((o) => be(`${H("context.sourceSkill")}: ${o}`)) ?? []
  ];
  return i.length > 0 && r.push({ label: "context.source", values: i, variant: "chips" }), t?.agentFingerprint && r.push({ label: "context.agent", value: be(t.agentFingerprint), variant: "mono" }), t?.grantId && r.push({ label: "context.grant", value: be(t.grantId), variant: "mono" }), e.grantTtlSeconds !== void 0 && r.push({ label: "context.agentAccess", value: Z1(e.grantTtlSeconds) }), r.push({
    label: "context.secrets",
    values: e.secrets.map(
      (o) => be(`${o.name} · ${H(o.kind === "static" ? "context.kindStatic" : "context.kindKeypair")}`)
    ),
    variant: "chips",
    tone: "warning"
  }), { rows: r };
}
function X1(e) {
  return e instanceof Error && e.message === "vault-locked";
}
function Y1(e) {
  return e instanceof Error && e.message === "vault-operation-aborted";
}
function tp(e, t) {
  return X1(e) ? !0 : Y1(e) && bt(t).state === "locked";
}
async function J1(e, t = Ae(), n) {
  await id({ wrapMeta: e, currentRpId: vt(), policy: t, abortSignal: n });
}
async function Q1(e, t = Ae()) {
  await Ln((n) => J1(e, t, n));
}
async function np(e) {
  try {
    await ya({ ...e, passkey: "unlock" });
  } catch (t) {
    throw bt(e.wrapMeta).state === "unlocked" && qe({ broadcast: !0, reason: "manual-lock" }), t;
  }
}
async function e2(e, t) {
  if (bt(e).state !== "unlocked") {
    if (!e) throw new Error("vault-locked");
    await Q1(e);
  }
  return await zs(t, { renewOnSuccess: !0 });
}
async function rp(e) {
  const t = bt(e.wrapMeta).state, n = Ae(), r = Uf({ tier: e.tier, vaultState: t, policy: n });
  let i = !1;
  const o = async () => {
    await np({
      ...e.lockedPrompt,
      wrapMeta: e.wrapMeta,
      policy: n,
      parentSurface: e.parentSurface,
      authorizationWindow: e.authorizationWindow
    }), i = !0;
  }, s = async (a, c = !1) => {
    let l = !1;
    try {
      const u = await zs(
        async (d, h, p) => {
          const g = await e.buildPrompt(d, h, p);
          return r.confirm && !c && (await ya({
            ...g,
            wrapMeta: h,
            passkey: a,
            abortSignal: p.signal,
            parentSurface: e.parentSurface,
            authorizationWindow: e.authorizationWindow
          }), p.assertCurrent()), e.operation(d, h, p);
        },
        { renewOnSuccess: r.renewOnSuccess, beforeSuccess: e.beforeSuccess }
      );
      return l = !0, u;
    } catch (u) {
      throw i && !l && bt().state === "unlocked" && qe({ broadcast: !0, reason: "manual-lock" }), u;
    }
  };
  try {
    return r.passkey === "unlock" ? (await o(), await s("none", !0)) : await s(r.passkey === "uv" ? "uv" : "none");
  } catch (a) {
    if (r.passkey !== "unlock" && tp(a, e.wrapMeta))
      return await o(), await s("none", !0);
    throw a;
  }
}
function ze(e, t) {
  if (e instanceof m) return e;
  if (Xg(e)) return new m("passkey_cancelled", "passkey-cancelled", !0);
  const n = e instanceof Error ? e.message : t;
  switch (n) {
    case "passkey-cancelled":
      return new m("passkey_cancelled", n, !0);
    case "passkey-prf-unavailable":
      return new m("passkey_prf_unavailable", n);
    case "passkey-not-configured":
      return new m("passkey_not_configured", n);
    case "passkey-multiple-not-supported":
      return new m("passkey_multiple_not_supported", n);
    case "vault-operation-aborted":
      return new m("vault_operation_aborted", n, !0);
    default:
      return new m(t, n);
  }
}
function t2(e) {
  return Q(new TextEncoder().encode(JSON.stringify(e))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function n2(e) {
  return Xf(JSON.parse(new TextDecoder().decode(pe(e))));
}
function r2(e, t) {
  return pl("setup", e, qf(), { req: t2(t) });
}
function Ao(e) {
  const t = We(e);
  return {
    credentialId: Pe(t.credentialId, "credentialId"),
    ...t.authzRegistration !== void 0 ? { authzRegistration: t.authzRegistration } : {}
  };
}
function Zi(e) {
  return `${T1}${e}`;
}
function Xi(e) {
  return `${B1}${e}`;
}
function ul(e) {
  try {
    localStorage.removeItem(Zi(e)), localStorage.removeItem(Xi(e));
  } catch {
  }
}
function i2(e, t) {
  try {
    localStorage.setItem(Zi(e), JSON.stringify({ build: ae, result: t })), localStorage.removeItem(Xi(e));
  } catch {
  }
}
function o2(e, t) {
  try {
    localStorage.setItem(
      Xi(e),
      JSON.stringify({ build: ae, code: t.code, message: t.message, retryable: t.retryable })
    ), localStorage.removeItem(Zi(e));
  } catch {
  }
}
function ip(e) {
  if (window.self === window.top)
    return Ms({
      rpId: vt(),
      vaultUserHandle: e.vaultUserHandle,
      displayName: e.displayName,
      authzCreationOptions: e.authzCreationOptions
    });
  const t = Yf(), n = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${jf}${t}`);
  let r = null;
  return ul(t), new Promise((i, o) => {
    let s = !1, a = null, c = null, l = null;
    const u = () => d(() => o(new m("setup_window_closed", "requesting sandbox was unloaded", !0))), d = (w) => {
      if (!s) {
        s = !0, a && clearTimeout(a), c && clearInterval(c), l && clearTimeout(l), window.removeEventListener("visibilitychange", p), window.removeEventListener("focus", p), window.removeEventListener("storage", p), window.removeEventListener("message", g), window.removeEventListener("pagehide", u), n?.close(), ul(t);
        try {
          r?.close();
        } catch {
        }
        w();
      }
    }, h = () => {
      let w = null, f = null;
      try {
        w = localStorage.getItem(Zi(t)), f = localStorage.getItem(Xi(t));
      } catch {
        return !1;
      }
      if (w) {
        try {
          const y = We(JSON.parse(w));
          if (!mn(y.build)) throw new m("sandbox_build_mismatch");
          const v = Ao(y.result);
          d(() => i(v));
        } catch (y) {
          const v = y instanceof Error ? y.message : "stored setup result is invalid";
          d(() => o(new m("setup_storage_invalid", v)));
        }
        return !0;
      }
      if (f) {
        try {
          const y = We(JSON.parse(f));
          if (!mn(y.build)) throw new m("sandbox_build_mismatch");
          d(
            () => o(new m(Pe(y.code, "code"), Gf(y.message, "message"), !!y.retryable))
          );
        } catch (y) {
          const v = y instanceof Error ? y.message : "stored setup error is invalid";
          d(() => o(new m("setup_storage_invalid", v)));
        }
        return !0;
      }
      return !1;
    }, p = () => {
      h();
    }, g = (w) => {
      if (w.origin !== window.location.origin || w.source !== r) return;
      const f = w.data;
      !f || f.id !== t || !mn(f.build) || (f.type === "setup-created" ? d(() => i(Ao(f.result))) : f.type === "setup-error" && d(() => o(new m(f.code, f.message, !!f.retryable))));
    };
    if (a = setTimeout(() => {
      h() || d(() => o(new m("setup_window_timeout", "setup window timed out", !0)));
    }, $1), c = setInterval(() => {
      !r?.closed || l || h() || (l = setTimeout(() => {
        h() || d(() => o(new m("setup_window_closed", "setup window was closed", !0)));
      }, 2500));
    }, 500), n && (n.onmessage = (w) => {
      const f = w.data;
      !f || f.id !== t || !mn(f.build) || (f.type === "setup-created" ? d(() => i(Ao(f.result))) : f.type === "setup-error" && d(() => o(new m(f.code, f.message, !!f.retryable))));
    }), window.addEventListener("visibilitychange", p), window.addEventListener("focus", p), window.addEventListener("storage", p), window.addEventListener("message", g), window.addEventListener("pagehide", u, { once: !0 }), r = window.open(r2(t, e), "avibe-vault-setup", "popup,width=440,height=620"), !r) {
      d(() => o(new m("setup_popup_blocked", "setup window was blocked", !0)));
      return;
    }
    r.focus();
  });
}
function zn(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function tn(e) {
  if (e?.aborted) throw zn(e);
}
function s2() {
  const e = window.navigator;
  return /iP(?:hone|ad|od)/.test(e.userAgent) || e.platform === "MacIntel" && e.maxTouchPoints > 1;
}
function dl(e) {
  return s2() && Zu(e);
}
async function op(e, t, n, r, i = Ae()) {
  const o = Lw();
  let s, a;
  try {
    tn(r), s = (await Us([{ credentialId: n.credentialId, prfSalt: o }], t, r)).prfOutput, tn(r), a = Rw();
    const l = await zw(a, [{ kind: "passkey", prfOutput: s, prfSalt: o, credentialId: n.credentialId }]);
    tn(r);
    let u = Mw(l, {
      rpId: t,
      vaultUserHandle: e.vaultUserHandle
    });
    e.rootMetadata && (u = Kw(u, await Fw(a, e.rootMetadata, u)), tn(r));
    const d = Fu({
      vmk: a,
      wrapMeta: u,
      freshSetup: !e.existingProtectedVault,
      scopeId: Ig(e.vaultUserHandle),
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
function a2(e, t, n) {
  return Ln((r) => {
    const i = js(
      "setup.title",
      "setup.subtitle",
      "setup.iframeBody"
    ), o = pr("setup.createPasskey"), s = rd("setup.ready");
    return bn(i.card, o), bn(i.card, s), new Promise((a, c) => {
      let l = !1, u = "create", d = null, h = !1;
      const p = (E) => {
        l || (l = !0, r.removeEventListener("abort", f), Vs(), E());
      }, g = (E) => {
        p(() => c(E));
      }, w = (E) => {
        l || (u = "create", d = null, X(o, "setup.createPasskey"), o.disabled = !1, X(s, E));
      }, f = () => {
        g(zn(r));
      }, y = (E) => {
        if (E instanceof m && E.code === "setup_popup_blocked") {
          w("setup.popupBlocked");
          return;
        }
        g(E);
      }, v = () => {
        h = !0, o.disabled = !0, X(s, "setup.popupPrompt");
        let E;
        try {
          E = ip(e);
        } catch (T) {
          y(T);
          return;
        }
        E.then(
          (T) => {
            l || (d = T, u = "finish", X(o, "setup.finish"), o.disabled = !1, X(s, "setup.createdFinish"));
          },
          (T) => {
            y(T);
          }
        );
      }, x = (E) => {
        o.disabled = !0, X(s, "setup.passkeyPrompt"), op(e, t, E, r, n).then(
          (T) => p(() => a(T)),
          (T) => g(T)
        );
      }, I = () => {
        o.disabled = !0, X(s, "setup.passkeyPrompt");
        let E;
        try {
          E = Ms({
            rpId: t,
            vaultUserHandle: e.vaultUserHandle,
            displayName: e.displayName,
            authzCreationOptions: e.authzCreationOptions
          });
        } catch (T) {
          if (dl(T)) {
            v();
            return;
          }
          g(T);
          return;
        }
        E.then(
          (T) => x(T),
          (T) => {
            if (dl(T)) {
              v();
              return;
            }
            g(T);
          }
        );
      };
      if (r.aborted) {
        f();
        return;
      }
      r.addEventListener("abort", f, { once: !0 }), o.addEventListener("click", () => {
        if (!(l || o.disabled)) {
          if (u === "finish" && d) {
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
async function c2(e) {
  const t = Xf(e);
  try {
    const n = vt(), r = t.policy ?? Ae(), i = window.self !== window.top ? await a2(t, n, r) : await op(t, n, await ip(t), void 0, r), o = t.policy ? Ls(r) : Ae();
    return { ...i, policy: o };
  } catch (n) {
    throw ze(n, "setup_failed");
  }
}
async function l2(e) {
  const t = U1(e);
  try {
    const n = vt(), r = t.policy ?? Ae(), i = await Ln(async (s) => {
      let a;
      if (await hr(
        {
          title: "unlock.sessionTitle",
          subtitle: fr("unlock.sessionSubtitle", {
            minutes: r.windowSeconds / 60
          }),
          body: "unlock.sessionBody",
          confirmLabel: "common.continue"
        },
        s,
        void 0,
        async () => {
          a = await id({ wrapMeta: t.wrapMeta, currentRpId: n, abortSignal: s, policy: r });
        }
      ), !a) throw new Error("vault unlock did not complete");
      return a;
    }), o = t.policy ? Ls(r) : Ae();
    return { ...i, policy: o };
  } catch (n) {
    throw ze(n, "unlock_failed");
  }
}
async function u2(e) {
  const t = km(e);
  t.wrapMeta && Ds(t.wrapMeta);
  try {
    if (t.kind === "static" && !Ae().parentValueSealAllowed)
      throw new m("parent_value_seal_disabled", "parent-value sealing is disabled by policy");
    return await e2(t.wrapMeta, async (n, r) => ({ ...t.kind === "static" ? await w1({ name: t.name, value: t.value, vmk: n, wrapMeta: r }) : await g1({ name: t.name, curve: t.curve, vmk: n, wrapMeta: r }), establishingVmk: Lg() }));
  } catch (n) {
    throw ze(n, "seal_failed");
  }
}
async function d2(e, t) {
  const n = M1(e);
  try {
    const { sealed: r, vmkWrapMeta: i, recordMetadata: o } = Pn(n.material.envelope);
    if (o?.kind === "keypair") throw new Error("keypair protected records cannot be revealed as plaintext");
    return Uo({
      context: n.context,
      expectedPurpose: "reveal",
      secret: { name: n.material.name, kind: "static" }
    }), await rp({
      tier: "R2",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: {
        title: "reveal.showConfirmTitle",
        subtitle: be(n.material.name),
        body: "reveal.confirmBody",
        details: vr(n.context.display),
        label: "common.continue"
      },
      buildPrompt: async (s, a) => {
        const c = await _s(a, s);
        return Ei({ context: n.context, rootMetadata: c, expectedPurpose: "reveal" }), Uo({
          context: n.context,
          expectedPurpose: "reveal",
          secret: { name: n.material.name, kind: "static" }
        }), Si([n.context]), {
          title: "reveal.showConfirmTitle",
          subtitle: be(n.material.name),
          body: "reveal.confirmBody",
          details: vr(n.context.display),
          challenge: await cl(Mi(n.context)),
          label: "common.continue"
        };
      },
      operation: async (s, a, c) => {
        const l = await As(r, s, Es(n.material.name, o));
        try {
          c.assertCurrent(), await Hi([n.context]), await mm({
            name: n.material.name,
            plaintext: l,
            abortSignal: c.signal,
            prepareCopy: Ae().strictApprovals ? async (u) => {
              await Qf({
                wrapMeta: a,
                challenge: await cl(`reveal-copy:${n.context.requestId}:${n.material.name}`),
                abortSignals: [u, c.signal]
              }), tn(u), tn(c.signal);
            } : void 0
          });
        } finally {
          l.fill(0);
        }
        return { completed: !0 };
      }
    });
  } catch (r) {
    throw ze(r, "reveal_failed");
  }
}
async function f2(e, t) {
  try {
    const n = i1(e), { request: r, unpacked: { vmkWrapMeta: i } } = n, o = (s) => {
      const a = r.context.display.sessionLabel;
      if (!s.review && s.display === void 0) throw new m("invalid_payload", "signing request has no reviewable content");
      return {
        title: "sign.title",
        subtitle: be(r.material.name),
        ...s.review ? { review: s.review, ...a ? { reviewSession: a } : {} } : { body: be(s.display ?? "") },
        details: vr(r.context.display, void 0, "sign"),
        label: "sign.confirm"
      };
    };
    return await rp({
      tier: "R3",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: o(n.verified),
      // Shown only after the daemon signature, expiry and replay state are
      // verified with the vault key, from content re-derived at that moment.
      buildPrompt: async (s, a) => ({
        ...o(await o1(n, s, a)),
        challenge: new Uint8Array(n.challenge)
      }),
      operation: (s, a) => s1(n, s, a),
      beforeSuccess: () => Hi([r.context])
    });
  } catch (n) {
    throw ze(n, "sign_failed");
  }
}
async function p2(e, t) {
  const n = H1(e);
  try {
    const i = Pn(n.items[0].material.envelope).vmkWrapMeta;
    for (const h of n.items) {
      const p = Pn(h.material.envelope);
      if (p.vmkWrapMeta !== i) throw new m("invalid_payload", "approveRelease items must share one VMK context");
      if (!Mf(p.recordMetadata)) throw new m("invalid_payload", "approveRelease only supports static protected records");
    }
    const o = Hf(n.items), s = bt(i).state, a = Ae(), c = Uf({ tier: "R2", vaultState: s, policy: a });
    let l = !1;
    const u = async () => {
      await np({
        title: "release.title",
        subtitle: fr("release.subtitle", { count: n.items.length }),
        details: vr(o.display, o.recipient),
        label: "release.confirm",
        wrapMeta: i,
        policy: a,
        parentSurface: t.latestSurface,
        authorizationWindow: t.takeAuthorizationWindow
      }), l = !0;
    }, d = async (h, p = !1) => {
      let g = !1, w;
      try {
        const f = await zs(
          async (y, v, x) => {
            const I = await b1({
              items: n.items,
              vmk: y,
              wrapMeta: v,
              consumeReplayIds: !1,
              onApprovalAccepted: (E) => {
                w = E;
              },
              confirm: (E) => !c.confirm || p ? Promise.resolve() : ya({
                title: "release.title",
                subtitle: fr("release.subtitle", { count: n.items.length }),
                details: vr(E.display, E.recipient),
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
            beforeSuccess: () => Hi(n.items.map((y) => y.context), w)
          }
        );
        return g = !0, f;
      } catch (f) {
        throw l && !g && bt().state === "unlocked" && qe({ broadcast: !0, reason: "manual-lock" }), f;
      }
    };
    try {
      return c.passkey === "unlock" ? (await u(), await d("none", !0)) : await d(c.passkey === "uv" ? "uv" : "none");
    } catch (h) {
      if (c.passkey !== "unlock" && tp(h, i))
        return await u(), await d("none", !0);
      throw h;
    }
  } catch (r) {
    throw ze(r, "approve_release_failed");
  }
}
async function h2(e, t) {
  let n;
  if (await Ln(async (r) => {
    Mt(e);
    const i = { kind: "signing-policy-downgrade", context: e }, o = async (c) => {
      Mt(e);
      const l = await ep(i, r, c);
      if (l.kind !== "assertion") throw new m("authorization_result_invalid", "policy assertion is missing");
      n = Yo(l.assertion, e);
    }, s = t.takeAuthorizationWindow();
    if (s) {
      await o(s);
      return;
    }
    Mt(e);
    const a = Xu(e);
    await hr(
      a,
      r,
      (c) => Df({
        uiShowPending: ed,
        parentSurface: () => t.latestSurface(),
        visibilityTarget: c,
        requireParentSurface: !0
      }),
      () => o()
    );
  }), !n) throw new m("authorization_result_invalid", "policy assertion is missing");
  return Yo(n, e);
}
async function y2(e, t) {
  try {
    const n = _1(e, {
      origin: window.location.origin,
      hostname: window.location.hostname
    }), r = await h2(n, t);
    return { challengeId: n.challengeId, assertion: r };
  } catch (n) {
    throw ze(n, "policy_authorization_failed");
  }
}
async function w2(e) {
  const t = F1(e);
  try {
    const n = await Gg(t.webauthn);
    return { challengeId: t.challengeId, assertion: n };
  } catch (n) {
    throw ze(n, "delete_authz_failed");
  }
}
function g2() {
  const e = Jo();
  if (e.get("mode") !== "setup") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Wi({ locale: t, theme: n });
  const r = e.get("id"), i = document.getElementById("page"), o = i?.querySelector(".card"), s = i?.querySelector("h1"), a = i?.querySelector(".sub"), c = i?.querySelector(".body");
  if (!r || !o || !s || !a || !c) return;
  document.body.classList.add("setup-mode"), X(s, "setup.title"), X(a, "setup.subtitle"), X(c, "setup.topLevelBody");
  const l = document.createElement("button");
  l.type = "button", l.className = "action", X(l, "setup.createPasskey");
  const u = document.createElement("p");
  u.className = "setup-status", X(u, "setup.readyOnOrigin"), o.append(l, u);
  const d = new URLSearchParams(window.location.hash.slice(1)).get("req");
  if (!d) {
    l.disabled = !0, X(u, "setup.requestMissing");
    return;
  }
  let h;
  try {
    h = n2(d);
  } catch {
    l.disabled = !0, X(u, "setup.requestInvalid");
    return;
  }
  const p = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${jf}${r}`), g = (w) => {
    const f = ze(w, "setup_create_failed"), y = {
      type: "setup-error",
      build: ae,
      id: r,
      code: f.code,
      message: f.message,
      retryable: f.retryable
    };
    o2(r, f), p?.postMessage(y);
    try {
      window.opener?.postMessage(y, window.location.origin);
    } catch {
    }
    l.disabled = !1, X(u, be(f.message));
  };
  l.addEventListener("click", () => {
    l.disabled = !0, X(u, "setup.passkeyPrompt");
    let w;
    try {
      w = Ms({
        rpId: vt(),
        vaultUserHandle: h.vaultUserHandle,
        displayName: h.displayName,
        authzCreationOptions: h.authzCreationOptions
      });
    } catch (f) {
      g(f);
      return;
    }
    w.then((f) => {
      const y = { type: "setup-created", build: ae, id: r, result: f };
      i2(r, f), p?.postMessage(y);
      try {
        window.opener?.postMessage(y, window.location.origin);
      } catch {
      }
      X(u, "setup.created"), setTimeout(() => window.close(), 250);
    }).catch(g);
  });
}
function m2() {
  const e = Jo();
  if (e.get("mode") !== "authorize") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Wi({ locale: t, theme: n });
  const r = e.get("id"), o = document.getElementById("page")?.querySelector(".card"), s = o?.querySelector(".body");
  if (!r || !o || !s || (s.classList.add("waiting"), X(s, window.opener ? "authorize.waiting" : "authorize.waitingLong"), !window.opener)) return;
  const a = () => {
    const f = window.opener;
    if (!f) return [];
    const y = [f];
    try {
      for (let v = 0; v < f.frames.length; v += 1) y.push(f.frames[v]);
    } catch {
    }
    return y;
  };
  let c = null;
  const l = (f) => {
    try {
      c?.postMessage(f, window.location.origin);
    } catch {
    }
  };
  let u = !1, d = null;
  const h = setTimeout(() => X(s, "authorize.waitingLong"), L1), p = () => {
    if (!u)
      for (const f of a())
        try {
          f.postMessage({ type: "authorization-ready", build: ae, id: r }, window.location.origin);
        } catch {
        }
  }, g = (f) => {
    if (u) return;
    u = !0, d && clearInterval(d), clearTimeout(h), s.classList.remove("waiting"), window.removeEventListener("message", w), document.body.classList.add("setup-mode");
    const y = new AbortController(), v = (T) => {
      const P = ze(T, T instanceof Error && T.message === "operation-cancelled" ? "authorization_cancelled" : "authorization_failed");
      l({
        type: "authorization-error",
        build: ae,
        id: r,
        code: P.code,
        message: P.message,
        retryable: P.retryable
      }), setTimeout(() => window.close(), 250);
    }, x = (T, P) => {
      T.then(
        () => {
          try {
            l({ type: "authorization-approved", build: ae, id: r, result: P() }), setTimeout(() => window.close(), 250);
          } catch (O) {
            v(O);
          }
        },
        v
      );
    };
    if ("kind" in f) {
      if (f.kind !== "signing-policy-downgrade") {
        v(new m("authorization_request_invalid", "authorization request kind is invalid"));
        return;
      }
      try {
        Mt(f.context);
      } catch (O) {
        v(O);
        return;
      }
      let T;
      const P = Xu(f.context);
      x(
        hr(
          P,
          y.signal,
          (O) => Df({
            uiShowPending: ed,
            visibilityTarget: O,
            requireParentSurface: !0
          }),
          async () => {
            Mt(f.context), T = await P1(f.context, y.signal);
          }
        ),
        () => {
          if (!T) throw new m("authorization_result_invalid", "policy assertion is missing");
          return { kind: "assertion", assertion: T };
        }
      );
      return;
    }
    let I = { kind: "confirmed" };
    const E = async () => {
      if (f.passkey === "none") return;
      if (f.passkey === "uv") {
        if (!f.prompt.challenge) throw new m("invalid_payload", "authorization challenge is missing");
        await Qf({
          wrapMeta: f.wrapMeta,
          challenge: f.prompt.challenge,
          abortSignals: [y.signal]
        });
        return;
      }
      const T = await Us(xs(f.wrapMeta), vt(), y.signal);
      I = {
        kind: "unlock",
        prfOutput: Q(T.prfOutput),
        prfSalt: Q(T.prfSalt)
      }, T.prfOutput.fill(0);
    };
    x(hr(zf(f.prompt), y.signal, void 0, E), () => I);
  }, w = (f) => {
    if (f.origin !== window.location.origin) return;
    const y = f.data;
    if (!y || y.type !== "authorization-request" || y.id !== r || !mn(y.build)) return;
    const v = a().find((x) => x === f.source);
    if (v) {
      c = v;
      try {
        g(q1(y.request));
      } catch (x) {
        const I = ze(x, "authorization_request_invalid");
        l({
          type: "authorization-error",
          build: ae,
          id: r,
          code: I.code,
          message: I.message,
          retryable: I.retryable
        });
      }
    }
  };
  window.addEventListener("message", w), p(), d = setInterval(p, 500);
}
function b2() {
  if (window.self !== window.top || Jo().has("mode")) return;
  const e = document.querySelector("#page .body");
  e && X(e, "sandbox.intro");
}
_e.register("handshake", (e) => {
  const t = e ?? {}, n = typeof t.expectedBuildHash == "string" ? t.expectedBuildHash : null;
  if (typeof t.parentOrigin != "string" || typeof t.nonce != "string" || t.nonce.length < 16)
    throw new m("invalid_handshake", "handshake must include parentOrigin and nonce");
  if (n !== null && n !== ae.buildHash)
    throw new m("build_hash_mismatch", "sandbox build hash does not match parent expectation");
  const r = N1(t.appearance);
  r && Wi(r);
  const i = al ? Ae() : Ls(t.policy === void 0 ? void 0 : Rs(t.policy));
  return al = !0, {
    accepted: !0,
    channel: Zt,
    version: 2,
    sandboxOrigin: window.location.origin,
    build: ae,
    policy: i
  };
});
_e.register("set-appearance", (e) => (Wi(Vf(e)), { accepted: !0 }));
_e.register("status", (e) => {
  const t = D1(e);
  try {
    t.wrapMeta && bt(t.wrapMeta);
    const n = bt(), r = Ae();
    return {
      ...n,
      policy: r,
      session: {
        ...n.expiresAt !== void 0 ? { expiresAt: n.expiresAt } : {},
        strict: r.strictApprovals
      }
    };
  } catch (n) {
    throw ze(n, "invalid_wrap_meta");
  }
});
_e.register("setup", c2);
_e.register("unlock", l2);
_e.register("lock", () => ({ ...qe({ broadcast: !0, reason: "manual-lock" }), policy: Ae() }));
_e.register("seal", u2);
_e.register("reveal", d2);
_e.register("sign", f2);
_e.register("approveRelease", p2);
_e.register("deleteAuthzAssertion", w2);
_e.register("authorizeSigningPolicyDowngrade", y2);
Ag((e) => _e.emit("vault.state", e));
fm((e) => _e.emit(e));
await fp();
_e.start();
window.addEventListener("pagehide", Cg);
g2();
m2();
b2();
window.self !== window.top && document.body.classList.add("embedded");
const sp = globalThis.crypto, v2 = { webcrypto: sp }, ap = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: v2,
  webcrypto: sp
}, Symbol.toStringTag, { value: "Module" }));
