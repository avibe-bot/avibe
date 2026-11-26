import Foundation
import Network

// 「线路测速」 on the iPhone: one measurement of the path the App uses to an
// instance now and of the Cloudflare tunnel, on demand (`direct.measure()`,
// docs/plans/ios-app.md section 4). The same basis as the desktop app's
// (`desktop/src-tauri/src/direct/measure.rs`): for each path one
// connection, one untimed warm-up request, then five timed HTTPS
// `GET /health` round trips on that same connection.
//
// A measurement is a diagnosis and nothing else. It makes no plan, takes
// part in no race, and changes no path, choice, stickiness, closure or
// count; under 「仅直连和中继」 too.
//
// - The path in use is measured through a loopback port of its own
//   (`DirectMeasurePort`), open for the measurement only. That port accepts
//   one request, `CONNECT <the instance's host>:443`, and answers it over the
//   App's admitted direct or relay link and over nothing else: it has no way
//   to the public origin. A URLSession of its own is routed to it with no
//   failover, and does TLS to the instance end to end, with the system's
//   validation of the instance's name. Nothing here reads or ends TLS.
// - The tunnel is measured by a second URLSession of its own, with no
//   route, to the instance's public address.
// - A timed sample counts only if it ran on the connection the warm-up
//   made, through the port, on the path the measurement began on. A
//   reconnection, a request that went round the port, or a change of the
//   path ends that leg: what is left would be another connection's or
//   another path's numbers.
// - The port holds a few connections at most and opens one stream over the
//   link in its whole life, for the first request for its host that reaches
//   it. The port cannot tell who asks: another process on this device that
//   asks first gets that stream, to the instance's ingress, which is where
//   the App's own listener would have taken it, and the measurement's
//   request is then refused and its leg fails. Nothing is opened beyond
//   that one stream, and no handler beyond those few connections.

/// The App's admitted direct or relay link to one instance, for a
/// measurement only (`DirectPathController.measurementLink`).
struct DirectMeasureLink: Sendable {
    let authority: DirectAuthority
    /// The kind of path the link runs on, as it was when this was made.
    let kind: DirectPathKind
    /// For a path to an address: whether that address is on a local
    /// network. Nil through the relay, and when the transport cannot tell.
    let local: Bool?
    /// Through the relay: its region, when the selected path names one of
    /// the known relays.
    let relayRegion: String?
    /// Opens one stream over the link. Throws when the link is gone.
    let open: @Sendable () async throws -> any DirectByteStream
    /// Whether the selection, lease, listener and path this was made under
    /// are all still the same, with no other path found selected in
    /// between (`DirectPathMark`).
    let isCurrent: @MainActor @Sendable () -> Bool
}

enum DirectMeasure {
    /// Timed requests per path.
    static let samples = 5
    static let basis = "warm-https-health"
    static let healthPath = "/health"

    struct Limits: Equatable, Sendable {
        /// One request, the warm-up included.
        var sampleSeconds: TimeInterval = 5
        /// The whole measurement, both paths.
        var totalSeconds: TimeInterval = 30
        /// The largest request head the port reads, and the largest answer
        /// head from the instance.
        var headBytes = 4096
        /// The most bytes one relay holds in memory per direction.
        var chunkBytes = 64 * 1024
        /// The largest `/health` body read.
        var bodyBytes = 8192
        /// The most connections the port holds at once, whatever they have
        /// sent. A measurement needs one.
        var portConnections = 4

        static let standard = Limits()
    }

    enum Failure: String, Error, Equatable, Sendable {
        case failed
        case timeout
    }

    /// One path's samples (`MeasureLeg` in the protocol of 2026-11-25).
    /// Labels and numbers only: no address, port or identifier.
    struct Leg: Equatable, Sendable {
        enum Kind: String, Equatable, Sendable {
            case relay, ipv4, ipv6, tunnel
        }

        enum Scope: String, Equatable, Sendable {
            /// The same network.
            case local
            /// Across networks.
            case global
        }

