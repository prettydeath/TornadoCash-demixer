# Security and data handling

## Reporting a vulnerability

Open a [GitHub issue](https://github.com/prettydeath/TornadoCash-demixer/issues) for
anything that does not itself expose data. For something that does — a way to
make the tool leak case material, or a defect that silently corrupts an
investigative result — mark the issue clearly and keep the detail out of the
title, or contact the maintainers privately first.

## What this tool touches

It reads public blockchain data through a block-explorer API. It never signs,
never sends a transaction, and never holds a key of any kind. The only secret it
handles is a read-only explorer API key.

## Where secrets and case data live

| Path | Contents | Git |
|---|---|---|
| `config/api.csv` | your explorer API key | ignored |
| `config/wallets.csv` | the addresses under investigation | ignored |
| `config/networks.csv` | your registry override, if any | ignored |
| `.cache/` | cached forward hops: addresses, tx hashes, timestamps | ignored |
| `calibration/cases.csv` | confirmed depositor→exit pairs | ignored |
| `--out-dir` output | the analysis itself | yours to place |

Everything in that table except the output directory is in `.gitignore`. The
key can also come from `ETHERSCAN_API_KEY` instead of a file.

**The cache is case material.** `.cache/` holds subject addresses, downstream
addresses and transaction hashes in plain JSON, and it persists between runs by
design so an interrupted trace can resume. Treat it like the rest of the case
file: put it inside the case directory (`--cache-dir`), and delete it when the
case closes. It is not encrypted.

**Use one config directory per case.** `TORNADO_DEMIX_CONFIG=/cases/case-42`
*scopes* the search rather than prepending to it, so a missing `wallets.csv`
raises instead of quietly falling back to another case's file. The verified pool
registry is the one exception: it ships inside the package and is always found,
because it is a constant rather than case input.

## The web UI

`webui/app.py` binds to `127.0.0.1` with `debug=False` and is meant for one
local analyst. It has **no authentication and no CSRF protection**, and results
are held in a process-wide store keyed by a session cookie. Do not put it on a
network interface, behind a reverse proxy, or on a shared machine. Anyone who
can reach the port can spend your API quota and read the last run's results.

## Interpreting output safely

The output is a set of **probabilistic leads**. Two properties of it matter for
anyone acting on a name:

- A confidence score orders leads. It is not a probability that an address
  belongs to the subject, and it is not calibrated against known cases.
- An empty result means "nothing was found", which is not the same as "there is
  nothing". The tool tries hard to distinguish the two — a provider that fails
  raises rather than returning nothing, a pool whose window will not resolve is
  reported as unsearched, and a registry row it cannot parse is named on stderr
  — but a chain, a proxy, or a hop it does not cover will still read as silence.

Corroborate any address independently before it appears in a referral, a filing,
or a freeze request.
