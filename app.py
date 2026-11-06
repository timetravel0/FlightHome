#!/usr/bin/env python3
"""Show nearby ADS-B aircraft using the public adsb.lol API."""

import argparse
import csv
import email.utils
import io
import json
import math
import os
import re
import statistics
import sqlite3
import threading
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"
ROUTE_API = "https://hexdb.io/api/v2/routes/{callsign}"
route_cache = {}
aircraft_cache = {}
UNSET_LOCATION = (0.0, 0.0)
RESET = "\033[0m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
DIM = "\033[2m"
COUNTRY_NAMES = {
    "AE": "United Arab Emirates", "AU": "Australia", "BE": "Belgium", "BR": "Brazil",
    "CA": "Canada", "CH": "Switzerland", "CN": "China", "DE": "Germany",
    "DK": "Denmark", "EG": "Egypt", "ES": "Spain", "FI": "Finland",
    "FR": "France", "GB": "United Kingdom", "GR": "Greece", "HK": "Hong Kong",
    "IE": "Ireland", "IN": "India", "IT": "Italy", "JP": "Japan",
    "KE": "Kenya", "KR": "South Korea", "MA": "Morocco", "MX": "Mexico",
    "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand", "PT": "Portugal",
    "QA": "Qatar", "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore",
    "TH": "Thailand", "TR": "Turkey", "TW": "Taiwan", "US": "United States",
    "ZA": "South Africa",
}
AIRLINE_BY_ICAO = {
    "BAW": "British Airways", "EZY": "easyJet", "RYR": "Ryanair",
    "VIR": "Virgin Atlantic", "UAL": "United Airlines", "AAL": "American Airlines",
    "DLH": "Lufthansa", "EIN": "Aer Lingus", "SWR": "Swiss International Air Lines",
    "ACA": "Air Canada", "DAL": "Delta Air Lines", "EXS": "Jet2",
    "TOM": "TUI Airways", "KLM": "KLM", "AFR": "Air France",
    "IBE": "Iberia", "SAS": "Scandinavian Airlines", "TAP": "TAP Air Portugal",
    "WZZ": "Wizz Air", "LOG": "Loganair", "EJU": "easyJet Europe",
}
AIRLINE_ALIASES = {
    "british airways plc": "British Airways", "british airways": "British Airways", "baw": "British Airways",
    "easyjet uk": "easyJet", "easyjet airline company limited": "easyJet", "easyjet": "easyJet",
    "ryanair uk": "Ryanair", "ryanair dac": "Ryanair", "ryanair": "Ryanair",
    "virgin atlantic airways": "Virgin Atlantic", "virgin atlantic": "Virgin Atlantic",
    "united airlines inc": "United Airlines", "united airlines": "United Airlines",
    "american airlines inc": "American Airlines", "american airlines": "American Airlines",
    "delta air lines inc": "Delta Air Lines", "delta air lines": "Delta Air Lines",
    "lufthansa german airlines": "Lufthansa", "lufthansa": "Lufthansa",
}
latest_snapshot = {"lat": UNSET_LOCATION[0], "lon": UNSET_LOCATION[1], "view_lat": UNSET_LOCATION[0], "view_lon": UNSET_LOCATION[1], "radius": 100, "flights": [], "updated": None, "position_snapshot_at": None, "location_configured": False}
active_location = {"lat": UNSET_LOCATION[0], "lon": UNSET_LOCATION[1], "configured": False}
active_view = {"lat": UNSET_LOCATION[0], "lon": UNSET_LOCATION[1], "radius": 100.0, "requested_at": 0.0}
location_lock = threading.Lock()
MAX_RADAR_RADIUS_KM = 463.0  # adsb.lol's 250 NM request limit
RADAR_DENSE_RADIUS_KM = 120.0
RADAR_MAX_FLIGHTS = 120
history_path = None


def normalize_code(value):
    value = str(value or "").strip().upper()
    return value if value and value not in {"N/A", "UNKNOWN", "NONE"} else None


def normalize_airport_code(label, route_json=None, end=None):
    if route_json:
        try:
            stops = json.loads(route_json)
            stop = (stops[0] if end == "origin" else stops[-1]) if stops else {}
            code = normalize_code(stop.get("iata") or stop.get("icao"))
            if code:
                return code
        except (TypeError, ValueError, AttributeError):
            pass
    label = str(label or "").strip()
    if not label or label.upper() in {"N/A", "UNKNOWN"}:
        return None
    match = re.match(r"^\s*([A-Za-z0-9]{3,4})(?:\s|$|\()", label)
    return match.group(1).upper() if match else None


def normalize_operator(company, airline_code=None, callsign=None):
    code = normalize_code(airline_code) or normalize_code(str(callsign or "")[:3])
    if code in AIRLINE_BY_ICAO:
        display = AIRLINE_BY_ICAO[code]
    else:
        display = " ".join(str(company or "").split()).strip() or None
        if display:
            display = AIRLINE_ALIASES.get(display.casefold(), display)
    if not display:
        return None, None
    key = re.sub(r"[^a-z0-9]+", " ", display.casefold()).strip()
    return key or None, display


