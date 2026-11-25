import WebKit
import XCTest
@testable import Avibe

/// Removes the named WebKit stores the tests make, for every test class.
///
/// What a test owns is asserted where it removes a store: its Apps (and
/// whatever else holds the store on its side) are released and no page of
/// theirs uses the store, within 5 s, or the removal fails.
///
/// On iOS 27 WebKit keeps a store while a web process of its own for it has
/// not shut down, and answers every removal "in use (by network process)"
/// until then. That process waits for the web content to answer what WebKit
/// last asked it: 11 ms at the median on CI, but over 30 s for 3 of 683
/// processes in one run, and five minutes for one that never answered. No
/// wait covers that, so such a store, once nothing of the test that held it
/// is alive, is left to later removals (one try each, in any test class),
/// and what WebKit still holds when the test bundle ends is printed with its
/// age. It is not a failure: the App itself never needs a store removed at
/// once (its sweep keeps a store listed until WebKit drops it).
///
/// WebKit's answer to a removal itself waits for that process: in the
/// teardown of tests that had passed it took up to eight minutes. So a
/// removal is waited for at most `removalAnswer`; one not answered by then
/// is treated as WebKit's own hold.
@MainActor
enum FixtureStoreRemoval {
    /// A store WebKit still listed, with the reason WebKit gave.
    struct StillRegistered: Error, CustomStringConvertible {
        let identifier: UUID
        let ownersAlive: Bool
        let error: Error?
        /// WebKit did not answer the removal within `removalAnswer`.
        var unanswered = false

        var description: String {
            "Fixture store still registered: \(identifier); something of the test still holds it: "
                + "\(ownersAlive); error=\(String(describing: error))"
                + (unanswered ? "; WebKit did not answer the removal within \(removalAnswer)" : "")
        }
    }

    /// Stores WebKit alone still held when their test removed them, and
    /// since when.
    private static var heldByWebKit: [UUID: Date] = [:]
    /// Stores whose removal WebKit has not answered yet. That call goes on:
    /// it removes the store whenever WebKit answers, in whatever test runs
    /// then.
    private static var unansweredRemovals: Set<UUID> = []

    /// Stores that may still disappear during a later test, by WebKit's
    /// hand: held by WebKit, or with a removal not yet answered. A test that
    /// checks the App never removed someone else's store leaves these out.
    static var leftToWebKit: Set<UUID> {
        Set(heldByWebKit.keys).union(unansweredRemovals)
    }
    private static var bundleEnd: BundleEnd?

    /// Removes `identifier`. `nil` when WebKit no longer lists it, or when
    /// WebKit alone still holds it (`ownersAlive` is false and WebKit says
    /// "in use (by network process)"): it is then left to later removals.
    /// Otherwise what is wrong, for the caller to fail on.
    static func remove(
        _ identifier: UUID,
        ownersAlive: @MainActor () -> Bool
    ) async -> StillRegistered? {
        await retryHeldByWebKit()
        var lastError: Error?
        var unanswered = false
        for attempt in 0..<50 {
            guard let answer = await boundedRemoval(identifier) else {
                // WebKit has not answered: it holds the store until a web
                // process of its own for it exits, which has taken minutes.
                unanswered = true
                break
            }
            lastError = answer.error
            if await !registered().contains(identifier) {
                heldByWebKit[identifier] = nil
                print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=true")
                return nil
            }
            if attempt < 49 {
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        let alive = ownersAlive()
        if !alive, unanswered || isHeldByWebKitAlone(lastError) {
            leaveToLaterRemovals(identifier)
            print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=false, "
                + (unanswered
                    ? "WebKit did not answer within \(removalAnswer): left to a later removal"
                    : "WebKit still holds it: left to a later removal"))
            return nil
        }
        print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=false"
            + (unanswered ? ", WebKit did not answer within \(removalAnswer)" : ""))
        return StillRegistered(identifier: identifier, ownersAlive: alive, error: lastError, unanswered: unanswered)
    }

