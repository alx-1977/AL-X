"""Additional drawing helpers loaded into the confined KiCad interpreter."""

BOARD_HELPERS = r'''
def _pin(text):
    ref, number = text.split(".", 1)
    return ref, number


def power_template(s, port):
    port = port.split(":", 1)[-1]
    for item in s.symbols():
        if item["ref"].startswith("#") and item["value"] == port:
            # By uuid: imported power symbols often share #PWR? (15 Nov).
            return item["uuid"] or item["ref"]
    ports = sorted({item["value"] for item in s.symbols() if item["ref"].startswith("#")})
    raise KeyError("no %s port on %s to copy; this sheet has %s" % (port, s.path.rsplit("/", 1)[-1],
                                                                       ", ".join(ports) or "none"))


def sync_from_schematic(b, place=None):
    """The board takes the schematic as it is: each part's footprint (swapped in place where it
    differs), value, fields and pad nets. A part new to the board goes where place says
    ({ref: [x, y, rotation?]}); parts only on the board are named, never removed."""
    place = place or {}
    data = SCHEMATIC_SYNC
    out = {"replaced": [], "added": [], "fields_set": 0, "pads_renetted": 0, "needs_place": [],
           "not_in_schematic": data.get("not_in_schematic", [])}
    for ref, info in sorted(data["parts"].items()):
        if not info["footprint"]:
            continue
        found = b.FindFootprintByReference(ref)
        library_name = info["footprint"].replace(":", "/", 1)
        if found is None:
            if ref not in place:
                out["needs_place"].append(ref)
                continue
            where = place[ref]
            add_part(b, ref, library_name, (where[0], where[1]), where[2] if len(where) > 2 else 0,
                     value=info["value"])
            found = b.FindFootprintByReference(ref)
            found.SetPath(_pcbnew.KIID_PATH(info["path"]))
            out["added"].append(ref)
        elif str(found.GetFPID().GetUniStringLibId()) != info["footprint"]:
            replace_part(b, ref, library_name)
            found = b.FindFootprintByReference(ref)
            out["replaced"].append(ref)
        if info["value"] and found.GetValue() != info["value"]:
            found.SetValue(info["value"])
        for name, text in info["fields"].items():
            if name in ("Reference", "Value", "Footprint"):
                continue
            if found.HasField(name):
                if found.GetField(name).GetText() != text:
                    found.GetField(name).SetText(text)
                    out["fields_set"] += 1
            else:
                field = _pcbnew.PCB_FIELD(found, _pcbnew.FIELD_T_USER, name)
                field.SetText(text)
                field.SetVisible(False)
                field.SetLayer(_pcbnew.F_Fab if not found.IsFlipped() else _pcbnew.B_Fab)
                field.SetPosition(found.GetPosition())
                found.Add(field)
                out["fields_set"] += 1
        nets = data["nets"].get(ref, {})
        for item in found.Pads():
            name = nets.get(item.GetNumber())
            if name and item.GetNetname() != name:
                item.SetNet(_net(b, name))
                out["pads_renetted"] += 1
    return out


def remove_parts(b, refs, with_tracks=True):
    nets = {item.GetNetname() for ref in refs for item in part(b, ref).Pads()} - {""}
    for ref in refs:
        b.Delete(part(b, ref))
    if with_tracks:
        still = {item.GetNetname() for fp in b.GetFootprints() for item in fp.Pads()}
        gone = [t for t in list(b.GetTracks()) if t.GetNetname() in nets - still]
        for item in gone:
            remove(b, item)
        return len(gone)
    return 0


def place_part(b, ref, at=None, near=None, rotation=None, side=None):
    found = part(b, ref)
    if near:
        anchor = part(b, near["ref"]).GetPosition()
        dx, dy = near.get("offset", (0, 0))
        move_part(b, ref, mm(anchor.x) + dx, mm(anchor.y) + dy)
    elif at:
        move_part(b, ref, *at)
    if side in ("F", "B") and (side == "B") != found.IsFlipped():
        found.Flip(found.GetPosition(), _pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    if rotation is not None:
        rotate_part(b, ref, rotation)
    return describe(b, ref)["at"]


def remove_tracks(b, nets, area=None):

    def inside(item):
        if area is None:
            return True
        ends = [item.GetPosition()] if item.GetClass() == "PCB_VIA" else [item.GetStart(), item.GetEnd()]
        return all(area[0] <= mm(p.x) <= area[2] and area[1] <= mm(p.y) <= area[3] for p in ends)
    gone = [t for t in list(b.GetTracks()) if t.GetNetname() in nets and inside(t)]
    for item in gone:
        remove(b, item)
    return len(gone)


def remove_dangling_tracks(b, area):
    # KiCad's own connectivity says which ends lead nowhere; repeated, since
    # taking one stub off can leave the next hanging (16 Nov: 17 stubs and 2 vias
    # were left where moved parts had been).
    total = 0

    def within(item):
        ends = [item.GetPosition()] if item.GetClass() == "PCB_VIA" else [item.GetStart(), item.GetEnd()]
        return all(min(area[0], area[2]) <= mm(p.x) <= max(area[0], area[2])
                   and min(area[1], area[3]) <= mm(p.y) <= max(area[1], area[3]) for p in ends)
    for _ in range(20):
        # Zones filled as the board now is, as the DRC will see them.
        refill(b)
        b.BuildConnectivity()
        links = b.GetConnectivity()
        copper = [_pcbnew.F_Cu, _pcbnew.B_Cu] + [_pcbnew.In1_Cu + 2 * i for i in range(30)
                                                if b.IsLayerEnabled(_pcbnew.In1_Cu + 2 * i)]

        def dangling(item):
            # A via joins nothing unless it is connected on two layers, as KiCad's DRC counts it.
            if item.GetClass() == "PCB_VIA":
                return sum(1 for layer in copper if links.IsConnectedOnLayer(item, layer)) < 2
            return links.TestTrackEndpointDangling(item, False)
        gone = [t for t in list(b.GetTracks()) if within(t) and dangling(t)]
        if not gone:
            break
        for item in gone:
            remove(b, item)
        total += len(gone)
    return total


def add_keepout(b, outline, layers=None, name=None, no_tracks=True, no_vias=True):
    # A keep-out (rule) area: no zone fill, and by default no tracks or vias, on the
    # copper layers named (all by default). The pours pull back from it when refilled
    # (17 Nov: a chip antenna's clearance from its datasheet layout).
    area = _pcbnew.ZONE(b)
    area.SetIsRuleArea(True)
    area.SetDoNotAllowZoneFills(True)
    area.SetDoNotAllowTracks(bool(no_tracks))
    area.SetDoNotAllowVias(bool(no_vias))
    area.SetDoNotAllowPads(False)
    area.SetDoNotAllowFootprints(False)
    names = layers or ["F", "B"] + ["In%d" % i for i in range(1, 31)]
    layers = _pcbnew.LSET()
    for layer_name in names:
        copper = {"F": _pcbnew.F_Cu, "B": _pcbnew.B_Cu}.get(layer_name)
        if copper is None and layer_name.startswith("In") and layer_name[2:].isdigit():
            copper = _pcbnew.In1_Cu + 2 * (int(layer_name[2:]) - 1)
        if copper is not None and b.IsLayerEnabled(copper):
            layers.AddLayer(copper)
    area.SetLayerSet(layers)
    if name:
        area.SetZoneName(name)
    polygon = area.Outline()
    polygon.NewOutline()
    for x, y in outline:
        polygon.Append(_pcbnew.FromMM(x), _pcbnew.FromMM(y))
    b.Add(area)
    return {"corners": len(outline), "layers": layers.CuStack().size()}


def route_connection(b, start, end, width=None, layers=None, follow=None, radius=None, net=None, on=None):
    # One connection, path found by code: 45-degree bends, as few bends and vias as it
    # can, clear of everything (17 Nov). from/to: REF.PAD or [x, y]; the net comes from
    # the pads; it may cross to another layer through vias.
    ends, sides = [], []
    for endpoint in (start, end):
        if isinstance(endpoint, str):
            ref, number = _pin(endpoint)
            found = pad(b, ref, number)
            net = net or found.GetNetname()
            ends.append(xy(found.GetPosition()))
            sides.append(on if on and found.IsOnLayer(layer(on))
                         else ("F" if found.IsOnLayer(_pcbnew.F_Cu) else "B"))
        else:
            # A point end is on the layer named, else the first of the layers given
            # (17 Nov: a route between points with layers [In2] started on top).
            ends.append((float(endpoint[0]), float(endpoint[1])))
            sides.append(on or (layers or ["F"])[0])
    if not net:
        raise ValueError("route: name its net, or start or end it on a pad")
    # His style (17 Nov), from his own board: its usual width and via, its routing layers;
    # an RF-wide line (0.3 mm and up) stays on its own layer with arcs at his RF radius.
    profile = style(b)
    width = float(width or profile["width"])
    rf = width >= 0.3
    route_layers = layers or ([sides[0]] if rf and sides[0] == sides[1] else None)
    radius = radius if radius is not None else (rf_radius(width) if rf else None)
    via_size = tuple(profile["via"])
    runs, vias = route(b, net, ends[0], ends[1], width, sides[0], end_on=sides[1],
                       layers=route_layers, via=via_size, follow=follow)
    for name, corners in runs:
        # A curve sits inside its corner, nearer what is there; where the full RF radius
        # would touch something the curve tightens, and at worst stays a 45-degree corner.
        for tried in ([radius, radius / 2.0, None] if radius else [None]):
            try:
                add_track(b, net, corners, width, name, tried, fit=True)
                break
            except ValueError:
                if tried is None:
                    raise
    for spot in vias:
        add_via(b, net, spot, *via_size)
    # A point end where this net's copper on another layer already ends gets the via
    # that joins them (17 Nov: pieces routed layer by layer met at points, unjoined).
    for spot, side, given in zip(ends, sides, (start, end)):
        if not isinstance(given, str):
            here = point(*spot)
            has_via = any(t.GetClass() == "PCB_VIA" and t.GetNetname() == net and t.GetPosition() == here
                          for t in b.GetTracks())
            other = any(t.GetClass() != "PCB_VIA" and t.GetNetname() == net and t.GetLayer() != layer(side)
                        and here in (t.GetStart(), t.GetEnd()) for t in b.GetTracks())
            if other and not has_via:
                add_via(b, net, spot, *via_size)
                vias.append(spot)
    import math as _m
    length = sum(_m.hypot(q[0] - p[0], q[1] - p[1]) for _, pts in runs for p, q in zip(pts, pts[1:]))
    # The neat score: what in this route goes against his style, for her to see.
    odd = sum(1 for _, pts in runs for p, q in zip(pts, pts[1:])
              if _m.hypot(q[0] - p[0], q[1] - p[1]) > 0.2
              and abs(((_m.degrees(_m.atan2(q[1] - p[1], q[0] - p[0])) + 22.5) % 45) - 22.5) > 1)
    neat = []
    square = 0
    for _, pts in runs:
        for p, q, r in zip(pts, pts[1:], pts[2:]):
            a = _m.atan2(q[1] - p[1], q[0] - p[0])
            c = _m.atan2(r[1] - q[1], r[0] - q[0])
            if abs((_m.degrees(c - a) + 180) % 360 - 180) > 60:
                square += 1
    if square:
        neat.append("%d corner(s) of 90 degrees or sharper (his rule: none); route from pad to "
                    "pad, or put your in-between points where the line turns by 45" % square)
    if odd:
        neat.append("%d run(s) not at 0/45/90 degrees" % odd)
    strays = sorted({n for n, _ in runs} - set(route_layers or profile["layers"]) - set(sides))
    if strays:
        neat.append("on %s, not one of this board's routing layers" % ", ".join(strays))
    if rf and vias:
        neat.append("an RF line through %d via(s): keep it on one layer" % len(vias))
    if vias and len(vias) > 2:
        neat.append("%d vias" % len(vias))
    bends = sum(len(pts) - 2 for _, pts in runs)
    if bends > 4 + 2 * len(vias):
        neat.append("%d bends" % bends)
    straight = _m.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1])
    if straight > 2 and length > 1.6 * straight:
        neat.append("%.0f%% longer than straight" % (100 * (length / straight - 1)))
    return {"net": net, "width": width, "radius": radius,
                     "runs": [{"layer": n, "points": pts} for n, pts in runs],
                     "vias": vias, "bends": bends, "length_mm": round(length, 3),
                     "straight_mm": round(straight, 3), "neat": neat or "clean"}


def to_plane(b, pins, via=None):
    # His rule (17 Nov): a power or ground pin goes into its plane as quickly as it can, by a
    # short stub and one via right at the pin, not a track wandering off to a via somewhere.
    import math as _m
    profile = style(b)
    via_size = tuple(via or profile["via"])
    rules = [z for z in list(b.Zones()) + [z for fp in b.GetFootprints() for z in fp.Zones()]
             if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
    outline = _pcbnew.SHAPE_POLY_SET()
    inside = outline if b.GetBoardPolygonOutlines(outline, False) and outline.OutlineCount() else None
    if inside is not None:
        inside.Deflate(_pcbnew.FromMM(via_size[0] / 2 + 0.3), _pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                       _pcbnew.FromMM(0.01))
    made = []
    for pin in pins:
        ref, number = _pin(pin)
        found = pad(b, ref, number)
        net = found.GetNetname()
        side = _pcbnew.F_Cu if found.IsOnLayer(_pcbnew.F_Cu) else _pcbnew.B_Cu
        planes = sorted({b.GetLayerName(on) for z in b.Zones() if not z.GetIsRuleArea()
                         and z.GetNetname() == net for on in z.GetLayerSet().CuStack()
                         if on != side})
        if not planes:
            raise ValueError("%s is on %s, which has no plane on another layer to go down to"
                             % (pin, net or "no net"))
        centre = xy(found.GetPosition())
        # Already in its plane only if copper really joins it: a via in the pad, or a
        # track of its net from the pad to a via (review of #247: any same-net via within
        # 1.2 mm, another pin's, counted, and nothing was drawn).
        # Only copper on the pin's own layer joins it: a via that reaches that layer, and
        # tracks drawn on it (review of #247: ends that merely lay over one another on
        # different layers were taken as joined).
        vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == net
                and t.IsOnLayer(side)]

        def at_via(spot):
            return any(_m.hypot(*(a - c for a, c in zip(xy(spot), xy(v.GetPosition()))))
                       <= _pcbnew.ToMM(v.GetWidth(_pcbnew.F_Cu)) / 2 + 1e-3 for v in vias)
        # Followed through any number of its net's tracks and arcs, not just one.
        pieces = [t for t in b.GetTracks() if t.GetClass() in ("PCB_TRACK", "PCB_ARC")
                  and t.GetNetname() == net and t.GetLayer() == side]
        reached, ends, grown = set(), [], True
        while grown:
            grown = False
            for t in pieces:
                if id(t) in reached:
                    continue
                tips = (t.GetStart(), t.GetEnd())
                if any(found.HitTest(tip) or any(_m.hypot(*(a - c for a, c in zip(xy(tip), e))) < 1e-3
                                                 for e in ends) for tip in tips):
                    reached.add(id(t))
                    ends.extend(xy(tip) for tip in tips)
                    grown = True
        joined = any(found.HitTest(v.GetPosition()) for v in vias) or any(at_via(_pcbnew.VECTOR2I(
            _pcbnew.FromMM(e[0]), _pcbnew.FromMM(e[1]))) for e in ends)
        if found.GetAttribute() == _pcbnew.PAD_ATTRIB_PTH or joined:
            made.append({"pin": pin, "already": "a hole or via of %s joined to the pin" % net})
            continue
        body = xy(found.GetParentFootprint().GetPosition())
        out = (centre[0] - body[0], centre[1] - body[1])
        size = found.GetSize(side)
        half = _pcbnew.ToMM(max(size.x, size.y)) / 2
        stub = max(0.15, min(0.4, _pcbnew.ToMM(min(size.x, size.y))))
        directions = [(_m.cos(a * _m.pi / 4), _m.sin(a * _m.pi / 4)) for a in range(8)]
        directions.sort(key=lambda d: -(d[0] * out[0] + d[1] * out[1]))
        placed = None
        for distance in [half + via_size[0] / 2 + 0.1 + 0.05 * k for k in range(25)]:
            for dx, dy in directions:
                spot = (round(centre[0] + dx * distance, 3), round(centre[1] + dy * distance, 3))
                here = point(*spot)
                if inside is not None and not inside.Contains(here):
                    continue
                if any(z.Outline().Contains(here) for z in rules):
                    continue
                try:
                    via_item = add_via(b, net, spot, *via_size)
                except ValueError:
                    continue
                try:
                    add_track(b, net, [centre, spot], stub, "F" if side == _pcbnew.F_Cu else "B")
                except ValueError:
                    b.Remove(via_item)
                    continue
                placed = spot
                break
            if placed:
                break
        if not placed:
            raise ValueError("no room for a via within %.1f mm of %s clear of everything; move "
                             "what crowds it, or route it with route" % (half + 1.6, pin))
        made.append({"pin": pin, "net": net, "via": placed, "plane": planes,
                     "stub_mm": round(_m.hypot(placed[0] - centre[0], placed[1] - centre[1]), 3)})
    return made


def remove_copper_shapes(b, nets, area):
    # Copper shapes and pours of these nets lying wholly inside area: to redraw a taper
    # or feed copper (17 Nov: pour-type shapes from an earlier version, empty in a keep-out).
    x0, y0, x1, y1 = (min(area[0], area[2]), min(area[1], area[3]),
                      max(area[0], area[2]), max(area[1], area[3]))

    def inside(box):
        return (x0 <= _pcbnew.ToMM(box.GetLeft()) and _pcbnew.ToMM(box.GetRight()) <= x1
                and y0 <= _pcbnew.ToMM(box.GetTop()) and _pcbnew.ToMM(box.GetBottom()) <= y1)
    gone = 0
    for zone in list(b.Zones()):
        if (not zone.GetIsRuleArea() and not zone.IsTeardropArea()
                and zone.GetNetname() in nets and inside(zone.GetBoundingBox())):
            b.Delete(zone)
            gone += 1
    for item in list(b.GetDrawings()):
        if (item.GetClass() == "PCB_SHAPE" and item.IsOnCopperLayer()
                and item.GetNetname() in nets and inside(item.GetBoundingBox())):
            b.Delete(item)
            gone += 1
    return gone


def remove_keepouts(b, area, name=None):
    # Keep-out areas lying wholly inside area [x1, y1, x2, y2] (or only the one named),
    # to resize or replace them (17 Nov: AE1's old clearance was the wrong size).
    x0, y0, x1, y1 = (min(area[0], area[2]), min(area[1], area[3]),
                      max(area[0], area[2]), max(area[1], area[3]))
    gone = []
    for zone in list(b.Zones()):
        if not zone.GetIsRuleArea():
            continue
        if name and zone.GetZoneName() != name:
            continue
        box = zone.GetBoundingBox()
        if (x0 <= _pcbnew.ToMM(box.GetLeft()) and _pcbnew.ToMM(box.GetRight()) <= x1
                and y0 <= _pcbnew.ToMM(box.GetTop()) and _pcbnew.ToMM(box.GetBottom()) <= y1):
            gone.append(zone.GetZoneName() or "unnamed")
            b.Delete(zone)
    return gone


def set_title_block(b, title=None, date=None, revision=None, company=None):
    block = b.GetTitleBlock()
    if title is not None:
        block.SetTitle(title)
    if date is not None:
        block.SetDate(date)
    if revision is not None:
        block.SetRevision(revision)
    if company is not None:
        block.SetCompany(company)
    b.SetTitleBlock(block)
    return {"title": block.GetTitle(), "date": block.GetDate(),
                           "revision": block.GetRevision()}
'''
