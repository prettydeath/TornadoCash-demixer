# Evaluation

Deposit → withdrawal pairs with a known answer are not publicly available. This
document combines four checks, each with its own limits:

1. a **synthetic benchmark** (`tools/simulate.py`): generated data with a known
   exit, to see what each component contributes and how the method fails;
2. a **placebo test on real depositors** (`tools/placebo_eval.py`,
   `tools/placebo_windows.py`): the real pipeline run on decoy windows that cannot
   hold the wallet's notes, which estimates the share of chance leads per evidence
   family without any labels;
3. **two public laundering cases** (KuCoin, Harmony);
4. an **ENS-labelled set**, also scored under the protocol of Wang et al. (2023).

The placebo test is the only one that measures the signals on real withdrawals at
scale. It found amount+timing and gas-price leads as frequent in decoy windows as
in real ones; above chance stood the linked-address family (a direct counterparty,
a withdrawal sent by the depositor's side, a deposit address swept to a labelled
exchange) and, measured later, an early multi-pool profile match. A band needs one
of these (since version 2.13 for the linked family, since 2.15 for the profile);
the synthetic tables below use the current rule. The gas-price signal turned out
to fire almost only on withdrawals whose gas price a relayer chose; since 2.15 it
counts only on withdrawals the user sent.

## Synthetic benchmark

### Setup

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

### Metrics

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

### Results

#### Ablation, 10 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.430 | 0.969 | 0.596 | 0.129 | 0.000 | 0.000 | 0.555 | 0.985 | 0.417 |
| B | + discrimination gate | 0.433 | 0.966 | 0.598 | 0.127 | 0.000 | 0.000 | 0.685 | 0.995 | 0.722 |
| C | + self-relay | 0.433 | 0.966 | 0.598 | 0.127 | 0.000 | 0.000 | 0.680 | 0.995 | 0.724 |
| D | + gas price | 0.433 | 0.966 | 0.597 | 0.128 | 0.000 | 0.000 | 0.705 | 0.995 | 0.743 |
| E | + linked address | 0.434 | 0.973 | 0.600 | 0.128 | 1.000 | 0.185 | 0.775 | 0.995 | 0.795 |
| F | + denomination profile | 0.435 | 0.976 | 0.602 | 0.128 | 1.000 | 0.185 | 0.845 | 0.995 | 0.913 |

#### Ablation, 50 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.250 | 0.705 | 0.127 |
| B | + discrimination gate | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.415 | 0.785 | 0.392 |
| C | + self-relay | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.410 | 0.830 | 0.395 |
| D | + gas price | 0.130 | 0.976 | 0.229 | 0.132 | 0.000 | 0.000 | 0.470 | 0.845 | 0.444 |
| E | + linked address | 0.130 | 0.983 | 0.230 | 0.132 | 1.000 | 0.182 | 0.605 | 0.880 | 0.590 |
| F | + denomination profile | 0.130 | 0.983 | 0.230 | 0.132 | 1.000 | 0.182 | 0.725 | 0.920 | 0.778 |

#### Ablation, 200 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.100 | 0.350 | 0.038 |
| B | + discrimination gate | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.130 | 0.490 | 0.156 |
| C | + self-relay | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.145 | 0.510 | 0.156 |
| D | + gas price | 0.040 | 0.970 | 0.076 | 0.118 | 0.000 | 0.000 | 0.265 | 0.590 | 0.233 |
| E | + linked address | 0.040 | 0.977 | 0.077 | 0.118 | 1.000 | 0.200 | 0.405 | 0.665 | 0.425 |
| F | + denomination profile | 0.040 | 0.977 | 0.077 | 0.118 | 1.000 | 0.200 | 0.570 | 0.805 | 0.663 |

#### Negative controls, 50 unrelated recipients per pool window

