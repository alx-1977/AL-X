"""3D and SPICE models of a new FireFli part (19 Nov 2026).

PCBs go into Fusion for enclosure design, so every part needs a 3D body; and circuits are
simulated, so a part needs the maker's SPICE model where the maker publishes one.

- 3D: the maker's STEP (from a maker or SamacSys download Friedl drops in Parts-Inbox), its
  size checked against the datasheet, or, without one, a body generated from the datasheet's
  package size and height; a file in the library's 3dmodels folder, which the footprint names
  as the FireFli resistors and capacitors do.
- SPICE: the maker's model file goes in the library and the symbol names it (KiCad's
  Sim.* fields); ngspice must load it before the part counts as done.

Everything here has one correct result (Law 2); which file, which model and which pins
is AL/X's reading of the download and the datasheet (Law 1).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MODEL_SUFFIXES = (".stp", ".step")
SPICE_SUFFIXES = (".lib", ".cir", ".mod", ".sp", ".spi", ".spice", ".txt", ".sub", ".ckt")
MAX_FILE = 50_000_000


# ---------------------------------------------------------------- STEP

def step_extent(step: bytes) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """The bounding box of a STEP model's points, in the file's own axes (mm)."""
    source = step.decode("latin-1", "replace")
    if "MILLI" not in source.upper():
        return None
    points = [tuple(float(v) for v in m) for m in re.findall(
        r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.E+]+)\s*,\s*([-\d.E+]+)\s*,\s*([-\d.E+]+)\s*\)", source)]
    if not points:
        return None
    return (tuple(min(p[i] for p in points) for i in range(3)),
            tuple(max(p[i] for p in points) for i in range(3)))


def box_step(name: str, boxes: list[tuple[float, float, float, float, float, float]]) -> bytes:
    """A STEP (AP214) of axis-aligned solid boxes (x0, y0, z0, x1, y1, z1) in mm, z up: the
    package body and its leads, for enclosure fit."""
    entities: list[str] = []

    def add(text: str) -> int:
        entities.append(text)
        return len(entities)

    def point(x, y, z):
        return add("CARTESIAN_POINT('',(%r,%r,%r))" % (float(x), float(y), float(z)))

    def direction(x, y, z):
        return add("DIRECTION('',(%r,%r,%r))" % (float(x), float(y), float(z)))
    context = add("( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#U)) "
                  "GLOBAL_UNIT_ASSIGNED_CONTEXT((#L,#A,#S)) REPRESENTATION_CONTEXT('','') )")
    length = add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
    angle = add("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
    solid_angle = add("( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )")
    uncertainty = add("UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#%d,'distance_accuracy_value','')"
                      % length)
    entities[context - 1] = entities[context - 1].replace("#U", "#%d" % uncertainty).replace(
        "#L", "#%d" % length).replace("#A", "#%d" % angle).replace("#S", "#%d" % solid_angle)
    solids = []
    for x0, y0, z0, x1, y1, z1 in boxes:
        corners = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        vertices = [add("VERTEX_POINT('',#%d)" % point(*c)) for c in corners]
        edges = {}

        def edge(a, b):
            key = (min(a, b), max(a, b))
            if key not in edges:
                pa, pb = corners[key[0]], corners[key[1]]
                vec = tuple(pb[i] - pa[i] for i in range(3))
                size = max(abs(v) for v in vec)
                line = add("LINE('',#%d,#%d)" % (point(*pa), add("VECTOR('',#%d,%r)" % (
                    direction(*(v / size for v in vec)), float(size)))))
                edges[key] = add("EDGE_CURVE('',#%d,#%d,#%d,.T.)" % (vertices[key[0]], vertices[key[1]], line))
            return edges[key], a < b
        faces = []
        # Each face: its four corners counter-clockwise seen from outside, and its outward normal.
        for loop, normal in (((0, 2, 3, 1), (0, 0, -1)), ((4, 5, 7, 6), (0, 0, 1)), ((0, 1, 5, 4), (0, -1, 0)),
                             ((2, 6, 7, 3), (0, 1, 0)), ((0, 4, 6, 2), (-1, 0, 0)), ((1, 3, 7, 5), (1, 0, 0))):
            oriented = []
            for a, b in zip(loop, loop[1:] + loop[:1]):
                curve, forward = edge(a, b)
                oriented.append(add("ORIENTED_EDGE('',*,*,#%d,%s)" % (curve, ".T." if forward else ".F.")))
            edge_loop = add("EDGE_LOOP('',(%s))" % ",".join("#%d" % o for o in oriented))
            bound = add("FACE_OUTER_BOUND('',#%d,.T.)" % edge_loop)
            ax = add("AXIS2_PLACEMENT_3D('',#%d,#%d,$)" % (point(*corners[loop[0]]), direction(*normal)))
            plane = add("PLANE('',#%d)" % ax)
            faces.append(add("ADVANCED_FACE('',(#%d),#%d,.T.)" % (bound, plane)))
        shell = add("CLOSED_SHELL('',(%s))" % ",".join("#%d" % f for f in faces))
        solids.append(add("MANIFOLD_SOLID_BREP('',#%d)" % shell))
    origin = add("AXIS2_PLACEMENT_3D('',#%d,#%d,#%d)" % (point(0, 0, 0), direction(0, 0, 1), direction(1, 0, 0)))
    shape = add("ADVANCED_BREP_SHAPE_REPRESENTATION('%s',(%s,#%d),#%d)"
                % (name, ",".join("#%d" % s for s in solids), origin, context))
    application = add("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
    add("APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#%d)" % application)
    product_context = add("PRODUCT_CONTEXT('',#%d,'mechanical')" % application)
    product = add("PRODUCT('%s','%s','',(#%d))" % (name, name, product_context))
    formation = add("PRODUCT_DEFINITION_FORMATION('','',#%d)" % product)
    definition_context = add("PRODUCT_DEFINITION_CONTEXT('part definition',#%d,'design')" % application)
    definition = add("PRODUCT_DEFINITION('design','',#%d,#%d)" % (formation, definition_context))
    shape_definition = add("PRODUCT_DEFINITION_SHAPE('','',#%d)" % definition)
    add("SHAPE_DEFINITION_REPRESENTATION(#%d,#%d)" % (shape_definition, shape))
    body = "\n".join("#%d=%s;" % (i, text) for i, text in enumerate(entities, 1))
    header = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('FireFli generated body'),'2;1');\n"
              "FILE_NAME('%s.step','',(''),(''),'AL/X','AL/X','');\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
              "ENDSEC;\nDATA;\n" % name)
    return (header + body + "\nENDSEC;\nEND-ISO-10303-21;\n").encode("ascii")


def body_boxes(spec: dict[str, Any], pads: list[tuple[float, float, float, float]]) -> list[tuple]:
    """The package body (datasheet width, height in the board plane, and its height above the
    board) and, under it, a thin plate for each pad: KiCad's footprint axes (x right, y down)
    become the model's x and -y."""
    width, depth = (float(v) for v in spec["body"])
    tall = float(spec["height"])
    boxes = [(-width / 2, -depth / 2, 0.0, width / 2, depth / 2, tall)]
    for x0, y0, x1, y1 in pads:
        boxes.append((x0, -y1, 0.0, x1, -y0, min(0.05, tall / 4)))
    return boxes


