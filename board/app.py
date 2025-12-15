from flask import Flask, Response
import requests

# PATH Train API
TRAIN_URL = "https://www.panynj.gov/bin/portauthority/ridepath.json"

# NJ Transit Bus API

# Authentication URL 
BUS_AUTH_URL = "https://pcsdata.njtransit.com/api/GTFSRT/authenticateUser"

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
                        except:
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
    files = {
        "username": (None, username),
        "password": (None, password),
    }

    headers = {
        "accept": "text/plain",
    }

    r = requests.post(BUS_AUTH_URL, headers=headers, files=files, timeout=10)
    r.raise_for_status()

    token = r.text.strip().strip('"')

    if not token:
        raise RuntimeError("No token returned from NJ Transit")
        
    return token



# Get NJ Transit bus times  
#def get_buses():

#    r = requests.get(BUS_URL)
#    r.raise_for_status()


#    buses = []

 #   return buses

# Display results on / page

@app.route("/")
def board():
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
