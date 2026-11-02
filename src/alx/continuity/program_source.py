"""Durable program completion facts join the existing occasion producer."""

import json
from datetime import UTC, datetime
from pathlib import Path

from alx.contracts import BackgroundEvent, ContentOrigin, RetentionPolicy
from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity


class ProgramCompletionSource:
    def __init__(self, root: Path, ledger, enabled: bool = False, *, reconcile):
        self.root, self.ledger, self.enabled = root, ledger, enabled
        self.reconcile_run = reconcile

    def _ready(self):
        for path in self.root.glob("*/request.json"):
            self.reconcile_run(path.parent)
        return tuple(path.parent for path in self.root.glob("*/result.json")
                     if (path.parent / "background").exists()
                     and not (path.parent / "handled").exists())

    def due_opportunities(self):
        if not self.enabled:
            return ()
        result = []
        for root in self._ready():
            identity = "program:" + root.name
            if not self.ledger.exists(identity):
                request = json.loads((root / "request.json").read_text())
                result.append(CognitionOpportunity(
                    identity, CognitionOrigin.WORK_COMPLETED,
                    datetime.fromtimestamp((root / "result.json").stat().st_mtime, UTC),
                    conversation_id=request["conversation_id"], references=(identity,),
                ))
        return tuple(result)

    def contextual_events(self, conversation_id=None):
        events = []
        for root in self._ready():
            request = json.loads((root / "request.json").read_text())
            if conversation_id is not None and request["conversation_id"] != conversation_id:
                continue
            at = datetime.fromtimestamp((root / "result.json").stat().st_mtime, UTC)
            events.append(BackgroundEvent(
                "program-result:" + root.name, "program.completed", at,
                {"run_id": root.name}, json.loads((root / "result.json").read_text()),
                provenance=RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, at),
            ))
        return tuple(events)

    def owns(self, opportunity):
        return opportunity.opportunity_id.startswith("program:")

    def claim(self, opportunity):
        root = self.root / opportunity.opportunity_id.removeprefix("program:")
        if (root / "handled").exists() or not (root / "result.json").exists():
            return False
        return self.ledger.record_created(opportunity)

    def release(self, opportunity):
        self.ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity):
        (self.root / opportunity.opportunity_id.removeprefix("program:") / "handled").touch()

    def recover(self, spend=None):
        if self.enabled:
            self._ready()
        for row in self.ledger.unfinished():
            identity = row["opportunity_id"]
            if not identity.startswith("program:"):
                continue
            if spend is not None and spend.dispatch_started(identity):
                self.ledger.mark_unreconciled(identity)
            else:
                self.ledger.release(identity)
