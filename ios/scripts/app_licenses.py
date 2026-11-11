#!/usr/bin/env python3
"""Generate and verify the App's Open-source licenses data.

The page (Account -> About -> Open-source licenses) renders `licenses.json` from
the bundled mobile UI. Its JavaScript notices come from `licenses-web.json`,
which `npm run build:mobile` writes from the modules the bundle actually ships
(ui/scripts/bundleNotices.mjs). Its native notices come only from the published
`IrohLib-support.zip` that `packaging/iroh-apple/release.json` pins; a pending
pin is never consumed. Nothing here links iroh, publishes, or promotes a pin.

    app_licenses.py generate --bundle ios/Avibe/Resources/MobileApp
    app_licenses.py verify --bundle ios/Avibe/Resources/MobileApp

`generate` downloads the pinned asset unless `--support-zip` names a local copy;
either way the bytes must match the pin's size and SHA-256. The archive is read
in memory and never extracted or executed.
"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PIN = ROOT / "packaging/iroh-apple/release.json"
POLICY = ROOT / "packaging/iroh-apple/license-sources.json"
COLLECTOR = ROOT / "scripts/cargo_notices.py"
SUPPORT_NAME = "IrohLib-support.zip"
OUTPUT_NAME = "licenses.json"
WEB_NAME = "licenses-web.json"
SCHEMA_VERSION = 1

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
RELEASE_URL = re.compile(r"https://github\.com/avibe-bot/avibe/releases/download/iroh-apple-v[0-9.]+-[1-9][0-9]*/[A-Za-z0-9._-]+")
REGISTRY_URL = re.compile(r"https://static\.crates\.io/crates/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)-([0-9A-Za-z.+-]+)\.crate")
UPSTREAM_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/tree/[0-9a-f]{40}")
# Members that describe the collection rather than one component's notice.
INDEX_FILES = {"LICENSES/packages.json", "LICENSES/provenance.json", "LICENSES/NOTICE.md"}
BINDING_STATEMENT = "Generated IrohLib.swift binding"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_pin(path: Path = PIN) -> dict:
    pin = json.loads(path.read_text())
    require(pin.get("schema_version") == 1, "unknown release pin schema")
    require(pin.get("release_state") in {"pending", "published"}, "unknown release pin state")
    return pin


def native_state(pin: dict) -> dict:
    """What the page data claims about the native binary, derived from the pin only."""
    if pin["release_state"] != "published":
        return {"state": "pending", "release_tag": pin["release_tag"]}
    asset = pin["assets"].get(SUPPORT_NAME) or {}
    return {"state": "published", "release_tag": pin["release_tag"], "binary_sha256": pin["sha256"],
            "support_sha256": asset.get("sha256")}


def check_published_pin(pin: dict) -> dict:
    """The pin's own fields that bind the support bundle to the reviewed release."""
    require(pin["release_state"] == "published", "the iroh release pin is pending; its notices are not consumed")
    require(isinstance(pin.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) is not None,
            "published pin has no binary checksum")
    asset = pin.get("assets", {}).get(SUPPORT_NAME)
    require(isinstance(asset, dict), f"published pin does not name {SUPPORT_NAME}")
    require(RELEASE_URL.fullmatch(asset.get("url", "")) is not None
            and asset["url"].endswith(f"/{pin['release_tag']}/{SUPPORT_NAME}"), "support asset URL is not the pinned release")
    require(re.fullmatch(r"[0-9a-f]{64}", asset.get("sha256", "")) is not None, "support asset has no checksum")
    require(isinstance(asset.get("size"), int) and 0 < asset["size"] <= MAX_ARCHIVE_BYTES, "support asset size is invalid")
    require(pin.get("license_policy_sha256") == sha256(POLICY.read_bytes()),
            "the pinned release used a different license policy than this checkout")
    require(pin.get("notice_collector_sha256") == sha256(COLLECTOR.read_bytes()),
            "the pinned release used a different notice collector than this checkout")
    return asset


def download(asset: dict) -> bytes:
    with urllib.request.urlopen(asset["url"], timeout=60) as response:  # noqa: S310 - pinned https URL
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    return data


