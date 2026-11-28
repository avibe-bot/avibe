import HealthKit
import SwiftUI
import UIKit
import XCTest
@testable import Avibe

/// Synthetic samples only. These tests never construct a reader or contact HKHealthStore.
final class HealthDataReaderTests: XCTestCase {
    @MainActor
    func testNativeConsentPresentsTheTrustedDestinationAndDisclosure() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let previous = scene.windows.first(where: \.isKeyWindow)
        let request = HealthConsentRequest(target: HealthConnectionTarget(
            accountID: "fixture", instanceID: "fixture-instance", name: "Personal Fixture",
            origin: "https://personal-fixture.invalid"
        ))
        let host = UIHostingController(rootView: HealthConsentView(request: request, answer: { _ in }))
        let window = UIWindow(windowScene: scene)
        window.rootViewController = host
        window.overrideUserInterfaceStyle = .light
        window.makeKeyAndVisible()
        defer { window.isHidden = true; window.rootViewController = nil; previous?.makeKey() }
        host.view.setNeedsLayout()
        host.view.layoutIfNeeded()
        try await Task.sleep(for: .milliseconds(300))
        let image = UIGraphicsImageRenderer(bounds: host.view.bounds).image { _ in
            host.view.drawHierarchy(in: host.view.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = "Native Health consent — synthetic instance"
        attachment.lifetime = .keepAlways
        add(attachment)
        XCTAssertGreaterThan(host.view.bounds.height, 0)
    }

    @MainActor
    func testHeartStatisticsQueryCombinesSourcesAndUsesTheLocalDSTCalendar() throws {
        let aggregation = try window(days: 2, now: "2026-11-02T12:00:00-08:00", timezone: "America/Los_Angeles")
        XCTAssertEqual(aggregation.days[0].end.timeIntervalSince(aggregation.days[0].start), 25 * 3600)
        let heart = HealthDataReader.heartStatisticsQuery(.heartRate, aggregation: aggregation)
        XCTAssertEqual(heart.options, [.discreteMin, .discreteAverage, .discreteMax])
        XCTAssertFalse(heart.options.contains(.separateBySource), "Use combined readable sources, not source subtotals")
        XCTAssertEqual(heart.intervalComponents.timeZone, aggregation.calendar.timeZone)
        XCTAssertEqual(heart.intervalComponents.day, 1)
        let hrv = HealthDataReader.heartStatisticsQuery(.heartRateVariabilitySDNN, aggregation: aggregation)
        XCTAssertEqual(hrv.options, .discreteAverage)
    }

    func testHeartDailySummariesPreserveEmptyRealZeroAndSampleFreshness() throws {
        let aggregation = try window(days: 2)
        let latest = date("2026-11-18T07:00:00+08:00")
        let values: [Date: HealthDayAggregation.HeartStatistics] = [aggregation.days[1].start:
            .init(minimum: 50, average: 72.5, maximum: 140, latest: latest)]
        let rows = aggregation.heart("heart_rate", statistics: values, queriedAt: aggregation.now)
        XCTAssertEqual(rows[0]["availability"] as? String, "empty")
        XCTAssertTrue(rows[0]["value"] is NSNull)
        XCTAssertEqual(try value(rows[1])["average_bpm"] as? Double, 72.5)
        XCTAssertEqual(Set(try value(rows[1]).keys), ["min_bpm", "average_bpm", "max_bpm"])
        XCTAssertEqual(rows[1]["latest_sample_end_at"] as? String, HealthDayAggregation.timestamp(latest))
        let hrv = aggregation.heart("hrv", statistics: [aggregation.days[1].start:
            .init(minimum: nil, average: 0, maximum: nil, latest: latest)], queriedAt: aggregation.now)
        XCTAssertEqual(try value(hrv[1])["sdnn_ms"] as? Double, 0)
        XCTAssertEqual(hrv[1]["availability"] as? String, "ok")
    }

    func testHeartAverageIsClampedBeforeBuildingTheUploadPayload() throws {
        let aggregation = try window(days: 1)
        for average in [Double(72).nextUp, Double(72).nextDown] {
            let rows = aggregation.heart("heart_rate", statistics: [aggregation.days[0].start:
                .init(minimum: 72, average: average, maximum: 72, latest: aggregation.now)], queriedAt: aggregation.now)
            XCTAssertEqual(try value(rows[0])["average_bpm"] as? Double, 72)
            XCTAssertEqual(rows[0]["availability"] as? String, "ok")
        }
    }

    func testRestingHeartRateUsesLatestDailyEstimateAndExcludesNextMidnight() throws {
        let aggregation = try window(days: 2)
        let type = HKQuantityType(.restingHeartRate)
        let unit = HKUnit.count().unitDivided(by: .minute())
        func sample(_ time: String, _ value: Double) -> HKQuantitySample {
            HKQuantitySample(type: type, quantity: HKQuantity(unit: unit, doubleValue: value),
                             start: date(time), end: date(time))
        }
        let samples = [sample("2026-11-17T09:00:00+08:00", 50), sample("2026-11-17T23:59:59+08:00", 55),
                       sample("2026-11-18T00:00:00+08:00", 60), sample("2026-11-19T00:00:00+08:00", 99)]
        let rows = aggregation.restingHeartRate(samples, queriedAt: aggregation.now)
        XCTAssertEqual(try value(rows[0])["bpm"] as? Double, 55)
        XCTAssertEqual(try value(rows[1])["bpm"] as? Double, 60)
        let empty = aggregation.restingHeartRate([], queriedAt: aggregation.now)
        XCTAssertTrue(empty.allSatisfy { $0["availability"] as? String == "empty" && $0["value"] is NSNull })
    }

    func testWorkoutActivityNamesIncludeModernTypesAndUnknownFallback() {
        XCTAssertEqual(HealthDayAggregation.activityName(.highIntensityIntervalTraining), "high_intensity_interval_training")
        XCTAssertEqual(HealthDayAggregation.activityName(.pickleball), "pickleball")
        XCTAssertEqual(HealthDayAggregation.activityName(.underwaterDiving), "underwater_diving")
        XCTAssertEqual(HealthDayAggregation.activityName(HKWorkoutActivityType(rawValue: 99999)!), "other_99999")
    }

    private func date(_ text: String) -> Date {
        ISO8601DateFormatter().date(from: text)!
    }

    private func window(
        days: Int = 1, now: String = "2026-11-18T12:00:00+08:00",
        timezone: String = "Asia/Shanghai"
    ) throws -> HealthDayAggregation {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: timezone)!
        return try HealthDayAggregation(days: days, now: date(now), calendar: calendar)
    }

