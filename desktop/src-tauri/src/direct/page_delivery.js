(origin, asker, message, keep) => {
  // What the shell delivers to a window of another computer's instance
  // (`bridge.rs`): to the document of `origin` in the window's top frame,
  // and to no other document. A navigation can commit between the shell's
  // look at the window and this script's run.
  if (window.self !== window.top || window.location.origin !== origin) return;
  // An answer is for the document that asked, which marked itself when its
  // bridge started (`page_bridge.js`). A document that has taken its place
  // since (a reload, a navigation within the instance) has another mark, or
  // none: the answer is handed to nothing of it.
  if (asker !== null && window.__AVIBE_DIRECT_DOCUMENT__ !== asker) return;
  const deliver = window.__AVIBE_DIRECT_DELIVER__;
  if (typeof deliver === 'function') deliver(message);
  // A state that arrives before the bridge is kept for it; an answer has no
  // one to wait for.
  else if (keep) window.__AVIBE_DIRECT_EARLY__ = message;
}
