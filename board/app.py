from flask import Flask, Response
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

_token_lock = Lock()
_cached_token = None
_cached_token_time = 0.0
TOKEN_TTL_SECONDS = 25 * 60 

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
                    trains.append(
                        {
                            "headsign": msg.get("headSign", ""),
                            "arrival": msg.get("arrivalTimeMessage", ""),
                            "seconds": seconds,
                        }
                    )

    # Sort results in order of departure time
    trains.sort(key=lambda t: t["seconds"])

    return trains


# --- BUSDV2 helpers ---
def bus_post(path: str, fields: dict) -> dict:
    # NJT BUSDV2 endpoints expect multipart form-data in practice
    files = {k: (None, str(v)) for k, v in fields.items() if v is not None}
    r = requests.post(f"{BUSDV2_BASE}/{path}", files=files, timeout=10)
    r.raise_for_status()
    return r.json()


# Bus API AUthentication 
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


# Get the bus departure times
def get_bus_dv(token: str, route: str, stop: str, direction: str) -> dict:
    return bus_post(
        "getBusDV",
        {
            "token": token,
            "route": route,
            "stop": stop,
            "direction": direction,
            "IP": "",
        },
        )


@app.route("/")

def board():

    stop = "20955"
    direction = "Exchange Place"
    routes = ["80", "86"]

    try:

        now = datetime.now().strftime("%I:%M:%S %p")
        refreshed = datetime.now().strftime("%I:%M:%S %p")

        lines = []
        lines.append(f"Current time: {now}")
        lines.append(f"Last refreshed: {refreshed}")
        lines.append("")

        # bus info
        token = get_bus_token_cached()
        lines.append("Route 80 & 86 - Stop Newark & Chestnut")
        lines.append("")

        bus_found = False

        for route in routes:
            data = get_bus_dv(
                token,
                route=route,
                stop=stop,
                direction=direction
            )
            
            for row in data.get("DVTrip", []):
                bus_found = True
                status = row.get("departurestatus", "")
                header = row.get("header", "")
                lines.append(f"{status:>10}  {header}")

        if not bus_found:
            lines.append("No upcoming bus departures")

        # train info
        lines.append("")
        lines.append("Upcoming trains to NYC leaving JSQ")
        lines.append("")

        trains = get_trains()

        if not trains:
            lines.append("No upcoming train departures found")
        else:
            for t in trains[:6]:
                lines.append(f"{t['arrival']:>10} {t['headsign']}")

        body = "\n".join(lines)

        html = f"""
            <html>
              <head>
                <meta http-equiv="refresh" content="15">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                  body {{
                    font-family: monospace;
                    font-size: 20px;
                    padding: 16px;
                    background: black;
                    color: white;
                  }}
                  pre {{
                    white-space: pre-wrap;
                  }}
                </style>
              </head>
              <body>
                <pre>{body}</pre>
              </body>
            </html>
            """


        return Response(html
            ,
            mimetype="text/html",
            )

    except Exception as e:
        return Response(
            f"FAIL: {e}\n",
            mimetype="text/plain",
            status=500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)



