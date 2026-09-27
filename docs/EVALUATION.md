# Evaluation on a synthetic benchmark

Deposit → withdrawal pairs with a known answer are not publicly available, so the
method's accuracy on real cases cannot be measured here. This document measures how
the method behaves on generated data where the answer is known, with
`tools/simulate.py`. The data are synthetic and the numbers depend on the assumptions
below; they show what each component contributes and how the method fails, not how
often it is right on real Tornado Cash withdrawals.

## Setup

A fake explorer replays a depositor's deposits and the pools' `Withdrawal` logs
through the real pipeline (`multi.correlate`, `run_demix`, `ranked_candidates`). Each
trial draws:

- **the depositor**: 2, 3 or 5 notes in the 1 ETH pool, and in half of the trials also
  2–4 notes in the 0.1 ETH pool, deposited minutes apart with a hand-set gas price;
- **its exit**: one address receiving every note inside a 24-hour exit window; the exit
  self-relays with probability 0.5, reuses the deposit gas price once with probability
  0.3 and is a direct counterparty of the depositor with probability 0.2;
- **the field**: `N` unrelated recipients per pool window (N = 10, 50, 200), each with a
  geometric number of withdrawals (continue with probability 0.4, so most receive one),
  10 % of them active in every pool, 15 % of withdrawals self-relayed, and each
  withdrawal reusing the depositor's gas price by chance with probability 0.002.

Blocks are treated as pre-EIP-1559, so the gas-price signal is live, and no
counterparty is a contract. 200 trials per setting, seed 1:

```
python tools/simulate.py --experiment all --trials 200 --seed 1
```

## Metrics

Every recipient of a searched pool window is one instance; the depositor's exits are
the positives. A listed candidate is a positive prediction.

- **P, R, F1, FPR** — precision, recall, F1 and false-positive rate of "listed";
- **P ≥mod, R ≥mod** — the same when only `moderate` and `strong` count as positive;
- **Top-1, Top-5** — share of trials with a true exit in the first 1 or 5 rows;
- **PR-AUC** — average precision of the band-then-score order over all instances;
- **found / no lead** — share of trials where a true exit is listed / nothing is listed;
- **bystander strong / moderate / weak** — share of trials where the strongest band given
  to an unrelated address is that band.

Configurations add one component at a time: **A** voucher-sized counts with no gate, in
no order; **B** the discrimination gate and a `disc`-scaled score; **C** self-relay;
**D** gas price; **E** linked address; **F** denomination profile (the full model). **G**
is the window-overlap suppression in the operator graph, measured separately on pairs
of wallets.

## Results

#### Ablation, 10 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.430 | 0.969 | 0.596 | 0.129 | 0.000 | 0.000 | 0.555 | 0.985 | 0.417 |
| B | + discrimination gate | 0.433 | 0.966 | 0.598 | 0.127 | 0.000 | 0.000 | 0.670 | 0.995 | 0.722 |
| C | + self-relay | 0.433 | 0.966 | 0.598 | 0.127 | 0.515 | 0.483 | 0.670 | 0.995 | 0.633 |
| D | + gas price | 0.430 | 0.973 | 0.597 | 0.130 | 0.555 | 0.603 | 0.780 | 1.000 | 0.719 |
| E | + linked address | 0.432 | 0.979 | 0.600 | 0.130 | 0.580 | 0.668 | 0.835 | 1.000 | 0.781 |
| F | + denomination profile | 0.433 | 0.983 | 0.601 | 0.130 | 0.582 | 0.671 | 0.850 | 1.000 | 0.843 |

#### Ablation, 50 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.250 | 0.705 | 0.127 |
| B | + discrimination gate | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.370 | 0.810 | 0.392 |
| C | + self-relay | 0.130 | 0.976 | 0.230 | 0.131 | 0.208 | 0.517 | 0.350 | 0.775 | 0.307 |
| D | + gas price | 0.128 | 0.983 | 0.227 | 0.134 | 0.229 | 0.644 | 0.515 | 0.875 | 0.444 |
| E | + linked address | 0.129 | 0.986 | 0.227 | 0.134 | 0.246 | 0.709 | 0.615 | 0.910 | 0.581 |
| F | + denomination profile | 0.129 | 0.986 | 0.227 | 0.134 | 0.246 | 0.709 | 0.665 | 0.920 | 0.682 |

