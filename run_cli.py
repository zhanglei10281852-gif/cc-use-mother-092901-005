"""命令行冒烟：演示多人共车、同意撤回、合法保留与删除作业的完整链路。"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from consent_governance import (
    ConsentDecision,
    GovernanceBackend,
    JobStatus,
    Operation,
    Purpose,
)

t0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
backend = GovernanceBackend(":memory:", now_fn=lambda: t0)

voice = Purpose("voice-personalization", "语音偏好", frozenset({
    Operation.COLLECT, Operation.USE, Operation.SHARE, Operation.EXPORT,
}))
investigation = Purpose(
    "accident-investigation", "事故调查", frozenset({Operation.USE, Operation.EXPORT}),
    legal_investigation=True,
)
backend.register_purpose(voice)
backend.register_purpose(investigation)
backend.register_vehicle("VIN-7")
backend.register_account("user-8")
backend.register_account("user-9")

# 同一辆车，两个驾驶人轮换
backend.grant("user-8", voice.code, t0)
backend.start_session("S-8", "VIN-7", "user-8", t0)
rec = backend.collect("R-1", "user-8", "VIN-7", "S-8", voice.code, "voice-profile", t0)
backend.end_session("S-8", t0 + timedelta(hours=1))

backend.start_session("S-9", "VIN-7", "user-9", t0 + timedelta(hours=2))
cross = backend.evaluate_access(
    "user-9", Operation.USE, voice.code, at=t0 + timedelta(hours=2), record_id="R-1"
)

# 撤回后历史访问仍可解释，新用途被阻止
withdrawn = backend.withdraw("user-8", voice.code, t0 + timedelta(days=1))
after = backend.evaluate_access(
    "user-8", Operation.USE, voice.code,
    at=t0 + timedelta(days=1, hours=1), record_id="R-1",
)

# 删除遇到事故调查保留：挂起；到期 tick 后自动完成
job = backend.request_delete("user-8", "req-1", t0 + timedelta(days=2))
backend.place_hold("R-1", t0 + timedelta(days=30), "事故调查 #A21", at=t0 + timedelta(days=2))
held = backend.run_delete(job.job_id, t0 + timedelta(days=2))
resumed = backend.tick(t0 + timedelta(days=31))
done = backend.get_delete_job(job.job_id)

print(json.dumps({
    "collected": rec.record_id,
    "cross_driver_use_allowed": cross.allowed,
    "cross_driver_reason": cross.reasons[-1],
    "withdrawn_version": withdrawn.version,
    "use_after_withdraw_allowed": after.allowed,
    "delete_while_held": held.status.value,
    "hold_expired_tick": resumed,
    "delete_final": done.status.value,
    "record_final": backend.repo.get_record("R-1").status.value,
}, ensure_ascii=False, indent=2))
backend.close()
