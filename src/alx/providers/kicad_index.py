"""One reading of a board: every object with its own address (18 Nov 2026).

The board file is parsed once, with the one s-expression reader (`kicad.sexpr`), into a flat
list of objects. Each object has the id KiCad gave it (its uuid) where it has one, else a
stable name (a pad is REF.NUMBER), so the same address works to find it, select it and change
it. Selection (`select`) is plain filtering of that list, with no KiCad process.

18 Nov review: pad nets, net groups and the overview each walked the board their own way.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from typing import Any

# Bumped whenever an object gains or changes a property, so a kept index of an older shape is
# never used (18 Nov review: text angle was added while step-2 indexes of the same bytes remained).
INDEX_VERSION = 2
KINDS = ("part", "field", "pad", "track", "arc", "via", "zone", "keepout", "teardrop", "text", "graphic")


def _value(node, name, index=1, default=None):
    found = node.named(name)
    if not found or len(found[0].children) <= index:
        return default
    return found[0].children[index]


def _numbers(node, name):
    found = node.named(name)
    out = []
    for value in (found[0].children[1:] if found else ()):
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            pass
    return out


def _uuid(node):
    return _value(node, "uuid") or _value(node, "tstamp")


def _hidden(node):
    """A field's or text's own hide flag, KiCad 8 to 10 (`(hide yes)`, a bare `hide`)."""
    for holder in (node, *node.named("effects")):
        for child in holder.children[1:]:
            if child == "hide":
                return True
        flag = holder.named("hide")
        if flag:
            return len(flag[0].children) < 2 or flag[0].children[1] != "no"
    return False


def _font(node):
    effects = node.named("effects")
    font = effects[0].named("font") if effects else []
    if not font:
        return None, None
    size = _numbers(font[0], "size")
    thickness = _numbers(font[0], "thickness")
    return (size[:2] or None), (thickness[0] if thickness else None)


def _net(node, names):
    net = node.named("net")
    if not net:
        return ""
    values = [str(v) for v in net[0].children[1:]]
    # KiCad 9 writes the number and the name; KiCad 10 the name alone.
    if len(values) > 1:
        return values[-1]
    return names.get(values[0], values[0]) if values else ""


def _absolute(origin, angle, offset):
    """A footprint child's board position from its offset: KiCad's own rotation (checked against
    pcbnew on P0009: 3433 fields and pads, no difference)."""
    if len(offset) < 2 or len(origin) < 2:
        return None
    theta = math.radians(angle)
    return [round(origin[0] + offset[0] * math.cos(theta) + offset[1] * math.sin(theta), 6),
            round(origin[1] - offset[0] * math.sin(theta) + offset[1] * math.cos(theta), 6)]


def _arc_length(start, mid, end):
    """The length along an arc through three points (18 Nov review: it was the chord)."""
    if not (len(start) == len(mid) == len(end) == 2):
        return None
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return round(math.dist(start, end), 4)
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.dist((ux, uy), start)
    angles = [math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end)]
    sweep = (angles[2] - angles[0]) % (2 * math.pi)
    through = (angles[1] - angles[0]) % (2 * math.pi)
    if through > sweep:
        sweep = 2 * math.pi - sweep
    return round(r * sweep, 4)


def board_index(board_text: str) -> dict[str, Any]:
    """Every object on the board as a flat list, with the board's nets."""
    from alx.providers.kicad import sexpr
    tree = sexpr(board_text)
    names = {str(n.children[1]): str(n.children[2]) for n in tree.named("net") if len(n.children) > 2}
    objects: list[dict[str, Any]] = []
    for footprint in tree.named("footprint"):
        at = _numbers(footprint, "at")
        layer = _value(footprint, "layer", default="F.Cu")
        props = {str(p.children[1]): p for p in footprint.named("property") if len(p.children) > 2}
        ref = str(props["Reference"].children[2]) if "Reference" in props else ""
        attributes = [str(v) for v in (footprint.named("attr")[0].children[1:] if footprint.named("attr") else ())]
        part = {"id": _uuid(footprint) or "part:" + ref, "kind": "part", "ref": ref,
                "value": str(props["Value"].children[2]) if "Value" in props else "",
                "footprint": str(footprint.children[1]) if len(footprint.children) > 1 else "",
                "side": "B" if str(layer).startswith("B.") else "F",
                "at": at[:2], "rotation": at[2] if len(at) > 2 else 0.0,
                "fields": {name: str(p.children[2]) for name, p in props.items()},
                "dnp": "dnp" in attributes, "exclude_from_bom": "exclude_from_bom" in attributes}
        objects.append(part)
        for name, prop in props.items():
            size, thickness = _font(prop)
            offset = _numbers(prop, "at")[:2]
            objects.append({"id": _uuid(prop) or "field:%s:%s" % (ref, name), "kind": "field", "ref": ref,
                            "name": name, "value": str(prop.children[2]), "layer": _value(prop, "layer", default=""),
                            "offset": offset, "at": _absolute(at[:2], part["rotation"], offset),
                            "angle": (_numbers(prop, "at")[2:3] or [0.0])[0],
                            "size": size, "thickness": thickness, "visible": not _hidden(prop)})
        for pad in footprint.named("pad"):
            number = str(pad.children[1]) if len(pad.children) > 1 else ""
            objects.append({"id": _uuid(pad) or "%s.%s" % (ref, number), "kind": "pad", "ref": ref,
                            "number": number, "pad": "%s.%s" % (ref, number), "net": _net(pad, names),
                            "type": str(pad.children[2]) if len(pad.children) > 2 else "",
                            "shape": str(pad.children[3]) if len(pad.children) > 3 else "",
                            "size": _numbers(pad, "size")[:2],
                            "at": _absolute(at[:2], part["rotation"], _numbers(pad, "at")[:2]),
                            "layers": [str(v) for v in (pad.named("layers")[0].children[1:] if pad.named("layers") else ())]})
    for node in tree.children:
        if not hasattr(node, "named"):
            continue
        kind = node.name
        if kind in ("segment", "arc"):
            start, end = _numbers(node, "start")[:2], _numbers(node, "end")[:2]
            objects.append({"id": _uuid(node), "kind": "track" if kind == "segment" else "arc",
                            "net": _net(node, names), "layer": _value(node, "layer", default=""),
                            "width": (_numbers(node, "width") or [None])[0], "start": start, "end": end,
                            "length": (_arc_length(start, _numbers(node, "mid")[:2], end) if kind == "arc" else
                                       round(math.dist(start, end), 4) if len(start) == len(end) == 2 else None)})
        elif kind == "via":
            objects.append({"id": _uuid(node), "kind": "via", "net": _net(node, names),
                            "at": _numbers(node, "at")[:2], "size": (_numbers(node, "size") or [None])[0],
                            "drill": (_numbers(node, "drill") or [None])[0],
                            "layers": [str(v) for v in (node.named("layers")[0].children[1:] if node.named("layers") else ())]})
        elif kind == "zone":
            layers = node.named("layers") or node.named("layer")
            attr = node.named("attr")
            teardrop = bool(attr and attr[0].named("teardrop"))
            # KiCad writes no uuid on teardrop zones: their address is their own text (18 Nov review).
            # From its net, layer and outline, not its fill, which KiCad redraws on every save.
            outline = "".join(board_text[p.start:p.end] for p in node.named("polygon"))
            uid = _uuid(node) or "teardrop:" + hashlib.sha256(
                (_net(node, names) + "|" + str(_value(node, "layer", default="")) + "|" + outline).encode()).hexdigest()[:16]
            objects.append({"id": uid, "kind": ("teardrop" if teardrop else
                                                        "keepout" if node.named("keepout") else "zone"),
                            "net": _net(node, names), "name": _value(node, "name", default=""),
                            "layers": [str(v) for v in (layers[0].children[1:] if layers else ())]})
        elif kind in ("gr_text", "gr_text_box"):
            size, thickness = _font(node)
            objects.append({"id": _uuid(node), "kind": "text", "box": kind == "gr_text_box",
                            "text": str(node.children[1]) if len(node.children) > 1 else "",
                            "layer": _value(node, "layer", default=""),
                            "at": (_numbers(node, "at") or _numbers(node, "start"))[:2],
                            "angle": (_numbers(node, "at")[2:3] or [0.0])[0],
                            "size": size, "thickness": thickness, "visible": not _hidden(node)})
        elif kind.startswith("gr_"):
            objects.append({"id": _uuid(node), "kind": "graphic", "shape": kind[3:],
                            "layer": _value(node, "layer", default=""),
                            "width": (_numbers(node.named("stroke")[0], "width") if node.named("stroke") else
                                      _numbers(node, "width") or [None])[0] if (node.named("stroke") or node.named("width")) else None})
    # Every address is unique: a repeated one (two uuid-less pads with one number) gets #2, #3.
    seen: dict[str, int] = {}
    for o in objects:
        count = seen.get(o["id"], 0) + 1
        seen[o["id"]] = count
        if count > 1:
            o["id"] = "%s#%d" % (o["id"], count)
    # KiCad 10 keeps no list of nets: they are the names its objects carry.
    nets = set(names.values()) | {o.get("net") for o in objects if o.get("net")}
    return {"version": INDEX_VERSION, "objects": objects, "nets": sorted(nets - {"", None})}


