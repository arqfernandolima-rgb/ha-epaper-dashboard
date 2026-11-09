"""Kitchen display: a Seeed reTerminal E1001 (7.5" black and white, 800x480, on battery).

It runs ESPHome as a thin client and sleeps between updates. Each time it wakes up
(on its timer or a button) it asks

    GET /kitchen/plan?act=timer|refresh|next|prev|poll&bat=<volts>

which applies the button and answers "<sleep seconds> <full refresh 0/1> <stay awake 0/1>",
then downloads GET /kitchen.png?act=<same> (the page to show, a 1-bit PNG), draws it and
sleeps again. A timer wake whose page hasn't changed gets 304, so the panel isn't refreshed
for nothing. Pages for timer wakes are drawn ~30 s ahead, so the display isn't kept awake
waiting for Chromium.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time

import render

log = logging.getLogger("dash")
SIZE = (800, 480)
PAGES = ("setup", "type")              # setup-check pages until the real layouts are in
ACTS = ("timer", "refresh", "next", "prev", "poll")
WOKE = {"timer": "its timer", "refresh": "the green button", "next": "the right button",
        "prev": "the left button", "poll": "a stay-awake check"}
PREP = 45                              # draw this many seconds before a timer wake
RESEND = 6 * 3600                      # send the same page again after this long anyway
STAY_DEFAULT = "input_boolean.kitchen_display_stay_awake"
BATTERY_ENTITY = "sensor.kitchen_display_battery"
# LiPo resting voltage -> charge (roughly; good enough to tell full, half and "charge me")
CURVE = ((4.15, 100), (4.05, 90), (3.97, 80), (3.90, 70), (3.84, 60), (3.79, 50),
         (3.75, 40), (3.71, 30), (3.67, 20), (3.60, 10), (3.27, 0))


def battery_pct(v):
    if not v:
        return None
    if v >= CURVE[0][0]:
        return 100
    for (v1, p1), (v2, p2) in zip(CURVE, CURVE[1:]):
        if v >= v2:
            return round(p2 + (p1 - p2) * (v - v2) / (v1 - v2))
    return 0


def t12(d):
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


class Kitchen:
    def __init__(self, app, data_dir, workdir):
        self.app, self.path, self.workdir = app, os.path.join(data_dir, "kitchen.json"), workdir
        self.lock = threading.Lock()
        self.st = {"page": 0, "bat": None, "seen": None, "ip": None, "act": None, "wakes": 0,
                   "next": None, "sent": None, "sent_at": 0, "cleaned": None, "result": None}
        self.cache = {}                    # page -> {"for": wake ts or None, "png", "hash", "at"}
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

    # -- when to wake up next
    def next_wake(self, now):
        """The next round time (every N minutes from midnight), skipping one that's only a few minutes away
        (its clock runs a little fast or slow, so a wake just before a round time is that round time's wake).
        After quiet hours if it falls in them."""
        t = now + dt.timedelta(seconds=max(60, min(180, self.every() * 12)))
        mins = t.hour * 60 + t.minute + (1 if t.second or t.microsecond else 0)
        mins = -(-mins // self.every()) * self.every()
        nxt = t.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=mins)   # wall clock
        if self.app.quiet(nxt):
            end = int(self.app.opts.get("quiet_hours_end", 6))
            wake = nxt.replace(hour=end, minute=0)
            if wake <= nxt:
                wake += dt.timedelta(days=1)
            nxt = wake
        return nxt

    def stay_awake(self):
        eid = (self.app.opts.get("kitchen_stay_awake") or STAY_DEFAULT).strip()
        try:
            return self.app.ctx.ha.state(eid).get("state") == "on"
        except Exception:
            st = (self.app.sources["states"].data or {}).get(eid) or {}
            return st.get("state") == "on"

    # -- GET /kitchen/plan
    def plan(self, act, bat, ip, now):
        act = act if act in ACTS else "timer"
        try:
            bat = round(float(bat), 3) if bat not in (None, "") else None
            if bat is not None and not 2.5 < bat < 4.6:
                bat = None
        except ValueError:
            bat = None
        stay = self.stay_awake()
        with self.lock:
            st = self.st
            if act == "next":
                st["page"] = (st["page"] + 1) % len(PAGES)
            elif act == "prev":
                st["page"] = (st["page"] - 1) % len(PAGES)
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
            self._save()
        sleep = int(nxt.timestamp() - now.timestamp())     # real seconds (right across DST changes)
        if bat is not None:
            threading.Thread(target=self._publish_battery, args=(bat,), daemon=True).start()
        log.debug("kitchen %s from %s: page %d, sleep %ds, full %d, stay %d", act, ip, self.st["page"] + 1,
                  sleep, full, stay)
        return f"{sleep} {int(full)} {int(stay)}\n"

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
    def context(self, page, when, act):
        st = self.st
        nxt = self.next_wake(when)                      # what the plan at `when` answers
        quiet_next = nxt.timestamp() - when.timestamp() > (self.every() + 1) * 60
        v = st.get("bat")
        return {
            "page": PAGES[page], "n": page + 1, "of": len(PAGES), "drawn": t12(when),
            "next": t12(nxt) + (" (after quiet hours)" if quiet_next else ""), "every": self.every(),
            "woke": WOKE.get(act or "timer", act), "bat_v": f"{v:.2f} V" if v else None,
            "bat": battery_pct(v), "wakes": st.get("wakes") or 0,
        }

    def draw(self, page, when, act):
        """Render one page. Caller holds the app's render_lock."""
        html = render._env.get_template("kitchen.html").render(
            k=self.context(page, when, act), static="file://" + render.STATIC, icon=render.icon)
        png = render.mono_png(render.screenshot(html, self.workdir, SIZE, "kitchen"))
        return {"png": png, "hash": hashlib.sha1(png).hexdigest()[:16], "at": time.time()}

    def prepare(self, now):
        """Called from the main loop: draw the page for the coming timer wake ahead of time."""
        nxt = self.st.get("next")
        if not nxt or not 0 < nxt - now.timestamp() <= PREP:
            return
        page = self.st["page"]
        c = self.cache.get(page)
        if c and c.get("for") == nxt:
            return
        when = dt.datetime.fromtimestamp(nxt, now.tzinfo)
        with self.app.render_lock:
            try:
                out = self.draw(page, when, "timer")
            except Exception as e:
                log.warning("kitchen page: %s", e)
                return
        self.cache[page] = dict(out, **{"for": nxt})

    # -- GET /kitchen.png
    def image(self, act, now, device=True):
        """(status, png bytes or None). device=False for the status page preview (records nothing)."""
        page = self.st["page"]
        c = self.cache.get(page)
        fresh = c and (
            (act == "timer" and c.get("for") and abs(c["for"] - now.timestamp()) < 120)
            or (not device and time.time() - c["at"] < 600))
        if not fresh:
            with self.app.render_lock:
                out = self.draw(page, now, act if device else self.st.get("act"))
            c = self.cache[page] = dict(out, **{"for": None})
        if not device:
            return 200, c["png"]
        with self.lock:
            st = self.st
            same = act == "timer" and st.get("sent") == c["hash"] and time.time() - st.get("sent_at", 0) < RESEND
            st["result"] = "unchanged (304)" if same else "new page"
            if not same:
                st["sent"], st["sent_at"] = c["hash"], time.time()
            self._save()
        return (304, None) if same else (200, c["png"])

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
        return {
            "seen": when(st.get("seen")), "ip": st.get("ip"), "woke": WOKE.get(st.get("act"), st.get("act")),
            "page": f"{st['page'] + 1} of {len(PAGES)}", "next": when(st.get("next")),
            "battery": f"{battery_pct(v)}% ({v:.2f} V)" if v else None, "result": st.get("result"),
            "every": self.every(), "wakes": st.get("wakes") or 0,
            "stay": (self.app.opts.get("kitchen_stay_awake") or STAY_DEFAULT),
        }
