#!/usr/bin/env bash
# deploy.sh -- Atualiza o staging ou o app principal na VM a partir do checkout
# git (G9). Roda na VM, como caio, de dentro do checkout (/home/caio/aidungeon-git):
#
#   tools/deploy.sh --target staging --ref origin/<branch> [--frontend]
#   tools/deploy.sh --target main --ref <commit> [--idle-min 10] [--force]
#   tools/deploy.sh --target main --rollback <dir de backup> [--with-db]
#
# Deploy:
#   1. --ref: fetch + checkout --detach e reexecuta a versão nova deste script;
#   2. backup do banco (API de backup do sqlite) e do código em
#      $BACKUPS/deploy-<alvo>-<data>/ (commit anterior em DEPLOYED);
#   3. rsync do código do checkout por cima do alvo, sem tocar em .env, data/
#      e backend/venv;
#   4. pip install se o requirements.txt mudou (o staging usa o venv do app
#      principal);
#   5. --frontend: npm ci && npm run build no checkout e rsync do dist. Sem a
#      flag o dist do alvo fica como está (o fonte da v0.9.0 não está no repo, K0);
#   6. restart do serviço (as migrations rodam no boot) e tools/smoke_check.py.
#      Smoke falhou: volta o código do backup, reinicia e sai 1.
# No app principal, recusa se o jogador mexeu nos últimos --idle-min minutos.
#
# Rollback: backup do estado atual, código do backup indicado e restart.
# --with-db também copia o banco do backup por cima (perde o que foi gravado
# depois dele).
#
# Variáveis DEPLOY_* trocam caminhos e comandos (testes fora da VM).
set -euo pipefail

CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUPS="${DEPLOY_BACKUPS:-/home/caio/aidungeon-backups}"
ENV_FILE="${DEPLOY_ENV_FILE:-/home/caio/aidungeon/.env}"
VENV="${DEPLOY_VENV:-/home/caio/aidungeon/backend/venv}"
MAIN_DB="${DEPLOY_MAIN_DB:-/home/caio/aidungeon/data/aidungeon.db}"
PY="${DEPLOY_PY:-python3}"
CODE=(backend/app backend/memory backend/migrations backend/schema.sql
      backend/requirements.txt backend/exporter.py backend/importer.py config)

TARGET="" REF="" ROLLBACK="" FRONTEND=0 WITH_DB=0 FORCE=0 IDLE_MIN=10
PASS=()

usage() {
    sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
    echo "deploy: $*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --target) TARGET="$2"; PASS+=("$1" "$2"); shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --rollback) ROLLBACK="$2"; PASS+=("$1" "$2"); shift 2 ;;
        --frontend) FRONTEND=1; PASS+=("$1"); shift ;;
        --with-db) WITH_DB=1; PASS+=("$1"); shift ;;
        --idle-min) IDLE_MIN="$2"; PASS+=("$1" "$2"); shift 2 ;;
        --force) FORCE=1; PASS+=("$1"); shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

case "$TARGET" in
    main) APP=/home/caio/aidungeon SERVICE=aidungeon PORT=2122 ;;
    staging) APP=/home/caio/aidungeon-staging SERVICE=aidungeon-staging PORT=2123 ;;
    *) usage >&2; exit 2 ;;
esac
APP="${DEPLOY_APP:-$APP}"
PORT="${DEPLOY_PORT:-$PORT}"
RESTART="${DEPLOY_RESTART:-sudo systemctl restart $SERVICE}"
STOP="${DEPLOY_STOP:-sudo systemctl stop $SERVICE}"

if [[ -n "$REF" ]]; then
    [[ -z "$(git -C "$CHECKOUT" status --porcelain --untracked-files=no)" ]] \
        || die "checkout com mudanças locais em $CHECKOUT"
    git -C "$CHECKOUT" fetch --quiet origin
    git -C "$CHECKOUT" checkout --quiet --detach "$REF"
    exec "$CHECKOUT/tools/deploy.sh" "${PASS[@]}"
fi

