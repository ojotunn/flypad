# Exporta o modelo da mosca (NeuroMechFly compilado pelo MuJoCo, ja com as cores de mosca real) para o navegador:
#   site/fly-model.json  - corpos (arvore), juntas (tipo, eixo, posicao, endereco em qpos), geoms, materiais, malhas
#   site/fly-model.bin   - por malha: posicoes float32, uv float32 (se houver) e indices uint16/uint32, alinhados a 4
#   site/fly-tex/*.png   - texturas recoloridas (as mesmas que o render local usa), sem repeticao, no maximo 512 px
# O site reconstroi a cinematica direta (mj_kinematics) em three.js e recebe so os angulos (qpos) a cada quadro:
# ~400 bytes por quadro em vez de um JPEG de 40 KB. As malhas originais somam 502 mil triangulos (48 MB soltos);
# aqui sao decimadas (quadricas, fast_simplification) mantendo a uv por vertice, para ~1,5 MB no total.
# Rodar uma vez (e de novo se o modelo/cores mudarem):
#   py\Scripts\python.exe corpo\exportar_modelo.py
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('FLY_CORPO_CORES', 'real')
import corpo                                     # noqa: E402
import mujoco                                    # noqa: E402
from PIL import Image                            # noqa: E402
import fast_simplification as fs                 # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
SITE = RAIZ / 'site'
FRACAO = float(os.environ.get('FLY_EXPORT_FRACAO', '0.14'))   # fracao dos triangulos que fica nas malhas grandes
MIN_FACES = 1200                                               # malhas ate aqui nao sao decimadas
TEX_MAX = 512


def nome(m, tipo, i):
    return (mujoco.mj_id2name(m, tipo, i) or '').split('/')[-1]


def fovy_cam(m, cam):
    """Abertura vertical da camera que o render local usa (camera_id pode ser nome ou indice)."""
    cid = cam.camera_id
    if isinstance(cid, str):
        cid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, cid)
    try:
        return float(m.cam_fovy[int(cid)])
    except Exception:
        return 45.0


def decimar(pos, uv, faces):
    """Reduz a malha mantendo a uv: cada vertice novo herda a uv de um vertice original que caiu nele."""
    fn = len(faces)
    if fn <= MIN_FACES:
        return pos, uv, faces
    alvo = max(MIN_FACES, int(fn * FRACAO))
    red = 1.0 - alvo / fn
    _, _, colapsos = fs.simplify(pos.astype(np.float64), faces.astype(np.int64), target_reduction=red, return_collapses=True)
    pos2, faces2, mapa = fs.replay_simplification(pos.astype(np.float64), faces.astype(np.int64), colapsos)
    uv2 = None
    if uv is not None:
        uv2 = np.zeros((len(pos2), 2), dtype=np.float32)
        mapa = np.asarray(mapa)
        ok = mapa >= 0
        uv2[mapa[ok]] = uv[ok]
    return pos2.astype(np.float32), uv2, faces2.astype(np.int64)


