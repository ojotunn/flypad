# Corpo 3D da mosca (NeuroMechFly v2 via flygym 1.2 + MuJoCo), movido pelos neuronios descendentes.
# Le as taxas dos grupos motores pelo WebSocket do servidor do cerebro e devolve os quadros renderizados
# por HTTP, que o servidor repassa a quem esta na pagina. A traducao neuronio -> movimento segue o
# fly-brain (MIT): prioridade fuga > limpeza > alimentacao > marcha, com histerese de modo.
#
# Dois modos (FLY_CORPO_MODO):
#   cinematico (padrao) - TEMPO REAL. O mesmo modelo anatomico e as mesmas trajetorias de perna gravadas de
#                         mosca real (PreprogrammedSteps), ritmadas pelo gerador de marcha (CPG) que o cerebro
#                         comanda; o corpo se desloca por cinematica. Sem contato nem gravidade simulados.
#   fisica              - contato e gravidade de verdade, mas no maximo ~4x mais lento que o relogio nesta
#                         maquina (a fisica pura em C ja e 4x; passo maior derruba a mosca). Fica para clipes.
import asyncio
import io
import json
import os
import queue
import struct
import threading
import time
import urllib.request

os.environ.setdefault('MUJOCO_GL', 'glfw')
import numpy as np                      # noqa: E402
import mujoco                            # noqa: E402
from PIL import Image                    # noqa: E402
from flygym import Fly, SingleFlySimulation                  # noqa: E402
from flygym.camera import YawOnlyCamera                      # noqa: E402  segue a direcao da mosca (vista lateral sempre)
from flygym.arena import BaseArena                           # noqa: E402
from flygym.examples.locomotion import PreprogrammedSteps    # noqa: E402
from flygym.examples.locomotion.turning_controller import HybridTurningController  # noqa: E402
import sys as _sys                       # noqa: E402
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aparencia                         # noqa: E402  cores de mosca real e pisos
import corpo3d_envio                     # noqa: E402  quadro de pose (angulos) para a mosca desenhada no navegador

SERVIDOR = os.environ.get('FLY_SERVIDOR', 'http://localhost:8435')
PISO = os.environ.get('FLY_CORPO_PISO', 'preto')            # preto | ardosia | bancada | madeira | banana | musgo | xadrez
CORES = os.environ.get('FLY_CORPO_CORES', 'real')           # real | original
WS_URL = SERVIDOR.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws'
MODO_CORPO = os.environ.get('FLY_CORPO_MODO', 'cinematico')
LARGURA = int(os.environ.get('FLY_CORPO_W', '1280'))   # quadro largo (2,37:1): a faixa do corpo na pagina e larga e
ALTURA = int(os.environ.get('FLY_CORPO_H', '540'))     # baixa; com piso e ceu pretos, a borda do encaixe some
QUALIDADE = int(os.environ.get('FLY_CORPO_JPEG', '75'))
FPS = int(os.environ.get('FLY_CORPO_FPS', '60'))               # quadros por segundo de relogio (cinematico)
PLAY_SPEED = float(os.environ.get('FLY_CORPO_PLAY', '0.7'))   # fisica: um quadro a cada play/30 s de mosca
MAX_RATE = 200.0                                               # Hz -> [0,1], igual ao DNRateDecoder do fly-brain
ESCALA_MARCHA = 100.0                                          # P9 medido a 39-57 Hz: com 200 a marcha ficava a 0,2
LIMIAR = {'escape': 0.3, 'groom': 0.02, 'feed': 0.05, 'startle': 0.12, 'halt': 0.05, 'back': 0.15}
ESCALA_RECOMPENSA = 60.0        # PAM a ~50 Hz -> excitacao ~0,8
MIN_MODO_S = 0.3                                               # histerese: segundos de mosca antes de trocar de modo
# Fisica (medido 06/09): 1e-4 com decisao a cada passo = 18x; 2e-4 estavel; SUB sub-passos por decisao com o
# controlador remendado para contar SUB*dt chega a ~4,4x. 4e-4 e 5e-4 derrubam a mosca.
TIMESTEP = float(os.environ.get('FLY_CORPO_DT', '2e-4'))
SUB = int(os.environ.get('FLY_CORPO_SUB', '8'))
DT_CTRL = TIMESTEP * SUB
# Cinematica. O controlador do flygym anda sempre a 12 Hz e so encurta a passada quando o drive cai; na tela
# isso parece vibracao/time-lapse ("a animacao parece time lapse", 06/09). A mosca real reduz o ritmo com a
# velocidade, entao aqui a frequencia vai de FREQ_MIN a FREQ_MAX com o drive, e a velocidade do corpo e
# passada x ritmo, para o pe nao deslizar. PASSADA_MM calibrada na fisica: drive 1,0 a 12 Hz andou ~14 mm/s.
FREQ_MIN_HZ = 4.0
FREQ_MAX_HZ = 14.0
PASSADA_MM = 1.17
GIRO_RAD_S = 1.5                                               # 2,5 virava a mosca em 115 graus em 10 s com assimetria pequena
CPG_DT = 1e-3                                                  # sub-passo do gerador de marcha no modo cinematico
ESCALA_TEMPO = float(os.environ.get('FLY_CORPO_TEMPO', '1.0'))  # 1 = tempo real; 0,5 = metade da velocidade
# Saida (07/09): 'pose' manda so os angulos (qpos) e a camera, ~400 B a POSE_FPS; o navegador desenha a mosca
# (site/fly-cliente.js + modelo de corpo/exportar_modelo.py). 'jpeg' e o render local de antes; 'ambos' os dois.
SAIDA = os.environ.get('FLY_CORPO_SAIDA', 'pose')
POSE_FPS = int(os.environ.get('FLY_CORPO_POSE_FPS', '30'))
# Voo (cinematico): a fuga (fibra gigante) vira decolagem, como na mosca real; pousa quando a fibra silencia.
# Batida real e ~200 Hz e viraria borrao; na tela fica estilizada (ASA_HZ), dito no rodape.
ALT_VOO_MM = 5.0
VEL_VOO_MM_S = 35.0
GIRO_VOO_RAD_S = 1.2
MIN_VOO_S = 2.0
ASA_ABRE_RAD = 1.15
ASA_BATE_RAD = 0.75
ASA_HZ = 9.0
LIMITE_ARENA_MM = 600.0                                        # volta ao centro se andar ate aqui


