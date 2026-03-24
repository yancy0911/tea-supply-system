"""
挂账订单与客户 current_debt 联动：下单计入、收款/取消回冲；幂等字段 Order.is_debt_counted。
"""
from django.core.exceptions import ValidationError

from .models import Customer, Order
from .money_utils import money_dec, money_float, money_q2


def apply_credit_debt_if_needed(order):
    """
    挂账订单首次计入客户欠款（下单成功或历史未入账订单在确认时补计）；幂等。
    仅 settlement_type=CREDIT 且已绑定客户时生效；现金/转账等非挂账不处理。
    """
    order = Order.objects.select_for_update().get(pk=order.pk)
    if (
        order.settlement_type != Order.SettlementType.CREDIT
        or not order.customer_id
        or order.is_debt_counted
    ):
        return
    cust = Customer.objects.select_for_update().get(pk=order.customer_id)
    latest_debt = money_dec(cust.current_debt or 0.0)
    latest_limit = money_dec(cust.credit_limit or 0.0)
    order_amt = money_dec(order.total_revenue or 0.0)
    if latest_debt >= latest_limit:
        raise ValidationError("额度已用完，无法挂账本单")
    if latest_debt + order_amt > latest_limit:
        raise ValidationError("本次挂账将超出信用额度，无法挂账本单")
    cust.current_debt = money_float(max(money_dec(0), latest_debt + order_amt))
    cust.save(update_fields=["current_debt"])
    order.is_debt_counted = True
    order.save(update_fields=["is_debt_counted"])


def reverse_credit_debt_if_counted(order):
    """
    挂账订单在收款、取消等场景回冲欠款；仅当本单曾计入欠款（is_debt_counted）时执行，幂等。
    """
    order = Order.objects.select_for_update().get(pk=order.pk)
    if (
        order.settlement_type != Order.SettlementType.CREDIT
        or not order.customer_id
        or not order.is_debt_counted
    ):
        return
    cust = Customer.objects.select_for_update().get(pk=order.customer_id)
    new_debt = money_q2(money_dec(cust.current_debt or 0.0) - money_dec(order.total_revenue or 0.0))
    cust.current_debt = money_float(max(money_dec(0), new_debt))
    cust.save(update_fields=["current_debt"])
    order.is_debt_counted = False
    order.save(update_fields=["is_debt_counted"])