#### Ablation, 200 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.100 | 0.350 | 0.038 |
| B | + discrimination gate | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.080 | 0.455 | 0.156 |
| C | + self-relay | 0.040 | 0.967 | 0.076 | 0.118 | 0.059 | 0.470 | 0.120 | 0.430 | 0.107 |
| D | + gas price | 0.039 | 0.970 | 0.075 | 0.120 | 0.065 | 0.563 | 0.285 | 0.545 | 0.233 |
| E | + linked address | 0.039 | 0.977 | 0.075 | 0.120 | 0.074 | 0.647 | 0.415 | 0.620 | 0.425 |
| F | + denomination profile | 0.039 | 0.977 | 0.075 | 0.120 | 0.074 | 0.647 | 0.490 | 0.670 | 0.515 |

#### Negative controls, 50 unrelated recipients per pool window

| control | setting | found | top-1 | no lead | bystander strong | bystander moderate | bystander weak |
| --- | --- | --- | --- | --- | --- | --- | --- |
| exit after the window | config A | 0.00 | 0.00 | 0.06 | 0.00 | 0.00 | 0.94 |
| exit after the window | config B | 0.00 | 0.00 | 0.06 | 0.00 | 0.00 | 0.94 |
| exit after the window | config F | 0.00 | 0.00 | 0.06 | 0.11 | 0.78 | 0.06 |
| one note per fresh address | config A | 0.00 | 0.00 | 0.10 | 0.00 | 0.00 | 0.90 |
| one note per fresh address | config B | 0.00 | 0.00 | 0.10 | 0.00 | 0.00 | 0.90 |
| one note per fresh address | config F | 0.00 | 0.00 | 0.07 | 0.10 | 0.78 | 0.05 |

#### Counter-measures against the full model, 50 unrelated recipients

| counter-measure | setting | found | top-1 | no lead | bystander strong | bystander moderate | bystander weak |
| --- | --- | --- | --- | --- | --- | --- | --- |
| delay | 0 of 3 notes after the window | 1.00 | 0.01 | 0.00 | 0.04 | 0.85 | 0.10 |
| delay | 1 of 3 notes after the window | 0.00 | 0.00 | 0.01 | 0.04 | 0.86 | 0.10 |
| delay | 2 of 3 notes after the window | 0.00 | 0.00 | 0.01 | 0.06 | 0.82 | 0.12 |
| delay | 3 of 3 notes after the window | 0.00 | 0.00 | 0.01 | 0.07 | 0.82 | 0.10 |
| fresh addresses | 1 exit address(es) | 1.00 | 0.01 | 0.00 | 0.04 | 0.85 | 0.10 |
| fresh addresses | 2 exit address(es) | 0.00 | 0.00 | 0.01 | 0.06 | 0.84 | 0.10 |
| fresh addresses | 3 exit address(es) | 0.00 | 0.00 | 0.00 | 0.06 | 0.83 | 0.11 |
| relayer | self-relayed | 1.00 | 0.39 | 0.00 | 0.04 | 0.85 | 0.10 |
| relayer | through a relayer | 1.00 | 0.01 | 0.00 | 0.04 | 0.85 | 0.10 |
| gas strategy | deposit gas price reused | 1.00 | 0.99 | 0.00 | 0.04 | 0.85 | 0.10 |
| gas strategy | wallet default gas | 1.00 | 0.01 | 0.00 | 0.04 | 0.85 | 0.10 |
| denominations | both pools to one exit | 1.00 | 0.01 | 0.00 | 0.12 | 0.88 | 0.01 |
| denominations | each pool to its own exit | 1.00 | 0.01 | 0.00 | 0.14 | 0.86 | 0.01 |

#### Operator links between two wallets (configuration G)

| Wallets | merged without suppression | merged with suppression |
| --- | --- | --- |
| unrelated, identical fingerprints, same hour | 1.00 | 0.00 |
| unrelated, distinct fingerprints, same hour | 1.00 | 0.00 |
| one operator, distinct fingerprints, days apart | 0.94 | 0.91 |

#### Sensitivity to the field assumptions (full model)

| Field | Top-1 | PR-AUC | control: strong | control: moderate | control: no lead |
| --- | --- | --- | --- | --- | --- |
| baseline | 0.67 | 0.68 | 0.11 | 0.78 | 0.06 |
| no chance gas reuse | 0.65 | 0.73 | 0.00 | 0.84 | 0.07 |
| chance gas reuse x5 (p=0.01) | 0.59 | 0.61 | 0.36 | 0.57 | 0.04 |
| self-relay 5 % | 0.77 | 0.77 | 0.09 | 0.61 | 0.06 |
| self-relay 30 % | 0.64 | 0.63 | 0.15 | 0.79 | 0.06 |
| busier recipients (p_more=0.6) | 0.62 | 0.67 | 0.14 | 0.82 | 0.01 |

