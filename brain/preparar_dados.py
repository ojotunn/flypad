# Prepara os dados da tela: posicao e regiao de cada neuronio, na ordem em que o modelo os indexa.
# Gera:
#   site/neurons.bin   float32 x,y,z normalizados em [-1,1] (3 valores por neuronio)
#   site/regions.bin   uint8 id da regiao de cada neuronio
#   site/regions.json  legenda (id, chave, nome em ingles, cor, contagem)
#   brain/data/neuronios.parquet  metadados p/ o servidor (regiao, classe, tipo, lado, neurotransmissor)
import json
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / 'brain' / 'data' / 'flywire'
SITE = RAIZ / 'site'

# super_class do FlyWire -> nome na tela (ingles) e cor
REGIOES = [
    ('optic',              'Optic lobes',                 '#3d6fd8'),
    ('central',            'Central brain',               '#f4d36a'),
    ('sensory',            'Sensory neurons',             '#4bd39b'),
    ('visual_projection',  'Visual projection',           '#39c8e8'),
    ('visual_centrifugal', 'Visual centrifugal',          '#2ba79b'),
    ('ascending',          'Ascending',                   '#c56ce2'),
    ('sensory_ascending',  'Sensory ascending',           '#82d86c'),
    ('descending',         'Descending (motor commands)', '#ff5e5e'),
    ('motor',              'Motor neurons',               '#ff9b3d'),
    ('endocrine',          'Endocrine',                   '#ff80b7'),
    ('unknown',            'Unannotated',                 '#6b6b6b'),
]
ID_DESCONHECIDO = len(REGIOES) - 1


def main():
    comp = pd.read_csv(DADOS / '2025_Completeness_783.csv', index_col=0)
    ann = pd.read_csv(
        DADOS / 'flywire_annotations.tsv', sep='\t', low_memory=False,
        usecols=['root_id', 'pos_x', 'pos_y', 'pos_z', 'super_class', 'cell_class',
                 'cell_type', 'hemibrain_type', 'side', 'top_nt'],
    ).drop_duplicates('root_id').set_index('root_id')
    m = comp.join(ann, how='left')
    assert len(m) == len(comp), 'join alterou a quantidade de neuronios'
    n = len(m)

    # x,y estao em voxels de 4 nm e z em voxels de 40 nm: multiplica z por 10 p/ ficar isotropico
    x = m['pos_x'].to_numpy(float)
    y = m['pos_y'].to_numpy(float)
    z = m['pos_z'].to_numpy(float) * 10.0
    ok = ~np.isnan(x)
    centro = np.array([(np.nanmin(v) + np.nanmax(v)) / 2 for v in (x, y, z)])
    extensao = max(np.nanmax(v) - np.nanmin(v) for v in (x, y, z)) / 2
    xyz = np.stack([x, y, z], 1)
    xyz = (xyz - centro) / extensao
    xyz[~ok] = 0.0

    chave2id = {ch: i for i, (ch, _, _) in enumerate(REGIOES)}
    reg = m["super_class"].map(chave2id).fillna(ID_DESCONHECIDO).astype(int).to_numpy().copy()
    reg[~ok] = ID_DESCONHECIDO

    SITE.mkdir(exist_ok=True)
    (SITE / 'neurons.bin').write_bytes(xyz.astype('<f4').tobytes())
    (SITE / 'regions.bin').write_bytes(reg.astype(np.uint8).tobytes())
    contagem = np.bincount(reg, minlength=len(REGIOES))
    legenda = [
        {'id': i, 'key': ch, 'name': nome, 'color': cor, 'count': int(contagem[i])}
        for i, (ch, nome, cor) in enumerate(REGIOES)
    ]
    (SITE / 'regions.json').write_text(json.dumps(
        {'n': n, 'unpositioned': int((~ok).sum()), 'regions': legenda}, indent=1))

    meta = pd.DataFrame({
        'root_id': m.index.astype('int64'),
        'regiao': reg.astype(np.int16),
        'super_class': m['super_class'].fillna('unknown').to_numpy(),
        'cell_class': m['cell_class'].fillna('').to_numpy(),
        'cell_type': m['cell_type'].fillna('').to_numpy(),
        'hemibrain_type': m['hemibrain_type'].fillna('').to_numpy(),
        'side': m['side'].fillna('').to_numpy(),
        'top_nt': m['top_nt'].fillna('').to_numpy(),
        'x': xyz[:, 0].astype(np.float32),
        'y': xyz[:, 1].astype(np.float32),
        'z': xyz[:, 2].astype(np.float32),
    })
    meta.to_parquet(RAIZ / 'brain' / 'data' / 'neuronios.parquet', index=False)
    print(f'neuronios: {n}  sem posicao: {int((~ok).sum())}')
    for r in legenda:
        print(f"  {r['id']:2d} {r['key']:<20} {r['count']:>7}")


if __name__ == '__main__':
    main()
