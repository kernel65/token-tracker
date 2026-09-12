# -*- coding: utf-8 -*-
"""
Token Tracker — local token tracker (any network).

Run:  python server.py   →  http://127.0.0.1:8765

Components:
  detector.py  — multi-window movement detection (catches spike-and-return)
  sound.py     — piano sound alerts (Windows)
  telegram.py  — Telegram notifications with the token image
  dashboard.html — UI

DexScreener data (free API, no key):
  https://api.dexscreener.com/latest/dex/tokens/<addr1>,<addr2>
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import detector
import pnl
import portfolio
import sound
import telegram as tgmod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PORT = 8765
# Secrets in /api/state are masked with a marker string; if the client sends
# the marker back when saving settings, the real value is not overwritten.
MASK_MARK = "•••"
# Networks: any (DexScreener identifies the chain by address; we take the most liquid pool).

DEFAULTS = {
    "pollSeconds": 15,
    "cooldownSeconds": 180,
    "pnlIntervalSec": 60,
    "portfolioIntervalSec": 60,
    "scanIntervalSec": 120,
    # auto-scan/portfolio chains: major ETH-native ones (native=ETH -> gas and native
    # are priced the same). Polygon(GPOL)/Gnosis(xDAI)/declining chains: no.
    "chains": ["robinhood", "base", "ethereum", "arbitrum", "optimism"],
    "volume": 70,
    "soundEnabled": True,
    "windows": [
        {"name": "1m", "seconds": 60, "threshold": 8},
        {"name": "5m", "seconds": 300, "threshold": 20},
        {"name": "15m", "seconds": 900, "threshold": 40},
        {"name": "1h", "seconds": 3600, "threshold": 80},
        {"name": "24h", "seconds": 86400, "threshold": 150},
    ],
    "telegram": {"botToken": "", "chatId": ""},
    "adaptive": {"enabled": True, "k": 4.0, "lookbackMin": 30},
    "wallets": [],
}

# ---------------------------------------------------------------- state

STATE = {
    "tokens": {},          # addr -> {meta..., history:[[ts,p]], det:{}, added_ts}
    "wallet_tokens": {},   # wallet(lower)|"*" -> [addr...] - per-wallet watchlists
    "feed": [],            # latest alerts for the UI
    "log": [],             # server log (short)
    "last_poll": 0,
    "poll_error": "",
}

# Auto-scan of a new wallet: "junk" is filtered by position value and
# pool liquidity (cheap drops/scam pairs don't go into the watchlist)
MIN_AUTO_VALUE = 1.0     # position worth more than $1
MIN_AUTO_LIQ = 1000      # pool more liquid than $1000
MIN_KEEP_VALUE = 0.10    # auto-added token's position fell below this - pruned
CONFIG = json.loads(json.dumps(DEFAULTS))
# RLock: save_all()/public_state() take the lock themselves but may be called from
# sections that already hold it (_update_config) - a plain Lock would deadlock.
_LOCK = threading.RLock()
SEARCH_CACHE = {}   # query -> (ts, results)
PNL = {}            # addr -> {"pnl": {...}} | {"err": str}
PNL_META = {"ts": 0, "running": False, "error": ""}
PORTFOLIO = None    # wallets' cash-flow PnL (deposits/withdrawals/current value)
PORTFOLIO_META = {"ts": 0, "running": False, "error": "", "stale": True}


def _log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    # file log: lets the tracker run without a console (hidden)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        p = os.path.join(DATA_DIR, "server.log")
        try:
            if os.path.getsize(p) > 1_000_000:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    STATE["log"] = (STATE["log"] + [line])[-200:]


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def save_all():
    # snapshot under the lock: iterating STATE["tokens"] runs in parallel with
    # _add_tokens/poller - without the lock it's "dictionary changed size during iteration"
    with _LOCK:
        toks = {}
        for a, t in STATE["tokens"].items():
            toks[a] = {k: v for k, v in t.items() if k != "det"}
            toks[a]["det"] = {k: v for k, v in (t.get("det") or {}).items()}
        addrs = list(STATE["tokens"].keys())
        feed = list(STATE["feed"][-120:])
        cfg = json.loads(json.dumps(CONFIG))
        wt = {k: list(v) for k, v in STATE["wallet_tokens"].items()}
    try:
        _atomic_write(os.path.join(DATA_DIR, "tokens.json"), {"addrs": addrs})
        _atomic_write(os.path.join(DATA_DIR, "state.json"),
                      {"tokens": toks, "feed": feed, "wallet_tokens": wt})
        _atomic_write(os.path.join(DATA_DIR, "config.json"), cfg)
    except Exception as e:
        _log(f"[save] error: {e}")


def load_all():
    global CONFIG
    try:
        if os.path.exists(os.path.join(DATA_DIR, "config.json")):
            with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as f:
                user = json.load(f)
            merged = json.loads(json.dumps(DEFAULTS))
            merged.update(user)
            merged["telegram"].update(user.get("telegram") or {})
            merged["adaptive"].update(user.get("adaptive") or {})
            CONFIG = merged
    except Exception as e:
        _log(f"[load config] {e}")
    try:
        p = os.path.join(DATA_DIR, "state.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
            for a, t in (st.get("tokens") or {}).items():
                t.setdefault("det", {})
                STATE["tokens"][a] = t
            STATE["feed"] = st.get("feed") or []
            STATE["wallet_tokens"] = st.get("wallet_tokens") or {}
            # single-list-era migration: unbound tokens -> all wallets
            if not STATE["wallet_tokens"] and STATE["tokens"]:
                init = list(STATE["tokens"].keys())
                for w in (CONFIG.get("wallets") or ["*"]):
                    STATE["wallet_tokens"][w.lower()] = list(init)
            _log(f"[load] восстановлено токенов: {len(STATE['tokens'])}")
    except Exception as e:
        _log(f"[load state] {e}")

# ------------------------------------------------------------ DexScreener

PREFERRED_QUOTES = ["WETH", "ETH", "SOL", "USDG", "GLD", "USDC", "USDT", "DAI", "cbBTC", "WBTC"]

import re as _re
EVM_ADDR_RE = _re.compile(r"^0x[0-9a-fA-F]{40}$")
B58_ADDR_RE = _re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")   # Solana etc.


def normalize_addr(a):
    """EVM (0x…, case-insensitive) -> lowercase; base58 (Solana) -> as-is
    (case is significant). Returns the tracker key, or None if not an address."""
    if EVM_ADDR_RE.match(a):
        return a.lower()
    if B58_ADDR_RE.match(a) and not a.startswith("0x"):
        return a
    return None


def _match_key(raw_addr):
    """Key for matching API responses against our storage."""
    raw = (raw_addr or "").strip()
    return raw.lower() if raw.startswith("0x") else raw


def _http_get_json(url, timeout=20, tries=2):
    """GET JSON with a short retry: DexScreener currently throws timeouts
    consistently through the loop — one instant retry saves the poll tick
    (and thus the detection window) from being missed."""
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if k + 1 < tries:
                time.sleep(0.6)
    raise last


def fetch_batch(addrs):
    """GET latest/dex/tokens → map addr -> best pair (ANY network).

    If an address exists on multiple chains — take the pair with the highest
    liquidity; the network is shown as a badge on the card.
    """
    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(addrs)
    data = _http_get_json(url)
    wanted = set(addrs)
    out = {}
    for pair in data.get("pairs") or []:
        base = (pair.get("baseToken") or {})
        addr = _match_key(base.get("address"))
        if addr not in wanted:
            continue
        liq = (pair.get("liquidity") or {}).get("usd") or 0
        quote = (pair.get("quoteToken") or {}).get("symbol") or ""
        # Rank: liquidity is primary in "bands" ×2 (int.bit_length = log2);
        # WITHIN a band, a preferred quote from PREFERRED_QUOTES — its
        # price is more stable (lower index in the list = better; the
        # "rank > cur" comparison below picks the maximum, unknown quote = worst).
        qrank = -PREFERRED_QUOTES.index(quote) if quote in PREFERRED_QUOTES else -1000
        rank = (1 if liq > 1000 else 0, int(liq).bit_length(), qrank, liq)
        cur = out.get(addr)
        if cur is None or rank > cur["_rank"]:
            pc = pair.get("priceChange") or {}
            pinfo = pair.get("info") or {}
            logo = (base.get("imageUrl") or pinfo.get("imageUrl")
                    or pair.get("imagePreviewUrl") or pinfo.get("header") or "")
            out[addr] = {
                "_rank": rank,
                "symbol": base.get("symbol"),
                "name": base.get("name"),
                "logo": logo,
                "chain": pair.get("chainId"),
                "addrChecksum": base.get("address"),   # EIP-55 for Blockscout filters
                "price": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
                "m5": pc.get("m5"), "h1": pc.get("h1"), "h6": pc.get("h6"),
                "h24": pc.get("h24"),
                "liquidity": liq,
                "mcap": pair.get("marketCap") or pair.get("fdv"),
                "dex": pair.get("dexId"),
                "url": "https://dexscreener.com/{}/{}".format(
                    pair.get("chainId"), pair.get("pairAddress") or ""),
            }
    for v in out.values():
        v.pop("_rank", None)
    return out


def search_tokens(query):
    """Live search by name/ticker/address via DexScreener /search.
    Returns up to 8 unique tokens sorted by liquidity.
    Responses are cached for 60 seconds so we don't hammer the API on every keystroke."""
    q = (query or "").strip()
    if len(q) < 1:
        return []
    key = q.lower()
    hit = SEARCH_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < 60:
        return hit[1]
    url = ("https://api.dexscreener.com/latest/dex/search?q="
           + urllib.parse.quote(q))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        _log(f"[search] {e}")
        return (hit[1] if hit else [])
    seen, rows = set(), []
    pairs = data.get("pairs") or []
    # the API response is large and its order is "floating": first do
    # dedup across the whole list (liquidity leader per pair), then
    # tokens with an image take priority — otherwise the top-8 fills with
    # faceless entries with info:{}, while ones with logos get cut off
    best_pair = {}
    for pair in sorted(pairs, key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0)):
        b = (pair.get("baseToken") or {}).get("address")
        if not b:
            continue
        k = (pair.get("chainId"), b.lower() if b.startswith("0x") else b)
        if k not in best_pair:
            best_pair[k] = pair
    for pair in best_pair.values():
        base = pair.get("baseToken") or {}
        addr = base.get("address")
        if not addr:
            continue
        liq = (pair.get("liquidity") or {}).get("usd") or 0
        pc = pair.get("priceChange") or {}
        h1 = pc.get("h1")
        if liq < 1000 and h1 is None:
            continue
        pinfo = pair.get("info") or {}
        rows.append({
            "addr": addr, "chain": pair.get("chainId"),
            "symbol": base.get("symbol"), "name": base.get("name"),
            "logo": (pinfo.get("imageUrl") or pinfo.get("header")
                     or base.get("imageUrl") or ""),
            "liquidity": liq, "h1": h1,
            "price": pair.get("priceUsd"),
        })
    # letter-relevant ones first (DexScreener already ranks them, but play it safe)
    def rank(r):
        txt = ((r.get("symbol") or "") + " " + (r.get("name") or "")).lower()
        exact = txt.startswith(q.lower()) or r["addr"].lower() == q.lower()
        return (exact, bool(r["logo"]), r["liquidity"])
    rows.sort(key=rank, reverse=True)
    top = rows[:8]
    SEARCH_CACHE[key] = (now, top)
    if len(SEARCH_CACHE) > 200:
        for k in sorted(SEARCH_CACHE, key=lambda k: SEARCH_CACHE[k][0])[:80]:
            SEARCH_CACHE.pop(k, None)
    return top


