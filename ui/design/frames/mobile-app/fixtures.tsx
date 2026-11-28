import type { ReactNode } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { AppLicenses } from '@/mobile/appLicenses';
import type { MobileInstance, MobileSessionState } from '@/mobile/bridge';
import type { FrameSpec } from '../../frame';
import { copyFor } from '../desktop/instances-fixtures';

export type MobileLocale = 'zh' | 'en';
export type SignedInMobileSession = Extract<MobileSessionState, { status: 'signedIn' }>;

export const noop = () => {};
export const account = { name: 'Max', email: 'max@example.com' };
export const longEmail = 'alexandra.chen+research-and-product-planning@international.example.com';

export const personal: MobileInstance = {
  id: 'personal',
  name: 'Personal Workbench',
  host: 'personal-app.avibe.bot',
  url: 'https://personal-app.avibe.bot',
  relationship: 'owner',
  owner_email: null,
  available: true,
};

export function signedInSession(
  instances: MobileInstance[],
  selectedId: string | null = personal.id,
  email = account.email,
): SignedInMobileSession {
  return {
    status: 'signedIn',
    account: { id: 'account', email },
    selected_instance_id: selectedId,
    instances,
  };
}

export type BilingualOptions = {
  size?: [number, number];
  status?: FrameSpec['status'];
  click?: FrameSpec['click'];
};

/** Locale is explicit so both translations survive the exporter's id+theme key. */
export function bilingual(
  name: string,
  title: string,
  render: (lang: MobileLocale) => ReactNode,
  sizeOrOptions: [number, number] | BilingualOptions = [390, 844],
): FrameSpec[] {
  const options: BilingualOptions = Array.isArray(sizeOrOptions) ? { size: sizeOrOptions } : sizeOrOptions;
  const [width, height] = options.size ?? [390, 844];
  return (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    width,
    height,
    status: options.status ?? 'shipped',
    themes: ['light', 'dark'],
    ...(options.click ? { click: options.click } : {}),
    fixtures: {
      bots: [],
      api: { '/api/session': { remote: true, authenticated: true, email: account.email } },
      // Locale-specific ids must carry the same locale into shipped component strings,
      // even when the canvas itself is opened in the other language.
      copy: copyFor(lang === 'en' ? en : zh),
    },
    render: () => <div lang={lang} className="h-full">{render(lang)}</div>,
  }));
}

/** License texts keep the hard wraps they ship with; the page reflows them into paragraphs. */
const mitText = `Permission is hereby granted, free of charge, to any
person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the
Software without restriction, including without
limitation the rights to use, copy, modify, merge,
publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software
is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice
shall be included in all copies or substantial portions
of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR
IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.`;
const placeholder = 'The full license text, reflowed into paragraphs, shows when this group is open.';
const uniffiCommit = '05b45b110f652d58d94c653992aeb3adc3a094f4';
const crate = (name: string, version: string) => ({ name, registry: `https://static.crates.io/crates/${name}/${name}-${version}.crate` });
/** Illustrative component names (crates.io names, not the actual iOS graph); group sizes as reviewed. */
const names = (list: string) => list.split(' ').map((name) => ({ name, version: '1.0.0', origin: 'native' as const }));
/**
 * Shaped like the notices the iOS iroh build collects (packaging/iroh-apple, #3551): attohttpc and
 * nine uniffi-family crates under MPL-2.0. Versions, names and counts are illustrative; the App
 * renders the generated file.
 */
