"""促销治理测试的共享夹具：组织、角色与一套双品牌价盘规则。"""

from __future__ import annotations

from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.promo_service import PromoGovernanceService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


def ts(year: int, month: int, day: int, hour: int = 0) -> str:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class PromoFixture:
    """搭建两个品牌、三类角色和覆盖三个渠道的价盘规则。"""

    def __init__(self, now: datetime | None = None) -> None:
        self.database = Database()
        clock = FixedClock(now or datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = PromoGovernanceService(self.database, clock)
        self._seq = 0

        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="酒业集团")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                 display_name="销售经理", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                 display_name="复核人", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        self.base.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                 display_name="销售经理二", role="operator", organization_id="o1")

    def req(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def close(self) -> None:
        self.database.close()

    def publish_standard_rules(self) -> None:
        """高端培育产品 + 成熟大众产品，三渠道合同与底价、叠加规则、费用上限。"""

        s = self.service
        # 高端培育产品，厂价 1000 元/瓶。
        s.publish_product_hierarchy(request_id=self.req("r"), actor_id="a1", product_id="p-premium",
                                    brand_id="b1", name="珍藏30年", tier="premium",
                                    lifecycle="nurturing", list_price_cents=100000,
                                    effective_from=ts(2026, 1, 1))
        # 成熟大众产品，厂价 100 元/瓶。
        s.publish_product_hierarchy(request_id=self.req("r"), actor_id="a1", product_id="p-mass",
                                    brand_id="b1", name="家常光瓶", tier="mass",
                                    lifecycle="mature", list_price_cents=10000,
                                    effective_from=ts(2026, 1, 1))
        # 华东底价：高端 800，大众 85。
        s.publish_floor_price(request_id=self.req("r"), actor_id="a1", product_id="p-premium",
                              region="east", floor_cents=80000, effective_from=ts(2026, 1, 1))
        s.publish_floor_price(request_id=self.req("r"), actor_id="a1", product_id="p-mass",
                              region="east", floor_cents=8500, effective_from=ts(2026, 1, 1))
        # 渠道合同折让：商超 5%、餐饮 8%、即时零售 3%。
        s.publish_channel_contract(request_id=self.req("r"), actor_id="a1", brand_id="b1",
                                   channel="hypermarket", region="east", discount_bps=500,
                                   effective_from=ts(2026, 1, 1))
        s.publish_channel_contract(request_id=self.req("r"), actor_id="a1", brand_id="b1",
                                   channel="restaurant", region="east", discount_bps=800,
                                   effective_from=ts(2026, 1, 1))
        s.publish_channel_contract(request_id=self.req("r"), actor_id="a1", brand_id="b1",
                                   channel="instant_retail", region="east", discount_bps=300,
                                   effective_from=ts(2026, 1, 1))
        # 商超允许 立减+优惠券；优惠券+返利禁止叠加（任何渠道默认禁止，这里显式登记禁止）。
        s.publish_stack_rule(request_id=self.req("r"), actor_id="a1", channel="hypermarket",
                             from_group="instant_discount", to_group="coupon", allowed=True,
                             effective_from=ts(2026, 1, 1))
        s.publish_stack_rule(request_id=self.req("r"), actor_id="a1", channel="hypermarket",
                             from_group="coupon", to_group="rebate", allowed=False,
                             effective_from=ts(2026, 1, 1))
        # 培育专项费用与通用费用。
        s.publish_fund(request_id=self.req("r"), actor_id="a1", fund_id="f-nurture",
                       name="培育产品专项", lifecycle_scope="nurturing",
                       effective_from=ts(2026, 1, 1))
        s.publish_fund(request_id=self.req("r"), actor_id="a1", fund_id="f-general",
                       name="通用渠道费用", lifecycle_scope="any",
                       effective_from=ts(2026, 1, 1))
        # 费用上限：高端/商超 10 月 5 万元。
        s.publish_expense_cap(request_id=self.req("r"), actor_id="a1", fund_id="f-nurture",
                              product_id="p-premium", channel="hypermarket",
                              period_start=ts(2026, 10, 1), period_end=ts(2026, 10, 31, 23),
                              amount_cents=5_000_000, effective_from=ts(2026, 1, 1))
        s.publish_expense_cap(request_id=self.req("r"), actor_id="a1", fund_id="f-general",
                              product_id="p-premium", channel="hypermarket",
                              period_start=ts(2026, 10, 1), period_end=ts(2026, 10, 31, 23),
                              amount_cents=2_000_000, effective_from=ts(2026, 1, 1))
