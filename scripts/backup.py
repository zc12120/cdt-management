#!/usr/bin/env python3
"""Produce an operator-only consistent SQLite/key backup; no secret output."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3


def backup(source: Path, target: Path) -> None:
    os.umask(0o077)
    if target.exists():
        raise ValueError("Backup destination must be new")
    if not (source / "master.key").is_file():
        raise ValueError("Master key is missing")
    target.mkdir(parents=True, mode=0o700)
    key = (source / "master.key").read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid master key")
    with sqlite3.connect(
        f"file:{source / 'cdt-management.db'}?mode=ro", uri=True
    ) as db:
        with sqlite3.connect(target / "cdt-management.db") as out:
            db.backup(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
    if key != (source / "master.key").read_bytes():
        raise RuntimeError("Master key changed during backup")
    (target / "master.key").write_bytes(key)
    for file in target.iterdir():
        file.chmod(0o600)
    manifest = {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest()
        for file in target.iterdir()
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    backup(args.source, args.target)
    print("Consistent backup and integrity check completed")
