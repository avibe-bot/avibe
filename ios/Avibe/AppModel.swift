import os
import SwiftUI
import WebKit

@MainActor
protocol RuntimeOpenTimer: AnyObject {
    func cancel()
}

@MainActor
protocol RuntimeOpenTiming: AnyObject {
    var now: Duration { get }

    /// The deadlines of the account transports the App opens. The App's are
    /// `.standard`; a timing for the CI simulator may give the control-plane
    /// page a web content launch's time there.
    var accountTransportDeadlines: MobileAuthTransportDeadlines { get }

    func schedule(
        after delay: Duration,
        _ action: @escaping @MainActor () -> Void
    ) -> any RuntimeOpenTimer
}

extension RuntimeOpenTiming {
    var accountTransportDeadlines: MobileAuthTransportDeadlines { .standard }
}

@MainActor
final class ContinuousRuntimeOpenTiming: RuntimeOpenTiming {
    private final class TaskTimer: RuntimeOpenTimer {
        private let task: Task<Void, Never>

        init(task: Task<Void, Never>) {
            self.task = task
        }

        deinit {
            task.cancel()
        }

        func cancel() {
            task.cancel()
        }
    }

    private let clock = ContinuousClock()
    private let origin: ContinuousClock.Instant

    init() {
        origin = clock.now
    }

    var now: Duration {
        origin.duration(to: clock.now)
    }

    func schedule(
        after delay: Duration,
        _ action: @escaping @MainActor () -> Void
    ) -> any RuntimeOpenTimer {
        let boundedDelay = max(.zero, delay)
        let deadline = clock.now.advanced(by: boundedDelay)
        return TaskTimer(
            task: Task { @MainActor in
                do {
                    try await Task.sleep(until: deadline, clock: clock)
                } catch {
                    return
                }
                guard !Task.isCancelled else {
                    return
                }
                action()
            }
        )
    }
}

enum RuntimeNavigationStage: Equatable {
    case started
    case committed
    case finished
    case handedOff
    case cancelled
    case restarting
    case failed
}

struct RuntimeNavigationEvent: Equatable {
    let stage: RuntimeNavigationStage
    let destination: NavigationDestination?
    /// The document came from the App's cache of the instance's last page
    /// (`RuntimePageCache`), not from the network.
    let fromCache: Bool

    init(
        stage: RuntimeNavigationStage,
        destination: NavigationDestination?,
        fromCache: Bool = false
    ) {
        self.stage = stage
        self.destination = destination
        self.fromCache = fromCache
    }
}

enum RuntimeOpenPhase: Equatable {
    case preparingRoute
    case connecting
    case authenticating
    case loadingPage
    /// The instance's last page has loaded from the App's cache, still
    /// covered: the instance itself has not answered yet.
    case showingCached
    /// The instance's last page is on screen from the App's cache, live to
    /// touch (its shell waits out an unreachable instance); the instance
    /// itself has not answered yet.
    case showingCachedLive
    /// The open of a page shown from the cache ran out of time: the page
    /// stays, with a banner offering Retry.
    case cachedUnreachable
    /// avibe.bot lists the instance offline while its last page is shown
    /// uncovered from the cache: the page stays, with a row offering Retry
    /// and Switch instance, until the instance answers the page.
    case cachedOffline
    case awaitingUser
    /// A legacy document finished without a product-ready contract. The page
    /// is handed to the user, but Native does not claim the Runtime is ready.
    case documentLoaded
    case failed
}

struct RuntimeOpenAttempt: Equatable {
    let id: Int
    let runtime: MobileRuntimeContext
    let startedAt: Duration
    var phase: RuntimeOpenPhase
    var remainingAutomaticBudget: Duration
    var automaticStartedAt: Duration?
    var deadline: Duration?
    /// A paused authentication/restart budget may resume. A handoff or
    /// terminal outcome has ended automatic timing even if its loading layer
    /// remains visible until the user cancels or a later document arrives.
    var canResumeAutomaticBudget: Bool
    var isTakingLong: Bool
    /// avibe.bot lists the instance offline. Only a page shown uncovered
    /// from the cache keeps such an open (`.cachedOffline`); until that is
    /// decided the open waits, and any other outcome ends at the list.
    var listedOffline = false
    /// A live load of the instance has started in this open.
    var startedLive = false

    /// Whether the open's loading layer offers Cancel: past the long-wait
    /// mark, or once automatic timing has ended for good (a handoff) while
    /// the layer stays, since no timeout will end it then.
    var offersCancel: Bool {
        isTakingLong || (automaticStartedAt == nil && !canResumeAutomaticBudget)
    }

    /// Native-only v1 has no product-ready signal. `documentLoaded` therefore
    /// remains explicitly readiness-unknown.
    var isVerifiedReady: Bool { false }
}

enum RuntimeOpenTerminationReason: Equatable {
    case timedOut
    case cancelled
    case replaced
    case accountTransition
}

struct RuntimeOpenTermination: Equatable {
    let attemptID: Int
    let instanceName: String
    let lastPhase: RuntimeOpenPhase
    let reason: RuntimeOpenTerminationReason
}

struct RuntimeOpenNotice: Equatable {
    let instanceID: String
    let instanceName: String
    let lastPhase: RuntimeOpenPhase
}

enum AppRoute: Equatable {
    case loading
    case onboarding(MobileOnboardingStartScreen)
    case browser(MobileRuntimeContext)
    /// The bundled account page covers the runtime it was opened from. That
    /// runtime keeps its document until an instance switch or an account
    /// transition releases it.
    case accountOverBrowser(MobileRuntimeContext)

    /// The runtime the root keeps alive, shown or covered by Account.
    var runtime: MobileRuntimeContext? {
        switch self {
        case .browser(let runtime), .accountOverBrowser(let runtime):
            return runtime
        case .loading, .onboarding:
            return nil
        }
    }

    /// The bundled page the root shows, above any covered runtime.
    var onboardingStartScreen: MobileOnboardingStartScreen? {
        switch self {
        case .onboarding(let startScreen):
            return startScreen
        case .accountOverBrowser, .loading, .browser:
            return nil
        }
    }

    var coversRuntime: Bool {
        if case .accountOverBrowser = self {
            return true
        }
        return false
    }
}

/// A request to open an instance page that arrived before the bootstrap
/// settled. Only the latest waits: a newer one replaces it.
private enum PendingInstanceOpen {
    case pushTap(PushNotificationTap)
    case selectedInstance(path: String)
    case liveActivity(URL)
}

private struct PendingPushOpened {
    let id: String
    let accountID: String
    let instanceOrigin: String
    let url: String
    let runtimeIdentity: Int
    let createdAt: Date
}

@MainActor
final class AppModel: ObservableObject {
    /// The total automatic budget of one opening attempt, measured in
    /// foreground time. It equals WebKit's own request timeout: the
    /// runtime's first load is a `URLRequest(url:)`, whose default
    /// `timeoutInterval` is 60 seconds, so a slow but healthy first load (a
    /// cold cache on a cellular network) is never cut off before WebKit
    /// itself would give up on it.
    static let runtimeOpenAutomaticBudget: Duration = .seconds(60)

    private final class PendingSystemSchedule {
        let id: UUID
        let request: SystemScheduleRequest
        let context: SystemScheduleContext
        let continuation: CheckedContinuation<SystemScheduleReceipt, Never>
        var retryContinuations: [(
            requestID: String,
            continuation: CheckedContinuation<SystemScheduleReceipt, Never>
        )] = []
        var expiryTask: Task<Void, Never>?
        var isSubmitting = false
        var isInvalidated = false

        init(
            id: UUID,
            request: SystemScheduleRequest,
            context: SystemScheduleContext,
            continuation: CheckedContinuation<SystemScheduleReceipt, Never>
        ) {
            self.id = id
            self.request = request
            self.context = context
            self.continuation = continuation
        }
    }

    private enum SystemScheduleContextResolution {
        case available(SystemScheduleContext)
        case localStateUnavailable
        case localStateWriteFailed(instanceID: String)
        case invalid
    }

    @Published private(set) var route: AppRoute = .loading
    /// Chosen once when the one runtime-preserving sheet opens. Repeated
    /// presentation requests never replace its document or change its mode.
    @Published private(set) var accountSheetStartScreen: MobileOnboardingStartScreen = .sheet
    /// The scheme the instance page on screen reports, or `nil` while it is
    /// unknown. The sheets over it take this appearance, so they match the
    /// page rather than the system when the two differ.
    @Published var runtimePageColorScheme: PageColorScheme?
    /// Advances each time a runtime opens, so the root builds a new runtime
    /// controller even when the same instance is selected again.
    @Published private(set) var runtimeIdentity = 0 {
        // A new or retired runtime has reported no scheme yet: its sheets
        // follow the system until it does.
        didSet { runtimePageColorScheme = nil }
    }
    /// The direct listener faulted in this process. One-way: only a new
    /// launch clears it.
    @Published private(set) var directListenerFault = false
    /// The one Native-owned opening attempt. It outlives `didFinish` as
    /// `documentLoaded` so callbacks keep their generation fence, but its
    /// automatic budget ends there because legacy pages have no ready signal.
    @Published private(set) var runtimeOpenAttempt: RuntimeOpenAttempt? {
        didSet { signpostRuntimeOpen(from: oldValue, to: runtimeOpenAttempt) }
    }
    /// Recovery shown over the instance list after an automatic timeout.
    @Published private(set) var runtimeOpenNotice: RuntimeOpenNotice?
    /// The instances' last pages, per account and origin, kept with the
    /// account's WebKit store: each store has its own directory, which goes
    /// when the account changes or signs out.
    var runtimePageCache: RuntimePageCache {
        RuntimePageCache(directory: RuntimePageCache.root
            .appendingPathComponent(websiteDataStore.identifier.uuidString, isDirectory: true))
    }
    @Published private(set) var lastRuntimeOpenTermination: RuntimeOpenTermination?
    @Published private(set) var systemScheduleConfirmation: SystemScheduleConfirmation?

    let websiteDataStore: AccountWebsiteDataStore
    /// The next open of this runtime loads live, skipping the cache once
    /// (Retry over a cached page that could not connect).
    private var runtimeLoadsLiveOnce: MobileRuntimeContext?
    /// This device's path choice per account and instance.
    private let directPathPreferences: DirectPathPreferenceStore
    /// Whether the device has a network: without one, an instance that
    /// cannot be reached says nothing about its path.
    private let networkPath: NetworkPathReading
    /// Where the next open of this runtime starts: a reopen after this
    /// device's path choice changed, at the page that changed it.
    private var runtimeStartURL: (runtime: MobileRuntimeContext, identity: Int, url: URL)?

    private let defaults: UserDefaults
    private let controlPlane: MobileControlPlaneConfiguration
    private let runtimeOpenTiming: any RuntimeOpenTiming
    private let runtimeOpenBudget: Duration
    private let runtimeOpenLongWait: Duration
    private let runtimeOpenNotificationCenter: NotificationCenter
    private var runtimeOpenLifecycleObservers: [NSObjectProtocol] = []
    private var runtimeOpenDeadlineTimer: (any RuntimeOpenTimer)?
    private var runtimeOpenLongWaitTimer: (any RuntimeOpenTimer)?
    /// When the App went to the background, on `runtimeOpenTiming`'s clock;
    /// `nil` in the foreground. WebKit is suspended there, so the opening
    /// budget is not charged for that time. It starts `nil`: the App declares
    /// no background modes, and a scene-based launch can still report
    /// `.background` before its scene is active, which would stop the clock
    /// for the whole first session.
    private var runtimeOpenBackgroundedAt: Duration?
    /// The background time already excluded from the opening budget.
    private var runtimeOpenBackgroundTime: Duration = .zero
    /// The direct transport, when this build has one. Without it there is no
    /// path controller: no `direct` capability, no listener, no route write,
    /// no event, and runtimes open exactly as they did before.
    private let directTransport: DirectTransporting?
    private let directFaultMarker: DirectFaultMarkerStoring
    /// A runtime whose open is held because its host has a route and the
    /// listener does not serve yet. One slot, not a queue: a newer open, a
    /// stop, or a sign-out clears it, and it is settled only for the
    /// identity it was made under.
    private var deferredOpen: (runtime: MobileRuntimeContext, identity: Int)?
    /// The identity of a runtime opened while the listener fault covered
    /// it: it shows only the reopen message, and no WebView is made for it.
    /// (A runtime the fault reaches later keeps its WebView under the
    /// message; the same gate value cannot tell the two apart.)
    private var directFaultOnlyIdentity: Int?
    /// A call of the fault marker's `mark()` has returned in this process.
    /// That is all it says: not that the marker is on disk.
    private var directFaultIsMarked = false
    let healthSync: HealthSyncCoordinator?

