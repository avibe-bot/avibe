import AVFoundation
import UIKit
import WebKit

/// A page in a WebView set up as the instance WebView is for hold-to-talk:
/// the production bridge, the production touch observer
/// (`NativeHoldTouchObserver`) and the production hold capture
/// (`NativeHoldCapture.shared`). The page is a stand-in for the Workbench that
/// speaks the same protocol (`voice.holdSurfaces`, `avibe:native:voice.hold`,
/// `voice.holdAdopt`, `voice.captureStop`). XCUITest then makes real touches,
/// so what is under test is the claim no unit test can make: that the App's
/// observer cancels and delays nothing the page does with a touch.
///
/// The Simulator has no usable microphone on CI: a hold there ends as the
/// App's `failed` and the page's own failed start. Both are clean ends.
@MainActor
final class FixtureHoldViewController: UIViewController {
    static let origin = "https://current.example.invalid"

    private var webView: WKWebView?
    private var evidenceTimer: Timer?
    /// What the App kept of the touches it followed (`NativeHoldTimings`):
    /// proof, read by the tests, that the observer saw them at all.
    private let evidence = UIView()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        // Each launch starts with nothing kept.
        UserDefaults.standard.removeObject(forKey: NativeHoldTimings.key)
        UserDefaults.standard.removeObject(forKey: NativeHoldTaps.key)
        // The App follows a press only with the microphone allowed. CI allows
        // it before the run (`simctl privacy`); elsewhere the system asks
        // here, and the tests answer.
        AVAudioApplication.requestRecordPermission { _ in }

