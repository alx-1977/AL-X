"""Structured general program execution; meaning and permission stay with Core."""

from datetime import UTC, datetime

from alx.contracts import (
    CapabilityDefinition, CapabilityResult, CapabilityResultState, SideEffect,
    StructuredSchema, ValueKind, ContentOrigin, RetentionPolicy,
)

RUN_PROGRAM = "run_program"
_S = StructuredSchema(ValueKind.STRING)
_N = StructuredSchema(ValueKind.NUMBER)
DEFINITION = CapabilityDefinition(
    RUN_PROGRAM,
    "Execute argv directly as Friedl's user with network access (D-063). "
    "Access is not permission: retain D-060 approvals for spending, sending, "
    "deleting, posting, paying, governance and Friedl's files. Never access "
    "banking or Keychain. Use service APIs directly when keys exist; ask for "
    "the specific missing key otherwise. Long runs return a reference and "
    "completion arrives as untrusted evidence at a Core occasion.",
    StructuredSchema(ValueKind.OBJECT, {
        "argv": StructuredSchema(ValueKind.ARRAY, items=_S),
        "working_directory": _S, "time_limit_seconds": _N,
    }, ("argv", "working_directory", "time_limit_seconds"), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, {
        "run_id": _S, "status": _S, "exit_code": StructuredSchema(ValueKind.INTEGER),
        "duration_seconds": _N, "stdout": _S, "stderr": _S,
        "full_output_path": _S, "stdout_path": _S, "stderr_path": _S,
        "timed_out": StructuredSchema(ValueKind.BOOLEAN), "error": _S,
    }, ("run_id", "status", "full_output_path"), extra_properties=False),
    SideEffect.EFFECTFUL,
    ("arguments_unusable", "target_prohibited", "program_unavailable"),
    durable_input_fields=(),
)


def build_program_executors(run, call_id_source):
    def execute(arguments):
        call_id = call_id_source()
        try:
            values = run(arguments)
        except PermissionError as error:
            return CapabilityResult(call_id, RUN_PROGRAM, CapabilityResultState.FAILED,
                                    failure={"code": "target_prohibited", "detail": str(error)})
        except (ValueError, TypeError, KeyError):
            return CapabilityResult(call_id, RUN_PROGRAM, CapabilityResultState.FAILED,
                                    failure={"code": "arguments_unusable"})
        except OSError as error:
            return CapabilityResult(call_id, RUN_PROGRAM, CapabilityResultState.FAILED,
                                    failure={"code": "program_unavailable", "detail": str(error)})
        return CapabilityResult(
            call_id, RUN_PROGRAM, CapabilityResultState.SUCCEEDED, values,
            durable_values={k: v for k, v in values.items() if k not in ("stdout", "stderr")},
            provenance=RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, datetime.now(UTC)),
        )
    return {RUN_PROGRAM: execute}
