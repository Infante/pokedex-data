#!/usr/bin/env python3
"""Builds the card data the app reads instead of calling TCGdex. Standard library only.

Files, in OUT_DIR:
- catalog.json: eras, their sets, and every card's details (name, art, rarity, illustrator, types, variants).
- prices.json: each card's TCGplayer market prices (USD, per variant) and Cardmarket 30-day average (EUR).
- products.json: each card's TCGplayer product ids, for the daily price update. Not used by the app.
Each is also written raw-deflate compressed (`.deflate`, what the app downloads and bundles), next to manifest.json,
which gives each file's content hash and build time so the app only downloads what changed.

Commands:
  full OUT_DIR [CACHE_DIR]  Everything from TCGdex, one request per card (~21,000), so it takes a couple of hours:
                            TCGdex has no bulk endpoint and slows down busy clients. Weekly is plenty, since cards
                            only change when sets come out. Files already in OUT_DIR fill in any card TCGdex won't
                            serve this time; CACHE_DIR keeps responses so an interrupted run can resume.
  prices OUT_DIR            Today's TCGplayer prices from tcgcsv.com, which mirrors TCGplayer's whole price list
                            (~220 requests, under a minute). Needs products.json and prices.json from a full run;
                            Cardmarket prices are kept from it.
  pack OUT_DIR              Only redo the compressed files and manifest.

Run on a schedule by .github/workflows/build.yml, which publishes the files to this repo's `data` release.
"""

import hashlib
import json
import random
import sys
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

TCGDEX = "https://api.tcgdex.net/v2/en"
# Pokémon is category 3 on TCGplayer.
TCGCSV = "https://tcgcsv.com/tcgplayer/3"
HEADERS = {"User-Agent": "pokedex-catalog/1"}
# TCG Pocket is a digital-only game.
SKIP_ERAS = {"tcgp"}
WORKERS = 6
FORMAT = 1
CACHE_AGE = 3 * 24 * 60 * 60
# Stands in for a response that never came, as opposed to None for one the server says doesn't exist.
FAILED = object()


def log(message):
    print(message, file=sys.stderr, flush=True)


def get(url, attempts=8):
    """A JSON response, or None for a 404. TCGdex answers 503 when busy, so failures are retried for a few minutes."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
        time.sleep(min(60, 2 ** attempt) + random.random())


def get_or_failed(url, cache=None):
    """Like `get`, but a fetch that keeps failing gives FAILED, so one bad card doesn't sink the run."""
    file = cache / (url.rsplit("/", 2)[-2] + "_" + url.rsplit("/", 1)[-1] + ".json") if cache else None
    if file and file.exists() and time.time() - file.stat().st_mtime < CACHE_AGE:
        return json.loads(file.read_text())
    try:
        value = get(url)
    except Exception as error:  # noqa: BLE001 - logged and counted by the caller
        log(f"  gave up on {url}: {error}")
        return FAILED
    if file and value is not None:
        file.write_text(json.dumps(value))
    return value


def pool_map(fn, items, label):
    results = []
    with ThreadPoolExecutor(WORKERS) as pool:
        for done, result in enumerate(pool.map(fn, items), 1):
            results.append(result)
            if done % 500 == 0 or done == len(items):
                log(f"  {label}: {done}/{len(items)}")
    return results


def strip_none(d):
    return {k: v for k, v in d.items() if v not in (None, [], {})}


def read(out, name):
    """A file from OUT_DIR, from its JSON or else its compressed copy; None if neither is there."""
    try:
        return json.loads((out / f"{name}.json").read_text())
    except OSError:
        pass
    try:
        return json.loads(zlib.decompress((out / f"{name}.deflate").read_bytes(), -15))
    except OSError:
        return None


