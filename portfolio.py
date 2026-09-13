# -*- coding: utf-8 -*-
"""
Portfolio PnL of a wallet — by IDENTITY:  PnL = value now − (deposited − withdrawn).

Why this is right (and why per-token PnL doesn't answer
"how much did I earn/lose in total"):
  * sold tokens already turned into cash — realized is not lost;
  * tokens bought outside the tracked list or «for free» (two-
    transaction Robinhood swap: stable OUT in tx1 + token IN in tx2)
    are also there — in the balance;
  * no need to guess each coin's entry price — the money is already in the wallet.

Stable-flow classification (per tx):
  IN  from EOA                          -> DEP (deposited into the ecosystem);
     but if a comparable OUT (±15%) happened within the previous 15 min —
     this is a withdrawal rollback (draft/failed retrake) -> the pair cancels out.
  OUT to a contract, tx has a token-IN  -> buy (inside the ecosystem — not a flow)
  IN  from a contract, tx has a token-OUT-> sell (inside)
  OUT to a contract, no token            -> candidate for WITHDRAWALS; if in the next
     30 min a token arrived in a separate tx with no payment — this is the first leg of a
     2-tx swap (buy), not a withdrawal.
"""

import calendar
import datetime
import re
import time
import urllib.parse

import pnl  # _get, _rpc_call, quote_usd, BS_CHAINS

STABLE_SYMS = {"USDG", "USDC", "USDT", "DAI", "FDUSD", "PYUSD", "EURC", "USDBC"}
# Canon stable contracts per chain (verified: name + holder counts, Blockscout).
# A symbol-only rule is poisonable: spam airdrops named "USDT"/"USDC" with
# identical qty across 3 non-canon addresses drew $19.4K fake "cash" (a real
# drop of 6469.20 × fake decimals). Position counts as cash only on a canon
# address; non-canon "stable" is junk — priced as a regular token (no pool).
STABLE_CANON = {
    "ethereum": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",   # USDC
                 "0xdac17f958d2ee523a2206206994597c13d831ec7"},  # USDT
    "arbitrum": {"0xaf88d065e77c8cc2239327c5edb3a432268e5831",   # USDC native
                 "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",   # USDT0 (LayerZero)
                 "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"},  # USDC.e bridged
    "base":     {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"},  # USDC
    "optimism": {"0x0b2c639c533813f4aa9d7837caf62653d097ff85",   # USDC
                 "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58"},  # USDT bridged
    "robinhood": {"0x5fc5360d0400a0fd4f2af552add042d716f1d168"},  # USDG (Paxos)
}
# DEX/relay routers: stable-OUT to them is a swap (the result may be native
# ETH, invisible in token-transfers), NEVER a withdrawal from the system
# DEX/relay routers: stable-OUT to them is a swap (the result may be native
# ETH, invisible in token-transfers), NEVER a withdrawal from the system.
# IMPORTANT: do NOT add bridge aggregators (Relay*, Across*, Stargate*) to this list:
# their OUT/IN is capital moving between chains — it goes through bridge netting.
ROUTER_RX = re.compile(r"router|poolmanager|uniswap|swap|aggregator|merkle|collector", re.I)
W30 = 30 * 60_000
W15 = 15 * 60_000
BRIDGE_WIN = 7 * 24 * 3600_000   # bridge reconciliation window: slow routes (L1→L2)
BRIDGE_TOL = 0.06            # amounts must match within 6% — a random
                             # coincidental transfer with such a window is unlikely


def net_bridges(per_chain):
    """Cross-chain bridge netting (the main source of phantoms in cash-flow PnL).

    Stable OUT to a contract with no counter leg (counted as a withdrawal from chain A) ↔
    free stable IN from a contract on ANOTHER chain B of the same wallet, same
    amount ±6% within ≤72h — that is one capital that moved across a bridge.
    Accounting: on A the outflow stays a withdrawal (money left chain A), while on B
    the inflow is recorded as a DEPOSIT at the outflow's cost (cost = usd_out, the delta
    = bridge fee — honestly lands in chain B's PnL). Total injected across all
    chains stays conservative: real external capital = EOA deposits,
    bridges only shuffle it between chains.
    per_chain: {chain: {"out":[{ts,usd}], "in":[{ts,usd}]}} — mutates used/cost
    marks. Returns (number of links, total amount moved)."""
    pairs, usd = 0, 0.0
    ins = [(ch, i) for ch, d in per_chain.items() for i in d["in"]]
    outs = sorted(((ch, o) for ch, d in per_chain.items() for o in d["out"]),
                  key=lambda x: x[1]["ts"])
    for ch_o, o in outs:
        best = None
        for ch_i, i in ins:
            if ch_i == ch_o or i.get("cost") is not None:
                continue
            dt = abs(i["ts"] - o["ts"])
            if dt > BRIDGE_WIN:
                continue
            mx = max(i["usd"], o["usd"], 1e-9)
            if abs(i["usd"] - o["usd"]) / mx > BRIDGE_TOL:
                continue
            if best is None or dt < best[2]:
                best = (ch_i, i, dt)
        if best:
            o["used"] = True
            best[1]["cost"] = o["usd"]     # deposit on the receiving chain
            pairs += 1
            usd += o["usd"]
    return pairs, usd


