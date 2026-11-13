import Foundation
import os
import UIKit

// The direct transport over iroh (rows A1 and A2 of
// docs/plans/remote-direct-phase1.md; docs/plans/remote-direct-ios.md
// section 12). Nothing in the App creates it: `AvibeApp` registers no
// transport, and no build links an iroh binary yet.
// Design: docs/plans/remote-direct-ios-transport.md.

/// What the transport is asked to reach: one opened runtime's instance.
struct DirectIrohSelection: Equatable, Sendable {
    /// Issued by the path controller for each opened runtime and each
    /// return from the background, so two selections are never equal.
    let number: Int
    /// The instance's identifier in the mobile session: the rendezvous
    /// answer's `instance_id` must equal it.
    let instanceID: String
    /// The instance's exact host: the answer's `hostname`, and the
    /// authority of the CONNECT a probe sends.
    let host: String
    /// The account data store the runtime opened on.
    let storeIdentifier: UUID
}

/// The tags one direct stream is opened under.
struct DirectIrohStreamTag: Equatable, Sendable {
    let selection: DirectIrohSelection
    /// The `generation` of the rendezvous answer the path was found with.
    let rendezvousGeneration: String
    /// Issued by the path controller; it advances at every event of the
    /// path-generation table (docs/plans/remote-direct-ios.md section 7).
    let pathGeneration: Int
}

enum DirectIrohReport: Equatable, Sendable {
    /// A path to the instance answered a CONNECT. `generation` is the
    /// rendezvous answer's.
    case usable(generation: String)
    /// The network changed and the path is being probed again.
    case verifying
    /// The path is gone, or its probe failed.
    case lost
}

/// Fetches the rendezvous answer for a selection, natively and on that
/// selection's account data store (`DirectRendezvousFetcher`). It throws
/// `DirectRendezvousFetchError`.
typealias DirectIrohRendezvousFetch =
    @MainActor @Sendable (DirectIrohSelection, DirectEndpointID) async throws -> Data

/// The probe: one stream, the canonical CONNECT, and the instance's `200`.
/// It shows that the whole chain serves this host: admission, the sidecar,
/// the stream listener, and the strict ingress.
enum DirectIrohProbe {
    static let maxAnswerBytes = 4 * 1024

    /// The request head of docs/plans/remote-direct-ingress.md section 3.
    /// `nil` unless `host` is a lowercase DNS name: it is written into a
    /// request head, so it may hold only host-name characters.
    static func connectHead(for host: String) -> Data? {
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard
            host.utf8.count <= 253,
            let last = labels.last,
            labels.allSatisfy({ label in
                !label.isEmpty
                    && label.utf8.allSatisfy { byte in
                        (UInt8(ascii: "a")...UInt8(ascii: "z")).contains(byte)
                            || (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
                            || byte == UInt8(ascii: "-")
                    }
            }),
            // A numeric last label is how an IPv4 literal reads.
            !last.utf8.allSatisfy({ (UInt8(ascii: "0")...UInt8(ascii: "9")).contains($0) })
        else {
            return nil
        }
        return Data("CONNECT \(host):443 HTTP/1.1\r\nHost: \(host):443\r\n\r\n".utf8)
    }

    /// Whether `answer`, a complete response head, is the instance's `200`.
    static func isEstablished(_ answer: Data) -> Bool {
        guard let lineEnd = answer.range(of: Data("\r\n".utf8)) else {
            return false
        }
        let line = String(decoding: answer[answer.startIndex..<lineEnd.lowerBound], as: UTF8.self)
        return line == "HTTP/1.1 200" || line.hasPrefix("HTTP/1.1 200 ")
    }

    /// Throws unless the instance answers within `seconds`. The stream is
    /// abandoned either way: a probe carries nothing.
    ///
    /// `lease` is asked in the step that starts each of the probe's
    /// network actions: the open, the CONNECT, and each read. An action it
    /// refuses is not started, and the probe ends there with `stale`, which
    /// says nothing about the path.
    static func run(
        on connection: any DirectIrohConnection,
        host: String,
        within seconds: TimeInterval,
        lease: DirectIrohLeaseDeadline
    ) async throws {
        guard let head = connectHead(for: host) else {
            throw DirectIrohError.invalidHost
        }
        let allowed: @Sendable () -> Bool = {
            lease.allowsStart
        }
        try await directIrohDeadline(seconds) {
            let halves = try await directIrohCall(
                allowed: allowed,
                discard: { (late: DirectIrohStreamHalves) -> Void in
                    DirectIrohByteStream.abort(late)
                },
                { () async throws -> DirectIrohStreamHalves in
                    try await connection.openStream()
                }
            )
            defer {
                DirectIrohByteStream.abort(halves)
            }
            try await directIrohCall(allowed: allowed) { () async throws -> Void in
                try await halves.send.writeAll(head)
            }
            let terminator = Data("\r\n\r\n".utf8)
            var answer = Data()
            while answer.range(of: terminator) == nil {
                let limit = maxAnswerBytes - answer.count
                guard limit > 0 else {
                    throw DirectIrohError.probeRefused
                }
                let chunk = try await directIrohCall(allowed: allowed) { () async throws -> Data in
                    try await halves.receive.read(sizeLimit: limit)
                }
                guard !chunk.isEmpty else {
                    throw DirectIrohError.probeRefused
                }
                answer.append(chunk)
            }
            guard isEstablished(answer) else {
                throw DirectIrohError.probeRefused
            }
        }
    }
}

/// The direct transport: for the selected instance it fetches the
/// rendezvous, dials the instance's endpoint, probes the path, reports
/// `usable`, `verifying`, and `lost`, and opens streams on it.
///
/// Its four entry points are the `DirectTransporting` requirements of the
/// persistent-proxy seam. Its rules:
///
/// - **Nothing exists before a `ready` answer the App accepted:** no
///   endpoint, no UDP socket, and so no Local Network prompt. The launch
///   key is made at the first selection and opens nothing.
/// - **One path at a time.** A session is one endpoint and one connection,
///   for one answer. Ending it closes its streams, its connection, and its
///   endpoint, which is also what ends every binding call left running.
/// - **Nothing starts on the network without the right to, asked where it
///   starts.** Each bind, connect, probe action, and stream open goes
///   through `directIrohCall`, which starts it only if its caller has not
///   left and its lease allows it at that moment. A dial or a probe that
///   ended, however it ended, has no caller left to start anything; a
///   stage that ends late is followed by nothing.
/// - **Everything late is dropped.** Each piece of work carries a stamp;
///   a result whose stamp is no longer the current one changes nothing,
///   and whatever it produced is closed.
/// - **Nothing lives in the background,** whatever the caller does.
@MainActor
final class DirectIrohTransport {
    private struct Link: Sendable {
        let endpoint: any DirectIrohEndpoint
        let connection: any DirectIrohConnection

