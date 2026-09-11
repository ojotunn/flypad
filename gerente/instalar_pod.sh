#!/usr/bin/env bash
# Instala o gerente do FLY PAD num pod do RunPod (template runpod/pytorch, Ubuntu + CUDA + torch).
# Tudo fica em /workspace (o volume do pod, que sobrevive a parar/ligar). Roda como root dentro do pod.
#   bash instalar_pod.sh            # instala/atualiza
#   bash /workspace/flypad/gerente/rodar_pod.sh   # sobe o gerente (criado por este script)
set -euo pipefail
cd /workspace
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq git rsync libgl1 libegl1 libosmesa6 xvfb > /dev/null

if [ ! -d flypad/.git ]; then
  git clone -q https://github.com/ojotunn/flypad.git
else
  git -C flypad pull -q
fi
cd flypad
python -m pip install -q --upgrade pip
python -m pip install -q -r gerente/requirements-pod.txt
python - <<'EOF'
import torch, mujoco, flygym
print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')
print('mujoco', mujoco.__version__, 'flygym importado de', flygym.__file__)
EOF
mkdir -p brain/data/estado moscas
# dados do conectoma: chegam do PC como /workspace/flypad_dados.tar.gz (parquet de conectividade, anotacoes,
# completude e neuronios.parquet; os caches weight_*.pkl sao regenerados aqui no CUDA na primeira carga)
if [ -f /workspace/flypad_dados.tar.gz ]; then tar -xzf /workspace/flypad_dados.tar.gz -C brain/data && echo "dados extraidos"; fi
if [ -f brain/data/neuronios.parquet ] && [ -d brain/data/flywire ]; then echo "dados: ok ($(du -sh brain/data/flywire | cut -f1))"; else echo "dados: FALTAM (rsync brain/data/flywire e neuronios.parquet)"; fi

cat > gerente/rodar_pod.sh <<'EOF'
#!/usr/bin/env bash
# Sobe o gerente do FLY PAD em segundo plano (nohup) e reinicia se cair. Log em /workspace/gerente.log
cd /workspace/flypad
export FLY_RELAY_URL="${FLY_RELAY_URL:-https://flypad-production-f6f5.up.railway.app}"
export FLY_MAX_ACORDADAS="${FLY_MAX_ACORDADAS:-8}"
export FLY_PASSOS_S="${FLY_PASSOS_S:-120}"
export FLY_CORPO_PISO=preto FLY_CORPO_CORES=real
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export FLY_CEREBRO_AFINIDADE=0 FLY_CORPO_AFINIDADE=0
export FLY_PLATAFORMA="${FLY_PLATAFORMA:-0x3EF754638fF72dC83693B3D099eEf2C335D804A7}"   # carteira do FLY PAD: recebe a taxa de lancamento
export FLY_TAXA_ETH="${FLY_TAXA_ETH:-0}"   # 0 = hatch de graca (fase de teste); as creator fees do token sao do dev
pkill -f 'gerente/loop_pod.sh' || true; pkill -f 'gerente/gerente.py' || true
pkill -f 'brain/servidor.py' || true; pkill -f 'corpo/corpo.py' || true; pkill -f 'mercado/mercado.py' || true   # o gerente novo reabre as moscas
sleep 2
nohup bash gerente/loop_pod.sh >> /workspace/gerente.log 2>&1 &
echo "gerente no ar (log: /workspace/gerente.log)"
EOF
cat > gerente/loop_pod.sh <<'EOF'
#!/usr/bin/env bash
# Mantem o gerente vivo (reabre se cair). Parar: pkill -f gerente/loop_pod.sh; pkill -f gerente/gerente.py
cd /workspace/flypad
while true; do python gerente/gerente.py; echo "[loop] gerente saiu, reiniciando em 5 s"; sleep 5; done
EOF
chmod +x gerente/rodar_pod.sh gerente/loop_pod.sh
echo "instalacao concluida em /workspace/flypad"
