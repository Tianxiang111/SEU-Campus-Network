#!/usr/bin/python3
"""SEU Portal authentication and connection-level IPv4 balancing.

Public adaptation of the two-uplink router implementation. It does not create
STA interfaces, configure NAT, or manage IPv6. See docs/scripts.md before use.
"""
import argparse, concurrent.futures, ipaddress, json, os, re, subprocess, time, urllib.parse
from pathlib import Path

CONFIG = Path(os.environ.get("SEU_ACCOUNTS_FILE", "/etc/seu-balancer/accounts.json"))
STATE = Path("/tmp/seu-balancer-state.json")
WEIGHTS = {"wan1": 2, "wan2": 1}
NETWORKS = {"wan1": "wan1", "wan2": "wan2"}
LAN_DEVICE = os.environ.get("SEU_LAN_DEVICE", "br-lan")
LAN_CIDR = str(ipaddress.IPv4Network(os.environ.get("SEU_LAN_CIDR", "192.168.8.0/24")))
REPAIR_RADIO = os.environ.get("SEU_REPAIR_RADIO", "")
RUNTIME = Path("/var/run/seu-balancer")
RP_FILTER_BACKUP = RUNTIME / "rp-filter.json"
if not re.fullmatch(r"[A-Za-z0-9_.:-]+", LAN_DEVICE):
    raise ValueError("invalid LAN device")


