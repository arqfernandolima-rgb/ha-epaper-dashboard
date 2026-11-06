"""Small Home Assistant REST client.

Inside the app, Supervisor proxies the Core API at http://supervisor/core and
hands us SUPERVISOR_TOKEN. For testing elsewhere, set HA_URL and HA_TOKEN.
"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request


def _token():
    for name in ("HA_TOKEN", "SUPERVISOR_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    # s6 keeps the container environment here even if it wasn't passed down
    try:
        with open("/run/s6/container_environment/SUPERVISOR_TOKEN") as f:
            return f.read().strip()
    except OSError:
        return ""


class HA:
    def __init__(self, url=None, token=None):
        self.url = (url or os.environ.get("HA_URL") or "http://supervisor/core").rstrip("/")
        self.token = token or _token()

    def _req(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def get(self, path):
        return json.loads(self._req("GET", path))

    def config(self):
        return self.get("/api/config")

    def states(self):
        return {s["entity_id"]: s for s in self.get("/api/states")}

    def template(self, tpl):
        return self._req("POST", "/api/template", {"template": tpl}).decode()

    def integration_entities(self, domain):
        """Entity IDs that belong to one integration (e.g. 'airthings')."""
        try:
            return json.loads(self.template("{{ integration_entities('%s') | to_json }}" % domain))
        except Exception:
            return []

    def calendars(self):
        return self.get("/api/calendars")

    def state(self, entity_id):
        return self.get(f"/api/states/{urllib.parse.quote(entity_id)}")

    def service_response(self, domain, service, data):
        """Call a service that returns data (e.g. weather.get_forecasts)."""
        out = json.loads(self._req("POST", f"/api/services/{domain}/{service}?return_response", data))
        return out.get("service_response", out)

    def history(self, entity_ids, start):
        """Every change of these entities since `start`, attribute-only changes included."""
        when = urllib.parse.quote(start.astimezone(dt.timezone.utc).isoformat())
        q = urllib.parse.urlencode({"filter_entity_id": ",".join(entity_ids), "significant_changes_only": "0"})
        return self.get(f"/api/history/period/{when}?{q}")

    def calendar_events(self, entity_id, start, end):
        q = urllib.parse.urlencode({"start": start.isoformat(), "end": end.isoformat()})
        return self.get(f"/api/calendars/{urllib.parse.quote(entity_id)}?{q}")
