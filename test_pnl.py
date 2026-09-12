# -*- coding: utf-8 -*-
"""Unit tests for the PnL engine (swap legs + FIFO)."""
import pnl

# --- extract_trades on synthetic legs (no network) ---
W = "0x" + "11" * 20
TK = "0x" + "22" * 20
Q = "0x" + "33" * 20     # USDC-like leg
OTHER = "0x" + "44" * 20


def fake_legs(mapping):
    def f(chain, tx):
        return mapping.get(tx)
    return f


def run_extract(txmap, events):
    orig = pnl.bs_tx_legs
    orig_q = pnl.quote_usd
    pnl.bs_tx_legs = fake_legs(txmap)
    pnl.quote_usd = lambda c, t, s: (1.0 if t == Q.lower() else 0.02, t not in (Q.lower(),))
    try:
        return pnl.extract_trades("robinhood", W, TK, events, {W.lower()})
    finally:
        pnl.bs_tx_legs = orig
        pnl.quote_usd = orig_q


# buy: wallet received the token, sent 12 USDC
ev = [{"ts": 1000, "qty": 100000, "tx": "tx1", "in": True, "out": False}]
txmap = {"tx1": [
    {"tok": TK.lower(), "sym": "LIB", "from": "pool", "to": W.lower(), "qty": 100000},
    {"tok": Q.lower(), "sym": "USDC", "from": W.lower(), "to": "pool", "qty": 12.0},
]}
tr = run_extract(txmap, ev)
assert len(tr) == 1 and tr[0]["dir"] == "buy" and abs(tr[0]["usd"] - 12.0) < 1e-9, tr
assert not tr[0]["unknown"]

# sell: wallet gave up the token, received 20 USDC
ev = [{"ts": 2000, "qty": 50000, "tx": "tx2", "in": False, "out": True}]
txmap = {"tx2": [
    {"tok": TK.lower(), "sym": "LIB", "from": W.lower(), "to": "pool", "qty": 50000},
    {"tok": Q.lower(), "sym": "USDC", "from": "pool", "to": W.lower(), "qty": 20.0},
]}
tr = run_extract(txmap, ev)
assert tr[0]["dir"] == "sell" and abs(tr[0]["usd"] - 20.0) < 1e-9

# clean incoming transfer (no payment) → usd=None unknown
ev = [{"ts": 3000, "qty": 700, "tx": "tx3", "in": True, "out": False}]
txmap = {"tx3": [{"tok": TK.lower(), "sym": "LIB", "from": "friend", "to": W.lower(), "qty": 700}]}
tr = run_extract(txmap, ev)
assert tr[0]["usd"] is None and tr[0]["unknown"], tr

# self-transfer in==out — not a trade
ev = [{"ts": 4000, "qty": 10, "tx": "tx4", "in": True, "out": True}]
tr = run_extract({"tx4": []}, ev)
assert tr == [], tr

# --- compute_position FIFO over real usd legs ---
# bought for $12, sold for $20 → realized = 20-12=8 (position fully closed)
pos = pnl.compute_position(
    [{"ts": 1, "qty": 100, "dir": "buy", "usd": 12.0, "unknown": False},
     {"ts": 2, "qty": 100, "dir": "sell", "usd": 20.0, "unknown": False}],
    cur_price=0.5)
assert abs(pos["realized"] - 8.0) < 1e-9, pos
assert pos["qty"] == 0 and abs(pos["unrealized"]) < 1e-9
assert abs(pos["pnl"] - 8.0) < 1e-9 and abs(pos["pnlPct"] - 8.0 / 12.0 * 100) < 1e-6
assert not pos["noPriceData"]

# partial sell: bought 100k for $12 (avg 0.00012), sold 50k for $10
pos = pnl.compute_position(
    [{"ts": 1, "qty": 100_000, "dir": "buy", "usd": 12.0, "unknown": False},
     {"ts": 2, "qty": 50_000, "dir": "sell", "usd": 10.0, "unknown": False}],
    cur_price=0.00016)
assert abs(pos["realized"] - (10.0 - 6.0)) < 1e-9, pos          # 50k*0.00012 = 6
assert abs(pos["unrealized"] - (50_000 * 0.00016 - 6.0)) < 1e-9  # 8-6=2
assert abs(pos["pnl"] - 6.0) < 1e-9

# unknown lot: airdrop buy with no payment → realized stays clean, partial
pos = pnl.compute_position(
    [{"ts": 1, "qty": 1000, "dir": "buy", "usd": None, "unknown": True},
     {"ts": 2, "qty": 500, "dir": "sell", "usd": 50.0, "unknown": False}],
    cur_price=0.1)