def load_accounts():
    if CONFIG.stat().st_mode & 0o077:
        raise ValueError("accounts file must have mode 0600")
    rows = json.loads(CONFIG.read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty accounts list")
    accounts = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid account entry")
        line = row.get("line")
        if line not in NETWORKS or line in accounts:
            raise ValueError("invalid or duplicate uplink")
        for key in ("username", "password"):
            value = row.get(key)
            if (
                not isinstance(value, str)
                or not value
                or (value.startswith("<") and value.endswith(">"))
            ):
                raise ValueError("fill in the local credentials first")
        accounts[line] = row
    return accounts


def call(args, data=None, timeout=25):
    return subprocess.run(
        args,
        input=data,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def checked(args, data=None):
    p = call(args, data)
    if p.returncode:
        raise RuntimeError("network command failed")
    return p


def set_rp_filter(device):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", device):
        raise ValueError("invalid device")
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    saved = (
        json.loads(RP_FILTER_BACKUP.read_text()) if RP_FILTER_BACKUP.exists() else {}
    )
    if device not in saved:
        saved[device] = (
            Path("/proc/sys/net/ipv4/conf", device, "rp_filter").read_text().strip()
        )
        RP_FILTER_BACKUP.write_text(json.dumps(saved))
        os.chmod(RP_FILTER_BACKUP, 0o600)
    checked(["sysctl", "-w", "net.ipv4.conf." + device + ".rp_filter=0"])


def cleanup():
    call(["nft", "delete", "table", "inet", "seu_lb"])
    for pref in [101, 102, 103, 201, 202, 203, 300]:
        while call(["ip", "rule", "del", "pref", str(pref)]).returncode == 0:
            pass
    for table in [201, 202, 203, 204]:
        call(["ip", "route", "flush", "table", str(table)])
    if RP_FILTER_BACKUP.exists():
        saved = json.loads(RP_FILTER_BACKUP.read_text())
        for device, value in saved.items():
            if re.fullmatch(r"[A-Za-z0-9_.:-]+", device) and value in ("0", "1", "2"):
                path = Path("/proc/sys/net/ipv4/conf", device, "rp_filter")
                if path.exists():
                    path.write_text(value + "\n")
        RP_FILTER_BACKUP.unlink()
    STATE.unlink(missing_ok=True)


def curl(ip, url):
    cfg = (
        "url = "
        + json.dumps(url)
        + "\ninterface = "
        + json.dumps(ip)
        + '\nresolve = "w.seu.edu.cn:443:10.80.128.2"\nresolve = "w.seu.edu.cn:802:10.80.128.2"\nresolve = "www.cloudflare.com:443:104.16.124.96,104.16.123.96"\nsilent\nshow-error\nconnect-timeout = 4\nmax-time = 8\n'
    )
    return call(["curl", "--config", "-"], cfg)


def interfaces():
    result = {}
    for n in range(1, 4):
        line = "wan" + str(n)
        try:
            s = json.loads(
                call(
                    ["ubus", "call", "network.interface." + NETWORKS[line], "status"]
                ).stdout
            )
            if not s.get("up"):
                continue
            a = s["ipv4-address"][0]
            ip = a["address"]
            dev = s["l3_device"]
            gw = next(r["nexthop"] for r in s["route"] if r["target"] == "0.0.0.0")
            result[line] = {
                "ip": ip,
                "device": dev,
                "gateway": gw,
                "subnet": str(
                    ipaddress.ip_network(ip + "/" + str(a["mask"]), strict=False)
                ),
                "table": 200 + n,
                "mark": 256 + n,
            }
        except (ValueError, KeyError, IndexError, StopIteration):
            pass
    return result


def routes(lines):
    for n in range(1, 4):
        line = "wan" + str(n)
        table = 200 + n
        call(["ip", "rule", "del", "pref", str(100 + n)])
        call(["ip", "rule", "del", "pref", str(200 + n)])
        call(["ip", "route", "flush", "table", str(table)])
        if line in lines:
            s = lines[line]
            checked(
                [
                    "ip",
                    "route",
                    "add",
                    s["subnet"],
                    "dev",
                    s["device"],
                    "src",
                    s["ip"],
                    "table",
                    str(table),
                ]
            )
            checked(
                ["ip", "route", "add", LAN_CIDR, "dev", LAN_DEVICE, "table", str(table)]
            )
            checked(
                [
                    "ip",
                    "route",
                    "add",
                    "default",
                    "via",
                    s["gateway"],
                    "dev",
                    s["device"],
                    "src",
                    s["ip"],
                    "table",
                    str(table),
                ]
            )
            checked(
                [
                    "ip",
                    "rule",
                    "add",
                    "pref",
                    str(100 + n),
                    "from",
                    s["ip"] + "/32",
                    "table",
                    str(table),
                ]
            )
            set_rp_filter(s["device"])
        else:
            checked(["ip", "route", "add", "blackhole", "default", "table", str(table)])
        checked(
            [
                "ip",
                "rule",
                "add",
                "pref",
                str(200 + n),
                "fwmark",
                str(256 + n) + "/0xfff",
                "table",
                str(table),
            ]
        )
    set_rp_filter("all")


def router_default(active, lines):
    # DNS and time synchronization must also move away from a failed uplink.
    choices = [line for line in active if line in lines] or list(lines)
    choices.sort(key=lambda line: -WEIGHTS[line])
    call(["ip", "rule", "del", "pref", "300"])
    call(["ip", "route", "flush", "table", "204"])
    checked(["ip", "route", "add", LAN_CIDR, "dev", LAN_DEVICE, "table", "204"])
    if "wan3" in lines:
        relay = lines["wan3"]
        checked(
            [
                "ip",
                "route",
                "add",
                relay["subnet"],
                "dev",
                relay["device"],
                "src",
                relay["ip"],
                "table",
                "204",
            ]
        )
    if choices:
        s = lines[choices[0]]
        checked(
            [
                "ip",
                "route",
                "add",
                s["subnet"],
                "dev",
                s["device"],
                "src",
                s["ip"],
                "table",
                "204",
            ]
        )
        checked(
            [
                "ip",
                "route",
                "add",
                "default",
                "via",
                s["gateway"],
                "dev",
                s["device"],
                "src",
                s["ip"],
                "table",
                "204",
            ]
        )
    checked(["ip", "rule", "add", "pref", "300", "table", "204"])


def rules(active):
    # Dedicated table survives fw4 reload. A single transaction changes selection.
    if call(["nft", "list", "table", "inet", "seu_lb"]).returncode:
        checked(["nft", "add", "table", "inet", "seu_lb"])
    marks = [256 + int(line[-1]) for line in active for _ in range(WEIGHTS[line])]
    if not marks and active:
        marks = [256 + int(line[-1]) for line in active]
    select = ""
    if marks:
        mapping = ", ".join(str(i) + " : " + str(mark) for i, mark in enumerate(marks))
        select = (
            "ct mark set numgen inc mod "
            + str(len(marks))
            + " map { "
            + mapping
            + " } counter meta mark set ct mark"
        )
    else:
        select = "counter drop"
    script = (
        """flush table inet seu_lb
add chain inet seu_lb choose
add chain inet seu_lb prerouting { type filter hook prerouting priority -150; policy accept; }
add rule inet seu_lb prerouting meta nfproto != ipv4 return
add rule inet seu_lb prerouting iifname "@LAN_DEVICE@" meta nfproto ipv4 fib daddr type local return
add rule inet seu_lb prerouting iifname "@LAN_DEVICE@" ip daddr { @LAN_CIDR@, 224.0.0.0/4, 255.255.255.255 } return
add rule inet seu_lb prerouting iifname "@LAN_DEVICE@" meta nfproto ipv4 ct mark & 0xfff != 0 meta mark set ct mark & 0xfff
add rule inet seu_lb prerouting iifname "@LAN_DEVICE@" meta nfproto ipv4 ct mark & 0xfff == 0 jump choose
add rule inet seu_lb choose """
        + select
        + "\n"
        + "".join(
            'add rule inet seu_lb prerouting iifname "@LAN_DEVICE@" ct mark '
            + str(256 + n)
            + ' counter comment "wan'
            + str(n)
            + ' traffic"\n'
            for n in range(1, 4)
        )
    )
    script = script.replace("@LAN_DEVICE@", LAN_DEVICE).replace("@LAN_CIDR@", LAN_CIDR)
    p = call(["nft", "-f", "-"], script)
    if p.returncode:
        raise RuntimeError("nft rules rejected")


def health(s):
    # HTTPS validates remote identity: a captive portal cannot count as success.
    for url in ["https://www.cloudflare.com/cdn-cgi/trace", "https://www.baidu.com/"]:
        try:
            p = curl(s["ip"], url)
        except subprocess.TimeoutExpired:
            continue
        if p.returncode == 0 and (
            ("ip=" in p.stdout and "h=" in p.stdout)
            if "cloudflare" in url
            else "<html" in p.stdout.lower()
        ):
            return True
    return False


def authenticate(line, s, account):
    try:
        page = curl(s["ip"], "https://w.seu.edu.cn/")
    except subprocess.TimeoutExpired:
        return "unreachable"
    if page.returncode or "Dr.COMWebLoginID_0.htm" not in page.stdout:
        return "not-needed-or-unreachable"
    portal_ip = s["ip"]
    if line == "wan3":
        candidates = {
            ip
            for ip in re.findall("[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+", page.stdout)
            if ip.startswith("10.210.")
        }
        if len(candidates) != 1:
            return "relay-address-not-confirmed"
        portal_ip = candidates.pop()
    params = {
        "c": "Portal",
        "a": "login",
        "callback": "dr1000",
        "login_method": "1",
        "user_account": ",0," + account["username"],
        "user_password": account["password"],
        "wlan_user_ip": portal_ip,
        "wlan_user_ipv6": "",
        "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "10.80.128.2",
        "wlan_ac_name": "",
        "jsVersion": "3.3.3",
    }
    try:
        p = curl(
            s["ip"],
            "https://w.seu.edu.cn:802/eportal/?" + urllib.parse.urlencode(params),
        )
    except subprocess.TimeoutExpired:
        return "unreachable"
    try:
        response = json.loads(p.stdout[p.stdout.index("{") : p.stdout.rindex("}") + 1])
        if str(response.get("result")) in ["1", "ok"]:
            return "success"
        if response.get("ret_code") == 2:
            return "already-online"
        return "rejected"
    except (ValueError, IndexError):
        return "unreachable"


def main():
    accounts = load_accounts()
    counters = {
        line: {
            "failures": 0,
            "successes": 0,
            "active": False,
            "last_auth": 0,
            "auth_result": "pending",
        }
        for line in accounts
    }
    # Preserve recently verified uplinks during a service-only restart.
    try:
        saved = json.loads(STATE.read_text())
        if 0 <= time.time() - saved["time"] <= 60:
            for line in saved["active"]:
                if line in counters and saved["lines"][line]["healthy"]:
                    counters[line].update(active=True, successes=2)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    previous = None
    selected = None
    last_repair = 0
    while True:
        start = time.monotonic()
        lines = interfaces()
        changed = lines != previous
        if changed:
            routes(lines)
            previous = lines
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {line: pool.submit(health, s) for line, s in lines.items()}
            healthy = {line: f.result() for line, f in futures.items()}
        for line, c in counters.items():
            ok = healthy.get(line, False)
            if ok:
                c["successes"] += 1
                c["failures"] = 0
                if c["successes"] >= 2:
                    c["active"] = True
            else:
                c["failures"] += 1
                c["successes"] = 0
                if c["failures"] >= 3 or line not in lines:
                    c["active"] = False
                if line in lines and time.monotonic() - c["last_auth"] >= 120:
                    c["last_auth"] = time.monotonic()
                    c["auth_result"] = authenticate(line, lines[line], accounts[line])
        # netifd may lose its radio0 device association during a network reload.
        # Repair only the dedicated STA radio, preserving both local AP radios.
        if (
            REPAIR_RADIO
            and "wan2" in counters
            and "wan2" not in lines
            and counters["wan2"]["failures"] >= 3
            and time.monotonic() - last_repair >= 180
        ):
            last_repair = time.monotonic()
            checked(["wifi", "down", REPAIR_RADIO])
            checked(["wifi", "up", REPAIR_RADIO])
            checked(["ifup", "wan2"])
        active = [line for line, c in counters.items() if c["active"] and line in lines]
        if active != selected:
            rules(active)
            router_default(active, lines)
            selected = active
        elif changed:
            router_default(active, lines)
        public = {
            line: {k: v for k, v in c.items() if k != "last_auth"}
            | {
                "ip": lines.get(line, {}).get("ip"),
                "device": lines.get(line, {}).get("device"),
                "healthy": healthy.get(line, False),
            }
            for line, c in counters.items()
        }
        tmp = STATE.with_suffix(".new")
        tmp.write_text(
            json.dumps(
                {
                    "time": int(time.time()),
                    "active": active,
                    "weights": WEIGHTS,
                    "lines": public,
                }
            )
        )
        os.chmod(tmp, 0o644)
        tmp.replace(STATE)
        time.sleep(max(1, 30 - (time.monotonic() - start)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="remove owned IPv4 policies after stopping the service",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run on the router as root")
    if args.cleanup:
        cleanup()
        raise SystemExit(0)
    try:
        load_accounts()
    except (OSError, ValueError, TypeError, KeyError):
        parser.error("invalid local accounts file; see docs/scripts.md")
    while True:
        try:
            main()
        except Exception as error:
            # Log only the exception class: URLs, account names and passwords stay private.
            call(
                [
                    "logger",
                    "-t",
                    "seu-balancer",
                    "Restarting after " + type(error).__name__,
                ]
            )
            time.sleep(5)
