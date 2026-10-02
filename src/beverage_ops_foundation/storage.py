"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_versions (
    version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    brand_id TEXT NOT NULL,
    name TEXT NOT NULL,
    tier TEXT NOT NULL CHECK(tier IN ('premium', 'sub_premium', 'mass')),
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('nurturing', 'growth', 'mature')),
    list_price_cents INTEGER NOT NULL CHECK(list_price_cents > 0),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(product_id, effective_from)
);
CREATE TABLE IF NOT EXISTS floor_price_versions (
    version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    region TEXT NOT NULL,
    floor_cents INTEGER NOT NULL CHECK(floor_cents >= 0),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(product_id, region, effective_from)
);
CREATE TABLE IF NOT EXISTS channel_contract_versions (
    version_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('hypermarket', 'restaurant', 'instant_retail')),
    region TEXT NOT NULL,
    discount_bps INTEGER NOT NULL CHECK(discount_bps BETWEEN 0 AND 9999),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(brand_id, channel, region, effective_from)
);
CREATE TABLE IF NOT EXISTS stack_rule_versions (
    version_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    from_group TEXT NOT NULL,
    to_group TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(channel, from_group, to_group, effective_from)
);
CREATE TABLE IF NOT EXISTS fund_versions (
    version_id TEXT PRIMARY KEY,
    fund_id TEXT NOT NULL,
    name TEXT NOT NULL,
    lifecycle_scope TEXT NOT NULL CHECK(lifecycle_scope IN ('nurturing', 'any')),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(fund_id, effective_from)
);
CREATE TABLE IF NOT EXISTS expense_cap_versions (
    version_id TEXT PRIMARY KEY,
    fund_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(fund_id, product_id, channel, period_start, period_end, effective_from)
);
CREATE TABLE IF NOT EXISTS promotions (
    promotion_id TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_versions (
    version_id TEXT PRIMARY KEY,
    promotion_id TEXT NOT NULL REFERENCES promotions(promotion_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('blocked', 'pending_approval', 'approved', 'rejected')),
    product_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    region TEXT NOT NULL,
    store_ids_json TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    planned_units INTEGER NOT NULL CHECK(planned_units > 0),
    mechanics_json TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    snapshot_json TEXT,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    exception_id TEXT,
    UNIQUE(promotion_id, version_no)
);
CREATE TABLE IF NOT EXISTS emergency_exceptions (
    exception_id TEXT PRIMARY KEY,
    promotion_version_id TEXT NOT NULL UNIQUE REFERENCES promotion_versions(version_id),
    store_ids_json TEXT NOT NULL,
    quantity_limit INTEGER NOT NULL CHECK(quantity_limit > 0),
    deadline TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
    requested_by TEXT NOT NULL REFERENCES actors(actor_id),
    reviewed_by TEXT REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS promo_orders (
    order_id TEXT PRIMARY KEY,
    promotion_version_id TEXT NOT NULL REFERENCES promotion_versions(version_id),
    store_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_list_cents INTEGER NOT NULL,
    unit_net_cents INTEGER NOT NULL,
    total_net_cents INTEGER NOT NULL,
    rule_snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_entries (
    entry_id TEXT PRIMARY KEY,
    promotion_version_id TEXT NOT NULL REFERENCES promotion_versions(version_id),
    order_id TEXT REFERENCES promo_orders(order_id),
    fund_id TEXT,
    cap_version_id TEXT,
    original_voucher_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('writeoff', 'return', 'invalid')),
    external_voucher_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(promotion_version_id, external_voucher_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
