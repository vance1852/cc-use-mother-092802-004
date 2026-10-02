"""渠道价盘与促销治理的离线端到端验收。

覆盖一条完整业务链：
1. 按生效期保存产品层级/生命周期、区域底价、渠道合同、促销承诺与费用上限；
2. 活动提交计算全部优惠后的实际条件并给出冲突来源（击穿底价、培育费错配、未放行叠加）；
3. 紧急例外限定门店/数量/期限，由另一角色复核；
4. 生效订单固化下单时规则快照，后续追加新版本不重写历史；
5. 结算把核销/退货/无效凭证归回原促销承诺，重复回传幂等；
6. 财务从一笔费用追查到批准版本、订单与剩余额度。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from channel_pricing.errors import (
    GovernanceError,
    PermissionDenied,
    PriceFloorBreached,
    StateConflict,
)
from channel_pricing.service import GovernanceService


def run() -> dict[str, object]:
    checks: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "governance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="示范酒业集团")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                            display_name="销售经理", role="operator", organization_id="o1")
        base.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                            display_name="渠道复核", role="reviewer", organization_id="o1")
        base.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                            display_name="财务审计", role="auditor", organization_id="o1")

        service = GovernanceService(database, clock)

        # ---- 主数据：高端品牌、区域、商超门店、两个产品（成熟高端 + 培育次高档）----
        service.register_brand(request_id="brand", actor_id="op1", brand_id="b1", name="云岭")
        service.register_region(request_id="region", actor_id="op1", region_id="east", name="华东")
        service.register_store(request_id="store", actor_id="op1", store_id="st1",
                               region_id="east", channel="supermarket", name="华东一号店")
        service.register_product(request_id="p-mature", actor_id="op1", product_id="p1",
                                 brand_id="b1", sku="YL-H1", name="云岭珍藏")
        service.register_product(request_id="p-cult", actor_id="op1", product_id="p2",
                                 brand_id="b1", sku="YL-S1", name="云岭醇酿")

        # ---- 生效期版本：层级 / 生命周期 / 挂牌价，双人复核后生效 ----
        service.create_product_version(request_id="pv-mature", actor_id="op1", product_id="p1",
                                       tier="premium", lifecycle_stage="maturity",
                                       list_price_per_case="1000", effective_from="2026-01-01")
        pv_cult = service.create_product_version(request_id="pv-cult", actor_id="op1", product_id="p2",
                                                 tier="sub_premium", lifecycle_stage="cultivation",
                                                 list_price_per_case="600", effective_from="2026-01-01")
        # 创建人不能批准自己的版本。
        try:
            service.approve_product_version(request_id="pv-self", actor_id="op1",
                                            version_id=pv_cult["resource_id"])
            raise AssertionError("创建人不应能批准自己的版本")
        except PermissionDenied:
            checks["self_approval_blocked"] = True

        # 用查询不到 id 的方式直接列出再批准：这里通过返回值拿到版本 id。
        # （p1 的版本 id 通过数据库辅助取得）
        row = database.connection.execute(
            "SELECT version_id FROM cp_product_versions WHERE product_id='p1'").fetchone()
        service.approve_product_version(request_id="pv-mature-a", actor_id="rv1",
                                        version_id=row["version_id"])
        service.approve_product_version(request_id="pv-cult-a", actor_id="rv1",
                                        version_id=pv_cult["resource_id"])

        # 已生效版本不能被修改：只能追加新版本。
        try:
            service.approve_product_version(request_id="pv-mature-b", actor_id="rv1",
                                            version_id=row["version_id"])
            raise AssertionError("已生效版本不应能重复批准")
        except StateConflict:
            checks["effective_version_immutable"] = True

        # ---- 区域底价 ----
        service.create_floor_version(request_id="fv1", actor_id="op1", product_id="p1",
                                     region_id="east", floor_price_per_case="850",
                                     effective_from="2026-01-01")
        frow = database.connection.execute(
            "SELECT floor_id FROM cp_floor_versions WHERE product_id='p1'").fetchone()
        service.approve_floor_version(request_id="fv1a", actor_id="rv1",
                                      floor_id=frow["floor_id"])

        # ---- 渠道合同：商超综合折扣率上限 12% ----
        service.create_contract(request_id="ct1", actor_id="op1", brand_id="b1",
                                channel="supermarket", max_discount_rate="0.12",
                                effective_from="2026-01-01",
                                terms={"payment_days": 30})
        crow = database.connection.execute(
            "SELECT contract_id FROM cp_contracts WHERE brand_id='b1'").fetchone()
        service.approve_contract(request_id="ct1a", actor_id="rv1",
                                 contract_id=crow["contract_id"])

        # ---- 促销承诺：普通折让 + 培育专项费 ----
        service.create_promise(
            request_id="pr1", actor_id="op1", promise_id="promo-off", brand_id="b1",
            name="商超票面折让", channel="supermarket", fund_type="general",
            start_date="2026-09-01", end_date="2026-12-31", budget_cap="100000",
            components=[{"type": "off_invoice", "amount_per_case": "100"}])
        service.approve_promise(request_id="pr1a", actor_id="rv1", promise_id="promo-off")

        service.create_promise(
            request_id="pr2", actor_id="op1", promise_id="promo-cult", brand_id="b1",
            name="培育期陈列费", channel="supermarket", fund_type="cultivation",
            start_date="2026-09-01", end_date="2026-12-31", budget_cap="20000",
            components=[{"type": "cultivation", "amount_per_case": "20"}])
        service.approve_promise(request_id="pr2a", actor_id="rv1", promise_id="promo-cult")

        # ===== 场景 A：成熟高端产品误用培育专项费 + 两个承诺未放行叠加 =====
        bad = service.submit_campaign(
            request_id="camp-bad", actor_id="op1", brand_id="b1", channel="supermarket",
            region_id="east", product_id="p1", cases="10", activity_date="2026-10-05",
            promise_ids=["promo-off", "promo-cult"], store_id="st1")
        codes = {c["code"] for c in bad["conflicts"]}
        assert "CULTIVATION_FUND_MISMATCH" in codes, codes
        assert "STACK_NOT_ALLOWED" in codes, codes
        checks["cultivation_mismatch_and_stack_detected"] = True
        # 存在阻断冲突时不能批准。
        try:
            service.approve_campaign(request_id="camp-bad-a", actor_id="rv1",
                                     campaign_id=bad["resource_id"])
            raise AssertionError("存在冲突的活动不应能批准")
        except StateConflict:
            checks["blocked_campaign_rejected"] = True

        # ===== 场景 B：放行叠加后，折让过大击穿区域底价（1000-200=800 < 850）=====
        # 给成熟产品配一个普通兑付返利承诺（general 费用），与票面折让叠加。
        service.create_promise(
            request_id="pr3", actor_id="op1", promise_id="promo-bb", brand_id="b1",
            name="商超兑付返利", channel="supermarket", fund_type="general",
            start_date="2026-09-01", end_date="2026-12-31", budget_cap="50000",
            components=[{"type": "billback", "amount_per_case": "100"}])
        service.approve_promise(request_id="pr3a", actor_id="rv1", promise_id="promo-bb")
        service.add_stack_rule(request_id="sr1", actor_id="op1",
                               promise_id_a="promo-off", promise_id_b="promo-bb")
        breach = service.submit_campaign(
            request_id="camp-breach", actor_id="op1", brand_id="b1", channel="supermarket",
            region_id="east", product_id="p1", cases="10", activity_date="2026-10-05",
            promise_ids=["promo-off", "promo-bb"], store_id="st1")
        bcodes = {c["code"] for c in breach["conflicts"]}
        assert "PRICE_FLOOR_BREACH" in bcodes, bcodes
        breach_conflict = next(c for c in breach["conflicts"] if c["code"] == "PRICE_FLOOR_BREACH")
        # 击穿来源必须指向具体优惠承诺。
        assert set(breach_conflict["sources"]) == {"promo-off", "promo-bb"}, breach_conflict
        assert breach["evaluation"]["net_price_per_case"] == "800.00"
        checks["floor_breach_sources_reported"] = True
        try:
            service.approve_campaign(request_id="camp-breach-a", actor_id="rv1",
                                     campaign_id=breach["resource_id"])
            raise AssertionError("击穿底价的活动不应能批准")
        except PriceFloorBreached:
            checks["floor_breach_blocks_approval"] = True

        # ===== 场景 C：合规活动（单承诺、折让 100，净价 900 >= 850）=====
        ok = service.submit_campaign(
            request_id="camp-ok", actor_id="op1", brand_id="b1", channel="supermarket",
            region_id="east", product_id="p1", cases="10", activity_date="2026-10-05",
            promise_ids=["promo-off"], store_id="st1")
        assert not ok["has_blocker"], ok["conflicts"]
        assert ok["evaluation"]["net_price_per_case"] == "900.00"
        service.approve_campaign(request_id="camp-ok-a", actor_id="rv1",
                                 campaign_id=ok["resource_id"])
        # 提交人不能批准自己的活动。
        checks["clean_campaign_approved"] = True
        campaign_id = ok["resource_id"]

        # 批准后费用被预留。
        budget_after_reserve = service.remaining_budget("promo-off")
        assert budget_after_reserve["used"] == "1000.00", budget_after_reserve
        assert budget_after_reserve["remaining"] == "99000.00", budget_after_reserve
        checks["budget_reserved_on_approval"] = True

        # ===== 场景 D：紧急例外必须限定门店、数量、期限并双人复核 =====
        # 独立的紧急渠道承诺，避免与其它活动的预算混用。
        service.create_promise(
            request_id="pr4", actor_id="op1", promise_id="promo-emg", brand_id="b1",
            name="商超紧急应对折让", channel="supermarket", fund_type="general",
            start_date="2026-09-01", end_date="2026-12-31", budget_cap="10000",
            components=[{"type": "off_invoice", "amount_per_case": "50"}])
        service.approve_promise(request_id="pr4a", actor_id="rv1", promise_id="promo-emg")
        exc = service.request_emergency_exception(
            request_id="exc1", actor_id="op1", promise_id="promo-emg", product_id="p1",
            store_id="st1", max_cases="5", amount_per_case="30",
            valid_from="2026-10-10", valid_to="2026-10-12", reason="竞品临时促销应对")
        # 申请人自己不能复核。
        try:
            service.review_emergency_exception(request_id="exc-self", actor_id="op1",
                                               exception_id=exc["resource_id"],
                                               decision="approved")
            raise AssertionError("申请人不应能复核自己的例外")
        except PermissionDenied:
            checks["emergency_separation_of_duty"] = True
        service.review_emergency_exception(request_id="exc1a", actor_id="rv1",
                                           exception_id=exc["resource_id"],
                                           decision="approved", review_note="同意限时应对")

        # 用例外提交活动并批准、下单。活动 5 箱，恰为例外上限。
        exc_camp = service.submit_campaign(
            request_id="camp-exc", actor_id="op1", brand_id="b1", channel="supermarket",
            region_id="east", product_id="p1", cases="5", activity_date="2026-10-11",
            promise_ids=["promo-emg"], store_id="st1",
            exception_id=exc["resource_id"])
        assert not exc_camp["has_blocker"], exc_camp["conflicts"]
        # 票面 50 + 例外 30 = 80 折让，净价 920 >= 底价 850。
        assert exc_camp["evaluation"]["net_price_per_case"] == "920.00", \
            exc_camp["evaluation"]["net_price_per_case"]
        service.approve_campaign(request_id="camp-exc-a", actor_id="rv1",
                                 campaign_id=exc_camp["resource_id"])
        service.book_order(request_id="ord-exc", actor_id="op1", order_id="o-exc",
                           campaign_id=exc_camp["resource_id"], store_id="st1",
                           cases="5", order_date="2026-10-11")

        # 第二个活动同样 5 箱、自身尚有额度，但同一例外已累计用满 5 箱，不能再下单。
        exc_camp2 = service.submit_campaign(
            request_id="camp-exc2", actor_id="op1", brand_id="b1", channel="supermarket",
            region_id="east", product_id="p1", cases="5", activity_date="2026-10-12",
            promise_ids=["promo-emg"], store_id="st1",
            exception_id=exc["resource_id"])
        service.approve_campaign(request_id="camp-exc2-a", actor_id="rv1",
                                 campaign_id=exc_camp2["resource_id"])
        try:
            service.book_order(request_id="ord-exc2", actor_id="op1", order_id="o-exc2",
                               campaign_id=exc_camp2["resource_id"], store_id="st1",
                               cases="1", order_date="2026-10-12")
            raise AssertionError("超过紧急例外跨订单累计数量上限不应能下单")
        except Exception as e:
            assert e.code == "budget_exceeded", e.code
            checks["emergency_quantity_enforced"] = True
        # 期限外不能下单。
        try:
            service.book_order(request_id="ord-exc3", actor_id="op1", order_id="o-exc3",
                               campaign_id=exc_camp["resource_id"], store_id="st1",
                               cases="1", order_date="2026-10-13")
            raise AssertionError("期限外不应能下单")
        except StateConflict:
            checks["emergency_period_enforced"] = True
        # 限定门店：其它门店不能使用该例外（活动门店限定与例外门店限定双重拦截）。
        service.register_region(request_id="region-n", actor_id="op1", region_id="north", name="华北")
        service.register_store(request_id="store-n", actor_id="op1", store_id="st2",
                               region_id="north", channel="supermarket", name="华北一号店")
        try:
            service.book_order(request_id="ord-exc4", actor_id="op1", order_id="o-exc4",
                               campaign_id=exc_camp["resource_id"], store_id="st2",
                               cases="1", order_date="2026-10-11")
            raise AssertionError("非例外限定门店不应能下单")
        except GovernanceError:
            checks["emergency_store_enforced"] = True

        # ===== 场景 E：合规订单固化规则快照，后续调价不重写历史 =====
        service.book_order(request_id="ord1", actor_id="op1", order_id="o1",
                           campaign_id=campaign_id, store_id="st1",
                           cases="10", order_date="2026-10-06")
        order_before = service.get_order("o1")
        snapshot_version = order_before["snapshot"]["resolution"]["product_version_id"]
        net_before = order_before["evaluation"]["net_price_per_case"]
        assert net_before == "900.00", net_before

        # 后续追加新挂牌价版本（涨价），历史订单净价不变。
        service.create_product_version(request_id="pv3", actor_id="op1", product_id="p1",
                                       tier="premium", lifecycle_stage="maturity",
                                       list_price_per_case="1200", effective_from="2026-10-20")
        pv3 = database.connection.execute(
            "SELECT version_id FROM cp_product_versions WHERE product_id='p1' "
            "ORDER BY effective_from DESC, rowid DESC LIMIT 1").fetchone()
        service.approve_product_version(request_id="pv3a", actor_id="rv1",
                                        version_id=pv3["version_id"])
        order_after = service.get_order("o1")
        assert order_after["evaluation"]["net_price_per_case"] == net_before
        assert order_after["snapshot"]["resolution"]["product_version_id"] == snapshot_version
        checks["order_snapshot_immutable_after_repricing"] = True

        # ===== 场景 F：结算——核销、退货、无效凭证归回原承诺，重复回传幂等 =====
        # 核销 10 箱（金额 = 票面折让 100/箱 × 10 = 1000）。
        redeem = service.post_settlement_event(
            request_id="set1", actor_id="op1", event_id="ev1", order_id="o1",
            promise_id="promo-off", event_type="redemption", cases="10",
            amount="1000", occurrence_date="2026-10-07")
        assert not redeem["replayed"]
        redeem_replay = service.post_settlement_event(
            request_id="set1", actor_id="op1", event_id="ev1", order_id="o1",
            promise_id="promo-off", event_type="redemption", cases="10",
            amount="1000", occurrence_date="2026-10-07")
        assert redeem_replay["replayed"]
        checks["settlement_idempotent"] = True

        # 核销后预算：预留被释放，实际占用 1000，剩余额度仍为 99000。
        budget_after_redeem = service.remaining_budget("promo-off")
        assert budget_after_redeem["used"] == "1000.00", budget_after_redeem
        assert budget_after_redeem["remaining"] == "99000.00", budget_after_redeem
        checks["budget_realized_after_redemption"] = True

        # 退货 3 箱，金额冲回 300。
        service.post_settlement_event(
            request_id="set2", actor_id="op1", event_id="ev2", order_id="o1",
            promise_id="promo-off", event_type="return", cases="3",
            amount="300", occurrence_date="2026-10-08")
        # 退货不能超过已核销。
        try:
            service.post_settlement_event(
                request_id="set2x", actor_id="op1", event_id="ev2x", order_id="o1",
                promise_id="promo-off", event_type="return", cases="99",
                amount="9900", occurrence_date="2026-10-08")
            raise AssertionError("退货不应超过已核销数量")
        except StateConflict:
            checks["return_cannot_exceed_redeemed"] = True

        # 1 箱凭证无效，再冲回 100（净核销 10-3-1=6 箱）。
        service.post_settlement_event(
            request_id="set3", actor_id="op1", event_id="ev3", order_id="o1",
            promise_id="promo-off", event_type="invalid_voucher", cases="1",
            amount="100", occurrence_date="2026-10-09")
        budget_final = service.remaining_budget("promo-off")
        # 净占用 = 1000(核销) - 300(退货) - 100(无效) = 600
        assert budget_final["used"] == "600.00", budget_final
        assert budget_final["remaining"] == "99400.00", budget_final
        checks["returns_and_invalid_release_budget"] = True

        # ===== 场景 G：财务从一笔费用追查到批准版本、订单与剩余额度 =====
        trace = service.trace_expense("ev3")
        assert trace["event"]["event_id"] == "ev3"
        assert trace["promise_approval"]["promise_id"] == "promo-off"
        assert trace["promise_approval"]["approved_by"] == "rv1"
        assert trace["promise_approval"]["content_hash"]
        assert trace["order"]["order_id"] == "o1"
        assert trace["order"]["snapshot"]["resolution"]["product_version_id"] == snapshot_version
        assert trace["budget"]["remaining"] == "99400.00"
        entry_types = {e["entry_type"] for e in trace["ledger_entries"]}
        assert {"reserve", "release_reserve", "redeem", "return", "invalid"} <= entry_types, entry_types
        checks["financial_traceability_complete"] = True

        # 审计链完整。
        valid, event_count = base.verify_audit()
        assert valid
        checks["audit_chain_valid"] = True
        checks["audit_events"] = event_count
        checks["status"] = "ok"
        database.close()
        return checks


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("status") == "ok" and result.get("audit_chain_valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
