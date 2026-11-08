import ObjectiveC
import UIKit
import WebKit

/// Hides WebKit's form accessory bar (previous, next, Done) above the keyboard
/// in instance WebViews.
///
/// WebKit gives a page no way to remove the bar, and UIKit asks the first
/// responder (WebKit's private content view) for `inputAccessoryView`. The
/// content view gets a runtime subclass whose `inputAccessoryView` is nil,
/// the technique Capacitor's Keyboard plugin uses. The owner accepted its
/// App Review risk on 2026-11-08 (`docs/plans/ios-app.md` section 5); the
/// native composer would make it unnecessary for chat.
///
/// Only the keyboard's accessory changes. QuickType suggestions, including
/// one-time codes and saved passwords, belong to the keyboard and stay.
@MainActor
enum FormAccessoryBar {
    static let subclassSuffix = "_AvibeNoFormAccessory"

    /// Returns whether `webView`'s content view now has no accessory bar.
    @discardableResult
    static func hide(in webView: WKWebView) -> Bool {
        guard let contentView = contentView(of: webView) else {
            return false
        }
        let base: AnyClass = type(of: contentView)
        let baseName = NSStringFromClass(base)
        if baseName.hasSuffix(subclassSuffix) {
            return true
        }
        guard let subclass = subclass(of: base, named: baseName + subclassSuffix) else {
            return false
        }
        object_setClass(contentView, subclass)
        return true
    }

    static func contentView(of webView: WKWebView) -> UIView? {
        webView.scrollView.subviews.first {
            NSStringFromClass(type(of: $0)).hasPrefix("WKContent")
        }
    }

    private static func subclass(of base: AnyClass, named name: String) -> AnyClass? {
        if let existing = NSClassFromString(name) {
            return existing
        }
        let selector = #selector(getter: UIResponder.inputAccessoryView)
        guard
            let method = class_getInstanceMethod(UIView.self, selector),
            let created = objc_allocateClassPair(base, name, 0)
        else {
            return nil
        }
        let noAccessory: @convention(block) (AnyObject) -> UIView? = { _ in nil }
        class_addMethod(
            created,
            selector,
            imp_implementationWithBlock(noAccessory),
            method_getTypeEncoding(method)
        )
        objc_registerClassPair(created)
        return created
    }
}