def open_history(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY,
            captured_at INTEGER NOT NULL,
            observer_lat REAL NOT NULL,
            observer_lon REAL NOT NULL,
            view_lat REAL,
            view_lon REAL,
            view_center_source TEXT NOT NULL DEFAULT 'legacy_unknown',
            query_radius_km REAL,
            feed_status TEXT NOT NULL DEFAULT 'ok',
            radius_km REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS scans_captured_at ON scans(captured_at);
        CREATE TABLE IF NOT EXISTS observations (
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            hex TEXT NOT NULL,
            callsign TEXT,
            registration TEXT,
            aircraft_type TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            altitude_ft REAL,
            speed_kt REAL,
            heading_deg REAL,
            vertical_rate_fpm REAL,
            squawk TEXT,
            position_source TEXT NOT NULL,
            position_age_s REAL,
            origin TEXT,
            destination TEXT,
            route_airports_json TEXT,
            airline_code TEXT,
            company TEXT,
            origin_code TEXT,
            destination_code TEXT,
            operator_key TEXT,
            aircraft_model TEXT,
            manufacturer TEXT,
            planned_route_km REAL,
            session_distance_km REAL,
            session_id INTEGER,
            raw_json TEXT,
            PRIMARY KEY(scan_id, hex)
        );
        CREATE INDEX IF NOT EXISTS observations_hex ON observations(hex, scan_id);
        CREATE TABLE IF NOT EXISTS flight_sessions (
            id INTEGER PRIMARY KEY,
            hex TEXT NOT NULL,
            callsign TEXT,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            origin TEXT,
            destination TEXT,
            route_airports_json TEXT,
            airline_code TEXT,
            company TEXT,
            origin_code TEXT,
            destination_code TEXT,
            operator_key TEXT,
            registration TEXT,
            aircraft_type TEXT,
            aircraft_model TEXT,
            manufacturer TEXT,
            planned_route_km REAL,
            observed_distance_km REAL NOT NULL DEFAULT 0,
            track_points INTEGER NOT NULL DEFAULT 0,
            max_altitude_ft REAL,
            min_altitude_ft REAL,
            speed_sum_kt REAL NOT NULL DEFAULT 0,
            speed_samples INTEGER NOT NULL DEFAULT 0,
            last_lat REAL,
            last_lon REAL
        );
        CREATE INDEX IF NOT EXISTS flight_sessions_time ON flight_sessions(first_seen, last_seen);
        CREATE INDEX IF NOT EXISTS flight_sessions_route ON flight_sessions(origin, destination);
        CREATE INDEX IF NOT EXISTS flight_sessions_hex_last_seen ON flight_sessions(hex, last_seen DESC);
        CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS external_tracks (
            hex TEXT NOT NULL,
            flight_time INTEGER NOT NULL,
            fetched_at INTEGER NOT NULL,
            points_json TEXT NOT NULL,
            PRIMARY KEY(hex, flight_time)
        );
    """)
    # Additive migrations keep existing personal SQLite archives usable.
    scan_columns = {row[1] for row in connection.execute("PRAGMA table_info(scans)")}
    for name in ("view_lat", "view_lon"):
        if name not in scan_columns:
            connection.execute(f"ALTER TABLE scans ADD COLUMN {name} REAL")
    scan_columns = {row[1] for row in connection.execute("PRAGMA table_info(scans)")}
    for name, declaration in (("query_radius_km", "REAL"), ("feed_status", "TEXT NOT NULL DEFAULT 'ok'")):
        if name not in scan_columns:
            connection.execute(f"ALTER TABLE scans ADD COLUMN {name} {declaration}")
    if "view_center_source" not in scan_columns:
        connection.execute("ALTER TABLE scans ADD COLUMN view_center_source TEXT NOT NULL DEFAULT 'legacy_unknown'")
    for table in ("observations", "flight_sessions"):
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name in ("origin_code", "destination_code", "operator_key"):
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}
    additions = {
        "origin": "TEXT", "destination": "TEXT", "route_airports_json": "TEXT",
        "airline_code": "TEXT", "company": "TEXT", "aircraft_model": "TEXT",
        "manufacturer": "TEXT", "planned_route_km": "REAL", "session_distance_km": "REAL",
        "session_id": "INTEGER", "raw_json": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE observations ADD COLUMN {name} {declaration}")
    # Backfill normalized dimensions once; legacy viewport centers that were not
    # recorded cannot be reconstructed and stay explicitly marked as proxies.
    connection.execute("UPDATE scans SET view_center_source='recorded' WHERE view_lat IS NOT NULL AND view_lon IS NOT NULL AND view_center_source='legacy_unknown'")
    connection.execute("UPDATE scans SET view_lat=observer_lat, view_lon=observer_lon, view_center_source='observer_fallback' WHERE view_lat IS NULL OR view_lon IS NULL")
    migration = connection.execute(
        "SELECT 1 FROM app_migrations WHERE name='normalized_bi_dimensions_v1'"
    ).fetchone()
    if not migration:
        for table in ("observations", "flight_sessions"):
            rows = connection.execute(
                f"SELECT rowid, origin, destination, route_airports_json, airline_code, company, callsign FROM {table}"
            ).fetchall()
            for row in rows:
                operator_key, normalized_company = normalize_operator(row[5], row[4], row[6])
                connection.execute(
                    f"UPDATE {table} SET origin_code=COALESCE(?, origin_code), destination_code=COALESCE(?, destination_code), operator_key=?, company=COALESCE(?, company) WHERE rowid=?",
                    (normalize_airport_code(row[1], row[3], "origin"),
                     normalize_airport_code(row[2], row[3], "destination"), operator_key,
                     normalized_company, row[0]),
                )
        connection.execute(
            "INSERT INTO app_migrations(name, applied_at) VALUES ('normalized_bi_dimensions_v1', ?)",
            (int(time.time()),),
        )
    connection.execute("CREATE INDEX IF NOT EXISTS flight_sessions_route_code ON flight_sessions(origin_code, destination_code)")
    connection.execute("CREATE INDEX IF NOT EXISTS flight_sessions_operator_key ON flight_sessions(operator_key)")
    connection.commit()
    return connection


def save_history(connection, aircraft, flights, lat, lon, radius, view_lat=None, view_lon=None):
    captured_at = int(time.time())
    with connection:
        cursor = connection.execute(
            "INSERT INTO scans(captured_at, observer_lat, observer_lon, view_lat, view_lon, view_center_source, query_radius_km, radius_km) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (captured_at, lat, lon, view_lat if view_lat is not None else lat,
             view_lon if view_lon is not None else lon, "recorded" if view_lat is not None and view_lon is not None else "observer_fallback", radius, radius),
        )
        scan_id = cursor.lastrowid
        records = []
        flight_by_hex = {str(flight.get("hex", "")).lower(): flight for flight in flights}
        for plane in aircraft:
            hex_code = (plane.get("hex") or "").strip().lower()
            plat, plon = plane.get("lat"), plane.get("lon")
            altitude = plane.get("alt_baro")
            if altitude == "ground":
                continue
            if not isinstance(altitude, (int, float)):
                altitude = plane.get("alt_geom")
            if not isinstance(altitude, (int, float)) or not math.isfinite(altitude):
                altitude = None
            if (not hex_code or not isinstance(plat, (int, float)) or not isinstance(plon, (int, float))
                    or not math.isfinite(plat) or not math.isfinite(plon)
                    or not -90 <= plat <= 90 or not -180 <= plon <= 180):
                continue
            position_age = plane.get("seen_pos")
            if not isinstance(position_age, (int, float)):
                position_age = plane.get("seen")
            source = "MLAT" if plane.get("type") == "mlat" or "lat" in (plane.get("mlat") or []) else "ADS-B"
            flight = flight_by_hex.get(hex_code, {})
            callsign = (plane.get("flight") or "").strip() or None
            route_info = route_cache.get(callsign) if callsign else None
            route = flight.get("route_airports") or (route_airports(route_info) if route_info else [])
            route_json = json.dumps(route, ensure_ascii=False) if route else None
            aircraft_info = aircraft_cache.get(hex_code) or {}
            origin = flight.get("origin") or (airport_label(route_info.get("origin")) if route_info else None)
            destination = flight.get("destination") or (airport_label(route_info.get("destination")) if route_info else None)
            airline_code = flight.get("airline_code") or (callsign[:3].upper() if callsign and callsign[:3].isalpha() else None)
            raw_company = flight.get("company") or plane.get("ownOp") or aircraft_info.get("registered_owner") or aircraft_info.get("operator")
            operator_key, company = normalize_operator(raw_company, airline_code, callsign)
            origin_code = normalize_airport_code(origin, route_json, "origin")
            destination_code = normalize_airport_code(destination, route_json, "destination")
            manufacturer = flight.get("manufacturer") or aircraft_info.get("manufacturer")
            model = flight.get("model") or plane.get("desc") or aircraft_info.get("model") or aircraft_info.get("type")
            registration = flight.get("registration") or plane.get("r") or aircraft_info.get("registration")
            aircraft_type = flight.get("aircraft_type") or plane.get("t") or aircraft_info.get("type")
            planned_route_km = flight.get("planned_route_km") or route_distance_km(route)
            # Track the segment observed by this installation; never label it as the full flight.
            session = connection.execute("""
                SELECT id, callsign, last_seen, last_lat, last_lon, observed_distance_km,
                       track_points, max_altitude_ft, min_altitude_ft, speed_sum_kt, speed_samples
                FROM flight_sessions WHERE hex=? ORDER BY last_seen DESC LIMIT 1
            """, (hex_code,)).fetchone()
            if session and (captured_at - session[2] > 180 or (callsign and session[1] and callsign != session[1])):
                session = None
            step_km = distance_km(session[3], session[4], plat, plon) if session and session[3] is not None and session[4] is not None else 0.0
            observed_km = (session[5] if session else 0.0) + step_km
            track_points = (session[6] if session else 0) + 1
            max_alt = max(session[7], altitude) if session and session[7] is not None and altitude is not None else (session[7] if session else altitude)
            min_alt = min(session[8], altitude) if session and session[8] is not None and altitude is not None else (session[8] if session else altitude)
            speed = plane.get("gs")
            valid_speed = isinstance(speed, (int, float)) and math.isfinite(speed)
            speed_sum = (session[9] if session else 0.0) + (speed if valid_speed else 0.0)
            speed_samples = (session[10] if session else 0) + int(valid_speed)
            if session:
                session_id = session[0]
                connection.execute("""
                    UPDATE flight_sessions SET callsign=COALESCE(?, callsign), last_seen=?, origin=COALESCE(?, origin),
                      destination=COALESCE(?, destination), route_airports_json=COALESCE(?, route_airports_json),
                      airline_code=COALESCE(?, airline_code), company=COALESCE(?, company),
                      origin_code=COALESCE(?, origin_code), destination_code=COALESCE(?, destination_code),
                      operator_key=COALESCE(?, operator_key),
                      registration=COALESCE(?, registration), aircraft_type=COALESCE(?, aircraft_type),
                      aircraft_model=COALESCE(?, aircraft_model), manufacturer=COALESCE(?, manufacturer),
                      planned_route_km=COALESCE(?, planned_route_km), observed_distance_km=?, track_points=?,
                      max_altitude_ft=?, min_altitude_ft=?, speed_sum_kt=?, speed_samples=?, last_lat=?, last_lon=?
                    WHERE id=?
                """, (callsign, captured_at, origin, destination, route_json,
                      airline_code, company, origin_code, destination_code, operator_key,
                      registration, aircraft_type, model, manufacturer,
                      planned_route_km, observed_km, track_points, max_alt, min_alt,
                      speed_sum, speed_samples, plat, plon, session_id))
            else:
                session_cursor = connection.execute("""
                    INSERT INTO flight_sessions(hex, callsign, first_seen, last_seen, origin, destination,
                      route_airports_json, airline_code, company, origin_code, destination_code, operator_key,
                      registration, aircraft_type, aircraft_model,
                      manufacturer, planned_route_km, observed_distance_km, track_points, max_altitude_ft,
                      min_altitude_ft, speed_sum_kt, speed_samples, last_lat, last_lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (hex_code, callsign, captured_at, captured_at, origin, destination,
                      route_json, airline_code, company, origin_code, destination_code, operator_key,
                      registration, aircraft_type, model, manufacturer,
                      planned_route_km, observed_km, track_points, max_alt, min_alt,
                      speed_sum, speed_samples, plat, plon))
                session_id = session_cursor.lastrowid
            records.append((
                scan_id, hex_code, callsign,
                registration, aircraft_type, plat, plon,
                altitude,
                plane.get("gs"), plane.get("track"),
                plane.get("baro_rate", plane.get("geom_rate")), plane.get("squawk"),
                source, position_age if isinstance(position_age, (int, float)) else None,
                origin, destination, route_json, airline_code,
                company, origin_code, destination_code, operator_key, model, manufacturer, planned_route_km, observed_km,
                session_id, json.dumps(plane, ensure_ascii=False, separators=(",", ":")),
            ))
        connection.executemany("""
            INSERT OR IGNORE INTO observations(
                scan_id, hex, callsign, registration, aircraft_type, lat, lon,
                altitude_ft, speed_kt, heading_deg, vertical_rate_fpm, squawk,
                position_source, position_age_s, origin, destination, route_airports_json,
                airline_code, company, origin_code, destination_code, operator_key,
                aircraft_model, manufacturer, planned_route_km, session_distance_km,
                session_id, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
    return len(records)


def history_summary(path, days):
    now = int(time.time())
    cutoff = now - days * 86400
    connection = sqlite3.connect(path)
    try:
        scans = connection.execute(
            "SELECT COUNT(*), MIN(captured_at), MAX(captured_at) FROM scans WHERE captured_at >= ? AND captured_at <= ?",
            (cutoff, now),
        ).fetchone()
        totals = connection.execute("""
            SELECT COUNT(*), COUNT(DISTINCT o.hex)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ?
        """, (cutoff,)).fetchone()
        by_hour = connection.execute("""
            SELECT CAST(strftime('%H', datetime(s.captured_at, 'unixepoch', 'localtime')) AS INTEGER), COUNT(*)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? GROUP BY 1 ORDER BY 1
        """, (cutoff,)).fetchall()
        types = connection.execute("""
            SELECT COALESCE(o.aircraft_type, 'ND'), COUNT(*)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """, (cutoff,)).fetchall()
        callsigns = connection.execute("""
            SELECT COALESCE(o.callsign, o.hex), COUNT(*)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """, (cutoff,)).fetchall()
        altitudes = [row[0] for row in connection.execute("""
            SELECT o.altitude_ft FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? AND o.altitude_ft IS NOT NULL
        """, (cutoff,))]
        headings = [row[0] for row in connection.execute("""
            SELECT o.heading_deg FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? AND o.heading_deg IS NOT NULL
        """, (cutoff,))]
        hour_counts = {hour: count for hour, count in by_hour}
        daily_rows = connection.execute("""
            SELECT date(datetime(s.captured_at, 'unixepoch', 'localtime')),
                   CAST(strftime('%H', datetime(s.captured_at, 'unixepoch', 'localtime')) AS INTEGER), COUNT(*)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? GROUP BY 1, 2 ORDER BY 1, 2
        """, (cutoff,)).fetchall()
        daily_counts = {}
        for day, hour, count in daily_rows:
            daily_counts.setdefault(day, {})[hour] = count
        days_seen = sorted(daily_counts)
        unusual_peaks = []
        if len(days_seen) >= 3:
            latest_day = days_seen[-1]
            previous_days = days_seen[:-1]
            for hour in range(24):
                current = daily_counts[latest_day].get(hour, 0)
                baseline = sum(daily_counts[day].get(hour, 0) for day in previous_days) / len(previous_days)
                if current >= max(20, baseline * 2.5) and current - baseline >= 15:
                    unusual_peaks.append({"day": latest_day, "hour": hour, "current": current, "baseline": round(baseline)})
            unusual_peaks.sort(key=lambda peak: peak["current"] - peak["baseline"], reverse=True)
        compass = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        direction_counts = {direction: 0 for direction in compass}
        for heading in headings:
            direction_counts[compass[int((heading % 360 + 22.5) // 45) % 8]] += 1
        return {
            "days": days,
            "scan_count": scans[0] or 0,
            "first_scan": scans[1],
            "last_scan": scans[2],
            "observation_count": totals[0] or 0,
            "aircraft_count": totals[1] or 0,
            "by_hour": [{"label": f"{hour:02d}:00", "value": hour_counts.get(hour, 0)} for hour in range(24)],
            "types": [{"label": label, "value": count} for label, count in types],
            "callsigns": [{"label": label, "value": count} for label, count in callsigns],
            "directions": [{"label": direction, "value": count} for direction, count in direction_counts.items()],
            "unusual_peaks": unusual_peaks[:3],
            "altitude_bands": [
                {"label": label, "value": sum(1 for altitude in altitudes if low <= altitude < high)}
                for label, low, high in (
                    ("< 3.000 ft", -1000, 3000), ("3–10.000 ft", 3000, 10000),
                    ("10–20.000 ft", 10000, 20000), ("20–30.000 ft", 20000, 30000),
                    ("30–40.000 ft", 30000, 40000), ("> 40.000 ft", 40000, float("inf")),
                )
            ],
        }
    finally:
        connection.close()


def history_replay(path, requested_at):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        scan = connection.execute("""
            SELECT * FROM scans ORDER BY ABS(captured_at - ?) LIMIT 1
        """, (requested_at,)).fetchone()
        if scan is None:
            return None
        aircraft = connection.execute("""
            SELECT hex, callsign, registration, aircraft_type, lat, lon, altitude_ft,
                   speed_kt, heading_deg, vertical_rate_fpm, squawk, position_source,
                   position_age_s, origin, destination, route_airports_json, airline_code,
                   company, origin_code, destination_code, operator_key, aircraft_model, manufacturer, planned_route_km, session_distance_km,
                   session_id
            FROM observations WHERE scan_id=? ORDER BY hex
        """, (scan["id"],)).fetchall()
        return {"scan": dict(scan), "aircraft": [dict(row) for row in aircraft]}
    finally:
        connection.close()


def history_export(path, days, fmt):
    cutoff = int(time.time()) - days * 86400
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT s.captured_at, s.observer_lat, s.observer_lon, s.view_lat, s.view_lon,
                   s.view_center_source, s.query_radius_km, s.radius_km,
                   o.hex, o.callsign, o.registration, o.aircraft_type, o.lat, o.lon,
                   o.altitude_ft, o.speed_kt, o.heading_deg, o.vertical_rate_fpm,
                   o.squawk, o.position_source, o.position_age_s, o.origin, o.destination,
                   o.route_airports_json, o.airline_code, o.company, o.origin_code, o.destination_code,
                   o.operator_key, o.aircraft_model,
                   o.manufacturer, o.planned_route_km, o.session_distance_km, o.session_id, o.raw_json
            FROM scans s JOIN observations o ON o.scan_id=s.id
            WHERE s.captured_at >= ? ORDER BY s.captured_at, o.hex
        """, (cutoff,)).fetchall()
        records = [dict(row) for row in rows]
        if fmt == "json":
            return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"
        output = io.StringIO(newline="")
        columns = list(records[0]) if records else [
            "captured_at", "observer_lat", "observer_lon", "view_lat", "view_lon", "view_center_source",
            "query_radius_km", "radius_km", "hex", "callsign",
            "registration", "aircraft_type", "lat", "lon", "altitude_ft", "speed_kt",
            "heading_deg", "vertical_rate_fpm", "squawk", "position_source", "position_age_s",
            "origin", "destination", "route_airports_json", "airline_code", "company",
            "origin_code", "destination_code", "operator_key",
            "aircraft_model", "manufacturer", "planned_route_km", "session_distance_km", "session_id", "raw_json",
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
        return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    finally:
        connection.close()


def history_sessions_export(path, days, fmt):
    cutoff = int(time.time()) - days * 86400
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT id AS session_id, hex, callsign, first_seen, last_seen,
                   origin, destination, route_airports_json, airline_code, company,
                   origin_code, destination_code, operator_key,
                   registration, aircraft_type, aircraft_model, manufacturer,
                   planned_route_km, observed_distance_km, track_points,
                   max_altitude_ft, min_altitude_ft,
                   CASE WHEN speed_samples > 0 THEN ROUND(speed_sum_kt / speed_samples, 1) END AS average_speed_kt,
                   speed_samples, last_lat, last_lon
            FROM flight_sessions WHERE last_seen >= ? ORDER BY first_seen, callsign, hex
        """, (cutoff,)).fetchall()
        records = [dict(row) for row in rows]
        if fmt == "json":
            return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"
        output = io.StringIO(newline="")
        columns = list(records[0]) if records else [
            "session_id", "hex", "callsign", "first_seen", "last_seen", "origin", "destination",
            "route_airports_json", "airline_code", "company", "origin_code", "destination_code",
            "operator_key", "registration", "aircraft_type",
            "aircraft_model", "manufacturer", "planned_route_km", "observed_distance_km", "track_points",
            "max_altitude_ft", "min_altitude_ft", "average_speed_kt", "speed_samples", "last_lat", "last_lon",
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
        return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    finally:
        connection.close()


def history_business_summary(path, days):
    now = int(time.time())
    cutoff = now - days * 86400
    connection = sqlite3.connect(path)
    try:
        routes = connection.execute("""
            SELECT origin_code, destination_code, MAX(origin), MAX(destination), COUNT(*),
                   ROUND(AVG(observed_distance_km), 1), ROUND(AVG(planned_route_km), 1)
            FROM flight_sessions
            WHERE first_seen <= ? AND last_seen >= ? AND origin_code IS NOT NULL AND destination_code IS NOT NULL
            GROUP BY origin_code, destination_code ORDER BY COUNT(*) DESC LIMIT 8
        """, (now, cutoff)).fetchall()
        companies = connection.execute("""
            SELECT operator_key, MAX(company), COUNT(*) FROM flight_sessions
            WHERE first_seen <= ? AND last_seen >= ? AND operator_key IS NOT NULL
            GROUP BY operator_key ORDER BY COUNT(*) DESC LIMIT 8
        """, (now, cutoff)).fetchall()
        models = connection.execute("""
            SELECT LOWER(TRIM(aircraft_model)), MAX(aircraft_model), COUNT(*) FROM flight_sessions
            WHERE first_seen <= ? AND last_seen >= ? AND aircraft_model IS NOT NULL
            GROUP BY LOWER(TRIM(aircraft_model)) ORDER BY COUNT(*) DESC LIMIT 8
        """, (now, cutoff)).fetchall()
        scans = connection.execute("""
            SELECT COUNT(*), MIN(captured_at), MAX(captured_at), AVG(COALESCE(query_radius_km, radius_km)),
                   COUNT(DISTINCT CASE WHEN view_center_source='recorded' AND view_lat IS NOT NULL AND view_lon IS NOT NULL
                     THEN ROUND(view_lat, 1) || ':' || ROUND(view_lon, 1) END)
            FROM scans WHERE captured_at >= ? AND captured_at <= ?
        """, (cutoff, now)).fetchone()
        scan_times = [row[0] for row in connection.execute(
            "SELECT captured_at FROM scans WHERE captured_at >= ? AND captured_at <= ? ORDER BY captured_at",
            (cutoff, now),
        )]
        gaps = [later - earlier for earlier, later in zip(scan_times, scan_times[1:])]
        elapsed = max(0, scan_times[-1] - scan_times[0]) if len(scan_times) > 1 else 0
        covered_seconds = sum(gap for gap in gaps if 0 < gap <= 180)
        ordered_gaps = sorted(gaps)
        p90_gap = ordered_gaps[min(len(ordered_gaps) - 1, int(len(ordered_gaps) * .9))] if ordered_gaps else None
        long_gaps = sum(gap > 180 for gap in gaps)
        coverage_pct = round(100 * min(1, covered_seconds / elapsed), 1) if elapsed else (100.0 if scan_times else 0.0)
        hourly = connection.execute("""
            SELECT CAST(strftime('%H', datetime(s.captured_at, 'unixepoch', 'localtime')) AS INTEGER), COUNT(*)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? AND s.captured_at <= ? GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """, (cutoff, now)).fetchone()
        busiest_hour = {"label": f"{hourly[0]:02d}:00", "value": hourly[1]} if hourly else None
        quality = connection.execute("""
            SELECT COUNT(*), COUNT(DISTINCT hex),
                   SUM(origin_code IS NOT NULL AND destination_code IS NOT NULL),
                   SUM(operator_key IS NOT NULL),
                   SUM(aircraft_model IS NOT NULL OR aircraft_type IS NOT NULL),
                   SUM(position_source='MLAT'), SUM(position_age_s > 30), SUM(position_age_s <= 30)
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? AND s.captured_at <= ?
        """, (cutoff, now)).fetchone()
        session_window = connection.execute("""
            SELECT COUNT(*), COUNT(DISTINCT hex), SUM(track_points), AVG(observed_distance_km), AVG(planned_route_km)
            FROM flight_sessions WHERE first_seen <= ? AND last_seen >= ?
        """, (now, cutoff)).fetchone()
        # Count only position-to-position segments with a gap no longer than 3 minutes.
        points = connection.execute("""
            SELECT o.hex, o.session_id, s.captured_at, o.lat, o.lon
            FROM observations o JOIN scans s ON s.id=o.scan_id
            WHERE s.captured_at >= ? AND s.captured_at <= ?
            ORDER BY o.hex, o.session_id, s.captured_at
        """, (cutoff, now)).fetchall()
        observed_distance = 0.0
        observed_sessions = set()
        previous = None
        for point in points:
            if previous and point[0] == previous[0] and point[1] == previous[1]:
                elapsed_seconds = point[2] - previous[2]
                if 0 < elapsed_seconds <= 180:
                    observed_distance += distance_km(previous[3], previous[4], point[3], point[4])
                    observed_sessions.add((point[0], point[1]))
            previous = point
        total_observations = quality[0] or 0
        route_pct = round(100 * (quality[2] or 0) / total_observations, 1) if total_observations else 0
        operator_pct = round(100 * (quality[3] or 0) / total_observations, 1) if total_observations else 0
        model_pct = round(100 * (quality[4] or 0) / total_observations, 1) if total_observations else 0
        freshness_pct = round(100 * (quality[7] or 0) / total_observations, 1) if total_observations else 0
        quality_score = round((coverage_pct + route_pct + operator_pct + model_pct + freshness_pct) / 5)
        quality_grade = ("Good" if quality_score >= 80 and coverage_pct >= 90 and long_gaps == 0
                         else "Fair" if quality_score >= 55 and coverage_pct >= 70 else "Limited")
        comparison_ready = days >= 7 and elapsed >= 5 * 86400 and coverage_pct >= 70 and long_gaps == 0
        return {
            "days": days, "flight_sessions": session_window[0] or 0, "unique_aircraft": session_window[1] or 0,
            "track_points": session_window[2] or 0, "observation_count": total_observations,
            "unique_observations_aircraft": quality[1] or 0, "observed_distance_km": round(observed_distance, 1),
            "scan_count": scans[0] or 0, "first_scan": scans[1], "last_scan": scans[2],
            "monitoring_elapsed_seconds": elapsed, "median_scan_interval_seconds": round(statistics.median(gaps)) if gaps else None,
            "p90_scan_interval_seconds": p90_gap, "long_gaps": long_gaps, "coverage_pct": coverage_pct,
            "mean_query_radius_km": round(scans[3], 1) if scans[3] is not None else None,
            "observed_areas": scans[4] or 0,
            "legacy_area_fallback_scans": connection.execute(
                "SELECT COUNT(*) FROM scans WHERE captured_at >= ? AND captured_at <= ? AND view_center_source='observer_fallback'",
                (cutoff, now),
            ).fetchone()[0],
            "mlat_observations": quality[5] or 0,
            "stale_position_observations": quality[6] or 0, "busiest_hour": busiest_hour,
            "mean_observed_distance_km": round(observed_distance / len(observed_sessions), 1) if observed_sessions else None,
            "mean_planned_route_km": round(session_window[4], 1) if session_window[4] is not None else None,
            "route_coverage_pct": route_pct, "company_coverage_pct": operator_pct,
            "model_coverage_pct": model_pct, "fresh_position_pct": freshness_pct,
            "quality_score": quality_score, "quality_grade": quality_grade,
            "comparison_ready": comparison_ready,
            "routes": [{"label": f"{row[2] or row[0]} → {row[3] or row[1]}", "value": row[4], "mean_observed_km": row[5], "mean_planned_km": row[6]} for row in routes],
            "companies": [{"key": row[0], "label": row[1] or row[0], "value": row[2]} for row in companies],
            "models": [{"key": row[0], "label": row[1], "value": row[2]} for row in models],
            "comparison": {"current_sessions": session_window[0] or 0},
        }
    finally:
        connection.close()


class RadarHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        attachment = None
        if parsed.path == "/health":
            body = b'{"status":"ok"}'
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/data":
            with location_lock:
                snapshot = dict(latest_snapshot)
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/view":
            try:
                with location_lock:
                    current_view = dict(active_view)
                view_lat = float(params.get("lat", [str(current_view["lat"])])[0])
                view_lon = float(params.get("lon", [str(current_view["lon"])])[0])
                radius = float(params.get("radius", [str(current_view["radius"])])[0])
                if (not math.isfinite(view_lat) or not -90 <= view_lat <= 90
                        or not math.isfinite(view_lon) or not -180 <= view_lon <= 180
                        or not math.isfinite(radius) or radius <= 0):
                    raise ValueError
            except (ValueError, TypeError):
                self.send_error(400, "Invalid map view radius")
                return
            radius = max(5.0, min(MAX_RADAR_RADIUS_KM, radius))
            with location_lock:
                active_view.update({"lat": view_lat, "lon": view_lon, "radius": radius,
                                    "requested_at": time.monotonic()})
                latest_snapshot.update({"requested_radius": radius,
                                        "requested_view_lat": view_lat, "requested_view_lon": view_lon})
            body = json.dumps({"lat": view_lat, "lon": view_lon, "radius": radius}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/search":
            query = params.get("q", [""])[0].strip().upper()
            if len(query) < 3 or len(query) > 12 or not all(char.isalnum() or char == "-" for char in query):
                self.send_error(400, "Enter a callsign, registration, or valid ICAO code")
                return
            try:
                matches = search_aircraft(query)
                position_snapshot_at = int(time.time())
                location = latest_snapshot
                results = prepare_flights(
                    location["lat"], location["lon"], matches, location["radius"],
                    include_outside=True, include_unknown_routes=True,
                )
                body = json.dumps({"query": query, "results": results, "position_snapshot_at": position_snapshot_at,
                                   "searched_at": int(time.time()), "matched_count": len(matches)},
                                  ensure_ascii=False).encode("utf-8")
            except urllib.error.HTTPError as exc:
                status = 429 if exc.code == 429 else 502
                body = json.dumps({"query": query, "results": [], "error": f"Feed ADS-B HTTP {exc.code}"}, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                body = json.dumps({"query": query, "results": [], "error": f"ADS-B feed unavailable: {exc}"}, ensure_ascii=False).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            content_type = "application/json; charset=utf-8"
        elif parsed.path in ("/", "/index.html"):
            body = Path(__file__).with_name("radar.html").read_bytes()
            content_type = "text/html; charset=utf-8"
        elif parsed.path == "/history/summary":
            days = max(1, min(365, int(params.get("days", ["7"])[0])))
            body = json.dumps(history_summary(history_path, days), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/history/business":
            days = max(1, min(365, int(params.get("days", ["7"])[0])))
            body = json.dumps(history_business_summary(history_path, days), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/history/replay":
            requested_at = int(params.get("at", [str(int(time.time()))])[0])
            result = history_replay(history_path, requested_at)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/history/track":
            hex_code = params.get("hex", [""])[0].lower()
            start = int(params.get("from", [str(int(time.time()) - 3600)])[0])
            end = int(params.get("to", [str(int(time.time()))])[0])
            connection = sqlite3.connect(history_path)
            try:
                track = connection.execute("""
                    SELECT s.captured_at, o.lat, o.lon, o.altitude_ft
                    FROM observations o JOIN scans s ON s.id=o.scan_id
                    WHERE o.hex=? AND s.captured_at BETWEEN ? AND ?
                    ORDER BY s.captured_at
                """, (hex_code, start, end)).fetchall()
            finally:
                connection.close()
            body = json.dumps([{"captured_at": t, "lat": lat, "lon": lon, "altitude_ft": alt} for t, lat, lon, alt in track]).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/track/full":
            hex_code = params.get("hex", [""])[0].lower()
            if not hex_code or len(hex_code) != 6 or any(char not in "0123456789abcdef" for char in hex_code):
                self.send_error(400, "Invalid ICAO code")
                return
            try:
                result = fetch_opensky_track(hex_code)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
                result = {"points": [], "source": "OpenSky", "error": str(exc)}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == "/history/export":
            days = max(1, min(365, int(params.get("days", ["7"])[0])))
            fmt = params.get("format", ["csv"])[0].lower()
            if fmt not in ("csv", "json"):
                self.send_error(400, "Unsupported format")
                return
            body, content_type = history_export(history_path, days, fmt)
            attachment = f"attachment; filename=flighthome-history-{days}d.{fmt}"
        elif parsed.path == "/history/sessions-export":
            days = max(1, min(365, int(params.get("days", ["7"])[0])))
            fmt = params.get("format", ["csv"])[0].lower()
            if fmt not in ("csv", "json"):
                self.send_error(400, "Unsupported format")
                return
            body, content_type = history_sessions_export(history_path, days, fmt)
            attachment = f"attachment; filename=flighthome-flights-{days}d.{fmt}"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header("Content-Disposition", attachment)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/location":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            location = json.loads(self.rfile.read(length))
            lat, lon = float(location["lat"]), float(location["lon"])
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("Coordinates out of range")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.send_error(400, "Invalid coordinates")
            return
        with location_lock:
            radius = active_view.get("radius", latest_snapshot.get("radius", 100))
            active_location.update({"lat": lat, "lon": lon, "configured": True})
            active_view.update({"lat": lat, "lon": lon, "radius": radius, "requested_at": time.monotonic()})
            latest_snapshot.update({"lat": lat, "lon": lon, "view_lat": lat, "view_lon": lon,
                                    "radius": radius, "requested_radius": radius,
                                    "flights": [], "updated": None, "location_configured": True})
        body = json.dumps({"lat": lat, "lon": lon}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FlightHome/1.0"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_opensky_track(hex_code):
    """Fetch and cache a full flight track through the optional OpenSky credentials."""
    client_id, client_secret = os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return {"points": [], "source": "OpenSky", "error": "OpenSky credentials are not configured"}
    now = int(time.time())
    connection = sqlite3.connect(history_path)
    try:
        cached = connection.execute(
            "SELECT points_json, fetched_at FROM external_tracks WHERE hex=? ORDER BY fetched_at DESC LIMIT 1",
            (hex_code,),
        ).fetchone()
        if cached and now - cached[1] < 300:
            return {"points": json.loads(cached[0]), "source": "OpenSky", "cached": True}
    finally:
        connection.close()
    auth_req = urllib.request.Request(
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "FlightHome/1.0"},
    )
    with urllib.request.urlopen(auth_req, timeout=15) as response:
        token = json.loads(response.read().decode())["access_token"]
    # time=0 asks OpenSky for the active flight's track; it does not need local history.
    track_url = f"https://opensky-network.org/api/tracks/all?icao24={hex_code}&time=0"
    request = urllib.request.Request(track_url, headers={"Authorization": f"Bearer {token}", "User-Agent": "FlightHome/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"points": [], "source": "OpenSky", "error": "Full track unavailable for this flight"}
        raise
    points = []
    for waypoint in payload.get("path") or []:
        if len(waypoint) < 4 or waypoint[1] is None or waypoint[2] is None:
            continue
        points.append({"captured_at": int(waypoint[0]), "lat": waypoint[1], "lon": waypoint[2], "altitude_ft": round(waypoint[3] * 3.28084) if waypoint[3] is not None else None})
    with sqlite3.connect(history_path) as connection:
        connection.execute("INSERT OR REPLACE INTO external_tracks(hex, flight_time, fetched_at, points_json) VALUES (?, ?, ?, ?)", (hex_code, first_seen, now, json.dumps(points)))
    return {"points": points, "source": "OpenSky", "cached": False}


def distance_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def route_distance_km(stops):
    """Great-circle airport-to-airport distance for all known route legs."""
    points = []
    for stop in stops:
        try:
            lat, lon = float(stop["lat"]), float(stop["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append((lat, lon))
    if len(points) < 2:
        return None
    return round(sum(distance_km(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])), 1)


def fetch(lat, lon, radius_km):
    # adsb.lol expects distance in nautical miles.
    nm = max(1, min(250, math.ceil(radius_km / 1.852)))
    url = API.format(lat=urllib.parse.quote(str(lat)), lon=urllib.parse.quote(str(lon)), dist=nm)
    payload = get_json(url)
    # adsb.lol segue il formato readsb: la lista live si chiama "ac".
    # "aircraft" remains as a fallback for compatible endpoints.
    return payload.get("ac", payload.get("aircraft", []))


def search_aircraft(query):
    if len(query) == 6 and all(char in "0123456789ABCDEF" for char in query):
        kinds = ("hex",)
    elif "-" in query:
        kinds = ("reg",)
    else:
        # North American registrations often have no dash; try them after callsign lookup.
        kinds = ("callsign", "reg")
    for kind in kinds:
        url = f"https://api.adsb.lol/v2/{kind}/{urllib.parse.quote(query, safe='')}"
        try:
            payload = get_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        aircraft = payload.get("ac", payload.get("aircraft", []))
        if isinstance(aircraft, list) and aircraft:
            return aircraft
    return []


def retry_after_seconds(error, fallback):
    """Honor Retry-After when present; otherwise use the caller's backoff."""
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return fallback
    try:
        return max(1, int(math.ceil(float(value))))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value).timestamp()
            return max(1, int(math.ceil(retry_at - time.time())))
        except (TypeError, ValueError, OverflowError):
            return fallback


def get_route(callsign):
    callsign = callsign.strip()
    if not callsign:
        return None
    if callsign not in route_cache:
        try:
            route_cache[callsign] = get_json(ROUTE_API.format(callsign=urllib.parse.quote(callsign)))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            route_cache[callsign] = None
    route = route_cache[callsign]
    if not isinstance(route, dict) or "origin" not in route or "destination" not in route:
        return None
    return route


def get_aircraft_info(plane):
    hex_code = plane.get("hex", "").strip().lower()
    if not hex_code:
        return None
    if hex_code not in aircraft_cache:
        try:
            aircraft_cache[hex_code] = get_json(f"https://hexdb.io/api/v2/aircraft/{urllib.parse.quote(hex_code)}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            aircraft_cache[hex_code] = None
    info = aircraft_cache[hex_code]
    return info if isinstance(info, dict) and "error" not in info else None


def airport_label(airport):
    if not isinstance(airport, dict):
        return "N/A"
    name = airport.get("name")
    iata = airport.get("iata")
    icao = airport.get("icao")
    country_code = airport.get("country_code")
    country = COUNTRY_NAMES.get(country_code, country_code or airport.get("country"))
    code = iata or icao
    if name:
        for suffix in (" International Airport", " International", " Airport"):
            name = name.removesuffix(suffix)
        if len(name) > 18:
            name = name[:17].rstrip() + "…"
    if code and country:
        label = f"{code} ({country})"
        if name:
            label += f" {name}"
    else:
        label = f"{code} {name}".strip() if code and name else name or code or "N/A"
    return label


def route_airports(route):
    stops = route.get("airports") or [route.get("origin"), route.get("destination")]
    normalized = []
    for stop in stops:
        if not isinstance(stop, dict):
            continue
        normalized.append({
            "label": airport_label(stop),
            "iata": stop.get("iata"),
            "icao": stop.get("icao"),
            "lat": stop.get("latitude"),
            "lon": stop.get("longitude"),
        })
    return normalized


def get_airborne_nearby(lat, lon, aircraft, radius):
    found = []
    for plane in aircraft:
        if plane.get("lat") is None or plane.get("lon") is None:
            continue
        altitude = plane.get("alt_baro")
        if altitude == "ground":
            continue
        if not isinstance(altitude, (int, float)):
            altitude = plane.get("alt_geom")
        if not isinstance(altitude, (int, float)):
            continue
        km = distance_km(lat, lon, plane["lat"], plane["lon"])
        if km <= radius:
            found.append((km, plane, altitude))
    found.sort(key=lambda item: item[0])
    return found


def predict_closest_approach(lat, lon, plane, altitude_ft, horizon_min=15):
    """Linear short-term closest approach from the current ground vector."""
    speed, heading = plane.get("gs"), plane.get("track")
    if not isinstance(speed, (int, float)) or not isinstance(heading, (int, float)) or speed < 20:
        return None, None, None
    north_km = (plane["lat"] - lat) * 111.32
    east_km = (plane["lon"] - lon) * 111.32 * math.cos(math.radians(lat))
    velocity = speed * 1.852 / 3600
    heading_rad = math.radians(heading)
    east_v = velocity * math.sin(heading_rad)
    north_v = velocity * math.cos(heading_rad)
    v2 = east_v * east_v + north_v * north_v
    seconds = -(east_km * east_v + north_km * north_v) / v2
    if seconds < 0 or seconds > horizon_min * 60:
        return None, None, None
    cpa_east = east_km + east_v * seconds
    cpa_north = north_km + north_v * seconds
    cpa_distance = math.hypot(cpa_east, cpa_north)
    vertical_rate = plane.get("baro_rate") or plane.get("geom_rate") or 0
    cpa_altitude = altitude_ft + vertical_rate * seconds / 60
    return seconds / 60, cpa_distance, cpa_altitude


def draw_map(lat, lon, found, radius, width=31, height=15):
    """Draw a local map with IDs matching the flight list."""
    width = max(25, min(width, 39))
    height = max(11, min(height, 19))
    cells = [["   " for _ in range(width)] for _ in range(height)]
    center_x, center_y = width // 2, height // 2
        # Axes centered on the observer position.
    for x in range(width):
        cells[center_y][x] = "───"
    for y in range(height):
        cells[y][center_x] = " │ "
    cells[center_y][center_x] = " ME"
    for index, (_, plane, _) in enumerate(found, 1):
        plat, plon = plane["lat"], plane["lon"]
        dy_km = (plat - lat) * 111.32
        dx_km = (plon - lon) * 111.32 * math.cos(math.radians(lat))
        x = round(center_x + dx_km / radius * (width // 2 - 1))
        y = round(center_y - dy_km / radius * (height // 2 - 1))
        label = f"{index:02d} "
        # Move overlapping labels to nearby cells while retaining the flight-list ID.
        if cells[y][x].strip() not in ("", "───", "│"):
            placed = False
            for dy, dx in ((-1, 0), (0, 1), (0, -1), (1, 0), (-1, 1), (1, 1), (-1, -1), (1, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < height and 0 <= nx < width and cells[ny][nx].strip() in ("", "───", "│"):
                    y, x, placed = ny, nx, True
                    break
            if not placed:
                continue
        cells[y][x] = label
    print(f"\nMAP · radius {radius:g} km · north ↑")
    print("       N")
    for row in cells:
        print(" " + "".join(row).rstrip())
    print("       S")
    print("Each number identifies the matching flight in the list below.")


def prepare_flights(lat, lon, aircraft, radius, include_outside=False, include_unknown_routes=False,
                    candidate_limit=None, candidate_center=None):
    if include_outside:
        found = []
        for plane in aircraft:
            plat, plon = plane.get("lat"), plane.get("lon")
            if (not isinstance(plat, (int, float)) or not isinstance(plon, (int, float))
                    or not math.isfinite(plat) or not math.isfinite(plon)
                    or not -90 <= plat <= 90 or not -180 <= plon <= 180):
                continue
            altitude = plane.get("alt_baro")
            if altitude == "ground":
                continue
            if not isinstance(altitude, (int, float)):
                altitude = plane.get("alt_geom")
            if not isinstance(altitude, (int, float)) or not math.isfinite(altitude):
                continue
            found.append((distance_km(lat, lon, plat, plon), plane, altitude))
    else:
        found = get_airborne_nearby(lat, lon, aircraft, radius)
    if candidate_limit is not None:
        if candidate_center is None:
            found.sort(key=lambda item: item[0])
        else:
            found.sort(key=lambda item: distance_km(candidate_center[0], candidate_center[1], item[1]["lat"], item[1]["lon"]))
        found = found[:candidate_limit]
    callsigns = list(dict.fromkeys((p.get("flight") or "").strip() for _, p, _ in found))
    callsigns = [callsign for callsign in callsigns if callsign]
    with ThreadPoolExecutor(max_workers=8) as executor:
        routes = dict(zip(callsigns, executor.map(get_route, callsigns)))
    with ThreadPoolExecutor(max_workers=8) as executor:
        details = list(executor.map(get_aircraft_info, [p for _, p, _ in found]))
    info_by_hex = {p.get("hex", "").lower(): info for (_, p, _), info in zip(found, details)}
    flights = []
    for km, p, altitude in found:
        callsign = (p.get("flight") or "").strip() or "N/A"
        route = routes.get(callsign)
        if not route and not include_unknown_routes:
            continue
        origin = airport_label(route.get("origin") if route else None)
        destination = airport_label(route.get("destination") if route else None)
        if (origin == "N/A" or destination == "N/A") and not include_unknown_routes:
            continue
        info = info_by_hex.get((p.get("hex") or "").lower()) or {}
        model = info.get("type") or p.get("t") or "Type unavailable"
        manufacturer = info.get("manufacturer")
        if manufacturer and model != p.get("t"):
            model = f"{manufacturer} {model}"
        registration = info.get("registration") or p.get("r") or ""
        route_stops = route_airports(route) if route else []
        raw_company = p.get("ownOp") or info.get("registered_owner") or info.get("operator")
        airline_code = callsign[:3].upper() if len(callsign) >= 3 else None
        operator_key, company = normalize_operator(raw_company, airline_code, callsign)
        eta_min, cpa_km, cpa_altitude = predict_closest_approach(lat, lon, p, altitude)
        position_age = p.get("seen_pos")
        if not isinstance(position_age, (int, float)):
            position_age = p.get("seen")
        position_source = "MLAT" if p.get("type") == "mlat" or "lat" in (p.get("mlat") or []) else "ADS-B"
        flights.append({
            "hex": p.get("hex", ""), "lat": p.get("lat"), "lon": p.get("lon"),
            "distance_km": km, "callsign": callsign, "type": model,
            "view_distance_km": distance_km(candidate_center[0], candidate_center[1], p["lat"], p["lon"]) if candidate_center else None,
            "registration": registration, "altitude_ft": altitude,
            "speed_kt": p.get("gs"), "heading_deg": p.get("track"),
            "origin": origin, "destination": destination,
            "route_airports": route_stops,
            "airline_code": airline_code, "operator_key": operator_key,
            "company": company, "manufacturer": manufacturer,
            "model": info.get("model") or info.get("type") or p.get("desc") or model,
            "aircraft_type": p.get("t"),
            "planned_route_km": route_distance_km(route_stops),
            "route_source": "HexDB · indicative route",
            "route_updated_at": route.get("updated_at") if route else None,
            "cpa_eta_min": eta_min, "cpa_distance_km": cpa_km,
            "cpa_altitude_ft": cpa_altitude,
            "position_source": position_source, "position_age_s": position_age,
            "squawk": p.get("squawk"), "vertical_rate_fpm": p.get("baro_rate", p.get("geom_rate")),
            "category": p.get("category"), "emergency": p.get("emergency"),
            "last_seen_s": p.get("seen"), "messages": p.get("messages"),
        })
    return flights


def show(lat, lon, flights, radius, draw_terminal_map=True):
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    stamp = time.strftime("%H:%M:%S")
    color = sys.stdout.isatty()
    cyan, green, yellow, dim, reset = (CYAN, GREEN, YELLOW, DIM, RESET) if color else ("", "", "", "", "")
    print(f"{cyan}AIRCRAFT IN FLIGHT{reset}  {dim}{stamp} · within {radius:g} km · {lat:.4f}, {lon:.4f}{reset}")
    if not flights:
        print(f"{dim}No flights with recognized origin and destination.{reset}")
        return
    if draw_terminal_map:
        map_flights = [(f["distance_km"], f, f["altitude_ft"]) for f in flights]
        draw_map(lat, lon, map_flights, radius)
    rows = [(
        f["distance_km"], f["altitude_ft"],
        f"{f['speed_kt']:g} kt" if isinstance(f["speed_kt"], (int, float)) else "N/A",
        f["callsign"], f["type"], f["registration"],
        f"{f['cpa_eta_min']:.0f} min" if f["cpa_eta_min"] is not None else "—",
        f"{f['cpa_distance_km']:.1f} km" if f["cpa_distance_km"] is not None else "—",
        f"{f['cpa_altitude_ft']:,.0f} ft" if f["cpa_altitude_ft"] is not None else "—",
        f["origin"], f["destination"],
    ) for f in flights]
    if rows:
        terminal_width = shutil.get_terminal_size((120, 24)).columns
        headers = ("#", "KM", "ALTITUDE", "SPEED", "CALLSIGN", "AIRCRAFT", "REG", "CPA IN", "CPA", "CPA ALT", "ORIGIN", "DESTINATION")
        table_rows = [(
            f"{i:02d}", f"{km:.1f}", f"{altitude:,.0f} ft", speed, callsign, model,
            registration or "—", cpa_eta, cpa_distance, cpa_altitude, origin, destination
        ) for i, (km, altitude, speed, callsign, model, registration, cpa_eta, cpa_distance, cpa_altitude, origin, destination) in enumerate(rows, 1)]
        widths = [max(len(headers[i]), *(len(row[i]) for row in table_rows)) for i in range(len(headers))]
        # Long airport/model names may exceed the terminal width; keep each flight on one line.
        while sum(widths) + 3 * (len(widths) - 1) > terminal_width:
            candidates = [i for i in (5, 10, 11) if widths[i] > len(headers[i]) + 4]
            if not candidates:
                break
            i = max(candidates, key=lambda index: widths[index])
            widths[i] -= 1
        def cell(value, width, index):
            if len(value) > width:
                value = value[:max(1, width - 1)] + "…"
            return value.rjust(width) if index in (1, 2, 3, 7, 8, 9) else value.ljust(width)
        print("  ".join(cell(value, widths[i], i) for i, value in enumerate(headers)))
        print("  ".join("─" * width for width in widths))
        for row in table_rows:
            parts = [cell(value, widths[i], i) for i, value in enumerate(row)]
            if color:
                parts[0] = f"{cyan}{parts[0]}{reset}"
                parts[4] = f"{yellow}{parts[4]}{reset}"
            print("  ".join(parts))
    if flights[0]["distance_km"] < 2:
        print("The nearest aircraft is within 2 km of the selected location; it may be overhead or nearby.")


def map_feed_loop(args, database_path):
    global latest_snapshot
    history = open_history(database_path)
    last_history_sample = 0.0
    feed_backoff = 30
    consecutive_rate_limits = 0
    last_rate_limit = 0.0
    feed_poll_interval = args.intervallo
    last_view_signature = None
    while True:
        with location_lock:
            location_configured = active_location["configured"]
            observer_lat, observer_lon = active_location["lat"], active_location["lon"]
            view_lat, view_lon = active_view["lat"], active_view["lon"]
            requested_radius = active_view.get("radius", args.raggio)
            view_changed_at = active_view.get("requested_at", 0.0)
        if not location_configured:
            time.sleep(1)
            continue
        view_signature = (round(view_lat, 3), round(view_lon, 3), round(requested_radius))
        if (view_signature != last_view_signature and view_changed_at
                and time.monotonic() - view_changed_at < 1.2):
            time.sleep(.25)
            continue
        cycle_radius = max(5.0, min(MAX_RADAR_RADIUS_KM, float(requested_radius)))
        try:
            planes = fetch(view_lat, view_lon, cycle_radius)
            position_snapshot_at = int(time.time())
            if last_rate_limit and time.monotonic() - last_rate_limit > 1800:
                consecutive_rate_limits = 0
                feed_backoff = 30
                feed_poll_interval = args.intervallo
                last_rate_limit = 0.0
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                print(f"ADS-B feed request failed: {exc}", file=sys.stderr)
                with location_lock:
                    latest_snapshot.update({"error": f"ADS-B feed request failed: HTTP {exc.code}", "retry_at": int(time.time()) + feed_poll_interval})
                time.sleep(feed_poll_interval)
                continue
            now_monotonic = time.monotonic()
            if now_monotonic - last_rate_limit > 1800:
                consecutive_rate_limits = 0
                feed_backoff = 30
            consecutive_rate_limits += 1
            last_rate_limit = now_monotonic
            fallback_wait = min(300, feed_backoff * (2 ** min(consecutive_rate_limits - 1, 4)))
            wait_seconds = max(retry_after_seconds(exc, fallback_wait), fallback_wait)
            feed_poll_interval = max(feed_poll_interval, min(300, 15 * (2 ** min(consecutive_rate_limits - 1, 4))))
            message = f"ADS-B feed rate-limited (HTTP 429); retrying in {wait_seconds}s; polling slowed to {feed_poll_interval}s."
            print(message, file=sys.stderr)
            with location_lock:
                latest_snapshot.update({"error": message, "retry_at": int(time.time()) + wait_seconds, "poll_interval": feed_poll_interval})
            time.sleep(wait_seconds)
            feed_backoff = min(300, max(30, feed_backoff * 2))
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"ADS-B feed request failed: {exc}", file=sys.stderr)
            with location_lock:
                latest_snapshot.update({"error": f"ADS-B feed request failed: {exc}", "retry_at": int(time.time()) + feed_poll_interval})
            time.sleep(feed_poll_interval)
            continue
        wide_view = cycle_radius > RADAR_DENSE_RADIUS_KM
        flights = prepare_flights(
            observer_lat, observer_lon, planes, cycle_radius, include_outside=True,
            candidate_limit=RADAR_MAX_FLIGHTS * 2 if wide_view else None,
            candidate_center=(view_lat, view_lon),
        )
        result_count = len(flights)
        if wide_view:
            flights.sort(key=lambda plane: (
                plane.get("view_distance_km", float("inf")),
                plane.get("cpa_eta_min") is None,
                plane.get("cpa_distance_km") if plane.get("cpa_distance_km") is not None else float("inf"),
            ))
            flights = flights[:RADAR_MAX_FLIGHTS]
        with location_lock:
            location_unchanged = (active_location["lat"] == observer_lat and active_location["lon"] == observer_lon
                                  and active_view["lat"] == view_lat and active_view["lon"] == view_lon
                                  and round(active_view.get("radius", args.raggio)) == round(requested_radius))
            if location_unchanged:
                latest_snapshot.update({
                    "lat": observer_lat, "lon": observer_lon, "view_lat": view_lat, "view_lon": view_lon,
                    "radius": cycle_radius, "requested_radius": cycle_radius,
                    "flights": flights, "limited": wide_view and result_count > RADAR_MAX_FLIGHTS,
                    "result_count": result_count, "updated": int(time.time()), "error": None, "retry_at": None,
                    "poll_interval": feed_poll_interval, "position_snapshot_at": position_snapshot_at, "location_configured": True,
                })
                last_view_signature = view_signature
        now = time.monotonic()
        if location_unchanged and now - last_history_sample >= args.storico_intervallo:
            try:
                save_history(history, planes, flights, observer_lat, observer_lon, cycle_radius, view_lat, view_lon)
                last_history_sample = now
            except sqlite3.Error as exc:
                print(f"SQLite history error: {exc}", file=sys.stderr)
        time.sleep(feed_poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Find ADS-B aircraft near a selected location.")
    parser.add_argument("--lat", type=float, help="observer latitude; required for terminal mode")
    parser.add_argument("--lon", type=float, help="observer longitude; required for terminal mode")
    parser.add_argument("--raggio", type=float, default=100, help="initial map extent or terminal search radius in km (default: 100)")
    parser.add_argument("--intervallo", type=int, default=15, help="seconds between ADS-B updates (default: 15)")
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("FLIGHTHOME_DATABASE", Path(__file__).with_name("flighthome.sqlite3"))), help="SQLite history database path")
    parser.add_argument("--storico-intervallo", type=int, default=60, help="seconds between saved samples (default: 60)")
    parser.add_argument("--giorni-storico", type=int, default=None,
                        help="deprecated compatibility option; history is always retained")
    parser.add_argument("--mappa", action="store_true", help="open the interactive radar map in a browser")
    parser.add_argument("--porta", type=int, default=0, help="map port; defaults to Render PORT or an available local port")
    args = parser.parse_args()
    global history_path
    if (args.lat is None) != (args.lon is None):
        parser.error("Provide both --lat and --lon")
    if args.raggio <= 0 or args.intervallo < 0 or args.storico_intervallo <= 0:
        parser.error("Radius must be positive; intervals cannot be negative and the history interval must be greater than zero")
    if args.giorni_storico is not None:
        print("--giorni-storico is deprecated and ignored; FlightHome retains history indefinitely.", file=sys.stderr)
    if args.mappa and args.intervallo == 0:
        parser.error("--intervallo must be greater than zero with --mappa to avoid continuous requests")
    history = None
    server = None
    server_thread = None
    try:
        configured_lat = args.lat if args.lat is not None else (float(os.environ["FLIGHTHOME_LAT"]) if os.environ.get("FLIGHTHOME_LAT") else None)
        configured_lon = args.lon if args.lon is not None else (float(os.environ["FLIGHTHOME_LON"]) if os.environ.get("FLIGHTHOME_LON") else None)
        if (configured_lat is None) != (configured_lon is None):
            parser.error("Set both FLIGHTHOME_LAT and FLIGHTHOME_LON, or neither")
        if configured_lat is None:
            if not args.mappa:
                parser.error("Set --lat and --lon for terminal mode, or use --mappa and select browser GPS.")
            lat, lon = UNSET_LOCATION
            place = "location not set; choose browser GPS or provide coordinates"
        else:
            lat, lon = configured_lat, configured_lon
            place = "command-line coordinates" if args.lat is not None else "environment coordinates"
        print(f"Position: {place} ({lat:.5f}, {lon:.5f})")
        print(f"ADS-B update interval: {args.intervallo}s")
        if configured_lat is not None and (not -90 <= lat <= 90 or not -180 <= lon <= 180):
            parser.error("Coordinates out of range")
        with location_lock:
            initial_radius = max(5.0, min(MAX_RADAR_RADIUS_KM, args.raggio))
            active_location.update({"lat": lat, "lon": lon, "configured": configured_lat is not None})
            active_view.update({"lat": lat, "lon": lon, "radius": initial_radius, "requested_at": time.monotonic()})
            latest_snapshot.update({"lat": lat, "lon": lon, "radius": initial_radius,
                                    "view_lat": lat, "view_lon": lon,
                                    "requested_radius": initial_radius, "flights": [], "updated": None,
                                    "poll_interval": args.intervallo,
                                    "location_configured": configured_lat is not None})
        history_path = args.database.expanduser().resolve()
        history = open_history(history_path)
        print(f"SQLite history: {args.database.expanduser()} · sample every {args.storico_intervallo}s · retained indefinitely")
        if args.mappa:
            is_render = os.environ.get("RENDER") == "true"
            host = os.environ.get("HOST", "0.0.0.0" if is_render else "127.0.0.1")
            port = args.porta or int(os.environ.get("PORT", "0"))
            server = ThreadingHTTPServer((host, port), RadarHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            map_url = f"http://{browser_host}:{server.server_port}/"
            print(f"Local radar map: {map_url}")
            if not is_render and os.environ.get("FLIGHTHOME_NO_BROWSER") != "1":
                webbrowser.open(map_url)
            # Serve HTTP immediately while the first upstream ADS-B request
            # runs, so platform health checks do not wait on external feeds.
            threading.Thread(target=map_feed_loop, args=(args, history_path), daemon=True).start()
            server_thread.join()
            return 0
        feed_poll_interval = args.intervallo
        while True:
            try:
                planes = fetch(lat, lon, args.raggio)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                wait_seconds = retry_after_seconds(exc, min(300, feed_poll_interval * 2))
                feed_poll_interval = min(300, max(feed_poll_interval, 15) * 2)
                print(f"ADS-B feed rate-limited (HTTP 429); retrying in {wait_seconds}s.", file=sys.stderr)
                time.sleep(wait_seconds)
                continue
            flights = prepare_flights(lat, lon, planes, args.raggio)
            save_history(history, planes, flights, lat, lon, args.raggio)
            show(lat, lon, flights, args.raggio)
            if args.intervallo == 0:
                break
            time.sleep(feed_poll_interval)
    except KeyboardInterrupt:
        print("\nShutting down.")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"SQLite history error: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if history is not None:
            history.execute("PRAGMA optimize")
            history.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