    private func sleep(
        _ start: String, _ end: String,
        _ category: HKCategoryValueSleepAnalysis = .asleepCore, source: String = "watch"
    ) -> HealthDayAggregation.SleepSample {
        .init(start: date(start), end: date(end), category: category, source: source)
    }

    private func value(_ item: [String: Any]) throws -> [String: Any] {
        try XCTUnwrap(item["value"] as? [String: Any])
    }

    // Observable contract: inclusive calendar windows, not fixed second offsets.
    // Credible regression: subtracting 24 h mislabels dates around DST. No prior reader coverage exists.
    func testInitialAndRoutineWindowsIncludeTodayExactlyOnce() throws {
        let initial = try window(days: 30)
        XCTAssertEqual(initial.days.count, 30)
        XCTAssertEqual(initial.days.first?.name, "2026-10-20")
        XCTAssertEqual(initial.days.last?.name, "2026-11-18")
        let routine = try window(days: 8)
        XCTAssertEqual(routine.days.map(\.name), [
            "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14",
            "2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18",
        ])
        for count in [0, -1, 32] {
            XCTAssertThrowsError(try window(days: count))
        }
        XCTAssertEqual(try window(days: 31).days.count, 31)
    }

    func testSpringAndFallDaysUseTheCalendarTimezone() throws {
        let spring = try window(days: 2, now: "2026-03-09T12:00:00-07:00", timezone: "America/Los_Angeles")
        XCTAssertEqual(spring.days.map(\.name), ["2026-03-08", "2026-03-09"])
        XCTAssertEqual(spring.days[0].end.timeIntervalSince(spring.days[0].start), 23 * 3600)
        let fall = try window(days: 2, now: "2026-11-02T12:00:00-08:00", timezone: "America/Los_Angeles")
        XCTAssertEqual(fall.days.map(\.name), ["2026-11-01", "2026-11-02"])
        XCTAssertEqual(fall.days[0].end.timeIntervalSince(fall.days[0].start), 25 * 3600)
    }

