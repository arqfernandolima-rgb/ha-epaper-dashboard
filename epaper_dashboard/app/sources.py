"""Data fetchers. Each returns plain data; view.py turns it into what's on screen."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

log = logging.getLogger("sources")

VERSION = "0.11.1"
UA = f"epaper-dashboard/{VERSION} (+https://github.com/arqfernandolima-rgb/ha-epaper-dashboard)"


def http_json(url, headers=None, timeout=15, tries=2):
    """GET JSON. Retries once on a server error or dropped connection, and puts the
    server's own words in the error (so the status page says *why* it refused)."""
    host = urllib.parse.urlsplit(url).hostname
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                said = " ".join(e.read(400).decode("utf-8", "replace").split())[:180]
            except Exception:
                said = ""
            if e.code < 500 or attempt == tries - 1:
                raise RuntimeError(f"HTTP {e.code} from {host}" + (f": {said}" if said else "")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise RuntimeError(f"{host}: {getattr(e, 'reason', e)}") from None
        time.sleep(2)


# ---------------------------------------------------------------- weather
def _qs(params):
    return urllib.parse.urlencode(params, safe=",/")      # commas and slashes as in Open-Meteo's own docs


def weather(ctx, prev=None):
    """Open-Meteo forecast + air quality for HA's home location (free, no key).
    If Open-Meteo won't answer, fall back to Home Assistant's own weather entity (Met.no)."""
    base = {"latitude": round(ctx.lat, 4), "longitude": round(ctx.lon, 4), "timezone": ctx.tz_name}
    p = dict(base,
             current="temperature_2m,weather_code,is_day,wind_speed_10m,wind_gusts_10m",
             hourly="temperature_2m,apparent_temperature,precipitation_probability,weather_code,uv_index,is_day",
             daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
             forecast_days=8)                       # the kitchen display shows the week
    if ctx.imperial:
        p.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    errors, source = [], "Open-Meteo"
    try:
        forecast = http_json("https://api.open-meteo.com/v1/forecast?" + _qs(p))
    except Exception as e:
        try:
            forecast, why = _ha_weather(ctx), "no weather entity in Home Assistant"
        except Exception as e2:
            forecast, why = None, str(e2)
        if forecast is None:
            raise RuntimeError(f"Open-Meteo: {e} (backup: {why})") from None
        errors.append(f"Open-Meteo: {e}; showing Home Assistant's forecast instead")
        source = "Home Assistant"
    try:
        aq = http_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + _qs(dict(base, current="us_aqi")))
    except Exception:
        aq = (prev or {}).get("aq")          # keep the last reading if only AQI failed
    return {"forecast": forecast, "aq": aq, "imperial": ctx.imperial, "source": source, "errors": errors}


# HA weather conditions -> WMO codes (what the Open-Meteo-shaped view expects)
HA_WMO = {"sunny": 0, "clear-night": 0, "partlycloudy": 2, "cloudy": 3, "windy": 3, "windy-variant": 3,
          "exceptional": 3, "fog": 45, "rainy": 61, "pouring": 65, "snowy": 71, "snowy-rainy": 67,
          "hail": 96, "lightning": 95, "lightning-rainy": 95}


