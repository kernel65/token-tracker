# -*- coding: utf-8 -*-
"""Synthetic fixtures for the flow classifier: 2-tx swaps, withdrawal rollbacks, real withdrawals."""
import portfolio as P

M = 60_000

def tx(ts, s_in=0, s_in_eoa=0, s_out=0, s_out_to_eoa=0, ti=None, to=None):
    return {"ts": ts, "s_in": s_in, "s_in_eoa": s_in_eoa, "s_out": s_out,
            "s_out_to_eoa": s_out_to_eoa, "tok_in": ti or [], "tok_out": to or []}

# 1) classic: deposit from EOA, buy (stable->contract + token IN), sell
f = [tx(0, s_in_eoa=100, s_in=100),
     tx(M, s_out=55, ti=["TOK"]),
     tx(2 * M, s_in=105, ti=[], to=["TOK"])]
a = P.aggregate(f)
assert a["deposits"] == 100 and a["withdrawals"] == 0, a

# 2) withdrawal to a contract without token-IN, and NO token later -> withdrawal
f = [tx(0, s_in_eoa=100, s_in=100), tx(M, s_out=20)]
a = P.aggregate(f)
assert a["withdrawals"] == 20, a

# 3) Robinhood 2-tx swap: OUT 20 to a contract, 2 min later token in with no payment
f = [tx(0, s_in_eoa=100, s_in=100), tx(M, s_out=20), tx(M + 2 * M, ti=["TOK"])]
a = P.aggregate(f)
assert a["withdrawals"] == 0 and a["deposits"] == 100, a

# 4) churn rollback: OUT 50 -> 5 min later IN 49.8 from EOA (not a DEP!)
f = [tx(0, s_in_eoa=100, s_in=100), tx(M, s_out=50), tx(M + 5 * M, s_in_eoa=49.8, s_in=49.8)]
a = P.aggregate(f)
assert a["withdrawals"] == 0 and a["deposits"] == 100 and abs(a["churn_net"] + 0.2) < 1e-9, a

# 5) withdrawal to an EOA (s_out_to_eoa) — always a withdrawal, even if a churn-IN follows
f = [tx(0, s_in_eoa=100, s_in=100), tx(M, s_out=16.7, s_out_to_eoa=16.7),
     tx(M + 3 * M, s_in_eoa=16.5, s_in=16.5)]   # paired rollback to an EOA withdrawal? withdrawal already counted
a = P.aggregate(f)
assert a["withdrawals"] == 16.7, a
# will IN 16.5 from EOA find a pending? no — EOA-OUT isn't put in pending, so DEP+16.5
assert a["deposits"] == 116.5, a

# 6) old OUT and DEP-IN far apart beyond the 15 min window — not churn
f = [tx(0, s_out=50), tx(60 * M, s_in_eoa=50, s_in=50)]
a = P.aggregate(f)
assert a["withdrawals"] == 50 and a["deposits"] == 50, a

# 7) two withdrawals in a row, one rolled back — the second must not be eaten
f = [tx(0, s_in_eoa=200, s_in=200), tx(M, s_out=20), tx(2 * M, s_out=50),
     tx(2 * M + 6 * M, s_in_eoa=50.0, s_in=50.0)]
a = P.aggregate(f)
assert abs(a["withdrawals"] - 20) < 1e-9 and a["deposits"] == 200, a

print("test_portfolio: OK (7 сценариев)")
