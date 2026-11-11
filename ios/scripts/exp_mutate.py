import sys
p = "ios/Avibe/MobileAccount.swift"
s = open(p).read()
variant = sys.argv[1]
if variant == "m1":
    # The ledger forgets a store as soon as its removal reports success (the old rule).
    old = """            if let error = await operations.removeIdentifier(candidate) {
                Self.logger.error("""
    assert s.count(old) == 1
    s = s.replace(old, """            if let error = await operations.removeIdentifier(candidate) {
                _ = error
                Self.logger.error(""")
    old2 = """                    \"Named WebKit store cleanup failed for \\(candidate.uuidString, privacy: .public): \\(String(describing: error), privacy: .public)\"
                )
            }
        }"""
    assert s.count(old2) == 1, s.count(old2)
    s = s.replace(old2, """                    \"Named WebKit store cleanup failed for \\(candidate.uuidString, privacy: .public): \\(String(describing: error), privacy: .public)\"
                )
            } else {
                ownedIdentifiers.remove(candidate)
            }
        }""")
elif variant == "m2":
    # The sweep removes every listed store but the current one, its owner's or not.
    old = """        for candidate in candidates.sorted(by: {"""
    assert s.count(old) == 1
    s = s.replace(old, """        for candidate in candidates.union(listed).subtracting([identifier]).sorted(by: {""")
else:
    raise SystemExit("unknown variant")
open(p, "w").write(s)
