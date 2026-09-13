# -*- coding: utf-8 -*-
"""
Wallet PnL for tracked tokens — by SWAP LEGS (the exact method).

Why not "price from candle history at the transfer moment":
  * a transfer ≠ a trade: repliers/refunds/airdrops look like buy/sell;
  * GeckoTerminal price history by pairId on Robinhood Chain lies (measured:
    LIBTARD "bought at $160/token" at the real price $0.00004 → −$20M).

What we do instead: take the tracked token''s transfers for the wallet from
Blockscout, and for each transaction fetch ALL its ERC20 legs. If the wallet
received the token and simultaneously SENT USDC/GLD/WETH — that is a buy, and its price
= sum of the sent quotes in USD (what was actually paid). If it
gave the token up and received quotes — a sale with real proceeds. These are
the "entry/exit points" without guessing from candles.

USD value of quotes: stables = 1.0; the rest — current DexScreener price of
the quote token''s most liquid pool (6 h cache).
For short trades (hours) quote drift is immaterial; for entries older
than 48 h a partial-accuracy flag is set.

Solana: marked "not supported in v1".
"""

import calendar
import datetime
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BS_CHAINS = {
    "ethereum": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "robinhood": "https://robinhoodchain.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
    "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
    "gnosis": "https://gnosis.blockscout.com",
    "scroll": "https://scroll.blockscout.com",
    "linea": "https://linea.blockscout.com",
}
STABLES = {"USDC", "USDT", "DAI", "USDG", "FDUSD", "PYUSD", "EURC", "USDBC"}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Accept": "application/json"}

_txlegs_lock = threading.Lock()


def _get(url, timeout=15, tries=2):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            # 5xx/429 — transient Blockscout issues: retry with a pause
            if e.code in (429, 500, 502, 503, 504) and k + 1 < tries:
                last = f"HTTP {e.code}"
                time.sleep(2.0 + 2.5 * k)
                continue
            return None, f"HTTP {e.code}"
        except Exception as e:
            last = repr(e)[:80]
            time.sleep(1.0)
    return None, last


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(dt.utctimetuple()) * 1000
    except Exception:
        return None


# --------------------------------------------------- public RPC (legs)
# Blockscout /transactions/{tx}/token-transfers on Robinhood Chain is closed by
# Cloudflare (403), so swap legs are taken from transaction logs via
# public JSON-RPC — more reliable and without explorer limits.
BASE_RPCS = {
    "robinhood": ["https://rpc.mainnet.chain.robinhood.com"],
    "base": ["https://mainnet.base.org", "https://base.llamarpc.com"],
    # llamarpc requires a key; publicnode/drpc/1rpc — public working ones
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org",
                 "https://1rpc.io/eth"],
    "arbitrum": ["https://arb1.arbitrum.io/rpc"],
    "optimism": ["https://mainnet.optimism.io"],
    "polygon": ["https://polygon-rpc.com"],
    "gnosis": ["https://rpc.gnosischain.com"],
    "scroll": ["https://rpc.scroll.io"],
    "linea": ["https://rpc.linea.build"],
}
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


RPC_UA = {"Content-Type": "application/json", "User-Agent": "curl/8.7"}


def _rpc_call(chain, method, params, timeout=12):
    urls = BASE_RPCS.get(chain) or []
    last = None
    for url in urls:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params}).encode()
        try:
            req = urllib.request.Request(url, data=body, headers=dict(RPC_UA))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            if "result" in d and d["result"] is not None:
                return d["result"], None
            last = str(d.get("error"))[:100]
        except Exception as e:
            last = repr(e)[:90]
    return None, last


def _evm_addr(topic_hex):
    return "0x" + topic_hex[-40:].lower()


def _erc20_meta(chain, tok, _cache={}):
    """(symbol, decimals) of a token via eth_call; cached for the process lifetime."""
    key = (chain, tok)
    if key in _cache:
        return _cache[key]
    sym, dec = "", 18
    r, _ = _rpc_call(chain, "eth_call",
                     [{"to": tok, "data": "0x95d89b41"}, "latest"])
    if r and len(r) > 130:
        try:
            ln = int(r[66:130], 16) * 2
            sym = bytes.fromhex(r[130:130 + ln]).decode("ascii", "ignore").strip("\x00")
        except Exception:
            pass
    r, _ = _rpc_call(chain, "eth_call",
                     [{"to": tok, "data": "0x313ce567"}, "latest"])
    if r and len(r) >= 66:
        try:
            dec = int(r[2:66], 16)
            if dec > 36:
                dec = 18
        except Exception:
            pass
    if len(_cache) > 3000:
        _cache.clear()
    _cache[key] = (sym.upper(), dec)
    return sym.upper(), dec