        let kind: Kind
        /// For `ipv4` and `ipv6`, when known.
        let scope: Scope?
        /// For `relay`, when known.
        let relayRegion: String?
        /// Timed samples that completed.
        let ok: Int
        /// Over the completed samples, in milliseconds.
        let medianMs: Double?
        let maxMs: Double?
        /// The first non-success's kind, when `ok` is below `samples`.
        let error: Failure?
    }

    struct Measurement: Equatable, Sendable {
        /// When it finished.
        let measuredAt: Date
        /// The direct or relay path in use now; nil when there is none.
        let current: Leg?
        /// The public origin through Cloudflare; nil only when it could not
        /// be attempted at all.
        let tunnel: Leg?
    }

    enum Outcome: Equatable, Sendable {
        case measured(Measurement)
        /// Another measurement is running.
        case busy
    }

    // MARK: One at a time

    @MainActor private static var running = false

    /// Runs `measurement` unless one is running already, anywhere in the
    /// App: then `busy`, at once.
    @MainActor
    static func exclusively(
        _ measurement: @MainActor () async -> Measurement
    ) async -> Outcome {
        guard !running else {
            return .busy
        }
        running = true
        defer {
            running = false
        }
        return .measured(await measurement())
    }

    // MARK: Both paths

    /// Measures both paths of the instance at `url` at once, within
    /// `limits.totalSeconds` for the two together. `link`: the App's
    /// admitted link to that instance, if it has one.
    static func measurement(
        ofInstanceAt url: URL,
        link: DirectMeasureLink?,
        limits: Limits = .standard
    ) async -> Measurement {
        guard
            let authority = DirectAuthority(instanceURL: url),
            let health = URL(string: "https://\(authority.host)\(healthPath)")
        else {
            return Measurement(measuredAt: Date(), current: nil, tunnel: nil)
        }
        let clock = ContinuousClock()
        let end = clock.now.advanced(by: .seconds(limits.totalSeconds))
        // A link for another instance than the one asked about is none.
        let link = link.flatMap { $0.authority == authority ? $0 : nil }
        async let current = currentLeg(link, health: health, limits: limits, end: end)
        async let tunnel = tunnelLeg(health: health, limits: limits, end: end)
        let legs = await (current, tunnel)
        return Measurement(measuredAt: Date(), current: legs.0, tunnel: legs.1)
    }

    private static func tunnelLeg(
        health: URL,
        limits: Limits,
        end: ContinuousClock.Instant
    ) async -> Leg {
        let requester = Requester(
            configuration: tunnelConfiguration(limits: limits),
            url: health,
            limits: limits
        )
        defer {
            requester.finish()
        }
        let result = await sample(limits: limits, end: end, unchanged: { _ in true }) { index in
            let observed = try await requester.get()
            // The connection the warm-up made, and no other.
            return Sample(
                elapsed: observed.elapsed,
                healthy: observed.healthy,
                counts: observed.transactions == 1 && (index == 0 || observed.reused)
            )
        }
        return leg(.tunnel, scope: nil, relayRegion: nil, result)
    }

    private static func currentLeg(
        _ link: DirectMeasureLink?,
        health: URL,
        limits: Limits,
        end: ContinuousClock.Instant
    ) async -> Leg? {
        guard let link, await link.isCurrent() else {
            return nil
        }
        let kind: Leg.Kind
        switch link.kind {
        case .relay: kind = .relay
        case .ipv4: kind = .ipv4
        case .ipv6: kind = .ipv6
        }
        let scope: Leg.Scope? = link.kind == .relay ? nil : link.local.map { $0 ? .local : .global }
        let region = link.kind == .relay ? link.relayRegion : nil
        let port = DirectMeasurePort(link: link, limits: limits)
        defer {
            port.stop()
        }
        let started = await bounded(until: end) { try await port.start() }
        guard
            case .success(let bound) = started,
            let endpoint = NWEndpoint.Port(rawValue: bound)
        else {
            // Out of time is `timeout` here too; a port that could not be
            // bound is `failed`.
            var failure = Failure.failed
            if case .failure(let reason) = started {
                failure = reason
            }
            return leg(kind, scope: scope, relayRegion: region, SampleResult(times: [], error: failure))
        }
        let requester = Requester(
            configuration: currentConfiguration(port: endpoint, limits: limits),
            url: health,
            limits: limits
        )
        defer {
            requester.finish()
        }
        let result = await sample(
            limits: limits,
            end: end,
            unchanged: { index in
                // Before a request: the path the measurement began on, and
                // no tunnel through the port but the warm-up's. After it,
                // the same, read again.
                let tunnels = port.tunnels
                guard index == 0 ? tunnels <= 1 : tunnels == 1 else {
                    return false
                }
                return await link.isCurrent()
            }
        ) { index in
            let observed = try await requester.get()
            // Through the port, on the one tunnel the warm-up opened: not
            // round it, and not on a connection made since.
            return Sample(
                elapsed: observed.elapsed,
                healthy: observed.healthy,
                counts: observed.viaProxy
                    && observed.transactions == 1
                    && (index == 0 || observed.reused)
                    && port.tunnels == 1
            )
        }
        return leg(kind, scope: scope, relayRegion: region, result)
    }

