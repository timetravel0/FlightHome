# FlightHome

FlightHome is a personal ADS-B flight observatory. It shows aircraft near a chosen location, estimates which aircraft may pass closest, and records locally collected observations for later review. It is designed to explain the sky around you rather than reproduce a global flight-tracking service.

Flight positions come from the public [adsb.lol API](https://www.adsb.lol/docs/open-data/api); route and aircraft metadata are enriched with [HexDB](https://hexdb.io/) when available. No API keys or third-party Python packages are required for the basic app.

## Requirements

- Python 3.9 or later
- Internet access for ADS-B and metadata feeds, map tiles, and browser libraries
- A modern browser for the interactive map

The Python application uses the standard library. The browser map loads Leaflet, Leaflet Rotate, and OpenStreetMap tiles from their public CDNs/services.

## Quick start

```bash
python3 app.py --mappa
```

The map is served locally at `127.0.0.1`; the app prints the selected port and opens the page in your browser. To use the terminal view without a browser:

```bash
python3 app.py --lat 51.5072 --lon -0.1276
```

The map starts without a preset location. Select **USE GPS** in the browser or provide `--lat` and `--lon`; terminal mode requires both coordinates:

```bash
python3 app.py --lat 45.4642 --lon 9.1900 --mappa
```

## Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `--lat`, `--lon` | None | Observer latitude and longitude; provide both together. Required for terminal mode. |
| `--raggio` | `100` km | Search radius. |
| `--intervallo` | `15` seconds | Delay between ADS-B feed requests. Set to `0` for a one-shot terminal query; the map requires a positive interval. |
| `--mappa` | Off | Open the interactive browser map. |
| `--porta` | Automatic | Local HTTP port for the map; `0` selects a free port. |
| `--database` | `flighthome.sqlite3` | SQLite history file path. |
| `--storico-intervallo` | `60` seconds | How often to save an observation sample. |
| `--giorni-storico` | `30` days | History retention period. |

Example with a wider radius and a faster feed interval:

```bash
python3 app.py --lat 45.4642 --lon 9.1900 --raggio 150 --intervallo 10 --mappa
```

## Interactive map

The map includes live aircraft, a local radar display, airport infrastructure, flight search, browser alerts, aircraft details, and an aircraft-type legend. Aircraft markers use different silhouettes and colors for narrow-body and wide-body jets, superjumbos, regional aircraft, turboprops, helicopters, business aircraft, and military aircraft. Type code and callsign labels appear on hover or selection; click a marker to open its details.

The bottom detail panel can be minimized with **−**, restored with **＋**, or closed with **×**. The **DETAILS** map control can hide or reopen the panel. The **LIVE ADS-B** indicator shows the timestamp and age of the latest received feed sample, along with the configured polling interval. Browser animation between samples is identified as projected position and is not presented as a new feed update.

### Aircraft perspective

Select an aircraft and choose **ONBOARD VIEW** to follow it, orient the map to its heading, and tilt the map for a navigation-style perspective. The observer location is marked **YOU**, with its distance and relative bearing from the aircraft. The zoom adapts to the separation between the aircraft and observer. If heading data is missing, the map stays north-up and reports that limitation. Use **OBSERVED AREA** to leave the perspective and return to your location.

The basemap uses OpenStreetMap road tiles with a perspective effect. It is not Google Maps, satellite imagery, or a terrain-rendered 3D view. Map rotation and pitch use the MIT-licensed [Leaflet Rotate](https://github.com/tomickigrzegorz/leaflet-rotate) plugin, loaded from a CDN.

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

The **HOME SKY** view turns local history into summaries: observed sessions and routes, busiest hours, unusual-activity signals, and a comparison with the preceding period of equal length. Counts depend on local sampling, ADS-B coverage, and available metadata; they do not explain operational causes.

### Airports

The airport infrastructure panel can show nearby airports and runways, center the map on an airport, and filter likely traffic flows. Airport data is queried from OpenStreetMap via Overpass, with public-domain [OurAirports](https://ourairports.com/data/) CSV files as a fallback. Results are cached in memory for six hours, and the active source is shown in the panel.

Likely arrivals and departures are contextual estimates based on aircraft heading, position, altitude, vertical rate, and runway alignment. A flow is shown only when at least three aircraft are consistent with the same airport and direction. Confidence is higher when heading aligns with a runway and vertical rate supports the classification. These are not operational facts and do not prove that an aircraft is using a particular runway.

## Persistent local history

Whenever FlightHome runs, with or without the browser map, it records ADS-B samples in `flighthome.sqlite3` beside `app.py` by default. SQLite keeps the data across restarts. The default sampling interval is 60 seconds and the default retention period is 30 days. Both can be configured:

```bash
python3 app.py --mappa --storico-intervallo 30 --giorni-storico 14
python3 app.py --mappa --database ~/Documents/my-flight-history.sqlite3
```

History starts when the application is run; no earlier data is backfilled. Existing databases are migrated additively. New fields are populated only by future samples. The database remains on the computer and is excluded from version control.

The **HOME SKY** view includes sample, observation, and distinct-aircraft counts; hourly activity; altitude, type, callsign, and direction summaries; a timeline replay; and Business Intelligence summaries for observed sessions, routes, operators, and aircraft models. CSV and JSON exports are available for observations, and a separate CSV export is available for flight sessions. The history can be cleared from the map after confirmation.

Observed distance is the sum of locally sampled position-to-position segments; it is not the aircraft's full-flight distance. Planned route distance is an orthodromic estimate between known airports. A session is split after more than three minutes without samples or when its callsign changes, so gaps are not treated as observed travel. Sample counts are repeated detections, not unique flights. Metadata may be missing or incomplete.

Stop FlightHome with `Ctrl+C` before copying the SQLite database for a full backup; SQLite uses WAL mode. The database can be opened with standard SQLite tools.

## Search and alerts

Search the live ADS-B feed by callsign, registration, or ICAO address from the LIVE panel. Search runs only when submitted; it does not add another polling loop. Results can be outside the local search radius and remain subject to ADS-B coverage.

Browser alerts can be enabled from **ALERTS** and configured by time and predicted distance in **SETTINGS**. They require browser notification permission and the page to remain open. Each aircraft can trigger at most one alert every 30 minutes. Alert settings are stored in the browser.

The browser asks for permission when **USE GPS** is selected. The selected observer coordinates are sent to the local FlightHome process, which uses them for its nearby feed request.

## Feed limits and data freshness

The ADS-B feed is queried every 15 seconds by default; change this with `--intervallo`. The browser reads the local snapshot once per second and smoothly projects position and altitude from the latest available speed, heading, and vertical rate. The projection is marked as estimated, stops after 90 seconds without a fresh sample, and is not written to history.

If adsb.lol returns HTTP 429, FlightHome honors `Retry-After` with a minimum wait of 30 seconds. Repeated rate limits increase the backoff and live polling interval up to five minutes. The interval returns to its configured value after 30 minutes without a new 429. Feed limits vary and have no fixed public threshold; multiple running instances can contribute to rate limiting.

Aircraft on the ground, aircraft without a numeric altitude, and flights without both recognized airports are omitted from the nearby-flight list. Aircraft proximity does not mean the aircraft is directly overhead: FlightHome reports horizontal distance and reported altitude. Coverage and metadata depend on the public feeds.

## Data sources and acknowledgements

- [adsb.lol](https://www.adsb.lol/docs/open-data/api) — live aircraft positions
- [HexDB](https://hexdb.io/) — aircraft and route metadata when available
- [OpenStreetMap](https://www.openstreetmap.org/copyright) and Overpass — map tiles and airport infrastructure
- [OurAirports](https://ourairports.com/data/) — airport-data fallback
- [Leaflet](https://leafletjs.com/) and [Leaflet Rotate](https://github.com/tomickigrzegorz/leaflet-rotate) — browser mapping

Press `Ctrl+C` to stop the application and local map server.