assert pos["realized"] == 0 and pos["noPriceData"], pos
assert abs(pos["qty"] - 500) < 1e-9
# unreal is computed only over known lots: the lot was unknown → known empty → unreal=0
assert abs(pos["unrealized"]) < 1e-9, pos

# absurd PnL (>20× turnover) → unreliable
pos = pnl.compute_position(
    [{"ts": 1, "qty": 100, "dir": "buy", "usd": 1.0, "unknown": False},
     {"ts": 2, "qty": 100, "dir": "sell", "usd": 1e9, "unknown": False}],
    cur_price=0.01)
assert pos["unreliable"] and pos["pnl"] is None, pos

# --- 2-tx linking (Robinhood: payment and token in different txs) ---
def _tx(ts_ms, frm_h, frm_c, to_h, to_c, sym, addr, val):
    # tx ts in Flow are seconds (no ms//1000 inside _parse_ts:
    # _flow_txs expects ISO; build ISO from ms)
    import datetime, calendar
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000000Z"
    dec = 6 if sym in ("USDG", "USDC") else 18
    return {"transaction_hash": f"0xh{ts_ms}", "timestamp": iso,
            "from": {"hash": frm_h, "is_contract": frm_c},
            "to": {"hash": to_h, "is_contract": to_c},
            "token": {"symbol": sym, "address_hash": addr, "decimals": dec},
            "total": {"value": str(int(val * (10 ** dec))), "decimals": dec}}

WAL = "0x" + "1" * 40
POOL = "0x" + "9" * 40
EOA = "0x" + "7" * 40
USDG = "0x" + "5" * 40
CA = "0x" + "2" * 40
M = 60_000
t0 = 1_789_000_000_000

# a) payment $20 (tx1) → token-IN with no payment (tx2, +120 s) = buy-link
items = [_tx(t0, WAL, False, POOL, True, "USDG", USDG, 20),
         _tx(t0 + 2 * M, POOL, True, WAL, False, "CA", CA, 14554)]
lk = pnl._links_from_txs(pnl._flow_txs(items, WAL))
assert lk[CA][items[1]["transaction_hash"]] == (20.0, "buy"), lk

# b) withdrawal rolled back by a return from EOA (±15%, ≤15 min) — payment is NOT linked
items = [_tx(t0, WAL, False, POOL, True, "USDG", USDG, 50),
         _tx(t0 + 6 * M, EOA, False, WAL, False, "USDG", USDG, 49.8),
         _tx(t0 + 2 * M, POOL, True, WAL, False, "CA", CA, 100)]  # airdrop
lk = pnl._links_from_txs(pnl._flow_txs(items, WAL))
assert lk.get(CA) in (None, {}), lk

# c) symmetric sell: token-OUT with no payment → orphan stable-IN = sell-link
items = [_tx(t0, WAL, False, POOL, True, "CA", CA, 1000),
         _tx(t0 + M, POOL, True, WAL, False, "USDG", USDG, 25)]
lk = pnl._links_from_txs(pnl._flow_txs(items, WAL))
assert lk[CA][items[0]["transaction_hash"]] == (25.0, "sell"), lk

# d) extract_trades: substitute linked price into an unknown lot with partial
ev = [{"ts": t0 + 2 * M, "qty": 14554, "tx": items[1]["transaction_hash"] if False else "txT",
       "in": True, "out": False}]
orig_legs, orig_q = pnl.bs_tx_legs, pnl.quote_usd
pnl.bs_tx_legs = lambda c, tx: []          # no legs — clean incoming transfer
try:
    tr = pnl.extract_trades("robinhood", WAL, CA, ev, {WAL.lower()},
                            linked={"txT": (20.0, "buy")})
finally:
    pnl.bs_tx_legs, pnl.quote_usd = orig_legs, orig_q
assert tr[0]["usd"] == 20.0 and not tr[0]["unknown"] and tr[0]["partial"], tr

# e) FIFO with a linked lot: CAYENNE pattern — buy $20, position → unreal −19.5
pos = pnl.compute_position(
    [{"ts": 1, "qty": 14554, "dir": "buy", "usd": 20.0, "unknown": False,
      "partial": True}], cur_price=0.0000344)
assert abs(pos["unrealized"] - (14554 * 0.0000344 - 20)) < 1e-6, pos
assert abs(pos["invested"] - 20) < 1e-9 and pos["noPriceData"]

print("PnL unit tests (swap-legs + 2tx links): PASS")
