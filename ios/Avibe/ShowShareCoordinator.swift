import Foundation
import UIKit
import WebKit

/// C1 v1 outbound requests name a Show, never an arbitrary URL or local file.
struct ShowShareRequest: Equatable {
    enum Kind: String { case link, pdf, image }
    let operationID: UUID
    let kind: Kind
    let sessionID: String

    static func parse(_ params: [String: Any]) -> ShowShareRequest? {
        guard Set(params.keys) == ["operationId", "kind", "sessionId"],
              let rawID = params["operationId"] as? String,
              let id = UUID(uuidString: rawID),
              id.uuidString.lowercased() == rawID.lowercased(),
              rawID[rawID.index(rawID.startIndex, offsetBy: 14)] == "4",
              ["8", "9", "a", "b"].contains(String(rawID.lowercased()[rawID.index(rawID.startIndex, offsetBy: 19)])),
              let rawKind = params["kind"] as? String, let kind = Kind(rawValue: rawKind),
              let session = params["sessionId"] as? String,
              !session.isEmpty, session.utf8.count <= 200,
              session.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) }),
              !session.contains("/"), !session.contains("\\") else { return nil }
        return ShowShareRequest(operationID: id, kind: kind, sessionID: session)
    }
}

struct ShowShareResult: Equatable {
    enum Status: String { case completed, cancelled, failed, expired }
    let request: ShowShareRequest
    let status: Status
    var error: String? = nil

    var jsonObject: [String: Any] {
        var result: [String: Any] = [
            "operationId": request.operationID.uuidString.lowercased(),
            "kind": request.kind.rawValue,
            "status": status.rawValue,
        ]
        if let error { result["error"] = ["code": error] }
        return result
    }
}

/// Only this directory holds C1 exports. No consumer-supplied path is accepted.
struct ShowShareFiles {
    static let maximumBytes = 50 * 1024 * 1024
    static let completionGrace: TimeInterval = 5 * 60
    static let operationLifetime: TimeInterval = 15 * 60
    static let orphanLifetime: TimeInterval = 24 * 60 * 60
    let root: URL
    private let manager = FileManager.default

    init(root: URL = FileManager.default.temporaryDirectory.appendingPathComponent("AvibeShowShares", isDirectory: true)) {
        self.root = root
    }

    func write(_ data: Data, request: ShowShareRequest) throws -> URL {
        guard !data.isEmpty, data.count <= Self.maximumBytes, request.kind != .link else {
            throw Failure.invalidExport
        }
        let directory = root.appendingPathComponent(request.operationID.uuidString, isDirectory: true)
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        // Opaque names keep titles, account data and paths out of temporary metadata.
        let url = directory.appendingPathComponent(request.kind == .pdf ? "Show.pdf" : "Show.png")
        try data.write(to: url, options: [.atomic, .completeFileProtectionUnlessOpen])
        return url
    }

    func remove(_ id: UUID) throws {
        let directory = root.appendingPathComponent(id.uuidString, isDirectory: true)
        guard manager.fileExists(atPath: directory.path) else { return }
        try manager.removeItem(at: directory)
    }

    func removeOrphans(asOf now: Date = Date()) throws {
        guard manager.fileExists(atPath: root.path) else { return }
        for directory in try manager.contentsOfDirectory(
            at: root, includingPropertiesForKeys: [.creationDateKey, .isDirectoryKey, .isSymbolicLinkKey]
        ) {
            guard UUID(uuidString: directory.lastPathComponent) != nil else { continue }
            let values = try directory.resourceValues(forKeys: [.creationDateKey, .isDirectoryKey, .isSymbolicLinkKey])
            guard values.isSymbolicLink != true, values.isDirectory == true,
                  let created = values.creationDate,
                  now.timeIntervalSince(created) >= Self.orphanLifetime else { continue }
            try manager.removeItem(at: directory)
        }
    }

    enum Failure: Error { case invalidExport }
}

