import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance.contracts import ConsentDecision, ConsentVersion, OccupantSession


class ConsentContractTests(unittest.TestCase):
    def test_session_separates_vehicle_and_account(self):
        session = OccupantSession("S-2", "VIN-7", "U-3", datetime.now(timezone.utc))
        self.assertNotEqual(session.vehicle_id, session.account_id)

    def test_consent_requires_purpose(self):
        with self.assertRaises(ValueError):
            ConsentVersion("C-2", "U-3", 1, " ", ConsentDecision.DENIED)


if __name__ == "__main__":
    unittest.main()
