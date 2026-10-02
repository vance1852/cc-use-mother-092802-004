# 治理多品牌渠道价盘与促销承诺基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/channel_pricing/`：多品牌渠道价盘与促销治理领域（版本化价盘/底价/合同、促销叠加与费用上限、活动冲突核算、紧急例外双人复核、订单规则快照、结算幂等与财务追溯）；
- `tests/`：基础规则、事务边界、接口路由、治理规则/服务/接口和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 多品牌渠道价盘与促销治理

`channel_pricing` 与基础服务共用同一个 SQLite 数据库、操作者、事务与哈希审计链，治理表统一使用 `cp_` 前缀。它实现：

- **按生效期版本保存且生效不可变**：产品层级（高端/次高档/大众）、生命周期（导入/培育/成熟/衰退）、区域底价、渠道合同，调价只能追加新版本，由创建人之外的复核角色批准。
- **促销承诺**：保存渠道/区域/层级/生命周期适用范围、费用类型（普通费用与培育专项费分离）、可叠加两两放行规则和费用上限。
- **活动提交核算**：计算票面折让、兑付返利、即时立减、培育费与紧急例外叠加后的实际成交单价/总额，逐条输出冲突来源（`PRICE_FLOOR_BREACH` 击穿底价、`STACK_NOT_ALLOWED` 未放行叠加、`CULTIVATION_FUND_MISMATCH` 培育费错配成熟产品、`CONTRACT_RATE_EXCEEDED` 合同折扣率超限、`BUDGET_EXCEEDED` 费用超限等）；存在阻断冲突的活动不能批准。
- **紧急例外**：必须限定单一门店、数量上限与有效期限，申请与复核必须是不同的人且不同角色，下单时跨订单累计校验数量、门店与期限。
- **订单规则快照**：下单时刻重新解析并固化当时的产品版本/底价/合同/承诺及其内容哈希，后续调价追加版本不重写历史订单。
- **结算归因与幂等**：核销、退货、无效凭证必须归回订单引用的原促销承诺；`request_id` 与业务 `event_id` 双重幂等，重复回传不重复入账，退货/无效凭证冲回预算且数量受已核销约束。
- **财务追溯**：`trace-expense` 从一笔费用追查到促销承诺批准版本（内容哈希、批准人）、订单规则快照、预算台账流水与剩余额度。

治理域离线验收：

```bash
PYTHONPATH=src python3 -m channel_pricing.acceptance
```

治理 HTTP 服务（默认 8081 端口，与基础服务指向同一数据库文件即可共享操作者）：

```bash
PYTHONPATH=src python3 -m channel_pricing.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /cp/health`；主要写入入口为 `/cp/brands`、`/cp/product-versions`、`/cp/floor-versions`、`/cp/contracts`、`/cp/promises`、`/cp/stack-rules`、`/cp/emergency-exceptions`、`/cp/campaigns`、`/cp/orders`、`/cp/settlement-events`，查询与追溯使用 `GET /cp/campaigns`、`/cp/orders`、`/cp/budget`、`/cp/trace-expense`。