    // MARK: Sampling

    /// One request, as far as the sampling rules need it.
    struct Sample: Equatable, Sendable {
        let elapsed: Duration
        /// `200` and a body that says the instance is well.
        let healthy: Bool
        /// It ran on the connection the leg measures: the warm-up made it,
        /// and every timed request used it again.
        let counts: Bool
    }

    struct SampleResult: Equatable, Sendable {
        /// The timed samples that completed.
        var times: [Duration]
        var error: Failure?
    }

    /// The warm-up and the timed requests of one path, one after another.
    ///
    /// - `request(index)` makes the request; index 0 is the warm-up. Each
    ///   has `limits.sampleSeconds`, and none runs past `end` or is begun
    ///   at or after it: a request that takes longer, or whose turn comes
    ///   too late, ends the leg as `timeout`.
    /// - `unchanged(index)` is asked before and after every request. `false`
    ///   ends the leg as `failed`, with what was timed before it.
    /// - A request that throws, or that did not run on the leg's connection,
    ///   ends the leg as `failed`.
    /// - A timed request that was answered, but not with a healthy answer,
    ///   is the leg's error and is not timed; the rest go on.
    static func sample(
        limits: Limits,
        end: ContinuousClock.Instant,
        unchanged: @escaping @Sendable (Int) async -> Bool,
        request: @escaping @Sendable (Int) async throws -> Sample
    ) async -> SampleResult {
        let clock = ContinuousClock()
        var result = SampleResult(times: [], error: nil)
        for index in 0...samples {
            guard await unchanged(index) else {
                result.error = result.error ?? .failed
                return result
            }
            let bound = min(end, clock.now.advanced(by: .seconds(limits.sampleSeconds)))
            switch await bounded(until: bound, { try await request(index) }) {
            case .failure(let failure):
                result.error = result.error ?? failure
                return result
            case .success(let sample):
                guard sample.counts, await unchanged(index) else {
                    result.error = result.error ?? .failed
                    return result
                }
                if index == 0 {
                    // Answered in full; only timed requests count.
                    continue
                }
                if sample.healthy {
                    result.times.append(sample.elapsed)
                } else {
                    result.error = result.error ?? .failed
                }
            }
        }
        return result
    }

    static func leg(
        _ kind: Leg.Kind,
        scope: Leg.Scope?,
        relayRegion: String?,
        _ result: SampleResult
    ) -> Leg {
        let times = result.times.sorted()
        let milliseconds = { (duration: Duration) -> Double in
            Double(duration.components.seconds) * 1_000
                + Double(duration.components.attoseconds) / 1e15
        }
        let median = times.isEmpty
            ? nil
            : (milliseconds(times[(times.count - 1) / 2]) + milliseconds(times[times.count / 2])) / 2
        return Leg(
            kind: kind,
            scope: scope,
            relayRegion: relayRegion,
            ok: times.count,
            medianMs: median,
            maxMs: times.last.map(milliseconds),
            // Fewer than all of them, and nothing said why: it failed.
            error: result.error ?? (times.count < samples ? .failed : nil)
        )
    }

