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

Status: **Complete — 5 November 2026; free vector 3D view added 6 November 2026**.

- [x] Estimate closest point of approach (CPA), predicted distance, and time to pass from ADS-B position, speed, and heading over a 15-minute horizon.
- [x] Show the prediction and estimated CPA altitude in the list, map, and flight details.
- [x] Add configurable browser alerts for predicted closest distance and maximum wait; keep thresholds and preference in the browser.
- [x] Show position source (ADS-B/MLAT), sample age, and whether a route is indicative.
- [x] Explain prediction limits: short-term linear projection is sensitive to turns, climb/descent, and missing updates.
- [x] Show airport sequence as an indicative planned route and distinguish it from locally recorded ADS-B tracks.

**Outcome:** prediction is available in both map and terminal views; alert thresholds persist locally; each aircraft triggers at most one notification every 30 minutes; source and freshness indicators are visible; no prediction is shown when speed or heading is missing.

## Phase 2 — Home sky history

Status: **Complete — 5 November 2026**.

- [x] Store local track samples in SQLite with a configurable sampling interval and indefinite retention.
- [x] Add timeline replay and summaries by hour, altitude, type, callsign, and direction.
- [x] Highlight unusual activity relative to previous days as statistical signals that need interpretation.
- [x] Export history as CSV and JSON.
- [x] Enrich samples with route, airline code, operator/owner, model, manufacturer, estimated route distance, and raw ADS-B payload.
- [x] Aggregate observed sessions with local distance, maximum/minimum altitude, and average speed; add BI summaries and separate session export.
- [x] Migrate the SQLite schema additively and retain archived records indefinitely.

**Outcome:** local history persists across restarts and is never pruned or deleted by FlightHome. Replay and statistics state their time period and what they count. Observed distances represent locally sampled segments; airport-to-airport distances are orthodromic estimates. **Home Sky** provides BI summaries and separate exports for samples and sessions; SQLite stays on the user's computer.

## Phase 3 — Airport traffic context (retired)

Status: **Retired — 6 November 2026**. The infrastructure panel, runway overlays, and estimated arrival/departure flows were removed during interface simplification. Airport names and route metadata remain available on flights.

- [x] Add nearby airports and runway geometry from OpenStreetMap via Overpass, falling back to public OurAirports CSV files; cache results in memory for six hours and show the source.
- [x] Highlight likely approaches and departures using ADS-B vectors, filterable by altitude and direction.
- [x] Add quick views for nearby airports, with map selection and centering.
- [x] Explain classification criteria and show confidence.

**Historical outcome:** the map previously distinguished airport infrastructure, ADS-B positions, and estimated arrival/departure flows. These features are no longer part of the product interface.

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
- [x] Disclose estimated movement in flight details and stop projection after 90 seconds without fresh data.
- [x] Reject a zero interval in map mode to prevent continuous requests.

**Outcome:** map markers move smoothly between samples; the configured interval and HTTP 429 backoff determine actual feed request frequency. Interpolation is labelled in flight details, while repeated per-row labels and the standalone radar overlay are omitted.

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
- [x] Add optional MapLibre/OpenFreeMap dark vector map with extruded OpenStreetMap buildings, aircraft marker, observer marker, and a connecting line; retain the standard map fallback.

**Outcome:** **Onboard View** places the MapLibre camera at the aircraft's reported ADS-B altitude and position, looks along its heading, raises the aircraft marker above the ground projection, and renders mapped terrain and extruded buildings using free, keyless data services. If WebGL is unavailable, it uses a CSS-tilted Leaflet view and an altitude-offset aircraft marker, without 3D terrain/buildings. Altitude and position remain feed estimates; building and terrain coverage vary, and neither mode is satellite imagery.

## Phase 8 — Recognizable aircraft-type icons

Status: **Complete — 5 November 2026**.

- [x] Draw distinct silhouettes for airliners, widebodies, superjumbos, regional aircraft, turboprops, helicopters, business/light aircraft, and military aircraft.
- [x] Classify by ICAO aircraft type code first and use model metadata as a fallback.
- [x] Show type code and callsign on markers and full details on hover.
- [x] Reuse icons in live map, search, and history replay.
- [x] Keep type/callsign labels readable on hover or selection while leaving markers clickable.

