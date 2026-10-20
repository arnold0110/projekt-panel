"""Baut und startet dieses Projekt als Docker-Container, optional öffentlich per Cloudflare Tunnel.

Wird vom Projekt-Panel (Starten / Neu deployen / Umzug) und vom Autostart aufgerufen.
Die Werte unten liest das Panel direkt aus dieser Datei – Namen bitte so lassen.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys

# ------------------------------------------------------------------ Einstellungen
# "local"  -> nur http://localhost:<HOST_PORT>
# "online" -> zusätzlich öffentlich unter https://<PUBLIC_HOSTNAME>
HOSTING_MODE = "online"

TUNNEL_NAME = "mein-projekt"
PUBLIC_HOSTNAME = "mein-projekt.arnold0110.com"

HOST_PORT = 5099                 # Port auf dem Rechner (pro Projekt eindeutig!)
CONTAINER_PORT = 5000            # Port in der App (siehe Dockerfile)
CONTAINER_NAME = "mein-projekt"
IMAGE_NAME = "mein-projekt"
VOLUME_NAME = "mein-projekt_data"  # Daten unter /data – überleben Redeploys und werden beim Umzug mitgenommen
# ----------------------------------------------------------------------------------

IS_WINDOWS = os.name == "nt"
TUNNEL_LOG = Path("cloudflared.log")
TUNNEL_PID = Path("cloudflared.pid")


def run(description, cmd, ignore_errors=False):
    print(f"⏳ {description}...")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 and not ignore_errors:
        print(f"❌ Fehler bei '{description}':\n{result.stderr or result.stdout}")
        sys.exit(1)
    print("✅ Erledigt!\n")


def stop_previous_tunnel():
    if not TUNNEL_PID.exists():
        return
    pid = TUNNEL_PID.read_text().strip()
    if pid.isdigit():
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
        else:
            subprocess.run(["kill", pid], capture_output=True)
    TUNNEL_PID.unlink(missing_ok=True)


def start_tunnel():
    exe = shutil.which("cloudflared")
    if exe is None:
        print("❌ 'cloudflared' wurde nicht gefunden. Installieren: winget install --id Cloudflare.cloudflared")
        sys.exit(1)
    listing = subprocess.run([exe, "tunnel", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if listing.returncode != 0 or TUNNEL_NAME not in listing.stdout:
        print(f"❌ Der Tunnel '{TUNNEL_NAME}' ist auf diesem Rechner nicht eingerichtet. Einmalig ausführen:")
        print(f"     cloudflared tunnel create {TUNNEL_NAME}")
        print(f"     cloudflared tunnel route dns {TUNNEL_NAME} {PUBLIC_HOSTNAME}")
        sys.exit(1)
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if IS_WINDOWS else 0
    with TUNNEL_LOG.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{HOST_PORT}", "run", TUNNEL_NAME],
                                stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags, start_new_session=not IS_WINDOWS)
    TUNNEL_PID.write_text(str(proc.pid), encoding="utf-8")
    print(f"🌐 Cloudflare Tunnel gestartet -> https://{PUBLIC_HOSTNAME}")


def main():
    os.chdir(Path(__file__).resolve().parent)
    if HOSTING_MODE not in ("local", "online"):
        print(f"❌ Ungültiger HOSTING_MODE '{HOSTING_MODE}'")
        sys.exit(1)
    if not Path("config.py").exists():
        print("❌ config.py fehlt. Kopiere config.example.py zu config.py und trage die Werte ein.")
        sys.exit(1)

    print(f"🚀 Deployment von {CONTAINER_NAME} (Modus: {HOSTING_MODE})\n")
    stop_previous_tunnel()

    docker_run = ["docker", "run", "-d", "--restart", "unless-stopped",
                  "-p", f"{HOST_PORT}:{CONTAINER_PORT}", "-v", f"{VOLUME_NAME}:/data",
                  "--name", CONTAINER_NAME]
    if HOSTING_MODE == "local":
        docker_run += ["-e", "INSECURE_COOKIES=1"]
    docker_run.append(IMAGE_NAME)

    run("Entferne alten Container", ["docker", "rm", "-f", CONTAINER_NAME], ignore_errors=True)
    run("Baue Docker-Image", ["docker", "build", "-t", IMAGE_NAME, "."])
    run("Starte Container", docker_run)

    print(f"🎉 Läuft lokal auf http://localhost:{HOST_PORT}")
    if HOSTING_MODE == "online":
        start_tunnel()


if __name__ == "__main__":
    main()
