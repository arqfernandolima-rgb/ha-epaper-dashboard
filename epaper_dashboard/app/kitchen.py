"""Kitchen display: a Seeed reTerminal E1001 (7.5" black and white, 800x480, on battery).

It runs ESPHome as a thin client and sleeps between updates. Each time it wakes up
(on its timer or a button) it asks

    GET /kitchen/plan?act=timer|refresh|next|prev|poll&bat=<volts>

which applies the button and answers "<sleep seconds> <full refresh 0/1> <stay awake 0/1>",
then downloads GET /kitchen.png?act=<same> (the page to show, a 1-bit PNG), draws it and
sleeps again. A timer wake whose page hasn't changed gets 304, so the panel isn't refreshed
for nothing. Pages for timer wakes are drawn ~45 s ahead, so the display isn't kept awake
waiting for Chromium. What the pages show is built in kview.py.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time

import kview
import render

log = logging.getLogger("dash")
SIZE = (800, 480)
PAGES = ("week", "house")
BACK_TO_WEEK = 10 * 60                 # a timer wake after this long goes back to page 1
ACTS = ("timer", "refresh", "next", "prev", "poll", "goodnight")
WOKE = {"timer": "its timer", "refresh": "the green button", "next": "the right button",
        "prev": "the left button", "poll": "a stay-awake check", "goodnight": "good night (green held)"}
STAY_MAX = 30 * 60                     # the stay-awake switch turns itself off after this (battery)
PREP = 45                              # draw this many seconds before a timer wake
RESEND = 6 * 3600                      # send the same page again after this long anyway
STAY_DEFAULT = "input_boolean.kitchen_display_stay_awake"
BATTERY_ENTITY = "sensor.kitchen_display_battery"
# LiPo resting voltage -> charge (roughly; good enough to tell full, half and "charge me")
CURVE = ((4.15, 100), (4.05, 90), (3.97, 80), (3.90, 70), (3.84, 60), (3.79, 50),
         (3.75, 40), (3.71, 30), (3.67, 20), (3.60, 10), (3.27, 0))


BAT_LOG_DAYS = 21                      # readings kept for the days-left estimate
BAT_EVERY = 30 * 60                    # at most one logged reading per half hour (plus any jump)
CHARGED = 0.08                         # a rise this big (volts) between readings = it was charged
LOW_PCT, LOW_DAYS = 10, 2              # page 1 shows the battery when it's this low


def battery_pct(v):
    if not v:
        return None
    if v >= CURVE[0][0]:
        return 100
    for (v1, p1), (v2, p2) in zip(CURVE, CURVE[1:]):
        if v >= v2:
            return round(p2 + (p1 - p2) * (v - v2) / (v1 - v2))
    return 0


def battery_estimate(log, now_ts):
    """(% a day, days left) from the readings since the last charge (last 7 days), or Nones while
    there's under a day of readings to go on."""
    start = 0
    for i in range(len(log) - 1, 0, -1):
        if log[i][1] - log[i - 1][1] > CHARGED:
            start = i
            break
    pts = [(t, battery_pct(v)) for t, v in log[start:] if now_ts - t <= 7 * 86400]
    if len(pts) < 4 or pts[-1][0] - pts[0][0] < 20 * 3600:
        return None, None
    n = len(pts)
    mt, mp = sum(t for t, _ in pts) / n, sum(p for _, p in pts) / n
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    rate = -sum((t - mt) * (p - mp) for t, p in pts) / sxx * 86400
    if rate < 0.1:                     # not measurably dropping (yet)
        return max(rate, 0.0), None
    return rate, pts[-1][1] / rate


def battery_icon(pct, low):
    return "battery-warning" if low else "battery-full" if pct >= 70 else "battery-medium" if pct >= 35 else "battery-low"


def t12(d):
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


