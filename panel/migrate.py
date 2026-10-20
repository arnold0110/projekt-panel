"""Hosting-Umzug eines Projekts von diesem Rechner auf einen anderen (über Cloudflare).

Ablauf (Quelle):
  1. Ziel erreichbar + Docker läuft dort?
  2. Projekt hier stoppen (Container + Tunnel) – ab jetzt läuft es nirgends doppelt.
  3. Paket bauen: Projektordner (inkl. .git, config.py, Bilder …), Docker-Volumes,
     Tunnel-Zugangsdaten aus ~/.cloudflared.
  4. Paket in Stücken hochladen, Ziel importiert und startet es (deploy.py).
  5. Erfolg: Autostart hier aus, dort an.  Fehler: Ziel stoppen, hier wieder starten.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import core
from remote import PeerError, peer_request

CHUNK_SIZE = 32 * 1024 * 1024          # bleibt unter Cloudflares 100-MB-Limit pro Request
MAX_PACKAGE = 4 * 1024 * 1024 * 1024    # 4 GB
INCOMING_DIR = core.BASE_DIR / "incoming"
OUTGOING_DIR = core.BASE_DIR / "outgoing"
BACKUP_DIR = core.BASE_DIR / "backups"
CLOUDFLARED_DIR = Path.home() / ".cloudflared"
ALPINE = "alpine:3.20"

EXCLUDE_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}
EXCLUDE_FILES = {"cloudflared.pid", "cloudflared.log"}


# ------------------------------------------------------------ Helfer

def _docker_bin(args, stdin=None, stdout=None, timeout=600):
    p = subprocess.run(["docker", *args], stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                       timeout=timeout, creationflags=core.NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {p.stderr.decode(errors='replace').strip()}")


def container_volumes(container):
    if not container:
        return []
    code, out = core.run(["docker", "inspect", "--format", "{{json .Mounts}}", container])
    if code != 0:
        return []
    try:
        mounts = json.loads(out.strip() or "[]")
    except json.JSONDecodeError:
        return []
    return [m["Name"] for m in mounts if m.get("Type") == "volume" and m.get("Name")]


def tunnel_credentials(folder):
    """Zugangsdaten-Datei des Projekt-Tunnels (+ cert.pem) – Liste von Pfaden."""
    files = []
    name = core.parse_deploy(folder).get("tunnel_name")
    if name:
        try:
            # nur stdout lesen – Warnungen (z.B. "version outdated") kommen auf stderr
            p = subprocess.run(["cloudflared", "tunnel", "list", "--output", "json"], capture_output=True,
                               timeout=30, creationflags=core.NO_WINDOW)
            out = p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out.strip():
            try:
                for t in json.loads(out):
                    if t.get("name") == name:
                        f = CLOUDFLARED_DIR / f"{t['id']}.json"
                        if f.exists():
                            files.append(f)
            except (ValueError, KeyError):
                pass
    cert = CLOUDFLARED_DIR / "cert.pem"
    if cert.exists():
        files.append(cert)
    return files


def _safe_member(name):
    p = Path(name)
    return not (p.is_absolute() or ".." in p.parts or name.startswith(("/", "\\")) or ":" in name)


def _rmtree(path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)  # .git-Dateien sind unter Windows schreibgeschützt
        func(p)
    shutil.rmtree(path, onerror=onerror)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------ Quelle

def build_package(folder, container, log):
    OUTGOING_DIR.mkdir(exist_ok=True)
    folder = Path(folder)
    volumes = container_volumes(container)
    creds = tunnel_credentials(folder)
    pkg = OUTGOING_DIR / f"{folder.name}-{int(time.time())}.zip"
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        count = 0
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                if f in EXCLUDE_FILES or f.endswith(".pyc"):
                    continue
                full = Path(root) / f
                z.write(full, "project/" + full.relative_to(folder).as_posix())
                count += 1
        log(f"   {count} Dateien aus dem Projektordner")
        for vol in volumes:
            tar = Path(tmp) / f"{vol}.tar"
            with open(tar, "wb") as out:
                _docker_bin(["run", "--rm", "-v", f"{vol}:/src:ro", ALPINE, "tar", "cf", "-", "-C", "/src", "."], stdout=out)
            z.write(tar, f"volumes/{vol}.tar")
            log(f"   Docker-Volume {vol} ({tar.stat().st_size // 1024} KB)")
        for c in creds:
            z.write(c, f"cloudflared/{c.name}")
        if creds:
            log(f"   Tunnel-Zugang: {', '.join(c.name for c in creds)}")
        else:
            log("   ⚠️ keine Tunnel-Zugangsdaten gefunden – Ziel braucht sie schon selbst")
        z.writestr("manifest.json", json.dumps({
            "folder_name": folder.name, "volumes": volumes, "created": time.time(),
        }))
    if pkg.stat().st_size > MAX_PACKAGE:
        pkg.unlink()
        raise RuntimeError("Paket größer als 4 GB – Umzug abgebrochen")
    return pkg


def upload(machine, peer, pkg, log):
    upload_id = os.urandom(8).hex()
    size = pkg.stat().st_size
    sent = 0
    with open(pkg, "rb") as f:
        index = 0
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            for attempt in range(3):
                try:
                    peer_request(machine, peer, "POST", f"/api/peer/upload?id={upload_id}&index={index}",
                                 raw=chunk, timeout=300)
                    break
                except PeerError:
                    if attempt == 2:
                        raise
                    time.sleep(3)
            sent += len(chunk)
            index += 1
            log(f"   hochgeladen: {sent * 100 // max(size, 1)} % ({sent // (1024 * 1024)} / {max(size // (1024 * 1024), 1)} MB)")
    return upload_id


def move_project(machine, peer, entry, log):
    folder = entry["path"]
    name = Path(folder).name
    container = core.project_container(entry)

    log(f"⇄ Umzug von „{name}“: {machine['name']} → {peer['name']}")
    log("1/5 Prüfe Ziel …")
    ov = peer_request(machine, peer, "GET", "/api/overview", timeout=60)
    if not ov.get("docker_ok"):
        raise RuntimeError(f"Docker läuft auf {peer['name']} nicht – bitte dort Docker Desktop starten")
    log(f"   {peer['name']} erreichbar, Docker läuft")

    log("2/5 Stoppe Projekt hier (Container + Tunnel) …")
    out = core.stop_project(folder, container)
    if out:
        log("   " + out.replace("\n", "\n   "))

    pkg = None
    target_path = None
    try:
        log("3/5 Packe Projekt …")
        pkg = build_package(folder, container, log)
        log(f"   Paket: {pkg.stat().st_size // 1024} KB")

        log(f"4/5 Übertrage nach {peer['name']} …")
        upload_id = upload(machine, peer, pkg, log)
        r = peer_request(machine, peer, "POST", "/api/peer/import",
                         body={"id": upload_id, "sha256": _sha256(pkg), "source": machine["name"]}, timeout=120)
        target_path = r.get("path")

        log(f"5/5 {peer['name']} importiert und startet …")
        job_id, seen = r["job"], 0
        while True:
            time.sleep(2)
            j = peer_request(machine, peer, "GET", f"/api/job/{job_id}", timeout=60)
            new = j["output"][seen:]
            seen = len(j["output"])
            for line in new.splitlines():
                log(f"   [{peer['name']}] {line}")
            if j["done"]:
                break
        if j["code"] != 0:
            raise RuntimeError(f"Start auf {peer['name']} fehlgeschlagen (Exit-Code {j['code']})")
    except Exception as e:  # noqa: BLE001
        log(f"❌ {e}")
        log("↩ Rollback: Projekt wird wieder hier gestartet …")
        if target_path:
            try:
                peer_request(machine, peer, "POST", "/api/action/stop", body={"path": target_path}, timeout=120)
                peer_request(machine, peer, "POST", "/api/action/set_autostart",
                             body={"path": target_path, "value": False}, timeout=60)
                log(f"   auf {peer['name']} gestoppt")
            except PeerError as pe:
                log(f"   ⚠️ konnte {peer['name']} nicht stoppen: {pe} – bitte dort prüfen, sonst läuft es doppelt!")
        if (Path(folder) / "deploy.py").exists():
            code = core.stream_cmd([core.python_exe(), "deploy.py"], folder, log, prefix="   ")
            log("   wieder hier gestartet" if code == 0 else f"   ⚠️ Neustart hier fehlgeschlagen (Exit-Code {code})")
        return 1
    finally:
        if pkg and pkg.exists():
            pkg.unlink()

    cfg = core.load_config()
    e = core.find_project(cfg, folder)
    if e:
        e["autostart"] = False
        e["hosted_on"] = peer["name"]
        core.save_config(cfg)
    log("")
    log(f"✅ Fertig – „{name}“ läuft jetzt auf {peer['name']}.")
    log(f"   Autostart: hier aus, auf {peer['name']} an")
    return 0


# ------------------------------------------------------------ Ziel

_upload_re = re.compile(r"^[0-9a-f]{16}$")


def receive_chunk(upload_id, index, data):
    if not _upload_re.match(upload_id or "") or index < 0 or index > 10000:
        raise ValueError("ungültiger Upload")
    INCOMING_DIR.mkdir(exist_ok=True)
    part = INCOMING_DIR / f"{upload_id}.part"
    expected = (part.stat().st_size if part.exists() else 0)
    if index == 0 and part.exists():
        part.unlink()
        expected = 0
    if expected != index * CHUNK_SIZE:
        raise ValueError(f"Stück {index} passt nicht (erwartet ab Byte {expected})")
    with open(part, "ab") as f:
        f.write(data)
    if part.stat().st_size > MAX_PACKAGE:
        part.unlink()
        raise ValueError("Paket zu groß")
    return {"ok": True}


def import_package(upload_id, sha256, source):
    """Prüft das Paket und startet den Import als Job. Gibt {job, path} zurück."""
    if not _upload_re.match(upload_id or ""):
        raise ValueError("ungültiger Upload")
    part = INCOMING_DIR / f"{upload_id}.part"
    if not part.exists():
        raise ValueError("Upload nicht gefunden")
    if _sha256(part) != sha256:
        part.unlink()
        raise ValueError("Prüfsumme stimmt nicht – Übertragung fehlerhaft")
    with zipfile.ZipFile(part) as z:
        manifest = json.loads(z.read("manifest.json"))
        bad = [n for n in z.namelist() if not _safe_member(n)]
        if bad:
            raise ValueError("Paket enthält unsichere Pfade")
    folder_name = manifest["folder_name"]
    if not re.match(r"^[\w.\- ]+$", folder_name):
        raise ValueError("ungültiger Ordnername")
    cfg = core.load_config()
    existing = next((p for p in cfg["projects"] if Path(p["path"]).name.lower() == folder_name.lower()), None)
    target = Path(existing["path"]) if existing else Path(cfg["scan_root"]) / folder_name

    def job(log):
        log(f"Import von „{folder_name}“ (von {source}) nach {target}")
        with zipfile.ZipFile(part) as z:
            # laufende Instanz hier stoppen, bevor Dateien ersetzt werden
            container = None
            if target.exists():
                entry = core.find_project(cfg, str(target)) or {"path": str(target)}
                container = core.project_container(entry)
                out = core.stop_project(str(target), container)
                if out:
                    log(out)
                BACKUP_DIR.mkdir(exist_ok=True)
                backup = BACKUP_DIR / f"{folder_name}-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.make_archive(str(backup), "zip", target)
                log(f"Sicherung des alten Ordners: {backup}.zip")
                _rmtree(target)
            target.mkdir(parents=True)
            n = 0
            for info in z.infolist():
                if info.filename.startswith("project/") and not info.is_dir():
                    rel = info.filename[len("project/"):]
                    dest = (target / rel).resolve()
                    if not str(dest).startswith(str(target.resolve())):
                        raise RuntimeError("unsicherer Pfad im Paket")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(dest, "wb") as out_f:
                        shutil.copyfileobj(src, out_f)
                    n += 1
            log(f"{n} Dateien entpackt")

            names = z.namelist()
            creds = [n_ for n_ in names if n_.startswith("cloudflared/") and not n_.endswith("/")]
            if creds:
                CLOUDFLARED_DIR.mkdir(exist_ok=True)
                for c in creds:
                    dest = CLOUDFLARED_DIR / Path(c).name
                    if dest.name == "cert.pem" and dest.exists():
                        continue
                    dest.write_bytes(z.read(c))
                log("Tunnel-Zugang übernommen")

            container = container or core.parse_deploy(str(target)).get("container")
            for vol in manifest.get("volumes", []):
                if not re.match(r"^[\w.\-]+$", vol):
                    raise RuntimeError(f"ungültiger Volume-Name {vol}")
                if container:
                    core.run(["docker", "rm", "-f", container], timeout=60)
                core.run(["docker", "volume", "rm", "-f", vol], timeout=60)
                _docker_bin(["volume", "create", vol], stdout=subprocess.DEVNULL)
                with tempfile.TemporaryFile() as tmp:
                    with z.open(f"volumes/{vol}.tar") as src:
                        shutil.copyfileobj(src, tmp)
                    tmp.seek(0)
                    _docker_bin(["run", "--rm", "-i", "-v", f"{vol}:/dest", ALPINE, "tar", "xf", "-", "-C", "/dest"], stdin=tmp)
                log(f"Docker-Volume {vol} wiederhergestellt")
        part.unlink(missing_ok=True)

        cfg2 = core.load_config()
        e = core.find_project(cfg2, str(target))
        if not e:
            e = {"path": str(target)}
            cfg2["projects"].append(e)
        e["autostart"] = True
        e.pop("hosted_on", None)
        core.save_config(cfg2)
        log("Autostart hier eingeschaltet")

        if not (target / "deploy.py").exists():
            log("kein deploy.py – Projekt wurde nur kopiert")
            return 0
        log("Starte deploy.py …")
        return core.stream_cmd([core.python_exe(), "deploy.py"], str(target), log)

    return {"job": core.start_func_job(f"Import: {folder_name}", job), "path": str(target)}