export const licensesFixture: AppLicenses = {
  schema_version: 1,
  native: { state: 'published', release_tag: 'iroh-apple-v1.2.0-1' },
  web: { sha256: '0'.repeat(64), packages: 19 },
  components: 330,
  sources: [
    { crates: [crate('attohttpc', '0.30.1')], version: '0.30.1', upstream: `https://github.com/sbstp/attohttpc/tree/${'0'.repeat(40)}` },
    { crates: ['uniffi', 'uniffi_bindgen', 'uniffi_build', 'uniffi_core', 'uniffi_internal_macros', 'uniffi_macros', 'uniffi_meta', 'uniffi_pipeline', 'uniffi_udl'].map((name) => crate(name, '0.31.2')), version: '0.31.2', upstream: `https://github.com/mozilla/uniffi-rs/tree/${uniffiCommit}` },
  ],
  groups: [
    { title: 'MIT', components: names('adler2 aes allocator-api2 android_system_properties apple-cf archery arrayvec async-compat async-lock async-trait atomic-waker autotools base32 basic-toml bitflags block-buffer borrow-or-share bs58 bytecount bytes camino cap-std cesu8 cfg-expr chrono-tz ciborium-ll clap_builder cmov combine convert_case cookie_store core-foundation-sys crossbeam-channel crossbeam-utils ctor ctr curve25519-dalek-derive data-encoding-macro-internal der digest dispatch2 dlopen2 dpi dtor ed25519 email_address embed-resource enumflags2_derive errno fax fiat-crypto find-msvc-tools flagset fnv foreign-types-shared fs_extra fs2 futures-core futures-lite gdk gdk-sys gdkx11-sys gio glib-macros goblin gtk half hex hmac http-body-util httpdate hyper-rustls icu_locale_core icu_locid_transform icu_provider identity-hash idna_adapter inotify-sys io-lifetimes iroh-metrics is_terminal_polyfill is-wsl jni-sys js-sys k256 kqueue-sys lenient_semver_parser lexical-util libc litemap log mac-addr maybe-owned micromap miniz_oxide msbuild n0-dns-resolver native-tls ndk-sys netwatch nix noq-proto nu-ansi-term num_enum_derive num-integer num-rational objc2 objc2-core-image objc2-core-text objc2-metal objc2-service-management objc2-ui-kit openssl-probe ordered-stream papaya parking_lot_core paste phf_codegen phf_shared pkcs8 plist poly1305 potential_utf precomputed-hash psl-types pyo3 pyo3-macros quinn quote rand_core redox_syscall ref-cast-impl regorus rfd rustix rustls-native-certs rustls-webpki schemars screencapturekit security-framework-sys selectors serde_bytes serde_spanned serde_with serdect servo_arc sha1_smol signature simdutf8 smappservice-rs softbuffer solana-atomic-u64 solana-msg solana-program-pack solana-short-vec solana-transaction soup3 spki ssign-core string_cache strum swift-rs system-configuration tao tauri tauri-macros tauri-plugin-deep-link tauri-plugin-opener tauri-runtime tendril textwrap thread_local tiny-keccak tinystr tokio-macros tokio-rustls tokio-util toml_writer tower-http tracing-log triomphe tss-esapi typenum unic-char-property unicode-general-category unicode-xid universal-hash urlpattern utf16_iter uuid-simd vsimd walkdir wasm-bindgen-backend wasm-bindgen-macro-support web-time webpki-roots webview2-com-sys winapi-util wincode-derive windows_i686_gnu windows_x86_64_gnu windows-collections windows-link windows-result windows-version wit-bindgen writeable x11rb x509-cert zbus_macros zerocopy zerofrom-derive zip zmij'), text: mitText },
    { title: 'Apache-2.0', components: names('aho-corasick anstyle-parse askama_parser async-recursion aws-lc-sys bit-set borsh-derive byteorder cargo-platform chacha20poly1305 clipboard-rs const-random coset crypto-common darling_macro derive_more document-features dunce enum_dispatch event-listener-strategy five8_const foreign-types futures-buffered futures-task getrandom global-hotkey heapless html5ever iana-time-zone-haiku icu_normalizer_data indoc iroh javascriptcore-rs-sys jsonptr lenient_version libredox malloced minimal-lexical n0-future netlink-packet-route notify-rust num-complex objc2-core-data objc2-exception-helper once_cell outref pem-rfc7468 pin-project-internal portable-atomic proc-macro-error-attr python3-dll-a rand regex-automata rust-ini salsa20 scrypt serde_derive_internals serde_yaml_ng sharded-slab slab solana-instruction solana-sanitize speedate static_assertions sync_wrapper tar tauri-plugin-global-shortcut tauri-winres time-core tls_codec toml_datetime tracing tungstenite unic-ucd-ident ureq utf8-zero wasi wasm-streams weezl windows_aarch64_gnullvm windows-implement windows-targets x11 yoke zerotrie zstd-sys zvariant'), text: placeholder },
    { title: 'MPL-2.0', components: names('attohttpc uniffi uniffi_bindgen uniffi_build uniffi_core uniffi_internal_macros uniffi_macros uniffi_meta uniffi_pipeline uniffi_udl'), text: placeholder },
    { title: 'BSD-3-Clause', components: names('ambient-authority anstream anstyle anstyle-query anyhow apple-metal arc-swap arrayref askama'), text: placeholder },
    { title: 'ISC', components: names('brotli-decompressor bstr bumpalo bytemuck bytemuck_derive byteorder-lite'), text: placeholder },
    { title: 'Unicode-3.0', components: names('crypto-bigint cssparser cssparser-macros ctor-proc-macro'), text: placeholder },
  ],
};
