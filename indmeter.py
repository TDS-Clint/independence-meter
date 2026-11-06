#!/usr/bin/env python3
"""indmeter — the Independence Meter.

Reads a ballots CSV (N verifiers' 0/1 decisions over M items, plus a gold
column) and reports how independent the verifiers actually are.

The independence that governs effective votes is **error-correlation** — how
often verifiers make the *same mistakes* — not how often they agree. So that is
the primary metric here. Decision agreement (how often they make the same call,
right or wrong) is reported too, but only as a secondary diagnostic.

A precision guard refuses to certify independence when a verifier is too
imprecise to count: a statistically independent voter buys nothing if its flags
are mostly wrong.

Standalone tool. Pure Python standard library — no numpy, no network, no
model calls. Runs anywhere Python 3 runs.

Independence is measured **relative to the labeled set you provide**. A reading
on one set does not generalize to another (a checker can be independent on
natural text yet correlated on clean fixtures). Always name the set.

CSV format (header required):
    item_id, gold, v1, v2, ... vN      (with gold)
    item_id, v1, v2, ... vN            (no gold — error-correlation uncomputable)
      gold = correct decision: 1 = item SHOULD be flagged, 0 = should pass
      vK   = verifier K's decision: 1 = flagged, 0 = passed
    Positive class = decision == 1 (a flag raised).

    Error-correlation needs gold (an error is a disagreement with gold). Without
    gold the independence metric is uncomputable, precision cannot be checked,
    and the run is NOT certifiable (fail-closed, exit 2).

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
# phi_pair / phi_bar operate on any list of 0/1 vectors — decision vectors for
# the secondary diagnostic, error vectors for the primary independence metric.
# ---------------------------------------------------------------------------
def phi_pair(a, b):
    """Phi coefficient between two 0/1 vectors of equal length.

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


def phi_bar(vectors):
    """Mean pairwise phi over all N*(N-1)/2 pairs of 0/1 vectors.

    Returns (phi_bar, pairwise_values, degenerate_pairs).
    """
    n = len(vectors)
    vals = []
    degenerate = []
    for i in range(n):
        for j in range(i + 1, n):
            p, deg = phi_pair(vectors[i], vectors[j])
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