class Palco(BaseArena):
    """Chao plano e ceu escuro, para a mosca aparecer sobre o fundo do site. O piso vem de aparencia.PISOS
    (texturas geradas por codigo em corpo/texturas; 'xadrez' e o tabuleiro embutido)."""

    def __init__(self, tamanho=1500, piso=None):
        super().__init__()
        piso = piso or PISO
        a = self.root_element.asset
        a.add('texture', type='skybox', builtin='gradient', rgb1=(0.02, 0.024, 0.032),
              rgb2=(0.05, 0.06, 0.085), width=256, height=256)
        hl = self.root_element.visual.headlight
        hl.ambient = (0.36, 0.36, 0.4)
        hl.diffuse = (0.85, 0.82, 0.76)
        hl.specular = (0.35, 0.35, 0.35)
        self.root_element.worldbody.add('light', name='sol', pos=(0, 0, 200), dir=(0.3, 0.4, -1.0),
                                        diffuse=(1.0, 0.96, 0.9), specular=(0.4, 0.4, 0.4),
                                        castshadow=True, directional=True)
        arq, rep, refl = aparencia.PISOS.get(piso, aparencia.PISOS['preto'])
        if arq == 'preto':
            mat = a.add('material', name='chao', reflectance=refl, rgba=(0.012, 0.013, 0.018, 1.0),
                        specular=0.3, shininess=0.6)
        else:
            if arq is None or not (aparencia.PASTA / arq).exists():
                tex = a.add('texture', type='2d', builtin='checker', width=256, height=256,
                            rgb1=(0.075, 0.08, 0.095), rgb2=(0.095, 0.1, 0.12))
            else:
                tex = a.add('texture', type='2d', file=str(aparencia.PASTA / arq))
            mat = a.add('material', name='chao', texture=tex, texrepeat=(tamanho / 5 * rep, tamanho / 5 * rep),
                        reflectance=refl, rgba=(1.0, 1.0, 1.0, 1.0))
        self.root_element.worldbody.add('geom', type='plane', name='ground', material=mat,
                                        size=[tamanho, tamanho, 1], friction=(1, 0.005, 0.0001),
                                        conaffinity=0)
        self.friction = (1, 0.005, 0.0001)

    def get_spawn_position(self, rel_pos, rel_angle):
        return rel_pos, rel_angle

    def _get_max_floor_height(self):
        return 0.0


class Limpeza:
    """Oscilacao das pernas da frente para limpar as antenas (copiado do fly-brain)."""

    def __init__(self, passos=None, freq_hz=4.0):
        self.passos = passos or PreprogrammedSteps()
        self.freq = freq_hz
        self.neutro = np.zeros(42)
        for i, perna in enumerate(self.passos.legs):
            self.neutro[i * 7:(i + 1) * 7] = self.passos.get_joint_angles(perna, np.pi, 0.0)

    def juntas(self, t, olhos=False):
        """Antenas: pernas da frente alternando. Olhos: as duas juntas, mais altas, esfregando."""
        j = self.neutro.copy()
        fase = 2 * np.pi * self.freq * t
        if olhos:
            femur = -0.45 + 0.25 * np.sin(fase * 1.5)
            tibia = 0.55 + 0.3 * np.sin(fase * 1.5 + np.pi / 2)
        else:
            femur = 0.3 * np.sin(fase)
            tibia = 0.4 * np.sin(fase + np.pi / 2)
        for base in (0, 21):          # LF e RF
            j[base + 3] += femur
            j[base + 5] += tibia
        return j

    def acao(self, t):
        return {'joints': self.juntas(t), 'adhesion': np.array([0, 1, 1, 0, 1, 1])}


