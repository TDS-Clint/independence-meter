#!/usr/bin/env python3
"""indmeter — the Independence Meter.

Reads a ballots CSV (N verifiers' 0/1 decisions over M items, plus a gold
column) and reports how independent the verifiers actually are, with a
precision guard that refuses to certify independence when a verifier is too
imprecise to count.

Standalone tool. Pure Python standard library — no numpy, no network, no
model calls. Runs anywhere Python 3 runs.

CSV format (header required):
    item_id, gold, v1, v2, ... vN
      gold = correct decision: 1 = item SHOULD be flagged, 0 = should pass
      vK   = verifier K's decision: 1 = flagged, 0 = passed
    Positive class = decision == 1 (a flag raised).

Usage:
    python indmeter.py sample_ballots.csv [--min-precision 0.5]
    python indmeter.py --selftest
"""
import argparse
import csv
import math
import sys


# ---------------------------------------------------------------------------
# phi coefficient (standard 2x2) and Kish effective-sample-size.
# The n_eff design-effect form is vendored from the faithfulness-independence
# repo's neff.py (kish_neff); copied here so this repo has no cross-repo import.
# ---------------------------------------------------------------------------
def phi_pair(a, b):
    """Phi coefficient between two 0/1 decision vectors of equal length.

    Returns (phi, degenerate) where degenerate is True when a margin is all-ones
    or all-zeros (denominator 0); phi is defined as 0.0 in that case.
    """
    n11 = n10 = n01 = n00 = 0
    for x, y in zip(a, b):
        if x and y:
            n11 += 1
        elif x and not y:
            n10 += 1
        elif not x and y:
            n01 += 1
        else:
            n00 += 1
    denom_sq = (n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)
    if denom_sq == 0:
        return 0.0, True
    phi = (n11 * n00 - n10 * n01) / math.sqrt(denom_sq)
    return phi, False


def phi_bar(ballots):
    """Mean pairwise phi over all N*(N-1)/2 verifier pairs.

    ballots: list of N decision vectors (each a list of 0/1).
    Returns (phi_bar, pairs, degenerate_pairs).
    """
    n = len(ballots)
    vals = []
    degenerate = []
    for i in range(n):
        for j in range(i + 1, n):
            p, deg = phi_pair(ballots[i], ballots[j])
            vals.append(p)
            if deg:
                degenerate.append((i, j))
    pb = sum(vals) / len(vals) if vals else 0.0
    return pb, vals, degenerate


def kish_neff(n, pb):
    """Kish design-effect effective number of independent verifiers.

    n_eff = N / (1 + (N-1) * phi_bar_clamped), phi_bar_clamped = max(phi_bar, 0).
    (Vendored from faithfulness-independence/neff.py kish_neff.)
    """
    pb_clamped = max(pb, 0.0)
    return n / (1.0 + (n - 1) * pb_clamped)