## Reading the results

- **The gate and the score order the field; they do not shrink it.** A and B list the
  same addresses at these field sizes (every voucher-sized count in a field of 10+ passes
  `disc ≥ 0.5`), but B's `disc`-scaled score lifts PR-AUC about threefold (0.13 → 0.39 at
  N = 50).
- **Each independent family adds ranking power.** Through B, D, E and F, PR-AUC rises
  0.39 → 0.44 → 0.58 → 0.68 and top-1 0.37 → 0.67 at N = 50; at N = 200 top-1 rises from
  0.08 to 0.49. Recall of "listed" stays near 0.98: the exit is almost always among the
  candidates, and the question is how high it ranks.
- **Self-relay alone lowers PR-AUC** (0.39 → 0.31 at N = 50) because 15 % of unrelated
  withdrawals are self-relayed too and move bystanders to `moderate`. With 5 % it helps
  (sensitivity table). Self-relay is a structural tie, not evidence on its own, which is
  why it shares a family with the count match.
- **`moderate` is a lead, not a finding.** In the negative controls, where no exit is
  findable, an unrelated address reaches `moderate` in about 80 % of trials.
- **False `strong` comes from chance gas-price reuse.** It occurs in 11 % of negative
  controls at the baseline, 0 % when no withdrawal reuses the deposit gas price by chance
  and 36 % at five times the baseline rate. The gas-price signal is only as good as the
  deposit gas price is unique.
- **Counter-measures remove the exit, not the leads.** Delaying one note past the window
  or splitting the notes over fresh addresses drops "found" from 1.00 to 0.00, but the
  tool still lists unrelated addresses (mostly `moderate`); "no lead" stays near 0.
- **Suppression removes false operator links.** Unrelated wallets that deposit in the
  same hour are merged in every trial by a naive shared-candidate rule and in none with
  the window-overlap suppression, while two wallets of one operator that share a
  consolidator are still merged in 91 % of trials (94 % naive).

## Two public laundering cases

`tools/real_cases.py` runs the tool on the depositors of two cases attributed to the
Lazarus group, with addresses from the public investigations collected at
github.com/tayvano/lazarus-bluenoroff-research. Default settings, 30-day window.

| Case | Depositors (deposits) | Exits known | Exits in a window | Candidates | True | Other |
|---|---|---|---|---|---|---|
| KuCoin 2020 | 2 (55) | 7 | 6 per depositor | 7 | 6 | 1 `moderate` (chance gas-price reuse) |
| Harmony 2022 | 14 (857) | 30 of 55 listed | 0-30 per depositor | 0 | 0 | 0 |

**KuCoin.** The attacker ran its own withdrawal caller, `0x82e6...`, which sent 128
withdrawals to 7 addresses. That is the ground truth; no demix signal uses the
transaction sender, except `linked_sender` when the sender is a counterparty of the
depositor. Before `linked_sender` the tool listed nothing: each exit received 11-29
withdrawals, none equal to a voucher (24 or 30 notes), because the exits collected
notes of several deposits. With it, the depositor `0x820a...`, which had transacted
with the caller, gets 6 candidates, all 6 true exits (the seventh exit received its
withdrawals months later, outside the window). The other depositor gets one
unrelated `moderate` candidate from a chance gas-price match.

**Harmony.** Investigators listed 55 withdrawal addresses; 30 of them received 180
withdrawals from the 100 ETH pool within 30 days of the deposits, 6 per address in
most cases, against vouchers of 60 notes. The tool lists no candidate and no false
one. The investigators selected these addresses partly by withdrawal count and
batching, so this list is not independent of count-based reasoning.

Both cases show the same limit as the synthetic counter-measures: when notes are
pooled and redistributed, a count match finds nothing, and the tool reports no
lead rather than a wrong one. Real laundering elsewhere shows the same pattern:
in the 27 MixLaunder cases, direct linkage covers 1.27 % of laundering addresses
(4 cases), the same address on both sides 1.48 % (3 cases).

## Limits

The field model is simple: recipients are independent, gas prices are drawn from a
short list of defaults, exits never reuse addresses across cases, and every exit lands
inside the window unless a counter-measure says otherwise. Real pools have bursts,
relayer-specific patterns and post-EIP-1559 fee markets. The benchmark is a controlled
test of the method's components and failure modes; a labelled set of real deposit →
withdrawal pairs remains the only way to measure its accuracy.
