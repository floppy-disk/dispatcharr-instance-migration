# Dispatcharr Dev → Prod Migration Tool

`v0.1.0-alpha`

Migrates state from a one Dispatcharr instance to another via the backup/restore API. Single Python script designed to run from Komodo or the command line. Built with the help of [Claude](https://claude.ai).

## How It Works

1. **Pre-flight** — Authenticates to both instances, verifies connectivity, warns on version mismatch
2. **Snapshot** — Saves prod core settings as a safety net
3. **Dev backup** — Creates a backup on dev via Celery task
4. **Transfer** — Downloads dev backup (signed token), uploads to prod
5. **Prod safety backup** — Creates a backup on prod and downloads it locally before restoring
6. **Restore** — Restores dev backup on prod, re-authenticates after DB flush
7. **Guardrails** — Diffs core settings and patches back any that drifted, triggers M3U refresh + EPG import
8. **Verify** — Confirms prod is alive, reports channel/M3U account/EPG source counts

## Requirements

- Python 3.9+
- `requests` (required)
- `python-dotenv` (optional, for `.env` file loading)

```bash
pip install requests python-dotenv
```

## Setup

Copy `.env.example` to `.env` and fill in your credentials:

```
DISPATCHARR_DEV_URL=https://tv-dev.example.tld
DISPATCHARR_PROD_URL=https://tv.example.tld
DISPATCHARR_DEV_USER=admin
DISPATCHARR_DEV_PASS=<password>
DISPATCHARR_PROD_USER=admin
DISPATCHARR_PROD_PASS=<password>
```

`python-dotenv` is optional — you can export vars directly or inject them via your pipeline tool (e.g. Komodo).

## Usage

```bash
# Full migration
python migrate.py

# Dry run — creates backup on dev and downloads it, but doesn't touch prod
python migrate.py --dry-run

# Skip M3U/EPG refresh after restore (useful if you want to control timing)
python migrate.py --skip-refresh

# Custom backup download directory
python migrate.py --backup-dir /tmp/dispatcharr-backups
```

Always do a `--dry-run` first to confirm auth works and a backup creates cleanly before running a full migration.

## Rollback

Every migration automatically creates a safety backup of prod before restoring. If something goes wrong:

```bash
# Rollback using the most recent backup in the backup directory
python migrate.py --rollback

# Rollback using a specific backup file
python migrate.py --rollback backups/dispatcharr-backup-2026.02.14.13.06.58.zip
```

The exact rollback command is printed at the end of each successful migration.

## Notes & Be Careful stuff...

**Version match is important.** The tool warns on version mismatch but won't stop you. Restoring a backup from a different Dispatcharr version can cause schema errors or silent data loss. Make sure both instances are running the same version before migrating.

**This is destructive on prod.** The restore operation flushes the prod database entirely before loading the dev backup. There is no partial merge — everything in prod is replaced. The prod safety backup (Phase 5) is your only safety net within this script.

**Active streams will be interrupted.** The restore causes a full database reload. Any clients currently streaming (HDHR, M3U) will be disconnected. This has been minimally impactful during my testing, but plan your migration window accordingly.

**Dynamic channels are not migrated.** Channels created by plugins or scripts that use IDs tied to a specific instance and that are replaced out of band from the tool will be overwritten with dev's versions. They will be recreated the next time the relevant plugin or scheduled task runs on prod. No manual action is needed, but it could impact your stream hashes based on your situation.

**M3U/EPG refresh is async.** The post-restore refresh calls are fire-and-forget — the tool triggers them and moves on. Depending on playlist size and provider speed, it may take a few minutes before streams are fully functional after migration.

**Core settings are instance-specific.** Things like server hostname, base URL, or feature flags that differ between dev and prod live in core settings. The tool snapshots prod's settings before the restore and patches them back automatically. Review the log output to confirm what (if anything) was patched.

**Users.** Both instances are assumed to share the same user accounts. Users are carried through the backup, so if your dev instance has different users than prod, prod's user list will be replaced after restore.

**The backup directory accumulates files.** Each run downloads at least two backup files (one from dev, one prod safety backup). These are not automatically cleaned up. Prune the `backups/` directory periodically.

**Run from a machine with access to both instances.** The script downloads the dev backup locally and re-uploads it to prod. It does not do a direct instance-to-instance transfer. Make sure the machine running the script can reach both URLs.

## Contribute

Feel free to add issue requests for bugs. I'm open to considering non-breaking features if it would be helpful to someone.

## License

MIT — see [LICENSE](LICENSE).