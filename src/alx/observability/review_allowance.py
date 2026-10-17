"""How many external reviews AL/X has asked for, against Friedl's allowance.

Each Greptile review is paid for. Friedl, 2026-10-14: "Three reviews per PR is
fine, after that she needs to ask." So every review she actually asks for is
recorded here - one that only joined a round already on the head asked for
nothing and is not - and the next request for that pull request is refused
once its count is reached. Past that, a review needs Friedl's word for that
exact request.

There is no monthly limit. Friedl set twelve a month on 2026-10-14 and removed
it on 2026-10-17 ("remove this limit please"), when it stopped AL/X reviewing
her own launcher fix (D-046a).

A review is counted before its trigger is posted, never after: a request whose
outcome is unclear (the trigger may have gone out) stays counted, and only one
GitHub definitely refused is released. Counting afterwards let a review that
went out go uncounted when anything between the post and the record failed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

REVIEWS_PER_PULL_REQUEST = 3


@dataclass(frozen=True, slots=True)
class ReviewAllowanceState:
    """Reviews already asked for on one pull request, and whether another fits."""

    pull_request_reviews: int
    per_pull_request: int = REVIEWS_PER_PULL_REQUEST

    @property
    def reached(self) -> bool:
        return self.pull_request_reviews >= self.per_pull_request

    def as_values(self) -> dict[str, int]:
        return {
            "pull_request_reviews": self.pull_request_reviews,
            "reviews_per_pull_request": self.per_pull_request,
        }


class ReviewAllowanceReached(Exception):
    """Raised instead of counting a review that does not fit the allowance."""

    def __init__(self, state: ReviewAllowanceState) -> None:
        self.state = state
        super().__init__("review allowance reached")


class SQLiteReviewAllowance:
    """The record of reviews asked for, kept across restarts."""

    def __init__(
        self,
        path: Path,
        per_pull_request: int = REVIEWS_PER_PULL_REQUEST,
    ) -> None:
        if per_pull_request <= 0:
            raise ValueError("review allowances must be positive")
        self._path = Path(path)
        self._per_pull_request = per_pull_request
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # The month column is kept, written empty, so a record made while
            # the monthly limit existed still reads; nothing counts by it.
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
    def _count(connection: sqlite3.Connection, pull_request_number: int) -> int:
        return int(connection.execute(
            "SELECT COUNT(*) FROM review_requests WHERE pull_request_number = ?",
            (pull_request_number,),
        ).fetchone()[0])

    def state(self, pull_request_number: int, at: datetime) -> ReviewAllowanceState:
        with self._lock, self._connect() as connection:
            return ReviewAllowanceState(
                self._count(connection, pull_request_number), self._per_pull_request
            )

    def reserve(
        self, pull_request_number: int, head_sha: str, at: datetime, enforce: bool = True
    ) -> int:
        """Count one review about to be asked for, and return its record.

        With `enforce`, refuses (ReviewAllowanceReached) instead of counting
        when the pull request's allowance is already used; checked and counted
        in one transaction, so two requests cannot both take the last review.
        Without it - a review Friedl approved past the allowance - it counts.
        """
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if enforce:
                state = ReviewAllowanceState(
                    self._count(connection, pull_request_number), self._per_pull_request
                )
                if state.reached:
                    raise ReviewAllowanceReached(state)
            cursor = connection.execute(
                "INSERT INTO review_requests VALUES (?, ?, ?, ?)",
                (pull_request_number, head_sha, at.isoformat(), ""),
            )
            return int(cursor.lastrowid)

    def release(self, record: int) -> None:
        """Uncount a review that was certainly never asked for."""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM review_requests WHERE rowid = ?", (record,))


__all__ = [
    "REVIEWS_PER_PULL_REQUEST",
    "ReviewAllowanceReached",
    "ReviewAllowanceState",
    "SQLiteReviewAllowance",
]
