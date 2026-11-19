import ActivityKit
import SwiftUI
import WidgetKit

@main
struct ProbeWidgets: WidgetBundle {
    var body: some Widget {
        ProbeLiveActivity()
    }
}

struct ProbeLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: ProbeAttributes.self) { context in
            Text("\(context.attributes.app): \(context.state.label)").padding()
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.center) {
                    Text("\(context.attributes.app): \(context.state.label)")
                }
            } compactLeading: {
                Image(systemName: "waveform")
            } compactTrailing: {
                Text(context.attributes.app)
            } minimal: {
                Image(systemName: "waveform")
            }
        }
    }
}