def rpc_tx_legs(chain, txhash, _cache={}):
    """ERC20 transfer legs of a transaction, from RPC logs."""
    key = (chain, txhash)
    if key in _cache:
        return _cache[key]
    rcpt, err = _rpc_call(chain, "eth_getTransactionReceipt", [txhash])
    if rcpt is None:
        return None
    legs = []
    for lg in rcpt.get("logs") or []:
        topics = lg.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TOPIC_TRANSFER:
            continue
        tok = lg.get("address", "").lower()
        try:
            raw = int((lg.get("data") or "0x0")[2:] or "0", 16)
        except Exception:
            continue
        if raw <= 0:
            continue
        sym, dec = _erc20_meta(chain, tok)
        legs.append({"tok": tok, "sym": sym,
                     "from": _evm_addr(topics[1]), "to": _evm_addr(topics[2]),
                     "qty": raw / (10 ** dec)})
    if len(_cache) > 6000:
        _cache.clear()
    _cache[key] = legs
    return legs


# ------------------------------------------------- Blockscout: events


def bs_token_transfers(chain, wallet, token):
    """Transfers of the tracked token for a wallet (the checksum in token= matters).
    Depth — like everywhere else: incremental cache cached_get_all (max_pages=60)."""
    base = BS_CHAINS.get(chain)
    if not base:
        return None, f"нет Blockscout для сети {chain}"
    events = []
    wl = wallet.lower()
    items, ok = cached_get_all(
        f"{base}/api/v2/addresses/{wallet}/token-transfers?token={token}")
    if not ok:
        return None, "Blockscout недоступен"
    for it in items:
        tok = it.get("token") or {}
        if (tok.get("address_hash") or "").lower() != token.lower():
            continue
        total = it.get("total")
        dec = int(tok.get("decimals") or 18)
        if isinstance(total, dict):
            dec = int(total.get("decimals") or dec)
            val = float(total.get("value") or 0)
        else:
            val = float(total or 0)
        qty = val / (10 ** dec)
        if qty <= 0:
            continue
        frm = ((it.get("from") or {}).get("hash") or "").lower()
        to = ((it.get("to") or {}).get("hash") or "").lower()
        ts_ms = _parse_ts(it.get("timestamp"))
        if ts_ms is None or not it.get("transaction_hash"):
            continue
        events.append({"ts": ts_ms, "qty": qty, "tx": it["transaction_hash"],
                       "in": to == wl, "out": frm == wl})
    events.sort(key=lambda e: e["ts"])
    return events, None


_TXLEGS = {}   # (chain,tx) -> legs|None


def bs_tx_legs(chain, txhash):
    """ERC20 legs of a transaction: Blockscout first; on failure (Cloudflare 403 etc.) —
    transaction logs from public JSON-RPC. Failures are not cached."""
    key = (chain, txhash)
    with _txlegs_lock:
        if key in _TXLEGS:
            return _TXLEGS[key]
    legs = None
    base = BS_CHAINS.get(chain)
    if base:
        d, err = _get(f"{base}/api/v2/transactions/{txhash}/token-transfers", tries=1)
        if d is not None:
            legs = _legs_from_bs(chain, d)
    if legs is None:
        legs = rpc_tx_legs(chain, txhash)
    if legs is not None:
        with _txlegs_lock:
            if len(_TXLEGS) > 6000:
                _TXLEGS.clear()
            _TXLEGS[key] = legs
    return legs


def _legs_from_bs(chain, d):
    legs = []
    for it in d.get("items") or []:
        tok = it.get("token") or {}
        total = it.get("total")
        dec = int(tok.get("decimals") or 18)
        if isinstance(total, dict):
            dec = int(total.get("decimals") or dec)
            val = float(total.get("value") or 0)
        else:
            val = float(total or 0)
        if val <= 0:
            continue
        legs.append({"tok": (tok.get("address_hash") or "").lower(),
                     "sym": (tok.get("symbol") or "").upper(),
                     "from": ((it.get("from") or {}).get("hash") or "").lower(),
                     "to": ((it.get("to") or {}).get("hash") or "").lower(),
                     "qty": val / (10 ** dec)})
    return legs