    private let pushKeychain: PushKeychainStoring
    private let pushGenerationStore: PushGenerationStoring
    private let pushInstallationMarker: PushInstallationMarkerStoring
    private let pushNotificationCenter: PushNotificationCentering?
    private let pushOpenedNow: () -> Date
    private let pushOpenedLifetime: TimeInterval
    /// The App icon badge: the selected instance's Inbox total, which its Web
    /// UI sets through `badge.set` (and a push sets while the App is away).
    private let systemScheduleCoordinator: SystemScheduleCoordinator
    var systemScheduleDeviceID: String? { systemScheduleCoordinator.installationID }
    private let systemScheduleNow: () -> Date
    private var pendingSystemSchedule: PendingSystemSchedule?
    private let nativeBadge: NativeBadge
    /// Retires an instance's Session notifications once nothing there is unread.
    private let sessionNotifications: SessionNotificationCleaner
    /// The runtime whose page has sent `notifications.reconcile`: from then
    /// on its unread set, not a zero badge, retires notifications.
    private var reconciledRuntimeIdentity: Int?
    private weak var runtimeController: WebViewController?
    private var pendingRuntimeReleaseBarriers: [
        WebsiteDataStoreReleaseBarrier
    ] = []
    private var hasCompletedBootstrap = false
    private var pendingInstanceOpen: PendingInstanceOpen?
    /// Advances with each open request after the bootstrap, so a shortcut
    /// that waited for a selection-keeping operation does not open after a
    /// newer request.
    private var instanceOpenRequest = 0
    private var pendingPushOpened: PendingPushOpened?
    private var pendingPushOpenedExpiryTask: Task<Void, Never>?

    private lazy var sessionCoordinator = MobileSessionCoordinator(
        defaults: defaults,
        controlPlane: controlPlane,
        websiteDataStore: websiteDataStore,
        transportFactory: { [deadlines = runtimeOpenTiming.accountTransportDeadlines] url, store in
            MobileAuthTransport(
                controlPlaneURL: url,
                websiteDataStore: store,
                deadlines: deadlines
            )
        },
        onOpenRuntime: { [weak self] runtime in
            _ = self?.openRuntime(runtime)
        },
        onPrepareAccountTransition: { [weak self] in
            self?.prepareForAccountTransition() ?? []
        },
        onDetachActiveOwner: { [weak self] in
            self?.systemScheduleCoordinator.detachActiveOwner() ?? false
        },
        onDetachActiveOwnerForAuthenticatedAccount: { [weak self] accountID in
            self?.systemScheduleCoordinator.detachActiveOwner(
                unlessAccountID: accountID
            ) ?? false
        },
        onAuthenticatedAccount: { [weak self] accountID in
            self?.systemScheduleCoordinator.synchronizeAccount(
                accountID: accountID
            ) ?? false
        },
        onHealthAccountEvent: { [weak self] event in
            self?.healthSync?.accountEvent(event)
        },
        onPreparePushLogout: { [weak self] reason in
            // Signing out or another account: the badge counted this
            // account's Inbox. (A session refresh of the same account keeps it.)
            self?.nativeBadge.clear()
            await self?.turnActivities.endAll(reason: reason == .accountChanged ? "account_switch" : "logout")
            await self?.nativePushCoordinator.prepareForLogout(reason: reason)
        },
        onReconcileInstancePush: { [weak self] liveOrigins, accountID in
            // Instances that left the account's list lose only their own
            // registrations; the account keeps the rest.
            await self?.turnActivities.removeInstances(outside: liveOrigins, accountID: accountID)
            await self?.nativePushCoordinator.revokeRegistrations(
                outside: liveOrigins,
                accountID: accountID
            )
        }
    )

    private lazy var turnActivities = TurnActivityCoordinator(defaults: defaults)

    private lazy var nativePushCoordinator = NativePushCoordinator(
        keychain: pushKeychain,
        generationStore: pushGenerationStore,
        managementRequest: { [weak self] request in
            guard let self else {
                throw NativePushError.unavailable
            }
            return try await self.mobileSessionCoordinator.pushManagementRequest(
                request
            )
        },
        accountProvider: { [weak self] in
            self?.mobileSessionCoordinator.currentAccount
        },
        installationMarker: pushInstallationMarker,
        notificationCenter: pushNotificationCenter,
        onTap: { [weak self] tap in
            self?.handlePushTap(tap)
        },
        onTapRecovery: { [weak self] in
            self?.showInstancePicker()
        }
    )

    private lazy var directPathController: DirectPathController? =
        directTransport.map { transport in
            DirectPathController(
                websiteDataStore: websiteDataStore,
                transport: transport,
                isCurrentRuntime: { [weak self] runtime in
                    self?.sessionCoordinator.isCurrentRuntime(runtime) ?? false
                },
                onStatusChanged: { [weak self] in
                    self?.deliverDirectStatus()
                },
                onFault: { [weak self] in
                    self?.directListenerDidFault()
                },
                onAvailabilityChanged: { [weak self] in
                    self?.directAvailabilityDidChange()
                }
            )
        }

    var mobileSessionCoordinator: MobileSessionCoordinator {
        sessionCoordinator
    }

    /// Answers `direct.status` for the runtime's WebViews; `nil` without a
    /// direct transport, so they offer no `direct` capability.
    var directStatusProvider: DirectStatusProvider? {
        guard directPathController != nil else {
            return nil
        }
        return { [weak self] origin, reply in
            guard let controller = self?.directPathController else {
                reply(nil)
                return
            }
            controller.status(for: origin, reply: reply)
        }
    }

    /// Answers `direct.handover` for the runtime's WebViews; `nil` without a
    /// direct transport.
    var directHandoverProvider: DirectHandoverProvider? {
        guard directPathController != nil else {
            return nil
        }
        return { [weak self] origin, soleDocument, reply in
            guard let controller = self?.directPathController else {
                reply(.skipped(.notUsable))
                return
            }
            controller.handover(for: origin, soleDocument: soleDocument, reply: reply)
        }
    }

    /// Answers `direct.preference` and `direct.setPreference` for the
    /// runtime's pages; `nil` without a direct transport, when there is
    /// nothing to choose between.
    var directPreferenceProvider: DirectPreferenceProvider? {
        guard directPathController != nil else {
            return nil
        }
        return DirectPreferenceProvider(
            read: { [weak self] origin in
                guard let self, let runtime = self.runtimeShowing(origin) else { return nil }
                return self.directPathPreference(for: runtime)
            },
            write: { [weak self] origin, mode, url in
                self?.setDirectPathPreference(mode, at: origin, reopeningAt: url)
            }
        )
    }

    var pushCoordinator: NativePushCoordinator {
        nativePushCoordinator
    }

    var runtimeWebsiteDataStore: WKWebsiteDataStore {
        websiteDataStore.dataStore
    }

