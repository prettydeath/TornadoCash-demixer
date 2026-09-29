# TornadoCash Demixer

Probabilistic demixing of Tornado Cash deposits using only public on-chain data.
The toolkit links a depositor wallet to likely withdrawal addresses by amount and
timing correlation, adds independent signals where they exist, and reports each
candidate with an evidence band. It is the practical part of the master's thesis
"Methods, models and software implementation of probabilistic demixing of
crypto-asset mixer transactions".

Output is a set of leads for further investigation, not proof. Tornado Cash breaks
the deposit-withdrawal link cryptographically; the tool looks for behavioural
traces users leave around it.

## What it does

- Finds a wallet's deposits into the 55 registered pools on 8 EVM networks
  (native transfers, internal transfers from contract wallets, ERC-20 transfers,
  direct or through a router).
- Groups deposits into vouchers (one pool, one session, `--gap-hours`, default 24)
  and gives each voucher its own withdrawal search window.
- Reads the pools' `Withdrawal` events in those windows and counts how many
  withdrawals each recipient received. A recipient that received exactly *N*
  withdrawals matches a voucher of *N* notes.
- Scores every recipient: a count match weighted by how much of the recipient
  field it eliminates (`disc`; counted only at `disc >= 0.5` in a field of at
  least 5 recipients), self-relayed withdrawals, reuse of a deposit gas price
  (only in blocks without an EIP-1559 base fee), and direct transactions with the
  depositor (not counted when the counterparty is a contract).
- Reports a band per candidate: `strong` (a linked address plus another evidence
  family), `moderate` (a linked address alone), `weak` (amount+timing and/or gas
  price without a linked address — chance-level on real depositors, see the
  placebo test in [EVALUATION.md](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md)), with the evidence behind it: every family, whether it holds,
  and the numbers (share of the recipient field with the same count, `disc`,
  gas price). Candidates are ordered by band, then by an uncalibrated noisy-OR
  score taken across evidence families (within a family only the strongest
  signal counts). Reports state the analysis parameters and the block ranges read.
- Marks the top candidates that had at most a day of history before their first
  withdrawal (fresh, disposable exits; shown as context, never scored).
- Groups exits withdrawn together in repeated bursts, the payout rhythm of an
  operator that pooled several deposits; a group is listed when a corroborated
  candidate or an exit you already know (`--known-exit`) anchors it.
- Marks a linked exit that withdrew within 72 hours of the deposit (early exit;
  context, never scored).
- Optionally runs a placebo check (`--placebo`, or the web UI option): the same
  analysis on a decoy window that ends before the wallet's first deposit, so the
  report shows how many leads chance alone produces for this wallet.
- Opens the HTML report with a case overview and a flow diagram: depositor, pools,
  candidate exits coloured by band.
- For several wallets: shared candidates, denomination-profile matches,
  cross-wallet consolidators graded against the window-overlap artefact, deposit
  synchronicity, shared private funders, and operator clusters (union-find on
  discriminating edges only).
- Traces split exits one hop forward to a common collection address (`cluster`).
- Follows withdrawn funds forward over several hops with FIFO attribution and
  DEX-swap resolution, up to a labelled service (`trace`).
- Characterises an exit candidate: activity, pool inflows, next hops, a disposable
  flag, optional address labels (`characterize`).

The method, thresholds and the reasoning behind them are in
[docs/METHODOLOGY.md](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/METHODOLOGY.md).

## Components

