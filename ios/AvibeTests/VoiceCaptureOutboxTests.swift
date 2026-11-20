import XCTest
@testable import Avibe

/// The takes that are not sent yet, kept on disk in their order
/// (VoiceCaptureOutbox).
final class VoiceCaptureOutboxTests: XCTestCase {
    private var base: URL!
    private var outbox: VoiceCaptureOutbox!

    override func setUpWithError() throws {
        base = FileManager.default.temporaryDirectory.appending(path: "outbox-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        outbox = VoiceCaptureOutbox(directory: base.appending(path: "VoiceOutbox"))
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: base)
    }

    private func take(_ bytes: [UInt8] = [1, 2, 3]) throws -> URL {
        let url = base.appending(path: "take-\(UUID().uuidString).m4a")
        try Data(bytes).write(to: url)
        return url
    }

    private func entry(createdAt: Date = Date(timeIntervalSince1970: 1_795_135_000)) -> VoiceOutboxEntry {
        VoiceOutboxEntry(
            id: UUID(), sequence: outbox.nextSequence(), createdAt: createdAt,
            accountID: "account-1", instanceID: "instance-1", origin: "https://instance-one.example",
            storeIdentifier: UUID(), dictationID: "ios-action-\(UUID().uuidString.lowercased())",
            capture: "0 [background warm]", timing: "island=10 record=190", inBackground: true
        )
    }

    func testTakesComeBackInTheOrderTheyWereRecordedAfterARelaunch() throws {
        var kept: [VoiceOutboxEntry] = []
        for index in 0..<3 {
            let entry = entry()
            try outbox.add(entry, movingAudioFrom: take([UInt8(index)]))
            kept.append(entry)
        }
        XCTAssertEqual(kept.map(\.sequence), [1, 2, 3])
        // Another process reading the same directory.
        let again = VoiceCaptureOutbox(directory: outbox.directory)
        XCTAssertEqual(again.entries(), kept)
        XCTAssertEqual(try Data(contentsOf: again.audioURL(for: kept[1].id)), Data([1]))
        // A later take goes after them, however many were sent meanwhile.
        again.remove(kept[0])
        again.remove(kept[1])
        XCTAssertEqual(again.nextSequence(), 4)
        XCTAssertEqual(again.entries(), [kept[2]])
    }

    func testAKeptTakeMovesItsAudioOutOfCachesAndIsNotBackedUp() throws {
        let audio = try take()
        let entry = entry()
        try outbox.add(entry, movingAudioFrom: audio)
        XCTAssertFalse(FileManager.default.fileExists(atPath: audio.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: outbox.audioURL(for: entry.id).path))
        XCTAssertEqual(try outbox.directory.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup, true)
    }

    func testWhatATakeLearnedIsKeptWithIt() throws {
        var entry = entry()
        try outbox.add(entry, movingAudioFrom: take())
        entry.text = "hello"
        entry.posted = true
        entry.retries = 2
        entry.reason = "offline"
        try outbox.update(entry)
        XCTAssertEqual(outbox.entries(), [entry])
        XCTAssertEqual(outbox.entries().first?.target?.origin.serialized, "https://instance-one.example")
    }

    func testATakeIsRemovedWithItsAudioAndAnAccountChangeRemovesAll() throws {
        let first = entry()
        try outbox.add(first, movingAudioFrom: take())
        let second = entry()
        try outbox.add(second, movingAudioFrom: take())
        outbox.remove(first)
        XCTAssertFalse(FileManager.default.fileExists(atPath: outbox.audioURL(for: first.id).path))
        XCTAssertEqual(outbox.entries(), [second])
        outbox.removeAll()
        XCTAssertTrue(outbox.entries().isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: outbox.audioURL(for: second.id).path))
        // And it can be used again afterwards.
        let third = entry()
        try outbox.add(third, movingAudioFrom: take())
        XCTAssertEqual(outbox.entries(), [third])
    }

    func testTakesOlderThanADayAreRemoved() throws {
        let now = Date(timeIntervalSince1970: 1_795_200_000)
        let old = entry(createdAt: now.addingTimeInterval(-VoiceCaptureOutbox.retention))
        try outbox.add(old, movingAudioFrom: take())
        let recent = entry(createdAt: now.addingTimeInterval(-VoiceCaptureOutbox.retention + 1))
        try outbox.add(recent, movingAudioFrom: take())
        XCTAssertEqual(outbox.removeExpired(now: now), [old])
        XCTAssertEqual(outbox.entries(), [recent])
        XCTAssertFalse(FileManager.default.fileExists(atPath: outbox.audioURL(for: old.id).path))
    }

    func testARecordWithoutItsAudioOrThatCannotBeReadIsDropped() throws {
        let lost = entry()
        try outbox.add(lost, movingAudioFrom: take())
        try FileManager.default.removeItem(at: outbox.audioURL(for: lost.id))
        try Data("not json".utf8).write(to: outbox.directory.appending(path: "0000000009-broken.json"))
        XCTAssertTrue(outbox.entries().isEmpty)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: outbox.directory.path), [])

        // Its words are enough once it was transcribed.
        var transcribed = entry()
        try outbox.add(transcribed, movingAudioFrom: take())
        transcribed.text = "hello"
        try outbox.update(transcribed)
        try FileManager.default.removeItem(at: outbox.audioURL(for: transcribed.id))
        XCTAssertEqual(outbox.entries(), [transcribed])
    }

    func testTheOldestGoesWhenTooManyAreKept() throws {
        var kept: [VoiceOutboxEntry] = []
        var dropped: [VoiceOutboxEntry] = []
        for _ in 0..<(VoiceCaptureOutbox.maximumEntries + 2) {
            let entry = entry()
            dropped += try outbox.add(entry, movingAudioFrom: take())
            kept.append(entry)
        }
        XCTAssertEqual(dropped, Array(kept.prefix(2)))
        XCTAssertEqual(outbox.entries(), Array(kept.dropFirst(2)))
    }
}
