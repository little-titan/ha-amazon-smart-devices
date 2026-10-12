#!/usr/bin/env python3
"""Milestone 1 discovery: which Alexa devices exist, and what do they expose?

Answers two questions against your real Amazon account and writes the raw
payloads to disk:

  1. Does ``aioamazondevices`` (the library behind the core "Alexa Devices"
     integration) return the thermostat or smart plugs from its own device
     discovery?
  2. What do the supported devices (Amazon Smart Thermostat, Amazon Smart
     Plug) look like in the Alexa Smart Home GraphQL API (categories,
     features, properties, live values), and in the legacy ``/api/phoenix``
     tree (capability configuration)?

Usage (any machine with Python >= 3.12, not necessarily the Pi):

    python3 -m venv .venv && . .venv/bin/activate
    pip install "aioamazondevices>=16,<17"
    python scripts/discover.py --email you@example.com

You are prompted for your password; once Amazon accepts it you are asked for
a one-time code from your authenticator app or SMS (Amazon 2-Step
Verification must be enabled). Login tokens
are cached in ``out/login-data.json`` (mode 600) so re-runs skip the login.
That file is a credential: never share it.

Everything else under ``out/<timestamp>/`` is redacted by default (serial
numbers, customer/appliance/endpoint ids are replaced with stable hashes, so
cross-references between files still line up) and is safe to share.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import getpass
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

try:
    from aioamazondevices.api import AmazonEchoApi
    from aioamazondevices.exceptions import AmazonError
    from aioamazondevices.structures import AmazonSaveDataConfig
    from aiohttp import ClientSession
except ImportError:
    sys.exit('Missing dependency. Run: pip install "aioamazondevices>=16,<17"')

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "amazon_smart_devices"
API_PATH = PKG_DIR / "api.py"
LOGIN_PATH = PKG_DIR / "login.py"

# Keys whose values identify the account or the hardware. Replaced by a
# stable hash so you can still see that two ids are the same id.
REDACT_KEYS = {
    "serialNumber",
    "deviceSerialNumber",
    "serial_number",
    "dsn",
    "customerId",
    "deviceOwnerCustomerId",
    "account_customer_id",
    "endpointId",
    "entityId",
    "applianceId",
    "id",
    "macAddress",
    "ssid",
    "email",
    "user_id",
}


def _load_module(name: str, path: Path) -> Any:
    """Import a module file without importing the HA package __init__."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        sys.exit(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_client_module() -> Any:
    """Load api.py (Alexa Smart Home client)."""
    return _load_module("ast_api", API_PATH)


def load_login_module() -> Any:
    """Load login.py (two-step login)."""
    return _load_module("ast_login", LOGIN_PATH)


def _hash(value: Any) -> str:
    return "REDACTED-" + hashlib.sha256(str(value).encode()).hexdigest()[:10]


def redact(obj: Any, *, enabled: bool) -> Any:
    """Recursively replace identifying values with stable hashes."""
    if not enabled:
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            # Results keyed by endpoint id must not leak it through the key.
            if isinstance(key, str) and key.startswith("amzn1."):
                key = _hash(key)  # noqa: PLW2901
            if key in REDACT_KEYS and isinstance(value, (str, int)):
                out[key] = _hash(value)
            elif key in REDACT_KEYS and isinstance(value, dict):
                # GraphQL wraps text as {"value": {"text": "..."}}
                out[key] = _redact_wrapped(value)
            else:
                out[key] = redact(value, enabled=enabled)
        return out
    if isinstance(obj, list):
        return [redact(item, enabled=enabled) for item in obj]
    return obj


def _redact_wrapped(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_wrapped(v) for k, v in value.items()}
    if isinstance(value, (str, int)):
        return _hash(value)
    return value


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False, default=str))
    print(
        f"  wrote {path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}"
    )


