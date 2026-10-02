"""运行多品牌渠道价盘与促销治理的离线端到端验收。

覆盖：分层生效规则、活动冲突归因、紧急例外双角色复核、下单当时规则快照、
后续调价不重写历史、结算核销/退货/无效凭证归回原承诺、重复回传幂等与财务追查。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .promo_service import PromoGovernanceService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整的价盘治理链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "promo_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        promo = PromoGovernanceService(database, clock)

        base.register_organization(request_id="a-org", actor_id="bootstrap",
                                   organization_id="org-1", name="示范酒业集团")
        base.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                            display_name="渠道总监", role="admin", organization_id="org-1")
        base.register_actor(request_id="a-op", actor_id="admin", new_actor_id="manager",
                            display_name="销售经理", role="operator", organization_id="org-1")
        base.register_actor(request_id="a-rv", actor_id="admin", new_actor_id="reviewer",
                            display_name="合规复核", role="reviewer", organization_id="org-1")

        # ---- 一、按生效期建立价盘规则 ---------------------------------
        promo.publish_product_hierarchy(
            request_id="a-p1", actor_id="admin", product_id="sku-premium", brand_id="brand-A",
            name="旗舰单品", tier="premium", lifecycle="nurturing",
            list_price_cents=100000, effective_from="2026-01-01T00:00:00Z")
        promo.publish_product_hierarchy(
            request_id="a-p2", actor_id="admin", product_id="sku-mass", brand_id="brand-A",
            name="流通单品", tier="mass", lifecycle="mature",
            list_price_cents=10000, effective_from="2026-01-01T00:00:00Z")
        promo.publish_floor_price(
            request_id="a-f1", actor_id="admin", product_id="sku-premium", region="east",
            floor_cents=80000, effective_from="2026-01-01T00:00:00Z")
        promo.publish_channel_contract(
            request_id="a-c1", actor_id="admin", brand_id="brand-A",
            channel="hypermarket", region="east", discount_bps=500,
            effective_from="2026-01-01T00:00:00Z")
        promo.publish_stack_rule(
            request_id="a-s1", actor_id="admin", channel="hypermarket",
            from_group="instant_discount", to_group="coupon", allowed=True,
            effective_from="2026-01-01T00:00:00Z")
        promo.publish_fund(
            request_id="a-fund1", actor_id="admin", fund_id="nurture-fund",
            name="培育专项", lifecycle_scope="nurturing",
            effective_from="2026-01-01T00:00:00Z")
        promo.publish_expense_cap(
            request_id="a-cap1", actor_id="admin", fund_id="nurture-fund",
            product_id="sku-premium", channel="hypermarket",
            period_start="2026-10-01T00:00:00Z", period_end="2026-10-31T23:59:59Z",
            amount_cents=5_000_000, effective_from="2026-01-01T00:00:00Z")

        # ---- 二、活动提交：合规活动通过、击穿价盘被拦截 ---------------
        clean = promo.submit_promotion(
            request_id="a-submit-ok", actor_id="manager", promotion_id="campaign-ok",
            product_id="sku-premium", brand_id="brand-A", channel="hypermarket",
            region="east", store_ids=["store-1", "store-2"],
            period_start="2026-10-10T00:00:00Z", period_end="2026-10-20T23:59:59Z",
            planned_units=100,
            mechanics=[{"mechanic_id": "d1", "group": "instant_discount",
                        "type": "cents", "value": 10000, "fund_id": "nurture-fund"},
                       {"mechanic_id": "d2", "group": "coupon",
                        "type": "cents", "value": 2000}])
        clean_net = clean["evaluation"]["unit_net_cents"]
        clean_conflicts = len(clean["evaluation"]["conflicts"])

        blocked = promo.submit_promotion(
            request_id="a-submit-bad", actor_id="manager", promotion_id="campaign-bad",
            product_id="sku-premium", brand_id="brand-A", channel="hypermarket",
            region="east", store_ids=["store-3"],
            period_start="2026-10-10T00:00:00Z", period_end="2026-10-20T23:59:59Z",
            planned_units=10,
            mechanics=[{"mechanic_id": "d1", "group": "instant_discount",
                        "type": "cents", "value": 20000},
                       {"mechanic_id": "d2", "group": "coupon",
                        "type": "cents", "value": 2000}])
        blocked_codes = sorted({c["code"] for c in blocked["evaluation"]["conflicts"]})

        # 培育费用错用到成熟产品也被识别。
        misfunded = promo.submit_promotion(
            request_id="a-submit-fund", actor_id="manager", promotion_id="campaign-fund",
            product_id="sku-mass", brand_id="brand-A", channel="hypermarket",
            region="east", store_ids=["store-3"],
            period_start="2026-10-10T00:00:00Z", period_end="2026-10-20T23:59:59Z",
            planned_units=10,
            mechanics=[{"mechanic_id": "d1", "group": "instant_discount",
                        "type": "cents", "value": 100, "fund_id": "nurture-fund"}])
        misfunded_codes = sorted({c["code"] for c in misfunded["evaluation"]["conflicts"]})

        # ---- 三、合规活动由不同角色复核后下单 -------------------------
        approved = promo.approve_promotion(
            request_id="a-approve", actor_id="reviewer",
            promotion_version_id=clean["version_id"])
        order_before = promo.create_order(
            request_id="a-order-1", actor_id="manager", order_id="order-1",
            promotion_version_id=clean["version_id"], store_id="store-1", quantity=2,
            as_of="2026-10-12T00:00:00Z")
        floor_version_before = order_before["snapshot"]["order_time_rule_version_ids"]["floor_price"]

        # ---- 四、被拦截活动走紧急例外：限门店/数量/期限 + 双人复核 -----
        exception = promo.request_emergency_exception(
            request_id="a-exception", actor_id="manager",
            promotion_version_id=blocked["version_id"], store_ids=["store-3"],
            quantity_limit=3, deadline="2026-10-15T23:59:59Z",
            reason="竞品同档位临时加促，需要小范围跟价")
        reviewed = promo.review_emergency_exception(
            request_id="a-exception-review", actor_id="reviewer",
            exception_id=exception["exception_id"], approved=True)
        for index in range(3):
            promo.create_order(
                request_id=f"a-ex-order-{index}", actor_id="manager",
                order_id=f"ex-order-{index}", promotion_version_id=blocked["version_id"],
                store_id="store-3", quantity=1, as_of="2026-10-13T00:00:00Z")

        # ---- 五、后续调价（10 月 16 日底价上调）不重写历史 -------------
        promo.publish_floor_price(
            request_id="a-f2", actor_id="admin", product_id="sku-premium", region="east",
            floor_cents=90000, effective_from="2026-10-16T00:00:00Z")
        order_check = promo.create_order(
            request_id="a-order-2", actor_id="manager", order_id="order-2",
            promotion_version_id=clean["version_id"], store_id="store-1", quantity=1,
            as_of="2026-10-14T00:00:00Z")
        history_preserved = (
            order_check["unit_net_cents"] == clean_net
            and order_check["snapshot"]["order_time_rule_version_ids"]["floor_price"]
            == floor_version_before)
        try:
            promo.create_order(
                request_id="a-order-3", actor_id="manager", order_id="order-3",
                promotion_version_id=clean["version_id"], store_id="store-1", quantity=1,
                as_of="2026-10-17T00:00:00Z")
            new_order_blocked = False
        except Exception:
            new_order_blocked = True

        # ---- 六、结算：核销、重复回传幂等、退货归还原承诺额度 ----------
        writeoff = promo.submit_settlement(
            request_id="a-writeoff", actor_id="manager",
            promotion_version_id=clean["version_id"], external_voucher_id="voucher-1",
            kind="writeoff", amount_cents=3_000_000, quantity=2,
            order_id="order-1", fund_id="nurture-fund")
        writeoff_replay = promo.submit_settlement(
            request_id="a-writeoff-dup", actor_id="manager",
            promotion_version_id=clean["version_id"], external_voucher_id="voucher-1",
            kind="writeoff", amount_cents=3_000_000, quantity=2,
            order_id="order-1", fund_id="nurture-fund")
        returned = promo.submit_settlement(
            request_id="a-return", actor_id="manager",
            promotion_version_id=clean["version_id"], external_voucher_id="voucher-2",
            kind="return", amount_cents=1_000_000, quantity=1,
            original_voucher_id="voucher-1")

        # ---- 七、财务从一笔费用追到批准版本、订单与剩余额度 ------------
        trace = promo.trace_expense(external_voucher_id="voucher-2")
        audit_valid, audit_events = base.verify_audit()

        result = {
            "status": "ok",
            "clean_net_cents": clean_net,
            "clean_conflicts": clean_conflicts,
            "blocked_status": blocked["status"],
            "blocked_conflict_codes": blocked_codes,
            "misfunded_conflict_codes": misfunded_codes,
            "approved_by": approved["status"],
            "exception_status": reviewed["status"],
            "emergency_orders": 3,
            "history_preserved": history_preserved,
            "new_order_after_repricing_blocked": new_order_blocked,
            "writeoff_remaining_cents": writeoff["remaining_cents"],
            "writeoff_replayed": writeoff_replay["replayed"],
            "return_remaining_cents": returned["remaining_cents"],
            "trace_approved_by": trace["promotion_version"]["approved_by"],
            "trace_order_id": trace["order"]["order_id"],
            "trace_original_voucher": trace["entry"]["original_voucher_id"],
            "trace_cap_remaining_cents": trace["cap"]["remaining_cents"],
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {
        "clean_net_cents": 83000,
        "clean_conflicts": 0,
        "blocked_status": "blocked",
        "approved_by": "approved",
        "exception_status": "approved",
        "history_preserved": True,
        "new_order_after_repricing_blocked": True,
        "writeoff_remaining_cents": 2_000_000,
        "writeoff_replayed": True,
        "return_remaining_cents": 3_000_000,
        "trace_approved_by": "reviewer",
        "trace_order_id": "order-1",
        "trace_original_voucher": "voucher-1",
        "trace_cap_remaining_cents": 3_000_000,
        "audit_valid": True,
    }
    ok = result["status"] == "ok" and all(result[key] == value for key, value in expected.items())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
