import Foundation

/// The input a take records from, as the session has it.
struct NativeHoldInputState: Equatable, Sendable {
    /// The input port's type (`MicrophoneBuiltIn`, `BluetoothHFP`…).
    var port: String
    var rate: Int
    var channels: Int

    /// `BuiltIn:48000/1`: short enough for the phone's line (`Microphone`
    /// dropped from the port's name).
    var text: String {
        let name = port.hasPrefix("Microphone") ? String(port.dropFirst("Microphone".count)) : port
        return "\(name):\(rate)/\(channels)"
    }
    /// The engine can record from it.
    var usable: Bool { rate > 0 && channels > 0 }
}

/// One change of a take's engine configuration: the input before and after
/// (nil: none), and whether the input started again.
struct NativeHoldInputChange: Equatable, Sendable {
    var from: NativeHoldInputState?
    var to: NativeHoldInputState?
    var restarted: Bool

    /// `BuiltIn:48000/1>BluetoothHFP:16000/1` (`none` for no input),
    /// and `!` where the input did not start again.
    var text: String {
        "\(from?.text ?? "none")>\(to?.text ?? "none")" + (restarted ? "" : "!")
    }

    /// For the page's trace of the take.
    var fields: [String: Any] {
        var fields: [String: Any] = ["cfgFrom": from?.port ?? "none", "cfgTo": to?.port ?? "none"]
        if let from { fields["cfgFromHz"] = from.rate; fields["cfgFromCh"] = from.channels }
        if let to { fields["cfgToHz"] = to.rate; fields["cfgToCh"] = to.channels }
        return fields
    }
}

/// An input whose engine can start again after its configuration changed.
protocol NativeHoldRestartableInput: AnyObject {
    /// The input as the session has it now; nil when there is none.
    func current() -> NativeHoldInputState?
    /// Fits the tap to the input's present format and starts the engine again.
    func restart() throws
}

/// What a take does when its engine's configuration changes under it. The
/// system stops the engine then (a headset came or went, or the hardware's
/// format settled after a slow start: 11-28 15:07, an engine that took
/// 658 ms to start delivered zeros, changed, and the take ended). While an
/// input remains, the engine starts again on it and the take goes on; with
/// none, the take ends as before. A few restarts at most: an input that
/// keeps changing ends the take.
///
/// Used on one queue only: the held input's, or the main one.
final class NativeHoldEngineRecovery: @unchecked Sendable {
    static let maxRestarts = 3

    private var last: NativeHoldInputState?
    private(set) var restarts = 0
    /// The take's input stopped for good (at the bound of a take no page
    /// took): a change that comes after, or was already on its way, starts
    /// nothing.
    private(set) var stopped = false

    init(input: NativeHoldInputState?) {
        last = input
    }

    func stop() {
        stopped = true
    }

    func changed(_ input: NativeHoldRestartableInput) -> NativeHoldInputChange {
        let from = last
        let now = input.current()
        if let now { last = now }
        guard !stopped, let now, now.usable, restarts < Self.maxRestarts else {
            return .init(from: from, to: now, restarted: false)
        }
        do {
            try input.restart()
        } catch {
            return .init(from: from, to: now, restarted: false)
        }
        restarts += 1
        return .init(from: from, to: now, restarted: true)
    }
}
