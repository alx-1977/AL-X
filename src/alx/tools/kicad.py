"""One KiCad capability with an action per job (D-074); structured evidence returns to Core."""
from dataclasses import replace
from enum import Enum

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState,
    SideEffect, StructuredSchema, ValueKind,
)

S = StructuredSchema(ValueKind.STRING)
I = StructuredSchema(ValueKind.INTEGER)
B = StructuredSchema(ValueKind.BOOLEAN)
N = StructuredSchema(ValueKind.NUMBER)


def obj(properties, required=None):
    return StructuredSchema(ValueKind.OBJECT, properties,
                            tuple(properties) if required is None else required, extra_properties=False)


def array(item):
    return StructuredSchema(ValueKind.ARRAY, items=item)


TOTALS = obj({'error': I, 'warning': I, 'ignore': I, 'total': I})
RULE_CITATIONS = array(obj({'name': S, 'count': I}))
RECOVERY = obj({'backed_up': array(S), 'written': array(S), 'rollback_status': S,
                'restorations': array(obj({'original': S, 'backup': S, 'restored': B, 'error': S})),
                'remaining_changed': array(S), 'cleanup_errors': array(S),
                'error': obj({'code': S, 'type': S})})
ITEM = obj({'description': S, 'designator': S, 'pad_number': S, 'uuid': S,
            'position': obj({'x': N, 'y': N}, ())})
GROUP = obj({'check_type': S, 'count': I, 'noise': B, 'omitted_findings': I, 'findings': array(
    obj({'severity': S, 'description': S, 'items': array(ITEM)}))})
FAILURES = ('arguments_unusable', 'path_denied', 'unsupported_project', 'invalid_report',
            'cli_failed', 'cli_timeout', 'render_failed', 'footprint_ambiguous', 'board_open',
            'original_changed', 'work_copy_changed', 'work_copy_tainted', 'work_copy_busy',
            'script_failed', 'export_would_overwrite', 'rename_ambiguous', 'unsupported_file',
            'project_too_large', 'evidence_full', 'verification_failed', 'unavailable')
FAILURES += ('engine_unavailable', 'engine_timeout', 'engine_failed', 'step_not_approved',
             'schematic_board_differ', 'board_not_clean', 'change_unmet')

MAP = StructuredSchema(ValueKind.OBJECT)
SECTION = obj({'before': S, 'after': S, 'changed': B, 'truncated': B})
BOARD_ITEM = obj({'id': S, 'kind': S, 'layers': array(S)})
ERC = obj({'checked': B, 'reason': S, 'totals': TOTALS, 'top_level_sheets': I, 'only_new': B, 'new': TOTALS,
          'groups': array(obj({
    'check_type': S, 'count': I, 'severity': S, 'omitted_findings': I, 'findings': array(obj({
        'severity': S, 'sheet': S, 'description': S, 'items': array(obj({
            'description': S, 'designator': S, 'position': obj({'x': N, 'y': N}, ())}))}))}))},
    ('checked', 'reason', 'totals', 'top_level_sheets', 'groups'))

LAYOUT_RULES = obj({'findings': array(StructuredSchema(ValueKind.OBJECT)), 'omitted': I,
                    'not_checked': S}, ('findings', 'omitted'))
CHANGE_REPORT = obj({'files_changed': array(S), 'files_truncated': B,
    'file_diffs': array(obj({'name': S, 'diff': S, 'truncated': B})), 'diffs_truncated': B, 'board': obj({
    'layers': SECTION, 'setup': SECTION, 'stackup': SECTION,
    'items_added': array(BOARD_ITEM), 'items_removed': array(BOARD_ITEM),
    'layer_moves': array(obj({'id': S, 'kind': S, 'before': array(S), 'after': array(S)})),
    'added_count': I, 'removed_count': I, 'moved_count': I, 'truncated': B,
    'per_layer_before': MAP, 'per_layer_after': MAP}), 'full_diff_path': S, 'before_copy_path': S,
    'after_copy_path': S, 'layout_rules': LAYOUT_RULES, 'erc': ERC, 'compared_to_original': MAP,
    'step_refusal': MAP, 'change_result': array(MAP), 'unmet': array(S), 'reverted_edits': I,
    'set': array(MAP)},
    ('files_changed', 'files_truncated', 'file_diffs', 'diffs_truncated', 'board'))
COMPARISON = obj({'files': array(obj({'name': S, 'status': S, 'differing_lines': I,
                                    'line_count_truncated': B})), 'truncated': B})
REFERENCE = obj({'kind': S, 'path': S, 'text': S, 'truncated': B, 'entry': S,
    'media_type': S, 'width': I, 'height': I, 'page_count': I,
    'pages': array(obj({'page': I, 'text': S, 'truncated': B})),
    'documents': array(S), 'streams': array(S), 'board_records_decoded': B,
    'entries': array(obj({'name': S, 'kind': S, 'bytes': I}, ('name',)))}, ('kind',))

ENDPOINT = obj({'reference': S, 'pad': S, 'x_mm': N, 'y_mm': N, 'layer': S}, ())
PORT = obj({'id': S, 'start': ENDPOINT, 'end': ENDPOINT})
MEASUREMENT = obj({'id': S, 'kind': S, 'positive': S, 'negative': S, 'source': S}, ('id', 'kind'))
ANALYSIS = obj({'kind': S, 'start_hz': N, 'stop_hz': N, 'points': I, 'scale': S,
                'step_seconds': N, 'stop_seconds': N}, ('kind',))
TARGET = obj({'measurement_id': S, 'quantity': S, 'at': N, 'minimum': N,
              'maximum': N, 'error_scale': N})
TUNING = obj({'components': array(obj({'reference': S, 'values': array(N),
                                      'tolerance_fraction': N, 'tied_references': array(S)},
                                     ('reference', 'values', 'tolerance_fraction'))),
              'targets': array(TARGET), 'tie_margin': N})

