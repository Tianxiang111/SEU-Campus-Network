#!/usr/bin/python3
"""One-shot SEU Portal login using the same helpers as seu-balancer.

This is a diagnostic entry point, not a second authentication daemon. By
default it only reads the effective route; --prepare-routes is an explicit
request to create the balancer's source-address policies.
"""
import argparse
import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path


def load_balancer():
    directory = Path(__file__).resolve().parent
    source = directory / "seu-balancer.py"
    if not source.is_file():
        source = directory / "seu-balancer"
    loader = SourceFileLoader("seu_balancer", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def verify_route(balancer, line):
    result = balancer.call(
        ["ip", "-j", "-4", "route", "get", "10.80.128.2", "from", line["ip"]]
    )
    if result.returncode:
        raise RuntimeError("source route lookup failed")
    routes = json.loads(result.stdout)
    if not routes or routes[0].get("dev") != line["device"]:
        raise RuntimeError("source route uses another uplink")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--line", choices=("wan1", "wan2"), required=True)
    parser.add_argument(
        "--prepare-routes",
        action="store_true",
        help="install reserved routing policies first; read docs/scripts.md",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run on the router as root")
    try:
        balancer = load_balancer()
        accounts = balancer.load_accounts()
        lines = balancer.interfaces()
        if args.line not in accounts or args.line not in lines:
            raise ValueError("configured account and active uplink required")
        if args.prepare_routes:
            balancer.routes(lines)
        line = lines[args.line]
        verify_route(balancer, line)
        result = balancer.authenticate(args.line, line, accounts[args.line])
        reachable = balancer.health(line)
        print(f"{args.line}: login={result}; internet_reachable={reachable}")
        return 0 if reachable else 1
    except Exception as error:
        # Do not print exception values, raw Portal replies, URLs, or credentials.
        print(f"Login check failed ({type(error).__name__}); see docs/scripts.md")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
