"""Fernzugriff: Rechner-Konfiguration, Cloudflare-Access-Prüfung, Verbindung zu anderen Rechnern.

Sicherheitsmodell
-----------------
* Lokal (Desktop-Fenster): Zugriff nur über 127.0.0.1 mit Zufalls-Token.
* Über die Panel-Subdomain: Cloudflare Access lässt nur deine E-Mail (Einmal-Code)
  und das Service-Token der anderen Panels durch. Zusätzlich prüft das Panel das
  von Cloudflare signierte JWT (RS256) selbst – Signatur, Audience, Issuer, Ablauf
  und Identität. Ohne gültiges JWT gibt es nur 403.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
BASE_DIR = PANEL_DIR.parent
MACHINE_PATH = BASE_DIR / "machine.json"


def load_machine():
    """machine.json – fehlt sie, läuft das Panel nur lokal (wie bisher)."""
    if not MACHINE_PATH.exists():
        return None
    m = json.loads(MACHINE_PATH.read_text(encoding="utf-8-sig"))
    m.setdefault("name", "Dieser PC")
    m.setdefault("port", 8765)
    m.setdefault("peers", [])
    m.setdefault("access", {})
    m.setdefault("service_token", {})
    return m


# ------------------------------------------------------------ JWT (RS256) ohne Zusatzpakete

_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
_jwks = {"keys": {}, "fetched": 0.0, "team": None}
_jwks_lock = threading.Lock()


def _b64url(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _fetch_jwks(team_domain):
    url = f"https://{team_domain}/cdn-cgi/access/certs"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Projekt-Panel"}), timeout=10) as r:
        data = json.loads(r.read())
    keys = {}
    for k in data.get("keys", []):
        if k.get("kty") == "RSA" and "n" in k and "e" in k:
            keys[k["kid"]] = (int.from_bytes(_b64url(k["n"]), "big"), int.from_bytes(_b64url(k["e"]), "big"))
    return keys


def _get_key(team_domain, kid):
    with _jwks_lock:
        fresh = time.time() - _jwks["fetched"] < 3600 and _jwks["team"] == team_domain
        if not fresh or kid not in _jwks["keys"]:
            # bei unbekannter kid neu laden (Schlüsselrotation), aber höchstens alle 30 s
            if not fresh or time.time() - _jwks["fetched"] > 30:
                _jwks["keys"] = _fetch_jwks(team_domain)
                _jwks["fetched"] = time.time()
                _jwks["team"] = team_domain
        return _jwks["keys"].get(kid)


def rsa_pkcs1_sha256_verify(n, e, message, signature):
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return secrets.compare_digest(em, expected)


def verify_jwt(token, team_domain, audience, key_lookup=None, now=None):
    """Prüft ein Cloudflare-Access-JWT. Gibt die Claims zurück oder wirft ValueError."""
    if not token or token.count(".") != 2:
        raise ValueError("kein Token")
    h64, p64, s64 = token.split(".")
    header = json.loads(_b64url(h64))
    if header.get("alg") != "RS256":
        raise ValueError("falscher Algorithmus")
    key = (key_lookup or (lambda kid: _get_key(team_domain, kid)))(header.get("kid"))
    if not key:
        raise ValueError("unbekannter Schlüssel")
    if not rsa_pkcs1_sha256_verify(key[0], key[1], f"{h64}.{p64}".encode(), _b64url(s64)):
        raise ValueError("Signatur ungültig")
    claims = json.loads(_b64url(p64))
    now = now or time.time()
    if claims.get("exp", 0) < now - 30:
        raise ValueError("abgelaufen")
    if claims.get("nbf", 0) > now + 30:
        raise ValueError("noch nicht gültig")
    if audience:  # optional: ohne AUD gilt jedes Access-Token deines Teams (Identität wird trotzdem geprüft)
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if audience not in auds:
            raise ValueError("falsche Audience")
    if claims.get("iss") != f"https://{team_domain}":
        raise ValueError("falscher Issuer")
    return claims


ACCESS_LOG = PANEL_DIR / "access.log"


def log_access(text):
    try:
        if ACCESS_LOG.exists() and ACCESS_LOG.stat().st_size > 512 * 1024:
            ACCESS_LOG.replace(ACCESS_LOG.with_suffix(".old.log"))
        with open(ACCESS_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def identify(headers, machine, path=""):
    """Gibt 'user:<mail>' / 'peer' zurück oder None, wenn nicht erlaubt (Grund landet in access.log)."""
    acc = machine.get("access", {})
    ip = headers.get("Cf-Connecting-Ip", "?")
    if not acc.get("team_domain"):
        log_access(f"DENY {ip} {path}: keine team_domain in machine.json")
        return None
    token = headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        log_access(f"DENY {ip} {path}: kein Cf-Access-Jwt-Assertion-Header")
        return None
    try:
        claims = verify_jwt(token, acc["team_domain"], acc.get("aud") or None)
    except Exception as e:  # noqa: BLE001
        log_access(f"DENY {ip} {path}: JWT ungültig ({type(e).__name__}: {e})")
        return None
    email = (claims.get("email") or "").lower()
    if email and email in [e.lower() for e in acc.get("allowed_emails", [])]:
        return "user:" + email
    client_id = machine.get("service_token", {}).get("client_id")
    if client_id and claims.get("common_name") == client_id:
        return "peer"
    log_access(f"DENY {ip} {path}: Identität nicht erlaubt (email={email!r}, common_name={claims.get('common_name')!r})")
    return None


# ------------------------------------------------------------ andere Rechner

class PeerError(Exception):
    pass


def find_peer(machine, name):
    for p in (machine or {}).get("peers", []):
        if p.get("name") == name:
            return p
    return None


def peer_request(machine, peer, method, path, body=None, raw=None, timeout=60):
    """Ruft die API eines anderen Panels über Cloudflare auf (mit Service-Token)."""
    url = peer["url"].rstrip("/") + path
    headers = {"X-Panel": "1", "User-Agent": "Projekt-Panel"}
    st = machine.get("service_token", {})
    if st.get("client_id"):
        headers["CF-Access-Client-Id"] = st["client_id"]
        headers["CF-Access-Client-Secret"] = st.get("client_secret", "")
    if peer.get("test_identity"):  # nur für automatisierte Tests ohne Cloudflare
        headers["X-Test-Identity"] = peer["test_identity"]
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            payload = r.read()
            if "application/json" not in ctype:
                raise PeerError(f"{peer['name']}: unerwartete Antwort (Cloudflare Access/Service-Token prüfen)")
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise PeerError(f"{peer['name']}: HTTP {e.code} {msg}".strip()) from None
    except urllib.error.URLError as e:
        raise PeerError(f"{peer['name']} nicht erreichbar ({e.reason})") from None
    except TimeoutError:
        raise PeerError(f"{peer['name']}: Zeitüberschreitung") from None
