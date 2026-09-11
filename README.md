# FLY PAD

A launchpad on Robinhood Chain (Pons) where every coin gets a whole fruit-fly brain: 138,639 neurons simulated live on a GPU, with the token's trades wired into its senses, a NeuroMechFly body moved by its own descending neurons, and (next) courtship between the flies of different tokens.

- `relay/` — public server (Railway): many flies, one page per token (`/t/<id>`), the colony home (`/`), the hatch queue.
- `gerente/` — runs on the GPU box: keeps the colony, boots one brain + body + market reader per fly, sleeps quiet flies, pulls the hatch queue.
- `brain/`, `corpo/`, `mercado/`, `site/` — the fly itself (from FLY / SEX FLY): connectome LIF brain, body, senses from the token's curve, page.

Run on the GPU box: `py\Scripts\python.exe gerente\gerente.py` with `FLY_RELAY_URL` and `relay.token`. Data: `brain/data/flywire` (connectome), `brain/data/neuronios.parquet`.
