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

> **Twin pools.** Some denominations have two contracts: on Ethereum 1000 DAI, 50000 cDAI
> and 500000 cDAI (token pools), on Avalanche 10 AVAX and 100 AVAX (native pools). A deposit
> through a router (any declared router of the network, for example both Avalanche routers
> `0x171f…` and `0x0d55…`) names only the router. Since 2.23 the pool is read from the
> contract that emitted the Deposit event in the receipt, on every network and for every
> router, native and token alike (2.18 did this for Ethereum token twins only; before 2.18,
> and for Avalanche native twins before 2.23, such a deposit went to the first twin by
> registry order, whichever contract received it). A deposit the receipt cannot settle is
> marked `pool_ambiguous`: it is listed, not searched and left out of the note profile;
> while any deposit is `pool_ambiguous` the early multi-pool profile is not applied
> ("early profile not applied: pool uncertain"), because the remaining notes are only part
> of the fingerprint; and the Merkle-root check places it by looking its transaction hash
> up in each twin's tree, or, when that fails (and in decoy runs), leaves every pool it
> may belong to unchecked with the reason recorded.
>
> The early profile also needs the whole `[first deposit, last deposit + 72 h]` span of every
> pool read. With a search window under 72 h (`exit_window_hours`) it is not applied
> ("early profile not applied: window shorter than 72 h", also when the last deposit was
> less than 72 h old at the time of the run, `params.read_ts`, so the window ended at the
> chain head); nor when a pool's withdrawal list
> was cut off or only partly indexed by the explorer, or the pool's windows leave a hole in
> the span ("early profile not applied: withdrawal history incomplete"). Both notes are
> recorded in `heuristics.early_profile_note` and shown in the evidence.

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
precisely the most incriminating withdrawals. For one Ethereum
depositor, event mode returned 2,159 withdrawals in the 0.1 ETH window versus 1,905 for the heuristic, and one more
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
  and only fingerprints that include a lead are listed. Illustration with synthetic addresses: eight self-relayed exits to
  `0xAAAA…AAAA` all used a 3 gwei priority fee and gas limits 428,274–428,318, the same
  as two exits to `0xBBBB…BBBB`, while `0xCCCC…CCCC` used 0.15 gwei and ~535,400; the
  first two share a fingerprint, the third does not.

> Note: this toolkit deliberately does **not** report "nullifier double-spends".
> The pool contract enforces nullifier uniqueness at the consensus level, so a
> repeated nullifier can only ever be a data-source artefact, never a finding.

## 2b. Withdrawal side — transfer mode (legacy heuristic)

Tornado withdrawals appear as **internal transactions sent from the pool contract**. Each
withdrawal pays the recipient `denomination − relayer fee`. We keep payouts whose value
falls in the band `[denomination × fee_lo, denomination × fee_hi]` (default `0.90 .. 0.995`).

Each voucher gets its own search window rather than one window spanning a wallet's
first deposit to its last — see [Search windows](#search-windows). Block ranges are
resolved from timestamps; Etherscan caps a response at 10,000 rows, so a query that fills
all ten pages continues from the last block seen (inclusive, de-duplicated by transaction
hash and log index). A page shorter than 1,000 rows is not treated as the end, because
the explorer can return a short page in the middle of a result set; a list ends only on an
empty page. A single block holding more rows than the window pages stays cut off; the
client records it (`client.truncated`) and warns, so a missing row is not read as "no candidate".

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

**Failed lookups (since 2.22).** The depositor's own history is required: an error
there stops the run. Every other lookup is optional for the run but not for the
answer, and a failure is never read as "none found". Each failure becomes one record
`{"kind": "lookup_failed", "what", "address" or "tx", "error"}` in the run's
`errors` list (`tornado_demix/incomplete.py`); `run_demix`, `multi`, `characterize`
and `trace` results carry `errors`, `error_count` and `truncated` (the lists the
explorer cut off during that run, and since 2.23 the lists it answered while part of
the range was still being indexed: Blockscout status "2", reason `partial_indexing`;
the rows are kept, never cached, the run notice names "partial indexing" and no trace
ledger built on such a list is certain). The CLI prints "N lookups failed - results may be
incomplete" with the list, and the HTML report, the web UI and the JSON show the same
notice near the top. What is recorded: a counterparty whose history, busy check,
contract check or transaction read failed in the deposit-address classifier (it
answers `unknown`, not "not a deposit address", whenever the failed lookup could have
changed the outcome), a counterparty the `linked_sender` check could not check or
read, a failed `eth_getCode` or base-fee lookup, and a failed first-activity read.
A failed RPC lookup is no longer cached for the run: it is tried again on the next
use. The hold-out and placebo summaries count failed lookups per run, since a failed
lookup can change that side's counts. A transaction or receipt read (`tx_info`, `tx_receipt`) that neither the explorer's proxy module nor the RPC node answered raises instead of reading as an unknown hash, so a forwarder check answers `unknown`, a bridge deposit ends as `lookup_failed`, an unread twin-pool receipt is recorded and a broadcaster stays unverified. `cluster` never caches the layers of a run with failed lookups or cut-off lists and shows the same notice; in `multi` a failed funder busy check keeps the funder (unchecked) and is recorded; in the placebo a failed contract check makes the direct link `contract_unknown`. A window end whose block lookup failed is searched up to the current block and recorded as a note (`window_end_fallback`). CSV output adds `lookup_errors.csv` when something failed. The Merkle-root check keeps its own
`root_check_incomplete` flag and also records its failed lookups (a transaction not read, a tree that could not be built or has no insertion blocks, a leaf that was not found, `ROOT_HISTORY_SIZE` not read) in `errors` in the same record shape.

## 4. Cross-wallet correlation (multi)

Given several wallets we compute:

| Result | Definition | How much it says |
|--------|-----------|----------|
| **Profile match (exact, multi-pool)** | one address received a wallet's *entire* fingerprint, e.g. `6×0.1 + 4×1.0` | single-wallet signal (`profile_match`); it admits a candidate and belongs to the amount+timing family, so on its own it is `weak`, and with a linked address it is `moderate` (one lead source) |
| **Cross consolidator** | one address is a full-fingerprint match for 2+ wallets | graded: `strong` with an independent gas-price/linked signal, `moderate` for distinct fingerprints of wallets that did not deposit together, `weak` (window-overlap artefact) otherwise |
| **Synchronous deposits** | wallets whose deposits chain within `SYNC_GAP_HOURS` (6 h) | behavioural link in its own right |
| **Shared funder** | wallets funded, before their first deposit, by the same plain native transfer sender that is neither labelled nor busy (`BUSY_FUNDER_TXS` = 200 transactions or more) | edge `shared funder` in the operator graph; in the 27 MixLaunder cases 90.1 % of laundering deposit addresses shared an immediate funder with another deposit address of the case |
| **Shared count-matched candidate (single pool)** | a count-matched candidate shared by 2+ wallets | window overlap, not evidence (wallets with equal voucher sizes in similar windows share candidates by construction) |
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

### Group view (`multi --group`, exploratory)

When one actor splits funds over several intermediate wallets that each deposit, every
wallet's own voucher sizes and windows can miss the pattern: a recipient that received
the group's combined notes matches none of the per-wallet voucher sizes. `--group` (web
UI: the "Group view" checkbox under multi) adds a separate **Group view** next to the
per-wallet results:

- the wallets' deposits are merged into one depositor (each merged deposit keeps the
  wallet it came from) and clustered into vouchers with the usual `--gap-hours` and
  `--max-voucher-span`;
- each pool is searched over **one** window, from the group's first deposit into it to
  the window end after its last one (`--window-days` or `--exit-window`);
- the existing rules run on the merged notes, unchanged: the count-candidate gate
  (`MIN_FIELD_SIZE`, `MIN_COUNT_DISCRIMINATION`), the voucher-order check, the gas-price
  signal and the early multi-pool profile. A direct link counts against the union of the
  wallets' filtered counterparties and the wallets themselves, with the usual
  `EARLY_EXIT_HOURS` limit. A withdrawal to one of the merged wallets itself, within
  that limit after a group voucher, is therefore a `linked` lead (a withdrawal back to
  the asserted actor). A direct transfer between two merged wallets only makes members
  counterparties of each other; it gives no third-party recipient a link (both cases are
  tested). Withdrawal senders and shared deposit addresses (context only) are not
  recomputed for the group;
- the bands are those of the per-wallet runs (`confidence_band`): the group view raises
  a band only when a lead rule fires on the merged notes, typically the early profile
  of a group whose merged profile spans two pools and ten notes.

**Merging is the analyst's assertion that the wallets are one actor; the group view is
conditional on it.** The tool does not test the assertion, and merging unrelated wallets
produces count matches and profile matches by chance. Every output (CLI, HTML report,
web UI, JSON `group` key) shows this caveat next to the section. The per-wallet results,
the shared candidates, the consolidators and the operator clusters are computed exactly
as without `--group`. A failed read of a group window lists the pool as not searched and
counts as a failed lookup (`wallet` = `group`); it is not fatal. Cost: one block lookup
pair and one log read per pool the group used. A group window longer than
`GROUP_LONG_WINDOW_DAYS` (90 days; the wallets deposited far apart) gets a notice in
every output (`group.notices`): the recipient field is then large, a count match rarely
passes the gate and the read is costly. Not measured on labelled data.

**Exact-k detector (`--exact-k`, context only).** For every pool where the group
deposited k >= 2 notes, the detector reports how many recipients received exactly k
withdrawals inside the group window (*observed*, from the events already read) and the
same count in `--exact-k-windows` earlier windows of the same length in the same pool
(default 6, at most 20), back to back and not overlapping the group window. The base
windows of one pool together span at most `EXACT_K_MAX_BASE_DAYS` (365 days): a longer
group window gets fewer of them, none when one window alone exceeds a year, and the row
says how many were dropped (`capped_windows`). Observed and base counts both count only
withdrawals timed inside their window, so a withdrawal in a shared boundary block is
counted once. A window with no withdrawal at all (before the pool existed) is not
comparable and is skipped (`empty_windows`). Reported: observed, the base counts and
their mean, and the smoothed empirical tail P(>= observed) = (x + 1) / (n + 1), where x
of the n usable base windows hold at least the observed number; the outputs print x and
n next to it, so 1 / 7 with six windows is not read as "impossible". Biases: a base
window only partly inside the pool's life, or cut off by the explorer's list limit,
holds fewer withdrawals and pushes P down; the observed count may include the group's
own lead. It is never scored and never a lead. Cost: per pool, up to `--exact-k-windows`
extra log reads (each two block lookups plus as many pages as the window holds); with a
30-day window and 6 base windows that reads six months of the pool's withdrawals, at
most a year in any case.

