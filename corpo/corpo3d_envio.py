# Quadro de POSE do corpo para o navegador: em vez do JPEG, manda os angulos (qpos inteiro, float32) e a camera.
# O site reconstroi a mosca em three.js a partir de site/fly-model.json (corpo/exportar_modelo.py).
# ~400 bytes por quadro a 30/s = 12 KB/s por espectador; o JPEG de 1280x540 a 60/s eram ~3 MB/s.
import struct
import json

import numpy as np


def quadro_pose(d, cab, alvo, az, el, dist):
    """Devolve (cabecalho, bytes) do quadro de pose. 'cab' e o mesmo cabecalho do quadro JPEG."""
    c = dict(cab)
    c['fmt'] = 'pose'
    c['cam'] = [round(float(alvo[0]), 4), round(float(alvo[1]), 4), round(float(alvo[2]), 4),
                round(float(az), 5), round(float(el), 5), round(float(dist), 3)]
    c['nq'] = int(d.qpos.shape[0])
    return c, np.asarray(d.qpos, dtype='<f4').tobytes()


def empacotar(cab, dados):
    j = json.dumps(cab, separators=(',', ':')).encode('utf-8')
    return struct.pack('<I', len(j)) + j + dados
