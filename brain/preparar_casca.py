# Gera a casca do cerebro (malha do cerebro inteiro do FlyWire, via flybrains) no mesmo espaco normalizado
# dos neuronios de site/neurons.bin. Saida: site/shell.bin = [uint32 nv][uint32 nf][float32 xyz*nv][uint32 3*nf]
# Antes do lancamento, trocar pela malha da Janelia (CC BY); esta vem do template FAFB/FlyWire.
import json
import struct
from pathlib import Path
import numpy as np
import pandas as pd
import flybrains

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / 'brain' / 'data' / 'flywire'
SITE = RAIZ / 'site'

comp = pd.read_csv(DADOS / '2025_Completeness_783.csv', index_col=0)
ann = pd.read_csv(DADOS / 'flywire_annotations.tsv', sep='\t', low_memory=False,
                  usecols=['root_id', 'pos_x', 'pos_y', 'pos_z']).drop_duplicates('root_id').set_index('root_id')
m = comp.join(ann, how='left')
x = m['pos_x'].to_numpy(float)
y = m['pos_y'].to_numpy(float)
z = m['pos_z'].to_numpy(float) * 10.0          # mesmo espaco do preparar_dados: unidades de 4 nm
centro = np.array([(np.nanmin(v) + np.nanmax(v)) / 2 for v in (x, y, z)])
extensao = max(np.nanmax(v) - np.nanmin(v) for v in (x, y, z)) / 2

mesh = flybrains.FLYWIRE.mesh
v = np.asarray(mesh.vertices, dtype=np.float64) / 4.0   # nm -> unidades de 4 nm
f = np.asarray(mesh.faces, dtype=np.uint32)
vn = ((v - centro) / extensao).astype('<f4')
SITE.joinpath('shell.bin').write_bytes(struct.pack('<II', len(vn), len(f)) + vn.tobytes() + f.astype('<u4').tobytes())
SITE.joinpath('space.json').write_text(json.dumps({'centro': centro.tolist(), 'extensao': float(extensao)}))
print(f'casca: {len(vn)} vertices, {len(f)} faces; caixa x[{vn[:,0].min():.2f},{vn[:,0].max():.2f}] '
      f'y[{vn[:,1].min():.2f},{vn[:,1].max():.2f}] z[{vn[:,2].min():.2f},{vn[:,2].max():.2f}]')
