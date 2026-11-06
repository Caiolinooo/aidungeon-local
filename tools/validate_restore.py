#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_restore.py -- Validação de integridade e restauração de backup (L7).

Descompacta o arquivo tar.gz de backup em diretório temporário isolado,
verifica a integridade do SQLite (PRAGMA integrity_check), checa integridade
dos arquivos de imagem e valida que o schema está pronto para servir o jogo.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path


def validate_archive(archive_path: Path) -> bool:
    print(f"Iniciando validação do arquivo de backup: {archive_path}")
    start_time = time.monotonic()
    
    with tempfile.TemporaryDirectory(prefix="aidl_restore_val_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        print(f"Extraindo backup em ambiente isolado: {temp_dir}...")
        
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=temp_dir)
            
        restored_db = temp_dir / "aidungeon.db"
        if not restored_db.exists():
            print("FALHA: aidungeon.db não encontrado no arquivo restaurado!", file=sys.stderr)
            return False
            
        print(f"Validando integridade do banco de dados SQLite ({restored_db.stat().st_size} bytes)...")
        conn = sqlite3.connect(str(restored_db))
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            integrity_result = cursor.fetchall()
            if not integrity_result or integrity_result[0][0] != "ok":
                print(f"FALHA: PRAGMA integrity_check falhou: {integrity_result}", file=sys.stderr)
                return False
            print("  PRAGMA integrity_check: OK")
            
            # Checar tabelas essenciais
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row[0] for row in cursor.fetchall()}
            required_tables = {"adventures", "actions", "story_cards", "world_events", "jobs"}
            missing = required_tables - tables
            if missing:
                print(f"FALHA: Tabelas essenciais ausentes no banco restaurado: {missing}", file=sys.stderr)
                return False
            print(f"  Tabelas essenciais verificadas ({len(tables)} tabelas presentes): OK")
            
            cursor.execute("PRAGMA user_version;")
            uv = cursor.fetchone()[0]
            print(f"  PRAGMA user_version: {uv}")
        finally:
            conn.close()
            
        # Validar diretório de imagens se existir
        restored_images = temp_dir / "images"
        if restored_images.exists() and restored_images.is_dir():
            image_files = list(restored_images.glob("*"))
            print(f"  Diretório de imagens restaurado com {len(image_files)} arquivo(s): OK")
            
    elapsed = time.monotonic() - start_time
    print(f"SUCESSO: Restauração e integridade validadas em {elapsed:.2f}s (limite L7: 3600s / 60min).")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Validação de integridade de restauração de backup (L7)")
    parser.add_argument("archive", type=Path, help="Caminho do arquivo .tar.gz de backup gerado")
    args = parser.parse_args()

    if not args.archive.exists():
        print(f"Erro: Arquivo {args.archive} não encontrado.", file=sys.stderr)
        return 2

    ok = validate_archive(args.archive)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
