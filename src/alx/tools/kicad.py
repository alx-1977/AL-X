"""Single-outcome KiCad capabilities; structured evidence returns to Core."""
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
CHANGE_REPORT = obj({'files_changed': array(S), 'files_truncated': B,
    'file_diffs': array(obj({'name': S, 'diff': S, 'truncated': B})), 'diffs_truncated': B, 'board': obj({
    'layers': SECTION, 'setup': SECTION, 'stackup': SECTION,
    'items_added': array(BOARD_ITEM), 'items_removed': array(BOARD_ITEM),
    'layer_moves': array(obj({'id': S, 'kind': S, 'before': array(S), 'after': array(S)})),
    'added_count': I, 'removed_count': I, 'moved_count': I, 'truncated': B,
    'per_layer_before': MAP, 'per_layer_after': MAP}), 'full_diff_path': S, 'before_copy_path': S,
    'after_copy_path': S},
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

DEFINITIONS = (
    CapabilityDefinition('check_kicad_board',
        'Run all-severity KiCad DRC with zone refill on a new isolated project copy. '
        'Requires exactly one top-level board. Returns grouped facts, configured noise flags '
        'and revision for exact write approval. custom_rule_citations counts rule names '
        'cited in violations, not all loaded rules; does not interpret findings.',
        obj({'project_dir': S}), obj({'work_copy_id': S, 'work_copy_revision': S,
            'custom_rule_citations': RULE_CITATIONS, 'work_copy_tainted': B, 'baseline_eligible': B,
            'totals': TOTALS, 'violations': array(GROUP), 'unconnected_items': array(GROUP),
            'schematic_parity': array(GROUP)}), SideEffect.NONE, FAILURES),
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
        'requesting write-back approval; a new revision invalidates previous approval.',
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
        'Write changed board/project/custom-rule files from the exact approved work-copy '
        'revision to its recorded original project. Requires approval of this exact call. '
        'Refuses open boards and changed originals; verifies backups and staged files before '
        'replacement. On failure, already replaced files may remain; recovery depends on '
        'the returned backups and evidence, with no automatic undo. '
        'Generated reports and renders are excluded.',
        obj({'work_copy_id': S, 'work_copy_revision': S, 'project_dir': S}),
        RECOVERY, SideEffect.EFFECTFUL, FAILURES),
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


def build_kicad_executors(provider, call_id_source):
    def bind(definition, execute):
        def invoke(arguments):
            code = 'arguments_unusable'
            failure = {}
            if definition.input_schema.accepts(arguments):
                try:
                    values = execute(arguments)
                    if not definition.output_schema.accepts(values):
                        raise ValueError('invalid_report')
                    return CapabilityResult(call_id_source(), definition.capability_id,
                                            CapabilityResultState.SUCCEEDED, values)
                except Exception as error:
                    code = (str(error) if str(error) in FAILURES else
                            'unsupported_project' if isinstance(error, (KeyError, ValueError, TypeError, IndexError))
                            else 'unavailable')
                    recovery = getattr(error, 'recovery', None)
                    if recovery is not None:
                        failure['recovery'] = recovery
            return CapabilityResult(call_id_source(), definition.capability_id,
                                    CapabilityResultState.FAILED, failure=dict(failure, code=code))
        return invoke
    return {definition.capability_id: bind(definition, method) for definition, method in zip(
        DEFINITIONS, (provider.check, provider.edit, provider.export, provider.read_reference,
                      provider.write_back, provider.analyse_copper, provider.simulate_circuit))}
