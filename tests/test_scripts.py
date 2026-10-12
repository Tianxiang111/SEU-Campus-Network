"""Offline regression tests. Every network/process call is mocked."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


balancer = load_module("tested_balancer", "seu-balancer.py")
login = load_module("tested_login", "seu-login.py")


def result(code=0, stdout=""):
    return subprocess.CompletedProcess([], code, stdout, "")


class ScriptTests(unittest.TestCase):
    def setUp(self):
        # Safety net: an accidentally unmocked command must never run.
        self.process_guard = patch.object(
            balancer.subprocess,
            "run",
            side_effect=AssertionError("unmocked subprocess call"),
        )
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = self.directory / "accounts.json"
        self.config_patch = patch.object(balancer, "CONFIG", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def write_accounts(self, entries, mode=0o600):
        self.config.write_text(json.dumps(entries))
        self.config.chmod(mode)

    def account(self, line="wan1"):
        return {"line": line, "username": "test-user", "password": "test-pass"}

    def test_single_account_is_supported(self):
        self.write_accounts([self.account()])
        self.assertEqual(list(balancer.load_accounts()), ["wan1"])

    def test_single_account_daemon_iteration_does_not_require_wan2(self):
        class EndIteration(Exception):
            pass

        self.write_accounts([self.account()])
        state = self.directory / "single-account-state.json"
        line = {"ip": "10.0.0.2", "device": "sta0"}
        with patch.object(balancer, "STATE", state), patch.object(
            balancer, "REPAIR_RADIO", "radio0"
        ), patch.object(
            balancer, "interfaces", return_value={"wan1": line}
        ), patch.object(
            balancer, "health", return_value=True
        ), patch.object(
            balancer, "routes"
        ), patch.object(
            balancer, "rules"
        ), patch.object(
            balancer, "router_default"
        ), patch.object(
            balancer.time, "sleep", side_effect=EndIteration
        ), patch.object(
            balancer, "authenticate"
        ) as auth:
            with self.assertRaises(EndIteration):
                balancer.main()
        saved = json.loads(state.read_text())
        self.assertEqual(list(saved["lines"]), ["wan1"])
        self.assertTrue(saved["lines"]["wan1"]["healthy"])
        self.assertNotIn("test-pass", state.read_text())
        self.assertNotIn("test-user", state.read_text())
        auth.assert_not_called()

    def test_world_readable_credentials_rejected(self):
        self.write_accounts([self.account()], 0o644)
        with self.assertRaises(ValueError):
            balancer.load_accounts()

    def test_group_readable_credentials_rejected(self):
        self.write_accounts([self.account()], 0o640)
        with self.assertRaises(ValueError):
            balancer.load_accounts()

    def test_placeholder_and_empty_credentials_rejected(self):
        for key in ("username", "password"):
            for value in ("<FILL_ME>", "", None):
                with self.subTest(key=key, value=value):
                    entry = self.account()
                    entry[key] = value
                    self.write_accounts([entry])
                    with self.assertRaises(ValueError):
                        balancer.load_accounts()

    def test_duplicate_uplink_and_unknown_uplink_rejected(self):
        for entries in ([self.account(), self.account()], [self.account("wan9")]):
            with self.subTest(entries=entries):
                self.write_accounts(entries)
                with self.assertRaises(ValueError):
                    balancer.load_accounts()

    def test_checked_stops_on_failed_command(self):
        with patch.object(balancer, "call", return_value=result(1)) as call:
            with self.assertRaises(RuntimeError):
                balancer.checked(["ip", "route", "add", "default"])
            call.assert_called_once()

    def test_login_wrong_route_stops_before_authentication(self):
        line = {"ip": "10.0.0.2", "device": "expected-sta"}
        accounts = {"wan1": self.account()}
        with patch.object(login, "load_balancer", return_value=balancer), patch.object(
            login.os, "geteuid", return_value=0
        ), patch.object(sys, "argv", ["seu-login", "--line", "wan1"]), patch.object(
            balancer, "load_accounts", return_value=accounts
        ), patch.object(
            balancer, "interfaces", return_value={"wan1": line}
        ), patch.object(
            balancer, "call", return_value=result(stdout='[{"dev":"wrong-sta"}]')
        ), patch.object(
            balancer, "authenticate"
        ) as auth, patch.object(
            balancer, "health"
        ) as health, patch(
            "builtins.print"
        ):
            self.assertEqual(login.main(), 2)
            auth.assert_not_called()
            health.assert_not_called()

    def test_login_route_lookup_failure_and_valid_route(self):
        line = {"ip": "10.0.0.2", "device": "sta0"}
        with patch.object(balancer, "call", return_value=result(1)):
            with self.assertRaises(RuntimeError):
                login.verify_route(balancer, line)
        with patch.object(
            balancer, "call", return_value=result(stdout='[{"dev":"sta0"}]')
        ) as call:
            login.verify_route(balancer, line)
            self.assertEqual(
                call.call_args.args[0],
                ["ip", "-j", "-4", "route", "get", "10.80.128.2", "from", "10.0.0.2"],
            )

    def test_auth_credentials_are_stdin_only_and_tls_validation_preserved(self):
        account = {"username": "offline-user", "password": "secret & + value"}
        responses = [
            result(stdout="Dr.COMWebLoginID_0.htm"),
            result(stdout='dr1000({"result":1});'),
        ]
        with patch.object(balancer.subprocess, "run", side_effect=responses) as run:
            self.assertEqual(
                balancer.authenticate("wan1", {"ip": "10.0.0.2"}, account), "success"
            )
            self.assertEqual(run.call_count, 2)
            for invocation in run.call_args_list:
                argv = invocation.args[0]
                self.assertEqual(argv, ["curl", "--config", "-"])
                self.assertNotIn(account["username"], " ".join(argv))
                self.assertNotIn(account["password"], " ".join(argv))
                cfg = invocation.kwargs["input"]
                self.assertNotIn("insecure", cfg.lower())
                self.assertIn('interface = "10.0.0.2"', cfg)
            cfg = run.call_args.kwargs["input"]
            url = json.loads(cfg.splitlines()[0].split(" = ", 1)[1])
            self.assertEqual(urlsplit(url).scheme, "https")
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["user_password"], [account["password"]])
            self.assertEqual(params["user_account"], [",0," + account["username"]])

    def test_rules_weighted_selection_and_ipv6_skip(self):
        with patch.object(balancer, "call", return_value=result()) as call:
            balancer.rules(["wan1", "wan2"])
            script = call.call_args.args[1]
            self.assertIn("numgen inc mod 3 map { 0 : 257, 1 : 257, 2 : 258 }", script)
            self.assertLess(
                script.index("meta nfproto != ipv4 return"), script.index("jump choose")
            )
            self.assertIn(f'iifname "{balancer.LAN_DEVICE}"', script)
            self.assertEqual(call.call_args.args[0], ["nft", "-f", "-"])

    def test_no_active_uplink_drops_and_rejected_nft_stops(self):
        with patch.object(balancer, "call", return_value=result()) as call:
            balancer.rules([])
            self.assertIn(
                "add rule inet seu_lb choose counter drop", call.call_args.args[1]
            )
        with patch.object(balancer, "call", side_effect=[result(), result(1)]):
            with self.assertRaises(RuntimeError):
                balancer.rules(["wan1"])

    def test_cleanup_restores_snapshot_and_only_reserved_network_resources(self):
        backup = self.directory / "rp-filter.json"
        backup.write_text(
            json.dumps({"all": "1", "sta0": "2", "unsafe/name": "1", "sta1": "9"})
        )
        state = self.directory / "state.json"
        state.write_text("{}")
        restored = {}
        original_path = Path

        def fake_path(*parts):
            if parts and str(parts[0]) == "/proc/sys/net/ipv4/conf":
                device = parts[1]
                fake = MagicMock()
                fake.exists.return_value = True
                fake.write_text.side_effect = lambda content: restored.__setitem__(
                    device, content
                )
                return fake
            return original_path(*parts)

        deleted = set()

        def fake_call(argv, data=None, timeout=25):
            if argv[:3] == ["ip", "rule", "del"]:
                key = tuple(argv)
                if key not in deleted:
                    deleted.add(key)
                    return result()
                return result(2)
            return result()

        with patch.object(balancer, "RP_FILTER_BACKUP", backup), patch.object(
            balancer, "STATE", state
        ), patch.object(balancer, "Path", side_effect=fake_path), patch.object(
            balancer, "call", side_effect=fake_call
        ) as call:
            balancer.cleanup()
        self.assertEqual(restored, {"all": "1\n", "sta0": "2\n"})
        self.assertFalse(backup.exists())
        self.assertFalse(state.exists())
        commands = [invocation.args[0] for invocation in call.call_args_list]
        allowed = [["nft", "delete", "table", "inet", "seu_lb"]]
        allowed += [
            ["ip", "rule", "del", "pref", str(p)]
            for p in (101, 102, 103, 201, 202, 203, 300)
        ]
        allowed += [
            ["ip", "route", "flush", "table", str(t)] for t in (201, 202, 203, 204)
        ]
        for command in commands:
            self.assertIn(command, allowed)
        for command in allowed:
            self.assertIn(command, commands)


if __name__ == "__main__":
    unittest.main()
