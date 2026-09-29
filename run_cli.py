import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from consent_governance.contracts import ConsentDecision, ConsentVersion, OccupantSession


session = OccupantSession("S-1", "VIN-2", "user-8", datetime(2026, 10, 21, tzinfo=timezone.utc))
consent = ConsentVersion("C-1", session.account_id, 2, "assistant-personalization", ConsentDecision.GRANTED)
print(json.dumps({"session": session.session_id, "purpose": consent.purpose, "decision": consent.decision.value}, ensure_ascii=False))
