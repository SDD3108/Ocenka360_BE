from ipaddress import ip_address

from django.conf import settings


def client_ip(request):
    # Deployment contract: one loopback proxy OVERWRITES XFF, never appends it.
    value = request.META.get("REMOTE_ADDR", "unknown")
    if settings.REST_FRAMEWORK.get("NUM_PROXIES", 0) == 1:
        value = request.META.get("HTTP_X_FORWARDED_FOR", value)
    try:
        return str(ip_address(value.strip()))
    except (ValueError, AttributeError):
        return "unknown"
