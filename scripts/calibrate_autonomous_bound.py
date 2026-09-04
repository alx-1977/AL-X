"""Measure our input-bound heuristic against a real provider's token count.

`input_token_upper_bound` is a byte-length heuristic — the serialized request
plus a fixed framing allowance — and it assumes at least one encoded byte per
token. Every autonomous safety property rests on that assumption: the 96,000
ceiling decides whether a turn may run, and the worst case decides what is
withdrawn before it does. If real tokenization is denser than one token per
byte, both are wrong in the same direction and the fuse under-protects.

Nothing in the test suite can settle that, because no fake knows how a real
model tokenizes. So this asks one real model one real question and compares.

It is a provider-contract calibration and nothing else. It creates no cognition
opportunity, claims no request, touches neither the autonomous spend ledger nor
any continuity state, starts no polling, enables no autonomous cognition, and
speaks nothing. It builds the exact request the production Core would send and
sends it once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.live_voice import load_environment  # noqa: E402
from alx.bootstrap.reasoning import build_model_reasoner  # noqa: E402
from alx.config import (  # noqa: E402
    AUTONOMOUS_MAX_INPUT_TOKENS,
    AUTONOMOUS_MAX_OUTPUT_TOKENS,
)
from alx.contracts import CognitionOrigin, ReasoningContext  # noqa: E402
from alx.contracts.continuity import CarriedThought  # noqa: E402
from alx.contracts.models import input_token_upper_bound  # noqa: E402
from alx.contracts.usage import normalise_usage  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from alx.observability.pricing import cost_usd, price_of  # noqa: E402
from alx.providers.openai import OpenAIReasoningModel  # noqa: E402

PROVIDER = "openai"
MODEL = "gpt-5.6-luna"
EFFORT = "max"


def _capabilities():
    """The real registered catalogue, exactly as the runtime composes it."""
    from alx.tools import (
        CONTINUITY_DEFINITIONS, NOTEBOOK_DEFINITIONS, RESEARCH_DEFINITION,
    )
    from alx.tools.mail import DEFINITIONS as MAIL

    definitions = (
        tuple(NOTEBOOK_DEFINITIONS) + (RESEARCH_DEFINITION,)
        + tuple(CONTINUITY_DEFINITIONS) + tuple(MAIL)
    )
    for module, name in (("alx.tools.xero", "DEFINITIONS"), ("alx.tools.dhl", "DEFINITIONS")):
        try:
            definitions += tuple(getattr(__import__(module, fromlist=[name]), name))
        except Exception:
            pass
    return definitions


def _context(now):
    """A realistic autonomous turn: full catalogue, thoughts, real origin."""
    from datetime import timedelta

    thoughts = tuple(
        CarriedThought(
            f"thought-{index}",
            "Something I have not finished thinking about, written at the "
            "length these usually run to when they are worth keeping.",
            now - timedelta(hours=index),
        )
        for index in range(1, 6)
    )
    return ReasoningContext(
        None,
        (),
        _capabilities(),
        conversation_id="calibration",
        origin=CognitionOrigin.SELF_REQUESTED,
        carried_thoughts=thoughts,
    )


def main() -> int:
    from datetime import UTC, datetime

    environment = load_environment(ROOT / ".env")
    api_key = environment.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not configured; cannot calibrate.")
        return 2

    now = datetime.now(UTC)
    context = _context(now)

    # Built by the production builder, so the measured request is the one a
    # real autonomous turn would send. No simplified calibration prompt.
    reasoner = build_model_reasoner(
        OpenAIReasoningModel(
            MODEL,
            api_key,
            environment.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/"),
            int(environment.get("ALX_AUTONOMOUS_TIMEOUT_SECONDS", "300")),
            streaming=False,
            reasoning_effort=EFFORT,
        ),
        ROOT,
        AUTONOMOUS_MAX_OUTPUT_TOKENS,
        AUTONOMOUS_MAX_INPUT_TOKENS,
        _NullAuthority(),
    )
    request = reasoner.build_request(context)

    serialized = json.dumps(
        [{"role": m.role.value, "content": m.content} for m in request.messages],
        ensure_ascii=False,
    ).encode("utf-8")
    predicted = input_token_upper_bound(request)
    rate = price_of(PROVIDER, MODEL)
    worst = (
        None if rate is None
        else round(
            AUTONOMOUS_MAX_INPUT_TOKENS / 1e6 * (rate.uncached_input + (rate.cache_write or 0.0))
            + AUTONOMOUS_MAX_OUTPUT_TOKENS / 1e6 * rate.output,
            6,
        )
    )

    print("=" * 66)
    print("BEFORE DISPATCH")
    print("=" * 66)
    print(f"  provider / model / effort : {PROVIDER} / {MODEL} / {EFFORT}")
    print(f"  serialized request bytes  : {len(serialized):,}")
    print(f"  input_token_upper_bound   : {predicted:,}")
    print(f"  configured input ceiling  : {AUTONOMOUS_MAX_INPUT_TOKENS:,}")
    print(f"  max_output_tokens sent    : {request.max_output_tokens:,}")
    print(f"  predicted worst case      : ${worst}")
    schema_bytes = len(json.dumps(request.output_schema, default=dict))
    print(f"  strict schema bytes       : {schema_bytes:,}")
    if predicted > AUTONOMOUS_MAX_INPUT_TOKENS:
        print("\n  REFUSED before dispatch: request exceeds the ceiling.")
        return 1

    print("\nDispatching one call...\n")
    completion = reasoner._model.complete(request)

    # Written before any post-processing. A calibration whose evidence is lost
    # to a formatting bug has spent real money for nothing, which is exactly
    # what happened on the first attempt.
    # ModelCompletion freezes output and usage into mappingproxy, which is a
    # Mapping but not a dict. Checking for dict here silently replaced a real
    # usage report with {} and threw away the measurement this call was made
    # to obtain. Production never had this bug: it checks Mapping.
    usage = dict(completion.usage) if isinstance(completion.usage, Mapping) else {}
    output_is_mapping = isinstance(completion.output, Mapping)
    evidence = ROOT / ".alx" / "calibration-luna.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "provider": completion.provider,
                "model": completion.model,
                "raw_usage": usage,
                "predicted_input": predicted,
                "serialized_bytes": len(serialized),
                "max_output_tokens_sent": request.max_output_tokens,
                "output_is_mapping": output_is_mapping,
                "output_keys": sorted(completion.output) if output_is_mapping else None,
                "output_type": type(completion.output).__name__,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"  raw evidence saved to {evidence}\n")
    normalised = normalise_usage(usage)
    actual_input = normalised.get("input_tokens", 0)
    actual = cost_usd(completion.provider, completion.model, normalised)

    print("=" * 66)
    print("AFTER RETURN")
    print("=" * 66)
    print(f"  provider / model reported : {completion.provider} / {completion.model}")
    print(f"  raw usage field names     : {sorted(usage.keys())}")
    print(f"  raw usage                 : {json.dumps(usage)[:400]}")
    print(f"  normalised usage          : {normalised}")
    print(f"  calculated actual cost    : ${actual}")
    print(f"  strict schema accepted    : {output_is_mapping}")
    print(f"  output keys returned      : {sorted(completion.output)[:8] if output_is_mapping else 'n/a'}")
    out = normalised.get("output_tokens", 0)
    print(f"  output tokens             : {out:,} (bound {AUTONOMOUS_MAX_OUTPUT_TOKENS:,}; "
          f"{'within' if out <= AUTONOMOUS_MAX_OUTPUT_TOKENS else 'EXCEEDED'})")

    print("\n" + "=" * 66)
    print("THE QUESTION")
    print("=" * 66)
    print(f"  our heuristic predicted   : {predicted:,}")
    print(f"  provider billed for input : {actual_input:,}")
    if actual_input:
        margin = predicted - actual_input
        ratio = predicted / actual_input
        print(f"  margin                    : {margin:+,} ({ratio:.2f}x)")
        safe = predicted >= actual_input
        print(f"\n  CONSERVATIVE (>= actual)  : {'YES' if safe else 'NO'}")
        if not safe:
            print("\n  STOP. The heuristic under-counts real tokens, so the 96k")
            print("  ceiling and the reservation both under-protect. Phase 8 must")
            print("  not be activated until the bound is replaced.")
            return 1
    else:
        print("\n  Provider reported no input tokens; cannot conclude.")
        return 1
    return 0


class _NullAuthority:
    """Satisfies the reasoner's bound/authority pairing without spending.

    The autonomous spend ledger is deliberately untouched: this is a provider
    contract measurement, not an autonomous turn, and it must not consume the
    daily fuse or appear in the occasion record.
    """

    def reserve(self, max_input_tokens: int, max_output_tokens: int):
        return "calibration"

    def mark_dispatched(self, reservation) -> None:
        return None

    def settle(self, reservation, usage) -> float:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