_ACTION_DEFINITIONS = (
    CapabilityDefinition('check_kicad_board',
        'Run all-severity KiCad DRC with zone refill on a new isolated project copy. '
        'project_dir is his project\'s folder or just its number (P0009): the folder holding its one '
        'board is found. Requires exactly one top-level board. Returns grouped facts, configured noise flags '
        'and revision for exact write approval. custom_rule_citations counts rule names '
        'cited in violations, not all loaded rules; does not interpret findings. A review of his '
        'board ends in corrections on a copy shown as Current and Amended, not in questions to '
        'him (Friedl, 2026-11-12). erc is '
        "KiCad's electrical rules check of the schematic, errors and warnings; Friedl wants "
        'both clean, like the DRC. When erc.top_level_sheets is more than 1 (an Altium import), '
        "KiCad's board-against-schematic check (schematic_parity) reads only the first sheet: "
        'run make_hierarchical on the copy first, and write it back with his approval. '
        'work_copy_id instead of project_dir checks one of your copies again as it is now; '
        'compared_to_original says what is new against his original.',
        obj({'project_dir': S, 'work_copy_id': S}, ()), obj({'work_copy_id': S, 'work_copy_revision': S,
            'custom_rule_citations': RULE_CITATIONS, 'work_copy_tainted': B, 'baseline_eligible': B,
            'totals': TOTALS, 'violations': array(GROUP), 'unconnected_items': array(GROUP),
            'schematic_parity': array(GROUP), 'erc': ERC, 'layout_rules': LAYOUT_RULES,
            'compared_to_original': MAP}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('edit_kicad_work_copy',
        'Change a registered untainted work copy. change (D-084) is the way: a list of operations, '
        'each {op, ...}, with exact references, pins as REF.PIN and nets. Schematic ops name their '
        'sheet (file name): remove_symbols {refs}; swap_symbol {ref, lib_id, value?, footprint?, at?, '
        'rotation?} (wiring left on old pins is tidied; pins_still_wired names new pins where old wiring '
        'still ends, now joined to them: check each; the part takes the fields of its library part, MPN and LCSC included); '
        'add_symbol {ref, like, at, rotation?, lib_id?, value?} (a new part copied from part `like` on that sheet, under a '
        'free reference, then swapped to lib_id if given; its pins may touch nothing); wire {from, to, via?} (to a pin '
        'already wired: straight onto that wiring with a junction where it can; otherwise straight out of the pin one '
        'grid step, then one turn, his way; via: [[x, y], ...] in mm on the 1.27 grid to route around '
        'what is in the way, each piece straight across or up and down); label {pin, net, kind?: label|global_label}; power {pin | pins, '
        'port} (a GND or power symbol already on that sheet with that value is copied, GND down, '
        'power up, on a short wire; pins: neighbouring pins on one side share one wire and one port; where they are boxed in, '
        'the wire goes on, crossing a wire once, to a port clear of everything); no_connect {pins}; tidy_fields {refs}; remove_labels {nets, area?} (with the stubs only they '
        'held; without sheet, on every sheet); remove_items {area: [x1, y1, x2, y2], kinds?: wire|label|no_connect|junction|power} '
        '(what lies wholly inside; power symbols found by place, unannotated too; never parts); '
        'unwire {pins} (whatever is wired to those pins, and wiring left hanging; wiring on to other '
        'pins stays); remove_dangling {area?} (no-connects off pins, labels on nothing, ports touching nothing, '
        'wires with a free end). New wiring that would touch anything already there (a wire, a '
        'label, a flag, another pin) is refused, naming it and where, unless it is already on the same net. Board ops: route {from: REF.PAD | [x, y], to: REF.PAD | [x, y], width?, layers?, follow?: [NET, ...], radius?, net?} (one '
        'connection whose path the tool works out in Friedl\'s style: straight runs and 45-degree bends, as few '
        'bends and vias as it can, clear of everything, on the layers this board is routed on, with its usual '
        'width and via; a line 0.3 mm or wider is RF: it stays on its layer with arcs at the RF radius. follow: '
        'lines already routed that go the same way; this one runs beside them at even spacing. Reports bends, '
        'length and "neat" (what goes against his style, else "clean"). Ordinary lines must use it: track '
        'refuses lines under 0.3 mm longer than 2 mm. Route lines that travel together one after another '
        'with follow); to_plane {pins: [REF.PAD, ...]} (a power or ground '
        'pin straight into its plane: a short stub and one via at the pin); add_teardrops {skip_nets?, area?} '
        '(teardrops where tracks meet pads and vias and have none, shaped like the board\'s own; RF nets with '
        'curved tracks are skipped); set {kind: field|text|via|track|arc, where: {refs?, nets?, layer?: F|B|layer, '
        'name?: field name, value?, footprint?, area?: [x0, y0, x1, y1], reads?: a text\'s exact words, ids?, named?: true, all?: true}, '
        'to: {...}} (Altium\'s Find Similar then Inspector: every object of that kind matching all of where, '
        'from select\'s reading of this revision, gets exactly the properties in to and nothing else. field and '
        'text: size (mm, or [height, width]), thickness, visible, angle, at [x, y] (one object), move_by [dx, dy]; '
        'text also text; via: size, drill; track and arc: width. E.g. every designator 0.5 mm: {kind: field, '
        'where: {name: Reference, named: true}, to: {size: 0.5}}; pass select\'s work_copy_revision with the edit. The report\'s set receipt says matched, already as asked, '
        'changed; an object not as asked, or one not picked that changed, undoes the edit); '
        'title_block {title?, date?, revision?, company?}; copper_shape {net, outline: [[x, y], ...], layer?} (a solid copper polygon on one net, kept inside '
        'a no-pour keep-out like a track, touching nothing of another net: a taper from wide feed copper to a '
        'small pad, or feed copper); remove_copper_shapes {nets, area: [x1, y1, x2, y2]} (copper shapes and '
        'pours of those nets wholly inside, to redraw them); keepout {outline: [[x, y], ...], layers?: [F, In1, ..., B], no_tracks?, no_vias?, name?} (a '
        'keep-out area: pours pull back from it, e.g. an antenna clearance from its datasheet layout); '
        'remove_keepouts {area: [x1, y1, x2, y2], name?} (keep-out areas wholly inside, to resize or replace one); sync_from_schematic {place?: {REF: [x, y, rotation?]}} (the board takes the schematic as it '
        'is, like KiCad\'s Update PCB from Schematic: changed footprints swapped in place, values and fields '
        'such as MPN and LCSC copied, every pad on its schematic net; parts new to the board go where place '
        'says, else are listed in needs_place; parts only on the board are listed, never removed: remove_parts '
        'takes them off); remove_parts '
        '{refs, with_tracks?} (copper only they used goes too); replace_part {ref, footprint, '
        'pin_nets?}; add_part {ref, footprint, at, rotation?, side?, value?, pin_nets?}; place {ref, '
        'at? | near: {ref, offset}, rotation?, side?}; track {width, from?: REF.PAD, to?: REF.PAD, '
        'points?, net?, layer?, radius?}; via {net, at}; remove_tracks {nets, area?}; remove_teardrops '
        '{area}; remove_dangling_tracks {area} (tracks and vias inside that lead nowhere, by KiCad\'s '
        'own connectivity, repeated until none). The library parts named are found by themselves. Code carries the change out in one '
        'run and checks the result: every wire, label, power and no-connect holds in KiCad\'s netlist, '
        'every replaced pad has its net, removed parts are gone, and no pin (other than a '
        'swapped or removed part\'s) is joined to anything the change did not join, nor split '
        'unless the change removes labels or leftovers; otherwise nothing is kept and change_unmet lists each unmet item, so a refused change costs '
        'nothing: make the change rather than preview it. A change runs no ERC (checks true asks for '
        'it); run check work_copy_id once at the end of the step. revert_to (a revision '
        'from an earlier result, or start) alone puts the copy back as it was then. '
        'change_report.change_result says what each operation did. script (bounded Python with '
        'pcbnew) remains for what change cannot yet express, for you alone, not your board agent. '
        'argv[1] is the board. dry_run true runs it, reports everything it would change '
        '(printed output, change report, DRC, comparison with his original, and step_refusal if '
        'his order would refuse it) and puts the copy back as it was: look before you change, '
        'never raise an error to undo. A script cannot start programs: check a copy with check '
        'work_copy_id. Kernel confines writes to its project folder, including '
        'child processes. May edit board, project and rules freely. Returns a bounded plain '
        'change report, structural board diff and DRC totals before/after. Failed edits '
        'restore the copy; uncertain restoration taints it durably. When diffs_truncated, '
        'full_diff_path holds the complete diff and before_copy_path/after_copy_path copies '
        'from before and after the edit; later edits keep them. Read them with '
        'kicad read_reference, continuing long text with offset. evidence_full when retained '
        'evidence would exceed its own limit; the edit is then undone. Review the report before '
        'requesting write-back approval; a new revision invalidates previous approval. '
        'Tested helpers come before your script, in millimetres and degrees, layers as '
        "'F', 'B', 'In1': b = load(); describe(b, ref) gives position, rotation and every pad; "
        'part, move_part(b, ref, x, y), rotate_part(b, ref, deg), pad(b, ref, n), '
        'set_pad(b, ref, n, size=(w, h), at=(x, y)), fields(b, ref), set_field(b, ref, name, value), '
        'tracks(b, net), track_ends(t), '
        'add_track(b, net, [(x, y), ...], width, on), vias(b, net), add_via(b, net, (x, y)), '
        'remove(b, item), zones(b, net, keepout), zone_corners(z), set_zone_corners(z, corners), '
        "a part's drawings by id: graphics(b, ref, on='F.Silk'), set_graphic(b, ref, id, start, "
        "end), remove_graphic(b, ref, id), add_line(b, ref, on, start, end, width); copper "
        'drawn as a shape: copper_shapes(b, net), set_copper_corners(b, id, corners), '
        'remove_copper(b, id), '
        'add_copper(b, net, on, corners), add_taper(b, net, on, narrow, narrow_width, wide, '
        "wide_width); add_track(..., radius=1.0) makes every bend a tangent arc (RF traces, "
        "Friedl 14 Nov); track_length(b, net) in mm, to match P and N; teardrops(b, area) lists "
        "teardrop areas, which a moved part leaves behind as stray copper (remove them); "
        "replace_part(b, ref, 'LIBRARY/NAME', pin_nets={pad: net}) swaps a part for a library "
        "footprint in its place, keeping reference, fields, side and schematic link, after "
        "naming it in footprints so the tool copies it in; footprints and symbols are found in his "
        "library tables, in KiCad's own libraries (power:GND, Device:R) and, for symbols, among those "
        "the project already uses (inspect_schematic lists them) (pads with no net come back as "
        "unconnected); add_part(b, ref, 'LIBRARY/NAME', (x, y), rotation, side, value, pin_nets) "
        "places a part the approved schematic added, or every part of a new board (a net the "
        "board lacks is made); the schematic, which KiCad cannot edit from Python, through "
        "s = sheet('NAME.kicad_sch') (sheets() lists them): s.symbols(), s.symbol(ref) with its "
        "pins {number: (x, y)}, s.wires(), s.labels(); s.swap_symbol(ref, 'LIBRARY:NAME', "
        "value=, footprint=, at=, rotation=) after naming it in symbols (returns old pin points "
        "no new pin lands on), s.remove_symbol(ref) (takes the wiring only it used), "
        "s.tidy(points), s.add_wire(p, q, ...), s.add_label(name, at, angle, kind), "
        "s.add_no_connect(at), s.add_junction(at), s.copy_symbol('#PWR..', at, rotation) for "
        "power and ground, then s.save(); sheet points are in mm on KiCad's 1.27 grid. His way "
        "of drawing is built into s.wire_pin_to(ref, pin, target) (straight out one grid step, "
        "then one turn), s.label_pin(ref, pin, name), s.ground_pin(ref, pin, '#PWR..') and "
        "s.power_pin(...) (on a short wire, GND down, power up), s.pin_direction(ref, pin) and "
        "s.tidy_fields(ref) (reference and value beside the part, the same way every time): use "
        "them rather than placing wires, ports and text by hand; "
        "refill(b) and save(b); on a library copy (copy_library): footprints(lib), "
        "footprint(lib, name), save_footprint(lib, fp). Use them rather than guessing pcbnew names; refill "
        "after every change. Never remove items while looping over pcbnew's own lists, never "
        "Remove a part's drawing through the board, never read an outline that is not there: "
        'all crash KiCad. The script may write only inside the work copy. Friedl\'s layout rules: '
        'copper wider than the pad it leaves or meets joins it through a taper (add_taper), '
        'never a step; where the pad is the wider one, no taper; inside an antenna clearance '
        "follow the manufacturer's geometry exactly. Every check and edit report lists "
        'layout_rules findings (steps where copper meets a narrower pad); clear them, or say '
        'why one stands, before showing a board. Board work goes in Friedl\'s order (D-082): '
        'schematic, then placement, then routing, each shown to him and approved with '
        'approve_step before the next. Until he approves the schematic the board cannot change; '
        'until he approves the placement no copper can be added (parts may move and copper be '
        'removed). An edit that gets ahead is undone with step_not_approved naming the step he '
        'must see. steps gives each step as unchanged, approved, needs_approval or waiting; '
        'changing an approved step needs his approval again, and so does every step after it. '
        'change_report.compared_to_original lists DRC findings his original did not have (new, '
        'new_findings), how many it no longer has (resolved), and schematic/board disagreements '
        'that are new (schematic_vs_board_new) - judge the work by these, not by totals.',
        obj({'work_copy_id': S, 'script': S, 'change': array(MAP), 'footprints': array(S), 'symbols': array(S),
             'dry_run': B, 'revert_to': S, 'checks': B, 'work_copy_revision': S}, ('work_copy_id',)),
        obj({'work_copy_id': S, 'work_copy_revision': S, 'stdout': S, 'stderr': S,
             'change_report': CHANGE_REPORT, 'before': TOTALS, 'after': TOTALS, 'steps': MAP,
             'previewed': B}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('export_kicad_manufacturing',
        'Export Gerbers and drill files from an untainted work copy through KiCad CLI. '
        'Gerbers use the board saved plot settings, including layer selection and Protel '
        'extensions. separate_drills (default false) separates plated and non-plated Excellon files. '
        'Gerbers, drill and positions share one origin: the board\'s drill/place origin when it has one, '
        'else absolute. assembly true adds JLC\'s bom.csv (Comment, Designator, Footprint, LCSC Part #) '
        'and positions.csv (Designator, Mid X, Mid Y, Layer, Rotation) beside the zip, from KiCad\'s own '
        'exports, each part turned and shifted by its library footprint\'s JLC Rotation Offset and JLC '
        'Position Offset; the result lists lines without an LCSC number, corrected parts and bottom-side '
        'parts (check those in JLC\'s preview), and each correction used. work_copy_revision (required): the revision '
        'the files must be of. '
        'output_dir is relative to the original project. Optional renames map generated file '
        'or layer names to exact manufacturer filenames; ambiguous mappings return. Optional '
        'zip_name adds a zip. Publication only adds files and refuses existing targets before '
        'writing. A concurrent collision may leave added files, reported in recovery; never '
        'deletes or overwrites. compare_with is a previous zip/folder under home; comparison '
        'by final filename ignores Gerber comments and creation/generator metadata.',
        obj({'work_copy_id': S, 'output_dir': S, 'renames': MAP, 'zip_name': S, 'compare_with': S,
             'separate_drills': B, 'assembly': B, 'work_copy_revision': S},
            ('work_copy_id', 'output_dir', 'work_copy_revision')),
        obj({'files_written': array(S), 'comparison': COMPARISON, 'staging_path': S, 'origin': S,
             'assembly': MAP}, ('files_written', 'comparison', 'staging_path', 'origin')),
        SideEffect.EFFECTFUL, FAILURES),
    CapabilityDefinition('read_reference_file',
        'Read bounded structured reference facts from an absolute or tilde-relative path '
        'under home, including iCloud Drive. Refuses credential paths and symlink escapes. '
        'Folders list entries; images give dimensions; PDFs give per-page text; Altium '
        'projects give contents and OLE documents give stream names when board records '
        'cannot be decoded; text/Gerber/KiCad give bounded text; offset (characters, default 0) '
        'continues truncated text at offset plus the length already read, PDFs too (offset counts '
        'page text already read). Also reads retained '
        'KiCad edit evidence paths. Zip lists entries or reads '
        'one named entry. iCloud placeholders return not_downloaded. Does not interpret content.',
        obj({'path': S, 'entry': S, 'offset': I}, ('path',)), REFERENCE, SideEffect.NONE, FAILURES),
    CapabilityDefinition('write_back_kicad_board',
        'Write changed KiCad design files (board, schematic, project, rules, libraries) from the exact approved work-copy '
        'revision to its recorded original project. Requires approval of this exact call. '
        'Refuses open boards and changed originals; verifies backups and staged files before '
        'replacement. A board copy is written only when every changed step is approved '
        '(step_not_approved names the one still needed) and the board disagrees with the '
        'schematic nowhere his original did not (schematic_board_differ lists where), and the DRC '
        'finds no error and nothing unconnected that his original did not have (board_not_clean '
        'lists them). On failure, already replaced files may remain; recovery depends on '
        'the returned backups and evidence, with no automatic undo. '
        'Generated reports and renders are excluded. report, when given, is your written '
        'review of this board in Markdown (findings, what you changed, what is still open, '
        'first line its title): after the files are written it is saved, new and never over '
        "an existing one, in the project's docs/reviews folder, under the same approval.",
        obj({'work_copy_id': S, 'work_copy_revision': S, 'project_dir': S, 'report': S},
            ('work_copy_id', 'work_copy_revision', 'project_dir')),
        StructuredSchema(ValueKind.OBJECT, {**RECOVERY.properties, 'report_path': S, 'report_error': S},
                         RECOVERY.required, extra_properties=False),
        SideEffect.EFFECTFUL, FAILURES),
    CapabilityDefinition('analyse_kicad_copper',
        'Read a registered untainted work copy. Ports use two reference/pad endpoints or '
        'two x_mm/y_mm/layer endpoints; a point attaches to the track end whose copper '
        'covers it. Trace each port\'s coil through tracks, arcs, vias and net ties, run '
        'the configured FastHenry at both supplied mesh densities at frequency_hz, and '
        'report per-port inductance, resistance and Q, coupling coefficients, geometry, '
        'board stack-up, mesh convergence and, for square spirals, the Mohan modified '
        'Wheeler cross-check. self_capacitance_estimate_f is the stated self-capacitance '
        'per coil (a few pF for a PCB coil), used for the self-resonance estimate and '
        'reported as stated_estimate. convergence_margin and formula_margin are fractional '
        'limits. Ambiguous geometry, a second route between a port\'s ends, copper of '
        'another net touching a coil, or numerical uncertainty returns needs_judgement. '
        'Never edits a design.',
        obj({'work_copy_id': S, 'ports': array(PORT), 'frequency_hz': N,
             'mesh_densities': array(I), 'convergence_margin': N, 'formula_margin': N,
             'self_capacitance_estimate_f': N}),
        obj({'status': S, 'reasons': array(S), 'geometry': array(MAP), 'stackup': array(MAP),
             'meshes': array(MAP), 'ports': array(MAP), 'coupling': array(MAP),
             'convergence': MAP, 'formula': array(MAP), 'formula_unavailable': array(MAP),
             'conventions': MAP}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('simulate_kicad_circuit',
        'Run configured ngspice on exactly one of a registered untainted work-copy schematic '
        '(exported with KiCad CLI) or a supplied SPICE netlist. model_blocks supply missing '
        'models. analysis.kind is ac, transient or operating_point; measurements.kind is '
        'voltage, current (voltage source branch) or impedance. Impedance requires a single '
        'measurement and AC analysis: inject 1 A positive-to-return and zero other AC sources. '
        'Optional tuning enumerates only supplied R/L/C references and discrete SI values; '
        'tied_references take the tuned part\'s value together, as matched halves of a '
        'balanced network do; '
        'targets use real, imag, magnitude or q, at frequency/time, with inclusive ranges '
        'and error_scale. Returns the 20 best ranked candidates (candidates_omitted counts the '
        'rest), every target error and one-part-at-a-time '
        'tolerance sensitivity. Never selects a candidate or edits a design. Ties and no '
        'acceptable candidate return needs_judgement. Netlists and complete logs accompany results.',
        obj({'work_copy_id': S, 'netlist': S, 'model_blocks': array(S), 'analysis': ANALYSIS,
             'measurements': array(MEASUREMENT), 'tuning': TUNING}, ('analysis', 'measurements')),
        obj({'status': S, 'reasons': array(S), 'curves': MAP, 'candidates': array(MAP),
             'candidates_omitted': I, 'netlist': S, 'log': S, 'conventions': MAP}),
        SideEffect.NONE, FAILURES),
)


INSPECT = CapabilityDefinition('inspect',
    'Read a board without changing anything, and look before you judge: every part '
    '(reference, value, footprint, side, position, rotation, every field such as the '
    'manufacturer part number, and each pad with its net), every net with the pads it '
    'joins and its track and via counts, every zone and keep-out with its layers and '
    'extent, the board outline and the copper layers. The board is project_dir (one '
    'top-level board), board (a .kicad_pcb path) or work_copy_id. refs, nets and contains '
    '(text in a reference, value, footprint or field) narrow the parts and nets; limit '
    '(default 300) bounds each list, with truncated saying when more exist. window [x0, y0, x1, y1] '
    '(mm) adds copper: the tracks (points, width, layer, net), vias and pads (ref, number, net, '
    'centre, size) inside it, narrowed by nets and refs when given: what to route to. brief: '
    'just each part\'s reference, value and footprint, and no nets: the list of what is on the board.',
    obj({'project_dir': S, 'board': S, 'work_copy_id': S, 'refs': array(S), 'nets': array(S),
         'contains': S, 'limit': I, 'window': array(N), 'brief': B}, ()),
    obj({'board': MAP, 'parts': array(MAP), 'nets': array(MAP), 'zones': array(MAP),
         'truncated': MAP, 'copper': MAP}, ('board', 'parts', 'nets', 'zones', 'truncated')),
    SideEffect.NONE, FAILURES)

FETCH_DATASHEET = CapabilityDefinition('fetch_datasheet',
    "A part's datasheet as a PDF, by manufacturer part number (manufacturer narrows it). "
    "Asks DigiKey for its link first, and Nexar (Octopart keeps copies of most datasheets, "
    "but its allowance is small) only when that gives no PDF, "
    'downloads each plainly and keeps the first real PDF, in ALX_Storage/Datasheets, where '
    'check_layout, look and read_reference can read it where it is; never copy it elsewhere '
    "(not to his Desktop). Use it before saying a datasheet "
    'cannot be found; tried lists every source and why it failed.',
    obj({'part_number': S, 'manufacturer': S}, ('part_number',)),
    obj({'path': S, 'source': S, 'url': S, 'tried': array(MAP)}),
    SideEffect.NONE, FAILURES)
COPY_LIBRARY = CapabilityDefinition('copy_library',
    'A work copy of a KiCad library folder (the FireFli library: footprints and symbols), for '
    'adding or correcting parts. Edit it with edit (argv[1] is a file in the copy folder; '
    'footprints(lib), footprint(lib, name) and save_footprint(lib, fp) read and save '
    'footprints; symbol libraries are text, edit them as files), measure a footprint against '
    "its datasheet land pattern with check_layout (footprint LIBRARY/NAME, part FP), and "
    'write it back with write_back (project_dir is the library folder) on his approval; then '
    'commit and push the library repository. Build a new part from the manufacturer\'s '
    'datasheet land pattern, never by copying an imported Altium footprint; compare the '
    'two and show him any difference rather than choosing.',
    obj({'library_dir': S}),
    obj({'work_copy_id': S, 'work_copy_revision': S, 'footprint_libraries': array(MAP),
         'symbol_libraries': array(S)}),
    SideEffect.NONE, FAILURES)
READING = obj({'number': S, 'measures': MAP, 'not_applicable': S,
               'derived': obj({'value_mm': N, 'basis': S})}, ('number',))
LAYOUT_TARGET = {'work_copy_id': S, 'board': S, 'footprint': S, 'document': S, 'page': I,
                 'section': array(N), 'part': S}
CHECK_LAYOUT = CapabilityDefinition('check_kicad_layout',
    "Measure a board, or a library footprint, against the manufacturer's drawing (layout or "
    'land pattern); you never type a measured value. Name the board (work_copy_id, or board '
    'for his; for a footprint, a copy_library work_copy_id with footprint LIBRARY/NAME and '
    'part FP), the datasheet document, page, '
    'section [x0, y0, x1, y1] in points around the drawing, and part (the reference the '
    'drawing is for, e.g. AE2). Every number printed in the section comes back with an id. '
    'For each, give a reading once: measures says what it is on the board, in the part\'s own '
    'directions - {distance: [from, to], axis: x|y|direct}, {pad_size: REF.PAD, axis}, '
    '{copper_width: NET, at: [x, y] mm from the part centre, axis} or {keepout_size: true, '
    'axis} - where a point is REF.PAD:centre|left|right|top|bottom, REF.keepout:left|right|top|bottom (its keep-out), net=NET@x,y:left|right|top|bottom (the edge of that net\'s copper on the line through x,y mm from the part centre) or REF:centre. A shaped '
    'pad dimensioned arm by arm (an L-shaped feed pad): {pad_size: REF.PAD, at: [x, y], axis} '
    'is the width of its copper on the line through that point, and REF.PAD@x,y:left|right|'
    'top|bottom the edge of its copper there; pads sharing a number are one pad; or '
    'not_applicable with a short reason (a tolerance, a size of the part itself). Part values '
    '(1.8 pF) are set aside by the tool. Where the drawing shows a position without printing '
    'a number (a shunt halfway between its series parts), add a reading with number a short '
    'name, derived {value_mm, basis} and measures; it is measured the same way and shown as '
    'derived. Readings are kept per project and drawing and reused. '
    'KiCad measures; within 0.05 mm matches. passed needs no differing and no unaccounted '
    'number, and for an RF or high-speed analog part (rf true) his confirmation of the '
    'readings (confirm_layout_readings). Show it with show_on_screen checked.layout_check = '
    'check_id.',
    obj({**LAYOUT_TARGET, 'readings': array(READING), 'rf': B},
        ('document', 'page', 'section', 'part')),
    obj({'check_id': S, 'work_copy_id': S, 'document': S, 'document_path': S, 'page': I,
         'section': array(N), 'part': S, 'footprint': S,
         'rf': B, 'confirmed': B, 'passed': B, 'reason': S, 'counts': MAP, 'numbers': array(MAP),
         'numbers_omitted': I}),
    SideEffect.NONE, FAILURES)
CONFIRM_READINGS = CapabilityDefinition('confirm_layout_readings',
    "Record that Friedl has confirmed the readings of the layout check he saw (what each "
    'number measures), for an RF or high-speed analog part. check_id is that check. Needs '
    'his approval of this exact call. If the readings changed after that check nothing is '
    'confirmed; changing a reading later needs it again.',
    obj({'check_id': S}),
    obj({'confirmed': B, 'readings': I}), SideEffect.EFFECTFUL, FAILURES)


APPROVE_STEP = CapabilityDefinition('approve_step',
    "Record Friedl's approval of one step of a board work copy, as he saw it on your screen: "
    'step is schematic, placement or routing (D-082). Needs his approval of this exact call; '
    'you may list it with the next write in one go. Ask only after showing him that step, '
    'current against your copy. work_copy_revision is the revision he saw: a later edit '
    'refuses with work_copy_changed. A step needs every step before it unchanged or approved, '
    'else step_not_approved names the one he must see first. A step he has not changed needs no '
    'approval. The approval holds only while that step and those before it stay as he saw '
    'them. Returns every step\'s state.',
    obj({'work_copy_id': S, 'work_copy_revision': S, 'step': S}),
    obj({'step': S, 'steps': MAP, 'work_copy_id': S, 'work_copy_revision': S}),
    SideEffect.EFFECTFUL, FAILURES)


SELECT = CapabilityDefinition('select',
    'Find Similar on a board, without changing anything: every object of one kind that matches all '
    'the filters given, with its address (id) and properties, from one reading of the board per '
    'revision (no KiCad started). kind: part, field (a part\'s Reference, Value or other text on the '
    'board), pad, track, arc, via, zone, keepout, teardrop, text, graphic. Filters: refs, nets, layer '
    '(F, B or a layer name), name (a field\'s name), value, footprint (LIBRARY:NAME or NAME), area '
    '[x0, y0, x1, y1] mm, reads (a board text\'s exact words), ids. count is how many match; objects '
    'lists them up to limit (default 100); named true leaves out parts without a designator (logos). '
    'work_copy_revision is the revision read: pass it to the edit whose set uses the same where, '
    'and the edit is refused if the copy has changed since.',
    obj({'project_dir': S, 'board': S, 'work_copy_id': S, 'kind': S, 'refs': array(S), 'nets': array(S),
         'layer': S, 'name': S, 'value': S, 'footprint': S, 'area': array(N), 'reads': S, 'ids': array(S),
         'named': B, 'limit': I}, ('kind',)),
    obj({'kind': S, 'count': I, 'objects': array(MAP), 'truncated': B, 'board': S, 'work_copy_revision': S},
        ('kind', 'count', 'objects', 'truncated')),
    SideEffect.NONE, FAILURES)

INSPECT_SCHEMATIC = CapabilityDefinition('inspect_schematic',
    "Read a work copy's schematic (work_copy_id), or one of his projects where it is (project_dir, "
    "e.g. P0020), as facts, changing nothing: every part (reference, value, "
    'footprint, sheet, place and rotation) with each pin number, pin name and net; every net '
    "with the pins it joins, from KiCad's own netlist across every sheet; and each sheet's "
    'labels. With refs, each pin also gives at (its point, mm), points (left/right/up/down) and '
    'wired (wiring already ends there). library_symbols (LIBRARY:NAME, up to 10) gives each such symbol\'s pins (number, '
    'name, type, and the part\'s own notes) before it is placed, for a swap. original true reads his project as your copy was made from it. refs, nets and '
    'contains (text in a reference, value or footprint) narrow it; limit (default 200) bounds '
    'each list. Read the schematic this way, never by printing sheet text from an edit script.',
    obj({'work_copy_id': S, 'project_dir': S, 'original': B, 'refs': array(S), 'nets': array(S), 'contains': S,
         'limit': I, 'library_symbols': array(S)}, ()),
    obj({'sheets': array(MAP), 'parts': array(MAP), 'nets': array(MAP), 'library_symbols': array(MAP),
         'truncated': MAP}), SideEffect.NONE, FAILURES)
RENDER = CapabilityDefinition('render',
    "KiCad's own drawing of a board work copy as a PDF in ALX_Storage, to read with look "
    '(whole page first, then a section for detail). what: schematic (every sheet, one page '
    'each), board (copper, silkscreen, fab with every pad outline and number, edges) or '
    'placement (the same without copper: where each part sits and where its pins point). '
    'original true draws his project as your copy was made from it. Look at your work before '
    'you show it or report it.',
    obj({'work_copy_id': S, 'what': S, 'original': B}, ('work_copy_id', 'what')),
    obj({'path': S, 'pages': I, 'what': S, 'original': B}), SideEffect.NONE, FAILURES)
OPEN_IN_KICAD = CapabilityDefinition('open_in_kicad',
    "Show Friedl your copy in KiCad's own window on his screen, in front: what is schematic or "
    'board. beside_original true also opens his design as it is, for when he wants to compare '
    '(windows lists them in order). Each is a fresh copy made for looking, so nothing done in '
    'those windows changes his project or your copy (when he saves an edit there and asks, take_edits '
    'brings it into your copy); windows from an earlier call are closed first. A screenshot of his screen comes back as a path: look at it before you say anything '
    'about it. This is how each step of a board job (schematic, placement, routing) is shown to '
    'him. If he saved an edit in the last window that is not in your copy, a new window is refused '
    'until you take_edits, or he says to drop it (discard_saved_edits true).',
    obj({'work_copy_id': S, 'what': S, 'beside_original': B, 'discard_saved_edits': B}, ('work_copy_id', 'what')),
    obj({'opened': array(S), 'windows': array(S), 'screenshot': S}, ('opened', 'windows', 'screenshot')),
    SideEffect.NONE, FAILURES)

TAKE_EDITS = CapabilityDefinition('take_edits',
    "When Friedl has changed your copy himself in the KiCad window open_in_kicad showed him, and "
    'saved it, and asks you to take his edits: they go into your copy as one kept edit (revert_to '
    'can go back over it), and the parts he moved are listed. Only while your copy is as it was '
    'when that window opened. Go on from his edit; do not redo it.',
    obj({'work_copy_id': S}, ('work_copy_id',)),
    obj({'changed': array(S), 'deleted': array(S), 'moved': array(S), 'moved_total': I, 'work_copy_revision': S,
         'note': S}, ('changed', 'moved', 'work_copy_revision')),
    SideEffect.NONE, FAILURES)


MAKE_HIERARCHICAL = CapabilityDefinition('make_hierarchical',
    "Join a copy's separate top-level schematic sheets (an Altium import) into one "
    'hierarchical design: a new top sheet placing every sheet, symbols and board parts '
    'relinked; then a global label used on more than one sheet becomes pins on those '
    "sheets' blocks with wires (buses for bus labels) between them, as Altium showed "
    "(connected lists them). The sheet that shares signals with the others stands in the "
    "middle and each other block beside it, close in, pins level, so every connection is one "
    "straight wire with its net name on a stub clear of the pins; other patterns are drawn in "
    "two columns with tracks between. top_sheet_crossings counts any crossings. Net labels on "
    "the top sheet are plain labels, as KiCad intends, so those nets take KiCad's hierarchical "
    "name (HALO becomes /HALO); the board, its design rules and net classes are renamed to "
    "match (renamed_nets), as Update PCB from Schematic would. "
    "Works on a design already hierarchical too. Proven by KiCad's "
    "netlist before and after; if any connection or net name "
    'changes, the copy is put back and verification_failed names the nets. Part of every '
    'import; write it back with his approval, and ask him to open it once in KiCad.',
    obj({'work_copy_id': S}),
    obj({'converted': B, 'reason': S, 'connected': array(S), 'carried_by_buses': array(S), 'bus_aliases_renamed': MAP,
         'sheets': array(S),
         'renamed': MAP, 'footprints_relinked': I, 'top_sheet_crossings': I, 'renamed_nets': MAP, 'rename_counts': MAP,
         'footprints_not_found': array(S), 'nets': I, 'netlist_identical': B, 'work_copy_id': S,
         'work_copy_revision': S, 'schematic_parity': array(GROUP), 'erc': ERC},
        ('converted', 'reason', 'connected', 'sheets', 'renamed', 'footprints_relinked', 'footprints_not_found')),
    SideEffect.NONE, FAILURES)


class KiCadAction(str, Enum):
    """Every KiCad job, a closed list like the repository operations (D-074)."""

    INSPECT = 'inspect'
    CHECK = 'check'
    EDIT = 'edit'
    EXPORT = 'export'
    READ_REFERENCE = 'read_reference'
    WRITE_BACK = 'write_back'
    ANALYSE_COPPER = 'analyse_copper'
    SIMULATE = 'simulate'
    CHECK_LAYOUT = 'check_layout'
    CONFIRM_LAYOUT_READINGS = 'confirm_layout_readings'
    MAKE_HIERARCHICAL = 'make_hierarchical'
    COPY_LIBRARY = 'copy_library'
    FETCH_DATASHEET = 'fetch_datasheet'
    APPROVE_STEP = 'approve_step'
    RENDER = 'render'
    INSPECT_SCHEMATIC = 'inspect_schematic'
    SELECT = 'select'
    OPEN_IN_KICAD = 'open_in_kicad'
    TAKE_EDITS = 'take_edits'


# Each action keeps its own contract; the capability is one (D-074).
CONTRACTS = {
    KiCadAction.INSPECT: INSPECT,
    KiCadAction.CHECK: _ACTION_DEFINITIONS[0],
    KiCadAction.EDIT: _ACTION_DEFINITIONS[1],
    KiCadAction.EXPORT: _ACTION_DEFINITIONS[2],
    KiCadAction.READ_REFERENCE: _ACTION_DEFINITIONS[3],
    KiCadAction.WRITE_BACK: _ACTION_DEFINITIONS[4],
    KiCadAction.ANALYSE_COPPER: _ACTION_DEFINITIONS[5],
    KiCadAction.SIMULATE: _ACTION_DEFINITIONS[6],
    KiCadAction.CHECK_LAYOUT: CHECK_LAYOUT,
    KiCadAction.CONFIRM_LAYOUT_READINGS: CONFIRM_READINGS,
    KiCadAction.MAKE_HIERARCHICAL: MAKE_HIERARCHICAL,
    KiCadAction.COPY_LIBRARY: COPY_LIBRARY,
    KiCadAction.FETCH_DATASHEET: FETCH_DATASHEET,
    KiCadAction.APPROVE_STEP: APPROVE_STEP,
    KiCadAction.RENDER: RENDER,
    KiCadAction.INSPECT_SCHEMATIC: INSPECT_SCHEMATIC,
    KiCadAction.SELECT: SELECT,
    KiCadAction.OPEN_IN_KICAD: OPEN_IN_KICAD,
    KiCadAction.TAKE_EDITS: TAKE_EDITS,
}
# What changes nothing of Friedl's: reads, and edits of her own work copies.
FREE_ACTIONS = tuple(item.value for item in (
    KiCadAction.INSPECT, KiCadAction.CHECK, KiCadAction.EDIT, KiCadAction.READ_REFERENCE,
    KiCadAction.ANALYSE_COPPER, KiCadAction.SIMULATE, KiCadAction.CHECK_LAYOUT,
    KiCadAction.MAKE_HIERARCHICAL, KiCadAction.COPY_LIBRARY, KiCadAction.FETCH_DATASHEET,
    KiCadAction.RENDER, KiCadAction.OPEN_IN_KICAD, KiCadAction.INSPECT_SCHEMATIC, KiCadAction.TAKE_EDITS,
    KiCadAction.SELECT))
KICAD = 'kicad'
FAILURES += ('board_unavailable', 'section_failed', 'section_too_large', 'document_unavailable',
             'page_failed')

_FIELDS = {}
for _definition in CONTRACTS.values():
    _FIELDS.update(_definition.input_schema.properties)

DEFINITION = CapabilityDefinition(KICAD,
    "KiCad PCB or schematic: inspect the board, read the part's datasheet in the project's "
    'datasheets folder (read_reference) before judging or searching the web, show layout '
    'findings with show_on_screen. inspect gives every part with its value and part number, '
    'nets and keep-outs; read_reference reads PDFs page by page. Then edit a work copy, '
    'check it (DRC), analyse copper, simulate a circuit, export manufacturing files or apply '
    'an approved copy to his project. action names which; its fields are listed per action '
    'below. export, write_back, approve_step and confirm_layout_readings need his approval of the exact '
    'call; every other action is free. Each action below lists its fields and the ones it requires. ' + ' '.join(
        f'[{name.value}] fields: {", ".join(definition.input_schema.properties) or "none"}; '
        f'required: {", ".join(definition.input_schema.required) or "none"}. {definition.purpose}'
        for name, definition in CONTRACTS.items()),
    StructuredSchema(ValueKind.OBJECT, {'action': S, **_FIELDS}, ('action',), extra_properties=False),
    obj({'action': S, 'result': MAP}),
    SideEffect.EFFECTFUL, FAILURES,
    trace_fields=('action',),
    read_only_when=tuple((('action', name),) for name in FREE_ACTIONS),
)
DEFINITIONS = (DEFINITION,)


def definition_for(actions):
    """The kicad capability as an agent holding only these actions reads it (D-083).

    The same capability and dispatch; only the description lists less, so an
    agent re-reads its own actions at every step and not all of hers.
    """
    chosen = {name: definition for name, definition in CONTRACTS.items() if name.value in actions}
    return replace(DEFINITION, purpose=(
        "KiCad PCB or schematic, on your work copies. action names which; each action below lists "
        "its fields and the ones it requires. " + ' '.join(
            f'[{name.value}] fields: {", ".join(definition.input_schema.properties) or "none"}; '
            f'required: {", ".join(definition.input_schema.required) or "none"}. {definition.purpose}'
            for name, definition in chosen.items())))


def build_kicad_executors(provider, call_id_source):
    methods = {KiCadAction.INSPECT: provider.inspect, KiCadAction.CHECK: provider.check,
               KiCadAction.EDIT: provider.edit, KiCadAction.EXPORT: provider.export,
               KiCadAction.READ_REFERENCE: provider.read_reference,
               KiCadAction.WRITE_BACK: provider.write_back,
               KiCadAction.ANALYSE_COPPER: provider.analyse_copper,
               KiCadAction.SIMULATE: provider.simulate_circuit,
               KiCadAction.CHECK_LAYOUT: provider.check_layout,
               KiCadAction.CONFIRM_LAYOUT_READINGS: provider.confirm_layout_readings,
               KiCadAction.MAKE_HIERARCHICAL: provider.make_hierarchical,
               KiCadAction.COPY_LIBRARY: provider.copy_library,
               KiCadAction.FETCH_DATASHEET: provider.fetch_datasheet,
               KiCadAction.APPROVE_STEP: provider.approve_step,
               KiCadAction.RENDER: provider.render,
               KiCadAction.INSPECT_SCHEMATIC: provider.inspect_schematic,
               KiCadAction.SELECT: provider.select,
               KiCadAction.OPEN_IN_KICAD: provider.open_in_kicad,
               KiCadAction.TAKE_EDITS: provider.take_edits}

    def invoke(arguments):
        code = 'arguments_unusable'
        failure = {}
        try:
            action = KiCadAction(arguments.get('action'))
        except ValueError:
            action = None
        contract = CONTRACTS.get(action)
        values = {key: value for key, value in arguments.items() if key != 'action'}
        if contract is not None and contract.input_schema.accepts(values):
            try:
                result = methods[action](values)
                if not contract.output_schema.accepts(result):
                    raise ValueError('invalid_report')
                return CapabilityResult(call_id_source(), KICAD, CapabilityResultState.SUCCEEDED,
                                        {'action': action.value, 'result': result})
            except Exception as error:
                code = (str(error) if str(error) in FAILURES else
                        'unsupported_project' if isinstance(error, (KeyError, ValueError, TypeError, IndexError))
                        else 'unavailable')
                recovery = getattr(error, 'recovery', None)
                if recovery is not None:
                    failure['recovery'] = recovery
                elif str(error) not in FAILURES:
                    # An unexpected fault says what it was, so it is not guessed at
                    # (15 Nov: seven bare unsupported_project replies in a row).
                    failure['fault'] = ('%s: %s' % (type(error).__name__, error))[:300]
        elif contract is None:
            failure['reason'] = 'action must be one of ' + ', '.join(item.value for item in KiCadAction)
        else:
            # Which fields are wrong, so they are not guessed at (16 Nov: a dozen bare
            # arguments_unusable replies, mostly project_dir or original sent where they are not taken).
            schema = contract.input_schema
            allowed = sorted(getattr(schema, 'properties', {}) or {})
            unknown = sorted(set(values) - set(allowed))
            missing = sorted(set(getattr(schema, 'required', ()) or ()) - set(values))
            parts = []
            if unknown:
                parts.append('not taken by %s: %s' % (action.value, ', '.join(unknown)))
            if missing:
                parts.append('needed: %s' % ', '.join(missing))
            if not parts:
                parts.append('a field has the wrong type')
            failure['recovery'] = {'detail': '; '.join(parts) + '. %s takes: %s' % (action.value, ', '.join(allowed))}
        return CapabilityResult(call_id_source(), KICAD, CapabilityResultState.FAILED,
                                failure=dict(failure, code=code))
    return {KICAD: invoke}