**Multi JSON (`multi --json`, web UI JSON button).** Top-level keys: `tool`,
`generated_utc`, `wallets`, `incomplete` (the one-line notice or ""), `errors` and
`truncated` (failed lookups, each tagged with `wallet`, `group` for the group view, and
cut-off lists), `shared_candidates` (`[pool, address, wallets]`, window overlap, not
evidence), `soft_overlaps`, `fingerprints`, `profile_matches`
(`wallet -> [[address, {pool: [received, needed]}, exact]]`), `cross_profile`,
`consolidator_grades`, `sync_groups`, `shared_funders`, `operator_clusters`,
`per_wallet` (`wallet -> {candidates, result}`: the ranked candidates and the raw
run_demix result) and `group` (`null` without `--group`; otherwise `caveat`, `wallets`,
`profile`, `candidates`, `exact_k` rows, `exact_k_caveat`, `notices` and the raw
`result`).

**JSON honesty fields and `cluster --json`.** Every JSON export (demix, multi, trace,
classify-address, characterize, cluster) carries `limitations`, `exploratory` and
`exploratory_banner`. The cluster JSON (`cluster --json`, web UI `cluster.json`) is an
object `{tool, limitations, exploratory, exploratory_banner, traces}`; before v2.23 the web
UI file was a bare list of traces, so a reader of that file must take `traces` now.

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
  the check cannot be made (a failed `eth_getCode`, or no RPC endpoint) the address
  gets `linked_unknown_contract`, context, and the failure is recorded in the run's
  errors (since 2.23; before, the signal was kept). A withdrawal back to the depositor's
  own address is `linked` as well, and so is a direct ERC-20 transfer between the
  depositor and the candidate (token transfers are read once per run, on every network
  since 2.22). A token row counts only when the depositor really took part in it
  (since 2.22): an outgoing transfer with value in a transaction the depositor sent (or
  a sponsored EIP-7702 call to it), or an incoming transfer with value of a token the
  depositor later moves in one of its own transactions. Anyone can make a token
  contract emit a Transfer naming any sender and recipient (a fake token, a zero-value
  `transferFrom` of a real one), in their own transaction and at any time, so a raw
  row would let a third party attach any recipient to any depositor as a lead. The
  price: a genuine token payment the depositor received but never moved itself is not
  a link. Native transactions count unless they failed (`isError` 1 or receipt status
  0); an incoming one counts only with value, even 1 wei (it costs the sender and
  needs its key, while a 0-value transaction can be sent to anyone, as address
  poisoners do). Internal transfers count unless they failed. Such rows make a
  counterparty; since 2.23 a counterparty is a direct link (`linked` or `linked_late`)
  only when one of its linking transactions moved real value **before the first
  withdrawal to it** (in any pool, strictly earlier): native coin or an internal transfer
  of at least the network's dust floor (`networks.DUST_FLOOR_WEI`: 0.001 ETH on Ethereum,
  Optimism, Base and Arbitrum, 0.001 BNB, 1 POL, 1 xDAI, 0.1 AVAX), or a transfer of a
  listed real token at or above its floor (`data/real_tokens.csv`: the Ethereum pool
  tokens, USDC from Circle's list, USDT from Tether's, WETH/WPOL, each with its source
  URL; floors such as 1 USDC or 0.001 WETH). Otherwise the candidate gets a context
  signal that says why: `linked_dust` (zero-value or below the floor), `linked_unverified_token`
  (a token off the list: a fake token can emit a Transfer naming anyone inside the
  depositor's own transaction), `linked_posthoc` (real value only after the withdrawal:
  whoever controls the depositor can create such a link afterwards). These are excluded
  by construction, not measured. The evidence of every direct link cites one transaction
  (the earliest link that counts, else the earliest real-value one, else the earliest)
  with its value, asset, direction, block, UTC time and position against the first
  withdrawal ("0-value tx after the first withdrawal to it"). The floors and the token
  list are judgement calls: a genuine payment in an unlisted token is context. The floors
  are roughly equal in value, about 0.001 ETH each at CoinGecko prices of 2026-11-14
  (0.001 ETH; 0.003 BNB; 20 POL; 2 xDAI; 0.25 AVAX; tokens 2 USD of a stablecoin,
  0.001 WETH, 0.00003 WBTC); prices move and the floors do not. **The floor is the price
  of a planted link, not a barrier**: an outgoing transfer above it needs only the
  depositor's key, so whoever controls the depositor can create one to any address for
  about the floor plus the gas of a transfer (about 2 USD on every network at the
  floors above, plus gas), for example to addresses that often receive
  withdrawals. In a decoy window (placebo test) each link is compared at its time minus
  the deposit shift, so the decoy applies the same timing relative to the deposits as
  the target. Since 2.17
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
  is flagged `linked_sender`. At most 25 counterparties are read, busiest first;
  since 2.23 failed rows and zero-value incoming rows are not counted, so spam cannot
  fill those slots. Since 2.19 this is **context only**: it is shown in
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
  and Tornado contracts are skipped unless the label itself names a deposit address,
  other contracts are rejected when checked) and takes one for a deposit
  address when it has fewer than 1,000 transactions and at most 300 senders, and at
  least 80 % of its outgoing transfers go to hot wallets (95 % when it has more than 50
  senders): among its three most
  frequent destinations, those labelled as an exchange or with at least 10,000
  transactions. A busy address counts as a hot wallet only if it is not a contract
  (a token, a DEX router or the Tornado router is busy too) and receives amounts
  forwarded within 38,400 seconds (3,200 Ethereum blocks of 12 s; the window is a
  duration, taken from the row timestamps, and falls back to 3,200 blocks only for a
  row without one), short by at most 0.01 ETH or 1 % of a token amount (the
  forwarding test of Victor used by Tutela); a sweep may also forward the sum of
  several incoming transfers of the same asset received in the window and after the
  previous outgoing transfer of that asset, with the same tolerance. A target labelled
  as an exchange needs evidence of a sweep too since 2.23 (the label names the exchange,
  it is not evidence of a sweep), under a balance-sweep rule of its own
  (`sweeps_balance_to`), because exchanges batch their sweeps (daily or less often) and
  may leave a fee or dust behind: an outgoing transfer of an asset to the labelled
  wallet counts when it moves at least 90 % (`LABELLED_SWEEP_SHARE`) of the value of
  that asset the address received since its previous outgoing transfer of that asset (to
  anyone; several deposits swept together add up), and happens at most 30 days
  (`LABELLED_SWEEP_SECONDS`; 216,000 blocks for a row without a timestamp) after the
  latest of those receipts. A transfer that passes the forwarding test above counts as
  well (deposits forwarded one by one, each within 38,400 s), so a label never makes the
  test harder than for an unlabelled wallet. Both values are exploratory judgement calls,
  not measured on a labelled set; one such transfer is enough. In 2.22 a labelled target was accepted
  on the label alone, so an ordinary wallet that received 1 ETH and paid 1 wei to a
  labelled exchange years later came out as an `exchange_deposit` with `evidence`. An
  address whose outgoing transfers reach the sweep share only by counting payments to
  labelled exchange wallets that are not such sweeps (a small share of what arrived, or
  more than 30 days later) is now `paid_exchange` (see Kinds). Counting rows:
  a token transfer is one transfer (the `txlist` call to the token contract, value
  0, is not counted next to the `tokentx` row), zero-value transfers are ignored,
  and a token transfer out of the address is kept only when its transaction is one of
  the address's own: sent by it, or sent to it (an EIP-7702 account whose sponsor pays
  the gas calls the account itself; a call to a plain EOA runs no code). Anything else
  is dropped (address poisoning by `transferFrom` spoofing). When the explorer's
  `txlist` does not list such sponsored calls, they are found by their transaction
  target and the test is run again with them (`sponsored`). The depositor's own
  counterparties are filtered the same way, and its token transfers are read on every
  network (before 2.22 only on chains with token pools, so a USDC or USDT deposit
  address on Polygon, BSC, Arbitrum, Optimism, Base, Gnosis or Avalanche was never
  examined): spoofed "from the depositor" transfers to look-alike addresses cannot take
  one of the 25 slots. Senders (since 2.22): an incoming transfer counts only when it
  is non-zero and the address later sweeps the same asset, so a fake token (which can
  forge any "from", even an exchange hot wallet) does not make a sender. Since 2.23 the
  transfer must also reach the dust floor of its asset, the same floors as the direct-link
  rule: the native coin at the network's floor (0.001 ETH on Ethereum, 20 POL on Polygon,
  see `networks.DUST_FLOOR_WEI`) and a listed real token at its floor in
  `data/real_tokens.csv`; an unlisted token needs a non-zero amount. So a 1-wei spoof or a
  small top-up does not make a sender even when the address later sweeps that asset
  (in 2.22 it did when the address swept that asset, although the text said otherwise).
  A per-customer address can have a few hundred one-off senders
  (exchange deposit addresses with 56 to 142 were seen); a busy hub pays out to many
  destinations, a deposit address forwards what it receives, hence the 95 % sweep share
  above 50 senders. An address with more than 300 senders is not classified (it is taken
  as a hub and dropped, not given a separate status); the cap is unchanged in 2.23, and
  with the floors dust can no longer push an address over it. The 300 / 50 / 95 % values
  are judgement calls, not measured on a labelled set. A spoof routed through a contract the address itself called is not
  detected. An account
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

  Kinds (since 2.21). Every listed address has a `kind`. `exchange_deposit` is the rule
  above: a sweep target is labelled as an exchange, receives a swept balance (since
  2.23, the labelled rule above) and none has another category, and
  only a sweep to labelled exchange wallets is `evidence`; this is the only kind that
  was ever a lead (since 2.21 it is context too). `deposit_like` (since 2.22) has the
  same sweep behaviour but only unlabelled busy sweep targets: whose deposit address it
  is, is not known (`service: unknown`), so the word "exchange" is not used; before 2.22
  it was shown as an unscored `exchange_deposit`. Only these two kinds can share a
  deposit address between two addresses, and `deposit_like` only as context.
  Exchange names from several labels are case-folded and deduplicated ("Binance,
  binance" is "Binance").
  `paid_exchange` (since 2.23) is an address that paid a labelled exchange wallet without
  sweeping what it had received (no payment of at least 90 % of what arrived since the
  previous outgoing transfer of that asset within 30 days of the latest arrival, and none
  that passes the forwarding test): a
  customer paying an exchange, not an address the exchange controls. It is reported only
  where the 2.22 rule would have accepted it (sweep targets and paid targets together
  reach the sweep share), names the exchange and the paid wallets (`paid_targets`), has no
  senders, no gas funder and no `sweep_target`, is never `evidence`, never scored and
  never links two addresses (not even as context). In `demix` such a counterparty is
  listed apart from the deposit addresses (CLI `[paid]`, report row "paid an exchange",
  not counted as a deposit address in the log line or the section count). An
  operator-executed forwarder found for the same address outranks it.
  `service_deposit` has the same sweep behaviour but a labelled sweep target of another
  category (gambling, bridge, payment, ...): it is shown with the service name, never
  scored and never linked. `forwarder` is a per-account forwarder whose token outflows
  are executed by an operator or relayer instead of the address: at least 80 % of the
  address's non-zero outgoing real-token transfers (any token contracts) are in
  transactions the address did not send, in at least two distinct transactions, and up
  to three of them read with `tx_info` share one `to` contract (the operator);
  zero-value transfers never make a forwarder. When that operator is a public ERC-4337
  EntryPoint (v0.6, v0.7 or v0.8) the kind is `smart_account` instead: the user's own
  account run through a bundler, not a service address. A contract account (ERC-4337 accounts normally are contracts) is read too, but only these two kinds can be returned for a contract. A forwarder whose operator is the token contract itself (a fake-token spoof) is rejected. A public DEX router or aggregator (1inch, 0x, Uniswap, Paraswap, CoW, LI.FI, Odos, KyberSwap, MetaMask Swap; `tornado_demix/data/dex_routers.csv`, one source URL per row) is never the operator: such transfers are swaps or approvals of the address's user. The 80 % share is counted over transactions, not transfers. Both kinds are never scored and
  never link two addresses. This pass reads the outgoing
  token rows before the poisoning filter, which stays unchanged for the other two
  kinds. A forwarder also needs at least one of the tokens the operator moves to have
  been received by the address (a poisoner's batch contract "moves" only fake tokens
  the victim never held), and its senders are those who sent such a token. When the
  `to` of those transactions is the address itself they are its own sponsored EIP-7702
  calls, not a forwarder (see above). Exchange, service and deposit-like addresses also
  record a `gas_funder` (context, not scored): the sender of the first native transfer,
  if it is below the network's threshold and precedes the first token sweep. The
  thresholds are fixed amounts per chain in `networks.GAS_FUND_WEI`, no price feed:
  0.01 ETH on Ethereum, Arbitrum, Optimism and Base, 0.01 BNB, 2 POL on Polygon (exchange
  top-ups of 0.0155 to 1.6 POL were seen; 0.01 found none of them), 1 xDAI and 0.5 AVAX;
  apart from Polygon these are estimates. A result whose token history hit the
  1,000-row cap is marked `incomplete`. A failed transaction (`isError` 1 or receipt
  status 0) moved nothing: since 2.23 its rows count in no test of the classifier
  (senders, sweeps, forwarding, gas funder, forwarder), in either direction; the
  failed transactions the address sent are shown apart as `failed_sends`.

  Checking one address. `tornado-demix classify-address <address> --network N
  [--labels-blockscout]` (and "classify" in the web UI) runs the same classifier on a
  given address and shows the kind, sweep targets, senders and gas funder, with an HTML
  report and JSON. `characterize` shows the same check for the address and for each
  next hop it lists (`--no-deposit-check` skips it; two history queries per address
  plus a busy check per unlabelled sweep target). The check describes behaviour, not
  identity. Without labels the classifier misses
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
profile. Lead signals come from two different families, whose independence is not established: an early direct link
(`linked`) and an early multi-pool profile (`early_profile`). `strong` needs lead
signals from both of them, `moderate` a lead signal from one, and `weak` is
amount+timing, gas price and/or context-only signals. Amount+timing and gas price never raise the band; they
are shown as context. The rule follows the
placebo test on real depositors ([EVALUATION.md](EVALUATION.md)): run on decoy
windows that end before the wallet's first deposit, the pipeline found
amount+timing and gas-price leads as often as in the real windows, and only
linked-address leads clearly more often (30 against 7 over 30 days; 19 against 1
within 72 hours, measured before 2.17, and counting the withdrawal sender, which 2.19 removed); the early multi-pool profile was added in 2.15 on the same kind
of evidence. Version 2.16 added the two-family rule for `strong`: among
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
block) keeps the withdrawal and is counted as "not found". So does a root that the pool no longer
accepted at the withdrawal block, and a transaction without a single decoded withdraw root (see below).

