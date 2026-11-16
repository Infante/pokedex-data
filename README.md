# pokedex-data

Card data for a personal Pokémon card app, rebuilt automatically so the app never has to wait on an API.

The app downloads these from the `data` release (`releases/download/data/<file>`):

| File | What's in it | Updated |
| --- | --- | --- |
| `manifest.json` | Each file's content hash and when it last changed | Every run |
| `catalog.deflate` | Every English card: eras, sets, names, art links, rarity, illustrator, types, variants | Mondays, from [TCGdex](https://tcgdex.dev) |
| `prices.deflate` | TCGplayer market prices (USD, per variant) and Cardmarket 30-day averages (EUR) | Daily, from [tcgcsv.com](https://tcgcsv.com) (TCGplayer) and TCGdex (Cardmarket) |
| `products.deflate` | Each card's TCGplayer product ids, for the daily price update | Mondays |

`.deflate` files are raw-deflate compressed JSON (`zlib.decompress(data, -15)` in Python).

`build-catalog.py` builds them (Python standard library only); `.github/workflows/build.yml` runs it.
Run `python3 build-catalog.py` for its options.

Card data and images belong to their owners; this repo only mirrors what TCGdex and tcgcsv publish.
