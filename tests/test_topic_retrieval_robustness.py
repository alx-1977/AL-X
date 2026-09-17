"""Stage 2A under the conditions fresh-database tests never reach.

Five defects reached external review because every test built a new store in a
temporary directory, on this machine, with this SQLite. That exercises the
happy path and nothing else, so it said nothing about the database Friedl
already has, a runtime without FTS5, or the part numbers and tolerances his
memories are actually full of.

Each class here is one of those conditions:

- an existing database, populated under the previous schema, opened by the new
  store — the index must be filled from the memories already there;
- a SQLite without FTS5 — memory must work and topic retrieval must say
  plainly that it cannot, rather than answering emptily;
- engineering content — `13.56 MHz` and `1/3 oz` must find what they name;
- the model-facing schema — what the runtime contract accepts, the decision
  contract must be able to express, and what it refuses must be unreachable.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    MAX_MEMORY_RETRIEVAL_LIMIT,
    ConversationOrigin,
    ConversationTurn,
    MemoryKind,
    MemoryMatchReason,
    MemoryProposal,
    MemoryQuery,
    ModelCompletion,
    ReasoningContext,
)
from alx.memories import SQLiteMemoryStore, TopicRetrievalUnavailable  # noqa: E402
from alx.memories import store as memory_store  # noqa: E402

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
FACTUAL = (MemoryKind.FACTUAL,)

# Distinguishes "this field is absent from the response" from "this field is
# present and null", which are different things to the parser.
_OMITTED = object()


def legacy_database(path: Path, memories: dict[str, str]) -> None:
    """A database as the previous authoritative schema wrote it.

    Built by hand rather than by an older copy of the store, so the test keeps
    describing the shape Friedl's file is actually in even after the store
    moves on.
    """
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, kind TEXT NOT "
        "NULL, person_id TEXT, supersedes_memory_id TEXT, retention_until "
        "TEXT NOT NULL, scope TEXT)"
    )
    connection.execute(
        "CREATE TABLE memory_revisions (memory_id TEXT NOT NULL REFERENCES "
        "memories(memory_id) ON DELETE CASCADE, revision INTEGER NOT NULL, "
        "revision_json TEXT NOT NULL, content_origins TEXT, "
        "content_recorded_at TEXT, content_expires_at TEXT, mail_references "
        "TEXT, PRIMARY KEY(memory_id, revision))"
    )
    for memory_id, content in memories.items():
        connection.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?)",
            (memory_id, "factual", None, None, RETENTION.isoformat(), None),
        )
        connection.execute(
            "INSERT INTO memory_revisions(memory_id, revision, revision_json) "
            "VALUES (?, ?, ?)",
            (
                memory_id,
                1,
                json.dumps(
                    {
                        "revision": 1,
                        "content": content,
                        "source_references": [f"turn:{memory_id}"],
                        "recorded_at": NOW.isoformat(),
                        "reason": None,
                        "meaning": None,
                    }
                ),
            ),
        )
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()


class ExistingDatabaseTests(unittest.TestCase):
    """An index added to a populated database must be filled from it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memories.sqlite3"
        legacy_database(
            self.path,
            {
                "antenna": "the PN532 antenna matched at 13.56 MHz",
                "copper": "we specified 1/3 oz copper weight",
            },
        )

    def open(self) -> SQLiteMemoryStore:
        store = SQLiteMemoryStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_memories_written_before_the_index_are_findable_by_topic(self) -> None:
        """The defect: search that knows nothing about anything remembered.

        The table was created empty and filled only by later writes, so every
        memory formed before Stage 2A loaded perfectly by identifier and was
        invisible to topic retrieval — a confident empty answer about the whole
        of what she already knew.
        """
        store = self.open()
        found = store.retrieve(MemoryQuery("q", FACTUAL, topic="antenna"), NOW)
        self.assertEqual([item.memory_id for item in found], ["antenna"])

    def test_existing_content_is_not_rewritten(self) -> None:
        store = self.open()
        self.assertEqual(
            store.load("antenna").current.content,
            "the PN532 antenna matched at 13.56 MHz",
        )
        self.assertEqual(
            store.load("copper").current.content, "we specified 1/3 oz copper weight"
        )

    def test_exact_retrieval_is_unchanged_by_migration(self) -> None:
        store = self.open()
        found = store.retrieve(MemoryQuery("q", FACTUAL, memory_ids=("copper",)), NOW)
        self.assertEqual([item.memory_id for item in found], ["copper"])

    def test_migration_is_idempotent(self) -> None:
        """Reopening must not rebuild, and must not lose what was indexed."""
        first = SQLiteMemoryStore(self.path)
        first.close()
        store = self.open()
        found = store.retrieve(MemoryQuery("q", FACTUAL, topic="copper"), NOW)
        self.assertEqual([item.memory_id for item in found], ["copper"])

    def test_a_memory_added_after_migration_joins_the_existing_ones(self) -> None:
        store = self.open()
        store.remember(
            MemoryProposal(
                "later", MemoryKind.FACTUAL, "the antenna was re-tuned",
                ("turn:later",), NOW,
            ),
            RETENTION,
        )
        found = store.retrieve(MemoryQuery("q", FACTUAL, topic="antenna"), NOW)
        self.assertEqual(
            sorted(item.memory_id for item in found), ["antenna", "later"]
        )