[[ -d "$APP/backend" && -f "$APP/data/aidungeon.db" ]] || die "$APP não parece um app instalado"
if [[ "$TARGET" == main && $FORCE -eq 0 ]]; then
    "$PY" "$CHECKOUT/tools/smoke_check.py" --player-idle-min "$IDLE_MIN" --idle-db "$MAIN_DB" \
        || die "jogador ativo no app principal (--force ignora)"
fi

smoke() {
    "$PY" "$CHECKOUT/tools/smoke_check.py" --base "http://127.0.0.1:$PORT" \
        --env-file "$ENV_FILE" --db "$APP/data/aidungeon.db"
}

backup() {
    local dest="$BACKUPS/deploy-$TARGET-$(date +%Y%m%d-%H%M%S)" items=()
    mkdir -p "$dest"
    "$PY" -c 'import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()' "$APP/data/aidungeon.db" "$dest/aidungeon.db"
    for item in "${CODE[@]}" frontend/dist DEPLOYED; do
        if [[ -e "$APP/$item" ]]; then
            items+=("$item")
        fi
    done
    tar czf "$dest/code.tgz" -C "$APP" --exclude=__pycache__ "${items[@]}"
    echo "$dest"
}

restore_code() {
    local tmp
    tmp="$(mktemp -d)"
    tar xzf "$1/code.tgz" -C "$tmp"
    for item in "${CODE[@]}" frontend/dist DEPLOYED; do
        if [[ -d "$tmp/$item" ]]; then
            mkdir -p "$APP/$item"
            rsync -a --delete --exclude=__pycache__ "$tmp/$item/" "$APP/$item/"
        elif [[ -f "$tmp/$item" ]]; then
            cp -p "$tmp/$item" "$APP/$item"
        elif [[ "$item" == DEPLOYED ]]; then
            rm -f "$APP/DEPLOYED"
        fi
    done
    rm -rf "$tmp"
}

requirements_sum() {
    sha256sum "$APP/backend/requirements.txt" 2>/dev/null | cut -d' ' -f1
}

if [[ -n "$ROLLBACK" ]]; then
    [[ -f "$ROLLBACK/code.tgz" ]] || die "sem $ROLLBACK/code.tgz"
    SAFETY="$(backup)"
    echo "estado atual salvo em $SAFETY"
    restore_code "$ROLLBACK"
    if ((WITH_DB)); then
        [[ -f "$ROLLBACK/aidungeon.db" ]] || die "sem $ROLLBACK/aidungeon.db"
        $STOP
        cp "$ROLLBACK/aidungeon.db" "$APP/data/aidungeon.db"
    fi
    $RESTART
    smoke || die "smoke falhou depois do rollback (estado anterior em $SAFETY)"
    echo "rollback de $TARGET para $ROLLBACK ok"
    exit 0
fi

COMMIT="$(git -C "$CHECKOUT" rev-parse HEAD)"
BACKUP="$(backup)"
echo "backup em $BACKUP"
BEFORE="$(requirements_sum)"

for item in "${CODE[@]}"; do
    if [[ -d "$CHECKOUT/$item" ]]; then
        mkdir -p "$APP/$item"
        rsync -a --delete --exclude=__pycache__ "$CHECKOUT/$item/" "$APP/$item/"
    else
        cp -p "$CHECKOUT/$item" "$APP/$item"
    fi
done
if [[ "$(requirements_sum)" != "$BEFORE" ]]; then
    "$VENV/bin/pip" install --quiet -r "$APP/backend/requirements.txt"
fi
if ((FRONTEND)); then
    (cd "$CHECKOUT/frontend" && npm ci --no-audit --no-fund && npm run build)
    mkdir -p "$APP/frontend/dist"
    rsync -a --delete "$CHECKOUT/frontend/dist/" "$APP/frontend/dist/"
fi
printf 'commit=%s\ndescribe=%s\ndeployed_at=%s\nbackup=%s\n' "$COMMIT" \
    "$(git -C "$CHECKOUT" describe --always --dirty)" "$(date -u +%FT%TZ)" "$BACKUP" \
    > "$APP/DEPLOYED"

$RESTART
if ! smoke; then
    echo "smoke falhou; voltando o código de $BACKUP" >&2
    restore_code "$BACKUP"
    $RESTART
    smoke || true
    exit 1
fi
echo "deploy de $COMMIT em $TARGET ok (backup: $BACKUP)"
