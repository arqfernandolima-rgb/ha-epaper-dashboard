# E-paper Dashboard

Draws the office dashboard for the Waveshare 10.85" (G) color e-paper panel and serves it to the ESP32-C6 display.

## How it works

The screen updates on one beat: every **Refresh every** minutes (default 5), on round times (:00, :05, :10 ...). Each update shows the latest data, and the flip clock reads that time. The app tells the display when to check next (`X-Poll-In` header), so the display fetches about 25 seconds early and the new picture finishes appearing right on the minute.

- Each update flashes the panel for about 21 seconds. 3 minutes is the panel's minimum, and the app never sends new frames closer together than that.
- **Flip clock** off: updates where nothing on screen changed are skipped.
- **Show new alerts right away**: a new alert (garage door, failed print) shows within 3 minutes instead of waiting for the next beat.
- Readings are rounded and held so tiny wiggles don't count as changes (indoor 1° / 3% / 25 ppb, steps in 500s, printer nozzle and bed to 5°; print progress and finish time are exact).
- Quiet hours (midnight to 6 AM by default): nothing changes. The last frame before them shows tomorrow, without the clock. The first frame after them reads 6:00 right at 6:00.

The display reads `http://<HA address>:8090/frame.epd` and only redraws when the frame's ETag changes.

## Where the data comes from

| Widget | Source | Setup |
|---|---|---|
| Weather, AQI, UV | Open-Meteo, using your Home Assistant home location | none |
| Stocks | Finnhub (official free API); the day chart is drawn from the app's own samples | free API key + symbols in Configuration (use SPY for the S&P 500) |
| Portfolio | SnapTrade Personal (free): balances and holdings for E*TRADE (OAuth) and Vanguard, refreshed every 2 hours; stocks and ETFs priced live with Finnhub | SnapTrade client ID + consumer key in Configuration |
| Claude | Your Clawdmeter daemon pushes Max and Enterprise usage to `http://<HA>:8090/claude`; a plan with no new usage for a few hours hides | two lines in the daemon's config (shown on the sidebar page) |
| Calendar | Home Assistant calendar entities | any calendar integration |
| Indoor air | any temperature / humidity / VOC / CO2 sensors, per area (up to 3 rooms); a reading the main sensor lacks (CO2 on a Wave Mini) comes from another sensor in the same area | list areas in "Indoor rooms" (default: every Airthings device) |
| Car | Rivian integration (HACS), labeled by model (R1T): shows only when it's below 25% (red), charging ("Charging · full at 6:40 AM"), or plugged in waiting to charge ("Plugged in", "Charge scheduled"); bar with the charge-limit mark, plus range | found automatically (or any EV's battery sensor in Configuration) |
| 3D printers | Bambu Lab integration (HACS), every printer; only ones printing or just done show: progress, finish time, and nozzle / bed / chamber temperatures (°C) when there's room | found automatically |
| Packages | Nest package alerts from your phone (companion app "Last notification" sensor, Google Home app only), and/or Mail and Packages | turn on the sensor in the companion app |
| Health | Companion app Health Connect sensors | enable Daily steps, Sleep duration, Resting heart rate in the app |
| Garage | any garage door (MyQ, ratgdo ...): banner after it's been open 10 minutes | found automatically |
| Alerts | any entity and state | add in Configuration |

### The You column

Health is always there. Everything else shows only while it's active (the car when it's low, charging or plugged in): a printer while it prints (and for a while after it finishes), a Claude plan while its usage is moving. **Hide idle items after** (default 3 hours) sets "a while". If the column gets full (two prints and both plans), printer temperatures go first, then printers shrink to one line, then the car drops its range line, then the last item drops.

### Packages from Nest

The Nest integration doesn't pass package events to Home Assistant, but your phone gets them from the Google Home app. In the companion app: **Settings > Companion app > Manage sensors > Last notification**, turn it on, allow notification access, and in its **Allow list** pick only **Google Home**. The dashboard reads today's history of that sensor and counts "package" alerts (delivered, or picked up). It ignores every other notification, and with the allow list set, nothing else reaches Home Assistant.

## Kitchen display (reTerminal E1001)

A second, battery-powered display: Seeed's reTerminal E1001 (7.5" black and white, 800x480) running ESPHome as a thin client. The app draws its pages (same fonts and look as the office panel); the display only downloads and shows them, then deep-sleeps.