def error_vectors(ballots, gold):
    """Per verifier, e_i = 1 if its decision disagrees with gold on item i, else 0."""
    return [[1 if d != g else 0 for d, g in zip(vote, gold)] for vote in ballots]


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
    """Returns (names, gold, ballots). gold is a 0/1 list, or None when the CSV
    has no gold column. names is the verifier column names; ballots is a list of
    0/1 vectors parallel to names.

    Header must start with item_id. If the second column is 'gold' it is the
    gold column and verifiers follow; otherwise there is no gold and verifiers
    start at column 2."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            raise ValueError("empty CSV: no header row")
        header = [h.strip() for h in header]
        if header[0] != "item_id":
            raise ValueError(
                "header must start with item_id (got: %s)" % ", ".join(header)
            )
        has_gold = len(header) >= 2 and header[1] == "gold"
        first_v = 2 if has_gold else 1
        names = header[first_v:]
        if len(names) < 1:
            raise ValueError(
                "need at least one verifier column after item_id%s"
                % (" and gold" if has_gold else "")
            )
        gold = [] if has_gold else None
        cols = [[] for _ in names]
        for row in reader:
            if not row or all(c.strip() == "" for c in row):
                continue
            if len(row) != len(header):
                raise ValueError(
                    "row %r has %d fields, expected %d" % (row, len(row), len(header))
                )
            if has_gold:
                gold.append(_bit(row[1], "gold"))
            for k in range(len(names)):
                cols[k].append(_bit(row[first_v + k], names[k]))
    if (gold is not None and not gold) or (gold is None and not cols[0]):
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
def _degenerate_note(names, degenerate, label):
    pairs = ", ".join("%s~%s" % (names[i], names[j]) for i, j in degenerate)
    return "    note: %d %s pair(s) had a constant margin; phi defined as 0.0 for: %s" % (
        len(degenerate), label, pairs)


def run_report(path, min_precision):
    names, gold, ballots = load_ballots(path)
    n = len(names)
    m = len(ballots[0]) if ballots else 0
    if n < 2:
        print("need at least 2 verifier columns to measure independence (got %d)" % n)
        return 2

    print("Independence Meter")
    print("  file:       %s" % path)
    print("  verifiers:  %d  (%s)" % (n, ", ".join(names)))
    print("  items:      %d" % m)
    print("  (independence is measured relative to THIS labeled set; it does not generalize)")

    # Secondary diagnostic (always available): decision agreement.
    dpb, dvals, ddeg = phi_bar(ballots)

    # ---- No gold: error-correlation uncomputable -> not certifiable ----
    if gold is None:
        print()
        print("  -- secondary diagnostic: decision agreement (NOT independence) --")
        print("  decision_phi_bar: %+.4f  (how often verifiers make the same call, right or wrong)" % dpb)
        if ddeg:
            print(_degenerate_note(names, ddeg, "decision"))
        print()
        print("independence NOT certifiable (no gold)")
        print("  error-correlation (the independence metric) is an error vs gold;")
        print("  without a gold column it is uncomputable and precision cannot be")
        print("  checked. Decision agreement above is only a shape hint. Fail-closed.")
        return 2

    # ---- PRIMARY: error-correlation (shared mistakes) ----
    errs = error_vectors(ballots, gold)
    epb, evals, edeg = phi_bar(errs)
    eneff = kish_neff(n, epb)
    print()
    print("  == INDEPENDENCE METRIC: error-correlation (shared mistakes) ==")
    print("  error_phi_bar:                  %+.4f  (mean pairwise phi of error vectors;" % epb)
    print("                                          <0 = better than independent, 1 = same mistakes)")
    print("  effective independent verifiers: %.3f of %d  (Kish n_eff; error_phi_bar clamped >=0)"
          % (eneff, n))
    if edeg:
        print(_degenerate_note(names, edeg, "error"))

    # ---- SECONDARY: decision agreement ----
    print()
    print("  -- secondary diagnostic: decision agreement (NOT the independence metric) --")
    print("  decision_phi_bar:               %+.4f  (how often verifiers make the same call," % dpb)
    print("                                          right or wrong; high even for good judges)")
    if ddeg:
        print(_degenerate_note(names, ddeg, "decision"))

    # ---- Precision guard (gold-based; unchanged) ----
    print()
    print("  precision guard (threshold = %.2f):" % min_precision)
    failed = []
    for k, name in enumerate(names):
        prec, tp, fp = precision(ballots[k], gold)
        if prec is None:
            print("    %-8s precision=UNDEFINED (raised 0 flags)  -> FAIL" % name)
            failed.append((name, None))
        else:
            mark = "ok" if prec >= min_precision else "FAIL"
            print("    %-8s precision=%.3f  (TP=%d FP=%d)  -> %s" % (name, prec, tp, fp, mark))
            if prec < min_precision:
                failed.append((name, prec))

    # ---- Verdict ----
    print()
    if failed:
        parts = [name if prec is None else "%s (%.3f)" % (name, prec) for name, prec in failed]
        print("PRECISION FAIL — independence NOT certifiable")
        print("  below-threshold verifier(s): %s" % ", ".join(parts))
        print("  statistical independence without precision is worthless: an")
        print("  imprecise verifier's 'independent' errors just add noise.")
        return 2
    print("INDEPENDENCE CERTIFIABLE")
    print("  (per the error-correlation metric above, with every verifier clearing")
    print("   the precision floor, on this labeled set)")
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

    # ===== PRIMARY: error-correlation known-answer cases =====
    # Build decisions + gold so the ERROR vectors are exactly known.
    # gold fixed; a verifier's error vector = (decision != gold).
    gold = [1, 1, 1, 1, 0, 0, 0, 0]

    # identical error vectors -> error_phi 1.0, n_eff -> 1.0
    # two verifiers wrong on exactly the same items (items 0 and 4).
    vA = [0, 1, 1, 1, 1, 0, 0, 0]  # wrong on 0 and 4
    vB = [0, 1, 1, 1, 1, 0, 0, 0]  # same
    eA, eB = error_vectors([vA, vB], gold)
    p, deg = phi_pair(eA, eB)
    check("error-corr: identical error vectors -> phi == 1.0", abs(p - 1.0) < 1e-9 and not deg)
    check("error-corr: identical errors -> n_eff == 1.0", abs(kish_neff(2, p) - 1.0) < 1e-9)

    # independent errors -> phi ~ 0, n_eff -> N.
    # error vectors eA=[1,1,0,0], eB=[1,0,1,0] over 4 items (balanced 2x2).
    g4 = [0, 0, 0, 0]
    wA = [1, 1, 0, 0]  # errors on 0,1
    wB = [1, 0, 1, 0]  # errors on 0,2
    eA, eB = error_vectors([wA, wB], g4)
    p, deg = phi_pair(eA, eB)
    check("error-corr: independent error vectors -> phi == 0.0", abs(p - 0.0) < 1e-9 and not deg)
    check("error-corr: independent errors -> n_eff == N", abs(kish_neff(2, p) - 2.0) < 1e-9)

    # anti-correlated errors -> phi == -1.0 (one wrong exactly when the other is right)
    g4b = [0, 0, 0, 0]
    aA = [1, 1, 0, 0]  # errors on 0,1
    aB = [0, 0, 1, 1]  # errors on 2,3
    eA, eB = error_vectors([aA, aB], g4b)
    p, deg = phi_pair(eA, eB)
    check("error-corr: anti-correlated errors -> phi == -1.0", abs(p - (-1.0)) < 1e-9 and not deg)

    # ===== SECONDARY: decision-agreement known-answer cases (relabeled) =====
    a = [1, 0, 1, 0, 1, 1, 0, 0]
    p, _ = phi_pair(a, a)
    check("decision-agreement: identical decisions -> phi == 1.0", abs(p - 1.0) < 1e-9)
    b = [0, 1, 0, 1, 0, 0, 1, 1]
    p, _ = phi_pair(a, b)
    check("decision-agreement: opposite decisions -> phi == -1.0", abs(p - (-1.0)) < 1e-9)
    p, _ = phi_pair([1, 1, 0, 0], [1, 0, 1, 0])
    check("decision-agreement: independent decisions -> phi == 0.0", abs(p - 0.0) < 1e-9)

    # degenerate margin -> phi defined as 0.0, flagged degenerate
    p, deg = phi_pair([1, 1, 1, 1], [1, 0, 1, 0])
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
        description="Measure verifier independence (error-correlation) with a precision guard.",
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
