"""Structured local Particle capabilities authorised by D-062."""
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


FAILURES = ('arguments_unusable', 'repository_denied', 'revision_mismatch',
            'toolchain_unavailable', 'platform_unavailable', 'cli_unavailable',
            'usb_list_failed', 'usb_device_count', 'device_mismatch', 'binary_untrusted',
            'serial_failed', 'local_failed')
COMPILE_RESULT = obj({'succeeded': B, 'errors': array(obj({'file': S, 'line': I, 'message': S})),
                      'warning_count': I, 'binary': S, 'size': I, 'duration_seconds': N})
DEFINITIONS = (
    CapabilityDefinition('compile_particle_firmware',
        'Compile an allowlisted firmware repository clone locally. repository selects the configured '
        'clone; job_id selects that firmware coding job clone. revision, if supplied, must match '
        'its current HEAD. Defaults: b5som, Device OS 6.4.1, buildscripts 1.17.2. Installed '
        'gcc-arm 10.2.1 and buildtools are required. Timeout 600 seconds. Returns at most 40 '
        'compiler/linker errors, warning count and a proven binary artifact, never the build log.',
        obj({'repository': S, 'job_id': S, 'revision': S, 'platform': S, 'device_os': S,
             'buildscripts': S}, ('repository',)), COMPILE_RESULT, SideEffect.NONE, FAILURES),
    CapabilityDefinition('list_particle_usb_devices',
        'List connected Particle USB devices with their IDs, names, platforms and modes.',
        obj({}), obj({'devices': array(obj({'device_id': S, 'name': S, 'platform': S, 'mode': S}))}),
        SideEffect.NONE, FAILURES),
    CapabilityDefinition('flash_bench_reader_usb',
        'Flash an unchanged binary returned by compile_particle_firmware to the single USB '
        'bench reader. Standing authority D-062; no per-call approval. Refuses zero/multiple '
        'devices or expected_device_id mismatch. Uses only local USB, never OTA.',
        obj({'binary': S, 'expected_device_id': S}, ('binary',)),
        obj({'device_id': S, 'binary': S, 'outcome': S, 'error': S}), SideEffect.EFFECTFUL, FAILURES),
    CapabilityDefinition('read_particle_serial_log',
        'Capture USB Particle serial output for seconds (integer 1–60, default 10). '
        'Requires one USB device and optional expected_device_id match. Returns at most '
        '32768 bytes of decoded text and a truncated flag.',
        obj({'seconds': I, 'expected_device_id': S}, ()), obj({'text': S, 'truncated': B}),
        SideEffect.NONE, FAILURES),
)


def build_particle_local_executors(provider, call_id_source):
    methods = (provider.compile, provider.usb_devices, provider.flash, provider.serial)

    def executor(definition, method):
        def execute(arguments):
            if not definition.input_schema.accepts(arguments):
                return CapabilityResult(call_id_source(), definition.capability_id,
                                        CapabilityResultState.FAILED,
                                        failure={'code': 'arguments_unusable'})
            try:
                values = method(**arguments)
                if not definition.output_schema.accepts(values):
                    raise ValueError('invalid_report')
            except Exception as error:
                code = getattr(error, 'code', 'local_failed')
                return CapabilityResult(call_id_source(), definition.capability_id,
                                        CapabilityResultState.FAILED,
                                        failure={'code': code if code in FAILURES else 'local_failed'})
            return CapabilityResult(call_id_source(), definition.capability_id,
                                    CapabilityResultState.SUCCEEDED, values)
        return execute
    return {definition.capability_id: executor(definition, method)
            for definition, method in zip(DEFINITIONS, methods, strict=True)}