**Which root counts.** The root is the `_root` argument of the decoded `withdraw` call that
reached the pool (`tornado_demix/withdraw_root.py`): the pool's
`withdraw(bytes,bytes32,bytes32,address,address,uint256,uint256)` called on the pool, the
router's `withdraw(address,bytes,bytes32,...)` called on a router the network declares with
`_tornado` equal to the pool, or either of them inside a Multicall wrapper (Multicall3/Multicall2
`aggregate`, `aggregate3`, `aggregate3Value`, `tryAggregate`, `blockAndAggregate`,
`tryBlockAndAggregate`, and `multicall(bytes[])` on the same contract), decoded recursively up
to four levels with each sub-call's own target. A decoded call counts only when its
`_nullifierHash` is the withdrawal's nullifier. Several different decoded roots
(`roots_ambiguous`) or no decoded call (`roots_undecoded`, shown as "no withdraw call decoded")
leave the root unknown and the withdrawal is never excluded. So does a matching call inside a
frame that lets it fail without reverting the transaction (`aggregate3`/`aggregate3Value` with
`allowFailure`, `tryAggregate`/`tryBlockAndAggregate` with `requireSuccess` false,
`multicall(bytes[])` of an unknown contract, a wrapper selector called on an address that is not
a known Multicall deployment of the chain, or anything nested in one): it may have reverted
while the real withdrawal ran elsewhere (`roots_unverified`, "call may have failed"). A call in a
strict frame must have succeeded, and one nullifier is spent once, so a strict root is used even
next to such calls. Decoding is bounded per transaction (calldata size, calls visited, bytes
sliced); calldata past the bound counts as undecoded. Known Multicall deployments are listed
with their source in `tornado_demix/data/multicalls.csv`: Multicall3
(`0xca11bde05977b3631167028862be2a173976ca11`, the same deterministic address on every supported
chain) and, on Ethereum only, Multicall2 and Multicall (MakerDAO). Any other contract can answer
to a strict wrapper selector and still ignore a failing sub-call, so its frames are treated as
able to fail. The old search for the 32-byte
word before the nullifier is kept only as a diagnostic: a wrapper can place an older root right
before the nullifier in a call that is not the withdrawal and build the real call elsewhere, so
a root found that way is never used. A decoded root is then accepted only when the pool still
accepted it at the withdrawal block: it is in the rebuilt tree's root index, it was not
produced after the block, and fewer than `ROOT_HISTORY_SIZE` insertions happened after it (the
pool keeps a ring of the last `ROOT_HISTORY_SIZE` roots; insertions of the withdrawal's own
block are counted in the way that favours acceptance); otherwise it is counted in
`roots_outside_history`.

**History size per pool.** `ROOT_HISTORY_SIZE` is read from each pool contract with `eth_call`
once and cached in `.cache/merkle/<chain>_<pool>.history.json`. It is not the same for every
pool: 8 of the 28 Ethereum pools keep 30 roots, the others 100, and the Polygon pools keep 30. If the
contract cannot be read, no default is substituted (a guess below the true size would reject a
real older root): the pool is left unchecked and a `ROOT_HISTORY_SIZE` entry is recorded in
`errors`, shown in every view. The tree index stores the block of every
insertion (`blocks` in the cache); a cache written before that is completed from the deposit logs
once, without rebuilding the tree.

**How the pieces are obtained.** The
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
last `ROOT_HISTORY_SIZE` roots (a per-pool constant, read from each contract, see above; the ceiling
below was computed for the pools it lists, the 0.1, 1, 10 and 100 ETH pools with 100 and the Polygon
pools with 30), so a withdrawal proven against a root older than the wallet's newest note is
the only kind that can be excluded. In the cached hold-out windows at most 11.01 % of
Ethereum and 10.94 % of Polygon withdrawals could be excluded at all (`tools/root_ceiling.py`,
upper bound, newest note only, measured before 2.23 with one history size per chain; the tool
now reads it per pool and a re-run is planned; see [EVALUATION.md](EVALUATION.md)). Most withdrawals in a
window are made after the wallet's notes are in every accepted root, so the exclusion cannot
decide them; it can only remove those that are old enough.

**Limits.** The exclusion tests the wallet's *notes*, not its funds: a withdrawal proven
against a newer root is not excluded, whoever made it. A withdrawal whose calldata holds no decodable
`withdraw` call to the pool (an unknown wrapper contract, a call built inside another contract)
is not excluded. Candidates
admitted only through a count that the removal changes are re-evaluated once; a candidate that
would newly appear after the removal is not looked for.

