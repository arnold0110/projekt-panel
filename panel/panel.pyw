"""Projekt-Panel – Control Panel für deine (Docker-)Projekte auf mehreren Rechnern.

Start-Varianten
  panel.pyw            Desktop-Verknüpfung: öffnet das Fenster (startet den Server bei Bedarf)
  panel.pyw --server   Hintergrund-Server (beim Login per Aufgabenplanung), inkl. Panel-Tunnel

Ohne machine.json läuft das Panel nur lokal (127.0.0.1 + Zufalls-Token) und beendet
sich, wenn das Fenster zu ist. Mit machine.json ist es zusätzlich über die eigene
Subdomain hinter Cloudflare Access erreichbar und kennt die anderen Rechner.
"""
import atexit
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402
import migrate  # noqa: E402
import remote  # noqa: E402

PANEL_DIR = core.PANEL_DIR
RUNNING_PATH = core.RUNNING_PATH
UI_PATH = core.UI_PATH
TUNNEL_LOG = PANEL_DIR / "panel_tunnel.log"
TEST_MODE = os.environ.get("PANEL_TEST_MODE") == "1"   # nur für automatisierte Tests

MACHINE = remote.load_machine()
TOKEN_PATH = PANEL_DIR / ".token"


def _load_token():
    """Lokales Zugangs-Token – bleibt über Neustarts gleich, damit offene Fenster weiter funktionieren."""
    try:
        t = TOKEN_PATH.read_text().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    try:
        TOKEN_PATH.write_text(t)
    except OSError:
        pass
    return t


TOKEN = _load_token()
SERVER_MODE = "--server" in sys.argv
last_ping = time.time()

LOCAL_ONLY_ACTIONS = {"open_folder", "open_vscode", "open_terminal", "open_url"}


