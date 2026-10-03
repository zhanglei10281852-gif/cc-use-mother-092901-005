"""命令行冒烟：演示共车隔离、同意换版、保留冲突与删除恢复。

用法：python run_cli.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from consent_governance import GovernanceService, JobStatus, Operation  # noqa: E402

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def show(title: str, decision) -> None:
    print(f"[{title}] {'获准' if decision.allowed else '拒绝'} "
          f"{decision.operation.value} / {decision.purpose} -> "
          f"{decision.reason_code.value}: {decision.explanation}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = GovernanceService(tmp)

        svc.register_vehicle("VIN-1", at=t(0))
        svc.register_account("alice", at=t(0))
        svc.register_account("bob", at=t(0))
        svc.register_purpose("voice-personalization", "语音偏好")
        svc.register_purpose("trip-summary", "行程摘要")
        svc.register_purpose("training", "训练样本")
        svc.start_session("S-a", "VIN-1", "alice", at=t(1))
        svc.start_session("S-b", "VIN-1", "bob", at=t(10))

        # 1) 两个驾驶人各自授权、各自采集，互不可见
        svc.grant_consent("alice", "voice-personalization", at=t(2))
        svc.grant_consent("bob", "voice-personalization", at=t(11))
        da = svc.collect("alice", "voice-personalization", "voice-profile",
                         at=t(3), session_id="S-a", record_id="Ra")
        db = svc.collect("bob", "voice-personalization", "voice-profile",
                         at=t(12), session_id="S-b", record_id="Rb")
        show("alice 采集", da)
        show("bob 采集", db)
        cross = svc.evaluate_access(Operation.USE, "bob", "voice-personalization",
                                    at=t(13), record_id="Ra", session_id="S-b")
        show("bob 跨账号访问 alice 记录", cross)

        # 2) 同意换版：授予 → 撤回 → 历史仍可解释
        svc.grant_consent("alice", "training", at=t(1))
        svc.collect("alice", "training", "sample", at=t(2),
                    session_id="S-a", record_id="Rt")
        svc.withdraw_consent("alice", "training", at=t(3))
        show("撤回后 t4 使用", svc.evaluate_access(
            Operation.USE, "alice", "training", at=t(4), record_id="Rt"))
        show("以历史时点 t2 重放", svc.evaluate_access(
            Operation.USE, "alice", "training", at=t(2), record_id="Rt"))

        # 3) 事故调查保留与到期自动删除
        svc.grant_consent("alice", "trip-summary", at=t(1))
        svc.collect("alice", "trip-summary", "summary", at=t(2),
                    session_id="S-a", record_id="Rs")
        svc.add_legal_hold("H1", "alice", "事故调查", start_at=t(3), end_at=t(30))
        job = svc.request_delete("alice", "REQ-1", at=t(5))
        run1 = svc.process_job(job.job_id, at=t(5))
        print(f"[删除] 保留期内: {run1.status.value}, "
              f"明细={[(i.record_id, i.state.value) for i in run1.items]}")
        done = svc.run_due(at=t(30))
        print(f"[删除] 到期恢复: {done[0].status.value}, "
              f"记录状态={svc.get_record('Rs').state.value}")

        print(json.dumps({"final_jobs": [j.status.value for j in done]},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
