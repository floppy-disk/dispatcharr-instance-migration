#!/usr/bin/env python3
"""
Dispatcharr Dev → Prod Migration Tool

Migrates state from a dev Dispatcharr instance to prod via the backup/restore API.
Designed to run from Komodo as: python migrate.py

Phases:
  1. Pre-flight: authenticate, verify connectivity, compare versions
  2. Snapshot prod core settings (safety net)
  3. Create backup on dev
  4. Transfer dev backup file from dev to prod
  5. Create safety backup on prod (before restore)
  6. Restore dev backup on prod
  7. Post-restore guardrails (re-apply prod settings, refresh M3U/EPG)
  8. Verification

Rollback:
  python migrate.py --rollback                    # restore most recent prod safety backup
  python migrate.py --rollback <backup-file>      # restore a specific local backup file
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

__version__ = "0.1.0-alpha"

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("migrate")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEV_URL = os.environ.get("DISPATCHARR_DEV_URL", "").rstrip("/")
PROD_URL = os.environ.get("DISPATCHARR_PROD_URL", "").rstrip("/")
DEV_USER = os.environ.get("DISPATCHARR_DEV_USER", "")
DEV_PASS = os.environ.get("DISPATCHARR_DEV_PASS", "")
PROD_USER = os.environ.get("DISPATCHARR_PROD_USER", "")
PROD_PASS = os.environ.get("DISPATCHARR_PROD_PASS", "")

POLL_INTERVAL = 3  # seconds between status polls
POLL_TIMEOUT = 300  # max seconds to wait for a Celery task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DispatcharrClient:
    """Thin wrapper around requests for a single Dispatcharr instance."""

    def __init__(self, base_url: str, label: str):
        self.base_url = base_url
        self.label = label
        self.session = requests.Session()
        self.token = None

    def authenticate(self, username: str, password: str):
        """Obtain a JWT access token."""
        log.info("[%s] Authenticating as %s ...", self.label, username)
        resp = self.session.post(
            f"{self.base_url}/api/accounts/token/",
            json={"username": username, "password": password},
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        log.info("[%s] Authenticated.", self.label)

    def get(self, path: str, **kwargs) -> requests.Response:
        resp = self.session.get(f"{self.base_url}{path}", **kwargs)
        resp.raise_for_status()
        return resp

    def post(self, path: str, **kwargs) -> requests.Response:
        resp = self.session.post(f"{self.base_url}{path}", **kwargs)
        resp.raise_for_status()
        return resp

    def patch(self, path: str, **kwargs) -> requests.Response:
        resp = self.session.patch(f"{self.base_url}{path}", **kwargs)
        resp.raise_for_status()
        return resp


def poll_task(client: DispatcharrClient, task_id: str, description: str) -> dict:
    """Poll a Celery task until it completes or times out."""
    log.info("[%s] Polling task %s (%s) ...", client.label, task_id, description)
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > POLL_TIMEOUT:
            raise TimeoutError(
                f"Task {task_id} ({description}) did not complete within {POLL_TIMEOUT}s"
            )

        resp = client.get(f"/api/backups/status/{task_id}/")
        data = resp.json()
        state = (data.get("state") or data.get("status", "UNKNOWN")).upper()

        if state in ("SUCCESS", "COMPLETE", "COMPLETED"):
            log.info(
                "[%s] Task %s completed in %.1fs.", client.label, task_id, elapsed
            )
            return data
        if state in ("FAILURE", "FAILED", "REVOKED"):
            raise RuntimeError(
                f"Task {task_id} ({description}) failed: {json.dumps(data, indent=2)}"
            )

        log.info(
            "[%s] Task %s state=%s (%.0fs elapsed) ...",
            client.label,
            task_id,
            state,
            elapsed,
        )
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Phase 1: Pre-flight
# ---------------------------------------------------------------------------


def preflight(dev: DispatcharrClient, prod: DispatcharrClient) -> None:
    log.info("=" * 60)
    log.info("PHASE 1: Pre-flight checks")
    log.info("=" * 60)

    # Authenticate
    dev.authenticate(DEV_USER, DEV_PASS)
    prod.authenticate(PROD_USER, PROD_PASS)

    # Verify connectivity / get versions
    dev_ver = dev.get("/api/core/version/").json()
    prod_ver = prod.get("/api/core/version/").json()
    log.info("[DEV]  Version: %s", json.dumps(dev_ver))
    log.info("[PROD] Version: %s", json.dumps(prod_ver))

    if dev_ver != prod_ver:
        log.warning(
            "Version mismatch between dev and prod! Migration may still work, "
            "but verify compatibility."
        )

    log.info("Pre-flight checks passed.")


# ---------------------------------------------------------------------------
# Phase 2: Snapshot prod core settings
# ---------------------------------------------------------------------------


def snapshot_prod_settings(prod: DispatcharrClient) -> list[dict]:
    log.info("=" * 60)
    log.info("PHASE 2: Snapshot prod core settings")
    log.info("=" * 60)

    resp = prod.get("/api/core/settings/")
    settings = resp.json()
    log.info("Captured %d prod core setting(s).", len(settings))
    for s in settings:
        log.info("  %s (id=%s): %s", s.get("key"), s.get("id"), s.get("value"))
    return settings


# ---------------------------------------------------------------------------
# Phase 3 / 5: Create backup (generic)
# ---------------------------------------------------------------------------


def create_backup(client: DispatcharrClient) -> str:
    """Create a backup on the given instance and return the filename."""
    resp = client.post("/api/backups/create/")
    data = resp.json()
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        raise RuntimeError(f"No task_id in backup create response: {data}")

    log.info("[%s] Backup creation started, task_id=%s", client.label, task_id)
    poll_task(client, task_id, f"backup creation on {client.label.lower()}")

    # List backups and find the newest one
    resp = client.get("/api/backups/")
    backups = resp.json()
    if not backups:
        raise RuntimeError(f"No backups found on {client.label} after creation.")

    # Backups are typically returned as a list; pick the most recent
    if isinstance(backups, list):
        entry = backups[0]
        newest = entry if isinstance(entry, str) else entry.get("filename") or entry.get("name")
    else:
        # If it's a dict with a list inside
        backup_list = backups.get("backups", backups.get("results", []))
        if not backup_list:
            raise RuntimeError(f"Unexpected backup list format: {backups}")
        entry = backup_list[0]
        newest = entry if isinstance(entry, str) else entry.get("filename") or entry.get("name")

    log.info("[%s] Newest backup: %s", client.label, newest)
    return newest


def download_backup(client: DispatcharrClient, filename: str, backup_dir: Path) -> Path:
    """Download a backup file from the given instance to local disk."""
    resp = client.get(f"/api/backups/{filename}/download-token/")
    token_data = resp.json()
    download_token = token_data.get("download_token") or token_data.get("token")
    log.info("[%s] Got download token for %s", client.label, filename)

    backup_dir.mkdir(parents=True, exist_ok=True)
    local_path = backup_dir / filename
    log.info("[%s] Downloading backup to %s ...", client.label, local_path)

    resp = client.get(
        f"/api/backups/{filename}/download/",
        params={"download_token": download_token},
        stream=True,
    )
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    size_mb = local_path.stat().st_size / (1024 * 1024)
    log.info("[%s] Downloaded %.2f MB to %s", client.label, size_mb, local_path)
    return local_path


# ---------------------------------------------------------------------------
# Phase 4: Transfer backup
# ---------------------------------------------------------------------------


def upload_backup(client: DispatcharrClient, local_path: Path) -> None:
    """Upload a local backup file to the given instance."""
    filename = local_path.name
    log.info("[%s] Uploading backup %s ...", client.label, filename)
    with open(local_path, "rb") as f:
        resp = client.post(
            "/api/backups/upload/",
            files={"file": (filename, f)},
        )
    log.info("[%s] Backup uploaded: %s", client.label, resp.status_code)


def transfer_backup(
    dev: DispatcharrClient,
    prod: DispatcharrClient,
    filename: str,
    backup_dir: Path,
    dry_run: bool,
) -> Path:
    log.info("=" * 60)
    log.info("PHASE 4: Transfer dev backup")
    log.info("=" * 60)

    local_path = download_backup(dev, filename, backup_dir)

    if dry_run:
        log.info("[DRY RUN] Skipping upload to prod.")
        return local_path

    upload_backup(prod, local_path)
    return local_path


# ---------------------------------------------------------------------------
# Phase 5: Create safety backup on prod (before restore)
# ---------------------------------------------------------------------------


def create_prod_safety_backup(prod: DispatcharrClient, backup_dir: Path) -> Path:
    log.info("=" * 60)
    log.info("PHASE 5: Create safety backup on prod (pre-restore)")
    log.info("=" * 60)

    filename = create_backup(prod)
    local_path = download_backup(prod, filename, backup_dir)
    log.info("[PROD] Safety backup saved to: %s", local_path)
    log.info("[PROD] Use --rollback %s to restore if needed.", local_path)
    return local_path


# ---------------------------------------------------------------------------
# Phase 6: Restore on prod
# ---------------------------------------------------------------------------


def restore_on_prod(prod: DispatcharrClient, filename: str) -> None:
    log.info("=" * 60)
    log.info("PHASE 6: Restore dev backup on prod")
    log.info("=" * 60)

    resp = prod.post(f"/api/backups/{filename}/restore/")
    data = resp.json()
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        raise RuntimeError(f"No task_id in restore response: {data}")

    log.info("[PROD] Restore started, task_id=%s", task_id)
    poll_task(prod, task_id, "restore on prod")

    # Re-authenticate after restore (DB was flushed and reloaded)
    log.info("[PROD] Re-authenticating after restore ...")
    prod.authenticate(PROD_USER, PROD_PASS)


# ---------------------------------------------------------------------------
# Phase 7: Post-restore guardrails
# ---------------------------------------------------------------------------


def post_restore_guardrails(
    prod: DispatcharrClient,
    original_settings: list[dict],
    skip_refresh: bool,
) -> None:
    log.info("=" * 60)
    log.info("PHASE 7: Post-restore guardrails")
    log.info("=" * 60)

    # Compare core settings
    current = prod.get("/api/core/settings/").json()
    current_by_key = {s["key"]: s for s in current}
    original_by_key = {s["key"]: s for s in original_settings}

    drifted = []
    for key, orig in original_by_key.items():
        cur = current_by_key.get(key)
        if cur is None:
            log.warning("Setting %s missing after restore!", key)
            drifted.append(orig)
        elif cur.get("value") != orig.get("value"):
            log.warning(
                "Setting %s changed: %s → %s",
                key,
                orig.get("value"),
                cur.get("value"),
            )
            drifted.append(orig)

    if drifted:
        log.info("Re-applying %d prod-specific setting(s) ...", len(drifted))
        for orig in drifted:
            setting_id = current_by_key.get(orig["key"], {}).get("id", orig.get("id"))
            prod.patch(
                f"/api/core/settings/{setting_id}/",
                json={"value": orig["value"]},
            )
            log.info("  Patched %s (id=%s) back to %s", orig["key"], setting_id, orig["value"])
    else:
        log.info("No core setting drift detected. All good.")

    # Refresh M3U and EPG
    if skip_refresh:
        log.info("Skipping M3U/EPG refresh (--skip-refresh).")
        return

    log.info("[PROD] Triggering M3U refresh ...")
    try:
        prod.post("/api/m3u/refresh/")
        log.info("[PROD] M3U refresh triggered.")
    except requests.HTTPError as e:
        log.warning("[PROD] M3U refresh failed: %s", e)

    log.info("[PROD] Triggering EPG import ...")
    try:
        prod.post("/api/epg/import/")
        log.info("[PROD] EPG import triggered.")
    except requests.HTTPError as e:
        log.warning("[PROD] EPG import failed: %s", e)


# ---------------------------------------------------------------------------
# Phase 8: Verification
# ---------------------------------------------------------------------------


def verify(prod: DispatcharrClient) -> None:
    log.info("=" * 60)
    log.info("PHASE 8: Verification")
    log.info("=" * 60)

    # Version check (prod is alive)
    ver = prod.get("/api/core/version/").json()
    log.info("[PROD] Version: %s", json.dumps(ver))

    # Channels
    resp = prod.get("/api/channels/channels/")
    channels_data = resp.json()
    if isinstance(channels_data, list):
        count = len(channels_data)
    elif isinstance(channels_data, dict):
        count = channels_data.get("count", len(channels_data.get("results", [])))
    else:
        count = "unknown"
    log.info("[PROD] Channels: %s", count)

    # M3U accounts
    resp = prod.get("/api/m3u/accounts/")
    accounts = resp.json()
    if isinstance(accounts, list):
        acct_count = len(accounts)
    else:
        acct_count = len(accounts.get("results", []))
    log.info("[PROD] M3U accounts: %s", acct_count)

    # EPG sources
    resp = prod.get("/api/epg/sources/")
    sources = resp.json()
    if isinstance(sources, list):
        src_count = len(sources)
    else:
        src_count = len(sources.get("results", []))
    log.info("[PROD] EPG sources: %s", src_count)

    log.info("=" * 60)
    log.info("Migration complete!")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def find_latest_prod_backup(backup_dir: Path) -> Path:
    """Find the most recent prod safety backup in the backup directory."""
    candidates = sorted(backup_dir.glob("dispatcharr-backup-*.zip"), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            f"No backup files found in {backup_dir}. "
            "Specify a backup file path explicitly with --rollback <path>."
        )
    log.info("Found %d backup(s) in %s, using newest: %s", len(candidates), backup_dir, candidates[0].name)
    return candidates[0]


def rollback(backup_arg: str, backup_dir: Path, skip_refresh: bool) -> None:
    """Restore prod from a local backup file."""
    log.info("=" * 60)
    log.info("ROLLBACK MODE")
    log.info("=" * 60)

    # Resolve backup file
    if backup_arg == "latest":
        local_path = find_latest_prod_backup(backup_dir)
    else:
        local_path = Path(backup_arg)

    if not local_path.is_file():
        log.error("Backup file not found: %s", local_path)
        sys.exit(1)

    size_mb = local_path.stat().st_size / (1024 * 1024)
    log.info("Rollback file: %s (%.2f MB)", local_path, size_mb)

    # Only need prod for rollback
    if not PROD_URL or not PROD_USER or not PROD_PASS:
        log.error("Missing DISPATCHARR_PROD_URL/USER/PASS environment variables.")
        sys.exit(1)

    prod = DispatcharrClient(PROD_URL, "PROD")
    prod.authenticate(PROD_USER, PROD_PASS)

    # Verify prod is reachable
    ver = prod.get("/api/core/version/").json()
    log.info("[PROD] Version: %s", json.dumps(ver))

    # Upload the backup
    upload_backup(prod, local_path)

    # Restore
    filename = local_path.name
    log.info("[PROD] Restoring from %s ...", filename)
    resp = prod.post(f"/api/backups/{filename}/restore/")
    data = resp.json()
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        raise RuntimeError(f"No task_id in restore response: {data}")

    poll_task(prod, task_id, "rollback restore on prod")

    # Re-authenticate after restore
    prod.authenticate(PROD_USER, PROD_PASS)

    # Refresh M3U/EPG
    if not skip_refresh:
        log.info("[PROD] Triggering M3U refresh ...")
        try:
            prod.post("/api/m3u/refresh/")
            log.info("[PROD] M3U refresh triggered.")
        except requests.HTTPError as e:
            log.warning("[PROD] M3U refresh failed: %s", e)

        log.info("[PROD] Triggering EPG import ...")
        try:
            prod.post("/api/epg/import/")
            log.info("[PROD] EPG import triggered.")
        except requests.HTTPError as e:
            log.warning("[PROD] EPG import failed: %s", e)

    # Quick verification
    ver = prod.get("/api/core/version/").json()
    log.info("[PROD] Version after rollback: %s", json.dumps(ver))

    log.info("=" * 60)
    log.info("Rollback complete!")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Dispatcharr state from dev to prod via backup/restore."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run phases 1-4 (backup + download) but skip upload/restore to prod.",
    )
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Skip M3U/EPG refresh after restore.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.cwd() / "backups",
        help="Directory to store downloaded backup files.",
    )
    parser.add_argument(
        "--rollback",
        nargs="?",
        const="latest",
        default=None,
        metavar="BACKUP_FILE",
        help=(
            "Rollback prod to a safety backup. Pass a path to a specific backup file, "
            "or omit the path to use the most recent prod safety backup in --backup-dir."
        ),
    )
    args = parser.parse_args()

    # Validate config
    missing = []
    if not DEV_URL:
        missing.append("DISPATCHARR_DEV_URL")
    if not PROD_URL:
        missing.append("DISPATCHARR_PROD_URL")
    if not DEV_USER:
        missing.append("DISPATCHARR_DEV_USER")
    if not DEV_PASS:
        missing.append("DISPATCHARR_DEV_PASS")
    if not PROD_USER:
        missing.append("DISPATCHARR_PROD_USER")
    if not PROD_PASS:
        missing.append("DISPATCHARR_PROD_PASS")
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        log.error("Set them in your environment or in a .env file.")
        sys.exit(1)

    # --- Rollback mode ---
    if args.rollback is not None:
        rollback(args.rollback, args.backup_dir, args.skip_refresh)
        return

    # --- Normal migration ---
    log.info("Dev:  %s", DEV_URL)
    log.info("Prod: %s", PROD_URL)
    if args.dry_run:
        log.info("*** DRY RUN MODE — prod will not be modified ***")

    dev = DispatcharrClient(DEV_URL, "DEV")
    prod = DispatcharrClient(PROD_URL, "PROD")

    # Phase 1
    preflight(dev, prod)

    # Phase 2
    original_settings = snapshot_prod_settings(prod)

    # Phase 3
    log.info("=" * 60)
    log.info("PHASE 3: Create backup on dev")
    log.info("=" * 60)
    backup_filename = create_backup(dev)

    # Phase 4
    local_path = transfer_backup(dev, prod, backup_filename, args.backup_dir, args.dry_run)

    if args.dry_run:
        log.info("*** DRY RUN COMPLETE ***")
        log.info("Backup saved to: %s", local_path)
        log.info("Re-run without --dry-run to apply to prod.")
        return

    # Phase 5 — safety backup on prod before we overwrite it
    prod_safety_path = create_prod_safety_backup(prod, args.backup_dir)

    # Phase 6
    restore_on_prod(prod, backup_filename)

    # Phase 7
    post_restore_guardrails(prod, original_settings, args.skip_refresh)

    # Phase 8
    verify(prod)

    log.info("")
    log.info("To rollback prod to pre-migration state:")
    log.info("  python migrate.py --rollback %s", prod_safety_path)


if __name__ == "__main__":
    main()