        func close() {
            connection.close()
            let endpoint = endpoint
            Task {
                await endpoint.close()
            }
        }
    }

    /// An accepted answer and the moment it runs out, on the clock that
    /// never runs backwards. The deadline is data: it is compared with the
    /// clock wherever something new would be started, committed, or
    /// reported under the answer. The timer set for it only wakes the
    /// transport up.
    private struct Lease {
        let offer: DirectRendezvousOffer
        let deadline: TimeInterval
    }

    private struct WeakStream {
        weak var stream: DirectIrohByteStream?
    }

    /// How a probe ended.
    private enum ProbeVerdict {
        /// The instance answered.
        case passed
        /// It did not: the path is taken for lost.
        case failed
        /// One of the probe's actions was not allowed to start. That says
        /// something about the probe's right to act, and nothing about the
        /// path.
        case refused
    }

    private enum ProbeReason {
        /// The network changed under a usable path.
        case networkChange
        /// A direct candidate failed before anything was forwarded.
        case candidateFailure
        /// An answer that had run out was renewed.
        case renewal
    }

    /// One endpoint and one connection, for one accepted answer.
    private final class Session {
        let id: Int
        let offer: DirectRendezvousOffer
        let link: Link
        /// `false` while the path is probed again, and once its answer has
        /// run out.
        var isUsable = true
        /// The answer's lifetime passed without a renewed answer. Nothing
        /// new is opened or probed on the path until one arrives.
        var isExpired = false
        /// The right to start streams on the path. It carries the lease's
        /// deadline, renewed by a newly accepted answer, and how many
        /// times the path stopped being usable.
        let authority: DirectIrohAuthority
        private var streams: [WeakStream] = []

        init(id: Int, offer: DirectRendezvousOffer, link: Link, authority: DirectIrohAuthority) {
            self.id = id
            self.offer = offer
            self.link = link
            self.authority = authority
        }

        /// The path stops being usable for anything new. `true` if it was
        /// usable until now.
        func suspend() -> Bool {
            guard isUsable else {
                return false
            }
            isUsable = false
            authority.suspend()
            return true
        }

        func adopt(_ stream: DirectIrohByteStream) {
            streams.removeAll { $0.stream == nil }
            streams.append(WeakStream(stream: stream))
        }

        func cancelStreams() {
            for entry in streams {
                entry.stream?.cancel()
            }
            streams.removeAll()
        }
    }

    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.avibe",
        category: "DirectTransport"
    )

    private let engine: any DirectIrohEngine
    private let fetchRendezvous: DirectIrohRendezvousFetch
    private let network: any DirectIrohNetworkMonitoring
    private let scheduler: any DirectIrohScheduling
    private let timing: DirectIrohTiming
    private let notificationCenter: NotificationCenter
    /// A clock that never runs backwards: seconds since the device started.
    private let uptime: @Sendable () -> TimeInterval
    private var lifecycleObservers: [NSObjectProtocol] = []

