#!/bin/sh
# restart_service.sh -- Reinicia um serviço do app sem sudo.
#
#   DEPLOY_RESTART="tools/restart_service.sh aidungeon 2122" tools/deploy.sh --target main --ref <commit>
#
# Os serviços aidungeon e aidungeon-staging rodam como caio com Restart=always:
# basta encerrar o processo principal que o systemd sobe outro. O uvicorn, ao
# receber SIGTERM, espera as conexões abertas fecharem, e o stream de eventos
# do navegador nunca fecha: em 12/11/2026 produção ficou 2,5 min sem atender,
# presa em "Waiting for connections to close". Por isso: SIGTERM, até
# GRACE segundos de espera e, se o processo continuar lá, SIGKILL.
unit="$1"; port="$2"; grace="${RESTART_GRACE:-15}"
old=$(systemctl show "$unit" -p MainPID --value)
[ "$old" -gt 0 ] 2>/dev/null || { echo "sem MainPID para $unit" >&2; exit 1; }
kill -TERM "$old" || exit 1
waited=0
while kill -0 "$old" 2>/dev/null && [ "$waited" -lt "$grace" ]; do
    sleep 1; waited=$((waited + 1))
done
if kill -0 "$old" 2>/dev/null; then
    echo "$unit não saiu em ${grace}s com SIGTERM; SIGKILL"
    kill -KILL "$old" 2>/dev/null
fi
i=0
while [ $i -lt 60 ]; do
    sleep 2; i=$((i + 1))
    new=$(systemctl show "$unit" -p MainPID --value)
    if [ "$new" != "$old" ] && [ "$new" != "0" ] && \
       [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$port/api/adventures")" != "000" ]; then
        echo "$unit reiniciado: $old -> $new"; exit 0
    fi
done
echo "$unit não voltou em 120 s" >&2; exit 1
