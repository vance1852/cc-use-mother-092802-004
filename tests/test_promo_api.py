"""促销治理 HTTP/JSON 边界测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.api import route
from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.promo_service import PromoGovernanceService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

try:
    from .promo_fixture import PromoFixture, ts
except ImportError:  # python3 -m unittest discover -s tests
    from promo_fixture import PromoFixture, ts


class PromoApiTest(unittest.TestCase):
    def setUp(self):
        self.fx = PromoFixture(datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.fx.publish_standard_rules()
        self.service = self.fx.base
        self.service.promo = self.fx.service

    def tearDown(self):
        self.fx.close()

    def _headers(self, actor="op1"):
        return {"X-Actor-Id": actor}

    def test_promo_end_to_end_over_http(self):
        body = {
            "request_id": "http-sub-1", "promotion_id": "promo-web",
            "product_id": "p-premium", "brand_id": "b1", "channel": "hypermarket",
            "region": "east", "store_ids": ["st1", "st2"],
            "period_start": ts(2026, 10, 10), "period_end": ts(2026, 10, 20, 23),
            "planned_units": 100,
            "mechanics": [
                {"mechanic_id": "m1", "group": "instant_discount", "type": "cents",
                 "value": 10000, "fund_id": "f-nurture"},
                {"mechanic_id": "m2", "group": "coupon", "type": "cents",
                 "value": 2000, "fund_id": "f-general"},
            ],
        }
        status, payload = route(self.service, "POST", "/promotions/submit", body,
                                self._headers())
        self.assertEqual(201, status)
        self.assertEqual("pending_approval", payload["status"])
        version_id = payload["version_id"]

        status, payload = route(self.service, "POST", "/promotions/approve",
                                {"request_id": "http-ap-1",
                                 "promotion_version_id": version_id},
                                self._headers("rv1"))
        self.assertEqual(201, status)
        self.assertEqual("approved", payload["status"])

        status, order = route(self.service, "POST", "/orders",
                              {"request_id": "http-od-1", "order_id": "web-order-1",
                               "promotion_version_id": version_id, "store_id": "st1",
                               "quantity": 2, "as_of": ts(2026, 10, 11)},
                              self._headers())
        self.assertEqual(201, status)
        self.assertEqual(83000, order["unit_net_cents"])

        status, writeoff = route(self.service, "POST", "/settlements",
                                 {"request_id": "http-st-1",
                                  "promotion_version_id": version_id,
                                  "external_voucher_id": "web-v-1", "kind": "writeoff",
                                  "amount_cents": 1_000_000, "quantity": 2,
                                  "order_id": "web-order-1", "fund_id": "f-nurture"},
                                 self._headers())
        self.assertEqual(201, status)
        self.assertEqual(4_000_000, writeoff["remaining_cents"])

        status, trace = route(
            self.service, "GET",
            f"/expenses/trace?external_voucher_id=web-v-1", None, self._headers())
        self.assertEqual(200, status)
        self.assertEqual(version_id, trace["promotion_version"]["version_id"])
        self.assertEqual("web-order-1", trace["order"]["order_id"])

    def test_blocked_promotion_conflicts_returned_in_response(self):
        body = {
            "request_id": "http-sub-blocked", "promotion_id": "promo-bad",
            "product_id": "p-premium", "brand_id": "b1", "channel": "hypermarket",
            "region": "east", "store_ids": ["st1"],
            "period_start": ts(2026, 10, 10), "period_end": ts(2026, 10, 20, 23),
            "planned_units": 10,
            "mechanics": [
                {"mechanic_id": "m1", "group": "instant_discount", "type": "cents",
                 "value": 10000},
                {"mechanic_id": "m2", "group": "coupon", "type": "cents", "value": 10000},
            ],
        }
        status, payload = route(self.service, "POST", "/promotions/submit", body,
                                self._headers())
        self.assertEqual(201, status)
        self.assertEqual("blocked", payload["status"])
        codes = {c["code"] for c in payload["evaluation"]["conflicts"]}
        self.assertIn("price_below_floor", codes)

    def test_promo_route_absent_without_service(self):
        database = Database()
        bare = DomainService(database)
        status, payload = route(bare, "POST", "/promotions/submit", {}, {})
        self.assertEqual(404, status)
        database.close()


if __name__ == "__main__":
    unittest.main()