def open_archive(data: bytes, asset: dict) -> dict[str, bytes]:
    """Every member's bytes, after size, checksum and member-name safety checks."""
    require(len(data) == asset["size"], "support asset size does not match the pin")
    require(sha256(data) == asset["sha256"], "support asset checksum does not match the pin")
    members: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        require(len(infos) <= MAX_MEMBERS, "support archive has too many members")
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            path = PurePosixPath(name)
            require(not path.is_absolute() and "\\" not in name and ".." not in path.parts
                    and all(part not in {"", "."} for part in path.parts), f"unsafe archive member: {name!r}")
            require(not stat.S_ISLNK(info.external_attr >> 16), f"archive member is a symlink: {name}")
            require(name not in members, f"duplicate archive member: {name}")
            require(info.file_size <= MAX_MEMBER_BYTES, f"archive member is too large: {name}")
            total += info.file_size
            require(total <= MAX_TOTAL_BYTES, "support archive expands too far")
            content = archive.read(info)
            require(len(content) == info.file_size, f"archive member size mismatch: {name}")
            members[name] = content
    return members


def decode(name: str, content: bytes) -> str:
    require(bool(content.strip()), f"empty notice: {name}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"notice is not UTF-8 text: {name}") from error
    if name.lower().endswith((".html", ".htm")):
        text = html_text(text)
    # Line endings and trailing spaces only; words, terms and copyright lines are untouched.
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


class _HtmlText(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "tr", "table", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.skip += 1
        if tag == "pre":
            self.pre += 1
        if tag in self.BLOCKS:
            self.parts.append("\n\n" if tag != "br" and tag != "li" else "\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.skip = max(0, self.skip - 1)
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_text(markup: str) -> str:
    parser = _HtmlText()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts)
    text = "\n".join(line.strip() if not line.startswith(" ") else line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


SPDX_NAMES = (
    (re.compile(r"(?i)apache"), "Apache-2.0"),
    (re.compile(r"(?i)\bmit\b|[-_.]mit$"), "MIT"),
    (re.compile(r"(?i)bsd"), "BSD"),
    (re.compile(r"(?i)\bisc\b"), "ISC"),
    (re.compile(r"(?i)zlib"), "Zlib"),
    (re.compile(r"(?i)unicode"), "Unicode"),
    (re.compile(r"(?i)mpl|mozilla"), "MPL-2.0"),
    (re.compile(r"(?i)cc0"), "CC0-1.0"),
)


def title_for(name: str, text: str, declared: str | None = None) -> str:
    """A label for display only; grouping never uses it."""
    base = PurePosixPath(name).name
    stem = re.sub(r"(?i)^(licen[cs]e|copying)[._-]?", "", PurePosixPath(base).stem)
    for pattern, label in SPDX_NAMES:
        if stem and pattern.search(stem):
            return label
    if re.fullmatch(r"(?i)licen[cs]e(\.(md|txt))?", base) and declared and re.fullmatch(r"[A-Za-z0-9.+-]+", declared):
        return declared
    if re.match(r"(?i)^notice", base):
        return "NOTICE"
    if re.match(r"(?i)^copyright", base):
        return "COPYRIGHT"
    first = next((line.strip() for line in text.split("\n") if line.strip()), "")
    if 0 < len(first) <= 60 and not first.lower().startswith(("copyright", "permission")):
        return first.strip("#= ").strip() or base
    return base


class Texts:
    """Groups by the exact text (after line-ending normalization); labels never merge groups."""

    def __init__(self) -> None:
        self.groups: dict[str, dict] = {}

    def add(self, component: dict, name: str, content: bytes, declared: str | None = None) -> None:
        text = decode(name, content)
        group = self.groups.setdefault(sha256(text.encode()),
                                       {"title": title_for(name, text, declared), "text": text, "components": []})
        if component not in group["components"]:
            group["components"].append(component)

    def result(self) -> list[dict]:
        groups = sorted(self.groups.values(), key=lambda g: (-len(g["components"]), g["title"], g["text"]))
        for group in groups:
            group["components"].sort(key=lambda c: (c["name"], c["version"], c["origin"]))
        return groups


def native_notices(pin: dict, members: dict[str, bytes], texts: Texts) -> list[dict]:
    """Adds a verified support archive's notices; returns its MPL-2.0 source offer. Fails on anything unaccounted for."""
    inventory = json.loads(members.get("LICENSES/packages.json", b"null"))
    provenance = json.loads(members.get("LICENSES/provenance.json", b"null"))
    notice = decode("LICENSES/NOTICE.md", members.get("LICENSES/NOTICE.md", b""))
    require(isinstance(inventory, list) and inventory, "missing package inventory")
    require(isinstance(provenance, dict) and provenance.get("packages") == inventory, "inventory/provenance mismatch")
    require(provenance.get("policy_sha256") == pin["license_policy_sha256"], "notice policy differs from the pin")
    require("IrohLib.swift" in members, "missing generated binding")
    require(BINDING_STATEMENT in notice, "missing generated binding source statement")
    used: set[str] = set()

    def add(component: dict, name: str, declared: str | None = None) -> None:
        require(name in members, f"missing notice file: {name}")
        texts.add({**component, "origin": "native"}, name, members[name], declared)
        used.add(name)

    selected_mpl = set()
    for package in inventory:
        require(isinstance(package.get("name"), str) and isinstance(package.get("version"), str), "invalid package entry")
        files = [name for name in package.get("files", []) if not name.endswith("/Cargo.toml")]
        require(bool(files), f"no notice for {package['name']} {package['version']}")
        component = {"name": package["name"], "version": package["version"]}
        if package.get("class") == "standard":
            # Canonical texts name no holder; keep the crate's own declared authors with it.
            component["authors"] = list(package.get("authors") or [])
        for name in files:
            add(component, name, package.get("license"))
        used.update(name for name in package.get("files", []) if name.endswith("/Cargo.toml"))
        selected = package.get("selected_license") or ""
        # The collector offers source only for a plain MPL-2.0 selection; anything else needs a review.
        require(selected == "MPL-2.0" or "MPL" not in selected,
                f"MPL in a compound selection needs a reviewed source offer: {package['name']} {selected}")
        if selected == "MPL-2.0":
            selected_mpl.add((package["name"], package["version"]))

    root = next(({"name": p["name"], "version": p["version"]} for p in inventory if p["name"] == "iroh-ffi"),
                {"name": "iroh-ffi", "version": pin["release_tag"]})
    for name in sorted(members):
        match = re.fullmatch(r"LICENSES/(iroh-ffi|rust-([0-9][0-9A-Za-z.+-]*))/(.+)", name)
        if match and name not in used:
            component = root if match.group(1) == "iroh-ffi" else {"name": "Rust", "version": match.group(2)}
            add(component, name)
    for required in ("LICENSES/iroh-ffi/LICENSE-APACHE", "LICENSES/iroh-ffi/LICENSE-MIT"):
        require(required in used, f"missing upstream license: {required}")
    rust = [name for name in used if re.fullmatch(r"LICENSES/rust-[^/]+/COPYRIGHT\.html", name)]
    require(len(rust) == 1, "missing Rust distribution notice")
    # The collection-wide statements, source offer and binding note included.
    add(root, "LICENSES/NOTICE.md")
    unknown = sorted(set(members) - used - INDEX_FILES - {"IrohLib.swift"})
    require(not unknown, "unaccounted archive members: " + ", ".join(unknown))

    sources: dict[tuple, dict] = {}
    offered = set()
    for item in provenance.get("mpl_sources", []):
        name, version = item.get("name"), item.get("version")
        registry, upstream = item.get("registry_url", ""), item.get("upstream_url", "")
        match = REGISTRY_URL.fullmatch(registry)
        require(match is not None and match.groups() == (name, name, version), f"invalid registry source for {name}")
        require(UPSTREAM_URL.fullmatch(upstream) is not None, f"invalid upstream source for {name}")
        require(item.get("modification_status") == "unmodified", f"MPL source is not marked unmodified: {name}")
        require(registry in notice and upstream in notice, f"NOTICE.md omits the source offer for {name}")
        offered.add((name, version))
        line = sources.setdefault((version, upstream), {"version": version, "upstream": upstream, "crates": []})
        line["crates"].append({"name": name, "registry": registry})
    require(offered == selected_mpl, "MPL source offer does not match the MPL-2.0 packages")
    require(("uniffi_bindgen" in {name for name, _ in offered}), "missing the generator's source offer")
    for line in sources.values():
        line["crates"].sort(key=lambda c: c["name"])
    return sorted(sources.values(), key=lambda s: (len(s["crates"]), s["crates"][0]["name"]))


def web_notices(data: bytes, texts: Texts) -> int:
    """Adds the bundled JavaScript's notices (ui/scripts/bundleNotices.mjs); returns its package count."""
    try:
        web = json.loads(data)
    except json.JSONDecodeError as error:
        raise ValueError(f"{WEB_NAME} is not JSON") from error
    require(isinstance(web, dict) and web.get("schema_version") == 1, f"{WEB_NAME} has an unknown schema")
    packages = web.get("packages")
    require(isinstance(packages, list) and packages, f"{WEB_NAME} lists no bundled packages")
    for package in packages:
        name, version, files = package.get("name"), package.get("version"), package.get("files")
        require(isinstance(name, str) and isinstance(version, str), f"invalid {WEB_NAME} package entry")
        require(isinstance(files, list) and files, f"bundled package {name}@{version} has no notice")
        for file in files:
            require(isinstance(file.get("name"), str) and isinstance(file.get("text"), str),
                    f"invalid notice entry for {name}@{version}")
            texts.add({"name": name, "version": version, "origin": "web"}, f"{name}/{file['name']}",
                      file["text"].encode(), package.get("license") if isinstance(package.get("license"), str) else None)
    return len(packages)


def assemble(pin: dict, texts: Texts, sources: list[dict], web: bytes, web_packages: int) -> dict:
    groups = texts.result()
    return {
        "schema_version": SCHEMA_VERSION,
        "native": native_state(pin),
        "web": {"sha256": sha256(web), "packages": web_packages},
        "components": len({(c["name"], c["version"], c["origin"]) for g in groups for c in g["components"]}),
        "sources": sources,
        "groups": groups,
    }


def generate(pin: dict, support_zip: Path | None, web: bytes) -> dict:
    """The page data: the bundle's JavaScript notices, plus the native ones when the pin is published."""
    texts = Texts()
    web_packages = web_notices(web, texts)
    sources: list[dict] = []
    if pin["release_state"] == "published":
        asset = check_published_pin(pin)
        data = support_zip.read_bytes() if support_zip else download(asset)
        sources = native_notices(pin, open_archive(data, asset), texts)
    else:
        # Pending: iroh is neither consumed nor linked, so the App carries no native notices.
        require(support_zip is None, "a pending pin cannot consume a support archive")
    return assemble(pin, texts, sources, web, web_packages)


def encode(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()


def verify(pin: dict, bundle: Path) -> None:
    """The bundled data was generated for this pin and this bundle, not left over from another."""
    path, web = bundle / OUTPUT_NAME, bundle / WEB_NAME
    require(path.is_file(), f"missing {OUTPUT_NAME}; run ios/scripts/app_licenses.py generate")
    require(web.is_file(), f"missing {WEB_NAME}; run 'npm run build:mobile' first")
    data = json.loads(path.read_text())
    require(data.get("schema_version") == SCHEMA_VERSION, f"{OUTPUT_NAME} has an unknown schema")
    require(data.get("native") == native_state(pin), f"{OUTPUT_NAME} was generated for a different iroh pin")
    require((data.get("web") or {}).get("sha256") == sha256(web.read_bytes()),
            f"{OUTPUT_NAME} was generated from a different bundle")
    groups = data.get("groups") or []
    origins = {c.get("origin") for g in groups for c in g.get("components", [])}
    require("web" in origins, f"{OUTPUT_NAME} has no notices for the bundled JavaScript")
    if pin["release_state"] == "published":
        require("native" in origins and bool(data.get("sources")), f"{OUTPUT_NAME} has no native notices")
    else:
        require(origins == {"web"} and data.get("sources") == [],
                f"{OUTPUT_NAME} carries native notices while the iroh pin is pending")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("generate", "verify"))
    parser.add_argument("--bundle", type=Path, required=True, help="the bundled mobile UI directory")
    parser.add_argument("--pin", type=Path, default=PIN)
    parser.add_argument("--support-zip", type=Path, help="a local copy of the pinned support asset")
    args = parser.parse_args(argv)
    pin = load_pin(args.pin)
    output = args.bundle / OUTPUT_NAME
    try:
        if args.command == "generate":
            require(args.bundle.is_dir(), f"missing bundle directory: {args.bundle}")
            web = args.bundle / WEB_NAME
            require(web.is_file(), f"missing {web}; run 'npm run build:mobile' first")
            output.write_bytes(encode(generate(pin, args.support_zip, web.read_bytes())))
        verify(pin, args.bundle)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{output}: native notices {pin['release_state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
