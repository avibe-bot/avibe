import Foundation
import HealthKit

@MainActor
protocol HealthDataReading {
    func authorize() async throws
    func query(days: Int, now: Date) async throws -> HealthQueryWindow
    /// Return only after the change has been processed or durably queued.
    func startObserving(onChange: @escaping () async -> Void) async -> Bool
    func stopObserving()
    func disableBackgroundDelivery() async -> Bool
}

struct HealthQueryWindow {
    static let metrics = ["steps", "activity", "workouts", "sleep"]

    let computedAt: Date
    let from: String
    let to: String
    let items: [[String: Any]]
    let failures: [[String: Any]]
    var metricStates: [String: String]

    func payload(sequence: Int64) -> [String: Any] {
        [
            "device_seq": sequence,
            "computed_at": HealthDayAggregation.timestamp(computedAt),
            "window": ["metrics": Self.metrics, "from": from, "to": to],
            "items": items,
            "failures": failures,
        ]
    }
}

enum HealthReadError: Error {
    case capabilityUnavailable
    case invalidWindow
    case missingQueryResult

    static func code(for error: Error) -> String {
        if case HealthReadError.capabilityUnavailable = error { return "capability_unavailable" }
        let nsError = error as NSError
        if nsError.domain == HKErrorDomain {
            if nsError.code == HKError.Code.errorDatabaseInaccessible.rawValue {
                return "protected_data_unavailable"
            }
            if nsError.code == HKError.Code.errorHealthDataUnavailable.rawValue {
                return "capability_unavailable"
            }
        }
        return "internal"
    }
}

/// A query owns one calendar snapshot, so timezone changes cannot split a window.
struct HealthDayAggregation {
    struct Day {
        let name: String
        let start: Date
        let end: Date
        let sleepStart: Date
        let sleepEnd: Date
    }

    struct Quantity {
        let value: Double
        let latestSampleEnd: Date?
    }

    struct SleepSample {
        let start: Date
        let end: Date
        let category: HKCategoryValueSleepAnalysis
        /// HKSource.bundleIdentifier, deliberately excluding version/device revisions.
        let source: String
    }

    let calendar: Calendar
    let now: Date
    let days: [Day]

    init(days count: Int, now: Date, calendar: Calendar) throws {
        guard (1...31).contains(count) else { throw HealthReadError.invalidWindow }
        self.calendar = calendar
        self.now = now
        let today = calendar.startOfDay(for: now)
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        self.days = (0..<count).map { offset in
            let start = calendar.date(byAdding: .day, value: offset - count + 1, to: today)!
            let end = calendar.date(byAdding: .day, value: 1, to: start)!
            let previous = calendar.date(byAdding: .day, value: -1, to: start)!
            return Day(
                name: formatter.string(from: start), start: start, end: end,
                sleepStart: calendar.date(bySettingHour: 18, minute: 0, second: 0, of: previous)!,
                sleepEnd: calendar.date(bySettingHour: 18, minute: 0, second: 0, of: start)!
            )
        }
    }

    static func timestamp(_ date: Date) -> String {
        // UTC is an ISO 8601 timestamp; each item separately names its local timezone.
        ISO8601DateFormatter().string(from: date)
    }

    func item(
        metric: String, day: Day, value: [String: Any]?, latest: Date?, queriedAt: Date
    ) -> [String: Any] {
        [
            "metric": metric,
            "day": day.name,
            "timezone": calendar.timeZone.identifier,
            "availability": value == nil ? "empty" : "ok",
            "value": value.map { $0 as Any } ?? NSNull(),
            "latest_sample_end_at": latest.map { Self.timestamp($0) as Any } ?? NSNull(),
            "queried_at": Self.timestamp(queriedAt),
            "closed": (metric == "sleep" ? day.sleepEnd : day.end) <= now,
        ]
    }

    func steps(_ quantities: [Date: Quantity], queriedAt: Date) -> [[String: Any]] {
        days.map { day in
            let quantity = quantities[day.start]
            return item(
                metric: "steps", day: day, value: quantity.map { ["count": $0.value] },
                latest: quantity?.latestSampleEnd, queriedAt: queriedAt
            )
        }
    }