    /// Made at the first selection; kept in memory for this launch only.
    private var launchKey: DirectIrohLaunchKey?
    private var selection: DirectIrohSelection?
    private var report: (@MainActor (DirectIrohReport) -> Void)?
    private var isInBackground: Bool

    /// Issues the stamps below and the session identifiers.
    private var stamp = 0
    private var fetchStamp: Int?
    private var fetchTask: Task<Void, Never>?
    private var dialStamp: Int?
    private var dialTask: Task<Void, Never>?
    private var probeStamp: Int?
    private var probeTask: Task<Void, Never>?
    /// Why the probe under way was started, to start it again when a newly
    /// accepted answer replaces the lease it runs under.
    private var probeReason: ProbeReason?
    /// The next rendezvous request: a retry, or the refresh of a live path.
    private var fetchTimer: (any DirectIrohTimer)?
    /// The accepted answer in force: for the dial under way, or for the
    /// session. Only a newly accepted answer replaces it.
    private var lease: Lease?
    /// A wake-up at the lease's deadline. Not the authority on it.
    private var leaseTimer: (any DirectIrohTimer)?
    private var debounceTimer: (any DirectIrohTimer)?
    private var stableTimer: (any DirectIrohTimer)?
    private var spacingTimer: (any DirectIrohTimer)?
    /// The live path. Whenever this is cleared or replaced, the path's
    /// authority is revoked.
    private var session: Session? {
        didSet {
            if oldValue !== session {
                oldValue?.authority.revoke()
            }
        }
    }

    /// Consecutive failures: the index into the retry ladder.
    private var failures = 0
    private var pageRetries = 0
    private var isSpacingFailureProbes = false
    /// The highest path generation seen in a tag, and the lowest still
    /// accepted. Every report that ends or suspends a path makes the
    /// controller advance its generation, so a tag at or below the highest
    /// one seen by then is stale. This is a second line: the controller's
    /// own check at the commit point is the authority on path generations.
    private var pathHighWater = -1
    private var pathFloor = 0