def pad_nets(index: Mapping[str, Any]) -> dict[str, str]:
    """Each pad of a part with a reference as REF.PAD with its net's name."""
    return {o["pad"]: o["net"] for o in index["objects"] if o["kind"] == "pad" and o["ref"]}


def net_groups(index: Mapping[str, Any], with_parts: bool = False):
    """Every net joining two or more pads, as its set of (reference, pad); with_parts also the
    parts with a connected pad (a logo or a '#' item is not a part)."""
    groups: dict[str, set] = {}
    for o in index["objects"]:
        if o["kind"] == "pad" and o["ref"] and not o["ref"].startswith("#") and o["net"]:
            groups.setdefault(o["net"], set()).add((o["ref"], o["number"]))
    joined = {frozenset(nodes) for nodes in groups.values() if len(nodes) > 1}
    if with_parts:
        return joined, {ref for nodes in groups.values() for ref, _ in nodes}
    return joined


def _where(o: Mapping[str, Any]) -> list[float] | None:
    if o.get("at"):
        return o["at"]
    if o.get("start") and o.get("end"):
        return [(o["start"][0] + o["end"][0]) / 2, (o["start"][1] + o["end"][1]) / 2]
    return None


def select(index: Mapping[str, Any], kind: str, *, refs: Iterable[str] | None = None,
           nets: Iterable[str] | None = None, layer: str | None = None, name: str | None = None,
           value: str | None = None, footprint: str | None = None, area: Iterable[float] | None = None,
           reads: str | None = None, ids: Iterable[str] | None = None, named: bool = False) -> list[dict[str, Any]]:
    """The objects of one kind that match every filter given; exact values, no meaning guessed.

    refs: designators (a field, pad or part of these parts); nets: exact net names; layer: an
    exact layer, or "F"/"B" for a side; name: a field's name (Reference, Value, MPN ...); value:
    a part's or field's exact value; footprint: LIBRARY:NAME or just NAME; area: [x0, y0, x1, y1]
    mm (everything by its own board position, tracks by midpoint); reads: what a board text reads,
    exactly; named: only parts with a designator, and their fields and pads (not logos)."""
    if kind not in KINDS:
        raise ValueError("kind is one of " + ", ".join(KINDS))
    refs = set(refs) if refs is not None else None
    nets = set(nets) if nets is not None else None
    ids = set(ids) if ids is not None else None
    box = None
    if area is not None:
        x0, y0, x1, y1 = list(area)
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    parts = {o["ref"]: o for o in index["objects"] if o["kind"] == "part"}
    found = []
    for o in index["objects"]:
        if o["kind"] != kind:
            continue
        if ids is not None and o["id"] not in ids:
            continue
        if named and not o.get("ref"):
            continue
        if refs is not None and o.get("ref") not in refs:
            continue
        if nets is not None and o.get("net") not in nets:
            continue
        if layer is not None:
            here = [o.get("layer")] if o.get("layer") else list(o.get("layers") or [])
            if kind == "part":
                here = [o["side"] + ".Cu"]
            if layer in ("F", "B"):
                if not any(str(item).startswith(layer + ".") or str(item) == "*.Cu" for item in here):
                    continue
            elif layer not in here:
                continue
        if name is not None and o.get("name") != name:
            continue
        if value is not None and o.get("value") != value:
            continue
        if footprint is not None:
            fp = (parts.get(o.get("ref")) or o).get("footprint", "")
            if footprint not in (fp, fp.split(":", 1)[-1]):
                continue
        if reads is not None and o.get("text") != reads:
            continue
        if box is not None:
            spot = _where(o)
            if not spot or not (box[0] <= spot[0] <= box[2] and box[1] <= spot[1] <= box[3]):
                continue
        found.append(o)
    return found




