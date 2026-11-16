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
local analyst. It has **no authentication**; the form carries a per-session CSRF
token and the session cookie is `SameSite=Strict`. Results are held in a
process-wide store keyed by the session cookie. Do not put it on a
network interface, behind a reverse proxy, or on a shared machine. Results are
kept per session: a run is read back only with the session cookie that started it, so
another client cannot open it by guessing a link (the result token is random). The real
risk of an exposed port is different: anyone who can reach it can start runs, spend your
API quota, fill the process's memory and keep it busy, which is a denial of service, and the
*Provider settings* take local paths (see below).

**The HTTP cache directory field is local-only.** The form's *HTTP cache directory*
(`http_cache`, the `--http-cache` flag) makes the process create and write cache files in
any directory the account running it can write to. It is checked for length and for
a null byte only, not confined to a folder. That is acceptable for one analyst on
`127.0.0.1` and is another reason not to expose the UI.

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

The web UI keeps the last runs' reports in the memory of its process. Run it as
one process (`python webui/app.py`); under a multi-worker server a download may
land on a worker that never saw the run and answer 404.
