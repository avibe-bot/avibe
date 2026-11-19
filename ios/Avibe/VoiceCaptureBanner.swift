import UIKit

/// One plain line over Avibe for a few seconds, with nothing to tap: says
/// why "Talk to Main Bot" opened the App (it records there already).
@MainActor
enum VoiceCaptureBanner {
    static func show(_ text: String, attempt: Int = 0) {
        guard let window = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .filter({ $0.activationState == .foregroundActive })
            .flatMap(\.windows)
            .first(where: \.isKeyWindow)
        else {
            // The scene is still coming up after `continueInForeground`.
            guard attempt < 20 else { return }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                show(text, attempt: attempt + 1)
            }
            return
        }
        let label = PaddedLabel()
        label.text = text
        label.numberOfLines = 0
        label.font = .preferredFont(forTextStyle: .subheadline)
        label.adjustsFontForContentSizeCategory = true
        label.textColor = .label
        label.backgroundColor = .secondarySystemBackground
        label.layer.cornerRadius = 12
        label.layer.masksToBounds = true
        label.isUserInteractionEnabled = false
        label.accessibilityTraits = .staticText
        label.translatesAutoresizingMaskIntoConstraints = false
        window.addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: window.safeAreaLayoutGuide.topAnchor, constant: 8),
            label.leadingAnchor.constraint(equalTo: window.leadingAnchor, constant: 16),
            label.trailingAnchor.constraint(equalTo: window.trailingAnchor, constant: -16),
        ])
        UIAccessibility.post(notification: .announcement, argument: text)
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(6))
            UIView.animate(withDuration: 0.3, animations: { label.alpha = 0 }) { _ in
                label.removeFromSuperview()
            }
        }
    }

    private final class PaddedLabel: UILabel {
        override func drawText(in rect: CGRect) {
            super.drawText(in: rect.inset(by: UIEdgeInsets(top: 10, left: 14, bottom: 10, right: 14)))
        }

        override var intrinsicContentSize: CGSize {
            let size = super.intrinsicContentSize
            return CGSize(width: size.width + 28, height: size.height + 20)
        }
    }
}
