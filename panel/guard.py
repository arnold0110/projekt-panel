"""Prüft vor dem Autostart, ob ein Projekt schon auf einem anderen Rechner läuft.

Aufruf (aus start_all.ps1):  python guard.py <projektordner>
Exit-Code 0 = starten, 3 = läuft schon woanders (überspringen).
Ist das Internet beim Hochfahren noch nicht da, wird bis zu 2 Minuten gewartet.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote  # noqa: E402


def main():
    if len(sys.argv) < 2:
        return 0
    machine = remote.load_machine()
    if not machine or not machine.get("peers"):
        return 0
    path = sys.argv[1]
    deadline = time.time() + 120
    while True:
        network_down = False
        for peer in machine["peers"]:
            try:
                remote.peer_request(machine, peer, "GET", "/api/ping", timeout=15)
            except remote.PeerError as e:
                network_down = network_down or e.network
        if not network_down or time.time() > deadline:
            break
        time.sleep(10)
    hits = remote.find_elsewhere(machine, path, "running")
    if hits:
        print(f"läuft bereits auf {hits[0]['machine']} – wird hier nicht gestartet")
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 – im Zweifel nicht blockieren
        print(f"Prüfung fehlgeschlagen ({e}) – starte trotzdem")
        sys.exit(0)