    // Observable contract: unavailable components remain unknown, explicit recorded zero remains zero.
    // Credible regression: sumQuantity nil is converted to 0 or incomplete activity looks complete.
    func testQuantityAbsenceIsEmptyAndAnExplicitZeroIsPreserved() throws {
        let aggregation = try window(days: 2)
        let recordedEnd = date("2026-11-18T10:42:00+08:00")
        let rows = aggregation.steps([
            aggregation.days[1].start: .init(value: 0, latestSampleEnd: recordedEnd),
        ], queriedAt: aggregation.now)
        XCTAssertEqual(rows[0]["availability"] as? String, "empty")
        XCTAssertTrue(rows[0]["value"] is NSNull)
        XCTAssertTrue(rows[0]["latest_sample_end_at"] is NSNull)
        XCTAssertEqual(rows[1]["availability"] as? String, "ok")
        XCTAssertEqual(try value(rows[1])["count"] as? Double, 0)
        XCTAssertEqual(rows[1]["latest_sample_end_at"] as? String, "2026-11-18T02:42:00Z")
        XCTAssertEqual(rows[1]["timezone"] as? String, "Asia/Shanghai")
        XCTAssertEqual(rows[0]["closed"] as? Bool, true)
        XCTAssertEqual(rows[1]["closed"] as? Bool, false)
    }

    func testActivityRequiresBothQuantitiesAndUsesTheirRealLatestSampleEnd() throws {
        let aggregation = try window(days: 3)
        let earlier = date("2026-11-16T07:01:00+08:00")
        let later = date("2026-11-16T17:05:00+08:00")
        let exercise: [Date: HealthDayAggregation.Quantity] = [
            aggregation.days[0].start: .init(value: 23, latestSampleEnd: earlier),
            aggregation.days[1].start: .init(value: 4, latestSampleEnd: earlier),
        ]
        let energy: [Date: HealthDayAggregation.Quantity] = [
            aggregation.days[0].start: .init(value: 156, latestSampleEnd: later),
            aggregation.days[2].start: .init(value: 55, latestSampleEnd: later),
        ]
        let rows = aggregation.activity(exercise: exercise, energy: energy, queriedAt: aggregation.now)
        XCTAssertEqual(try value(rows[0])["exercise_minutes"] as? Double, 23)
        XCTAssertEqual(try value(rows[0])["active_energy_kcal"] as? Double, 156)
        XCTAssertEqual(rows[0]["latest_sample_end_at"] as? String, "2026-11-16T09:05:00Z")
        for row in rows.dropFirst() {
            XCTAssertEqual(row["availability"] as? String, "empty")
            XCTAssertTrue(row["value"] is NSNull)
        }
    }

    // Observable contract: deduplicate across samples/stages/sources before summing asleep time.
    // Credible regression: duplicates and overlapping source summaries inflate sleep duration.
    func testSleepUnionsDuplicateOverlappingAndAdjacentSamples() throws {
        let aggregation = try window()
        let first = sleep("2026-11-17T22:00:00+08:00", "2026-11-18T02:00:00+08:00")
        let rows = aggregation.sleep([
            first, first,
            sleep("2026-11-18T01:00:00+08:00", "2026-11-18T03:00:00+08:00", .asleepDeep),
            sleep("2026-11-18T03:00:00+08:00", "2026-11-18T04:00:00+08:00", .asleepREM),
            sleep("2026-11-17T21:00:00+08:00", "2026-11-18T05:00:00+08:00", .inBed, source: "phone"),
            sleep("2026-11-17T21:00:00+08:00", "2026-11-18T05:00:00+08:00", .inBed, source: "watch"),
        ], queriedAt: aggregation.now)
        let result = try value(rows[0])
        XCTAssertEqual(result["asleep_minutes"] as? Double, 360)
        XCTAssertEqual(result["in_bed_minutes"] as? Double, 480)
        let stages = try XCTUnwrap(result["stages"] as? [String: Any])
        XCTAssertEqual(stages["core"] as? Double, 240)
        XCTAssertEqual(stages["deep"] as? Double, 120)
        XCTAssertEqual(stages["rem"] as? Double, 60)
        XCTAssertNil(stages["awake"])
        XCTAssertEqual(result["start_at"] as? String, "2026-11-17T13:00:00Z")
        XCTAssertEqual(result["end_at"] as? String, "2026-11-17T21:00:00Z")
    }