# ----- leitura das taxas do cerebro (thread com seu proprio loop asyncio) -----
def leitor_ws(estado):
    import websockets

    async def laco():
        while True:
            try:
                async with websockets.connect(WS_URL + '?papel=corpo', max_size=8_000_000) as ws:
                    estado['ligado'] = True
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            n = struct.unpack_from('<I', msg, 0)[0]
                            cab = json.loads(bytes(msg[4:4 + n]).decode('utf-8'))
                            if cab.get('tipo') == 'quadro':
                                estado['dn'] = cab.get('dn', {})
                                estado['estimulos'] = cab.get('stimuli', [])
                                estado['t_cerebro'] = cab.get('t', 0)
                                cfg = cab.get('body_cfg') or {}
                                if 'time_scale' in cfg:
                                    estado['escala'] = float(cfg['time_scale'])
                                if 'rot' in cab:            # relogio de rotacao compartilhado com o cerebro da pagina
                                    estado['rot'] = float(cab['rot'])
                                    estado['rot_speed'] = float(cab.get('rot_speed', 0.12))
                                    estado['rot_t'] = time.time()
            except Exception as e:
                estado['ligado'] = False
                estado['erro'] = str(e)[:80]
                await asyncio.sleep(2)

    asyncio.run(laco())


# ----- envio dos quadros (thread): comprime em JPEG e manda por um WebSocket persistente -----
# (um POST por quadro chegava aos trancos no servidor, com buracos de ate 1,7 s a 60 quadros/s)
def enviador(fila, estado):
    import websockets
    url = WS_URL.rsplit('/ws', 1)[0] + '/corpo/ws'

    def comprimir(quadro):
        buf = io.BytesIO()
        Image.fromarray(quadro).save(buf, format='JPEG', quality=QUALIDADE)
        return buf.getvalue()

    async def laco():
        while True:
            try:
                async with websockets.connect(url, max_size=8_000_000) as ws:
                    estado.pop('erro_envio', None)
                    while True:
                        cab, quadro = await asyncio.get_running_loop().run_in_executor(None, fila.get)
                        dados = quadro if isinstance(quadro, (bytes, bytearray)) else comprimir(quadro)
                        j = json.dumps(cab, separators=(',', ':')).encode('utf-8')
                        await ws.send(struct.pack('<I', len(j)) + j + dados)
                        estado['enviados'] = estado.get('enviados', 0) + 1
            except Exception as e:
                estado['erro_envio'] = str(e)[:80]
                await asyncio.sleep(1)

    asyncio.run(laco())


def enfileirar(fila, cab, quadro):
    if fila.full():
        try:
            fila.get_nowait()
        except queue.Empty:
            pass
    fila.put_nowait((cab, quadro))


def remendar(sim):
    """Fisica: o controlador conta SUB*dt por decisao enquanto a fisica continua em dt."""
    if SUB <= 1:
        return
    sim.timestep = DT_CTRL
    sim.cpg_network.timestep = DT_CTRL
    sim.max_increment *= SUB
    sim.retraction_persistence_duration *= SUB
    sim.retraction_persistence_initiation_threshold *= SUB


def reservar_cpu():
    """Prioridade acima do normal e nucleos proprios: o corpo e o cerebro brigavam pela CPU."""
    if os.name != 'nt':
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.GetCurrentProcess()
    k32.SetPriorityClass(h, 0x00008000)                 # ABOVE_NORMAL_PRIORITY_CLASS
    mascara = int(os.environ.get('FLY_CORPO_AFINIDADE', '0xFF00'), 0)
    if mascara:
        k32.SetProcessAffinityMask(h, mascara)


def quat_yaw(yaw):
    return np.array([np.cos(yaw / 2), 0.0, 0.0, np.sin(yaw / 2)])


def quat_mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                     w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


class Decisor:
    """Taxas dos grupos motores -> modo e drive, como o BrainBodyBridge do fly-brain."""

    def __init__(self, estado):
        self.estado = estado
        self.modo, self.t_modo = 'walking', 0.0
        self.turn = 0.0
        self.recompensa = 0.0

    def norm(self, k, escala=MAX_RATE):
        return min(float(self.estado['dn'].get(k, 0.0)) / escala, 1.0)

    def decidir(self, dt):
        fwd = self.norm('forward', ESCALA_MARCHA) + 0.5 * self.norm('feed')   # MN9 = aproximacao
        back = self.norm('backward', ESCALA_MARCHA)
        turn = self.norm('turn_L') - self.norm('turn_R')
        self.turn = turn
        esc, grm, feed = self.norm('escape'), self.norm('groom'), self.norm('feed')
        halt = self.norm('halt')
        self.recompensa = self.norm('reward', ESCALA_RECOMPENSA)
        # prioridade: fuga > sobressalto > parada > limpeza > alimentacao > re > marcha
        if esc > LIMIAR['escape']:
            novo = 'escape'
        elif esc > LIMIAR['startle']:
            novo = 'startle'
        elif halt > LIMIAR['halt'] and halt > fwd:      # DNp09 dispara junto com o P9; so para se vencer a marcha
            novo = 'freeze'
        elif grm > LIMIAR['groom']:
            novo = 'eye_grooming' if 'eye_touch' in self.estado.get('estimulos', []) else 'grooming'
        elif feed > LIMIAR['feed']:
            novo = 'feeding'
        elif back > LIMIAR['back'] and back > fwd:
            novo = 'backing'
        else:
            novo = 'walking'
        self.t_modo += dt
        if novo != self.modo and self.t_modo >= MIN_MODO_S:
            self.modo, self.t_modo = novo, 0.0
        if self.modo == 'escape':
            drive = np.array([1.3, 1.3])
        elif self.modo in ('grooming', 'eye_grooming', 'feeding', 'startle', 'freeze'):
            drive = np.array([0.0, 0.0])
        else:
            drive = np.array([np.clip(fwd * (1.0 + turn) - back, -0.5, 1.5),
                              np.clip(fwd * (1.0 - turn) - back, -0.5, 1.5)])
        return self.modo, drive


