# Gerente do FLY PAD: roda no PC com a placa. Guarda a colonia (moscas/colonia.json), sobe e derruba os tres
# processos de cada mosca (cerebro, corpo, mercado), puxa a fila de hatch do relay, empurra a colonia para o relay
# e respeita o teto de moscas acordadas (as demais dormem com as sinapses salvas e acordam quando uma compra chega).
#
# Uso: py\Scripts\python.exe gerente\gerente.py
# Variaveis: FLY_RELAY_URL (https://...), FLY_RELAY_TOKEN (ou relay.token na raiz), FLY_MAX_ACORDADAS (padrao 4),
#            FLY_PASSOS_S (teto por cerebro, padrao 120), FLY_PORTA_BASE (padrao 8500), FLY_GERENTE_PORTA (8440)
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

RAIZ = Path(__file__).resolve().parent.parent
PY = str(RAIZ / 'py' / 'Scripts' / 'python.exe') if (RAIZ / 'py' / 'Scripts' / 'python.exe').exists() else sys.executable
MOSCAS = RAIZ / 'moscas'
MOSCAS.mkdir(exist_ok=True)
ARQ = MOSCAS / 'colonia.json'
RELAY = os.environ.get('FLY_RELAY_URL', '').rstrip('/')
TOKEN = os.environ.get('FLY_RELAY_TOKEN') or ((RAIZ / 'relay.token').read_text().strip() if (RAIZ / 'relay.token').exists() else '')
MAX_ACORDADAS = int(os.environ.get('FLY_MAX_ACORDADAS', '4'))
PASSOS_S = os.environ.get('FLY_PASSOS_S', '120')
PORTA_BASE = int(os.environ.get('FLY_PORTA_BASE', '8500'))
PORTA_GERENTE = int(os.environ.get('FLY_GERENTE_PORTA', '8440'))
BOOT_S = 150          # tempo que um cerebro leva para carregar o conectoma antes de responder


def agora():
    return time.time()


