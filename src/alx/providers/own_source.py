"""Bounded source inspection through Git objects only (D-057).

Tree entries supply blob identities and literal search paths, after the
shared deny policy has been applied.
"""

from typing import Any, Mapping

MAX_BLOB_BYTES = 1_048_576
MAX_TREE_BYTES = 8_388_608
MAX_OUTPUT_CHARS = 32_000
MAX_MATCHES = 200


class SourceError(Exception):
    pass


def validate_path(path: str) -> str:
    if (not path or len(path) > 4096 or path.startswith("/") or "\\" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in ("", ".", "..") for part in path.split("/"))
            or ":" in path):
        raise SourceError("arguments_unusable")
    return path


def denied(path: str) -> bool:
    parts = path.casefold().split("/")
    # Treat credential-shaped directory names as protected too.
    return any(
        part in (".alx", ".env", ".netrc", ".npmrc")
        or part in ("id_rsa", "id_rsa_sk", "id_dsa", "id_dsa_sk", "id_ecdsa", "id_ecdsa_sk",
                    "id_ed25519", "id_ed25519_sk")
        or part.startswith((".env.", "id_rsa"))
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        or any(word in part for word in ("secret", "credential", "token", "password"))
        for part in parts
    )


class OwnSource:
    def __init__(self, authority: Any, running_commit: str):
        self.authority = authority
        self.commit = running_commit

    def _git(self, *args: str, bound: int) -> bytes:
        return self.authority.read_source_object(*args, bound=bound)

    def inspect(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        operation = arguments["operation"]
        if operation not in ("search", "read"):
            raise SourceError("arguments_unusable")
        allowed = ({"operation", "path", "start_line", "end_line"} if operation == "read"
                   else {"operation", "query", "ignore_case", "path_prefix", "limit"})
        if set(arguments) - allowed:
            raise SourceError("arguments_unusable")
        path = validate_path(arguments.get("path", "")) if operation == "read" else ""
        prefix = arguments.get("path_prefix", "")
        if prefix:
            validate_path(prefix)
        if denied(path or prefix):
            raise SourceError("path_denied")
        start, end = arguments.get("start_line", 1), arguments.get("end_line")
        limit = arguments.get("limit", 50)
        query = arguments.get("query", "")
        if (start < 1 or (end is not None and end < start)
                or not 1 <= limit <= MAX_MATCHES
                or (operation == "search" and (not query or len(query) > 4096))):
            raise SourceError("arguments_unusable")
        if (len(self.commit) not in (40, 64)
                or any(char not in "0123456789abcdefABCDEF" for char in self.commit)):
            raise SourceError("unavailable")
        if self._git("cat-file", "-t", self.commit, bound=32).strip() != b"commit":
            raise SourceError("unavailable")
        if operation == "search":
            return self._search(query, prefix, arguments.get("ignore_case", False), limit)
        tree = self._git("ls-tree", "-r", "-z", self.commit, bound=MAX_TREE_BYTES)
        values: dict[str, Any] = {"running_commit": self.commit, "truncated": False}
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            try:
                name = raw_name.decode("utf-8")
                validate_path(name)
            except (UnicodeError, SourceError):
                continue
            if denied(name):
                continue
            if operation == "read" and name != path:
                continue
            if prefix and not (name == prefix or name.startswith(prefix + "/")):
                continue
            mode, kind, oid = metadata.split()
            if mode not in (b"100644", b"100755") or kind != b"blob":
                if operation == "read":
                    raise SourceError("unsupported_file")
                continue
            size = int(self._git("cat-file", "-s", oid.decode("ascii"), bound=32))
            if size > MAX_BLOB_BYTES:
                if operation == "read":
                    raise SourceError("source_too_large")
                values["truncated"] = True
                continue
            blob = self._git("cat-file", "blob", oid.decode("ascii"), bound=MAX_BLOB_BYTES)
            try:
                if b"\0" in blob:
                    raise UnicodeError()
                content = blob.decode("utf-8")
            except UnicodeError:
                if operation == "read":
                    raise SourceError("binary_file")
                continue
            # Git source line numbers count LF, not Unicode separators or
            # form feeds that may occur inside a literal source line.
            parts = content.split("\n")
            lines = [part + "\n" for part in parts[:-1]]
            if parts[-1]:
                lines.append(parts[-1])
            if operation == "read":
                selected = "".join(lines[start - 1:end])
                values.update(path=name, start_line=start, total_lines=len(lines),
                              content=selected[:MAX_OUTPUT_CHARS],
                              truncated=len(selected) > MAX_OUTPUT_CHARS)
                return values
        raise SourceError("file_not_found")

    def _search(self, query: str, prefix: str, ignore_case: bool, limit: int) -> dict[str, Any]:
        tree = self._git("ls-tree", "-r", "-l", "-z", self.commit, bound=MAX_TREE_BYTES)
        values: dict[str, Any] = {"running_commit": self.commit, "truncated": False, "matches": []}
        paths = []
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            try:
                name = raw_name.decode("utf-8")
                validate_path(name)
            except (UnicodeError, SourceError):
                continue
            if denied(name) or (prefix and not (name == prefix or name.startswith(prefix + "/"))):
                continue
            mode, kind, oid, size = metadata.split()
            if mode not in (b"100644", b"100755") or kind != b"blob":
                continue
            if int(size) > MAX_BLOB_BYTES:
                values["truncated"] = True
                continue
            paths.append(name)
        # A query containing LF cannot occur within a source line. Git would
        # otherwise treat its parts as separate patterns, even with -F.
        if not paths or any(ord(char) in (0, 10) for char in query):
            return values
        output, truncated = self.authority.search_source(
            self.commit, query, tuple(paths), ignore_case=ignore_case,
            limit=limit, bound=MAX_OUTPUT_CHARS + limit * (len(self.commit) + 32))
        values["truncated"] |= truncated
        eligible = set(paths)
        records = []
        if output and not output.endswith("\n"):
            raise SourceError("unavailable")
        for segment in output.split("\n")[:-1]:
            fields = segment.split("\0", 2)
            if len(fields) != 3:
                if not records or "\0" in segment:
                    raise SourceError("unavailable")
                # The text runner translates lone CRs into LF. Segments
                # without a record prefix continue the preceding Git line.
                records[-1][2] += "\r" + segment
                records[-1][3] = True
                continue
            filename, number, line = fields
            if not filename.startswith(self.commit + ":"):
                raise SourceError("unavailable")
            name = filename[len(self.commit) + 1:]
            if name not in eligible or not number.isdecimal() or int(number) < 1:
                raise SourceError("unavailable")
            records.append([name, int(number), line.rstrip("\r"), False])
        used = 0
        needle = query.casefold() if ignore_case else query
        for name, number, line, continued in records:
            if continued:
                # Restore the CR at the end of the continued CRLF line too;
                # the runner collapsed that pair into the record's final LF.
                line += "\r"
            try:
                line.encode("utf-8")
            except UnicodeError:
                continue
            if needle not in (line.casefold() if ignore_case else line):
                continue
            cost = len(name) + len(line) + 64
            if len(values["matches"]) == limit or used + cost > MAX_OUTPUT_CHARS:
                values["truncated"] = True
                break
            values["matches"].append({"path": name, "line_number": number, "line_text": line})
            used += cost
        return values
