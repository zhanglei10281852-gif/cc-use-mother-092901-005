"""数据授权治理核心引擎。

判定原则：
- 所有访问都按 *事件发生时点* 有效的同意版本判断（point-in-time 判定）。
- 撤回只产生新的同意版本，阻止后续用途；历史判定仍按当时版本，且全程留痕。
- 合法保留（如事故调查）有期限：期内仅调查用途可访问、删除挂起；到期自动释放并续处理。
- 删除追踪原始记录与沿派生谱系可定位的全部派生物。
- 导出/删除作业幂等（request_id 去重）、可重试（FAILED/AWAITING_HOLD 可再跑），重启可恢复。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from .contracts import (
    AccessDecision,
    Account,
    ConsentDecision,
    ConsentVersion,
    DataRecord,
    DeleteItem,
    DeleteItemState,
    DeleteJob,
    ExportJob,
    JobStatus,
    LegalHold,
    OccupantSession,
    Operation,
    Purpose,
    RecordStatus,
    Vehicle,
)
from .storage import Repository


class AccessDeniedError(PermissionError):
    """访问被拒绝；decision 携带完整解释（且已写入审计）。"""

    def __init__(self, decision: AccessDecision):
        self.decision = decision
        super().__init__("; ".join(decision.reasons))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GovernanceBackend:
    def __init__(self, repo: Repository | str = ":memory:", now_fn=_utcnow):
        self.now_fn = now_fn
        if isinstance(repo, Repository):
            self.repo = repo
            self._owns_repo = False
        else:
            self.repo = Repository(repo)
            self._owns_repo = True
        self.recover()

    def close(self) -> None:
        self.repo.commit()
        if self._owns_repo:
            self.repo.close()

    def now(self) -> datetime:
        return self.now_fn()

    # ---- 基础实体注册 -------------------------------------------------

    def register_vehicle(self, vehicle_id: str, at: datetime | None = None) -> Vehicle:
        vehicle = Vehicle(vehicle_id, at or self.now())
        self.repo.add_vehicle(vehicle)
        return vehicle

    def register_account(self, account_id: str, at: datetime | None = None) -> Account:
        account = Account(account_id, at or self.now())
        self.repo.add_account(account)
        return account

    def register_purpose(self, purpose: Purpose) -> Purpose:
        self.repo.add_purpose(purpose)
        return purpose

    def start_session(
        self, session_id: str, vehicle_id: str, account_id: str, at: datetime | None = None
    ) -> OccupantSession:
        session = OccupantSession(session_id, vehicle_id, account_id, at or self.now())
        self.repo.add_session(session)
        return session

    def end_session(self, session_id: str, at: datetime | None = None) -> None:
        self.repo.end_session(session_id, at or self.now())

    # ---- 同意版本（授予 / 拒绝 / 撤回均产生新版本） -------------------

    def _new_consent(
        self,
        account_id: str,
        purpose: str,
        decision: ConsentDecision,
        at: datetime,
    ) -> ConsentVersion:
        version = self.repo.max_consent_version(account_id, purpose) + 1
        consent = ConsentVersion(
            consent_id=f"C-{uuid.uuid4().hex[:12]}",
            account_id=account_id,
            version=version,
            purpose=purpose,
            decision=decision,
            effective_at=at,
            created_at=self.now(),
        )
        self.repo.add_consent(consent)
        return consent

    def grant(self, account_id: str, purpose: str, at: datetime | None = None) -> ConsentVersion:
        return self._new_consent(account_id, purpose, ConsentDecision.GRANTED, at or self.now())

    def deny(self, account_id: str, purpose: str, at: datetime | None = None) -> ConsentVersion:
        return self._new_consent(account_id, purpose, ConsentDecision.DENIED, at or self.now())

    def withdraw(self, account_id: str, purpose: str, at: datetime | None = None) -> ConsentVersion:
        """撤回同意：不删除历史版本，仅新增 withdrawn 版本，自该时点起阻止后续用途。"""

        return self._new_consent(account_id, purpose, ConsentDecision.WITHDRAWN, at or self.now())

    # ---- 访问判定与解释 ----------------------------------------------

    def evaluate_access(
        self,
        actor_account_id: str,
        operation: Operation,
        purpose: str,
        at: datetime | None = None,
        record_id: str | None = None,
        vehicle_id: str | None = None,
        session_id: str | None = None,
        commit: bool = True,
    ) -> AccessDecision:
        """判定一次访问并写入审计；返回带完整理由链的 AccessDecision。

        无论允许还是拒绝都会落审计，满足“历史访问必须留痕”。
        """

        at = at or self.now()
        reasons: list[str] = []
        record = self.repo.get_record(record_id) if record_id else None
        owner = record.owner_account_id if record else actor_account_id
        eff_vehicle = record.vehicle_id if record else vehicle_id
        eff_session = record.session_id if record else session_id
        consent: ConsentVersion | None = None
        purpose_obj = self.repo.get_purpose(purpose)

        allowed = True

        if purpose_obj is None:
            allowed = False
            reasons.append(f"用途 {purpose} 未登记，无法据此处理数据")

        # 1. 操作必须落在该用途声明的操作范围内
        if purpose_obj is not None and operation not in purpose_obj.operations:
            allowed = False
            allowed_ops = ", ".join(sorted(o.value for o in purpose_obj.operations))
            reasons.append(
                f"用途 {purpose} 仅授权操作 {allowed_ops}，不包含 {operation.value}"
            )

        # 2. 记录状态与合法保留
        active_holds: list[LegalHold] = []
        if record is not None:
            if record.status == RecordStatus.DELETED:
                allowed = False
                reasons.append(f"记录 {record.record_id} 已删除，不能再访问")
            active_holds = self.repo.active_holds_for(record.record_id, at)

        is_investigation = bool(purpose_obj and purpose_obj.legal_investigation)
        if record is not None and active_holds:
            hold_desc = "; ".join(
                f"{h.hold_id}（{h.reason}，保留至 {h.until.isoformat()}）" for h in active_holds
            )
            if is_investigation:
                reasons.append(f"记录处于合法保留 {hold_desc}，依法定调查用途访问")
            else:
                allowed = False
                reasons.append(
                    f"记录处于合法保留 {hold_desc}：仅事故调查等法定用途可访问，"
                    f"到期前不得用于 {purpose}"
                )

        # 3. 调查用途 + 有效保留构成独立法律依据，无需同意；其余路径继续校验同意
        legal_basis = bool(active_holds) and is_investigation and record is not None
        if record is not None and record.status != RecordStatus.DELETED and not legal_basis:
            if actor_account_id != owner:
                allowed = False
                reasons.append(
                    f"请求账号 {actor_account_id} 不是记录归属人 {owner}，"
                    "不能依据他人授权访问该数据（多人共车边界）"
                )

        # 4. 车内采集要求归属人在该时点为车辆的活动乘员
        if operation == Operation.COLLECT and not record and eff_vehicle:
            sessions = self.repo.resolve_sessions(eff_vehicle, at)
            if not any(s.account_id == owner for s in sessions):
                allowed = False
                reasons.append(
                    f"账号 {owner} 在 {at.isoformat()} 不是车辆 {eff_vehicle} 的活动乘员，"
                    "采集必须绑定其本人的乘员会话"
                )
        if session_id is not None:
            session = self.repo.get_session(session_id)
            if session is None:
                allowed = False
                reasons.append(f"乘员会话 {session_id} 不存在")
            elif session.account_id != owner:
                allowed = False
                reasons.append(
                    f"会话 {session_id} 属于 {session.account_id}，与数据归属人 {owner} 不一致"
                )

        # 5. 按事件时点解析同意版本（合法保留+调查用途的独立法律依据除外）
        need_consent = (
            purpose_obj is not None
            and not legal_basis
            and (record is None or record.status != RecordStatus.DELETED)
            and actor_account_id == owner
        )
        if need_consent:
            consent = self.repo.resolve_consent(owner, purpose, at)
            if consent is None:
                allowed = False
                reasons.append(
                    f"事件时点 {at.isoformat()} 账号 {owner} 对用途 {purpose} 无任何有效同意版本"
                )
            elif consent.decision == ConsentDecision.GRANTED:
                reasons.append(
                    f"命中同意版本 v{consent.version}（{consent.consent_id}，"
                    f"自 {consent.effective_at.isoformat()} 生效）：GRANTED"
                )
            elif consent.decision == ConsentDecision.WITHDRAWN:
                allowed = False
                reasons.append(
                    f"命中同意版本 v{consent.version}（{consent.consent_id}，"
                    f"自 {consent.effective_at.isoformat()} 生效）：WITHDRAWN——"
                    "撤回仅阻止该时点之后的用途，不改变历史访问的判定"
                )
            else:
                allowed = False
                reasons.append(
                    f"命中同意版本 v{consent.version}（{consent.consent_id}，"
                    f"自 {consent.effective_at.isoformat()} 生效）：DENIED"
                )

        decision = AccessDecision(
            allowed=allowed,
            operation=operation,
            account_id=owner,
            purpose=purpose,
            at=at,
            reasons=reasons,
            vehicle_id=eff_vehicle,
            session_id=eff_session,
            record_id=record_id,
            consent_id=consent.consent_id if consent else None,
            consent_version=consent.version if consent else None,
            consent_decision=consent.decision if consent else None,
            consent_effective_at=consent.effective_at if consent else None,
            evaluated_at=self.now(),
            detail={
                "actor_account_id": actor_account_id,
                "legal_basis_hold": legal_basis,
                "holds": [
                    {"hold_id": h.hold_id, "reason": h.reason, "until": h.until.isoformat()}
                    for h in active_holds
                ],
            },
        )
        audit_id = self.repo.insert_audit(
            {
                "at": self.now(),
                "evaluated_at": at,
                "operation": operation.value,
                "actor_account": actor_account_id,
                "target_record": record_id,
                "purpose": purpose,
                "vehicle_id": eff_vehicle,
                "session_id": eff_session,
                "allowed": allowed,
                "reasons": reasons,
                "resolved_consent_id": decision.consent_id,
                "resolved_version": decision.consent_version,
                "detail": decision.detail,
            }
        )
        decision = replace(decision, audit_id=audit_id)
        if commit:
            self.repo.commit()
        return decision

    def explain(self, audit_id: int) -> AccessDecision | None:
        """按审计编号还原一次访问为何获准或拒绝。"""

        entry = self.repo.get_audit(audit_id)
        if entry is None:
            return None
        return AccessDecision(
            allowed=entry.allowed,
            operation=Operation(entry.operation),
            account_id=entry.actor_account or "",
            purpose=entry.purpose or "",
            at=entry.evaluated_at,
            reasons=list(entry.reasons),
            vehicle_id=entry.vehicle_id,
            session_id=entry.session_id,
            record_id=entry.target_record,
            consent_id=entry.resolved_consent_id,
            consent_version=entry.resolved_version,
            evaluated_at=entry.at,
            audit_id=entry.audit_id,
            detail=entry.detail,
        )

    def list_audit(self, **kwargs):
        return self.repo.list_audit(**kwargs)

    # ---- 采集与派生 ---------------------------------------------------

    def collect(
        self,
        record_id: str,
        account_id: str,
        vehicle_id: str,
        session_id: str,
        purpose: str,
        kind: str,
        at: datetime | None = None,
    ) -> DataRecord:
        at = at or self.now()
        decision = self.evaluate_access(
            account_id,
            Operation.COLLECT,
            purpose,
            at=at,
            vehicle_id=vehicle_id,
            session_id=session_id,
        )
        if not decision.allowed:
            raise AccessDeniedError(decision)
        record = DataRecord(
            record_id=record_id,
            owner_account_id=account_id,
            vehicle_id=vehicle_id,
            session_id=session_id,
            purpose=purpose,
            kind=kind,
            collected_at=at,
        )
        self.repo.add_record(record)
        self.repo.commit()
        return record

    def create_derived(
        self,
        record_id: str,
        owner_account_id: str,
        purpose: str,
        kind: str,
        source_ids: tuple[str, ...],
        at: datetime | None = None,
        vehicle_id: str | None = None,
        session_id: str | None = None,
    ) -> DataRecord:
        """从来源记录生成派生物（如训练样本）：按 USE + 目标用途做时点判定并记录谱系。"""

        at = at or self.now()
        if not source_ids:
            raise ValueError("派生物必须至少有一个来源记录")
        sources = [self.repo.get_record(sid) for sid in source_ids]
        if any(s is None for s in sources):
            raise ValueError("来源记录不存在")
        foreign = {s.record_id for s in sources if s.owner_account_id != owner_account_id}
        if foreign:
            raise ValueError(
                f"来源记录 {sorted(foreign)} 不属于 {owner_account_id}，"
                "不能混入其派生物（多人共车数据边界）"
            )
        first = sources[0]
        decision = self.evaluate_access(
            owner_account_id,
            Operation.USE,
            purpose,
            at=at,
            record_id=first.record_id,
        )
        if not decision.allowed:
            raise AccessDeniedError(decision)
        record = DataRecord(
            record_id=record_id,
            owner_account_id=owner_account_id,
            vehicle_id=vehicle_id or first.vehicle_id,
            session_id=session_id or first.session_id,
            purpose=purpose,
            kind=kind,
            collected_at=at,
            derived_from=source_ids,
        )
        self.repo.add_record(record)
        self.repo.commit()
        return record

    # ---- 合法保留（含派生物传播，有期限，到期自动释放） ---------------

    def place_hold(
        self,
        record_id: str,
        until: datetime,
        reason: str,
        hold_id: str | None = None,
        at: datetime | None = None,
    ) -> list[LegalHold]:
        """对记录及其可定位派生物设置同一保留期限的合法保留。"""

        at = at or self.now()
        root = self.repo.get_record(record_id)
        if root is None:
            raise ValueError(f"记录 {record_id} 不存在")
        hold_id = hold_id or f"H-{uuid.uuid4().hex[:10]}"
        targets = self._descendants(record_id) | {record_id}
        holds: list[LegalHold] = []
        for rid in sorted(targets):
            hid = hold_id if rid == record_id else f"{hold_id}:{rid}"
            h = LegalHold(
                hold_id=hid,
                record_id=rid,
                reason=reason if rid == record_id else f"{reason}（派生物随原始记录保留）",
                created_at=at,
                until=until,
            )
            self.repo.add_hold(h)
            holds.append(h)
        self.repo.commit()
        return holds

    def _descendants(self, record_id: str) -> set[str]:
        """沿 derived_from 反向边求可定位的派生物闭包。"""

        result: set[str] = set()
        frontier = [record_id]
        while frontier:
            parent = frontier.pop()
            for rec in self.repo.records_for_account(
                self.repo.get_record(parent).owner_account_id
            ):
                if parent in rec.derived_from and rec.record_id not in result:
                    result.add(rec.record_id)
                    frontier.append(rec.record_id)
        return result

    def _lineage_targets(self, account_id: str) -> list[DataRecord]:
        """账号名下全部活跃记录，以及沿谱系可定位的派生物。"""

        roots = [
            r
            for r in self.repo.records_for_account(account_id)
            if r.status == RecordStatus.ACTIVE
        ]
        target_ids = {r.record_id for r in roots}
        for r in roots:
            target_ids |= self._descendants(r.record_id)
        targets = [self.repo.get_record(rid) for rid in target_ids]
        return sorted(
            (t for t in targets if t is not None and t.status == RecordStatus.ACTIVE),
            key=lambda r: (r.collected_at, r.record_id),
        )

    # ---- 导出作业（幂等、可重试） -------------------------------------

    def request_export(
        self,
        account_id: str,
        request_id: str,
        purpose: str | None = None,
        at: datetime | None = None,
    ) -> ExportJob:
        existing = self.repo.get_export_job_by_request(request_id)
        if existing is not None:
            return existing  # 重复请求：返回同一作业，不产生第二个作业
        at = at or self.now()
        job = ExportJob(
            job_id=f"E-{uuid.uuid4().hex[:10]}",
            request_id=request_id,
            account_id=account_id,
            purpose=purpose,
            status=JobStatus.PENDING,
            attempts=0,
            last_error=None,
            bundle=None,
            created_at=at,
            updated_at=at,
            completed_at=None,
        )
        self.repo.insert_export_job(job)
        self.repo.commit()
        return job

    def run_export(self, job_id: str, at: datetime | None = None, fail: bool = False) -> ExportJob:
        at = at or self.now()
        job = self.repo.get_export_job(job_id)
        if job is None:
            raise ValueError(f"导出作业 {job_id} 不存在")
        if job.status == JobStatus.COMPLETED:
            return job  # 已完成的重试是幂等空操作

        job = replace(job, status=JobStatus.RUNNING, attempts=job.attempts + 1, updated_at=at)
        self.repo.update_export_job(job)
        self.repo.commit()

        try:
            included, excluded = [], []
            records = self.repo.records_for_account(job.account_id)
            for rec in records:
                if rec.status == RecordStatus.DELETED:
                    continue
                if job.purpose and rec.purpose != job.purpose:
                    continue
                decision = self.evaluate_access(
                    job.account_id, Operation.EXPORT, job.purpose or rec.purpose,
                    at=at, record_id=rec.record_id,
                )
                if decision.allowed:
                    included.append(
                        {
                            "record_id": rec.record_id,
                            "kind": rec.kind,
                            "purpose": rec.purpose,
                            "collected_at": rec.collected_at.isoformat(),
                            "derived_from": list(rec.derived_from),
                            "audit_id": decision.audit_id,
                        }
                    )
                else:
                    excluded.append(
                        {"record_id": rec.record_id, "reasons": decision.reasons,
                         "audit_id": decision.audit_id}
                    )
            if fail:
                raise RuntimeError("模拟导出通道瞬时故障")
            bundle = {"exported_at": at.isoformat(), "records": included, "excluded": excluded}
            job = replace(
                job, status=JobStatus.COMPLETED, bundle=bundle,
                last_error=None, completed_at=at, updated_at=at,
            )
        except Exception as exc:  # 故障可重试：保留 PENDING 之外的全部审计痕迹
            job = replace(job, status=JobStatus.FAILED, last_error=str(exc), updated_at=at)
        self.repo.update_export_job(job)
        self.repo.commit()
        return job

    def get_export_job(self, job_id: str) -> ExportJob | None:
        return self.repo.get_export_job(job_id)

    # ---- 删除作业（追踪原始+派生、保留冲突挂起、到期自动续处理） ------

    def request_delete(
        self, account_id: str, request_id: str, at: datetime | None = None
    ) -> DeleteJob:
        existing = self.repo.get_delete_job_by_request(request_id)
        if existing is not None:
            return existing  # 重复请求幂等
        at = at or self.now()
        job = DeleteJob(
            job_id=f"D-{uuid.uuid4().hex[:10]}",
            request_id=request_id,
            account_id=account_id,
            status=JobStatus.PENDING,
            attempts=0,
            last_error=None,
            items=(),
            created_at=at,
            updated_at=at,
            completed_at=None,
        )
        self.repo.insert_delete_job(job)
        self.repo.commit()
        return job

    def run_delete(self, job_id: str, at: datetime | None = None, fail: bool = False) -> DeleteJob:
        at = at or self.now()
        job = self.repo.get_delete_job(job_id)
        if job is None:
            raise ValueError(f"删除作业 {job_id} 不存在")
        if job.status == JobStatus.COMPLETED:
            return job

        # 首次运行时展开目标：原始记录 + 可定位派生物
        items = list(self.repo.delete_items_for(job_id))
        if not items:
            targets = self._lineage_targets(job.account_id)
            items = [
                DeleteItem(
                    record_id=t.record_id,
                    owner_account_id=t.owner_account_id,
                    kind=t.kind,
                    state=DeleteItemState.PENDING,
                    reason="纳入删除范围",
                    updated_at=at,
                )
                for t in targets
            ]
            for item in items:
                self.repo.insert_delete_item(job.job_id, item)
        job = replace(job, status=JobStatus.RUNNING, attempts=job.attempts + 1, updated_at=at)
        self.repo.update_delete_job(job, items)
        self.repo.commit()

        try:
            processed = 0
            for idx, item in enumerate(items):
                rec = self.repo.get_record(item.record_id)
                if rec is not None and rec.status == RecordStatus.DELETED:
                    reason = "已删除" if item.state == DeleteItemState.PENDING else "此前尝试中已删除"
                    items[idx] = replace(
                        item, state=DeleteItemState.DELETED, reason=reason, updated_at=at,
                    )
                    continue
                holds = self.repo.active_holds_for(item.record_id, at) if rec else []
                if holds:
                    nearest = min(h.until for h in holds)
                    items[idx] = replace(
                        item, state=DeleteItemState.HELD,
                        reason=f"合法保留 {holds[0].reason}，挂起至 {nearest.isoformat()}",
                        hold_id=holds[0].hold_id, held_until=nearest, updated_at=at,
                    )
                    self._audit_delete(job, item, at, allowed=False,
                                       reason=f"删除被合法保留 {holds[0].hold_id} 挂起")
                else:
                    if rec is not None:
                        self.repo.mark_record_deleted(rec.record_id, at)
                    items[idx] = replace(
                        item, state=DeleteItemState.DELETED, reason="已删除",
                        hold_id=None, held_until=None, updated_at=at,
                    )
                    self._audit_delete(job, item, at, allowed=True, reason="用户删除请求已执行")
                processed += 1
                if fail and processed == 1:
                    raise RuntimeError("模拟删除执行器瞬时故障")

            still_held = [i for i in items if i.state == DeleteItemState.HELD]
            if still_held:
                status = JobStatus.AWAITING_HOLD
                completed_at = None
                last_error = None
            else:
                status = JobStatus.COMPLETED
                completed_at = at
                last_error = None
            job = replace(
                job, status=status, last_error=last_error,
                completed_at=completed_at, updated_at=at,
            )
        except Exception as exc:
            job = replace(job, status=JobStatus.FAILED, last_error=str(exc), updated_at=at)
        self.repo.update_delete_job(job, items)
        self.repo.commit()
        return self.repo.get_delete_job(job_id)  # 从库重载，items 反映本次处理结果

    def _audit_delete(self, job: DeleteJob, item: DeleteItem, at: datetime,
                      allowed: bool, reason: str) -> None:
        self.repo.insert_audit(
            {
                "at": self.now(),
                "evaluated_at": at,
                "operation": Operation.DELETE.value,
                "actor_account": job.account_id,
                "target_record": item.record_id,
                "purpose": "erasure-request",
                "vehicle_id": None,
                "session_id": None,
                "allowed": allowed,
                "reasons": [reason, f"request_id={job.request_id}"],
                "resolved_consent_id": None,
                "resolved_version": None,
                "detail": {"job_id": job.job_id, "hold_id": item.hold_id},
            }
        )

    def get_delete_job(self, job_id: str) -> DeleteJob | None:
        return self.repo.get_delete_job(job_id)

    # ---- 时间推进与重启恢复 -------------------------------------------

    def tick(self, at: datetime | None = None) -> dict:
        """推进时钟：释放到期保留，并自动续处理挂起/失败的删除与失败的导出作业。"""

        at = at or self.now()
        released = self.repo.release_due_holds(at)
        resumed: list[str] = []
        for job in self.repo.delete_jobs_by_status([JobStatus.AWAITING_HOLD, JobStatus.FAILED]):
            self.run_delete(job.job_id, at=at)
            resumed.append(job.job_id)
        for job in self.repo.export_jobs_by_status([JobStatus.FAILED]):
            self.run_export(job.job_id, at=at)
            resumed.append(job.job_id)
        self.repo.commit()
        return {
            "at": at.isoformat(),
            "released_holds": [h.hold_id for h in released],
            "resumed_jobs": resumed,
        }

    def recover(self) -> list[str]:
        """重启恢复：运行中断在 RUNNING 的作业回到 FAILED，等待重试/tick 续处理。"""

        recovered: list[str] = []
        note = "进程中断，作业已恢复为可重试状态"
        for job in self.repo.export_jobs_by_status([JobStatus.RUNNING]):
            job = replace(job, status=JobStatus.FAILED, last_error=note, updated_at=self.now())
            self.repo.update_export_job(job)
            recovered.append(job.job_id)
        for job in self.repo.delete_jobs_by_status([JobStatus.RUNNING]):
            job = replace(job, status=JobStatus.FAILED, last_error=note, updated_at=self.now())
            items = self.repo.delete_items_for(job.job_id)
            self.repo.update_delete_job(job, items)
            recovered.append(job.job_id)
        self.repo.commit()
        return recovered