    init(
        engine: any DirectIrohEngine,
        fetchRendezvous: @escaping DirectIrohRendezvousFetch,
        network: (any DirectIrohNetworkMonitoring)? = nil,
        scheduler: (any DirectIrohScheduling)? = nil,
        timing: DirectIrohTiming = .standard,
        notificationCenter: NotificationCenter = .default,
        isInBackground: Bool? = nil,
        uptime: @escaping @Sendable () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.engine = engine
        self.fetchRendezvous = fetchRendezvous
        self.network = network ?? DirectIrohPathMonitor()
        self.scheduler = scheduler ?? DirectIrohTaskScheduler()
        self.timing = timing
        self.notificationCenter = notificationCenter
        self.uptime = uptime
        // The App can be launched into the background.
        self.isInBackground = isInBackground
            ?? (UIApplication.shared.applicationState == .background)

        lifecycleObservers = [
            notificationCenter.addObserver(
                forName: UIApplication.didEnterBackgroundNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.applicationDidEnterBackground()
                }
            },
            notificationCenter.addObserver(
                forName: UIApplication.willEnterForegroundNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.applicationWillEnterForeground()
                }
            },
        ]
    }

    deinit {
        for observer in lifecycleObservers {
            notificationCenter.removeObserver(observer)
        }
        fetchTask?.cancel()
        dialTask?.cancel()
        probeTask?.cancel()
        // Cancelling those tasks closes the gate of every action they were
        // waiting on. An open is waited on by someone else: the path's
        // authority is what refuses it.
        session?.authority.revoke()
        session?.link.close()
    }

    // MARK: The seam

    /// A runtime opened for `selection`. Work for the previous selection
    /// stops, and `report` hears whenever this selection's path becomes
    /// usable, is being verified, or is lost.
    func select(
        _ selection: DirectIrohSelection,
        report: @escaping @MainActor (DirectIrohReport) -> Void
    ) {
        stopWork()
        self.selection = selection
        self.report = report
        failures = 0
        pageRetries = 0
        // Path generations are compared within one selection only: nothing
        // is assumed about how the controller numbers them across
        // selections. A tag of another selection is refused as such.
        pathHighWater = -1
        pathFloor = 0
        guard !isInBackground else {
            return
        }
        startNetworkWatch()
        startFetch()
    }

    /// No instance is selected: every stream, the connection, and the
    /// endpoint close, and nothing is asked or dialled any more.
    func deselect() {
        stopWork()
        selection = nil
        report = nil
        network.stop()
    }

    /// A direct candidate opened under `tag` failed before anything was
    /// forwarded. The path is probed: if it answers, the candidate was
    /// refused or slow and the path stays; if not, it is lost.
    func pathDidFail(for tag: DirectIrohStreamTag) {
        reconcileLease()
        guard
            let session,
            session.isUsable,
            tag.selection == selection,
            tag.rendezvousGeneration == session.offer.generation,
            tag.pathGeneration >= pathFloor,
            probeStamp == nil,
            !isSpacingFailureProbes
        else {
            return
        }
        pathHighWater = max(pathHighWater, tag.pathGeneration)
        startProbe(.candidateFailure)
    }

    /// Opens one stream over the path reported usable for `tag`. Throws
    /// when that path is gone or the tag is stale, and at once when the
    /// caller's task is cancelled. A stream that opens after either is
    /// abandoned, never returned.
    nonisolated func openStream(for tag: DirectIrohStreamTag) async throws -> DirectIrohByteStream {
        let admitted = try await admit(tag)
        let connection = admitted.connection
        let halves = try await directIrohCall(
            // Asked again where the open starts: the admission was on the
            // main actor, some awaits ago, and the path is not this
            // caller's to keep.
            allowed: {
                admitted.authority.allowsStart(admittedAt: admitted.suspensions)
            },
            discard: { (late: DirectIrohStreamHalves) -> Void in
                DirectIrohByteStream.abort(late)
            },
            { () async throws -> DirectIrohStreamHalves in
                try await connection.openStream()
            }
        )
        let stream = DirectIrohByteStream(halves)
        guard await adopt(stream, admitted) else {
            stream.cancel()
            throw DirectIrohError.stale
        }
        return stream
    }

    // MARK: Streams

    private struct Admission: Sendable {
        let connection: any DirectIrohConnection
        let sessionID: Int
        let authority: DirectIrohAuthority
        let suspensions: Int
    }

    private func admit(_ tag: DirectIrohStreamTag) throws -> Admission {
        reconcileLease()
        guard
            let session,
            session.isUsable,
            tag.selection == selection,
            tag.rendezvousGeneration == session.offer.generation,
            tag.pathGeneration >= pathFloor
        else {
            throw DirectIrohError.stale
        }
        pathHighWater = max(pathHighWater, tag.pathGeneration)
        return Admission(
            connection: session.link.connection,
            sessionID: session.id,
            authority: session.authority,
            suspensions: session.authority.suspensionCount
        )
    }

    /// `false` when, while the stream opened, the path it was opened on
    /// ended or stopped being usable, even if it is usable again by now:
    /// every such event made the controller advance its path generation,
    /// so the tag the stream was asked under is stale.
    private func adopt(_ stream: DirectIrohByteStream, _ admitted: Admission) -> Bool {
        reconcileLease()
        guard
            let session,
            session.id == admitted.sessionID,
            session.authority.allowsStart(admittedAt: admitted.suspensions),
            session.isUsable
        else {
            return false
        }
        session.adopt(stream)
        return true
    }

    private func raiseFloor() {
        pathFloor = max(pathFloor, pathHighWater + 1)
    }

    // MARK: Rendezvous

    private func startFetch() {
        fetchTimer?.cancel()
        fetchTimer = nil
        cancelFetch()
        guard
            let selection,
            !isInBackground,
            DirectIrohProbe.connectHead(for: selection.host) != nil
        else {
            return
        }
        let key: DirectIrohLaunchKey
        if let existing = launchKey {
            key = existing
        } else if let made = try? engine.makeLaunchKey() {
            launchKey = made
            key = made
        } else {
            fetchDidFail(DirectIrohError.noLaunchKey)
            return
        }
        stamp += 1
        let token = stamp
        fetchStamp = token
        let fetch = fetchRendezvous
        let seconds = timing.fetchSeconds
        let askedAt = uptime()
        fetchTask = Task { @MainActor [weak self] in
            let result: Result<Data, Error>
            do {
                let data: Data = try await directIrohDeadline(seconds) {
                    // No lease gives the right to ask: the request is sent
                    // only if it is still the current one in the step
                    // that sends it (`sendRequest`).
                    try await directIrohCall(allowed: { true }) { [weak self] () async throws -> Data in
                        guard let self else {
                            throw DirectIrohError.stale
                        }
                        return try await self.sendRequest(
                            token,
                            fetch: fetch,
                            selection: selection,
                            clientEndpointID: key.endpointID
                        )
                    }
                }
                result = .success(data)
            } catch {
                result = .failure(error)
            }
            guard let self, self.fetchStamp == token else {
                return
            }
            // Before the answer is read, and while this request still
            // counts as under way: a lease the clock has passed is marked
            // so, and an answer of the same generation then renews it
            // through a probe.
            self.reconcileLease()
            self.fetchStamp = nil
            self.fetchTask = nil
            self.fetchDidFinish(result, selection: selection, key: key, askedAt: askedAt)
        }
    }

    /// Sends the request, unless it stopped being the current one between
    /// the decision to send it and this step: the selection ended, the App
    /// went to the background, or a newer request replaced it.
    private func sendRequest(
        _ token: Int,
        fetch: DirectIrohRendezvousFetch,
        selection: DirectIrohSelection,
        clientEndpointID: DirectEndpointID
    ) async throws -> Data {
        guard fetchStamp == token else {
            throw DirectIrohError.stale
        }
        return try await fetch(selection, clientEndpointID)
    }

    private func fetchDidFinish(
        _ result: Result<Data, Error>,
        selection: DirectIrohSelection,
        key: DirectIrohLaunchKey,
        askedAt: TimeInterval
    ) {
        let answer: DirectRendezvousAnswer
        do {
            answer = try DirectRendezvous.answer(
                from: try result.get(),
                expecting: DirectRendezvousExpectation(
                    instanceID: selection.instanceID,
                    hostname: selection.host,
                    clientEndpointID: key.endpointID
                )
            )
        } catch {
            fetchDidFail(error)
            return
        }
        pageRetries = 0
        switch answer {
        case .unavailable(let retryAfterSeconds):
            Self.logger.info("Direct transport: the instance offers no direct path")
            // The newest accepted answer speaks for the instance: whatever
            // an earlier one started, a dial under way included, ends.
            let hadSession = session != nil
            cancelDial()
            endSession()
            scheduleFetch(after: TimeInterval(retryAfterSeconds))
            if hadSession {
                emit(.lost)
            }
        case .ready(let offer):
            // The instance issued the answer after the request was sent, so
            // a deadline counted from then is never later than the
            // instance's own, whatever the phone's wall clock says and
            // however long the answer took to arrive.
            let accepted = Lease(
                offer: offer,
                deadline: askedAt + TimeInterval(offer.lifetimeSeconds)
            )
            let now = uptime()
            let remaining = accepted.deadline - now
            // An answer with too little left is not worth a dial, and an
            // instance that keeps sending such answers is asked no faster
            // than the retry ladder allows.
            guard remaining >= timing.minimumLifetimeSeconds else {
                fetchDidFail(DirectRendezvousRejection.invalidLifetime)
                return
            }
            let refresh = max(
                timing.refreshRetrySeconds,
                TimeInterval(offer.lifetimeSeconds) * timing.refreshFraction - max(0, now - askedAt)
            )
            if
                let session,
                session.offer.generation == offer.generation,
                session.offer.endpointID == offer.endpointID
            {
                // The same endpoint and addresses: the path stays, under
                // the new answer's deadline.
                lease = accepted
                session.authority.renew(deadline: accepted.deadline)
                armLeaseTimer()
                scheduleFetch(after: refresh)
                if session.isExpired {
                    // Renewed after the old answer had run out, whether or
                    // not its timer had fired: usable again only once the
                    // path answers a probe.
                    session.isExpired = false
                    startProbe(.renewal)
                } else if let reason = probeReason, probeStamp != nil {
                    // A probe is under way on the lease this answer
                    // replaces. Its right to act ends with that lease and
                    // is not extended: the probe is retired, and the same
                    // question is asked again under the new lease.
                    startProbe(reason)
                }
                return
            }
            // A new endpoint key or address set. The old path is retired
            // before the new one is dialled: new connections use the
            // public origin until the new path answers.
            let hadSession = session != nil
            endSession()
            startDial(under: accepted, key: key, host: selection.host)
            scheduleFetch(after: refresh)
            if hadSession {
                emit(.lost)
            }
        }
    }

    // MARK: The lease

    private func armLeaseTimer() {
        leaseTimer?.cancel()
        leaseTimer = nil
        guard let lease else {
            return
        }
        leaseTimer = scheduler.schedule(after: max(0, lease.deadline - uptime())) { [weak self] in
            guard let self else {
                return
            }
            self.leaseTimer = nil
            // The clock decides, not the timer: one that fires early only
            // sets itself again.
            self.reconcileLease()
            if let lease = self.lease, self.uptime() < lease.deadline {
                self.armLeaseTimer()
            }
        }
    }

    private func dropLease() {
        leaseTimer?.cancel()
        leaseTimer = nil
        lease = nil
    }

    /// Compares the lease with the clock and, if it has run out, makes the
    /// state say so. It runs first wherever the main actor commits or
    /// reports something under the lease: before a stream is admitted or
    /// adopted, before a probe is started, when a request, a dial, or a
    /// probe ends, on `pathDidFail`, on a network change, and when the
    /// lease's wake-up or the next-request timer fires. It does not guard
    /// the network actions themselves: those run in other tasks, and each
    /// asks its `DirectIrohAuthority` in the step that starts it.
    private func reconcileLease() {
        guard let lease, uptime() >= lease.deadline else {
            return
        }
        guard let session else {
            // Still dialling under it: the dial is given up.
            if dialStamp != nil {
                cancelDial()
                scheduleRetry()
            } else {
                dropLease()
            }
            return
        }
        guard !session.isExpired else {
            return
        }
        // No newer answer was accepted in time: every refresh failed, or
        // none came back. Nothing is opened or probed on the path any more.
        // It is kept but not usable, so what it already carries is not
        // closed on the phone's own timing; the instance closes the
        // connection when the admission ends, and that is reported as
        // `lost`.
        Self.logger.info("Direct transport: the rendezvous answer ran out")
        session.isExpired = true
        cancelProbe()
        debounceTimer?.cancel()
        debounceTimer = nil
        let wasUsable = session.suspend()
        raiseFloor()
        ensureAsking()
        if wasUsable {
            emit(.verifying)
        }
    }

    /// Makes sure the rendezvous is asked again, unless a request is under
    /// way or already set.
    private func ensureAsking() {
        if fetchStamp == nil, fetchTimer == nil {
            startFetch()
        }
    }

    private func fetchDidFail(_ error: Error) {
        if let rejection = error as? DirectRendezvousRejection {
            Self.logger.info(
                "Direct transport: rendezvous answer not accepted: \(String(describing: rejection), privacy: .public)"
            )
        }
        if session != nil || dialStamp != nil {
            // A refresh failed while an accepted answer is in force, for a
            // path or for a dial under way. It extends nothing and ends
            // nothing: the lease runs as it was going to, and the
            // rendezvous is asked again soon.
            scheduleFetch(after: timing.refreshRetrySeconds)
            return
        }
        if
            let fetchError = error as? DirectRendezvousFetchError,
            fetchError == .pageNotReady,
            pageRetries < timing.pageRetryLimit
        {
            pageRetries += 1
            scheduleFetch(after: timing.pageRetrySeconds)
            return
        }
        scheduleRetry()
    }

    private func scheduleRetry() {
        let ladder = timing.retryLadder
        let delay = ladder.isEmpty ? 1 : ladder[min(failures, ladder.count - 1)]
        failures += 1
        scheduleFetch(after: delay)
    }

    private func scheduleFetch(after seconds: TimeInterval) {
        fetchTimer?.cancel()
        fetchTimer = scheduler.schedule(after: seconds) { [weak self] in
            guard let self else {
                return
            }
            self.fetchTimer = nil
            self.reconcileLease()
            self.ensureAsking()
        }
    }

    private func cancelFetch() {
        fetchTask?.cancel()
        fetchTask = nil
        fetchStamp = nil
    }

    // MARK: Dial

    private func startDial(under accepted: Lease, key: DirectIrohLaunchKey, host: String) {
        cancelDial()
        lease = accepted
        armLeaseTimer()
        let offer = accepted.offer
        stamp += 1
        let token = stamp
        dialStamp = token
        // Fixed at the lease the dial starts under. A newer answer replaces
        // the dial; it never lends this one its time.
        let deadline = DirectIrohLeaseDeadline(deadline: accepted.deadline, clock: uptime)
        let engine = engine
        let timing = timing
        dialTask = Task { @MainActor [weak self] in
            let outcome = await DirectIrohTransport.dial(
                offer,
                key: key,
                host: host,
                engine: engine,
                timing: timing,
                lease: deadline
            )
            // A dial that ends after its lease ran out is given up here,
            // before its result can be taken.
            self?.reconcileLease()
            guard let self, self.dialStamp == token else {
                // Deselected, replaced, run out, or in the background
                // meanwhile.
                if case .success(let link) = outcome {
                    link.close()
                }
                return
            }
            self.dialStamp = nil
            self.dialTask = nil
            self.dialDidFinish(outcome, lease: accepted)
        }
    }

    /// Binds an endpoint, dials the answer's endpoint at the admitted
    /// addresses, checks the peer's identity, and probes.
    ///
    /// Each stage starts through `directIrohCall` with the dial's lease: it
    /// starts only if the lease allows it at that moment and the dial is
    /// still waiting for it. A bind or a connect that ends after the lease
    /// ran out is followed by nothing, and so is one whose dial was timed
    /// out, withdrawn, or replaced in the meantime: the stage's task finds
    /// nobody waiting. A stage that does not start closes what the stage
    /// before produced. On any failure, and when its task is cancelled,
    /// everything the dial opened is closed; closing the endpoint is what
    /// ends a dial the binding still runs.
    private nonisolated static func dial(
        _ offer: DirectRendezvousOffer,
        key: DirectIrohLaunchKey,
        host: String,
        engine: any DirectIrohEngine,
        timing: DirectIrohTiming,
        lease: DirectIrohLeaseDeadline
    ) async -> Result<Link, Error> {
        let allowed: @Sendable () -> Bool = {
            lease.allowsStart
        }
        // An endpoint or a connection that arrives when nobody takes it any
        // more is closed, whichever step it was lost at: a call that was
        // left, or a deadline that was taken in the same instant.
        let closeEndpoint: @Sendable (any DirectIrohEndpoint) -> Void = { late in
            _ = Task {
                await late.close()
            }
        }
        let closeConnection: @Sendable (any DirectIrohConnection) -> Void = { late in
            late.close()
        }
        let endpoint: any DirectIrohEndpoint
        do {
            endpoint = try await directIrohDeadline(timing.bindSeconds, discard: closeEndpoint) {
                try await directIrohCall(
                    allowed: allowed,
                    discard: closeEndpoint,
                    { () async throws -> any DirectIrohEndpoint in
                        try await engine.bind(secretKey: key.secret)
                    }
                )
            }
        } catch {
            return .failure(error)
        }
        do {
            let addresses = offer.addresses.map(\.socketAddress)
            let alpn = Data(DirectRendezvous.alpn.utf8)
            let connection: any DirectIrohConnection = try await directIrohDeadline(
                timing.dialSeconds,
                discard: closeConnection
            ) {
                try await directIrohCall(
                    allowed: allowed,
                    discard: closeConnection,
                    { () async throws -> any DirectIrohConnection in
                        try await endpoint.connect(
                            to: offer.endpointID,
                            addresses: addresses,
                            alpn: alpn
                        )
                    }
                )
            }
            do {
                // The handshake already proves the identity that was
                // dialled; this is the second line.
                guard connection.remoteEndpointID == offer.endpointID else {
                    throw DirectIrohError.wrongPeer
                }
                try await DirectIrohProbe.run(
                    on: connection,
                    host: host,
                    within: timing.probeSeconds,
                    lease: lease
                )
            } catch {
                connection.close()
                throw error
            }
            return .success(Link(endpoint: endpoint, connection: connection))
        } catch {
            Task {
                await endpoint.close()
            }
            return .failure(error)
        }
    }

    private func dialDidFinish(_ outcome: Result<Link, Error>, lease accepted: Lease) {
        let offer = accepted.offer
        guard case .success(let link) = outcome else {
            Self.logger.info("Direct transport: no path to the instance")
            dropLease()
            scheduleRetry()
            return
        }
        stamp += 1
        let session = Session(
            id: stamp,
            offer: offer,
            link: link,
            authority: DirectIrohAuthority(deadline: accepted.deadline, clock: uptime)
        )
        self.session = session
        watchClose(of: session)
        stableTimer?.cancel()
        stableTimer = scheduler.schedule(after: timing.stableSeconds) { [weak self] in
            self?.stableTimer = nil
            self?.failures = 0
        }
        Self.logger.info("Direct transport: usable")
        emit(.usable(generation: offer.generation))
    }

    private func cancelDial() {
        // Cancelling the task closes the gate of whatever stage it was
        // waiting on, at once: a stage that has not started does not.
        dialTask?.cancel()
        dialTask = nil
        dialStamp = nil
        if session == nil {
            // The answer that was being dialled is given up with its dial.
            dropLease()
        }
    }

    // MARK: The live path

    private func watchClose(of session: Session) {
        let connection = session.link.connection
        let id = session.id
        Task { @MainActor [weak self] in
            await connection.closed()
            self?.connectionDidClose(id)
        }
    }

    private func connectionDidClose(_ id: Int) {
        guard let session, session.id == id else {
            return
        }
        sessionDidEnd(retryNow: false)
    }

    /// The path is gone. `lost` is reported after the next attempt is
    /// arranged, so new connections are already on the public origin.
    private func sessionDidEnd(retryNow: Bool) {
        Self.logger.info("Direct transport: lost")
        endSession()
        cancelFetch()
        if retryNow {
            failures = 0
            pageRetries = 0
            startFetch()
        } else {
            scheduleRetry()
        }
        emit(.lost)
    }

    private func startProbe(_ reason: ProbeReason) {
        // Nothing is probed under an answer that has run out.
        reconcileLease()
        guard let session, !session.isExpired, let selection else {
            return
        }
        cancelProbe()
        stamp += 1
        let token = stamp
        probeStamp = token
        let connection = session.link.connection
        let host = selection.host
        let seconds = timing.probeSeconds
        // Fixed at the deadline of the lease in force now. It is asked
        // where each of the probe's actions starts, which is after this
        // task was scheduled and after its own awaits. A newer answer does
        // not extend it: it retires this probe and starts another.
        let deadline = DirectIrohLeaseDeadline(
            deadline: lease?.deadline ?? uptime(),
            clock: uptime
        )
        probeReason = reason
        probeTask = Task { @MainActor [weak self] in
            let verdict: ProbeVerdict
            do {
                try await DirectIrohProbe.run(
                    on: connection,
                    host: host,
                    within: seconds,
                    lease: deadline
                )
                verdict = .passed
            } catch DirectIrohError.stale {
                verdict = .refused
            } catch {
                verdict = .failed
            }
            // A probe that ends after the lease ran out reports nothing.
            self?.reconcileLease()
            guard let self, self.probeStamp == token else {
                return
            }
            self.probeStamp = nil
            self.probeTask = nil
            self.probeReason = nil
            self.probeDidFinish(verdict, reason: reason)
        }
    }

    private func probeDidFinish(_ verdict: ProbeVerdict, reason: ProbeReason) {
        guard let session, !session.isExpired else {
            return
        }
        switch verdict {
        case .passed:
            break
        case .failed:
            sessionDidEnd(retryNow: reason == .networkChange)
            return
        case .refused:
            // The probe was not allowed to finish, and the lease in force
            // has not run out: it is no verdict on the path. The question
            // is asked again under the lease in force now.
            startProbe(reason)
            return
        }
        switch reason {
        case .candidateFailure:
            // The path answers: the failed candidate was refused or slow.
            isSpacingFailureProbes = true
            spacingTimer?.cancel()
            spacingTimer = scheduler.schedule(after: timing.failureProbeSpacingSeconds) { [weak self] in
                self?.spacingTimer = nil
                self?.isSpacingFailureProbes = false
            }
        case .networkChange:
            session.isUsable = true
            // The contract asks again after a network change. An answer of
            // the same generation keeps this path.
            startFetch()
            emit(.usable(generation: session.offer.generation))
        case .renewal:
            session.isUsable = true
            emit(.usable(generation: session.offer.generation))
        }
    }

    private func cancelProbe() {
        // As for a dial: cancelling the task closes the gate of the action
        // it was waiting on.
        probeTask?.cancel()
        probeTask = nil
        probeStamp = nil
        probeReason = nil
    }

    /// Closes the live path, if there is one: its streams, its connection,
    /// and its endpoint. Reports nothing.
    private func endSession() {
        cancelProbe()
        debounceTimer?.cancel()
        debounceTimer = nil
        stableTimer?.cancel()
        stableTimer = nil
        spacingTimer?.cancel()
        spacingTimer = nil
        isSpacingFailureProbes = false
        guard let session else {
            return
        }
        dropLease()
        self.session = nil
        raiseFloor()
        session.cancelStreams()
        session.link.close()
    }

    private func stopWork() {
        fetchTimer?.cancel()
        fetchTimer = nil
        cancelFetch()
        cancelDial()
        endSession()
    }

    private func emit(_ report: DirectIrohReport) {
        self.report?(report)
    }

    // MARK: The network and the App's lifecycle

    private func startNetworkWatch() {
        network.start { [weak self] in
            self?.networkDidChange()
        }
    }

    /// A network callback alone is not proof of loss: a live path is
    /// reported as `verifying` and probed once the network is quiet.
    private func networkDidChange() {
        guard selection != nil, !isInBackground else {
            return
        }
        reconcileLease()
        debounceTimer?.cancel()
        if let session {
            guard !session.isExpired else {
                // Nothing is probed under an answer that has run out: the
                // rendezvous is asked again instead.
                debounceTimer = scheduler.schedule(after: timing.networkDebounceSeconds) { [weak self] in
                    self?.debounceTimer = nil
                    self?.startFetch()
                }
                return
            }
            cancelProbe()
            let wasUsable = session.suspend()
            raiseFloor()
            debounceTimer = scheduler.schedule(after: timing.networkDebounceSeconds) { [weak self] in
                self?.debounceTimer = nil
                self?.startProbe(.networkChange)
            }
            if wasUsable {
                emit(.verifying)
            }
            return
        }
        // Nothing is usable yet. Whatever was under way belongs to the old
        // network: start over on the new one.
        fetchTimer?.cancel()
        fetchTimer = nil
        cancelFetch()
        cancelDial()
        failures = 0
        pageRetries = 0
        debounceTimer = scheduler.schedule(after: timing.networkDebounceSeconds) { [weak self] in
            self?.debounceTimer = nil
            self?.startFetch()
        }
    }

    private func applicationDidEnterBackground() {
        guard !isInBackground else {
            return
        }
        isInBackground = true
        let hadSession = session != nil
        stopWork()
        network.stop()
        if hadSession {
            emit(.lost)
        }
    }

    private func applicationWillEnterForeground() {
        guard isInBackground else {
            return
        }
        isInBackground = false
        guard selection != nil else {
            return
        }
        failures = 0
        pageRetries = 0
        startNetworkWatch()
        startFetch()
    }
}
