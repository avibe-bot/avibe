import SwiftUI
import WebKit

@MainActor
protocol RuntimeOpenTimer: AnyObject {
    func cancel()
}

@MainActor
protocol RuntimeOpenTiming: AnyObject {
    var now: Duration { get }

    func schedule(
        after delay: Duration,
        _ action: @escaping @MainActor () -> Void
    ) -> any RuntimeOpenTimer
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
}

enum RuntimeOpenPhase: Equatable {
    case preparingRoute
    case connecting
    case authenticating
    case loadingPage
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

    @Published private(set) var route: AppRoute = .loading
    /// Chosen once when the one runtime-preserving sheet opens. Repeated
    /// presentation requests never replace its document or change its mode.
    @Published private(set) var accountSheetStartScreen: MobileOnboardingStartScreen = .sheet
    /// Advances each time a runtime opens, so the root builds a new runtime
    /// controller even when the same instance is selected again.
    @Published private(set) var runtimeIdentity = 0
    /// The direct listener faulted in this process. One-way: only a new
    /// launch clears it.
    @Published private(set) var directListenerFault = false
    /// The one Native-owned opening attempt. It outlives `didFinish` as
    /// `documentLoaded` so callbacks keep their generation fence, but its
    /// automatic budget ends there because legacy pages have no ready signal.
    @Published private(set) var runtimeOpenAttempt: RuntimeOpenAttempt?
    /// Recovery shown over the instance list after an automatic timeout.
    @Published private(set) var runtimeOpenNotice: RuntimeOpenNotice?
    @Published private(set) var lastRuntimeOpenTermination: RuntimeOpenTermination?

    let websiteDataStore: AccountWebsiteDataStore

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
    private let pushKeychain: PushKeychainStoring
    private let pushGenerationStore: PushGenerationStoring
    private let pushInstallationMarker: PushInstallationMarkerStoring
    private let pushNotificationCenter: PushNotificationCentering?
    private let pushOpenedNow: () -> Date
    private let pushOpenedLifetime: TimeInterval
    /// The App icon badge: the selected instance's Inbox total, which its Web
    /// UI sets through `badge.set` (and a push sets while the App is away).
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
    private var pendingPushOpened: PendingPushOpened?
    private var pendingPushOpenedExpiryTask: Task<Void, Never>?