/// One owner for snapshot, system handoff, idempotent replies and file leases.
/// Its source is the existing authenticated WKWebView, never a copied cookie jar.
@MainActor
final class ShowShareCoordinator {
    static let shared = ShowShareCoordinator()
    static let capabilities: [String: Any] = [
        "version": 1,
        "inbound_kinds": [String](),
        "outbound_kinds": ["link", "pdf", "image"],
        "max_bytes": ["link": 32768, "pdf": ShowShareFiles.maximumBytes, "image": ShowShareFiles.maximumBytes],
        "max_total_bytes": ShowShareFiles.maximumBytes,
        "max_attachments": 1,
        "supports_pending": false,
    ]
    typealias Reply = @MainActor (ShowShareResult) -> Void
    typealias Presenter = @MainActor (UIActivityViewController, WKWebView) -> Bool
    typealias Snapshot = @MainActor (WKWebView, CGRect) async throws -> UIImage

    @MainActor
    private final class Operation {
        let request: ShowShareRequest
        weak var webView: WKWebView?
        let origin: WebOrigin
        let sourceURL: URL?
        let started: Date
        var replies: [Reply]
        var activity: UIActivityViewController?
        var deadline: DispatchWorkItem?
        var preparation: Task<Void, Never>?
        init(_ request: ShowShareRequest, webView: WKWebView, origin: WebOrigin, now: Date, reply: @escaping Reply) {
            self.request = request
            self.webView = webView
            self.origin = origin
            sourceURL = webView.url
            started = now
            replies = [reply]
        }
    }
    private struct Terminal {
        let result: ShowShareResult
        weak var webView: WKWebView?
        let origin: WebOrigin
        let date: Date
    }
    private let files: ShowShareFiles
    private let now: () -> Date
    private let presentActivity: Presenter
    private let snapshot: Snapshot
    private var active: Operation?
    private var terminal: [UUID: Terminal] = [:]
    private var leases: [UUID: Date] = [:]
    private var cleanupWork: DispatchWorkItem?
    private var foregroundObserver: NSObjectProtocol?