    func testMultipleStageSourcesSuppressStagesWithoutDuplicatingTotalSleep() throws {
        let aggregation = try window()
        let rows = aggregation.sleep([
            sleep("2026-11-17T22:00:00+08:00", "2026-11-18T03:00:00+08:00", .asleepCore),
            sleep("2026-11-17T23:00:00+08:00", "2026-11-18T04:00:00+08:00", .asleepDeep, source: "other"),
        ], queriedAt: aggregation.now)
        let result = try value(rows[0])
        XCTAssertEqual(result["asleep_minutes"] as? Double, 360)
        XCTAssertTrue(result["stages"] is NSNull)
        XCTAssertTrue(result["in_bed_minutes"] is NSNull)
    }

    func testUnspecifiedSleepFromAnotherSourceDoesNotSuppressTheOnlyStageSource() throws {
        let aggregation = try window()
        let rows = aggregation.sleep([
            sleep("2026-11-17T22:00:00+08:00", "2026-11-18T02:00:00+08:00", .asleepUnspecified, source: "phone"),
            sleep("2026-11-17T23:00:00+08:00", "2026-11-18T01:00:00+08:00", .asleepDeep),
        ], queriedAt: aggregation.now)
        let result = try value(rows[0])
        XCTAssertEqual(result["asleep_minutes"] as? Double, 240)
        let stages = try XCTUnwrap(result["stages"] as? [String: Any])
        XCTAssertEqual(stages["deep"] as? Double, 120)
    }

    func testSleepAbsenceAndInBedOnlyDoNotInventAsleepMinutes() throws {
        let aggregation = try window(days: 2)
        let rows = aggregation.sleep([
            sleep("2026-11-17T22:00:00+08:00", "2026-11-18T02:00:00+08:00", .inBed),
        ], queriedAt: aggregation.now)
        XCTAssertEqual(rows[0]["availability"] as? String, "empty")
        XCTAssertTrue(rows[0]["value"] is NSNull)
        let result = try value(rows[1])
        XCTAssertTrue(result["asleep_minutes"] is NSNull)
        XCTAssertEqual(result["in_bed_minutes"] as? Double, 240)
        XCTAssertTrue(result["stages"] is NSNull)
    }

    // Observable contract: the night ending on D belongs to D, clipped at 18:00 and the query time.
    // Credible regression: midnight bucketing, inclusive interval edges, or future durations leak in.
    func testSleepSplitsAtEighteenAndDoesNotCountAnAdjacentOrFutureSample() throws {
        let aggregation = try window(days: 2, now: "2026-11-18T19:00:00+08:00")
        let rows = aggregation.sleep([
            sleep("2026-11-16T17:00:00+08:00", "2026-11-16T18:00:00+08:00"),
            sleep("2026-11-17T17:00:00+08:00", "2026-11-17T19:00:00+08:00"),
            sleep("2026-11-18T18:00:00+08:00", "2026-11-18T19:00:00+08:00"),
        ], queriedAt: aggregation.now)
        XCTAssertEqual(try value(rows[0])["asleep_minutes"] as? Double, 60)
        XCTAssertEqual(try value(rows[1])["asleep_minutes"] as? Double, 60)
        XCTAssertEqual(rows[0]["closed"] as? Bool, true)
        XCTAssertEqual(rows[1]["closed"] as? Bool, true)
        // Freshness is the real sample end, even when the value was clipped to a window.
        XCTAssertEqual(rows[0]["latest_sample_end_at"] as? String, "2026-11-17T11:00:00Z")
    }

    func testSleepClipsAnOngoingSampleAtNowButKeepsItsRealEndTimestamp() throws {
        let aggregation = try window(now: "2026-11-18T06:00:00+08:00")
        let rows = aggregation.sleep([
            sleep("2026-11-17T22:00:00+08:00", "2026-11-18T08:00:00+08:00"),
            sleep("2026-11-18T07:00:00+08:00", "2026-11-18T09:00:00+08:00"),
        ], queriedAt: aggregation.now)
        let result = try value(rows[0])
        XCTAssertEqual(result["asleep_minutes"] as? Double, 480)
        XCTAssertEqual(result["end_at"] as? String, "2026-11-17T22:00:00Z")
        XCTAssertEqual(rows[0]["latest_sample_end_at"] as? String, "2026-11-18T00:00:00Z")
        XCTAssertEqual(rows[0]["closed"] as? Bool, false)
    }