def seed_history(addr, info, now_ms):
    """Rough history seed from the priceChange API — so the card is alive immediately."""
    price = info.get("price")
    if not price:
        return []
    pts = []
    for key, ms in (("h24", 86400000), ("h6", 21600000), ("h1", 3600000), ("m5", 300000)):
        c = info.get(key)
        if c is None or c <= -99.9:
            continue
        ref = price / (1 + c / 100.0)
        if ref > 0:
            pts.append([now_ms - ms, round(ref, 12)])
    pts.append([now_ms, price])
    pts.sort(key=lambda p: p[0])
    return pts


# ------------------------------------------------ token image proxy
# Problems with direct links to cdn.dexscreener.com: heavy originals (800×800,
# ~1MB) for a 38px avatar, periodic timeouts/referer blocks in the
# webview. Proxy through ourselves: one fetch → disk cache → instant
# serving from localhost; on error — 404, the UI shows a letter-avatar.

import hashlib

LOGO_DIR = os.path.join(DATA_DIR, "logos")
_IMG_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "image/svg+xml": ".svg"}


def _thumb_url(url):
    """The DexScreener CDN supports resizing via ?width=&height= — request 128px
    instead of the 800×800 (~1MB) original; cards load instantly."""
    try:
        parts = urllib.parse.urlsplit(url)
        if "dexscreener" not in parts.netloc:
            return url
        q = urllib.parse.parse_qs(parts.query)
        if "width" in q or "height" in q:
            q["width"] = ["128"]
            q["height"] = ["128"]
            url = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path,
                 urllib.parse.urlencode({k: v[0] for k, v in q.items()}), ""))
        return url
    except Exception:
        return url


