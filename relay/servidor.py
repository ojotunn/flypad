# Relay publico do FLY PAD (roda no Railway). Muitas moscas, uma por token: o gerente (PC com a placa) conecta
# cada cerebro em /fonte?token=&quem=<id> e manda os quadros; quem assiste abre /t/<id> e recebe /ws?t=<id>.
# A home (/) lista a colonia, que o gerente empurra em /colonia. O formulario de hatch entra na fila (/api/hatch);
# o gerente puxa a fila (/fila) e sobe a mosca. O relay nunca fala com o PC por conta propria (o PC esta atras de NAT).
import asyncio
import hashlib
import json
import os
import re
import secrets
import struct
import time
import uuid
from collections import deque
from pathlib import Path

from aiohttp import web

PORTA = int(os.environ.get('PORT', '8080'))
TOKEN = os.environ.get('FLY_RELAY_TOKEN', '')
INSTANCIA = uuid.uuid4().hex[:8]     # cada conteiner tem o seu; a fonte confere pelo /health se esta no conteiner que atende o publico
RAIZ = Path(__file__).resolve().parent.parent
SITE = RAIZ / 'site'
FILA_MAX = 6
MAX_MOSCAS = int(os.environ.get('FLY_MAX_MOSCAS', '200'))


def cabecalho(dados):
    try:
        n = struct.unpack_from('<I', dados, 0)[0]
        return json.loads(dados[4:4 + n].decode('utf-8'))
    except Exception:
        return {}


def nova_mosca():
    return {'ws': None, 'ola': None, 'quadro': None, 'corpo': None, 'resumo': None, 'config': None,
            'eventos': deque(maxlen=200), 'ordens': deque(maxlen=50), 'fonte_t': 0.0, 'espectadores': set()}


def mosca(app, quem):
    m = app['moscas'].get(quem)
    if m is None:
        if len(app['moscas']) >= MAX_MOSCAS:
            raise web.HTTPTooManyRequests(text='colonia cheia')
        m = app['moscas'][quem] = nova_mosca()
    return m


ID_OK = re.compile(r'^[a-z0-9][a-z0-9-]{1,39}$')


def exige_token(request):
    if not TOKEN or request.query.get('token') != TOKEN:
        raise web.HTTPForbidden(text='token')


class Espectador:
    def __init__(self, ws):
        self.ws = ws
        self.fila = asyncio.Queue(maxsize=FILA_MAX)
        self.perdidos = 0

    def enviar(self, item):
        if self.fila.full():
            try:
                self.fila.get_nowait()
                self.perdidos += 1
            except asyncio.QueueEmpty:
                pass
        self.fila.put_nowait(item)

    async def escritor(self):
        while True:
            tipo, dados = await self.fila.get()
            if tipo == 'b':
                await self.ws.send_bytes(dados)
            else:
                await self.ws.send_str(dados)


def espalhar(m, tipo, dados):
    for e in list(m['espectadores']):
        e.enviar((tipo, dados))


async def fonte(request):
    """Um cerebro do PC. Uma fonte por mosca; a mais nova derruba a anterior."""
    exige_token(request)
    quem = str(request.query.get('quem', '')).lower()
    if not ID_OK.match(quem):
        raise web.HTTPBadRequest(text='quem')
    app = request.app
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    m = mosca(app, quem)
    velho = m['ws']
    if velho is not None and not velho.closed:
        await velho.close()
    m['ws'] = ws
    m['fonte_t'] = time.time()
    print(f'[relay] fonte conectada ({quem})', flush=True)

    async def contar():
        while not ws.closed:
            try:
                await ws.send_str(json.dumps({'viewers': len(m['espectadores']), 'instancia': INSTANCIA}))
            except Exception:
                break
            await asyncio.sleep(2)

    contador = asyncio.create_task(contar())
    try:
        async for msg in ws:
            m['fonte_t'] = time.time()
            if msg.type == web.WSMsgType.BINARY:
                cab = cabecalho(msg.data)
                tipo = cab.get('tipo')
                if tipo == 'quadro':
                    m['quadro'] = (cab, msg.data)
                elif tipo == 'corpo':
                    m['corpo'] = msg.data
                espalhar(m, 'b', msg.data)
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    j = json.loads(msg.data)
                except Exception:
                    continue
                if j.get('tipo') == 'ola':
                    m['ola'] = msg.data
                    continue
                if j.get('tipo') == 'config':            # CA e X vindos do PC: guarda (injeta na pagina) e espalha ao vivo
                    m['config'] = {'ca': str(j.get('ca', '')), 'x': str(j.get('x', ''))}
                    espalhar(m, 't', msg.data)
                    continue
                if j.get('tipo') == 'mercado':
                    if j.get('classe') == 'resumo':
                        m['resumo'] = msg.data
                    elif j.get('classe') == 'limpar':
                        m['eventos'].clear()
                        m['ordens'].clear()
                    else:
                        m['eventos'].append(msg.data)
                        if j.get('classe') == 'ordem':          # ordens dela guardadas a parte: nao se perdem no feed
                            m['ordens'].append(msg.data)
                espalhar(m, 't', msg.data)
    finally:
        contador.cancel()
        if m['ws'] is ws:
            m['ws'] = None
        print(f'[relay] fonte desconectada ({quem})', flush=True)
    return ws


