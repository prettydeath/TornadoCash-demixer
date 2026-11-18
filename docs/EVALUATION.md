# Evaluation

Deposit → withdrawal pairs with a known answer are not publicly available. This
document combines five checks, each with its own limits:

1. a **synthetic benchmark** (`tools/simulate.py`): generated data with a known
   exit, to see what each component contributes and how the method fails;
2. a **placebo test on real depositors** (`tools/placebo_eval.py`,
   `tools/placebo_windows.py`): the real pipeline run on decoy windows that cannot
   hold the wallet's notes, which gives the number of leads per evidence family in
   such a window without any labels (it can contain the depositor's own earlier
   activity, so it is not guaranteed to be pure chance);
3. **two public laundering cases** (KuCoin, Harmony);
4. an **ENS-labelled set**, also scored under the protocol of Wang et al. (2023);
5. a **pre-registered hold-out** of the frozen rule on new Ethereum and Polygon
   depositors, with published heuristics and a semi-synthetic benchmark beside it.

The placebo test is the only one that measures the signals on real withdrawals at
scale. It found amount+timing and gas-price leads as frequent in decoy windows as
in real ones; above chance stood the linked-address family (a direct counterparty,
a withdrawal sent by the depositor's side, a deposit address swept to a labelled
exchange) and, measured later, an early multi-pool profile match. A band needs one
of these (since version 2.13 for the linked family, since 2.15 for the profile).
Since version 2.16 `strong` needs lead signals from two different families (their independence is not established)
(direct link, shared deposit address, early profile; since 2.21 only the early direct link and the early profile); one source is `moderate`,
and amount+timing and gas price are context that never raises the band. Before
2.16, a lead signal plus any other family was `strong`, so a direct link plus a
count match was `strong`. The synthetic tables below use the current rule. The gas-price signal turned out
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

Regenerated on 2026-11-13 with `python tools/simulate.py --experiment all --trials 200
--seed 1` (offline, about a minute) on version 2.21.0 (commit `6a6c546`); the 2.22 tree
prints the same tables. The tables published before were produced by earlier versions
and differ from these in the `linked address` and `denomination profile` rows of the
ablations (R ≥mod 0.175-0.187 now, 0.182-0.200 before; top-1 and PR-AUC by at most
0.01), in two cells of the sensitivity table, and in the "also a direct transfer" row of
the intensity tables (84-95 % found for one to five delayed notes, 1.00 before); the
negative controls, counter-measures and operator tables are unchanged. The cause of the
intensity difference was not traced to a single change.

#### Ablation, 10 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.430 | 0.969 | 0.596 | 0.129 | 0.000 | 0.000 | 0.555 | 0.985 | 0.417 |
| B | + discrimination gate | 0.433 | 0.966 | 0.598 | 0.127 | 0.000 | 0.000 | 0.685 | 0.995 | 0.722 |
| C | + self-relay | 0.433 | 0.966 | 0.598 | 0.127 | 0.000 | 0.000 | 0.680 | 0.995 | 0.724 |
| D | + gas price | 0.433 | 0.966 | 0.597 | 0.128 | 0.000 | 0.000 | 0.705 | 0.995 | 0.743 |
| E | + linked address | 0.434 | 0.973 | 0.600 | 0.128 | 1.000 | 0.178 | 0.770 | 0.995 | 0.794 |
| F | + denomination profile | 0.435 | 0.976 | 0.602 | 0.128 | 1.000 | 0.178 | 0.840 | 0.995 | 0.913 |

#### Ablation, 50 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.250 | 0.705 | 0.127 |
| B | + discrimination gate | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.415 | 0.785 | 0.392 |
| C | + self-relay | 0.130 | 0.976 | 0.230 | 0.131 | 0.000 | 0.000 | 0.410 | 0.830 | 0.395 |
| D | + gas price | 0.130 | 0.976 | 0.229 | 0.132 | 0.000 | 0.000 | 0.470 | 0.845 | 0.444 |
| E | + linked address | 0.130 | 0.983 | 0.230 | 0.132 | 1.000 | 0.175 | 0.605 | 0.880 | 0.589 |
| F | + denomination profile | 0.130 | 0.983 | 0.230 | 0.132 | 1.000 | 0.175 | 0.725 | 0.920 | 0.777 |

#### Ablation, 200 unrelated recipients per pool window

| Config | Components | P | R | F1 | FPR | P >=mod | R >=mod | Top-1 | Top-5 | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | amount+timing, no gate | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.100 | 0.350 | 0.038 |
| B | + discrimination gate | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.130 | 0.490 | 0.156 |
| C | + self-relay | 0.040 | 0.967 | 0.076 | 0.118 | 0.000 | 0.000 | 0.145 | 0.510 | 0.156 |
| D | + gas price | 0.040 | 0.970 | 0.076 | 0.118 | 0.000 | 0.000 | 0.265 | 0.590 | 0.233 |
| E | + linked address | 0.040 | 0.977 | 0.077 | 0.118 | 1.000 | 0.187 | 0.400 | 0.665 | 0.413 |
| F | + denomination profile | 0.040 | 0.977 | 0.077 | 0.118 | 1.000 | 0.187 | 0.565 | 0.805 | 0.657 |

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
| also a direct transfer | 1.00 | 0.84 | 0.84 | 0.86 | 0.94 | 0.95 | 0.00 |

Exit addresses the six notes are spread over:

| Exit leaves | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| amount and timing only | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| also a direct transfer | 1.00 | 0.89 | 0.92 | 0.95 | 0.97 | 0.98 |
| also a direct transfer, share of exits found | 1.00 | 0.44 | 0.31 | 0.24 | 0.19 | 0.16 |

One delayed note or a second exit address is enough to remove an exit that
left only amount and timing. An exit that also transacted with the depositor is
still found in 84-95 % of trials with one to five delayed notes; it is lost when
all six notes are delayed. With k exit addresses only one is found, so the share of
exits found falls roughly as 1/k. No bystander reaches `strong`
or `moderate` at any intensity: without a linked address a chance count or gas-price
match stays `weak`.

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
| no chance gas reuse | 0.76 | 0.87 | 0.00 | 0.00 | 0.07 |
| chance gas reuse x5 (p=0.01) | 0.67 | 0.78 | 0.00 | 0.00 | 0.09 |
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
- **A band now means a linked address.** `moderate` is reached only by
  exits that transacted with the depositor (20 % of generated exits), so P ≥mod is 1.00
  and R ≥mod about 0.18-0.19. The generated field has no shared deposit address and
  no early profile, so `strong` (two lead-signal families) never arises in it; the tables
  are byte-identical under the 2.16 rule. In the negative controls, where no exit is findable, no
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

