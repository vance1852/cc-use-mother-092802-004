"""价盘促销治理：规则版本、冲突归因、审批分离与紧急例外测试。"""

import unittest

from beverage_ops_foundation.errors import ConflictError, PermissionDenied, ValidationError

try:
    from .promo_fixture import PromoFixture, ts
except ImportError:  # python3 -m unittest discover -s tests
    from promo_fixture import PromoFixture, ts


class PricingGovernanceTest(unittest.TestCase):
    def setUp(self):
        self.fx = PromoFixture()
        self.fx.publish_standard_rules()

    def tearDown(self):
        self.fx.close()

    def _clean_mechanics(self):
        return [
            {"mechanic_id": "m1", "group": "instant_discount", "type": "cents",
             "value": 10000, "fund_id": "f-nurture"},
            {"mechanic_id": "m2", "group": "coupon", "type": "cents",
             "value": 2000, "fund_id": "f-general"},
        ]

    def _submit_clean(self):
        # 1000 元厂价 - 商超合同 5% (50 元) - 立减 100 元 - 券 20 元 = 830 元，高于底价 800 元。
        return self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-clean",
            product_id="p-premium", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1", "st2"], period_start=ts(2026, 10, 10),
            period_end=ts(2026, 10, 20, 23), planned_units=100,
            mechanics=self._clean_mechanics())

    def test_clean_promotion_computes_net_price_and_versions(self):
        result = self._submit_clean()
        self.assertEqual("pending_approval", result["status"])
        ev = result["evaluation"]
        self.assertEqual(100000, ev["list_price_cents"])
        self.assertEqual(5000, ev["contract"]["discount_cents"])
        self.assertEqual(83000, ev["unit_net_cents"])
        self.assertEqual([], ev["conflicts"])
        self.assertIn("product", ev["rule_version_ids"])
        self.assertIn("floor_price", ev["rule_version_ids"])
        self.assertIn("channel_contract", ev["rule_version_ids"])
        self.assertIn("cap:f-nurture", ev["rule_version_ids"])

    def test_stacking_discounts_below_floor_is_blocked_with_source(self):
        mechanics = [
            {"mechanic_id": "m1", "group": "instant_discount", "type": "cents", "value": 10000},
            {"mechanic_id": "m2", "group": "coupon", "type": "cents", "value": 10000},
        ]
        result = self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-floor",
            product_id="p-premium", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1"], period_start=ts(2026, 10, 10), period_end=ts(2026, 10, 20, 23),
            planned_units=10, mechanics=mechanics)
        self.assertEqual("blocked", result["status"])
        conflict = next(c for c in result["evaluation"]["conflicts"]
                        if c["code"] == "price_below_floor")
        self.assertEqual(75000, conflict["net_cents"])
        self.assertEqual(80000, conflict["floor_cents"])
        # 冲突必须能指回是哪一版底价造成的。
        self.assertIsNotNone(conflict["source_version_id"])

    def test_forbidden_stack_pair_reports_rule_version(self):
        mechanics = [
            {"mechanic_id": "m1", "group": "coupon", "type": "cents", "value": 1000},
            {"mechanic_id": "m2", "group": "rebate", "type": "bps", "value": 100},
        ]
        result = self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-stack",
            product_id="p-premium", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1"], period_start=ts(2026, 10, 10), period_end=ts(2026, 10, 20, 23),
            planned_units=10, mechanics=mechanics)
        conflict = next(c for c in result["evaluation"]["conflicts"]
                        if c["code"] == "stack_forbidden")
        self.assertEqual("coupon", conflict["conflict_with"])
        self.assertIsNotNone(conflict["source_version_id"])

    def test_nurture_fund_cannot_apply_to_mature_product(self):
        mechanics = [{"mechanic_id": "m1", "group": "instant_discount", "type": "cents",
                      "value": 500, "fund_id": "f-nurture"}]
        result = self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-mature",
            product_id="p-mass", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1"], period_start=ts(2026, 10, 10), period_end=ts(2026, 10, 20, 23),
            planned_units=10, mechanics=mechanics)
        codes = {c["code"] for c in result["evaluation"]["conflicts"]}
        self.assertIn("fund_lifecycle_mismatch", codes)
        mismatch = next(c for c in result["evaluation"]["conflicts"]
                        if c["code"] == "fund_lifecycle_mismatch")
        self.assertIsNotNone(mismatch["source_version_id"])
        self.assertEqual("blocked", result["status"])

    def test_reviewer_approves_but_submitter_cannot_self_approve(self):
        submitted = self._submit_clean()
        version_id = submitted["version_id"]
        with self.assertRaises(PermissionDenied):
            self.fx.service.approve_promotion(
                request_id=self.fx.req("ap"), actor_id="op1",
                promotion_version_id=version_id)
        with self.assertRaises(PermissionDenied):
            self.fx.service.approve_promotion(
                request_id=self.fx.req("ap"), actor_id="au1",
                promotion_version_id=version_id)
        approved = self.fx.service.approve_promotion(
            request_id=self.fx.req("ap"), actor_id="rv1", promotion_version_id=version_id)
        self.assertEqual("approved", approved["status"])
        self.assertIn("floor_price", approved["snapshot"]["rule_version_ids"])
        detail = self.fx.service.get_promotion_version(version_id)
        self.assertEqual("rv1", detail["approved_by"])

    def test_blocked_promotion_cannot_be_approved_directly(self):
        mechanics = [
            {"mechanic_id": "m1", "group": "instant_discount", "type": "cents", "value": 10000},
            {"mechanic_id": "m2", "group": "coupon", "type": "cents", "value": 10000},
        ]
        blocked = self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-blocked",
            product_id="p-premium", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1", "st2"], period_start=ts(2026, 10, 10),
            period_end=ts(2026, 10, 20, 23), planned_units=50, mechanics=mechanics)
        with self.assertRaises(ConflictError):
            self.fx.service.approve_promotion(
                request_id=self.fx.req("ap"), actor_id="rv1",
                promotion_version_id=blocked["version_id"])

    def test_emergency_exception_bounded_and_dual_role(self):
        mechanics = [
            {"mechanic_id": "m1", "group": "instant_discount", "type": "cents", "value": 10000},
            {"mechanic_id": "m2", "group": "coupon", "type": "cents", "value": 10000},
        ]
        blocked = self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-emg",
            product_id="p-premium", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1", "st2"], period_start=ts(2026, 10, 10),
            period_end=ts(2026, 10, 20, 23), planned_units=50, mechanics=mechanics)
        # 例外门店必须是活动门店子集。
        with self.assertRaises(ValidationError):
            self.fx.service.request_emergency_exception(
                request_id=self.fx.req("ex"), actor_id="op1",
                promotion_version_id=blocked["version_id"], store_ids=["st9"],
                quantity_limit=5, deadline=ts(2026, 10, 15), reason="竞品突发活动")
        # 数量不能超过计划量。
        with self.assertRaises(ValidationError):
            self.fx.service.request_emergency_exception(
                request_id=self.fx.req("ex"), actor_id="op1",
                promotion_version_id=blocked["version_id"], store_ids=["st1"],
                quantity_limit=99, deadline=ts(2026, 10, 15), reason="竞品突发活动")
        exception = self.fx.service.request_emergency_exception(
            request_id=self.fx.req("ex"), actor_id="op1",
            promotion_version_id=blocked["version_id"], store_ids=["st1"],
            quantity_limit=5, deadline=ts(2026, 10, 15), reason="竞品突发活动")
        # 申请人不能自己复核。
        with self.assertRaises(PermissionDenied):
            self.fx.service.review_emergency_exception(
                request_id=self.fx.req("rv"), actor_id="op1",
                exception_id=exception["exception_id"], approved=True)
        reviewed = self.fx.service.review_emergency_exception(
            request_id=self.fx.req("rv"), actor_id="rv1",
            exception_id=exception["exception_id"], approved=True)
        self.assertEqual("approved", reviewed["status"])
        version_id = blocked["version_id"]
        # 非例外门店不能下单。
        with self.assertRaises(PermissionDenied):
            self.fx.service.create_order(
                request_id=self.fx.req("od"), actor_id="op1", order_id="o-bad-store",
                promotion_version_id=version_id, store_id="st2", quantity=1,
                as_of=ts(2026, 10, 11))
        # 正常下单（st1，数量累计 5）。
        for i in range(5):
            self.fx.service.create_order(
                request_id=self.fx.req("od"), actor_id="op1", order_id=f"ok{i}",
                promotion_version_id=version_id, store_id="st1", quantity=1,
                as_of=ts(2026, 10, 11))
        # 超过例外数量上限。
        with self.assertRaises(ConflictError):
            self.fx.service.create_order(
                request_id=self.fx.req("od"), actor_id="op1", order_id="o-over",
                promotion_version_id=version_id, store_id="st1", quantity=1,
                as_of=ts(2026, 10, 11))
        # 超过例外期限。
        with self.assertRaises(PermissionDenied):
            self.fx.service.create_order(
                request_id=self.fx.req("od"), actor_id="op1", order_id="o-late",
                promotion_version_id=version_id, store_id="st1", quantity=1,
                as_of=ts(2026, 10, 16))

    def test_same_effective_date_rejected_for_price_change(self):
        with self.assertRaises(ConflictError):
            self.fx.service.publish_product_hierarchy(
                request_id=self.fx.req("r"), actor_id="a1", product_id="p-premium",
                brand_id="b1", name="珍藏30年", tier="premium", lifecycle="nurturing",
                list_price_cents=99900, effective_from=ts(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
