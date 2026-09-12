#!/usr/bin/env bash
# Para TUDO do FLY PAD no pod: o laco que reabre o gerente, o gerente e as moscas (cerebro, corpo, mercado).
# Use antes de subir de novo, quando sobrar processo duplicado:
#   bash gerente/parar_pod.sh && bash gerente/rodar_pod.sh
set -u
ALVOS="gerente/loop_pod.sh gerente/gerente.py brain/servidor.py corpo/corpo.py mercado/mercado.py"
for alvo in $ALVOS; do pkill -f "$alvo" || true; done
sleep 3
for alvo in $ALVOS; do pkill -9 -f "$alvo" || true; done
sleep 1
echo "restantes: loop=$(pgrep -fc gerente/loop_pod.sh || true) gerente=$(pgrep -fc gerente/gerente.py || true) cerebro=$(pgrep -fc brain/servidor.py || true) corpo=$(pgrep -fc corpo/corpo.py || true) mercado=$(pgrep -fc mercado/mercado.py || true)"
