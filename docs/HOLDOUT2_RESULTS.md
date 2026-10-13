# Second hold-out results (rule 2.19)

Results of the test pre-registered in [PREREG_HOLDOUT2.md](PREREG_HOLDOUT2.md) (tag
`prereg-holdout2-v1`). The rule, labels, samples and hypotheses are the ones frozen there;
numbers come from `tools/holdout_report.py` on the cached runs. R = (decoy leads / decoy
withdrawals) / (target leads / target withdrawals); 95 % intervals by bootstrap over depositors
(2,000 resamples, seed 20260801), exact when a side has fewer than five leads.

## Samples

| | Ethereum (`holdout2`) | Polygon (`polygon2`) |
|---|---:|---:|
| seed / planned size | 21 / 600 | 22 / 400 |
| sample SHA-256 (matches the registration) | `68e9711c…` | `1f6db763…` |
| depositors with both runs | 599 | 400 |
| target / decoy withdrawals examined (30 d) | 1,114,418 / 1,049,123 | 266,798 / 261,591 |

One Ethereum decoy run failed after retries; that depositor is left out, not replaced.

## Results, 30 days (target / decoy leads; R, 95 % interval)

| Signal or class | Ethereum (599) | Polygon (400) |
|---|---|---|
| early direct link | 85 / 5; 0.062 (0.012–0.126) | 92 / 5; 0.055 (0.011–0.116) |
| late direct link (context) | 31 / 52; 1.78 (1.09–2.98) | 42 / 26; 0.63 (0.37–1.03) |
| linked sender (context) | 7 / 1; 0.15 (0.003–1.18) exact | 45 / 49; 1.11 (0.00–1.32) |
| labelled shared deposit | 18 / 30; 1.77 (0.16–3.91) | 23 / 13; 0.58 (0.00–3.16) |
| early profile | 10 / 0; 0 (0–0.47) exact | 11 / 0; 0 (0–0.41) exact |
| count match | 22,779 / 22,733; 1.06 (1.00–1.12) | 5,507 / 5,183; 0.96 (0.90–1.04) |
| gas price | 0 / 0 | 0 / 0 |
| class moderate | 113 / 35; 0.33 (0.05–0.75) | 126 / 18; 0.15 (0.03–0.35) |
| class strong | 0 / 0 | 0 / 0 |

At 72 hours: class moderate 109 / 7, R 0.071 (0.020–0.144) on Ethereum and 116 / 5, R 0.048
(0.010–0.104) on Polygon; labelled shared deposit 14 / 2 and 23 / 0.

## Hypotheses

| | Criterion | Result | Verdict |
|---|---|---|---|
| H1 | Ethereum early direct link, upper bound (30 d) < 1 | 0.126 | **supported** |
| H2 | Ethereum count match, interval contains 1 or lies above | 1.002–1.119 | **supported** |
| H3 | Ethereum class moderate, upper bound (30 d) < 0.25 | 0.746 | **not supported** |
| H4 | Ethereum labelled shared deposit, upper bound (30 d) < 1 (≥ 5 target leads) | 3.91 | **not supported** |
| H5 | Polygon early direct link, upper bound (30 d) < 1 | 0.116 | **supported** |
| H6 | Polygon class moderate, upper bound (30 d) < 0.25 | 0.353 | **not supported** |

The early direct link was above chance on both chains, as in every earlier sample. H3, H4 and
H6 failed, and they failed for one reason: the labelled shared deposit address produced many
decoy leads at 30 days (Ethereum 30, Polygon 13), while at 72 hours it did not (2 and 0).

## Deviations

None from the registered procedure. As registered, the rule is not revised on these data.

## Exploratory analyses after the results (not part of the registered test)

- **Concentration.** The 30 Ethereum decoy shared-deposit leads came from 5 depositors, 20 of
  them from one; the 13 Polygon ones from 3 depositors, 9 from one. The share of depositors with
  at least one decoy lead of class moderate or strong was 10 / 599 = 1.7 % (Clopper–Pearson upper
  bound 3.0 %) on Ethereum and 8 / 400 = 2.0 % (3.9 %) on Polygon (`tools/decoy_rates.py`). The
  depositor-clustered Poisson rate ratio for class moderate on Ethereum is 0.33 (0.11–1.04).
- **Why.** The deposit addresses behind those decoy leads had 31, 35 and 18 distinct senders; the
  rule accepts up to 50 (`MAX_DEPOSIT_SENDERS`), while the median deposit address in the first
  hold-out has one sender (90th percentile 10). Such addresses are shared, not per customer.
- **Sender cap** (`tools/shared_deposit_cap.py`): with at most 10 senders the labelled shared
  deposit gives 6 / 1 (H1), 3 / 0 (H1p), 6 / 0 (H2) and 5 / 2 (H2p) target / decoy leads; with at
  most 5, 3 / 0, 1 / 0, 3 / 0 and 2 / 0. A cap removes most false leads but also most true ones;
  this is a hypothesis for a future registered test, not a result.

- **Same-time decoy** (`tools/placebo_sametime.py`: real windows, nearest-denomination ETH pool the
  depositor never used, mapped one to one; 547 of H1 and 559 of H2 depositors, the rest had used three or more of the four ETH pools).
  30 days, target / decoy, R: early direct link 72 / 6, 0.09 (0.03–0.17) and 71 / 6, 0.09
  (0.03–0.18); labelled shared deposit 12 / 1, 0.09 and 18 / 3, 0.19 (0.04–0.63); late direct link
  41 / 8, 0.21 (0.08–0.42) and 30 / 16, 0.59 (0.27–1.13); class moderate 80 / 7, 0.09 (0.03–0.17)
  and 93 / 11, 0.13 (0.05–0.23); per-wallet decoy rate 1.3 % (upper 2.6 %) and 1.8 % (3.3 %).
  The two decoys agree on the early direct link and the per-wallet rate and disagree on the shared
  deposit and the late direct link.

## Reproduce

```bash
python tools/placebo_eval.py --network ethereum --seed 21 --sample 600 --dir holdout2 --exclude-dir . --exclude-dir post2022 --exclude-dir dar --exclude-dir dar_labels_only --exclude-dir dar_nolabels --exclude-dir holdout1 --exclude-file .cache/labels/placebo/exclude_ens_eval.txt
python tools/placebo_eval.py --network polygon --seed 22 --sample 400 --dir polygon2 --exclude-dir polygon1
python tools/holdout_report.py .cache/labels/placebo/holdout2
python tools/holdout_report.py .cache/labels/placebo/polygon2
python tools/decoy_rates.py holdout2 polygon2
python tools/shared_deposit_cap.py holdout1 polygon1 holdout2 polygon2
python tools/placebo_sametime.py holdout2 && python tools/holdout_report.py .cache/labels/placebo/holdout2_st
```