    func activity(
        exercise: [Date: Quantity], energy: [Date: Quantity], queriedAt: Date
    ) -> [[String: Any]] {
        days.map { day in
            let minutes = exercise[day.start]
            let calories = energy[day.start]
            var value: [String: Any]?
            if let minutes, let calories {
                value = ["exercise_minutes": minutes.value, "active_energy_kcal": calories.value]
            }
            return item(
                metric: "activity", day: day, value: value,
                latest: [minutes?.latestSampleEnd, calories?.latestSampleEnd].compactMap { $0 }.max(),
                queriedAt: queriedAt
            )
        }
    }

    func workouts(_ samples: [HKWorkout], queriedAt: Date) -> [[String: Any]] {
        days.map { day in
            let workouts = samples.filter {
                $0.startDate >= day.start && $0.startDate < day.end && $0.startDate <= now
            }.sorted {
                if $0.startDate == $1.startDate { return $0.uuid.uuidString < $1.uuid.uuidString }
                return $0.startDate < $1.startDate
            }
            let values: [[String: Any]] = workouts.map { sample in
                var value: [String: Any] = [
                    // Stable HealthKit activity identifier, independent of UI language.
                    "activity": Self.activityName(sample.workoutActivityType),
                    "start_at": Self.timestamp(sample.startDate),
                    "end_at": Self.timestamp(sample.endDate),
                    "duration_minutes": sample.duration / 60,
                ]
                if let energy = sample.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()
                    ?? sample.totalEnergyBurned {
                    value["active_energy_kcal"] = energy.doubleValue(for: .kilocalorie())
                }
                if let distance = sample.totalDistance {
                    value["distance_m"] = distance.doubleValue(for: .meter())
                }
                return value
            }
            return item(
                metric: "workouts", day: day, value: values.isEmpty ? nil : ["workouts": values],
                latest: workouts.map(\.endDate).max(), queriedAt: queriedAt
            )
        }
    }

    func sleep(_ samples: [SleepSample], queriedAt: Date) -> [[String: Any]] {
        days.map { day in
            let end = min(day.sleepEnd, now)
            let relevant = samples.filter { $0.start < end && $0.end > day.sleepStart && $0.end > $0.start }
            func intervals(_ matching: (SleepSample) -> Bool) -> [DateInterval] {
                relevant.filter(matching).map {
                    DateInterval(start: max($0.start, day.sleepStart), end: min($0.end, end))
                }
            }
            let asleep = intervals { Self.isAsleep($0.category) }
            let inBed = intervals { $0.category == .inBed }
            let staged = relevant.filter { Self.isStage($0.category) }
            var value: [String: Any]?
            if !relevant.isEmpty {
                var stages: [String: Any]?
                if Set(staged.map(\.source)).count == 1 {
                    stages = [:]
                    for (key, category) in [
                        ("awake", HKCategoryValueSleepAnalysis.awake),
                        ("core", .asleepCore), ("deep", .asleepDeep),
                        ("rem", .asleepREM), ("unspecified", .asleepUnspecified),
                    ] {
                        let spans = intervals { $0.category == category }
                        // A missing stage is unknown, including a partially authorized source.
                        if !spans.isEmpty { stages?[key] = Self.unionMinutes(spans) }
                    }
                }
                value = [
                    "asleep_minutes": asleep.isEmpty ? NSNull() : Self.unionMinutes(asleep) as Any,
                    "in_bed_minutes": inBed.isEmpty ? NSNull() : Self.unionMinutes(inBed) as Any,
                    "start_at": Self.timestamp(relevant.map { max($0.start, day.sleepStart) }.min()!),
                    "end_at": Self.timestamp(relevant.map { min($0.end, end) }.max()!),
                    "stages": stages.map { $0 as Any } ?? NSNull(),
                ]
            }
            return item(
                metric: "sleep", day: day, value: value,
                latest: relevant.map(\.end).max(), queriedAt: queriedAt
            )
        }
    }