# ------------------------------------------- USD price of quote legs

QUOTE_USD = {}   # (chain,tok) -> (ts, price|None)
_Q_TTL_POS = 10 * 60     # known price — keep 10 min: memecoins slide for hours
                         # between quotes; a 6h TTL showed DeBank $202 while the
                         # dashboard drew $220 purely from a stale SCHIFFY price
_Q_TTL_NEG = 10 * 60     # "no pool" — 10 min: a DexScreener timeout must not
                         # hide real prices for hours


# counterparty tokens whose pools are trusted as market anchors
MAJOR_QUOTES = {"WETH", "ETH", "WBTC", "USDC", "USDT", "USDG", "USDT0", "DAI",
                "FDUSD", "PYUSD", "EURC", "USDBC"}


def _pool_consensus(cands):
    """USD price from own-chain pools: MEDIAN consensus first, liquidity second.
    cands: [(liq_usd, price_usd, quote_symbol)].

    «Most liquid pool» alone is poisonable: a WBTC/CRO defiswap pool reported
    $522,985,305,301 with fake $7.7B liquidity while 30 honest pools agreed on
    ~$76.7K → $66M phantom balance. Rules:
      2+ pools: keep those within ±15% of the (lower) median, take the most
                liquid of them;
      one pool (no corroboration): accept ONLY against a major quote
                (USDC/USDT/WETH/…), else unpriced — attacker-set spam pools
                quote against junk tokens and must not enter balances."""
    good = [(l, p, q) for l, p, q in cands if p > 0]
    if not good:
        return None
    if len(good) >= 2:
        ps = sorted(p for _, p, _ in good)
        med = ps[(len(ps) - 1) // 2]          # lower median: 2-element poison
        near = [(l, p) for l, p, _ in good if abs(p - med) / med <= 0.15]
        if near:
            return max(near, key=lambda x: x[0])[1]
    majors = [(l, p) for l, p, q in good if (q or "").upper() in MAJOR_QUOTES]
    if majors:
        return max(majors, key=lambda x: x[0])[1]
    if len(good) >= 2:
        return max(good, key=lambda x: x[0])[1]  # wide-spread crowd: best liq
    return None                                   # lone junk-quote pool: unpriced

# ------------------------------------------------------------------ transfers cache
# (url) -> {"key": id-key of the last seen item, "items": [...]}
# Sorting by ts inside the engines exists (bs_token_transfers/_flow_txs), so
# prepending fresh pages is safe. A page fully covered by the cache — stop
# (deeper is only history). Key is (hash log_index/tx): unique,
# requires no datetime parsing.
_TR_CACHE = {}
_TR_LOCK = threading.Lock()


def _tr_key(it):
    return (it.get("transaction_hash") or it.get("hash"),
            it.get("log_index"),
            (it.get("token") or {}).get("address_hash"))


def cached_get_all(url, max_pages=60):
    """Like _get_all, but incremental: Blockscout returns lists newest-first,
    so after the first full pass only pages up to the cache boundary are
    re-read. Bare lists (/token-balances — a snapshot, not history) are always
    fetched fresh and not cached. Returns (items, ok)."""
    base = url
    sep = "&" if "?" in base else "?"
    with _TR_LOCK:
        ent = _TR_CACHE.get(base)
    cached_items = ent["items"] if ent else []
    cached_keys = {_tr_key(i) for i in cached_items}
    items = []
    params = {}
    ok = False
    for _ in range(max_pages):
        q = urllib.parse.urlencode({k: v for k, v in params.items()
                                    if v not in ("", None)})
        d = None
        for a in range(4):
            d, err = _get(base + ((sep + q) if q else ""), timeout=25, tries=2)
            if d is not None:
                break
            time.sleep(1.5 + 1.5 * a)
        if d is None:
            break
        if isinstance(d, list):          # snapshot endpoint: always re-fetched
            ok = True
            return d, True
        if isinstance(d, dict) and "items" not in d:
            break                        # junk response (Blockscout under load
                                         # sometimes sends {} with 200) — NOT counted as success
        ok = True
        page = d.get("items") or []
        new_on_page = 0
        for it in page:
            k = _tr_key(it)
            if k in cached_keys:
                continue
            cached_keys.add(k)
            items.append(it)
            new_on_page += 1
        if cached_items and not new_on_page:
            break                       # hit the cached boundary
        nxt = d.get("next_page_params")
        if not nxt:
            break
        params = dict(nxt)
    all_items = (items + cached_items) if ok else cached_items
    if ok and all_items:
        with _TR_LOCK:
            # LRU instead of wiping the whole cache: re-reading all history on
            # overflow = a request storm at the already-unstable Blockscout.
            # Cap 200: many keys now (3 cycles × chains × wallets/tokens),
            # 60 was not enough — there was an eviction storm and hours without updates.
            _TR_CACHE.pop(base, None)          # move the key to "recent"
            _TR_CACHE[base] = {"items": all_items[:1500]}
            while len(_TR_CACHE) > 200:
                _TR_CACHE.pop(next(iter(_TR_CACHE)))  # dict preserves insertion order
    return all_items, bool(ok or cached_items)


def quote_usd_batch(chain, toks):
    """Warm up a batch of unknown tokens with one DexScreener request (25/req).
    Found prices go into QUOTE_USD; not found — into a short negative
    (10 min): DexScreener often drops out on timeout, and a 6-hour None cache
    hid real prices ($12 from the RH balance vanished exactly that way)."""
    now = time.time()
    need, seen = [], set()
    for t in toks:
        tl = (t or "").lower()
        if not tl or tl in seen:
            continue
        seen.add(tl)
        hit = QUOTE_USD.get((chain, tl))
        if hit:
            age = now - hit[0]
            ttl = _Q_TTL_POS if hit[1] is not None else _Q_TTL_NEG
            if age < ttl:
                continue
        need.append(tl)
    for i in range(0, len(need), 25):
        chunk = need[i:i + 25]
        try:
            d, err = _get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk),
                          timeout=12, tries=1)   # negative is appended on the next cycle
        except Exception:
            d = None
        best = {}   # addr -> [(liq, price, quote_sym)] — own-chain pool set
        wanted = set(chunk)
        seen_any = set()   # address present in the response on ANY chain — not "poolless"
        for p in (d or {}).get("pairs") or []:
            bt = ((p.get("baseToken") or {}).get("address") or "").lower()
            if bt:
                seen_any.add(bt)
            if p.get("chainId") != chain or bt not in wanted:
                # address-first match, like single quote_usd: pairing only by
                # chain lets a poison pool where our token is the QUOTE slip in
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            try:
                pu = float(p["priceUsd"])
            except Exception:
                continue
            if pu > 0:
                best.setdefault(bt, []).append(
                    (liq, pu, ((p.get("quoteToken") or {}).get("symbol") or "")))
        for a in chunk:
            hit = QUOTE_USD.get((chain, a))
            if hit and time.time() - hit[0] < _Q_TTL_POS:
                continue                     # a parallel thread already warmed it
            if a in best:
                px = _pool_consensus(best[a])
                if px:
                    QUOTE_USD[(chain, a)] = (time.time(), px)
            elif d is not None and a not in seen_any:
                QUOTE_USD[(chain, a)] = (time.time(), None)   # no pool anywhere — negative
            # pool exists but on another chain — leave it to single quote_usd
            # (symbol search for proxy tokens like SPY/GLD on RH)


