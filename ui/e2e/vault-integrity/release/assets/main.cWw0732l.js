const Nt = "avibe.vault.crypto";
const ie = {
  sandboxVersion: "0.1.0",
  // Resource hashes are the authority; this source-input identity additionally
  // binds ready/handshake and same-release top-level ceremony pairing.
  buildHash: "af209c9739dcbe209f0c15d9c68cbcba85ca3ad19de24593865be4fbfe41f455"
};
class m extends Error {
  code;
  retryable;
  constructor(t, n, r = !1) {
    super(n ?? t), this.code = t, this.retryable = r;
  }
}
function Hs(e) {
  let t;
  try {
    t = new URL(e);
  } catch {
    return !1;
  }
  const n = t.hostname.toLowerCase();
  return n === "localhost" || n === "127.0.0.1" || n === "::1" || n === "[::1]" ? t.protocol === "http:" || t.protocol === "https:" : !!(t.protocol === "https:" && (n === "avibe.bot" || n.endsWith(".avibe.bot")));
}
function Dd(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return t.channel === Nt && t.version === 2 && typeof t.id == "string" && t.id.length > 0 && typeof t.op == "string";
}
const jd = /^[A-Za-z0-9_-]{16,128}$/;
function gc(e) {
  return typeof e == "object" && e !== null;
}
function Vd(e) {
  return gc(e) ? e.channel === Nt && e.version === 2 && e.kind === "event" && e.event === "confirm.surface" : !1;
}
class qd {
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
        channel: Nt,
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
      channel: Nt,
      version: 2,
      build: ie,
      capabilities: {
        operations: Array.from(this.handlers.keys())
      }
    });
  }
  async onMessage(t) {
    if (!Hs(t.origin)) return;
    if (Vd(t.data)) {
      if (t.origin === this.pinnedParentOrigin && t.source === this.pinnedSource) {
        const c = t.data.surface ?? t.data.payload;
        c !== void 0 && this.recordParentSurfaceEvent(t.data, c);
      }
      return;
    }
    if (!Dd(t.data)) return;
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
    if (n.authorizationWindow !== void 0 && (typeof n.authorizationWindow != "string" || !jd.test(n.authorizationWindow))) {
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
        channel: Nt,
        version: 2,
        id: n.id,
        ok: !0,
        result: c
      });
    } catch (c) {
      const u = c instanceof m ? c.code : "internal_error", l = c instanceof m ? c.retryable : !1, d = c instanceof m ? c.message : void 0;
      this.reply(t.source, this.fail(n.id, u, d, l)), r && this.clearPin();
    } finally {
      this.activeSurfaceSlots.delete(n.id);
    }
  }
  validateInitialHandshake(t, n) {
    if (t.op !== "handshake" || !gc(t.payload)) return null;
    const r = t.payload.parentOrigin, i = t.payload.nonce;
    return typeof r != "string" || r !== n || !Hs(r) || typeof i != "string" || i.length < 16 || i.length > 256 ? null : i;
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
    return { channel: Nt, version: 2, id: t, ok: !1, error: { code: n, message: r, retryable: i } };
  }
  reply(t, n) {
    !t || this.pinnedParentOrigin === null || t.postMessage(n, this.pinnedParentOrigin);
  }
  postToParent(t, n) {
    t.postMessage(n, "*");
  }
}
function xo() {
  return new URLSearchParams((window.location.hash ?? "").slice(1));
}
function wc(e, t, n, r = {}) {
  const i = new URL(window.location.href);
  return ie.buildHash !== "dev" && (i.pathname = `/v/${ie.buildHash}/index.html`), i.search = "", i.hash = new URLSearchParams({ mode: e, id: t, ...n, ...r }).toString(), i.toString();
}
function sn(e) {
  if (!e || typeof e != "object") return !1;
  const t = e;
  return t.buildHash === ie.buildHash && t.sandboxVersion === ie.sandboxVersion;
}
async function Gd() {
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
class Ve extends Error {
  constructor(t) {
    let n;
    t instanceof Error ? n = t.message : typeof t == "string" ? n = t : n = "", super(n), this.name = this.constructor.name;
  }
}
class Ze extends Ve {
}
class Pr extends Ve {
}
class cr extends Ve {
}
class Wd extends Ve {
}
class Zd extends Ve {
}
class Xd extends Ve {
}
class Yd extends Ve {
}
class Jd extends Ve {
}
class Qd extends Ve {
}
class ef extends Ve {
}
class Eo extends Ve {
}
const tf = {}, bc = nf(globalThis, tf);
function nf(e, t) {
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
async function rf() {
  if (bc !== void 0 && globalThis.crypto !== void 0)
    return globalThis.crypto.subtle;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Kd);
    return e.subtle;
  } catch (e) {
    throw new Eo(e);
  }
}
class ko {
  constructor() {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    });
  }
  async _setup() {
    this._api === void 0 && (this._api = await rf());
  }
}
const wt = {
  Base: 0,
  Psk: 1,
  Auth: 2,
  AuthPsk: 3
}, Fs = {
  DhkemX25519HkdfSha256: 32
}, mc = {
  HkdfSha256: 1
}, So = {
  Aes128Gcm: 1,
  Aes256Gcm: 2,
  ExportOnly: 65535
}, Ur = 8192, of = 268435456, Ks = 32, me = /* @__PURE__ */ new Uint8Array(0), Xe = 0n, Jt = 1n, ur = 2n, sf = /* @__PURE__ */ new Uint8Array([
  75,
  69,
  77,
  0,
  0
]), Ds = /* @__PURE__ */ new Uint8Array([
  72,
  80,
  75,
  69,
  45,
  118,
  49
]);
function Br(e) {
  return new Uint8Array(Z(e));
}
function Z(e) {
  return e instanceof ArrayBuffer ? e : ArrayBuffer.isView(e) ? new Uint8Array(e.buffer, e.byteOffset, e.byteLength).slice().buffer : new Uint8Array(e).slice().buffer;
}
class af extends ko {
  constructor() {
    super(), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: mc.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: me
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
    return r.set(Ds, 0), r.set(this._suiteId, 7), r.set(t, 7 + this._suiteId.byteLength), r.set(n, 7 + this._suiteId.byteLength + t.byteLength), r;
  }
  buildLabeledInfo(t, n, r) {
    this._checkInit();
    const i = new Uint8Array(9 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return i.set(new Uint8Array([0, r]), 0), i.set(Ds, 2), i.set(this._suiteId, 9), i.set(t, 9 + this._suiteId.byteLength), i.set(n, 9 + this._suiteId.byteLength + t.byteLength), i;
  }
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : Z(t);
    if (r.byteLength !== this.hashSize)
      throw new Ze("The salt length must be the same as the hashSize");
    const i = Z(n), o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
  async expand(t, n, r) {
    await this._setup();
    const i = Z(t), o = await this._api.importKey("raw", i, this.algHash, !1, [
      "sign"
    ]), s = new ArrayBuffer(r), a = new Uint8Array(s);
    let c = me;
    const u = Br(n), l = new Uint8Array(1);
    if (r > 255 * this.hashSize)
      throw new Error("Entropy limit reached");
    const d = new Uint8Array(this.hashSize + u.length + 1);
    for (let h = 1, f = 0; f < a.length; h++)
      l[0] = h, d.set(c, 0), d.set(u, c.length), d.set(l, c.length + u.length), c = new Uint8Array(await this._api.sign("HMAC", o, d.slice(0, c.length + u.length + 1))), a.length - f >= c.length ? (a.set(c, f), f += c.length) : (a.set(c.slice(0, a.length - f), f), f += a.length - f);
    return s;
  }
  async extractAndExpand(t, n, r, i) {
    await this._setup();
    const o = Z(n), s = await this._api.importKey("raw", o, "HKDF", !1, ["deriveBits"]);
    return await this._api.deriveBits({
      name: "HKDF",
      hash: this.algHash.hash,
      salt: Z(t),
      info: Z(r)
    }, s, i * 8);
  }
  async labeledExtract(t, n, r) {
    return await this.extract(t, this.buildLabeledIkm(n, r));
  }
  async labeledExpand(t, n, r, i) {
    return await this.expand(t, this.buildLabeledInfo(n, r, i), i);
  }
  _checkInit() {
    if (this._suiteId === me)
      throw new Error("Not initialized. Call init()");
  }
}
class vc extends af {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: mc.HkdfSha256
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
const _n = (e) => typeof e == "object" && e !== null && typeof e.privateKey == "object" && typeof e.publicKey == "object";
function Nn(e, t) {
  if (t <= 0)
    throw new Error("i2Osp: too small size");
  if (e >= 256 ** t)
    throw new Error("i2Osp: too large integer");
  const n = new Uint8Array(t);
  for (let r = 0; r < t && e; r++)
    n[t - (r + 1)] = e % 256, e = Math.floor(e / 256);
  return n;
}
function lr(e, t) {
  const n = new Uint8Array(e.length + t.length);
  return n.set(e, 0), n.set(t, e.length), n;
}
function js(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = atob(t), r = new Uint8Array(n.length);
  for (let i = 0; i < n.length; i++)
    r[i] = n.charCodeAt(i);
  return r;
}
async function cf() {
  if (typeof bc < "u" && globalThis.crypto !== void 0)
    return globalThis.crypto;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Kd);
    return e;
  } catch {
    throw new Error("failed to load Crypto");
  }
}
function uf(e, t) {
  if (e.byteLength !== t.byteLength)
    throw new Error("xor: different length inputs");
  const n = new Uint8Array(e.byteLength);
  for (let r = 0; r < e.byteLength; r++)
    n[r] = e[r] ^ t[r];
  return n;
}
const lf = /* @__PURE__ */ new Uint8Array([
  101,
  97,
  101,
  95,
  112,
  114,
  107
]), df = /* @__PURE__ */ new Uint8Array([
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
function ff(e, t, n) {
  const r = new Uint8Array(e.length + t.length + n.length);
  return r.set(e, 0), r.set(t, e.length), r.set(n, e.length + t.length), r;
}
class pf {
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
    const i = new Uint8Array(sf);
    i.set(Nn(this.id, 2), 3), this._kdf.init(i);
  }
  async serializePublicKey(t) {
    return await this._prim.serializePublicKey(t);
  }
  async deserializePublicKey(t) {
    return await this._prim.deserializePublicKey(Z(t));
  }
  async serializePrivateKey(t) {
    return await this._prim.serializePrivateKey(t);
  }
  async deserializePrivateKey(t) {
    return await this._prim.deserializePrivateKey(Z(t));
  }
  async importKey(t, n, r = !0) {
    return await this._prim.importKey(t, n, r);
  }
  async generateKeyPair() {
    return await this._prim.generateKeyPair();
  }
  async deriveKeyPair(t) {
    const n = Z(t);
    if (n.byteLength > Ur)
      throw new Ze("Too long ikm");
    return await this._prim.deriveKeyPair(n);
  }
  async encap(t) {
    let n;
    t.ekm === void 0 ? n = await this.generateKeyPair() : _n(t.ekm) ? n = t.ekm : n = await this.deriveKeyPair(t.ekm);
    const r = await this._prim.serializePublicKey(n.publicKey), i = await this._prim.serializePublicKey(t.recipientPublicKey);
    try {
      let o;
      if (t.senderKey === void 0)
        o = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey));
      else {
        const c = _n(t.senderKey) ? t.senderKey.privateKey : t.senderKey, u = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey)), l = new Uint8Array(await this._prim.dh(c, t.recipientPublicKey));
        o = lr(u, l);
      }
      let s;
      if (t.senderKey === void 0)
        s = lr(new Uint8Array(r), new Uint8Array(i));
      else {
        const c = _n(t.senderKey) ? t.senderKey.publicKey : await this._prim.derivePublicKey(t.senderKey), u = await this._prim.serializePublicKey(c);
        s = ff(new Uint8Array(r), new Uint8Array(i), new Uint8Array(u));
      }
      const a = await this._generateSharedSecret(o, s);
      return {
        enc: r,
        sharedSecret: a
      };
    } catch (o) {
      throw new Wd(o);
    }
  }
  async decap(t) {
    const n = Z(t.enc), r = await this._prim.deserializePublicKey(n), i = _n(t.recipientKey) ? t.recipientKey.privateKey : t.recipientKey, o = _n(t.recipientKey) ? t.recipientKey.publicKey : await this._prim.derivePublicKey(t.recipientKey), s = await this._prim.serializePublicKey(o);
    try {
      let a;
      if (t.senderPublicKey === void 0)
        a = new Uint8Array(await this._prim.dh(i, r));
      else {
        const u = new Uint8Array(await this._prim.dh(i, r)), l = new Uint8Array(await this._prim.dh(i, t.senderPublicKey));
        a = lr(u, l);
      }
      let c;
      if (t.senderPublicKey === void 0)
        c = lr(new Uint8Array(n), new Uint8Array(s));
      else {
        const u = await this._prim.serializePublicKey(t.senderPublicKey);
        c = new Uint8Array(n.byteLength + s.byteLength + u.byteLength), c.set(new Uint8Array(n), 0), c.set(new Uint8Array(s), n.byteLength), c.set(new Uint8Array(u), n.byteLength + s.byteLength);
      }
      return await this._generateSharedSecret(a, c);
    } catch (a) {
      throw new Zd(a);
    }
  }
  async _generateSharedSecret(t, n) {
    const r = this._kdf.buildLabeledIkm(lf, t), i = this._kdf.buildLabeledInfo(df, n, this.secretSize);
    return await this._kdf.extractAndExpand(me, r, i, this.secretSize);
  }
}
const dr = ["deriveBits"], hf = /* @__PURE__ */ new Uint8Array([
  100,
  107,
  112,
  95,
  112,
  114,
  107
]), yf = /* @__PURE__ */ new Uint8Array([115, 107]);
class Qt {
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
class gf {
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
      return Promise.reject(new Pr(n));
    }
  }
  async deserializePublicKey(t) {
    try {
      return await this._importRawKey(Z(t), !0);
    } catch (n) {
      throw new cr(n);
    }
  }
  serializePrivateKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new Pr(n));
    }
  }
  async deserializePrivateKey(t) {
    try {
      return await this._importRawKey(Z(t), !1);
    } catch (n) {
      throw new cr(n);
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
      throw new cr(i);
    }
  }
  async generateKeyPair() {
    try {
      const t = await this._curve.utils.randomSecretKey(), n = new Qt(this._algName, t, "private", dr);
      return { publicKey: await this.derivePublicKey(n), privateKey: n };
    } catch (t) {
      throw new Eo(t);
    }
  }
  async deriveKeyPair(t) {
    try {
      const n = Z(t), r = await this._hkdf.labeledExtract(me.buffer, hf, new Uint8Array(n)), i = await this._hkdf.labeledExpand(r, yf, me, this._nSk), o = new Qt(this._algName, new Uint8Array(i), "private", dr);
      return {
        privateKey: o,
        publicKey: await this.derivePublicKey(o)
      };
    } catch (n) {
      throw new ef(n);
    }
  }
  derivePublicKey(t) {
    try {
      const n = this._curve.getPublicKey(t.key);
      return Promise.resolve(new Qt(this._algName, n, "public"));
    } catch (n) {
      return Promise.reject(new cr(n));
    }
  }
  dh(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t.key, n.key).buffer);
    } catch (r) {
      return Promise.reject(new Pr(r));
    }
  }
  _importRawKey(t, n) {
    return new Promise((r, i) => {
      n && t.byteLength !== this._nPk && i(new Error("Invalid length of the key")), !n && t.byteLength !== this._nSk && i(new Error("Invalid length of the key")), r(new Qt(this._algName, new Uint8Array(t), n ? "public" : "private", n ? [] : dr));
    });
  }
  _importJWK(t, n) {
    return new Promise((r, i) => {
      t.kty !== "OKP" && i(new Error(`Invalid kty: ${t.kty}`)), t.crv !== this._algName && i(new Error(`Invalid crv: ${t.crv}`)), n ? (typeof t.d < "u" && i(new Error("Invalid key: `d` should not be set")), typeof t.x != "string" && i(new Error("Invalid key: `x` not found")), r(new Qt(this._algName, js(t.x), "public"))) : (typeof t.d != "string" && i(new Error("Invalid key: `d` not found")), r(new Qt(this._algName, js(t.d), "private", dr)));
    });
  }
}
const wf = ["encrypt", "decrypt"];
function bf(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function jn(e, t = "") {
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new Error(`${n}expected integer >0, got ${e}`);
  }
}
function dt(e, t, n = "") {
  const r = bf(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function Nr(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function mf(e, t) {
  dt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new Error('"digestInto() output" expected to be of length >=' + n);
}
function vf(e) {
  if (typeof e == "bigint") {
    if (!$r(e))
      throw new Error("positive bigint expected, got " + e);
  } else
    jn(e);
  return e;
}
function Mr(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
const xf = /* @__PURE__ */ new Uint32Array([287454020]), Ef = /* @__PURE__ */ new Uint8Array(xf.buffer);
Ef[0];
function Pi(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function qe(e, t) {
  return e << 32 - t | e >>> t;
}
const xc = /* @ts-ignore: to use toHex */ typeof Uint8Array.from([]).toHex == "function" && // @ts-ignore: to use fromHex
typeof Uint8Array.fromHex == "function", kf = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0")), Sf = [
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
function Af(e) {
  if (dt(e), xc)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += kf[e[n]];
  return t;
}
const st = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function Qi(e) {
  if (e >= st._0 && e <= st._9)
    return e - st._0;
  if (e >= st.A && e <= st.F)
    return e - (st.A - 10);
  if (e >= st.a && e <= st.f)
    return e - (st.a - 10);
}
function _f(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (xc)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = Qi(e.charCodeAt(o)), a = Qi(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function If(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  let t = Xe;
  for (let n = 0; n < e.length; n++) {
    const r = Qi(e.charCodeAt(n));
    if (r === void 0)
      throw new Error('hex string expected, got non-hex character "' + e[n] + '" at index ' + n);
    t = t << 4n | Sf[r];
  }
  return t;
}
function Pf(e) {
  jn(e, "numberToBigint");
  let t = e, n = Xe, r = 1n;
  for (; t > 0; )
    t % 2 === 1 && (n += r), t = Math.floor(t / 2), r <<= 1n;
  return n;
}
function Vs(e) {
  return If(Af(eo(dt(e)).reverse()));
}
function Bf(e, t) {
  jn(t), e = vf(e);
  const n = _f(e.toString(16).padStart(t * 2, "0"));
  if (n.length !== t)
    throw new Error("number too large");
  return n;
}
function $f(e, t) {
  return Bf(e, t).reverse();
}
function eo(e) {
  return Uint8Array.from(e);
}
function $r(e) {
  return typeof e == "bigint" && Xe <= e;
}
function Tf(e, t, n) {
  return $r(e) && $r(t) && $r(n) && t <= e && e < n;
}
function qs(e, t, n, r) {
  if (!Tf(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Of(e, t = {}, n = {}) {
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
async function Lf(e = 32) {
  const t = await cf(), n = new Uint8Array(e);
  return t.getRandomValues(n), n;
}
function Rf(e) {
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
function Cf(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash must wrapped by utils.createHasher");
  jn(e.outputLen), jn(e.blockLen);
}
function zf(e, t = {}) {
  const n = (o, s) => e(s).update(o).digest(), r = e(void 0), i = Object.assign(n, {
    outputLen: r.outputLen,
    blockLen: r.blockLen,
    create: (o) => e(o),
    ...t
  });
  return Object.freeze(i);
}
let Ec = class {
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
    }), Cf(t), dt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), Mr(i);
  }
  update(t) {
    return Nr(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    Nr(this), dt(t, this.outputLen, "output"), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const kc = (e, t, n) => new Ec(e, t).update(n).digest();
kc.create = (e, t) => new Ec(e, t);
function Uf(e, t, n) {
  return e & t ^ ~e & n;
}
function Nf(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Mf = class {
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
    }), this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Pi(this.buffer);
  }
  update(t) {
    Nr(this), dt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Pi(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Nr(this), mf(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Mr(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, Pf(this.length * 8), o), this.process(r, 0);
    const a = Pi(t), c = this.outputLen;
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
const bt = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Hf = /* @__PURE__ */ Uint32Array.from([
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
]), mt = /* @__PURE__ */ new Uint32Array(64);
let Ff = class extends Mf {
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
      mt[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = mt[d - 15], f = mt[d - 2], b = qe(h, 7) ^ qe(h, 18) ^ h >>> 3, y = qe(f, 17) ^ qe(f, 19) ^ f >>> 10;
      mt[d] = y + mt[d - 7] + b + mt[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: u, H: l } = this;
    for (let d = 0; d < 64; d++) {
      const h = qe(a, 6) ^ qe(a, 11) ^ qe(a, 25), f = l + h + Uf(a, c, u) + Hf[d] + mt[d] | 0, y = (qe(r, 2) ^ qe(r, 13) ^ qe(r, 22)) + Nf(r, i, o) | 0;
      l = u, u = c, c = a, a = s + f | 0, s = o, o = i, i = r, r = f + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, u = u + this.G | 0, l = l + this.H | 0, this.set(r, i, o, s, a, c, u, l);
  }
  roundClean() {
    Mr(mt);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Mr(this.buffer);
  }
}, Kf = class extends Ff {
  constructor() {
    super(32), Object.defineProperty(this, "A", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[0] | 0
    }), Object.defineProperty(this, "B", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[1] | 0
    }), Object.defineProperty(this, "C", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[2] | 0
    }), Object.defineProperty(this, "D", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[3] | 0
    }), Object.defineProperty(this, "E", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[4] | 0
    }), Object.defineProperty(this, "F", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[5] | 0
    }), Object.defineProperty(this, "G", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[6] | 0
    }), Object.defineProperty(this, "H", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: bt[7] | 0
    });
  }
};
const Df = /* @__PURE__ */ zf(
  () => new Kf(),
  /* @__PURE__ */ Rf(1)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Sc(e, t) {
  const n = e % t;
  return n >= Xe ? n : t + n;
}
function Re(e, t, n) {
  let r = e;
  for (; t-- > Xe; )
    r *= r, r %= n;
  return r;
}
function jf(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Vf(e) {
  return Of(e, {
    adjustScalarBytes: "function",
    powPminus2: "function"
  }), Object.freeze({ ...e });
}
function qf(e) {
  const t = Vf(e), { P: n, type: r, adjustScalarBytes: i, powPminus2: o, randomBytes: s } = t, a = r === "x25519";
  if (!a && r !== "x448")
    throw new Error("invalid type");
  const c = s || Lf, u = a ? 255n : 448n, l = a ? 32 : 56, d = a ? 9n : 5n, h = a ? 121665n : 39081n, f = a ? ur ** 254n : ur ** 447n, b = a ? 8n * ur ** 251n - Jt : 4n * ur ** 445n - Jt, y = f + b + Jt, p = (k) => Sc(k, n), g = v(d);
  function v(k) {
    return $f(p(k), l);
  }
  function A(k) {
    const x = eo(dt(k, l, "uCoordinate"));
    return a && (x[31] &= 127), p(Vs(x));
  }
  function I(k) {
    return Vs(i(eo(dt(k, l, "scalar"))));
  }
  function _(k, x) {
    const S = U(A(x), I(k));
    if (S === Xe)
      throw new Error("invalid private or public key received");
    return v(S);
  }
  function O(k) {
    return _(k, g);
  }
  const C = O, L = _;
  function B(k, x, S) {
    const P = p(k * (x - S));
    return x = p(x - P), S = p(S + P), { x_2: x, x_3: S };
  }
  function U(k, x) {
    qs("u", k, Xe, n), qs("scalar", x, f, y);
    const S = x, P = k;
    let T = Jt, $ = Xe, R = k, z = Jt, M = Xe;
    for (let H = u - 1n; H >= Xe; H--) {
      const ee = S >> H & Jt;
      M ^= ee, { x_2: T, x_3: R } = B(M, T, R), { x_2: $, x_3: z } = B(M, $, z), M = ee;
      const te = T + $, ne = p(te * te), J = T - $, ye = p(J * J), ot = ne - ye, Rt = R + z, Yt = R - z, Sn = p(Yt * te), An = p(Rt * J), Ns = Sn + An, Ms = Sn - An;
      R = p(Ns * Ns), z = p(P * p(Ms * Ms)), T = p(ne * ye), $ = p(ot * (ne + p(h * ot)));
    }
    ({ x_2: T, x_3: R } = B(M, T, R)), { x_2: $, x_3: z } = B(M, $, z);
    const G = o($);
    return p(T * G);
  }
  const N = {
    secretKey: l,
    publicKey: l,
    seed: l
  }, w = async (k) => (k === void 0 && (k = await c(l)), dt(k, N.seed, "seed"), k), E = { randomSecretKey: w };
  return Object.freeze({
    keygen: jf(w, C),
    getSharedSecret: L,
    getPublicKey: C,
    scalarMult: _,
    scalarMultBase: O,
    utils: E,
    GuBytes: g.slice(),
    lengths: N
  });
}
class Gf extends ko {
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
    }), this._rawKey = Z(t);
  }
  async seal(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: Z(t),
      additionalData: Z(r)
    };
    return await this._api.encrypt(i, this._key, Z(n));
  }
  async open(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: Z(t),
      additionalData: Z(r)
    };
    return await this._api.decrypt(i, this._key, Z(n));
  }
  async _setupKey() {
    if (this._key !== void 0)
      return;
    await this._setup();
    const t = await this._importKey(this._rawKey);
    new Uint8Array(this._rawKey).fill(0), this._key = t;
  }
  async _importKey(t) {
    return await this._api.importKey("raw", t, { name: "AES-GCM" }, !0, wf);
  }
}
class Wf {
  constructor() {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: So.Aes128Gcm
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
    return new Gf(t);
  }
}
class Zf extends Wf {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: So.Aes256Gcm
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
function Gs() {
  return new Promise((e, t) => {
    t(new Eo("Not supported"));
  });
}
const Xf = new Uint8Array([115, 101, 99]);
class Ao {
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
    return await Gs();
  }
  async open(t, n) {
    return await Gs();
  }
  async export(t, n) {
    const r = Z(t);
    if (r.byteLength > Ur)
      throw new Ze("Too long exporter context");
    try {
      return await this._kdf.labeledExpand(this.exporterSecret, Xf, new Uint8Array(r), n);
    } catch (i) {
      throw new Xd(i);
    }
  }
}
class Yf extends Ao {
}
class Jf extends Ao {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.enc = i;
  }
}
class Ac extends Ao {
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
    const n = Nn(t.seq, t.baseNonce.byteLength);
    return uf(t.baseNonce, n).buffer;
  }
  incrementSeq(t) {
    if (t.seq > Number.MAX_SAFE_INTEGER)
      throw new Qd("Message limit reached");
    t.seq += 1;
  }
}
var Qf = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, ep = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Tr;
class _c {
  constructor() {
    Tr.set(this, Promise.resolve());
  }
  async lock() {
    let t;
    const n = new Promise((i) => {
      t = i;
    }), r = Qf(this, Tr, "f");
    return ep(this, Tr, n, "f"), await r, t;
  }
}
Tr = /* @__PURE__ */ new WeakMap();
var Ws = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, tp = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Ln;
class np extends Ac {
  constructor() {
    super(...arguments), Ln.set(this, void 0);
  }
  async open(t, n = me.buffer) {
    tp(this, Ln, Ws(this, Ln, "f") ?? new _c(), "f");
    const r = await Ws(this, Ln, "f").lock();
    let i;
    try {
      i = await this._ctx.key.open(this.computeNonce(this._ctx), Z(t), Z(n));
    } catch (o) {
      throw new Jd(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Ln = /* @__PURE__ */ new WeakMap();
var Zs = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, rp = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Rn;
class ip extends Ac {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), Rn.set(this, void 0), this.enc = i;
  }
  async seal(t, n = me.buffer) {
    rp(this, Rn, Zs(this, Rn, "f") ?? new _c(), "f");
    const r = await Zs(this, Rn, "f").lock();
    let i;
    try {
      i = await this._ctx.key.seal(this.computeNonce(this._ctx), Z(t), Z(n));
    } catch (o) {
      throw new Yd(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
Rn = /* @__PURE__ */ new WeakMap();
const op = new Uint8Array([
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
]), sp = new Uint8Array([101, 120, 112]), ap = new Uint8Array([
  105,
  110,
  102,
  111,
  95,
  104,
  97,
  115,
  104
]), cp = new Uint8Array([107, 101, 121]), up = new Uint8Array([
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
]), lp = new Uint8Array([115, 101, 99, 114, 101, 116]), dp = new Uint8Array([
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
class fp extends ko {
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
      throw new Ze("KemId cannot be used");
    if (this._kem = t.kem, typeof t.kdf == "number")
      throw new Ze("KdfId cannot be used");
    if (this._kdf = t.kdf, typeof t.aead == "number")
      throw new Ze("AeadId cannot be used");
    this._aead = t.aead, this._suiteId = new Uint8Array(dp), this._suiteId.set(Nn(this._kem.id, 2), 4), this._suiteId.set(Nn(this._kdf.id, 2), 6), this._suiteId.set(Nn(this._aead.id, 2), 8), this._kdf.init(this._suiteId);
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
    return t.psk !== void 0 ? r = t.senderKey !== void 0 ? wt.AuthPsk : wt.Psk : r = t.senderKey !== void 0 ? wt.Auth : wt.Base, await this._keyScheduleS(r, n.sharedSecret, n.enc, t);
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
    return t.psk !== void 0 ? r = t.senderPublicKey !== void 0 ? wt.AuthPsk : wt.Psk : r = t.senderPublicKey !== void 0 ? wt.Auth : wt.Base, await this._keyScheduleR(r, n, t);
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
  async seal(t, n, r = me.buffer) {
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
  async open(t, n, r = me.buffer) {
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
    const i = r.psk === void 0 ? me : Br(r.psk.id), o = await this._kdf.labeledExtract(me, up, i), s = r.info === void 0 ? me : Br(r.info), a = await this._kdf.labeledExtract(me, ap, s), c = new Uint8Array(1 + o.byteLength + a.byteLength);
    c.set(new Uint8Array([t]), 0), c.set(new Uint8Array(o), 1), c.set(new Uint8Array(a), 1 + o.byteLength);
    const u = r.psk === void 0 ? me : Br(r.psk.key), l = this._kdf.buildLabeledIkm(lp, u), d = this._kdf.buildLabeledInfo(sp, c, this._kdf.hashSize), h = await this._kdf.extractAndExpand(n, l, d, this._kdf.hashSize);
    if (this._aead.id === So.ExportOnly)
      return { aead: this._aead, exporterSecret: h };
    const f = this._kdf.buildLabeledInfo(cp, c, this._aead.keySize), b = await this._kdf.extractAndExpand(n, l, f, this._aead.keySize), y = this._kdf.buildLabeledInfo(op, c, this._aead.nonceSize), p = await this._kdf.extractAndExpand(n, l, y, this._aead.nonceSize);
    return {
      aead: this._aead,
      exporterSecret: h,
      key: b,
      baseNonce: new Uint8Array(p),
      seq: 0
    };
  }
  async _keyScheduleS(t, n, r, i) {
    const o = await this._keySchedule(t, n, i);
    return o.key === void 0 ? new Jf(this._api, this._kdf, o.exporterSecret, r) : new ip(this._api, this._kdf, o, r);
  }
  async _keyScheduleR(t, n, r) {
    const i = await this._keySchedule(t, n, r);
    return i.key === void 0 ? new Yf(this._api, this._kdf, i.exporterSecret) : new np(this._api, this._kdf, i);
  }
  _validateInputLength(t) {
    if (t.info !== void 0 && t.info.byteLength > of)
      throw new Ze("Too long info");
    if (t.psk !== void 0) {
      if (t.psk.key.byteLength < Ks)
        throw new Ze(`PSK must have at least ${Ks} bytes`);
      if (t.psk.key.byteLength > Ur)
        throw new Ze("Too long psk.key");
      if (t.psk.id.byteLength > Ur)
        throw new Ze("Too long psk.id");
    }
  }
}
class pp extends fp {
}
let hp = class extends vc {
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const yp = 1n, Xs = 2n, gp = 3n, wp = 5n, Ic = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn;
function bp(e) {
  const t = 10n, n = 20n, r = 40n, i = 80n, o = Ic, a = e * e % o * e % o, c = Re(a, Xs, o) * a % o, u = Re(c, yp, o) * e % o, l = Re(u, wp, o) * u % o, d = Re(l, t, o) * l % o, h = Re(d, n, o) * d % o, f = Re(h, r, o) * h % o, b = Re(f, i, o) * f % o, y = Re(b, i, o) * f % o, p = Re(y, t, o) * l % o;
  return { pow_p_5_8: Re(p, Xs, o) * e % o, b2: a };
}
function mp(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const vp = /* @__PURE__ */ (() => {
  const e = Ic;
  return qf({
    P: e,
    type: "x25519",
    powPminus2: (t) => {
      const { pow_p_5_8: n, b2: r } = bp(t);
      return Sc(Re(n, gp, e) * r, e);
    },
    adjustScalarBytes: mp
  });
})();
class xp extends vc {
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : Z(t), i = Z(n);
    if (r.byteLength !== this.hashSize)
      return kc(Df, new Uint8Array(r), new Uint8Array(i)).buffer;
    const o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
}
class Ep extends gf {
  constructor(t) {
    super("X25519", 32, vp, t);
  }
  derive(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t, n));
    } catch (r) {
      return Promise.reject(new Pr(r));
    }
  }
}
class kp extends pf {
  constructor() {
    const t = new xp();
    super(Fs.DhkemX25519HkdfSha256, new Ep(t), t), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Fs.DhkemX25519HkdfSha256
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
function Pc(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array" && "BYTES_PER_ELEMENT" in e && e.BYTES_PER_ELEMENT === 1;
}
function Qe(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(`${n}expected number, got ${typeof e}`);
  }
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new RangeError(`${n}expected integer >= 0, got ${e}`);
  }
}
function et(e, t, n = "") {
  const r = Pc(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`, u = s + "expected Uint8Array" + a + ", got " + c;
    throw r ? new RangeError(u) : new TypeError(u);
  }
  return e;
}
function Bc(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new TypeError("Hash must wrapped by utils.createHasher");
  if (Qe(e.outputLen), Qe(e.blockLen), e.outputLen < 1)
    throw new Error('"outputLen" must be >= 1');
  if (e.blockLen < 1)
    throw new Error('"blockLen" must be >= 1');
}
function dn(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function _o(e, t) {
  et(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new RangeError('"digestInto() output" expected to be of length >=' + n);
}
function Sp(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function tt(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Bi(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Ge(e, t) {
  return e << 32 - t | e >>> t;
}
function fr(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const Ap = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function _p(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function Ip(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = _p(e[t]);
  return e;
}
const Ys = Ap ? (e) => e : Ip, $c = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", Pp = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Io(e) {
  if (et(e), $c)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += Pp[e[n]];
  return t;
}
const at = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function Js(e) {
  if (e >= at._0 && e <= at._9)
    return e - at._0;
  if (e >= at.A && e <= at.F)
    return e - (at.A - 10);
  if (e >= at.a && e <= at.f)
    return e - (at.a - 10);
}
function Po(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  if ($c)
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
    const s = Js(e.charCodeAt(o)), a = Js(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new RangeError('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function Bp(...e) {
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
function ci(e, t = {}) {
  const n = (i, o) => e(o).update(i).digest(), r = e(void 0);
  return n.outputLen = r.outputLen, n.blockLen = r.blockLen, n.canXOF = r.canXOF, n.create = (i) => e(i), Object.assign(n, t), Object.freeze(n);
}
function Bo(e = 32) {
  Qe(e, "bytesLength");
  const t = typeof globalThis == "object" ? globalThis.crypto : null;
  if (typeof t?.getRandomValues != "function")
    throw new Error("crypto.getRandomValues must be defined");
  if (e > 65536)
    throw new RangeError(`"bytesLength" expected <= 65536, got ${e}`);
  return t.getRandomValues(new Uint8Array(e));
}
const Tc = (e) => ({
  // Current NIST hashAlgs suffixes used here fit in one DER subidentifier octet.
  // Larger suffix values would need base-128 OID encoding and a different length byte.
  oid: Uint8Array.from([6, 9, 96, 134, 72, 1, 101, 3, 4, 2, e])
});
function $p(e, t, n) {
  return e & t ^ ~e & n;
}
function Tp(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let $o = class {
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
    this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Bi(this.buffer);
  }
  update(t) {
    dn(this), et(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Bi(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    dn(this), _o(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, tt(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Bi(t), c = this.outputLen;
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
const vt = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), we = /* @__PURE__ */ Uint32Array.from([
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
]), pr = /* @__PURE__ */ BigInt(2 ** 32 - 1), Qs = /* @__PURE__ */ BigInt(32);
function Op(e, t = !1) {
  return t ? { h: Number(e & pr), l: Number(e >> Qs & pr) } : { h: Number(e >> Qs & pr) | 0, l: Number(e & pr) | 0 };
}
function Oc(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = Op(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const ea = (e, t, n) => e >>> n, ta = (e, t, n) => e << 32 - n | t >>> n, en = (e, t, n) => e >>> n | t << 32 - n, tn = (e, t, n) => e << 32 - n | t >>> n, hr = (e, t, n) => e << 64 - n | t >>> n - 32, yr = (e, t, n) => e >>> n - 32 | t << 64 - n, Lp = (e, t, n) => e << n | t >>> 32 - n, Rp = (e, t, n) => t << n | e >>> 32 - n, Cp = (e, t, n) => t << n - 32 | e >>> 64 - n, zp = (e, t, n) => e << n - 32 | t >>> 64 - n;
function ct(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const Up = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), Np = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, Mp = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), Hp = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, Fp = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), Kp = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, Dp = /* @__PURE__ */ Uint32Array.from([
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
]), xt = /* @__PURE__ */ new Uint32Array(64);
class jp extends $o {
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
      xt[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const h = xt[d - 15], f = xt[d - 2], b = Ge(h, 7) ^ Ge(h, 18) ^ h >>> 3, y = Ge(f, 17) ^ Ge(f, 19) ^ f >>> 10;
      xt[d] = y + xt[d - 7] + b + xt[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: u, H: l } = this;
    for (let d = 0; d < 64; d++) {
      const h = Ge(a, 6) ^ Ge(a, 11) ^ Ge(a, 25), f = l + h + $p(a, c, u) + Dp[d] + xt[d] | 0, y = (Ge(r, 2) ^ Ge(r, 13) ^ Ge(r, 22)) + Tp(r, i, o) | 0;
      l = u, u = c, c = a, a = s + f | 0, s = o, o = i, i = r, r = f + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, u = u + this.G | 0, l = l + this.H | 0, this.set(r, i, o, s, a, c, u, l);
  }
  roundClean() {
    tt(xt);
  }
  destroy() {
    this.destroyed = !0, this.set(0, 0, 0, 0, 0, 0, 0, 0), tt(this.buffer);
  }
}
class Vp extends jp {
  // We cannot use array here since array allows indexing by variable
  // which means optimizer/compiler cannot use registers.
  A = vt[0] | 0;
  B = vt[1] | 0;
  C = vt[2] | 0;
  D = vt[3] | 0;
  E = vt[4] | 0;
  F = vt[5] | 0;
  G = vt[6] | 0;
  H = vt[7] | 0;
  constructor() {
    super(32);
  }
}
const Lc = Oc([
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
].map((e) => BigInt(e))), qp = Lc[0], Gp = Lc[1], Et = /* @__PURE__ */ new Uint32Array(80), kt = /* @__PURE__ */ new Uint32Array(80);
class Wp extends $o {
  constructor(t) {
    super(128, t, 16, !1);
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: u, El: l, Fh: d, Fl: h, Gh: f, Gl: b, Hh: y, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, u, l, d, h, f, b, y, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, u, l, d, h, f, b, y, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = u | 0, this.El = l | 0, this.Fh = d | 0, this.Fl = h | 0, this.Gh = f | 0, this.Gl = b | 0, this.Hh = y | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let A = 0; A < 16; A++, n += 4)
      Et[A] = t.getUint32(n), kt[A] = t.getUint32(n += 4);
    for (let A = 16; A < 80; A++) {
      const I = Et[A - 15] | 0, _ = kt[A - 15] | 0, O = en(I, _, 1) ^ en(I, _, 8) ^ ea(I, _, 7), C = tn(I, _, 1) ^ tn(I, _, 8) ^ ta(I, _, 7), L = Et[A - 2] | 0, B = kt[A - 2] | 0, U = en(L, B, 19) ^ hr(L, B, 61) ^ ea(L, B, 6), N = tn(L, B, 19) ^ yr(L, B, 61) ^ ta(L, B, 6), w = Mp(C, N, kt[A - 7], kt[A - 16]), E = Hp(w, O, U, Et[A - 7], Et[A - 16]);
      Et[A] = E | 0, kt[A] = w | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: u, Dl: l, Eh: d, El: h, Fh: f, Fl: b, Gh: y, Gl: p, Hh: g, Hl: v } = this;
    for (let A = 0; A < 80; A++) {
      const I = en(d, h, 14) ^ en(d, h, 18) ^ hr(d, h, 41), _ = tn(d, h, 14) ^ tn(d, h, 18) ^ yr(d, h, 41), O = d & f ^ ~d & y, C = h & b ^ ~h & p, L = Fp(v, _, C, Gp[A], kt[A]), B = Kp(L, g, I, O, qp[A], Et[A]), U = L | 0, N = en(r, i, 28) ^ hr(r, i, 34) ^ hr(r, i, 39), w = tn(r, i, 28) ^ yr(r, i, 34) ^ yr(r, i, 39), E = r & o ^ r & a ^ o & a, k = i & s ^ i & c ^ s & c;
      g = y | 0, v = p | 0, y = f | 0, p = b | 0, f = d | 0, b = h | 0, { h: d, l: h } = ct(u | 0, l | 0, B | 0, U | 0), u = a | 0, l = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const x = Up(U, w, k);
      r = Np(x, B, N, E), i = x | 0;
    }
    ({ h: r, l: i } = ct(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = ct(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = ct(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: u, l } = ct(this.Dh | 0, this.Dl | 0, u | 0, l | 0), { h: d, l: h } = ct(this.Eh | 0, this.El | 0, d | 0, h | 0), { h: f, l: b } = ct(this.Fh | 0, this.Fl | 0, f | 0, b | 0), { h: y, l: p } = ct(this.Gh | 0, this.Gl | 0, y | 0, p | 0), { h: g, l: v } = ct(this.Hh | 0, this.Hl | 0, g | 0, v | 0), this.set(r, i, o, s, a, c, u, l, d, h, f, b, y, p, g, v);
  }
  roundClean() {
    tt(Et, kt);
  }
  destroy() {
    this.destroyed = !0, tt(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
class Zp extends Wp {
  Ah = we[0] | 0;
  Al = we[1] | 0;
  Bh = we[2] | 0;
  Bl = we[3] | 0;
  Ch = we[4] | 0;
  Cl = we[5] | 0;
  Dh = we[6] | 0;
  Dl = we[7] | 0;
  Eh = we[8] | 0;
  El = we[9] | 0;
  Fh = we[10] | 0;
  Fl = we[11] | 0;
  Gh = we[12] | 0;
  Gl = we[13] | 0;
  Hh = we[14] | 0;
  Hl = we[15] | 0;
  constructor() {
    super(64);
  }
}
const ue = /* @__PURE__ */ ci(
  () => new Vp(),
  /* @__PURE__ */ Tc(1)
), Xp = /* @__PURE__ */ ci(
  () => new Zp(),
  /* @__PURE__ */ Tc(3)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const q = (e, t, n) => et(e, t, n), Rc = Qe, X = Io, Te = (...e) => Bp(...e), fn = (e) => Po(e), To = Pc, Oo = (e) => Bo(e), Hr = /* @__PURE__ */ BigInt(0), to = /* @__PURE__ */ BigInt(1);
function Ot(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function Lo(e) {
  if (typeof e == "bigint") {
    if (!Or(e))
      throw new RangeError("positive bigint expected, got " + e);
  } else
    Rc(e);
  return e;
}
function Fr(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected number, got type=" + typeof e);
  }
  if (!Number.isSafeInteger(e)) {
    const n = t && `"${t}" `;
    throw new RangeError(n + "expected safe integer, got " + e);
  }
}
function gr(e) {
  const t = Lo(e).toString(16);
  return t.length & 1 ? "0" + t : t;
}
function Cc(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  return e === "" ? Hr : BigInt("0x" + e);
}
function ir(e) {
  return Cc(Io(e));
}
function pn(e) {
  return Cc(Io(no(et(e)).reverse()));
}
function Ro(e, t) {
  if (Qe(t), t === 0)
    throw new RangeError("zero length");
  e = Lo(e);
  const n = e.toString(16);
  if (n.length > t * 2)
    throw new RangeError("number too large");
  return Po(n.padStart(t * 2, "0"));
}
function zc(e, t) {
  return Ro(e, t).reverse();
}
function Yp(e, t) {
  if (e = q(e), t = q(t), e.length !== t.length)
    return !1;
  let n = 0;
  for (let r = 0; r < e.length; r++)
    n |= e[r] ^ t[r];
  return n === 0;
}
function no(e) {
  return Uint8Array.from(q(e));
}
function Jp(e) {
  if (typeof e != "string")
    throw new TypeError("ascii string expected, got " + typeof e);
  return Uint8Array.from(e, (t, n) => {
    const r = t.charCodeAt(0);
    if (t.length !== 1 || r > 127)
      throw new RangeError(`string contains non-ASCII character "${e[n]}" with code ${r} at position ${n}`);
    return r;
  });
}
const Or = (e) => typeof e == "bigint" && Hr <= e;
function Qp(e, t, n) {
  return Or(e) && Or(t) && Or(n) && t <= e && e < n;
}
function Kr(e, t, n, r) {
  if (!Qp(t, n, r))
    throw new RangeError("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Co(e) {
  if (e < Hr)
    throw new Error("expected non-negative bigint, got " + e);
  let t;
  for (t = 0; e > Hr; e >>= to, t += 1)
    ;
  return t;
}
const zo = (e) => (to << BigInt(e)) - to;
function eh(e, t, n) {
  if (Qe(e, "hashLen"), Qe(t, "qByteLen"), typeof n != "function")
    throw new TypeError("hmacFn must be a function");
  const r = (p) => new Uint8Array(p), i = Uint8Array.of(), o = Uint8Array.of(0), s = Uint8Array.of(1), a = 1e3;
  let c = r(e), u = r(e), l = 0;
  const d = () => {
    c.fill(1), u.fill(0), l = 0;
  }, h = (...p) => n(u, Te(c, ...p)), f = (p = i) => {
    u = h(o, p), c = h(), p.length !== 0 && (u = h(s, p), c = h());
  }, b = () => {
    if (l++ >= a)
      throw new Error("drbg: tried max amount of iterations");
    let p = 0;
    const g = [];
    for (; p < t; ) {
      c = h();
      const v = c.slice();
      g.push(v), p += c.length;
    }
    return Te(...g);
  };
  return (p, g) => {
    d(), f(p);
    let v;
    for (; (v = g(b())) === void 0; )
      f();
    return d(), v;
  };
}
function mn(e, t = {}, n = {}) {
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
const na = () => {
  throw new Error("not implemented");
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const be = /* @__PURE__ */ BigInt(0), oe = /* @__PURE__ */ BigInt(1), Mt = /* @__PURE__ */ BigInt(2), Uc = /* @__PURE__ */ BigInt(3), Nc = /* @__PURE__ */ BigInt(4), Mc = /* @__PURE__ */ BigInt(5), th = /* @__PURE__ */ BigInt(7), Hc = /* @__PURE__ */ BigInt(8), nh = /* @__PURE__ */ BigInt(9), Fc = /* @__PURE__ */ BigInt(16);
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
function ra(e, t) {
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
function Uo(e, t, n) {
  const r = e;
  if (!r.eql(r.sqr(t), n))
    throw new Error("Cannot find square root");
}
function Kc(e, t) {
  const n = e, r = (n.ORDER + oe) / Nc, i = n.pow(t, r);
  return Uo(n, i, t), i;
}
function rh(e, t) {
  const n = e, r = (n.ORDER - Mc) / Hc, i = n.mul(t, Mt), o = n.pow(i, r), s = n.mul(t, o), a = n.mul(n.mul(s, Mt), o), c = n.mul(s, n.sub(a, n.ONE));
  return Uo(n, c, t), c;
}
function ih(e) {
  const t = ui(e), n = Dc(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + th) / Fc;
  return ((a, c) => {
    const u = a;
    let l = u.pow(c, s), d = u.mul(l, r);
    const h = u.mul(l, i), f = u.mul(l, o), b = u.eql(u.sqr(d), c), y = u.eql(u.sqr(h), c);
    l = u.cmov(l, d, b), d = u.cmov(f, h, y);
    const p = u.eql(u.sqr(d), c), g = u.cmov(l, d, p);
    return Uo(u, g, c), g;
  });
}
function Dc(e) {
  if (e < Uc)
    throw new Error("sqrt is not defined for small field");
  let t = e - oe, n = 0;
  for (; t % Mt === be; )
    t /= Mt, n++;
  let r = Mt;
  const i = ui(e);
  for (; ia(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return Kc;
  let o = i.pow(r, t);
  const s = (t + oe) / Mt;
  return function(c, u) {
    const l = c;
    if (l.is0(u))
      return u;
    if (ia(l, u) !== 1)
      throw new Error("Cannot find square root");
    let d = n, h = l.mul(l.ONE, o), f = l.pow(u, t), b = l.pow(u, s);
    for (; !l.eql(f, l.ONE); ) {
      if (l.is0(f))
        return l.ZERO;
      let y = 1, p = l.sqr(f);
      for (; !l.eql(p, l.ONE); )
        if (y++, p = l.sqr(p), y === d)
          throw new Error("Cannot find square root");
      const g = oe << BigInt(d - y - 1), v = l.pow(h, g);
      d = y, h = l.sqr(v), f = l.mul(f, h), b = l.mul(b, v);
    }
    return b;
  };
}
function oh(e) {
  return e % Nc === Uc ? Kc : e % Hc === Mc ? rh : e % Fc === nh ? ih(e) : Dc(e);
}
const Ct = (e, t) => (ce(e, t) & oe) === oe, sh = [
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
function ah(e) {
  const t = {
    ORDER: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = sh.reduce((r, i) => (r[i] = "function", r), t);
  if (mn(e, n), Fr(e.BYTES, "BYTES"), Fr(e.BITS, "BITS"), e.BYTES < 1 || e.BITS < 1)
    throw new Error("invalid field: expected BYTES/BITS > 0");
  if (e.ORDER <= oe)
    throw new Error("invalid field: expected ORDER > 1, got " + e.ORDER);
  return e;
}
function ch(e, t, n) {
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
function jc(e, t, n = !1) {
  const r = e, i = new Array(t.length).fill(n ? r.ZERO : void 0), o = t.reduce((a, c, u) => r.is0(c) ? a : (i[u] = a, r.mul(a, c)), r.ONE), s = r.inv(o);
  return t.reduceRight((a, c, u) => r.is0(c) ? a : (i[u] = r.mul(a, i[u]), r.mul(a, c)), s), i;
}
function ia(e, t) {
  const n = e, r = (n.ORDER - oe) / Mt, i = n.pow(t, r), o = n.eql(i, n.ONE), s = n.eql(i, n.ZERO), a = n.eql(i, n.neg(n.ONE));
  if (!o && !s && !a)
    throw new Error("invalid Legendre symbol result");
  return o ? 1 : s ? 0 : -1;
}
function uh(e, t) {
  if (t !== void 0 && Rc(t), e <= be)
    throw new Error("invalid n length: expected positive n, got " + e);
  if (t !== void 0 && t < 1)
    throw new Error("invalid n length: expected positive bit length, got " + t);
  const n = Co(e);
  if (t !== void 0 && t < n)
    throw new Error(`invalid n length: expected bit length (${n}) >= n.length (${t})`);
  const r = t !== void 0 ? t : n, i = Math.ceil(r / 8);
  return { nBitLength: r, nByteLength: i };
}
const oa = /* @__PURE__ */ new WeakMap();
class Vc {
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
    const { nBitLength: i, nByteLength: o } = uh(t, r);
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
    return ch(this, t, n);
  }
  div(t, n) {
    return ce(t * ra(n, this.ORDER), this.ORDER);
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
    return ra(t, this.ORDER);
  }
  sqrt(t) {
    let n = oa.get(this);
    return n || oa.set(this, n = oh(this.ORDER)), n(this, t);
  }
  toBytes(t) {
    return this.isLE ? zc(t, this.BYTES) : Ro(t, this.BYTES);
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
    let c = o ? pn(t) : ir(t);
    if (a && (c = ce(c, s)), !n && !this.isValid(c))
      throw new Error("invalid field element: outside of range 0..ORDER");
    return c;
  }
  // TODO: we don't need it here, move out to separate fn
  invertBatch(t) {
    return jc(this, t);
  }
  // We can't move this out because Fp6, Fp12 implement it
  // and it's unclear what to return in there.
  cmov(t, n, r) {
    return Ot(r, "condition"), r ? n : t;
  }
}
Object.freeze(Vc.prototype);
function ui(e, t = {}) {
  return new Vc(e, t);
}
function qc(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  if (e <= oe)
    throw new Error("field order must be greater than 1");
  const t = Co(e - oe);
  return Math.ceil(t / 8);
}
function Gc(e) {
  const t = qc(e);
  return t + Math.ceil(t / 2);
}
function Wc(e, t, n = !1) {
  q(e);
  const r = e.length, i = qc(t), o = Math.max(Gc(t), 16);
  if (r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? pn(e) : ir(e), a = ce(s, t - oe) + oe;
  return n ? zc(a, i) : Ro(a, i);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const hn = /* @__PURE__ */ BigInt(0), Ht = /* @__PURE__ */ BigInt(1);
function Dr(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function Mn(e, t) {
  const n = jc(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function Zc(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function $i(e, t) {
  Zc(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = zo(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function sa(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += Ht);
  const u = t * r, l = u + Math.abs(a) - 1, d = a === 0, h = a < 0, f = t % 2 !== 0;
  return { nextN: c, offset: l, isZero: d, isNeg: h, isNegF: f, offsetF: u };
}
const Ti = /* @__PURE__ */ new WeakMap(), Xc = /* @__PURE__ */ new WeakMap();
function Oi(e) {
  return Xc.get(e) || 1;
}
function aa(e) {
  if (e !== hn)
    throw new Error("invalid wNAF");
}
class Yc {
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
    for (; n > hn; )
      n & Ht && (r = r.add(i)), i = i.double(), n >>= Ht;
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
    const { windows: r, windowSize: i } = $i(n, this.bits), o = [];
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
    const s = $i(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: u, isZero: l, isNeg: d, isNegF: h, offsetF: f } = sa(r, a, s);
      r = c, l ? o = o.add(Dr(h, n[f])) : i = i.add(Dr(d, n[u]));
    }
    return aa(r), { p: i, f: o };
  }
  /**
   * Implements unsafe EC multiplication using precomputed tables
   * and w-ary non-adjacent form.
   * @param acc - accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = $i(t, this.bits);
    for (let s = 0; s < o.windows && r !== hn; s++) {
      const { nextN: a, offset: c, isZero: u, isNeg: l } = sa(r, s, o);
      if (r = a, !u) {
        const d = n[c];
        i = i.add(l ? d.negate() : d);
      }
    }
    return aa(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Ti.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Ti.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = Oi(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = Oi(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    Zc(n, this.bits), Xc.set(t, n), Ti.delete(t);
  }
  hasCache(t) {
    return Oi(t) !== 1;
  }
}
function lh(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > hn || r > hn; )
    n & Ht && (o = o.add(i)), r & Ht && (s = s.add(i)), i = i.double(), n >>= Ht, r >>= Ht;
  return { p1: o, p2: s };
}
function ca(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return ah(t), t;
  } else
    return ui(e, { isLE: n });
}
function Jc(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const u = t[c];
    if (!(typeof u == "bigint" && u > hn))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = ca(t.p, n.Fp, r), o = ca(t.n, n.Fn, r), a = ["Gx", "Gy", "a", e === "weierstrass" ? "b" : "d"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
function No(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const St = /* @__PURE__ */ BigInt(0), pe = /* @__PURE__ */ BigInt(1), Li = /* @__PURE__ */ BigInt(2), dh = /* @__PURE__ */ BigInt(8);
function fh(e, t, n, r) {
  const i = e.sqr(n), o = e.sqr(r), s = e.add(e.mul(t.a, i), o), a = e.add(e.ONE, e.mul(t.d, e.mul(i, o)));
  return e.eql(s, a);
}
function ph(e, t = {}) {
  const n = t, r = Jc("edwards", e, n, n.FpFnLE), { Fp: i, Fn: o } = r;
  let s = r.CURVE;
  const { h: a } = s;
  mn(n, {}, { uvRatio: "function" });
  const c = Li << BigInt(o.BYTES * 8) - pe, u = (y) => i.create(y), l = n.uvRatio === void 0 ? (y, p) => {
    try {
      return { isValid: !0, value: i.sqrt(i.div(y, p)) };
    } catch {
      return { isValid: !1, value: St };
    }
  } : n.uvRatio;
  if (!fh(i, s, s.Gx, s.Gy))
    throw new Error("bad curve params: generator point");
  function d(y, p, g = !1) {
    const v = g ? pe : St;
    return Kr("coordinate " + y, p, v, c), p;
  }
  function h(y) {
    if (!(y instanceof f))
      throw new Error("EdwardsPoint expected");
  }
  class f {
    // base / generator point
    static BASE = new f(s.Gx, s.Gy, pe, u(s.Gx * s.Gy));
    // zero / infinity / identity point
    static ZERO = new f(St, pe, pe, St);
    // 0, 1, 1, 0
    // math field
    static Fp = i;
    // scalar field
    static Fn = o;
    X;
    Y;
    Z;
    T;
    constructor(p, g, v, A) {
      this.X = d("x", p), this.Y = d("y", g), this.Z = d("z", v, !0), this.T = d("t", A), Object.freeze(this);
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
      const { x: g, y: v } = p || {};
      return d("x", g), d("y", v), new f(g, v, pe, u(g * v));
    }
    // Uses algo from RFC8032 5.1.3.
    static fromBytes(p, g = !1) {
      const v = i.BYTES, { a: A, d: I } = s;
      p = no(q(p, v, "point")), Ot(g, "zip215");
      const _ = no(p), O = p[v - 1];
      _[v - 1] = O & -129;
      const C = pn(_), L = g ? c : i.ORDER;
      Kr("point.y", C, St, L);
      const B = u(C * C), U = u(B - pe), N = u(I * B - A);
      let { isValid: w, value: E } = l(U, N);
      if (!w)
        throw new Error("bad point: invalid y coordinate");
      const k = (E & pe) === pe, x = (O & 128) !== 0;
      if (!g && E === St && x)
        throw new Error("bad point: x=0 and x_0=1");
      return x !== k && (E = u(-E)), f.fromAffine({ x: E, y: C });
    }
    static fromHex(p, g = !1) {
      return f.fromBytes(fn(p), g);
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    precompute(p = 8, g = !0) {
      return b.createCache(this, p), g || this.multiply(Li), this;
    }
    // Useful in fromAffine() - not for fromBytes(), which always created valid points.
    assertValidity() {
      const p = this, { a: g, d: v } = s;
      if (p.is0())
        throw new Error("bad point: ZERO");
      const { X: A, Y: I, Z: _, T: O } = p, C = u(A * A), L = u(I * I), B = u(_ * _), U = u(B * B), N = u(C * g), w = u(B * u(N + L)), E = u(U + u(v * u(C * L)));
      if (w !== E)
        throw new Error("bad point: equation left != right (1)");
      const k = u(A * I), x = u(_ * O);
      if (k !== x)
        throw new Error("bad point: equation left != right (2)");
    }
    // Compare one point to another.
    equals(p) {
      h(p);
      const { X: g, Y: v, Z: A } = this, { X: I, Y: _, Z: O } = p, C = u(g * O), L = u(I * A), B = u(v * O), U = u(_ * A);
      return C === L && B === U;
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
      const { a: p } = s, { X: g, Y: v, Z: A } = this, I = u(g * g), _ = u(v * v), O = u(Li * u(A * A)), C = u(p * I), L = g + v, B = u(u(L * L) - I - _), U = C + _, N = U - O, w = C - _, E = u(B * N), k = u(U * w), x = u(B * w), S = u(N * U);
      return new f(E, k, S, x);
    }
    // Fast algo for adding 2 Extended Points.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#addition-add-2008-hwcd
    // Cost: 9M + 1*a + 1*d + 7add.
    add(p) {
      h(p);
      const { a: g, d: v } = s, { X: A, Y: I, Z: _, T: O } = this, { X: C, Y: L, Z: B, T: U } = p, N = u(A * C), w = u(I * L), E = u(O * v * U), k = u(_ * B), x = u((A + I) * (C + L) - N - w), S = k - E, P = k + E, T = u(w - g * N), $ = u(x * S), R = u(P * T), z = u(x * T), M = u(S * P);
      return new f($, R, M, z);
    }
    subtract(p) {
      return h(p), this.add(p.negate());
    }
    // Constant-time multiplication.
    multiply(p) {
      if (!o.isValidNot0(p))
        throw new RangeError("invalid scalar: expected 1 <= sc < curve.n");
      const { p: g, f: v } = b.cached(this, p, (A) => Mn(f, A));
      return Mn(f, [g, v])[0];
    }
    // Non-constant-time multiplication. Uses double-and-add algorithm.
    // It's faster, but should only be used when you don't care about
    // an exposed private key e.g. sig verification.
    // Keeps the same subgroup-scalar contract: 0 is allowed for public-scalar callers, but
    // n and larger values are rejected instead of being reduced mod n to the identity point.
    multiplyUnsafe(p) {
      if (!o.isValid(p))
        throw new RangeError("invalid scalar: expected 0 <= sc < curve.n");
      return p === St ? f.ZERO : this.is0() || p === pe ? this : b.unsafe(this, p, (g) => Mn(f, g));
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
      return b.unsafe(this, s.n).is0();
    }
    // Converts Extended point to default (x, y) coordinates.
    // Can accept precomputed Z^-1 - for example, from invertBatch.
    toAffine(p) {
      const g = this;
      let v = p;
      const { X: A, Y: I, Z: _ } = g, O = g.is0();
      v == null && (v = O ? dh : i.inv(_));
      const C = u(A * v), L = u(I * v), B = i.mul(_, v);
      if (O)
        return { x: St, y: pe };
      if (B !== pe)
        throw new Error("invZ was invalid");
      return { x: C, y: L };
    }
    clearCofactor() {
      return a === pe ? this : this.multiplyUnsafe(a);
    }
    toBytes() {
      const { x: p, y: g } = this.toAffine(), v = i.toBytes(g);
      return v[v.length - 1] |= p & pe ? 128 : 0, v;
    }
    toHex() {
      return X(this.toBytes());
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const b = new Yc(f, o.BITS);
  return o.BITS >= 8 && f.BASE.precompute(8), Object.freeze(f.prototype), Object.freeze(f), f;
}
class hh {
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
    na();
  }
  static fromHex(t) {
    na();
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
function yh(e, t, n = {}) {
  if (typeof t != "function")
    throw new Error('"hash" function param is required');
  const r = t, i = n;
  mn(i, {}, {
    adjustScalarBytes: "function",
    randomBytes: "function",
    domain: "function",
    prehash: "function",
    zip215: "boolean",
    mapToCurve: "function"
  });
  const { prehash: o } = i, { BASE: s, Fp: a, Fn: c } = e, u = r.outputLen, l = 2 * a.BYTES;
  if (u !== void 0 && (Fr(u, "hash.outputLen"), u !== l))
    throw new Error(`hash.outputLen must be ${l}, got ${u}`);
  const d = i.randomBytes === void 0 ? Oo : i.randomBytes, h = i.adjustScalarBytes === void 0 ? (w) => w : i.adjustScalarBytes, f = i.domain === void 0 ? (w, E, k) => {
    if (Ot(k, "phflag"), E.length || k)
      throw new Error("Contexts/pre-hash are not supported");
    return w;
  } : i.domain;
  function b(w) {
    return c.create(pn(w));
  }
  function y(w) {
    const E = C.secretKey;
    q(w, C.secretKey, "secretKey");
    const k = q(r(w), 2 * E, "hashedSecretKey"), x = h(k.slice(0, E)), S = k.slice(E, 2 * E), P = b(x);
    return { head: x, prefix: S, scalar: P };
  }
  function p(w) {
    const { head: E, prefix: k, scalar: x } = y(w), S = s.multiply(x), P = S.toBytes();
    return { head: E, prefix: k, scalar: x, point: S, pointBytes: P };
  }
  function g(w) {
    return p(w).pointBytes;
  }
  function v(w = Uint8Array.of(), ...E) {
    const k = Te(...E);
    return b(r(f(k, q(w, void 0, "context"), !!o)));
  }
  function A(w, E, k = {}) {
    w = q(w, void 0, "message"), o && (w = o(w));
    const { prefix: x, scalar: S, pointBytes: P } = p(E), T = v(k.context, x, w), $ = s.multiply(T).toBytes(), R = v(k.context, $, P, w), z = c.create(T + R * S);
    if (!c.isValid(z))
      throw new Error("sign failed: invalid s");
    const M = Te($, c.toBytes(z));
    return q(M, C.signature, "result");
  }
  const I = {
    zip215: i.zip215
  };
  function _(w, E, k, x = I) {
    const { context: S } = x, P = x.zip215 === void 0 ? !!I.zip215 : x.zip215, T = C.signature;
    w = q(w, T, "signature"), E = q(E, void 0, "message"), k = q(k, C.publicKey, "publicKey"), P !== void 0 && Ot(P, "zip215"), o && (E = o(E));
    const $ = T / 2, R = w.subarray(0, $), z = pn(w.subarray($, T));
    let M, G, H;
    try {
      M = e.fromBytes(k, P), G = e.fromBytes(R, P), H = s.multiplyUnsafe(z);
    } catch {
      return !1;
    }
    if (!P && M.isSmallOrder())
      return !1;
    const ee = v(S, R, k, E);
    return G.add(M.multiplyUnsafe(ee)).subtract(H).clearCofactor().is0();
  }
  const O = a.BYTES, C = {
    secretKey: O,
    publicKey: O,
    signature: 2 * O,
    seed: O
  };
  function L(w) {
    return w = w === void 0 ? d(C.seed) : w, q(w, C.seed, "seed");
  }
  function B(w) {
    return To(w) && w.length === C.secretKey;
  }
  function U(w, E) {
    try {
      return !!e.fromBytes(w, E === void 0 ? I.zip215 : E);
    } catch {
      return !1;
    }
  }
  const N = {
    getExtendedPublicKey: p,
    randomSecretKey: L,
    isValidSecretKey: B,
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
    toMontgomery(w) {
      const { y: E } = e.fromBytes(w), k = C.publicKey, x = k === 32;
      if (!x && k !== 57)
        throw new Error("only defined for 25519 and 448");
      const S = x ? a.div(pe + E, pe - E) : a.div(E - pe, E + pe);
      return a.toBytes(S);
    },
    toMontgomerySecret(w) {
      const E = C.secretKey;
      q(w, E);
      const k = r(w.subarray(0, E));
      return h(k).subarray(0, E);
    }
  };
  return Object.freeze(C), Object.freeze(N), Object.freeze({
    keygen: No(L, g),
    getPublicKey: g,
    sign: A,
    verify: _,
    utils: N,
    Point: e,
    lengths: C
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const gh = /* @__PURE__ */ BigInt(0), Hn = /* @__PURE__ */ BigInt(1), ua = /* @__PURE__ */ BigInt(2), wh = /* @__PURE__ */ BigInt(5), bh = /* @__PURE__ */ BigInt(8), Vn = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed"), Qc = {
  p: Vn,
  n: BigInt("0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"),
  h: bh,
  a: BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffec"),
  d: BigInt("0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3"),
  Gx: BigInt("0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a"),
  Gy: BigInt("0x6666666666666666666666666666666666666666666666666666666666666658")
};
function mh(e) {
  const t = BigInt(10), n = BigInt(20), r = BigInt(40), i = BigInt(80), o = Vn, a = e * e % o * e % o, c = re(a, ua, o) * a % o, u = re(c, Hn, o) * e % o, l = re(u, wh, o) * u % o, d = re(l, t, o) * l % o, h = re(d, n, o) * d % o, f = re(h, r, o) * h % o, b = re(f, i, o) * f % o, y = re(b, i, o) * f % o, p = re(y, t, o) * l % o;
  return { pow_p_5_8: re(p, ua, o) * e % o, b2: a };
}
function vh(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const ro = /* @__PURE__ */ BigInt("19681161376707505956807079304988542015446066515923890162744021073123829784752");
function eu(e, t) {
  const n = Vn, r = ce(t * t * t, n), i = ce(r * r * t, n), o = mh(e * i).pow_p_5_8;
  let s = ce(e * r * o, n);
  const a = ce(t * s * s, n), c = s, u = ce(s * ro, n), l = a === e, d = a === ce(-e, n), h = a === ce(-e * ro, n);
  return l && (s = c), (d || h) && (s = u), Ct(s, n) && (s = ce(-s, n)), { isValid: l || d, value: s };
}
const Ft = /* @__PURE__ */ ph(Qc, { uvRatio: eu }), zt = Ft.Fp, xh = Ft.Fn;
function Eh(e) {
  return yh(Ft, Xp, Object.assign({ adjustScalarBytes: vh, zip215: !0 }, e));
}
const Bt = /* @__PURE__ */ Eh({}), la = ro, kh = /* @__PURE__ */ BigInt("54469307008909316920995813868745141605393597292927456921205312896311721017578"), da = (e) => eu(Hn, e), Sh = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), Ah = (e) => zt.create(pn(e) & Sh);
class _e extends hh {
  // Do NOT change syntax: the following gymnastics is done,
  // because typescript strips comments, which makes bundlers disable tree-shaking.
  // prettier-ignore
  static BASE = new _e(Ft.BASE);
  // prettier-ignore
  static ZERO = new _e(Ft.ZERO);
  // prettier-ignore
  static Fp = zt;
  // prettier-ignore
  static Fn = xh;
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
    return new _e(Ft.fromAffine(t));
  }
  assertSame(t) {
    if (!(t instanceof _e))
      throw new Error("RistrettoPoint expected");
  }
  init(t) {
    return new _e(t);
  }
  static fromBytes(t) {
    et(t, 32);
    const { a: n, d: r } = Qc, i = Vn, o = (I) => zt.create(I), s = Ah(t);
    if (!Yp(zt.toBytes(s), t) || Ct(s, i))
      throw new Error("invalid ristretto255 encoding 1");
    const a = o(s * s), c = o(Hn + n * a), u = o(Hn - n * a), l = o(c * c), d = o(u * u), h = o(n * r * l - d), { isValid: f, value: b } = da(o(h * d)), y = o(b * u), p = o(b * y * h);
    let g = o((s + s) * y);
    Ct(g, i) && (g = o(-g));
    const v = o(c * p), A = o(g * v);
    if (!f || Ct(A, i) || v === gh)
      throw new Error("invalid ristretto255 encoding 2");
    return new _e(new Ft(g, v, Hn, A));
  }
  /**
   * Converts ristretto-encoded string to ristretto point.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-decode).
   * @param hex - Ristretto-encoded 32 bytes. Not every 32-byte string is valid ristretto encoding
   */
  static fromHex(t) {
    return _e.fromBytes(Po(t));
  }
  /**
   * Encodes ristretto point to Uint8Array.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-encode).
   */
  toBytes() {
    let { X: t, Y: n, Z: r, T: i } = this.ep;
    const o = Vn, s = (p) => zt.create(p), a = s(s(r + n) * s(r - n)), c = s(t * n), u = s(c * c), { value: l } = da(s(a * u)), d = s(l * a), h = s(l * c), f = s(d * h * i);
    let b;
    if (Ct(i * f, o)) {
      let p = s(n * la), g = s(t * la);
      t = p, n = g, b = s(d * kh);
    } else
      b = h;
    Ct(t * f, o) && (n = s(-n));
    let y = s((r - n) * b);
    return Ct(y, o) && (y = s(-y)), zt.toBytes(y);
  }
  /**
   * Compares two Ristretto points.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-equals).
   */
  equals(t) {
    this.assertSame(t);
    const { X: n, Y: r } = this.ep, { X: i, Y: o } = t.ep, s = (u) => zt.create(u), a = s(n * o) === s(r * i), c = s(r * o) === s(n * i);
    return a || c;
  }
  is0() {
    return this.equals(_e.ZERO);
  }
}
Object.freeze(_e.BASE);
Object.freeze(_e.ZERO);
Object.freeze(_e.prototype);
Object.freeze(_e);
class fa {
  oHash;
  iHash;
  blockLen;
  outputLen;
  canXOF = !1;
  finished = !1;
  destroyed = !1;
  constructor(t, n) {
    if (Bc(t), et(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), tt(i);
  }
  update(t) {
    return dn(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    dn(this), _o(t, this), this.finished = !0;
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
const _h = /* @__PURE__ */ (() => {
  const e = ((t, n, r) => new fa(t, n).update(r).digest());
  return e.create = (t, n) => new fa(t, n), e;
})();
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const pa = (e, t) => (e + (e >= 0 ? t : -t) / tu) / t;
function Ih(e, t, n) {
  Kr("scalar", e, Ye, n);
  const [[r, i], [o, s]] = t, a = pa(s * e, n), c = pa(-i * e, n);
  let u = e - a * r - c * o, l = -a * i - c * s;
  const d = u < Ye, h = l < Ye;
  d && (u = -u), h && (l = -l);
  const f = zo(Math.ceil(Co(n) / 2)) + Pt;
  if (u < Ye || u >= f || l < Ye || l >= f)
    throw new Error("splitScalar (endomorphism): failed for k");
  return { k1neg: d, k1: u, k2neg: h, k2: l };
}
function io(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function Ri(e, t) {
  mn(e);
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Ot(n.lowS, "lowS"), Ot(n.prehash, "prehash"), n.format !== void 0 && io(n.format), n;
}
class Ph extends Error {
  constructor(t = "") {
    super(t);
  }
}
const ze = {
  // asn.1 DER encoding utils
  Err: Ph,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = ze;
      if (Fr(e, "tag"), e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (typeof t != "string")
        throw new TypeError('"data" expected string, got type=' + typeof t);
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = gr(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? gr(i.length / 2 | 128) : "";
      return gr(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = ze;
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
      const { Err: t } = ze;
      if (Lo(e), e < Ye)
        throw new t("integer: negative integers are not allowed");
      let n = gr(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = ze;
      if (e.length < 1)
        throw new t("invalid signature integer: empty");
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e.length > 1 && e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return ir(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = ze, i = q(e, void 0, "signature"), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: u, l } = r.decode(2, c);
    if (l.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(u) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = ze, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
};
Object.freeze(ze._tlv);
Object.freeze(ze._int);
Object.freeze(ze);
const Ye = /* @__PURE__ */ BigInt(0), Pt = /* @__PURE__ */ BigInt(1), tu = /* @__PURE__ */ BigInt(2), wr = /* @__PURE__ */ BigInt(3), Bh = /* @__PURE__ */ BigInt(4);
function $h(e, t = {}) {
  const n = Jc("weierstrass", e, t), r = n.Fp, i = n.Fn;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  mn(t, {}, {
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
  const l = ru(r, i);
  function d() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function h(N, w, E) {
    if (u && w.is0())
      return Uint8Array.of(0);
    const { x: k, y: x } = w.toAffine(), S = r.toBytes(k);
    if (Ot(E, "isCompressed"), E) {
      d();
      const P = !r.isOdd(x);
      return Te(nu(P), S);
    } else
      return Te(Uint8Array.of(4), S, r.toBytes(x));
  }
  function f(N) {
    q(N, void 0, "Point");
    const { publicKey: w, publicKeyUncompressed: E } = l, k = N.length, x = N[0], S = N.subarray(1);
    if (u && k === 1 && x === 0)
      return { x: r.ZERO, y: r.ZERO };
    if (k === w && (x === 2 || x === 3)) {
      const P = r.fromBytes(S);
      if (!r.isValid(P))
        throw new Error("bad point: is not on curve, wrong x");
      const T = p(P);
      let $;
      try {
        $ = r.sqrt(T);
      } catch (M) {
        const G = M instanceof Error ? ": " + M.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + G);
      }
      d();
      const R = r.isOdd($);
      return (x & 1) === 1 !== R && ($ = r.neg($)), { x: P, y: $ };
    } else if (k === E && x === 4) {
      const P = r.BYTES, T = r.fromBytes(S.subarray(0, P)), $ = r.fromBytes(S.subarray(P, P * 2));
      if (!g(T, $))
        throw new Error("bad point: is not on curve");
      return { x: T, y: $ };
    } else
      throw new Error(`bad point: got length ${k}, expected compressed=${w} or uncompressed=${E}`);
  }
  const b = t.toBytes === void 0 ? h : t.toBytes, y = t.fromBytes === void 0 ? f : t.fromBytes;
  function p(N) {
    const w = r.sqr(N), E = r.mul(w, N);
    return r.add(r.add(E, r.mul(N, o.a)), o.b);
  }
  function g(N, w) {
    const E = r.sqr(w), k = p(N);
    return r.eql(E, k);
  }
  if (!g(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const v = r.mul(r.pow(o.a, wr), Bh), A = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(v, A)))
    throw new Error("bad curve params: a or b");
  function I(N, w, E = !1) {
    if (!r.isValid(w) || E && r.is0(w))
      throw new Error(`bad point coordinate ${N}`);
    return w;
  }
  function _(N) {
    if (!(N instanceof L))
      throw new Error("Weierstrass Point expected");
  }
  function O(N) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return Ih(N, c.basises, i.ORDER);
  }
  function C(N, w, E, k, x) {
    return E = new L(r.mul(E.X, N), E.Y, E.Z), w = Dr(k, w), E = Dr(x, E), w.add(E);
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
    constructor(w, E, k) {
      this.X = I("x", w), this.Y = I("y", E, !0), this.Z = I("z", k), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(w) {
      const { x: E, y: k } = w || {};
      if (!w || !r.isValid(E) || !r.isValid(k))
        throw new Error("invalid affine point");
      if (w instanceof L)
        throw new Error("projective point not allowed");
      return r.is0(E) && r.is0(k) ? L.ZERO : new L(E, k, r.ONE);
    }
    static fromBytes(w) {
      const E = L.fromAffine(y(q(w, void 0, "point")));
      return E.assertValidity(), E;
    }
    static fromHex(w) {
      return L.fromBytes(fn(w));
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
    precompute(w = 8, E = !0) {
      return U.createCache(this, w), E || this.multiply(wr), this;
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
      const { x: E, y: k } = w.toAffine();
      if (!r.isValid(E) || !r.isValid(k))
        throw new Error("bad point: x or y not field elements");
      if (!g(E, k))
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
      _(w);
      const { X: E, Y: k, Z: x } = this, { X: S, Y: P, Z: T } = w, $ = r.eql(r.mul(E, T), r.mul(S, x)), R = r.eql(r.mul(k, T), r.mul(P, x));
      return $ && R;
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
      const { a: w, b: E } = o, k = r.mul(E, wr), { X: x, Y: S, Z: P } = this;
      let T = r.ZERO, $ = r.ZERO, R = r.ZERO, z = r.mul(x, x), M = r.mul(S, S), G = r.mul(P, P), H = r.mul(x, S);
      return H = r.add(H, H), R = r.mul(x, P), R = r.add(R, R), T = r.mul(w, R), $ = r.mul(k, G), $ = r.add(T, $), T = r.sub(M, $), $ = r.add(M, $), $ = r.mul(T, $), T = r.mul(H, T), R = r.mul(k, R), G = r.mul(w, G), H = r.sub(z, G), H = r.mul(w, H), H = r.add(H, R), R = r.add(z, z), z = r.add(R, z), z = r.add(z, G), z = r.mul(z, H), $ = r.add($, z), G = r.mul(S, P), G = r.add(G, G), z = r.mul(G, H), T = r.sub(T, z), R = r.mul(G, M), R = r.add(R, R), R = r.add(R, R), new L(T, $, R);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(w) {
      _(w);
      const { X: E, Y: k, Z: x } = this, { X: S, Y: P, Z: T } = w;
      let $ = r.ZERO, R = r.ZERO, z = r.ZERO;
      const M = o.a, G = r.mul(o.b, wr);
      let H = r.mul(E, S), ee = r.mul(k, P), te = r.mul(x, T), ne = r.add(E, k), J = r.add(S, P);
      ne = r.mul(ne, J), J = r.add(H, ee), ne = r.sub(ne, J), J = r.add(E, x);
      let ye = r.add(S, T);
      return J = r.mul(J, ye), ye = r.add(H, te), J = r.sub(J, ye), ye = r.add(k, x), $ = r.add(P, T), ye = r.mul(ye, $), $ = r.add(ee, te), ye = r.sub(ye, $), z = r.mul(M, J), $ = r.mul(G, te), z = r.add($, z), $ = r.sub(ee, z), z = r.add(ee, z), R = r.mul($, z), ee = r.add(H, H), ee = r.add(ee, H), te = r.mul(M, te), J = r.mul(G, J), ee = r.add(ee, te), te = r.sub(H, te), te = r.mul(M, te), J = r.add(J, te), H = r.mul(ee, J), R = r.add(R, H), H = r.mul(ye, J), $ = r.mul(ne, $), $ = r.sub($, H), H = r.mul(ne, ee), z = r.mul(ye, z), z = r.add(z, H), new L($, R, z);
    }
    subtract(w) {
      return _(w), this.add(w.negate());
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
      const { endo: E } = t;
      if (!i.isValidNot0(w))
        throw new RangeError("invalid scalar: out of range");
      let k, x;
      const S = (P) => U.cached(this, P, (T) => Mn(L, T));
      if (E) {
        const { k1neg: P, k1: T, k2neg: $, k2: R } = O(w), { p: z, f: M } = S(T), { p: G, f: H } = S(R);
        x = M.add(H), k = C(E.beta, z, G, P, $);
      } else {
        const { p: P, f: T } = S(w);
        k = P, x = T;
      }
      return Mn(L, [k, x])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(w) {
      const { endo: E } = t, k = this, x = w;
      if (!i.isValid(x))
        throw new RangeError("invalid scalar: out of range");
      if (x === Ye || k.is0())
        return L.ZERO;
      if (x === Pt)
        return k;
      if (U.hasCache(this))
        return this.multiply(x);
      if (E) {
        const { k1neg: S, k1: P, k2neg: T, k2: $ } = O(x), { p1: R, p2: z } = lh(L, k, P, $);
        return C(E.beta, R, z, S, T);
      } else
        return U.unsafe(k, x);
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * (X, Y, Z) ∋ (x=X/Z, y=Y/Z).
     * @param invertedZ - Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(w) {
      const E = this;
      let k = w;
      const { X: x, Y: S, Z: P } = E;
      if (r.eql(P, r.ONE))
        return { x, y: S };
      const T = E.is0();
      k == null && (k = T ? r.ONE : r.inv(P));
      const $ = r.mul(x, k), R = r.mul(S, k), z = r.mul(P, k);
      if (T)
        return { x: r.ZERO, y: r.ZERO };
      if (!r.eql(z, r.ONE))
        throw new Error("invZ was invalid");
      return { x: $, y: R };
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: w } = t;
      return s === Pt ? !0 : w ? w(L, this) : U.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: w } = t;
      return s === Pt ? this : w ? w(L, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return s === Pt ? this.is0() : this.clearCofactor().is0();
    }
    toBytes(w = !0) {
      return Ot(w, "isCompressed"), this.assertValidity(), b(L, this, w);
    }
    toHex(w = !0) {
      return X(this.toBytes(w));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const B = i.BITS, U = new Yc(L, t.endo ? Math.ceil(B / 2) : B);
  return B >= 8 && L.BASE.precompute(8), Object.freeze(L.prototype), Object.freeze(L), L;
}
function nu(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function ru(e, t) {
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
function Th(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes === void 0 ? Oo : t.randomBytes, i = Object.assign(ru(e.Fp, n), {
    seed: Math.max(Gc(n.ORDER), 16)
  });
  function o(f) {
    try {
      const b = n.fromBytes(f);
      return n.isValidNot0(b);
    } catch {
      return !1;
    }
  }
  function s(f, b) {
    const { publicKey: y, publicKeyUncompressed: p } = i;
    try {
      const g = f.length;
      return b === !0 && g !== y || b === !1 && g !== p ? !1 : !!e.fromBytes(f);
    } catch {
      return !1;
    }
  }
  function a(f) {
    return f = f === void 0 ? r(i.seed) : f, Wc(q(f, i.seed, "seed"), n.ORDER);
  }
  function c(f, b = !0) {
    return e.BASE.multiply(n.fromBytes(f)).toBytes(b);
  }
  function u(f) {
    const { secretKey: b, publicKey: y, publicKeyUncompressed: p } = i, g = n._lengths;
    if (!To(f))
      return;
    const v = q(f, void 0, "key").length, A = v === y || v === p, I = v === b || !!g?.includes(v);
    if (!(A && I))
      return A;
  }
  function l(f, b, y = !0) {
    if (u(f) === !0)
      throw new Error("first arg must be private key");
    if (u(b) === !1)
      throw new Error("second arg must be public key");
    const p = n.fromBytes(f);
    return e.fromBytes(b).multiply(p).toBytes(y);
  }
  const d = {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a
  }, h = No(a, c);
  return Object.freeze(d), Object.freeze(i), Object.freeze({ getPublicKey: c, getSharedSecret: l, keygen: h, Point: e, utils: d, lengths: i });
}
function Oh(e, t, n = {}) {
  const r = t;
  Bc(r), mn(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  }), n = Object.assign({}, n);
  const i = n.randomBytes === void 0 ? Oo : n.randomBytes, o = n.hmac === void 0 ? (x, S) => _h(r, x, S) : n.hmac, { Fp: s, Fn: a } = e, { ORDER: c, BITS: u } = a, { keygen: l, getPublicKey: d, getSharedSecret: h, utils: f, lengths: b } = Th(e, n), y = {
    prehash: !0,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !0,
    format: "compact",
    extraEntropy: !1
  }, p = c * tu + Pt < s.ORDER;
  function g(x) {
    const S = c >> Pt;
    return x > S;
  }
  function v(x, S) {
    if (!a.isValidNot0(S))
      throw new Error(`invalid signature ${x}: out of range 1..Point.Fn.ORDER`);
    return S;
  }
  function A() {
    if (p)
      throw new Error('"recovered" sig type is not supported for cofactor >2 curves');
  }
  function I(x, S) {
    io(S);
    const P = b.signature, T = S === "compact" ? P : S === "recovered" ? P + 1 : void 0;
    return q(x, T);
  }
  class _ {
    r;
    s;
    recovery;
    constructor(S, P, T) {
      if (this.r = v("r", S), this.s = v("s", P), T != null) {
        if (A(), ![0, 1, 2, 3].includes(T))
          throw new Error("invalid recovery id");
        this.recovery = T;
      }
      Object.freeze(this);
    }
    static fromBytes(S, P = y.format) {
      I(S, P);
      let T;
      if (P === "der") {
        const { r: M, s: G } = ze.toSig(q(S));
        return new _(M, G);
      }
      P === "recovered" && (T = S[0], P = "compact", S = S.subarray(1));
      const $ = b.signature / 2, R = S.subarray(0, $), z = S.subarray($, $ * 2);
      return new _(a.fromBytes(R), a.fromBytes(z), T);
    }
    static fromHex(S, P) {
      return this.fromBytes(fn(S), P);
    }
    assertRecovery() {
      const { recovery: S } = this;
      if (S == null)
        throw new Error("invalid recovery id: must be present");
      return S;
    }
    addRecoveryBit(S) {
      return new _(this.r, this.s, S);
    }
    // Unlike the top-level helper below, this method expects a digest that has
    // already been hashed to the curve's message representative.
    recoverPublicKey(S) {
      const { r: P, s: T } = this, $ = this.assertRecovery(), R = $ === 2 || $ === 3 ? P + c : P;
      if (!s.isValid(R))
        throw new Error("invalid recovery id: sig.r+curve.n != R.x");
      const z = s.toBytes(R), M = e.fromBytes(Te(nu(($ & 1) === 0), z)), G = a.inv(R), H = C(q(S, void 0, "msgHash")), ee = a.create(-H * G), te = a.create(T * G), ne = e.BASE.multiplyUnsafe(ee).add(M.multiplyUnsafe(te));
      if (ne.is0())
        throw new Error("invalid recovery: point at infinify");
      return ne.assertValidity(), ne;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return g(this.s);
    }
    toBytes(S = y.format) {
      if (io(S), S === "der")
        return fn(ze.hexFromSig(this));
      const { r: P, s: T } = this, $ = a.toBytes(P), R = a.toBytes(T);
      return S === "recovered" ? (A(), Te(Uint8Array.of(this.assertRecovery()), $, R)) : Te($, R);
    }
    toHex(S) {
      return X(this.toBytes(S));
    }
  }
  Object.freeze(_.prototype), Object.freeze(_);
  const O = n.bits2int === void 0 ? function(S) {
    if (S.length > 8192)
      throw new Error("input is too large");
    const P = ir(S), T = S.length * 8 - u;
    return T > 0 ? P >> BigInt(T) : P;
  } : n.bits2int, C = n.bits2int_modN === void 0 ? function(S) {
    return a.create(O(S));
  } : n.bits2int_modN, L = zo(u);
  function B(x) {
    return Kr("num < 2^" + u, x, Ye, L), a.toBytes(x);
  }
  function U(x, S) {
    return q(x, void 0, "message"), S ? q(r(x), void 0, "prehashed message") : x;
  }
  function N(x, S, P) {
    const { lowS: T, prehash: $, extraEntropy: R } = Ri(P, y);
    x = U(x, $);
    const z = C(x), M = a.fromBytes(S);
    if (!a.isValidNot0(M))
      throw new Error("invalid private key");
    const G = [B(M), B(z)];
    if (R != null && R !== !1) {
      const ne = R === !0 ? i(b.secretKey) : R;
      G.push(q(ne, void 0, "extraEntropy"));
    }
    const H = Te(...G), ee = z;
    function te(ne) {
      const J = O(ne);
      if (!a.isValidNot0(J))
        return;
      const ye = a.inv(J), ot = e.BASE.multiply(J).toAffine(), Rt = a.create(ot.x);
      if (Rt === Ye)
        return;
      const Yt = a.create(ye * a.create(ee + Rt * M));
      if (Yt === Ye)
        return;
      let Sn = (ot.x === Rt ? 0 : 2) | Number(ot.y & Pt), An = Yt;
      return T && g(Yt) && (An = a.neg(Yt), Sn ^= 1), new _(Rt, An, p ? void 0 : Sn);
    }
    return { seed: H, k2sig: te };
  }
  function w(x, S, P = {}) {
    const { seed: T, k2sig: $ } = N(x, S, P);
    return eh(r.outputLen, a.BYTES, o)(T, $).toBytes(P.format);
  }
  function E(x, S, P, T = {}) {
    const { lowS: $, prehash: R, format: z } = Ri(T, y);
    if (P = q(P, void 0, "publicKey"), S = U(S, R), !To(x)) {
      const M = x instanceof _ ? ", use sig.toBytes()" : "";
      throw new Error("verify expects Uint8Array signature" + M);
    }
    I(x, z);
    try {
      const M = _.fromBytes(x, z), G = e.fromBytes(P);
      if ($ && M.hasHighS())
        return !1;
      const { r: H, s: ee } = M, te = C(S), ne = a.inv(ee), J = a.create(te * ne), ye = a.create(H * ne), ot = e.BASE.multiplyUnsafe(J).add(G.multiplyUnsafe(ye));
      return ot.is0() ? !1 : a.create(ot.x) === H;
    } catch {
      return !1;
    }
  }
  function k(x, S, P = {}) {
    const { prehash: T } = Ri(P, y);
    return S = U(S, T), _.fromBytes(x, "recovered").recoverPublicKey(S).toBytes();
  }
  return Object.freeze({
    keygen: l,
    getPublicKey: d,
    getSharedSecret: h,
    utils: f,
    lengths: b,
    Point: e,
    sign: w,
    verify: E,
    recoverPublicKey: k,
    Signature: _,
    hash: r
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const li = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, Lh = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, Rh = /* @__PURE__ */ BigInt(0), oo = /* @__PURE__ */ BigInt(2);
function Ch(e) {
  const t = li.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), u = e * e * e % t, l = u * u * e % t, d = re(l, n, t) * l % t, h = re(d, n, t) * l % t, f = re(h, oo, t) * u % t, b = re(f, i, t) * f % t, y = re(b, o, t) * b % t, p = re(y, a, t) * y % t, g = re(p, c, t) * p % t, v = re(g, a, t) * y % t, A = re(v, n, t) * l % t, I = re(A, s, t) * b % t, _ = re(I, r, t) * u % t, O = re(_, oo, t);
  if (!jr.eql(jr.sqr(O), e))
    throw new Error("Cannot find square root");
  return O;
}
const jr = ui(li.p, { sqrt: Ch }), Wt = /* @__PURE__ */ $h(li, {
  Fp: jr,
  endo: Lh
}), Ee = /* @__PURE__ */ Oh(Wt, ue), ha = {};
function Vr(e, ...t) {
  let n = ha[e];
  if (n === void 0) {
    const r = ue(Jp(e));
    n = Te(r, r), ha[e] = n;
  }
  return ue(Te(n, ...t));
}
const di = (e) => e.toBytes(!0).slice(1), fi = (e) => e % oo === Rh;
function iu(e) {
  const { Fn: t, BASE: n } = Wt, r = t.fromBytes(e), i = n.multiply(r);
  return { scalar: fi(i.y) ? r : t.neg(r), bytes: di(i) };
}
function ou(e) {
  const t = jr;
  if (!t.isValidNot0(e))
    throw new Error("invalid x: Fail if x ≥ p");
  const n = t.create(e * e), r = t.create(n * e + BigInt(7));
  let i = t.sqrt(r);
  fi(i) || (i = t.neg(i));
  const o = Wt.fromAffine({ x: e, y: i });
  return o.assertValidity(), o;
}
const cn = ir;
function su(...e) {
  return Wt.Fn.create(cn(Vr("BIP0340/challenge", ...e)));
}
function ya(e) {
  return iu(e).bytes;
}
function zh(e, t, n = Bo(32)) {
  const { Fn: r, BASE: i } = Wt, o = q(e, void 0, "message"), { bytes: s, scalar: a } = iu(t), c = q(n, 32, "auxRand"), u = r.toBytes(a ^ cn(Vr("BIP0340/aux", c))), l = Vr("BIP0340/nonce", u, s, o), d = r.create(cn(l));
  if (d === 0n)
    throw new Error("sign failed: k is zero");
  const h = i.multiply(d), f = fi(h.y) ? d : r.neg(d), b = di(h), y = su(b, s, o), p = new Uint8Array(64);
  if (p.set(b, 0), p.set(r.toBytes(r.create(f + y * a)), 32), !au(p, o, s))
    throw new Error("sign: Invalid signature produced");
  return p;
}
function au(e, t, n) {
  const { Fp: r, Fn: i, BASE: o } = Wt, s = q(e, 64, "signature"), a = q(t, void 0, "message"), c = q(n, 32, "publicKey");
  try {
    const u = ou(cn(c)), l = cn(s.subarray(0, 32));
    if (!r.isValidNot0(l))
      return !1;
    const d = cn(s.subarray(32, 64));
    if (!i.isValidNot0(d))
      return !1;
    const h = su(i.toBytes(l), di(u), a), f = o.multiplyUnsafe(d).add(u.multiplyUnsafe(i.neg(h))), { x: b, y } = f.toAffine();
    return !(f.is0() || !fi(y) || b !== l);
  } catch {
    return !1;
  }
}
const Ut = /* @__PURE__ */ (() => {
  const n = (r) => (r = r === void 0 ? Bo(48) : r, Wc(r, li.n));
  return Object.freeze({
    keygen: No(n, ya),
    getPublicKey: ya,
    sign: zh,
    verify: au,
    Point: Wt,
    utils: Object.freeze({
      randomSecretKey: n,
      taggedHash: Vr,
      lift_x: ou,
      pointToBytes: di
    }),
    lengths: Object.freeze({
      secretKey: 32,
      publicKey: 32,
      publicKeyHasPrefix: !1,
      signature: 64,
      seed: 48
    })
  });
})(), Uh = /* @__PURE__ */ Uint8Array.from([
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
]), cu = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), Nh = cu.map((e) => (9 * e + 5) % 16), uu = /* @__PURE__ */ (() => {
  const n = [[cu], [Nh]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => Uh[o]));
  return n;
})(), lu = uu[0], du = uu[1], fu = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), Mh = /* @__PURE__ */ lu.map((e, t) => e.map((n) => fu[t][n])), Hh = /* @__PURE__ */ du.map((e, t) => e.map((n) => fu[t][n])), Fh = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), Kh = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function ga(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const br = /* @__PURE__ */ new Uint32Array(16);
class Dh extends $o {
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
      br[f] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, u = this.h3 | 0, l = u, d = this.h4 | 0, h = d;
    for (let f = 0; f < 5; f++) {
      const b = 4 - f, y = Fh[f], p = Kh[f], g = lu[f], v = du[f], A = Mh[f], I = Hh[f];
      for (let _ = 0; _ < 16; _++) {
        const O = fr(r + ga(f, o, a, u) + br[g[_]] + y, A[_]) + d | 0;
        r = d, d = u, u = fr(a, 10) | 0, a = o, o = O;
      }
      for (let _ = 0; _ < 16; _++) {
        const O = fr(i + ga(b, s, c, l) + br[v[_]] + p, I[_]) + h | 0;
        i = h, h = l, l = fr(c, 10) | 0, c = s, s = O;
      }
    }
    this.set(this.h1 + a + l | 0, this.h2 + u + h | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    tt(br);
  }
  destroy() {
    this.destroyed = !0, tt(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const pu = /* @__PURE__ */ ci(() => new Dh()), jh = BigInt(0), In = BigInt(1), Vh = BigInt(2), qh = BigInt(7), Gh = BigInt(256), Wh = BigInt(113), hu = [], yu = [], gu = [];
for (let e = 0, t = In, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], hu.push(2 * (5 * r + n)), yu.push((e + 1) * (e + 2) / 2 % 64);
  let i = jh;
  for (let o = 0; o < 7; o++)
    t = (t << In ^ (t >> qh) * Wh) % Gh, t & Vh && (i ^= In << (In << BigInt(o)) - In);
  gu.push(i);
}
const wu = Oc(gu, !0), Zh = wu[0], Xh = wu[1], wa = (e, t, n) => n > 32 ? Cp(e, t, n) : Lp(e, t, n), ba = (e, t, n) => n > 32 ? zp(e, t, n) : Rp(e, t, n);
function Yh(e, t = 24) {
  if (Qe(t, "rounds"), t < 1 || t > 24)
    throw new Error('"rounds" expected integer 1..24');
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, u = n[c], l = n[c + 1], d = wa(u, l, 1) ^ n[a], h = ba(u, l, 1) ^ n[a + 1];
      for (let f = 0; f < 50; f += 10)
        e[s + f] ^= d, e[s + f + 1] ^= h;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = yu[s], c = wa(i, o, a), u = ba(i, o, a), l = hu[s];
      i = e[l], o = e[l + 1], e[l] = c, e[l + 1] = u;
    }
    for (let s = 0; s < 50; s += 10) {
      const a = e[s], c = e[s + 1], u = e[s + 2], l = e[s + 3];
      e[s] ^= ~e[s + 2] & e[s + 4], e[s + 1] ^= ~e[s + 3] & e[s + 5], e[s + 2] ^= ~e[s + 4] & e[s + 6], e[s + 3] ^= ~e[s + 5] & e[s + 7], e[s + 4] ^= ~e[s + 6] & e[s + 8], e[s + 5] ^= ~e[s + 7] & e[s + 9], e[s + 6] ^= ~e[s + 8] & a, e[s + 7] ^= ~e[s + 9] & c, e[s + 8] ^= ~a & u, e[s + 9] ^= ~c & l;
    }
    e[0] ^= Zh[r], e[1] ^= Xh[r];
  }
  tt(n);
}
let Jh = class bu {
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
    if (this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.canXOF = i, this.rounds = o, Qe(r, "outputLen"), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = Sp(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    Ys(this.state32), Yh(this.state32, this.rounds), Ys(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    dn(this), et(t);
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
    dn(this, !1), et(t), this.finish();
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
    return Qe(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (_o(t, this), this.finished)
      throw new Error("digest() was already called");
    this.writeInto(t.subarray(0, this.outputLen)), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.outputLen);
    return this.digestInto(t), t;
  }
  destroy() {
    this.destroyed = !0, tt(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t ||= new bu(n, r, i, s, o), t.blockLen = n, t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.canXOF = this.canXOF, t.destroyed = this.destroyed, t;
  }
};
const Qh = (e, t, n, r = {}) => ci(() => new Jh(t, e, n), r), mu = /* @__PURE__ */ Qh(1, 136, 32), ey = 1024 * 1024, vu = Object.getPrototypeOf(Uint8Array.prototype), ty = Object.getOwnPropertyDescriptor(vu, Symbol.toStringTag).get, ny = Object.getOwnPropertyDescriptor(vu, "byteLength").get, ry = Uint8Array.prototype.set;
function xu(e) {
  if (!Number.isSafeInteger(e) || e < 0) throw new Error("exact message byte length is invalid");
  if (e > ey) throw new Error("exact message exceeds the 1 MiB decoded limit");
}
function Eu(e) {
  const t = e.startsWith("0x") ? 2 : 0;
  xu(Math.ceil((e.length - t) / 2));
}
function Mo(e) {
  if (typeof e == "string")
    return Eu(e), fn(e.startsWith("0x") ? e.slice(2) : e);
  if (ty.call(e) !== "Uint8Array") throw new TypeError("exact message must be hex or Uint8Array");
  const t = ny.call(e);
  xu(t);
  const n = new Uint8Array(t);
  return ry.call(n, e), n;
}
const iy = "hpke-x25519-hkdfsha256-aes256gcm-v1", oy = "avault:blind-box:v1", sy = "avault:blind-box:aad:v1", qr = "machine-aesgcm-v1", pi = 1, hi = "ecdsa-secp256k1-recoverable", qn = "ed25519", Ho = "ecdsa-secp256k1-der", Fo = "schnorr-secp256k1-bip340", Gn = "schnorr-secp256k1-bip341-keypath", se = 32, Gr = 12, yi = 32, ay = "avault:protected-vmk:kek-passkey:v1", cy = /^[A-Za-z_][A-Za-z0-9_]*$/, uy = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[a-z][a-z0-9_]{0,63})?$/, ly = "avault:protected-root-meta:v1", dy = new TextEncoder(), fy = new TextDecoder();
function Ko(e) {
  switch (e) {
    case qn:
    case hi:
    case Ho:
    case Fo:
    case Gn:
      return e;
    default:
      throw new Error("unsupported signing scheme");
  }
}
function vn(e) {
  if (e === void 0 || e === "secp256k1") return "secp256k1";
  if (e === "ed25519") return e;
  throw new Error("unsupported signing curve");
}
function Do(e) {
  if (Ko(e), e === qn) throw new Error("Ed25519 requires exact-message signing, not a digest");
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
function Pe(e) {
  const t = ae(e), n = new ArrayBuffer(t.byteLength);
  return new Uint8Array(n).set(t), t.fill(0), n;
}
function ve(e) {
  return dy.encode(e);
}
function Wr(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(Wr).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${Wr(t[n])}`).join(",")}}`;
}
function jt(e) {
  const t = new Uint8Array(e);
  return crypto.getRandomValues(t), t;
}
function Y(e) {
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
  return fn(e.startsWith("0x") ? e.slice(2) : e);
}
function ma(e) {
  return /^(0x)?[0-9a-fA-F]+$/.test(e) && (e.startsWith("0x"), e.length % 2 === 0) ? Oe(e) : fe(e);
}
function py(e) {
  return X(ae(e));
}
function ku(e, t = "bytes") {
  return ae(e, t);
}
function he(e, t, n) {
  if (e.length !== t)
    throw new Error(`${n} must be ${t} bytes`);
}
function Me(e) {
  e && new Uint8Array(e).fill(0);
}
async function Zr(e, t, n, r) {
  let i, o, s, a;
  try {
    i = Pe(e), o = Pe(t), s = Pe(n), a = r ? Pe(r) : void 0;
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
    Me(i), Me(o), Me(s), Me(a);
  }
}
async function gi(e, t, n, r) {
  let i, o, s, a;
  try {
    i = Pe(e), o = Pe(t), s = Pe(n), a = r ? Pe(r) : void 0;
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
    Me(i), Me(o), Me(s), Me(a);
  }
}
async function hy(e, t, n, r) {
  let i, o, s;
  try {
    i = Pe(e), o = Pe(t), s = Pe(n);
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
    Me(i), Me(o), Me(s);
  }
}
async function Su(e, t) {
  const n = ae(e, "passkey PRF output");
  try {
    const r = ae(t, "passkey PRF salt");
    try {
      if (n.length === 0)
        throw new Error("passkey PRF output is empty");
      return he(r, yi, "passkey PRF salt"), await hy(n, r, ve(ay), se);
    } finally {
      r.fill(0);
    }
  } finally {
    n.fill(0);
  }
}
function yy() {
  return jt(se);
}
function gy() {
  return jt(yi);
}
function wy(e) {
  const t = ae(e, "passkey PRF salt");
  try {
    return he(t, yi, "passkey PRF salt"), { prf: { eval: { first: Pe(t) } } };
  } finally {
    t.fill(0);
  }
}
function rt(e) {
  const t = typeof e == "string" ? JSON.parse(e) : e;
  if (t?.v !== pi || !Array.isArray(t.copies))
    throw new Error("invalid protected wrap_meta");
  return t;
}
async function by(e, t) {
  const n = ae(e, "VMK"), r = jt(Gr);
  let i, o;
  try {
    he(n, se, "VMK"), o = ae(t.prfSalt, "passkey PRF salt"), he(o, yi, "passkey PRF salt"), i = await Su(t.prfOutput, o);
    const s = await Zr(i, r, n);
    return {
      kind: "passkey",
      kdf: "webauthn-prf-hkdf-sha256",
      prf_salt: Y(o),
      nonce: Y(r),
      wrapped: Y(s),
      ...t.credentialId ? { credential_id: t.credentialId } : {}
    };
  } finally {
    i?.fill(0), o?.fill(0), n.fill(0);
  }
}
async function my(e, t) {
  const n = ae(e, "VMK");
  try {
    if (he(n, se, "VMK"), t.length === 0)
      throw new Error("at least one protected unlock factor is required");
    const r = [];
    for (const i of t)
      r.push(await by(n, i));
    return JSON.stringify({ v: pi, copies: r });
  } finally {
    n.fill(0);
  }
}
async function vy(e, t) {
  const n = fe(e.prf_salt);
  if (t.prfSalt && Y(t.prfSalt) !== e.prf_salt)
    throw n.fill(0), new Error("passkey PRF salt does not match copy");
  const r = await Su(t.prfOutput, n);
  try {
    return await gi(r, fe(e.nonce), fe(e.wrapped));
  } finally {
    r.fill(0), n.fill(0);
  }
}
async function xy(e, t) {
  const n = rt(e);
  for (const r of n.copies)
    try {
      if (r.kind === "passkey") {
        const i = await vy(r, t);
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
function jo(e) {
  return rt(e).copies.filter((t) => t.kind === "passkey").map((t) => ({
    prfSalt: fe(t.prf_salt),
    ...t.credential_id ? { credentialId: t.credential_id } : {}
  }));
}
function Ey(e, t) {
  const n = rt(e);
  return JSON.stringify({
    ...n,
    rp_id: t.rpId,
    vault_user_handle: t.vaultUserHandle
  });
}
function Au(e) {
  const t = rt(e);
  return delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, JSON.stringify(t);
}
function Vo(e) {
  if (!e.kind) {
    if (e.curve !== void 0) throw new Error("protected record curve requires keypair metadata");
    return;
  }
  const t = vn(e.curve);
  if (e.kind === "static") {
    if (e.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (e.kind === "keypair") {
    if (typeof e.publicKey != "string" || e.publicKey.length === 0)
      throw new Error("keypair protected record requires a public key");
    return t === "ed25519" && Jo(e.publicKey), {
      kind: "keypair",
      public_key: e.publicKey.toLowerCase(),
      ...t === "ed25519" ? { curve: t } : {}
    };
  }
  throw new Error("unsupported protected record kind");
}
function ky(e) {
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
    return Vo({ kind: "keypair", publicKey: t.public_key, curve: t.curve });
  throw new Error("protected record metadata kind is invalid");
}
function qo(e, t) {
  return {
    name: e,
    ...t ? { kind: t.kind } : {},
    ...t?.public_key ? { publicKey: t.public_key } : {},
    ...t?.curve ? { curve: t.curve } : {}
  };
}
function Go(e) {
  return ve(
    Wr({
      domain: "avault:protected-record:aad:v2",
      name: e.kind === "keypair" ? Tu(e.name) : gn(e.name),
      scheme: e.scheme ?? qr,
      version: e.version ?? pi,
      record: Vo(e) ?? null
    })
  );
}
async function _u(e, t, n) {
  const r = ae(t, "VMK"), i = jt(se);
  let o;
  try {
    he(r, se, "VMK"), o = ae(e, "value");
    const s = Go(n), a = jt(Gr), c = await Zr(i, a, o, s), u = jt(Gr), l = await Zr(r, u, i, s);
    return {
      ciphertext: Y(c),
      nonce: Y(a),
      dek_nonce: Y(u),
      wrapped_dek: Y(l)
    };
  } finally {
    i.fill(0), r.fill(0), o?.fill(0);
  }
}
async function Iu(e, t, n) {
  const r = ae(t, "VMK");
  try {
    he(r, se, "VMK");
    const i = await gi(
      r,
      fe(e.dek_nonce),
      fe(e.wrapped_dek),
      Go(n)
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
async function Wo(e, t, n) {
  const r = await Iu(e, t, n);
  try {
    return await gi(
      r,
      fe(e.nonce),
      fe(e.ciphertext),
      Go(n)
    );
  } finally {
    r.fill(0);
  }
}
function Pu(e, t, n) {
  const r = rt(t);
  if ("dek_nonce" in r || "wrapped_dek" in r)
    throw new Error("vmk wrap_meta must not already carry a wrapped DEK");
  if ("scheme" in r && r.scheme !== qr)
    throw new Error("protected wrap_meta has unsupported scheme");
  const i = n ? Vo(n) : void 0;
  return {
    ciphertext: e.ciphertext,
    nonce: e.nonce,
    wrap_meta: JSON.stringify({
      ...r,
      scheme: qr,
      ...i ? { record_meta: i } : {},
      dek_nonce: e.dek_nonce,
      wrapped_dek: e.wrapped_dek
    })
  };
}
function yn(e) {
  const t = rt(e.wrap_meta), { dek_nonce: n, wrapped_dek: r, record_meta: i, ...o } = t;
  if (typeof n != "string" || typeof r != "string")
    throw new Error("protected record wrap_meta is missing the wrapped DEK");
  const s = ky(i);
  return {
    sealed: { ciphertext: e.ciphertext, nonce: e.nonce, dek_nonce: n, wrapped_dek: r },
    vmkWrapMeta: JSON.stringify(o),
    ...s ? { recordMetadata: s } : {}
  };
}
function Bu(e) {
  const t = { ...rt(e) };
  return delete t.root_meta, delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, delete t.scheme, ve(Wr({ domain: ly, vault: t }));
}
async function Sy(e, t, n) {
  const r = jt(Gr), i = ve(JSON.stringify(t));
  try {
    const o = await Zr(e, r, i, Bu(n));
    return { v: 1, nonce: Y(r), ciphertext: Y(o) };
  } finally {
    i.fill(0);
  }
}
async function Zo(e, t) {
  const n = rt(e).root_meta;
  if (!n) return null;
  if (n.v !== 1 || typeof n.nonce != "string" || typeof n.ciphertext != "string")
    throw new Error("invalid protected root metadata");
  const r = await gi(t, fe(n.nonce), fe(n.ciphertext), Bu(e));
  try {
    const i = JSON.parse(fy.decode(r));
    if (typeof i != "object" || i === null) throw new Error("invalid protected root metadata");
    return i;
  } finally {
    r.fill(0);
  }
}
function Ay(e, t) {
  const n = rt(e);
  return JSON.stringify({ ...n, root_meta: t });
}
function _y() {
  return new pp({
    kem: new kp(),
    kdf: new hp(),
    aead: new Zf()
  });
}
function Xo(e) {
  return fe(typeof e == "string" ? e : e.public_key);
}
async function $u(e) {
  const t = await crypto.subtle.digest("SHA-256", Pe(e));
  return X(new Uint8Array(t));
}
async function Iy(e) {
  return $u(Xo(e));
}
function Py(e) {
  if (!e.fingerprint)
    throw new Error("protected DEK release requires a pinned avault public key fingerprint");
  return e;
}
async function By(e) {
  const t = Py(e), n = Xo(t);
  if (he(n, se, "avault public key"), await $u(n) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  return t;
}
async function $y(e, t, n) {
  const r = _y(), i = Xo(t);
  if (he(i, se, "avault public key"), typeof t != "string" && t.fingerprint && await Iy(t) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  const o = await r.kem.deserializePublicKey(i), s = typeof e == "string" ? ve(e) : ae(e, "plaintext");
  try {
    const a = await r.seal(
      { recipientPublicKey: o, info: ve(oy) },
      s,
      Ny(n)
    );
    return {
      scheme: iy,
      enc: Y(new Uint8Array(a.enc)),
      ct: Y(new Uint8Array(a.ct))
    };
  } finally {
    s.fill(0);
  }
}
function Tu(e) {
  if (typeof e != "string" || !cy.test(e))
    throw new Error("vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$");
  return e;
}
function gn(e) {
  if (typeof e != "string" || !uy.test(e))
    throw new Error("vault record name must match ^[A-Za-z_][A-Za-z0-9_]*(\\.[a-z][a-z0-9_]{0,63})?$");
  return e;
}
function va(e, t) {
  if (typeof e != "string" || e.trim() === "")
    throw new Error(`${t} is required`);
  return e;
}
function Yo(e) {
  const t = typeof e == "string" ? Oe(e) : ae(e, "digest");
  return he(t, se, "digest"), t;
}
function Ty(e) {
  const t = typeof e == "string" ? Oe(e) : ae(e, "private key");
  try {
    if (he(t, se, "private key"), !Ee.utils.isValidSecretKey(t))
      throw new Error("invalid secp256k1 private key");
    return t;
  } catch (n) {
    throw t.fill(0), n;
  }
}
function Xr(e, t) {
  return typeof e == "string" ? Oe(e) : ae(e, t);
}
function Yr(e) {
  const t = Xr(e, "operation hash");
  return he(t, se, "operation hash"), t;
}
function Jr(e, t) {
  if (typeof e == "number" && (!Number.isSafeInteger(e) || e < 0))
    throw new Error(`${t} out of bounds`);
  const n = typeof e == "bigint" ? e : BigInt(e);
  if (n < 0n || n > 0xffffffffffffffffn)
    throw new Error(`${t} out of bounds`);
  return n;
}
function Ou(e, t) {
  const n = Jr(e, t), r = new Uint8Array(8);
  return new DataView(r.buffer).setBigUint64(0, n, !1), r;
}
function Oy(e) {
  return Ou(e, "approval expiry");
}
function xa(e) {
  const t = ae(e.nonce, "approval nonce");
  if (t.length < 16 || t.length > 128)
    throw new Error("approval nonce must be 16..128 bytes");
  return { approvalNonce: t, approvalExpiresAtUnix: Jr(e.expiresAtUnix, "approval expiry") };
}
function $e(e, t) {
  const n = ae(t);
  if (n.length > 4294967295)
    throw new Error("blind-box AAD field is too large");
  e.push(n.length >>> 24 & 255, n.length >>> 16 & 255, n.length >>> 8 & 255, n.length & 255), e.push(...n);
}
function Ly(e) {
  switch (e.purpose) {
    case "seal":
      return Ry(e.name);
    case "agent-deliver": {
      const t = xa({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-deliver",
        name: gn(e.name),
        grantId: va(e.grantId, "grant id"),
        ttlSecs: Jr(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: Yr(e.operationHash)
      };
    }
    case "agent-sign": {
      Do(e.signScheme);
      const t = xa({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-sign",
        name: Tu(e.name),
        grantId: va(e.grantId, "grant id"),
        signScheme: e.signScheme,
        digest: Yo(e.digest),
        ttlSecs: Jr(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: Yr(e.operationHash)
      };
    }
    default:
      throw new Error("unsupported blind-box context purpose");
  }
}
function Ry(e) {
  return { purpose: "seal", name: gn(e) };
}
async function Cy(e) {
  const t = Yr(e.operationHash), n = await Uy(e.name, e.ttlSecs);
  if (X(t) !== X(n))
    throw new Error("operation hash does not match release operation fields");
}
async function zy(e) {
  const t = [];
  for (const n of e)
    $e(t, typeof n == "string" ? ve(n) : ae(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(t)));
}
async function Uy(e, t) {
  return zy(["agent-deliver", gn(e), Ou(t, "ttl seconds")]);
}
function Ny(e) {
  const t = Ly(e), n = [...ve(sy)];
  return $e(n, ve(t.purpose)), $e(n, ve(t.name)), $e(n, ve(qr)), $e(n, new Uint8Array([pi])), $e(n, t.purpose === "agent-deliver" || t.purpose === "agent-sign" ? ve(t.grantId) : new Uint8Array()), $e(n, new Uint8Array()), $e(n, t.purpose === "agent-sign" ? ve(t.signScheme) : new Uint8Array()), $e(n, t.purpose === "agent-sign" ? Yo(t.digest) : new Uint8Array()), $e(n, t.purpose === "seal" ? new Uint8Array() : ae(t.approvalNonce, "approval nonce")), $e(n, t.purpose === "seal" ? new Uint8Array() : Oy(t.approvalExpiresAtUnix)), $e(n, t.purpose === "seal" ? new Uint8Array() : Yr(t.operationHash)), new Uint8Array(n);
}
async function My(e, t, n, r, i) {
  if (r.kind !== void 0 && r.kind !== "static")
    throw new Error("protected DEK release requires a static protected record");
  if (gn(r.name) !== gn(i.name))
    throw new Error("protected DEK release context name does not match record name");
  await Cy(i);
  const o = await By(n), s = await Iu(e, t, r);
  try {
    return await $y(s, o, i);
  } finally {
    s.fill(0);
  }
}
function Hy(e = "secp256k1") {
  if (vn(e) === "ed25519") {
    const n = Bt.utils.randomSecretKey();
    try {
      return { privateKey: n, publicKey: X(Bt.getPublicKey(n)) };
    } catch (r) {
      throw n.fill(0), r;
    }
  }
  const t = Ee.utils.randomSecretKey();
  return { privateKey: t, publicKey: X(Ee.getPublicKey(t, !0)) };
}
function Ea(e) {
  return Oe(e.toString(16).padStart(se * 2, "0"));
}
function Fy(e, t, n, r = {}) {
  if (Do(n), !r || typeof r != "object" || Array.isArray(r) || Object.keys(r).some((s) => s !== "schnorrAuxRand"))
    throw new Error("unsupported signing option; script trees are not supported");
  const i = Ty(e);
  let o;
  try {
    const s = Yo(t);
    if (n === hi) {
      const a = Ee.sign(s, i, { prehash: !1, lowS: !0, format: "recovered" });
      return {
        scheme: n,
        signature: X(a.slice(1)),
        recovery_id: a[0] ?? null
      };
    }
    if (n === Ho)
      return {
        scheme: n,
        signature: X(Ee.sign(s, i, { prehash: !1, lowS: !0, format: "der" })),
        recovery_id: null
      };
    if (n === Fo) {
      o = r.schnorrAuxRand !== void 0 ? Xr(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(se)), he(o, se, "schnorr aux randomness");
      const a = Ut.sign(s, i, o);
      if (!Ut.verify(a, s, Ut.getPublicKey(i))) throw new Error("signing self-verification failed");
      return { scheme: n, signature: X(a), recovery_id: null };
    }
    if (n === Gn) {
      const a = Ee.getPublicKey(i, !0), c = Qo(a.slice(1));
      let u = BigInt(`0x${X(i)}`);
      a[0] === 3 && (u = Cn - u);
      const l = BigInt(`0x${X(Ut.utils.taggedHash("TapTweak", a.slice(1)))}`);
      if (l >= Cn) throw new Error("invalid taproot tweak");
      let d = (u + l) % Cn;
      if (d === 0n) throw new Error("invalid taproot signing key");
      let h = Ea(d);
      try {
        c.toBytes(!0)[0] === 3 && (d = Cn - d, h.fill(0), h = Ea(d)), o = r.schnorrAuxRand !== void 0 ? Xr(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(se)), he(o, se, "schnorr aux randomness");
        const f = Ut.sign(s, h, o);
        if (!Ut.verify(f, s, c.toBytes(!0).slice(1))) throw new Error("Taproot signing self-verification failed");
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
async function Ky(e, t, n, r, i, o = {}) {
  if (n.kind !== "keypair" || !n.publicKey)
    throw new Error("protected signing requires a keypair protected record");
  if (Do(i), vn(n.curve) !== "secp256k1") throw new Error("digest signing requires a secp256k1 protected record");
  const s = await Wo(e, t, n);
  try {
    if (X(Ee.getPublicKey(s, !0)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return Fy(s, r, i, o);
  } finally {
    s.fill(0);
  }
}
function Dy(e, t) {
  const n = Mo(t), r = Xr(e, "Ed25519 seed");
  try {
    he(r, se, "Ed25519 seed");
    const i = Bt.sign(n, r);
    if (!Bt.verify(i, n, Bt.getPublicKey(r), { zip215: !1 }))
      throw new Error("Ed25519 signing self-verification failed");
    return { scheme: qn, signature: X(i), recovery_id: null };
  } finally {
    r.fill(0);
  }
}
function Jo(e) {
  if (!/^[0-9a-fA-F]{64}$/.test(e)) throw new Error("expected a 32-byte Ed25519 public key");
  const t = Oe(e);
  if (!Bt.utils.isValidPublicKey(t, !1)) throw new Error("invalid Ed25519 public key");
  return t;
}
async function jy(e, t, n, r) {
  if (n.kind !== "keypair" || n.curve !== "ed25519" || !n.publicKey)
    throw new Error("exact-message signing requires an Ed25519 keypair protected record");
  const i = Mo(r);
  if (i.length === 0) throw new Error("protected Ed25519 message must not be empty");
  Jo(n.publicKey);
  const o = await Wo(e, t, n);
  try {
    if (he(o, se, "Ed25519 seed"), X(Bt.getPublicKey(o)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return Dy(o, i);
  } finally {
    o.fill(0);
  }
}
function Vy(e) {
  return Ru(Jo(e));
}
function qy(e) {
  const t = e.rootMetadata?.daemon?.verificationKeys?.find(
    (n) => n.alg === "ed25519" && n.keyId === e.keyId
  );
  if (!t)
    throw new Error("daemon verification key is not pinned");
  return Bt.verify(ma(e.signature), ve(e.message), ma(t.publicKey));
}
function Lu(e) {
  const t = Oe(e.trim());
  if (t.length === 33 && (t[0] === 2 || t[0] === 3) && Ee.utils.isValidPublicKey(t))
    return t;
  if (t.length === 65 && t[0] === 4 && Ee.utils.isValidPublicKey(t))
    return Ee.Point.fromBytes(t).toBytes(!0);
  throw new Error("expected a valid secp256k1 public key");
}
const Gy = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", Wy = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", Zy = 1, Xy = 734539939, Cn = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
function Yy(e) {
  return pu(ue(e));
}
function Jy(e) {
  const t = ue(ue(e)).slice(0, 4), n = new Uint8Array(e.length + t.length);
  return n.set(e), n.set(t, e.length), Ru(n);
}
function Ru(e) {
  let t = BigInt(`0x${X(e)}`), n = "";
  for (; t > 0n; ) {
    const i = Number(t % 58n);
    t /= 58n, n = Gy[i] + n;
  }
  let r = 0;
  for (const i of e) {
    if (i !== 0) break;
    r += 1;
  }
  return "1".repeat(r) + n;
}
function Qy(e) {
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
function eg(e) {
  return [...e].map((t) => t.charCodeAt(0) >>> 5).concat([0], [...e].map((t) => t.charCodeAt(0) & 31));
}
function tg(e, t, n) {
  const r = eg(e).concat(t), i = Qy(r.concat([0, 0, 0, 0, 0, 0])) ^ n;
  return [0, 1, 2, 3, 4, 5].map((o) => i >>> 5 * (5 - o) & 31);
}
function ng(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >>> i & s);
  return i && o.push(r << n - i & s), o;
}
function ka(e, t, n) {
  const r = t === 0 ? Zy : Xy, i = [t].concat(ng(n, 8, 5));
  return `${e}1${i.concat(tg(e, i, r)).map((o) => Wy[o]).join("")}`;
}
function rg(e) {
  const t = e.toLowerCase(), n = X(mu(ve(t)));
  return [...t].map((r, i) => /[a-f]/.test(r) && Number.parseInt(n[i], 16) >= 8 ? r.toUpperCase() : r).join("");
}
function Qo(e) {
  const t = BigInt(`0x${X(Ut.utils.taggedHash("TapTweak", e))}`);
  if (t >= Cn) throw new Error("invalid taproot tweak");
  const n = Ee.Point.fromBytes(new Uint8Array([2, ...e])), r = t === 0n ? n : n.add(Ee.Point.BASE.multiply(t));
  if (r.equals(Ee.Point.ZERO)) throw new Error("invalid taproot output key");
  return r;
}
function ig(e) {
  return Qo(Lu(e).slice(1)).toBytes(!0).slice(1);
}
function Cu(e) {
  const t = Lu(e), n = Ee.Point.fromBytes(t), r = n.toBytes(!1).slice(1), i = mu(r), o = Yy(t), s = new Uint8Array(21);
  s[0] = 0, s.set(o, 1);
  const a = n.toBytes(!1).slice(1, 33), c = Qo(a);
  return {
    eth: `0x${rg(X(i.slice(-20)))}`,
    btc_legacy: Jy(s),
    btc_segwit: ka("bc", 0, o),
    btc_taproot: ka("bc", 1, c.toBytes(!1).slice(1, 33))
  };
}
const es = {
  windowSeconds: 600,
  strictApprovals: !1,
  parentValueSealAllowed: !0
};
let so = { ...es };
function og(e) {
  return e === 300 || e === 600 || e === 1800 ? e : es.windowSeconds;
}
function sg(e, t) {
  if (e === 300 || e === 600 || e === 1800) return e;
  throw new m("invalid_payload", `${t}.windowSeconds must be 300, 600, or 1800`);
}
function ts(e, t = "policy") {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be an object`);
  const n = e;
  if (typeof n.strictApprovals != "boolean") throw new m("invalid_payload", `${t}.strictApprovals must be a boolean`);
  if (typeof n.parentValueSealAllowed != "boolean")
    throw new m("invalid_payload", `${t}.parentValueSealAllowed must be a boolean`);
  return {
    windowSeconds: sg(n.windowSeconds, t),
    strictApprovals: n.strictApprovals,
    parentValueSealAllowed: n.parentValueSealAllowed
  };
}
function ag(e) {
  if (typeof e != "object" || e === null) return { ...es };
  const t = e;
  return {
    windowSeconds: og(t.windowSeconds),
    strictApprovals: t.strictApprovals === !0,
    parentValueSealAllowed: t.parentValueSealAllowed !== !1
  };
}
function ns(e) {
  return so = ag(e), so;
}
function xe() {
  return { ...so };
}
const cg = "avibe-vault-lock:v1:", j = {
  vmk: null,
  wrapMeta: null,
  freshSetup: !1,
  scopeId: null
};
let pt = null, Fn = null, Wn = null, ao = null, rs = 0;
const Qr = /* @__PURE__ */ new Set();
let zu = null;
function ug(e) {
  zu = e;
}
function Sa() {
  return j.vmk ? "unlocked" : j.wrapMeta ? "locked" : "needs-setup";
}
function Uu(e) {
  zu?.({
    state: Sa(),
    ...pt !== null && Sa() === "unlocked" ? { expiresAt: pt } : {},
    reason: e
  });
}
function is() {
  Fn !== null && (clearTimeout(Fn), Fn = null);
}
function ei() {
  Wn?.close(), Wn = null, ao = null;
}
function Nu(e) {
  const t = new TextEncoder().encode(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return btoa(n).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function lg(e) {
  let t = 2166136261;
  for (let n = 0; n < e.length; n += 1)
    t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
  return t.toString(16).padStart(8, "0");
}
function Mu(e) {
  const t = rt(e);
  return typeof t.vault_user_handle == "string" && t.vault_user_handle.length > 0 ? Nu(t.vault_user_handle) : `wrap-${lg(e)}`;
}
function dg(e) {
  return Nu(e);
}
function Hu(e) {
  if (typeof window > "u" || typeof BroadcastChannel > "u" || !e) {
    ei();
    return;
  }
  const t = `${cg}${e}`;
  ao !== t && (ei(), ao = t, Wn = new BroadcastChannel(t), Wn.onmessage = (n) => {
    const r = n.data;
    r?.type === "lock" && r.scopeId === e && Fe({ broadcast: !1, reason: "manual-lock" });
  });
}
function fg() {
  j.scopeId && (Hu(j.scopeId), Wn?.postMessage({ type: "lock", scopeId: j.scopeId }));
}
function Fu() {
  return new Error("vault-operation-aborted");
}
function Ku() {
  rs += 1;
  for (const e of Qr)
    e.abort(Fu());
  Qr.clear();
}
function pg() {
  j.vmk?.fill(0), j.vmk = null, pt = null, is(), ei(), j.freshSetup && (j.wrapMeta = null, j.freshSetup = !1, j.scopeId = null);
}
function hg() {
  return pt !== null && Date.now() >= pt;
}
function Du() {
  return j.vmk && hg() ? (Fe({ broadcast: !1, reason: "auto-lock" }), !1) : j.vmk !== null;
}
function ju(e, t = xe()) {
  if (!j.vmk)
    throw new Error("vault VMK is not loaded");
  const n = t.windowSeconds * 1e3;
  return pt = Date.now() + n, is(), Fn = setTimeout(() => {
    Fe({ broadcast: !1, reason: "auto-lock" });
  }, n), Uu(e), pt;
}
function yg() {
  const e = pt;
  if (!j.vmk || e === null) return () => {
  };
  is();
  let t = !1;
  return () => {
    if (t || !j.vmk) return;
    t = !0;
    const n = e - Date.now();
    if (n <= 0) {
      Fe({ broadcast: !1, reason: "auto-lock" });
      return;
    }
    Fn = setTimeout(() => {
      Fe({ broadcast: !1, reason: "auto-lock" });
    }, n);
  };
}
function gg(e) {
  if (e instanceof m && e.retryable) return !0;
  const t = e instanceof Error ? e.message : String(e);
  return t === "operation-cancelled" || t === "operation-superseded" || t === "passkey-cancelled" || t === "vault-operation-aborted";
}
function wg(e) {
  if (e.signal.aborted || e.epoch !== rs || !j.vmk)
    throw Fu();
}
async function os(e, t = {}) {
  if (!Du() || !j.vmk || !j.wrapMeta)
    throw new Error("vault-locked");
  const n = new Uint8Array(j.vmk), r = j.wrapMeta, i = rs, o = new AbortController();
  Qr.add(o);
  const s = {
    epoch: i,
    signal: o.signal,
    assertCurrent: () => wg(s)
  };
  let a = null;
  try {
    s.assertCurrent();
    const c = await e(n, r, s);
    return s.assertCurrent(), t.renewOnSuccess !== !1 && ju("renew"), s.assertCurrent(), t.beforeSuccess && (a = yg(), await t.beforeSuccess(), s.assertCurrent(), a(), a = null), c;
  } catch (c) {
    throw gg(c) || Fe({ broadcast: !0, reason: "manual-lock" }), c;
  } finally {
    a?.(), Qr.delete(o), n.fill(0);
  }
}
function bg() {
  return j.vmk !== null && j.freshSetup;
}
function Vu(e) {
  return Ku(), j.vmk?.fill(0), j.vmk = e.vmk, j.wrapMeta = e.wrapMeta, j.freshSetup = e.freshSetup, j.scopeId = e.scopeId, Hu(e.scopeId), { state: "unlocked", expiresAt: ju("unlock", e.policy) };
}
function Fe(e = {}) {
  return e.broadcast && fg(), Ku(), pg(), Uu(e.reason ?? "manual-lock"), { state: "locked" };
}
function ht(e) {
  return Du(), j.vmk ? {
    state: "unlocked",
    expiresAt: pt ?? void 0,
    ...j.freshSetup ? { freshSetup: !0 } : {}
  } : (e && (j.wrapMeta = Au(e), j.scopeId = Mu(j.wrapMeta)), j.wrapMeta ? { state: "locked" } : { state: "needs-setup" });
}
function ss(e) {
  const t = Au(e), n = Mu(t);
  return j.wrapMeta = t, j.scopeId = n, { wrapMeta: t, scopeId: n };
}
function mg() {
  Fe({ broadcast: !1, reason: "unload" }), ei();
}
const mr = "Avibe Vault", Aa = "avibe-vault";
function Vt(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function $t(e) {
  const t = new ArrayBuffer(e.byteLength);
  return new Uint8Array(t).set(e), t;
}
function qu() {
  return $t(crypto.getRandomValues(new Uint8Array(32)));
}
function Gu(e) {
  try {
    return fe(e);
  } catch {
    return null;
  }
}
function wi(e, t) {
  if (e instanceof ArrayBuffer || ArrayBuffer.isView(e))
    return $t(ku(e));
  if (typeof e == "string") {
    const n = Gu(e);
    return $t(n ?? new TextEncoder().encode(e));
  }
  if (Array.isArray(e))
    return $t(Uint8Array.from(e));
  throw new Error("webauthn bytes are missing");
}
function Ci(e) {
  if (typeof e != "object" || e === null || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null)
    throw new Error("invalid webauthn creation options object");
  return e;
}
function vg(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 88 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(e) || /[+/]/.test(e) && /[-_]/.test(e))
    throw new Error("invalid webauthn user.id encoding");
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = Gu(t);
  if (!n || n.byteLength === 0 || n.byteLength > 64)
    throw new Error("webauthn user.id must be 1-64 bytes");
  const r = Y(n);
  if (t !== r && t !== r.replace(/=+$/, ""))
    throw new Error("invalid webauthn user.id encoding");
  return $t(n);
}
function xg(e) {
  const t = wi(e);
  if (t.byteLength === 0 || t.byteLength > 64)
    throw new Error("vault user handle must be 1-64 bytes");
  return t;
}
function ti(e) {
  if (Array.isArray(e))
    return e.map((t) => {
      const n = Vt(t);
      if (!n || n.type !== "public-key")
        throw new Error("invalid WebAuthn credential descriptor");
      return {
        type: "public-key",
        id: wi(n.id),
        ...Array.isArray(n.transports) ? { transports: n.transports } : {}
      };
    });
}
function _a(e) {
  return !Array.isArray(e) || e.length === 0 ? [
    { type: "public-key", alg: -7 },
    { type: "public-key", alg: -257 }
  ] : e.map((t) => {
    const n = Vt(t);
    if (!n || n.type !== "public-key" || typeof n.alg != "number")
      throw new Error("invalid WebAuthn pubKeyCredParams");
    return { type: "public-key", alg: n.alg };
  });
}
function Ia(e) {
  return {
    ...Vt(e) ?? {},
    residentKey: "required",
    userVerification: "required"
  };
}
function Pa(e) {
  if (e === "none" || e === "indirect" || e === "direct" || e === "enterprise") return e;
}
function Eg(e) {
  const t = Vt(e) ?? {};
  return { ...t, prf: Vt(t.prf) ?? {} };
}
function yt() {
  return window.location.hostname;
}
function kg(e) {
  const t = e.authzCreationOptions === void 0 ? void 0 : Ci(e.authzCreationOptions), n = t && Object.prototype.hasOwnProperty.call(t, "webauthn") ? Ci(t.webauthn) : t, r = typeof t?.challenge_id == "string" ? t.challenge_id : void 0, i = Vt(n?.rp), o = typeof i?.id == "string" ? i.id : void 0;
  if (o && o !== e.rpId)
    throw new Error("webauthn rp_id does not match sandbox host");
  if (!n)
    return {
      options: {
        rp: { name: mr, id: e.rpId },
        user: {
          id: xg(e.vaultUserHandle),
          name: e.vaultUserHandle || Aa,
          displayName: e.displayName || mr
        },
        challenge: qu(),
        pubKeyCredParams: _a(void 0),
        authenticatorSelection: Ia(void 0),
        extensions: { prf: {} }
      }
    };
  const s = Ci(n.user);
  return {
    options: {
      rp: {
        name: typeof i?.name == "string" ? i.name : mr,
        id: e.rpId
      },
      user: {
        id: vg(s.id),
        name: typeof s.name == "string" ? s.name : e.vaultUserHandle || Aa,
        displayName: typeof s.displayName == "string" ? s.displayName : e.displayName || mr
      },
      challenge: wi(n.challenge),
      pubKeyCredParams: _a(n.pubKeyCredParams),
      authenticatorSelection: Ia(n.authenticatorSelection),
      extensions: Eg(n.extensions),
      ...ti(n.excludeCredentials) ? { excludeCredentials: ti(n.excludeCredentials) } : {},
      ...typeof n.timeout == "number" ? { timeout: n.timeout } : {},
      ...Pa(n.attestation) ? { attestation: Pa(n.attestation) } : {}
    },
    ...r ? { challengeId: r } : {}
  };
}
function Sg(e) {
  if (e.length === 0) throw new Error("passkey-not-configured");
  if (e.length > 1) throw new Error("passkey-multiple-not-supported");
  return e[0];
}
function Ag(e, t) {
  const n = Sg(e), r = {
    challenge: qu(),
    rpId: t,
    userVerification: "required",
    extensions: wy(n.prfSalt)
  };
  return n.credentialId && (r.allowCredentials = [
    {
      type: "public-key",
      id: $t(fe(n.credentialId))
    }
  ]), r;
}
function _g(e, t, n) {
  const r = {
    challenge: $t(n),
    rpId: t,
    userVerification: "required"
  }, i = e.filter((o) => o.credentialId).map((o) => ({ type: "public-key", id: $t(fe(o.credentialId)) }));
  return i.length > 0 && (r.allowCredentials = i), r;
}
function Ig(e, t = yt()) {
  const n = Vt(e);
  if (!n) throw new Error("webauthn assertion options are missing");
  const r = typeof n.rpId == "string" ? n.rpId : typeof n.rp_id == "string" ? n.rp_id : void 0;
  if (r && r !== t)
    throw new Error("webauthn rpId does not match sandbox host");
  return {
    challenge: wi(n.challenge),
    rpId: t,
    userVerification: "required",
    ...ti(n.allowCredentials) ? { allowCredentials: ti(n.allowCredentials) } : {},
    ...typeof n.timeout == "number" ? { timeout: n.timeout } : {}
  };
}
function Wu(e, t) {
  return {
    publicKey: e,
    ...t ? { signal: t } : {}
  };
}
function Zu(e) {
  return {
    id: e.id,
    rawId: Y(e.rawId),
    type: e.type
  };
}
function Pg(e) {
  const t = e.response, n = typeof t.getTransports == "function" ? t.getTransports() : [];
  return {
    ...Zu(e),
    response: {
      clientDataJSON: Y(t.clientDataJSON),
      attestationObject: Y(t.attestationObject),
      transports: n
    }
  };
}
function Bg(e) {
  const t = e.response;
  return {
    ...Zu(e),
    response: {
      clientDataJSON: Y(t.clientDataJSON),
      authenticatorData: Y(t.authenticatorData),
      signature: Y(t.signature),
      userHandle: t.userHandle ? Y(t.userHandle) : null
    }
  };
}
function $g(e) {
  const n = e?.getClientExtensionResults()?.prf?.results?.first;
  if (!n) throw new Error("passkey-prf-unavailable");
  const r = ku(n, "passkey PRF output");
  if (r.byteLength === 0) throw new Error("passkey-prf-unavailable");
  return r;
}
async function as(e, t, n) {
  const r = await navigator.credentials.get(
    Wu(Ag(e, t), n)
  );
  if (!r) throw new Error("passkey-cancelled");
  const i = $g(r), o = Y(r.rawId), s = e.find((a) => a.credentialId === o);
  return { prfOutput: i, prfSalt: s?.prfSalt ?? e[0].prfSalt, credentialId: o };
}
async function Tg(e, t, n, r) {
  if (!await navigator.credentials.get(
    Wu(_g(e, t, n), r)
  )) throw new Error("passkey-cancelled");
}
async function Og(e) {
  const t = await navigator.credentials.get({
    publicKey: Ig(e)
  });
  if (!t) throw new Error("passkey-cancelled");
  return Bg(t);
}
const Lg = /ancestor|not the same as its|cross-?origin|different origin/i;
function Xu(e) {
  return typeof e?.name == "string" ? e.name : "";
}
function Rg(e) {
  return e instanceof Error || typeof e?.message == "string" ? e.message : typeof e == "string" ? e : "";
}
function Yu(e) {
  const t = Xu(e);
  return t === "SecurityError" || t === "NotSupportedError" || Lg.test(Rg(e));
}
function Cg(e) {
  return Xu(e) === "NotAllowedError" && !Yu(e);
}
function cs(e) {
  const { options: t, challengeId: n } = kg(e);
  return navigator.credentials.create({
    publicKey: t
  }).then((i) => {
    if (!i) throw new Error("passkey-cancelled");
    const o = Y(i.rawId), s = Pg(i), a = e.authzCreationOptions !== void 0;
    return {
      credentialId: o,
      ...a ? { authzRegistration: n ? { challenge_id: n, credential: s } : s } : {}
    };
  });
}
const co = {
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
let ni = "en";
function zg(e) {
  return e === "zh" ? "zh" : "en";
}
function Ug(e) {
  const t = zg(e);
  return t === ni ? !1 : (ni = t, !0);
}
function Ng() {
  return ni;
}
function Mg(e) {
  return typeof e == "string" && Object.hasOwn(co.en, e);
}
function F(e, t = {}) {
  const n = co[ni], r = co.en;
  return (n[e] ?? r[e] ?? e).replace(/\{\{(\w+)\}\}/g, (o, s) => String(t[s] ?? ""));
}
function Hg(e) {
  return JSON.stringify(e ?? {});
}
function Ba(e) {
  if (!e) return {};
  try {
    const t = JSON.parse(e);
    return typeof t == "object" && t !== null ? t : {};
  } catch {
    return {};
  }
}
function Be(e, t, n) {
  e.dataset.i18nKey = t, e.dataset.i18nParams = Hg(n), e.textContent = F(t, n);
}
function qt(e, t) {
  delete e.dataset.i18nKey, delete e.dataset.i18nParams, e.textContent = t;
}
function Fg(e = document) {
  e.querySelectorAll("[data-i18n-key]").forEach((t) => {
    const n = t.dataset.i18nKey;
    n && (t.textContent = F(n, Ba(t.dataset.i18nParams)));
  }), e.querySelectorAll("[data-i18n-placeholder-key]").forEach((t) => {
    const n = t.dataset.i18nPlaceholderKey;
    n && (t.placeholder = F(n, Ba(t.dataset.i18nPlaceholderParams)));
  });
}
const Ju = {
  sol: { symbol: "SOL", baseUnit: "lamports", decimals: 9 },
  eth: { symbol: "ETH", baseUnit: "wei", decimals: 18 }
};
function Kg(e, t) {
  const { symbol: n, baseUnit: r, decimals: i } = Ju[t];
  if (!/^(?:0|[1-9][0-9]*)$/.test(e)) return `${e} ${r}`;
  const o = e.padStart(i + 1, "0"), s = o.slice(-i).replace(/0+$/, "");
  return `${o.slice(0, -i)}${s ? `.${s}` : ""} ${n} (${e} ${r})`;
}
function Q(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
const Dg = "http://www.w3.org/2000/svg";
function vr(e, t) {
  const n = document.createElementNS(Dg, e);
  for (const [r, i] of Object.entries(t)) n.setAttribute(r, i);
  return n;
}
function jg() {
  const e = vr("svg", {
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
    vr("path", { d: "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" }),
    vr("path", { d: "M12 9v4.5" }),
    vr("path", { d: "M12 17h.01" })
  ), e;
}
function Vg(e) {
  if (!e.notes?.length) return null;
  const t = Q("span", "review-notes");
  for (const n of e.notes) {
    const r = Q("span", "review-note");
    typeof n == "string" ? Be(r, n) : Be(r, n.key, { count: n.count }), t.append(r);
  }
  return t;
}
function zi(e, t = "review-label") {
  const n = Q(e.heading ? "p" : "span", t), r = {
    ...e.index === void 0 ? {} : { index: e.index },
    ...e.name === void 0 ? {} : { name: e.name }
  };
  return Be(n, e.label, Object.keys(r).length > 0 ? r : void 0), n;
}
function qg(e, t) {
  const n = Q("button", "review-copy");
  return n.type = "button", Be(n, "review.copy"), n.addEventListener("click", () => {
    const r = () => {
      window.getSelection()?.selectAllChildren(e), Be(n, "review.copySelected");
    };
    let i;
    try {
      i = navigator.clipboard.writeText(t);
    } catch {
      r();
      return;
    }
    i.then(() => Be(n, "review.copied"), r);
  }), n;
}
function Ui(e) {
  if (e.heading) return zi(e, "review-subheading");
  const t = Q("div", e.raw ? "review-field review-field-raw" : "review-field"), n = Vg(e);
  if (e.raw) {
    const o = Q("div", "review-raw-head"), s = Q("pre", "review-raw");
    return qt(s, e.value), o.append(zi(e)), n && o.append(n), o.append(qg(s, e.value)), t.append(o, s), t;
  }
  const r = Q("div", "review-value"), i = Q("span", "review-mono");
  return qt(i, e.native ? Kg(e.value, e.native) : e.value), r.append(i), n && r.append(n), t.append(zi(e), r), t;
}
function $a(e, t, n) {
  const r = Q("section", "review-group"), i = Q("h2", "review-group-title");
  return Be(i, e, t === void 0 ? void 0 : { index: t }), r.append(i, ...n), r;
}
function Gg(e, t, n = {}) {
  qt(e, "");
  const r = Q("div", "review"), i = Q("div", "review-heading"), o = Q("p", "review-kind");
  if (Be(o, t.kind), i.append(o), n.session) {
    const l = Q("p", "review-session"), d = Q("span", "review-session-label");
    Be(d, "context.session");
    const h = Q("span", "review-session-value");
    qt(h, n.session), l.append(d, h), i.append(l);
  }
  if (r.append(i), t.undecoded) {
    const l = Q("p", "review-notice"), d = Q("span", "review-notice-text");
    Be(d, "sign.blindNotice"), l.append(jg(), d), r.append(l);
  }
  const s = Q("div", "review-summary");
  s.append(Ui(t.signer), ...t.summary.map(Ui)), r.append(s);
  const a = Q("details", "review-details"), c = Q("summary", "review-details-toggle");
  Be(c, "review.details");
  const u = Q("div", "review-details-body");
  n.rows && u.append($a("review.group.request", void 0, [n.rows]));
  for (const l of t.details) u.append($a(l.title, l.index, l.fields.map(Ui)));
  a.append(c, u), r.append(a), e.append(r);
}
function Ie(e) {
  throw new Error(`signing review is malformed: ${e}`);
}
function ri(e, t, n) {
  (typeof e != "object" || e === null || Array.isArray(e)) && Ie(`${n} must be an object`);
  const r = e;
  for (const i of Object.keys(r)) t.includes(i) || Ie(`${n}.${i} is not a review field`);
  return r;
}
function Kn(e, t, n) {
  return (!Mg(e) || !e.startsWith(`${t}.`)) && Ie(`${n} must be a ${t} translation key`), e;
}
function uo(e, t) {
  if (e !== void 0)
    return (typeof e != "number" || !Number.isSafeInteger(e) || e < 0) && Ie(`${t} must be a non-negative integer`), e;
}
function Lr(e, t) {
  return Array.isArray(e) || Ie(`${t} must be a list`), e;
}
const Wg = /^[A-Za-z_][A-Za-z0-9_]{0,63} \([a-z][a-z0-9[\]]{0,31}\)$/;
function Ni(e, t) {
  const n = ri(e, ["label", "index", "name", "value", "native", "notes", "raw", "heading"], t), r = uo(n.index, `${t}.index`);
  n.name !== void 0 && (typeof n.name != "string" || !Wg.test(n.name)) && Ie(`${t}.name must be a built-in parameter name`), typeof n.value != "string" && Ie(`${t}.value must be a string`), n.native !== void 0 && !Object.hasOwn(Ju, n.native) && Ie(`${t}.native is not a native currency`), n.raw !== void 0 && typeof n.raw != "boolean" && Ie(`${t}.raw must be a boolean`), n.heading !== void 0 && typeof n.heading != "boolean" && Ie(`${t}.heading must be a boolean`), n.heading && (n.value !== "" || n.native !== void 0 || n.notes !== void 0 || n.raw !== void 0) && Ie(`${t} is a heading and cannot carry a value`);
  const i = n.notes === void 0 ? void 0 : Lr(n.notes, `${t}.notes`).map((o, s) => {
    const a = `${t}.notes[${s}]`;
    if (typeof o == "string") return Kn(o, "review.note", a);
    const c = ri(o, ["key", "count"], a), u = uo(c.count, `${a}.count`);
    return u === void 0 && Ie(`${a}.count is required`), { key: Kn(c.key, "review.note", `${a}.key`), count: u };
  });
  return {
    label: Kn(n.label, "review.field", `${t}.label`),
    ...r === void 0 ? {} : { index: r },
    ...n.name === void 0 ? {} : { name: n.name },
    value: n.value,
    ...n.native === void 0 ? {} : { native: n.native },
    ...i === void 0 ? {} : { notes: i },
    ...n.raw === void 0 ? {} : { raw: n.raw },
    ...n.heading === void 0 ? {} : { heading: n.heading }
  };
}
function Zg(e) {
  const t = ri(e, ["kind", "signer", "summary", "undecoded", "details"], "review");
  return typeof t.undecoded != "boolean" && Ie("review.undecoded must be a boolean"), {
    kind: Kn(t.kind, "review.kind", "review.kind"),
    signer: Ni(t.signer, "review.signer"),
    summary: Lr(t.summary, "review.summary").map((n, r) => Ni(n, `review.summary[${r}]`)),
    undecoded: t.undecoded,
    details: Lr(t.details, "review.details").map((n, r) => {
      const i = `review.details[${r}]`, o = ri(n, ["title", "index", "fields"], i), s = uo(o.index, `${i}.index`);
      return {
        title: Kn(o.title, "review.group", `${i}.title`),
        ...s === void 0 ? {} : { index: s },
        fields: Lr(o.fields, `${i}.fields`).map((a, c) => Ni(a, `${i}.fields[${c}]`))
      };
    })
  };
}
let us = null;
function Xg(e) {
  us = e;
}
function Yg() {
  us?.("ui.show");
}
function Jg() {
  us?.("ui.hide");
}
function Qg() {
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
function Qu(e) {
  e.querySelectorAll(".dynamic").forEach((t) => t.remove());
}
function ge(e) {
  return { text: e };
}
function Zn(e, t) {
  return { key: e, params: t };
}
function W(e, t) {
  if (typeof t == "string") {
    Be(e, t);
    return;
  }
  if ("text" in t) {
    qt(e, t.text);
    return;
  }
  Be(e, t.key, t.params);
}
function ls(e, t, n) {
  const r = Qg();
  return document.body.classList.add("interactive"), document.body.classList.remove("confirming"), Yg(), r.card.classList.remove("confirm-card"), W(r.title, e), W(r.subtitle, t), n ? W(r.body, n) : qt(r.body, ""), Qu(r.card), r;
}
function ds() {
  document.body.classList.remove("interactive"), document.body.classList.remove("confirming");
  const e = document.getElementById("page")?.querySelector(".card");
  e && (e.classList.remove("confirm-card"), Qu(e)), Jg();
}
function an(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic"), (e.querySelector(".card-scroll") ?? e).append(t);
}
function Ta(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic");
  const n = e.querySelector(".card-footer");
  if (!n) throw new Error("sandbox confirmation footer is unavailable");
  n.append(t);
}
function ew(e) {
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
function el(e) {
  const t = document.createElement("div");
  t.className = "confirmation-details";
  for (const n of e.rows) {
    const r = document.createElement("div");
    r.className = "detail-row";
    const i = document.createElement("span");
    i.className = "detail-label", W(i, n.label), r.append(i, ew(n)), t.append(r);
  }
  return t;
}
function tw(e, t, n) {
  if (qt(e, ""), e.append(el(t)), n) {
    const r = document.createElement("p");
    r.className = "confirmation-note", W(r, n), e.append(r);
  }
}
function Xn(e) {
  const t = document.createElement("button");
  return t.type = "button", t.className = "action dynamic", W(t, e), t;
}
function tl(e = ge("")) {
  const t = document.createElement("p");
  return t.className = "setup-status dynamic", W(t, e), t;
}
let xr = null;
function fs() {
  return new Error("operation-superseded");
}
function Dn(e) {
  const t = e.reason;
  return t instanceof Error ? t : fs();
}
function Oa(e) {
  if (e?.aborted) throw Dn(e);
}
function or(e) {
  xr?.abort(fs());
  const t = new AbortController();
  return xr = t, Promise.resolve().then(() => e(t.signal)).finally(() => {
    xr === t && (xr = null);
  });
}
async function nw(e) {
  return or(async (t) => {
    Oa(t), Oa(e.abortSignal);
    const n = new TextDecoder().decode(e.plaintext), r = ls("plaintext.title", ge(e.name), "plaintext.body"), i = document.createElement("pre");
    i.className = "plaintext dynamic", i.textContent = n;
    const o = Xn("common.done"), s = Xn("plaintext.copy");
    s.classList.add("secondary");
    const a = tl("plaintext.copyWarning");
    an(r.card, i), an(r.card, a), an(r.card, s), an(r.card, o);
    try {
      await new Promise((c, u) => {
        if (t.aborted) {
          u(Dn(t));
          return;
        }
        if (e.abortSignal?.aborted) {
          u(Dn(e.abortSignal));
          return;
        }
        let l = !1, d = () => {
        }, h = null, f = null, b = !e.prepareCopy;
        const y = () => {
          h?.abort(fs());
        }, p = (I) => {
          l || (l = !0, d(), y(), I());
        }, g = (I) => {
          p(() => u(Dn(I)));
        }, v = () => g(t), A = () => {
          e.abortSignal && g(e.abortSignal);
        };
        d = () => {
          t.removeEventListener("abort", v), e.abortSignal?.removeEventListener("abort", A);
        }, t.addEventListener("abort", v, { once: !0 }), e.abortSignal?.addEventListener("abort", A, { once: !0 }), s.addEventListener("click", () => {
          if (f) return;
          if (t.aborted) {
            g(t);
            return;
          }
          if (e.abortSignal?.aborted) {
            g(e.abortSignal);
            return;
          }
          s.disabled = !0;
          const I = new AbortController();
          h = I;
          const _ = () => {
            try {
              return Promise.resolve(e.onCopy ? e.onCopy(n, I.signal) : navigator.clipboard.writeText(n));
            } catch (C) {
              return Promise.reject(C);
            }
          }, O = e.prepareCopy && !b ? Promise.resolve(e.prepareCopy(I.signal)).then(() => {
            I.signal.aborted || t.aborted || e.abortSignal?.aborted || (b = !0, W(a, "plaintext.copyReady"), s.disabled = !1);
          }) : _().then(() => {
            I.signal.aborted || t.aborted || e.abortSignal?.aborted || (e.prepareCopy && (b = !1), W(a, "plaintext.copiedBody"), s.disabled = !1);
          });
          f = O, O.then(
            () => {
              I.signal.aborted;
            },
            (C) => {
              if (!(I.signal.aborted || l)) {
                if (t.aborted) {
                  g(t);
                  return;
                }
                if (e.abortSignal?.aborted) {
                  g(e.abortSignal);
                  return;
                }
                s.disabled = !1, W(a, "plaintext.copyFailed");
              }
            }
          ).finally(() => {
            h === I && (h = null), f === O && (f = null);
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
      ds();
    }
  });
}
function ps(e, t, n, r) {
  const i = ls(e.title, e.subtitle, e.review ? void 0 : e.body);
  document.body.classList.add("confirming"), i.card.classList.add("confirm-card"), e.review ? Gg(i.body, e.review, {
    session: e.reviewSession,
    rows: e.details && el(e.details)
  }) : e.details && tw(i.body, e.details, e.body);
  const o = Xn(e.confirmLabel), s = Xn("common.cancel");
  return s.classList.add("secondary"), Ta(i.card, s), Ta(i.card, o), o.disabled = !0, new Promise((a, c) => {
    let u = !1, l;
    const d = setTimeout(() => {
      try {
        l = n?.(i.card);
      } catch (y) {
        f(() => c(y));
        return;
      }
      (l?.ready ?? Promise.resolve()).then(
        () => {
          !u && !t.aborted && (o.disabled = !1);
        },
        (y) => f(() => c(y))
      );
    }, 500), h = () => {
      f(() => c(Dn(t)));
    }, f = (b) => {
      u || (u = !0, clearTimeout(d), l?.dispose(), t.removeEventListener("abort", h), ds(), b());
    };
    if (t.aborted) {
      h();
      return;
    }
    t.addEventListener("abort", h, { once: !0 }), o.addEventListener("click", () => {
      if (o.disabled) return;
      o.disabled = !0, s.disabled = !0;
      let b;
      try {
        l?.assertCurrent(), b = r ? r() : Promise.resolve();
      } catch (y) {
        f(() => c(y));
        return;
      }
      b.then(
        () => f(a),
        (y) => f(() => c(y))
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
function ii(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function rw(e) {
  return new Promise((t, n) => {
    const r = () => n(ii(e));
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function La(e) {
  if (e?.aborted) throw ii(e);
}
async function nl(e) {
  const t = ss(e.wrapMeta), n = jo(t.wrapMeta);
  let r;
  try {
    const i = await Promise.race([
      as(n, e.currentRpId, e.abortSignal).then(
        (o) => {
          if (e.abortSignal?.aborted)
            throw o.prfOutput.fill(0), ii(e.abortSignal);
          return o;
        },
        (o) => {
          throw e.abortSignal?.aborted ? ii(e.abortSignal) : o;
        }
      ),
      ...e.abortSignal ? [rw(e.abortSignal)] : []
    ]);
    return r = i.prfOutput, await rl({
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
async function rl(e) {
  const t = ss(e.wrapMeta);
  let n;
  try {
    La(e.abortSignal), n = await xy(t.wrapMeta, {
      kind: "passkey",
      prfOutput: e.assertion.prfOutput,
      prfSalt: e.assertion.prfSalt
    }), La(e.abortSignal);
    const r = Vu({
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
function iw(e) {
  if (typeof e != "object" || e === null)
    throw new m("invalid_payload", "request payload must be an object");
  return e;
}
function ow(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new m("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ra(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function sw(e) {
  const t = iw(e), n = t.kind === "keypair" ? "keypair" : t.kind === "static" ? "static" : null;
  if (!n) throw new m("invalid_payload", "kind must be static or keypair");
  const r = Ra(t.name, "name");
  if (t.rootMetadata !== void 0) throw new m("invalid_payload", "seal cannot set vault root metadata");
  const i = ow(t.wrapMeta, "wrapMeta");
  if (n === "static") {
    if (t.curve !== void 0) throw new m("invalid_payload", "static seal cannot carry a curve");
    if (t.inputMode !== "parent-value")
      throw new m("invalid_payload", "static seal inputMode must be parent-value");
    return {
      name: r,
      kind: "static",
      inputMode: "parent-value",
      value: Ra(t.value, "value"),
      wrapMeta: i
    };
  }
  if (t.inputMode !== void 0)
    throw new m("invalid_payload", "keypair seal is generate-only and does not accept inputMode");
  if (t.value !== void 0)
    throw new m("invalid_payload", "keypair seal cannot accept parent-provided private key material");
  let o;
  try {
    o = vn(t.curve);
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
function hs(e) {
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
const aw = 512, il = "avibe-vault-signed-context-replay:v2", cw = "avibe-vault-signed-context-replay:v2", uw = "avibe-vault-signed-context-replay:v2", He = /* @__PURE__ */ new Map();
let Ca = !1, zn = null;
function Ce(e) {
  return typeof e == "object" && e !== null;
}
function lt(e, t) {
  if (typeof e != "string" || e.length === 0) throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Pn(e, t) {
  if (e !== void 0) {
    if (typeof e != "string") throw new m("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Mi(e, t) {
  if (e !== void 0) {
    if (!Array.isArray(e) || !e.every((n) => typeof n == "string"))
      throw new m("invalid_payload", `${t} must be a string array`);
    return e;
  }
}
function lw(e, t) {
  if (e !== "static" && e !== "keypair") throw new m("invalid_payload", `${t} must be static or keypair`);
  return e;
}
function dw(e) {
  if (e !== "agent-deliver" && e !== "sign" && e !== "reveal")
    throw new m("invalid_payload", "context purpose is invalid");
  return e;
}
function lo(e, t) {
  if (typeof e != "number" || !Number.isSafeInteger(e) || e < 0)
    throw new m("invalid_payload", `${t} must be a non-negative integer`);
  return e;
}
function fw(e, t) {
  if (!Array.isArray(e) || e.length < 16 || e.length > 128 || !e.every((n) => Number.isInteger(n) && n >= 0 && n <= 255))
    throw new m("invalid_payload", `${t} must contain 16..128 byte integers`);
  return e;
}
function ys(e, t) {
  if (!Ce(e)) throw new m(t, "agent delivery context.release must be an object");
  return {
    name: lt(e.name, "context.release.name"),
    ttlSecs: lo(e.ttlSecs, "context.release.ttlSecs"),
    approvalNonce: fw(e.approvalNonce, "context.release.approvalNonce"),
    approvalExpiresAtUnix: lo(e.approvalExpiresAtUnix, "context.release.approvalExpiresAtUnix"),
    operationHash: lt(e.operationHash, "context.release.operationHash")
  };
}
function nt(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(nt).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${nt(t[n])}`).join(",")}}`;
}
function gs(e) {
  const t = Ce(e) ? e : null;
  if (!t) throw new m("invalid_payload", "context must be an object");
  if (t.v !== 2) throw new m("invalid_payload", "context version must be 2");
  const n = dw(t.purpose);
  lt(t.requestId, "context.requestId"), t.grantId !== void 0 && Pn(t.grantId, "context.grantId");
  const r = Ce(t.display) ? t.display : null;
  if (!r) throw new m("invalid_payload", "context.display must be an object");
  if (!Array.isArray(r.secrets) || r.secrets.length === 0)
    throw new m("invalid_payload", "context.display.secrets must be a non-empty array");
  r.secrets.forEach((c, u) => {
    if (!Ce(c)) throw new m("invalid_payload", `context.display.secrets[${u}] must be an object`);
    lt(c.name, `context.display.secrets[${u}].name`), lw(c.kind, `context.display.secrets[${u}].kind`);
  }), r.sessionLabel !== void 0 && Pn(r.sessionLabel, "context.display.sessionLabel"), r.command !== void 0 && Pn(r.command, "context.display.command"), r.egress !== void 0 && Pn(r.egress, "context.display.egress");
  const i = r.source;
  if (i !== void 0) {
    if (!Ce(i)) throw new m("invalid_payload", "context.display.source must be an object");
    i.env !== void 0 && Mi(i.env, "context.display.source.env"), i.tags !== void 0 && Mi(i.tags, "context.display.source.tags"), i.skills !== void 0 && Mi(i.skills, "context.display.source.skills");
  }
  r.grantTtlSeconds !== void 0 && lo(r.grantTtlSeconds, "context.display.grantTtlSeconds");
  const o = Ce(t.signature) ? t.signature : null;
  if (!o) throw new m("invalid_payload", "context.signature must be an object");
  if (o.alg !== "ed25519") throw new m("invalid_payload", "context signature alg must be ed25519");
  lt(o.keyId, "context.signature.keyId"), lt(o.value, "context.signature.value");
  const s = t.agent;
  if (s !== void 0) {
    if (!Ce(s)) throw new m("invalid_payload", "context.agent must be an object");
    const c = Ce(s.publicKey) ? s.publicKey : null;
    if (!c) throw new m("invalid_payload", "context.agent.publicKey must be an object");
    lt(c.public_key, "context.agent.publicKey.public_key"), c.fingerprint !== void 0 && Pn(c.fingerprint, "context.agent.publicKey.fingerprint"), lt(s.fingerprint, "context.agent.fingerprint");
  }
  const a = t.release;
  if (n === "agent-deliver") {
    const c = ys(a, "invalid_payload");
    if (r.grantTtlSeconds === void 0)
      throw new m("invalid_payload", "agent delivery context must display its release TTL");
    if (r.grantTtlSeconds !== c.ttlSecs)
      throw new m("invalid_payload", "signed release TTL does not match display TTL");
  } else if (a !== void 0 && !Ce(a))
    throw new m("invalid_payload", "context.release must be an object");
  return lt(t.expiresAt, "context.expiresAt"), t;
}
function bi(e) {
  const { signature: t, ...n } = e;
  return nt(n);
}
function fo(e) {
  if (e.context.purpose !== e.expectedPurpose)
    throw new m("invalid_context", `${e.expectedPurpose} context has the wrong purpose`);
  if (!e.context.display.secrets.some(
    (n) => n.name === e.secret.name && n.kind === e.secret.kind
  )) {
    const n = e.secret.kind === "keypair" ? "key" : "secret";
    throw new m("invalid_context", `${e.expectedPurpose} context does not cover the requested ${n}`);
  }
}
function oi(e) {
  if (e.expectedPurpose && e.context.purpose !== e.expectedPurpose)
    throw new m("invalid_context", "signed context purpose does not match operation");
  const t = e.now ?? Date.now(), n = Date.parse(e.context.expiresAt);
  if (!Number.isFinite(n) || n <= t)
    throw new m("context_expired", "signed context is expired", !0);
  if (!qy({
    rootMetadata: e.rootMetadata,
    keyId: e.context.signature.keyId,
    signature: e.context.signature.value,
    message: bi(e.context)
  })) throw new m("invalid_context_signature", "signed context signature is invalid");
  if (e.context.purpose === "agent-deliver" && ys(e.context.release, "invalid_context").approvalExpiresAtUnix * 1e3 <= t)
    throw new m("context_expired", "signed release approval is expired", !0);
}
function ws(e) {
  for (const [t, n] of He)
    n <= e && He.delete(t);
}
function pw() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
function wn() {
  return typeof window < "u";
}
function bs() {
  return new m("context_replay_state_unavailable", "signed context replay state is unavailable", !0);
}
function ol(e) {
  const t = pw();
  if (!t && e && wn()) throw bs();
  return t;
}
function sl(e, t) {
  const n = /* @__PURE__ */ new Map();
  if (!Ce(e) || e.version !== 2 || !Array.isArray(e.entries)) return n;
  for (const r of e.entries) {
    if (!Array.isArray(r) || r.length !== 2) continue;
    const [i, o] = r;
    typeof i != "string" || i.length === 0 || typeof o != "number" || !Number.isFinite(o) || o > t && n.set(i, Math.max(n.get(i) ?? 0, o));
  }
  return n;
}
function ms(e = Date.now(), t = !1) {
  ws(e);
  const n = ol(t);
  if (n)
    try {
      n.setItem(il, JSON.stringify({ version: 2, entries: [...He] }));
    } catch {
      if (t && wn()) throw bs();
    }
}
function al(e, t = Date.now(), n = !0) {
  for (const [r, i] of e)
    i > t && He.set(r, Math.max(He.get(r) ?? 0, i));
  n && ms(t);
}
function vs() {
  if (!(zn || typeof BroadcastChannel > "u"))
    try {
      zn = new BroadcastChannel(cw), zn.addEventListener("message", (e) => {
        const t = Ce(e.data) ? e.data : null;
        !t || t.type !== "signed-context-consumed" || al(sl({ version: 2, entries: t.entries }, Date.now()));
      });
    } catch {
      zn = null;
    }
}
function hw(e = Date.now()) {
  Ca || (vs(), cl(e, wn()), Ca = !0);
}
function cl(e = Date.now(), t = !1) {
  vs();
  const n = ol(t);
  if (n)
    try {
      al(sl(JSON.parse(n.getItem(il) ?? "null"), e), e, !1), ws(e);
    } catch {
      if (t && wn()) throw bs();
      ms(e);
    }
}
function yw(e) {
  vs();
  try {
    zn?.postMessage({ type: "signed-context-consumed", entries: e });
  } catch {
  }
}
function gw() {
  const t = globalThis.navigator?.locks;
  return !t || typeof t != "object" || typeof t.request != "function" ? null : t;
}
async function ww(e) {
  const t = gw();
  if (t) return await t.request(uw, { mode: "exclusive" }, e);
  if (typeof window < "u")
    throw new m("context_replay_lock_unavailable", "signed context replay lock is unavailable", !0);
  return await e();
}
function ul(e, t) {
  hw(t);
  const n = /* @__PURE__ */ new Map();
  for (const r of e) {
    const i = Date.parse(r.expiresAt);
    if (!Number.isFinite(i) || i <= t)
      throw new m("context_expired", "signed context is expired", !0);
    n.set(r.requestId, Math.max(n.get(r.requestId) ?? 0, i));
  }
  ws(t);
  for (const r of n.keys())
    if (He.has(r))
      throw new m("context_replayed", "signed context requestId was already used", !0);
  if (He.size + n.size > aw)
    throw new m("context_replay_cache_full", "signed context replay cache is full", !0);
  return n;
}
function si(e, t = Date.now()) {
  ul(e, t);
}
async function mi(e, t = Date.now()) {
  const n = [...e];
  await ww(() => {
    cl(t, wn());
    const r = ul(n, t), i = /* @__PURE__ */ new Map();
    for (const o of r.keys()) i.set(o, He.get(o));
    for (const [o, s] of r) He.set(o, s);
    try {
      ms(t, wn());
    } catch (o) {
      for (const [s, a] of i)
        a === void 0 ? He.delete(s) : He.set(s, a);
      throw o;
    }
    yw([...r]);
  });
}
async function bw(e, t) {
  if (e.purpose !== "agent-deliver") throw new m("invalid_context", "context is not an agent delivery");
  if (!e.grantId) throw new m("invalid_context", "agent delivery context is missing grantId");
  const n = ys(e.release, "invalid_context");
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
function mw(e) {
  if (!e.agent) throw new m("invalid_context", "agent delivery context is missing agent key");
  if (e.agent.publicKey.fingerprint && e.agent.publicKey.fingerprint !== e.agent.fingerprint)
    throw new m("invalid_context", "agent public key fingerprint mismatch");
  return { ...e.agent.publicKey, fingerprint: e.agent.fingerprint };
}
async function vw(e) {
  const t = e.map((n) => bi(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(nt(t))));
}
function za(e) {
  return nt(e.display);
}
const xw = "1.2.3";
let Le = class po extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof po ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof po && n.cause.docsPath || n.docsPath, o = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...i ? [`Docs: https://abitype.dev${i}`] : [],
      ...r ? [`Details: ${r}`] : [],
      `Version: abitype@${xw}`
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
function gt(e, t) {
  return e.exec(t)?.groups;
}
const ll = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, dl = /^u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/, fl = /^\(.+?\).*?$/, Ua = /^tuple(?<array>(\[(\d*)\])*)$/;
function ho(e) {
  let t = e.type;
  if (Ua.test(e.type) && "components" in e) {
    t = "(";
    const n = e.components.length;
    for (let i = 0; i < n; i++) {
      const o = e.components[i];
      t += ho(o), i < n - 1 && (t += ", ");
    }
    const r = gt(Ua, e.type);
    return t += `)${r?.array || ""}`, ho({
      ...e,
      type: t
    });
  }
  return "indexed" in e && e.indexed && (t = `${t} indexed`), e.name ? `${t} ${e.name}` : t;
}
function Bn(e) {
  let t = "";
  const n = e.length;
  for (let r = 0; r < n; r++) {
    const i = e[r];
    t += ho(i), r !== n - 1 && (t += ", ");
  }
  return t;
}
function Ew(e) {
  return e.type === "function" ? `function ${e.name}(${Bn(e.inputs)})${e.stateMutability && e.stateMutability !== "nonpayable" ? ` ${e.stateMutability}` : ""}${e.outputs?.length ? ` returns (${Bn(e.outputs)})` : ""}` : e.type === "event" ? `event ${e.name}(${Bn(e.inputs)})` : e.type === "error" ? `error ${e.name}(${Bn(e.inputs)})` : e.type === "constructor" ? `constructor(${Bn(e.inputs)})${e.stateMutability === "payable" ? " payable" : ""}` : e.type === "fallback" ? `fallback() external${e.stateMutability === "payable" ? " payable" : ""}` : "receive() external payable";
}
const pl = /^error (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function kw(e) {
  return pl.test(e);
}
function Sw(e) {
  return gt(pl, e);
}
const hl = /^event (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function Aw(e) {
  return hl.test(e);
}
function _w(e) {
  return gt(hl, e);
}
const yl = /^function (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)(?: (?<scope>external|public{1}))?(?: (?<stateMutability>pure|view|nonpayable|payable{1}))?(?: returns\s?\((?<returns>.*?)\))?$/;
function Iw(e) {
  return yl.test(e);
}
function Pw(e) {
  return gt(yl, e);
}
const gl = /^struct (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*) \{(?<properties>.*?)\}$/;
function wl(e) {
  return gl.test(e);
}
function Bw(e) {
  return gt(gl, e);
}
const bl = /^constructor\((?<parameters>.*?)\)(?:\s(?<stateMutability>payable{1}))?$/;
function $w(e) {
  return bl.test(e);
}
function Tw(e) {
  return gt(bl, e);
}
const ml = /^fallback\(\) external(?:\s(?<stateMutability>payable{1}))?$/;
function Ow(e) {
  return ml.test(e);
}
function Lw(e) {
  return gt(ml, e);
}
const Rw = /^receive\(\) external payable$/;
function Cw(e) {
  return Rw.test(e);
}
const zw = /* @__PURE__ */ new Set(["indexed"]), yo = /* @__PURE__ */ new Set([
  "calldata",
  "memory",
  "storage"
]);
class Uw extends Le {
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
class Nw extends Le {
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
class Mw extends Le {
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
class Hw extends Le {
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
class Fw extends Le {
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
class Kw extends Le {
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
class Dw extends Le {
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
class xn extends Le {
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
class jw extends Le {
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
class Vw extends Le {
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
class qw extends Le {
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
class Gw extends Le {
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
function Ww(e, t, n) {
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
const Hi = /* @__PURE__ */ new Map([
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
function Zw(e, t = {}) {
  if (Iw(e))
    return Xw(e, t);
  if (Aw(e))
    return Yw(e, t);
  if (kw(e))
    return Jw(e, t);
  if ($w(e))
    return Qw(e, t);
  if (Ow(e))
    return eb(e);
  if (Cw(e))
    return {
      type: "receive",
      stateMutability: "payable"
    };
  throw new jw({ signature: e });
}
function Xw(e, t = {}) {
  const n = Pw(e);
  if (!n)
    throw new xn({ signature: e, type: "function" });
  const r = Ue(n.parameters), i = [], o = r.length;
  for (let a = 0; a < o; a++)
    i.push(Gt(r[a], {
      modifiers: yo,
      structs: t,
      type: "function"
    }));
  const s = [];
  if (n.returns) {
    const a = Ue(n.returns), c = a.length;
    for (let u = 0; u < c; u++)
      s.push(Gt(a[u], {
        modifiers: yo,
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
function Yw(e, t = {}) {
  const n = _w(e);
  if (!n)
    throw new xn({ signature: e, type: "event" });
  const r = Ue(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(Gt(r[s], {
      modifiers: zw,
      structs: t,
      type: "event"
    }));
  return { name: n.name, type: "event", inputs: i };
}
function Jw(e, t = {}) {
  const n = Sw(e);
  if (!n)
    throw new xn({ signature: e, type: "error" });
  const r = Ue(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(Gt(r[s], { structs: t, type: "error" }));
  return { name: n.name, type: "error", inputs: i };
}
function Qw(e, t = {}) {
  const n = Tw(e);
  if (!n)
    throw new xn({ signature: e, type: "constructor" });
  const r = Ue(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(Gt(r[s], { structs: t, type: "constructor" }));
  return {
    type: "constructor",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i
  };
}
function eb(e) {
  const t = Lw(e);
  if (!t)
    throw new xn({ signature: e, type: "fallback" });
  return {
    type: "fallback",
    stateMutability: t.stateMutability ?? "nonpayable"
  };
}
const tb = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, nb = /^\((?<type>.+?)\)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, rb = /^u?int$/;
function Gt(e, t) {
  const n = Ww(e, t?.type, t?.structs);
  if (Hi.has(n))
    return Hi.get(n);
  const r = fl.test(e), i = gt(r ? nb : tb, e);
  if (!i)
    throw new Mw({ param: e });
  if (i.name && ob(i.name))
    throw new Hw({ param: e, name: i.name });
  const o = i.name ? { name: i.name } : {}, s = i.modifier === "indexed" ? { indexed: !0 } : {}, a = t?.structs ?? {};
  let c, u = {};
  if (r) {
    c = "tuple";
    const d = Ue(i.type), h = [], f = d.length;
    for (let b = 0; b < f; b++)
      h.push(Gt(d[b], { structs: a }));
    u = { components: h };
  } else if (i.type in a)
    c = "tuple", u = { components: a[i.type] };
  else if (rb.test(i.type))
    c = `${i.type}256`;
  else if (i.type === "address payable")
    c = "address";
  else if (c = i.type, t?.type !== "struct" && !vl(c))
    throw new Nw({ type: c });
  if (i.modifier) {
    if (!t?.modifiers?.has?.(i.modifier))
      throw new Fw({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
    if (yo.has(i.modifier) && !sb(c, !!i.array))
      throw new Kw({
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
  return Hi.set(n, l), l;
}
function Ue(e, t = [], n = "", r = 0) {
  const i = e.trim().length;
  for (let o = 0; o < i; o++) {
    const s = e[o], a = e.slice(o + 1);
    switch (s) {
      case ",":
        return r === 0 ? Ue(a, [...t, n.trim()]) : Ue(a, t, `${n}${s}`, r);
      case "(":
        return Ue(a, t, `${n}${s}`, r + 1);
      case ")":
        return Ue(a, t, `${n}${s}`, r - 1);
      default:
        return Ue(a, t, `${n}${s}`, r);
    }
  }
  if (n === "")
    return t;
  if (r !== 0)
    throw new Gw({ current: n, depth: r });
  return t.push(n.trim()), t;
}
function vl(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || ll.test(e) || dl.test(e);
}
const ib = /^(?:after|alias|anonymous|apply|auto|byte|calldata|case|catch|constant|copyof|default|defined|error|event|external|false|final|function|immutable|implements|in|indexed|inline|internal|let|mapping|match|memory|mutable|null|of|override|partial|private|promise|public|pure|reference|relocatable|return|returns|sizeof|static|storage|struct|super|supports|switch|this|true|try|typedef|typeof|var|view|virtual)$/;
function ob(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || e === "tuple" || ll.test(e) || dl.test(e) || ib.test(e);
}
function sb(e, t) {
  return t || e === "bytes" || e === "string" || e === "tuple";
}
function ab(e) {
  const t = {}, n = e.length;
  for (let s = 0; s < n; s++) {
    const a = e[s];
    if (!wl(a))
      continue;
    const c = Bw(a);
    if (!c)
      throw new xn({ signature: a, type: "struct" });
    const u = c.properties.split(";"), l = [], d = u.length;
    for (let h = 0; h < d; h++) {
      const b = u[h].trim();
      if (!b)
        continue;
      const y = Gt(b, {
        type: "struct"
      });
      l.push(y);
    }
    if (!l.length)
      throw new Vw({ signature: a });
    t[c.name] = l;
  }
  const r = {}, i = Object.entries(t), o = i.length;
  for (let s = 0; s < o; s++) {
    const [a, c] = i[s];
    r[a] = xl(c, t);
  }
  return r;
}
const cb = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*)(?<array>(?:\[\d*?\])+?)?$/;
function xl(e = [], t = {}, n = /* @__PURE__ */ new Set()) {
  const r = [], i = e.length;
  for (let o = 0; o < i; o++) {
    const s = e[o];
    if (fl.test(s.type))
      r.push(s);
    else {
      const c = gt(cb, s.type);
      if (!c?.type)
        throw new Dw({ abiParameter: s });
      const { array: u, type: l } = c;
      if (l in t) {
        if (n.has(l))
          throw new qw({ type: l });
        r.push({
          ...s,
          type: `tuple${u ?? ""}`,
          components: xl(t[l], t, /* @__PURE__ */ new Set([...n, l]))
        });
      } else if (vl(l))
        r.push(s);
      else
        throw new Uw({ type: l });
    }
  }
  return r;
}
function ub(e) {
  const t = ab(e), n = [], r = e.length;
  for (let i = 0; i < r; i++) {
    const o = e[i];
    wl(o) || n.push(Zw(o, t));
  }
  return n;
}
function El(e, { includeName: t = !1 } = {}) {
  return e ? e.map((n) => lb(n, { includeName: t })).join(t ? ", " : ",") : "";
}
function lb(e, { includeName: t }) {
  return e.type.startsWith("tuple") ? `(${El(e.components, { includeName: t })})${e.type.slice(5)}` : e.type + (t && e.name ? ` ${e.name}` : "");
}
function sr(e, { strict: t = !0 } = {}) {
  return !e || typeof e != "string" ? !1 : t ? /^0x[0-9a-fA-F]*$/.test(e) : e.startsWith("0x");
}
function le(e) {
  return sr(e, { strict: !1 }) ? Math.ceil((e.length - 2) / 2) : e.length;
}
const kl = "2.54.6";
let Fi = {
  getDocsUrl: ({ docsBaseUrl: e, docsPath: t = "", docsSlug: n }) => t ? `${e ?? "https://viem.sh"}${t}${n ? `#${n}` : ""}` : void 0,
  version: `viem@${kl}`
};
class D extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof D ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof D && n.cause.docsPath || n.docsPath, o = Fi.getDocsUrl?.({ ...n, docsPath: i }), s = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...o ? [`Docs: ${o}`] : [],
      ...r ? [`Details: ${r}`] : [],
      ...Fi.version ? [`Version: ${Fi.version}`] : []
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
    }), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.name = n.name ?? this.name, this.shortMessage = t, this.version = kl;
  }
  walk(t) {
    return Sl(this, t);
  }
}
function Sl(e, t) {
  return t?.(e) ? e : e && typeof e == "object" && "cause" in e && e.cause !== void 0 ? Sl(e.cause, t) : t ? null : e;
}
class db extends D {
  constructor({ data: t, params: n, size: r }) {
    super([`Data size of ${r} bytes is too small for given parameters.`].join(`
`), {
      metaMessages: [
        `Params: (${El(n, { includeName: !0 })})`,
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
class fb extends D {
  constructor({ cause: t } = {}) {
    super('Cannot decode zero data ("0x") with ABI parameters.', {
      name: "AbiDecodingZeroDataError",
      cause: t
    });
  }
}
class pb extends D {
  constructor({ expectedLength: t, givenLength: n, type: r }) {
    super([
      `ABI encoding array length mismatch for type ${r}.`,
      `Expected length: ${t}`,
      `Given length: ${n}`
    ].join(`
`), { name: "AbiEncodingArrayLengthMismatchError" });
  }
}
class hb extends D {
  constructor({ expectedSize: t, value: n }) {
    super(`Size of bytes "${n}" (bytes${le(n)}) does not match expected size (bytes${t}).`, { name: "AbiEncodingBytesSizeMismatchError" });
  }
}
class yb extends D {
  constructor({ expectedLength: t, givenLength: n }) {
    super([
      "ABI encoding params/values length mismatch.",
      `Expected length (params): ${t}`,
      `Given length (values): ${n}`
    ].join(`
`), { name: "AbiEncodingLengthMismatchError" });
  }
}
class gb extends D {
  constructor({ expectedSize: t, givenSize: n }) {
    super(`Expected bytes${t}, got bytes${n}.`, {
      name: "BytesSizeMismatchError"
    });
  }
}
class wb extends D {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid encoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiEncodingType" });
  }
}
class bb extends D {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid decoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiDecodingType" });
  }
}
class mb extends D {
  constructor(t) {
    super([`Value "${t}" is not a valid array.`].join(`
`), {
      name: "InvalidArrayError"
    });
  }
}
class Al extends D {
  constructor({ offset: t, position: n, size: r }) {
    super(`Slice ${n === "start" ? "starting" : "ending"} at offset "${t}" is out-of-bounds (size: ${r}).`, { name: "SliceOffsetOutOfBoundsError" });
  }
}
class _l extends D {
  constructor({ size: t, targetSize: n, type: r }) {
    super(`${r.charAt(0).toUpperCase()}${r.slice(1).toLowerCase()} size (${t}) exceeds padding size (${n}).`, { name: "SizeExceedsPaddingSizeError" });
  }
}
function En(e, { dir: t, size: n = 32 } = {}) {
  return typeof e == "string" ? Tt(e, { dir: t, size: n }) : vb(e, { dir: t, size: n });
}
function Tt(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  const r = e.replace("0x", "");
  if (r.length > n * 2)
    throw new _l({
      size: Math.ceil(r.length / 2),
      targetSize: n,
      type: "hex"
    });
  return `0x${r[t === "right" ? "padEnd" : "padStart"](n * 2, "0")}`;
}
function vb(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  if (e.length > n)
    throw new _l({
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
class xs extends D {
  constructor({ max: t, min: n, signed: r, size: i, value: o }) {
    super(`Number "${o}" is not in safe ${i ? `${i * 8}-bit ${r ? "signed" : "unsigned"} ` : ""}integer range ${t ? `(${n} to ${t})` : `(above ${n})`}`, { name: "IntegerOutOfRangeError" });
  }
}
class xb extends D {
  constructor(t) {
    super(`Bytes value "${t}" is not a valid boolean. The bytes array must contain a single byte of either a 0 or 1 value.`, {
      name: "InvalidBytesBooleanError"
    });
  }
}
class Eb extends D {
  constructor({ givenSize: t, maxSize: n }) {
    super(`Size cannot exceed ${n} bytes. Given size: ${t} bytes.`, { name: "SizeOverflowError" });
  }
}
function Yn(e, { dir: t = "left" } = {}) {
  let n = typeof e == "string" ? e.replace("0x", "") : e, r = 0;
  for (let i = 0; i < n.length - 1 && n[t === "left" ? i : n.length - i - 1].toString() === "0"; i++)
    r++;
  return n = t === "left" ? n.slice(r) : n.slice(0, n.length - r), typeof e == "string" ? (n.length === 1 && t === "right" && (n = `${n}0`), `0x${n.length % 2 === 1 ? `0${n}` : n}`) : n;
}
function it(e, { size: t }) {
  if (le(e) > t)
    throw new Eb({
      givenSize: le(e),
      maxSize: t
    });
}
function Il(e, t = {}) {
  const { signed: n } = t;
  t.size && it(e, { size: t.size });
  const r = BigInt(e);
  if (!n)
    return r;
  const i = (e.length - 2) / 2, o = (1n << BigInt(i) * 8n - 1n) - 1n;
  return r <= o ? r : r - BigInt(`0x${"f".padStart(i * 2, "f")}`) - 1n;
}
function Es(e, t = {}) {
  const n = Il(e, t), r = Number(n);
  if (!Number.isSafeInteger(r))
    throw new xs({
      max: `${Number.MAX_SAFE_INTEGER}`,
      min: `${Number.MIN_SAFE_INTEGER}`,
      signed: t.signed,
      size: t.size,
      value: `${n}n`
    });
  return r;
}
const kb = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Jn(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? K(e, t) : typeof e == "string" ? Bl(e, t) : typeof e == "boolean" ? Pl(e, t) : Se(e, t);
}
function Pl(e, t = {}) {
  const n = `0x${Number(e)}`;
  return typeof t.size == "number" ? (it(n, { size: t.size }), En(n, { size: t.size })) : n;
}
function Se(e, t = {}) {
  let n = "";
  for (let i = 0; i < e.length; i++)
    n += kb[e[i]];
  const r = `0x${n}`;
  return typeof t.size == "number" ? (it(r, { size: t.size }), En(r, { dir: "right", size: t.size })) : r;
}
function K(e, t = {}) {
  const { signed: n, size: r } = t, i = BigInt(e);
  let o;
  r ? n ? o = (1n << BigInt(r) * 8n - 1n) - 1n : o = 2n ** (BigInt(r) * 8n) - 1n : typeof e == "number" && (o = BigInt(Number.MAX_SAFE_INTEGER));
  const s = typeof o == "bigint" && n ? -o - 1n : 0;
  if (o && i > o || i < s) {
    const c = typeof e == "bigint" ? "n" : "";
    throw new xs({
      max: o ? `${o}${c}` : void 0,
      min: `${s}${c}`,
      signed: n,
      size: r,
      value: `${e}${c}`
    });
  }
  const a = `0x${(n && i < 0 ? (1n << BigInt(r * 8)) + BigInt(i) : i).toString(16)}`;
  return r ? En(a, { size: r }) : a;
}
const Sb = /* @__PURE__ */ new TextEncoder();
function Bl(e, t = {}) {
  const n = Sb.encode(e);
  return Se(n, t);
}
const Ab = /* @__PURE__ */ new TextEncoder();
function ks(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? Ib(e, t) : typeof e == "boolean" ? _b(e, t) : sr(e) ? Lt(e, t) : $l(e, t);
}
function _b(e, t = {}) {
  const n = new Uint8Array(1);
  return n[0] = Number(e), typeof t.size == "number" ? (it(n, { size: t.size }), En(n, { size: t.size })) : n;
}
const ut = {
  zero: 48,
  nine: 57,
  A: 65,
  F: 70,
  a: 97,
  f: 102
};
function Na(e) {
  if (e >= ut.zero && e <= ut.nine)
    return e - ut.zero;
  if (e >= ut.A && e <= ut.F)
    return e - (ut.A - 10);
  if (e >= ut.a && e <= ut.f)
    return e - (ut.a - 10);
}
function Lt(e, t = {}) {
  let n = e;
  t.size && (it(n, { size: t.size }), n = En(n, { dir: "right", size: t.size }));
  let r = n.slice(2);
  r.length % 2 && (r = `0${r}`);
  const i = r.length / 2, o = new Uint8Array(i);
  for (let s = 0, a = 0; s < i; s++) {
    const c = Na(r.charCodeAt(a++)), u = Na(r.charCodeAt(a++));
    if (c === void 0 || u === void 0)
      throw new D(`Invalid byte sequence ("${r[a - 2]}${r[a - 1]}" in "${r}").`);
    o[s] = c * 16 + u;
  }
  return o;
}
function Ib(e, t) {
  const n = K(e, t);
  return Lt(n);
}
function $l(e, t = {}) {
  const n = Ab.encode(e);
  return typeof t.size == "number" ? (it(n, { size: t.size }), En(n, { dir: "right", size: t.size })) : n;
}
const Er = /* @__PURE__ */ BigInt(2 ** 32 - 1), Ma = /* @__PURE__ */ BigInt(32);
function Pb(e, t = !1) {
  return t ? { h: Number(e & Er), l: Number(e >> Ma & Er) } : { h: Number(e >> Ma & Er) | 0, l: Number(e & Er) | 0 };
}
function Bb(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = Pb(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const $b = (e, t, n) => e << n | t >>> 32 - n, Tb = (e, t, n) => t << n | e >>> 32 - n, Ob = (e, t, n) => t << n - 32 | e >>> 64 - n, Lb = (e, t, n) => e << n - 32 | t >>> 64 - n;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Rb(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Ha(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function Qn(e, ...t) {
  if (!Rb(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function ai(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Tl(e, t) {
  Qn(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function Cb(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function er(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Ki(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function We(e, t) {
  return e << 32 - t | e >>> t;
}
const zb = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function Ub(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function Nb(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = Ub(e[t]);
  return e;
}
const Fa = zb ? (e) => e : Nb;
function Mb(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function Ss(e) {
  return typeof e == "string" && (e = Mb(e)), Qn(e), e;
}
class Ol {
}
function Ll(e) {
  const t = (r) => e().update(Ss(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
const Hb = BigInt(0), $n = BigInt(1), Fb = BigInt(2), Kb = BigInt(7), Db = BigInt(256), jb = BigInt(113), Rl = [], Cl = [], zl = [];
for (let e = 0, t = $n, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], Rl.push(2 * (5 * r + n)), Cl.push((e + 1) * (e + 2) / 2 % 64);
  let i = Hb;
  for (let o = 0; o < 7; o++)
    t = (t << $n ^ (t >> Kb) * jb) % Db, t & Fb && (i ^= $n << ($n << /* @__PURE__ */ BigInt(o)) - $n);
  zl.push(i);
}
const Ul = Bb(zl, !0), Vb = Ul[0], qb = Ul[1], Ka = (e, t, n) => n > 32 ? Ob(e, t, n) : $b(e, t, n), Da = (e, t, n) => n > 32 ? Lb(e, t, n) : Tb(e, t, n);
function Gb(e, t = 24) {
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, u = n[c], l = n[c + 1], d = Ka(u, l, 1) ^ n[a], h = Da(u, l, 1) ^ n[a + 1];
      for (let f = 0; f < 50; f += 10)
        e[s + f] ^= d, e[s + f + 1] ^= h;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = Cl[s], c = Ka(i, o, a), u = Da(i, o, a), l = Rl[s];
      i = e[l], o = e[l + 1], e[l] = c, e[l + 1] = u;
    }
    for (let s = 0; s < 50; s += 10) {
      for (let a = 0; a < 10; a++)
        n[a] = e[s + a];
      for (let a = 0; a < 10; a++)
        e[s + a] ^= ~n[(a + 2) % 10] & n[(a + 4) % 10];
    }
    e[0] ^= Vb[r], e[1] ^= qb[r];
  }
  er(n);
}
class As extends Ol {
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (super(), this.pos = 0, this.posOut = 0, this.finished = !1, this.destroyed = !1, this.enableXOF = !1, this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.rounds = o, Ha(r), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = Cb(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    Fa(this.state32), Gb(this.state32, this.rounds), Fa(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    ai(this), t = Ss(t), Qn(t);
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
    ai(this, !1), Qn(t), this.finish();
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
    return Ha(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (Tl(t, this), this.finished)
      throw new Error("digest() was already called");
    return this.writeInto(t), this.destroy(), t;
  }
  digest() {
    return this.digestInto(new Uint8Array(this.outputLen));
  }
  destroy() {
    this.destroyed = !0, er(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t || (t = new As(n, r, i, s, o)), t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.destroyed = this.destroyed, t;
  }
}
const Wb = (e, t, n) => Ll(() => new As(t, e, n)), Zb = Wb(1, 136, 256 / 8);
function Je(e, t) {
  const n = t || "hex", r = Zb(sr(e, { strict: !1 }) ? ks(e) : e);
  return n === "bytes" ? r : Jn(r);
}
const Xb = (e) => Je(ks(e));
function Yb(e) {
  return Xb(e);
}
function Jb(e) {
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
const Qb = (e) => {
  const t = typeof e == "string" ? e : Ew(e);
  return Jb(t);
};
function em(e) {
  return Yb(Qb(e));
}
class Zt extends D {
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
class Nl extends Map {
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
const Di = /* @__PURE__ */ new Nl(8192);
function Ml(e, t) {
  if (Di.has(`${e}.${t}`))
    return Di.get(`${e}.${t}`);
  const n = e.substring(2).toLowerCase(), r = Je($l(n), "bytes"), i = n.split("");
  for (let s = 0; s < 40; s += 2)
    r[s >> 1] >> 4 >= 8 && i[s] && (i[s] = i[s].toUpperCase()), (r[s >> 1] & 15) >= 8 && i[s + 1] && (i[s + 1] = i[s + 1].toUpperCase());
  const o = `0x${i.join("")}`;
  return Di.set(`${e}.${t}`, o), o;
}
const tm = /^0x[a-fA-F0-9]{40}$/, ji = /* @__PURE__ */ new Nl(8192);
function Xt(e, t) {
  const { strict: n = !0 } = t ?? {}, r = `${e}.${n}`;
  if (ji.has(r))
    return ji.get(r);
  const i = tm.test(e) ? e.toLowerCase() === e ? !0 : n ? Ml(e) === e : !0 : !1;
  return ji.set(r, i), i;
}
function nm(e) {
  return typeof e[0] == "string" ? Ke(e) : rm(e);
}
function rm(e) {
  let t = 0;
  for (const i of e)
    t += i.length;
  const n = new Uint8Array(t);
  let r = 0;
  for (const i of e)
    n.set(i, r), r += i.length;
  return n;
}
function Ke(e) {
  return `0x${e.reduce((t, n) => t + n.replace("0x", ""), "")}`;
}
function _s(e, t, n, { strict: r } = {}) {
  return sr(e, { strict: !1 }) ? Dl(e, t, n, {
    strict: r
  }) : Kl(e, t, n, {
    strict: r
  });
}
function Hl(e, t) {
  if (typeof t == "number" && t > 0 && t > le(e) - 1)
    throw new Al({
      offset: t,
      position: "start",
      size: le(e)
    });
}
function Fl(e, t, n) {
  if (typeof t == "number" && typeof n == "number" && le(e) !== n - t)
    throw new Al({
      offset: n,
      position: "end",
      size: le(e)
    });
}
function Kl(e, t, n, { strict: r } = {}) {
  Hl(e, t);
  const i = e.slice(t, n);
  return r && Fl(i, t, n), i;
}
function Dl(e, t, n, { strict: r } = {}) {
  Hl(e, t);
  const i = `0x${e.replace("0x", "").slice((t ?? 0) * 2, (n ?? e.length) * 2)}`;
  return r && Fl(i, t, n), i;
}
const im = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, jl = /^(u?int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/;
function Is(e, t) {
  if (e.length !== t.length)
    throw new yb({
      expectedLength: e.length,
      givenLength: t.length
    });
  const n = om({
    params: e,
    values: t
  });
  return Bs(n);
}
function om({ params: e, values: t }) {
  const n = [];
  for (let r = 0; r < e.length; r++)
    n.push(Ps({ param: e[r], value: t[r] }));
  return n;
}
function Ps({ param: e, value: t }) {
  const n = vi(e.type);
  if (n) {
    const [r, i] = n;
    return am(t, { length: r, param: { ...e, type: i } });
  }
  if (e.type === "tuple")
    return fm(t, {
      param: e
    });
  if (e.type === "address")
    return sm(t);
  if (e.type === "bool")
    return um(t);
  if (e.type.startsWith("uint") || e.type.startsWith("int")) {
    const r = e.type.startsWith("int"), [, , i = "256"] = jl.exec(e.type) ?? [];
    return lm(t, {
      signed: r,
      size: Number(i)
    });
  }
  if (e.type.startsWith("bytes"))
    return cm(t, { param: e });
  if (e.type === "string")
    return dm(t);
  throw new wb(e.type, {
    docsPath: "/docs/contract/encodeAbiParameters"
  });
}
function Bs(e) {
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
  return Ke([...n, ...r]);
}
function sm(e) {
  if (!Xt(e))
    throw new Zt({ address: e });
  return { dynamic: !1, encoded: Tt(e.toLowerCase()) };
}
function am(e, { length: t, param: n }) {
  const r = t === null;
  if (!Array.isArray(e))
    throw new mb(e);
  if (!r && e.length !== t)
    throw new pb({
      expectedLength: t,
      givenLength: e.length,
      type: `${n.type}[${t}]`
    });
  let i = e.length === 0 && go(n);
  const o = [];
  for (let s = 0; s < e.length; s++) {
    const a = Ps({ param: n, value: e[s] });
    a.dynamic && (i = !0), o.push(a);
  }
  if (r || i) {
    const s = Bs(o);
    if (r) {
      const a = K(o.length, { size: 32 });
      return {
        dynamic: !0,
        encoded: Ke([a, s])
      };
    }
    if (i)
      return { dynamic: !0, encoded: s };
  }
  return {
    dynamic: !1,
    encoded: Ke(o.map(({ encoded: s }) => s))
  };
}
function cm(e, { param: t }) {
  const [, n] = t.type.split("bytes"), r = le(e);
  if (!n) {
    let i = e;
    return r % 32 !== 0 && (i = Tt(i, {
      dir: "right",
      size: Math.ceil((e.length - 2) / 2 / 32) * 32
    })), {
      dynamic: !0,
      encoded: Ke([
        Tt(K(r, { size: 32 })),
        i
      ])
    };
  }
  if (r !== Number.parseInt(n, 10))
    throw new hb({
      expectedSize: Number.parseInt(n, 10),
      value: e
    });
  return { dynamic: !1, encoded: Tt(e, { dir: "right" }) };
}
function um(e) {
  if (typeof e != "boolean")
    throw new D(`Invalid boolean value: "${e}" (type: ${typeof e}). Expected: \`true\` or \`false\`.`);
  return { dynamic: !1, encoded: Tt(Pl(e)) };
}
function lm(e, { signed: t, size: n = 256 }) {
  if (typeof n == "number") {
    const r = 2n ** (BigInt(n) - (t ? 1n : 0n)) - 1n, i = t ? -r - 1n : 0n;
    if (e > r || e < i)
      throw new xs({
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
function dm(e) {
  const t = Bl(e), n = Math.ceil(le(t) / 32), r = [];
  for (let i = 0; i < n; i++)
    r.push(Tt(_s(t, i * 32, (i + 1) * 32), {
      dir: "right"
    }));
  return {
    dynamic: !0,
    encoded: Ke([
      Tt(K(le(t), { size: 32 })),
      ...r
    ])
  };
}
function fm(e, { param: t }) {
  let n = !1;
  const r = [];
  for (let i = 0; i < t.components.length; i++) {
    const o = t.components[i], s = Array.isArray(e) ? i : o.name, a = Ps({
      param: o,
      value: e[s]
    });
    r.push(a), a.dynamic && (n = !0);
  }
  return {
    dynamic: n,
    encoded: n ? Bs(r) : Ke(r.map(({ encoded: i }) => i))
  };
}
function vi(e) {
  const t = e.match(/^(.*)\[(\d+)?\]$/);
  return t ? (
    // Return `null` if the array is dynamic.
    [t[2] ? Number(t[2]) : null, t[1]]
  ) : void 0;
}
function go(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components.some(go);
  const n = vi(t);
  return n ? go({ ...e, type: n[1] }) : !1;
}
const pm = (e) => _s(em(e), 0, 4);
class ja extends D {
  constructor({ offset: t }) {
    super(`Offset \`${t}\` cannot be negative.`, {
      name: "NegativeOffsetError"
    });
  }
}
class hm extends D {
  constructor({ length: t, position: n }) {
    super(`Position \`${n}\` is out of bounds (\`0 < position < ${t}\`).`, { name: "PositionOutOfBoundsError" });
  }
}
class ym extends D {
  constructor({ count: t, limit: n }) {
    super(`Recursive read limit of \`${n}\` exceeded (recursive read count: \`${t}\`).`, { name: "RecursiveReadLimitExceededError" });
  }
}
const gm = {
  bytes: new Uint8Array(),
  dataView: new DataView(new ArrayBuffer(0)),
  position: 0,
  positionReadCount: /* @__PURE__ */ new Map(),
  recursiveReadCount: 0,
  recursiveReadLimit: Number.POSITIVE_INFINITY,
  assertReadLimit() {
    if (this.recursiveReadCount >= this.recursiveReadLimit)
      throw new ym({
        count: this.recursiveReadCount + 1,
        limit: this.recursiveReadLimit
      });
  },
  assertPosition(e) {
    if (e < 0 || e > this.bytes.length - 1)
      throw new hm({
        length: this.bytes.length,
        position: e
      });
  },
  decrementPosition(e) {
    if (e < 0)
      throw new ja({ offset: e });
    const t = this.position - e;
    this.assertPosition(t), this.position = t;
  },
  getReadCount(e) {
    return this.positionReadCount.get(e || this.position) || 0;
  },
  incrementPosition(e) {
    if (e < 0)
      throw new ja({ offset: e });
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
function $s(e, { recursiveReadLimit: t = 8192 } = {}) {
  const n = Object.create(gm);
  return n.bytes = e, n.dataView = new DataView(e.buffer ?? e, e.byteOffset, e.byteLength), n.positionReadCount = /* @__PURE__ */ new Map(), n.recursiveReadLimit = t, n;
}
function wm(e, t = {}) {
  typeof t.size < "u" && it(e, { size: t.size });
  const n = Se(e, t);
  return Il(n, t);
}
function bm(e, t = {}) {
  let n = e;
  if (typeof t.size < "u" && (it(n, { size: t.size }), n = Yn(n)), n.length > 1 || n[0] > 1)
    throw new xb(n);
  return !!n[0];
}
function ft(e, t = {}) {
  typeof t.size < "u" && it(e, { size: t.size });
  const n = Se(e, t);
  return Es(n, t);
}
function mm(e, t = {}) {
  let n = e;
  return typeof t.size < "u" && (it(n, { size: t.size }), n = Yn(n, { dir: "right" })), new TextDecoder().decode(n);
}
function vm(e, t) {
  const n = typeof t == "string" ? Lt(t) : t, r = $s(n);
  if (le(n) === 0 && e.length > 0)
    throw new fb();
  if (le(t) && le(t) < 32)
    throw new db({
      data: typeof t == "string" ? t : Se(t),
      params: e,
      size: le(t)
    });
  let i = 0;
  const o = [];
  for (let s = 0; s < e.length; ++s) {
    const a = e[s];
    i < n.length && r.setPosition(i);
    const [c, u] = un(r, a, {
      staticPosition: 0
    });
    i += u, o.push(c);
  }
  return o;
}
function un(e, t, { staticPosition: n }) {
  const r = vi(t.type);
  if (r) {
    const [i, o] = r;
    return Em(e, { ...t, type: o }, { length: i, staticPosition: n });
  }
  if (t.type === "tuple")
    return _m(e, t, { staticPosition: n });
  if (t.type === "address")
    return xm(e);
  if (t.type === "bool")
    return km(e);
  if (t.type.startsWith("bytes"))
    return Sm(e, t, { staticPosition: n });
  if (t.type.startsWith("uint") || t.type.startsWith("int"))
    return Am(e, t);
  if (t.type === "string")
    return Im(e, { staticPosition: n });
  throw new bb(t.type, {
    docsPath: "/docs/contract/decodeAbiParameters"
  });
}
const Va = 32, wo = 32;
function xm(e) {
  const t = e.readBytes(32);
  return [Ml(Se(Kl(t, -20))), 32];
}
function Em(e, t, { length: n, staticPosition: r }) {
  if (n === null) {
    const s = ft(e.readBytes(wo)), a = r + s, c = a + Va;
    e.setPosition(a);
    const u = ft(e.readBytes(Va)), l = tr(t);
    let d = 0;
    const h = [];
    for (let f = 0; f < u; ++f) {
      e.setPosition(c + (l ? f * 32 : d));
      const [b, y] = un(e, t, {
        staticPosition: c
      });
      d += y, h.push(b), y === 0 && (e.assertReadLimit(), e._touch());
    }
    return e.setPosition(r + 32), [h, 32];
  }
  if (tr(t)) {
    const s = ft(e.readBytes(wo)), a = r + s, c = [];
    for (let u = 0; u < n; ++u) {
      e.setPosition(a + u * 32);
      const [l] = un(e, t, {
        staticPosition: a
      });
      c.push(l);
    }
    return e.setPosition(r + 32), [c, 32];
  }
  let i = 0;
  const o = [];
  for (let s = 0; s < n; ++s) {
    const [a, c] = un(e, t, {
      staticPosition: r + i
    });
    i += c, o.push(a), c === 0 && (e.assertReadLimit(), e._touch());
  }
  return [o, i];
}
function km(e) {
  return [bm(e.readBytes(32), { size: 32 }), 32];
}
function Sm(e, t, { staticPosition: n }) {
  const [r, i] = t.type.split("bytes");
  if (!i) {
    const s = ft(e.readBytes(32));
    e.setPosition(n + s);
    const a = ft(e.readBytes(32));
    if (a === 0)
      return e.setPosition(n + 32), ["0x", 32];
    const c = e.readBytes(a);
    return e.setPosition(n + 32), [Se(c), 32];
  }
  return [Se(e.readBytes(Number.parseInt(i, 10), 32)), 32];
}
function Am(e, t) {
  const n = t.type.startsWith("int"), r = Number.parseInt(t.type.split("int")[1] || "256", 10), i = e.readBytes(32);
  return [
    r > 48 ? wm(i, { signed: n }) : ft(i, { signed: n }),
    32
  ];
}
function _m(e, t, { staticPosition: n }) {
  const r = t.components.length === 0 || t.components.some(({ name: s }) => !s), i = r ? [] : {};
  let o = 0;
  if (tr(t)) {
    const s = ft(e.readBytes(wo)), a = n + s;
    for (let c = 0; c < t.components.length; ++c) {
      const u = t.components[c];
      e.setPosition(a + o);
      const [l, d] = un(e, u, {
        staticPosition: a
      });
      o += d, i[r ? c : u?.name] = l;
    }
    return e.setPosition(n + 32), [i, 32];
  }
  for (let s = 0; s < t.components.length; ++s) {
    const a = t.components[s], [c, u] = un(e, a, {
      staticPosition: n
    });
    i[r ? s : a?.name] = c, o += u;
  }
  return [i, o];
}
function Im(e, { staticPosition: t }) {
  const n = ft(e.readBytes(32)), r = t + n;
  e.setPosition(r);
  const i = ft(e.readBytes(32));
  if (i === 0)
    return e.setPosition(t + 32), ["", 32];
  const o = e.readBytes(i, 32), s = mm(Yn(o));
  return e.setPosition(t + 32), [s, 32];
}
function tr(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components?.some(tr);
  const n = vi(e.type);
  return !!(n && tr({ ...e, type: n[1] }));
}
const Pm = (e, t, n) => JSON.stringify(e, (r, i) => typeof i == "bigint" ? i.toString() : i, n), Bm = {
  ether: -9,
  wei: 9
};
function $m(e, t) {
  let n = e.toString();
  const r = n.startsWith("-");
  r && (n = n.slice(1)), n = n.padStart(t, "0");
  let [i, o] = [
    n.slice(0, n.length - t),
    n.slice(n.length - t)
  ];
  return o = o.replace(/(0+)$/, ""), `${r ? "-" : ""}${i || "0"}${o ? `.${o}` : ""}`;
}
function bo(e, t = "wei") {
  return $m(e, Bm[t]);
}
function Tm(e) {
  const t = Object.entries(e).map(([r, i]) => i === void 0 || i === !1 ? null : [r, i]).filter(Boolean), n = t.reduce((r, [i]) => Math.max(r, i.length), 0);
  return t.map(([r, i]) => `  ${`${r}:`.padEnd(n + 1)}  ${i}`).join(`
`);
}
class Om extends D {
  constructor({ transaction: t }) {
    super("Cannot infer a transaction type from provided transaction.", {
      metaMessages: [
        "Provided Transaction:",
        "{",
        Tm(t),
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
class Lm extends D {
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
class Rm extends D {
  constructor({ storageKey: t }) {
    super(`Size for storage key "${t}" is invalid. Expected 32 bytes. Got ${Math.floor((t.length - 2) / 2)} bytes.`, { name: "InvalidStorageKeySizeError" });
  }
}
function bn(e, t = "hex") {
  const n = Vl(e), r = $s(new Uint8Array(n.length));
  return n.encode(r), t === "hex" ? Se(r.bytes) : r.bytes;
}
function Vl(e) {
  return Array.isArray(e) ? Cm(e.map((t) => Vl(t))) : zm(e);
}
function Cm(e) {
  const t = e.reduce((i, o) => i + o.length, 0), n = ql(t);
  return {
    length: t <= 55 ? 1 + t : 1 + n + t,
    encode(i) {
      t <= 55 ? i.pushByte(192 + t) : (i.pushByte(247 + n), n === 1 ? i.pushUint8(t) : n === 2 ? i.pushUint16(t) : n === 3 ? i.pushUint24(t) : i.pushUint32(t));
      for (const { encode: o } of e)
        o(i);
    }
  };
}
function zm(e) {
  const t = typeof e == "string" ? Lt(e) : e, n = ql(t.length);
  return {
    length: t.length === 1 && t[0] < 128 ? 1 : t.length <= 55 ? 1 + t.length : 1 + n + t.length,
    encode(i) {
      t.length === 1 && t[0] < 128 ? i.pushBytes(t) : t.length <= 55 ? (i.pushByte(128 + t.length), i.pushBytes(t)) : (i.pushByte(183 + n), n === 1 ? i.pushUint8(t.length) : n === 2 ? i.pushUint16(t.length) : n === 3 ? i.pushUint24(t.length) : i.pushUint32(t.length), i.pushBytes(t));
    }
  };
}
function ql(e) {
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
class xi extends D {
  constructor({ cause: t, maxFeePerGas: n } = {}) {
    super(`The fee cap (\`maxFeePerGas\`${n ? ` = ${bo(n)} gwei` : ""}) cannot be higher than the maximum allowed value (2^256-1).`, {
      cause: t,
      name: "FeeCapTooHighError"
    });
  }
}
Object.defineProperty(xi, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max fee per gas higher than 2\^256-1|fee cap higher than 2\^256-1/
});
class Gl extends D {
  constructor({ cause: t, maxPriorityFeePerGas: n, maxFeePerGas: r } = {}) {
    super([
      `The provided tip (\`maxPriorityFeePerGas\`${n ? ` = ${bo(n)} gwei` : ""}) cannot be higher than the fee cap (\`maxFeePerGas\`${r ? ` = ${bo(r)} gwei` : ""}).`
    ].join(`
`), {
      cause: t,
      name: "TipAboveFeeCapError"
    });
  }
}
Object.defineProperty(Gl, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max priority fee per gas higher than max fee per gas|tip higher than fee cap/
});
const Ts = 2n ** 256n - 1n;
function Wl(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((o) => Lt(o)) : e.blobs, i = [];
  for (const o of r)
    i.push(Uint8Array.from(t.blobToKzgCommitment(o)));
  return n === "bytes" ? i : i.map((o) => Se(o));
}
function Zl(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((s) => Lt(s)) : e.blobs, i = typeof e.commitments[0] == "string" ? e.commitments.map((s) => Lt(s)) : e.commitments, o = [];
  for (let s = 0; s < r.length; s++) {
    const a = r[s], c = i[s];
    o.push(Uint8Array.from(t.computeBlobKzgProof(a, c)));
  }
  return n === "bytes" ? o : o.map((s) => Se(s));
}
function Um(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, u = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + u, a, r);
}
function Nm(e, t, n) {
  return e & t ^ ~e & n;
}
function Mm(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
class Hm extends Ol {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Ki(this.buffer);
  }
  update(t) {
    ai(this), t = Ss(t), Qn(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Ki(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    ai(this), Tl(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, er(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    Um(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Ki(t), c = this.outputLen;
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
const At = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Fm = /* @__PURE__ */ Uint32Array.from([
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
class Km extends Hm {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = At[0] | 0, this.B = At[1] | 0, this.C = At[2] | 0, this.D = At[3] | 0, this.E = At[4] | 0, this.F = At[5] | 0, this.G = At[6] | 0, this.H = At[7] | 0;
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
      const h = _t[d - 15], f = _t[d - 2], b = We(h, 7) ^ We(h, 18) ^ h >>> 3, y = We(f, 17) ^ We(f, 19) ^ f >>> 10;
      _t[d] = y + _t[d - 7] + b + _t[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: u, H: l } = this;
    for (let d = 0; d < 64; d++) {
      const h = We(a, 6) ^ We(a, 11) ^ We(a, 25), f = l + h + Nm(a, c, u) + Fm[d] + _t[d] | 0, y = (We(r, 2) ^ We(r, 13) ^ We(r, 22)) + Mm(r, i, o) | 0;
      l = u, u = c, c = a, a = s + f | 0, s = o, o = i, i = r, r = f + y | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, u = u + this.G | 0, l = l + this.H | 0, this.set(r, i, o, s, a, c, u, l);
  }
  roundClean() {
    er(_t);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), er(this.buffer);
  }
}
const Dm = /* @__PURE__ */ Ll(() => new Km()), jm = Dm;
function Vm(e, t) {
  return jm(sr(e, { strict: !1 }) ? ks(e) : e);
}
function qm(e) {
  const { commitment: t, version: n = 1 } = e, r = e.to ?? (typeof t == "string" ? "hex" : "bytes"), i = Vm(t);
  return i.set([n], 0), r === "bytes" ? i : Se(i);
}
function Gm(e) {
  const { commitments: t, version: n } = e, r = e.to ?? (typeof t[0] == "string" ? "hex" : "bytes"), i = [];
  for (const o of t)
    i.push(qm({
      commitment: o,
      to: r,
      version: n
    }));
  return i;
}
const qa = 6, Xl = 32, Os = 4096, Yl = Xl * Os, Ga = Yl * qa - // terminator byte (0x80).
1 - // zero byte (0x00) appended to each field element.
1 * Os * qa, Jl = 1;
class Wm extends D {
  constructor({ maxSize: t, size: n }) {
    super("Blob size is too large.", {
      metaMessages: [`Max: ${t} bytes`, `Given: ${n} bytes`],
      name: "BlobSizeTooLargeError"
    });
  }
}
class Ql extends D {
  constructor() {
    super("Blob data must not be empty.", { name: "EmptyBlobError" });
  }
}
class Zm extends D {
  constructor({ hash: t, size: n }) {
    super(`Versioned hash "${t}" size is invalid.`, {
      metaMessages: ["Expected: 32", `Received: ${n}`],
      name: "InvalidVersionedHashSizeError"
    });
  }
}
class Xm extends D {
  constructor({ hash: t, version: n }) {
    super(`Versioned hash "${t}" version is invalid.`, {
      metaMessages: [
        `Expected: ${Jl}`,
        `Received: ${n}`
      ],
      name: "InvalidVersionedHashVersionError"
    });
  }
}
function Ym(e) {
  const t = e.to ?? (typeof e.data == "string" ? "hex" : "bytes"), n = typeof e.data == "string" ? Lt(e.data) : e.data, r = le(n);
  if (!r)
    throw new Ql();
  if (r > Ga)
    throw new Wm({
      maxSize: Ga,
      size: r
    });
  const i = [];
  let o = !0, s = 0;
  for (; o; ) {
    const a = $s(new Uint8Array(Yl));
    let c = 0;
    for (; c < Os; ) {
      const u = n.slice(s, s + (Xl - 1));
      if (a.pushByte(0), a.pushBytes(u), u.length < 31) {
        a.pushByte(128), o = !1;
        break;
      }
      c++, s += 31;
    }
    i.push(a);
  }
  return t === "bytes" ? i.map((a) => a.bytes) : i.map((a) => Se(a.bytes));
}
function Jm(e) {
  const { data: t, kzg: n, to: r } = e, i = e.blobs ?? Ym({ data: t, to: r }), o = e.commitments ?? Wl({ blobs: i, kzg: n, to: r }), s = e.proofs ?? Zl({ blobs: i, commitments: o, kzg: n, to: r }), a = [];
  for (let c = 0; c < i.length; c++)
    a.push({
      blob: i[c],
      commitment: o[c],
      proof: s[c]
    });
  return a;
}
function Qm(e) {
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
  throw new Om({ transaction: e });
}
class Ei extends D {
  constructor({ chainId: t }) {
    super(typeof t == "number" ? `Chain ID "${t}" is invalid.` : "Chain ID is invalid.", { name: "InvalidChainIdError" });
  }
}
function e0(e) {
  const { authorizationList: t } = e;
  if (t)
    for (const n of t) {
      const { chainId: r } = n, i = n.address;
      if (!Xt(i))
        throw new Zt({ address: i });
      if (r < 0)
        throw new Ei({ chainId: r });
    }
  Ls(e);
}
function t0(e) {
  const { blobVersionedHashes: t } = e;
  if (t) {
    if (t.length === 0)
      throw new Ql();
    for (const n of t) {
      const r = le(n), i = Es(_s(n, 0, 1));
      if (r !== 32)
        throw new Zm({ hash: n, size: r });
      if (i !== Jl)
        throw new Xm({
          hash: n,
          version: i
        });
    }
  }
  Ls(e);
}
function Ls(e) {
  const { chainId: t, maxPriorityFeePerGas: n, maxFeePerGas: r, to: i } = e;
  if (t <= 0)
    throw new Ei({ chainId: t });
  if (i && !Xt(i))
    throw new Zt({ address: i });
  if (r && r > Ts)
    throw new xi({ maxFeePerGas: r });
  if (n && r && n > r)
    throw new Gl({ maxFeePerGas: r, maxPriorityFeePerGas: n });
}
function n0(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (t <= 0)
    throw new Ei({ chainId: t });
  if (o && !Xt(o))
    throw new Zt({ address: o });
  if (n || i)
    throw new D("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid EIP-2930 Transaction attribute.");
  if (r && r > Ts)
    throw new xi({ maxFeePerGas: r });
}
function r0(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (o && !Xt(o))
    throw new Zt({ address: o });
  if (typeof t < "u" && t <= 0)
    throw new Ei({ chainId: t });
  if (n || i)
    throw new D("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid Legacy Transaction attribute.");
  if (r && r > Ts)
    throw new xi({ maxFeePerGas: r });
}
function ki(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (let n = 0; n < e.length; n++) {
    const { address: r, storageKeys: i } = e[n];
    for (let o = 0; o < i.length; o++)
      if (i[o].length - 2 !== 64)
        throw new Rm({ storageKey: i[o] });
    if (!Xt(r, { strict: !1 }))
      throw new Zt({ address: r });
    t.push([r, i]);
  }
  return t;
}
function i0(e, t) {
  const n = Qm(e);
  return n === "eip1559" ? a0(e, t) : n === "eip2930" ? c0(e, t) : n === "eip4844" ? s0(e, t) : n === "eip7702" ? o0(e, t) : u0(e);
}
function o0(e, t) {
  const { authorizationList: n, chainId: r, gas: i, nonce: o, to: s, value: a, maxFeePerGas: c, maxPriorityFeePerGas: u, accessList: l, data: d } = e;
  e0(e);
  const h = ki(l), f = l0(n);
  return Ke([
    "0x04",
    bn([
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
      ...ar(e, t)
    ])
  ]);
}
function s0(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerBlobGas: a, maxFeePerGas: c, maxPriorityFeePerGas: u, accessList: l, data: d } = e;
  t0(e);
  let h = e.blobVersionedHashes, f = e.sidecars;
  if (e.blobs && (typeof h > "u" || typeof f > "u")) {
    const A = typeof e.blobs[0] == "string" ? e.blobs : e.blobs.map((O) => Se(O)), I = e.kzg, _ = Wl({
      blobs: A,
      kzg: I
    });
    if (typeof h > "u" && (h = Gm({
      commitments: _
    })), typeof f > "u") {
      const O = Zl({ blobs: A, commitments: _, kzg: I });
      f = Jm({ blobs: A, commitments: _, proofs: O });
    }
  }
  const b = ki(l), y = [
    K(n),
    i ? K(i) : "0x",
    u ? K(u) : "0x",
    c ? K(c) : "0x",
    r ? K(r) : "0x",
    o ?? "0x",
    s ? K(s) : "0x",
    d ?? "0x",
    b,
    a ? K(a) : "0x",
    h ?? [],
    ...ar(e, t)
  ], p = [], g = [], v = [];
  if (f)
    for (let A = 0; A < f.length; A++) {
      const { blob: I, commitment: _, proof: O } = f[A];
      p.push(I), g.push(_), v.push(O);
    }
  return Ke([
    "0x03",
    // If sidecars are enabled, envelope turns into a "wrapper":
    bn(f ? [y, p, g, v] : y)
  ]);
}
function a0(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerGas: a, maxPriorityFeePerGas: c, accessList: u, data: l } = e;
  Ls(e);
  const d = ki(u), h = [
    K(n),
    i ? K(i) : "0x",
    c ? K(c) : "0x",
    a ? K(a) : "0x",
    r ? K(r) : "0x",
    o ?? "0x",
    s ? K(s) : "0x",
    l ?? "0x",
    d,
    ...ar(e, t)
  ];
  return Ke([
    "0x02",
    bn(h)
  ]);
}
function c0(e, t) {
  const { chainId: n, gas: r, data: i, nonce: o, to: s, value: a, accessList: c, gasPrice: u } = e;
  n0(e);
  const l = ki(c), d = [
    K(n),
    o ? K(o) : "0x",
    u ? K(u) : "0x",
    r ? K(r) : "0x",
    s ?? "0x",
    a ? K(a) : "0x",
    i ?? "0x",
    l,
    ...ar(e, t)
  ];
  return Ke([
    "0x01",
    bn(d)
  ]);
}
function u0(e, t) {
  const { chainId: n = 0, gas: r, data: i, nonce: o, to: s, value: a, gasPrice: c } = e;
  r0(e);
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
  ]), bn(u);
}
function ar(e, t) {
  const n = t ?? e, { v: r, yParity: i } = n;
  if (typeof n.r > "u")
    return [];
  if (typeof n.s > "u")
    return [];
  if (typeof r > "u" && typeof i > "u")
    return [];
  const o = Yn(n.r), s = Yn(n.s);
  return [typeof i == "number" ? i ? K(1) : "0x" : r === 0n ? "0x" : r === 1n ? K(1) : r === 27n ? "0x" : K(1), o === "0x00" ? "0x" : o, s === "0x00" ? "0x" : s];
}
function l0(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (const n of e) {
    const { chainId: r, nonce: i, ...o } = n, s = n.address;
    t.push([
      r ? Jn(r) : "0x",
      s,
      i ? Jn(i) : "0x",
      ...ar({}, o)
    ]);
  }
  return t;
}
class d0 extends D {
  constructor({ domain: t }) {
    super(`Invalid domain "${Pm(t)}".`, {
      metaMessages: ["Must be a valid EIP-712 domain."]
    });
  }
}
class f0 extends D {
  constructor({ primaryType: t, types: n }) {
    super(`Invalid primary type \`${t}\` must be one of \`${JSON.stringify(Object.keys(n))}\`.`, {
      docsPath: "/api/glossary/Errors#typeddatainvalidprimarytypeerror",
      metaMessages: ["Check that the primary type is a key in `types`."]
    });
  }
}
class p0 extends D {
  constructor({ type: t }) {
    super(`Struct type "${t}" is invalid.`, {
      metaMessages: ["Struct type must not be a Solidity type."],
      name: "InvalidStructTypeError"
    });
  }
}
function h0(e) {
  const { domain: t, message: n, primaryType: r, types: i } = e, o = (s, a) => {
    for (const c of s) {
      const { name: u, type: l } = c, d = a[u], h = l.match(jl);
      if (h && (typeof d == "number" || typeof d == "bigint")) {
        const [y, p, g] = h;
        K(d, {
          signed: p === "int",
          size: Number.parseInt(g, 10) / 8
        });
      }
      if (l === "address" && typeof d == "string" && !Xt(d))
        throw new Zt({ address: d });
      const f = l.match(im);
      if (f) {
        const [y, p] = f;
        if (p && le(d) !== Number.parseInt(p, 10))
          throw new gb({
            expectedSize: Number.parseInt(p, 10),
            givenSize: le(d)
          });
      }
      const b = i[l];
      b && (g0(l), o(b, d));
    }
  };
  if (i.EIP712Domain && t) {
    if (typeof t != "object")
      throw new d0({ domain: t });
    o(i.EIP712Domain, t);
  }
  if (r !== "EIP712Domain")
    if (i[r])
      o(i[r], n);
    else
      throw new f0({ primaryType: r, types: i });
}
function y0({ domain: e }) {
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
function g0(e) {
  if (e === "address" || e === "bool" || e === "string" || e.startsWith("bytes") || e.startsWith("uint") || e.startsWith("int"))
    throw new p0({ type: e });
}
function w0(e) {
  const { domain: t = {}, message: n, primaryType: r } = e, i = {
    EIP712Domain: y0({ domain: t }),
    ...e.types
  };
  h0({
    domain: t,
    message: n,
    primaryType: r,
    types: i
  });
  const o = ["0x1901"];
  return t && o.push(b0({
    domain: t,
    types: i
  })), r !== "EIP712Domain" && o.push(ed({
    data: n,
    primaryType: r,
    types: i
  })), Je(nm(o));
}
function b0({ domain: e, types: t }) {
  return ed({
    data: e,
    primaryType: "EIP712Domain",
    types: t
  });
}
function ed({ data: e, primaryType: t, types: n }) {
  const r = td({
    data: e,
    primaryType: t,
    types: n
  });
  return Je(r);
}
function td({ data: e, primaryType: t, types: n }) {
  const r = [{ type: "bytes32" }], i = [m0({ primaryType: t, types: n })];
  for (const o of n[t]) {
    const [s, a] = rd({
      types: n,
      name: o.name,
      type: o.type,
      value: e[o.name]
    });
    r.push(s), i.push(a);
  }
  return Is(r, i);
}
function m0({ primaryType: e, types: t }) {
  const n = Jn(v0({ primaryType: e, types: t }));
  return Je(n);
}
function v0({ primaryType: e, types: t }) {
  let n = "";
  const r = nd({ primaryType: e, types: t });
  r.delete(e);
  const i = [e, ...Array.from(r).sort()];
  for (const o of i)
    n += `${o}(${t[o].map(({ name: s, type: a }) => `${a} ${s}`).join(",")})`;
  return n;
}
function nd({ primaryType: e, types: t }, n = /* @__PURE__ */ new Set()) {
  const i = e.match(/^\w*/u)?.[0];
  if (n.has(i) || t[i] === void 0)
    return n;
  n.add(i);
  for (const o of t[i])
    nd({ primaryType: o.type, types: t }, n);
  return n;
}
function rd({ types: e, name: t, type: n, value: r }) {
  if (e[n] !== void 0)
    return [
      { type: "bytes32" },
      Je(td({ data: r, primaryType: n, types: e }))
    ];
  if (n === "bytes")
    return [{ type: "bytes32" }, Je(r)];
  if (n === "string")
    return [{ type: "bytes32" }, Je(Jn(r))];
  if (n.lastIndexOf("]") === n.length - 1) {
    const i = n.slice(0, n.lastIndexOf("[")), o = r.map((s) => rd({
      name: t,
      type: i,
      types: e,
      value: s
    }));
    return [
      { type: "bytes32" },
      Je(Is(o.map(([s]) => s), o.map(([, s]) => s)))
    ];
  }
  return [{ type: n }, r];
}
function x0(e) {
  const t = Dl(e, 0, 1);
  if (t === "0x04")
    return "eip7702";
  if (t === "0x03")
    return "eip4844";
  if (t === "0x02")
    return "eip1559";
  if (t === "0x01")
    return "eip2930";
  if (t !== "0x" && Es(t) >= 192)
    return "legacy";
  throw new Lm({ serializedType: t });
}
const Wa = "bitcoin-taproot-keypath-v1", Za = "bip341-sighash-default-keypath", E0 = ["bitcoin", "testnet", "signet", "regtest"], mo = 21000000n * 100000000n, id = 137, od = /^[0-9a-f]+$/, k0 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", S0 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", A0 = 734539939, _0 = 1;
function V(e) {
  throw new m("invalid_payload", e);
}
function Xa(e, t) {
  return (!e || typeof e != "object" || Array.isArray(e)) && V(`${t} must be an object`), e;
}
function Ya(e, t, n) {
  const r = Object.keys(e).filter((o) => !t.includes(o));
  r.length > 0 && V(`${n} carries unsupported fields: ${r.sort().join(", ")}`);
  const i = t.filter((o) => !(o in e));
  i.length > 0 && V(`${n} is missing required fields: ${i.join(", ")}`);
}
function Rr(e, t, n) {
  return (typeof e != "string" || n !== void 0 && e.length !== n || e.length === 0 || !od.test(e)) && V(`${t} must be lowercase hexadecimal${n === void 0 ? "" : ` of length ${n}`}`), e;
}
function I0(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < n || e > r) && V(`${t} must be an integer in ${n}..${r}`), e;
}
function Ne(...e) {
  const t = new Uint8Array(e.reduce((r, i) => r + i.length, 0));
  let n = 0;
  for (const r of e)
    t.set(r, n), n += r.length;
  return t;
}
function nn(e, t, n, r) {
  return t + n > e.length && V(`transaction is truncated while reading ${r}`), [e.slice(t, t + n), t + n];
}
function kr(e, t, n) {
  t >= e.length && V(`transaction is truncated before ${n}`);
  const r = e[t];
  return r >= 253 && V(`${n} must use a single-byte CompactSize`), [r, t + 1];
}
function Vi(e) {
  return ((e[0] ?? 0) | (e[1] ?? 0) << 8 | (e[2] ?? 0) << 16 | (e[3] ?? 0) << 24) >>> 0;
}
function P0(e) {
  let t = 0n;
  for (let n = 0; n < 8; n += 1) t |= BigInt(e[n] ?? 0) << BigInt(n * 8);
  return t;
}
function Tn(e) {
  return new Uint8Array([e & 255, e >>> 8 & 255, e >>> 16 & 255, e >>> 24 & 255]);
}
function Ja(e) {
  const t = new Uint8Array(8);
  let n = e;
  for (let r = 0; r < 8; r += 1)
    t[r] = Number(n & 0xffn), n >>= 8n;
  return t;
}
function sd(e) {
  if (e.length === 25 && e[0] === 118 && e[1] === 169 && e[2] === 20 && e[23] === 136 && e[24] === 172) return "p2pkh";
  if (e.length === 23 && e[0] === 169 && e[1] === 20 && e[22] === 135) return "p2sh";
  if (e.length === 22 && e[0] === 0 && e[1] === 20) return "p2wpkh";
  if (e.length === 34 && e[0] === 0 && e[1] === 32) return "p2wsh";
  if (e.length === 34 && e[0] === 81 && e[1] === 32) return "p2tr";
  V("transaction output uses an unsupported script template");
}
function B0(e) {
  (e.length === 0 || e.length % 2 || e.length > id * 2 || !od.test(e)) && V("unsignedTransaction must be lowercase hex, without 0x or whitespace, and at most 137 bytes");
  const t = Uint8Array.from(e.match(/../g), (C) => Number.parseInt(C, 16));
  (t.length < 4 || t[0] !== 2 || t[1] !== 0 || t[2] !== 0 || t[3] !== 0) && V("transaction version must be exactly 2");
  let n = 4;
  const [r, i] = kr(t, n, "input count");
  n = i, r !== 1 && V("transaction must contain exactly one input");
  const [o, s] = nn(t, n, 32, "outpoint hash");
  n = s, o.every((C) => C === 0) && V("outpoint hash must not be null");
  const [a, c] = nn(t, n, 4, "outpoint index");
  n = c;
  const u = Vi(a), [l, d] = kr(t, n, "scriptSig length");
  n = d, l !== 0 && V("scriptSig must be empty");
  const [h, f] = nn(t, n, 4, "sequence");
  n = f;
  const b = Vi(h);
  b !== 4294967293 && b !== 4294967294 && b !== 4294967295 && V("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff");
  const [y, p] = kr(t, n, "output count");
  n = p, y !== 1 && y !== 2 && V("transaction must contain one or two outputs");
  const g = [], v = { p2pkh: 546n, p2sh: 540n, p2wpkh: 294n, p2wsh: 330n, p2tr: 330n };
  for (let C = 0; C < y; C += 1) {
    const [L, B] = nn(t, n, 8, `output ${C} amount`);
    n = B;
    const U = P0(L);
    (U < 1n || U > mo) && V(`output ${C} amount is outside the Bitcoin money range`);
    const [N, w] = kr(t, n, `output ${C} script length`);
    n = w;
    const [E, k] = nn(t, n, N, `output ${C} script`);
    n = k;
    const x = sd(E);
    N !== { p2pkh: 25, p2sh: 23, p2wpkh: 22, p2wsh: 34, p2tr: 34 }[x] && V(`output ${C} script length is not canonical for ${x}`), U < v[x] && V(`output ${C} is below the V1 ${x} dust floor`), g.push({ amountSats: U, scriptPubKey: E, kind: x });
  }
  const [A, I] = nn(t, n, 4, "locktime");
  n = I;
  const _ = Vi(A);
  _ !== 0 && V("locktime must be exactly zero"), n !== t.length && V("transaction has trailing bytes");
  const O = g.reduce((C, L) => C + L.amountSats, 0n);
  return (O < 1n || O > mo) && V("output total is outside the Bitcoin money range"), { raw: t, prevoutHashLe: o, prevoutIndex: u, sequence: b, outputs: g, locktime: _ };
}
function $0(e, t) {
  const n = ue(new TextEncoder().encode(e));
  return ue(Ne(n, n, t));
}
function T0(e, t, n) {
  const r = Ne(e.prevoutHashLe, Tn(e.prevoutIndex)), i = Ne(new Uint8Array([n.length]), n), o = e.outputs.map((a) => Ne(Ja(a.amountSats), new Uint8Array([a.scriptPubKey.length]), a.scriptPubKey)), s = Ne(
    new Uint8Array([0, 0]),
    Tn(2),
    Tn(e.locktime),
    ue(r),
    ue(Ja(t)),
    ue(i),
    ue(Tn(e.sequence)),
    ue(Ne(...o)),
    new Uint8Array([0]),
    Tn(0)
  );
  return $0("TapSighash", s);
}
function O0(e) {
  return BigInt(`0x${X(e)}`);
}
function Qa(e) {
  return pu(ue(e));
}
function ec(e, t) {
  const n = Ne(new Uint8Array([e]), t), r = ue(ue(n)).slice(0, 4), i = Ne(n, r);
  let o = O0(i), s = "";
  for (; o > 0n; ) {
    const a = Number(o % 58n);
    s = k0[a] + s, o /= 58n;
  }
  for (const a of i) {
    if (a !== 0) break;
    s = `1${s}`;
  }
  return s;
}
function L0(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >> i & s);
  return i > 0 && o.push(r << n - i & s), o;
}
function R0(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1) i >>> o & 1 && (n ^= t[o]);
  }
  return n >>> 0;
}
function qi(e, t, n) {
  const r = e === "bitcoin" ? "bc" : e === "regtest" ? "bcrt" : "tb", i = [t, ...L0(n, 8, 5)], o = [...r].map((u) => u.charCodeAt(0) >> 5).concat([0], [...r].map((u) => u.charCodeAt(0) & 31)), s = t === 0 ? _0 : A0, a = R0(o.concat(i, [0, 0, 0, 0, 0, 0])) ^ s, c = Array.from({ length: 6 }, (u, l) => a >>> 5 * (5 - l) & 31);
  return `${r}1${i.concat(c).map((u) => S0[u]).join("")}`;
}
function C0(e, t) {
  const n = sd(t), r = e === "bitcoin";
  return n === "p2pkh" ? ec(r ? 0 : 111, t.slice(3, 23)) : n === "p2sh" ? ec(r ? 5 : 196, t.slice(2, 22)) : n === "p2wpkh" || n === "p2wsh" ? qi(e, 0, t.slice(2)) : qi(e, 1, t.slice(2));
}
function z0(e) {
  const t = Qa(e), n = Ne(new Uint8Array([0, 20]), t);
  return [
    Ne(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    n,
    Ne(new Uint8Array([169, 20]), Qa(n), new Uint8Array([135]))
  ];
}
function Gi(e, t) {
  return e.length === t.length && e.every((n, r) => n === t[r]);
}
function Sr(e) {
  const t = e / 100000000n, n = e % 100000000n;
  return n === 0n ? `${t}.00000000` : `${t}.${n.toString().padStart(8, "0")}`;
}
function U0(e, t) {
  const n = e / BigInt(t), r = e % BigInt(t);
  if (r === 0n) return `${n.toString()} sat/vB`;
  const i = (r * 10000n / BigInt(t)).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n.toString()}.${i} sat/vB`;
}
function N0(e) {
  return X(ue(ue(e.raw)).slice().reverse());
}
function M0(e, t) {
  const n = t.transaction, r = F("bitcoin.network", { network: e.network }), i = [
    F("bitcoin.title"),
    ...t.highFeeWarning ? [F("bitcoin.highFeeWarning"), ""] : [],
    r,
    F("bitcoin.networkWarning"),
    F("bitcoin.input", { txid: X(n.prevoutHashLe.slice().reverse()), vout: n.prevoutIndex }),
    F("bitcoin.inputAmount", { sats: e.prevout.amountSats, btc: Sr(BigInt(e.prevout.amountSats)) }),
    F("bitcoin.outputs")
  ];
  return n.outputs.forEach((o, s) => {
    const a = t.sameKeyOutputIndexes.includes(s);
    i.push(F("bitcoin.output", {
      index: s + 1,
      address: C0(e.network, o.scriptPubKey),
      sats: o.amountSats.toString(),
      btc: Sr(o.amountSats),
      role: F(a ? "bitcoin.sameKeyReturn" : "bitcoin.recipient")
    }));
  }), i.push(
    F("bitcoin.fee", { sats: t.feeSats.toString(), btc: Sr(t.feeSats), rate: U0(t.feeSats, t.signedVsize) }),
    F("bitcoin.vsize", { vsize: t.signedVsize }),
    F(n.sequence === 4294967293 ? "bitcoin.rbfSignals" : "bitcoin.rbfDoesNotSignal"),
    F("bitcoin.netLeaving", { sats: t.netLeavingSats.toString(), btc: Sr(t.netLeavingSats) }),
    F("bitcoin.unsignedTxid", { txid: N0(n) }),
    F("bitcoin.outputKey", { key: X(t.outputKeyXOnly) })
  ), i.join(`
`);
}
function H0(e, t) {
  const n = Xa(e, "signingContext");
  Ya(n, ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"], "bitcoin signingContext"), n.kind !== Wa && V("unsupported Bitcoin signing_context kind"), (typeof n.network != "string" || !E0.includes(n.network)) && V("bitcoin signing_context network is invalid");
  const r = Rr(n.unsignedTransaction, "unsignedTransaction");
  r.length > id * 2 && V("unsignedTransaction exceeds the 137-byte V1 limit");
  const i = Xa(n.prevout, "signingContext.prevout");
  Ya(i, ["amountSats", "scriptPubKey"], "signingContext.prevout");
  const o = I0(i.amountSats, "prevout.amountSats", 1, Number(mo)), s = Rr(i.scriptPubKey, "prevout.scriptPubKey", 68);
  s.startsWith("5120") || V("prevout.scriptPubKey must be a lowercase P2TR output key script"), n.digestAlgorithm !== Za && V("unsupported Bitcoin digest algorithm");
  const a = Rr(n.digest, "signingContext.digest", 64);
  return a !== t && V("Bitcoin signing_context digest must match the sign request digest exactly"), {
    kind: Wa,
    network: n.network,
    unsignedTransaction: r,
    prevout: { amountSats: o, scriptPubKey: s },
    digestAlgorithm: Za,
    digest: a
  };
}
function ad(e, t, n) {
  const r = H0(e, t), i = B0(r.unsignedTransaction), o = Uint8Array.from(r.prevout.scriptPubKey.match(/../g), (y) => Number.parseInt(y, 16)), s = o.slice(2);
  let a;
  if (n !== void 0) {
    const y = Rr(n, "signing public key", 66);
    a = Uint8Array.from(y.match(/../g), (g) => Number.parseInt(g, 16)), a[0] !== 2 && a[0] !== 3 && V("signing public key must be compressed secp256k1");
    const p = ig(y);
    Gi(s, p) || V("prevout.scriptPubKey does not bind to the signing key's BIP86 output key");
  }
  if (a)
    for (const y of i.outputs)
      z0(a).some((p) => Gi(p, y.scriptPubKey)) && V("transaction sends to a same-key legacy or SegWit-v0 address this workflow cannot spend");
  const c = i.outputs.flatMap((y, p) => Gi(y.scriptPubKey, o) ? [p] : []);
  c.length > 1 && V("transaction may contain at most one same-key P2TR return output");
  const u = i.outputs.reduce((y, p) => y + p.amountSats, 0n), l = BigInt(r.prevout.amountSats) - u;
  l <= 0n && V("transaction fee must be positive");
  const d = i.raw.length * 4 + 68, h = Math.ceil(d / 4);
  l > 10000n * BigInt(h) && V("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling");
  const f = X(T0(i, BigInt(r.prevout.amountSats), o));
  f !== r.digest && V("Bitcoin signing_context digest does not match the independently computed BIP341 digest");
  const b = c.reduce((y, p) => y + i.outputs[p].amountSats, 0n);
  return {
    digest: f,
    display: M0(r, {
      transaction: i,
      outputKeyXOnly: s,
      feeSats: l,
      signedVsize: h,
      sameKeyOutputIndexes: c,
      highFeeWarning: l > 100n * BigInt(h) || l * 100n > BigInt(r.prevout.amountSats) * 2n,
      netLeavingSats: BigInt(r.prevout.amountSats) - b
    }),
    transaction: i,
    outputKeyXOnly: s
  };
}
const tc = `avault-agent-operation:v1
`;
function rn(e) {
  if (typeof e != "object" || e === null || Array.isArray(e)) throw new Error("signing context is malformed");
  return e;
}
function nc(e) {
  return e.startsWith("0x") ? e.toLowerCase() : `0x${e.toLowerCase()}`;
}
function Wi(e, t) {
  const n = nc(e), r = nc(t);
  if (n !== r)
    throw new Error("signing digest does not match verified context");
  return r.slice(2);
}
function Cr(e) {
  return typeof e == "bigint" ? e.toString() : typeof e == "string" || typeof e == "number" || typeof e == "boolean" ? String(e) : e == null ? "" : ln(e);
}
function ln(e) {
  if (typeof e == "bigint") return JSON.stringify(e.toString());
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(ln).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${ln(t[n])}`).join(",")}}`;
}
const F0 = ub([
  "function approve(address spender,uint256 amount)",
  "function transfer(address to,uint256 amount)",
  "function transferFrom(address from,address to,uint256 amount)",
  "function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
  "function setApprovalForAll(address operator,bool approved)",
  "function safeTransferFrom(address from,address to,uint256 tokenId)",
  "function safeTransferFrom(address from,address to,uint256 tokenId,bytes data)"
]), K0 = {
  approve: "ERC-20/ERC-721 approve",
  transfer: "ERC-20 transfer",
  transferFrom: "ERC-20/ERC-721 transferFrom",
  permit: "ERC-20 permit",
  setApprovalForAll: "ERC-721 setApprovalForAll",
  safeTransferFrom: "ERC-721 safeTransferFrom",
  safeTransferFromWithData: "ERC-721 safeTransferFrom"
}, D0 = new Map(
  F0.map((e) => {
    const t = e.name === "safeTransferFrom" && e.inputs.length === 4 ? "safeTransferFromWithData" : e.name;
    return [pm(e), { call: t, item: e }];
  })
);
function j0(e) {
  return `${e.name}(${e.inputs.map((t) => t.type).join(",")})`;
}
function V0(e) {
  if (e == null || e === "" || e === "0x") return "0x";
  if (typeof e != "string" || !/^0x[0-9a-fA-F]*$/.test(e) || e.length % 2 !== 0)
    throw new Error("EVM transaction calldata is malformed");
  return e.toLowerCase();
}
const q0 = /* @__PURE__ */ new Set([
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
]), G0 = /* @__PURE__ */ new Set(["domain", "types", "primaryType", "message"]);
function Zi(e, t, n) {
  const r = Object.keys(e).filter((i) => !t.has(i));
  if (r.length > 0)
    throw new Error(`${n} carries unsupported field: ${r.sort().join(", ")}`);
}
function cd(e) {
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
function W0(e, t) {
  if (t.chainId === void 0 || t.chainId === null || t.chainId === "")
    throw new Error("EVM transaction chain id is required");
  if (String(t.chainId) !== String(e.chainId))
    throw new Error("EVM transaction chain id mismatch");
}
function Z0(e) {
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
function X0(e) {
  if (e.length < 10) return null;
  const t = D0.get(e.slice(0, 10));
  if (!t) return null;
  let n;
  try {
    if (n = vm(t.item.inputs, `0x${e.slice(10)}`), Is(t.item.inputs, n).toLowerCase() !== `0x${e.slice(10)}`) return null;
  } catch {
    return null;
  }
  return {
    call: t.call,
    signature: j0(t.item),
    arguments: t.item.inputs.map((r, i) => ({
      name: r.name ?? `arg${i}`,
      type: r.type,
      value: Cr(n[i])
    }))
  };
}
function Y0(e, t, n) {
  const r = cd(e), i = V0(e.data), o = (c) => (t[c] === void 0 ? 0n : t[c]).toString(), s = e.to === void 0 || e.to === null || e.to === "" ? null : String(e.to), a = s === null ? null : X0(i);
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
function J0(e) {
  const t = [F("evm.title", { chainId: e.chainId })];
  if (e.blindSigning && t.push(F("sign.blindNotice")), t.push(
    F("evm.type", { type: e.type }),
    e.to === null ? F("evm.contractCreation") : F("evm.to", { to: e.to }),
    F("evm.value", { value: e.value }),
    F("evm.nonce", { value: e.nonce }),
    F("evm.gas", { value: e.gas })
  ), e.gasPrice !== void 0 && t.push(F("evm.gasPrice", { value: e.gasPrice })), e.maxFeePerGas !== void 0 && t.push(F("evm.maxFeePerGas", { value: e.maxFeePerGas })), e.maxPriorityFeePerGas !== void 0 && t.push(F("evm.maxPriorityFeePerGas", { value: e.maxPriorityFeePerGas })), e.decoded) {
    t.push(F("evm.call", { call: `${K0[e.decoded.call]} ${e.decoded.signature}` }));
    for (const n of e.decoded.arguments) t.push(`  ${n.name} (${n.type}): ${n.value}`);
  }
  return t.push(
    e.selector === null ? F("evm.selectorNone") : F("evm.selector", { selector: e.selector }),
    e.calldata === "0x" ? F("evm.calldataNone") : F("evm.calldata", { bytes: e.calldataBytes, calldata: e.calldata })
  ), t.join(`
`);
}
function Q0(e) {
  hs(e);
  const t = rn(e), n = {
    "evm-transaction": ["kind", "chainId", "unsignedTransaction", "digestAlgorithm", "digest"],
    "eip-712-typed-data": ["kind", "typedData", "digestAlgorithm", "digest"],
    "avault-agent-operation": ["kind", "canonicalPreimage", "digestAlgorithm", "digest"],
    "bitcoin-taproot-keypath-v1": ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"]
  }, r = Object.hasOwn(n, e.kind) ? n[e.kind] : void 0;
  if (!r) throw new Error("unsupported signing context");
  if (Zi(t, new Set(r), "signing context"), typeof e.digest != "string" || !/^(?:0x)?[0-9a-fA-F]{64}$/.test(e.digest))
    throw new Error("signing context digest must be 32 bytes");
  switch (e.kind) {
    case "evm-transaction": {
      if (e.digestAlgorithm !== "keccak256") throw new Error("unsupported EVM digest algorithm");
      const i = rn(e.unsignedTransaction);
      Zi(i, q0, "EVM transaction"), W0(e, i), cd(i);
      const o = Z0(i), s = i0(o), a = Wi(e.digest, Je(s)), c = Y0(i, o, s);
      if (x0(s) !== c.type) throw new Error("EVM transaction type does not match its serialization");
      return {
        digest: a,
        challenge: Oe(a),
        display: J0(c),
        evm: c
      };
    }
    case "eip-712-typed-data": {
      if (e.digestAlgorithm !== "eip712") throw new Error("unsupported EIP-712 digest algorithm");
      const i = rn(e.typedData);
      Zi(i, G0, "EIP-712 typed data");
      const o = rn(i.domain);
      if (o.chainId === void 0 || o.verifyingContract === void 0)
        throw new Error("EIP-712 domain must include chainId and verifyingContract");
      const s = Wi(e.digest, w0(i));
      return {
        digest: s,
        challenge: Oe(s),
        display: [
          "EIP-712 typed data",
          `domain.chainId: ${Cr(o.chainId)}`,
          `domain.verifyingContract: ${Cr(o.verifyingContract)}`,
          `domain: ${ln(o)}`,
          `types: ${ln(rn(i.types))}`,
          `primaryType: ${Cr(i.primaryType)}`,
          `message: ${ln(rn(i.message))}`
        ].join(`
`)
      };
    }
    case "avault-agent-operation": {
      if (e.digestAlgorithm !== "avault-operation-hash-v1") throw new Error("unsupported avault digest algorithm");
      if (typeof e.canonicalPreimage != "string" || !e.canonicalPreimage.startsWith(tc) || e.canonicalPreimage.slice(tc.length).trim().length === 0)
        throw new Error("canonical preimage must start with avault-agent-operation:v1\\n followed by operation text; recreate the request");
      const i = Wi(e.digest, py(ue(new TextEncoder().encode(e.canonicalPreimage))));
      return {
        digest: i,
        challenge: Oe(i),
        display: ["Avault agent operation", e.canonicalPreimage].join(`
`)
      };
    }
    case "bitcoin-taproot-keypath-v1": {
      const i = ad(e, e.digest);
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
function zr(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) return;
  const t = e.messageHex;
  if (typeof t == "string")
    try {
      Eu(t);
    } catch {
      throw new m("invalid_payload", "signing messageHex exceeds the 1 MiB decoded limit");
    }
}
function Si(e) {
  if (zr(e), hs(e), !e || typeof e != "object" || Array.isArray(e)) throw new m("invalid_payload", "Solana signing context must be an object");
  const t = e;
  if (t.kind !== "solana-transaction-v1" || Object.keys(t).some((n) => n !== "kind" && n !== "messageHex") || typeof t.messageHex != "string" || t.messageHex.length === 0 || t.messageHex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(t.messageHex))
    throw new m("invalid_payload", "Solana context requires only kind and nonempty lowercase even messageHex; no digest alias");
  return { kind: "solana-transaction-v1", messageHex: t.messageHex };
}
function ev(e, t, n) {
  if (n.kind !== "keypair" || !n.publicKey) throw new Error("protected signing requires a keypair protected record");
  const r = vn(n.curve);
  if (e === qn || t.kind === "solana-transaction-v1" || r === "ed25519") {
    if (e !== qn || t.kind !== "solana-transaction-v1" || r !== "ed25519")
      throw new m("invalid_payload", "Ed25519 signing requires an Ed25519 record, ed25519 scheme and Solana context together");
    Si(t);
  }
}
const ud = "11111111111111111111111111111111", ld = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", tv = 2, nv = 3, rv = 12;
function nr(e) {
  return { decoded: null, undecodedReason: e };
}
function dd(e, t) {
  let n = 0n;
  for (let r = 7; r >= 0; r -= 1) n = n << 8n | BigInt(e[t + r]);
  return n.toString();
}
function fd(e) {
  const t = [];
  for (const n of e) {
    if (n.source !== "static") return null;
    t.push(n.address);
  }
  return t;
}
function iv(e, t) {
  if ((t.length === 12 ? t[0] | t[1] << 8 | t[2] << 16 | t[3] << 24 : -1) !== tv || e.length !== 2) return nr("unsupported_instruction");
  const r = fd(e);
  if (!r) return nr("unresolved_account");
  const [i, o] = r;
  return { decoded: { kind: "system-transfer", source: i, destination: o, lamports: dd(t, 4) }, undecodedReason: null };
}
function ov(e, t) {
  const n = t.length === 9 && t[0] === nv && e.length === 3, r = t.length === 10 && t[0] === rv && e.length === 4;
  if (!n && !r) return nr("unsupported_instruction");
  const i = fd(e);
  if (!i) return nr("unresolved_account");
  const o = dd(t, 1);
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
function sv(e, t, n) {
  return e === ud ? iv(t, n) : e === ld ? ov(t, n) : nr("unsupported_program");
}
const Xi = 3, Yi = 3, av = 3;
function pd(e) {
  return e === ud ? ["review.note.systemProgram"] : e === ld ? ["review.note.tokenProgram"] : [];
}
function Rs(e) {
  return e.length / 2;
}
function hd(e) {
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
function cv(e) {
  return e.kind === "spl-transfer-checked" ? "TransferChecked" : "Transfer";
}
function rc(e) {
  return {
    label: "review.field.undecodedInstruction",
    value: e.programId,
    notes: [{ key: "review.note.dataBytes", count: Rs(e.dataHex) }, "review.note.notDecoded"]
  };
}
function uv(e, t) {
  const n = (r) => ({ label: r, index: t, value: "", heading: !0 });
  switch (e.kind) {
    case "system-transfer":
      return [
        n("review.field.transferSol"),
        { label: "review.field.to", value: e.destination },
        { label: "review.field.amount", value: e.lamports, native: "sol" }
      ];
    case "spl-transfer":
      return [
        n("review.field.transferTokenNoMint"),
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount },
        { label: "review.field.amount", value: e.amount, notes: ["review.note.baseUnits"] }
      ];
    case "spl-transfer-checked":
      return [
        n("review.field.transferToken"),
        { label: "review.field.toTokenAccount", value: e.destinationTokenAccount },
        {
          label: "review.field.amount",
          value: e.amount,
          notes: ["review.note.baseUnits", { key: "review.note.decimals", count: e.decimals }]
        },
        { label: "review.field.mint", value: e.mint }
      ];
  }
}
function lv(e) {
  if (e.instructions.length === 1) {
    const [i] = e.instructions;
    return i.decoded ? hd(i.decoded) : [rc(i)];
  }
  let t = 0, n = 0;
  const r = e.instructions.flatMap((i) => i.decoded ? (t += 1, t <= Xi ? uv(i.decoded, t) : []) : (n += 1, n <= Yi ? [rc(i)] : []));
  return [
    {
      label: "review.field.instructions",
      value: String(e.instructions.length),
      notes: [
        ...t > 0 ? [{ key: "review.note.transfersDecoded", count: t }] : [],
        ...n > 0 ? [{ key: "review.note.instructionsNotDecoded", count: n }] : []
      ]
    },
    ...r,
    ...t > Xi ? [{ label: "review.field.moreTransfers", value: "", notes: [{ key: "review.note.moreInDetails", count: t - Xi }] }] : [],
    ...n > Yi ? [{ label: "review.field.moreUndecoded", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: n - Yi }] }] : []
  ];
}
function dv(e) {
  return e.map((t) => t.index).join(", ");
}
function fv(e) {
  const t = e.decoded, n = {
    label: "review.field.data",
    value: e.dataHex,
    notes: [{ key: "review.note.bytes", count: Rs(e.dataHex) }, ...t ? [] : ["review.note.notDecoded"]]
  };
  return {
    title: "review.group.instruction",
    index: e.index + 1,
    fields: [
      { label: "review.field.program", value: e.programId, notes: pd(e.programId) },
      ...t ? [
        { label: "review.field.instruction", value: cv(t) },
        ...hd(t),
        ...t.kind === "system-transfer" ? [] : [{ label: "review.field.authority", value: t.authority }]
      ] : [],
      { label: "review.field.accountIndexes", value: dv(e.accounts) },
      n
    ]
  };
}
function pv(e) {
  const t = e.instructions.length === 1 ? e.instructions[0].decoded : null;
  return {
    kind: t ? t.kind === "system-transfer" ? "review.kind.solanaSystemTransfer" : "review.kind.solanaTokenTransfer" : "review.kind.solanaTransaction",
    signer: { label: "review.field.signingKey", value: e.expectedSigner.address },
    summary: lv(e),
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
            ...pd(n.address)
          ]
        }))
      },
      ...e.instructions.map(fv),
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
function ic(e) {
  return e.chainId === "1" ? { label: "review.field.value", value: e.value, native: "eth" } : { label: "review.field.value", value: e.value, notes: ["review.note.wei"] };
}
function oc(e) {
  return e.to === null ? { label: "review.field.contract", value: "", notes: ["review.note.contractCreation"] } : { label: "review.field.contract", value: e.to };
}
function yd(e) {
  return (e.decoded?.arguments ?? []).map((t, n) => ({
    label: "review.field.argument",
    index: n + 1,
    name: `${t.name} (${t.type})`,
    value: t.value
  }));
}
function hv(e) {
  if (e.decoded) {
    const n = { label: "review.field.call", value: e.decoded.signature }, r = yd(e);
    return r.length <= av ? [n, ...r] : [{ ...n, notes: [{ key: "review.note.argumentsInDetails", count: r.length }] }];
  }
  if (e.calldata === "0x") return [];
  const t = [{ key: "review.note.dataBytes", count: e.calldataBytes }, "review.note.notDecoded"];
  return e.to === null || e.selector === null ? [{ label: "review.field.initCode", value: "", notes: t }] : [{ label: "review.field.selector", value: e.selector, notes: t }];
}
function yv(e, t) {
  const n = Cu(t.publicKey).eth;
  if (!n) throw new Error("EVM review requires a secp256k1 signing key");
  const r = (i, o) => o === void 0 ? [] : [{ label: i, value: o, notes: ["review.note.wei"] }];
  return {
    kind: "review.kind.evmTransaction",
    signer: { label: "review.field.signingAddress", value: n },
    summary: [
      { label: "review.field.chainId", value: e.chainId },
      oc(e),
      ic(e),
      ...hv(e)
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
          oc(e),
          ic(e),
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
          ...yd(e)
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
            notes: [{ key: "review.note.bytes", count: Rs(e.unsignedTransactionHex) - 1 }],
            raw: !0
          },
          { label: "review.field.digest", value: `0x${t.digest}` }
        ]
      }
    ]
  };
}
const gv = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function Ar(e) {
  let t = 0;
  for (; t < e.length && e[t] === 0; ) t += 1;
  let n = 0n;
  for (const i of e) n = n << 8n | BigInt(i);
  let r = "";
  for (; n > 0n; )
    r = gv[Number(n % 58n)] + r, n /= 58n;
  return "1".repeat(t) + r;
}
class gd extends Error {
  code;
  constructor(t, n) {
    super(n), this.name = "SolanaMessageError", this.code = t;
  }
}
const _r = 1232, sc = 64, On = 32, wv = 32, ac = 128, cc = 256;
function de(e, t) {
  throw new gd(e, t);
}
function uc(e) {
  let t = "";
  for (const n of e) t += n.toString(16).padStart(2, "0");
  return t;
}
function bv(e) {
  return e < 128 ? 1 : e < 16384 ? 2 : 3;
}
function mv(e, t, n = "compact-u16") {
  let r = 0;
  for (let i = 0; ; i += 1) {
    t + i >= e.length && de("truncated", `message ends inside ${n}`);
    const o = e[t + i];
    o === 0 && i !== 0 && de("invalid_compact_u16", `${n} is not minimally encoded`);
    const s = (o & 128) === 0;
    if (i === 2 && !s && de("invalid_compact_u16", `${n} is longer than three bytes`), r |= (o & 127) << 7 * i, r > 65535 && de("invalid_compact_u16", `${n} exceeds 65535`), s) return { value: r, length: i + 1 };
  }
}
class vv {
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
    const { value: n, length: r } = mv(this.bytes, this.offset, t);
    return this.offset += r, n;
  }
}
function wd(e) {
  if (typeof e == "object" && e !== null) {
    for (const t of Object.values(e)) wd(t);
    Object.freeze(e);
  }
  return e;
}
function xv(e, t) {
  e instanceof Uint8Array || de("invalid_input", "message must be a byte array"), (!(t instanceof Uint8Array) || t.length !== On) && de("invalid_input", "expected public key must be 32 bytes"), e.length > _r - 1 - sc && de("packet_too_large", `message of ${e.length} bytes cannot fit a ${_r}-byte transaction`);
  const n = new Uint8Array(e), r = Ar(new Uint8Array(t)), i = new vv(n), o = i.u8("message prefix");
  let s = "legacy", a = o;
  if ((o & ac) !== 0) {
    const B = o & ~ac;
    B !== 0 && de("unsupported_version", `Solana message version ${B} is not supported`), s = "v0", a = i.u8("required signature count");
  }
  const c = i.u8("read-only signed account count"), u = i.u8("read-only unsigned account count"), l = i.compactU16("account key count"), d = i.take(l * On, "account keys"), h = Array.from({ length: l }, (B, U) => Ar(d.subarray(U * On, (U + 1) * On))), f = Ar(i.take(wv, "recent blockhash")), b = [];
  for (let B = i.compactU16("instruction count"); B > 0; B -= 1) {
    const U = i.u8("instruction program id index"), N = i.take(i.compactU16("instruction account count"), "instruction account indexes"), w = i.take(i.compactU16("instruction data length"), "instruction data");
    b.push({ programIdIndex: U, accountIndexes: N, data: w });
  }
  const y = [];
  if (s === "v0")
    for (let B = i.compactU16("address table lookup count"); B > 0; B -= 1) {
      const U = i.take(On, "address table key"), N = i.take(i.compactU16("writable lookup index count"), "writable lookup indexes"), w = i.take(i.compactU16("read-only lookup index count"), "read-only lookup indexes");
      y.push({ accountKey: U, writableIndexes: N, readonlyIndexes: w });
    }
  i.remaining !== 0 && de("trailing_bytes", `${i.remaining} bytes follow the message`), a + u > l && de("invalid_header", "header counts more signed and read-only unsigned accounts than the message carries"), c >= a && de("invalid_header", "header leaves no writable signer to pay the fee");
  let p = 0, g = 0;
  for (const B of y)
    B.writableIndexes.length + B.readonlyIndexes.length === 0 && de("invalid_address_table_lookup", "an address table lookup loads no account"), p += B.writableIndexes.length, g += B.readonlyIndexes.length;
  const v = l + p + g;
  v > cc && de("invalid_address_table_lookup", `message addresses ${v} accounts; the limit is ${cc}`);
  for (const B of b) {
    (B.programIdIndex === 0 || B.programIdIndex >= l) && de("invalid_program_id_index", `instruction program id index ${B.programIdIndex} is not a static non-payer key`);
    for (const U of B.accountIndexes)
      U >= v && de("invalid_account_index", `instruction account index ${U} is outside the message's ${v} accounts`);
  }
  const A = bv(a) + a * sc + n.length;
  A > _r && de("packet_too_large", `signed transaction would be ${A} bytes; the limit is ${_r}`);
  const I = h.indexOf(r);
  (I === -1 || I >= a) && de("signer_not_required", "the expected key is not a required signer of this message");
  const _ = h.map((B, U) => ({
    index: U,
    address: B,
    signer: U < a,
    writable: U < a ? U < a - c : U < l - u
  })), O = y.map((B) => ({
    accountKey: Ar(B.accountKey),
    writableIndexes: Array.from(B.writableIndexes),
    readonlyIndexes: Array.from(B.readonlyIndexes)
  })), C = (B) => {
    if (B < l) return { source: "static", ..._[B] };
    const U = B - l < p;
    let N = B - l - (U ? 0 : p);
    for (const [w, E] of O.entries()) {
      const k = U ? E.writableIndexes : E.readonlyIndexes;
      if (N < k.length)
        return { source: "lookup", index: B, lookupIndex: w, accountKey: E.accountKey, tableIndex: k[N], writable: U };
      N -= k.length;
    }
    throw new Error("account index was validated against the lookup tables");
  }, L = b.map((B, U) => {
    const N = h[B.programIdIndex], w = Array.from(B.accountIndexes, C);
    return {
      index: U,
      programIdIndex: B.programIdIndex,
      programId: N,
      accounts: w,
      dataHex: uc(B.data),
      ...sv(N, w, B.data)
    };
  });
  return wd({
    version: s,
    messageHex: uc(n),
    messageLength: n.length,
    transactionLength: A,
    header: { numRequiredSignatures: a, numReadonlySignedAccounts: c, numReadonlyUnsignedAccounts: u },
    feePayer: h[0],
    expectedSigner: { index: I, address: r, isFeePayer: I === 0 },
    staticAccountKeys: _,
    recentBlockhash: f,
    instructions: L,
    addressTableLookups: O,
    hasUndecodedContent: O.length > 0 || L.some((B) => B.decoded === null)
  });
}
function on(e, t) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new m("invalid_payload", `${t} must be an object`);
  return e;
}
function Ir(e, t) {
  if (typeof e != "string" || e.length === 0) throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Un(e, t, n) {
  if (Object.keys(e).some((r) => !t.includes(r)))
    throw new m("invalid_payload", `${n} carries unsupported fields; script trees are not supported`);
}
function Cs(e) {
  if (e && typeof e == "object") {
    for (const t of Object.values(e)) Cs(t);
    Object.freeze(e);
  }
  return e;
}
function zs(e, t) {
  if (zr(e), t && typeof t == "object") {
    const n = t.release;
    zr(n), n && typeof n == "object" && zr(n.signingContext);
  }
}
function bd(e) {
  const t = on(e, "sign request");
  zs(t.signingContext, t.context), hs(t);
  const n = on(structuredClone(t), "sign request");
  Un(n, ["material", "scheme", "signingContext", "context"], "sign request");
  const r = on(n.material, "material");
  Un(r, ["name", "envelope"], "material");
  const i = on(r.envelope, "envelope");
  Un(i, ["ciphertext", "nonce", "wrap_meta"], "envelope");
  const o = gs(n.context);
  Un(on(o, "context"), ["v", "purpose", "requestId", "grantId", "display", "release", "agent", "expiresAt", "signature"], "sign context");
  let s;
  try {
    s = Ko(n.scheme);
  } catch {
    throw new m("invalid_payload", "unsupported signing scheme");
  }
  const a = on(n.signingContext, "signingContext");
  return a.kind === "solana-transaction-v1" && Si(a), Cs({
    material: {
      name: Ir(r.name, "material.name"),
      envelope: {
        ciphertext: Ir(i.ciphertext, "envelope.ciphertext"),
        nonce: Ir(i.nonce, "envelope.nonce"),
        wrap_meta: Ir(i.wrap_meta, "envelope.wrap_meta")
      }
    },
    scheme: s,
    signingContext: a,
    context: o
  });
}
function It() {
  throw new m("invalid_sign_binding", "signing approval is missing or does not match its signed binding; update Avibe and recreate the request");
}
function md(e, t, n) {
  zs(e.signingContext, e.context), fo({ context: e.context, expectedPurpose: "sign", secret: { name: e.material.name, kind: "keypair" } });
  const r = e.context.release;
  (!r || Array.isArray(r)) && It();
  const i = t.kind === "message";
  if (!i && (t.kind !== "digest" || !/^[0-9a-f]{64}$/.test(t.digest)) && It(), Un(r, ["name", "scheme", i ? "messageHex" : "digest", "publicKey", "signingContext"], "sign context.release"), i) {
    const o = Si(e.signingContext);
    (t.messageHex !== o.messageHex || r.messageHex !== t.messageHex || e.scheme !== "ed25519" || !/^[0-9a-f]{64}$/.test(n) || r.publicKey !== n) && It();
  } else (e.scheme === "ed25519" || e.signingContext.kind === "solana-transaction-v1" || r.digest !== t.digest) && It();
  (r.name !== e.material.name || r.scheme !== e.scheme || typeof r.publicKey != "string" || r.publicKey.toLowerCase() !== n.toLowerCase() || !r.signingContext || nt(r.signingContext) !== nt(e.signingContext)) && It();
}
function Ev(e) {
  return zs(e.signingContext, e.context), ue(new TextEncoder().encode(bi(e.context)));
}
function kv(e, t) {
  if (Ko(e), e === "ed25519") throw new Error("Ed25519 exact-message signing is reviewed structurally and has no prose description");
  if (!/^(02|03)[0-9a-fA-F]{64}$/.test(t)) throw new Error("signing public key must be compressed secp256k1");
  return e === Fo ? `BIP340 raw Schnorr (untweaked internal key)
internal key: ${t.slice(2).toLowerCase()}` : e === Gn ? F("bitcoin.signingIdentity", { key: t.slice(2).toLowerCase() }) : `${e === hi ? "ECDSA secp256k1 recoverable" : "ECDSA secp256k1 DER"}
public key: ${t.toLowerCase()}`;
}
function Sv(e, t) {
  const n = Si(e.signingContext);
  md(e, { kind: "message", messageHex: n.messageHex }, t);
  let r;
  try {
    r = xv(Mo(n.messageHex), Oe(t));
  } catch (i) {
    throw i instanceof gd ? new m("invalid_payload", `Solana message cannot be signed (${i.code}): ${i.message}`) : i;
  }
  return r.messageHex !== n.messageHex && It(), { payload: { kind: "message", messageHex: r.messageHex }, review: pv(r) };
}
function Av(e, t) {
  const n = e.signingContext;
  n.kind === "solana-transaction-v1" && It();
  let r = Q0(n);
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    if (e.scheme !== Gn)
      throw new m("invalid_payload", "Bitcoin Taproot contexts require the BIP341 key-path signing scheme");
  } else {
    if (e.scheme === Gn)
      throw new m("invalid_payload", "the BIP341 key-path signing scheme requires a Bitcoin Taproot context");
    if (n.kind !== "avault-agent-operation" && e.scheme !== hi && e.scheme !== Ho)
      throw new m("invalid_payload", "EVM signing contexts require an ECDSA scheme");
  }
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    const i = ad(n, r.digest, t);
    r = { digest: i.digest, challenge: r.challenge, display: i.display };
  }
  return md(e, { kind: "digest", digest: r.digest }, t), {
    payload: { kind: "digest", digest: r.digest },
    display: `${kv(e.scheme, t)}
scheme: ${e.scheme}
${r.display}`,
    ...r.evm ? { review: yv(r.evm, { publicKey: t, scheme: e.scheme, digest: r.digest }) } : {}
  };
}
function vd(e) {
  const t = yn(e.material.envelope), n = qo(e.material.name, t.recordMetadata);
  ev(e.scheme, e.signingContext, n);
  const r = n.publicKey, i = e.signingContext.kind === "solana-transaction-v1" ? Sv(e, r) : Av(e, r);
  return { unpacked: t, recordContext: n, content: i };
}
function _v(e) {
  const t = bd(e), { unpacked: n, recordContext: r, content: i } = vd(t), o = Ev(t);
  return Cs({ request: t, verified: i, unpacked: n, recordContext: r, challenge: [...o] });
}
async function xd(e, t, n) {
  const r = bd(e.request), i = vd(r);
  (!e.verified || nt(e.verified.payload) !== nt(i.content.payload)) && It();
  const o = await Zo(n, t);
  return oi({ context: r.context, rootMetadata: o, expectedPurpose: "sign" }), si([r.context]), { request: r, ...i };
}
async function Iv(e, t, n) {
  return (await xd(e, t, n)).content;
}
async function Pv(e, t, n) {
  const { request: r, content: i, unpacked: o, recordContext: s } = await xd(e, t, n);
  return i.payload.kind === "message" ? jy(o.sealed, t, s, i.payload.messageHex) : Ky(o.sealed, t, s, i.payload.digest, r.scheme);
}
function Ed(e, t = e.label) {
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
function Bv(e) {
  const { challenge: t, ...n } = e;
  return {
    ...n,
    ...t ? { challenge: Y(t) } : {}
  };
}
function vo(e, t) {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be an object`);
  return e;
}
function Kt(e, t) {
  if (typeof e == "string" && e.length > 0) return e;
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be a text spec`);
  const n = e;
  if (typeof n.text == "string") return ge(n.text);
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
function $v(e) {
  const t = vo(e, "details");
  if (!Array.isArray(t.rows)) throw new m("invalid_payload", "details must contain rows");
  const n = [];
  for (const [r, i] of t.rows.entries()) {
    const o = vo(i, `details.rows[${r}]`), s = o.variant;
    if (s !== void 0 && s !== "text" && s !== "command" && s !== "mono" && s !== "chips")
      throw new m("invalid_payload", `details.rows[${r}].variant is invalid`);
    const a = Kt(o.label, `details.rows[${r}].label`);
    if (s === "chips") {
      if (!Array.isArray(o.values)) throw new m("invalid_payload", `details.rows[${r}].values must be an array`);
      const c = o.tone;
      if (c !== void 0 && c !== "default" && c !== "warning")
        throw new m("invalid_payload", `details.rows[${r}].tone is invalid`);
      const u = c === "default" || c === "warning" ? c : void 0;
      n.push({
        label: a,
        values: o.values.map((l, d) => Kt(l, `details.rows[${r}].values[${d}]`)),
        variant: "chips",
        ...u !== void 0 ? { tone: u } : {}
      });
      continue;
    }
    n.push({
      label: a,
      value: Kt(o.value, `details.rows[${r}].value`),
      ...s !== void 0 ? { variant: s } : {}
    });
  }
  return { rows: n };
}
function Tv(e) {
  try {
    return Zg(e);
  } catch (t) {
    throw new m("invalid_payload", t instanceof Error ? t.message : "signing review is malformed");
  }
}
function Ov(e) {
  const t = vo(e, "authorization prompt");
  if (t.challenge !== void 0 && (typeof t.challenge != "string" || t.challenge.length === 0))
    throw new m("invalid_payload", "prompt.challenge must be a non-empty string");
  if (t.reviewSession !== void 0 && typeof t.reviewSession != "string")
    throw new m("invalid_payload", "prompt.reviewSession must be a string");
  const n = t.challenge === void 0 ? void 0 : fe(t.challenge);
  return {
    title: Kt(t.title, "prompt.title"),
    subtitle: Kt(t.subtitle, "prompt.subtitle"),
    ...t.body !== void 0 ? { body: Kt(t.body, "prompt.body") } : {},
    ...t.details !== void 0 ? { details: $v(t.details) } : {},
    ...t.review !== void 0 ? { review: Tv(t.review) } : {},
    ...t.reviewSession !== void 0 ? { reviewSession: t.reviewSession } : {},
    ...n ? { challenge: n } : {},
    label: Kt(t.label, "prompt.label")
  };
}
function kd(e) {
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
async function Lv(e) {
  let t = new TextEncoder().encode(e.value);
  try {
    const n = { name: e.name, kind: "static" }, r = await _u(t, e.vmk, n);
    return { envelope: Pu(r, e.wrapMeta, n) };
  } finally {
    t?.fill(0), t = void 0;
  }
}
async function Rv(e) {
  const t = vn(e.curve), n = Hy(t);
  let r = n.privateKey;
  try {
    const i = {
      name: e.name,
      kind: "keypair",
      publicKey: n.publicKey,
      ...t === "ed25519" ? { curve: t } : {}
    }, o = await _u(r, e.vmk, i);
    return {
      envelope: Pu(o, e.wrapMeta, i),
      publicKey: n.publicKey,
      addresses: t === "ed25519" ? { solana: Vy(n.publicKey) } : Cu(n.publicKey)
    };
  } finally {
    r?.fill(0), r = void 0;
  }
}
function Sd(e) {
  return (e?.kind ?? "static") === "static";
}
function Cv(e, t) {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", "approveRelease item must be an object");
  const n = e;
  return {
    material: t(n.material),
    context: gs(n.context)
  };
}
function Ad(e) {
  const t = e[0]?.context.display;
  if (!t) throw new m("invalid_payload", "approveRelease requires at least one item");
  if (e[0].context.purpose !== "agent-deliver") throw new m("invalid_context", "approveRelease only accepts agent-deliver contexts");
  const n = za(e[0].context), r = lc(e[0].context);
  for (const s of e) {
    if (s.context.purpose !== "agent-deliver") throw new m("invalid_context", "approveRelease only accepts agent-deliver contexts");
    if (za(s.context) !== n)
      throw new m("invalid_context", "approveRelease batch contexts must share one signed display block");
    if (lc(s.context) !== r)
      throw new m("invalid_context", "approveRelease batch contexts must share one signed recipient");
  }
  const i = new Set(t.secrets.map((s) => `${s.name}:${s.kind}`));
  for (const s of e) {
    const { recordMetadata: a } = yn(s.material.envelope), c = a?.kind ?? "static";
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
function lc(e) {
  if (!e.grantId) throw new m("invalid_context", "agent delivery context is missing grantId");
  if (!e.agent) throw new m("invalid_context", "agent delivery context is missing agent key");
  return nt({ agent: e.agent, grantId: e.grantId });
}
async function zv(e) {
  if (e.items.length === 0) throw new m("invalid_payload", "approveRelease requires at least one item");
  const t = Ad(e.items), n = await Zo(e.wrapMeta, e.vmk), r = e.items.map((a) => a.context);
  for (const a of e.items)
    oi({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: e.now
    });
  si(r, e.now);
  const i = await vw(r);
  await e.confirm({
    ...t,
    challenge: i
  });
  const o = e.now ?? Date.now();
  for (const a of e.items)
    oi({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: o
    });
  e.onApprovalAccepted?.(o), si(r, o);
  const s = [];
  for (const a of e.items) {
    const { sealed: c, recordMetadata: u } = yn(a.material.envelope);
    if (!Sd(u)) throw new m("invalid_payload", "approveRelease requires static protected records");
    s.push(
      await My(
        c,
        e.vmk,
        mw(a.context),
        qo(a.material.name, u),
        await bw(a.context, a.material.name)
      )
    );
  }
  return e.consumeReplayIds !== !1 && await mi(r, o), { blindBoxes: s };
}
const _d = `avibe-vault-setup:v1:${ie.buildHash}:`, Uv = `avibe-vault-setup-result:${ie.buildHash}:`, Nv = `avibe-vault-setup-error:${ie.buildHash}:`, Mv = 300 * 1e3, Hv = 300 * 1e3, Fv = 30 * 1e3, Kv = 8 * 1e3, Ae = new qd();
let dc = !1;
function Dv(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return (t.locale === "en" || t.locale === "zh") && (t.theme === "light" || t.theme === "dark");
}
function Id(e) {
  if (!Dv(e))
    throw new m("invalid_payload", "appearance must include locale and theme");
  return e;
}
function jv(e) {
  return e == null ? null : Id(e);
}
function Ai(e) {
  const t = Ug(e.locale);
  document.documentElement.setAttribute("data-theme", e.theme), t && Fg();
}
function Pd() {
  const e = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  return { locale: Ng(), theme: e };
}
function De(e) {
  if (typeof e != "object" || e === null)
    throw new m("invalid_payload", "request payload must be an object");
  return e;
}
function Bd(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new m("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function ke(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new m("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function $d(e, t) {
  if (typeof e != "object" || e === null) throw new m("invalid_payload", `${t} must be an object`);
  return e;
}
function Vv(e) {
  const t = $d(e, "envelope");
  return {
    ciphertext: ke(t.ciphertext, "envelope.ciphertext"),
    nonce: ke(t.nonce, "envelope.nonce"),
    wrap_meta: ke(t.wrap_meta, "envelope.wrap_meta")
  };
}
function Td(e) {
  const t = $d(e, "material");
  return { name: ke(t.name, "material.name"), envelope: Vv(t.envelope) };
}
function qv(e) {
  const t = e == null ? {} : De(e);
  return { wrapMeta: Bd(t.wrapMeta, "wrapMeta") };
}
function Od(e) {
  const t = De(e), n = !!t.existingProtectedVault;
  if (n && t.rootMetadata !== void 0)
    throw new m("invalid_payload", "vault root metadata cannot be replaced during setup");
  return {
    vaultUserHandle: ke(t.vaultUserHandle, "vaultUserHandle"),
    displayName: ke(t.displayName, "displayName"),
    existingProtectedVault: n,
    authzCreationOptions: t.authzCreationOptions,
    ...t.rootMetadata !== void 0 ? { rootMetadata: t.rootMetadata } : {},
    ...t.policy !== void 0 ? { policy: ts(t.policy) } : {}
  };
}
function Gv(e) {
  const t = De(e);
  return {
    wrapMeta: ke(t.wrapMeta, "wrapMeta"),
    ...t.policy !== void 0 ? { policy: ts(t.policy) } : {}
  };
}
function Wv(e) {
  const t = De(e);
  return { material: Td(t.material), context: gs(t.context) };
}
function Zv(e) {
  const t = De(e);
  if (!Array.isArray(t.items) || t.items.length === 0)
    throw new m("invalid_payload", "approveRelease requires a non-empty items array");
  return { items: t.items.map((n) => Cv(n, Td)) };
}
function Xv(e) {
  const t = De(e);
  if (t.operation !== "delete_secret") throw new m("invalid_payload", "deleteAuthzAssertion only supports delete_secret");
  return {
    challengeId: ke(t.challengeId, "challengeId"),
    operation: "delete_secret",
    secretName: ke(t.secretName, "secretName"),
    webauthn: t.webauthn
  };
}
function Ld() {
  if (typeof crypto.randomUUID == "function") return crypto.randomUUID();
  const e = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(e, (t) => t.toString(16).padStart(2, "0")).join("");
}
async function fc(e) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(e)));
}
function Yv(e) {
  return new Promise((t, n) => {
    const r = () => {
      n(kn(e));
    };
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function Jv(e) {
  for (const t of e)
    if (t.aborted) return kn(t);
  return new Error("operation-superseded");
}
function Rd(e) {
  const t = new AbortController(), n = [], r = (i) => {
    t.signal.aborted || t.abort(kn(i));
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
async function Cd(e) {
  const t = Rd(e.abortSignals);
  try {
    await Promise.race([
      Tg(jo(e.wrapMeta), yt(), e.challenge, t.signal).catch((n) => {
        throw t.signal.aborted ? Jv(e.abortSignals) : n;
      }),
      ...e.abortSignals.map((n) => Yv(n))
    ]);
  } finally {
    t.cleanup();
  }
}
async function Us(e) {
  await or(async (t) => {
    const n = async (i) => {
      const o = Rd([t, ...e.abortSignal ? [e.abortSignal] : []]);
      try {
        const s = await rx(
          {
            prompt: pc(e),
            wrapMeta: e.wrapMeta,
            passkey: e.passkey
          },
          o.signal,
          i
        );
        if (e.passkey === "unlock") {
          if (s.kind !== "unlock") throw new Error("authorization unlock result is missing");
          await rl({
            wrapMeta: e.wrapMeta,
            currentRpId: yt(),
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
    await ps(Ed(pc(e), "common.continue"), t, void 0, n);
  });
}
function pc(e) {
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
function Qv(e) {
  return {
    ...e,
    prompt: Bv(e.prompt)
  };
}
function ex(e) {
  const t = De(e);
  if (!t) throw new m("invalid_payload", "authorization request must be an object");
  const n = t.passkey;
  if (n !== "none" && n !== "unlock" && n !== "uv")
    throw new m("invalid_payload", "authorization passkey requirement is invalid");
  return {
    prompt: Ov(t.prompt),
    wrapMeta: ke(t.wrapMeta, "wrapMeta"),
    passkey: n
  };
}
function tx(e) {
  return wc("authorize", e, Pd());
}
function nx(e) {
  const t = De(e);
  if (!t) throw new m("authorization_result_invalid", "authorization result is invalid");
  if (t.kind === "confirmed") return { kind: "confirmed" };
  if (t.kind === "unlock")
    return {
      kind: "unlock",
      prfOutput: ke(t.prfOutput, "result.prfOutput"),
      prfSalt: ke(t.prfSalt, "result.prfSalt")
    };
  throw new m("authorization_result_invalid", "authorization result kind is invalid");
}
function rx(e, t, n) {
  Dt(t);
  const r = n ?? Ld(), i = `avibe-vault-authorization-${r}`;
  return new Promise((o, s) => {
    let a = null, c = !1, u = null, l = null, d = null, h = null;
    const f = () => v(() => s(kn(t))), b = () => v(() => s(new m("authorization_cancelled", "requesting sandbox was unloaded", !0))), y = () => new m("authorization_window_closed", "authorization window was closed", !0), p = (A) => {
      a = A, l = setInterval(() => {
        !A.closed || d || (d = setTimeout(() => v(() => s(y())), 2500));
      }, 500);
    }, g = (A) => {
      if (A.origin !== window.location.origin) return;
      const I = A.data;
      if (!(!I || I.id !== r || !sn(I.build)) && (!a && n && I.type === "authorization-ready" && A.source && (h && clearTimeout(h), h = null, p(A.source)), A.source === a))
        if (I.type === "authorization-ready")
          try {
            a?.postMessage(
              {
                type: "authorization-request",
                build: ie,
                id: r,
                request: Qv(e)
              },
              window.location.origin
            );
          } catch (_) {
            v(() => s(_));
          }
        else if (I.type === "authorization-approved")
          try {
            const _ = nx(I.result);
            v(() => o(_));
          } catch (_) {
            v(() => s(_));
          }
        else I.type === "authorization-error" && v(() => s(new m(I.code, I.message, !!I.retryable)));
    }, v = (A) => {
      if (!c) {
        c = !0, u && clearTimeout(u), l && clearInterval(l), d && clearTimeout(d), h && clearTimeout(h), window.removeEventListener("message", g), window.removeEventListener("pagehide", b), t?.removeEventListener("abort", f);
        try {
          a && !a.closed && a.close();
        } catch {
        }
        A();
      }
    };
    if (window.addEventListener("message", g), window.addEventListener("pagehide", b, { once: !0 }), n)
      h = setTimeout(() => v(() => s(y())), Fv);
    else {
      const A = window.open(tx(r), i, "popup,width=520,height=760");
      if (!A) {
        v(() => s(new m("authorization_popup_blocked", "authorization window was blocked", !0)));
        return;
      }
      A.focus(), p(A);
    }
    if (u = setTimeout(
      () => v(() => s(new m("authorization_window_timeout", "authorization window timed out", !0))),
      Hv
    ), t) {
      if (t.aborted) {
        f();
        return;
      }
      t.addEventListener("abort", f, { once: !0 });
    }
  });
}
function ix(e) {
  return e === 0 ? "context.oneTime" : e % 60 === 0 ? Zn("context.minutes", { count: e / 60 }) : Zn("context.seconds", { count: e });
}
function rr(e, t) {
  const n = [];
  e.sessionLabel && n.push({ label: "context.session", value: ge(e.sessionLabel) }), e.command && n.push({ label: "context.command", value: ge(e.command), variant: "command" }), e.egress && n.push({ label: "context.egress", value: ge(e.egress) });
  const r = [
    ...e.source?.env?.map((i) => ge(`${F("context.sourceEnv")}: ${i}`)) ?? [],
    ...e.source?.tags?.map((i) => ge(`${F("context.sourceTag")}: ${i}`)) ?? [],
    ...e.source?.skills?.map((i) => ge(`${F("context.sourceSkill")}: ${i}`)) ?? []
  ];
  return r.length > 0 && n.push({ label: "context.source", values: r, variant: "chips" }), t?.agentFingerprint && n.push({ label: "context.agent", value: ge(t.agentFingerprint), variant: "mono" }), t?.grantId && n.push({ label: "context.grant", value: ge(t.grantId), variant: "mono" }), e.grantTtlSeconds !== void 0 && n.push({ label: "context.agentAccess", value: ix(e.grantTtlSeconds) }), n.push({
    label: "context.secrets",
    values: e.secrets.map(
      (i) => ge(`${i.name} · ${F(i.kind === "static" ? "context.kindStatic" : "context.kindKeypair")}`)
    ),
    variant: "chips",
    tone: "warning"
  }), { rows: n };
}
function ox(e) {
  return e instanceof Error && e.message === "vault-locked";
}
function sx(e) {
  return e instanceof Error && e.message === "vault-operation-aborted";
}
function zd(e, t) {
  return ox(e) ? !0 : sx(e) && ht(t).state === "locked";
}
async function ax(e, t = xe(), n) {
  await nl({ wrapMeta: e, currentRpId: yt(), policy: t, abortSignal: n });
}
async function cx(e, t = xe()) {
  await or((n) => ax(e, t, n));
}
async function Ud(e) {
  try {
    await Us({ ...e, passkey: "unlock" });
  } catch (t) {
    throw ht(e.wrapMeta).state === "unlocked" && Fe({ broadcast: !0, reason: "manual-lock" }), t;
  }
}
async function ux(e, t) {
  if (ht(e).state !== "unlocked") {
    if (!e) throw new Error("vault-locked");
    await cx(e);
  }
  return await os(t, { renewOnSuccess: !0 });
}
async function Nd(e) {
  const t = ht(e.wrapMeta).state, n = xe(), r = kd({ tier: e.tier, vaultState: t, policy: n });
  let i = !1;
  const o = async () => {
    await Ud({
      ...e.lockedPrompt,
      wrapMeta: e.wrapMeta,
      policy: n,
      parentSurface: e.parentSurface,
      authorizationWindow: e.authorizationWindow
    }), i = !0;
  }, s = async (a, c = !1) => {
    let u = !1;
    try {
      const l = await os(
        async (d, h, f) => {
          const b = await e.buildPrompt(d, h, f);
          return r.confirm && !c && (await Us({
            ...b,
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
      throw i && !u && ht().state === "unlocked" && Fe({ broadcast: !0, reason: "manual-lock" }), l;
    }
  };
  try {
    return r.passkey === "unlock" ? (await o(), await s("none", !0)) : await s(r.passkey === "uv" ? "uv" : "none");
  } catch (a) {
    if (r.passkey !== "unlock" && zd(a, e.wrapMeta))
      return await o(), await s("none", !0);
    throw a;
  }
}
function je(e, t) {
  if (e instanceof m) return e;
  if (Cg(e)) return new m("passkey_cancelled", "passkey-cancelled", !0);
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
function lx(e) {
  return Y(new TextEncoder().encode(JSON.stringify(e))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function dx(e) {
  return Od(JSON.parse(new TextDecoder().decode(fe(e))));
}
function fx(e, t) {
  return wc("setup", e, Pd(), { req: lx(t) });
}
function Ji(e) {
  const t = De(e);
  return {
    credentialId: ke(t.credentialId, "credentialId"),
    ...t.authzRegistration !== void 0 ? { authzRegistration: t.authzRegistration } : {}
  };
}
function _i(e) {
  return `${Uv}${e}`;
}
function Ii(e) {
  return `${Nv}${e}`;
}
function hc(e) {
  try {
    localStorage.removeItem(_i(e)), localStorage.removeItem(Ii(e));
  } catch {
  }
}
function px(e, t) {
  try {
    localStorage.setItem(_i(e), JSON.stringify({ build: ie, result: t })), localStorage.removeItem(Ii(e));
  } catch {
  }
}
function hx(e, t) {
  try {
    localStorage.setItem(
      Ii(e),
      JSON.stringify({ build: ie, code: t.code, message: t.message, retryable: t.retryable })
    ), localStorage.removeItem(_i(e));
  } catch {
  }
}
function Md(e) {
  if (window.self === window.top)
    return cs({
      rpId: yt(),
      vaultUserHandle: e.vaultUserHandle,
      displayName: e.displayName,
      authzCreationOptions: e.authzCreationOptions
    });
  const t = Ld(), n = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${_d}${t}`);
  let r = null;
  return hc(t), new Promise((i, o) => {
    let s = !1, a = null, c = null, u = null;
    const l = () => d(() => o(new m("setup_window_closed", "requesting sandbox was unloaded", !0))), d = (y) => {
      if (!s) {
        s = !0, a && clearTimeout(a), c && clearInterval(c), u && clearTimeout(u), window.removeEventListener("visibilitychange", f), window.removeEventListener("focus", f), window.removeEventListener("storage", f), window.removeEventListener("message", b), window.removeEventListener("pagehide", l), n?.close(), hc(t);
        try {
          r?.close();
        } catch {
        }
        y();
      }
    }, h = () => {
      let y = null, p = null;
      try {
        y = localStorage.getItem(_i(t)), p = localStorage.getItem(Ii(t));
      } catch {
        return !1;
      }
      if (y) {
        try {
          const g = De(JSON.parse(y));
          if (!sn(g.build)) throw new m("sandbox_build_mismatch");
          const v = Ji(g.result);
          d(() => i(v));
        } catch (g) {
          const v = g instanceof Error ? g.message : "stored setup result is invalid";
          d(() => o(new m("setup_storage_invalid", v)));
        }
        return !0;
      }
      if (p) {
        try {
          const g = De(JSON.parse(p));
          if (!sn(g.build)) throw new m("sandbox_build_mismatch");
          d(
            () => o(new m(ke(g.code, "code"), Bd(g.message, "message"), !!g.retryable))
          );
        } catch (g) {
          const v = g instanceof Error ? g.message : "stored setup error is invalid";
          d(() => o(new m("setup_storage_invalid", v)));
        }
        return !0;
      }
      return !1;
    }, f = () => {
      h();
    }, b = (y) => {
      if (y.origin !== window.location.origin || y.source !== r) return;
      const p = y.data;
      !p || p.id !== t || !sn(p.build) || (p.type === "setup-created" ? d(() => i(Ji(p.result))) : p.type === "setup-error" && d(() => o(new m(p.code, p.message, !!p.retryable))));
    };
    if (a = setTimeout(() => {
      h() || d(() => o(new m("setup_window_timeout", "setup window timed out", !0)));
    }, Mv), c = setInterval(() => {
      !r?.closed || u || h() || (u = setTimeout(() => {
        h() || d(() => o(new m("setup_window_closed", "setup window was closed", !0)));
      }, 2500));
    }, 500), n && (n.onmessage = (y) => {
      const p = y.data;
      !p || p.id !== t || !sn(p.build) || (p.type === "setup-created" ? d(() => i(Ji(p.result))) : p.type === "setup-error" && d(() => o(new m(p.code, p.message, !!p.retryable))));
    }), window.addEventListener("visibilitychange", f), window.addEventListener("focus", f), window.addEventListener("storage", f), window.addEventListener("message", b), window.addEventListener("pagehide", l, { once: !0 }), r = window.open(fx(t, e), "avibe-vault-setup", "popup,width=440,height=620"), !r) {
      d(() => o(new m("setup_popup_blocked", "setup window was blocked", !0)));
      return;
    }
    r.focus();
  });
}
function kn(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function Dt(e) {
  if (e?.aborted) throw kn(e);
}
function yx() {
  const e = window.navigator;
  return /iP(?:hone|ad|od)/.test(e.userAgent) || e.platform === "MacIntel" && e.maxTouchPoints > 1;
}
function yc(e) {
  return yx() && Yu(e);
}
async function Hd(e, t, n, r, i = xe()) {
  const o = gy();
  let s, a;
  try {
    Dt(r), s = (await as([{ credentialId: n.credentialId, prfSalt: o }], t, r)).prfOutput, Dt(r), a = yy();
    const u = await my(a, [{ kind: "passkey", prfOutput: s, prfSalt: o, credentialId: n.credentialId }]);
    Dt(r);
    let l = Ey(u, {
      rpId: t,
      vaultUserHandle: e.vaultUserHandle
    });
    e.rootMetadata && (l = Ay(l, await Sy(a, e.rootMetadata, l)), Dt(r));
    const d = Vu({
      vmk: a,
      wrapMeta: l,
      freshSetup: !e.existingProtectedVault,
      scopeId: dg(e.vaultUserHandle),
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
function gx(e, t, n) {
  return or((r) => {
    const i = ls(
      "setup.title",
      "setup.subtitle",
      "setup.iframeBody"
    ), o = Xn("setup.createPasskey"), s = tl("setup.ready");
    return an(i.card, o), an(i.card, s), new Promise((a, c) => {
      let u = !1, l = "create", d = null, h = !1;
      const f = (_) => {
        u || (u = !0, r.removeEventListener("abort", p), ds(), _());
      }, b = (_) => {
        f(() => c(_));
      }, y = (_) => {
        u || (l = "create", d = null, W(o, "setup.createPasskey"), o.disabled = !1, W(s, _));
      }, p = () => {
        b(kn(r));
      }, g = (_) => {
        if (_ instanceof m && _.code === "setup_popup_blocked") {
          y("setup.popupBlocked");
          return;
        }
        b(_);
      }, v = () => {
        h = !0, o.disabled = !0, W(s, "setup.popupPrompt");
        let _;
        try {
          _ = Md(e);
        } catch (O) {
          g(O);
          return;
        }
        _.then(
          (O) => {
            u || (d = O, l = "finish", W(o, "setup.finish"), o.disabled = !1, W(s, "setup.createdFinish"));
          },
          (O) => {
            g(O);
          }
        );
      }, A = (_) => {
        o.disabled = !0, W(s, "setup.passkeyPrompt"), Hd(e, t, _, r, n).then(
          (O) => f(() => a(O)),
          (O) => b(O)
        );
      }, I = () => {
        o.disabled = !0, W(s, "setup.passkeyPrompt");
        let _;
        try {
          _ = cs({
            rpId: t,
            vaultUserHandle: e.vaultUserHandle,
            displayName: e.displayName,
            authzCreationOptions: e.authzCreationOptions
          });
        } catch (O) {
          if (yc(O)) {
            v();
            return;
          }
          b(O);
          return;
        }
        _.then(
          (O) => A(O),
          (O) => {
            if (yc(O)) {
              v();
              return;
            }
            b(O);
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
            A(d);
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
async function wx(e) {
  const t = Od(e);
  try {
    const n = yt(), r = t.policy ?? xe(), i = window.self !== window.top ? await gx(t, n, r) : await Hd(t, n, await Md(t), void 0, r), o = t.policy ? ns(r) : xe();
    return { ...i, policy: o };
  } catch (n) {
    throw je(n, "setup_failed");
  }
}
async function bx(e) {
  const t = Gv(e);
  try {
    const n = yt(), r = t.policy ?? xe(), i = await or(async (s) => {
      let a;
      if (await ps(
        {
          title: "unlock.sessionTitle",
          subtitle: Zn("unlock.sessionSubtitle", {
            minutes: r.windowSeconds / 60
          }),
          body: "unlock.sessionBody",
          confirmLabel: "common.continue"
        },
        s,
        void 0,
        async () => {
          a = await nl({ wrapMeta: t.wrapMeta, currentRpId: n, abortSignal: s, policy: r });
        }
      ), !a) throw new Error("vault unlock did not complete");
      return a;
    }), o = t.policy ? ns(r) : xe();
    return { ...i, policy: o };
  } catch (n) {
    throw je(n, "unlock_failed");
  }
}
async function mx(e) {
  const t = sw(e);
  t.wrapMeta && ss(t.wrapMeta);
  try {
    if (t.kind === "static" && !xe().parentValueSealAllowed)
      throw new m("parent_value_seal_disabled", "parent-value sealing is disabled by policy");
    return await ux(t.wrapMeta, async (n, r) => ({ ...t.kind === "static" ? await Lv({ name: t.name, value: t.value, vmk: n, wrapMeta: r }) : await Rv({ name: t.name, curve: t.curve, vmk: n, wrapMeta: r }), establishingVmk: bg() }));
  } catch (n) {
    throw je(n, "seal_failed");
  }
}
async function vx(e, t) {
  const n = Wv(e);
  try {
    const { sealed: r, vmkWrapMeta: i, recordMetadata: o } = yn(n.material.envelope);
    if (o?.kind === "keypair") throw new Error("keypair protected records cannot be revealed as plaintext");
    return fo({
      context: n.context,
      expectedPurpose: "reveal",
      secret: { name: n.material.name, kind: "static" }
    }), await Nd({
      tier: "R2",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: {
        title: "reveal.showConfirmTitle",
        subtitle: ge(n.material.name),
        body: "reveal.confirmBody",
        details: rr(n.context.display),
        label: "common.continue"
      },
      buildPrompt: async (s, a) => {
        const c = await Zo(a, s);
        return oi({ context: n.context, rootMetadata: c, expectedPurpose: "reveal" }), fo({
          context: n.context,
          expectedPurpose: "reveal",
          secret: { name: n.material.name, kind: "static" }
        }), si([n.context]), {
          title: "reveal.showConfirmTitle",
          subtitle: ge(n.material.name),
          body: "reveal.confirmBody",
          details: rr(n.context.display),
          challenge: await fc(bi(n.context)),
          label: "common.continue"
        };
      },
      operation: async (s, a, c) => {
        const u = await Wo(r, s, qo(n.material.name, o));
        try {
          c.assertCurrent(), await mi([n.context]), await nw({
            name: n.material.name,
            plaintext: u,
            abortSignal: c.signal,
            prepareCopy: xe().strictApprovals ? async (l) => {
              await Cd({
                wrapMeta: a,
                challenge: await fc(`reveal-copy:${n.context.requestId}:${n.material.name}`),
                abortSignals: [l, c.signal]
              }), Dt(l), Dt(c.signal);
            } : void 0
          });
        } finally {
          u.fill(0);
        }
        return { completed: !0 };
      }
    });
  } catch (r) {
    throw je(r, "reveal_failed");
  }
}
async function xx(e, t) {
  try {
    const n = _v(e), { request: r, unpacked: { vmkWrapMeta: i } } = n, o = (s) => {
      const a = r.context.display.sessionLabel;
      if (!s.review && s.display === void 0) throw new m("invalid_payload", "signing request has no reviewable content");
      return {
        title: "sign.title",
        subtitle: ge(r.material.name),
        ...s.review ? { review: s.review, ...a ? { reviewSession: a } : {} } : { body: ge(s.display ?? "") },
        details: rr(r.context.display),
        label: "sign.confirm"
      };
    };
    return await Nd({
      tier: "R3",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: o(n.verified),
      // Shown only after the daemon signature, expiry and replay state are
      // verified with the vault key, from content re-derived at that moment.
      buildPrompt: async (s, a) => ({
        ...o(await Iv(n, s, a)),
        challenge: new Uint8Array(n.challenge)
      }),
      operation: (s, a) => Pv(n, s, a),
      beforeSuccess: () => mi([r.context])
    });
  } catch (n) {
    throw je(n, "sign_failed");
  }
}
async function Ex(e, t) {
  const n = Zv(e);
  try {
    const i = yn(n.items[0].material.envelope).vmkWrapMeta;
    for (const h of n.items) {
      const f = yn(h.material.envelope);
      if (f.vmkWrapMeta !== i) throw new m("invalid_payload", "approveRelease items must share one VMK context");
      if (!Sd(f.recordMetadata)) throw new m("invalid_payload", "approveRelease only supports static protected records");
    }
    const o = Ad(n.items), s = ht(i).state, a = xe(), c = kd({ tier: "R2", vaultState: s, policy: a });
    let u = !1;
    const l = async () => {
      await Ud({
        title: "release.title",
        subtitle: Zn("release.subtitle", { count: n.items.length }),
        details: rr(o.display, o.recipient),
        label: "release.confirm",
        wrapMeta: i,
        policy: a,
        parentSurface: t.latestSurface,
        authorizationWindow: t.takeAuthorizationWindow
      }), u = !0;
    }, d = async (h, f = !1) => {
      let b = !1, y;
      try {
        const p = await os(
          async (g, v, A) => {
            const I = await zv({
              items: n.items,
              vmk: g,
              wrapMeta: v,
              consumeReplayIds: !1,
              onApprovalAccepted: (_) => {
                y = _;
              },
              confirm: (_) => !c.confirm || f ? Promise.resolve() : Us({
                title: "release.title",
                subtitle: Zn("release.subtitle", { count: n.items.length }),
                details: rr(_.display, _.recipient),
                challenge: _.challenge,
                label: "release.confirm",
                wrapMeta: v,
                passkey: h,
                abortSignal: A.signal,
                parentSurface: t.latestSurface,
                authorizationWindow: t.takeAuthorizationWindow
              })
            });
            return A.assertCurrent(), I;
          },
          {
            renewOnSuccess: c.renewOnSuccess,
            beforeSuccess: () => mi(n.items.map((g) => g.context), y)
          }
        );
        return b = !0, p;
      } catch (p) {
        throw u && !b && ht().state === "unlocked" && Fe({ broadcast: !0, reason: "manual-lock" }), p;
      }
    };
    try {
      return c.passkey === "unlock" ? (await l(), await d("none", !0)) : await d(c.passkey === "uv" ? "uv" : "none");
    } catch (h) {
      if (c.passkey !== "unlock" && zd(h, i))
        return await l(), await d("none", !0);
      throw h;
    }
  } catch (r) {
    throw je(r, "approve_release_failed");
  }
}
async function kx(e) {
  const t = Xv(e);
  try {
    const n = await Og(t.webauthn);
    return { challengeId: t.challengeId, assertion: n };
  } catch (n) {
    throw je(n, "delete_authz_failed");
  }
}
function Sx() {
  const e = xo();
  if (e.get("mode") !== "setup") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Ai({ locale: t, theme: n });
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
    h = dx(d);
  } catch {
    u.disabled = !0, W(l, "setup.requestInvalid");
    return;
  }
  const f = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${_d}${r}`), b = (y) => {
    const p = je(y, "setup_create_failed"), g = {
      type: "setup-error",
      build: ie,
      id: r,
      code: p.code,
      message: p.message,
      retryable: p.retryable
    };
    hx(r, p), f?.postMessage(g);
    try {
      window.opener?.postMessage(g, window.location.origin);
    } catch {
    }
    u.disabled = !1, W(l, ge(p.message));
  };
  u.addEventListener("click", () => {
    u.disabled = !0, W(l, "setup.passkeyPrompt");
    let y;
    try {
      y = cs({
        rpId: yt(),
        vaultUserHandle: h.vaultUserHandle,
        displayName: h.displayName,
        authzCreationOptions: h.authzCreationOptions
      });
    } catch (p) {
      b(p);
      return;
    }
    y.then((p) => {
      const g = { type: "setup-created", build: ie, id: r, result: p };
      px(r, p), f?.postMessage(g);
      try {
        window.opener?.postMessage(g, window.location.origin);
      } catch {
      }
      W(l, "setup.created"), setTimeout(() => window.close(), 250);
    }).catch(b);
  });
}
function Ax() {
  const e = xo();
  if (e.get("mode") !== "authorize") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && Ai({ locale: t, theme: n });
  const r = e.get("id"), o = document.getElementById("page")?.querySelector(".card"), s = o?.querySelector(".body");
  if (!r || !o || !s || (s.classList.add("waiting"), W(s, window.opener ? "authorize.waiting" : "authorize.waitingLong"), !window.opener)) return;
  const a = () => {
    const p = window.opener;
    if (!p) return [];
    const g = [p];
    try {
      for (let v = 0; v < p.frames.length; v += 1) g.push(p.frames[v]);
    } catch {
    }
    return g;
  };
  let c = null;
  const u = (p) => {
    try {
      c?.postMessage(p, window.location.origin);
    } catch {
    }
  };
  let l = !1, d = null;
  const h = setTimeout(() => W(s, "authorize.waitingLong"), Kv), f = () => {
    if (!l)
      for (const p of a())
        try {
          p.postMessage({ type: "authorization-ready", build: ie, id: r }, window.location.origin);
        } catch {
        }
  }, b = (p) => {
    if (l) return;
    l = !0, d && clearInterval(d), clearTimeout(h), s.classList.remove("waiting"), window.removeEventListener("message", y), document.body.classList.add("setup-mode");
    const g = new AbortController();
    let v = { kind: "confirmed" };
    const A = async () => {
      if (p.passkey === "none") return;
      if (p.passkey === "uv") {
        if (!p.prompt.challenge) throw new m("invalid_payload", "authorization challenge is missing");
        await Cd({
          wrapMeta: p.wrapMeta,
          challenge: p.prompt.challenge,
          abortSignals: [g.signal]
        });
        return;
      }
      const I = await as(jo(p.wrapMeta), yt(), g.signal);
      v = {
        kind: "unlock",
        prfOutput: Y(I.prfOutput),
        prfSalt: Y(I.prfSalt)
      }, I.prfOutput.fill(0);
    };
    ps(Ed(p.prompt), g.signal, void 0, A).then(
      () => {
        u({ type: "authorization-approved", build: ie, id: r, result: v }), setTimeout(() => window.close(), 250);
      },
      (I) => {
        const _ = je(I, I instanceof Error && I.message === "operation-cancelled" ? "authorization_cancelled" : "authorization_failed");
        u({
          type: "authorization-error",
          build: ie,
          id: r,
          code: _.code,
          message: _.message,
          retryable: _.retryable
        }), setTimeout(() => window.close(), 250);
      }
    );
  }, y = (p) => {
    if (p.origin !== window.location.origin) return;
    const g = p.data;
    if (!g || g.type !== "authorization-request" || g.id !== r || !sn(g.build)) return;
    const v = a().find((A) => A === p.source);
    if (v) {
      c = v;
      try {
        b(ex(g.request));
      } catch (A) {
        const I = je(A, "authorization_request_invalid");
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
  window.addEventListener("message", y), f(), d = setInterval(f, 500);
}
function _x() {
  if (window.self !== window.top || xo().has("mode")) return;
  const e = document.querySelector("#page .body");
  e && W(e, "sandbox.intro");
}
Ae.register("handshake", (e) => {
  const t = e ?? {}, n = typeof t.expectedBuildHash == "string" ? t.expectedBuildHash : null;
  if (typeof t.parentOrigin != "string" || typeof t.nonce != "string" || t.nonce.length < 16)
    throw new m("invalid_handshake", "handshake must include parentOrigin and nonce");
  if (n !== null && n !== ie.buildHash)
    throw new m("build_hash_mismatch", "sandbox build hash does not match parent expectation");
  const r = jv(t.appearance);
  r && Ai(r);
  const i = dc ? xe() : ns(t.policy === void 0 ? void 0 : ts(t.policy));
  return dc = !0, {
    accepted: !0,
    channel: Nt,
    version: 2,
    sandboxOrigin: window.location.origin,
    build: ie,
    policy: i
  };
});
Ae.register("set-appearance", (e) => (Ai(Id(e)), { accepted: !0 }));
Ae.register("status", (e) => {
  const t = qv(e);
  try {
    t.wrapMeta && ht(t.wrapMeta);
    const n = ht(), r = xe();
    return {
      ...n,
      policy: r,
      session: {
        ...n.expiresAt !== void 0 ? { expiresAt: n.expiresAt } : {},
        strict: r.strictApprovals
      }
    };
  } catch (n) {
    throw je(n, "invalid_wrap_meta");
  }
});
Ae.register("setup", wx);
Ae.register("unlock", bx);
Ae.register("lock", () => ({ ...Fe({ broadcast: !0, reason: "manual-lock" }), policy: xe() }));
Ae.register("seal", mx);
Ae.register("reveal", vx);
Ae.register("sign", xx);
Ae.register("approveRelease", Ex);
Ae.register("deleteAuthzAssertion", kx);
ug((e) => Ae.emit("vault.state", e));
Xg((e) => Ae.emit(e));
await Gd();
Ae.start();
window.addEventListener("pagehide", mg);
Sx();
Ax();
_x();
window.self !== window.top && document.body.classList.add("embedded");
const Fd = globalThis.crypto, Ix = { webcrypto: Fd }, Kd = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: Ix,
  webcrypto: Fd
}, Symbol.toStringTag, { value: "Module" }));
