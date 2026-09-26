from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "sqlite:///data/db/keiba.sqlite3")
    if database_url.startswith("sqlite:///"):
        source = Path(database_url.removeprefix("sqlite:///"))
        if source.exists():
            backup_dir = source.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            destination = backup_dir / f"{source.stem}-{datetime.now():%Y%m%d-%H%M%S}{source.suffix}"
            shutil.copy2(source, destination)
            print(f"backup: {destination}")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    print("migration: head")


if __name__ == "__main__":
    main()