def quote_usd(chain, tok, sym, symbol_fallback=True):
    """What 1 unit of the quote token costs in USD (10-min cache).

    Pool selection = _pool_consensus (median ±15%, then liquidity): the
    highest-liquidity pool alone can be poisoned (WBTC/CRO fake pool at
    $522B/coin with $7.7B fake liq → a $66M phantom balance).

    symbol_fallback=False — for wallet POSITIONS: price strictly from the token''s
    own pool by address. Symbol search is dangerous on drops: a fake airdrop
    "KING" (address indexed nowhere) got the real KING''s price ($227)
    and drew a $134K balance. Swap legs (quotes) still use the fallback."""
    if sym in STABLES:
        return 1.0, False
    tok = tok.lower()
    key = (chain, tok)
    now = time.time()
    hit = QUOTE_USD.get(key)
    addr_cached = bool(hit) and now - hit[0] < (_Q_TTL_POS if hit[1] is not None else _Q_TTL_NEG)
    if addr_cached and hit[1] is not None:
        return hit[1], False
    px = None
    if not addr_cached:
        try:
            d, err = _get(f"https://api.dexscreener.com/latest/dex/tokens/{tok}")
            if d:
                cands = []
                for p in d.get("pairs") or []:
                    if p.get("chainId") != chain:
                        continue
                    bt = ((p.get("baseToken") or {}).get("address") or "").lower()
                    if bt != tok:
                        continue
                    liq = (p.get("liquidity") or {}).get("usd") or 0
                    try:
                        pu = float(p["priceUsd"])
                    except Exception:
                        continue
                    if liq > 0 and pu > 0:
                        cands.append((liq, pu,
                                      ((p.get("quoteToken") or {}).get("symbol") or "")))
                px = _pool_consensus(cands)
        except Exception:
            pass
        QUOTE_USD[key] = (now, px)   # None = "no own pool" (10-min negative)
    if px is not None:
        return px, False
    if not symbol_fallback or not sym:
        return None, True
    # address is not indexed — search by SYMBOL in the same chain (only for
    # swap quote LEGS; positions run with strict=True). Cache the result under a
    # separate key: this is the price of ANOTHER token with the same ticker, not ours.
    skey = (chain, "sym:" + tok)
    hit2 = QUOTE_USD.get(skey)
    if hit2 and now - hit2[0] < (_Q_TTL_POS if hit2[1] is not None else _Q_TTL_NEG):
        return hit2[1], hit2[1] is None
    try:
        d, err = _get("https://api.dexscreener.com/latest/dex/search?q="
                      + urllib.parse.quote(sym))
        if d:
            cands = []
            for p in d.get("pairs") or []:
                if p.get("chainId") != chain:
                    continue
                bt = (p.get("baseToken") or {})
                qt = (p.get("quoteToken") or {})
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if (bt.get("symbol") or "").upper() == sym:
                    try:
                        pu = float(p["priceUsd"])
                    except Exception:
                        continue
                    if liq > 0 and pu > 0:
                        cands.append((liq, pu, (qt.get("symbol") or "")))
                elif (qt.get("symbol") or "").upper() == sym:
                    # token as quote: price = base_usd / base_native
                    try:
                        bn = float(str(p.get("priceNative") or "0").split()[0])
                        pu = float(p["priceUsd"])
                    except Exception:
                        continue
                    if bn > 0 and liq > 0:
                        # derived price; counterparty trust unknown → empty quote
                        # tag: this candidate never wins the "major quote" gate,
                        # it can only pass median consensus
                        cands.append((liq, pu / bn, ""))
            px = _pool_consensus(cands)
    except Exception:
        pass
    QUOTE_USD[skey] = (now, px)
    return px, px is None