    /// `work`'s result if it ended before `deadline`, else `timeout`.
    ///
    /// - The deadline is a point on the continuous clock, and the clock is
    ///   what decides: `work` is not begun at or after it, however late
    ///   its task is first run, and a result that arrives at or after it
    ///   is `timeout`, whichever of the work and the timer the scheduler
    ///   ran first.
    /// - The wait ends at the deadline even if `work` does not end when it
    ///   is cancelled: it is cancelled then and left to finish by itself.
    /// - `work` reporting that its own request timed out is `timeout` too;
    ///   any other error is `failed`.
    static func bounded<Value: Sendable>(
        until deadline: ContinuousClock.Instant,
        _ work: @escaping @Sendable () async throws -> Value
    ) async -> Result<Value, Failure> {
        let clock = ContinuousClock()
        guard clock.now < deadline else {
            return .failure(.timeout)
        }
        let settled = Settled<Result<Value, Failure>>()
        let working = Task {
            // This task may first run long after it was made. Asked here,
            // immediately before the work: nothing is begun for a caller
            // that has gone, or at or after the deadline.
            guard !Task.isCancelled else {
                settled.resume(.failure(.failed))
                return
            }
            guard clock.now < deadline else {
                settled.resume(.failure(.timeout))
                return
            }
            let result: Result<Value, Failure>
            do {
                result = .success(try await work())
            } catch let error as URLError where error.code == .timedOut {
                result = .failure(.timeout)
            } catch {
                result = .failure(.failed)
            }
            settled.resume(clock.now < deadline ? result : .failure(.timeout))
        }
        let timing = Task {
            try? await Task.sleep(until: deadline, clock: .continuous)
            // (A sleep that was cancelled is the work having ended first.)
            settled.resume(.failure(.timeout))
        }
        let result = await withTaskCancellationHandler {
            await settled.value()
        } onCancel: {
            settled.resume(.failure(.failed))
        }
        working.cancel()
        timing.cancel()
        return result
    }

    /// A result given once and awaited once.
    private final class Settled<Value: Sendable>: @unchecked Sendable {
        private let lock = NSLock()
        private var result: Value?
        private var waiter: CheckedContinuation<Value, Never>?

        func resume(_ value: Value) {
            let waiter: CheckedContinuation<Value, Never>? = lock.withLock {
                guard result == nil else {
                    return nil
                }
                result = value
                defer {
                    self.waiter = nil
                }
                return self.waiter
            }
            waiter?.resume(returning: value)
        }

        func value() async -> Value {
            await withCheckedContinuation { continuation in
                let ready: Value? = lock.withLock {
                    if result == nil {
                        waiter = continuation
                    }
                    return result
                }
                if let ready {
                    continuation.resume(returning: ready)
                }
            }
        }
    }

    // MARK: The two sessions

