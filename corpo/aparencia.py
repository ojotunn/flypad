# Aparencia: cores de mosca de verdade e texturas de piso geradas por codigo (sem download).
#
# Cores: o NeuroMechFly pinta a mosca com texturas de imagem laranja. Multiplicar pelo rgba do material so
# escurece, nunca tira o laranja. Entao as texturas sao RECOLORIDAS na memoria depois de compilar e antes do
# primeiro render: a luminancia de cada pixel (sombreado, faixas do abdomen) vira uma rampa entre duas cores
# de Drosophila melanogaster: castanho-claro no torax e cabeca, faixas pretas no abdomen, ponta escura,
# pernas castanhas, olhos vermelho-tijolo, asas transparentes com brilho.
from pathlib import Path
import numpy as np
from PIL import Image

PASTA = Path(__file__).resolve().parent / 'texturas'

# textura -> (cor escura, cor clara) em 0..255; a luminancia original escolhe o ponto da rampa
RAMPAS = {
    'head_texture':      ((70, 50, 33), (188, 152, 110)),
    'thorax_texture':    ((70, 50, 33), (188, 152, 110)),
    'antenna_texture':   ((42, 28, 17), (118, 88, 58)),
    'proboscis_texture': ((68, 46, 32), (162, 122, 92)),
    'coxa_texture':      ((60, 42, 27), (172, 134, 96)),
    'femur_texture':     ((60, 42, 27), (172, 134, 96)),
    'tibia_texture':     ((54, 37, 23), (162, 124, 90)),
    'tarsus_texture':    ((44, 29, 17), (140, 106, 74)),
    'a12345_texture':    ((30, 19, 12), (198, 158, 106)),
    'a6_texture':        ((20, 13, 8), (108, 78, 50)),
}
CONTRASTE_ABDOMEN = 1.4          # realca as faixas claras/escuras do abdomen
# materiais sem textura, e brilho (especular, shininess)
TINTAS = {
    'eye_material':     (0.74, 0.14, 0.08, 1.0),
    'arista_material':  (0.20, 0.13, 0.08, 1.0),
    'haltere_material': (0.70, 0.55, 0.35, 0.8),
    'wing_material':    (0.86, 0.90, 0.96, 0.32),
}
BRILHO = {'eye_material': (0.7, 0.5), 'wing_material': (0.9, 0.7), 'thorax_material': (0.35, 0.3),
          'head_material': (0.35, 0.3), 'a12345_material': (0.3, 0.25), 'a6_material': (0.3, 0.25)}
# escurecimento progressivo dos segmentos do abdomen (materiais proprios criados antes de compilar)
FAIXAS = {'A3': 0.80, 'A4': 0.62, 'A5': 0.45}


def pintar_mjcf(fly):
    """Antes de compilar: um material por segmento do abdomen (A3, A4, A5), com a MESMA textura de faixas
    do a12345 e rgba progressivamente mais escuro. As faixas ficam e o abdomen escurece ate a ponta."""
    tex = fly.model.find('texture', 'a12345_texture')
    if tex is None:
        return
    for corpo, f in FAIXAS.items():
        b = fly.model.find('body', corpo)
        if b is None:
            continue
        mat = fly.model.asset.add('material', name=f'faixa_{corpo}', texture=tex, rgba=(f, f, f, 1.0),
                                  specular=0.3, shininess=0.25)
        for g in b.find_all('geom'):
            if g.parent is b:
                g.material = mat


def pintar(m, mujoco):
    """Depois de compilar e ANTES do primeiro render: recolore as texturas na memoria do modelo e ajusta
    os materiais. m = mjModel (sim.physics.model.ptr)."""
    for i in range(m.ntex):
        nome = (mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_TEXTURE, i) or '').split('/')[-1]
        if nome not in RAMPAS:
            continue
        w, h, c = int(m.tex_width[i]), int(m.tex_height[i]), int(m.tex_nchannel[i])
        adr = int(m.tex_adr[i])
        dados = m.tex_data[adr:adr + w * h * c].reshape(h, w, c).astype(np.float32)
        lum = dados[..., :3].mean(-1)
        lum = (lum - lum.min()) / (np.ptp(lum) + 1e-6)
        if nome.startswith('a'):
            # faixas do abdomen: suaviza o grao da textura original e realca claro/escuro
            from PIL import ImageFilter
            lum = np.array(Image.fromarray((lum * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.6))) / 255.0
            lum = np.clip((lum - 0.5) * CONTRASTE_ABDOMEN + 0.5, 0.0, 1.0)
        esc, cla = (np.array(v, dtype=np.float32) for v in RAMPAS[nome])
        dados[..., :3] = esc + (cla - esc) * lum[..., None]
        m.tex_data[adr:adr + w * h * c] = dados.reshape(-1).astype(np.uint8)
    for i in range(m.nmat):
        nome = (mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_MATERIAL, i) or '').split('/')[-1]
        if nome in TINTAS:
            m.mat_rgba[i] = TINTAS[nome]
        elif nome in ('head_material', 'thorax_material', 'antenna_material', 'proboscis_material',
                      'coxa_material', 'femur_material', 'tibia_material', 'tarsus_material',
                      'a12345_material', 'a6_material'):
            m.mat_rgba[i] = (1.0, 1.0, 1.0, m.mat_rgba[i][3])   # a cor agora esta na textura
        if nome in BRILHO:
            m.mat_specular[i], m.mat_shininess[i] = BRILHO[nome]


