# Projektvorlage

Grundgerüst für neue, selbst gehostete Projekte: Flask-App im Docker-Container,
öffentlich über Cloudflare Tunnel, verwaltet über das Projekt-Panel.

## Neues Projekt anlegen

Im Projekt-Panel: **＋ Projekt → Neues Projekt aus Vorlage**. Das Panel
- kopiert diese Vorlage nach `C:\Users\<du>\<name>`,
- trägt Name, Subdomain und einen auf allen Rechnern freien Port in `deploy.py` ein,
- erzeugt `config.py` mit zufälligem `SECRET_KEY`,
- legt ein Git-Repo mit erstem Commit an,
- erstellt Cloudflare-Tunnel + DNS-Eintrag `<subdomain>.arnold0110.com`,
- nimmt das Projekt ins Panel auf.

Danach: **▶ Starten** – fertig.

## Aufbau

| Datei | Zweck |
|---|---|
| `app.py`, `templates/`, `static/` | deine App |
| `deploy.py` | Einstellungen (Name, Port, Subdomain) + Bauen/Starten – vom Panel genutzt |
| `config.py` | Geheimnisse, **nicht** im Repo (`config.example.py` als Muster) |
| `Dockerfile`, `requirements.txt` | Container |

**Daten** (Datenbank, Uploads …) immer unter `/data` speichern (`DATA_DIR` in `app.py`).
Das ist ein Docker-Volume: bleibt bei „Neu deployen“ erhalten und wird beim Umzug
zwischen PC und Laptop mitgenommen.

## Eigenes GitHub-Repo

```powershell
git remote add origin https://github.com/arnold0110/<name>.git
git push -u origin main
```
