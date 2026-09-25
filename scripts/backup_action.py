#!/usr/bin/env python3
"""Run a dedicated backup workflow without exposing credentials to model tasks."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from banana_backup.files import atomic_write
from banana_backup.store import Store
from bananavibe.backups import export_package
from bananavibe.config import Config
from bananavibe.forge import Forge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".bananavibe.toml")
    args = parser.parse_args()
    required = ("BANANAVIBE_TOKEN", "BACKUP_REPO_TOKEN", "BACKUP_RECOVERY_KEY", "BACKUP_REPO", "BACKUP_FORGE", "BACKUP_NAME")
    if any(not os.environ.get(key, "").strip() for key in required):
        raise ValueError("Configure all backup repository variables and secrets before enabling this workflow.")
    with tempfile.TemporaryDirectory(prefix="bananavibe-backup-") as name:
        work = Path(name)
        token, identity = work / "backup.token", work / "recovery.agekey"
        atomic_write(token, os.environ["BACKUP_REPO_TOKEN"])
        atomic_write(identity, os.environ["BACKUP_RECOVERY_KEY"])
        store = Store(work / "store", "BananaVibe")
        store.configure(repo=os.environ["BACKUP_REPO"], forge=os.environ["BACKUP_FORGE"], name=os.environ["BACKUP_NAME"],
                        username=os.environ.get("BACKUP_USERNAME", "git") or "git", token_file=token, key_file=identity,
                        keep=int(os.environ.get("BACKUP_KEEP", "7") or "7"), max_mib=int(os.environ.get("BACKUP_MAX_MIB", "512") or "512"))
        forge = Forge(Config.load(args.config), os.environ["BANANAVIBE_TOKEN"])
        package = export_package(forge, args.config, work / "agent.tar.gz", maximum=store.settings()["max_mib"])
        print(json.dumps(store.upload(package), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print("BananaVibe backup:", str(error), file=sys.stderr)
        raise SystemExit(1) from None
