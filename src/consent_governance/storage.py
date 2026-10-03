"""JSON 文档 + 预写日志（WAL）的可重启存储。

设计目标是让作业在进程崩溃后可恢复：

* 每个变更先顺序追加到 ``events.jsonl``（fsync 后才算生效）；
* ``save()`` 时写出快照并截断日志；
* 启动时先加载快照再重放日志。

单进程使用，写操作由 GovernanceService 的锁串行化，故不需要并发控制。
"""

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return {"__dt__": obj.isoformat()}
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(f"不可序列化的对象: {type(obj)!r}")


def _revive(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__dt__"}:
            return datetime.fromisoformat(value["__dt__"])
        return {k: _revive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_revive(v) for v in value]
    return value


def dumps(value: Any) -> str:
    return json.dumps(value, default=_default, ensure_ascii=False, sort_keys=True)


class JsonStore:
    """以一个目录承载快照与 WAL。"""

    SNAPSHOT = "snapshot.json"
    WAL = "events.jsonl"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_path = self.dir / self.SNAPSHOT
        self._wal_path = self.dir / self.WAL

    # ---- 基础读写 -----------------------------------------------------

    def load_raw(self) -> dict:
        data: dict = {}
        if self._snapshot_path.exists():
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        if self._wal_path.exists():
            with self._wal_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    self._apply(data, event)
        return _revive(data)

    @staticmethod
    def _apply(data: dict, event: dict) -> None:
        kind = event["op"]
        if kind == "put":
            data.setdefault(event["table"], {})[event["key"]] = event["value"]
        elif kind == "delete":
            data.get(event["table"], {}).pop(event["key"], None)
        elif kind == "append":
            data.setdefault(event["table"], []).append(event["value"])
        else:
            raise ValueError(f"未知 WAL 事件: {kind}")

    def append_wal(self, op: str, table: str, key: str | None, value: Any) -> None:
        event = {"op": op, "table": table, "key": key, "value": value}
        with self._wal_path.open("a", encoding="utf-8") as fh:
            fh.write(dumps(event) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def save(self, data: dict) -> None:
        """原子写快照，成功后截断 WAL。"""
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, default=_default, ensure_ascii=False, indent=2, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._snapshot_path)
            # 截断日志并 fsync 目录，保证崩溃后不会重放已入快照的事件
            with self._wal_path.open("w", encoding="utf-8") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            dir_fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
