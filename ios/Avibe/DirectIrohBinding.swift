import Foundation
import IrohLib

// The generated wrapper is compiled in its own Swift module by the IrohLib
// target. Keep every generated name qualified here so it cannot collide with
// App types, and keep this adapter out of the notification extension.
struct DirectIrohBinding: DirectIrohEngine {
    func makeLaunchKey() throws -> DirectIrohLaunchKey {
        let key = IrohLib.SecretKey.generate()
        guard let endpointID = DirectEndpointID(bytes: key.public().toBytes()) else {
            throw DirectIrohError.noLaunchKey
        }
        return DirectIrohLaunchKey(secret: key.toBytes(), endpointID: endpointID)
    }

    func bind(secretKey: Data, relayURLs: [String]) async throws -> any DirectIrohEndpoint {
        // The minimal preset adds no address lookup: no n0 DNS or pkarr
        // lookup and no publishing. Only the given relays, or none.
        let options = IrohLib.EndpointOptions(
            preset: IrohLib.presetMinimal(),
            secretKey: secretKey,
            relayMode: relayURLs.isEmpty
                ? IrohLib.RelayMode.disabled()
                : IrohLib.RelayMode.custom(map: try IrohLib.RelayMap.fromUrls(urls: relayURLs))
        )
        return Bound(endpoint: try await IrohLib.Endpoint.bind(options: options))
    }

    private final class Bound: DirectIrohEndpoint, @unchecked Sendable {
        private let endpoint: IrohLib.Endpoint

        init(endpoint: IrohLib.Endpoint) {
            self.endpoint = endpoint
        }

        func connect(
            to endpointID: DirectEndpointID,
            addresses: [String],
            relayURL: String?,
            alpn: Data
        ) async throws -> any DirectIrohConnection {
            let address = IrohLib.EndpointAddr(
                id: try IrohLib.EndpointId.fromBytes(bytes: endpointID.bytes),
                relayUrl: relayURL,
                addresses: addresses
            )
            return Live(connection: try await endpoint.connect(addr: address, alpn: alpn))
        }

        func close() async {
            try? await endpoint.close()
        }

        var localFamilies: DirectAddressFamilies? {
            DirectAddressFamilies(socketAddresses: endpoint.addr().directAddresses())
        }
    }

    private final class Live: DirectIrohConnection, @unchecked Sendable {
        private let connection: IrohLib.Connection

        init(connection: IrohLib.Connection) {
            self.connection = connection
        }

        var remoteEndpointID: DirectEndpointID? {
            DirectEndpointID(bytes: connection.remoteId().toBytes())
        }

        func openStream() async throws -> DirectIrohStreamHalves {
            let stream = try await connection.openBi()
            return DirectIrohStreamHalves(
                send: Send(stream: stream.send()),
                receive: Receive(stream: stream.recv())
            )
        }

        func closed() async {
            _ = await connection.closed()
        }

        func close() {
            try? connection.close(errorCode: 0, reason: Data())
        }

        var selectedPath: DirectPathKind? {
            Self.kind(of: connection.paths())
        }

        var selectedPathDetail: DirectSelectedPath? {
            guard let path = connection.paths().first(where: \.isSelected) else {
                return nil
            }
            return DirectSelectedPath(
                isRelay: path.isRelay,
                isIp: path.isIp,
                remoteAddress: path.remoteAddr,
                rttMs: path.rttMs
            )
        }

        var selectedPathRead: DirectSelectedPathRead? {
            guard let path = connection.paths().first(where: \.isSelected) else {
                return nil
            }
            return DirectSelectedPathRead(
                // For a relay path the far address is the relay's URL, so
                // another relay is another path, as another address is.
                identity: "\(path.isRelay ? "relay" : "address") \(path.remoteAddr)",
                labels: DirectSelectedPath(
                    isRelay: path.isRelay,
                    isIp: path.isIp,
                    remoteAddress: path.remoteAddr,
                    rttMs: path.rttMs
                )
            )
        }

        func watchSelectedPath(
            _ onChange: @escaping @Sendable (DirectPathKind?) -> Void
        ) -> (any DirectPathWatch)? {
            Watch(handle: connection.watchPaths(callback: PathCallback(handler: onChange)))
        }

        static func kind(of paths: [IrohLib.PathSnapshot]) -> DirectPathKind? {
            guard let path = paths.first(where: \.isSelected) else {
                return nil
            }
            if path.isRelay {
                return .relay
            }
            return path.isIp ? DirectPathKind.family(ofSocketAddress: path.remoteAddr) : nil
        }
    }

    private final class PathCallback: IrohLib.PathChangeCallback, @unchecked Sendable {
        private let handler: @Sendable (DirectPathKind?) -> Void

        init(handler: @escaping @Sendable (DirectPathKind?) -> Void) {
            self.handler = handler
        }

        func onChange(paths: [IrohLib.PathSnapshot]) async throws {
            handler(Live.kind(of: paths))
        }
    }

    private final class Watch: DirectPathWatch, @unchecked Sendable {
        private let handle: IrohLib.WatchHandle

        init(handle: IrohLib.WatchHandle) {
            self.handle = handle
        }

        func stop() {
            let handle = handle
            Task {
                await handle.stop()
            }
        }
    }

    private final class Send: DirectIrohSendHalf, @unchecked Sendable {
        private let stream: IrohLib.SendStream

        init(stream: IrohLib.SendStream) {
            self.stream = stream
        }

        func writeAll(_ data: Data) async throws {
            try await stream.writeAll(buf: data)
        }

        func finish() async throws {
            try await stream.finish()
        }

        func reset() async {
            try? await stream.reset(errorCode: 0)
        }
    }

    private final class Receive: DirectIrohReceiveHalf, @unchecked Sendable {
        private let stream: IrohLib.RecvStream

        init(stream: IrohLib.RecvStream) {
            self.stream = stream
        }

        func read(sizeLimit: Int) async throws -> Data {
            try await stream.read(sizeLimit: UInt32(clamping: max(sizeLimit, 1)))
        }

        func stop() async {
            try? await stream.stop(errorCode: 0)
        }
    }
}
