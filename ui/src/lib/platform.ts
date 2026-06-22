// Shared device / display-context detection. An iOS Home-Screen PWA has no
// browser chrome (no address bar, no back button), so a same-origin top-level
// navigation — e.g. a plain ``<a download>`` to our media proxy — has no way
// back and traps the app on the file preview. Callers that would otherwise
// navigate use these checks to pick a non-trapping path instead. Kept in one
// place so the detection can't drift between the InstallHint nudge and the
// media-download helper.

// iPhone / iPad / iPod, plus iPadOS 13+ which reports as desktop "MacIntel"
// while still exposing multi-touch.
export function isIosDevice(): boolean {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent || '';
  return (
    /iP(hone|ad|od)/.test(ua) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  );
}

// Launched from the Home Screen / installed as a PWA: no browser chrome.
// ``navigator.standalone`` is Apple's proprietary signal; the display-mode
// media query is the cross-browser one.
export function isStandalonePwa(): boolean {
  if (typeof window === 'undefined') return false;
  return (
    (navigator as unknown as { standalone?: boolean }).standalone === true ||
    (!!window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
  );
}

// iOS in-app webviews (IM/social apps) and alternative browsers (Chrome/Firefox
// on iOS) can't reach Safari's "Add to Home Screen" page-share flow, so callers
// that nudge toward it must exclude them. Real Safari carries both a "Version/<n>"
// and a "Safari" token, which WKWebView-based in-app browsers and CriOS/FxiOS/etc.
// lack. Shared so the InstallHint nudge and the Show Page share hint agree.
const NON_SAFARI_IOS_UA = /CriOS|FxiOS|EdgiOS|OPiOS|mercury/i;
const IN_APP_BROWSER_UA =
  /MicroMessenger|FBAN|FBAV|FB_IAB|Instagram|Line\/|Twitter|WhatsApp|Snapchat|DingTalk|QQ\/|QQTheme|Slack|Discord|Feishu|Lark|; wv\)/i;

export function isRealMobileSafari(): boolean {
  if (!isIosDevice()) return false;
  const ua = navigator.userAgent || '';
  return (
    /Safari/.test(ua) &&
    /Version\//.test(ua) &&
    !NON_SAFARI_IOS_UA.test(ua) &&
    !IN_APP_BROWSER_UA.test(ua)
  );
}