| control | setting | found | top-1 | no lead | bystander strong | bystander moderate | bystander weak |
| --- | --- | --- | --- | --- | --- | --- | --- |
| exit after the window | config A | 0.00 | 0.00 | 0.06 | 0.00 | 0.00 | 0.94 |
| exit after the window | config B | 0.00 | 0.00 | 0.06 | 0.00 | 0.00 | 0.94 |
| exit after the window | config F | 0.00 | 0.00 | 0.06 | 0.00 | 0.00 | 0.94 |
| one note per fresh address | config A | 0.00 | 0.00 | 0.10 | 0.00 | 0.00 | 0.90 |
| one note per fresh address | config B | 0.00 | 0.00 | 0.10 | 0.00 | 0.00 | 0.90 |
| one note per fresh address | config F | 0.00 | 0.00 | 0.10 | 0.00 | 0.00 | 0.91 |

#### Counter-measures against the full model, 50 unrelated recipients

| counter-measure | setting | found | top-1 | no lead | bystander strong | bystander moderate | bystander weak |
| --- | --- | --- | --- | --- | --- | --- | --- |
| delay | 0 of 3 notes after the window | 1.00 | 0.17 | 0.00 | 0.00 | 0.00 | 0.99 |
| delay | 1 of 3 notes after the window | 0.00 | 0.00 | 0.01 | 0.00 | 0.00 | 0.99 |
| delay | 2 of 3 notes after the window | 0.00 | 0.00 | 0.01 | 0.00 | 0.00 | 0.99 |
| delay | 3 of 3 notes after the window | 0.00 | 0.00 | 0.01 | 0.00 | 0.00 | 0.99 |
| fresh addresses | 1 exit address(es) | 1.00 | 0.17 | 0.00 | 0.00 | 0.00 | 0.99 |
| fresh addresses | 2 exit address(es) | 0.00 | 0.00 | 0.01 | 0.00 | 0.00 | 0.99 |
| fresh addresses | 3 exit address(es) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |
| relayer | self-relayed | 1.00 | 0.17 | 0.00 | 0.00 | 0.00 | 0.99 |
| relayer | through a relayer | 1.00 | 0.17 | 0.00 | 0.00 | 0.00 | 0.99 |
| gas strategy | deposit gas price reused | 1.00 | 0.17 | 0.00 | 0.00 | 0.00 | 0.99 |
| gas strategy | wallet default gas | 1.00 | 0.17 | 0.00 | 0.00 | 0.00 | 0.99 |
| denominations | both pools to one exit | 1.00 | 0.86 | 0.00 | 0.00 | 0.00 | 1.00 |
| denominations | each pool to its own exit | 1.00 | 0.18 | 0.00 | 0.00 | 0.00 | 1.00 |

#### Counter-measure intensity, six-note voucher (full model)

`python tools/simulate.py --experiment intensity`: the share of trials in which
the exit is listed, against the number of notes withdrawn after the window and
the number of exit addresses the notes are spread over.

Notes withdrawn after the window (of 6):

| Exit leaves | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| amount and timing only | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| also a direct transfer | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 |

Exit addresses the six notes are spread over:

| Exit leaves | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| amount and timing only | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| also a direct transfer | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| also a direct transfer, share of exits found | 1.00 | 0.50 | 0.33 | 0.25 | 0.20 | 0.17 |

One delayed note or a second exit address is enough to remove an exit that
left only amount and timing. An exit that also transacted with the depositor is
found as long as one note lands in the window; with k exit addresses only that
one is found, so the share of exits found falls as 1/k. No
bystander reaches `strong` or `moderate` at any intensity: without a linked
address a chance count or gas-price match stays `weak`.

#### Operator links between two wallets (configuration G)

| Wallets | merged without suppression | merged with suppression |
| --- | --- | --- |
| unrelated, identical fingerprints, same hour | 1.00 | 0.00 |
| unrelated, distinct fingerprints, same hour | 1.00 | 0.00 |
| one operator, distinct fingerprints, days apart | 0.94 | 0.91 |

#### Sensitivity to the field assumptions (full model)

| Field | Top-1 | PR-AUC | control: strong | control: moderate | control: no lead |
| --- | --- | --- | --- | --- | --- |
| baseline | 0.72 | 0.78 | 0.00 | 0.00 | 0.06 |
| no chance gas reuse | 0.77 | 0.87 | 0.00 | 0.00 | 0.07 |
| chance gas reuse x5 (p=0.01) | 0.67 | 0.79 | 0.00 | 0.00 | 0.09 |
| self-relay 5 % | 0.74 | 0.79 | 0.00 | 0.00 | 0.06 |
| self-relay 30 % | 0.70 | 0.76 | 0.00 | 0.00 | 0.06 |
| busier recipients (p_more=0.6) | 0.66 | 0.77 | 0.00 | 0.00 | 0.01 |

