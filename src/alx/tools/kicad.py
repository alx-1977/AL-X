"""One KiCad capability with an action per job (D-074); structured evidence returns to Core."""
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
FAILURES += ('engine_unavailable', 'engine_timeout', 'engine_failed')

MAP = StructuredSchema(ValueKind.OBJECT)
SECTION = obj({'before': S, 'after': S, 'changed': B, 'truncated': B})
BOARD_ITEM = obj({'id': S, 'kind': S, 'layers': array(S)})
ERC = obj({'checked': B, 'reason': S, 'totals': TOTALS, 'top_level_sheets': I, 'groups': array(obj({
    'check_type': S, 'count': I, 'severity': S, 'omitted_findings': I, 'findings': array(obj({
        'severity': S, 'sheet': S, 'description': S, 'items': array(obj({
            'description': S, 'designator': S, 'position': obj({'x': N, 'y': N}, ())}))}))}))})

LAYOUT_RULES = obj({'findings': array(StructuredSchema(ValueKind.OBJECT)), 'omitted': I,
                    'not_checked': S}, ('findings', 'omitted'))
CHANGE_REPORT = obj({'files_changed': array(S), 'files_truncated': B,
    'file_diffs': array(obj({'name': S, 'diff': S, 'truncated': B})), 'diffs_truncated': B, 'board': obj({
    'layers': SECTION, 'setup': SECTION, 'stackup': SECTION,
    'items_added': array(BOARD_ITEM), 'items_removed': array(BOARD_ITEM),
    'layer_moves': array(obj({'id': S, 'kind': S, 'before': array(S), 'after': array(S)})),
    'added_count': I, 'removed_count': I, 'moved_count': I, 'truncated': B,
    'per_layer_before': MAP, 'per_layer_after': MAP}), 'full_diff_path': S, 'before_copy_path': S,
    'after_copy_path': S, 'layout_rules': LAYOUT_RULES, 'erc': ERC},
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
        'Requires exactly one top-level board. Returns grouped facts, configured noise flags '
        'and revision for exact write approval. custom_rule_citations counts rule names '
        'cited in violations, not all loaded rules; does not interpret findings. A review of his '
        'board ends in corrections on a copy shown as Current and Amended, not in questions to '
        'him (Friedl, 2026-11-12). erc is '
        "KiCad's electrical rules check of the schematic, errors and warnings; Friedl wants "
        'both clean, like the DRC. When erc.top_level_sheets is more than 1 (an Altium import), '
        "KiCad's board-against-schematic check (schematic_parity) reads only the first sheet: "
        'run make_hierarchical on the copy first, and write it back with his approval.',
        obj({'project_dir': S}), obj({'work_copy_id': S, 'work_copy_revision': S,
            'custom_rule_citations': RULE_CITATIONS, 'work_copy_tainted': B, 'baseline_eligible': B,
            'totals': TOTALS, 'violations': array(GROUP), 'unconnected_items': array(GROUP),
            'schematic_parity': array(GROUP), 'erc': ERC, 'layout_rules': LAYOUT_RULES}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('edit_kicad_work_copy',
        'Run bounded Python source with bundled KiCad pcbnew on a registered untainted work '
        'copy. argv[1] is the board. Kernel confines writes to its project folder, including '
        'child processes. May edit board, project and rules freely. Returns a bounded plain '
        'change report, structural board diff and DRC totals before/after. Failed edits '
        'restore the copy; uncertain restoration taints it durably. When diffs_truncated, '
        'full_diff_path holds the complete diff and before_copy_path/after_copy_path copies '
        'from before and after the edit; later edits keep them. Read them with '
        'read_reference_file, continuing long text with offset. evidence_full when retained '
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
        "wide_width); refill(b) and save(b); on a library copy (copy_library): footprints(lib), "
        "footprint(lib, name), save_footprint(lib, fp). Use them rather than guessing pcbnew names; refill "
        "after every change. Never remove items while looping over pcbnew's own lists, never "
        "Remove a part's drawing through the board, never read an outline that is not there: "
        'all crash KiCad. The script may write only inside the work copy. Friedl\'s layout rules: '
        'copper wider than the pad it leaves or meets joins it through a taper (add_taper), '
        'never a step; where the pad is the wider one, no taper; inside an antenna clearance '
        "follow the manufacturer's geometry exactly. Every check and edit report lists "
        'layout_rules findings (steps where copper meets a narrower pad); clear them, or say '
        'why one stands, before showing a board.',
        obj({'work_copy_id': S, 'script': S}),
        obj({'work_copy_id': S, 'work_copy_revision': S, 'stdout': S, 'stderr': S,
             'change_report': CHANGE_REPORT, 'before': TOTALS, 'after': TOTALS}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('export_kicad_manufacturing',
        'Export Gerbers and drill files from an untainted work copy through KiCad CLI. '
        'Gerbers use the board saved plot settings, including layer selection and Protel '
        'extensions. separate_drills (default false) separates plated and non-plated Excellon files. '
        'drill_origin is absolute (default) or plot, the drill/aux origin the Gerbers use when the '
        'board plot settings use it. '
        'output_dir is relative to the original project. Optional renames map generated file '
        'or layer names to exact manufacturer filenames; ambiguous mappings return. Optional '
        'zip_name adds a zip. Publication only adds files and refuses existing targets before '
        'writing. A concurrent collision may leave added files, reported in recovery; never '
        'deletes or overwrites. compare_with is a previous zip/folder under home; comparison '
        'by final filename ignores Gerber comments and creation/generator metadata.',
        obj({'work_copy_id': S, 'output_dir': S, 'renames': MAP, 'zip_name': S, 'compare_with': S,
             'separate_drills': B, 'drill_origin': S},
            ('work_copy_id', 'output_dir')),
        obj({'files_written': array(S), 'comparison': COMPARISON, 'staging_path': S}),
        SideEffect.EFFECTFUL, FAILURES),
    CapabilityDefinition('read_reference_file',
        'Read bounded structured reference facts from an absolute or tilde-relative path '
        'under home, including iCloud Drive. Refuses credential paths and symlink escapes. '
        'Folders list entries; images give dimensions; PDFs give per-page text; Altium '
        'projects give contents and OLE documents give stream names when board records '
        'cannot be decoded; text/Gerber/KiCad give bounded text; offset (characters, default 0) '
        'continues truncated text at offset plus the length already read. Also reads retained '
        'KiCad edit evidence paths. Zip lists entries or reads '
        'one named entry. iCloud placeholders return not_downloaded. Does not interpret content.',
        obj({'path': S, 'entry': S, 'offset': I}, ('path',)), REFERENCE, SideEffect.NONE, FAILURES),
    CapabilityDefinition('write_back_kicad_board',
        'Write changed KiCad design files (board, schematic, project, rules, libraries) from the exact approved work-copy '
        'revision to its recorded original project. Requires approval of this exact call. '
        'Refuses open boards and changed originals; verifies backups and staged files before '
        'replacement. On failure, already replaced files may remain; recovery depends on '
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
    '(default 300) bounds each list, with truncated saying when more exist.',
    obj({'project_dir': S, 'board': S, 'work_copy_id': S, 'refs': array(S), 'nets': array(S),
         'contains': S, 'limit': I}, ()),
    obj({'board': MAP, 'parts': array(MAP), 'nets': array(MAP), 'zones': array(MAP),
         'truncated': MAP}), SideEffect.NONE, FAILURES)

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
    'axis} - where a point is REF.PAD:centre|left|right|top|bottom or REF:centre; or '
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
         'section': array(N), 'part': S,
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
    obj({'converted': B, 'reason': S, 'connected': array(S), 'sheets': array(S), 'renamed': MAP,
         'footprints_relinked': I, 'top_sheet_crossings': I, 'renamed_nets': MAP, 'rename_counts': MAP,
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
}
# What changes nothing of Friedl's: reads, and edits of her own work copies.
FREE_ACTIONS = tuple(item.value for item in (
    KiCadAction.INSPECT, KiCadAction.CHECK, KiCadAction.EDIT, KiCadAction.READ_REFERENCE,
    KiCadAction.ANALYSE_COPPER, KiCadAction.SIMULATE, KiCadAction.CHECK_LAYOUT,
    KiCadAction.MAKE_HIERARCHICAL, KiCadAction.COPY_LIBRARY))
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
    'below. export, write_back and confirm_layout_readings need his approval of the exact '
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
               KiCadAction.COPY_LIBRARY: provider.copy_library}

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
        elif contract is None:
            failure['reason'] = 'action must be one of ' + ', '.join(item.value for item in KiCadAction)
        return CapabilityResult(call_id_source(), KICAD, CapabilityResultState.FAILED,
                                failure=dict(failure, code=code))
    return {KICAD: invoke}