def _ha_weather(ctx):
    """Home Assistant's weather entity (usually Met.no's weather.forecast_home), reshaped like Open-Meteo."""
    eid = ctx.opts.get("weather_entity") or (ctx.discovered or {}).get("weather")
    if not eid:
        return None
    st = ctx.ha.state(eid)
    a = st.get("attributes", {})
    fc = {}
    for kind in ("hourly", "daily"):
        try:
            fc[kind] = ctx.ha.service_response("weather", "get_forecasts",
                                               {"entity_id": eid, "type": kind})[eid]["forecast"]
        except Exception:
            fc[kind] = []
    try:
        day = ctx.ha.state("sun.sun").get("state") == "above_horizon"
    except Exception:
        day = 7 <= ctx.clock().hour < 18
    now = ctx.clock()

    def local(t):
        return dt.datetime.fromisoformat(t).astimezone(ctx.tz)

    hours = [(local(f["datetime"]), f) for f in fc["hourly"]]
    hourly = {"time": [t.strftime("%Y-%m-%dT%H:%M") for t, _ in hours],
              "temperature_2m": [f.get("temperature") for _, f in hours],
              "apparent_temperature": [f.get("apparent_temperature", f.get("temperature")) for _, f in hours],
              "precipitation_probability": [f.get("precipitation_probability") or 0 for _, f in hours],
              "weather_code": [HA_WMO.get(f.get("condition"), 3) for _, f in hours],
              "uv_index": [f.get("uv_index") or 0 for _, f in hours],
              "is_day": [0 if f.get("condition") == "clear-night" else 1 for _, f in hours]}
    by_day = {local(f["datetime"]).date(): f for f in fc["daily"]}
    daily = {"time": [], "weather_code": [], "temperature_2m_max": [], "temperature_2m_min": [],
             "precipitation_probability_max": [], "uv_index_max": []}
    for k in range(8):
        d = now.date() + dt.timedelta(days=k)
        f = by_day.get(d)
        temps = [x.get("temperature") for t, x in hours if t.date() == d and x.get("temperature") is not None]
        if k == 0 and a.get("temperature") is not None:
            temps.append(a["temperature"])
        hi = f.get("temperature") if f else (max(temps) if temps else None)
        lo = f.get("templow") if f and f.get("templow") is not None else (min(temps) if temps else None)
        if hi is None or lo is None:
            continue
        daily["time"].append(d.isoformat())
        daily["weather_code"].append(HA_WMO.get((f or {}).get("condition") or st.get("state"), 3))
        daily["temperature_2m_max"].append(max(hi, lo))
        daily["temperature_2m_min"].append(min(hi, lo))
        daily["precipitation_probability_max"].append((f or {}).get("precipitation_probability") or 0)
        daily["uv_index_max"].append((f or {}).get("uv_index"))
    if len(daily["time"]) < 2 or a.get("temperature") is None:
        raise RuntimeError(f"{eid} has no usable forecast")
    current = {"temperature_2m": a["temperature"], "weather_code": HA_WMO.get(st.get("state"), 3),
               "is_day": 1 if day else 0, "wind_speed_10m": a.get("wind_speed") or 0,
               "wind_gusts_10m": a.get("wind_gust_speed")}
    return {"current": current, "hourly": hourly, "daily": daily}


# ---------------------------------------------------------------- markets
# Finnhub's free API (official, needs a free key): live quotes and the market's open/closed status.
# The free plan has no intraday history, so the day chart is built from our own samples,
# bucketed into 5-minute steps and kept in /data so a restart doesn't lose the morning.
ET = ZoneInfo("America/New_York")
BUCKET = 300
SAMPLES = os.path.join(os.environ.get("DASH_DATA", "/data"), "market_samples.json")


def _finnhub(path, key):
    sep = "&" if "?" in path else "?"
    return http_json(f"https://finnhub.io/api/v1{path}{sep}token={urllib.parse.quote(key)}")


def _session(day):
    """The regular US session (9:30-16:00 Eastern) on a date, as epoch seconds."""
    return (int(dt.datetime.combine(day, dt.time(9, 30), ET).timestamp()),
            int(dt.datetime.combine(day, dt.time(16, 0), ET).timestamp()))


def _session_for(now, status):
    """Today's session while it's on or still ahead; otherwise the next weekday's."""
    today = now.astimezone(ET).date()
    if today.weekday() < 5 and not (status or {}).get("holiday"):
        o, c = _session(today)
        ts = now.timestamp()
        closed_early = status and o <= ts < c and status.get("isOpen") is False   # half days
        if ts < c and not closed_early:
            return o, c
    d = today + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return _session(d)


def _load_samples():
    try:
        with open(SAMPLES) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_samples(samples):
    try:
        with open(SAMPLES, "w") as f:
            json.dump(samples, f)
    except OSError:
        pass