### Reading the results

- **The gate and the score order the field; they do not shrink it.** A and B list the
  same addresses at these field sizes (every voucher-sized count in a field of 10+ passes
  `disc ≥ 0.5`), but B's `disc`-scaled score lifts PR-AUC about threefold (0.13 → 0.39 at
  N = 50).
- **On generated data each family adds ranking power.** Through B, D, E and F, PR-AUC
  rises 0.39 → 0.44 → 0.59 → 0.78 and top-1 0.42 → 0.73 at N = 50; at N = 200 top-1
  rises from 0.13 to 0.57. The gas-price step is smaller than before version 2.15:
  a generated exit that reuses the deposit gas price through a relayer no longer
  counts (in the counter-measure table, top-1 for "deposit gas price reused" falls
  from 0.99 to 0.17), which is what the placebo test says about real relayed
  withdrawals. Recall of "listed" stays near 0.98: the exit is almost always
  among the candidates, and the question is how high it ranks. The placebo test below
  shows that the amount+timing and gas-price part of this does not carry over to real
  pools.
- **A band now means a linked address.** `moderate` and `strong` are reached only by
  exits that transacted with the depositor (20 % of generated exits), so P ≥mod is 1.00
  and R ≥mod about 0.18-0.20. In the negative controls, where no exit is findable, no
  unrelated address reaches `moderate` or `strong` (before version 2.13: `moderate` in
  about 80 % and `strong` in 11 % of trials, through self-relay and chance gas-price
  reuse).
- **Self-relay no longer lowers PR-AUC** (0.392 → 0.395 at N = 50): it used to lift
  self-relayed bystanders to `moderate`; it is now context inside the amount+timing
  family.
- **Counter-measures remove the exit, not the candidates.** Delaying one note past the
  window or splitting the notes over fresh addresses drops "found" from 1.00 to 0.00; the
  tool still lists unrelated addresses, now all `weak`, and "no lead" stays near 0.
- **Suppression removes false operator links.** Unrelated wallets that deposit in the
  same hour are merged in every trial by a naive shared-candidate rule and in none with
  the window-overlap suppression, while two wallets of one operator that share a
  consolidator are still merged in 91 % of trials (94 % naive).

## Placebo test on real depositors

`tools/placebo_eval.py` draws random Ethereum depositors from the ENS universe
(deposits spanning at most 60 days, windows complete before August 2026; seed 1)
and runs the real pipeline twice per depositor: on the usual windows after its
deposits (target), and on the same deposits shifted back so that every window
ends a day before its first real deposit (decoy). A withdrawal in a decoy window
cannot spend one of the wallet's notes, so every decoy lead is a false note link;
everything else — history, counterparties, deposit gas prices — is the wallet's
own. The share of target leads that chance explains (a false-discovery rate) is
estimated as decoy leads per withdrawal searched over target leads per
withdrawal searched; 95 % intervals come from a bootstrap over depositors.
`tools/placebo_windows.py` re-scores the cached runs offline for narrower exit
windows (it reproduces the 30-day bands exactly in all 304 runs).

152 depositors, 30-day window, leads that reached `strong` or `moderate` under
the previous band rule (so that every family is visible), by family:

| Family | Target leads | Decoy leads | Chance share (95 % CI) |
|---|---|---|---|
| linked address (`linked`, `linked_sender`) | 30 | 7 | 0.25 (0.07-0.54) |
| amount+timing (count match, self-relay) | 160 | 188 | 1.25 (0.79-1.81) |
| gas price | 45 | 64 | 1.51 (0.92-2.31) |
| all, previous rule | 228 | 252 | 1.17 (0.82-1.60) |

(268,745 withdrawals searched in target windows, 253,634 in decoy windows.)

The same families by exit window (target / decoy leads):