    static func activityName(_ activity: HKWorkoutActivityType) -> String {
        switch activity.rawValue {
        case 1: return "american_football"
        case 2: return "archery"
        case 3: return "australian_football"
        case 4: return "badminton"
        case 5: return "baseball"
        case 6: return "basketball"
        case 7: return "bowling"
        case 8: return "boxing"
        case 9: return "climbing"
        case 10: return "cricket"
        case 11: return "cross_training"
        case 12: return "curling"
        case 13: return "cycling"
        case 14: return "dance"
        case 15: return "dance_inspired_training"
        case 16: return "elliptical"
        case 17: return "equestrian_sports"
        case 18: return "fencing"
        case 19: return "fishing"
        case 20: return "functional_strength_training"
        case 21: return "golf"
        case 22: return "gymnastics"
        case 23: return "handball"
        case 24: return "hiking"
        case 25: return "hockey"
        case 26: return "hunting"
        case 27: return "lacrosse"
        case 28: return "martial_arts"
        case 29: return "mind_and_body"
        case 30: return "mixed_metabolic_cardio_training"
        case 31: return "paddle_sports"
        case 32: return "play"
        case 33: return "preparation_and_recovery"
        case 34: return "racquetball"
        case 35: return "rowing"
        case 36: return "rugby"
        case 37: return "running"
        case 38: return "sailing"
        case 39: return "skating_sports"
        case 40: return "snow_sports"
        case 41: return "soccer"
        case 42: return "softball"
        case 43: return "squash"
        case 44: return "stair_climbing"
        case 45: return "surfing_sports"
        case 46: return "swimming"
        case 47: return "table_tennis"
        case 48: return "tennis"
        case 49: return "track_and_field"
        case 50: return "traditional_strength_training"
        case 51: return "volleyball"
        case 52: return "walking"
        case 53: return "water_fitness"
        case 54: return "water_polo"
        case 55: return "water_sports"
        case 56: return "wrestling"
        case 57: return "yoga"
        case 58: return "barre"
        case 59: return "core_training"
        case 60: return "cross_country_skiing"
        case 61: return "downhill_skiing"
        case 62: return "flexibility"
        case 63: return "high_intensity_interval_training"
        case 64: return "jump_rope"
        case 65: return "kickboxing"
        case 66: return "pilates"
        case 67: return "snowboarding"
        case 68: return "stairs"
        case 69: return "step_training"
        case 70: return "wheelchair_walk_pace"
        case 71: return "wheelchair_run_pace"
        case 72: return "tai_chi"
        case 73: return "mixed_cardio"
        case 74: return "hand_cycling"
        case 75: return "disc_sports"
        case 76: return "fitness_gaming"
        case 77: return "cardio_dance"
        case 78: return "social_dance"
        case 79: return "pickleball"
        case 80: return "cooldown"
        case 82: return "swim_bike_run"
        case 83: return "transition"
        case 84: return "underwater_diving"
        case 3000: return "other"
        default: return "other_\(activity.rawValue)"
        }
    }

    private static func isAsleep(_ category: HKCategoryValueSleepAnalysis) -> Bool {
        [.asleepUnspecified, .asleepCore, .asleepDeep, .asleepREM].contains(category)
    }

    private static func isStage(_ category: HKCategoryValueSleepAnalysis) -> Bool {
        [.awake, .asleepCore, .asleepDeep, .asleepREM].contains(category)
    }

    static func unionMinutes(_ intervals: [DateInterval]) -> Double {
        let sorted = intervals.filter { $0.duration > 0 }.sorted { $0.start < $1.start }
        guard var current = sorted.first else { return 0 }
        var seconds: TimeInterval = 0
        for next in sorted.dropFirst() {
            if next.start <= current.end {
                current = DateInterval(start: current.start, end: max(current.end, next.end))
            } else {
                seconds += current.duration
                current = next
            }
        }
        return (seconds + current.duration) / 60
    }

    /// Failures exclude the entire metric, including days assembled before a query failed.
    func window(
        results: [String: Result<[[String: Any]], Error>], computedAt: Date
    ) -> HealthQueryWindow {
        var items: [[String: Any]] = []
        var failures: [[String: Any]] = []
        var states: [String: String] = [:]
        for metric in HealthQueryWindow.metrics {
            switch results[metric] ?? .failure(HealthReadError.missingQueryResult) {
            case .success(let rows):
                items += rows
                states[metric] = rows.contains { $0["availability"] as? String == "ok" } ? "ok" : "empty"
            case .failure(let error):
                let code = HealthReadError.code(for: error)
                failures.append(["metric": metric, "at": Self.timestamp(computedAt), "code": code])
                states[metric] = code
            }
        }
        return HealthQueryWindow(
            computedAt: computedAt, from: days.first!.name, to: days.last!.name,
            items: items, failures: failures, metricStates: states
        )
    }
}

