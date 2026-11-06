"""Data fetchers. Each returns plain data; view.py turns it into what's on screen."""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UA = "epaper-dashboard (Home Assistant app)"


def http_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- weather
def weather(ctx, prev=None):
    """Open-Meteo forecast + air quality for HA's home location. Free, no key."""
    base = {"latitude": ctx.lat, "longitude": ctx.lon, "timezone": ctx.tz_name}
    p = dict(base,
             current="temperature_2m,weather_code,is_day,wind_speed_10m,wind_gusts_10m",
             hourly="temperature_2m,precipitation_probability,weather_code,uv_index,is_day",
             daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
             forecast_days=3)
    if ctx.imperial:
        p.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    forecast = http_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(p))
    try:
        aq = http_json("https://air-quality-api.open-meteo.com/v1/air-quality?"
                       + urllib.parse.urlencode(dict(base, current="us_aqi")))
    except Exception:
        aq = (prev or {}).get("aq")          # keep the last reading if only AQI failed
    return {"forecast": forecast, "aq": aq, "imperial": ctx.imperial}


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

    quotes, errors = {}, []
    for s in ctx.opts.get("stocks", []):
        sym = s["symbol"].upper()
        try:
            q = _finnhub(f"/quote?symbol={urllib.parse.quote(sym)}", key)
            if not q.get("c") or not q.get("pc"):
                raise ValueError("no quote (indexes like ^GSPC aren't on the free plan; try SPY)")
            price, prev_close = float(q["c"]), float(q["pc"])
            if in_session:
                rec = samples.get(sym)
                if not rec or rec.get("day") != day:
                    rec = samples[sym] = {"day": day, "open": o, "close": c, "pts": []}
                rec["pts"].append([int(now.timestamp()), price])
            rec = samples.get(sym) or {}
            series = _day_chart(rec.get("pts", []), rec.get("open", o), rec.get("close", c),
                                now.timestamp(), q.get("o")) if rec else []
            quotes[sym] = {"price": price, "prev": prev_close, "series": series,
                           "open_ts": o, "close_ts": c, "source": "Finnhub"}
        except Exception as e:
            errors.append(f"{sym}: {e}"[:160])
            if sym in old.get("quotes", {}):
                quotes[sym] = old["quotes"][sym]
    if in_session:
        _save_samples(samples)
    if errors and len(errors) == len(ctx.opts.get("stocks", [])):
        raise RuntimeError("; ".join(errors))      # all failed: keep the last result, show the error
    return {"quotes": quotes, "errors": errors, "status": status, "samples": samples}


def market_open(mk, now_ts):
    """True while the regular session is open."""
    for q in (mk or {}).get("quotes", {}).values():
        if q.get("open_ts") and q.get("close_ts"):
            return q["open_ts"] <= now_ts < q["close_ts"]
    return False


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

    printer = o.get("printer") or first(ha.integration_entities("bambu_lab"),
                                        lambda e: e.startswith("sensor.") and e.endswith("_print_status"))
    if printer and printer.endswith("_print_status"):
        printer = printer[: -len("_print_status")]

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
    return {"indoor": indoor, "printer": printer, "packages": packages, "health": health, "calendars": cals}
