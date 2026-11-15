import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings


class PushError(Exception):
    def __init__(self, code, retryable=False, uncertain=False):
        self.code, self.retryable, self.uncertain = code, retryable, uncertain
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def enabled():
    return bool(settings.EXPO_PUSH_ENABLED and settings.EXPO_PROJECT_ID and settings.EXPO_ACCESS_TOKEN)


def request_json(endpoint, payload):
    if not enabled():
        raise PushError("disabled")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + settings.EXPO_ACCESS_TOKEN,
    }
    req = Request(
        "https://exp.host/--/api/v2/push/" + endpoint,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(req, timeout=10) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise PushError("invalid_response", uncertain=True)
            result = json.loads(raw)
    except HTTPError as exc:
        raise PushError("http_" + str(exc.code), retryable=exc.code == 429 or exc.code >= 500) from None
    except (URLError, TimeoutError, OSError):
        raise PushError("network_unknown", uncertain=True) from None
    except (ValueError, UnicodeError):
        raise PushError("invalid_response", uncertain=True) from None
    if not isinstance(result, dict) or result.get("errors"):
        raise PushError("provider_error", uncertain=True)
    return result


def send(payload):
    result = request_json("send", [payload]).get("data")
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise PushError("invalid_ticket", uncertain=True)
    return result[0]


def receipts(ids):
    result = request_json("getReceipts", {"ids": ids}).get("data")
    if not isinstance(result, dict):
        raise PushError("invalid_receipts")
    return result
