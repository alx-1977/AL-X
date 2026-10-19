"""D-057: source evidence belongs to the running commit, never the checkout."""

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import SideEffect, CapabilityResultState
from alx.core.model_reasoner import _catalogue_payload
from alx.providers.own_source import OwnSource, MAX_OUTPUT_CHARS, MAX_BLOB_BYTES
from alx.providers.repository_authority import RepositoryAuthority
from alx.contracts.repository_authority import CanonicalSystem
from alx.tools.own_source import DEFINITION, READ_OWN_SOURCE, build_own_source_executors


class OwnSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-b", "running")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.denied = (
            ".env", ".env.local", "nested/.ENV.PROD", ".alx/data.py",
            "nested/.ALX/data.py", "cert.pem", "CERT.KEY", "cert.p12", "cert.pfx",
            "id_rsa", "nested/id_rsa.pub", ".netrc", ".npmrc", "my_secret.py",
            "credentials.json", "access_token.txt", "passwords.py",
        )
        for name in self.denied:
            self.write(name, "needle private\n")
        self.write("src/main.py", "Running needle\nsecond [literal].*\nTHIRD\n")
        self.write("src/other.py", "needle again\n")
        self.write("src-more.py", "needle outside prefix\n")
        self.write("odd [x]*.py", "needle literal path\n")
        self.write("binary.bin", b"needle\0private")
        self.write("invalid.bin", b"\xff")
        self.write("crlf.py", b"first\r\nsecond\r\n")
        self.write("oversized.txt", b"a" * (MAX_BLOB_BYTES + 1))
        self.write("long.txt", "needle" + "x" * MAX_OUTPUT_CHARS)
        (self.root / "link.py").symlink_to("src/main.py")
        self.git("add", "--all")
        self.git("commit", "-m", "running")
        self.commit = self.git("rev-parse", "HEAD").strip()
        authority = RepositoryAuthority(CanonicalSystem(self.root, "fixture/source"))
        self.source = OwnSource(authority, self.commit)
        self.executors = build_own_source_executors(self.source.inspect, lambda: "source-call")
        # A different branch, committed content, and a dirty working tree.
        # Neither is a source of truth for this process.
        self.git("checkout", "-b", "other")
        self.write("src/main.py", "checkout content\n")
        self.git("add", "src/main.py")
        self.git("commit", "-m", "other")
        self.write("src/main.py", "dirty content\n")

    def git(self, *arguments):
        return subprocess.run(("git", *arguments), cwd=self.root, check=True,
                              capture_output=True, text=True).stdout

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())

    def call(self, **arguments):
        result = self.executors[READ_OWN_SOURCE](arguments)
        if result.state is CapabilityResultState.SUCCEEDED:
            self.assertTrue(DEFINITION.output_schema.accepts(result.values))
        return result

    def success(self, **arguments):
        result = self.call(**arguments)
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values["running_commit"], self.commit)
        return result.values

    def failed(self, code, **arguments):
        result = self.call(**arguments)
        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], code)

    def test_running_commit_isolation_for_read_and_search(self):
        read = self.success(operation="read", path="src/main.py")
        self.assertEqual(read["content"], "Running needle\nsecond [literal].*\nTHIRD\n")
        found = self.success(operation="search", query="Running", path_prefix="src")
        self.assertEqual(list(found["matches"]), [
            {"path": "src/main.py", "line_number": 1, "line_text": "Running needle"}])
        self.assertFalse(self.success(operation="search", query="checkout")["matches"])
        self.assertFalse(self.success(operation="search", query="dirty")["matches"])

    def test_deny_policy_precedes_blob_retrieval_for_every_category(self):
        original = self.source._git
        blobs = []

        def observed(*args, **kwargs):
            if args[:2] == ("cat-file", "blob"):
                blobs.append(args[-1])
            return original(*args, **kwargs)

        with patch.object(self.source, "_git", side_effect=observed):
            for name in self.denied:
                with self.subTest(name=name):
                    self.failed("path_denied", operation="read", path=name)
            self.assertEqual(blobs, [])
            found = self.success(operation="search", query="needle")
            self.assertFalse(set(self.denied) & {item["path"] for item in found["matches"]})
            for name in self.denied:
                oid = self.git("rev-parse", f"{self.commit}:{name}").strip()
                self.assertNotIn(oid, blobs)
        self.failed("path_denied", operation="search", query="needle", path_prefix=".alx")

    def test_invalid_paths_in_both_operations(self):
        for path in ("../src", "src/../main.py", "/etc/passwd", str(self.root / "src/main.py"),
                     "./src", "src//main.py", "src/", "C:/file", "a\\b", "a\0b", ":(glob)*"):
            for key, operation in (("path", "read"), ("path_prefix", "search")):
                with self.subTest(path=path, operation=operation):
                    arguments = {"operation": operation, key: path}
                    if operation == "search":
                        arguments["query"] = "needle"
                    self.failed("arguments_unusable", **arguments)

    def test_literal_case_prefix_and_unusual_file_names(self):
        found = self.success(operation="search", query="[literal].*", path_prefix="src")
        self.assertEqual(found["matches"][0]["line_number"], 2)
        self.assertFalse(self.success(operation="search", query="running")["matches"])
        self.assertEqual(len(self.success(operation="search", query="running", ignore_case=True)["matches"]), 1)
        found = self.success(operation="search", query="needle", path_prefix="src")
        self.assertEqual({m["path"] for m in found["matches"]}, {"src/main.py", "src/other.py"})
        self.assertFalse(found["truncated"])
        self.assertEqual(self.success(operation="read", path="odd [x]*.py")["content"], "needle literal path\n")

    def test_ranges_and_bounds(self):
        self.assertEqual(self.success(operation="read", path="crlf.py")["content"],
                         "first\r\nsecond\r\n")
        read = self.success(operation="read", path="src/main.py", start_line=2, end_line=2)
        self.assertEqual(read["content"], "second [literal].*\n")
        self.assertEqual(read["total_lines"], 3)
        self.assertEqual(self.success(operation="read", path="src/main.py", start_line=8)["content"], "")
        self.failed("arguments_unusable", operation="read", path="src/main.py", start_line=0)
        self.failed("arguments_unusable", operation="read", path="src/main.py", start_line=3, end_line=2)
        read = self.success(operation="read", path="long.txt")
        self.assertEqual(len(read["content"]), MAX_OUTPUT_CHARS)
        self.assertTrue(read["truncated"])
        found = self.success(operation="search", query="needle", path_prefix="src", limit=1)
        self.assertEqual(len(found["matches"]), 1)
        self.assertTrue(found["truncated"])
        self.assertTrue(self.success(operation="search", query="absent")["truncated"])
        self.assertTrue(self.success(operation="search", query="needle", path_prefix="long.txt")["truncated"])
        self.failed("source_too_large", operation="read", path="oversized.txt")

    def test_failures_and_no_commit_fallback(self):
        for path, code in (("link.py", "unsupported_file"), ("binary.bin", "binary_file"),
                           ("invalid.bin", "binary_file"),
                           ("absent.py", "file_not_found")):
            self.failed(code, operation="read", path=path)
        for commit in ("", "HEAD", "a" * 40, self.git("rev-parse", f"{self.commit}:src/main.py").strip()):
            self.source.commit = commit
            self.failed("unavailable", operation="read", path="src/main.py")
        for arguments in ({"operation": "bad"}, {"operation": "search", "query": ""},
                          {"operation": "read"}, {"operation": "search", "query": "a", "limit": True},
                          {"operation": "search", "query": "a", "limit": 201},
                          {"operation": "read", "path": "src/main.py", "query": "a"}):
            self.failed("arguments_unusable", **arguments)


