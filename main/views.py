from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def dashboard(request):
    # Step 1: dashboard 骨架（后续会接入订单/收入/利润/库存预警等数据）
    return render(request, "home.html")