    func testSleepAcrossBothDSTChangesCountsElapsedTimeAndKeepsEighteenOClockWindows() throws {
        for (now, start, end, minutes, windowHours) in [
            ("2026-03-08T19:00:00-07:00", "2026-03-07T22:00:00-08:00", "2026-03-08T08:00:00-07:00", 540.0, 23.0),
            ("2026-11-01T19:00:00-08:00", "2026-10-31T22:00:00-07:00", "2026-11-01T08:00:00-08:00", 660.0, 25.0),
        ] {
            let aggregation = try window(now: now, timezone: "America/Los_Angeles")
            let day = aggregation.days[0]
            XCTAssertEqual(aggregation.calendar.component(.hour, from: day.sleepStart), 18)
            XCTAssertEqual(aggregation.calendar.component(.hour, from: day.sleepEnd), 18)
            XCTAssertEqual(day.sleepEnd.timeIntervalSince(day.sleepStart) / 3600, windowHours)
            let rows = aggregation.sleep([sleep(start, end)], queriedAt: aggregation.now)
            XCTAssertEqual(try value(rows[0])["asleep_minutes"] as? Double, minutes)
        }
    }

    func testInvalidAndOutOfWindowSleepSamplesAreIgnored() throws {
        let aggregation = try window()
        let rows = aggregation.sleep([
            sleep("2026-11-16T22:00:00+08:00", "2026-11-17T06:00:00+08:00"),
            sleep("2026-11-18T01:00:00+08:00", "2026-11-18T01:00:00+08:00"),
            sleep("2026-11-18T03:00:00+08:00", "2026-11-18T02:00:00+08:00"),
        ], queriedAt: aggregation.now)
        XCTAssertEqual(rows[0]["availability"] as? String, "empty")
    }

    // Observable contract: workouts are assigned by start date, preserving actual duration/end.
    // Credible regression: splitting at midnight double counts a workout or fabricates energy.
    func testWorkoutsBelongToTheirStartDayAndKeepMissingOptionalQuantitiesAbsent() throws {
        let aggregation = try window(days: 2)
        let overnight = HKWorkout(
            activityType: .running, start: date("2026-11-17T23:30:00+08:00"),
            end: date("2026-11-18T00:30:00+08:00"), duration: 3000,
            totalEnergyBurned: nil, totalDistance: HKQuantity(unit: .meter(), doubleValue: 5000),
            metadata: nil
        )
        let outside = HKWorkout(
            activityType: .walking, start: date("2026-11-16T23:30:00+08:00"),
            end: date("2026-11-17T00:30:00+08:00")
        )
        let rows = aggregation.workouts([outside, overnight], queriedAt: aggregation.now)
        let workouts = try XCTUnwrap(try value(rows[0])["workouts"] as? [[String: Any]])
        XCTAssertEqual(workouts.count, 1)
        XCTAssertEqual(workouts[0]["activity"] as? String, "running")
        XCTAssertEqual(workouts[0]["duration_minutes"] as? Double, 50, "active duration, not wall time")
        XCTAssertEqual(workouts[0]["distance_m"] as? Double, 5000)
        XCTAssertNil(workouts[0]["active_energy_kcal"])
        XCTAssertEqual(rows[0]["latest_sample_end_at"] as? String, "2026-11-17T16:30:00Z")
        XCTAssertEqual(rows[1]["availability"] as? String, "empty")
        XCTAssertTrue(rows[1]["value"] is NSNull)
    }

    // Legacy workouts can have a total without associated quantity statistics.
    // The preceding missing-energy case cannot detect dropping such a recorded total.
    func testLegacyWorkoutEnergyIsPreservedWhenNoAssociatedStatisticsExist() throws {
        let aggregation = try window()
        let workout = HKWorkout(
            activityType: .walking, start: date("2026-11-18T07:00:00+08:00"),
            end: date("2026-11-18T07:30:00+08:00"), duration: 1800,
            totalEnergyBurned: HKQuantity(unit: .kilocalorie(), doubleValue: 123),
            totalDistance: nil, metadata: nil
        )
        let rows = aggregation.workouts([workout], queriedAt: aggregation.now)
        let workouts = try XCTUnwrap(try value(rows[0])["workouts"] as? [[String: Any]])
        XCTAssertEqual(workouts[0]["active_energy_kcal"] as? Double, 123)
        XCTAssertNil(workouts[0]["distance_m"])
    }

