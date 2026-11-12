# Methodology

This document explains the demixing heuristics implemented in `tornado-demix` and how to
read their output honestly.

## 1. Deposits and vouchers

Tornado.Cash is deployed on many chains. The registry holds a set of
fixed-denomination **pool contracts** per network — 32 native-currency pools across 8
chains, plus 24 ERC-20 pools on Ethereum (DAI, cDAI, USDC, cUSDC, USDT, WBTC), 56 pools
as shipped — and every definition below is stated against a pool, not against a number.
See [Pool identity](#pool-identity).

A **deposit** is an outgoing, successful transaction from the subject wallet, on the
selected network, that takes one of two shapes:

- A **native** deposit sends the chain's native currency, and either:
  - goes **straight to a pool contract**, and whose value matches *that pool's own*
    denomination within 0.5% — the pool decides, not the value, because one chain can
    run two contracts at the same number; or
  - goes to a **declared router/proxy** for that network, in which case the pool is
    inferred from the value against the network's native pools.
- An **ERC-20** deposit is a token transfer whose raw amount equals the pool's
  denomination **exactly** (a token amount is an integer, so there is no tolerance
  band). Sent straight to a token pool, it is matched on (pool, token contract, raw
  amount); sent to a declared router, the pool is inferred from (token contract, raw
  amount), which is unique among token pools.

Denominations are per chain and per asset: `0.1 / 1 / 10 / 100 ETH` on Ethereum,
`100 / 1000 / 10000 MATIC` on Polygon, `10 (×2) / 100 (×2) / 500 AVAX` on Avalanche,
`100 / 1000 (×2) / 10000 / 100000 DAI` and five more ERC-20 denomination sets on Ethereum, and so
on.

> **Twin token pools.** Ethereum runs two contracts for 1000 DAI, 50000 cDAI and 500000
> cDAI. A deposit through the router names only the router, so since 2.18 the pool is read
> from the contract that emitted the Deposit event in the receipt; a deposit whose receipt
> cannot be read is marked `pool_ambiguous`. Before 2.18 such a deposit went to one twin by
> registry order, whichever contract received it.

> **Detection gap: undeclared routers.** The router path only fires for an address the
> network declares in its `router_address` column. Ethereum, Polygon, Avalanche,
> Arbitrum, Optimism, Gnosis and Base ship verified routers (the Tornado proxy
> `0x0D55...9b17` on the last four: sampled deposits call `deposit(_tornado, _commitment)`
> into a known pool); **BNB Smart Chain declares none, so there only direct-to-pool
> deposits are detected.** A deposit routed through an undeclared proxy is invisible,
> and the run reports "no Tornado deposits found" — a false negative, not a clean
> wallet. No proxy on BSC could be sampled with free data sources, and an unverified
> address must never enter the registry.

Consecutive deposits **into the same pool** that occur within `--gap-hours` are grouped
into a **voucher**. A voucher of size *N* means the depositor put *N* notes of that pool
in during one session — e.g. `9 × 1 ETH`. Grouping is by pool, not by denomination, so
two contracts at one denomination produce two independent vouchers rather than one merged
one. Grouping chains: deposits each within `--gap-hours` of the previous one form one
voucher even if the whole session spans much longer than the gap;
`--max-voucher-span HOURS` starts a new voucher once a deposit is more than HOURS
after the voucher's first one.

## 2a. Withdrawal side — event mode (default, exact)

The pool contracts emit:

```solidity
event Deposit(bytes32 indexed commitment, uint32 leafIndex, uint256 timestamp)
event Withdrawal(address to, bytes32 nullifierHash, address indexed relayer, uint256 fee)
```

Reading `Withdrawal` logs (Etherscan `getLogs`) yields the **exact recipient**,
**relayer** and **fee** directly from the contract, so no value band is involved.
Only `relayer` is indexed; `to`, `nullifierHash` and `fee` are decoded from the
96-byte data field.

**Why this is strictly better than the value-band heuristic.** A withdrawal that
is *self-relayed* carries **fee = 0**, so the recipient receives 100% of the
denomination — which lies *above* a `0.995 × denom` upper bound and is therefore
silently dropped by a fee-window filter. The heuristic systematically discards
precisely the most incriminating withdrawals. For the wallet
`0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2`, event mode returned 2,159
withdrawals in the 0.1 ETH window versus 1,905 for the heuristic, and one more
exit candidate.

### Relayer analysis

* **Self-relayed withdrawals** are those whose proof names **no relayer**.
  Nobody was paid to broadcast, so whoever sent the transaction paid its gas
  from an address they already controlled — a funded on-chain identity next to
  an exit. Count-matched self-relayed withdrawals are reported as
  `self_relayed_leads.csv`. Self-relaying belongs to the amount+timing evidence
  family (see §4b): it strengthens a count match but never admits a candidate on
  its own.

  Two boundaries matter, and both are enforced in code rather than left to the
  reader. A **named relayer taking a zero fee is not this**: the relayer paid
  the gas and the recipient is tied to nothing, so those are kept apart
  (`zero_fee_relayed` in the JSON export). And **the sender is an inference until it
  is read**, because `withdraw()` may be called by anyone and a careful subject
  funds a throwaway address to send it. Every lead therefore carries a
  `broadcaster_status`:

  | Status | Meaning |
  |---|---|
  | `self` | the sender is the recipient — established from the transaction |
  | `other` | a different funded address sent it; a separate lead in its own right |
  | `unverified` | the provider could not serve the transaction. **Not** a confirmation |
* **Relayer concentration**: a relayer serving few distinct recipients narrows
  the candidate set; a heavily used one does not.
* **Fee fingerprint (since 2.20.0, context only).** The transaction read to verify
  the broadcaster also carries its fee settings: transaction type, priority fee
  (`maxPriorityFeePerGas`), max fee and gas limit. These are chosen by the sender's
  wallet or script. Each self-relayed lead reports them, and leads to *different*
  recipients with the same priority fee (or legacy gas price) and the same gas
  limit to the nearest 1,000 are listed as a **shared fee fingerprint** (`fee_profiles`
  and `fee_fingerprints` in the JSON export). A shared fingerprint hints at one tool
  or operator behind several exits; many users share wallet defaults, so it is
  **context: it never admits a candidate, never enters the score and never raises
  the band**. The leads' own settings cost no extra explorer calls; to compare them,
  up to three self-relayed withdrawals of each other recipient with two or more
  notes in the windows are read as well (larger pools first, at most 150 lookups),
  and only fingerprints that include a lead are listed. Example (case 0x303d0a, 2026): the
  eight self-relayed exits to 0xd71dd9b6e634412713c47fe7ae02c628e338c384 all used a
  3 gwei priority fee and gas limits 428,274–428,318, the same as two exits to
  0x0d7c00bfec641157efb7edd4121e52dd917ac142, while 0xd8b356356d7b143d7ece9f5876fe4b954e93b745
  used 0.15 gwei and ~535,400.

> Note: this toolkit deliberately does **not** report "nullifier double-spends".
> The pool contract enforces nullifier uniqueness at the consensus level, so a
> repeated nullifier can only ever be a data-source artefact, never a finding.

## 2b. Withdrawal side — transfer mode (legacy heuristic)

Tornado withdrawals appear as **internal transactions sent from the pool contract**. Each
withdrawal pays the recipient `denomination − relayer fee`. We keep payouts whose value
falls in the band `[denomination × fee_lo, denomination × fee_hi]` (default `0.90 .. 0.995`).

Each voucher gets its own search window rather than one window spanning a wallet's
first deposit to its last — see [Search windows](#search-windows). Block ranges are
resolved from timestamps; Etherscan caps a response at 10,000 rows, so ranges that
hit the cap are split recursively to avoid missing data.

## 3. Candidate matching (single wallet)

Within the window we count how many qualifying payouts each recipient received. A
recipient hit **exactly N times** (where *N* is a voucher size) is a **candidate
consolidation address**: the depositor may have withdrawn all *N* notes to it.

A count only counts as evidence to the extent that few recipients share it. The
discrimination of a count is

```
disc = 1 - (share - 1) / (unique - 1)
```

where `unique` is the number of distinct recipients in the window and `share` the
number of them with the same count. It is 0 when the whole field shares the count
(every recipient of a single-note voucher window has count 1) and approaches 1 as
the count becomes unique. The gate reduces the field: it removes about twelve of
every thirteen count matches in the placebo test. It does not separate real from
chance count matches, though: in every discrimination bin the chance share stays
near 1 ([EVALUATION.md](EVALUATION.md)). A count match is a signal **only at
`disc >= MIN_COUNT_DISCRIMINATION` (0.5)**; below that it neither adds to the
score nor counts as an evidence family, and it admits no candidate. Above it, its
weight scales with `disc`.

`disc` is relative: in a window with only two or three recipients a unique count
reaches 1.0 although a chance match is still likely. A count match therefore also
needs a field of at least `MIN_FIELD_SIZE` (5) recipients, and the report shows the
field size next to `disc`.

The count is compared with each voucher's size and, when a pool holds several
vouchers, with their sum: an address that collected every note of two sessions in
one pool (2 + 3 = 5 withdrawals) is a candidate, and its evidence line says "all 5
notes of the pool's vouchers". The same gate applies.

**Voucher order (since 2.20.0).** A pool's windows are searched together, so a
recipient's count is taken over all of them. A count equal to a voucher's size is
only a count match when every one of those withdrawals falls inside *that*
voucher's own window (first deposit to last deposit plus the window); a count equal
to the sum of the pool's vouchers needs no withdrawal before the pool's first
deposit. Up to 2.19.0 a recipient with seven withdrawals in June could match a
seven-note voucher deposited in August, because a one-note June voucher of the same
pool had opened a window then. Runs record `params.voucher_order = true`; cached
runs without the flag (the evaluation and placebo runs in
[EVALUATION.md](EVALUATION.md)) are re-scored unchanged, so the published numbers
stay reproducible. The evidence line of a voucher-sized count that fails the check
says "not counted".

## Search windows

Each voucher gets its own window: from its first deposit to `window_days` after
its last (or `--exit-window` hours after it). Windows that overlap are merged. A
wallet that used a pool twice, two years apart, produces two windows, not one
spanning both, which would make every withdrawal in between a candidate. A window
that ends in the future is clamped to the current block.

Laundering tends to use the mixer as a fast transit. In 27 public laundering cases
(the MixLaunder set, Fu et al., arXiv:2609.27807) 85.5 % of the intervals between
adjacent deposits and withdrawals were within a day and 95.8 % within seven days,
and deposits and withdrawals interleaved in 25 of the cases. `--rapid` is a
shortcut for `--exit-window 168`; a narrower window shrinks the field, which is
what makes a count match discriminate. In the KuCoin case below the first exits
came 7 days after the last deposit, so a narrower window would have missed them.

## When the analysis stops

A timestamp that cannot be resolved to a block number aborts that pool and is
recorded under `unresolved`. The CLI prints a `NOT searched` warning per such
pool, and the HTML report renders an "Unresolved pools (not searched)" section,
so a pool that was never searched is never mistaken for one that was searched
and found no exits. The alternative — proceeding across the whole chain —
removes the timing correlation the method depends on while still producing
confident-looking candidates, which is the more dangerous failure. The web UI
lists unresolved pools as well. A provider that fails, or answers with an error,
raises instead of returning an empty list, so it cannot read as "no deposits".

## 4. Cross-wallet correlation (multi)

Given several wallets we compute:

| Result | Definition | How much it says |
|--------|-----------|----------|
| **Profile match (exact, multi-pool)** | one address received a wallet's *entire* fingerprint, e.g. `6×0.1 + 4×1.0` | single-wallet signal (`profile_match`); it admits a candidate and belongs to the amount+timing family, so on its own it is `weak`, and with a linked address it is `moderate` (one lead source) |
| **Cross consolidator** | one address is a full-fingerprint match for 2+ wallets | graded: `strong` with an independent gas-price/linked signal, `moderate` for distinct fingerprints of wallets that did not deposit together, `weak` (window-overlap artefact) otherwise |
| **Synchronous deposits** | wallets whose deposits chain within `SYNC_GAP_HOURS` (6 h) | behavioural link in its own right |
| **Shared funder** | wallets funded, before their first deposit, by the same plain native transfer sender that is neither labelled nor busy (`BUSY_FUNDER_TXS` = 200 transactions or more) | edge `shared funder` in the operator graph; in the 27 MixLaunder cases 90.1 % of laundering deposit addresses shared an immediate funder with another deposit address of the case |
| **Strong link (single pool)** | a count-matched candidate shared by 2+ wallets | lead; often shared window |
| **Soft overlap** | any qualifying recipient shared by 2+ wallets | usually a shared-window artefact |

### The window-overlap caveat

If two wallets have the **same voucher size** and **overlapping windows** (because they
deposited at nearly the same time), their count-matched candidate sets are computed from
the *same* pool payouts and are therefore **almost identical by construction**. In that
situation, intersections do **not** distinguish "linked" from "unlinked" — they are an
artefact of the shared window.

What remains discriminating:

- **Multi-denomination profile matches**, because a single address receiving a specific
  combination like `8×0.1 + 4×1.0 + 1×10` is unlikely by chance.
- **Deposit-time synchronicity**, which is a behavioural signal independent of the exit
  analysis.

The tool surfaces this caveat directly in the multi-wallet report.

## 4b. Extra heuristics (gas price, linked address) and scoring

Two further signals from the Tornado forensics literature (as used by Tutela) are
layered onto the count-matched candidates. Neither costs Etherscan quota; the
gas-price gate and the contract check make bounded, memoised RPC calls.

- **Unique gas price.** The `Withdrawal` log endpoint returns each withdrawal's
  `gasPrice`, and the wallet's deposit gas prices come from its tx list. A
  withdrawal whose gas price exactly equals a deposit gas price, and which is rare
  in the window (shared by ≤3 withdrawals), is flagged `gas_price`. Only a
  withdrawal the user sent without a relayer counts: a relayer chooses the gas
  price of the transactions it sends, so a match there says nothing about the
  user. On real depositors 44 of 45 matches were on relayed withdrawals before this
  rule (version 2.15, [EVALUATION.md](EVALUATION.md)).

  **Gated on EIP-1559.** The heuristic assumes the sender chose the price. After
  London the effective price on a log is base fee plus tip, and the base fee
  belongs to the block — every transaction in it shares the value, so a match
  says nothing about who sent them. The signal is credited only for blocks whose
  `baseFeePerGas` is absent (pre-London) or zero (BSC reports `0x0`, and the
  price there is still user-chosen), read per block from the network's own RPC
  and memoised. A failed lookup, or a network with no RPC endpoint, **disables**
  the signal rather than assuming it holds. In practice this means `gas_price`
  rarely fires on post-2021 Ethereum data — which is the honest outcome, not a
  regression.
- **Linked address.** If a candidate exit address is a **direct counterparty** of
  the depositor (they transact outside Tornado), the mix is bypassed entirely —
  flagged `linked`, the single strongest structural signal. A counterparty that is
  a contract (`eth_getCode` over the network's RPC) is a router, a DEX or a service
  rather than a person and does not earn `linked`; the evidence line says so. When
  the check cannot be made the signal is kept. A withdrawal back to the depositor's
  own address is `linked` as well, and so is a direct ERC-20 transfer between the
  depositor and the candidate (token transfers are read once per run). Since 2.17
  a direct link is `linked` only when the recipient's first withdrawal in that
  pool came within 72 hours (`EARLY_EXIT_HOURS`) after a voucher's last deposit
  into the same pool; a later one is `linked_late`, shown in the evidence with
  its delay but never scored and never a lead. On 302 random Ethereum depositors
  (`tools/placebo_eval.py`, 152 plus 150 after 2022-08-08) a direct link withdrawn
  within 72 hours was a lead 26 times in real windows against once in decoy
  windows (before 2022-08-08 15 against 0, after it 11 against 1); a later one 8
  against 13 (5 against 4, 3 against 9), chance level ([EVALUATION.md](EVALUATION.md#direct-links-early-versus-late)).
  The rule applies to the depositor's own address as well.
- **Linked withdrawal sender.** `withdraw()` may be called by anyone, and the
  caller is recorded as the transaction sender. If the depositor, or one of its
  direct non-contract counterparties, sent a withdrawal transaction, its recipient
  is flagged `linked_sender`. Since 2.19 this is **context only**: it is shown in
  the evidence but has no weight, never raises the band and is not an independent
  family (like `linked_late`). It did not separate from chance in any placebo
  sample: 8 target against 2 decoy leads on 302 depositors (R 0.27, interval
  0.03–1.36), 14 / 11 in the 2026 Ethereum hold-out (R 0.84), 9 / 2 on Polygon
  (R 0.22, 0.02–1.07). In the Ethereum hold-out it was 11 of the 15 decoy
  `moderate` leads; without it `moderate` there is 101 / 4, R 0.042 (exact
  0.011–0.111). The tool reads the outgoing
  transactions of at most 25 counterparties, busiest first, within the searched
  blocks only. It catches an operator that relays its own withdrawals when the
  relaying address is a counterparty of the depositor: in the KuCoin case it listed
  the 6 exits of that caller, and nothing else, but none of the 30 exits of the
  attacker's second caller, which the depositor never transacted with. Since 2.19
  those 6 are shown as context, not as `moderate` leads: the price of the rule
  ([EVALUATION.md](EVALUATION.md#rule-219)).
- **Shared exchange deposit address.** An exchange gives each customer their own
  deposit address and sweeps what arrives there to its hot wallets, so two
  addresses that sent funds to the same deposit address are, with few exceptions,
  one customer (the address-reuse idea of Tutela). The tool examines the
  depositor's outgoing counterparties (at most 25, busiest first; labelled services
  and Tornado contracts are skipped, other contracts are rejected when checked) and takes one for a deposit
  address when it has fewer than 1,000 transactions and at most 50 senders, and at
  least 80 % of its outgoing transfers go to hot wallets: among its three most
  frequent destinations, those labelled as an exchange or with at least 10,000
  transactions. A busy address counts as a hot wallet only if it is not a contract
  (a token, a DEX router or the Tornado router is busy too) and receives amounts
  forwarded within 38,400 seconds (3,200 Ethereum blocks of 12 s; the window is a
  duration, taken from the row timestamps, and falls back to 3,200 blocks only for a
  row without one), short by at most 0.01 ETH or 1 % of a token amount (the
  forwarding test of Victor used by Tutela); a sweep may also forward the sum of
  several incoming transfers of the same asset received in the window and after the
  previous outgoing transfer of that asset, with the same tolerance. Counting rows:
  a token transfer is one transfer (the `txlist` call to the token contract, value
  0, is not counted next to the `tokentx` row), zero-value transfers are ignored,
  and a token transfer out of the address whose transaction the address did not send
  is dropped (address poisoning by `transferFrom` spoofing); a spoofed non-zero
  transfer into the address by a fake token is not detected offline. An account
  whose code is an EIP-7702 delegation designator (`0xef0100` plus a 20-byte
  address) is an EOA, not a contract. A recipient in the window that
  also sent funds to one of these addresses is flagged `shared_deposit`. Since 2.21
  this is **context only**: it is shown in the evidence but has no weight, never
  raises the band and is not an independent family (like `linked_sender`). In the
  second hold-out, at 30 days, a labelled shared deposit was 18 target against 30
  decoy leads on Ethereum (R 1.77, interval 0.16-3.91) and 23 against 13 on Polygon
  (R 0.58, 0.00-3.16), while at 72 hours it was 14 against 2 and 23 against 0
  ([HOLDOUT2_RESULTS.md](HOLDOUT2_RESULTS.md)). Before that, in the sample of rule
  2.17, deposit addresses swept to labelled exchanges gave 27 real-window against 4
  decoy hits and those recognised by activity alone were at chance (16 against 12).
  Deposit addresses without a label stay context as before. The lookup costs up to 50
  history queries plus a busy check per new sweep target, and can be turned off
  (`--no-deposit-addresses`).

  Kinds (unreleased). Every listed address has a `kind`. `exchange_deposit` is the rule
  above: every labelled sweep target is an exchange, and only a sweep to labelled
  exchange wallets is `evidence`; this is the only kind that was ever a lead (since
  2.21 it is context too) and the only kind that links two addresses.
  `service_deposit` has the same sweep behaviour but a labelled sweep target of another
  category (gambling, bridge, payment, ...): it is shown with the service name, never
  scored and never linked. `forwarder` is a per-account forwarder whose token outflows
  are executed by an operator or relayer instead of the address: at least 80 % of the
  address's non-zero outgoing real-token transfers (any token contracts) are in
  transactions the address did not send, in at least two distinct transactions, and up
  to three of them read with `tx_info` share one `to` contract (the operator);
  zero-value transfers never make a forwarder. When that operator is a public ERC-4337
  EntryPoint (v0.6, v0.7 or v0.8) the kind is `smart_account` instead: the user's own
  account run through a bundler, not a service address. A contract account (ERC-4337 accounts normally are contracts) is read too, but only these two kinds can be returned for a contract. A forwarder whose operator is the token contract itself (a fake-token spoof) is rejected. Both kinds are never scored and
  never link two addresses. This pass reads the outgoing
  token rows before the poisoning filter, which stays unchanged for the other two
  kinds. Exchange and service addresses also record a `gas_funder` (context, not
  scored): the sender of the first native transfer, if it is below 0.01 of the native
  coin and precedes the first token sweep. Without labels the classifier misses
  Binance (its hot wallet is a proxy contract) and Crypto.com (its hot wallet has
  fewer than 10,000 transactions); these are documented limits.
- **Early multi-pool profile.** A wallet that deposited at least 10 notes over two
  or more pools has a profile (notes per pool). A recipient that, in every one of
  those pools, received exactly that many withdrawals between the pool's first
  deposit and 72 hours after its last one is flagged `early_profile`. One pool is
  the count match again and is chance-level; over two or more pools and a short
  window the placebo test on 4,194 such depositors gave 318 real-window against 34
  decoy hits (chance share 0.12). It belongs to the amount+timing family (it reads
  the same withdrawals as the count match) but, unlike the rest of that family,
  makes a lead on its own.

**Band, evidence and score.** Each recipient accumulates a signal set
(`count_match`, `self_relayed`, `early_profile`, `gas_price`, `linked`,
and `profile_match` on `multi` runs), and
possibly `linked_late`, `linked_sender` and `shared_deposit`, which are context
only (since 2.19 for the sender, since 2.21 for the shared deposit address): no
family, no weight, no band. The signals
fall into evidence families: amount+timing (`count_match`, `self_relayed`,
`early_profile`, `profile_match`), gas price, linked address (`linked`). The
**band** needs a lead signal — an early direct link or an early multi-pool
profile. Lead signals come from two independent sources: an early direct link
(`linked`) and an early multi-pool profile (`early_profile`). `strong` needs lead
signals from both of them, `moderate` a lead signal from one, and `weak` is
amount+timing, gas price and/or context-only signals. Amount+timing and gas price never raise the band; they
are shown as context. The rule follows the
placebo test on real depositors ([EVALUATION.md](EVALUATION.md)): run on decoy
windows that end before the wallet's first deposit, the pipeline found
amount+timing and gas-price leads as often as in the real windows, and only
linked-address leads clearly more often (30 against 7 over 30 days; 19 against 1
within 72 hours, measured before 2.17, and counting the withdrawal sender, which 2.19 removed); the early multi-pool profile was added in 2.15 on the same kind
of evidence. Version 2.16 added the two-source rule for `strong`: among
candidates with a lead signal, a second amount+timing family did not lower the
chance share (4 real against 1 decoy at 72 hours, 4 against 0 over 30 days;
`tools/placebo_strong.py`). Before 2.16 a lead signal plus any other family was
`strong`, so a direct link plus a count match was `strong`; before version 2.13 a
gas-price match or a self-relayed count match alone reached `moderate`, and two
non-linked families reached `strong`. Since 2.17 a direct link counts as a lead
only within 72 hours (see above); the pooled 30-day number hid that a later one
is chance-level. Before 2.17 a direct link at any delay was `linked`. Before 2.19 a
`linked_sender` was a lead (the second source with the direct link); before 2.21 a
`shared_deposit` was a third lead source. A
linked exit whose first withdrawal came within 72 hours of the deposit is marked
as an **early exit**; like the fresh-address mark it does not change the band or
the score by itself (an early direct link is `linked` by definition; for
the context-only `shared_deposit` and `linked_sender` the mark is context). Every candidate carries its **evidence**: each family, whether it
holds, and the numbers behind it (share of the field with the same count, `disc`,
gas price, number of relayer-free withdrawals).

The **score** combines documented weights by noisy-OR across evidence families,
`1 − Π_f (1 − w_f)`, where `w_f` is the strongest weight among the signals of
family `f` that hold (the `count_match` weight scaled by `disc`). Signals of one
family read the same withdrawals, so they are not added twice: a count match with a
self-relayed withdrawal scores the stronger of the two, and a profile match counts
in the amount+timing family. Candidates are ordered by band, then by
score. The score orders leads within a band; it is not a probability and it is not
calibrated against known outcomes. The band does not depend on the weights at all:
re-scoring the thesis cases with every weight scaled by a random factor in
[0.5, 1.5] (and [0.1, 1.9]) never changed a band, and changed the order only
between same-band candidates with different signals (`tools/sensitivity.py`).
On generated data with a known exit, each family adds ranking power; on real
depositors only the lead signals stand above chance. Under the pre-2.13
band rule `moderate` was common on unrelated addresses and false `strong` came from chance gas-price
reuse ([EVALUATION.md](EVALUATION.md)).

The report and the web UI also state the analysis parameters (voucher gap, window,
thresholds) and the block ranges read, and `demix --json` saves the full result.

**Independence, for cross-method matches.** Agreement between methods is only
evidence when the methods are independent. `count_match` and `self_relayed` are
**not**: the second is credited only on an address that already matched the
first, and both read the same withdrawals. They are treated as one family, so
reaching two families requires a count match corroborated by a gas-price reuse
or a direct counterparty relationship.

**Operator graph.** For multiple wallets, a *wallet-specific* edge unions the
two wallets; connected components are reported as operator clusters. Only two
signals qualify, and the reason is the window-overlap artefact of §4:
`gas_price` is computed against a particular wallet's own deposit gas prices and
`linked` against its own counterparty set, so either one holding at a shared
address is a statement about the pair. `linked_late` does not qualify: a direct
link this late is chance-level on real depositors (since 2.17). `self_relayed` is **not** — it is a
property of the withdrawal, identical for every wallet whose window contains it,
and since overlapping windows share count-matched recipients by construction and
zero-fee withdrawals are ordinary, crediting it would merge unrelated depositors
into a single "operator". A shared multi-pool consolidator unions unless it is
graded as a window-overlap artefact. Bare count-overlap never does.

## 4c. Merkle-root exclusion of withdrawals (exploratory, off by default)

A withdrawal proof is built against one Merkle root of the pool's tree, and the withdrawal
transaction names that root in its calldata. A root computed after `n_root` insertions holds
the leaves `0 .. n_root - 1`, so a note with a leaf index of `n_root` or more cannot have been
spent by that withdrawal. `demix --root-check` (the "Merkle-root check" box in the web UI,
`run_demix(root_check=True)`) uses this to exclude withdrawals of candidates.

Rules (the semantics are fixed; the wording is the code's):

- A withdrawal is **excluded** for the wallet if `n_root <= min_leaf`: the tree at the time of
  that root held **none** of the wallet's notes in this pool. This is a cryptographic fact, not
  a probability.
- If `min_leaf < n_root <= max_leaf`, only the later notes are excluded. Nothing is removed and
  the withdrawal is still counted; the candidate carries the separate context signal
  `root_partial` ("some of the wallet's notes were not yet in the tree at this withdrawal's
  root"), not `root_excluded`.
- A candidate whose **all** withdrawals in a pool are excluded is removed from the ranking and
  shown in a separate block, "excluded by Merkle root", with each transaction and its `n_root`.
  A candidate with some withdrawals removed stays, only the non-excluded withdrawals are
  counted (the count match, `candidates_by_count` and the signals are recomputed) and it carries
  the context signal `root_excluded`. If the recount costs it its count match and it no longer qualifies,
  it is listed in the same block as "dropped after recount". Only that candidate's own hits
  change: the field statistics other candidates are judged against (recipient counts,
  `unique_recipients`, the gas-price share) stay at their pre-removal values, because an
  excluded withdrawal is still a real withdrawal of someone else.
- Only the withdrawals of ranked candidates are read, not every withdrawal in the window: one
  `tx_info` call per transaction, at most `ROOT_CHECK_LIMIT = 200` per run. Past the limit
  (or when a pool cannot be checked) the check stops, the run logs it, and
  `root_check_incomplete` is set; candidates not reached are not excluded.

`root_excluded` and `root_partial` are not leads, have no weight in the score and never change a band. The check removes
withdrawals; it does not add evidence for anyone. A root that is not in the pool's index
(a withdrawal whose root cannot be read from the calldata, or an index that does not reach that
block) keeps the withdrawal and is counted as "not found".

**How the pieces are obtained.** The root is the 32-byte word before the withdrawal's nullifier
in the transaction input, found at any byte offset (`tornado_demix/withdraw_root.py`). The
number of leaves behind a root comes from an index built by recomputing the pool's tree from its
`Deposit` events (`tornado_demix/merkle.py`: MiMCSponge, 20 levels, the contract's empty-tree
values; the empty-tree values are tested against the deployed pools in `tests/test_merkle.py`,
and a wrong index would show as withdrawal roots that are not found, see
[EVALUATION.md](EVALUATION.md)). The first build of a
large pool takes about half an hour of pure-Python hashing (17 ms per deposit) and is cached
under `.cache/merkle`, later runs are incremental. The wallet's leaves are the `leafIndex` of
the `Deposit` event of each of its deposit transactions.

**Decoy windows.** In a decoy run (`deposit_shift_seconds` > 0, used by `--placebo`) every
withdrawal predates the wallet's real deposits, so the real leaf indices would exclude all of
them and bias the comparison. In decoy mode the wallet's emulated leaf `L` for a deposit is the
number of pool insertions up to the block of the *shifted* deposit time (real mode uses the
actual leaf index). The emulated note counts as inserted at the block of the pool's real
insertion number `L`; a withdrawal mined before that block is not checked and is counted as
"decoy gap, not checked", because between the shifted block and that insertion the newest root
holds exactly `L` leaves and would exclude the withdrawal by construction. Only later
withdrawals with `n_root <= L` are excluded, which asks the same question as a real window.

**Direct-link observability in the placebo.** The placebo plants one synthetic withdrawal to a
non-contract counterparty of the wallet in the decoy run and re-scores it with the same code
(`tornado_demix/power.py`). When the planted lead is not found, or the wallet has no non-contract
counterparty at all, no direct link to a third-party address can be observed for this wallet, and the same counterparties
stand in its real window: it is blind on both sides. The report then says "decoy window cannot show
a planted lead; no chance estimate for this wallet" for direct links (`decoy_detectable` and
`direct_link_observable` in the summary) and does not read the decoy count as a chance level.
Early-profile leads are not affected and are reported normally. Planting at the wallet itself is
tried too (`self_detectable`); a withdrawal back to the depositor can be observed and does occur
(12 of 95 target leads on Ethereum, 17 of 103 on Polygon), but it does not make a wallet
observable for links to third parties. In rate tables such wallets are removed from target and decoy alike.

**Ceiling.** A root can exclude a deposit only while it is still accepted: the pool keeps the
last `ROOT_HISTORY_SIZE` roots (100 on the Ethereum pools, 30 on the Polygon ones, read from the
verified source), so a withdrawal proven against a root older than the wallet's newest note is
the only kind that can be excluded. In the cached hold-out windows at most 11.01 % of
Ethereum and 10.94 % of Polygon withdrawals could be excluded at all (`tools/root_ceiling.py`,
upper bound, newest note only; see [EVALUATION.md](EVALUATION.md)). Most withdrawals in a
window are made after the wallet's notes are in every accepted root, so the exclusion cannot
decide them; it can only remove those that are old enough.

**Limits.** The exclusion tests the wallet's *notes*, not its funds: a withdrawal proven
against a newer root is not excluded, whoever made it. A withdrawal whose calldata does not
expose the root next to the nullifier is not excluded. Candidates
admitted only through a count that the removal changes are re-evaluated once; a candidate that
would newly appear after the removal is not looked for.

The evaluation tools log every test they compute to a hash-chained register
(`tornado_demix/registry.py`, see "Test register" in EVALUATION.md), so the number of
tests actually run, exploratory sweeps included, is known when results are corrected for
multiple comparisons.

A record is marked `confirmatory` only when the protocol lock lists the SHA-256 of the script that produced it. That is all the status says: it depends on the script file's hash alone, so a change in the library code the script calls (or in the inputs, parameters or data) is not covered by it.

## 5. Cluster tracing (split exits)

When a wallet does not consolidate to a single address, its notes are spread across many
recipients. For each pool we take the count-matched candidates, follow their funds **one
hop forward** (within `FORWARD_DAYS`, ignoring dust), and look for a **shared downstream
address `Z` fed by two or more pool layers**. Such a `Z` is where the split exits
reconverge.

The forward hop excludes **the traced network's own pools and routers** and the burn
address. Re-depositing into the mixer is ordinary behaviour, and counting it as a
downstream hop would report the pool contract itself as the operator's consolidation
point — ranked first, since the sort favours layer count. The exclusion set is read from
the network being traced, not from a fixed Ethereum list, or it would filter nothing on
the other seven chains.

Layer results are cached on disk to make an interrupted run resumable. Each cache file is
scoped to the network, to a fingerprint of its pool set, and to the wallet and analysis
parameters, because a cached entry carries pool keys, candidate addresses and transaction
hashes that mean something only for that chain and that run.

A reconvergence point is a lead, not an evidence band: a busy service (an exchange hot
wallet, a DEX) collects unrelated exits one hop away.

Noisy layers are skipped: a layer whose count-match is not discriminating — including
every single-note layer, where the count is shared by the entire window and eliminates
nobody — contributes no candidates, as does any layer with more than `--layer-cap`
candidates. Tracing hundreds of false candidates, or a count that discriminates nobody,
dilutes the signal. Absence of a 1-hop reconvergence is itself informative: it suggests
dispersed exits (good operational security) or reconvergence deeper than one hop.

## 5a. Exit groups

When an operator pools several deposits and pays the notes out to many fresh
addresses, no exit receives a voucher-sized count and the count match finds
nothing (both public cases in docs/EVALUATION.md). The payout rhythm remains:
the operator withdraws in bursts and its exits recur in the same bursts. Among
the recipients with two or more withdrawals, two are joined when at least
`MIN_JOINT` = 2 of their withdrawals fall within `JOINT_WINDOW_S` = 10 minutes of
each other; connected groups of three or more are exit groups.

A group says that its members were paid out together, not whose notes they
were: another busy withdrawer forms groups as well. It is therefore tied to the
depositor only through an anchor, a member that is a `strong` or `moderate`
candidate or an exit the investigator already knows (`--known-exit`). Unanchored
groups are counted, not listed, and no group enters a score or a band.

## 5b. Multi-hop tracing (trace)

`trace` follows an amount forward from an exit address, after MixGuard's
Algorithm 1. At each address the outgoing transfers of the traced asset after
the funds arrived are attributed first in, first out until the amount is spent;
each attributed transfer is an edge to the next address. A transfer into a
contract that pays the sender back in the same transaction (a DEX swap: an
internal transfer or a token transfer to the sender in that transaction) is
resolved: the trace continues from the sender with the swap output, scaled by the
share of the input the trace owns. A contract that pays nothing back (a deposit
contract, a bridge the tool cannot decode) ends that branch. The walk also stops at a labelled address, at
`--max-hops` (default 4), at an address that did not move the funds on, and after
`MAX_NODES` = 40 expanded addresses, which bounds the API calls. Amounts below 1 %
of the start are not followed.

**Bridges.** A deposit into an Across SpokePool is followed to the destination
chain. The deposit event of the origin transaction (`V3FundsDeposited` or
`FundsDeposited`) carries the destination chain id, a deposit id, the depositor and
the recipient; the destination SpokePool's `FilledRelay` / `FilledV3Relay` event
repeats the origin chain id and the deposit id as indexed topics, so the fill is read
with one `getLogs` query on the destination chain (any chain of the networks registry)
and no bridge API. The walk continues from the fill recipient, in the token the fill
paid (the native coin when a wrapped native token is paid out), with the amount the
fill delivered (the traced share of the deposit, after the bridge fee); the hop count
carries on and every edge and end point names its chain. A deposit with no fill in
the explorer ends the branch as "bridge, fill not found"; a destination outside the
registry as "bridge, destination chain N not supported"; an explorer error on the
destination chain (block lookup, API) ends only that branch as "bridge, fill lookup
failed: ..." (a rejected API key still aborts). The fill is accepted only when its
original relay data (tokens, amounts, recipient, depositor) equal the deposit's; a
sped-up fill is followed to its updated recipient and amount
(`relayExecutionInfo`). Only the V3 and current SpokePool events are followed:
Tornado-era v2 deposits (2022 to early 2024) are not decoded, and a call to a listed
SpokePool with no decodable deposit ends as "bridge across, deposit event not decoded"
("(v2 not decoded)" for the v2 pool). A deposit that pays out to a contract recipient
in a wrapped native token is continued as that token or as the native coin per
`WRAPPED_NATIVE`; a contract recipient is not looked into. The contracts that count as
bridges are listed in `tornado_demix/data/bridges.csv` (columns chain_id, address,
protocol, role, kind, source_url; kind is contract, aggregator or maker_eoa), each row
with the public page it came from; any other contract is treated as before. Other
bridges and aggregators are listed but not decoded. Maker-based bridges (Orbiter,
Layerswap, Owlto, Rhino.fi) pay out of EOAs; no maker address has an official or
public-label source here, so none is listed. The join is by on-chain id; it identifies
where the bridged amount
arrived, not who controls the recipient.

Only the first matching deposit of a transaction is followed (one deposit per transaction: the depositor and the traced token must match); a transaction with several Across deposits from the same address is not split. Labels end a trace on the start chain, where the attribution labels and, with `--labels-blockscout`, the cached Blockscout labels apply; the addresses reached on a destination chain after a bridge are checked against the attribution labels only, never against Blockscout labels.

FIFO is a convention: funds in one account are fungible, so an edge says where
the traced amount *would* have gone under that rule. It is a lead for
corroboration, like every other output.

**Allocation bounds.** Next to the FIFO amount every edge carries `lower` and
`upper`: how much of the transfer can be the traced funds under *any* ordering
convention. The sender's history of the same asset (its inflows and outflows, one
extra explorer call per expanded node: internal transactions for the native
currency, nothing extra for a token) splits its balance B into a traced pool T and
an other pool. Per outflow `o`, with T known to lie in `[t_lo, t_hi]`:
`lower = max(0, o - (B - t_lo))`, `upper = min(o, t_hi)`, then
`t_lo = max(0, t_lo - o)`, `t_hi = min(t_hi, B - o)`, `B -= o`. These are tight and
FIFO always lies inside them (derivation in `tornado_demix/allocation.py`). `lower`
above 0 means "certainly at least that much"; `lower` 0 means the transfer is only
possible. Events are ordered by block, then by transaction index when the explorer
row has one (internal transfers do not: an unindexed inflow counts as first in its
block, an unindexed outflow as last). Native and token assets are separate
histories. An outflow larger than the balance the history explains sets
`history_incomplete` on the edge (internal transfers or pages missing); the
shortfall is treated as unknown other funds, never clamped. Gas fees are not
subtracted, which only widens the interval. Inflows the explorer does not list
(beacon-chain withdrawals, block rewards, self-destruct payouts) understate the
balance and can raise `lower`; `history_incomplete` is set per expanded node, not per
edge, and is not triggered by such unlisted inflows. The traced inflow is the arrival
transaction of the hop (the swap or bridge fill transaction too); for the start
address, whose arrival is not known, the first inflow at or after the start block
that matches the amount if exactly one does (`arrival_matched` = `exact`); with
several equal inflows (`ambiguous`, common with Tornado denominations) or none
(`synthetic`) the amount is placed at the start block, which only widens the bounds.
`--arrival-tx` (web: arrival transaction) names the arrival explicitly. An edge whose
transfer precedes the arrival, or whose FIFO amount falls outside its own bounds, shows
FIFO only with `bounds_available` false.

## 6. Reading the output responsibly

- Treat every candidate as a **lead**, corroborated by other evidence (KYC records,
  off-chain data, subsequent hops, exchange attribution).
- Read the band and its evidence, not the score.
- Prefer exact multi-pool profile matches over single-count candidates.
- Do not present soft overlaps from synchronous-deposit wallets as evidence of a link.

## 7. Recipient characterisation

`characterize` describes an exit candidate: activity counts, inflows from the
network's native pools (a received note is at least half the denomination, so a
relayer's collected fees are not counted), and the dominant next transactions. A
next transaction that carried no native value is marked as a contract call (for
example a token transfer or approval); token amounts are not traced. The class
(`high-activity service`, `aggregator`, `possible personal exit`, `no pool
inflows`) follows fixed thresholds on activity and pool diversity. Attribution
labels, when a label set is configured, are shown next to addresses and never
change a score or a band.

An address whose first transaction came at most `DISPOSABLE_HOURS` (24 h) before
its first pool inflow is flagged as disposable. The demix run makes the same
check for its ten top candidates with two single-row queries each (first normal
and first internal transaction) and marks an exit with at most a day of history
before its first withdrawal as fresh. In the 27 MixLaunder cases 98.6 % of
laundering exits were such fresh addresses; so is any newly created wallet, so the
mark is context in the report and never enters the score or the band.

## 7a. Label sources and lineage

Labels decide whether a deposit address is shown as swept to a labelled exchange, so a thin label set costs recall (on Polygon a 10-address re-test found one Binance wallet in the wallet-attribution set, while the Blockscout metadata service tagged Binance, Bybit, Coinbase and Crypto.com). There are two sources:

- **wallet-attribution** (`labels fetch`, `--attribution-dir`): one CSV per network, as before.
- **Blockscout metadata** (opt-in: `--labels-blockscout` / the "Blockscout labels (cached)" checkbox): `tornado-demix labels blockscout --network N --addresses-file F` asks `metadata.services.blockscout.com` for tags, 20 addresses per call, by EIP-55 checksummed address (a lowercase lookup finds nothing; checksumming needs keccak from the `labels` extra), and caches them in `.cache/labels/blockscout/<chainId>.csv`. A failed or exhausted request raises; it is never read as "no tags". The `/api/v2/addresses/<a>` field `metadata` is not used (it is null even when tags exist).

Classification is conservative. `exchange` only when the tag name starts with one of Binance, Coinbase, Bybit, OKX, Kraken, Crypto.com, KuCoin, Gate, HTX/Huobi, Bitfinex, Bitget, MEXC followed by `:` or a space, and names no token, bridge, router or contract (so "Binance Smart Chain: Token" is `other`); a generic "DEPOSIT ADDRESS" tag is `deposit`; everything else is `other`. Nothing is guessed beyond that.

The deposit-address classifier (section 4b, shared exchange deposit address) uses a Blockscout-sourced label only when its category is `exchange`: a Blockscout `deposit` tag does not hide a counterparty as "a service's own wallet", and a Blockscout `other` tag on a sweep target does not make the address a `service_deposit`. Labels from the attribution source keep their full effect there. The tags remain visible as labels in the reports.

**Lineage.** Blockscout tags are often copied from Etherscan, and commercial tools copy Blockscout, so agreement between those is one observation, not two. A merged label lists its `sources` and carries `independent_sources`: Etherscan- and Blockscout-derived labels count as ONE lineage (`etherscan/blockscout`), any other source is its own lineage, and a label naming a different entity is listed but not counted. The wallet-attribution entry wins on category and entity; the Blockscout label only adds addresses it does not know. Scoring does not yet use `independent_sources`; it is recorded so a reader can see how many independent lineages stand behind a label.

## Pool identity

A denomination is not a unique identifier. Avalanche runs two live 10 AVAX and
two 100 AVAX pools, Ethereum two 1000 DAI, two 50000 cDAI and two 500000 cDAI pools,
and on Ethereum `100 DAI`, `100 USDC` and `100 USDT` are three different
contracts at the same number. Results are therefore keyed by a pool key — the
denomination, the asset, and a `#2` suffix where a chain holds more than one
contract at that pair.

Signals are keyed by `(pool, address)` as well: keyed by address alone, a
self-relayed flag earned in the 1 ETH pool would follow the address into every
other pool it appears in.

## Verifying a pool

An address is accepted as a Tornado pool only on on-chain evidence. The primary
check reads the contract's own getters over a public RPC:

- `denomination()` — the fixed amount, in the asset's smallest unit;
- `token()` — zero for a native pool, otherwise the ERC-20 contract, whose
  `decimals()` and `symbol()` name the asset;
- `levels()` — the Merkle tree depth, 20 for every genuine deployment;
- `nextIndex()` — deposits ever accepted, which distinguishes a live pool from
  a deployed-but-unused one.

Three of those decide the verdict. A contract is **rejected** if it does not
answer `denomination()`, if it answers with a value outside the standard set, or
if `levels()` is anything other than 20 — a different tree depth is a different
contract wearing the same interface. `nextIndex()` **annotates** rather than
rejects: an unused pool is still a real pool, and flagging it is more useful
than dropping it. The rejection message names which check failed.

The older method — sampling `Withdrawal` logs and requiring `payout + fee` to be
constant — is retained as an independent cross-check for chains without a usable
public RPC. It reads no token metadata, so it verifies native pools only.
