"""DOM selectors from the integrated UpdatePanel/AccountApp contract.

Baseline UI inspected at 216ea93bec9ef04cc5dacfa4e53097ae7a3c2d24; integrated
contract at ac20a5be3411b1654913632c69f8cb1a7ffd2ac2. The actual checked-out
catalogs supply labels, not a duplicate English test UI or mocked IPC.
"""
import json
from pathlib import Path


class Panel:
    def __init__(self, case, source: Path):
        self.case = case
        self.copies = [json.loads((source / f"ui/src/i18n/{lang}.json").read_text(encoding="utf-8"))["update"]
                       for lang in ("en", "zh")]

    def snapshot(self):
        return self.case.js("""
const p = document.querySelector('[data-update-panel]');
return p ? {status:p.dataset.updateStatus, text:p.innerText,
  switches:Array.from(p.querySelectorAll('[role=switch]')).map(e=>({label:e.getAttribute('aria-label'), checked:e.getAttribute('aria-checked'), disabled:e.disabled})),
  width:p.getBoundingClientRect().width, height:p.getBoundingClientRect().height,
  bridge:Boolean(window.__TAURI_INTERNALS__), origin:location.origin, url:location.href} : null;
""")

    def click(self, kind: str) -> None:
        if kind == "check":
            labels = [copy["action"]["check"] for copy in self.copies]
            match = f"{json.dumps(labels)}.includes(e.getAttribute('aria-label'))"
        elif kind == "install":
            labels = [copy["action"]["update"] for copy in self.copies]
            match = f"{json.dumps(labels)}.includes(e.textContent.trim())"
        else:
            raise ValueError(kind)
        result = self.case.js("""
const p = document.querySelector('[data-update-panel]');
const matches = Array.from(p.querySelectorAll('button')).filter(e => """ + match + """);
if(matches.length !== 1 || matches[0].disabled) throw new Error('Expected one enabled panel control');
const e=matches[0], before=p.innerText; e.click(); return {clicked:true, before};
""")
        assert result["clicked"]

    def switch(self, name: str, enabled: bool) -> None:
        labels = [copy["setting"][name] for copy in self.copies]
        script = """
const p=document.querySelector('[data-update-panel]');
const matches=Array.from(p.querySelectorAll('button[role=switch]')).filter(e=>LABELS.includes(e.getAttribute('aria-label')));
if(matches.length!==1 || matches[0].disabled) throw new Error('Expected one enabled panel switch');
const e=matches[0]; if((e.getAttribute('aria-checked')==='true')===DESIRED) throw new Error('Switch already has requested state');
e.click(); return true;
""".replace("LABELS", json.dumps(labels)).replace("DESIRED", json.dumps(enabled))
        assert self.case.js(script) is True
        self.case.wait(lambda: self.checked(name) is enabled, 15, "panel switch did not acknowledge")

    def checked(self, name: str):
        value = self.snapshot()
        if not value:
            return None
        labels = [copy["setting"][name] for copy in self.copies]
        items = [item for item in value["switches"] if item["label"] in labels]
        return items[0]["checked"] == "true" if len(items) == 1 else None
