from django.apps import AppConfig


class TeaSupplyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tea_supply"

    def ready(self):
        # Django 4.2 的 BaseContext.__copy__ 实现为：
        #   duplicate = copy(super()); duplicate.dicts = self.dicts[:]
        # 在较新的 Python 上，copy(super()) 可能得到 super 代理对象，没有 .dicts，
        # 会在复制模板上下文时触发全局错误（admin changelist、auth/user 等）。
        # 此处用等价且安全的复制方式替换，不改业务逻辑。
        from django.template.context import BaseContext

        def _basecontext_copy(self):
            duplicate = object.__new__(self.__class__)
            duplicate.dicts = self.dicts[:]
            return duplicate

        BaseContext.__copy__ = _basecontext_copy
