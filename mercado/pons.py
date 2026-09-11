# Pons V2 na Robinhood Chain: leitura da curva, cotacao, compra e venda com a carteira da mosca.
# Escrito do zero para o projeto. So compra e vende na curva; nao ha funcao de saque.
# Interface dos contratos: docs.ponsfamily.com/docs/v2 (fabrica getLaunchedToken; curva buy/sell/getReserves...).
import time
from web3 import Web3

RPC = 'https://rpc.mainnet.chain.robinhood.com'
CHAIN_ID = 4663
FABRICA = '0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e'
BPS = 10_000

ABI_FABRICA = [{"type": "function", "name": "getLaunchedToken", "stateMutability": "view",
                "inputs": [{"name": "token", "type": "address"}],
                "outputs": [{"type": "tuple", "components": [
                    {"name": "token", "type": "address"}, {"name": "curve", "type": "address"},
                    {"name": "deployer", "type": "address"}, {"name": "creatorFeeRecipient", "type": "address"},
                    {"name": "pairToken", "type": "address"}, {"name": "graduationThreshold", "type": "uint256"},
                    {"name": "poolFee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"},
                    {"name": "creatorTaxBps", "type": "uint16"}, {"name": "buybackEnabled", "type": "bool"},
                    {"name": "phase", "type": "uint8"}, {"name": "sweptQuote", "type": "uint256"},
                    {"name": "sweptTokens", "type": "uint256"}, {"name": "sweptAt", "type": "uint256"},
                    {"name": "exists", "type": "bool"}]}]}]
ABI_CURVA = [
    {"type": "function", "name": "buy", "stateMutability": "payable",
     "inputs": [{"name": "quoteIn", "type": "uint256"}, {"name": "minTokensOut", "type": "uint256"}, {"name": "recipient", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "sell", "stateMutability": "nonpayable",
     "inputs": [{"name": "tokensIn", "type": "uint256"}, {"name": "minQuoteOut", "type": "uint256"}, {"name": "recipient", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "getReserves", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
    {"type": "function", "name": "sellableTokens", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "feeBps", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "creatorTaxBps", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "currentSnipeTaxBps", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "graduated", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "isNativeQuote", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bool"}]},
]
ABI_ERC20 = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"type": "function", "name": "symbol", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
]


class Pons:
    def __init__(self, rpc=RPC):
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 30}))
        self.fabrica = self.w3.eth.contract(address=Web3.to_checksum_address(FABRICA), abi=ABI_FABRICA)

    # ----- leitura -----
    def lancamento(self, token):
        lt = self.fabrica.functions.getLaunchedToken(Web3.to_checksum_address(token)).call()
        return {'token': lt[0], 'curva': lt[1], 'pairToken': lt[4], 'creatorTaxBps': int(lt[8]),
                'phase': int(lt[10]), 'exists': bool(lt[14]), 'na_curva': int(lt[10]) == 0 and bool(lt[14])}

    def curva(self, endereco):
        return self.w3.eth.contract(address=Web3.to_checksum_address(endereco), abi=ABI_CURVA)

    def erc20(self, endereco):
        return self.w3.eth.contract(address=Web3.to_checksum_address(endereco), abi=ABI_ERC20)

    def estado_curva(self, curva_addr, recipient):
        c = self.curva(curva_addr)
        R, T = c.functions.getReserves().call()
        return {'R': int(R), 'T': int(T), 'sellable': int(c.functions.sellableTokens().call()),
                'feeBps': int(c.functions.feeBps().call()), 'taxBps': int(c.functions.creatorTaxBps().call()),
                'snipeBps': int(c.functions.currentSnipeTaxBps(Web3.to_checksum_address(recipient)).call()),
                'graduated': bool(c.functions.graduated().call()), 'nativo': bool(c.functions.isNativeQuote().call()),
                'preco_eth': (int(R) / int(T)) if int(T) else 0.0}

    def saldo_eth(self, endereco):
        return self.w3.eth.get_balance(Web3.to_checksum_address(endereco)) / 1e18

    def saldo_token(self, token, endereco):
        return self.erc20(token).functions.balanceOf(Web3.to_checksum_address(endereco)).call()

    # ----- cotacao (mesma conta da doc: taxas saem da entrada, so o resto move a curva) -----
    @staticmethod
    def cotar_compra(quote_in, R, T, sellable, fee_bps, tax_bps, snipe_bps=0):
        snipe = min(snipe_bps, max(0, BPS - fee_bps - tax_bps - 100)) if snipe_bps > 0 else 0
        net = quote_in - quote_in * fee_bps // BPS - quote_in * tax_bps // BPS - quote_in * snipe // BPS
        out = net * T // (R + net)
        return min(out, sellable)

    @staticmethod
    def cotar_venda(tokens_in, R, T, fee_bps, tax_bps):
        bruto = tokens_in * R // (T + tokens_in)
        return bruto - bruto * fee_bps // BPS - bruto * tax_bps // BPS

    # ----- transacoes -----
    def _params(self, conta, valor=0):
        preco_gas = int(self.w3.eth.gas_price)
        return {'from': conta.address, 'value': int(valor), 'chainId': CHAIN_ID,
                'nonce': self.w3.eth.get_transaction_count(conta.address),
                'maxFeePerGas': preco_gas * 2 + 1, 'maxPriorityFeePerGas': preco_gas}

    def _enviar(self, conta, fn, valor=0):
        """Monta (simulando, o que ja estima o gas), assina com a conta e espera o recibo. Falha = excecao."""
        tx = fn.build_transaction(self._params(conta, valor))
        tx['gas'] = int(tx['gas'] * 1.3)
        assinada = conta.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(assinada.raw_transaction)
        hx = Web3.to_hex(h)
        rec = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if rec['status'] != 1:
            raise RuntimeError(f'transacao revertida: {hx}')
        return hx, rec

    def simular_compra(self, conta, curva_addr, eth_in, min_tokens_out):
        """So estima o gas da compra (a chain simula a chamada). Nao envia nada."""
        wei = int(eth_in * 1e18)
        return self.curva(curva_addr).functions.buy(wei, int(min_tokens_out), conta.address).estimate_gas(
            {'from': conta.address, 'value': wei})

    def comprar(self, conta, curva_addr, eth_in, min_tokens_out):
        wei = int(eth_in * 1e18)
        fn = self.curva(curva_addr).functions.buy(wei, int(min_tokens_out), conta.address)
        return self._enviar(conta, fn, wei)

    def vender(self, conta, curva_addr, token, tokens_in, min_quote_out):
        t = self.erc20(token)
        curva_cs = Web3.to_checksum_address(curva_addr)
        if t.functions.allowance(conta.address, curva_cs).call() < tokens_in:
            self._enviar(conta, t.functions.approve(curva_cs, 2 ** 256 - 1))
        fn = self.curva(curva_addr).functions.sell(int(tokens_in), int(min_quote_out), conta.address)
        return self._enviar(conta, fn)