# ---------------------------------- 2-tx swaps (Robinhood Uniswap v4 pattern)
# "Sent a stable" and "received the token" arrive as TWO separate transactions.
# Swap legs in the receiving tx are empty → entry price is invisible. Join by time:
# an orphan-OUT (stable went to a contract, no token in this tx) is closed by
# an unpaid token-IN within 30 min. A return from an EOA (withdrawal rollback,
# amount ±15%, ≤15 min) voids the orphan-OUT — it is not a buy.
# Symmetrically for sells: an orphan stable-IN without a token closes
# an unpaid token-OUT in the preceding 30 min.

LINK_TTL = 240.0
_LINK_CACHE = {}   # (chain,wallet) -> (ts, {token_addr: {tx: (usd, dir)}})


def _flow_txs(items, wallet):
    """All wallet transfers -> tx-groups with classified legs."""
    wl = wallet.lower()
    txs = {}
    for it in items:
        tx = it.get("transaction_hash")
        if not tx:
            continue
        tok = it.get("token") or {}
        sym = (tok.get("symbol") or "?").upper()
        addr = (tok.get("address_hash") or "").lower()
        try:
            dec = int(tok.get("decimals") or 18)
        except (TypeError, ValueError):
            dec = 18
        tot = it.get("total")
        try:
            val = float((tot.get("value") if isinstance(tot, dict) else tot) or 0)
        except (TypeError, ValueError):
            val = 0.0
        try:
            val = val / (10 ** dec)
        except Exception:
            val = 0.0
        frm = it.get("from") or {}
        to = it.get("to") or {}
        f_h = (frm.get("hash") or "").lower()
        t_h = (to.get("hash") or "").lower()
        f_c = bool(frm.get("is_contract"))
        t_c = bool(to.get("is_contract"))
        e = txs.setdefault(tx, {"ts": 0, "s_in": 0.0, "s_in_eoa": 0.0,
                                "s_out": 0.0, "s_out_eoa": 0.0,
                                "tin": [], "tout": []})
        e["ts"] = max(e["ts"], _parse_ts(it.get("timestamp")) or 0)
        if sym in STABLES:
            if t_h == wl:
                e["s_in"] += val
                if not f_c:
                    e["s_in_eoa"] += val
            if f_h == wl:
                e["s_out"] += val
                if not t_c:
                    e["s_out_eoa"] += val
        elif val > 0:
            if t_h == wl:
                e["tin"].append(addr)
            if f_h == wl:
                e["tout"].append(addr)
    return txs