def montar(fps_camera, play_speed):
    # o HybridTurningController exige sensores de contato em tibia e tarsos para detectar tropeco
    sensores = [f'{perna}{seg}' for perna in ('LF', 'LM', 'LH', 'RF', 'RM', 'RH')
                for seg in ('Tibia', 'Tarsus1', 'Tarsus2', 'Tarsus3', 'Tarsus4', 'Tarsus5')]
    fly = Fly(enable_adhesion=True, draw_adhesion=False, init_pose='stretch', control='position',
              contact_sensor_placements=sensores)
    # A proboscide nao tem junta no NeuroMechFly; o fly-brain adiciona uma dobradica no Rostrum.
    rostrum = fly.model.find('body', 'Rostrum')
    if rostrum is not None:
        rostrum.add('joint', name='joint_Proboscis', type='hinge', axis=[0, 1, 0],
                    range=[-0.1, 1.2], stiffness=50.0, damping=5.0)
    # As asas tambem nao tem junta: uma para abrir (eixo vertical) e uma para bater (eixo do corpo, que no
    # referencial da asa, girado 90 graus, e o eixo y). O sentido e calibrado na partida (calibrar_asas).
    for lado in ('L', 'R'):
        asa = fly.model.find('body', f'{lado}Wing')
        if asa is not None:
            asa.add('joint', name=f'joint_{lado}Wing_abre', type='hinge', axis=[0, 0, 1],
                    range=[-2.0, 2.0], stiffness=20.0, damping=1.0)
            asa.add('joint', name=f'joint_{lado}Wing_bate', type='hinge', axis=[0, 1, 0],
                    range=[-1.6, 1.6], stiffness=20.0, damping=1.0)
    if CORES == 'real':
        aparencia.pintar_mjcf(fly)
    cam = YawOnlyCamera(smoothing_window_length=20, attachment_point=fly.model.worldbody,
                        camera_name='camera_right', targeted_fly_names=[fly.name], play_speed=play_speed,
                        window_size=(LARGURA, ALTURA), fps=fps_camera, play_speed_text=False)
    sim = HybridTurningController(fly=fly, timestep=TIMESTEP, seed=0, arena=Palco(), cameras=[cam])
    sim.reset()
    if CORES == 'real':
        aparencia.pintar(sim.physics.model.ptr, mujoco)   # antes do primeiro render
    return fly, cam, sim


def renderizar(sim, cam, fly):
    """Atualiza a camera que segue a mosca e renderiza um quadro, sem acumular lista de quadros."""
    fly.get_observation(sim)                       # atualiza fly.last_obs, que traz "pos" e "rot" para a camera
    cam._update_camera(sim.physics, sim._floor_height, fly.last_obs)
    return sim.physics.render(width=LARGURA, height=ALTURA, camera_id=cam.camera_id).copy()


CAM_DIST_MM = 5.6      # 7,5 deixava a mosca pequena no quadro largo
CAM_ELEV_RAD = np.radians(12.0)


def renderizar_orbita(sim, cam, alvo, az, el=CAM_ELEV_RAD, dist=CAM_DIST_MM):
    """Camera orbitando a mosca: 'az' e o angulo no mundo em que a camera fica em relacao ao alvo.
    Sincronia com o cerebro da pagina: az = yaw da mosca - rot (0 = de frente para a cara dela)."""
    pos = np.array(alvo) + dist * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    frente = np.array(alvo) - pos
    frente /= np.linalg.norm(frente)
    direita = np.cross(frente, np.array([0.0, 0.0, 1.0]))
    direita /= np.linalg.norm(direita)
    cima = np.cross(direita, frente)
    # camera do MuJoCo olha pelo -z proprio, com +y para cima: colunas [direita, cima, -frente]
    xmat = np.column_stack([direita, cima, -frente])
    b = sim.physics.bind(cam._cam)
    b.xpos = pos
    b.xmat = xmat.flatten()
    return sim.physics.render(width=LARGURA, height=ALTURA, camera_id=cam.camera_id).copy()


