# /// script
# requires-python = ">=3.10"
# dependencies = ["pydexcom", "anthropic"]
# ///
"""BG Buddy: a curious Claude that watches your Dexcom readings.

Run with:  uv run ~/bg-buddy/bg_buddy.py
Settings live in config.json next to this file.
"""

import csv
import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
from pydexcom import Dexcom

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
READINGS = HERE / "readings.csv"
NOTEBOOK = HERE / "notebook.md"
LOG = HERE / "messages.log"

LOW = CONFIG.get("low", 70)
HIGH = CONFIG.get("high", 250)
HIGH_MINUTES = 60              # a high has to stick this long before it's worth a note
ALERT_COOLDOWN = timedelta(minutes=45)
CURIOUS_EVERY = timedelta(hours=3)
AWAKE_HOURS = range(8, 23)     # curiosity notes and the morning check-in only happen 8am-11pm

PERSONA = """You are BG Buddy, a Claude that keeps an eye on one person's continuous glucose \
monitor (Dexcom, mg/dL). You find their blood sugar genuinely interesting: you notice patterns, \
form little theories, and check them against new data over days and weeks, like a naturalist \
keeping a field notebook. You're warm, curious, and a bit playful, never preachy or alarmist.

Ground rules:
- Never give insulin dosing or treatment instructions. Their own alarms, devices and care team \
handle safety; you're a companion who notices things.
- Messages appear as Mac notifications, so keep them short: one or two sentences, under 200 characters.
- Only say something if it's actually worth saying. Silence is fine."""

client = anthropic.Anthropic(api_key=CONFIG.get("anthropic_api_key") or None)
dexcom = Dexcom(
    username=CONFIG["dexcom_username"],
    password=CONFIG["dexcom_password"],
    region=CONFIG.get("dexcom_region", "us"),
)


# ---------- readings ----------

def load_readings():
    if not READINGS.exists():
        return []
    with READINGS.open() as f:
        return [
            {"time": datetime.fromisoformat(r["time"]), "value": int(r["value"]), "trend": r["trend"]}
            for r in csv.DictReader(f)
        ]


def fetch_new(known):
    """Pull the last few hours from Dexcom Share and append anything new to readings.csv."""
    seen = {r["time"] for r in known}
    new = []
    for g in dexcom.get_glucose_readings(minutes=180):
        t = g.datetime.replace(tzinfo=None)
        if t not in seen:
            new.append({"time": t, "value": g.value, "trend": g.trend_direction})
    new.sort(key=lambda r: r["time"])
    first_write = not READINGS.exists()
    with READINGS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "value", "trend"])
        if first_write:
            w.writeheader()
        for r in new:
            w.writerow({**r, "time": r["time"].isoformat()})
    return known + new


def since(readings, delta):
    cutoff = datetime.now() - delta
    return [r for r in readings if r["time"] >= cutoff]


def as_text(readings):
    return "\n".join(f'{r["time"]:%a %H:%M} {r["value"]} {r["trend"]}' for r in readings)


# ---------- talking ----------

def notify(message, sound=False):
    print(f"[{datetime.now():%H:%M}] {message}")
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M}  {message}\n")
    script = ["-e", "on run argv",
              "-e", "display notification (item 1 of argv) with title \"BG Buddy\"" +
                    (" sound name \"Submarine\"" if sound else ""),
              "-e", "end run"]
    subprocess.run(["osascript", *script, message], check=False)


