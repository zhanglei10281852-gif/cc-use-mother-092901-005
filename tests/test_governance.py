"""数据授权治理端到端测试。

覆盖：多人共车隔离、同意换版与时点判断、撤回只阻断后续、
合法保留冲突与到期恢复、派生物血缘删除、幂等重复请求、
失败重试，以及崩溃后基于 WAL 的重启恢复。
"""

import json
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance import (
    ConsentDecision,
    GovernanceService,
    ItemStatus,
    JobKind,
    JobStatus,
    JsonStore,
    Operation,
    ReasonCode,
    RecordState,
    SimulatedCrash,
)
from consent_governance.api import create_app

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def ts(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class GovernanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = GovernanceService(self.tmp.name)
        self._bootstrap()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bootstrap(self) -> None:
        s = self.svc
        s.register_vehicle("VIN-1", at=ts(0))
        s.register_account("alice", at=ts(0))
        s.register_account("bob", at=ts(0))
        for code, desc in [
            ("voice-personalization", "语音偏好个性化"),
            ("trip-summary", "行程摘要"),
            ("training", "模型训练样本"),
        ]:
            s.register_purpose(code, desc)
        # 同一辆车的两段会话，两个驾驶人
        s.start_session("S-alice", "VIN-1", "alice", at=ts(1))
        s.start_session("S-bob", "VIN-1", "bob", at=ts(10))

    # ------------------------------------------------------------------
    # 1. 多人共车：车辆、账号、会话隔离
    # ------------------------------------------------------------------

    def test_shared_vehicle_sessions_are_isolated(self):
        s = self.svc
        s.grant_consent("alice", "voice-personalization", at=ts(2))
        s.grant_consent("bob", "voice-personalization", at=ts(11))

        d_a = s.collect("alice", "voice-personalization", "voice-profile",
                        at=ts(3), session_id="S-alice", record_id="Ra")
        d_b = s.collect("bob", "voice-personalization", "voice-profile",
                        at=ts(12), session_id="S-bob", record_id="Rb")
        self.assertTrue(d_a.allowed)
        self.assertTrue(d_b.allowed)
        self.assertNotEqual(d_a.record_id, d_b.record_id)

        # bob 不能用自己的会话访问 alice 的记录
        denied = s.evaluate_access(Operation.USE, "bob", "voice-personalization",
                                   at=ts(13), record_id="Ra", session_id="S-bob")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason_code, ReasonCode.RECORD_OWNER_MISMATCH)

        # alice 也不能拿 bob 的会话访问自己的记录（会话/账号不一致）
        denied2 = s.evaluate_access(Operation.USE, "alice", "voice-personalization",
                                    at=ts(13), session_id="S-bob")
        self.assertEqual(denied2.reason_code, ReasonCode.SESSION_MISMATCH)

        # 同车两场会话确实并存
        self.assertEqual(
            sorted(x.session_id for x in s.sessions_of_vehicle("VIN-1")),
            ["S-alice", "S-bob"],
        )

    def test_record_collected_before_session_start_is_rejected(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(0))
        d = s.collect("alice", "trip-summary", "trip",
                      at=ts(0.5), session_id="S-alice", record_id="R-early")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason_code, ReasonCode.SESSION_MISMATCH)
        self.assertNotIn("R-early", s.data["records"])

    # ------------------------------------------------------------------
    # 2. 同意换版：判断以事件发生时点的版本为准
    # ------------------------------------------------------------------

    def test_consent_versioning_uses_event_time(self):
        s = self.svc
        s.grant_consent("alice", "training", at=ts(1))          # v1 granted
        r = s.collect("alice", "training", "sample", at=ts(2),
                      session_id="S-alice", record_id="Rtr")
        self.assertTrue(r.allowed)

        # t3 撤回
        s.withdraw_consent("alice", "training", at=ts(3))      # v2 withdrawn
        # t4 的使用被拒，且解释指明撤回版本
        d4 = s.evaluate_access(Operation.USE, "alice", "training",
                               at=ts(4), record_id="Rtr")
        self.assertFalse(d4.allowed)
        self.assertEqual(d4.reason_code, ReasonCode.CONSENT_WITHDRAWN)
        self.assertEqual(d4.consent.version, 2)
        self.assertIn("撤回只阻止后续用途", d4.explanation)

        # 以历史时点 t2 重放判断，仍应依据 v1 获准
        d2 = s.evaluate_access(Operation.USE, "alice", "training",
                               at=ts(2), record_id="Rtr")
        self.assertTrue(d2.allowed)
        self.assertEqual(d2.consent.version, 1)

        # 重新授权 v3，后续用途恢复
        s.grant_consent("alice", "training", at=ts(5))         # v3 granted
        d6 = s.evaluate_access(Operation.USE, "alice", "training",
                               at=ts(6), record_id="Rtr")
        self.assertTrue(d6.allowed)
        self.assertEqual(d6.consent.version, 3)
        self.assertEqual([c.version for c in s.consent_versions("alice", "training")],
                         [1, 2, 3])

    def test_purposes_are_independent(self):
        s = self.svc
        s.grant_consent("alice", "voice-personalization", at=ts(1))
        # 训练用途从未授权 → 拒绝采集
        d = s.collect("alice", "training", "sample", at=ts(2),
                      session_id="S-alice", record_id="Rx")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason_code, ReasonCode.NO_CONSENT)

    def test_explicit_denial_blocks_and_is_explained(self):
        s = self.svc
        s.deny_consent("bob", "training", at=ts(1))
        d = s.evaluate_access(Operation.COLLECT, "bob", "training", at=ts(2))
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason_code, ReasonCode.CONSENT_DENIED)
        self.assertIn("v1", d.explanation)

    # ------------------------------------------------------------------
    # 3. 撤回只阻止后续用途，历史访问必须留痕
    # ------------------------------------------------------------------

    def test_withdrawal_keeps_history_but_blocks_future(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rsum")
        s.evaluate_access(Operation.SHARE, "alice", "trip-summary",
                          at=ts(2.5), record_id="Rsum", recipient="insurer-x")
        s.withdraw_consent("alice", "trip-summary", at=ts(3))

        # 历史记录仍在、历史留痕仍可查
        self.assertEqual(s.get_record("Rsum").state, RecordState.ACTIVE)
        history = s.access_history(account_id="alice", record_id="Rsum")
        self.assertTrue(all(e.allowed for e in history))
        self.assertEqual(len(history), 2)  # 采集 + 共享
        share_log = next(e for e in history if e.operation == Operation.SHARE)
        self.assertEqual(share_log.recipient, "insurer-x")

        # 撤回后的新共享被拒，且这次拒绝本身也留痕
        d = s.evaluate_access(Operation.SHARE, "alice", "trip-summary",
                              at=ts(4), record_id="Rsum", recipient="insurer-x")
        self.assertFalse(d.allowed)
        denied_logs = [e for e in s.access_history(account_id="alice") if not e.allowed]
        self.assertEqual(len(denied_logs), 1)
        self.assertEqual(denied_logs[0].reason_code, ReasonCode.CONSENT_WITHDRAWN)

    # ------------------------------------------------------------------
    # 4. 合法保留冲突与到期自动恢复
    # ------------------------------------------------------------------

    def test_legal_hold_blocks_then_expiry_resumes_deletion(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rsum")
        # 事故调查保留：t3 ~ t30
        s.add_legal_hold("H-accident", "alice", "事故调查", end_at=ts(30),
                         start_at=ts(3))

        # 保留期内使用被拒
        blocked = s.evaluate_access(Operation.USE, "alice", "trip-summary",
                                    at=ts(4), record_id="Rsum")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason_code, ReasonCode.LEGAL_HOLD)

        job = s.request_delete("alice", "REQ-D1", at=ts(5))
        run = s.process_job(job.job_id, at=ts(5))
        self.assertEqual(run.status, JobStatus.PARTIAL)
        self.assertEqual(run.items[0].state, ItemStatus.HELD)
        self.assertEqual(run.items[0].detail["hold_id"], "H-accident")
        # 记录仍在
        self.assertEqual(s.get_record("Rsum").state, RecordState.ACTIVE)

        # 保留未到期，调度器不会空转它
        self.assertEqual(s.run_due(at=ts(29)), [])
        self.assertEqual(s.get_job(job.job_id).status, JobStatus.PARTIAL)

        # 到期：自动恢复处理，删除完成
        done = s.run_due(at=ts(30))
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].status, JobStatus.SUCCEEDED)
        self.assertEqual(s.get_record("Rsum").state, RecordState.DELETED)

        # 删除后访问被拒（墓碑）
        d = s.evaluate_access(Operation.USE, "alice", "trip-summary",
                              at=ts(31), record_id="Rsum")
        self.assertEqual(d.reason_code, ReasonCode.RECORD_DELETED)

    def test_export_under_hold_resumes_after_expiry(self):
        s = self.svc
        s.grant_consent("bob", "trip-summary", at=ts(1))
        s.collect("bob", "trip-summary", "summary", at=ts(12),
                  session_id="S-bob", record_id="Rh")
        s.add_legal_hold("H2", "bob", "事故调查", start_at=ts(3), end_at=ts(20))
        job = s.request_export("bob", "REQ-EH", at=ts(13))
        run1 = s.process_job(job.job_id, at=ts(13))
        self.assertEqual(run1.status, JobStatus.PARTIAL)
        self.assertEqual(run1.items[0].state, ItemStatus.EXCLUDED)
        self.assertEqual(run1.items[0].detail["reason"], ReasonCode.LEGAL_HOLD.value)

        # 未到期不唤醒；到期自动重试并导出成功
        self.assertEqual(s.run_due(at=ts(19)), [])
        done = s.run_due(at=ts(20))
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].status, JobStatus.SUCCEEDED)
        self.assertEqual(done[0].result["count"], 1)

    def test_hold_scoped_to_purpose_leaves_other_purpose_usable(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.grant_consent("alice", "voice-personalization", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rt")
        s.collect("alice", "voice-personalization", "voice", at=ts(2),
                  session_id="S-alice", record_id="Rv")
        s.add_legal_hold("H1", "alice", "事故调查", end_at=ts(30),
                         purpose="trip-summary", start_at=ts(3))

        blocked = s.evaluate_access(Operation.USE, "alice", "trip-summary",
                                    at=ts(4), record_id="Rt")
        allowed = s.evaluate_access(Operation.USE, "alice", "voice-personalization",
                                    at=ts(4), record_id="Rv")
        self.assertEqual(blocked.reason_code, ReasonCode.LEGAL_HOLD)
        self.assertTrue(allowed.allowed)

    # ------------------------------------------------------------------
    # 5. 派生物血缘追踪
    # ------------------------------------------------------------------

    def test_delete_tracks_origin_and_locatable_derivatives(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.grant_consent("alice", "training", at=ts(1))
        # 原始语音 → 行程摘要 → 训练样本；另有不可定位的匿名聚合
        s.collect("alice", "trip-summary", "raw", at=ts(2),
                  session_id="S-alice", record_id="R1")
        s.collect("alice", "trip-summary", "summary", at=ts(3),
                  derived_from="R1", record_id="R2")
        s.collect("alice", "training", "sample", at=ts(4),
                  derived_from="R2", record_id="R3")
        s.collect("alice", "training", "aggregate", at=ts(5),
                  derived_from="R3", record_id="R4", locatable=False)

        lineage = s.lineage("R3")
        self.assertEqual(lineage["root_record_ids"], ["R1"])
        self.assertEqual(lineage["chain"], ["R1", "R2", "R3"])
        self.assertEqual(lineage["locatable_descendants"], ["R2", "R3"])
        self.assertIn("R4", lineage["non_locatable"])

        job = s.request_delete("alice", "REQ-DL", at=ts(6))
        run = s.process_job(job.job_id, at=ts(6))
        # R1/R2/R3 被删除，R4 报告为不可定位
        self.assertEqual(run.status, JobStatus.PARTIAL)
        states = {it.record_id: it.state for it in run.items}
        self.assertEqual(states["R1"], ItemStatus.SUCCEEDED)
        self.assertEqual(states["R2"], ItemStatus.SUCCEEDED)
        self.assertEqual(states["R3"], ItemStatus.SUCCEEDED)
        self.assertEqual(states["R4"], ItemStatus.NON_LOCATABLE)
        self.assertEqual(s.get_record("R1").state, RecordState.DELETED)
        self.assertEqual(s.get_record("R3").state, RecordState.DELETED)
        self.assertEqual(s.get_record("R4").state, RecordState.ACTIVE)
        self.assertIn("R4", run.result["non_locatable"])

    def test_cannot_create_derivative_across_accounts(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.grant_consent("bob", "training", at=ts(1))
        s.collect("alice", "trip-summary", "raw", at=ts(2),
                  session_id="S-alice", record_id="Ra")
        with self.assertRaises(Exception):
            s.collect("bob", "training", "sample", at=ts(3),
                      derived_from="Ra", record_id="Rb")

    # ------------------------------------------------------------------
    # 6. 导出作业：授权逐项判断、可重试
    # ------------------------------------------------------------------

    def test_export_excludes_unauthorized_then_succeeds_on_retry(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rok")
        # 训练样本采集时已授权，但在导出前被撤回
        s.grant_consent("alice", "training", at=ts(0))
        s.withdraw_consent("alice", "training", at=ts(1.5))
        s.collect("alice", "training", "sample", at=ts(1.4),
                  session_id="S-alice", record_id="Rno")

        job = s.request_export("alice", "REQ-E1", at=ts(5))
        run = s.process_job(job.job_id, at=ts(5))
        self.assertEqual(run.status, JobStatus.PARTIAL)
        by_id = {it.record_id: it for it in run.items}
        self.assertEqual(by_id["Rok"].state, ItemStatus.SUCCEEDED)
        self.assertEqual(by_id["Rno"].state, ItemStatus.EXCLUDED)
        self.assertEqual(by_id["Rno"].detail["reason"],
                         ReasonCode.CONSENT_WITHDRAWN.value)
        self.assertEqual(run.result["count"], 1)

        # 重新授权后重试同一作业，两项均导出
        s.grant_consent("alice", "training", at=ts(6))
        retry = s.process_job(job.job_id, at=ts(7))
        self.assertEqual(retry.status, JobStatus.SUCCEEDED)
        self.assertEqual(retry.attempts, 2)
        self.assertEqual(retry.result["count"], 2)

    # ------------------------------------------------------------------
    # 7. 删除瞬时失败可重试
    # ------------------------------------------------------------------

    def test_failed_delete_item_is_retried(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rd")
        job = s.request_delete("alice", "REQ-D2", at=ts(3))
        s.fail_next_delete_for("Rd")
        run1 = s.process_job(job.job_id, at=ts(3))
        self.assertEqual(run1.status, JobStatus.FAILED)
        self.assertEqual(run1.items[0].state, ItemStatus.FAILED)
        self.assertIn("注入故障", run1.items[0].detail["error"])

        run2 = s.process_job(job.job_id, at=ts(4))
        self.assertEqual(run2.status, JobStatus.SUCCEEDED)
        self.assertEqual(s.get_record("Rd").state, RecordState.DELETED)

    # ------------------------------------------------------------------
    # 8. 重复请求幂等
    # ------------------------------------------------------------------

    def test_duplicate_request_returns_same_job(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rp")
        j1 = s.request_export("alice", "REQ-DUP", at=ts(3))
        s.process_job(j1.job_id, at=ts(3))
        dup = s.duplicate_request("REQ-DUP")
        self.assertIsNotNone(dup)
        self.assertTrue(dup.duplicated)
        self.assertEqual(dup.job_id, j1.job_id)
        # 再发一次仍然返回同一作业，不新建
        j2 = s.request_export("alice", "REQ-DUP", at=ts(4))
        self.assertEqual(j2.job_id, j1.job_id)
        self.assertEqual(len(s.data["jobs"]), 1)

        # 同一 request_id 改作删除 → 冲突
        with self.assertRaises(Exception):
            s.request_delete("alice", "REQ-DUP", at=ts(5))

    # ------------------------------------------------------------------
    # 9. 重启恢复：WAL 重放 + RUNNING 作业复位
    # ------------------------------------------------------------------

    def test_crash_mid_job_recovers_on_restart(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rc")
        s.checkpoint()
        job = s.request_delete("alice", "REQ-CRASH", at=ts(3))
        s.crash_on_next_job_run = True
        with self.assertRaises(SimulatedCrash):
            s.process_job(job.job_id, at=ts(3))
        self.assertEqual(s.get_job(job.job_id).status, JobStatus.RUNNING)

        # 新进程：从同一目录加载
        svc2 = GovernanceService(self.tmp.name)
        recovered = svc2.get_job(job.job_id)
        self.assertEqual(recovered.status, JobStatus.PENDING)
        self.assertEqual(recovered.attempts, 1)
        # 版本链、计数器都完好
        self.assertEqual(
            [c.version for c in svc2.consent_versions("alice", "trip-summary")], [1]
        )
        done = svc2.run_due(at=ts(4))
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].status, JobStatus.SUCCEEDED)
        self.assertEqual(svc2.get_record("Rc").state, RecordState.DELETED)

    def test_wal_replay_without_checkpoint(self):
        s = self.svc
        s.grant_consent("bob", "trip-summary", at=ts(1))
        # 故意不 checkpoint，完全靠 WAL 重放
        svc2 = GovernanceService(self.tmp.name)
        versions = svc2.consent_versions("bob", "trip-summary")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].decision, ConsentDecision.GRANTED)
        # 计数器不回退，新 ID 不冲突
        c2 = svc2.grant_consent("bob", "trip-summary", at=ts(2))
        self.assertEqual(c2.version, 2)
        self.assertNotIn(
            c2.consent_id,
            {v.consent_id for v in svc2.consent_versions("bob", "trip-summary")} - {c2.consent_id},
        )

    # ------------------------------------------------------------------
    # 10. 快照 + 日志截断仍保持状态
    # ------------------------------------------------------------------

    def test_checkpoint_snapshot_then_continue(self):
        s = self.svc
        s.grant_consent("alice", "trip-summary", at=ts(1))
        s.collect("alice", "trip-summary", "summary", at=ts(2),
                  session_id="S-alice", record_id="Rs")
        s.checkpoint()
        wal = Path(self.tmp.name) / JsonStore.WAL
        self.assertEqual(wal.read_text(encoding="utf-8").strip(), "")
        svc2 = GovernanceService(self.tmp.name)
        self.assertEqual(svc2.get_record("Rs").state, RecordState.ACTIVE)
        self.assertEqual(len(svc2.access_history()), 1)  # 采集判断留痕一次


class ApiSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_app(self.tmp.name)
        self.host, self.port = self.server.server_address[0], self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _req(self, method: str, path: str, payload: dict | None = None):
        url = f"http://{self.host}:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_flow_over_http(self):
        st, _ = self._req("POST", "/vehicles", {"vehicle_id": "V1"})
        self.assertEqual(st, 201)
        st, _ = self._req("POST", "/accounts", {"account_id": "u1"})
        self.assertEqual(st, 201)
        st, _ = self._req("POST", "/purposes", {"code": "p1", "description": "测试用途"})
        self.assertEqual(st, 201)
        st, _ = self._req("POST", "/sessions",
                          {"session_id": "S1", "vehicle_id": "V1", "account_id": "u1",
                           "at": ts(0).isoformat()})
        self.assertEqual(st, 201)
        st, body = self._req("POST", "/accounts/u1/consents/p1",
                             {"decision": "granted", "at": ts(1).isoformat()})
        self.assertEqual(st, 201)
        st, body = self._req("POST", "/collect", {
            "account_id": "u1", "purpose": "p1", "kind": "voice",
            "session_id": "S1", "record_id": "R1", "at": ts(2).isoformat(),
        })
        self.assertEqual(st, 201)
        self.assertTrue(body["allowed"])
        self.assertIn("v1", body["explanation"])

        # 撤回后使用 → 403 且带解释
        self._req("POST", "/accounts/u1/consents/p1",
                  {"decision": "withdrawn", "at": ts(3).isoformat()})
        st, body = self._req("POST", "/access/evaluate", {
            "operation": "use", "account_id": "u1", "purpose": "p1",
            "record_id": "R1", "at": ts(4).isoformat(),
        })
        self.assertEqual(st, 403)
        self.assertEqual(body["reason_code"], ReasonCode.CONSENT_WITHDRAWN.value)

        # 幂等删除：第二次返回 200 + duplicated
        payload = {"account_id": "u1", "request_id": "REQ-1", "at": ts(5).isoformat()}
        st1, j1 = self._req("POST", "/deletions", payload)
        st2, j2 = self._req("POST", "/deletions", payload)
        self.assertEqual(st1, 201)
        self.assertEqual(st2, 200)
        self.assertEqual(j1["job_id"], j2["job_id"])
        self.assertTrue(j2["duplicated"])

        st, j3 = self._req("POST", f"/jobs/{j1['job_id']}/run", {"at": ts(5).isoformat()})
        self.assertEqual(st, 200)
        self.assertEqual(j3["status"], JobStatus.SUCCEEDED.value)

        st, logs = self._req("GET", "/access-logs?account_id=u1")
        self.assertEqual(st, 200)
        self.assertTrue(any(e["allowed"] is False for e in logs["entries"]))


if __name__ == "__main__":
    unittest.main()
