"""渠道价盘治理的领域枚举与纯规则函数。

本模块不触碰数据库，所有叠加、底价、费用适用性判断都是纯函数，
便于对「击穿价盘」「培育费用错配成熟产品」等核心场景做单元测试。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

# 产品层级：高端 / 次高档 / 大众
PRODUCT_TIERS = frozenset({"premium", "sub_premium", "mass"})

# 产品生命周期：导入期、培育期、成熟期、衰退期
LIFECYCLE_STAGES = frozenset({"introduction", "cultivation", "maturity", "decline"})

# 只有培育期（含导入期）产品允许使用培育专项费用。
CULTIVATION_ALLOWED_STAGES = frozenset({"introduction", "cultivation"})

# 渠道：商超、餐饮、即时零售
CHANNELS = frozenset({"supermarket", "restaurant", "instant_retail"})

# 优惠类型。
#   off_invoice  票面折让（按箱，直接降价）
#   billback     兑付返利（按箱，核销后返还）
#   instant      即时零售立减（按单笔，按金额封顶）
#   cultivation  培育专项费（按箱，仅培育/导入期产品可用）
#   emergency    紧急例外（按门店数量与期限限定）
DISCOUNT_TYPES = frozenset({"off_invoice", "billback", "instant", "cultivation", "emergency"})

# 默认每条规则只允许与同组或显式放行的规则叠加。
# 票面折让始终是成交价的第一道；培育专项费不进入消费者成交价，
# 只占用费用预算，因此不参与价盘击穿判定。
PRICE_AFFECTING_TYPES = frozenset({"off_invoice", "billback", "instant", "emergency"})
FEE_ONLY_TYPES = frozenset({"cultivation"})

# 折让计算口径
PER_CASE_TYPES = frozenset({"off_invoice", "billback", "cultivation", "emergency"})
PER_ORDER_TYPES = frozenset({"instant"})


def money(value: Any) -> Decimal:
    """把输入归一化为两位小数的金额。"""

    if isinstance(value, Decimal):
        decimal_value = value
    else:
        decimal_value = Decimal(str(value))
    return decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def as_cases(quantity: Any) -> Decimal:
    """把数量归一化为三位小数的箱数。"""

    return Decimal(str(quantity)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def cultivation_allowed(stage: str) -> bool:
    """培育专项费用是否可用于给定生命周期阶段。"""

    return stage in CULTIVATION_ALLOWED_STAGES


def resolve_lifecycle(versions: Iterable[dict[str, Any]], on_date: str) -> dict[str, Any] | None:
    """从按生效期排序的版本中找到某日生效的最新版本。

    每个版本带 effective_from / effective_to（左闭右开，to 可空表示至今）。
    选 effective_from <= on_date 中 effective_from 最大的一条。
    """

    candidates = [
        version
        for version in versions
        if version["effective_from"] <= on_date
        and (version.get("effective_to") is None or on_date < version["effective_to"])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda version: version["effective_from"])


def _per_case_amount(component: dict[str, Any], cases: Decimal) -> Decimal:
    amount = money(component["amount_per_case"])
    if amount < 0:
        raise ValueError("折让金额不能为负")
    return amount * cases


def evaluate_offer(
    *,
    list_price_per_case: Any,
    cases: Any,
    components: list[dict[str, Any]],
    floor_price_per_case: Any | None = None,
) -> dict[str, Any]:
    """计算全部优惠后的实际成交条件。

    components 每项形如：
        {"promise_id", "type", "amount_per_case"}          # 按箱
        {"promise_id", "type", "amount_per_order"}         # 按单笔（即时零售）
    返回逐项折让、成交价、是否击穿底价及击穿来源。
    金额一律使用 Decimal 计算，避免浮点误差。
    """

    list_price = money(list_price_per_case)
    case_count = as_cases(cases)
    list_total = (list_price * case_count).quantize(Decimal("0.01"))

    line_items: list[dict[str, Any]] = []
    price_discount_total = Decimal("0.00")
    fee_only_total = Decimal("0.00")

    for component in components:
        discount_type = component["type"]
        promise_id = component.get("promise_id")
        if discount_type in PER_CASE_TYPES:
            discount = _per_case_amount(component, case_count)
            rate_basis = "per_case"
        elif discount_type in PER_ORDER_TYPES:
            discount = money(component["amount_per_order"])
            if discount < 0:
                raise ValueError("立减金额不能为负")
            rate_basis = "per_order"
        else:
            raise ValueError(f"未知优惠类型: {discount_type}")

        line_items.append(
            {
                "promise_id": promise_id,
                "type": discount_type,
                "rate_basis": rate_basis,
                "discount_amount": discount,
                "affects_price": discount_type in PRICE_AFFECTING_TYPES,
            }
        )
        if discount_type in FEE_ONLY_TYPES:
            fee_only_total += discount
        else:
            price_discount_total += discount

    net_total = list_total - price_discount_total
    if case_count > 0:
        net_price_per_case = (net_total / case_count).quantize(Decimal("0.01"))
    else:
        net_price_per_case = list_price

    floor = money(floor_price_per_case) if floor_price_per_case is not None else None
    breach_sources: list[str] = []
    floor_breached = False
    if floor is not None and net_price_per_case < floor:
        floor_breached = True
        # 击穿来源：所有进入成交价的优惠，按折让额从大到小。
        breach_sources = [
            item["promise_id"]
            for item in sorted(
                (item for item in line_items if item["affects_price"]),
                key=lambda item: item["discount_amount"],
                reverse=True,
            )
        ]

    return {
        "list_price_per_case": list_price,
        "cases": case_count,
        "list_total": list_total,
        "line_items": line_items,
        "price_discount_total": price_discount_total.quantize(Decimal("0.01")),
        "fee_only_total": fee_only_total.quantize(Decimal("0.01")),
        "net_total": net_total.quantize(Decimal("0.01")),
        "net_price_per_case": net_price_per_case,
        "floor_price_per_case": floor,
        "floor_breached": floor_breached,
        "floor_breach_sources": breach_sources,
    }