def logo_file(url):
    """Returns path to the cached file; downloads on a miss.
    None if the image couldn't be fetched."""
    if not url or not url.startswith("http"):
        return None
    url = _thumb_url(url)
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    os.makedirs(LOGO_DIR, exist_ok=True)
    for ext in _IMG_EXT.values():
        p = os.path.join(LOGO_DIR, key + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    stale = os.path.join(LOGO_DIR, key + ".bad")
    if os.path.exists(stale) and time.time() - os.path.getmtime(stale) < 600:
        return None                      # recently failed - don't hammer it
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "image/*", "Referer": "https://dexscreener.com/"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            blob = r.read(5_000_000)
        ext = _IMG_EXT.get(ct)
        if not ext or len(blob) < 100:
            raise ValueError("not an image: " + ct)
        p = os.path.join(LOGO_DIR, key + ext)
        with open(p, "wb") as f:
            f.write(blob)
        if os.path.exists(stale):
            os.remove(stale)
        return p
    except Exception as e:
        try:
            open(stale, "wb").close()
        except OSError:
            pass
        return None

def _wallet_addrs():
    """EVM wallets from config (lowercase)."""
    return [w.lower() for w in (CONFIG.get("wallets") or []) if w]


def _attach_token(addr, wallet):
    """Attach a token to a wallet's watchlist (or '*'). Call under _LOCK."""
    lst = STATE["wallet_tokens"].setdefault(wallet, [])
    if addr not in lst:
        lst.append(addr)


def _detach_token(addr, wallet):
    lst = STATE["wallet_tokens"].get(wallet) or []
    if addr in lst:
        lst.remove(addr)
    return lst


def _token_wallets(addr):
    """Which wallets watch the token (for the PnL cycle)."""
    return [w for w, lst in STATE["wallet_tokens"].items() if addr in lst and w != "*"]


def _register_token(key, auto=False):
    """New token in the master registry (dedup by history/polling). Under lock.
    auto=True — added by scan/rescan: subject to auto-pruning at zero;
    manual tokens carry no mark and are never pruned."""
    if key not in STATE["tokens"]:
        STATE["tokens"][key] = {"addr": key, "added_ts": int(time.time() * 1000),
                                "history": [], "det": {}, "symbol": "…"}
        if auto:
            STATE["tokens"][key]["auto"] = 1
        return True
    return False


# ------------------------------------------------------- wallet auto-scan

def _positions_on_chain(wallet, chain):
    """Wallet's positions on a chain (Blockscout token-balances), no stables/zeros.
    None — chain unavailable (don't confuse with an empty list)."""
    bs = pnl.BS_CHAINS.get(chain)
    if not bs:
        return None
    try:
        items, ok = portfolio._get_all(bs + f"/api/v2/addresses/{wallet}/token-balances")
    except Exception as e:
        _log(f"[scan] {chain} balances: {e}")
        return None
    if not ok:
        return None
    pos = []
    for it in items or []:
        tk = it.get("token") or {}
        addr = (tk.get("address_hash") or "").lower()
        sym = (tk.get("symbol") or "?").upper()
        try:
            dec = int(tk.get("decimals") or 18)
            qty = float(it.get("value") or 0) / (10 ** dec)
        except (TypeError, ValueError):
            continue
        if not addr or qty <= 0 or sym in portfolio.STABLE_SYMS:
            continue
        pos.append({"addr": addr, "sym": sym, "qty": qty, "chain": chain})
    return pos


def scan_wallet(wallet, chains=None):
    """Wallet positions across ALL working chains -> watchlist, with junk filters.

    token-balances per chain (Blockscout), meta/liquidity — DexScreener.
    Filter out: stables, positions worth less than $1, pools with liquidity < $1000
    (drops/scam pairs), tokens without a price, and foreign chains.
    Returns {added:[addr], skipped:[{sym,why}], byChain:{chain:count}}.
    """
    wallet = wallet.lower()
    chains = [c for c in (chains or CONFIG.get("chains") or ["robinhood"])
              if c in pnl.BS_CHAINS]
    try:
        pos = []
        dead = []
        for ch in chains:
            pp = _positions_on_chain(wallet, ch)
            if pp is None:
                dead.append(ch)
            else:
                pos.extend(pp)
        if not pos:
            return {"added": [], "skipped": [], "byChain": {},
                    **({"err": "explorer unavailable: " + ",".join(dead)} if dead else {})}
        # batches within THEIR OWN chain: fetch_batch takes the most liquid pool and
        # doesn't distinguish chains — never mix addresses from different chains in one request
        by_addr = {}
        bychain = {}
        for p in pos:
            bychain.setdefault(p["chain"], []).append(p)
        for ch, plist in bychain.items():
            for i in range(0, len(plist), 25):
                chunk = [p["addr"] for p in plist[i:i + 25]]
                try:
                    by_addr.update(fetch_batch(chunk))
                except Exception as e:
                    _log(f"[scan] fetch_batch {ch}: {e}")
                if i + 25 < len(plist):
                    time.sleep(1.0)
        added, skipped = [], []
        with _LOCK:
            for p in pos:
                meta = by_addr.get(p["addr"])
                if not meta:
                    skipped.append({"sym": p["sym"], "why": "no pool"})
                    continue
                if (meta.get("chain") or p["chain"]) != p["chain"]:
                    skipped.append({"sym": p["sym"], "why": "other chain"})
                    continue
                px = meta.get("price") or 0
                liq = meta.get("liquidity") or 0
                val = px * p["qty"]
                if val < MIN_AUTO_VALUE:
                    skipped.append({"sym": p["sym"], "why": f"dust ${val:.2f}"})
                    continue
                if liq < MIN_AUTO_LIQ:
                    skipped.append({"sym": p["sym"], "why": f"low liq ${liq:.0f}"})
                    continue
                if not _register_token(p["addr"], auto=True):
                    _attach_token(p["addr"], wallet)
                    added.append(p["addr"])
                    continue
                t = STATE["tokens"][p["addr"]]
                t.update({k: v for k, v in meta.items() if v not in (None, "")})
                t["chain"] = p["chain"]     # don't override with another chain's most liquid pool
                if not t.get("history"):
                    t["history"] = seed_history(p["addr"], meta, int(time.time() * 1000))
                _attach_token(p["addr"], wallet)
                added.append(p["addr"])
        _log(f"[scan] {wallet[:10]}…: добавлено {len(added)}, отфильтровано {len(skipped)}"
             + (f" по сетям {', '.join(f'{c}={len(v)}' for c, v in bychain.items())}" if len(bychain) > 1 else "")
             + (" — " + ", ".join(f"{s['sym']}({s['why']})" for s in skipped[:6]) if skipped else ""))
        save_all()
        return {"added": added, "skipped": skipped,
                "byChain": {c: len(v) for c, v in bychain.items()}}
    except Exception as e:
        _log(f"[scan] error: {e}")
        return {"err": str(e)[:120]}


# ------------------------------------------------- background detection of new purchases

_scanned_pos = {}   # wallet -> {chain: {addr: qty}} — snapshot from the previous pass
SCAN_META = {}      # wallet -> {running, ts, added?, skipped?, byChain?, err?}
_PF_REFRESH = set()   # wallets with an active on-demand slice recompute


def _scan_bg(wallet):
    """Background full wallet scan (on add or via button): doesn't hang
    the HTTP handler for minutes of network round-trips; progress and result — in SCAN_META."""
    SCAN_META[wallet] = {"running": True, "ts": int(time.time() * 1000)}
    try:
        res = scan_wallet(wallet)
        SCAN_META[wallet] = {**res, "running": False, "ts": int(time.time() * 1000)}
    except Exception as e:
        SCAN_META[wallet] = {"running": False, "err": str(e)[:120],
                             "ts": int(time.time() * 1000)}


def _auto_prune(wallet, seen):
    """Auto-tokens whose position died (sold or fell below $0.10) —
    out of the watchlist. Manual ones (no auto mark) are never touched: the user
    may keep a token watched after selling. seen = {chain: {addr: qty}}
    ONLY for chains alive in this pass: a chain that glitched in this pass
    is not counted as a sale."""
    removed = []
    with _LOCK:
        lst = STATE["wallet_tokens"].get(wallet) or []
        for a in list(lst):
            t = STATE["tokens"].get(a)
            if not t or not t.get("auto"):
                continue
            ch = t.get("chain")
            if ch not in seen:
                continue                      # chain dead in this pass — don't decide
            qty = seen[ch].get(a, 0.0)
            px = t.get("price")
            if qty > 0 and (px is None or qty * px >= MIN_KEEP_VALUE):
                continue                        # position alive (or price unknown) — leave it
            lst.remove(a)                       # sold to zero or fell below $0.10
            removed.append((a, t.get("symbol") or a[:8]))
            if not any(a in l for l in STATE["wallet_tokens"].values()):
                STATE["tokens"].pop(a, None)
                PNL.pop(a, None)
    if removed:
        _log(f"[prune] {wallet[:10]}…: убрано {len(removed)} авто-токенов с мёртвой "
             f"позицией: " + ", ".join(s for _, s in removed[:8]))
        save_all()
    return removed


def rescan_pass():
    """One pass: every wallet's positions across all chains.
    An address that appeared/grew vs the previous snapshot — fresh
    purchase: the token flies into THIS wallet's watchlist (same junk filters).
    Manual input is never undone: add ONLY what's new, deletion stays manual."""
    wallets = _wallet_addrs()
    chains = [c for c in (CONFIG.get("chains") or []) if c in pnl.BS_CHAINS]
    for w in wallets:
        seen = {}
        for ch in chains:
            pp = _positions_on_chain(w, ch)
            if pp is None:
                continue          # chain glitching — don't touch its snapshot
            seen[ch] = {p["addr"]: p["qty"] for p in pp}
        if not seen:
            continue              # all chains dead — don't overwrite the baseline
        old = _scanned_pos.get(w)
        _scanned_pos[w] = seen
        _auto_prune(w, seen)       # auto-tokens with dead positions — out (manual untouched)
        if old is None:
            continue              # first pass for this wallet: baseline, no alerts
        new = []                  # (addr, sym, chain, qty)
        for ch, m in seen.items():
            om = old.get(ch)
            if om is None:
                continue          # chain dropped out in the previous pass (glitch) — its tokens
                                  # are NOT purchases: silently accept as baseline
            for a, q in m.items():
                if q > (om.get(a) or 0) * 1.01:   # appeared or grew >1%
                    t = STATE["tokens"].get(a)
                    sym = (t or {}).get("symbol") or a[:8]
                    new.append((a, sym, ch, q))
        if not new:
            continue
        _log(f"[rescan] {w[:10]}…: новых/выросших позиций: {len(new)}")
        to_scan = {(a, ch, q) for a, _, ch, q in new
                   if a not in (STATE["wallet_tokens"].get(w) or [])}
        bychain = {}
        for a, ch, q in to_scan:
            bychain.setdefault(ch, []).append((a, q))
        for ch, pairs in bychain.items():
            addrs = [a for a, _ in pairs]
            qty_of = dict(pairs)
            for i in range(0, len(addrs), 25):     # batches ≤25: DexScreener 400 on long lists
                chunk = addrs[i:i + 25]
                try:
                    metas = fetch_batch(chunk)
                except Exception as e:
                    _log(f"[rescan] fetch {ch}: {e}")
                    time.sleep(1.0)
                    continue
                with _LOCK:
                    for a in chunk:
                        meta = metas.get(a)
                        liq = (meta or {}).get("liquidity") or 0
                        px = (meta or {}).get("price") or 0
                        if not meta or px <= 0 or liq < MIN_AUTO_LIQ:
                            continue          # fresh purchase without pool/liq — don't pull in
                        if px * qty_of.get(a, 0) < MIN_AUTO_VALUE:
                            continue          # dust <$1 — same filter as in scan_wallet
                        _register_token(a, auto=True)
                        t = STATE["tokens"][a]
                        t.update({k: v for k, v in meta.items() if v not in (None, "")})
                        t["chain"] = ch
                        if not t.get("history"):
                            t["history"] = seed_history(a, meta, int(time.time() * 1000))
                        _attach_token(a, w)
                        _log(f"[rescan] + {meta.get('symbol') or a[:8]} ({ch}) → {w[:10]}…")
                save_all()
                time.sleep(0.4)


def rescan_loop():
    while True:
        t0 = time.time()
        ivl = max(30, int(CONFIG.get("scanIntervalSec", 120) or 120))
        try:
            rescan_pass()
        except Exception as e:
            _log(f"[rescan] {str(e)[:120]}")
        time.sleep(max(10, ivl - (time.time() - t0)))


# ---------------------------------------------------------------- polling

def poll_once():
    with _LOCK:
        addrs = list(STATE["tokens"].keys())
    if not addrs:
        return
    now_ms = int(time.time() * 1000)
    infos = {}
    err = ""
    for i in range(0, len(addrs), 25):        # batches of ~25 addresses
        chunk = addrs[i:i + 25]
        try:
            infos.update(fetch_batch(chunk))
        except Exception as e:
            err = str(e)[:160]
            _log(f"[poll] fetch error: {err}")
        if i + 25 < len(addrs):
            time.sleep(1.2)
    STATE["last_poll"] = now_ms
    STATE["poll_error"] = err

    new_alerts = []
    for a in addrs:
        info = infos.get(a)
        t = STATE["tokens"][a]
        if not info:
            continue
        for k in ("symbol", "name", "logo", "chain", "addrChecksum", "price", "m5",
                  "h1", "h6", "h24", "liquidity", "mcap", "dex", "url"):
            if info.get(k) not in (None, ""):
                t[k] = info[k]
        price = t.get("price")
        if not price:
            continue
        hist = t.setdefault("history", [])
        if not hist:
            t["history"] = seed_history(a, info, now_ms)
            hist = t["history"]
        else:
            if hist and hist[-1][0] == now_ms:
                hist[-1][1] = price
            else:
                hist.append([now_ms, price])
            cutoff = now_ms - 86400000 - 3600000
            if len(hist) > 2 and hist[0][0] < cutoff:
                t["history"] = [p for p in hist if p[0] >= cutoff]
                hist = t["history"]
        fires = detector.evaluate(
            t, now_ms,
            detector.adaptive_windows(CONFIG["windows"], t.get("history") or [],
                                      CONFIG.get("adaptive") or {}),
            CONFIG["cooldownSeconds"])
        sig, _ = detector.estimate_vol(hist, int(CONFIG.get("adaptive", {})
                                                 .get("lookbackMin", 30)) * 60000)
        t["vol"] = round(sig * 100.0, 4) if sig else None
        for f in fires:
            f["addr"] = a
            f["id"] = f"{a}-{f['ts']}-{f['window']}"
            new_alerts.append((t, f))

    for t, f in new_alerts:
        payload = {
            "id": f["id"], "ts": f["ts"], "addr": f["addr"],
            "symbol": t.get("symbol"), "name": t.get("name"),
            "logo": t.get("logo"), "url": t.get("url"), "chain": t.get("chain"),
            "direction": f["direction"], "window": f["window"],
            "pct": f["pct"], "reason": f["reason"], "price": f["price"],
            "m5": t.get("m5"), "h1": t.get("h1"), "h24": t.get("h24"),
            "liquidity": t.get("liquidity"), "mcap": t.get("mcap"),
            "tg": "",
        }
        STATE["feed"] = ([payload] + STATE["feed"])[:120]
        _log(f"[ALERT] {payload['symbol']} {f['direction']} {f['pct']:+.1f}% "
             f"за {f['window']} ({f['reason']}) tg=off")
        if CONFIG.get("soundEnabled", True):
            sound.play(f["direction"], CONFIG.get("volume", 70))
        # telegram — outside the lock, in the background
        threading.Thread(target=_send_tg, args=(dict(payload),), daemon=True).start()


def _send_tg(item):
    t = STATE["tokens"].get(item["addr"]) or {}
    info = {"symbol": item.get("symbol"), "name": item.get("name"),
            "price": item.get("price"), "m5": item.get("m5"),
            "liquidity": item.get("liquidity"), "url": item.get("url"),
            "chain": item.get("chain"), "mcap": item.get("mcap") or t.get("mcap"),
            "image": item.get("logo")}
    alert = {"direction": item["direction"], "window": item["window"],
             "pct": item["pct"], "reason": item["reason"], "price": item["price"]}
    ok, detail = tgmod.send_alert(CONFIG, alert, info)
    item["tg"] = "ok" if ok else f"err: {detail}"
    _log(f"[tg] {item['symbol']} {item['direction']}: {item['tg']}")
    with _LOCK:   # feed is read in parallel by public_state/poller
        for row in STATE["feed"]:
            if row.get("id") == item["id"]:
                row["tg"] = item["tg"]


def pnl_loop():
    """Background PnL recompute: right after startup and every pnlIntervalSec."""
    global PNL
    while True:
        t0 = time.time()
        wallets = _wallet_addrs()
        try:
            if not wallets:
                if PNL:
                    PNL.clear()
                time.sleep(20)
                continue
            if STATE["tokens"]:
                PNL_META["running"] = True
                with _LOCK:
                    snap = {a: dict(t) for a, t in STATE["tokens"].items()}
                    scopes = {a: _token_wallets(a) for a in snap}
                res = {}
                for a, t in snap.items():
                    wl = scopes[a] or wallets   # '*'/'no binding' → all wallets
                    if not wl:
                        continue
                    r = pnl.compute_pnl_for_tokens({a: t}, wl)
                    if a in r:
                        res[a] = r[a]
                PNL.update(res)
                # tokens removed from the tracker — clean up
                for a in list(PNL.keys()):
                    if a not in STATE["tokens"]:
                        PNL.pop(a, None)
                PNL_META["ts"] = int(time.time() * 1000)
                PNL_META["error"] = ""
        except Exception as e:
            PNL_META["error"] = str(e)[:120]
            _log(f"[pnl] {PNL_META['error']}")
        finally:
            PNL_META["running"] = False
        ivl = max(30, int(CONFIG.get("pnlIntervalSec", 60) or 60))
        time.sleep(max(5, ivl - (time.time() - t0)) if wallets else 20)


PORTFOLIO_FRESH = None  # last snapshot from live data (for stale-if-error)


def _portfolio_cache_path():
    return os.path.join(DATA_DIR, "portfolio.json")


def load_portfolio_cache():
    global PORTFOLIO, PORTFOLIO_FRESH
    try:
        if os.path.exists(_portfolio_cache_path()):
            with open(_portfolio_cache_path(), encoding="utf-8") as f:
                d = json.load(f)
            snap = d.get("portfolio") or {}
            if snap.get("wallets"):
                for r in snap["wallets"]:
                    r["staleCache"] = True
                snap["staleCache"] = True
                PORTFOLIO = snap
                PORTFOLIO_FRESH = snap
                PORTFOLIO_META["ts"] = d.get("ts") or 0
                PORTFOLIO_META["stale"] = True
                _log("[portfolio] кэш снимка загружен (ждём живые данные)")
    except Exception as e:
        _log(f"[portfolio cache load] {e}")


def portfolio_loop():
    """Wallets' cash-flow PnL: right after startup and every portfolioIntervalSec."""
    global PORTFOLIO, PORTFOLIO_FRESH
    while True:
        t0 = time.time()
        wallets = [w for w in CONFIG.get("wallets") or [] if w]
        try:
            if not wallets:
                if PORTFOLIO:
                    PORTFOLIO = None
                PORTFOLIO_META["stale"] = True
                time.sleep(20)
                continue
            PORTFOLIO_META["running"] = True
            def publish(r_):
                # partial snapshot: each wallet is published immediately without
                # waiting for the slowest ones (the frontend sees a fresh slice in seconds on click)
                global PORTFOLIO
                with _LOCK:
                    base = PORTFOLIO or {"wallets": []}
                    was = [x for x in (base.get("wallets") or [])
                           if (x.get("wallet") or "").lower() != (r_.get("wallet") or "").lower()]
                    PORTFOLIO = dict(base, wallets=was + [r_])
            res = portfolio.compute_portfolio(
                wallets, chains=tuple(CONFIG.get("chains") or ("robinhood",)),
                prev=PORTFOLIO_FRESH or PORTFOLIO, on_wallet=publish)
            if not res.get("wallets"):
                PORTFOLIO_META["error"] = "нет валидных EVM-кошельков"
            else:
                PORTFOLIO = res
                PORTFOLIO_META["error"] = ""
                if not res.get("staleCache"):
                    PORTFOLIO_FRESH = res
                    PORTFOLIO_META["ts"] = int(time.time() * 1000)
                    PORTFOLIO_META["stale"] = False
                    _atomic_write(_portfolio_cache_path(),
                                  {"ts": PORTFOLIO_META["ts"],
                                   "portfolio": PORTFOLIO_FRESH})
        except Exception as e:
            PORTFOLIO_META["error"] = str(e)[:120]
            _log(f"[portfolio] {PORTFOLIO_META['error']}")
        finally:
            PORTFOLIO_META["running"] = False
        ivl = max(30, int(CONFIG.get("portfolioIntervalSec", 60) or 60))
        time.sleep(max(5, ivl - (time.time() - t0)) if wallets else 20)


def poller_loop():
    next_save = time.time() + 120
    while True:
        t0 = time.time()
        try:
            with _LOCK:
                poll_once()
        except Exception as e:
            _log(f"[poll] crash: {e}")
        if time.time() >= next_save:
            save_all()
            next_save = time.time() + 120
        time.sleep(max(3, CONFIG.get("pollSeconds", 15) - (time.time() - t0)))

# ---------------------------------------------------------------- HTTP API

def public_state():
    out = []
    now_ms = int(time.time() * 1000)
    acfg = CONFIG.get("adaptive") or {}
    pfw = {(r.get("wallet") or "").lower(): r
           for r in ((PORTFOLIO or {}).get("wallets") or [])}
    with _LOCK:   # poller/_add_tokens mutate STATE under this same lock
        wsnap = [{"wallet": w,
                  "label": "0x" + w[2:6] + "…" + w[-4:],
                  "tokens": len(STATE["wallet_tokens"].get(w) or []),
                  "value": (pfw.get(w) or {}).get("value"),
                  "pnl": (pfw.get(w) or {}).get("pnl")}
                 for w in _wallet_addrs()]
        for a, t in STATE["tokens"].items():
            row = {k: t.get(k) for k in ("symbol", "name", "logo", "chain", "addrChecksum",
                                         "price", "m5", "h1", "h24", "liquidity", "mcap",
                                         "dex", "url", "vol")}
            row["addr"] = a
            # "visibility scopes": which watchlists observe the token (w.lower() or '*')
            row["wallets"] = [w for w, lst in STATE["wallet_tokens"].items() if a in lst]
            row["pnl"] = PNL.get(a)
            row["addedTs"] = t.get("added_ts")
            row["spark"] = [[ts, p] for ts, p in (t.get("history") or [])][-240:]
            row["stale"] = (now_ms - (t["history"][-1][0])) > 180000 if t.get("history") else True
            eff = detector.adaptive_windows(CONFIG["windows"], t.get("history") or [], acfg)
            row["effWindows"] = [{"name": e["name"], "threshold": e["threshold"],
                                  "base": s["threshold"]}
                                 for e, s in zip(eff, CONFIG["windows"])]
            row["adaptiveActive"] = bool(acfg.get("enabled")) and any(
                e["threshold"] != s["threshold"] for e, s in zip(eff, CONFIG["windows"]))
            out.append(row)
        feed = list(STATE["feed"][:60])
        cfg = json.loads(json.dumps(CONFIG))
    out.sort(key=lambda r: r.get("mcap") or 0, reverse=True)
    tgc = cfg.get("telegram") or {}            # don't expose the bot token
    if tgc.get("botToken"):
        tgc["botToken"] = MASK_MARK
    return {
        "tokens": out, "feed": feed,
        "config": cfg, "lastPoll": STATE["last_poll"],
        "wallets": wsnap,
        "scan": {k: {kk: vv for kk, vv in v.items() if kk != "skipped"}
                 for k, v in SCAN_META.items()},
        "pollError": STATE["poll_error"], "pnlMeta": dict(PNL_META),
        "portfolio": PORTFOLIO, "portfolioMeta": dict(PORTFOLIO_META),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "dashboard.html"), encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html")
            except Exception as e:
                self._send(500, f"dashboard.html: {e}", "text/plain")
        elif path == "/api/state":
            self._send(200, public_state())
        elif path == "/logo":
            u = (urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("u") or [""])[0]
            p = logo_file(u)
            if p:
                ext = os.path.splitext(p)[1].lower()
                ctype = {".jpg": "image/jpeg", ".png": "image/png",
                         ".webp": "image/webp", ".gif": "image/gif",
                         ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
                with open(p, "rb") as f:
                    blob = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(blob)
            else:
                self._send(404, {"error": "no image"})
        elif path == "/api/ping":
            self._send(200, {"ok": True, "tokens": len(STATE["tokens"]),
                             "lastPoll": STATE["last_poll"],
                             "pollError": STATE["poll_error"]})
        elif path == "/api/wallets":
            wl = _wallet_addrs()
            with _LOCK:
                rows = [{"wallet": w,
                         "tokens": len(STATE["wallet_tokens"].get(w) or [])}
                        for w in wl]
                # short names from the first position — so the pills are readable
            pf = PORTFOLIO or {}
            pfw = {(r.get("wallet") or "").lower(): r
                   for r in (pf.get("wallets") or [])}
            for r in rows:
                w = pfw.get(r["wallet"]) or {}
                r["value"] = w.get("value")
                r["pnl"] = w.get("pnl")
                r["label"] = "0x" + r["wallet"][2:6] + "…" + r["wallet"][-4:]
            self._send(200, {"ok": True, "wallets": rows})
        elif path == "/api/portfolio":
            self._send(200, {"portfolio": PORTFOLIO,
                             "meta": dict(PORTFOLIO_META)})
        elif path == "/api/search":
            qs = urllib.parse.urlparse(self.path).query
            q = (urllib.parse.parse_qs(qs).get("q") or [""])[0]
            known = set(STATE["tokens"].keys())
            rows = search_tokens(q)
            for r in rows:
                k = r["addr"].lower() if r["addr"].startswith("0x") else r["addr"]
                r["inList"] = k in known
            self._send(200, {"ok": True, "results": rows})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._read_body()
        except Exception:
            body = {}
        if path == "/api/tokens/add":
            self._add_tokens(body)
        elif path == "/api/tokens/remove":
            a = (body.get("addr") or "").strip()
            addr = normalize_addr(a) or a
            wallet = (body.get("wallet") or "").strip().lower()
            with _LOCK:
                if wallet and wallet in STATE["wallet_tokens"]:
                    _detach_token(addr, wallet)
                    # token dropped from all lists — clean it from registry and history
                    if not any(addr in l for l in STATE["wallet_tokens"].values()):
                        STATE["tokens"].pop(addr, None)
                else:
                    STATE["tokens"].pop(addr, None)
                    for lst in STATE["wallet_tokens"].values():
                        if addr in lst:
                            lst.remove(addr)
            save_all()
            self._send(200, {"ok": True})
        elif path == "/api/wallets/add":
            self._wallets_add(body)
        elif path == "/api/wallets/remove":
            self._wallets_remove(body)
        elif path == "/api/wallets/scan":
            self._wallets_scan(body)
        elif path == "/api/wallets/refresh":
            self._wallets_refresh(body)
        elif path == "/api/config":
            self._update_config(body)
        elif path == "/api/test-sound":
            kind = "blip" if not body.get("direction") else body["direction"]
            sound.play(kind, CONFIG.get("volume", 70))
            self._send(200, {"ok": True, "msg": f"играю {kind}"})
        elif path == "/api/test-telegram":
            ok, detail = tgmod.send_test(CONFIG)
            self._send(200, {"ok": ok, "msg": detail})
        elif path == "/api/test-alert":
            self._test_alert()
        elif path == "/api/shutdown":
            self._shutdown()
        else:
            self._send(404, {"error": "not found"})

    def _shutdown(self):
        """Graceful shutdown: save data, respond to the client, then exit."""
        self._send(200, {"ok": True, "msg": "сохраняю и останавливаюсь"})
        try:
            save_all()
        except Exception:
            pass

        def bye():
            time.sleep(0.5)   # give the client time to read the response
            _log("[shutdown] корректная остановка по запросу")
            save_all()
            os._exit(0)
        threading.Thread(target=bye, daemon=True).start()

    def _add_tokens(self, body):
        raw = (body.get("addr") or "").strip()
        if not raw:
            self._send(200, {"ok": False, "msg": "пустой ввод"})
            return
        wallet = normalize_addr((body.get("wallet") or "").strip())
        added, skipped = [], []
        for a in raw.replace(";", ",").split(","):
            a = a.strip()
            if not a:
                continue
            key = normalize_addr(a)
            if not key:
                skipped.append({"why": "bad", "a": a[:12]})
                continue
            with _LOCK:
                _register_token(key)
                if wallet:
                    _attach_token(key, wallet)
                else:
                    _attach_token(key, "*")
                # attaching to an existing token also counts as "adding to the list"
                added.append(key) if key not in added else None
        # list-duplicates: token was already in this wallet's list — not counted as new
        status = {"ok": bool(added), "added": added, "skipped": skipped,
                  "msg": f"added {len(added)}" + (f", skipped {len(skipped)}" if skipped else "")}
        # immediately try to pull metadata so the card appears instantly
        if added:
            threading.Thread(target=self._warm_fetch, args=(added,), daemon=True).start()
        save_all()
        self._send(200, status)

    # ------------------------------------------------------- wallets

    def _wallets_add(self, body):
        """Add wallets as a list (newline-separated / commas).
        A new wallet is immediately scanned for positions (auto-watchlist)."""
        raw = body.get("wallets") or body.get("wallet") or ""
        if isinstance(raw, list):
            raw = ",".join(raw)
        new, existing = [], [w.lower() for w in (CONFIG.get("wallets") or [])]
        for a in str(raw).replace(";", ",").replace("\n", ",").split(","):
            a = a.strip()
            if not a:
                continue
            key = normalize_addr(a)
            if not key or not key.startswith("0x"):
                continue
            if key not in existing and key not in new:
                new.append(key)
        if not new:
            self._send(200, {"ok": False, "msg": "no valid EVM wallets", "wallets": existing})
            return
        with _LOCK:
            CONFIG["wallets"] = (existing + new)[:8]
            for w in new:
                STATE["wallet_tokens"].setdefault(w, [])
        save_all()
        for w in new:
            threading.Thread(target=_scan_bg, args=(w,), daemon=True).start()
        self._send(200, {"ok": True, "added": new, "wallets": CONFIG["wallets"],
                         "scan": "started"})   # progress — in state.meta.scan[w]

    def _wallets_remove(self, body):
        w = normalize_addr((body.get("wallet") or "").strip())
        if not w:
            self._send(200, {"ok": False, "msg": "bad wallet"})
            return
        with _LOCK:
            CONFIG["wallets"] = [x for x in (CONFIG.get("wallets") or [])
                                 if normalize_addr(x) != w]
            STATE["wallet_tokens"].pop(w, None)
            # tokens orphaned after wallet removal — out of the registry
            for a in list(STATE["tokens"].keys()):
                _detach_token(a, w)
                if not any(a in lst for lst in STATE["wallet_tokens"].values()):
                    STATE["tokens"].pop(a, None)
        save_all()
        self._send(200, {"ok": True, "wallets": CONFIG["wallets"]})

    def _wallets_refresh(self, body):
        """On-demand recompute of ONE wallet (click on a pill in the frontend when
        its slice is older than FRESH_SLICE_SEC). While running — duplicate requests
        are suppressed by a flag; the result merges into the common snapshot the same
        way as in the loop."""
        w = normalize_addr((body.get("wallet") or "").strip())
        if not w:
            self._send(200, {"ok": False, "msg": "bad wallet"})
            return
        with _LOCK:
            if w in _PF_REFRESH:
                self._send(200, {"ok": True, "state": "running"})
                return
            _PF_REFRESH.add(w)
            cur = next((x for x in ((PORTFOLIO or {}).get("wallets") or [])
                        if (x.get("wallet") or "").lower() == w), None)
        def bg():
            global PORTFOLIO_FRESH, PORTFOLIO
            try:
                r = portfolio.wallet_slice(
                    w, chains=tuple(CONFIG.get("chains") or ("robinhood",)),
                    prev=PORTFOLIO_FRESH or PORTFOLIO,
                    all_wallets=CONFIG.get("wallets") or [])
                with _LOCK:
                    base = PORTFOLIO or {"wallets": []}
                    was = [x for x in (base.get("wallets") or [])
                           if (x.get("wallet") or "").lower() != w]
                    PORTFOLIO = dict(base, wallets=was + [r])
                if not r.get("staleCache"):
                    with _LOCK:
                        pf = PORTFOLIO_FRESH or {"wallets": []}
                        was = [x for x in (pf.get("wallets") or [])
                               if (x.get("wallet") or "").lower() != w]
                        PORTFOLIO_FRESH = dict(pf, wallets=was + [r])
                _log(f"[pf1] {w[:10]}…: срез обновлён"
                     + (" (stale)" if r.get("staleCache") else ""))
            except Exception as e:
                _log(f"[pf1] {str(e)[:120]}")
            finally:
                with _LOCK:
                    _PF_REFRESH.discard(w)
        threading.Thread(target=bg, daemon=True).start()
        self._send(200, {"ok": True, "state": "started",
                         "hasCurrent": bool(cur)})

    def _wallets_scan(self, body):
        """Re-run auto-scan of the wallet's positions (all chains), in the background."""
        w = normalize_addr((body.get("wallet") or "").strip())
        if not w:
            self._send(200, {"ok": False, "msg": "bad wallet"})
            return
        if (SCAN_META.get(w) or {}).get("running"):
            self._send(200, {"ok": True, "wallet": w, "scan": "already running"})
            return
        threading.Thread(target=_scan_bg, args=(w,), daemon=True).start()
        self._send(200, {"ok": True, "wallet": w, "scan": "started"})

    def _warm_fetch(self, addrs):
        try:
            infos = fetch_batch(addrs)
            now_ms = int(time.time() * 1000)
            with _LOCK:
                for a, info in infos.items():
                    t = STATE["tokens"].get(a)
                    if t is None:
                        continue
                    t.update({k: v for k, v in info.items() if v not in (None, "")})
                    if not t.get("history") and t.get("price"):
                        t["history"] = seed_history(a, info, now_ms)
        except Exception as e:
            _log(f"[warm] {e}")

    def _update_config(self, body):
        with _LOCK:
            for k in ("pollSeconds", "cooldownSeconds", "volume"):
                if k in body:
                    CONFIG[k] = max(1 if k == "pollSeconds" else 0, int(body[k]))
            for k in ("pnlIntervalSec", "portfolioIntervalSec", "scanIntervalSec"):
                if k in body:
                    CONFIG[k] = min(3600, max(30, int(body[k])))
            if "chains" in body and isinstance(body["chains"], list):
                ok_ch = [c for c in body["chains"] if c in pnl.BS_CHAINS]
                if ok_ch:
                    CONFIG["chains"] = ok_ch
            for k in ("soundEnabled",):
                if k in body:
                    CONFIG[k] = bool(body[k])
            if "windows" in body and isinstance(body["windows"], list) and body["windows"]:
                CONFIG["windows"] = [
                    {"name": w.get("name", "5m"),
                     "seconds": max(15, int(w.get("seconds", 300))),
                     "threshold": max(1, float(w.get("threshold", 20)))}
                    for w in body["windows"]]
            if "wallets" in body and isinstance(body["wallets"], list):
                ws = []
                for w in body["wallets"]:
                    k = normalize_addr(str(w).strip())
                    if k and k.startswith("0x") and k not in ws:
                        ws.append(k)
                ws = ws[:8]
                old = set(CONFIG.get("wallets") or [])
                removed = [w for w in old if w.lower() not in {x.lower() for x in ws}]
                CONFIG["wallets"] = ws
                for w in ws:                       # new ones — empty scope until scanned
                    STATE["wallet_tokens"].setdefault(w.lower(), [])
                for w in removed:                  # removed — clean up their scopes and orphans
                    STATE["wallet_tokens"].pop(w.lower(), None)
                    for a in list(STATE["tokens"].keys()):
                        _detach_token(a, w.lower())
                        if not any(a in lst for lst in STATE["wallet_tokens"].values()):
                            STATE["tokens"].pop(a, None)
            if "telegram" in body:
                upd = {k: str(v).strip() for k, v in body["telegram"].items()
                       if k in ("botToken", "chatId")}
                # the dashboard gets the token masked; if the marker is
                # returned as-is — keep the real value
                if upd.get("botToken") == MASK_MARK:
                    upd.pop("botToken")
                CONFIG["telegram"].update(upd)
            if "adaptive" in body:
                a = body["adaptive"] or {}
                if "enabled" in a:
                    CONFIG["adaptive"]["enabled"] = bool(a["enabled"])
                if "k" in a:
                    CONFIG["adaptive"]["k"] = max(1.5, min(10.0, float(a["k"])))
                if "lookbackMin" in a:
                    CONFIG["adaptive"]["lookbackMin"] = max(10, min(240, int(a["lookbackMin"])))
        save_all()
        self._send(200, {"ok": True, "config": CONFIG})

    def _test_alert(self):
        with _LOCK:
            t = next(iter(STATE["tokens"].values()), None)
        if not t:
            self._send(200, {"ok": False, "msg": "сначала добавьте токен"})
            return
        alert = {"direction": "up", "window": "5m", "pct": 42.0, "reason": "spike",
                 "price": t.get("price") or 0, "ts": int(time.time() * 1000)}
        info = {"symbol": t.get("symbol"), "name": t.get("name"),
                "price": t.get("price"), "m5": t.get("m5"),
                "liquidity": t.get("liquidity"), "url": t.get("url"),
                "chain": t.get("chain"), "mcap": t.get("mcap"),
                "image": t.get("logo")}
        if CONFIG.get("soundEnabled", True):
            sound.play("up", CONFIG.get("volume", 70))
        ok, detail = tgmod.send_alert(CONFIG, alert, info)
        self._send(200, {"ok": ok, "msg": f"звук сыграл; telegram: {detail}"})


def main():
    load_all()
    load_portfolio_cache()
    # On Windows allow_reuse_address silently allows binding to an already
    # occupied port — two server copies effectively fight over the socket and requests
    # go to the old copy. Disable it: a second launch must fail with an explicit error.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"Не удалось занять порт {PORT}: {e}\n"
              f"Похоже, трекер уже запущен — открой http://127.0.0.1:{PORT}",
              flush=True)
        raise SystemExit(1)
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=pnl_loop, daemon=True).start()
    threading.Thread(target=portfolio_loop, daemon=True).start()
    threading.Thread(target=rescan_loop, daemon=True).start()
    _log(f"Token Tracker → http://127.0.0.1:{PORT}")
    _log(f"токенов в трекере: {len(STATE['tokens'])}, сети: "
         f"{', '.join(CONFIG.get('chains') or [])}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
