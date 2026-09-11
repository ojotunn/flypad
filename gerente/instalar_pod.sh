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
print('mujoco', mujoco.__version__, 'flygym', flygym.__version__)
EOF
mkdir -p brain/data/estado moscas
# dados do conectoma: chegam por rsync do PC (brain/data/flywire e brain/data/neuronios.parquet); confere
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
pkill -f 'gerente/gerente.py' || true
nohup bash -c 'while true; do python gerente/gerente.py; echo "[rodar] gerente saiu, reiniciando em 5 s"; sleep 5; done' >> /workspace/gerente.log 2>&1 &
echo "gerente no ar (log: /workspace/gerente.log)"
EOF
chmod +x gerente/rodar_pod.sh
echo "instalacao concluida em /workspace/flypad"
