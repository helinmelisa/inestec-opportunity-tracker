# INESC TEC Opportunity Monitor

Monitors [inesctec.pt/en/opportunities](https://www.inesctec.pt/en/opportunities/list?type=open) for new openings and sends email alerts for positions matching your filters. Includes a local web UI and an AI-powered motivation letter writer.

![Python](https://img.shields.io/badge/python-3.9+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Auto-detection** — scrapes the INESC TEC opportunities table on a configurable interval
- **Smart filtering** — matches by academic qualification (Bachelor's) and work area keywords (Computer Vision, AI, ML, NLP, etc.)
- **Email alerts** — sends formatted HTML emails with opportunity details when new matches are found
- **Tracked tab** — live dashboard of active matched opportunities with urgency highlighting (deadlines ≤ 3 days in red, ≤ 7 days in amber)
- **AI letter writer** — generates personalised motivation letters via a local Ollama LLM, streamed in real time
- **No cloud dependency** — fully local; the only outbound connections are to INESC TEC and your SMTP server

## Screenshots

| Dashboard | Tracked Opportunities | Letter Writer |
|-----------|----------------------|---------------|
| Live log, status, controls | Active matches with deadlines | Streaming AI letter generation |

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com) (optional — only needed for letter generation)

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Configure your profile**

```bash
cp profile.example.json profile.json
```

Edit `profile.json` with your name, education, experience, and skills. This is used to personalise generated motivation letters.

**3. Install an Ollama model** *(optional)*

```bash
# Install Ollama from https://ollama.com, then:
ollama pull llama3.1:8b
```

**4. Run**

```bash
python3 monitor.py
```

The web UI opens automatically at `http://localhost:8766`. Configure your email settings in the **Settings** tab, then click **Start Monitoring**.

## Auto-start on macOS

To run the monitor silently on every login:

```bash
cp com.inesctec.monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.inesctec.monitor.plist
```

Logs are written to `monitor.log` (auto-rotated at 500 lines).

## Letter Writer

The letter writer runs as a separate server on port 8767:

```bash
python3 letter_writer.py
```

Or open it directly from the **Tracked** tab by clicking the **✦ Letter** button on any opportunity — fields are pre-filled automatically.

## Filtering

Edit the keyword lists at the top of `monitor.py` to customise what gets matched:

```python
WORK_AREA_KEYWORDS = [
    "computer vision", "machine learning", "artificial intelligence", ...
]
QUALIFICATION_KEYWORDS = ["bachelor"]
```

## Project structure

```
inesctec_monitor/
├── monitor.py              # Main monitor + web UI (port 8766)
├── letter_writer.py        # Standalone letter writer UI (port 8767)
├── letter_generator.py     # Ollama API wrapper
├── profile.example.json    # Profile template — copy to profile.json
├── requirements.txt
└── com.inesctec.monitor.plist  # macOS LaunchAgent
```

## Email setup (Gmail)

Use a [Gmail App Password](https://myaccount.google.com/apppasswords) — not your regular password. Enter it in the Settings tab of the web UI.

## License

MIT
