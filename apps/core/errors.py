from django.http import JsonResponse
from django.views import defaults

from .exceptions import error_payload


def bad_request(request, exception):
    if request.path.startswith("/api/"):
        return JsonResponse(error_payload("bad_request", "Invalid request."), status=400)
    return defaults.bad_request(request, exception)


def permission_denied(request, exception):
    if request.path.startswith("/api/"):
        return JsonResponse(error_payload("permission_denied", "Access denied."), status=403)
    return defaults.permission_denied(request, exception)


def not_found(request, exception):
    if request.path.startswith("/api/"):
        return JsonResponse(error_payload("not_found", "Endpoint not found."), status=404)
    return defaults.page_not_found(request, exception)


def server_error(request):
    if request.path.startswith("/api/"):
        return JsonResponse(error_payload("server_error", "Internal server error."), status=500)
    return defaults.server_error(request)
