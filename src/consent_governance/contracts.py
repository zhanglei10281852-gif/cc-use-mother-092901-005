"""数据授权治理的领域契约（值对象与枚举）。

授权判断一律以"事件发生时点"为准，因此所有领域对象都携带可比较的时间戳。
本模块只定义形状，不依赖存储实现。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


def utc_now() -> datetime:
    """统一的 UTC 时钟入口，测试可通过服务层的 at 参数替换。"""
    return datetime.now(timezone.utc)


class ConsentDecision(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


class Operation(StrEnum):
    """受授权约束的处理活动。"""

    COLLECT = "collect"
    USE = "use"
    SHARE = "share"
    EXPORT = "export"


class RecordState(StrEnum):
    ACTIVE = "active"      # 正常可处理（仍需事件时点的同意）
    HELD = "held"          # 处于合法保留期，暂停处理
    DELETED = "deleted"    # 已删除，仅保留墓碑信息


class JobKind(StrEnum):
    EXPORT = "export"
    DELETE = "delete"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"      # 部分完成（如保留期阻挡、部分记录被排除）
    FAILED = "failed"        # 本轮失败，可重试


class ItemStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    HELD = "held"                  # 删除被合法保留暂缓，到期后自动恢复
    EXCLUDED = "excluded"          # 导出时授权不足被排除
    NON_LOCATABLE = "non_locatable"  # 不可定位的匿名派生物


class ReasonCode(StrEnum):
    OK = "ok"
    UNKNOWN_PURPOSE = "unknown_purpose"
    NO_CONSENT = "no_consent"
    CONSENT_DENIED = "consent_denied"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    SESSION_MISMATCH = "session_mismatch"
    RECORD_NOT_FOUND = "record_not_found"
    RECORD_OWNER_MISMATCH = "record_owner_mismatch"
    RECORD_DELETED = "record_deleted"
    LEGAL_HOLD = "legal_hold"


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    registered_at: datetime


@dataclass(frozen=True)
class Account:
    account_id: str
    created_at: datetime


@dataclass(frozen=True)
class OccupantSession:
    """乘员会话：把车辆、账号与一段驾驶时间绑定，隔离同一辆车的不同驾驶人。"""

    session_id: str
    vehicle_id: str
    account_id: str
    started_at: datetime


@dataclass(frozen=True)
class DataPurpose:
    code: str
    description: str = ""


@dataclass(frozen=True)
class ConsentVersion:
    """同一账号同一用途的一次授权状态，版本只增不改。"""

    consent_id: str
    account_id: str
    version: int
    purpose: str
    decision: ConsentDecision
    recorded_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("同意版本必须为正整数")
        if not self.purpose.strip():
            raise ValueError("数据用途不能为空")
        # 触发枚举校验，拒绝未知决策字符串
        ConsentDecision(self.decision)


@dataclass(frozen=True)
class DataRecord:
    """一条被治理的数据（语音偏好、行程摘要、训练样本等）。

    derived_from 指向原始记录，构成可定位派生链；locatable=False 表示
    已匿名化聚合、无法通过血缘定位的派生物。
    """

    record_id: str
    owner_account: str
    purpose: str
    kind: str
    created_at: datetime
    session_id: str | None = None
    derived_from: str | None = None
    locatable: bool = True
    state: RecordState = RecordState.ACTIVE
    deleted_at: datetime | None = None


@dataclass
class AccessDecision:
    """一次访问判断的完整解释，获准与拒绝都要能说清依据。"""

    allowed: bool
    operation: Operation
    account_id: str
    purpose: str
    at: datetime
    reason_code: ReasonCode
    explanation: str
    consent: ConsentVersion | None = None
    session_id: str | None = None
    record_id: str | None = None
    recipient: str | None = None
    log_id: int | None = None
    # 采集获准后回填新建的数据记录
    record: DataRecord | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "operation": self.operation.value,
            "account_id": self.account_id,
            "purpose": self.purpose,
            "at": self.at.isoformat(),
            "reason_code": self.reason_code.value,
            "explanation": self.explanation,
            "consent_id": self.consent.consent_id if self.consent else None,
            "consent_version": self.consent.version if self.consent else None,
            "session_id": self.session_id,
            "record_id": self.record_id,
            "recipient": self.recipient,
            "log_id": self.log_id,
        }


@dataclass(frozen=True)
class AccessLogEntry:
    log_id: int
    at: datetime
    operation: Operation
    account_id: str
    purpose: str
    allowed: bool
    reason_code: ReasonCode
    explanation: str
    consent_id: str | None
    session_id: str | None
    record_id: str | None
    recipient: str | None


@dataclass(frozen=True)
class JobItem:
    record_id: str
    state: ItemStatus
    detail: dict
    decided_consent_id: str | None = None


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: JobKind
    account_id: str
    status: JobStatus
    request_id: str
    request_at: datetime
    attempts: int
    duplicated: bool
    items: tuple[JobItem, ...]
    result: dict | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    scope_purpose: str | None = None
    scope_record_ids: tuple[str, ...] | None = None


class SimulatedCrash(RuntimeError):
    """测试专用：模拟进程在作业执行中途崩溃（作业停留在 running）。

    服务不捕获该异常，重启后由恢复逻辑把作业重新置为 pending。
    """