    /// What both sessions share in kind, and nothing in fact: each is an
    /// ephemeral session of its own, with its own connections, no cache, no
    /// cookies and no credentials, and one connection to the instance.
    private static func ephemeral(limits: Limits) -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpMaximumConnectionsPerHost = 1
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.urlCredentialStorage = nil
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = limits.sampleSeconds
        configuration.timeoutIntervalForResource = limits.totalSeconds
        return configuration
    }

    /// The session that measures the path in use: every connection it makes
    /// goes to the measurement's own port, or fails.
    static func currentConfiguration(port: NWEndpoint.Port, limits: Limits) -> URLSessionConfiguration {
        let configuration = ephemeral(limits: limits)
        var route = ProxyConfiguration(
            httpCONNECTProxy: .hostPort(host: "127.0.0.1", port: port)
        )
        route.allowFailover = false
        configuration.proxyConfigurations = [route]
        return configuration
    }

    /// The session that measures the tunnel: no route of the App's.
    static func tunnelConfiguration(limits: Limits) -> URLSessionConfiguration {
        ephemeral(limits: limits)
    }

    /// One session's requests, one at a time, with what the system says
    /// about the connection each ran on.
    final class Requester: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
        struct Observed: Sendable {
            let elapsed: Duration
            let healthy: Bool
            /// The request went through a proxy: for the measured path, the
            /// measurement's port.
            let viaProxy: Bool
            /// It used a connection that was already there.
            let reused: Bool
            /// How many attempts the system made for it: more than one is a
            /// request sent again on another connection.
            let transactions: Int
        }

        private let url: URL
        private let limits: Limits
        private let lock = NSLock()
        private var metrics: [Int: URLSessionTaskMetrics] = [:]
        private var session: URLSession?

        init(configuration: URLSessionConfiguration, url: URL, limits: Limits) {
            self.url = url
            self.limits = limits
            super.init()
            session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        }

        /// Ends the session and everything it has open.
        func finish() {
            let session: URLSession? = lock.withLock {
                defer {
                    self.session = nil
                }
                return self.session
            }
            session?.invalidateAndCancel()
        }

        func get() async throws -> Observed {
            guard let session = lock.withLock({ self.session }) else {
                throw CancellationError()
            }
            var request = URLRequest(url: url)
            request.httpMethod = "GET"
            request.setValue("no-cache, no-store", forHTTPHeaderField: "Cache-Control")
            let clock = ContinuousClock()
            let started = clock.now
            let (bytes, response) = try await session.bytes(for: request)
            let identifier = bytes.task.taskIdentifier
            var body = Data()
            var whole = true
            for try await byte in bytes {
                guard body.count < limits.bodyBytes else {
                    whole = false
                    bytes.task.cancel()
                    break
                }
                body.append(byte)
            }
            let elapsed = started.duration(to: clock.now)
            let collected = await collectedMetrics(for: identifier)
            let last = collected?.transactionMetrics.last
            let status = (response as? HTTPURLResponse)?.statusCode
            let healthy = whole
                && status == 200
                && ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["status"] as? String == "ok"
            return Observed(
                elapsed: elapsed,
                healthy: healthy,
                viaProxy: last?.isProxyConnection ?? false,
                reused: last?.isReusedConnection ?? false,
                // Without the system's account of it, it does not count.
                transactions: collected?.transactionMetrics.count ?? 0
            )
        }

        /// The system reports a task's metrics as the task ends, which is
        /// about when its last byte is read: waited for briefly.
        private func collectedMetrics(for identifier: Int) async -> URLSessionTaskMetrics? {
            for _ in 0..<50 {
                if let found = lock.withLock({ metrics.removeValue(forKey: identifier) }) {
                    return found
                }
                try? await Task.sleep(for: .milliseconds(10))
            }
            return nil
        }

        func urlSession(
            _ session: URLSession,
            task: URLSessionTask,
            didFinishCollecting metrics: URLSessionTaskMetrics
        ) {
            lock.withLock {
                self.metrics[task.taskIdentifier] = metrics
            }
        }

        /// The instance's `/health` answers by itself. A redirect is not
        /// followed: where it leads is not what is being measured.
        func urlSession(
            _ session: URLSession,
            task: URLSessionTask,
            willPerformHTTPRedirection response: HTTPURLResponse,
            newRequest request: URLRequest,
            completionHandler: @escaping (URLRequest?) -> Void
        ) {
            completionHandler(nil)
        }
    }
}

/// The measurement's own loopback port (see the top of this file). It
/// serves `CONNECT <the link's host>:443` and nothing else, over the link
/// and nothing else, for as long as one measurement runs.
final class DirectMeasurePort: @unchecked Sendable {
    private let link: DirectMeasureLink
    private let limits: DirectMeasure.Limits
    private let queue = DispatchQueue(label: "bot.avibe.direct.measure")
    private let lock = NSLock()
    private var listener: NWListener?
    private var serving: [UUID: Task<Void, Never>] = [:]
    private var stopped = false
    private var tunnelCount = 0

    init(link: DirectMeasureLink, limits: DirectMeasure.Limits = .standard) {
        self.link = link
        self.limits = limits
    }

    /// How many requests for the link's host it has had so far. A
    /// measurement's requests all run in one tunnel, and only the first
    /// request the port has is given a stream, whoever sent it. A second is
    /// refused: it is the session's connection made anew, or the session's
    /// own after someone else's came first, and either ends the leg.
    var tunnels: Int {
        lock.withLock { tunnelCount }
    }

