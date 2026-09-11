# Sentidos dela lidos DIRETO DA CHAIN: cada compra e venda na curva de um token (o dela, no lancamento) vira
# estimulo em segundos, sem esperar o indexador (a GeckoTerminal atrasa minutos). Le os logs da curva por
# eth_getLogs a cada poucos segundos e classifica pela transacao: buy(quoteIn,minOut,to) leva o ETH em value;
# sell(tokensIn,minOut,to) traz a quantidade no primeiro argumento. Conferido em 07/09: a interface da Pons chama
# a curva direto (to = curva), seletores 0x59a87bc1 (buy) e 0xd04c6983 (sell); bloco de 0,1 s.
import time
from collections import deque

from web3 import Web3


def _seletor(assinatura):
    h = Web3.keccak(text=assinatura)[:4].hex()
    return '0x' + h[2:] if h.startswith('0x') else '0x' + h


SEL_BUY = _seletor('buy(uint256,uint256,address)')
SEL_SELL = _seletor('sell(uint256,uint256,address)')


class SentidosChain:
    def __init__(self, chain, token, eth_usd=0.0):
        self.chain = chain
        self.eth_usd0 = float(eth_usd)                  # para dar valor em dolar aos trades semeados
        lanc = chain.lancamento(token)
        if not lanc['exists']:
            raise RuntimeError('token nao e um lancamento da Pons V2')
        self.token, self.curva = lanc['token'], lanc['curva']
        erc = chain.erc20(self.token)
        try:
            self.nome = str(erc.functions.symbol().call())
        except Exception:
            self.nome = self.token[:8]
        try:
            self.decimais = int(erc.functions.decimals().call())
        except Exception:
            self.decimais = 18
        self.bloco = int(chain.w3.eth.block_number) - 40   # comeca uns 4 s atras (reinicio nao perde trade)
        self.preco_eth = 0.0
        self.historico = deque(maxlen=600)
        self.vistos = set()
        self.erro = ''
        self.lidos = 0
        self.erros_429 = 0
        self.pausa_ate = 0.0                            # depois de um 429 do RPC, respira antes de pedir de novo
        self._preco()
        self._semear()

    def _semear(self, blocos=36000):
        """Carrega os trades da ultima ~1 h como historico (sem estimular): o replay nao fica vazio depois de
        um reinicio (07/09 22:20, 'parece que esta tudo parado' = historico zerado pelo reinicio + token quieto)."""
        w3 = self.chain.w3
        try:
            logs = w3.eth.get_logs({'address': self.curva, 'fromBlock': max(0, self.bloco - blocos), 'toBlock': self.bloco})
        except Exception as e:
            self.erro = 'semear: ' + str(e)[:60]
            return
        hashes = list(dict.fromkeys(Web3.to_hex(l['transactionHash']) for l in logs))[-120:]
        for h, tx in zip(hashes, self._transacoes(hashes)):
            self.vistos.add(h)
            t = self._classificar(h, tx, self.eth_usd0)
            if t:
                self.historico.append(t)
        self.semeados = len(self.historico)

    def _classificar(self, h, tx, eth_usd):
        if tx is None:
            return None
        dados = Web3.to_hex(tx['input']).lower()
        sel = dados[:10]
        quando = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if sel == SEL_BUY or int(tx['value']) > 0:
            kind, eth = 'buy', int(tx['value']) / 1e18
        elif sel == SEL_SELL and len(dados) >= 74:
            kind, eth = 'sell', int(dados[10:74], 16) / 10 ** self.decimais * self.preco_eth
        else:
            return None
        return {'tx': h, 'kind': kind, 'usd': float(eth * eth_usd), 'eth': float(eth), 'de': str(tx['from']),
                'quando': quando, 'preco_usd': self.preco_eth * eth_usd, 'fonte': 'chain'}

    def _transacoes(self, hashes):
        """Busca as transacoes num unico pedido em lote (JSON-RPC batch): 1 chamada em vez de N.
        Se o lote nao for suportado, cai no pedido um a um."""
        w3 = self.chain.w3
        try:
            with w3.batch_requests() as lote:
                for h in hashes:
                    lote.add(w3.eth.get_transaction(h))
                return list(lote.execute())
        except Exception:
            saida = []
            for h in hashes:
                try:
                    saida.append(w3.eth.get_transaction(h))
                except Exception:
                    saida.append(None)
            return saida

    def _preco(self):
        try:
            R, T = self.chain.curva(self.curva).functions.getReserves().call()
            self.preco_eth = (R / T) if T else 0.0
        except Exception as e:
            self.erro = str(e)[:80]

    def ler(self, eth_usd):
        """Trades novos desde a ultima leitura, no formato dos trades da GeckoTerminal (+ fonte='chain')."""
        w3 = self.chain.w3
        if time.time() < self.pausa_ate:
            return []
        try:
            atual = int(w3.eth.block_number)
            if atual <= self.bloco:
                return []
            logs = w3.eth.get_logs({'address': self.curva, 'fromBlock': self.bloco + 1, 'toBlock': atual})
            self.bloco = atual
            self.erro = ''
        except Exception as e:
            self.erro = str(e)[:80]
            if '429' in self.erro:
                self.erros_429 += 1
                self.pausa_ate = time.time() + 12       # o cursor fica onde esta: nada se perde, so atrasa
            return []
        hashes = [h for h in dict.fromkeys(Web3.to_hex(l['transactionHash']) for l in logs) if h not in self.vistos]
        if hashes:
            self._preco()
        novos = []
        for h, tx in zip(hashes, self._transacoes(hashes)):
            self.vistos.add(h)
            t = self._classificar(h, tx, eth_usd)
            if t is None:
                continue                                    # outra chamada na curva (aprovacao, admin): nao e trade
            novos.append(t)
            self.historico.append(t)
            self.lidos += 1
        if len(self.vistos) > 5000:
            self.vistos = set(t['tx'] for t in self.historico)
        return novos
