import Foundation

/// One Action Button take that is not sent yet, as it is kept on the phone:
/// where it goes, what it needs to be sent, and how far it got. Its audio
/// is the file next to it.
struct VoiceOutboxEntry: Codable, Equatable {
    let id: UUID
    /// Its place in the order takes were recorded in: lower is earlier.
    let sequence: Int
    let createdAt: Date
    /// When it was spoken (the end of its recording): what "ten minutes"
    /// and the alert's "3 hours ago" are counted from.
    let spokenAt: Date
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

    /// Removes audio no record points at: a write the App did not live to
    /// finish, or a record that could not be read. Without this such audio
    /// would outlast `retention`, which only records are held to. Called
    /// when a run first reads the outbox.
    func removeOrphanedAudio() {
        let kept = Set(entries().map { audioURL(for: $0.id).lastPathComponent })
        let names = (try? files.contentsOfDirectory(atPath: directory.path)) ?? []
        for name in names where name.hasSuffix(".m4a") && !kept.contains(name) {
            try? files.removeItem(at: directory.appending(path: name))
        }
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
            // The audio goes back where it was: it is the take's only copy.
            try? files.moveItem(at: destination, to: audio)
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

/// The line a take that is sent late carries before its words, so the main
/// Bot (and the conversation) know it was not said just now:
/// （这条语音说于今天 08:44，刚刚才送达。） or, for another day,
/// （这条语音说于 11 月 19 日 23:10，刚刚才送达。）, in the phone's local time
/// and the App's language.
enum VoiceCaptureLateNote {
    /// The time as 08:44, and the day as 11 月 19 日 or Nov 19.
    static func parts(
        spoken: Date, language: String, calendar: Calendar = .current
    ) -> (time: String, day: String) {
        let fields = calendar.dateComponents([.month, .day, .hour, .minute], from: spoken)
        let time = String(format: "%02d:%02d", fields.hour ?? 0, fields.minute ?? 0)
        if language.hasPrefix("zh") {
            return (time, "\(fields.month ?? 1) 月 \(fields.day ?? 1) 日")
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "MMM d"
        return (time, formatter.string(from: spoken))
    }

    static func line(
        spoken: Date,
        now: Date,
        language: String = Bundle.main.preferredLocalizations.first ?? "en",
        calendar: Calendar = .current
    ) -> String {
        let (time, day) = parts(spoken: spoken, language: language, calendar: calendar)
        if calendar.isDate(spoken, inSameDayAs: now) {
            return String(localized: LocalizedStringResource(
                String.LocalizationValue("voiceCapture.late.today \(time)"), table: "VoiceCapture"
            ))
        }
        return String(localized: LocalizedStringResource(
            String.LocalizationValue("voiceCapture.late.day \(day) \(time)"), table: "VoiceCapture"
        ))
    }

    /// The message a late take is sent as: the line, a line break, the words.
    static func message(_ words: String, spoken: Date, now: Date) -> String {
        "\(line(spoken: spoken, now: now))\n\(words)"
    }
}
