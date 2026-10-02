"""订单规则快照、后续调价不重写历史、结算核销/退货/无效凭证与财务追查测试。"""

import unittest

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import ConflictError, NotFoundError

try:
    from .promo_fixture import PromoFixture, ts
except ImportError:  # python3 -m unittest discover -s tests
    from promo_fixture import PromoFixture, ts


class OrderAndSettlementTest(unittest.TestCase):
    def setUp(self):
        # 时钟固定在 10 月 11 日，例外期限校验使用当前时间。
        from datetime import datetime, timezone
        self.fx = PromoFixture(datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.fx.publish_standard_rules()
        self.version_id = self._approved_promotion()

    def tearDown(self):
        self.fx.close()

    def _approved_promotion(self) -> str:
        submitted = self.fx.service.submit_promotion(
            request_id=self.fx.req("sub"), actor_id="op1", promotion_id="promo-1",
            product_id="p-premium", brand_id="b1", channel="hypermarket", region="east",
            store_ids=["st1", "st2"], period_start=ts(2026, 10, 10),
            period_end=ts(2026, 10, 20, 23), planned_units=100,
            mechanics=[
                {"mechanic_id": "m1", "group": "instant_discount", "type": "cents",
                 "value": 10000, "fund_id": "f-nurture"},
                {"mechanic_id": "m2", "group": "coupon", "type": "cents",
                 "value": 2000, "fund_id": "f-general"},
            ])
        self.fx.service.approve_promotion(
            request_id=self.fx.req("ap"), actor_id="rv1",
            promotion_version_id=submitted["version_id"])
        return submitted["version_id"]

    def test_order_freezes_rule_versions_and_later_floor_change_keeps_history(self):
        first = self.fx.service.create_order(
            request_id=self.fx.req("od"), actor_id="op1", order_id="order-1",
            promotion_version_id=self.version_id, store_id="st1", quantity=2,
            as_of=ts(2026, 10, 11))
        self.assertEqual(83000, first["unit_net_cents"])
        self.assertEqual(166000, first["total_net_cents"])
        first_floor_version = first["snapshot"]["order_time_rule_version_ids"]["floor_price"]

        # 10 月 15 日底价上调到 900 元（新版本只追加）。
        self.fx.service.publish_floor_price(
            request_id=self.fx.req("r"), actor_id="a1", product_id="p-premium",
            region="east", floor_cents=90000, effective_from=ts(2026, 10, 15))

        # 历史订单仍然引用旧底价版本、旧净价，不被重写。
        trace_order = self.fx.service.create_order(
            request_id=self.fx.req("od"), actor_id="op1", order_id="order-hist-check",
            promotion_version_id=self.version_id, store_id="st1", quantity=1,
            as_of=ts(2026, 10, 12))
        self.assertEqual(83000, trace_order["unit_net_cents"])
        self.assertEqual(first_floor_version,
                         trace_order["snapshot"]["order_time_rule_version_ids"]["floor_price"])

        # 调价后的新订单按下单当时规则被拦截（净价 830 < 新底价 900）。
        with self.assertRaises(ConflictError):
            self.fx.service.create_order(
                request_id=self.fx.req("od"), actor_id="op1", order_id="order-2",
                promotion_version_id=self.version_id, store_id="st1", quantity=1,
                as_of=ts(2026, 10, 16))

    def test_order_idempotent_replay(self):
        payload = dict(request_id="od-replay", actor_id="op1", order_id="order-r",
                       promotion_version_id=self.version_id, store_id="st1", quantity=1)
        first = self.fx.service.create_order(**payload, as_of=ts(2026, 10, 11))
        second = self.fx.service.create_order(**payload, as_of=ts(2026, 10, 11))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["order_id"], second["order_id"])

    def test_settlement_writeoff_cap_enforcement_and_trace(self):
        self.fx.service.create_order(
            request_id=self.fx.req("od"), actor_id="op1", order_id="order-s",
            promotion_version_id=self.version_id, store_id="st1", quantity=10,
            as_of=ts(2026, 10, 11))
        # 第一笔核销 30000 元（培育费用上限 50000 元）。
        w1 = self.fx.service.submit_settlement(
            request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
            external_voucher_id="v-001", kind="writeoff", amount_cents=3_000_000, quantity=10,
            order_id="order-s", fund_id="f-nurture")
        self.assertEqual(2_000_000, w1["remaining_cents"])

        # 重复回传同一凭证：幂等回放，不重复占用额度。
        replay = self.fx.service.submit_settlement(
            request_id=self.fx.req("st-dup"), actor_id="op1", promotion_version_id=self.version_id,
            external_voucher_id="v-001", kind="writeoff", amount_cents=3_000_000, quantity=10,
            order_id="order-s", fund_id="f-nurture")
        self.assertTrue(replay["replayed"])
        self.assertEqual(2_000_000, replay["remaining_cents"])

        # 再核销 25000 元，超出剩余 20000 元上限，拒绝。
        with self.assertRaises(ConflictError):
            self.fx.service.submit_settlement(
                request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
                external_voucher_id="v-002", kind="writeoff", amount_cents=2_500_000, quantity=8,
                order_id="order-s", fund_id="f-nurture")

        # 财务追查：费用 -> 上限版本 -> 订单 -> 批准版本。
        trace = self.fx.service.trace_expense(external_voucher_id="v-001")
        self.assertEqual("writeoff", trace["entry"]["kind"])
        self.assertEqual("rv1", trace["promotion_version"]["approved_by"])
        self.assertIsNotNone(trace["promotion_version"]["frozen_rule_version_ids"])
        self.assertEqual("order-s", trace["order"]["order_id"])
        self.assertEqual(3_000_000, trace["cap"]["used_cents"])
        self.assertEqual(2_000_000, trace["cap"]["remaining_cents"])
        self.assertEqual(trace["cap"]["cap_version_id"], w1["cap_version_id"])

    def test_returns_and_invalid_vouchers_restore_cap_against_original(self):
        self.fx.service.create_order(
            request_id=self.fx.req("od"), actor_id="op1", order_id="order-r",
            promotion_version_id=self.version_id, store_id="st1", quantity=10,
            as_of=ts(2026, 10, 11))
        self.fx.service.submit_settlement(
            request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
            external_voucher_id="v-100", kind="writeoff", amount_cents=4_000_000, quantity=10,
            order_id="order-r", fund_id="f-nurture")
        # 退货 1500 元，额度回到 25000 元。
        ret = self.fx.service.submit_settlement(
            request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
            external_voucher_id="v-101", kind="return", amount_cents=1_500_000, quantity=4,
            original_voucher_id="v-100")
        self.assertEqual(2_500_000, ret["remaining_cents"])
        # 退货凭证重复回传幂等。
        ret_replay = self.fx.service.submit_settlement(
            request_id=self.fx.req("st-rr"), actor_id="op1", promotion_version_id=self.version_id,
            external_voucher_id="v-101", kind="return", amount_cents=1_500_000, quantity=4,
            original_voucher_id="v-100")
        self.assertTrue(ret_replay["replayed"])
        self.assertEqual(2_500_000, ret_replay["remaining_cents"])
        # 累计冲回不能超过原核销：剩余可冲 25000，再冲 30000 拒绝。
        with self.assertRaises(ConflictError):
            self.fx.service.submit_settlement(
                request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
                external_voucher_id="v-102", kind="invalid", amount_cents=3_000_000, quantity=6,
                original_voucher_id="v-100")
        # 无效凭证必须引用原核销凭证。
        with self.assertRaises(NotFoundError):
            self.fx.service.submit_settlement(
                request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
                external_voucher_id="v-103", kind="invalid", amount_cents=100, quantity=1,
                original_voucher_id="v-missing")
        # 额度恢复后可以再核销。
        w2 = self.fx.service.submit_settlement(
            request_id=self.fx.req("st"), actor_id="op1", promotion_version_id=self.version_id,
            external_voucher_id="v-104", kind="writeoff", amount_cents=2_000_000, quantity=5,
            order_id="order-r", fund_id="f-nurture")
        self.assertEqual(500_000, w2["remaining_cents"])
        # 追查链在退货后仍闭合。
        trace = self.fx.service.trace_expense(external_voucher_id="v-101")
        self.assertEqual("v-100", trace["entry"]["original_voucher_id"])
        self.assertEqual(4_500_000, trace["cap"]["used_cents"])
        self.assertEqual(500_000, trace["cap"]["remaining_cents"])
        self.assertEqual("order-r", trace["order"]["order_id"])


if __name__ == "__main__":
    unittest.main()
