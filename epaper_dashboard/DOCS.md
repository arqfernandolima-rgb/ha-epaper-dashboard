# E-paper Dashboard

Draws the office dashboard for the Waveshare 10.85" (G) color e-paper panel and serves it to the ESP32-C6 display.

## How it works

The screen updates on one beat: every **Refresh every** minutes (default 5), on round times (:00, :05, :10 ...). Each update shows the latest data, and the flip clock reads that time. The app tells the display when to check next (`X-Poll-In` header), so the display fetches about 25 seconds early and the new picture finishes appearing right on the minute.

- Each update flashes the panel for about 21 seconds. 3 minutes is the panel's minimum, and the app never sends new frames closer together than that.
- **Flip clock** off: updates where nothing on screen changed are skipped.
- **Show new alerts right away**: a new alert (garage door, failed print) shows within 3 minutes instead of waiting for the next beat.
- Readings are rounded and held so tiny wiggles don't count as changes (indoor 1° / 3% / 25 ppb, steps in 500s, printer progress in 10% steps, finish time to 15 minutes).
- Quiet hours (midnight to 6 AM by default): nothing changes. The last frame before them shows tomorrow, without the clock. The first frame after them reads 6:00 right at 6:00.

The display reads `http://<HA address>:8090/frame.epd` and only redraws when the frame's ETag changes.

## Where the data comes from

| Widget | Source | Setup |
|---|---|---|
| Weather, AQI, UV | Open-Meteo, using your Home Assistant home location | none |
| Stocks | Finnhub (official free API); the day chart is drawn from the app's own samples | free API key + symbols in Configuration (use SPY for the S&P 500) |
| Calendar | Home Assistant calendar entities | any calendar integration |
| Indoor air | Airthings integration | found automatically |
| 3D printer | Bambu Lab integration (HACS) | found automatically |
| Packages | Mail and Packages integration (HACS) | found automatically |
| Health | Companion app Health Connect sensors | enable Daily steps, Sleep duration, Resting heart rate in the app |
| Alerts | any entity and state | add in Configuration |

The sidebar page shows a live preview, what was found in Home Assistant, and the status of each source. **Update now** draws a frame right away.

## Ports

- `8090`: the frame, for the display (LAN only, no login, serves nothing else).
- The sidebar page is only reachable through Home Assistant.
