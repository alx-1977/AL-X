"""How many external reviews AL/X has asked for, against Friedl's allowance.

Each Greptile review is paid for. Friedl, 2026-10-14: "Three reviews per PR is
fine, after that she needs to ask. Then make it 12 for the month (4 x 3) after
which she also needs to ask." So every review she actually asks for is
recorded here - one that only joined a round already on the head asked for
nothing and is not - and the next request is refused once either count is
reached. Past that, a review needs Friedl's word for that exact request.

The month is the calendar month in local time, as Friedl reads it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

REVIEWS_PER_PULL_REQUEST = 3
REVIEWS_PER_MONTH = 12


@dataclass(frozen=True, slots=True)
class ReviewAllowanceState:
    """Reviews already asked for, and whether another fits."""

    pull_request_reviews: int
    month_reviews: int
    per_pull_request: int = REVIEWS_PER_PULL_REQUEST
    per_month: int = REVIEWS_PER_MONTH

    @property
    def reached(self) -> bool:
        return (
            self.pull_request_reviews >= self.per_pull_request
            or self.month_reviews >= self.per_month
        )

    def as_values(self) -> dict[str, int]:
        return {
            "pull_request_reviews": self.pull_request_reviews,
            "reviews_per_pull_request": self.per_pull_request,
            "month_reviews": self.month_reviews,
            "reviews_per_month": self.per_month,
        }


class SQLiteReviewAllowance:
    """The record of reviews asked for, kept across restarts."""

    def __init__(
        self,
        path: Path,
        per_pull_request: int = REVIEWS_PER_PULL_REQUEST,
        per_month: int = REVIEWS_PER_MONTH,
    ) -> None:
        if per_pull_request <= 0 or per_month <= 0:
            raise ValueError("review allowances must be positive")
        self._path = Path(path)
        self._per_pull_request = per_pull_request
        self._per_month = per_month
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_requests ("
                "pull_request_number INTEGER NOT NULL, "
                "head_sha TEXT NOT NULL, "
                "requested_at TEXT NOT NULL, "
                "month TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    @staticmethod
    def _month(at: datetime) -> str:
        return at.astimezone().strftime("%Y-%m")

    def state(self, pull_request_number: int, at: datetime) -> ReviewAllowanceState:
        with self._lock, self._connect() as connection:
            pull_request = connection.execute(
                "SELECT COUNT(*) FROM review_requests WHERE pull_request_number = ?",
                (pull_request_number,),
            ).fetchone()[0]
            month = connection.execute(
                "SELECT COUNT(*) FROM review_requests WHERE month = ?",
                (self._month(at),),
            ).fetchone()[0]
        return ReviewAllowanceState(
            int(pull_request), int(month), self._per_pull_request, self._per_month
        )

    def record(self, pull_request_number: int, head_sha: str, at: datetime) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO review_requests VALUES (?, ?, ?, ?)",
                (pull_request_number, head_sha, at.isoformat(), self._month(at)),
            )


__all__ = [
    "REVIEWS_PER_MONTH",
    "REVIEWS_PER_PULL_REQUEST",
    "ReviewAllowanceState",
    "SQLiteReviewAllowance",
]