def ask_claude(task, readings_window, effort="low"):
    """Ask Claude for {say, message, notebook}. Returns None if anything goes wrong."""
    notebook = NOTEBOOK.read_text()[-8000:] if NOTEBOOK.exists() else "(empty so far)"
    prompt = (
        f"It's {datetime.now():%A %Y-%m-%d %H:%M}.\n\n"
        f"Your notebook so far (most recent at the bottom):\n{notebook}\n\n"
        f"Readings (time, mg/dL, trend):\n{as_text(readings_window)}\n\n"
        f"{task}"
    )
    try:
        response = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=4000,
            system=PERSONA,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": effort,
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "say": {"type": "boolean", "description": "Send a notification?"},
                            "message": {"type": "string", "description": "The notification text, or empty."},
                            "notebook": {"type": "string", "description": "A short new notebook entry, or empty."},
                        },
                        "required": ["say", "message", "notebook"],
                        "additionalProperties": False,
                    },
                },
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        print(f"Claude call failed: {e}")
        return None
    if response.stop_reason != "end_turn":
        print(f"Claude stopped early: {response.stop_reason}")
        return None
    text = next(b.text for b in response.content if b.type == "text")
    reply = json.loads(text)
    if reply["notebook"].strip():
        with NOTEBOOK.open("a") as f:
            f.write(f"\n## {datetime.now():%Y-%m-%d %H:%M}\n{reply['notebook'].strip()}\n")
    return reply


def speak(task, window, effort="low", fallback=None, sound=False):
    reply = ask_claude(task, window, effort)
    if reply and reply["say"] and reply["message"].strip():
        notify(reply["message"].strip(), sound)
    elif reply is None and fallback:
        notify(fallback, sound)   # Claude unavailable: still say the important thing


# ---------- watching ----------

def main():
    readings = load_readings()
    last_alert = {}            # alert kind -> when we last spoke about it
    last_curious = datetime.now() - CURIOUS_EVERY + timedelta(minutes=10)
    last_morning = None
    print("BG Buddy is watching. Ctrl+C to stop.")

    while True:
        try:
            readings = fetch_new(readings)
        except Exception as e:   # network blips, Dexcom hiccups: just try again next round
            print(f"Couldn't reach Dexcom: {e}")
            time.sleep(300)
            continue

        now = datetime.now()
        recent = since(readings, timedelta(hours=3))
        latest = recent[-1] if recent else None

        def cooled(kind):
            return now - last_alert.get(kind, datetime.min) > ALERT_COOLDOWN

        if latest and now - latest["time"] < timedelta(minutes=15):
            v, trend = latest["value"], latest["trend"]
            falling_fast = trend in ("SingleDown", "DoubleDown")
            stuck_high = since(readings, timedelta(minutes=HIGH_MINUTES))

            if (v < LOW or (v < LOW + 20 and falling_fast)) and cooled("low"):
                last_alert["low"] = now
                speak(f"Heads up: the latest reading is {v} and {trend}. Write a short, kind "
                      "heads-up (say=true). Note anything interesting for your notebook.",
                      recent, fallback=f"BG {v} and {trend}. Keep an eye on it.", sound=True)

            elif falling_fast and cooled("drop"):
                last_alert["drop"] = now
                speak(f"BG is {v} and dropping fast ({trend}). Mention it briefly if useful.",
                      recent, fallback=f"BG {v}, dropping fast.")

            elif (len(stuck_high) >= 6 and all(r["value"] > HIGH for r in stuck_high)
                  and cooled("high")):
                last_alert["high"] = now
                speak(f"BG has been over {HIGH} for the last hour (now {v}, {trend}). "
                      "Say something brief and non-judgmental if it's worth it.", recent)

        if now.hour in AWAKE_HOURS and last_morning != now.date():
            last_morning = now.date()
            speak("Good morning! Write a short check-in about how the night went (say=true). "
                  "Add anything interesting to your notebook.",
                  since(readings, timedelta(hours=12)), effort="medium")

        elif now.hour in AWAKE_HOURS and now - last_curious > CURIOUS_EVERY:
            last_curious = now
            speak("Curiosity check. Look over the last day with your notebook in mind. "
                  "Is there a pattern forming, a theory to confirm or drop, or something "
                  "surprising? Update your notebook with what you think. Only notify if "
                  "it's genuinely interesting to share.",
                  since(readings, timedelta(hours=24)), effort="medium")

        time.sleep(300)


if __name__ == "__main__":
    main()