async def ws_handler(request):
    """Um espectador de UMA mosca (?t=<id>): recebe o estado atual e depois tudo o que a fonte dela manda."""
    quem = str(request.query.get('t', '')).lower()
    if not ID_OK.match(quem):
        raise web.HTTPBadRequest(text='t')
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    m = mosca(app, quem)
    e = Espectador(ws)
    if m['ola']:
        e.enviar(('t', m['ola']))
    if m['config']:
        e.enviar(('t', json.dumps(dict(m['config'], tipo='config'), separators=(',', ':'))))
    if m['corpo']:
        e.enviar(('b', m['corpo']))
    if m['quadro']:
        e.enviar(('b', m['quadro'][1]))
    if m['resumo']:
        e.enviar(('t', m['resumo']))
    for ev in list(m['ordens'])[-6:]:
        e.enviar(('t', ev))
    for ev in list(m['eventos'])[-12:]:
        e.enviar(('t', ev))
    m['espectadores'].add(e)
    escritor = asyncio.create_task(e.escritor())
    try:
        async for msg in ws:          # o publico nao manda nada que valha; so mantem a conexao viva
            if msg.type == web.WSMsgType.ERROR:
                break
    finally:
        escritor.cancel()
        m['espectadores'].discard(e)
    return ws


# ---------------- colonia (lista das moscas, vinda do gerente) e fila de hatch ----------------
async def colonia_post(request):
    exige_token(request)
    j = await request.json()
    lista = j.get('moscas') or []
    app = request.app
    app['colonia'] = lista
    app['colonia_t'] = time.time()
    for f in lista:
        if ID_OK.match(str(f.get('id', ''))):
            mosca(app, f['id'])
    return web.json_response({'ok': True, 'n': len(lista)})


def resumo_mosca(app, f):
    """Ficha publica de uma mosca: o que o gerente disse + o que esta chegando ao vivo pelo relay."""
    m = app['moscas'].get(f.get('id'))
    ao_vivo = bool(m and m['ws'] is not None and time.time() - m['fonte_t'] < 15)
    q = m['quadro'][0] if (m and m['quadro']) else {}
    r = None
    if m and m['resumo']:
        try:
            r = json.loads(m['resumo'])
        except Exception:
            r = None
    return dict(f, live=ao_vivo, spikes_per_s=q.get('spikes_per_s'), state=q.get('state'), lived=q.get('lived'),
                stimuli=q.get('stimuli'), viewers=len(m['espectadores']) if m else 0,
                mcap_usd=(r or {}).get('mcap_usd'), preco_usd=(r or {}).get('preco_usd'), vol_h24=(r or {}).get('vol_h24'))


async def api_colonia(request):
    app = request.app
    return web.json_response({'moscas': [resumo_mosca(app, f) for f in app['colonia']],
                              'atualizada_ha_s': round(time.time() - app['colonia_t']) if app['colonia_t'] else None,
                              'pendentes': len(app['fila'])})


async def api_hatch(request):
    """Formulario publico: entra na fila; o gerente puxa e sobe a mosca. Sem pagamento neste estagio (modo teste)."""
    try:
        j = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='json')
    nome = str(j.get('name', '')).strip()[:40]
    ticker = re.sub(r'[^A-Z0-9]', '', str(j.get('ticker', '')).upper())[:12]
    ca = str(j.get('ca', '')).strip().lower()
    sexo = 'm' if str(j.get('sex', 'f')).lower().startswith('m') else 'f'
    x = str(j.get('x', '')).strip()[:200]
    imagem = str(j.get('image', '')).strip()[:300]
    if not nome or not ticker:
        raise web.HTTPBadRequest(text='name and ticker are required')
    if ca and not re.match(r'^0x[0-9a-f]{40}$', ca):
        raise web.HTTPBadRequest(text='CA must be a 0x address')
    if x and not re.match(r'^https?://', x):
        raise web.HTTPBadRequest(text='X must be a link')
    app = request.app
    if len(app['fila']) >= 50:
        raise web.HTTPTooManyRequests(text='queue full, try again in a minute')
    fid = f"{ticker.lower()[:8]}-{secrets.token_hex(2)}"
    pedido = {'id': fid, 'name': nome, 'ticker': ticker, 'ca': ca, 'sex': sexo, 'x': x, 'image': imagem, 't': time.time()}
    app['fila'].append(pedido)
    mosca(app, fid)
    return web.json_response({'ok': True, 'id': fid, 'pending': True, 'page': f'/t/{fid}'})


