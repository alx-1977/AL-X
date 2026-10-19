# Goal content retention implementation

This records the goal-store correction under D-013 and D-057; it does not amend
either decision. No new capability, authority, or law exception is introduced.

The production outcome is retained goal content available through the existing
`SQLiteGoalStore`. `_goal_provenance` owns its initial deadline: the earlier of
the goal's `retention_until` and thirty days after its own write, bounded below
by that write timestamp. Source origins and mail references survive, but source
deadlines do not determine a goal's deadline. Non-mail goals have no separate
mail-content expiry. Introducing mail on an update starts the mail-content clock
at that update, bounded by the existing goal retention horizon. Replacements of
an already mail-derived goal preserve its clock, including when newer or older
source mail is added. Rewriting does not renew a logical record.

Both `create` and `_replace_rows` (including memory-batch replacements) enforce
these semantics. The superseded path was accepting caller/source deadlines on
creation and replacement. Those assignments are removed; incoming deadlines
neither shorten nor renew an existing goal. The shared `RetentionPolicy.derive`
still describes source provenance for other record types; it cannot write goals.
No Core, frontend, tool registration, self-wake, or conversation path is added.
Raw language continues through the existing Core; the store operates on
structured provenance and timestamps only.

Schema version 10 adds nullable `created_at`, populated on new goals. Its migration
repairs only deadlines earlier than creation, using the same `_goal_provenance`
calculation. For legacy rows, the migration first consults the existing sibling
`trace.sqlite3` read-only. Only Core's explicit `goals` / `completed` /
`Goal created` records establish creation. The earliest timezone-aware timestamp
is used, and the eight-character reference must identify exactly one stored
goal. Missing or unreadable traces, invalid timestamps, unrelated events and
colliding references leave creation unknown. Neither `updated_at` nor
`content_recorded_at` establishes creation; unknown rows stay unchanged.
For a deadline proven to predate creation, the repair uses the later of creation
and the existing provenance record timestamp, never migration execution time.
It fills verified missing creation times and repairs provenance columns,
retaining revisions, state, update and finish times, associations, and scopes.
Valid deadlines remain untouched. Restarting
again leaves the repair stable; elapsed retention remains elapsed. Migration
runs before any retrieval can filter out an invalid row.

`read_history` (text, exact id and prefix) and `list_recent_finished` share
`RETAINED_GOAL`: both goal retention and content expiry must be in the future.
SQLite compares parsed instants so supported ISO offsets and `Z` timestamps
have the same meaning. Other goal queries do not filter on content expiry;
retention purge and the retention inventory consume the persisted goal facts.

Regression coverage is in `tests/test_finished_goal_history.py`: old mail on a
new completed goal, invalid stored deadlines, legacy repair after real expiry,
restart stability, unchanged valid records, non-mail origins, mail introduction
and subsequent updates, timezone-equivalent history deadlines, and actual
schema-9 upgrades using persisted creation traces. Update-only, invalid,
unrelated and ambiguous traces cannot revive expired goals. The existing
retention wiring tests use explicit store clocks and assert goal-owned deadlines
alongside unchanged source provenance for memory and response records.

The supplied top-level `goals.sqlite3` contained no goals table. No production
database was modified or inspected under `.alx`; verification uses temporary
SQLite stores. AL/X must run the applicable checks and review the final revision
before committing or merging.
