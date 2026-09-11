# Carteira da mosca: chave privada cifrada num keystore (padrao Ethereum, scrypt) em mercado/carteira.json,
# que o git ignora. A chave nunca passa pelo chat, pelo log nem pela tela: entra pelo terminal, sem eco.
#
#   py\Scripts\python.exe mercado\carteira.py gerar      cria uma carteira nova (recomendado)
#   py\Scripts\python.exe mercado\carteira.py importar   cola uma chave existente (digitada as escuras)
#   py\Scripts\python.exe mercado\carteira.py endereco   mostra o endereco publico
#
# A senha do keystore vai para a variavel FLY_CARTEIRA_SENHA quando o modo real existir. O codigo da mosca
# nunca tera funcao de saque: so compra e venda na curva da Pons. Para tirar dinheiro, e o Michel, com a chave.
import getpass
import json
import sys
import os
from pathlib import Path

from eth_account import Account

PASTA = Path(os.environ.get('FLY_MERCADO_DIR') or Path(__file__).resolve().parent)   # FLY PAD: carteira por mosca
ARQUIVO = PASTA / 'carteira.json'
ARQ_SENHA = PASTA / 'carteira.senha'   # modo automatico: senha aleatoria local (git ignora)


def carregar():
    """Abre o keystore com a senha de FLY_CARTEIRA_SENHA ou do arquivo local. Devolve a conta (eth_account)."""
    import os
    senha = os.environ.get('FLY_CARTEIRA_SENHA') or (ARQ_SENHA.read_text().strip() if ARQ_SENHA.exists() else None)
    if not senha or not ARQUIVO.exists():
        raise RuntimeError('sem keystore ou senha: rode mercado/carteira.py gerar')
    return Account.from_key(Account.decrypt(json.loads(ARQUIVO.read_text()), senha))


def salvar(conta, senha):
    ks = Account.encrypt(conta.key, senha)
    ARQUIVO.write_text(json.dumps(ks, indent=1))
    print(f'keystore salvo em {ARQUIVO}')
    print(f'endereco da mosca: {conta.address}')
    print('Guarde a senha. Sem ela, o keystore e inutil e o dinheiro fica preso.')


def pedir_senha(confirmar=True):
    s1 = getpass.getpass('senha do keystore (nao aparece ao digitar): ')
    if len(s1) < 8:
        sys.exit('senha curta demais (minimo 8)')
    if confirmar and getpass.getpass('repita a senha: ') != s1:
        sys.exit('as senhas nao batem')
    return s1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'gerar':
        if ARQUIVO.exists():
            sys.exit(f'ja existe {ARQUIVO}; apague antes se quiser outra')
        if '--auto' in sys.argv:
            # sem digitar nada: senha aleatoria de 32 bytes guardada em carteira.senha, ao lado do keystore
            import secrets
            senha = secrets.token_urlsafe(32)
            ARQ_SENHA.write_text(senha)
            conta = Account.create()
            ks = Account.encrypt(conta.key, senha)
            ARQUIVO.write_text(json.dumps(ks, indent=1))
            print(f'keystore: {ARQUIVO}\nsenha:    {ARQ_SENHA} (nao mostrada)\nendereco da mosca: {conta.address}')
            return
        senha = pedir_senha()
        salvar(Account.create(), senha)
    elif cmd == 'importar':
        if ARQUIVO.exists():
            sys.exit(f'ja existe {ARQUIVO}; apague antes se quiser outra')
        chave = getpass.getpass('chave privada (nao aparece ao digitar): ').strip()
        try:
            conta = Account.from_key(chave)
        except Exception:
            sys.exit('chave invalida')
        senha = pedir_senha()
        salvar(conta, senha)
    elif cmd == 'endereco':
        if not ARQUIVO.exists():
            sys.exit('nao ha keystore; rode gerar ou importar')
        end = json.loads(ARQUIVO.read_text()).get('address', '?')
        print(end if end.startswith('0x') else '0x' + end)
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
