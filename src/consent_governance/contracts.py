"""乘员会话、同意版本与数据治理的领域契约。

本模块只定义数据结构与枚举，不包含持久化与判定逻辑，
以便契约层可以被接口层、存储层与测试共同引用。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ConsentDecision(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


class Operation(StrEnum):
    """受授权治理的数据处理操作。"""

    COLLECT = "collect"
    USE = "use"
    SHARE = "share"
    EXPORT = "export"
    DELETE = "delete"  # 仅用于审计：删除请求本身不以同意为前提


class RecordStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_HOLD = "awaiting_hold"  # 部分目标处于合法保留期，到期后自动续处理


class DeleteItemState(StrEnum):
    PENDING = "pending"
    DELETED = "deleted"
    HELD = "held"


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    registered_at: datetime


@dataclass(frozen=True)
class Account:
    account_id: str
    created_at: datetime


@dataclass(frozen=True)
class Purpose:
    """数据用途：声明该用途下允许的处理操作。"""

    code: str
    title: str
    operations: frozenset[Operation]
    legal_investigation: bool = False  # 法定调查用途，可访问合法保留中的记录

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("用途编码不能为空")
        if not self.operations:
            raise ValueError("用途至少需要授权一种操作")


@dataclass(frozen=True)
class OccupantSession:
    """乘员会话：把某一时段内的车辆与账号绑定，是多人共车边界的关键。"""

    session_id: str
    vehicle_id: str
    account_id: str
    started_at: datetime
    ended_at: datetime | None = None


@dataclass(frozen=True)
class ConsentVersion:
    consent_id: str
    account_id: str
    version: int
    purpose: str
    decision: ConsentDecision
    effective_at: datetime | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("同意版本必须为正整数")
        if not self.purpose.strip():
            raise ValueError("数据用途不能为空")


@dataclass(frozen=True)
class DataRecord:
    """被治理的数据记录。derived_from 指向其来源记录，构成可追踪的派生谱系。"""

    record_id: str
    owner_account_id: str
    vehicle_id: str
    session_id: str
    purpose: str
    kind: str
    collected_at: datetime
    status: RecordStatus = RecordStatus.ACTIVE
    derived_from: tuple[str, ...] = ()
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class LegalHold:
    """有期限的合法保留（例如事故调查）。"""

    hold_id: str
    record_id: str
    reason: str
    created_at: datetime
    until: datetime
    released_at: datetime | None = None


@dataclass
class AccessDecision:
    """一次访问判定的完整解释：结论、事实依据与可读理由。"""

    allowed: bool
    operation: Operation
    account_id: str
    purpose: str
    at: datetime
    reasons: list[str] = field(default_factory=list)
    vehicle_id: str | None = None
    session_id: str | None = None
    record_id: str | None = None
    consent_id: str | None = None
    consent_version: int | None = None
    consent_decision: ConsentDecision | None = None
    consent_effective_at: datetime | None = None
    evaluated_at: datetime | None = None
    audit_id: int | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditEntry:
    audit_id: int
    at: datetime
    evaluated_at: datetime
    operation: str
    actor_account: str | None
    target_record: str | None
    purpose: str | None
    vehicle_id: str | None
    session_id: str | None
    allowed: bool
    reasons: tuple[str, ...]
    resolved_consent_id: str | None
    resolved_version: int | None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExportJob:
    job_id: str
    request_id: str
    account_id: str
    purpose: str | None
    status: JobStatus
    attempts: int
    last_error: str | None
    bundle: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class DeleteItem:
    record_id: str
    owner_account_id: str
    kind: str
    state: DeleteItemState
    reason: str
    hold_id: str | None = None
    held_until: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class DeleteJob:
    job_id: str
    request_id: str
    account_id: str
    status: JobStatus
    attempts: int
    last_error: str | None
    items: tuple[DeleteItem, ...]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
