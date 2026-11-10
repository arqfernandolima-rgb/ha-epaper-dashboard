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
        self.st = {"page": 0, "bat": None, "seen": None, "ip": None, "act": None, "wakes": 0, "pressed": 0,
                   "next": None, "sent": None, "sent_at": 0, "cleaned": None, "result": None}
        self.cache = {}                    # page -> {"for": wake ts or None, "key", "png", "at"}
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
            if act in ("next", "prev", "refresh"):
                st["pressed"] = now.timestamp()
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
    def model(self, page, when):
        """What a page shows at `when`, and a key for it (without the "Updated" time)."""
        build = kview.build_week if PAGES[page] == "week" else kview.build_house
        vm = build(self.app, when)
        key = hashlib.sha1(json.dumps(vm, sort_keys=True, default=str).encode()).hexdigest()[:16]
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
        vm, key = self.model(page, now)
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
            c = self.cache[page] = dict(out, **{"for": None, "key": key})
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
        return {
            "seen": when(st.get("seen")), "ip": st.get("ip"), "woke": WOKE.get(st.get("act"), st.get("act")),
            "page": f"{st['page'] + 1} of {len(PAGES)} ({'the week' if st['page'] == 0 else 'the house'})",
            "next": when(st.get("next")),
            "battery": f"{battery_pct(v)}% ({v:.2f} V)" if v else None, "result": st.get("result"),
            "every": self.every(), "wakes": st.get("wakes") or 0,
            "stay": (self.app.opts.get("kitchen_stay_awake") or STAY_DEFAULT),
        }