**Page 1, the week.** Today's date, the weather now and every 3 hours, and one line of advice (what to wear, an umbrella, the trash going out tonight). Below, four rolling days plus the weekend (Saturday and Sunday stacked in one column, always on screen; on Mondays it takes Friday's place). Each day shows its weather, trash and recycling, birthdays, holidays, and family events with a badge for who they're for; all-day events (trips) are bars across the days someone is away. After 6 PM the page is about tomorrow. The bottom row only shows exceptions: a garage door left open, a package waiting, a room too hot or cold, the car low.

What comes from the calendars:
- **Trash and recycling** from calendars named trash/recycling (or all-day events saying so).
- **Birthdays** and **holidays** from those calendars, as small tags.
- **Trips**: all-day events that aren't birthdays, holidays or bins. The person is whoever's name is in the title (**Kitchen display people**), otherwise the first person (the calendars' owner). Weekly all-day events (like the cleaner) are tags, not trips.
- **Family events**: timed events whose title has a family member's name (other than the owner's) or a family word (school, bus, practice, game, recital, dentist, birthday party, field trip, buscar, levar…). Work meetings stay off. Weekly routine events show on today and tomorrow only.
- Canceled events are skipped.

**Page 2, the house.** A snapshot: one inside temperature (the average of the rooms, with any room 5°F off the rest called out), lights on (outdoor ones are lights with "outdoor" in the name), air (Good / Fair / Poor from CO2, VOC, radon and PM2.5 sensors, readings by room), what music is playing, garage doors, doors and windows (contact sensors), the car, outdoor lights and the to-do lists (**Kitchen display to-do lists**: any to-do entities, comma-separated, e.g. Microsoft To Do lists from the MS365 To Do integration; default `todo.shopping_list`). Each list with something on it gets a line. Anything that needs a look goes to the top in black, and the header says "All good" or how many things to check. Rooms are HA areas; garage, greenhouse, pool house, porch and similar areas don't count as inside.

**Good night.** Hold the green button for 2 seconds: it runs **Kitchen display good night script** (default `script.good_night`, if you have one), otherwise turns off every light (not the 3D printers') and pauses any music. The display then shows what it did, anything still open, and tomorrow's weather and first event.

- It wakes on its own every **Kitchen display refresh** minutes (default 15), on round times, and sleeps from **Kitchen display sleeps from** (10 PM) to **wakes at** (6 AM). Any button wakes it right away. If someone leaves it on page 2, the next update goes back to page 1.
- Buttons: **left** previous page, **right** next page, **green** refresh. After a press it stays awake 30 seconds, so more presses flip pages with a quick partial refresh.
- Each wake: `GET http://<HA>:8090/kitchen/plan?act=<timer|refresh|next|prev>&bat=<volts>` answers `<sleep seconds> <full refresh 0/1> <stay awake 0/1>`, then `GET /kitchen.png?act=...` returns the page as a 1-bit PNG (or 304 on a timer wake when nothing changed, so the panel isn't refreshed for nothing). Pages for timer wakes are drawn ahead of time, so the display isn't kept awake waiting.
- The first update of each day, and every green-button press, is a full refresh, which clears any ghosting the quick refreshes leave behind.
- Battery: published to Home Assistant as `sensor.kitchen_display_battery` at every wake. The house page shows it small in the footer; the week page shows a battery icon only when it's time to charge (10% or less, or about 2 days left at the current rate). The sidebar page shows the days-left estimate once there is a day of readings (it starts over after a charge).
- Firmware updates: turn on a toggle helper named **Kitchen display stay awake** (`input_boolean.kitchen_display_stay_awake`), and the display stays awake from its next wake-up (or press any button to wake it now) for the ESPHome update. Turn the helper off and it goes back to sleep within a couple of minutes; if you forget, the app turns it off 30 minutes after the display first saw it on.

The ESPHome config is in this repository at `esphome/kitchen-display.yaml`.

The sidebar page shows a live preview, what was found in Home Assistant, and the status of each source. **Update now** draws a frame right away.

## Ports

- `8090`: the frames, for the displays (LAN only, no login): `/frame.epd`, `/kitchen/plan`, `/kitchen.png`, plus `/claude` for the Clawdmeter daemon (needs its token).
- The sidebar page is only reachable through Home Assistant.
