#!/usr/bin/env python3
"""Import all Remnawave hosts into hosts.yml.

Reads REMNAWAVE_API_URL and REMNAWAVE_API_KEY from .env,
fetches all hosts from the panel, and writes them to hosts.yml
with their UUIDs and remarks as labels.

Usage (run inside the container):
    docker exec remnawave-cloudflare-nodes python /app/scripts/import_hosts.py
    docker exec remnawave-cloudflare-nodes python /app/scripts/import_hosts.py --output /app/hosts.yml
    docker exec remnawave-cloudflare-nodes python /app/scripts/import_hosts.py --env /app/.env --output /app/hosts.yml
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Support running both inside container (/app) and from repo root
_script_dir = Path(__file__).resolve().parent
_repo_root = _script_dir.parent
sys.path.insert(0, str(_repo_root))

from src.panel.client import RemnawaveClient


def load_env(env_path: str | None = None) -> None:
    if env_path and Path(env_path).exists():
        load_dotenv(env_path)
    else:
        load_dotenv()


async def fetch_hosts(api_url: str, api_key: str) -> list[dict]:
    client = RemnawaveClient(api_url=api_url, api_key=api_key)
    hosts = await client.get_hosts()
    entries = []
    for host in hosts:
        entry: dict = {"uuid": str(host.uuid)}
        if host.remark:
            entry["label"] = host.remark
        entries.append(entry)
    return entries


def write_hosts_yml(entries: list[dict], output_path: str) -> None:
    data = {"hosts": entries}
    with open(output_path, "w") as f:
        yaml.dump(
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import Remnawave hosts into hosts.yml"
    )
    parser.add_argument(
        "--env", default=".env", help="Path to .env file (default: .env)"
    )
    parser.add_argument(
        "--output",
        default="hosts.yml",
        help="Output hosts.yml path (default: hosts.yml)",
    )
    args = parser.parse_args()

    env_file = Path(args.env)
    if env_file.exists():
        load_env(str(env_file))
    else:
        load_env()

    api_url = os.getenv("REMNAWAVE_API_URL", "").strip()
    api_key = os.getenv("REMNAWAVE_API_KEY", "").strip()

    if not api_url:
        print("✗  REMNAWAVE_API_URL is not set in .env")
        return 1
    if not api_key:
        print("✗  REMNAWAVE_API_KEY is not set in .env")
        return 1

    print(f"→  Fetching hosts from {api_url} ...")
    try:
        entries = asyncio.run(fetch_hosts(api_url, api_key))
    except Exception as e:
        print(f"✗  Failed to fetch hosts: {e}")
        return 1

    if not entries:
        print("⚠  No hosts found in Remnawave panel")
        return 0

    write_hosts_yml(entries, args.output)
    print(f"✓  Wrote {len(entries)} host(s) to {args.output}")
    for entry in entries:
        label = entry.get("label", "")
        label_str = f"  ({label})" if label else ""
        print(f"   {entry['uuid']}{label_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
