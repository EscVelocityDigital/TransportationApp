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
LOCATION_RADIUS_DEG = 0.072

# AviationStack API
AVIATIONSTACK_BASE = "http://api.aviationstack.com/v1"
AVIATIONSTACK_FLIGHT_TTL_SECONDS = 10 * 60  # cache each flight lookup for 10 min

# OpenSky aircraft metadata
OPENSKY_METADATA_URL = "https://opensky-network.org/api/metadata/aircraft/icao"
AIRCRAFT_META_TTL_SECONDS = 24 * 60 * 60  # aircraft type rarely changes, cache for 24h
_aircraft_meta_cache: dict = {}  # keyed by icao24

# ICAO -> IATA airline code mapping for common carriers near EWR/JFK/LGA
ICAO_TO_IATA = {
    "AAL": "AA",  # American
    "AFR": "AF",  # Air France
    "AMX": "AM",  # Aeromexico
    "ASA": "AS",  # Alaska
    "AUA": "OS",  # Austrian
    "AZA": "IZ",  # Alitalia (historic)
    "BAW": "BA",  # British Airways
    "BWA": "BW",  # Caribbean Airlines
    "CAL": "CI",  # China Airlines
    "CLX": "CV",  # Cargolux
    "CNW": "KR",  # Caribbean Sun
    "CPZ": "CP",  # Compass Airlines
    "CPA": "CX",  # Cathay Pacific
    "DAL": "DL",  # Delta
    "DLH": "LH",  # Lufthansa
    "EIN": "EI",  # Aer Lingus
    "EJA": "EJ",  # NetJets
    "ENY": "MQ",  # Envoy/American Eagle
    "ETD": "EY",  # Etihad
    "ETH": "ET",  # Ethiopian
    "EWG": "EW",  # Eurowings
    "FDX": "FX",  # FedEx
    "FFT": "F9",  # Frontier
    "GTI": "GT",  # Atlas Air
    "HAL": "HA",  # Hawaiian
    "IBE": "IB",  # Iberia
    "JBU": "B6",  # JetBlue
    "KAL": "KE",  # Korean Air
    "KLM": "KL",  # KLM
    "LXJ": "XJ",  # Flexjet
    "NKS": "NK",  # Spirit
    "PDT": "OE",  # Piedmont
    "PSA": "KS",  # PSA Airlines
    "QTR": "QR",  # Qatar
    "QXE": "QX",  # Horizon Air
    "RPA": "YX",  # Republic Airways
    "SKW": "OO",  # SkyWest
    "SWA": "WN",  # Southwest
    "SWR": "LX",  # Swiss
    "THY": "TK",  # Turkish
    "UAL": "UA",  # United
    "UPS": "5X",  # UPS
    "VIR": "VS",  # Virgin Atlantic
}

_token_lock = Lock()
_cached_token = None
_cached_token_time = 0.0
TOKEN_TTL_SECONDS = 25 * 60

_opensky_token_lock = Lock()
_opensky_cached_token = None
_opensky_cached_token_time = 0.0
OPENSKY_TOKEN_TTL_SECONDS = 4 * 60  # tokens expire in 5 min, refresh at 4

_flight_info_cache: dict = {}  # keyed by callsign -> {data, fetched_at}

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