def main():
    fly, cam, sim = corpo.montar(60, 1.0)
    m = sim.physics.model.ptr
    prefixo = fly.name + '/'
    ids = [b for b in range(m.nbody) if (mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, b) or '').startswith(prefixo)]
    idx_local = {b: k for k, b in enumerate(ids)}
    corpos = [{'nome': nome(m, mujoco.mjtObj.mjOBJ_BODY, b), 'pai': idx_local.get(int(m.body_parentid[b]), -1),
               'pos': m.body_pos[b].round(6).tolist(), 'quat': m.body_quat[b].round(6).tolist()} for b in ids]
    juntas = []
    for j in range(m.njnt):
        b = int(m.jnt_bodyid[j])
        if b in idx_local:
            juntas.append({'nome': nome(m, mujoco.mjtObj.mjOBJ_JOINT, j), 'corpo': idx_local[b], 'tipo': int(m.jnt_type[j]),
                           'eixo': m.jnt_axis[j].round(6).tolist(), 'pos': m.jnt_pos[j].round(6).tolist(),
                           'qadr': int(m.jnt_qposadr[j])})

    # texturas: sem repeticao (varios nomes apontam para a mesma imagem), reduzidas
    (SITE / 'fly-tex').mkdir(exist_ok=True)
    for velho in (SITE / 'fly-tex').glob('*.png'):
        velho.unlink()
    texturas, por_hash = {}, {}

    def textura(tid):
        tnome = nome(m, mujoco.mjtObj.mjOBJ_TEXTURE, tid) or f'tex{tid}'
        if tnome in texturas:
            return texturas[tnome]
        w, h, c = int(m.tex_width[tid]), int(m.tex_height[tid]), int(m.tex_nchannel[tid])
        adr = int(m.tex_adr[tid])
        px = np.asarray(m.tex_data[adr:adr + w * h * c]).reshape(h, w, c)
        chave = hashlib.md5(px.tobytes()).hexdigest()
        if chave in por_hash:
            texturas[tnome] = por_hash[chave]
            return por_hash[chave]
        img = Image.fromarray(px, 'RGB' if c == 3 else 'RGBA')
        if max(w, h) > TEX_MAX:
            img = img.resize((max(1, w * TEX_MAX // max(w, h)), max(1, h * TEX_MAX // max(w, h))), Image.LANCZOS)
        img.save(SITE / 'fly-tex' / f'{tnome}.png', optimize=True)
        texturas[tnome] = por_hash[chave] = tnome
        return tnome

    materiais = {}

    def material(mat, rgba_geom):
        if mat < 0:
            chave = 'rgba:' + ','.join(f'{v:.3f}' for v in rgba_geom)
            materiais.setdefault(chave, {'rgba': [float(v) for v in rgba_geom], 'tex': None, 'specular': 0.3, 'shininess': 0.3})
            return chave
        chave = nome(m, mujoco.mjtObj.mjOBJ_MATERIAL, mat)
        if chave not in materiais:
            texid = m.mat_texid[mat]
            tid = int(texid[mujoco.mjtTextureRole.mjTEXROLE_RGB]) if np.ndim(texid) else int(texid)
            materiais[chave] = {'rgba': [float(v) for v in m.mat_rgba[mat]], 'tex': textura(tid) if tid >= 0 else None,
                                'specular': float(m.mat_specular[mat]), 'shininess': float(m.mat_shininess[mat]),
                                'emission': float(m.mat_emission[mat])}
        return chave

    blobs, malhas = [], {}
    offset = 0
    tri_antes = 0

    def alinhar():
        nonlocal offset
        if offset % 4:
            pad = b'\0' * (4 - offset % 4)
            blobs.append(pad)
            offset += len(pad)

    def malha(mid):
        nonlocal offset, tri_antes
        if mid in malhas:
            return malhas[mid]['id']
        va, vn = int(m.mesh_vertadr[mid]), int(m.mesh_vertnum[mid])
        fa, fn = int(m.mesh_faceadr[mid]), int(m.mesh_facenum[mid])
        vert = np.asarray(m.mesh_vert[va:va + vn], dtype=np.float32)
        faces = np.asarray(m.mesh_face[fa:fa + fn], dtype=np.int64)
        tri_antes += fn
        ta, tn = int(m.mesh_texcoordadr[mid]), int(m.mesh_texcoordnum[mid])
        if tn > 0:
            # a uv e por canto de face: separa os vertices por (vertice, uv) para virar uv por vertice
            ftex = np.asarray(m.mesh_facetexcoord[fa:fa + fn], dtype=np.int64)
            chave = faces.ravel() * (tn + 1) + ftex.ravel()
            uniq, inv = np.unique(chave, return_inverse=True)
            pos = vert[uniq // (tn + 1)]
            uv = np.asarray(m.mesh_texcoord[ta:ta + tn], dtype=np.float32)[uniq % (tn + 1)]
            faces = inv.reshape(-1, 3)
        else:
            pos, uv = vert, None
        pos, uv, faces = decimar(pos, uv, faces)
        # tira faces degeneradas e vertices que ficaram sem uso
        faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
        usados, faces = np.unique(faces.ravel(), return_inverse=True)
        faces = faces.reshape(-1, 3)
        pos = pos[usados]
        uv = uv[usados] if uv is not None else None
        nv, nf = len(pos), len(faces)
        idx16 = nv < 65536
        alinhar()
        off_pos = offset
        b = pos.astype('<f4').tobytes(); blobs.append(b); offset += len(b)
        off_uv = offset
        if uv is not None:
            b = uv.astype('<f4').tobytes(); blobs.append(b); offset += len(b)
        alinhar()
        off_idx = offset
        b = faces.astype('<u2' if idx16 else '<u4').tobytes(); blobs.append(b); offset += len(b)
        malhas[mid] = {'id': len(malhas), 'nome': nome(m, mujoco.mjtObj.mjOBJ_MESH, mid), 'nv': int(nv), 'nf': int(nf),
                       'off_pos': off_pos, 'off_uv': off_uv, 'off_idx': off_idx, 'idx16': bool(idx16), 'uv': uv is not None}
        return malhas[mid]['id']

    geoms = []
    for g in range(m.ngeom):
        b = int(m.geom_bodyid[g])
        if b not in idx_local or m.geom_type[g] != mujoco.mjtGeom.mjGEOM_MESH:
            continue
        if m.geom_group[g] > 2 or m.geom_rgba[g][3] <= 0.0:      # grupos 3+ nao aparecem no render do MuJoCo
            continue
        if int(m.geom_contype[g]) != 0 or int(m.geom_conaffinity[g]) != 0:
            continue                                              # geoms de colisao: a mosca visual e outra
        geoms.append({'corpo': idx_local[b], 'malha': malha(int(m.geom_dataid[g])),
                      'pos': m.geom_pos[g].round(6).tolist(), 'quat': m.geom_quat[g].round(6).tolist(),
                      'mat': material(int(m.geom_matid[g]), m.geom_rgba[g]), 'nome': nome(m, mujoco.mjtObj.mjOBJ_GEOM, g)})
    saida = {'nq': int(m.nq), 'corpos': corpos, 'juntas': juntas, 'geoms': geoms, 'materiais': materiais,
             'texturas': sorted(set(texturas.values())), 'malhas': sorted(malhas.values(), key=lambda x: x['id']),
             'cam': {'dist': corpo.CAM_DIST_MM, 'elev': float(corpo.CAM_ELEV_RAD), 'fovy': fovy_cam(m, cam)},
             'raiz_nome': fly.name}
    (SITE / 'fly-model.json').write_text(json.dumps(saida, separators=(',', ':')))
    (SITE / 'fly-model.bin').write_bytes(b''.join(blobs))
    tri = sum(x['nf'] for x in malhas.values())
    tex_kb = sum(p.stat().st_size for p in (SITE / 'fly-tex').glob('*.png')) / 1e3
    print(f'corpos {len(corpos)}, juntas {len(juntas)}, nq {m.nq}, geoms {len(geoms)}, malhas {len(malhas)}, '
          f'triangulos {tri_antes:,} -> {tri:,}, bin {offset / 1e6:.2f} MB, texturas {len(set(texturas.values()))} ({tex_kb:.0f} KB)')
    print('maiores malhas:', sorted(((x['nome'], x['nf']) for x in malhas.values()), key=lambda t: -t[1])[:8])


if __name__ == '__main__':
    main()
