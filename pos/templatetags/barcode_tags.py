from django import template
import barcode
from barcode.writer import ImageWriter
from io import BytesIO
import base64
from django.utils.safestring import mark_safe

register = template.Library()

@register.simple_tag
def barcode_image(sku, width=100):
    """
    Generate a Code128 barcode image (inline base64).
    """
    try:
        EAN = barcode.get_barcode_class("code128")
        ean = EAN(sku, writer=ImageWriter())
        buffer = BytesIO()
        ean.write(buffer)
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return mark_safe(f'<img src="data:image/png;base64,{b64}" width="{width}">')
    except Exception:
        return ""
