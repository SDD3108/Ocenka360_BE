"""Create local .env once, using only the Python standard library."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
target = root / ".env"
content = (root / ".env.example").read_text(encoding="utf-8")
content = content.replace("REPLACE_WITH_RANDOM_SECRET", secrets.token_urlsafe(64))
try:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(".env already exists; left unchanged.")
else:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    print("Created .env with a unique LOCAL secret. Do not commit this file.")