# ----- pisos -----
def _ruido(n, rng, oitavas=4, esticar=1):
    """Ruido fractal simples; esticar > 1 alonga o ruido no eixo x (veios)."""
    total = np.zeros((n, n))
    for o in range(oitavas):
        k = 2 ** o * 4
        base = rng.random((k, max(1, k // esticar)))
        img = np.array(Image.fromarray((base * 255).astype(np.uint8)).resize((n, n), Image.BICUBIC)) / 255.0
        total += img / (2 ** o)
    total -= total.min()
    return total / total.max()


def gerar_pisos(n=1024, semente=7):
    PASTA.mkdir(exist_ok=True)
    rng = np.random.default_rng(semente)
    y, x = np.mgrid[0:n, 0:n] / n
    r = _ruido(n, rng)
    fino = rng.random((n, n))

    # ardosia escura: quase preto com grao fino e manchas suaves (o clima do site)
    ard = 0.07 + 0.05 * r + 0.015 * fino
    ardosia = np.stack([ard * 0.95, ard * 1.0, ard * 1.15], -1)

    # bancada de laboratorio: branco fosco com grao sutil
    lab = 0.86 + 0.06 * r + 0.01 * fino
    bancada = np.stack([lab, lab, lab * 1.02], -1)

    # madeira: veios finos esticados no eixo x, cor dessaturada, emendas de tabua
    veio_r = _ruido(n, rng, oitavas=5, esticar=16)
    veio = 0.5 + 0.5 * np.sin(2 * np.pi * (y * 5 + 0.8 * veio_r))
    mad = 0.46 + 0.10 * veio + 0.05 * veio_r + 0.02 * fino
    emenda = (np.abs((y * 4) % 1.0 - 0.5) > 0.492).astype(float)
    mad = mad * (1 - 0.35 * emenda)
    madeira = np.stack([mad * 1.0, mad * 0.78, mad * 0.58], -1)

    # casca de banana: amarelo com pintas castanhas e leve mancha
    pintas = (_ruido(n, rng, oitavas=3) > 0.70).astype(float)
    pintas = np.array(Image.fromarray((pintas * 255).astype(np.uint8)).filter(__import__('PIL.ImageFilter', fromlist=['GaussianBlur']).GaussianBlur(2))) / 255.0
    ban = 0.92 - 0.06 * r + 0.02 * fino
    banana = np.stack([ban * 0.98, ban * 0.84, ban * 0.30], -1)
    marrom = np.array([0.36, 0.22, 0.08])
    banana = banana * (1 - pintas[..., None]) + marrom * pintas[..., None]

    # musgo: verde mosqueado, sem nervura (a folha tileada virava grade)
    mus = 0.22 + 0.18 * r + 0.03 * fino
    musgo = np.stack([mus * 0.62, mus * 1.0, mus * 0.40], -1)

    nomes = ('ardosia', 'bancada', 'madeira', 'banana', 'musgo')
    for nome, img in zip(nomes, (ardosia, bancada, madeira, banana, musgo)):
        Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)).save(PASTA / f'{nome}.png')
    return [str(PASTA / f'{n_}.png') for n_ in nomes]


PISOS = {  # nome -> (arquivo, repeticoes relativas (1 = uma a cada 5 mm), reflectancia)
    'preto':   ('preto', 1.0, 0.18),      # liso, preto, so o reflexo da mosca (escolha do Michel, 06/09)
    'ardosia': ('ardosia.png', 1.0, 0.10),
    'bancada': ('bancada.png', 1.0, 0.06),
    'madeira': ('madeira.png', 0.25, 0.05),
    'banana':  ('banana.png', 0.5, 0.04),
    'musgo':   ('musgo.png', 0.7, 0.02),
    'xadrez':  (None, 1.0, 0.10),
}


if __name__ == '__main__':
    print(gerar_pisos())