def with_model(footprint: str, path: str, offset=(0.0, 0.0, 0.0), rotate=(0.0, 0.0, 0.0)) -> str:
    """The footprint with one 3D model at path, placed; any model it had is replaced. Models are
    files in the library, named by path as the FireFli resistors and capacitors are: a model
    embedded in a footprint does not reach a board placed through KiCad's Python."""
    def n(v):
        return ("%.6f" % v).rstrip("0").rstrip(".") or "0"
    block = ('\t(model "%s"\n\t\t(offset\n\t\t\t(xyz %s %s %s)\n\t\t)\n\t\t(scale\n\t\t\t(xyz 1 1 1)\n\t\t)\n'
             '\t\t(rotate\n\t\t\t(xyz %s %s %s)\n\t\t)\n\t)\n'
             % (path, *(n(v) for v in offset), *(n(v) for v in rotate)))
    stripped = re.sub(r'\n\t\(model "(?:[^"\\]|\\.)*".*?\n\t\)', "", footprint, flags=re.S)
    end = stripped.rstrip().rindex(")")
    return stripped[:end].rstrip("\n") + "\n" + block + stripped[end:]


# ---------------------------------------------------------------- downloads

def package_files(source: Path) -> dict[str, dict[str, bytes]]:
    """The KiCad footprint, 3D models and SPICE files in a maker or SamacSys download (a zip or
    folder), by kind and name. Nothing in it is run; other formats are left alone."""
    found: dict[str, dict[str, bytes]] = {"footprint": {}, "model": {}, "spice": {}}

    def take(name: str, read) -> None:
        path = PurePosixPath(name.replace("\\", "/"))
        suffix = path.suffix.lower()
        if ".." in path.parts or path.name.startswith("."):
            return
        if suffix == ".kicad_mod":
            found["footprint"][path.name] = read()
        elif suffix in MODEL_SUFFIXES:
            found["model"][path.name] = read()
        elif suffix in SPICE_SUFFIXES and not path.name.lower().startswith(("license", "readme", "part_info")):
            found["spice"][path.name] = read()
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as bundle:
            for entry in bundle.infolist():
                if not entry.is_dir() and entry.file_size <= MAX_FILE:
                    take(entry.filename, lambda entry=entry: bundle.read(entry))
    elif source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_FILE:
                take(str(path.relative_to(source)), path.read_bytes)
    elif source.is_file():
        take(source.name, source.read_bytes)
    return found