| Window | linked address | amount+timing | gas price |
|---|---|---|---|
| 6 h | 14 / 0 | 3 / 2 | 1 / 1 |
| 24 h | 16 / 0 | 11 / 12 | 8 / 2 |
| 72 h | 19 / 1 (0.06, 0-0.24) | 20 / 20 | 12 / 7 |
| 30 days | 30 / 7 (0.25, 0.07-0.54) | 160 / 188 | 45 / 64 |

Of the 152 depositors, 115 deposited after EIP-1559: for them the gas-price signal
never fires (the base-fee gate) and amount+timing has a chance share of 1.2-1.9 at
every window. The 37 earlier depositors give too few narrow-window leads to decide
either family.

What this shows:

- **Amount+timing and gas price do not separate real exits from chance on real
  depositors**, at any window from 6 hours to 30 days. The synthetic benchmark
  says they rank a planted exit well; on real pools the same count or gas price
  turns up as often where no note of the wallet can be.
- **The linked-address family does.** Its leads are four times as frequent in real
  windows as in decoy ones over 30 days, and within 72 hours of the deposit 19
  against 1. That is why a band now needs a linked address, and why a linked exit
  within 72 hours is marked as an early exit (context, not scored).
- A decoy lead is a false *note* link, not necessarily a false *identity* link: a
  linked address in a decoy window may still belong to the depositor. For the
  linked family the chance share is therefore an upper bound.
- The sample is 152 random depositors, most of whom leave no linked exit at all;
  the intervals are wide, and the result says nothing about careful users beyond
  the fact that the tool finds no evidence on them.

**Why gas price failed.** Of the gas-price matches in these runs, 44 of 45 in
target windows and 62 of 64 in decoy windows were on withdrawals sent through a
relayer: the relayer chose that gas price, not the user, so a match with the
deposit's gas price says nothing about who withdrew. Since version 2.15 the signal
counts only on withdrawals the user sent (no relayer); re-scored with that rule,
the same 152 runs give no gas-price lead at all. The tables above keep the
previous rule, under which the test was run.

### Early multi-pool profile

`tools/placebo_profile.py` tests a denomination-profile match offline on every
eligible depositor of the ENS universe (28,739 depositors with two or more notes;
no explorer calls). The profile is the wallet's note count per pool; a recipient
matches when, in every pool the wallet used, it received exactly that many
withdrawals between the pool's first deposit and its last deposit plus a window.
Decoy windows are shifted back as above.

| Profile | 24 h | 72 h | 30 days |
|---|---|---|---|
| 2+ notes, any pools | 117724 / 108163 (0.99) | 175944 / 164315 (1.00) | 911742 / 876567 (1.01) |
| 2+ notes, 2+ pools | 3222 / 1282 (0.43) | 6509 / 4275 (0.71) | 57693 / 52887 (0.96) |
| 6+ notes, any pools | 3551 / 2394 (0.74) | 4809 / 3357 (0.77) | 19681 / 16874 (0.90) |
| 6+ notes, 2+ pools | 784 / 114 (0.16) | 1089 / 331 (0.34) | 3992 / 2966 (0.78) |
| 10+ notes, any pools | 477 / 206 (0.47) | 605 / 265 (0.48) | 1557 / 1187 (0.81) |
| 10+ notes, 2+ pools | 248 / 13 (0.06) | 318 / 34 (0.12) | 642 / 314 (0.52) |

(target / decoy hits, chance share.) A single-pool profile is the count match again
and stays near chance. A profile over two or more pools is far rarer by chance,
and the shorter the window the cleaner: with at least 10 notes over two or more
pools, 318 hits against 34 within 72 hours (chance share 0.12, 95 % interval
0.08-0.16), cleaner than a direct linked address. Since version 2.15 this is the
`early_profile` signal (at least 10 notes, two or more pools, 72 hours); it makes
a lead on its own. Being a stricter count match, it belongs to the amount+timing
family, so with a count match it stays `moderate`.

### Shared exchange deposit addresses

