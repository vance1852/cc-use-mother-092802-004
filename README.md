# 治理多品牌渠道价盘与促销承诺基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

在基础能力之上，`promo_service` / `pricing` 模块实现了多品牌渠道价盘与促销治理：

- **按生效期保存且只追加的价盘规则**：产品层级（高端/次高档/大众）与生命周期（培育/成长/成熟）、区域底价、渠道合同折让、渠道内优惠手法可叠加规则、费用方案与按产品/渠道/期间的费用上限。调价只能发布新生效期版本，同一生效时点重复发布被拒绝，历史版本永不被覆盖。
- **活动提交即算清实际条件**：按活动开始时点解析全部生效规则，依次计算渠道合同折让、立减、优惠券、返利等手法后的实际净价；每个冲突（击穿区域底价、禁止叠加、培育费用错配成熟产品、缺少费用上限等）都带有造成冲突的规则版本号。
- **审批分离与紧急例外**：合规活动进入待批准，必须由非提交人的管理/复核角色批准；被价盘拦截的活动可申请紧急例外，强制限定门店子集、数量上限（不超过计划量）和到期时间，并必须由不同角色双人复核。
- **订单引用下单当时规则**：订单保存批准时规则版本与下单时规则版本两份快照；后续调价只影响新生效期之后的订单，已成交订单的净价与引用版本不被重写。
- **结算归回原促销承诺**：核销必须挂订单与费用方案并校验费用上限；退货与无效凭证必须引用原核销凭证，冲回金额不得超过原核销，额度随之恢复；`(促销版本, 外部凭证号)` 唯一键与请求回执共同保证重复回传幂等。
- **财务可追查**：从任一笔结算凭证可追到原核销凭证、订单及其规则快照、促销批准版本与批准人、紧急例外记录，以及费用上限版本的已用/剩余额度。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、价盘促销治理服务与离线验收；
- `tests/`：基础规则、事务边界、接口路由、价盘冲突、紧急例外、订单快照、结算幂等、财务追查和端到端验收测试。

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
PYTHONPATH=src python3 -m beverage_ops_foundation.promo_acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链；促销验收则走通规则发布、冲突拦截、紧急例外双人复核、下单快照、后续调价不改历史、结算核销/退货与财务追查全链路。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

促销治理接口（均以 `request_id` 保证幂等）：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /price-products` | 发布产品层级/生命周期/厂价的生效期版本 |
| `POST /floor-prices` | 发布区域底价的生效期版本 |
| `POST /channel-contracts` | 发布品牌×渠道×区域合同折让版本 |
| `POST /stack-rules` | 发布渠道内优惠手法可叠加规则 |
| `POST /funds` / `POST /expense-caps` | 发布费用方案与费用上限 |
| `POST /promotions/submit` | 提交活动，返回净价计算与全部冲突来源 |
| `POST /promotions/approve` / `/reject` | 不同角色复核活动 |
| `POST /emergency-exceptions/request` / `/review` | 紧急例外申请与双人复核 |
| `POST /orders` | 按订单下单当时规则成交并冻结规则版本 |
| `POST /settlements` | 核销 / 退货 / 无效凭证回传，重复回传幂等 |
| `GET /promotions/version?promotion_version_id=` | 查看活动版本与评估明细 |
| `GET /expenses/trace?external_voucher_id=` | 财务追查：凭证→订单→批准版本→上限额度 |