@MainActor
final class HealthDataReader: HealthDataReading {
    private let store: HKHealthStore
    private var observers: [HKObserverQuery] = []
    private var observationGeneration = UUID()
    /// Serialize enables/disables so stop during an enable cannot leave delivery enabled.
    private var deliveryOperation: Task<Bool, Never>?

    init(store: HKHealthStore = HKHealthStore()) {
        self.store = store
    }

    private var observedTypes: [HKSampleType] {
        [
            HKQuantityType(.stepCount), HKQuantityType(.appleExerciseTime),
            HKQuantityType(.activeEnergyBurned), HKObjectType.workoutType(),
            HKCategoryType(.sleepAnalysis),
        ]
    }

    func authorize() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthReadError.capabilityUnavailable }
        // A successful authorization request does not imply any read permission.
        try await store.requestAuthorization(toShare: [], read: Set(observedTypes))
    }

    func query(days: Int, now: Date) async throws -> HealthQueryWindow {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let aggregation = try HealthDayAggregation(days: days, now: now, calendar: calendar)
        var results: [String: Result<[[String: Any]], Error>] = [:]
        for metric in HealthQueryWindow.metrics {
            try Task.checkCancellation()
            do {
                guard HKHealthStore.isHealthDataAvailable() else { throw HealthReadError.capabilityUnavailable }
                switch metric {
                case "steps":
                    let quantities = try await quantities(.stepCount, unit: .count(), aggregation: aggregation)
                    results[metric] = .success(aggregation.steps(quantities, queriedAt: Date()))
                case "activity":
                    let exercise = try await quantities(.appleExerciseTime, unit: .minute(), aggregation: aggregation)
                    let energy = try await quantities(.activeEnergyBurned, unit: .kilocalorie(), aggregation: aggregation)
                    results[metric] = .success(aggregation.activity(exercise: exercise, energy: energy, queriedAt: Date()))
                case "workouts":
                    let samples = try await samples(
                        type: HKObjectType.workoutType(), start: aggregation.days.first!.start,
                        end: now, options: .strictStartDate
                    )
                    results[metric] = .success(aggregation.workouts(samples.compactMap { $0 as? HKWorkout }, queriedAt: Date()))
                default:
                    let samples = try await samples(
                        type: HKCategoryType(.sleepAnalysis), start: aggregation.days.first!.sleepStart,
                        end: min(aggregation.days.last!.sleepEnd, now)
                    )
                    let sleep = samples.compactMap { sample -> HealthDayAggregation.SleepSample? in
                        guard let sample = sample as? HKCategorySample,
                              let category = HKCategoryValueSleepAnalysis(rawValue: sample.value) else { return nil }
                        return .init(
                            start: sample.startDate, end: sample.endDate, category: category,
                            source: sample.sourceRevision.source.bundleIdentifier
                        )
                    }
                    results[metric] = .success(aggregation.sleep(sleep, queriedAt: Date()))
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                results[metric] = .failure(error)
            }
        }
        try Task.checkCancellation()
        return aggregation.window(results: results, computedAt: Date())
    }

    private func samples(
        type: HKSampleType, start: Date, end: Date, options: HKQueryOptions = []
    ) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: options)
            let query = HKSampleQuery(
                sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil
            ) { _, samples, error in
                if let error { continuation.resume(throwing: error) }
                else if let samples { continuation.resume(returning: samples) }
                else { continuation.resume(throwing: HealthReadError.missingQueryResult) }
            }
            store.execute(query)
        }
    }

    private func quantities(
        _ identifier: HKQuantityTypeIdentifier, unit: HKUnit, aggregation: HealthDayAggregation
    ) async throws -> [Date: HealthDayAggregation.Quantity] {
        let type = HKQuantityType(identifier)
        let start = aggregation.days.first!.start
        let collection: HKStatisticsCollection = try await withCheckedThrowingContinuation { continuation in
            var interval = DateComponents()
            interval.calendar = aggregation.calendar
            interval.timeZone = aggregation.calendar.timeZone
            interval.day = 1
            let query = HKStatisticsCollectionQuery(
                quantityType: type,
                quantitySamplePredicate: HKQuery.predicateForSamples(withStart: start, end: aggregation.now),
                options: .cumulativeSum, anchorDate: start, intervalComponents: interval
            )
            query.initialResultsHandler = { [store] query, collection, error in
                // Collection queries can live-update; this read needs only the initial result.
                store.stop(query)
                if let error { continuation.resume(throwing: error) }
                else if let collection { continuation.resume(returning: collection) }
                else { continuation.resume(throwing: HealthReadError.missingQueryResult) }
            }
            store.execute(query)
        }
        // Statistics.endDate is the bucket boundary, never a sample freshness timestamp.
        let raw = try await samples(type: type, start: start, end: aggregation.now)
        var result: [Date: HealthDayAggregation.Quantity] = [:]
        for day in aggregation.days {
            guard let sum = collection.statistics(for: day.start)?.sumQuantity() else { continue }
            let end = min(day.end, aggregation.now)
            let latest = raw.filter {
                $0.startDate < end && $0.endDate >= day.start
                    && ($0.endDate > day.start || $0.startDate == day.start)
            }.map(\.endDate).max()
            result[day.start] = .init(value: sum.doubleValue(for: unit), latestSampleEnd: latest)
        }
        return result
    }

    func startObserving(onChange: @escaping () async -> Void) async -> Bool {
        guard HKHealthStore.isHealthDataAvailable() else { return false }
        stopObserving()
        let generation = observationGeneration
        let types = observedTypes
        for type in types {
            let query = HKObserverQuery(sampleType: type, predicate: nil) { [weak self] _, completion, error in
                Task { @MainActor in
                    // HealthKit must be told even for errors, stale callbacks, or a released owner.
                    defer { completion() }
                    guard let self, self.observationGeneration == generation, error == nil else { return }
                    await onChange()
                }
            }
            observers.append(query)
            store.execute(query)
        }
        let previous = deliveryOperation
        let operation = Task { @MainActor [weak self] in
            _ = await previous?.value
            guard let self, self.observationGeneration == generation else { return false }
            var enabled = true
            for type in types {
                guard self.observationGeneration == generation else { return false }
                let success = await withCheckedContinuation { continuation in
                    self.store.enableBackgroundDelivery(
                        for: type, frequency: type.identifier == HKQuantityTypeIdentifier.stepCount.rawValue ? .hourly : .immediate
                    ) { success, error in
                        continuation.resume(returning: success && error == nil)
                    }
                }
                enabled = enabled && success
            }
            return enabled && self.observationGeneration == generation
        }
        deliveryOperation = operation
        return await operation.value
    }

    func disableBackgroundDelivery() async -> Bool {
        stopObserving()
        let previous = deliveryOperation
        let operation = Task { @MainActor in
            _ = await previous?.value
            guard HKHealthStore.isHealthDataAvailable() else { return true }
            return await withCheckedContinuation { continuation in
                self.store.disableAllBackgroundDelivery { success, error in
                    continuation.resume(returning: success && error == nil)
                }
            }
        }
        deliveryOperation = operation
        return await operation.value
    }

    func stopObserving() {
        observationGeneration = UUID()
        for observer in observers { store.stop(observer) }
        let needsDisable = !observers.isEmpty || deliveryOperation != nil
        observers.removeAll()
        guard needsDisable else { return }
        let previous = deliveryOperation
        let store = store
        let types = observedTypes
        deliveryOperation = Task { @MainActor in
            _ = await previous?.value
            var disabled = true
            for type in types {
                let success = await withCheckedContinuation { continuation in
                    store.disableBackgroundDelivery(for: type) { success, error in
                        continuation.resume(returning: success && error == nil)
                    }
                }
                disabled = disabled && success
            }
            return disabled
        }
    }
}
