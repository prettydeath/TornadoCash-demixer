# Pre-registration: hold-out placebo test of the 2.17 rule

This file is committed and tagged (`prereg-holdout-v1`) **before** any hold-out data
are collected. Its git timestamp and the tag fix what is tested and how; the results
are reported against it, including where they disagree with the earlier estimates.

## Frozen rule

- Code: the evidence rule of release 2.17.0 (commit `de1ecb0`, Zenodo version DOI
  10.5281/zenodo.23085805). `tornado_demix/heuristics.py`, `tornado_demix/demix.py`
  and `tornado_demix/deposit_addresses.py` are byte-identical to that commit
  (SHA-256 below) and are not changed until the hold-out report is written.
- Lead sources: early direct link (`linked`, first withdrawal ≤ 72 h after the
  voucher's last deposit) or `linked_sender`; labelled shared exchange deposit
  (`shared_deposit`); early multi-pool profile (`early_profile`). `strong` = leads
  from two independent sources, `moderate` = one, `weak` = otherwise. Amount+timing,
  gas price and late links never raise the class.
- Attribution labels: prettydeath/wallet-attribution at commit `cca7567`
  (2026-10-01), passed with `TORNADO_DEMIX_ATTRIBUTION`: `ethereum.csv` 105,267 rows
  (32,943 exchange), SHA-256 `0392efcc458a60260dad72d08717ff46ad62df5e147bf551be7db3d1b27b81d6`;
  `polygon.csv` 4,859 rows (156 exchange), SHA-256
  `2f311d3314b9e952efedcbf4df1fb3c5ad56c840f2e07490b347d84101f6f07b`.

| File | SHA-256 |
|---|---|
| `tornado_demix/heuristics.py` | `109bb16bc67a759167510beb25868f3c6dc40b77a7e936f0a58232cf5c7dafa7` |
| `tornado_demix/demix.py` | `2a5d1bdfc4b1f9bc0ae95cd52b19363d65f1d273a05addd6f96720463e1a4873` |
| `tornado_demix/deposit_addresses.py` | `d65d13ed16be30569b060395aec473a29667aa04e83cad7201bcdb1ad0f60211` |

## Samples

1. **Ethereum hold-out.** `tools/placebo_eval.py --network ethereum --seed 11
   --sample 600 --dir holdout1 --exclude-dir . --exclude-dir post2022 --exclude-dir dar`
   `--exclude-dir dar_labels_only --exclude-dir dar_nolabels
   --exclude-file .cache/labels/placebo/exclude_ens_eval.txt` (the 27 depositors of
   the ENS evaluation; the list is private, its SHA-256 is
   `26549ae2f6705d00b38ef51147ebe94fa406ee1a5a2e607aa79ee6580869dfc8`). Eligibility is
   the same as in the earlier samples (deposits span ≤ 60 days, decoy windows after
   the pool's first withdrawal, target windows complete before 2026-08-01). No wallet
   of any earlier placebo sample, of the 1,000-depositor shared-deposit universe or of
   the ENS evaluation is eligible.
   Dry run at tagging time: 55,033 eligible, 1,323 excluded, 53,710 left, 600 drawn;
   SHA-256 of the sorted sample
   `2048de4df7c52315f42de18dca51bebb1759bb9559c6162f0b25ad03bdc66768`.
2. **Second chain, address-link signals.** `tools/placebo_eval.py --network polygon
   --seed 12 --sample 300 --dir polygon1`.
   Dry run at tagging time: 4,444 eligible, 300 drawn; SHA-256 of the sorted sample
   `effb45d3815f0d082e24a3573453d83503349e8182f9a4ecca1c3ec0901dcf67`.
3. If the free explorer tier refuses Polygon, Arbitrum with the same parameters
   (`--network arbitrum --seed 12 --dir arbitrum1`) replaces it, and the switch is
   reported.

Failed explorer requests are retried; depositors whose runs still fail are listed in
the report, never silently dropped.

## Measures

For each sample, `tools/holdout_report.py` re-scores the cached runs with the frozen
rule and reports, for 30-day and 72-hour windows:

- per signal (early direct link, late direct link, linked sender, labelled shared
  deposit, early profile, count match, gas price) and per class (moderate, strong,
  strong + moderate): leads in target and decoy windows, withdrawals examined, the
  ratio R = (L_decoy / N_decoy) / (L_target / N_target);
- 95 % intervals: bootstrap over depositors (2,000 resamples, fixed seed), and an
  exact interval when either side has fewer than five leads;
- the source combination of every `strong` candidate.

## Hypotheses and how they are judged

- **H1.** The early direct link exceeds chance on the hold-out: the upper bound of
  the 95 % interval of R (30 days) is below 1.
- **H2.** Amount+timing (count match) does not: the interval of R contains 1 or lies
  above it.
- **H3.** The class `moderate` keeps a chance share below 0.25 (upper bound of R at
  72 hours).
- **H4.** On the second chain, the early direct link and the labelled shared deposit
  each have an R interval below 1 where at least five target leads exist; with fewer
  target leads the result is reported as too few to judge.
- **Strong.** No hypothesis: the counts of `strong` in target and decoy windows are
  reported; an R is given only if both are large enough for the interval rules above.

A hypothesis that fails is reported as failed. No threshold, window, signal or
sample size is changed after data collection; any deviation from this plan is listed
in the report with its reason.

## Also run on the existing samples (not part of the hold-out)

- `tools/tutela_baseline.py`: the Tutela / Béres et al. heuristics under the same
  target / decoy windows.
- `tools/semisynthetic.py`: real background withdrawals with planted exits whose
  error rates differ from the model's assumptions.