async def login(api: AmazonEchoApi, args: argparse.Namespace, data_file: Path) -> None:
    if data_file.exists():
        print(f"Using cached login data from {data_file}")
        await api.login.login_mode_stored_data()
        return
    login_mod = load_login_module()
    two_step = login_mod.create_login(api)
    # Same order as Amazon's sign-in: password first, code only once Amazon
    # asks for it. (In fallback mode the password is checked with the code.)
    await two_step.async_submit_credentials()
    if two_step.supported:
        print("Password accepted. Amazon is asking for a one-time code.")
    while True:
        otp = args.otp or input("One-time code (authenticator app or SMS): ").strip()
        args.otp = None  # a pre-supplied code is only tried once
        try:
            login_data = await two_step.async_submit_otp(otp)
            break
        except login_mod.InvalidOtp:
            print("Amazon rejected that code; enter the next one.")
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(json.dumps(login_data, default=str))
    os.chmod(data_file, 0o600)
    print(f"Login OK, tokens cached in {data_file} (do NOT share this file)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", "-e", required=True)
    parser.add_argument("--password", "-p", help="prompted if omitted")
    parser.add_argument("--otp", "-o", help="prompted if omitted")
    parser.add_argument("--out", default=str(REPO_ROOT / "out"))
    parser.add_argument(
        "--no-redact", action="store_true", help="keep raw ids (local use only)"
    )
    parser.add_argument("--debug", action="store_true", help="library debug logs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    client_mod = load_client_module()

    out_root = Path(args.out)
    data_file = out_root / "login-data.json"
    run_dir = out_root / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    do_redact = not args.no_redact

    login_data = json.loads(data_file.read_text()) if data_file.exists() else None
    password = args.password or (
        "" if login_data else getpass.getpass("Amazon password: ")
    )

    summary: list[str] = []

    async with ClientSession() as session:
        api = AmazonEchoApi(
            session,
            args.email,
            password,
            login_data=login_data,
            save_data=AmazonSaveDataConfig(path=out_root),
        )
        try:
            await login(api, args, data_file)
        except AmazonError as err:
            print(f"Login failed: {err!r}")
            if data_file.exists():
                print(f"Delete {data_file} and retry for a fresh login.")
            return 1

        client = client_mod.AlexaSmartHomeClient(api)

        # -- Q1: aioamazondevices' own discovery --------------------------
        print(
            "\n[1/4] aioamazondevices device discovery (what core Alexa Devices sees)"
        )
        lib_devices: list[dict[str, Any]] = []
        try:
            devices = await api.get_devices_data()
            for dev in devices.values():
                lib_devices.append(
                    {
                        "account_name": dev.account_name,
                        "device_family": dev.device_family,
                        "device_type": dev.device_type,
                        "model": dev.model,
                        "manufacturer": dev.manufacturer,
                        "endpoint_id": dev.endpoint_id,
                        "serial_number": dev.serial_number,
                    }
                )
        except AmazonError as err:
            summary.append(f"aioamazondevices discovery raised {err!r}")
        lib_supported = [
            d
            for d in lib_devices
            if any(
                word in f"{d['account_name']} {d['model']} {d['device_family']}".lower()
                for word in ("thermostat", "plug")
            )
        ]
        write_json(
            run_dir / "01_aioamazondevices_devices.json",
            redact(lib_devices, enabled=do_redact),
        )
        summary.append(
            f"aioamazondevices returned {len(lib_devices)} devices; "
            f"thermostat- or plug-like among them: {len(lib_supported)}"
        )

        # -- Q2: raw Smart Home endpoints ---------------------------------
        print("[2/4] Alexa Smart Home GraphQL listEndpoints")
        endpoints = await client.async_list_endpoints()
        supported = [e for e in endpoints if client_mod.device_kind(e)]
        write_json(
            run_dir / "02_endpoints_summary.json",
            client_mod.summarize_endpoints(endpoints),
        )
        write_json(
            run_dir / "02_endpoints_supported.json",
            redact(supported, enabled=do_redact),
        )
        # Every endpoint (ids redacted): shows which other Alexa-only devices
        # are on the account and what features they expose.
        write_json(
            run_dir / "02_endpoints_all.json", redact(endpoints, enabled=do_redact)
        )
        summary.append(
            f"GraphQL listEndpoints returned {len(endpoints)} endpoints; "
            f"supported devices: {len(supported)}"
        )
        for t in supported:
            primary, _ = client_mod.endpoint_categories(t)
            features = sorted(
                {
                    f"{f.get('name')}"
                    + (f"[{f['instance']}]" if f.get("instance") else "")
                    for f in t.get("features") or []
                }
            )
            summary.append(
                f"  - [{client_mod.device_kind(t)}] "
                f"{client_mod.endpoint_text(t, 'friendlyNameObject')!r} "
                f"model={client_mod.endpoint_text(t, 'model')!r} "
                f"mfr={client_mod.endpoint_text(t, 'manufacturer')!r} "
                f"primary={primary} features={features}"
            )

        # -- Q3: live state -----------------------------------------------
        print("[3/4] Live state of supported devices (rich + minimal queries)")
        ids = [t["endpointId"] for t in supported]
        for label, rich in (("rich", True), ("minimal", False)):
            try:
                states = await client.async_get_states(ids, rich=rich)
                write_json(
                    run_dir / f"03_state_{label}.json",
                    redact(states, enabled=do_redact),
                )
                summary.append(f"state query ({label}): OK, {len(states)} endpoint(s)")
            except (AmazonError, ValueError) as err:
                errors = getattr(err, "errors", None)
                write_json(
                    run_dir / f"03_state_{label}_ERROR.json",
                    {"error": repr(err), "errors": errors},
                )
                summary.append(f"state query ({label}): FAILED {err!r}")

        # -- Q4: legacy phoenix (capability configuration) ----------------
        print("[4/4] Legacy /api/phoenix appliance tree")
        try:
            phoenix = client_mod.parse_phoenix(await client.async_get_phoenix())
            phoenix_supported = [
                a for a in phoenix if client_mod.phoenix_is_supported(a)
            ]
            write_json(
                run_dir / "04_phoenix_supported.json",
                redact(phoenix_supported, enabled=do_redact),
            )
            summary.append(
                f"phoenix: {len(phoenix)} appliances, "
                f"supported: {len(phoenix_supported)}"
            )
        except (AmazonError, ValueError) as err:
            summary.append(f"phoenix: FAILED {err!r}")

    text = "\n".join(summary)
    (run_dir / "SUMMARY.txt").write_text(text + "\n")
    print("\n=== SUMMARY ===\n" + text)
    print(
        f"\nShare the folder {run_dir} (redacted={do_redact}). Never share login-data.json."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
