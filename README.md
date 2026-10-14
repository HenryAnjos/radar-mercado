# Radar Mercado · bot de sinais Quotex → Telegram

O bot lê candles da Quotex, roda a estratégia e publica no grupo:

1. **Abertura da sessão** (imagem "Operações iniciando") quando uma janela de `ALLOWED_WINDOWS` começa.
2. **Sinal** no padrão Radar Mercado, com entrada na abertura do próximo candle (cerca de 1 min de antecedência no M1).
3. **Resultado** (WIN, LOSS ou EMPATE) após a expiração, com o placar da sessão.
4. **Relatório** ao fim da janela, depois que todas as entradas tiverem resultado: wins, losses, taxa, lista de entradas, placar do dia e da semana.

> O bot não executa ordens. Usa a biblioteca não oficial PyQuotex só para ler candles.

## Como o resultado é calculado

- Preço de entrada: **abertura** do candle do horário de entrada.
- Preço final: **fechamento** do último candle dentro da expiração.
- CALL ganha se o final for maior que a entrada; PUT, se for menor; igual é EMPATE (fica fora da taxa).
- Se faltar candle, a entrada vira "resultado indisponível" e não entra no placar.

É uma aproximação: o preço real de quem clicou pode variar alguns ticks.

## Instalação (Windows)

Requer Python 3.12+ (python.org, marque "Add to PATH").

```powershell
cd caminho\para\radar-mercado
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Preencha o `.env` com a conta **demo** da Quotex, o token do bot e o ID de um **grupo de teste**.

## Executar

```powershell
.\.venv\Scripts\Activate.ps1
python -m src.main
```

## Testes

Sem rede e sem credenciais:

```powershell
pip install pytest
python -m pytest -q
```

## Deploy no Railway

1. New Project → Deploy from GitHub repo → escolha este repositório. O `railway.json` manda usar o `Dockerfile`.
2. No serviço, aba **Variables**: cadastre as mesmas variáveis do `.env.example` (sem o arquivo `.env`).
3. Clique com o botão direito no serviço → **Attach Volume**, mount path `/data`. Sem isso o placar zera a cada deploy.
4. Mantenha **1 réplica**. Duas instâncias mandariam sinais duplicados.
5. Acompanhe a aba **Logs**: deve aparecer "Conectado. Lendo somente candles…".

## Backtest

O bot grava os candles de todos os pares em `DATA_DIR/candles/`. Depois de um ou dois dias rodando:

```
python -m src.backtest
```

No Railway, rode na aba **Console** do serviço. A ferramenta compara variações (confirmações, expiração,
filtro de tendência, filtro de mercado parado) e mostra a taxa de acerto nos primeiros 70% e nos últimos 30%
dos dados. Só vale trocar a configuração se a variação for melhor nas duas partes.

## Arquivos de auditoria (`data/`)

- `state.json`: sessão aberta, entradas aguardando resultado e último candle lido. Permite reiniciar sem perder nada.
- `events.jsonl`: todos os sinais e resultados, em ordem. Nunca apague losses daqui.
- `candles/`: candles fechados de cada par, para o backtest.

## Avisos

- As estratégias em `src/strategy.py` (auto, tendencia, reversao, classica) não foram validadas com dados reais. Rode o backtest antes do grupo oficial.
- Não use martingale nem a função `buy()` da PyQuotex.
- Se a Quotex pedir código PIN/2FA no login, o bot para com erro claro (não há terminal no servidor). Use uma conta demo sem 2FA.
- A PyQuotex está fixada num commit revisado em `requirements.txt`. Antes de trocar o hash, revise o diff da biblioteca.
- Para rodar todos os dias, use um servidor (VPS) ou deixe o PC ligado nos horários das sessões.
