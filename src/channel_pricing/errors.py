"""渠道价盘治理领域使用的业务异常。"""


class GovernanceError(Exception):
    """所有可预期业务异常的基类。"""

    code = "governance_error"
    status = 400


class ValidationError(GovernanceError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(GovernanceError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(GovernanceError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(GovernanceError):
    """请求编号、业务唯一键或版本状态与既有内容冲突。"""

    code = "conflict"
    status = 409


class StateConflict(GovernanceError):
    """对象状态不允许当前操作（如版本已生效后被修改）。"""

    code = "state_conflict"
    status = 409


class BudgetExceeded(GovernanceError):
    """促销费用超出了预算上限。"""

    code = "budget_exceeded"
    status = 422


class PriceFloorBreached(GovernanceError):
    """优惠叠加后的实际成交价击穿了区域底价。"""

    code = "price_floor_breached"
    status = 422
