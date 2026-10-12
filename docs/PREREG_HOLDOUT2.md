# Pre-registration: second hold-out, rule 2.19

This file is committed and tagged (`prereg-holdout2-v1`) **before** any data of the
samples below are collected. It follows the first registration
([PREREG_HOLDOUT.md](PREREG_HOLDOUT.md), tag `prereg-holdout-v1`), whose results are in
[HOLDOUT_RESULTS.md](HOLDOUT_RESULTS.md).

## Why a second hold-out

The first hold-out tested rule 2.17. In it the linked withdrawal sender
(`linked_sender`) was again at chance level (Ethereum 14 / 11, R 0.84) and was 11 of
the 15 decoy `moderate` leads. Rule 2.19 therefore makes `linked_sender` context only.
That decision was taken **after** looking at the first hold-out, so re-scoring the same
depositors under 2.19 (moderate, 30 days: 101 / 4, R 0.042, exact interval 0.011–0.111)
is exploratory. This registration tests 2.19 on depositors that no earlier analysis
has seen.

## Frozen rule

- Code: rule 2.19 as committed with this file. `linked_sender` is shown as context: no
  weight, no class, not an independent family (like `linked_late` since 2.17).
- Lead sources: early direct link (`linked`, first withdrawal ≤ 72 h after the
  voucher's last deposit); labelled shared exchange deposit (`shared_deposit`); early
  multi-pool profile (`early_profile`). `strong` = leads from two independent sources,
  `moderate` = one, `weak` = otherwise. Amount+timing, gas price, late links and linked
  senders never raise the class.
- The files below are not changed until the report is written:

| File | SHA-256 |
|---|---|
| `tornado_demix/heuristics.py` | `017c372093d20f2caa0dfb091dda268919e12f3024876983ff0e20bd7cc5de71` |
| `tornado_demix/demix.py` | `97fc58e815c3e04588397549f321d0595cb596c50d212a71d8ddfa6ee2948d86` |
| `tornado_demix/deposit_addresses.py` | `d65d13ed16be30569b060395aec473a29667aa04e83cad7201bcdb1ad0f60211` |
| `tornado_demix/etherscan.py` | `5f3d6b26aab4f21cb3ad66bdfc89393c2b70c62a731cb843cc18fbe30f31f2c7` |

- Attribution labels: unchanged from the first registration (prettydeath/wallet-attribution
  `cca7567`; `ethereum.csv` SHA-256 `0392efcc…b81d6`, `polygon.csv` SHA-256 `2f311d33…6f07b`).

## Samples

Eligibility is the same as before (deposits span ≤ 60 days, decoy windows after the
pool's first withdrawal, target windows complete before 2026-08-01). No depositor of any
earlier sample is eligible.

1. **Ethereum.** `tools/placebo_eval.py --network ethereum --seed 21 --sample 600
   --dir holdout2 --exclude-dir . --exclude-dir post2022 --exclude-dir dar
   --exclude-dir dar_labels_only --exclude-dir dar_nolabels --exclude-dir holdout1
   --exclude-file .cache/labels/placebo/exclude_ens_eval.txt` (the ENS list is private;
   SHA-256 of its sorted, newline-terminated content `26549ae2f6705d00b38ef51147ebe94fa406ee1a5a2e607aa79ee6580869dfc8`,
   unchanged since the first registration).
   Dry run at tagging time: 55,033 eligible, 1,917 excluded, 600 drawn; sample SHA-256
   `68e9711c7a0947c409a94ff854e17c65bce3dd35e43516740003f6dd67948347`.
2. **Polygon.** `tools/placebo_eval.py --network polygon --seed 22 --sample 400
   --dir polygon2 --exclude-dir polygon1`.
   Dry run at tagging time: 4,444 eligible, 300 excluded, 400 drawn; sample SHA-256
   `1f6db763abd5de5acfdefa18ea869bcc980ccab795e79cb76e7e98b81a7742e5`.

Failed explorer requests are retried; depositors whose runs still fail are listed in the
report, never silently dropped or replaced.

## Measures

As in the first registration: `tools/holdout_report.py` per sample, 30-day and 72-hour
windows, per signal and per class, R = (L_decoy / N_decoy) / (L_target / N_target), 95 %
intervals by bootstrap over depositors (2,000 resamples, seed 20260801) or exact when a
side has fewer than five leads; the source combination of every `strong` candidate.

## Hypotheses (rule 2.19)

- **H1.** Ethereum, early direct link: upper bound of the R interval (30 days) below 1.
- **H2.** Ethereum, count match: the R interval (30 days) contains 1 or lies above it.
- **H3.** Ethereum, class `moderate`: upper bound of R (30 days, the tool's default
  window) below 0.25.
- **H4.** Ethereum, labelled shared deposit: upper bound of R (30 days) below 1 if at
  least five target leads exist; otherwise too few to judge.
- **H5.** Polygon, early direct link: upper bound of R (30 days) below 1.
- **H6.** Polygon, class `moderate`: upper bound of R (30 days) below 0.25.
- **Descriptive only:** early profile, linked sender, gas price, `strong` (counts, and R
  where the interval rules allow).

A hypothesis that fails is reported as failed. No threshold, window, signal or sample
size is changed after collection; any deviation is listed with its reason.
