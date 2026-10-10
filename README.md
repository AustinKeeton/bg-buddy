# BG Buddy

A curious Claude that watches my Dexcom glucose readings and keeps a field notebook about the patterns.

Every five minutes it pulls readings from Dexcom Share. It sends a short Mac notification when something is worth mentioning (a low, a fast drop, a long high), checks in each morning about the night, and every few hours looks over the last day to form (and test) little theories in `notebook.md`, like a naturalist would.

It never gives dosing or treatment advice. My devices, alarms and care team handle safety; this is a companion that notices things.

## Run it

1. Copy `config.example.json` to `config.json` and fill in your Dexcom Share login and an Anthropic API key. (`config.json` is git-ignored.)
2. `uv run bg_buddy.py`

Readings, the notebook and the message log are written next to the script and are git-ignored too.

Built by [Audie Keeton](https://audie.coach).