def _links_from_txs(txs):
    """Pure joining logic: {token_addr: {tx_in/out: (usd, 'buy'|'sell')}}.

    Two passes so there is no race: first withdrawal rollbacks (IN from an EOA ≈
    a recent OUT amount) cross out payment candidates, then surviving payments
    are matched to unpaid token-INs, and unpaid token-OUTs —
    to orphan stable proceeds.
    """
    W30, W15 = 30 * 60_000, 15 * 60_000
    links = {}
    ordered = sorted(((tx, e) for tx, e in txs.items() if e["ts"]),
                     key=lambda k: k[1]["ts"])
    # pass 1 — payment candidates and rollbacks
    pay_outs = []      # {ts, usd, used}
    for tx, e in ordered:
        if e["s_out"] > 0 and not e["tin"] and e["s_out_eoa"] < e["s_out"] * 0.5:
            pay_outs.append({"ts": e["ts"], "usd": e["s_out"], "used": False})
        if e["s_in_eoa"] > 0:
            for p in pay_outs:
                if p["used"] or not (0 < e["ts"] - p["ts"] <= W15):
                    continue
                if abs(p["usd"] - e["s_in_eoa"]) <= 0.15 * max(p["usd"], 1e-9):
                    p["used"] = True     # withdrawal rollback — not a payment
                    break
    # pass 2 — time matching in one pass over the order
    open_pays = [p for p in pay_outs if not p["used"]]
    open_sells = []  # {ts, tx, addr}
    for tx, e in ordered:
        ts = e["ts"]
        if e["tin"] and not e["s_in"] and not e["s_out"]:
            for a in dict.fromkeys(e["tin"]):
                best = None
                for p in open_pays:
                    if p["used"] or not (0 < ts - p["ts"] <= W30):
                        continue
                    if best is None or ts - p["ts"] < ts - best["ts"]:
                        best = p
                if best:
                    best["used"] = True
                    links.setdefault(a, {})[tx] = (best["usd"], "buy")
        if e["tout"] and not e["s_in"] and not e["s_out"]:
            for a in dict.fromkeys(e["tout"]):
                open_sells.append({"ts": ts, "tx": tx, "addr": a,
                                   "used": False})
        if e["s_in"] > 0 and not e["tout"] and e["s_in_eoa"] < e["s_in"] * 0.5:
            for s in open_sells:
                if s["used"] or not (0 < ts - s["ts"] <= W30):
                    continue
                s["used"] = True
                links.setdefault(s["addr"], {})[s["tx"]] = (e["s_in"], "sell")
                break
    return links


def two_tx_links(chain, wallet):
    """{token_addr: {tx: (usd, dir)}} — cached for LINK_TTL seconds."""
    key = (chain, wallet.lower())
    now = time.time()
    hit = _LINK_CACHE.get(key)
    if hit and now - hit[0] < LINK_TTL:
        return hit[1]
    base = BS_CHAINS.get(chain)
    links = {}
    if base:
        items, _ok = cached_get_all(
            f"{base}/api/v2/addresses/{wallet}/token-transfers")
        if items:
            links = _links_from_txs(_flow_txs(items, wallet))
    _LINK_CACHE[key] = (now, links)
    return links


# ------------------------------------------------ trade extraction