`tools/placebo_eval.py` (its JSON keys `fdr` / `fdr_95ci` and register keys
`placebo.fdr.*` keep their historic names for registered records; they hold the
decoy-to-target rate ratio, not a false-discovery rate) draws random Ethereum depositors from the ENS universe
(deposits spanning at most 60 days, windows complete before August 2026; seed 1)
and runs the real pipeline twice per depositor: on the usual windows after its
deposits (target), and on the same deposits shifted back so that every window
ends a day before its first real deposit (decoy). A withdrawal in a decoy window
cannot spend one of the wallet's notes, so a decoy lead is a false note link (the window
can contain the depositor's own earlier activity, so it is not guaranteed to be
pure chance); everything else — history, counterparties, deposit gas prices — is the wallet's
own. The share of target leads that chance explains (the ratio R below) is
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
  against 1. That is why a band needs a lead signal such as a linked address, and why a linked exit
  within 72 hours is marked as an early exit. Since 2.17 a direct link counts only
  within 72 hours ([below](#direct-links-early-versus-late)).
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
pools (4,194 depositors), 318 hits against 34 within 72 hours (chance share 0.12,
95 % interval 0.08-0.16). A direct linked address is cleaner in the same 72-hour
window (0.06), but the profile rests on different data than an address link. Since version 2.15 this is the
`early_profile` signal (at least 10 notes, two or more pools, 72 hours); it makes
a lead on its own. Being a stricter count match, it belongs to the amount+timing
family; as one lead source it is `moderate`, and it reaches `strong` only together
with a second lead source (a direct link; before 2.21 also a shared deposit address).

The thresholds (at least 10 notes, two or more pools, 72 hours) were chosen on this
universe, which includes depositors that the two hold-outs later drew; the hold-outs
therefore did not test the choice of these thresholds on independent depositors.

**Hold-out and sensitivity.** `tools/placebo_profile.py` takes `--period
before|after` (split at the sanctions, 2022-08-08), `--notes`, `--hours` and
`--out`; its default output is unchanged. The rule as used by the tool (10+ notes, 2+
pools, 72 h) on each half:

| Period | Depositors | Target / decoy hits | Chance share (95 % CI) |
|---|---|---|---|
| before 2022-08-08 | 1,448 (1,455 with the 2.23 tool) | 142 / 5 | 0.04 (0.01-0.08) |
| after 2022-08-08 | 2,746 | 176 / 29 | 0.19 (0.12-0.27) |
| whole set | 4,194 | 318 / 34 | 0.12 |

The rule is cleaner before the sanctions than after, but it holds in both halves.
Sensitivity on the whole set (2+ pools, 72 h): 6+ notes 1,089 / 331 (0.34), 10+
notes 318 / 34 (0.12), 15+ notes 112 / 7 (0.07); a 168 h window with 10+ notes
gives 383 / 79 (0.22). The thresholds were chosen from the whole-set table above
and are checked here on each half, so this is a consistency check rather than a
fully independent hold-out; a lower note threshold admits many more chance
matches, a higher one finds fewer exits.

**Permutation control.** `tools/placebo_profile_perm.py` (output
`profile_perm.json`) keeps the real 72 h windows and replaces the depositor's
profile with that of another depositor with the same pool set. The own profile
gives 319 hits; the other depositors' profiles give a mean of 45 (min 28, max 61)
over 20 rounds, a ratio of 0.14. The hits therefore come from the depositor's own
note counts, not from the pool set or the window alone.

**Other chains.** `tools/ens_labels.py universe --network ... --extra ...` builds
the same deposit/withdrawal universe for another chain (on L2 chains deposits go
through the Tornado proxy `0x0D5550d52428E7e3175bfc9550207e4ad3859b17`, which the
registry does not list), and `tools/placebo_profile.py --universe` and
`tools/placebo_profile_perm.py --universe` read it. BSC, Gnosis and Base are not
served by the free explorer tier, so Arbitrum (the same four ETH pools) and
Polygon (100, 1,000 and 10,000 MATIC) were used; universes from June 2021 to
September 2026. The rule as used by the tool (10+ notes, 2+ pools, 72 h):

| Chain | Deposits / withdrawals | Eligible depositors | Target / decoy hits | Chance share (95 % CI) | Permutation: own / borrowed profile |
|---|---|---|---|---|---|
| Ethereum | (see above) | 4,194 | 318 / 34 | 0.12 (0.08-0.16) | 319 / 45 |
| Arbitrum | 6,721 / 6,315 | 74 | 16 / 0 | 0 | 17 / 0.2 |
| Polygon | 20,194 / 19,428 | 253 | 50 / 1 | 0.02 (0.00-0.09) | 54 / 4.9 |

The early profile stands above chance on both chains; the samples are small,
Arbitrum's especially. The linked-address placebo test was not repeated there.

### Does a second family make a lead stronger?

Before 2.16, a lead signal plus any other family was `strong`. `tools/placebo_strong.py`
(output `strong.json`) checks this on the 152 depositor runs: among candidates
with a lead signal, how often does a second family appear in real versus decoy
windows?

| Candidates with a lead signal and | 72 h target / decoy | 30 days target / decoy |
|---|---|---|
| amount+timing | 4 / 1 (chance share 0.30, 95 % CI 0-1.87) | 4 / 0 |
| nothing else | 17 / 0 | 28 / 7 (0.27) |

A count match next to a lead did not lower the chance share, and the counts are
small. So amount+timing and gas price no longer raise the band; they are shown
as context. Under the 2.16 rule these 152 runs give no `strong` lead: they predate
`shared_deposit`, so `strong` could only have come from a direct link plus an early
profile. `moderate` (one lead source) is 21 / 1 at 72 h (0.06, CI 0-0.23) and
32 / 7 at 30 days (0.23, CI 0.07-0.53).

### Direct links: early versus late

The 152 runs are mostly from before the sanctions. A second sample of 150
depositors whose first deposit is on or after 2022-08-08 (`tools/placebo_eval.py
--after 2022-08-08 --dir post2022 --seed 3`) was run the same way, and
`tools/placebo_periods.py` splits both by period. Together they hold 302
depositors (210 after the sanctions: 150 plus 60 of the first sample). Splitting
the direct-link leads (`linked`, at the 30-day window) by the delay of the
recipient's first withdrawal after the voucher's last deposit (target / decoy
leads):

| First deposit | within 72 h | later than 72 h |
|---|---|---|
| before 2022-08-08 | 15 / 0 | 5 / 4 |
| on or after 2022-08-08 | 11 / 1 | 3 / 9 |
| all 302 | 26 / 1 | 8 / 13 |

Re-run on 2.23.0: see [Re-run on 2.23.0](#re-run-on-2230-2026-111617).

A direct link withdrawn within 72 hours stands well above chance in both
periods; a later one is at chance level. For the 210 post-2022 depositors the
whole linked-address family gives 12 / 1 within 72 hours (chance share 0.11) but
14 / 12 within 30 days (0.95). The pooled number above, 30 against 7 over 30
days, hid this: it mixed early links, which separate, with late ones, which do
not. Since 2.17 a direct link is `linked` only when the first withdrawal came
within 72 hours (`EARLY_EXIT_HOURS`); a later one is `linked_late`, listed in the
evidence with its delay, never scored and never a lead. The withdrawal-sender
signal gave 3 / 0 early and 5 / 2 late, too few to decide, and in the KuCoin case
the attacker's real exits started 6.9 days after the deposit, so `linked_sender`
is left as is; so are `shared_deposit` and `early_profile`. The tables above
keep the rule under which they were measured.

Re-scored under 2.17 (all strong and moderate leads, real / decoy):

| Sample | within 72 h | within 30 days |
|---|---|---|
| 152 depositors (first sample) | 21 / 1 (0.06, 0-0.21) | 25 / 1 (0.04, 0-0.17) |
| 210 post-2022 depositors | 12 / 1 (0.11, 0-0.53) | 12 / 3 (0.28, 0-0.90) |
| all 302 depositors | 31 / 1 (0.04, 0-0.15) | 35 / 3 (0.09, 0-0.27) |

Re-run on 2.23.0: see [Re-run on 2.23.0](#re-run-on-2230-2026-111617).

(Before 2.17 the first sample gave 32 / 7, 0.23, over 30 days.) On the ENS set
2.17 finds 12 of the 31 pairs (21 inside the window) as `moderate` or `strong`
among 28 such candidates (precision lower bound 43 %, against 16 of 40 before);
the two `strong` candidates are both labelled pairs; four labelled pairs fall to
`weak` because their direct link came later than 72 hours. Under the protocol of
Wang et al. demix up to `moderate` scores precision 1.00, recall 0.41, F1 0.59
(0.71 before). The evaluation tools re-apply the current heuristics to cached
runs, so these numbers follow the rules as shipped.

### Bybit (2025)

The Bybit theft of 21 February 2025 is a later, independently labelled case: the
repository of Liu et al. (*Evasion Under Blockchain Sanctions*, WWW 2026) lists
about 9,300 tracked and 12,200 Elliptic-flagged Ethereum addresses. Nine of them
deposited into the Ethereum ETH pools (24 February to 2 April 2025; 1 to 22 notes).
`demix` (30-day window, attribution set loaded) gives one `moderate` lead in
total, a withdrawal back to the depositor's own address, and one listed address
among the `weak` candidates (a count match). No early multi-pool profile fired
for the three depositors with 11 to 22 notes, and no unlisted address reached
`moderate` or `strong`. The launderers spread the exits, so the method finds
almost nothing here, but it raises no false lead either.

### Signals tested and not used

**Priority tip after EIP-1559.** Since London a sender's gas price is the block's
base fee plus a priority tip, so the old gas-price signal cannot fire; the tip is
the part a user chooses. `tools/placebo_tip.py` computes tips for every
self-relayed withdrawal and every deposit of the 302 placebo depositors (base
fees from a public node) and flags a recipient whose tip equals one of the
depositor's deposit tips and is rare in the pool window:

| Rarity rule | 72 h, target / decoy | 30 days, target / decoy |
|---|---|---|
| tip shared by at most 3 self-relayed withdrawals | 33 / 44 (1.57, 0.98-2.58) | 60 / 70 (1.26, 0.83-1.97) |
| at most 10 | 102 / 105 (1.21) | 181 / 204 (1.22) |

Tips cluster on wallet defaults (3, 0.5, 1 and 2 gwei), so a shared tip is
chance-level and the signal is not used. Wallet fingerprints (gas limit,
transaction type, fee settings) belong to the same class of client defaults and
would need a transaction lookup per withdrawal; given this result they were not
pursued. Anonymity-mining reveals (AP/TORN claims) apply only to 2020-2021
deposits, too few in these samples to test.

### Does the discrimination gate remove chance matches?

`tools/placebo_disc.py` (output `disc.json`) bins count matches (every recipient
whose withdrawal count equals a voucher size) by discrimination, in real and decoy
windows:

| Bin | Window | Target / decoy | Chance share (95 % CI) |
|---|---|---|---|
| all count matches | 72 h | 8,966 / 7,640 | 1.01 |
| gated in (disc ≥ 0.5) | 72 h | 717 / 591 | 0.98 (0.80-1.20) |
| disc ≥ 0.95 | 72 h | 119 / 117 | 1.17 |
| gated in | 30 days | 5,569 / 5,256 | 1.00 |

The gate cuts the number of count matches about twelvefold, but the chance share
stays near 1 in every bin: it reduces the field the analyst has to read and does
not separate real count matches from chance ones.

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

### Current rule (2.17) in one table

Since 2.19 the "linked sender (lead)" row below is context only (see [Rule 2.19](#rule-219)). Since 2.21 so is the "shared deposit" row (see [Rule 2.21](#rule-221-shared-deposit-address-is-context)).

`tools/strong_eval_*.py` re-score the cached runs offline under the 2.17 rule. R is
decoy leads per withdrawal searched over target leads per withdrawal searched;
below 1 it estimates the chance share of target leads, near or above 1 the signal
is chance level. Intervals: bootstrap over depositors, or exact (*) when a side
has fewer than five leads.

| Signal or class | Depositors | Window | Target / decoy | R (95 % CI) |
|---|---:|---|---|---|
| early direct link (lead) | 302 | 30 d | 26 / 1 | 0.04 (0.00–0.16) |
| late direct link (context) | 302 | 30 d | 8 / 13 | 1.76 (0.69–6.4) |
| linked sender (lead) | 302 | 30 d | 8 / 2 | 0.27 (0.03–1.36)* |
| shared deposit, labelled exchange (lead) | 1,000 | 30 d | 27 / 4 | 0.16 (0.03–0.44) (re-run on 2.23.0: 55 / 16, R 0.31, 0.14–0.51) |
| shared deposit, no labels (context) | 1,000 | 30 d | 16 / 12 | 0.80 (0.21–2.37) (re-run on 2.23.0: 67 / 35, R 0.56, 0.32–0.93) |
| early multi-pool profile (lead) | 4,194 | 72 h | 318 / 34 | 0.12 (0.08–0.16) |
| count match (context) | 302 | 30 d | 9,305 / 8,540 | 0.99 (0.92–1.07) |
| gas price (context) | 302 | 30 d | 1 / 2 | too few |
| moderate | 302 | 72 h | 30 / 1 | 0.04 (0.00–0.15) |
| moderate | 302 | 30 d | 34 / 3 | 0.10 (0.00–0.29) |
| strong | 302 | 30 d | 1 / 0 | too few |
| strong + moderate | 302 | 72 h / 30 d | 31 / 1, 35 / 3 | 0.04, 0.09 |

Re-run on 2.23.0: see [Re-run on 2.23.0](#re-run-on-2230-2026-111617).

The first 152 runs predate the shared-deposit signal; adding its later-computed
labelled hits gives 33 / 1 and 38 / 3 for strong + moderate and 2 / 0 for strong.

**`strong` is structural, not measured.** It appeared only in target windows (one
or two cases, each an early direct link plus a labelled shared deposit — the same
combination as both `strong` candidates of the ENS set). On the 1,000 depositors an
early profile never coincided with a labelled shared deposit in either window; a
direct link plus shared deposit cannot be counted there (no counterparty history
was fetched). Only `moderate` has a measured chance share, so the tool suggests the
same independent check for both classes.

The 72 h split of the direct link was decided on these data (the 72 h bound itself
came from the earlier early-exit flag) and the 150 post-2022 depositors were part
of that analysis, so the 2.17 rule was exploratory until checked on new depositors
(done below).
The early profile differs between periods (0.04 before the 2022 sanctions, 0.19
after; the intervals do not overlap) and fires for 4,194 of the 64,681 depositors
in the universe (about 6.5 %).

### Pre-registered hold-out and a second chain

The rule above was frozen (file hashes, labels, samples and four hypotheses in
[PREREG_HOLDOUT.md](PREREG_HOLDOUT.md), tag `prereg-holdout-v1`) and then run on 600
new Ethereum depositors (591 completed) and 300 Polygon depositors. Full tables and
deviations: [HOLDOUT_RESULTS.md](HOLDOUT_RESULTS.md).

| | Ethereum | Polygon |
|---|---|---|
| early direct link, 30 d | 84 / 2, R 0.025 (0.003–0.094) | 59 / 1, R 0.017 (0–0.098) |
| labelled shared deposit, 30 d | 12 / 2, R 0.18 (0.02–0.80) | 5 / 0, R 0 (0–1.09) |
| count match, 30 d | R 0.97 (0.90–1.05) | R 1.03 (0.95–1.10) |
| moderate, 72 h | 103 / 7, R 0.08 (0–0.23) | 71 / 1, R 0.016 (0–0.091) |
| strong, 30 d | 3 / 0 | 0 / 0 |

Re-run on 2.23.0: see [Re-run on 2.23.0](#re-run-on-2230-2026-111617).

H1 (early direct link above chance), H2 (count match at chance level) and H3
(moderate chance share below 0.25) held. H4 held for the direct link on Polygon but
not for the labelled shared deposit: five target leads and no decoy lead cannot
exclude chance. `linked_sender` stayed at chance level on Ethereum (R 0.84) and
remains an expert decision. One deviation: a pool-assignment bug for twin token
pools was fixed after collection and six runs redone; only count-match totals moved.

### Rule 2.19

Version 2.19 makes `linked_sender` (a withdrawal sent by the depositor or one of its
direct counterparties) context only: it is shown as evidence, has no weight, never
raises the band and is not an independent family, like `linked_late` since 2.17.
Lead-signal families are now the early direct link, the labelled shared deposit and the
early multi-pool profile.

Reason: in no placebo sample did `linked_sender` separate from chance. On the 302
depositors it was 8 target against 2 decoy leads (R 0.27, interval 0.03–1.36); in
the 2026 Ethereum hold-out 14 / 11 (R 0.84); on Polygon 9 / 2 (R 0.22, 0.02–1.07).
In the Ethereum hold-out it was 11 of the 15 decoy `moderate` leads. Without it,
`moderate` there is 101 / 4, R 0.042 (exact 0.011–0.111). Re-run on 2.23.0: see [Re-run on 2.23.0](#re-run-on-2230-2026-111617).

Trade-off: in the KuCoin 2020 case the linked withdrawal sender found 6 of the
attacker's 35 exits. They are now shown as context, not as `moderate` leads.

The change was decided after seeing the hold-out numbers, so it is exploratory. A
second hold-out that tests 2.19 is pre-registered in
[PREREG_HOLDOUT2.md](PREREG_HOLDOUT2.md) (tag `prereg-holdout2-v1`); until it is
reported the rule has not been confirmed on new data. The tables above are
historical and were not rewritten: they describe the rule in force when each was
measured.

### Rule 2.21: shared deposit address is context

The second hold-out ([HOLDOUT2_RESULTS.md](HOLDOUT2_RESULTS.md)) tested rule 2.19 on 599
Ethereum and 400 Polygon depositors. The labelled shared deposit address was not shown above
chance at 30 days (Ethereum 18 target against 30 decoy leads, R 1.77, interval 0.16-3.91;
Polygon 23 against 13, R 0.58, 0.00-3.16; clustered by depositor). Pre-registered hypothesis
H4 (upper bound of R below 1) was not supported, and H3 and H6 failed with it. At 72 hours
the counts were 14 against 2 and 23 against 0. The 30 Ethereum decoy leads came from 5
depositors, 20 of them from one. The deposit addresses behind them had 31, 35 and 18 distinct
senders (the rule accepts up to 50), so they were shared, not per customer.
Re-run on 2.23.0: see [Re-run on 2.23.0](#re-run-on-2230-2026-111617).

Version 2.21 therefore treats `shared_deposit` as `linked_sender` has been treated since
2.19: it is shown as evidence, has no weight, never raises the band and is not an independent
family. Lead-signal families are now the early direct link and the early multi-pool profile; `strong`
needs both. This is a post-hoc change: HOLDOUT2_RESULTS.md (frozen) states that, as
registered, the rule is not revised on these data, and the change was decided afterwards,
having seen the hold-out numbers. It is exploratory and has not been tested on new data. The
tables above are historical and were not rewritten.

The same release fixes five defects in the deposit-address classifier
(`tornado_demix/deposit_addresses.py`, `tornado_demix/rpc.py`) that were reported on
2026-11-06 and confirmed:

1. A token transfer was counted twice as an outgoing transfer (the `txlist` call to the
   token contract and the `tokentx` row); the call row is now dropped.
2. Address poisoning: zero-value transfers are ignored, and a token transfer out of the
   address whose transaction the address did not send is dropped. A spoofed non-zero
   transfer into the address by a fake token is not detected (no token metadata offline).
3. An EIP-7702 delegated account (code `0xef0100` plus a 20-byte address) is an EOA, not a
   contract.
4. The forwarding window was 3,200 blocks, which is 10.7 hours only on Ethereum; it is now
   38,400 seconds from the row timestamps (blocks only when a timestamp is missing), and a
   sweep that forwards the sum of several incoming transfers since the previous outgoing
   transfer is accepted.
5. With the contract status unknown (a failed `eth_getCode` lookup), an unlabelled busy
   address could still be taken for a hot wallet, so contracts such as a bridge depository
   passed as exchange wallets in the reported test; an unknown status is now not taken for
   an EOA.

The fixes change which addresses are listed as deposit addresses; they were not re-run on the
cached hold-out data, so no number above reflects them.

### Deposit-address kinds (2.21)

A report of 2026-11-06 and a follow-up check ran the classifier on 10 addresses.
With an exchange label set, the Binance, Crypto.com, Bybit and Coinbase deposit addresses
were found and swept to a labelled exchange (evidence; context since 2.21). Without labels, the Bybit and Coinbase ones were found by activity
and shown unscored; the Binance one was missed because its hot wallet is a proxy
contract, and the Crypto.com one because its hot wallet has fewer than 10,000
transactions. These two are limits, not tuned away. The same check showed a Polygon
address that sweeps like a per-customer deposit address to a sweep target that no available public label set names, and
three Polygon addresses that the report called forwarders, whose outflows are executed by
an operator and were dropped only by the poisoning filter. The classifier now reports a `kind`
(`exchange_deposit`, `service_deposit`, `forwarder`, `smart_account`; see
[METHODOLOGY.md](METHODOLOGY.md)) and a context-only `gas_funder`. Re-run on the 10
addresses after the change, the three Polygon addresses are all executed through the
ERC-4337 EntryPoint v0.8 (`0x4337084d9e255ff0702461cf8895ce9e3b5ff108`, with different
bundlers as sender) and are classified `smart_account`, not `forwarder`; the Polygon
address that sweeps to that unnamed target stays `exchange_deposit` by activity,
because no label set available to the run names its sweep target. The service,
forwarder and smart-account kinds were added after seeing those 10 addresses, so they are exploratory and
have not been tested on new data; they are never scored and never link two addresses.

### Deposit-address classifier fixes (2.22, exploratory)

Dated 2026-11-13. Defects in the classifier and in the Blockscout tag classes, and what
changed (details in [METHODOLOGY.md](METHODOLOGY.md)):

1. Blockscout tags that start with an exchange brand were taken for exchange wallets even
   when they named an exploiter ("Bybit Exploiter 1", "Bybit exploit 21Feb2025"), an asset
   ("Binance Coin"), a wallet product ("Coinbase Smart Wallet") or a DEX contract ("OKX
   DEX: Aggregator"). With `--labels-blockscout`, a sweep to an exploiter address could then
   count as evidence. An exchange tag now needs a wallet word and no veto word.
2. The depositor's token transfers were read only on networks with token pools, so on
   Polygon, BSC, Arbitrum, Optimism, Base, Gnosis and Avalanche a USDC or USDT deposit
   address of the depositor was never examined, and a direct token transfer to a candidate
   never earned `linked` there. They are read on every network now (one more explorer call
   per depositor), so on those 7 networks `linked` can now also come from a token
   counterparty; this was not measured (exploratory).
7. Token rows fed the depositor's direct counterparties unfiltered (on Ethereum since token
   pools were read). A fake-token Transfer naming the depositor as sender or recipient, a
   zero-value `transferFrom` of a real token, or such a row placed after the withdrawal,
   made any withdrawal recipient a `linked` lead (moderate band; strong with
   `early_profile`). Only token rows the depositor really took part in count now (see the
   linked-address rule in [METHODOLOGY.md](METHODOLOGY.md)), on every network.
3. Sweeps sent by an EIP-7702 sponsor (the transaction goes to the address itself) were
   dropped as address poisoning (48 of 69 real sweeps of one Polygon address); spoofed "from
   the depositor" fake-token transfers to look-alike addresses could fill the 25 examined
   slots; and a fake token could make any address a "sender". Outflows in the address's own
   transactions (sent by it or to it) are kept, only real transfers of the depositor are
   counterparties, and a sender counts only when the address later sweeps its asset.
4. `MAX_DEPOSIT_SENDERS = 50` rejected exchange deposit addresses with 56 to 142 one-off
   senders. Up to 300 are admitted when at least 95 % of the outgoing transfers are sweeps
   (above 50 senders); up to 50 the 80 % share is unchanged.
5. The gas-funder threshold of 0.01 native coin found none of the gas top-ups on Polygon
   (0.0155 to 1.6 POL, 8 addresses). Per-network thresholds, Polygon 2 POL.
6. An address swept only to unlabelled busy addresses was shown as an (unscored)
   "exchange deposit", although nothing named an exchange; it is `deposit_like` with
   `service: unknown` now. Exchange names were not case-normalised ("Binance, binance").

Corrections to numbers published earlier, with their status, are in [Errata (2.22)](#errata-222).

Only the `shared_deposit` scoring is unchanged: it stays context (rule 2.21), and only an
`exchange_deposit` swept to labelled exchange wallets is `evidence`. `linked` changes: it now
uses the filtered token counterparties on all networks (items 2 and 7). The changes were not
re-run on the cached hold-out data, so no number above reflects them, and the new thresholds
(300 senders, 95 %, the gas-funder table) are judgement calls, not measured.

### Decoy rates without blind wallets (exploratory)

The detectability check (`tools/power_measure.py`, `.cache/labels/placebo/power/RESULTS.md`) planted one
early direct link in each cached decoy window of the second hold-out. In 14 of 599 Ethereum
windows (2.34 %) and 31 of 400 Polygon windows (7.75 %) there was nowhere to plant it: every
counterparty of the wallet is a contract. Planted but missed: 0. This measures counterparty
availability, not the power of the pipeline. A withdrawal back to the depositor itself is rated
moderate in all 45 cases and does occur (self leads are 12 of 95 target leads on Ethereum and 17
of 103 on Polygon in the real windows). What these wallets cannot show is a direct link to a
third-party address, in the real window as in the decoy one, so they are blind to it on both sides and are dropped from target and decoy
together (`tools/decoy_rates.py`, never from the decoy only), and reported as a stratum of their own.

| sample | stratum | wallets | moderate+strong target / decoy leads | wallets with a decoy lead (upper 95 %) |
|---|---|---|---|---|
| Ethereum hold-out 2 | all | 599 | 95 / 5 | 5 (1.94 %) |
| | without blind | 585 | 95 / 5 | 5 (1.98 %) |
| | blind only | 14 | 0 / 0 | 0 (23.2 %) |
| Polygon 2 | all | 400 | 103 / 5 | 5 (2.89 %) |
| | without blind | 369 | 103 / 4 | 4 (2.75 %) |
| | blind only | 31 | 0 / 1 | 1 (16.7 %) |

Dropping blind wallets leaves the picture unchanged: all target leads sit in the observable
stratum and the decoy rate per wallet moves from 0.83 to 0.85 % (Ethereum) and from 1.25 to 1.08 %
(Polygon); the plain ratio of decoy to target lead rates is 0.056 before and after on Ethereum and goes from 0.050 to 0.040 on Polygon.
Early-profile leads: 10 / 0 and 11 / 0 in both samples, none among blind wallets. The one Polygon
decoy lead of a blind wallet (0x8330936aa55d45a89913a42a9ebb6811b5af50fd, pool 1000 MATIC) is a
self `linked` lead, a withdrawal to the depositor itself; it lies outside the observable stratum
by definition and is shown only in the blind one.
Re-run on 2.23.0: planted link not detectable in 59 of 600 (Ethereum; 58 blind wallets) and 145 of 400 (Polygon) windows, most of them
`no_qualifying_link` (new in 2.23); moderate+strong 31 / 3 and 49 / 4 (all), 29 / 3 and 41 / 3
(without blind). Under the 2.23 direct-link rule a planted link cannot count in 9.8 % of Ethereum and
36 % of Polygon decoy windows.
Both strata are a post-hoc split of data already seen; not pre-registered, not a new test.
`--placebo` carries the same flag per wallet (`decoy_detectable`,
`direct_link_observable`). Numbers: `.cache/labels/placebo/decoy_rates_2_2/`.

### Merkle-root exclusion (exploratory)

`demix --root-check` excludes a candidate's withdrawal when the Merkle root it proves against
held none of the depositor's notes in the pool (rule in [METHODOLOGY.md](METHODOLOGY.md) 4c).
It is not a lead and not scored. (Numbers in this section were measured before 2.23 with the
old root read and one history size per chain; see Errata (2.23); re-run on 2.23.0: [Re-run on 2.23.0](#re-run-on-2230-2026-111617).)
Feasibility came first (`tools/root_ceiling.py`, root history
100 on Ethereum and 30 on Polygon pools): at most 11.01 % of the Ethereum and 10.94 % of the
Polygon withdrawals in the hold-out windows could be excluded at all (upper bound, all
withdrawals of the pool in the window, newest note only).

Measurement: `tools/root_exclusion_eval.py`, offline on the cached second hold-out runs
(`holdout2` Ethereum, `polygon2` Polygon, target and decoy), read in full in
`.cache/labels/placebo/root_exclusion/RESULTS.md`. The runs were not re-run or re-scored.
Samples: every moderate candidate (no strong ones exist in these runs) and the weak candidates of
the first 10 (Ethereum) or 40 (Polygon) depositors in sorted file order; the Ethereum weak sample
was cut from 40 to 10 for time (explorer rate limit, one transaction lookup per withdrawal).
Candidate rows lost to a failed explorer lookup: Ethereum target/moderate 5 (so 90 of 95 read);
Polygon none in the final run.

Decoy windows. With the real leaf indices every decoy withdrawal is excluded (1,277 of 1,277
on Ethereum, 1,095 of 1,095 on Polygon), so the decoy note's leaf is emulated: the number of
pool insertions up to the shifted deposit's block, `L`. A first version excluded any decoy
withdrawal with `n_root <= L`. That is biased: between the shifted block and the pool's real
insertion number `L` the newest root holds exactly `L` leaves, so every decoy withdrawal mined
in that gap is excluded by construction, whereas in a real window the note is inserted at the
deposit itself. Checked on the cached runs, this is what the first version's decoy exclusions
were: of the 17 decoy withdrawals it excluded (9 Ethereum, 8 Polygon) all 17 had `n_root`
equal to the emulated leaf and 16 were mined before insertion `L` (all 8 on Polygon, 8 of 9
on Ethereum). The rule now treats the emulated note as inserted at the block of insertion `L`
and does not check a withdrawal mined before it ("decoy gap, not checked": 8 on each chain).

| | Ethereum target | Ethereum decoy | Polygon target | Polygon decoy |
|---|---|---|---|---|
| moderate candidates read | 90 | 5 | 103 | 5 |
| weak candidates read | 568 | 545 | 335 | 407 |
| candidate withdrawals checked | 1,527 | 1,277 | 1,266 | 1,095 |
| decoy gap, not checked | 0 | 8 | 0 | 8 |
| roots found / not found | 1,527 / 0 | 1,277 / 0 | 1,266 / 0 | 1,095 / 0 |
| withdrawals excluded | 0 | 1 | 0 | 0 |
| candidates fully excluded (any band) | 0 | 0 | 0 | 0 |
| candidates partly excluded (weak) | 0 | 1 | 0 | 0 |
| note-partial withdrawals (context) | 436 | 316 | 200 | 76 |

Every root was found in the recomputed index (5,165 of 5,165), so the tree reconstruction and
the calldata parsing agree with the chain. In the real windows nothing was excluded: none of 193
moderate and 903 weak candidates lost a withdrawal. In the decoy windows, with the gap
withdrawals set aside, one withdrawal of 2,372 was excluded (one weak Ethereum candidate, partly)
and no lead of any band disappeared. The numbers are small and do not support a rate or a claim
that the check removes chance leads; they say it removed none of the 193 real-window moderate
leads read and almost none of the decoy candidates. The ceiling is for all withdrawals of a
pool; candidates' withdrawals, which come after the deposit, are excluded far less often.
This was not pre-registered; it is a description of two cached samples.

### Published heuristics under the same design

`tools/tutela_baseline.py` re-implements the heuristics of Béres et al. (2021) and
Tutela (2022) and scores them on the same target / decoy windows (302 depositors,
pooled):

| heuristic | target / decoy | R (95 % CI) |
|---|---|---|
| address match (Tutela 6.1, Béres H1) | 3 / 3 | 1.08 (0.15–8.1)* |
| gas price (Tutela 6.2, Béres H2) | 0 / 0 | – |
| Béres H3 (linked addresses) | 36 / 11 | 0.33 (0.14–0.65) |
| Tutela 6.3 (linked) | 9 / 2 | 0.24 (0.03–1.16)* |
| Tutela 6.4 (multi-denomination) | 716 / 706 | 1.07 (0.88–1.29) |
| any Tutela heuristic | 728 / 711 | 1.06 (0.88–1.27) |

Only the linked-address heuristics stand above chance; the multi-denomination
heuristic, which dominates Tutela's counts, fires as often in windows that cannot
hold the wallet's notes. TORN anonymity mining (Tutela 6.5) is left out: the decoy
shifts time, not the mining blocks.

On the four hold-out samples (1,889 depositors, pooled; `.cache/labels/placebo/tutela_holdout/`)
the result held out of sample: multi-denomination 2,890 / 2,632 (R 0.96, 0.90–1.03), any Tutela
heuristic R 0.94 (0.88–1.00), gas price (Béres H2, Polygon) 98 / 98 (R 1.06); Béres H3 386 / 110
(R 0.30, 0.23–0.38), Tutela 6.3 37 / 6 (R 0.17, 0.05–0.34), address reuse 94 / 57 (R 0.64,
0.45–0.89). The early direct link (H3 restricted to 72 h) gave 320 / 13 (R 0.04) on the same samples.

### Semi-synthetic benchmark

`tools/semisynthetic.py` plants depositors with a known exit into the real
withdrawal background of the 302 placebo depositors (12 per background run and
scenario; errors none / rare / model / frequent, three delay laws, single or spread
withdrawals). In decoy windows, where every inherited link is chance:

| errors | precision ≥ moderate | recall ≥ moderate | false strong |
|---|---|---|---|
| none | 0.00–0.79 (few leads) | 0.00–0.03 | 0 |
| rare | 0.81–0.89 | 0.02–0.06 | 0 |
| as modelled | 0.97–0.98 | 0.20–0.35 | 0 |
| frequent | 0.99 | 0.60–0.84 | 0 |

In target windows precision is a lower bound (0.73–0.95 at modelled or higher
error rates), because some "false" leads are the real wallet's own exits. Recall
follows the depositor's error rate, not the tool: a careful depositor is rarely
found. Count match alone has a precision of 0.03 on the real background.

### Bridge exits after Tornado (exploratory)

Do exits go on through a bridge within two hops? Measured with `tools/bridge_exits.py` on
the moderate/strong candidate exits of the cached hold-out 1 and 2 target runs
(Ethereum): 190 address-pool rows, 157 distinct addresses (no strong candidates exist in
either run). For each address the transactions it sent in the 30 days after its first
withdrawal were classified against `tornado_demix/data/bridges.csv` (55 Ethereum
addresses from Etherscan-verified contract names, including aggregators, plus 3
destination SpokePools); the recipients of plain transfers of at least 1 % of the
received amount (five largest) were read the same way. Calls that bring funds in (Polygon
`exit`, Wormhole redeem) are not counted. Numbers: `.cache/bridge_exits/RESULTS.md`.

| protocol | kind | exit addresses (n = 157 exits) |
|---|---|---:|
| Polygon PoS | contract | 9 |
| Across | contract | 3 |
| Relay | contract | 3 |
| Arbitrum | contract | 1 |
| Celer | contract | 1 |
| Hop | contract | 1 |
| Optimism | contract | 1 |
| Rango | aggregator | 1 |
| Socket | aggregator | 1 |
| THORChain | contract | 1 |
| Wormhole | contract | 1 |

22 of 157 addresses called a listed bridge or aggregator within two hops. Counts are
tiny: the Polygon PoS figure is 9 addresses, and the address
0xe1822d5939b8cfa1eb7412c3d53efbf8ccc0e076 appears in 3 pool rows and is counted once.
Bridge calls are not amount-attributed, so these are lower bounds and leads, not rates with
intervals. Unrecognised recipients are listed in RESULTS.md: 130 of 157 exits called some
contract outside the table, the most common being tokens and DEX routers (USDT, Uniswap,
USDC, WETH, the Tornado router); no further bridge appears among the top 15. The gate (a
protocol with at least 5 % of the exits that hit a bridge) passed. The trace continuation was
built for Across, not for the top protocol: the Polygon PoS deposit carries a state-sync id,
but the destination mint is a Bor state-sync transaction that the Etherscan-family APIs do
not index (no `StateCommitted` log, no receipt), so the only join left is by recipient, token
and amount, which is not an on-chain id. Across repeats the origin chain id and the deposit
id as indexed topics of the fill, and its destination chains are in the networks registry.
The extended table did not change which protocol is the top joinable one. Live check on one
real pair (Ethereum deposit 3888199, Arbitrum fill) in `tests/test_bridges.py -m live`.

### Bridge coverage of Tornado ETH-pool exits (exploratory)

How much of what leaves a Tornado ETH pool does the trace follow across a bridge, and how
much does it only name? `tools/bridge_coverage.py` draws a seeded random sample of distinct
recipients of ETH-pool withdrawals in a block range, runs the tool's own `trace_funds` over
the withdrawn amount for two hops (the code of the `trace` command, bridges followed across
chains where the trace follows them) and gives every recipient one class from the edges and
end states of that trace. This replaces an earlier ad hoc note on 198 recipients (69.2 % any
bridge, 38.4 % Arbitrum) that no script of the repository reproduced. A dry classification of
the first transactions would be cheaper but cannot tell a followed deposit from a named one,
since a bridge call that is not tied to the traced transfer is not followed; hence the full
trace.

Pilot: n = 60 distinct recipients, seed 20261114, Ethereum blocks 26,100,000 to 26,300,000,
run on 2026-11-14 with the 2.23 code on the Etherscan free tier (`--pause 0.4`), 105,267
Ethereum labels fetched the same day (`tornado-demix labels fetch`). Intervals are 95 %
Wilson intervals over the 60 recipients; recipients of one window are not independent, so
they understate the uncertainty.

| class (one per recipient) | recipients | share | 95 % CI |
|---|---:|---:|---|
| bridge followed to the other side | 20 | 33.3 % | 22.7-45.9 % |
| bridge or aggregator named, not followed | 21 | 35.0 % | 24.2-47.6 % |
| swap resolved (DEX, CoW Protocol) | 0 | 0.0 % | 0.0-6.0 % |
| ended at an exchange-labelled address | 0 | 0.0 % | 0.0-6.0 % |
| ended at another labelled address | 6 | 10.0 % | 4.7-20.1 % |
| sent into Tornado again | 1 | 1.7 % | 0.3-8.9 % |
| unknown contract (pays nothing back, in no table) | 3 | 5.0 % | 1.7-13.7 % |
| a lookup failed, nothing recognised | 0 | 0.0 % | 0.0-6.0 % |
| plain address, funds held, or hop limit | 9 | 15.0 % | 8.1-26.1 % |

Any bridge hop within two hops: 41 of 60 (68.3 %, 55.8-78.7 %). Per protocol (a recipient
counts once per protocol, so rows overlap):

| protocol | followed | named, not followed | share of recipients | 95 % CI |
|---|---:|---:|---:|---|
| Arbitrum | 19 | 0 | 31.7 % | 21.3-44.2 % |
| Layerswap | 0 | 7 | 11.7 % | 5.8-22.2 % |
| NEAR Intents | 0 | 6 | 10.0 % | 4.7-20.1 % |
| Relay | 0 | 4 | 6.7 % | 2.6-15.9 % |
| Mayan | 0 | 2 | 3.3 % | 0.9-11.4 % |
| Across | 1 | 0 | 1.7 % | 0.3-8.9 % |
| LI.FI | 0 | 1 | 1.7 % | 0.3-8.9 % |
| Optimism | 0 | 1 | 1.7 % | 0.3-8.9 % |
| THORChain | 0 | 1 | 1.7 % | 0.3-8.9 % |

Reading: in this window about a third of the recipients went on over a bridge the trace
follows (almost all of them Arbitrum retryables), and about as many over one it only names
(Layerswap, NEAR Intents, Relay, Mayan); the interval of each is wide. The six recipients
that ended at another labelled address ended at addresses with a defi label (four: Uniswap routers and a wrapped-ether
contract) and with a bridge label (two: a wallet's bridge router): the trace stops at a labelled address before it reads a swap or a
bridge, so these are recognised by their label only, not decoded. No recipient ended at an
exchange label, which at n = 60 says only that the share is below about 6 %. The sample is
one window, the labels are leads and not attribution, and a bridge the code does not
recognise falls under unknown contract or plain address, so the shares of bridges are lower
bounds. A hop is followed here only when the decoded call is tied to the traced transfer,
so "followed" is a statement about the decoder, not about how many funds reached the other
chain.

Without `--n` the tool draws 150 recipients (the default since the pilot; the pilot passed
`--n 60`). Full run: 300 recipients over a wider window, same seed.

```
python tools/bridge_coverage.py --n 300 --seed 20261114 --pause 0.4     --start-block 26000000 --end-block 26300000
```

The run is resumable (answers are kept in `.cache/bridge_coverage/per_recipient.json` and
the explorer responses in `.cache/bridge_coverage/http`), prints the tables above and writes
`RESULTS.md` and `summary.json` there. `--pause` keeps the free tier within its rate limit; a
run of 300 recipients makes about five times the calls of the pilot.

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

## Six further public incidents (2021-2025, exploratory)

Six incidents with named depositors, taken from the investigations collected at
github.com/tayvano/lazarus-bluenoroff-research (`hacks-and-thefts/`) and ZachXBT's
write-up of the 2021 consolidations. Default settings, 30-day window, `demix` per
depositor and `multi` over each incident's depositors. Not pre-registered, run once.

**What the exit lists are.** They come from investigators who matched withdrawals by
timing, amount and denomination and checked them against downstream links (shared
P2P deposit addresses, consolidation). A list built that way is not independent
ground truth for a tool that also matches amount and timing, so "found" would mean
agreement with those investigators, not validation. Only the Deribit depositors are
from a sworn document, the DOJ civil forfeiture complaint (D.D.C. 1:24-cv-02828,
filed 2024-10-04); the complaint does not list the 17 withdrawal addresses, and the
9 used here come from the investigators' file quoting it.

| Incident | Depositor | Known exits in the window | Candidates (all `weak`) | Known exits among them |
|---|---|---|---|---|
| Steadefi 2023 | `0xe10d4a5bd440775226c7e1858f573e379d0aca36` | 3 of 3 | 82 | 0 |
| Coinshift 2023 | `0x68c4a151d436ec1c5448d225a97bd19cce4dfed0` | 2 of 2 | 0 | 0 |
| Deribit 2022 | `0xb0606f433496bf66338b8ad6b6d51fc4d84a44cd` | 9 of 9 | 0 | 0 |
| Deribit 2022 | `0x3089df0e2349faea1c8ec4a08593c137da10fe2d` | 7 of 9 | 0 | 0 |
| CoinMetro 2021 | `0x0864b5ef4d8086cd0062306f39adea5da5bd2603` | 1 of 1 | 1 | 0 |
| CoinMetro 2021 | `0x1031ffaf5d00c6bc1ee0978eb7ec196b1d164129` | 1 of 1 | 80 | 0 |
| bZx 2021 | `0x20d9e73aafe69a3cd66ad7477de7358d0ed44b10` | 3 of 3 | 20 | 0 |
| Garden Finance 2025 | `0x95f0564879d5e33000d53107adaea6809aaa3838` | 23 of 28 | 57 | 0 |
| Garden Finance 2025 | `0xa9a166febace09fd620a3b85cbc6d40e535fbdf1` | 10 of 28 | 175 | 1 |

Sources (all in `hacks-and-thefts/`): `steadefi.md` (3 exits; the depositor also
appears in UN Panel of Experts report S/2024/215), `coinshift.md` (2 exits, the same
addresses as two of the Steadefi exits), `deribit.md` (9 of the 17 listed exits;
depositors from the complaint), `coinmetro.md` (exit
`0x05492cbc8fb228103744ecca0df62473b2858810`, 45 x 100 ETH), `bZx.md` (3 exits, called a
partial demix by the source), `garden-finance.md` (28 exits; third depositor
`0x7c1cf2f256d2e83dee5ae15e7ac0f7f6197e780b`, no method stated by the source).
Base URL: `https://github.com/tayvano/lazarus-bluenoroff-research/blob/main/hacks-and-thefts/`.

The one known exit that was picked is
`0x3653904f32679b1f3bdcff3e8da596d0a97991f1` (Garden Finance, second depositor): a
`weak` count match of 3 x 1 ETH, rank 29 of 175, i.e. chance level. In every other
case no known exit was a candidate. The known exits nearly always lie in the searched
window, but the number of withdrawals each received does not equal a voucher size:
they collect notes of several depositors, or the notes are split across exits, which
is the same limit as in the two cases above.

`multi`: for Garden Finance the funder
`0x98bcc6c34a489cefdd9dfa8d792cfefb02ea2d12` is shared by `0x7c1cf2f256d2e83dee5ae15e7ac0f7f6197e780b` and
`0x95f0564879d5e33000d53107adaea6809aaa3838` (the source lists it as "Direct Theft"); none of the source's exits is
among the 36 shared count-matched addresses. For CoinMetro the known exit appears among
the 4 non-exact profile matches of the first depositor (45 withdrawals against a
30-note fingerprint, a `>=` match, not a lead), next to three other addresses. For
Steadefi with Coinshift and for Deribit there is no shared funder and no known exit in
the profile matches.

## Errata (2.22)

Dated 2026-11-13. Defects found in the code before version 2.22 and corrections to numbers
and wording of this document, of [HOLDOUT_RESULTS.md](HOLDOUT_RESULTS.md) and of
[HOLDOUT2_RESULTS.md](HOLDOUT2_RESULTS.md) (frozen files are not edited; their corrections are
here). The cached runs were not re-run for this section. "Re-run planned" means that the
affected hold-out, placebo and bridge runs are repeated with the 2.22 code and the new
numbers are published beside the old ones; until then the old numbers stand as
measured with the older code.

| Published number | Affected by | Status |
|---|---|---|
| Every count or rate computed from explorer lists: placebo test, early profile, hold-outs 1 and 2, ENS-labelled set, the two public cases, bridge-exit tables | Explorer pagination: before 2.22 a list ended at the first page shorter than the page size, and the explorer can return a short page in the middle of a result set, so rows could be missing on the target and on the decoy side | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one; not re-run: the ENS-labelled set, the two public cases and the bridge-exit tables |
| Synthetic benchmark | Not computed from explorer data | Unaffected; tables regenerated, see [Synthetic benchmark](#results) |
| `linked` counts (this document, hold-outs 1 and 2) | Token and native rows: a spoofed or zero-value token transfer, or a zero-value or failed native transaction, could make an address a direct counterparty before 2.22 | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one |
| Shared-deposit counts and sender numbers (rule 2.21 section: 18 / 30 and 23 / 13 at 30 days, 14 / 2 and 23 / 0 at 72 hours; 31, 35 and 18 senders) | Classifier defects fixed in 2.22: EIP-7702 sponsor sweeps dropped as poisoning, 50-sender cap (300 since 2.22 with a sweep-share condition), per-network gas-funder threshold, tag classes of Blockscout | Re-run on 2.23.0: 56 / 35, 58 / 8 at 30 days; 46 / 3, 45 / 1 at 72 hours (context since 2.21). The note in HOLDOUT2_RESULTS.md that the rule accepts up to 50 senders describes the rule of that run |
| Polygon address that sweeps to an unnamed target (Deposit-address kinds, 2.21) | Classifier | Changed: it is `deposit_like` since 2.22, not `exchange_deposit` |
| Any figure that treated a failed lookup as "nothing found" (deposit-address, busy, contract and first-activity checks, trace ends) | Before 2.22 a failed lookup looked like an empty answer; runs now carry a list of failed lookups and say that results may be incomplete | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one |
| Merkle-root exclusion numbers (5,165 of 5,165 roots found, exclusions in real and decoy windows) | `tools/root_exclusion_eval.py` takes the calldata root with the unvalidated unique-candidate read (`withdraw_root.root_from_input`: the root when exactly one candidate precedes the nullifier), not the read of `demix --root-check`, which keeps only a candidate that the pool still accepted at the withdrawal block (history size read from the contract) and leaves zero or several as unknown | The published numbers come from the unvalidated read; the validated read can classify some withdrawals as unknown instead. Unknown how many; the tool was not changed in 2.22 (changed in 2.23, see Errata (2.23)). Re-run on 2.23.0 with the validated read: 4,769 of 4,769 roots found; withdrawals excluded as before (Ethereum 0 real / 1 decoy, Polygon 0 / 0). |
| Root ceiling (11.01 % Ethereum, 10.94 % Polygon of withdrawals) | Assumed one history size per chain (100 and 30); 8 of 28 Ethereum pools keep 30 | Re-run on 2.23.0 with the per-pool read: unchanged (11.01 %, 10.94 %); every ETH pool 100, every MATIC pool 30. |
| Bridge-exit table (22 of 157 exits, Polygon PoS the largest) | Measured on cached hold-out exits before Arbitrum retryables and ZKsync recipients were decoded; the order of bridges differs on recent exits (see [Bridge coverage](#bridge-coverage-of-tornado-eth-pool-exits-exploratory)) | Superseded for the order of bridges by the 2.23 pilot (exploratory); both are small samples |
| "Chance by construction" for decoy leads (placebo test, reports) | Wording: a decoy window can contain the depositor's own earlier activity | Wording corrected; numbers unchanged |
| "One minus R estimates precision" (`tools/placebo_eval.py`, field `precision_estimate`) | Wording: R is a ratio of decoy to target leads, not a precision | Removed; no published number used it |
| Rule 2.21 (shared deposit address is context) | Post-hoc: decided after the second hold-out, whose frozen text says the rule is not revised on these data | Wording corrected in the Rule 2.21 section; the change is untested on new data |
| Early-profile thresholds (at least 10 notes, two or more pools, 72 hours) | Chosen on a universe that includes depositors later drawn by the hold-outs | Stated in the Early multi-pool profile section; hold-outs are not independent for the choice of these thresholds |
| Synthetic benchmark tables | Published tables came from earlier versions | Regenerated with 2.21.0 (identical output on the 2.22 tree); differences listed in [Results](#results) |
| "Supported" verdicts in HOLDOUT_RESULTS.md and HOLDOUT2_RESULTS.md | No `docs/PROTOCOL.lock` existed when they were written | Exploratory by the register's definition (a result counts as confirmatory only under a lock); the numbers stand as measured, the word "supported" does not make them confirmatory |

Corrections to individual statements:

- HOLDOUT2_RESULTS.md: "the rule accepts up to 50 senders (`MAX_DEPOSIT_SENDERS`)" describes the
  rule of that run; since 2.22 the cap is 300 when at least 95 % of the address's outgoing
  transfers are sweeps, and 50 otherwise (80 % share).
- HOLDOUT2_RESULTS.md: "as registered, the rule is not revised on these data" is the
  registered procedure. Rule 2.21 was decided after the numbers were seen (post-hoc, see the
  Rule 2.21 section).
- Deposit-address kinds (2.21): the Polygon address that sweeps to an unnamed target is
  `deposit_like`, not `exchange_deposit`.

## Errata (2.23)

Dated 2026-11-14. Defects found in the code of 2.22 and their effect on numbers published
earlier, in one table (frozen files are not edited; their corrections are here). The cached runs
were not re-run for this section. Most of them were re-run on 2.23.0 on 2026-11-16/17: see
[Re-run on 2.23.0](#re-run-on-2230-2026-111617). Where a status says "re-run planned", that run was
not repeated; the old numbers stand as measured with the older code.

| Published number | Affected by | Status |
|---|---|---|
| `linked` counts and every lead count built on them (this document: placebo test, direct links early versus late, power check, ENS-labelled set; hold-outs 1 and 2) | Before 2.23 a direct link did not have to move real value or come before the withdrawal: a zero-value or 1-wei transaction, one sent after the withdrawal, a token row of an unlisted token inside the depositor's own transaction, or a counterparty whose contract check failed could make `linked`. Since 2.23 such links are context (`linked_dust`, `linked_posthoc`, `linked_unverified_token`, `linked_unknown_contract`) | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one; not re-run: the ENS-labelled set. Cached runs carry no link records, so tools that re-score them without a fresh run still apply the old rule; those tools print how many runs they scored with the pre-2.23 rule |
| Merkle-root exclusion numbers (5,165 of 5,165 roots found; exclusions in real and decoy windows; Merkle-root exclusion section) | Before 2.23 the root was not taken from the decoded `withdraw` call: `tools/root_exclusion_eval.py` used the 32-byte word before the nullifier when only one such word existed, without checking that the pool still accepted it at the withdrawal block, and `demix --root-check` checked every such word against the root history. A wrapper can place an older root right before the nullifier in a call that is not the withdrawal (a Multicall no-op, or a contract that builds the real call itself), and that older root could exclude a withdrawal it did not prove. Since 2.23 the root is the `_root` argument of the decoded `withdraw` call to the pool (directly, through a declared router or inside a Multicall wrapper); without one, or when the call sits in a batch frame that lets it fail silently (including any wrapper not at a known Multicall address), the root is unknown and nothing is excluded. The evaluation tool now uses this read, accepts a root only when the pool still accepted it at the withdrawal block, with each pool's `ROOT_HISTORY_SIZE` read from its contract, and keeps its decoded roots in a separate cache (`roots_v2_<network>.json`) | The published numbers were measured with the old assumptions. Re-run on 2.23.0 with the decoded and validated read: 4,769 of 4,769 roots found; withdrawals excluded as before (Ethereum 0 real / 1 decoy, Polygon 0 / 0). |
| Root ceiling (11.01 % Ethereum, 10.94 % Polygon of withdrawals) | `tools/root_ceiling.py` took one history size per run (`--history 100` for Ethereum, `30` for Polygon). Since 2.23 it reads `ROOT_HISTORY_SIZE` from each pool contract (cached in `.cache/merkle`), leaves out a pool whose size cannot be read and reports the size per pool | The published figures used the old assumption. The four Ethereum ETH pools of the published run list 100 in their verified source. Re-run on 2.23.0 with the per-pool read: unchanged (11.01 %, 10.94 %); every ETH pool 100, every MATIC pool 30. |
| Test register records written before 2.23 | A record carried the SHA-256 of its script only, so a change in library code was not visible in it, and a full rewrite of the chain from its first line verified clean. Since 2.23 every record carries the SHA-256 of each loaded `tornado_demix` module, a confirmatory status needs those modules in the lock too, and `verify_chain` / `registry_report.py --check-witness` check one or more external witnesses | No status changes: no lock existed, every record was and is exploratory. Older records have no `modules` field. A full rewrite stays undetectable without a witness published outside the repository (see Test register) |
| Power check (planted withdrawal) | The planted withdrawal carried a hash that was not hexadecimal (`PLANT_HASH`) | Now a valid 64-hex-digit constant. No published number depended on the hash |
| Counts that include deposits through a router on a network with twin pools (Avalanche 10 and 100 AVAX through the routers `0x171f…` and `0x0d55…`; the early multi-pool profile; Merkle-root exclusion notes) | Before 2.23 a router deposit was assigned to the first pool of its denomination, so it could land on the wrong twin. Since 2.23 the pool is the contract that emitted the `Deposit` event in the receipt, on every network; a deposit that cannot be resolved is recorded as `pool_ambiguous`, is not searched and is left out of the profile, the early multi-pool profile is not applied while any deposit is `pool_ambiguous`, the root check places such a deposit by its leaf in each twin's tree or leaves the pools it may belong to unchecked, and the report names it instead of saying that there are no deposits | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one. |
| Runs that needed the JSON-RPC log fallback on Polygon, Arbitrum or Optimism | Before 2.23 the providers' own range and archive errors were not recognised, so such a run stopped with an API error after its retries. Since 2.23 the range is halved or the run says that the fallback is unavailable for historical blocks, and a Cloudflare challenge is reported as `provider_blocked` (never bypassed) | No published number is known to depend on the fallback on these networks: an affected run failed, it did not return other numbers. Not checked for every cached run |
| Bridge-exit table and every count of where traced funds ended (22 of 157 exits; the ad hoc sample of 198 exits of 2026-11-13, shares 69.2 % any bridge and 38.4 % Arbitrum, which was not run by a script of the repository and is replaced by the pilot of the Bridge coverage section) | Before 2.23 token gateways, Circle CCTP, CoW Protocol orders and the named bridges (Relay, Mayan, THORChain, Maya, Layerswap, NEAR Intents, Unit, Hyperliquid) were not decoded, so such a hop ended as an unlabelled contract. Since 2.23 a hop is followed only when the decoded call is tied to the traced transfer and each deposit is consumed once; otherwise the branch ends with a named state and the recipient is shown | The published tables use the older decoding; the order and the shares of end states can change. The pilot of the Bridge coverage section is the first measurement with the 2.23 decoding (60 recipients); a full run is documented there. Both samples are small and exploratory |
| Page 1 of the report: third figure | Wording: the figure was read as attribution although it is the total received by the candidate addresses from all sources | Wording and layout changed (the depositor's deposited total is shown beside it); no published number depended on it |
| Wording that calls the two lead sources of `strong` "independent" in the frozen PREREG_HOLDOUT.md and PREREG_HOLDOUT2.md | Independence of the two lead families was assumed, not established (see the lead-source discussion in this document) | Read as "two different lead-signal families; their independence is not established". The rules and the registered numbers are unchanged; the frozen files are not edited |
| Deposit-address kinds and the sweep counts (`exchange_deposit`, `shared_deposit`; ENS-labelled set, hold-outs) | Before 2.23 the deposit-address classifier counted failed transactions (`isError`, `txreceipt_status`) as moved value, so a failed attempt to send to a labelled exchange could make an address an exchange sweep. Since 2.23 failed transactions are kept apart (`failed_sends`) and not counted | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one; not re-run: the ENS-labelled set. A shared deposit address does not raise the demix band, so band counts are not known to move |
| `early_profile` lead counts (early multi-pool profile section; hold-outs; placebo test) | Before 2.23 the profile counted only the withdrawals already loaded, so a short exit window, a cut-off or partial list, a hole in the data or a deposit younger than 72 hours at read time could produce a profile (and a `moderate` lead) that the full 72-hour history does not give. Since 2.23 the profile is not applied unless the data cover [first deposit, last deposit + 72 h] | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one. Published figures used windows of 72 hours or more on complete lists as far as recorded; not checked run by run |
| Exchange deposit-address counts (ENS-labelled set; hold-out shared-deposit rows) | Before 2.23 a label alone made a target an exchange sweep: an address that paid a labelled exchange once was `exchange_deposit`. Since 2.23 a labelled target needs forwarding of the received value or a balance sweep (at least 90 % within 30 days); otherwise the context kind is `paid_exchange` ("the user paid an exchange", not "the exchange runs the address") | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one; not re-run: the ENS-labelled set |
| Lists that end in a Blockscout `status=2` (partial indexing) | Before 2.23 the client accepted the available rows without recording that the list was incomplete, so a run could look complete. Since 2.23 such a lookup is recorded as incomplete (`partial_indexing`) in CLI, JSON, HTML and CSV, and the result is not cached or called certain | Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one. Cached runs carry no such record |
| Trace results where an outflow and an inflow share one transaction (bridge-exit and end-state counts; trace tables) | Before 2.23 all inflows of a transaction were placed before all outflows, and `transactionIndex` stood in for the order of internal events, so an outflow could spend value that arrived later in the same transaction. Since 2.23 the order inside one transaction comes from `traceId` (internal), `logIndex` (ERC-20) or the Blockscout index; an outflow spends a same-transaction inflow only when it is known to follow it, otherwise the state is `order_unknown` ("order inside one transaction unknown") and a multi-inflow arrival is `ambiguous_in_tx` | Unknown (magnitude not measured); re-run planned. Not known to affect a published number except through such transactions |
| Wording "the register proves pre-registration", "chance alone" for decoy leads, "indistinguishable from chance" and the claim that the two lead families are independent, in tools, docs and the web UI | Wording only: the hash chain is relative to an external witness, a decoy window can hold the depositor's own earlier activity, no equivalence test is run, and independence of the two lead-signal families is not established. A web UI run slot limit also held for background runs only; since 2.23 synchronous and background runs share one slot. The report also names the limitation that an unsolicited receipt can create a lead | Wording corrected, one run slot; no published number depended on it |
| Exact and bootstrap intervals of the hold-out ratio R (`tools/holdout_report.py`, `tools/placebo_eval.py`; hold-outs 1 and 2, polygon, placebo test) | The exact interval assumes independent withdrawals and the bootstrap drops resamples with undefined R without saying how many. When the leads of a side come from a few depositors both are over-confident: in a synthetic case of 30 depositors with 100 target leads in one of them and no decoy leads, the exact interval was [0, 0.038] and the bootstrap [0, 0] after discarding every resample without that depositor. Since 2.23 the tools print the depositors carrying leads per side, the largest single depositor's share, the number of dropped replicates and the label "assumes independent withdrawals", and print "insufficient information (leads concentrated in N depositor(s))" instead of a reliable interval when a side has leads from fewer than 3 depositors or one depositor holds more than 50 % | The intervals of the published runs were not recomputed (per-depositor lead counts are in the private caches); which of them are concentrated is unknown. Re-run 2026-11-16/17 on 2.23.0 (fresh): see [Re-run on 2.23.0](#re-run-on-2230-2026-111617); the effects are not separated one by one |

Three defects were found and fixed during the 2.23 work itself, before its release; no released
version had them, so no published number depends on them: a merged member wallet of a group view
that is a contract could become a direct link (`linked`; in 2.22 and again now it does not, and a
member whose contract check failed is `linked_unknown_contract`, context); the root check could
exclude a withdrawal because a deposit with an undecided twin pool was left out of the pool's notes
(2.22 left that pool unchecked; now the deposit is placed by its leaf or the pool stays unchecked);
and the early multi-pool profile could fire on the notes that remained after such a deposit was left
out (2.22 counted every deposit, in the first twin; now the profile is not applied while a pool is
undecided).

Corrections to individual statements:

- README: the label set that `labels fetch` returns is 105,267 Ethereum addresses, 32,943 of them
  exchange labels (131,117 rows on the eight networks, fetched 2026-11-14); "about 88,000 / 26,000" was out of date.
  "About 115,000 addresses on 47 networks" for the whole wallet-attribution dataset was not re-checked and is no longer stated.
- Checks table below: the `pytest` count was the 2.22 figure; it is the count of the 2.23 tree, and the
  `pip-audit` row carries its date. The lock pinned `certifi` 2026.7.22, which is affected by PYSEC-2026-4320;
  it pins 2026.10.23 since 2.23 and CI runs `pip-audit` on the lock.
- SECURITY.md: results of the web UI are keyed to the session cookie, another session does not read them;
  the risk of an exposed port is spent API quota and denial of service. The text said "read the last run's results".
- Deposit-address senders (defect present in 2.22): the text said that a dust spoof or a gas
  top-up does not make a sender, but an incoming transfer of any non-zero amount counted when the
  address later swept that asset, so swept dust could add senders (and push an address over the
  300-sender cap). Since 2.23 a sender's transfer must reach the dust floor of its asset (the
  floors of the direct-link rule). `shared_deposit` is context with no weight, so no band or lead
  count changes; the published `shared_deposit` counts of the hold-outs were measured with the
  2.22 rule and were not re-run.
- Withdrawal senders (defect present in 2.22): the sender lookup read the wallet's zero-value
  and failed incoming rows too, so spam rows could fill its 25 slots; since 2.23 they are
  ignored. `linked_sender` is context, no band changes.
- CSV export: cells that start with a control character or a byte-order mark, or with spaces before a
  formula character, are now escaped as well (`=`, `+`, `-`, `@`, tab and carriage return were before).
- Trace, arrival index (defect present in 2.22): the start address's arrival was located once,
  before the first replay. When a history read at a later level gave one of the start address's
  unindexed rows its transaction index (or moved a block's timestamp), the start history was
  re-sorted but the replay still credited the traced amount at the old position; if that
  position held an outgoing transfer the credit was never applied and the whole traced amount
  disappeared (no edges and no end points, so the end amounts no longer added up to the start
  amount). Rare: 49 of 12,000 random test graphs built to trigger re-sorts; no published
  number is known to come from such a run, since an affected trace shows no result at all. Since
  2.23 the arrival is located again before every replay.
- Trace, Across: before 2.23 a contract depositing into Across in its own name, within 10 % below the
  traced transfer of the same token, was taken whenever the traced address sent the transaction, even
  when the contract also received another user's funds in it. Since 2.23 the traced address must have
  called that contract directly and no other inflow of the token may reach it in the call; otherwise
  the branch ends as "deposit not tied". At a listed aggregator, a native payment back within the value
  the sender attached to the transaction was read as a swap output; since 2.23 it is a refund, and
  without a tied deposit or a bridge event the branch ends as "aggregator <protocol> (bridge or swap
  unknown)". Published trace end-state counts belong to the bridge-exit row above (re-run planned).

Planned with the re-run (not part of the 2.23 rule). The dust floor prices a planted link but
does not stop one: whoever controls the depositor can send the floor (about 0.001 ETH worth)
to any address right after depositing. Once the runs carry link records, the current `linked`
leads will be tallied by direction (incoming, which needs the counterparty's key, against
outgoing), by time (before the voucher's first deposit, or between the deposit and the
withdrawal) and by value, in target and decoy windows. A tiered rule (an incoming link, or an
outgoing one made before the deposit, stays a lead; an outgoing-only link made after the
deposit needs a higher floor, for example 0.01 ETH worth) is the candidate, to be registered
and tested on a hold-out before it replaces the 2.23 rule.

## Re-run on 2.23.0 (2026-11-16/17)

The samples of the placebo test (152 and 150 post-2022 depositors) and of both hold-outs were run
again with 2.23.0, fresh from the explorer, on exactly the frozen depositors (sample hashes as
registered; the 10 depositors that failed in October completed). Every run carries link records, so
the 2.23 direct-link rule applies; the bands are those of 2.23 (`linked_sender` and
`shared_deposit` are context). The old tables stay as measured; this section replaces them as the
current numbers. Exploratory: no protocol lock, and the rule was not frozen before these runs.

| | Ethereum hold-out 1 (600) | Polygon 1 (300) | Ethereum hold-out 2 (600) | Polygon 2 (400) |
|---|---|---|---|---|
| early direct link, 30 d | 31 / 2; R 0.069 (0.008-0.27)† | 14 / 1; 0.071 (0.002-0.47)† | 21 / 3; 0.15 (0.03-0.51) | 38 / 4; 0.11 (0.03-0.30) |
| late direct link (context), 30 d | 18 / 33; 1.95 (1.07-3.86) | 12 / 29; 2.41 (1.09-5.64) | 21 / 50; 2.53 (1.51-4.63) | 25 / 22; 0.90 (0.48-1.59) |
| labelled shared deposit (context), 30 d | 38 / 6; 0.17 (0.02-0.39) | 9 / 0; 0 (0-0.51) | 56 / 35; 0.66 (0.11-1.49)† | 58 / 8; 0.14 (0.02-0.36) |
| early profile, 30 d | 11 / 0; 0 (0-0.42) | 6 / 2; 0.33† | 10 / 0; 0 (0-0.47) | 11 / 0; 0 (0-0.41) |
| count match (context), 30 d | 0.98 (0.92-1.05) | 1.02 (0.94-1.09) | 1.06 (1.007-1.117) | 0.98 (0.92-1.07) |
| moderate, 72 h | 42 / 2; 0.056 (0.007-0.22)† | 13 / 1; 0.086 (0.002-0.58)† | 29 / 3; 0.12 (0.02-0.37) | 38 / 4; 0.12 (0.03-0.33) |
| moderate, 30 d | 42 / 2; 0.051 (0.006-0.20)† | 20 / 3; 0.15 (0.03-0.50)† | 31 / 3; 0.10 (0.02-0.33) | 49 / 4; 0.083 (0.02-0.23) |
| strong | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

† decoy leads from fewer than 3 depositors or one depositor holding more than half: the tool prints
"insufficient information"; the interval is shown but not reliable.

Placebo samples (302 depositors, 2.23): early direct link 11 / 1 at 30 days (R 0.10†), late
direct link 6 / 11, `linked_sender` 8 / 2, count match 9,077 / 8,357 (R 1.00, 0.92-1.08),
`moderate` 13 / 1 at 72 hours and at 30 days, no `strong`.

What changed. The early direct link lost 58-76 % of its target leads in every sample. Most of them are
now `linked_posthoc` (a real-value link not known to precede the candidate's first withdrawal):
74 / 3, 38 / 3, 70 / 3 and 57 / 3 target / decoy in the four hold-outs, so they were mostly not chance
links. On Polygon many are `linked_dust` (27 / 8, 20 / 3). Re-scoring the old runs with the 2.23 tools gives the
published counts, so the change comes from the fresh runs and their link records, not from the scorer.
`linked_sender`, `early_profile` and gas-price counts did not change. Labelled shared-deposit hits rose
(12 to 38, 5 to 9, 18 to 56, 23 to 58 target leads), after the classifier fixes of 2.22 and 2.23.
They remain context.

Merkle-root exclusion (hold-out 2, decoded and validated root): 4,769 of 4,769 roots found; nothing
excluded in real windows; one weak Ethereum decoy withdrawal partly excluded; no candidate fully
excluded. Root ceiling with ROOT_HISTORY_SIZE read per pool (100 for the four ETH pools, 30 for the
three MATIC pools): 11.01 % and 10.94 %, as published.

Links that are now context (`linked_posthoc`, 239 target against 12 decoy over the four hold-outs)
still separate target from decoy windows in these samples. They are shown as evidence and are not a
lead: using them would be a new rule, which needs registration and a hold-out first, like the
tiered rule planned in Errata (2.23).

Same-time decoy (real windows, a pool the depositor never used; 553 and 560 depositors): early direct
link 30 / 4, R 0.14 (0.04-0.40) and 18 / 4, R 0.25 (0.06-0.75); `moderate` 32 / 4, R 0.13 (0.03-0.37)
and 22 / 6, R 0.30 (0.05-0.73); per-wallet decoy rate 0.7 % (upper 1.8 %) and 0.9 % (2.1 %). The early
direct link is above chance under both decoy designs. Its ratio is higher with the same-time decoy.

Detectability (planted early direct link in each decoy window, hold-out 2): not detectable in 59 of
600 Ethereum windows (9.83 %) and 145 of 400 Polygon windows (36.25 %), against 2.34 % and 7.75 %
before 2.23. Most of the new cases (41 and 113) have counterparties, but none that moved real value
before the planted withdrawal at the decoy's shifted time (`no_qualifying_link`). On Polygon more than a third of the wallets can
therefore not show a direct link in the decoy window. Dropping these wallets from both sides leaves
the decoy rates nearly unchanged (moderate R 0.109 and 0.074 without them). Whether the shifted decoy
under-counts chance direct links for this reason was not tested.

Shared deposit address on the 1,000-depositor universe (same depositors as before): labelled
55 / 16, R 0.31 (0.14-0.51), decoy hits from 13 depositors (before 27 / 4, R 0.16); no labels
67 / 35, R 0.56 (0.32-0.93) (before 16 / 12, R 0.80). The 2.22/2.23 classifier finds about twice as
many addresses. On Ethereum hold-out 2 the labelled shared deposit separates at 72 hours (46 / 3, decoy leads from 2
depositors: insufficient information) but not at 30 days with the shifted decoy (56 / 35, R 0.66†);
with the same-time decoy it gives 49 / 6, R 0.14 (0.05-0.25) at 30 days. Whether it separates depends
on the window and on the decoy design. It stays context.

Not re-run: ENS-labelled set and the Wang et al. protocol (private pairs), the Tutela baseline,
bridge tables and the case studies. The deposit/withdrawal universes were not rebuilt with the
paging fix.

## Reproducing the published numbers

What a third party can run, and what depends on caches that are not in the repository
(`.cache/` is not published). Install with `python -m pip install -e ".[dev,eval]"` (the
`eval` extra adds numpy, scipy and statsmodels, used by the hold-out and baseline tools);
`requirements-lock.txt` pins the versions of the tests. The tests pass from any working
directory.

| Numbers | How | Needs | Reproducible? |
|---|---|---|---|
| Synthetic benchmark (all tables of that section) | `python tools/simulate.py --experiment all --trials 200 --seed 1` | nothing (offline, about a minute) | Yes, exactly; the output is deterministic for a given version |
| Semi-synthetic benchmark | `python tools/semisynthetic.py` | cached real runs from the placebo test | No, depends on private caches |
| Placebo test (304 runs), early profile, hold-outs 1 and 2, Tutela / Beres baseline, decoy rates | `tools/placebo_eval.py` with the seeds and exclusion lists of [PREREG_HOLDOUT.md](PREREG_HOLDOUT.md) and [PREREG_HOLDOUT2.md](PREREG_HOLDOUT2.md); `tools/holdout_report.py` per sample | Explorer API key; the ENS universe and exclusion list (private; their SHA-256 is in the registrations); target windows complete before 2026-08-01 | Partly: the procedure is registered and the sample hashes are published, but the draw depends on the private universe and the cached runs. A re-run today also reads explorer data as it is now |
| Rule re-scoring on cached runs (`tools/placebo_windows.py`, `tools/strong_eval_*.py`, `tools/supplementary_s1.py`) | Offline on `.cache/labels/placebo` | The cached runs | No, private caches |
| Merkle-root exclusion and root ceiling | `tools/root_exclusion_eval.py`, `tools/root_ceiling.py` | Cached hold-out 2 runs and `.cache/merkle` (the tree index can be rebuilt from pool logs with an API key) | Partly |
| Bridge-exit table (22 of 157 exits) | `tools/bridge_exits.py` | Cached hold-out exits | No, depends on private caches |
| Bridge coverage of recent exits | `python tools/bridge_coverage.py --n 60 --seed 20261114 --pause 0.4` (pilot); full run in the Bridge coverage section | Explorer API key; a fetched label set for the exchange row | Yes in principle: the draw is fixed by the seed and the block range, the explorer data and the labels are read as they are today |
| Two public cases (KuCoin, Harmony) | `python tools/real_cases.py` | Explorer API key; public addresses | Yes in principle; the explorer data changes, and runs before 2.22 are affected by the pagination defect |
| ENS-labelled set and the Wang et al. protocol | `tools/ens_labels.py`, `tools/evaluate_labels.py`, `tools/wang_baseline.py` | Explorer key; names are read as they are today | No: the set moves with the ENS records |
| Test register, tests, linter | `python -m pytest -q`, `ruff check .`, `ruff format --check .` | `.[dev]` | Yes |

Data cut dates: the hold-out target windows end before 2026-08-01 (registrations); the placebo
test windows end before August 2026; the bridge coverage pilot covers Ethereum blocks 26,100,000 to
26,300,000.

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
| Pairs found (of 31; 21 inside the window) | 2 | 12 (57 % of those in the window) | 16 |
| Precision, lower bound | 2 of 2 | 12 of 28 (43 %) | 16 of 2,811 |
| Pairs found without the `linked` signal | 0 | 2 | 3 |

(Version 2.17. Four labelled pairs fall to `weak` because their direct link came
later than 72 hours; under 2.16 the column read 16 found, 16 of 40 (40 %). Under
the band rule before 2.13, where a gas-price match or a self-relayed count
match alone reached `moderate`, the lower bound was 16 of 85, 19 %. Before 2.16,
`strong` was a lead signal plus any other family: 1 pair found, 1 of 4 candidates.)

All 16 pairs are found through a direct transaction between the depositor and
the recipient (`linked`). The ENS link and `linked` see the same relationship,
so this confirms that careless users also transact directly; it does not
measure the other signals. Two of the 16 pairs are also found by `shared_deposit`
(depositor and recipient sent funds to the same deposit address of a
labelled exchange); with the direct link that is two lead-signal families, so these two
are the `strong` pairs, and without `linked` they stay `moderate` (measured under 2.17; since 2.21 the shared deposit is context, so they are `moderate`). Otherwise the other
signals find almost nothing here, for a reason the
method states in advance: 24 of the 31 pairs come from depositors whose only
voucher in that pool is a single note, where the count match cannot narrow the
field (discrimination 0.12-0.33 against the 0.5 threshold), and a gas-price
match is rarely checkable after EIP-1559. The count match fires for one pair only
(a two-note voucher); with a direct link that is one lead source, so that pair is
`moderate` (before 2.16 it was `strong`). Ten pairs fall outside
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
| demix, up to `moderate` | 1.00 | 0.41 | 0.59 |
| demix without `linked` | 1.00 | 0.07 | 0.13 |

A direct-transfer check alone, with no mixer analysis at all, scores 0.96,
because 27 of the 29 pairs transacted directly. Labels built from address links
(ENS, airdrop aggregation) measure whether two addresses ever met, not whether a
withdrawal spends a deposit; this holds for the F1 of 0.55 in Wang et al. as much
as for the numbers here. demix recalls less than H3 because it only looks at
withdrawals in the 30-day window after the deposit.

## Do the signals move together?

On the placebo runs under the current signal set (302 depositors, 30-day window:
283,383 recipient-pool rows in target windows, 254,263 in decoy windows;
`tools/strong_eval_analyze.py`), only one pair has enough expected co-occurrences to
test: count match and self-relay (555 together against 252 expected in target
windows, 526 against 263 in decoy windows; phi 0.037 and 0.033). Both read the same
withdrawals and belong to one family, which is why the score takes only the larger
weight. Every other pair has an expected count below one. Cross-source
co-occurrences of lead signals appear only in target windows (twice: early direct
link plus shared deposit) and never in decoy windows. The early profile coincides
with a count match by construction (2 against 0.07), but a count match is not a
lead source. Independence of the lead sources is therefore an assumption of the
model, not a measurement.

(An earlier check on the ENS, KuCoin and Harmony runs, made before the
shared-deposit and early-profile signals and the gas-price restriction, found every
pair within three co-occurrences of independence on 43,424 rows.)

## Test register

The seven evaluation tools wired to the register (`tools/holdout_report.py`, `tools/decoy_rates.py`,
`tools/placebo_eval.py`, `tools/power_measure.py`, `tools/root_exclusion_eval.py`,
`tools/root_ceiling.py`, `tools/bridge_exits.py`) append every statistical test they compute (hold-out ratios, decoy rates, placebo
FDR, detectability, root exclusion, root ceiling, bridge-exit gate) as one JSON line to
`.cache/registry/tests.jsonl` (override with `TORNADO_DEMIX_REGISTRY`); other tools do not write to it. Each line carries
the SHA-256 of the script that produced it and the hash of the previous line, so an edited
or deleted line is detected. `python tools/registry_report.py` verifies the chain and counts
tests per family, per script and per status; the count, exploratory sweeps included, is the
number of comparisons a multiple-testing correction has to account for.

A record is `confirmatory` only when `docs/PROTOCOL.lock` lists the SHA-256 of its script
and of the library modules it ran with (`registry_report.py --lock SCRIPT... --out
docs/PROTOCOL.lock`); with no lock file every
record is `exploratory`. No lock exists yet: nothing is registered as confirmatory until a
hold-out 3 is. The default lock is looked up from the register's location
(`<project>/docs/PROTOCOL.lock` for `<project>/.cache/registry/`), not from the working
directory. The register cannot show from its own content that its tail was cut off, nor that
the whole chain was rewritten from its first line with every hash recomputed: only an
external anchor shows either. Keep a witness (`registry_report.py --witness OUT`, last seq
and hash) outside the register directory, publish it where the writer of the register cannot
change it (a release asset, a Zenodo record, an OpenTimestamps proof of the witness file) and
check against it, or against several, with `registry_report.py --check-witness FILE...`.
Since 2.23 every record also carries the SHA-256 of each `tornado_demix` module loaded when
it was written (`modules`); `registry_report.py --lock` lists every package module, tagged
`[module]`, besides the scripts, and a record is confirmatory only when its script and all its
modules are in the lock. The lock is a plain file and needs the same external anchor. A `confirmatory`
record whose script hash the current lock does not list is printed by `registry_report.py`
as "not listed" (it is not a broken chain and does not change the exit code); the lock
should therefore only ever be appended to. A last line that was cut
off mid-write is reported and does not stop later appends.

## Checks of the code itself

Run on 2026-09-28; tests, coverage and the live check re-run on 2026-10-02, tests and coverage again on 2026-10-07 (2.18.0). The `pytest` row was re-run on 2026-11-16 (2.23.0) and the `pip-audit` row on 2026-11-14; the other rows are from the earlier runs and were not repeated.

| Check | Result |
|---|---|
| `pytest` (network blocked, incl. `getaddrinfo`) | 2379 passed, 7 deselected (live; 2026-11-16, 2.23.0) |
| `pytest-randomly`, seeds 1, 2, 3 | all pass in every order |
| Branch coverage (`--cov-branch`) | 93 % overall; `heuristics` 97 %, `demix` 93 %, `cli` 89 % |
| Property-based tests (Hypothesis, 24) and edge-case tests (10) | pass; the four that documented defects now pin the fixes |
| Mutation testing (`mutmut` 3.8 on `heuristics.py` and `demix.py`, 3061 mutants, version 2.17.0; the static encoding check deselected) | 2305 killed (75 %), 754 survived, 2 not reached (2.16: 74 % of 2952) |
| `pip-audit -r requirements-lock.txt` | no known vulnerabilities (2026-11-14; `certifi` 2026.10.23, after PYSEC-2026-4320 in the earlier lock) |
| `pytest -m live` (every shipped pool re-verified on chain) | 56 pools on 8 networks verified (Polygon via drpc.org: publicnode returns empty `eth_call` results there) |
| README cases re-run (Ronin, Wintermute, Beanstalk) | same figures: 12,595.3 ETH hop; one 9.9435 ETH inflow; 271 deposits in 2.97 h, one `weak` candidate |

Most surviving mutants change log and evidence wording, result keys that no
test reads, or fallback values that the pipeline never reaches (a signal
without a weight, a boundary that no input can hit). Mutation testing raised
the score from 65 % by pinning what it found in the core: transfer mode had no
test at all, and the voucher span limit, counterparty collection, the shape of a
voucher and the end-to-end run of `run_demix` were unpinned at their edges.
mutmut does not run natively on Windows; the run used the `python:3.13-slim`
container.
