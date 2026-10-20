#!/usr/bin/env python3
"""Automated backend verification for task #482 (Emergency Contact testing).

What this does and does not cover
-----------------------------------
The #482 test plan's section B (23 cities, verified by faking GPS location
in Chrome DevTools -> Sensors) and section E (API checks via curl) both
exercise the same backend: `emergency_number_api`. The one B-section run
done so far (by hamsaram1406, on Windows) could NOT actually use the
DevTools Sensors override -- Windows Location Service overrides it -- so
they fell back to spot-checking a handful of countries by hand with curl.

This script automates that same kind of API call, but across EVERY B-case
coordinate pair AND all 249 countries in the answer key, and diffs the
response against the answer key automatically instead of eyeballing it.

What it does NOT do, and cannot replace:
  - It does not open a real browser, so it cannot confirm the page's UI
    actually RENDERS these numbers correctly (test plan sections A-D).
  - It does not log in; you must supply a token captured from DevTools
    yourself (see --token below), same as test plan section E.

Authentication -- deliberately NOT a "Bearer" scheme
------------------------------------------------------
The #482 test plan (section E) is explicit: send the token as the
Authorization header value AS-IS, with no "Bearer " prefix -- its own
curl example is `-H "Authorization: <token>"`, not `"Authorization:
Bearer <token>"`. "Strip the Bearer prefix" in that plan (and below)
means: the value DevTools shows you already starts with "Bearer ", and
you must remove that before using it, because this API's own convention
is to receive the bare token. Do not "fix" _call_api to re-add "Bearer "
-- that would break it against the real API.

Response shape caveat
----------------------
The exact JSON field names returned by the API were not confirmed against
a live call while writing this (no login session available in this
environment). This script tries a short list of plausible field-name
candidates for each logical value (see _FIELD_CANDIDATES below) and
reports a field as unresolved -- with the raw response printed -- rather
than silently assuming a guessed field name is right. If the real field
names differ, update _FIELD_CANDIDATES once and every case benefits.

Usage
-----
    export EMERGENCY_API_TOKEN="<the Authorization value from DevTools, with 'Bearer ' removed>"
    python verify_emergency_contact_api.py --mode both

    # Or pass the token directly (prefer the env var on a shared machine --
    # a --token argument is visible in shell history and process listings):
    python verify_emergency_contact_api.py --token eyJ... --mode coords

Output: a summary to stdout, and a CSV (default
emergency_contact_api_results.csv, written relative to the current
directory -- pass --out to control this) with one row per test case,
formatted to paste straight into the #482 tracker. Results are written
even if the sweep is interrupted partway through.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.help-for-everyone.org/v1/genai/emergency_number_api"

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ANSWER_KEY = os.path.join(_MODULE_DIR, "emergency_contacts_expected_values.csv")

# The 23 B-section cases from the #482 test plan (id, location, lat, lng,
# expected country code, expected police/fire/ambulance/mental-health).
# Duplicates the same countries' rows in the answer key CSV -- checked
# against it at startup in _check_b_cases_match_answer_key() so a future
# answer-key revision can't silently drift out of sync with this table.
B_CASES = [
    ("B1", "New Delhi, India", 28.6139, 77.2090, "IN", "112", "101", "108", "9152987821"),
    ("B2", "Karachi, Pakistan", 24.8607, 67.0011, "PK", "15", "16", "1122", "15"),
    ("B3", "Dubai, UAE", 25.2048, 55.2708, "AE", "999", "997", "998", "999"),
    ("B4", "Tokyo, Japan", 35.6762, 139.6503, "JP", "110", "119", "119", "110"),
    ("B5", "Singapore", 1.3521, 103.8198, "SG", "999", "995", "995", "999"),
    ("B6", "Manila, Philippines", 14.5995, 120.9842, "PH", "911", "911", "911", "911"),
    ("B7", "Istanbul, Turkiye", 41.0082, 28.9784, "TR", "155", "112", "112", "112"),
    ("B8", "Cairo, Egypt", 30.0444, 31.2357, "EG", "122", "180", "123", "112"),
    ("B9", "London, UK", 51.5074, -0.1278, "GB", "999", "999", "999", "999"),
    ("B10", "Paris, France", 48.8566, 2.3522, "FR", "17", "18", "15", "112"),
    ("B11", "Berlin, Germany", 52.5200, 13.4050, "DE", "110", "112", "112", "112"),
    ("B12", "Bern, Switzerland", 46.9480, 7.4474, "CH", "117", "118", "144", "117"),
    ("B13", "Kyiv, Ukraine", 50.4501, 30.5234, "UA", "102", "101", "103", "112"),
    ("B14", "Lagos, Nigeria", 6.5244, 3.3792, "NG", "112", "112", "112", "112"),
    ("B15", "Nairobi, Kenya", -1.2921, 36.8219, "KE", "112", "112", "112", "112"),
    ("B16", "Johannesburg, South Africa", -26.2041, 28.0473, "ZA", "10111", "10177", "10177", "10111"),
    ("B17", "New York, USA", 40.7128, -74.0060, "US", "911", "911", "911", "988"),
    ("B18", "Toronto, Canada", 43.6532, -79.3832, "CA", "911", "911", "911", "988"),
    ("B19", "Mexico City, Mexico", 19.4326, -99.1332, "MX", "911", "911", "911", "911"),
    ("B20", "Sao Paulo, Brazil", -23.5505, -46.6333, "BR", "190", "193", "192", "190"),
    ("B21", "Bogota, Colombia", 4.7110, -74.0721, "CO", "112", "119", "125", "123"),
    ("B22", "Sydney, Australia", -33.8688, 151.2093, "AU", "000", "000", "000", "131114"),
    ("B23", "Bengaluru, India (state data)", 12.9716, 77.5946, "IN", "112", "101", "108", "9152987821"),
]

# Candidate JSON key names per logical field -- see the module docstring.
_FIELD_CANDIDATES = {
    "police": ("police", "police_number", "policeNumber"),
    "fire": ("fire", "fire_number", "fireNumber"),
    "ambulance": ("ambulance", "medical", "medical_emergency", "ambulanceNumber", "ambulance_number"),
    "mental_health": ("suicide_helpline", "mental_health", "suicideHelpline", "mentalHealth"),
    "country": ("country", "country_code", "countryCode"),
}

UNRESOLVED = object()  # sentinel: none of a field's candidate keys were found (or found as null)


def _extract(payload, field):
    """Returns the field's value as a string, or UNRESOLVED if none of its
    candidate keys are present with a non-null value. Distinguishing a
    missing/null value from a real one matters: a real `null` would
    otherwise stringify to the literal text "None" and get compared as if
    it were a genuine (wrong) answer instead of an unresolved field.
    """
    for key in _FIELD_CANDIDATES[field]:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return UNRESOLVED


def _call_api(token, params, timeout=10):
    """Returns (status, payload). status is None on a connection-level
    failure (no HTTP response at all); payload is always a dict, never
    raises -- malformed/non-JSON/non-UTF-8 bodies on ANY status (including
    200) are caught and surfaced as {"_raw": ...} rather than propagating,
    so one bad response can't abort an in-progress sweep.
    """
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    # Deliberately no "Bearer " prefix -- see the module docstring.
    request = urllib.request.Request(url, headers={"Authorization": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw_body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw_body = exc.code, exc.read()
    except urllib.error.URLError as exc:
        return None, {"_error": str(exc)}

    try:
        return status, json.loads(raw_body.decode(errors="replace"))
    except json.JSONDecodeError:
        return status, {"_raw": raw_body.decode(errors="replace")}


def _load_answer_key(path):
    with open(path, newline="") as f:
        return {row["Country code"]: row for row in csv.DictReader(f)}


def _check_b_cases_match_answer_key(answer_key):
    """Warns (doesn't fail) if B_CASES has drifted from the answer key CSV
    it duplicates -- e.g. after an answer-key revision. Checked once at
    startup so drift is visible immediately rather than silently producing
    misleading FAIL rows later.
    """
    mismatches = []
    for test_id, _location, _lat, _lng, country, police, fire, ambulance, mental_health in B_CASES:
        row = answer_key.get(country)
        if row is None:
            mismatches.append(f"{test_id}: country {country} not found in answer key")
            continue
        expected = (row["Police"], row["Fire"], row["Medical Emergency / Ambulance"],
                    row["Mental Health / Suicide Prevention"])
        actual = (police, fire, ambulance, mental_health)
        if expected != actual:
            mismatches.append(f"{test_id} ({country}): B_CASES has {actual}, answer key has {expected}")
    if mismatches:
        print("WARNING: B_CASES has drifted from the answer key:", file=sys.stderr)
        for m in mismatches:
            print(f"  {m}", file=sys.stderr)


def _compare(expected, actual):
    if actual is UNRESOLVED:
        return "UNKNOWN_SHAPE"
    return "PASS" if actual.strip() == expected.strip() else "FAIL"


def _aggregate(outcomes):
    """One case's overall status from its per-field outcomes. A single
    real mismatch always wins (FAIL) even if other fields matched or were
    unresolved; an unresolved field only shows as UNKNOWN_SHAPE when
    nothing actually failed -- otherwise a schema-guessing gap in one
    field would mask a real data mismatch in another.
    """
    values = outcomes.values()
    if any(v == "FAIL" for v in values):
        return "FAIL"
    if any(v == "UNKNOWN_SHAPE" for v in values):
        return "UNKNOWN_SHAPE"
    return "PASS"


def _detail(fields, outcomes):
    mismatches = "; ".join(
        f"{name}: expected={exp} actual={act if act is not UNRESOLVED else '(field not found)'}"
        for name, (exp, act) in fields.items()
        if outcomes[name] != "PASS"
    )
    return mismatches or "all fields matched"


def run_country_sweep(token, answer_key, delay, results):
    """Appends into `results` (a list owned by the caller) as each case
    completes, rather than building and returning a local list -- so if
    something still manages to raise partway through (anything other than
    a single bad API response, which _call_api already absorbs), whatever
    was appended so far is still visible to the caller's try/finally.
    """
    for code, row in sorted(answer_key.items()):
        status, payload = _call_api(token, {"country": code})

        if not row["Police"].strip():
            # No-data territory (AQ/BV/HM/NF/PN/SJ/TF/UM): the answer key's
            # own Notes column says the API should 404 here, falling back
            # to the caller's IP-country numbers on the frontend. A 404 is
            # the PASS case for these rows, not a failure.
            results.append({
                "test_id": f"E-country-{code}", "location": row["Country"],
                "status": "PASS" if status == 404 else "FAIL",
                "detail": "no-data territory, expected 404"
                          if status == 404 else f"expected 404 (no-data territory), got {status}: {json.dumps(payload)[:300]}",
            })
            time.sleep(delay)
            continue

        if status != 200:
            results.append({
                "test_id": f"E-country-{code}", "location": row["Country"],
                "status": f"HTTP_{status}", "detail": json.dumps(payload)[:300],
            })
            time.sleep(delay)
            continue

        fields = {
            "police": (row["Police"], _extract(payload, "police")),
            "fire": (row["Fire"], _extract(payload, "fire")),
            "ambulance": (row["Medical Emergency / Ambulance"], _extract(payload, "ambulance")),
            "mental_health": (row["Mental Health / Suicide Prevention"], _extract(payload, "mental_health")),
        }
        outcomes = {name: _compare(expected, actual) for name, (expected, actual) in fields.items()}
        results.append({
            "test_id": f"E-country-{code}", "location": row["Country"],
            "status": _aggregate(outcomes),
            "detail": _detail(fields, outcomes),
        })
        time.sleep(delay)


def run_b_case_sweep(token, delay, results):
    """Appends into `results` (a list owned by the caller) -- see
    run_country_sweep's docstring for why.
    """
    for test_id, location, lat, lng, expected_country, exp_police, exp_fire, exp_ambulance, exp_mh in B_CASES:
        status, payload = _call_api(token, {"lat": lat, "lng": lng})
        if status != 200:
            results.append({
                "test_id": test_id, "location": location,
                "status": f"HTTP_{status}", "detail": json.dumps(payload)[:300],
            })
            time.sleep(delay)
            continue

        fields = {
            "country": (expected_country, _extract(payload, "country")),
            "police": (exp_police, _extract(payload, "police")),
            "fire": (exp_fire, _extract(payload, "fire")),
            "ambulance": (exp_ambulance, _extract(payload, "ambulance")),
            "mental_health": (exp_mh, _extract(payload, "mental_health")),
        }
        outcomes = {name: _compare(expected, actual) for name, (expected, actual) in fields.items()}
        results.append({
            "test_id": test_id, "location": location,
            "status": _aggregate(outcomes),
            "detail": _detail(fields, outcomes),
        })
        time.sleep(delay)


def _write_results(results, out_path):
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["test_id", "location", "status", "detail"])
        writer.writeheader()
        writer.writerows(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token", default=os.environ.get("EMERGENCY_API_TOKEN"),
                         help="Token from DevTools, 'Bearer ' prefix removed. Prefer EMERGENCY_API_TOKEN "
                              "over this flag -- a CLI argument is visible in shell history and to other "
                              "local users via `ps`.")
    parser.add_argument("--mode", choices=["country", "coords", "both"], default="both",
                         help="country: sweep all 249 countries by code. coords: the 23 B-section "
                              "lat/lng cases. both: run both sweeps (default).")
    parser.add_argument("--answer-key", default=DEFAULT_ANSWER_KEY,
                         help=f"Path to the answer key CSV (default: {DEFAULT_ANSWER_KEY})")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between API calls (default 0.3)")
    parser.add_argument("--out", default="emergency_contact_api_results.csv",
                         help="Where to write the results CSV, relative to the current directory "
                              "unless given as an absolute path (default: emergency_contact_api_results.csv)")
    args = parser.parse_args()

    if not args.token:
        print("No token given. Pass --token or set EMERGENCY_API_TOKEN.", file=sys.stderr)
        print("Get it from DevTools -> Network -> emergency_number_api -> Headers -> Authorization, "
              "with the 'Bearer ' prefix removed. It expires after about an hour.", file=sys.stderr)
        sys.exit(1)

    answer_key = _load_answer_key(args.answer_key)
    _check_b_cases_match_answer_key(answer_key)

    results = []
    try:
        if args.mode in ("coords", "both"):
            print(f"Running the 23 B-section coordinate cases (delay={args.delay}s)...")
            run_b_case_sweep(args.token, args.delay, results)
        if args.mode in ("country", "both"):
            print(f"Running the full {len(answer_key)}-country sweep (delay={args.delay}s)...")
            run_country_sweep(args.token, answer_key, args.delay, results)
    finally:
        # Written even on an unexpected exception or Ctrl-C, so a failure
        # partway through a ~272-call sweep doesn't discard everything
        # already collected.
        if results:
            _write_results(results, args.out)

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\n{len(results)} cases run. Results written to {args.out}\n")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    failures = [r for r in results if r["status"] != "PASS"]
    if failures:
        print("\nNon-passing cases:")
        for r in failures:
            print(f"  [{r['status']}] {r['test_id']} ({r['location']}): {r['detail']}")


if __name__ == "__main__":
    main()
