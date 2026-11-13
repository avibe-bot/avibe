import sys
MUTATIONS = {
    "m1-showAccount-needs-idle": ("ios/Avibe/AppModel.swift",
        "            where runtimeController != nil\n                && sessionCoordinator.isSelectedRuntime(runtime):",
        "            where runtimeController != nil\n                && sessionCoordinator.isCurrentRuntime(runtime):"),
    "m2-read-refused-during-push": ("ios/Avibe/MobileAccount.swift",
        "        if let snapshot {\n            guard keepsSelection else {",
        "        if let snapshot {\n            guard phase == .ready else {"),
    "m3-server-order-only": ("ios/Avibe/MobileAccount.swift",
        "        return MobileSessionSnapshot(account: snapshot.account, instances: instances)",
        "        _ = instances\n        return snapshot"),
    "m4-swipe-presses-nothing": ("ios/Avibe/MobileOnboardingBridge.swift",
        "          back.click();\n          return true;",
        "          return false;"),
    "m5-transport-released-under-request": ("ios/Avibe/AppModel.swift",
        "        sessionCoordinator.leaveAccountSurface()\n        route = .browser(runtime)",
        "        sessionCoordinator.deactivateAccountSurface()\n        route = .browser(runtime)"),
}
path, old, new = MUTATIONS[sys.argv[1]]
s = open(path).read()
assert s.count(old) == 1, (sys.argv[1], s.count(old))
open(path, "w").write(s.replace(old, new))