def _ms(s):
    if not s:
        return 0
    try:
        dt = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(dt.utctimetuple()) * 1000
    except Exception:
        return 0


def _get_all(url, max_pages=40):
    """Blockscout pagination with retries (the RH explorer sometimes returns 504).
    ok=False if not a single page answered with a valid body (empty {}
    with 200 is also garbage, as in pnl.cached_get_all)."""
    params, items = {}, []
    sep = "&" if "?" in url else "?"
    ok = False
    for _ in range(max_pages):
        q = urllib.parse.urlencode(params)
        d = None
        for a in range(5):
            d, err = pnl._get(url + ((sep + q) if q else ""), timeout=25, tries=2)
            if d:
                break
            time.sleep(1.5 + 1.5 * a)
        if not d:
            break
        if isinstance(d, dict) and "items" not in d and not params:
            break                        # garbage response (Blockscout under load)
        if isinstance(d, dict) and "items" not in d:
            break                        # mid-pagination without items — stop
        ok = True
        if isinstance(d, list):          # /token-balances returns a bare array
            items += d
            break
        items += d.get("items") or []
        params = d.get("next_page_params") or {}
        if not params:
            break
    return items, ok


# ------------------------------------------------- transfers -> flows/positions

def _is_stable(chain, addr, sym):
    """Stable only on a CANON contract: airdrops named USDT/USDC (same fake
    qty on several junk addresses) must not count as cash at $1 each."""
    return (sym in STABLE_SYMS
            and addr.lower() in STABLE_CANON.get(chain, set()))


def parse_flows(wallet, transfers, chain=None):
    """From token-transfers records: tx groups + the wallet's net token balances.

    Each group: {ts, s_in, s_in_eoa, s_out, s_out_to_eoa, tok_in[], tok_out[]}
    Plus net: {addr: {sym, qty}} — incoming sum minus outgoing.
    chain: enable canon-stable gating (non-canon USDT/USDC drops are junk).
    """
    wl = wallet.lower()
    txs, net = {}, {}
    for it in transfers:
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
            val /= (10 ** dec)
        except Exception:
            val = 0.0
        frm = it.get("from") or {}
        to = it.get("to") or {}
        f_h = (frm.get("hash") or "").lower()
        t_h = (to.get("hash") or "").lower()
        f_c = bool(frm.get("is_contract"))
        t_c = bool(to.get("is_contract"))
        e = txs.setdefault(tx, {"ts": _ms(it.get("timestamp")), "hash": tx,
                                "s_in": 0.0, "s_in_eoa": 0.0, "s_out": 0.0,
                                "s_out_to_eoa": 0.0, "s_out_router": 0.0,
                                "tok_in": [], "tok_out": []})
        e["ts"] = max(e["ts"], _ms(it.get("timestamp")))
        is_stable = _is_stable(chain, addr, sym) if chain else (sym in STABLE_SYMS)
        if is_stable:
            if t_h == wl:
                e["s_in"] += val
                if not f_c:
                    e["s_in_eoa"] += val
            if f_h == wl:
                e["s_out"] += val
                if not t_c:
                    e["s_out_to_eoa"] += val
                elif ROUTER_RX.search(to.get("name") or ""):
                    # swap via a DEX/relay router: the result may be native
                    # ETH (absent from token-transfers) — money stays in the chain
                    e["s_out_router"] += val
        elif val > 0:
            if t_h == wl:
                e["tok_in"].append(sym)
                n = net.setdefault(addr, {"sym": sym, "qty": 0.0})
                n["qty"] += val
            if f_h == wl:
                e["tok_out"].append(sym)
                n = net.setdefault(addr, {"sym": sym, "qty": 0.0})
                n["qty"] -= val
    flows = sorted(txs.values(), key=lambda e: e["ts"])
    positions = [{"addr": a, "sym": v["sym"], "qty": v["qty"]}
                 for a, v in net.items() if v["qty"] > 1e-9]
    return flows, positions