    init(
        defaults: UserDefaults = .standard,
        controlPlane: MobileControlPlaneConfiguration =
            MobileControlPlaneConfiguration(),
        directTransport: DirectTransporting? = nil,
        directFaultMarker: DirectFaultMarkerStoring? = nil,
        pushKeychain: PushKeychainStoring = PushKeychainStore(),
        pushGenerationStore: PushGenerationStoring =
            SharedPushGenerationStore(),
        pushInstallationMarker: PushInstallationMarkerStoring =
            UserDefaultsPushInstallationMarkerStore(),
        pushNotificationCenter: PushNotificationCentering? = nil,
        pushOpenedNow: @escaping () -> Date = Date.init,
        pushOpenedLifetime: TimeInterval = 120,
        systemSchedulePlatform: SystemSchedulePlatforming? = nil,
        systemScheduleStore: SystemScheduleStoring? = nil,
        systemScheduleNow: @escaping () -> Date = Date.init,
        runtimeOpenTiming: (any RuntimeOpenTiming)? = nil,
        runtimeOpenBudget: Duration = AppModel.runtimeOpenAutomaticBudget,
        runtimeOpenLongWait: Duration = .seconds(8),
        runtimeOpenNotificationCenter: NotificationCenter = .default,
        badgeCenter: AppBadgeCentering? = nil,
        healthSync: HealthSyncCoordinator? = nil,
        networkPath: NetworkPathReading? = nil
    ) {
        self.defaults = defaults
        self.directPathPreferences = DirectPathPreferenceStore(defaults: defaults)
        self.networkPath = networkPath ?? SystemNetworkPath()
        self.healthSync = healthSync
        self.controlPlane = controlPlane
        let directFaultMarker = directFaultMarker
            ?? UserDefaultsDirectFaultMarker(defaults: defaults)
        self.directFaultMarker = directFaultMarker
        // A launch after a listener fault uses no listener at all, decided
        // here, before any route or first load: the user was asked to
        // reopen the App, and this launch must work. It clears the marker,
        // so the launch after it uses the transport again. Without a
        // transport the marker is never read.
        if directTransport != nil, directFaultMarker.take() {
            self.directTransport = nil
        } else {
            self.directTransport = directTransport
        }
        self.pushKeychain = pushKeychain
        self.pushGenerationStore = pushGenerationStore
        self.pushInstallationMarker = pushInstallationMarker
        self.pushNotificationCenter = pushNotificationCenter
        self.pushOpenedNow = pushOpenedNow
        self.pushOpenedLifetime = pushOpenedLifetime
        self.systemScheduleNow = systemScheduleNow
        self.systemScheduleCoordinator = SystemScheduleCoordinator(
            platform: systemSchedulePlatform ?? LiveSystemSchedulePlatform(),
            store: systemScheduleStore ?? FileSystemSystemScheduleStore(
                legacyEvidence: SystemScheduleDeviceIdentity.legacyEvidence(defaults: defaults)
            ),
            now: systemScheduleNow
        )
        self.runtimeOpenTiming = runtimeOpenTiming ?? ContinuousRuntimeOpenTiming()
        self.runtimeOpenBudget = runtimeOpenBudget
        self.runtimeOpenLongWait = runtimeOpenLongWait
        self.runtimeOpenNotificationCenter = runtimeOpenNotificationCenter
        nativeBadge = NativeBadge(center: badgeCenter ?? SystemAppBadgeCenter())
        sessionNotifications = SessionNotificationCleaner(
            center: pushNotificationCenter ?? SystemPushNotificationCenter()
        )
        healthSync?.captureStartupReadiness()
        websiteDataStore = AccountWebsiteDataStore(defaults: defaults)
        healthSync?.websiteDataStoreIDProvider = { [weak self] in self?.websiteDataStore.identifier }
        healthSync?.accountIDProvider = { [weak self] in
            self?.sessionCoordinator.healthAccountID
        }
        healthSync?.targetProvider = { [weak self] in
            guard let self, let accountID = self.sessionCoordinator.healthAccountID,
                  let instance = self.sessionCoordinator.selectedInstance,
                  let origin = WebOrigin(url: instance.url) else { return nil }
            return HealthConnectionTarget(accountID: accountID, instanceID: instance.id, name: instance.name, origin: origin.serialized)
        }
        // The list avibe.bot answered for the account (`healthAccountID` is
        // set only by its answer in this process); unknown otherwise.
        healthSync?.instancesProvider = { [weak self] in
            guard let self, self.sessionCoordinator.healthAccountID != nil else { return nil }
            var listed: [String: String] = [:]
            for instance in self.sessionCoordinator.currentInstances {
                if let origin = WebOrigin(url: instance.url) { listed[instance.id] = origin.serialized }
            }
            return listed
        }
        _ = nativePushCoordinator
        _ = directPathController
        // The Action Button's hidden WebView loads through the same local
        // listener as the page, so it asks the same gate before its load.
        VoiceCaptureLoadGate.provider = { [weak self] origin, storeIdentifier in
            guard let self, let controller = self.directPathController else {
                return .open
            }
            // Asked by the take's own host, with no runtime: a take kept
            // for an instance that is no longer the one shown still goes
            // through the listener its host is routed to.
            return VoiceCaptureLoadGate.gate(
                for: origin, store: storeIdentifier, currentStore: self.websiteDataStore.identifier
            ) { url in
                controller.settleRecordedFault()
                return controller.loadGate(forInstanceURL: url)
            }
        }
        runtimeOpenLifecycleObservers = [
            runtimeOpenNotificationCenter.addObserver(
                forName: UIApplication.didEnterBackgroundNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.runtimeOpenDidEnterBackground()
                }
            },
            runtimeOpenNotificationCenter.addObserver(
                forName: UIApplication.willEnterForegroundNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.runtimeOpenWillEnterForeground()
                }
            }
        ]

        Task { [weak self] in
            await self?.systemScheduleCoordinator.retryPendingCleanup()
            await self?.bootstrap()
        }
    }

    deinit {
        pendingSystemSchedule?.expiryTask?.cancel()
        for observer in runtimeOpenLifecycleObservers {
            runtimeOpenNotificationCenter.removeObserver(observer)
        }
    }

    func registerRuntimeController(
        _ controller: WebViewController,
        for runtime: MobileRuntimeContext,
        attemptID: Int? = nil
    ) {
        let attemptID = attemptID ?? runtimeIdentity
        // The view makes its WebView right after the open, while the push
        // update that open started may already be running: that request
        // keeps the selection, so it does not abandon the browser.
        guard
            attemptID == runtimeIdentity,
            runtimeOpenAttempt?.id == attemptID,
            runtimeOpenAttempt?.runtime == runtime,
            route == .browser(runtime),
            sessionCoordinator.keepsSelectedRuntime(runtime)
        else {
            recordRuntimeReleaseBarrier(
                controller.invalidateForAccountTransition()
            )
            if runtimeOpenAttempt?.id == attemptID,
               route == .browser(runtime)
            {
                // This browser is abandoned without `stopRuntime()`, so the
                // direct selection made for it ends here. A WebView that
                // arrives late for an older runtime does not reach this
                // branch and leaves the newer runtime's selection alone.
                retireRuntimeOpenForAccountTransition()
                directPathController?.runtimeDidStop()
                route = .onboarding(.instances)
            }
            return
        }
        runtimeController = controller
    }

    func recordRuntimeReleaseBarrier(
        _ barrier: WebsiteDataStoreReleaseBarrier
    ) {
        pendingRuntimeReleaseBarriers.removeAll(where: \.isReleased)
        guard !barrier.isReleased else {
            return
        }
        pendingRuntimeReleaseBarriers.append(barrier)
    }

    /// Covers the live runtime with the bundled instances sheet. The runtime
    /// remains selected and its document is not released, including when the
    /// runtime currently shows its native load-failure screen.
    func showAccount() {
        showRuntimeSheet(startScreen: .sheet, fallback: .account)
    }

    /// A Google, GitHub or Apple sign-in tapped inside an instance page: that page asked avibe.bot to
    /// sign in, so the App's avibe.bot session has lapsed. Reading it settles that the
    /// explicit way (a `401` signs out and the App shows its own sign-in screen, which offers
    /// Google, GitHub and Apple); a session that still reads signed in shows the instance list.
    func showSignIn() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let state = try? await self.sessionCoordinator.refresh()
            if case .signedIn = state {
                self.showInstancePicker()
            }
        }
    }

    func showAccountRoot() {
        showRuntimeSheet(startScreen: .sheetAccount, fallback: .account)
    }

    func showInstances() {
        showRuntimeSheet(startScreen: .sheetInstances, fallback: .instances)
    }

    /// App metadata is read by the Runtime on document load. The catalog,
    /// rather than the runtime's creation-time snapshot, owns its display name.
    func connectHealth(requestID: String, pairingCode: String, origin: WebOrigin) async -> String {
        guard !route.coversRuntime, let healthSync else { return "failed" }
        return await healthSync.connect(requestID: requestID, pairingCode: pairingCode, origin: origin)
    }

    private var liveActivityTarget: TurnActivityCoordinator.Target? {
        guard let account = sessionCoordinator.currentAccount,
              let instance = sessionCoordinator.selectedInstance,
              let origin = WebOrigin(url: instance.url) else { return nil }
        return .init(accountID: account.id, instanceOrigin: origin.serialized, storeID: websiteDataStore.identifier)
    }

    func handleLiveActivity(_ request: NativeBridgeRequest, origin: WebOrigin) async throws -> [String: Any] {
        guard let runtime = route.runtime, sessionCoordinator.isSelectedRuntime(runtime),
              let target = liveActivityTarget, target.instanceOrigin == origin.serialized else {
            throw NativeBridgeError.forbidden
        }
        if request.method == .liveActivityOpenSettings {
            guard request.params.isEmpty else { throw NativeBridgeError.invalidRequest }
            await UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
            return ["ok": true]
        }
        guard let sessionID = request.params["sessionId"] as? String,
              !sessionID.isEmpty, sessionID.count <= 256 else { throw NativeBridgeError.invalidRequest }
        switch request.method {
        case .liveActivityStatus:
            guard request.params.count == 1 else { throw NativeBridgeError.invalidRequest }
            return turnActivities.status(sessionID: sessionID, target: target)
        case .liveActivityStop:
            guard request.params.count == 1 else { throw NativeBridgeError.invalidRequest }
            await turnActivities.stop(sessionID: sessionID, target: target)
            return ["ok": true]
        case .liveActivityStart:
            guard request.params.count == 3, let name = request.params["agentName"] as? String,
                  !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  request.params["sessionTitle"] is String || request.params["sessionTitle"] is NSNull else {
                throw NativeBridgeError.invalidRequest
            }
            return await turnActivities.start(sessionID: sessionID, agentName: name,
                sessionTitle: request.params["sessionTitle"] as? String, target: target,
                registrationID: nativePushCoordinator.liveActivityRegistrationID(
                    accountID: target.accountID, instanceOrigin: target.instanceOrigin))
        default: throw NativeBridgeError.unsupported
        }
    }

    func openLiveActivity(_ url: URL) {
        guard TurnActivityAttributes.activityID(from: url) != nil else { return }
        guard hasCompletedBootstrap else { pendingInstanceOpen = .liveActivity(url); return }
        guard let attributes = turnActivities.attributes(for: url) else { return }
        let pathComponent = attributes.sessionID.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        if let target = liveActivityTarget, target.accountID == attributes.accountID,
           target.instanceOrigin == attributes.instanceOrigin {
            openSelectedInstance(path: "/chat/" + pathComponent)
        } else {
            _ = openInstance(accountID: attributes.accountID, instanceOrigin: attributes.instanceOrigin,
                             path: "/chat/" + pathComponent)
        }
    }

    func selectedInstanceName() -> String? {
        guard let runtime = route.runtime,
              sessionCoordinator.isSelectedRuntime(runtime) else { return nil }
        return sessionCoordinator.currentInstances.first {
            $0.id == runtime.instance.id
                && WebOrigin(url: $0.url) == WebOrigin(url: runtime.instance.url)
        }?.name
    }

    func systemScheduleCapabilities(origin: WebOrigin) -> [String: Any]? {
        guard currentSystemScheduleContext(origin: origin) != nil else {
            return nil
        }
        return systemScheduleCoordinator.capabilities().jsonObject
    }

    /// Called synchronously by NativeBridge admission, before its Task exists.
    func captureSystemScheduleAdmission(origin: WebOrigin) -> SystemScheduleAdmission? {
        switch resolveCurrentSystemScheduleContext(origin: origin) {
        case .available(let context): return .authorized(context)
        case .localStateUnavailable: return .localStateUnavailable
        case .localStateWriteFailed(let instanceID):
            // A live request already has an immutable admitted context. Keep
            // it only for cancel control, after checking the same document
            // authority again; create still returns the typed write failure.
            let controlContext = pendingSystemSchedule.flatMap {
                isCurrentSystemScheduleContext($0.context) ? $0.context : nil
            }
            return .localStateWriteFailed(instanceID: instanceID, controlContext: controlContext)
        case .invalid: return nil
        }
    }

    private func isCurrentSystemScheduleContext(_ context: SystemScheduleContext) -> Bool {
        guard let url = URL(string: context.origin),
              let origin = WebOrigin(url: url, requiresHTTPS: false),
              case .browser(let runtime) = route,
              sessionCoordinator.isSelectedRuntime(runtime),
              runtime.account.id == context.accountID,
              sessionCoordinator.currentAccount?.id == context.accountID,
              runtime.instance.id == context.instanceID,
              WebOrigin(url: runtime.instance.url) == origin,
              runtimeIdentity == context.runtimeIdentity,
              runtimeController?.primaryScheduleWebViewIdentity == context.webViewIdentity,
              runtimeController?.trustedPrimaryScheduleNavigationEpoch(origin: origin) == context.navigationEpoch
        else { return false }
        return systemScheduleCoordinator.isActive(owner: context.owner)
    }

    func requestSystemSchedule(
        _ request: SystemScheduleRequest,
        admission: SystemScheduleAdmission
    ) async -> [String: Any] {
        let context: SystemScheduleContext
        switch admission {
        case .authorized(let captured): context = captured
        case .localStateUnavailable:
            return systemScheduleCoordinator.localStateUnavailable(request).jsonObject
        case .localStateWriteFailed:
            return systemScheduleCoordinator.localStateWriteFailed(request).jsonObject
        }
        if let failure = systemScheduleCoordinator.persistenceFailure(request, context: context) {
            return failure.jsonObject
        }
        guard request.instanceID == context.instanceID, isCurrentSystemScheduleContext(context) else {
            return systemScheduleReceipt(
                request,
                state: .staleContext,
                code: "stale_context",
                message: "The request no longer belongs to the selected account, instance, and Runtime."
            ).jsonObject
        }
        Task { @MainActor in
            await systemScheduleCoordinator.retryPendingCleanup(
                excludingOperationID: request.operationID, context: context)
        }
        if let existing = systemScheduleCoordinator.prepare(
            request,
            context: context
        ) {
            return existing.jsonObject
        }
        if let pending = pendingSystemSchedule {
            if pending.request.operationID == request.operationID {
                guard pending.request.hasSameIntent(as: request),
                      pending.context.hasSameOwner(as: context)
                else {
                    return systemScheduleReceipt(
                        request,
                        state: .failed,
                        code: "operation_conflict",
                        message: "This operation ID already belongs to a different system schedule."
                    ).jsonObject
                }
                if pending.isSubmitting {
                    return await systemScheduleCoordinator.schedule(
                        request, context: context,
                        creationFence: { [weak self] in self?.systemScheduleCreationFence(pending) ?? .staleContext },
                        postWriteFence: { [weak self] in self?.systemSchedulePostWriteFence(pending) ?? .staleContext }
                    ).jsonObject
                }
                let receipt = await withCheckedContinuation {
                    (continuation: CheckedContinuation<SystemScheduleReceipt, Never>) in
                    pending.retryContinuations.append(
                        (requestID: request.requestID, continuation: continuation)
                    )
                }
                return receipt.jsonObject
            }
            return systemScheduleReceipt(
                request,
                state: .failed,
                code: "confirmation_in_progress",
                message: "Confirm or cancel the system schedule already shown on this iPhone."
            ).jsonObject
        }
        if systemScheduleCoordinator.requiresRecovery(request, context: context) {
            return await recoverSystemSchedule(request, context: context).jsonObject
        }
        let now = systemScheduleNow()
        guard request.expiresAt > now else {
            return systemScheduleCoordinator.finishWithoutEffect(
                request,
                context: context,
                state: .expired,
                code: "expired",
                message: "The system schedule request expired before confirmation."
            ).jsonObject
        }
        guard request.fireAt > now else {
            return systemScheduleCoordinator.finishWithoutEffect(
                request,
                context: context,
                state: .failed,
                code: "fire_time_in_past",
                message: "The requested time has already passed."
            ).jsonObject
        }

        let receipt = await withCheckedContinuation {
            (continuation: CheckedContinuation<SystemScheduleReceipt, Never>) in
            let id = UUID()
            let pending = PendingSystemSchedule(
                id: id,
                request: request,
                context: context,
                continuation: continuation
            )
            pendingSystemSchedule = pending
            systemScheduleConfirmation = SystemScheduleConfirmation(
                id: id,
                request: request,
                instanceName: selectedInstanceName() ?? request.instanceID,
                isSubmitting: false
            )
            let delay = max(0, request.expiresAt.timeIntervalSince(now))
            pending.expiryTask = Task { @MainActor [weak self, weak pending] in
                try? await Task.sleep(for: .seconds(delay))
                guard !Task.isCancelled,
                      let self,
                      let pending,
                      self.pendingSystemSchedule === pending
                else {
                    return
                }
                if pending.isSubmitting {
                    return
                }
                self.finishSystemSchedule(
                    pending,
                    receipt: self.systemScheduleCoordinator.finishWithoutEffect(
                        pending.request,
                        context: pending.context,
                        state: .expired,
                        code: "expired",
                        message: "The system schedule request expired before it was created."
                    )
                )
            }
        }
        return receipt.jsonObject
    }

    private func recoverSystemSchedule(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext
    ) async -> SystemScheduleReceipt {
        await withCheckedContinuation {
            (continuation: CheckedContinuation<SystemScheduleReceipt, Never>) in
            let pending = PendingSystemSchedule(
                id: UUID(),
                request: request,
                context: context,
                continuation: continuation
            )
            pending.isSubmitting = true
            pendingSystemSchedule = pending
            Task { @MainActor [weak self, weak pending] in
                guard let self, let pending else { return }
                let receipt = await self.systemScheduleCoordinator.schedule(
                    pending.request,
                    context: pending.context,
                    creationFence: { [weak self, weak pending] in
                        guard let self, let pending else { return .staleContext }
                        return self.systemScheduleCreationFence(pending)
                    },
                    postWriteFence: { [weak self, weak pending] in
                        guard let self, let pending else { return .staleContext }
                        return self.systemSchedulePostWriteFence(pending)
                    }
                )
                guard self.pendingSystemSchedule === pending else {
                    return
                }
                self.finishSystemSchedule(pending, receipt: receipt)
            }
        }
    }

    func cancelSystemSchedule(
        _ request: SystemScheduleCancelRequest,
        admission: SystemScheduleAdmission
    ) async -> [String: Any]? {
        let context: SystemScheduleContext
        switch admission {
        case .authorized(let captured): context = captured
        case .localStateUnavailable:
            return systemScheduleCoordinator.localStateUnavailable(request).jsonObject
        case .localStateWriteFailed(let instanceID, let controlContext):
            guard let controlContext else {
                return systemScheduleCoordinator.localStateWriteFailed(request, instanceID: instanceID).jsonObject
            }
            context = controlContext
        }
        guard isCurrentSystemScheduleContext(context) else {
            // A rejected caller never sends control to a slot. Preserve an
            // already unresolved write outcome without treating it as success.
            return systemScheduleCoordinator.persistenceFailure(request, context: context)?.jsonObject
        }
        let pending = pendingSystemSchedule.flatMap { candidate in
            candidate.request.operationID == request.operationID
                && candidate.request.kind == request.kind
                && candidate.context.hasSameOwner(as: context) ? candidate : nil
        }
        // Presentation revocation is immediate; execution/commit settlement is
        // owned by the coordinator, including each cancel request's waiter.
        if let pending {
            pending.isInvalidated = true
            pending.expiryTask?.cancel()
            pending.expiryTask = nil
            systemScheduleConfirmation = nil
        }
        Task { @MainActor in
            await systemScheduleCoordinator.retryPendingCleanup(
                excludingOperationID: request.operationID, context: context)
        }
        let receipt = await systemScheduleCoordinator.cancel(
            request,
            context: context,
            stillCurrent: { [weak self] in self?.isCurrentSystemScheduleContext(context) == true }
        )
        if let pending, !pending.isSubmitting {
            finishSystemSchedule(pending, receipt: receipt.replacingRequestID(pending.request.requestID))
        }
        return receipt.jsonObject
    }

    func confirmSystemSchedule(id: UUID) {
        guard let pending = pendingSystemSchedule,
              pending.id == id,
              !pending.isSubmitting
        else {
            return
        }
        guard systemScheduleStillCurrent(pending) else {
            finishSystemSchedule(
                pending,
                receipt: systemScheduleCoordinator.finishWithoutEffect(
                    pending.request,
                    context: pending.context,
                    state: .staleContext,
                    code: "stale_context",
                    message: "The account, instance, or Runtime changed before confirmation."
                )
            )
            return
        }
        let now = systemScheduleNow()
        guard pending.request.expiresAt > now else {
            finishSystemSchedule(
                pending,
                receipt: systemScheduleCoordinator.finishWithoutEffect(
                    pending.request,
                    context: pending.context,
                    state: .expired,
                    code: "expired",
                    message: "The system schedule request expired before it was created."
                )
            )
            return
        }
        guard pending.request.fireAt > now else {
            finishSystemSchedule(
                pending,
                receipt: systemScheduleCoordinator.finishWithoutEffect(
                    pending.request,
                    context: pending.context,
                    state: .failed,
                    code: "fire_time_in_past",
                    message: "The requested time has already passed."
                )
            )
            return
        }
        pending.expiryTask?.cancel()
        pending.expiryTask = nil
        pending.isSubmitting = true
        systemScheduleConfirmation = SystemScheduleConfirmation(
            id: pending.id,
            request: pending.request,
            instanceName: systemScheduleConfirmation?.instanceName
                ?? pending.request.instanceID,
            isSubmitting: true
        )
        Task { @MainActor [weak self, weak pending] in
            guard let self, let pending else { return }
            let receipt = await self.systemScheduleCoordinator.schedule(
                pending.request,
                context: pending.context,
                creationFence: { [weak self, weak pending] in
                    guard let self, let pending else { return .staleContext }
                    return self.systemScheduleCreationFence(pending)
                },
                postWriteFence: { [weak self, weak pending] in
                    guard let self, let pending else { return .staleContext }
                    return self.systemSchedulePostWriteFence(pending)
                }
            )
            guard self.pendingSystemSchedule === pending else {
                return
            }
            self.finishSystemSchedule(pending, receipt: receipt)
        }
    }

    func denySystemSchedule(id: UUID) {
        guard let pending = pendingSystemSchedule,
              pending.id == id,
              !pending.isSubmitting
        else {
            return
        }
        finishSystemSchedule(
            pending,
            receipt: systemScheduleCoordinator.finishWithoutEffect(
                pending.request,
                context: pending.context,
                state: .userDenied,
                code: "user_denied",
                message: "The user did not approve this system schedule."
            )
        )
    }

    private func currentSystemScheduleContext(
        origin: WebOrigin,
        instanceID: String? = nil
    ) -> SystemScheduleContext? {
        guard case .available(let context) = resolveCurrentSystemScheduleContext(
            origin: origin,
            instanceID: instanceID
        ) else {
            return nil
        }
        return context
    }

    private func resolveCurrentSystemScheduleContext(
        origin: WebOrigin,
        instanceID: String? = nil
    ) -> SystemScheduleContextResolution {
        guard
            case .browser(let runtime) = route,
            sessionCoordinator.isSelectedRuntime(runtime),
            instanceID == nil || runtime.instance.id == instanceID,
            WebOrigin(url: runtime.instance.url)?.serialized == origin.serialized,
            let navigationEpoch = runtimeController?
                .trustedPrimaryScheduleNavigationEpoch(origin: origin),
            sessionCoordinator.currentAccount?.id == runtime.account.id
        else {
            return .invalid
        }
        guard let owner = systemScheduleCoordinator.managementOwner(
            accountID: runtime.account.id
        ) else {
            if systemScheduleCoordinator.persistenceState == .writeFailed {
                return .localStateWriteFailed(instanceID: runtime.instance.id)
            }
            return .localStateUnavailable
        }
        return .available(
            SystemScheduleContext(
                owner: owner,
                instanceID: runtime.instance.id,
                origin: origin.serialized,
                runtimeIdentity: runtimeIdentity,
                navigationEpoch: navigationEpoch,
                webViewIdentity: runtimeController?.primaryScheduleWebViewIdentity
            )
        )
    }

    private func systemScheduleStillCurrent(
        _ pending: PendingSystemSchedule
    ) -> Bool {
        pendingSystemSchedule === pending && !pending.isInvalidated
            && isCurrentSystemScheduleContext(pending.context)
    }

    private func systemScheduleCreationFence(
        _ pending: PendingSystemSchedule
    ) -> SystemScheduleFenceFailure? {
        guard systemScheduleStillCurrent(pending) else {
            return .staleContext
        }
        let now = systemScheduleNow()
        guard pending.request.expiresAt > now else { return .expired }
        guard pending.request.fireAt > now else { return .fireTimeInPast }
        return nil
    }

    private func systemSchedulePostWriteFence(
        _ pending: PendingSystemSchedule
    ) -> SystemScheduleFenceFailure? {
        return systemScheduleStillCurrent(pending) ? nil : .staleContext
    }

    private func systemScheduleReceipt(
        _ request: SystemScheduleRequest,
        state: SystemScheduleState,
        code: String?,
        message: String?
    ) -> SystemScheduleReceipt {
        let capabilities = systemScheduleCoordinator.capabilities()
        let authorization = request.kind == .alarm
            ? capabilities.alarmAuthorization
            : capabilities.remindersAuthorization
        return SystemScheduleReceipt.terminal(
            request: request,
            state: state,
            authorization: authorization,
            code: code,
            message: message
        )
    }

    private func invalidatePendingSystemSchedule() {
        guard let pending = pendingSystemSchedule else { return }
        if pending.isSubmitting {
            pending.expiryTask?.cancel()
            pending.expiryTask = nil
            pending.isInvalidated = true
            systemScheduleConfirmation = nil
            return
        }
        finishSystemSchedule(
            pending,
            receipt: systemScheduleCoordinator.finishWithoutEffect(
                pending.request,
                context: pending.context,
                state: .staleContext,
                code: "stale_context",
                message: "The account, instance, or Runtime changed before the system schedule was created."
            )
        )
    }

    private func finishSystemSchedule(
        _ pending: PendingSystemSchedule,
        receipt: SystemScheduleReceipt
    ) {
        guard pendingSystemSchedule === pending else { return }
        pending.expiryTask?.cancel()
        pending.expiryTask = nil
        pendingSystemSchedule = nil
        systemScheduleConfirmation = nil
        pending.continuation.resume(returning: receipt)
        for retry in pending.retryContinuations {
            retry.continuation.resume(
                returning: receipt.replacingRequestID(retry.requestID)
            )
        }
        pending.retryContinuations.removeAll()
    }

    private func showRuntimeSheet(
        startScreen: MobileOnboardingStartScreen,
        fallback: MobileOnboardingStartScreen
    ) {
        invalidatePendingSystemSchedule()
        switch route {
        case .accountOverBrowser:
            return
        case .browser(let runtime)
            where sessionCoordinator.isSelectedRuntime(runtime):
            runtimeController?.prepareForAccountCover()
            accountSheetStartScreen = startScreen
            route = .accountOverBrowser(runtime)
        default:
            retireRuntimeOpenForAccountTransition()
            stopRuntime()
            route = .onboarding(fallback)
        }
    }

    /// Shows the runtime the account/instances surface covers again, unchanged.
    /// A failed runtime may not have a controller, but it is still a valid
    /// selected route and must return to its failure screen.
    func returnToRuntime() -> Bool {
        guard
            case .accountOverBrowser(let runtime) = route,
            sessionCoordinator.isSelectedRuntime(runtime)
        else {
            return false
        }
        sessionCoordinator.leaveAccountSurface()
        route = .browser(runtime)
        return true
    }

    func showInstancePicker() {
        clearPendingPushOpened()
        if case .browser(let runtime) = route,
           sessionCoordinator.isSelectedRuntime(runtime) {
            showAccount()
            return
        }
        if case .accountOverBrowser = route {
            return
        }
        retireRuntimeOpenForAccountTransition()
        stopRuntime()
        route = .onboarding(.instances)
    }

    /// Cold start timing in Instruments (os_signpost, category ColdStart):
    /// the bootstrap and its session restore, and each runtime open from its
    /// attempt to its first document, with an event per stage.
    private static let coldStartSignposter = OSSignposter(
        subsystem: Bundle.main.bundleIdentifier ?? "bot.avibe.ios",
        category: "ColdStart"
    )
    private var runtimeOpenSignpost: OSSignpostIntervalState?

    private func signpostRuntimeOpen(
        from old: RuntimeOpenAttempt?,
        to new: RuntimeOpenAttempt?
    ) {
        let signposter = Self.coldStartSignposter
        if old?.id != new?.id, let state = runtimeOpenSignpost {
            signposter.endInterval("runtime open", state, "ended")
            runtimeOpenSignpost = nil
        }
        guard let new else {
            return
        }
        if old?.id != new.id {
            runtimeOpenSignpost = signposter.beginInterval("runtime open")
        }
        if old?.id != new.id || old?.phase != new.phase {
            signposter.emitEvent("runtime open stage", "\(String(describing: new.phase), privacy: .public)")
        }
        if new.phase == .documentLoaded || new.phase == .failed,
           let state = runtimeOpenSignpost {
            signposter.endInterval("runtime open", state, "\(String(describing: new.phase), privacy: .public)")
            runtimeOpenSignpost = nil
        }
    }

    private func bootstrap() async {
        let launch = Self.coldStartSignposter.beginInterval("bootstrap")
        defer { Self.coldStartSignposter.endInterval("bootstrap", launch) }
        await websiteDataStore.sweepOrphans()
        // Open first: with the last session avibe.bot confirmed and its
        // selected instance on this device, open that instance now and check
        // the session behind it. The check's answer decides as a refresh
        // does (another account, a signed-out session, or the instance gone
        // closes the runtime; a failed check keeps it).
        // A launch from a notification tap or a shortcut opens what it
        // names, against a session checked first, as before.
        if
            pendingInstanceOpen == nil,
            let restored = sessionCoordinator.restoreLastGoodSession(
                opensOffline: hasUsableCachedPage(for:)
            ),
            let runtime = sessionCoordinator.restorableRuntime(
                from: restored,
                opensOffline: hasUsableCachedPage(for:)
            )
        {
            Self.coldStartSignposter.emitEvent("opened before the session check")
            openRuntime(runtime)
            let check = Self.coldStartSignposter.beginInterval("session check")
            let checked = try? await sessionCoordinator.refresh()
            Self.coldStartSignposter.endInterval("session check", check)
            settleOpenFirst(runtime, checked: checked)
            // A tap or shortcut that arrived meanwhile waited for the
            // check, so it opens against the answered session.
            await completeBootstrap()
            return
        }
        sessionCoordinator.activateAccountSurface()
        do {
            let restore = Self.coldStartSignposter.beginInterval("session restore")
            let state: MobileSessionState
            do {
                defer { Self.coldStartSignposter.endInterval("session restore", restore) }
                state = try await sessionCoordinator.bootstrap()
            }
            if let runtime = sessionCoordinator.restorableRuntime(
                from: state,
                opensOffline: hasUsableCachedPage(for:)
            ) {
                sessionCoordinator.deactivateAccountSurface()
                openRuntime(runtime)
                // Opened although listed offline (only for its cached page):
                // the confirmed listing applies as after an open-first.
                settleListedAvailability(runtime, checked: state)
            } else {
                route = .onboarding(.instances)
            }
        } catch {
            // Bootstrap failure is not authenticated sign-out evidence. Keep
            // the persisted schedule owner unchanged until a later bootstrap
            // confirms signed-out or a different account.
            route = .onboarding(.instances)
        }
        await completeBootstrap()
    }

    /// After an open-first's session check: push work the open started was
    /// held off by the check (the account was busy), so it resumes now; and
    /// a confirmed list that shows the instance offline is applied to the
    /// open (`settleListedAvailability`).
    private func settleOpenFirst(
        _ runtime: MobileRuntimeContext,
        checked: MobileSessionState?
    ) {
        guard sessionCoordinator.isCurrentRuntime(runtime) else {
            return
        }
        nativePushCoordinator.resumeAfterAccountActivation()
        settleListedAvailability(runtime, checked: checked)
    }

    /// A session read listed the instance being opened offline. The same
    /// situation always ends the same way, whenever the answer arrives: the
    /// open stays only for a cached page that keeps its shell offline
    /// (marked, declaring a replayed shell, not at setup, this account,
    /// store and origin, within a day), shown uncovered with the offline
    /// row; an open that may still show one waits for it; anything else
    /// ends at the instance list, as before.
    private func settleListedAvailability(
        _ runtime: MobileRuntimeContext,
        checked: MobileSessionState?
    ) {
        guard
            case .signedIn(let snapshot, _) = checked,
            let listed = snapshot.instances.first(where: { $0.id == runtime.instance.id }),
            !listed.available,
            var attempt = runtimeOpenAttempt,
            attempt.runtime == runtime,
            // Not the route: an open preparing its direct route shows the
            // loading screen, and is settled the same way.
            sessionCoordinator.isSelectedRuntime(runtime),
            attempt.phase != .documentLoaded,
            attempt.phase != .cachedOffline
        else {
            return
        }
        if attempt.phase == .showingCachedLive || attempt.phase == .cachedUnreachable {
            showCachedPageOffline(attemptID: attempt.id)
        } else if isWaitingForCachedPage(attempt), hasUsableCachedPage(for: runtime) {
            // The cached page is still to show, or to say whether it keeps
            // its shell: the open waits for that (`handleRuntimeNavigation`,
            // `runtimeCachedPageKeepsShell`).
            attempt.listedOffline = true
            runtimeOpenAttempt = attempt
        } else {
            cancelRuntimeOpen(attemptID: attempt.id)
        }
    }

    /// Nothing live has started, and no cached page has shown or declined:
    /// the open may still show one.
    private func isWaitingForCachedPage(_ attempt: RuntimeOpenAttempt) -> Bool {
        !attempt.startedLive
            && [.preparingRoute, .connecting, .showingCached].contains(attempt.phase)
    }

    /// Whether the cache holds a page for `runtime` that may be shown
    /// uncovered: marked as keeping its shell offline, at most a day old,
    /// and not skipped for this open. Whether its Web UI replays an outcome
    /// is known only once it has loaded.
    private func hasUsableCachedPage(for runtime: MobileRuntimeContext) -> Bool {
        guard
            runtimeLoadsLiveOnce != runtime,
            let origin = WebOrigin(url: runtime.instance.url)?.serialized
        else {
            return false
        }
        let cache = RuntimePageCache(directory: RuntimePageCache.root
            .appendingPathComponent(runtime.websiteDataStoreID.uuidString, isDirectory: true))
        guard let page = cache.page(accountID: runtime.account.id, origin: origin) else {
            return false
        }
        let age = Date().timeIntervalSince(page.capturedAt)
        return page.keepsShell && age >= 0 && age < RuntimePageCache.maximumAge
    }

    private func showCachedPageOffline(attemptID: Int) {
        guard var attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return
        }
        attempt.listedOffline = true
        runtimeOpenAttempt = attempt
        // No timeout: the page stays until the instance answers it, or the
        // user retries or switches.
        finishRuntimeOpenAutomaticBudget(phase: .cachedOffline, attemptID: attemptID)
    }

    private func completeBootstrap() async {
        hasCompletedBootstrap = true
        // Navigation requested before bootstrap is delivered before Health
        // restoration, which may wait on the network and HealthKit.
        if let pendingInstanceOpen {
            self.pendingInstanceOpen = nil
            switch pendingInstanceOpen {
            case .pushTap(let tap):
                handlePushTap(tap)
            case .selectedInstance(let path):
                openSelectedInstance(path: path)
            case .liveActivity(let url):
                openLiveActivity(url)
            }
        }
        if !AvibeAppDelegate.isRunningTests { turnActivities.observeAuthorization() }
        turnActivities.selectTarget(liveActivityTarget)
        await turnActivities.reconcile(target: liveActivityTarget)
        await healthSync?.resume(foreground: UIApplication.shared.applicationState != .background)
    }

    private func stopRuntime() {
        deferredOpen = nil
        directPathController?.runtimeDidStop()
        if let runtimeController {
            recordRuntimeReleaseBarrier(
                runtimeController.invalidateForAccountTransition()
            )
        }
        runtimeController = nil
    }

    private func retireRuntimeOpenForAccountTransition() {
        runtimeOpenNotice = nil
        invalidatePendingSystemSchedule()
        guard let attempt = runtimeOpenAttempt else {
            return
        }
        lastRuntimeOpenTermination = RuntimeOpenTermination(
            attemptID: attempt.id,
            instanceName: attempt.runtime.instance.name,
            lastPhase: attempt.phase,
            reason: .accountTransition
        )
        runtimeOpenAttempt = nil
        cancelRuntimeOpenTimers()
        runtimeIdentity += 1
    }

    private func cancelRuntimeOpenTimers() {
        runtimeOpenDeadlineTimer?.cancel()
        runtimeOpenDeadlineTimer = nil
        runtimeOpenLongWaitTimer?.cancel()
        runtimeOpenLongWaitTimer = nil
    }

    /// The clock every opening budget is measured on: `runtimeOpenTiming`'s
    /// time with the App's background intervals left out, so it stands
    /// still while the App is in the background. An attempt's
    /// `automaticStartedAt` and `deadline` are on this clock.
    private var runtimeOpenNow: Duration {
        (runtimeOpenBackgroundedAt ?? runtimeOpenTiming.now)
            - runtimeOpenBackgroundTime
    }

    private func automaticRemaining(
        for attempt: RuntimeOpenAttempt,
        at now: Duration
    ) -> Duration {
        guard let automaticStartedAt = attempt.automaticStartedAt else {
            return attempt.remainingAutomaticBudget
        }
        return max(
            .zero,
            attempt.remainingAutomaticBudget - (now - automaticStartedAt)
        )
    }

    private func scheduleRuntimeOpenTimers(attemptID: Int) {
        cancelRuntimeOpenTimers()
        guard
            // The budget clock stands still in the background; the return
            // to the foreground schedules the remainder.
            runtimeOpenBackgroundedAt == nil,
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.automaticStartedAt != nil
        else {
            return
        }
        let remaining = automaticRemaining(
            for: attempt,
            at: runtimeOpenNow
        )
        guard remaining > .zero else {
            // As the deadline timer would (a page shown uncovered stays).
            expireRuntimeOpenAttemptIfNeeded(attemptID: attemptID)
            return
        }
        runtimeOpenDeadlineTimer = runtimeOpenTiming.schedule(after: remaining) {
            [weak self] in
            self?.expireRuntimeOpenAttemptIfNeeded(attemptID: attemptID)
        }

        guard !attempt.isTakingLong else {
            return
        }
        let elapsed = runtimeOpenBudget - remaining
        let untilLongWait = runtimeOpenLongWait - elapsed
        if untilLongWait <= .zero {
            markRuntimeOpenAsTakingLong(attemptID: attemptID)
        } else if untilLongWait < remaining {
            runtimeOpenLongWaitTimer = runtimeOpenTiming.schedule(
                after: untilLongWait
            ) { [weak self] in
                self?.markRuntimeOpenAsTakingLong(attemptID: attemptID)
            }
        }
    }

    private func markRuntimeOpenAsTakingLong(attemptID: Int) {
        guard
            !expireRuntimeOpenAttemptIfNeeded(attemptID: attemptID),
            var attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.automaticStartedAt != nil,
            !attempt.isTakingLong
        else {
            return
        }
        attempt.isTakingLong = true
        runtimeOpenAttempt = attempt
    }

    @discardableResult
    private func expireRuntimeOpenAttemptIfNeeded(attemptID: Int) -> Bool {
        guard
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.automaticStartedAt != nil
        else {
            return false
        }
        guard automaticRemaining(for: attempt, at: runtimeOpenNow) <= .zero else {
            return false
        }
        if revertTunnelOnlyIfUnreachable(attemptID: attemptID) {
            // Opened again, automatic: this attempt is replaced.
            openRuntime(attempt.runtime)
            return true
        }
        if attempt.phase == .showingCachedLive {
            // The page stays; a banner says the instance is not reachable,
            // with Retry. A later live navigation is covered as any open.
            finishRuntimeOpenAutomaticBudget(phase: .cachedUnreachable, attemptID: attemptID)
            return true
        }
        terminateRuntimeOpen(
            attemptID: attemptID,
            reason: .timedOut,
            showsNotice: true
        )
        return true
    }

    private func beginRuntimeOpenAttempt(
        for runtime: MobileRuntimeContext,
        phase: RuntimeOpenPhase
    ) -> Int {
        invalidatePendingSystemSchedule()
        cancelRuntimeOpenTimers()
        runtimeOpenNotice = nil
        runtimeIdentity += 1
        let now = runtimeOpenNow
        let attempt = RuntimeOpenAttempt(
            id: runtimeIdentity,
            runtime: runtime,
            startedAt: now,
            phase: phase,
            remainingAutomaticBudget: runtimeOpenBudget,
            automaticStartedAt: now,
            deadline: now + runtimeOpenBudget,
            canResumeAutomaticBudget: true,
            isTakingLong: false
        )
        runtimeOpenAttempt = attempt
        scheduleRuntimeOpenTimers(attemptID: attempt.id)
        return attempt.id
    }

    private func setRuntimeOpenPhase(
        _ phase: RuntimeOpenPhase,
        attemptID: Int
    ) {
        guard var attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return
        }
        attempt.phase = phase
        runtimeOpenAttempt = attempt
    }

    private func pauseRuntimeOpenBudget(
        phase: RuntimeOpenPhase,
        canResume: Bool,
        attemptID: Int
    ) {
        guard var attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return
        }
        attempt.remainingAutomaticBudget = automaticRemaining(
            for: attempt,
            at: runtimeOpenNow
        )
        attempt.automaticStartedAt = nil
        attempt.deadline = nil
        attempt.canResumeAutomaticBudget = canResume
        attempt.phase = phase
        runtimeOpenAttempt = attempt
        cancelRuntimeOpenTimers()
    }

    @discardableResult
    private func resumeRuntimeOpenBudget(attemptID: Int) -> Bool {
        guard var attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return false
        }
        guard attempt.canResumeAutomaticBudget else {
            return false
        }
        guard attempt.automaticStartedAt == nil else {
            return true
        }
        guard attempt.remainingAutomaticBudget > .zero else {
            terminateRuntimeOpen(
                attemptID: attemptID,
                reason: .timedOut,
                showsNotice: true
            )
            return false
        }
        let now = runtimeOpenNow
        attempt.automaticStartedAt = now
        attempt.deadline = now + attempt.remainingAutomaticBudget
        runtimeOpenAttempt = attempt
        scheduleRuntimeOpenTimers(attemptID: attemptID)
        return true
    }

    private func finishRuntimeOpenAutomaticBudget(
        phase: RuntimeOpenPhase,
        attemptID: Int
    ) {
        pauseRuntimeOpenBudget(
            phase: phase,
            canResume: false,
            attemptID: attemptID
        )
    }

    private func terminateRuntimeOpen(
        attemptID: Int,
        reason: RuntimeOpenTerminationReason,
        showsNotice: Bool
    ) {
        guard let attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return
        }
        let termination = RuntimeOpenTermination(
            attemptID: attempt.id,
            instanceName: attempt.runtime.instance.name,
            lastPhase: attempt.phase,
            reason: reason
        )
        lastRuntimeOpenTermination = termination
        if showsNotice {
            runtimeOpenNotice = RuntimeOpenNotice(
                instanceID: attempt.runtime.instance.id,
                instanceName: attempt.runtime.instance.name,
                lastPhase: attempt.phase
            )
        }
        runtimeOpenAttempt = nil
        cancelRuntimeOpenTimers()
        invalidatePendingSystemSchedule()
        // Retire the generation before controller teardown, so a callback
        // already queued on the main actor cannot regain authority.
        runtimeIdentity += 1
        clearPendingPushOpened()
        stopRuntime()
        route = .onboarding(.instances)
    }

    func cancelRuntimeOpen(attemptID: Int) {
        terminateRuntimeOpen(
            attemptID: attemptID,
            reason: .cancelled,
            showsNotice: false
        )
    }

    @discardableResult
    func retryRuntimeOpen(attemptID: Int) -> Bool {
        guard
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.phase == .failed,
            route.runtime == attempt.runtime,
            sessionCoordinator.isSelectedRuntime(attempt.runtime),
            authorizeReload(of: attempt.runtime)
        else {
            return false
        }
        return openRuntime(attempt.runtime)
    }

    func dismissRuntimeOpenNotice() {
        runtimeOpenNotice = nil
    }

    /// A page shown from the cache reached its instance (the instance
    /// answered a request from that page's origin): the open is done.
    func runtimeCachedPageConnected(attemptID: Int, runtime: MobileRuntimeContext) {
        guard
            runtimeIdentity == attemptID,
            route.runtime == runtime,
            sessionCoordinator.isSelectedRuntime(runtime)
        else {
            return
        }
        guard
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.runtime == runtime,
            [.showingCached, .showingCachedLive, .cachedUnreachable, .cachedOffline]
                .contains(attempt.phase)
        else {
            return
        }
        if var connected = runtimeOpenAttempt, connected.id == attemptID {
            // It answered: whatever avibe.bot listed, it is online.
            connected.listedOffline = false
            runtimeOpenAttempt = connected
        }
        finishRuntimeOpenAutomaticBudget(phase: .documentLoaded, attemptID: attemptID)
        didFinishRuntimeLoading()
    }

    /// Retry from the banner over a cached page whose instance could not be
    /// reached: the instance opens again, live, skipping the cache once.
    @discardableResult
    func retryRuntimeCachedPage() -> Bool {
        guard
            let attempt = runtimeOpenAttempt,
            attempt.phase == .cachedUnreachable,
            route.runtime == attempt.runtime,
            sessionCoordinator.isSelectedRuntime(attempt.runtime),
            authorizeReload(of: attempt.runtime)
        else {
            return false
        }
        runtimeLoadsLiveOnce = attempt.runtime
        return openRuntime(attempt.runtime)
    }

    /// Retry from the offline row: the instance opens again (its cached page
    /// first, as any open), and avibe.bot is asked again whether it is
    /// listed online; the answer applies as at launch.
    @discardableResult
    func retryOfflineCachedPage() -> Bool {
        guard
            let attempt = runtimeOpenAttempt,
            attempt.phase == .cachedOffline,
            route.runtime == attempt.runtime,
            sessionCoordinator.isSelectedRuntime(attempt.runtime),
            authorizeReload(of: attempt.runtime),
            openRuntime(attempt.runtime)
        else {
            return false
        }
        let runtime = attempt.runtime
        Task { @MainActor [weak self] in
            guard let self else { return }
            let checked = try? await self.sessionCoordinator.refresh()
            self.settleListedAvailability(runtime, checked: checked)
        }
        return true
    }

    /// The page shown from the cache said it keeps its shell while its
    /// instance cannot be reached (`RuntimeCachedPage.keepsShell`, and the
    /// Web UI behind a replayable outcome): it is uncovered, live to touch.
    func runtimeCachedPageKeepsShell(attemptID: Int, runtime: MobileRuntimeContext) {
        guard
            runtimeIdentity == attemptID,
            route.runtime == runtime,
            sessionCoordinator.isSelectedRuntime(runtime),
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.runtime == runtime,
            attempt.phase == .showingCached,
            !expireRuntimeOpenAttemptIfNeeded(attemptID: attemptID)
        else {
            return
        }
        if attempt.listedOffline {
            showCachedPageOffline(attemptID: attemptID)
            return
        }
        setRuntimeOpenPhase(.showingCachedLive, attemptID: attemptID)
    }

    /// A live document replaced a page shown uncovered from the cache (or
    /// its process ended): a full budget from now, as a new open has.
    private func restartRuntimeOpenBudget(attemptID: Int) {
        guard var attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return
        }
        let now = runtimeOpenNow
        attempt.remainingAutomaticBudget = runtimeOpenBudget
        attempt.canResumeAutomaticBudget = true
        attempt.isTakingLong = false
        attempt.automaticStartedAt = now
        attempt.deadline = now + runtimeOpenBudget
        runtimeOpenAttempt = attempt
        scheduleRuntimeOpenTimers(attemptID: attemptID)
    }

    /// The cache as the runtime controller for `runtime` uses it: its page
    /// for this account and origin, and storing a new one while `runtime`
    /// is still the account's selected instance.
    func runtimePageCacheAccess(for runtime: MobileRuntimeContext) -> RuntimePageCacheAccess? {
        guard let origin = WebOrigin(url: runtime.instance.url)?.serialized else {
            return nil
        }
        let accountID = runtime.account.id
        // The runtime's own store's pages, whatever store is current later.
        let cache = RuntimePageCache(directory: RuntimePageCache.root
            .appendingPathComponent(runtime.websiteDataStoreID.uuidString, isDirectory: true))
        return RuntimePageCacheAccess(
            load: { [weak self] in
                if let self, self.runtimeLoadsLiveOnce == runtime {
                    self.runtimeLoadsLiveOnce = nil
                    return nil
                }
                guard let page = cache.page(accountID: accountID, origin: origin) else {
                    return nil
                }
                // At most a day old: then a live load recaptures it (a new
                // build, new headers) and renews the instance's cookie as a
                // top-level load does.
                let age = Date().timeIntervalSince(page.capturedAt)
                guard age >= 0, age < RuntimePageCache.maximumAge else {
                    cache.removePage(accountID: accountID, origin: origin)
                    return nil
                }
                return page
            },
            store: { [weak self] page in
                guard let self, self.sessionCoordinator.isSelectedRuntime(runtime) else { return }
                cache.store(page, accountID: accountID, origin: origin)
            },
            remove: {
                cache.removePage(accountID: accountID, origin: origin)
            }
        )
    }

    func handleRuntimeNavigation(
        _ event: RuntimeNavigationEvent,
        attemptID: Int,
        runtime: MobileRuntimeContext
    ) {
        if event.stage == .started,
           runtimeIdentity == attemptID,
           route.runtime == runtime,
           sessionCoordinator.isSelectedRuntime(runtime)
        {
            invalidatePendingSystemSchedule()
        }
        guard
            runtimeIdentity == attemptID,
            runtimeOpenAttempt?.id == attemptID,
            runtimeOpenAttempt?.runtime == runtime,
            route.runtime == runtime,
            sessionCoordinator.isSelectedRuntime(runtime)
        else {
            return
        }
        // Out of time: the open ends, unless a page shown uncovered stays
        // (`.cachedUnreachable`), which still takes this event (a live
        // document replacing it must not leave the banner over it).
        if expireRuntimeOpenAttemptIfNeeded(attemptID: attemptID),
           runtimeOpenAttempt?.phase != .cachedUnreachable {
            return
        }
        guard var attempt = runtimeOpenAttempt, attempt.id == attemptID else {
            return
        }

        let showsCachedPageUncovered = attempt.phase == .showingCachedLive
            || attempt.phase == .cachedUnreachable
            || attempt.phase == .cachedOffline
        if !event.fromCache, event.stage == .started {
            if attempt.listedOffline, isWaitingForCachedPage(attempt) {
                // Listed offline, and the instance loads live instead of a
                // cached page that keeps its shell (none, it declined, or
                // it fell to setup): as without a cache.
                cancelRuntimeOpen(attemptID: attemptID)
                return
            }
            attempt.startedLive = true
            runtimeOpenAttempt = attempt
        }
        switch event.stage {
        case .started:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed,
                  // A live navigation from a page shown uncovered: the page
                  // stays until a document replaces it (committed).
                  !showsCachedPageUncovered
            else {
                return
            }
            if attempt.automaticStartedAt == nil,
               attempt.canResumeAutomaticBudget,
               !resumeRuntimeOpenBudget(attemptID: attemptID)
            {
                return
            }
            setRuntimeOpenPhase(
                isAuthenticationDestination(event.destination)
                    ? .authenticating
                    : event.fromCache ? .showingCached : .connecting,
                attemptID: attemptID
            )
        case .committed:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
            else {
                return
            }
            if showsCachedPageUncovered {
                // A live document replaced the page shown uncovered: from
                // here it opens as any open does, with a fresh budget. The
                // instance served it, so it is not offline.
                attempt.listedOffline = false
                runtimeOpenAttempt = attempt
                restartRuntimeOpenBudget(attemptID: attemptID)
            }
            // A cached document stays covered from its start: never the
            // uncovered `.loadingPage`.
            setRuntimeOpenPhase(
                isAuthenticationDestination(event.destination)
                    ? .authenticating
                    : event.fromCache ? .showingCached : .loadingPage,
                attemptID: attemptID
            )
        case .finished:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
            else {
                return
            }
            if event.fromCache, !isAuthenticationDestination(event.destination) {
                // Loaded but covered until the instance answers for this
                // session (`runtimeCachedPageConnected`): the Web UI cannot
                // yet show content without its instance (it would fall back
                // to setup). The budget keeps running; out of time, the open
                // ends as any open does.
                setRuntimeOpenPhase(.showingCached, attemptID: attemptID)
                return
            }
            if isAuthenticationDestination(event.destination) {
                pauseRuntimeOpenBudget(
                    phase: .awaitingUser,
                    canResume: attempt.canResumeAutomaticBudget,
                    attemptID: attemptID
                )
            } else {
                finishRuntimeOpenAutomaticBudget(
                    phase: .documentLoaded,
                    attemptID: attemptID
                )
                didFinishRuntimeLoading()
            }
        case .handedOff:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed,
                  attempt.phase != .cachedUnreachable,
                  attempt.phase != .cachedOffline
            else {
                return
            }
            if attempt.phase == .showingCachedLive {
                // A link handed off from the page on screen: it stays, and
                // the open keeps waiting for the instance, budget running.
                return
            }
            pauseRuntimeOpenBudget(
                phase: attempt.phase,
                canResume: false,
                attemptID: attemptID
            )
        case .cancelled:
            // Nothing moved; a page on screen (live, or uncovered from the
            // cache) stays as it is.
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed,
                  attempt.phase != .showingCachedLive,
                  attempt.phase != .cachedUnreachable,
                  attempt.phase != .cachedOffline
            else {
                return
            }
            if attempt.listedOffline {
                // Waiting for a cached page while listed offline: none can
                // show now, so as without one.
                cancelRuntimeOpen(attemptID: attemptID)
                return
            }
            finishRuntimeOpenAutomaticBudget(
                phase: .failed,
                attemptID: attemptID
            )
        case .restarting:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
            else {
                return
            }
            if showsCachedPageUncovered {
                // The page shown uncovered is gone with its process; the
                // instance loads live, as any open.
                restartRuntimeOpenBudget(attemptID: attemptID)
            } else if attempt.automaticStartedAt == nil,
               attempt.canResumeAutomaticBudget,
               !resumeRuntimeOpenBudget(attemptID: attemptID)
            {
                return
            }
            setRuntimeOpenPhase(.connecting, attemptID: attemptID)
        case .failed:
            // A live navigation from a page shown uncovered failed before
            // replacing it: the page stays.
            guard attempt.phase != .failed, !showsCachedPageUncovered else {
                return
            }
            if attempt.listedOffline, attempt.phase != .documentLoaded {
                cancelRuntimeOpen(attemptID: attemptID)
                return
            }
            if attempt.phase == .documentLoaded {
                setRuntimeOpenPhase(.failed, attemptID: attemptID)
            } else {
                let revertible = !isAuthenticationDestination(event.destination)
                    && attempt.phase != .authenticating
                    && attempt.phase != .awaitingUser
                finishRuntimeOpenAutomaticBudget(
                    phase: .failed,
                    attemptID: attemptID
                )
                if revertible, revertTunnelOnlyIfUnreachable(attemptID: attemptID) {
                    // Opened again, automatic, once this callback has
                    // returned; the failed attempt waits for nothing else.
                    let runtime = attempt.runtime
                    Task { @MainActor [weak self] in
                        guard
                            let self,
                            self.runtimeIdentity == attemptID,
                            self.runtimeOpenAttempt?.id == attemptID,
                            self.runtimeOpenAttempt?.phase == .failed
                        else {
                            return
                        }
                        self.openRuntime(runtime)
                    }
                }
            }
        }
    }

    // MARK: This device's path choice

    private func preferenceOrigin(_ runtime: MobileRuntimeContext) -> String? {
        WebOrigin(url: runtime.instance.url)?.serialized
    }

    func directPathPreference(for runtime: MobileRuntimeContext) -> DirectPathPreference {
        guard let origin = preferenceOrigin(runtime) else {
            return .automatic
        }
        return directPathPreferences.preference(accountID: runtime.account.id, origin: origin)
    }

    /// Whether opens of `runtime` may use the direct path.
    private func allowsDirect(_ runtime: MobileRuntimeContext) -> Bool {
        directPathPreference(for: runtime).mode == .automatic
    }

    /// The runtime on screen, if it is `origin`'s and still selected.
    private func runtimeShowing(_ origin: WebOrigin) -> MobileRuntimeContext? {
        guard
            let runtime = route.runtime,
            WebOrigin(url: runtime.instance.url) == origin,
            sessionCoordinator.isSelectedRuntime(runtime)
        else {
            return nil
        }
        return runtime
    }

    /// The page chose a path for its instance. Kept, any earlier automatic
    /// reversion cleared; a change of mode reopens the instance at `url`
    /// after this answer, so every connection starts again on the new path.
    private func setDirectPathPreference(
        _ mode: DirectPathPreference.Mode,
        at origin: WebOrigin,
        reopeningAt url: URL
    ) -> DirectPathPreference? {
        guard
            let runtime = runtimeShowing(origin),
            let key = preferenceOrigin(runtime)
        else {
            return nil
        }
        let previous = directPathPreference(for: runtime)
        let next = DirectPathPreference(mode: mode, reverted: nil)
        directPathPreferences.set(next, accountID: runtime.account.id, origin: key)
        guard previous.mode != mode else {
            return next
        }
        let identity = runtimeIdentity
        Task { @MainActor [weak self] in
            guard
                let self,
                self.runtimeIdentity == identity,
                self.route.runtime == runtime,
                self.sessionCoordinator.isSelectedRuntime(runtime)
            else {
                return
            }
            // For this reopen's attempt only: an open that never reaches
            // its first load leaves nothing for a later one.
            if self.openRuntime(runtime) {
                self.runtimeStartURL = (runtime, self.runtimeIdentity, url)
            }
        }
        return next
    }

    /// The URL the new controller of `runtime` starts at, once.
    func takeRuntimeStartURL(for runtime: MobileRuntimeContext) -> URL? {
        guard
            let start = runtimeStartURL,
            start.runtime == runtime,
            start.identity == runtimeIdentity
        else {
            return nil
        }
        runtimeStartURL = nil
        return start.url
    }

    /// "Cloudflare tunnel only", and this open of the instance ran out of
    /// time or its document failed to load, while the device has a network
    /// and avibe.bot lists the instance online: the path is what fails, so
    /// the choice goes back to automatic (once; it stays automatic until
    /// chosen again), and the page is told. The caller opens it again.
    /// Not while a sign-in page is in front (its host is not the tunnel),
    /// nor over a cached page shown uncovered (it stays, with its banner).
    private func revertTunnelOnlyIfUnreachable(attemptID: Int) -> Bool {
        guard
            directPathController != nil,
            !directListenerFault,
            runtimeIdentity == attemptID,
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            ![.authenticating, .awaitingUser, .showingCachedLive].contains(attempt.phase),
            route.runtime == attempt.runtime,
            // As the reopen requires.
            sessionCoordinator.keepsSelectedRuntime(attempt.runtime),
            networkPath.isSatisfied,
            sessionCoordinator.currentInstances
                .first(where: { $0.id == attempt.runtime.instance.id })?.available == true,
            directPathPreference(for: attempt.runtime).mode == .tunnel,
            let origin = preferenceOrigin(attempt.runtime)
        else {
            return false
        }
        let reverted = DirectPathPreference(
            mode: .automatic,
            reverted: .init(at: Date(), reason: .unreachable)
        )
        directPathPreferences.set(reverted, accountID: attempt.runtime.account.id, origin: origin)
        runtimeController?.deliverDirectPreferenceChanged(reverted)
        return true
    }

    private func isAuthenticationDestination(
        _ destination: NavigationDestination?
    ) -> Bool {
        switch destination {
        case .authentication, .thirdPartySignIn:
            return true
        case .instance, .external, .browserInternal, nil:
            return false
        }
    }

    /// WebKit is suspended in the background, so a slow open there is
    /// paused, not failing: the budget clock stops here and its timers are
    /// cancelled. Resigning active alone (Control Center, an alert) keeps
    /// WebKit running and keeps charging the budget.
    private func runtimeOpenDidEnterBackground() {
        healthSync?.setBackground()
        guard runtimeOpenBackgroundedAt == nil else {
            return
        }
        runtimeOpenBackgroundedAt = runtimeOpenTiming.now
        cancelRuntimeOpenTimers()
    }

    /// Restarts the budget clock and schedules only the running attempt's
    /// remainder. A timer that could not fire on time (no background
    /// interval was reported) ends its attempt here.
    private func runtimeOpenWillEnterForeground() {
        if hasCompletedBootstrap {
            turnActivities.selectTarget(liveActivityTarget)
            Task { await turnActivities.reconcile(target: liveActivityTarget) }
        }
        Task { await healthSync?.resume(foreground: true) }
        Task { [weak self] in
            await self?.systemScheduleCoordinator.retryPendingCleanup()
        }
        if let backgroundedAt = runtimeOpenBackgroundedAt {
            runtimeOpenBackgroundTime += runtimeOpenTiming.now - backgroundedAt
            runtimeOpenBackgroundedAt = nil
        }
        guard let attemptID = runtimeOpenAttempt?.id else {
            return
        }
        scheduleRuntimeOpenTimers(attemptID: attemptID)
    }

    @discardableResult
    private func openRuntime(_ runtime: MobileRuntimeContext) -> Bool {
        // The open path asks `keepsSelectedRuntime`: a push update the open
        // itself starts (or any selection-keeping request) does not end it;
        // a reset, a sign-out, an account change, or any other operation
        // does. Direct authorization keeps asking `isCurrentRuntime`.
        guard sessionCoordinator.keepsSelectedRuntime(runtime) else {
            return false
        }
        if let pendingPushOpened,
           pendingPushOpened.instanceOrigin
                != WebOrigin(url: runtime.instance.url)?.serialized
        {
            clearPendingPushOpened()
        }
        let turnTarget = liveActivityTarget
        turnActivities.selectTarget(turnTarget)
        Task { await turnActivities.reconcile(target: turnTarget) }
        nativePushCoordinator.resumeAfterAccountActivation()
        if let previous = runtimeOpenAttempt {
            lastRuntimeOpenTermination = RuntimeOpenTermination(
                attemptID: previous.id,
                instanceName: previous.runtime.instance.name,
                lastPhase: previous.phase,
                reason: .replaced
            )
        }
        runtimeOpenAttempt = nil
        cancelRuntimeOpenTimers()
        stopRuntime()
        let identity = beginRuntimeOpenAttempt(
            for: runtime,
            phase: directPathController == nil ? .connecting : .preparingRoute
        )
        guard let directPathController else {
            route = .browser(runtime)
            return true
        }
        // The route is prepared whatever this device's path choice: a host
        // loaded once without one keeps WebKit's pooled public connections
        // on this store, so a later Automatic could never take effect
        // (remote-direct-ios.md section 2). Under "Cloudflare tunnel only"
        // nothing is selected (`settleOpen`): the transport never dials and
        // every plan has only the public candidate.
        // The route for the instance's host must be in place before the
        // runtime's first load (docs/plans/remote-direct-ios.md section 5).
        // Its wait consumes the same fixed attempt budget as Web navigation.
        route = .loading
        let stillWanted = { [weak self] () -> Bool in
            guard let self else {
                return false
            }
            return self.runtimeIdentity == identity
                && self.runtimeOpenAttempt?.id == identity
                && self.route == .loading
                && self.sessionCoordinator.keepsSelectedRuntime(runtime)
        }
        Task { [weak self] in
            await directPathController.prepareRoute(
                for: runtime,
                stillWanted: stillWanted
            )
            guard let self,
                  self.runtimeIdentity == identity,
                  self.runtimeOpenAttempt?.id == identity,
                  self.route == .loading,
                  !self.expireRuntimeOpenAttemptIfNeeded(attemptID: identity)
            else {
                // Something newer took the route, or the total budget ended.
                return
            }
            guard self.sessionCoordinator.keepsSelectedRuntime(runtime) else {
                self.terminateRuntimeOpen(
                    attemptID: identity,
                    reason: .accountTransition,
                    showsNotice: false
                )
                return
            }
            self.settleOpen(of: runtime, identity: identity)
        }
        return true
    }

    /// The creation boundary of a runtime's WebView, decided synchronously
    /// from the load gate. Open: the browser. Held (the host has a route
    /// and the listener is closed or being bound again): the loading route
    /// stays and the open waits in its slot for the return to settle.
    /// Fault: the reopen message alone.
    private func settleOpen(of runtime: MobileRuntimeContext, identity: Int) {
        guard let directPathController else {
            return
        }
        // About to act on the gate: a failure already recorded is delivered
        // first, so the marker never lags behind what is shown.
        directPathController.settleRecordedFault()
        switch directPathController.loadGate(for: runtime) {
        case .open:
            deferredOpen = nil
            setRuntimeOpenPhase(.connecting, attemptID: identity)
            route = .browser(runtime)
            // "Cloudflare tunnel only": routed, never selected.
            if allowsDirect(runtime) {
                directPathController.runtimeDidOpen(runtime)
            }
        case .held:
            deferredOpen = (runtime, identity)
        case .fault:
            // The fault is already terminal for this process. There will be
            // no new listener callback to end an attempt started after it,
            // so retire the automatic budget at the gate decision itself.
            finishRuntimeOpenAutomaticBudget(
                phase: .failed,
                attemptID: identity
            )
            deferredOpen = nil
            directFaultOnlyIdentity = identity
            route = .browser(runtime)
        }
    }

    /// A load gate may have changed: the listener was closed for the
    /// background, a return was settled, or the fault was entered. Nothing
    /// is replayed from an earlier decision: the root reads the gate again,
    /// a held open is decided again for its own identity, and the live
    /// runtime's WebView is told, for an automatic load it is holding.
    private func directAvailabilityDidChange() {
        objectWillChange.send()
        if let deferred = deferredOpen {
            if runtimeIdentity != deferred.identity
                || runtimeOpenAttempt?.id != deferred.identity
                || route != .loading
            {
                deferredOpen = nil
            } else if expireRuntimeOpenAttemptIfNeeded(
                attemptID: deferred.identity
            ) {
                deferredOpen = nil
            } else if !sessionCoordinator.keepsSelectedRuntime(deferred.runtime) {
                // The selection ended underneath; do not leave the spinner
                // or its attempt alive. A push update in flight (one the open
                // or the return to the foreground started) keeps it.
                deferredOpen = nil
                retireRuntimeOpenForAccountTransition()
                stopRuntime()
                route = .onboarding(.instances)
            } else {
                settleOpen(of: deferred.runtime, identity: deferred.identity)
            }
        }
        runtimeController?.directAvailabilityDidChange()
    }

    /// Whether the shell may create or reload `runtime`'s WebView now
    /// (`DirectPathController.loadGate`). Always open without a transport.
    func directLoadGate(for runtime: MobileRuntimeContext) -> DirectLoadGate {
        directPathController?.loadGate(for: runtime) ?? .open
    }

    /// The load gate for `runtime`'s WebView controller, which asks it at
    /// every load of its own; `nil` without a direct transport, so the
    /// controller loads exactly as it did before.
    func directLoadGateProvider(
        for runtime: MobileRuntimeContext
    ) -> DirectLoadGateProvider? {
        guard directPathController != nil else {
            return nil
        }
        return { [weak self] in
            // Asked where a load is about to be made, never in a view
            // update: a failure already recorded is delivered first.
            self?.directPathController?.settleRecordedFault()
            return self?.directLoadGate(for: runtime) ?? .open
        }
    }

    /// The WebView the direct transport's rendezvous request runs in: the
    /// primary WebView of the runtime the path controller selected for the
    /// transport, and only while all of this holds: `selection` is the
    /// controller's current target, the account's data store is the
    /// target's, the target's runtime is the account's current one, and it
    /// is the runtime shown (or covered by Account). Otherwise `nil`, which
    /// the transport takes as a page that is not ready yet. Always `nil`
    /// without a transport.
    func directRendezvousWebView(for selection: DirectIrohSelection) -> WKWebView? {
        guard
            let target = directPathController?.selectedTarget,
            DirectIrohSelection(target) == selection,
            target.storeIdentifier == websiteDataStore.identifier,
            route.runtime == target.runtime,
            sessionCoordinator.isCurrentRuntime(target.runtime),
            let runtimeController
        else {
            return nil
        }
        return runtimeController.livePrimaryWebView
    }

    /// Whether the shell's own reload of `runtime` (Retry) may be offered
    /// now. Synchronous and without side effects, for the view: only when
    /// the host is on WebKit's normal path or its route points at a serving
    /// listener. Never while the listener is closed or being bound again,
    /// and never under the fault. Always `true` without a transport.
    func mayReload(_ runtime: MobileRuntimeContext) -> Bool {
        directLoadGate(for: runtime) == .open
    }

    /// The Retry tap itself: decides again, now, whether the reload may be
    /// made, and delivers a failure already recorded first.
    func authorizeReload(of runtime: MobileRuntimeContext) -> Bool {
        directPathController?.settleRecordedFault()
        return mayReload(runtime)
    }

    /// Whether the listener fault covers `runtime`: its host is routed to a
    /// port the App may not hold. Its content is then replaced by the
    /// reopen message.
    func directFaultCovers(_ runtime: MobileRuntimeContext) -> Bool {
        directLoadGate(for: runtime) == .fault
    }

    /// Whether `runtime` was opened under the fault: it has no WebView, only
    /// the reopen message.
    func presentsDirectFaultOnly(_ runtime: MobileRuntimeContext) -> Bool {
        directFaultOnlyIdentity == runtimeIdentity && directFaultCovers(runtime)
    }

    private func directListenerDidFault() {
        // The marker first, and whatever else happens: it is what makes the
        // next launch work. Repeated only until one call has returned.
        if !directFaultIsMarked {
            directFaultMarker.mark()
            directFaultIsMarked = true
        }
        guard !directListenerFault else {
            return
        }
        directListenerFault = true
        if let attempt = runtimeOpenAttempt,
           attempt.phase != .documentLoaded,
           attempt.phase != .failed,
           directFaultCovers(attempt.runtime)
        {
            finishRuntimeOpenAutomaticBudget(
                phase: .failed,
                attemptID: attempt.id
            )
        }
        // A child window is presented above the runtime's own view, which
        // the reopen message covers; it goes, when the message covers it.
        if let runtime = route.runtime, directFaultCovers(runtime) {
            runtimeController?.discardPopupsForListenerFault()
        }
        // A held open ends as the reopen message, and a held automatic
        // load is dropped.
        directAvailabilityDidChange()
    }

    /// Sends the current `direct` state to the runtime's documents.
    private func deliverDirectStatus() {
        guard let directPathController else {
            return
        }
        runtimeController?.deliverDirectTransportChanged(
            statusFor: directPathController.currentStatus(for:)
        )
    }

    func didFinishRuntimeLoading() {
        guard
            let pendingPushOpened,
            let runtimeController
        else {
            return
        }
        guard pendingPushOpened.createdAt.addingTimeInterval(
            pushOpenedLifetime
        ) > pushOpenedNow(),
              pendingPushOpened.runtimeIdentity == runtimeIdentity,
              sessionCoordinator.currentAccount?.id == pendingPushOpened.accountID,
              sessionCoordinator.currentInstances.contains(where: {
                  $0.available
                      && WebOrigin(url: $0.url)?.serialized
                          == pendingPushOpened.instanceOrigin
              })
        else {
            clearPendingPushOpened()
            return
        }
        runtimeController.deliverPushOpened(
            id: pendingPushOpened.id,
            path: pendingPushOpened.url
        )
    }

    @discardableResult
    func acknowledgePushOpened(id: String, origin: WebOrigin) -> Bool {
        guard
            let pendingPushOpened,
            pendingPushOpened.id == id,
            pendingPushOpened.instanceOrigin == origin.serialized,
            pendingPushOpened.runtimeIdentity == runtimeIdentity,
            pendingPushOpened.createdAt.addingTimeInterval(
                pushOpenedLifetime
            ) > pushOpenedNow(),
            sessionCoordinator.currentAccount?.id == pendingPushOpened.accountID,
            let runtime = route.runtime,
            WebOrigin(url: runtime.instance.url)?.serialized == origin.serialized
        else {
            if pendingPushOpened?.id == id {
                clearPendingPushOpened()
            }
            return false
        }
        clearPendingPushOpened()
        return true
    }

    /// `badge.set` from a runtime page: only the selected instance's page
    /// sets the badge, the rule `push.*` follows. A push-management request
    /// in flight changes neither, so it does not hold the badge back.
    @discardableResult
    func setBadgeCount(_ count: Int, origin: WebOrigin) -> Bool {
        guard
            let runtime = route.runtime,
            sessionCoordinator.isSelectedRuntime(runtime),
            WebOrigin(url: runtime.instance.url)?.serialized == origin.serialized
        else {
            return false
        }
        nativeBadge.set(count)
        // Once this runtime's page names its unread Sessions, that set
        // retires notifications; the total is preference-filtered and has no
        // snapshot time.
        if count == 0,
           reconciledRuntimeIdentity != runtimeIdentity,
           let account = sessionCoordinator.currentAccount
        {
            // Nothing notified on this instance is unread. The zero was
            // computed before this call arrived, so only what was delivered
            // by now is covered by it; anything later stays.
            sessionNotifications.clearAllSessions(
                instanceOrigin: origin.serialized,
                accountID: account.id,
                readAt: Date()
            )
        }
        return true
    }

    /// `notifications.reconcile` from a runtime page: only the selected
    /// instance's page retires its notifications, the rule `badge.set`
    /// follows, and only this account's.
    @discardableResult
    func reconcileNotifications(
        _ reconcile: NotificationReconcile,
        origin: WebOrigin
    ) -> Bool {
        guard
            let runtime = route.runtime,
            sessionCoordinator.isSelectedRuntime(runtime),
            WebOrigin(url: runtime.instance.url)?.serialized == origin.serialized,
            let account = sessionCoordinator.currentAccount
        else {
            return false
        }
        reconciledRuntimeIdentity = runtimeIdentity
        sessionNotifications.reconcile(
            reconcile,
            instanceOrigin: origin.serialized,
            accountID: account.id
        )
        return true
    }

    private func handlePushTap(_ tap: PushNotificationTap) {
        guard hasCompletedBootstrap else {
            pendingInstanceOpen = .pushTap(tap)
            return
        }
        instanceOpenRequest += 1
        guard openInstance(
            accountID: tap.accountID,
            instanceOrigin: tap.instanceOrigin,
            path: tap.url
        ) else {
            showInstancePicker()
            return
        }
    }

    /// Opens the selected instance at `path` (a same-origin path, query,
    /// and fragment) the way a notification tap opens a chat: the instance
    /// opens again and its page receives `push.opened` once it has loaded.
    /// App Shortcuts use it. Signed out, with no instance selected, with the
    /// selected instance unavailable, or while the account may not leave the
    /// account page (account deletion), nothing changes: the App keeps the
    /// screen it shows, with no prompt.
    ///
    /// A refresh or push-management request the App runs on its own (as on
    /// a return to the foreground, which is when a shortcut arrives) keeps
    /// the selection but holds the account. The open waits for it, up to
    /// `selectionKeepingWait`, then opens once, unless meanwhile a newer
    /// open was requested or the screen, the runtime, the account, or the
    /// selection changed: the user has moved on.
    func openSelectedInstance(path: String) {
        guard hasCompletedBootstrap else {
            pendingInstanceOpen = .selectedInstance(path: path)
            return
        }
        instanceOpenRequest += 1
        guard sessionCoordinator.isHeldBySelectionKeepingOperation else {
            openSelectedInstanceNow(path: path)
            return
        }
        let request = instanceOpenRequest
        let route = route
        let identity = runtimeIdentity
        let accountID = sessionCoordinator.currentAccount?.id
        let selected = sessionCoordinator.selectedInstance
        Task { [weak self] in
            await self?.sessionCoordinator.waitForSelectionKeepingOperations(
                atMost: Self.selectionKeepingWait
            )
            guard
                let self,
                self.instanceOpenRequest == request,
                self.route == route,
                self.runtimeIdentity == identity,
                self.sessionCoordinator.currentAccount?.id == accountID,
                self.sessionCoordinator.selectedInstance == selected
            else {
                return
            }
            self.openSelectedInstanceNow(path: path)
        }
    }

    /// How long a shortcut waits for a selection-keeping operation. Someone
    /// who pressed a button expects it to act within a few seconds; a
    /// session read or push update normally ends well within this.
    static let selectionKeepingWait: Duration = .seconds(5)

    private func openSelectedInstanceNow(path: String) {
        guard
            let account = sessionCoordinator.currentAccount,
            let instance = sessionCoordinator.selectedInstance,
            let origin = WebOrigin(url: instance.url)
        else {
            return
        }
        openInstance(
            accountID: account.id,
            instanceOrigin: origin.serialized,
            path: path
        )
    }

    /// Selects the instance at `instanceOrigin` for `accountID` and keeps
    /// `path` for its page. `false` when it is not this account's available
    /// instance, the path is not a same-origin path, or the selection is
    /// refused; the screen is then unchanged.
    @discardableResult
    private func openInstance(
        accountID: String,
        instanceOrigin: String,
        path: String
    ) -> Bool {
        guard
            let account = sessionCoordinator.currentAccount,
            account.id == accountID,
            let instance = sessionCoordinator.currentInstances.first(where: {
                $0.available
                    && WebOrigin(url: $0.url)?.serialized == instanceOrigin
            }),
            Self.isSafeInstancePath(path)
        else {
            return false
        }
        let pending = PendingPushOpened(
            id: UUID().uuidString,
            accountID: accountID,
            instanceOrigin: instanceOrigin,
            url: path,
            runtimeIdentity: runtimeIdentity + 1,
            createdAt: pushOpenedNow()
        )
        pendingPushOpened = pending
        pendingPushOpenedExpiryTask?.cancel()
        let lifetime = pushOpenedLifetime
        pendingPushOpenedExpiryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(lifetime))
            guard !Task.isCancelled else {
                return
            }
            self?.expirePendingPushOpened(id: pending.id)
        }
        do {
            _ = try sessionCoordinator.selectInstance(id: instance.id)
        } catch {
            clearPendingPushOpened()
            return false
        }
        return true
    }

    private func expirePendingPushOpened(id: String) {
        guard pendingPushOpened?.id == id else {
            return
        }
        clearPendingPushOpened()
    }

    private func clearPendingPushOpened() {
        pendingPushOpenedExpiryTask?.cancel()
        pendingPushOpenedExpiryTask = nil
        pendingPushOpened = nil
    }

    private static func isSafeInstancePath(_ value: String) -> Bool {
        guard
            !value.isEmpty,
            value.first == "/",
            !value.contains("\\"),
            !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
            let components = URLComponents(string: value),
            components.scheme == nil,
            components.host == nil,
            components.user == nil,
            components.password == nil
        else {
            return false
        }
        return true
    }

    private func prepareForAccountTransition() -> [
        WebsiteDataStoreReleaseBarrier
    ] {
        invalidatePendingSystemSchedule()
        clearPendingPushOpened()
        // An account's pages go with it (each account's data is kept
        // separately and removed when it signs out or changes).
        runtimePageCache.removeAll()
        runtimeLoadsLiveOnce = nil
        runtimeStartURL = nil
        // Any account invalidation ends a route-prepare or held open; neither
        // may leave the Native loading surface behind without an attempt.
        retireRuntimeOpenForAccountTransition()
        stopRuntime()
        switch route {
        case .browser:
            route = .onboarding(.instances)
        case .accountOverBrowser:
            // The account page stays where it is; only the runtime goes.
            route = .onboarding(.account)
        case .loading:
            route = .onboarding(.instances)
        case .onboarding:
            break
        }
        // A background voice take belongs to the account that is going.
        let barriers = pendingRuntimeReleaseBarriers
            + turnActivities.invalidateRequests()
            + VoiceCaptureController.shared.cancelForAccountTransition()
        pendingRuntimeReleaseBarriers.removeAll()
        return barriers
    }
}