The shared-deposit-address signal (`shared_deposit`, see
[METHODOLOGY.md](METHODOLOGY.md)) was tested the same way. `tools/placebo_dar.py`
applies it to the 152 depositors above; `tools/placebo_dar_universe.py` reads the
windows offline from the ENS universe (it reproduces the full-run result for all
152 depositors) and draws 1,000 new random depositors (seed 2). Lookups that hit an
explorer error are retried rather than silently skipped; none failed in the final
runs. Version 2.15 code, attribution set loaded unless stated.

| Sample and rule | Depositors with a deposit address | Target hits | Decoy hits | Chance share (95 % CI) |
|---|---|---|---|---|
| 152 depositors (full runs): swept to a labelled exchange (scored) | | 4 | 0 | 0 |
| 152 depositors: any deposit address found | 28 | 9 | 1 | 0.12 (0-0.53) |
| 1,000 depositors: swept to a labelled exchange (scored) | | 27 (19 depositors) | 4 | 0.16 (0.03-0.44) |
| 1,000 depositors: any deposit address found | 251 | 38 (28 depositors) | 13 | 0.36 (0.11-0.89) |
| 1,000 depositors, no attribution set (activity only) | 164 | 16 (13 depositors) | 12 | 0.80 (0.21-2.37) |

(About 2.1 million withdrawals searched in target windows and 2.0 million in decoy
windows.) An address counts as a deposit address when its outflow goes to hot
wallets. A hot wallet recognised by an exchange label gives a clean signal: 27
against 4, and 25 of the 27 are not direct counterparties of the depositor, so the
signal finds exits that `linked` does not. A hot wallet recognised by activity
alone does not: without an attribution set the signal is at chance (16 against
12). Two fixes on the way did not change that: a busy contract (a token, a DEX
router, the Tornado router) is no longer taken for a hot wallet, and an unlabelled
hot wallet must receive amounts forwarded within 3,200 blocks (the forwarding test
of Victor used by Tutela). So since version 2.15 a shared deposit address is scored
only when it sweeps to labelled exchange wallets; one found by activity alone is
shown as context. Without an attribution set the signal therefore does not fire.

(History: 2.14.0 published 57 against 18 for "label or activity"; that run used a
busy check the explorer's page cap had turned off, and later ones counted token and
router contracts as hot wallets. The "labelled exchange" numbers are the same in
every run.)

The check is also available per case (`demix --placebo`, and the web UI option):
the decoy window of the wallet under investigation, beside the real one.

## Two public laundering cases

`tools/real_cases.py` runs the tool on the depositors of two cases attributed to the
Lazarus group, with addresses from the public investigations collected at
github.com/tayvano/lazarus-bluenoroff-research. Default settings, 30-day window.

| Case | Depositors (deposits) | Exits known | Exits in a window | Candidates | True | Other |
|---|---|---|---|---|---|---|
| KuCoin 2020 | 2 (55) | 36 | 35 per depositor | 6 | 6 | 0 |
| Harmony 2022 | 14 (857) | 30 of 55 listed | 0-30 per depositor | 0 | 0 | 0 |

Run with `python tools/real_cases.py`; it also prints the context marks below.

**KuCoin.** Koh (2020, "Deanonymising the Kucoin Hacker") attributes to the
attacker two addresses that called `withdraw()` themselves without a relayer:
`0x8bd8...` (323 withdrawals) and `0x82e6...` (128, of which 114 straight to the
100 ETH pool and 14 through the router); together they sent the 437 withdrawals from
the 100 ETH pool counted there. Their recipients, 36 addresses, are the ground truth;
no demix signal uses the transaction sender, except `linked_sender` when the sender
is a counterparty of the depositor. The check covers the two case addresses that
deposited themselves (55 notes); Koh counts 497 deposits of 100 ETH for the attacker
overall, so this is a slice of the case.

