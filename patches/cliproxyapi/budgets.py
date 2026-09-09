"""One bounded allowance for verifier commands, namespace cleanup and its caller."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class PhaseBudget:
    commands: int
    command_seconds: int
    input_seconds: int = 180
    preflight_seconds: int = 30
    setup_seconds: int = 60
    cleanup_seconds: int = 30

    @property
    def candidate_seconds(self) -> int:
        return self.commands * self.command_seconds + self.input_seconds

    @property
    def namespace_seconds(self) -> int:
        return self.candidate_seconds + self.preflight_seconds + self.setup_seconds + self.cleanup_seconds

    @property
    def driver_seconds(self) -> int:
        return self.namespace_seconds + self.cleanup_seconds

    def receipt(self) -> dict:
        return {
            **asdict(self), "candidate_seconds": self.candidate_seconds,
            "namespace_seconds": self.namespace_seconds, "driver_seconds": self.driver_seconds,
        }


PHASES = {
    "test": PhaseBudget(3, 600),
    "build": PhaseBudget(1, 600),
    "wire": PhaseBudget(1, 600),
    "check": PhaseBudget(1, 600),
    "probe": PhaseBudget(1, 10, input_seconds=0),
    # Deliberate harmless timeout/teardown diagnostic; never an engine phase.
    "timeout": PhaseBudget(1, 1, input_seconds=0),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--layer", choices=("driver", "namespace", "candidate"))
    args = parser.parse_args()
    budget = PHASES[args.phase]
    print(getattr(budget, args.layer + "_seconds") if args.layer else json.dumps(budget.receipt()))


if __name__ == "__main__":
    main()
