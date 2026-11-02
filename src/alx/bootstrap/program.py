"""Compose the single D-063 program capability with standing authority."""

from alx.providers.program import ProgramRunner
from alx.safety import AuthorityPolicy
from alx.tools.program import DEFINITION, RUN_PROGRAM, build_program_executors


def build_program_runtime(root, banking_targets, call_id_source, conversation_id_source):
    runner = ProgramRunner(root, banking_targets)
    executors = build_program_executors(
        lambda arguments: runner.run(arguments, conversation_id_source()), call_id_source,
    )
    return (DEFINITION,), {RUN_PROGRAM: AuthorityPolicy(frozenset({"program.execute"}))}, executors