        guard let origin = WebOrigin(scheme: "https", host: "current.example.invalid", port: nil) else {
            preconditionFailure("The fixed fixture origin must be valid")
        }
        let webView = WKWebView(
            frame: view.bounds,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: origin,
                metadata: NativeShellMetadata(bundle: .main),
                setOuterScrollLocked: { _, _ in true },
                instanceName: { "Studio" }
            )
        )
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        // As `WebViewController` sets its primary WebView up.
        webView.addGestureRecognizer(NativeHoldTouchObserver())
        NativeHoldCapture.shared.watch(webView)
        // The fixture's own record of each touch as UIKit delivers it, beside
        // the production observer: when a check fails, it says whether the
        // touch reached the App late, without its moves, or not at all.
        webView.addGestureRecognizer(touchLog)
        view.addSubview(webView)
        self.webView = webView

        evidence.isAccessibilityElement = true
        evidence.accessibilityIdentifier = "fixture-hold-evidence"
        evidence.isUserInteractionEnabled = false
        evidence.frame = CGRect(x: 0, y: 0, width: 1, height: 1)
        view.addSubview(evidence)
        publishEvidence()
        evidenceTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.publishEvidence() }
        }

        webView.loadHTMLString(Self.page, baseURL: URL(string: "\(Self.origin)/hold.html")!)
    }

    private let touchLog = FixtureHoldTouchLog()

    private func publishEvidence() {
        let lines = NativeHoldTimings.lines
        evidence.accessibilityValue = (lines.isEmpty ? "none" : lines.joined(separator: " | "))
            + " || touches: " + (touchLog.touches.isEmpty ? "none" : touchLog.touches.joined(separator: " "))
    }

    /// The stand-in page. Its states are shown as text, one fact a line, for
    /// the tests to read: `state=…`, `surfaces=…`, `events=…`, and counters.
    private static let page = #"""
    <!doctype html>
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
    <meta name="avibe-voice-consent" content="1">
    <style>
      html, body { margin: 0; font: 15px -apple-system, sans-serif; }
      body { padding: 70px 16px 0; }
      p { margin: 2px 0; }
      #row { display: flex; gap: 8px; margin: 12px 0; }
      #input { flex: 1; height: 56px; font-size: 16px; }
      /* As the composer while a hold may start on it (ui: Composer `holdBoxStyle`). */
      #input.holdable { touch-action: none; -webkit-touch-callout: none; }
      button { min-width: 64px; min-height: 44px; font-size: 15px; }
      #plain, #control { margin: 12px 0; height: 64px; background: #dde; display: flex; align-items: center; justify-content: center; }
      #control { background: #ded; }
      #filler { height: 1600px; background: linear-gradient(#eef, #fee); }
      #sheet { position: fixed; left: 0; right: 0; top: 60px; bottom: 0; background: #fff; border-top: 2px solid #888; padding: 16px; display: none; }
      #sheet.open { display: block; }
      #sheet-body { height: 100%; }
    </style>
    </head>
    <body>
      <p id="s-ready">ready=no</p>
      <p id="s-surfaces">surfaces=none</p>
      <p id="s-state">state=idle</p>
      <p id="s-events">events=</p>
      <p id="s-holds">holds=0:none</p>
      <p id="s-focus">focus=no</p>
      <p id="s-typed">typed=0</p>
      <p id="s-send">send=0/0</p>
      <p id="s-sheet">sheet=0</p>
      <p id="s-scroll">scrolled=no</p>
      <p id="s-warm">warm=no</p>
      <p id="s-press">press=none</p>
      <p id="s-drag">drag=none</p>
      <div id="row">
        <textarea id="input" class="holdable" aria-label="Message"></textarea>
        <button id="send" type="button">Send</button>
      </div>
      <button id="open-sheet" type="button">Open sheet</button>
      <button id="blur" type="button">Hide keyboard</button>
      <div id="plain">Plain surface</div>
      <div id="control">Control box</div>
      <div id="filler"></div>
      <div id="sheet"><div id="sheet-body"><button id="close-sheet" type="button">Close sheet</button><p>Sheet content</p></div></div>
    <script>
    (() => {
      const HOLD_MS = 200, GRACE_MS = 100, SLOP = 10, CANCEL = 80, UNCANCEL = 60;
      const native = window.avibeNative;
      const input = document.getElementById("input");
      const plain = document.getElementById("plain");
      const sheet = document.getElementById("sheet");
      const set = (id, text) => { document.getElementById(id).textContent = text; };
      const events = [];
      const note = (phase) => { events.push(phase); set("s-events", "events=" + events.join(",")); };
      let holds = 0;
      const state = (next) => set("s-state", "state=" + next);
      const stop = (take) => native.call("voice.captureStop", { take }).catch(() => {});

      // Where a hold may record now. The input, as a composer: not while it is focused, and
      // not under the sheet. The plain box is named but never held by the page: the App's
      // recognition there is refused, and a drag on it scrolls the page.
      const holdable = () => !sheet.classList.contains("open") && document.activeElement !== input;
      let last = "";
      const name = () => {
        input.classList.toggle("holdable", holdable());
        const viewport = window.visualViewport;
        const dx = viewport ? viewport.offsetLeft : 0, dy = viewport ? viewport.offsetTop : 0;
        const rect = (id, element) => {
          const box = element.getBoundingClientRect();
          return { id, x: box.left - dx, y: box.top - dy, width: box.width, height: box.height };
        };
        const surfaces = sheet.classList.contains("open") ? [] : [
          ...(holdable() ? [rect("input", input)] : []),
          rect("plain", plain),
        ];
        const next = JSON.stringify(surfaces);
        if (next === last) return;
        last = next;
        native.call("voice.holdSurfaces", surfaces.length ? { surfaces, holdMs: HOLD_MS } : { surfaces }).then(
          () => set("s-surfaces", "surfaces=" + (surfaces.length ? surfaces.map((surface) => surface.id).join("+") : "none")),
          (error) => {
            const why = error && error.code === "failed" ? error.message : "error";
            if (why !== "superseded") set("s-surfaces", "surfaces=" + why);
          },
        );
      };
      window.addEventListener("scroll", () => { if (window.scrollY > 20) set("s-scroll", "scrolled=yes"); name(); }, { passive: true });
      window.addEventListener("resize", name);
      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", name);
        window.visualViewport.addEventListener("scroll", name);
      }

      // What the page saw of a drag on a box: its pointer events, in order.
      for (const box of [plain, document.getElementById("control")]) {
        let seen = [], moves = 0;
        const show = () => set("s-drag", "drag=" + box.id + ":" + seen.join(",") + (moves ? ",move" + moves : ""));
        box.addEventListener("pointerdown", () => { seen = ["down"]; moves = 0; show(); });
        box.addEventListener("pointermove", () => { moves += 1; show(); });
        box.addEventListener("pointerup", () => { seen.push("up"); show(); });
        box.addEventListener("pointercancel", () => { seen.push("cancel"); show(); });
      }
      // The first touch after a launch is slow on a CI simulator: the tests warm up on this.
      document.getElementById("s-ready").addEventListener("pointerup", () => set("s-warm", "warm=yes"));

      // One press on the input, and the take it becomes.
      let press = null;
      // How long the page saw the last press on the input, and how it ended.
      const pressed = (how) => set("s-press", "press=" + how + ":" + Math.round(performance.now() - press.at));
      // As the composer: a press the hold took keeps iOS from focusing or selecting, and a
      // tap focuses in touchend, where iOS allows it.
      let touchHeld = false, tapFocus = false;
      const drop = () => { clearTimeout(press.timer); press = null; state("idle"); };
      const end = (outcome) => {
        if (!press) return;
        clearTimeout(press.timer);
        if (press.take) stop(press.take);
        press = null;
        holds += 1;
        set("s-holds", "holds=" + holds + ":" + outcome);
        state("idle");
        name();
      };
      // The page's own recognition, the fallback: its own start.
      const ownStart = () => {
        if (!press || press.take || press.own) return;
        press.own = true;
        state("own-start");
        const take = "p" + Date.now().toString(36);
        const mine = press;
        native.call("voice.captureStart", { take }).then(
          () => { if (press === mine) { mine.take = take; state("recording"); } else stop(take); },
          () => { if (press === mine) { mine.failed = true; state("own-failed"); } },
        );
      };
      input.addEventListener("pointerdown", (event) => {
        touchHeld = false;
        if (press || !holdable()) return;
        touchHeld = true;
        press = { id: event.pointerId, x: event.clientX, y: event.clientY, at: performance.now(),
          take: null, own: false, armed: false, failed: false, recognised: false,
          timer: setTimeout(ownStart, HOLD_MS + GRACE_MS) };
        state("pressing");
      });
      input.addEventListener("touchstart", (event) => { if (touchHeld) event.preventDefault(); }, { passive: false });
      input.addEventListener("touchend", () => {
        touchHeld = false;
        if (!tapFocus) return;
        tapFocus = false;
        input.focus();
      });
      input.addEventListener("pointermove", (event) => {
        if (!press || event.pointerId !== press.id) return;
        if (!press.recognised && !press.own) {
          if (Math.hypot(event.clientX - press.x, event.clientY - press.y) > SLOP) drop();
          return;
        }
        const up = press.y - event.clientY;
        if (!press.armed && up > CANCEL) { press.armed = true; state("armed"); }
        else if (press.armed && up <= UNCANCEL) { press.armed = false; state(press.failed ? "own-failed" : "recording"); }
      });
      input.addEventListener("pointerup", (event) => {
        if (!press || event.pointerId !== press.id) return;
        pressed("up");
        if (!press.recognised && !press.own) {
          // A tap: the input takes the focus.
          drop();
          tapFocus = true;
          return;
        }
        end(press.armed ? "cancelled" : press.failed ? "failed" : press.take ? "sent" : "early");
      });
      input.addEventListener("pointercancel", (event) => {
        if (!press || event.pointerId !== press.id) return;
        pressed("cancel");
        if (press.recognised || press.own) end("system");
        else drop();
      });
      window.addEventListener("avibe:native:voice.hold", (event) => {
        const detail = event.detail || {};
        note(detail.phase);
        if (detail.phase === "recognised") {
          if (!press || press.own || press.take || press.recognised || detail.surface !== "input") {
            native.call("voice.holdReject", { take: detail.take }).catch(() => {});
            note("refused");
            return;
          }
          clearTimeout(press.timer);
          press.recognised = true;
          state("recording");
          const mine = press;
          native.call("voice.holdAdopt", { take: detail.take }).then(
            () => { if (press === mine) mine.take = detail.take; else stop(detail.take); },
            // The App no longer has it: the page's own start, with the finger still down.
            () => { note("gone"); if (press === mine) ownStart(); },
          );
        } else if (detail.phase === "failed") {
          // The App is not following this press: the page recognises it itself, at its own time.
          if (press && !press.own && !press.take && !press.recognised) {
            clearTimeout(press.timer);
            press.timer = setTimeout(ownStart, Math.max(0, HOLD_MS - (performance.now() - press.at)));
          }
        }
      });
      window.addEventListener("avibe:native:voice.capture", (event) => {
        const detail = event.detail || {};
        if (detail.pcm && events[events.length - 1] !== "pcm") note("pcm");
        if (detail.ended && press && press.take === detail.take) { press.take = null; end("ended"); }
      });

      input.addEventListener("focus", () => { set("s-focus", "focus=yes"); name(); });
      input.addEventListener("blur", () => { set("s-focus", "focus=no"); name(); });
      input.addEventListener("input", () => set("s-typed", "typed=" + input.value.length));
      document.getElementById("blur").addEventListener("click", () => input.blur());

      // A button beside the input: short and long presses are its own. (A long press
      // makes no click on iOS: it is counted by the press itself.)
      let taps = 0, longs = 0, downAt = 0;
      const send = document.getElementById("send");
      send.addEventListener("pointerdown", () => { downAt = performance.now(); });
      const pressedSend = () => {
        if (performance.now() - downAt >= 500) longs += 1; else taps += 1;
        set("s-send", "send=" + taps + "/" + longs);
      };
      send.addEventListener("pointerup", pressedSend);
      send.addEventListener("pointercancel", pressedSend);

      let sheetTaps = 0;
      document.getElementById("open-sheet").addEventListener("click", () => { sheet.classList.add("open"); name(); });
      document.getElementById("close-sheet").addEventListener("click", (event) => { event.stopPropagation(); sheet.classList.remove("open"); name(); });
      document.getElementById("sheet-body").addEventListener("click", () => { sheetTaps += 1; set("s-sheet", "sheet=" + sheetTaps); });

      name();
      set("s-ready", "ready=yes");
    })();
    </script>
    </body>
    </html>
    """#
}

/// Each touch as UIKit delivered it to a recogniser on the WebView: how late
/// its start and its end arrived (ms after the touch's own time), how many
/// moves came between, and how long the touch lasted by its own times.
/// `b+3,m12,e+5/340` is a touch whose start came 3 ms late, with 12 moves,
/// whose end came 5 ms late after 340 ms. `r@520` is a touch UIKit never
/// ended for the recogniser: it reset it 520 ms after the touch's start (a
/// press the page took for itself). Passive, as the production observer.
private final class FixtureHoldTouchLog: UIGestureRecognizer {
    private(set) var touches: [String] = []
    private var current = ""
    private var moves = 0
    private var began: TimeInterval = 0
    private var open = false

    init() {
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
    }

    private func late(_ touch: UITouch) -> Int {
        Int(((ProcessInfo.processInfo.systemUptime - touch.timestamp) * 1000).rounded())
    }

    override func canPrevent(_ preventedGestureRecognizer: UIGestureRecognizer) -> Bool { false }
    override func canBePrevented(by preventingGestureRecognizer: UIGestureRecognizer) -> Bool { false }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        guard let touch = touches.first else { return }
        began = touch.timestamp
        moves = 0
        current = "b+\(late(touch))"
        open = true
    }

    override func reset() {
        super.reset()
        guard open else { return }
        open = false
        let after = Int(((ProcessInfo.processInfo.systemUptime - began) * 1000).rounded())
        touches.append("\(current),m\(moves),r@\(after)")
        if touches.count > 6 { touches.removeFirst() }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
        moves += 1
    }

    private func finish(_ touches: Set<UITouch>, as kind: String) {
        open = false
        if let touch = touches.first {
            let length = Int(((touch.timestamp - began) * 1000).rounded())
            self.touches.append("\(current),m\(moves),\(kind)+\(late(touch))/\(length)")
            if self.touches.count > 6 { self.touches.removeFirst() }
        }
        state = .failed
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        finish(touches, as: "e")
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        finish(touches, as: "c")
    }
}
