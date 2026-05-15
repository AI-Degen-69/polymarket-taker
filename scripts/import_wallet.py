"""Import an existing private key (e.g. from MetaMask) into .env.

Usage:
    .venv/bin/python scripts/import_wallet.py

The script will prompt you for the private key (input is hidden — won't
appear on screen or in shell history). Paste the key, press Enter.

What it does:
  1. Validates the private key + derives the EOA address.
  2. Tries to auto-find the Polymarket deposit wallet for this EOA by
     scanning DepositWalletFactory events on-chain. If found, configures
     .env for V2 trading (SIGNATURE_TYPE=3, FUNDER_ADDRESS=<deposit>).
  3. Otherwise configures .env in EOA mode and tells you to register on
     polymarket.com first.

Refuses to overwrite an existing .env so a funded key can't be wiped.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Optional

from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
GITIGNORE_PATH = ROOT / ".gitignore"

DEFAULT_RPC = "https://polygon-bor-rpc.publicnode.com"
FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"


def _find_deposit_wallet(rpc: str, eoa: str) -> Optional[str]:
    """Scan the DepositWalletFactory for an event mentioning this EOA as owner."""
    import requests
    # latest block
    r = requests.post(rpc, json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []
    }, timeout=10).json()
    end = int(r["result"], 16)
    padded = "0x" + eoa.lower().replace("0x", "").rjust(64, "0")

    # Scan windows of 10k blocks going back ~3 months.
    step = 10_000
    for start in range(end, end - 4_000_000, -step):
        a = max(0, start - step + 1)
        b = start
        try:
            r = requests.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(a),
                    "toBlock": hex(b),
                    "address": FACTORY,
                }],
                "timeout": 10,
            }, timeout=15).json()
            logs = r.get("result") or []
        except Exception:
            continue
        for lg in logs:
            topics = lg.get("topics") or []
            # owner appears as one of the indexed topics
            if any(t.lower() == padded for t in topics):
                # the deposit wallet address is in topic[1] of the deployment event
                if len(topics) >= 2:
                    return "0x" + topics[1][-40:]
    return None


def upsert_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    keys = set(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in keys:
            out.append(f"{k}={updates[k]}")
            keys.discard(k)
        else:
            out.append(line)
    for k in keys:
        out.append(f"{k}={updates[k]}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def main() -> int:
    if len(sys.argv) > 1:
        print("This script reads the private key from a secure prompt, not", file=sys.stderr)
        print("from the command line (the CLI would leak it into shell", file=sys.stderr)
        print("history). Run with no arguments:", file=sys.stderr)
        print("    .venv/bin/python scripts/import_wallet.py", file=sys.stderr)
        return 2

    if ENV_PATH.exists():
        print(f"refusing to overwrite existing {ENV_PATH}", file=sys.stderr)
        print("  delete .env first if you really want to re-import.", file=sys.stderr)
        return 1

    print("Paste your MetaMask private key. It WILL NOT be echoed to the screen.")
    print("(In MetaMask: account ⋮ -> Show private key. 64 hex chars, '0x' prefix optional.)")
    pk = getpass.getpass("private key: ").strip()
    if not pk:
        print("no key entered", file=sys.stderr)
        return 1
    if not pk.startswith("0x"):
        pk = "0x" + pk
    if len(pk) != 66:
        print(f"private key must be 64 hex chars (got {len(pk) - 2})", file=sys.stderr)
        return 1

    try:
        acct = Account.from_key(pk)
    except Exception as e:
        print(f"invalid private key: {e}", file=sys.stderr)
        return 1

    eoa = acct.address
    print(f"wallet address: {eoa}")

    # Look for an existing deposit wallet
    print("scanning Polymarket DepositWalletFactory for your deposit wallet...")
    deposit = _find_deposit_wallet(DEFAULT_RPC, eoa)

    # Write base env
    base = {
        "PRIVATE_KEY": pk,
        "WALLET_ADDRESS": eoa,
        "CHAIN_ID": "137",
        "POLYGON_RPC_URL": DEFAULT_RPC,
        "CLOB_HOST": "https://clob.polymarket.com",
        "GAMMA_HOST": "https://gamma-api.polymarket.com",
    }

    header = "# Polymarket trading wallet — KEEP SECRET. Never commit.\n"
    ENV_PATH.write_text(header)
    upsert_env(base)

    if deposit:
        upsert_env({
            "FUNDER_ADDRESS": deposit,
            "DEPOSIT_WALLET_ADDRESS": deposit,
            "SIGNATURE_TYPE": "3",
        })
        ENV_PATH.chmod(0o600)
        print()
        print(f"FOUND deposit wallet: {deposit}")
        print("env configured for V2 trading (SIGNATURE_TYPE=3).")
        print()
        print("NEXT STEP:")
        print("  1. Make sure your deposit wallet is funded on polymarket.com.")
        print("  2. Derive L2 API creds:  .venv/bin/python scripts/derive_api_creds.py")
        print("  3. Verify everything:    .venv/bin/python scripts/verify_setup.py")
    else:
        upsert_env({
            "FUNDER_ADDRESS": eoa,
            "SIGNATURE_TYPE": "0",
        })
        ENV_PATH.chmod(0o600)
        print()
        print("No deposit wallet found for this EOA.")
        print("That means you haven't registered this wallet on polymarket.com yet.")
        print()
        print("NEXT STEP:")
        print("  1. Open https://polymarket.com (referral: https://polymarket.com/?r=allaboutai)")
        print(f"  2. Sign in with MetaMask using {eoa}")
        print("  3. Deposit funds via the Polymarket UI (avatar -> Deposit)")
        print("  4. Re-run this script (after `rm .env`) to auto-configure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
