import AVFAudio
import Foundation
import UIKit
import WebKit

struct NativeBridgePolicy {
    let instanceOrigin: WebOrigin

    func allows(isMainFrame: Bool, origin: WebOrigin?) -> Bool {
        isMainFrame && origin == instanceOrigin
    }
}

struct NativeShellMetadata: Equatable {
    static let contract = 1
    static let platform = "ios"
    static let capabilities = [
        "app": 1,
        "haptics": 1,
        "voice": 1,
    ]

    let shellVersion: String
    let userAgentVersion: String

    init(
        shellVersion: String,
        userAgentVersion: String
    ) {
        self.shellVersion = shellVersion
        self.userAgentVersion = userAgentVersion
    }

    init(bundle: Bundle = .main) {
        let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? "0"
        let build = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String
            ?? "0"
        self.init(
            shellVersion: "\(version) (\(build))",
            userAgentVersion: version
        )
    }

    var userAgentSuffix: String {
        "AvibeiOS/\(userAgentVersion)"
    }

    var jsonObject: [String: Any] {
        [
            "contract": Self.contract,
            "platform": Self.platform,
            "shellVersion": shellVersion,
            "capabilities": Self.capabilities,
        ]
    }
}

enum NativeBridgeMethod: String, CaseIterable {
    case appInfo = "app.info"
    case appOpenExternal = "app.openExternal"
    case hapticsImpact = "haptics.impact"
    case hapticsNotify = "haptics.notify"
    case hapticsSelection = "haptics.selection"
    case voicePermission = "voice.permission"
}

struct NativeBridgeRequest {
    let method: NativeBridgeMethod
    let params: [String: Any]

    static func parse(_ body: Any) -> Result<NativeBridgeRequest, NativeBridgeError> {
        guard
            let object = body as? [String: Any],
            let methodName = object["method"] as? String,
            !methodName.isEmpty
        else {
            return .failure(.invalidRequest)
        }
        guard let method = NativeBridgeMethod(rawValue: methodName) else {
            return .failure(.unsupported)
        }

        let rawParams = object["params"]
        guard
            rawParams == nil
                || rawParams is NSNull
                || rawParams is [String: Any]
        else {
            return .failure(.invalidRequest)
        }
        return .success(
            NativeBridgeRequest(
                method: method,
                params: rawParams as? [String: Any] ?? [:]
            )
        )
    }
}

enum NativeExternalURL {
    static func parse(_ rawValue: String) -> URL? {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            !value.isEmpty,
            let components = URLComponents(string: value),
            let scheme = components.scheme?.lowercased()
        else {
            return nil
        }

        switch scheme {
        case "http", "https":
            guard components.host?.isEmpty == false else {
                return nil
            }
        case "mailto", "tel":
            guard !components.path.isEmpty else {
                return nil
            }
        default:
            return nil
        }
        return components.url
    }
}

enum NativeBridgeError: Error, Equatable {
    case forbidden
    case invalidArgument(String)
    case invalidRequest
    case unsupported

    var code: String {
        switch self {
        case .forbidden:
            return "forbidden"
        case .invalidArgument:
            return "invalid_argument"
        case .invalidRequest:
            return "invalid_request"
        case .unsupported:
            return "unsupported"
        }
    }

    var message: String {
        switch self {
        case .forbidden:
            return "The native bridge is available only to the configured instance main frame."
        case .invalidArgument(let message):
            return message
        case .invalidRequest:
            return "The native bridge request is malformed."
        case .unsupported:
            return "The native bridge method is not supported by this shell."
        }
    }

    var jsonObject: [String: Any] {
        [
            "code": code,
            "message": message,
        ]
    }
}

enum NativeBridgeConfiguration {
    static let messageHandlerName = "avibeNative"

    static func make(
        instanceOrigin: WebOrigin,
        metadata: NativeShellMetadata = NativeShellMetadata()
    ) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        let controller = WKUserContentController()
        let handler = NativeBridgeHandler(
            policy: NativeBridgePolicy(instanceOrigin: instanceOrigin),
            metadata: metadata
        )
        controller.addScriptMessageHandler(
            handler,
            contentWorld: .page,
            name: messageHandlerName
        )
        controller.addUserScript(
            WKUserScript(
                source: scriptSource(
                    instanceOrigin: instanceOrigin,
                    metadata: metadata
                ),
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true,
                in: .page
            )
        )
        configuration.userContentController = controller
        configuration.applicationNameForUserAgent = metadata.userAgentSuffix
        return configuration
    }

    static func scriptSource(
        instanceOrigin: WebOrigin,
        metadata: NativeShellMetadata
    ) -> String {
        let payload: [String: Any] = [
            "instanceOrigin": instanceOrigin.serialized,
            "metadata": metadata.jsonObject,
            "messageHandler": messageHandlerName,
        ]
        let data = try! JSONSerialization.data(
            withJSONObject: payload,
            options: [.sortedKeys]
        )
        let json = String(decoding: data, as: UTF8.self)

        return """
        (() => {
          "use strict";
          const config = \(json);
          if (window !== window.top || window.location.origin !== config.instanceOrigin) return;
          if (Object.prototype.hasOwnProperty.call(window, "avibeNative")) return;

          const call = (method, params = {}) => {
            const handler = window.webkit?.messageHandlers?.[config.messageHandler];
            if (!handler || typeof handler.postMessage !== "function") {
              return Promise.reject({
                code: "unavailable",
                message: "The native bridge transport is unavailable."
              });
            }
            return Promise.resolve(handler.postMessage({ method, params })).then((reply) => {
              if (reply?.ok === true) return reply.result;
              return Promise.reject(reply?.error ?? {
                code: "failed",
                message: "The native bridge call failed."
              });
            });
          };

          const on = (event, handler) => {
            if (typeof event !== "string" || typeof handler !== "function") return () => {};
            const type = `avibe:native:${event}`;
            const listener = (nativeEvent) => handler(nativeEvent.detail);
            window.addEventListener(type, listener);
            return () => window.removeEventListener(type, listener);
          };

          const metadata = config.metadata;
          Object.freeze(metadata.capabilities);
          Object.defineProperty(window, "avibeNative", {
            configurable: false,
            enumerable: false,
            writable: false,
            value: Object.freeze({
              contract: metadata.contract,
              platform: metadata.platform,
              shellVersion: metadata.shellVersion,
              capabilities: metadata.capabilities,
              call,
              on
            })
          });
        })();
        """
    }
}

