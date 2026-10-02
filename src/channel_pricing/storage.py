"""渠道价盘治理的 SQLite 表结构。

所有治理表使用 ``cp_`` 前缀，与 beverage_ops_foundation 的表共存在同一个
SQLite 数据库与事务边界内：操作者直接读取基础库的 actors 表，审计事件写入
基础库的 audit_events 哈希链，业务状态彼此独立。
"""

from __future__ import annotations

import sqlite3

# 基础库已经建立了 organizations / actors / sites / audit_events 等表。
# 这里只追加渠道价盘治理所需的 cp_ 前缀表，幂等建表、可重复执行。
GOVERNANCE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cp_brands (
    brand_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cp_regions (
    region_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cp_stores (
    store_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES cp_regions(region_id),
    channel TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cp_products (
    product_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES cp_brands(brand_id),
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 产品层级 / 生命周期 / 挂牌价按生效期版本化，生效后内容不可变，调价只追加新版本。
CREATE TABLE IF NOT EXISTS cp_product_versions (
    version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES cp_products(product_id),
    tier TEXT NOT NULL,
    lifecycle_stage TEXT NOT NULL,
    list_price_per_case TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','effective')),
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS cp_product_version_effective_uk
    ON cp_product_versions(product_id, effective_from) WHERE status = 'effective';

-- 区域底价同样按生效期版本化，永不重写。
CREATE TABLE IF NOT EXISTS cp_floor_versions (
    floor_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES cp_products(product_id),
    region_id TEXT NOT NULL REFERENCES cp_regions(region_id),
    floor_price_per_case TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','effective')),
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS cp_floor_effective_uk
    ON cp_floor_versions(product_id, region_id, effective_from) WHERE status = 'effective';

-- 渠道合同：商超 / 餐饮 / 即时零售；region_id 为空表示通用于该品牌渠道。
CREATE TABLE IF NOT EXISTS cp_contracts (
    contract_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES cp_brands(brand_id),
    channel TEXT NOT NULL,
    region_id TEXT,
    max_discount_rate TEXT,
    terms_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','effective')),
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS cp_contract_effective_uk
    ON cp_contracts(brand_id, channel, COALESCE(region_id, '*'), effective_from)
    WHERE status = 'effective';

-- 促销承诺：带渠道/区域/层级/生命周期适用范围、费用类型与预算上限。
CREATE TABLE IF NOT EXISTS cp_promises (
    promise_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES cp_brands(brand_id),
    name TEXT NOT NULL,
    channel TEXT NOT NULL,
    region_id TEXT REFERENCES cp_regions(region_id),
    fund_type TEXT NOT NULL CHECK(fund_type IN ('general','cultivation')),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    budget_cap TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','active','closed')),
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS cp_promise_components (
    component_id TEXT PRIMARY KEY,
    promise_id TEXT NOT NULL REFERENCES cp_promises(promise_id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount_per_case TEXT,
    amount_per_order TEXT,
    UNIQUE(promise_id, seq)
);
-- 可叠加规则：显式两两放行（无序对）。
CREATE TABLE IF NOT EXISTS cp_promise_stack_rules (
    rule_id TEXT PRIMARY KEY,
    promise_id_a TEXT NOT NULL REFERENCES cp_promises(promise_id),
    promise_id_b TEXT NOT NULL REFERENCES cp_promises(promise_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(promise_id_a, promise_id_b)
);

-- 紧急例外：限定单一门店、数量上限与期限，双人复核后生效。
CREATE TABLE IF NOT EXISTS cp_emergency_exceptions (
    exception_id TEXT PRIMARY KEY,
    promise_id TEXT NOT NULL REFERENCES cp_promises(promise_id),
    product_id TEXT NOT NULL REFERENCES cp_products(product_id),
    store_id TEXT NOT NULL REFERENCES cp_stores(store_id),
    max_cases TEXT NOT NULL,
    amount_per_case TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','rejected','closed')),
    requested_by TEXT NOT NULL,
    reviewer_id TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

-- 活动提交。
CREATE TABLE IF NOT EXISTS cp_campaigns (
    campaign_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    region_id TEXT NOT NULL,
    store_id TEXT,
    product_id TEXT NOT NULL,
    cases TEXT NOT NULL,
    activity_date TEXT NOT NULL,
    exception_id TEXT REFERENCES cp_emergency_exceptions(exception_id),
    status TEXT NOT NULL CHECK(status IN ('submitted','approved','rejected','closed')),
    evaluation_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS cp_campaign_promises (
    campaign_id TEXT NOT NULL REFERENCES cp_campaigns(campaign_id),
    promise_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, promise_id)
);
CREATE TABLE IF NOT EXISTS cp_campaign_conflicts (
    conflict_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES cp_campaigns(campaign_id),
    position INTEGER NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    sources_json TEXT NOT NULL
);

-- 生效订单：固化下单时解析到的规则版本与核算结果，后续调价不影响。
CREATE TABLE IF NOT EXISTS cp_orders (
    order_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES cp_campaigns(campaign_id),
    exception_id TEXT REFERENCES cp_emergency_exceptions(exception_id),
    store_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    cases TEXT NOT NULL,
    order_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('booked','settled','closed')),
    snapshot_json TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 结算事件：核销 / 退货 / 无效凭证，event_id 即业务幂等键。
CREATE TABLE IF NOT EXISTS cp_settlement_events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES cp_orders(order_id),
    promise_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('redemption','return','invalid_voucher')),
    cases TEXT NOT NULL,
    amount TEXT NOT NULL,
    occurrence_date TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 预算台账：reserve 活动批准预留；release_reserve 首次核销时把该活动预留转为实际；
-- redeem 核销实占；return/invalid 为负向冲回。
CREATE TABLE IF NOT EXISTS cp_budget_ledger (
    ledger_id TEXT PRIMARY KEY,
    promise_id TEXT NOT NULL REFERENCES cp_promises(promise_id),
    campaign_id TEXT REFERENCES cp_campaigns(campaign_id),
    order_id TEXT REFERENCES cp_orders(order_id),
    event_id TEXT REFERENCES cp_settlement_events(event_id),
    entry_type TEXT NOT NULL CHECK(entry_type IN
        ('reserve','redeem','release_reserve','return','invalid')),
    amount_cases TEXT NOT NULL,
    amount_money TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cp_budget_ledger_promise_idx ON cp_budget_ledger(promise_id);
CREATE INDEX IF NOT EXISTS cp_budget_ledger_event_idx ON cp_budget_ledger(event_id);

-- 治理域自己的幂等回执空间。
CREATE TABLE IF NOT EXISTS cp_request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_governance_schema(connection: sqlite3.Connection) -> None:
    """在基础库连接上幂等地追加治理表。"""

    connection.executescript(GOVERNANCE_SCHEMA)
