"""渠道价盘与促销治理领域服务。

在基础服务的操作者、事务与哈希审计链之上实现：

* 产品层级/生命周期、区域底价、渠道合同按生效期版本保存，生效后不可变；
* 促销承诺保存适用范围、费用类型、可叠加规则与费用上限；
* 活动提交时计算全部优惠后的实际条件，并逐条给出冲突来源；
* 紧急例外限定单一门店、数量与期限，由另一名角色复核；
* 订单固化下单时刻的规则快照，后续调价不重写历史；
* 结算把核销、退货、无效凭证归回原促销承诺，重复回传幂等；
* 财务可从一笔费用追查到批准版本、订单与剩余额度。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.clock import Clock, SystemClock
from beverage_ops_foundation.models import Actor

from .errors import (
    BudgetExceeded,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StateConflict,
    ValidationError,
)
from .rules import (
    CHANNELS,
    DISCOUNT_TYPES,
    LIFECYCLE_STAGES,
    PRODUCT_TIERS,
    cultivation_allowed,
    evaluate_offer,
    money,
)
from .storage import ensure_governance_schema

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CREATOR_ROLES = frozenset({"admin", "operator"})
APPROVER_ROLES = frozenset({"admin", "reviewer"})


class GovernanceService:
    """协调价盘规则、促销核算、紧急例外、订单快照与结算追溯。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.connection = database.connection
        ensure_governance_schema(self.connection)
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not DATE.fullmatch(value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是有效日期") from exc
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

    def _require(self, actor: Actor, roles: frozenset[str]) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM cp_request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            receipt = {"request_id": request_id, "resource_type": row["resource_type"],
                       "resource_id": row["resource_id"]}
            receipt.update(json.loads(row["response_json"]))
            receipt["replayed"] = True
            return receipt
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO cp_request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        receipt = {"request_id": request_id, "resource_type": resource_type,
                   "resource_id": resource_id}
        receipt.update(response)
        receipt["replayed"] = False
        return receipt

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    def tx(self):
        return self.database.transaction(immediate=True)

    # ----------------------------------------------------------- 主数据登记

    def register_brand(self, *, request_id: str, actor_id: str,
                       brand_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "brand_id": brand_id, "name": name}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, CREATOR_ROLES)
            brand_id = self._id(brand_id, "brand_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO cp_brands(brand_id,organization_id,name,created_at) VALUES(?,?,?,?)",
                        (brand_id, actor.organization_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("品牌编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="brand.registered",
                            resource_type="brand", resource_id=brand_id, detail={"name": name})
                return "brand", brand_id, {"brand_id": brand_id}

            return self._idempotent(connection, request_id=request_id, action="register_brand",
                                    payload=payload, create=create)

    def register_region(self, *, request_id: str, actor_id: str,
                        region_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "region_id": region_id, "name": name}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            region_id = self._id(region_id, "region_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO cp_regions(region_id,name,created_at) VALUES(?,?,?)",
                        (region_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("区域编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="region.registered",
                            resource_type="region", resource_id=region_id, detail={"name": name})
                return "region", region_id, {"region_id": region_id}

            return self._idempotent(connection, request_id=request_id, action="register_region",
                                    payload=payload, create=create)

    def register_store(self, *, request_id: str, actor_id: str, store_id: str,
                       region_id: str, channel: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "store_id": store_id, "region_id": region_id,
                   "channel": channel, "name": name}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            store_id = self._id(store_id, "store_id")
            region_id = self._id(region_id, "region_id")
            name = self._text(name, "name")
            if channel not in CHANNELS:
                raise ValidationError("channel 必须是 supermarket/restaurant/instant_retail")
            if connection.execute("SELECT 1 FROM cp_regions WHERE region_id=?",
                                  (region_id,)).fetchone() is None:
                raise NotFoundError("区域不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO cp_stores(store_id,region_id,channel,name,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (store_id, region_id, channel, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("门店编号已经存在或区域无效") from exc
                self._audit(connection, actor_id=actor_id, action="store.registered",
                            resource_type="store", resource_id=store_id,
                            detail={"region_id": region_id, "channel": channel, "name": name})
                return "store", store_id, {"store_id": store_id}

            return self._idempotent(connection, request_id=request_id, action="register_store",
                                    payload=payload, create=create)

    def register_product(self, *, request_id: str, actor_id: str, product_id: str,
                         brand_id: str, sku: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "product_id": product_id, "brand_id": brand_id,
                   "sku": sku, "name": name}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, CREATOR_ROLES)
            product_id = self._id(product_id, "product_id")
            brand_id = self._id(brand_id, "brand_id")
            sku = self._text(sku, "sku", 80)
            name = self._text(name, "name")
            brand = connection.execute("SELECT * FROM cp_brands WHERE brand_id=?",
                                       (brand_id,)).fetchone()
            if brand is None:
                raise NotFoundError("品牌不存在")
            if brand["organization_id"] != actor.organization_id and actor.role != "admin":
                raise PermissionDenied("不能为其他组织的品牌登记产品")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO cp_products(product_id,brand_id,sku,name,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (product_id, brand_id, sku, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("产品编号或 SKU 已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="product.registered",
                            resource_type="product", resource_id=product_id,
                            detail={"brand_id": brand_id, "sku": sku, "name": name})
                return "product", product_id, {"product_id": product_id}

            return self._idempotent(connection, request_id=request_id, action="register_product",
                                    payload=payload, create=create)

    # --------------------------------------------------------- 生效期版本规则

    def _approve_version(self, connection, *, actor: Actor, table: str, key: str,
                         version_id: str, action: str, resource_type: str) -> None:
        row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{resource_type} 版本不存在")
        if row["status"] == "effective":
            raise StateConflict("版本已经生效，不能重复批准或修改")
        if row["created_by"] == actor.actor_id:
            raise PermissionDenied("批准人不能与创建人相同")
        try:
            connection.execute(
                f"UPDATE {table} SET status='effective', approved_by=?, approved_at=? WHERE {key}=?",
                (actor.actor_id, self._now(), version_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一业务键在该生效日已有生效版本，不能重复生效") from exc
        self._audit(connection, actor_id=actor.actor_id, action=action,
                    resource_type=resource_type, resource_id=version_id,
                    detail={"content_hash": row["content_hash"], "created_by": row["created_by"]})

    def create_product_version(self, *, request_id: str, actor_id: str, product_id: str,
                               tier: str, lifecycle_stage: str, list_price_per_case: Any,
                               effective_from: str) -> dict[str, Any]:
        """登记一个产品价盘版本（层级/生命周期/挂牌价），批准后按生效日生效。"""

        payload = {"actor_id": actor_id, "product_id": product_id, "tier": tier,
                   "lifecycle_stage": lifecycle_stage, "list_price_per_case": str(list_price_per_case),
                   "effective_from": effective_from}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            product_id = self._id(product_id, "product_id")
            effective_from = self._date(effective_from, "effective_from")
            if tier not in PRODUCT_TIERS:
                raise ValidationError("tier 必须是 premium/sub_premium/mass")
            if lifecycle_stage not in LIFECYCLE_STAGES:
                raise ValidationError("lifecycle_stage 不合法")
            list_price = money(list_price_per_case)
            if list_price <= 0:
                raise ValidationError("挂牌价必须为正数")
            if connection.execute("SELECT 1 FROM cp_products WHERE product_id=?",
                                  (product_id,)).fetchone() is None:
                raise NotFoundError("产品不存在")
            content = {"product_id": product_id, "tier": tier,
                       "lifecycle_stage": lifecycle_stage,
                       "list_price_per_case": str(list_price), "effective_from": effective_from}
            content_hash = digest(content)

            def create():
                version_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO cp_product_versions(version_id,product_id,tier,lifecycle_stage,"
                        "list_price_per_case,effective_from,status,content_hash,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'draft',?,?,?)",
                        (version_id, product_id, tier, lifecycle_stage, str(list_price),
                         effective_from, content_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一产品同一天已有生效版本") from exc
                self._audit(connection, actor_id=actor_id, action="product_version.created",
                            resource_type="product_version", resource_id=version_id,
                            detail={**content, "content_hash": content_hash})
                return "product_version", version_id, {"version_id": version_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_product_version", payload=payload, create=create)

    def approve_product_version(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, APPROVER_ROLES)
            version_id = self._id(version_id, "version_id")

            def create():
                self._approve_version(connection, actor=actor, table="cp_product_versions",
                                      key="version_id", version_id=version_id,
                                      action="product_version.approved",
                                      resource_type="product_version")
                return "product_version", version_id, {"version_id": version_id, "status": "effective"}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_product_version", payload=payload, create=create)

    def create_floor_version(self, *, request_id: str, actor_id: str, product_id: str,
                             region_id: str, floor_price_per_case: Any,
                             effective_from: str) -> dict[str, Any]:
        """登记区域底价版本，批准后生效，调价只能追加新版本。"""

        payload = {"actor_id": actor_id, "product_id": product_id, "region_id": region_id,
                   "floor_price_per_case": str(floor_price_per_case), "effective_from": effective_from}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            product_id = self._id(product_id, "product_id")
            region_id = self._id(region_id, "region_id")
            effective_from = self._date(effective_from, "effective_from")
            floor_price = money(floor_price_per_case)
            if floor_price < 0:
                raise ValidationError("底价不能为负")
            if connection.execute("SELECT 1 FROM cp_products WHERE product_id=?",
                                  (product_id,)).fetchone() is None:
                raise NotFoundError("产品不存在")
            if connection.execute("SELECT 1 FROM cp_regions WHERE region_id=?",
                                  (region_id,)).fetchone() is None:
                raise NotFoundError("区域不存在")
            content = {"product_id": product_id, "region_id": region_id,
                       "floor_price_per_case": str(floor_price), "effective_from": effective_from}
            content_hash = digest(content)

            def create():
                floor_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO cp_floor_versions(floor_id,product_id,region_id,"
                        "floor_price_per_case,effective_from,status,content_hash,created_by,created_at) "
                        "VALUES(?,?,?,?,?,'draft',?,?,?)",
                        (floor_id, product_id, region_id, str(floor_price), effective_from,
                         content_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一产品/区域/日期已有生效底价") from exc
                self._audit(connection, actor_id=actor_id, action="floor_version.created",
                            resource_type="floor_version", resource_id=floor_id,
                            detail={**content, "content_hash": content_hash})
                return "floor_version", floor_id, {"floor_id": floor_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_floor_version", payload=payload, create=create)

    def approve_floor_version(self, *, request_id: str, actor_id: str, floor_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "floor_id": floor_id}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, APPROVER_ROLES)
            floor_id = self._id(floor_id, "floor_id")

            def create():
                self._approve_version(connection, actor=actor, table="cp_floor_versions",
                                      key="floor_id", version_id=floor_id,
                                      action="floor_version.approved", resource_type="floor_version")
                return "floor_version", floor_id, {"floor_id": floor_id, "status": "effective"}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_floor_version", payload=payload, create=create)

    def create_contract(self, *, request_id: str, actor_id: str, brand_id: str, channel: str,
                        max_discount_rate: Any, effective_from: str,
                        region_id: str | None = None, terms: dict[str, Any] | None = None) -> dict[str, Any]:
        """登记渠道合同（含最大综合折扣率），region_id 为空表示渠道通用。"""

        payload = {"actor_id": actor_id, "brand_id": brand_id, "channel": channel,
                   "region_id": region_id, "max_discount_rate": str(max_discount_rate),
                   "effective_from": effective_from, "terms": terms or {}}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            brand_id = self._id(brand_id, "brand_id")
            effective_from = self._date(effective_from, "effective_from")
            if channel not in CHANNELS:
                raise ValidationError("channel 不合法")
            if region_id is not None:
                region_id = self._id(region_id, "region_id")
                if connection.execute("SELECT 1 FROM cp_regions WHERE region_id=?",
                                      (region_id,)).fetchone() is None:
                    raise NotFoundError("区域不存在")
            rate = Decimal(str(max_discount_rate))
            if not Decimal("0") <= rate <= Decimal("1"):
                raise ValidationError("max_discount_rate 必须在 0 到 1 之间")
            if connection.execute("SELECT 1 FROM cp_brands WHERE brand_id=?",
                                  (brand_id,)).fetchone() is None:
                raise NotFoundError("品牌不存在")
            terms = terms or {}
            content = {"brand_id": brand_id, "channel": channel, "region_id": region_id,
                       "max_discount_rate": str(rate), "effective_from": effective_from, "terms": terms}
            content_hash = digest(content)

            def create():
                contract_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO cp_contracts(contract_id,brand_id,channel,region_id,"
                        "max_discount_rate,terms_json,effective_from,status,content_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,'draft',?,?,?)",
                        (contract_id, brand_id, channel, region_id, str(rate),
                         canonical_json(terms), effective_from, content_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一品牌/渠道/区域/日期已有生效合同") from exc
                self._audit(connection, actor_id=actor_id, action="contract.created",
                            resource_type="contract", resource_id=contract_id,
                            detail={**content, "content_hash": content_hash})
                return "contract", contract_id, {"contract_id": contract_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_contract", payload=payload, create=create)

    def approve_contract(self, *, request_id: str, actor_id: str, contract_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "contract_id": contract_id}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, APPROVER_ROLES)
            contract_id = self._id(contract_id, "contract_id")

            def create():
                self._approve_version(connection, actor=actor, table="cp_contracts",
                                      key="contract_id", version_id=contract_id,
                                      action="contract.approved", resource_type="contract")
                return "contract", contract_id, {"contract_id": contract_id, "status": "effective"}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_contract", payload=payload, create=create)

    # --------------------------------------------------------------- 促销承诺

    def create_promise(self, *, request_id: str, actor_id: str, promise_id: str, brand_id: str,
                       name: str, channel: str, fund_type: str, start_date: str, end_date: str,
                       budget_cap: Any, components: list[dict[str, Any]],
                       region_id: str | None = None,
                       scope: dict[str, Any] | None = None) -> dict[str, Any]:
        """登记促销承诺。

        components 每项：{"type": ..., "amount_per_case": x} 或 amount_per_order。
        scope：{"tiers": [...], "stages": [...]}，留空表示不限制。
        """

        payload = {"actor_id": actor_id, "promise_id": promise_id, "brand_id": brand_id,
                   "name": name, "channel": channel, "fund_type": fund_type,
                   "start_date": start_date, "end_date": end_date, "budget_cap": str(budget_cap),
                   "components": components, "region_id": region_id, "scope": scope or {}}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, CREATOR_ROLES)
            promise_id = self._id(promise_id, "promise_id")
            brand_id = self._id(brand_id, "brand_id")
            name = self._text(name, "name")
            start_date = self._date(start_date, "start_date")
            end_date = self._date(end_date, "end_date")
            if start_date > end_date:
                raise ValidationError("生效起止日期颠倒")
            if channel not in CHANNELS:
                raise ValidationError("channel 不合法")
            if fund_type not in {"general", "cultivation"}:
                raise ValidationError("fund_type 必须是 general/cultivation")
            cap = money(budget_cap)
            if cap <= 0:
                raise ValidationError("预算上限必须为正数")
            if not isinstance(components, list) or not components:
                raise ValidationError("至少包含一个优惠组件")
            normalised: list[dict[str, str]] = []
            for position, component in enumerate(components):
                ctype = component.get("type")
                if ctype not in DISCOUNT_TYPES:
                    raise ValidationError(f"第 {position + 1} 个组件类型不合法")
                if ctype == "instant":
                    amount = money(component.get("amount_per_order"))
                    if amount <= 0:
                        raise ValidationError("即时立减金额必须为正")
                    normalised.append({"type": ctype, "amount_per_order": str(amount),
                                       "amount_per_case": None})
                else:
                    amount = money(component.get("amount_per_case"))
                    if amount <= 0:
                        raise ValidationError("按箱折让金额必须为正")
                    normalised.append({"type": ctype, "amount_per_case": str(amount),
                                       "amount_per_order": None})
            scope = scope or {}
            tiers = set(scope.get("tiers") or [])
            stages = set(scope.get("stages") or [])
            if not tiers <= PRODUCT_TIERS or not stages <= LIFECYCLE_STAGES:
                raise ValidationError("scope 中的层级或生命周期不合法")
            if region_id is not None:
                region_id = self._id(region_id, "region_id")
            brand = connection.execute("SELECT * FROM cp_brands WHERE brand_id=?",
                                       (brand_id,)).fetchone()
            if brand is None:
                raise NotFoundError("品牌不存在")
            if brand["organization_id"] != actor.organization_id and actor.role != "admin":
                raise PermissionDenied("不能为其他组织的品牌创建承诺")
            scope_json = {"tiers": sorted(tiers), "stages": sorted(stages)}
            content = {"promise_id": promise_id, "brand_id": brand_id, "name": name,
                       "channel": channel, "region_id": region_id, "fund_type": fund_type,
                       "start_date": start_date, "end_date": end_date,
                       "budget_cap": str(cap), "components": normalised, "scope": scope_json}
            content_hash = digest(content)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO cp_promises(promise_id,brand_id,name,channel,region_id,"
                        "fund_type,start_date,end_date,budget_cap,scope_json,status,content_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)",
                        (promise_id, brand_id, name, channel, region_id, fund_type,
                         start_date, end_date, str(cap), canonical_json(scope_json),
                         content_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("促销承诺编号已经存在") from exc
                for seq, component in enumerate(normalised):
                    connection.execute(
                        "INSERT INTO cp_promise_components(component_id,promise_id,seq,type,"
                        "amount_per_case,amount_per_order) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, promise_id, seq, component["type"],
                         component["amount_per_case"], component["amount_per_order"]),
                    )
                self._audit(connection, actor_id=actor_id, action="promise.created",
                            resource_type="promise", resource_id=promise_id,
                            detail={**content, "content_hash": content_hash})
                return "promise", promise_id, {"promise_id": promise_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id, action="create_promise",
                                    payload=payload, create=create)

    def add_stack_rule(self, *, request_id: str, actor_id: str,
                       promise_id_a: str, promise_id_b: str) -> dict[str, Any]:
        """显式允许两个促销承诺叠加（无序对）。"""

        payload = {"actor_id": actor_id, "promise_id_a": promise_id_a, "promise_id_b": promise_id_b}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            a = self._id(promise_id_a, "promise_id_a")
            b = self._id(promise_id_b, "promise_id_b")
            if a == b:
                raise ValidationError("不能与自身建立叠加规则")
            a, b = sorted((a, b))
            for promise_id in (a, b):
                if connection.execute("SELECT 1 FROM cp_promises WHERE promise_id=?",
                                      (promise_id,)).fetchone() is None:
                    raise NotFoundError(f"促销承诺 {promise_id} 不存在")

            def create():
                existing = connection.execute(
                    "SELECT rule_id FROM cp_promise_stack_rules WHERE promise_id_a=? AND promise_id_b=?",
                    (a, b),
                ).fetchone()
                if existing:
                    return "stack_rule", existing["rule_id"], {"rule_id": existing["rule_id"]}
                rule_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cp_promise_stack_rules(rule_id,promise_id_a,promise_id_b,"
                    "created_by,created_at) VALUES(?,?,?,?,?)",
                    (rule_id, a, b, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="stack_rule.added",
                            resource_type="stack_rule", resource_id=rule_id,
                            detail={"promise_id_a": a, "promise_id_b": b})
                return "stack_rule", rule_id, {"rule_id": rule_id}

            return self._idempotent(connection, request_id=request_id, action="add_stack_rule",
                                    payload=payload, create=create)

    def approve_promise(self, *, request_id: str, actor_id: str, promise_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "promise_id": promise_id}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, APPROVER_ROLES)
            promise_id = self._id(promise_id, "promise_id")

            def create():
                row = connection.execute("SELECT * FROM cp_promises WHERE promise_id=?",
                                         (promise_id,)).fetchone()
                if row is None:
                    raise NotFoundError("促销承诺不存在")
                if row["status"] == "active":
                    raise StateConflict("促销承诺已经生效")
                if row["created_by"] == actor.actor_id:
                    raise PermissionDenied("批准人不能与创建人相同")
                connection.execute(
                    "UPDATE cp_promises SET status='active', approved_by=?, approved_at=? "
                    "WHERE promise_id=?",
                    (actor.actor_id, self._now(), promise_id),
                )
                self._audit(connection, actor_id=actor.actor_id, action="promise.approved",
                            resource_type="promise", resource_id=promise_id,
                            detail={"content_hash": row["content_hash"],
                                    "created_by": row["created_by"]})
                return "promise", promise_id, {"promise_id": promise_id, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_promise", payload=payload, create=create)

    # --------------------------------------------------------------- 紧急例外

    def request_emergency_exception(self, *, request_id: str, actor_id: str, promise_id: str,
                                    product_id: str, store_id: str, max_cases: Any,
                                    amount_per_case: Any, valid_from: str, valid_to: str,
                                    reason: str) -> dict[str, Any]:
        """申请紧急例外：必须限定单一门店、数量上限与期限。"""

        payload = {"actor_id": actor_id, "promise_id": promise_id, "product_id": product_id,
                   "store_id": store_id, "max_cases": str(max_cases),
                   "amount_per_case": str(amount_per_case), "valid_from": valid_from,
                   "valid_to": valid_to, "reason": reason}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, CREATOR_ROLES)
            promise_id = self._id(promise_id, "promise_id")
            product_id = self._id(product_id, "product_id")
            store_id = self._id(store_id, "store_id")
            valid_from = self._date(valid_from, "valid_from")
            valid_to = self._date(valid_to, "valid_to")
            if valid_from > valid_to:
                raise ValidationError("例外期限起止颠倒")
            max_cases_dec = Decimal(str(max_cases))
            if max_cases_dec <= 0:
                raise ValidationError("数量上限必须为正")
            amount = money(amount_per_case)
            if amount <= 0:
                raise ValidationError("例外折让金额必须为正")
            reason = self._text(reason, "reason", 500)
            if connection.execute("SELECT 1 FROM cp_promises WHERE promise_id=? AND status='active'",
                                  (promise_id,)).fetchone() is None:
                raise NotFoundError("促销承诺不存在或未生效")
            if connection.execute("SELECT 1 FROM cp_products WHERE product_id=?",
                                  (product_id,)).fetchone() is None:
                raise NotFoundError("产品不存在")
            store = connection.execute("SELECT * FROM cp_stores WHERE store_id=?",
                                       (store_id,)).fetchone()
            if store is None:
                raise NotFoundError("门店不存在")

            def create():
                exception_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cp_emergency_exceptions(exception_id,promise_id,product_id,store_id,"
                    "max_cases,amount_per_case,valid_from,valid_to,reason,status,requested_by,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?, 'requested',?,?)",
                    (exception_id, promise_id, product_id, store_id, str(max_cases_dec),
                     str(amount), valid_from, valid_to, reason, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="emergency.requested",
                            resource_type="emergency_exception", resource_id=exception_id,
                            detail={"promise_id": promise_id, "store_id": store_id,
                                    "max_cases": str(max_cases_dec), "valid_from": valid_from,
                                    "valid_to": valid_to})
                return ("emergency_exception", exception_id,
                        {"exception_id": exception_id, "status": "requested"})

            return self._idempotent(connection, request_id=request_id,
                                    action="request_emergency_exception",
                                    payload=payload, create=create)

    def review_emergency_exception(self, *, request_id: str, actor_id: str, exception_id: str,
                                   decision: str, review_note: str = "") -> dict[str, Any]:
        """由另一名复核角色批准/驳回紧急例外。"""

        payload = {"actor_id": actor_id, "exception_id": exception_id, "decision": decision,
                   "review_note": review_note}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, APPROVER_ROLES)
            exception_id = self._id(exception_id, "exception_id")
            if decision not in {"approved", "rejected"}:
                raise ValidationError("decision 必须是 approved/rejected")
            review_note = str(review_note).strip()[:500]

            def create():
                row = connection.execute(
                    "SELECT * FROM cp_emergency_exceptions WHERE exception_id=?", (exception_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("紧急例外不存在")
                if row["status"] != "requested":
                    raise StateConflict("紧急例外已经复核")
                if row["requested_by"] == actor.actor_id:
                    raise PermissionDenied("复核人不能与申请人相同")
                requester = connection.execute(
                    "SELECT role FROM actors WHERE actor_id=?", (row["requested_by"],)
                ).fetchone()
                if requester is not None and requester["role"] == actor.role:
                    raise PermissionDenied("紧急例外必须由不同角色复核")
                connection.execute(
                    "UPDATE cp_emergency_exceptions SET status=?, reviewer_id=?, review_note=?, "
                    "reviewed_at=? WHERE exception_id=?",
                    (decision, actor.actor_id, review_note, self._now(), exception_id),
                )
                self._audit(connection, actor_id=actor.actor_id,
                            action=f"emergency.{decision}",
                            resource_type="emergency_exception", resource_id=exception_id,
                            detail={"requested_by": row["requested_by"], "note": review_note})
                return ("emergency_exception", exception_id,
                        {"exception_id": exception_id, "status": decision})

            return self._idempotent(connection, request_id=request_id,
                                    action="review_emergency_exception",
                                    payload=payload, create=create)

    # ------------------------------------------------------------- 规则解析

    def _product_version(self, connection, product_id: str, on_date: str):
        return connection.execute(
            "SELECT * FROM cp_product_versions WHERE product_id=? AND status='effective' "
            "AND effective_from<=? ORDER BY effective_from DESC, rowid DESC LIMIT 1",
            (product_id, on_date),
        ).fetchone()

    def _floor_version(self, connection, product_id: str, region_id: str, on_date: str):
        return connection.execute(
            "SELECT * FROM cp_floor_versions WHERE product_id=? AND region_id=? AND status='effective' "
            "AND effective_from<=? ORDER BY effective_from DESC, rowid DESC LIMIT 1",
            (product_id, region_id, on_date),
        ).fetchone()

    def _contract(self, connection, brand_id: str, channel: str, region_id: str, on_date: str):
        # 优先区域专属合同，回退渠道通用合同（region_id 为空）。
        return connection.execute(
            "SELECT * FROM cp_contracts WHERE brand_id=? AND channel=? AND status='effective' "
            "AND effective_from<=? AND (region_id=? OR region_id IS NULL) "
            "ORDER BY (region_id IS NULL), effective_from DESC, rowid DESC LIMIT 1",
            (brand_id, channel, on_date, region_id),
        ).fetchone()

    def _promise_components(self, connection, promise_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM cp_promise_components WHERE promise_id=? ORDER BY seq", (promise_id,)
        ).fetchall()
        return [{"type": row["type"], "amount_per_case": row["amount_per_case"],
                 "amount_per_order": row["amount_per_order"]} for row in rows]

    def _stack_allowed(self, connection, a: str, b: str) -> bool:
        left, right = sorted((a, b))
        return connection.execute(
            "SELECT 1 FROM cp_promise_stack_rules WHERE promise_id_a=? AND promise_id_b=?",
            (left, right),
        ).fetchone() is not None

    # --------------------------------------------------------------- 活动提交

    def _evaluate_campaign(self, connection, *, brand_id: str, channel: str, region_id: str,
                           store_id: str | None, product_id: str, cases_dec: Decimal,
                           activity_date: str, promise_ids: list[str],
                           exception_id: str | None,
                           self_campaign_id: str | None = None) -> dict[str, Any]:
        """解析当日全部规则并计算实际成交条件与冲突来源。

        self_campaign_id 用于下单复核：计算预算净占用时排除本活动自己的台账
        （该额度在活动批准时已整体预留），避免同一笔预留被重复计算。
        """

        conflicts: list[dict[str, Any]] = []

        def add(code: str, severity: str, message: str, sources: list[Any]) -> None:
            conflicts.append({"code": code, "severity": severity, "message": message,
                              "sources": sources})

        product_version = self._product_version(connection, product_id, activity_date)
        if product_version is None:
            add("PRODUCT_RULE_MISSING", "blocker",
                f"产品 {product_id} 在 {activity_date} 没有已生效的价盘版本", [product_id])
            return {"conflicts": conflicts, "resolution": None, "evaluation": None,
                     "promise_rows": [], "exception": None}

        floor_row = self._floor_version(connection, product_id, region_id, activity_date)
        if floor_row is None:
            add("FLOOR_MISSING", "blocker",
                f"产品 {product_id} 在区域 {region_id} 没有已生效底价",
                [product_id, region_id])

        contract_row = self._contract(connection, brand_id, channel, region_id, activity_date)
        if contract_row is None:
            add("CONTRACT_MISSING", "blocker",
                f"品牌 {brand_id} 的 {channel} 渠道在 {activity_date} 没有已生效合同",
                [brand_id, channel])

        # 紧急例外（可选）。
        exception = None
        if exception_id is not None:
            exception = connection.execute(
                "SELECT * FROM cp_emergency_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if exception is None:
                add("EXCEPTION_MISSING", "blocker", "紧急例外不存在", [exception_id])
            else:
                if exception["status"] != "approved":
                    add("EXCEPTION_NOT_APPROVED", "blocker",
                        "紧急例外未经双人复核批准", [exception_id])
                if exception["store_id"] != store_id:
                    add("EXCEPTION_STORE_MISMATCH", "blocker",
                        "紧急例外限定的门店与活动门店不一致",
                        [exception["store_id"], store_id])
                if exception["product_id"] != product_id:
                    add("EXCEPTION_PRODUCT_MISMATCH", "blocker",
                        "紧急例外限定的产品与活动产品不一致",
                        [exception["product_id"], product_id])
                if not exception["valid_from"] <= activity_date <= exception["valid_to"]:
                    add("EXCEPTION_OUT_OF_PERIOD", "blocker",
                        "活动日期不在紧急例外的有效期限内",
                        [exception["valid_from"], exception["valid_to"], activity_date])
                if cases_dec > Decimal(exception["max_cases"]):
                    add("EXCEPTION_CASES_EXCEEDED", "blocker",
                        f"活动数量 {cases_dec} 超过紧急例外上限 {exception['max_cases']}",
                        [exception_id, str(cases_dec), exception["max_cases"]])

        # 解析每条促销承诺并做适用性校验。
        promise_rows = []
        components: list[dict[str, Any]] = []
        seen: set[str] = set()
        for position, promise_id in enumerate(promise_ids):
            if promise_id in seen:
                add("PROMISE_DUPLICATED", "blocker", f"促销承诺 {promise_id} 重复引用",
                    [promise_id])
                continue
            seen.add(promise_id)
            row = connection.execute("SELECT * FROM cp_promises WHERE promise_id=?",
                                     (promise_id,)).fetchone()
            if row is None:
                add("PROMISE_MISSING", "blocker", f"促销承诺 {promise_id} 不存在", [promise_id])
                continue
            source = [promise_id]
            if row["brand_id"] != brand_id:
                add("PROMISE_BRAND_MISMATCH", "blocker",
                    f"促销承诺 {promise_id} 属于其他品牌", source)
            if row["channel"] != channel:
                add("PROMISE_CHANNEL_MISMATCH", "blocker",
                    f"促销承诺 {promise_id} 不适用于 {channel} 渠道",
                    source + [row["channel"], channel])
            if row["region_id"] is not None and row["region_id"] != region_id:
                add("PROMISE_REGION_MISMATCH", "blocker",
                    f"促销承诺 {promise_id} 不适用于区域 {region_id}",
                    source + [row["region_id"], region_id])
            if row["status"] != "active":
                add("PROMISE_NOT_ACTIVE", "blocker",
                    f"促销承诺 {promise_id} 当前状态为 {row['status']}", source)
            if not row["start_date"] <= activity_date <= row["end_date"]:
                add("PROMISE_OUT_OF_PERIOD", "blocker",
                    f"活动日期 {activity_date} 不在承诺有效期 {row['start_date']}~{row['end_date']}",
                    source + [row["start_date"], row["end_date"]])
            scope = json.loads(row["scope_json"])
            if scope.get("tiers") and product_version["tier"] not in scope["tiers"]:
                add("PROMISE_TIER_OUT_OF_SCOPE", "blocker",
                    f"促销承诺 {promise_id} 不适用于 {product_version['tier']} 层级产品",
                    source + [product_version["tier"]])
            if scope.get("stages") and product_version["lifecycle_stage"] not in scope["stages"]:
                add("PROMISE_STAGE_OUT_OF_SCOPE", "blocker",
                    f"促销承诺 {promise_id} 不适用于 {product_version['lifecycle_stage']} 阶段产品",
                    source + [product_version["lifecycle_stage"]])
            # 培育专项费用只能用于培育/导入期产品。
            if row["fund_type"] == "cultivation" and not cultivation_allowed(
                    product_version["lifecycle_stage"]):
                add("CULTIVATION_FUND_MISMATCH", "blocker",
                    f"培育专项费用 {promise_id} 不能用于 {product_version['lifecycle_stage']} 产品",
                    source + [product_version["lifecycle_stage"]])
            if exception is not None and exception["promise_id"] == promise_id:
                # 例外折让挂在被例外的承诺上。
                if exception["status"] == "approved":
                    components.append({"promise_id": promise_id, "type": "emergency",
                                       "amount_per_case": exception["amount_per_case"]})
            for component in self._promise_components(connection, promise_id):
                converted = {"promise_id": promise_id, "type": component["type"]}
                if component["amount_per_case"] is not None:
                    converted["amount_per_case"] = component["amount_per_case"]
                if component["amount_per_order"] is not None:
                    converted["amount_per_order"] = component["amount_per_order"]
                components.append(converted)
            promise_rows.append(row)

        # 可叠加规则：每一对承诺都必须被显式放行。
        for i in range(len(promise_ids)):
            for j in range(i + 1, len(promise_ids)):
                a, b = promise_ids[i], promise_ids[j]
                if a != b and not self._stack_allowed(connection, a, b):
                    add("STACK_NOT_ALLOWED", "blocker",
                        f"促销承诺 {a} 与 {b} 没有可叠加规则，禁止叠加", [a, b])

        # 核算全部优惠后的实际条件。
        evaluation = evaluate_offer(
            list_price_per_case=product_version["list_price_per_case"],
            cases=cases_dec,
            components=components,
            floor_price_per_case=floor_row["floor_price_per_case"] if floor_row else None,
        )

        # 击穿区域底价：给出造成击穿的优惠来源。
        if evaluation["floor_breached"]:
            add("PRICE_FLOOR_BREACH", "blocker",
                f"优惠后单箱成交价 {evaluation['net_price_per_case']} 低于区域底价 "
                f"{evaluation['floor_price_per_case']}",
                evaluation["floor_breach_sources"])

        # 渠道合同最大综合折扣率。
        if contract_row is not None and contract_row["max_discount_rate"] is not None:
            cap_rate = Decimal(contract_row["max_discount_rate"])
            if evaluation["list_total"] > 0:
                actual_rate = (evaluation["price_discount_total"]
                               / evaluation["list_total"]).quantize(Decimal("0.0001"))
                if actual_rate > cap_rate:
                    add("CONTRACT_RATE_EXCEEDED", "blocker",
                        f"综合折扣率 {actual_rate} 超过渠道合同上限 {cap_rate}",
                        [contract_row["contract_id"], str(actual_rate), str(cap_rate)])

        # 费用上限：按承诺汇总本次活动将占用的费用（含费项与影响价盘的折让）。
        per_promise_spend: dict[str, Decimal] = {}
        for item in evaluation["line_items"]:
            per_promise_spend[item["promise_id"]] = (
                per_promise_spend.get(item["promise_id"], Decimal("0"))
                + item["discount_amount"]
            )
        for row in promise_rows:
            spend = money(per_promise_spend.get(row["promise_id"], Decimal("0")))
            # 净占用 = 尚未核销的预留 + 已核销净费用（退货/无效凭证已冲回）。
            # 下单复核时排除本活动自己已预留的额度，避免重复计算。
            committed = money(self._used(connection, row["promise_id"],
                                         exclude_campaign_id=self_campaign_id))
            cap = Decimal(row["budget_cap"])
            if committed + spend > cap:
                add("BUDGET_EXCEEDED", "blocker",
                    f"承诺 {row['promise_id']} 本次占用 {spend}，当前净占用 {committed}，"
                    f"超过费用上限 {cap}",
                    [row["promise_id"], str(spend), str(committed), str(cap)])

        resolution = {
            "product_version_id": product_version["version_id"],
            "product_content_hash": product_version["content_hash"],
            "tier": product_version["tier"],
            "lifecycle_stage": product_version["lifecycle_stage"],
            "floor_id": floor_row["floor_id"] if floor_row else None,
            "floor_content_hash": floor_row["content_hash"] if floor_row else None,
            "contract_id": contract_row["contract_id"] if contract_row else None,
            "contract_content_hash": contract_row["content_hash"] if contract_row else None,
            "promises": [{"promise_id": row["promise_id"], "content_hash": row["content_hash"],
                          "fund_type": row["fund_type"], "budget_cap": row["budget_cap"]}
                         for row in promise_rows],
            "exception_id": exception_id,
        }
        return {"conflicts": conflicts, "resolution": resolution, "evaluation": evaluation,
                "promise_rows": promise_rows, "exception": exception,
                "per_promise_spend": {pid: str(amount) for pid, amount in per_promise_spend.items()}}

    def submit_campaign(self, *, request_id: str, actor_id: str, brand_id: str, channel: str,
                        region_id: str, product_id: str, cases: Any, activity_date: str,
                        promise_ids: list[str], store_id: str | None = None,
                        exception_id: str | None = None) -> dict[str, Any]:
        """提交活动：计算全部优惠后的实际条件并返回冲突来源。"""

        payload = {"actor_id": actor_id, "brand_id": brand_id, "channel": channel,
                   "region_id": region_id, "store_id": store_id, "product_id": product_id,
                   "cases": str(cases), "activity_date": activity_date,
                   "promise_ids": promise_ids, "exception_id": exception_id}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            brand_id = self._id(brand_id, "brand_id")
            region_id = self._id(region_id, "region_id")
            product_id = self._id(product_id, "product_id")
            activity_date = self._date(activity_date, "activity_date")
            if channel not in CHANNELS:
                raise ValidationError("channel 不合法")
            if store_id is not None:
                store_id = self._id(store_id, "store_id")
            if exception_id is not None:
                exception_id = self._id(exception_id, "exception_id")
            cases_dec = Decimal(str(cases)).quantize(Decimal("0.001"))
            if cases_dec <= 0:
                raise ValidationError("活动数量必须为正")
            if not isinstance(promise_ids, list) or not promise_ids:
                raise ValidationError("至少选择一个促销承诺")
            promise_ids = [self._id(pid, "promise_id") for pid in promise_ids]

            result = self._evaluate_campaign(
                connection, brand_id=brand_id, channel=channel, region_id=region_id,
                store_id=store_id, product_id=product_id, cases_dec=cases_dec,
                activity_date=activity_date, promise_ids=promise_ids,
                exception_id=exception_id)

            def create():
                campaign_id = uuid.uuid4().hex
                evaluation_json = self._evaluation_json(result)
                connection.execute(
                    "INSERT INTO cp_campaigns(campaign_id,brand_id,channel,region_id,store_id,"
                    "product_id,cases,activity_date,exception_id,status,evaluation_json,"
                    "submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'submitted',?,?,?)",
                    (campaign_id, brand_id, channel, region_id, store_id, product_id,
                     str(cases_dec), activity_date, exception_id,
                     canonical_json(evaluation_json), actor_id, self._now()),
                )
                for position, promise_id in enumerate(promise_ids):
                    connection.execute(
                        "INSERT INTO cp_campaign_promises(campaign_id,promise_id,position) "
                        "VALUES(?,?,?)",
                        (campaign_id, promise_id, position),
                    )
                for position, conflict in enumerate(result["conflicts"]):
                    connection.execute(
                        "INSERT INTO cp_campaign_conflicts(conflict_id,campaign_id,position,code,"
                        "severity,message,sources_json) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, campaign_id, position, conflict["code"],
                         conflict["severity"], conflict["message"],
                         canonical_json(conflict["sources"])),
                    )
                self._audit(connection, actor_id=actor_id, action="campaign.submitted",
                            resource_type="campaign", resource_id=campaign_id,
                            detail={"brand_id": brand_id, "product_id": product_id,
                                    "activity_date": activity_date,
                                    "conflict_count": len(result["conflicts"]),
                                    "blockers": sum(1 for c in result["conflicts"]
                                                    if c["severity"] == "blocker")})
                return ("campaign", campaign_id,
                        {"campaign_id": campaign_id, "status": "submitted",
                         "conflicts": result["conflicts"],
                         "evaluation": self._evaluation_json(result)["evaluation"],
                         "per_promise_spend": result["per_promise_spend"],
                         "has_blocker": any(c["severity"] == "blocker"
                                            for c in result["conflicts"])})

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_campaign", payload=payload, create=create)

    @staticmethod
    def _evaluation_json(result: dict[str, Any]) -> dict[str, Any]:
        evaluation = result["evaluation"]
        serialisable_evaluation = None
        if evaluation is not None:
            serialisable_evaluation = json_ready(evaluation)
        return {"resolution": result["resolution"], "evaluation": serialisable_evaluation,
                "per_promise_spend": result["per_promise_spend"]}

    def approve_campaign(self, *, request_id: str, actor_id: str, campaign_id: str) -> dict[str, Any]:
        """批准活动：存在阻断性冲突时拒绝；批准后按承诺预留费用。"""

        payload = {"actor_id": actor_id, "campaign_id": campaign_id}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, APPROVER_ROLES)
            campaign_id = self._id(campaign_id, "campaign_id")

            def create():
                row = connection.execute("SELECT * FROM cp_campaigns WHERE campaign_id=?",
                                         (campaign_id,)).fetchone()
                if row is None:
                    raise NotFoundError("活动不存在")
                if row["status"] != "submitted":
                    raise StateConflict(f"活动当前状态为 {row['status']}，不能批准")
                if row["submitted_by"] == actor.actor_id:
                    raise PermissionDenied("批准人不能与提交人相同")
                blockers = connection.execute(
                    "SELECT code,message,sources_json FROM cp_campaign_conflicts "
                    "WHERE campaign_id=? AND severity='blocker' ORDER BY position",
                    (campaign_id,),
                ).fetchall()
                if blockers:
                    details = [{"code": b["code"], "message": b["message"],
                                "sources": json.loads(b["sources_json"])}
                               for b in blockers]
                    # 价盘击穿用专门的异常类型，其余阻断统一状态冲突。
                    if any(b["code"] == "PRICE_FLOOR_BREACH" for b in blockers):
                        from .errors import PriceFloorBreached
                        raise PriceFloorBreached(f"活动存在 {len(blockers)} 项阻断冲突: {details}")
                    raise StateConflict(f"活动存在 {len(blockers)} 项阻断冲突: {details}")

                evaluation_bundle = json.loads(row["evaluation_json"])
                spend_map = evaluation_bundle["per_promise_spend"]
                cases = Decimal(row["cases"])
                # 批准时做硬预算校验，防止提交后、批准前其它活动已占用额度（TOCTOU）。
                for promise_id, spend_text in spend_map.items():
                    committed = money(self._used(connection, promise_id))
                    cap_row = connection.execute(
                        "SELECT budget_cap FROM cp_promises WHERE promise_id=?", (promise_id,)
                    ).fetchone()
                    if committed + money(spend_text) > Decimal(cap_row["budget_cap"]):
                        raise BudgetExceeded(
                            f"批准时承诺 {promise_id} 净占用 {committed} 加本次 {money(spend_text)} "
                            f"超过费用上限 {cap_row['budget_cap']}")
                for promise_id, spend_text in spend_map.items():
                    connection.execute(
                        "INSERT INTO cp_budget_ledger(ledger_id,promise_id,campaign_id,order_id,"
                        "event_id,entry_type,amount_cases,amount_money,created_at) "
                        "VALUES(?,?,?,NULL,NULL,'reserve',?,?,?)",
                        (uuid.uuid4().hex, promise_id, campaign_id, str(cases),
                         str(money(spend_text)), self._now()),
                    )
                connection.execute(
                    "UPDATE cp_campaigns SET status='approved', approved_by=?, approved_at=? "
                    "WHERE campaign_id=?",
                    (actor.actor_id, self._now(), campaign_id),
                )
                self._audit(connection, actor_id=actor.actor_id, action="campaign.approved",
                            resource_type="campaign", resource_id=campaign_id,
                            detail={"reservations": spend_map})
                return "campaign", campaign_id, {"campaign_id": campaign_id, "status": "approved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_campaign", payload=payload, create=create)

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        connection = self.connection
        row = connection.execute("SELECT * FROM cp_campaigns WHERE campaign_id=?",
                                 (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError("活动不存在")
        conflicts = [
            {"position": c["position"], "code": c["code"], "severity": c["severity"],
             "message": c["message"], "sources": json.loads(c["sources_json"])}
            for c in connection.execute(
                "SELECT * FROM cp_campaign_conflicts WHERE campaign_id=? ORDER BY position",
                (campaign_id,))
        ]
        bundle = json.loads(row["evaluation_json"])
        return {"campaign_id": campaign_id, "brand_id": row["brand_id"],
                "channel": row["channel"], "region_id": row["region_id"],
                "store_id": row["store_id"], "product_id": row["product_id"],
                "cases": row["cases"], "activity_date": row["activity_date"],
                "exception_id": row["exception_id"], "status": row["status"],
                "submitted_by": row["submitted_by"], "approved_by": row["approved_by"],
                "resolution": bundle["resolution"], "evaluation": bundle["evaluation"],
                "per_promise_spend": bundle["per_promise_spend"], "conflicts": conflicts}

    # ----------------------------------------------------------------- 订单

    def book_order(self, *, request_id: str, actor_id: str, order_id: str, campaign_id: str,
                   store_id: str, cases: Any, order_date: str) -> dict[str, Any]:
        """按已批准活动下单，固化下单时刻的规则快照（后续调价不影响本订单）。"""

        payload = {"actor_id": actor_id, "order_id": order_id, "campaign_id": campaign_id,
                   "store_id": store_id, "cases": str(cases), "order_date": order_date}
        with self.tx() as connection:
            self._require(self._actor(connection, actor_id), CREATOR_ROLES)
            order_id = self._id(order_id, "order_id")
            campaign_id = self._id(campaign_id, "campaign_id")
            store_id = self._id(store_id, "store_id")
            order_date = self._date(order_date, "order_date")
            cases_dec = Decimal(str(cases)).quantize(Decimal("0.001"))
            if cases_dec <= 0:
                raise ValidationError("订单数量必须为正")

            def create():
                campaign = connection.execute("SELECT * FROM cp_campaigns WHERE campaign_id=?",
                                              (campaign_id,)).fetchone()
                if campaign is None:
                    raise NotFoundError("活动不存在")
                if campaign["status"] != "approved":
                    raise StateConflict("活动未批准，不能下单")
                if campaign["store_id"] and campaign["store_id"] != store_id:
                    raise ValidationError("订单门店与活动限定门店不一致")

                booked = connection.execute(
                    "SELECT COALESCE(SUM(cases),0) AS total FROM cp_orders WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchone()["total"]
                if Decimal(str(booked)) + cases_dec > Decimal(campaign["cases"]):
                    raise StateConflict("订单累计数量超过活动批准数量")

                promise_rows = connection.execute(
                    "SELECT promise_id FROM cp_campaign_promises WHERE campaign_id=? ORDER BY position",
                    (campaign_id,),
                ).fetchall()
                promise_ids = [r["promise_id"] for r in promise_rows]

                # 紧急例外：累计下单不得超过数量上限，且门店/期限在下单时刻重新校验。
                exception_id = campaign["exception_id"]
                if exception_id:
                    exception = connection.execute(
                        "SELECT * FROM cp_emergency_exceptions WHERE exception_id=? AND status='approved'",
                        (exception_id,),
                    ).fetchone()
                    if exception is None:
                        raise StateConflict("紧急例外未批准，不能下单")
                    if exception["store_id"] != store_id:
                        raise PermissionDenied("订单门店不在紧急例外限定门店内")
                    if not exception["valid_from"] <= order_date <= exception["valid_to"]:
                        raise StateConflict("下单日期不在紧急例外期限内")
                    used = connection.execute(
                        "SELECT COALESCE(SUM(cases),0) AS total FROM cp_orders "
                        "WHERE exception_id=?",
                        (exception_id,),
                    ).fetchone()["total"]
                    if Decimal(str(used)) + cases_dec > Decimal(exception["max_cases"]):
                        raise BudgetExceeded("累计下单数量超过紧急例外数量上限")

                # 以「下单日期」重新解析规则并固化快照。
                result = self._evaluate_campaign(
                    connection, brand_id=campaign["brand_id"], channel=campaign["channel"],
                    region_id=campaign["region_id"], store_id=store_id,
                    product_id=campaign["product_id"], cases_dec=cases_dec,
                    activity_date=order_date, promise_ids=promise_ids,
                    exception_id=exception_id, self_campaign_id=campaign_id)
                blockers = [c for c in result["conflicts"] if c["severity"] == "blocker"]
                if blockers:
                    raise StateConflict(f"下单时刻规则核算存在阻断冲突: {blockers}")

                snapshot = {"ordered_at": self._now(), "order_date": order_date,
                            "resolution": result["resolution"],
                            "promise_ids": promise_ids}
                evaluation_json = json_ready(result["evaluation"])
                connection.execute(
                    "INSERT INTO cp_orders(order_id,campaign_id,exception_id,store_id,product_id,"
                    "cases,order_date,status,snapshot_json,evaluation_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?, 'booked',?,?,?,?)",
                    (order_id, campaign_id, exception_id, store_id, campaign["product_id"],
                     str(cases_dec), order_date, canonical_json(snapshot),
                     canonical_json(evaluation_json), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="order.booked",
                            resource_type="order", resource_id=order_id,
                            detail={"campaign_id": campaign_id, "snapshot": snapshot["resolution"]})
                return "order", order_id, {"order_id": order_id, "status": "booked",
                                           "snapshot": snapshot, "evaluation": evaluation_json}

            return self._idempotent(connection, request_id=request_id, action="book_order",
                                    payload=payload, create=create)

    def get_order(self, order_id: str) -> dict[str, Any]:
        """返回订单及其规则快照——历史订单永远引用下单时规则。"""

        row = self.connection.execute("SELECT * FROM cp_orders WHERE order_id=?",
                                      (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("订单不存在")
        return {"order_id": order_id, "campaign_id": row["campaign_id"],
                "exception_id": row["exception_id"], "store_id": row["store_id"],
                "product_id": row["product_id"], "cases": row["cases"],
                "order_date": row["order_date"], "status": row["status"],
                "snapshot": json.loads(row["snapshot_json"]),
                "evaluation": json.loads(row["evaluation_json"]),
                "created_by": row["created_by"], "created_at": row["created_at"]}

    # ----------------------------------------------------------------- 结算

    def post_settlement_event(self, *, request_id: str, actor_id: str, event_id: str,
                              order_id: str, promise_id: str, event_type: str, cases: Any,
                              amount: Any, occurrence_date: str) -> dict[str, Any]:
        """回传核销/退货/无效凭证，归回原促销承诺，event_id 重复回传保持幂等。"""

        payload = {"actor_id": actor_id, "event_id": event_id, "order_id": order_id,
                   "promise_id": promise_id, "event_type": event_type, "cases": str(cases),
                   "amount": str(amount), "occurrence_date": occurrence_date}
        with self.tx() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, CREATOR_ROLES)
            event_id = self._id(event_id, "event_id")
            order_id = self._id(order_id, "order_id")
            promise_id = self._id(promise_id, "promise_id")
            occurrence_date = self._date(occurrence_date, "occurrence_date")
            if event_type not in {"redemption", "return", "invalid_voucher"}:
                raise ValidationError("event_type 不合法")
            cases_dec = Decimal(str(cases)).quantize(Decimal("0.001"))
            if cases_dec <= 0:
                raise ValidationError("结算数量必须为正")
            amount_dec = money(amount)
            if amount_dec <= 0:
                raise ValidationError("结算金额必须为正（方向由事件类型决定）")

            def create():
                # event_id 是结算回传的天然幂等键：即便对端更换 request_id 重试，
                # 同一 event_id 也绝不重复入账。
                existing_event = connection.execute(
                    "SELECT * FROM cp_settlement_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if existing_event is not None:
                    if existing_event["payload_hash"] != digest(payload):
                        raise ConflictError("event_id 已被不同结算内容使用")
                    return ("settlement_event", event_id,
                            {"event_id": event_id, "event_type": existing_event["event_type"],
                             "duplicate_event": True})
                order = connection.execute("SELECT * FROM cp_orders WHERE order_id=?",
                                           (order_id,)).fetchone()
                if order is None:
                    raise NotFoundError("订单不存在")
                snapshot = json.loads(order["snapshot_json"])
                if promise_id not in snapshot["promise_ids"]:
                    raise ValidationError("结算费用必须归属订单引用的原促销承诺")

                def qty(kind: str) -> Decimal:
                    row = connection.execute(
                        "SELECT COALESCE(SUM(cases),0) AS total FROM cp_settlement_events "
                        "WHERE order_id=? AND promise_id=? AND event_type=?",
                        (order_id, promise_id, kind),
                    ).fetchone()
                    return Decimal(str(row["total"]))

                redeemed = qty("redemption")
                returned = qty("return")
                invalided = qty("invalid_voucher")
                order_cases = Decimal(order["cases"])
                if event_type == "redemption":
                    if redeemed + cases_dec > order_cases:
                        raise StateConflict("核销累计数量超过订单数量")
                elif event_type == "return":
                    if returned + cases_dec > redeemed:
                        raise StateConflict("退货数量超过已核销数量")
                else:  # invalid_voucher：无效凭证只能冲销净核销
                    if invalided + cases_dec > redeemed - returned:
                        raise StateConflict("无效凭证数量超过可冲销的净核销数量")

                payload_hash = digest(payload)
                connection.execute(
                    "INSERT INTO cp_settlement_events(event_id,order_id,promise_id,event_type,"
                    "cases,amount,occurrence_date,payload_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id, order_id, promise_id, event_type, str(cases_dec),
                     str(amount_dec), occurrence_date, payload_hash, actor_id, self._now()),
                )

                # 预算台账：核销占用费用并按数量释放对应预留；退货/无效凭证冲回预算。
                if event_type == "redemption":
                    reserve_row = connection.execute(
                        "SELECT amount_money, amount_cases FROM cp_budget_ledger "
                        "WHERE campaign_id=? AND promise_id=? AND entry_type='reserve'",
                        (order["campaign_id"], promise_id),
                    ).fetchone()
                    released_row = connection.execute(
                        "SELECT COALESCE(SUM(amount_cases),0) AS cases, "
                        "COALESCE(SUM(amount_money),0) AS money FROM cp_budget_ledger "
                        "WHERE campaign_id=? AND promise_id=? AND entry_type='release_reserve'",
                        (order["campaign_id"], promise_id),
                    ).fetchone()
                    remaining_reserve_cases = (Decimal(reserve_row["amount_cases"])
                                               - Decimal(str(released_row["cases"])))
                    release_cases = min(cases_dec, remaining_reserve_cases)
                    if release_cases > 0 and reserve_row is not None:
                        per_case = (Decimal(reserve_row["amount_money"])
                                    / Decimal(reserve_row["amount_cases"]))
                        release_money = money(per_case * release_cases)
                    else:
                        release_money = Decimal("0.00")
                    self._insert_ledger(connection, promise_id=promise_id,
                                        campaign_id=order["campaign_id"], order_id=order_id,
                                        event_id=event_id, entry_type="release_reserve",
                                        cases=release_cases, amount=-release_money)
                    self._insert_ledger(connection, promise_id=promise_id,
                                        campaign_id=order["campaign_id"], order_id=order_id,
                                        event_id=event_id, entry_type="redeem",
                                        cases=cases_dec, amount=amount_dec)
                    # 硬不变量：承诺净占用永远不允许超过费用上限。
                    cap_row = connection.execute(
                        "SELECT budget_cap FROM cp_promises WHERE promise_id=?", (promise_id,)
                    ).fetchone()
                    used = self._used(connection, promise_id)
                    if used > Decimal(cap_row["budget_cap"]):
                        raise BudgetExceeded(
                            f"核销后承诺 {promise_id} 净占用 {used} 超过费用上限 "
                            f"{cap_row['budget_cap']}")
                else:
                    entry_type = "return" if event_type == "return" else "invalid"
                    self._insert_ledger(connection, promise_id=promise_id,
                                        campaign_id=order["campaign_id"], order_id=order_id,
                                        event_id=event_id, entry_type=entry_type,
                                        cases=cases_dec, amount=-amount_dec)

                self._audit(connection, actor_id=actor_id,
                            action=f"settlement.{event_type}",
                            resource_type="settlement_event", resource_id=event_id,
                            detail={"order_id": order_id, "promise_id": promise_id,
                                    "cases": str(cases_dec), "amount": str(amount_dec)})
                return "settlement_event", event_id, {"event_id": event_id,
                                                       "event_type": event_type}

            return self._idempotent(connection, request_id=request_id,
                                    action="post_settlement_event", payload=payload,
                                    create=create)

    def _insert_ledger(self, connection, *, promise_id: str, campaign_id: str, order_id: str,
                       event_id: str | None, entry_type: str, cases: Decimal,
                       amount: Decimal) -> None:
        connection.execute(
            "INSERT INTO cp_budget_ledger(ledger_id,promise_id,campaign_id,order_id,event_id,"
            "entry_type,amount_cases,amount_money,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, promise_id, campaign_id, order_id, event_id, entry_type,
             str(cases), str(money(amount)), self._now()),
        )

    def _used(self, connection, promise_id: str,
              exclude_campaign_id: str | None = None) -> Decimal:
        if exclude_campaign_id is not None:
            row = connection.execute(
                "SELECT COALESCE(SUM(amount_money),0) AS total FROM cp_budget_ledger "
                "WHERE promise_id=? AND COALESCE(campaign_id,'') <> ?",
                (promise_id, exclude_campaign_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COALESCE(SUM(amount_money),0) AS total FROM cp_budget_ledger "
                "WHERE promise_id=?",
                (promise_id,),
            ).fetchone()
        return Decimal(str(row["total"]))

    def remaining_budget(self, promise_id: str) -> dict[str, Any]:
        """返回承诺费用上限、净占用与剩余额度。"""

        row = self.connection.execute(
            "SELECT * FROM cp_promises WHERE promise_id=?", (promise_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("促销承诺不存在")
        used = self._used(self.connection, promise_id)
        cap = Decimal(row["budget_cap"])
        return {"promise_id": promise_id, "budget_cap": str(cap), "used": str(money(used)),
                "remaining": str(money(cap - used)), "status": row["status"],
                "content_hash": row["content_hash"], "approved_by": row["approved_by"]}

    def trace_expense(self, event_id: str) -> dict[str, Any]:
        """从一笔结算费用追查到：费用事件 → 原促销承诺批准版本 → 订单规则快照 → 剩余额度。"""

        event = self.connection.execute(
            "SELECT * FROM cp_settlement_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if event is None:
            raise NotFoundError("结算事件不存在")
        promise = self.connection.execute(
            "SELECT * FROM cp_promises WHERE promise_id=?", (event["promise_id"],)
        ).fetchone()
        order = self.connection.execute(
            "SELECT * FROM cp_orders WHERE order_id=?", (event["order_id"],)
        ).fetchone()
        ledger = [
            {"ledger_id": row["ledger_id"], "entry_type": row["entry_type"],
             "campaign_id": row["campaign_id"], "order_id": row["order_id"],
             "event_id": row["event_id"], "amount_cases": row["amount_cases"],
             "amount_money": row["amount_money"], "created_at": row["created_at"]}
            for row in self.connection.execute(
                "SELECT * FROM cp_budget_ledger WHERE event_id=? OR campaign_id=? "
                "ORDER BY rowid",
                (event_id, order["campaign_id"]))
        ]
        budget = self.remaining_budget(event["promise_id"])
        return {
            "event": {"event_id": event_id, "order_id": event["order_id"],
                      "promise_id": event["promise_id"], "event_type": event["event_type"],
                      "cases": event["cases"], "amount": event["amount"],
                      "occurrence_date": event["occurrence_date"],
                      "payload_hash": event["payload_hash"]},
            "promise_approval": {"promise_id": event["promise_id"],
                                 "content_hash": promise["content_hash"],
                                 "status": promise["status"],
                                 "approved_by": promise["approved_by"],
                                 "approved_at": promise["approved_at"],
                                 "fund_type": promise["fund_type"],
                                 "budget_cap": promise["budget_cap"]},
            "order": {"order_id": event["order_id"],
                      "campaign_id": order["campaign_id"],
                      "order_date": order["order_date"],
                      "snapshot": json.loads(order["snapshot_json"])},
            "budget": budget,
            "ledger_entries": ledger,
        }

    def list_settlement_events(self, order_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM cp_settlement_events WHERE order_id=? ORDER BY rowid", (order_id,)
        ).fetchall()
        return [{"event_id": row["event_id"], "promise_id": row["promise_id"],
                 "event_type": row["event_type"], "cases": row["cases"],
                 "amount": row["amount"], "occurrence_date": row["occurrence_date"]}
                for row in rows]


def json_ready(value: Any) -> Any:
    """把 Decimal 等转为可 JSON 序列化的形式。"""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value
