"""Durable storage for AL/X's project scopes.

Identity and lifecycle, and nothing else. This module records a project id, a
readable name, a status and provenance. It never reads a record's content,
never decides which project a memory or goal belongs to, and holds no durable
knowledge of its own.

Archiving is the lifecycle operation, and it is deliberately the only one that
retires a project. Records scoped to an archived project keep their own
retention and supersession untouched: a project's lifecycle describes the
work, never the truth or reachability of what was learned doing it.

Deletion exists for a project created by mistake and is refused once anything
references it, because removing a scope that records still name would leave
those records pointing at nothing while appearing to have lost their history.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from alx.contracts.provenance import provenance_from_storage, provenance_to_storage
from alx.contracts.scope import Project, ProjectStatus

SCHEMA_VERSION = 1
PROVENANCE_COLUMNS = (
    "content_origins",
    "content_recorded_at",
    "content_expires_at",
    "mail_references",
)


class ProjectStoreError(Exception):
    pass


class ProjectNotFound(ProjectStoreError):
    pass


class DuplicateProject(ProjectStoreError):
    pass


class ProjectInUse(ProjectStoreError):
    """A project still named by durable records cannot be deleted."""


class UnsupportedSchema(ProjectStoreError):
    pass


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class SQLiteProjectStore:
    """Persist project scopes without interpreting what they contain."""

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteProjectStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._connection.close()
            raise UnsupportedSchema(
                f"project database schema {version} is newer than supported "
                f"schema {SCHEMA_VERSION}"
            )
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS projects ("
                "project_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "created_at TEXT NOT NULL, status TEXT NOT NULL, "
                "content_origins TEXT, content_recorded_at TEXT, "
                "content_expires_at TEXT, mail_references TEXT)"
            )
            columns = {
                item[1]
                for item in self._connection.execute("PRAGMA table_info(projects)")
            }
            for column in PROVENANCE_COLUMNS:
                if column not in columns:
                    self._connection.execute(
                        f'ALTER TABLE projects ADD COLUMN "{column}" TEXT'
                    )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create(self, project: Project) -> Project:
        _aware(project.created_at, "created_at")
        if self._exists(project.project_id):
            raise DuplicateProject(project.project_id)
        origins, recorded, expires, references = provenance_to_storage(
            project.provenance
        )
        with self._connection:
            self._connection.execute(
                "INSERT INTO projects(project_id, name, created_at, status, "
                "content_origins, content_recorded_at, content_expires_at, "
                "mail_references) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project.project_id,
                    project.name,
                    project.created_at.isoformat(),
                    project.status.value,
                    origins,
                    recorded,
                    expires,
                    references,
                ),
            )
        return project

    def load(self, project_id: str) -> Project:
        row = self._connection.execute(
            "SELECT project_id, name, created_at, status, content_origins, "
            "content_recorded_at, content_expires_at, mail_references "
            "FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProjectNotFound(project_id)
        return _project_from_row(row)

    def list_projects(
        self, *, status: ProjectStatus | None = None
    ) -> tuple[Project, ...]:
        """Every project, or every project of one status, in creation order.

        The order is presentation only. Which project a record belongs to is
        never decided here.
        """
        if status is None:
            rows = self._connection.execute(
                "SELECT project_id, name, created_at, status, content_origins, "
                "content_recorded_at, content_expires_at, mail_references "
                "FROM projects ORDER BY rowid"
            ).fetchall()
        else:
            if not isinstance(status, ProjectStatus):
                raise TypeError("status must be a ProjectStatus")
            rows = self._connection.execute(
                "SELECT project_id, name, created_at, status, content_origins, "
                "content_recorded_at, content_expires_at, mail_references "
                "FROM projects WHERE status = ? ORDER BY rowid",
                (status.value,),
            ).fetchall()
        return tuple(_project_from_row(item) for item in rows)

    def set_status(self, project_id: str, status: ProjectStatus) -> Project:
        """Retire or reopen a project without touching anything scoped to it."""
        if not isinstance(status, ProjectStatus):
            raise TypeError("status must be a ProjectStatus")
        if not self._exists(project_id):
            raise ProjectNotFound(project_id)
        with self._connection:
            self._connection.execute(
                "UPDATE projects SET status = ? WHERE project_id = ?",
                (status.value, project_id),
            )
        return self.load(project_id)

    def delete(self, project_id: str, referencing_records: int = 0) -> None:
        """Remove a project created in error.

        `referencing_records` is supplied by the caller that knows which stores
        hold scopes, because this store deliberately cannot see them: reaching
        into memories or goals from here would make the project store a second
        reader of records it has no business interpreting. A non-zero count
        refuses the deletion, so a scope that records still name cannot vanish
        beneath them.
        """
        if not isinstance(referencing_records, int) or isinstance(
            referencing_records, bool
        ):
            raise TypeError("referencing_records must be an int")
        if referencing_records < 0:
            raise ValueError("referencing_records must not be negative")
        if not self._exists(project_id):
            raise ProjectNotFound(project_id)
        if referencing_records:
            raise ProjectInUse(project_id)
        with self._connection:
            self._connection.execute(
                "DELETE FROM projects WHERE project_id = ?", (project_id,)
            )

    def _exists(self, project_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            is not None
        )


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        project_id=row["project_id"],
        name=row["name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        status=ProjectStatus(row["status"]),
        provenance=provenance_from_storage(
            row["content_origins"],
            row["content_recorded_at"],
            row["content_expires_at"],
            row["mail_references"],
        ),
    )
