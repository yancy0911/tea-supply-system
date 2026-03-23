"""
URL configuration for tea_supply project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from tea_supply.views import register_view

# 后台管理仅老板（superuser）可进入；员工使用业务页面处理订单。
admin.site.has_permission = lambda request: bool(
    request.user.is_active and request.user.is_staff
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path("register/", register_view, name="register"),
    path("", include("main.urls")),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