def extract_trades(chain, wallet, token, events, wallets_set, max_txs=250,
                   linked=None):
    """Transfers -> list of trades with the real payment in USD.

    buy:  in the tx the wallet RECEIVED the token and SENT something else (a quote) → usd=value of what was sent
    sell: the wallet GAVE the token and RECEIVED a quote → usd=proceeds
    Other incoming transfers (without payment) — transfers/airdrops: usd=None (lot with unknown basis).
    """
    by_tx = {}
    for e in events:
        by_tx.setdefault(e["tx"], []).append(e)
    wl = wallet.lower()
    tk = token.lower()
    linked = linked or {}
    trades = []
    txs = sorted(by_tx.items(), key=lambda kv: min(e["ts"] for e in kv[1]))
    for tx, evs in txs[:max_txs]:
        qty_in = sum(e["qty"] for e in evs if e["in"])
        qty_out = sum(e["qty"] for e in evs if e["out"])
        ts = min(e["ts"] for e in evs)
        if qty_in and qty_out and qty_in == qty_out:
            continue                    # transfer to self
        legs = bs_tx_legs(chain, tx)
        unknown = legs is None
        leg_unknown = False          # some legs priceless — mark partial,
        cost = proceeds = 0.0        # but keep what was computed
        if legs is not None:
            for l in legs:
                if l["tok"] == tk:
                    continue
                if l["from"] == wl and l["to"] != tk:      # what was paid with
                    px, unk = quote_usd(chain, l["tok"], l["sym"])
                    if unk:
                        leg_unknown = True
                    else:
                        cost += l["qty"] * px
                if l["to"] == wl and l["from"] != tk:      # what was received
                    px, unk = quote_usd(chain, l["tok"], l["sym"])
                    if unk:
                        leg_unknown = True
                    else:
                        proceeds += l["qty"] * px
        link = linked.get(tx)
        if qty_in:
            usd = cost if cost > 0 else None
            part = bool(leg_unknown)
            if usd is None and link and link[1] == "buy":
                usd, unknown = link[0], False
                part = True                    # price from the neighbouring tx — "≈"
            trades.append({"ts": ts, "qty": qty_in, "dir": "buy",
                           "usd": usd,
                           "unknown": (unknown or (usd or 0) <= 0),
                           "partial": part})
        if qty_out:
            usd = proceeds if proceeds > 0 else None
            part = bool(leg_unknown)
            if usd is None and link and link[1] == "sell":
                usd, unknown = link[0], False
                part = True
            trades.append({"ts": ts, "qty": qty_out, "dir": "sell",
                           "usd": usd,
                           "unknown": (unknown or (usd or 0) <= 0),
                           "partial": part})
    trades.sort(key=lambda t: t["ts"])
    return trades


# ------------------------------------------------------------- FIFO


def compute_position(trades, cur_price):
    """FIFO over trades. realized/cost are computed only where the basis is known;
    unknown lots/prices do not spoil the number, they mark partial (~ lower estimate)."""
    trades = [t for t in trades if t["qty"] > 0]
    if not trades:
        return None
    lots = []                      # [{"qty","usd_per":float|None}]
    realized = 0.0
    cost_total = 0.0               # total invested (from known buys)
    partial = False
    buys = sells = 0
    first_ts = last_ts = None
    for t in trades:
        if t.get("partial"):
            partial = True
        if t["dir"] == "buy":
            lots.append({"qty": t["qty"], "usd_per": None if t["unknown"] else t["usd"] / t["qty"]})
            if t["unknown"]:
                partial = True
            else:
                cost_total += t["usd"]
            buys += 1
        else:
            rem = t["qty"]
            sp_unit = None if t["unknown"] else t["usd"] / t["qty"]
            while rem > 0 and lots:
                lot = lots[0]
                take = min(lot["qty"], rem)
                if lot["usd_per"] is not None and sp_unit is not None:
                    realized += take * (sp_unit - lot["usd_per"])
                else:
                    partial = True
                lot["qty"] -= take
                rem -= take
                if lot["qty"] <= 1e-12:
                    lots.pop(0)
            if rem > 1e-12:
                partial = True          # sold more than the lots cover
            sells += 1
        first_ts = first_ts or t["ts"]
        last_ts = max(last_ts or 0, t["ts"])
    open_qty = sum(l["qty"] for l in lots)
    known = [l for l in lots if l["usd_per"] is not None]
    cost_known = sum(l["qty"] * l["usd_per"] for l in known)
    qty_known = sum(l["qty"] for l in known)
    pos_val = open_qty * (cur_price or 0.0)
    unreal = qty_known * (cur_price or 0.0) - cost_known
    if len(known) < len(lots):
        partial = True
    pnl_v = realized + unreal
    scale = max(cost_total + pos_val, 1.0)
    unreliable = abs(pnl_v) > 20.0 * scale or abs(pnl_v) > 5e6
    # % is computed over ALL invested money (closed position: cost_known=0,
    # but there was profit — denominator is cost_total)
    base_pct = cost_total if cost_total > 0 else (cost_known or 0.0)
    return {
        "qty": open_qty, "posValue": pos_val, "cost": cost_known,
        "invested": cost_total,
        "avgCost": cost_known / qty_known if qty_known > 0 else 0.0,
        "realized": realized, "unrealized": unreal,
        "pnl": None if unreliable else pnl_v,
        "pnlPct": (pnl_v / base_pct * 100.0 if base_pct > 0 else None)
                   if not unreliable else None,
        "unreliable": unreliable,
        "buys": buys, "sells": sells,
        "firstTs": first_ts, "lastTs": last_ts, "noPriceData": partial,
    }


