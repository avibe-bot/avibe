#!/usr/bin/env python3
"""Pair the screenshots of two SDK builds and write a side-by-side page.

Each side is an `xcresulttool export attachments` output tree (one directory
per scheme, each with its `manifest.json`). Screens are paired by attachment
name; each pair gets a diff image and the share of pixels that changed.
Writes `index.html`, `summary.json`, and the images under `--output`.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path

from PIL import Image, ImageChops

# `xcresulttool` adds `_<index>_<UUID>` to the name it was given.
SUFFIX = re.compile(r"_\d+_[0-9A-Fa-f-]{36}$")


def captures(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for manifest in sorted(root.rglob("manifest.json")):
        for test in json.loads(manifest.read_text()):
            for item in test.get("attachments", []):
                exported = manifest.parent / item["exportedFileName"]
                if exported.suffix.lower() not in {".png", ".jpg", ".jpeg", ".heic"}:
                    continue
                name = SUFFIX.sub("", Path(item.get("suggestedHumanReadableName", exported.name)).stem)
                found[name] = exported
    return found


def changed_share(before: Path, after: Path, diff_path: Path) -> tuple[float, str]:
    first = Image.open(before).convert("RGB")
    second = Image.open(after).convert("RGB")
    if first.size != second.size:
        return 1.0, f"size {first.size} → {second.size}"
    diff = ImageChops.difference(first, second)
    mask = diff.convert("L").point(lambda value: 255 if value > 16 else 0)
    changed = mask.histogram()[255] / (first.size[0] * first.size[1])
    if changed:
        highlight = Image.composite(Image.new("RGB", first.size, (255, 0, 64)), first.point(lambda v: v // 3), mask)
        highlight.save(diff_path)
    return changed, ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before-label", default="Xcode 26.6 (iOS 26.5 SDK)")
    parser.add_argument("--after-label", default="Xcode 27.2 (iOS 27.2 SDK)")
    args = parser.parse_args()

    before, after = captures(args.before), captures(args.after)
    images = args.output / "images"
    images.mkdir(parents=True, exist_ok=True)
    rows, summary = [], []
    for name in sorted(set(before) | set(after)):
        entry = {"name": name, "before": name in before, "after": name in after}
        cells = []
        for side, found in (("before", before), ("after", after)):
            if name in found:
                target = images / f"{name}.{side}.png"
                shutil.copy(found[name], target)
                cells.append(f'<img loading="lazy" src="images/{target.name}">')
            else:
                cells.append("<em>missing</em>")
        if name in before and name in after:
            share, note = changed_share(before[name], after[name], images / f"{name}.diff.png")
            entry.update(changed=round(share, 5), note=note)
            diff_file = images / f"{name}.diff.png"
            cells.append(f'<img loading="lazy" src="images/{diff_file.name}">' if diff_file.exists() else "identical")
        else:
            cells.append("")
        summary.append(entry)
        changed = entry.get("changed")
        flag = "same" if changed == 0 else ("missing" if changed is None else f"{changed:.2%} changed {entry.get('note', '')}")
        rows.append(
            f'<tr class="{"diff" if changed else "same"}"><th>{html.escape(name)}<br><small>{html.escape(flag)}</small></th>'
            + "".join(f"<td>{cell}</td>" for cell in cells)
            + "</tr>"
        )
    page = f"""<!doctype html><meta charset="utf-8"><title>iOS SDK comparison</title>
<style>body{{font:14px -apple-system,sans-serif;margin:16px}}td img{{width:260px}}th{{text-align:left;vertical-align:top;width:220px}}
tr.diff th{{color:#c0003a}}label{{margin-left:12px}}</style>
<h1>{html.escape(args.before_label)} vs {html.escape(args.after_label)}, same iOS 27.2 simulator</h1>
<p>{sum(1 for e in summary if e.get('changed'))} of {len(summary)} screens differ. Fixture data only.
<label><input type=checkbox onchange="document.querySelectorAll('tr.same').forEach(r=>r.hidden=this.checked)"> only differences</label></p>
<table><tr><th>screen</th><th>{html.escape(args.before_label)}</th><th>{html.escape(args.after_label)}</th><th>changed pixels</th></tr>
{''.join(rows)}</table>"""
    (args.output / "index.html").write_text(page)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"screens": len(summary), "differ": sum(1 for e in summary if e.get("changed"))}))


if __name__ == "__main__":
    main()
