"""多品牌渠道价盘与促销治理领域包。

在 beverage_ops_foundation 的稳定边界（操作者、事务、幂等回执、哈希审计链）
之上，保存按生效期版本化的产品层级、生命周期、区域底价、渠道合同、可叠加
规则与费用上限，提供活动提交冲突核算、紧急例外双人复核、订单规则快照与
结算费用追溯能力。
"""

from .service import GovernanceService

__all__ = ["GovernanceService"]
