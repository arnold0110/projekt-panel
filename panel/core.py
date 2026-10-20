"""Kernlogik des Projekt-Panels: Projekte, Git/Docker-Status, Aktionen, Jobs."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
BASE_DIR = PANEL_DIR.parent
CONFIG_PATH = BASE_DIR / "projects.json"
RUNNING_PATH = PANEL_DIR / ".running.json"
UI_PATH = PANEL_DIR / "ui.html"
AUTOSTART_TASK = "Docker-Projekte Autostart"
IS_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: keine aufblitzenden Konsolen

config_lock = threading.Lock()
LOG_DIR = BASE_DIR / "logs"


def refresh_path():
    """PATH frisch aus der Windows-Registry lesen.

    Als geplante Aufgabe gestartet, erbt das Panel einen evtl. veralteten PATH – frisch
    installierte Programme (cloudflared, docker, git …) wären dann unsichtbar.
    """
    if not IS_WINDOWS:
        return
    try:
        import winreg
        parts = []
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, r"Environment")):
            try:
                with winreg.OpenKey(root, key) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    parts += [os.path.expandvars(x) for x in val.split(";") if x.strip()]
            except OSError:
                pass
        current = os.environ.get("PATH", "").split(";")
        merged = []
        for x in parts + current:
            if x and x.lower() not in (m.lower() for m in merged):
                merged.append(x)
        os.environ["PATH"] = ";".join(merged)
    except Exception:  # noqa: BLE001
        pass


refresh_path()


# ------------------------------------------------------------ Hilfen

def run(cmd, cwd=None, timeout=30):
    """Führt einen Befehl aus und gibt (exitcode, ausgabe) zurück – wirft nie."""
    refresh_path()
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=NO_WINDOW,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"Programm nicht gefunden: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "Zeitüberschreitung"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def powershell(script, timeout=30):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)


def python_exe():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def default_scan_root():
    return str(Path.home())


# ------------------------------------------------------------ Konfiguration

def load_config():
    with config_lock:
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError:
                cfg = {}
        else:
            cfg = {}
    cfg.setdefault("scan_root", default_scan_root())
    cfg.setdefault("projects", [])
    return cfg


def save_config(cfg):
    with config_lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)


def find_project(cfg, path):
    norm = os.path.normcase(os.path.normpath(path))
    for p in cfg["projects"]:
        if os.path.normcase(os.path.normpath(p["path"])) == norm:
            return p
    return None


# ------------------------------------------------------------ Projekt-Infos

def parse_deploy(folder):
    """Liest Container-Name, Port und öffentliche Adresse aus deploy.py."""
    f = Path(folder) / "deploy.py"
    if not f.exists():
        return {}
    text = f.read_text(encoding="utf-8", errors="replace")
    info = {}
    m = re.search(r'^\s*CONTAINER_NAME\s*=\s*["\']([^"\']+)', text, re.M) or re.search(r"--name\s+([\w.-]+)", text)
    if m:
        info["container"] = m.group(1)
    m = re.search(r"^\s*HOST_PORT\s*=\s*(\d+)", text, re.M) or re.search(r"-p\s+(\d+):\d+", text)
    if m:
        info["port"] = int(m.group(1))
    m = re.search(r'^\s*PUBLIC_HOSTNAME\s*=\s*["\']([^"\']+)', text, re.M)
    if m:
        info["hostname"] = m.group(1)
    m = re.search(r'^\s*TUNNEL_NAME\s*=\s*["\']([^"\']+)', text, re.M)
    if m:
        info["tunnel_name"] = m.group(1)
    m = re.search(r'^\s*HOSTING_MODE\s*=\s*["\'](\w+)', text, re.M)
    if m:
        info["mode"] = m.group(1)
    return info


def github_url(remote):
    remote = remote.strip()
    m = re.match(r"git@github\.com:(.+?)(\.git)?$", remote)
    if m:
        return "https://github.com/" + m.group(1)
    m = re.match(r"https://(?:[^@/]+@)?github\.com/(.+?)(\.git)?$", remote)
    if m:
        return "https://github.com/" + m.group(1)
    return None


def git_info(folder):
    if not (Path(folder) / ".git").exists():
        return None
    info = {}
    _, out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=folder)
    info["branch"] = out.strip().splitlines()[0] if out.strip() else "?"
    code, out = run(["git", "log", "-1", "--format=%h%x1f%s%x1f%cr%x1f%cI"], cwd=folder)
    if code == 0 and out.strip():
        parts = out.strip().split("\x1f")
        if len(parts) == 4:
            info["commit"] = {"hash": parts[0], "subject": parts[1], "relative": parts[2], "date": parts[3]}
    _, out = run(["git", "status", "--porcelain"], cwd=folder)
    info["changes"] = len([l for l in out.splitlines() if l.strip()])
    code, out = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=folder)
    if code == 0 and out.strip():
        ahead, behind = out.split()[:2]
        info["ahead"], info["behind"] = int(ahead), int(behind)
    code, out = run(["git", "remote", "get-url", "origin"], cwd=folder)
    info["remote"] = out.strip() if code == 0 and out.strip() else None
    info["github"] = github_url(info["remote"]) if info["remote"] else None
    return info


def docker_containers():
    code, out = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=15)
    if code != 0:
        return None  # Docker läuft nicht / nicht installiert
    result = {}
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        result[c.get("Names")] = {"state": c.get("State"), "status": c.get("Status"), "ports": c.get("Ports")}
    return result


def tunnel_running(folder):
    pid_file = Path(folder) / "cloudflared.pid"
    if not pid_file.exists():
        return False
    pid = pid_file.read_text(errors="replace").strip()
    if not pid.isdigit():
        return False
    if IS_WINDOWS:
        _, out = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return pid in out
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def check_url(url):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Projekt-Panel"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return {"ok": r.status < 400, "code": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": e.code < 500, "code": e.code}
    except Exception:  # noqa: BLE001
        return {"ok": False, "code": None}


def project_status(entry, containers, check_online):
    folder = entry["path"]
    exists = Path(folder).is_dir()
    deploy = parse_deploy(folder) if exists else {}
    container = entry.get("container") or deploy.get("container")
    hostname = deploy.get("hostname")
    public_url = entry.get("url") or (f"https://{hostname}" if hostname else None)
    local_url = f"http://localhost:{deploy['port']}" if deploy.get("port") else None
    st = {
        "path": folder,
        "name": entry.get("name") or Path(folder).name,
        "note": entry.get("note", ""),
        "overrides": {k: entry.get(k, "") for k in ("name", "url", "container", "note")},
        "exists": exists,
        "autostart": bool(entry.get("autostart")),
        "hosted_on": entry.get("hosted_on"),
        "deployable": exists and (Path(folder) / "deploy.py").exists(),
        "has_dockerfile": exists and (Path(folder) / "Dockerfile").exists(),
        "mode": deploy.get("mode"),
        "container": container,
        "public_url": public_url,
        "local_url": local_url,
        "git": git_info(folder) if exists else None,
        "docker": None,
        "tunnel": tunnel_running(folder) if exists else False,
        "online": check_url(public_url) if check_online else None,
    }
    if containers is not None and container:
        st["docker"] = containers.get(container) or {"state": "missing", "status": "nicht vorhanden"}
    return st


def autostart_task():
    if not IS_WINDOWS:
        return {"exists": False, "state": None, "last_run": None, "last_result": None}
    code, out = powershell(
        f"$t = Get-ScheduledTask -TaskName '{AUTOSTART_TASK}' -ErrorAction SilentlyContinue; "
        "if ($t) { $i = $t | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{state=[string]$t.State; last_run=$i.LastRunTime.ToString('dd.MM.yyyy HH:mm'); "
        "last_result=$i.LastTaskResult} | ConvertTo-Json -Compress }"
    )
    out = out.strip()
    if code != 0 or not out.startswith("{"):
        return {"exists": False, "state": None, "last_run": None, "last_result": None}
    data = json.loads(out)
    data["exists"] = True
    return data


def discover(scan_root, tracked):
    root = Path(scan_root)
    found = []
    if not root.is_dir():
        return found
    tracked_norm = {os.path.normcase(os.path.normpath(p["path"])) for p in tracked}
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for d in entries:
        try:
            if not d.is_dir() or d.name.startswith(".") or not (d / ".git").exists():
                continue
        except OSError:
            continue
        found.append({
            "path": str(d),
            "name": d.name,
            "deploy": (d / "deploy.py").exists(),
            "dockerfile": (d / "Dockerfile").exists(),
            "tracked": os.path.normcase(os.path.normpath(str(d))) in tracked_norm,
        })
    return found


# ------------------------------------------------------------ Hintergrund-Jobs

jobs = {}
jobs_lock = threading.Lock()


def start_job(title, cmd, cwd):
    return start_func_job(title, lambda log: stream_cmd(cmd, cwd, log))


def start_func_job(title, fn):
    """Startet fn(log) im Hintergrund; log(text) hängt Zeilen an die Job-Ausgabe."""
    job_id = secrets.token_hex(6)
    job = {"id": job_id, "title": title, "output": "", "done": False, "code": None, "started": time.time()}
    with jobs_lock:
        jobs[job_id] = job

    LOG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.\- ]+", "_", title)[:60].strip()
    log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d_%H-%M-%S')} {safe}.log"
    job["log_file"] = str(log_file)

    def log(text=""):
        line = str(text) + "\n"
        job["output"] += line
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def worker():
        refresh_path()
        try:
            result = fn(log)
            job["code"] = 0 if result is None else int(result)
        except Exception as e:  # noqa: BLE001
            log(f"❌ Fehler: {e}")
            job["code"] = 1
        log(f"--- Ende (Exit-Code {job['code']}) ---")
        job["done"] = True
        # alte Protokolle aufräumen (nur die letzten 50 behalten)
        try:
            old = sorted(LOG_DIR.glob("*.log"))[:-50]
            for f in old:
                f.unlink()
        except OSError:
            pass

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def stream_cmd(cmd, cwd, log, prefix=""):
    """Führt einen Befehl aus und schreibt die Ausgabe zeilenweise ins Job-Log.

    Gewartet wird auf das Prozessende, nicht auf das Ende der Ausgabe: Startet das
    Skript selbst Hintergrundprozesse (z.B. einen Tunnel), die die Pipe erben,
    würde man sonst ewig warten.
    """
    try:
        p = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
    except FileNotFoundError:
        log(f"{prefix}Programm nicht gefunden: {cmd[0]}")
        return 127

    def reader():
        for raw in iter(p.stdout.readline, b""):
            log(prefix + raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    code = p.wait()
    t.join(timeout=2)
    return code


def project_container(entry):
    return entry.get("container") or parse_deploy(entry["path"]).get("container")


def stop_project(folder, container):
    """Stoppt Container und Tunnel eines Projekts. Gibt Ausgabe zurück."""
    out = []
    if container:
        out.append(run(["docker", "stop", container], timeout=90)[1].strip())
    pid_file = Path(folder) / "cloudflared.pid"
    if pid_file.exists():
        pid = pid_file.read_text(errors="replace").strip()
        if pid.isdigit():
            if IS_WINDOWS:
                out.append(run(["taskkill", "/F", "/T", "/PID", pid])[1].strip())
            else:
                try:
                    os.kill(int(pid), 15)
                except OSError:
                    pass
        pid_file.unlink(missing_ok=True)
    return "\n".join(o for o in out if o)


# ------------------------------------------------------------ Panel selbst (Version / Update)

def panel_version():
    """Stand des Panel-Codes (Git-Commit) – None, wenn der Ordner kein Git-Repo ist."""
    if not (BASE_DIR / ".git").exists():
        return None
    code, out = run(["git", "-C", str(BASE_DIR), "log", "-1", "--format=%h%x1f%s%x1f%cI"])
    if code != 0 or "\x1f" not in out:
        return None
    h, subject, date = out.strip().split("\x1f")[:3]
    return {"hash": h, "subject": subject, "date": date}


RESTART_HOOK = None  # wird von panel.pyw gesetzt: startet den Server-Prozess neu


def schedule_panel_restart(delay=3):
    """Startet den Panel-Server nach kurzer Pause neu (der neue Prozess übernimmt den Port)."""
    if RESTART_HOOK is None:
        return False
    threading.Timer(delay, RESTART_HOOK).start()
    return True


def panel_update(log):
    if not (BASE_DIR / ".git").exists():
        log("Dieser Panel-Ordner ist noch kein Git-Repo – siehe ANLEITUNG.md, Abschnitt „Panel-Updates“.")
        return 1
    before = (panel_version() or {}).get("hash")
    log(f"Aktueller Stand: {before}")
    if stream_cmd(["git", "-C", str(BASE_DIR), "pull", "--ff-only"], str(BASE_DIR), log, prefix="  ") != 0:
        log("❌ git pull fehlgeschlagen (lokale Änderungen im Panel-Ordner?). Nichts verändert.")
        return 1
    after = (panel_version() or {}).get("hash")
    if after == before:
        log("✅ Panel ist schon auf dem neuesten Stand.")
        return 0
    log(f"Neuer Stand: {after} – Panel startet in 3 Sekunden neu …")
    if not schedule_panel_restart():
        log("⚠️ Neustart nicht möglich – bitte restart_panel.ps1 von Hand ausführen.")
    return 0


# ------------------------------------------------------------ Aktionen

def open_path(target):
    if IS_WINDOWS:
        os.startfile(target)  # noqa: S606
    else:
        webbrowser.open(target)


def action(name, data):
    cfg = load_config()
    path = data.get("path")
    entry = find_project(cfg, path) if path else None
    if path and not entry and name not in ("add_project",):
        raise ValueError("Unbekanntes Projekt")

    if name == "set_autostart":
        entry["autostart"] = bool(data.get("value"))
        save_config(cfg)
        return {"ok": True}

    if name == "update_project":
        for key in ("name", "url", "container", "note"):
            if key in data:
                val = str(data[key]).strip()
                if val:
                    entry[key] = val
                else:
                    entry.pop(key, None)
        save_config(cfg)
        return {"ok": True}

    if name == "add_project":
        p = Path(path)
        if not p.is_dir():
            raise ValueError("Ordner existiert nicht")
        if not find_project(cfg, str(p)):
            cfg["projects"].append({"path": str(p), "autostart": False})
            save_config(cfg)
        return {"ok": True}

    if name == "remove_project":
        cfg["projects"] = [p for p in cfg["projects"] if p is not entry]
        save_config(cfg)
        return {"ok": True}

    if name == "move_project_order":
        idx = cfg["projects"].index(entry)
        new = max(0, min(len(cfg["projects"]) - 1, idx + int(data.get("delta", 0))))
        cfg["projects"].insert(new, cfg["projects"].pop(idx))
        save_config(cfg)
        return {"ok": True}

    if name == "set_scan_root":
        cfg["scan_root"] = str(data.get("value") or default_scan_root())
        save_config(cfg)
        return {"ok": True}

    folder = entry["path"] if entry else None
    st_container = (entry or {}).get("container") or parse_deploy(folder).get("container") if folder else None

    if name == "deploy":
        if entry.pop("hosted_on", None):
            save_config(cfg)
        return {"job": start_job(f"Deploy: {Path(folder).name}", [python_exe(), "deploy.py"], folder)}

    if name == "stop":
        return {"ok": True, "output": stop_project(folder, st_container)}

    if name == "restart_container":
        if not st_container:
            raise ValueError("Kein Container bekannt")
        code, out = run(["docker", "restart", st_container], timeout=90)
        return {"ok": code == 0, "output": out}

    if name == "container_logs":
        if not st_container:
            raise ValueError("Kein Container bekannt")
        _, out = run(["docker", "logs", "--tail", "300", st_container], timeout=20)
        return {"output": out}

    if name == "tunnel_log":
        f = Path(folder) / "cloudflared.log"
        return {"output": f.read_text(encoding="utf-8", errors="replace")[-20000:] if f.exists() else "(kein Tunnel-Log)"}

    if name == "git_fetch":
        return {"job": start_job(f"git fetch: {Path(folder).name}", ["git", "fetch", "--prune"], folder)}

    if name == "git_pull":
        return {"job": start_job(f"git pull: {Path(folder).name}", ["git", "pull", "--ff-only"], folder)}

    if name == "git_status":
        _, out = run(["git", "status"], cwd=folder)
        _, log = run(["git", "log", "-10", "--format=%h  %cr  %s"], cwd=folder)
        return {"output": out + "\n\nLetzte Commits:\n" + log}

    if name == "open_folder":
        open_path(folder)
        return {"ok": True}

    if name == "open_vscode":
        code_cmd = shutil.which("code") or shutil.which("code.cmd")
        if not code_cmd:
            raise ValueError("VS Code ('code') nicht im PATH gefunden")
        subprocess.Popen([code_cmd, folder], creationflags=NO_WINDOW)
        return {"ok": True}

    if name == "open_terminal":
        if IS_WINDOWS:
            if shutil.which("wt"):
                subprocess.Popen(["wt", "-d", folder])
            else:
                subprocess.Popen(["cmd", "/c", "start", "powershell", "-NoExit"], cwd=folder)
        return {"ok": True}

    if name == "open_url":
        url = data.get("url", "")
        if not re.match(r"^https?://", url):
            raise ValueError("Ungültige Adresse")
        webbrowser.open(url)
        return {"ok": True}

    # ---- globale Aktionen
    if name == "autostart_enable":
        code, out = powershell(f"Enable-ScheduledTask -TaskName '{AUTOSTART_TASK}' | Out-Null")
        return {"ok": code == 0, "output": out}

    if name == "autostart_disable":
        code, out = powershell(f"Disable-ScheduledTask -TaskName '{AUTOSTART_TASK}' | Out-Null")
        return {"ok": code == 0, "output": out}

    if name == "autostart_register":
        script = BASE_DIR / "register_autostart.ps1"
        return {"job": start_job("Autostart einrichten",
                                 ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                 str(BASE_DIR))}

    if name == "autostart_run_now":
        return {"job": start_job("Autostart jetzt ausführen",
                                 ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                  str(BASE_DIR / "start_all.ps1")], str(BASE_DIR))}

    if name == "autostart_log":
        f = BASE_DIR / "start_all.log"
        return {"output": f.read_text(encoding="utf-8-sig", errors="replace")[-20000:] if f.exists() else "(noch kein Log)"}

    if name == "job_logs":
        files = sorted(LOG_DIR.glob("*.log"), reverse=True)[:15] if LOG_DIR.exists() else []
        if data.get("file"):
            f = LOG_DIR / Path(data["file"]).name
            return {"output": f.read_text(encoding="utf-8", errors="replace")[-30000:] if f.exists() else "(nicht gefunden)"}
        return {"files": [f.name for f in files]}

    if name == "panel_update":
        return {"job": start_func_job("Panel-Update", panel_update)}

    if name == "start_docker":
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe"
        if not exe.exists():
            raise ValueError("Docker Desktop nicht gefunden")
        subprocess.Popen([str(exe)])
        return {"ok": True}

    raise ValueError(f"Unbekannte Aktion: {name}")


def overview(check_online):
    cfg = load_config()
    containers = docker_containers()
    with ThreadPoolExecutor(max_workers=8) as ex:
        projects = list(ex.map(lambda e: project_status(e, containers, check_online), cfg["projects"]))
        task_future = ex.submit(autostart_task)
        task = task_future.result()
    return {
        "docker_ok": containers is not None,
        "projects": projects,
        "autostart_task": task,
        "scan_root": cfg["scan_root"],
        "config_path": str(CONFIG_PATH),
        "is_windows": IS_WINDOWS,
        "panel_version": panel_version(),
    }


