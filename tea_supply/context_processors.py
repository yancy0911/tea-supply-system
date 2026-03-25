from django.conf import settings


def currency(request):
    return {
        "currency_symbol": getattr(settings, "CURRENCY_SYMBOL", "$"),
    }