def aggregate(pflows):
    """Deposits/withdrawals after untangling 2-tx swaps and withdrawal rollbacks.
    out_cands — outflows to contracts counted as WITHDRAWALS: candidates for
    bridge reconciliation (cross-chain netting in portfolio_for_wallet)."""
    dep = wdr = churn_net = 0.0
    pending = []             # OUT to a contract without a counter token
    out_cands = []           # of those, the ones that became withdrawals — bridge candidates
    in_free = []             # stable IN from a contract with no payment/tokens — bridge arrivals
    for e in pflows:
        fc = e["s_in"] - e["s_in_eoa"]
        if fc > 0 and not e["tok_out"] and e["s_out"] <= 0:
            # free stable IN (candidate for «arrived via bridge»). IN in the same
            # tx where something was paid (stable/token OUT) is a swap, not a bridge
            in_free.append({"ts": e["ts"], "usd": fc})
        if e["s_in_eoa"] > 0:
            hit = None
            for p in pending:
                if p["used"] or not (0 < e["ts"] - p["ts"] <= W15):
                    continue
                if abs(p["usd"] - e["s_in_eoa"]) / max(p["usd"], 1e-9) < 0.15:
                    hit = p
                    break
            if hit:
                hit["used"] = True           # rollback: neither a withdrawal nor a deposit
                churn_net += e["s_in_eoa"] - hit["usd"]
            else:
                dep += e["s_in_eoa"]
        if e["s_out"] > 0 and not e["tok_in"]:
            if e["s_out_to_eoa"] >= e["s_out"] * 0.5:
                wdr += e["s_out"]            # transfer to an EOA — definitely a withdrawal
            else:
                # outflow to a contract without a counter token: Candidate — withdrawal
                # OR a swap into native ETH via a router (result visible only
                # in eth_getBalance). Keep in pending in case of a rollback-return;
                # in the final pass, router outflows are NOT counted as withdrawals.
                pending.append({"ts": e["ts"], "usd": e["s_out"], "used": False,
                                "router": e.get("s_out_router", 0.0) >= e["s_out"] * 0.5})
    # unmatched OUTs: would become withdrawals unless a free token arrives later
    for p in pending:
        if p["used"] or p.get("router"):
            continue
        paired = any(e["tok_in"] and not e["s_in"] and not e["s_out"]
                     and 0 < e["ts"] - p["ts"] <= W30 for e in pflows)
        if paired:
            p["used"] = True                 # leg of a 2-tx swap (buy)
        else:
            wdr += p["usd"]
            out_cands.append(p)              # «withdrawal» to a contract — possibly a bridge
    return {"deposits": dep, "withdrawals": wdr, "churn_net": churn_net,
            "out_cands": out_cands, "in_free": in_free}


def _merge_positions(positions, api_items):
    """Merge net positions from transfers with /token-balances (richer in the API)."""
    have = {p["addr"].lower(): p for p in positions}
    for it in api_items:
        tok = it.get("token") or {}
        addr = (tok.get("address_hash") or "").lower()
        try:
            dec = int(tok.get("decimals") or 18)
        except (TypeError, ValueError):
            dec = 18
        try:
            qty = float(it.get("value") or 0) / (10 ** dec)
        except (TypeError, ValueError):
            continue
        sym = (tok.get("symbol") or "?").upper()
        if qty <= 1e-9:
            continue
        if addr in have:
            have[addr]["qty"] = max(have[addr]["qty"], qty)  # API is more precise (no None-qty)
        else:
            have[addr] = {"addr": addr, "sym": sym, "qty": qty}
            positions.append(have[addr])


# ----------------------------------------------------------------- wallet