def _day_chart(pts, o, c, now_ts, open_price):
    """5-minute steps from the open up to now (or the close), each the last price seen in that step."""
    if not pts:
        return []
    upto = min(int((min(now_ts, c) - o) // BUCKET), (c - o) // BUCKET - 1,
               int((pts[-1][0] - o) // BUCKET))             # stop where our samples stop
    vals, cur, j = [None] * (max(0, upto) + 1), None, 0
    if open_price:
        vals[0] = round(float(open_price), 4)      # the opening price anchors 9:30
    for i in range(len(vals)):
        end = o + (i + 1) * BUCKET
        while j < len(pts) and pts[j][0] < end:
            cur = pts[j][1]
            j += 1
        if cur is not None:
            vals[i] = round(cur, 4)
    return vals                                   # None = no sample yet (the line skips it)


EQUITY = {"stock", "etf", "adr", "cef", "preferred_stock"}     # kinds Finnhub can price live


def _record(samples, key, day, o, c, now, value):
    rec = samples.get(key)
    if not rec or rec.get("day") != day:
        rec = samples[key] = {"day": day, "open": o, "close": c, "pts": []}
    rec["pts"].append([int(now.timestamp()), value])


def _chart(samples, key, o, c, now, anchor):
    rec = samples.get(key)
    if not rec:
        return []
    return _day_chart(rec.get("pts", []), rec.get("open", o), rec.get("close", c), now.timestamp(), anchor)


def markets(ctx, prev=None):
    key = (ctx.opts.get("finnhub_api_key") or "").strip()
    if not key:
        raise RuntimeError("No Finnhub API key yet: get a free one at finnhub.io and add it in Configuration")
    now = ctx.clock()
    old = prev or {}
    try:
        status = _finnhub("/stock/market-status?exchange=US", key)
    except Exception:
        status = old.get("status")
    o, c = _session_for(now, status)
    in_session = o <= now.timestamp() < c
    samples = old.get("samples") or _load_samples()
    day = now.astimezone(ET).date().isoformat()

    hold = getattr(ctx, "holdings", None) or {}
    held = sorted({p["symbol"].upper() for p in hold.get("positions", [])
                   if p.get("kind") in EQUITY and p.get("symbol") and not p.get("cash_equivalent")})
    watch = [s["symbol"].upper() for s in ctx.opts.get("stocks", [])]
    raw, errors = {}, []
    for sym in dict.fromkeys(watch + held):              # each symbol once, watch list first
        try:
            q = _finnhub(f"/quote?symbol={urllib.parse.quote(sym)}", key)
            if not q.get("c") or not q.get("pc"):
                raise ValueError("no quote (indexes like ^GSPC aren't on the free plan; try SPY)")
            raw[sym] = q
        except Exception as e:
            if sym in watch:
                errors.append(f"{sym}: {e}"[:160])

    quotes = {}
    for sym in watch:
        q = raw.get(sym)
        if not q:
            if sym in old.get("quotes", {}):
                quotes[sym] = old["quotes"][sym]       # keep the last good quote
            continue
        price, prev_close = float(q["c"]), float(q["pc"])
        if in_session:
            _record(samples, sym, day, o, c, now, price)
        quotes[sym] = {"price": price, "prev": prev_close, "series": _chart(samples, sym, o, c, now, q.get("o")),
                       "open_ts": o, "close_ts": c, "source": "Finnhub"}

    portfolio = None
    if hold.get("net_value") is not None:
        # total = the broker's last net value + how far live-priced holdings moved since that snapshot;
        # day change = shares x (price now - yesterday's close). Funds reprice overnight (no intraday move).
        change = moved = 0.0
        priced = 0
        for p in hold.get("positions", []):
            q = raw.get((p.get("symbol") or "").upper())
            if not q or p.get("kind") not in EQUITY or p.get("cash_equivalent"):
                continue
            units, cur, pc = float(p["units"]), float(q["c"]), float(q["pc"])
            change += units * (cur - pc)
            moved += units * (cur - float(p.get("price") or pc))
            priced += 1
        total = float(hold["net_value"]) + moved
        if in_session:
            _record(samples, "__portfolio__", day, o, c, now, round(total, 2))
        portfolio = {"total": total, "change": change, "prev": total - change, "priced": priced,
                     "series": _chart(samples, "__portfolio__", o, c, now, total - change),
                     "open_ts": o, "close_ts": c}
    elif old.get("portfolio") and hold:
        portfolio = old["portfolio"]

    if in_session:
        _save_samples(samples)
    if errors and len(errors) == len(watch) and not portfolio:
        raise RuntimeError("; ".join(errors))      # all failed: keep the last result, show the error
    return {"quotes": quotes, "errors": errors, "status": status, "samples": samples, "portfolio": portfolio}


def market_open(mk, now_ts):
    """True while the regular session is open."""
    for q in (mk or {}).get("quotes", {}).values():
        if q.get("open_ts") and q.get("close_ts"):
            return q["open_ts"] <= now_ts < q["close_ts"]
    return False


# ---------------------------------------------------------------- portfolio (SnapTrade)
# SnapTrade Personal: free API to your own brokerage accounts. E*TRADE connects with OAuth,
# Vanguard with its login (it has no API). Holdings are synced about once a day on their side,
# so every 2 hours is plenty; live pricing happens in markets().
SNAPTRADE = "https://api.snaptrade.com"


def snap_signature(path, query, consumer_key, body=None):
    """Same as the official SDK: HMAC-SHA256 over {"content","path","query"} as compact sorted JSON."""
    content = json.dumps({"content": body, "path": path, "query": query}, separators=(",", ":"), sort_keys=True)
    return base64.b64encode(hmac.new(consumer_key.encode(), content.encode(), hashlib.sha256).digest()).decode()


def snap_get(path, client_id, consumer_key, params=()):
    query = urllib.parse.urlencode(list(params) + [("clientId", client_id), ("timestamp", str(int(time.time())))])
    return http_json(f"{SNAPTRADE}{path}?{query}", headers={"Signature": snap_signature(path, query, consumer_key)})


def portfolio(ctx, prev=None):
    cid = (ctx.opts.get("snaptrade_client_id") or "").strip()
    key = (ctx.opts.get("snaptrade_consumer_key") or "").strip()
    if not cid or not key:
        return {"configured": False}
    accounts, positions, net = [], [], 0.0
    for a in snap_get("/accounts/all", cid, key).get("results", []):
        if a.get("kind") != "investment" or a.get("is_paper") or a.get("status") not in (None, "open"):
            continue
        aid = a["id"]
        bal = snap_get(f"/accounts/{urllib.parse.quote(aid)}/balances/v2", cid, key)
        value = float((bal.get("net_value") or {}).get("amount") or (a.get("net_value") or {}).get("amount") or 0)
        net += value
        for p in snap_get(f"/accounts/{urllib.parse.quote(aid)}/positions/all", cid, key).get("results", []):
            ins = p.get("instrument") or {}
            if ins.get("kind") in (None, "cash"):
                continue                                  # cash is already in the net value
            positions.append({"symbol": ins.get("symbol") or "", "kind": ins.get("kind"),
                              "units": float(p.get("units") or 0), "price": float(p.get("price") or 0),
                              "cash_equivalent": bool(p.get("cash_equivalent"))})
        accounts.append({"name": a.get("display_name") or a.get("raw_type") or "Account",
                         "mask": a.get("masked_account_number") or "", "value": value})
    if not accounts:
        raise RuntimeError("SnapTrade has no open investment accounts yet: connect them in its dashboard")
    return {"configured": True, "accounts": accounts, "positions": positions, "net_value": net}


# ---------------------------------------------------------------- calendar
def _parse_when(v, tz):
    if "dateTime" in v:
        return dt.datetime.fromisoformat(v["dateTime"]).astimezone(tz), False
    d = dt.date.fromisoformat(v["date"])
    return dt.datetime.combine(d, dt.time(0), tz), True


def calendar(ctx, prev=None):
    """Events from HA calendar entities (HA handles recurring events for us)."""
    cals = ctx.discovered.get("calendars") or []
    if not cals:
        return {"calendars": [], "events": []}
    now = ctx.clock()
    start, end = now - dt.timedelta(hours=12), now + dt.timedelta(days=8)    # the kitchen shows the week
    events, errors, seen = [], [], set()
    for c in cals:
        try:
            raw = ctx.ha.calendar_events(c, start, end)
        except Exception as e:
            errors.append(f"{c}: {e}")
            continue
        for e in raw:
            title = (e.get("summary") or "(no title)").strip()
            if title.lower().startswith(("canceled:", "cancelled:")):
                continue
            st, all_day = _parse_when(e.get("start", {}), ctx.tz)
            en, _ = _parse_when(e.get("end") or e.get("start", {}), ctx.tz)
            key = (title, st.isoformat())
            if key in seen:            # same event in two calendars
                continue
            seen.add(key)
            events.append({"start": st.isoformat(), "end": en.isoformat(), "all_day": all_day,
                           "title": title, "location": (e.get("location") or "").strip(), "calendar": c,
                           "recurring": bool(e.get("rrule") or e.get("recurrence_id"))})
    if errors and len(errors) == len(cals):
        raise RuntimeError("; ".join(errors))
    events.sort(key=lambda e: e["start"])
    return {"calendars": cals, "events": events, "errors": errors}


def todo_list_name(friendly, eid):
    """A list's name for the display: its friendly name, minus a trailing "list"/account tag in
    brackets (the Microsoft To Do integration can add one)."""
    n = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", friendly or "").strip()
    return n or eid.split(".", 1)[1].replace("_", " ").capitalize()


def shopping(ctx, prev=None):
    """What's left on the kitchen's to-do lists (the house page): any Home Assistant to-do entities,
    comma-separated in the option (HA's shopping list, Microsoft To Do lists ...)."""
    spec = ctx.opts.get("kitchen_shopping_list") or "todo.shopping_list"
    eids = [e for e in re.split(r"[,\s]+", spec) if e.startswith("todo.")] or ["todo.shopping_list"]
    lists, errors = [], []
    for eid in eids:
        try:
            items = ctx.ha.service_response("todo", "get_items", {"entity_id": eid, "status": "needs_action"})[eid]["items"]
            names = [i.get("summary") for i in items if i.get("summary")]
        except Exception as e:
            if eid != "todo.shopping_list":
                errors.append(f"{eid}: {e}")
                continue
            try:                                      # the old shopping list API
                names = [i["name"] for i in ctx.ha.get("/api/shopping_list") if not i.get("complete")]
            except Exception as e2:
                errors.append(f"{eid}: {e2}")
                continue
        try:
            friendly = (ctx.ha.state(eid).get("attributes") or {}).get("friendly_name")
        except Exception:
            friendly = None
        lists.append({"entity": eid, "name": todo_list_name(friendly, eid), "items": names})
    if not lists:
        raise RuntimeError("no to-do list: " + "; ".join(errors))
    if errors:
        log.warning("to-do lists: %s", "; ".join(errors))
    return {"entity": lists[0]["entity"], "items": lists[0]["items"], "lists": lists}


# ---------------------------------------------------------------- HA entities
AIR_CLASSES = {"temperature": "temperature", "humidity": "humidity",
               "volatile_organic_compounds_parts": "voc", "volatile_organic_compounds": "voc",
               "carbon_dioxide": "co2"}
AIR_KINDS = ("temperature", "humidity", "voc", "co2")

# One template call: sensors in the listed areas (directly or via their devices) + every Airthings sensor
# (cloud or Bluetooth integration), each with its device, so readings can be grouped per device and per room.
ROOMS_TPL = """{%- set ns = namespace(out=[]) -%}
{%- for a in AREAS -%}
  {%- set en = namespace(ents=(area_entities(a) | list)) -%}
  {%- for d in area_devices(a) -%}{%- set en.ents = en.ents + (device_entities(d) | list) -%}{%- endfor -%}
  {%- for e in en.ents | unique if e.startswith('sensor.') -%}
    {%- set ns.out = ns.out + [[a, e, state_attr(e, 'device_class') or '', device_id(e) or e,
        device_attr(device_id(e), 'name_by_user') or device_attr(device_id(e), 'name') or '', a]] -%}
  {%- endfor -%}
{%- endfor -%}
{%- for dom in ['airthings', 'airthings_ble'] -%}
{%- for e in integration_entities(dom) if e.startswith('sensor.') -%}
  {%- set ns.out = ns.out + [['', e, state_attr(e, 'device_class') or '', device_id(e) or e,
      device_attr(device_id(e), 'name_by_user') or device_attr(device_id(e), 'name') or '', area_name(e) or '']] -%}
{%- endfor -%}
{%- endfor -%}
{{ ns.out | to_json }}"""


def _indoor_rooms(ctx, states):
    """Rooms for the Indoor air widget: the areas listed in "Indoor rooms", or every Airthings device when
    none are listed. In each room the device with the most readings leads (Airthings wins ties), and
    readings it can't measure (e.g. CO2 on a Wave Mini) come from other sensors in the same area.
    Manual entity options make a room of their own."""
    o, ha = ctx.opts, ctx.ha
    wanted = [a.strip() for a in (o.get("indoor_rooms") or []) if a and a.strip()]
    try:
        rows = json.loads(ha.template(ROOMS_TPL.replace("AREAS", json.dumps(wanted))))
    except Exception:
        rows = []
    devices = {}                                   # device -> {room, name, readings, airthings}
    for asked, eid, dc, dev, dev_name, area in rows:
        kind = AIR_CLASSES.get(dc) or AIR_CLASSES.get(states.get(eid, {}).get("attributes", {}).get("device_class", ""))
        if not kind:
            continue
        d = devices.setdefault((asked, dev), {"room": asked or area or dev_name or "Indoor", "readings": {},
                                              "airthings": not asked})
        d["readings"].setdefault(kind, eid)
    by_room = {}
    for d in devices.values():
        by_room.setdefault(d["room"].lower(), []).append(d)
    rooms = {}
    for key, devs in by_room.items():
        devs.sort(key=lambda d: (len(d["readings"]), d["airthings"]), reverse=True)
        readings = dict(devs[0]["readings"])
        for d in devs[1:]:                         # fill what the main sensor doesn't measure
            for kind, eid in d["readings"].items():
                readings.setdefault(kind, eid)
        rooms[key] = {"room": devs[0]["room"], "readings": readings}
    manual = {k: o.get(f"indoor_{k}") for k in AIR_KINDS if o.get(f"indoor_{k}")}
    if manual:
        rooms["__manual__"] = {"room": "Indoor", "readings": manual}
    if wanted:
        order = [w.lower() for w in wanted]
        picked = [rooms[k] for k in order if k in rooms]
        picked += [r for k, r in rooms.items() if k == "__manual__"]
    else:
        picked = sorted(rooms.values(), key=lambda r: ("office" not in r["room"].lower(), r["room"]))
    return [{"name": r["room"], **r["readings"]} for r in picked[:3]]


def ha_states(ctx, prev=None):
    return ctx.ha.states()


# Every room sensor with its area, for the kitchen display's house page (#house# marks it for tests)
HOUSE_TPL = """{#house#}{%- set ns = namespace(out=[]) -%}
{%- for s in states.sensor -%}
  {%- set dc = s.attributes.device_class or ('radon' if 'radon' in s.entity_id else '') -%}
  {%- if dc in ['temperature', 'humidity', 'carbon_dioxide', 'volatile_organic_compounds',
                'volatile_organic_compounds_parts', 'radon', 'pm25'] -%}
    {%- set a = area_name(s.entity_id) -%}
    {%- if a -%}{%- set ns.out = ns.out + [[s.entity_id, a, dc, s.attributes.unit_of_measurement or '']] -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{{ ns.out | to_json }}"""


def _house_sensors(ctx):
    try:
        return json.loads(ctx.ha.template(HOUSE_TPL))
    except Exception:
        return []


def discover(ctx, states):
    """Find the entities we need without any setup. Options override each guess."""
    o, ha = ctx.opts, ctx.ha

    def first(cands, test):
        return next((e for e in cands if test(e)), None)

    def by_class(cands, cls, hint):
        return (first(cands, lambda e: states.get(e, {}).get("attributes", {}).get("device_class") == cls)
                or first(cands, lambda e: hint in e))

    rooms = _indoor_rooms(ctx, states)
    printers = _printers(ctx, states)
    vehicles = _vehicles(ctx, states)

    # Garage doors (MyQ, ratgdo, Meross ...): covers marked as garage/gate, or with "garage" in the name
    garages = sorted(e for e, st in states.items() if e.startswith("cover.") and (
        st.get("attributes", {}).get("device_class") in ("garage", "gate")
        or "garage" in e or "garage" in str(st.get("attributes", {}).get("friendly_name", "")).lower()))

    phone = [e for e in ha.integration_entities("mobile_app") if e.startswith("sensor.")]
    mail = ha.integration_entities("mail_and_packages")
    packages = {
        "arriving": o.get("packages") or first(mail, lambda e: e.endswith("packages_in_transit")),
        "delivered": None if o.get("packages") else first(mail, lambda e: e.endswith("packages_delivered")),
        # the companion app's "Last notification" sensor: Nest/Google Home package alerts on the phone
        "notifications": _split(o.get("package_notifications"))
                         or [e for e in phone if e.endswith("_last_notification")],
    }
    health = {
        "steps": o.get("steps") or first(phone, lambda e: "daily_steps" in e),
        "sleep": o.get("sleep") or first(phone, lambda e: "sleep_duration" in e),
        "rhr": o.get("resting_heart_rate") or first(phone, lambda e: "resting_heart_rate" in e),
    }

    cals = list(o.get("calendars") or [])
    if not cals:
        try:
            cals = [c["entity_id"] for c in ha.calendars()]
        except Exception:
            cals = []
    weather_eid = o.get("weather_entity") or ("weather.forecast_home" if "weather.forecast_home" in states
                                              else next((e for e in states if e.startswith("weather.")), None))
    return {"rooms": rooms, "printers": printers, "vehicles": vehicles, "packages": packages, "health": health,
            "calendars": cals, "weather": weather_eid, "garages": garages, "house": _house_sensors(ctx)}


def _split(v):
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


PRINTER_SUFFIXES = ("_print_status", "_print_progress", "_remaining_time", "_current_stage")


def _printers(ctx, states):
    """Every Bambu Lab printer, by entity prefix (sensor.h2c_print_status -> sensor.h2c), with its name.
    The "3D printer" option (comma-separated) replaces the search."""
    def prefix(e):
        return next((e[: -len(sf)] for sf in PRINTER_SUFFIXES if e.endswith(sf)), None)

    picked = [prefix(p) or p for p in _split(ctx.opts.get("printer"))]
    if not picked:
        bambu = [e for e in ctx.ha.integration_entities("bambu_lab") if e.startswith("sensor.")]
        main = {prefix(e) for e in bambu if e.endswith(("_print_status", "_print_progress"))}
        picked = sorted(main or {p for p in map(prefix, bambu) if p})
    out = []
    for p in picked:
        name = None
        for sf in ("_print_status", "_print_progress", "_task_name"):
            fn = str(states.get(p + sf, {}).get("attributes", {}).get("friendly_name") or "")
            tail = sf[1:].replace("_", " ")
            if fn.lower().endswith(" " + tail):
                name = fn[: -len(tail)].strip()
                break
        name = name or p.split(".", 1)[-1].replace("_", " ").upper()
        # Bambu names default to "<model>_<serial>" (H2C_31B8AP5B2300863): keep the model
        name = re.sub(r"[ _-]+[0-9A-Z]{12,}$", "", name) or name
        out.append({"id": p, "name": name, "temps": _printer_temp_sensors(ctx, p, states)})
    return out


PRINTER_TEMPS_TPL = """{%%- set ns = namespace(out=[]) -%%}
{%%- for e in device_entities(device_id(%s)) if e.startswith('sensor.') -%%}
  {%%- if state_attr(e, 'device_class') == 'temperature' -%%}
    {%%- set ns.out = ns.out + [[e, state_attr(e, 'friendly_name') or '']] -%%}
  {%%- endif -%%}
{%%- endfor -%%}
{{ ns.out | to_json }}"""


def temp_kind(eid, name=""):
    """Nozzle / Bed / Chamber from a temperature sensor's id or name; None for targets and others."""
    t = f"{eid} {name}".lower()
    if "target" in t:
        return None
    if any(w in t for w in ("nozzle", "hotend", "extruder")):
        return "Nozzle"
    if "bed" in t:
        return "Bed"
    if "chamber" in t:
        return "Chamber"
    return None


def _printer_temp_sensors(ctx, p, states):
    """{kind: [entity ids]} for one printer: every temperature sensor on the printer's own device
    (names vary by model and HA version, so the device is asked rather than names guessed).
    AMS units are separate devices, so their sensors don't get in."""
    anchor = next((p + sf for sf in PRINTER_SUFFIXES if p + sf in states), None)
    found = {}
    if anchor:
        try:
            for eid, name in json.loads(ctx.ha.template(PRINTER_TEMPS_TPL % json.dumps(anchor))):
                kind = temp_kind(eid, name)
                if kind:
                    found.setdefault(kind, []).append(eid)
        except Exception:
            found = {}
    return found


# ---------------------------------------------------------------- vehicles (Rivian, or any EV battery sensor)
VEHICLE_TPL = """{%%- set d = device_id(%s) -%%}
{%%- set ns = namespace(out=[]) -%%}
{%%- for e in (device_entities(d) if d else []) -%%}
  {%%- set ns.out = ns.out + [[e, state_attr(e, 'friendly_name') or '']] -%%}
{%%- endfor -%%}
{{ {"name": (device_attr(d, 'name_by_user') or device_attr(d, 'name') or '') if d else '',
    "model": (device_attr(d, 'model') or '') if d else '', "entities": ns.out} | to_json }}"""

# Rivian (HACS) names, English: what each of the vehicle's sensors is called
VEHICLE_PARTS = (("limit", "sensor.", "battery state of charge limit"),
                 ("range", "sensor.", "estimated vehicle range"),
                 ("remaining", "sensor.", "charging time remaining"),
                 ("charger", "sensor.", "charger state"),
                 ("plug", "binary_sensor.", "charger connection"))
SOC_NAME = "battery state of charge"


def _vehicles(ctx, states):
    """Each car's state-of-charge sensor plus its range / charge limit / charger sensors, found
    on the same HA device (so renamed entity ids still work). Rivian is found on its own; the
    "Vehicle battery" option (comma-separated) adds or replaces it with any EV's sensor."""
    picked = _split(ctx.opts.get("vehicle"))
    if not picked:
        picked = [e for e in ctx.ha.integration_entities("rivian")
                  if e.startswith("sensor.") and e.endswith("_battery_state_of_charge")]
    out = []
    for soc in picked[:2]:
        dev_name, model, siblings = "", "", []
        try:
            got = json.loads(ctx.ha.template(VEHICLE_TPL % json.dumps(soc)))
            dev_name, siblings = str(got.get("name") or "").strip(), got.get("entities") or []
            model = str(got.get("model") or "").strip()
        except Exception:
            pass
        # the name you gave the car (its HA device), else from the sensor's name, else "Car"
        fn = str(states.get(soc, {}).get("attributes", {}).get("friendly_name") or "")
        name = dev_name or (fn[: -len(SOC_NAME)].strip() if fn.lower().endswith(SOC_NAME) else "")
        if not name:
            name = re.sub(r"\s*(battery|state of charge|soc|level|%)\b.*$", "", fn, flags=re.I).strip() or "Car"
        car = {"soc": soc, "name": name, "model": model, "metric": not ctx.imperial}
        stem = soc.split(".", 1)[1][: -len("_battery_state_of_charge")] if soc.endswith("_battery_state_of_charge") else None
        for key, domain, label in VEHICLE_PARTS:
            hit = next((e for e, f in siblings if e.startswith(domain) and f.lower().endswith(" " + label)), None)
            if not hit and stem:                       # by the usual entity id
                guess = domain + stem + "_" + label.replace(" ", "_")
                hit = guess if guess in states else None
            if hit:
                car[key] = hit
        out.append(car)
    # label by model (R1T) unless two cars share it, then by the names you gave them
    models = [c["model"] for c in out]
    for c in out:
        c["label"] = c["model"] if c["model"] and models.count(c["model"]) == 1 else c["name"]
    return out


# ---------------------------------------------------------------- packages from phone notifications
# Nest cameras/doorbells announce packages in the Google Home (or Nest) app. The companion app's
# "Last notification" sensor mirrors each notification into HA; HA's history keeps every one of them.
HOME_APPS = {"com.google.android.apps.chromecast.app", "com.nest.android"}
PICKED_UP = ("picked up", "pick up", "retrieved", "removed", "taken", "collected")
SAME_DROP = dt.timedelta(minutes=10)          # one drop-off often sets off more than one alert


def _package_note(st, tz):
    a = st.get("attributes") or {}
    app_id = str(a.get("package") or "")
    if app_id and app_id not in HOME_APPS:
        return None
    text = " ".join(str(a.get(k) or "") for k in ("android.title", "android.text", "android.bigText"))
    text = " ".join(f"{text} {st.get('state') or ''}".split())
    low = text.lower()
    if "package" not in low:
        return None
    when = None
    try:
        if a.get("post_time") not in (None, ""):
            when = dt.datetime.fromtimestamp(float(a["post_time"]) / 1000, tz)
    except (TypeError, ValueError, OverflowError):
        when = None
    if when is None and st.get("last_changed"):
        when = dt.datetime.fromisoformat(st["last_changed"]).astimezone(tz)
    if when is None:
        return None
    return when, ("pickup" if any(w in low for w in PICKED_UP) else "delivery"), text[:120]


def deliveries(ctx, prev=None):
    """Today's package deliveries and pickups seen in phone notifications."""
    ents = ((ctx.discovered or {}).get("packages") or {}).get("notifications") or []
    if not ents:
        return {"configured": False}
    now = ctx.clock()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seen, notes = set(), []
    for series in ctx.ha.history(ents, start):
        for st in series or []:
            note = _package_note(st, now.tzinfo)
            if not note or note[0] < start:
                continue
            key = (int(note[0].timestamp()), note[1])
            if key not in seen:
                seen.add(key)
                notes.append(note)
    notes.sort(key=lambda n: n[0])
    out = {"delivery": [], "pickup": []}
    for when, kind, _ in notes:
        lst = out[kind]
        if not lst or when - lst[-1] >= SAME_DROP:
            lst.append(when)
    return {"configured": True, "day": start.date().isoformat(), "entities": ents,
            "deliveries": [t.isoformat() for t in out["delivery"]],
            "pickups": [t.isoformat() for t in out["pickup"]],
            "last": notes[-1][2] if notes else None}