| Module | Role |
|---|---|
| `tornado_demix/demix.py` | deposit detection, vouchers, search windows, withdrawal counting |
| `tornado_demix/heuristics.py` | signals, `count_discrimination`, noisy-OR score, bands |
| `tornado_demix/multi.py`, `graph.py` | multi-wallet correlation, consolidator grading, clustering |
| `tornado_demix/cluster.py` | one-hop tracing of split exits, on-disk cache |
| `tornado_demix/groups.py` | exit groups from joint withdrawal bursts |
| `tornado_demix/trace.py` | multi-hop FIFO tracing of withdrawn funds with swap resolution |
| `tornado_demix/characterize.py`, `attribution.py` | recipient-side analysis, address labels |
| `tornado_demix/etherscan.py`, `rpc.py`, `events.py` | explorer client, JSON-RPC probes, event decoding |
| `tornado_demix/networks.py`, `pools.py`, `data/networks.csv` | verified pool registry |
| `tornado_demix/report.py` (`report_csv`, `report_html`, `report_json`) | CSV, self-contained HTML and JSON reports |
| `tornado_demix/cli.py`, `webui/app.py` | command line and local Flask UI over the same functions |
| `tools/verify_pools.py` | on-chain verification of registry entries |
| `tools/calibrate.py` | precision/recall against confirmed cases (needs private data) |
| `tools/sensitivity.py` | ranking stability of saved results under perturbed weights |
| `tools/simulate.py` | synthetic benchmark: ablation, negative controls, counter-measures ([results](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md)) |
| `tools/real_cases.py` | the method on two public laundering cases (KuCoin, Harmony) |
| `tools/ens_labels.py`, `tools/evaluate_labels.py` | an ENS-labelled set of depositor/exit pairs and the evaluation on it ([results](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md#a-labelled-set-from-ens)) |
| `tools/wang_baseline.py` | the ENS set scored under the Wang et al. (2023) protocol, with their H2/H3/H5 re-implemented |
| `tools/placebo_eval.py`, `tools/placebo_windows.py` | placebo (target-decoy) test on random real depositors, per evidence family and exit window ([results](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md#placebo-test-on-real-depositors)) |
| `tools/review_sample.py` | a blinded manual-review sheet of leads and hidden controls, and its scoring |

## Requirements

- Python 3.9 or newer. Runtime dependency: `requests`. The web UI adds `flask`.
- An Etherscan V2 API key. One key covers Ethereum, BNB Smart Chain, Polygon,
  Arbitrum, Base and Gnosis. Avalanche (Routescan) and Optimism (Blockscout) need
  no key. On the free Etherscan plan the `getLogs` endpoint was not
  available for BNB Smart Chain, Gnosis and Base at the time of writing.

## Installation

From a checkout of the repository:

```bash
python -m pip install .            # core: the tornado-demix command and the pool registry
python -m pip install ".[web]"     # plus Flask; run the UI from a checkout
python -m pip install -e ".[dev]"  # for development: tests and ruff
```

Dependencies and their version bounds are declared in `pyproject.toml`.

## Configuration

Copy `config/api.csv.example` to `config/api.csv` and put your key in it, or set
`ETHERSCAN_API_KEY`:

```csv
service,api_key
etherscan,YOUR_KEY
```

Config files are looked up in `$TORNADO_DEMIX_CONFIG` (only there, when set),
then `./config/`, then the repository's `config/`. One directory per case keeps
case inputs apart. `config/wallets.csv` (column `address`) can replace addresses
on the command line. `config/networks.csv`, if present, overrides the bundled
registry.

### Address labels (optional)

Labels are not bundled. The tool reads one `<network>.csv` per chain
(`ethereum.csv`, `bsc.csv`, ...) with an `address` column and optionally `entity`,
`label`, `category`, `source`, `confidence`, from the directory given by
`--attribution-dir` or `TORNADO_DEMIX_ATTRIBUTION`.

A ready dataset in this format is
[prettydeath/wallet-attribution](https://github.com/prettydeath/wallet-attribution):
about 115,000 addresses (exchanges, OFAC-sanctioned entities, scams, mixers,
bridges, DeFi) on 47 networks, covering all eight networks of this tool.

```bash
git clone https://github.com/prettydeath/wallet-attribution
python -m tornado_demix characterize <address> --attribution-dir wallet-attribution/data
```

It is aggregated from public sources: the OFAC SDN address list (US government
public record, via 0xB10C/ofac-sanctioned-digital-currency-addresses),
dawsbot/eth-labels, tradezon/cex-list, MyEtherWallet/ethereum-lists and the
proof-of-reserves wallets in DefiLlama-Adapters. The dataset's code is MIT; each
label keeps the license of its upstream source, so check those before
redistributing labels or using them commercially.

Labels are only displayed. They never change a score or a band, and a label is
a claim by its source, not a finding of this tool.

## Command line

```bash
# one wallet: CSV files, an HTML report and the full result as JSON
python -m tornado_demix demix 0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2 \
    --network ethereum --out-dir out/demix --report out/demix.html --json out/demix.json

# narrow the search to 6 hours after each voucher's last deposit
python -m tornado_demix demix <wallet> --exit-window 6

# list the exit group of an exit already known from the investigation
python -m tornado_demix demix <wallet> --known-exit <exit address>

# also run the same analysis on a decoy window before the first deposit
python -m tornado_demix demix <wallet> --placebo

# a 7-day exit window, as seen in public laundering cases
python -m tornado_demix demix <wallet> --rapid

# several wallets
python -m tornado_demix multi <wallet1> <wallet2> --report out/multi.html
python -m tornado_demix multi --wallets-csv config/wallets.csv

# split exits, one hop forward (resumable cache in --cache-dir)
python -m tornado_demix cluster <wallet> --cache-dir .cache/case-42

# describe an exit candidate
python -m tornado_demix characterize <address> --attribution-dir path/to/labels

# follow 10 ETH withdrawn to <address> in block 12000000 over up to 4 hops
python -m tornado_demix trace <address> --amount 10 --start-block 12000000 --report out/trace.html
```

`python -m tornado_demix <command> --help` lists every option. A configuration or
provider error exits with a non-zero status and a one-line message. Progress goes
to stderr; the summary to stdout.

## Web UI

```bash
python webui/app.py      # http://127.0.0.1:5000
```

The form runs the same five analyses. For a demix run it shows the strongest
band, the ranked candidates with their band and score, and for each candidate an
**Evidence** panel listing every evidence family that was checked, whether it
holds, and the numbers behind it. **Analysis parameters** lists the assumptions
(voucher gap, window, thresholds) and the block ranges read. The CSV table, the
HTML report and the full JSON result can be downloaded.

For `trace`, enter one exit address, the amount to follow and optionally the
block the funds arrived in, an ERC-20 token and the number of hops (up to 8). The
page lists where the traced funds stop and why (labelled address, hop limit, a
contract that paid nothing back, not moved on) and every edge with its attributed
amount, swaps included; the HTML report, CSV and JSON can be downloaded.

![demix in the web UI with the evidence panel open](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/demix-evidence.png)
*A demix run on Avalanche: two vouchers, a `strong` candidate (count match plus a direct transaction with the depositor) and the evidence panel listing every family that was checked.*

The UI binds to localhost and has no authentication (forms carry a CSRF token); see
[SECURITY.md](https://github.com/prettydeath/TornadoCash-demixer/blob/main/SECURITY.md).

## Documented cases

Five public cases with an external source of truth. Each can be re-run from the
addresses shown (an Etherscan API key is enough; the Ronin labels need an
attribution set). The screenshots are from the web UI; the CLI prints the same
results. A new run can show slightly different counts as the chain grows and open
search windows are clamped to the current block.

| # | Case | Analysis | What it shows | Source of truth |
|---|---|---|---|---|
| 1 | Ronin Bridge 2022 | `characterize` | OFAC-labelled exploiter, bridge and USDC calls, 12,595 ETH to a second sanctioned address | [OFAC, 14 Apr 2022](https://ofac.treasury.gov/recent-actions/20220414) |
| 2 | Wintermute 2022 | `characterize` | one 9.9435 ETH inflow from the 10 ETH pool; a disposable address | [Merkle Science](https://www.merklescience.com/blog/hack-track-analysis-of-wintermute-attack) |
| 3 | Beanstalk 2022 | `demix --exit-window 24` | 271 deposits in three hours; no candidate above `weak` | [Merkle Science](https://www.merklescience.com/blog/hack-track-analysis-of-beanstalk-flash-loan-attack) |
| 4 | KuCoin 2020 | `demix` | 6 of 7 exits found through the linked withdrawal sender | [tayvano/lazarus-bluenoroff-research](https://github.com/tayvano/lazarus-bluenoroff-research) |
| 5 | Harmony 2022 | `demix --known-exit`, `multi`, `trace` | an exit group from one known exit; 14 depositors in 5 funder clusters; a three-hop trace | [tayvano/lazarus-bluenoroff-research](https://github.com/tayvano/lazarus-bluenoroff-research) |

Attributing the Ronin, KuCoin and Harmony thefts to Lazarus is the conclusion of
government and industry investigators, not of on-chain analysis; the identity of
the Wintermute and Beanstalk attackers is not established.

### 1. Ronin Bridge exploiter

```bash
python -m tornado_demix characterize 0x098B716B8Aaf21512996dC57EB0615e2383E2f96
```

The exploiter received nothing from a Tornado pool; `characterize` shows where
its funds went next. With the attribution set loaded, the address itself and the
12,595.3 ETH destination are labelled OFAC-sanctioned, and the contract calls go
to Circle: USDC and the Ronin Bridge. Without labels the same hops are shown
unlabelled.

![Ronin exploiter in characterize](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-ronin.png)
*`characterize` on the Ronin exploiter: attribution labels, no pool inflows, and the dominant next hops with the 12,595.3 ETH transfer to a second sanctioned address.*

### 2. Wintermute attacker

```bash
python -m tornado_demix characterize 0xe74b28c2eAe8679e3cCc3a94d5d0dE83CCB84705
```

One inflow of 9.9435 ETH from the 10 ETH pool on the day of the attack
(2022-09-20), the first activity of the address: it is flagged as disposable, the
pattern of 98.6 % of laundering exits in the MixLaunder cases.

![Wintermute attacker in characterize](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-wintermute.png)
*`characterize` on the Wintermute attacker: one pool inflow, classified as a possible personal exit and flagged disposable.*

### 3. Beanstalk attacker

```bash
python -m tornado_demix demix 0x1c5dCdd006EA78a7E4783f9e6021C32935a10fb4 --exit-window 24
```

271 deposits (247 x 100, 14 x 10, 9 x 1 and 1 x 0.1 ETH) within about three
hours. With a 24-hour exit window the count match leaves one `weak` candidate and
nothing corroborated: a careful operator leaves no lead, and the tool says so
rather than naming an unrelated address.

![Beanstalk attacker in demix](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-beanstalk.png)
*`demix` on the Beanstalk attacker: four vouchers, 271 notes, and a single weak count-match candidate.*

### 4. KuCoin hack (2020)

```bash
python -m tornado_demix demix 0x820a7a97dd146fd97f79881afdf4767624973368
```

The attacker called `withdraw()` itself from an address investigators attribute
to it: 128 withdrawals to 7 exits. No exit received a voucher-sized count (each
got 11-29 notes against vouchers of 24 and 30 notes), so the count match finds nothing. The
depositor had transacted with that caller, so every withdrawal it sent marks its
recipient (`linked_sender`): 6 candidates, all 6 true exits; the seventh
received its withdrawals months later, outside the window. The report opens with
a flow diagram from the depositor through the pool to the candidates.

![KuCoin depositor in demix](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-kucoin.png)
*`demix` on a KuCoin depositor: six moderate candidates, each an exit that received withdrawals sent by the depositor's counterparty; the evidence panel shows which families hold.*

![KuCoin case overview in the HTML report](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-kucoin-flow.png)
*The case overview of the HTML report: depositor, pool and the six candidates, coloured by band.*

With `--placebo` the same analysis also runs on a decoy window that ends a day
before the first deposit, where no withdrawal can spend this depositor's notes:

![Placebo check on the KuCoin depositor](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-kucoin-placebo.png)
*The placebo check: six `moderate` leads in the real window, none in the decoy window; the one decoy candidate is `weak`.*

### 5. Harmony Bridge hack (2022)

Investigators listed 14 depositors and 55 withdrawal addresses. The attacker
pooled 857 notes of the 100 ETH pool and paid them out mostly six at a time, so
the count match finds no exit and no false one. Three other analyses do.

**Exit groups.** Starting from one exit known from the investigation, `demix`
lists the recipients paid out in the same bursts:

```bash
python -m tornado_demix demix 0xe71d5fa89d1086d5c3b0ab03eeee2483d2d5ca97 \
    --known-exit 0x0562ddf7ea5ab56728852eea2eacab61c4b78a1a
```

![Harmony exit group from one known exit](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-harmony-groups.png)
*An exit group anchored by one known exit: 22 addresses paid out in joint bursts, 20 of them on the investigators' list. Across all depositors, one known exit gives 23 addresses on average, 73 % of them on the list.*

**Shared funders.** Correlating the 14 depositors links all of them through five
immediate funders, each of which was paid directly by the bridge exploiter
`0x0d04...ded00`; a sixth, busy funder is left out:

```bash
python -m tornado_demix multi --wallets-csv docs/cases/harmony_depositors.csv
```

![Harmony depositors linked by shared funders](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-harmony-funders.png)
*`multi` on the 14 Harmony depositors: operator clusters linked by a shared funder, and the funders themselves.*

**Trace.** From an exit, `trace` follows the 100 ETH it received forward:

```bash
python -m tornado_demix trace 0x04bca8fa79f36749fa605597e9c9f6788c126944 --amount 100 --max-hops 3
```

![Three-hop trace from a Harmony exit](https://raw.githubusercontent.com/prettydeath/TornadoCash-demixer/main/docs/img/case-harmony-trace.png)
*`trace` from a Harmony exit: the 100 ETH moves through two intermediate addresses; the trace stops at the hop limit.*

The method on both public cases, with the numbers behind each claim, is in
[docs/EVALUATION.md](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md); `python tools/real_cases.py` repeats it.

## Limitations

- Leads, not proof. The weights and `MIN_COUNT_DISCRIMINATION = 0.5` are expert
  judgements; the score has not been calibrated on cases with known outcomes.
  The band does not depend on the weights at all; they only order candidates
  within a band (check a saved result with `tools/sensitivity.py`). How each
  component behaves on generated data with a known answer is in
  [docs/EVALUATION.md](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md).
- On real depositors the count match, self-relay and gas-price reuse do not beat
  chance: a placebo test on 152 random Ethereum depositors found as many such
  leads in decoy windows before the first deposit as in the real windows, at every
  window from 6 hours to 30 days. Only a linked address (a direct counterparty, or
  a withdrawal sent by the depositor's side) stood above chance (30 against 7 over
  30 days, 19 against 1 within 72 hours), so only it makes a `moderate` or `strong`
  band ([details](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md#placebo-test-on-real-depositors)).
- On 31 depositor/exit pairs labelled through ENS (2019-2026), demix found 16 of
  the 21 pairs inside its window, all through a direct transaction between the
  two addresses, which the label sees as well; without that signal it found one.
  Most labelled depositors made single-note deposits, which the count match
  cannot narrow ([details](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md#a-labelled-set-from-ens)).
- Single-note vouchers cannot be narrowed by count: every recipient in the
  window has count 1.
- Busy pools and long windows produce many equal counts. `--exit-window` trades
  recall for discrimination.
- A careful user defeats the method: relayers, long delays, split withdrawals
  to fresh addresses. The result is then "no lead", or weak leads on unrelated
  addresses that happen to share the voucher count, never a `moderate` or `strong` one.
  On the public KuCoin and Harmony laundering cases the count match found no exit
  (every exit collected notes of several deposits); in KuCoin the linked withdrawal
  sender found 6 of 7 exits for one depositor ([docs/EVALUATION.md](https://github.com/prettydeath/TornadoCash-demixer/blob/main/docs/EVALUATION.md)).
- Only Tornado Cash pools in the registry are covered. Bridges, other mixers and
  cross-chain hops are not followed; `cluster` follows one hop only, and `trace`
  follows funds by FIFO attribution, a convention that does not hold when an
  address mixes the traced funds with others.
- Native chains other than Ethereum, Polygon and Avalanche have no verified
  router in the registry, so routed deposits there are not detected.
- `characterize` counts inflows from native pools only.
- Results depend on the explorer API. Provider failures raise errors instead of
  returning empty results, and pools whose search window cannot be resolved are
  reported as not searched.

## Development

```bash
python -m pytest -q          # network access is blocked in tests
python -m pytest -m live     # on-chain registry check, needs network
python -m ruff check . && python -m ruff format --check .
```

Weight sensitivity of saved results (no API calls):

```bash
python tools/sensitivity.py out/demix.json:<expected exit> --samples 1000 --spread 0.5
```

Synthetic benchmark (no API calls, about half a minute):

```bash
python tools/simulate.py --experiment all --trials 200 --seed 1
```

Public laundering cases (KuCoin 2020, Harmony 2022; needs an API key):

```bash
python tools/real_cases.py
```

`requirements-lock.txt` pins every dependency with hashes (generated by
`uv pip compile pyproject.toml --extra web --extra dev --python-version 3.9
--generate-hashes --universal`); install it with
`pip install --require-hashes -r requirements-lock.txt` and then
`pip install --no-deps -e .` to reproduce a result with the same libraries.

CI runs the tests on Linux and Windows with Python 3.9 and 3.13 against the lock file, a test under a
legacy Windows codepage, the linter, and a wheel install check.

## Legal and ethical use

For research, compliance and authorised investigations. Every address processed
is public on-chain data, but a label or a band attached to an address is an
allegation about a person. Corroborate any lead independently before it appears
in a report, a referral or a freeze request, and follow the terms of service of
the data providers you use.

## License

MIT, see [LICENSE](https://github.com/prettydeath/TornadoCash-demixer/blob/main/LICENSE).
