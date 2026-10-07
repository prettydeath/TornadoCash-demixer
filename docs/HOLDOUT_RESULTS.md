# Hold-out results

Results of the test pre-registered in [PREREG_HOLDOUT.md](PREREG_HOLDOUT.md)
(tag `prereg-holdout-v1`). The rule, labels, samples and hypotheses are the ones
frozen there; the numbers come from `tools/holdout_report.py` on the cached runs.

R = (decoy leads / decoy withdrawals) / (target leads / target withdrawals). R near 1
is chance level; R well below 1 means the signal fires above chance. Intervals are
95 %: bootstrap over depositors (2,000 resamples, seed 20260801), exact when a side
has fewer than five leads.

## Samples

| | Ethereum | Polygon |
|---|---:|---:|
| seed / planned size | 11 / 600 | 12 / 300 |
| sample SHA-256 (matches the registration) | `2048de4d…` | `effb45d3…` |
| depositors with both runs | 591 | 300 |
| target / decoy withdrawals examined (30 d) | 1,093,754 / 1,029,187 | 206,282 / 207,106 |

Nine Ethereum depositors have no completed pair of runs (explorer errors) and are
left out; none was replaced.

## Ethereum, 30 days

| signal / class | target | decoy | R | 95 % CI |
|---|---:|---:|---:|---|
| early direct link | 84 | 2 | 0.025 | 0.003–0.094 (exact) |
| late direct link (context) | 45 | 35 | 0.83 | 0.51–1.31 |
| linked sender | 14 | 11 | 0.84 | 0.10–1.63 |
| labelled shared deposit | 12 | 2 | 0.18 | 0.02–0.80 (exact) |
| early profile | 11 | 0 | 0 | 0–0.42 (exact) |
| count match | 23,700 | 21,561 | 0.97 | 0.90–1.05 |
| moderate | 109 | 15 | 0.15 | 0.04–0.31 |
| strong | 3 | 0 | 0 | 0–2.57 (exact) |

At 72 hours: moderate 103 / 7, R 0.08 (0–0.23); early direct link 85 / 2,
R 0.028 (0.003–0.103). All three `strong` leads combine a direct link with a shared
deposit address.

## Polygon, 30 days

| signal / class | target | decoy | R | 95 % CI |
|---|---:|---:|---:|---|
| early direct link | 59 | 1 | 0.017 | 0–0.098 (exact) |
| late direct link (context) | 29 | 38 | 1.31 | 0.68–2.45 |
| linked sender | 9 | 2 | 0.22 | 0.02–1.07 (exact) |
| labelled shared deposit | 5 | 0 | 0 | 0–1.09 (exact) |
| early profile | 6 | 2 | 0.33 | 0.03–1.86 (exact) |
| count match | 4,805 | 4,955 | 1.03 | 0.95–1.10 |
| moderate | 74 | 5 | 0.067 | 0.012–0.147 |
| strong | 0 | 0 | – | – |

At 72 hours: moderate 71 / 1, R 0.016 (0–0.091); labelled shared deposit 4 / 0
(fewer than five target leads).

## Hypotheses

| | criterion | result | verdict |
|---|---|---|---|
| H1 | early direct link, upper bound of R (30 d) < 1 | 0.094 | **supported** |
| H2 | count match, interval contains 1 or lies above it | 0.90–1.05 | **supported** |
| H3 | moderate, upper bound of R (72 h) < 0.25 | 0.23 | **supported** (narrow margin) |
| H4 | Polygon: early direct link and labelled shared deposit each below 1 where ≥ 5 target leads | early link 0–0.098; shared deposit 5 leads, 0–1.09 | **not supported** for the shared deposit |

H4 fails on the shared deposit: no chance match was seen (0 decoy leads), but five
target leads cannot exclude chance level. It is reported as failed, as the
registration requires. The registration names no window for H4; the 30-day window of
H1 was used. At 72 hours the shared deposit has four target leads, too few to judge.

`linked_sender` stays at chance level on Ethereum (R 0.84) and is inconclusive on
Polygon (upper bound 1.07). Its use as a lead source remains an expert decision,
not a measured one.

## Deviations from the registration

1. **Twin-pool fix in `demix.py`.** After collection a bug was found: a deposit made
   through the Tornado router was assigned to a pool by (token, amount), but three
   pairs of Ethereum pools share that pair (1000 DAI, 50000 cDAI, 500000 cDAI and
   their `#2` twins). The pool is now read from the contract that emitted the
   Deposit event in the receipt (`EtherscanClient.deposit_emitters`); an unresolved
   case is flagged `pool_ambiguous`. `demix.py` and `etherscan.py` therefore differ
   from the frozen hashes. Four hold-out depositors (and one in each of the earlier
   samples) had such deposits; their runs were redone with the fix. Effect: count
   match +4 / +6 leads and +213 / +199 withdrawals examined (target / decoy, 30 d);
   no other row changed, no verdict changed. The runs from before the fix are kept
   for comparison. `heuristics.py` and `deposit_addresses.py` are unchanged.
2. **Interrupted collection.** The Polygon collection stopped at 165 of 300
   depositors when the machine restarted and was resumed with the same seed; cached
   runs were reused, the sample is unchanged (same hash).
3. **H4 window** was not named in the registration; 30 days was used (see above).

## Reproduce

```bash
python tools/placebo_eval.py --network ethereum --seed 11 --sample 600 --dir holdout1 --exclude-dir . --exclude-dir post2022 --exclude-dir dar --exclude-dir dar_labels_only --exclude-dir dar_nolabels --exclude-file .cache/labels/placebo/exclude_ens_eval.txt
python tools/placebo_eval.py --network polygon --seed 12 --sample 300 --dir polygon1
python tools/holdout_report.py .cache/labels/placebo/holdout1
python tools/holdout_report.py .cache/labels/placebo/polygon1
```