def model_placement(footprint: str) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """The offset (mm) and rotation of the first 3D model in a footprint, current format
    (offset in mm) or legacy (at in inches, as SamacSys still writes)."""
    model = re.search(r'\(model\s+"?[^\s"()]+"?(.*?)\n\s*\)\s*(?:\n|$)', footprint, re.S) or \
        re.search(r'\(model\s+"?[^\s"()]+"?(.*)', footprint, re.S)
    if not model:
        return None

    def triple(key):
        found = re.search(r"\(%s\s*\(xyz\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)" % key, model.group(1))
        return tuple(float(v) for v in found.groups()) if found else None
    offset = triple("offset")
    if offset is None:
        at = triple("at")
        offset = tuple(v * 25.4 for v in at) if at else (0.0, 0.0, 0.0)
    return offset, triple("rotate") or (0.0, 0.0, 0.0)


# ---------------------------------------------------------------- SPICE

def spice_models(source: str) -> dict[str, dict[str, Any]]:
    """The models and subcircuits a SPICE file defines: name -> {kind, nodes}."""
    found = {}
    joined = re.sub(r"\n\+", " ", source.replace("\r", ""))
    for line in joined.split("\n"):
        words = line.strip().split()
        if len(words) >= 2 and words[0].lower() == ".subckt":
            nodes = [w for w in words[2:] if "=" not in w and w.lower() not in ("params:", "optional:")]
            found[words[1]] = {"kind": "subckt", "nodes": nodes}
        elif len(words) >= 3 and words[0].lower() == ".model":
            kind = re.split(r"[\s(]", words[2])[0].lower()
            found[words[1]] = {"kind": kind, "nodes": []}
    return found


# KiCad's model pin names for a built-in device (Sim.Pins "1=A 2=K"; checked with kicad-cli on
# 19 Nov 2026 for the diode), and the order ngspice takes the nodes in.
DEVICE_NODES = {"d": ("A", "K"), "nmos": ("D", "G", "S"), "pmos": ("D", "G", "S"), "npn": ("C", "B", "E"),
                "pnp": ("C", "B", "E"), "njf": ("D", "G", "S"), "pjf": ("D", "G", "S")}
DEVICE_LETTER = {"d": "D", "nmos": "M", "pmos": "M", "npn": "Q", "pnp": "Q", "njf": "J", "pjf": "J"}
KICAD_DEVICE = {"d": "D", "nmos": "NMOS", "pmos": "PMOS", "npn": "NPN", "pnp": "PNP", "njf": "NJFET",
                "pjf": "PJFET", "subckt": "SUBCKT"}


def check_deck(library_name: str, model: dict[str, Any], name: str) -> str:
    """An ngspice deck that loads the model and places one of it, each node to its own resistor:
    it runs only if the file and model are sound."""
    nodes = model["nodes"] if model["kind"] == "subckt" else list(DEVICE_NODES.get(model["kind"], ()))
    if not nodes:
        raise ValueError("ngspice cannot place a %s model on its own" % model["kind"])
    wires = ["n%d" % i for i in range(len(nodes))]
    if model["kind"] == "subckt":
        part = "X1 %s %s" % (" ".join(wires), name)
    elif model["kind"] in ("nmos", "pmos"):
        part = "M1 %s %s %s" % (wires[0], wires[1], wires[2]) + " %s %s" % (wires[2], name)
    else:
        part = "%s1 %s %s" % (DEVICE_LETTER[model["kind"]], " ".join(wires), name)
    loads = "\n".join("R%d %s 0 1k" % (i, w) for i, w in enumerate(wires))
    return ("* FireFli model check\n.include %s\n%s\n%s\nV1 n0 0 0.1\n.op\n.end\n"
            % (library_name, part, loads))


__all__ = ["DEVICE_NODES", "KICAD_DEVICE", "body_boxes", "box_step", "check_deck", "model_placement", "package_files",
           "spice_models", "step_extent", "with_model"]
