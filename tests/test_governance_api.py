"""渠道价盘治理 HTTP 路由测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from channel_pricing.api import route
from channel_pricing.service import GovernanceService


class GovernanceApiTest(unittest.TestCase):
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

    def tearDown(self):
        self.database.close()

    def test_health_without_actor(self):
        status, payload = route(self.service, "GET", "/cp/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_404(self):
        status, payload = route(self.service, "GET", "/cp/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_permission_denied_maps_to_403(self):
        status, payload = route(self.service, "POST", "/cp/brands",
                                {"request_id": "xb", "brand_id": "bx", "name": "品牌"},
                                {"X-Actor-Id": "au1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_full_chain_via_routes_reports_floor_breach(self):
        def post(path, body, actor="op1"):
            status, payload = route(self.service, "POST", path, body, {"X-Actor-Id": actor})
            return status, payload

        self.assertEqual(201, post("/cp/brands", {"request_id": "br", "brand_id": "b1",
                                                  "name": "品牌"})[0])
        post("/cp/regions", {"request_id": "rg", "region_id": "r1", "name": "区域"})
        post("/cp/stores", {"request_id": "st", "store_id": "st1", "region_id": "r1",
                            "channel": "instant_retail", "name": "门店"})
        post("/cp/products", {"request_id": "pd", "product_id": "p1", "brand_id": "b1",
                              "sku": "S1", "name": "产品"})
        post("/cp/product-versions", {"request_id": "pv",
                                      "product_id": "p1", "tier": "mass",
                                      "lifecycle_stage": "maturity",
                                      "list_price_per_case": "100",
                                      "effective_from": "2026-01-01"})
        # 复核角色批准。
        pv = self.database.connection.execute(
            "SELECT version_id FROM cp_product_versions").fetchone()
        post("/cp/product-versions/approve", {"request_id": "pva", "version_id": pv["version_id"]},
             actor="rv1")
        post("/cp/floor-versions", {"request_id": "fv", "product_id": "p1",
                                    "region_id": "r1", "floor_price_per_case": "90",
                                    "effective_from": "2026-01-01"})
        fv = self.database.connection.execute("SELECT floor_id FROM cp_floor_versions").fetchone()
        post("/cp/floor-versions/approve", {"request_id": "fva", "floor_id": fv["floor_id"]},
             actor="rv1")
        post("/cp/contracts", {"request_id": "ct", "brand_id": "b1",
                               "channel": "instant_retail", "max_discount_rate": "0.5",
                               "effective_from": "2026-01-01"})
        cv = self.database.connection.execute("SELECT contract_id FROM cp_contracts").fetchone()
        post("/cp/contracts/approve", {"request_id": "cta", "contract_id": cv["contract_id"]},
             actor="rv1")
        post("/cp/promises", {"request_id": "pr", "promise_id": "pm",
                              "brand_id": "b1", "name": "立减", "channel": "instant_retail",
                              "fund_type": "general", "start_date": "2026-09-01",
                              "end_date": "2026-12-31", "budget_cap": "10000",
                              "components": [{"type": "off_invoice",
                                              "amount_per_case": "30"}]})
        post("/cp/promises/approve", {"request_id": "pra", "promise_id": "pm"}, actor="rv1")

        # 100 - 30 = 70 < 底价 90，提交即报告击穿来源。
        status, campaign = post("/cp/campaigns", {"request_id": "cp",
                                                  "brand_id": "b1",
                                                  "channel": "instant_retail", "region_id": "r1",
                                                  "store_id": "st1", "product_id": "p1",
                                                  "cases": "2", "activity_date": "2026-10-05",
                                                  "promise_ids": ["pm"]})
        self.assertEqual(201, status)
        codes = {c["code"] for c in campaign["conflicts"]}
        self.assertIn("PRICE_FLOOR_BREACH", codes)
        self.assertEqual(campaign["evaluation"]["net_price_per_case"], "70.00")

        # GET 活动能取回冲突。
        status, fetched = route(self.service, "GET", f"/cp/campaigns?id={campaign['resource_id']}",
                                None)
        self.assertEqual(200, status)
        self.assertTrue(fetched["conflicts"])


if __name__ == "__main__":
    unittest.main()
