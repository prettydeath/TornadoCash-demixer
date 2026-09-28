#!/usr/bin/env python3
"""Check the demix method against two public laundering cases (needs an API key).

KuCoin (2020): investigators attribute to the attacker the address that called
``withdraw()`` 128 times (``0x82e6...``). Every withdrawal it sent is a true exit;
the ground truth comes from the transaction sender, which no demix signal reads,
except linked_sender when that sender is also a counterparty of the depositor.

Harmony (2022): investigators list the addresses that received the attacker's
withdrawals. They selected them partly by withdrawal count and batching, so an
agreement with this list is consistency with their reading, not an independent
check.

Depositors are the case addresses that made Tornado Cash deposits. For each
depositor the tool reports how many true exits fell inside the searched window,
how many were listed as candidates, and how many candidates are not exits.

Usage
-----
    python tools/real_cases.py [--case kucoin|harmony|all]
"""

import argparse
import contextlib
import io
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix.config import load_api_key  # noqa: E402
from tornado_demix.constants import TOPIC_WITHDRAWAL  # noqa: E402
from tornado_demix.demix import detect_deposits, run_demix  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.events import decode_withdrawal  # noqa: E402
from tornado_demix.groups import co_withdrawal_groups  # noqa: E402
from tornado_demix.heuristics import ranked_candidates  # noqa: E402
from tornado_demix.multi import shared_funders  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402

