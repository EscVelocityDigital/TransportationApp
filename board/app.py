from flask import Flask, Response
import requests
import os
import time
from threading import Lock
from dotenv import load_dotenv
load_dotenv()


# PATH Train API
TRAIN_URL = "https://www.panynj.gov/bin/portauthority/ridepath.json"

# NJ Transit Bus API

# Authentication 
BUS_AUTH_URL = "https://pcsdata.njtransit.com/api/BUSDV2/authenticateUser"

_token_lock = Lock()
_cached_token = None
_cached_token_time = 0.0
TOKEN_TTL_SECONDS = 25 * 60 

app = Flask(__name__)

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

                        seconds = msg.get("secondsToArrival", "")
                        try:
                            seconds = int(seconds)
                        except (TypeError, ValueError):
                            # Fallback if missing
                            seconds = 999999  

                        trains.append({
                            "headsign": msg.get("headSign", ""),
                            "arrival": msg.get("arrivalTimeMessage", ""),
                            "line": msg.get("lineColor", ""),
                            "seconds": seconds,
                            })

    # Sort results in order of departure time
    trains.sort(key=lambda t: t["seconds"])

    return trains

# Bus API AUthentication 
def get_bus_auth(username: str, password: str) -> str:
    files = {"username": (None, username), "password": (None, password)}

    if not username or not password:
        raise RuntimeError("Missing NJT_USERNAME or NJT_PASSWORD")

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

@app.route("/")

def board():

    try:
        token = get_bus_token_cached()
        return Response(
            f"Bus auth OK\nToken prefix: {token[:12]}...\n",
            mimetype="text/plain",
        )

    except Exception as e:
        return Response(
            f"Bus auth FAILED: {e}\n",
            mimetype="text/plain",
            status=500,
        )

    trains = get_trains()
    if not trains:
        body = "No trains found\n"
    else:



        body = "Upcoming trains to NYC leaving JSQ\n\n"

        body += "\n".join(
            f"{t['headsign']:<30} {t['arrival']:>10}"
            for t in trains
            ) + "\n"


    return Response(body, mimetype="text/plain")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
