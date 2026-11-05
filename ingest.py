#!/usr/bin/env python3
"""ingest — turn real gate decision output into an indmeter ballots CSV.

Format conversion only. No model calls, no network, no scoring — this reads
verifiers' decision files and writes the `item_id, [gold,] v1..vN` CSV that
indmeter.py consumes.

REAL INPUT SCHEMA (observed, not invented) — a gate decision file is either:
  * a list of records, or
  * an object {"decisions": [ ...records... ], ...}
  each record: {"action_id": "<id>", "decision": "BLOCKED"|"ALLOW", ...}
A gold / expected-decision file has the same shape (its decisions are the
correct answers). IDs join verifiers to each other and to gold.

DECISION MAPPING (positive class = a flag/block raised):
  BLOCKED / BLOCK  -> 1      ALLOW / ALLOWED / PASS -> 0
Case-insensitive. Any other token is an error — nothing is guessed.

GOLD DISCIPLINE (keeps the precision guard honest):
  * gold covers every joined item   -> emit the gold column.
  * gold missing or only partial     -> emit WITHOUT gold and warn; indmeter
                                        then fails closed ("NO GOLD — not
                                        certifiable"). Gold is never synthesized.
  * --gold-only                      -> restrict output to the gold-covered
                                        subset and emit gold for it.

Usage:
  python ingest.py --out ballots.csv v_a=gate_a_decisions.json v_b=gate_b_decisions.json
  python ingest.py --out ballots.csv --gold expected_decisions.json \\
        v_a=gate_a_decisions.json v_b=gate_b_decisions.json
"""
import argparse
import csv
import json
import sys


_POS = {"BLOCKED", "BLOCK"}
_NEG = {"ALLOW", "ALLOWED", "PASS", "PASSED", "RELEASE", "RELEASED"}


def _to_bit(token, where):
    if token is None:
        raise ValueError("%s: missing decision" % where)
    t = str(token).strip().upper()
    if t in _POS:
        return 1
    if t in _NEG:
        return 0
    raise ValueError(
        "%s: unrecognized decision %r (known: %s / %s). Not guessing."
        % (where, token, "|".join(sorted(_POS)), "|".join(sorted(_NEG)))
    )


def extract(path, id_field, dec_field):
    """Returns {id: decision_bit} from a real decision/gold file."""
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    data = obj["decisions"] if isinstance(obj, dict) and "decisions" in obj else obj
    out = {}
    if isinstance(data, dict):
        # {id: "DECISION"} or {id: {decision: ...}}
        for k, v in data.items():
            dec = v if isinstance(v, str) else (v.get(dec_field) if isinstance(v, dict) else None)
            out[str(k)] = _to_bit(dec, "%s[%s]" % (path, k))
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise ValueError("%s[%d]: record is not an object" % (path, i))
            idv = r.get(id_field)
            if idv is None:
                for alt in ("action_id", "item_id", "id"):
                    if r.get(alt) is not None:
                        idv = r[alt]
                        break
            if idv is None:
                raise ValueError("%s[%d]: no id field (looked for %s)" % (path, i, id_field))
            out[str(idv)] = _to_bit(r.get(dec_field), "%s[%s]" % (path, idv))
    else:
        raise ValueError("%s: top-level is neither list nor object-with-decisions" % path)
    if not out:
        raise ValueError("%s: no decision records found" % path)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ingest",
        description="Convert gate decision files into an indmeter ballots CSV.",
    )
    ap.add_argument("verifiers", nargs="+", metavar="NAME=FILE",
                    help="one or more verifier columns, e.g. v_a=gate_a_decisions.json")
    ap.add_argument("--gold", help="expected-decision file (optional)")
    ap.add_argument("--out", default="ballots.csv", help="output CSV path (default ballots.csv)")
    ap.add_argument("--id-field", default="action_id", help="verifier id field name (default action_id)")
    ap.add_argument("--gold-id-field", default=None,
                    help="gold id field if it differs from --id-field (e.g. snapshot_id)")
    ap.add_argument("--decision-field", default="decision", help="decision field name (default decision)")
    ap.add_argument("--strip-suffix", default=None,
                    help="strip this separator and everything after it from VERIFIER ids "
                         "before joining to gold (e.g. '::' turns CASE-01::PRIMARY into "
                         "CASE-01). Refuses to merge if it would collide two ids.")
    ap.add_argument("--gold-only", action="store_true",
                    help="emit only items that have gold, with the gold column")
    args = ap.parse_args(argv)

    # parse NAME=FILE pairs
    sources = []
    for spec in args.verifiers:
        if "=" not in spec:
            ap.error("verifier must be NAME=FILE (got %r)" % spec)
        name, _, path = spec.partition("=")
        name = name.strip()
        if not name:
            ap.error("empty verifier name in %r" % spec)
        sources.append((name, path.strip()))

    try:
        cols = {name: extract(path, args.id_field, args.decision_field) for name, path in sources}
        gold_id_field = args.gold_id_field or args.id_field
        gold = extract(args.gold, gold_id_field, args.decision_field) if args.gold else None
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    names = [n for n, _ in sources]

    # Optionally normalize verifier ids to join to a differently-keyed gold.
    if args.strip_suffix:
        sep = args.strip_suffix
        for n in names:
            remapped = {}
            for k, v in cols[n].items():
                nk = k.split(sep)[0]
                if nk in remapped and remapped[nk] != v:
                    print("error: --strip-suffix %r collides ids in %s "
                          "(%r and another map to %r with different decisions). Not merging."
                          % (sep, n, k, nk), file=sys.stderr)
                    return 1
                remapped[nk] = v
            cols[n] = remapped

    # items = intersection of all verifiers' ids (only items every verifier voted on)
    common = None
    for n in names:
        ids = set(cols[n].keys())
        common = ids if common is None else (common & ids)
    common = sorted(common or [])
    if not common:
        print("error: no item ids shared across all verifiers", file=sys.stderr)
        return 1

    dropped = {}
    for n in names:
        extra = len(cols[n]) - len(common)
        if extra:
            dropped[n] = extra

    # gold coverage
    use_gold = False
    items = common
    if gold is not None:
        covered = [i for i in common if i in gold]
        missing = [i for i in common if i not in gold]
        if args.gold_only:
            items = sorted(covered)
            use_gold = True
            if missing:
                print("note: --gold-only -> keeping %d gold-covered items, dropping %d without gold"
                      % (len(items), len(missing)), file=sys.stderr)
            if not items:
                print("error: no items have gold", file=sys.stderr)
                return 1
        elif not missing:
            use_gold = True
        else:
            print("WARNING: gold covers %d of %d items; %d have no gold."
                  % (len(covered), len(common), len(missing)), file=sys.stderr)
            print("         Emitting WITHOUT a gold column — indmeter will fail closed"
                  " (not certifiable). Use --gold-only to keep just the covered subset.",
                  file=sys.stderr)

    # write CSV
    header = ["item_id"] + (["gold"] if use_gold else []) + names
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for it in items:
            row = [it]
            if use_gold:
                row.append(gold[it])
            row.extend(cols[n][it] for n in names)
            w.writerow(row)

    print("wrote %s" % args.out)
    print("  verifiers: %d (%s)" % (len(names), ", ".join(names)))
    print("  items:     %d" % len(items))
    print("  gold:      %s" % ("yes" if use_gold else "NO -> indmeter will fail closed"))
    if dropped:
        print("  dropped (voted by some but not all verifiers): %s"
              % ", ".join("%s +%d" % (n, c) for n, c in dropped.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
