"""Interactive local API smoke client; never prints or persists tokens."""

import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
if not (
    base.startswith("https://")
    or base.startswith("http://127.0.0.1:")
    or base.startswith("http://localhost:")
):
    raise SystemExit("Use HTTPS or localhost for this demo.")


def api(path, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(base + "/api/v1/" + path, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read()
            return json.loads(content) if content else None
    except HTTPError as exc:
        print("API error", exc.code, exc.read().decode())
        raise SystemExit(1) from None


language = input("Language [ru/kk/en], default ru: ").strip() or "ru"
documents = api("legal/documents/?language=" + language)
email = input("Email (use a test address in console mode): ").strip()
challenge = api("auth/request-code/", {"email": email, "language": language})
print("Code requested. In console mode, read it in the RUNSERVER terminal.")
code = input("6-digit code: ").strip()
tokens = api(
    "auth/verify-code/", {"challenge_id": challenge["challenge_id"], "code": code, "device_name": "CLI demo"}
)
print("Signed in:", tokens["user"]["email"], "role:", tokens["user"]["role"])
print("Tokens are kept in memory only and are not printed.")
if tokens["user"]["requires_consent"]:
    # Existing users may have a different saved language than the email request.
    language = tokens["user"]["preferred_language"]
    documents = api("legal/documents/?language=" + language)
    for document in documents:
        print("\n", document["title"], "version:", document["version"])
        print(document["content"])
    accept = input("After reading, do you accept ALL these documents? Type YES: ") == "YES"
    adult = input("Are you at least 18? Type YES: ") == "YES"
    if accept and adult:
        api(
            "legal/consents/",
            {"documents": [doc["id"] for doc in documents], "adult_confirmed": True},
            tokens["access"],
        )
        print("Acceptance recorded.")
    else:
        print("No acceptance recorded.")
profile = api("accounts/me/", token=tokens["access"])
print("Requires consent:", profile["requires_consent"])
tokens = api("auth/refresh/", {"refresh": tokens["refresh"]})
print("Token rotation succeeded.")
api("auth/logout/", {}, tokens["access"])
print("Logged out; this application's session is revoked.")