def achar_juntas(sim, fly):
    m = sim.physics.model.ptr
    jid = lambda nome: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, nome)  # noqa: E731
    qadr = -1
    for nome in ('joint_Proboscis', f'{fly.name}/joint_Proboscis'):
        j = jid(nome)
        if j >= 0:
            qadr = int(m.jnt_qposadr[j])
            break
    pernas = np.array([int(m.jnt_qposadr[jid(f'{fly.name}/{jn}')]) for jn in fly.actuated_joints])
    livres = [j for j in range(m.njnt) if m.jnt_type[j] == mujoco.mjtJoint.mjJNT_FREE]
    raiz = int(m.jnt_qposadr[livres[0]]) if livres else -1
    antenas = [int(m.jnt_qposadr[jid(f'{fly.name}/{n}')]) for n in ('joint_LPedicel', 'joint_RPedicel')
               if jid(f'{fly.name}/{n}') >= 0]
    return qadr, pernas, raiz, antenas


def achar_extras(sim, fly):
    """Enderecos qpos das juntas de asa e cabeca. Cabeca pelos EIXOS (os nomes do flygym estao trocados):
    pitch = joint_Head (eixo y), yaw = joint_Head_roll (eixo z), roll = joint_Head_yaw (eixo x)."""
    m = sim.physics.model.ptr
    def adr(nome):
        for n in (nome, f'{fly.name}/{nome}'):
            j = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, n)
            if j >= 0:
                return int(m.jnt_qposadr[j])
        return -1
    return {'L_abre': adr('joint_LWing_abre'), 'L_bate': adr('joint_LWing_bate'),
            'R_abre': adr('joint_RWing_abre'), 'R_bate': adr('joint_RWing_bate'),
            'pitch': adr('joint_Head'), 'yaw': adr('joint_Head_roll'), 'roll': adr('joint_Head_yaw')}


def calibrar_asas(sim, fly, extras):
    """Descobre o sinal de cada junta de asa medindo para onde a ponta vai: abrir = para fora, bater = para cima."""
    m, d = sim.physics.model.ptr, sim.physics.data.ptr
    sinais = {}
    for lado, fora in (('L', +1.0), ('R', -1.0)):
        bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, f'{fly.name}/{lado}Wing')
        gids = [g for g in range(m.ngeom) if m.geom_bodyid[g] == bid and m.geom_dataid[g] >= 0]
        if bid < 0 or not gids or extras[f'{lado}_abre'] < 0:
            sinais[f'{lado}_abre'] = sinais[f'{lado}_bate'] = 1.0
            continue
        g = gids[0]
        mid = m.geom_dataid[g]
        v = m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid] + m.mesh_vertnum[mid]]
        ponta = v[np.argmax(np.linalg.norm(v, axis=1))]          # vertice mais longe da base

        def ponta_mundo():
            mujoco.mj_forward(m, d)
            return d.geom_xpos[g] + d.geom_xmat[g].reshape(3, 3) @ ponta

        base = ponta_mundo()
        d.qpos[extras[f'{lado}_abre']] = 0.6
        aberta = ponta_mundo()
        d.qpos[extras[f'{lado}_abre']] = 0.0
        sinais[f'{lado}_abre'] = 1.0 if (aberta[1] - base[1]) * fora > 0 else -1.0
        d.qpos[extras[f'{lado}_bate']] = 0.6
        batida = ponta_mundo()
        d.qpos[extras[f'{lado}_bate']] = 0.0
        sinais[f'{lado}_bate'] = 1.0 if batida[2] > base[2] else -1.0
        mujoco.mj_forward(m, d)
    return sinais


