"""数据授权治理核心服务。

所有判断都以 *事件发生时点*（``at``）为准：

* 同意是只增不改的版本流，取 ``recorded_at <= at`` 的最新版本；
* 撤回只产生一个 ``withdrawn`` 新版本，只阻断之后的处理，
  之前已经做出并留痕的访问不受影响；
* 合法保留按 ``[start_at, end_at)`` 区间生效，到期自动放行被暂缓的删除。

变更先写 WAL（见 :mod:`consent_governance.storage`），作业状态可跨重启恢复。
"""

import threading
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable

from .storage import JsonStore

from .contracts import (
    AccessDecision,
    AccessLogEntry,
    Account,
    ConsentDecision,
    ConsentVersion,
    DataPurpose,
    DataRecord,
    ItemStatus,
    Job,
    JobItem,
    JobKind,
    JobStatus,
    OccupantSession,
    Operation,
    ReasonCode,
    RecordState,
    SimulatedCrash,
    Vehicle,
    utc_now,
)
from .storage import JsonStore


def _enum(value, enum_cls):
    return enum_cls(value) if not isinstance(value, enum_cls) else value


class GovernanceError(ValueError):
    """请求参数冲突等客户端错误。"""


class GovernanceService:
    def __init__(self, store: JsonStore | str):
        self.store = store if isinstance(store, JsonStore) else JsonStore(store)
        self._lock = threading.RLock()
        self.data = self.store.load_raw()
        self.data.setdefault("vehicles", {})
        self.data.setdefault("accounts", {})
        self.data.setdefault("purposes", {})
        self.data.setdefault("sessions", {})
        self.data.setdefault("consents", {})       # consent_id -> dict
        self.data.setdefault("consent_index", {})  # account|purpose -> [consent_id...]
        self.data.setdefault("records", {})
        self.data.setdefault("holds", {})
        self.data.setdefault("jobs", {})
        self.data.setdefault("idempotency", {})
        self.data.setdefault("access_log", [])
        self.data.setdefault("counters", {})
        # 测试钩子：下一次处理该记录时抛一次异常，用于验证 FAILED 重试
        self._fail_once_for: str | None = None
        # 测试钩子：作业一旦进入 RUNNING 即模拟进程崩溃
        self.crash_on_next_job_run = False
        self.recover_stale_jobs()

    # ==================================================================
    # 持久化原语
    # ==================================================================

    def _commit(self, op: str, table: str, key: str | None, value) -> None:
        raw = self._to_raw(value)
        self.store.append_wal(op, table, key, raw)
        if op == "put":
            self.data.setdefault(table, {})[key] = raw
        elif op == "delete":
            self.data.get(table, {}).pop(key, None)
        elif op == "append":
            self.data.setdefault(table, []).append(raw)
        else:
            raise GovernanceError(f"未知 WAL 操作: {op}")

    @staticmethod
    def _to_raw(value):
        """内存与快照统一为 JSON 原生形状（datetime 保留为对象）。

        WAL 序列化时再由 storage 层把 datetime 转为带标记的字符串，
        重放后还原，因此同进程内访问器始终可按 dict + datetime 读取。
        """
        if is_dataclass(value) and not isinstance(value, type):
            return GovernanceService._to_raw(asdict(value))
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {k: GovernanceService._to_raw(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [GovernanceService._to_raw(v) for v in value]
        return value

    def checkpoint(self) -> None:
        """把当前状态落为快照并截断 WAL。"""
        with self._lock:
            self.store.save(self.data)

    def _next_seq(self, name: str) -> int:
        n = int(self.data["counters"].get(name, 0)) + 1
        self._commit("put", "counters", name, n)
        return n

    # ==================================================================
    # 实体注册：车辆 / 账号 / 用途 / 乘员会话
    # ==================================================================

    def register_vehicle(self, vehicle_id: str, at: datetime | None = None) -> Vehicle:
        with self._lock:
            if vehicle_id in self.data["vehicles"]:
                raise GovernanceError(f"车辆已存在: {vehicle_id}")
            vehicle = Vehicle(vehicle_id, at or utc_now())
            self._commit("put", "vehicles", vehicle_id, vehicle)
            return vehicle

    def register_account(self, account_id: str, at: datetime | None = None) -> Account:
        with self._lock:
            if account_id in self.data["accounts"]:
                raise GovernanceError(f"账号已存在: {account_id}")
            account = Account(account_id, at or utc_now())
            self._commit("put", "accounts", account_id, account)
            return account

    def register_purpose(self, code: str, description: str = "") -> DataPurpose:
        with self._lock:
            if not code.strip():
                raise GovernanceError("数据用途编码不能为空")
            if code in self.data["purposes"]:
                raise GovernanceError(f"数据用途已存在: {code}")
            purpose = DataPurpose(code, description)
            self._commit("put", "purposes", code, purpose)
            return purpose

    def start_session(
        self,
        session_id: str,
        vehicle_id: str,
        account_id: str,
        at: datetime | None = None,
    ) -> OccupantSession:
        """开启一次乘员会话；车辆与账号在此绑定，共车场景据此隔离。"""
        with self._lock:
            if vehicle_id not in self.data["vehicles"]:
                raise GovernanceError(f"未知车辆: {vehicle_id}")
            if account_id not in self.data["accounts"]:
                raise GovernanceError(f"未知账号: {account_id}")
            if session_id in self.data["sessions"]:
                raise GovernanceError(f"会话已存在: {session_id}")
            session = OccupantSession(session_id, vehicle_id, account_id, at or utc_now())
            self._commit("put", "sessions", session_id, session)
            return session

    def sessions_of_vehicle(self, vehicle_id: str) -> list[OccupantSession]:
        return [
            self._session(sid)
            for sid, s in self.data["sessions"].items()
            if s["vehicle_id"] == vehicle_id
        ]

    # ==================================================================
    # 同意版本（只增不改）
    # ==================================================================

    def _record_consent(
        self,
        account_id: str,
        purpose: str,
        decision: ConsentDecision,
        at: datetime,
    ) -> ConsentVersion:
        if account_id not in self.data["accounts"]:
            raise GovernanceError(f"未知账号: {account_id}")
        if purpose not in self.data["purposes"]:
            raise GovernanceError(f"未知数据用途: {purpose}")
        index_key = f"{account_id}|{purpose}"
        prior = self.data["consent_index"].get(index_key, [])
        version = len(prior) + 1
        consent_id = f"C:{account_id}:{purpose}:v{version}"
        consent = ConsentVersion(consent_id, account_id, version, purpose, decision, at)
        self._commit("put", "consents", consent_id, consent)
        ids = prior + [consent_id]
        self._commit("put", "consent_index", index_key, ids)
        return consent

    def grant_consent(self, account_id: str, purpose: str, at: datetime | None = None) -> ConsentVersion:
        with self._lock:
            return self._record_consent(account_id, purpose, ConsentDecision.GRANTED, at or utc_now())

    def deny_consent(self, account_id: str, purpose: str, at: datetime | None = None) -> ConsentVersion:
        with self._lock:
            return self._record_consent(account_id, purpose, ConsentDecision.DENIED, at or utc_now())

    def withdraw_consent(self, account_id: str, purpose: str, at: datetime | None = None) -> ConsentVersion:
        """撤回 = 追加一个 withdrawn 版本；历史版本与历史访问记录保持不变。"""
        with self._lock:
            index_key = f"{account_id}|{purpose}"
            if not self.data["consent_index"].get(index_key):
                raise GovernanceError(f"不存在可撤回的授权: {account_id}/{purpose}")
            return self._record_consent(account_id, purpose, ConsentDecision.WITHDRAWN, at or utc_now())

    def consent_versions(self, account_id: str, purpose: str) -> list[ConsentVersion]:
        return [self._consent(cid) for cid in self.data["consent_index"].get(f"{account_id}|{purpose}", [])]

    def effective_consent(
        self, account_id: str, purpose: str, at: datetime
    ) -> ConsentVersion | None:
        """事件时点生效的同意版本（recorded_at <= at 中的最新一版）。"""
        candidates = [
            c
            for c in self.consent_versions(account_id, purpose)
            if c.recorded_at <= at
        ]
        return candidates[-1] if candidates else None

    # ==================================================================
    # 合法保留（有期限）
    # ==================================================================

    def add_legal_hold(
        self,
        hold_id: str,
        account_id: str,
        reason: str,
        end_at: datetime,
        purpose: str | None = None,
        start_at: datetime | None = None,
    ) -> dict:
        """登记一次合法保留，覆盖该账号（可限定用途）的记录。

        生效区间为 ``[start_at, end_at)``；``end_at`` 到期后自动恢复处理。
        """
        with self._lock:
            if hold_id in self.data["holds"]:
                raise GovernanceError(f"保留令已存在: {hold_id}")
            if account_id not in self.data["accounts"]:
                raise GovernanceError(f"未知账号: {account_id}")
            if not reason.strip():
                raise GovernanceError("保留原因不能为空")
            start = start_at or utc_now()
            if end_at <= start:
                raise GovernanceError("保留结束时间必须晚于开始时间")
            hold = {
                "hold_id": hold_id,
                "account_id": account_id,
                "purpose": purpose,
                "reason": reason,
                "start_at": start,
                "end_at": end_at,
            }
            self._commit("put", "holds", hold_id, hold)
            return dict(hold)

    def active_holds(self, account_id: str, purpose: str, at: datetime) -> list[dict]:
        out = []
        for hold in self.data["holds"].values():
            if hold["account_id"] != account_id:
                continue
            if hold["purpose"] is not None and hold["purpose"] != purpose:
                continue
            if hold["start_at"] <= at < hold["end_at"]:
                out.append(hold)
        return out

    def _release_expired_holds(self, at: datetime) -> list[str]:
        """到期恢复：把因保留而暂缓的删除/导出作业重新置为 PENDING。

        保留是否生效完全由时间区间计算，记录本身不需要状态翻转；
        这里只需唤醒等待中的作业。只挑"确有保留项已到期"的作业，
        避免对授权撤回等其他原因导致的 PARTIAL 空转。
        """
        requeued: list[str] = []
        for job in self.data["jobs"].values():
            if job["status"] != JobStatus.PARTIAL.value:
                continue
            waiting = [
                it for it in job["items"]
                if it["detail"].get("hold_end_at") is not None
                and it["detail"]["hold_end_at"] <= at
                and (
                    it["state"] == ItemStatus.HELD.value
                    or (it["state"] == ItemStatus.EXCLUDED.value
                        and it["detail"].get("reason") == ReasonCode.LEGAL_HOLD.value)
                )
            ]
            if waiting:
                job["status"] = JobStatus.PENDING.value
                job["updated_at"] = at
                self._commit("put", "jobs", job["job_id"], job)
                requeued.append(job["job_id"])
        return requeued

    # ==================================================================
    # 访问判断与留痕
    # ==================================================================

    def evaluate_access(
        self,
        operation: Operation | str,
        account_id: str,
        purpose: str,
        at: datetime | None = None,
        record_id: str | None = None,
        session_id: str | None = None,
        recipient: str | None = None,
        log_record_id: str | None = None,
    ) -> AccessDecision:
        """依据事件发生时的授权判断一次访问，无论获准或拒绝一律留痕。

        ``log_record_id`` 供采集场景使用：记录在获准后才创建，
        但日志仍应记录即将产生的记录 ID。
        """
        with self._lock:
            at = at or utc_now()
            operation = _enum(operation, Operation)
            decision = self._evaluate(operation, account_id, purpose, at,
                                      record_id, session_id, recipient)
            # 采集获准后记录才会真正创建，此时才把其 ID 补进留痕
            if log_record_id is not None and decision.allowed:
                decision.record_id = log_record_id
            log = self._write_log(decision)
            decision.log_id = log.log_id
            return decision

    def _deny(self, operation, account_id, purpose, at, code, explanation,
              consent=None, session_id=None, record_id=None, recipient=None) -> AccessDecision:
        return AccessDecision(
            allowed=False, operation=operation, account_id=account_id, purpose=purpose,
            at=at, reason_code=code, explanation=explanation, consent=consent,
            session_id=session_id, record_id=record_id, recipient=recipient,
        )

    def _evaluate(
        self, operation: Operation, account_id: str, purpose: str, at: datetime,
        record_id: str | None, session_id: str | None, recipient: str | None,
    ) -> AccessDecision:
        def deny(code, explanation, **kw) -> AccessDecision:
            return self._deny(operation, account_id, purpose, at, code, explanation,
                              session_id=session_id, record_id=record_id, recipient=recipient, **kw)

        if purpose not in self.data["purposes"]:
            return deny(ReasonCode.UNKNOWN_PURPOSE, f"数据用途未注册: {purpose}")
        if account_id not in self.data["accounts"]:
            return deny(ReasonCode.NO_CONSENT, f"账号不存在或从未授权: {account_id}")
        if operation == Operation.SHARE and not (recipient or "").strip():
            return deny(ReasonCode.NO_CONSENT, "共享必须指定接收方")

        record = None
        if record_id is not None:
            raw = self.data["records"].get(record_id)
            if raw is None:
                return deny(ReasonCode.RECORD_NOT_FOUND, f"数据记录不存在: {record_id}")
            record = self._record(record_id)
            if record.owner_account != account_id:
                return deny(
                    ReasonCode.RECORD_OWNER_MISMATCH,
                    f"记录 {record_id} 属于 {record.owner_account}，与请求账号 {account_id} 不符",
                )
            if record.state == RecordState.DELETED:
                return deny(ReasonCode.RECORD_DELETED, f"记录已删除: {record_id}")
            holds = self.active_holds(account_id, record.purpose, at)
            if holds:
                hold = holds[0]
                return deny(
                    ReasonCode.LEGAL_HOLD,
                    f"记录处于合法保留（{hold['hold_id']}：{hold['reason']}），"
                    f"{hold['end_at'].isoformat()} 到期前暂停处理",
                )

        if session_id is not None:
            raw_session = self.data["sessions"].get(session_id)
            if raw_session is None:
                return deny(ReasonCode.SESSION_MISMATCH, f"乘员会话不存在: {session_id}")
            session = self._session(session_id)
            if session.account_id != account_id:
                return deny(
                    ReasonCode.SESSION_MISMATCH,
                    f"会话 {session_id} 属于 {session.account_id}，与请求账号 {account_id} 不符",
                )
            if record is not None and record.session_id is not None and record.session_id != session_id:
                return deny(
                    ReasonCode.SESSION_MISMATCH,
                    f"记录 {record_id} 采集自会话 {record.session_id}，不能用会话 {session_id} 访问",
                )
            if at < session.started_at:
                return deny(
                    ReasonCode.SESSION_MISMATCH,
                    f"访问时间 {at.isoformat()} 早于会话开始时间 {session.started_at.isoformat()}",
                )

        consent = self.effective_consent(account_id, purpose, at)
        if consent is None:
            return deny(
                ReasonCode.NO_CONSENT,
                f"{at.isoformat()} 时 {account_id} 对用途 {purpose} 没有任何授权版本",
            )
        if consent.decision == ConsentDecision.DENIED:
            return deny(
                ReasonCode.CONSENT_DENIED,
                f"用途 {purpose} 在 v{consent.version} 中被明确拒绝",
                consent=consent,
            )
        if consent.decision == ConsentDecision.WITHDRAWN:
            return deny(
                ReasonCode.CONSENT_WITHDRAWN,
                f"用途 {purpose} 的授权已在 v{consent.version}（{consent.recorded_at.isoformat()}）撤回，"
                "撤回只阻止后续用途",
                consent=consent,
            )

        action = {
            Operation.COLLECT: "采集",
            Operation.USE: "使用",
            Operation.SHARE: f"共享给 {recipient}",
            Operation.EXPORT: "导出",
        }[operation]
        explanation = (
            f"获准{action}：{at.isoformat()} 时生效的是 v{consent.version}"
            f"（{consent.recorded_at.isoformat()} 记录为 granted）"
        )
        if record_id:
            explanation += f"，记录 {record_id}"
        return AccessDecision(
            allowed=True, operation=operation, account_id=account_id, purpose=purpose,
            at=at, reason_code=ReasonCode.OK, explanation=explanation, consent=consent,
            session_id=session_id, record_id=record_id, recipient=recipient,
        )

    def _write_log(self, d: AccessDecision) -> AccessLogEntry:
        log_id = self._next_seq_log()
        entry = AccessLogEntry(
            log_id=log_id, at=d.at, operation=d.operation, account_id=d.account_id,
            purpose=d.purpose, allowed=d.allowed, reason_code=d.reason_code,
            explanation=d.explanation,
            consent_id=d.consent.consent_id if d.consent else None,
            session_id=d.session_id, record_id=d.record_id, recipient=d.recipient,
        )
        self._commit("append", "access_log", None, entry)
        return entry

    def _next_seq_log(self) -> int:
        n = int(self.data["counters"].get("log_seq", 0)) + 1
        self._commit("put", "counters", "log_seq", n)
        return n

    def access_history(
        self,
        account_id: str | None = None,
        record_id: str | None = None,
        operation: Operation | str | None = None,
    ) -> list[AccessLogEntry]:
        operation = _enum(operation, Operation) if operation is not None else None
        out = []
        for raw in self.data["access_log"]:
            entry = self._log(raw)
            if account_id is not None and entry.account_id != account_id:
                continue
            if record_id is not None and entry.record_id != record_id:
                continue
            if operation is not None and entry.operation != operation:
                continue
            out.append(entry)
        return out

    # ==================================================================
    # 采集：获准后才落记录，并维护派生血缘
    # ==================================================================

    def collect(
        self,
        account_id: str,
        purpose: str,
        kind: str,
        at: datetime | None = None,
        session_id: str | None = None,
        record_id: str | None = None,
        derived_from: str | None = None,
        locatable: bool = True,
    ) -> AccessDecision:
        with self._lock:
            at = at or utc_now()
            record_id = record_id or f"R{self._next_seq('record_seq')}"
            if record_id in self.data["records"]:
                raise GovernanceError(f"记录 ID 已存在: {record_id}")
            parent = None
            if derived_from is not None:
                parent = self.data["records"].get(derived_from)
                if parent is None:
                    raise GovernanceError(f"派生来源记录不存在: {derived_from}")
                if parent["owner_account"] != account_id:
                    raise GovernanceError(
                        f"派生物不能跨账号生成: {derived_from} 属于 {parent['owner_account']}"
                    )
            decision = self.evaluate_access(
                Operation.COLLECT, account_id, purpose, at=at,
                session_id=session_id, log_record_id=record_id,
            )
            if not decision.allowed:
                return decision
            record = DataRecord(
                record_id=record_id, owner_account=account_id, purpose=purpose,
                kind=kind, created_at=at, session_id=session_id,
                derived_from=derived_from, locatable=locatable,
            )
            self._commit("put", "records", record_id, record)
            decision.record = record
            decision.record_id = record_id
            return decision

    def get_record(self, record_id: str) -> DataRecord:
        if record_id not in self.data["records"]:
            raise GovernanceError(f"数据记录不存在: {record_id}")
        return self._record(record_id)

    def lineage(self, record_id: str) -> dict:
        """返回原始记录与全部可定位派生物。"""
        record = self.get_record(record_id)
        roots, chain = self._root_and_chain(record_id)
        descendants: list[str] = []
        stack = list(roots)
        seen = set()
        while stack:
            rid = stack.pop()
            if rid in seen:
                continue
            seen.add(rid)
            for cand, raw in self.data["records"].items():
                if raw.get("derived_from") == rid and raw.get("locatable", True):
                    descendants.append(cand)
                    stack.append(cand)
        return {
            "root_record_ids": sorted(roots),
            "chain": chain,
            "locatable_descendants": sorted(set(descendants)),
            "non_locatable": sorted(
                rid for rid, raw in self.data["records"].items()
                if not raw.get("locatable", True)
            ),
        }

    def _root_and_chain(self, record_id: str) -> tuple[list[str], list[str]]:
        chain: list[str] = []
        current = record_id
        while current is not None:
            rec = self.data["records"].get(current)
            if rec is None:
                break
            chain.append(current)
            current = rec.get("derived_from")
        chain.reverse()
        root = chain[0] if chain else record_id
        return [root], chain

    # ==================================================================
    # 导出 / 删除作业（幂等、可重试、可恢复）
    # ==================================================================

    def request_export(
        self,
        account_id: str,
        request_id: str,
        at: datetime | None = None,
        purpose: str | None = None,
        record_ids: Iterable[str] | None = None,
    ) -> Job:
        with self._lock:
            return self._request_job(
                JobKind.EXPORT, account_id, request_id, at or utc_now(), purpose, record_ids
            )

    def request_delete(
        self,
        account_id: str,
        request_id: str,
        at: datetime | None = None,
        purpose: str | None = None,
        record_ids: Iterable[str] | None = None,
    ) -> Job:
        with self._lock:
            return self._request_job(
                JobKind.DELETE, account_id, request_id, at or utc_now(), purpose, record_ids
            )

    def _request_job(
        self, kind: JobKind, account_id: str, request_id: str, at: datetime,
        purpose: str | None, record_ids: Iterable[str] | None,
    ) -> Job:
        if account_id not in self.data["accounts"]:
            raise GovernanceError(f"未知账号: {account_id}")
        if purpose is not None and purpose not in self.data["purposes"]:
            raise GovernanceError(f"未知数据用途: {purpose}")
        existing = self.data["idempotency"].get(request_id)
        if existing is not None:
            if existing["kind"] != kind.value or existing["account_id"] != account_id:
                raise GovernanceError(f"请求号 {request_id} 已用于不同类型的作业")
            return self._job(existing["job_id"])

        scope = self._snapshot_scope(account_id, purpose, record_ids)
        job_id = f"J{self._next_seq('job_seq')}"
        job = Job(
            job_id=job_id, kind=kind, account_id=account_id, status=JobStatus.PENDING,
            request_id=request_id, request_at=at, attempts=0, duplicated=False,
            items=tuple(JobItem(rid, ItemStatus.PENDING, {}) for rid in scope),
            created_at=at, updated_at=at,
            scope_purpose=purpose, scope_record_ids=tuple(scope),
        )
        self._commit("put", "jobs", job_id, self._job_to_dict(job))
        self._commit("put", "idempotency", request_id, {
            "job_id": job_id, "kind": kind.value, "account_id": account_id, "at": at,
        })
        return job

    def _snapshot_scope(
        self, account_id: str, purpose: str | None, record_ids: Iterable[str] | None
    ) -> list[str]:
        if record_ids is not None:
            scope = []
            for rid in record_ids:
                raw = self.data["records"].get(rid)
                if raw is None:
                    raise GovernanceError(f"数据记录不存在: {rid}")
                if raw["owner_account"] != account_id:
                    raise GovernanceError(f"记录 {rid} 不属于账号 {account_id}")
                if purpose is not None and raw["purpose"] != purpose:
                    raise GovernanceError(f"记录 {rid} 的用途与请求范围不符")
                scope.append(rid)
            return scope
        return sorted(
            rid for rid, raw in self.data["records"].items()
            if raw["owner_account"] == account_id
            and (purpose is None or raw["purpose"] == purpose)
        )

    def get_job(self, job_id: str) -> Job:
        if job_id not in self.data["jobs"]:
            raise GovernanceError(f"作业不存在: {job_id}")
        return self._job(job_id)

    def get_job_by_request(self, request_id: str) -> Job:
        ref = self.data["idempotency"].get(request_id)
        if ref is None:
            raise GovernanceError(f"未知请求号: {request_id}")
        return self._job(ref["job_id"])

    def process_job(self, job_id: str, at: datetime | None = None) -> Job:
        """执行（或重试）一个作业。FAILED/PARTIAL/PENDING 均可再次调用。"""
        with self._lock:
            at = at or utc_now()
            job = self._job(job_id)
            if job.status in (JobStatus.SUCCEEDED,):
                return job
            job = replace(job, status=JobStatus.RUNNING, attempts=job.attempts + 1, updated_at=at)
            self._persist_job(job)

            if self.crash_on_next_job_run:
                # 状态已持久化为 RUNNING：模拟进程此刻被杀掉
                self.crash_on_next_job_run = False
                raise SimulatedCrash(f"作业 {job_id} 执行中崩溃")

            items: list[JobItem] = []
            result: dict | None = job.result
            last_error: str | None = None
            try:
                if job.kind == JobKind.EXPORT:
                    items, result = self._process_export(job, at)
                else:
                    items, result = self._process_delete(job, at)
            except SimulatedCrash:
                raise
            except Exception as exc:  # 单项之外的意外：作业保持可重试
                last_error = repr(exc)
                items = list(job.items)

            has_failed = any(it.state == ItemStatus.FAILED for it in items)
            has_held = any(it.state == ItemStatus.HELD for it in items)
            has_excluded = any(it.state == ItemStatus.EXCLUDED for it in items)
            has_nonloc = any(it.state == ItemStatus.NON_LOCATABLE for it in items)
            if has_failed:
                status = JobStatus.FAILED
            elif has_held:
                status = JobStatus.PARTIAL
            elif has_excluded or has_nonloc:
                status = JobStatus.PARTIAL
            elif all(it.state == ItemStatus.SUCCEEDED for it in items):
                status = JobStatus.SUCCEEDED
            else:
                status = JobStatus.PARTIAL
            job = replace(job, items=tuple(items), status=status, result=result,
                          last_error=last_error, updated_at=at)
            self._persist_job(job)
            return job

    def _process_export(self, job: Job, at: datetime) -> tuple[list[JobItem], dict]:
        exported: list[dict] = []
        items: list[JobItem] = []
        for item in job.items:
            rid = item.record_id
            raw = self.data["records"].get(rid)
            if raw is None or raw["state"] == RecordState.DELETED.value:
                items.append(JobItem(rid, ItemStatus.EXCLUDED, {"reason": ReasonCode.RECORD_DELETED.value}))
                continue
            record = self._record(rid)
            if not record.locatable:
                items.append(JobItem(rid, ItemStatus.NON_LOCATABLE,
                                     {"reason": "派生物不可定位，无法纳入导出"}))
                continue
            holds = self.active_holds(job.account_id, record.purpose, at)
            if holds:
                items.append(JobItem(rid, ItemStatus.EXCLUDED, {
                    "reason": ReasonCode.LEGAL_HOLD.value,
                    "hold_id": holds[0]["hold_id"],
                    "hold_end_at": holds[0]["end_at"],
                }))
                continue
            decision = self.evaluate_access(
                Operation.EXPORT, job.account_id, record.purpose, at=at, record_id=rid,
            )
            if decision.allowed:
                exported.append({
                    "record_id": rid,
                    "kind": record.kind,
                    "purpose": record.purpose,
                    "session_id": record.session_id,
                    "derived_from": record.derived_from,
                    "created_at": record.created_at,
                })
                items.append(JobItem(rid, ItemStatus.SUCCEEDED,
                                     {"consent_id": decision.consent.consent_id},
                                     decided_consent_id=decision.consent.consent_id))
            else:
                items.append(JobItem(rid, ItemStatus.EXCLUDED, {
                    "reason": decision.reason_code.value,
                    "explanation": decision.explanation,
                }, decided_consent_id=decision.consent.consent_id if decision.consent else None))
        result = {"exported": exported, "count": len(exported), "generated_at": at}
        return items, result

    def _process_delete(self, job: Job, at: datetime) -> tuple[list[JobItem], dict]:
        items: list[JobItem] = []
        deleted: list[str] = []
        held: list[dict] = []
        non_locatable: list[str] = []

        # 以原始记录为起点，把可定位派生链一并纳入删除追踪
        target_ids = self._expand_delete_targets(job.scope_record_ids)

        for rid in target_ids:
            raw = self.data["records"].get(rid)
            if raw is None:
                items.append(JobItem(rid, ItemStatus.SUCCEEDED, {"note": "记录已不存在"}))
                continue
            record = self._record(rid)
            if record.state == RecordState.DELETED:
                items.append(JobItem(rid, ItemStatus.SUCCEEDED, {"note": "此前已删除"}))
                continue
            if not record.locatable:
                non_locatable.append(rid)
                items.append(JobItem(rid, ItemStatus.NON_LOCATABLE,
                                     {"reason": "匿名聚合派生物不可定位，无法定向删除"}))
                continue
            holds = self.active_holds(job.account_id, record.purpose, at)
            if holds:
                hold = holds[0]
                held.append({"record_id": rid, "hold_id": hold["hold_id"],
                             "hold_end_at": hold["end_at"]})
                items.append(JobItem(rid, ItemStatus.HELD, {
                    "reason": ReasonCode.LEGAL_HOLD.value,
                    "hold_id": hold["hold_id"],
                    "hold_reason": hold["reason"],
                    "hold_end_at": hold["end_at"],
                }))
                continue
            try:
                if self._fail_once_for == rid:
                    self._fail_once_for = None
                    raise RuntimeError(f"注入故障：删除 {rid} 时存储暂时不可用")
                self._hard_delete(record, at)
            except Exception as exc:
                items.append(JobItem(rid, ItemStatus.FAILED, {"error": repr(exc)}))
                continue
            deleted.append(rid)
            items.append(JobItem(rid, ItemStatus.SUCCEEDED, {"deleted_at": at}))

        result = {
            "deleted": deleted,
            "held": held,
            "non_locatable": non_locatable,
            "processed_at": at,
        }
        return items, result

    def _expand_delete_targets(self, scope_record_ids: tuple[str, ...]) -> list[str]:
        """从请求范围内的记录出发，追踪原始记录与全部可定位派生物。"""
        targets: set[str] = set()
        for rid in scope_record_ids:
            raw = self.data["records"].get(rid)
            if raw is None:
                targets.add(rid)
                continue
            # 向上找到原始记录
            root = rid
            while True:
                parent = self.data["records"][root].get("derived_from")
                if parent is None or parent not in self.data["records"]:
                    break
                root = parent
            # 向下收集同血缘的全部派生物；不可定位者也纳入追踪，
            # 执行时标记为 non_locatable 而不是静默遗漏
            stack = [root]
            while stack:
                cur = stack.pop()
                if cur in targets:
                    continue
                cur_raw = self.data["records"].get(cur)
                if cur_raw is None:
                    continue
                targets.add(cur)
                for cand, c in self.data["records"].items():
                    if c.get("derived_from") == cur:
                        stack.append(cand)
        return sorted(targets)

    def _hard_delete(self, record: DataRecord, at: datetime) -> None:
        """逻辑删除：保留可审计墓碑，内容不再可访问。"""
        tombstone = replace(record, state=RecordState.DELETED, deleted_at=at)
        self._commit("put", "records", record.record_id, tombstone)

    def fail_next_delete_for(self, record_id: str) -> None:
        """测试钩子：下一轮删除该记录时制造一次瞬时故障。"""
        self._fail_once_for = record_id

    def run_due(self, at: datetime | None = None) -> list[Job]:
        """到期处理：先恢复到期保留，再执行所有待处理/失败的作业。"""
        with self._lock:
            at = at or utc_now()
            self._release_expired_holds(at)
            # PARTIAL（保留暂缓）只由到期恢复唤醒，避免保留期内空转；
            # FAILED 属于可重试的瞬时故障，每次调度都重试。
            due = [
                jid for jid, job in self.data["jobs"].items()
                if job["status"] in (JobStatus.PENDING.value, JobStatus.FAILED.value)
            ]
            results = []
            for jid in sorted(due, key=lambda x: self.data["jobs"][x]["created_at"] or at):
                results.append(self.process_job(jid, at=at))
            return results

    def recover_stale_jobs(self) -> list[str]:
        """重启恢复：崩溃时停留在 RUNNING 的作业回到 PENDING，等待重试。"""
        recovered = []
        for jid, job in self.data["jobs"].items():
            if job["status"] == JobStatus.RUNNING.value:
                job["status"] = JobStatus.PENDING.value
                self.store.append_wal("put", "jobs", jid, job)
                recovered.append(jid)
        return recovered

    def mark_duplicate_reply(self, job: Job) -> Job:
        """重复请求取回的既有作业在视图上标注 duplicated（状态本身不改变）。"""
        return replace(job, duplicated=True)

    def duplicate_request(self, request_id: str) -> Job | None:
        """按幂等键取回作业；调用方据此识别重复请求。"""
        ref = self.data["idempotency"].get(request_id)
        if ref is None:
            return None
        return replace(self._job(ref["job_id"]), duplicated=True)

    # ==================================================================
    # 反序列化
    # ==================================================================

    def _consent(self, consent_id: str) -> ConsentVersion:
        d = self.data["consents"][consent_id]
        return ConsentVersion(
            consent_id=d["consent_id"], account_id=d["account_id"], version=d["version"],
            purpose=d["purpose"], decision=_enum(d["decision"], ConsentDecision),
            recorded_at=d["recorded_at"],
        )

    def _session(self, session_id: str) -> OccupantSession:
        d = self.data["sessions"][session_id]
        return OccupantSession(d["session_id"], d["vehicle_id"], d["account_id"], d["started_at"])

    def _record(self, record_id: str) -> DataRecord:
        d = self.data["records"][record_id]
        return DataRecord(
            record_id=d["record_id"], owner_account=d["owner_account"], purpose=d["purpose"],
            kind=d["kind"], created_at=d["created_at"], session_id=d.get("session_id"),
            derived_from=d.get("derived_from"), locatable=d.get("locatable", True),
            state=_enum(d.get("state", RecordState.ACTIVE), RecordState),
            deleted_at=d.get("deleted_at"),
        )

    def _log(self, d: dict) -> AccessLogEntry:
        return AccessLogEntry(
            log_id=d["log_id"], at=d["at"], operation=_enum(d["operation"], Operation),
            account_id=d["account_id"], purpose=d["purpose"], allowed=d["allowed"],
            reason_code=_enum(d["reason_code"], ReasonCode), explanation=d["explanation"],
            consent_id=d.get("consent_id"), session_id=d.get("session_id"),
            record_id=d.get("record_id"), recipient=d.get("recipient"),
        )

    def _job(self, job_id: str) -> Job:
        d = self.data["jobs"][job_id]
        return Job(
            job_id=d["job_id"], kind=_enum(d["kind"], JobKind), account_id=d["account_id"],
            status=_enum(d["status"], JobStatus), request_id=d["request_id"],
            request_at=d["request_at"], attempts=d["attempts"], duplicated=d.get("duplicated", False),
            items=tuple(
                JobItem(
                    record_id=it["record_id"], state=_enum(it["state"], ItemStatus),
                    detail=it.get("detail", {}), decided_consent_id=it.get("decided_consent_id"),
                )
                for it in d.get("items", [])
            ),
            result=d.get("result"), last_error=d.get("last_error"),
            created_at=d.get("created_at"), updated_at=d.get("updated_at"),
            scope_purpose=d.get("scope_purpose"),
            scope_record_ids=tuple(d.get("scope_record_ids") or ()),
        )

    @staticmethod
    def _job_to_dict(job: Job) -> dict:
        return {
            "job_id": job.job_id,
            "kind": job.kind.value,
            "account_id": job.account_id,
            "status": job.status.value,
            "request_id": job.request_id,
            "request_at": job.request_at,
            "attempts": job.attempts,
            "duplicated": job.duplicated,
            "items": [
                {
                    "record_id": it.record_id,
                    "state": it.state.value,
                    "detail": it.detail,
                    "decided_consent_id": it.decided_consent_id,
                }
                for it in job.items
            ],
            "result": job.result,
            "last_error": job.last_error,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "scope_purpose": job.scope_purpose,
            "scope_record_ids": list(job.scope_record_ids or ()),
        }

    def _persist_job(self, job: Job) -> None:
        payload = self._job_to_dict(job)
        self._commit("put", "jobs", job.job_id, payload)
