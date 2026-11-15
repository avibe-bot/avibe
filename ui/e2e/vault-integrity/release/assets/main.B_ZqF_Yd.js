const Fn = "avibe.vault.crypto";
const he = {
  sandboxVersion: "0.1.0",
  // Resource hashes are the authority; this source-input identity additionally
  // binds ready/handshake and same-release top-level ceremony pairing.
  buildHash: "f861216ffb5d366cc962fac8651da691434194c5b5459af8e249df0b956e1fa5"
};
class S extends Error {
  code;
  retryable;
  constructor(t, n, r = !1) {
    super(n ?? t), this.code = t, this.retryable = r;
  }
}
function gd(e) {
  let t;
  try {
    t = new URL(e);
  } catch {
    return !1;
  }
  const n = t.hostname.toLowerCase();
  return n === "localhost" || n === "127.0.0.1" || n === "::1" || n === "[::1]" ? t.protocol === "http:" || t.protocol === "https:" : !!(t.protocol === "https:" && (n === "avibe.bot" || n.endsWith(".avibe.bot")));
}
function Jg(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return t.channel === Fn && t.version === 2 && typeof t.id == "string" && t.id.length > 0 && typeof t.op == "string";
}
const Qg = /^[A-Za-z0-9_-]{16,128}$/;
function Vf(e) {
  return typeof e == "object" && e !== null;
}
function em(e) {
  return Vf(e) ? e.channel === Fn && e.version === 2 && e.kind === "event" && e.event === "confirm.surface" : !1;
}
class tm {
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
        channel: Fn,
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
      channel: Fn,
      version: 2,
      build: he,
      capabilities: {
        operations: Array.from(this.handlers.keys())
      }
    });
  }
  async onMessage(t) {
    if (window.parent === window || t.source !== window.parent || !gd(t.origin)) return;
    if (em(t.data)) {
      if (t.origin === this.pinnedParentOrigin && t.source === this.pinnedSource) {
        const c = t.data.surface ?? t.data.payload;
        c !== void 0 && this.recordParentSurfaceEvent(t.data, c);
      }
      return;
    }
    if (!Jg(t.data)) return;
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
    if (n.authorizationWindow !== void 0 && (typeof n.authorizationWindow != "string" || !Qg.test(n.authorizationWindow))) {
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
        channel: Fn,
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
    if (t.op !== "handshake" || !Vf(t.payload)) return null;
    const r = t.payload.parentOrigin, i = t.payload.nonce;
    return typeof r != "string" || r !== n || !gd(r) || typeof i != "string" || i.length < 16 || i.length > 256 ? null : i;
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
    return { channel: Fn, version: 2, id: t, ok: !1, error: { code: n, message: r, retryable: i } };
  }
  reply(t, n) {
    !t || this.pinnedParentOrigin === null || t.postMessage(n, this.pinnedParentOrigin);
  }
  postToParent(t, n) {
    t.postMessage(n, "*");
  }
}
function Lc() {
  return new URLSearchParams((window.location.hash ?? "").slice(1));
}
function qf(e, t, n, r = {}) {
  const i = new URL(window.location.href);
  return he.buildHash !== "dev" && (i.pathname = `/v/${he.buildHash}/index.html`), i.search = "", i.hash = new URLSearchParams({ mode: e, id: t, ...n, ...r }).toString(), i.toString();
}
function Br(e) {
  if (!e || typeof e != "object") return !1;
  const t = e;
  return t.buildHash === he.buildHash && t.sandboxVersion === he.sandboxVersion;
}
async function nm() {
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
class wt extends Error {
  constructor(t) {
    let n;
    t instanceof Error ? n = t.message : typeof t == "string" ? n = t : n = "", super(n), this.name = this.constructor.name;
  }
}
class At extends wt {
}
class $o extends wt {
}
class Gi extends wt {
}
class rm extends wt {
}
class im extends wt {
}
class om extends wt {
}
class sm extends wt {
}
class am extends wt {
}
class cm extends wt {
}
class lm extends wt {
}
class Uc extends wt {
}
const dm = {}, Gf = um(globalThis, dm);
function um(e, t) {
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
async function fm() {
  if (Gf !== void 0 && globalThis.crypto !== void 0)
    return globalThis.crypto.subtle;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Wg);
    return e.subtle;
  } catch (e) {
    throw new Uc(e);
  }
}
class Nc {
  constructor() {
    Object.defineProperty(this, "_api", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    });
  }
  async _setup() {
    this._api === void 0 && (this._api = await fm());
  }
}
const an = {
  Base: 0,
  Psk: 1,
  Auth: 2,
  AuthPsk: 3
}, md = {
  DhkemX25519HkdfSha256: 32
}, Wf = {
  HkdfSha256: 1
}, Dc = {
  Aes128Gcm: 1,
  Aes256Gcm: 2,
  ExportOnly: 65535
}, zo = 8192, hm = 268435456, bd = 32, Ue = /* @__PURE__ */ new Uint8Array(0), _t = 0n, pr = 1n, Wi = 2n, pm = /* @__PURE__ */ new Uint8Array([
  75,
  69,
  77,
  0,
  0
]), vd = /* @__PURE__ */ new Uint8Array([
  72,
  80,
  75,
  69,
  45,
  118,
  49
]);
function To(e) {
  return new Uint8Array(ae(e));
}
function ae(e) {
  return e instanceof ArrayBuffer ? e : ArrayBuffer.isView(e) ? new Uint8Array(e.buffer, e.byteOffset, e.byteLength).slice().buffer : new Uint8Array(e).slice().buffer;
}
class ym extends Nc {
  constructor() {
    super(), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Wf.HkdfSha256
    }), Object.defineProperty(this, "hashSize", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: 0
    }), Object.defineProperty(this, "_suiteId", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Ue
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
    return r.set(vd, 0), r.set(this._suiteId, 7), r.set(t, 7 + this._suiteId.byteLength), r.set(n, 7 + this._suiteId.byteLength + t.byteLength), r;
  }
  buildLabeledInfo(t, n, r) {
    this._checkInit();
    const i = new Uint8Array(9 + this._suiteId.byteLength + t.byteLength + n.byteLength);
    return i.set(new Uint8Array([0, r]), 0), i.set(vd, 2), i.set(this._suiteId, 9), i.set(t, 9 + this._suiteId.byteLength), i.set(n, 9 + this._suiteId.byteLength + t.byteLength), i;
  }
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : ae(t);
    if (r.byteLength !== this.hashSize)
      throw new At("The salt length must be the same as the hashSize");
    const i = ae(n), o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
  async expand(t, n, r) {
    await this._setup();
    const i = ae(t), o = await this._api.importKey("raw", i, this.algHash, !1, [
      "sign"
    ]), s = new ArrayBuffer(r), a = new Uint8Array(s);
    let c = Ue;
    const l = To(n), u = new Uint8Array(1);
    if (r > 255 * this.hashSize)
      throw new Error("Entropy limit reached");
    const d = new Uint8Array(this.hashSize + l.length + 1);
    for (let f = 1, h = 0; h < a.length; f++)
      u[0] = f, d.set(c, 0), d.set(l, c.length), d.set(u, c.length + l.length), c = new Uint8Array(await this._api.sign("HMAC", o, d.slice(0, c.length + l.length + 1))), a.length - h >= c.length ? (a.set(c, h), h += c.length) : (a.set(c.slice(0, a.length - h), h), h += a.length - h);
    return s;
  }
  async extractAndExpand(t, n, r, i) {
    await this._setup();
    const o = ae(n), s = await this._api.importKey("raw", o, "HKDF", !1, ["deriveBits"]);
    return await this._api.deriveBits({
      name: "HKDF",
      hash: this.algHash.hash,
      salt: ae(t),
      info: ae(r)
    }, s, i * 8);
  }
  async labeledExtract(t, n, r) {
    return await this.extract(t, this.buildLabeledIkm(n, r));
  }
  async labeledExpand(t, n, r, i) {
    return await this.expand(t, this.buildLabeledInfo(n, r, i), i);
  }
  _checkInit() {
    if (this._suiteId === Ue)
      throw new Error("Not initialized. Call init()");
  }
}
class Zf extends ym {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Wf.HkdfSha256
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
const ti = (e) => typeof e == "object" && e !== null && typeof e.privateKey == "object" && typeof e.publicKey == "object";
function mi(e, t) {
  if (t <= 0)
    throw new Error("i2Osp: too small size");
  if (e >= 256 ** t)
    throw new Error("i2Osp: too large integer");
  const n = new Uint8Array(t);
  for (let r = 0; r < t && e; r++)
    n[t - (r + 1)] = e % 256, e = Math.floor(e / 256);
  return n;
}
function Zi(e, t) {
  const n = new Uint8Array(e.length + t.length);
  return n.set(e, 0), n.set(t, e.length), n;
}
function xd(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = atob(t), r = new Uint8Array(n.length);
  for (let i = 0; i < n.length; i++)
    r[i] = n.charCodeAt(i);
  return r;
}
async function wm() {
  if (typeof Gf < "u" && globalThis.crypto !== void 0)
    return globalThis.crypto;
  try {
    const { webcrypto: e } = await Promise.resolve().then(() => Wg);
    return e;
  } catch {
    throw new Error("failed to load Crypto");
  }
}
function gm(e, t) {
  if (e.byteLength !== t.byteLength)
    throw new Error("xor: different length inputs");
  const n = new Uint8Array(e.byteLength);
  for (let r = 0; r < e.byteLength; r++)
    n[r] = e[r] ^ t[r];
  return n;
}
const mm = /* @__PURE__ */ new Uint8Array([
  101,
  97,
  101,
  95,
  112,
  114,
  107
]), bm = /* @__PURE__ */ new Uint8Array([
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
function vm(e, t, n) {
  const r = new Uint8Array(e.length + t.length + n.length);
  return r.set(e, 0), r.set(t, e.length), r.set(n, e.length + t.length), r;
}
class xm {
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
    const i = new Uint8Array(pm);
    i.set(mi(this.id, 2), 3), this._kdf.init(i);
  }
  async serializePublicKey(t) {
    return await this._prim.serializePublicKey(t);
  }
  async deserializePublicKey(t) {
    return await this._prim.deserializePublicKey(ae(t));
  }
  async serializePrivateKey(t) {
    return await this._prim.serializePrivateKey(t);
  }
  async deserializePrivateKey(t) {
    return await this._prim.deserializePrivateKey(ae(t));
  }
  async importKey(t, n, r = !0) {
    return await this._prim.importKey(t, n, r);
  }
  async generateKeyPair() {
    return await this._prim.generateKeyPair();
  }
  async deriveKeyPair(t) {
    const n = ae(t);
    if (n.byteLength > zo)
      throw new At("Too long ikm");
    return await this._prim.deriveKeyPair(n);
  }
  async encap(t) {
    let n;
    t.ekm === void 0 ? n = await this.generateKeyPair() : ti(t.ekm) ? n = t.ekm : n = await this.deriveKeyPair(t.ekm);
    const r = await this._prim.serializePublicKey(n.publicKey), i = await this._prim.serializePublicKey(t.recipientPublicKey);
    try {
      let o;
      if (t.senderKey === void 0)
        o = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey));
      else {
        const c = ti(t.senderKey) ? t.senderKey.privateKey : t.senderKey, l = new Uint8Array(await this._prim.dh(n.privateKey, t.recipientPublicKey)), u = new Uint8Array(await this._prim.dh(c, t.recipientPublicKey));
        o = Zi(l, u);
      }
      let s;
      if (t.senderKey === void 0)
        s = Zi(new Uint8Array(r), new Uint8Array(i));
      else {
        const c = ti(t.senderKey) ? t.senderKey.publicKey : await this._prim.derivePublicKey(t.senderKey), l = await this._prim.serializePublicKey(c);
        s = vm(new Uint8Array(r), new Uint8Array(i), new Uint8Array(l));
      }
      const a = await this._generateSharedSecret(o, s);
      return {
        enc: r,
        sharedSecret: a
      };
    } catch (o) {
      throw new rm(o);
    }
  }
  async decap(t) {
    const n = ae(t.enc), r = await this._prim.deserializePublicKey(n), i = ti(t.recipientKey) ? t.recipientKey.privateKey : t.recipientKey, o = ti(t.recipientKey) ? t.recipientKey.publicKey : await this._prim.derivePublicKey(t.recipientKey), s = await this._prim.serializePublicKey(o);
    try {
      let a;
      if (t.senderPublicKey === void 0)
        a = new Uint8Array(await this._prim.dh(i, r));
      else {
        const l = new Uint8Array(await this._prim.dh(i, r)), u = new Uint8Array(await this._prim.dh(i, t.senderPublicKey));
        a = Zi(l, u);
      }
      let c;
      if (t.senderPublicKey === void 0)
        c = Zi(new Uint8Array(n), new Uint8Array(s));
      else {
        const l = await this._prim.serializePublicKey(t.senderPublicKey);
        c = new Uint8Array(n.byteLength + s.byteLength + l.byteLength), c.set(new Uint8Array(n), 0), c.set(new Uint8Array(s), n.byteLength), c.set(new Uint8Array(l), n.byteLength + s.byteLength);
      }
      return await this._generateSharedSecret(a, c);
    } catch (a) {
      throw new im(a);
    }
  }
  async _generateSharedSecret(t, n) {
    const r = this._kdf.buildLabeledIkm(mm, t), i = this._kdf.buildLabeledInfo(bm, n, this.secretSize);
    return await this._kdf.extractAndExpand(Ue, r, i, this.secretSize);
  }
}
const Yi = ["deriveBits"], km = /* @__PURE__ */ new Uint8Array([
  100,
  107,
  112,
  95,
  112,
  114,
  107
]), Em = /* @__PURE__ */ new Uint8Array([115, 107]);
class yr {
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
class Am {
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
      return Promise.reject(new $o(n));
    }
  }
  async deserializePublicKey(t) {
    try {
      return await this._importRawKey(ae(t), !0);
    } catch (n) {
      throw new Gi(n);
    }
  }
  serializePrivateKey(t) {
    try {
      return Promise.resolve(t.key.buffer);
    } catch (n) {
      return Promise.reject(new $o(n));
    }
  }
  async deserializePrivateKey(t) {
    try {
      return await this._importRawKey(ae(t), !1);
    } catch (n) {
      throw new Gi(n);
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
      throw new Gi(i);
    }
  }
  async generateKeyPair() {
    try {
      const t = await this._curve.utils.randomSecretKey(), n = new yr(this._algName, t, "private", Yi);
      return { publicKey: await this.derivePublicKey(n), privateKey: n };
    } catch (t) {
      throw new Uc(t);
    }
  }
  async deriveKeyPair(t) {
    try {
      const n = ae(t), r = await this._hkdf.labeledExtract(Ue.buffer, km, new Uint8Array(n)), i = await this._hkdf.labeledExpand(r, Em, Ue, this._nSk), o = new yr(this._algName, new Uint8Array(i), "private", Yi);
      return {
        privateKey: o,
        publicKey: await this.derivePublicKey(o)
      };
    } catch (n) {
      throw new lm(n);
    }
  }
  derivePublicKey(t) {
    try {
      const n = this._curve.getPublicKey(t.key);
      return Promise.resolve(new yr(this._algName, n, "public"));
    } catch (n) {
      return Promise.reject(new Gi(n));
    }
  }
  dh(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t.key, n.key).buffer);
    } catch (r) {
      return Promise.reject(new $o(r));
    }
  }
  _importRawKey(t, n) {
    return new Promise((r, i) => {
      n && t.byteLength !== this._nPk && i(new Error("Invalid length of the key")), !n && t.byteLength !== this._nSk && i(new Error("Invalid length of the key")), r(new yr(this._algName, new Uint8Array(t), n ? "public" : "private", n ? [] : Yi));
    });
  }
  _importJWK(t, n) {
    return new Promise((r, i) => {
      t.kty !== "OKP" && i(new Error(`Invalid kty: ${t.kty}`)), t.crv !== this._algName && i(new Error(`Invalid crv: ${t.crv}`)), n ? (typeof t.d < "u" && i(new Error("Invalid key: `d` should not be set")), typeof t.x != "string" && i(new Error("Invalid key: `x` not found")), r(new yr(this._algName, xd(t.x), "public"))) : (typeof t.d != "string" && i(new Error("Invalid key: `d` not found")), r(new yr(this._algName, xd(t.d), "private", Yi)));
    });
  }
}
const Sm = ["encrypt", "decrypt"];
function _m(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Ai(e, t = "") {
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new Error(`${n}expected integer >0, got ${e}`);
  }
}
function Xt(e, t, n = "") {
  const r = _m(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function Mo(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Im(e, t) {
  Xt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new Error('"digestInto() output" expected to be of length >=' + n);
}
function Bm(e) {
  if (typeof e == "bigint") {
    if (!Po(e))
      throw new Error("positive bigint expected, got " + e);
  } else
    Ai(e);
  return e;
}
function Ko(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
const $m = /* @__PURE__ */ new Uint32Array([287454020]), Tm = /* @__PURE__ */ new Uint8Array($m.buffer);
Tm[0];
function pa(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function bt(e, t) {
  return e << 32 - t | e >>> t;
}
const Yf = /* @ts-ignore: to use toHex */ typeof Uint8Array.from([]).toHex == "function" && // @ts-ignore: to use fromHex
typeof Uint8Array.fromHex == "function", Pm = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0")), Om = [
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
function Cm(e) {
  if (Xt(e), Yf)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += Pm[e[n]];
  return t;
}
const Dt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function ec(e) {
  if (e >= Dt._0 && e <= Dt._9)
    return e - Dt._0;
  if (e >= Dt.A && e <= Dt.F)
    return e - (Dt.A - 10);
  if (e >= Dt.a && e <= Dt.f)
    return e - (Dt.a - 10);
}
function Rm(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (Yf)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = ec(e.charCodeAt(o)), a = ec(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function Hm(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  let t = _t;
  for (let n = 0; n < e.length; n++) {
    const r = ec(e.charCodeAt(n));
    if (r === void 0)
      throw new Error('hex string expected, got non-hex character "' + e[n] + '" at index ' + n);
    t = t << 4n | Om[r];
  }
  return t;
}
function Lm(e) {
  Ai(e, "numberToBigint");
  let t = e, n = _t, r = 1n;
  for (; t > 0; )
    t % 2 === 1 && (n += r), t = Math.floor(t / 2), r <<= 1n;
  return n;
}
function kd(e) {
  return Hm(Cm(tc(Xt(e)).reverse()));
}
function Um(e, t) {
  Ai(t), e = Bm(e);
  const n = Rm(e.toString(16).padStart(t * 2, "0"));
  if (n.length !== t)
    throw new Error("number too large");
  return n;
}
function Nm(e, t) {
  return Um(e, t).reverse();
}
function tc(e) {
  return Uint8Array.from(e);
}
function Po(e) {
  return typeof e == "bigint" && _t <= e;
}
function Dm(e, t, n) {
  return Po(e) && Po(t) && Po(n) && t <= e && e < n;
}
function Ed(e, t, n, r) {
  if (!Dm(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function zm(e, t = {}, n = {}) {
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
async function Mm(e = 32) {
  const t = await wm(), n = new Uint8Array(e);
  return t.getRandomValues(n), n;
}
function Km(e) {
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
function Fm(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash must wrapped by utils.createHasher");
  Ai(e.outputLen), Ai(e.blockLen);
}
function jm(e, t = {}) {
  const n = (o, s) => e(s).update(o).digest(), r = e(void 0), i = Object.assign(n, {
    outputLen: r.outputLen,
    blockLen: r.blockLen,
    create: (o) => e(o),
    ...t
  });
  return Object.freeze(i);
}
let Xf = class {
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
    }), Fm(t), Xt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), Ko(i);
  }
  update(t) {
    return Mo(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    Mo(this), Xt(t, this.outputLen, "output"), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const Jf = (e, t, n) => new Xf(e, t).update(n).digest();
Jf.create = (e, t) => new Xf(e, t);
function Vm(e, t, n) {
  return e & t ^ ~e & n;
}
function qm(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Gm = class {
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
    }), this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = pa(this.buffer);
  }
  update(t) {
    Mo(this), Xt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = pa(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Mo(this), Im(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Ko(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, Lm(this.length * 8), o), this.process(r, 0);
    const a = pa(t), c = this.outputLen;
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
const cn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Wm = /* @__PURE__ */ Uint32Array.from([
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
]), ln = /* @__PURE__ */ new Uint32Array(64);
let Zm = class extends Gm {
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
      ln[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = ln[d - 15], h = ln[d - 2], y = bt(f, 7) ^ bt(f, 18) ^ f >>> 3, w = bt(h, 17) ^ bt(h, 19) ^ h >>> 10;
      ln[d] = w + ln[d - 7] + y + ln[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = bt(a, 6) ^ bt(a, 11) ^ bt(a, 25), h = u + f + Vm(a, c, l) + Wm[d] + ln[d] | 0, w = (bt(r, 2) ^ bt(r, 13) ^ bt(r, 22)) + qm(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    Ko(ln);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Ko(this.buffer);
  }
}, Ym = class extends Zm {
  constructor() {
    super(32), Object.defineProperty(this, "A", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[0] | 0
    }), Object.defineProperty(this, "B", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[1] | 0
    }), Object.defineProperty(this, "C", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[2] | 0
    }), Object.defineProperty(this, "D", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[3] | 0
    }), Object.defineProperty(this, "E", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[4] | 0
    }), Object.defineProperty(this, "F", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[5] | 0
    }), Object.defineProperty(this, "G", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[6] | 0
    }), Object.defineProperty(this, "H", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: cn[7] | 0
    });
  }
};
const Xm = /* @__PURE__ */ jm(
  () => new Ym(),
  /* @__PURE__ */ Km(1)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Qf(e, t) {
  const n = e % t;
  return n >= _t ? n : t + n;
}
function rt(e, t, n) {
  let r = e;
  for (; t-- > _t; )
    r *= r, r %= n;
  return r;
}
function Jm(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Qm(e) {
  return zm(e, {
    adjustScalarBytes: "function",
    powPminus2: "function"
  }), Object.freeze({ ...e });
}
function e0(e) {
  const t = Qm(e), { P: n, type: r, adjustScalarBytes: i, powPminus2: o, randomBytes: s } = t, a = r === "x25519";
  if (!a && r !== "x448")
    throw new Error("invalid type");
  const c = s || Mm, l = a ? 255n : 448n, u = a ? 32 : 56, d = a ? 9n : 5n, f = a ? 121665n : 39081n, h = a ? Wi ** 254n : Wi ** 447n, y = a ? 8n * Wi ** 251n - pr : 4n * Wi ** 445n - pr, w = h + y + pr, p = (b) => Qf(b, n), g = _(d);
  function _(b) {
    return Nm(p(b), u);
  }
  function k(b) {
    const m = tc(Xt(b, u, "uCoordinate"));
    return a && (m[31] &= 127), p(kd(m));
  }
  function C(b) {
    return kd(i(tc(Xt(b, u, "scalar"))));
  }
  function B(b, m) {
    const A = D(k(m), C(b));
    if (A === _t)
      throw new Error("invalid private or public key received");
    return _(A);
  }
  function R(b) {
    return B(b, g);
  }
  const L = R, H = B;
  function $(b, m, A) {
    const P = p(b * (m - A));
    return m = p(m - P), A = p(A + P), { x_2: m, x_3: A };
  }
  function D(b, m) {
    Ed("u", b, _t, n), Ed("scalar", m, h, w);
    const A = m, P = b;
    let E = pr, I = _t, T = b, O = pr, U = _t;
    for (let N = l - 1n; N >= _t; N--) {
      const M = A >> N & pr;
      U ^= M, { x_2: E, x_3: T } = $(U, E, T), { x_2: I, x_3: O } = $(U, I, O), U = M;
      const F = E + I, j = p(F * F), V = E - I, q = p(V * V), W = j - q, de = T + O, se = T - O, ye = p(se * F), ze = p(de * V), rn = ye + ze, hr = ye - ze;
      T = p(rn * rn), O = p(P * p(hr * hr)), E = p(j * q), I = p(W * (j + p(f * W)));
    }
    ({ x_2: E, x_3: T } = $(U, E, T)), { x_2: I, x_3: O } = $(U, I, O);
    const K = o(I);
    return p(E * K);
  }
  const z = {
    secretKey: u,
    publicKey: u,
    seed: u
  }, x = async (b) => (b === void 0 && (b = await c(u)), Xt(b, z.seed, "seed"), b), v = { randomSecretKey: x };
  return Object.freeze({
    keygen: Jm(x, L),
    getSharedSecret: H,
    getPublicKey: L,
    scalarMult: B,
    scalarMultBase: R,
    utils: v,
    GuBytes: g.slice(),
    lengths: z
  });
}
class t0 extends Nc {
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
    }), this._rawKey = ae(t);
  }
  async seal(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: ae(t),
      additionalData: ae(r)
    };
    return await this._api.encrypt(i, this._key, ae(n));
  }
  async open(t, n, r) {
    await this._setupKey();
    const i = {
      name: "AES-GCM",
      iv: ae(t),
      additionalData: ae(r)
    };
    return await this._api.decrypt(i, this._key, ae(n));
  }
  async _setupKey() {
    if (this._key !== void 0)
      return;
    await this._setup();
    const t = await this._importKey(this._rawKey);
    new Uint8Array(this._rawKey).fill(0), this._key = t;
  }
  async _importKey(t) {
    return await this._api.importKey("raw", t, { name: "AES-GCM" }, !0, Sm);
  }
}
class n0 {
  constructor() {
    Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Dc.Aes128Gcm
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
    return new t0(t);
  }
}
class r0 extends n0 {
  constructor() {
    super(...arguments), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: Dc.Aes256Gcm
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
function Ad() {
  return new Promise((e, t) => {
    t(new Uc("Not supported"));
  });
}
const i0 = new Uint8Array([115, 101, 99]);
class zc {
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
    return await Ad();
  }
  async open(t, n) {
    return await Ad();
  }
  async export(t, n) {
    const r = ae(t);
    if (r.byteLength > zo)
      throw new At("Too long exporter context");
    try {
      return await this._kdf.labeledExpand(this.exporterSecret, i0, new Uint8Array(r), n);
    } catch (i) {
      throw new om(i);
    }
  }
}
class o0 extends zc {
}
class s0 extends zc {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), this.enc = i;
  }
}
class eh extends zc {
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
    const n = mi(t.seq, t.baseNonce.byteLength);
    return gm(t.baseNonce, n).buffer;
  }
  incrementSeq(t) {
    if (t.seq > Number.MAX_SAFE_INTEGER)
      throw new cm("Message limit reached");
    t.seq += 1;
  }
}
var a0 = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, c0 = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, Oo;
class th {
  constructor() {
    Oo.set(this, Promise.resolve());
  }
  async lock() {
    let t;
    const n = new Promise((i) => {
      t = i;
    }), r = a0(this, Oo, "f");
    return c0(this, Oo, n, "f"), await r, t;
  }
}
Oo = /* @__PURE__ */ new WeakMap();
var Sd = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, l0 = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, ui;
class d0 extends eh {
  constructor() {
    super(...arguments), ui.set(this, void 0);
  }
  async open(t, n = Ue.buffer) {
    l0(this, ui, Sd(this, ui, "f") ?? new th(), "f");
    const r = await Sd(this, ui, "f").lock();
    let i;
    try {
      i = await this._ctx.key.open(this.computeNonce(this._ctx), ae(t), ae(n));
    } catch (o) {
      throw new am(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
ui = /* @__PURE__ */ new WeakMap();
var _d = function(e, t, n, r) {
  if (n === "a" && !r) throw new TypeError("Private accessor was defined without a getter");
  if (typeof t == "function" ? e !== t || !r : !t.has(e)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
  return n === "m" ? r : n === "a" ? r.call(e) : r ? r.value : t.get(e);
}, u0 = function(e, t, n, r, i) {
  if (r === "m") throw new TypeError("Private method is not writable");
  if (r === "a" && !i) throw new TypeError("Private accessor was defined without a setter");
  if (typeof t == "function" ? e !== t || !i : !t.has(e)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
  return r === "a" ? i.call(e, n) : i ? i.value = n : t.set(e, n), n;
}, fi;
class f0 extends eh {
  constructor(t, n, r, i) {
    super(t, n, r), Object.defineProperty(this, "enc", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: void 0
    }), fi.set(this, void 0), this.enc = i;
  }
  async seal(t, n = Ue.buffer) {
    u0(this, fi, _d(this, fi, "f") ?? new th(), "f");
    const r = await _d(this, fi, "f").lock();
    let i;
    try {
      i = await this._ctx.key.seal(this.computeNonce(this._ctx), ae(t), ae(n));
    } catch (o) {
      throw new sm(o);
    } finally {
      r();
    }
    return this.incrementSeq(this._ctx), i;
  }
}
fi = /* @__PURE__ */ new WeakMap();
const h0 = new Uint8Array([
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
]), p0 = new Uint8Array([101, 120, 112]), y0 = new Uint8Array([
  105,
  110,
  102,
  111,
  95,
  104,
  97,
  115,
  104
]), w0 = new Uint8Array([107, 101, 121]), g0 = new Uint8Array([
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
]), m0 = new Uint8Array([115, 101, 99, 114, 101, 116]), b0 = new Uint8Array([
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
class v0 extends Nc {
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
      throw new At("KemId cannot be used");
    if (this._kem = t.kem, typeof t.kdf == "number")
      throw new At("KdfId cannot be used");
    if (this._kdf = t.kdf, typeof t.aead == "number")
      throw new At("AeadId cannot be used");
    this._aead = t.aead, this._suiteId = new Uint8Array(b0), this._suiteId.set(mi(this._kem.id, 2), 4), this._suiteId.set(mi(this._kdf.id, 2), 6), this._suiteId.set(mi(this._aead.id, 2), 8), this._kdf.init(this._suiteId);
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
    return t.psk !== void 0 ? r = t.senderKey !== void 0 ? an.AuthPsk : an.Psk : r = t.senderKey !== void 0 ? an.Auth : an.Base, await this._keyScheduleS(r, n.sharedSecret, n.enc, t);
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
    return t.psk !== void 0 ? r = t.senderPublicKey !== void 0 ? an.AuthPsk : an.Psk : r = t.senderPublicKey !== void 0 ? an.Auth : an.Base, await this._keyScheduleR(r, n, t);
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
  async seal(t, n, r = Ue.buffer) {
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
  async open(t, n, r = Ue.buffer) {
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
    const i = r.psk === void 0 ? Ue : To(r.psk.id), o = await this._kdf.labeledExtract(Ue, g0, i), s = r.info === void 0 ? Ue : To(r.info), a = await this._kdf.labeledExtract(Ue, y0, s), c = new Uint8Array(1 + o.byteLength + a.byteLength);
    c.set(new Uint8Array([t]), 0), c.set(new Uint8Array(o), 1), c.set(new Uint8Array(a), 1 + o.byteLength);
    const l = r.psk === void 0 ? Ue : To(r.psk.key), u = this._kdf.buildLabeledIkm(m0, l), d = this._kdf.buildLabeledInfo(p0, c, this._kdf.hashSize), f = await this._kdf.extractAndExpand(n, u, d, this._kdf.hashSize);
    if (this._aead.id === Dc.ExportOnly)
      return { aead: this._aead, exporterSecret: f };
    const h = this._kdf.buildLabeledInfo(w0, c, this._aead.keySize), y = await this._kdf.extractAndExpand(n, u, h, this._aead.keySize), w = this._kdf.buildLabeledInfo(h0, c, this._aead.nonceSize), p = await this._kdf.extractAndExpand(n, u, w, this._aead.nonceSize);
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
    return o.key === void 0 ? new s0(this._api, this._kdf, o.exporterSecret, r) : new f0(this._api, this._kdf, o, r);
  }
  async _keyScheduleR(t, n, r) {
    const i = await this._keySchedule(t, n, r);
    return i.key === void 0 ? new o0(this._api, this._kdf, i.exporterSecret) : new d0(this._api, this._kdf, i);
  }
  _validateInputLength(t) {
    if (t.info !== void 0 && t.info.byteLength > hm)
      throw new At("Too long info");
    if (t.psk !== void 0) {
      if (t.psk.key.byteLength < bd)
        throw new At(`PSK must have at least ${bd} bytes`);
      if (t.psk.key.byteLength > zo)
        throw new At("Too long psk.key");
      if (t.psk.id.byteLength > zo)
        throw new At("Too long psk.id");
    }
  }
}
class x0 extends v0 {
}
let k0 = class extends Zf {
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const E0 = 1n, Id = 2n, A0 = 3n, S0 = 5n, nh = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn;
function _0(e) {
  const t = 10n, n = 20n, r = 40n, i = 80n, o = nh, a = e * e % o * e % o, c = rt(a, Id, o) * a % o, l = rt(c, E0, o) * e % o, u = rt(l, S0, o) * l % o, d = rt(u, t, o) * u % o, f = rt(d, n, o) * d % o, h = rt(f, r, o) * f % o, y = rt(h, i, o) * h % o, w = rt(y, i, o) * h % o, p = rt(w, t, o) * u % o;
  return { pow_p_5_8: rt(p, Id, o) * e % o, b2: a };
}
function I0(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const B0 = /* @__PURE__ */ (() => {
  const e = nh;
  return e0({
    P: e,
    type: "x25519",
    powPminus2: (t) => {
      const { pow_p_5_8: n, b2: r } = _0(t);
      return Qf(rt(n, A0, e) * r, e);
    },
    adjustScalarBytes: I0
  });
})();
class $0 extends Zf {
  async extract(t, n) {
    await this._setup();
    const r = t.byteLength === 0 ? new ArrayBuffer(this.hashSize) : ae(t), i = ae(n);
    if (r.byteLength !== this.hashSize)
      return Jf(Xm, new Uint8Array(r), new Uint8Array(i)).buffer;
    const o = await this._api.importKey("raw", r, this.algHash, !1, [
      "sign"
    ]);
    return await this._api.sign("HMAC", o, i);
  }
}
class T0 extends Am {
  constructor(t) {
    super("X25519", 32, B0, t);
  }
  derive(t, n) {
    try {
      return Promise.resolve(this._curve.getSharedSecret(t, n));
    } catch (r) {
      return Promise.reject(new $o(r));
    }
  }
}
class P0 extends xm {
  constructor() {
    const t = new $0();
    super(md.DhkemX25519HkdfSha256, new T0(t), t), Object.defineProperty(this, "id", {
      enumerable: !0,
      configurable: !0,
      writable: !0,
      value: md.DhkemX25519HkdfSha256
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
function rh(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array" && "BYTES_PER_ELEMENT" in e && e.BYTES_PER_ELEMENT === 1;
}
function Ct(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(`${n}expected number, got ${typeof e}`);
  }
  if (!Number.isSafeInteger(e) || e < 0) {
    const n = t && `"${t}" `;
    throw new RangeError(`${n}expected integer >= 0, got ${e}`);
  }
}
function Rt(e, t, n = "") {
  const r = rh(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`, l = s + "expected Uint8Array" + a + ", got " + c;
    throw r ? new RangeError(l) : new TypeError(l);
  }
  return e;
}
function ih(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new TypeError("Hash must wrapped by utils.createHasher");
  if (Ct(e.outputLen), Ct(e.blockLen), e.outputLen < 1)
    throw new Error('"outputLen" must be >= 1');
  if (e.blockLen < 1)
    throw new Error('"blockLen" must be >= 1');
}
function Ur(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function Mc(e, t) {
  Rt(e, void 0, "digestInto() output");
  const n = t.outputLen;
  if (e.length < n)
    throw new RangeError('"digestInto() output" expected to be of length >=' + n);
}
function O0(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function Ht(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function ya(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function vt(e, t) {
  return e << 32 - t | e >>> t;
}
function Xi(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const C0 = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function R0(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function H0(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = R0(e[t]);
  return e;
}
const Bd = C0 ? (e) => e : H0, oh = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", L0 = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Kc(e) {
  if (Rt(e), oh)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += L0[e[n]];
  return t;
}
const zt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function $d(e) {
  if (e >= zt._0 && e <= zt._9)
    return e - zt._0;
  if (e >= zt.A && e <= zt.F)
    return e - (zt.A - 10);
  if (e >= zt.a && e <= zt.f)
    return e - (zt.a - 10);
}
function Fc(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  if (oh)
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
    const s = $d(e.charCodeAt(o)), a = $d(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new RangeError('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function U0(...e) {
  let t = 0;
  for (let r = 0; r < e.length; r++) {
    const i = e[r];
    Rt(i), t += i.length;
  }
  const n = new Uint8Array(t);
  for (let r = 0, i = 0; r < e.length; r++) {
    const o = e[r];
    n.set(o, i), i += o.length;
  }
  return n;
}
function Cs(e, t = {}) {
  const n = (i, o) => e(o).update(i).digest(), r = e(void 0);
  return n.outputLen = r.outputLen, n.blockLen = r.blockLen, n.canXOF = r.canXOF, n.create = (i) => e(i), Object.assign(n, t), Object.freeze(n);
}
function jc(e = 32) {
  Ct(e, "bytesLength");
  const t = typeof globalThis == "object" ? globalThis.crypto : null;
  if (typeof t?.getRandomValues != "function")
    throw new Error("crypto.getRandomValues must be defined");
  if (e > 65536)
    throw new RangeError(`"bytesLength" expected <= 65536, got ${e}`);
  return t.getRandomValues(new Uint8Array(e));
}
const sh = (e) => ({
  // Current NIST hashAlgs suffixes used here fit in one DER subidentifier octet.
  // Larger suffix values would need base-128 OID encoding and a different length byte.
  oid: Uint8Array.from([6, 9, 96, 134, 72, 1, 101, 3, 4, 2, e])
});
function N0(e, t, n) {
  return e & t ^ ~e & n;
}
function D0(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Vc = class {
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
    this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = ya(this.buffer);
  }
  update(t) {
    Ur(this), Rt(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = ya(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Ur(this), Mc(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Ht(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    r.setBigUint64(i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = ya(t), c = this.outputLen;
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
const dn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Te = /* @__PURE__ */ Uint32Array.from([
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
]), Ji = /* @__PURE__ */ BigInt(2 ** 32 - 1), Td = /* @__PURE__ */ BigInt(32);
function z0(e, t = !1) {
  return t ? { h: Number(e & Ji), l: Number(e >> Td & Ji) } : { h: Number(e >> Td & Ji) | 0, l: Number(e & Ji) | 0 };
}
function ah(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = z0(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const Pd = (e, t, n) => e >>> n, Od = (e, t, n) => e << 32 - n | t >>> n, wr = (e, t, n) => e >>> n | t << 32 - n, gr = (e, t, n) => e << 32 - n | t >>> n, Qi = (e, t, n) => e << 64 - n | t >>> n - 32, eo = (e, t, n) => e >>> n - 32 | t << 64 - n, M0 = (e, t, n) => e << n | t >>> 32 - n, K0 = (e, t, n) => t << n | e >>> 32 - n, F0 = (e, t, n) => t << n - 32 | e >>> 64 - n, j0 = (e, t, n) => e << n - 32 | t >>> 64 - n;
function Mt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const V0 = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), q0 = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, G0 = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), W0 = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, Z0 = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), Y0 = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, X0 = /* @__PURE__ */ Uint32Array.from([
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
]), un = /* @__PURE__ */ new Uint32Array(64);
class J0 extends Vc {
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
      un[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = un[d - 15], h = un[d - 2], y = vt(f, 7) ^ vt(f, 18) ^ f >>> 3, w = vt(h, 17) ^ vt(h, 19) ^ h >>> 10;
      un[d] = w + un[d - 7] + y + un[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = vt(a, 6) ^ vt(a, 11) ^ vt(a, 25), h = u + f + N0(a, c, l) + X0[d] + un[d] | 0, w = (vt(r, 2) ^ vt(r, 13) ^ vt(r, 22)) + D0(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    Ht(un);
  }
  destroy() {
    this.destroyed = !0, this.set(0, 0, 0, 0, 0, 0, 0, 0), Ht(this.buffer);
  }
}
class Q0 extends J0 {
  // We cannot use array here since array allows indexing by variable
  // which means optimizer/compiler cannot use registers.
  A = dn[0] | 0;
  B = dn[1] | 0;
  C = dn[2] | 0;
  D = dn[3] | 0;
  E = dn[4] | 0;
  F = dn[5] | 0;
  G = dn[6] | 0;
  H = dn[7] | 0;
  constructor() {
    super(32);
  }
}
const ch = ah([
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
].map((e) => BigInt(e))), eb = ch[0], tb = ch[1], fn = /* @__PURE__ */ new Uint32Array(80), hn = /* @__PURE__ */ new Uint32Array(80);
class nb extends Vc {
  constructor(t) {
    super(128, t, 16, !1);
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: f, Gh: h, Gl: y, Hh: w, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, f, h, y, w, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, f, h, y, w, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = y | 0, this.Hh = w | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let k = 0; k < 16; k++, n += 4)
      fn[k] = t.getUint32(n), hn[k] = t.getUint32(n += 4);
    for (let k = 16; k < 80; k++) {
      const C = fn[k - 15] | 0, B = hn[k - 15] | 0, R = wr(C, B, 1) ^ wr(C, B, 8) ^ Pd(C, B, 7), L = gr(C, B, 1) ^ gr(C, B, 8) ^ Od(C, B, 7), H = fn[k - 2] | 0, $ = hn[k - 2] | 0, D = wr(H, $, 19) ^ Qi(H, $, 61) ^ Pd(H, $, 6), z = gr(H, $, 19) ^ eo(H, $, 61) ^ Od(H, $, 6), x = G0(L, z, hn[k - 7], hn[k - 16]), v = W0(x, R, D, fn[k - 7], fn[k - 16]);
      fn[k] = v | 0, hn[k] = x | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: f, Fh: h, Fl: y, Gh: w, Gl: p, Hh: g, Hl: _ } = this;
    for (let k = 0; k < 80; k++) {
      const C = wr(d, f, 14) ^ wr(d, f, 18) ^ Qi(d, f, 41), B = gr(d, f, 14) ^ gr(d, f, 18) ^ eo(d, f, 41), R = d & h ^ ~d & w, L = f & y ^ ~f & p, H = Z0(_, B, L, tb[k], hn[k]), $ = Y0(H, g, C, R, eb[k], fn[k]), D = H | 0, z = wr(r, i, 28) ^ Qi(r, i, 34) ^ Qi(r, i, 39), x = gr(r, i, 28) ^ eo(r, i, 34) ^ eo(r, i, 39), v = r & o ^ r & a ^ o & a, b = i & s ^ i & c ^ s & c;
      g = w | 0, _ = p | 0, w = h | 0, p = y | 0, h = d | 0, y = f | 0, { h: d, l: f } = Mt(l | 0, u | 0, $ | 0, D | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const m = V0(D, x, b);
      r = q0(m, $, z, v), i = m | 0;
    }
    ({ h: r, l: i } = Mt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Mt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Mt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = Mt(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: f } = Mt(this.Eh | 0, this.El | 0, d | 0, f | 0), { h, l: y } = Mt(this.Fh | 0, this.Fl | 0, h | 0, y | 0), { h: w, l: p } = Mt(this.Gh | 0, this.Gl | 0, w | 0, p | 0), { h: g, l: _ } = Mt(this.Hh | 0, this.Hl | 0, g | 0, _ | 0), this.set(r, i, o, s, a, c, l, u, d, f, h, y, w, p, g, _);
  }
  roundClean() {
    Ht(fn, hn);
  }
  destroy() {
    this.destroyed = !0, Ht(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
class rb extends nb {
  Ah = Te[0] | 0;
  Al = Te[1] | 0;
  Bh = Te[2] | 0;
  Bl = Te[3] | 0;
  Ch = Te[4] | 0;
  Cl = Te[5] | 0;
  Dh = Te[6] | 0;
  Dl = Te[7] | 0;
  Eh = Te[8] | 0;
  El = Te[9] | 0;
  Fh = Te[10] | 0;
  Fl = Te[11] | 0;
  Gh = Te[12] | 0;
  Gl = Te[13] | 0;
  Hh = Te[14] | 0;
  Hl = Te[15] | 0;
  constructor() {
    super(64);
  }
}
const le = /* @__PURE__ */ Cs(
  () => new Q0(),
  /* @__PURE__ */ sh(1)
), nc = /* @__PURE__ */ Cs(
  () => new rb(),
  /* @__PURE__ */ sh(3)
);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const oe = (e, t, n) => Rt(e, t, n), lh = Ct, te = Kc, et = (...e) => U0(...e), lt = (e) => Fc(e), qc = rh, Gc = (e) => jc(e), Fo = /* @__PURE__ */ BigInt(0), rc = /* @__PURE__ */ BigInt(1);
function Hn(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function Wc(e) {
  if (typeof e == "bigint") {
    if (!Co(e))
      throw new RangeError("positive bigint expected, got " + e);
  } else
    lh(e);
  return e;
}
function jo(e, t = "") {
  if (typeof e != "number") {
    const n = t && `"${t}" `;
    throw new TypeError(n + "expected number, got type=" + typeof e);
  }
  if (!Number.isSafeInteger(e)) {
    const n = t && `"${t}" `;
    throw new RangeError(n + "expected safe integer, got " + e);
  }
}
function to(e) {
  const t = Wc(e).toString(16);
  return t.length & 1 ? "0" + t : t;
}
function dh(e) {
  if (typeof e != "string")
    throw new TypeError("hex string expected, got " + typeof e);
  return e === "" ? Fo : BigInt("0x" + e);
}
function Mi(e) {
  return dh(Kc(e));
}
function Nr(e) {
  return dh(Kc(ic(Rt(e)).reverse()));
}
function Zc(e, t) {
  if (Ct(t), t === 0)
    throw new RangeError("zero length");
  e = Wc(e);
  const n = e.toString(16);
  if (n.length > t * 2)
    throw new RangeError("number too large");
  return Fc(n.padStart(t * 2, "0"));
}
function uh(e, t) {
  return Zc(e, t).reverse();
}
function ib(e, t) {
  if (e = oe(e), t = oe(t), e.length !== t.length)
    return !1;
  let n = 0;
  for (let r = 0; r < e.length; r++)
    n |= e[r] ^ t[r];
  return n === 0;
}
function ic(e) {
  return Uint8Array.from(oe(e));
}
function ob(e) {
  if (typeof e != "string")
    throw new TypeError("ascii string expected, got " + typeof e);
  return Uint8Array.from(e, (t, n) => {
    const r = t.charCodeAt(0);
    if (t.length !== 1 || r > 127)
      throw new RangeError(`string contains non-ASCII character "${e[n]}" with code ${r} at position ${n}`);
    return r;
  });
}
const Co = (e) => typeof e == "bigint" && Fo <= e;
function sb(e, t, n) {
  return Co(e) && Co(t) && Co(n) && t <= e && e < n;
}
function Vo(e, t, n, r) {
  if (!sb(t, n, r))
    throw new RangeError("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function Yc(e) {
  if (e < Fo)
    throw new Error("expected non-negative bigint, got " + e);
  let t;
  for (t = 0; e > Fo; e >>= rc, t += 1)
    ;
  return t;
}
const Xc = (e) => (rc << BigInt(e)) - rc;
function ab(e, t, n) {
  if (Ct(e, "hashLen"), Ct(t, "qByteLen"), typeof n != "function")
    throw new TypeError("hmacFn must be a function");
  const r = (p) => new Uint8Array(p), i = Uint8Array.of(), o = Uint8Array.of(0), s = Uint8Array.of(1), a = 1e3;
  let c = r(e), l = r(e), u = 0;
  const d = () => {
    c.fill(1), l.fill(0), u = 0;
  }, f = (...p) => n(l, et(c, ...p)), h = (p = i) => {
    l = f(o, p), c = f(), p.length !== 0 && (l = f(s, p), c = f());
  }, y = () => {
    if (u++ >= a)
      throw new Error("drbg: tried max amount of iterations");
    let p = 0;
    const g = [];
    for (; p < t; ) {
      c = f();
      const _ = c.slice();
      g.push(_), p += c.length;
    }
    return et(...g);
  };
  return (p, g) => {
    d(), h(p);
    let _;
    for (; (_ = g(y())) === void 0; )
      h();
    return d(), _;
  };
}
function qr(e, t = {}, n = {}) {
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
const Cd = () => {
  throw new Error("not implemented");
};
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Re = /* @__PURE__ */ BigInt(0), ge = /* @__PURE__ */ BigInt(1), jn = /* @__PURE__ */ BigInt(2), fh = /* @__PURE__ */ BigInt(3), hh = /* @__PURE__ */ BigInt(4), ph = /* @__PURE__ */ BigInt(5), cb = /* @__PURE__ */ BigInt(7), yh = /* @__PURE__ */ BigInt(8), lb = /* @__PURE__ */ BigInt(9), wh = /* @__PURE__ */ BigInt(16);
function ke(e, t) {
  if (t <= Re)
    throw new Error("mod: expected positive modulus, got " + t);
  const n = e % t;
  return n >= Re ? n : t + n;
}
function ue(e, t, n) {
  if (t < Re)
    throw new Error("pow2: expected non-negative exponent, got " + t);
  let r = e;
  for (; t-- > Re; )
    r *= r, r %= n;
  return r;
}
function Rd(e, t) {
  if (e === Re)
    throw new Error("invert: expected non-zero number");
  if (t <= Re)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = ke(e, t), r = t, i = Re, o = ge;
  for (; n !== Re; ) {
    const a = r / n, c = r - n * a, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== ge)
    throw new Error("invert: does not exist");
  return ke(i, t);
}
function Jc(e, t, n) {
  const r = e;
  if (!r.eql(r.sqr(t), n))
    throw new Error("Cannot find square root");
}
function gh(e, t) {
  const n = e, r = (n.ORDER + ge) / hh, i = n.pow(t, r);
  return Jc(n, i, t), i;
}
function db(e, t) {
  const n = e, r = (n.ORDER - ph) / yh, i = n.mul(t, jn), o = n.pow(i, r), s = n.mul(t, o), a = n.mul(n.mul(s, jn), o), c = n.mul(s, n.sub(a, n.ONE));
  return Jc(n, c, t), c;
}
function ub(e) {
  const t = Rs(e), n = mh(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + cb) / wh;
  return ((a, c) => {
    const l = a;
    let u = l.pow(c, s), d = l.mul(u, r);
    const f = l.mul(u, i), h = l.mul(u, o), y = l.eql(l.sqr(d), c), w = l.eql(l.sqr(f), c);
    u = l.cmov(u, d, y), d = l.cmov(h, f, w);
    const p = l.eql(l.sqr(d), c), g = l.cmov(u, d, p);
    return Jc(l, g, c), g;
  });
}
function mh(e) {
  if (e < fh)
    throw new Error("sqrt is not defined for small field");
  let t = e - ge, n = 0;
  for (; t % jn === Re; )
    t /= jn, n++;
  let r = jn;
  const i = Rs(e);
  for (; Hd(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return gh;
  let o = i.pow(r, t);
  const s = (t + ge) / jn;
  return function(c, l) {
    const u = c;
    if (u.is0(l))
      return l;
    if (Hd(u, l) !== 1)
      throw new Error("Cannot find square root");
    let d = n, f = u.mul(u.ONE, o), h = u.pow(l, t), y = u.pow(l, s);
    for (; !u.eql(h, u.ONE); ) {
      if (u.is0(h))
        return u.ZERO;
      let w = 1, p = u.sqr(h);
      for (; !u.eql(p, u.ONE); )
        if (w++, p = u.sqr(p), w === d)
          throw new Error("Cannot find square root");
      const g = ge << BigInt(d - w - 1), _ = u.pow(f, g);
      d = w, f = u.sqr(_), h = u.mul(h, f), y = u.mul(y, _);
    }
    return y;
  };
}
function fb(e) {
  return e % hh === fh ? gh : e % yh === ph ? db : e % wh === lb ? ub(e) : mh(e);
}
const Dn = (e, t) => (ke(e, t) & ge) === ge, hb = [
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
function pb(e) {
  const t = {
    ORDER: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = hb.reduce((r, i) => (r[i] = "function", r), t);
  if (qr(e, n), jo(e.BYTES, "BYTES"), jo(e.BITS, "BITS"), e.BYTES < 1 || e.BITS < 1)
    throw new Error("invalid field: expected BYTES/BITS > 0");
  if (e.ORDER <= ge)
    throw new Error("invalid field: expected ORDER > 1, got " + e.ORDER);
  return e;
}
function yb(e, t, n) {
  const r = e;
  if (n < Re)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === Re)
    return r.ONE;
  if (n === ge)
    return t;
  let i = r.ONE, o = t;
  for (; n > Re; )
    n & ge && (i = r.mul(i, o)), o = r.sqr(o), n >>= ge;
  return i;
}
function bh(e, t, n = !1) {
  const r = e, i = new Array(t.length).fill(n ? r.ZERO : void 0), o = t.reduce((a, c, l) => r.is0(c) ? a : (i[l] = a, r.mul(a, c)), r.ONE), s = r.inv(o);
  return t.reduceRight((a, c, l) => r.is0(c) ? a : (i[l] = r.mul(a, i[l]), r.mul(a, c)), s), i;
}
function Hd(e, t) {
  const n = e, r = (n.ORDER - ge) / jn, i = n.pow(t, r), o = n.eql(i, n.ONE), s = n.eql(i, n.ZERO), a = n.eql(i, n.neg(n.ONE));
  if (!o && !s && !a)
    throw new Error("invalid Legendre symbol result");
  return o ? 1 : s ? 0 : -1;
}
function wb(e, t) {
  if (t !== void 0 && lh(t), e <= Re)
    throw new Error("invalid n length: expected positive n, got " + e);
  if (t !== void 0 && t < 1)
    throw new Error("invalid n length: expected positive bit length, got " + t);
  const n = Yc(e);
  if (t !== void 0 && t < n)
    throw new Error(`invalid n length: expected bit length (${n}) >= n.length (${t})`);
  const r = t !== void 0 ? t : n, i = Math.ceil(r / 8);
  return { nBitLength: r, nByteLength: i };
}
const Ld = /* @__PURE__ */ new WeakMap();
class vh {
  ORDER;
  BITS;
  BYTES;
  isLE;
  ZERO = Re;
  ONE = ge;
  _lengths;
  _mod;
  constructor(t, n = {}) {
    if (t <= ge)
      throw new Error("invalid field: expected ORDER > 1, got " + t);
    let r;
    this.isLE = !1, n != null && typeof n == "object" && (typeof n.BITS == "number" && (r = n.BITS), typeof n.sqrt == "function" && Object.defineProperty(this, "sqrt", { value: n.sqrt, enumerable: !0 }), typeof n.isLE == "boolean" && (this.isLE = n.isLE), n.allowedLengths && (this._lengths = Object.freeze(n.allowedLengths.slice())), typeof n.modFromBytes == "boolean" && (this._mod = n.modFromBytes));
    const { nBitLength: i, nByteLength: o } = wb(t, r);
    if (o > 2048)
      throw new Error("invalid field: expected ORDER of <= 2048 bytes");
    this.ORDER = t, this.BITS = i, this.BYTES = o, Object.freeze(this);
  }
  create(t) {
    return ke(t, this.ORDER);
  }
  isValid(t) {
    if (typeof t != "bigint")
      throw new TypeError("invalid field element: expected bigint, got " + typeof t);
    return Re <= t && t < this.ORDER;
  }
  is0(t) {
    return t === Re;
  }
  // is valid and invertible
  isValidNot0(t) {
    return !this.is0(t) && this.isValid(t);
  }
  isOdd(t) {
    return (t & ge) === ge;
  }
  neg(t) {
    return ke(-t, this.ORDER);
  }
  eql(t, n) {
    return t === n;
  }
  sqr(t) {
    return ke(t * t, this.ORDER);
  }
  add(t, n) {
    return ke(t + n, this.ORDER);
  }
  sub(t, n) {
    return ke(t - n, this.ORDER);
  }
  mul(t, n) {
    return ke(t * n, this.ORDER);
  }
  pow(t, n) {
    return yb(this, t, n);
  }
  div(t, n) {
    return ke(t * Rd(n, this.ORDER), this.ORDER);
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
    return Rd(t, this.ORDER);
  }
  sqrt(t) {
    let n = Ld.get(this);
    return n || Ld.set(this, n = fb(this.ORDER)), n(this, t);
  }
  toBytes(t) {
    return this.isLE ? uh(t, this.BYTES) : Zc(t, this.BYTES);
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
    let c = o ? Nr(t) : Mi(t);
    if (a && (c = ke(c, s)), !n && !this.isValid(c))
      throw new Error("invalid field element: outside of range 0..ORDER");
    return c;
  }
  // TODO: we don't need it here, move out to separate fn
  invertBatch(t) {
    return bh(this, t);
  }
  // We can't move this out because Fp6, Fp12 implement it
  // and it's unclear what to return in there.
  cmov(t, n, r) {
    return Hn(r, "condition"), r ? n : t;
  }
}
Object.freeze(vh.prototype);
function Rs(e, t = {}) {
  return new vh(e, t);
}
function xh(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  if (e <= ge)
    throw new Error("field order must be greater than 1");
  const t = Yc(e - ge);
  return Math.ceil(t / 8);
}
function kh(e) {
  const t = xh(e);
  return t + Math.ceil(t / 2);
}
function Eh(e, t, n = !1) {
  oe(e);
  const r = e.length, i = xh(t), o = Math.max(kh(t), 16);
  if (r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? Nr(e) : Mi(e), a = ke(s, t - ge) + ge;
  return n ? uh(a, i) : Zc(a, i);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Dr = /* @__PURE__ */ BigInt(0), Vn = /* @__PURE__ */ BigInt(1);
function qo(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function bi(e, t) {
  const n = bh(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function Ah(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function wa(e, t) {
  Ah(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = Xc(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function Ud(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += Vn);
  const l = t * r, u = l + Math.abs(a) - 1, d = a === 0, f = a < 0, h = t % 2 !== 0;
  return { nextN: c, offset: u, isZero: d, isNeg: f, isNegF: h, offsetF: l };
}
const ga = /* @__PURE__ */ new WeakMap(), Sh = /* @__PURE__ */ new WeakMap();
function ma(e) {
  return Sh.get(e) || 1;
}
function Nd(e) {
  if (e !== Dr)
    throw new Error("invalid wNAF");
}
let _h = class {
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
    for (; n > Dr; )
      n & Vn && (r = r.add(i)), i = i.double(), n >>= Vn;
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
    const { windows: r, windowSize: i } = wa(n, this.bits), o = [];
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
    const s = wa(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: u, isNeg: d, isNegF: f, offsetF: h } = Ud(r, a, s);
      r = c, u ? o = o.add(qo(f, n[h])) : i = i.add(qo(d, n[l]));
    }
    return Nd(r), { p: i, f: o };
  }
  /**
   * Implements unsafe EC multiplication using precomputed tables
   * and w-ary non-adjacent form.
   * @param acc - accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = wa(t, this.bits);
    for (let s = 0; s < o.windows && r !== Dr; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: u } = Ud(r, s, o);
      if (r = a, !l) {
        const d = n[c];
        i = i.add(u ? d.negate() : d);
      }
    }
    return Nd(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = ga.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), ga.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = ma(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = ma(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    Ah(n, this.bits), Sh.set(t, n), ga.delete(t);
  }
  hasCache(t) {
    return ma(t) !== 1;
  }
};
function gb(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > Dr || r > Dr; )
    n & Vn && (o = o.add(i)), r & Vn && (s = s.add(i)), i = i.double(), n >>= Vn, r >>= Vn;
  return { p1: o, p2: s };
}
function Dd(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return pb(t), t;
  } else
    return Rs(e, { isLE: n });
}
function Ih(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > Dr))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = Dd(t.p, n.Fp, r), o = Dd(t.n, n.Fn, r), a = ["Gx", "Gy", "a", e === "weierstrass" ? "b" : "d"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
function Qc(e, t) {
  return function(r) {
    const i = e(r);
    return { secretKey: i, publicKey: t(i) };
  };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const pn = /* @__PURE__ */ BigInt(0), Ie = /* @__PURE__ */ BigInt(1), ba = /* @__PURE__ */ BigInt(2), mb = /* @__PURE__ */ BigInt(8);
function bb(e, t, n, r) {
  const i = e.sqr(n), o = e.sqr(r), s = e.add(e.mul(t.a, i), o), a = e.add(e.ONE, e.mul(t.d, e.mul(i, o)));
  return e.eql(s, a);
}
function vb(e, t = {}) {
  const n = t, r = Ih("edwards", e, n, n.FpFnLE), { Fp: i, Fn: o } = r;
  let s = r.CURVE;
  const { h: a } = s;
  qr(n, {}, { uvRatio: "function" });
  const c = ba << BigInt(o.BYTES * 8) - Ie, l = (w) => i.create(w), u = n.uvRatio === void 0 ? (w, p) => {
    try {
      return { isValid: !0, value: i.sqrt(i.div(w, p)) };
    } catch {
      return { isValid: !1, value: pn };
    }
  } : n.uvRatio;
  if (!bb(i, s, s.Gx, s.Gy))
    throw new Error("bad curve params: generator point");
  function d(w, p, g = !1) {
    const _ = g ? Ie : pn;
    return Vo("coordinate " + w, p, _, c), p;
  }
  function f(w) {
    if (!(w instanceof h))
      throw new Error("EdwardsPoint expected");
  }
  class h {
    // base / generator point
    static BASE = new h(s.Gx, s.Gy, Ie, l(s.Gx * s.Gy));
    // zero / infinity / identity point
    static ZERO = new h(pn, Ie, Ie, pn);
    // 0, 1, 1, 0
    // math field
    static Fp = i;
    // scalar field
    static Fn = o;
    X;
    Y;
    Z;
    T;
    constructor(p, g, _, k) {
      this.X = d("x", p), this.Y = d("y", g), this.Z = d("z", _, !0), this.T = d("t", k), Object.freeze(this);
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
      const { x: g, y: _ } = p || {};
      return d("x", g), d("y", _), new h(g, _, Ie, l(g * _));
    }
    // Uses algo from RFC8032 5.1.3.
    static fromBytes(p, g = !1) {
      const _ = i.BYTES, { a: k, d: C } = s;
      p = ic(oe(p, _, "point")), Hn(g, "zip215");
      const B = ic(p), R = p[_ - 1];
      B[_ - 1] = R & -129;
      const L = Nr(B), H = g ? c : i.ORDER;
      Vo("point.y", L, pn, H);
      const $ = l(L * L), D = l($ - Ie), z = l(C * $ - k);
      let { isValid: x, value: v } = u(D, z);
      if (!x)
        throw new Error("bad point: invalid y coordinate");
      const b = (v & Ie) === Ie, m = (R & 128) !== 0;
      if (!g && v === pn && m)
        throw new Error("bad point: x=0 and x_0=1");
      return m !== b && (v = l(-v)), h.fromAffine({ x: v, y: L });
    }
    static fromHex(p, g = !1) {
      return h.fromBytes(lt(p), g);
    }
    get x() {
      return this.toAffine().x;
    }
    get y() {
      return this.toAffine().y;
    }
    precompute(p = 8, g = !0) {
      return y.createCache(this, p), g || this.multiply(ba), this;
    }
    // Useful in fromAffine() - not for fromBytes(), which always created valid points.
    assertValidity() {
      const p = this, { a: g, d: _ } = s;
      if (p.is0())
        throw new Error("bad point: ZERO");
      const { X: k, Y: C, Z: B, T: R } = p, L = l(k * k), H = l(C * C), $ = l(B * B), D = l($ * $), z = l(L * g), x = l($ * l(z + H)), v = l(D + l(_ * l(L * H)));
      if (x !== v)
        throw new Error("bad point: equation left != right (1)");
      const b = l(k * C), m = l(B * R);
      if (b !== m)
        throw new Error("bad point: equation left != right (2)");
    }
    // Compare one point to another.
    equals(p) {
      f(p);
      const { X: g, Y: _, Z: k } = this, { X: C, Y: B, Z: R } = p, L = l(g * R), H = l(C * k), $ = l(_ * R), D = l(B * k);
      return L === H && $ === D;
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
      const { a: p } = s, { X: g, Y: _, Z: k } = this, C = l(g * g), B = l(_ * _), R = l(ba * l(k * k)), L = l(p * C), H = g + _, $ = l(l(H * H) - C - B), D = L + B, z = D - R, x = L - B, v = l($ * z), b = l(D * x), m = l($ * x), A = l(z * D);
      return new h(v, b, A, m);
    }
    // Fast algo for adding 2 Extended Points.
    // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#addition-add-2008-hwcd
    // Cost: 9M + 1*a + 1*d + 7add.
    add(p) {
      f(p);
      const { a: g, d: _ } = s, { X: k, Y: C, Z: B, T: R } = this, { X: L, Y: H, Z: $, T: D } = p, z = l(k * L), x = l(C * H), v = l(R * _ * D), b = l(B * $), m = l((k + C) * (L + H) - z - x), A = b - v, P = b + v, E = l(x - g * z), I = l(m * A), T = l(P * E), O = l(m * E), U = l(A * P);
      return new h(I, T, U, O);
    }
    subtract(p) {
      return f(p), this.add(p.negate());
    }
    // Constant-time multiplication.
    multiply(p) {
      if (!o.isValidNot0(p))
        throw new RangeError("invalid scalar: expected 1 <= sc < curve.n");
      const { p: g, f: _ } = y.cached(this, p, (k) => bi(h, k));
      return bi(h, [g, _])[0];
    }
    // Non-constant-time multiplication. Uses double-and-add algorithm.
    // It's faster, but should only be used when you don't care about
    // an exposed private key e.g. sig verification.
    // Keeps the same subgroup-scalar contract: 0 is allowed for public-scalar callers, but
    // n and larger values are rejected instead of being reduced mod n to the identity point.
    multiplyUnsafe(p) {
      if (!o.isValid(p))
        throw new RangeError("invalid scalar: expected 0 <= sc < curve.n");
      return p === pn ? h.ZERO : this.is0() || p === Ie ? this : y.unsafe(this, p, (g) => bi(h, g));
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
      let _ = p;
      const { X: k, Y: C, Z: B } = g, R = g.is0();
      _ == null && (_ = R ? mb : i.inv(B));
      const L = l(k * _), H = l(C * _), $ = i.mul(B, _);
      if (R)
        return { x: pn, y: Ie };
      if ($ !== Ie)
        throw new Error("invZ was invalid");
      return { x: L, y: H };
    }
    clearCofactor() {
      return a === Ie ? this : this.multiplyUnsafe(a);
    }
    toBytes() {
      const { x: p, y: g } = this.toAffine(), _ = i.toBytes(g);
      return _[_.length - 1] |= p & Ie ? 128 : 0, _;
    }
    toHex() {
      return te(this.toBytes());
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const y = new _h(h, o.BITS);
  return o.BITS >= 8 && h.BASE.precompute(8), Object.freeze(h.prototype), Object.freeze(h), h;
}
class xb {
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
    Cd();
  }
  static fromHex(t) {
    Cd();
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
    return te(this.toBytes());
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
function kb(e, t, n = {}) {
  if (typeof t != "function")
    throw new Error('"hash" function param is required');
  const r = t, i = n;
  qr(i, {}, {
    adjustScalarBytes: "function",
    randomBytes: "function",
    domain: "function",
    prehash: "function",
    zip215: "boolean",
    mapToCurve: "function"
  });
  const { prehash: o } = i, { BASE: s, Fp: a, Fn: c } = e, l = r.outputLen, u = 2 * a.BYTES;
  if (l !== void 0 && (jo(l, "hash.outputLen"), l !== u))
    throw new Error(`hash.outputLen must be ${u}, got ${l}`);
  const d = i.randomBytes === void 0 ? Gc : i.randomBytes, f = i.adjustScalarBytes === void 0 ? (x) => x : i.adjustScalarBytes, h = i.domain === void 0 ? (x, v, b) => {
    if (Hn(b, "phflag"), v.length || b)
      throw new Error("Contexts/pre-hash are not supported");
    return x;
  } : i.domain;
  function y(x) {
    return c.create(Nr(x));
  }
  function w(x) {
    const v = L.secretKey;
    oe(x, L.secretKey, "secretKey");
    const b = oe(r(x), 2 * v, "hashedSecretKey"), m = f(b.slice(0, v)), A = b.slice(v, 2 * v), P = y(m);
    return { head: m, prefix: A, scalar: P };
  }
  function p(x) {
    const { head: v, prefix: b, scalar: m } = w(x), A = s.multiply(m), P = A.toBytes();
    return { head: v, prefix: b, scalar: m, point: A, pointBytes: P };
  }
  function g(x) {
    return p(x).pointBytes;
  }
  function _(x = Uint8Array.of(), ...v) {
    const b = et(...v);
    return y(r(h(b, oe(x, void 0, "context"), !!o)));
  }
  function k(x, v, b = {}) {
    x = oe(x, void 0, "message"), o && (x = o(x));
    const { prefix: m, scalar: A, pointBytes: P } = p(v), E = _(b.context, m, x), I = s.multiply(E).toBytes(), T = _(b.context, I, P, x), O = c.create(E + T * A);
    if (!c.isValid(O))
      throw new Error("sign failed: invalid s");
    const U = et(I, c.toBytes(O));
    return oe(U, L.signature, "result");
  }
  const C = {
    zip215: i.zip215
  };
  function B(x, v, b, m = C) {
    const { context: A } = m, P = m.zip215 === void 0 ? !!C.zip215 : m.zip215, E = L.signature;
    x = oe(x, E, "signature"), v = oe(v, void 0, "message"), b = oe(b, L.publicKey, "publicKey"), P !== void 0 && Hn(P, "zip215"), o && (v = o(v));
    const I = E / 2, T = x.subarray(0, I), O = Nr(x.subarray(I, E));
    let U, K, N;
    try {
      U = e.fromBytes(b, P), K = e.fromBytes(T, P), N = s.multiplyUnsafe(O);
    } catch {
      return !1;
    }
    if (!P && U.isSmallOrder())
      return !1;
    const M = _(A, T, b, v);
    return K.add(U.multiplyUnsafe(M)).subtract(N).clearCofactor().is0();
  }
  const R = a.BYTES, L = {
    secretKey: R,
    publicKey: R,
    signature: 2 * R,
    seed: R
  };
  function H(x) {
    return x = x === void 0 ? d(L.seed) : x, oe(x, L.seed, "seed");
  }
  function $(x) {
    return qc(x) && x.length === L.secretKey;
  }
  function D(x, v) {
    try {
      return !!e.fromBytes(x, v === void 0 ? C.zip215 : v);
    } catch {
      return !1;
    }
  }
  const z = {
    getExtendedPublicKey: p,
    randomSecretKey: H,
    isValidSecretKey: $,
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
    toMontgomery(x) {
      const { y: v } = e.fromBytes(x), b = L.publicKey, m = b === 32;
      if (!m && b !== 57)
        throw new Error("only defined for 25519 and 448");
      const A = m ? a.div(Ie + v, Ie - v) : a.div(v - Ie, v + Ie);
      return a.toBytes(A);
    },
    toMontgomerySecret(x) {
      const v = L.secretKey;
      oe(x, v);
      const b = r(x.subarray(0, v));
      return f(b).subarray(0, v);
    }
  };
  return Object.freeze(L), Object.freeze(z), Object.freeze({
    keygen: Qc(H, g),
    getPublicKey: g,
    sign: k,
    verify: B,
    utils: z,
    Point: e,
    lengths: L
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Eb = /* @__PURE__ */ BigInt(0), vi = /* @__PURE__ */ BigInt(1), zd = /* @__PURE__ */ BigInt(2), Ab = /* @__PURE__ */ BigInt(5), Sb = /* @__PURE__ */ BigInt(8), Si = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffed"), Bh = {
  p: Si,
  n: BigInt("0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3ed"),
  h: Sb,
  a: BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffec"),
  d: BigInt("0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3"),
  Gx: BigInt("0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51a"),
  Gy: BigInt("0x6666666666666666666666666666666666666666666666666666666666666658")
};
function _b(e) {
  const t = BigInt(10), n = BigInt(20), r = BigInt(40), i = BigInt(80), o = Si, a = e * e % o * e % o, c = ue(a, zd, o) * a % o, l = ue(c, vi, o) * e % o, u = ue(l, Ab, o) * l % o, d = ue(u, t, o) * u % o, f = ue(d, n, o) * d % o, h = ue(f, r, o) * f % o, y = ue(h, i, o) * h % o, w = ue(y, i, o) * h % o, p = ue(w, t, o) * u % o;
  return { pow_p_5_8: ue(p, zd, o) * e % o, b2: a };
}
function Ib(e) {
  return e[0] &= 248, e[31] &= 127, e[31] |= 64, e;
}
const oc = /* @__PURE__ */ BigInt("19681161376707505956807079304988542015446066515923890162744021073123829784752");
function $h(e, t) {
  const n = Si, r = ke(t * t * t, n), i = ke(r * r * t, n), o = _b(e * i).pow_p_5_8;
  let s = ke(e * r * o, n);
  const a = ke(t * s * s, n), c = s, l = ke(s * oc, n), u = a === e, d = a === ke(-e, n), f = a === ke(-e * oc, n);
  return u && (s = c), (d || f) && (s = l), Dn(s, n) && (s = ke(-s, n)), { isValid: u || d, value: s };
}
const qn = /* @__PURE__ */ vb(Bh, { uvRatio: $h }), zn = qn.Fp, Bb = qn.Fn;
function $b(e) {
  return kb(qn, nc, Object.assign({ adjustScalarBytes: Ib, zip215: !0 }, e));
}
const ft = /* @__PURE__ */ $b({}), Md = oc, Tb = /* @__PURE__ */ BigInt("54469307008909316920995813868745141605393597292927456921205312896311721017578"), Kd = (e) => $h(vi, e), Pb = /* @__PURE__ */ BigInt("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"), Ob = (e) => zn.create(Nr(e) & Pb);
class Ve extends xb {
  // Do NOT change syntax: the following gymnastics is done,
  // because typescript strips comments, which makes bundlers disable tree-shaking.
  // prettier-ignore
  static BASE = new Ve(qn.BASE);
  // prettier-ignore
  static ZERO = new Ve(qn.ZERO);
  // prettier-ignore
  static Fp = zn;
  // prettier-ignore
  static Fn = Bb;
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
    return new Ve(qn.fromAffine(t));
  }
  assertSame(t) {
    if (!(t instanceof Ve))
      throw new Error("RistrettoPoint expected");
  }
  init(t) {
    return new Ve(t);
  }
  static fromBytes(t) {
    Rt(t, 32);
    const { a: n, d: r } = Bh, i = Si, o = (C) => zn.create(C), s = Ob(t);
    if (!ib(zn.toBytes(s), t) || Dn(s, i))
      throw new Error("invalid ristretto255 encoding 1");
    const a = o(s * s), c = o(vi + n * a), l = o(vi - n * a), u = o(c * c), d = o(l * l), f = o(n * r * u - d), { isValid: h, value: y } = Kd(o(f * d)), w = o(y * l), p = o(y * w * f);
    let g = o((s + s) * w);
    Dn(g, i) && (g = o(-g));
    const _ = o(c * p), k = o(g * _);
    if (!h || Dn(k, i) || _ === Eb)
      throw new Error("invalid ristretto255 encoding 2");
    return new Ve(new qn(g, _, vi, k));
  }
  /**
   * Converts ristretto-encoded string to ristretto point.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-decode).
   * @param hex - Ristretto-encoded 32 bytes. Not every 32-byte string is valid ristretto encoding
   */
  static fromHex(t) {
    return Ve.fromBytes(Fc(t));
  }
  /**
   * Encodes ristretto point to Uint8Array.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-encode).
   */
  toBytes() {
    let { X: t, Y: n, Z: r, T: i } = this.ep;
    const o = Si, s = (p) => zn.create(p), a = s(s(r + n) * s(r - n)), c = s(t * n), l = s(c * c), { value: u } = Kd(s(a * l)), d = s(u * a), f = s(u * c), h = s(d * f * i);
    let y;
    if (Dn(i * h, o)) {
      let p = s(n * Md), g = s(t * Md);
      t = p, n = g, y = s(d * Tb);
    } else
      y = f;
    Dn(t * h, o) && (n = s(-n));
    let w = s((r - n) * y);
    return Dn(w, o) && (w = s(-w)), zn.toBytes(w);
  }
  /**
   * Compares two Ristretto points.
   * Described in [RFC9496](https://www.rfc-editor.org/rfc/rfc9496#name-equals).
   */
  equals(t) {
    this.assertSame(t);
    const { X: n, Y: r } = this.ep, { X: i, Y: o } = t.ep, s = (l) => zn.create(l), a = s(n * o) === s(r * i), c = s(r * o) === s(n * i);
    return a || c;
  }
  is0() {
    return this.equals(Ve.ZERO);
  }
}
Object.freeze(Ve.BASE);
Object.freeze(Ve.ZERO);
Object.freeze(Ve.prototype);
Object.freeze(Ve);
class Fd {
  oHash;
  iHash;
  blockLen;
  outputLen;
  canXOF = !1;
  finished = !1;
  destroyed = !1;
  constructor(t, n) {
    if (ih(t), Rt(n, void 0, "key"), this.iHash = t.create(), typeof this.iHash.update != "function")
      throw new Error("Expected instance of class which extends utils.Hash");
    this.blockLen = this.iHash.blockLen, this.outputLen = this.iHash.outputLen;
    const r = this.blockLen, i = new Uint8Array(r);
    i.set(n.length > r ? t.create().update(n).digest() : n);
    for (let o = 0; o < i.length; o++)
      i[o] ^= 54;
    this.iHash.update(i), this.oHash = t.create();
    for (let o = 0; o < i.length; o++)
      i[o] ^= 106;
    this.oHash.update(i), Ht(i);
  }
  update(t) {
    return Ur(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    Ur(this), Mc(t, this), this.finished = !0;
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
const sc = /* @__PURE__ */ (() => {
  const e = ((t, n, r) => new Fd(t, n).update(r).digest());
  return e.create = (t, n) => new Fd(t, n), e;
})();
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const jd = (e, t) => (e + (e >= 0 ? t : -t) / Th) / t;
function Cb(e, t, n) {
  Vo("scalar", e, It, n);
  const [[r, i], [o, s]] = t, a = jd(s * e, n), c = jd(-i * e, n);
  let l = e - a * r - c * o, u = -a * i - c * s;
  const d = l < It, f = u < It;
  d && (l = -l), f && (u = -u);
  const h = Xc(Math.ceil(Yc(n) / 2)) + $n;
  if (l < It || l >= h || u < It || u >= h)
    throw new Error("splitScalar (endomorphism): failed for k");
  return { k1neg: d, k1: l, k2neg: f, k2: u };
}
function ac(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function va(e, t) {
  qr(e);
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Hn(n.lowS, "lowS"), Hn(n.prehash, "prehash"), n.format !== void 0 && ac(n.format), n;
}
let Rb = class extends Error {
  constructor(t = "") {
    super(t);
  }
};
const ot = {
  // asn.1 DER encoding utils
  Err: Rb,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = ot;
      if (jo(e, "tag"), e < 0 || e > 255)
        throw new n("tlv.encode: wrong tag");
      if (typeof t != "string")
        throw new TypeError('"data" expected string, got type=' + typeof t);
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = to(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? to(i.length / 2 | 128) : "";
      return to(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = ot;
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
      const { Err: t } = ot;
      if (Wc(e), e < It)
        throw new t("integer: negative integers are not allowed");
      let n = to(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = ot;
      if (e.length < 1)
        throw new t("invalid signature integer: empty");
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e.length > 1 && e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return Mi(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = ot, i = oe(e, void 0, "signature"), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: u } = r.decode(2, c);
    if (u.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = ot, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
};
Object.freeze(ot._tlv);
Object.freeze(ot._int);
Object.freeze(ot);
const It = /* @__PURE__ */ BigInt(0), $n = /* @__PURE__ */ BigInt(1), Th = /* @__PURE__ */ BigInt(2), no = /* @__PURE__ */ BigInt(3), Hb = /* @__PURE__ */ BigInt(4);
function Lb(e, t = {}) {
  const n = Ih("weierstrass", e, t), r = n.Fp, i = n.Fn;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  qr(t, {}, {
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
  const u = Oh(r, i);
  function d() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function f(z, x, v) {
    if (l && x.is0())
      return Uint8Array.of(0);
    const { x: b, y: m } = x.toAffine(), A = r.toBytes(b);
    if (Hn(v, "isCompressed"), v) {
      d();
      const P = !r.isOdd(m);
      return et(Ph(P), A);
    } else
      return et(Uint8Array.of(4), A, r.toBytes(m));
  }
  function h(z) {
    oe(z, void 0, "Point");
    const { publicKey: x, publicKeyUncompressed: v } = u, b = z.length, m = z[0], A = z.subarray(1);
    if (l && b === 1 && m === 0)
      return { x: r.ZERO, y: r.ZERO };
    if (b === x && (m === 2 || m === 3)) {
      const P = r.fromBytes(A);
      if (!r.isValid(P))
        throw new Error("bad point: is not on curve, wrong x");
      const E = p(P);
      let I;
      try {
        I = r.sqrt(E);
      } catch (U) {
        const K = U instanceof Error ? ": " + U.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + K);
      }
      d();
      const T = r.isOdd(I);
      return (m & 1) === 1 !== T && (I = r.neg(I)), { x: P, y: I };
    } else if (b === v && m === 4) {
      const P = r.BYTES, E = r.fromBytes(A.subarray(0, P)), I = r.fromBytes(A.subarray(P, P * 2));
      if (!g(E, I))
        throw new Error("bad point: is not on curve");
      return { x: E, y: I };
    } else
      throw new Error(`bad point: got length ${b}, expected compressed=${x} or uncompressed=${v}`);
  }
  const y = t.toBytes === void 0 ? f : t.toBytes, w = t.fromBytes === void 0 ? h : t.fromBytes;
  function p(z) {
    const x = r.sqr(z), v = r.mul(x, z);
    return r.add(r.add(v, r.mul(z, o.a)), o.b);
  }
  function g(z, x) {
    const v = r.sqr(x), b = p(z);
    return r.eql(v, b);
  }
  if (!g(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const _ = r.mul(r.pow(o.a, no), Hb), k = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(_, k)))
    throw new Error("bad curve params: a or b");
  function C(z, x, v = !1) {
    if (!r.isValid(x) || v && r.is0(x))
      throw new Error(`bad point coordinate ${z}`);
    return x;
  }
  function B(z) {
    if (!(z instanceof H))
      throw new Error("Weierstrass Point expected");
  }
  function R(z) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return Cb(z, c.basises, i.ORDER);
  }
  function L(z, x, v, b, m) {
    return v = new H(r.mul(v.X, z), v.Y, v.Z), x = qo(b, x), v = qo(m, v), x.add(v);
  }
  class H {
    // base / generator point
    static BASE = new H(o.Gx, o.Gy, r.ONE);
    // zero / infinity / identity point
    static ZERO = new H(r.ZERO, r.ONE, r.ZERO);
    // 0, 1, 0
    // math field
    static Fp = r;
    // scalar field
    static Fn = i;
    X;
    Y;
    Z;
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    constructor(x, v, b) {
      this.X = C("x", x), this.Y = C("y", v, !0), this.Z = C("z", b), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(x) {
      const { x: v, y: b } = x || {};
      if (!x || !r.isValid(v) || !r.isValid(b))
        throw new Error("invalid affine point");
      if (x instanceof H)
        throw new Error("projective point not allowed");
      return r.is0(v) && r.is0(b) ? H.ZERO : new H(v, b, r.ONE);
    }
    static fromBytes(x) {
      const v = H.fromAffine(w(oe(x, void 0, "point")));
      return v.assertValidity(), v;
    }
    static fromHex(x) {
      return H.fromBytes(lt(x));
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
    precompute(x = 8, v = !0) {
      return D.createCache(this, x), v || this.multiply(no), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      const x = this;
      if (x.is0()) {
        if (t.allowInfinityPoint && r.is0(x.X) && r.eql(x.Y, r.ONE) && r.is0(x.Z))
          return;
        throw new Error("bad point: ZERO");
      }
      const { x: v, y: b } = x.toAffine();
      if (!r.isValid(v) || !r.isValid(b))
        throw new Error("bad point: x or y not field elements");
      if (!g(v, b))
        throw new Error("bad point: equation left != right");
      if (!x.isTorsionFree())
        throw new Error("bad point: not in prime-order subgroup");
    }
    hasEvenY() {
      const { y: x } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(x);
    }
    /** Compare one point to another. */
    equals(x) {
      B(x);
      const { X: v, Y: b, Z: m } = this, { X: A, Y: P, Z: E } = x, I = r.eql(r.mul(v, E), r.mul(A, m)), T = r.eql(r.mul(b, E), r.mul(P, m));
      return I && T;
    }
    /** Flips point to one corresponding to (x, -y) in Affine coordinates. */
    negate() {
      return new H(this.X, r.neg(this.Y), this.Z);
    }
    // Renes-Costello-Batina exception-free doubling formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 3
    // Cost: 8M + 3S + 3*a + 2*b3 + 15add.
    double() {
      const { a: x, b: v } = o, b = r.mul(v, no), { X: m, Y: A, Z: P } = this;
      let E = r.ZERO, I = r.ZERO, T = r.ZERO, O = r.mul(m, m), U = r.mul(A, A), K = r.mul(P, P), N = r.mul(m, A);
      return N = r.add(N, N), T = r.mul(m, P), T = r.add(T, T), E = r.mul(x, T), I = r.mul(b, K), I = r.add(E, I), E = r.sub(U, I), I = r.add(U, I), I = r.mul(E, I), E = r.mul(N, E), T = r.mul(b, T), K = r.mul(x, K), N = r.sub(O, K), N = r.mul(x, N), N = r.add(N, T), T = r.add(O, O), O = r.add(T, O), O = r.add(O, K), O = r.mul(O, N), I = r.add(I, O), K = r.mul(A, P), K = r.add(K, K), O = r.mul(K, N), E = r.sub(E, O), T = r.mul(K, U), T = r.add(T, T), T = r.add(T, T), new H(E, I, T);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(x) {
      B(x);
      const { X: v, Y: b, Z: m } = this, { X: A, Y: P, Z: E } = x;
      let I = r.ZERO, T = r.ZERO, O = r.ZERO;
      const U = o.a, K = r.mul(o.b, no);
      let N = r.mul(v, A), M = r.mul(b, P), F = r.mul(m, E), j = r.add(v, b), V = r.add(A, P);
      j = r.mul(j, V), V = r.add(N, M), j = r.sub(j, V), V = r.add(v, m);
      let q = r.add(A, E);
      return V = r.mul(V, q), q = r.add(N, F), V = r.sub(V, q), q = r.add(b, m), I = r.add(P, E), q = r.mul(q, I), I = r.add(M, F), q = r.sub(q, I), O = r.mul(U, V), I = r.mul(K, F), O = r.add(I, O), I = r.sub(M, O), O = r.add(M, O), T = r.mul(I, O), M = r.add(N, N), M = r.add(M, N), F = r.mul(U, F), V = r.mul(K, V), M = r.add(M, F), F = r.sub(N, F), F = r.mul(U, F), V = r.add(V, F), N = r.mul(M, V), T = r.add(T, N), N = r.mul(q, V), I = r.mul(j, I), I = r.sub(I, N), N = r.mul(j, M), O = r.mul(q, O), O = r.add(O, N), new H(I, T, O);
    }
    subtract(x) {
      return B(x), this.add(x.negate());
    }
    is0() {
      return this.equals(H.ZERO);
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
    multiply(x) {
      const { endo: v } = t;
      if (!i.isValidNot0(x))
        throw new RangeError("invalid scalar: out of range");
      let b, m;
      const A = (P) => D.cached(this, P, (E) => bi(H, E));
      if (v) {
        const { k1neg: P, k1: E, k2neg: I, k2: T } = R(x), { p: O, f: U } = A(E), { p: K, f: N } = A(T);
        m = U.add(N), b = L(v.beta, O, K, P, I);
      } else {
        const { p: P, f: E } = A(x);
        b = P, m = E;
      }
      return bi(H, [b, m])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(x) {
      const { endo: v } = t, b = this, m = x;
      if (!i.isValid(m))
        throw new RangeError("invalid scalar: out of range");
      if (m === It || b.is0())
        return H.ZERO;
      if (m === $n)
        return b;
      if (D.hasCache(this))
        return this.multiply(m);
      if (v) {
        const { k1neg: A, k1: P, k2neg: E, k2: I } = R(m), { p1: T, p2: O } = gb(H, b, P, I);
        return L(v.beta, T, O, A, E);
      } else
        return D.unsafe(b, m);
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * (X, Y, Z) ∋ (x=X/Z, y=Y/Z).
     * @param invertedZ - Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(x) {
      const v = this;
      let b = x;
      const { X: m, Y: A, Z: P } = v;
      if (r.eql(P, r.ONE))
        return { x: m, y: A };
      const E = v.is0();
      b == null && (b = E ? r.ONE : r.inv(P));
      const I = r.mul(m, b), T = r.mul(A, b), O = r.mul(P, b);
      if (E)
        return { x: r.ZERO, y: r.ZERO };
      if (!r.eql(O, r.ONE))
        throw new Error("invZ was invalid");
      return { x: I, y: T };
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: x } = t;
      return s === $n ? !0 : x ? x(H, this) : D.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: x } = t;
      return s === $n ? this : x ? x(H, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return s === $n ? this.is0() : this.clearCofactor().is0();
    }
    toBytes(x = !0) {
      return Hn(x, "isCompressed"), this.assertValidity(), y(H, this, x);
    }
    toHex(x = !0) {
      return te(this.toBytes(x));
    }
    toString() {
      return `<Point ${this.is0() ? "ZERO" : this.toHex()}>`;
    }
  }
  const $ = i.BITS, D = new _h(H, t.endo ? Math.ceil($ / 2) : $);
  return $ >= 8 && H.BASE.precompute(8), Object.freeze(H.prototype), Object.freeze(H), H;
}
function Ph(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function Oh(e, t) {
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
function Ub(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes === void 0 ? Gc : t.randomBytes, i = Object.assign(Oh(e.Fp, n), {
    seed: Math.max(kh(n.ORDER), 16)
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
    return h = h === void 0 ? r(i.seed) : h, Eh(oe(h, i.seed, "seed"), n.ORDER);
  }
  function c(h, y = !0) {
    return e.BASE.multiply(n.fromBytes(h)).toBytes(y);
  }
  function l(h) {
    const { secretKey: y, publicKey: w, publicKeyUncompressed: p } = i, g = n._lengths;
    if (!qc(h))
      return;
    const _ = oe(h, void 0, "key").length, k = _ === w || _ === p, C = _ === y || !!g?.includes(_);
    if (!(k && C))
      return k;
  }
  function u(h, y, w = !0) {
    if (l(h) === !0)
      throw new Error("first arg must be private key");
    if (l(y) === !1)
      throw new Error("second arg must be public key");
    const p = n.fromBytes(h);
    return e.fromBytes(y).multiply(p).toBytes(w);
  }
  const d = {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a
  }, f = Qc(a, c);
  return Object.freeze(d), Object.freeze(i), Object.freeze({ getPublicKey: c, getSharedSecret: u, keygen: f, Point: e, utils: d, lengths: i });
}
function Nb(e, t, n = {}) {
  const r = t;
  ih(r), qr(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  }), n = Object.assign({}, n);
  const i = n.randomBytes === void 0 ? Gc : n.randomBytes, o = n.hmac === void 0 ? (m, A) => sc(r, m, A) : n.hmac, { Fp: s, Fn: a } = e, { ORDER: c, BITS: l } = a, { keygen: u, getPublicKey: d, getSharedSecret: f, utils: h, lengths: y } = Ub(e, n), w = {
    prehash: !0,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !0,
    format: "compact",
    extraEntropy: !1
  }, p = c * Th + $n < s.ORDER;
  function g(m) {
    const A = c >> $n;
    return m > A;
  }
  function _(m, A) {
    if (!a.isValidNot0(A))
      throw new Error(`invalid signature ${m}: out of range 1..Point.Fn.ORDER`);
    return A;
  }
  function k() {
    if (p)
      throw new Error('"recovered" sig type is not supported for cofactor >2 curves');
  }
  function C(m, A) {
    ac(A);
    const P = y.signature, E = A === "compact" ? P : A === "recovered" ? P + 1 : void 0;
    return oe(m, E);
  }
  class B {
    r;
    s;
    recovery;
    constructor(A, P, E) {
      if (this.r = _("r", A), this.s = _("s", P), E != null) {
        if (k(), ![0, 1, 2, 3].includes(E))
          throw new Error("invalid recovery id");
        this.recovery = E;
      }
      Object.freeze(this);
    }
    static fromBytes(A, P = w.format) {
      C(A, P);
      let E;
      if (P === "der") {
        const { r: U, s: K } = ot.toSig(oe(A));
        return new B(U, K);
      }
      P === "recovered" && (E = A[0], P = "compact", A = A.subarray(1));
      const I = y.signature / 2, T = A.subarray(0, I), O = A.subarray(I, I * 2);
      return new B(a.fromBytes(T), a.fromBytes(O), E);
    }
    static fromHex(A, P) {
      return this.fromBytes(lt(A), P);
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
      const { r: P, s: E } = this, I = this.assertRecovery(), T = I === 2 || I === 3 ? P + c : P;
      if (!s.isValid(T))
        throw new Error("invalid recovery id: sig.r+curve.n != R.x");
      const O = s.toBytes(T), U = e.fromBytes(et(Ph((I & 1) === 0), O)), K = a.inv(T), N = L(oe(A, void 0, "msgHash")), M = a.create(-N * K), F = a.create(E * K), j = e.BASE.multiplyUnsafe(M).add(U.multiplyUnsafe(F));
      if (j.is0())
        throw new Error("invalid recovery: point at infinify");
      return j.assertValidity(), j;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return g(this.s);
    }
    toBytes(A = w.format) {
      if (ac(A), A === "der")
        return lt(ot.hexFromSig(this));
      const { r: P, s: E } = this, I = a.toBytes(P), T = a.toBytes(E);
      return A === "recovered" ? (k(), et(Uint8Array.of(this.assertRecovery()), I, T)) : et(I, T);
    }
    toHex(A) {
      return te(this.toBytes(A));
    }
  }
  Object.freeze(B.prototype), Object.freeze(B);
  const R = n.bits2int === void 0 ? function(A) {
    if (A.length > 8192)
      throw new Error("input is too large");
    const P = Mi(A), E = A.length * 8 - l;
    return E > 0 ? P >> BigInt(E) : P;
  } : n.bits2int, L = n.bits2int_modN === void 0 ? function(A) {
    return a.create(R(A));
  } : n.bits2int_modN, H = Xc(l);
  function $(m) {
    return Vo("num < 2^" + l, m, It, H), a.toBytes(m);
  }
  function D(m, A) {
    return oe(m, void 0, "message"), A ? oe(r(m), void 0, "prehashed message") : m;
  }
  function z(m, A, P) {
    const { lowS: E, prehash: I, extraEntropy: T } = va(P, w);
    m = D(m, I);
    const O = L(m), U = a.fromBytes(A);
    if (!a.isValidNot0(U))
      throw new Error("invalid private key");
    const K = [$(U), $(O)];
    if (T != null && T !== !1) {
      const j = T === !0 ? i(y.secretKey) : T;
      K.push(oe(j, void 0, "extraEntropy"));
    }
    const N = et(...K), M = O;
    function F(j) {
      const V = R(j);
      if (!a.isValidNot0(V))
        return;
      const q = a.inv(V), W = e.BASE.multiply(V).toAffine(), de = a.create(W.x);
      if (de === It)
        return;
      const se = a.create(q * a.create(M + de * U));
      if (se === It)
        return;
      let ye = (W.x === de ? 0 : 2) | Number(W.y & $n), ze = se;
      return E && g(se) && (ze = a.neg(se), ye ^= 1), new B(de, ze, p ? void 0 : ye);
    }
    return { seed: N, k2sig: F };
  }
  function x(m, A, P = {}) {
    const { seed: E, k2sig: I } = z(m, A, P);
    return ab(r.outputLen, a.BYTES, o)(E, I).toBytes(P.format);
  }
  function v(m, A, P, E = {}) {
    const { lowS: I, prehash: T, format: O } = va(E, w);
    if (P = oe(P, void 0, "publicKey"), A = D(A, T), !qc(m)) {
      const U = m instanceof B ? ", use sig.toBytes()" : "";
      throw new Error("verify expects Uint8Array signature" + U);
    }
    C(m, O);
    try {
      const U = B.fromBytes(m, O), K = e.fromBytes(P);
      if (I && U.hasHighS())
        return !1;
      const { r: N, s: M } = U, F = L(A), j = a.inv(M), V = a.create(F * j), q = a.create(N * j), W = e.BASE.multiplyUnsafe(V).add(K.multiplyUnsafe(q));
      return W.is0() ? !1 : a.create(W.x) === N;
    } catch {
      return !1;
    }
  }
  function b(m, A, P = {}) {
    const { prehash: E } = va(P, w);
    return A = D(A, E), B.fromBytes(m, "recovered").recoverPublicKey(A).toBytes();
  }
  return Object.freeze({
    keygen: u,
    getPublicKey: d,
    getSharedSecret: f,
    utils: h,
    lengths: y,
    Point: e,
    sign: x,
    verify: v,
    recoverPublicKey: b,
    Signature: B,
    hash: r
  });
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const Hs = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, Db = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, zb = /* @__PURE__ */ BigInt(0), cc = /* @__PURE__ */ BigInt(2);
function Mb(e) {
  const t = Hs.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, u = l * l * e % t, d = ue(u, n, t) * u % t, f = ue(d, n, t) * u % t, h = ue(f, cc, t) * l % t, y = ue(h, i, t) * h % t, w = ue(y, o, t) * y % t, p = ue(w, a, t) * w % t, g = ue(p, c, t) * p % t, _ = ue(g, a, t) * w % t, k = ue(_, n, t) * u % t, C = ue(k, s, t) * y % t, B = ue(C, r, t) * l % t, R = ue(B, cc, t);
  if (!Go.eql(Go.sqr(R), e))
    throw new Error("Cannot find square root");
  return R;
}
const Go = Rs(Hs.p, { sqrt: Mb }), cr = /* @__PURE__ */ Lb(Hs, {
  Fp: Go,
  endo: Db
}), we = /* @__PURE__ */ Nb(cr, le), Vd = {};
function Wo(e, ...t) {
  let n = Vd[e];
  if (n === void 0) {
    const r = le(ob(e));
    n = et(r, r), Vd[e] = n;
  }
  return le(et(n, ...t));
}
const Ls = (e) => e.toBytes(!0).slice(1), Us = (e) => e % cc === zb;
function Ch(e) {
  const { Fn: t, BASE: n } = cr, r = t.fromBytes(e), i = n.multiply(r);
  return { scalar: Us(i.y) ? r : t.neg(r), bytes: Ls(i) };
}
function Rh(e) {
  const t = Go;
  if (!t.isValidNot0(e))
    throw new Error("invalid x: Fail if x ≥ p");
  const n = t.create(e * e), r = t.create(n * e + BigInt(7));
  let i = t.sqrt(r);
  Us(i) || (i = t.neg(i));
  const o = cr.fromAffine({ x: e, y: i });
  return o.assertValidity(), o;
}
const Pr = Mi;
function Hh(...e) {
  return cr.Fn.create(Pr(Wo("BIP0340/challenge", ...e)));
}
function qd(e) {
  return Ch(e).bytes;
}
function Kb(e, t, n = jc(32)) {
  const { Fn: r, BASE: i } = cr, o = oe(e, void 0, "message"), { bytes: s, scalar: a } = Ch(t), c = oe(n, 32, "auxRand"), l = r.toBytes(a ^ Pr(Wo("BIP0340/aux", c))), u = Wo("BIP0340/nonce", l, s, o), d = r.create(Pr(u));
  if (d === 0n)
    throw new Error("sign failed: k is zero");
  const f = i.multiply(d), h = Us(f.y) ? d : r.neg(d), y = Ls(f), w = Hh(y, s, o), p = new Uint8Array(64);
  if (p.set(y, 0), p.set(r.toBytes(r.create(h + w * a)), 32), !Lh(p, o, s))
    throw new Error("sign: Invalid signature produced");
  return p;
}
function Lh(e, t, n) {
  const { Fp: r, Fn: i, BASE: o } = cr, s = oe(e, 64, "signature"), a = oe(t, void 0, "message"), c = oe(n, 32, "publicKey");
  try {
    const l = Rh(Pr(c)), u = Pr(s.subarray(0, 32));
    if (!r.isValidNot0(u))
      return !1;
    const d = Pr(s.subarray(32, 64));
    if (!i.isValidNot0(d))
      return !1;
    const f = Hh(i.toBytes(u), Ls(l), a), h = o.multiplyUnsafe(d).add(l.multiplyUnsafe(i.neg(f))), { x: y, y: w } = h.toAffine();
    return !(h.is0() || !Us(w) || y !== u);
  } catch {
    return !1;
  }
}
const Sn = /* @__PURE__ */ (() => {
  const n = (r) => (r = r === void 0 ? jc(48) : r, Eh(r, Hs.n));
  return Object.freeze({
    keygen: Qc(n, qd),
    getPublicKey: qd,
    sign: Kb,
    verify: Lh,
    Point: cr,
    utils: Object.freeze({
      randomSecretKey: n,
      taggedHash: Wo,
      lift_x: Rh,
      pointToBytes: Ls
    }),
    lengths: Object.freeze({
      secretKey: 32,
      publicKey: 32,
      publicKeyHasPrefix: !1,
      signature: 64,
      seed: 48
    })
  });
})(), Fb = /* @__PURE__ */ Uint8Array.from([
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
]), Uh = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), jb = Uh.map((e) => (9 * e + 5) % 16), Nh = /* @__PURE__ */ (() => {
  const n = [[Uh], [jb]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => Fb[o]));
  return n;
})(), Dh = Nh[0], zh = Nh[1], Mh = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), Vb = /* @__PURE__ */ Dh.map((e, t) => e.map((n) => Mh[t][n])), qb = /* @__PURE__ */ zh.map((e, t) => e.map((n) => Mh[t][n])), Gb = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), Wb = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function Gd(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const ro = /* @__PURE__ */ new Uint32Array(16);
class Zb extends Vc {
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
      ro[h] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, u = l, d = this.h4 | 0, f = d;
    for (let h = 0; h < 5; h++) {
      const y = 4 - h, w = Gb[h], p = Wb[h], g = Dh[h], _ = zh[h], k = Vb[h], C = qb[h];
      for (let B = 0; B < 16; B++) {
        const R = Xi(r + Gd(h, o, a, l) + ro[g[B]] + w, k[B]) + d | 0;
        r = d, d = l, l = Xi(a, 10) | 0, a = o, o = R;
      }
      for (let B = 0; B < 16; B++) {
        const R = Xi(i + Gd(y, s, c, u) + ro[_[B]] + p, C[B]) + f | 0;
        i = f, f = u, u = Xi(c, 10) | 0, c = s, s = R;
      }
    }
    this.set(this.h1 + a + u | 0, this.h2 + l + f | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    Ht(ro);
  }
  destroy() {
    this.destroyed = !0, Ht(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const el = /* @__PURE__ */ Cs(() => new Zb()), Yb = BigInt(0), ni = BigInt(1), Xb = BigInt(2), Jb = BigInt(7), Qb = BigInt(256), ev = BigInt(113), Kh = [], Fh = [], jh = [];
for (let e = 0, t = ni, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], Kh.push(2 * (5 * r + n)), Fh.push((e + 1) * (e + 2) / 2 % 64);
  let i = Yb;
  for (let o = 0; o < 7; o++)
    t = (t << ni ^ (t >> Jb) * ev) % Qb, t & Xb && (i ^= ni << (ni << BigInt(o)) - ni);
  jh.push(i);
}
const Vh = ah(jh, !0), tv = Vh[0], nv = Vh[1], Wd = (e, t, n) => n > 32 ? F0(e, t, n) : M0(e, t, n), Zd = (e, t, n) => n > 32 ? j0(e, t, n) : K0(e, t, n);
function rv(e, t = 24) {
  if (Ct(t, "rounds"), t < 1 || t > 24)
    throw new Error('"rounds" expected integer 1..24');
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], u = n[c + 1], d = Wd(l, u, 1) ^ n[a], f = Zd(l, u, 1) ^ n[a + 1];
      for (let h = 0; h < 50; h += 10)
        e[s + h] ^= d, e[s + h + 1] ^= f;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = Fh[s], c = Wd(i, o, a), l = Zd(i, o, a), u = Kh[s];
      i = e[u], o = e[u + 1], e[u] = c, e[u + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      const a = e[s], c = e[s + 1], l = e[s + 2], u = e[s + 3];
      e[s] ^= ~e[s + 2] & e[s + 4], e[s + 1] ^= ~e[s + 3] & e[s + 5], e[s + 2] ^= ~e[s + 4] & e[s + 6], e[s + 3] ^= ~e[s + 5] & e[s + 7], e[s + 4] ^= ~e[s + 6] & e[s + 8], e[s + 5] ^= ~e[s + 7] & e[s + 9], e[s + 6] ^= ~e[s + 8] & a, e[s + 7] ^= ~e[s + 9] & c, e[s + 8] ^= ~a & l, e[s + 9] ^= ~c & u;
    }
    e[0] ^= tv[r], e[1] ^= nv[r];
  }
  Ht(n);
}
let iv = class qh {
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
    if (this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.canXOF = i, this.rounds = o, Ct(r, "outputLen"), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = O0(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    Bd(this.state32), rv(this.state32, this.rounds), Bd(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Ur(this), Rt(t);
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
    Ur(this, !1), Rt(t), this.finish();
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
    return Ct(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (Mc(t, this), this.finished)
      throw new Error("digest() was already called");
    this.writeInto(t.subarray(0, this.outputLen)), this.destroy();
  }
  digest() {
    const t = new Uint8Array(this.outputLen);
    return this.digestInto(t), t;
  }
  destroy() {
    this.destroyed = !0, Ht(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t ||= new qh(n, r, i, s, o), t.blockLen = n, t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.canXOF = this.canXOF, t.destroyed = this.destroyed, t;
  }
};
const ov = (e, t, n, r = {}) => Cs(() => new iv(t, e, n), r), Zo = /* @__PURE__ */ ov(1, 136, 32), sv = 1024 * 1024, Gh = Object.getPrototypeOf(Uint8Array.prototype), av = Object.getOwnPropertyDescriptor(Gh, Symbol.toStringTag).get, cv = Object.getOwnPropertyDescriptor(Gh, "byteLength").get, lv = Uint8Array.prototype.set;
function Wh(e) {
  if (!Number.isSafeInteger(e) || e < 0) throw new Error("exact message byte length is invalid");
  if (e > sv) throw new Error("exact message exceeds the 1 MiB decoded limit");
}
function Zh(e) {
  const t = e.startsWith("0x") ? 2 : 0;
  Wh(Math.ceil((e.length - t) / 2));
}
function tl(e) {
  if (typeof e == "string")
    return Zh(e), lt(e.startsWith("0x") ? e.slice(2) : e);
  if (av.call(e) !== "Uint8Array") throw new TypeError("exact message must be hex or Uint8Array");
  const t = cv.call(e);
  Wh(t);
  const n = new Uint8Array(t);
  return lv.call(n, e), n;
}
const mr = typeof globalThis == "object" && "crypto" in globalThis ? globalThis.crypto : void 0;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Ns(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function lc(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function qe(e, ...t) {
  if (!Ns(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function Yh(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash should be wrapped by utils.createHasher");
  lc(e.outputLen), lc(e.blockLen);
}
function Yo(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function dv(e, t) {
  qe(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function Ln(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Or(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function xt(e, t) {
  return e << 32 - t | e >>> t;
}
function io(e, t) {
  return e << t | e >>> 32 - t >>> 0;
}
const Xh = /* @ts-ignore */ typeof Uint8Array.from([]).toHex == "function" && typeof Uint8Array.fromHex == "function", uv = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function tr(e) {
  if (qe(e), Xh)
    return e.toHex();
  let t = "";
  for (let n = 0; n < e.length; n++)
    t += uv[e[n]];
  return t;
}
const Kt = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
function Yd(e) {
  if (e >= Kt._0 && e <= Kt._9)
    return e - Kt._0;
  if (e >= Kt.A && e <= Kt.F)
    return e - (Kt.A - 10);
  if (e >= Kt.a && e <= Kt.f)
    return e - (Kt.a - 10);
}
function _i(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  if (Xh)
    return Uint8Array.fromHex(e);
  const t = e.length, n = t / 2;
  if (t % 2)
    throw new Error("hex string expected, got unpadded hex of length " + t);
  const r = new Uint8Array(n);
  for (let i = 0, o = 0; i < n; i++, o += 2) {
    const s = Yd(e.charCodeAt(o)), a = Yd(e.charCodeAt(o + 1));
    if (s === void 0 || a === void 0) {
      const c = e[o] + e[o + 1];
      throw new Error('hex string expected, got non-hex character "' + c + '" at index ' + o);
    }
    r[i] = s * 16 + a;
  }
  return r;
}
function Jh(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function nl(e) {
  return typeof e == "string" && (e = Jh(e)), qe(e), e;
}
function Qe(...e) {
  let t = 0;
  for (let r = 0; r < e.length; r++) {
    const i = e[r];
    qe(i), t += i.length;
  }
  const n = new Uint8Array(t);
  for (let r = 0, i = 0; r < e.length; r++) {
    const o = e[r];
    n.set(o, i), i += o.length;
  }
  return n;
}
let Qh = class {
};
function rl(e) {
  const t = (r) => e().update(nl(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
function ep(e = 32) {
  if (mr && typeof mr.getRandomValues == "function")
    return mr.getRandomValues(new Uint8Array(e));
  if (mr && typeof mr.randomBytes == "function")
    return Uint8Array.from(mr.randomBytes(e));
  throw new Error("crypto.getRandomValues must be defined");
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const il = /* @__PURE__ */ BigInt(0), dc = /* @__PURE__ */ BigInt(1);
function Xo(e, t = "") {
  if (typeof e != "boolean") {
    const n = t && `"${t}"`;
    throw new Error(n + "expected boolean, got type=" + typeof e);
  }
  return e;
}
function Gn(e, t, n = "") {
  const r = Ns(e), i = e?.length, o = t !== void 0;
  if (!r || o && i !== t) {
    const s = n && `"${n}" `, a = o ? ` of length ${t}` : "", c = r ? `length=${i}` : `type=${typeof e}`;
    throw new Error(s + "expected Uint8Array" + a + ", got " + c);
  }
  return e;
}
function oo(e) {
  const t = e.toString(16);
  return t.length & 1 ? "0" + t : t;
}
function tp(e) {
  if (typeof e != "string")
    throw new Error("hex string expected, got " + typeof e);
  return e === "" ? il : BigInt("0x" + e);
}
function Ds(e) {
  return tp(tr(e));
}
function np(e) {
  return qe(e), tp(tr(Uint8Array.from(e).reverse()));
}
function ol(e, t) {
  return _i(e.toString(16).padStart(t * 2, "0"));
}
function rp(e, t) {
  return ol(e, t).reverse();
}
function Me(e, t, n) {
  let r;
  if (typeof t == "string")
    try {
      r = _i(t);
    } catch (i) {
      throw new Error(e + " must be hex string or Uint8Array, cause: " + i);
    }
  else if (Ns(t))
    r = Uint8Array.from(t);
  else
    throw new Error(e + " must be hex string or Uint8Array");
  return r.length, r;
}
const xa = (e) => typeof e == "bigint" && il <= e;
function fv(e, t, n) {
  return xa(e) && xa(t) && xa(n) && t <= e && e < n;
}
function hv(e, t, n, r) {
  if (!fv(t, n, r))
    throw new Error("expected valid " + e + ": " + n + " <= n < " + r + ", got " + t);
}
function ip(e) {
  let t;
  for (t = 0; e > il; e >>= dc, t += 1)
    ;
  return t;
}
const Ki = (e) => (dc << BigInt(e)) - dc;
function pv(e, t, n) {
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
    const y = [];
    for (; h < t; ) {
      o = l();
      const w = o.slice();
      y.push(w), h += o.length;
    }
    return Qe(...y);
  };
  return (h, y) => {
    c(), u(h);
    let w;
    for (; !(w = y(d())); )
      u();
    return c(), w;
  };
}
function sl(e, t, n = {}) {
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
function Xd(e) {
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
const Ke = BigInt(0), De = BigInt(1), Wn = /* @__PURE__ */ BigInt(2), op = /* @__PURE__ */ BigInt(3), sp = /* @__PURE__ */ BigInt(4), ap = /* @__PURE__ */ BigInt(5), yv = /* @__PURE__ */ BigInt(7), cp = /* @__PURE__ */ BigInt(8), wv = /* @__PURE__ */ BigInt(9), lp = /* @__PURE__ */ BigInt(16);
function Je(e, t) {
  const n = e % t;
  return n >= Ke ? n : t + n;
}
function Ze(e, t, n) {
  let r = e;
  for (; t-- > Ke; )
    r *= r, r %= n;
  return r;
}
function Jd(e, t) {
  if (e === Ke)
    throw new Error("invert: expected non-zero number");
  if (t <= Ke)
    throw new Error("invert: expected positive modulus, got " + t);
  let n = Je(e, t), r = t, i = Ke, o = De;
  for (; n !== Ke; ) {
    const a = r / n, c = r % n, l = i - o * a;
    r = n, n = c, i = o, o = l;
  }
  if (r !== De)
    throw new Error("invert: does not exist");
  return Je(i, t);
}
function al(e, t, n) {
  if (!e.eql(e.sqr(t), n))
    throw new Error("Cannot find square root");
}
function dp(e, t) {
  const n = (e.ORDER + De) / sp, r = e.pow(t, n);
  return al(e, r, t), r;
}
function gv(e, t) {
  const n = (e.ORDER - ap) / cp, r = e.mul(t, Wn), i = e.pow(r, n), o = e.mul(t, i), s = e.mul(e.mul(o, Wn), i), a = e.mul(o, e.sub(s, e.ONE));
  return al(e, a, t), a;
}
function mv(e) {
  const t = Fi(e), n = up(e), r = n(t, t.neg(t.ONE)), i = n(t, r), o = n(t, t.neg(r)), s = (e + yv) / lp;
  return (a, c) => {
    let l = a.pow(c, s), u = a.mul(l, r);
    const d = a.mul(l, i), f = a.mul(l, o), h = a.eql(a.sqr(u), c), y = a.eql(a.sqr(d), c);
    l = a.cmov(l, u, h), u = a.cmov(f, d, y);
    const w = a.eql(a.sqr(u), c), p = a.cmov(l, u, w);
    return al(a, p, c), p;
  };
}
function up(e) {
  if (e < op)
    throw new Error("sqrt is not defined for small field");
  let t = e - De, n = 0;
  for (; t % Wn === Ke; )
    t /= Wn, n++;
  let r = Wn;
  const i = Fi(e);
  for (; Qd(i, r) === 1; )
    if (r++ > 1e3)
      throw new Error("Cannot find square root: probably non-prime P");
  if (n === 1)
    return dp;
  let o = i.pow(r, t);
  const s = (t + De) / Wn;
  return function(c, l) {
    if (c.is0(l))
      return l;
    if (Qd(c, l) !== 1)
      throw new Error("Cannot find square root");
    let u = n, d = c.mul(c.ONE, o), f = c.pow(l, t), h = c.pow(l, s);
    for (; !c.eql(f, c.ONE); ) {
      if (c.is0(f))
        return c.ZERO;
      let y = 1, w = c.sqr(f);
      for (; !c.eql(w, c.ONE); )
        if (y++, w = c.sqr(w), y === u)
          throw new Error("Cannot find square root");
      const p = De << BigInt(u - y - 1), g = c.pow(d, p);
      u = y, d = c.sqr(g), f = c.mul(f, d), h = c.mul(h, g);
    }
    return h;
  };
}
function bv(e) {
  return e % sp === op ? dp : e % cp === ap ? gv : e % lp === wv ? mv(e) : up(e);
}
const vv = [
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
function xv(e) {
  const t = {
    ORDER: "bigint",
    MASK: "bigint",
    BYTES: "number",
    BITS: "number"
  }, n = vv.reduce((r, i) => (r[i] = "function", r), t);
  return sl(e, n), e;
}
function kv(e, t, n) {
  if (n < Ke)
    throw new Error("invalid exponent, negatives unsupported");
  if (n === Ke)
    return e.ONE;
  if (n === De)
    return t;
  let r = e.ONE, i = t;
  for (; n > Ke; )
    n & De && (r = e.mul(r, i)), i = e.sqr(i), n >>= De;
  return r;
}
function fp(e, t, n = !1) {
  const r = new Array(t.length).fill(n ? e.ZERO : void 0), i = t.reduce((s, a, c) => e.is0(a) ? s : (r[c] = s, e.mul(s, a)), e.ONE), o = e.inv(i);
  return t.reduceRight((s, a, c) => e.is0(a) ? s : (r[c] = e.mul(s, r[c]), e.mul(s, a)), o), r;
}
function Qd(e, t) {
  const n = (e.ORDER - De) / Wn, r = e.pow(t, n), i = e.eql(r, e.ONE), o = e.eql(r, e.ZERO), s = e.eql(r, e.neg(e.ONE));
  if (!i && !o && !s)
    throw new Error("invalid Legendre symbol result");
  return i ? 1 : o ? 0 : -1;
}
function hp(e, t) {
  t !== void 0 && lc(t);
  const n = t !== void 0 ? t : e.toString(2).length, r = Math.ceil(n / 8);
  return { nBitLength: n, nByteLength: r };
}
function Fi(e, t, n = !1, r = {}) {
  if (e <= Ke)
    throw new Error("invalid field: expected ORDER > 0, got " + e);
  let i, o, s = !1, a;
  if (typeof t == "object" && t != null) {
    if (r.sqrt || n)
      throw new Error("cannot specify opts in two arguments");
    const f = t;
    f.BITS && (i = f.BITS), f.sqrt && (o = f.sqrt), typeof f.isLE == "boolean" && (n = f.isLE), typeof f.modFromBytes == "boolean" && (s = f.modFromBytes), a = f.allowedLengths;
  } else
    typeof t == "number" && (i = t), r.sqrt && (o = r.sqrt);
  const { nBitLength: c, nByteLength: l } = hp(e, i);
  if (l > 2048)
    throw new Error("invalid field: expected ORDER of <= 2048 bytes");
  let u;
  const d = Object.freeze({
    ORDER: e,
    isLE: n,
    BITS: c,
    BYTES: l,
    MASK: Ki(c),
    ZERO: Ke,
    ONE: De,
    allowedLengths: a,
    create: (f) => Je(f, e),
    isValid: (f) => {
      if (typeof f != "bigint")
        throw new Error("invalid field element: expected bigint, got " + typeof f);
      return Ke <= f && f < e;
    },
    is0: (f) => f === Ke,
    // is valid and invertible
    isValidNot0: (f) => !d.is0(f) && d.isValid(f),
    isOdd: (f) => (f & De) === De,
    neg: (f) => Je(-f, e),
    eql: (f, h) => f === h,
    sqr: (f) => Je(f * f, e),
    add: (f, h) => Je(f + h, e),
    sub: (f, h) => Je(f - h, e),
    mul: (f, h) => Je(f * h, e),
    pow: (f, h) => kv(d, f, h),
    div: (f, h) => Je(f * Jd(h, e), e),
    // Same as above, but doesn't normalize
    sqrN: (f) => f * f,
    addN: (f, h) => f + h,
    subN: (f, h) => f - h,
    mulN: (f, h) => f * h,
    inv: (f) => Jd(f, e),
    sqrt: o || ((f) => (u || (u = bv(e)), u(d, f))),
    toBytes: (f) => n ? rp(f, l) : ol(f, l),
    fromBytes: (f, h = !0) => {
      if (a) {
        if (!a.includes(f.length) || f.length > l)
          throw new Error("Field.fromBytes: expected " + a + " bytes, got " + f.length);
        const w = new Uint8Array(l);
        w.set(f, n ? 0 : w.length - f.length), f = w;
      }
      if (f.length !== l)
        throw new Error("Field.fromBytes: expected " + l + " bytes, got " + f.length);
      let y = n ? np(f) : Ds(f);
      if (s && (y = Je(y, e)), !h && !d.isValid(y))
        throw new Error("invalid field element: outside of range 0..ORDER");
      return y;
    },
    // TODO: we don't need it here, move out to separate fn
    invertBatch: (f) => fp(d, f),
    // We can't move this out because Fp6, Fp12 implement it
    // and it's unclear what to return in there.
    cmov: (f, h, y) => y ? h : f
  });
  return Object.freeze(d);
}
function pp(e) {
  if (typeof e != "bigint")
    throw new Error("field order must be bigint");
  const t = e.toString(2).length;
  return Math.ceil(t / 8);
}
function yp(e) {
  const t = pp(e);
  return t + Math.ceil(t / 2);
}
function Ev(e, t, n = !1) {
  const r = e.length, i = pp(t), o = yp(t);
  if (r < 16 || r < o || r > 1024)
    throw new Error("expected " + o + "-1024 bytes of input, got " + r);
  const s = n ? np(e) : Ds(e), a = Je(s, t - De) + De;
  return n ? rp(a, i) : ol(a, i);
}
function Av(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function Sv(e, t, n) {
  return e & t ^ ~e & n;
}
function _v(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let cl = class extends Qh {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Or(this.buffer);
  }
  update(t) {
    Yo(this), t = nl(t), qe(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Or(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Yo(this), dv(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Ln(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    Av(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Or(t), c = this.outputLen;
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
const yn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Pe = /* @__PURE__ */ Uint32Array.from([
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
]), so = /* @__PURE__ */ BigInt(2 ** 32 - 1), eu = /* @__PURE__ */ BigInt(32);
function Iv(e, t = !1) {
  return t ? { h: Number(e & so), l: Number(e >> eu & so) } : { h: Number(e >> eu & so) | 0, l: Number(e & so) | 0 };
}
function Bv(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = Iv(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const tu = (e, t, n) => e >>> n, nu = (e, t, n) => e << 32 - n | t >>> n, br = (e, t, n) => e >>> n | t << 32 - n, vr = (e, t, n) => e << 32 - n | t >>> n, ao = (e, t, n) => e << 64 - n | t >>> n - 32, co = (e, t, n) => e >>> n - 32 | t << 64 - n;
function Ft(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const $v = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), Tv = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, Pv = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), Ov = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, Cv = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), Rv = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, Hv = /* @__PURE__ */ Uint32Array.from([
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
let Lv = class extends cl {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = yn[0] | 0, this.B = yn[1] | 0, this.C = yn[2] | 0, this.D = yn[3] | 0, this.E = yn[4] | 0, this.F = yn[5] | 0, this.G = yn[6] | 0, this.H = yn[7] | 0;
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
      wn[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = wn[d - 15], h = wn[d - 2], y = xt(f, 7) ^ xt(f, 18) ^ f >>> 3, w = xt(h, 17) ^ xt(h, 19) ^ h >>> 10;
      wn[d] = w + wn[d - 7] + y + wn[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = xt(a, 6) ^ xt(a, 11) ^ xt(a, 25), h = u + f + Sv(a, c, l) + Hv[d] + wn[d] | 0, w = (xt(r, 2) ^ xt(r, 13) ^ xt(r, 22)) + _v(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    Ln(wn);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Ln(this.buffer);
  }
};
const wp = Bv([
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
].map((e) => BigInt(e))), Uv = wp[0], Nv = wp[1], gn = /* @__PURE__ */ new Uint32Array(80), mn = /* @__PURE__ */ new Uint32Array(80);
let Dv = class extends cl {
  constructor(t = 64) {
    super(128, t, 16, !1), this.Ah = Pe[0] | 0, this.Al = Pe[1] | 0, this.Bh = Pe[2] | 0, this.Bl = Pe[3] | 0, this.Ch = Pe[4] | 0, this.Cl = Pe[5] | 0, this.Dh = Pe[6] | 0, this.Dl = Pe[7] | 0, this.Eh = Pe[8] | 0, this.El = Pe[9] | 0, this.Fh = Pe[10] | 0, this.Fl = Pe[11] | 0, this.Gh = Pe[12] | 0, this.Gl = Pe[13] | 0, this.Hh = Pe[14] | 0, this.Hl = Pe[15] | 0;
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: f, Gh: h, Gl: y, Hh: w, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, f, h, y, w, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, f, h, y, w, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = y | 0, this.Hh = w | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let k = 0; k < 16; k++, n += 4)
      gn[k] = t.getUint32(n), mn[k] = t.getUint32(n += 4);
    for (let k = 16; k < 80; k++) {
      const C = gn[k - 15] | 0, B = mn[k - 15] | 0, R = br(C, B, 1) ^ br(C, B, 8) ^ tu(C, B, 7), L = vr(C, B, 1) ^ vr(C, B, 8) ^ nu(C, B, 7), H = gn[k - 2] | 0, $ = mn[k - 2] | 0, D = br(H, $, 19) ^ ao(H, $, 61) ^ tu(H, $, 6), z = vr(H, $, 19) ^ co(H, $, 61) ^ nu(H, $, 6), x = Pv(L, z, mn[k - 7], mn[k - 16]), v = Ov(x, R, D, gn[k - 7], gn[k - 16]);
      gn[k] = v | 0, mn[k] = x | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: f, Fh: h, Fl: y, Gh: w, Gl: p, Hh: g, Hl: _ } = this;
    for (let k = 0; k < 80; k++) {
      const C = br(d, f, 14) ^ br(d, f, 18) ^ ao(d, f, 41), B = vr(d, f, 14) ^ vr(d, f, 18) ^ co(d, f, 41), R = d & h ^ ~d & w, L = f & y ^ ~f & p, H = Cv(_, B, L, Nv[k], mn[k]), $ = Rv(H, g, C, R, Uv[k], gn[k]), D = H | 0, z = br(r, i, 28) ^ ao(r, i, 34) ^ ao(r, i, 39), x = vr(r, i, 28) ^ co(r, i, 34) ^ co(r, i, 39), v = r & o ^ r & a ^ o & a, b = i & s ^ i & c ^ s & c;
      g = w | 0, _ = p | 0, w = h | 0, p = y | 0, h = d | 0, y = f | 0, { h: d, l: f } = Ft(l | 0, u | 0, $ | 0, D | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const m = $v(D, x, b);
      r = Tv(m, $, z, v), i = m | 0;
    }
    ({ h: r, l: i } = Ft(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = Ft(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = Ft(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = Ft(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: f } = Ft(this.Eh | 0, this.El | 0, d | 0, f | 0), { h, l: y } = Ft(this.Fh | 0, this.Fl | 0, h | 0, y | 0), { h: w, l: p } = Ft(this.Gh | 0, this.Gl | 0, w | 0, p | 0), { h: g, l: _ } = Ft(this.Hh | 0, this.Hl | 0, g | 0, _ | 0), this.set(r, i, o, s, a, c, l, u, d, f, h, y, w, p, g, _);
  }
  roundClean() {
    Ln(gn, mn);
  }
  destroy() {
    Ln(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
};
const ll = /* @__PURE__ */ rl(() => new Lv()), ru = /* @__PURE__ */ rl(() => new Dv());
let gp = class extends Qh {
  constructor(t, n) {
    super(), this.finished = !1, this.destroyed = !1, Yh(t);
    const r = nl(n);
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
    this.oHash.update(o), Ln(o);
  }
  update(t) {
    return Yo(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    Yo(this), qe(t, this.outputLen), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const Jo = (e, t, n) => new gp(e, t).update(n).digest();
Jo.create = (e, t) => new gp(e, t);
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const zr = BigInt(0), Zn = BigInt(1);
function Qo(e, t) {
  const n = t.negate();
  return e ? n : t;
}
function ka(e, t) {
  const n = fp(e.Fp, t.map((r) => r.Z));
  return t.map((r, i) => e.fromAffine(r.toAffine(n[i])));
}
function mp(e, t) {
  if (!Number.isSafeInteger(e) || e <= 0 || e > t)
    throw new Error("invalid window size, expected [1.." + t + "], got W=" + e);
}
function Ea(e, t) {
  mp(e, t);
  const n = Math.ceil(t / e) + 1, r = 2 ** (e - 1), i = 2 ** e, o = Ki(e), s = BigInt(e);
  return { windows: n, windowSize: r, mask: o, maxNumber: i, shiftBy: s };
}
function iu(e, t, n) {
  const { windowSize: r, mask: i, maxNumber: o, shiftBy: s } = n;
  let a = Number(e & i), c = e >> s;
  a > r && (a -= o, c += Zn);
  const l = t * r, u = l + Math.abs(a) - 1, d = a === 0, f = a < 0, h = t % 2 !== 0;
  return { nextN: c, offset: u, isZero: d, isNeg: f, isNegF: h, offsetF: l };
}
function zv(e, t) {
  if (!Array.isArray(e))
    throw new Error("array expected");
  e.forEach((n, r) => {
    if (!(n instanceof t))
      throw new Error("invalid point at index " + r);
  });
}
function Mv(e, t) {
  if (!Array.isArray(e))
    throw new Error("array of scalars expected");
  e.forEach((n, r) => {
    if (!t.isValid(n))
      throw new Error("invalid scalar at index " + r);
  });
}
const Aa = /* @__PURE__ */ new WeakMap(), bp = /* @__PURE__ */ new WeakMap();
function Sa(e) {
  return bp.get(e) || 1;
}
function ou(e) {
  if (e !== zr)
    throw new Error("invalid wNAF");
}
class Kv {
  // Parametrized with a given Point class (not individual point)
  constructor(t, n) {
    this.BASE = t.BASE, this.ZERO = t.ZERO, this.Fn = t.Fn, this.bits = n;
  }
  // non-const time multiplication ladder
  _unsafeLadder(t, n, r = this.ZERO) {
    let i = t;
    for (; n > zr; )
      n & Zn && (r = r.add(i)), i = i.double(), n >>= Zn;
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
    const { windows: r, windowSize: i } = Ea(n, this.bits), o = [];
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
    const s = Ea(t, this.bits);
    for (let a = 0; a < s.windows; a++) {
      const { nextN: c, offset: l, isZero: u, isNeg: d, isNegF: f, offsetF: h } = iu(r, a, s);
      r = c, u ? o = o.add(Qo(f, n[h])) : i = i.add(Qo(d, n[l]));
    }
    return ou(r), { p: i, f: o };
  }
  /**
   * Implements ec unsafe (non const-time) multiplication using precomputed tables and w-ary non-adjacent form.
   * @param acc accumulator point to add result of multiplication
   * @returns point
   */
  wNAFUnsafe(t, n, r, i = this.ZERO) {
    const o = Ea(t, this.bits);
    for (let s = 0; s < o.windows && r !== zr; s++) {
      const { nextN: a, offset: c, isZero: l, isNeg: u } = iu(r, s, o);
      if (r = a, !l) {
        const d = n[c];
        i = i.add(u ? d.negate() : d);
      }
    }
    return ou(r), i;
  }
  getPrecomputes(t, n, r) {
    let i = Aa.get(n);
    return i || (i = this.precomputeWindow(n, t), t !== 1 && (typeof r == "function" && (i = r(i)), Aa.set(n, i))), i;
  }
  cached(t, n, r) {
    const i = Sa(t);
    return this.wNAF(i, this.getPrecomputes(i, t, r), n);
  }
  unsafe(t, n, r, i) {
    const o = Sa(t);
    return o === 1 ? this._unsafeLadder(t, n, i) : this.wNAFUnsafe(o, this.getPrecomputes(o, t, r), n, i);
  }
  // We calculate precomputes for elliptic curve point multiplication
  // using windowed method. This specifies window size and
  // stores precomputed values. Usually only base point would be precomputed.
  createCache(t, n) {
    mp(n, this.bits), bp.set(t, n), Aa.delete(t);
  }
  hasCache(t) {
    return Sa(t) !== 1;
  }
}
function Fv(e, t, n, r) {
  let i = t, o = e.ZERO, s = e.ZERO;
  for (; n > zr || r > zr; )
    n & Zn && (o = o.add(i)), r & Zn && (s = s.add(i)), i = i.double(), n >>= Zn, r >>= Zn;
  return { p1: o, p2: s };
}
function jv(e, t, n, r) {
  zv(n, e), Mv(r, t);
  const i = n.length, o = r.length;
  if (i !== o)
    throw new Error("arrays of points and scalars must have equal length");
  const s = e.ZERO, a = ip(BigInt(i));
  let c = 1;
  a > 12 ? c = a - 3 : a > 4 ? c = a - 2 : a > 0 && (c = 2);
  const l = Ki(c), u = new Array(Number(l) + 1).fill(s), d = Math.floor((t.BITS - 1) / c) * c;
  let f = s;
  for (let h = d; h >= 0; h -= c) {
    u.fill(s);
    for (let w = 0; w < o; w++) {
      const p = r[w], g = Number(p >> BigInt(h) & l);
      u[g] = u[g].add(n[w]);
    }
    let y = s;
    for (let w = u.length - 1, p = s; w > 0; w--)
      p = p.add(u[w]), y = y.add(p);
    if (f = f.add(y), h !== 0)
      for (let w = 0; w < c; w++)
        f = f.double();
  }
  return f;
}
function su(e, t, n) {
  if (t) {
    if (t.ORDER !== e)
      throw new Error("Field.ORDER must match order: Fp == p, Fn == n");
    return xv(t), t;
  } else
    return Fi(e, { isLE: n });
}
function Vv(e, t, n = {}, r) {
  if (r === void 0 && (r = e === "edwards"), !t || typeof t != "object")
    throw new Error(`expected valid ${e} CURVE object`);
  for (const c of ["p", "n", "h"]) {
    const l = t[c];
    if (!(typeof l == "bigint" && l > zr))
      throw new Error(`CURVE.${c} must be positive bigint`);
  }
  const i = su(t.p, n.Fp, r), o = su(t.n, n.Fn, r), a = ["Gx", "Gy", "a", "b"];
  for (const c of a)
    if (!i.isValid(t[c]))
      throw new Error(`CURVE.${c} must be valid field element of CURVE.Fp`);
  return t = Object.freeze(Object.assign({}, t)), { CURVE: t, Fp: i, Fn: o };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const au = (e, t) => (e + (e >= 0 ? t : -t) / vp) / t;
function qv(e, t, n) {
  const [[r, i], [o, s]] = t, a = au(s * e, n), c = au(-i * e, n);
  let l = e - a * r - c * o, u = -a * i - c * s;
  const d = l < Yt, f = u < Yt;
  d && (l = -l), f && (u = -u);
  const h = Ki(Math.ceil(ip(n) / 2)) + Cr;
  if (l < Yt || l >= h || u < Yt || u >= h)
    throw new Error("splitScalar (endomorphism): failed, k=" + e);
  return { k1neg: d, k1: l, k2neg: f, k2: u };
}
function uc(e) {
  if (!["compact", "recovered", "der"].includes(e))
    throw new Error('Signature format must be "compact", "recovered", or "der"');
  return e;
}
function _a(e, t) {
  const n = {};
  for (let r of Object.keys(t))
    n[r] = e[r] === void 0 ? t[r] : e[r];
  return Xo(n.lowS, "lowS"), Xo(n.prehash, "prehash"), n.format !== void 0 && uc(n.format), n;
}
class Gv extends Error {
  constructor(t = "") {
    super(t);
  }
}
const Wt = {
  // asn.1 DER encoding utils
  Err: Gv,
  // Basic building block is TLV (Tag-Length-Value)
  _tlv: {
    encode: (e, t) => {
      const { Err: n } = Wt;
      if (e < 0 || e > 256)
        throw new n("tlv.encode: wrong tag");
      if (t.length & 1)
        throw new n("tlv.encode: unpadded data");
      const r = t.length / 2, i = oo(r);
      if (i.length / 2 & 128)
        throw new n("tlv.encode: long form length too big");
      const o = r > 127 ? oo(i.length / 2 | 128) : "";
      return oo(e) + o + i + t;
    },
    // v - value, l - left bytes (unparsed)
    decode(e, t) {
      const { Err: n } = Wt;
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
      const { Err: t } = Wt;
      if (e < Yt)
        throw new t("integer: negative integers are not allowed");
      let n = oo(e);
      if (Number.parseInt(n[0], 16) & 8 && (n = "00" + n), n.length & 1)
        throw new t("unexpected DER parsing assertion: unpadded hex");
      return n;
    },
    decode(e) {
      const { Err: t } = Wt;
      if (e[0] & 128)
        throw new t("invalid signature integer: negative");
      if (e[0] === 0 && !(e[1] & 128))
        throw new t("invalid signature integer: unnecessary leading zero");
      return Ds(e);
    }
  },
  toSig(e) {
    const { Err: t, _int: n, _tlv: r } = Wt, i = Me("signature", e), { v: o, l: s } = r.decode(48, i);
    if (s.length)
      throw new t("invalid signature: left bytes after parsing");
    const { v: a, l: c } = r.decode(2, o), { v: l, l: u } = r.decode(2, c);
    if (u.length)
      throw new t("invalid signature: left bytes after parsing");
    return { r: n.decode(a), s: n.decode(l) };
  },
  hexFromSig(e) {
    const { _tlv: t, _int: n } = Wt, r = t.encode(2, n.encode(e.r)), i = t.encode(2, n.encode(e.s)), o = r + i;
    return t.encode(48, o);
  }
}, Yt = BigInt(0), Cr = BigInt(1), vp = BigInt(2), lo = BigInt(3), Wv = BigInt(4);
function $r(e, t) {
  const { BYTES: n } = e;
  let r;
  if (typeof t == "bigint")
    r = t;
  else {
    let i = Me("private key", t);
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
function Zv(e, t = {}) {
  const n = Vv("weierstrass", e, t), { Fp: r, Fn: i } = n;
  let o = n.CURVE;
  const { h: s, n: a } = o;
  sl(t, {}, {
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
  const l = kp(r, i);
  function u() {
    if (!r.isOdd)
      throw new Error("compression is not supported: Field does not have .isOdd()");
  }
  function d(x, v, b) {
    const { x: m, y: A } = v.toAffine(), P = r.toBytes(m);
    if (Xo(b, "isCompressed"), b) {
      u();
      const E = !r.isOdd(A);
      return Qe(xp(E), P);
    } else
      return Qe(Uint8Array.of(4), P, r.toBytes(A));
  }
  function f(x) {
    Gn(x, void 0, "Point");
    const { publicKey: v, publicKeyUncompressed: b } = l, m = x.length, A = x[0], P = x.subarray(1);
    if (m === v && (A === 2 || A === 3)) {
      const E = r.fromBytes(P);
      if (!r.isValid(E))
        throw new Error("bad point: is not on curve, wrong x");
      const I = w(E);
      let T;
      try {
        T = r.sqrt(I);
      } catch (K) {
        const N = K instanceof Error ? ": " + K.message : "";
        throw new Error("bad point: is not on curve, sqrt error" + N);
      }
      u();
      const O = r.isOdd(T);
      return (A & 1) === 1 !== O && (T = r.neg(T)), { x: E, y: T };
    } else if (m === b && A === 4) {
      const E = r.BYTES, I = r.fromBytes(P.subarray(0, E)), T = r.fromBytes(P.subarray(E, E * 2));
      if (!p(I, T))
        throw new Error("bad point: is not on curve");
      return { x: I, y: T };
    } else
      throw new Error(`bad point: got length ${m}, expected compressed=${v} or uncompressed=${b}`);
  }
  const h = t.toBytes || d, y = t.fromBytes || f;
  function w(x) {
    const v = r.sqr(x), b = r.mul(v, x);
    return r.add(r.add(b, r.mul(x, o.a)), o.b);
  }
  function p(x, v) {
    const b = r.sqr(v), m = w(x);
    return r.eql(b, m);
  }
  if (!p(o.Gx, o.Gy))
    throw new Error("bad curve params: generator point");
  const g = r.mul(r.pow(o.a, lo), Wv), _ = r.mul(r.sqr(o.b), BigInt(27));
  if (r.is0(r.add(g, _)))
    throw new Error("bad curve params: a or b");
  function k(x, v, b = !1) {
    if (!r.isValid(v) || b && r.is0(v))
      throw new Error(`bad point coordinate ${x}`);
    return v;
  }
  function C(x) {
    if (!(x instanceof $))
      throw new Error("ProjectivePoint expected");
  }
  function B(x) {
    if (!c || !c.basises)
      throw new Error("no endo");
    return qv(x, c.basises, i.ORDER);
  }
  const R = Xd((x, v) => {
    const { X: b, Y: m, Z: A } = x;
    if (r.eql(A, r.ONE))
      return { x: b, y: m };
    const P = x.is0();
    v == null && (v = P ? r.ONE : r.inv(A));
    const E = r.mul(b, v), I = r.mul(m, v), T = r.mul(A, v);
    if (P)
      return { x: r.ZERO, y: r.ZERO };
    if (!r.eql(T, r.ONE))
      throw new Error("invZ was invalid");
    return { x: E, y: I };
  }), L = Xd((x) => {
    if (x.is0()) {
      if (t.allowInfinityPoint && !r.is0(x.Y))
        return;
      throw new Error("bad point: ZERO");
    }
    const { x: v, y: b } = x.toAffine();
    if (!r.isValid(v) || !r.isValid(b))
      throw new Error("bad point: x or y not field elements");
    if (!p(v, b))
      throw new Error("bad point: equation left != right");
    if (!x.isTorsionFree())
      throw new Error("bad point: not in prime-order subgroup");
    return !0;
  });
  function H(x, v, b, m, A) {
    return b = new $(r.mul(b.X, x), b.Y, b.Z), v = Qo(m, v), b = Qo(A, b), v.add(b);
  }
  class $ {
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    constructor(v, b, m) {
      this.X = k("x", v), this.Y = k("y", b, !0), this.Z = k("z", m), Object.freeze(this);
    }
    static CURVE() {
      return o;
    }
    /** Does NOT validate if the point is valid. Use `.assertValidity()`. */
    static fromAffine(v) {
      const { x: b, y: m } = v || {};
      if (!v || !r.isValid(b) || !r.isValid(m))
        throw new Error("invalid affine point");
      if (v instanceof $)
        throw new Error("projective point not allowed");
      return r.is0(b) && r.is0(m) ? $.ZERO : new $(b, m, r.ONE);
    }
    static fromBytes(v) {
      const b = $.fromAffine(y(Gn(v, void 0, "point")));
      return b.assertValidity(), b;
    }
    static fromHex(v) {
      return $.fromBytes(Me("pointHex", v));
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
    precompute(v = 8, b = !0) {
      return z.createCache(this, v), b || this.multiply(lo), this;
    }
    // TODO: return `this`
    /** A point on curve is valid if it conforms to equation. */
    assertValidity() {
      L(this);
    }
    hasEvenY() {
      const { y: v } = this.toAffine();
      if (!r.isOdd)
        throw new Error("Field doesn't support isOdd");
      return !r.isOdd(v);
    }
    /** Compare one point to another. */
    equals(v) {
      C(v);
      const { X: b, Y: m, Z: A } = this, { X: P, Y: E, Z: I } = v, T = r.eql(r.mul(b, I), r.mul(P, A)), O = r.eql(r.mul(m, I), r.mul(E, A));
      return T && O;
    }
    /** Flips point to one corresponding to (x, -y) in Affine coordinates. */
    negate() {
      return new $(this.X, r.neg(this.Y), this.Z);
    }
    // Renes-Costello-Batina exception-free doubling formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 3
    // Cost: 8M + 3S + 3*a + 2*b3 + 15add.
    double() {
      const { a: v, b } = o, m = r.mul(b, lo), { X: A, Y: P, Z: E } = this;
      let I = r.ZERO, T = r.ZERO, O = r.ZERO, U = r.mul(A, A), K = r.mul(P, P), N = r.mul(E, E), M = r.mul(A, P);
      return M = r.add(M, M), O = r.mul(A, E), O = r.add(O, O), I = r.mul(v, O), T = r.mul(m, N), T = r.add(I, T), I = r.sub(K, T), T = r.add(K, T), T = r.mul(I, T), I = r.mul(M, I), O = r.mul(m, O), N = r.mul(v, N), M = r.sub(U, N), M = r.mul(v, M), M = r.add(M, O), O = r.add(U, U), U = r.add(O, U), U = r.add(U, N), U = r.mul(U, M), T = r.add(T, U), N = r.mul(P, E), N = r.add(N, N), U = r.mul(N, M), I = r.sub(I, U), O = r.mul(N, K), O = r.add(O, O), O = r.add(O, O), new $(I, T, O);
    }
    // Renes-Costello-Batina exception-free addition formula.
    // There is 30% faster Jacobian formula, but it is not complete.
    // https://eprint.iacr.org/2015/1060, algorithm 1
    // Cost: 12M + 0S + 3*a + 3*b3 + 23add.
    add(v) {
      C(v);
      const { X: b, Y: m, Z: A } = this, { X: P, Y: E, Z: I } = v;
      let T = r.ZERO, O = r.ZERO, U = r.ZERO;
      const K = o.a, N = r.mul(o.b, lo);
      let M = r.mul(b, P), F = r.mul(m, E), j = r.mul(A, I), V = r.add(b, m), q = r.add(P, E);
      V = r.mul(V, q), q = r.add(M, F), V = r.sub(V, q), q = r.add(b, A);
      let W = r.add(P, I);
      return q = r.mul(q, W), W = r.add(M, j), q = r.sub(q, W), W = r.add(m, A), T = r.add(E, I), W = r.mul(W, T), T = r.add(F, j), W = r.sub(W, T), U = r.mul(K, q), T = r.mul(N, j), U = r.add(T, U), T = r.sub(F, U), U = r.add(F, U), O = r.mul(T, U), F = r.add(M, M), F = r.add(F, M), j = r.mul(K, j), q = r.mul(N, q), F = r.add(F, j), j = r.sub(M, j), j = r.mul(K, j), q = r.add(q, j), M = r.mul(F, q), O = r.add(O, M), M = r.mul(W, q), T = r.mul(V, T), T = r.sub(T, M), M = r.mul(V, F), U = r.mul(W, U), U = r.add(U, M), new $(T, O, U);
    }
    subtract(v) {
      return this.add(v.negate());
    }
    is0() {
      return this.equals($.ZERO);
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
    multiply(v) {
      const { endo: b } = t;
      if (!i.isValidNot0(v))
        throw new Error("invalid scalar: out of range");
      let m, A;
      const P = (E) => z.cached(this, E, (I) => ka($, I));
      if (b) {
        const { k1neg: E, k1: I, k2neg: T, k2: O } = B(v), { p: U, f: K } = P(I), { p: N, f: M } = P(O);
        A = K.add(M), m = H(b.beta, U, N, E, T);
      } else {
        const { p: E, f: I } = P(v);
        m = E, A = I;
      }
      return ka($, [m, A])[0];
    }
    /**
     * Non-constant-time multiplication. Uses double-and-add algorithm.
     * It's faster, but should only be used when you don't care about
     * an exposed secret key e.g. sig verification, which works over *public* keys.
     */
    multiplyUnsafe(v) {
      const { endo: b } = t, m = this;
      if (!i.isValid(v))
        throw new Error("invalid scalar: out of range");
      if (v === Yt || m.is0())
        return $.ZERO;
      if (v === Cr)
        return m;
      if (z.hasCache(this))
        return this.multiply(v);
      if (b) {
        const { k1neg: A, k1: P, k2neg: E, k2: I } = B(v), { p1: T, p2: O } = Fv($, m, P, I);
        return H(b.beta, T, O, A, E);
      } else
        return z.unsafe(m, v);
    }
    multiplyAndAddUnsafe(v, b, m) {
      const A = this.multiplyUnsafe(b).add(v.multiplyUnsafe(m));
      return A.is0() ? void 0 : A;
    }
    /**
     * Converts Projective point to affine (x, y) coordinates.
     * @param invertedZ Z^-1 (inverted zero) - optional, precomputation is useful for invertBatch
     */
    toAffine(v) {
      return R(this, v);
    }
    /**
     * Checks whether Point is free of torsion elements (is in prime subgroup).
     * Always torsion-free for cofactor=1 curves.
     */
    isTorsionFree() {
      const { isTorsionFree: v } = t;
      return s === Cr ? !0 : v ? v($, this) : z.unsafe(this, a).is0();
    }
    clearCofactor() {
      const { clearCofactor: v } = t;
      return s === Cr ? this : v ? v($, this) : this.multiplyUnsafe(s);
    }
    isSmallOrder() {
      return this.multiplyUnsafe(s).is0();
    }
    toBytes(v = !0) {
      return Xo(v, "isCompressed"), this.assertValidity(), h($, this, v);
    }
    toHex(v = !0) {
      return tr(this.toBytes(v));
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
    toRawBytes(v = !0) {
      return this.toBytes(v);
    }
    _setWindowSize(v) {
      this.precompute(v);
    }
    static normalizeZ(v) {
      return ka($, v);
    }
    static msm(v, b) {
      return jv($, i, v, b);
    }
    static fromPrivateKey(v) {
      return $.BASE.multiply($r(i, v));
    }
  }
  $.BASE = new $(o.Gx, o.Gy, r.ONE), $.ZERO = new $(r.ZERO, r.ONE, r.ZERO), $.Fp = r, $.Fn = i;
  const D = i.BITS, z = new Kv($, t.endo ? Math.ceil(D / 2) : D);
  return $.BASE.precompute(8), $;
}
function xp(e) {
  return Uint8Array.of(e ? 2 : 3);
}
function kp(e, t) {
  return {
    secretKey: t.BYTES,
    publicKey: 1 + e.BYTES,
    publicKeyUncompressed: 1 + 2 * e.BYTES,
    publicKeyHasPrefix: !0,
    signature: 2 * t.BYTES
  };
}
function Yv(e, t = {}) {
  const { Fn: n } = e, r = t.randomBytes || ep, i = Object.assign(kp(e.Fp, n), { seed: yp(n.ORDER) });
  function o(h) {
    try {
      return !!$r(n, h);
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
    return Ev(Gn(h, i.seed, "seed"), n.ORDER);
  }
  function c(h, y = !0) {
    return e.BASE.multiply($r(n, h)).toBytes(y);
  }
  function l(h) {
    const y = a(h);
    return { secretKey: y, publicKey: c(y) };
  }
  function u(h) {
    if (typeof h == "bigint")
      return !1;
    if (h instanceof e)
      return !0;
    const { secretKey: y, publicKey: w, publicKeyUncompressed: p } = i;
    if (n.allowedLengths || y === w)
      return;
    const g = Me("key", h).length;
    return g === w || g === p;
  }
  function d(h, y, w = !0) {
    if (u(h) === !0)
      throw new Error("first arg must be private key");
    if (u(y) === !1)
      throw new Error("second arg must be public key");
    const p = $r(n, h);
    return e.fromHex(y).multiply(p).toBytes(w);
  }
  return Object.freeze({ getPublicKey: c, getSharedSecret: d, keygen: l, Point: e, utils: {
    isValidSecretKey: o,
    isValidPublicKey: s,
    randomSecretKey: a,
    // TODO: remove
    isValidPrivateKey: o,
    randomPrivateKey: a,
    normPrivateKeyToScalar: (h) => $r(n, h),
    precompute(h = 8, y = e.BASE) {
      return y.precompute(h, !1);
    }
  }, lengths: i });
}
function Xv(e, t, n = {}) {
  Yh(t), sl(n, {}, {
    hmac: "function",
    lowS: "boolean",
    randomBytes: "function",
    bits2int: "function",
    bits2int_modN: "function"
  });
  const r = n.randomBytes || ep, i = n.hmac || ((b, ...m) => Jo(t, b, Qe(...m))), { Fp: o, Fn: s } = e, { ORDER: a, BITS: c } = s, { keygen: l, getPublicKey: u, getSharedSecret: d, utils: f, lengths: h } = Yv(e, n), y = {
    prehash: !1,
    lowS: typeof n.lowS == "boolean" ? n.lowS : !1,
    format: void 0,
    //'compact' as ECDSASigFormat,
    extraEntropy: !1
  }, w = "compact";
  function p(b) {
    const m = a >> Cr;
    return b > m;
  }
  function g(b, m) {
    if (!s.isValidNot0(m))
      throw new Error(`invalid signature ${b}: out of range 1..Point.Fn.ORDER`);
    return m;
  }
  function _(b, m) {
    uc(m);
    const A = h.signature, P = m === "compact" ? A : m === "recovered" ? A + 1 : void 0;
    return Gn(b, P, `${m} signature`);
  }
  class k {
    constructor(m, A, P) {
      this.r = g("r", m), this.s = g("s", A), P != null && (this.recovery = P), Object.freeze(this);
    }
    static fromBytes(m, A = w) {
      _(m, A);
      let P;
      if (A === "der") {
        const { r: O, s: U } = Wt.toSig(Gn(m));
        return new k(O, U);
      }
      A === "recovered" && (P = m[0], A = "compact", m = m.subarray(1));
      const E = s.BYTES, I = m.subarray(0, E), T = m.subarray(E, E * 2);
      return new k(s.fromBytes(I), s.fromBytes(T), P);
    }
    static fromHex(m, A) {
      return this.fromBytes(_i(m), A);
    }
    addRecoveryBit(m) {
      return new k(this.r, this.s, m);
    }
    recoverPublicKey(m) {
      const A = o.ORDER, { r: P, s: E, recovery: I } = this;
      if (I == null || ![0, 1, 2, 3].includes(I))
        throw new Error("recovery id invalid");
      if (a * vp < A && I > 1)
        throw new Error("recovery id is ambiguous for h>1 curve");
      const O = I === 2 || I === 3 ? P + a : P;
      if (!o.isValid(O))
        throw new Error("recovery id 2 or 3 invalid");
      const U = o.toBytes(O), K = e.fromBytes(Qe(xp((I & 1) === 0), U)), N = s.inv(O), M = B(Me("msgHash", m)), F = s.create(-M * N), j = s.create(E * N), V = e.BASE.multiplyUnsafe(F).add(K.multiplyUnsafe(j));
      if (V.is0())
        throw new Error("point at infinify");
      return V.assertValidity(), V;
    }
    // Signatures should be low-s, to prevent malleability.
    hasHighS() {
      return p(this.s);
    }
    toBytes(m = w) {
      if (uc(m), m === "der")
        return _i(Wt.hexFromSig(this));
      const A = s.toBytes(this.r), P = s.toBytes(this.s);
      if (m === "recovered") {
        if (this.recovery == null)
          throw new Error("recovery bit must be present");
        return Qe(Uint8Array.of(this.recovery), A, P);
      }
      return Qe(A, P);
    }
    toHex(m) {
      return tr(this.toBytes(m));
    }
    // TODO: remove
    assertValidity() {
    }
    static fromCompact(m) {
      return k.fromBytes(Me("sig", m), "compact");
    }
    static fromDER(m) {
      return k.fromBytes(Me("sig", m), "der");
    }
    normalizeS() {
      return this.hasHighS() ? new k(this.r, s.neg(this.s), this.recovery) : this;
    }
    toDERRawBytes() {
      return this.toBytes("der");
    }
    toDERHex() {
      return tr(this.toBytes("der"));
    }
    toCompactRawBytes() {
      return this.toBytes("compact");
    }
    toCompactHex() {
      return tr(this.toBytes("compact"));
    }
  }
  const C = n.bits2int || function(m) {
    if (m.length > 8192)
      throw new Error("input is too large");
    const A = Ds(m), P = m.length * 8 - c;
    return P > 0 ? A >> BigInt(P) : A;
  }, B = n.bits2int_modN || function(m) {
    return s.create(C(m));
  }, R = Ki(c);
  function L(b) {
    return hv("num < 2^" + c, b, Yt, R), s.toBytes(b);
  }
  function H(b, m) {
    return Gn(b, void 0, "message"), m ? Gn(t(b), void 0, "prehashed message") : b;
  }
  function $(b, m, A) {
    if (["recovered", "canonical"].some((F) => F in A))
      throw new Error("sign() legacy options not supported");
    const { lowS: P, prehash: E, extraEntropy: I } = _a(A, y);
    b = H(b, E);
    const T = B(b), O = $r(s, m), U = [L(O), L(T)];
    if (I != null && I !== !1) {
      const F = I === !0 ? r(h.secretKey) : I;
      U.push(Me("extraEntropy", F));
    }
    const K = Qe(...U), N = T;
    function M(F) {
      const j = C(F);
      if (!s.isValidNot0(j))
        return;
      const V = s.inv(j), q = e.BASE.multiply(j).toAffine(), W = s.create(q.x);
      if (W === Yt)
        return;
      const de = s.create(V * s.create(N + W * O));
      if (de === Yt)
        return;
      let se = (q.x === W ? 0 : 2) | Number(q.y & Cr), ye = de;
      return P && p(de) && (ye = s.neg(de), se ^= 1), new k(W, ye, se);
    }
    return { seed: K, k2sig: M };
  }
  function D(b, m, A = {}) {
    b = Me("message", b);
    const { seed: P, k2sig: E } = $(b, m, A);
    return pv(t.outputLen, s.BYTES, i)(P, E);
  }
  function z(b) {
    let m;
    const A = typeof b == "string" || Ns(b), P = !A && b !== null && typeof b == "object" && typeof b.r == "bigint" && typeof b.s == "bigint";
    if (!A && !P)
      throw new Error("invalid signature, expected Uint8Array, hex string or Signature instance");
    if (P)
      m = new k(b.r, b.s);
    else if (A) {
      try {
        m = k.fromBytes(Me("sig", b), "der");
      } catch (E) {
        if (!(E instanceof Wt.Err))
          throw E;
      }
      if (!m)
        try {
          m = k.fromBytes(Me("sig", b), "compact");
        } catch {
          return !1;
        }
    }
    return m || !1;
  }
  function x(b, m, A, P = {}) {
    const { lowS: E, prehash: I, format: T } = _a(P, y);
    if (A = Me("publicKey", A), m = H(Me("message", m), I), "strict" in P)
      throw new Error("options.strict was renamed to lowS");
    const O = T === void 0 ? z(b) : k.fromBytes(Me("sig", b), T);
    if (O === !1)
      return !1;
    try {
      const U = e.fromBytes(A);
      if (E && O.hasHighS())
        return !1;
      const { r: K, s: N } = O, M = B(m), F = s.inv(N), j = s.create(M * F), V = s.create(K * F), q = e.BASE.multiplyUnsafe(j).add(U.multiplyUnsafe(V));
      return q.is0() ? !1 : s.create(q.x) === K;
    } catch {
      return !1;
    }
  }
  function v(b, m, A = {}) {
    const { prehash: P } = _a(A, y);
    return m = H(m, P), k.fromBytes(b, "recovered").recoverPublicKey(m).toBytes();
  }
  return Object.freeze({
    keygen: l,
    getPublicKey: u,
    getSharedSecret: d,
    utils: f,
    lengths: h,
    Point: e,
    sign: D,
    verify: x,
    recoverPublicKey: v,
    Signature: k,
    hash: t
  });
}
function Jv(e) {
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
  const i = Fi(t.n, {
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
function Qv(e) {
  const { CURVE: t, curveOpts: n } = Jv(e), r = {
    hmac: e.hmac,
    randomBytes: e.randomBytes,
    lowS: e.lowS,
    bits2int: e.bits2int,
    bits2int_modN: e.bits2int_modN
  };
  return { CURVE: t, curveOpts: n, hash: e.hash, ecdsaOpts: r };
}
function e1(e, t) {
  const n = t.Point;
  return Object.assign({}, t, {
    ProjectivePoint: n,
    CURVE: Object.assign({}, e, hp(n.Fn.ORDER, n.Fn.BITS))
  });
}
function t1(e) {
  const { CURVE: t, curveOpts: n, hash: r, ecdsaOpts: i } = Qv(e), o = Zv(t, n), s = Xv(o, r, i);
  return e1(e, s);
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function n1(e, t) {
  const n = (r) => t1({ ...e, hash: r });
  return { ...n(t), create: n };
}
/*! noble-curves - MIT License (c) 2022 Paul Miller (paulmillr.com) */
const dl = {
  p: BigInt("0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f"),
  n: BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"),
  h: BigInt(1),
  a: BigInt(0),
  b: BigInt(7),
  Gx: BigInt("0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
  Gy: BigInt("0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")
}, r1 = {
  beta: BigInt("0x7ae96a2b657c07106e64479eac3434e99cf0497512f58995c1396c28719501ee"),
  basises: [
    [BigInt("0x3086d221a7d46bcde86c90e49284eb15"), -BigInt("0xe4437ed6010e88286f547fa90abfe4c3")],
    [BigInt("0x114ca50f7a8e2f3f657c1108d9d44cfd8"), BigInt("0x3086d221a7d46bcde86c90e49284eb15")]
  ]
}, cu = /* @__PURE__ */ BigInt(2);
function i1(e) {
  const t = dl.p, n = BigInt(3), r = BigInt(6), i = BigInt(11), o = BigInt(22), s = BigInt(23), a = BigInt(44), c = BigInt(88), l = e * e * e % t, u = l * l * e % t, d = Ze(u, n, t) * u % t, f = Ze(d, n, t) * u % t, h = Ze(f, cu, t) * l % t, y = Ze(h, i, t) * h % t, w = Ze(y, o, t) * y % t, p = Ze(w, a, t) * w % t, g = Ze(p, c, t) * p % t, _ = Ze(g, a, t) * w % t, k = Ze(_, n, t) * u % t, C = Ze(k, s, t) * y % t, B = Ze(C, r, t) * l % t, R = Ze(B, cu, t);
  if (!fc.eql(fc.sqr(R), e))
    throw new Error("Cannot find square root");
  return R;
}
const fc = Fi(dl.p, { sqrt: i1 }), qt = n1({ ...dl, Fp: fc, lowS: !0, endo: r1 }, ll), o1 = /* @__PURE__ */ Uint8Array.from([
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
]), Ep = Uint8Array.from(new Array(16).fill(0).map((e, t) => t)), s1 = Ep.map((e) => (9 * e + 5) % 16), Ap = /* @__PURE__ */ (() => {
  const n = [[Ep], [s1]];
  for (let r = 0; r < 4; r++)
    for (let i of n)
      i.push(i[r].map((o) => o1[o]));
  return n;
})(), Sp = Ap[0], _p = Ap[1], Ip = /* @__PURE__ */ [
  [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
  [12, 13, 11, 15, 6, 9, 9, 7, 12, 15, 11, 13, 7, 8, 7, 7],
  [13, 15, 14, 11, 7, 7, 6, 8, 13, 14, 13, 12, 5, 5, 6, 9],
  [14, 11, 12, 14, 8, 6, 5, 5, 15, 12, 15, 14, 9, 9, 8, 6],
  [15, 12, 13, 13, 9, 5, 8, 6, 14, 11, 12, 11, 8, 6, 5, 5]
].map((e) => Uint8Array.from(e)), a1 = /* @__PURE__ */ Sp.map((e, t) => e.map((n) => Ip[t][n])), c1 = /* @__PURE__ */ _p.map((e, t) => e.map((n) => Ip[t][n])), l1 = /* @__PURE__ */ Uint32Array.from([
  0,
  1518500249,
  1859775393,
  2400959708,
  2840853838
]), d1 = /* @__PURE__ */ Uint32Array.from([
  1352829926,
  1548603684,
  1836072691,
  2053994217,
  0
]);
function lu(e, t, n, r) {
  return e === 0 ? t ^ n ^ r : e === 1 ? t & n | ~t & r : e === 2 ? (t | ~n) ^ r : e === 3 ? t & r | n & ~r : t ^ (n | ~r);
}
const uo = /* @__PURE__ */ new Uint32Array(16);
class u1 extends cl {
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
      uo[h] = t.getUint32(n, !0);
    let r = this.h0 | 0, i = r, o = this.h1 | 0, s = o, a = this.h2 | 0, c = a, l = this.h3 | 0, u = l, d = this.h4 | 0, f = d;
    for (let h = 0; h < 5; h++) {
      const y = 4 - h, w = l1[h], p = d1[h], g = Sp[h], _ = _p[h], k = a1[h], C = c1[h];
      for (let B = 0; B < 16; B++) {
        const R = io(r + lu(h, o, a, l) + uo[g[B]] + w, k[B]) + d | 0;
        r = d, d = l, l = io(a, 10) | 0, a = o, o = R;
      }
      for (let B = 0; B < 16; B++) {
        const R = io(i + lu(y, s, c, u) + uo[_[B]] + p, C[B]) + f | 0;
        i = f, f = u, u = io(c, 10) | 0, c = s, s = R;
      }
    }
    this.set(this.h1 + a + u | 0, this.h2 + l + f | 0, this.h3 + d + i | 0, this.h4 + r + s | 0, this.h0 + o + c | 0);
  }
  roundClean() {
    Ln(uo);
  }
  destroy() {
    this.destroyed = !0, Ln(this.buffer), this.set(0, 0, 0, 0, 0);
  }
}
const f1 = /* @__PURE__ */ rl(() => new u1());
/*! scure-base - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function Ii(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Bp(e, t) {
  return Array.isArray(t) ? t.length === 0 ? !0 : e ? t.every((n) => typeof n == "string") : t.every((n) => Number.isSafeInteger(n)) : !1;
}
function $p(e) {
  if (typeof e != "function")
    throw new Error("function expected");
  return !0;
}
function Mr(e, t) {
  if (typeof t != "string")
    throw new Error(`${e}: string expected`);
  return !0;
}
function Gr(e) {
  if (!Number.isSafeInteger(e))
    throw new Error(`invalid integer: ${e}`);
}
function es(e) {
  if (!Array.isArray(e))
    throw new Error("array expected");
}
function ts(e, t) {
  if (!Bp(!0, t))
    throw new Error(`${e}: array of strings expected`);
}
function ul(e, t) {
  if (!Bp(!1, t))
    throw new Error(`${e}: array of numbers expected`);
}
// @__NO_SIDE_EFFECTS__
function zs(...e) {
  const t = (o) => o, n = (o, s) => (a) => o(s(a)), r = e.map((o) => o.encode).reduceRight(n, t), i = e.map((o) => o.decode).reduce(n, t);
  return { encode: r, decode: i };
}
// @__NO_SIDE_EFFECTS__
function fl(e) {
  const t = typeof e == "string" ? e.split("") : e, n = t.length;
  ts("alphabet", t);
  const r = new Map(t.map((i, o) => [i, o]));
  return {
    encode: (i) => (es(i), i.map((o) => {
      if (!Number.isSafeInteger(o) || o < 0 || o >= n)
        throw new Error(`alphabet.encode: digit index outside alphabet "${o}". Allowed: ${e}`);
      return t[o];
    })),
    decode: (i) => (es(i), i.map((o) => {
      Mr("alphabet.decode", o);
      const s = r.get(o);
      if (s === void 0)
        throw new Error(`Unknown letter: "${o}". Allowed: ${e}`);
      return s;
    }))
  };
}
// @__NO_SIDE_EFFECTS__
function hl(e = "") {
  return Mr("join", e), {
    encode: (t) => (ts("join.decode", t), t.join(e)),
    decode: (t) => (Mr("join.decode", t), t.split(e))
  };
}
// @__NO_SIDE_EFFECTS__
function h1(e, t = "=") {
  return Gr(e), Mr("padding", t), {
    encode(n) {
      for (ts("padding.encode", n); n.length * e % 8; )
        n.push(t);
      return n;
    },
    decode(n) {
      ts("padding.decode", n);
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
function hc(e, t, n) {
  if (t < 2)
    throw new Error(`convertRadix: invalid from=${t}, base cannot be less than 2`);
  if (n < 2)
    throw new Error(`convertRadix: invalid to=${n}, base cannot be less than 2`);
  if (es(e), !e.length)
    return [];
  let r = 0;
  const i = [], o = Array.from(e, (a) => {
    if (Gr(a), a < 0 || a >= t)
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
const Tp = (e, t) => t === 0 ? e : Tp(t, e % t), ns = /* @__NO_SIDE_EFFECTS__ */ (e, t) => e + (t - Tp(e, t)), Ro = /* @__PURE__ */ (() => {
  let e = [];
  for (let t = 0; t < 40; t++)
    e.push(2 ** t);
  return e;
})();
function rs(e, t, n, r) {
  if (es(e), t <= 0 || t > 32)
    throw new Error(`convertRadix2: wrong from=${t}`);
  if (n <= 0 || n > 32)
    throw new Error(`convertRadix2: wrong to=${n}`);
  if (/* @__PURE__ */ ns(t, n) > 32)
    throw new Error(`convertRadix2: carry overflow from=${t} to=${n} carryBits=${/* @__PURE__ */ ns(t, n)}`);
  let i = 0, o = 0;
  const s = Ro[t], a = Ro[n] - 1, c = [];
  for (const l of e) {
    if (Gr(l), l >= s)
      throw new Error(`convertRadix2: invalid data word=${l} from=${t}`);
    if (i = i << t | l, o + t > 32)
      throw new Error(`convertRadix2: carry overflow pos=${o} from=${t}`);
    for (o += t; o >= n; o -= n)
      c.push((i >> o - n & a) >>> 0);
    const u = Ro[o];
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
function Pp(e) {
  Gr(e);
  const t = 2 ** 8;
  return {
    encode: (n) => {
      if (!Ii(n))
        throw new Error("radix.encode input should be Uint8Array");
      return hc(Array.from(n), t, e);
    },
    decode: (n) => (ul("radix.decode", n), Uint8Array.from(hc(n, e, t)))
  };
}
// @__NO_SIDE_EFFECTS__
function Op(e, t = !1) {
  if (Gr(e), e <= 0 || e > 32)
    throw new Error("radix2: bits should be in (0..32]");
  if (/* @__PURE__ */ ns(8, e) > 32 || /* @__PURE__ */ ns(e, 8) > 32)
    throw new Error("radix2: carry overflow");
  return {
    encode: (n) => {
      if (!Ii(n))
        throw new Error("radix2.encode input should be Uint8Array");
      return rs(Array.from(n), 8, e, !t);
    },
    decode: (n) => (ul("radix2.decode", n), Uint8Array.from(rs(n, e, 8, t)))
  };
}
function du(e) {
  return $p(e), function(...t) {
    try {
      return e.apply(null, t);
    } catch {
    }
  };
}
function Cp(e, t) {
  return Gr(e), $p(t), {
    encode(n) {
      if (!Ii(n))
        throw new Error("checksum.encode: input should be Uint8Array");
      const r = t(n).slice(0, e), i = new Uint8Array(n.length + e);
      return i.set(n), i.set(r, n.length), i;
    },
    decode(n) {
      if (!Ii(n))
        throw new Error("checksum.decode: input should be Uint8Array");
      const r = n.slice(0, -e), i = n.slice(-e), o = t(r).slice(0, e);
      for (let s = 0; s < e; s++)
        if (o[s] !== i[s])
          throw new Error("Invalid checksum");
      return r;
    }
  };
}
const fo = {
  alphabet: fl,
  chain: zs,
  checksum: Cp,
  convertRadix: hc,
  convertRadix2: rs,
  radix: Pp,
  radix2: Op,
  join: hl,
  padding: h1
}, p1 = /* @__NO_SIDE_EFFECTS__ */ (e) => /* @__PURE__ */ zs(/* @__PURE__ */ Pp(58), /* @__PURE__ */ fl(e), /* @__PURE__ */ hl("")), Rp = /* @__PURE__ */ p1("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"), Hp = (e) => /* @__PURE__ */ zs(Cp(4, (t) => e(e(t))), Rp), y1 = Hp, pc = /* @__PURE__ */ zs(/* @__PURE__ */ fl("qpzry9x8gf2tvdw0s3jn54khce6mua7l"), /* @__PURE__ */ hl("")), uu = [996825010, 642813549, 513874426, 1027748829, 705979059];
function ri(e) {
  const t = e >> 25;
  let n = (e & 33554431) << 5;
  for (let r = 0; r < uu.length; r++)
    (t >> r & 1) === 1 && (n ^= uu[r]);
  return n;
}
function fu(e, t, n = 1) {
  const r = e.length;
  let i = 1;
  for (let o = 0; o < r; o++) {
    const s = e.charCodeAt(o);
    if (s < 33 || s > 126)
      throw new Error(`Invalid prefix (${e})`);
    i = ri(i) ^ s >> 5;
  }
  i = ri(i);
  for (let o = 0; o < r; o++)
    i = ri(i) ^ e.charCodeAt(o) & 31;
  for (let o of t)
    i = ri(i) ^ o;
  for (let o = 0; o < 6; o++)
    i = ri(i);
  return i ^= n, pc.encode(rs([i % Ro[30]], 30, 5, !1));
}
// @__NO_SIDE_EFFECTS__
function Lp(e) {
  const t = e === "bech32" ? 1 : 734539939, n = /* @__PURE__ */ Op(5), r = n.decode, i = n.encode, o = du(r);
  function s(d, f, h = 90) {
    Mr("bech32.encode prefix", d), Ii(f) && (f = Array.from(f)), ul("bech32.encode", f);
    const y = d.length;
    if (y === 0)
      throw new TypeError(`Invalid prefix length ${y}`);
    const w = y + 7 + f.length;
    if (h !== !1 && w > h)
      throw new TypeError(`Length ${w} exceeds limit ${h}`);
    const p = d.toLowerCase(), g = fu(p, f, t);
    return `${p}1${pc.encode(f)}${g}`;
  }
  function a(d, f = 90) {
    Mr("bech32.decode input", d);
    const h = d.length;
    if (h < 8 || f !== !1 && h > f)
      throw new TypeError(`invalid string length: ${h} (${d}). Expected (8..${f})`);
    const y = d.toLowerCase();
    if (d !== y && d !== d.toUpperCase())
      throw new Error("String must be lowercase or uppercase");
    const w = y.lastIndexOf("1");
    if (w === 0 || w === -1)
      throw new Error('Letter "1" must be present between prefix and data only');
    const p = y.slice(0, w), g = y.slice(w + 1);
    if (g.length < 6)
      throw new Error("Data must be at least 6 characters long");
    const _ = pc.decode(g).slice(0, -6), k = fu(p, _, t);
    if (!g.endsWith(k))
      throw new Error(`Invalid checksum in ${d}: expected "${k}"`);
    return { prefix: p, words: _ };
  }
  const c = du(a);
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
const hu = /* @__PURE__ */ Lp("bech32"), pu = /* @__PURE__ */ Lp("bech32m");
/*! scure-bip32 - MIT License (c) 2022 Patricio Palladino, Paul Miller (paulmillr.com) */
const ho = qt.ProjectivePoint, Ia = Hp(ll);
function yu(e) {
  qe(e);
  const t = e.length === 0 ? "0" : tr(e);
  return BigInt("0x" + t);
}
function w1(e) {
  if (typeof e != "bigint")
    throw new Error("bigint expected");
  return _i(e.toString(16).padStart(64, "0"));
}
const g1 = Jh("Bitcoin seed"), Ba = { private: 76066276, public: 76067358 }, $a = 2147483648, m1 = (e) => f1(ll(e)), b1 = (e) => Or(e).getUint32(0, !1), po = (e) => {
  if (!Number.isSafeInteger(e) || e < 0 || e > 2 ** 32 - 1)
    throw new Error("invalid number, should be from 0 to 2**32-1, got " + e);
  const t = new Uint8Array(4);
  return Or(t).setUint32(0, e, !1), t;
};
class _n {
  get fingerprint() {
    if (!this.pubHash)
      throw new Error("No publicKey set!");
    return b1(this.pubHash);
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
    return Ia.encode(this.serialize(this.versions.private, Qe(new Uint8Array([0]), t)));
  }
  get publicExtendedKey() {
    if (!this.pubKey)
      throw new Error("No public key");
    return Ia.encode(this.serialize(this.versions.public, this.pubKey));
  }
  static fromMasterSeed(t, n = Ba) {
    if (qe(t), 8 * t.length < 128 || 8 * t.length > 512)
      throw new Error("HDKey: seed length must be between 128 and 512 bits; 256 bits is advised, got " + t.length);
    const r = Jo(ru, g1, t);
    return new _n({
      versions: n,
      chainCode: r.slice(32),
      privateKey: r.slice(0, 32)
    });
  }
  static fromExtendedKey(t, n = Ba) {
    const r = Ia.decode(t), i = Or(r), o = i.getUint32(0, !1), s = {
      versions: n,
      depth: r[4],
      parentFingerprint: i.getUint32(5, !1),
      index: i.getUint32(9, !1),
      chainCode: r.slice(13, 45)
    }, a = r.slice(45), c = a[0] === 0;
    if (o !== n[c ? "private" : "public"])
      throw new Error("Version mismatch");
    return c ? new _n({ ...s, privateKey: a.slice(1) }) : new _n({ ...s, publicKey: a });
  }
  static fromJSON(t) {
    return _n.fromExtendedKey(t.xpriv);
  }
  constructor(t) {
    if (this.depth = 0, this.index = 0, this.chainCode = null, this.parentFingerprint = 0, !t || typeof t != "object")
      throw new Error("HDKey.constructor must not be called directly");
    if (this.versions = t.versions || Ba, this.depth = t.depth || 0, this.chainCode = t.chainCode || null, this.index = t.index || 0, this.parentFingerprint = t.parentFingerprint || 0, !this.depth && (this.parentFingerprint || this.index))
      throw new Error("HDKey: zero depth with non-zero index/parent fingerprint");
    if (t.publicKey && t.privateKey)
      throw new Error("HDKey: publicKey and privateKey at same time.");
    if (t.privateKey) {
      if (!qt.utils.isValidPrivateKey(t.privateKey))
        throw new Error("Invalid private key");
      this.privKey = typeof t.privateKey == "bigint" ? t.privateKey : yu(t.privateKey), this.privKeyBytes = w1(this.privKey), this.pubKey = qt.getPublicKey(t.privateKey, !0);
    } else if (t.publicKey)
      this.pubKey = ho.fromHex(t.publicKey).toRawBytes(!0);
    else
      throw new Error("HDKey: no public or private key provided");
    this.pubHash = m1(this.pubKey);
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
      if (!Number.isSafeInteger(a) || a >= $a)
        throw new Error("Invalid index");
      o[2] === "'" && (a += $a), r = r.deriveChild(a);
    }
    return r;
  }
  deriveChild(t) {
    if (!this.pubKey || !this.chainCode)
      throw new Error("No publicKey or chainCode set");
    let n = po(t);
    if (t >= $a) {
      const a = this.privateKey;
      if (!a)
        throw new Error("Could not derive hardened child key");
      n = Qe(new Uint8Array([0]), a, n);
    } else
      n = Qe(this.pubKey, n);
    const r = Jo(ru, this.chainCode, n), i = yu(r.slice(0, 32)), o = r.slice(32);
    if (!qt.utils.isValidPrivateKey(i))
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
        const a = Je(this.privKey + i, qt.CURVE.n);
        if (!qt.utils.isValidPrivateKey(a))
          throw new Error("The tweak was out of range or the resulted private key is invalid");
        s.privateKey = a;
      } else {
        const a = ho.fromHex(this.pubKey).add(ho.fromPrivateKey(i));
        if (a.equals(ho.ZERO))
          throw new Error("The tweak was equal to negative P, which made the result key invalid");
        s.publicKey = a.toRawBytes(!0);
      }
      return new _n(s);
    } catch {
      return this.deriveChild(t + 1);
    }
  }
  sign(t) {
    if (!this.privateKey)
      throw new Error("No privateKey set!");
    return qe(t, 32), qt.sign(t, this.privKey).toCompactRawBytes();
  }
  verify(t, n) {
    if (qe(t, 32), qe(n, 64), !this.publicKey)
      throw new Error("No publicKey set!");
    let r;
    try {
      r = qt.Signature.fromCompact(n);
    } catch {
      return !1;
    }
    return qt.verify(r, t, this.publicKey);
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
    return qe(n, 33), Qe(po(t), new Uint8Array([this.depth]), po(this.parentFingerprint), po(this.index), this.chainCode, n);
  }
}
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function v1(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function xi(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function Wr(e, ...t) {
  if (!v1(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function Up(e) {
  if (typeof e != "function" || typeof e.create != "function")
    throw new Error("Hash should be wrapped by utils.createHasher");
  xi(e.outputLen), xi(e.blockLen);
}
function is(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function x1(e, t) {
  Wr(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function ir(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function Ho(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function kt(e, t) {
  return e << 32 - t | e >>> t;
}
function Np(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function pl(e) {
  return typeof e == "string" && (e = Np(e)), Wr(e), e;
}
function wu(e) {
  return typeof e == "string" && (e = Np(e)), Wr(e), e;
}
function k1(e, t) {
  if (t !== void 0 && {}.toString.call(t) !== "[object Object]")
    throw new Error("options should be object or undefined");
  return Object.assign(e, t);
}
let Dp = class {
};
function zp(e) {
  const t = (r) => e().update(pl(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
class Mp extends Dp {
  constructor(t, n) {
    super(), this.finished = !1, this.destroyed = !1, Up(t);
    const r = pl(n);
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
    this.oHash.update(o), ir(o);
  }
  update(t) {
    return is(this), this.iHash.update(t), this;
  }
  digestInto(t) {
    is(this), Wr(t, this.outputLen), this.finished = !0, this.iHash.digestInto(t), this.oHash.update(t), this.oHash.digestInto(t), this.destroy();
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
const Kp = (e, t, n) => new Mp(e, t).update(n).digest();
Kp.create = (e, t) => new Mp(e, t);
function E1(e, t, n, r) {
  Up(e);
  const i = k1({ dkLen: 32, asyncTick: 10 }, r), { c: o, dkLen: s, asyncTick: a } = i;
  if (xi(o), xi(s), xi(a), o < 1)
    throw new Error("iterations (c) should be >= 1");
  const c = wu(t), l = wu(n), u = new Uint8Array(s), d = Kp.create(e, c), f = d._cloneInto().update(l);
  return { c: o, dkLen: s, asyncTick: a, DK: u, PRF: d, PRFSalt: f };
}
function A1(e, t, n, r, i) {
  return e.destroy(), t.destroy(), r && r.destroy(), ir(i), n;
}
function S1(e, t, n, r) {
  const { c: i, dkLen: o, DK: s, PRF: a, PRFSalt: c } = E1(e, t, n, r);
  let l;
  const u = new Uint8Array(4), d = Ho(u), f = new Uint8Array(a.outputLen);
  for (let h = 1, y = 0; y < o; h++, y += a.outputLen) {
    const w = s.subarray(y, y + a.outputLen);
    d.setInt32(0, h, !1), (l = c._cloneInto(l)).update(u).digestInto(f), w.set(f.subarray(0, w.length));
    for (let p = 1; p < i; p++) {
      a._cloneInto(l).update(f).digestInto(f);
      for (let g = 0; g < w.length; g++)
        w[g] ^= f[g];
    }
  }
  return A1(a, c, s, l, f);
}
function _1(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function I1(e, t, n) {
  return e & t ^ ~e & n;
}
function B1(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
let Fp = class extends Dp {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = Ho(this.buffer);
  }
  update(t) {
    is(this), t = pl(t), Wr(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = Ho(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    is(this), x1(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, ir(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    _1(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = Ho(t), c = this.outputLen;
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
const bn = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), Oe = /* @__PURE__ */ Uint32Array.from([
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
]), yo = /* @__PURE__ */ BigInt(2 ** 32 - 1), gu = /* @__PURE__ */ BigInt(32);
function $1(e, t = !1) {
  return t ? { h: Number(e & yo), l: Number(e >> gu & yo) } : { h: Number(e >> gu & yo) | 0, l: Number(e & yo) | 0 };
}
function T1(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = $1(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const mu = (e, t, n) => e >>> n, bu = (e, t, n) => e << 32 - n | t >>> n, xr = (e, t, n) => e >>> n | t << 32 - n, kr = (e, t, n) => e << 32 - n | t >>> n, wo = (e, t, n) => e << 64 - n | t >>> n - 32, go = (e, t, n) => e >>> n - 32 | t << 64 - n;
function jt(e, t, n, r) {
  const i = (t >>> 0) + (r >>> 0);
  return { h: e + n + (i / 2 ** 32 | 0) | 0, l: i | 0 };
}
const P1 = (e, t, n) => (e >>> 0) + (t >>> 0) + (n >>> 0), O1 = (e, t, n, r) => t + n + r + (e / 2 ** 32 | 0) | 0, C1 = (e, t, n, r) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0), R1 = (e, t, n, r, i) => t + n + r + i + (e / 2 ** 32 | 0) | 0, H1 = (e, t, n, r, i) => (e >>> 0) + (t >>> 0) + (n >>> 0) + (r >>> 0) + (i >>> 0), L1 = (e, t, n, r, i, o) => t + n + r + i + o + (e / 2 ** 32 | 0) | 0, U1 = /* @__PURE__ */ Uint32Array.from([
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
]), vn = /* @__PURE__ */ new Uint32Array(64);
let N1 = class extends Fp {
  constructor(t = 32) {
    super(64, t, 8, !1), this.A = bn[0] | 0, this.B = bn[1] | 0, this.C = bn[2] | 0, this.D = bn[3] | 0, this.E = bn[4] | 0, this.F = bn[5] | 0, this.G = bn[6] | 0, this.H = bn[7] | 0;
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
      vn[d] = t.getUint32(n, !1);
    for (let d = 16; d < 64; d++) {
      const f = vn[d - 15], h = vn[d - 2], y = kt(f, 7) ^ kt(f, 18) ^ f >>> 3, w = kt(h, 17) ^ kt(h, 19) ^ h >>> 10;
      vn[d] = w + vn[d - 7] + y + vn[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = kt(a, 6) ^ kt(a, 11) ^ kt(a, 25), h = u + f + I1(a, c, l) + U1[d] + vn[d] | 0, w = (kt(r, 2) ^ kt(r, 13) ^ kt(r, 22)) + B1(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    ir(vn);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), ir(this.buffer);
  }
};
const jp = T1([
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
].map((e) => BigInt(e))), D1 = jp[0], z1 = jp[1], xn = /* @__PURE__ */ new Uint32Array(80), kn = /* @__PURE__ */ new Uint32Array(80);
class M1 extends Fp {
  constructor(t = 64) {
    super(128, t, 16, !1), this.Ah = Oe[0] | 0, this.Al = Oe[1] | 0, this.Bh = Oe[2] | 0, this.Bl = Oe[3] | 0, this.Ch = Oe[4] | 0, this.Cl = Oe[5] | 0, this.Dh = Oe[6] | 0, this.Dl = Oe[7] | 0, this.Eh = Oe[8] | 0, this.El = Oe[9] | 0, this.Fh = Oe[10] | 0, this.Fl = Oe[11] | 0, this.Gh = Oe[12] | 0, this.Gl = Oe[13] | 0, this.Hh = Oe[14] | 0, this.Hl = Oe[15] | 0;
  }
  // prettier-ignore
  get() {
    const { Ah: t, Al: n, Bh: r, Bl: i, Ch: o, Cl: s, Dh: a, Dl: c, Eh: l, El: u, Fh: d, Fl: f, Gh: h, Gl: y, Hh: w, Hl: p } = this;
    return [t, n, r, i, o, s, a, c, l, u, d, f, h, y, w, p];
  }
  // prettier-ignore
  set(t, n, r, i, o, s, a, c, l, u, d, f, h, y, w, p) {
    this.Ah = t | 0, this.Al = n | 0, this.Bh = r | 0, this.Bl = i | 0, this.Ch = o | 0, this.Cl = s | 0, this.Dh = a | 0, this.Dl = c | 0, this.Eh = l | 0, this.El = u | 0, this.Fh = d | 0, this.Fl = f | 0, this.Gh = h | 0, this.Gl = y | 0, this.Hh = w | 0, this.Hl = p | 0;
  }
  process(t, n) {
    for (let k = 0; k < 16; k++, n += 4)
      xn[k] = t.getUint32(n), kn[k] = t.getUint32(n += 4);
    for (let k = 16; k < 80; k++) {
      const C = xn[k - 15] | 0, B = kn[k - 15] | 0, R = xr(C, B, 1) ^ xr(C, B, 8) ^ mu(C, B, 7), L = kr(C, B, 1) ^ kr(C, B, 8) ^ bu(C, B, 7), H = xn[k - 2] | 0, $ = kn[k - 2] | 0, D = xr(H, $, 19) ^ wo(H, $, 61) ^ mu(H, $, 6), z = kr(H, $, 19) ^ go(H, $, 61) ^ bu(H, $, 6), x = C1(L, z, kn[k - 7], kn[k - 16]), v = R1(x, R, D, xn[k - 7], xn[k - 16]);
      xn[k] = v | 0, kn[k] = x | 0;
    }
    let { Ah: r, Al: i, Bh: o, Bl: s, Ch: a, Cl: c, Dh: l, Dl: u, Eh: d, El: f, Fh: h, Fl: y, Gh: w, Gl: p, Hh: g, Hl: _ } = this;
    for (let k = 0; k < 80; k++) {
      const C = xr(d, f, 14) ^ xr(d, f, 18) ^ wo(d, f, 41), B = kr(d, f, 14) ^ kr(d, f, 18) ^ go(d, f, 41), R = d & h ^ ~d & w, L = f & y ^ ~f & p, H = H1(_, B, L, z1[k], kn[k]), $ = L1(H, g, C, R, D1[k], xn[k]), D = H | 0, z = xr(r, i, 28) ^ wo(r, i, 34) ^ wo(r, i, 39), x = kr(r, i, 28) ^ go(r, i, 34) ^ go(r, i, 39), v = r & o ^ r & a ^ o & a, b = i & s ^ i & c ^ s & c;
      g = w | 0, _ = p | 0, w = h | 0, p = y | 0, h = d | 0, y = f | 0, { h: d, l: f } = jt(l | 0, u | 0, $ | 0, D | 0), l = a | 0, u = c | 0, a = o | 0, c = s | 0, o = r | 0, s = i | 0;
      const m = P1(D, x, b);
      r = O1(m, $, z, v), i = m | 0;
    }
    ({ h: r, l: i } = jt(this.Ah | 0, this.Al | 0, r | 0, i | 0)), { h: o, l: s } = jt(this.Bh | 0, this.Bl | 0, o | 0, s | 0), { h: a, l: c } = jt(this.Ch | 0, this.Cl | 0, a | 0, c | 0), { h: l, l: u } = jt(this.Dh | 0, this.Dl | 0, l | 0, u | 0), { h: d, l: f } = jt(this.Eh | 0, this.El | 0, d | 0, f | 0), { h, l: y } = jt(this.Fh | 0, this.Fl | 0, h | 0, y | 0), { h: w, l: p } = jt(this.Gh | 0, this.Gl | 0, w | 0, p | 0), { h: g, l: _ } = jt(this.Hh | 0, this.Hl | 0, g | 0, _ | 0), this.set(r, i, o, s, a, c, l, u, d, f, h, y, w, p, g, _);
  }
  roundClean() {
    ir(xn, kn);
  }
  destroy() {
    ir(this.buffer), this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
  }
}
const K1 = /* @__PURE__ */ zp(() => new N1()), F1 = /* @__PURE__ */ zp(() => new M1());
/*! scure-bip39 - MIT License (c) 2022 Patricio Palladino, Paul Miller (paulmillr.com) */
const j1 = (e) => e[0] === "あいこくしん";
function Vp(e) {
  if (typeof e != "string")
    throw new TypeError("invalid mnemonic type: " + typeof e);
  return e.normalize("NFKD");
}
function qp(e) {
  const t = Vp(e), n = t.split(" ");
  if (![12, 15, 18, 21, 24].includes(n.length))
    throw new Error("Invalid mnemonic");
  return { nfkd: t, words: n };
}
function Gp(e) {
  Wr(e, 16, 20, 24, 28, 32);
}
const V1 = (e) => {
  const t = 8 - e.length / 4;
  return new Uint8Array([K1(e)[0] >> t << t]);
};
function Wp(e) {
  if (!Array.isArray(e) || e.length !== 2048 || typeof e[0] != "string")
    throw new Error("Wordlist: expected array of 2048 strings");
  return e.forEach((t) => {
    if (typeof t != "string")
      throw new Error("wordlist: non-string element: " + t);
  }), fo.chain(fo.checksum(1, V1), fo.radix2(11, !0), fo.alphabet(e));
}
function Zp(e, t) {
  const { words: n } = qp(e), r = Wp(t).decode(n);
  return Gp(r), r;
}
function q1(e, t) {
  return Gp(e), Wp(t).encode(e).join(j1(t) ? "　" : " ");
}
function G1(e, t) {
  try {
    Zp(e, t);
  } catch {
    return !1;
  }
  return !0;
}
const W1 = (e) => Vp("mnemonic" + e);
function Z1(e, t = "") {
  return S1(F1, qp(e).nfkd, W1(t), { c: 2048, dkLen: 64 });
}
const Zr = `abandon
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
`), os = "bitcoin-address-types-v1", yl = 20, Yp = new TextEncoder().encode(`avibe:protected-hd-wallet:v1
`), Xp = [16, 20, 24, 28, 32], Y1 = ["account", "evm", "bitcoin", "bitcoin_p2pkh", "bitcoin_p2wpkh", "solana"];
function ht(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new Error("invalid protected HD object");
  return e;
}
function Ot(e, t) {
  if (Object.keys(e).length !== t.length || t.some((n) => !Object.hasOwn(e, n))) throw new Error("invalid protected HD fields");
}
function wl(e) {
  if (typeof e != "number" || !Number.isInteger(e) || e < 0 || e >= yl) throw new Error("invalid protected HD account");
  return e;
}
function Jp(e) {
  const t = ht(e), n = wl(t.account);
  if (t.chain === "bitcoin") {
    if (Ot(t, ["chain", "account", "address_type"]), t.address_type !== "p2tr") throw new Error("protected HD Bitcoin only supports explicit p2tr");
    return { chain: "bitcoin", account: n, address_type: "p2tr" };
  }
  if (Ot(t, ["chain", "account"]), t.chain !== "evm" && t.chain !== "solana") throw new Error("invalid protected HD chain");
  return { chain: t.chain, account: n };
}
function ss(e, t) {
  if (wl(t), e === "evm") return `m/44'/60'/0'/0/${t}`;
  if (e === "bitcoin") return `m/86'/0'/0'/0/${t}`;
  if (e === "solana") return `m/44'/501'/${t}'/0'`;
  throw new Error("invalid protected HD chain");
}
function Zt(e, t = !1) {
  if (typeof e != "string" || !(t ? /^[0-9a-f]{64}$/ : /^(02|03)[0-9a-f]{64}$/).test(e)) throw new Error("invalid protected HD public key");
  const n = lt(e);
  if (t ? !ft.utils.isValidPublicKey(n, !1) : !we.utils.isValidPublicKey(n)) throw new Error("invalid protected HD public key");
  return e;
}
function Ms(e) {
  const t = ht(e);
  if (Ot(t, ["kind", "profile", "accounts"]), t.kind !== "hd-seed" || t.profile !== os || !Array.isArray(t.accounts) || t.accounts.length !== yl) throw new Error("invalid protected HD profile/table");
  return { kind: "hd-seed", profile: os, accounts: t.accounts.map((n, r) => {
    const i = ht(n);
    if (Ot(i, Y1), wl(i.account) !== r) throw new Error("protected HD accounts must be ordered 0..19");
    return { account: r, evm: Zt(i.evm), bitcoin: Zt(i.bitcoin), solana: Zt(i.solana, !0), bitcoin_p2pkh: Zt(i.bitcoin_p2pkh), bitcoin_p2wpkh: Zt(i.bitcoin_p2wpkh) };
  }) };
}
function X1(e) {
  if (!Xp.includes(e.entropy.length) || e.seed.length !== 64 || typeof e.passphraseRequired != "boolean") throw new Error("invalid protected HD payload");
  const t = new Uint8Array(95 + e.entropy.length);
  return t.set(Yp), t[29] = e.entropy.length, t.set(e.entropy, 30), t.set(e.seed, 30 + e.entropy.length), t[t.length - 1] = Number(e.passphraseRequired), t;
}
function Qp(e) {
  const t = e[29];
  if (!Xp.includes(t) || e.length !== 95 + t || Yp.some((n, r) => n !== e[r]) || ![0, 1].includes(e[e.length - 1])) throw new Error("invalid protected HD payload");
  return { entropy: e.slice(30, 30 + t), seed: e.slice(30 + t, 94 + t), passphraseRequired: e[e.length - 1] === 1 };
}
function as(e) {
  e?.entropy.fill(0), e?.seed.fill(0);
}
function ey(e) {
  return e.normalize("NFKD").toLowerCase().trim().split(/\s+/u).filter(Boolean).join(" ");
}
function J1(e) {
  return q1(e, Zr);
}
function ty(e, t) {
  const n = ey(e), r = Zp(n, Zr);
  try {
    return { entropy: r, seed: Z1(n, t), passphraseRequired: t.normalize("NFKD").length > 0 };
  } catch (i) {
    throw r.fill(0), i;
  }
}
function ny(e, t) {
  let n = sc(nc, new TextEncoder().encode("ed25519 seed"), e);
  try {
    for (const r of [44, 501, t, 0]) {
      const i = new Uint8Array(37);
      i.set(n.subarray(0, 32), 1), new DataView(i.buffer).setUint32(33, 2147483648 + r, !1);
      let o;
      try {
        o = sc(nc, n.subarray(32), i);
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
function Q1(e, t, n) {
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  const r = _n.fromMasterSeed(e);
  let i, o;
  try {
    if (i = r.derive(t), !i.privateKey) throw new Error("protected HD derivation failed");
    return o = new Uint8Array(i.privateKey), n(o);
  } finally {
    o?.fill(0), i?.wipePrivateData(), r.wipePrivateData();
  }
}
function ex(e, t, n) {
  const r = Jp(t);
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  if (r.chain !== "solana") return Q1(e, ss(r.chain, r.account), n);
  const i = ny(e, r.account);
  try {
    return n(i);
  } finally {
    i.fill(0);
  }
}
function yc(e) {
  if (e.length !== 64) throw new Error("protected HD seed must be 64 bytes");
  const t = _n.fromMasterSeed(e);
  try {
    return Array.from({ length: yl }, (n, r) => {
      const i = (s) => {
        const a = t.derive(s);
        try {
          if (!a.publicKey) throw new Error("protected HD derivation failed");
          return te(a.publicKey);
        } finally {
          a.wipePrivateData();
        }
      }, o = ny(e, r);
      try {
        return { account: r, evm: i(ss("evm", r)), bitcoin: i(ss("bitcoin", r)), solana: te(ft.getPublicKey(o)), bitcoin_p2pkh: i(`m/44'/0'/0'/0/${r}`), bitcoin_p2wpkh: i(`m/84'/0'/0'/0/${r}`) };
      } finally {
        o.fill(0);
      }
    });
  } finally {
    t.wipePrivateData();
  }
}
function gl(e) {
  const t = lt(Zt(e.evm)), n = te(Zo(we.Point.fromBytes(t).toBytes(!1).slice(1)).slice(-20)), r = te(Zo(new TextEncoder().encode(n))), i = (l) => el(le(lt(Zt(l)))), o = lt(Zt(e.bitcoin)).slice(1), s = BigInt(`0x${te(Sn.utils.taggedHash("TapTweak", o))}`);
  if (s >= we.Point.Fn.ORDER) throw new Error("invalid protected HD Taproot tweak");
  const a = we.Point.fromBytes(new Uint8Array([2, ...o])), c = s === 0n ? a : a.add(we.Point.BASE.multiply(s));
  if (c.equals(we.Point.ZERO)) throw new Error("invalid protected HD Taproot output");
  return {
    eth: "0x" + [...n].map((l, u) => parseInt(r[u], 16) >= 8 ? l.toUpperCase() : l).join(""),
    btc_legacy: y1(le).encode(new Uint8Array([0, ...i(e.bitcoin_p2pkh)])),
    btc_segwit: hu.encode("bc", [0, ...hu.toWords(i(e.bitcoin_p2wpkh))]),
    btc_taproot: pu.encode("bc", [1, ...pu.toWords(c.toBytes(!0).slice(1))]),
    solana: Rp.encode(lt(Zt(e.solana, !0)))
  };
}
function tx(e) {
  const t = JSON.stringify([e.ciphertext, e.nonce, e.wrap_meta]).replace(/[\u007f-\uffff]/g, (n) => "\\u" + n.charCodeAt(0).toString(16).padStart(4, "0"));
  return te(le(new TextEncoder().encode(t)));
}
function cs(e) {
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
  return r(t, 1), ht(t);
}
function Yn(e, t) {
  if (typeof e != "string" || e.length > Math.ceil(Math.max(...t) / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) throw new Error("invalid protected HD base64");
  const n = Uint8Array.from(atob(e), (r) => r.charCodeAt(0));
  if (!t.includes(n.length) || btoa(String.fromCharCode(...n)) !== e) throw new Error("invalid protected HD field length/encoding");
  return n;
}
function nx(e, t) {
  if (t.v !== 1 || t.scheme !== "machine-aesgcm-v1") throw new Error("invalid protected HD envelope version/scheme");
  Yn(e.nonce, [12]), Yn(e.ciphertext, [127, 131, 135, 139, 143]), Yn(t.dek_nonce, [12]), Yn(t.wrapped_dek, [48]);
}
const vu = { '"': 34, "\\": 92, "/": 47, b: 8, f: 12, n: 10, r: 13, t: 9 };
function rx(e) {
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
          if (!Object.hasOwn(vu, d)) return !1;
          u = vu[d];
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
const ix = "hpke-x25519-hkdfsha256-aes256gcm-v1", ox = "avault:blind-box:v1", sx = "avault:blind-box:aad:v1", ls = "machine-aesgcm-v1", Ks = 1, Fs = "ecdsa-secp256k1-recoverable", Bi = "ed25519", ml = "ecdsa-secp256k1-der", bl = "schnorr-secp256k1-bip340", $i = "schnorr-secp256k1-bip341-keypath", me = 32, ds = 12, js = 32, ax = "avault:protected-vmk:kek-passkey:v1", cx = /^[A-Za-z_][A-Za-z0-9_]*$/, lx = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[a-z][a-z0-9_]{0,63})?$/, dx = "avault:protected-root-meta:v1", ux = new TextEncoder(), fx = new TextDecoder();
function vl(e) {
  switch (e) {
    case Bi:
    case Fs:
    case ml:
    case bl:
    case $i:
      return e;
    default:
      throw new Error("unsupported signing scheme");
  }
}
function Yr(e) {
  if (e === void 0 || e === "secp256k1") return "secp256k1";
  if (e === "ed25519") return e;
  throw new Error("unsupported signing curve");
}
function xl(e) {
  if (vl(e), e === Bi) throw new Error("Ed25519 requires exact-message signing, not a digest");
}
function be(e, t = "bytes") {
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
function Ge(e) {
  const t = be(e), n = new ArrayBuffer(t.byteLength);
  return new Uint8Array(n).set(t), t.fill(0), n;
}
function Ne(e) {
  return ux.encode(e);
}
function us(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(us).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${us(t[n])}`).join(",")}}`;
}
function nr(e) {
  const t = new Uint8Array(e);
  return crypto.getRandomValues(t), t;
}
function ce(e) {
  const t = be(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return t.fill(0), btoa(n);
}
function Ae(e) {
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = t.padEnd(Math.ceil(t.length / 4) * 4, "=");
  return Uint8Array.from(atob(n), (r) => r.charCodeAt(0));
}
function tt(e) {
  return lt(e.startsWith("0x") ? e.slice(2) : e);
}
function xu(e) {
  return /^(0x)?[0-9a-fA-F]+$/.test(e) && (e.startsWith("0x"), e.length % 2 === 0) ? tt(e) : Ae(e);
}
function hx(e) {
  return te(be(e));
}
function ry(e, t = "bytes") {
  return be(e, t);
}
function Be(e, t, n) {
  if (e.length !== t)
    throw new Error(`${n} must be ${t} bytes`);
}
function dt(e) {
  e && new Uint8Array(e).fill(0);
}
async function fs(e, t, n, r) {
  let i, o, s, a;
  try {
    i = Ge(e), o = Ge(t), s = Ge(n), a = r ? Ge(r) : void 0;
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
    dt(i), dt(o), dt(s), dt(a);
  }
}
async function Vs(e, t, n, r) {
  let i, o, s, a;
  try {
    i = Ge(e), o = Ge(t), s = Ge(n), a = r ? Ge(r) : void 0;
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
    dt(i), dt(o), dt(s), dt(a);
  }
}
async function px(e, t, n, r) {
  let i, o, s;
  try {
    i = Ge(e), o = Ge(t), s = Ge(n);
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
    dt(i), dt(o), dt(s);
  }
}
async function iy(e, t) {
  const n = be(e, "passkey PRF output");
  try {
    const r = be(t, "passkey PRF salt");
    try {
      if (n.length === 0)
        throw new Error("passkey PRF output is empty");
      return Be(r, js, "passkey PRF salt"), await px(n, r, Ne(ax), me);
    } finally {
      r.fill(0);
    }
  } finally {
    n.fill(0);
  }
}
function yx() {
  return nr(me);
}
function wx() {
  return nr(js);
}
function gx(e) {
  const t = be(e, "passkey PRF salt");
  try {
    return Be(t, js, "passkey PRF salt"), { prf: { eval: { first: Ge(t) } } };
  } finally {
    t.fill(0);
  }
}
function gt(e) {
  const t = typeof e == "string" ? JSON.parse(e) : e;
  if (t?.v !== Ks || !Array.isArray(t.copies))
    throw new Error("invalid protected wrap_meta");
  return t;
}
async function mx(e, t) {
  const n = be(e, "VMK"), r = nr(ds);
  let i, o;
  try {
    Be(n, me, "VMK"), o = be(t.prfSalt, "passkey PRF salt"), Be(o, js, "passkey PRF salt"), i = await iy(t.prfOutput, o);
    const s = await fs(i, r, n);
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
async function bx(e, t) {
  const n = be(e, "VMK");
  try {
    if (Be(n, me, "VMK"), t.length === 0)
      throw new Error("at least one protected unlock factor is required");
    const r = [];
    for (const i of t)
      r.push(await mx(n, i));
    return JSON.stringify({ v: Ks, copies: r });
  } finally {
    n.fill(0);
  }
}
async function vx(e, t) {
  const n = Ae(e.prf_salt);
  if (t.prfSalt && ce(t.prfSalt) !== e.prf_salt)
    throw n.fill(0), new Error("passkey PRF salt does not match copy");
  const r = await iy(t.prfOutput, n);
  try {
    return await Vs(r, Ae(e.nonce), Ae(e.wrapped));
  } finally {
    r.fill(0), n.fill(0);
  }
}
async function oy(e, t) {
  const n = gt(e);
  for (const r of n.copies)
    try {
      if (r.kind === "passkey") {
        const i = await vx(r, t);
        try {
          return Be(i, me, "VMK"), i;
        } catch (o) {
          throw i.fill(0), o;
        }
      }
    } catch {
    }
  throw new Error("no protected VMK copy could be unwrapped");
}
function qs(e) {
  return gt(e).copies.filter((t) => t.kind === "passkey").map((t) => ({
    prfSalt: Ae(t.prf_salt),
    ...t.credential_id ? { credentialId: t.credential_id } : {}
  }));
}
function xx(e, t) {
  const n = gt(e);
  return JSON.stringify({
    ...n,
    rp_id: t.rpId,
    vault_user_handle: t.vaultUserHandle
  });
}
function sy(e) {
  const t = gt(e);
  return delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, JSON.stringify(t);
}
function kl(e) {
  if (e.kind === "hd-seed") {
    if (e.publicKey !== void 0 || e.curve !== void 0 || !e.hd) throw new Error("invalid protected HD context");
    return Ms(e.hd);
  }
  if (e.hd !== void 0) throw new Error("protected HD table requires HD metadata");
  if (!e.kind) {
    if (e.curve !== void 0) throw new Error("protected record curve requires keypair metadata");
    return;
  }
  const t = Yr(e.curve);
  if (e.kind === "static") {
    if (e.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (e.kind === "keypair") {
    if (typeof e.publicKey != "string" || e.publicKey.length === 0)
      throw new Error("keypair protected record requires a public key");
    return t === "ed25519" && Bl(e.publicKey), {
      kind: "keypair",
      public_key: e.publicKey.toLowerCase(),
      ...t === "ed25519" ? { curve: t } : {}
    };
  }
  throw new Error("unsupported protected record kind");
}
function kx(e) {
  if (e === void 0) return;
  if (typeof e != "object" || e === null)
    throw new Error("protected record metadata is invalid");
  const t = e;
  if (t.kind === "hd-seed") return Ms(t);
  if (t.curve !== void 0 && t.curve !== "ed25519")
    throw new Error("protected record metadata curve is invalid");
  if (t.kind === "static") {
    if (t.curve !== void 0) throw new Error("static protected records cannot carry a curve");
    return { kind: "static" };
  }
  if (t.kind === "keypair")
    return kl({ kind: "keypair", publicKey: t.public_key, curve: t.curve });
  throw new Error("protected record metadata kind is invalid");
}
function Gs(e, t) {
  return t?.kind === "hd-seed" ? { name: e, kind: "hd-seed", hd: Ms(t) } : {
    name: e,
    ...t ? { kind: t.kind } : {},
    ...t?.public_key ? { publicKey: t.public_key } : {},
    ...t?.curve ? { curve: t.curve } : {}
  };
}
function El(e) {
  return Ne(
    us({
      domain: "avault:protected-record:aad:v2",
      name: e.kind === "keypair" || e.kind === "hd-seed" ? dy(e.name) : Kr(e.name),
      scheme: e.scheme ?? ls,
      version: e.version ?? Ks,
      record: kl(e) ?? null
    })
  );
}
async function Al(e, t, n) {
  const r = be(t, "VMK"), i = nr(me);
  let o;
  try {
    Be(r, me, "VMK"), o = be(e, "value");
    const s = El(n), a = nr(ds), c = await fs(i, a, o, s), l = nr(ds), u = await fs(r, l, i, s);
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
async function ay(e, t, n) {
  const r = be(t, "VMK");
  try {
    Be(r, me, "VMK");
    const i = await Vs(
      r,
      Ae(e.dek_nonce),
      Ae(e.wrapped_dek),
      El(n)
    );
    try {
      return Be(i, me, "DEK"), i;
    } catch (o) {
      throw i.fill(0), o;
    }
  } finally {
    r.fill(0);
  }
}
async function ji(e, t, n) {
  const r = await ay(e, t, n);
  try {
    return await Vs(
      r,
      Ae(e.nonce),
      Ae(e.ciphertext),
      El(n)
    );
  } finally {
    r.fill(0);
  }
}
function Sl(e, t, n) {
  const r = gt(t);
  if ("dek_nonce" in r || "wrapped_dek" in r)
    throw new Error("vmk wrap_meta must not already carry a wrapped DEK");
  if ("scheme" in r && r.scheme !== ls)
    throw new Error("protected wrap_meta has unsupported scheme");
  const i = n ? kl(n) : void 0;
  return {
    ciphertext: e.ciphertext,
    nonce: e.nonce,
    wrap_meta: JSON.stringify({
      ...r,
      scheme: ls,
      ...i ? { record_meta: i } : {},
      dek_nonce: e.dek_nonce,
      wrapped_dek: e.wrapped_dek
    })
  };
}
function Un(e) {
  const t = rx(e.wrap_meta), n = gt(t ? cs(e.wrap_meta) : e.wrap_meta);
  if (n.record_meta?.kind === "hd-seed") {
    const c = t ? n : cs(e.wrap_meta);
    nx(e, c);
  }
  const { dek_nonce: r, wrapped_dek: i, record_meta: o, ...s } = n;
  if (typeof r != "string" || typeof i != "string")
    throw new Error("protected record wrap_meta is missing the wrapped DEK");
  const a = kx(o);
  return {
    sealed: { ciphertext: e.ciphertext, nonce: e.nonce, dek_nonce: r, wrapped_dek: i },
    vmkWrapMeta: JSON.stringify(s),
    ...a ? { recordMetadata: a } : {}
  };
}
function cy(e) {
  const t = { ...gt(e) };
  return delete t.root_meta, delete t.dek_nonce, delete t.wrapped_dek, delete t.record_meta, delete t.scheme, Ne(us({ domain: dx, vault: t }));
}
async function Ex(e, t, n) {
  const r = nr(ds), i = Ne(JSON.stringify(t));
  try {
    const o = await fs(e, r, i, cy(n));
    return { v: 1, nonce: ce(r), ciphertext: ce(o) };
  } finally {
    i.fill(0);
  }
}
async function hs(e, t) {
  const n = gt(e).root_meta;
  if (!n) return null;
  if (n.v !== 1 || typeof n.nonce != "string" || typeof n.ciphertext != "string")
    throw new Error("invalid protected root metadata");
  const r = await Vs(t, Ae(n.nonce), Ae(n.ciphertext), cy(e));
  try {
    const i = JSON.parse(fx.decode(r));
    if (typeof i != "object" || i === null) throw new Error("invalid protected root metadata");
    return i;
  } finally {
    r.fill(0);
  }
}
function Ax(e, t) {
  const n = gt(e);
  return JSON.stringify({ ...n, root_meta: t });
}
function Sx() {
  return new x0({
    kem: new P0(),
    kdf: new k0(),
    aead: new r0()
  });
}
function _l(e) {
  return Ae(typeof e == "string" ? e : e.public_key);
}
async function ly(e) {
  const t = await crypto.subtle.digest("SHA-256", Ge(e));
  return te(new Uint8Array(t));
}
async function _x(e) {
  return ly(_l(e));
}
function Ix(e) {
  if (!e.fingerprint)
    throw new Error("protected DEK release requires a pinned avault public key fingerprint");
  return e;
}
async function Bx(e) {
  const t = Ix(e), n = _l(t);
  if (Be(n, me, "avault public key"), await ly(n) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  return t;
}
async function $x(e, t, n) {
  const r = Sx(), i = _l(t);
  if (Be(i, me, "avault public key"), typeof t != "string" && t.fingerprint && await _x(t) !== t.fingerprint.toLowerCase())
    throw new Error("avault public key fingerprint mismatch");
  const o = await r.kem.deserializePublicKey(i), s = typeof e == "string" ? Ne(e) : be(e, "plaintext");
  try {
    const a = await r.seal(
      { recipientPublicKey: o, info: Ne(ox) },
      s,
      Ux(n)
    );
    return {
      scheme: ix,
      enc: ce(new Uint8Array(a.enc)),
      ct: ce(new Uint8Array(a.ct))
    };
  } finally {
    s.fill(0);
  }
}
function dy(e) {
  if (typeof e != "string" || !cx.test(e))
    throw new Error("vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$");
  return e;
}
function Kr(e) {
  if (typeof e != "string" || !lx.test(e))
    throw new Error("vault record name must match ^[A-Za-z_][A-Za-z0-9_]*(\\.[a-z][a-z0-9_]{0,63})?$");
  return e;
}
function ku(e, t) {
  if (typeof e != "string" || e.trim() === "")
    throw new Error(`${t} is required`);
  return e;
}
function Il(e) {
  const t = typeof e == "string" ? tt(e) : be(e, "digest");
  return Be(t, me, "digest"), t;
}
function Tx(e) {
  const t = typeof e == "string" ? tt(e) : be(e, "private key");
  try {
    if (Be(t, me, "private key"), !we.utils.isValidSecretKey(t))
      throw new Error("invalid secp256k1 private key");
    return t;
  } catch (n) {
    throw t.fill(0), n;
  }
}
function ps(e, t) {
  return typeof e == "string" ? tt(e) : be(e, t);
}
function ys(e) {
  const t = ps(e, "operation hash");
  return Be(t, me, "operation hash"), t;
}
function ws(e, t) {
  if (typeof e == "number" && (!Number.isSafeInteger(e) || e < 0))
    throw new Error(`${t} out of bounds`);
  const n = typeof e == "bigint" ? e : BigInt(e);
  if (n < 0n || n > 0xffffffffffffffffn)
    throw new Error(`${t} out of bounds`);
  return n;
}
function uy(e, t) {
  const n = ws(e, t), r = new Uint8Array(8);
  return new DataView(r.buffer).setBigUint64(0, n, !1), r;
}
function Px(e) {
  return uy(e, "approval expiry");
}
function Eu(e) {
  const t = be(e.nonce, "approval nonce");
  if (t.length < 16 || t.length > 128)
    throw new Error("approval nonce must be 16..128 bytes");
  return { approvalNonce: t, approvalExpiresAtUnix: ws(e.expiresAtUnix, "approval expiry") };
}
function Xe(e, t) {
  const n = be(t);
  if (n.length > 4294967295)
    throw new Error("blind-box AAD field is too large");
  e.push(n.length >>> 24 & 255, n.length >>> 16 & 255, n.length >>> 8 & 255, n.length & 255), e.push(...n);
}
function Ox(e) {
  switch (e.purpose) {
    case "seal":
      return Cx(e.name);
    case "agent-deliver": {
      const t = Eu({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-deliver",
        name: Kr(e.name),
        grantId: ku(e.grantId, "grant id"),
        ttlSecs: ws(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: ys(e.operationHash)
      };
    }
    case "agent-sign": {
      xl(e.signScheme);
      const t = Eu({
        nonce: e.approvalNonce,
        expiresAtUnix: e.approvalExpiresAtUnix
      });
      return {
        purpose: "agent-sign",
        name: dy(e.name),
        grantId: ku(e.grantId, "grant id"),
        signScheme: e.signScheme,
        digest: Il(e.digest),
        ttlSecs: ws(e.ttlSecs, "ttl seconds"),
        approvalNonce: t.approvalNonce,
        approvalExpiresAtUnix: t.approvalExpiresAtUnix,
        operationHash: ys(e.operationHash)
      };
    }
    default:
      throw new Error("unsupported blind-box context purpose");
  }
}
function Cx(e) {
  return { purpose: "seal", name: Kr(e) };
}
async function Rx(e) {
  const t = ys(e.operationHash), n = await Lx(e.name, e.ttlSecs);
  if (te(t) !== te(n))
    throw new Error("operation hash does not match release operation fields");
}
async function Hx(e) {
  const t = [];
  for (const n of e)
    Xe(t, typeof n == "string" ? Ne(n) : be(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(t)));
}
async function Lx(e, t) {
  return Hx(["agent-deliver", Kr(e), uy(t, "ttl seconds")]);
}
function Ux(e) {
  const t = Ox(e), n = [...Ne(sx)];
  return Xe(n, Ne(t.purpose)), Xe(n, Ne(t.name)), Xe(n, Ne(ls)), Xe(n, new Uint8Array([Ks])), Xe(n, t.purpose === "agent-deliver" || t.purpose === "agent-sign" ? Ne(t.grantId) : new Uint8Array()), Xe(n, new Uint8Array()), Xe(n, t.purpose === "agent-sign" ? Ne(t.signScheme) : new Uint8Array()), Xe(n, t.purpose === "agent-sign" ? Il(t.digest) : new Uint8Array()), Xe(n, t.purpose === "seal" ? new Uint8Array() : be(t.approvalNonce, "approval nonce")), Xe(n, t.purpose === "seal" ? new Uint8Array() : Px(t.approvalExpiresAtUnix)), Xe(n, t.purpose === "seal" ? new Uint8Array() : ys(t.operationHash)), new Uint8Array(n);
}
async function Nx(e, t, n, r, i) {
  if (r.kind !== void 0 && r.kind !== "static")
    throw new Error("protected DEK release requires a static protected record");
  if (Kr(r.name) !== Kr(i.name))
    throw new Error("protected DEK release context name does not match record name");
  await Rx(i);
  const o = await Bx(n), s = await ay(e, t, r);
  try {
    return await $x(s, o, i);
  } finally {
    s.fill(0);
  }
}
function Dx(e = "secp256k1") {
  if (Yr(e) === "ed25519") {
    const n = ft.utils.randomSecretKey();
    try {
      return { privateKey: n, publicKey: te(ft.getPublicKey(n)) };
    } catch (r) {
      throw n.fill(0), r;
    }
  }
  const t = we.utils.randomSecretKey();
  return { privateKey: t, publicKey: te(we.getPublicKey(t, !0)) };
}
function Au(e) {
  return tt(e.toString(16).padStart(me * 2, "0"));
}
function fy(e, t, n, r = {}) {
  if (xl(n), !r || typeof r != "object" || Array.isArray(r) || Object.keys(r).some((s) => s !== "schnorrAuxRand"))
    throw new Error("unsupported signing option; script trees are not supported");
  const i = Tx(e);
  let o;
  try {
    const s = Il(t);
    if (n === Fs) {
      const a = we.sign(s, i, { prehash: !1, lowS: !0, format: "recovered" });
      return {
        scheme: n,
        signature: te(a.slice(1)),
        recovery_id: a[0] ?? null
      };
    }
    if (n === ml)
      return {
        scheme: n,
        signature: te(we.sign(s, i, { prehash: !1, lowS: !0, format: "der" })),
        recovery_id: null
      };
    if (n === bl) {
      o = r.schnorrAuxRand !== void 0 ? ps(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(me)), Be(o, me, "schnorr aux randomness");
      const a = Sn.sign(s, i, o);
      if (!Sn.verify(a, s, Sn.getPublicKey(i))) throw new Error("signing self-verification failed");
      return { scheme: n, signature: te(a), recovery_id: null };
    }
    if (n === $i) {
      const a = we.getPublicKey(i, !0), c = $l(a.slice(1));
      let l = BigInt(`0x${te(i)}`);
      a[0] === 3 && (l = hi - l);
      const u = BigInt(`0x${te(Sn.utils.taggedHash("TapTweak", a.slice(1)))}`);
      if (u >= hi) throw new Error("invalid taproot tweak");
      let d = (l + u) % hi;
      if (d === 0n) throw new Error("invalid taproot signing key");
      let f = Au(d);
      try {
        c.toBytes(!0)[0] === 3 && (d = hi - d, f.fill(0), f = Au(d)), o = r.schnorrAuxRand !== void 0 ? ps(r.schnorrAuxRand, "schnorr aux randomness") : crypto.getRandomValues(new Uint8Array(me)), Be(o, me, "schnorr aux randomness");
        const h = Sn.sign(s, f, o);
        if (!Sn.verify(h, s, c.toBytes(!0).slice(1))) throw new Error("Taproot signing self-verification failed");
        return { scheme: n, signature: te(h), recovery_id: null };
      } finally {
        f.fill(0);
      }
    }
    throw new Error("unsupported signing scheme");
  } finally {
    o?.fill(0), i.fill(0);
  }
}
async function zx(e, t, n, r, i, o = {}) {
  if (n.kind !== "keypair" || !n.publicKey)
    throw new Error("protected signing requires a keypair protected record");
  if (xl(i), Yr(n.curve) !== "secp256k1") throw new Error("digest signing requires a secp256k1 protected record");
  const s = await ji(e, t, n);
  try {
    if (te(we.getPublicKey(s, !0)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return fy(s, r, i, o);
  } finally {
    s.fill(0);
  }
}
function hy(e, t) {
  const n = tl(t), r = ps(e, "Ed25519 seed");
  try {
    Be(r, me, "Ed25519 seed");
    const i = ft.sign(n, r);
    if (!ft.verify(i, n, ft.getPublicKey(r), { zip215: !1 }))
      throw new Error("Ed25519 signing self-verification failed");
    return { scheme: Bi, signature: te(i), recovery_id: null };
  } finally {
    r.fill(0);
  }
}
function Bl(e) {
  if (!/^[0-9a-fA-F]{64}$/.test(e)) throw new Error("expected a 32-byte Ed25519 public key");
  const t = tt(e);
  if (!ft.utils.isValidPublicKey(t, !1)) throw new Error("invalid Ed25519 public key");
  return t;
}
async function Mx(e, t, n, r) {
  if (n.kind !== "keypair" || n.curve !== "ed25519" || !n.publicKey)
    throw new Error("exact-message signing requires an Ed25519 keypair protected record");
  const i = tl(r);
  if (i.length === 0) throw new Error("protected Ed25519 message must not be empty");
  Bl(n.publicKey);
  const o = await ji(e, t, n);
  try {
    if (Be(o, me, "Ed25519 seed"), te(ft.getPublicKey(o)) !== n.publicKey.toLowerCase())
      throw new Error("protected signing key does not match record public key");
    return hy(o, i);
  } finally {
    o.fill(0);
  }
}
function Kx(e) {
  return wy(Bl(e));
}
function Fx(e) {
  const t = e.rootMetadata?.daemon?.verificationKeys?.find(
    (n) => n.alg === "ed25519" && n.keyId === e.keyId
  );
  if (!t)
    throw new Error("daemon verification key is not pinned");
  return ft.verify(xu(e.signature), Ne(e.message), xu(t.publicKey));
}
function py(e) {
  const t = tt(e.trim());
  if (t.length === 33 && (t[0] === 2 || t[0] === 3) && we.utils.isValidPublicKey(t))
    return t;
  if (t.length === 65 && t[0] === 4 && we.utils.isValidPublicKey(t))
    return we.Point.fromBytes(t).toBytes(!0);
  throw new Error("expected a valid secp256k1 public key");
}
const jx = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", Vx = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", qx = 1, Gx = 734539939, hi = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");
function Wx(e) {
  return el(le(e));
}
function yy(e) {
  const t = le(le(e)).slice(0, 4), n = new Uint8Array(e.length + t.length);
  return n.set(e), n.set(t, e.length), wy(n);
}
function wy(e) {
  let t = BigInt(`0x${te(e)}`), n = "";
  for (; t > 0n; ) {
    const i = Number(t % 58n);
    t /= 58n, n = jx[i] + n;
  }
  let r = 0;
  for (const i of e) {
    if (i !== 0) break;
    r += 1;
  }
  return "1".repeat(r) + n;
}
function Zx(e) {
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
function Yx(e) {
  return [...e].map((t) => t.charCodeAt(0) >>> 5).concat([0], [...e].map((t) => t.charCodeAt(0) & 31));
}
function Xx(e, t, n) {
  const r = Yx(e).concat(t), i = Zx(r.concat([0, 0, 0, 0, 0, 0])) ^ n;
  return [0, 1, 2, 3, 4, 5].map((o) => i >>> 5 * (5 - o) & 31);
}
function Jx(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >>> i & s);
  return i && o.push(r << n - i & s), o;
}
function wc(e, t, n) {
  const r = t === 0 ? qx : Gx, i = [t].concat(Jx(n, 8, 5));
  return `${e}1${i.concat(Xx(e, i, r)).map((o) => Vx[o]).join("")}`;
}
function gy(e) {
  const t = e.toLowerCase(), n = te(Zo(Ne(t)));
  return [...t].map((r, i) => /[a-f]/.test(r) && Number.parseInt(n[i], 16) >= 8 ? r.toUpperCase() : r).join("");
}
function $l(e) {
  const t = BigInt(`0x${te(Sn.utils.taggedHash("TapTweak", e))}`);
  if (t >= hi) throw new Error("invalid taproot tweak");
  const n = we.Point.fromBytes(new Uint8Array([2, ...e])), r = t === 0n ? n : n.add(we.Point.BASE.multiply(t));
  if (r.equals(we.Point.ZERO)) throw new Error("invalid taproot output key");
  return r;
}
function Qx(e) {
  return $l(py(e).slice(1)).toBytes(!0).slice(1);
}
function Ws(e) {
  const t = py(e), n = we.Point.fromBytes(t), r = n.toBytes(!1).slice(1), i = Zo(r), o = Wx(t), s = new Uint8Array(21);
  s[0] = 0, s.set(o, 1);
  const a = n.toBytes(!1).slice(1, 33), c = $l(a);
  return {
    eth: `0x${gy(te(i.slice(-20)))}`,
    btc_legacy: yy(s),
    btc_segwit: wc("bc", 0, o),
    btc_taproot: wc("bc", 1, c.toBytes(!1).slice(1, 33))
  };
}
const Tl = {
  windowSeconds: 600,
  strictApprovals: !1,
  parentValueSealAllowed: !0
};
let gc = { ...Tl };
function e2(e) {
  return e === 300 || e === 600 || e === 1800 ? e : Tl.windowSeconds;
}
function t2(e, t) {
  if (e === 300 || e === 600 || e === 1800) return e;
  throw new S("invalid_payload", `${t}.windowSeconds must be 300, 600, or 1800`);
}
function Pl(e, t = "policy") {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be an object`);
  const n = e;
  if (typeof n.strictApprovals != "boolean") throw new S("invalid_payload", `${t}.strictApprovals must be a boolean`);
  if (typeof n.parentValueSealAllowed != "boolean")
    throw new S("invalid_payload", `${t}.parentValueSealAllowed must be a boolean`);
  return {
    windowSeconds: t2(n.windowSeconds, t),
    strictApprovals: n.strictApprovals,
    parentValueSealAllowed: n.parentValueSealAllowed
  };
}
function n2(e) {
  if (typeof e != "object" || e === null) return { ...Tl };
  const t = e;
  return {
    windowSeconds: e2(t.windowSeconds),
    strictApprovals: t.strictApprovals === !0,
    parentValueSealAllowed: t.parentValueSealAllowed !== !1
  };
}
function Ol(e) {
  return gc = n2(e), gc;
}
function He() {
  return { ...gc };
}
const r2 = "avibe-vault-lock:v1:", Z = {
  vmk: null,
  wrapMeta: null,
  freshSetup: !1,
  scopeId: null
};
let Jt = null, Fr = null, rr = null, Ti = null, mc = null, tn = 0;
const gs = /* @__PURE__ */ new Set();
let ki = 0, my = null;
function i2(e) {
  my = e;
}
function Su() {
  return Z.vmk ? "unlocked" : Z.wrapMeta ? "locked" : "needs-setup";
}
function by(e) {
  my?.({
    state: Su(),
    ...Jt !== null && Su() === "unlocked" ? { expiresAt: Jt } : {},
    reason: e
  });
}
function Cl() {
  rr !== null && (clearTimeout(rr), rr = null);
}
function Pi() {
  Ti?.close(), Ti = null, mc = null;
}
function vy(e) {
  const t = new TextEncoder().encode(e);
  let n = "";
  for (const r of t)
    n += String.fromCharCode(r);
  return btoa(n).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function o2(e) {
  let t = 2166136261;
  for (let n = 0; n < e.length; n += 1)
    t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
  return t.toString(16).padStart(8, "0");
}
function xy(e) {
  const t = gt(e);
  return typeof t.vault_user_handle == "string" && t.vault_user_handle.length > 0 ? vy(t.vault_user_handle) : `wrap-${o2(e)}`;
}
function s2(e) {
  return vy(e);
}
function Rl(e) {
  if (typeof window > "u" || typeof BroadcastChannel > "u" || !e) {
    Pi();
    return;
  }
  const t = `${r2}${e}`;
  mc !== t && (Pi(), mc = t, Ti = new BroadcastChannel(t), Ti.onmessage = (n) => {
    const r = n.data;
    r?.type === "lock" && r.scopeId === e && mt({ broadcast: !1, reason: "manual-lock" });
  });
}
function a2() {
  Z.scopeId && (Rl(Z.scopeId), Ti?.postMessage({ type: "lock", scopeId: Z.scopeId }));
}
function Bt() {
  return new Error("vault-operation-aborted");
}
function ky() {
  tn += 1;
  for (const e of gs)
    e.abort(Bt());
  gs.clear();
  for (const e of st.keys()) e.abort(Bt());
  st.clear();
}
function Tn(e) {
  const t = { ...gt(e) };
  for (const n of ["scheme", "record_meta", "dek_nonce", "wrapped_dek"]) delete t[n];
  return t;
}
function Rr(e, t) {
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
const st = /* @__PURE__ */ new Map();
function Ey(e) {
  if (st.size === 0) return;
  const t = Tn(e);
  for (const [n, r] of st)
    Rr(r, t) || n.abort(Bt());
}
function Zs(e) {
  ms();
  const t = Tn(e);
  if (Z.wrapMeta && !Rr(Tn(Z.wrapMeta), t) || (pt(e), !Z.wrapMeta || !Rr(Tn(Z.wrapMeta), t))) throw Bt();
  for (const s of st.keys()) s.abort(Bt());
  st.clear();
  const n = ++ki, r = tn, i = bc(), o = new AbortController();
  return st.set(o, t), Rl(Z.scopeId), {
    freshSetup: i,
    signal: o.signal,
    assertCurrent: () => {
      if (ms(), o.signal.aborted || r !== tn || n !== ki || i !== bc() || !Z.wrapMeta || !Rr(Tn(Z.wrapMeta), t)) throw Bt();
    },
    lockIfCurrent: (s = {}) => o.signal.aborted || n !== ki ? !1 : Ay(r, t, s),
    release: () => {
      st.delete(o), o.abort(Bt()), !Z.vmk && st.size === 0 && Pi();
    }
  };
}
function Ay(e, t, n = {}) {
  return e !== tn || !Z.vmk || !Z.wrapMeta || !Rr(Tn(Z.wrapMeta), t) ? !1 : (mt(n), !0);
}
function c2() {
  Z.vmk?.fill(0), Z.vmk = null, Jt = null, Fr = null, Cl(), Pi(), Z.freshSetup && (Z.wrapMeta = null, Z.freshSetup = !1, Z.scopeId = null);
}
function l2() {
  return Jt !== null && Date.now() >= Jt;
}
function ms() {
  return Z.vmk && l2() ? (mt({ broadcast: !1, reason: "auto-lock" }), !1) : Z.vmk !== null;
}
function Sy(e) {
  const t = setTimeout(() => {
    rr !== t || Fr !== e || e.epoch !== tn || !Z.vmk || (rr = null, mt({ broadcast: !1, reason: "auto-lock" }));
  }, Math.max(0, e.expiresAt - Date.now()));
  rr = t;
}
function _y(e, t = He()) {
  if (!Z.vmk)
    throw new Error("vault VMK is not loaded");
  const n = t.windowSeconds * 1e3;
  return Jt = Date.now() + n, Fr = { epoch: tn, expiresAt: Jt, pendingCommits: 0 }, Cl(), Sy(Fr), by(e), Jt;
}
function d2() {
  const e = Fr;
  if (!Z.vmk || !e) return () => {
  };
  e.pendingCommits += 1, Cl();
  let t = !1;
  return () => {
    if (!t && (t = !0, e.pendingCommits -= 1, !(Fr !== e || e.epoch !== tn || !Z.vmk || e.pendingCommits !== 0 || rr !== null))) {
      if (e.expiresAt <= Date.now()) {
        mt({ broadcast: !1, reason: "auto-lock" });
        return;
      }
      Sy(e);
    }
  };
}
function u2(e) {
  if (e instanceof S && e.retryable) return !0;
  const t = e instanceof Error ? e.message : String(e);
  return t === "operation-cancelled" || t === "operation-superseded" || t === "passkey-cancelled" || t === "vault-operation-aborted";
}
function f2(e) {
  if (e.signal.aborted || e.epoch !== tn || !Z.vmk)
    throw Bt();
}
async function Ys(e, t = {}) {
  if (!ms() || !Z.vmk || !Z.wrapMeta)
    throw new Error("vault-locked");
  const n = Z.wrapMeta, r = t.expectedWrapMeta === void 0 ? void 0 : Tn(t.expectedWrapMeta);
  if (r !== void 0 && !Rr(Tn(n), r)) throw Bt();
  const i = new Uint8Array(Z.vmk), o = tn, s = ki, a = new AbortController();
  gs.add(a), r !== void 0 && st.set(a, r);
  const c = {
    epoch: o,
    signal: a.signal,
    assertCurrent: () => f2(c)
  };
  let l = null;
  try {
    c.assertCurrent();
    const u = await e(i, n, c);
    return c.assertCurrent(), t.renewOnSuccess !== !1 && _y("renew"), c.assertCurrent(), t.beforeSuccess && (l = d2(), await t.beforeSuccess(), c.assertCurrent(), l(), l = null), u;
  } catch (u) {
    throw u2(u) || (r !== void 0 ? !a.signal.aborted && s === ki && Ay(o, r, { broadcast: !0, reason: "manual-lock" }) : mt({ broadcast: !0, reason: "manual-lock" })), u;
  } finally {
    l?.(), gs.delete(a), st.delete(a), i.fill(0);
  }
}
function bc() {
  return Z.vmk !== null && Z.freshSetup;
}
function Iy(e) {
  ky(), Z.vmk?.fill(0), Z.vmk = e.vmk, Z.wrapMeta = e.wrapMeta, Z.freshSetup = e.freshSetup, Z.scopeId = e.scopeId, Rl(e.scopeId);
  const t = { state: "unlocked", expiresAt: _y("unlock", e.policy) };
  if (e.onCommitted) {
    const n = Zs(e.wrapMeta);
    try {
      e.onCommitted(n);
    } catch (r) {
      throw n.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), n.release(), r;
    }
  }
  return t;
}
function mt(e = {}) {
  return e.broadcast && a2(), ky(), c2(), by(e.reason ?? "manual-lock"), { state: "locked" };
}
function pt(e) {
  ms();
  const t = e ? sy(e) : void 0;
  return t && Ey(t), Z.vmk ? {
    state: "unlocked",
    expiresAt: Jt ?? void 0,
    ...Z.freshSetup ? { freshSetup: !0 } : {}
  } : (t && (Z.wrapMeta = t, Z.scopeId = xy(t)), Z.wrapMeta ? { state: "locked" } : { state: "needs-setup" });
}
function Hl(e) {
  const t = sy(e), n = xy(t);
  return Ey(t), Z.wrapMeta = t, Z.scopeId = n, { wrapMeta: t, scopeId: n };
}
function h2() {
  mt({ broadcast: !1, reason: "unload" }), Pi();
}
const mo = "Avibe Vault", _u = "avibe-vault";
function or(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Pn(e) {
  const t = new ArrayBuffer(e.byteLength);
  return new Uint8Array(t).set(e), t;
}
function By() {
  return Pn(crypto.getRandomValues(new Uint8Array(32)));
}
function $y(e) {
  try {
    return Ae(e);
  } catch {
    return null;
  }
}
function Xs(e, t) {
  if (e instanceof ArrayBuffer || ArrayBuffer.isView(e))
    return Pn(ry(e));
  if (typeof e == "string") {
    const n = $y(e);
    return Pn(n ?? new TextEncoder().encode(e));
  }
  if (Array.isArray(e))
    return Pn(Uint8Array.from(e));
  throw new Error("webauthn bytes are missing");
}
function Ta(e) {
  if (typeof e != "object" || e === null || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null)
    throw new Error("invalid webauthn creation options object");
  return e;
}
function p2(e) {
  if (typeof e != "string" || e.length === 0 || e.length > 88 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(e) || /[+/]/.test(e) && /[-_]/.test(e))
    throw new Error("invalid webauthn user.id encoding");
  const t = e.replace(/-/g, "+").replace(/_/g, "/"), n = $y(t);
  if (!n || n.byteLength === 0 || n.byteLength > 64)
    throw new Error("webauthn user.id must be 1-64 bytes");
  const r = ce(n);
  if (t !== r && t !== r.replace(/=+$/, ""))
    throw new Error("invalid webauthn user.id encoding");
  return Pn(n);
}
function y2(e) {
  const t = Xs(e);
  if (t.byteLength === 0 || t.byteLength > 64)
    throw new Error("vault user handle must be 1-64 bytes");
  return t;
}
function bs(e) {
  if (Array.isArray(e))
    return e.map((t) => {
      const n = or(t);
      if (!n || n.type !== "public-key")
        throw new Error("invalid WebAuthn credential descriptor");
      return {
        type: "public-key",
        id: Xs(n.id),
        ...Array.isArray(n.transports) ? { transports: n.transports } : {}
      };
    });
}
function Iu(e) {
  return !Array.isArray(e) || e.length === 0 ? [
    { type: "public-key", alg: -7 },
    { type: "public-key", alg: -257 }
  ] : e.map((t) => {
    const n = or(t);
    if (!n || n.type !== "public-key" || typeof n.alg != "number")
      throw new Error("invalid WebAuthn pubKeyCredParams");
    return { type: "public-key", alg: n.alg };
  });
}
function Bu(e) {
  return {
    ...or(e) ?? {},
    residentKey: "required",
    userVerification: "required"
  };
}
function $u(e) {
  if (e === "none" || e === "indirect" || e === "direct" || e === "enterprise") return e;
}
function w2(e) {
  const t = or(e) ?? {};
  return { ...t, prf: or(t.prf) ?? {} };
}
function Ut() {
  return window.location.hostname;
}
function g2(e) {
  const t = e.authzCreationOptions === void 0 ? void 0 : Ta(e.authzCreationOptions), n = t && Object.prototype.hasOwnProperty.call(t, "webauthn") ? Ta(t.webauthn) : t, r = typeof t?.challenge_id == "string" ? t.challenge_id : void 0, i = or(n?.rp), o = typeof i?.id == "string" ? i.id : void 0;
  if (o && o !== e.rpId)
    throw new Error("webauthn rp_id does not match sandbox host");
  if (!n)
    return {
      options: {
        rp: { name: mo, id: e.rpId },
        user: {
          id: y2(e.vaultUserHandle),
          name: e.vaultUserHandle || _u,
          displayName: e.displayName || mo
        },
        challenge: By(),
        pubKeyCredParams: Iu(void 0),
        authenticatorSelection: Bu(void 0),
        extensions: { prf: {} }
      }
    };
  const s = Ta(n.user);
  return {
    options: {
      rp: {
        name: typeof i?.name == "string" ? i.name : mo,
        id: e.rpId
      },
      user: {
        id: p2(s.id),
        name: typeof s.name == "string" ? s.name : e.vaultUserHandle || _u,
        displayName: typeof s.displayName == "string" ? s.displayName : e.displayName || mo
      },
      challenge: Xs(n.challenge),
      pubKeyCredParams: Iu(n.pubKeyCredParams),
      authenticatorSelection: Bu(n.authenticatorSelection),
      extensions: w2(n.extensions),
      ...bs(n.excludeCredentials) ? { excludeCredentials: bs(n.excludeCredentials) } : {},
      ...typeof n.timeout == "number" ? { timeout: n.timeout } : {},
      ...$u(n.attestation) ? { attestation: $u(n.attestation) } : {}
    },
    ...r ? { challengeId: r } : {}
  };
}
function m2(e) {
  if (e.length === 0) throw new Error("passkey-not-configured");
  if (e.length > 1) throw new Error("passkey-multiple-not-supported");
  return e[0];
}
function b2(e, t) {
  const n = m2(e), r = {
    challenge: By(),
    rpId: t,
    userVerification: "required",
    extensions: gx(n.prfSalt)
  };
  return n.credentialId && (r.allowCredentials = [
    {
      type: "public-key",
      id: Pn(Ae(n.credentialId))
    }
  ]), r;
}
function v2(e, t, n) {
  const r = {
    challenge: Pn(n),
    rpId: t,
    userVerification: "required"
  }, i = e.filter((o) => o.credentialId).map((o) => ({ type: "public-key", id: Pn(Ae(o.credentialId)) }));
  return i.length > 0 && (r.allowCredentials = i), r;
}
function x2(e, t = Ut()) {
  const n = or(e);
  if (!n) throw new Error("webauthn assertion options are missing");
  const r = typeof n.rpId == "string" ? n.rpId : typeof n.rp_id == "string" ? n.rp_id : void 0;
  if (r && r !== t)
    throw new Error("webauthn rpId does not match sandbox host");
  return {
    challenge: Xs(n.challenge),
    rpId: t,
    userVerification: "required",
    ...bs(n.allowCredentials) ? { allowCredentials: bs(n.allowCredentials) } : {},
    ...typeof n.timeout == "number" ? { timeout: n.timeout } : {}
  };
}
function Ty(e, t) {
  return {
    publicKey: e,
    ...t ? { signal: t } : {}
  };
}
function Py(e) {
  return {
    id: e.id,
    rawId: ce(e.rawId),
    type: e.type
  };
}
function k2(e) {
  const t = e.response, n = typeof t.getTransports == "function" ? t.getTransports() : [];
  return {
    ...Py(e),
    response: {
      clientDataJSON: ce(t.clientDataJSON),
      attestationObject: ce(t.attestationObject),
      transports: n
    }
  };
}
function Oy(e) {
  const t = e.response;
  return {
    ...Py(e),
    response: {
      clientDataJSON: ce(t.clientDataJSON),
      authenticatorData: ce(t.authenticatorData),
      signature: ce(t.signature),
      userHandle: t.userHandle ? ce(t.userHandle) : null
    }
  };
}
function E2(e) {
  const n = e?.getClientExtensionResults()?.prf?.results?.first;
  if (!n) throw new Error("passkey-prf-unavailable");
  const r = ry(n, "passkey PRF output");
  if (r.byteLength === 0) throw new Error("passkey-prf-unavailable");
  return r;
}
async function Js(e, t, n) {
  const r = await navigator.credentials.get(
    Ty(b2(e, t), n)
  );
  if (!r) throw new Error("passkey-cancelled");
  const i = E2(r), o = ce(r.rawId), s = e.find((a) => a.credentialId === o);
  return { prfOutput: i, prfSalt: s?.prfSalt ?? e[0].prfSalt, credentialId: o };
}
async function A2(e, t, n, r) {
  if (!await navigator.credentials.get(
    Ty(v2(e, t, n), r)
  )) throw new Error("passkey-cancelled");
}
async function S2(e) {
  const t = await navigator.credentials.get({
    publicKey: x2(e)
  });
  if (!t) throw new Error("passkey-cancelled");
  return Oy(t);
}
const _2 = /ancestor|not the same as its|cross-?origin|different origin/i;
function Cy(e) {
  return typeof e?.name == "string" ? e.name : "";
}
function I2(e) {
  return e instanceof Error || typeof e?.message == "string" ? e.message : typeof e == "string" ? e : "";
}
function Ry(e) {
  const t = Cy(e);
  return t === "SecurityError" || t === "NotSupportedError" || _2.test(I2(e));
}
function Hy(e) {
  return Cy(e) === "NotAllowedError" && !Ry(e);
}
function Ll(e) {
  const { options: t, challengeId: n } = g2(e);
  return navigator.credentials.create({
    publicKey: t
  }).then((i) => {
    if (!i) throw new Error("passkey-cancelled");
    const o = ce(i.rawId), s = k2(i), a = e.authzCreationOptions !== void 0;
    return {
      credentialId: o,
      ...a ? { authzRegistration: n ? { challenge_id: n, credential: s } : s } : {}
    };
  });
}
const vc = {
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
let vs = "en";
function B2(e) {
  return e === "zh" ? "zh" : "en";
}
function $2(e) {
  const t = B2(e);
  return t === vs ? !1 : (vs = t, !0);
}
function T2() {
  return vs;
}
function P2(e) {
  return typeof e == "string" && Object.hasOwn(vc.en, e);
}
function X(e, t = {}) {
  const n = vc[vs], r = vc.en;
  return (n[e] ?? r[e] ?? e).replace(/\{\{(\w+)\}\}/g, (o, s) => String(t[s] ?? ""));
}
function O2(e) {
  return JSON.stringify(e ?? {});
}
function Tu(e) {
  if (!e) return {};
  try {
    const t = JSON.parse(e);
    return typeof t == "object" && t !== null ? t : {};
  } catch {
    return {};
  }
}
function fe(e, t, n) {
  e.dataset.i18nKey = t, e.dataset.i18nParams = O2(n), e.textContent = X(t, n);
}
function Ce(e, t) {
  delete e.dataset.i18nKey, delete e.dataset.i18nParams, e.textContent = t;
}
function C2(e = document) {
  e.querySelectorAll("[data-i18n-key]").forEach((t) => {
    const n = t.dataset.i18nKey;
    n && (t.textContent = X(n, Tu(t.dataset.i18nParams)));
  }), e.querySelectorAll("[data-i18n-placeholder-key]").forEach((t) => {
    const n = t.dataset.i18nPlaceholderKey;
    n && (t.placeholder = X(n, Tu(t.dataset.i18nPlaceholderParams)));
  });
}
function Ly(e) {
  return {
    title: "policyDowngrade.title",
    subtitle: { text: e.target.name },
    confirmLabel: "policyDowngrade.confirm",
    policyDowngrade: e
  };
}
function $t(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
function Ye(e, t, n = "mono") {
  const r = $t("div", "review-field"), i = $t("span", "review-label");
  fe(i, e);
  const o = $t("div", "review-value"), s = $t("span", n === "word" ? "review-word" : "review-mono");
  return typeof t == "string" ? Ce(s, t) : fe(s, t.key, t.params), o.append(s), r.append(i, o), r;
}
function bo(e, t) {
  const n = $t("p", t);
  return fe(n, e), n;
}
function R2(e, t) {
  Ce(e, "");
  const { target: n } = t, r = $t("div", "review policy-downgrade-review");
  r.append(n.protection === "standard" ? bo("policyDowngrade.standardWarning", "review-warning") : bo("policyDowngrade.protectedNotice", "review-info"));
  const i = $t("div", "review-summary");
  i.append(
    Ye("policyDowngrade.setting", { key: "policyDowngrade.settingValue" }, "word"),
    Ye("policyDowngrade.key", n.name),
    Ye("policyDowngrade.tier", { key: n.protection === "standard" ? "policyDowngrade.standard" : "policyDowngrade.protected" }, "word")
  ), n.account !== null && i.append(
    Ye("policyDowngrade.account", n.accountName !== null ? { key: "policyDowngrade.accountNamed", params: { index: n.account, name: n.accountName } } : { key: "policyDowngrade.accountUnnamed", params: { index: n.account } }, "word"),
    Ye("policyDowngrade.appliesTo", { key: "policyDowngrade.appliesToValue" }, "word")
  ), r.append(i, bo("policyDowngrade.onlyPolicy", "confirmation-note"));
  const o = $t("details", "review-details"), s = $t("summary", "review-details-toggle");
  fe(s, "policyDowngrade.details");
  const a = $t("div", "review-details-body"), c = [
    ["eth", "policyDowngrade.eth"],
    ["btc_legacy", "policyDowngrade.btcLegacy"],
    ["btc_segwit", "policyDowngrade.btcSegwit"],
    ["btc_taproot", "policyDowngrade.btcTaproot"],
    ["solana", "policyDowngrade.solana"]
  ];
  a.append(bo("policyDowngrade.serverFacts", "confirmation-note"));
  for (const [l, u] of c) {
    const d = n.addresses[l];
    d !== null ? a.append(Ye(u, d)) : n.signerKind === "hd-seed" && a.append(Ye(u, { key: "policyDowngrade.notUsed" }));
  }
  a.append(
    Ye("policyDowngrade.recordId", n.secretId),
    Ye("policyDowngrade.identity", n.identitySha256),
    Ye("policyDowngrade.revision", t.change.revision),
    Ye("policyDowngrade.expires", t.expiresAt),
    Ye("policyDowngrade.challengeId", t.challengeId)
  ), o.append(s, a), r.append(o), e.append(r);
}
const Uy = {
  sol: { symbol: "SOL", baseUnit: "lamports", decimals: 9 },
  eth: { symbol: "ETH", baseUnit: "wei", decimals: 18 },
  btc: { symbol: "BTC", baseUnit: "sats", decimals: 8 }
}, Ul = /^(?:0|[1-9][0-9]*)$/;
function Ny(e, t) {
  if (t === 0) return e;
  const n = e.padStart(t + 1, "0"), r = n.slice(-t).replace(/0+$/, "");
  return `${n.slice(0, -t)}${r ? `.${r}` : ""}`;
}
function Pu(e, t, n) {
  const { symbol: r, baseUnit: i, decimals: o } = Uy[t];
  if (!Ul.test(e) || n === "base") return `${e} ${i}`;
  const s = `${Ny(e, o)} ${r}`;
  return n === "symbol" ? s : `${s} (${e} ${i})`;
}
const H2 = 253402300799n;
function L2(e) {
  if (!Ul.test(e) || e.length > 12 || BigInt(e) > H2) return null;
  const t = new Date(Number(e) * 1e3).toISOString(), n = t.slice(17, 19);
  return `${t.slice(0, 10)} ${t.slice(11, 16)}${n === "00" ? "" : `:${n}`} UTC`;
}
const xs = 8, ks = 6, Ou = 28;
function Cu(e, t) {
  if (!t || e.length <= xs + ks + 2) {
    const r = ie("span", "review-mono");
    return Ce(r, e), r;
  }
  const n = ie("button", "review-mono review-elided");
  return n.type = "button", n.setAttribute("aria-expanded", "false"), n.title = e, Ce(n, `${e.slice(0, xs)}…${e.slice(-ks)}`), n.addEventListener("click", () => {
    n.getAttribute("aria-expanded") !== "true" && (n.setAttribute("aria-expanded", "true"), Ce(n, e));
  }), n;
}
function ie(e, t) {
  const n = document.createElement(e);
  return n.className = t, n;
}
const U2 = "http://www.w3.org/2000/svg";
function vo(e, t) {
  const n = document.createElementNS(U2, e);
  for (const [r, i] of Object.entries(t)) n.setAttribute(r, i);
  return n;
}
function Ru() {
  const e = vo("svg", {
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
    vo("path", { d: "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" }),
    vo("path", { d: "M12 9v4.5" }),
    vo("path", { d: "M12 17h.01" })
  ), e;
}
function N2(e) {
  if (!e.notes?.length) return null;
  const t = ie("span", "review-notes");
  for (const n of e.notes) {
    const r = ie("span", "review-note");
    typeof n == "string" ? fe(r, n) : fe(r, n.key, { count: n.count }), t.append(r);
  }
  return t;
}
function Pa(e, t = "review-label") {
  const n = ie(e.heading ? "p" : "span", t), r = {
    ...e.index === void 0 ? {} : { index: e.index },
    ...e.name === void 0 ? {} : { name: e.name }
  };
  return fe(n, e.label, Object.keys(r).length > 0 ? r : void 0), n;
}
function D2(e, t) {
  const n = ie("button", "review-copy");
  return n.type = "button", fe(n, "review.copy"), n.addEventListener("click", () => {
    const r = () => {
      window.getSelection()?.selectAllChildren(e), fe(n, "review.copySelected");
    };
    let i;
    try {
      i = navigator.clipboard.writeText(t);
    } catch {
      r();
      return;
    }
    i.then(() => fe(n, "review.copied"), r);
  }), n;
}
function xo(e, t = !1) {
  if (e.heading) return Pa(e, "review-subheading");
  const n = ie("div", e.raw ? "review-field review-field-raw" : "review-field"), r = N2(e);
  if (e.raw) {
    const c = ie("div", "review-raw-head"), l = ie("pre", "review-raw");
    return Ce(l, e.value), c.append(Pa(e)), r && c.append(r), c.append(D2(l, e.value)), n.append(c, l), n;
  }
  const i = ie("div", "review-value"), o = e.decimals !== void 0 && Ul.test(e.value) ? Ny(e.value, e.decimals) : null, s = e.time ? L2(e.value) : null, a = t && e.shownAs ? 0 : e.native ? Pu(e.value, e.native, e.unit).length : o !== null || e.time ? Ou + 1 : t && e.elide && e.value.length > xs + ks + 2 ? xs + ks + 1 : e.value.length;
  if (t && e.to === void 0 && a + (e.secondary?.length ?? 0) <= Ou && n.classList.add("review-field-inline"), t && e.shownAs) {
    const c = ie("span", "review-word");
    fe(c, e.shownAs), i.append(c);
  } else if (e.native) {
    const c = ie("span", "review-mono");
    Ce(c, Pu(e.value, e.native, e.unit)), i.append(c);
  } else if (o !== null) {
    const c = ie("span", "review-mono");
    Ce(c, o);
    const l = ie("span", "review-exact");
    fe(l, "review.amount.baseUnitsDecimals", { value: e.value, count: e.decimals }), i.append(c, l);
  } else if (e.time) {
    const c = ie("span", "review-mono");
    Ce(c, s ?? e.value);
    const l = ie("span", "review-exact");
    s === null ? fe(l, "review.time.noDate") : fe(l, "review.time.raw", { value: e.value }), i.append(c, l);
  } else
    i.append(Cu(e.value, t && e.elide === !0));
  if (e.secondary !== void 0) {
    const c = ie("span", "review-mono review-secondary");
    Ce(c, e.secondary), i.append(c);
  }
  if (e.to !== void 0) {
    const c = ie("span", "review-arrow");
    Ce(c, "→"), i.append(c, Cu(e.to, t));
  }
  return r && i.append(r), n.append(Pa(e), i), n;
}
function Hu(e, t, n) {
  const r = ie("section", "review-group"), i = ie("h2", "review-group-title");
  return fe(i, e, t === void 0 ? void 0 : { index: t }), r.append(i, ...n), r;
}
function z2(e, t, n = {}) {
  Ce(e, "");
  const r = ie("div", "review"), i = ie("div", "review-heading"), o = ie("p", "review-kind");
  if (fe(o, t.kind), i.append(o), n.session) {
    const u = ie("p", "review-session"), d = ie("span", "review-session-label");
    fe(d, "context.session");
    const f = ie("span", "review-session-value");
    Ce(f, n.session), u.append(d, f), i.append(u);
  }
  r.append(i);
  for (const u of t.warnings ?? []) {
    const d = ie("p", "review-warning"), f = ie("span", "review-warning-text");
    fe(f, u), d.append(Ru(), f), r.append(d);
  }
  if (t.undecoded) {
    const u = ie("p", "review-notice"), d = ie("span", "review-notice-text");
    fe(d, "sign.blindNotice"), u.append(Ru(), d), r.append(u);
  }
  const s = ie("div", "review-summary");
  s.append(...t.summary.map((u) => xo(u, !0)), xo(t.signer, !0)), r.append(s);
  const a = ie("details", "review-details"), c = ie("summary", "review-details-toggle");
  fe(c, "review.details");
  const l = ie("div", "review-details-body");
  t.details.forEach((u, d) => {
    const f = u.fields.map((h) => xo(h));
    l.append(Hu(u.title, u.index, d === 0 ? [xo(t.signer), ...f] : f));
  }), n.rows && l.append(Hu("review.group.request", void 0, [n.rows])), a.append(c, l), r.append(a), e.append(r);
}
function Se(e) {
  throw new Error(`signing review is malformed: ${e}`);
}
function Es(e, t, n) {
  (typeof e != "object" || e === null || Array.isArray(e)) && Se(`${n} must be an object`);
  const r = e;
  for (const i of Object.keys(r)) t.includes(i) || Se(`${n}.${i} is not a review field`);
  return r;
}
function Xn(e, t, n) {
  return (!P2(e) || !e.startsWith(`${t}.`)) && Se(`${n} must be a ${t} translation key`), e;
}
function xc(e, t) {
  if (e !== void 0)
    return (typeof e != "number" || !Number.isSafeInteger(e) || e < 0) && Se(`${t} must be a non-negative integer`), e;
}
function pi(e, t) {
  return Array.isArray(e) || Se(`${t} must be a list`), e;
}
const M2 = 4096, K2 = /^[A-Za-z_][A-Za-z0-9_]{0,63} \([a-z][a-z0-9[\]]{0,31}\)$/;
function Oa(e, t) {
  const n = Es(e, ["label", "index", "name", "value", "native", "unit", "decimals", "time", "to", "secondary", "elide", "shownAs", "notes", "raw", "heading"], t), r = xc(n.index, `${t}.index`);
  n.name !== void 0 && (typeof n.name != "string" || (n.label === "review.field.typedField" ? n.name.length > M2 : !K2.test(n.name))) && Se(`${t}.name is not a valid field name`), typeof n.value != "string" && Se(`${t}.value must be a string`), n.native !== void 0 && !Object.hasOwn(Uy, n.native) && Se(`${t}.native is not a native currency`), n.unit !== void 0 && (n.native === void 0 || n.unit !== "symbol" && n.unit !== "base") && Se(`${t}.unit is not a unit of its currency`), n.decimals !== void 0 && (typeof n.decimals != "number" || !Number.isInteger(n.decimals) || n.decimals < 0 || n.decimals > 255 || n.native !== void 0) && Se(`${t}.decimals must be an encoded decimal count`), n.time !== void 0 && (n.time !== "unix-seconds" || n.native !== void 0 || n.decimals !== void 0) && Se(`${t}.time is not a time form`), n.to !== void 0 && typeof n.to != "string" && Se(`${t}.to must be a string`), n.secondary !== void 0 && typeof n.secondary != "string" && Se(`${t}.secondary must be a string`), n.elide !== void 0 && typeof n.elide != "boolean" && Se(`${t}.elide must be a boolean`);
  const i = n.shownAs === void 0 ? void 0 : Xn(n.shownAs, "review.value", `${t}.shownAs`);
  n.raw !== void 0 && typeof n.raw != "boolean" && Se(`${t}.raw must be a boolean`), n.heading !== void 0 && typeof n.heading != "boolean" && Se(`${t}.heading must be a boolean`), n.heading && (n.value !== "" || n.native !== void 0 || n.decimals !== void 0 || n.time !== void 0 || n.notes !== void 0 || n.raw !== void 0 || n.to !== void 0 || n.secondary !== void 0 || n.elide !== void 0 || i !== void 0) && Se(`${t} is a heading and cannot carry a value`);
  const o = n.notes === void 0 ? void 0 : pi(n.notes, `${t}.notes`).map((s, a) => {
    const c = `${t}.notes[${a}]`;
    if (typeof s == "string") return Xn(s, "review.note", c);
    const l = Es(s, ["key", "count"], c), u = xc(l.count, `${c}.count`);
    return u === void 0 && Se(`${c}.count is required`), { key: Xn(l.key, "review.note", `${c}.key`), count: u };
  });
  return {
    label: Xn(n.label, "review.field", `${t}.label`),
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
function Dy(e) {
  const t = Es(e, ["kind", "signer", "summary", "undecoded", "warnings", "details"], "review");
  typeof t.undecoded != "boolean" && Se("review.undecoded must be a boolean");
  const n = t.warnings === void 0 ? void 0 : pi(t.warnings, "review.warnings").map((r, i) => Xn(r, "review.warning", `review.warnings[${i}]`));
  return {
    kind: Xn(t.kind, "review.kind", "review.kind"),
    signer: Oa(t.signer, "review.signer"),
    summary: pi(t.summary, "review.summary").map((r, i) => Oa(r, `review.summary[${i}]`)),
    undecoded: t.undecoded,
    ...n === void 0 ? {} : { warnings: n },
    details: pi(t.details, "review.details").map((r, i) => {
      const o = `review.details[${i}]`, s = Es(r, ["title", "index", "fields"], o), a = xc(s.index, `${o}.index`);
      return {
        title: Xn(s.title, "review.group", `${o}.title`),
        ...a === void 0 ? {} : { index: a },
        fields: pi(s.fields, `${o}.fields`).map((c, l) => Oa(c, `${o}.fields[${l}]`))
      };
    })
  };
}
let Nl = null, Dl = 0;
function F2(e) {
  Nl = e;
}
function zy() {
  return Date.now() < Dl;
}
function j2() {
  Dl = Date.now() + 150, Nl?.("ui.show");
}
function V2() {
  Dl = 0, Nl?.("ui.hide");
}
function q2() {
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
function My(e) {
  e.querySelectorAll(".dynamic").forEach((t) => t.remove());
}
function xe(e) {
  return { text: e };
}
function $e(e, t) {
  return { key: e, params: t };
}
function Y(e, t) {
  if (typeof t == "string") {
    fe(e, t);
    return;
  }
  if ("text" in t) {
    Ce(e, t.text);
    return;
  }
  fe(e, t.key, t.params);
}
function Qs(e, t, n) {
  const r = q2();
  return document.body.classList.add("interactive"), document.body.classList.remove("confirming"), j2(), r.card.classList.remove("confirm-card"), Y(r.title, e), Y(r.subtitle, t), n ? Y(r.body, n) : Ce(r.body, ""), My(r.card), r;
}
function ea() {
  document.body.classList.remove("interactive"), document.body.classList.remove("confirming");
  const e = document.getElementById("page")?.querySelector(".card");
  e && (e.classList.remove("confirm-card"), My(e)), V2();
}
function J(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic"), (e.querySelector(".card-scroll") ?? e).append(t);
}
function Lu(e, t) {
  t instanceof HTMLElement && t.classList.add("dynamic");
  const n = e.querySelector(".card-footer");
  if (!n) throw new Error("sandbox confirmation footer is unavailable");
  n.append(t);
}
function G2(e) {
  const t = document.createElement("div");
  if (t.className = "detail-value", e.variant === "chips") {
    t.classList.add("detail-chips");
    for (const r of e.values) {
      const i = document.createElement("span");
      i.className = `detail-chip${e.tone === "warning" ? " detail-chip-warning" : ""}`, Y(i, r), t.append(i);
    }
    return t;
  }
  const n = document.createElement(e.variant === "command" ? "code" : "span");
  return n.className = e.variant === "command" ? "detail-command" : e.variant === "mono" ? "detail-mono" : "detail-text", Y(n, e.value), e.variant === "mono" && (n.title = n.textContent ?? ""), t.append(n), t;
}
function Ky(e) {
  const t = document.createElement("div");
  t.className = "confirmation-details";
  for (const n of e.rows) {
    const r = document.createElement("div");
    r.className = "detail-row";
    const i = document.createElement("span");
    i.className = "detail-label", Y(i, n.label), r.append(i, G2(n)), t.append(r);
  }
  return t;
}
function W2(e, t, n) {
  if (Ce(e, ""), e.append(Ky(t)), n) {
    const r = document.createElement("p");
    r.className = "confirmation-note", Y(r, n), e.append(r);
  }
}
function Tt(e) {
  const t = document.createElement("button");
  return t.type = "button", t.className = "action dynamic", Y(t, e), t;
}
function As(e = xe("")) {
  const t = document.createElement("p");
  return t.className = "setup-status dynamic", Y(t, e), t;
}
let ko = null;
function zl() {
  return new Error("operation-superseded");
}
function Ei(e) {
  const t = e.reason;
  return t instanceof Error ? t : zl();
}
function Uu(e) {
  if (e?.aborted) throw Ei(e);
}
function lr(e) {
  ko?.abort(zl());
  const t = new AbortController();
  return ko = t, Promise.resolve().then(() => e(t.signal)).finally(() => {
    ko === t && (ko = null);
  });
}
async function Z2(e) {
  return lr(async (t) => {
    Uu(t), Uu(e.abortSignal);
    const n = new TextDecoder().decode(e.plaintext), r = Qs("plaintext.title", xe(e.name), "plaintext.body"), i = document.createElement("pre");
    i.className = "plaintext dynamic", i.textContent = n;
    const o = Tt("common.done"), s = Tt("plaintext.copy");
    s.classList.add("secondary");
    const a = As("plaintext.copyWarning");
    J(r.card, i), J(r.card, a), J(r.card, s), J(r.card, o);
    try {
      await new Promise((c, l) => {
        if (t.aborted) {
          l(Ei(t));
          return;
        }
        if (e.abortSignal?.aborted) {
          l(Ei(e.abortSignal));
          return;
        }
        let u = !1, d = () => {
        }, f = null, h = null, y = !e.prepareCopy;
        const w = () => {
          f?.abort(zl());
        }, p = (C) => {
          u || (u = !0, d(), w(), C());
        }, g = (C) => {
          p(() => l(Ei(C)));
        }, _ = () => g(t), k = () => {
          e.abortSignal && g(e.abortSignal);
        };
        d = () => {
          t.removeEventListener("abort", _), e.abortSignal?.removeEventListener("abort", k);
        }, t.addEventListener("abort", _, { once: !0 }), e.abortSignal?.addEventListener("abort", k, { once: !0 }), s.addEventListener("click", () => {
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
          const C = new AbortController();
          f = C;
          const B = () => {
            try {
              return Promise.resolve(e.onCopy ? e.onCopy(n, C.signal) : navigator.clipboard.writeText(n));
            } catch (L) {
              return Promise.reject(L);
            }
          }, R = e.prepareCopy && !y ? Promise.resolve(e.prepareCopy(C.signal)).then(() => {
            C.signal.aborted || t.aborted || e.abortSignal?.aborted || (y = !0, Y(a, "plaintext.copyReady"), s.disabled = !1);
          }) : B().then(() => {
            C.signal.aborted || t.aborted || e.abortSignal?.aborted || (e.prepareCopy && (y = !1), Y(a, "plaintext.copiedBody"), s.disabled = !1);
          });
          h = R, R.then(
            () => {
              C.signal.aborted;
            },
            (L) => {
              if (!(C.signal.aborted || u)) {
                if (t.aborted) {
                  g(t);
                  return;
                }
                if (e.abortSignal?.aborted) {
                  g(e.abortSignal);
                  return;
                }
                s.disabled = !1, Y(a, "plaintext.copyFailed");
              }
            }
          ).finally(() => {
            f === C && (f = null), h === R && (h = null);
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
      ea();
    }
  });
}
function Oi(e, t, n, r) {
  const i = Qs(e.title, e.subtitle, e.review ? void 0 : e.body);
  document.body.classList.add("confirming"), i.card.classList.add("confirm-card"), e.policyDowngrade ? R2(i.body, e.policyDowngrade) : e.review ? z2(i.body, e.review, {
    session: e.reviewSession,
    rows: e.details && Ky(e.details)
  }) : e.details && W2(i.body, e.details, e.body);
  const o = Tt(e.confirmLabel), s = Tt("common.cancel");
  return s.classList.add("secondary"), Lu(i.card, s), Lu(i.card, o), o.disabled = !0, new Promise((a, c) => {
    let l = !1, u;
    const d = setTimeout(() => {
      try {
        u = n?.(i.card);
      } catch (w) {
        h(() => c(w));
        return;
      }
      (u?.ready ?? Promise.resolve()).then(
        () => {
          !l && !t.aborted && (o.disabled = !1);
        },
        (w) => h(() => c(w))
      );
    }, 500), f = () => {
      h(() => c(Ei(t)));
    }, h = (y) => {
      l || (l = !0, clearTimeout(d), u?.dispose(), t.removeEventListener("abort", f), ea(), y());
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
        u?.assertCurrent(), y = r ? r() : Promise.resolve();
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
function Ss(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function Y2(e) {
  return new Promise((t, n) => {
    const r = () => n(Ss(e));
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function Nu(e) {
  if (e?.aborted) throw Ss(e);
}
async function Fy(e) {
  const t = Hl(e.wrapMeta), n = qs(t.wrapMeta);
  let r;
  try {
    const i = await Promise.race([
      Js(n, e.currentRpId, e.abortSignal).then(
        (o) => {
          if (e.abortSignal?.aborted)
            throw o.prfOutput.fill(0), Ss(e.abortSignal);
          return o;
        },
        (o) => {
          throw e.abortSignal?.aborted ? Ss(e.abortSignal) : o;
        }
      ),
      ...e.abortSignal ? [Y2(e.abortSignal)] : []
    ]);
    return r = i.prfOutput, await jy({
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
async function jy(e) {
  const t = Hl(e.wrapMeta);
  let n;
  try {
    Nu(e.abortSignal), n = await oy(t.wrapMeta, {
      kind: "passkey",
      prfOutput: e.assertion.prfOutput,
      prfSalt: e.assertion.prfSalt
    }), Nu(e.abortSignal);
    const r = Iy({
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
function X2(e) {
  if (typeof e != "object" || e === null)
    throw new S("invalid_payload", "request payload must be an object");
  return e;
}
function J2(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new S("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Du(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Q2(e) {
  const t = X2(e), n = t.kind === "keypair" ? "keypair" : t.kind === "static" ? "static" : null;
  if (!n) throw new S("invalid_payload", "kind must be static or keypair");
  const r = Du(t.name, "name");
  if (t.rootMetadata !== void 0) throw new S("invalid_payload", "seal cannot set vault root metadata");
  const i = J2(t.wrapMeta, "wrapMeta");
  if (n === "static") {
    if (t.curve !== void 0) throw new S("invalid_payload", "static seal cannot carry a curve");
    if (t.inputMode !== "parent-value")
      throw new S("invalid_payload", "static seal inputMode must be parent-value");
    return {
      name: r,
      kind: "static",
      inputMode: "parent-value",
      value: Du(t.value, "value"),
      wrapMeta: i
    };
  }
  if (t.inputMode !== void 0)
    throw new S("invalid_payload", "keypair seal is generate-only and does not accept inputMode");
  if (t.value !== void 0)
    throw new S("invalid_payload", "keypair seal cannot accept parent-provided private key material");
  let o;
  try {
    o = Yr(t.curve);
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
function Xr(e) {
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
const ek = 512, Vy = "avibe-vault-signed-context-replay:v2", tk = "avibe-vault-signed-context-replay:v2", nk = "avibe-vault-signed-context-replay:v2", ut = /* @__PURE__ */ new Map();
let zu = !1, yi = null;
function it(e) {
  return typeof e == "object" && e !== null;
}
function Gt(e, t) {
  if (typeof e != "string" || e.length === 0) throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function ii(e, t) {
  if (e !== void 0) {
    if (typeof e != "string") throw new S("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Ca(e, t) {
  if (e !== void 0) {
    if (!Array.isArray(e) || !e.every((n) => typeof n == "string"))
      throw new S("invalid_payload", `${t} must be a string array`);
    return e;
  }
}
function rk(e, t) {
  if (e !== "static" && e !== "keypair") throw new S("invalid_payload", `${t} must be static or keypair`);
  return e;
}
function ik(e) {
  if (e !== "agent-deliver" && e !== "sign" && e !== "reveal")
    throw new S("invalid_payload", "context purpose is invalid");
  return e;
}
function kc(e, t) {
  if (typeof e != "number" || !Number.isSafeInteger(e) || e < 0)
    throw new S("invalid_payload", `${t} must be a non-negative integer`);
  return e;
}
function ok(e, t) {
  if (!Array.isArray(e) || e.length < 16 || e.length > 128 || !e.every((n) => Number.isInteger(n) && n >= 0 && n <= 255))
    throw new S("invalid_payload", `${t} must contain 16..128 byte integers`);
  return e;
}
function Ml(e, t) {
  if (!it(e)) throw new S(t, "agent delivery context.release must be an object");
  return {
    name: Gt(e.name, "context.release.name"),
    ttlSecs: kc(e.ttlSecs, "context.release.ttlSecs"),
    approvalNonce: ok(e.approvalNonce, "context.release.approvalNonce"),
    approvalExpiresAtUnix: kc(e.approvalExpiresAtUnix, "context.release.approvalExpiresAtUnix"),
    operationHash: Gt(e.operationHash, "context.release.operationHash")
  };
}
function pe(e) {
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(pe).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${pe(t[n])}`).join(",")}}`;
}
function Kl(e) {
  const t = it(e) ? e : null;
  if (!t) throw new S("invalid_payload", "context must be an object");
  if (t.v !== 2) throw new S("invalid_payload", "context version must be 2");
  const n = ik(t.purpose);
  Gt(t.requestId, "context.requestId"), t.grantId !== void 0 && ii(t.grantId, "context.grantId");
  const r = it(t.display) ? t.display : null;
  if (!r) throw new S("invalid_payload", "context.display must be an object");
  if (!Array.isArray(r.secrets) || r.secrets.length === 0)
    throw new S("invalid_payload", "context.display.secrets must be a non-empty array");
  r.secrets.forEach((c, l) => {
    if (!it(c)) throw new S("invalid_payload", `context.display.secrets[${l}] must be an object`);
    Gt(c.name, `context.display.secrets[${l}].name`), rk(c.kind, `context.display.secrets[${l}].kind`);
  }), r.sessionLabel !== void 0 && ii(r.sessionLabel, "context.display.sessionLabel"), r.command !== void 0 && ii(r.command, "context.display.command"), r.egress !== void 0 && ii(r.egress, "context.display.egress");
  const i = r.source;
  if (i !== void 0) {
    if (!it(i)) throw new S("invalid_payload", "context.display.source must be an object");
    i.env !== void 0 && Ca(i.env, "context.display.source.env"), i.tags !== void 0 && Ca(i.tags, "context.display.source.tags"), i.skills !== void 0 && Ca(i.skills, "context.display.source.skills");
  }
  r.grantTtlSeconds !== void 0 && kc(r.grantTtlSeconds, "context.display.grantTtlSeconds");
  const o = it(t.signature) ? t.signature : null;
  if (!o) throw new S("invalid_payload", "context.signature must be an object");
  if (o.alg !== "ed25519") throw new S("invalid_payload", "context signature alg must be ed25519");
  Gt(o.keyId, "context.signature.keyId"), Gt(o.value, "context.signature.value");
  const s = t.agent;
  if (s !== void 0) {
    if (!it(s)) throw new S("invalid_payload", "context.agent must be an object");
    const c = it(s.publicKey) ? s.publicKey : null;
    if (!c) throw new S("invalid_payload", "context.agent.publicKey must be an object");
    Gt(c.public_key, "context.agent.publicKey.public_key"), c.fingerprint !== void 0 && ii(c.fingerprint, "context.agent.publicKey.fingerprint"), Gt(s.fingerprint, "context.agent.fingerprint");
  }
  const a = t.release;
  if (n === "agent-deliver") {
    const c = Ml(a, "invalid_payload");
    if (r.grantTtlSeconds === void 0)
      throw new S("invalid_payload", "agent delivery context must display its release TTL");
    if (r.grantTtlSeconds !== c.ttlSecs)
      throw new S("invalid_payload", "signed release TTL does not match display TTL");
  } else if (a !== void 0 && !it(a))
    throw new S("invalid_payload", "context.release must be an object");
  return Gt(t.expiresAt, "context.expiresAt"), t;
}
function ta(e) {
  const { signature: t, ...n } = e;
  return pe(n);
}
function _s(e) {
  if (e.context.purpose !== e.expectedPurpose)
    throw new S("invalid_context", `${e.expectedPurpose} context has the wrong purpose`);
  if (!e.context.display.secrets.some(
    (n) => n.name === e.secret.name && n.kind === e.secret.kind
  )) {
    const n = e.secret.kind === "keypair" ? "key" : "secret";
    throw new S("invalid_context", `${e.expectedPurpose} context does not cover the requested ${n}`);
  }
}
function Jn(e) {
  if (e.expectedPurpose && e.context.purpose !== e.expectedPurpose)
    throw new S("invalid_context", "signed context purpose does not match operation");
  const t = e.now ?? Date.now(), n = Date.parse(e.context.expiresAt);
  if (!Number.isFinite(n) || n <= t)
    throw new S("context_expired", "signed context is expired", !0);
  if (!Fx({
    rootMetadata: e.rootMetadata,
    keyId: e.context.signature.keyId,
    signature: e.context.signature.value,
    message: ta(e.context)
  })) throw new S("invalid_context_signature", "signed context signature is invalid");
  if (e.context.purpose === "agent-deliver" && Ml(e.context.release, "invalid_context").approvalExpiresAtUnix * 1e3 <= t)
    throw new S("context_expired", "signed release approval is expired", !0);
}
function Fl(e) {
  for (const [t, n] of ut)
    n <= e && ut.delete(t);
}
function sk() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
function jr() {
  return typeof window < "u";
}
function jl() {
  return new S("context_replay_state_unavailable", "signed context replay state is unavailable", !0);
}
function qy(e) {
  const t = sk();
  if (!t && e && jr()) throw jl();
  return t;
}
function Gy(e, t) {
  const n = /* @__PURE__ */ new Map();
  if (!it(e) || e.version !== 2 || !Array.isArray(e.entries)) return n;
  for (const r of e.entries) {
    if (!Array.isArray(r) || r.length !== 2) continue;
    const [i, o] = r;
    typeof i != "string" || i.length === 0 || typeof o != "number" || !Number.isFinite(o) || o > t && n.set(i, Math.max(n.get(i) ?? 0, o));
  }
  return n;
}
function Vl(e = Date.now(), t = !1) {
  Fl(e);
  const n = qy(t);
  if (n)
    try {
      n.setItem(Vy, JSON.stringify({ version: 2, entries: [...ut] }));
    } catch {
      if (t && jr()) throw jl();
    }
}
function Wy(e, t = Date.now(), n = !0) {
  for (const [r, i] of e)
    i > t && ut.set(r, Math.max(ut.get(r) ?? 0, i));
  n && Vl(t);
}
function ql() {
  if (!(yi || typeof BroadcastChannel > "u"))
    try {
      yi = new BroadcastChannel(tk), yi.addEventListener("message", (e) => {
        const t = it(e.data) ? e.data : null;
        !t || t.type !== "signed-context-consumed" || Wy(Gy({ version: 2, entries: t.entries }, Date.now()));
      });
    } catch {
      yi = null;
    }
}
function ak(e = Date.now()) {
  zu || (ql(), Zy(e, jr()), zu = !0);
}
function Zy(e = Date.now(), t = !1) {
  ql();
  const n = qy(t);
  if (n)
    try {
      Wy(Gy(JSON.parse(n.getItem(Vy) ?? "null"), e), e, !1), Fl(e);
    } catch {
      if (t && jr()) throw jl();
      Vl(e);
    }
}
function ck(e) {
  ql();
  try {
    yi?.postMessage({ type: "signed-context-consumed", entries: e });
  } catch {
  }
}
function lk() {
  const t = globalThis.navigator?.locks;
  return !t || typeof t != "object" || typeof t.request != "function" ? null : t;
}
async function dk(e) {
  const t = lk();
  if (t) return await t.request(nk, { mode: "exclusive" }, e);
  if (typeof window < "u")
    throw new S("context_replay_lock_unavailable", "signed context replay lock is unavailable", !0);
  return await e();
}
function Yy(e, t) {
  ak(t);
  const n = /* @__PURE__ */ new Map();
  for (const r of e) {
    const i = Date.parse(r.expiresAt);
    if (!Number.isFinite(i) || i <= t)
      throw new S("context_expired", "signed context is expired", !0);
    n.set(r.requestId, Math.max(n.get(r.requestId) ?? 0, i));
  }
  Fl(t);
  for (const r of n.keys())
    if (ut.has(r))
      throw new S("context_replayed", "signed context requestId was already used", !0);
  if (ut.size + n.size > ek)
    throw new S("context_replay_cache_full", "signed context replay cache is full", !0);
  return n;
}
function Ci(e, t = Date.now()) {
  Yy(e, t);
}
async function Ri(e, t = Date.now()) {
  const n = [...e];
  await dk(() => {
    Zy(t, jr());
    const r = Yy(n, t), i = /* @__PURE__ */ new Map();
    for (const o of r.keys()) i.set(o, ut.get(o));
    for (const [o, s] of r) ut.set(o, s);
    try {
      Vl(t, jr());
    } catch (o) {
      for (const [s, a] of i)
        a === void 0 ? ut.delete(s) : ut.set(s, a);
      throw o;
    }
    ck([...r]);
  });
}
async function uk(e, t) {
  if (e.purpose !== "agent-deliver") throw new S("invalid_context", "context is not an agent delivery");
  if (!e.grantId) throw new S("invalid_context", "agent delivery context is missing grantId");
  const n = Ml(e.release, "invalid_context");
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
function fk(e) {
  if (!e.agent) throw new S("invalid_context", "agent delivery context is missing agent key");
  if (e.agent.publicKey.fingerprint && e.agent.publicKey.fingerprint !== e.agent.fingerprint)
    throw new S("invalid_context", "agent public key fingerprint mismatch");
  return { ...e.agent.publicKey, fingerprint: e.agent.fingerprint };
}
async function hk(e) {
  const t = e.map((n) => ta(n));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(pe(t))));
}
function Mu(e) {
  return pe(e.display);
}
const pk = "1.2.3";
let nt = class Ec extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof Ec ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof Ec && n.cause.docsPath || n.docsPath, o = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...i ? [`Docs: https://abitype.dev${i}`] : [],
      ...r ? [`Details: ${r}`] : [],
      `Version: abitype@${pk}`
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
function nn(e, t) {
  return e.exec(t)?.groups;
}
const Xy = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, Jy = /^u?int(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/, Qy = /^\(.+?\).*?$/, Ku = /^tuple(?<array>(\[(\d*)\])*)$/;
function Ac(e) {
  let t = e.type;
  if (Ku.test(e.type) && "components" in e) {
    t = "(";
    const n = e.components.length;
    for (let i = 0; i < n; i++) {
      const o = e.components[i];
      t += Ac(o), i < n - 1 && (t += ", ");
    }
    const r = nn(Ku, e.type);
    return t += `)${r?.array || ""}`, Ac({
      ...e,
      type: t
    });
  }
  return "indexed" in e && e.indexed && (t = `${t} indexed`), e.name ? `${t} ${e.name}` : t;
}
function oi(e) {
  let t = "";
  const n = e.length;
  for (let r = 0; r < n; r++) {
    const i = e[r];
    t += Ac(i), r !== n - 1 && (t += ", ");
  }
  return t;
}
function yk(e) {
  return e.type === "function" ? `function ${e.name}(${oi(e.inputs)})${e.stateMutability && e.stateMutability !== "nonpayable" ? ` ${e.stateMutability}` : ""}${e.outputs?.length ? ` returns (${oi(e.outputs)})` : ""}` : e.type === "event" ? `event ${e.name}(${oi(e.inputs)})` : e.type === "error" ? `error ${e.name}(${oi(e.inputs)})` : e.type === "constructor" ? `constructor(${oi(e.inputs)})${e.stateMutability === "payable" ? " payable" : ""}` : e.type === "fallback" ? `fallback() external${e.stateMutability === "payable" ? " payable" : ""}` : "receive() external payable";
}
const ew = /^error (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function wk(e) {
  return ew.test(e);
}
function gk(e) {
  return nn(ew, e);
}
const tw = /^event (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)$/;
function mk(e) {
  return tw.test(e);
}
function bk(e) {
  return nn(tw, e);
}
const nw = /^function (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?<parameters>.*?)\)(?: (?<scope>external|public{1}))?(?: (?<stateMutability>pure|view|nonpayable|payable{1}))?(?: returns\s?\((?<returns>.*?)\))?$/;
function vk(e) {
  return nw.test(e);
}
function xk(e) {
  return nn(nw, e);
}
const rw = /^struct (?<name>[a-zA-Z$_][a-zA-Z0-9$_]*) \{(?<properties>.*?)\}$/;
function iw(e) {
  return rw.test(e);
}
function kk(e) {
  return nn(rw, e);
}
const ow = /^constructor\((?<parameters>.*?)\)(?:\s(?<stateMutability>payable{1}))?$/;
function Ek(e) {
  return ow.test(e);
}
function Ak(e) {
  return nn(ow, e);
}
const sw = /^fallback\(\) external(?:\s(?<stateMutability>payable{1}))?$/;
function Sk(e) {
  return sw.test(e);
}
function _k(e) {
  return nn(sw, e);
}
const Ik = /^receive\(\) external payable$/;
function Bk(e) {
  return Ik.test(e);
}
const $k = /* @__PURE__ */ new Set(["indexed"]), Sc = /* @__PURE__ */ new Set([
  "calldata",
  "memory",
  "storage"
]);
class Tk extends nt {
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
class Pk extends nt {
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
class Ok extends nt {
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
class Ck extends nt {
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
class Rk extends nt {
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
class Hk extends nt {
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
class Lk extends nt {
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
class Jr extends nt {
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
class Uk extends nt {
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
class Nk extends nt {
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
class Dk extends nt {
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
class zk extends nt {
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
function Mk(e, t, n) {
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
const Ra = /* @__PURE__ */ new Map([
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
function Kk(e, t = {}) {
  if (vk(e))
    return Fk(e, t);
  if (mk(e))
    return jk(e, t);
  if (wk(e))
    return Vk(e, t);
  if (Ek(e))
    return qk(e, t);
  if (Sk(e))
    return Gk(e);
  if (Bk(e))
    return {
      type: "receive",
      stateMutability: "payable"
    };
  throw new Uk({ signature: e });
}
function Fk(e, t = {}) {
  const n = xk(e);
  if (!n)
    throw new Jr({ signature: e, type: "function" });
  const r = at(n.parameters), i = [], o = r.length;
  for (let a = 0; a < o; a++)
    i.push(sr(r[a], {
      modifiers: Sc,
      structs: t,
      type: "function"
    }));
  const s = [];
  if (n.returns) {
    const a = at(n.returns), c = a.length;
    for (let l = 0; l < c; l++)
      s.push(sr(a[l], {
        modifiers: Sc,
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
function jk(e, t = {}) {
  const n = bk(e);
  if (!n)
    throw new Jr({ signature: e, type: "event" });
  const r = at(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(sr(r[s], {
      modifiers: $k,
      structs: t,
      type: "event"
    }));
  return { name: n.name, type: "event", inputs: i };
}
function Vk(e, t = {}) {
  const n = gk(e);
  if (!n)
    throw new Jr({ signature: e, type: "error" });
  const r = at(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(sr(r[s], { structs: t, type: "error" }));
  return { name: n.name, type: "error", inputs: i };
}
function qk(e, t = {}) {
  const n = Ak(e);
  if (!n)
    throw new Jr({ signature: e, type: "constructor" });
  const r = at(n.parameters), i = [], o = r.length;
  for (let s = 0; s < o; s++)
    i.push(sr(r[s], { structs: t, type: "constructor" }));
  return {
    type: "constructor",
    stateMutability: n.stateMutability ?? "nonpayable",
    inputs: i
  };
}
function Gk(e) {
  const t = _k(e);
  if (!t)
    throw new Jr({ signature: e, type: "fallback" });
  return {
    type: "fallback",
    stateMutability: t.stateMutability ?? "nonpayable"
  };
}
const Wk = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, Zk = /^\((?<type>.+?)\)(?<array>(?:\[\d*?\])+?)?(?:\s(?<modifier>calldata|indexed|memory|storage{1}))?(?:\s(?<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?$/, Yk = /^u?int$/;
function sr(e, t) {
  const n = Mk(e, t?.type, t?.structs);
  if (Ra.has(n))
    return Ra.get(n);
  const r = Qy.test(e), i = nn(r ? Zk : Wk, e);
  if (!i)
    throw new Ok({ param: e });
  if (i.name && Jk(i.name))
    throw new Ck({ param: e, name: i.name });
  const o = i.name ? { name: i.name } : {}, s = i.modifier === "indexed" ? { indexed: !0 } : {}, a = t?.structs ?? {};
  let c, l = {};
  if (r) {
    c = "tuple";
    const d = at(i.type), f = [], h = d.length;
    for (let y = 0; y < h; y++)
      f.push(sr(d[y], { structs: a }));
    l = { components: f };
  } else if (i.type in a)
    c = "tuple", l = { components: a[i.type] };
  else if (Yk.test(i.type))
    c = `${i.type}256`;
  else if (i.type === "address payable")
    c = "address";
  else if (c = i.type, t?.type !== "struct" && !aw(c))
    throw new Pk({ type: c });
  if (i.modifier) {
    if (!t?.modifiers?.has?.(i.modifier))
      throw new Rk({
        param: e,
        type: t?.type,
        modifier: i.modifier
      });
    if (Sc.has(i.modifier) && !Qk(c, !!i.array))
      throw new Hk({
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
  return Ra.set(n, u), u;
}
function at(e, t = [], n = "", r = 0) {
  const i = e.trim().length;
  for (let o = 0; o < i; o++) {
    const s = e[o], a = e.slice(o + 1);
    switch (s) {
      case ",":
        return r === 0 ? at(a, [...t, n.trim()]) : at(a, t, `${n}${s}`, r);
      case "(":
        return at(a, t, `${n}${s}`, r + 1);
      case ")":
        return at(a, t, `${n}${s}`, r - 1);
      default:
        return at(a, t, `${n}${s}`, r);
    }
  }
  if (n === "")
    return t;
  if (r !== 0)
    throw new zk({ current: n, depth: r });
  return t.push(n.trim()), t;
}
function aw(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || Xy.test(e) || Jy.test(e);
}
const Xk = /^(?:after|alias|anonymous|apply|auto|byte|calldata|case|catch|constant|copyof|default|defined|error|event|external|false|final|function|immutable|implements|in|indexed|inline|internal|let|mapping|match|memory|mutable|null|of|override|partial|private|promise|public|pure|reference|relocatable|return|returns|sizeof|static|storage|struct|super|supports|switch|this|true|try|typedef|typeof|var|view|virtual)$/;
function Jk(e) {
  return e === "address" || e === "bool" || e === "function" || e === "string" || e === "tuple" || Xy.test(e) || Jy.test(e) || Xk.test(e);
}
function Qk(e, t) {
  return t || e === "bytes" || e === "string" || e === "tuple";
}
function eE(e) {
  const t = {}, n = e.length;
  for (let s = 0; s < n; s++) {
    const a = e[s];
    if (!iw(a))
      continue;
    const c = kk(a);
    if (!c)
      throw new Jr({ signature: a, type: "struct" });
    const l = c.properties.split(";"), u = [], d = l.length;
    for (let f = 0; f < d; f++) {
      const y = l[f].trim();
      if (!y)
        continue;
      const w = sr(y, {
        type: "struct"
      });
      u.push(w);
    }
    if (!u.length)
      throw new Nk({ signature: a });
    t[c.name] = u;
  }
  const r = {}, i = Object.entries(t), o = i.length;
  for (let s = 0; s < o; s++) {
    const [a, c] = i[s];
    r[a] = cw(c, t);
  }
  return r;
}
const tE = /^(?<type>[a-zA-Z$_][a-zA-Z0-9$_]*)(?<array>(?:\[\d*?\])+?)?$/;
function cw(e = [], t = {}, n = /* @__PURE__ */ new Set()) {
  const r = [], i = e.length;
  for (let o = 0; o < i; o++) {
    const s = e[o];
    if (Qy.test(s.type))
      r.push(s);
    else {
      const c = nn(tE, s.type);
      if (!c?.type)
        throw new Lk({ abiParameter: s });
      const { array: l, type: u } = c;
      if (u in t) {
        if (n.has(u))
          throw new Dk({ type: u });
        r.push({
          ...s,
          type: `tuple${l ?? ""}`,
          components: cw(t[u], t, /* @__PURE__ */ new Set([...n, u]))
        });
      } else if (aw(u))
        r.push(s);
      else
        throw new Tk({ type: u });
    }
  }
  return r;
}
function nE(e) {
  const t = eE(e), n = [], r = e.length;
  for (let i = 0; i < r; i++) {
    const o = e[i];
    iw(o) || n.push(Kk(o, t));
  }
  return n;
}
function lw(e, { includeName: t = !1 } = {}) {
  return e ? e.map((n) => rE(n, { includeName: t })).join(t ? ", " : ",") : "";
}
function rE(e, { includeName: t }) {
  return e.type.startsWith("tuple") ? `(${lw(e.components, { includeName: t })})${e.type.slice(5)}` : e.type + (t && e.name ? ` ${e.name}` : "");
}
function Vi(e, { strict: t = !0 } = {}) {
  return !e || typeof e != "string" ? !1 : t ? /^0x[0-9a-fA-F]*$/.test(e) : e.startsWith("0x");
}
function Ee(e) {
  return Vi(e, { strict: !1 }) ? Math.ceil((e.length - 2) / 2) : e.length;
}
const dw = "2.54.6";
let Ha = {
  getDocsUrl: ({ docsBaseUrl: e, docsPath: t = "", docsSlug: n }) => t ? `${e ?? "https://viem.sh"}${t}${n ? `#${n}` : ""}` : void 0,
  version: `viem@${dw}`
};
class ee extends Error {
  constructor(t, n = {}) {
    const r = n.cause instanceof ee ? n.cause.details : n.cause?.message ? n.cause.message : n.details, i = n.cause instanceof ee && n.cause.docsPath || n.docsPath, o = Ha.getDocsUrl?.({ ...n, docsPath: i }), s = [
      t || "An error occurred.",
      "",
      ...n.metaMessages ? [...n.metaMessages, ""] : [],
      ...o ? [`Docs: ${o}`] : [],
      ...r ? [`Details: ${r}`] : [],
      ...Ha.version ? [`Version: ${Ha.version}`] : []
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
    }), this.details = r, this.docsPath = i, this.metaMessages = n.metaMessages, this.name = n.name ?? this.name, this.shortMessage = t, this.version = dw;
  }
  walk(t) {
    return uw(this, t);
  }
}
function uw(e, t) {
  return t?.(e) ? e : e && typeof e == "object" && "cause" in e && e.cause !== void 0 ? uw(e.cause, t) : t ? null : e;
}
class iE extends ee {
  constructor({ data: t, params: n, size: r }) {
    super([`Data size of ${r} bytes is too small for given parameters.`].join(`
`), {
      metaMessages: [
        `Params: (${lw(n, { includeName: !0 })})`,
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
class oE extends ee {
  constructor({ cause: t } = {}) {
    super('Cannot decode zero data ("0x") with ABI parameters.', {
      name: "AbiDecodingZeroDataError",
      cause: t
    });
  }
}
class sE extends ee {
  constructor({ expectedLength: t, givenLength: n, type: r }) {
    super([
      `ABI encoding array length mismatch for type ${r}.`,
      `Expected length: ${t}`,
      `Given length: ${n}`
    ].join(`
`), { name: "AbiEncodingArrayLengthMismatchError" });
  }
}
class aE extends ee {
  constructor({ expectedSize: t, value: n }) {
    super(`Size of bytes "${n}" (bytes${Ee(n)}) does not match expected size (bytes${t}).`, { name: "AbiEncodingBytesSizeMismatchError" });
  }
}
class cE extends ee {
  constructor({ expectedLength: t, givenLength: n }) {
    super([
      "ABI encoding params/values length mismatch.",
      `Expected length (params): ${t}`,
      `Given length (values): ${n}`
    ].join(`
`), { name: "AbiEncodingLengthMismatchError" });
  }
}
class lE extends ee {
  constructor({ expectedSize: t, givenSize: n }) {
    super(`Expected bytes${t}, got bytes${n}.`, {
      name: "BytesSizeMismatchError"
    });
  }
}
class dE extends ee {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid encoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiEncodingType" });
  }
}
class uE extends ee {
  constructor(t, { docsPath: n }) {
    super([
      `Type "${t}" is not a valid decoding type.`,
      "Please provide a valid ABI type."
    ].join(`
`), { docsPath: n, name: "InvalidAbiDecodingType" });
  }
}
class fE extends ee {
  constructor(t) {
    super([`Value "${t}" is not a valid array.`].join(`
`), {
      name: "InvalidArrayError"
    });
  }
}
class fw extends ee {
  constructor({ offset: t, position: n, size: r }) {
    super(`Slice ${n === "start" ? "starting" : "ending"} at offset "${t}" is out-of-bounds (size: ${r}).`, { name: "SliceOffsetOutOfBoundsError" });
  }
}
class hw extends ee {
  constructor({ size: t, targetSize: n, type: r }) {
    super(`${r.charAt(0).toUpperCase()}${r.slice(1).toLowerCase()} size (${t}) exceeds padding size (${n}).`, { name: "SizeExceedsPaddingSizeError" });
  }
}
function Qr(e, { dir: t, size: n = 32 } = {}) {
  return typeof e == "string" ? On(e, { dir: t, size: n }) : hE(e, { dir: t, size: n });
}
function On(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  const r = e.replace("0x", "");
  if (r.length > n * 2)
    throw new hw({
      size: Math.ceil(r.length / 2),
      targetSize: n,
      type: "hex"
    });
  return `0x${r[t === "right" ? "padEnd" : "padStart"](n * 2, "0")}`;
}
function hE(e, { dir: t, size: n = 32 } = {}) {
  if (n === null)
    return e;
  if (e.length > n)
    throw new hw({
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
class Gl extends ee {
  constructor({ max: t, min: n, signed: r, size: i, value: o }) {
    super(`Number "${o}" is not in safe ${i ? `${i * 8}-bit ${r ? "signed" : "unsigned"} ` : ""}integer range ${t ? `(${n} to ${t})` : `(above ${n})`}`, { name: "IntegerOutOfRangeError" });
  }
}
class pE extends ee {
  constructor(t) {
    super(`Bytes value "${t}" is not a valid boolean. The bytes array must contain a single byte of either a 0 or 1 value.`, {
      name: "InvalidBytesBooleanError"
    });
  }
}
class yE extends ee {
  constructor({ givenSize: t, maxSize: n }) {
    super(`Size cannot exceed ${n} bytes. Given size: ${t} bytes.`, { name: "SizeOverflowError" });
  }
}
function Hi(e, { dir: t = "left" } = {}) {
  let n = typeof e == "string" ? e.replace("0x", "") : e, r = 0;
  for (let i = 0; i < n.length - 1 && n[t === "left" ? i : n.length - i - 1].toString() === "0"; i++)
    r++;
  return n = t === "left" ? n.slice(r) : n.slice(0, n.length - r), typeof e == "string" ? (n.length === 1 && t === "right" && (n = `${n}0`), `0x${n.length % 2 === 1 ? `0${n}` : n}`) : n;
}
function Nt(e, { size: t }) {
  if (Ee(e) > t)
    throw new yE({
      givenSize: Ee(e),
      maxSize: t
    });
}
function pw(e, t = {}) {
  const { signed: n } = t;
  t.size && Nt(e, { size: t.size });
  const r = BigInt(e);
  if (!n)
    return r;
  const i = (e.length - 2) / 2, o = (1n << BigInt(i) * 8n - 1n) - 1n;
  return r <= o ? r : r - BigInt(`0x${"f".padStart(i * 2, "f")}`) - 1n;
}
function Wl(e, t = {}) {
  const n = pw(e, t), r = Number(n);
  if (!Number.isSafeInteger(r))
    throw new Gl({
      max: `${Number.MAX_SAFE_INTEGER}`,
      min: `${Number.MIN_SAFE_INTEGER}`,
      signed: t.signed,
      size: t.size,
      value: `${n}n`
    });
  return r;
}
const wE = /* @__PURE__ */ Array.from({ length: 256 }, (e, t) => t.toString(16).padStart(2, "0"));
function Li(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? Q(e, t) : typeof e == "string" ? ww(e, t) : typeof e == "boolean" ? yw(e, t) : je(e, t);
}
function yw(e, t = {}) {
  const n = `0x${Number(e)}`;
  return typeof t.size == "number" ? (Nt(n, { size: t.size }), Qr(n, { size: t.size })) : n;
}
function je(e, t = {}) {
  let n = "";
  for (let i = 0; i < e.length; i++)
    n += wE[e[i]];
  const r = `0x${n}`;
  return typeof t.size == "number" ? (Nt(r, { size: t.size }), Qr(r, { dir: "right", size: t.size })) : r;
}
function Q(e, t = {}) {
  const { signed: n, size: r } = t, i = BigInt(e);
  let o;
  r ? n ? o = (1n << BigInt(r) * 8n - 1n) - 1n : o = 2n ** (BigInt(r) * 8n) - 1n : typeof e == "number" && (o = BigInt(Number.MAX_SAFE_INTEGER));
  const s = typeof o == "bigint" && n ? -o - 1n : 0;
  if (o && i > o || i < s) {
    const c = typeof e == "bigint" ? "n" : "";
    throw new Gl({
      max: o ? `${o}${c}` : void 0,
      min: `${s}${c}`,
      signed: n,
      size: r,
      value: `${e}${c}`
    });
  }
  const a = `0x${(n && i < 0 ? (1n << BigInt(r * 8)) + BigInt(i) : i).toString(16)}`;
  return r ? Qr(a, { size: r }) : a;
}
const gE = /* @__PURE__ */ new TextEncoder();
function ww(e, t = {}) {
  const n = gE.encode(e);
  return je(n, t);
}
const mE = /* @__PURE__ */ new TextEncoder();
function Zl(e, t = {}) {
  return typeof e == "number" || typeof e == "bigint" ? vE(e, t) : typeof e == "boolean" ? bE(e, t) : Vi(e) ? Nn(e, t) : gw(e, t);
}
function bE(e, t = {}) {
  const n = new Uint8Array(1);
  return n[0] = Number(e), typeof t.size == "number" ? (Nt(n, { size: t.size }), Qr(n, { size: t.size })) : n;
}
const Vt = {
  zero: 48,
  nine: 57,
  A: 65,
  F: 70,
  a: 97,
  f: 102
};
function Fu(e) {
  if (e >= Vt.zero && e <= Vt.nine)
    return e - Vt.zero;
  if (e >= Vt.A && e <= Vt.F)
    return e - (Vt.A - 10);
  if (e >= Vt.a && e <= Vt.f)
    return e - (Vt.a - 10);
}
function Nn(e, t = {}) {
  let n = e;
  t.size && (Nt(n, { size: t.size }), n = Qr(n, { dir: "right", size: t.size }));
  let r = n.slice(2);
  r.length % 2 && (r = `0${r}`);
  const i = r.length / 2, o = new Uint8Array(i);
  for (let s = 0, a = 0; s < i; s++) {
    const c = Fu(r.charCodeAt(a++)), l = Fu(r.charCodeAt(a++));
    if (c === void 0 || l === void 0)
      throw new ee(`Invalid byte sequence ("${r[a - 2]}${r[a - 1]}" in "${r}").`);
    o[s] = c * 16 + l;
  }
  return o;
}
function vE(e, t) {
  const n = Q(e, t);
  return Nn(n);
}
function gw(e, t = {}) {
  const n = mE.encode(e);
  return typeof t.size == "number" ? (Nt(n, { size: t.size }), Qr(n, { dir: "right", size: t.size })) : n;
}
const Eo = /* @__PURE__ */ BigInt(2 ** 32 - 1), ju = /* @__PURE__ */ BigInt(32);
function xE(e, t = !1) {
  return t ? { h: Number(e & Eo), l: Number(e >> ju & Eo) } : { h: Number(e >> ju & Eo) | 0, l: Number(e & Eo) | 0 };
}
function kE(e, t = !1) {
  const n = e.length;
  let r = new Uint32Array(n), i = new Uint32Array(n);
  for (let o = 0; o < n; o++) {
    const { h: s, l: a } = xE(e[o], t);
    [r[o], i[o]] = [s, a];
  }
  return [r, i];
}
const EE = (e, t, n) => e << n | t >>> 32 - n, AE = (e, t, n) => t << n | e >>> 32 - n, SE = (e, t, n) => t << n - 32 | e >>> 64 - n, _E = (e, t, n) => e << n - 32 | t >>> 64 - n;
/*! noble-hashes - MIT License (c) 2022 Paul Miller (paulmillr.com) */
function IE(e) {
  return e instanceof Uint8Array || ArrayBuffer.isView(e) && e.constructor.name === "Uint8Array";
}
function Vu(e) {
  if (!Number.isSafeInteger(e) || e < 0)
    throw new Error("positive integer expected, got " + e);
}
function Ui(e, ...t) {
  if (!IE(e))
    throw new Error("Uint8Array expected");
  if (t.length > 0 && !t.includes(e.length))
    throw new Error("Uint8Array expected of length " + t + ", got length=" + e.length);
}
function Is(e, t = !0) {
  if (e.destroyed)
    throw new Error("Hash instance has been destroyed");
  if (t && e.finished)
    throw new Error("Hash#digest() has already been called");
}
function mw(e, t) {
  Ui(e);
  const n = t.outputLen;
  if (e.length < n)
    throw new Error("digestInto() expects output buffer of length at least " + n);
}
function BE(e) {
  return new Uint32Array(e.buffer, e.byteOffset, Math.floor(e.byteLength / 4));
}
function Ni(...e) {
  for (let t = 0; t < e.length; t++)
    e[t].fill(0);
}
function La(e) {
  return new DataView(e.buffer, e.byteOffset, e.byteLength);
}
function Et(e, t) {
  return e << 32 - t | e >>> t;
}
const $E = new Uint8Array(new Uint32Array([287454020]).buffer)[0] === 68;
function TE(e) {
  return e << 24 & 4278190080 | e << 8 & 16711680 | e >>> 8 & 65280 | e >>> 24 & 255;
}
function PE(e) {
  for (let t = 0; t < e.length; t++)
    e[t] = TE(e[t]);
  return e;
}
const qu = $E ? (e) => e : PE;
function OE(e) {
  if (typeof e != "string")
    throw new Error("string expected");
  return new Uint8Array(new TextEncoder().encode(e));
}
function Yl(e) {
  return typeof e == "string" && (e = OE(e)), Ui(e), e;
}
class bw {
}
function vw(e) {
  const t = (r) => e().update(Yl(r)).digest(), n = e();
  return t.outputLen = n.outputLen, t.blockLen = n.blockLen, t.create = () => e(), t;
}
const CE = BigInt(0), si = BigInt(1), RE = BigInt(2), HE = BigInt(7), LE = BigInt(256), UE = BigInt(113), xw = [], kw = [], Ew = [];
for (let e = 0, t = si, n = 1, r = 0; e < 24; e++) {
  [n, r] = [r, (2 * n + 3 * r) % 5], xw.push(2 * (5 * r + n)), kw.push((e + 1) * (e + 2) / 2 % 64);
  let i = CE;
  for (let o = 0; o < 7; o++)
    t = (t << si ^ (t >> HE) * UE) % LE, t & RE && (i ^= si << (si << /* @__PURE__ */ BigInt(o)) - si);
  Ew.push(i);
}
const Aw = kE(Ew, !0), NE = Aw[0], DE = Aw[1], Gu = (e, t, n) => n > 32 ? SE(e, t, n) : EE(e, t, n), Wu = (e, t, n) => n > 32 ? _E(e, t, n) : AE(e, t, n);
function zE(e, t = 24) {
  const n = new Uint32Array(10);
  for (let r = 24 - t; r < 24; r++) {
    for (let s = 0; s < 10; s++)
      n[s] = e[s] ^ e[s + 10] ^ e[s + 20] ^ e[s + 30] ^ e[s + 40];
    for (let s = 0; s < 10; s += 2) {
      const a = (s + 8) % 10, c = (s + 2) % 10, l = n[c], u = n[c + 1], d = Gu(l, u, 1) ^ n[a], f = Wu(l, u, 1) ^ n[a + 1];
      for (let h = 0; h < 50; h += 10)
        e[s + h] ^= d, e[s + h + 1] ^= f;
    }
    let i = e[2], o = e[3];
    for (let s = 0; s < 24; s++) {
      const a = kw[s], c = Gu(i, o, a), l = Wu(i, o, a), u = xw[s];
      i = e[u], o = e[u + 1], e[u] = c, e[u + 1] = l;
    }
    for (let s = 0; s < 50; s += 10) {
      for (let a = 0; a < 10; a++)
        n[a] = e[s + a];
      for (let a = 0; a < 10; a++)
        e[s + a] ^= ~n[(a + 2) % 10] & n[(a + 4) % 10];
    }
    e[0] ^= NE[r], e[1] ^= DE[r];
  }
  Ni(n);
}
class Xl extends bw {
  // NOTE: we accept arguments in bytes instead of bits here.
  constructor(t, n, r, i = !1, o = 24) {
    if (super(), this.pos = 0, this.posOut = 0, this.finished = !1, this.destroyed = !1, this.enableXOF = !1, this.blockLen = t, this.suffix = n, this.outputLen = r, this.enableXOF = i, this.rounds = o, Vu(r), !(0 < t && t < 200))
      throw new Error("only keccak-f1600 function is supported");
    this.state = new Uint8Array(200), this.state32 = BE(this.state);
  }
  clone() {
    return this._cloneInto();
  }
  keccak() {
    qu(this.state32), zE(this.state32, this.rounds), qu(this.state32), this.posOut = 0, this.pos = 0;
  }
  update(t) {
    Is(this), t = Yl(t), Ui(t);
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
    Is(this, !1), Ui(t), this.finish();
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
    return Vu(t), this.xofInto(new Uint8Array(t));
  }
  digestInto(t) {
    if (mw(t, this), this.finished)
      throw new Error("digest() was already called");
    return this.writeInto(t), this.destroy(), t;
  }
  digest() {
    return this.digestInto(new Uint8Array(this.outputLen));
  }
  destroy() {
    this.destroyed = !0, Ni(this.state);
  }
  _cloneInto(t) {
    const { blockLen: n, suffix: r, outputLen: i, rounds: o, enableXOF: s } = this;
    return t || (t = new Xl(n, r, i, s, o)), t.state32.set(this.state32), t.pos = this.pos, t.posOut = this.posOut, t.finished = this.finished, t.rounds = o, t.suffix = r, t.outputLen = i, t.enableXOF = s, t.destroyed = this.destroyed, t;
  }
}
const ME = (e, t, n) => vw(() => new Xl(t, e, n)), KE = ME(1, 136, 256 / 8);
function Pt(e, t) {
  const n = t || "hex", r = KE(Vi(e, { strict: !1 }) ? Zl(e) : e);
  return n === "bytes" ? r : Li(r);
}
const FE = (e) => Pt(Zl(e));
function jE(e) {
  return FE(e);
}
function VE(e) {
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
const qE = (e) => {
  const t = typeof e == "string" ? e : yk(e);
  return VE(t);
};
function GE(e) {
  return jE(qE(e));
}
class dr extends ee {
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
class Sw extends Map {
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
const Ua = /* @__PURE__ */ new Sw(8192);
function _w(e, t) {
  if (Ua.has(`${e}.${t}`))
    return Ua.get(`${e}.${t}`);
  const n = e.substring(2).toLowerCase(), r = Pt(gw(n), "bytes"), i = n.split("");
  for (let s = 0; s < 40; s += 2)
    r[s >> 1] >> 4 >= 8 && i[s] && (i[s] = i[s].toUpperCase()), (r[s >> 1] & 15) >= 8 && i[s + 1] && (i[s + 1] = i[s + 1].toUpperCase());
  const o = `0x${i.join("")}`;
  return Ua.set(`${e}.${t}`, o), o;
}
const WE = /^0x[a-fA-F0-9]{40}$/, Na = /* @__PURE__ */ new Sw(8192);
function ur(e, t) {
  const { strict: n = !0 } = t ?? {}, r = `${e}.${n}`;
  if (Na.has(r))
    return Na.get(r);
  const i = WE.test(e) ? e.toLowerCase() === e ? !0 : n ? _w(e) === e : !0 : !1;
  return Na.set(r, i), i;
}
function ZE(e) {
  return typeof e[0] == "string" ? yt(e) : YE(e);
}
function YE(e) {
  let t = 0;
  for (const i of e)
    t += i.length;
  const n = new Uint8Array(t);
  let r = 0;
  for (const i of e)
    n.set(i, r), r += i.length;
  return n;
}
function yt(e) {
  return `0x${e.reduce((t, n) => t + n.replace("0x", ""), "")}`;
}
function Jl(e, t, n, { strict: r } = {}) {
  return Vi(e, { strict: !1 }) ? Tw(e, t, n, {
    strict: r
  }) : $w(e, t, n, {
    strict: r
  });
}
function Iw(e, t) {
  if (typeof t == "number" && t > 0 && t > Ee(e) - 1)
    throw new fw({
      offset: t,
      position: "start",
      size: Ee(e)
    });
}
function Bw(e, t, n) {
  if (typeof t == "number" && typeof n == "number" && Ee(e) !== n - t)
    throw new fw({
      offset: n,
      position: "end",
      size: Ee(e)
    });
}
function $w(e, t, n, { strict: r } = {}) {
  Iw(e, t);
  const i = e.slice(t, n);
  return r && Bw(i, t, n), i;
}
function Tw(e, t, n, { strict: r } = {}) {
  Iw(e, t);
  const i = `0x${e.replace("0x", "").slice((t ?? 0) * 2, (n ?? e.length) * 2)}`;
  return r && Bw(i, t, n), i;
}
const XE = /^bytes([1-9]|1[0-9]|2[0-9]|3[0-2])?$/, Pw = /^(u?int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$/;
function Ql(e, t) {
  if (e.length !== t.length)
    throw new cE({
      expectedLength: e.length,
      givenLength: t.length
    });
  const n = JE({
    params: e,
    values: t
  });
  return td(n);
}
function JE({ params: e, values: t }) {
  const n = [];
  for (let r = 0; r < e.length; r++)
    n.push(ed({ param: e[r], value: t[r] }));
  return n;
}
function ed({ param: e, value: t }) {
  const n = na(e.type);
  if (n) {
    const [r, i] = n;
    return eA(t, { length: r, param: { ...e, type: i } });
  }
  if (e.type === "tuple")
    return oA(t, {
      param: e
    });
  if (e.type === "address")
    return QE(t);
  if (e.type === "bool")
    return nA(t);
  if (e.type.startsWith("uint") || e.type.startsWith("int")) {
    const r = e.type.startsWith("int"), [, , i = "256"] = Pw.exec(e.type) ?? [];
    return rA(t, {
      signed: r,
      size: Number(i)
    });
  }
  if (e.type.startsWith("bytes"))
    return tA(t, { param: e });
  if (e.type === "string")
    return iA(t);
  throw new dE(e.type, {
    docsPath: "/docs/contract/encodeAbiParameters"
  });
}
function td(e) {
  let t = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? t += 32 : t += Ee(a);
  }
  const n = [], r = [];
  let i = 0;
  for (let o = 0; o < e.length; o++) {
    const { dynamic: s, encoded: a } = e[o];
    s ? (n.push(Q(t + i, { size: 32 })), r.push(a), i += Ee(a)) : n.push(a);
  }
  return yt([...n, ...r]);
}
function QE(e) {
  if (!ur(e))
    throw new dr({ address: e });
  return { dynamic: !1, encoded: On(e.toLowerCase()) };
}
function eA(e, { length: t, param: n }) {
  const r = t === null;
  if (!Array.isArray(e))
    throw new fE(e);
  if (!r && e.length !== t)
    throw new sE({
      expectedLength: t,
      givenLength: e.length,
      type: `${n.type}[${t}]`
    });
  let i = e.length === 0 && _c(n);
  const o = [];
  for (let s = 0; s < e.length; s++) {
    const a = ed({ param: n, value: e[s] });
    a.dynamic && (i = !0), o.push(a);
  }
  if (r || i) {
    const s = td(o);
    if (r) {
      const a = Q(o.length, { size: 32 });
      return {
        dynamic: !0,
        encoded: yt([a, s])
      };
    }
    if (i)
      return { dynamic: !0, encoded: s };
  }
  return {
    dynamic: !1,
    encoded: yt(o.map(({ encoded: s }) => s))
  };
}
function tA(e, { param: t }) {
  const [, n] = t.type.split("bytes"), r = Ee(e);
  if (!n) {
    let i = e;
    return r % 32 !== 0 && (i = On(i, {
      dir: "right",
      size: Math.ceil((e.length - 2) / 2 / 32) * 32
    })), {
      dynamic: !0,
      encoded: yt([
        On(Q(r, { size: 32 })),
        i
      ])
    };
  }
  if (r !== Number.parseInt(n, 10))
    throw new aE({
      expectedSize: Number.parseInt(n, 10),
      value: e
    });
  return { dynamic: !1, encoded: On(e, { dir: "right" }) };
}
function nA(e) {
  if (typeof e != "boolean")
    throw new ee(`Invalid boolean value: "${e}" (type: ${typeof e}). Expected: \`true\` or \`false\`.`);
  return { dynamic: !1, encoded: On(yw(e)) };
}
function rA(e, { signed: t, size: n = 256 }) {
  if (typeof n == "number") {
    const r = 2n ** (BigInt(n) - (t ? 1n : 0n)) - 1n, i = t ? -r - 1n : 0n;
    if (e > r || e < i)
      throw new Gl({
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
function iA(e) {
  const t = ww(e), n = Math.ceil(Ee(t) / 32), r = [];
  for (let i = 0; i < n; i++)
    r.push(On(Jl(t, i * 32, (i + 1) * 32), {
      dir: "right"
    }));
  return {
    dynamic: !0,
    encoded: yt([
      On(Q(Ee(t), { size: 32 })),
      ...r
    ])
  };
}
function oA(e, { param: t }) {
  let n = !1;
  const r = [];
  for (let i = 0; i < t.components.length; i++) {
    const o = t.components[i], s = Array.isArray(e) ? i : o.name, a = ed({
      param: o,
      value: e[s]
    });
    r.push(a), a.dynamic && (n = !0);
  }
  return {
    dynamic: n,
    encoded: n ? td(r) : yt(r.map(({ encoded: i }) => i))
  };
}
function na(e) {
  const t = e.match(/^(.*)\[(\d+)?\]$/);
  return t ? (
    // Return `null` if the array is dynamic.
    [t[2] ? Number(t[2]) : null, t[1]]
  ) : void 0;
}
function _c(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components.some(_c);
  const n = na(t);
  return n ? _c({ ...e, type: n[1] }) : !1;
}
const sA = (e) => Jl(GE(e), 0, 4);
class Zu extends ee {
  constructor({ offset: t }) {
    super(`Offset \`${t}\` cannot be negative.`, {
      name: "NegativeOffsetError"
    });
  }
}
class aA extends ee {
  constructor({ length: t, position: n }) {
    super(`Position \`${n}\` is out of bounds (\`0 < position < ${t}\`).`, { name: "PositionOutOfBoundsError" });
  }
}
class cA extends ee {
  constructor({ count: t, limit: n }) {
    super(`Recursive read limit of \`${n}\` exceeded (recursive read count: \`${t}\`).`, { name: "RecursiveReadLimitExceededError" });
  }
}
const lA = {
  bytes: new Uint8Array(),
  dataView: new DataView(new ArrayBuffer(0)),
  position: 0,
  positionReadCount: /* @__PURE__ */ new Map(),
  recursiveReadCount: 0,
  recursiveReadLimit: Number.POSITIVE_INFINITY,
  assertReadLimit() {
    if (this.recursiveReadCount >= this.recursiveReadLimit)
      throw new cA({
        count: this.recursiveReadCount + 1,
        limit: this.recursiveReadLimit
      });
  },
  assertPosition(e) {
    if (e < 0 || e > this.bytes.length - 1)
      throw new aA({
        length: this.bytes.length,
        position: e
      });
  },
  decrementPosition(e) {
    if (e < 0)
      throw new Zu({ offset: e });
    const t = this.position - e;
    this.assertPosition(t), this.position = t;
  },
  getReadCount(e) {
    return this.positionReadCount.get(e || this.position) || 0;
  },
  incrementPosition(e) {
    if (e < 0)
      throw new Zu({ offset: e });
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
function nd(e, { recursiveReadLimit: t = 8192 } = {}) {
  const n = Object.create(lA);
  return n.bytes = e, n.dataView = new DataView(e.buffer ?? e, e.byteOffset, e.byteLength), n.positionReadCount = /* @__PURE__ */ new Map(), n.recursiveReadLimit = t, n;
}
function dA(e, t = {}) {
  typeof t.size < "u" && Nt(e, { size: t.size });
  const n = je(e, t);
  return pw(n, t);
}
function uA(e, t = {}) {
  let n = e;
  if (typeof t.size < "u" && (Nt(n, { size: t.size }), n = Hi(n)), n.length > 1 || n[0] > 1)
    throw new pE(n);
  return !!n[0];
}
function Qt(e, t = {}) {
  typeof t.size < "u" && Nt(e, { size: t.size });
  const n = je(e, t);
  return Wl(n, t);
}
function fA(e, t = {}) {
  let n = e;
  return typeof t.size < "u" && (Nt(n, { size: t.size }), n = Hi(n, { dir: "right" })), new TextDecoder().decode(n);
}
function hA(e, t) {
  const n = typeof t == "string" ? Nn(t) : t, r = nd(n);
  if (Ee(n) === 0 && e.length > 0)
    throw new oE();
  if (Ee(t) && Ee(t) < 32)
    throw new iE({
      data: typeof t == "string" ? t : je(t),
      params: e,
      size: Ee(t)
    });
  let i = 0;
  const o = [];
  for (let s = 0; s < e.length; ++s) {
    const a = e[s];
    i < n.length && r.setPosition(i);
    const [c, l] = Hr(r, a, {
      staticPosition: 0
    });
    i += l, o.push(c);
  }
  return o;
}
function Hr(e, t, { staticPosition: n }) {
  const r = na(t.type);
  if (r) {
    const [i, o] = r;
    return yA(e, { ...t, type: o }, { length: i, staticPosition: n });
  }
  if (t.type === "tuple")
    return bA(e, t, { staticPosition: n });
  if (t.type === "address")
    return pA(e);
  if (t.type === "bool")
    return wA(e);
  if (t.type.startsWith("bytes"))
    return gA(e, t, { staticPosition: n });
  if (t.type.startsWith("uint") || t.type.startsWith("int"))
    return mA(e, t);
  if (t.type === "string")
    return vA(e, { staticPosition: n });
  throw new uE(t.type, {
    docsPath: "/docs/contract/decodeAbiParameters"
  });
}
const Yu = 32, Ic = 32;
function pA(e) {
  const t = e.readBytes(32);
  return [_w(je($w(t, -20))), 32];
}
function yA(e, t, { length: n, staticPosition: r }) {
  if (n === null) {
    const s = Qt(e.readBytes(Ic)), a = r + s, c = a + Yu;
    e.setPosition(a);
    const l = Qt(e.readBytes(Yu)), u = Di(t);
    let d = 0;
    const f = [];
    for (let h = 0; h < l; ++h) {
      e.setPosition(c + (u ? h * 32 : d));
      const [y, w] = Hr(e, t, {
        staticPosition: c
      });
      d += w, f.push(y), w === 0 && (e.assertReadLimit(), e._touch());
    }
    return e.setPosition(r + 32), [f, 32];
  }
  if (Di(t)) {
    const s = Qt(e.readBytes(Ic)), a = r + s, c = [];
    for (let l = 0; l < n; ++l) {
      e.setPosition(a + l * 32);
      const [u] = Hr(e, t, {
        staticPosition: a
      });
      c.push(u);
    }
    return e.setPosition(r + 32), [c, 32];
  }
  let i = 0;
  const o = [];
  for (let s = 0; s < n; ++s) {
    const [a, c] = Hr(e, t, {
      staticPosition: r + i
    });
    i += c, o.push(a), c === 0 && (e.assertReadLimit(), e._touch());
  }
  return [o, i];
}
function wA(e) {
  return [uA(e.readBytes(32), { size: 32 }), 32];
}
function gA(e, t, { staticPosition: n }) {
  const [r, i] = t.type.split("bytes");
  if (!i) {
    const s = Qt(e.readBytes(32));
    e.setPosition(n + s);
    const a = Qt(e.readBytes(32));
    if (a === 0)
      return e.setPosition(n + 32), ["0x", 32];
    const c = e.readBytes(a);
    return e.setPosition(n + 32), [je(c), 32];
  }
  return [je(e.readBytes(Number.parseInt(i, 10), 32)), 32];
}
function mA(e, t) {
  const n = t.type.startsWith("int"), r = Number.parseInt(t.type.split("int")[1] || "256", 10), i = e.readBytes(32);
  return [
    r > 48 ? dA(i, { signed: n }) : Qt(i, { signed: n }),
    32
  ];
}
function bA(e, t, { staticPosition: n }) {
  const r = t.components.length === 0 || t.components.some(({ name: s }) => !s), i = r ? [] : {};
  let o = 0;
  if (Di(t)) {
    const s = Qt(e.readBytes(Ic)), a = n + s;
    for (let c = 0; c < t.components.length; ++c) {
      const l = t.components[c];
      e.setPosition(a + o);
      const [u, d] = Hr(e, l, {
        staticPosition: a
      });
      o += d, i[r ? c : l?.name] = u;
    }
    return e.setPosition(n + 32), [i, 32];
  }
  for (let s = 0; s < t.components.length; ++s) {
    const a = t.components[s], [c, l] = Hr(e, a, {
      staticPosition: n
    });
    i[r ? s : a?.name] = c, o += l;
  }
  return [i, o];
}
function vA(e, { staticPosition: t }) {
  const n = Qt(e.readBytes(32)), r = t + n;
  e.setPosition(r);
  const i = Qt(e.readBytes(32));
  if (i === 0)
    return e.setPosition(t + 32), ["", 32];
  const o = e.readBytes(i, 32), s = fA(Hi(o));
  return e.setPosition(t + 32), [s, 32];
}
function Di(e) {
  const { type: t } = e;
  if (t === "string" || t === "bytes" || t.endsWith("[]"))
    return !0;
  if (t === "tuple")
    return e.components?.some(Di);
  const n = na(e.type);
  return !!(n && Di({ ...e, type: n[1] }));
}
const xA = (e, t, n) => JSON.stringify(e, (r, i) => typeof i == "bigint" ? i.toString() : i, n), kA = {
  ether: -9,
  wei: 9
};
function EA(e, t) {
  let n = e.toString();
  const r = n.startsWith("-");
  r && (n = n.slice(1)), n = n.padStart(t, "0");
  let [i, o] = [
    n.slice(0, n.length - t),
    n.slice(n.length - t)
  ];
  return o = o.replace(/(0+)$/, ""), `${r ? "-" : ""}${i || "0"}${o ? `.${o}` : ""}`;
}
function Bc(e, t = "wei") {
  return EA(e, kA[t]);
}
function AA(e) {
  const t = Object.entries(e).map(([r, i]) => i === void 0 || i === !1 ? null : [r, i]).filter(Boolean), n = t.reduce((r, [i]) => Math.max(r, i.length), 0);
  return t.map(([r, i]) => `  ${`${r}:`.padEnd(n + 1)}  ${i}`).join(`
`);
}
class SA extends ee {
  constructor({ transaction: t }) {
    super("Cannot infer a transaction type from provided transaction.", {
      metaMessages: [
        "Provided Transaction:",
        "{",
        AA(t),
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
class _A extends ee {
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
class IA extends ee {
  constructor({ storageKey: t }) {
    super(`Size for storage key "${t}" is invalid. Expected 32 bytes. Got ${Math.floor((t.length - 2) / 2)} bytes.`, { name: "InvalidStorageKeySizeError" });
  }
}
function Vr(e, t = "hex") {
  const n = Ow(e), r = nd(new Uint8Array(n.length));
  return n.encode(r), t === "hex" ? je(r.bytes) : r.bytes;
}
function Ow(e) {
  return Array.isArray(e) ? BA(e.map((t) => Ow(t))) : $A(e);
}
function BA(e) {
  const t = e.reduce((i, o) => i + o.length, 0), n = Cw(t);
  return {
    length: t <= 55 ? 1 + t : 1 + n + t,
    encode(i) {
      t <= 55 ? i.pushByte(192 + t) : (i.pushByte(247 + n), n === 1 ? i.pushUint8(t) : n === 2 ? i.pushUint16(t) : n === 3 ? i.pushUint24(t) : i.pushUint32(t));
      for (const { encode: o } of e)
        o(i);
    }
  };
}
function $A(e) {
  const t = typeof e == "string" ? Nn(e) : e, n = Cw(t.length);
  return {
    length: t.length === 1 && t[0] < 128 ? 1 : t.length <= 55 ? 1 + t.length : 1 + n + t.length,
    encode(i) {
      t.length === 1 && t[0] < 128 ? i.pushBytes(t) : t.length <= 55 ? (i.pushByte(128 + t.length), i.pushBytes(t)) : (i.pushByte(183 + n), n === 1 ? i.pushUint8(t.length) : n === 2 ? i.pushUint16(t.length) : n === 3 ? i.pushUint24(t.length) : i.pushUint32(t.length), i.pushBytes(t));
    }
  };
}
function Cw(e) {
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
class ra extends ee {
  constructor({ cause: t, maxFeePerGas: n } = {}) {
    super(`The fee cap (\`maxFeePerGas\`${n ? ` = ${Bc(n)} gwei` : ""}) cannot be higher than the maximum allowed value (2^256-1).`, {
      cause: t,
      name: "FeeCapTooHighError"
    });
  }
}
Object.defineProperty(ra, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max fee per gas higher than 2\^256-1|fee cap higher than 2\^256-1/
});
class Rw extends ee {
  constructor({ cause: t, maxPriorityFeePerGas: n, maxFeePerGas: r } = {}) {
    super([
      `The provided tip (\`maxPriorityFeePerGas\`${n ? ` = ${Bc(n)} gwei` : ""}) cannot be higher than the fee cap (\`maxFeePerGas\`${r ? ` = ${Bc(r)} gwei` : ""}).`
    ].join(`
`), {
      cause: t,
      name: "TipAboveFeeCapError"
    });
  }
}
Object.defineProperty(Rw, "nodeMessage", {
  enumerable: !0,
  configurable: !0,
  writable: !0,
  value: /max priority fee per gas higher than max fee per gas|tip higher than fee cap/
});
const rd = 2n ** 256n - 1n;
function Hw(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((o) => Nn(o)) : e.blobs, i = [];
  for (const o of r)
    i.push(Uint8Array.from(t.blobToKzgCommitment(o)));
  return n === "bytes" ? i : i.map((o) => je(o));
}
function Lw(e) {
  const { kzg: t } = e, n = e.to ?? (typeof e.blobs[0] == "string" ? "hex" : "bytes"), r = typeof e.blobs[0] == "string" ? e.blobs.map((s) => Nn(s)) : e.blobs, i = typeof e.commitments[0] == "string" ? e.commitments.map((s) => Nn(s)) : e.commitments, o = [];
  for (let s = 0; s < r.length; s++) {
    const a = r[s], c = i[s];
    o.push(Uint8Array.from(t.computeBlobKzgProof(a, c)));
  }
  return n === "bytes" ? o : o.map((s) => je(s));
}
function TA(e, t, n, r) {
  if (typeof e.setBigUint64 == "function")
    return e.setBigUint64(t, n, r);
  const i = BigInt(32), o = BigInt(4294967295), s = Number(n >> i & o), a = Number(n & o), c = r ? 4 : 0, l = r ? 0 : 4;
  e.setUint32(t + c, s, r), e.setUint32(t + l, a, r);
}
function PA(e, t, n) {
  return e & t ^ ~e & n;
}
function OA(e, t, n) {
  return e & t ^ e & n ^ t & n;
}
class CA extends bw {
  constructor(t, n, r, i) {
    super(), this.finished = !1, this.length = 0, this.pos = 0, this.destroyed = !1, this.blockLen = t, this.outputLen = n, this.padOffset = r, this.isLE = i, this.buffer = new Uint8Array(t), this.view = La(this.buffer);
  }
  update(t) {
    Is(this), t = Yl(t), Ui(t);
    const { view: n, buffer: r, blockLen: i } = this, o = t.length;
    for (let s = 0; s < o; ) {
      const a = Math.min(i - this.pos, o - s);
      if (a === i) {
        const c = La(t);
        for (; i <= o - s; s += i)
          this.process(c, s);
        continue;
      }
      r.set(t.subarray(s, s + a), this.pos), this.pos += a, s += a, this.pos === i && (this.process(n, 0), this.pos = 0);
    }
    return this.length += t.length, this.roundClean(), this;
  }
  digestInto(t) {
    Is(this), mw(t, this), this.finished = !0;
    const { buffer: n, view: r, blockLen: i, isLE: o } = this;
    let { pos: s } = this;
    n[s++] = 128, Ni(this.buffer.subarray(s)), this.padOffset > i - s && (this.process(r, 0), s = 0);
    for (let d = s; d < i; d++)
      n[d] = 0;
    TA(r, i - 8, BigInt(this.length * 8), o), this.process(r, 0);
    const a = La(t), c = this.outputLen;
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
const En = /* @__PURE__ */ Uint32Array.from([
  1779033703,
  3144134277,
  1013904242,
  2773480762,
  1359893119,
  2600822924,
  528734635,
  1541459225
]), RA = /* @__PURE__ */ Uint32Array.from([
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
class HA extends CA {
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
      const f = An[d - 15], h = An[d - 2], y = Et(f, 7) ^ Et(f, 18) ^ f >>> 3, w = Et(h, 17) ^ Et(h, 19) ^ h >>> 10;
      An[d] = w + An[d - 7] + y + An[d - 16] | 0;
    }
    let { A: r, B: i, C: o, D: s, E: a, F: c, G: l, H: u } = this;
    for (let d = 0; d < 64; d++) {
      const f = Et(a, 6) ^ Et(a, 11) ^ Et(a, 25), h = u + f + PA(a, c, l) + RA[d] + An[d] | 0, w = (Et(r, 2) ^ Et(r, 13) ^ Et(r, 22)) + OA(r, i, o) | 0;
      u = l, l = c, c = a, a = s + h | 0, s = o, o = i, i = r, r = h + w | 0;
    }
    r = r + this.A | 0, i = i + this.B | 0, o = o + this.C | 0, s = s + this.D | 0, a = a + this.E | 0, c = c + this.F | 0, l = l + this.G | 0, u = u + this.H | 0, this.set(r, i, o, s, a, c, l, u);
  }
  roundClean() {
    Ni(An);
  }
  destroy() {
    this.set(0, 0, 0, 0, 0, 0, 0, 0), Ni(this.buffer);
  }
}
const LA = /* @__PURE__ */ vw(() => new HA()), UA = LA;
function NA(e, t) {
  return UA(Vi(e, { strict: !1 }) ? Zl(e) : e);
}
function DA(e) {
  const { commitment: t, version: n = 1 } = e, r = e.to ?? (typeof t == "string" ? "hex" : "bytes"), i = NA(t);
  return i.set([n], 0), r === "bytes" ? i : je(i);
}
function zA(e) {
  const { commitments: t, version: n } = e, r = e.to ?? (typeof t[0] == "string" ? "hex" : "bytes"), i = [];
  for (const o of t)
    i.push(DA({
      commitment: o,
      to: r,
      version: n
    }));
  return i;
}
const Xu = 6, Uw = 32, id = 4096, Nw = Uw * id, Ju = Nw * Xu - // terminator byte (0x80).
1 - // zero byte (0x00) appended to each field element.
1 * id * Xu, Dw = 1;
class MA extends ee {
  constructor({ maxSize: t, size: n }) {
    super("Blob size is too large.", {
      metaMessages: [`Max: ${t} bytes`, `Given: ${n} bytes`],
      name: "BlobSizeTooLargeError"
    });
  }
}
class zw extends ee {
  constructor() {
    super("Blob data must not be empty.", { name: "EmptyBlobError" });
  }
}
class KA extends ee {
  constructor({ hash: t, size: n }) {
    super(`Versioned hash "${t}" size is invalid.`, {
      metaMessages: ["Expected: 32", `Received: ${n}`],
      name: "InvalidVersionedHashSizeError"
    });
  }
}
class FA extends ee {
  constructor({ hash: t, version: n }) {
    super(`Versioned hash "${t}" version is invalid.`, {
      metaMessages: [
        `Expected: ${Dw}`,
        `Received: ${n}`
      ],
      name: "InvalidVersionedHashVersionError"
    });
  }
}
function jA(e) {
  const t = e.to ?? (typeof e.data == "string" ? "hex" : "bytes"), n = typeof e.data == "string" ? Nn(e.data) : e.data, r = Ee(n);
  if (!r)
    throw new zw();
  if (r > Ju)
    throw new MA({
      maxSize: Ju,
      size: r
    });
  const i = [];
  let o = !0, s = 0;
  for (; o; ) {
    const a = nd(new Uint8Array(Nw));
    let c = 0;
    for (; c < id; ) {
      const l = n.slice(s, s + (Uw - 1));
      if (a.pushByte(0), a.pushBytes(l), l.length < 31) {
        a.pushByte(128), o = !1;
        break;
      }
      c++, s += 31;
    }
    i.push(a);
  }
  return t === "bytes" ? i.map((a) => a.bytes) : i.map((a) => je(a.bytes));
}
function VA(e) {
  const { data: t, kzg: n, to: r } = e, i = e.blobs ?? jA({ data: t, to: r }), o = e.commitments ?? Hw({ blobs: i, kzg: n, to: r }), s = e.proofs ?? Lw({ blobs: i, commitments: o, kzg: n, to: r }), a = [];
  for (let c = 0; c < i.length; c++)
    a.push({
      blob: i[c],
      commitment: o[c],
      proof: s[c]
    });
  return a;
}
function qA(e) {
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
  throw new SA({ transaction: e });
}
class ia extends ee {
  constructor({ chainId: t }) {
    super(typeof t == "number" ? `Chain ID "${t}" is invalid.` : "Chain ID is invalid.", { name: "InvalidChainIdError" });
  }
}
function GA(e) {
  const { authorizationList: t } = e;
  if (t)
    for (const n of t) {
      const { chainId: r } = n, i = n.address;
      if (!ur(i))
        throw new dr({ address: i });
      if (r < 0)
        throw new ia({ chainId: r });
    }
  od(e);
}
function WA(e) {
  const { blobVersionedHashes: t } = e;
  if (t) {
    if (t.length === 0)
      throw new zw();
    for (const n of t) {
      const r = Ee(n), i = Wl(Jl(n, 0, 1));
      if (r !== 32)
        throw new KA({ hash: n, size: r });
      if (i !== Dw)
        throw new FA({
          hash: n,
          version: i
        });
    }
  }
  od(e);
}
function od(e) {
  const { chainId: t, maxPriorityFeePerGas: n, maxFeePerGas: r, to: i } = e;
  if (t <= 0)
    throw new ia({ chainId: t });
  if (i && !ur(i))
    throw new dr({ address: i });
  if (r && r > rd)
    throw new ra({ maxFeePerGas: r });
  if (n && r && n > r)
    throw new Rw({ maxFeePerGas: r, maxPriorityFeePerGas: n });
}
function ZA(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (t <= 0)
    throw new ia({ chainId: t });
  if (o && !ur(o))
    throw new dr({ address: o });
  if (n || i)
    throw new ee("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid EIP-2930 Transaction attribute.");
  if (r && r > rd)
    throw new ra({ maxFeePerGas: r });
}
function YA(e) {
  const { chainId: t, maxPriorityFeePerGas: n, gasPrice: r, maxFeePerGas: i, to: o } = e;
  if (o && !ur(o))
    throw new dr({ address: o });
  if (typeof t < "u" && t <= 0)
    throw new ia({ chainId: t });
  if (n || i)
    throw new ee("`maxFeePerGas`/`maxPriorityFeePerGas` is not a valid Legacy Transaction attribute.");
  if (r && r > rd)
    throw new ra({ maxFeePerGas: r });
}
function oa(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (let n = 0; n < e.length; n++) {
    const { address: r, storageKeys: i } = e[n];
    for (let o = 0; o < i.length; o++)
      if (i[o].length - 2 !== 64)
        throw new IA({ storageKey: i[o] });
    if (!ur(r, { strict: !1 }))
      throw new dr({ address: r });
    t.push([r, i]);
  }
  return t;
}
function XA(e, t) {
  const n = qA(e);
  return n === "eip1559" ? eS(e, t) : n === "eip2930" ? tS(e, t) : n === "eip4844" ? QA(e, t) : n === "eip7702" ? JA(e, t) : nS(e);
}
function JA(e, t) {
  const { authorizationList: n, chainId: r, gas: i, nonce: o, to: s, value: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: u, data: d } = e;
  GA(e);
  const f = oa(u), h = rS(n);
  return yt([
    "0x04",
    Vr([
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
      ...qi(e, t)
    ])
  ]);
}
function QA(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerBlobGas: a, maxFeePerGas: c, maxPriorityFeePerGas: l, accessList: u, data: d } = e;
  WA(e);
  let f = e.blobVersionedHashes, h = e.sidecars;
  if (e.blobs && (typeof f > "u" || typeof h > "u")) {
    const k = typeof e.blobs[0] == "string" ? e.blobs : e.blobs.map((R) => je(R)), C = e.kzg, B = Hw({
      blobs: k,
      kzg: C
    });
    if (typeof f > "u" && (f = zA({
      commitments: B
    })), typeof h > "u") {
      const R = Lw({ blobs: k, commitments: B, kzg: C });
      h = VA({ blobs: k, commitments: B, proofs: R });
    }
  }
  const y = oa(u), w = [
    Q(n),
    i ? Q(i) : "0x",
    l ? Q(l) : "0x",
    c ? Q(c) : "0x",
    r ? Q(r) : "0x",
    o ?? "0x",
    s ? Q(s) : "0x",
    d ?? "0x",
    y,
    a ? Q(a) : "0x",
    f ?? [],
    ...qi(e, t)
  ], p = [], g = [], _ = [];
  if (h)
    for (let k = 0; k < h.length; k++) {
      const { blob: C, commitment: B, proof: R } = h[k];
      p.push(C), g.push(B), _.push(R);
    }
  return yt([
    "0x03",
    // If sidecars are enabled, envelope turns into a "wrapper":
    Vr(h ? [w, p, g, _] : w)
  ]);
}
function eS(e, t) {
  const { chainId: n, gas: r, nonce: i, to: o, value: s, maxFeePerGas: a, maxPriorityFeePerGas: c, accessList: l, data: u } = e;
  od(e);
  const d = oa(l), f = [
    Q(n),
    i ? Q(i) : "0x",
    c ? Q(c) : "0x",
    a ? Q(a) : "0x",
    r ? Q(r) : "0x",
    o ?? "0x",
    s ? Q(s) : "0x",
    u ?? "0x",
    d,
    ...qi(e, t)
  ];
  return yt([
    "0x02",
    Vr(f)
  ]);
}
function tS(e, t) {
  const { chainId: n, gas: r, data: i, nonce: o, to: s, value: a, accessList: c, gasPrice: l } = e;
  ZA(e);
  const u = oa(c), d = [
    Q(n),
    o ? Q(o) : "0x",
    l ? Q(l) : "0x",
    r ? Q(r) : "0x",
    s ?? "0x",
    a ? Q(a) : "0x",
    i ?? "0x",
    u,
    ...qi(e, t)
  ];
  return yt([
    "0x01",
    Vr(d)
  ]);
}
function nS(e, t) {
  const { chainId: n = 0, gas: r, data: i, nonce: o, to: s, value: a, gasPrice: c } = e;
  YA(e);
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
  ]), Vr(l);
}
function qi(e, t) {
  const n = t ?? e, { v: r, yParity: i } = n;
  if (typeof n.r > "u")
    return [];
  if (typeof n.s > "u")
    return [];
  if (typeof r > "u" && typeof i > "u")
    return [];
  const o = Hi(n.r), s = Hi(n.s);
  return [typeof i == "number" ? i ? Q(1) : "0x" : r === 0n ? "0x" : r === 1n ? Q(1) : r === 27n ? "0x" : Q(1), o === "0x00" ? "0x" : o, s === "0x00" ? "0x" : s];
}
function rS(e) {
  if (!e || e.length === 0)
    return [];
  const t = [];
  for (const n of e) {
    const { chainId: r, nonce: i, ...o } = n, s = n.address;
    t.push([
      r ? Li(r) : "0x",
      s,
      i ? Li(i) : "0x",
      ...qi({}, o)
    ]);
  }
  return t;
}
class iS extends ee {
  constructor({ domain: t }) {
    super(`Invalid domain "${xA(t)}".`, {
      metaMessages: ["Must be a valid EIP-712 domain."]
    });
  }
}
class oS extends ee {
  constructor({ primaryType: t, types: n }) {
    super(`Invalid primary type \`${t}\` must be one of \`${JSON.stringify(Object.keys(n))}\`.`, {
      docsPath: "/api/glossary/Errors#typeddatainvalidprimarytypeerror",
      metaMessages: ["Check that the primary type is a key in `types`."]
    });
  }
}
class sS extends ee {
  constructor({ type: t }) {
    super(`Struct type "${t}" is invalid.`, {
      metaMessages: ["Struct type must not be a Solidity type."],
      name: "InvalidStructTypeError"
    });
  }
}
function aS(e) {
  const { domain: t, message: n, primaryType: r, types: i } = e, o = (s, a) => {
    for (const c of s) {
      const { name: l, type: u } = c, d = a[l], f = u.match(Pw);
      if (f && (typeof d == "number" || typeof d == "bigint")) {
        const [w, p, g] = f;
        Q(d, {
          signed: p === "int",
          size: Number.parseInt(g, 10) / 8
        });
      }
      if (u === "address" && typeof d == "string" && !ur(d))
        throw new dr({ address: d });
      const h = u.match(XE);
      if (h) {
        const [w, p] = h;
        if (p && Ee(d) !== Number.parseInt(p, 10))
          throw new lE({
            expectedSize: Number.parseInt(p, 10),
            givenSize: Ee(d)
          });
      }
      const y = i[u];
      y && (cS(u), o(y, d));
    }
  };
  if (i.EIP712Domain && t) {
    if (typeof t != "object")
      throw new iS({ domain: t });
    o(i.EIP712Domain, t);
  }
  if (r !== "EIP712Domain")
    if (i[r])
      o(i[r], n);
    else
      throw new oS({ primaryType: r, types: i });
}
function Mw({ domain: e }) {
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
function cS(e) {
  if (e === "address" || e === "bool" || e === "string" || e.startsWith("bytes") || e.startsWith("uint") || e.startsWith("int"))
    throw new sS({ type: e });
}
function lS(e) {
  const { domain: t = {}, message: n, primaryType: r } = e, i = {
    EIP712Domain: Mw({ domain: t }),
    ...e.types
  };
  aS({
    domain: t,
    message: n,
    primaryType: r,
    types: i
  });
  const o = ["0x1901"];
  return t && o.push(dS({
    domain: t,
    types: i
  })), r !== "EIP712Domain" && o.push(Kw({
    data: n,
    primaryType: r,
    types: i
  })), Pt(ZE(o));
}
function dS({ domain: e, types: t }) {
  return Kw({
    data: e,
    primaryType: "EIP712Domain",
    types: t
  });
}
function Kw({ data: e, primaryType: t, types: n }) {
  const r = Fw({
    data: e,
    primaryType: t,
    types: n
  });
  return Pt(r);
}
function Fw({ data: e, primaryType: t, types: n }) {
  const r = [{ type: "bytes32" }], i = [uS({ primaryType: t, types: n })];
  for (const o of n[t]) {
    const [s, a] = Vw({
      types: n,
      name: o.name,
      type: o.type,
      value: e[o.name]
    });
    r.push(s), i.push(a);
  }
  return Ql(r, i);
}
function uS({ primaryType: e, types: t }) {
  const n = Li(fS({ primaryType: e, types: t }));
  return Pt(n);
}
function fS({ primaryType: e, types: t }) {
  let n = "";
  const r = jw({ primaryType: e, types: t });
  r.delete(e);
  const i = [e, ...Array.from(r).sort()];
  for (const o of i)
    n += `${o}(${t[o].map(({ name: s, type: a }) => `${a} ${s}`).join(",")})`;
  return n;
}
function jw({ primaryType: e, types: t }, n = /* @__PURE__ */ new Set()) {
  const i = e.match(/^\w*/u)?.[0];
  if (n.has(i) || t[i] === void 0)
    return n;
  n.add(i);
  for (const o of t[i])
    jw({ primaryType: o.type, types: t }, n);
  return n;
}
function Vw({ types: e, name: t, type: n, value: r }) {
  if (e[n] !== void 0)
    return [
      { type: "bytes32" },
      Pt(Fw({ data: r, primaryType: n, types: e }))
    ];
  if (n === "bytes")
    return [{ type: "bytes32" }, Pt(r)];
  if (n === "string")
    return [{ type: "bytes32" }, Pt(Li(r))];
  if (n.lastIndexOf("]") === n.length - 1) {
    const i = n.slice(0, n.lastIndexOf("[")), o = r.map((s) => Vw({
      name: t,
      type: i,
      types: e,
      value: s
    }));
    return [
      { type: "bytes32" },
      Pt(Ql(o.map(([s]) => s), o.map(([, s]) => s)))
    ];
  }
  return [{ type: n }, r];
}
function hS(e) {
  const t = Tw(e, 0, 1);
  if (t === "0x04")
    return "eip7702";
  if (t === "0x03")
    return "eip4844";
  if (t === "0x02")
    return "eip1559";
  if (t === "0x01")
    return "eip2930";
  if (t !== "0x" && Wl(t) >= 192)
    return "legacy";
  throw new _A({ serializedType: t });
}
const Qu = "bitcoin-taproot-keypath-v1", ef = "bip341-sighash-default-keypath", pS = ["bitcoin", "testnet", "signet", "regtest"], $c = 21000000n * 100000000n, qw = 137, Gw = /^[0-9a-f]+$/, yS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", wS = "qpzry9x8gf2tvdw0s3jn54khce6mua7l", gS = 734539939, mS = 1;
function re(e) {
  throw new S("invalid_payload", e);
}
function tf(e, t) {
  return (!e || typeof e != "object" || Array.isArray(e)) && re(`${t} must be an object`), e;
}
function nf(e, t, n) {
  const r = Object.keys(e).filter((o) => !t.includes(o));
  r.length > 0 && re(`${n} carries unsupported fields: ${r.sort().join(", ")}`);
  const i = t.filter((o) => !(o in e));
  i.length > 0 && re(`${n} is missing required fields: ${i.join(", ")}`);
}
function Lo(e, t, n) {
  return (typeof e != "string" || n !== void 0 && e.length !== n || e.length === 0 || !Gw.test(e)) && re(`${t} must be lowercase hexadecimal${n === void 0 ? "" : ` of length ${n}`}`), e;
}
function bS(e, t, n, r) {
  return (typeof e != "number" || !Number.isSafeInteger(e) || e < n || e > r) && re(`${t} must be an integer in ${n}..${r}`), e;
}
function ct(...e) {
  const t = new Uint8Array(e.reduce((r, i) => r + i.length, 0));
  let n = 0;
  for (const r of e)
    t.set(r, n), n += r.length;
  return t;
}
function Er(e, t, n, r) {
  return t + n > e.length && re(`transaction is truncated while reading ${r}`), [e.slice(t, t + n), t + n];
}
function Ao(e, t, n) {
  t >= e.length && re(`transaction is truncated before ${n}`);
  const r = e[t];
  return r >= 253 && re(`${n} must use a single-byte CompactSize`), [r, t + 1];
}
function Da(e) {
  return ((e[0] ?? 0) | (e[1] ?? 0) << 8 | (e[2] ?? 0) << 16 | (e[3] ?? 0) << 24) >>> 0;
}
function vS(e) {
  let t = 0n;
  for (let n = 0; n < 8; n += 1) t |= BigInt(e[n] ?? 0) << BigInt(n * 8);
  return t;
}
function ai(e) {
  return new Uint8Array([e & 255, e >>> 8 & 255, e >>> 16 & 255, e >>> 24 & 255]);
}
function rf(e) {
  const t = new Uint8Array(8);
  let n = e;
  for (let r = 0; r < 8; r += 1)
    t[r] = Number(n & 0xffn), n >>= 8n;
  return t;
}
function Ww(e) {
  if (e.length === 25 && e[0] === 118 && e[1] === 169 && e[2] === 20 && e[23] === 136 && e[24] === 172) return "p2pkh";
  if (e.length === 23 && e[0] === 169 && e[1] === 20 && e[22] === 135) return "p2sh";
  if (e.length === 22 && e[0] === 0 && e[1] === 20) return "p2wpkh";
  if (e.length === 34 && e[0] === 0 && e[1] === 32) return "p2wsh";
  if (e.length === 34 && e[0] === 81 && e[1] === 32) return "p2tr";
  re("transaction output uses an unsupported script template");
}
function xS(e) {
  (e.length === 0 || e.length % 2 || e.length > qw * 2 || !Gw.test(e)) && re("unsignedTransaction must be lowercase hex, without 0x or whitespace, and at most 137 bytes");
  const t = Uint8Array.from(e.match(/../g), (L) => Number.parseInt(L, 16));
  (t.length < 4 || t[0] !== 2 || t[1] !== 0 || t[2] !== 0 || t[3] !== 0) && re("transaction version must be exactly 2");
  let n = 4;
  const [r, i] = Ao(t, n, "input count");
  n = i, r !== 1 && re("transaction must contain exactly one input");
  const [o, s] = Er(t, n, 32, "outpoint hash");
  n = s, o.every((L) => L === 0) && re("outpoint hash must not be null");
  const [a, c] = Er(t, n, 4, "outpoint index");
  n = c;
  const l = Da(a), [u, d] = Ao(t, n, "scriptSig length");
  n = d, u !== 0 && re("scriptSig must be empty");
  const [f, h] = Er(t, n, 4, "sequence");
  n = h;
  const y = Da(f);
  y !== 4294967293 && y !== 4294967294 && y !== 4294967295 && re("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff");
  const [w, p] = Ao(t, n, "output count");
  n = p, w !== 1 && w !== 2 && re("transaction must contain one or two outputs");
  const g = [], _ = { p2pkh: 546n, p2sh: 540n, p2wpkh: 294n, p2wsh: 330n, p2tr: 330n };
  for (let L = 0; L < w; L += 1) {
    const [H, $] = Er(t, n, 8, `output ${L} amount`);
    n = $;
    const D = vS(H);
    (D < 1n || D > $c) && re(`output ${L} amount is outside the Bitcoin money range`);
    const [z, x] = Ao(t, n, `output ${L} script length`);
    n = x;
    const [v, b] = Er(t, n, z, `output ${L} script`);
    n = b;
    const m = Ww(v);
    z !== { p2pkh: 25, p2sh: 23, p2wpkh: 22, p2wsh: 34, p2tr: 34 }[m] && re(`output ${L} script length is not canonical for ${m}`), D < _[m] && re(`output ${L} is below the V1 ${m} dust floor`), g.push({ amountSats: D, scriptPubKey: v, kind: m });
  }
  const [k, C] = Er(t, n, 4, "locktime");
  n = C;
  const B = Da(k);
  B !== 0 && re("locktime must be exactly zero"), n !== t.length && re("transaction has trailing bytes");
  const R = g.reduce((L, H) => L + H.amountSats, 0n);
  return (R < 1n || R > $c) && re("output total is outside the Bitcoin money range"), { raw: t, prevoutHashLe: o, prevoutIndex: l, sequence: y, outputs: g, locktime: B };
}
function kS(e, t) {
  const n = le(new TextEncoder().encode(e));
  return le(ct(n, n, t));
}
function ES(e, t, n) {
  const r = ct(e.prevoutHashLe, ai(e.prevoutIndex)), i = ct(new Uint8Array([n.length]), n), o = e.outputs.map((a) => ct(rf(a.amountSats), new Uint8Array([a.scriptPubKey.length]), a.scriptPubKey)), s = ct(
    new Uint8Array([0, 0]),
    ai(2),
    ai(e.locktime),
    le(r),
    le(rf(t)),
    le(i),
    le(ai(e.sequence)),
    le(ct(...o)),
    new Uint8Array([0]),
    ai(0)
  );
  return kS("TapSighash", s);
}
function AS(e) {
  return BigInt(`0x${te(e)}`);
}
function of(e) {
  return el(le(e));
}
function sf(e, t) {
  const n = ct(new Uint8Array([e]), t), r = le(le(n)).slice(0, 4), i = ct(n, r);
  let o = AS(i), s = "";
  for (; o > 0n; ) {
    const a = Number(o % 58n);
    s = yS[a] + s, o /= 58n;
  }
  for (const a of i) {
    if (a !== 0) break;
    s = `1${s}`;
  }
  return s;
}
function SS(e, t, n) {
  let r = 0, i = 0;
  const o = [], s = (1 << n) - 1;
  for (const a of e)
    for (r = r << t | a, i += t; i >= n; )
      i -= n, o.push(r >> i & s);
  return i > 0 && o.push(r << n - i & s), o;
}
function _S(e) {
  const t = [996825010, 642813549, 513874426, 1027748829, 705979059];
  let n = 1;
  for (const r of e) {
    const i = n >>> 25;
    n = (n & 33554431) << 5 ^ r;
    for (let o = 0; o < 5; o += 1) i >>> o & 1 && (n ^= t[o]);
  }
  return n >>> 0;
}
function za(e, t, n) {
  const r = e === "bitcoin" ? "bc" : e === "regtest" ? "bcrt" : "tb", i = [t, ...SS(n, 8, 5)], o = [...r].map((l) => l.charCodeAt(0) >> 5).concat([0], [...r].map((l) => l.charCodeAt(0) & 31)), s = t === 0 ? mS : gS, a = _S(o.concat(i, [0, 0, 0, 0, 0, 0])) ^ s, c = Array.from({ length: 6 }, (l, u) => a >>> 5 * (5 - u) & 31);
  return `${r}1${i.concat(c).map((l) => wS[l]).join("")}`;
}
function Tc(e, t) {
  const n = Ww(t), r = e === "bitcoin";
  return n === "p2pkh" ? sf(r ? 0 : 111, t.slice(3, 23)) : n === "p2sh" ? sf(r ? 5 : 196, t.slice(2, 22)) : n === "p2wpkh" || n === "p2wsh" ? za(e, 0, t.slice(2)) : za(e, 1, t.slice(2));
}
function IS(e) {
  const t = of(e), n = ct(new Uint8Array([0, 20]), t);
  return [
    ct(new Uint8Array([118, 169, 20]), t, new Uint8Array([136, 172])),
    n,
    ct(new Uint8Array([169, 20]), of(n), new Uint8Array([135]))
  ];
}
function Ma(e, t) {
  return e.length === t.length && e.every((n, r) => n === t[r]);
}
function So(e) {
  const t = e / 100000000n, n = e % 100000000n;
  return n === 0n ? `${t}.00000000` : `${t}.${n.toString().padStart(8, "0")}`;
}
function Zw(e, t) {
  const n = e / BigInt(t), r = e % BigInt(t);
  if (r === 0n) return `${n.toString()} sat/vB`;
  const i = (r * 10000n / BigInt(t)).toString().padStart(4, "0").replace(/0+$/, "");
  return `${n.toString()}.${i} sat/vB`;
}
function Yw(e) {
  return te(le(le(e.raw)).slice().reverse());
}
function BS(e, t) {
  const n = t.transaction, r = X("bitcoin.network", { network: e.network }), i = [
    X("bitcoin.title"),
    ...t.highFeeWarning ? [X("bitcoin.highFeeWarning"), ""] : [],
    r,
    X("bitcoin.networkWarning"),
    X("bitcoin.input", { txid: te(n.prevoutHashLe.slice().reverse()), vout: n.prevoutIndex }),
    X("bitcoin.inputAmount", { sats: e.prevout.amountSats, btc: So(BigInt(e.prevout.amountSats)) }),
    X("bitcoin.outputs")
  ];
  return n.outputs.forEach((o, s) => {
    const a = t.sameKeyOutputIndexes.includes(s);
    i.push(X("bitcoin.output", {
      index: s + 1,
      address: Tc(e.network, o.scriptPubKey),
      sats: o.amountSats.toString(),
      btc: So(o.amountSats),
      role: X(a ? "bitcoin.sameKeyReturn" : "bitcoin.recipient")
    }));
  }), i.push(
    X("bitcoin.fee", { sats: t.feeSats.toString(), btc: So(t.feeSats), rate: Zw(t.feeSats, t.signedVsize) }),
    X("bitcoin.vsize", { vsize: t.signedVsize }),
    X(n.sequence === 4294967293 ? "bitcoin.rbfSignals" : "bitcoin.rbfDoesNotSignal"),
    X("bitcoin.netLeaving", { sats: t.netLeavingSats.toString(), btc: So(t.netLeavingSats) }),
    X("bitcoin.unsignedTxid", { txid: Yw(n) }),
    X("bitcoin.outputKey", { key: te(t.outputKeyXOnly) })
  ), i.join(`
`);
}
function $S(e, t) {
  const n = tf(e, "signingContext");
  nf(n, ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"], "bitcoin signingContext"), n.kind !== Qu && re("unsupported Bitcoin signing_context kind"), (typeof n.network != "string" || !pS.includes(n.network)) && re("bitcoin signing_context network is invalid");
  const r = Lo(n.unsignedTransaction, "unsignedTransaction");
  r.length > qw * 2 && re("unsignedTransaction exceeds the 137-byte V1 limit");
  const i = tf(n.prevout, "signingContext.prevout");
  nf(i, ["amountSats", "scriptPubKey"], "signingContext.prevout");
  const o = bS(i.amountSats, "prevout.amountSats", 1, Number($c)), s = Lo(i.scriptPubKey, "prevout.scriptPubKey", 68);
  s.startsWith("5120") || re("prevout.scriptPubKey must be a lowercase P2TR output key script"), n.digestAlgorithm !== ef && re("unsupported Bitcoin digest algorithm");
  const a = Lo(n.digest, "signingContext.digest", 64);
  return a !== t && re("Bitcoin signing_context digest must match the sign request digest exactly"), {
    kind: Qu,
    network: n.network,
    unsignedTransaction: r,
    prevout: { amountSats: o, scriptPubKey: s },
    digestAlgorithm: ef,
    digest: a
  };
}
function Xw(e, t, n) {
  const r = $S(e, t), i = xS(r.unsignedTransaction), o = Uint8Array.from(r.prevout.scriptPubKey.match(/../g), (g) => Number.parseInt(g, 16)), s = o.slice(2);
  let a;
  if (n !== void 0) {
    const g = Lo(n, "signing public key", 66);
    a = Uint8Array.from(g.match(/../g), (k) => Number.parseInt(k, 16)), a[0] !== 2 && a[0] !== 3 && re("signing public key must be compressed secp256k1");
    const _ = Qx(g);
    Ma(s, _) || re("prevout.scriptPubKey does not bind to the signing key's BIP86 output key");
  }
  if (a)
    for (const g of i.outputs)
      IS(a).some((_) => Ma(_, g.scriptPubKey)) && re("transaction sends to a same-key legacy or SegWit-v0 address this workflow cannot spend");
  const c = i.outputs.flatMap((g, _) => Ma(g.scriptPubKey, o) ? [_] : []);
  c.length > 1 && re("transaction may contain at most one same-key P2TR return output");
  const l = i.outputs.reduce((g, _) => g + _.amountSats, 0n), u = BigInt(r.prevout.amountSats) - l;
  u <= 0n && re("transaction fee must be positive");
  const d = i.raw.length * 4 + 68, f = Math.ceil(d / 4);
  u > 10000n * BigInt(f) && re("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling");
  const h = te(ES(i, BigInt(r.prevout.amountSats), o));
  h !== r.digest && re("Bitcoin signing_context digest does not match the independently computed BIP341 digest");
  const y = c.reduce((g, _) => g + i.outputs[_].amountSats, 0n), w = u > 100n * BigInt(f) || u * 100n > BigInt(r.prevout.amountSats) * 2n, p = BigInt(r.prevout.amountSats) - y;
  return {
    digest: h,
    display: BS(r, {
      transaction: i,
      outputKeyXOnly: s,
      feeSats: u,
      signedVsize: f,
      sameKeyOutputIndexes: c,
      highFeeWarning: w,
      netLeavingSats: p
    }),
    facts: {
      network: r.network,
      spendingAddress: Tc(r.network, o),
      inputTxid: te(i.prevoutHashLe.slice().reverse()),
      inputVout: i.prevoutIndex,
      inputAmountSats: BigInt(r.prevout.amountSats),
      outputs: i.outputs.map((g, _) => ({
        address: Tc(r.network, g.scriptPubKey),
        amountSats: g.amountSats,
        sameKey: c.includes(_)
      })),
      feeSats: u,
      feeRate: Zw(u, f),
      signedVsize: f,
      netLeavingSats: p,
      highFeeWarning: w,
      sequence: i.sequence,
      signalsRbf: i.sequence === 4294967293,
      locktime: i.locktime,
      unsignedTxid: Yw(i),
      ...n !== void 0 ? { internalKeyXOnly: n.slice(2).toLowerCase() } : {},
      outputKeyXOnly: te(s),
      unsignedTransaction: r.unsignedTransaction,
      prevoutScriptPubKey: r.prevout.scriptPubKey
    },
    transaction: i,
    outputKeyXOnly: s
  };
}
const af = `avault-agent-operation:v1
`;
function Ar(e) {
  if (typeof e != "object" || e === null || Array.isArray(e)) throw new Error("signing context is malformed");
  return e;
}
function cf(e) {
  return e.startsWith("0x") ? e.toLowerCase() : `0x${e.toLowerCase()}`;
}
function Ka(e, t) {
  const n = cf(e), r = cf(t);
  if (n !== r)
    throw new Error("signing digest does not match verified context");
  return r.slice(2);
}
function Uo(e) {
  return typeof e == "bigint" ? e.toString() : typeof e == "string" || typeof e == "number" || typeof e == "boolean" ? String(e) : e == null ? "" : Lr(e);
}
function Lr(e) {
  if (typeof e == "bigint") return JSON.stringify(e.toString());
  if (e === null || typeof e != "object") return JSON.stringify(e);
  if (Array.isArray(e)) return `[${e.map(Lr).join(",")}]`;
  const t = e;
  return `{${Object.keys(t).sort().filter((n) => t[n] !== void 0).map((n) => `${JSON.stringify(n)}:${Lr(t[n])}`).join(",")}}`;
}
const TS = nE([
  "function approve(address spender,uint256 amount)",
  // A common extension, not an EIP-20 method: the amount is added to the current allowance.
  "function increaseAllowance(address spender,uint256 addedValue)",
  "function transfer(address to,uint256 amount)",
  "function transferFrom(address from,address to,uint256 amount)",
  "function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)",
  "function setApprovalForAll(address operator,bool approved)",
  "function safeTransferFrom(address from,address to,uint256 tokenId)",
  "function safeTransferFrom(address from,address to,uint256 tokenId,bytes data)"
]), PS = {
  approve: "ERC-20/ERC-721 approve",
  increaseAllowance: "ERC-20 increaseAllowance (extension)",
  transfer: "ERC-20 transfer",
  transferFrom: "ERC-20/ERC-721 transferFrom",
  permit: "ERC-20 permit",
  setApprovalForAll: "ERC-721 setApprovalForAll",
  safeTransferFrom: "ERC-721 safeTransferFrom",
  safeTransferFromWithData: "ERC-721 safeTransferFrom"
}, OS = new Map(
  TS.map((e) => {
    const t = e.name === "safeTransferFrom" && e.inputs.length === 4 ? "safeTransferFromWithData" : e.name;
    return [sA(e), { call: t, item: e }];
  })
);
function CS(e) {
  return `${e.name}(${e.inputs.map((t) => t.type).join(",")})`;
}
function RS(e) {
  if (e == null || e === "" || e === "0x") return "0x";
  if (typeof e != "string" || !/^0x[0-9a-fA-F]*$/.test(e) || e.length % 2 !== 0)
    throw new Error("EVM transaction calldata is malformed");
  return e.toLowerCase();
}
const HS = /* @__PURE__ */ new Set([
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
]), LS = /* @__PURE__ */ new Set(["domain", "types", "primaryType", "message"]);
function Fa(e, t, n) {
  const r = Object.keys(e).filter((i) => !t.has(i));
  if (r.length > 0)
    throw new Error(`${n} carries unsupported field: ${r.sort().join(", ")}`);
}
function Jw(e) {
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
function US(e, t) {
  if (t.chainId === void 0 || t.chainId === null || t.chainId === "")
    throw new Error("EVM transaction chain id is required");
  if (String(t.chainId) !== String(e.chainId))
    throw new Error("EVM transaction chain id mismatch");
}
function NS(e) {
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
function DS(e) {
  if (e.length < 10) return null;
  const t = OS.get(e.slice(0, 10));
  if (!t) return null;
  let n;
  try {
    if (n = hA(t.item.inputs, `0x${e.slice(10)}`), Ql(t.item.inputs, n).toLowerCase() !== `0x${e.slice(10)}`) return null;
  } catch {
    return null;
  }
  return {
    call: t.call,
    signature: CS(t.item),
    arguments: t.item.inputs.map((r, i) => ({
      name: r.name ?? `arg${i}`,
      type: r.type,
      value: Uo(n[i])
    }))
  };
}
function zS(e, t, n) {
  const r = Jw(e), i = RS(e.data), o = (c) => (t[c] === void 0 ? 0n : t[c]).toString(), s = e.to === void 0 || e.to === null || e.to === "" ? null : String(e.to), a = s === null ? null : DS(i);
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
function MS(e) {
  const t = [X("evm.title", { chainId: e.chainId })];
  if (e.blindSigning && t.push(X("sign.blindNotice")), t.push(
    X("evm.type", { type: e.type }),
    e.to === null ? X("evm.contractCreation") : X("evm.to", { to: e.to }),
    X("evm.value", { value: e.value }),
    X("evm.nonce", { value: e.nonce }),
    X("evm.gas", { value: e.gas })
  ), e.gasPrice !== void 0 && t.push(X("evm.gasPrice", { value: e.gasPrice })), e.maxFeePerGas !== void 0 && t.push(X("evm.maxFeePerGas", { value: e.maxFeePerGas })), e.maxPriorityFeePerGas !== void 0 && t.push(X("evm.maxPriorityFeePerGas", { value: e.maxPriorityFeePerGas })), e.decoded) {
    t.push(X("evm.call", { call: `${PS[e.decoded.call]} ${e.decoded.signature}` }));
    for (const n of e.decoded.arguments) t.push(`  ${n.name} (${n.type}): ${n.value}`);
  }
  return t.push(
    e.selector === null ? X("evm.selectorNone") : X("evm.selector", { selector: e.selector }),
    e.calldata === "0x" ? X("evm.calldataNone") : X("evm.calldata", { bytes: e.calldataBytes, calldata: e.calldata })
  ), t.join(`
`);
}
function KS(e) {
  Xr(e);
  const t = Ar(e), n = {
    "evm-transaction": ["kind", "chainId", "unsignedTransaction", "digestAlgorithm", "digest"],
    "eip-712-typed-data": ["kind", "typedData", "digestAlgorithm", "digest"],
    "avault-agent-operation": ["kind", "canonicalPreimage", "digestAlgorithm", "digest"],
    "bitcoin-taproot-keypath-v1": ["kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"]
  }, r = Object.hasOwn(n, e.kind) ? n[e.kind] : void 0;
  if (!r) throw new Error("unsupported signing context");
  if (Fa(t, new Set(r), "signing context"), typeof e.digest != "string" || !/^(?:0x)?[0-9a-fA-F]{64}$/.test(e.digest))
    throw new Error("signing context digest must be 32 bytes");
  switch (e.kind) {
    case "evm-transaction": {
      if (e.digestAlgorithm !== "keccak256") throw new Error("unsupported EVM digest algorithm");
      const i = Ar(e.unsignedTransaction);
      Fa(i, HS, "EVM transaction"), US(e, i), Jw(i);
      const o = NS(i), s = XA(o), a = Ka(e.digest, Pt(s)), c = zS(i, o, s);
      if (hS(s) !== c.type) throw new Error("EVM transaction type does not match its serialization");
      return {
        digest: a,
        challenge: tt(a),
        display: MS(c),
        evm: c
      };
    }
    case "eip-712-typed-data": {
      if (e.digestAlgorithm !== "eip712") throw new Error("unsupported EIP-712 digest algorithm");
      const i = Ar(e.typedData);
      Fa(i, LS, "EIP-712 typed data");
      const o = Ar(i.domain);
      if (o.chainId === void 0 || o.verifyingContract === void 0)
        throw new Error("EIP-712 domain must include chainId and verifyingContract");
      const s = Ka(e.digest, lS(i));
      return {
        digest: s,
        challenge: tt(s),
        display: [
          "EIP-712 typed data",
          `domain.chainId: ${Uo(o.chainId)}`,
          `domain.verifyingContract: ${Uo(o.verifyingContract)}`,
          `domain: ${Lr(o)}`,
          `types: ${Lr(Ar(i.types))}`,
          `primaryType: ${Uo(i.primaryType)}`,
          `message: ${Lr(Ar(i.message))}`
        ].join(`
`),
        typedData: i
      };
    }
    case "avault-agent-operation": {
      if (e.digestAlgorithm !== "avault-operation-hash-v1") throw new Error("unsupported avault digest algorithm");
      if (typeof e.canonicalPreimage != "string" || !e.canonicalPreimage.startsWith(af) || e.canonicalPreimage.slice(af.length).trim().length === 0)
        throw new Error("canonical preimage must start with avault-agent-operation:v1\\n followed by operation text; recreate the request");
      const i = Ka(e.digest, hx(le(new TextEncoder().encode(e.canonicalPreimage))));
      return {
        digest: i,
        challenge: tt(i),
        display: ["Avault agent operation", e.canonicalPreimage].join(`
`)
      };
    }
    case "bitcoin-taproot-keypath-v1": {
      const i = Xw(e, e.digest);
      return {
        digest: i.digest,
        challenge: tt(i.digest),
        display: i.display
      };
    }
    default:
      throw new Error("unsupported signing context");
  }
}
function No(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) return;
  const t = e.messageHex;
  if (typeof t == "string")
    try {
      Zh(t);
    } catch {
      throw new S("invalid_payload", "signing messageHex exceeds the 1 MiB decoded limit");
    }
}
function sa(e) {
  if (No(e), Xr(e), !e || typeof e != "object" || Array.isArray(e)) throw new S("invalid_payload", "Solana signing context must be an object");
  const t = e;
  if (t.kind !== "solana-transaction-v1" || Object.keys(t).some((n) => n !== "kind" && n !== "messageHex") || typeof t.messageHex != "string" || t.messageHex.length === 0 || t.messageHex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(t.messageHex))
    throw new S("invalid_payload", "Solana context requires only kind and nonempty lowercase even messageHex; no digest alias");
  return { kind: "solana-transaction-v1", messageHex: t.messageHex };
}
function FS(e, t, n) {
  if (n.kind !== "keypair" || !n.publicKey) throw new Error("protected signing requires a keypair protected record");
  const r = Yr(n.curve);
  if (e === Bi || t.kind === "solana-transaction-v1" || r === "ed25519") {
    if (e !== Bi || t.kind !== "solana-transaction-v1" || r !== "ed25519")
      throw new S("invalid_payload", "Ed25519 signing requires an Ed25519 record, ed25519 scheme and Solana context together");
    sa(t);
  }
}
const Qw = "11111111111111111111111111111111", eg = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", jS = "SysvarRecentB1ockHashes11111111111111111111", VS = 2, qS = 4, GS = 3, WS = 12;
function en(e) {
  return { decoded: null, undecodedReason: e };
}
function tg(e, t) {
  let n = 0n;
  for (let r = 7; r >= 0; r -= 1) n = n << 8n | BigInt(e[t + r]);
  return n.toString();
}
function sd(e) {
  const t = [];
  for (const n of e) {
    if (n.source !== "static") return null;
    t.push(n.address);
  }
  return t;
}
function ZS(e, t) {
  const n = t.length >= 4 ? (t[0] | t[1] << 8 | t[2] << 16 | t[3] << 24) >>> 0 : -1;
  return n === VS && t.length === 12 ? YS(e, t) : n === qS && t.length === 4 ? XS(e) : en("unsupported_instruction");
}
function YS(e, t) {
  if (e.length !== 2) return en("unsupported_instruction");
  const n = sd(e);
  if (!n) return en("unresolved_account");
  const [r, i] = n;
  return { decoded: { kind: "system-transfer", source: r, destination: i, lamports: tg(t, 4) }, undecodedReason: null };
}
function XS(e) {
  if (e.length !== 3) return en("unsupported_instruction");
  const t = sd(e);
  if (!t) return en("unresolved_account");
  const [n, r, i] = t;
  return r !== jS ? en("unsupported_instruction") : { decoded: { kind: "system-advance-nonce", nonceAccount: n, recentBlockhashesSysvar: r, nonceAuthority: i }, undecodedReason: null };
}
function JS(e, t) {
  const n = t.length === 9 && t[0] === GS && e.length === 3, r = t.length === 10 && t[0] === WS && e.length === 4;
  if (!n && !r) return en("unsupported_instruction");
  const i = sd(e);
  if (!i) return en("unresolved_account");
  const o = tg(t, 1);
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
function QS(e, t, n) {
  return e === Qw ? ZS(t, n) : e === eg ? JS(t, n) : en("unsupported_program");
}
const e_ = 32, ja = 512, ci = 256, lf = 8, ng = 3, rg = /* @__PURE__ */ new Map([
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
]), ig = /* @__PURE__ */ new Set(["1", "10", "8453", "42161", "11155111", "11155420", "84532", "421614"]);
function og(e) {
  return rg.has(e) ? [] : ["review.note.unknownChain"];
}
function sg(e) {
  const t = rg.get(e);
  return t === void 0 ? e : `${e} · ${t}`;
}
function Bs(e) {
  return typeof e == "object" && e !== null && !Array.isArray(e);
}
function ag(e) {
  if (typeof e == "bigint") return e;
  if (typeof e != "string" && typeof e != "number" && typeof e != "boolean" && !Array.isArray(e)) return null;
  try {
    return BigInt(e);
  } catch {
    return null;
  }
}
function Cn(e, t) {
  if (/^u?int\d*$/.test(e)) {
    const n = ag(t);
    return n === null ? JSON.stringify(t) ?? String(t) : n.toString();
  }
  return typeof t == "string" ? t : JSON.stringify(t) ?? String(t);
}
class df extends Error {
}
function $s(e, t, n, r, i, o) {
  if (i > e_) {
    o.omitted += 1;
    return;
  }
  const s = e[t];
  if (s !== void 0) {
    if (!Object.hasOwn(e, t) || !Array.isArray(s) || n === null || n === void 0) throw new df(r);
    const a = Object(n), c = new Set(s.map((l) => l.name));
    for (const l of s) $s(e, l.type, a[l.name], r ? `${r}.${l.name}` : l.name, i + 1, o);
    if (typeof n == "object")
      for (const l of Object.keys(n)) c.has(l) || o.uncovered.push(r ? `${r}.${l}` : l);
    return;
  }
  if (t !== "bytes" && t !== "string" && t.endsWith("]")) {
    if (!Array.isArray(n)) throw new df(r);
    const a = t.slice(0, t.lastIndexOf("["));
    n.length === 0 && o.covered.push({ path: r, type: t, value: "[]" }), n.forEach((c, l) => $s(e, a, c, `${r}[${l}]`, i + 1, o));
    return;
  }
  o.covered.push({ path: r, type: t, value: Cn(t, n) });
}
function t_(e, t) {
  const n = /* @__PURE__ */ new Set(), r = (i) => {
    const o = /^\w*/.exec(i)[0];
    if (!(n.has(o) || !e[o])) {
      n.add(o);
      for (const s of e[o]) r(s.type);
    }
  };
  return r(t), n;
}
const cg = [["token", "address"], ["amount", "uint160"], ["expiration", "uint48"], ["nonce", "uint48"]], lg = [["token", "address"], ["amount", "uint256"]], n_ = { primaryType: "Permit", types: { Permit: [["owner", "address"], ["spender", "address"], ["value", "uint256"], ["nonce", "uint256"], ["deadline", "uint256"]] } }, r_ = { primaryType: "PermitSingle", types: { PermitSingle: [["details", "PermitDetails"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: cg } }, i_ = { primaryType: "PermitBatch", types: { PermitBatch: [["details", "PermitDetails[]"], ["spender", "address"], ["sigDeadline", "uint256"]], PermitDetails: cg } }, o_ = {
  primaryType: "PermitTransferFrom",
  types: { PermitTransferFrom: [["permitted", "TokenPermissions"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: lg }
}, s_ = {
  primaryType: "PermitBatchTransferFrom",
  types: { PermitBatchTransferFrom: [["permitted", "TokenPermissions[]"], ["spender", "address"], ["nonce", "uint256"], ["deadline", "uint256"]], TokenPermissions: lg }
};
function li(e, t, n) {
  if (t !== n.primaryType) return !1;
  const r = t_(e, t), i = Object.keys(n.types);
  return r.size !== i.length || !i.every((o) => r.has(o)) ? !1 : i.every((o) => {
    const s = e[o], a = n.types[o];
    return Array.isArray(s) && s.length === a.length && s.every((c, l) => c.name === a[l][0] && c.type === a[l][1]);
  });
}
const uf = (1n << 256n) - 1n, a_ = (1n << 160n) - 1n;
function Va(e, t, n, r, i) {
  const o = ag(t), s = o === n;
  return {
    field: {
      label: e,
      ...i === void 0 ? {} : { index: i },
      value: o === null ? Cn("string", t) : o.toString(),
      ...s ? { shownAs: r, notes: ["review.note.exactInDetails"] } : { notes: ["review.note.rawTokenUnits"] }
    },
    isMax: s
  };
}
function Mn(e) {
  return typeof e == "string" ? e : Cn("string", e);
}
function qa(e, t, n) {
  return { label: e, value: Cn("uint256", t), time: "unix-seconds", notes: [n] };
}
function ff(e) {
  const t = e.filter((o) => o.isMax), n = e.filter((o) => !o.isMax), r = n.slice(0, Math.max(0, ng - t.length)), i = n.length - r.length;
  return {
    rows: [
      ...[...t, ...r].flatMap((o) => o.rows()),
      ...i > 0 ? [{ label: "review.field.moreTokens", value: "", notes: [{ key: "review.note.moreEntriesInDetails", count: i }] }] : []
    ],
    maxima: t.length
  };
}
function hf(e, t, n, r) {
  return t.maxima <= ng ? [e, ...t.rows, r] : [
    e,
    { label: "review.field.maxEntries", value: String(t.maxima), notes: [{ key: "review.note.ofEntries", count: n }] },
    r,
    ...t.rows
  ];
}
function c_(e, t, n) {
  const r = ["review.warning.tokenAuthorization"];
  if (li(e, t, n_)) {
    const s = Va("review.field.permitAmount", n.value, uf, "review.value.unlimited");
    return {
      kind: "review.kind.erc2612Permit",
      warnings: s.isMax ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: [
        { label: "review.field.spender", value: Mn(n.spender), elide: !0 },
        s.field,
        qa("review.field.permitDeadline", n.deadline, "review.note.permitDeadline")
      ],
      identity: [
        { label: "review.field.permitOwner", value: Mn(n.owner), elide: !0 },
        { label: "review.field.permitNonce", value: Cn("uint256", n.nonce) }
      ]
    };
  }
  const i = li(e, t, r_);
  if (i || li(e, t, i_)) {
    const s = i ? [n.details] : Array.isArray(n.details) ? n.details : [], a = ff(s.map((c, l) => {
      const u = Bs(c) ? c : {}, d = Va("review.field.permitTokenAmount", u.amount, a_, "review.value.unlimited", l + 1);
      return {
        isMax: d.isMax,
        rows: () => [
          d.field,
          { label: "review.field.permitToken", index: l + 1, value: Mn(u.token), elide: !0 },
          { label: "review.field.permitExpiration", index: l + 1, value: Cn("uint48", u.expiration), notes: ["review.note.permit2Expiration"] }
        ]
      };
    }));
    return {
      kind: "review.kind.permit2Allowance",
      warnings: a.maxima > 0 ? [...r, "review.warning.unlimitedAllowance"] : r,
      summary: hf(
        { label: "review.field.spender", value: Mn(n.spender), elide: !0 },
        a,
        s.length,
        qa("review.field.signatureDeadline", n.sigDeadline, "review.note.signatureDeadline")
      ),
      identity: []
    };
  }
  const o = li(e, t, o_);
  if (o || li(e, t, s_)) {
    const s = o ? [n.permitted] : Array.isArray(n.permitted) ? n.permitted : [], a = ff(s.map((c, l) => {
      const u = Bs(c) ? c : {}, d = Va("review.field.permitTokenAmount", u.amount, uf, "review.value.maximumOneTime", l + 1);
      return {
        isMax: d.isMax,
        rows: () => [d.field, { label: "review.field.permitToken", index: l + 1, value: Mn(u.token), elide: !0 }]
      };
    }));
    return {
      kind: "review.kind.permit2Transfer",
      warnings: a.maxima > 0 ? ["review.warning.oneTimeTransfer", "review.warning.maximumOneTimeTransfer"] : ["review.warning.oneTimeTransfer"],
      summary: hf(
        // The Permit2 contract signs msg.sender as the spender; this is the value in the message.
        { label: "review.field.spender", value: Mn(n.spender), elide: !0 },
        a,
        s.length,
        qa("review.field.permitDeadline", n.deadline, "review.note.signatureDeadline")
      ),
      identity: [{ label: "review.field.permitNonce", value: Cn("uint256", n.nonce) }]
    };
  }
  return null;
}
function dg(e) {
  return e.length <= ja ? { name: e } : { name: `${e.slice(0, ja / 2)} … ${e.slice(-ja / 4)}`, notes: ["review.note.pathShortened"] };
}
function Ga(e) {
  const { name: t, notes: n } = dg(`${e.path} (${e.type})`);
  return { label: "review.field.typedField", name: t, value: e.value, ...n ? { notes: n } : {} };
}
function l_(e, t) {
  const n = e, r = Bs(n.domain) ? n.domain : {}, i = n.types, o = {
    EIP712Domain: Mw({ domain: r }),
    ...i
  }, s = Ws(t.publicKey).eth;
  if (!s) throw new Error("EIP-712 review requires a secp256k1 signing key");
  const a = { covered: [], uncovered: [], omitted: 0 };
  $s(o, "EIP712Domain", r, "domain", 0, a);
  const c = a.covered, l = a.uncovered, u = new Set(o.EIP712Domain.map((L) => L.name)), d = (L, H) => {
    if (!Object.hasOwn(r, L)) return [];
    const $ = u.has(L), D = o.EIP712Domain.find((x) => x.name === L), z = $ && D ? Cn(D.type, r[L]) : Mn(r[L]);
    return [{
      label: H,
      value: L === "chainId" ? sg(z) : z,
      notes: [...L === "chainId" ? og(z) : [], ...$ ? [] : ["review.note.notInTypedHash"]]
    }];
  }, f = n.primaryType !== "EIP712Domain", h = Bs(n.message) ? n.message : {}, y = { covered: [], uncovered: l, omitted: 0 };
  f ? $s(o, n.primaryType, n.message, "", 0, y) : n.message !== void 0 && l.push("message");
  const w = y.covered, p = a.omitted + y.omitted;
  let g = null;
  try {
    g = f ? c_(o, n.primaryType, h) : null;
  } catch {
    g = null;
  }
  const _ = w.slice(0, lf).map((L) => L.type === "address" ? { ...Ga(L), elide: !0 } : Ga(L)), k = Math.max(0, w.length - lf) + p;
  k > 0 && _.push({ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInstructionsInDetails", count: k }] });
  const C = (L, H = 0) => {
    const $ = Math.max(0, L.length - ci) + H;
    return [
      ...L.slice(0, ci).map(Ga),
      ...$ > 0 ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: $ }] }] : []
    ];
  }, B = JSON.stringify(n, null, 2), R = [
    { title: "review.group.typedDomain", fields: C(c, a.omitted) },
    ...f ? [{ title: "review.group.typedMessage", fields: C(w, y.omitted) }] : [],
    ...l.length > 0 ? [{
      title: "review.group.notInTypedHash",
      fields: [
        ...l.slice(0, ci).map((L) => {
          const H = dg(L);
          return { label: "review.field.typedField", name: H.name, value: "", notes: ["review.note.notInTypedHash", ...H.notes ?? []] };
        }),
        ...l.length > ci ? [{ label: "review.field.moreFields", value: "", notes: [{ key: "review.note.moreInRawData", count: l.length - ci }] }] : []
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
      ...g ? g.summary : _,
      ...l.length > 0 ? [{ label: "review.field.notInTypedHash", value: "", notes: [{ key: "review.note.fieldsOutsideHash", count: l.length }] }] : [],
      ...d("chainId", "review.field.chainId"),
      ...d("name", "review.field.domainName"),
      ...d("verifyingContract", "review.field.verifyingContract").map((L) => ({ ...L, elide: !0 })),
      { label: "review.field.primaryType", value: n.primaryType, ...f ? {} : { notes: ["review.note.messageNotHashed"] } },
      ...g ? g.identity : []
    ],
    // Every hashed value is shown; nothing is blind.
    undecoded: !1,
    ...g ? { warnings: g.warnings } : {},
    details: R
  };
}
const pf = 3, yf = 3, d_ = 3;
function ug(e) {
  return e === Qw ? ["review.note.systemProgram"] : e === eg ? ["review.note.tokenProgram"] : [];
}
function aa(e) {
  return e.length / 2;
}
function fg(e) {
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
function u_(e) {
  return e.kind === "spl-transfer-checked" ? "TransferChecked" : "Transfer";
}
function wf(e) {
  return {
    label: "review.field.undecodedInstruction",
    value: e.programId,
    notes: [{ key: "review.note.dataBytes", count: aa(e.dataHex) }, "review.note.notDecoded"]
  };
}
function f_(e, t) {
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
function gf(e) {
  return e === 0 ? [] : [{ label: "review.field.durableNonce", value: "", ...e > 1 ? { notes: [{ key: "review.note.nonceAdvances", count: e }] } : {} }];
}
function h_(e) {
  const t = fg(e).map((r) => r.label === "review.field.amount" ? r : { ...r, elide: !0 }), n = ["review.field.amount", "review.field.to", "review.field.toTokenAccount", "review.field.mint", "review.field.from", "review.field.fromTokenAccount"];
  return [...t].sort((r, i) => n.indexOf(r.label) - n.indexOf(i.label));
}
const Ts = (e) => e.kind !== "system-advance-nonce";
function p_(e) {
  if (e.instructions.length === 1) {
    const [s] = e.instructions;
    return s.decoded ? Ts(s.decoded) ? h_(s.decoded) : gf(1) : [{ ...wf(s), elide: !0 }];
  }
  let t = 0, n = 0, r = 0;
  const i = e.instructions.flatMap((s) => {
    const a = s.decoded;
    return a && Ts(a) ? (t += 1, t <= pf ? f_(a, t) : []) : a ? (n += 1, []) : (r += 1, r <= yf ? [{ ...wf(s), elide: !0 }] : []);
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
    ...o("review.field.moreTransfers", "review.note.moreInDetails", t, pf),
    ...o("review.field.moreUndecoded", "review.note.moreInstructionsInDetails", r, yf),
    ...gf(n)
  ];
}
function y_(e) {
  return Ts(e) ? [
    { label: "review.field.instruction", value: u_(e) },
    ...fg(e),
    ...e.kind === "system-transfer" ? [] : [{ label: "review.field.authority", value: e.authority }]
  ] : [
    { label: "review.field.instruction", value: "AdvanceNonceAccount" },
    { label: "review.field.nonceAccount", value: e.nonceAccount },
    { label: "review.field.recentBlockhashesSysvar", value: e.recentBlockhashesSysvar },
    { label: "review.field.nonceAuthority", value: e.nonceAuthority }
  ];
}
function w_(e) {
  return e.map((t) => t.index).join(", ");
}
function g_(e) {
  const t = e.decoded, n = {
    label: "review.field.data",
    value: e.dataHex,
    notes: [{ key: "review.note.bytes", count: aa(e.dataHex) }, ...t ? [] : ["review.note.notDecoded"]]
  };
  return {
    title: "review.group.instruction",
    index: e.index + 1,
    fields: [
      { label: "review.field.program", value: e.programId, notes: ug(e.programId) },
      ...t ? y_(t) : [],
      { label: "review.field.accountIndexes", value: w_(e.accounts) },
      n
    ]
  };
}
function m_(e) {
  const t = e.instructions.length === 1 ? e.instructions[0].decoded : null;
  return {
    kind: !t || !Ts(t) ? "review.kind.solanaTransaction" : t.kind === "system-transfer" ? "review.kind.solanaSystemTransfer" : "review.kind.solanaTokenTransfer",
    signer: { label: "review.field.signingKey", value: e.expectedSigner.address, elide: !0 },
    summary: p_(e),
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
            ...ug(n.address)
          ]
        }))
      },
      ...e.instructions.map(g_),
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
function Pc(e) {
  return ig.has(e.chainId) ? { label: "review.field.value", value: e.value, native: "eth" } : { label: "review.field.value", value: e.value, notes: ["review.note.wei"] };
}
function hg(e) {
  return e.to === null ? { label: "review.field.contract", value: "", notes: ["review.note.contractCreation"] } : { label: "review.field.contract", value: e.to };
}
function pg(e) {
  return (e.decoded?.arguments ?? []).map((t, n) => ({
    label: "review.field.argument",
    index: n + 1,
    name: `${t.name} (${t.type})`,
    value: t.value
  }));
}
function b_(e) {
  if (e.decoded) {
    const n = { label: "review.field.call", value: e.decoded.signature }, r = pg(e).map((i) => i.name?.endsWith(" (address)") ? { ...i, elide: !0 } : i);
    return r.length <= d_ ? [n, ...r] : [{ ...n, notes: [{ key: "review.note.argumentsInDetails", count: r.length }] }];
  }
  if (e.calldata === "0x") return [];
  const t = [{ key: "review.note.dataBytes", count: e.calldataBytes }, "review.note.notDecoded"];
  return e.to === null || e.selector === null ? [{ label: "review.field.initCode", value: "", notes: t }] : [{ label: "review.field.selector", value: e.selector, notes: t }];
}
const mf = ((1n << 256n) - 1n).toString();
function v_(e) {
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
    const i = r.value === mf;
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
    const i = r.value === mf;
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
function x_(e) {
  const t = { label: "review.field.chainId", value: sg(e.chainId), notes: og(e.chainId) };
  if (e.to !== null && e.calldata === "0x")
    return { kind: "review.kind.evmTransaction", summary: [ig.has(e.chainId) ? { label: "review.field.nativeTransfer", value: e.value, native: "eth", unit: "symbol", to: e.to } : { label: "review.field.nativeTransfer", value: e.value, notes: ["review.note.wei"], to: e.to }, t] };
  const n = v_(e);
  return n ? {
    kind: n.kind,
    ...n.warnings ? { warnings: n.warnings } : {},
    // The native value the transaction carries, as encoded, never assumed to be zero.
    summary: [...n.lead, { label: "review.field.tokenContract", value: e.to, elide: !0 }, Pc(e), t]
  } : { kind: "review.kind.evmTransaction", summary: [{ ...hg(e), elide: !0 }, Pc(e), ...b_(e), t] };
}
function k_(e, t) {
  const n = Ws(t.publicKey).eth;
  if (!n) throw new Error("EVM review requires a secp256k1 signing key");
  const r = (a, c) => c === void 0 ? [] : [{ label: a, value: c, notes: ["review.note.wei"] }], { kind: i, summary: o, warnings: s } = x_(e);
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
          hg(e),
          Pc(e),
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
          ...pg(e)
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
            notes: [{ key: "review.note.bytes", count: aa(e.unsignedTransactionHex) - 1 }],
            raw: !0
          },
          { label: "review.field.digest", value: `0x${t.digest}` }
        ]
      }
    ]
  };
}
function E_(e, t) {
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
            notes: [{ key: "review.note.bytes", count: aa(e.unsignedTransaction) }],
            raw: !0
          },
          { label: "review.field.prevoutScript", value: e.prevoutScriptPubKey }
        ]
      }
    ]
  };
}
function ad(e, t) {
  try {
    const n = Jp(e.context.release?.derivation), r = t.accounts[n.account][n.chain], i = e.signingContext.kind;
    if (n.chain === "bitcoin" ? e.scheme !== "schnorr-secp256k1-bip341-keypath" || i !== "bitcoin-taproot-keypath-v1" : n.chain === "solana" ? e.scheme !== "ed25519" || i !== "solana-transaction-v1" : e.scheme !== "ecdsa-secp256k1-recoverable" || i !== "evm-transaction" && i !== "eip-712-typed-data") throw new Error("protected HD chain/scheme/context mismatch");
    const o = e.context.release?.signingKey;
    if (!o || typeof o != "object" || Array.isArray(o)) throw new Error("missing protected HD signing key binding");
    const s = o;
    if (Object.keys(s).length !== 3 || typeof s.secret_id != "string" || s.secret_id.length === 0 || s.public_key !== r || e.context.release?.publicKey !== r || typeof s.envelope_sha256 != "string" || !/^[0-9a-f]{64}$/.test(s.envelope_sha256) || s.envelope_sha256 !== tx(e.material.envelope)) throw new Error("protected HD signing identity mismatch");
    return { selection: n, publicKey: r, path: ss(n.chain, n.account) };
  } catch {
    throw new S("invalid_sign_binding", "protected HD signing requires the exact signed selection/key/envelope binding");
  }
}
async function yg(e, t) {
  if (e.context.kind !== "hd-seed") throw new Error("protected HD custody requires HD metadata");
  const n = await ji(e.sealed, e.vmk, e.context);
  let r;
  try {
    return r = Qp(n), ex(r.seed, e.identity.selection, (i) => {
      if (te(e.identity.selection.chain === "solana" ? ft.getPublicKey(i) : we.getPublicKey(i, !0)) !== e.identity.publicKey) throw new S("invalid_sign_binding", "protected HD seed does not match the selected authenticated public key");
      return t(i);
    });
  } finally {
    n.fill(0), as(r);
  }
}
async function A_(e) {
  return yg(e, (t) => e.payload.kind === "message" ? hy(t, e.payload.messageHex) : fy(t, e.payload.digest, e.request.scheme));
}
const S_ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function wi(e) {
  let t = 0;
  for (; t < e.length && e[t] === 0; ) t += 1;
  let n = 0n;
  for (const i of e) n = n << 8n | BigInt(i);
  let r = "";
  for (; n > 0n; )
    r = S_[Number(n % 58n)] + r, n /= 58n;
  return "1".repeat(t) + r;
}
class wg extends Error {
  code;
  constructor(t, n) {
    super(n), this.name = "SolanaMessageError", this.code = t;
  }
}
const _o = 1232, bf = 64, di = 32, __ = 32, vf = 128, xf = 256;
function _e(e, t) {
  throw new wg(e, t);
}
function kf(e) {
  let t = "";
  for (const n of e) t += n.toString(16).padStart(2, "0");
  return t;
}
function I_(e) {
  return e < 128 ? 1 : e < 16384 ? 2 : 3;
}
function B_(e, t, n = "compact-u16") {
  let r = 0;
  for (let i = 0; ; i += 1) {
    t + i >= e.length && _e("truncated", `message ends inside ${n}`);
    const o = e[t + i];
    o === 0 && i !== 0 && _e("invalid_compact_u16", `${n} is not minimally encoded`);
    const s = (o & 128) === 0;
    if (i === 2 && !s && _e("invalid_compact_u16", `${n} is longer than three bytes`), r |= (o & 127) << 7 * i, r > 65535 && _e("invalid_compact_u16", `${n} exceeds 65535`), s) return { value: r, length: i + 1 };
  }
}
class $_ {
  bytes;
  offset = 0;
  constructor(t) {
    this.bytes = t;
  }
  get remaining() {
    return this.bytes.length - this.offset;
  }
  u8(t) {
    return this.remaining < 1 && _e("truncated", `message ends before ${t}`), this.bytes[this.offset++];
  }
  take(t, n) {
    this.remaining < t && _e("truncated", `message ends inside ${n}`);
    const r = this.bytes.subarray(this.offset, this.offset + t);
    return this.offset += t, r;
  }
  compactU16(t) {
    const { value: n, length: r } = B_(this.bytes, this.offset, t);
    return this.offset += r, n;
  }
}
function gg(e) {
  if (typeof e == "object" && e !== null) {
    for (const t of Object.values(e)) gg(t);
    Object.freeze(e);
  }
  return e;
}
function T_(e, t) {
  e instanceof Uint8Array || _e("invalid_input", "message must be a byte array"), (!(t instanceof Uint8Array) || t.length !== di) && _e("invalid_input", "expected public key must be 32 bytes"), e.length > _o - 1 - bf && _e("packet_too_large", `message of ${e.length} bytes cannot fit a ${_o}-byte transaction`);
  const n = new Uint8Array(e), r = wi(new Uint8Array(t)), i = new $_(n), o = i.u8("message prefix");
  let s = "legacy", a = o;
  if ((o & vf) !== 0) {
    const $ = o & ~vf;
    $ !== 0 && _e("unsupported_version", `Solana message version ${$} is not supported`), s = "v0", a = i.u8("required signature count");
  }
  const c = i.u8("read-only signed account count"), l = i.u8("read-only unsigned account count"), u = i.compactU16("account key count"), d = i.take(u * di, "account keys"), f = Array.from({ length: u }, ($, D) => wi(d.subarray(D * di, (D + 1) * di))), h = wi(i.take(__, "recent blockhash")), y = [];
  for (let $ = i.compactU16("instruction count"); $ > 0; $ -= 1) {
    const D = i.u8("instruction program id index"), z = i.take(i.compactU16("instruction account count"), "instruction account indexes"), x = i.take(i.compactU16("instruction data length"), "instruction data");
    y.push({ programIdIndex: D, accountIndexes: z, data: x });
  }
  const w = [];
  if (s === "v0")
    for (let $ = i.compactU16("address table lookup count"); $ > 0; $ -= 1) {
      const D = i.take(di, "address table key"), z = i.take(i.compactU16("writable lookup index count"), "writable lookup indexes"), x = i.take(i.compactU16("read-only lookup index count"), "read-only lookup indexes");
      w.push({ accountKey: D, writableIndexes: z, readonlyIndexes: x });
    }
  i.remaining !== 0 && _e("trailing_bytes", `${i.remaining} bytes follow the message`), a + l > u && _e("invalid_header", "header counts more signed and read-only unsigned accounts than the message carries"), c >= a && _e("invalid_header", "header leaves no writable signer to pay the fee");
  let p = 0, g = 0;
  for (const $ of w)
    $.writableIndexes.length + $.readonlyIndexes.length === 0 && _e("invalid_address_table_lookup", "an address table lookup loads no account"), p += $.writableIndexes.length, g += $.readonlyIndexes.length;
  const _ = u + p + g;
  _ > xf && _e("invalid_address_table_lookup", `message addresses ${_} accounts; the limit is ${xf}`);
  for (const $ of y) {
    ($.programIdIndex === 0 || $.programIdIndex >= u) && _e("invalid_program_id_index", `instruction program id index ${$.programIdIndex} is not a static non-payer key`);
    for (const D of $.accountIndexes)
      D >= _ && _e("invalid_account_index", `instruction account index ${D} is outside the message's ${_} accounts`);
  }
  const k = I_(a) + a * bf + n.length;
  k > _o && _e("packet_too_large", `signed transaction would be ${k} bytes; the limit is ${_o}`);
  const C = f.indexOf(r);
  (C === -1 || C >= a) && _e("signer_not_required", "the expected key is not a required signer of this message");
  const B = f.map(($, D) => ({
    index: D,
    address: $,
    signer: D < a,
    writable: D < a ? D < a - c : D < u - l
  })), R = w.map(($) => ({
    accountKey: wi($.accountKey),
    writableIndexes: Array.from($.writableIndexes),
    readonlyIndexes: Array.from($.readonlyIndexes)
  })), L = ($) => {
    if ($ < u) return { source: "static", ...B[$] };
    const D = $ - u < p;
    let z = $ - u - (D ? 0 : p);
    for (const [x, v] of R.entries()) {
      const b = D ? v.writableIndexes : v.readonlyIndexes;
      if (z < b.length)
        return { source: "lookup", index: $, lookupIndex: x, accountKey: v.accountKey, tableIndex: b[z], writable: D };
      z -= b.length;
    }
    throw new Error("account index was validated against the lookup tables");
  }, H = y.map(($, D) => {
    const z = f[$.programIdIndex], x = Array.from($.accountIndexes, L);
    return {
      index: D,
      programIdIndex: $.programIdIndex,
      programId: z,
      accounts: x,
      dataHex: kf($.data),
      ...QS(z, x, $.data)
    };
  });
  return gg({
    version: s,
    messageHex: kf(n),
    messageLength: n.length,
    transactionLength: k,
    header: { numRequiredSignatures: a, numReadonlySignedAccounts: c, numReadonlyUnsignedAccounts: l },
    feePayer: f[0],
    expectedSigner: { index: C, address: r, isFeePayer: C === 0 },
    staticAccountKeys: B,
    recentBlockhash: h,
    instructions: H,
    addressTableLookups: R,
    hasUndecodedContent: R.length > 0 || H.some(($) => $.decoded === null)
  });
}
function Sr(e, t) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new S("invalid_payload", `${t} must be an object`);
  return e;
}
function Io(e, t) {
  if (typeof e != "string" || e.length === 0) throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function gi(e, t, n) {
  if (Object.keys(e).some((r) => !t.includes(r)))
    throw new S("invalid_payload", `${n} carries unsupported fields; script trees are not supported`);
}
function cd(e) {
  if (e && typeof e == "object") {
    for (const t of Object.values(e)) cd(t);
    Object.freeze(e);
  }
  return e;
}
function ld(e, t) {
  if (No(e), t && typeof t == "object") {
    const n = t.release;
    No(n), n && typeof n == "object" && No(n.signingContext);
  }
}
function mg(e) {
  const t = Sr(e, "sign request");
  ld(t.signingContext, t.context), Xr(t);
  const n = Sr(structuredClone(t), "sign request");
  gi(n, ["material", "scheme", "signingContext", "context"], "sign request");
  const r = Sr(n.material, "material");
  gi(r, ["name", "envelope"], "material");
  const i = Sr(r.envelope, "envelope");
  gi(i, ["ciphertext", "nonce", "wrap_meta"], "envelope");
  const o = Kl(n.context);
  gi(Sr(o, "context"), ["v", "purpose", "requestId", "grantId", "display", "release", "agent", "expiresAt", "signature"], "sign context");
  let s;
  try {
    s = vl(n.scheme);
  } catch {
    throw new S("invalid_payload", "unsupported signing scheme");
  }
  const a = Sr(n.signingContext, "signingContext");
  return a.kind === "solana-transaction-v1" && sa(a), cd({
    material: {
      name: Io(r.name, "material.name"),
      envelope: {
        ciphertext: Io(i.ciphertext, "envelope.ciphertext"),
        nonce: Io(i.nonce, "envelope.nonce"),
        wrap_meta: Io(i.wrap_meta, "envelope.wrap_meta")
      }
    },
    scheme: s,
    signingContext: a,
    context: o
  });
}
function St() {
  throw new S("invalid_sign_binding", "signing approval is missing or does not match its signed binding; update Avibe and recreate the request");
}
function bg(e, t, n) {
  ld(e.signingContext, e.context), _s({ context: e.context, expectedPurpose: "sign", secret: { name: e.material.name, kind: "keypair" } });
  const r = e.context.release;
  (!r || Array.isArray(r)) && St();
  const i = t.kind === "message";
  !i && (t.kind !== "digest" || !/^[0-9a-f]{64}$/.test(t.digest)) && St();
  const o = Un(e.material.envelope).recordMetadata, s = o?.kind === "hd-seed" ? ad(e, o) : void 0;
  if (s && s.publicKey !== n && St(), gi(r, ["name", "scheme", i ? "messageHex" : "digest", "publicKey", "signingContext", ...s ? ["derivation", "signingKey"] : []], "sign context.release"), i) {
    const a = sa(e.signingContext);
    (t.messageHex !== a.messageHex || r.messageHex !== t.messageHex || e.scheme !== "ed25519" || !/^[0-9a-f]{64}$/.test(n) || r.publicKey !== n) && St();
  } else (e.scheme === "ed25519" || e.signingContext.kind === "solana-transaction-v1" || r.digest !== t.digest) && St();
  (r.name !== e.material.name || r.scheme !== e.scheme || typeof r.publicKey != "string" || r.publicKey.toLowerCase() !== n.toLowerCase() || !r.signingContext || pe(r.signingContext) !== pe(e.signingContext)) && St();
}
function P_(e) {
  return ld(e.signingContext, e.context), le(new TextEncoder().encode(ta(e.context)));
}
function O_(e, t) {
  if (vl(e), e === "ed25519") throw new Error("Ed25519 exact-message signing is reviewed structurally and has no prose description");
  if (!/^(02|03)[0-9a-fA-F]{64}$/.test(t)) throw new Error("signing public key must be compressed secp256k1");
  return e === bl ? `BIP340 raw Schnorr (untweaked internal key)
internal key: ${t.slice(2).toLowerCase()}` : e === $i ? X("bitcoin.signingIdentity", { key: t.slice(2).toLowerCase() }) : `${e === Fs ? "ECDSA secp256k1 recoverable" : "ECDSA secp256k1 DER"}
public key: ${t.toLowerCase()}`;
}
function C_(e, t) {
  const n = sa(e.signingContext);
  bg(e, { kind: "message", messageHex: n.messageHex }, t);
  let r;
  try {
    r = T_(tl(n.messageHex), tt(t));
  } catch (i) {
    throw i instanceof wg ? new S("invalid_payload", `Solana message cannot be signed (${i.code}): ${i.message}`) : i;
  }
  return r.messageHex !== n.messageHex && St(), { payload: { kind: "message", messageHex: r.messageHex }, review: m_(r) };
}
function R_(e, t) {
  const n = e.signingContext;
  n.kind === "solana-transaction-v1" && St();
  let r = KS(n);
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    if (e.scheme !== $i)
      throw new S("invalid_payload", "Bitcoin Taproot contexts require the BIP341 key-path signing scheme");
  } else {
    if (e.scheme === $i)
      throw new S("invalid_payload", "the BIP341 key-path signing scheme requires a Bitcoin Taproot context");
    if (n.kind !== "avault-agent-operation" && e.scheme !== Fs && e.scheme !== ml)
      throw new S("invalid_payload", "EVM signing contexts require an ECDSA scheme");
  }
  let i;
  if (n.kind === "bitcoin-taproot-keypath-v1") {
    const o = Xw(n, r.digest, t);
    r = { digest: o.digest, challenge: r.challenge, display: o.display }, i = o.facts;
  }
  return bg(e, { kind: "digest", digest: r.digest }, t), {
    payload: { kind: "digest", digest: r.digest },
    display: `${O_(e.scheme, t)}
scheme: ${e.scheme}
${r.display}`,
    ...r.evm ? { review: k_(r.evm, { publicKey: t, scheme: e.scheme, digest: r.digest }) } : {},
    ...i ? { review: E_(i, { scheme: e.scheme, digest: r.digest }) } : {},
    ...H_(r.typedData, { publicKey: t, scheme: e.scheme, digest: r.digest })
  };
}
function H_(e, t) {
  if (e === void 0) return {};
  try {
    const n = l_(e, t);
    return Dy(JSON.parse(JSON.stringify(n))), { review: n };
  } catch {
    return {};
  }
}
function vg(e) {
  const t = Un(e.material.envelope), n = Gs(e.material.name, t.recordMetadata), r = t.recordMetadata?.kind === "hd-seed" ? ad(e, t.recordMetadata) : void 0, i = r ? {
    name: e.material.name,
    kind: "keypair",
    publicKey: r.publicKey,
    ...r.selection.chain === "solana" ? { curve: "ed25519" } : {}
  } : n;
  FS(e.scheme, e.signingContext, i);
  const o = i.publicKey, s = e.signingContext.kind === "solana-transaction-v1" ? C_(e, o) : R_(e, o);
  if (r) {
    const a = `#${r.selection.account} · ${r.path}${r.selection.chain === "bitcoin" ? " · Taproot" : ""}`;
    if (s.review) {
      const c = [{ label: "review.field.walletAccount", value: a }];
      s.review = { ...s.review, summary: [...s.review.summary, ...c], details: [{ title: "review.group.hdAccount", fields: c }, ...s.review.details] };
    } else if (typeof s.display == "string") {
      const c = Ws(r.publicKey).eth;
      (!c || r.selection.chain !== "evm") && St(), s.display = `${X("review.field.walletAccount")}: ${a}
${X("review.field.signingAddress")}: ${c}

${s.display}`;
    }
  }
  return { unpacked: t, recordContext: n, content: s, ...r ? { hd: r } : {} };
}
function L_(e) {
  const t = mg(e), { unpacked: n, recordContext: r, content: i } = vg(t), o = P_(t);
  return cd({ request: t, verified: i, unpacked: n, recordContext: r, challenge: [...o], ...n.recordMetadata?.kind === "hd-seed" ? { hd: ad(t, n.recordMetadata) } : {} });
}
async function xg(e, t, n) {
  const r = mg(e.request), i = vg(r);
  (!e.verified || pe(e.verified.payload) !== pe(i.content.payload)) && St();
  const o = await hs(n, t);
  return Jn({ context: r.context, rootMetadata: o, expectedPurpose: "sign" }), Ci([r.context]), i.hd && await yg({ sealed: i.unpacked.sealed, vmk: t, context: i.recordContext, identity: i.hd }, () => {
  }), { request: r, ...i };
}
async function Ef(e, t, n) {
  return (await xg(e, t, n)).content;
}
async function Af(e, t, n) {
  const { request: r, content: i, unpacked: o, recordContext: s, hd: a } = await xg(e, t, n);
  return a ? A_({ request: r, sealed: o.sealed, vmk: t, context: s, identity: a, payload: i.payload }) : i.payload.kind === "message" ? Mx(o.sealed, t, s, i.payload.messageHex) : zx(o.sealed, t, s, i.payload.digest, r.scheme);
}
function kg(e, t = e.label) {
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
function U_(e) {
  const { challenge: t, ...n } = e;
  return {
    ...n,
    ...t ? { challenge: ce(t) } : {}
  };
}
function Oc(e, t) {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be an object`);
  return e;
}
function Qn(e, t) {
  if (typeof e == "string" && e.length > 0) return e;
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be a text spec`);
  const n = e;
  if (typeof n.text == "string") return xe(n.text);
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
function N_(e) {
  const t = Oc(e, "details");
  if (!Array.isArray(t.rows)) throw new S("invalid_payload", "details must contain rows");
  const n = [];
  for (const [r, i] of t.rows.entries()) {
    const o = Oc(i, `details.rows[${r}]`), s = o.variant;
    if (s !== void 0 && s !== "text" && s !== "command" && s !== "mono" && s !== "chips")
      throw new S("invalid_payload", `details.rows[${r}].variant is invalid`);
    const a = Qn(o.label, `details.rows[${r}].label`);
    if (s === "chips") {
      if (!Array.isArray(o.values)) throw new S("invalid_payload", `details.rows[${r}].values must be an array`);
      const c = o.tone;
      if (c !== void 0 && c !== "default" && c !== "warning")
        throw new S("invalid_payload", `details.rows[${r}].tone is invalid`);
      const l = c === "default" || c === "warning" ? c : void 0;
      n.push({
        label: a,
        values: o.values.map((u, d) => Qn(u, `details.rows[${r}].values[${d}]`)),
        variant: "chips",
        ...l !== void 0 ? { tone: l } : {}
      });
      continue;
    }
    n.push({
      label: a,
      value: Qn(o.value, `details.rows[${r}].value`),
      ...s !== void 0 ? { variant: s } : {}
    });
  }
  return { rows: n };
}
function D_(e) {
  try {
    return Dy(e);
  } catch (t) {
    throw new S("invalid_payload", t instanceof Error ? t.message : "signing review is malformed");
  }
}
function z_(e) {
  const t = Oc(e, "authorization prompt");
  if (t.challenge !== void 0 && (typeof t.challenge != "string" || t.challenge.length === 0))
    throw new S("invalid_payload", "prompt.challenge must be a non-empty string");
  if (t.reviewSession !== void 0 && typeof t.reviewSession != "string")
    throw new S("invalid_payload", "prompt.reviewSession must be a string");
  const n = t.challenge === void 0 ? void 0 : Ae(t.challenge);
  return {
    title: Qn(t.title, "prompt.title"),
    subtitle: Qn(t.subtitle, "prompt.subtitle"),
    ...t.body !== void 0 ? { body: Qn(t.body, "prompt.body") } : {},
    ...t.details !== void 0 ? { details: N_(t.details) } : {},
    ...t.review !== void 0 ? { review: D_(t.review) } : {},
    ...t.reviewSession !== void 0 ? { reviewSession: t.reviewSession } : {},
    ...n ? { challenge: n } : {},
    label: Qn(t.label, "prompt.label")
  };
}
const Sf = 320, _f = 220, M_ = 6e4, K_ = 1e3;
function F_(e, t = {}) {
  if (e.uiShowPending) return { ok: !1, code: "sandbox_not_visible", detail: "ui show is still pending" };
  if (!e.documentVisible) return { ok: !1, code: "sandbox_not_visible", detail: "document is not visible" };
  if ((t.requireFocus ?? !0) && !e.documentFocused)
    return { ok: !1, code: "sandbox_not_visible", detail: "document is not focused" };
  if (e.frameWidth < Sf || e.frameHeight < _f)
    return { ok: !1, code: "sandbox_not_visible", detail: "sandbox frame is too small" };
  if (!e.visibleByIntersectionObserver || e.intersectionRatio < 0.99)
    return { ok: !1, code: "sandbox_not_visible", detail: "sandbox frame is not fully visible" };
  const n = [];
  if (e.embedded) {
    const r = e.parent;
    r ? (r.ageMs > M_ && n.push("parent frame visibility is stale"), (r.frameWidth < Sf || r.frameHeight < _f) && n.push("parent frame is too small"), (!r.visibleByIntersectionObserver || r.intersectionRatio < 0.99) && n.push("parent frame is not fully visible"), (r.opacity < 0.99 || !r.pointerEvents) && n.push("parent frame is visually occluded")) : n.push("parent frame visibility is not attested");
  }
  return n.length > 0 ? { ok: !0, warnings: n } : { ok: !0 };
}
function If(e) {
  return typeof e == "object" && e !== null ? e : null;
}
function Kn(e) {
  if (typeof e == "number") return Number.isFinite(e) ? e : void 0;
  if (typeof e != "string" || e.trim() === "") return;
  const t = Number(e);
  return Number.isFinite(t) ? t : void 0;
}
function _r(e) {
  const t = Kn(e);
  if (t !== void 0) return t;
  if (typeof e != "string" || e.trim() === "") return;
  const n = Date.parse(e);
  return Number.isFinite(n) ? n : void 0;
}
function Wa(e) {
  return typeof e == "boolean" ? e : void 0;
}
function j_(e) {
  if (typeof e == "boolean") return e;
  if (typeof e == "string")
    return e.toLowerCase() !== "none";
}
function V_(e) {
  if (!e) return;
  const t = If(e.value);
  if (!t) return;
  const n = If(t.frame) ?? t, r = Kn(n.frameWidth) ?? Kn(n.width), i = Kn(n.frameHeight) ?? Kn(n.height), o = Kn(n.intersectionRatio), s = Wa(n.visibleByIntersectionObserver) ?? Wa(n.isVisible) ?? Wa(n.visible), a = Kn(n.opacity), c = j_(n.pointerEvents), l = _r(t.sampledAt) ?? _r(t.measuredAt) ?? _r(t.timestamp) ?? _r(n.sampledAt) ?? _r(n.measuredAt) ?? _r(n.timestamp);
  if (r === void 0 || i === void 0 || o === void 0 || s === void 0 || a === void 0 || c === void 0 || l === void 0)
    return;
  const u = Date.now();
  if (!(l > u + K_))
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
function Eg(e) {
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
    parent: V_(e.parentSurface?.())
  }), c = (y) => {
    const w = a(), p = F_(w, { requireFocus: y });
    if (!p.ok) throw new S(p.code, p.detail, !0);
    if (e.requireParentSurface && w.embedded && (p.warnings?.length ?? 0) > 0)
      throw new S("sandbox_not_visible", p.warnings[0], !0);
    for (const g of p.warnings ?? []) console.warn(`[vault-sandbox] Confirm surface advisory: ${g}`);
  }, l = () => c(!0), u = new Promise((y, w) => {
    o = y, s = w;
  }), d = () => {
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
function Ag(e) {
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
async function q_(e) {
  let t = new TextEncoder().encode(e.value);
  try {
    const n = { name: e.name, kind: "static" }, r = await Al(t, e.vmk, n);
    return { envelope: Sl(r, e.wrapMeta, n) };
  } finally {
    t?.fill(0), t = void 0;
  }
}
async function G_(e) {
  const t = Yr(e.curve), n = Dx(t);
  let r = n.privateKey;
  try {
    const i = {
      name: e.name,
      kind: "keypair",
      publicKey: n.publicKey,
      ...t === "ed25519" ? { curve: t } : {}
    }, o = await Al(r, e.vmk, i);
    return {
      envelope: Sl(o, e.wrapMeta, i),
      publicKey: n.publicKey,
      addresses: t === "ed25519" ? { solana: Kx(n.publicKey) } : Ws(n.publicKey)
    };
  } finally {
    r?.fill(0), r = void 0;
  }
}
function dd(e) {
  return (e?.kind ?? "static") === "static";
}
function W_(e, t) {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", "approveRelease item must be an object");
  const n = e;
  return {
    material: t(n.material),
    context: Kl(n.context)
  };
}
function Sg(e) {
  const t = e[0]?.context.display;
  if (!t) throw new S("invalid_payload", "approveRelease requires at least one item");
  if (e[0].context.purpose !== "agent-deliver") throw new S("invalid_context", "approveRelease only accepts agent-deliver contexts");
  const n = Mu(e[0].context), r = Bf(e[0].context);
  for (const s of e) {
    if (s.context.purpose !== "agent-deliver") throw new S("invalid_context", "approveRelease only accepts agent-deliver contexts");
    if (Mu(s.context) !== n)
      throw new S("invalid_context", "approveRelease batch contexts must share one signed display block");
    if (Bf(s.context) !== r)
      throw new S("invalid_context", "approveRelease batch contexts must share one signed recipient");
  }
  const i = new Set(t.secrets.map((s) => `${s.name}:${s.kind}`));
  for (const s of e) {
    const { recordMetadata: a } = Un(s.material.envelope), c = a?.kind ?? "static";
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
function Bf(e) {
  if (!e.grantId) throw new S("invalid_context", "agent delivery context is missing grantId");
  if (!e.agent) throw new S("invalid_context", "agent delivery context is missing agent key");
  return pe({ agent: e.agent, grantId: e.grantId });
}
async function Z_(e) {
  if (e.items.length === 0) throw new S("invalid_payload", "approveRelease requires at least one item");
  const t = Sg(e.items), n = await hs(e.wrapMeta, e.vmk), r = e.items.map((a) => a.context);
  for (const a of e.items)
    Jn({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: e.now
    });
  Ci(r, e.now);
  const i = await hk(r);
  await e.confirm({
    ...t,
    challenge: i
  });
  const o = e.now ?? Date.now();
  for (const a of e.items)
    Jn({
      context: a.context,
      rootMetadata: n,
      expectedPurpose: "agent-deliver",
      now: o
    });
  e.onApprovalAccepted?.(o), Ci(r, o);
  const s = [];
  for (const a of e.items) {
    const { sealed: c, recordMetadata: l } = Un(a.material.envelope);
    if (!dd(l)) throw new S("invalid_payload", "approveRelease requires static protected records");
    s.push(
      await Nx(
        c,
        e.vmk,
        fk(a.context),
        Gs(a.material.name, l),
        await uk(a.context, a.material.name)
      )
    );
  }
  return e.consumeReplayIds !== !1 && await Ri(r, o), { blindBoxes: s };
}
const $f = "vault-signing-policy-downgrade", Y_ = `avibe:vault:signing-policy-downgrade:v1
`, Tr = 65536, X_ = /^[A-Za-z0-9_-]{1,80}$/, Tf = ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"];
function ne(e) {
  throw new S("policy_context_invalid", e);
}
function In(e, t, n) {
  (e === null || typeof e != "object" || Array.isArray(e) || Object.getPrototypeOf(e) !== Object.prototype && Object.getPrototypeOf(e) !== null) && ne(`${n} must be an object`);
  const r = e;
  return (Object.keys(r).length !== t.length || !t.every((i) => Object.hasOwn(r, i))) && ne(`${n} has missing or unknown fields`), r;
}
function Cc(e, t) {
  return (typeof e != "string" || e.length === 0) && ne(`${t} must be a nonempty string`), e;
}
function Za(e, t) {
  return (typeof e != "string" || !X_.test(e)) && ne(`${t} is not a valid ID`), e;
}
function Bn(e, t, n) {
  (typeof e != "string" || e.length > 4 * Math.ceil(n / 3) || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(e)) && ne("noncanonical policy base64");
  const r = Ae(e);
  return (r.length < t || r.length > n || ce(r) !== e) && ne("policy base64 length or padding is invalid"), r;
}
const J_ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function Q_(e) {
  let t = 0n;
  for (const i of e) {
    const o = J_.indexOf(i);
    o < 0 && ne("invalid Base58 address"), t = t * 58n + BigInt(o);
  }
  const n = [];
  for (; t > 0n; )
    n.push(Number(t & 255n)), t >>= 8n;
  const r = /^1*/.exec(e)[0].length;
  return Uint8Array.from([...new Array(r).fill(0), ...n.reverse()]);
}
const Pf = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function Of(e, t, n) {
  (!e.startsWith(`bc1${Pf[t]}`) || e.length !== (n === 20 ? 42 : 62)) && ne("invalid mainnet witness address");
  const r = [...e.slice(4, -6)].map((c) => {
    const l = Pf.indexOf(c);
    return l < 0 && ne("invalid Bech32 address character"), l;
  });
  let i = 0, o = 0;
  const s = [];
  for (const c of r)
    i = (i << 5 | c) & 65535, o += 5, o >= 8 && (o -= 8, s.push(i >>> o & 255));
  (o >= 5 || (i & (1 << o) - 1) !== 0 || s.length !== n) && ne("noncanonical witness address padding");
  const a = Uint8Array.from(s);
  return wc("bc", t, a) !== e && ne("invalid witness address checksum"), a;
}
function e6(e, t) {
  if (e === null) return null;
  const n = Cc(e, `addresses.${t}`);
  if (n.length > 128 && ne("policy address is too long"), t === "eth")
    (!/^0x[0-9a-fA-F]{40}$/.test(n) || `0x${gy(n.slice(2))}` !== n) && ne("noncanonical EIP-55 address");
  else if (t === "btc_segwit") Of(n, 0, 20);
  else if (t === "btc_taproot") Of(n, 1, 32);
  else {
    const r = Q_(n);
    t === "solana" ? (r.length !== 32 || wi(r) !== n) && ne("noncanonical Solana address") : (r.length !== 25 || r[0] !== 0 || yy(r.slice(0, 21)) !== n) && ne("invalid mainnet P2PKH address");
  }
  return n;
}
function t6(e) {
  return Y_ + pe(e);
}
function _g(e) {
  return le(new TextEncoder().encode(t6(e)));
}
function Rn(e, t = Date.now()) {
  const n = Date.parse(e.expiresAt) - t;
  if (Number.isFinite(n) || ne("invalid policy expiry"), n <= 0) throw new S("policy_context_expired", "policy context has expired", !0);
  if (n > 15e4) throw new S("policy_context_clock_skew", "check this device's clock and retry", !0);
  return n;
}
function Ig(e, t, n = Date.now()) {
  try {
    const r = JSON.stringify(e);
    (r === void 0 || r.length > Tr) && ne("policy context exceeds its size bound"), Xr(e);
    const i = In(e, ["v", "purpose", "challengeId", "nonce", "expiresAt", "rpId", "origin", "target", "change", "factors"], "context");
    (i.v !== 1 || i.purpose !== $f) && ne("unsupported policy context version or purpose"), (i.origin !== t.origin || i.rpId !== t.hostname) && ne("policy origin or RP does not match this sandbox");
    const o = Cc(i.expiresAt, "expiresAt"), s = Date.parse(o);
    (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(o) || !Number.isFinite(s) || new Date(s).toISOString() !== o) && ne("policy expiry must be an exact UTC millisecond date"), Bn(i.nonce, 32, 32);
    const a = In(i.target, ["secretId", "name", "protection", "signerKind", "account", "accountName", "addresses", "identitySha256"], "target");
    a.protection !== "standard" && a.protection !== "protected" && ne("invalid protection tier"), a.signerKind !== "local" && a.signerKind !== "hd-seed" && ne("unsupported signer kind"), (typeof a.identitySha256 != "string" || !/^[0-9a-f]{64}$/.test(a.identitySha256)) && ne("invalid identity digest");
    const c = In(a.addresses, Tf, "addresses"), l = Object.fromEntries(Tf.map((w) => [w, e6(c[w], w)]));
    if (a.signerKind === "hd-seed")
      (a.protection !== "standard" || typeof a.account != "number" || !Number.isSafeInteger(a.account) || a.account < 0 || a.account > 19) && ne("invalid shared HD account"), a.accountName !== null && (typeof a.accountName != "string" || [...a.accountName].length < 1 || [...a.accountName].length > 80) && ne("invalid HD account name"), (l.eth === null || l.btc_taproot === null || l.solana === null || l.btc_legacy !== null || l.btc_segwit !== null) && ne("invalid shared HD address set");
    else {
      (a.account !== null || a.accountName !== null) && ne("ordinary keys cannot carry an HD account");
      const w = l.eth !== null && l.btc_legacy !== null && l.btc_segwit !== null && l.btc_taproot !== null && l.solana === null, p = l.solana !== null && l.eth === null && l.btc_legacy === null && l.btc_segwit === null && l.btc_taproot === null;
      !w && !p && ne("invalid ordinary key address set");
    }
    const u = In(i.change, ["field", "from", "to", "revision"], "change");
    (u.field !== "always_ask" || u.from !== !0 || u.to !== !1 || typeof u.revision != "string" || !/^(?:legacy|[0-9a-f]{32})$/.test(u.revision)) && ne("only true-to-false always_ask changes are supported"), (!Array.isArray(i.factors) || i.factors.length === 0) && ne("policy factors must be nonempty");
    const d = /* @__PURE__ */ new Set();
    let f = "";
    const h = i.factors.map((w) => {
      const p = In(w, ["factorId", "credentialId"], "factor"), g = Za(p.factorId, "factorId");
      Bn(p.credentialId, 1, 1023);
      const _ = p.credentialId;
      return (g <= f || d.has(_)) && ne("policy factors must have unique credentials and sorted unique IDs"), f = g, d.add(_), { factorId: g, credentialId: _ };
    }), y = {
      v: 1,
      purpose: $f,
      challengeId: Za(i.challengeId, "challengeId"),
      nonce: i.nonce,
      expiresAt: o,
      rpId: i.rpId,
      origin: i.origin,
      target: {
        secretId: Za(a.secretId, "secretId"),
        name: Cc(a.name, "name"),
        protection: a.protection,
        signerKind: a.signerKind,
        account: a.account,
        accountName: a.accountName,
        addresses: l,
        identitySha256: a.identitySha256
      },
      change: { field: "always_ask", from: !0, to: !1, revision: u.revision },
      factors: h
    };
    return new TextEncoder().encode(pe(y)).length > Tr && ne("policy context exceeds its UTF-8 size bound"), Rn(y, n), y;
  } catch (r) {
    if (r instanceof S) throw r;
    ne("policy context is not canonical JSON");
  }
}
function n6(e, t) {
  return Ig(In(e, ["context"], "payload").context, t);
}
function r6(e) {
  const t = Rn(e), n = Math.min(12e4, Math.floor(t));
  if (n <= 0) throw new S("policy_context_expired", "policy context has expired", !0);
  return {
    challenge: new Uint8Array(_g(e)).buffer,
    rpId: e.rpId,
    userVerification: "required",
    timeout: n,
    allowCredentials: e.factors.map((r) => ({ type: "public-key", id: new Uint8Array(Bn(r.credentialId, 1, 1023)).buffer }))
  };
}
function Cf(e) {
  return ce(e).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function Rc(e, t) {
  try {
    const n = In(e, ["id", "rawId", "type", "response"], "assertion"), r = Bn(n.rawId, 1, 1023);
    (n.type !== "public-key" || n.id !== Cf(r) || !t.factors.some((l) => l.credentialId === n.rawId)) && ne("assertion does not identify a frozen factor");
    const i = In(n.response, ["clientDataJSON", "authenticatorData", "signature", "userHandle"], "assertion.response"), o = Bn(i.clientDataJSON, 1, Tr), s = JSON.parse(new TextDecoder("utf-8", { fatal: !0 }).decode(o));
    (s.type !== "webauthn.get" || s.challenge !== Cf(_g(t)) || s.origin !== t.origin || s.crossOrigin !== void 0 && s.crossOrigin !== !1) && ne("assertion client data does not bind this policy context");
    const a = Bn(i.authenticatorData, 37, Tr), c = le(new TextEncoder().encode(t.rpId));
    return ((a[32] & 5) !== 5 || !a.slice(0, 32).every((l, u) => l === c[u])) && ne("assertion RP or UP/UV flags are invalid"), Bn(i.signature, 1, Tr), i.userHandle !== null && Bn(i.userHandle, 0, Tr), Rn(t), { id: n.id, rawId: n.rawId, type: "public-key", response: { ...i } };
  } catch (n) {
    throw n instanceof S && n.code !== "policy_context_invalid" ? n : new S("authorization_result_invalid", "invalid policy assertion");
  }
}
async function i6(e, t) {
  if (t.aborted) throw t.reason;
  const n = await navigator.credentials.get({ publicKey: r6(e), signal: t });
  if (t.aborted) throw t.reason;
  if (!n) throw new Error("passkey-cancelled");
  return Rc(Oy(n), e);
}
function Ya(e) {
  if (!e || typeof e != "object" || Array.isArray(e)) throw new S("invalid_payload", "reveal material must be an object");
  return e;
}
function Bo(e) {
  if (typeof e != "string" || e.length === 0) throw new S("invalid_payload", "reveal material requires nonempty strings");
  return e;
}
function Bg(e) {
  return e && typeof e == "object" && (Object.values(e).forEach(Bg), Object.freeze(e)), e;
}
function o6(e) {
  const t = { ciphertext: e.ciphertext, nonce: e.nonce, wrap_meta: e.wrap_meta };
  return Xr(t), te(le(new TextEncoder().encode(pe(t))));
}
function Do(e) {
  _s({ context: e.context, expectedPurpose: "reveal", secret: { name: e.material.name, kind: "static" } });
  const t = e.context.release, n = t?.envelopeHash;
  if (!t || t.name !== e.material.name || !n || typeof n != "object" || Array.isArray(n))
    throw new S("invalid_reveal_binding", "reveal approval does not bind this envelope");
  const r = n;
  if (Object.keys(r).length !== 2 || r.alg !== "sha256" || typeof r.digest != "string" || !/^[0-9a-f]{64}$/.test(r.digest) || r.digest !== o6(e.material.envelope))
    throw new S("invalid_reveal_binding", "reveal approval does not bind this envelope");
}
function s6(e) {
  Xr(e);
  const t = Ya(structuredClone(e)), n = Ya(t.material), r = Ya(n.envelope), i = Bg({
    material: { name: Bo(n.name), envelope: {
      ciphertext: Bo(r.ciphertext),
      nonce: Bo(r.nonce),
      wrap_meta: Bo(r.wrap_meta)
    } },
    context: Kl(t.context)
  });
  return Do(i), i;
}
function $g(e) {
  const t = cs(e);
  if (t.v !== 1 || !Array.isArray(t.copies) || t.copies.length === 0 || ["record_meta", "dek_nonce", "wrapped_dek"].some((n) => Object.hasOwn(t, n)) || t.scheme !== void 0 && t.scheme !== "machine-aesgcm-v1") throw new Error("protected HD requires an existing base/root wrap");
  for (const n of t.copies) {
    const r = ht(n);
    if (r.kind !== "passkey" || r.kdf !== "webauthn-prf-hkdf-sha256" || r.credential_id !== void 0 && (typeof r.credential_id != "string" || r.credential_id.length === 0)) throw new Error("invalid protected HD passkey copy");
    Yn(r.prf_salt, [32]), Yn(r.nonce, [12]), Yn(r.wrapped, [48]);
  }
  return e;
}
function Rf(e) {
  const t = cs(e);
  for (const n of ["scheme", "record_meta", "dek_nonce", "wrapped_dek"]) delete t[n];
  return pe(t);
}
function Tg(e, t, n) {
  const r = ht(e);
  Ot(r, ["envelope", "accounts", "expectedAccount0"]);
  const i = ht(r.envelope);
  if (Ot(i, ["ciphertext", "nonce", "wrap_meta"]), typeof i.ciphertext != "string" || typeof i.nonce != "string" || typeof i.wrap_meta != "string") throw new Error("invalid protected HD envelope");
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(t)) throw new Error("invalid protected HD name");
  const o = i, s = Un(o);
  if (s.recordMetadata?.kind !== "hd-seed" || Rf(o.wrap_meta) !== Rf(n)) throw new Error("protected HD result root mismatch");
  const a = Ms({ kind: "hd-seed", profile: os, accounts: r.accounts });
  if (pe(a) !== pe(s.recordMetadata)) throw new Error("protected HD result table mismatch");
  const c = ht(r.expectedAccount0);
  Ot(c, ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"]);
  const l = gl(a.accounts[0]);
  if (pe(c) !== pe(l)) throw new Error("protected HD result account0 mismatch");
  return { envelope: { ...o }, accounts: a.accounts, expectedAccount0: l };
}
async function a6(e) {
  e.assertCurrent(), $g(e.wrapMeta);
  const t = ty(e.words, e.passphrase);
  let n, r, i;
  try {
    const o = yc(t.seed);
    e.assertCurrent();
    const s = { kind: "hd-seed", profile: os, accounts: o }, a = Gs(e.name, s);
    n = X1(t);
    const c = await Al(n, e.vmk, a);
    e.assertCurrent();
    const u = { envelope: Sl(c, e.wrapMeta, a), accounts: o, expectedAccount0: gl(o[0]) };
    r = await ji(c, e.vmk, a), e.assertCurrent(), i = Qp(r);
    const d = (h, y) => h.length === y.length && h.every((w, p) => w === y[p]);
    if (!d(i.entropy, t.entropy) || !d(i.seed, t.seed) || i.passphraseRequired !== t.passphraseRequired || pe(yc(i.seed)) !== pe(o)) throw new Error("protected HD creation reopen mismatch");
    const f = Tg(u, e.name, e.wrapMeta);
    return e.assertCurrent(), f;
  } finally {
    n?.fill(0), r?.fill(0), as(i), as(t);
  }
}
const Pg = 3e5;
function Og(e) {
  try {
    const t = ht(e);
    if (Ot(t, ["name", "mode", "wrapMeta"]), typeof t.name != "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(t.name) || t.mode !== "create" && t.mode !== "import") throw new Error("invalid protected HD name/mode");
    return { name: t.name, mode: t.mode, wrapMeta: $g(t.wrapMeta) };
  } catch {
    throw new S("invalid_payload", "sealHdWallet requires only name, create/import mode and an existing base root");
  }
}
function ar(e) {
  const t = e.deadlineAt - Date.now();
  if (!Number.isSafeInteger(e.deadlineAt) || t <= 0 || t > Pg) throw new S("authorization_window_timeout", "protected HD window expired", !0);
  return t;
}
function c6(e) {
  const t = ht(e);
  if (Ot(t, ["kind", "name", "mode", "wrapMeta", "deadlineAt"]), t.kind !== "protected-hd-wallet" || typeof t.deadlineAt != "number") throw new S("invalid_payload", "invalid protected HD window request");
  const r = { ...Og({ name: t.name, mode: t.mode, wrapMeta: t.wrapMeta }), kind: "protected-hd-wallet", deadlineAt: t.deadlineAt };
  return ar(r), Object.freeze(r);
}
function Ps(e, t) {
  try {
    const n = ht(e);
    if (Ot(n, ["kind", "envelope", "accounts", "expectedAccount0"]), n.kind !== "sealed-hd-wallet") throw new Error("invalid result kind");
    return ar(t), { kind: "sealed-hd-wallet", ...Tg({ envelope: n.envelope, accounts: n.accounts, expectedAccount0: n.expectedAccount0 }, t.name, t.wrapMeta) };
  } catch (n) {
    throw n instanceof S ? n : new S("authorization_result_invalid", "protected HD result is invalid");
  }
}
async function l6(e, t, n) {
  const r = new AbortController(), i = () => t.reason instanceof Error ? t.reason : new S("authorization_cancelled", "protected HD operation cancelled", !0), o = () => r.abort(i());
  t.addEventListener("abort", o, { once: !0 }), t.aborted && o();
  const s = () => {
    if (r.signal.aborted) throw r.signal.reason;
    ar(e);
  };
  let a, c;
  const l = (u) => c ? Promise.reject(new S("authorization_result_invalid", "protected HD wallet is already being sealed")) : (c = (async () => {
    let d, f, h, y = [];
    try {
      s(), y = qs(e.wrapMeta);
      const w = await Js(y, Ut(), r.signal);
      d = w.prfOutput, f = w.prfSalt, s(), h = await oy(e.wrapMeta, { kind: "passkey", prfOutput: d, prfSalt: f }), s();
      const p = await a6({ name: e.name, words: u.words, passphrase: u.passphrase, vmk: h, wrapMeta: e.wrapMeta, assertCurrent: s });
      if (s(), pe(p.accounts) !== pe(u.reviewedAccounts)) throw new S("authorization_result_invalid", "protected HD reviewed account table changed");
      a = Ps({ kind: "sealed-hd-wallet", ...p }, e);
    } finally {
      d?.fill(0), f?.fill(0), h?.fill(0), y.forEach((w) => w.prfSalt.fill(0)), u.words = "", u.passphrase = "", u.reviewedAccounts = [];
    }
  })(), c);
  try {
    if (s(), await n(e, r.signal, l), c && await c, s(), !a) throw new S("authorization_result_invalid", "protected HD wallet was not sealed");
    return Ps(a, e);
  } catch (u) {
    throw r.abort(u), c && await c.catch(() => {
    }), u;
  } finally {
    t.removeEventListener("abort", o);
  }
}
const d6 = [12, 15, 18, 21, 24];
function Hc(e) {
  return e.normalize("NFKD").toLowerCase().trim().split(/\s+/u).filter(Boolean);
}
const u6 = new Set(Zr);
function f6(e, t = 3) {
  for (let n = Math.min(4, e.length); n >= 1; n -= 1) {
    const r = e.slice(0, n), i = Zr.filter((o) => o.startsWith(r));
    if (i.length > 0) return i.slice(0, t);
  }
  return [];
}
function Hf(e) {
  const t = Hc(e);
  if (t.length === 0) return { ok: !1, reason: "empty" };
  const n = t.findIndex((r) => !u6.has(r));
  return n >= 0 ? { ok: !1, reason: "unknown-word", position: n + 1, word: t[n], suggestions: f6(t[n]) } : d6.includes(t.length) ? G1(t.join(" "), Zr) ? { ok: !0, words: t } : { ok: !1, reason: "checksum" } : { ok: !1, reason: "count", count: t.length };
}
function h6(e, t = Math.random) {
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
    for (; u.length < 2; ) u.push(s(a) ?? s(Zr));
    const d = [l, ...u];
    for (let f = d.length - 1; f > 0; f -= 1) {
      const h = Math.floor(t() * (f + 1));
      [d[f], d[h]] = [d[h], d[f]];
    }
    return { position: c, options: d, answer: l };
  });
}
const Lf = (e, t) => e.every((n) => t[n.position] === n.answer);
async function p6(e, t) {
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
function y6() {
  const e = new Uint32Array(1);
  return crypto.getRandomValues(e), e[0] / 2 ** 32;
}
function w6(e, t = y6) {
  return h6(e, t);
}
function G(e, t, n) {
  const r = document.createElement(e);
  return r.className = `${t} dynamic`, n !== void 0 && Y(r, n), r;
}
function Cg(e) {
  const t = document.createElement(e);
  return t.className = e === "textarea" ? "field textarea" : "field", t.setAttribute("autocomplete", "off"), t.setAttribute("autocapitalize", "off"), t.setAttribute("autocorrect", "off"), t.setAttribute("spellcheck", "false"), t instanceof HTMLInputElement && (t.type = "password"), t;
}
const Os = "http://www.w3.org/2000/svg", g6 = ["M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0"], m6 = [
  "M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49",
  "M14.084 14.158a3 3 0 0 1-4.242-4.242",
  "M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143",
  "m2 2 20 20"
];
function b6(e) {
  const t = document.createElementNS(Os, "svg"), n = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true"
  };
  for (const [r, i] of Object.entries(n)) t.setAttribute(r, i);
  for (const r of e ? m6 : g6) {
    const i = document.createElementNS(Os, "path");
    i.setAttribute("d", r), t.append(i);
  }
  return t;
}
function v6(e) {
  const t = document.createElementNS(Os, "svg");
  for (const [r, i] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" })) t.setAttribute(r, i);
  const n = document.createElementNS(Os, "path");
  return n.setAttribute("d", e ? "m6 9 6 6 6-6" : "m9 18 6-6-6-6"), t.append(n), t;
}
let x6 = 0;
function Xa(e) {
  const t = G("div", "hd-field"), n = `hd-passphrase-${++x6}`, r = G("label", "hd-field-label", e);
  r.htmlFor = n;
  const i = G("div", "hd-secret-row"), o = Cg("input");
  o.id = n;
  const s = document.createElement("button");
  s.type = "button", s.className = "hd-reveal", s.setAttribute("aria-controls", n);
  const a = G("span", "hd-reveal-label"), c = (l) => {
    o.type = l ? "text" : "password", s.setAttribute("aria-pressed", String(l)), Y(a, l ? "hd.hideValue" : "hd.showValue"), s.textContent = "", s.append(b6(l), a);
  };
  return s.addEventListener("click", () => c(o.type === "password")), c(!1), i.append(o, s), t.append(r, i), { wrap: t, input: o, toggle: s, hide: () => c(!1) };
}
function Ir(e, ...t) {
  const n = G("div", `hd-notice hd-notice-${e}`);
  return n.setAttribute("role", e === "error" ? "alert" : "note"), t.forEach((r, i) => n.append(G("span", i === 0 && t.length > 1 ? "hd-notice-lead" : "hd-notice-text", r))), n;
}
function Ja(e) {
  const t = G("ol", "hd-stepper");
  return Rg(t, "hd.stepsLabel"), ["hd.stepSetup", "hd.stepBackup", "hd.stepCheck"].forEach((n, r) => {
    const i = r + 1, o = document.createElement("li");
    o.className = i < e ? "done" : i === e ? "current" : "", i === e && o.setAttribute("aria-current", "step");
    const s = document.createElement("span");
    s.className = "hd-step-dot", s.textContent = i < e ? "✓" : String(i);
    const a = document.createElement("span");
    a.className = "hd-step-name", Y(a, n), o.append(s, a), t.append(o);
  }), t;
}
function Rg(e, t) {
  const n = document.createElement("span");
  Y(n, t), e.setAttribute("aria-label", n.textContent ?? "");
}
function Uf(e, t, n) {
  const r = G("div", "hd-fold"), i = document.createElement("button");
  i.type = "button", i.className = "hd-fold-header";
  const o = G("span", "hd-fold-chevron");
  o.setAttribute("aria-hidden", "true");
  const s = G("span", "hd-fold-title", e), a = G("span", "hd-fold-hint");
  i.append(o, s, a);
  const c = G("div", "hd-fold-body"), l = () => {
    i.setAttribute("aria-expanded", String(n)), o.textContent = "", o.append(v6(n)), c.hidden = !n, a.hidden = n, Y(a, n ? xe("") : t());
  };
  return i.addEventListener("click", () => {
    n = !n, l();
  }), l(), r.append(i, c), { wrap: r, body: c, refresh: l };
}
function Nf(e, t = {}) {
  const n = G("ol", "hd-words");
  return Rg(n, "hd.stepBackup"), t.hidden && (n.classList.add("hd-words-hidden"), n.setAttribute("aria-hidden", "true")), e.forEach((r, i) => {
    const o = document.createElement("li");
    t.bad === i && (o.className = "bad");
    const s = document.createElement("span");
    s.className = "hd-word-index", s.textContent = String(i + 1);
    const a = document.createElement("span");
    t.hidden ? Y(a, "hd.hiddenWord") : a.textContent = r, o.append(s, a), n.append(o);
  }), n;
}
function k6(e) {
  const t = gl(e), n = G("div", "hd-addresses"), r = [
    ["hd.reviewEvm", t.eth],
    ["hd.reviewBitcoin", t.btc_taproot],
    ["hd.reviewSolana", t.solana]
  ];
  for (const [i, o] of r) {
    const s = document.createElement("div");
    s.className = "hd-address";
    const a = document.createElement("span");
    a.className = "hd-address-label", Y(a, i);
    const c = document.createElement("span");
    c.className = "hd-address-value", c.textContent = o, s.append(a, c), n.append(s);
  }
  return n;
}
function Df(e, t) {
  const n = ty(e, t);
  try {
    return yc(n.seed);
  } finally {
    as(n);
  }
}
const E6 = 250, A6 = (e, t, n) => new Promise((r, i) => {
  const o = [];
  let s = [], a = "", c = "", l = "", u = [], d = {}, f = [], h = !1, y = !1, w = 0;
  const p = () => {
    for (const E of o.splice(0)) E();
  }, g = (E, I) => (p(), w += 1, Qs(E, I)), _ = (E) => {
    if (!h) {
      h = !0, t.removeEventListener("abort", k), p(), s = [], a = "", c = "", l = "";
      for (const I of u)
        I.answer = "", I.options.fill("");
      u = [], d = {}, f = [], ea(), E();
    }
  }, k = () => _(() => i(t.reason instanceof Error ? t.reason : new Error("operation-cancelled")));
  if (t.aborted) {
    k();
    return;
  }
  t.addEventListener("abort", k, { once: !0 });
  const C = "hd.title", B = xe(e.name), R = (E, I, T = !1, O = !1) => {
    const U = w;
    E.addEventListener("click", () => {
      if (!(h || U !== w || y && !O))
        try {
          I();
        } catch (K) {
          _(() => i(K));
        }
    }, { once: T });
  }, L = (E, I) => {
    const T = w;
    E.addEventListener("input", () => {
      if (!(h || T !== w || y))
        try {
          I();
        } catch (O) {
          _(() => i(O));
        }
    });
  }, H = (E) => {
    const I = Tt(E);
    return I.classList.add("secondary"), I;
  }, $ = () => {
    const E = H("common.cancel");
    return R(E, () => _(() => i(new Error("operation-cancelled"))), !0, !0), E;
  }, D = (E) => {
    o.push(() => {
      E.value = "";
    });
  }, z = (E) => {
    o.push(() => {
      E.textContent = "";
    });
  }, x = (E, I, T, O) => {
    if (y) return;
    y = !0;
    for (const K of ["input", "textarea", "button"])
      E.querySelectorAll(K).forEach((N) => {
        N !== I && (N.disabled = !0);
      });
    Y(T, "hd.working"), n(O).then(() => _(r), (K) => _(() => i(K)));
  }, v = () => {
    const E = () => !!(a || c), I = Uf("hd.passphraseTitle", () => E() ? "hd.passphraseSet" : "hd.passphraseFolded", E()), T = Xa("hd.passphraseLabel"), O = Xa("hd.passphraseAgain");
    T.input.value = a, O.input.value = c;
    for (const N of [T, O])
      D(N.input), o.push(N.hide);
    const U = G("p", "hd-hint"), K = () => {
      const N = E() && a !== c;
      U.className = `hd-hint dynamic${N ? " hd-error" : E() ? " hd-ok" : ""}`, U.setAttribute("role", N ? "alert" : "status"), O.input.setAttribute("aria-invalid", String(N)), Y(U, N ? "hd.passphraseMismatch" : E() ? "hd.passphraseMatch" : "hd.passphraseHint");
    };
    return I.body.append(G("p", "hd-hint", "hd.passphraseBody"), T.wrap, O.wrap, U), { wrap: I.wrap, fields: [T, O], refresh: K };
  }, b = () => {
    const E = g(C, B);
    J(E.card, Ja(1)), J(E.card, G("p", "hd-hint", "hd.generateHint"));
    const I = v(), T = Tt("hd.generateCta"), O = () => {
      a = I.fields[0].input.value, c = I.fields[1].input.value, I.refresh(), T.disabled = a !== c;
    };
    I.fields.forEach((U) => L(U.input, O)), I.refresh(), T.disabled = a !== c, R(T, () => {
      if (a === c) {
        if (s.length === 0) {
          const U = new Uint8Array(16);
          try {
            crypto.getRandomValues(U), s = J1(U).split(" ");
          } finally {
            U.fill(0);
          }
        }
        u.length === 0 && (u = w6(s)), m();
      }
    }), J(E.card, I.wrap), J(E.card, T), J(E.card, $());
  }, m = () => {
    const E = g(C, B);
    J(E.card, Ja(2)), J(E.card, G("p", "hd-step-title", $e("hd.backupTitle", { count: s.length }))), J(E.card, G("p", "hd-hint", "hd.backupBody"));
    const I = G("div", "hd-backup");
    z(I);
    let T = !1;
    const O = (N) => {
      I.textContent = "";
      const M = G("div", "hd-words-frame");
      if (M.append(Nf(s, { hidden: N })), N) {
        const W = G("div", "hd-reveal-overlay"), de = H($e("hd.backupShow", { count: s.length }));
        de.classList.add("hd-inline"), R(de, () => O(!1)), W.append(de, G("span", "hd-reveal-note", "hd.backupScreen")), M.append(W), I.append(M);
        return;
      }
      const F = G("div", "hd-word-tools"), j = H(T ? "hd.backupCopied" : "hd.backupCopy");
      j.classList.add("hd-inline");
      const V = w;
      R(j, () => {
        p6(s.join(" "), () => !h && V === w).then((W) => {
          !W || h || V !== w || (T = !0, Y(j, "hd.backupCopied"));
        }, () => {
        });
      });
      const q = H("hd.backupHide");
      q.classList.add("hd-inline"), R(q, () => O(!0)), F.append(j, q, G("span", "hd-hint", "hd.backupClipboard")), I.append(M, F);
    };
    O(!0), J(E.card, I), a && J(E.card, Ir("warning", "hd.passphraseBackupReminder")), J(E.card, Ir("note", "hd.backupAgentNote"));
    const U = H("hd.back");
    R(U, () => {
      d = {}, b();
    }, !0);
    const K = Tt("hd.backupDone");
    R(K, () => A(), !0), J(E.card, K), J(E.card, U), J(E.card, $());
  }, A = () => {
    const E = g(C, B);
    f = Df(s.join(" "), a), J(E.card, Ja(3)), J(E.card, G("p", "hd-step-title", "hd.checkTitle")), J(E.card, G("p", "hd-hint", "hd.checkBody"));
    const I = G("div", "hd-problem"), T = Tt("hd.createCta");
    T.classList.add("hd-gated");
    const O = () => {
      const M = u.find((F) => d[F.position] && d[F.position] !== F.answer);
      I.textContent = "", M && I.append(Ir("error", $e("hd.checkWrong", { position: M.position }), $e("hd.checkWrongHint", { position: M.position }))), T.disabled = y || !Lf(u, d);
    };
    for (const M of u) {
      const F = G("div", "hd-quiz");
      F.setAttribute("role", "radiogroup");
      const j = G("span", "hd-field-label", $e("hd.checkWord", { position: M.position }));
      j.id = `hd-quiz-${M.position}`, F.setAttribute("aria-labelledby", j.id), F.append(j);
      const V = M.options.map((q) => {
        const W = document.createElement("button");
        return W.type = "button", W.className = "hd-option", W.setAttribute("role", "radio"), W.setAttribute("aria-checked", "false"), W.textContent = q, R(W, () => {
          d = { ...d, [M.position]: q }, V.forEach((de) => {
            const se = de === W;
            de.setAttribute("aria-checked", String(se)), de.classList.remove("selected", "wrong"), se && de.classList.add(q === M.answer ? "selected" : "wrong");
          }), O();
        }), W;
      });
      F.append(...V), z(F), J(E.card, F);
    }
    O();
    const U = As(), K = H("hd.checkAgain"), N = $();
    R(K, () => {
      d = {}, f = [], m();
    }, !0), R(T, () => {
      T.disabled || !Lf(u, d) || x(E.card, N, U, { words: s.join(" "), passphrase: a, reviewedAccounts: f });
    }), J(E.card, I), J(E.card, G("p", "hd-hint", "hd.passkeyNote")), J(E.card, U), J(E.card, T), J(E.card, K), J(E.card, N);
  }, P = () => {
    const E = g(C, B);
    J(E.card, G("p", "hd-hint", "hd.importHint"));
    const I = G("label", "hd-field"), T = G("span", "hd-field-label", "hd.importLabel"), O = Cg("textarea");
    O.rows = 3;
    const U = document.createElement("span");
    Y(U, "hd.importPlaceholder"), O.placeholder = U.textContent ?? "", O.value = l, D(O), I.append(T, O);
    const K = G("div", "hd-import-words"), N = G("div", "hd-import-verdict"), M = G("div", "hd-import-passphrase"), F = G("div", "hd-preview");
    for (const ve of [K, N, F]) z(ve);
    const j = Uf("hd.importPassphrase", () => "hd.importPassphraseFolded", !!a), V = Xa("hd.passphraseLabel");
    V.input.value = a, D(V.input), o.push(V.hide), j.body.append(V.wrap, G("p", "hd-hint", "hd.importPassphraseHint")), M.append(j.wrap);
    const q = As(), W = Tt("hd.importCta"), de = $();
    W.classList.add("hd-gated");
    let se = Hf(l), ye = null, ze, rn = 0;
    o.push(() => {
      ze !== void 0 && clearTimeout(ze), ze = void 0, rn += 1, ye = null;
    });
    const hr = () => se.ok ? { words: ey(se.words.join(" ")), passphrase: a } : null, Zg = () => {
      N.textContent = "";
      const ve = Hc(l);
      if (K.textContent = "", ve.length > 0 ? K.append(Nf(ve, { bad: !se.ok && se.reason === "unknown-word" ? se.position - 1 : void 0 })) : K.append(G("p", "hd-hint", "hd.importLocal")), se.ok)
        N.append(G("p", "hd-hint hd-ok", $e("hd.importOk", { count: se.words.length })));
      else if (se.reason === "unknown-word") {
        const on = Ir(
          "error",
          $e("hd.importUnknown", { position: se.position, word: se.word }),
          ...se.suggestions[0] ? [$e("hd.importDidYouMean", { word: se.suggestions[0] })] : []
        );
        if (se.suggestions.length > 0) {
          const sn = G("div", "hd-suggestions"), ei = se.position;
          for (const wd of se.suggestions) {
            const ha = H($e("hd.importUse", { word: wd }));
            ha.classList.add("hd-inline"), R(ha, () => {
              l = Hc(l).map((Yg, Xg) => Xg === ei - 1 ? wd : Yg).join(" "), O.value = l, fa();
            }), sn.append(ha);
          }
          on.append(sn);
        }
        N.append(on);
      } else se.reason === "checksum" ? N.append(Ir("error", $e("hd.importChecksum", { count: ve.length }), "hd.importChecksumHint")) : se.reason === "count" && N.append(Ir("error", $e("hd.importCount", { count: se.count })));
      M.hidden = !se.ok;
    }, pd = () => {
      F.textContent = "";
      const ve = hr();
      if (W.disabled = y || !ye || !ve || ye.words !== ve.words || ye.passphrase !== ve.passphrase, !ye || W.disabled) return;
      const on = G("div", "hd-preview-box"), sn = G("p", "hd-preview-title");
      sn.append(G("span", "hd-account-index", $e("hd.accountIndex", { account: 0 })), G("span", "", "hd.importPreviewTitle")), on.append(sn, k6(ye.accounts[0]), G("p", "hd-hint", "hd.btcTaprootInfo"), G("p", "hd-hint", "hd.importPreviewHint")), F.append(on);
    }, yd = () => {
      ze !== void 0 && clearTimeout(ze), ze = void 0, ye = null, rn += 1, pd();
      const ve = hr();
      if (!ve) return;
      const on = rn, sn = w;
      ze = setTimeout(() => {
        if (ze = void 0, !(h || sn !== w || on !== rn))
          try {
            const ei = Df(ve.words, ve.passphrase);
            if (h || sn !== w || on !== rn) return;
            ye = { ...ve, accounts: ei }, pd();
          } catch (ei) {
            _(() => i(ei));
          }
      }, E6);
    }, fa = () => {
      se = Hf(l), Zg(), yd();
    };
    L(O, () => {
      l = O.value, fa();
    }), L(V.input, () => {
      a = V.input.value, yd();
    }), R(W, () => {
      const ve = hr();
      W.disabled || !ve || !ye || ye.words !== ve.words || ye.passphrase !== ve.passphrase || x(E.card, de, q, { words: ve.words, passphrase: ve.passphrase, reviewedAccounts: ye.accounts });
    }), fa(), J(E.card, I), J(E.card, K), J(E.card, N), J(E.card, M), J(E.card, F), J(E.card, G("p", "hd-hint", "hd.passkeyNote")), J(E.card, q), J(E.card, W), J(E.card, de);
  };
  try {
    e.mode === "create" ? b() : P();
  } catch (E) {
    _(() => i(E));
  }
}), Hg = `avibe-vault-setup:v1:${he.buildHash}:`, S6 = `avibe-vault-setup-result:${he.buildHash}:`, _6 = `avibe-vault-setup-error:${he.buildHash}:`, I6 = 300 * 1e3, B6 = 300 * 1e3, $6 = 30 * 1e3, T6 = 8 * 1e3, Le = new tm();
let zf = !1;
function P6(e) {
  if (typeof e != "object" || e === null) return !1;
  const t = e;
  return (t.locale === "en" || t.locale === "zh") && (t.theme === "light" || t.theme === "dark");
}
function Lg(e) {
  if (!P6(e))
    throw new S("invalid_payload", "appearance must include locale and theme");
  return e;
}
function O6(e) {
  return e == null ? null : Lg(e);
}
function ca(e) {
  const t = $2(e.locale);
  document.documentElement.setAttribute("data-theme", e.theme), t && C2();
}
function Ug() {
  const e = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  return { locale: T2(), theme: e };
}
function Lt(e) {
  if (typeof e != "object" || e === null)
    throw new S("invalid_payload", "request payload must be an object");
  return e;
}
function Ng(e, t) {
  if (e != null) {
    if (typeof e != "string") throw new S("invalid_payload", `${t} must be a string`);
    return e;
  }
}
function Fe(e, t) {
  if (typeof e != "string" || e.length === 0)
    throw new S("invalid_payload", `${t} must be a non-empty string`);
  return e;
}
function Dg(e, t) {
  if (typeof e != "object" || e === null) throw new S("invalid_payload", `${t} must be an object`);
  return e;
}
function C6(e) {
  const t = Dg(e, "envelope");
  return {
    ciphertext: Fe(t.ciphertext, "envelope.ciphertext"),
    nonce: Fe(t.nonce, "envelope.nonce"),
    wrap_meta: Fe(t.wrap_meta, "envelope.wrap_meta")
  };
}
function R6(e) {
  const t = Dg(e, "material");
  return { name: Fe(t.name, "material.name"), envelope: C6(t.envelope) };
}
function H6(e) {
  const t = e == null ? {} : Lt(e);
  return { wrapMeta: Ng(t.wrapMeta, "wrapMeta") };
}
function zg(e) {
  const t = Lt(e), n = !!t.existingProtectedVault;
  if (n && t.rootMetadata !== void 0)
    throw new S("invalid_payload", "vault root metadata cannot be replaced during setup");
  return {
    vaultUserHandle: Fe(t.vaultUserHandle, "vaultUserHandle"),
    displayName: Fe(t.displayName, "displayName"),
    existingProtectedVault: n,
    authzCreationOptions: t.authzCreationOptions,
    ...t.rootMetadata !== void 0 ? { rootMetadata: t.rootMetadata } : {},
    ...t.policy !== void 0 ? { policy: Pl(t.policy) } : {}
  };
}
function L6(e) {
  const t = Lt(e);
  return {
    wrapMeta: Fe(t.wrapMeta, "wrapMeta"),
    ...t.policy !== void 0 ? { policy: Pl(t.policy) } : {}
  };
}
function U6(e) {
  const t = Lt(e);
  if (!Array.isArray(t.items) || t.items.length === 0)
    throw new S("invalid_payload", "approveRelease requires a non-empty items array");
  return { items: t.items.map((n) => W_(n, R6)) };
}
function N6(e) {
  const t = Lt(e);
  if (t.operation !== "delete_secret") throw new S("invalid_payload", "deleteAuthzAssertion only supports delete_secret");
  return {
    challengeId: Fe(t.challengeId, "challengeId"),
    operation: "delete_secret",
    secretName: Fe(t.secretName, "secretName"),
    webauthn: t.webauthn
  };
}
function Mg() {
  if (typeof crypto.randomUUID == "function") return crypto.randomUUID();
  const e = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(e, (t) => t.toString(16).padStart(2, "0")).join("");
}
async function Mf(e) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(e)));
}
function D6(e) {
  return new Promise((t, n) => {
    const r = () => {
      n(fr(e));
    };
    if (e.aborted) {
      r();
      return;
    }
    e.addEventListener("abort", r, { once: !0 });
  });
}
function z6(e) {
  for (const t of e)
    if (t.aborted) return fr(t);
  return new Error("operation-superseded");
}
function ud(e) {
  const t = new AbortController(), n = [], r = (i) => {
    t.signal.aborted || t.abort(fr(i));
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
async function Kg(e) {
  const t = ud(e.abortSignals);
  try {
    await Promise.race([
      A2(qs(e.wrapMeta), Ut(), e.challenge, t.signal).catch((n) => {
        throw t.signal.aborted ? z6(e.abortSignals) : n;
      }),
      ...e.abortSignals.map((n) => D6(n))
    ]);
  } finally {
    t.cleanup();
  }
}
async function la(e) {
  await lr(async (t) => {
    const n = async (i) => {
      const o = ud([t, ...e.abortSignal ? [e.abortSignal] : []]);
      try {
        const s = await fd(
          {
            prompt: Kf(e),
            wrapMeta: e.wrapMeta,
            passkey: e.passkey
          },
          o.signal,
          i
        );
        if (e.passkey === "unlock") {
          if (s.kind !== "unlock") throw new Error("authorization unlock result is missing");
          await jy({
            wrapMeta: e.wrapMeta,
            currentRpId: Ut(),
            assertion: {
              prfOutput: Ae(s.prfOutput),
              prfSalt: Ae(s.prfSalt)
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
    await Oi(kg(Kf(e), "common.continue"), t, void 0, n);
  });
}
function Kf(e) {
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
function M6(e) {
  return "kind" in e ? e : {
    ...e,
    prompt: U_(e.prompt)
  };
}
function K6(e) {
  const t = Lt(e);
  if (!t) throw new S("invalid_payload", "authorization request must be an object");
  if (t.kind === "protected-hd-wallet") return c6(t);
  if (t.kind === "signing-policy-downgrade") {
    if (Object.keys(t).length !== 2 || !Object.hasOwn(t, "context"))
      throw new S("invalid_payload", "policy authorization request has unknown fields");
    return {
      kind: "signing-policy-downgrade",
      context: Ig(t.context, {
        origin: window.location.origin,
        hostname: window.location.hostname
      })
    };
  }
  if (t.kind !== void 0) throw new S("invalid_payload", "authorization request kind is invalid");
  const n = t.passkey;
  if (n !== "none" && n !== "unlock" && n !== "uv")
    throw new S("invalid_payload", "authorization passkey requirement is invalid");
  return {
    prompt: z_(t.prompt),
    wrapMeta: Fe(t.wrapMeta, "wrapMeta"),
    passkey: n
  };
}
function F6(e) {
  return qf("authorize", e, Ug());
}
function j6(e, t) {
  const n = Lt(e);
  if (!n) throw new S("authorization_result_invalid", "authorization result is invalid");
  if ("kind" in t) {
    if (t.kind === "protected-hd-wallet") return Ps(n, t);
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
      prfOutput: Fe(n.prfOutput, "result.prfOutput"),
      prfSalt: Fe(n.prfSalt, "result.prfSalt")
    };
  throw new S("authorization_result_invalid", "authorization result kind is invalid");
}
function fd(e, t, n) {
  er(t);
  const r = "kind" in e && e.kind === "protected-hd-wallet" ? e : void 0;
  r && ar(r);
  const i = n ?? Mg(), o = `avibe-vault-authorization-${i}`;
  return new Promise((s, a) => {
    let c = null, l = !1, u = null, d = null, f = null, h = null;
    const y = () => k(() => a(fr(t))), w = () => k(() => a(new S("authorization_cancelled", "requesting sandbox was unloaded", !0))), p = () => new S("authorization_window_closed", "authorization window was closed", !0), g = (C) => {
      c = C, d = setInterval(() => {
        !C.closed || f || (f = setTimeout(() => k(() => a(p())), 2500));
      }, 500);
    }, _ = (C) => {
      if (C.origin !== window.location.origin) return;
      const B = C.data;
      if (!(!B || B.id !== i || !Br(B.build)) && (!c && n && B.type === "authorization-ready" && C.source && (h && clearTimeout(h), h = null, g(C.source)), C.source === c))
        if (B.type === "authorization-ready")
          try {
            c?.postMessage(
              {
                type: "authorization-request",
                build: he,
                id: i,
                request: M6(e)
              },
              window.location.origin
            );
          } catch (R) {
            k(() => a(R));
          }
        else if (B.type === "authorization-approved")
          try {
            const R = j6(B.result, e);
            k(() => s(R));
          } catch (R) {
            k(() => a(R));
          }
        else B.type === "authorization-error" && k(() => a(new S(B.code, B.message, !!B.retryable)));
    }, k = (C) => {
      if (!l) {
        l = !0, u && clearTimeout(u), d && clearInterval(d), f && clearTimeout(f), h && clearTimeout(h), window.removeEventListener("message", _), window.removeEventListener("pagehide", w), t?.removeEventListener("abort", y);
        try {
          c && !c.closed && c.close();
        } catch {
        }
        C();
      }
    };
    if (window.addEventListener("message", _), window.addEventListener("pagehide", w, { once: !0 }), n)
      h = setTimeout(() => k(() => a(p())), $6);
    else {
      const C = window.open(F6(i), o, "popup,width=520,height=760");
      if (!C) {
        k(() => a(new S("authorization_popup_blocked", "authorization window was blocked", !0)));
        return;
      }
      C.focus(), g(C);
    }
    if (u = setTimeout(
      () => k(() => a(new S("authorization_window_timeout", "authorization window timed out", !0))),
      r ? ar(r) : B6
    ), t) {
      if (t.aborted) {
        y();
        return;
      }
      t.addEventListener("abort", y, { once: !0 });
    }
  });
}
function V6(e) {
  return e === 0 ? "context.oneTime" : e % 60 === 0 ? $e("context.minutes", { count: e / 60 }) : $e("context.seconds", { count: e });
}
function zi(e, t, n = "access") {
  const r = [];
  e.sessionLabel && r.push({ label: "context.session", value: xe(e.sessionLabel) }), e.command && r.push({ label: "context.command", value: xe(e.command), variant: "command" }), e.egress && r.push({ label: n === "sign" ? "context.signReturns" : "context.egress", value: xe(e.egress) });
  const i = [
    ...e.source?.env?.map((o) => xe(`${X(n === "sign" ? "context.sourceRequestedName" : "context.sourceEnv")}: ${o}`)) ?? [],
    ...e.source?.tags?.map((o) => xe(`${X("context.sourceTag")}: ${o}`)) ?? [],
    ...e.source?.skills?.map((o) => xe(`${X("context.sourceSkill")}: ${o}`)) ?? []
  ];
  return i.length > 0 && r.push({ label: "context.source", values: i, variant: "chips" }), t?.agentFingerprint && r.push({ label: "context.agent", value: xe(t.agentFingerprint), variant: "mono" }), t?.grantId && r.push({ label: "context.grant", value: xe(t.grantId), variant: "mono" }), e.grantTtlSeconds !== void 0 && r.push({ label: "context.agentAccess", value: V6(e.grantTtlSeconds) }), r.push({
    label: "context.secrets",
    values: e.secrets.map(
      (o) => xe(`${o.name} · ${X(o.kind === "static" ? "context.kindStatic" : "context.kindKeypair")}`)
    ),
    variant: "chips",
    tone: "warning"
  }), { rows: r };
}
function q6(e) {
  return e instanceof Error && e.message === "vault-locked";
}
function G6(e) {
  return e instanceof Error && e.message === "vault-operation-aborted";
}
function Fg(e, t) {
  return q6(e) ? !0 : G6(e) && pt(t).state === "locked";
}
async function W6(e, t = He(), n) {
  await Fy({ wrapMeta: e, currentRpId: Ut(), policy: t, abortSignal: n });
}
async function Z6(e, t = He()) {
  await lr((n) => W6(e, t, n));
}
async function hd(e) {
  let t;
  try {
    await la({
      ...e,
      passkey: "unlock",
      ...e.abortSignal ? { onUnlocked: (n) => {
        t = n, e.onUnlocked?.(n);
      } } : {}
    });
  } catch (n) {
    throw e.abortSignal ? (t?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), t?.release()) : pt(e.wrapMeta).state === "unlocked" && mt({ broadcast: !0, reason: "manual-lock" }), n;
  } finally {
    e.onUnlocked || t?.release();
  }
}
async function Y6(e, t) {
  if (pt(e).state !== "unlocked") {
    if (!e) throw new Error("vault-locked");
    await Z6(e);
  }
  return await Ys(t, { renewOnSuccess: !0 });
}
async function jg(e) {
  const t = pt(e.bindRoot ? void 0 : e.wrapMeta).state, n = He(), r = Ag({ tier: e.tier, vaultState: t, policy: n });
  let i = !1, o;
  const s = async () => {
    const c = e.bindRoot ? Zs(e.wrapMeta) : void 0;
    try {
      await hd({
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
    let u = !1;
    try {
      o?.assertCurrent();
      const d = await Ys(
        async (f, h, y) => {
          const w = await e.buildPrompt(f, h, y);
          return r.confirm && !l && (await la({
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
      return u = !0, d;
    } catch (d) {
      throw !e.bindRoot && i && !u && pt().state === "unlocked" && mt({ broadcast: !0, reason: "manual-lock" }), d;
    }
  };
  try {
    return r.passkey === "unlock" ? (await s(), await a("none", !0)) : await a(r.passkey === "uv" ? "uv" : "none");
  } catch (c) {
    if (!e.bindRoot && r.passkey !== "unlock" && Fg(c, e.wrapMeta))
      return await s(), await a("none", !0);
    throw e.bindRoot && o?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), c;
  } finally {
    o?.release();
  }
}
function We(e, t) {
  if (e instanceof S) return e;
  if (Hy(e)) return new S("passkey_cancelled", "passkey-cancelled", !0);
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
function X6(e) {
  return ce(new TextEncoder().encode(JSON.stringify(e))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function J6(e) {
  return zg(JSON.parse(new TextDecoder().decode(Ae(e))));
}
function Q6(e, t) {
  return qf("setup", e, Ug(), { req: X6(t) });
}
function Qa(e) {
  const t = Lt(e);
  return {
    credentialId: Fe(t.credentialId, "credentialId"),
    ...t.authzRegistration !== void 0 ? { authzRegistration: t.authzRegistration } : {}
  };
}
function da(e) {
  return `${S6}${e}`;
}
function ua(e) {
  return `${_6}${e}`;
}
function Ff(e) {
  try {
    localStorage.removeItem(da(e)), localStorage.removeItem(ua(e));
  } catch {
  }
}
function e3(e, t) {
  try {
    localStorage.setItem(da(e), JSON.stringify({ build: he, result: t })), localStorage.removeItem(ua(e));
  } catch {
  }
}
function t3(e, t) {
  try {
    localStorage.setItem(
      ua(e),
      JSON.stringify({ build: he, code: t.code, message: t.message, retryable: t.retryable })
    ), localStorage.removeItem(da(e));
  } catch {
  }
}
function Vg(e) {
  if (window.self === window.top)
    return Ll({
      rpId: Ut(),
      vaultUserHandle: e.vaultUserHandle,
      displayName: e.displayName,
      authzCreationOptions: e.authzCreationOptions
    });
  const t = Mg(), n = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${Hg}${t}`);
  let r = null;
  return Ff(t), new Promise((i, o) => {
    let s = !1, a = null, c = null, l = null;
    const u = () => d(() => o(new S("setup_window_closed", "requesting sandbox was unloaded", !0))), d = (w) => {
      if (!s) {
        s = !0, a && clearTimeout(a), c && clearInterval(c), l && clearTimeout(l), window.removeEventListener("visibilitychange", h), window.removeEventListener("focus", h), window.removeEventListener("storage", h), window.removeEventListener("message", y), window.removeEventListener("pagehide", u), n?.close(), Ff(t);
        try {
          r?.close();
        } catch {
        }
        w();
      }
    }, f = () => {
      let w = null, p = null;
      try {
        w = localStorage.getItem(da(t)), p = localStorage.getItem(ua(t));
      } catch {
        return !1;
      }
      if (w) {
        try {
          const g = Lt(JSON.parse(w));
          if (!Br(g.build)) throw new S("sandbox_build_mismatch");
          const _ = Qa(g.result);
          d(() => i(_));
        } catch (g) {
          const _ = g instanceof Error ? g.message : "stored setup result is invalid";
          d(() => o(new S("setup_storage_invalid", _)));
        }
        return !0;
      }
      if (p) {
        try {
          const g = Lt(JSON.parse(p));
          if (!Br(g.build)) throw new S("sandbox_build_mismatch");
          d(
            () => o(new S(Fe(g.code, "code"), Ng(g.message, "message"), !!g.retryable))
          );
        } catch (g) {
          const _ = g instanceof Error ? g.message : "stored setup error is invalid";
          d(() => o(new S("setup_storage_invalid", _)));
        }
        return !0;
      }
      return !1;
    }, h = () => {
      f();
    }, y = (w) => {
      if (w.origin !== window.location.origin || w.source !== r) return;
      const p = w.data;
      !p || p.id !== t || !Br(p.build) || (p.type === "setup-created" ? d(() => i(Qa(p.result))) : p.type === "setup-error" && d(() => o(new S(p.code, p.message, !!p.retryable))));
    };
    if (a = setTimeout(() => {
      f() || d(() => o(new S("setup_window_timeout", "setup window timed out", !0)));
    }, I6), c = setInterval(() => {
      !r?.closed || l || f() || (l = setTimeout(() => {
        f() || d(() => o(new S("setup_window_closed", "setup window was closed", !0)));
      }, 2500));
    }, 500), n && (n.onmessage = (w) => {
      const p = w.data;
      !p || p.id !== t || !Br(p.build) || (p.type === "setup-created" ? d(() => i(Qa(p.result))) : p.type === "setup-error" && d(() => o(new S(p.code, p.message, !!p.retryable))));
    }), window.addEventListener("visibilitychange", h), window.addEventListener("focus", h), window.addEventListener("storage", h), window.addEventListener("message", y), window.addEventListener("pagehide", u, { once: !0 }), r = window.open(Q6(t, e), "avibe-vault-setup", "popup,width=440,height=620"), !r) {
      d(() => o(new S("setup_popup_blocked", "setup window was blocked", !0)));
      return;
    }
    r.focus();
  });
}
function fr(e) {
  const t = e.reason;
  return t instanceof Error ? t : new Error("operation-superseded");
}
function er(e) {
  if (e?.aborted) throw fr(e);
}
function n3() {
  const e = window.navigator;
  return /iP(?:hone|ad|od)/.test(e.userAgent) || e.platform === "MacIntel" && e.maxTouchPoints > 1;
}
function jf(e) {
  return n3() && Ry(e);
}
async function qg(e, t, n, r, i = He()) {
  const o = wx();
  let s, a;
  try {
    er(r), s = (await Js([{ credentialId: n.credentialId, prfSalt: o }], t, r)).prfOutput, er(r), a = yx();
    const l = await bx(a, [{ kind: "passkey", prfOutput: s, prfSalt: o, credentialId: n.credentialId }]);
    er(r);
    let u = xx(l, {
      rpId: t,
      vaultUserHandle: e.vaultUserHandle
    });
    e.rootMetadata && (u = Ax(u, await Ex(a, e.rootMetadata, u)), er(r));
    const d = Iy({
      vmk: a,
      wrapMeta: u,
      freshSetup: !e.existingProtectedVault,
      scopeId: s2(e.vaultUserHandle),
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
function r3(e, t, n) {
  return lr((r) => {
    const i = Qs(
      "setup.title",
      "setup.subtitle",
      "setup.iframeBody"
    ), o = Tt("setup.createPasskey"), s = As("setup.ready");
    return J(i.card, o), J(i.card, s), new Promise((a, c) => {
      let l = !1, u = "create", d = null, f = !1;
      const h = (B) => {
        l || (l = !0, r.removeEventListener("abort", p), ea(), B());
      }, y = (B) => {
        h(() => c(B));
      }, w = (B) => {
        l || (u = "create", d = null, Y(o, "setup.createPasskey"), o.disabled = !1, Y(s, B));
      }, p = () => {
        y(fr(r));
      }, g = (B) => {
        if (B instanceof S && B.code === "setup_popup_blocked") {
          w("setup.popupBlocked");
          return;
        }
        y(B);
      }, _ = () => {
        f = !0, o.disabled = !0, Y(s, "setup.popupPrompt");
        let B;
        try {
          B = Vg(e);
        } catch (R) {
          g(R);
          return;
        }
        B.then(
          (R) => {
            l || (d = R, u = "finish", Y(o, "setup.finish"), o.disabled = !1, Y(s, "setup.createdFinish"));
          },
          (R) => {
            g(R);
          }
        );
      }, k = (B) => {
        o.disabled = !0, Y(s, "setup.passkeyPrompt"), qg(e, t, B, r, n).then(
          (R) => h(() => a(R)),
          (R) => y(R)
        );
      }, C = () => {
        o.disabled = !0, Y(s, "setup.passkeyPrompt");
        let B;
        try {
          B = Ll({
            rpId: t,
            vaultUserHandle: e.vaultUserHandle,
            displayName: e.displayName,
            authzCreationOptions: e.authzCreationOptions
          });
        } catch (R) {
          if (jf(R)) {
            _();
            return;
          }
          y(R);
          return;
        }
        B.then(
          (R) => k(R),
          (R) => {
            if (jf(R)) {
              _();
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
          if (u === "finish" && d) {
            k(d);
            return;
          }
          if (f) {
            _();
            return;
          }
          C();
        }
      });
    });
  });
}
async function i3(e) {
  const t = zg(e);
  try {
    const n = Ut(), r = t.policy ?? He(), i = window.self !== window.top ? await r3(t, n, r) : await qg(t, n, await Vg(t), void 0, r), o = t.policy ? Ol(r) : He();
    return { ...i, policy: o };
  } catch (n) {
    throw We(n, "setup_failed");
  }
}
async function o3(e) {
  const t = L6(e);
  try {
    const n = Ut(), r = t.policy ?? He(), i = await lr(async (s) => {
      let a;
      if (await Oi(
        {
          title: "unlock.sessionTitle",
          subtitle: $e("unlock.sessionSubtitle", {
            minutes: r.windowSeconds / 60
          }),
          body: "unlock.sessionBody",
          confirmLabel: "common.continue"
        },
        s,
        void 0,
        async () => {
          a = await Fy({ wrapMeta: t.wrapMeta, currentRpId: n, abortSignal: s, policy: r });
        }
      ), !a) throw new Error("vault unlock did not complete");
      return a;
    }), o = t.policy ? Ol(r) : He();
    return { ...i, policy: o };
  } catch (n) {
    throw We(n, "unlock_failed");
  }
}
async function s3(e) {
  const t = Q2(e);
  t.wrapMeta && Hl(t.wrapMeta);
  try {
    if (t.kind === "static" && !He().parentValueSealAllowed)
      throw new S("parent_value_seal_disabled", "parent-value sealing is disabled by policy");
    return await Y6(t.wrapMeta, async (n, r) => ({ ...t.kind === "static" ? await q_({ name: t.name, value: t.value, vmk: n, wrapMeta: r }) : await G_({ name: t.name, curve: t.curve, vmk: n, wrapMeta: r }), establishingVmk: bc() }));
  } catch (n) {
    throw We(n, "seal_failed");
  }
}
async function a3(e, t) {
  try {
    const n = Og(e), r = t.takeAuthorizationWindow();
    if (!r) throw new S("invalid_payload", "sealHdWallet requires a parent-opened authorization window");
    const i = Zs(n.wrapMeta), o = { ...n, kind: "protected-hd-wallet", deadlineAt: Date.now() + Pg };
    try {
      return await lr(async (s) => {
        const a = ud([s, i.signal]);
        try {
          i.assertCurrent();
          const c = await fd(o, a.signal, r);
          i.assertCurrent();
          const l = Ps(c, o);
          return i.assertCurrent(), { envelope: l.envelope, accounts: l.accounts, expectedAccount0: l.expectedAccount0, establishingVmk: i.freshSetup };
        } finally {
          a.cleanup();
        }
      });
    } finally {
      i.release();
    }
  } catch (n) {
    throw We(n, "seal_hd_failed");
  }
}
async function c3(e, t) {
  const n = s6(e);
  try {
    const { sealed: r, vmkWrapMeta: i, recordMetadata: o } = Un(n.material.envelope);
    if (!dd(o)) throw new Error("only static protected records can be revealed as plaintext");
    return _s({
      context: n.context,
      expectedPurpose: "reveal",
      secret: { name: n.material.name, kind: "static" }
    }), await jg({
      tier: "R2",
      wrapMeta: i,
      bindRoot: !0,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: {
        title: "reveal.showConfirmTitle",
        subtitle: xe(n.material.name),
        body: "reveal.confirmBody",
        details: zi(n.context.display),
        label: "common.continue"
      },
      buildPrompt: async (s, a) => {
        const c = await hs(a, s);
        return Jn({ context: n.context, rootMetadata: c, expectedPurpose: "reveal" }), Do(n), _s({
          context: n.context,
          expectedPurpose: "reveal",
          secret: { name: n.material.name, kind: "static" }
        }), Ci([n.context]), {
          title: "reveal.showConfirmTitle",
          subtitle: xe(n.material.name),
          body: "reveal.confirmBody",
          details: zi(n.context.display),
          challenge: await Mf(ta(n.context)),
          label: "common.continue"
        };
      },
      operation: async (s, a, c) => {
        const l = await hs(a, s);
        c.assertCurrent(), Jn({ context: n.context, rootMetadata: l, expectedPurpose: "reveal" }), Do(n), Ci([n.context]);
        const u = await ji(r, s, Gs(n.material.name, o));
        try {
          c.assertCurrent(), Jn({ context: n.context, rootMetadata: l, expectedPurpose: "reveal" }), Do(n), await Ri([n.context]), c.assertCurrent(), Jn({ context: n.context, rootMetadata: l, expectedPurpose: "reveal" }), await Z2({
            name: n.material.name,
            plaintext: u,
            abortSignal: c.signal,
            prepareCopy: He().strictApprovals ? async (d) => {
              await Kg({
                wrapMeta: a,
                challenge: await Mf(`reveal-copy:${n.context.requestId}:${n.material.name}`),
                abortSignals: [d, c.signal]
              }), er(d), er(c.signal);
            } : void 0
          });
        } finally {
          u.fill(0);
        }
        return { completed: !0 };
      }
    });
  } catch (r) {
    throw We(r, "reveal_failed");
  }
}
async function l3(e, t) {
  try {
    const n = L_(e), { request: r, unpacked: { vmkWrapMeta: i } } = n, o = (s) => {
      const a = r.context.display.sessionLabel;
      if (!s.review && s.display === void 0) throw new S("invalid_payload", "signing request has no reviewable content");
      return {
        title: "sign.title",
        subtitle: xe(r.material.name),
        ...s.review ? { review: s.review, ...a ? { reviewSession: a } : {} } : { body: xe(s.display ?? "") },
        details: zi(r.context.display, void 0, "sign"),
        label: "sign.confirm"
      };
    };
    if (n.hd) {
      let s;
      try {
        if (pt().state !== "unlocked") {
          const a = Zs(i);
          try {
            await hd({
              title: "hd.unlockTitle",
              subtitle: xe(r.material.name),
              body: "hd.unlockBody",
              label: "common.continue",
              wrapMeta: i,
              policy: He(),
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
        return s?.assertCurrent(), await Ys(async (a, c, l) => {
          const u = await Ef(n, a, c);
          return l.assertCurrent(), await la({
            ...o(u),
            wrapMeta: c,
            passkey: "uv",
            challenge: new Uint8Array(n.challenge),
            abortSignal: l.signal,
            parentSurface: t.latestSurface,
            authorizationWindow: t.takeAuthorizationWindow
          }), l.assertCurrent(), Af(n, a, c);
        }, { renewOnSuccess: !1, expectedWrapMeta: i, beforeSuccess: () => Ri([r.context]) });
      } catch (a) {
        throw s?.lockIfCurrent({ broadcast: !0, reason: "manual-lock" }), a;
      } finally {
        s?.release();
      }
    }
    return await jg({
      tier: "R3",
      wrapMeta: i,
      parentSurface: t.latestSurface,
      authorizationWindow: t.takeAuthorizationWindow,
      lockedPrompt: o(n.verified),
      // Shown only after the daemon signature, expiry and replay state are
      // verified with the vault key, from content re-derived at that moment.
      buildPrompt: async (s, a) => ({
        ...o(await Ef(n, s, a)),
        challenge: new Uint8Array(n.challenge)
      }),
      operation: (s, a) => Af(n, s, a),
      beforeSuccess: () => Ri([r.context])
    });
  } catch (n) {
    throw We(n, "sign_failed");
  }
}
async function d3(e, t) {
  const n = U6(e);
  try {
    const i = Un(n.items[0].material.envelope).vmkWrapMeta;
    for (const f of n.items) {
      const h = Un(f.material.envelope);
      if (h.vmkWrapMeta !== i) throw new S("invalid_payload", "approveRelease items must share one VMK context");
      if (!dd(h.recordMetadata)) throw new S("invalid_payload", "approveRelease only supports static protected records");
    }
    const o = Sg(n.items), s = pt(i).state, a = He(), c = Ag({ tier: "R2", vaultState: s, policy: a });
    let l = !1;
    const u = async () => {
      await hd({
        title: "release.title",
        subtitle: $e("release.subtitle", { count: n.items.length }),
        details: zi(o.display, o.recipient),
        label: "release.confirm",
        wrapMeta: i,
        policy: a,
        parentSurface: t.latestSurface,
        authorizationWindow: t.takeAuthorizationWindow
      }), l = !0;
    }, d = async (f, h = !1) => {
      let y = !1, w;
      try {
        const p = await Ys(
          async (g, _, k) => {
            const C = await Z_({
              items: n.items,
              vmk: g,
              wrapMeta: _,
              consumeReplayIds: !1,
              onApprovalAccepted: (B) => {
                w = B;
              },
              confirm: (B) => !c.confirm || h ? Promise.resolve() : la({
                title: "release.title",
                subtitle: $e("release.subtitle", { count: n.items.length }),
                details: zi(B.display, B.recipient),
                challenge: B.challenge,
                label: "release.confirm",
                wrapMeta: _,
                passkey: f,
                abortSignal: k.signal,
                parentSurface: t.latestSurface,
                authorizationWindow: t.takeAuthorizationWindow
              })
            });
            return k.assertCurrent(), C;
          },
          {
            renewOnSuccess: c.renewOnSuccess,
            beforeSuccess: () => Ri(n.items.map((g) => g.context), w)
          }
        );
        return y = !0, p;
      } catch (p) {
        throw l && !y && pt().state === "unlocked" && mt({ broadcast: !0, reason: "manual-lock" }), p;
      }
    };
    try {
      return c.passkey === "unlock" ? (await u(), await d("none", !0)) : await d(c.passkey === "uv" ? "uv" : "none");
    } catch (f) {
      if (c.passkey !== "unlock" && Fg(f, i))
        return await u(), await d("none", !0);
      throw f;
    }
  } catch (r) {
    throw We(r, "approve_release_failed");
  }
}
async function u3(e, t) {
  let n;
  if (await lr(async (r) => {
    Rn(e);
    const i = { kind: "signing-policy-downgrade", context: e }, o = async (c) => {
      Rn(e);
      const l = await fd(i, r, c);
      if (l.kind !== "assertion") throw new S("authorization_result_invalid", "policy assertion is missing");
      n = Rc(l.assertion, e);
    }, s = t.takeAuthorizationWindow();
    if (s) {
      await o(s);
      return;
    }
    Rn(e);
    const a = Ly(e);
    await Oi(
      a,
      r,
      (c) => Eg({
        uiShowPending: zy,
        parentSurface: () => t.latestSurface(),
        visibilityTarget: c,
        requireParentSurface: !0
      }),
      () => o()
    );
  }), !n) throw new S("authorization_result_invalid", "policy assertion is missing");
  return Rc(n, e);
}
async function f3(e, t) {
  try {
    const n = n6(e, {
      origin: window.location.origin,
      hostname: window.location.hostname
    }), r = await u3(n, t);
    return { challengeId: n.challengeId, assertion: r };
  } catch (n) {
    throw We(n, "policy_authorization_failed");
  }
}
async function h3(e) {
  const t = N6(e);
  try {
    const n = await S2(t.webauthn);
    return { challengeId: t.challengeId, assertion: n };
  } catch (n) {
    throw We(n, "delete_authz_failed");
  }
}
function p3() {
  const e = Lc();
  if (e.get("mode") !== "setup") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && ca({ locale: t, theme: n });
  const r = e.get("id"), i = document.getElementById("page"), o = i?.querySelector(".card"), s = i?.querySelector("h1"), a = i?.querySelector(".sub"), c = i?.querySelector(".body");
  if (!r || !o || !s || !a || !c) return;
  document.body.classList.add("setup-mode"), Y(s, "setup.title"), Y(a, "setup.subtitle"), Y(c, "setup.topLevelBody");
  const l = document.createElement("button");
  l.type = "button", l.className = "action", Y(l, "setup.createPasskey");
  const u = document.createElement("p");
  u.className = "setup-status", Y(u, "setup.readyOnOrigin"), o.append(l, u);
  const d = new URLSearchParams(window.location.hash.slice(1)).get("req");
  if (!d) {
    l.disabled = !0, Y(u, "setup.requestMissing");
    return;
  }
  let f;
  try {
    f = J6(d);
  } catch {
    l.disabled = !0, Y(u, "setup.requestInvalid");
    return;
  }
  const h = typeof BroadcastChannel > "u" ? null : new BroadcastChannel(`${Hg}${r}`), y = (w) => {
    const p = We(w, "setup_create_failed"), g = {
      type: "setup-error",
      build: he,
      id: r,
      code: p.code,
      message: p.message,
      retryable: p.retryable
    };
    t3(r, p), h?.postMessage(g);
    try {
      window.opener?.postMessage(g, window.location.origin);
    } catch {
    }
    l.disabled = !1, Y(u, xe(p.message));
  };
  l.addEventListener("click", () => {
    l.disabled = !0, Y(u, "setup.passkeyPrompt");
    let w;
    try {
      w = Ll({
        rpId: Ut(),
        vaultUserHandle: f.vaultUserHandle,
        displayName: f.displayName,
        authzCreationOptions: f.authzCreationOptions
      });
    } catch (p) {
      y(p);
      return;
    }
    w.then((p) => {
      const g = { type: "setup-created", build: he, id: r, result: p };
      e3(r, p), h?.postMessage(g);
      try {
        window.opener?.postMessage(g, window.location.origin);
      } catch {
      }
      Y(u, "setup.created"), setTimeout(() => window.close(), 250);
    }).catch(y);
  });
}
function y3() {
  const e = Lc();
  if (e.get("mode") !== "authorize") return;
  const t = e.get("locale"), n = e.get("theme");
  (t === "en" || t === "zh") && (n === "light" || n === "dark") && ca({ locale: t, theme: n });
  const r = e.get("id"), o = document.getElementById("page")?.querySelector(".card"), s = o?.querySelector(".body");
  if (!r || !o || !s || (s.classList.add("waiting"), Y(s, window.opener ? "authorize.waiting" : "authorize.waitingLong"), !window.opener)) return;
  const a = () => {
    const p = window.opener;
    if (!p) return [];
    const g = [p];
    try {
      for (let _ = 0; _ < p.frames.length; _ += 1) g.push(p.frames[_]);
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
  let u = !1, d = null;
  const f = setTimeout(() => Y(s, "authorize.waitingLong"), T6), h = () => {
    if (!u)
      for (const p of a())
        try {
          p.postMessage({ type: "authorization-ready", build: he, id: r }, window.location.origin);
        } catch {
        }
  }, y = (p) => {
    if (u) return;
    u = !0, d && clearInterval(d), clearTimeout(f), s.classList.remove("waiting"), window.removeEventListener("message", w), document.body.classList.add("setup-mode");
    const g = new AbortController(), _ = (R) => {
      const L = We(R, R instanceof Error && R.message === "operation-cancelled" ? "authorization_cancelled" : "authorization_failed");
      l({
        type: "authorization-error",
        build: he,
        id: r,
        code: L.code,
        message: L.message,
        retryable: L.retryable
      }), setTimeout(() => window.close(), 250);
    }, k = (R, L) => {
      R.then(
        () => {
          try {
            l({ type: "authorization-approved", build: he, id: r, result: L() }), setTimeout(() => window.close(), 250);
          } catch (H) {
            _(H);
          }
        },
        _
      );
    };
    if ("kind" in p) {
      if (p.kind === "protected-hd-wallet") {
        let H;
        const $ = () => g.abort(new S("authorization_cancelled", "protected HD window closed", !0)), D = setTimeout(() => g.abort(new S("authorization_window_timeout", "protected HD window expired", !0)), ar(p));
        window.addEventListener("pagehide", $, { once: !0 });
        const z = l6(p, g.signal, A6).then((x) => {
          H = x;
        }).catch((x) => {
          throw Hy(x) || x instanceof Error && ["operation-cancelled", "passkey-cancelled"].includes(x.message) ? new S("authorization_cancelled", "protected HD operation cancelled", !0) : x;
        }).finally(() => {
          clearTimeout(D), window.removeEventListener("pagehide", $);
        });
        k(z, () => {
          if (g.signal.aborted) throw fr(g.signal);
          if (ar(p), !H) throw new S("authorization_result_invalid", "protected HD result is missing");
          return H;
        });
        return;
      }
      if (p.kind !== "signing-policy-downgrade") {
        _(new S("authorization_request_invalid", "authorization request kind is invalid"));
        return;
      }
      try {
        Rn(p.context);
      } catch (H) {
        _(H);
        return;
      }
      let R;
      const L = Ly(p.context);
      k(
        Oi(
          L,
          g.signal,
          (H) => Eg({
            uiShowPending: zy,
            visibilityTarget: H,
            requireParentSurface: !0
          }),
          async () => {
            Rn(p.context), R = await i6(p.context, g.signal);
          }
        ),
        () => {
          if (!R) throw new S("authorization_result_invalid", "policy assertion is missing");
          return { kind: "assertion", assertion: R };
        }
      );
      return;
    }
    let C = { kind: "confirmed" };
    const B = async () => {
      if (p.passkey === "none") return;
      if (p.passkey === "uv") {
        if (!p.prompt.challenge) throw new S("invalid_payload", "authorization challenge is missing");
        await Kg({
          wrapMeta: p.wrapMeta,
          challenge: p.prompt.challenge,
          abortSignals: [g.signal]
        });
        return;
      }
      const R = await Js(qs(p.wrapMeta), Ut(), g.signal);
      C = {
        kind: "unlock",
        prfOutput: ce(R.prfOutput),
        prfSalt: ce(R.prfSalt)
      }, R.prfOutput.fill(0);
    };
    k(Oi(kg(p.prompt), g.signal, void 0, B), () => C);
  }, w = (p) => {
    if (p.origin !== window.location.origin) return;
    const g = p.data;
    if (!g || g.type !== "authorization-request" || g.id !== r || !Br(g.build)) return;
    const _ = a().find((k) => k === p.source);
    if (_) {
      c = _;
      try {
        y(K6(g.request));
      } catch (k) {
        const C = We(k, "authorization_request_invalid");
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
  window.addEventListener("message", w), h(), d = setInterval(h, 500);
}
function w3() {
  if (window.self !== window.top || Lc().has("mode")) return;
  const e = document.querySelector("#page .body");
  e && Y(e, "sandbox.intro");
}
Le.register("handshake", (e) => {
  const t = e ?? {}, n = typeof t.expectedBuildHash == "string" ? t.expectedBuildHash : null;
  if (typeof t.parentOrigin != "string" || typeof t.nonce != "string" || t.nonce.length < 16)
    throw new S("invalid_handshake", "handshake must include parentOrigin and nonce");
  if (n !== null && n !== he.buildHash)
    throw new S("build_hash_mismatch", "sandbox build hash does not match parent expectation");
  const r = O6(t.appearance);
  r && ca(r);
  const i = zf ? He() : Ol(t.policy === void 0 ? void 0 : Pl(t.policy));
  return zf = !0, {
    accepted: !0,
    channel: Fn,
    version: 2,
    sandboxOrigin: window.location.origin,
    build: he,
    policy: i
  };
});
Le.register("set-appearance", (e) => (ca(Lg(e)), { accepted: !0 }));
Le.register("status", (e) => {
  const t = H6(e);
  try {
    t.wrapMeta && pt(t.wrapMeta);
    const n = pt(), r = He();
    return {
      ...n,
      policy: r,
      session: {
        ...n.expiresAt !== void 0 ? { expiresAt: n.expiresAt } : {},
        strict: r.strictApprovals
      }
    };
  } catch (n) {
    throw We(n, "invalid_wrap_meta");
  }
});
Le.register("setup", i3);
Le.register("unlock", o3);
Le.register("lock", () => ({ ...mt({ broadcast: !0, reason: "manual-lock" }), policy: He() }));
Le.register("seal", s3);
Le.register("sealHdWallet", a3);
Le.register("reveal", c3);
Le.register("sign", l3);
Le.register("approveRelease", d3);
Le.register("deleteAuthzAssertion", h3);
Le.register("authorizeSigningPolicyDowngrade", f3);
i2((e) => Le.emit("vault.state", e));
F2((e) => Le.emit(e));
await nm();
Le.start();
window.addEventListener("pagehide", h2);
p3();
y3();
w3();
window.self !== window.top && document.body.classList.add("embedded");
const Gg = globalThis.crypto, g3 = { webcrypto: Gg }, Wg = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  default: g3,
  webcrypto: Gg
}, Symbol.toStringTag, { value: "Module" }));
