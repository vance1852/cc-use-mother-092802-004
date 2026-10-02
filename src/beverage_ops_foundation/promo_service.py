"""多品牌渠道价盘与促销治理服务。

在基础服务的组织、操作者、站点、角色权限、请求幂等与哈希审计链之上，
提供按生效期管理的价盘规则、活动冲突评估、紧急例外、订单规则快照、
结算核销与财务追查能力。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .pricing import CHANNELS, MECHANIC_GROUPS, TIERS, RuleBook, evaluate_mechanics, latest_effective
from .storage import Database

LIFECYCLES = frozenset({"nurturing", "growth", "mature"})
FUND_SCOPES = frozenset({"nurturing", "any"})
SETTLEMENT_KINDS = frozenset({"writeoff", "return", "invalid"})


class PromoGovernanceService:
    """协调价盘版本、促销审批、紧急例外、订单与结算规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    # ---- 基础校验 ------------------------------------------------------

    def _ts(self, value: str, field: str) -> str:
        value = str(value).strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.isoformat().replace("+00:00", "Z")

    def _identifier(self, value: Any, field: str, limit: int = 64) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**json.loads(row["response_json"]), "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {**response, "replayed": False}

    def _stores(self, store_ids: Any, field: str = "store_ids") -> list[str]:
        if not isinstance(store_ids, list) or not store_ids:
            raise ValidationError(f"{field} 必须是非空数组")
        cleaned = [self._identifier(item, f"{field}[]") for item in store_ids]
        if len(set(cleaned)) != len(cleaned):
            raise ValidationError(f"{field} 不能重复")
        return cleaned

    def _mechanics(self, mechanics: Any) -> list[dict[str, Any]]:
        if not isinstance(mechanics, list) or not mechanics:
            raise ValidationError("mechanics 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(mechanics):
            if not isinstance(item, dict):
                raise ValidationError(f"mechanics[{index}] 必须是对象")
            group = item.get("group")
            if group not in MECHANIC_GROUPS:
                raise ValidationError(f"mechanics[{index}].group 不受支持")
            mtype = item.get("type")
            if mtype not in {"bps", "cents"}:
                raise ValidationError(f"mechanics[{index}].type 必须是 bps 或 cents")
            try:
                value = int(item.get("value"))
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"mechanics[{index}].value 必须是整数") from exc
            normalized.append({
                "mechanic_id": self._identifier(item.get("mechanic_id", f"m{index + 1}"),
                                                f"mechanics[{index}].mechanic_id"),
                "group": group, "type": mtype, "value": value,
                "fund_id": self._identifier(item["fund_id"], f"mechanics[{index}].fund_id")
                if item.get("fund_id") else None,
            })
        return normalized

    # ---- 规则快照加载 --------------------------------------------------

    def _rulebook(self, connection, *, product_id: str, brand_id: str, channel: str,
                  region: str, as_of: str) -> RuleBook:
        product = latest_effective(connection.execute(
            "SELECT * FROM product_versions WHERE product_id=? ORDER BY effective_from, version_id",
            (product_id,),
        ).fetchall(), as_of)
        if product is None:
            raise NotFoundError(f"产品 {product_id} 在 {as_of} 没有生效的产品层级版本")
        if product["brand_id"] != brand_id:
            raise ValidationError("产品不属于提交的品牌")

        floor_row = latest_effective(connection.execute(
            "SELECT * FROM floor_price_versions WHERE product_id=? AND region=? "
            "ORDER BY effective_from, version_id",
            (product_id, region),
        ).fetchall(), as_of)
        floor_rows = {region: floor_row} if floor_row else {}

        contract = latest_effective(connection.execute(
            "SELECT * FROM channel_contract_versions WHERE brand_id=? AND channel=? AND region=? "
            "ORDER BY effective_from, version_id",
            (brand_id, channel, region),
        ).fetchall(), as_of)

        stack_rules: dict[tuple[str, str, str], tuple[bool, str]] = {}
        for row in connection.execute(
            "SELECT * FROM stack_rule_versions WHERE channel=? ORDER BY effective_from, version_id",
            (channel,),
        ):
            if row["effective_from"] <= as_of:
                stack_rules[(channel, row["from_group"], row["to_group"])] = (
                    bool(row["allowed"]), row["version_id"])

        funds: dict[str, Any] = {}
        fund_rows = connection.execute(
            "SELECT * FROM fund_versions WHERE effective_from<=? ORDER BY effective_from, version_id",
            (as_of,),
        ).fetchall()
        for row in fund_rows:
            funds[row["fund_id"]] = row

        # 费用上限必须在 as_of（活动开始/下单时点）已生效，且其覆盖期间包含该时点；
        # 同一费用存在多版时取生效时间最新者。
        caps = [row for row in connection.execute(
            "SELECT * FROM expense_cap_versions WHERE product_id=? AND channel=? "
            "AND effective_from<=? AND period_start<=? AND period_end>=? "
            "ORDER BY effective_from DESC, version_id DESC",
            (product_id, channel, as_of, as_of, as_of),
        ).fetchall()]

        return RuleBook(as_of=as_of, product=product, floor_rows=floor_rows,
                        contracts={channel: contract} if contract else {},
                        stack_rules=stack_rules, funds=funds, caps=caps)

    # ---- 价盘规则发布（全部按生效期追加版本） ---------------------------

    def publish_product_hierarchy(self, *, request_id: str, actor_id: str, product_id: str,
                                  brand_id: str, name: str, tier: str, lifecycle: str,
                                  list_price_cents: int, effective_from: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            product_id = self._identifier(product_id, "product_id")
            brand_id = self._identifier(brand_id, "brand_id")
            name = self._identifier(name, "name")
            if tier not in TIERS:
                raise ValidationError("tier 必须是 premium/sub_premium/mass")
            if lifecycle not in LIFECYCLES:
                raise ValidationError("lifecycle 必须是 nurturing/growth/mature")
            list_price_cents = int(list_price_cents)
            if list_price_cents <= 0:
                raise ValidationError("list_price_cents 必须为正")
            effective_from = self._ts(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._reject_duplicate_effective(
                    connection, "product_versions",
                    {"product_id": product_id, "effective_from": effective_from})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO product_versions(version_id,product_id,brand_id,name,tier,lifecycle,"
                    "list_price_cents,effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (version_id, product_id, brand_id, name, tier, lifecycle,
                     list_price_cents, effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="product_hierarchy.published",
                             resource_type="product_version", resource_id=version_id,
                             detail={"product_id": product_id, "brand_id": brand_id, "tier": tier,
                                     "lifecycle": lifecycle, "effective_from": effective_from},
                             occurred_at=self._now())
                return "product_version", version_id, {"version_id": version_id,
                                                       "product_id": product_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_product_hierarchy", payload=payload, create=create)

    def publish_floor_price(self, *, request_id: str, actor_id: str, product_id: str,
                            region: str, floor_cents: int, effective_from: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            product_id = self._identifier(product_id, "product_id")
            region = self._identifier(region, "region")
            floor_cents = int(floor_cents)
            if floor_cents < 0:
                raise ValidationError("floor_cents 不能为负")
            effective_from = self._ts(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._reject_duplicate_effective(
                    connection, "floor_price_versions",
                    {"product_id": product_id, "region": region,
                     "effective_from": effective_from})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO floor_price_versions(version_id,product_id,region,floor_cents,"
                    "effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, product_id, region, floor_cents,
                     effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="floor_price.published",
                             resource_type="floor_price_version", resource_id=version_id,
                             detail={"product_id": product_id, "region": region,
                                     "floor_cents": floor_cents, "effective_from": effective_from},
                             occurred_at=self._now())
                return "floor_price_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_floor_price", payload=payload, create=create)

    def publish_channel_contract(self, *, request_id: str, actor_id: str, brand_id: str,
                                 channel: str, region: str, discount_bps: int,
                                 effective_from: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            brand_id = self._identifier(brand_id, "brand_id")
            region = self._identifier(region, "region")
            if channel not in CHANNELS:
                raise ValidationError("channel 必须是 hypermarket/restaurant/instant_retail")
            discount_bps = int(discount_bps)
            if not 0 <= discount_bps <= 9999:
                raise ValidationError("discount_bps 必须在 0..9999 之间")
            effective_from = self._ts(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._reject_duplicate_effective(
                    connection, "channel_contract_versions",
                    {"brand_id": brand_id, "channel": channel, "region": region,
                     "effective_from": effective_from})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO channel_contract_versions(version_id,brand_id,channel,region,"
                    "discount_bps,effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, brand_id, channel, region, discount_bps,
                     effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="channel_contract.published",
                             resource_type="channel_contract_version", resource_id=version_id,
                             detail={"brand_id": brand_id, "channel": channel, "region": region,
                                     "discount_bps": discount_bps,
                                     "effective_from": effective_from},
                             occurred_at=self._now())
                return "channel_contract_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_channel_contract", payload=payload,
                                    create=create)

    def publish_stack_rule(self, *, request_id: str, actor_id: str, channel: str,
                           from_group: str, to_group: str, allowed: bool,
                           effective_from: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if channel not in CHANNELS:
                raise ValidationError("channel 必须是 hypermarket/restaurant/instant_retail")
            groups = {from_group, to_group}
            if not groups <= MECHANIC_GROUPS:
                raise ValidationError("叠加规则的手法分组不受支持")
            group_a, group_b = sorted((from_group, to_group))
            allowed = 1 if allowed else 0
            effective_from = self._ts(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._reject_duplicate_effective(
                    connection, "stack_rule_versions",
                    {"channel": channel, "from_group": group_a, "to_group": group_b,
                     "effective_from": effective_from})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stack_rule_versions(version_id,channel,from_group,to_group,allowed,"
                    "effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, channel, group_a, group_b, allowed,
                     effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="stack_rule.published",
                             resource_type="stack_rule_version", resource_id=version_id,
                             detail={"channel": channel, "from_group": group_a,
                                     "to_group": group_b, "allowed": bool(allowed),
                                     "effective_from": effective_from},
                             occurred_at=self._now())
                return "stack_rule_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_stack_rule", payload=payload, create=create)

    def publish_fund(self, *, request_id: str, actor_id: str, fund_id: str, name: str,
                     lifecycle_scope: str, effective_from: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            fund_id = self._identifier(fund_id, "fund_id")
            name = self._identifier(name, "name")
            if lifecycle_scope not in FUND_SCOPES:
                raise ValidationError("lifecycle_scope 必须是 nurturing 或 any")
            effective_from = self._ts(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._reject_duplicate_effective(
                    connection, "fund_versions",
                    {"fund_id": fund_id, "effective_from": effective_from})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO fund_versions(version_id,fund_id,name,lifecycle_scope,"
                    "effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, fund_id, name, lifecycle_scope,
                     effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="fund.published",
                             resource_type="fund_version", resource_id=version_id,
                             detail={"fund_id": fund_id, "lifecycle_scope": lifecycle_scope,
                                     "effective_from": effective_from},
                             occurred_at=self._now())
                return "fund_version", version_id, {"version_id": version_id, "fund_id": fund_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_fund", payload=payload, create=create)

    def publish_expense_cap(self, *, request_id: str, actor_id: str, fund_id: str,
                            product_id: str, channel: str, period_start: str, period_end: str,
                            amount_cents: int, effective_from: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            fund_id = self._identifier(fund_id, "fund_id")
            product_id = self._identifier(product_id, "product_id")
            if channel not in CHANNELS:
                raise ValidationError("channel 必须是 hypermarket/restaurant/instant_retail")
            period_start = self._ts(period_start, "period_start")
            period_end = self._ts(period_end, "period_end")
            if period_start > period_end:
                raise ValidationError("费用期间开始不能晚于结束")
            amount_cents = int(amount_cents)
            if amount_cents <= 0:
                raise ValidationError("amount_cents 必须为正")
            effective_from = self._ts(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._reject_duplicate_effective(
                    connection, "expense_cap_versions",
                    {"fund_id": fund_id, "product_id": product_id, "channel": channel,
                     "period_start": period_start, "period_end": period_end,
                     "effective_from": effective_from})
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO expense_cap_versions(version_id,fund_id,product_id,channel,"
                    "period_start,period_end,amount_cents,effective_from,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (version_id, fund_id, product_id, channel, period_start, period_end,
                     amount_cents, effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="expense_cap.published",
                             resource_type="expense_cap_version", resource_id=version_id,
                             detail={"fund_id": fund_id, "product_id": product_id,
                                     "channel": channel, "amount_cents": amount_cents,
                                     "period_start": period_start, "period_end": period_end,
                                     "effective_from": effective_from},
                             occurred_at=self._now())
                return "expense_cap_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_expense_cap", payload=payload, create=create)

    def _reject_duplicate_effective(self, connection, table: str, where: dict[str, Any]) -> None:
        clauses = " AND ".join(f"{column}=?" for column in where)
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE {clauses}", tuple(where.values())
        ).fetchone()
        if row:
            raise ConflictError("同一业务键在同一生效时点已经存在版本；调价请使用新的生效期")

    # ---- 活动提交与审批 ------------------------------------------------

    def submit_promotion(self, *, request_id: str, actor_id: str, promotion_id: str,
                         product_id: str, brand_id: str, channel: str, region: str,
                         store_ids: list[str], period_start: str, period_end: str,
                         planned_units: int, mechanics: list[dict[str, Any]]) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            promotion_id = self._identifier(promotion_id, "promotion_id")
            product_id = self._identifier(product_id, "product_id")
            brand_id = self._identifier(brand_id, "brand_id")
            if channel not in CHANNELS:
                raise ValidationError("channel 必须是 hypermarket/restaurant/instant_retail")
            region = self._identifier(region, "region")
            store_ids = self._stores(store_ids)
            period_start = self._ts(period_start, "period_start")
            period_end = self._ts(period_end, "period_end")
            if period_start >= period_end:
                raise ValidationError("活动开始时间必须早于结束时间")
            planned_units = int(planned_units)
            if planned_units <= 0:
                raise ValidationError("planned_units 必须为正")
            mechanics = self._mechanics(mechanics)

            rulebook = self._rulebook(connection, product_id=product_id, brand_id=brand_id,
                                      channel=channel, region=region, as_of=period_start)
            evaluation = evaluate_mechanics(rulebook, channel=channel, region=region,
                                            mechanics=mechanics, as_of=period_start)
            status = "blocked" if evaluation["conflicts"] else "pending_approval"

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM promotions WHERE promotion_id=?",
                                      (promotion_id,)).fetchone() is None:
                    connection.execute(
                        "INSERT INTO promotions(promotion_id,brand_id,created_by,created_at) "
                        "VALUES(?,?,?,?)",
                        (promotion_id, brand_id, actor_id, self._now()),
                    )
                version_no = connection.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 AS next FROM promotion_versions "
                    "WHERE promotion_id=?",
                    (promotion_id,),
                ).fetchone()["next"]
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO promotion_versions(version_id,promotion_id,version_no,status,"
                    "product_id,channel,region,store_ids_json,period_start,period_end,planned_units,"
                    "mechanics_json,evaluation_json,snapshot_json,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, promotion_id, version_no, status, product_id, channel, region,
                     canonical_json(store_ids), period_start, period_end, planned_units,
                     canonical_json(mechanics), canonical_json(evaluation), None,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="promotion.submitted",
                             resource_type="promotion_version", resource_id=version_id,
                             detail={"promotion_id": promotion_id, "version_no": version_no,
                                     "status": status,
                                     "conflict_count": len(evaluation["conflicts"])},
                             occurred_at=self._now())
                return "promotion_version", version_id, {
                    "promotion_id": promotion_id, "version_id": version_id,
                    "version_no": version_no, "status": status, "evaluation": evaluation,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_promotion", payload=payload, create=create)

    def _get_promotion_version(self, connection, version_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM promotion_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("促销版本不存在")
        return row

    def _freeze_snapshot(self, connection, version: Any) -> dict[str, Any]:
        """按活动期开始时点冻结批准所依据的全部规则版本。"""

        mechanics = json.loads(version["mechanics_json"])
        promotion = connection.execute(
            "SELECT * FROM promotions WHERE promotion_id=?", (version["promotion_id"],)
        ).fetchone()
        rulebook = self._rulebook(
            connection, product_id=version["product_id"], brand_id=promotion["brand_id"],
            channel=version["channel"], region=version["region"], as_of=version["period_start"])
        evaluation = evaluate_mechanics(
            rulebook, channel=version["channel"], region=version["region"],
            mechanics=mechanics, as_of=version["period_start"])
        return {
            "frozen_at": self._now(), "as_of": version["period_start"],
            "rule_version_ids": evaluation["rule_version_ids"],
            "evaluation": evaluation,
        }

    def approve_promotion(self, *, request_id: str, actor_id: str,
                          promotion_version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "promotion_version_id": promotion_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version = self._get_promotion_version(connection, promotion_version_id)
            if version["status"] != "pending_approval":
                raise ConflictError(f"版本当前状态为 {version['status']}，不能批准")
            if version["submitted_by"] == actor_id:
                raise PermissionDenied("提交人与复核人不能是同一人")

            def create() -> tuple[str, str, dict[str, Any]]:
                snapshot = self._freeze_snapshot(connection, version)
                connection.execute(
                    "UPDATE promotion_versions SET status='approved',snapshot_json=?,"
                    "approved_by=?,approved_at=? WHERE version_id=?",
                    (canonical_json(snapshot), actor_id, self._now(), promotion_version_id),
                )
                append_event(connection, actor_id=actor_id, action="promotion.approved",
                             resource_type="promotion_version", resource_id=promotion_version_id,
                             detail={"promotion_id": version["promotion_id"],
                                     "version_no": version["version_no"],
                                     "rule_version_ids": snapshot["rule_version_ids"]},
                             occurred_at=self._now())
                return "promotion_version", promotion_version_id, {
                    "version_id": promotion_version_id, "status": "approved",
                    "snapshot": snapshot,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_promotion", payload=payload, create=create)

    def reject_promotion(self, *, request_id: str, actor_id: str,
                         promotion_version_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "promotion_version_id": promotion_version_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version = self._get_promotion_version(connection, promotion_version_id)
            if version["status"] != "pending_approval":
                raise ConflictError(f"版本当前状态为 {version['status']}，不能驳回")
            reason = self._identifier(reason, "reason", )

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE promotion_versions SET status='rejected',approved_by=?,approved_at=? "
                    "WHERE version_id=?",
                    (actor_id, self._now(), promotion_version_id),
                )
                append_event(connection, actor_id=actor_id, action="promotion.rejected",
                             resource_type="promotion_version", resource_id=promotion_version_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "promotion_version", promotion_version_id, {
                    "version_id": promotion_version_id, "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_promotion", payload=payload, create=create)

    # ---- 紧急例外 ------------------------------------------------------

    def request_emergency_exception(self, *, request_id: str, actor_id: str,
                                    promotion_version_id: str, store_ids: list[str],
                                    quantity_limit: int, deadline: str,
                                    reason: str) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = self._get_promotion_version(connection, promotion_version_id)
            if version["status"] != "blocked":
                raise ConflictError("只有被价盘规则拦截的活动版本才能申请紧急例外")
            store_ids = self._stores(store_ids)
            allowed_stores = set(json.loads(version["store_ids_json"]))
            if not set(store_ids) <= allowed_stores:
                raise ValidationError("紧急例外门店必须是活动登记门店的子集")
            quantity_limit = int(quantity_limit)
            if quantity_limit <= 0 or quantity_limit > version["planned_units"]:
                raise ValidationError("例外数量必须为正且不超过活动计划数量")
            deadline = self._ts(deadline, "deadline")
            now = self._now()
            if deadline <= now or deadline > version["period_end"]:
                raise ValidationError("例外期限必须在未来且不晚于活动结束时间")
            reason = self._identifier(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                exception_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO emergency_exceptions(exception_id,promotion_version_id,store_ids_json,"
                    "quantity_limit,deadline,reason,status,requested_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (exception_id, promotion_version_id, canonical_json(store_ids),
                     quantity_limit, deadline, reason, "pending", actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="emergency_exception.requested",
                             resource_type="emergency_exception", resource_id=exception_id,
                             detail={"promotion_version_id": promotion_version_id,
                                     "stores": store_ids, "quantity_limit": quantity_limit,
                                     "deadline": deadline},
                             occurred_at=self._now())
                return "emergency_exception", exception_id, {
                    "exception_id": exception_id, "status": "pending",
                    "promotion_version_id": promotion_version_id,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="request_emergency_exception",
                                    payload=payload, create=create)

    def review_emergency_exception(self, *, request_id: str, actor_id: str,
                                   exception_id: str, approved: bool) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "exception_id": exception_id, "approved": approved}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            row = connection.execute(
                "SELECT * FROM emergency_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("紧急例外不存在")
            if row["status"] != "pending":
                raise ConflictError("紧急例外已经复核")
            if row["requested_by"] == actor_id:
                raise PermissionDenied("紧急例外必须由不同角色复核")

            def create() -> tuple[str, str, dict[str, Any]]:
                new_status = "approved" if approved else "rejected"
                connection.execute(
                    "UPDATE emergency_exceptions SET status=?,reviewed_by=?,reviewed_at=? "
                    "WHERE exception_id=?",
                    (new_status, actor_id, self._now(), exception_id),
                )
                result = {"exception_id": exception_id, "status": new_status}
                if approved:
                    version = self._get_promotion_version(
                        connection, row["promotion_version_id"])
                    snapshot = self._freeze_snapshot(connection, version)
                    connection.execute(
                        "UPDATE promotion_versions SET status='approved',snapshot_json=?,"
                        "approved_by=?,approved_at=?,exception_id=? WHERE version_id=?",
                        (canonical_json(snapshot), actor_id, self._now(), exception_id,
                         row["promotion_version_id"]),
                    )
                    result["promotion_version_id"] = row["promotion_version_id"]
                    result["snapshot"] = snapshot
                append_event(connection, actor_id=actor_id,
                             action="emergency_exception.reviewed",
                             resource_type="emergency_exception", resource_id=exception_id,
                             detail={"approved": bool(approved),
                                     "promotion_version_id": row["promotion_version_id"]},
                             occurred_at=self._now())
                return "emergency_exception", exception_id, result

            return self._idempotent(connection, request_id=request_id,
                                    action="review_emergency_exception",
                                    payload=payload, create=create)

    # ---- 订单（引用下单当时规则，永不重写） ------------------------------

    def create_order(self, *, request_id: str, actor_id: str, order_id: str,
                     promotion_version_id: str, store_id: str, quantity: int,
                     as_of: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "order_id": order_id,
                   "promotion_version_id": promotion_version_id, "store_id": store_id,
                   "quantity": quantity, "as_of": as_of}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            order_id = self._identifier(order_id, "order_id")
            version = self._get_promotion_version(connection, promotion_version_id)
            if version["status"] != "approved":
                raise ConflictError("促销版本未批准，不能下单")
            store_id = self._identifier(store_id, "store_id")
            store_ids = set(json.loads(version["store_ids_json"]))
            if store_id not in store_ids:
                raise ValidationError("门店不在活动范围内")
            quantity = int(quantity)
            if quantity <= 0:
                raise ValidationError("quantity 必须为正")
            order_time = self._ts(as_of, "as_of") if as_of else self._now()
            if not version["period_start"] <= order_time <= version["period_end"]:
                raise ValidationError("下单时间不在活动有效期内")

            exception = None
            if version["exception_id"]:
                exception = connection.execute(
                    "SELECT * FROM emergency_exceptions WHERE exception_id=?",
                    (version["exception_id"],),
                ).fetchone()
                if store_id not in set(json.loads(exception["store_ids_json"])):
                    raise PermissionDenied("紧急例外仅限指定门店")
                if order_time > exception["deadline"]:
                    raise PermissionDenied("紧急例外已超过期限")
                used = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS used FROM promo_orders "
                    "WHERE promotion_version_id=?",
                    (promotion_version_id,),
                ).fetchone()["used"]
                if used + quantity > exception["quantity_limit"]:
                    raise ConflictError("紧急例外数量上限不足")

            promotion = connection.execute(
                "SELECT * FROM promotions WHERE promotion_id=?", (version["promotion_id"],)
            ).fetchone()
            # 关键：按“下单当时”的生效规则重新解析，版本表只追加，历史永不被改写。
            rulebook = self._rulebook(
                connection, product_id=version["product_id"], brand_id=promotion["brand_id"],
                channel=version["channel"], region=version["region"], as_of=order_time)
            mechanics = json.loads(version["mechanics_json"])
            evaluation = evaluate_mechanics(
                rulebook, channel=version["channel"], region=version["region"],
                mechanics=mechanics, as_of=order_time)
            if evaluation["unit_net_cents"] < 0:
                raise ConflictError("下单当时优惠后净价为负，拒绝成交")
            # 后续调价抬高了底价/收紧了规则：新订单按下单当时规则拦截，
            # 但已生成的历史订单不受影响（只追加、不回写）。
            if exception is None:
                hard_codes = {"price_below_floor", "floor_missing"}
                hard = [c for c in evaluation["conflicts"] if c["code"] in hard_codes]
                if hard:
                    raise ConflictError(f"下单当时价盘规则已变化：{hard[0]['message']}")
            unit_net = evaluation["unit_net_cents"]
            total_net = unit_net * quantity
            snapshot = {
                "as_of": order_time,
                "promotion_version_id": promotion_version_id,
                "approved_snapshot_version_ids": json.loads(version["snapshot_json"])["rule_version_ids"]
                if version["snapshot_json"] else None,
                "order_time_rule_version_ids": evaluation["rule_version_ids"],
                "evaluation": evaluation,
                "exception_id": version["exception_id"],
            }

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO promo_orders(order_id,promotion_version_id,store_id,quantity,"
                        "unit_list_cents,unit_net_cents,total_net_cents,rule_snapshot_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (order_id, promotion_version_id, store_id, quantity,
                         evaluation["list_price_cents"], unit_net, total_net,
                         canonical_json(snapshot), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("订单编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="order.created",
                             resource_type="order", resource_id=order_id,
                             detail={"promotion_version_id": promotion_version_id,
                                     "unit_net_cents": unit_net, "quantity": quantity,
                                     "rule_version_ids": evaluation["rule_version_ids"]},
                             occurred_at=self._now())
                return "order", order_id, {
                    "order_id": order_id, "unit_net_cents": unit_net,
                    "total_net_cents": total_net, "snapshot": snapshot,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="create_order", payload=payload, create=create)

    # ---- 结算：核销 / 退货 / 无效凭证，全部归回原促销承诺 ----------------

    def _resolve_cap(self, connection, *, version: Any, order: Any, fund_id: str) -> Any:
        order_snapshot = json.loads(order["rule_snapshot_json"])
        as_of = order_snapshot["as_of"]
        rows = connection.execute(
            "SELECT * FROM expense_cap_versions WHERE fund_id=? AND product_id=? AND channel=? "
            "AND effective_from<=? AND period_start<=? AND period_end>=? "
            "ORDER BY effective_from DESC, version_id DESC",
            (fund_id, version["product_id"], version["channel"], as_of,
             as_of, as_of),
        ).fetchall()
        if not rows:
            raise ConflictError("下单当时没有生效且覆盖该期间的费用上限")
        return rows[0]

    def submit_settlement(self, *, request_id: str, actor_id: str, promotion_version_id: str,
                          external_voucher_id: str, kind: str, amount_cents: int,
                          quantity: int, order_id: str | None = None, fund_id: str | None = None,
                          original_voucher_id: str | None = None) -> dict[str, Any]:
        payload = locals_wo(locals(), "self", "request_id", "actor_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = self._get_promotion_version(connection, promotion_version_id)
            if version["status"] != "approved":
                raise ConflictError("促销版本未批准，不能结算")
            external_voucher_id = self._identifier(external_voucher_id, "external_voucher_id")

            # 同一促销承诺下凭证号唯一；重复回传直接回放原结果，保证幂等。
            existing = connection.execute(
                "SELECT * FROM settlement_entries WHERE promotion_version_id=? "
                "AND external_voucher_id=?",
                (promotion_version_id, external_voucher_id),
            ).fetchone()
            if existing is not None:
                replay_remaining = self._cap_remaining(connection, existing["cap_version_id"])
                return {"entry_id": existing["entry_id"], "replayed": True,
                        "kind": existing["kind"], "amount_cents": existing["amount_cents"],
                        "cap_version_id": existing["cap_version_id"],
                        "remaining_cents": replay_remaining}

            if kind not in SETTLEMENT_KINDS:
                raise ValidationError("kind 必须是 writeoff/return/invalid")
            amount_cents = int(amount_cents)
            quantity = int(quantity)
            if amount_cents < 0 or quantity < 0:
                raise ValidationError("金额与数量不能为负")

            order = None
            cap = None
            cap_version_id = None
            detail: dict[str, Any] = {}

            if kind == "writeoff":
                if not order_id or not fund_id:
                    raise ValidationError("核销必须提供 order_id 与 fund_id")
                order = self._get_order(connection, order_id)
                if order["promotion_version_id"] != promotion_version_id:
                    raise ValidationError("订单不属于该促销承诺")
                fund_ids = {m.get("fund_id") for m in json.loads(version["mechanics_json"])}
                if fund_id not in fund_ids:
                    raise ValidationError("费用方案不在促销承诺内")
                cap = self._resolve_cap(connection, version=version, order=order, fund_id=fund_id)
                cap_version_id = cap["version_id"]
                detail = {"order_id": order_id, "fund_id": fund_id}
            else:
                if not original_voucher_id:
                    raise ValidationError("退货或无效凭证必须提供原核销凭证号")
                original = connection.execute(
                    "SELECT * FROM settlement_entries WHERE promotion_version_id=? "
                    "AND external_voucher_id=?",
                    (promotion_version_id, original_voucher_id),
                ).fetchone()
                if original is None or original["kind"] != "writeoff":
                    raise NotFoundError("原核销凭证不存在")
                already_reversed = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS amount FROM settlement_entries "
                    "WHERE promotion_version_id=? AND original_voucher_id=? "
                    "AND kind IN ('return','invalid')",
                    (promotion_version_id, original_voucher_id),
                ).fetchone()["amount"]
                if already_reversed + amount_cents > original["amount_cents"]:
                    raise ConflictError("冲回金额超过原核销金额")
                order_id = original["order_id"]
                fund_id = original["fund_id"]
                cap_version_id = original["cap_version_id"]
                detail = {"order_id": order_id, "fund_id": fund_id,
                          "original_voucher_id": original_voucher_id}

            if cap_version_id:
                used = self._cap_used(connection, cap_version_id)
                cap_amount = cap["amount_cents"] if cap is not None else connection.execute(
                    "SELECT amount_cents FROM expense_cap_versions WHERE version_id=?",
                    (cap_version_id,),
                ).fetchone()["amount_cents"]
                if kind == "writeoff" and used + amount_cents > cap_amount:
                    raise ConflictError(
                        f"费用上限不足：已用 {used} 分，本笔 {amount_cents} 分，上限 {cap_amount} 分")
                remaining = cap_amount - used - (amount_cents if kind == "writeoff" else -amount_cents)
            else:
                remaining = None

            def create() -> tuple[str, str, dict[str, Any]]:
                entry_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO settlement_entries(entry_id,promotion_version_id,order_id,fund_id,"
                        "cap_version_id,original_voucher_id,kind,external_voucher_id,amount_cents,"
                        "quantity,detail_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, promotion_version_id, order_id, fund_id, cap_version_id,
                         original_voucher_id, kind, external_voucher_id, amount_cents, quantity,
                         canonical_json(detail), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("外部凭证号在该促销承诺下已经回传") from exc
                append_event(connection, actor_id=actor_id, action=f"settlement.{kind}",
                             resource_type="settlement_entry", resource_id=entry_id,
                             detail={"promotion_version_id": promotion_version_id,
                                     "order_id": order_id, "fund_id": fund_id,
                                     "cap_version_id": cap_version_id,
                                     "external_voucher_id": external_voucher_id,
                                     "amount_cents": amount_cents,
                                     "remaining_cents": remaining},
                             occurred_at=self._now())
                return "settlement_entry", entry_id, {
                    "entry_id": entry_id, "kind": kind, "amount_cents": amount_cents,
                    "cap_version_id": cap_version_id, "remaining_cents": remaining,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_settlement", payload=payload, create=create)

    def _cap_used(self, connection, cap_version_id: str | None) -> int:
        if not cap_version_id:
            return 0
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='writeoff' THEN amount_cents ELSE 0 END),0) "
            "- COALESCE(SUM(CASE WHEN kind IN ('return','invalid') THEN amount_cents ELSE 0 END),0) "
            "AS used FROM settlement_entries WHERE cap_version_id=?",
            (cap_version_id,),
        ).fetchone()
        return row["used"]

    def _cap_remaining(self, connection, cap_version_id: str | None) -> int | None:
        if not cap_version_id:
            return None
        cap_row = connection.execute(
            "SELECT amount_cents FROM expense_cap_versions WHERE version_id=?",
            (cap_version_id,),
        ).fetchone()
        if cap_row is None:
            return None
        return cap_row["amount_cents"] - self._cap_used(connection, cap_version_id)

    def _get_order(self, connection, order_id: str) -> Any:
        row = connection.execute("SELECT * FROM promo_orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("订单不存在")
        return row

    # ---- 财务追查 ------------------------------------------------------

    def trace_expense(self, *, external_voucher_id: str,
                      promotion_version_id: str | None = None) -> dict[str, Any]:
        """从一笔费用追到：批准版本 → 订单 → 费用上限版本与剩余额度。"""

        connection = self.database.connection
        if promotion_version_id:
            entry = connection.execute(
                "SELECT * FROM settlement_entries WHERE external_voucher_id=? "
                "AND promotion_version_id=?",
                (external_voucher_id, promotion_version_id),
            ).fetchone()
        else:
            entry = connection.execute(
                "SELECT * FROM settlement_entries WHERE external_voucher_id=? ORDER BY created_at",
                (external_voucher_id,),
            ).fetchone()
        if entry is None:
            raise NotFoundError("结算凭证不存在")

        version = connection.execute(
            "SELECT pv.*, p.brand_id FROM promotion_versions pv JOIN promotions p "
            "ON pv.promotion_id=p.promotion_id WHERE pv.version_id=?",
            (entry["promotion_version_id"],),
        ).fetchone()
        snapshot = json.loads(version["snapshot_json"]) if version["snapshot_json"] else None
        order = None
        if entry["order_id"]:
            order_row = self._get_order(connection, entry["order_id"])
            order = {
                "order_id": order_row["order_id"], "store_id": order_row["store_id"],
                "quantity": order_row["quantity"], "unit_net_cents": order_row["unit_net_cents"],
                "total_net_cents": order_row["total_net_cents"],
                "rule_snapshot": json.loads(order_row["rule_snapshot_json"]),
            }
        cap = None
        if entry["cap_version_id"]:
            cap_row = connection.execute(
                "SELECT * FROM expense_cap_versions WHERE version_id=?",
                (entry["cap_version_id"],),
            ).fetchone()
            totals = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN kind='writeoff' THEN amount_cents ELSE 0 END),0) "
                "AS writeoff, COALESCE(SUM(CASE WHEN kind='return' THEN amount_cents ELSE 0 END),0) "
                "AS returned, COALESCE(SUM(CASE WHEN kind='invalid' THEN amount_cents ELSE 0 END),0) "
                "AS invalid FROM settlement_entries WHERE cap_version_id=?",
                (entry["cap_version_id"],),
            ).fetchone()
            used = totals["writeoff"] - totals["returned"] - totals["invalid"]
            cap = {
                "cap_version_id": cap_row["version_id"], "fund_id": cap_row["fund_id"],
                "product_id": cap_row["product_id"], "channel": cap_row["channel"],
                "period_start": cap_row["period_start"], "period_end": cap_row["period_end"],
                "amount_cents": cap_row["amount_cents"],
                "writeoff_cents": totals["writeoff"], "returned_cents": totals["returned"],
                "invalid_cents": totals["invalid"], "used_cents": used,
                "remaining_cents": cap_row["amount_cents"] - used,
            }
        exception = None
        if version["exception_id"]:
            ex = connection.execute(
                "SELECT * FROM emergency_exceptions WHERE exception_id=?",
                (version["exception_id"],),
            ).fetchone()
            exception = {
                "exception_id": ex["exception_id"], "status": ex["status"],
                "store_ids": json.loads(ex["store_ids_json"]),
                "quantity_limit": ex["quantity_limit"], "deadline": ex["deadline"],
                "requested_by": ex["requested_by"], "reviewed_by": ex["reviewed_by"],
            }
        return {
            "entry": {
                "entry_id": entry["entry_id"], "kind": entry["kind"],
                "external_voucher_id": entry["external_voucher_id"],
                "amount_cents": entry["amount_cents"], "quantity": entry["quantity"],
                "fund_id": entry["fund_id"], "original_voucher_id": entry["original_voucher_id"],
                "created_by": entry["created_by"], "created_at": entry["created_at"],
            },
            "promotion_version": {
                "version_id": version["version_id"],
                "promotion_id": version["promotion_id"],
                "version_no": version["version_no"], "status": version["status"],
                "brand_id": version["brand_id"], "product_id": version["product_id"],
                "channel": version["channel"], "region": version["region"],
                "period_start": version["period_start"], "period_end": version["period_end"],
                "submitted_by": version["submitted_by"], "submitted_at": version["submitted_at"],
                "approved_by": version["approved_by"], "approved_at": version["approved_at"],
                "frozen_rule_version_ids": snapshot["rule_version_ids"] if snapshot else None,
            },
            "exception": exception,
            "order": order,
            "cap": cap,
        }

    def get_promotion_version(self, promotion_version_id: str) -> dict[str, Any]:
        """读取促销版本及其完整评估结果。"""

        row = self.database.connection.execute(
            "SELECT * FROM promotion_versions WHERE version_id=?", (promotion_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("促销版本不存在")
        return {
            "version_id": row["version_id"], "promotion_id": row["promotion_id"],
            "version_no": row["version_no"], "status": row["status"],
            "product_id": row["product_id"], "channel": row["channel"],
            "region": row["region"], "store_ids": json.loads(row["store_ids_json"]),
            "period_start": row["period_start"], "period_end": row["period_end"],
            "planned_units": row["planned_units"],
            "mechanics": json.loads(row["mechanics_json"]),
            "evaluation": json.loads(row["evaluation_json"]),
            "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] else None,
            "submitted_by": row["submitted_by"], "approved_by": row["approved_by"],
            "exception_id": row["exception_id"],
        }


def locals_wo(local_values: dict[str, Any], *names: str) -> dict[str, Any]:
    """构造幂等载荷时剔除上下文中的辅助参数。"""

    return {key: value for key, value in local_values.items() if key not in names}