# ============================================================================
# modo cinematico: tempo real
# ============================================================================
def laco_cinematico(fly, cam, sim, decisor, estado, fila):
    m, d = sim.physics.model.ptr, sim.physics.data.ptr
    qadr, pernas, raiz, antenas = achar_juntas(sim, fly)
    assert raiz >= 0, 'nao achei a junta livre da mosca'
    # assenta a mosca com fisica por 0,3 s para pegar a pose de repouso (altura e inclinacao)
    for _ in range(int(0.3 / TIMESTEP)):
        sim.step(np.array([0.0, 0.0]))
    raiz0 = d.qpos[raiz:raiz + 7].copy()
    z0, q_rest = float(raiz0[2]), raiz0[3:7].copy()
    passos = sim.preprogrammed_steps
    cpg = sim.cpg_network
    cpg.timestep = CPG_DT
    limpeza = Limpeza(passos)
    extras = achar_extras(sim, fly)
    sinais = calibrar_asas(sim, fly, extras)
    voo = {'estado': 'chao', 't': 0.0, 'alt': 0.0, 'abre': 0.0}
    cabeca = {'pitch': 0.0, 'yaw': 0.0, 'sacada': 0.0, 'prox_sacada': 3.0}
    excit = 0.0                      # excitacao: sobe com a dopamina, decai em ~30 s
    rng = np.random.default_rng()
    ang = limpeza.neutro.copy()
    print(f'[corpo] cinematico: {FPS} quadros/s, escala de tempo {ESCALA_TEMPO:g}, repouso z={z0:.2f} mm; '
          f'asas {sinais}; cabeca {[k for k, v in extras.items() if v >= 0]}', flush=True)

    dt_tela = 1.0 / FPS                 # ritmo do relogio
    dt = dt_tela * ESCALA_TEMPO         # quanto a mosca avanca por quadro
    x = y = yaw = 0.0
    t = 0.0
    prob = 0.0
    quadros = 0
    t0 = time.time()
    prox = t0
    t_log = t0
    atrasos = 0
    enviados_ant = 0
    rot_local = 0.0
    while True:
        escala = float(estado.get('escala', ESCALA_TEMPO))
        dt = dt_tela * escala
        modo, drive = decisor.decidir(dt)
        # voo: a fuga decola; pousa quando a fibra gigante silencia (depois de MIN_VOO_S no ar)
        if modo == 'escape' and voo['estado'] == 'chao':
            voo['estado'], voo['t'] = 'decolando', 0.0
        voo['t'] += dt
        if voo['estado'] == 'decolando':
            voo['alt'] = ALT_VOO_MM * min(1.0, voo['t'] / 0.5) ** 0.7
            if voo['t'] >= 0.5:
                voo['estado'], voo['t'] = 'voando', 0.0
        elif voo['estado'] == 'voando':
            voo['alt'] = ALT_VOO_MM + 0.3 * np.sin(2 * np.pi * 1.5 * t)
            if modo != 'escape' and voo['t'] >= MIN_VOO_S:
                voo['estado'], voo['t'] = 'pousando', 0.0
        elif voo['estado'] == 'pousando':
            voo['alt'] = ALT_VOO_MM * max(0.0, 1.0 - voo['t'] / 0.6) ** 1.5
            if voo['t'] >= 0.6:
                voo['estado'], voo['alt'] = 'chao', 0.0
        no_ar = voo['estado'] != 'chao'
        voo['abre'] += ((1.0 if no_ar else 0.0) - voo['abre']) * min(1.0, 8.0 * dt)
        if no_ar:
            drive = np.array([0.0, 0.0])          # pernas paradas no ar; o corpo voa pela cinematica abaixo
        # excitacao (dopamina): sobe rapido, decai devagar; acelera marcha e micro-movimentos
        if decisor.recompensa > excit:
            excit += (decisor.recompensa - excit) * min(1.0, 2.0 * dt)
        else:
            excit -= excit * dt / 30.0
        drive = drive * (1.0 + 0.4 * excit)
        # gerador de marcha: amplitude = |drive| de cada lado, sentido pelo sinal (igual ao controlador);
        # ritmo cresce com a velocidade (mosca real), em vez dos 12 Hz fixos do flygym
        amp = min(float(np.abs(drive).mean()), 1.0)
        freq = FREQ_MIN_HZ + (FREQ_MAX_HZ - FREQ_MIN_HZ) * amp
        amps = np.repeat(np.abs(drive)[:, None], 3, axis=1).ravel()
        freqs = np.ones(6) * freq
        freqs[:3] *= 1 if drive[0] > 0 else -1
        freqs[3:] *= 1 if drive[1] > 0 else -1
        cpg.intrinsic_amps = amps
        cpg.intrinsic_freqs = freqs
        n_sub = max(1, int(round(dt / CPG_DT)))
        cpg.timestep = dt / n_sub
        if modo != 'freeze':                    # parada: o gerador de marcha congela onde esta
            for _ in range(n_sub):
                cpg.step()
        # juntas das pernas: trajetorias gravadas de mosca real, na fase que o gerador manda
        if no_ar:
            ang = limpeza.neutro.copy()            # pernas recolhidas no ar
            for base in range(0, 42, 7):
                ang[base + 3] -= 0.5 * voo['abre']
                ang[base + 5] += 0.6 * voo['abre']
            for a in antenas:
                d.qpos[a] = 0.0
        elif modo == 'grooming':
            ang = limpeza.juntas(t)
            for a in antenas:
                d.qpos[a] = 0.25 * np.sin(2 * np.pi * 4.0 * t)
        elif modo == 'eye_grooming':
            ang = limpeza.juntas(t, olhos=True)
            for a in antenas:
                d.qpos[a] = 0.0
        elif modo == 'freeze':
            pass                                    # mantem o ultimo angulo
        else:
            ang = np.concatenate([passos.get_joint_angles(perna, cpg.curr_phases[i], cpg.curr_magnitudes[i])
                                  for i, perna in enumerate(passos.legs)])
            # fisiologia de repouso (nao e decisao): antenas tremem de leve, mais com excitacao
            for k, a in enumerate(antenas):
                d.qpos[a] = 0.05 * (1.0 + 2.5 * excit) * np.sin(2 * np.pi * (1.3 + 0.9 * excit) * t + 1.7 * k)
        d.qpos[pernas] = ang
        # asas: abrem quando decola, batem enquanto estiver no ar (batida estilizada)
        bate = ASA_BATE_RAD * np.sin(2 * np.pi * ASA_HZ * t) * voo['abre']
        for lado in ('L', 'R'):
            if extras[f'{lado}_abre'] >= 0:
                d.qpos[extras[f'{lado}_abre']] = sinais[f'{lado}_abre'] * ASA_ABRE_RAD * voo['abre']
                d.qpos[extras[f'{lado}_bate']] = sinais[f'{lado}_bate'] * bate
        # cabeca: abaixa para comer e para esfregar os olhos, acompanha a curva, balanca com a marcha,
        # sacode na limpeza; em repouso da pequenas sacadas (fisiologia, nao decisao), mais com excitacao
        if modo == 'feeding':
            pitch_alvo = 0.35
        elif modo == 'eye_grooming':
            pitch_alvo = 0.25
        elif no_ar:
            pitch_alvo = -0.12
        else:
            pitch_alvo = 0.05 * amp * np.sin(2 * float(cpg.curr_phases[0]))
        cabeca['pitch'] += (pitch_alvo - cabeca['pitch']) * min(1.0, 5.0 * dt)
        cabeca['prox_sacada'] -= dt * (1.0 + 2.0 * excit)
        if cabeca['prox_sacada'] <= 0 and modo in ('walking', 'backing') and not no_ar:
            cabeca['sacada'] = rng.uniform(-0.12, 0.12) * (1.0 + excit)
            cabeca['prox_sacada'] = rng.uniform(2.5, 6.0)
        cabeca['sacada'] -= cabeca['sacada'] * min(1.0, 0.8 * dt)
        cabeca['yaw'] += (0.45 * decisor.turn + cabeca['sacada'] - cabeca['yaw']) * min(1.0, 4.0 * dt)
        if extras['pitch'] >= 0:
            d.qpos[extras['pitch']] = cabeca['pitch']
        if extras['yaw'] >= 0:
            d.qpos[extras['yaw']] = cabeca['yaw'] + (0.15 * np.sin(2 * np.pi * 5.0 * t) if modo == 'grooming' else 0.0)
        if extras['roll'] >= 0:
            d.qpos[extras['roll']] = 0.0
        # proboscide: estende rapido, recolhe devagar
        alvo = 1.0 if modo == 'feeding' else 0.0
        prob += (alvo - prob) * min(1.0, (6.0 if alvo > prob else 2.0) * dt)
        if qadr >= 0:
            d.qpos[qadr] = prob
        # corpo: no chao, velocidade = passada x ritmo (pe nao desliza) e giro pelo drive; no ar, voa para
        # frente virando pelos neuronios de curva; balanco leve na marcha
        if no_ar:
            v = VEL_VOO_MM_S * (1.0 if voo['estado'] != 'pousando' else 0.4) * voo['abre']
            w = GIRO_VOO_RAD_S * decisor.turn
        else:
            v = PASSADA_MM * float(np.abs(drive).mean()) * freq * (1.0 if drive.mean() >= 0 else -1.0)
            w = GIRO_RAD_S * float(drive[1] - drive[0])
        yaw += w * dt
        x += v * np.cos(yaw) * dt
        y += v * np.sin(yaw) * dt
        if abs(x) > LIMITE_ARENA_MM or abs(y) > LIMITE_ARENA_MM:
            x = y = 0.0
        z = z0 + voo['alt']
        z += 0.02 * abs(float(drive.mean())) * np.sin(2 * float(cpg.curr_phases[0]))
        if modo == 'startle' and decisor.t_modo < 0.16:      # sobressalto: pulinho sem abrir as asas
            z += 0.7 * np.sin(np.pi * decisor.t_modo / 0.16)
        d.qpos[raiz:raiz + 3] = (x, y, z)
        d.qpos[raiz + 3:raiz + 7] = quat_mul(quat_yaw(yaw), q_rest)
        d.qvel[:] = 0.0
        mujoco.mj_forward(m, d)

        # angulo de rotacao compartilhado: avanca localmente e corrige para o relogio do servidor
        vel_rot = float(estado.get('rot_speed', 0.12))
        rot_local += vel_rot * dt_tela
        if 'rot' in estado:
            alvo_rot = estado['rot'] + vel_rot * (time.time() - estado['rot_t'])
            dif = (alvo_rot - rot_local + np.pi) % (2 * np.pi) - np.pi
            rot_local += dif * 0.1
        quadros += 1
        agora = time.time()
        rotulo = {'decolando': 'flying', 'voando': 'flying', 'pousando': 'landing'}.get(voo['estado'], modo)
        if rotulo == 'walking' and excit > 0.35:
            rotulo = 'excited'
        cab = {'mode': rotulo, 'drive': [round(float(drive[0]), 2), round(float(drive[1]), 2)],
               't_body': round(t, 2), 'brain_link': estado['ligado'], 'realtime': abs(escala - 1.0) < 1e-6,
               'time_scale': round(escala, 2), 'step_hz': round(freq, 1), 'rot': round(rot_local % (2 * np.pi), 3),
               'excitement': round(excit, 2),
               'body_mode': 'kinematic', 'fps': round(quadros / max(1e-6, agora - t0), 1), 'frame': quadros}
        alvo, az = (x, y, z + 0.6), yaw - rot_local
        if SAIDA in ('pose', 'ambos') and quadros % max(1, FPS // POSE_FPS) == 0:
            cab_p, dados = corpo3d_envio.quadro_pose(d, cab, alvo, az, CAM_ELEV_RAD, CAM_DIST_MM)
            cab_p['fps'] = POSE_FPS
            enfileirar(fila, cab_p, dados)
        if SAIDA in ('jpeg', 'ambos'):
            enfileirar(fila, cab, renderizar_orbita(sim, cam, alvo, az))

        t += dt
        prox += dt_tela
        folga = prox - time.time()
        if folga > 0:
            time.sleep(folga)
        else:
            atrasos += 1
            if folga < -1.0:          # ficou muito para tras: reancora o relogio
                prox = time.time()
        if agora - t_log >= 5.0:
            env = estado.get('enviados', 0)
            print(f'[corpo] {quadros / (agora - t0):.1f} quadros/s gerados (alvo {FPS}), '
                  f'{(env - enviados_ant) / (agora - t_log):.1f}/s enviados, atrasos {atrasos}, modo {modo}, '
                  f'ritmo {freq:.1f} Hz, drive {drive.round(2).tolist()}, cerebro {"ligado" if estado["ligado"] else "off"}'
                  f'{" | " + estado["erro_envio"] if estado.get("erro_envio") else ""}', flush=True)
            enviados_ant = env
            t_log = agora


# ============================================================================
# modo fisica: contato de verdade, lento
# ============================================================================
def laco_fisica(fly, cam, sim, decisor, estado, fila):
    remendar(sim)
    qadr, _, _, _ = achar_juntas(sim, fly)
    limpeza = Limpeza(sim.preprogrammed_steps)
    intervalo = PLAY_SPEED / 30.0
    prox_quadro = 0.0
    passo, erros, quadros = 0, 0, 0
    t0 = time.time()
    t_log = t0
    print(f'[corpo] fisica {TIMESTEP:g}s x{SUB}; proboscide {"ok" if qadr >= 0 else "ausente"}', flush=True)
    while True:
        modo, drive = decisor.decidir(DT_CTRL)
        try:
            if modo == 'grooming':
                SingleFlySimulation.step(sim, limpeza.acao(passo * DT_CTRL))
            else:
                sim.step(drive)
            for _ in range(SUB - 1):
                sim.physics.step()
            if qadr >= 0 and modo == 'feeding':
                sim.physics.data.ptr.qpos[qadr] = 1.0
            erros = 0
        except Exception as e:
            erros += 1
            if erros >= 50:
                print(f'[corpo] fisica instavel ({e}); reiniciando a mosca', flush=True)
                sim.reset()
                remendar(sim)
                erros = 0
            continue
        passo += 1
        if sim.curr_time >= prox_quadro:
            prox_quadro += intervalo
            quadros += 1
            agora = time.time()
            cab = {'mode': modo, 'drive': [round(float(drive[0]), 2), round(float(drive[1]), 2)],
                   't_body': round(passo * DT_CTRL, 3), 'brain_link': estado['ligado'], 'realtime': False,
                   'body_mode': 'physics', 'steps_per_s': round(passo * SUB / max(1e-6, agora - t0)),
                   'slowdown': round((agora - t0) / max(1e-6, passo * DT_CTRL), 1), 'frame': quadros}
            enfileirar(fila, cab, renderizar(sim, cam, fly))
        agora = time.time()
        if agora - t_log >= 5.0:
            print(f'[corpo] {passo * SUB / (agora - t0):.0f} passos/s ({(agora - t0) / max(1e-6, passo * DT_CTRL):.1f}x lento), '
                  f'{quadros / (agora - t0):.1f} quadros/s, modo {modo}, drive {drive.round(2).tolist()}, '
                  f'cerebro {"ligado" if estado["ligado"] else "off"}'
                  f'{" | " + estado["erro_envio"] if estado.get("erro_envio") else ""}', flush=True)
            t_log = agora


def main():
    reservar_cpu()
    estado = {'dn': {}, 'estimulos': [], 'ligado': False}
    threading.Thread(target=leitor_ws, args=(estado,), name='ws', daemon=True).start()
    fila = queue.Queue(maxsize=2)
    threading.Thread(target=enviador, args=(fila, estado), name='envio', daemon=True).start()
    cinematico = MODO_CORPO != 'fisica'
    fly, cam, sim = montar(FPS if cinematico else 30, 1.0 if cinematico else PLAY_SPEED)
    print(f'[corpo] mosca pronta; modo {"cinematico" if cinematico else "fisica"}; saida {SAIDA} '
          f'({POSE_FPS} poses/s; jpeg {LARGURA}x{ALTURA}); servidor {SERVIDOR}', flush=True)
    decisor = Decisor(estado)
    if cinematico:
        laco_cinematico(fly, cam, sim, decisor, estado, fila)
    else:
        laco_fisica(fly, cam, sim, decisor, estado, fila)


if __name__ == '__main__':
    main()
