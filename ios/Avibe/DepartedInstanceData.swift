import Foundation
import WebKit

/// The instance addresses an account has opened on this device, and the
/// removal of what an address left in the account's WebKit store once it is
/// no longer the account's.
///
/// An address that left the account may later be another account's
/// instance. The App then never opens it for this account again, but what
/// its pages stored (the Web UI's cached conversations, cookies) would still
/// be in the store. It is removed once avibe.bot has answered, signed in,
/// twice without listing the address, at least `confirmationInterval` apart:
/// the address cannot change hands sooner than 30 days, so nothing is lost
/// by waiting, and one wrong or empty answer cannot delete an instance's
/// local data.
@MainActor
final class DepartedInstanceData {
    /// One address the account opened: the WebKit store it was opened in,
    /// and since when signed-in answers have not listed it.
    struct Entry: Codable, Equatable {
        var store: UUID
        var missingSince: Date?
    }

    /// `[accountID: [origin: Entry]]`.
    static let key = "avibe.instances.openedOrigins.v1"
    static let confirmationInterval: TimeInterval = 24 * 60 * 60

    private let defaults: UserDefaults
    private let now: () -> Date
    private let currentStore: () -> UUID?
    /// Removes what the address left behind; false when it could not (it is
    /// tried again at the next answer).
    private let clean: (_ origin: String, _ accountID: String) async -> Bool
    private var isReconciling = false
    /// The latest answer that arrived while a removal was running: it is
    /// applied after it, so an address listed again meanwhile starts over.
    private var waiting: (live: Set<String>, accountID: String)?

    init(
        defaults: UserDefaults,
        now: @escaping () -> Date = Date.init,
        currentStore: @escaping () -> UUID?,
        clean: @escaping (_ origin: String, _ accountID: String) async -> Bool
    ) {
        self.defaults = defaults
        self.now = now
        self.currentStore = currentStore
        self.clean = clean
    }

    /// An instance is about to load in `store` for `accountID`.
    func noteOpened(accountID: String, origin: String, store: UUID) {
        var ledger = stored()
        // Opened again: whatever was counted against it starts over.
        ledger[accountID, default: [:]][origin] = Entry(store: store, missingSince: nil)
        save(ledger)
    }

    /// avibe.bot answered, signed in, for `accountID`, listing `live`.
    func reconcile(live: Set<String>, accountID: String) async {
        guard !isReconciling else {
            waiting = (live, accountID)
            return
        }
        isReconciling = true
        defer { isReconciling = false }
        var answer: (live: Set<String>, accountID: String)? = (live, accountID)
        while let next = answer {
            await apply(live: next.live, accountID: next.accountID)
            answer = waiting
            waiting = nil
        }
    }

    private func apply(live: Set<String>, accountID: String) async {
        guard let store = currentStore() else {
            return
        }
        var ledger = stored()
        // A store that is gone took its data with it (a sign-out, another
        // account): nothing of it is left to remove.
        for (account, entries) in ledger {
            ledger[account] = entries.filter { $0.value.store == store }
        }
        var due: [String] = []
        let moment = now()
        for (origin, entry) in ledger[accountID] ?? [:] {
            if live.contains(origin) {
                ledger[accountID]?[origin]?.missingSince = nil
            } else if let since = entry.missingSince {
                if moment.timeIntervalSince(since) >= Self.confirmationInterval {
                    due.append(origin)
                }
            } else {
                ledger[accountID]?[origin]?.missingSince = moment
                due.append(origin) // mutation: removed at the first missing answer
            }
        }
        save(ledger)
        for origin in due.sorted() {
            // A newer answer decides about what is left.
            guard waiting == nil else {
                break
            }
            guard currentStore() == store, await clean(origin, accountID) else {
                continue
            }
            var after = stored()
            // Unless it was opened again meanwhile.
            if after[accountID]?[origin]?.missingSince != nil {
                after[accountID]?[origin] = nil
                save(after)
            }
        }
    }

    /// The addresses recorded for `accountID`, for tests and diagnosis.
    func entries(accountID: String) -> [String: Entry] {
        stored()[accountID] ?? [:]
    }

    private func stored() -> [String: [String: Entry]] {
        guard
            let data = defaults.data(forKey: Self.key),
            let ledger = try? JSONDecoder().decode([String: [String: Entry]].self, from: data)
        else {
            return [:]
        }
        return ledger
    }

    private func save(_ ledger: [String: [String: Entry]]) {
        let kept = ledger.filter { !$0.value.isEmpty }
        guard !kept.isEmpty, let data = try? JSONEncoder().encode(kept) else {
            defaults.removeObject(forKey: Self.key)
            return
        }
        defaults.set(data, forKey: Self.key)
    }
}

/// Removes what one address stored in a WebKit store, and nothing else.
///
/// WebKit's own removal works on data records, which are grouped by
/// registrable domain: for `name.avibe.bot` that record is all of
/// `avibe.bot`, every instance of the account and its sign-in. So a page of
/// the App's own is shown at the address instead, without the network (a
/// simulated load, under a policy and a rule list that let it load nothing),
/// and a script the App runs in it deletes that origin's storage. Cookies
/// are removed natively, by host.
@MainActor
enum InstanceSiteDataCleaner {
    private static let world = WKContentWorld.world(name: "avibe-departed-instance")
    private static let loadTimeout: Duration = .seconds(10)

