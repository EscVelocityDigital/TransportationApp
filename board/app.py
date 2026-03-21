from flask import Flask, Response, render_template
import requests
import os
import time
from datetime import datetime
from threading import Lock
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# PATH Train API
TRAIN_URL = "https://www.panynj.gov/bin/portauthority/ridepath.json"

# NJ Transit Bus API
BUSDV2_BASE = "https://pcsdata.njtransit.com/api/BUSDV2"
BUS_AUTH_URL = f"{BUSDV2_BASE}/authenticateUser"

# OpenSky API
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
LOCATION_LAT = 40.72988255549963
LOCATION_LON = -74.05870460265524
LOCATION_RADIUS_DEG = 0.5

_token_lock = Lock()
_cached_token = None
_cached_token_time = 0.0
TOKEN_TTL_SECONDS = 25 * 60

_opensky_token_lock = Lock()
_opensky_cached_token = None
_opensky_cached_token_time = 0.0
OPENSKY_TOKEN_TTL_SECONDS = 4 * 60  # tokens expire in 5 min, refresh at 4

# Get train times
def get_trains():
    r = requests.get(TRAIN_URL, timeout=10)
    r.raise_for_status()
    data = r.json()

    trains = []

    for item in data.get("results", []):

        # Filter to show only JSQ trains
        if item.get("consideredStation") == "JSQ":

            for dest in item.get("destinations", []):

                # Filter to show only trains to NY
                if dest.get("label") == "ToNY":

                    for msg in dest.get("messages", []):
                        seconds_raw = msg.get("secondsToArrival")
                        try:
                            seconds = int(seconds_raw)
                        except (TypeError, ValueError):
                            seconds = 999999

                        line = (msg.get("lineColor") or "").strip()

                        trains.append(
                            {
                                "headsign": msg.get("headSign", ""),
                                "arrival": msg.get("arrivalTimeMessage", ""),
                                "seconds": seconds,
                                "line": line,
                            }
                        )

    # Sort results in order of departure time
    trains.sort(key=lambda t: t["seconds"])

    return trains


# Bus API Authentication
def get_bus_auth(username: str, password: str) -> str:

    if not username or not password:
        raise RuntimeError("Missing NJT_USERNAME or NJT_PASSWORD")

    files = {"username": (None, username), "password": (None, password)}

    r = requests.post(BUS_AUTH_URL, files=files, timeout=10)
    r.raise_for_status()

    data = r.json()
    authenticated = str(data.get("Authenticated", "")).lower() == "true"
    token = (data.get("UserToken") or "").strip()

    if not authenticated:
        raise RuntimeError("NJ Transit auth rejected credentials (Authenticated=False).")

    if not token:
        raise RuntimeError("Authenticated=True but UserToken is empty.")

    return token


# Cache the returned token
def get_bus_token_cached() -> str:
    global _cached_token, _cached_token_time

    username = os.getenv("NJT_USERNAME")
    password = os.getenv("NJT_PASSWORD")

    if not username or not password:
        raise RuntimeError("Missing NJT_USERNAME or NJT_PASSWORD environment variables")

    now = time.time()
    with _token_lock:
        if _cached_token and (now - _cached_token_time) < TOKEN_TTL_SECONDS:
            return _cached_token

        token = get_bus_auth(username, password)
        _cached_token = token
        _cached_token_time = now
        return token


# OpenSky token
def get_opensky_token() -> str:
    global _opensky_cached_token, _opensky_cached_token_time

    client_id = os.getenv("OPENSKY_CLIENT_ID")
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError("Missing OPENSKY_CLIENT_ID or OPENSKY_CLIENT_SECRET")

    now = time.time()
    with _opensky_token_lock:
        if _opensky_cached_token and (now - _opensky_cached_token_time) < OPENSKY_TOKEN_TTL_SECONDS:
            return _opensky_cached_token

        r = requests.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10,
        )
        r.raise_for_status()
        _opensky_cached_token = r.json()["access_token"]
        _opensky_cached_token_time = now
        return _opensky_cached_token


# Get flights overhead
def get_flights_overhead() -> list:
    token = get_opensky_token()
    params = {
        "lamin": LOCATION_LAT - LOCATION_RADIUS_DEG,
        "lomin": LOCATION_LON - LOCATION_RADIUS_DEG,
        "lamax": LOCATION_LAT + LOCATION_RADIUS_DEG,
        "lomax": LOCATION_LON + LOCATION_RADIUS_DEG,
    }
    r = requests.get(
        OPENSKY_STATES_URL,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    fields = [
        "icao24", "callsign", "origin_country", "time_position", "last_contact",
        "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
        "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
        "spi", "position_source",
    ]

    flights = []
    for state in (data.get("states") or []):
        flights.append(dict(zip(fields, state)))

    return flights


# Get the bus departure times
def get_bus_dv(token: str, route: str, stop: str, direction: str) -> dict:
    fields = {
        "token": token,
        "route": route,
        "stop": stop,
        "direction": direction,
    }
    files = {k: (None, str(v)) for k, v in fields.items()}
    r = requests.post(f"{BUSDV2_BASE}/getBusDV", files=files, timeout=10)
    r.raise_for_status()
    return r.json()


@app.route("/")
def board():

    stop = "20955"
    direction = "Exchange Place"
    routes = ["80", "86"]

    try:

        now = datetime.now().strftime("%I:%M:%S %p")
        refreshed = datetime.now().strftime("%I:%M:%S %p")

        # bus info
        token = get_bus_token_cached()
        buses = []

        for route in routes:
            data = get_bus_dv(
                token,
                route=route,
                stop=stop,
                direction=direction
            )

            for row in data.get("DVTrip", []):
                buses.append({
                    "status": row.get("departurestatus", ""),
                    "header": row.get("header", ""),
                })

        # train info
        trains = get_trains()

        # flights overhead
        flights = get_flights_overhead()

        return render_template(
            "board.html",
            buses=buses,
            now=now,
            refreshed=refreshed,
            trains=trains,
            flights=flights,
            refresh_seconds=15,
        )

    except Exception as e:
        return Response(
            f"FAIL: {e}\n",
            mimetype="text/plain",
            status=500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