def write(out, name, value):
    (out / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def save(out, name, body):
    """Writes `body` stamped with when it last changed: now, or the old stamp if it's the same as before, so phones
    don't download data they already have."""
    old = read(out, name) or {}
    same = {k: v for k, v in old.items() if k != "updated"} == body
    stamp = old["updated"] if same and "updated" in old else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write(out, name, {**body, "updated": stamp})


# MARK: - full


def card_details(card, set_id):
    """The fields the app shows. `id` is left out when it's the usual "<set id>-<number>"; variants are letters."""
    variants = card.get("variants") or {}
    letters = "".join(code for key, code in (("normal", "n"), ("reverse", "r"), ("holo", "h"), ("firstEdition", "1"))
                      if variants.get(key))
    return strip_none({
        "id": None if card["id"] == f"{set_id}-{card['localId']}" else card["id"],
        "localId": card["localId"],
        "name": card["name"],
        "image": card.get("image"),
        "rarity": card.get("rarity"),
        "illustrator": card.get("illustrator"),
        "types": card.get("types"),
    }) | {"variants": letters}


def card_prices(card):
    pricing = card.get("pricing") or {}
    tcgplayer = pricing.get("tcgplayer") or {}
    usd = {k: v["marketPrice"] for k, v in tcgplayer.items()
           if isinstance(v, dict) and v.get("marketPrice") is not None}
    return strip_none({"usd": usd, "eur": (pricing.get("cardmarket") or {}).get("avg30")})


def card_products(card):
    ids = {(v.get("thirdParty") or {}).get("tcgplayer") for v in card.get("variants_detailed") or []}
    return sorted(i for i in ids if i)


def full(out, cache):
    previous = read(out, "catalog") or {"eras": []}
    old_details = {c.get("id") or f"{s['id']}-{c['localId']}": c
                   for era in previous["eras"] for s in era["sets"] for c in s["cards"]}
    old_prices = (read(out, "prices") or {}).get("cards", {})
    old_products = read(out, "products") or {}

    # Eras and sets are few, so any failure there stops the run rather than publishing a catalog with holes.
    eras = [e for e in get(f"{TCGDEX}/series") if e["id"] not in SKIP_ERAS]
    log(f"{len(eras)} eras")
    era_details = pool_map(lambda e: get(f"{TCGDEX}/series/{e['id']}"), eras, "eras")
    set_ids = [s["id"] for era in era_details for s in era["sets"]]
    sets = dict(zip(set_ids, pool_map(lambda i: get(f"{TCGDEX}/sets/{i}"), set_ids, "sets")))

    card_ids = [brief["id"] for set_id in set_ids if sets[set_id] for brief in sets[set_id]["cards"]]
    cards = dict(zip(card_ids, pool_map(lambda i: get_or_failed(f"{TCGDEX}/cards/{i}", cache), card_ids, "cards")))

    catalog_eras, prices, products, kept, bare = [], {}, {}, 0, 0
    for era in era_details:
        catalog_sets = []
        for brief in era["sets"]:
            s = sets[brief["id"]]
            if not s:
                continue
            set_cards = []
            for c in s["cards"]:
                full_card = cards.get(c["id"])
                if full_card is FAILED and c["id"] in old_details:
                    # Couldn't fetch it this time; last run's copy is better than nothing.
                    kept += 1
                    set_cards.append(old_details[c["id"]])
                    for old, new in ((old_prices, prices), (old_products, products)):
                        if c["id"] in old:
                            new[c["id"]] = old[c["id"]]
                    continue
                if full_card is FAILED or full_card is None:
                    # TCGdex lists the card in its set but won't serve it; keep what the set says.
                    bare += 1
                    full_card = c
                set_cards.append(card_details(full_card, s["id"]))
                if found := card_prices(full_card):
                    prices[c["id"]] = found
                if found := card_products(full_card):
                    products[c["id"]] = found
            catalog_sets.append(strip_none({
                "id": s["id"], "name": s["name"], "logo": s.get("logo"), "releaseDate": s.get("releaseDate"),
                "cardCount": {"official": s["cardCount"].get("official", 0), "total": s["cardCount"].get("total", 0)},
            }) | {"cards": set_cards})
        catalog_eras.append(strip_none({
            "id": era["id"], "name": era["name"], "logo": era.get("logo"), "releaseDate": era.get("releaseDate"),
        }) | {"sets": catalog_sets})

    failed = sum(1 for v in cards.values() if v is FAILED)
    total = sum(len(s["cards"]) for e in catalog_eras for s in e["sets"])
    log(f"{total} cards in {len(set_ids)} sets, {len(prices)} priced; {failed} failed ({kept} kept from last run), "
        f"{bare} with name and art only")
    if failed - kept > total * 0.02:
        sys.exit("Too many cards failed to load; not writing a catalog. Run again later.")

    save(out, "catalog", {"format": FORMAT, "eras": catalog_eras})
    save(out, "prices", {"format": FORMAT, "cards": prices})
    write(out, "products", products)
    pack(out)


# MARK: - prices


def variant_key(sub_type):
    """TCGplayer's "Reverse Holofoil" is TCGdex's "reverse-holofoil"."""
    return sub_type.lower().replace(" ", "-")


def refresh_prices(out):
    products, previous = read(out, "products"), read(out, "prices")
    if products is None or previous is None:
        sys.exit("prices needs products.json and prices.json from a full run in OUT_DIR.")
    old = previous["cards"]

    groups = get(f"{TCGCSV}/groups")["results"]
    pages = pool_map(lambda g: get(f"{TCGCSV}/{g['groupId']}/prices"), groups, "groups")
    market = {}
    for page in pages:
        for row in (page or {}).get("results", []):
            if row.get("marketPrice") is not None:
                market.setdefault(row["productId"], {})[variant_key(row["subTypeName"])] = row["marketPrice"]
    log(f"{len(market)} TCGplayer products priced")
    if len(market) < len(products) // 2:
        sys.exit("tcgcsv returned too few prices; keeping yesterday's.")

    prices, moved = {}, 0
    for card in set(old) | set(products):
        usd = {}
        for product in products.get(card, []):
            for variant, price in market.get(product, {}).items():
                usd.setdefault(variant, price)
        before = old.get(card, {})
        # A product TCGplayer stopped listing keeps its last price.
        entry = strip_none({"usd": usd or before.get("usd"), "eur": before.get("eur")})
        if entry:
            prices[card] = entry
            moved += entry.get("usd") != before.get("usd")
    log(f"{len(prices)} cards priced, {moved} changed")
    save(out, "prices", {"format": FORMAT, "cards": prices})
    pack(out)


# MARK: - pack


def pack(out):
    """Compresses the files and writes the manifest. Hashes leave out the `updated` stamp, so a run that found
    nothing new doesn't make every phone download the catalog again."""
    manifest = {"format": FORMAT}
    for name in ("catalog", "prices", "products"):
        file = out / f"{name}.json"
        if not file.exists():
            continue
        raw = file.read_bytes()
        squeeze = zlib.compressobj(9, zlib.DEFLATED, -15)
        (out / f"{name}.deflate").write_bytes(squeeze.compress(raw) + squeeze.flush())
        data = json.loads(raw)
        if name != "products":
            content = json.dumps({k: v for k, v in data.items() if k != "updated"}, sort_keys=True).encode()
            manifest[name] = {"hash": hashlib.sha256(content).hexdigest()[:16], "updated": data["updated"]}
    (out / "manifest.json").write_text(json.dumps(manifest))


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("full", "prices", "pack") or len(args) > (3 if args[0] == "full" else 2):
        sys.exit(__doc__)
    out = Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    if args[0] == "full":
        cache = Path(args[2]) if len(args) == 3 else None
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
        full(out, cache)
    elif args[0] == "prices":
        refresh_prices(out)
    else:
        pack(out)


if __name__ == "__main__":
    main()
