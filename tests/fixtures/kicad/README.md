# KiCad 10.0.7 fixture provenance

Captured locally on 2026-10-19 using the installed macOS KiCad distribution:
`/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli version` → `10.0.7`.

- `minimal.kicad_pcb` was **saved by KiCad 10.0.7**, using
  `kicad-cli pcb upgrade --force minimal.kicad_pcb`. Its origin is the
  retained hand-authored `seed.kicad_pcb`, with one deliberately undersized
  F.SilkS text item added before the final KiCad save. The committed file is
  KiCad's saved output.
- `minimal.kicad_pro` is copied verbatim from that installation's official
  `Contents/SharedSupport/template/kicad.kicad_pro`. It supplies KiCad defaults;
  it is not claimed to be a GUI-saved settings file.
- `drc-10.0.7.json` is the unedited real CLI output of
  `kicad-cli pcb drc --format json --severity-all --refill-zones --output drc-10.0.7.json minimal.kicad_pcb`
  against the committed board/settings. It contains two real warnings:
  text thickness and text height. Its timestamp, version, UUIDs and descriptions
  are retained as emitted.

The captured-report test runs without KiCad. The real test copies the saved
board and official settings into temporary storage and invokes production DRC
and SVG export without mocking subprocesses or report bytes. It requires
KiCad **10 or later** to read this version-10 board and skips only if no CLI is
found (PATH or the standard macOS application location). KiCad 9 compatibility
is not verified by this fixture. Real DRC and both SVG exports passed on 10.0.7.

Run `python -m pytest -q tests/test_kicad.py tests/test_kicad_real.py`.

## Recovery concurrency contract

The provider records each published write's SHA-256 digest. A failed write-back
returns those digests, per-path conflicts, backup paths and remaining changes.
Mismatching, missing, symlinked, non-file and unreadable targets are preserved.
Even matching targets are left in place with `atomic_recovery_unavailable`:
POSIX replace/unlink cannot condition mutation on a content digest, and advisory
locks do not exclude other programs. Automatic restore/delete was removed so a
writer arriving after digest observation cannot lose its edit during recovery.
Backups are retained; recovery remains failed and requires AL/X's judgement.
The existing forward publication still uses validation followed by replacement;
this does not assert filesystem-wide isolation against arbitrary writers.

## SHA-256

- `minimal.kicad_pcb`: `82e68831d1b11eca4f0eb218693d7cb7073503b07730e24392bec6ba165732d5`
- `minimal.kicad_pro`: `15a17e27a30b0284403a4c96c3b73578ded4be8722e3db25d13fda196e81de87`
- `drc-10.0.7.json`: `bd491bb2871ebc63394593497e1a9a1430e8230a7fd33e405a9b75c4ce1faa04`
