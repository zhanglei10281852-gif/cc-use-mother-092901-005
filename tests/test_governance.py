"""治理后端端到端测试：

覆盖多人共车边界、同意换版的时点判定、合法保留冲突、
派生物谱系删除、重复请求幂等、作业重试与重启恢复。
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance import (
    ConsentDecision,
    GovernanceBackend,
    JobStatus,
    Operation,
    Purpose,
    RecordStatus,
    Repository,
)
from consent_governance.engine import AccessDeniedError


def t(offset_hours: float = 0) -> datetime:
    return datetime(2026, 10, 3, tzinfo=timezone.utc) + timedelta(hours=offset_hours)


VOICE = Purpose(
    "voice-personalization", "语音偏好",
    frozenset({Operation.COLLECT, Operation.USE, Operation.SHARE, Operation.EXPORT}),
)
TRIP = Purpose(
    "trip-summary", "行程摘要",
    frozenset({Operation.COLLECT, Operation.USE, Operation.EXPORT}),
)
TRAIN = Purpose(
    "model-training", "训练样本",
    frozenset({Operation.USE}),
)
INVESTIGATION = Purpose(
    "accident-investigation", "事故调查",
    frozenset({Operation.USE, Operation.EXPORT}), legal_investigation=True,
)


class BackendTestBase(unittest.TestCase):
    def setUp(self):
        self.backend = GovernanceBackend(":memory:")
        for purpose in (VOICE, TRIP, TRAIN, INVESTIGATION):
            self.backend.register_purpose(purpose)
        self.backend.register_vehicle("VIN-1")
        self.backend.register_account("alice")
        self.backend.register_account("bob")

    def tearDown(self):
        self.backend.close()


class MultiDriverTests(BackendTestBase):
    """多人共车：同一辆车轮换驾驶人时，授权按账号+会话隔离。"""

    def test_each_driver_only_governs_own_data(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.grant("bob", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.start_session("S-B", "VIN-1", "bob", t(1))

        rec_a = b.collect("R-A", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        rec_b = b.collect("R-B", "bob", "VIN-1", "S-B", VOICE.code, "voice", t(1))

        # 本人可用；另一驾驶人不能依据自己的同意使用前一人的偏好
        own = b.evaluate_access("alice", Operation.USE, VOICE.code, at=t(2), record_id="R-A")
        cross = b.evaluate_access("bob", Operation.USE, VOICE.code, at=t(2), record_id="R-A")
        self.assertTrue(own.allowed)
        self.assertFalse(cross.allowed)
        self.assertIn("不是记录归属人", " ".join(cross.reasons))

        # 共享被拒，且两条判定都留痕
        share = b.evaluate_access("bob", Operation.SHARE, VOICE.code, at=t(2), record_id="R-A")
        self.assertFalse(share.allowed)
        audit = b.list_audit(record_id="R-A")
        self.assertEqual([a.allowed for a in audit][:3], [True, False, False])

        # 语音偏好与行程摘要授权互不影响
        b.grant("alice", TRIP.code, t(0))
        b.collect("R-T", "alice", "VIN-1", "S-A", TRIP.code, "trip", t(0))
        mixed_use = b.evaluate_access(
            "alice", Operation.USE, TRAIN.code, at=t(2), record_id="R-T"
        )
        self.assertFalse(mixed_use.allowed)

    def test_collect_requires_active_session_at_event_time(self):
        b = self.backend
        b.grant("bob", VOICE.code, t(0))
        # bob 在第 5 小时才开始驾驶，第 2 小时的采集必须被拒
        b.start_session("S-B", "VIN-1", "bob", t(5))
        decision = b.evaluate_access(
            "bob", Operation.COLLECT, VOICE.code, at=t(2),
            vehicle_id="VIN-1", session_id="S-B",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("不是车辆 VIN-1 的活动乘员", " ".join(decision.reasons))

        # 会话结束后采集同样被拒
        b.end_session("S-B", t(7))
        decision2 = b.evaluate_access(
            "bob", Operation.COLLECT, VOICE.code, at=t(8),
            vehicle_id="VIN-1", session_id="S-B",
        )
        self.assertFalse(decision2.allowed)

    def test_session_owner_must_match_record_owner(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.start_session("S-B", "VIN-1", "bob", t(0))
        with self.assertRaises(AccessDeniedError):
            b.collect("R-X", "alice", "VIN-1", "S-B", VOICE.code, "voice", t(0))


class ConsentVersioningTests(BackendTestBase):
    """同意换版：判定永远锚定事件发生时点的版本，撤回不溯及既往。"""

    def test_point_in_time_decision_across_versions(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))  # v1 granted
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))

        b.deny("alice", VOICE.code, t(10))   # v2 denied
        self.assertFalse(
            b.evaluate_access("alice", Operation.USE, VOICE.code, at=t(11), record_id="R-1").allowed
        )
        # 事件发生在 v2 生效前，仍按 v1 允许
        self.assertTrue(
            b.evaluate_access("alice", Operation.USE, VOICE.code, at=t(9), record_id="R-1").allowed
        )

        b.withdraw("alice", VOICE.code, t(20))  # v3 withdrawn
        d3 = b.evaluate_access("alice", Operation.USE, VOICE.code, at=t(21), record_id="R-1")
        self.assertFalse(d3.allowed)
        self.assertEqual(d3.consent_version, 3)
        self.assertEqual(d3.consent_decision, ConsentDecision.WITHDRAWN)
        self.assertIn("撤回仅阻止该时点之后", " ".join(d3.reasons))

        # 重新授予产生 v4，之后的访问恢复
        b.grant("alice", VOICE.code, t(30))  # v4
        self.assertTrue(
            b.evaluate_access("alice", Operation.USE, VOICE.code, at=t(31), record_id="R-1").allowed
        )

    def test_withdraw_keeps_history_explainable(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        before = b.evaluate_access("alice", Operation.SHARE, VOICE.code, at=t(1), record_id="R-1")
        self.assertTrue(before.allowed)

        b.withdraw("alice", VOICE.code, t(2))
        after = b.evaluate_access("alice", Operation.SHARE, VOICE.code, at=t(3), record_id="R-1")
        self.assertFalse(after.allowed)

        # 历史获准判定仍可通过审计编号完整解释
        explained = b.explain(before.audit_id)
        self.assertTrue(explained.allowed)
        self.assertEqual(explained.consent_version, 1)
        self.assertEqual(explained.operation, Operation.SHARE)
        self.assertTrue(explained.reasons)
        self.assertIsNone(b.explain(999_999))

    def test_operation_outside_purpose_scope_denied(self):
        b = self.backend
        b.grant("alice", TRAIN.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        # TRAIN 用途不含 COLLECT，即使同意存在也不能采集
        with self.assertRaises(AccessDeniedError):
            b.collect("R-X", "alice", "VIN-1", "S-A", TRAIN.code, "sample", t(0))


class LineageAndDeletionTests(BackendTestBase):
    """删除追踪原始记录与可定位派生物。"""

    def test_deletion_follows_derived_lineage(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.grant("alice", TRAIN.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        raw = b.collect("R-raw", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        summary = b.create_derived(
            "R-sum", "alice", VOICE.code, "trip-summary", ("R-raw",), t(1)
        )
        # 训练样本是从语音记录和行程摘要派生的二级派生物
        b.grant("alice", TRAIN.code, t(0))
        sample = b.create_derived(
            "R-sample", "alice", TRAIN.code, "training-sample", ("R-raw", "R-sum"), t(2)
        )

        job0 = b.request_delete("alice", "del-1", t(3))
        job = b.run_delete(job0.job_id, t(3))
        self.assertEqual(job.status, JobStatus.COMPLETED)
        deleted = {i.record_id: i.state.value for i in job.items}
        self.assertEqual(set(deleted), {"R-raw", "R-sum", "R-sample"})
        self.assertTrue(all(v == "deleted" for v in deleted.values()))
        for rid in ("R-raw", "R-sum", "R-sample"):
            self.assertEqual(b.repo.get_record(rid).status, RecordStatus.DELETED)

        # 删除后任何访问都被拒并留痕
        d = b.evaluate_access("alice", Operation.USE, VOICE.code, at=t(4), record_id="R-raw")
        self.assertFalse(d.allowed)
        self.assertIn("已删除", " ".join(d.reasons))

    def test_derived_cannot_mix_other_drivers_sources(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.grant("bob", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.start_session("S-B", "VIN-1", "bob", t(1))
        b.collect("R-A", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        b.collect("R-B", "bob", "VIN-1", "S-B", VOICE.code, "voice", t(1))
        with self.assertRaises(ValueError):
            b.create_derived("R-X", "alice", TRAIN.code, "sample", ("R-A", "R-B"), t(2))


class LegalHoldTests(BackendTestBase):
    """保留冲突：调查期内删除挂起，仅调查用途可访问，到期自动续处理。"""

    def _setup_with_hold(self, hold_until_offset=30 * 24):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        b.grant("alice", TRAIN.code, t(0))
        b.create_derived("R-2", "alice", TRAIN.code, "training-sample", ("R-1",), t(1))
        b.place_hold(
            "R-1", t(hold_until_offset), "事故调查 #A21", hold_id="H-1", at=t(2)
        )

    def test_hold_blocks_normal_use_and_deletion_but_allows_investigation(self):
        b = self.backend
        self._setup_with_hold()

        normal = b.evaluate_access(
            "alice", Operation.USE, VOICE.code, at=t(3), record_id="R-1"
        )
        self.assertFalse(normal.allowed)
        self.assertIn("合法保留", " ".join(normal.reasons))

        invest = b.evaluate_access(
            "alice", Operation.USE, INVESTIGATION.code, at=t(3), record_id="R-1"
        )
        self.assertTrue(invest.allowed)
        self.assertIn("法定调查用途访问", " ".join(invest.reasons))

        # 保留传播到派生物
        self.assertTrue(b.repo.active_holds_for("R-2", t(3)))

        job0 = b.request_delete("alice", "del-held", t(3))
        job = b.run_delete(job0.job_id, t(3))
        self.assertEqual(job.status, JobStatus.AWAITING_HOLD)
        states = {i.record_id: i.state.value for i in job.items}
        self.assertEqual(states, {"R-1": "held", "R-2": "held"})
        # 被挂起的记录仍然存活
        self.assertEqual(b.repo.get_record("R-1").status, RecordStatus.ACTIVE)

    def test_hold_expiry_resumes_and_completes_delete(self):
        b = self.backend
        self._setup_with_hold(hold_until_offset=30 * 24)
        job0 = b.request_delete("alice", "del-held", t(3))
        b.run_delete(job0.job_id, t(3))

        result = b.tick(t(30 * 24 + 1))
        self.assertIn("H-1", result["released_holds"])
        self.assertIn(job0.job_id, result["resumed_jobs"])

        job = b.get_delete_job(job0.job_id)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertIsNotNone(job.completed_at)
        self.assertTrue(all(i.state.value == "deleted" for i in job.items))
        for rid in ("R-1", "R-2"):
            self.assertEqual(b.repo.get_record(rid).status, RecordStatus.DELETED)

        # 到期后普通调查访问也不再有保留依据
        d = b.evaluate_access(
            "alice", Operation.USE, INVESTIGATION.code,
            at=t(30 * 24 + 1), record_id="R-1",
        )
        self.assertFalse(d.allowed)

    def test_hold_created_after_delete_still_blocks_retry_until_expiry(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        job0 = b.request_delete("alice", "del-1", t(1))
        b.run_delete(job0.job_id, t(1))
        self.assertEqual(b.get_delete_job(job0.job_id).status, JobStatus.COMPLETED)
        self.assertEqual(b.repo.get_record("R-1").status, RecordStatus.DELETED)


class JobTests(BackendTestBase):
    """导出/删除作业：幂等请求、瞬时故障可重试、状态可查询。"""

    def _collect_one(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))

    def test_export_retry_after_transient_failure(self):
        b = self.backend
        self._collect_one()
        job0 = b.request_export("alice", "exp-1", purpose=VOICE.code, at=t(1))
        failed = b.run_export(job0.job_id, t(1), fail=True)
        self.assertEqual(failed.status, JobStatus.FAILED)
        self.assertEqual(failed.attempts, 1)
        self.assertIn("瞬时故障", failed.last_error)

        ok = b.run_export(failed.job_id, t(2))
        self.assertEqual(ok.status, JobStatus.COMPLETED)
        self.assertEqual(ok.attempts, 2)
        ids = [r["record_id"] for r in ok.bundle["records"]]
        self.assertEqual(ids, ["R-1"])
        # 重试已完成作业是空操作，attempts 不再增长
        again = b.run_export(ok.job_id, t(3))
        self.assertEqual(again.attempts, 2)

    def test_export_explains_excluded_records(self):
        b = self.backend
        self._collect_one()
        # bob 的数据不会出现在 alice 的导出包里
        b.grant("bob", VOICE.code, t(0))
        b.start_session("S-B", "VIN-1", "bob", t(0))
        b.collect("R-B", "bob", "VIN-1", "S-B", VOICE.code, "voice", t(0))
        job0 = b.request_export("alice", "exp-2", purpose=VOICE.code, at=t(1))
        job = b.run_export(job0.job_id, t(1))
        self.assertEqual([r["record_id"] for r in job.bundle["records"]], ["R-1"])

        # 撤回后再次导出：记录被排除且给出审计可查的理由
        b.withdraw("alice", VOICE.code, t(2))
        job1 = b.request_export("alice", "exp-3", purpose=VOICE.code, at=t(3))
        run = b.run_export(job1.job_id, t(3))
        self.assertEqual(run.bundle["records"], [])
        excluded = run.bundle["excluded"][0]
        self.assertEqual(excluded["record_id"], "R-1")
        self.assertTrue(excluded["reasons"])

    def test_duplicate_requests_are_idempotent(self):
        b = self.backend
        self._collect_one()
        j1 = b.request_delete("alice", "dup")
        j2 = b.request_delete("alice", "dup")
        self.assertEqual(j1.job_id, j2.job_id)
        b.run_delete(j1.job_id, t(1))
        j3 = b.request_delete("alice", "dup")
        self.assertEqual(j3.status, JobStatus.COMPLETED)

        e1 = b.request_export("alice", "dup-exp", purpose=VOICE.code)
        e2 = b.request_export("alice", "dup-exp", purpose=VOICE.code)
        self.assertEqual(e1.job_id, e2.job_id)

    def test_failed_delete_resumes_from_progress(self):
        b = self.backend
        b.grant("alice", VOICE.code, t(0))
        b.start_session("S-A", "VIN-1", "alice", t(0))
        b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        b.collect("R-2", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
        job0 = b.request_delete("alice", "del-retry", t(1))
        failed = b.run_delete(job0.job_id, t(1), fail=True)
        self.assertEqual(failed.status, JobStatus.FAILED)
        # 第一条已删除，第二条仍存活
        self.assertEqual(b.repo.get_record("R-1").status, RecordStatus.DELETED)
        self.assertEqual(b.repo.get_record("R-2").status, RecordStatus.ACTIVE)

        done = b.run_delete(failed.job_id, t(2))
        self.assertEqual(done.status, JobStatus.COMPLETED)
        self.assertEqual(b.repo.get_record("R-2").status, RecordStatus.DELETED)
        # R-1 未被重复删除（状态带进度说明）
        r1_item = next(i for i in done.items if i.record_id == "R-1")
        self.assertEqual(r1_item.reason, "此前尝试中已删除")


class RestartRecoveryTests(unittest.TestCase):
    """重启恢复：全部状态持久化，中断作业回到可重试状态，到期自动续处理。"""

    def test_interrupted_jobs_recover_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "gov.db"

            def fresh_backend():
                return GovernanceBackend(str(db))

            b = fresh_backend()
            b.register_purpose(VOICE)
            b.register_vehicle("VIN-1")
            b.register_account("alice")
            b.grant("alice", VOICE.code, t(0))
            b.start_session("S-A", "VIN-1", "alice", t(0))
            b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
            export0 = b.request_export("alice", "exp-persist", purpose=VOICE.code, at=t(1))
            delete0 = b.request_delete("alice", "del-persist", t(1))

            # 手工把作业置为 RUNNING 模拟进程在执行中崩溃
            from consent_governance import ExportJob
            from dataclasses import replace
            running = replace(b.repo.get_export_job(export0.job_id), status=JobStatus.RUNNING)
            b.repo.update_export_job(running)
            drunning = replace(b.repo.get_delete_job(delete0.job_id), status=JobStatus.RUNNING)
            b.repo.update_delete_job(drunning, b.repo.delete_items_for(delete0.job_id))
            b.repo.commit()
            b.close()

            # 重新打开：构造时自动恢复
            b2 = fresh_backend()
            self.assertEqual(b2.get_export_job(export0.job_id).status, JobStatus.FAILED)
            self.assertEqual(b2.get_delete_job(delete0.job_id).status, JobStatus.FAILED)

            # 重试两个作业并确认数据状态一致
            export_done = b2.run_export(export0.job_id, t(2))
            delete_done = b2.run_delete(delete0.job_id, t(2))
            self.assertEqual(export_done.status, JobStatus.COMPLETED)
            self.assertEqual(delete_done.status, JobStatus.COMPLETED)
            self.assertEqual(b2.repo.get_record("R-1").status, RecordStatus.DELETED)
            # 审计历史跨重启保留
            self.assertTrue(b2.list_audit(record_id="R-1"))
            b2.close()

    def test_awaiting_hold_survives_restart_and_resumes_on_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "gov2.db"

            def fresh_backend():
                return GovernanceBackend(str(db))

            b = fresh_backend()
            b.register_purpose(VOICE)
            b.register_purpose(INVESTIGATION)
            b.register_vehicle("VIN-1")
            b.register_account("alice")
            b.grant("alice", VOICE.code, t(0))
            b.start_session("S-A", "VIN-1", "alice", t(0))
            b.collect("R-1", "alice", "VIN-1", "S-A", VOICE.code, "voice", t(0))
            job0 = b.request_delete("alice", "del-hold", t(1))
            b.place_hold("R-1", t(48), "事故调查 #A21", hold_id="H-9", at=t(1))
            held = b.run_delete(job0.job_id, t(1))
            self.assertEqual(held.status, JobStatus.AWAITING_HOLD)
            b.close()

            b2 = fresh_backend()
            # 保留期内重启，作业仍是挂起状态
            self.assertEqual(b2.get_delete_job(job0.job_id).status, JobStatus.AWAITING_HOLD)
            self.assertEqual(b2.repo.get_record("R-1").status, RecordStatus.ACTIVE)
            # 时钟越过保留期限，tick 自动续处理并完成
            b2.tick(t(49))
            self.assertEqual(b2.get_delete_job(job0.job_id).status, JobStatus.COMPLETED)
            self.assertEqual(b2.repo.get_record("R-1").status, RecordStatus.DELETED)
            b2.close()


class RepositorySchemaTests(unittest.TestCase):
    def test_repository_can_persist_independently(self):
        repo = Repository(":memory:")
        repo.close()


if __name__ == "__main__":
    unittest.main()
