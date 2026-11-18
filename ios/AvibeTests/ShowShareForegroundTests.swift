import UIKit
import WebKit
import XCTest
@testable import Avibe

/// On-demand simulator evidence against a disposable server serving the real
/// design-frame component graph and the two read-only Show APIs. No account is used.
@MainActor
final class ShowShareForegroundTests: XCTestCase {
    func testRealShowControlsPresentTheSystemSheetInBothLanguagesAndThemes() async throws {
        guard let raw = ProcessInfo.processInfo.environment["AVIBE_I1_FOREGROUND_ORIGIN"],
              let base = URL(string: raw), base.host == "127.0.0.1", base.scheme == "http",
              let origin = WebOrigin(url: base, requiresHTTPS: false) else {
            throw XCTSkip("Set AVIBE_I1_FOREGROUND_ORIGIN to the disposable local Show sharing fixture server.")
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let share = ShowShareCoordinator(files: ShowShareFiles(root: root))
        let controller = UIViewController()
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let previous = scene.windows.first(where: \.isKeyWindow)
        let window = UIWindow(windowScene: scene)
        window.frame = scene.coordinateSpace.bounds
        window.rootViewController = controller
        window.makeKeyAndVisible()
        defer {
            controller.dismiss(animated: false)
            window.isHidden = true
            window.rootViewController = nil
            previous?.makeKeyAndVisible()
        }
        for language in ["en", "zh"] {
            for theme in ["light", "dark"] {
                window.overrideUserInterfaceStyle = theme == "dark" ? .dark : .light
                let metadata = NativeShellMetadata(shellVersion: "I1-foreground", userAgentVersion: "I1")
                let configuration = NativeBridgeConfiguration.make(
                    instanceOrigin: origin, metadata: metadata, websiteDataStore: .nonPersistent(),
                    setOuterScrollLocked: { _, _ in true }, showShare: share
                )
                // The design fixture declares its own bridge until it renders. Install the
                // production bridge afterwards, preserving its actual native message handler.
                configuration.userContentController.removeAllUserScripts()
                let webView = WKWebView(frame: controller.view.bounds, configuration: configuration)
                webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
                controller.view.addSubview(webView)
                let url = try XCTUnwrap(URL(string: "/design.html?frame=show-pages%2Fios-sharing%2Fmenu-\(language)&theme=\(theme)&lang=\(language)", relativeTo: base))
                webView.load(URLRequest(url: url))
                try await wait(in: webView, script: "document.body.dataset.designReady !== undefined && !!document.querySelector('iframe[data-avibe-show-session]')")
                try await webView.evaluateJavaScript("delete window.avibeNative;\n" + NativeBridgeConfiguration.scriptSource(instanceOrigin: origin, metadata: metadata))
                // Replace the design-only asset with a real same-origin Show. Isolated-world
                // DOM methods bypass the design network's page-world source mapper.
                _ = try await webView.callAsyncJavaScript(
                    "document.querySelector('iframe[data-avibe-show-session]').setAttribute('src','/show/ses_funnel/?embedded=1'); return true;",
                    arguments: [:], in: nil, contentWorld: .defaultClient
                )
                try await wait(in: webView, script: "(() => {const f=document.querySelector('iframe[data-avibe-show-session]');try{return f.contentWindow.location.pathname==='/show/ses_funnel/'&&f.contentDocument.readyState==='complete'}catch{return false}})()")
                let shareLabel = language == "en" ? "Share" : "分享"
                // The share icon's existing accessible label can include 'page'; use the
                // product's i18n catalog through the design fixture for exact naming.
                _ = try await webView.callAsyncJavaScript(
                    "const label=window.__DESIGN__.t('chat.showPage.share'); const b=[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')===label); b.click(); return true;",
                    arguments: [:], in: nil, contentWorld: .page
                )
                try await wait(in: webView, script: "[...document.querySelectorAll('button')].some(b=>b.textContent.includes(window.__DESIGN__.t('chat.showPage.sharePdf')))")
                let expectedPDF = language == "en" ? "Share PDF" : "分享 PDF"
                let actualPDF = try await webView.evaluateJavaScript("window.__DESIGN__.t('chat.showPage.sharePdf')") as? String
                XCTAssertEqual(actualPDF, expectedPDF)
                try await settle(in: webView)
                attach(window, name: "i1-\(language)-\(theme)-menu")
                _ = try await webView.callAsyncJavaScript(
                    "const label=window.__DESIGN__.t('chat.showPage.sharePdf'); [...document.querySelectorAll('button')].find(b=>b.textContent.trim()===label).click(); return true;",
                    arguments: [:], in: nil, contentWorld: .page
                )
                try await wait(in: webView, script: "!!document.querySelector('[role=dialog]')")
                try await settle(in: webView)
                attach(window, name: "i1-\(language)-\(theme)-confirmation")
                _ = try await webView.callAsyncJavaScript(
                    "const label=window.__DESIGN__.t('common.continue'); [...document.querySelectorAll('[role=dialog] button')].find(b=>b.textContent.trim()===label).click(); return true;",
                    arguments: [:], in: nil, contentWorld: .page
                )
                let deadline = ContinuousClock.now.advanced(by: .seconds(15))
                while controller.presentedViewController == nil, ContinuousClock.now < deadline {
                    try await Task.sleep(for: .milliseconds(50))
                }
                let activity = try XCTUnwrap(controller.presentedViewController as? UIActivityViewController,
                    "\(shareLabel): the real controls must reach the native system sheet (\(language)/\(theme)).")
                try await Task.sleep(for: .milliseconds(600))
                attach(window, name: "i1-\(language)-\(theme)-system-sheet")
                // Close through the same public completion callback that UIKit owns. No
                // destination is selected, and no message or file is sent externally.
                activity.completionWithItemsHandler?(nil, false, nil, nil)
                controller.dismiss(animated: false)
                share.cancel(in: webView)
                webView.removeFromSuperview()
            }
        }
    }

    private func settle(in webView: WKWebView) async throws {
        _ = try await webView.callAsyncJavaScript("""
            const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
            await frame(); await frame();
            await Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity)
              .map(a => a.finished.catch(() => {})));
            await frame(); await frame();
            return true;
            """, arguments: [:], in: nil, contentWorld: .page)
    }

    private func wait(in webView: WKWebView, script: String) async throws {
        let deadline = ContinuousClock.now.advanced(by: WebKitReadiness.webContentLaunch)
        while ContinuousClock.now < deadline {
            if (try? await webView.evaluateJavaScript(script)) as? Bool == true { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("The disposable foreground fixture did not reach its expected UI state.")
        throw CocoaError(.coderInvalidValue)
    }

    private func attach(_ window: UIWindow, name: String) {
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
