import SwiftUI
import XCTest
@testable import Avibe

/// DRAFT-ONLY (not for merge): review frames for the background voice Live
/// Activity, rendered from the real `VoiceCaptureLiveActivity` views. The
/// system draws the island and Lock Screen chrome on a device; here it is
/// a plain stand-in (black island, a material card), so only our content is
/// under review. Run with `-testLanguage zh-Hans`.
@MainActor
final class VoiceCaptureFramesTests: XCTestCase {
    private typealias State = VoiceCaptureAttributes.ContentState

    private var states: [(String, State)] {
        let now = Date()
        let start = now.addingTimeInterval(-7)
        let heard = "帮我把明天上午的会挪到下午三点，再提醒我带电脑"
        return [
            ("1-listening", State(phase: .listening, startedAt: start)),
            ("2-sending", State(phase: .sending, startedAt: start, endedAt: now)),
            ("3-sent", State(phase: .sent, startedAt: start, endedAt: now, heard: heard)),
            ("4-failed-offline", State(phase: .failed, startedAt: start, endedAt: now, reason: .offline)),
            ("5-needs-app-consent", State(phase: .needsApp, startedAt: now, endedAt: now, reason: .consent)),
            ("6-failed-update", State(phase: .failed, startedAt: now, endedAt: now, reason: .updateAvibe)),
        ]
    }

    func testRenderReviewFrames() throws {
        for scheme in [ColorScheme.light, .dark] {
            for (name, state) in states {
                let sheet = FrameSheet(state: state, scheme: scheme)
                    .environment(\.colorScheme, scheme)
                    .environment(\.locale, Locale(identifier: "zh-Hans"))
                let renderer = ImageRenderer(content: sheet)
                renderer.scale = 3
                guard let image = renderer.uiImage else {
                    XCTFail("no image for \(name)")
                    continue
                }
                let attachment = XCTAttachment(image: image)
                attachment.name = "frame-\(scheme == .dark ? "dark" : "light")-\(name)"
                attachment.lifetime = .keepAlways
                add(attachment)
            }
        }
    }
}

private struct FrameSheet: View {
    let state: VoiceCaptureAttributes.ContentState
    let scheme: ColorScheme

    var body: some View {
        VStack(spacing: 28) {
            caption("灵动岛 · 紧凑")
            compact
            caption("灵动岛 · 最小（与其他 App 同时进行时）")
            minimal
            caption("灵动岛 · 展开（长按）")
            expanded
            caption("锁屏")
            lockScreen
        }
        .padding(.vertical, 32)
        .frame(width: 402)
        .background(
            LinearGradient(
                colors: scheme == .dark
                    ? [Color(red: 0.05, green: 0.08, blue: 0.2), Color(red: 0.1, green: 0.25, blue: 0.3)]
                    : [Color(red: 0.75, green: 0.85, blue: 0.95), Color(red: 0.7, green: 0.9, blue: 0.85)],
                startPoint: .top, endPoint: .bottom
            )
        )
    }

    private func caption(_ text: String) -> some View {
        Text(text).font(.caption).foregroundStyle(.secondary)
    }

    private var compact: some View {
        HStack(spacing: 0) {
            VoiceCaptureMark().frame(width: 52)
            Spacer().frame(width: 126)
            VoiceCaptureCompactTrailing(state: state).frame(width: 52)
        }
        .font(.system(size: 15, weight: .semibold))
        .frame(height: 37)
        .background(Capsule().fill(.black))
        .environment(\.colorScheme, .dark)
    }

    private var minimal: some View {
        HStack(spacing: 8) {
            Capsule().fill(.black).frame(width: 126, height: 37)
            VoiceCaptureGlyph(state: state)
                .font(.system(size: 15, weight: .semibold))
                .frame(width: 37, height: 37)
                .background(Circle().fill(.black))
        }
        .environment(\.colorScheme, .dark)
    }

    private var expanded: some View {
        VStack(spacing: 10) {
            HStack(alignment: .center) {
                VoiceCaptureGlyph(state: state).font(.title2).padding(.leading, 4)
                Spacer()
                Text(VoiceCaptureCopy.title(state)).font(.headline).lineLimit(1)
                Spacer()
                VoiceCaptureClock(state: state).font(.title3.monospacedDigit()).padding(.trailing, 4)
            }
            VoiceCaptureDetail(state: state)
                .padding(.horizontal, VoiceCaptureDetail.islandInset)
                .padding(.bottom, 4)
        }
        .padding(.horizontal, 18)
        .padding(.top, 14)
        .padding(.bottom, 18)
        .frame(width: 382)
        .background(RoundedRectangle(cornerRadius: 44, style: .continuous).fill(.black))
        .foregroundStyle(.white)
        .environment(\.colorScheme, .dark)
    }

    private var lockScreen: some View {
        VoiceCaptureLockScreenView(state: state)
            .padding(16)
            .frame(width: 370, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .fill(scheme == .dark ? Color.black.opacity(0.55) : Color.white.opacity(0.8))
            )
    }
}