class Colonia:
    def __init__(self):
        self.moscas = []            # fichas: id, name, ticker, ca, sex, x, image, born, estado, porta, dono
        self.procs = {}             # id -> {'cerebro': Popen, 'corpo': Popen, 'mercado': Popen, 'desde': t}
        self.carregar()

    def carregar(self):
        if ARQ.exists():
            try:
                self.moscas = json.loads(ARQ.read_text(encoding='utf-8'))
            except Exception:
                self.moscas = []

    def salvar(self):
        ARQ.write_text(json.dumps(self.moscas, indent=1, ensure_ascii=False), encoding='utf-8')

    def por_id(self, fid):
        return next((m for m in self.moscas if m['id'] == fid), None)

    def porta_livre(self):
        usadas = {m.get('porta') for m in self.moscas}
        p = PORTA_BASE
        while p in usadas:
            p += 10
        return p

    # ---------- processos ----------
    def pasta(self, fid):
        d = MOSCAS / fid
        (d / 'mercado').mkdir(parents=True, exist_ok=True)
        (d / 'logs').mkdir(parents=True, exist_ok=True)
        return d

    def _env(self, m):
        e = dict(os.environ)
        e.update({'FLY_QUEM': m['id'], 'FLY_PORT': str(m['porta']), 'FLY_PASSOS_S': str(PASSOS_S), 'FLY_CORPO_SAIDA': 'pose',
                  'FLY_SERVIDOR': f'http://localhost:{m["porta"]}', 'FLY_SERVIDOR_ELE': '', 'FLY_PAR': '',
                  'FLY_MERCADO_DIR': str(self.pasta(m['id']) / 'mercado'), 'FLY_MERCADO_MODO': 'papel',
                  'FLY_MERCADO_SALDO_ETH': '0.05', 'FLY_MERCADO_MAX_ORDEM_USD': '10', 'FLY_MERCADO_ORDEM': '0.10',
                  'FLY_MERCADO_SENTIDOS': m.get('ca') or '', 'FLY_MERCADO_IGNORAR': m.get('ca') or '',
                  'FLY_RELAY_URL': (RELAY.replace('https://', 'wss://').replace('http://', 'ws://') + '/fonte') if RELAY else '',
                  'FLY_RELAY_TOKEN': TOKEN})
        return e

    def _abrir(self, m, nome, args, env):
        log = open(self.pasta(m['id']) / 'logs' / f'{nome}.log', 'ab')
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        return subprocess.Popen([PY] + args, cwd=str(RAIZ), env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)

    def acordar(self, m):
        if m['id'] in self.procs:
            return
        env = self._env(m)
        d = self.pasta(m['id']) / 'mercado'
        (d / 'ordens.txt').write_text('off\n')                    # estagio 1: papel, sem ordens
        (d / 'sentidos.txt').write_text((m.get('ca') or '') + '\n')
        (d / 'ignorar.txt').write_text((m.get('ca') or '') + '\n')
        if not (d / 'ajustes.txt').exists():
            (d / 'ajustes.txt').write_text('max_ordem_usd=10\nlote=0.10\n')
        p = {'cerebro': self._abrir(m, 'cerebro', ['brain/servidor.py'], env), 'desde': agora(), 'corpo': None, 'mercado': None}
        self.procs[m['id']] = p
        m['estado'] = 'booting'
        print(f'[gerente] {m["id"]} acordando (porta {m["porta"]})', flush=True)

    def _apos_boot(self, m):
        """Corpo e mercado so depois que o cerebro responde (eles falam com ele)."""
        p = self.procs.get(m['id'])
        if not p or p['corpo'] is not None:
            return
        env = self._env(m)
        p['corpo'] = self._abrir(m, 'corpo', ['corpo/corpo.py'], env)
        p['mercado'] = self._abrir(m, 'mercado', ['mercado/mercado.py'], env)
        m['estado'] = 'awake'
        m['acordou'] = agora()
        print(f'[gerente] {m["id"]} acordada: cerebro + corpo + mercado', flush=True)

    async def dormir(self, m, motivo=''):
        p = self.procs.pop(m['id'], None)
        if p:
            for nome in ('mercado', 'corpo'):
                if p.get(nome):
                    p[nome].kill()
            try:                                                     # o cerebro salva as sinapses antes de sair
                async with aiohttp.ClientSession() as s:
                    await s.post(f'http://localhost:{m["porta"]}/api/dormir', timeout=aiohttp.ClientTimeout(total=20))
                await asyncio.sleep(2)
            except Exception:
                pass
            if p['cerebro'].poll() is None:
                p['cerebro'].kill()
        m['estado'] = 'sleeping'
        m['dormiu'] = agora()
        print(f'[gerente] {m["id"]} dormindo {motivo}', flush=True)

    async def vigiar(self):
        """Cerebro que ja responde libera corpo+mercado; processo que morreu e reaberto; teto de acordadas."""
        for m in self.moscas:
            p = self.procs.get(m['id'])
            if not p:
                continue
            if p['corpo'] is None:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(f'http://localhost:{m["porta"]}/api/estado', timeout=aiohttp.ClientTimeout(total=3)) as r:
                            j = await r.json()
                            if j.get('steps_per_s'):
                                self._apos_boot(m)
                except Exception:
                    if agora() - p['desde'] > 600:
                        print(f'[gerente] {m["id"]} nao subiu em 10 min; reiniciando', flush=True)
                        await self.dormir(m, '(boot falhou)')
                        self.acordar(m)
                continue
            for nome in ('cerebro', 'corpo', 'mercado'):
                if p.get(nome) is not None and p[nome].poll() is not None:
                    print(f'[gerente] {m["id"]}: {nome} caiu (codigo {p[nome].returncode}); reabrindo', flush=True)
                    await self.dormir(m, '(queda)')
                    self.acordar(m)
                    break

    def acordadas(self):
        return [m for m in self.moscas if m['id'] in self.procs]

    async def hatch(self, pedido):
        fid = pedido['id']
        if self.por_id(fid):
            return self.por_id(fid)
        m = {'id': fid, 'name': pedido['name'], 'ticker': pedido['ticker'], 'ca': pedido.get('ca', ''), 'sex': pedido.get('sex', 'f'),
             'x': pedido.get('x', ''), 'image': pedido.get('image', ''), 'born': agora(), 'estado': 'sleeping', 'porta': self.porta_livre()}
        self.moscas.append(m)
        self.salvar()
        await self.garantir_vaga()
        self.acordar(m)
        return m

    async def garantir_vaga(self):
        """Teto de acordadas: a acordada ha mais tempo dorme (estagio 1: FIFO; depois: a com menos trades)."""
        while len(self.acordadas()) >= MAX_ACORDADAS:
            vitima = min(self.acordadas(), key=lambda m: m.get('acordou', 0))
            await self.dormir(vitima, '(vaga para outra mosca)')

    def ficha_publica(self, m):
        return {k: m.get(k) for k in ('id', 'name', 'ticker', 'ca', 'sex', 'x', 'image', 'born', 'estado', 'acordou', 'dormiu')}