class NoFtsConnection(sqlite3.Connection):
    """A SQLite build without FTS5, which is a real deployment condition.

    FTS5 is compiled in rather than guaranteed, so refusing the module is a
    truer test than deleting the table: it exercises construction, migration
    and every write path the way an unlucky runtime would.
    """

    def execute(self, sql, *arguments):  # type: ignore[override]
        if "fts5" in sql.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, *arguments)


class WithoutFtsTests(unittest.TestCase):
    """Durable memory does not depend on its search index."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memories.sqlite3"
        real = sqlite3.connect

        def without_fts(*arguments, **keywords):
            keywords.pop("factory", None)
            return real(*arguments, factory=NoFtsConnection, **keywords)

        sqlite3.connect = without_fts
        self.addCleanup(lambda: setattr(sqlite3, "connect", real))
        self.store = SQLiteMemoryStore(self.path)
        self.addCleanup(self.store.close)

    def remember(self, memory_id: str, content: str) -> None:
        self.store.remember(
            MemoryProposal(
                memory_id, MemoryKind.FACTUAL, content, (f"turn:{memory_id}",), NOW
            ),
            RETENTION,
        )

    def test_the_store_opens(self) -> None:
        """Construction must not fail over a missing search capability."""
        self.assertFalse(self.store._topic_index_available)

    def test_remembering_still_works(self) -> None:
        self.remember("a", "the antenna matched at 13.56 MHz")
        self.assertEqual(
            self.store.load("a").current.content, "the antenna matched at 13.56 MHz"
        )

    def test_exact_retrieval_still_works(self) -> None:
        self.remember("a", "the antenna matched")
        found = self.store.retrieve(MemoryQuery("q", FACTUAL, memory_ids=("a",)), NOW)
        self.assertEqual([item.memory_id for item in found], ["a"])

    def test_scope_retrieval_still_works(self) -> None:
        self.remember("a", "the antenna matched")
        found = self.store.retrieve(
            MemoryQuery("q", FACTUAL, formed_after=NOW - timedelta(days=1)), NOW
        )
        self.assertEqual([item.memory_id for item in found], ["a"])

    def test_correcting_and_deleting_still_work(self) -> None:
        from alx.contracts import MemoryCorrection

        self.remember("a", "the antenna matched")
        self.store.correct(
            "a", MemoryCorrection("the antenna was re-tuned", "measured again", ("turn:2",), NOW), 1
        )
        self.assertEqual(self.store.load("a").current.content, "the antenna was re-tuned")
        self.store.delete("a", 2)

    def test_purging_still_works(self) -> None:
        self.remember("a", "the antenna matched")
        self.assertEqual(
            self.store.purge_expired(RETENTION + timedelta(days=1)), ("a",)
        )

    def test_a_topic_retrieval_says_what_is_wrong(self) -> None:
        """Neither quiet answer is true, so neither is given.

        An empty result would claim nothing matched; the unranked eligible set
        would claim these are what she asked about. The capability is missing,
        and that is what gets reported.
        """
        self.remember("a", "the antenna matched")
        with self.assertRaises(TopicRetrievalUnavailable):
            self.store.retrieve(MemoryQuery("q", FACTUAL, topic="antenna"), NOW)

    def test_the_memories_survive(self) -> None:
        self.remember("a", "the antenna matched at 13.56 MHz")
        with self.assertRaises(TopicRetrievalUnavailable):
            self.store.retrieve(MemoryQuery("q1", FACTUAL, topic="antenna"), NOW)
        self.assertEqual(
            self.store.load("a").current.content, "the antenna matched at 13.56 MHz"
        )


class EngineeringTopicTests(unittest.TestCase):
    """Topics in the domain AL/X actually works in.

    Stripping punctuation turned `13.56 MHz` into `1356` and `1/3 oz` into
    `13`, which matched nothing. These search realistic memory content rather
    than asserting on tokenizer output, because what matters is whether she
    finds the memory, not what the query string looked like on the way.
    """

    CONTENT = {
        "frequency": "the PN532 antenna matched at 13.56 MHz after tuning",
        "copper": "we specified 1/3 oz copper weight for the inner layers",
        "divider": "the divider uses a 4.7 kΩ resistor to ground",
        "shunt": "current sense is a 0.47 ohm shunt",
        "rail": "the 3V3 rail droops under load",
        "gauge": "the MAX17048 fuel gauge reports state of charge",
        "charger": "the MP2723 charger handles the input path",
        "bus": "the I2C bus runs at 400 kHz",
        "firmware": "firmware v1.2.3 fixed the reset loop",
    }

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteMemoryStore(Path(self.directory.name) / "m.sqlite3")
        self.addCleanup(self.store.close)
        for memory_id, content in self.CONTENT.items():
            self.store.remember(
                MemoryProposal(
                    memory_id, MemoryKind.FACTUAL, content,
                    (f"turn:{memory_id}",), NOW,
                ),
                RETENTION,
            )

    def found(self, topic: str) -> list[str]:
        return [
            item.memory_id
            for item in self.store.retrieve(MemoryQuery("q", FACTUAL, topic=topic), NOW)
        ]

    def test_engineering_topics_find_their_memory(self) -> None:
        for topic, expected in (
            ("13.56 MHz", "frequency"),
            ("1/3 oz copper", "copper"),
            ("4.7 kΩ", "divider"),
            ("0.47 ohm", "shunt"),
            ("3V3", "rail"),
            ("MAX17048", "gauge"),
            ("MP2723", "charger"),
            ("I2C", "bus"),
            ("v1.2.3", "firmware"),
            ("PN532", "frequency"),
        ):
            with self.subTest(topic=topic):
                self.assertIn(expected, self.found(topic))

    def test_a_decimal_is_not_run_together(self) -> None:
        """`13.56` must not become `1356`, which is in no memory."""
        self.assertEqual(self.found("13.56"), ["frequency"])

    def test_a_fraction_is_not_truncated(self) -> None:
        """`1/3` must not become `13`."""
        self.assertEqual(self.found("1/3"), ["copper"])

    def test_a_near_miss_is_not_returned(self) -> None:
        """Precision must survive: 0.47 and 4.7 are different components."""
        self.assertEqual(self.found("0.47 ohm"), ["shunt"])

    def test_search_syntax_remains_inert(self) -> None:
        """Preserving punctuation must not let FTS5 syntax through."""
        for topic in (
            "antenna OR copper", "antenna*", 'antenna"', "antenna NEAR copper",
            "^antenna", "(antenna)",
        ):
            with self.subTest(topic=topic):
                # Either nothing or the antenna memory; never the copper one,
                # which only an OR would reach.
                self.assertNotIn("copper", self.found(topic))

    def test_pure_punctuation_matches_nothing_without_failing(self) -> None:
        for topic in ("---", "...", "///"):
            with self.subTest(topic=topic):
                self.assertEqual(self.found(topic), [])


class ModelContractAlignmentTests(unittest.TestCase):
    """What the contract accepts, the decision protocol must express.

    A combination the runtime allows but the model cannot emit is a capability
    AL/X does not have. A value the model can emit but the runtime refuses is a
    turn that dies — which is the failure Stage 2A exists to stop, so it must
    not be reintroduced by the schema.
    """

    def schema(self) -> dict:
        from alx.core.model_reasoner import decision_schema

        return decision_schema()

    def memory_action(self) -> dict:
        for variant in self.schema()["properties"]["action"]["anyOf"]:
            if variant["properties"]["type"].get("const") == "retrieve_memories":
                return variant
        raise AssertionError("the retrieval action is missing from the schema")

    def test_the_limit_is_bounded_in_the_schema(self) -> None:
        """Unbounded, a model emitting 1000 ends the turn on a ValueError."""
        field = self.memory_action()["properties"]["memory_limit"]
        self.assertEqual(field["minimum"], 1)
        self.assertEqual(field["maximum"], MAX_MEMORY_RETRIEVAL_LIMIT)

    def test_the_schema_bound_matches_the_runtime_contract(self) -> None:
        """One number, stated twice, must not drift apart."""
        field = self.memory_action()["properties"]["memory_limit"]
        MemoryQuery("q", FACTUAL, topic="x", limit=field["maximum"])
        with self.assertRaises(ValueError):
            MemoryQuery("q", FACTUAL, topic="x", limit=field["maximum"] + 1)
        with self.assertRaises(ValueError):
            MemoryQuery("q", FACTUAL, topic="x", limit=field["minimum"] - 1)

    def test_the_new_fields_are_expressible(self) -> None:
        properties = self.memory_action()["properties"]
        for name in ("memory_topic", "memory_project_id", "memory_limit"):
            with self.subTest(field=name):
                self.assertIn(name, properties)

    def test_topic_and_project_are_nullable(self) -> None:
        """Omitting them means unspecified, not an invalid decision."""
        properties = self.memory_action()["properties"]
        for name in ("memory_topic", "memory_project_id"):
            with self.subTest(field=name):
                self.assertIn("null", properties[name]["type"])

    def test_every_contract_combination_is_expressible(self) -> None:
        """Expressibility, not authorisation: each of these is legal today."""
        relationship = (MemoryKind.RELATIONSHIP,)
        for name, keywords in (
            ("topic only", dict(kinds=FACTUAL, topic="antenna")),
            ("topic + project", dict(kinds=FACTUAL, topic="antenna", project_id="p1")),
            ("topic + person", dict(kinds=relationship, person_id="friedl", topic="lamp")),
            (
                "topic + person + other kind",
                dict(
                    kinds=relationship + FACTUAL, person_id="friedl", topic="lamp"
                ),
            ),
            ("topic + date", dict(kinds=FACTUAL, topic="antenna", formed_after=NOW)),
            (
                "topic + source",
                dict(kinds=FACTUAL, topic="antenna", source_references=("turn:1",)),
            ),
            ("topic + ids", dict(kinds=FACTUAL, topic="antenna", memory_ids=("a",))),
            (
                "topic + project + person",
                dict(
                    kinds=relationship, person_id="friedl", topic="lamp",
                    project_id="p1",
                ),
            ),
        ):
            with self.subTest(combination=name):
                query = MemoryQuery("q", **keywords)
                self.assertEqual(query.topic, keywords["topic"])

    def parsed(self, **action_fields: object) -> MemoryQuery:
        """The `MemoryQuery` the production parser builds from one response.

        Driven through `ModelReasoner.decide` rather than asserted against a
        dictionary of the test's own making. A test that reads its own literal
        back cannot fail: it would keep passing if the parser returned to
        indexing these fields directly, which is the defect it exists to catch.

        `decide` also converts `KeyError` into `DecisionValidationError`, so a
        field the parser insists on and a provider omits ends the turn. Going
        through the real path is what makes that visible here.
        """
        from alx.core.model_reasoner import ModelReasoner

        action = {
            "type": "retrieve_memories",
            "memory_query_id": "q1",
            "memory_kinds": ["factual"],
            "memory_ids": [],
            "memory_person_id": None,
            "memory_formed_after": None,
            "memory_formed_before": None,
            "memory_source_references": [],
            "memory_source_match": "any",
            "memory_include_superseded": False,
            "memory_topic": "antenna",
            "memory_project_id": None,
            "memory_limit": MAX_MEMORY_RETRIEVAL_LIMIT,
        }
        for name, value in action_fields.items():
            if value is _OMITTED:
                action.pop(name, None)
            else:
                action[name] = value

        class Model:
            def complete(self, request):
                return ModelCompletion(
                    provider="test",
                    model="test",
                    output={
                        "goal_id": None,
                        "action": action,
                        "goal_update": None,
                        "memory_proposals": [],
                    },
                )

        decision = ModelReasoner(
            Model(), "The approved Laws.", "The identity context."
        ).decide(
            ReasoningContext(
                active_goal=None,
                turns=(
                    ConversationTurn(
                        "c1", "t1", ConversationOrigin.TYPED,
                        "what do we know?", NOW, "friedl",
                    ),
                ),
                capabilities=(),
                conversation_id="c1",
            )
        )
        assert decision.memory_query is not None
        return decision.memory_query

    def test_omitted_optional_fields_do_not_end_the_turn(self) -> None:
        """A provider dropping a null field must not end the turn.

        The fields are required by the schema, but a provider that omits a null
        one, or a response shaped before these existed, must mean "unspecified"
        rather than `DecisionValidationError`.
        """
        query = self.parsed(memory_project_id=_OMITTED, memory_limit=_OMITTED)
        self.assertEqual(query.topic, "antenna")
        self.assertIsNone(query.project_id)
        self.assertEqual(query.limit, MAX_MEMORY_RETRIEVAL_LIMIT)

    def test_an_explicit_null_scope_field_is_unspecified(self) -> None:
        """Present-and-null is a different path from absent.

        A provider that emits `"memory_project_id": null` and one that leaves
        the key out reach the parser as different dictionaries, so covering
        omission alone protects only half of what the schema permits. Both of
        these fields declare `["string", "null"]`, so null is contractual here
        rather than merely tolerated.
        """
        query = self.parsed(memory_project_id=None)
        self.assertIsNone(query.project_id)
        self.assertEqual(query.topic, "antenna")

    def test_an_explicit_null_topic_is_unspecified(self) -> None:
        query = self.parsed(memory_topic=None, memory_ids=["a"])
        self.assertIsNone(query.topic)
        self.assertEqual(query.memory_ids, ("a",))

    def test_an_explicit_null_limit_falls_back_to_the_bound(self) -> None:
        """Defensive tolerance, deliberately not a claim about the contract.

        `memory_limit` declares `integer` and not `["integer", "null"]`, so a
        null is outside what the schema allows and this asserts only that the
        parser survives one rather than that emitting one is legal. Widening
        the schema to make null contractual would loosen a bound that exists to
        stop an out-of-range value ending the turn, which is the opposite of
        what this field is for.
        """
        self.assertEqual(
            self.parsed(memory_limit=None).limit, MAX_MEMORY_RETRIEVAL_LIMIT
        )

    def test_explicit_null_and_omission_agree(self) -> None:
        """The two shapes of "unspecified" must not mean different things."""
        explicit = self.parsed(memory_project_id=None, memory_limit=None)
        omitted = self.parsed(memory_project_id=_OMITTED, memory_limit=_OMITTED)
        self.assertEqual(explicit.project_id, omitted.project_id)
        self.assertEqual(explicit.limit, omitted.limit)

    def test_the_schema_says_which_fields_may_be_null(self) -> None:
        """Null is tested where the schema permits it, and not invented where
        it does not."""
        properties = self.memory_action()["properties"]
        for name in ("memory_topic", "memory_project_id"):
            with self.subTest(field=name):
                self.assertIn("null", properties[name]["type"])
        self.assertEqual(properties["memory_limit"]["type"], "integer")

    def test_an_omitted_topic_is_unspecified_rather_than_fatal(self) -> None:
        query = self.parsed(
            memory_topic=_OMITTED, memory_ids=["a"],
        )
        self.assertIsNone(query.topic)
        self.assertEqual(query.memory_ids, ("a",))

    def test_every_combination_survives_production_parsing(self) -> None:
        """What the contract accepts, the real parser must actually produce."""
        for name, fields, check in (
            (
                "topic only",
                {},
                lambda q: (q.topic, q.project_id) == ("antenna", None),
            ),
            (
                "topic + project",
                {"memory_project_id": "pn532"},
                lambda q: q.project_id == "pn532",
            ),
            (
                "topic + person",
                {"memory_kinds": ["relationship"], "memory_person_id": "friedl"},
                lambda q: q.person_id == "friedl",
            ),
            (
                "topic + date",
                {"memory_formed_after": NOW.isoformat()},
                lambda q: q.formed_after == NOW,
            ),
            (
                "topic + source",
                {"memory_source_references": ["turn:1"]},
                lambda q: q.source_references == ("turn:1",),
            ),
            (
                "topic + kind",
                {"memory_kinds": ["factual", "autobiographical"]},
                lambda q: len(q.kinds) == 2,
            ),
            (
                "topic + project + person",
                {
                    "memory_kinds": ["relationship"],
                    "memory_person_id": "friedl",
                    "memory_project_id": "pn532",
                },
                lambda q: (q.person_id, q.project_id) == ("friedl", "pn532"),
            ),
            (
                "topic + ids",
                {"memory_ids": ["a"]},
                lambda q: q.memory_ids == ("a",),
            ),
        ):
            with self.subTest(combination=name):
                query = self.parsed(**fields)
                self.assertEqual(query.topic, "antenna")
                self.assertTrue(check(query), f"{name} did not parse as expected")

    def test_the_bounded_limit_reaches_the_contract(self) -> None:
        self.assertEqual(self.parsed(memory_limit=3).limit, 3)


if __name__ == "__main__":
    unittest.main()