The evaluation tools log every test they compute to a hash-chained register
(`tornado_demix/registry.py`, see "Test register" in EVALUATION.md), so the number of
tests actually run, exploratory sweeps included, is known when results are corrected for
multiple comparisons. The hash chain proves integrity only relative to an external witness
taken earlier (a copy of the chain head kept elsewhere); it is not proof of pre-registration.
A confirmatory claim needs a public timestamped witness, frozen parameters and a data
manifest made before the data were read.

A record is marked `confirmatory` only when the protocol lock lists the SHA-256 of the script that produced it and of every `tornado_demix` module loaded when it was written. That is all the status says: helper modules beside the script under `tools/`, the inputs, parameters and data are not covered by it.

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
each attributed transfer is an edge to the next address.

**One ledger per address.** Every (chain, address, asset) the funds reach has one
ledger. All traced inflows into it are merged, each tagged with the branch it came
by (the arrival transaction and its sender), and the address's events are replayed
once in chain order (block, then transaction index, then the position inside the
transaction, see below). FIFO starts at the first traced arrival: an outgoing
transfer before it carries none of the traced funds, and FIFO lets an outgoing
transfer take a traced lot only when the lot is known to have arrived before it, so
no edge comes from a transfer earlier than the arrival that funds it, also inside
one transaction (2.23). The
traced share of each outgoing transfer is split back across the incoming branches
by the lots FIFO consumed, oldest first (`sources` on the edge: arrival
transaction, sender, amount); an edge's `hop` is one more than the shortest
branch in it. A transfer reached by several branches (a diamond A→B, A→C, B→D,
C→D) is therefore one edge with the merged amount, and the sum attributed to one
transaction never exceeds its value. Histories are fetched breadth first, a whole
level at a time in a fixed order (hop, time of first arrival, address), and after
each level the replay runs again over all fetched histories merged in time order
(chains interleave by timestamp, so a bridge fill comes after its deposit); an
address reached again by a later branch is simply replayed with the merged ledger
and is expanded (fetched) once. A replay queues an address's events only while the
address holds traced funds or has traced funds pending; the events of an address
without them are plain arithmetic on its own ledger (no outflow can carry traced
funds) and are caught up, cached, when funds reach it or the replay ends, and a
history is sorted again only when a newly read one changes its order. The results
are the same as replaying every event (tested on random graphs); a 40-level chain
with 10,000 events per address takes about 5 s instead of about 65 s. An internal
transfer out of an address (a contract
wallet) is an outgoing transfer like any other. An internal transfer has no
transaction index of its own; it takes the index of any fetched normal or token
row of the same transaction, zero-value and failed rows included (the zero-value
call that makes a contract wallet pay gives its payment the call's index). When
no such row exists the order of that transfer inside its block is unknown, and
then: FIFO never lets an outflow take a lot that is not known to have arrived
before it (same block, an index unknown), so no edge comes from a transfer earlier
than its funding arrival; an address with such a block, once it holds traced
funds, is marked `history_incomplete`, so nothing on it reads as certain; and a
traced share credited after the replay already passed the recipient's inflow
(the sender's transfer sorted later) marks the recipient incomplete when one of
its outflows was replayed in between or the credit exceeds its balance. The cost
is that such an outflow can be missed as an edge (its funds stay at the address).
When the recipient's history does not list the inflow at all, the amount is
credited at the time of the sending transfer.

**Order inside one transaction (2.23).** One transaction can move the asset into
and out of an address several times (a contract that pays out its older balance and
is then paid the traced funds, or the reverse). An equal transaction index says only
that two transfers share a transaction, not which came first; before 2.23 the inflow
was taken as first, so an outflow could spend traced funds that arrived later in the
same transaction. Now each event keeps its position inside the transaction: the
top-level value of a normal transaction (it moves before every internal call of that
transaction), the `traceId` of an internal transfer (a path such as `0_1_2`, compared
component by component as integers, so a call's value moves before its sub-calls;
without a `traceId`, Blockscout's flat trace `index` of the call, compared as a number
and never against a `traceId` path: a transaction whose internal rows are placed in
more than one way (paths, flat indexes, or neither) has all its internal places unknown), the `logIndex` of a token transfer. Routescan rows are read
the same way (a `traceId` when present); their field set was not checked against a
saved response. An outflow takes a traced lot of its own
transaction only when both positions are known and comparable and the inflow's is
smaller (a top-level inflow before any internal outflow, a smaller `traceId` path, a
smaller `logIndex`); the replay sorts the transaction's events by these positions, so
an outflow known to come first is replayed on the balance before the inflow and
carries none of it. When a position is missing or the two are not comparable (an
internal transfer without a readable `traceId`, a token transfer without a
`logIndex`, a native position against a token one, an output paid back by a swap or
fill whose place is not read), the outflow does not take that lot under FIFO: the
amount stays at the address and the part such an outflow can hold ends as
`order_unknown` with the reason "order inside one transaction unknown" (`order_note`
on the end point, `same_tx` on each listed transfer), shown in the CLI, the report
(stops table and page 1), the JSON and the web UI. The bounds `[lower, upper]` stay
sound: with an unknown order the inflow is counted first, which can only widen
`upper`, and can only lower `lower` of that outflow (it is the same when the inflow is
fully traced; the untraced part of a partly traced inflow counts as other funds that
may have paid the outflow); only the FIFO edge is withheld.

Etherscan's `tokentx` rows carry no `logIndex`, so on a token trace every token
transfer into and out of one address inside one transaction has no known order and
ends as `order_unknown` (in practice contracts only: smart accounts, forwarders, batch
calls; an EOA does not receive and pay in one transaction). Many `order_unknown` ends
on a token trace through such contracts are therefore expected; the order can only be
recovered from the transaction's receipt logs, which the trace does not read for this.
Likewise a swap or same-chain fill output that lands at an address paying out again in
the same transaction (a smart-account or contract batch) ends as `order_unknown`: the
place of the output inside the transaction is not read.

**Several inflows of the traced transaction.** When the address receives more than one
inflow in the transaction that carries the traced funds, the traced one is singled out
by the sender of the previous hop's edge, then by its place inside the transaction (the
same explorer row as that edge's outflow), then, at the start address, by the traced
amount. When more than one still qualifies, which inflow is traced is not known: the
funds are credited at the earliest of them with their place inside the transaction
unknown (so no outflow of that transaction takes them under FIFO; the part one can hold
ends as `order_unknown`) and the address's history is marked incomplete, so `lower` is
0 and nothing on it reads as certain. At the start address `arrival_matched` is then
`ambiguous_in_tx`, and the CLI, page 1 (a warning) and the web UI say that the arrival
transaction holds several inflows that can carry the traced amount.

**Downstream bounds.** The next address is credited with the FIFO amount for FIFO,
but with the edge's bounds `[lower, upper]` for its own bounds (`t_lo += lower`,
`t_hi += upper`; sound because the recurrence is monotone in T). So a hop is
"certain" only when the edges before it are: an edge that is only possible
upstream cannot become certain downstream. A transfer into a
contract that pays the sender back in the same transaction (a DEX swap: an
internal transfer or a token transfer to the sender in that transaction) is
resolved: the trace continues from the sender with the swap output, scaled by the
share of the input the trace owns. A contract that pays nothing back (a deposit
contract, a bridge the tool cannot decode) ends that branch. The walk also stops at a labelled address, at
`--max-hops` (default 4), at an address that did not move the funds on, and after
`MAX_NODES` = 40 expanded addresses (one per address and asset), which bounds the
API calls. End points at the same address for the same reason are merged.

**End states (since 2.22).** Every traced unit ends exactly once, so the end amounts
add up to the start amount (rounding aside) unless a swap or bridge changed the asset;
the tests check this on random transfer graphs with failing lookups. Besides the
reasons above:

- `lookup_failed`: the history of an address (or a swap or bridge receipt lookup)
  could not be read. Only the branches reaching it end there, with their amount; the
  rest of the trace goes on and the result is always returned and written. A
  rejected API key still stops the trace. A failed bridge receipt or fill lookup
  ends as `lookup_failed` too (the bridge, its destination chain and the state stay
  on the edge and the end point) and is listed under `errors`.
- `below_threshold`: a branch under 1 % of the start amount (`DUST_FRACTION`) is not
  followed but ends with its amount: an arrival under the threshold, an outflow of
  an address that holds less than the threshold, or a remainder under it left at an
  address. Amounts under 10^-9 of the start are rounding noise and are dropped.
- `tornado_redeposit`: a transfer into a Tornado pool or router of that chain (from
  the networks registry), whatever its amount; it is checked before labels and the
  threshold.
- `order_unknown` (2.23): traced funds still at an address under FIFO that an outgoing
  transfer in the same block may have taken, because the order of that transfer against
  their arrival is not known (an internal transfer without a transaction index of its
  own, see above; or, inside the arrival's own transaction, a position missing or not
  comparable: "order inside one transaction unknown", see "Order inside one
  transaction"). FIFO cannot give them to it, so before 2.23 they read as "not moved
  on"; now the part such transfers can hold (at most their value) ends as
  `order_unknown`, the transfers listed with it (recipient, transaction, value), and only
  the rest as not moved on. The end amounts still add up to the start amount.

**Possible outflows (2.23).** On an incomplete history (overdraft, cut-off list,
same-block order unknown) an outgoing transfer that FIFO gives none of the traced funds
can still hold some: up to the traced funds credited to the address up to its block.
A transfer known to come before every traced arrival at the address (an earlier block,
or the same block with both transaction indexes known and smaller, or the same
transaction with comparable positions and a smaller one) is left out, and so
is one that FIFO did give traced funds to (an amount under the threshold ends as
`below_threshold` without an edge). Such transfers are listed in `possible` (lower 0, that
upper, attributed 0, status "possible (history incomplete)", kind `possible`), in time
order, the first 25 listed
and the rest counted in `possible_more` (`count`, `value`). They are not followed and
carry no end amount; the report lists them under "Possible outflows (history
incomplete)", page 1 counts them, and the CLI and the web CSV list them too.

A replay whose bounds have not settled at its cap (one replay per address plus three)
is used as it is and recorded in `errors` with kind `replay_unsettled`.

**Bridges.** Every transfer to an address of the bridge table is a bridge hop; this is
checked before the swap rule, so a listed bridge is always named. A listed bridge
whose deposits are not decoded ends the branch as "bridge <protocol> (not followed)"
(edge kind `bridge`, state `not_followed`). Only a row of kind `aggregator` (LI.FI,
Socket, Rango, Squid) can be a same-chain swap: when it pays something of material
size back to the sender in the same transaction and no bridge contract of the table
(kind `contract`) logged an event in that transaction, the trace goes on with the
output. Material means a payment in another token than the one sent (any amount), or
native coin above the native value the sender attached to the transaction it sent (the
transaction object's `value`; 0 when the traced address did not send the transaction,
for example a contract wallet). Known limit: a payment in another token has no size
rule (without prices it cannot be compared with the transfer), so a dust refund of a
third token still reads as a swap. A native payment back within that value is a refund of
unused bridge fees: the aggregator may have bridged through a bridge that is not in the
table, so unless an Across deposit is tied the branch ends as "aggregator <protocol>
(bridge or swap unknown)" (state `bridge_or_swap_unknown`, a bridge badge). With a
bridge event in the receipt the payment back is taken as a refund of unused fees or
dust and the aggregator is named as a bridge. A bridge row (kind `contract` or `maker_eoa`) is
never read as a swap: bridges such as Stargate refund unused messaging fees in the
native coin, and without a price feed a refund cannot be told from a swap output by its
size. Only Across contracts and aggregators are checked for an Across deposit; a
bridge of another protocol is named without reading its receipt. An Across deposit is found by decoding the Across deposit logs in the
receipt of the outgoing transaction, not by the recipient address alone, and it must be
tied to the traced transfer: same token; for a transfer straight to the SpokePool the
depositor is the traced address and the deposit's input amount equals the transfer;
through a periphery, router, aggregator or unlisted contract the input amount is at
most the transfer and at least 90 % of it (`TIE_FEE` = 10 %; the stored fixtures show a
1.0 % LI.FI fee and none for the periphery), and the depositor is the traced address or
the contract it paid. A contract that deposits in its own name is taken only on a
stronger tie: the traced address called it directly (it sent the transaction and the
contract is the transaction's `to`, or the deposit lies in a successful ERC-4337 user
operation of the traced address, see below) and no other inflow of the same token into
the contract is logged in that call (the traced transfer's own Transfer log is allowed
once). A deposit in the name of a third address is never taken (the sender may be a relayer or
batcher), and each deposit is taken by one transfer at most. A deposit of the traced
address that is not tied (a tip or fee in the same transaction, a second transfer, an
amount off by more than the bound) ends the branch as "bridge across (deposit not tied
to this transfer)" at an Across contract or a listed aggregator; at an unlisted contract
it keeps the plain end "contract without a payment back". A router depositing in its
own name for someone else in a transaction the traced address sent is therefore taken
only when nothing else of that token reached it in the transaction (a router pooling
several users' funds in one call is not tied). This covers direct SpokePool calls, the SpokePoolPeriphery
(`0x97ccdbea4632140639ad5ea9b944aa034eb15fd4`), aggregators such as LI.FI that deposit
into Across, and any unlisted contract that paid nothing back (its receipt is read
before the branch ends as "contract without a payment back"); the edge records the
contract it went through as `via`. When a router keeps a fee before it deposits, the
amount carried over is the fill's output over the larger of the deposit and the
transfer. The deposit event of the origin transaction (`V3FundsDeposited` or
`FundsDeposited`) carries the destination chain id, a deposit id, the depositor and
the recipient; the destination SpokePool's `FilledRelay` / `FilledV3Relay` event
repeats the origin chain id and the deposit id as indexed topics, so the fill is read
with one `getLogs` query on the destination chain (any chain of the networks registry)
and no bridge API. The walk continues from the fill recipient, in the token the fill
paid (the native coin when a wrapped native token is paid out), with the amount the
fill delivered (the traced share of the deposit, after the bridge fee); the hop count
carries on and every edge and end point names its chain. A deposit that is not
followed ends with one of five states (the end reason; protocol, destination chain
and deposit id stay on the edge and the end point): `fill_not_found` (no fill in the
search window), `deposit_expired_possible_refund` (no fill and the fill deadline has
passed: Across refunds an unfilled deposit after its deadline, so the funds may be
back with the depositor), `destination_chain_not_supported` (destination outside the
networks registry), `destination_pool_not_listed` (no SpokePool of that chain in the
bridge table) and `lookup_failed` (the receipt or the fill could not be read; a
rejected API key still aborts). The destination SpokePools are the hub contract's
`crossChainContracts(chainId)` answers (`0xc186fa914353c44b2e33ebe05f21846f1048beda`);
`tools/across_spokepools.py` re-reads them and prints the difference with the table.
Blockscout's `getLogs` answers "No logs found" when filtered by topic1/topic2 although
matching logs exist (checked on Optimism, 2026-11-13), so against Blockscout the fill
query is sent with topic0 only and the indexed topics are matched locally. The fill is accepted only when its
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
bridges and aggregators are listed but not decoded, except the two canonical
bridges and the named bridges below. Maker-based bridges (Orbiter, Owlto, Rhino.fi)
pay out of EOAs; no maker address of theirs has an official or public-label source
here, so none is listed (Layerswap's maker has a public name tag, see below). The join is by on-chain id; it identifies
where the bridged amount
arrived, not who controls the recipient.

**Arbitrum Inbox and ZKsync Bridgehub** (`tornado_demix/bridges_canonical.py`). A
native transfer to the Arbitrum Inbox (`0x4dbd4fc535ac27206064b68ffcf827b0a60bab3f`)
is read from its receipt: the Inbox's `InboxMessageDelivered(messageNum, data)` and the
Bridge's `MessageDelivered` with the same message number (kind 9 retryable, kind 12
ETH deposit, and the L2 sender). A retryable (`createRetryableTicket` or
`unsafeCreateRetryableTicket`) carries `to` (the L2 recipient), `l2CallValue` (what the
recipient receives), the deposit (`msg.value`) and the refund addresses; the rest of the
deposit pays L2 fees and its unused part goes to `excessFeeRefundAddress`, which is not
followed. An ETH deposit (`depositEth`) carries the credited address and the value; the
Inbox credits the caller itself when an EOA calls it directly and the caller's aliased
address otherwise, and the message holds the result, so the trace takes the address
from the message. Alias rule: L2 address = L1 address +
`0x1111000000000000000000000000000000001111` (mod 2^160). The message is tied to the
traced transfer like an Across deposit: native coin, deposit equal to the transfer,
`l2CallValue` above zero, the traced address is the L1 sender of the message (the
un-aliased sender of the Bridge log; the transaction sender counts only when that log is
missing), and each message is taken by one transfer at most. A retryable with
`l2CallValue` 0 is the fee of a token deposit through the gateway router: its `to` is the
L2 gateway, not where the funds go (the token recipient is inside the gateway calldata),
so it stays "bridge arbitrum (not followed)". The L2 credit is confirmed on Arbitrum One:
among the recipient's normal transactions from the L1 time, the earliest one from the
aliased L1 sender to the recipient with exactly the credited value that no earlier
message has claimed (two identical deposits a few minutes apart take two different
credits, or the second ends as not found). "Earlier" is by message number, whatever
order the trace reaches the messages in: when message 40 is reached only after message 41
took the credit 40 would have taken, 40 takes it back, 41 is looked up again without it
and the replay runs once more (the same for token gateway deposits). The first read
covers 2 hours after the L1
time; only when nothing matches is the rest of the 7-day retryable lifetime read. On the
three fixtures that is the auto-redeem or deposit transaction, 7 to 12 minutes after the
L1 block. The walk then continues on Arbitrum from the L2 recipient with
`l2CallValue` (the fees are not carried over); the edge records the message number, the
L2 recipient and the L2 transaction (state `l2_credited`). The first hop on Arbitrum
is usually marked history-incomplete: the L2 fee refunds arrive as internal
transactions (no transaction index) in the same block as the credit, so the order inside
that block is not known and its bounds are widened by the general rule above. Without such a transaction
(a retryable whose auto-redeem failed and was not redeemed within the window, one whose
retry failed so that `l2CallValue` went to `callValueRefundAddress` instead, which is
not followed, or one the explorer does not return) the branch ends as "bridge arbitrum, l2 credit not found"
(state `l2_credit_not_found`) with the L2 recipient shown; a failed receipt or L2
lookup ends as `lookup_failed`; without a reader for Arbitrum it ends as "bridge
arbitrum (recipient <address>, chain not supported)". Messages not tied to the
transfer keep "bridge arbitrum (not followed)".

*Arbitrum token gateways.* An ERC-20 deposit (`outboundTransfer` or
`outboundTransferCustomRefund` on the L1GatewayRouter
`0x72ce9c846789fdb6fc1f34ac4ad25dd9ef7031ef`) moves the tokens from the caller into
the token's L1 gateway (listed: the standard L1ERC20Gateway
`0xa3a7b6f88361f48403514059f1f16c8e78d60eec`, L1CustomGateway
`0xcee284f754e854890e311e3280b767f80797180d`, L1WethGateway
`0xd92023e9d9911199a6711321d1277285e6d4e2db`, from the Arbitrum docs' contract
address page). The gateway emits `DepositInitiated(l1Token, from, to, sequenceNumber,
amount)` and sends a fee-only retryable through the Inbox (`to` = the L2 gateway,
`l2CallValue` 0) whose L2 calldata is `finalizeInboundTransfer(l1Token, from, to, amount,
data)`; the sequence number is the message number. A deposit is read from the receipt
and counts only when the Inbox message with that number was sent by the contract that
emitted the event (the un-aliased sender of the Bridge log) and its L2 calldata names
the same token, sender, recipient and amount. A token with its own gateway that is not
listed (DAI uses one) is recognised too, but only when the router's `getGateway(l1Token)`
names that contract: any contract can emit the event and send a matching retryable
itself, so without the router's word an unlisted emitter is an ordinary contract (and a
failed router read ends as `lookup_failed`). It is tied to the traced transfer when the traced token went from the
traced address to that gateway, the event's token and sender are the traced ones and
the amount is equal (gateways keep no fee in the token; the L2 fees are paid in ETH),
and each message is taken once per replay. The L2 token is the router's
`calculateL2TokenAddress(l1Token)` (an `eth_call`, cached). Both router reads are made
at the latest block, not at the deposit's block (the explorer's `eth_call` takes no
block number): a token whose gateway or L2 token changed since the deposit is mapped as
it is now, and a gateway that is no longer the token's gateway is not confirmed. The
credit on Arbitrum One
is the earliest transfer of that L2 token to the recipient of exactly the amount, minted
(from the zero address) or paid by the L2 gateway (a WETH-style gateway), at or after the
L1 time, in the same 2-hour then 7-day windows, not claimed by an earlier deposit; the
walk continues from the recipient in the L2 token, and the edge records the L1 and L2
token, the gateway, the message number and the L2 transaction. The credit is matched by
recipient, token and amount, not by the retryable ticket id: when another deposit of the
same token and amount to the same recipient lands in the window, the credit taken may
be the other one's, so the L2 transaction and landing time are then off by that much
(the amount and recipient are the same either way). Edges, end points and the result
keep the token contract next to its symbol (`asset_address`, with `chain_id`), and the
totals are per (chain, token contract), so a token that only shares a symbol with
another is never merged with it. A failed mapping or L2
read ends as `lookup_failed`, no credit as "bridge arbitrum, l2 credit not found". The
fee-only retryable on its own (a native transfer of the fee to the router) stays "bridge
arbitrum (not followed)"; a deposit made by another contract on the traced address's
behalf (an aggregator) is not tied, since the event's sender is that contract.

