"""价盘与促销规则引擎：生效期查询、优惠叠加计算与冲突归因。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

# 优惠/费用允许使用的产品生命周期。
PROMO_LIFECYCLES = frozenset({"nurturing", "growth", "mature"})
TIERS = frozenset({"premium", "sub_premium", "mass"})
CHANNELS = frozenset({"hypermarket", "restaurant", "instant_retail"})

# 优惠手法分组，用于叠加规则判定。
MECHANIC_GROUPS = frozenset({"instant_discount", "coupon", "rebate", "gift"})


def money(cents: int) -> Decimal:
    """把分转换为元，便于展示。"""

    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def latest_effective(rows: Iterable[Any], as_of: str) -> Any | None:
    """返回 effective_from <= as_of 中生效时间最新的一行。"""

    candidates = [row for row in rows if row["effective_from"] <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row["effective_from"], row["version_id"]))


class RuleBook:
    """某个时点上的不可变规则快照，由治理服务在事务内组装。"""

    def __init__(self, *, as_of: str, product: Any, floor_rows: dict[str, Any],
                 contracts: dict[str, Any],
                 stack_rules: dict[tuple[str, str, str], tuple[bool, str]],
                 funds: dict[str, Any], caps: list[Any]) -> None:
        self.as_of = as_of
        self.product = product
        self.floor_rows = floor_rows
        self.contracts = contracts
        # (channel, group_a, group_b) 且 group_a <= group_b -> (是否允许, 规则版本)
        self.stack_rules = stack_rules
        self.funds = funds
        self.caps = caps

    def floor_for(self, region: str) -> int | None:
        row = self.floor_rows.get(region)
        return row["floor_cents"] if row else None

    def contract_for(self, channel: str) -> Any | None:
        return self.contracts.get(channel)

    def stack_allowed(self, channel: str, group_a: str, group_b: str) -> bool:
        """查询某渠道下两个手法分组能否叠加；未登记的配对默认禁止。"""

        if group_a == group_b:
            return True
        key = (channel, *sorted((group_a, group_b)))
        entry = self.stack_rules.get(key)
        return bool(entry and entry[0])

    def stack_rule_version(self, channel: str, group_a: str, group_b: str) -> str | None:
        key = (channel, *sorted((group_a, group_b)))
        entry = self.stack_rules.get(key)
        return entry[1] if entry else None

    def cap_for(self, fund_id: str, product_id: str, channel: str) -> Any | None:
        for cap in self.caps:
            if (cap["fund_id"] == fund_id and cap["product_id"] == product_id
                    and cap["channel"] == channel):
                return cap
        return None


def _bps_amount(base_cents: int, bps: int) -> int:
    """按基点（万分之一）计算折让金额，四舍五入到分。"""

    return int((Decimal(base_cents) * Decimal(bps) / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def evaluate_mechanics(rulebook: RuleBook, *, channel: str, region: str,
                       mechanics: list[dict[str, Any]], as_of: str) -> dict[str, Any]:
    """计算所有优惠后的实际成交条件，并给出每一个冲突的来源。

    每个手法形如：
      {"mechanic_id", "group", "type": "bps"|"cents", "value", "fund_id"?}
    group 取值 instant_discount/coupon/rebate/gift。
    返回净价、各项折让明细、冲突列表（含来源版本）与适用的费用上限。
    """

    product = rulebook.product
    list_price = product["list_price_cents"]
    lifecycle = product["lifecycle"]
    tier = product["tier"]

    conflicts: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    total_discount = 0

    contract = rulebook.contract_for(channel)
    contract_bps = contract["discount_bps"] if contract else 0
    contract_vid = contract["version_id"] if contract else None

    # 渠道合同价是计算起点（合同折让）。
    contract_discount = _bps_amount(list_price, contract_bps)
    running_price = list_price - contract_discount
    applications.append({
        "source": "channel_contract", "version_id": contract_vid,
        "group": "contract", "discount_cents": contract_discount,
    })

    seen_groups: set[str] = set()
    used_funds: list[str] = []
    cap_hits: list[dict[str, Any]] = []

    for index, mechanic in enumerate(mechanics):
        mechanic_id = str(mechanic.get("mechanic_id", f"mechanic-{index + 1}"))
        group = mechanic.get("group")
        if group not in MECHANIC_GROUPS:
            conflicts.append({
                "code": "unknown_mechanic_group", "mechanic_id": mechanic_id,
                "message": f"手法分组 {group} 不受支持", "source_version_id": None,
            })
            continue
        mtype = mechanic.get("type")
        value = int(mechanic.get("value", 0))
        if mtype == "bps":
            if not 0 <= value <= 9999:
                conflicts.append({"code": "invalid_bps", "mechanic_id": mechanic_id,
                                  "message": "基点折让必须在 0..9999 之间", "source_version_id": None})
                continue
            discount = _bps_amount(running_price, value)
        elif mtype == "cents":
            if value < 0:
                conflicts.append({"code": "invalid_cents", "mechanic_id": mechanic_id,
                                  "message": "定额折让不能为负", "source_version_id": None})
                continue
            discount = value
        else:
            conflicts.append({"code": "unknown_mechanic_type", "mechanic_id": mechanic_id,
                              "message": f"折让类型 {mtype} 不受支持", "source_version_id": None})
            continue

        # 叠加规则：与已经应用的每个分组逐一核对，冲突要能指回规则版本。
        for prior in sorted(seen_groups):
            if not rulebook.stack_allowed(channel, prior, group):
                conflicts.append({
                    "code": "stack_forbidden", "mechanic_id": mechanic_id,
                    "conflict_with": prior,
                    "message": f"渠道 {channel} 禁止 {prior} 与 {group} 叠加",
                    "source_version_id": rulebook.stack_rule_version(channel, prior, group),
                })

        fund_id = mechanic.get("fund_id")
        fund = rulebook.funds.get(fund_id) if fund_id else None
        if fund_id and fund is None:
            conflicts.append({"code": "fund_not_effective", "mechanic_id": mechanic_id,
                              "fund_id": fund_id, "message": "引用的费用方案在该时点未生效",
                              "source_version_id": None})
        elif fund is not None:
            used_funds.append(fund_id)
            # 培育费用不得用于成熟产品（成长/成熟产品不能套用 nurturing 专项费用）。
            if fund["lifecycle_scope"] == "nurturing" and lifecycle != "nurturing":
                conflicts.append({
                    "code": "fund_lifecycle_mismatch", "mechanic_id": mechanic_id,
                    "fund_id": fund_id, "product_lifecycle": lifecycle,
                    "message": "培育专项费用只能用于培育期产品",
                    "source_version_id": fund["version_id"],
                })
            cap = rulebook.cap_for(fund_id, product["product_id"], channel)
            if cap is None:
                conflicts.append({
                    "code": "cap_missing", "mechanic_id": mechanic_id, "fund_id": fund_id,
                    "message": "费用方案缺少该产品/渠道/期间的费用上限",
                    "source_version_id": fund["version_id"],
                })
            else:
                cap_hits.append({"fund_id": fund_id, "cap_version_id": cap["version_id"],
                                 "cap_id": cap["version_id"], "amount_cents": cap["amount_cents"],
                                 "period_start": cap["period_start"], "period_end": cap["period_end"]})

        running_price -= discount
        total_discount += discount
        seen_groups.add(group)
        applications.append({
            "source": "mechanic", "mechanic_id": mechanic_id, "group": group,
            "fund_id": fund_id, "discount_cents": discount,
        })

    floor_row = rulebook.floor_rows.get(region)
    floor_cents = floor_row["floor_cents"] if floor_row else None
    floor_version = floor_row["version_id"] if floor_row else None
    if floor_cents is None:
        conflicts.append({"code": "floor_missing", "region": region,
                          "message": "该区域没有生效中的底价", "source_version_id": None})
    else:
        if running_price < floor_cents:
            conflicts.append({
                "code": "price_below_floor", "region": region,
                "net_cents": running_price, "floor_cents": floor_cents,
                "message": f"叠加后成交价 {money(running_price)} 元击穿区域底价 {money(floor_cents)} 元",
                "source_version_id": floor_version,
            })

    return {
        "as_of": as_of,
        "product_id": product["product_id"],
        "product_version_id": product["version_id"],
        "tier": tier,
        "lifecycle": lifecycle,
        "channel": channel,
        "region": region,
        "list_price_cents": list_price,
        "contract": {"channel": channel, "discount_bps": contract_bps,
                     "discount_cents": contract_discount, "version_id": contract_vid},
        "floor_cents": floor_cents,
        "floor_version_id": floor_version,
        "total_discount_cents": list_price - running_price,
        "unit_net_cents": running_price,
        "applications": applications,
        "funds": used_funds,
        "caps": cap_hits,
        "conflicts": conflicts,
        "rule_version_ids": _collect_version_ids(rulebook, channel, region, mechanics),
    }


def _collect_version_ids(rulebook: RuleBook, channel: str, region: str,
                         mechanics: list[dict[str, Any]]) -> dict[str, str]:
    ids: dict[str, str] = {"product": rulebook.product["version_id"]}
    contract = rulebook.contract_for(channel)
    if contract:
        ids["channel_contract"] = contract["version_id"]
    floor_row = rulebook.floor_rows.get(region)
    if floor_row:
        ids["floor_price"] = floor_row["version_id"]
    for fund_id, fund in rulebook.funds.items():
        ids[f"fund:{fund_id}"] = fund["version_id"]
        cap = rulebook.cap_for(fund_id, rulebook.product["product_id"], channel)
        if cap:
            ids[f"cap:{fund_id}"] = cap["version_id"]
    for (rule_channel, group_a, group_b), entry in rulebook.stack_rules.items():
        if rule_channel == channel:
            ids[f"stack:{group_a}|{group_b}"] = entry[1]
    return ids
