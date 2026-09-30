# Methodology

This document explains the demixing heuristics implemented in `tornado-demix` and how to
read their output honestly.

## 1. Deposits and vouchers

Tornado.Cash is deployed on many chains. The registry holds a set of
fixed-denomination **pool contracts** per network — 31 native-currency pools across 8
chains, plus 24 ERC-20 pools on Ethereum (DAI, cDAI, USDC, cUSDC, USDT, WBTC), 55 pools
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

> **Detection gap: undeclared routers.** The router path only fires for an address the
> network declares in its `router_address` column. Ethereum, Polygon and Avalanche ship
> verified routers; **BNB Smart Chain, Arbitrum, Optimism, Gnosis and Base declare none,
> so there only direct-to-pool deposits are detected.** A deposit routed through an
> undeclared proxy is invisible, and the run reports "no Tornado deposits found" — a
> false negative, not a clean wallet. No proxy on those chains has been verified
> on-chain, and an unverified address must never enter the registry.

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
the count becomes unique. A count match is a signal **only at
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
| **Profile match (exact, multi-pool)** | one address received a wallet's *entire* fingerprint, e.g. `6×0.1 + 4×1.0` | single-wallet signal (`profile_match`); it admits a candidate and belongs to the amount+timing family, so on its own it is `weak`, and with a linked address it makes `strong` |
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
  depositor and the candidate (token transfers are read once per run).
- **Linked withdrawal sender.** `withdraw()` may be called by anyone, and the
  caller is recorded as the transaction sender. If the depositor, or one of its
  direct non-contract counterparties, sent a withdrawal transaction, its recipient
  is flagged `linked_sender` (linked-address family). The tool reads the outgoing
  transactions of at most 25 counterparties, busiest first, within the searched
  blocks only. It catches an operator that relays its own withdrawals when the
  relaying address is a counterparty of the depositor: in the KuCoin case it listed
  the 6 exits of that caller, and nothing else, but none of the 30 exits of the
  attacker's second caller, which the depositor never transacted with ([EVALUATION.md](EVALUATION.md)).
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
  forwarded within 3,200 blocks, short by at most 0.01 ETH or 1 % of a token amount
  (the forwarding test of Victor used by Tutela). A recipient in the window that
  also sent funds to one of these addresses is flagged `shared_deposit`
  (linked-address family), but only when the deposit address sweeps at least 80 %
  of its outflow to **labelled** exchange wallets: in the placebo test that version
  gave 27 real-window against 4 decoy hits, while deposit addresses recognised by
  activity alone were at chance (16 against 12). Those are shown as context. So
  without an attribution set the signal does not fire. The lookup costs up to 50
  history queries plus a busy check per new sweep target, and can be turned off
  (`--no-deposit-addresses`).
- **Early multi-pool profile.** A wallet that deposited at least 10 notes over two
  or more pools has a profile (notes per pool). A recipient that, in every one of
  those pools, received exactly that many withdrawals between the pool's first
  deposit and 72 hours after its last one is flagged `early_profile`. One pool is
  the count match again and is chance-level; over two or more pools and a short
  window the placebo test on 28,739 depositors gave 318 real-window against 34
  decoy hits (chance share 0.12). It belongs to the amount+timing family (it reads
  the same withdrawals as the count match) but, unlike the rest of that family,
  makes a lead on its own.

**Band, evidence and score.** Each recipient accumulates a signal set
(`count_match`, `self_relayed`, `early_profile`, `gas_price`, `linked`,
`linked_sender`, `shared_deposit`, and `profile_match` on `multi` runs). The signals
fall into evidence families: amount+timing (`count_match`, `self_relayed`,
`early_profile`, `profile_match`), gas price, linked address (`linked`,
`linked_sender`, `shared_deposit`). The **band** needs a lead signal — a linked
address or an early multi-pool profile: `strong` for a lead signal plus another
family, `moderate` for a lead signal alone, `weak` otherwise. The rule follows the
placebo test on real depositors ([EVALUATION.md](EVALUATION.md)): run on decoy
windows that end before the wallet's first deposit, the pipeline found
amount+timing and gas-price leads as often as in the real windows, and only
linked-address leads clearly more often (30 against 7 over 30 days; 19 against 1
within 72 hours); the early multi-pool profile was added in 2.15 on the same kind
of evidence. Before version 2.13 a gas-price match or a self-relayed count match
alone reached `moderate`, and two non-linked families reached `strong`. A
linked exit whose first withdrawal came within 72 hours of the deposit is marked
as an **early exit**; like the fresh-address mark it is context and does not
change the band or the score. Every candidate carries its **evidence**: each family, whether it
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
depositors only the linked-address family stands above chance. Under the previous
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
address is a statement about the pair. `self_relayed` is **not** — it is a
property of the withdrawal, identical for every wallet whose window contains it,
and since overlapping windows share count-matched recipients by construction and
zero-fee withdrawals are ordinary, crediting it would merge unrelated depositors
into a single "operator". A shared multi-pool consolidator unions unless it is
graded as a window-overlap artefact. Bare count-overlap never does.

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
share of the input the trace owns. A contract that pays nothing back (a bridge, a
deposit contract) ends that branch. The walk also stops at a labelled address, at
`--max-hops` (default 4), at an address that did not move the funds on, and after
`MAX_NODES` = 40 expanded addresses, which bounds the API calls. Amounts below 1 %
of the start are not followed.

FIFO is a convention: funds in one account are fungible, so an edge says where
the traced amount *would* have gone under that rule. It is a lead for
corroboration, like every other output.

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