A native call to the ZKsync L1Bridgehub
(`0x303a465b659cbb0ab36ee643ea362c509eeb5213`) is decoded from the calldata of the
traced address's own transaction: `requestL2TransactionDirect` gives `l2Contract` (the
L2 recipient), `l2Value`, `mintValue` and `refundRecipient`;
`requestL2TransactionTwoBridges` gives the L2 receiver from the second bridge's
calldata (legacy `abi.encode(l1Token, amount, l2Receiver)`, or version byte 0x01 then
`abi.encode(assetId, abi.encode(amount, l2Receiver, token))`; another encoding shows
"L2 recipient not decoded"). `refundRecipient` receives unused fees only and is shown
separately, labelled so (zero means the sender). The call must carry exactly the traced
transfer (`mintValue`, plus `secondBridgeValue` for two bridges). ZKsync chains are not
in the networks registry, so the branch ends as "bridge zksync (recipient <address>,
chain not supported)" with the ZKsync chain id on the edge. An ERC-20 deposit
(`requestL2TransactionTwoBridges` with the L1AssetRouter
`0x8829ad80e425c646dab305381ff105169feece56` as second bridge; the tokens go to the
L1NativeTokenVault `0xbed1eb542f9a5aa6419ff3deb921a372681111f6`; both from the ZKsync
docs' chain address page) is a token transfer, whose calldata the explorer does not
return, so the transaction is read: it must be the traced address's own call through a
listed ZKsync second bridge, and the decoded token and amount must be the traced token
and the transfer; the end then also shows the token. A legacy ZKsync Era request
(`requestL2Transaction(_contractL2, _l2Value, ...)` on the Era DiamondProxy
`0x32400084c286cf3e17e7b677ea9583e60a000324`, from before the Bridgehub) gives the L2
recipient `_contractL2` with `_l2Value`; msg.value also pays the L2 base cost, so it is
tied when `_l2Value` is above zero and at most the traced transfer, and ends the same
way (chain 324). Reports print the L2
recipient in full and link it (Arbitrum through the networks registry, ZKsync Era at
explorer.zksync.io; other ZKsync chains have no link). Selectors and event topics are
re-derived from the ABI signature strings in `tests/test_bridges_canonical.py` and
`tests/test_bridges_canonical_tokens.py`.

**Bridge table.** Every row of `tornado_demix/data/bridges.csv` and every decoder is in the
table below: how the bridge is recognised, whether the trace follows it to the other side
and, when not, the end state (`state` on the edge and the end point; the end reason is
the text in quotation marks). Without destination readers (`destinations` not given to
`trace_funds`) no bridge is followed and each one is named. The named bridges
(`tornado_demix/bridges_named.py`) are the ones a measured sample of Tornado ETH-pool exits
reached besides Across and Arbitrum. Each deposit is tied to the traced transfer like an
Across deposit: the
log is emitted by the listed contract the transfer went to, the traced address sent the
transaction (or is the Relay depositor), the token and the amount are the transfer's
(a native `forwardEth` call or a Relay receiver payment carries no amount in its log:
the payment is the transfer itself), and each log is taken by one transfer per replay.
A deposit that is not tied ends as "bridge <protocol> (not followed)" (`not_followed`); a
receipt that cannot be read as `lookup_failed`. The named bridges are not followed to the
other chain, for the reason in the table: the trace never guesses a payout. Each end keeps
the decoded details on the edge (`bridge`: request id, memo, destination chain, recipient,
token out, vault) and reports print the recipient in full (linked only on a chain of the
networks registry). The sample behind the choice and the share the code follows are in
[Evaluation](EVALUATION.md#bridge-coverage-of-tornado-eth-pool-exits-exploratory).

| Bridge | Recognised how (source of the address) | Followed? | Why not / end state |
|---|---|---|---|
| Across | SpokePools of the registry chains, the SpokePoolPeriphery and listed aggregators (Across hub contract `crossChainContracts`, Etherscan-verified names); a `V3FundsDeposited` / `FundsDeposited` log tied by token, amount and depositor | yes, to the fill on the destination chain (`filled`); the walk goes on from the fill recipient | `fill_not_found`; `deposit_expired_possible_refund`; `destination_chain_not_supported`; `destination_pool_not_listed`; `lookup_failed`; `deposit_not_tied` ("bridge across (deposit not tied to this transfer)"); `deposit_not_decoded` ("bridge across, deposit event not decoded", "(v2 not decoded)" for the v2 pool); a deposit after a swap whose payout row is not listed ("bridge across (swapped before the deposit, fill amount not read)"); a fill to the MulticallHandler without a tied onward payment ("bridge across (multicall handler, onward transfer not tied)") or the part it kept ("bridge across (multicall handler, rest not paid on)") |
| Arbitrum | Inbox `MessageDelivered` kind 9 (retryable) or 12 (`depositEth`), and the token gateways' `DepositInitiated` confirmed by the Inbox message; gateways listed or confirmed by the L1GatewayRouter (Arbitrum docs) | yes: native and ERC-20 deposits, on Arbitrum from the L2 recipient once the credit is found (`l2_credited`) | `l2_credit_not_found` (recipient shown); a fee-only retryable, a deposit not tied to the transfer or an unlisted gateway stays "bridge arbitrum (not followed)"; `lookup_failed` |
| ZKsync | Bridgehub `requestL2TransactionDirect` / `TwoBridges` (native and ERC-20 through the listed L1AssetRouter), legacy Era `requestL2Transaction` on the DiamondProxy (ZKsync docs) | no | ZKsync chains are not in the networks registry: `destination_chain_not_supported`, "bridge zksync (recipient <address>, chain not supported)"; a call that is not a deposit: `not_followed` |
| Circle CCTP | TokenMessenger / TokenMinter of v1 and v2 (Circle docs): `DepositForBurn` tied by depositor, token and exact amount | yes, on an EVM destination of the registry, from the mint (`minted`) | `mint_not_found`; `destination_chain_not_supported` ("..., chain not supported)"); `minted_off_evm` for Solana, Noble and other domains without an EVM chain id (recipient shown); a burn through another contract is `not_followed`; `lookup_failed` |
| CoW Protocol | GPv2Settlement (CoW docs); the owner's `Trade` tied by token and exact sell amount | yes, as a swap from the payout (`settled`) | `trade_not_tied` ("swap cow (trade not tied to this transfer)"); `payout_not_found` ("swap cow (order <uid>, payout not found)") |
| LI.FI, Socket, Rango, Squid | listed aggregator addresses (Etherscan-verified names, `kind` aggregator) | a swap when a material payment comes back (another token, or native coin above the value attached); an Across deposit in the receipt is followed | a native payment back within the value attached, with no tied deposit and no bridge event: `bridge_or_swap_unknown` ("aggregator <protocol> (bridge or swap unknown)"); a deposit of the traced address that is not tied: `deposit_not_tied`; with a bridge contract's event in the receipt the payment back is a refund and the branch ends as that bridge, `not_followed` |
| Hop, Synapse, Stargate, Multichain, Wormhole, Celer, deBridge, Polygon PoS, Optimism, Base, Scroll, Linea, Starknet, Polygon zkEVM, Blast, Mantle, Gnosis Omnibridge, Connext, Hyphen | listed contract addresses with a `source_url` (Etherscan-verified names) | no | no decoder: `not_followed`, "bridge <protocol> (not followed)", the address is named; a call that brings funds in (Polygon `exit`, Wormhole redeem) is an arrival, not an exit |
| Not in the table | an unlisted contract | no | "contract without a payment back" (an Across deposit made through it is still found); Maker-based bridges that pay out of EOAs (Orbiter, Owlto, Rhino.fi) are not listed |
| Relay | `RelayNativeDeposit` / `RelayErc20Deposit` of the RelayDepository, `FundsForwardedWithData` of the RelayReceiver (verified contracts on Etherscan) | no | the request id is all the chain shows; destination and recipient are in the off-chain quote: "bridge relay (request <id>, destination not on chain)", `destination_not_on_chain` |
| Mayan | `ForwardedEth`, `ForwardedERC20`, `SwapAndForwardedEth`, `SwapAndForwardedERC20` of MayanForwarder2 (Mayan docs, same address on every EVM chain); the forwarded call decoded for SwiftSource, FastMCTP, MayanCircle (verified contracts) | no (yes for a same-chain MonoChainSwap payout) | the destination fulfil is keyed by an order hash or a CCTP nonce that is not computed here: "bridge mayan (to <chain>, recipient <address>)", `fulfil_not_followed`; destination from the Wormhole chain id or CCTP domain; a Solana recipient in base58; another forwarded call: "bridge mayan (via <contract>, order not decoded)", `order_not_decoded`. MonoChainSwap pays out on the same chain in the same receipt (`TokenTransferred` / `EthTransferred`): followed from the payee like a swap (`same_chain_payout`); the payout taken is the nearest one logged before the forward and after the forward logged before it in the same receipt (the forwarder logs right after its call: observed on ten real MonoChainSwap receipts after Ethereum block 26,300,000, five token and five ETH payouts, each payout log directly before its forward log; the test receipts are synthetic), so a forward whose own payout is missing takes no other one; each payout pays one forward; none readable: "payout not found", `payout_not_found`. The forwarded call is decoded by selector, not by looking the Mayan contract up: MayanForwarder2 forwards only to protocols it whitelists. Forwarders are listed on the registry chains of the docs page (Ethereum, Arbitrum, Base, Optimism, Polygon, BNB Chain, Avalanche); Unichain, Linea, HyperEVM and Monad are not in the networks registry |
| THORChain, Maya | router `Deposit(to, asset, amount, memo)` (THORChain_Router verified on Etherscan; Maya routers from MAYANode `inbound_addresses`) | no | the outbound is a vault payout not tied on-chain to the memo: "bridge thorchain (to <chain>, recipient <address>)" for a swap memo (`SWAP`, `s`, `=`, limit swaps `=<` and `=>`; THORChain short asset names expanded); a synth (`/`), trade (`~`) or secured (`-`) asset, or a thor1 / maya1 recipient, stays on the protocol's chain: "to THORChain (BTC~BTC, ...)"; an empty destination: "pays the originator"; ADD / WITHDRAW / DONATE name the liquidity pool (short pool names expanded: `wd:e:100` is a WITHDRAW from ETH.ETH; ADD also its paired address) and show no destination or recipient, as nothing is paid out to one; TRADE+ / SECURE+ the account address; any other action shows only the action and the memo (nothing read from its other fields); `vault_payout_not_followed` |
| Layerswap | plain transfer to the maker EOA (Etherscan public name tag "Layerswap 1") | no | matched off chain; call data sent with the transfer shown as the reference: "bridge layerswap (maker <address>, matched off chain)", `off_chain_matching` |
| NEAR Intents | plain transfer to the NEAR Intents wallet (Etherscan public name tag); per-quote deposit addresses are ordinary EOAs that sweep there, so the trace reaches it one hop later | no | matched off chain: "bridge near-intents (deposit to <address>, matched off chain)", `off_chain_matching` |
| Unit | plain transfer to a Unit native-chain treasury (Unit docs, key addresses: Ethereum, Base, Avalanche) | no | the Hyperliquid recipient is assigned off chain: "bridge unit (to Hyperliquid, recipient assigned off chain)", `off_chain_matching` |
| Hyperliquid | native USDC sent to Bridge2 on Arbitrum (Hyperliquid docs, which call it the legacy bridge; CCTP is preferred, see below) | no | credited to the sender on HyperCore, not an EVM chain: "bridge hyperliquid (recipient <sender>, credited on HyperCore)", `credited_off_evm`; under 5 USDC is flagged `below_minimum` (not credited) |

Selectors and topics are re-derived in `tests/test_bridges_named.py`; fixtures are the
first matching transaction at or after a fixed block, picked as stated there.

**Circle CCTP: followed to the mint** (`tornado_demix/bridges_cctp.py`). CCTP burns USDC
on one chain and mints it on another. `depositForBurn` / `depositForBurnWithCaller` (v1)
and `depositForBurn` / `depositForBurnWithHook` (v2) pull the USDC from the caller into
the TokenMinter (the transfer the trace sees) and burn it; the TokenMessenger logs
`DepositForBurn` (v1: nonce, burn token, amount, depositor, mint recipient as bytes32,
destination domain, destination TokenMessenger, destination caller; v2 adds maxFee and
minFinalityThreshold and has no nonce, which the attestation service assigns). The
TokenMessenger, MessageTransmitter and TokenMinter of v1 and v2 are listed for every
registry chain with a CCTP domain (Ethereum, Avalanche, OP Mainnet, Arbitrum, Base,
Polygon PoS), from Circle's EVM contracts pages
(https://developers.circle.com/cctp/evm-smart-contracts and
https://developers.circle.com/cctp/v1/evm-smart-contracts); domains come from the same
table the Mayan decoder uses. A burn is tied when the transfer went to a listed
TokenMinter and the receipt holds a `DepositForBurn` of a listed TokenMessenger of the
same version whose depositor is the traced address, whose burn token is the traced token
and whose amount is exactly the transfer; each burn is taken by one transfer. A burn
made through another contract (an aggregator calling `depositForBurn` in its own name) is
not tied and ends as "bridge cctp (not followed)".

On an EVM destination of the networks registry the mint is searched for: the
recipient's `MintAndWithdraw` logs of the destination TokenMessenger the burn names, from
the burn up to 7 days later. A log counts when the minted amount plus the fee collected
is the burned amount (the fee at most maxFee) and its transaction holds a
`MessageReceived` of a listed MessageTransmitter from the burn's source domain whose
burn message repeats the burn token, recipient, amount and sender (and the nonce for
v1; for v2 the fee executed is the fee collected). Each mint is given to one burn per
trace. The trace goes on from the recipient in the minted token, with the minted amount
(a v2 fast-transfer fee is kept by Circle: on the real fast transfer of the fixtures
`MintAndWithdraw`'s amount is the burn less the fee and its feeCollected is the fee). End
states: `minted` (followed, "mint tx"
linked), "bridge cctp (to <chain>, recipient <address>, mint not found)"
(`mint_not_found`), "..., chain not supported)" for an EVM chain outside the registry,
`lookup_failed`, and "bridge cctp (to <domain>, recipient <address>)" (`minted_off_evm`)
for a domain without an EVM chain id in the domain table (Solana, Noble, Sui, Aptos,
Starknet, Stellar, and the newer Circle domains whose chain id is not taken from a
Circle page: they are named, not followed), the recipient decoded (base58 on Solana,
bytes32 hex otherwise). Selectors and topics are re-derived in `tests/test_bridges_cctp.py`; its
fixtures are the first matching burns at or after Ethereum block 26,300,000, as stated
there.

**CoW Protocol settlements** (`tornado_demix/swaps_cow.py`). GPv2Settlement
(`0x9008d19f58aabd9ed0d60971565aa8510560ab41`, the same address on every registry chain
per https://docs.cow.fi/cow-protocol/reference/contracts/core) settles a batch of signed
orders in one transaction: it pulls each order's sell tokens from the owner into the
settlement, logs `Trade(owner, sellToken, buyToken, sellAmount, buyAmount, feeAmount,
orderUid)` and pays the buy tokens to the order's receiver. A transfer into the settlement
is the traced address's order when a `Trade` of the settlement in the same receipt has
the traced address as owner, the traced token as sell token and exactly the transfer as
sell amount (each Trade taken once). The receiver is in the signed order, not in the
event, so the payout is read from the receipt: a `Transfer` of the buy token from the
settlement of exactly `buyAmount` (each payout taken once), confirmed in the receiver's
history. Payouts to the owner are taken only when there are as many as the owner has
trades of that buy token and amount in the receipt; with none to the owner, the only
such payout is taken only when no other trade of the receipt has the same buy token and
amount (it could be another order's receiver). The trace goes on from
the receiver as a swap into the buy token; a receiver other than the owner is shown as
"paid out by cow to <address>". A native buy token is paid by an internal transaction
that the receipt does not show, so only a payment to the owner sent by the settlement
is read, each such payment paying one trade. A partial fill is a Trade of its own in
each settlement and is tied there by its own amount. Otherwise the
branch ends at the settlement as "swap cow (trade not tied to this transfer)" or "swap
cow (order <uid>, payout not found)" (no payout, or equal payouts to several other
addresses). A transfer into the settlement that no Trade ties (a manual transfer, an
ETH-flow or pre-signed order owned by another contract) ends at the settlement by name:
the general "payment back in the same transaction" swap rule is not applied to it.
Batch caveat: other users' orders and the solver's liquidity share the
transaction, and the buy tokens of an order can come from another user's sell (a
coincidence of wants) rather than from a pool; the payout is the order's output, not a
path through the batch. Native-ETH sell orders go through CoW's ETH-flow contract, which
is the order owner; they are not tied to the user. Fixture: `tests/test_swaps_cow.py`.

**Across edge cases.** *Swap before the deposit.* A periphery, router or aggregator may
swap the traced token and deposit the token it bought (`swapAndBridge`). Such a deposit
of another token than the traced one is tied when it is the only one of its kind in the
transaction not yet taken, it is in the traced address's name (or in the contract's own
name with the stronger tie above), the traced transfer is the only inflow of the
traced token into the contract in that call (the swap can only have spent this
transfer), and that contract paid the deposit: a Transfer of the deposited token from it
to the SpokePool of exactly the input amount in the same call. A native-coin deposit
(no such Transfer) is accepted only when the contract is known to have made it: the
traced address sent the transaction to it, or, inside the traced address's user
operation, the deposit is in the contract's name or the contract paid the SpokePool exactly
the input amount by an internal transfer in that transaction (internal transfers carry no
log index, so this is checked per transaction, not per user operation). 2.23: a smart
account's batch that pays the contract and makes its own native deposit no longer ties the
two; a native swap-and-bridge whose deposit names the account and whose internal transfer
cannot be read ends as not tied (named, not followed). A deposit the
address made itself beside an unrelated payment in one batch transaction is therefore not
taken. Amounts in two tokens cannot be compared without prices, so there is no fee
bound; the whole transfer is taken as the deposit and the trace goes on from the fill
recipient as the amount the fill paid, in the filled token (its decimals read from the
recipient's payout row; the native coin has 18). The edge records `swapped_before`
(sold token, deposited token and amount). When the payout row is not listed the branch
ends as "bridge across (swapped before the deposit, fill amount not read)"; two such
deposits, another inflow of the sold token, a third depositor or a deposit the contract
did not pay end it as "bridge across (deposit not tied to this transfer)". *MulticallHandler fills.* A deposit with a
message is filled to an Across MulticallHandler, which runs the message's calls in the
fill transaction. Two deployments are listed, each address with its own source: the
MulticallHandler of Across's deployed-addresses list
(`0x0f7ae28de1c8532170ad4ee566b5801485c13a0e` on Ethereum, Optimism, BNB Chain,
Polygon, Base and Arbitrum; `0x9610954acdca5ff7905f051a040ce33fe613c60e` on Avalanche),
and `0x924a9f036260ddd5808007e1aa95f08ed08aa569`, a verified "MulticallHandler" on the
Ethereum, Optimism, Polygon, Base and Arbitrum explorers that Across fills also pay. The trace goes on from the handler's payments in that transaction: the
fill token's Transfer logs from the handler after the fill log (a burn to the zero
address, which unwraps, is left out) and, when the fill is in the wrapped native coin,
the handler's native payments (internal transactions of the fill block); each payee
gets its share of the fill. No payment, payments above the fill, or a transaction with
several fills to the handler end the branch at the handler on the destination chain as
"bridge across (multicall handler, onward transfer not tied)"; what the handler did not
pay on in these assets ends there as "bridge across (multicall handler, rest not paid
on)" (it may have been swapped by the message's calls, which are not followed). When
the client cannot list internal transactions, a native fill's handler payments are a
failed lookup (`lookup_failed`, with that reason in `errors`) unless the handler also
paid on in the token. Fixture: a real fill whose handler unwraps and pays 99.75 % back
to the depositor (`tests/test_trace_across_edges.py`). The relayer and the fee
recipient are replaced by synthetic addresses wherever they occur (in the fill
transaction and inside the deposit's message calldata); the transaction hashes and the
fill's message hash are the original ones and no longer match the edited content. *ERC-4337.* A smart account's call arrives through an EntryPoint (v0.6
`0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789`, v0.7
`0x0000000071727de22e5e9d8baf0edac6f37da032`, v0.8
`0x4337084d9e255ff0702461cf8895ce9e3b5ff108`) in a transaction a bundler sent. The
EntryPoint logs `UserOperationEvent` (indexed sender, success flag) after each
operation, so the logs of one operation lie between the previous operation's event and
its own; the smart account counts as the sender of a deposit in that span of a
successful operation (for the "made by" checks and the stronger tie, whose inflow check
is then limited to that span; the traced token transfer must itself be logged in the
same operation as the deposit). A failed operation, or an event of another contract,
does not count. The swap-before-deposit and ERC-4337 tests use synthetic receipts: the
real swap-and-bridge receipts picked by the fixture rule held swap-path contracts or
relayers that appear in the project's private data.

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
`t_lo = max(0, t_lo - o)`, `t_hi = min(t_hi, B - o)`, `B -= o`; a traced inflow of
share `a` adds `a` to both `t_lo` and `t_hi`, so merged branches add up. These are
tight and FIFO always lies inside them (derivation in `tornado_demix/allocation.py`;
an exhaustive test compares them with every feasible allocation of all short
integer histories). The FIFO amount and the bounds of an edge come from the same
pass over the ledger. On display `lower` is rounded down and `upper` up (6
decimals), so rounding never gives `lower > upper`. `lower`
above 0 means "certainly at least that much"; `lower` 0 means the transfer is only
possible. Events are ordered by block, then by transaction index when the explorer
row has one (internal transfers do not: an unindexed inflow counts as first in its
block, an unindexed outflow as last), then by the position inside the transaction
(top-level value, `traceId`, `logIndex`; an unknown position sorts an inflow first and
an outflow last, see "Order inside one transaction"). Native and token assets are separate
histories. An outflow larger than the balance the history explains (an overdraft)
sets `history_incomplete` (internal transfers or pages missing); nothing is
clamped. From that outflow on `lower` is 0, since the outflow and every later one
may consist of unseen other funds, and `upper` is `min(o, t_hi)` with `t_hi` reset
to the whole traced amount that arrived and no longer capped by the balance, since
unseen funds may also have paid the earlier outflows. The flag covers the whole
address: an edge from an outflow before the overdraft rests on the same missing
data, so `trace` and `allocation.bounds` report it with `lower` 0 and `upper` =
min(o, traced amount credited to inflows up to that block) as well. The traced
amount here counts credits that the replay applied late, and the credits passed
downstream from a widened edge are the widened ones: the replay runs again with
the incomplete addresses incomplete from the start until their credits settle. A randomised test with hidden inflows checks that the
bounds from the overdraft on contain every feasible allocation. An
explorer list recorded as cut off (`client.truncated`) or internal transactions
that could not be read make the ledger incomplete from the start. An edge from an
address with an incomplete history is never called certain: it reads "possible
(history incomplete)" in the CLI, the HTML report, the web UI and the JSON
(`status`). Gas fees are not
subtracted, which only widens the interval. Inflows the explorer does not list
(beacon-chain withdrawals, block rewards, self-destruct payouts) understate the
balance and can raise `lower`; `history_incomplete` is set per expanded node, not per
edge, and is not triggered by such unlisted inflows. The traced inflow is the arrival
transaction of the hop (the swap or bridge fill transaction too); for the start
address, whose arrival is not known, the first inflow at or after the start block
that matches the amount if exactly one does (`arrival_matched` = `exact`); with
several equal inflows (`ambiguous`, common with Tornado denominations) or none
(`synthetic`) the amount is placed at the start block, which only widens the bounds.
`--arrival-tx` (web: arrival transaction) names the arrival explicitly; FIFO starts at
that arrival (at the start block when it is not identified). Edges of results saved
by versions before 2.22 can lack bounds (`bounds_available` false, "bounds
unavailable"); new results always carry them.

## 6. Reading the output responsibly

- Treat every candidate as a **lead**, corroborated by other evidence (KYC records,
  off-chain data, subsequent hops, exchange attribution).
- Read the band and its evidence, not the score.
- Prefer exact multi-pool profile matches over single-count candidates.
- Do not present soft overlaps from synchronous-deposit wallets as evidence of a link.

**What every report says about itself (since 2.22).** The texts below come from one
module (`tornado_demix/honest.py`), so the console, the HTML report and the web UI
cannot disagree.

- *How to read this* is generated from `LEAD_SOURCE` and `CONTEXT_SIGNALS`: a signal
  that is context is never described as a lead.
- With no lead signal the summary says "No lead", names the highest-scoring
  chance-level candidates with the number tied at that score, and states the tie
  order (more withdrawals first, then address); it does not name a "top candidate".
- `cross_method` counts only evidence families: context and note signals are not a
  family and do not make an address "selected by two methods".
- The broadcaster status (`self`, `other`, `unverified`) is stored with the verified
  self-relayed list (`self_relayed_leads` in the JSON); the HTML report, the CSV and
  the web UI show that list, not a recomputed unchecked one.
- A shared fee fingerprint is context. It is shown with how common it is among the
  self-relayed withdrawals of the same pools in the window (count of total) when the
  fee settings of all of them were read, and otherwise as "frequency not measured"
  with how many were read; fee settings are read only for the leads and for a bounded
  sample of other recipients' withdrawals, so it is usually not measured.
- The console and every candidate CSV carry the band, the score, the evidence
  families, the signals and the full withdrawal transaction hashes of each candidate.
- Every report ends with the same *Limitations* list and, when `docs/PROTOCOL.lock`
  does not exist, an "exploratory: no PROTOCOL.lock" banner.
- The HTML report has a provenance section: version, git commit, the command line,
  parameters, data sources (host names only), the label files used with their
  SHA-256, the SHA-256 of the JSON export written in the same run, and the UTC time.

## 7. Recipient characterisation

`characterize` describes an exit candidate: activity counts, inflows from the
network's native pools (a received note is at least half the denomination, so a
relayer's collected fees are not counted), and the dominant next transactions. A
next transaction that carried no native value is marked as a contract call (for
example a token transfer or approval); token amounts are not traced. The class
(`high-activity service`, `multi-pool consolidator`, `possible personal exit`, `no pool
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

Before 2.22 the classification was: `exchange` whenever the tag name starts with one of Binance, Coinbase, Bybit, OKX, Kraken, Crypto.com, KuCoin, Gate, HTX/Huobi, Bitfinex, Bitget, MEXC followed by `:` or a space, and names no token, bridge, router or contract (so "Binance Smart Chain: Token" is `other`); a generic "DEPOSIT ADDRESS" tag is `deposit`; everything else is `other`. The current rule follows.

Classification of a tag (since 2.22): `exchange` needs a brand of the built-in list at the start of the tag, a wallet word (`hot`, `cold`, `deposit`, `wallet N`, or a numbered name such as "Coinbase 16") and no veto word (exploit, hack, phish, scam, drainer, coin, token, dex, aggregator, router, bridge, smart wallet, contract, deployer; also wallet products and non-custodial or merchant services: "Web3 / Bitget /
Coinbase / OKX / Binance / Trust / Exodus Wallet", "Commerce", "Staking"). Before 2.22 a brand alone was enough, so "Bybit Exploiter 1" (an exploiter address), "Binance Coin", "Coinbase Smart Wallet", "Bitget Wallet" and "OKX DEX: Aggregator" were `exchange`; they are `other` now, and a cached row written by the older rule is re-checked from its tag when the cache is read. A positive and negative table is in `tests/test_blockscout_veto.py`. A label whose category was filled from Blockscout during the merge keeps `category_source: blockscout`, so the rule below still applies to it. With `--labels-blockscout` and no cached tags for the network, the command says so on stderr; `--cache-dir` sets the cache root on every command that takes the flag.

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
