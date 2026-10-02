"""渠道价盘治理服务的集成测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from channel_pricing.errors import (
    BudgetExceeded,
    ConflictError,
    PermissionDenied,
    PriceFloorBreached,
    StateConflict,
    ValidationError,
)
from channel_pricing.service import GovernanceService


class GovernanceServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        base = DomainService(self.database, self.clock)
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="集团")
        base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="ad1",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="op", actor_id="ad1", new_actor_id="op1",
                            display_name="销售", role="operator", organization_id="o1")
        base.register_actor(request_id="rev", actor_id="ad1", new_actor_id="rv1",
                            display_name="复核", role="reviewer", organization_id="o1")
        base.register_actor(request_id="aud", actor_id="ad1", new_actor_id="au1",
                            display_name="审计", role="auditor", organization_id="o1")
        self.service = GovernanceService(self.database, self.clock)
        self.service.register_brand(request_id="brd", actor_id="op1", brand_id="b1", name="品牌")
        self.service.register_region(request_id="reg", actor_id="op1", region_id="r1", name="区域")
        self.service.register_store(request_id="sto", actor_id="op1", store_id="st1",
                                    region_id="r1", channel="restaurant", name="门店")
        self.service.register_product(request_id="prd", actor_id="op1", product_id="p1",
                                      brand_id="b1", sku="SKU1", name="产品")

    def tearDown(self):
        self.database.close()

    def _effective_rules(self, *, list_price="1000", floor="850", rate="0.2"):
        self.service.create_product_version(request_id="pv", actor_id="op1", product_id="p1",
                                            tier="premium", lifecycle_stage="maturity",
                                            list_price_per_case=list_price,
                                            effective_from="2026-01-01")
        pv = self.database.connection.execute(
            "SELECT version_id FROM cp_product_versions WHERE product_id='p1'").fetchone()
        self.service.approve_product_version(request_id="pva", actor_id="rv1",
                                             version_id=pv["version_id"])
        self.service.create_floor_version(request_id="fv", actor_id="op1", product_id="p1",
                                          region_id="r1", floor_price_per_case=floor,
                                          effective_from="2026-01-01")
        fv = self.database.connection.execute(
            "SELECT floor_id FROM cp_floor_versions WHERE product_id='p1'").fetchone()
        self.service.approve_floor_version(request_id="fva", actor_id="rv1",
                                           floor_id=fv["floor_id"])
        self.service.create_contract(request_id="ct", actor_id="op1", brand_id="b1",
                                     channel="restaurant", max_discount_rate=rate,
                                     effective_from="2026-01-01")
        cv = self.database.connection.execute(
            "SELECT contract_id FROM cp_contracts WHERE brand_id='b1'").fetchone()
        self.service.approve_contract(request_id="cta", actor_id="rv1",
                                      contract_id=cv["contract_id"])

    def _promise(self, *, request_id="pr", promise_id="pm", amount="100",
                 fund_type="general", ctype="off_invoice"):
        component = ({"type": ctype, "amount_per_case": amount} if ctype != "instant"
                     else {"type": ctype, "amount_per_order": amount})
        self.service.create_promise(
            request_id=request_id, actor_id="op1", promise_id=promise_id, brand_id="b1",
            name="承诺", channel="restaurant", fund_type=fund_type,
            start_date="2026-09-01", end_date="2026-12-31", budget_cap="100000",
            components=[component])
        self.service.approve_promise(request_id=request_id + "a", actor_id="rv1",
                                     promise_id=promise_id)

    def test_auditor_cannot_create_rules(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_brand(request_id="xb", actor_id="au1",
                                        brand_id="bx", name="越权品牌")

    def test_replay_same_request_is_idempotent(self):
        first = self.service.register_brand(request_id="dup", actor_id="op1",
                                            brand_id="bd", name="品牌D")
        second = self.service.register_brand(request_id="dup", actor_id="op1",
                                             brand_id="bd", name="品牌D")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])

    def test_same_request_changed_payload_conflicts(self):
        self.service.register_brand(request_id="dup", actor_id="op1", brand_id="bd", name="A")
        with self.assertRaises(ConflictError):
            self.service.register_brand(request_id="dup", actor_id="op1", brand_id="bd", name="B")

    def test_cultivation_fund_blocked_on_mature_product(self):
        self._effective_rules()
        self._promise(request_id="pc", promise_id="cult", amount="20", fund_type="cultivation",
                      ctype="cultivation")
        result = self.service.submit_campaign(
            request_id="c1", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["cult"], store_id="st1")
        self.assertIn("CULTIVATION_FUND_MISMATCH", {c["code"] for c in result["conflicts"]})

    def test_unstacked_promises_conflict(self):
        self._effective_rules()
        self._promise(request_id="p1", promise_id="px", amount="50")
        self._promise(request_id="p2", promise_id="py", amount="50")
        result = self.service.submit_campaign(
            request_id="c2", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["px", "py"], store_id="st1")
        self.assertIn("STACK_NOT_ALLOWED", {c["code"] for c in result["conflicts"]})

    def test_floor_breach_blocks_approval(self):
        self._effective_rules(floor="850")
        self._promise(amount="200")
        result = self.service.submit_campaign(
            request_id="c3", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["pm"], store_id="st1")
        self.assertTrue(result["has_blocker"])
        with self.assertRaises(PriceFloorBreached):
            self.service.approve_campaign(request_id="c3a", actor_id="rv1",
                                          campaign_id=result["resource_id"])

    def test_contract_rate_breach_is_flagged(self):
        self._effective_rules(rate="0.05")  # 合同只允许 5% 折扣
        self._promise(amount="100")         # 实际 10%
        result = self.service.submit_campaign(
            request_id="c4", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["pm"], store_id="st1")
        self.assertIn("CONTRACT_RATE_EXCEEDED", {c["code"] for c in result["conflicts"]})

    def test_approver_must_differ_from_submitter(self):
        self._effective_rules()
        self._promise(amount="100")
        result = self.service.submit_campaign(
            request_id="c5", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["pm"], store_id="st1")
        with self.assertRaises(PermissionDenied):
            self.service.approve_campaign(request_id="c5self", actor_id="op1",
                                          campaign_id=result["resource_id"])

    def test_emergency_requires_store_limit_and_review(self):
        self._effective_rules()
        self._promise(promise_id="pm", amount="50")
        exc = self.service.request_emergency_exception(
            request_id="e1", actor_id="op1", promise_id="pm", product_id="p1",
            store_id="st1", max_cases="3", amount_per_case="20",
            valid_from="2026-10-10", valid_to="2026-10-12", reason="应急")
        with self.assertRaises(PermissionDenied):
            self.service.review_emergency_exception(
                request_id="e1self", actor_id="op1", exception_id=exc["resource_id"],
                decision="approved")
        self.service.review_emergency_exception(
            request_id="e1a", actor_id="rv1", exception_id=exc["resource_id"],
            decision="approved")

    def test_emergency_rejects_invalid_period(self):
        self._effective_rules()
        self._promise(promise_id="pm", amount="50")
        with self.assertRaises(ValidationError):
            self.service.request_emergency_exception(
                request_id="e2", actor_id="op1", promise_id="pm", product_id="p1",
                store_id="st1", max_cases="3", amount_per_case="20",
                valid_from="2026-10-15", valid_to="2026-10-10", reason="应急")

    def test_order_history_survives_repricing(self):
        self._effective_rules()
        self._promise(amount="100")
        campaign = self.service.submit_campaign(
            request_id="c6", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["pm"], store_id="st1")
        self.service.approve_campaign(request_id="c6a", actor_id="rv1",
                                      campaign_id=campaign["resource_id"])
        self.service.book_order(request_id="o1", actor_id="op1", order_id="ord1",
                                campaign_id=campaign["resource_id"], store_id="st1",
                                cases="5", order_date="2026-10-06")
        before = self.service.get_order("ord1")

        # 追加一个涨价新版本并生效。
        self.service.create_product_version(request_id="pv2", actor_id="op1", product_id="p1",
                                            tier="premium", lifecycle_stage="maturity",
                                            list_price_per_case="1300",
                                            effective_from="2026-10-20")
        pv2 = self.database.connection.execute(
            "SELECT version_id FROM cp_product_versions ORDER BY rowid DESC LIMIT 1").fetchone()
        self.service.approve_product_version(request_id="pv2a", actor_id="rv1",
                                             version_id=pv2["version_id"])
        after = self.service.get_order("ord1")
        self.assertEqual(after["evaluation"]["net_price_per_case"],
                         before["evaluation"]["net_price_per_case"])
        self.assertEqual(after["snapshot"]["resolution"]["product_version_id"],
                         before["snapshot"]["resolution"]["product_version_id"])

    def test_settlement_is_idempotent_and_budget_reconciles(self):
        self._effective_rules()
        self._promise(amount="100")
        campaign = self.service.submit_campaign(
            request_id="c7", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["pm"], store_id="st1")
        self.service.approve_campaign(request_id="c7a", actor_id="rv1",
                                      campaign_id=campaign["resource_id"])
        self.service.book_order(request_id="o1", actor_id="op1", order_id="ord1",
                                campaign_id=campaign["resource_id"], store_id="st1",
                                cases="5", order_date="2026-10-06")
        kwargs = dict(actor_id="op1", event_id="ev1", order_id="ord1", promise_id="pm",
                      event_type="redemption", cases="5", amount="500",
                      occurrence_date="2026-10-07")
        first = self.service.post_settlement_event(request_id="s1", **kwargs)
        replay = self.service.post_settlement_event(request_id="s1", **kwargs)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.service.remaining_budget("pm")["used"], "500.00")

        # 即便对端更换 request_id，用同一 event_id 重试也不得重复入账。
        re_event = self.service.post_settlement_event(request_id="s1-alt", **kwargs)
        self.assertTrue(re_event.get("duplicate_event"))
        self.assertEqual(self.service.remaining_budget("pm")["used"], "500.00")

        self.service.post_settlement_event(
            request_id="s2", actor_id="op1", event_id="ev2", order_id="ord1", promise_id="pm",
            event_type="return", cases="2", amount="200", occurrence_date="2026-10-08")
        self.assertEqual(self.service.remaining_budget("pm")["used"], "300.00")

    def test_settlement_must_reference_original_promise(self):
        self._effective_rules()
        self._promise(request_id="p1", promise_id="px", amount="100")
        campaign = self.service.submit_campaign(
            request_id="c8", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["px"], store_id="st1")
        self.service.approve_campaign(request_id="c8a", actor_id="rv1",
                                      campaign_id=campaign["resource_id"])
        self.service.book_order(request_id="o1", actor_id="op1", order_id="ord1",
                                campaign_id=campaign["resource_id"], store_id="st1",
                                cases="5", order_date="2026-10-06")
        with self.assertRaises(ValidationError):
            self.service.post_settlement_event(
                request_id="s3", actor_id="op1", event_id="ev3", order_id="ord1",
                promise_id="not-on-order", event_type="redemption", cases="1",
                amount="100", occurrence_date="2026-10-07")

    def test_over_redemption_rejected(self):
        self._effective_rules()
        self._promise(amount="100")
        campaign = self.service.submit_campaign(
            request_id="c9", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["pm"], store_id="st1")
        self.service.approve_campaign(request_id="c9a", actor_id="rv1",
                                      campaign_id=campaign["resource_id"])
        self.service.book_order(request_id="o1", actor_id="op1", order_id="ord1",
                                campaign_id=campaign["resource_id"], store_id="st1",
                                cases="5", order_date="2026-10-06")
        with self.assertRaises(StateConflict):
            self.service.post_settlement_event(
                request_id="s4", actor_id="op1", event_id="ev4", order_id="ord1",
                promise_id="pm", event_type="redemption", cases="6", amount="600",
                occurrence_date="2026-10-07")

    def test_order_recheck_does_not_double_count_own_reserve(self):
        # 预算上限恰好等于本活动预留额：批准后额度用满，下单复核不得重复计入本活动预留。
        self._effective_rules()
        self.service.create_promise(
            request_id="pe", actor_id="op1", promise_id="exact", brand_id="b1", name="恰好",
            channel="restaurant", fund_type="general", start_date="2026-09-01",
            end_date="2026-12-31", budget_cap="500",
            components=[{"type": "off_invoice", "amount_per_case": "100"}])
        self.service.approve_promise(request_id="pea", actor_id="rv1", promise_id="exact")
        campaign = self.service.submit_campaign(
            request_id="ce", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="5", activity_date="2026-10-05",
            promise_ids=["exact"], store_id="st1")
        self.assertFalse(campaign["has_blocker"], campaign["conflicts"])
        self.service.approve_campaign(request_id="cea", actor_id="rv1",
                                      campaign_id=campaign["resource_id"])
        self.assertEqual(self.service.remaining_budget("exact")["remaining"], "0.00")
        # 若复核重复计入本活动预留，这里会抛 BUDGET_EXCEEDED。
        self.service.book_order(request_id="oe", actor_id="op1", order_id="orde",
                                campaign_id=campaign["resource_id"], store_id="st1",
                                cases="5", order_date="2026-10-06")

    def test_budget_cap_blocks_campaign_approval(self):
        self._effective_rules()
        # 预算只有 100，但 10 箱折让 100/箱 = 1000。
        self.service.create_promise(
            request_id="pb", actor_id="op1", promise_id="tight", brand_id="b1", name="紧",
            channel="restaurant", fund_type="general", start_date="2026-09-01",
            end_date="2026-12-31", budget_cap="100",
            components=[{"type": "off_invoice", "amount_per_case": "100"}])
        self.service.approve_promise(request_id="pba", actor_id="rv1", promise_id="tight")
        result = self.service.submit_campaign(
            request_id="c10", actor_id="op1", brand_id="b1", channel="restaurant",
            region_id="r1", product_id="p1", cases="10", activity_date="2026-10-05",
            promise_ids=["tight"], store_id="st1")
        self.assertIn("BUDGET_EXCEEDED", {c["code"] for c in result["conflicts"]})
        with self.assertRaises(StateConflict):
            self.service.approve_campaign(request_id="c10a", actor_id="rv1",
                                          campaign_id=result["resource_id"])


if __name__ == "__main__":
    unittest.main()
