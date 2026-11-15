import logging

from rest_framework.exceptions import NotAuthenticated, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def error_payload(code, message, details=None):
    return {"error": {"code": code, "message": str(message), "details": details or {}}}


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        # Never echo exception strings (they can contain SQL, credentials or documents).
        request_id = getattr(context.get("request"), "request_id", "unknown")
        logger.error("Unhandled API exception type=%s request_id=%s", type(exc).__name__, request_id)
        return Response(error_payload("server_error", "Internal server error."), status=500)
    if isinstance(exc, ValidationError):
        response.data = error_payload("validation_error", "Check the submitted fields.", response.data)
    else:
        code = getattr(exc, "default_code", "request_error")
        if isinstance(exc, NotAuthenticated):
            code = "not_authenticated"
        message = response.data.get("detail", "Request failed.")
        response.data = error_payload(code, message)
    return response
