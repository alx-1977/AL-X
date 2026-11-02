"""Configured banking targets, kept outside the execution implementation."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProgramSettings:
    banking_targets: tuple[str, ...]

    @classmethod
    def from_environment(cls, environment):
        path = Path(environment.get(
            "ALX_BANKING_TARGETS_FILE",
            str(Path(__file__).resolve().parents[3] / "config" / "banking-targets.json"),
        ))
        values = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(values, list) or not values
                or any(not isinstance(item, str) or not item.strip() for item in values)):
            raise ValueError("banking targets must be a nonempty string array")
        return cls(tuple(values))