def machine_info(identity):
    return {
        "machine": MACHINE["name"] if MACHINE else None,
        "peers": [p["name"] for p in MACHINE["peers"]] if MACHINE else [],
        "public_url": f"https://{MACHINE['public_hostname']}" if MACHINE and MACHINE.get("public_hostname") else None,
        "remote": identity != "local",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjektPanel"

    def log_message(self, *args):
        pass

    # ---------- Antworten
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    # ---------- Wer fragt?
    def _is_local_host(self):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        # Anfragen über den Cloudflare-Tunnel tragen immer Cf-Ray – die sind nie "lokal"
        if self.headers.get("Cf-Ray"):
            return False
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _identity(self):
        if self._is_local_host():
            if secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
                return "local"
            if TEST_MODE and self.headers.get("X-Test-Identity"):
                return self.headers["X-Test-Identity"]
            return None
        host = self.headers.get("Host", "")
        if MACHINE and host.split(":")[0] == MACHINE.get("public_hostname"):
            return remote.identify(self.headers, MACHINE, self.path.split("?")[0])
        if MACHINE:
            remote.log_access(f"DENY Host={host!r} {self.path.split('?')[0]}: unbekannter Host")
        return None

    def _read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("Anfrage zu groß")
        return self.rfile.read(length) if length else b""

    # ---------- Routing
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        global last_ping
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)

        if method == "GET" and path in ("/icon.png", "/favicon.ico"):
            f = PANEL_DIR / ("icon.png" if path == "/icon.png" else "icon.ico")
            if f.exists():
                return self._send(200, f.read_bytes(), "image/png" if f.suffix == ".png" else "image/x-icon")
            return self._send(404, {"error": "not found"})

        if method == "GET" and path == "/":
            # Lokal enthält die Seite selbst nichts Geheimes (Token steckt im #-Teil der URL).
            if self._is_local_host() or self._identity():
                return self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            return self._send(403, {"error": "forbidden"})

        identity = self._identity()
        if not identity or self.headers.get("X-Panel") != "1":
            if not self._is_local_host() or identity:
                remote.log_access(f"DENY {path}: identity={identity!r} X-Panel={self.headers.get('X-Panel')!r} "
                                  f"Host={self.headers.get('Host')!r}")
            return self._send(403, {"error": "forbidden"})
        if identity != "local":
            remote.log_access(f"ALLOW {identity} {method} {path}")
        last_ping = time.time()

        try:
            # Anfragen für einen anderen Rechner weiterreichen
            target = (query.get("m") or [None])[0]
            if target and MACHINE and target != MACHINE["name"]:
                if identity == "peer":
                    return self._send(403, {"error": "keine Weiterleitung für Peers"})
                peer = remote.find_peer(MACHINE, target)
                if not peer:
                    return self._send(404, {"error": f"Unbekannter Rechner {target}"})
                body = self._read_body(65536)
                fwd = urllib.parse.urlencode({k: v[0] for k, v in query.items() if k != "m"})
                result = remote.peer_request(MACHINE, peer, method, path + ("?" + fwd if fwd else ""),
                                             body=json.loads(body) if body else None, timeout=120)
                return self._send(200, result)

            if path.startswith("/api/peer/"):
                if identity not in ("peer", "local"):
                    return self._send(403, {"error": "nur für andere Panels"})
                if path == "/api/peer/upload" and method == "POST":
                    data = self._read_body(migrate.CHUNK_SIZE + 1024)
                    return self._send(200, migrate.receive_chunk(query.get("id", [""])[0],
                                                                 int(query.get("index", ["-1"])[0]), data))
                if path == "/api/peer/import" and method == "POST":
                    d = json.loads(self._read_body(65536) or b"{}")
                    return self._send(200, migrate.import_package(d.get("id"), d.get("sha256"), d.get("source", "?")))
                return self._send(404, {"error": "not found"})

            if method == "GET":
                if path == "/api/overview":
                    data = core.overview(check_online="online" in query)
                    data.update(machine_info(identity))
                    return self._send(200, data)
                if path == "/api/discover":
                    cfg = core.load_config()
                    return self._send(200, {"scan_root": cfg["scan_root"], "repos": core.discover(cfg["scan_root"], cfg["projects"])})
                if path.startswith("/api/job/"):
                    job = core.jobs.get(path.rsplit("/", 1)[-1])
                    return self._send(200, job) if job else self._send(404, {"error": "unbekannt"})
                if path == "/api/ping":
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})

            if method == "POST" and path.startswith("/api/action/"):
                name = path.rsplit("/", 1)[-1]
                data = json.loads(self._read_body(65536) or b"{}")
                if name in LOCAL_ONLY_ACTIONS and identity != "local":
                    return self._send(403, {"error": "Nur am Rechner selbst möglich"})
                if name == "move_project":
                    return self._send(200, start_move(data))
                # Doppelbetrieb verhindern: erst beim anderen Rechner nachfragen
                if name == "deploy" and not data.get("force"):
                    hits = remote.find_elsewhere(MACHINE, data.get("path", ""), "running")
                    if hits:
                        return self._send(409, {"error": f"Läuft bereits auf {hits[0]['machine']} – nicht doppelt starten.",
                                                "conflict": hits[0]})
                if name == "set_autostart" and data.get("value"):
                    hits = remote.find_elsewhere(MACHINE, data.get("path", ""), "autostart")
                    if hits:
                        return self._send(409, {"error": f"Autostart ist schon auf {hits[0]['machine']} an – "
                                                         f"ein Projekt darf nur auf einem Rechner automatisch starten.",
                                                "conflict": hits[0]})
                return self._send(200, core.action(name, data))
            return self._send(404, {"error": "not found"})
        except remote.PeerError as e:
            # Antwort des anderen Panels (z.B. 409 Konflikt) unverändert durchreichen
            if e.status and isinstance(e.body, dict) and 400 <= e.status < 500:
                return self._send(e.status, e.body)
            return self._send(502, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def start_move(data):
    if not MACHINE:
        raise ValueError("Dieser Rechner ist nicht für Fernzugriff eingerichtet (machine.json fehlt)")
    peer = remote.find_peer(MACHINE, data.get("target"))
    if not peer:
        raise ValueError("Unbekannter Ziel-Rechner")
    cfg = core.load_config()
    entry = core.find_project(cfg, data.get("path", ""))
    if not entry:
        raise ValueError("Unbekanntes Projekt")
    if any(not j["done"] and j["title"].startswith("Umzug") for j in core.jobs.values()):
        raise ValueError("Es läuft bereits ein Umzug")
    name = Path(entry["path"]).name
    return {"job": core.start_func_job(f"Umzug: {name} → {peer['name']}",
                                       lambda log: migrate.move_project(MACHINE, peer, entry, log))}


# ------------------------------------------------------------ Panel-Tunnel

tunnel_proc = None


def supervise_panel_tunnel(port):
    """Hält den Cloudflare-Tunnel für die Panel-Subdomain am Laufen."""
    global tunnel_proc
    name = MACHINE.get("panel_tunnel")
    exe = shutil.which("cloudflared")
    if not name or not exe:
        return
    while True:
        with open(TUNNEL_LOG, "a", encoding="utf-8") as log:
            log.write(f"\n--- Start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log.flush()
            tunnel_proc = subprocess.Popen([exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "run", name],
                                           stdout=log, stderr=subprocess.STDOUT, creationflags=core.NO_WINDOW)
            tunnel_proc.wait()
        time.sleep(15)


def _kill_tunnel():
    if tunnel_proc and tunnel_proc.poll() is None:
        tunnel_proc.terminate()


atexit.register(_kill_tunnel)


# ------------------------------------------------------------ Start

def open_window(url):
    if core.IS_WINDOWS:
        for candidate in (
            shutil.which("msedge"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), r"Microsoft\Edge\Application\msedge.exe"),
        ):
            if candidate and os.path.exists(candidate):
                subprocess.Popen([candidate, f"--app={url}", "--window-size=1250,880"])
                return
    webbrowser.open(url)


def running_url():
    """URL eines bereits laufenden Panels auf diesem Rechner (oder None)."""
    if not RUNNING_PATH.exists():
        return None
    try:
        info = json.loads(RUNNING_PATH.read_text())
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ping",
                                     headers={"X-Token": info["token"], "X-Panel": "1"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return f"http://127.0.0.1:{info['port']}/#{info['token']}"
    except Exception:  # noqa: BLE001
        return None
    return None


def restart_self():
    """Neuen Server-Prozess starten und diesen beenden (z.B. nach einem Update)."""
    args = [sys.executable, str(Path(__file__).resolve()), "--server", "--restarted"]
    subprocess.Popen(args, cwd=str(PANEL_DIR),
                     creationflags=(0x00000008 | 0x00000200) if core.IS_WINDOWS else 0,
                     start_new_session=not core.IS_WINDOWS)
    _kill_tunnel()
    os._exit(0)


def bind_server(port):
    """Port belegen – nach einem Neustart evtl. ein paar Sekunden warten, bis der alte Prozess weg ist."""
    for _ in range(60):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            if "--restarted" not in sys.argv or port == 0:
                raise
            time.sleep(0.5)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    global last_ping
    existing = None if "--restarted" in sys.argv else running_url()
    if existing:
        if not SERVER_MODE:
            open_window(existing)
        return

    # Fernzugriff eingerichtet -> Server dauerhaft im Hintergrund, Fenster nur als Ansicht
    if MACHINE and not SERVER_MODE and "--no-window" not in sys.argv:
        pyw = Path(sys.executable)
        subprocess.Popen([str(pyw), str(Path(__file__).resolve()), "--server"],
                         creationflags=(0x00000008 | 0x00000200) if core.IS_WINDOWS else 0,  # DETACHED | NEW_GROUP
                         cwd=str(PANEL_DIR))
        for _ in range(40):
            time.sleep(0.25)
            url = running_url()
            if url:
                open_window(url)
                return
        return

    if not core.CONFIG_PATH.exists():
        core.save_config(core.load_config())

    port = MACHINE["port"] if MACHINE else 0
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = bind_server(port)
    port = server.server_port
    RUNNING_PATH.write_text(json.dumps({"port": port, "token": TOKEN}))
    url = f"http://127.0.0.1:{port}/#{TOKEN}"

    persistent = SERVER_MODE or "--no-window" in sys.argv
    if "--no-window" in sys.argv:
        print(url, flush=True)
    elif not SERVER_MODE:
        open_window(url)

    if SERVER_MODE:
        core.RESTART_HOOK = restart_self
    if SERVER_MODE and MACHINE:
        threading.Thread(target=supervise_panel_tunnel, args=(port,), daemon=True).start()

    if not persistent:
        # Nur-lokal-Modus: beenden, wenn das Fenster länger zu ist
        def watchdog():
            while True:
                time.sleep(5)
                busy = any(not j["done"] for j in list(core.jobs.values()))
                if not busy and time.time() - last_ping > 240:
                    server.shutdown()
                    return
        last_ping = time.time() + 30
        threading.Thread(target=watchdog, daemon=True).start()

    try:
        server.serve_forever()
    finally:
        RUNNING_PATH.unlink(missing_ok=True)
        _kill_tunnel()


if __name__ == "__main__":
    main()