def portfolio_for_wallet(wallet, chains=("robinhood",), etpx=None, prev=None,
                         wallets_set=()):
    r = {"wallet": wallet, "deposits": 0.0, "withdrawals": 0.0,
         "churn_net": 0.0, "cash": 0.0, "native": 0.0, "native_dep": 0.0,
         "gas_eth": 0.0, "gas_usd": 0.0, "gas_txs": 0,
         "tokens": [], "unpriced": [], "partial": False, "degraded": False,
         "byChain": {}, "err": "", "noData": True, "staleCache": False}
    pos_value = 0.0
    bridge_cand = {}     # chain -> {"out": [...], "in": [...]} — bridge reconciliation candidates
    chain_acc = {}       # chain -> accumulators (byChain built after netting)
    chains_failed = []   # chains with no data this pass (glitchy Blockscout)
    for chain in chains:
        bs = pnl.BS_CHAINS.get(chain)
        if not bs:
            continue
        tr, ok = pnl.cached_get_all(bs + f"/api/v2/addresses/{wallet}/token-transfers")
        if not ok or not tr:
            # empty for a wallet with history = chain glitch (Blockscout sometimes sends
            # empty/garbage responses) — don't treat that as valid data
            chains_failed.append(chain)
            continue
        # per-chain accumulators for byChain (front-end filters by chain)
        ccash = cpv = cnat = cndep = 0.0
        cdep = cwdr = cgas = 0.0
        r["noData"] = False
        flows, positions = parse_flows(wallet, tr, chain=chain)
        a = aggregate(flows)
        for k in ("deposits", "withdrawals", "churn_net"):
            r[k] += a[k]
        cdep, cwdr = a.get("deposits", 0.0), a.get("withdrawals", 0.0)
        # candidates for bridge reconciliation on this chain (net_bridges after the loop)
        bc = bridge_cand.setdefault(chain, {"out": [], "in": []})
        bc["out"].extend({"ts": p["ts"], "usd": p["usd"]} for p in a.get("out_cands", []))
        bc["in"].extend({"ts": i["ts"], "usd": i["usd"]} for i in a.get("in_free", []))
        api, api_ok = _get_all(bs + f"/api/v2/addresses/{wallet}/token-balances")
        if not api_ok:
            # the balances endpoint died (empty {} / 5xx): cash and positions from
            # the snapshot would vanish «into nowhere» — frame is incomplete, serve prev stale
            r["degraded"] = True
        _merge_positions(positions, api if isinstance(api, list) else [])
        # quotes for positional tokens — ONE batched DexScreener call per 25 addresses:
        # previously each unknown drop triggered a personal request + search-
        # fallback (hundreds of seconds on a wallet with 200 positions — the portfolio
        # loop died and served «cache N minutes ago» for hours)
        pnl.quote_usd_batch(chain, [p["addr"] for p in positions
                                    if p["sym"] not in STABLE_SYMS])
        for p in positions:
            if p["sym"] in STABLE_SYMS:
                # ticker says stable: only the CANON contract counts as cash —
                # a non-canon "USDT"/"USDC" drop is spam, keep it invisible
                if _is_stable(chain, p["addr"], p["sym"]):
                    r["cash"] += p["qty"]
                    ccash += p["qty"]
                continue
            px, unk = pnl.quote_usd(chain, p["addr"], p["sym"], symbol_fallback=False)
            if unk or not px:
                if p["qty"] > 1:             # don't count dust
                    r["unpriced"].append({"chain": chain, "sym": p["sym"],
                                          "qty": round(p["qty"], 2)})
                    r["partial"] = True
                continue
            v = p["qty"] * px
            pos_value += v
            cpv += v
            r["tokens"].append({"chain": chain, "sym": p["sym"], "addr": p["addr"],
                                "qty": round(p["qty"], 4), "price": px,
                                "value": round(v, 4)})
        nb, _err = pnl._rpc_call(chain, "eth_getBalance", [wallet, "latest"])
        if not nb and tr:
            # chain native not fetched but TRANSFERS on it exist: balanceETH
            # would «vanish» from value — mark the frame incomplete (was a stale fallback).
            # If the wallet has no presence on the chain at all (0 transfers) — stay silent:
            # nothing to fetch there, no whiff of permanent degradation.
            r["degraded"] = True
        if nb and etpx:
            try:
                cnat = int(nb, 16) / 1e18 * etpx
                r["native"] += cnat
            except (TypeError, ValueError):
                pass
        # native deposits from EOA (gas from an exchange/another wallet)
        txs, _ = pnl.cached_get_all(
            bs + f"/api/v2/addresses/{wallet}/transactions?filter=to", max_pages=6)
        for it in txs:
            frm = it.get("from") or {}
            if not frm.get("is_contract"):
                # transfer between TWO tracked wallets — not an external
                # deposit: otherwise net capital double-counts and PnL sinks
                if (frm.get("hash") or "").lower() in wallets_set:
                    continue
                try:
                    v = int(it.get("value") or 0) / 1e18 * (etpx or 0)
                    r["native_dep"] += v
                    cndep += v
                except (TypeError, ValueError):
                    pass
        # ---- chain fees (gas): sum of fee over all OUTGOING tx ----
        # This is informational breakdown: gas is already in PnL by the identity
        # (ETH left -> wallet value dropped); here we just show
        # how much of the loss is fees. Failed txs burn gas too —
        # take all outgoing, their fee was debited from the balance.
        gout, gok = pnl.cached_get_all(
            bs + f"/api/v2/addresses/{wallet}/transactions?filter=from", max_pages=8)
        if gok:
            gas_wei = 0
            for it in gout:
                if (it.get("from") or {}).get("hash", "").lower() != wallet.lower():
                    continue
                try:
                    gas_wei += int((it.get("fee") or {}).get("value") or 0)
                except (TypeError, ValueError):
                    pass
            r["gas_eth"] = round(gas_wei / 1e18, 8)
            r["gas_txs"] = len(gout)
            cgas = round(gas_wei / 1e18 * (etpx or 0), 2)
            r["gas_usd"] = round(r["gas_usd"] + cgas, 2)
        chain_acc[chain] = {"ccash": ccash, "cpv": cpv, "cnat": cnat, "cndep": cndep,
                            "cdep": cdep, "cwdr": cwdr, "cgas": cgas,
                            "npos": len([1 for p in positions if p["sym"] not in STABLE_SYMS])}
    # ---- cross-chain bridge netting (conservative accounting) ----
    # On sender chain A the outflow stays a WITHDRAWAL (money left the chain).
    # On receiver chain B the inflow becomes a DEPOSIT at the outflow's value
    # (cost), the delta (usd_in − cost) = bridge fee → into chain B's PnL as a loss.
    # Σ injected over chains = EOA deposits: bridges only move capital, phantoms
    # (+$6K from negative injected) are gone by construction.
    b_pairs, b_usd = (net_bridges(bridge_cand) if len(bridge_cand) > 1 else (0, 0.0))
    if b_pairs:
        r["bridges"] = {"pairs": b_pairs, "usd": round(b_usd, 2)}
    arrived_by_chain = {ch: sum(i["cost"] for i in d["in"] if i.get("cost") is not None)
                        for ch, d in bridge_cand.items()}
    for chain, ca in chain_acc.items():
        value_c = ca["ccash"] + ca["cpv"] + ca["cnat"]
        inj_c = ca["cdep"] - ca["cwdr"] + ca["cndep"] + arrived_by_chain.get(chain, 0.0)
        r["byChain"][chain] = {
            "value": round(value_c, 2),
            "cash": round(ca["ccash"], 2), "native": round(ca["cnat"], 2),
            "injected": round(inj_c, 2),
            "pnl": round(value_c - inj_c, 2),
            "gas": round(ca["cgas"], 2),
            "tokens": ca["npos"],
        }
    if r["noData"]:
        r["err"] = "нет данных сети (Blockscout недоступен)"
        return r
    value = r["cash"] + pos_value + r["native"]
    # churn pair (withdrew 70, 69.58 came back): both legs outside dep/wdr; the
    # 0.42 fee already sits in value (money left and returned short) — it must
    # NOT be carried in injected (old code subtracted churn_net and penalized
    # twice). injected = real net capital from the outside world.
    # When the per-chain breakdown is fully assembled, the total = Σ chains: the identity
    # holds by construction, unmatched outflows (CEX, native sends that burned
    # gas) stay as honest "withdrawals" of their own chain.
    bychain_full = bool(chains) and not chains_failed and not r["degraded"]
    if bychain_full and chain_acc:
        injected = sum(ca["cdep"] - ca["cwdr"] + ca["cndep"]
                       + arrived_by_chain.get(ch, 0.0) for ch, ca in chain_acc.items())
    else:
        # bridge arrivals (arrived) must be here too: OUT on A was counted as a
        # withdrawal, IN on B is not an EOA deposit; without arrived, a dollar that
        # crossed via a bridge vanished from net capital (same phantom, just negative)
        injected = (r["deposits"] - r["withdrawals"] + r["native_dep"]
                    + sum(arrived_by_chain.values()))
    r["value"] = round(value, 2)
    r["injected"] = round(injected, 2)
    r["pnl"] = round(value - injected, 2)
    r["cash"] = round(r["cash"], 2)
    r["native"] = round(r["native"], 2)
    r["native_dep"] = round(r["native_dep"], 2)
    r["deposits"] = round(r["deposits"], 2)
    r["withdrawals"] = round(r["withdrawals"], 2)
    r["churn_net"] = round(r["churn_net"], 2)
    r["tokens"] = sorted(r["tokens"], key=lambda t: -t["value"])
    if not r.get("staleCache"):
        r["sliceTs"] = int(time.time() * 1000)   # freshness of this wallet's slice
    return r


