# E-paper Dashboard

Draws the office dashboard for the Waveshare 10.85" (G) color e-paper panel and serves it to the ESP32-C6 display.

## How it works

The screen updates on one beat: every **Refresh every** minutes (default 5), on round times (:00, :05, :10 ...). Each update shows the latest data, and the flip clock reads that time. The app tells the display when to check next (`X-Poll-In` header), so the display fetches about 25 seconds early and the new picture finishes appearing right on the minute.

- Each update flashes the panel for about 21 seconds. 3 minutes is the panel's minimum, and the app never sends new frames closer together than that.
- **Flip clock** off: updates where nothing on screen changed are skipped.
- **Show new alerts right away**: a new alert (garage door, failed print) shows within 3 minutes instead of waiting for the next beat.
- Readings are rounded and held so tiny wiggles don't count as changes (indoor 1° / 3% / 25 ppb, steps in 500s, printer progress in 10% steps, nozzle and bed to 5°; the finish time is exact).
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
| 3D printers | Bambu Lab integration (HACS), every printer; only ones printing or just done show: progress, finish time, and nozzle / bed / chamber temperatures (°C) when there's room | found automatically |
| Packages | Nest package alerts from your phone (companion app "Last notification" sensor, Google Home app only), and/or Mail and Packages | turn on the sensor in the companion app |
| Health | Companion app Health Connect sensors | enable Daily steps, Sleep duration, Resting heart rate in the app |
| Garage | any garage door (MyQ, ratgdo ...): banner after it's been open 10 minutes | found automatically |
| Alerts | any entity and state | add in Configuration |

### The You column

Health is always there. Everything else shows only while it's active: a printer while it prints (and for a while after it finishes), a Claude plan while its usage is moving. **Hide idle items after** (default 3 hours) sets "a while". If the column gets full (two prints and both plans), printer temperatures go first, then printers shrink to one line, then the last item drops.

### Packages from Nest

The Nest integration doesn't pass package events to Home Assistant, but your phone gets them from the Google Home app. In the companion app: **Settings > Companion app > Manage sensors > Last notification**, turn it on, allow notification access, and in its **Allow list** pick only **Google Home**. The dashboard reads today's history of that sensor and counts "package" alerts (delivered, or picked up). It ignores every other notification, and with the allow list set, nothing else reaches Home Assistant.

The sidebar page shows a live preview, what was found in Home Assistant, and the status of each source. **Update now** draws a frame right away.

## Ports

- `8090`: the frame, for the display (LAN only, no login, serves nothing else).
- The sidebar page is only reachable through Home Assistant.