# Source: github.com/tayvano/lazarus-bluenoroff-research, hacks-and-thefts/.
KUCOIN = {
    "addresses": [
        "0x00600423c03ec4b46f9b8a28c66d42bdd1b19c36",
        "0x1a98fcebebfea4ffbded5bf5e4650d71344f52ab",
        "0x1c3f856719a91735ccb78506bd504b17907ac814",
        "0x261985b27a12272ad96b21885fe89a1538cbe91c",
        "0x34a17418cec67b82de8cf77a987941f99dc87c6b",
        "0x3781e57863f00a2ebf77a8a7e47987c46474c9c9",
        "0x7374b5d31beda7acedf5dd379ed864b0d63afe1b",
        "0x820a7a97dd146fd97f79881afdf4767624973368",
        "0x8a6fe380ee2b274b0e01c5c89e9861cebe040745",
        "0xba3271bf528ca6d63b048ff3388eb6550616ed88",
        "0xc48da0b07004c361081eeea3903d049271c8c81a",
        "0xc787eeba9f55933ffb4c37a0284029c24444bbd4",
        "0xeb31973e0febf3e3d7058234a5ebbae1ab4b8c23",
    ],
    "callers": ["0x82e6b31b0fe94925b9cd1473d05894c86f277398"],
}
HARMONY = {
    # the case's exploiter addresses that deposited into Tornado Cash
    "depositors": [
        "0x20dbccd46eef96a1b78383cf0d26bb575ec00201",
        "0x40efc580e5cb5701797a762990d9e690108dadfd",
        "0x432a9cb4353bed67ec5351734d4a44c0826847ae",
        "0x4507ac1bdf4ae5e61ffcec3a9aeda312e2505970",
        "0x482f32c3e1a851a1dc08931e3087ac5a209f3342",
        "0x4fd7d95f721dac5b01daf306fa686b1f8c4dd66a",
        "0x569c6530a8fdbcf8a9967795bed367f785e32d07",
        "0x868a66bb9f016ab88f4a86d2eb9da299c82d3bff",
        "0x89f89d61644c6e606efb25a01210159f102fbd8b",
        "0x8a0858888beeb5d1435ecd3657831699f169c3f4",
        "0x8bab0b7962fb7537e7766175f211d8e4f7afa834",
        "0xb231759890157ca3bd7d664b9795796660374752",
        "0xe71d5fa89d1086d5c3b0ab03eeee2483d2d5ca97",
        "0xec3e23e7a7782b1b2d77901c478823c701d912ea",
    ],
    # "Harmony Laundry 2 TC Withdrawers"
    "withdrawers": [
        "0x04bca8fa79f36749fa605597e9c9f6788c126944",
        "0x0562ddf7ea5ab56728852eea2eacab61c4b78a1a",
        "0x0f6a306f484d29a20aa82da69680fcde806d7e47",
        "0x1d4e699b9d5dc5b153a8f44dcc163aec4c30807a",
        "0x233c7c0dbf64208c4bd99eedd092ad0294056fbd",
        "0x28262345ea880d7b501aa925b5d3f06383df2df7",
        "0x2e23f54038139e07f8f416c94b6b141745269db1",
        "0x2e92083f84075217a14fea5d3e0a3001c3fcc981",
        "0x332e0ab0daf3d2184f0012a14383ce104fa863c4",
        "0x393b22adc0bb332a698e2fd45c311b8cd83bd901",
        "0x3c0e0509296555e26e4bfcea0294687b8f49cbb2",
        "0x3e275ab86a5f664f2f1963694d872acdc3416a36",
        "0x4250ea45df81fa285d625a77b6e531ba77f6c98b",
        "0x44c691805395ed6a4e748155e3b0b54316cd636d",
        "0x491c01999a933bac7f7ab6347df192c11cd809ab",
        "0x4e36426895e70a26c64f3f311a1ee6d5069e80e2",
        "0x566e18b829468f074d9cfcf6db5817f46efc4536",
        "0x5a739160dae76e7aaa008f31ad36a186760898a4",
        "0x602f46ebe41b99466ab01f9ca274a21e1e99e0ea",
        "0x61a3f3ea99f9d87f2987ed4a29f43e16ce8a91d0",
        "0x677784d496e9fe37e5a5c18bbf93ac8251257521",
        "0x67f56d553d28a1b2a445a26a2c31e0894e438f0f",
        "0x6a15153a5616db7540b211cf84cea2e0c25e72bd",
        "0x6aa59d99e5fcee413dd11c0ef997417101b07b3b",
        "0x74668a6e920887293e8ac955e3dc6f597bee99f8",
        "0x7cacdd5937e0f49e4737cae8f503f9e497e6486d",
        "0x7f9cc09d15067a51bc21ded3ab64358a6fce85c9",
        "0x8010255e49e72b6ca90f8a306eb11421fab3ec92",
        "0x82e233d44ea78a4d1c71e64ff0bf089f937e265f",
        "0x88f8f6fb2fee31d3e667f7bfea591efde24cb117",
        "0x8c01fafee16a54cdb3b57fa97bd41a82e422259d",
        "0x8f2c0b4afeab2faa4a48f367194515b3c0fcbd58",
        "0x95c96e68d5433d5c4c4a7f4acca6e3dcb762d740",
        "0x9d30f66336defe1923480b3fdd75558c9988ee07",
        "0xa41056e0cdfdc7ed3846bdfcb3c0c5bc64b85c2c",
        "0xaf31358bad5411ef47c7720a0f3ea9867aea91d8",
        "0xb0951f6e4f6453f2e7c110ee963589132ffc8195",
        "0xb5b8841bb947a6c16695257559bc6164b613166c",
        "0xb93cec243fabdb95debf6d9399ec739839448fd8",
        "0xbd8093a62318b6236d27051f1e018c9e6ac5813c",
        "0xbeb5e1eb96cd3b463419c6b4a1fc9286f491f299",
        "0xc053e6b07a607e694626e10a389a8099a2785b5f",
        "0xc056dc42c0e8c41ba87db7112a72d1ae2bf7d3db",
        "0xc353f8689170b5a2c5547700dafec11633a1084d",
        "0xc8002e0dfff593c05fe37ef3cdc63132f5c6aa86",
        "0xca8f5e1e4c405d6bbe43dbf06aedede039a318e9",
        "0xd1e805c89bf8e500f655e4cb724c31ef15723121",
        "0xdb21c82672e188b4e9caaf5d017f5ec4bce6fe41",
        "0xdb4a650978b351c5d6c302ff07f641aeaef233be",
        "0xdb8022d52da22bc29861c04e19953964df23ff75",
        "0xded98553c9e7df69d82932a23ece7e2a8d975f55",
        "0xe7ae665649a216e4ded0d9f08e66f3737c26aa13",
        "0xf1a5ab19d862e556c6fc74b4f93008915a7ea8ff",
        "0xf5c9e3f3056f39be0bdc56eb95cb75d1c5fed188",
        "0xfe624b4247d5798e1f3d2d11ea7b059a19652f02",
    ],
}


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def depositors(client, network, addresses):
    return [a for a in addresses if _quiet(detect_deposits, client, a, network=network)]


