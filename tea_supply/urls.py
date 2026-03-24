"""
URL configuration for tea_supply project.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from tea_supply.views import register_view

# 后台管理仅老板（superuser）可进入；员工使用业务页面处理订单。
admin.site.has_permission = lambda request: bool(
    request.user.is_active and request.user.is_staff
)

urlpatterns = [
    path("register/", register_view, name="register"),
    path("admin/", admin.site.urls),
    path("", include("main.urls")),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
