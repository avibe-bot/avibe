import UIKit

@MainActor
final class KeyboardViewportLayoutController: NSObject {
    enum BottomEdge {
        case container
        case safeArea
    }

    let bottomConstraint: NSLayoutConstraint

    init(
        contentView: UIView,
        containerView: UIView,
        bottomEdge: BottomEdge
    ) {
        switch bottomEdge {
        case .container:
            bottomConstraint = contentView.bottomAnchor.constraint(
                equalTo: containerView.bottomAnchor
            )
        case .safeArea:
            bottomConstraint = contentView.bottomAnchor.constraint(
                equalTo: containerView.safeAreaLayoutGuide.bottomAnchor
            )
        }
        super.init()
    }

    func refreshForContainerGeometryChange() {}
}
