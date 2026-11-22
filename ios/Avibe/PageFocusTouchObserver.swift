import UIKit
import UIKit.UIGestureRecognizerSubclass

/// Gives a WebView's page the focus at the first touch on it, on iOS 27.
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
/// So at touch-down the App does what the ordinary tap does at its end.
/// Only at a touch: a page nobody has touched is as WebKit left it. A touch
/// reaches the WebView only when nothing lies over it (App Lock's window, a
/// cover, a sheet).
///
/// It never recognises, cancels or delays anything: the page gets every
/// touch exactly as before.
final class PageFocusTouchObserver: UIGestureRecognizer {
    init() {
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        if #available(iOS 27, *), let view {
            Self.focusPage(in: view)
        }
        // Never a gesture of its own: it has seen the touch come down and
        // fails at once, which prevents nothing; the next touch starts it
        // afresh.
        state = .failed
    }

    /// Makes `view` first responder (a WebView hands that to its content
    /// view), unless something inside it already is: the content view, with
    /// or without a keyboard up. Asking again then would restart WebKit's
    /// selection and input view for a page that is already focused.
    ///
    /// The view itself being first responder does not count: a WebView keeps
    /// it only when its content view refused, with the page still unfocused,
    /// and asked again it hands it on.
    static func focusPage(in view: UIView) {
        guard view.window != nil, !view.subviews.contains(where: holdsFirstResponder) else { return }
        view.becomeFirstResponder()
    }

    private static func holdsFirstResponder(_ view: UIView) -> Bool {
        view.isFirstResponder || view.subviews.contains(where: holdsFirstResponder)
    }
}
