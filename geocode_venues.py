"""
Look up venue coordinates from OpenStreetMap (via the Overpass API) and
compare them with the hand-estimated coordinates in data/spots.csv.

Usage:  python geocode_venues.py
Output: data/spots_geocoded.csv  (review this file before using it)

OpenStreetMap data (c) OpenStreetMap contributors, ODbL license.
"""
import difflib
import math
import re
import sys
import time

import pandas as pd
import requests

# Public Overpass servers are free and sometimes overloaded, so we try several.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# Rough bounding box around Arlington County, VA: south, west, north, east
BBOX = (38.827, -77.172, 38.935, -77.032)

# Short search patterns for each venue. Asking OpenStreetMap only for these names
# is far lighter than downloading every restaurant in Arlington.
SEARCH_PATTERNS = {
    "Roggenart European Bakery": "Roggenart",
    "For Five Coffee Roasters": "For Five",
    "Tic-Taco": "Tic.?Taco",
    "Ruthie's All-Day": "Ruthie",
    "Tatte Bakery & Cafe": "Tatte",
    "MidnighTreats": "Midnigh",
    "Brooklyn Bagel Bakery": "Brooklyn Bagel",
    "Paris Baguette": "Paris Baguette",
    "Bayou Bakery": "Bayou Bakery",
    "Inca Social": "Inca Social",
}


def build_query(names):
    """Build a small Overpass query that only looks for the given venue names."""
    patterns = []
    for n in names:
        p = SEARCH_PATTERNS.get(n) or re.split(r"[^A-Za-z]", n)[0]
        patterns.append(p)
    regex = "|".join(patterns)
    s, w, nn, e = BBOX
    return (f'[out:json][timeout:25];\n'
            f'nwr["name"~"{regex}",i]({s},{w},{nn},{e});\n'
            f'out center tags;')


def normalize(text):
    """Lowercase, drop punctuation and common filler words so names compare fairly."""
    text = text.lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    drop = {"the", "restaurant", "cafe", "coffee", "roasters", "bakery", "and", "bistro", "european"}
    words = [w for w in text.split() if w not in drop]
    return " ".join(words) or text


def miles_between(lat1, lon1, lat2, lon2):
    """Haversine distance in miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_json_with_retries(query, attempts_per_server=2):
    """Try each server a couple of times, waiting longer after each failure."""
    last_error = None
    for url in OVERPASS_URLS:
        for attempt in range(attempts_per_server):
            try:
                print(f"  trying {url} (attempt {attempt + 1})...")
                resp = requests.post(url, data={"data": query}, timeout=60,
                                     headers={"User-Agent": "theSpot-analysis (student project)"})
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as err:
                last_error = err
                print(f"    failed: {err}")
                time.sleep(5 * (attempt + 1))
    raise SystemExit(f"All Overpass servers failed. Last error: {last_error}\n"
                     "Wait a few minutes and run the script again.")


def fetch_osm_places(names):
    data = fetch_json_with_retries(build_query(names))
    places = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None or lon is None:
            continue
        street = f"{tags.get('addr:housenumber', '')} {tags.get('addr:street', '')}".strip()
        places.append({"osm_name": tags["name"], "osm_lat": lat, "osm_lon": lon,
                       "osm_address": street, "osm_id": f"{el['type']}/{el['id']}"})
    return places


def best_match(name, places, est_lat=None, est_lon=None):
    """Return (place, score). Among places whose names score about equally well
    (e.g. several branches of a chain), pick the one closest to the estimate."""
    target = normalize(name)
    scored = []
    for p in places:
        other = normalize(p["osm_name"])
        score = difflib.SequenceMatcher(None, target, other).ratio()
        # "Hawkers" should match "Hawkers Asian Street Food": if one name's words
        # all appear in the other, treat it as a strong match.
        t_words, o_words = set(target.split()), set(other.split())
        if t_words and o_words and (t_words <= o_words or o_words <= t_words):
            score = max(score, 0.95)
        scored.append((score, p))
    if not scored:
        return None, 0.0
    top = max(score for score, _ in scored)
    contenders = [(score, p) for score, p in scored if score >= top - 0.03]
    if est_lat is not None:
        contenders.sort(key=lambda sp: miles_between(est_lat, est_lon, sp[1]["osm_lat"], sp[1]["osm_lon"]))
    score, place = contenders[0]
    return place, score


MIN_NAME_SCORE = 0.85  # below this we do not trust the match at all


def match_all(df, places):
    rows = []
    for _, r in df.iterrows():
        place, score = best_match(r["name"], places, r["lat"], r["lon"])
        row = r.to_dict()
        if place is None or score < MIN_NAME_SCORE:
            # Don't show a wrong venue's coordinates as if they were a result.
            row.update({"osm_name": None if place is None else f"(rejected: {place['osm_name']})",
                        "osm_address": None, "osm_lat": None, "osm_lon": None,
                        "match_score": round(score, 2), "miles_from_estimate": None,
                        "status": "no match: look up by hand"})
        else:
            dist = miles_between(r["lat"], r["lon"], place["osm_lat"], place["osm_lon"])
            status = "ok" if dist <= 0.5 else "check: compare address, then decide"
            row.update({**place, "match_score": round(score, 2),
                        "miles_from_estimate": round(dist, 2), "status": status})
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    df = pd.read_csv("data/spots.csv")
    print("Downloading Arlington venues from OpenStreetMap...")
    places = fetch_osm_places(list(df["name"]))
    print(f"Found {len(places)} named venues. Matching your {len(df)} spots...")
    out = match_all(df, places)
    out.to_csv("data/spots_geocoded.csv", index=False)
    cols = ["name", "osm_name", "osm_address", "osm_lat", "osm_lon", "miles_from_estimate", "status"]
    print(out[cols].to_string(index=False))
    print("\nWrote data/spots_geocoded.csv. Review every row marked 'check' or 'no match'.")


if __name__ == "__main__":
    sys.exit(main())
