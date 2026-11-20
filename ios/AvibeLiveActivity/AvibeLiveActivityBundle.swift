import SwiftUI
import WidgetKit

@main
struct AvibeLiveActivityBundle: WidgetBundle {
    var body: some Widget {
        VoiceCaptureLiveActivity()
        TurnLiveActivity()
    }
}