async def fila_get(request):
    exige_token(request)
    return web.json_response({'fila': list(request.app['fila'])})


async def fila_feito(request):
    exige_token(request)
    j = await request.json()
    fid = str(j.get('id', ''))
    app = request.app
    app['fila'] = [p for p in app['fila'] if p['id'] != fid]
    return web.json_response({'ok': True, 'restam': len(app['fila'])})


# ---------------- paginas ----------------
async def api_estado(request):
    m = request.app['moscas'].get(str(request.query.get('t', '')).lower())
    return web.json_response(m['quadro'][0] if (m and m['quadro']) else {'tipo': 'sem fonte'})


async def api_mercado(request):
    m = request.app['moscas'].get(str(request.query.get('t', '')).lower())
    if not m:
        return web.json_response({'resumo': None, 'ordens': [], 'eventos': []})
    return web.json_response({'resumo': json.loads(m['resumo']) if m['resumo'] else None,
                              'ordens': [json.loads(x) for x in list(m['ordens'])[-20:]],
                              'eventos': [json.loads(x) for x in list(m['eventos'])[-40:]]})


async def versao(request):
    h = hashlib.md5((SITE / 'publico.html').read_bytes() + (SITE / 'fly-cliente.js').read_bytes() + (SITE / 'home.html').read_bytes()).hexdigest()[:12]
    return web.Response(text=h, content_type='text/plain', headers={'Cache-Control': 'no-store'})


async def saude(request):
    app = request.app
    vivas = [q for q, m in app['moscas'].items() if m['ws'] is not None]
    return web.json_response({'ok': True, 'instancia': INSTANCIA, 'moscas': len(app['colonia']), 'fontes': vivas,
                              'viewers': sum(len(m['espectadores']) for m in app['moscas'].values()),
                              'pendentes': len(app['fila'])})


def escapar_js(v):
    return str(v or '').replace('\\', '').replace("'", '').replace('<', '').replace('>', '')


async def pagina_mosca(request):
    quem = str(request.match_info['id']).lower()
    if not ID_OK.match(quem):
        raise web.HTTPNotFound()
    app = request.app
    ficha = next((f for f in app['colonia'] if f.get('id') == quem), None)
    pendente = next((p for p in app['fila'] if p['id'] == quem), None)
    if ficha is None and pendente is None and quem not in app['moscas']:
        raise web.HTTPNotFound(text='no fly with this id')
    f = ficha or pendente or {}
    m = app['moscas'].get(quem)
    cfg = (m['config'] if m else None) or {}
    html = (SITE / 'publico.html').read_text(encoding='utf-8')
    html = html.replace("const FLY_ID='';", f"const FLY_ID='{escapar_js(quem)}';", 1)
    html = html.replace("const FLY_NOME='';", f"const FLY_NOME='{escapar_js(f.get('name'))}';", 1)
    html = html.replace("const FLY_TICKER='';", f"const FLY_TICKER='{escapar_js(f.get('ticker'))}';", 1)
    ca = escapar_js(cfg.get('ca') or f.get('ca'))
    x = escapar_js(cfg.get('x') or f.get('x'))
    if ca:
        html = html.replace("const CA='';", f"const CA='{ca}';", 1)
    if x:
        html = html.replace("const X_URL='';", f"const X_URL='{x}';", 1)
    return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-store'})


async def home(request):
    html = (SITE / 'home.html').read_text(encoding='utf-8')
    return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-store'})


def main():
    app = web.Application()
    app['moscas'] = {}
    app['colonia'] = []
    app['colonia_t'] = 0.0
    app['fila'] = []
    app.router.add_get('/', home)
    app.router.add_get('/t/{id}', pagina_mosca)
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/fonte', fonte)
    app.router.add_get('/fila', fila_get)
    app.router.add_post('/fila/feito', fila_feito)
    app.router.add_post('/colonia', colonia_post)
    app.router.add_get('/api/colonia', api_colonia)
    app.router.add_post('/api/hatch', api_hatch)
    app.router.add_get('/api/estado', api_estado)
    app.router.add_get('/api/mercado', api_mercado)
    app.router.add_get('/health', saude)
    app.router.add_get('/api/versao', versao)
    app.router.add_static('/static', SITE, show_index=False)
    print(f'[relay] FLY PAD porta {PORTA}; token {"definido" if TOKEN else "AUSENTE (fonte nao consegue entrar)"}', flush=True)
    web.run_app(app, host='0.0.0.0', port=PORTA, print=None)


if __name__ == '__main__':
    main()