    /// Binds a port of the system's choosing on the loopback address and
    /// serves it. Returns the port.
    func start() async throws -> UInt16 {
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = false
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        let listener = try NWListener(using: parameters, on: .any)
        let kept = lock.withLock { () -> Bool in
            guard !stopped else {
                return false
            }
            self.listener = listener
            return true
        }
        guard kept else {
            throw CancellationError()
        }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else {
                connection.cancel()
                return
            }
            self.accept(connection)
        }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<UInt16, Error>) in
                let resumed = ResumedOnce()
                listener.stateUpdateHandler = { state in
                    switch state {
                    case .ready:
                        guard let port = listener.port?.rawValue, port != 0 else {
                            if resumed.claim() {
                                continuation.resume(throwing: CancellationError())
                            }
                            return
                        }
                        if resumed.claim() {
                            continuation.resume(returning: port)
                        }
                    case .failed(let error):
                        if resumed.claim() {
                            continuation.resume(throwing: error)
                        }
                    case .cancelled:
                        if resumed.claim() {
                            continuation.resume(throwing: CancellationError())
                        }
                    default:
                        break
                    }
                }
                listener.start(queue: queue)
            }
        } onCancel: {
            listener.cancel()
        }
    }

    /// Closes the port and everything it carries.
    func stop() {
        let (listener, tasks) = lock.withLock { () -> (NWListener?, [Task<Void, Never>]) in
            stopped = true
            defer {
                self.listener = nil
                self.serving = [:]
            }
            return (self.listener, Array(self.serving.values))
        }
        listener?.cancel()
        for task in tasks {
            task.cancel()
        }
    }

    /// Takes `connection` if the port has room for it, and closes it at
    /// once if not: a connection beyond `limits.portConnections` is never
    /// started, read or given a task. Called in the listener's handler.
    private func accept(_ connection: NWConnection) {
        let id = UUID()
        let accepted = lock.withLock { () -> Bool in
            guard !stopped, serving.count < limits.portConnections else {
                return false
            }
            // Started inside the handler, as the App's listener does.
            connection.start(queue: queue)
            // Kept before it can end, so its end always finds it.
            serving[id] = Task { [weak self] in
                await self?.serve(connection)
                connection.cancel()
                self?.ended(id)
            }
            return true
        }
        if !accepted {
            connection.cancel()
        }
    }

    private func ended(_ id: UUID) {
        lock.withLock {
            serving[id] = nil
        }
    }

    /// Counts a request for the link's host. `true` for the first one the
    /// port has had, which alone may open a stream over the link.
    private func noteTunnel() -> Bool {
        lock.withLock {
            tunnelCount += 1
            return tunnelCount == 1
        }
    }

    private func serve(_ connection: NWConnection) async {
        let clock = ContinuousClock()
        let patience = clock.now.advanced(by: .seconds(limits.sampleSeconds))
        // The request head, whole or not at all.
        var received = Data()
        var early = Data()
        reading: while true {
            switch DirectConnectRequest.parse(
                received,
                allowedHosts: [link.authority.host],
                limit: limits.headBytes
            ) {
            case .incomplete:
                let chunk = await DirectMeasure.bounded(until: patience) {
                    try await connection.receiveChunk(maximumLength: 4096)
                }
                // Gone, or too slow, before its head was whole.
                guard let chunk = chunk.value, !chunk.data.isEmpty else {
                    return
                }
                received.append(chunk.data)
            case .accepted(_, let earlyData):
                early = earlyData
                break reading
            case .ownershipCheck:
                // This is not the App's listener, and says so to no one.
                try? await connection.sendAll(DirectConnectWire.refusal(403))
                return
            case .refused(let status):
                try? await connection.sendAll(DirectConnectWire.refusal(status))
                return
            }
        }
        // One stream over the link for the whole measurement. A second
        // request is counted, which ends the leg, and opens nothing.
        guard noteTunnel() else {
            try? await connection.sendAll(DirectConnectWire.refusal(503))
            return
        }
        // Only over the link the measurement was started with, as it was.
        guard await link.isCurrent() else {
            try? await connection.sendAll(DirectConnectWire.refusal(503))
            return
        }
        let link = link
        // A stream that comes after its wait was given up is given up too,
        // whenever it comes: the one stream is never left open unused.
        let late = LateStream()
        let opening = await DirectMeasure.bounded(until: patience) { () -> any DirectByteStream in
            let stream = try await link.open()
            late.arrived(stream)
            return stream
        }
        guard let stream = opening.value else {
            late.abandon()
            try? await connection.sendAll(DirectConnectWire.refusal(502))
            return
        }
        defer {
            stream.cancel()
        }
        // The instance's ingress is asked as the App's listener asks it.
        let limit = limits.headBytes
        let authority = link.authority
        let answer = await DirectMeasure.bounded(until: patience) { () -> Data? in
            try await stream.write(DirectConnectWire.canonicalRequest(for: authority))
            var head = Data()
            while true {
                switch DirectConnectAnswer.parse(head, limit: limit) {
                case .incomplete:
                    guard let more = try await stream.read(maxLength: 4096), !more.isEmpty else {
                        return nil
                    }
                    head.append(more)
                case .established(let leftover):
                    return leftover
                case .refused, .malformed:
                    return nil
                }
            }
        }
        guard case .success(let leftover?) = answer else {
            try? await connection.sendAll(DirectConnectWire.refusal(502))
            return
        }
        // The link may have stopped being the one this began under while
        // the instance was answering. Nothing has been said to the client
        // and none of its bytes sent on: asked again before either.
        guard await link.isCurrent() else {
            stream.cancel()
            try? await connection.sendAll(DirectConnectWire.refusal(503))
            return
        }
        do {
            try await connection.sendAll(DirectConnectWire.established)
            if !leftover.isEmpty {
                try await connection.sendAll(leftover)
            }
            if !early.isEmpty {
                try await stream.write(early)
            }
        } catch {
            return
        }
        // Opaque bytes both ways until either side ends or the port stops.
        let chunk = limits.chunkBytes
        await withTaskGroup(of: Void.self) { group in
            group.addTask {
                do {
                    while true {
                        let next = try await connection.receiveChunk(maximumLength: chunk)
                        if !next.data.isEmpty {
                            try await stream.write(next.data)
                        }
                        if next.isComplete {
                            try await stream.closeWrite()
                            return
                        }
                    }
                } catch {
                    stream.cancel()
                    connection.cancel()
                }
            }
            group.addTask {
                do {
                    while let data = try await stream.read(maxLength: chunk) {
                        if !data.isEmpty {
                            try await connection.sendAll(data)
                        }
                    }
                    try await connection.finishSending()
                } catch {
                    stream.cancel()
                    connection.cancel()
                }
            }
            await withTaskCancellationHandler {
                await group.waitForAll()
            } onCancel: {
                stream.cancel()
                connection.cancel()
            }
        }
    }

    private final class LateStream: @unchecked Sendable {
        private let lock = NSLock()
        private var stream: (any DirectByteStream)?
        private var abandoned = false

        func arrived(_ stream: any DirectByteStream) {
            let unwanted = lock.withLock { () -> Bool in
                guard !abandoned else {
                    return true
                }
                self.stream = stream
                return false
            }
            if unwanted {
                stream.cancel()
            }
        }

        func abandon() {
            let held: (any DirectByteStream)? = lock.withLock {
                abandoned = true
                defer {
                    self.stream = nil
                }
                return self.stream
            }
            held?.cancel()
        }
    }

    private final class ResumedOnce: @unchecked Sendable {
        private let lock = NSLock()
        private var resumed = false

        func claim() -> Bool {
            lock.withLock {
                guard !resumed else {
                    return false
                }
                resumed = true
                return true
            }
        }
    }
}

private extension Result {
    var value: Success? {
        try? get()
    }
}

extension DirectPathController {
    /// 「线路测速」 for the instance at `url`: `measurePaths` of the settled
    /// contract, under this file's own result type. One at a time in the
    /// whole App; bounded at `DirectMeasure.Limits.standard.totalSeconds`.
    func measure(forInstanceURL url: URL) async -> DirectMeasure.Outcome {
        await DirectMeasure.exclusively {
            let link = measurementLink(forInstanceURL: url)
            return await DirectMeasure.measurement(ofInstanceAt: url, link: link)
        }
    }
}
