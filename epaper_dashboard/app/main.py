#!/usr/bin/env python3
"""E-paper dashboard app for Home Assistant.

The screen changes on one beat: every N minutes ("Refresh every" option), on round
times (N=5 -> :00, :05, :10 ...). Each refresh shows the latest data and the flip clock
reads that round time. The display is told when to check next (X-Poll-In header), so
it fetches ~25 s early and the new frame finishes appearing right on the minute.
If the clock is off and nothing on screen changed, that refresh is skipped.
New alerts can show at the display's next check-in instead of waiting for the beat.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
from zoneinfo import ZoneInfo

import ha as ha_api
import render
import sources
import view
import web

DATA = os.environ.get("DASH_DATA", "/data")
# The display fetches this long before the round time: download + ~21 s panel refresh.
LEAD = dt.timedelta(seconds=25)
PREP = dt.timedelta(seconds=10)      # draw a little ahead so the frame is ready when asked for
MIN_GAP = 175                        # panel spec: at least 180 s between refreshes (a little slack)
MAX_POLL = 180                       # the display checks in at least this often (for alerts)
WORK = os.environ.get("DASH_WORK", "/tmp/dash")
log = logging.getLogger("dash")


def load_options():
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"stocks": [{"symbol": "ADSK", "name": "Autodesk"}, {"symbol": "ZTS", "name": "Zoetis"}],
                "calendars": [], "quiet_hours_start": 0, "quiet_hours_end": 6, "steps_goal": 8000,
                "refresh_minutes": 5, "clock": True, "alerts_right_away": True, "alerts": []}


class Ctx:
    """What the data fetchers need: HA, location, time zone, units, options, found entities."""

    def __init__(self, ha, opts):
        self.ha, self.opts = ha, opts
        self.discovered = {}
        self.lat = self.lon = None
        self.tz, self.tz_name, self.imperial = dt.timezone.utc, "UTC", True
        self.clock = lambda: dt.datetime.now(self.tz)

    def load_ha_config(self):
        c = self.ha.config()
        self.lat, self.lon = c["latitude"], c["longitude"]
        self.tz_name = c.get("time_zone") or "UTC"
        self.tz = ZoneInfo(self.tz_name)
        self.imperial = (c.get("unit_system") or {}).get("temperature") == "°F"
        return c


class Source:
    def __init__(self, name, fn, every, stale_after=None, retry=120):
        self.name, self.fn, self.every, self.stale_after, self.retry = name, fn, every, stale_after, retry
        self.data, self.ok_at, self.tried_at, self.error = None, None, None, None

    def due(self, ctx):
        if self.tried_at is None:
            return True
        every = self.every(self) if callable(self.every) else self.every
        if self.error:
            every = min(every, self.retry)     # retry failures sooner (but don't hammer)
        return time.monotonic() - self.tried_at >= every

    def run(self, ctx):
        self.tried_at = time.monotonic()
        try:
            self.data = self.fn(ctx, self.data)
            self.ok_at, self.error = time.time(), None
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.warning("%s: %s", self.name, self.error)

    def fresh(self):
        if self.data is None:
            return None
        if self.stale_after and self.ok_at and time.time() - self.ok_at > self.stale_after:
            return None
        return self.data


class App:
    def __init__(self):
        self.opts = load_options()
        self.dev = os.environ.get("DASH_DEV") == "1"
        self.ctx = Ctx(ha_api.HA(), self.opts)
        self.ctx.clock = lambda: self.now()
        self.sticky = view.Sticky()
        self.sources = {
            "states": Source("Home Assistant", sources.ha_states, 60),
            "calendar": Source("Calendar", sources.calendar, 300, stale_after=6 * 3600),
            "weather": Source("Weather", sources.weather, 900, stale_after=3 * 3600),
            "markets": Source("Markets", sources.markets, self._markets_every, retry=300),
        }
        self.discovered_at = 0.0
        self.lock = threading.Lock()              # guards the frame/preview fields
        self.render_lock = threading.Lock()       # one Chromium at a time
        self.update_now = threading.Event()
        self.vm = self.vm_key = None
        self.vm_tomorrow = False
        self.frame_clock = None
        self.frame_slot = None                    # the round time the current frame was drawn for
        self.frame_alerts = set()                 # alerts already on the panel
        self.last_new_served = 0.0                # when the display last got a new frame
        self.frame = self.etag = None
        self.sim_png = self.panel_png = None
        self.last_key, self.drawn_at, self.colors = None, None, {}
        self.render_error, self.render_secs, self.state = None, None, "starting"
        self.last_poll = None
        self._load()

    # -- persistence: keep serving the same frame across restarts
    def _load(self):
        try:
            with open(os.path.join(DATA, "frame.epd"), "rb") as f:
                frame = f.read()
            with open(os.path.join(DATA, "state.json")) as f:
                st = json.load(f)
            if len(frame) == render.FRAME_BYTES:
                self.frame, self.etag = frame, st.get("etag")
                self.last_key, self.drawn_at = st.get("key"), st.get("drawn_at")
                self.sim_png, self.panel_png, self.colors = render.previews(render.unpack(frame))
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(os.path.join(DATA, "frame.epd"), "wb") as f:
                f.write(self.frame)
            with open(os.path.join(DATA, "state.json"), "w") as f:
                json.dump({"etag": self.etag, "key": self.last_key, "drawn_at": self.drawn_at}, f)
        except OSError as e:
            log.warning("couldn't save frame: %s", e)

    def _markets_every(self, src):
        return 180 if sources.market_open(src.data, self.now().timestamp()) else 600

    # -- quiet hours
    def quiet(self, now):
        a, b = int(self.opts.get("quiet_hours_start", 0)), int(self.opts.get("quiet_hours_end", 6))
        if a == b:
            return False
        return a <= now.hour < b if a < b else (now.hour >= a or now.hour < b)

    def tomorrow_mode(self, now):
        """The last frame before quiet hours shows tomorrow (first meeting, forecast)."""
        a, b = int(self.opts.get("quiet_hours_start", 0)), int(self.opts.get("quiet_hours_end", 6))
        if a == b:
            return False
        start = now.replace(hour=a, minute=0, second=0, microsecond=0)
        if start <= now:
            start += dt.timedelta(days=1)
        if start - now > dt.timedelta(minutes=15):
            return False
        wake = start.replace(hour=b)
        if wake <= start:
            wake += dt.timedelta(days=1)
        return wake.date() != now.date()       # we wake up on a new day

    def now(self):
        return dt.datetime.now(self.ctx.tz)

    def clock_on(self):
        return bool(self.opts.get("clock", True))

    def every(self):
        return max(3, min(60, int(self.opts.get("refresh_minutes", 5))))

    def slot_for(self, now):
        """The round time a frame fetched now will be showing (N-minute steps from midnight)."""
        t = now + LEAD + PREP
        m = t.hour * 60 + t.minute
        m -= m % self.every()
        return t.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)

    def next_fetch(self, now):
        """When the display should ask for the next frame so it finishes on the next round time."""
        nxt = self.slot_for(now) + dt.timedelta(minutes=self.every())
        if nxt.date() != (nxt - dt.timedelta(minutes=self.every())).date():
            nxt = nxt.replace(hour=0, minute=0)       # steps restart at midnight
        return nxt - LEAD

    def poll_in(self, now):
        """Seconds until the display should check again (sent as X-Poll-In)."""
        secs = (self.next_fetch(now) - now).total_seconds()
        return int(max(5, min(MAX_POLL, secs)))

    def ensure_frame(self, now, forced=False, why="beat"):
        """Make sure the frame on offer is the right one for this moment."""
        with self.render_lock:
            if self.vm is None:
                return
            if self.quiet(now + LEAD + PREP) and self.frame is not None and not forced:   # judged by when it'd show
                self.state = "quiet hours"
                return
            slot = None if self.vm_tomorrow else self.slot_for(now)
            t = now + LEAD + PREP                       # the minute this frame will be on screen
            clock = {"h": str(t.hour % 12 or 12), "m": f"{t.minute:02d}"} if slot and self.clock_on() else None
            new_slot = slot != self.frame_slot
            alerts = set(self.vm.get("alerts") or [])
            urgent = bool(self.opts.get("alerts_right_away", True)) and not alerts <= self.frame_alerts
            if forced or self.frame is None or urgent or (new_slot and (clock or self.vm_key != self.last_key)):
                self._draw(self.vm, self.vm_key, now, clock,
                           "update now" if forced else "alert" if urgent and not new_slot else "beat")
                self.frame_slot, self.frame_alerts = slot, alerts
            else:
                if new_slot:
                    self.frame_slot = slot              # nothing visible changed: skip this refresh
                self.state = "up to date"

    def _draw(self, vm, key, now, clock, why):
        """Render and publish. Caller holds render_lock."""
        t0 = time.time()
        try:
            out = render.render(vm, clock, WORK)
        except Exception as e:
            self.render_error = f"{type(e).__name__}: {e}"[:400]
            self.state = "drawing failed"
            log.error("render: %s", self.render_error)
            return
        self.render_secs = round(time.time() - t0, 1)
        with self.lock:
            changed = out["epd"] != self.frame
            self.sim_png, self.panel_png, self.colors = out["sim_png"], out["panel_png"], out["colors"]
            self.last_key, self.render_error, self.frame_clock = key, None, clock
            if changed:
                self.frame = out["epd"]
                self.etag = '"' + hashlib.sha1(self.frame).hexdigest()[:16] + '"'
                self.drawn_at = now.isoformat()
        self.state = "new frame" if changed else "up to date"
        if changed:
            self._save()
        (log.debug if why == "beat" else log.info)(
            "drew frame in %.1fs (%s, %s)", self.render_secs, why, "new" if changed else "same as before")

    def tick(self):
        ctx = self.ctx
        if ctx.lat is None:
            ctx.load_ha_config()
            log.info("home at %.3f,%.3f, time zone %s, %s units", ctx.lat, ctx.lon, ctx.tz_name,
                     "US" if ctx.imperial else "metric")
        st = self.sources["states"]
        if st.due(ctx):
            st.run(ctx)
        if st.data and (not ctx.discovered or time.time() - self.discovered_at > 600):
            try:
                ctx.discovered = sources.discover(ctx, st.data)
                self.discovered_at = time.time()
            except Exception as e:
                log.warning("discovery: %s", e)
        for key in ("calendar", "weather", "markets"):
            s = self.sources[key]
            if s.due(ctx):
                s.run(ctx)

        now = self.now()
        forced = self.update_now.is_set()
        self.update_now.clear()
        data = {k: s.fresh() for k, s in self.sources.items()}
        tomorrow = self.tomorrow_mode(now)
        vm = view.build(now, data, ctx.discovered, self.opts, self.sticky, tomorrow)
        key = hashlib.sha1(json.dumps(vm, sort_keys=True, default=str).encode()).hexdigest()
        self.vm, self.vm_key, self.vm_tomorrow = vm, key, tomorrow

        self.ensure_frame(now, forced)

    def note_poll(self, ip, code):
        self.last_poll = {"at": dt.datetime.now(self.ctx.tz).isoformat(), "ip": ip, "code": code}

    def run(self):
        web.start(self)
        while True:
            try:
                self.tick()
            except Exception as e:
                self.state = f"error: {e}"
                log.exception("tick failed")
            # wake at the next minute, or just before the display's next fetch, or for "Update now"
            now = self.now()
            wait = min(60 - time.time() % 60 + 1,
                       (self.next_fetch(now) - PREP - now).total_seconds() + 1)
            self.update_now.wait(timeout=max(1.0, wait))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    App().run()
