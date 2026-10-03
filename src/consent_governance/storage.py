"""SQLite 持久化层。

时间统一以带时区的 UTC ISO-8601 字符串存储，字典/集合以 JSON 存储。
所有治理状态（含作业与审计）都落库，使进程重启后可以完整恢复。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    Account,
    AuditEntry,
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purposes (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    operations TEXT NOT NULL,
    legal_investigation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS occupant_sessions (
    session_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS consent_versions (
    consent_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    decision TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, purpose, version)
);
CREATE TABLE IF NOT EXISTS data_records (
    record_id TEXT PRIMARY KEY,
    owner_account_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    kind TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    derived_from TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS legal_holds (
    hold_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    until TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    actor_account TEXT,
    target_record TEXT,
    purpose TEXT,
    vehicle_id TEXT,
    session_id TEXT,
    allowed INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    resolved_consent_id TEXT,
    resolved_version INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS export_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    purpose TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    bundle TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS delete_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS delete_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    owner_account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    hold_id TEXT,
    held_until TEXT,
    updated_at TEXT,
    UNIQUE(job_id, record_id)
);
"""


def to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def from_dt(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间戳必须带时区")
    return value.astimezone(timezone.utc).isoformat()


class Repository:
    """薄封装：只负责读写，不含业务判定。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ---- 基础实体 -----------------------------------------------------

    def add_vehicle(self, vehicle: Vehicle) -> None:
        self.conn.execute(
            "INSERT INTO vehicles(vehicle_id, registered_at) VALUES (?,?)",
            (vehicle.vehicle_id, from_dt(vehicle.registered_at)),
        )

    def add_account(self, account: Account) -> None:
        self.conn.execute(
            "INSERT INTO accounts(account_id, created_at) VALUES (?,?)",
            (account.account_id, from_dt(account.created_at)),
        )

    def add_purpose(self, purpose: Purpose) -> None:
        self.conn.execute(
            "INSERT INTO purposes(code, title, operations, legal_investigation) VALUES (?,?,?,?)",
            (
                purpose.code,
                purpose.title,
                json.dumps(sorted(op.value for op in purpose.operations)),
                1 if purpose.legal_investigation else 0,
            ),
        )

    def get_purpose(self, code: str) -> Purpose | None:
        row = self.conn.execute("SELECT * FROM purposes WHERE code=?", (code,)).fetchone()
        return self._row_to_purpose(row) if row else None

    @staticmethod
    def _row_to_purpose(row: sqlite3.Row) -> Purpose:
        return Purpose(
            code=row["code"],
            title=row["title"],
            operations=frozenset(Operation(o) for o in json.loads(row["operations"])),
            legal_investigation=bool(row["legal_investigation"]),
        )

    # ---- 乘员会话 -----------------------------------------------------

    def add_session(self, session: OccupantSession) -> None:
        self.conn.execute(
            "INSERT INTO occupant_sessions(session_id, vehicle_id, account_id, started_at, ended_at)"
            " VALUES (?,?,?,?,?)",
            (
                session.session_id,
                session.vehicle_id,
                session.account_id,
                from_dt(session.started_at),
                from_dt(session.ended_at) if session.ended_at else None,
            ),
        )

    def end_session(self, session_id: str, ended_at: datetime) -> None:
        self.conn.execute(
            "UPDATE occupant_sessions SET ended_at=? WHERE session_id=?",
            (from_dt(ended_at), session_id),
        )

    def get_session(self, session_id: str) -> OccupantSession | None:
        row = self.conn.execute(
            "SELECT * FROM occupant_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def open_sessions_for_vehicle(self, vehicle_id: str) -> list[OccupantSession]:
        rows = self.conn.execute(
            "SELECT * FROM occupant_sessions WHERE vehicle_id=? AND ended_at IS NULL",
            (vehicle_id,),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def sessions_for_vehicle(self, vehicle_id: str) -> list[OccupantSession]:
        rows = self.conn.execute(
            "SELECT * FROM occupant_sessions WHERE vehicle_id=? ORDER BY started_at",
            (vehicle_id,),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def resolve_sessions(self, vehicle_id: str, at: datetime) -> list[OccupantSession]:
        """车辆在某一时点处于活动状态的乘员会话（started_at <= at < ended_at）。"""

        iso = from_dt(at)
        rows = self.conn.execute(
            "SELECT * FROM occupant_sessions WHERE vehicle_id=? AND started_at<=?"
            " AND (ended_at IS NULL OR ended_at>?)",
            (vehicle_id, iso, iso),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> OccupantSession:
        return OccupantSession(
            session_id=row["session_id"],
            vehicle_id=row["vehicle_id"],
            account_id=row["account_id"],
            started_at=to_dt(row["started_at"]),
            ended_at=to_dt(row["ended_at"]) if row["ended_at"] else None,
        )

    # ---- 同意版本 -----------------------------------------------------

    def add_consent(self, consent: ConsentVersion) -> None:
        self.conn.execute(
            "INSERT INTO consent_versions(consent_id, account_id, version, purpose, decision,"
            " effective_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                consent.consent_id,
                consent.account_id,
                consent.version,
                consent.purpose,
                consent.decision.value,
                from_dt(consent.effective_at),
                from_dt(consent.created_at),
            ),
        )

    def max_consent_version(self, account_id: str, purpose: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS m FROM consent_versions WHERE account_id=? AND purpose=?",
            (account_id, purpose),
        ).fetchone()
        return row["m"] or 0

    def resolve_consent(
        self, account_id: str, purpose: str, at: datetime
    ) -> ConsentVersion | None:
        """事件时点有效的最新同意版本（effective_at <= at，版本号大者优先）。"""

        row = self.conn.execute(
            "SELECT * FROM consent_versions WHERE account_id=? AND purpose=? AND effective_at<=?"
            " ORDER BY effective_at DESC, version DESC LIMIT 1",
            (account_id, purpose, from_dt(at)),
        ).fetchone()
        return self._row_to_consent(row) if row else None

    def consents_for(self, account_id: str) -> list[ConsentVersion]:
        rows = self.conn.execute(
            "SELECT * FROM consent_versions WHERE account_id=? ORDER BY purpose, version",
            (account_id,),
        ).fetchall()
        return [self._row_to_consent(r) for r in rows]

    @staticmethod
    def _row_to_consent(row: sqlite3.Row) -> ConsentVersion:
        return ConsentVersion(
            consent_id=row["consent_id"],
            account_id=row["account_id"],
            version=row["version"],
            purpose=row["purpose"],
            decision=ConsentDecision(row["decision"]),
            effective_at=to_dt(row["effective_at"]),
            created_at=to_dt(row["created_at"]),
        )

    # ---- 数据记录与派生谱系 -------------------------------------------

    def add_record(self, record: DataRecord) -> None:
        self.conn.execute(
            "INSERT INTO data_records(record_id, owner_account_id, vehicle_id, session_id,"
            " purpose, kind, collected_at, status, derived_from, deleted_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                record.record_id,
                record.owner_account_id,
                record.vehicle_id,
                record.session_id,
                record.purpose,
                record.kind,
                from_dt(record.collected_at),
                record.status.value,
                json.dumps(list(record.derived_from)),
                from_dt(record.deleted_at) if record.deleted_at else None,
            ),
        )

    def get_record(self, record_id: str) -> DataRecord | None:
        row = self.conn.execute(
            "SELECT * FROM data_records WHERE record_id=?", (record_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def records_for_account(self, account_id: str) -> list[DataRecord]:
        rows = self.conn.execute(
            "SELECT * FROM data_records WHERE owner_account_id=? ORDER BY collected_at",
            (account_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def mark_record_deleted(self, record_id: str, at: datetime) -> None:
        self.conn.execute(
            "UPDATE data_records SET status=?, deleted_at=? WHERE record_id=?",
            (RecordStatus.DELETED.value, from_dt(at), record_id),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DataRecord:
        return DataRecord(
            record_id=row["record_id"],
            owner_account_id=row["owner_account_id"],
            vehicle_id=row["vehicle_id"],
            session_id=row["session_id"],
            purpose=row["purpose"],
            kind=row["kind"],
            collected_at=to_dt(row["collected_at"]),
            status=RecordStatus(row["status"]),
            derived_from=tuple(json.loads(row["derived_from"])),
            deleted_at=to_dt(row["deleted_at"]) if row["deleted_at"] else None,
        )

    # ---- 合法保留 -----------------------------------------------------

    def add_hold(self, hold: LegalHold) -> None:
        self.conn.execute(
            "INSERT INTO legal_holds(hold_id, record_id, reason, created_at, until, released_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                hold.hold_id,
                hold.record_id,
                hold.reason,
                from_dt(hold.created_at),
                from_dt(hold.until),
                from_dt(hold.released_at) if hold.released_at else None,
            ),
        )

    def active_holds_for(self, record_id: str, at: datetime) -> list[LegalHold]:
        iso = from_dt(at)
        rows = self.conn.execute(
            "SELECT * FROM legal_holds WHERE record_id=? AND created_at<=? AND released_at IS NULL"
            " AND until>?",
            (record_id, iso, iso),
        ).fetchall()
        return [self._row_to_hold(r) for r in rows]

    def release_due_holds(self, at: datetime) -> list[LegalHold]:
        iso = from_dt(at)
        rows = self.conn.execute(
            "SELECT * FROM legal_holds WHERE released_at IS NULL AND until<=?", (iso,)
        ).fetchall()
        released = [self._row_to_hold(r) for r in rows]
        self.conn.execute(
            "UPDATE legal_holds SET released_at=? WHERE released_at IS NULL AND until<=?",
            (iso, iso),
        )
        return released

    @staticmethod
    def _row_to_hold(row: sqlite3.Row) -> LegalHold:
        return LegalHold(
            hold_id=row["hold_id"],
            record_id=row["record_id"],
            reason=row["reason"],
            created_at=to_dt(row["created_at"]),
            until=to_dt(row["until"]),
            released_at=to_dt(row["released_at"]) if row["released_at"] else None,
        )

    # ---- 审计留痕 -----------------------------------------------------

    def insert_audit(self, params: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_log(at, evaluated_at, operation, actor_account, target_record,"
            " purpose, vehicle_id, session_id, allowed, reasons, resolved_consent_id,"
            " resolved_version, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                from_dt(params["at"]),
                from_dt(params["evaluated_at"]),
                params["operation"],
                params.get("actor_account"),
                params.get("target_record"),
                params.get("purpose"),
                params.get("vehicle_id"),
                params.get("session_id"),
                1 if params["allowed"] else 0,
                json.dumps(params["reasons"], ensure_ascii=False),
                params.get("resolved_consent_id"),
                params.get("resolved_version"),
                json.dumps(params.get("detail"), ensure_ascii=False)
                if params.get("detail") is not None
                else None,
            ),
        )
        return int(cur.lastrowid)

    def get_audit(self, audit_id: int) -> AuditEntry | None:
        row = self.conn.execute(
            "SELECT * FROM audit_log WHERE audit_id=?", (audit_id,)
        ).fetchone()
        return self.row_to_audit(row) if row else None

    def list_audit(
        self, record_id: str | None = None, account_id: str | None = None
    ) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        args: list[Any] = []
        if record_id is not None:
            sql += " AND target_record=?"
            args.append(record_id)
        if account_id is not None:
            sql += " AND actor_account=?"
            args.append(account_id)
        sql += " ORDER BY audit_id"
        return [self.row_to_audit(r) for r in self.conn.execute(sql, args).fetchall()]

    @staticmethod
    def row_to_audit(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            audit_id=row["audit_id"],
            at=to_dt(row["at"]),
            evaluated_at=to_dt(row["evaluated_at"]),
            operation=row["operation"],
            actor_account=row["actor_account"],
            target_record=row["target_record"],
            purpose=row["purpose"],
            vehicle_id=row["vehicle_id"],
            session_id=row["session_id"],
            allowed=bool(row["allowed"]),
            reasons=tuple(json.loads(row["reasons"])),
            resolved_consent_id=row["resolved_consent_id"],
            resolved_version=row["resolved_version"],
            detail=json.loads(row["detail"]) if row["detail"] else None,
        )

    # ---- 导出作业 -----------------------------------------------------

    def insert_export_job(self, job: ExportJob) -> None:
        self.conn.execute(
            "INSERT INTO export_jobs(job_id, request_id, account_id, purpose, status, attempts,"
            " last_error, bundle, created_at, updated_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.job_id,
                job.request_id,
                job.account_id,
                job.purpose,
                job.status.value,
                job.attempts,
                job.last_error,
                json.dumps(job.bundle, ensure_ascii=False) if job.bundle is not None else None,
                from_dt(job.created_at),
                from_dt(job.updated_at),
                from_dt(job.completed_at) if job.completed_at else None,
            ),
        )

    def get_export_job_by_request(self, request_id: str) -> ExportJob | None:
        row = self.conn.execute(
            "SELECT * FROM export_jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        return self.row_to_export_job(row) if row else None

    def get_export_job(self, job_id: str) -> ExportJob | None:
        row = self.conn.execute(
            "SELECT * FROM export_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return self.row_to_export_job(row) if row else None

    def update_export_job(self, job: ExportJob) -> None:
        self.conn.execute(
            "UPDATE export_jobs SET status=?, attempts=?, last_error=?, bundle=?,"
            " updated_at=?, completed_at=? WHERE job_id=?",
            (
                job.status.value,
                job.attempts,
                job.last_error,
                json.dumps(job.bundle, ensure_ascii=False) if job.bundle is not None else None,
                from_dt(job.updated_at),
                from_dt(job.completed_at) if job.completed_at else None,
                job.job_id,
            ),
        )

    def export_jobs_by_status(self, status: Iterable[JobStatus]) -> list[ExportJob]:
        marks = ",".join("?" for _ in status)
        rows = self.conn.execute(
            f"SELECT * FROM export_jobs WHERE status IN ({marks}) ORDER BY created_at",
            tuple(s.value for s in status),
        ).fetchall()
        return [self.row_to_export_job(r) for r in rows]

    @staticmethod
    def row_to_export_job(row: sqlite3.Row) -> ExportJob:
        return ExportJob(
            job_id=row["job_id"],
            request_id=row["request_id"],
            account_id=row["account_id"],
            purpose=row["purpose"],
            status=JobStatus(row["status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            bundle=json.loads(row["bundle"]) if row["bundle"] else None,
            created_at=to_dt(row["created_at"]),
            updated_at=to_dt(row["updated_at"]),
            completed_at=to_dt(row["completed_at"]) if row["completed_at"] else None,
        )

    # ---- 删除作业 -----------------------------------------------------

    def insert_delete_job(self, job: DeleteJob) -> None:
        self.conn.execute(
            "INSERT INTO delete_jobs(job_id, request_id, account_id, status, attempts,"
            " last_error, created_at, updated_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                job.job_id,
                job.request_id,
                job.account_id,
                job.status.value,
                job.attempts,
                job.last_error,
                from_dt(job.created_at),
                from_dt(job.updated_at),
                from_dt(job.completed_at) if job.completed_at else None,
            ),
        )
        for item in job.items:
            self.insert_delete_item(job.job_id, item)

    def insert_delete_item(self, job_id: str, item: DeleteItem) -> None:
        self.conn.execute(
            "INSERT INTO delete_items(job_id, record_id, owner_account_id, kind, state, reason,"
            " hold_id, held_until, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                item.record_id,
                item.owner_account_id,
                item.kind,
                item.state.value,
                item.reason,
                item.hold_id,
                from_dt(item.held_until) if item.held_until else None,
                from_dt(item.updated_at) if item.updated_at else None,
            ),
        )

    def get_delete_job_by_request(self, request_id: str) -> DeleteJob | None:
        row = self.conn.execute(
            "SELECT * FROM delete_jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        return self._row_to_delete_job(row) if row else None

    def get_delete_job(self, job_id: str) -> DeleteJob | None:
        row = self.conn.execute(
            "SELECT * FROM delete_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return self._row_to_delete_job(row) if row else None

    def update_delete_job(self, job: DeleteJob, items: list[DeleteItem]) -> None:
        self.conn.execute(
            "UPDATE delete_jobs SET status=?, attempts=?, last_error=?, updated_at=?,"
            " completed_at=? WHERE job_id=?",
            (
                job.status.value,
                job.attempts,
                job.last_error,
                from_dt(job.updated_at),
                from_dt(job.completed_at) if job.completed_at else None,
                job.job_id,
            ),
        )
        for item in items:
            self.conn.execute(
                "UPDATE delete_items SET state=?, reason=?, hold_id=?, held_until=?, updated_at=?"
                " WHERE job_id=? AND record_id=?",
                (
                    item.state.value,
                    item.reason,
                    item.hold_id,
                    from_dt(item.held_until) if item.held_until else None,
                    from_dt(item.updated_at) if item.updated_at else None,
                    job.job_id,
                    item.record_id,
                ),
            )

    def delete_jobs_by_status(self, status: Iterable[JobStatus]) -> list[DeleteJob]:
        marks = ",".join("?" for _ in status)
        rows = self.conn.execute(
            f"SELECT * FROM delete_jobs WHERE status IN ({marks}) ORDER BY created_at",
            tuple(s.value for s in status),
        ).fetchall()
        return [self._row_to_delete_job(r) for r in rows]

    def delete_items_for(self, job_id: str) -> list[DeleteItem]:
        rows = self.conn.execute(
            "SELECT * FROM delete_items WHERE job_id=? ORDER BY item_id", (job_id,)
        ).fetchall()
        return [self._row_to_delete_item(r) for r in rows]

    @staticmethod
    def _row_to_delete_item(row: sqlite3.Row) -> DeleteItem:
        return DeleteItem(
            record_id=row["record_id"],
            owner_account_id=row["owner_account_id"],
            kind=row["kind"],
            state=DeleteItemState(row["state"]),
            reason=row["reason"],
            hold_id=row["hold_id"],
            held_until=to_dt(row["held_until"]) if row["held_until"] else None,
            updated_at=to_dt(row["updated_at"]) if row["updated_at"] else None,
        )

    def _row_to_delete_job(self, row: sqlite3.Row) -> DeleteJob:
        return DeleteJob(
            job_id=row["job_id"],
            request_id=row["request_id"],
            account_id=row["account_id"],
            status=JobStatus(row["status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            items=tuple(self.delete_items_for(row["job_id"])),
            created_at=to_dt(row["created_at"]),
            updated_at=to_dt(row["updated_at"]),
            completed_at=to_dt(row["completed_at"]) if row["completed_at"] else None,
        )
