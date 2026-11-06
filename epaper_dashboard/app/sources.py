"""Data fetchers. Each returns plain data; view.py turns it into what's on screen."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

VERSION = "0.4.2"
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
             hourly="temperature_2m,precipitation_probability,weather_code,uv_index,is_day",
             daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
             forecast_days=3)
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
              "precipitation_probability": [f.get("precipitation_probability") or 0 for _, f in hours],
              "weather_code": [HA_WMO.get(f.get("condition"), 3) for _, f in hours],
              "uv_index": [f.get("uv_index") or 0 for _, f in hours],
              "is_day": [0 if f.get("condition") == "clear-night" else 1 for _, f in hours]}
    by_day = {local(f["datetime"]).date(): f for f in fc["daily"]}
    daily = {"time": [], "weather_code": [], "temperature_2m_max": [], "temperature_2m_min": [],
             "precipitation_probability_max": [], "uv_index_max": []}
    for k in range(3):
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
    start, end = now - dt.timedelta(hours=12), now + dt.timedelta(days=4)
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
                           "title": title, "location": (e.get("location") or "").strip(), "calendar": c})
    if errors and len(errors) == len(cals):
        raise RuntimeError("; ".join(errors))
    events.sort(key=lambda e: e["start"])
    return {"calendars": cals, "events": events, "errors": errors}


# ---------------------------------------------------------------- HA entities
def ha_states(ctx, prev=None):
    return ctx.ha.states()


def discover(ctx, states):
    """Find the entities we need without any setup. Options override each guess."""
    o, ha = ctx.opts, ctx.ha

    def first(cands, test):
        return next((e for e in cands if test(e)), None)

    def by_class(cands, cls, hint):
        return (first(cands, lambda e: states.get(e, {}).get("attributes", {}).get("device_class") == cls)
                or first(cands, lambda e: hint in e))

    air = [e for e in ha.integration_entities("airthings") if e.startswith("sensor.")]
    indoor = {
        "temperature": o.get("indoor_temperature") or by_class(air, "temperature", "temperature"),
        "humidity": o.get("indoor_humidity") or by_class(air, "humidity", "humidity"),
        "voc": o.get("indoor_voc") or by_class(air, "volatile_organic_compounds_parts", "voc"),
    }
    area = ""
    if indoor["temperature"]:
        try:
            area = ha.template("{{ area_name('%s') or '' }}" % indoor["temperature"]).strip()
        except Exception:
            pass
    indoor["area"] = area

    # Bambu Lab: the printer's sensors share a prefix (sensor.h2c_...); find it from any of the usual ones
    bambu = [e for e in ha.integration_entities("bambu_lab") if e.startswith("sensor.")]
    printer = o.get("printer")
    if not printer:
        for suffix in ("_print_status", "_print_progress", "_remaining_time", "_current_stage"):
            hit = first(bambu, lambda e, sf=suffix: e.endswith(sf))
            if hit:
                printer = hit
                break
    for suffix in ("_print_status", "_print_progress", "_remaining_time", "_current_stage"):
        if printer and printer.endswith(suffix):
            printer = printer[: -len(suffix)]

    # Garage doors (MyQ, ratgdo, Meross ...): covers marked as garage/gate, or with "garage" in the name
    garages = sorted(e for e, st in states.items() if e.startswith("cover.") and (
        st.get("attributes", {}).get("device_class") in ("garage", "gate")
        or "garage" in e or "garage" in str(st.get("attributes", {}).get("friendly_name", "")).lower()))

    mail = ha.integration_entities("mail_and_packages")
    packages = {
        "arriving": o.get("packages") or first(mail, lambda e: e.endswith("packages_in_transit")),
        "delivered": None if o.get("packages") else first(mail, lambda e: e.endswith("packages_delivered")),
    }

    phone = [e for e in ha.integration_entities("mobile_app") if e.startswith("sensor.")]
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
    return {"indoor": indoor, "printer": printer, "packages": packages, "health": health, "calendars": cals,
            "weather": weather_eid, "garages": garages}