class Kitchen:
    def __init__(self, app, data_dir, workdir):
        self.app, self.path, self.workdir = app, os.path.join(data_dir, "kitchen.json"), workdir
        self.lock = threading.Lock()
        self.st = {"page": 0, "bat": None, "seen": None, "ip": None, "act": None, "wakes": 0, "pressed": 0,
                   "next": None, "sent": None, "sent_at": 0, "cleaned": None, "result": None}
        self.cache = {}                    # page -> {"for": wake ts or None, "key", "png", "at"}
        self.seeded = False                # battery log filled from HA's history yet
        try:
            with open(self.path) as f:
                self.st.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.st["page"] = int(self.st.get("page") or 0) % len(PAGES)

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.st, f)
        except OSError as e:
            log.warning("couldn't save kitchen state: %s", e)

    def every(self):
        return max(5, min(60, int(self.app.opts.get("kitchen_refresh_minutes", 15))))

    def quiet_hours(self):
        o = self.app.opts
        return int(o.get("kitchen_quiet_start", 22)), int(o.get("kitchen_quiet_end", 6))

    def quiet(self, t):
        a, b = self.quiet_hours()
        if a == b:
            return False
        return a <= t.hour < b if a < b else (t.hour >= a or t.hour < b)

    # -- when to wake up next
    def next_wake(self, now):
        """The next round time (every N minutes from midnight), skipping one that's only a few minutes away
        (its clock runs a little fast or slow, so a wake just before a round time is that round time's wake).
        After quiet hours if it falls in them."""
        t = now + dt.timedelta(seconds=max(60, min(180, self.every() * 12)))
        mins = t.hour * 60 + t.minute + (1 if t.second or t.microsecond else 0)
        mins = -(-mins // self.every()) * self.every()
        nxt = t.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=mins)   # wall clock
        if self.quiet(nxt):
            end = self.quiet_hours()[1]
            wake = nxt.replace(hour=end, minute=0)
            if wake <= nxt:
                wake += dt.timedelta(days=1)
            nxt = wake
        return nxt

    def stay_awake(self, now):
        """The stay-awake switch (for firmware updates). It's switched off again 30 minutes after the
        display first saw it on (not after it was flipped: flipped at night, the display only sees it
        at its first wake in the morning and should still stay up then), so a forgotten switch can't
        drain the battery."""
        eid = (self.app.opts.get("kitchen_stay_awake") or STAY_DEFAULT).strip()
        try:
            st = self.app.ctx.ha.state(eid)
        except Exception:
            st = (self.app.sources["states"].data or {}).get(eid) or {}
        if st.get("state") != "on":
            return False
        turned_on = st.get("last_changed") or "on"
        with self.lock:
            if self.st.get("stay_on") != turned_on:          # first check-in since it was turned on
                self.st["stay_on"], self.st["stay_from"] = turned_on, now.timestamp()
                self._save()
            start = self.st["stay_from"]
        if now.timestamp() - start > STAY_MAX:
            try:
                self.app.ctx.ha.call("input_boolean", "turn_off", {"entity_id": eid})
                log.info("kitchen: stay-awake switch was on for 30+ min, turned it off")
            except Exception as e:
                log.warning("couldn't turn off %s: %s", eid, e)
            return False
        return True

    def goodnight(self, now):
        """Holding green at bedtime: the house's good night script if there is one, otherwise
        every light off (not the 3D printers') and the music paused."""
        ha, out = self.app.ctx.ha, {"at": now.timestamp(), "lights": 0, "music": 0, "script": None}
        try:
            states = ha.states()
        except Exception:
            states = self.app.sources["states"].data or {}
        script = (self.app.opts.get("kitchen_goodnight_script") or "script.good_night").strip()
        try:
            if script in states:
                ha.call("script", "turn_on", {"entity_id": script})
                out["script"] = script
            else:
                lin, lout = kview.house_lights(states)
                if lin + lout:
                    ha.call("light", "turn_off", {"entity_id": lin + lout})
                out["lights"] = len(lin + lout)
                playing = [e for e, st in states.items() if e.startswith("media_player.") and st.get("state") == "playing"]
                if playing:
                    ha.call("media_player", "media_pause", {"entity_id": playing})
                out["music"] = len(playing)
        except Exception as e:
            log.warning("kitchen good night: %s", e)
            out["error"] = str(e)
        log.info("kitchen good night: %s", out)
        try:                                         # so the page shows the lights off right away
            self.app.sources["states"].run(self.app.ctx)
        except Exception:
            pass
        return out

    # -- GET /kitchen/plan
    def plan(self, act, bat, ip, now):
        act = act if act in ACTS else "timer"
        try:
            bat = round(float(bat), 3) if bat not in (None, "") else None
            if bat is not None and not 2.5 < bat < 4.6:
                bat = None
        except ValueError:
            bat = None
        stay = self.stay_awake(now)
        night = self.goodnight(now) if act == "goodnight" else None
        with self.lock:
            st = self.st
            if act == "next":
                st["page"] = (st["page"] + 1) % len(PAGES)
            elif act == "prev":
                st["page"] = (st["page"] - 1) % len(PAGES)
            if act in ("next", "prev", "refresh", "goodnight"):
                st["pressed"] = now.timestamp()
            if night:
                st["night"] = night
            elif act == "timer" and st["page"] and now.timestamp() - (st.get("pressed") or 0) >= BACK_TO_WEEK:
                st["page"] = 0                       # someone left it on the house page
            today = now.date().isoformat()
            full = act == "refresh" or (act == "timer" and st.get("cleaned") != today)
            if full:
                st["cleaned"] = today
            nxt = self.next_wake(now)
            if act != "poll":
                st["act"], st["wakes"] = act, int(st.get("wakes") or 0) + 1
            st.update(seen=now.timestamp(), ip=ip, next=nxt.timestamp())
            if bat is not None:
                st["bat"] = bat
                self._log_battery(now.timestamp(), bat)
            self._save()
        sleep = int(nxt.timestamp() - now.timestamp())     # real seconds (right across DST changes)
        if bat is not None:
            threading.Thread(target=self._publish_battery, args=(bat,), daemon=True).start()
        log.debug("kitchen %s from %s: page %d, sleep %ds, full %d, stay %d", act, ip, self.st["page"] + 1,
                  sleep, full, stay)
        return f"{sleep} {int(full)} {int(stay)}\n"

    # -- battery: a log of readings for the days-left estimate. Caller holds self.lock.
    def _log_battery(self, ts, v):
        log_ = self.st.setdefault("bat_log", [])
        if not log_ or ts - log_[-1][0] >= BAT_EVERY or abs(v - log_[-1][1]) >= CHARGED:
            log_.append([round(ts), v])
        del log_[:max(0, len(log_) - 2000)]
        while log_ and ts - log_[0][0] > BAT_LOG_DAYS * 86400:
            log_.pop(0)

    def _seed_battery(self):
        """Once: fill the log from Home Assistant's history of the battery sensor (readings from
        before this version kept a log)."""
        now = time.time()
        try:
            rows = self.app.ctx.ha.history([BATTERY_ENTITY], dt.datetime.fromtimestamp(now - BAT_LOG_DAYS * 86400,
                                                                                        dt.timezone.utc))
        except Exception as e:
            log.debug("kitchen battery history: %s", e)
            return
        seen = []
        for s in (rows[0] if rows else []):
            v = (s.get("attributes") or {}).get("voltage")
            when = s.get("last_updated") or s.get("last_changed")
            if isinstance(v, (int, float)) and when:
                seen.append((dt.datetime.fromisoformat(when).timestamp(), float(v)))
        with self.lock:
            merged = sorted(set(seen) | {tuple(x) for x in self.st.get("bat_log") or []})
            self.st["bat_log"] = []
            for t, v in merged:
                self._log_battery(t, v)
            self._save()
        log.info("kitchen battery: %d readings in the log", len(self.st["bat_log"]))

    def battery(self, now):
        """{"pct", "v", "rate" (% a day), "days" (left), "low"} or None before the first reading."""
        v = self.st.get("bat")
        pct = battery_pct(v)
        if pct is None:
            return None
        rate, days = battery_estimate(self.st.get("bat_log") or [], now.timestamp())
        low = pct <= LOW_PCT or (days is not None and days <= LOW_DAYS)
        return {"pct": pct, "v": v, "rate": rate, "days": days, "low": low, "icon": battery_icon(pct, low)}

    def _publish_battery(self, v):
        pct = battery_pct(v)
        try:
            self.app.ctx.ha._req("POST", f"/api/states/{BATTERY_ENTITY}", {
                "state": pct, "attributes": {
                    "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement",
                    "friendly_name": "Kitchen display battery", "voltage": v}})
        except Exception as e:
            log.debug("couldn't publish the kitchen battery: %s", e)

    # -- the page
    def model(self, page, when, act=None):
        """What a page shows at `when`, and a key for it (without the "Updated" time)."""
        if act == "goodnight" and self.st.get("night"):
            vm = kview.build_night(self.app, when, self.st["night"])
        else:
            build = kview.build_week if PAGES[page] == "week" else kview.build_house
            vm = build(self.app, when)
            b = self.battery(when)
            # the house page always shows the charge (small); the week only when it's time to charge
            if b and (PAGES[page] == "house" or b["low"]):
                vm["battery"] = {"pct": b["pct"], "icon": b["icon"], "low": b["low"]}
                if PAGES[page] == "week":
                    vm["foot"] = vm["foot"][:2]      # room for it next to "Updated"
        key =hashlib.sha1(json.dumps(vm, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return vm, key

    def draw(self, page, vm, when):
        """Render one page. Caller holds the app's render_lock."""
        k = dict(vm, n=page + 1, of=len(PAGES), updated=f"Updated {t12(when)}")
        html = render._env.get_template("kitchen.html").render(k=k, static="file://" + render.STATIC, icon=render.icon)
        png = render.mono_png(render.screenshot(html, self.workdir, SIZE, "kitchen"))
        return {"png": png, "at": time.time()}

    def prepare(self, now):
        """Called from the main loop: draw the page for the coming timer wake ahead of time
        (unless nothing changed, in which case the wake gets 304 and nothing needs drawing)."""
        if not self.seeded:
            self.seeded = True
            threading.Thread(target=self._seed_battery, daemon=True).start()
        nxt = self.st.get("next")
        if not nxt or not 0 < nxt - now.timestamp() <= PREP:
            return
        page = self._page_at_wake(nxt)
        c = self.cache.get(page)
        if c and c.get("for") == nxt:
            return
        when = dt.datetime.fromtimestamp(nxt, now.tzinfo)
        try:
            vm, key = self.model(page, when)
            if key == self.st.get("sent"):
                return                               # nothing new: the wake gets 304
            if c and c.get("key") == key:
                c["for"] = nxt
                return
            with self.app.render_lock:
                out = self.draw(page, vm, when)
        except Exception as e:
            log.warning("kitchen page: %s", e)
            return
        self.cache[page] = dict(out, **{"for": nxt, "key": key})

    def _page_at_wake(self, ts):
        st = self.st
        if st["page"] and ts - (st.get("pressed") or 0) >= BACK_TO_WEEK:
            return 0
        return st["page"]

    # -- GET /kitchen.png
    def image(self, act, now, device=True, page=None):
        """(status, png bytes or None). device=False for the status page preview (records nothing,
        any page)."""
        page = self.st["page"] if page is None or device else page % len(PAGES)
        vm, key = self.model(page, now, act if device else None)
        c = self.cache.get(page)
        if device and act == "timer":
            with self.lock:
                st = self.st
                if st.get("sent") == key and time.time() - st.get("sent_at", 0) < RESEND:
                    st["result"] = "unchanged (304)"
                    self._save()
                    return 304, None
        if not (c and c.get("key") == key and (device or time.time() - c["at"] < 600)):
            with self.app.render_lock:
                out = self.draw(page, vm, now)
            c = dict(out, **{"for": None, "key": key})
            if vm.get("page") != "night":
                self.cache[page] = c
        if not device:
            return 200, c["png"]
        with self.lock:
            st = self.st
            st["result"] = "new page"
            st["sent"], st["sent_at"] = key, time.time()
            self._save()
        return 200, c["png"]

    def wait_hint(self, now):
        """When the main loop should wake to prepare the next page (or None)."""
        nxt = self.st.get("next")
        if not nxt:
            return None
        return nxt - PREP + 2 - now.timestamp()

    def status(self, tz):
        st = self.st
        def when(ts):
            if not ts:
                return None
            d = dt.datetime.fromtimestamp(ts, tz)
            return d.strftime("%a ") + t12(d)
        v = st.get("bat")
        b = self.battery(dt.datetime.now(tz)) or {}
        if b.get("days") is not None:
            left = f" · about {b['days']:.0f} days left ({b['rate']:.1f}% a day)"
        elif b.get("rate") is not None:
            left = " · barely dropping so far"
        else:
            left = " · days left: after a day of readings" if v else ""
        return {
            "seen": when(st.get("seen")), "ip": st.get("ip"), "woke": WOKE.get(st.get("act"), st.get("act")),
            "page": f"{st['page'] + 1} of {len(PAGES)} ({'the week' if st['page'] == 0 else 'the house'})",
            "next": when(st.get("next")),
            "battery": f"{battery_pct(v)}% ({v:.2f} V){left}" if v else None, "result": st.get("result"),
            "every": self.every(), "wakes": st.get("wakes") or 0,
            "stay": (self.app.opts.get("kitchen_stay_awake") or STAY_DEFAULT),
        }
