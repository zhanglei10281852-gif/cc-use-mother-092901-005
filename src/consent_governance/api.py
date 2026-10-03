"""基于标准库 http.server 的 REST 接口。

无需第三方依赖。每个判断结果都带 reason_code / explanation，
调用方可据此向用户解释"为什么获准或拒绝"。

主要路由：

* ``POST /vehicles`` ``POST /accounts`` ``POST /purposes`` ``POST /sessions``
* ``POST /accounts/<id>/consents/<purpose>``  body: {"decision": "granted|denied|withdrawn"}
* ``POST /holds``                              有期限合法保留
* ``POST /access/evaluate``                    访问判断（必留痕）
* ``POST /collect``                            采集（获准后落记录）
* ``POST /exports`` / ``POST /deletions``      幂等请求（body 需带 request_id）
* ``GET  /jobs/<id>`` / ``POST /jobs/<id>/run``
* ``POST /run-due``                            到期恢复 + 重试
* ``GET  /records/<id>/lineage``               原始记录与派生物追踪
* ``GET  /access-logs``                        历史访问留痕查询
"""

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .contracts import (
    AccessDecision,
    ConsentDecision,
    Job,
    JobKind,
    Operation,
)
from .service import GovernanceError, GovernanceService


def _parse_at(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _job_dict(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "kind": job.kind.value,
        "account_id": job.account_id,
        "status": job.status.value,
        "request_id": job.request_id,
        "request_at": job.request_at.isoformat(),
        "attempts": job.attempts,
        "duplicated": job.duplicated,
        "result": job.result,
        "last_error": job.last_error,
        "scope_purpose": job.scope_purpose,
        "scope_record_ids": list(job.scope_record_ids or ()),
        "items": [
            {
                "record_id": it.record_id,
                "state": it.state.value,
                "detail": _jsonable(it.detail),
                "decided_consent_id": it.decided_consent_id,
            }
            for it in job.items
        ],
    }


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _decision_dict(d: AccessDecision) -> dict:
    payload = d.to_dict()
    if d.record is not None:
        payload["record"] = {
            "record_id": d.record.record_id,
            "kind": d.record.kind,
            "purpose": d.record.purpose,
            "derived_from": d.record.derived_from,
            "session_id": d.record.session_id,
        }
    return payload


class GovernanceHandler(BaseHTTPRequestHandler):
    server_version = "ConsentGovernance/1.0"

    # ---- HTTP 工具 ----------------------------------------------------

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": code, "message": message})

    @property
    def svc(self) -> GovernanceService:
        return self.server.svc  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---- 路由 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            m = re.fullmatch(r"/jobs/([^/]+)", path)
            if m:
                return self._send(200, _job_dict(self.svc.get_job(m.group(1))))
            m = re.fullmatch(r"/requests/([^/]+)", path)
            if m:
                job = self.svc.duplicate_request(m.group(1))
                if job is None:
                    return self._error(404, "not_found", "未知请求号")
                return self._send(200, _job_dict(job))
            m = re.fullmatch(r"/records/([^/]+)/lineage", path)
            if m:
                return self._send(200, self.svc.lineage(m.group(1)))
            m = re.fullmatch(r"/records/([^/]+)", path)
            if m:
                rec = self.svc.get_record(m.group(1))
                return self._send(200, {
                    "record_id": rec.record_id,
                    "owner_account": rec.owner_account,
                    "purpose": rec.purpose,
                    "kind": rec.kind,
                    "session_id": rec.session_id,
                    "derived_from": rec.derived_from,
                    "locatable": rec.locatable,
                    "state": rec.state.value,
                    "created_at": rec.created_at.isoformat(),
                    "deleted_at": rec.deleted_at.isoformat() if rec.deleted_at else None,
                })
            if path == "/access-logs":
                entries = self.svc.access_history(
                    account_id=(query.get("account_id") or [None])[0],
                    record_id=(query.get("record_id") or [None])[0],
                    operation=(query.get("operation") or [None])[0],
                )
                return self._send(200, {"entries": [
                    {
                        "log_id": e.log_id, "at": e.at.isoformat(),
                        "operation": e.operation.value, "account_id": e.account_id,
                        "purpose": e.purpose, "allowed": e.allowed,
                        "reason_code": e.reason_code.value, "explanation": e.explanation,
                        "consent_id": e.consent_id, "session_id": e.session_id,
                        "record_id": e.record_id, "recipient": e.recipient,
                    }
                    for e in entries
                ]})
            if path == "/health":
                return self._send(200, {"status": "ok"})
            self._error(404, "not_found", f"未知路径: {path}")
        except GovernanceError as exc:
            self._error(404, "not_found", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal_error", repr(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            body = self._body()
            at = _parse_at(body.get("at"))

            if path == "/vehicles":
                v = self.svc.register_vehicle(body["vehicle_id"], at=at)
                return self._send(201, {"vehicle_id": v.vehicle_id})
            if path == "/accounts":
                a = self.svc.register_account(body["account_id"], at=at)
                return self._send(201, {"account_id": a.account_id})
            if path == "/purposes":
                p = self.svc.register_purpose(body["code"], body.get("description", ""))
                return self._send(201, {"code": p.code})
            if path == "/sessions":
                s = self.svc.start_session(
                    body["session_id"], body["vehicle_id"], body["account_id"], at=at
                )
                return self._send(201, {"session_id": s.session_id})

            m = re.fullmatch(r"/accounts/([^/]+)/consents/([^/]+)", path)
            if m:
                account_id, purpose = m.group(1), m.group(2)
                decision = ConsentDecision(body.get("decision", "granted"))
                if decision == ConsentDecision.GRANTED:
                    c = self.svc.grant_consent(account_id, purpose, at=at)
                elif decision == ConsentDecision.DENIED:
                    c = self.svc.deny_consent(account_id, purpose, at=at)
                else:
                    c = self.svc.withdraw_consent(account_id, purpose, at=at)
                return self._send(201, {
                    "consent_id": c.consent_id, "version": c.version,
                    "decision": c.decision.value, "recorded_at": c.recorded_at.isoformat(),
                })

            if path == "/holds":
                hold = self.svc.add_legal_hold(
                    hold_id=body["hold_id"], account_id=body["account_id"],
                    reason=body["reason"], end_at=_parse_at(body["end_at"]),
                    purpose=body.get("purpose"), start_at=_parse_at(body.get("start_at")),
                )
                return self._send(201, {
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in hold.items()
                })

            if path == "/access/evaluate":
                d = self.svc.evaluate_access(
                    operation=Operation(body["operation"]),
                    account_id=body["account_id"], purpose=body["purpose"], at=at,
                    record_id=body.get("record_id"), session_id=body.get("session_id"),
                    recipient=body.get("recipient"),
                )
                return self._send(200 if d.allowed else 403, _decision_dict(d))

            if path == "/collect":
                d = self.svc.collect(
                    account_id=body["account_id"], purpose=body["purpose"],
                    kind=body["kind"], at=at, session_id=body.get("session_id"),
                    record_id=body.get("record_id"),
                    derived_from=body.get("derived_from"),
                    locatable=body.get("locatable", True),
                )
                return self._send(201 if d.allowed else 403, _decision_dict(d))

            if path in ("/exports", "/deletions"):
                kind = JobKind.EXPORT if path == "/exports" else JobKind.DELETE
                request_id = body.get("request_id")
                if not request_id:
                    return self._error(400, "bad_request", "必须提供 request_id 以支持幂等")
                fn = self.svc.request_export if kind == JobKind.EXPORT else self.svc.request_delete
                existed = self.svc.duplicate_request(request_id)
                job = fn(
                    account_id=body["account_id"], request_id=request_id, at=at,
                    purpose=body.get("purpose"), record_ids=body.get("record_ids"),
                )
                code = 200 if (existed is not None) else 201
                payload = _job_dict(self.svc.mark_duplicate_reply(job))
                return self._send(code, payload)

            m = re.fullmatch(r"/jobs/([^/]+)/run", path)
            if m:
                job = self.svc.process_job(m.group(1), at=at)
                return self._send(200, _job_dict(job))

            if path == "/run-due":
                jobs = self.svc.run_due(at=at)
                return self._send(200, {"jobs": [_job_dict(j) for j in jobs]})

            self._error(404, "not_found", f"未知路径: {path}")
        except KeyError as exc:
            self._error(400, "bad_request", f"缺少字段: {exc.args[0]}")
        except GovernanceError as exc:
            self._error(409, "conflict", str(exc))
        except ValueError as exc:
            self._error(400, "bad_request", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal_error", repr(exc))


def create_app(store) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), GovernanceHandler)
    server.svc = GovernanceService(store)  # type: ignore[attr-defined]
    return server


def serve(store, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), GovernanceHandler)
    server.svc = GovernanceService(store)  # type: ignore[attr-defined]
    return server
