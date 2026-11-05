# independence-meter

**Measure how independent your verifiers actually are — and refuse to certify
independence when a verifier is too imprecise to count.**

`indmeter` reads a ballots CSV (N verifiers' 0/1 decisions over M items, plus a
gold column) and reports mean pairwise error correlation (phi_bar), the Kish
effective number of independent verifiers (n_eff), and a per-verifier
**precision guard** that fails closed.

Standalone tool — **not** part of any larger stack. Pure Python standard
library: no numpy, no network, no model calls. Runs anywhere Python 3 runs.

## Why the precision guard

Statistical independence is necessary but not sufficient. A second verifier
whose errors are genuinely uncorrelated with the first buys you nothing if it
is too imprecise to be trusted — its "independent" flags are just noise added
to the panel. So `indmeter` measures independence **and** checks each
verifier's precision against gold. If any verifier falls below the threshold
(or raised no flags at all), the tool prints the independence numbers but
refuses to certify, and exits non-zero. Fail-closed by design.

## Input format

CSV with a header row:

```
item_id, gold, v1, v2, ... vN
```

- `gold` — the correct decision: `1` = item SHOULD be flagged, `0` = should pass.
- `vK` — verifier K's decision: `1` = flagged, `0` = passed.
- Positive class = decision `== 1` (a flag raised).

## Usage

```
python indmeter.py sample_ballots.csv [--min-precision 0.5]
python indmeter.py --selftest
```

`--min-precision` defaults to `0.5`. Exit codes: `0` certifiable, `2` precision
fail, `1` bad input.

## Example

```
$ python indmeter.py sample_ballots.csv
Independence Meter
  file:       sample_ballots.csv
  verifiers:  3  (v1, v2, v3)
  items:      12
  phi_bar:    +0.3333  (mean pairwise phi; negative = better than independent)
  n_eff:      1.800 of 3  (Kish; phi_bar clamped to >=0 for n_eff)
  precision guard (threshold = 0.50):
    v1     precision=0.833  (TP=5 FP=1)  -> ok
    v2     precision=0.833  (TP=5 FP=1)  -> ok
    v3     precision=0.833  (TP=5 FP=1)  -> ok

INDEPENDENCE CERTIFIABLE
```

Raise the bar past what the verifiers can clear and it fails closed:

```
$ python indmeter.py sample_ballots.csv --min-precision 0.9
...
PRECISION FAIL — independence NOT certifiable
  below-threshold verifier(s): v1 (0.833), v2 (0.833), v3 (0.833)
```

## The math

- **phi** per verifier pair: the standard 2×2 phi coefficient over their 0/1
  decision vectors. If a margin is all-ones or all-zeros (denominator 0), phi
  for that pair is defined as `0.0` and the pair is noted.
- **phi_bar**: mean of all `N*(N-1)/2` pairwise phi values.
- **n_eff** (Kish design-effect form):
  `n_eff = N / (1 + (N-1) * max(phi_bar, 0))`.
  A negative phi_bar means the verifiers are *better* than independent; it is
  reported raw but clamped to 0 for n_eff (no spurious bonus).

The Kish computation is vendored from the `faithfulness-independence` repo's
`neff.py` (copied, not imported — this repo stands alone).

## Self-check

`python indmeter.py --selftest` runs known-answer math checks only (identical
verifiers → phi 1.0, n_eff 1.0; anti-correlated → phi −1.0; independent →
phi 0.0, n_eff N; degenerate margin; precision cases). These verify the
instrument against synthetic ground truth — the sample data is **not** tuned to
reproduce any external result.

## License

Apache-2.0 (see `LICENSE`). Copyright 2026 Clint Nelson / Snake River AI LLC.
