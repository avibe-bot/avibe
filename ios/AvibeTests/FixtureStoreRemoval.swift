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
@MainActor
enum FixtureStoreRemoval {
    /// A store WebKit still listed, with the reason WebKit gave.
    struct StillRegistered: Error, CustomStringConvertible {
        let identifier: UUID
        let ownersAlive: Bool
        let error: Error?

        var description: String {
            "Fixture store still registered: \(identifier); something of the test still holds it: "
                + "\(ownersAlive); error=\(String(describing: error))"
        }
    }

    /// Stores WebKit alone still held when their test removed them, and
    /// since when.
    private static var heldByWebKit: [UUID: Date] = [:]
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
        for attempt in 0..<50 {
            lastError = await AccountWebsiteDataStoreOperations.live.removeIdentifier(identifier)
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
        if !alive, isHeldByWebKitAlone(lastError) {
            leaveToLaterRemovals(identifier)
            print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=false, "
                + "WebKit still holds it: left to a later removal")
            return nil
        }
        print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=false")
        return StillRegistered(identifier: identifier, ownersAlive: alive, error: lastError)
    }

    /// One try for each store WebKit still held at an earlier removal. A
    /// store still listed with no reason given, or with WebKit's own hold,
    /// is tried again at the next removal. Any other refusal fails the test
    /// that is running now, though the store is an earlier test's: the
    /// message says so.
    static func retryHeldByWebKit() async {
        for (identifier, since) in heldByWebKit {
            let error = await AccountWebsiteDataStoreOperations.live.removeIdentifier(identifier)
            let age = Int(Date().timeIntervalSince(since))
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
