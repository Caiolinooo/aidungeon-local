#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup_offsite.py -- Faz backup consistente do banco SQLite e do acervo de imagens (L7).

Exporta o banco aidungeon.db via API online sqlite3.backup() e compacta junto
com o diretório de imagens data/images/ (e opcionalmente outputs/), gerando um arquivo tar.gz
pronto para retenção off-site ou replicação.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path


def backup_database(src_db_path: Path, dst_db_path: Path) -> None:
    dst_db_path.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src_db_path))
    dst_conn = sqlite3.connect(str(dst_db_path))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def create_backup_archive(
    db_file: Path,
    images_dir: Path | None,
    output_archive: Path,
    extra_files: list[Path] | None = None,
) -> Path:
    output_archive.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_archive.parent / f"tmp_backup_{int(datetime.datetime.now().timestamp())}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        staged_db = temp_dir / "aidungeon.db"
        print(f"Snapshotting SQLite database {db_file} -> {staged_db}...")
        backup_database(db_file, staged_db)
        
        with tarfile.open(output_archive, "w:gz") as tar:
            tar.add(staged_db, arcname="aidungeon.db")
            if images_dir and images_dir.exists():
                print(f"Adding images directory {images_dir} to archive...")
                tar.add(images_dir, arcname="images")
            if extra_files:
                for f in extra_files:
                    if f.exists():
                        tar.add(f, arcname=f.name)
        
        print(f"Backup archive created successfully: {output_archive} ({output_archive.stat().st_size} bytes)")
        return output_archive
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup consistente de DB + Imagens para offsite (L7)")
    parser.add_argument("--db", type=Path, default=Path("data/aidungeon.db"), help="Caminho do banco SQLite")
    parser.add_argument("--images", type=Path, default=Path("data/images"), help="Diretório de imagens")
    parser.add_argument("--out-dir", type=Path, default=Path("backups"), help="Diretório de destino")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Erro: banco de dados {args.db} não encontrado.", file=sys.stderr)
        return 1

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_name = args.out_dir / f"aidungeon_backup_{timestamp}.tar.gz"
    create_backup_archive(args.db, args.images if args.images.exists() else None, archive_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
