"""Startpunkt der App – hier baust du dein Projekt.

Die Vorlage bringt mit: Konfiguration aus config.py, ein Daten-Verzeichnis (/data, bleibt bei
Redeploy und Umzug erhalten), Sicherheits-Header und einen Health-Check unter /healthz.
"""
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template

try:
    import config
except ImportError:
    raise SystemExit("config.py fehlt – kopiere config.example.py zu config.py.")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data" if Path("/data").exists() else Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("INSECURE_COOKIES") != "1"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.route("/")
def index():
    return render_template("index.html", title=config.TITLE)


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
