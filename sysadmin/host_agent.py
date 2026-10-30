"""Read-only host metrics agent for the local Open WebUI assistant.

Runs as a user service on 172.17.0.1:8767 (docker bridge only) and exposes
GET /health, /metrics, /unit?name=..., /journal?unit=...&lines=N.
Every command is a fixed argv list with a timeout; nothing is written.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BIND = os.environ.get("HOSTAGENT_BIND", "172.17.0.1")
PORT = int(os.environ.get("HOSTAGENT_PORT", "8767"))
TIMEOUT = 8
UNIT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,119}$")


def _run(argv):
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def _meminfo():
    values = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if key and rest:
            values[key] = int(rest.split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {
        "total": total,
        "available": available,
        "used": total - available,
        "percent": round((total - available) * 100 / total, 1) if total else None,
        "swap_total": values.get("SwapTotal", 0),
        "swap_free": values.get("SwapFree", 0),
    }


def _disks():
    code, out, _ = _run(
        [
            "df",
            "-B1",
            "--output=source,size,used,avail,pcent,target",
            "-x",
            "tmpfs",
            "-x",
            "devtmpfs",
            "-x",
            "squashfs",
            "-x",
            "overlay",
        ]
    )
    if code != 0:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        source, size, used, avail, pcent, target = parts[0], parts[1], parts[2], parts[3], parts[4], " ".join(parts[5:])
        try:
            pct = int(pcent.rstrip("%"))
            size, used, avail = int(size), int(used), int(avail)
        except ValueError:
            continue
        if target in ("/", "/home", "/var", "/boot") or pct >= 80:
            rows.append(
                {"target": target, "source": source, "total": size, "used": used, "avail": avail, "percent": pct}
            )
    rows.sort(key=lambda r: r["target"])
    return rows[:8]


def _failed_units(user=False):
    argv = ["systemctl", "--failed", "--no-legend", "--plain"]
    if user:
        argv.insert(1, "--user")
    code, out, _ = _run(argv)
    if code != 0 or not out:
        return []
    return [line.strip()[:160] for line in out.splitlines()[:5]]


def _top_processes():
    code, out, _ = _run(["ps", "-eo", "pid=,comm=,pcpu=,pmem=,rss=", "--sort=-pcpu"])
    if code != 0:
        return []
    rows = []
    for line in out.splitlines()[:6]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, comm, pcpu, pmem, rss = parts
        rows.append({"pid": int(pid), "command": comm, "cpu_percent": float(pcpu), "mem_percent": float(pmem), "rss": int(rss) * 1024})
    return rows


def _gpu():
    if not shutil.which("nvidia-smi"):
        return None
    code, out, _ = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    if code != 0 or not out:
        return None
    first = out.splitlines()[0].split(",")
    if len(first) < 5:
        return None
    name, used, total, util, temp = [part.strip() for part in first[:5]]
    try:
        return {
            "name": name,
            "memory_used": int(used) * 1024 * 1024,
            "memory_total": int(total) * 1024 * 1024,
            "util_percent": int(util),
            "temperature_c": int(temp),
        }
    except ValueError:
        return None


def metrics():
    load = _read("/proc/loadavg").split()[:3]
    uptime = _read("/proc/uptime").split()
    return {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "uptime_seconds": float(uptime[0]) if uptime else None,
        "cpu_count": os.cpu_count(),
        "load": [float(x) for x in load] if len(load) == 3 else [],
        "memory": _meminfo(),
        "disks": _disks(),
        "gpu": _gpu(),
        "failed_units": _failed_units(),
        "user_failed_units": _failed_units(user=True),
        "top_processes": _top_processes(),
    }


def _show_unit(name, user=False):
    argv = ["systemctl", "show", "--no-pager"]
    if user:
        argv.insert(1, "--user")
    argv += [
        "-p",
        "LoadState",
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "UnitFileState",
        "-p",
        "NRestarts",
        "-p",
        "ExecMainStartTimestamp",
        name,
    ]
    return _run(argv)


def unit_status(name):
    code, out, err = _show_unit(name)
    scope = "system"
    data = {"unit": name, "scope": scope}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key:
            data[key] = value
    if data.get("LoadState") == "not-found":
        code, out, err = _show_unit(name, user=True)
        scope = "user"
        data = {"unit": name, "scope": scope}
        for line in out.splitlines():
            key, _, value = line.partition("=")
            if key:
                data[key] = value
    if data.get("LoadState") in (None, "not-found") and err:
        data["error"] = err
    return data


def journal(name, lines):
    code, out, err = _run(
        ["journalctl", "-u", name, "-n", str(lines), "-o", "short-iso", "--no-pager", "--no-hostname"]
    )
    if not out or "No entries" in out:
        code, out, err = _run(
            ["journalctl", f"--user-unit={name}", "-n", str(lines), "-o", "short-iso", "--no-pager", "--no-hostname"]
        )
    text = out or err
    return {"unit": name, "lines": lines, "text": text[:6000]}


class Handler(BaseHTTPRequestHandler):
    server_version = "hostagent/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(200, {"ok": True})
            return
        if parsed.path == "/metrics":
            self._send(200, metrics())
            return
        if parsed.path in ("/unit", "/journal"):
            name = (query.get("name") or query.get("unit") or [""])[0]
            if not UNIT_RE.match(name):
                self._send(400, {"error": "invalid unit name"})
                return
            if parsed.path == "/unit":
                self._send(200, unit_status(name))
                return
            try:
                lines = max(1, min(50, int((query.get("lines") or ["5"])[0])))
            except ValueError:
                lines = 5
            self._send(200, journal(name, lines))
            return
        self._send(404, {"error": "not found"})


def main():
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    print(f"host_agent on http://{BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