# ---------------------------------------------------------------------------
# Precision guard (the D74 rule): independence is worthless if a verifier is
# too imprecise to count. Per verifier, precision = TP / (TP + FP) vs gold.
# ---------------------------------------------------------------------------
def precision(vote, gold):
    """Returns (precision_or_None, tp, fp). None when the verifier raised no
    flags (TP+FP == 0): precision undefined -> treated as FAIL by the caller."""
    tp = sum(1 for v, g in zip(vote, gold) if v == 1 and g == 1)
    fp = sum(1 for v, g in zip(vote, gold) if v == 1 and g == 0)
    if tp + fp == 0:
        return None, tp, fp
    return tp / (tp + fp), tp, fp


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------
def load_ballots(path):
    """Returns (names, gold, ballots) where names is the verifier column names,
    gold is a 0/1 list, ballots is a list of 0/1 vectors parallel to names."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            raise ValueError("empty CSV: no header row")
        header = [h.strip() for h in header]
        if len(header) < 3 or header[0] != "item_id" or header[1] != "gold":
            raise ValueError(
                "header must be: item_id, gold, v1, v2, ... vN "
                "(got: %s)" % ", ".join(header)
            )
        names = header[2:]
        gold = []
        cols = [[] for _ in names]
        for row in reader:
            if not row or all(c.strip() == "" for c in row):
                continue
            if len(row) != len(header):
                raise ValueError(
                    "row %r has %d fields, expected %d" % (row, len(row), len(header))
                )
            gold.append(_bit(row[1], "gold"))
            for k in range(len(names)):
                cols[k].append(_bit(row[2 + k], names[k]))
    if not gold:
        raise ValueError("no data rows")
    return names, gold, cols


def _bit(s, col):
    s = s.strip()
    if s not in ("0", "1"):
        raise ValueError("column %s must be 0 or 1, got %r" % (col, s))
    return int(s)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def run_report(path, min_precision):
    names, gold, ballots = load_ballots(path)
    n = len(names)
    m = len(gold)
    if n < 2:
        print("need at least 2 verifier columns to measure independence (got %d)" % n)
        return 2

    pb, vals, degenerate = phi_bar(ballots)
    neff = kish_neff(n, pb)

    print("Independence Meter")
    print("  file:       %s" % path)
    print("  verifiers:  %d  (%s)" % (n, ", ".join(names)))
    print("  items:      %d" % m)
    print("  phi_bar:    %+.4f  (mean pairwise phi; negative = better than independent)" % pb)
    print("  n_eff:      %.3f of %d  (Kish; phi_bar clamped to >=0 for n_eff)" % (neff, n))
    if degenerate:
        pairs = ", ".join("%s~%s" % (names[i], names[j]) for i, j in degenerate)
        print("  note:       %d pair(s) had a constant margin; phi defined as 0.0 for: %s"
              % (len(degenerate), pairs))

    # Precision guard
    print("  precision guard (threshold = %.2f):" % min_precision)
    failed = []
    for k, name in enumerate(names):
        prec, tp, fp = precision(ballots[k], gold)
        if prec is None:
            print("    %-6s precision=UNDEFINED (raised 0 flags)  -> FAIL" % name)
            failed.append((name, None))
        else:
            mark = "ok" if prec >= min_precision else "FAIL"
            print("    %-6s precision=%.3f  (TP=%d FP=%d)  -> %s" % (name, prec, tp, fp, mark))
            if prec < min_precision:
                failed.append((name, prec))

    print()
    if failed:
        parts = []
        for name, prec in failed:
            parts.append(name if prec is None else "%s (%.3f)" % (name, prec))
        print("PRECISION FAIL — independence NOT certifiable")
        print("  below-threshold verifier(s): %s" % ", ".join(parts))
        print("  statistical independence without precision is worthless: an")
        print("  imprecise verifier's 'independent' errors just add noise.")
        return 2
    print("INDEPENDENCE CERTIFIABLE")
    return 0


# ---------------------------------------------------------------------------
# Self-check: known-answer synthetic cases only. No external numbers.
# ---------------------------------------------------------------------------
def selftest():
    ok = True

    def check(label, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            ok = False

    # two identical verifiers -> phi = 1.0, n_eff -> 1.0
    a = [1, 0, 1, 0, 1, 1, 0, 0]
    p, deg = phi_pair(a, a)
    check("identical verifiers: phi == 1.0", abs(p - 1.0) < 1e-9 and not deg)
    check("identical verifiers: n_eff == 1.0", abs(kish_neff(2, p) - 1.0) < 1e-9)

    # two perfectly anti-correlated verifiers -> phi = -1.0
    b = [0, 1, 0, 1, 0, 0, 1, 1]
    p, deg = phi_pair(a, b)
    check("anti-correlated verifiers: phi == -1.0", abs(p - (-1.0)) < 1e-9 and not deg)
    # negative phi_bar clamps -> n_eff == N (no penalty, no bonus)
    check("anti-correlated: n_eff == N (clamped)", abs(kish_neff(2, p) - 2.0) < 1e-9)

    # two statistically independent verifiers -> phi ~ 0, n_eff ~ N.
    # Construct an exact zero: a balanced 2x2 with n11=n10=n01=n00.
    ia = [1, 1, 0, 0]
    ib = [1, 0, 1, 0]
    p, deg = phi_pair(ia, ib)
    check("independent verifiers: phi == 0.0", abs(p - 0.0) < 1e-9 and not deg)
    check("independent verifiers: n_eff == N", abs(kish_neff(2, p) - 2.0) < 1e-9)

    # degenerate margin -> phi defined as 0.0, flagged degenerate
    const = [1, 1, 1, 1]
    varied = [1, 0, 1, 0]
    p, deg = phi_pair(const, varied)
    check("constant margin: phi == 0.0 and flagged degenerate", p == 0.0 and deg)

    # precision guard math
    prec, tp, fp = precision([1, 1, 0, 0], [1, 0, 1, 0])  # TP=1, FP=1
    check("precision: TP=1 FP=1 -> 0.5", prec == 0.5 and tp == 1 and fp == 1)
    prec, tp, fp = precision([0, 0, 0, 0], [1, 0, 1, 0])  # no flags
    check("precision: no flags -> UNDEFINED", prec is None)

    print()
    print("SELFTEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="indmeter",
        description="Measure verifier independence with a precision guard.",
    )
    ap.add_argument("csv", nargs="?", help="ballots CSV (item_id, gold, v1..vN)")
    ap.add_argument("--min-precision", type=float, default=0.5,
                    help="minimum per-verifier precision to certify (default 0.5)")
    ap.add_argument("--selftest", action="store_true",
                    help="run known-answer math checks and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.csv:
        ap.error("a ballots CSV is required (or use --selftest)")
    try:
        return run_report(args.csv, args.min_precision)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
