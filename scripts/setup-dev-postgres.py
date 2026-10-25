"""Provision ONLY the project-owned, loopback-only development database."""
from pathlib import Path
import hashlib
import json
import os
import secrets
import subprocess
import zipfile

root = Path(__file__).resolve().parents[1]
tools = root / ".devtools"
archive = tools / "postgresql-17.6-1-windows-x64-binaries.zip"
bin_dir = tools / "pgsql/bin"
if not bin_dir.exists():
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            if item.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                z.extract(item, tools)
state = root / "data/dev-postgres"
state.parent.mkdir(parents=True, exist_ok=True)
secret_file = root / "data/dev-postgres-password.txt"
if not state.exists():
    secret_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    subprocess.run([str(bin_dir / "initdb.exe"), "-D", str(state), "-U", "agi", "--pwfile=" + str(secret_file), "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"], check=True, capture_output=True)
    with (state / "postgresql.conf").open("a", encoding="utf-8") as stream:
        stream.write("\nlisten_addresses='127.0.0.1'\nport=55432\n")
status = subprocess.run([str(bin_dir / "pg_ctl.exe"), "-D", str(state), "status"], capture_output=True)
if status.returncode:
    subprocess.run([str(bin_dir / "pg_ctl.exe"), "-D", str(state), "-l", str(root / "data/dev-postgres.log"), "-w", "start"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
os.environ["PGPASSWORD"] = secret_file.read_text(encoding="utf-8")
db = subprocess.run([str(bin_dir / "psql.exe"), "-h", "127.0.0.1", "-p", "55432", "-U", "agi", "-d", "postgres", "-tAc", "SELECT 1 FROM pg_database WHERE datname='agi_wind_test'"], check=True, capture_output=True, text=True)
if db.stdout.strip() != "1":
    subprocess.run([str(bin_dir / "createdb.exe"), "-h", "127.0.0.1", "-p", "55432", "-U", "agi", "agi_wind_test"], check=True, capture_output=True)
url = "postgresql+psycopg://agi:" + os.environ["PGPASSWORD"] + "@127.0.0.1:55432/agi_wind_test"
(root / "data/dev-db.json").write_text(json.dumps({"url": url}), encoding="utf-8")
print("Project PostgreSQL 17.6 ready on 127.0.0.1:55432; password kept in ignored data/ only.")
print("Download SHA256:", hashlib.file_digest(archive.open("rb"), "sha256").hexdigest())