class OwnSourceRegistrationTests(unittest.TestCase):
    def test_catalogue_and_single_plain_permission_registration(self):
        self.assertIs(DEFINITION.side_effect, SideEffect.NONE)
        self.assertIn(READ_OWN_SOURCE, _catalogue_payload((DEFINITION,)))
        root = Path(__file__).resolve().parents[1]
        source = (root / "src/alx/bootstrap/live_voice.py").read_text()
        tree = ast.parse(source)
        registrations = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name) and node.func.id == "register"
                         and node.args and "OWN_SOURCE_DEFINITION" in ast.unparse(node.args[0])]
        self.assertEqual(len(registrations), 1)
        self.assertIn('frozenset({"self.read"}), approval_required=False', source)
        self.assertIn('OwnSource(repository_runtime.authority, running_commit).inspect', source)
        registration = registrations[0]
        guard = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                     and registration in tuple(ast.walk(node)))
        self.assertEqual(ast.unparse(guard.test), 'repository_runtime is not None')
        self.assertEqual(source.count('running_commit = running_commit_of('), 1)


class OwnSourceAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.oid = "a" * 40
        self.calls = []
        self.blob = b"needle\r\ninvalid\xff\0"

        def runner(argv, **kwargs):
            self.calls.append((argv, kwargs))
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
            if argv[2:4] == ["cat-file", "-s"]:
                output = str(len(self.blob))
            elif argv[2:] == ["rev-parse", "--absolute-git-dir"]:
                output = "/fixture/git"
            else:
                self.assertEqual(argv[2:], ["--git-dir=/fixture/git", "unpack-file", self.oid])
                self.temporary = Path(kwargs["cwd"])
                (self.temporary / ".merge_file_fixture").write_bytes(self.blob)
                output = ".merge_file_fixture\n"
            return SimpleNamespace(returncode=0, stdout=output)

        self.authority = RepositoryAuthority(
            CanonicalSystem(Path("/fixture"), "fixture/source"), runner=runner)

    def test_source_delegates_to_authority_and_preserves_exact_bounded_bytes(self):
        source = OwnSource(self.authority, self.oid)
        self.assertEqual(source._git("cat-file", "blob", self.oid, bound=100), self.blob)
        self.assertFalse(self.temporary.exists())
        self.assertEqual(len(self.calls), 3)

    def test_oversized_blob_is_refused_before_materializing_it(self):
        with self.assertRaisesRegex(ValueError, "source_too_large"):
            self.authority.read_source_object("cat-file", "blob", self.oid, bound=2)
        self.assertEqual(len(self.calls), 1)

    def test_only_enumerated_object_reads_can_reach_the_runner(self):
        for args in (("show", self.oid), ("cat-file", "blob", "HEAD"),
                     ("cat-file", "blob", "../path"), ("reset", self.oid)):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "unavailable"):
                self.authority.read_source_object(*args, bound=100)
        self.assertFalse(self.calls)

    def test_tree_listing_checks_sizes_before_each_tree_and_bounds_output(self):
        root_tree, child_tree, blob = "b" * 40, "c" * 40, "d" * 40
        responses = {
            ("rev-parse", "--verify", f"{self.oid}^{{tree}}"): root_tree,
            ("cat-file", "-s", root_tree): "40",
            ("cat-file", "-s", child_tree): "40",
            ("ls-tree", "-z", root_tree): f"040000 tree {child_tree}\tsrc\0",
            ("ls-tree", "-z", child_tree): f"100644 blob {blob}\tfile.py\0",
        }
        seen = []

        def runner(argv, **kwargs):
            key = tuple(argv[2:])
            seen.append(key)
            return SimpleNamespace(returncode=0, stdout=responses[key])

        self.authority._runner = runner
        result = self.authority.read_source_object("ls-tree", "-r", "-z", self.oid, bound=100)
        self.assertEqual(result, f"100644 blob {blob}\tsrc/file.py\0".encode())
        self.assertLess(seen.index(("cat-file", "-s", child_tree)),
                        seen.index(("ls-tree", "-z", child_tree)))
        seen.clear()
        with self.assertRaisesRegex(ValueError, "source_too_large"):
            self.authority.read_source_object("ls-tree", "-r", "-z", self.oid, bound=60)
        self.assertNotIn(("ls-tree", "-z", child_tree), seen)

    def test_blob_temporary_output_is_removed_after_command_failure(self):
        directories = []

        def runner(argv, **kwargs):
            if argv[2:4] == ["cat-file", "-s"]:
                return SimpleNamespace(returncode=0, stdout="1")
            if argv[2] == "rev-parse":
                return SimpleNamespace(returncode=0, stdout="/fixture/git")
            directories.append(Path(kwargs["cwd"]))
            return SimpleNamespace(returncode=1, stdout="")

        self.authority._runner = runner
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.authority.read_source_object("cat-file", "blob", self.oid, bound=100)
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())


