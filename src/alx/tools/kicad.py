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
            'project_too_large', 'verification_failed', 'unavailable')

MAP = StructuredSchema(ValueKind.OBJECT)
SECTION = obj({'before': S, 'after': S, 'changed': B, 'truncated': B})
BOARD_ITEM = obj({'id': S, 'kind': S, 'layers': array(S)})
CHANGE_REPORT = obj({'files_changed': array(S), 'files_truncated': B,
    'file_diffs': array(obj({'name': S, 'diff': S, 'truncated': B})), 'diffs_truncated': B, 'board': obj({
    'layers': SECTION, 'setup': SECTION, 'stackup': SECTION,
    'items_added': array(BOARD_ITEM), 'items_removed': array(BOARD_ITEM),
    'layer_moves': array(obj({'id': S, 'kind': S, 'before': array(S), 'after': array(S)})),
    'added_count': I, 'removed_count': I, 'moved_count': I, 'truncated': B,
    'per_layer_before': MAP, 'per_layer_after': MAP})})
COMPARISON = obj({'files': array(obj({'name': S, 'status': S, 'differing_lines': I,
                                    'line_count_truncated': B})), 'truncated': B})
REFERENCE = obj({'kind': S, 'path': S, 'text': S, 'truncated': B, 'entry': S,
    'media_type': S, 'width': I, 'height': I, 'page_count': I,
    'pages': array(obj({'page': I, 'text': S, 'truncated': B})),
    'documents': array(S), 'streams': array(S), 'board_records_decoded': B,
    'entries': array(obj({'name': S, 'kind': S, 'bytes': I}, ('name',)))}, ('kind',))

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
        'restore the copy; uncertain restoration taints it durably. Review the report before '
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
        'cannot be decoded; text/Gerber/KiCad give bounded text. Zip lists entries or reads '
        'one named entry. iCloud placeholders return not_downloaded. Does not interpret content.',
        obj({'path': S, 'entry': S}, ('path',)), REFERENCE, SideEffect.NONE, FAILURES),
    CapabilityDefinition('find_kicad_library_footprint',
        'Read installed library footprint candidates and pad counts. Supply library_id '
        '(Library:Footprint or exact footprint name), or designator and work_copy_id. '
        'Never replaces footprints or selects a candidate.',
        obj({'library_id': S, 'designator': S, 'work_copy_id': S}, ()),
        obj({'candidates': array(obj({'library_id': S, 'path': S, 'pad_count': I}))}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('write_back_kicad_board',
        'Write changed board/project/custom-rule files from the exact approved work-copy '
        'revision to its recorded original project. Requires approval of this exact call. '
        'Refuses open boards and changed originals; verifies backups and staged files before '
        'replacement. On failure, already replaced files may remain; recovery depends on '
        'the returned backups and evidence, with no automatic undo. '
        'Generated reports and renders are excluded.',
        obj({'work_copy_id': S, 'work_copy_revision': S, 'project_dir': S}),
        RECOVERY, SideEffect.EFFECTFUL, FAILURES),
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
        DEFINITIONS, (provider.check, provider.edit, provider.export, provider.read_reference, provider.find, provider.write_back))}