    /// One try for each store WebKit still held at an earlier removal. A
    /// store still listed with no reason given, or with WebKit's own hold,
    /// is tried again at the next removal. Any other refusal fails the test
    /// that is running now, though the store is an earlier test's: the
    /// message says so.
    static func retryHeldByWebKit() async {
        for (identifier, since) in heldByWebKit {
            let age = Int(Date().timeIntervalSince(since))
            // An earlier removal still waits on WebKit: no second one, only
            // a look whether it is gone.
            if unansweredRemovals.contains(identifier) {
                if await !registered().contains(identifier) {
                    heldByWebKit[identifier] = nil
                    print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=true, "
                        + "\(age) s after its test")
                }
                continue
            }
            // A try in passing, not a wait: not answered within a second,
            // it is still WebKit's and tried again next time.
            guard let answer = await boundedRemoval(identifier, within: .seconds(1)) else { continue }
            let error = answer.error
            if await !registered().contains(identifier) {
                heldByWebKit[identifier] = nil
                print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=true, "
                    + "\(age) s after its test")
            } else if error != nil, !isHeldByWebKitAlone(error) {
                heldByWebKit[identifier] = nil
                XCTFail(
                    "A fixture store an earlier test left to WebKit \(age) s ago is still registered, "
                        + "and not for WebKit's own hold: \(identifier); error=\(String(describing: error))"
                )
            }
        }
    }

    /// What WebKit answered a removal: `nil` when it did not answer within
    /// `removalAnswer`. Its completion comes only once a web process of its
    /// own for the store has exited; on CI that took up to eight minutes,
    /// in the teardown of tests that had passed.
    static let removalAnswer: Duration = .seconds(10)

    private struct RemovalAnswer {
        let error: Error?
    }

    private static func boundedRemoval(
        _ identifier: UUID,
        within bound: Duration = removalAnswer
    ) async -> RemovalAnswer? {
        await withCheckedContinuation { continuation in
            let first = FirstAnswer(continuation)
            Task { @MainActor in
                let error = await AccountWebsiteDataStoreOperations.live.removeIdentifier(identifier)
                if first.settle(RemovalAnswer(error: error)) {
                    return
                }
                // A late answer: the store may be gone now.
                unansweredRemovals.remove(identifier)
                if heldByWebKit[identifier] != nil, await !registered().contains(identifier) {
                    let age = heldByWebKit[identifier].map { Int(Date().timeIntervalSince($0)) } ?? 0
                    heldByWebKit[identifier] = nil
                    print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=true, "
                        + "\(age) s after its test, when WebKit answered")
                }
            }
            Task { @MainActor in
                try? await Task.sleep(for: bound)
                if first.settle(nil) {
                    unansweredRemovals.insert(identifier)
                }
            }
        }
    }

    /// The first of the removal's answer and the bound.
    @MainActor
    private final class FirstAnswer {
        private var continuation: CheckedContinuation<RemovalAnswer?, Never>?

        init(_ continuation: CheckedContinuation<RemovalAnswer?, Never>) {
            self.continuation = continuation
        }

        /// Whether this was the first.
        @discardableResult
        func settle(_ answer: RemovalAnswer?) -> Bool {
            guard let continuation else { return false }
            continuation.resume(returning: answer)
            self.continuation = nil
            return true
        }
    }

    static func registered() async -> Set<UUID> {
        await withCheckedContinuation { continuation in
            WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                continuation.resume(returning: Set($0))
            }
        }
    }

    /// WebKit's answer for a store none of our pages uses any more, while a
    /// web process of its own for that store has not shut down yet. A store
    /// one of our pages still uses is "Data store is in use".
    static func isHeldByWebKitAlone(_ error: Error?) -> Bool {
        error?.localizedDescription == "Data store is in use (by network process)"
    }

    private static func leaveToLaterRemovals(_ identifier: UUID) {
        heldByWebKit[identifier] = heldByWebKit[identifier] ?? Date()
        if bundleEnd == nil {
            let observer = BundleEnd()
            XCTestObservationCenter.shared.addTestObserver(observer)
            bundleEnd = observer
        }
    }

    /// Prints what WebKit still holds when the test bundle ends.
    fileprivate static func reportAtBundleEnd() {
        for (identifier, since) in heldByWebKit {
            print("[iOS fixture] stage=bundle end, identifier=\(identifier), "
                + "WebKit still holds it after \(Int(Date().timeIntervalSince(since))) s")
        }
    }
}

/// Tells `FixtureStoreRemoval` that the test bundle ended. XCTest calls it
/// on the main thread.
private final class BundleEnd: NSObject, XCTestObservation {
    func testBundleDidFinish(_ testBundle: Bundle) {
        MainActor.assumeIsolated {
            FixtureStoreRemoval.reportAtBundleEnd()
        }
    }
}