# -------------------------------------------------------------- top API

SOL_MSG = "PnL для Solana — скоро (нужен расчёт цен по свопам)"


def compute_pnl_for_tokens(tokens, wallets):
    """{addr: {chain, price, addrChecksum?...}} × wallet list → {addr: result}."""
    wallets = [w for w in wallets if w]
    if not wallets:
        return {}
    wallets_set = {w.lower() for w in wallets}
    out = {}
    for addr, t in tokens.items():
        chain = t.get("chain")
        if chain == "solana":
            out[addr] = {"err": SOL_MSG}
            continue
        if not chain or chain not in BS_CHAINS:
            out[addr] = {"err": f"сеть «{chain or '?'}» пока не поддерживается"}
            continue
        if not t.get("price"):
            out[addr] = {"err": "нет текущей цены"}
            continue
        checksum = t.get("addrChecksum") or addr
        agg = {"qty": 0.0, "posValue": 0.0, "cost": 0.0, "invested": 0.0,
               "realized": 0.0,
               "unrealized": 0.0, "buys": 0, "sells": 0,
               "lastTs": None, "noPriceData": False, "byWallet": {},
               "unreliable": False}
        err_seen = None
        ok_any = False
        links_by_wallet = {}
        for w in wallets:
            events, err = bs_token_transfers(chain, w, checksum)
            if err:
                err_seen = err
                continue
            if not events:
                continue
            if w not in links_by_wallet:
                try:
                    links_by_wallet[w] = two_tx_links(chain, w)
                except Exception:
                    links_by_wallet[w] = {}
            linked = links_by_wallet.get(w) or {}
            token_links = linked.get(addr.lower()) or {}
            trades = extract_trades(chain, w, checksum, events, wallets_set,
                                    linked=token_links)
            pos = compute_position(trades, t.get("price"))
            if pos:
                agg["byWallet"][w] = pos
                for k in ("qty", "posValue", "cost", "invested", "realized",
                          "unrealized", "buys", "sells"):
                    agg[k] += pos[k]
                agg["noPriceData"] |= pos["noPriceData"]
                agg["unreliable"] |= pos["unreliable"]
                agg["lastTs"] = max(agg["lastTs"] or 0, pos["lastTs"] or 0) or None
                ok_any = True
        if ok_any:
            agg["avgCost"] = agg["cost"] / agg["qty"] if agg["qty"] > 0 else 0.0
            agg["pnl"] = agg["realized"] + agg["unrealized"]
            scale = max(agg["cost"] + agg["posValue"], 1.0)
            agg["unreliable"] = agg["unreliable"] or \
                abs(agg["pnl"]) > 20.0 * scale or abs(agg["pnl"]) > 5e6
            if agg["unreliable"]:
                agg["pnlShow"] = None
                agg["pnlPct"] = None
            else:
                agg["pnlShow"] = agg["pnl"]
                base_pct = agg["invested"] or agg["cost"]
                agg["pnlPct"] = (agg["pnl"] / base_pct * 100.0
                                 if base_pct > 0 else None)
            out[addr] = {"pnl": agg}
        else:
            out[addr] = {"err": err_seen or "нет трансферов по этому токену"}
        time.sleep(0.2)
    return out
