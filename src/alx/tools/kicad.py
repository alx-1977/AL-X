"""Four single-outcome KiCad capabilities; structured evidence returns to Core."""
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
RECOVERY = obj({'backed_up': array(S), 'written': array(S), 'rollback_status': S,
                'restorations': array(obj({'original': S, 'backup': S, 'restored': B, 'error': S})),
                'remaining_changed': array(S), 'cleanup_errors': array(S),
                'error': obj({'code': S, 'type': S})})
ITEM = obj({'description': S, 'designator': S, 'pad_number': S, 'uuid': S,
            'position': obj({'x': N, 'y': N}, ())})
GROUP = obj({'check_type': S, 'count': I, 'noise': B, 'findings': array(
    obj({'severity': S, 'description': S, 'items': array(ITEM)}))})
FAILURES = ('arguments_unusable', 'path_denied', 'unsupported_project', 'invalid_report',
            'cli_failed', 'cli_timeout', 'render_failed', 'footprint_ambiguous', 'board_open',
            'original_changed', 'work_copy_changed', 'board_structure_changed',
            'project_too_large', 'verification_failed', 'unavailable')

DEFINITIONS = (
    CapabilityDefinition('check_kicad_board',
        'Run all-severity KiCad DRC with zone refill on a new isolated project copy. '
        'Requires exactly one top-level board. Returns grouped facts, configured noise flags '
        'and revision for exact write approval; does not interpret findings.',
        obj({'project_dir': S}), obj({'work_copy_id': S, 'work_copy_revision': S,
            'totals': TOTALS, 'violations': array(GROUP), 'unconnected_items': array(GROUP),
            'schematic_parity': array(GROUP)}), SideEffect.NONE, FAILURES),
    CapabilityDefinition('apply_kicad_mechanical_fixes',
        'On a registered work copy only, optionally raise silkscreen text dimensions to '
        'project minima, ignore configured noise checks, or apply named template rules and '
        'custom rules, or set same_footprint_pad_clearance_mm (finite and > 0) for pads '
        'of the same footprint. Reports that change as same_footprint_pad_clearance with '
        'clearance_mm. Returns actual changes, DRC totals and SVG paths before and after. '
        'Never routes or replaces footprints. Revision changes invalidate write approval.',
        obj({'work_copy_id': S, 'silkscreen_min_text': B, 'ignore_noise_checks': B,
             'apply_template': S, 'same_footprint_pad_clearance_mm': N}, ('work_copy_id',)),
        obj({'work_copy_id': S, 'work_copy_revision': S, 'before': TOTALS, 'after': TOTALS,
             'changes': array(obj({'kind': S, 'value': S, 'clearance_mm': N}, ('kind', 'value'))),
             'before_images': array(S), 'after_images': array(S)}), SideEffect.NONE, FAILURES),
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
        DEFINITIONS, (provider.check, provider.fix, provider.find, provider.write_back))}