def netlist_model(netlist_text: str) -> dict[str, Any]:
    """KiCad's exported netlist (its default s-expression format) as parts and nets.

    The one netlist reader (18 Nov review: five places exported and read it their own way,
    one by splitting on a literal "(net" and a newline)."""
    from alx.providers.kicad import sexpr

    def text(node, name):
        found = node.named(name)
        return str(found[0].children[1]) if found and len(found[0].children) > 1 else ""
    tree = sexpr(netlist_text)
    components = []
    for holder in tree.named("components"):
        for comp in holder.named("comp"):
            fields = {}
            for group in comp.named("fields"):
                for field in group.named("field"):
                    name = text(field, "name")
                    if name and len(field.children) > 2 and isinstance(field.children[-1], str):
                        fields[name] = str(field.children[-1])
            for name in ("datasheet", "description"):
                if text(comp, name):
                    fields.setdefault(name.capitalize(), text(comp, name))
            sheet = comp.named("sheetpath")
            components.append({"ref": text(comp, "ref"), "value": text(comp, "value"),
                               "footprint": text(comp, "footprint"), "fields": fields,
                               "sheet": text(sheet[0], "names") if sheet else "/",
                               "path": (text(sheet[0], "tstamps") if sheet else "/") + text(comp, "tstamps")})
    nets = []
    for holder in tree.named("nets"):
        for net in holder.named("net"):
            nets.append({"name": text(net, "name"),
                         "nodes": [{"ref": text(node, "ref"), "pin": text(node, "pin"),
                                    "function": text(node, "pinfunction")} for node in net.named("node")]})
    return {"components": components, "nets": nets}


def netlist_nets(model: Mapping[str, Any]) -> dict[frozenset, str]:
    """Each net of the netlist as its set of (reference, pin), with its name."""
    found = {}
    for net in model["nets"]:
        nodes = frozenset((node["ref"], node["pin"]) for node in net["nodes"])
        if nodes:
            found[nodes] = net["name"]
    return found


__all__ = ["INDEX_VERSION", "KINDS", "board_index", "net_groups", "netlist_model", "netlist_nets", "pad_nets", "select"]
