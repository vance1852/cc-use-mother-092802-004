"""渠道价盘核算纯函数的单元测试。"""

import unittest
from decimal import Decimal

from channel_pricing.rules import (
    cultivation_allowed,
    evaluate_offer,
    money,
    resolve_lifecycle,
)


class MoneyTest(unittest.TestCase):
    def test_money_is_two_place_half_up(self):
        self.assertEqual(money("1.005"), Decimal("1.01"))
        self.assertEqual(money(2.5), Decimal("2.50"))
        self.assertEqual(money(Decimal("10")), Decimal("10.00"))

    def test_negative_discount_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_offer(list_price_per_case="100", cases="2",
                           components=[{"type": "off_invoice", "amount_per_case": "-1"}])


class CultivationTest(unittest.TestCase):
    def test_only_intro_and_cultivation_allow_fund(self):
        self.assertTrue(cultivation_allowed("introduction"))
        self.assertTrue(cultivation_allowed("cultivation"))
        self.assertFalse(cultivation_allowed("maturity"))
        self.assertFalse(cultivation_allowed("decline"))


class LifecycleResolutionTest(unittest.TestCase):
    def test_latest_effective_version_wins(self):
        versions = [
            {"effective_from": "2026-01-01", "effective_to": None, "price": "100"},
            {"effective_from": "2026-06-01", "effective_to": None, "price": "120"},
        ]
        self.assertEqual(resolve_lifecycle(versions, "2026-05-31")["price"], "100")
        self.assertEqual(resolve_lifecycle(versions, "2026-06-01")["price"], "120")

    def test_closed_period_not_resolved_after_to(self):
        versions = [{"effective_from": "2026-01-01", "effective_to": "2026-06-01", "price": "100"}]
        self.assertIsNone(resolve_lifecycle(versions, "2026-06-01"))
        self.assertIsNotNone(resolve_lifecycle(versions, "2026-05-31"))


class EvaluateOfferTest(unittest.TestCase):
    def test_stacking_discounts_breach_floor_with_sources(self):
        result = evaluate_offer(
            list_price_per_case="1000", cases="10",
            components=[
                {"promise_id": "a", "type": "off_invoice", "amount_per_case": "100"},
                {"promise_id": "b", "type": "billback", "amount_per_case": "100"},
            ],
            floor_price_per_case="850",
        )
        self.assertEqual(result["list_total"], Decimal("10000.00"))
        self.assertEqual(result["price_discount_total"], Decimal("2000.00"))
        self.assertEqual(result["net_total"], Decimal("8000.00"))
        self.assertEqual(result["net_price_per_case"], Decimal("800.00"))
        self.assertTrue(result["floor_breached"])
        self.assertEqual(set(result["floor_breach_sources"]), {"a", "b"})

    def test_cultivation_fee_does_not_affect_price(self):
        result = evaluate_offer(
            list_price_per_case="600", cases="5",
            components=[
                {"promise_id": "c", "type": "cultivation", "amount_per_case": "40"},
            ],
            floor_price_per_case="550",
        )
        # 培育费只占用预算，不进入消费者成交价，因此不击穿底价。
        self.assertEqual(result["net_price_per_case"], Decimal("600.00"))
        self.assertFalse(result["floor_breached"])
        self.assertEqual(result["fee_only_total"], Decimal("200.00"))
        self.assertEqual(result["price_discount_total"], Decimal("0.00"))

    def test_instant_discount_is_per_order_capped(self):
        result = evaluate_offer(
            list_price_per_case="300", cases="3",
            components=[{"promise_id": "i", "type": "instant", "amount_per_order": "25"}],
        )
        self.assertEqual(result["net_total"], Decimal("875.00"))
        self.assertEqual(result["line_items"][0]["rate_basis"], "per_order")

    def test_at_or_above_floor_is_not_breach(self):
        result = evaluate_offer(
            list_price_per_case="1000", cases="1",
            components=[{"promise_id": "a", "type": "off_invoice", "amount_per_case": "150"}],
            floor_price_per_case="850",
        )
        self.assertEqual(result["net_price_per_case"], Decimal("850.00"))
        self.assertFalse(result["floor_breached"])


if __name__ == "__main__":
    unittest.main()
