import UIKit
import UIKit.UIGestureRecognizerSubclass

/// Gives a WebView's page the focus while the WebView is in front, on
/// iOS 27, before any touch.
///
/// A WebKit page has the focus only while its content view is first
/// responder (WebKit's `PageClientImpl::isViewFocused`). Without it
/// `document.hasFocus()` is false, and `element.focus()` sets the focused
/// element and nothing else: no `focus` event, no keyboard.
///
/// WebKit makes the content view first responder at an ordinary tap. On
/// iOS 27 a press the page takes for itself does not do it: the composer
/// prevents the default on `touchstart` for a press that may become a hold,
/// and focuses its input at the tap's end. So when such a tap is the first
/// touch on a WebView whose content view is not first responder (a fresh
/// WebView after a cold launch, a push tap or an instance switch; or after
/// the App ended editing, for App Lock or the account cover), the input is
/// not focused and no keyboard comes, until an ordinary tap lands
/// somewhere. On iOS 26 the same tap focuses.
///
/// So the App makes the WebView first responder itself, which leaves the
/// page as it is after any ordinary tap. **Never during a touch:** on a
/// cold App the first first responder costs the main thread up to seconds
/// (the keyboard's set-up), and between a finger coming down and lifting
/// that would keep the lift from the page, which would time a tap as a
/// hold. It is done when the main thread is idle and no finger is on the
/// WebView; a touch that comes first finds the page as it was.
///
/// Only a WebView in front gets it: a focused, visible page counts as
/// presented to the reader (the chat page marks what arrives as read), so
/// one under App Lock, in an App that is not active, under a cover or
/// behind a sheet must stay without it. And the focus given here is taken
/// back when the WebView stops being in front before the reader has
/// touched it. Once a finger has been on the WebView its focus is the
/// reader's, as on iOS 26 after a tap: taking that would close a keyboard
/// under Control Center or a system alert.
///
/// As a gesture recogniser it only notes the fingers that come down on the
/// WebView, and asks each touch itself whether it is still down. It never
/// recognises, cancels or delays anything: the page gets every touch
/// exactly as before.
final class PageFocusKeeper: UIGestureRecognizer {
    /// No oftener than this is the WebView looked at.
    static let interval: TimeInterval = 0.1
    /// Every keeper: two WebViews in front at once must not take the focus
    /// from each other in turns.
    private static let all = NSHashTable<PageFocusKeeper>.weakObjects()

    /// The App shows its pages: it is active, and App Lock covers nothing.
    private let appShowsPages: () -> Bool
    /// The touches that came down on the WebView. Each is asked itself
    /// whether it is still down: for a press the page took for itself UIKit
    /// takes the gesture back (`reset()`) with the finger still on the
    /// glass, and sends this recogniser no end.
    private let touches = NSHashTable<UITouch>.weakObjects()
    /// The page has the focus from here, and no finger has been on the
    /// WebView since.
    private var gaveUntouched = false
    private var observer: CFRunLoopObserver?
    private var nextLook: TimeInterval = 0
    private var wakeAsked = false

    init(appShowsPages: @escaping () -> Bool) {
        self.appShowsPages = appShowsPages
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
        Self.all.add(self)
        // iOS 26 focuses the page at the composer's own tap: nothing is
        // done there.
        guard #available(iOS 27, *) else { return }
        // Each time the main thread has nothing left to do.
        let observer = CFRunLoopObserverCreateWithHandler(
            kCFAllocatorDefault, CFRunLoopActivity.beforeWaiting.rawValue, true, 0
        ) { [weak self] _, _ in
            MainActor.assumeIsolated { self?.idle() }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
        self.observer = observer
    }

    deinit {
        if let observer { CFRunLoopObserverInvalidate(observer) }
    }

    // MARK: Whether a finger is on the WebView

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        for touch in touches { self.touches.add(touch) }
        // The reader has touched the page: its focus is theirs from here.
        gaveUntouched = false
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        lifted()
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        lifted()
    }

    private func lifted() {
        guard !touching else { return }
        // Never a gesture of its own: the next touch starts it afresh.
        state = .failed
    }

    /// A finger that came down on the WebView is still down, by the touch's
    /// own word. A touch that is gone has lifted.
    private var touching: Bool {
        touches.allObjects.contains { $0.phase == .began || $0.phase == .moved || $0.phase == .stationary }
    }

    // MARK: The look

    /// The main thread is idle: looks at the WebView, no oftener than
    /// `interval`. A look that came too soon is made up for when its time
    /// comes, so the last change before the App goes quiet is not missed.
    private func idle() {
        let now = ProcessInfo.processInfo.systemUptime
        guard now >= nextLook else {
            guard !wakeAsked else { return }
            wakeAsked = true
            // Waking the main thread is enough: it comes back here.
            DispatchQueue.main.asyncAfter(deadline: .now() + (nextLook - now)) { [weak self] in
                MainActor.assumeIsolated { self?.wakeAsked = false }
            }
            return
        }
        nextLook = now + Self.interval
        look()
    }

    /// With no finger on the WebView: makes it first responder (it hands
    /// that to its content view) when it is in front with an unfocused
    /// page; and ends that again when it is no longer in front and the
    /// reader never touched it.
    func look() {
        guard !touching, let view, let window = view.window else { return }
        let focused = Self.holdsFirstResponder(in: view)
        // The reader's own focus: nothing here gives or takes it.
        if focused, !gaveUntouched { return }
        let inFront = appShowsPages() && Self.isOnTop(view, in: window)
        if focused {
            guard !inFront else { return }
            gaveUntouched = false
            view.endEditing(true)
            return
        }
        gaveUntouched = false
        // Another WebView in front in this window has the focus: it keeps it.
        guard inFront, !Self.anotherInFrontIsFocused(than: self, in: window) else { return }
        view.becomeFirstResponder()
        gaveUntouched = true
    }

    private static func anotherInFrontIsFocused(than keeper: PageFocusKeeper, in window: UIWindow) -> Bool {
        all.allObjects.contains { other in
            guard other !== keeper, let view = other.view, view.window === window else { return false }
            return holdsFirstResponder(in: view) && isOnTop(view, in: window)
        }
    }

    /// Whether something inside `view` is first responder: the content
    /// view, with or without a keyboard up. The view itself does not count:
    /// a WebView keeps first responder only when its content view refused,
    /// with the page still unfocused, and asked again it hands it on.
    static func holdsFirstResponder(in view: UIView) -> Bool {
        view.subviews.contains { $0.isFirstResponder || holdsFirstResponder(in: $0) }
    }

    /// Whether a touch at the middle of `view` would reach it: nothing in
    /// its window lies over it there (a cover, a sheet, an alert, another
    /// WebView), and it takes touches at all.
    static func isOnTop(_ view: UIView, in window: UIWindow) -> Bool {
        let middle = view.convert(CGPoint(x: view.bounds.midX, y: view.bounds.midY), to: window)
        guard let hit = window.hitTest(middle, with: nil) else { return false }
        return hit === view || hit.isDescendant(of: view)
    }
}
