# FlightHome Product Roadmap

## Vision

A personal observatory for understanding aircraft traffic around the user's location: what an aircraft is, whether it is approaching, when it may pass closest, and how reliable the available data is. Location and preferences stay on the user's computer; traffic data comes from public ADS-B feeds.

## Product principles

- Answer “Which aircraft may pass near me, and when?” first.
- Keep observed data, indicative routes, and predictions distinct.
- Show the source and freshness of position data.
- Prefer free, local features and never promise coverage the feed cannot provide.
- Keep the map legible; details and alerts should help users decide what to inspect.

## Phase 1 — Personal radar

Status: **Complete — 5 November 2026**.

- [x] Estimate closest point of approach (CPA), predicted distance, and time to pass from ADS-B position, speed, and heading over a 15-minute horizon.
- [x] Show the prediction and estimated CPA altitude in the list, map, and flight details.
- [x] Add configurable browser alerts for predicted closest distance and maximum wait; keep thresholds and preference in the browser.
- [x] Show position source (ADS-B/MLAT), sample age, and whether a route is indicative.
- [x] Explain prediction limits: short-term linear projection is sensitive to turns, climb/descent, and missing updates.
- [x] Show airport sequence as an indicative planned route and distinguish it from locally recorded ADS-B tracks.

**Outcome:** prediction is available in both map and terminal views; alert thresholds persist locally; each aircraft triggers at most one notification every 30 minutes; source and freshness indicators are visible; no prediction is shown when speed or heading is missing.

## Phase 2 — Home sky history

Status: **Complete — 5 November 2026**.

- [x] Store local track samples in SQLite with configurable sampling and retention intervals, plus manual deletion from the map.
- [x] Add timeline replay and summaries by hour, altitude, type, callsign, and direction.
- [x] Highlight unusual activity relative to previous days as statistical signals that need interpretation.
- [x] Export history as CSV and JSON.
- [x] Enrich samples with route, airline code, operator/owner, model, manufacturer, estimated route distance, and raw ADS-B payload.
- [x] Aggregate observed sessions with local distance, maximum/minimum altitude, and average speed; add BI summaries and separate session export.
- [x] Migrate the SQLite schema additively and set default retention to 30 days.

**Outcome:** local history can be limited and cleared; replay and statistics state their time period and what they count. Observed distances represent locally sampled segments; airport-to-airport distances are orthodromic estimates. **Home Sky** provides BI summaries and separate exports for samples and sessions; SQLite stays on the user's computer.

## Phase 3 — Airport traffic context

Status: **Complete — 5 November 2026**.

- [x] Add nearby airports and runway geometry from OpenStreetMap via Overpass, falling back to public OurAirports CSV files; cache results in memory for six hours and show the source.
- [x] Highlight likely approaches and departures using ADS-B vectors, filterable by altitude and direction.
- [x] Add quick views for nearby airports, with map selection and centering.
- [x] Explain classification criteria and show confidence.

**Outcome:** the map distinguishes airport infrastructure, ADS-B positions, and estimated classifications. A flow is shown only when at least three aircraft in the live view are consistent with the same airport and direction; a single aircraft remains an unconfirmed trajectory. Confidence is high when runway alignment and vertical data support the classification, otherwise medium.

## Phase 4 — Live flight search

Status: **Complete — 5 November 2026**.

- [x] Search the live feed by callsign, registration, or ICAO address.
- [x] Show position, altitude, speed, heading, and distance from the observer.
- [x] Center the map and open result details, including for aircraft outside the local radius.
- [x] Run search requests only when submitted, without adding another polling loop.

**Outcome:** search uses adsb.lol live endpoints. Results remain subject to ADS-B coverage and are refreshed by submitting the query again.

## Phase 5 — Reduced polling and interpolated motion

Status: **Complete — 5 November 2026**.

- [x] Set default ADS-B polling to 15 seconds while keeping `--intervallo` configurable.
- [x] Interpolate browser position and altitude from telemetry between samples without writing projections to history.
- [x] Clearly mark projected positions and stop projection after 90 seconds without fresh data.
- [x] Reject a zero interval in map mode to prevent continuous requests.

**Outcome:** markers and radar indicators move smoothly between samples; the configured interval and HTTP 429 backoff determine actual feed request frequency.

## Phase 6 — Personal sky observatory

Status: **Complete — 5 November 2026**.

- [x] Add a live “Your Sky” card highlighting the aircraft with the closest predicted pass and linking to its details and track.
- [x] Summarize associated route, type, altitude, operator, and data freshness; clarify that a route association does not prove the actual path and that FlightHome does not detect sound.
- [x] Create a readable diary of observed sessions and routes, busiest hour, and comparison with the preceding window of equal duration.
- [x] Present comparisons as local archive statistics and explain when there is not enough prior data.

**Outcome:** the live view answers “Which aircraft may pass closest to me?” when telemetry supports a prediction; the diary makes historical patterns easier to inspect. Counts and comparisons depend on ADS-B coverage, sampling interval, and available metadata; they do not explain operational causes or estimate noise.

## Phase 7 — Aircraft perspective

Status: **Complete — 5 November 2026**.

- [x] Add a heading-up view that follows a selected aircraft using projected telemetry.
- [x] Mark the observer location as “YOU”, with a connecting line, distance, and direction from the aircraft.
- [x] Update the view from interpolated ADS-B samples and report when heading is unavailable.
- [x] Add a control to return to north-up and the observed area.

**Outcome:** **Onboard View** follows the aircraft, rotates and tilts the map according to heading, keeps the observer location visible, and closes the detail panel. Asynchronous track loading no longer changes the map framing. It uses OpenStreetMap road tiles with a perspective effect, not Google Maps satellite imagery or 3D terrain. Positions and distances remain estimates based on the latest samples.

## Phase 8 — Recognizable aircraft-type icons

Status: **Complete — 5 November 2026**.

- [x] Draw distinct silhouettes for airliners, widebodies, superjumbos, regional aircraft, turboprops, helicopters, business/light aircraft, and military aircraft.
- [x] Classify by ICAO aircraft type code first and use model metadata as a fallback.
- [x] Show type code and callsign on markers and full details on hover.
- [x] Reuse icons in live map, search, and history replay.
- [x] Keep type/callsign labels readable on hover or selection while leaving markers clickable.

**Outcome:** shape and color distinguish aircraft families at a glance; exact identification depends on feed metadata quality.

## Future ideas

- Favorites for callsign, registration, airline, and aircraft type.
- Quiet/night mode, keyboard shortcuts, and a compact always-on display layout.
- Optional local ADS-B receiver input to improve coverage and latency.

## Risks and dependencies

- Community feeds have variable coverage, latency, and request limits.
- Routes associated with callsigns may be scheduled or outdated and do not certify the actual itinerary.
- Linear prediction loses accuracy when an aircraft turns or changes altitude.
- Browser alerts require the page to remain open and permission to be granted; persistent system notifications would require a future installable app.
- ADS-B archives must be retained in accordance with the sources' licenses and terms.