**Outcome:** shape and color distinguish aircraft families at a glance; exact identification depends on feed metadata quality.

## Phase 9 — Shareable BI and flight snapshots

Status: **Complete**.

- [x] Add a shareable **Your Sky · Last 7 Days** recap using local SQLite history and a rolling seven-day window.
- [x] Summarize observed sessions, aircraft, observations, sampled distance within the selected window, top routes, busiest observed hour, and metadata coverage.
- [x] Generate a downloadable PNG locally in the browser; state that counts reflect local sampling and receiver coverage.
- [x] Add a **Sky Snapshot** export for a selected or searched aircraft with its available track, route metadata, observation time, aircraft details, and CPA estimate when available.
- [x] Distinguish recorded ADS-B/local tracks from callsign-associated indicative routes and calculated estimates.
- [x] Exclude observer coordinates by default and include them only after an explicit opt-in.

**Outcome:** FlightHome turns local observations into a clear, privacy-aware BI recap and shareable aircraft encounter card. No flight path or global coverage is inferred when the stored/feed data is missing.

## Phase 10 — Focused, lower-overhead map interface

Status: **Complete — 6 November 2026**.

- [x] Remove the standalone animated radar overlay and its duplicate aircraft indicators.
- [x] Consolidate live feed age, polling interval, observer area, and rate-limit state into one map status indicator.
- [x] Remove repeated projected-position labels while retaining the movement estimate in selected flight details.
- [x] Remove nearby-airport infrastructure, runway overlays, and estimated flow calculations and their network endpoints; keep airport route metadata.
- [x] Add a persistent sidebar hide/show control and move flight search to a compact header field without explanatory copy.
- [x] Reduce telemetry-driven marker mutations from up to 30 frames per second to 10 frames per second, and suspend map updates while the document is hidden.

**Outcome:** the live map gives more space to aircraft, avoids redundant airport/radar layers, and exposes its current data freshness and polling state in one place.

## Phase 11 — Data reliability and normalized BI

Status: **Core implemented — 6 November 2026**.

- [x] Normalize airport dimensions to stable IATA/ICAO codes and operator names/keys at live ingestion.
- [x] Backfill normalized airport and operator keys in existing SQLite observations and sessions; retain original labels and raw ADS-B payloads.
- [x] Store query radius and distinguish recorded map centers from observer-coordinate fallbacks in history.
- [x] Report monitoring continuity, scan cadence, long gaps, fresh-position share, ADS-B/MLAT counts, metadata completeness, and a documented quality score.
- [x] Mark temporal comparisons preliminary until minimum duration, continuity, and gap criteria are met.
- [x] Apply normalized dimensions to BI grouping and CSV/JSON exports.
- [ ] Add a canonical airport/operator alias registry with provenance and versioning as mappings expand.
- [ ] Add quality checks for implausible telemetry and outlier position jumps before calculating observed distance.
- [ ] Build day-over-day and week-over-week comparisons only for equivalent geographic coverage and comparable sampling completeness.

**Outcome:** BI reports describe the collected local sample alongside its coverage and data quality. Normalization runs for future data and is backfilled for existing records; missing historical map centers remain explicitly identified as observer-coordinate proxies.


## Deployment architecture — Render


Status: **Implemented for a single-instance Render service**.

- [x] Bind the map server to `0.0.0.0` and Render's injected `PORT`; keep browser auto-open local-only.
- [x] Start HTTP health checks independently from the ADS-B polling worker.
- [x] Persist SQLite under the mounted `/var/data` disk and configure location and optional OpenSky secrets through environment variables.
- [x] Add a Render Blueprint, `/health` endpoint, and deployment documentation.
- [ ] Move state to a network database and coordinate ingestion before running multiple web instances.

**Constraint:** each browser GPS update changes the observer for the entire service. Public deployments should use a fixed observer or add per-user sessions/authentication before supporting private locations.

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


### Live map query area

- [x] Recenter ADS-B queries and the visible flight set when the map is panned or zoomed.
- [x] Keep observer coordinates separate for distance and closest-pass calculations.
- [x] Store both observer and map query coordinates in SQLite history and exports.
