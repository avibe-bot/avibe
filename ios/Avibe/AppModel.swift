import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var instanceOrigin: WebOrigin?

    private static let instanceOriginKey = "avibe.instanceOrigin"
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let savedOrigin = defaults.string(forKey: Self.instanceOriginKey) {
            instanceOrigin = WebOrigin.parseConfigured(savedOrigin)
        }
    }

    @discardableResult
    func configureInstance(from input: String) -> Bool {
        guard let origin = WebOrigin.parseConfigured(input) else {
            return false
        }
        defaults.set(origin.url.absoluteString, forKey: Self.instanceOriginKey)
        instanceOrigin = origin
        return true
    }

    func forgetInstance() {
        defaults.removeObject(forKey: Self.instanceOriginKey)
        instanceOrigin = nil
    }
}
