# FlightHome

FlightHome is a personal ADS-B flight observatory. It shows aircraft near a chosen location, estimates which aircraft may pass closest, and records locally collected observations for later review. It is designed to explain the sky around you rather than reproduce a global flight-tracking service.

Flight positions come from the public [adsb.lol API](https://www.adsb.lol/docs/open-data/api); route and aircraft metadata are enriched with [HexDB](https://hexdb.io/) when available. No API keys or third-party Python packages are required for the basic app.

## Requirements

- Python 3.9 or later
- Internet access for ADS-B and metadata feeds, map tiles, and browser libraries
- A modern browser for the interactive map

The Python application uses the standard library. The browser map loads Leaflet, Leaflet Rotate, and OpenStreetMap tiles. Onboard View can additionally load MapLibre GL JS and OpenFreeMap vector tiles for a dark 3D perspective with extruded OpenStreetMap buildings; these services require no API key or billing account.

## Quick start

```bash
python3 app.py --mappa
```

The map is served locally at `127.0.0.1`; the app prints the selected port and opens the page in your browser. To use the terminal view without a browser:

```bash
python3 app.py --lat 51.5072 --lon -0.1276
```

## Deploy on Render

The repository includes a `render.yaml` Blueprint for a single web service with a persistent disk mounted at `/var/data`. Connect the GitHub repository in Render and create the Blueprint. The service binds to Render's `PORT`, listens on `0.0.0.0`, and does not attempt to open a browser. `/health` is used for health checks. The starter plan is required by the included persistent disk configuration.

Location is optional at deployment time. Without coordinates, the service starts and a visitor can provide a browser GPS location. This location is global to the single service: the most recent visitor setting GPS changes the observer for every open browser, so this mode is suitable for a private/personal deployment. Do not treat it as private per-user location in a public multi-user deployment. For a shared installation with a fixed observer, set `FLIGHTHOME_LAT` and `FLIGHTHOME_LON` together in the Render environment. Treat this as a shared location visible to all visitors. Set `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` as Render secrets if OpenSky track history is needed.

SQLite history is written to `/var/data/flighthome.sqlite3` by the Blueprint. Do not remove the disk if the history must survive deployments and restarts. SQLite is suitable for this single-process service; a multi-instance deployment requires moving history and shared live state to a network database and coordinating the ADS-B polling worker. Render's ephemeral filesystem without a persistent disk loses local history on restart.

The map starts without a preset location. Select **USE GPS** in the browser or provide `--lat` and `--lon`; terminal mode requires both coordinates:

```bash
python3 app.py --lat 45.4642 --lon 9.1900 --mappa
```

## Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `--lat`, `--lon` | None | Observer latitude and longitude; provide both together. Required for terminal mode. |
| `--raggio` | `100` km | Initial map extent in kilometres; live search radius follows the visible map. |
| `--intervallo` | `15` seconds | Delay between ADS-B feed requests. Set to `0` for a one-shot terminal query; the map requires a positive interval. |
| `--mappa` | Off | Open the interactive browser map. |
| `--porta` | Automatic | Local HTTP port for the map; `0` selects a free port. |
| `--database` | `flighthome.sqlite3` | SQLite history file path. |
| `--storico-intervallo` | `60` seconds | How often to save an observation sample. |
| `--giorni-storico` | Deprecated | Accepted for backward compatibility and ignored; history is retained indefinitely. |

Example with a wider radius and a faster feed interval:

```bash
python3 app.py --lat 45.4642 --lon 9.1900 --raggio 150 --intervallo 10 --mappa
```

## Interactive map

The map shows live aircraft, flight search, browser alerts, aircraft details, and an aircraft-type legend. Search by callsign, registration, or ICAO address from the compact header field. The traffic and history sidebar can be hidden and restored; on phones it becomes a bottom sheet that can also be expanded or collapsed. Live ADS-B queries follow the map center and visible extent: pan to another area to load its flights, zoom out to include more airspace, and zoom in to focus nearby. The GPS observer marker stays fixed; flight distances, closest-pass predictions, and alerts remain relative to that observer. The public ADS-B request is capped at 250 nautical miles (about 463 km). At radii above 120 km, FlightHome limits the display to 120 flights prioritised by predicted closest pass and distance. The map waits briefly after zoom or pan before changing the requested radius. Aircraft markers use different silhouettes and colors for narrow-body and wide-body jets, superjumbos, regional aircraft, turboprops, helicopters, business aircraft, and military aircraft. Type code and callsign labels appear on hover or selection; click a marker to open its details.

The bottom detail panel can be minimized with **−**, restored with **＋**, or closed with **×**. The **DETAILS** map control can hide or reopen the panel. A single map status indicator shows feed availability, age of the latest ADS-B sample, configured polling interval, and observer area. Aircraft positions are estimated between feed samples for smoother movement; the flight details show when position movement is estimated.

### Aircraft perspective

Select an aircraft and choose **ONBOARD VIEW** to follow it, orient the map to its heading, and tilt the map for a navigation-style perspective. The observer location is marked **YOU**, with its distance and relative bearing from the aircraft. The zoom adapts to the separation between the aircraft and observer. If heading data is missing, the map stays north-up and reports that limitation. Use **OBSERVED AREA** to leave the perspective and return to your location.

Onboard View uses [MapLibre GL JS](https://maplibre.org/maplibre-gl-js/docs/) with the [OpenFreeMap](https://openfreemap.org/) dark vector style and [Mapterhorn](https://mapterhorn.com/data-access/) open terrain elevation tiles. Its camera is calculated from the selected aircraft's latest ADS-B position, reported altitude, and heading; the aircraft marker is raised above its ground projection. It marks **YOU**, draws their connecting line, shows mapped terrain in 3D, and extrudes buildings where OpenStreetMap data includes building heights. Position and altitude are estimates from the feed and are not flight-grade navigation data. These services need no API key, registration, or billing account. OpenFreeMap states that its public instance has no request or map-view limits, but provides the service as-is without an availability warranty. Terrain and building detail vary by area. This is a 3D vector map, not aerial/satellite imagery or a photorealistic world model. The map credits OpenStreetMap/OpenMapTiles and Mapterhorn. If WebGL is unavailable, FlightHome uses a CSS-tilted Leaflet view with the aircraft marker raised by its reported altitude; terrain and extruded buildings are unavailable in this fallback. Map rotation for the standard view uses the MIT-licensed [Leaflet Rotate](https://github.com/tomickigrzegorz/leaflet-rotate) plugin, loaded from a CDN.

### Flight tracks and estimates

Selecting an aircraft requests its available track from the experimental OpenSky Tracks endpoint. If that is unavailable, FlightHome uses locally recorded ADS-B samples. An airport sequence associated with a callsign is an indicative route and does not prove the aircraft's actual path.

OpenSky track access requires free OAuth client credentials. Set `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` before starting FlightHome, for example:

```bash
export OPENSKY_CLIENT_ID='your-client-id'
export OPENSKY_CLIENT_SECRET='your-client-secret'
python3 app.py --mappa
```

The request is made when an aircraft is selected, and the response is cached in SQLite for five minutes.

FlightHome estimates the closest point of approach (CPA) for up to 15 minutes using current position, speed, heading, and vertical rate. It is a linear projection: turns, changes in altitude, stale samples, and incomplete vectors can make the estimate inaccurate. No CPA is shown when the required vector data is missing.

### Personal sky observatory

The **YOUR SKY** card highlights the aircraft with the closest predicted pass when the telemetry supports a prediction. It shows the associated route, aircraft type, operator, and data freshness. Route metadata does not establish the actual route, and FlightHome does not detect or measure aircraft noise.

The **HOME SKY** view turns local history into summaries: observed sessions and routes, busiest hours, unusual-activity signals, and Business Intelligence breakdowns. Counts depend on local sampling, ADS-B coverage, and available metadata; they do not explain operational causes.

## Persistent local history

Whenever FlightHome runs, with or without the browser map, it records ADS-B samples in `flighthome.sqlite3` beside `app.py` by default. SQLite keeps the data across restarts, and FlightHome does not automatically prune or expose an in-app delete action for history. The default sampling interval is 60 seconds and can be configured:

```bash
python3 app.py --mappa --storico-intervallo 30
python3 app.py --mappa --database ~/Documents/my-flight-history.sqlite3
```

History starts when the application is run; no earlier data is backfilled. Existing databases are migrated additively. New fields are populated only by future samples. The database remains on the computer and is excluded from version control.

The **HOME SKY** view includes sample, observation, and distinct-aircraft counts; hourly activity; altitude, type, callsign, and direction summaries; a timeline replay; and Business Intelligence summaries for observed sessions, routes, operators, and aircraft models. It also reports scan continuity, median and p90 cadence, long gaps, fresh-position share, metadata completeness, ADS-B/MLAT counts, and whether the sample meets a baseline for period comparisons. The quality score is the equal-weight mean of continuity, fresh positions, and route, operator, and model coverage; it is a diagnostic summary, not a guarantee that the sample represents all air traffic. Airports and known operators are normalized to stable codes/keys during ingestion, and additive startup migrations backfill existing local records. Original labels and raw feed payloads remain available for traceability. CSV and JSON exports include normalized dimensions. The **SHARE 7-DAY BI** control creates a local PNG recap with sampled activity, observed distance inside the seven-day window, top routes, busiest observed hour, and metadata coverage. Select an aircraft and choose **SHARE PASS** to preview and download a sky snapshot with its available track and flight details. The observer's coordinates are omitted unless **Include my location** is explicitly checked.

Observed distance is the sum of locally sampled position-to-position segments; it is not the aircraft's full-flight distance. Planned route distance is an orthodromic estimate between known airports. A session is split after more than three minutes without samples or when its callsign changes, so gaps are not treated as observed travel. Sample counts are repeated detections, not unique flights. Metadata may be missing or incomplete.

Monitoring continuity counts time between successful scans only when the interval is at most three minutes, divided by elapsed time between the first and last scan in the selected window. Longer intervals count as gaps. A position is considered fresh at 30 seconds or less. Period comparisons are marked preliminary until the window covers at least five days, continuity is at least 70%, and there are no gaps over three minutes. These thresholds are operational guardrails, not proof of statistical representativeness. Historical map centers that were never stored cannot be reconstructed; migrations retain the observer coordinates as an explicitly labelled area proxy.

Stop FlightHome with `Ctrl+C` before copying the SQLite database for a full backup; SQLite uses WAL mode. The database can be opened with standard SQLite tools.

## Search and alerts

Search the live ADS-B feed by callsign, registration, or ICAO address from the header. Lookup uses the identifier directly and is independent of the current map viewport and nearby-flight radius. A single result centers the map on the aircraft; multiple matches are fitted in view and can be focused individually. Results remain subject to the aircraft having a current airborne ADS-B position.

Browser alerts can be enabled from **ALERTS** and configured by time and predicted distance in **SETTINGS**. They require browser notification permission and the page to remain open. Each aircraft can trigger at most one alert every 30 minutes. Alert settings are stored in the browser.

The browser asks for permission when **USE GPS** is selected. The selected observer coordinates are sent to the local FlightHome process, which uses them for its nearby feed request.

## Feed limits and data freshness

The ADS-B feed is queried every 15 seconds by default; change this with `--intervallo`. The browser reads the local snapshot once per second and smoothly estimates position and altitude from the latest available speed, heading, and vertical rate. Estimated movement is described in flight details, stops after 90 seconds without a fresh sample, and is not written to history.

If adsb.lol returns HTTP 429, FlightHome honors `Retry-After` with a minimum wait of 30 seconds. Repeated rate limits increase the backoff and live polling interval up to five minutes. The interval returns to its configured value after 30 minutes without a new 429. Feed limits vary and have no fixed public threshold; multiple running instances can contribute to rate limiting.

Aircraft on the ground, aircraft without a numeric altitude, and flights without both recognized airports are omitted from the nearby-flight list. Aircraft proximity does not mean the aircraft is directly overhead: FlightHome reports horizontal distance and reported altitude. Coverage and metadata depend on the public feeds.

## Data sources and acknowledgements

- [adsb.lol](https://www.adsb.lol/docs/open-data/api) — live aircraft positions
- [HexDB](https://hexdb.io/) — aircraft and route metadata when available
- [OpenStreetMap](https://www.openstreetmap.org/copyright) — map tiles
- [Leaflet](https://leafletjs.com/) and [Leaflet Rotate](https://github.com/tomickigrzegorz/leaflet-rotate) — browser mapping

Press `Ctrl+C` to stop the application and local map server.