    private lazy var sessionCoordinator = MobileSessionCoordinator(
        defaults: defaults,
        controlPlane: controlPlane,
        websiteDataStore: websiteDataStore,
        onOpenRuntime: { [weak self] runtime in
            _ = self?.openRuntime(runtime)
        },
        onPrepareAccountTransition: { [weak self] in
            self?.prepareForAccountTransition() ?? []
        },
        onPreparePushLogout: { [weak self] in
            // Signing out or another account: the badge counted this
            // account's Inbox. (A session refresh of the same account keeps it.)
            self?.nativeBadge.clear()
            await self?.nativePushCoordinator.prepareForLogout()
        }
    )

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
        runtimeOpenTiming: (any RuntimeOpenTiming)? = nil,
        runtimeOpenBudget: Duration = AppModel.runtimeOpenAutomaticBudget,
        runtimeOpenLongWait: Duration = .seconds(8),
        runtimeOpenNotificationCenter: NotificationCenter = .default,
        badgeCenter: AppBadgeCentering? = nil
    ) {
        self.defaults = defaults
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
        self.runtimeOpenTiming = runtimeOpenTiming ?? ContinuousRuntimeOpenTiming()
        self.runtimeOpenBudget = runtimeOpenBudget
        self.runtimeOpenLongWait = runtimeOpenLongWait
        self.runtimeOpenNotificationCenter = runtimeOpenNotificationCenter
        nativeBadge = NativeBadge(center: badgeCenter ?? SystemAppBadgeCenter())
        sessionNotifications = SessionNotificationCleaner(
            center: pushNotificationCenter ?? SystemPushNotificationCenter()
        )
        websiteDataStore = AccountWebsiteDataStore(defaults: defaults)
        _ = nativePushCoordinator
        _ = directPathController
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
            await self?.bootstrap()
        }
    }

    deinit {
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

    func showAccountRoot() {
        showRuntimeSheet(startScreen: .sheetAccount, fallback: .account)
    }

    func showInstances() {
        showRuntimeSheet(startScreen: .sheetInstances, fallback: .instances)
    }

    /// App metadata is read by the Runtime on document load. The catalog,
    /// rather than the runtime's creation-time snapshot, owns its display name.
    func selectedInstanceName() -> String? {
        guard let runtime = route.runtime,
              sessionCoordinator.isSelectedRuntime(runtime) else { return nil }
        return sessionCoordinator.currentInstances.first {
            $0.id == runtime.instance.id
                && WebOrigin(url: $0.url) == WebOrigin(url: runtime.instance.url)
        }?.name
    }

    private func showRuntimeSheet(
        startScreen: MobileOnboardingStartScreen,
        fallback: MobileOnboardingStartScreen
    ) {
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

    private func bootstrap() async {
        await websiteDataStore.sweepOrphans()
        sessionCoordinator.activateAccountSurface()
        do {
            let state = try await sessionCoordinator.bootstrap()
            if let runtime = sessionCoordinator.restorableRuntime(from: state) {
                sessionCoordinator.deactivateAccountSurface()
                openRuntime(runtime)
            } else {
                route = .onboarding(.instances)
            }
        } catch {
            route = .onboarding(.instances)
        }
        hasCompletedBootstrap = true
        if let pendingInstanceOpen {
            self.pendingInstanceOpen = nil
            switch pendingInstanceOpen {
            case .pushTap(let tap):
                handlePushTap(tap)
            case .selectedInstance(let path):
                openSelectedInstance(path: path)
            }
        }
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
            terminateRuntimeOpen(
                attemptID: attemptID,
                reason: .timedOut,
                showsNotice: true
            )
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

    func handleRuntimeNavigation(
        _ event: RuntimeNavigationEvent,
        attemptID: Int,
        runtime: MobileRuntimeContext
    ) {
        guard
            runtimeIdentity == attemptID,
            let attempt = runtimeOpenAttempt,
            attempt.id == attemptID,
            attempt.runtime == runtime,
            route.runtime == runtime,
            sessionCoordinator.isSelectedRuntime(runtime),
            !expireRuntimeOpenAttemptIfNeeded(attemptID: attemptID)
        else {
            return
        }

        switch event.stage {
        case .started:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
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
                    : .connecting,
                attemptID: attemptID
            )
        case .committed:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
            else {
                return
            }
            setRuntimeOpenPhase(
                isAuthenticationDestination(event.destination)
                    ? .authenticating
                    : .loadingPage,
                attemptID: attemptID
            )
        case .finished:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
            else {
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
                  attempt.phase != .failed
            else {
                return
            }
            pauseRuntimeOpenBudget(
                phase: attempt.phase,
                canResume: false,
                attemptID: attemptID
            )
        case .cancelled:
            guard attempt.phase != .documentLoaded,
                  attempt.phase != .failed
            else {
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
            if attempt.automaticStartedAt == nil,
               attempt.canResumeAutomaticBudget,
               !resumeRuntimeOpenBudget(attemptID: attemptID)
            {
                return
            }
            setRuntimeOpenPhase(.connecting, attemptID: attemptID)
        case .failed:
            guard attempt.phase != .failed else {
                return
            }
            if attempt.phase == .documentLoaded {
                setRuntimeOpenPhase(.failed, attemptID: attemptID)
            } else {
                finishRuntimeOpenAutomaticBudget(
                    phase: .failed,
                    attemptID: attemptID
                )
            }
        }
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
        guard sessionCoordinator.isSelectedRuntime(runtime) else {
            return false
        }
        if let pendingPushOpened,
           pendingPushOpened.instanceOrigin
                != WebOrigin(url: runtime.instance.url)?.serialized
        {
            clearPendingPushOpened()
        }
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
                && self.sessionCoordinator.isSelectedRuntime(runtime)
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
            guard self.sessionCoordinator.isSelectedRuntime(runtime) else {
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
            directPathController.runtimeDidOpen(runtime)
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
            } else if !sessionCoordinator.isSelectedRuntime(deferred.runtime) {
                // The selection ended underneath; do not leave the spinner
                // or its attempt alive.
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
    func openSelectedInstance(path: String) {
        guard hasCompletedBootstrap else {
            pendingInstanceOpen = .selectedInstance(path: path)
            return
        }
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
        clearPendingPushOpened()
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
        let barriers = pendingRuntimeReleaseBarriers
        pendingRuntimeReleaseBarriers.removeAll()
        return barriers
    }
}
