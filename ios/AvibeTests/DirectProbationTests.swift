import XCTest
@testable import Avibe

/// The probation rule against the cases it shares with the Desktop core
/// (`tests/fixtures/direct_probation_cases.json`, written by the reference
/// model beside it; docs/plans/remote-direct-probation.md).
final class DirectProbationTests: XCTestCase {
    func testTheSharedCases() throws {
        let url = try XCTUnwrap(
            Bundle(for: Self.self).url(forResource: "direct_probation_cases", withExtension: "json")
        )
        let result = try DirectProbationCases.run(Data(contentsOf: url))
        // The file is the contract: fewer cases here means a stale copy.
        XCTAssertGreaterThanOrEqual(result.cases, 29)
        XCTAssertGreaterThanOrEqual(result.events, 485)
        XCTAssertEqual(result.failures.map(\.description), [])
    }

    func testAStoredLevelOutsideTheRuleIsDropped() throws {
        // A build with other levels may have written it.
        let stored = try JSONDecoder().decode(
            [String: DirectProbationRecord].self,
            from: Data(#"{"a/i":{"level":7,"rose":"adopted","last_early_wall_ms":5},"a/j":{"level":1,"rose":"adopted","last_early_wall_ms":5}}"#.utf8)
        )
        var rule = DirectProbationGuard(stored: stored)
        XCTAssertEqual(rule.levelSeconds("a/i", now: 0, wall: 10), 0)
        XCTAssertEqual(rule.levelSeconds("a/j", now: 0, wall: 10), 30)
    }
}

/// Runs the shared probation cases through `DirectProbationGuard` as a small
/// simulated shell: it calls what the controller calls, in the order the
/// case gives, and compares what the case expects after every keyed event.
enum DirectProbationCases {
    struct Failure: CustomStringConvertible {
        let description: String
    }

    static func run(_ data: Data) throws -> (cases: Int, events: Int, failures: [Failure]) {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              root["version"] as? Int == 1,
              let rule = root["rule"] as? [String: Any],
              let cases = root["cases"] as? [[String: Any]]
        else {
            return (0, 0, [Failure(description: "not a version 1 file")])
        }
        var failures: [Failure] = []
        if rule["levels_s"] as? [Int] != DirectProbationGuard.levelsSeconds
            || (rule["early_ms"] as? NSNumber)?.int64Value != DirectProbationGuard.earlyMs
            || (rule["held_ms"] as? NSNumber)?.int64Value != DirectProbationGuard.heldMs
            || (rule["expiry_ms"] as? NSNumber)?.int64Value != DirectProbationGuard.expiryMs {
            failures.append(Failure(description: "the file's rule is not this build's: \(rule)"))
        }
        var events = 0
        for item in cases {
            let name = item["name"] as? String ?? "?"
            var rule = DirectProbationGuard()
            var wallOffset: Int64 = 0
            var adoptions: [String: Int] = [:]
            for event in item["events"] as? [[String: Any]] ?? [] {
                events += 1
                let kind = event["event"] as? String ?? ""
                let at = (event["at_ms"] as? NSNumber)?.int64Value ?? -1
                let wall = at + wallOffset
                let key = event["key"] as? String ?? ""
                let link = event["link"] as? Int ?? -1
                var got: [String: AnyHashable] = [:]
                switch kind {
                case "foreground":
                    rule.foreground()
                    continue
                case "background":
                    rule.background()
                    continue
                case "relaunch":
                    // Through the stored form and back, as a new process.
                    let stored = try JSONEncoder().encode(rule.stored)
                    rule = DirectProbationGuard(
                        stored: try JSONDecoder().decode([String: DirectProbationRecord].self, from: stored)
                    )
                    continue
                case "clock_back":
                    wallOffset -= (event["by_ms"] as? NSNumber)?.int64Value ?? 0
                    continue
                case "link_up":
                    got["wait_ms"] = rule.linkUp(
                        key, link: link, now: at, wall: wall, strict: event["strict"] as? Bool ?? false
                    )
                case "adopt":
                    let adopted = rule.adopt(key, link: link, now: at)
                    got["adopted"] = adopted
                    adoptions[key, default: 0] += adopted ? 1 : 0
                case "link_down":
                    guard let cause = DirectLinkEnd(rawValue: event["cause"] as? String ?? "") else {
                        failures.append(Failure(description: "\(name) at \(at): unknown cause"))
                        continue
                    }
                    rule.linkDown(key, link: link, cause: cause, now: at, wall: wall)
                case "check":
                    break
                default:
                    failures.append(Failure(description: "\(name) at \(at): unknown event \(kind)"))
                    continue
                }
                got["level_s"] = rule.levelSeconds(key, now: at, wall: wall)
                got["rose"] = rule.rose(key).rawValue
                got["usable"] = rule.isAdopted(key)
                got["adoptions"] = adoptions[key, default: 0]
                let expect = event["expect"] as? [String: Any] ?? [:]
                var want: [String: AnyHashable] = [:]
                for (field, value) in expect {
                    if let flag = value as? Bool, field == "usable" || field == "adopted" {
                        want[field] = flag
                    } else if let number = value as? NSNumber {
                        want[field] = field == "wait_ms" ? AnyHashable(number.int64Value) : AnyHashable(number.intValue)
                    } else if let text = value as? String {
                        want[field] = text
                    }
                }
                if want.isEmpty || want != got {
                    failures.append(Failure(description: "\(name) at \(at) ms, \(kind): expected \(want), got \(got)"))
                }
            }
        }
        return (cases.count, events, failures)
    }
}
