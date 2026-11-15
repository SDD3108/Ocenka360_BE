"""Read-only catalog smoke check. No credentials, personal answers or writes."""

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
url = urlsplit(base)
if url.scheme not in {"http", "https"} or (
    url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1"}
):
    raise SystemExit("Use HTTPS or localhost.")


def get(path):
    try:
        with urlopen(base + "/api/v1/catalog/" + path, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        raise SystemExit(f"API returned {exc.code}. Check the URL and seed_catalog.") from None
    except URLError:
        raise SystemExit("Server unavailable. Start runserver first.") from None


categories = get("categories/?language=ru")
services = get("services/?language=ru")
print("Categories:", len(categories), "Services:", services["count"])
if not services["count"]:
    raise SystemExit("Catalog is empty. Run python manage.py seed_catalog.")
for lang in ("ru", "kk", "en"):
    service = get("services/apartment/?language=" + lang)
    form = get("services/apartment/form/?language=" + lang)
    print(lang, service["title"], "version:", form["version"], "fields:", len(form["fields"]))
print("PASS: public catalog and questionnaire are readable. No order was created.")
