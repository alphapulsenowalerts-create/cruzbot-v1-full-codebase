"""SQLite auto-backup helpers for CruzBot persistence."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")
def chicago_now() -> datetime:
    return datetime.now(_CT)


def backup_timestamp(when: Optional[datetime] = None) -> str:
    dt = when or chicago_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CT)
    else:
        dt = dt.astimezone(_CT)
    return dt.strftime("%Y%m%d_%H%M%S")


def seconds_until_chicago_midnight(now: Optional[datetime] = None) -> float:
    """Seconds until next America/Chicago local midnight (min 1s)."""
    from datetime import timedelta

    dt = now or chicago_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CT)
    else:
        dt = dt.astimezone(_CT)
    next_midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if next_midnight <= dt:
        next_midnight = next_midnight + timedelta(days=1)
    return max(1.0, (next_midnight - dt).total_seconds())


def _safe_copy_sqlite(src: Path, dest: Path) -> None:
    """Online-safe SQLite snapshot via backup API; fall back to shutil.copy2."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src_conn = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True)
        try:
            dest_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    except Exception as exc:
        logger.warning("sqlite backup API failed for %s (%s) — using copy2", src, exc)
        shutil.copy2(src, dest)


def prune_backups(backup_dir: Path, *, keep: int = 7) -> List[Path]:
    """Keep the newest ``keep`` backup files; delete older. Returns deleted paths."""
    keep_n = max(0, int(keep))
    if not backup_dir.exists():
        return []
    files = [p for p in backup_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    deleted: List[Path] = []
    for old in files[keep_n:]:
        try:
            old.unlink()
            deleted.append(old)
            logger.info("SQLITE_BACKUP pruned %s", old)
        except OSError as exc:
            logger.warning("SQLITE_BACKUP prune failed %s: %s", old, exc)
    return deleted


def backup_sqlite_dbs(
    sources: Sequence[Path | str],
    backup_dir: Path | str,
    *,
    keep: int = 7,
    when: Optional[datetime] = None,
) -> List[Path]:
    """
    Copy each existing SQLite DB into backup_dir with a timestamped filename.
    Creates backup_dir if missing. Keeps the last N backup files (global in dir).
    Returns list of written backup paths.
    """
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = backup_timestamp(when)
    written: List[Path] = []
    for raw in sources:
        src = Path(raw)
        if not src.exists() or not src.is_file():
            logger.debug("SQLITE_BACKUP skip missing %s", src)
            continue
        dest = out_dir / f"{src.stem}_{ts}{src.suffix}"
        try:
            _safe_copy_sqlite(src, dest)
            written.append(dest)
            logger.info("SQLITE_BACKUP wrote %s", dest)
        except Exception as exc:
            logger.warning("SQLITE_BACKUP failed for %s: %s", src, exc)
    prune_backups(out_dir, keep=keep)
    return written


def default_db_sources(project_root: Path, sqlite_path: str) -> List[Path]:
    """Primary trading DB + paper_ledger.db if present."""
    paths = [Path(sqlite_path)]
    ledger = project_root / "data" / "paper_ledger.db"
    if ledger.resolve() != Path(sqlite_path).resolve():
        paths.append(ledger)
    return paths
