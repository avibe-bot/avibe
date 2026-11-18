import Foundation

protocol HealthDeviceRequesting {
    func request(origin: String, credential: String?, method: String,
                 path: String, body: [String: Any]?) async throws -> [String: Any]
}

/// Dedicated session: cookies, URL cache, redirects and browser auth are disabled.
final class HealthDeviceClient: NSObject, HealthDeviceRequesting, URLSessionTaskDelegate {
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false
        config.httpCookieStorage = nil
        config.urlCache = nil
        config.timeoutIntervalForRequest = 35
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    static func url(origin: String, path: String) throws -> URL {
        guard let base = URL(string: origin), let host = base.host, !host.isEmpty,
              base.path.isEmpty || base.path == "/", base.user == nil, base.password == nil,
              base.query == nil, base.fragment == nil,
              base.scheme == "https" || (base.scheme == "http" && ["127.0.0.1", "localhost", "::1"].contains(base.host ?? "")),
              path.hasPrefix("/api/mobile/device/v1/"),
              let decoded = path.removingPercentEncoding, decoded == path,
              !path.contains("?"), !path.contains("#"), !path.contains("\\"),
              !path.split(separator: "/").contains(where: { $0 == ".." || $0 == "." }) else {
            throw HealthSyncError.code("invalid_target")
        }
        return base.appendingPathComponent(String(path.dropFirst()))
    }

    func request(origin: String, credential: String?, method: String,
                 path: String, body: [String: Any]?) async throws -> [String: Any] {
        var request = URLRequest(url: try Self.url(origin: origin, path: path))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let credential { request.setValue("Bearer " + credential, forHTTPHeaderField: "Authorization") }
        if let body {
            let data = try JSONSerialization.data(withJSONObject: body)
            guard data.count <= 256 * 1024 else { throw HealthSyncError.code("invalid_payload") }
            request.httpBody = data
        }
        let (data, response) = try await session.data(for: request)
        return try Self.response(data: data, response: response)
    }

    static func response(data: Data, response: URLResponse?) throws -> [String: Any] {
        guard let response = response as? HTTPURLResponse else { throw HealthSyncError.code("internal") }
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard (200..<300).contains(response.statusCode) else {
            let code = json["code"] as? String
                ?? (json["error"] as? [String: Any])?["code"] as? String ?? "internal"
            let retry = Self.retryDelay(response.value(forHTTPHeaderField: "Retry-After"))
            throw HealthSyncError.http(response.statusCode, code, retry)
        }
        guard !data.isEmpty, (try? JSONSerialization.jsonObject(with: data)) is [String: Any] else {
            throw HealthSyncError.code("invalid_response")
        }
        return json
    }

    static func retryDelay(_ value: String?, now: Date = Date()) -> TimeInterval? {
        guard let value else { return nil }
        if let seconds = TimeInterval(value), seconds.isFinite { return max(0, seconds) }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        return formatter.date(from: value).map { max(0, $0.timeIntervalSince(now)) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

/// Files remain in the durable outbox until the instance acknowledges them.
/// Delegates also run after iOS relaunches the app for a background transfer.
protocol HealthUploading: AnyObject {
    var onComplete: (@MainActor (String, Result<[String: Any], Error>) async -> Void)? { get set }
    func reconnect()
    func upload(id: String, file: URL, identity: HealthDeviceIdentity) async throws
    func cancelAll() async
}

final class HealthBackgroundUploader: NSObject, HealthUploading, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let identifier = "bot.avibe.ios.health.upload"
    var onComplete: (@MainActor (String, Result<[String: Any], Error>) async -> Void)?
    var systemCompletion: (() -> Void)?
    private var received: [Int: Data] = [:]
    private var processingTask: Task<Void, Never>?
    private let generationLock = NSLock()
    private var generation = UUID()
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.identifier)
        config.httpShouldSetCookies = false
        config.httpCookieStorage = nil
        config.urlCache = nil
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()

    func reconnect() { _ = session }

    func upload(id: String, file: URL, identity: HealthDeviceIdentity) async throws {
        let token = generationLock.withLock { generation }
        let tasks = await session.allTasks
        guard !tasks.contains(where: { $0.taskDescription == id }) else { return }
        var request = URLRequest(url: try HealthDeviceClient.url(origin: identity.origin,
                                                                 path: "/api/mobile/device/v1/health/days"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer " + identity.credential, forHTTPHeaderField: "Authorization")
        try generationLock.withLock {
            guard token == generation else { throw HealthSyncError.code("connection_changed") }
            let task = session.uploadTask(with: request, fromFile: file)
            task.taskDescription = id
            task.resume()
        }
    }

    func cancelAll() async {
        generationLock.withLock { generation = UUID() }
        for task in await session.allTasks { task.cancel() }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        // Response envelopes contain status only. Bound retained data as well.
        if (received[dataTask.taskIdentifier]?.count ?? 0) + data.count <= 256 * 1024 {
            received[dataTask.taskIdentifier, default: Data()].append(data)
        } else { dataTask.cancel() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let data = received.removeValue(forKey: task.taskIdentifier) ?? Data()
        guard let id = task.taskDescription else { return }
        let result = Result { () throws -> [String: Any] in
            if let error { throw error }
            return try HealthDeviceClient.response(data: data, response: task.response)
        }
        let previous = processingTask
        processingTask = Task { @MainActor in
            await previous?.value
            await self.onComplete?(id, result)
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        let processing = processingTask
        Task { @MainActor in
            // Persist upload ACKs before telling iOS it may suspend this process.
            await processing?.value
            let completion = self.systemCompletion
            self.systemCompletion = nil
            completion?()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
