# Projekt-Panel – Fernzugriff & Hosting-Umzug

Jeder Rechner hat sein eigenes Panel, das über eine Subdomain erreichbar ist:

| Rechner | Panel |
|---|---|
| PC | https://panel.arnold0110.com |
| Laptop | https://panel-laptop.arnold0110.com |

Beide Panels zeigen **beide Rechner** (Tabs oben). Mit **⇄ nach Laptop / ⇄ nach PC**
ziehst du ein Projekt komplett um. Dabei passiert Folgendes:

1. Das Projekt wird hier gestoppt, also Container und Tunnel. Ab diesem Moment läuft es nirgends doppelt.
2. Das Panel packt den Projektordner (inkl. `config.py`, Bilder, `.git`), die Docker-Volumes (z. B. die Song-Wünsche) und den Tunnel-Zugang.
3. Das Paket wird über Cloudflare zum anderen Panel übertragen. Dort wird es entpackt und mit `deploy.py` gestartet.
4. Danach steht der Autostart hier auf aus und dort auf an.
5. Schlägt etwas fehl, stoppt das Panel das Projekt auf dem Ziel wieder und startet es hier neu.

Einen alten Projektordner auf dem Ziel sichert das Panel vorher unter `backups/`.

**Wichtig:** Ein Projekt darf nie auf beiden Rechnern gleichzeitig laufen, sonst verteilt
Cloudflare die Besucher zufällig auf beide. Das Panel zeigt in so einem Fall eine rote Warnung.

---

## Schritt 1 – Cloudflare Zero Trust (einmalig, im Browser)

Gehe auf <https://one.dash.cloudflare.com> und wähle deinen Account.

**Team-Domain notieren:** Unter *Settings* (Einstellungen) steht die Team-Domain,
z. B. `arnold0110.cloudflareaccess.com`. Beim ersten Öffnen fragt Cloudflare danach.
Der kostenlose Plan reicht.

**Service-Token anlegen** (damit sich die beiden Panels gegenseitig erreichen):
*Access controls → Service credentials → Service Tokens → Create Service Token*
- Name: `panel-sync`, Laufzeit: *Non-expiring* (oder 1 Jahr)
- **Client ID und Client Secret sofort kopieren.** Das Secret zeigt Cloudflare nur einmal an.

**Anwendung anlegen:**
*Access controls → Applications → Add an application → Self-hosted*
- Name: `Projekt-Panel`
- Domains: `panel.arnold0110.com` **und** `panel-laptop.arnold0110.com` (beide in derselben Anwendung)
- Policy 1, „Nur Arne“: Action **Allow**, Include → **Emails** → `arne.com99@googlemail.com`
- Policy 2, „Panel-Sync“: Action **Service Auth**, Include → **Service Token** → `panel-sync`
- Login-Methode: **One-time PIN**, also ein Code per E-Mail. Optional geht auch Google-Login.
- Session-Dauer z. B. 24 Stunden

Nach dem Speichern öffnest du die Anwendung und kopierst den
**Application Audience (AUD) Tag**. Das Panel braucht ihn, um die Logins selbst nachzuprüfen.

## Schritt 2 – PC einrichten

```powershell
cd C:\Users\arnew\docker-autostart
powershell -ExecutionPolicy Bypass -File .\setup_machine.ps1
```
Name: `PC`, Adresse: `panel.arnold0110.com`, anderer Rechner: `Laptop`.
Das Skript erledigt Folgendes:
- Es legt den Tunnel `panel-pc` an und verknüpft ihn mit der Subdomain.
- Es fragt die Cloudflare-Werte ab und speichert sie in `machine.json`.
- Es richtet die Aufgabe „Projekt-Panel Server“ ein. Damit läuft das Panel beim Login im Hintergrund.

## Schritt 3 – Laptop einrichten

Auf dem Laptop müssen **Python, Docker Desktop, Git und cloudflared** installiert sein.
cloudflared installierst du mit `winget install --id Cloudflare.cloudflared`.

1. Den Ordner `docker-autostart` auf den Laptop bringen, entweder als privates GitHub-Repo
   (`.gitignore` ist vorbereitet, `machine.json` und `projects.json` bleiben lokal) oder per USB-Stick
   **ohne** `machine.json` und `projects.json`.
2. Im Ordner ausführen:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup_machine.ps1
   ```
   Name: `Laptop`, Adresse: `panel-laptop.arnold0110.com`, anderer Rechner: `PC`.
   Beim ersten Mal öffnet sich der Browser für `cloudflared tunnel login`. Dort wählst du `arnold0110.com`.

Die Projekte selbst musst du nicht vorher auf den Laptop kopieren. Das erledigt der Umzug.

## Benutzung

- **Zu Hause:** Desktop-Verknüpfung „Projekt-Panel“ oder die Panel-Adresse.
- **Unterwegs:** `https://panel.arnold0110.com` (PC) bzw. `https://panel-laptop.arnold0110.com`. Du loggst dich mit dem Code per E-Mail ein.
- **Hosting auf den Laptop holen:** im Tab *PC* beim Projekt **⇄ nach Laptop** klicken (zweimal zur Bestätigung).
  Dafür müssen beide Rechner an sein und das Panel muss laufen.
- **Zurück:** im Tab *Laptop* **⇄ nach PC**.

Ist der PC aus, kommst du an seine Daten nicht heran. Willst du das Hosting unterwegs übernehmen,
zieh das Projekt also um, **bevor** du den PC ausschaltest.

---

## Panel-Updates

Der Panel-Code liegt im privaten Repo **github.com/arnold0110/projekt-panel**.
Änderst du etwas am Panel, pushst du es dorthin und klickst im Panel oben auf **„⬆ Panel“ →
„Alle Rechner aktualisieren“**. Jeder Rechner holt dann den neuen Stand (`git pull`) und startet
sein Panel neu. `machine.json`, `projects.json` und Logs sind nicht im Repo und bleiben unverändert.