    // Observable contract: A2 emits each successful metric x day once, failures emit no rows.
    // Credible regression: a partial query failure replaces prior data with invented emptiness.
    func testPayloadHasCompleteSuccessfulMetricsAndNoItemsForFailedMetrics() throws {
        let aggregation = try window(days: 8)
        let locked = NSError(domain: HKErrorDomain, code: HKError.Code.errorDatabaseInaccessible.rawValue)
        let result = aggregation.window(results: [
            "steps": .success(aggregation.steps([:], queriedAt: aggregation.now)),
            "activity": .failure(locked),
            "workouts": .success(aggregation.workouts([], queriedAt: aggregation.now)),
            "sleep": .success(aggregation.sleep([], queriedAt: aggregation.now)),
        ], computedAt: aggregation.now)
        let payload = result.payload(sequence: 1042)
        XCTAssertEqual(payload["device_seq"] as? Int64, 1042)
        XCTAssertEqual(payload["computed_at"] as? String, "2026-11-18T04:00:00Z")
        let range = try XCTUnwrap(payload["window"] as? [String: Any])
        XCTAssertEqual(range["metrics"] as? [String], ["steps", "activity", "workouts", "sleep"])
        XCTAssertEqual(range["from"] as? String, "2026-11-11")
        XCTAssertEqual(range["to"] as? String, "2026-11-18")
        XCTAssertEqual(result.items.count, 24)
        for metric in ["steps", "workouts", "sleep"] {
            let items = result.items.filter { $0["metric"] as? String == metric }
            XCTAssertEqual(items.count, 8)
            XCTAssertEqual(Set(items.compactMap { $0["day"] as? String }).count, 8)
            XCTAssertEqual(result.metricStates[metric], "empty")
        }
        XCTAssertFalse(result.items.contains { $0["metric"] as? String == "activity" })
        XCTAssertEqual(result.failures.count, 1)
        XCTAssertEqual(result.failures[0]["metric"] as? String, "activity")
        XCTAssertEqual(result.failures[0]["code"] as? String, "protected_data_unavailable")
        XCTAssertEqual(result.metricStates["activity"], "protected_data_unavailable")
        XCTAssertNil(payload["metricStates"], "request result states are separate from A2")
        XCTAssertTrue(JSONSerialization.isValidJSONObject(payload))
        let encoded = try JSONSerialization.data(withJSONObject: payload)
        let decoded = try XCTUnwrap(try JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual((decoded["items"] as? [[String: Any]])?.count, 24)
    }

    func testMetricStateIsOkIfAnyDayHasDataAndMissingQueryResultIsAFailure() throws {
        let aggregation = try window(days: 2)
        let result = aggregation.window(results: [
            "steps": .success(aggregation.steps([
                aggregation.days[0].start: .init(value: 45, latestSampleEnd: aggregation.days[0].start),
            ], queriedAt: aggregation.now)),
        ], computedAt: aggregation.now)
        XCTAssertEqual(result.metricStates["steps"], "ok")
        XCTAssertEqual(result.items.count, 2)
        XCTAssertEqual(result.failures.count, 3)
        XCTAssertEqual(result.metricStates["sleep"], "internal")
        XCTAssertEqual(HealthReadError.code(for: HealthReadError.capabilityUnavailable), "capability_unavailable")
        XCTAssertEqual(
            HealthReadError.code(for: NSError(domain: HKErrorDomain, code: HKError.Code.errorHealthDataUnavailable.rawValue)),
            "capability_unavailable"
        )
        XCTAssertEqual(
            HealthReadError.code(for: NSError(domain: HKErrorDomain, code: HKError.Code.errorAuthorizationDenied.rawValue)),
            "internal", "read authorization state must not be inferred"
        )
        XCTAssertEqual(
            HealthReadError.code(for: NSError(domain: "other", code: HKError.Code.errorDatabaseInaccessible.rawValue)),
            "internal", "an unrelated domain must not look like a locked HealthKit database"
        )
    }
}
