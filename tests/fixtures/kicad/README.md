# Real KiCad test input

`seed.kicad_pcb` is a hand-authored legacy-format seed: a 10 mm square,
two copper layers, and no components. It is **not** a KiCad-saved fixture.

`tests/test_kicad_real.py` generates the actual test board in temporary storage
with the installed KiCad CLI (KiCad 9 or later, providing zone-refill DRC):

```
kicad-cli pcb upgrade --output <temporary-project>/minimal.kicad_pcb tests/fixtures/kicad/seed.kicad_pcb
```

Only that successfully saved/upgraded board is passed to the provider. An empty
project settings object supplies KiCad defaults; it is also not represented as
KiCad-generated. The test invokes production DRC and SVG export, with real
subprocesses and no mocked reports. It skips only when `kicad-cli` is absent.
With a CLI installed, upgrade, flag, format, parsing, or rendering errors fail.

Run `python3 -m unittest discover -s tests -p test_kicad_real.py`.

Provenance limitation: no KiCad executable was available in the implementation
checkout. The upgrade/test has not been run here, and no saved project or captured
DRC report is committed. A verified, committed KiCad-saved fixture remains
outstanding; this seed and test do not claim to provide that evidence.
