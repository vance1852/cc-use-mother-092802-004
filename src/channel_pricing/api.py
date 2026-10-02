"""渠道价盘治理的 HTTP/JSON 边界，风格与基础服务保持一致。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.storage import Database

from .errors import GovernanceError
from .service import GovernanceService


def route(service: GovernanceService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到治理服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    path = parsed.path

    def call(func: Any, *, receipt: bool = True) -> tuple[int, dict[str, Any]]:
        result = func(actor_id=actor_id, **body)
        status = 200 if (receipt and result.get("replayed")) else 201
        return status, result

    try:
        if method == "GET" and path == "/cp/health":
            return 200, {"status": "ok", "service": "channel_pricing"}

        # 主数据
        if method == "POST" and path == "/cp/brands":
            return call(service.register_brand)
        if method == "POST" and path == "/cp/regions":
            return call(service.register_region)
        if method == "POST" and path == "/cp/stores":
            return call(service.register_store)
        if method == "POST" and path == "/cp/products":
            return call(service.register_product)

        # 版本化规则
        if method == "POST" and path == "/cp/product-versions":
            return call(service.create_product_version)
        if method == "POST" and path == "/cp/product-versions/approve":
            return call(service.approve_product_version)
        if method == "POST" and path == "/cp/floor-versions":
            return call(service.create_floor_version)
        if method == "POST" and path == "/cp/floor-versions/approve":
            return call(service.approve_floor_version)
        if method == "POST" and path == "/cp/contracts":
            return call(service.create_contract)
        if method == "POST" and path == "/cp/contracts/approve":
            return call(service.approve_contract)

        # 促销承诺与叠加
        if method == "POST" and path == "/cp/promises":
            return call(service.create_promise)
        if method == "POST" and path == "/cp/promises/approve":
            return call(service.approve_promise)
        if method == "POST" and path == "/cp/stack-rules":
            return call(service.add_stack_rule)

        # 紧急例外
        if method == "POST" and path == "/cp/emergency-exceptions":
            return call(service.request_emergency_exception)
        if method == "POST" and path == "/cp/emergency-exceptions/review":
            return call(service.review_emergency_exception)

        # 活动
        if method == "POST" and path == "/cp/campaigns":
            return call(service.submit_campaign)
        if method == "POST" and path == "/cp/campaigns/approve":
            return call(service.approve_campaign)
        if method == "GET" and path == "/cp/campaigns":
            campaign_id = query.get("id", [""])[0]
            if not campaign_id:
                raise GovernanceError("id 不能为空")
            return 200, service.get_campaign(campaign_id)

        # 订单
        if method == "POST" and path == "/cp/orders":
            return call(service.book_order)
        if method == "GET" and path == "/cp/orders":
            order_id = query.get("id", [""])[0]
            if not order_id:
                raise GovernanceError("id 不能为空")
            return 200, service.get_order(order_id)

        # 结算与追溯
        if method == "POST" and path == "/cp/settlement-events":
            return call(service.post_settlement_event)
        if method == "GET" and path == "/cp/settlement-events":
            order_id = query.get("order_id", [""])[0]
            if not order_id:
                raise GovernanceError("order_id 不能为空")
            return 200, {"items": service.list_settlement_events(order_id)}
        if method == "GET" and path == "/cp/budget":
            promise_id = query.get("promise_id", [""])[0]
            if not promise_id:
                raise GovernanceError("promise_id 不能为空")
            return 200, service.remaining_budget(promise_id)
        if method == "GET" and path == "/cp/trace-expense":
            event_id = query.get("event_id", [""])[0]
            if not event_id:
                raise GovernanceError("event_id 不能为空")
            return 200, service.trace_expense(event_id)

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except GovernanceError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为治理路由调用。"""

    service: GovernanceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动渠道价盘治理 HTTP 服务（与基础服务共用同一 SQLite 数据库）。"""

    parser = argparse.ArgumentParser(description="启动多品牌渠道价盘与促销治理服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = GovernanceService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
