# E-paper Dashboard for Home Assistant

A Home Assistant app that draws an office dashboard for the Waveshare 10.85" (G) color e-paper panel (1360×480, black/white/yellow/red) and serves it to an ESP32-C6 display client.

- Today (calendar, flip clock, packages, alerts), Weather + indoor air, Markets, Health + 3D printer
- Renders HTML with headless Chromium, snaps it to the panel's 4 colors, and serves a ready-to-send `.epd` frame on port 8090 with ETags
- One refresh beat ("Refresh every N minutes"), on round times; the display is told when to check in so updates finish right on the minute
- Finds Airthings, Bambu Lab, Mail and Packages, and Companion app Health Connect sensors automatically

## Install

1. In Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**, add `https://github.com/arqfernandolima-rgb/ha-epaper-dashboard`.
2. Install **E-paper Dashboard**, start it, and turn on **Show in sidebar**.
3. Point the display at `http://<HA address>:8090/frame.epd`.

See [the app's docs](epaper_dashboard/DOCS.md) for settings and data sources.

Fonts: Barlow (SIL Open Font License). Icons: Lucide (ISC).