def wallet_slice(wallet, chains=("robinhood",), prev=None, all_wallets=None):
    """Slice of a single wallet (on-demand front-end refresh on click).
    Same path as the loop, + stale-if-error against the prev snapshot.
    all_wallets — ALL tracked wallets (native_dep filter: transfers
    between one's own wallets are not deposits)."""
    w = (wallet or "").lower()
    etpx, unk = pnl.quote_usd("ethereum",
                              "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "WETH")
    if unk:
        etpx = None
    r = portfolio_for_wallet(w, chains, etpx,
                             wallets_set={x.lower() for x in (all_wallets or [w]) if x})
    if etpx is None:
        r["degraded"] = True
    if (r["noData"] or r["degraded"]) and prev:
        for p in prev.get("wallets") or []:
            if (not p.get("noData") and not p.get("err") and not p.get("degraded")
                    and (p.get("wallet") or "").lower() == w):
                r = dict(p)
                r["staleCache"] = True
                r["err"] = ""
                r["noData"] = False
                break
    return r


def compute_portfolio(wallets, chains=("robinhood",), prev=None, on_wallet=None):
    etpx, unk = pnl.quote_usd("ethereum",
                              "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "WETH")
    if unk:
        etpx = None
    # stale-if-error: when Blockscout is unavailable, serve the last good snapshot
    prev_map = {}
    if prev:
        for r in prev.get("wallets") or []:
            if not r.get("noData") and not r.get("err") and not r.get("degraded"):
                prev_map[(r.get("wallet") or "").lower()] = r
    out = []
    wset = {w.lower() for w in wallets if w}
    wl = [w for w in wallets if w and w.startswith("0x")]

    def one(w):
        r = portfolio_for_wallet(w, chains, etpx, wallets_set=wset)
        if etpx is None:
            r["degraded"] = True   # ETH not quoted — native/native_dep will zero out
        if (r["noData"] or r["degraded"]) and w.lower() in prev_map:
            # any incomplete frame (chain glitching) — keep the last honest one
            r = dict(prev_map[w.lower()])
            r["staleCache"] = True
            r["err"] = ""
            r["noData"] = False
        return r
    if len(wl) > 1:
        # wallets are independent (different Blockscout URLs, own accumulators) —
        # in parallel: cold start of 3 wallets took ~4 min sequentially.
        # on_wallet: callback for each finished slice (the server loop publishes
        # a partial snapshot without waiting for the slowest wallets).
        from concurrent.futures import ThreadPoolExecutor
        def done(r_):
            if on_wallet:
                try:
                    on_wallet(r_)
                except Exception:
                    pass
            return r_
        with ThreadPoolExecutor(max_workers=min(4, len(wl))) as ex:
            out = list(ex.map(lambda w: done(one(w)), wl))
    else:
        out = []
        for w in wl:
            r = one(w)
            if on_wallet:
                try:
                    on_wallet(r)
                except Exception:
                    pass
            out.append(r)
    tot = {"wallets": out, "ethPrice": etpx}
    if out:
        tot["pnl"] = round(sum(r["pnl"] or 0 for r in out if not r["err"]), 2)
        tot["value"] = round(sum(r.get("value") or 0 for r in out), 2)
        tot["injected"] = round(sum(r.get("injected") or 0 for r in out), 2)
        tot["cash"] = round(sum(r["cash"] for r in out), 2)
        tot["gas"] = round(sum(r.get("gas_usd") or 0 for r in out), 2)
        tot["partial"] = any(r["partial"] for r in out)
        tot["staleCache"] = any(r.get("staleCache") for r in out)
        tot["degraded"] = any(r.get("degraded") for r in out)
    return tot
