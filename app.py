#!/usr/bin/env python3
"""Show nearby ADS-B aircraft using the public adsb.lol API."""

import argparse
import csv
import email.utils
import io
import json
import math
import os
import shutil
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
latest_snapshot = {"lat": UNSET_LOCATION[0], "lon": UNSET_LOCATION[1], "radius": 100, "flights": [], "updated": None, "position_snapshot_at": None, "location_configured": False}
active_location = {"lat": UNSET_LOCATION[0], "lon": UNSET_LOCATION[1], "configured": False}
location_lock = threading.Lock()
history_path = None
airport_cache = {}
ourairports_dataset_cache = None
OVERPASS_API = "https://overpass-api.de/api/interpreter"
OURAIRPORTS_BASE = "https://davidmegginson.github.io/ourairports-data"


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
        CREATE TABLE IF NOT EXISTS external_tracks (
            hex TEXT NOT NULL,
            flight_time INTEGER NOT NULL,
            fetched_at INTEGER NOT NULL,
            points_json TEXT NOT NULL,
            PRIMARY KEY(hex, flight_time)
        );
    """)
    # Additive migrations keep existing personal SQLite archives usable.
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
    return connection


def save_history(connection, aircraft, flights, lat, lon, radius, retention_days):
    captured_at = int(time.time())
    with connection:
        cursor = connection.execute(
            "INSERT INTO scans(captured_at, observer_lat, observer_lon, radius_km) VALUES (?, ?, ?, ?)",
            (captured_at, lat, lon, radius),
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
            company = flight.get("company") or plane.get("ownOp") or aircraft_info.get("registered_owner") or aircraft_info.get("operator")
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
                      registration=COALESCE(?, registration), aircraft_type=COALESCE(?, aircraft_type),
                      aircraft_model=COALESCE(?, aircraft_model), manufacturer=COALESCE(?, manufacturer),
                      planned_route_km=COALESCE(?, planned_route_km), observed_distance_km=?, track_points=?,
                      max_altitude_ft=?, min_altitude_ft=?, speed_sum_kt=?, speed_samples=?, last_lat=?, last_lon=?
                    WHERE id=?
                """, (callsign, captured_at, origin, destination, route_json,
                      airline_code, company, registration, aircraft_type, model, manufacturer,
                      planned_route_km, observed_km, track_points, max_alt, min_alt,
                      speed_sum, speed_samples, plat, plon, session_id))
            else:
                session_cursor = connection.execute("""
                    INSERT INTO flight_sessions(hex, callsign, first_seen, last_seen, origin, destination,
                      route_airports_json, airline_code, company, registration, aircraft_type, aircraft_model,
                      manufacturer, planned_route_km, observed_distance_km, track_points, max_altitude_ft,
                      min_altitude_ft, speed_sum_kt, speed_samples, last_lat, last_lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (hex_code, callsign, captured_at, captured_at, origin, destination,
                      route_json, airline_code, company, registration, aircraft_type, model, manufacturer,
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
                company, model, manufacturer, planned_route_km, observed_km,
                session_id, json.dumps(plane, ensure_ascii=False, separators=(",", ":")),
            ))
        connection.executemany("""
            INSERT OR IGNORE INTO observations(
                scan_id, hex, callsign, registration, aircraft_type, lat, lon,
                altitude_ft, speed_kt, heading_deg, vertical_rate_fpm, squawk,
                position_source, position_age_s, origin, destination, route_airports_json,
                airline_code, company, aircraft_model, manufacturer, planned_route_km, session_distance_km,
                session_id, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        cutoff = captured_at - retention_days * 86400
        connection.execute("DELETE FROM scans WHERE captured_at < ?", (cutoff,))
        connection.execute("DELETE FROM flight_sessions WHERE last_seen < ?", (cutoff,))
    return len(records)


def history_summary(path, days):
    cutoff = int(time.time()) - days * 86400
    connection = sqlite3.connect(path)
    try:
        scans = connection.execute(
            "SELECT COUNT(*), MIN(captured_at), MAX(captured_at) FROM scans WHERE captured_at >= ?",
            (cutoff,),
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
                   company, aircraft_model, manufacturer, planned_route_km, session_distance_km,
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
            SELECT s.captured_at, s.observer_lat, s.observer_lon, s.radius_km,
                   o.hex, o.callsign, o.registration, o.aircraft_type, o.lat, o.lon,
                   o.altitude_ft, o.speed_kt, o.heading_deg, o.vertical_rate_fpm,
                   o.squawk, o.position_source, o.position_age_s, o.origin, o.destination,
                   o.route_airports_json, o.airline_code, o.company, o.aircraft_model,
                   o.manufacturer, o.planned_route_km, o.session_distance_km, o.session_id, o.raw_json
            FROM scans s JOIN observations o ON o.scan_id=s.id
            WHERE s.captured_at >= ? ORDER BY s.captured_at, o.hex
        """, (cutoff,)).fetchall()
        records = [dict(row) for row in rows]
        if fmt == "json":
            return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"
        output = io.StringIO(newline="")
        columns = list(records[0]) if records else [
            "captured_at", "observer_lat", "observer_lon", "radius_km", "hex", "callsign",
            "registration", "aircraft_type", "lat", "lon", "altitude_ft", "speed_kt",
            "heading_deg", "vertical_rate_fpm", "squawk", "position_source", "position_age_s",
            "origin", "destination", "route_airports_json", "airline_code", "company",
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
            "route_airports_json", "airline_code", "company", "registration", "aircraft_type",
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
    previous_cutoff = cutoff - days * 86400
    connection = sqlite3.connect(path)
    try:
        totals = connection.execute("""
            SELECT COUNT(*), COUNT(DISTINCT hex), SUM(track_points),
                   AVG(observed_distance_km), AVG(planned_route_km),
                   SUM(origin IS NOT NULL AND destination IS NOT NULL),
                   SUM(company IS NOT NULL), SUM(aircraft_model IS NOT NULL)
            FROM flight_sessions WHERE last_seen >= ?
        """, (cutoff,)).fetchone()
        routes = connection.execute("""
            SELECT COALESCE(origin, 'N/A') || ' → ' || COALESCE(destination, 'N/A'), COUNT(*),
                   ROUND(AVG(observed_distance_km), 1), ROUND(AVG(planned_route_km), 1)
            FROM flight_sessions WHERE last_seen >= ? AND origin IS NOT NULL AND destination IS NOT NULL
            GROUP BY origin, destination
            ORDER BY COUNT(*) DESC LIMIT 8
        """, (cutoff,)).fetchall()
        companies = connection.execute("""
            SELECT COALESCE(company, airline_code, 'Airline N/A'), COUNT(*)
            FROM flight_sessions WHERE last_seen >= ? GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 8
        """, (cutoff,)).fetchall()
        models = connection.execute("""
            SELECT COALESCE(aircraft_model, aircraft_type, 'Model N/A'), COUNT(*)
            FROM flight_sessions WHERE last_seen >= ? GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 8
        """, (cutoff,)).fetchall()
        current_sessions = totals[0] or 0
        previous_sessions = connection.execute(
            "SELECT COUNT(*) FROM flight_sessions WHERE last_seen >= ? AND last_seen < ?",
            (previous_cutoff, cutoff),
        ).fetchone()[0]
        current_routes = connection.execute(
            "SELECT COUNT(DISTINCT COALESCE(origin,'') || '>' || COALESCE(destination,'')) FROM flight_sessions WHERE last_seen >= ? AND origin IS NOT NULL AND destination IS NOT NULL",
            (cutoff,),
        ).fetchone()[0]
        previous_routes = connection.execute(
            "SELECT COUNT(DISTINCT COALESCE(origin,'') || '>' || COALESCE(destination,'')) FROM flight_sessions WHERE last_seen >= ? AND last_seen < ? AND origin IS NOT NULL AND destination IS NOT NULL",
            (previous_cutoff, cutoff),
        ).fetchone()[0]
        return {
            "days": days, "flight_sessions": totals[0] or 0, "unique_aircraft": totals[1] or 0,
            "track_points": totals[2] or 0,
            "mean_observed_distance_km": round(totals[3], 1) if totals[3] is not None else None,
            "mean_planned_route_km": round(totals[4], 1) if totals[4] is not None else None,
            "route_coverage_pct": round(100 * totals[5] / totals[0], 1) if totals[0] else 0,
            "company_coverage_pct": round(100 * totals[6] / totals[0], 1) if totals[0] else 0,
            "model_coverage_pct": round(100 * totals[7] / totals[0], 1) if totals[0] else 0,
            "routes": [{"label": row[0], "value": row[1], "mean_observed_km": row[2], "mean_planned_km": row[3]} for row in routes],
            "companies": [{"label": row[0], "value": row[1]} for row in companies],
            "models": [{"label": row[0], "value": row[1]} for row in models],
            "comparison": {
                "current_sessions": current_sessions, "previous_sessions": previous_sessions,
                "current_routes": current_routes, "previous_routes": previous_routes,
                "previous_period_start": previous_cutoff, "previous_period_end": cutoff,
            },
        }
    finally:
        connection.close()


class RadarHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        attachment = None
        if parsed.path == "/data":
            body = json.dumps(latest_snapshot, ensure_ascii=False).encode("utf-8")
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
                                   "searched_at": int(time.time())}, ensure_ascii=False).encode("utf-8")
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
        elif parsed.path == "/airports":
            try:
                lat = float(params.get("lat", [str(latest_snapshot["lat"])])[0])
                lon = float(params.get("lon", [str(latest_snapshot["lon"])])[0])
                radius = max(5, min(100, float(params.get("radius", [str(latest_snapshot["radius"])])[0])))
                result = get_nearby_airports(lat, lon, radius)
            except (ValueError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                result = {"airports": [], "runways": [], "error": str(exc)}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
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
        if self.path == "/history/clear":
            connection = sqlite3.connect(history_path)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                with connection:
                    connection.execute("DELETE FROM scans")
                    connection.execute("DELETE FROM flight_sessions")
                    connection.execute("DELETE FROM external_tracks")
                body = b'{"ok":true}'
            finally:
                connection.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
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
            active_location.update({"lat": lat, "lon": lon, "configured": True})
            latest_snapshot.update({"lat": lat, "lon": lon, "flights": [], "updated": None, "location_configured": True})
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
        return {"points": [], "source": "OpenSky", "error": "Credenziali OpenSky non configurate"}
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


def overpass_airports(lat, lon, radius_km):
    dlat = radius_km / 111.32
    dlon = radius_km / max(1, 111.32 * math.cos(math.radians(lat)))
    south, west, north, east = lat - dlat, lon - dlon, lat + dlat, lon + dlon
    query = (
        f'[out:json][timeout:20];'
        f'nwr["aeroway"~"^(aerodrome|airport)$"]({south},{west},{north},{east});'
        f'out center tags;'
        f'way["aeroway"="runway"]({south},{west},{north},{east});'
        f'out geom tags;'
    )
    request = urllib.request.Request(
        OVERPASS_API,
        data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": "FlightHome/1.0 (personal local air-traffic viewer)", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    airports, runways = [], []
    seen_airports = set()
    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        if tags.get("aeroway") in ("aerodrome", "airport"):
            point = element.get("center", element)
            alat, alon = point.get("lat"), point.get("lon")
            if alat is None or alon is None or distance_km(lat, lon, alat, alon) > radius_km:
                continue
            identity = (tags.get("icao") or tags.get("iata") or tags.get("name") or str(element.get("id")), round(alat, 3), round(alon, 3))
            if identity in seen_airports:
                continue
            seen_airports.add(identity)
            airports.append({
                "id": element.get("id"), "osm_type": element.get("type"), "name": tags.get("name", "Unnamed airport"),
                "icao": tags.get("icao"), "iata": tags.get("iata"), "lat": alat, "lon": alon,
                "distance_km": round(distance_km(lat, lon, alat, alon), 1),
                "kind": tags.get("aerodrome", tags.get("access", "aerodromo")),
            })
        elif tags.get("aeroway") == "runway":
            geometry = element.get("geometry", [])
            if len(geometry) < 2:
                continue
            points = [[point["lat"], point["lon"]] for point in geometry if "lat" in point and "lon" in point]
            if len(points) < 2:
                continue
            middle = points[len(points) // 2]
            if distance_km(lat, lon, middle[0], middle[1]) > radius_km:
                continue
            runways.append({"id": element.get("id"), "ref": tags.get("ref", "runway"), "surface": tags.get("surface"), "length": tags.get("length"), "points": points})
    return {"airports": sorted(airports, key=lambda airport: airport["distance_km"])[:100], "runways": runways[:250], "source": "OpenStreetMap · Overpass", "cached_at": int(time.time())}


def download_ourairports_csv(name):
    request = urllib.request.Request(
        f"{OURAIRPORTS_BASE}/{name}.csv",
        headers={"User-Agent": "FlightHome/1.0 (personal local air-traffic viewer)", "Accept": "text/csv,*/*"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def ourairports_nearby(lat, lon, radius_km):
    """Use OurAirports' public-domain CSV snapshot when Overpass is unavailable."""
    global ourairports_dataset_cache
    if ourairports_dataset_cache is None or time.time() - ourairports_dataset_cache[0] > 21600:
        with ThreadPoolExecutor(max_workers=2) as executor:
            airports_csv, runways_csv = executor.map(download_ourairports_csv, ("airports", "runways"))
        airports_rows = list(csv.DictReader(io.StringIO(airports_csv)))
        runways_rows = list(csv.DictReader(io.StringIO(runways_csv)))
        ourairports_dataset_cache = (time.time(), airports_rows, runways_rows)
    _, airports_rows, runways_rows = ourairports_dataset_cache
    airports = []
    for row in airports_rows:
        if row.get("type") == "closed_airport":
            continue
        try:
            alat, alon = float(row["latitude_deg"]), float(row["longitude_deg"])
            km = distance_km(lat, lon, alat, alon)
        except (KeyError, TypeError, ValueError):
            continue
        if km > radius_km:
            continue
        airports.append({
            "id": row.get("id"), "osm_type": "ourairports", "name": row.get("name") or "Unnamed airport",
            "icao": row.get("gps_code") or row.get("local_code"), "iata": row.get("iata_code"),
            "lat": alat, "lon": alon, "distance_km": round(km, 1), "kind": row.get("type", "airport"),
        })
    nearby_ids = {str(airport["id"]) for airport in airports}
    runways = []
    for row in runways_rows:
        if str(row.get("airport_ref")) not in nearby_ids or row.get("closed") == "1":
            continue
        try:
            le = [float(row["le_latitude_deg"]), float(row["le_longitude_deg"])]
            he = [float(row["he_latitude_deg"]), float(row["he_longitude_deg"])]
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for point in (le, he) for value in point):
            continue
        runways.append({"id": row.get("id"), "airport_id": str(row.get("airport_ref")), "ref": f'{row.get("le_ident") or ""}/{row.get("he_ident") or ""}'.strip("/"), "surface": row.get("surface"), "length": row.get("length_ft"), "points": [le, he]})
    return {"airports": sorted(airports, key=lambda airport: airport["distance_km"])[:100], "runways": runways[:250], "source": "OurAirports · public-domain data", "cached_at": int(time.time())}


def get_nearby_airports(lat, lon, radius_km):
    """Fetch local airport data, falling back from Overpass to OurAirports."""
    key = (round(lat, 2), round(lon, 2), int(radius_km))
    cached = airport_cache.get(key)
    if cached and time.time() - cached[0] < 21600:
        return cached[1]
    overpass_error = None
    try:
        result = overpass_airports(lat, lon, radius_km)
        if result["airports"]:
            airport_cache[key] = (time.time(), result)
            return result
        overpass_error = "No airports returned by Overpass"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        overpass_error = str(exc)
    try:
        result = ourairports_nearby(lat, lon, radius_km)
        if result["airports"]:
            result["fallback_reason"] = overpass_error
            airport_cache[key] = (time.time(), result)
            return result
        result["error"] = f"No airports found in available sources ({overpass_error})"
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, csv.Error) as exc:
        result = {"airports": [], "runways": [], "source": "OpenStreetMap / OurAirports", "error": f"Overpass: {overpass_error}; OurAirports: {exc}"}
    return result


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


def prepare_flights(lat, lon, aircraft, radius, include_outside=False, include_unknown_routes=False):
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
        if origin == "N/A" or destination == "N/A":
            continue
        info = info_by_hex.get((p.get("hex") or "").lower()) or {}
        model = info.get("type") or p.get("t") or "Type unavailable"
        manufacturer = info.get("manufacturer")
        if manufacturer and model != p.get("t"):
            model = f"{manufacturer} {model}"
        registration = info.get("registration") or p.get("r") or ""
        route_stops = route_airports(route) if route else []
        company = p.get("ownOp") or info.get("registered_owner") or info.get("operator")
        eta_min, cpa_km, cpa_altitude = predict_closest_approach(lat, lon, p, altitude)
        position_age = p.get("seen_pos")
        if not isinstance(position_age, (int, float)):
            position_age = p.get("seen")
        position_source = "MLAT" if p.get("type") == "mlat" or "lat" in (p.get("mlat") or []) else "ADS-B"
        flights.append({
            "hex": p.get("hex", ""), "lat": p.get("lat"), "lon": p.get("lon"),
            "distance_km": km, "callsign": callsign, "type": model,
            "registration": registration, "altitude_ft": altitude,
            "speed_kt": p.get("gs"), "heading_deg": p.get("track"),
            "origin": origin, "destination": destination,
            "route_airports": route_stops,
            "airline_code": callsign[:3].upper() if len(callsign) >= 3 else None,
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


def main():
    parser = argparse.ArgumentParser(description="Find ADS-B aircraft near a selected location.")
    parser.add_argument("--lat", type=float, help="observer latitude; required for terminal mode")
    parser.add_argument("--lon", type=float, help="observer longitude; required for terminal mode")
    parser.add_argument("--raggio", type=float, default=100, help="search radius in km (default: 100)")
    parser.add_argument("--intervallo", type=int, default=15, help="seconds between ADS-B updates (default: 15)")
    parser.add_argument("--database", type=Path, default=Path(__file__).with_name("flighthome.sqlite3"), help="SQLite history database path")
    parser.add_argument("--storico-intervallo", type=int, default=60, help="seconds between saved samples (default: 60)")
    parser.add_argument("--giorni-storico", type=int, default=30, help="history retention in days (default: 30)")
    parser.add_argument("--mappa", action="store_true", help="open the interactive radar map in a browser")
    parser.add_argument("--porta", type=int, default=0, help="local map port; 0 selects an available port")
    args = parser.parse_args()
    global history_path
    if (args.lat is None) != (args.lon is None):
        parser.error("Provide both --lat and --lon")
    if args.raggio <= 0 or args.intervallo < 0 or args.storico_intervallo <= 0 or args.giorni_storico <= 0:
        parser.error("Radius and retention days must be positive; intervals cannot be negative and the history interval must be greater than zero")
    if args.mappa and args.intervallo == 0:
        parser.error("--intervallo must be greater than zero with --mappa to avoid continuous requests")
    history = None
    server = None
    try:
        if args.lat is None:
            if not args.mappa:
                parser.error("Set --lat and --lon for terminal mode, or use --mappa and select browser GPS.")
            lat, lon = UNSET_LOCATION
            place = "location not set; choose browser GPS or provide coordinates"
        else:
            lat, lon = args.lat, args.lon
            place = "command-line coordinates"
        print(f"Position: {place} ({lat:.5f}, {lon:.5f})")
        print(f"ADS-B update interval: {args.intervallo}s")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            parser.error("Coordinates out of range")
        with location_lock:
            active_location.update({"lat": lat, "lon": lon, "configured": True})
            latest_snapshot.update({"lat": lat, "lon": lon, "radius": args.raggio, "flights": [], "updated": None, "poll_interval": args.intervallo, "location_configured": args.lat is not None})
        history_path = args.database.expanduser().resolve()
        history = open_history(history_path)
        print(f"SQLite history: {args.database.expanduser()} · sample every {args.storico_intervallo}s · retain {args.giorni_storico} days")
        if args.mappa:
            server = ThreadingHTTPServer(("127.0.0.1", args.porta), RadarHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            map_url = f"http://127.0.0.1:{server.server_port}/"
            print(f"Local radar map: {map_url}")
            webbrowser.open(map_url)
        last_history_sample = 0.0
        feed_backoff = 30
        consecutive_rate_limits = 0
        last_rate_limit = 0.0
        feed_poll_interval = args.intervallo
        while True:
            if args.mappa:
                with location_lock:
                    location_configured = active_location["configured"]
                    cycle_lat, cycle_lon = active_location["lat"], active_location["lon"]
                if not location_configured:
                    time.sleep(1)
                    continue
            else:
                cycle_lat, cycle_lon = lat, lon
            try:
                planes = fetch(cycle_lat, cycle_lon, args.raggio)
                position_snapshot_at = int(time.time())
                if last_rate_limit and time.monotonic() - last_rate_limit > 1800:
                    consecutive_rate_limits = 0
                    feed_backoff = 30
                    feed_poll_interval = args.intervallo
                    last_rate_limit = 0.0
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
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
                if args.mappa:
                    with location_lock:
                        latest_snapshot.update({"error": message, "retry_at": int(time.time()) + wait_seconds, "poll_interval": feed_poll_interval})
                time.sleep(wait_seconds)
                feed_backoff = min(300, max(30, feed_backoff * 2))
                continue
            flights = prepare_flights(cycle_lat, cycle_lon, planes, args.raggio)
            location_unchanged = True
            if args.mappa:
                with location_lock:
                    # Discard a response started before a GPS location change.
                    location_unchanged = active_location["lat"] == cycle_lat and active_location["lon"] == cycle_lon
                    if location_unchanged:
                        latest_snapshot.update({
                            "lat": cycle_lat, "lon": cycle_lon, "radius": args.raggio,
                            "flights": flights, "updated": int(time.time()), "error": None, "retry_at": None,
                            "poll_interval": feed_poll_interval, "position_snapshot_at": position_snapshot_at, "location_configured": True,
                        })
            now = time.monotonic()
            if location_unchanged and now - last_history_sample >= args.storico_intervallo:
                save_history(history, planes, flights, cycle_lat, cycle_lon, args.raggio, args.giorni_storico)
                last_history_sample = now
            if not args.mappa:
                show(cycle_lat, cycle_lon, flights, args.raggio)
            if args.intervallo == 0 and not args.mappa:
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
        if history is not None:
            try:
                history.execute("PRAGMA optimize")
                history.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                history.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
