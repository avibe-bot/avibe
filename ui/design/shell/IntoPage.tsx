import { useEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/**
 * Draws a proposed element inside a shipped surface, at every element `selector` matches, once
 * the surface has rendered them.
 *
 * A shipped component that mixes data hooks and markup can only be drawn whole (ui/design/AGENTS.md),
 * so a proposal for one new element in it has no place to stand. This puts the kit element where it
 * would go, as the last child of each match, so the frame shows the real surface around it. It is for
 * `proposed` frames only: when the element ships, the frame renders the shipped component and this
 * wrapper goes away.
 */
export function IntoPage({ selector, children }: { selector: string; children: ReactNode }) {
  const [hosts, setHosts] = useState<HTMLElement[]>([]);
  useEffect(() => {
    const made = new Map<Element, HTMLElement>();
    const sync = () => {
      let changed = false;
      for (const target of document.querySelectorAll(selector)) {
        if (made.has(target)) continue;
        const host = document.createElement('div');
        host.dataset.designProposal = '';
        target.appendChild(host);
        made.set(target, host);
        changed = true;
      }
      if (changed) setHosts([...made.values()]);
    };
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      for (const host of made.values()) host.remove();
    };
  }, [selector]);
  return <>{hosts.map((host, index) => createPortal(children, host, String(index)))}</>;
}