# Look up flight details from AviationStack by ICAO callsign, with caching.
# Falls back to IATA lookup if the ICAO query returns nothing.
def get_aviationstack_flight(callsign: str) -> dict:
    api_key = os.getenv("AVIATIONSTACK_API_KEY")
    if not api_key or not callsign:
        return {}

    now = time.time()
    cached = _flight_info_cache.get(callsign)
    if cached and (now - cached["fetched_at"]) < AVIATIONSTACK_FLIGHT_TTL_SECONDS:
        return cached["data"]

    def query(param, value):
        r = requests.get(
            f"{AVIATIONSTACK_BASE}/flights",
            params={"access_key": api_key, param: value},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("data") or []
        return results[0] if results else {}

    try:
        info = query("flight_icao", callsign)

        if not info:
            # Try converting ICAO airline prefix to IATA and retry
            icao_prefix = callsign[:3].upper()
            flight_number = callsign[3:]
            iata_prefix = ICAO_TO_IATA.get(icao_prefix)
            if iata_prefix and flight_number:
                info = query("flight_iata", f"{iata_prefix}{flight_number}")
    except Exception:
        info = {}

    _flight_info_cache[callsign] = {"data": info, "fetched_at": now}
    return info


# Friendly names for common ICAO type codes
TYPECODE_NAMES = {
    # Boeing
    "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",
    "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",
    "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8",
    "B39M": "Boeing 737 MAX 9",
    "B752": "Boeing 757-200",
    "B753": "Boeing 757-300",
    "B762": "Boeing 767-200",
    "B763": "Boeing 767-300",
    "B764": "Boeing 767-400",
    "B772": "Boeing 777-200",
    "B773": "Boeing 777-300",
    "B77W": "Boeing 777-300ER",
    "B788": "Boeing 787-8 Dreamliner",
    "B789": "Boeing 787-9 Dreamliner",
    "B78X": "Boeing 787-10 Dreamliner",
    "B744": "Boeing 747-400",
    "B748": "Boeing 747-8",
    # Airbus
    "A19N": "Airbus A319neo",
    "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",
    "A318": "Airbus A318",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
    "A332": "Airbus A330-200",
    "A333": "Airbus A330-300",
    "A338": "Airbus A330-800neo",
    "A339": "Airbus A330-900neo",
    "A359": "Airbus A350-900",
    "A35K": "Airbus A350-1000",
    "A388": "Airbus A380",
    # Bombardier
    "CL30": "Bombardier Challenger 300",
    "CL35": "Bombardier Challenger 350",
    "CL60": "Bombardier Challenger 600",
    "CRJ2": "Bombardier CRJ-200",
    "CRJ7": "Bombardier CRJ-700",
    "CRJ9": "Bombardier CRJ-900",
    "CRJX": "Bombardier CRJ-1000",
    "GLEX": "Bombardier Global Express",
    "GL7T": "Bombardier Global 7500",
    "GL5T": "Bombardier Global 5000",
    # Embraer
    "E170": "Embraer E170",
    "E175": "Embraer E175",
    "E190": "Embraer E190",
    "E195": "Embraer E195",
    "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2",
    "E35L": "Embraer Legacy 600",
    "E55P": "Embraer Phenom 300",
    # Gulfstream
    "GLF4": "Gulfstream IV",
    "GLF5": "Gulfstream V",
    "GLF6": "Gulfstream G650",
    "G280": "Gulfstream G280",
    # Cessna
    "C25A": "Cessna Citation CJ2",
    "C25B": "Cessna Citation CJ3",
    "C25C": "Cessna Citation CJ4",
    "C56X": "Cessna Citation Excel",
    "C680": "Cessna Citation Sovereign",
    "C68A": "Cessna Citation Longitude",
    "C750": "Cessna Citation X",
    # Other
    "DH8D": "Dash 8-400",
    "MD11": "McDonnell Douglas MD-11",
    "MD82": "McDonnell Douglas MD-82",
    "MD83": "McDonnell Douglas MD-83",
    "LJ60": "Learjet 60",
    "LJ75": "Learjet 75",
    "PC12": "Pilatus PC-12",
    "BE20": "Beechcraft King Air 200",
}


# Look up aircraft model from OpenSky metadata by icao24 transponder code
def get_aircraft_model(icao24: str) -> str:
    if not icao24:
        return ""

    now = time.time()
    cached = _aircraft_meta_cache.get(icao24)
    if cached and (now - cached["fetched_at"]) < AIRCRAFT_META_TTL_SECONDS:
        return cached["model"]

    try:
        token = get_opensky_token()
        r = requests.get(
            f"{OPENSKY_METADATA_URL}/{icao24}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 404:
            model = ""
        else:
            r.raise_for_status()
            data = r.json()
            typecode = (data.get("typecode") or "").strip().upper()
            manufacturer = (data.get("manufacturerName") or "").strip()
            raw_model = (data.get("model") or "").strip()
            # Use friendly name if we have one, otherwise combine manufacturer + model
            if typecode in TYPECODE_NAMES:
                model = TYPECODE_NAMES[typecode]
            elif manufacturer and raw_model:
                model = f"{manufacturer} {raw_model}"
            else:
                model = raw_model
    except Exception:
        model = ""

    _aircraft_meta_cache[icao24] = {"model": model, "fetched_at": now}
    return model


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
        flight = dict(zip(fields, state))
        callsign = (flight.get("callsign") or "").strip()
        if not callsign:
            continue

        av = get_aviationstack_flight(callsign)
        flight["airline"] = (av.get("airline") or {}).get("name", "")
        flight["airline_iata"] = (av.get("airline") or {}).get("iata", "")
        flight["flight_iata"] = (av.get("flight") or {}).get("iata", "")
        flight["departure_airport"] = (av.get("departure") or {}).get("airport", "")
        flight["departure_iata"] = (av.get("departure") or {}).get("iata", "")
        flight["arrival_airport"] = (av.get("arrival") or {}).get("airport", "")
        flight["arrival_iata"] = (av.get("arrival") or {}).get("iata", "")
        flight["aircraft"] = get_aircraft_model(flight.get("icao24", ""))

        # Skip flights with no AviationStack data
        if not flight["airline"]:
            continue

        flights.append(flight)

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

    try:

        now = datetime.now().strftime("%I:%M:%S %p")
        refreshed = datetime.now().strftime("%I:%M:%S %p")

        # bus info — empty route/direction returns all buses at the stop
        token = get_bus_token_cached()
        data = get_bus_dv(token, route="", stop=stop, direction="")
        buses = [
            {
                "status": row.get("departurestatus", ""),
                "header": row.get("header", ""),
            }
            for row in data.get("DVTrip", [])
        ]

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
