"""Create private local configuration without printing secrets or overwriting files."""
import os
from pathlib import Path
import secrets


root = Path(__file__).resolve().parents[1]
target = root / ".env"
template = (root / ".env.example").read_text()
db_password = secrets.token_hex(32)
template = template.replace("replace-db-password", db_password)
for placeholder in (
    "replace-with-a-unique-random-secret", "replace-with-your-owner-login-secret",
    "replace-with-your-oauth-client-secret",
):
    template = template.replace(placeholder, secrets.token_urlsafe(48))
# The worker and n8n encryption key must differ too.
while "replace-with-another-random-secret" in template:
    template = template.replace("replace-with-another-random-secret", secrets.token_urlsafe(48), 1)
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env already exists; left it untouched.")
with os.fdopen(fd, "w") as file:
    file.write(template)
print("Created .env. Add your OpenAI API key and public domain there.")