def evaluate(client, network, wallets, truth):
    total = Counter()
    results, first_in = {}, {}
    for wallet in wallets:
        data = _quiet(run_demix, client, wallet, network=network)
        results[wallet] = data
        for res in data["denoms"].values():
            for addr, recs in res["detail"].items():
                if addr in truth:
                    ts = min(r["ts"] for r in recs)
                    first_in[addr] = min(first_in.get(addr, ts), ts)
        rows = ranked_candidates(data)
        in_window = {a for res in data["denoms"].values() for a in res["counts"] if a in truth}
        hits = [r for r in rows if r["address"] in truth]
        bands = Counter(r["band"] for r in rows if r["address"] not in truth)
        print(
            f"  {wallet}: exits in window {len(in_window)}, candidates {len(rows)}, "
            f"true {len(hits)}, other {sum(bands.values())} {dict(bands)}"
        )
        total.update(in_window=len(in_window), candidates=len(rows), true=len(hits))
        total.update({f"other_{b}": n for b, n in bands.items()})
    print(f"  total: {dict(total)}")
    # Context marks, never scored: fresh exits and funders shared by depositors.
    fresh = 0
    for addr, withdrawn in sorted(first_in.items()):
        first = _quiet(client.first_activity, addr)
        fresh += first is None or withdrawn - min(first, withdrawn) <= 86400
    print(
        f"  exits in a window with at most a day of history before it: {fresh} of {len(first_in)}"
    )
    # Exit groups: from each true exit as the only anchor, how many of the group's
    # members are on the truth list.
    seeds = anchored = found = size = 0
    for data in results.values():
        for res in data["denoms"].values():
            groups = co_withdrawal_groups(res)
            for addr in res["counts"]:
                if addr not in truth:
                    continue
                seeds += 1
                group = next((g for g in groups if addr in g), None)
                if group:
                    anchored += 1
                    size += len(group)
                    found += len(group & truth)
    if anchored:
        print(
            f"  exit groups: {anchored} of {seeds} true exits sit in a group; from one of them "
            f"the group has {size / anchored:.1f} addresses, {found / anchored:.1f} on the "
            f"truth list ({found / size:.0%})"
        )
    else:
        print(f"  exit groups: none of {seeds} true exits sits in a group")
    funders = _quiet(shared_funders, client, results)
    print(f"  funders shared by 2+ depositors (not busy): {len(funders)}")
    for funder, ws in funders.items():
        print(f"    {funder} -> {len(ws)} depositors")


def kucoin(client, network):
    print("KuCoin 2020 - exits are the withdrawals sent by the attacker's caller")
    tornado = {p.address for p in network.pools} | set(network.routers)
    sent = set()
    for caller in KUCOIN["callers"]:
        for tx in _quiet(client.outgoing_txs, caller):
            if (tx.get("from") or "").lower() == caller and (tx.get("to") or "").lower() in tornado:
                sent.add(tx["hash"].lower())
    wallets = depositors(client, network, KUCOIN["addresses"])
    truth = set()
    for key in ("100 ETH", "10 ETH"):
        logs = _quiet(
            client.get_logs, network.by_key[key].address, TOPIC_WITHDRAWAL, 11052431, 12367006
        )
        truth |= {w["to"] for w in map(decode_withdrawal, logs) if w["tx_hash"].lower() in sent}
    print(
        f"  depositors {len(wallets)}, withdrawals sent by the caller {len(sent)}, exits {len(truth)}"
    )
    evaluate(client, network, wallets, truth)


def harmony(client, network):
    print("Harmony 2022 - exits are the investigators' listed withdrawal addresses")
    evaluate(client, network, HARMONY["depositors"], set(HARMONY["withdrawers"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--case", choices=["kucoin", "harmony", "all"], default="all")
    args = parser.parse_args(argv)
    network = get_network("ethereum")
    client = EtherscanClient(load_api_key(), pause=0.25)
    if args.case in ("kucoin", "all"):
        kucoin(client, network)
    if args.case in ("harmony", "all"):
        harmony(client, network)


if __name__ == "__main__":
    main()