The count match finds nothing: exits received many notes each, none equal to a
voucher (24 or 30 notes), because they collected notes of several deposits. With
`linked_sender`, the depositor `0x820a...`, which had transacted with `0x82e6...`,
gets 6 candidates, all true exits of that caller, all `moderate`; the first of these
withdrawals came 6.9 days after its last deposit. The 30 exits of `0x8bd8...`, which
started withdrawing a day after that deposit, are not found: `0x8bd8...` is not a
counterparty of either depositor. So of the attacker's 35 exits inside the window
the tool finds 6. How many of the others carry notes of these two depositors (55
of the attacker's 497 notes) cannot be told from chain data, so this is not a
per-depositor recall: the second caller's notes may come from other deposits. The other depositor used to get one unrelated candidate from a chance gas-price
match on a relayed withdrawal (`moderate` before version 2.13, `weak` in 2.13-2.14);
since 2.15 a relayed withdrawal earns no gas-price signal and the candidate is gone.

**Harmony.** Investigators listed 55 withdrawal addresses; 30 of them received 180
withdrawals from the 100 ETH pool within 30 days of the deposits, 6 per address in
most cases, against vouchers of 60 notes. The tool lists no candidate and no false
one. The investigators selected these addresses partly by withdrawal count and
batching, so this list is not independent of count-based reasoning.

**Context marks.** Every true exit that fell in a window had at most a day of
history before its first withdrawal: 35 of 35 in KuCoin, 30 of 30 in Harmony. The
fresh mark is shown as context and never scored, because a new unrelated wallet
is fresh as well. The shared-funder edge links the two KuCoin depositors through
`0x0060...`, an address on the investigators' list. In Harmony it groups all 14
depositors into 5 clusters through 5 funders, each shared by 2-3 depositors; each
of the 5 funders received its funds directly from the bridge exploiter
`0x0d04...ded00`. A sixth funder, shared by 4 depositors, has 200 or more
transactions and is left out as busy.

**Exit groups.** In Harmony, 287 of the 295 (true exit, depositor window) pairs
sit in an exit group. With one true exit as the only anchor, the group
holds 23.0 addresses on average, 16.8 of them (73 %) on the investigators' list;
the other members were paid out in the same bursts and are leads, not errors, as
the list itself is partial. In KuCoin, 30 of the 70 (true exit, depositor window)
pairs sit in a group; from one true exit the group holds 4.9 addresses, 4.5 of them
(91 %) true exits.

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
test of the method's components and failure modes; on real depositors the
placebo test below shows that its amount+timing and gas-price results do not carry
over.

## A labelled set from ENS

There is no public set of matched deposits and withdrawals. As in Béres et al.
(2021), Tutela (2022) and Wang et al. (2023), ENS provides a partial one:
`tools/ens_labels.py` collects every deposit into and withdrawal from the four
Ethereum ETH pools from December 2019 to September 2026 (289,887 deposits by
64,681 depositors; 276,676 withdrawals to 129,752 recipients), reads the primary
ENS name of every address (4,606 have one) and links two addresses when one
controls the other's name (447 links; an owner of more than five names is taken
for a service and ignored). A depositor and a recipient of the same pool linked
this way, with the withdrawal after the deposit, form a labelled pair: 31 pairs
of 27 depositors. Only 2 of them deposited after the sanctions of August 2022.

`tools/evaluate_labels.py` runs `demix` with default settings (30-day window,
attribution labels loaded) on every labelled depositor:

| | strong | up to moderate | all bands |
|---|---|---|---|
| Pairs found (of 31; 21 inside the window) | 1 | 16 (76 % of those in the window) | 16 |
| Precision, lower bound | 1 of 4 | 16 of 40 (40 %) | 16 of 2,811 |
| Pairs found without the `linked` signal | 0 | 2 | 3 |

(Under the previous band rule, where a gas-price match or a self-relayed count
match alone reached `moderate`, the lower bound was 16 of 85, 19 %.)

All 16 pairs are found through a direct transaction between the depositor and
the recipient (`linked`). The ENS link and `linked` see the same relationship,
so this confirms that careless users also transact directly; it does not
measure the other signals. Two of the 16 pairs are also found by `shared_deposit`
(version 2.15: depositor and recipient sent funds to the same deposit address of a
labelled exchange), so they stay `moderate` without `linked`. Otherwise the other
signals find almost nothing here, for a reason the
method states in advance: 24 of the 31 pairs come from depositors whose only
voucher in that pool is a single note, where the count match cannot narrow the
field (discrimination 0.12-0.33 against the 0.5 threshold), and a gas-price
match is rarely checkable after EIP-1559. The count match fires for one pair only
(a two-note voucher), and that pair is found as `strong`. Ten pairs fall outside
the window (withdrawals 50-1,794 days after the deposit).

