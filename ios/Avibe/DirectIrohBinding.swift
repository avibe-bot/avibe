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

    func bind(secretKey: Data) async throws -> any DirectIrohEndpoint {
        let options = IrohLib.EndpointOptions(
            preset: IrohLib.presetMinimal(),
            secretKey: secretKey,
            relayMode: IrohLib.RelayMode.disabled()
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
            alpn: Data
        ) async throws -> any DirectIrohConnection {
            let address = IrohLib.EndpointAddr(
                id: try IrohLib.EndpointId.fromBytes(bytes: endpointID.bytes),
                relayUrl: nil,
                addresses: addresses
            )
            return Live(connection: try await endpoint.connect(addr: address, alpn: alpn))
        }

        func close() async {
            try? await endpoint.close()
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
