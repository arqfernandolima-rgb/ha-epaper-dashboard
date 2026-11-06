"""Data fetchers. Each returns plain data; view.py turns it into what's on screen."""
import datetime as dt
import json
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


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
def _yahoo(symbol):
    err = None
    for host in ("query1", "query2"):
        try:
            j = http_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                          f"{urllib.parse.quote(symbol)}?range=1d&interval=5m")
            r = j["chart"]["result"][0]
            m = r["meta"]
            closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
            reg = (m.get("currentTradingPeriod") or {}).get("regular") or {}
            return {
                "price": float(m["regularMarketPrice"]),
                "prev": float(m.get("chartPreviousClose") or m.get("previousClose")),
                "series": [round(float(c), 4) for c in closes if c is not None],
                "open_ts": reg.get("start"), "close_ts": reg.get("end"),
                "source": "Yahoo",
            }
        except Exception as e:      # try the other host
            err = e
    raise err


def _finnhub(symbol, key):
    q = http_json(f"https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(symbol)}&token={key}")
    if not q.get("c"):
        raise ValueError(f"Finnhub has no quote for {symbol}")
    return {"price": float(q["c"]), "prev": float(q["pc"]), "series": [],
            "open_ts": None, "close_ts": None, "source": "Finnhub"}


def markets(ctx, prev=None):
    """Delayed quotes and today's 5-minute chart. Yahoo's chart endpoint needs no key;
    Finnhub is a fallback if a key is set. A symbol that fails keeps its last quote."""
    quotes, errors = {}, []
    old = (prev or {}).get("quotes", {})
    for s in ctx.opts.get("stocks", []):
        sym = s["symbol"].upper()
        try:
            quotes[sym] = _yahoo(sym)
        except Exception as e:
            key = ctx.opts.get("finnhub_api_key")
            try:
                if not key:
                    raise
                quotes[sym] = _finnhub(sym, key)
            except Exception:
                errors.append(f"{sym}: {e}")
                if sym in old:
                    quotes[sym] = old[sym]
    if errors and len(errors) == len(ctx.opts.get("stocks", [])):
        raise RuntimeError("; ".join(errors))      # all failed: keep the last result, show the error
    return {"quotes": quotes, "errors": errors}


def market_open(mk, now_ts):
    """True while the regular session is open, based on the exchange's own hours."""
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
    now = dt.datetime.now(ctx.tz)
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
