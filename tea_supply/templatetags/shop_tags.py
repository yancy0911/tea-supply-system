import re

from django import template

register = template.Library()

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@register.filter(name="contains_cjk")
def contains_cjk(value):
    """True if value has CJK characters (hide on English storefront)."""
    if value is None:
        return False
    return bool(_CJK_RE.search(str(value)))


@register.filter(name="spec_en")
def spec_en(value):
    """Show label only if it has no CJK; otherwise em dash (for unit/case/category pills)."""
    if value is None:
        return "—"
    s = str(value).strip()
    if not s or _CJK_RE.search(s):
        return "—"
    return s