The precision is a lower bound. The labels say nothing about the 24 other
`strong` and `moderate` candidates, all direct counterparties of the depositor
(3 are the depositor itself; 16 withdrew within 72 hours of the deposit). The set covers only users careless enough to put an ENS
name on both sides, names are read as they are today, and a pair links two
addresses, not a deposit to a withdrawal. The pairs link named people to
Tornado Cash use, so they stay local; only these aggregates are published.

### The same set under the protocol of Wang et al.

Wang et al. (2023) validate their heuristics at the address level: test pairs are
every labelled depositor times every labelled withdrawer, and a predicted pair
that is not labelled counts as a false positive. Their average F1 of 0.55 comes
almost entirely from H3, a direct transfer between the two addresses.
`tools/wang_baseline.py` re-implements H2 (the depositor sent the withdrawal), H3
and H5 (cross-pool deposit profile) from the paper and applies the protocol to the
27 × 29 labelled addresses here:

| Method | Precision | Recall | F1 |
|---|---|---|---|
| Wang H2 | 1.00 | 0.03 | 0.07 |
| Wang H3 (direct transfer, whole history) | 1.00 | 0.93 | 0.96 |
| Wang H5 | 0.00 | 0.00 | 0.00 |
| demix, up to `moderate` | 1.00 | 0.55 | 0.71 |
| demix without `linked` | 1.00 | 0.07 | 0.13 |

A direct-transfer check alone, with no mixer analysis at all, scores 0.96,
because 27 of the 29 pairs transacted directly. Labels built from address links
(ENS, airdrop aggregation) measure whether two addresses ever met, not whether a
withdrawal spends a deposit; this holds for the F1 of 0.55 in Wang et al. as much
as for the numbers here. demix recalls less than H3 because it only looks at
withdrawals in the 30-day window after the deposit.

## Do the signals move together?

The families are assumed independent. Across the real runs behind the two
evaluations above (27 ENS-labelled depositors and 4 depositors of the KuCoin and
Harmony cases: 43,424 recipient-pool rows), every pair of signals fired together
about as often as independence predicts: count match and self-relay 4 times
(6.97 expected), count match and gas price 3 (2.68), count match and linked 4
(2.56), every other pair 0 against expectations below 0.4; the phi coefficient
stays within -0.006 and 0.004. The signals are rare (gas price 42, linked 40,
linked sender 6), so this describes the background rather than true pairs, where
signals are meant to coincide.

## Checks of the code itself

Run on 2026-09-28 at the commit that added this section.

| Check | Result |
|---|---|
| `pytest` (network blocked, incl. `getaddrinfo`) | 705 passed |
| `pytest-randomly`, seeds 1, 2, 3 | all pass in every order |
| Branch coverage (`--cov-branch`) | 93 % overall; `heuristics` 97 %, `demix` 93 %, `cli` 89 % |
| Property-based tests (Hypothesis, 24) and edge-case tests (10) | pass; the four that documented defects now pin the fixes |
| Mutation testing (`mutmut` 3.8 on `heuristics.py` and `demix.py`, 2479 mutants) | 1751 killed (71 %), 726 survived, 2 not reached (measured before the last `run_demix` test was added) |
| `pip-audit -r requirements-lock.txt` | no known vulnerabilities |
| `pytest -m live` (every shipped pool re-verified on chain) | 55 pools on 8 networks verified |
| README cases re-run (Ronin, Wintermute, Beanstalk) | same figures: 12,595.3 ETH hop; one 9.9435 ETH inflow; 271 deposits in 2.97 h, one `weak` candidate |

Most surviving mutants change log and evidence wording, result keys that no
test reads, or fallback values that the pipeline never reaches (a signal
without a weight, a boundary that no input can hit). Mutation testing raised
the score from 65 % by pinning what it found in the core: transfer mode had no
test at all, and the voucher span limit, counterparty collection, the shape of a
voucher and the end-to-end run of `run_demix` were unpinned at their edges.
mutmut does not run natively on Windows; the run used the `python:3.13-slim`
container.