    /// True when everything the script and the cookie store could remove is
    /// gone.
    /// `removingCookies`: false when the host is still one the account uses
    /// (cookies belong to a host, not to an origin). `willUse` receives the
    /// WebView before anything is loaded, for a caller that tracks the
    /// WebViews of a store. `stillWanted` is asked again right before each
    /// thing is deleted (the page is prepared across suspensions, and the
    /// address may have been listed again meanwhile): false stops there,
    /// with nothing more removed.
    static func clean(
        origin: WebOrigin,
        in dataStore: WKWebsiteDataStore,
        removingCookies: Bool = true,
        willUse: (WKWebView) -> Void = { _ in },
        stillWanted: () -> Bool = { true }
    ) async -> Bool {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        if let rules = await blockEverything() {
            configuration.userContentController.add(rules)
        }
        let webView = WKWebView(frame: .zero, configuration: configuration)
        willUse(webView)
        let loader = Loader()
        webView.navigationDelegate = loader
        guard
            let response = HTTPURLResponse(
                url: origin.url,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: [
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Security-Policy": "default-src 'none'",
                    "Cache-Control": "no-store",
                ]
            )
        else {
            return false
        }
        webView.loadSimulatedRequest(
            URLRequest(url: origin.url),
            response: response,
            responseData: Data("<!doctype html><title></title>".utf8)
        )
        guard
            await loader.finished(within: loadTimeout),
            webView.url.flatMap({ WebOrigin(url: $0, requiresHTTPS: false) }) == origin,
            stillWanted()
        else {
            return false
        }
        let outcome = try? await webView.callAsyncJavaScript(
            script,
            arguments: [:],
            in: nil,
            contentWorld: world
        ) as? Bool
        guard stillWanted() else {
            return false
        }
        if removingCookies {
            let cookies = dataStore.httpCookieStore
            for cookie in await cookies.allCookies()
            where cookie.domain.hasSuffix("avibe-test.example") || cookie.domain == origin.host { // mutation: the parent domain and siblings too
                await cookies.deleteCookie(cookie)
            }
        }
        return outcome == true
    }

    /// True when every store was emptied; a database another page holds
    /// open reports false, and the removal is tried again later.
    private static let script = """
        // Bounded: a store that never answers reports false, and the
        // removal is tried again later.
        const within = (work) => Promise.race([
          work,
          new Promise((resolve) => setTimeout(() => resolve("timeout"), 8000)),
        ]);
        let complete = true;
        try { localStorage.clear(); sessionStorage.clear(); } catch (error) { complete = false; }
        try {
          const databases = await within(indexedDB.databases());
          if (databases === "timeout") {
            complete = false;
          } else {
            const results = await within(Promise.all(databases.map((database) => new Promise((resolve) => {
              const request = indexedDB.deleteDatabase(database.name);
              request.onsuccess = () => resolve(true);
              request.onerror = () => resolve(false);
              request.onblocked = () => resolve(false);
            }))));
            if (results === "timeout" || results.includes(false)) complete = false;
          }
        } catch (error) { complete = false; }
        try {
          const names = await within(caches.keys());
          if (names === "timeout") {
            complete = false;
          } else if (await within(Promise.all(names.map((name) => caches.delete(name)))) === "timeout") {
            complete = false;
          }
        } catch (error) { complete = false; }
        try {
          if ("serviceWorker" in navigator) {
            const registrations = await within(navigator.serviceWorker.getRegistrations());
            if (registrations === "timeout") {
              complete = false;
            } else {
              await within(Promise.all(registrations.map((registration) => registration.unregister())));
            }
          }
        } catch (error) { complete = false; }
        return complete;
        """

    private static var compiledRules: WKContentRuleList?

    /// A rule list that blocks every load (a simulated load is not one).
    /// The page's policy already lets it load nothing; this is the second
    /// lock. Nil if it cannot be compiled.
    private static func blockEverything() async -> WKContentRuleList? {
        if let compiledRules {
            return compiledRules
        }
        compiledRules = try? await WKContentRuleListStore.default().compileContentRuleList(
            forIdentifier: "avibe-departed-instance-block-all",
            encodedContentRuleList: #"[{"trigger":{"url-filter":".*"},"action":{"type":"block"}}]"#
        )
        return compiledRules
    }

    @MainActor
    private final class Loader: NSObject, WKNavigationDelegate {
        private var result: Bool?
        private var continuation: CheckedContinuation<Bool, Never>?

        func finished(within timeout: Duration) async -> Bool {
            if let result {
                return result
            }
            let expiry = Task { [weak self] in
                try? await Task.sleep(for: timeout)
                guard !Task.isCancelled else {
                    return
                }
                self?.settle(false)
            }
            defer { expiry.cancel() }
            return await withCheckedContinuation { continuation = $0 }
        }

        private func settle(_ outcome: Bool) {
            guard result == nil else {
                return
            }
            result = outcome
            continuation?.resume(returning: outcome)
            continuation = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            settle(true)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            settle(false)
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            settle(false)
        }
    }
}