    init(
        files: ShowShareFiles = ShowShareFiles(),
        now: @escaping () -> Date = Date.init,
        presentActivity: @escaping Presenter = ShowShareCoordinator.present,
        snapshot: @escaping Snapshot = ShowShareCoordinator.capture
    ) {
        self.files = files
        self.now = now
        self.presentActivity = presentActivity
        self.snapshot = snapshot
        foregroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in self?.reconcile() }
        }
    }

    deinit {
        if let foregroundObserver { NotificationCenter.default.removeObserver(foregroundObserver) }
        cleanupWork?.cancel()
    }

    func cleanAtLaunch() {
        // Failed deletion remains in this controlled directory for the next launch.
        try? files.removeOrphans(asOf: now())
        reconcile()
    }

    func share(_ request: ShowShareRequest, from webView: WKWebView, origin: WebOrigin, reply: @escaping Reply) {
        reconcile()
        guard sourceIsCurrent(webView, origin: origin), webView.window != nil else {
            reply(ShowShareResult(request: request, status: .failed, error: "forbidden"))
            return
        }
        if let prior = terminal[request.operationID] {
            guard prior.result.request == request, prior.webView === webView, prior.origin == origin else {
                reply(ShowShareResult(request: request, status: .failed, error: "operation_conflict"))
                return
            }
            reply(prior.result)
            return
        }
        if let active {
            if active.request == request, active.webView === webView, active.origin == origin {
                active.replies.append(reply)
            } else {
                reply(ShowShareResult(request: request, status: .failed, error: "busy"))
            }
            return
        }
        let operation = Operation(request, webView: webView, origin: origin, now: now(), reply: reply)
        active = operation
        let deadline = DispatchWorkItem { [weak self, weak operation] in
            guard let self, let operation, self.active === operation else { return }
            self.finish(operation, status: .expired, error: "expired")
        }
        operation.deadline = deadline
        DispatchQueue.main.asyncAfter(deadline: .now() + ShowShareFiles.operationLifetime, execute: deadline)
        operation.preparation = Task { [weak self, weak operation] in
            guard let self, let operation else { return }
            await self.prepare(operation)
        }
    }

    func cancel(in webView: WKWebView) {
        if let active, active.webView === webView { finish(active, status: .cancelled) }
    }

    /// Re-evaluate deadlines after suspension; timers alone cannot run in the background.
    func reconcile() {
        let current = now()
        terminal = terminal.filter { current.timeIntervalSince($0.value.date) < ShowShareFiles.orphanLifetime }
        if let active, current.timeIntervalSince(active.started) >= ShowShareFiles.operationLifetime {
            finish(active, status: .expired, error: "expired")
        }
        for (id, deadline) in leases where deadline <= current {
            do {
                try files.remove(id)
                leases.removeValue(forKey: id)
            } catch {
                // Keep only the opaque lease and retry on the next foreground/cleanup.
                leases[id] = current.addingTimeInterval(60)
            }
        }
        scheduleCleanup()
    }

    private func prepare(_ operation: Operation) async {
        guard let webView = operation.webView else {
            finish(operation, status: .cancelled)
            return
        }
        do {
            let raw = try await webView.callAsyncJavaScript(
                Self.inspectScript,
                arguments: ["sessionID": operation.request.sessionID, "kind": operation.request.kind.rawValue,
                            "origin": operation.origin.serialized, "operationID": operation.request.operationID.uuidString],
                in: nil, contentWorld: .defaultClient
            )
            guard active === operation else { return }
            guard let state = raw as? [String: Any], state["allowed"] as? Bool == true,
                  sourceIsCurrent(webView, origin: operation.origin), webView.url == operation.sourceURL else {
                finish(operation, status: .failed, error: "forbidden")
                return
            }
            let item: Any
            if operation.request.kind == .link {
                guard let rawURL = state["url"] as? String, rawURL.utf8.count <= 32768,
                      let url = URL(string: rawURL), WebOrigin(url: url, requiresHTTPS: false) != nil,
                      url.scheme == "https" || WebOrigin(url: url, requiresHTTPS: false) == operation.origin else {
                    finish(operation, status: .failed, error: "invalid_link")
                    return
                }
                item = url
            } else {
                guard let rect = Self.captureRect(state, in: webView) else {
                    finish(operation, status: .failed, error: "not_visible")
                    return
                }
                let image = try await snapshot(webView, rect)
                guard active === operation else { return }
                let valid = try await webView.callAsyncJavaScript(
                    Self.validateScript, arguments: ["operationID": operation.request.operationID.uuidString],
                    in: nil, contentWorld: .defaultClient
                ) as? Bool
                guard active === operation else { return }
                guard valid == true, sourceIsCurrent(webView, origin: operation.origin), webView.url == operation.sourceURL else {
                    finish(operation, status: .failed, error: "view_changed")
                    return
                }
                let data: Data?
                if operation.request.kind == .image {
                    data = image.pngData()
                } else {
                    // Rasterize exactly the same visible pixels as PNG: printing would reflow
                    // the iframe, reveal hidden content, and omit dynamic canvas state.
                    data = UIGraphicsPDFRenderer(bounds: CGRect(origin: .zero, size: image.size)).pdfData { context in
                        context.beginPage()
                        image.draw(in: CGRect(origin: .zero, size: image.size))
                    }
                }
                guard let data else { throw ShowShareFiles.Failure.invalidExport }
                item = try files.write(data, request: operation.request)
            }
            guard active === operation else { return }
            guard sourceIsCurrent(webView, origin: operation.origin), webView.url == operation.sourceURL else {
                finish(operation, status: .cancelled)
                return
            }
            let activity = UIActivityViewController(activityItems: [item], applicationActivities: nil)
            operation.activity = activity
            activity.completionWithItemsHandler = { [weak self, weak operation] _, completed, _, error in
                guard let self, let operation, self.active === operation else { return }
                self.finish(operation, status: error != nil ? .failed : completed ? .completed : .cancelled,
                            error: error != nil ? "system_share_failed" : nil, dismiss: false)
            }
            guard presentActivity(activity, webView) else {
                finish(operation, status: .failed, error: "presentation_failed")
                return
            }
        } catch {
            if active === operation { finish(operation, status: .failed, error: "export_failed") }
        }
    }

    private func finish(_ operation: Operation, status: ShowShareResult.Status, error: String? = nil, dismiss: Bool = true) {
        guard active === operation else { return }
        active = nil
        operation.deadline?.cancel()
        operation.preparation?.cancel()
        operation.activity?.completionWithItemsHandler = nil
        if dismiss { operation.activity?.dismiss(animated: false) }
        let result = ShowShareResult(request: operation.request, status: status, error: error)
        terminal[operation.request.operationID] = Terminal(
            result: result, webView: operation.webView, origin: operation.origin, date: now()
        )
        leases[operation.request.operationID] = status == .completed
            ? now().addingTimeInterval(ShowShareFiles.completionGrace) : now()
        let replies = operation.replies
        operation.replies = []
        reconcile()
        for reply in replies { reply(result) }
    }

    private func scheduleCleanup() {
        cleanupWork?.cancel()
        guard let next = leases.values.min() else { cleanupWork = nil; return }
        let work = DispatchWorkItem { [weak self] in self?.reconcile() }
        cleanupWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0.1, next.timeIntervalSince(now())), execute: work)
    }

    private func sourceIsCurrent(_ webView: WKWebView, origin: WebOrigin) -> Bool {
        !webView.isLoading && webView.url.flatMap { WebOrigin(url: $0, requiresHTTPS: false) } == origin
    }

    static func captureRect(_ state: [String: Any], in webView: WKWebView) -> CGRect? {
        guard let rect = state["rect"] as? [String: Double], let viewport = state["viewport"] as? [String: Double],
              let x = rect["x"], let y = rect["y"], let width = rect["width"], let height = rect["height"],
              let viewportWidth = viewport["width"], let viewportHeight = viewport["height"],
              [x, y, width, height, viewportWidth, viewportHeight].allSatisfy({ $0.isFinite }),
              x >= 0, y >= 0, width > 0, height > 0, viewportWidth > 0, viewportHeight > 0,
              x + width <= viewportWidth + 0.5, y + height <= viewportHeight + 0.5 else { return nil }
        let visible = webView.bounds.inset(by: webView.scrollView.adjustedContentInset)
        let scale = visible.width / viewportWidth
        let result = CGRect(x: visible.minX + x * scale, y: visible.minY + y * scale,
                            width: width * scale, height: height * scale)
        guard !result.isEmpty, visible.insetBy(dx: -0.5, dy: -0.5).contains(result),
              result.width * result.height * pow(webView.window?.screen.scale ?? 3, 2) <= 40_000_000 else { return nil }
        return result
    }

    private static func capture(_ webView: WKWebView, rect: CGRect) async throws -> UIImage {
        let configuration = WKSnapshotConfiguration()
        configuration.rect = rect
        configuration.afterScreenUpdates = true
        return try await webView.takeSnapshot(configuration: configuration)
    }

    private static func present(_ activity: UIActivityViewController, from webView: WKWebView) -> Bool {
        var responder: UIResponder? = webView
        while let current = responder {
            if let controller = current as? UIViewController {
                guard controller.presentedViewController == nil, !controller.isBeingDismissed,
                      controller.viewIfLoaded?.window != nil else { return false }
                activity.popoverPresentationController?.sourceView = webView
                activity.popoverPresentationController?.sourceRect = CGRect(x: webView.bounds.midX, y: webView.bounds.midY, width: 1, height: 1)
                controller.present(activity, animated: true)
                return true
            }
            responder = current.next
        }
        return false
    }

    /// Runs in an isolated world. The page cannot replace fetch, the inspection
    /// function, or the stored DOM identities. Only two existing GET APIs are read.
    static let inspectScript = #"""
    if (location.origin !== origin || document.visibilityState !== 'visible') return {allowed:false};
    const sourceURL = location.href;
    const read = async (path) => {
      const response = await fetch(path, {credentials:'same-origin', cache:'no-store', redirect:'error'});
      if (!response.ok || new URL(response.url).origin !== origin) throw new Error('access');
      return response.json();
    };
    const path = '/api/show-pages/' + encodeURIComponent(sessionID);
    const access = await read(path + '/access');
    if (access.can_use !== true) return {allowed:false};
    const page = await read(path);
    if (location.href !== sourceURL || page.session_id !== sessionID || page.offline === true
        || !['private','limited','public'].includes(page.visibility)) return {allowed:false};
    if (kind === 'link') {
      const href = page.visibility === 'limited' ? page.public_url
        : page.active_url || (page.visibility === 'private' ? '/show/' + encodeURIComponent(sessionID) + '/'
          : page.share_id ? '/p/' + encodeURIComponent(page.share_id) + '/' : null);
      return {allowed:!!href, url:href ? new URL(href, origin).href : ''};
    }
    const frames = [...document.querySelectorAll('iframe[data-avibe-show-session]')]
      .filter(frame => frame.getAttribute('data-avibe-show-session') === sessionID);
    const measure = (frame) => {
      const expected = '/show/' + encodeURIComponent(sessionID) + '/';
      const child = frame.contentWindow;
      if (!child || child.location.origin !== origin || !child.location.pathname.startsWith(expected)
          || child.document.readyState !== 'complete' || !child.document.body) return null;
      const viewport = window.visualViewport;
      const left = viewport?.offsetLeft ?? 0, top = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? innerWidth, height = viewport?.height ?? innerHeight;
      let r = frame.getBoundingClientRect();
      let x = Math.max(left, r.left), y = Math.max(top, r.top);
      let right = Math.min(left + width, r.right), bottom = Math.min(top + height, r.bottom);
      for (let node = frame; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) < 1) return null;
        if (node !== frame) {
          const box = node.getBoundingClientRect();
          if (style.overflowX !== 'visible') { x = Math.max(x, box.left); right = Math.min(right, box.right); }
          if (style.overflowY !== 'visible') { y = Math.max(y, box.top); bottom = Math.min(bottom, box.bottom); }
        }
      }
      if (right <= x || bottom <= y) return null;
      // Reject overlays rather than include confirmation, another app, or a dialog.
      for (const px of [x + 1, (x + right)/2, right - 1])
        for (const py of [y + 1, (y + bottom)/2, bottom - 1])
          if (document.elementFromPoint(px, py) !== frame) return null;
      // A small toast can fall between the sample points. Check every intersecting
      // sibling as well, including pointer-transparent overlays that hit testing omits.
      for (const node of document.querySelectorAll('body *')) {
        if (node === frame || node.contains(frame)) continue;
        const box = node.getBoundingClientRect();
        const l = Math.max(x, box.left), t = Math.max(y, box.top);
        const r = Math.min(right, box.right), b = Math.min(bottom, box.bottom);
        if (r <= l || b <= t) continue;
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) continue;
        const stack = document.elementsFromPoint((l+r)/2, (t+b)/2);
        const front = stack.indexOf(node), target = stack.indexOf(frame);
        if ((front >= 0 && (target < 0 || front < target))
            || (style.pointerEvents === 'none' && ['fixed','sticky','absolute'].includes(style.position))) return null;
      }
      return {rect:{x:x-left, y:y-top, width:right-x, height:bottom-y}, viewport:{width,height},
        childURL:child.location.href, scrollX:child.scrollX, scrollY:child.scrollY};
    };
    const visible = frames.map(frame => { try { return {frame, state:measure(frame)}; } catch { return {frame,state:null}; } })
      .filter(entry => entry.state);
    if (visible.length !== 1) return {allowed:false};
    const {frame, state} = visible[0];
    globalThis.__avibeShowShare = {operationID, document, frame, sourceURL, state, measure};
    return {allowed:true, ...state};
    """#

    static let validateScript = #"""
    const saved = globalThis.__avibeShowShare;
    if (!saved || saved.operationID !== operationID || saved.document !== document
        || saved.sourceURL !== location.href || !saved.frame.isConnected || document.visibilityState !== 'visible') return false;
    try { return JSON.stringify(saved.measure(saved.frame)) === JSON.stringify(saved.state); }
    catch { return false; }
    """#
}