col = Colonia()


# ---------- API local (so no PC) ----------
async def api_moscas(request):
    return web.json_response({'moscas': [col.ficha_publica(m) for m in col.moscas], 'max_acordadas': MAX_ACORDADAS})


async def api_hatch_local(request):
    j = await request.json()
    ticker = ''.join(ch for ch in str(j.get('ticker', '')).upper() if ch.isalnum())[:12]
    if not j.get('name') or not ticker:
        raise web.HTTPBadRequest(text='name e ticker')
    pedido = {'id': f"{ticker.lower()[:8]}-{secrets.token_hex(2)}", 'name': str(j['name'])[:40], 'ticker': ticker,
              'ca': str(j.get('ca', '')).lower(), 'sex': j.get('sex', 'f'), 'x': j.get('x', ''), 'image': j.get('image', '')}
    m = await col.hatch(pedido)
    return web.json_response(col.ficha_publica(m))


async def api_dormir(request):
    m = col.por_id(request.match_info['id'])
    if not m:
        raise web.HTTPNotFound()
    await col.dormir(m, '(pedido)')
    col.salvar()
    return web.json_response(col.ficha_publica(m))


async def api_acordar(request):
    m = col.por_id(request.match_info['id'])
    if not m:
        raise web.HTTPNotFound()
    await col.garantir_vaga()
    col.acordar(m)
    col.salvar()
    return web.json_response(col.ficha_publica(m))


async def laco(app):
    """A cada 5 s: vigia processos, puxa a fila do relay, empurra a colonia."""
    await asyncio.sleep(1)
    for m in col.moscas:                        # ao iniciar: reacorda quem estava acordada (ate o teto)
        if m.get('estado') in ('awake', 'booting') and len(col.acordadas()) < MAX_ACORDADAS:
            col.acordar(m)
    while True:
        try:
            await col.vigiar()
            if RELAY and TOKEN:
                async with aiohttp.ClientSession() as s:
                    try:
                        async with s.get(f'{RELAY}/fila', params={'token': TOKEN}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                            fila = (await r.json()).get('fila', [])
                        for pedido in fila:
                            m = await col.hatch(pedido)
                            await s.post(f'{RELAY}/fila/feito', params={'token': TOKEN}, json={'id': pedido['id']})
                            print(f'[gerente] hatch da fila: {m["id"]} ({m["name"]})', flush=True)
                    except Exception as e:
                        print(f'[gerente] fila: {str(e)[:80]}', flush=True)
                    try:
                        await s.post(f'{RELAY}/colonia', params={'token': TOKEN},
                                     json={'moscas': [col.ficha_publica(m) for m in col.moscas], 'max_acordadas': MAX_ACORDADAS},
                                     timeout=aiohttp.ClientTimeout(total=10))
                    except Exception as e:
                        print(f'[gerente] colonia: {str(e)[:80]}', flush=True)
            col.salvar()
        except Exception as e:
            print(f'[gerente] laco: {e}', flush=True)
        await asyncio.sleep(5)


async def ao_iniciar(app):
    app['laco'] = asyncio.create_task(laco(app))


def main():
    app = web.Application()
    app.router.add_get('/api/moscas', api_moscas)
    app.router.add_post('/api/hatch', api_hatch_local)
    app.router.add_post('/api/dormir/{id}', api_dormir)
    app.router.add_post('/api/acordar/{id}', api_acordar)
    app.on_startup.append(ao_iniciar)
    print(f'[gerente] FLY PAD: {len(col.moscas)} moscas na colonia; teto {MAX_ACORDADAS} acordadas; relay {RELAY or "(sem relay)"}; porta {PORTA_GERENTE}', flush=True)
    web.run_app(app, host='127.0.0.1', port=PORTA_GERENTE, print=None)


if __name__ == '__main__':
    main()
