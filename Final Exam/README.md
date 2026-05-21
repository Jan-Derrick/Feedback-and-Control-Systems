# 🔮 NEXUS PRODUCTIVITY HUB — AI Productivity Agent
Removed credentials.json and tokens.json for security
Please be guided by setupAPIs.md

Nexus Hub is a local AI-powered productivity monitoring system that tracks your active PC windows, evaluates focus against your Google Calendar schedule, and communicates with you in real time via Facebook Messenger.

---

## ✨ Features

| Feature | Description |
|---|---|
| **Live Window Tracking** | Monitors your active application every second and logs it to a local file |
| **Productivity Verdict Engine** | Compares what you're doing against your current scheduled calendar task |
| **Distraction Warnings** | Sends automated Messenger alerts when you drift off-task |
| **Google Calendar Integration** | Reads, checks, and writes events directly to your primary calendar |
| **Daily Summary Reports** | Aggregates your app usage by time and delivers an AI-written focus diagnosis |
| **Smart Schedule Suggester** | Analyzes your activity log and proposes the next best task to work on |
| **Conflict Detection** | Warns you before adding a calendar event that overlaps an existing one |
| **Facebook Messenger Bot** | All commands and notifications are delivered through Messenger |

---

## 🗂️ Project Structure

```
project/
├── Final Exam/
│   ├── nexus.py                  # Main application entry point
│   ├── daily_activity_log.txt    # Auto-generated window tracking log
│   ├── credentials.json          # Google OAuth credentials (not included)
│   └── token.json                # Auto-generated after first Google auth
```

---

## ⚙️ Requirements

### Python Dependencies

Install all required packages with:

```bash
pip install flask requests openai pygetwindow python-dateutil google-auth google-auth-oauthlib google-api-python-client
```

### External Services

- **LM Studio** — Running locally at `http://localhost:1234` with a compatible model loaded (e.g., `meta-llama-3.1-8b-instruct`)
- **Facebook Developer App** — A configured Messenger webhook with a Page Access Token
- **Google Cloud Project** — OAuth 2.0 credentials with the Google Calendar API enabled

---

## 🔧 Configuration

Open `nexus.py` and update the following constants near the top of the file:

```python
FB_PAGE_ACCESS_TOKEN   = "your_facebook_page_access_token"
FB_RECIPIENT_USER_ID   = "your_messenger_user_id"
VERIFY_TOKEN           = "your_custom_webhook_verify_token"
```

Place your Google OAuth credentials file at:

```
Final Exam/credentials.json
```

---

## 🚀 Running the Agent

```bash
python "Final Exam/nexus.py"
```

On first run, a browser window will open for Google Calendar authorization. After approval, a `token.json` file is cached for future sessions.

The agent starts two concurrent processes:
1. **Flask webhook server** on port `5000` — receives Messenger messages
2. **Background tracking daemon** — monitors your active window every second

---

## 💬 Messenger Commands

Send these messages to your connected Facebook Page to control the agent:

| Command | Action |
|---|---|
| `status` | Live snapshot of your active window, session time, today's total time, and current calendar task with AI verdict |
| `summary` | Full breakdown of all apps used today with time durations and an AI focus analysis |
| `suggest schedule` | Analyzes your activity log and proposes a task + finds the next free calendar slot |
| `schedule [description]` | Parses natural language and adds an event to Google Calendar |
| `yes` / `no` | Confirms or cancels a pending calendar conflict override |

### Scheduling Examples

```
schedule "Deep Work Session" from 3pm to 5pm
add "Review Notes" at 10am for 45 minutes
remind me to study at 8pm for 2 hours
```

---

## 🤖 AI Models Used

The system uses two model variants via LM Studio's local OpenAI-compatible endpoint:

| Task | Model |
|---|---|
| Productivity verdict & summary analysis | `meta-llama-3-8b-instruct` |
| Calendar extraction & schedule suggestion | `meta-llama-3.1-8b-instruct` |

You can swap these to any locally available model by editing the `model=` parameter in each `client.chat.completions.create()` call.

---

## 📊 How Activity Tracking Works

1. Every **1 second**, the daemon reads the active foreground window title
2. Every **60 seconds**, it appends a log entry to `daily_activity_log.txt`
3. Browser window titles are parsed to extract the **active tab** (e.g., `YouTube (Google Chrome)`) rather than logging the generic browser name
4. On each window change, the agent **immediately checks** your current calendar task and sends a Messenger warning if a mismatch is detected
5. Repeat warnings for the same app are **throttled to once every 10 minutes**

---

## 📅 Smart Scheduling Flow

When you send `suggest schedule`:

1. The last 30 log entries are sent to the AI for context
2. The AI returns a suggested task title and estimated duration
3. Google Calendar is queried for the next 24 hours to find a **conflict-free time slot**
4. An interactive Messenger card is sent with **Approve & Log** / **Edit Details** buttons
5. Approving the card writes the event to your calendar

---

## ⚠️ Notes & Limitations

- **Windows only** — `pygetwindow` requires Windows for active window detection
- **Local LLM required** — LM Studio must be running before starting the agent
- The `ngrok` tool (or equivalent tunnel) is required to expose your local Flask server to Facebook's webhook infrastructure
- Log entries older than the current day are excluded from all summary calculations
- Sleep/suspend gaps longer than 2 hours in the log are automatically capped at 1 minute to prevent skewed totals

---

## 🔐 Security Reminder

The `credentials.json` and `token.json` files contain sensitive OAuth data. Add them to your `.gitignore` and never commit them to a public repository.

```
# .gitignore
Final Exam/credentials.json
Final Exam/token.json
```

---

## 📄 License

This project is for personal productivity use. Modify and extend freely.
