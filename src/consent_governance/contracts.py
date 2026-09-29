"""乘员会话与同意版本的数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ConsentDecision(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class OccupantSession:
    session_id: str
    vehicle_id: str
    account_id: str
    started_at: datetime


@dataclass(frozen=True)
class ConsentVersion:
    consent_id: str
    account_id: str
    version: int
    purpose: str
    decision: ConsentDecision

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("同意版本必须为正整数")
        if not self.purpose.strip():
            raise ValueError("数据用途不能为空")
