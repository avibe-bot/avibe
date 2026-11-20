import Foundation

/// One Action Button take that is not sent yet, as it is kept on the phone:
/// where it goes, what it needs to be sent, and how far it got. Its audio
/// is the file next to it.
struct VoiceOutboxEntry: Codable, Equatable {
    let id: UUID
    /// Its place in the order takes were recorded in: lower is earlier.
    let sequence: Int
    let createdAt: Date
    /// The account and instance it was spoken to; it is never sent anywhere
    /// else.
    let accountID: String
    let instanceID: String
    let origin: String
    let storeIdentifier: UUID
    /// The take's one transcription id, repeated on every send.
    let dictationID: String
    /// `capture_attempt`, and `capture_timing` as it stood when recording
    /// ended.
    var capture: String
    var timing: String
    /// Recorded in the background (not in front).
    var inBackground: Bool
    /// Its words, once transcribed: sending again does not transcribe again.
    var text: String?
    /// The message POST was begun. Its outcome may be unknown (a lost
    /// answer, the App ending meanwhile), so it is never made again.
    var posted = false
    /// Sends tried after the first.
    var retries = 0
    /// Times it could not be turned into text.
    var transcriptionFailures = 0
    /// Why it is kept (`offline`, `transcriptionFailed`, `signIn`).
    var reason: String?

    var target: VoiceCaptureTarget? {
        WebOrigin.parseConfigured(origin).map {
            VoiceCaptureTarget(accountID: accountID, instanceID: instanceID, origin: $0, storeIdentifier: storeIdentifier)
        }
    }
}

/// The takes that are not sent yet, in the order they were recorded, on
/// disk: a take outlives the island, a new press, and the App being ended.
///
/// They live in Application Support (Caches may be emptied by the system at
/// any time), excluded from backup, for at most `retention`; an account
/// sign-out or change removes them all. Each is `<sequence>-<id>.json` with
/// its audio `<id>.m4a`.
struct VoiceCaptureOutbox {
    /// How long an unsent take may stay on the phone (Product, 11-20).
    static let retention: TimeInterval = 24 * 60 * 60
    /// More than this many unsent takes: the oldest goes.
    static let maximumEntries = 20

    let directory: URL
    private let files = FileManager.default

    init(directory: URL) {
        self.directory = directory
    }

    /// `Application Support/VoiceOutbox`, made if missing and excluded from
    /// backup.
    static func standardDirectory() throws -> URL {
        let support = try FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        )
        return support.appending(path: "VoiceOutbox", directoryHint: .isDirectory)
    }

    private func prepare() throws {
        try files.createDirectory(at: directory, withIntermediateDirectories: true)
        var url = directory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
    }

    func audioURL(for id: UUID) -> URL {
        directory.appending(path: "\(id.uuidString.lowercased()).m4a")
    }

    private func entryURL(for entry: VoiceOutboxEntry) -> URL {
        directory.appending(path: "\(String(format: "%010d", entry.sequence))-\(entry.id.uuidString.lowercased()).json")
    }

    /// Every unsent take, earliest first. A record that cannot be read, or
    /// whose audio is gone, is removed.
    func entries() -> [VoiceOutboxEntry] {
        let names = (try? files.contentsOfDirectory(atPath: directory.path)) ?? []
        var entries: [VoiceOutboxEntry] = []
        for name in names where name.hasSuffix(".json") {
            let url = directory.appending(path: name)
            guard
                let data = try? Data(contentsOf: url),
                let entry = try? Self.decoder.decode(VoiceOutboxEntry.self, from: data),
                entry.text != nil || files.fileExists(atPath: audioURL(for: entry.id).path)
            else {
                try? files.removeItem(at: url)
                continue
            }
            entries.append(entry)
        }
        return entries.sorted { $0.sequence < $1.sequence }
    }

    /// The place the next take gets: after every take kept now.
    func nextSequence() -> Int {
        (entries().last?.sequence ?? 0) + 1
    }

    /// Keeps a take: its audio is moved in, then its record is written.
    /// Returns the takes that had to go to make room (the oldest first).
    @discardableResult
    func add(_ entry: VoiceOutboxEntry, movingAudioFrom audio: URL) throws -> [VoiceOutboxEntry] {
        try prepare()
        let destination = audioURL(for: entry.id)
        try? files.removeItem(at: destination)
        try files.moveItem(at: audio, to: destination)
        do {
            try write(entry)
        } catch {
            try? files.removeItem(at: destination)
            throw error
        }
        let kept = entries()
        let overflow = Array(kept.dropLast(Self.maximumEntries))
        overflow.forEach(remove)
        return overflow
    }

    /// Writes a take's record over its last one.
    func update(_ entry: VoiceOutboxEntry) throws {
        try write(entry)
    }

    private func write(_ entry: VoiceOutboxEntry) throws {
        try Self.encoder.encode(entry).write(to: entryURL(for: entry), options: .atomic)
    }

    func remove(_ entry: VoiceOutboxEntry) {
        try? files.removeItem(at: entryURL(for: entry))
        try? files.removeItem(at: audioURL(for: entry.id))
    }

    /// Removes everything: an account sign-out or change.
    func removeAll() {
        try? files.removeItem(at: directory)
    }

    /// Removes and returns the takes kept longer than `retention`.
    func removeExpired(now: Date) -> [VoiceOutboxEntry] {
        let expired = entries().filter { now.timeIntervalSince($0.createdAt) >= Self.retention }
        expired.forEach(remove)
        return expired
    }

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        return decoder
    }()
}