private final class NativeBridgeHandler: NSObject, WKScriptMessageHandlerWithReply {
    private let policy: NativeBridgePolicy
    private let metadata: NativeShellMetadata

    init(policy: NativeBridgePolicy, metadata: NativeShellMetadata) {
        self.policy = policy
        self.metadata = metadata
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        let origin = WebOrigin(
            scheme: message.frameInfo.securityOrigin.protocol,
            host: message.frameInfo.securityOrigin.host,
            port: message.frameInfo.securityOrigin.port == 0
                ? nil
                : message.frameInfo.securityOrigin.port
        )
        guard policy.allows(isMainFrame: message.frameInfo.isMainFrame, origin: origin) else {
            reply(replyHandler, error: .forbidden)
            return
        }

        switch NativeBridgeRequest.parse(message.body) {
        case .failure(let error):
            reply(replyHandler, error: error)
        case .success(let request):
            handle(request, replyHandler: replyHandler)
        }
    }

    private func handle(
        _ request: NativeBridgeRequest,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        switch request.method {
        case .appInfo:
            reply(replyHandler, result: metadata.jsonObject)
        case .appOpenExternal:
            guard
                let rawURL = request.params["url"] as? String,
                let url = NativeExternalURL.parse(rawURL)
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "app.openExternal requires an absolute http, https, mailto, or tel URL."
                    )
                )
                return
            }
            UIApplication.shared.open(url, options: [:]) { opened in
                self.reply(replyHandler, result: ["opened": opened])
            }
        case .hapticsImpact:
            guard
                let rawStyle = request.params["style"] as? String,
                let style = impactStyle(rawStyle)
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "haptics.impact requires light, medium, heavy, soft, or rigid."
                    )
                )
                return
            }
            let generator = UIImpactFeedbackGenerator(style: style)
            generator.prepare()
            generator.impactOccurred()
            reply(replyHandler, result: NSNull())
        case .hapticsNotify:
            guard
                let rawType = request.params["type"] as? String,
                let type = notificationType(rawType)
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "haptics.notify requires success, warning, or error."
                    )
                )
                return
            }
            let generator = UINotificationFeedbackGenerator()
            generator.prepare()
            generator.notificationOccurred(type)
            reply(replyHandler, result: NSNull())
        case .hapticsSelection:
            let generator = UISelectionFeedbackGenerator()
            generator.prepare()
            generator.selectionChanged()
            reply(replyHandler, result: NSNull())
        case .voicePermission:
            reply(
                replyHandler,
                result: ["state": microphonePermissionState()]
            )
        }
    }

    private func reply(
        _ replyHandler: @escaping (Any?, String?) -> Void,
        result: Any
    ) {
        replyHandler(
            [
                "ok": true,
                "result": result,
            ],
            nil
        )
    }

    private func reply(
        _ replyHandler: @escaping (Any?, String?) -> Void,
        error: NativeBridgeError
    ) {
        replyHandler(
            [
                "ok": false,
                "error": error.jsonObject,
            ],
            nil
        )
    }

    private func impactStyle(_ rawValue: String) -> UIImpactFeedbackGenerator.FeedbackStyle? {
        switch rawValue {
        case "light":
            return .light
        case "medium":
            return .medium
        case "heavy":
            return .heavy
        case "soft":
            return .soft
        case "rigid":
            return .rigid
        default:
            return nil
        }
    }

    private func notificationType(
        _ rawValue: String
    ) -> UINotificationFeedbackGenerator.FeedbackType? {
        switch rawValue {
        case "success":
            return .success
        case "warning":
            return .warning
        case "error":
            return .error
        default:
            return nil
        }
    }

    private func microphonePermissionState() -> String {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return "granted"
        case .denied:
            return "denied"
        case .undetermined:
            return "prompt"
        @unknown default:
            return "prompt"
        }
    }
}