class OwnSourceObjectTests(unittest.TestCase):
    """Exercise policy and result reduction independently of a Git installation."""

    def setUp(self):
        self.commit = "a" * 40
        self.source = OwnSource(None, self.commit)
        self.entries = {
            "src/file.py": (b"100644", b"First needle\nSecond NEEDLE\n"),
            "src/file2.py": (b"100755", b"needle\n"),
            "src-more.py": (b"100644", b"needle\n"),
            "[literal]*.py": (b"100644", b"[literal].*\n"),
            "binary.bin": (b"100644", b"needle\0"),
            "link.py": (b"120000", b"src/file.py"),
            "long.py": (b"100644", b"needle" + b"x" * MAX_OUTPUT_CHARS),
            "huge.py": (b"100644", b"x" * (MAX_BLOB_BYTES + 1)),
        }
        protected = (".env", "a/.env.test", ".ALX/file.py", "a.pem", "a.key", "a.p12",
                     "a.pfx", "id_rsa.pub", ".netrc", ".npmrc", "secret.txt",
                     "credential.txt", "token.txt", "password.txt")
        self.entries.update({name: (b"100644", b"protected") for name in protected})
        self.ids = {name: f"{number:040x}" for number, name in enumerate(self.entries, 1)}
        self.requested = []

        def git(*args, bound):
            if args == ("cat-file", "-t", self.commit):
                return b"commit\n"
            if args == ("ls-tree", "-r", "-z", self.commit):
                return b"".join(mode + b" blob " + self.ids[name].encode() + b"\t"
                                + name.encode() + b"\0" for name, (mode, _) in self.entries.items())
            self.assertEqual(args[0], "cat-file")
            name = next(name for name, oid in self.ids.items() if oid == args[2])
            self.requested.append(name)
            blob = self.entries[name][1]
            if args[1] == "-s":
                return str(len(blob)).encode()
            self.assertEqual(args[1], "blob")
            self.assertLessEqual(len(blob), bound)
            return blob

        self.patch = patch.object(self.source, "_git", side_effect=git)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.execute = build_own_source_executors(self.source.inspect, lambda: "call")[READ_OWN_SOURCE]

    def test_protected_candidates_never_retrieve_size_or_content(self):
        found = self.execute({"operation": "search", "query": "protected"})
        self.assertIs(found.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(found.values["matches"])
        for name in list(self.entries)[8:]:
            result = self.execute({"operation": "read", "path": name})
            self.assertEqual(result.failure["code"], "path_denied")
            self.assertNotIn(name, self.requested)

    def test_search_is_literal_and_prefix_is_a_path_boundary(self):
        result = self.execute({"operation": "search", "query": "NEEDLE", "ignore_case": True,
                               "path_prefix": "src"})
        self.assertTrue(DEFINITION.output_schema.accepts(result.values))
        self.assertEqual([(m["path"], m["line_number"]) for m in result.values["matches"]],
                         [("src/file.py", 1), ("src/file.py", 2), ("src/file2.py", 1)])
        literal = self.execute({"operation": "search", "query": "[literal].*"})
        self.assertEqual(literal.values["matches"][0]["path"], "[literal]*.py")
        case = self.execute({"operation": "search", "query": "NEEDLE", "path_prefix": "src"})
        self.assertEqual(len(case.values["matches"]), 1)

    def test_read_ranges_binary_symlinks_and_bounds(self):
        result = self.execute({"operation": "read", "path": "src/file.py", "start_line": 2, "end_line": 2})
        self.assertEqual(result.values["content"], "Second NEEDLE\n")
        self.assertEqual(result.values["running_commit"], self.commit)
        for name, code in (("binary.bin", "binary_file"), ("link.py", "unsupported_file"),
                           ("huge.py", "source_too_large"), ("missing", "file_not_found")):
            self.assertEqual(self.execute({"operation": "read", "path": name}).failure["code"], code)
        read = self.execute({"operation": "read", "path": "long.py"})
        self.assertEqual(len(read.values["content"]), MAX_OUTPUT_CHARS)
        self.assertTrue(read.values["truncated"])
        search = self.execute({"operation": "search", "query": "needle", "path_prefix": "src", "limit": 1})
        self.assertEqual(len(search.values["matches"]), 1)
        self.assertTrue(search.values["truncated"])
        search = self.execute({"operation": "search", "query": "absent"})
        self.assertTrue(search.values["truncated"])

    def test_invalid_requests_never_invoke_git(self):
        for arguments in ({"operation": "read", "path": "../outside"},
                          {"operation": "read", "path": "/outside"},
                          {"operation": "search", "query": "needle", "path_prefix": "a/../b"},
                          {"operation": "read", "path": "a", "start_line": True},
                          {"operation": "read", "path": "a", "start_line": 3, "end_line": 2},
                          {"operation": "search", "query": ""}):
            self.assertEqual(self.execute(arguments).failure["code"], "arguments_unusable")
        self.assertEqual(self.source._git.call_count, 0)

    def test_line_numbers_count_only_newlines_and_oversized_matches_are_marked(self):
        self.entries["src/file.py"] = (b"100644", b"first\x0bneedle\nnext\n")
        result = self.execute({"operation": "search", "query": "needle", "path_prefix": "src/file.py"})
        self.assertEqual(result.values["matches"][0]["line_number"], 1)
        result = self.execute({"operation": "search", "query": "needle", "path_prefix": "long.py"})
        self.assertFalse(result.values["matches"])
        self.assertTrue(result.values["truncated"])


if __name__ == "__main__":
    unittest.main()
