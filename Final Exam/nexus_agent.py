import os
import time
import datetime
from datetime import datetime, timedelta, time as dtime, date
import threading
import requests
import re
import json
import pygetwindow as gw
from flask import Flask, request, jsonify
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from openai import OpenAI
import dateutil.parser as dparser

app = Flask(__name__)

# --- CONFIGURATION CREDENTIALS ---
FB_PAGE_ACCESS_TOKEN = "" # Removed for security - Insert your Facebook Page Access Token here for Messenger API interactions
FB_RECIPIENT_USER_ID = "26869291139407323"
VERIFY_TOKEN = "NEXUS_SECRET_WEBHOOK_TOKEN"
SCOPES = ['https://www.googleapis.com/auth/calendar']

LOG_FILE = "Final Exam/daily_activity_log.txt"
TRACKING_INTERVAL_SECONDS = 60

# Pointing to LM Studio Local Backend
client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")

# Global thread-safe tracking states
state_lock = threading.Lock()
PENDING_SUGGESTION = {}
CURRENT_WINDOW_TITLE = ""
CURRENT_WINDOW_START_TIME = None

TOTAL_TIME_ACCUMULATOR = {}  # Tracks daily cumulative application presence 
LAST_TICK_TIME = None        # Tail tracking line for time updates

OVERRIDE_CACHE = {}          # Caches parsed data when a conflict arises awaiting Y/N reply
PROCESSED_MID_CACHE = {}     # Deduplication cache: stores {messaging_id: timestamp}

# Global mismatch warning rate-limiting tracking elements
LAST_WARN_TIME = datetime.min
LAST_WARNED_APP = ""

# --- SYSTEM MONITORING UTILITIES ---
def get_clean_app_name(window_title: str) -> str:
    """
    Parses active window titles to isolate specific websites/tabs from browsers.
    Differentiates "YouTube" or "Instagram" from general browser containers to inform the AI.
    """
    if not window_title:
        return "Unknown Application"
    
    parts = [p.strip() for p in window_title.split(' - ') if p.strip()]
    if not parts:
        return "Unknown Application"
    
    browsers = ["Google Chrome", "Mozilla Firefox", "Microsoft Edge", "Brave", "Opera", "Safari", "Chrome"]
    last_part = parts[-1]
    
    # Check if the active application is a known web browser
    is_browser = any(b.lower() in last_part.lower() for b in browsers)
    
    if is_browser:
        if len(parts) >= 3:
            # Example: ["Video Title", "YouTube", "Google Chrome"] -> "YouTube (Google Chrome)"
            return f"{parts[-2]} ({last_part})"
        elif len(parts) == 2:
            # Example: ["Instagram", "Google Chrome"] -> "Instagram (Google Chrome)"
            return f"{parts[0]} ({last_part})"
        else:
            return last_part
    else:
        # For non-browser utilities (e.g. VS Code, Discord), group by the main workspace name
        return last_part

def log_activity(window_title: str):
    """Appends window logs with local timestamps to text file tracking records."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if window_title and window_title != "Desktop Environment":
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] Active Window: {window_title}\n")

def get_pc_context() -> str:
    """Fetches current foreground window title name securely."""
    try:
        window_title = gw.getActiveWindowTitle()
        if window_title:
            log_activity(window_title)
            return window_title
        return "Desktop Environment"
    except Exception:
        return "Unknown Active App"

# --- FB MESSENGER INTERFACES ---
def send_messenger_text(recipient_id: str, message_text: str):
    """Dispatches plain-text messaging strings via Meta API."""
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to drop text push out: {e}")

def send_interactive_suggestion(recipient_id: str, summary: str, duration: int, target_day="today"):
    """Sends button-action interactive UI cards for direct mobile registration confirmation."""
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    
    header = "🔮 *NEXUS TOMORROW PROJECTION*" if target_day == "tomorrow" else "📅 *NEXUS PROPOSED BLOCK*"
    timeline_desc = "Target Schedule: Tomorrow Focus Window" if target_day == "tomorrow" else f"Duration Segment: {duration} mins"
    
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": f"{header}\n\nActivity: {summary}\n{timeline_desc}\n\nDo you want to write this entry to your Google Calendar?",
                    "buttons": [
                        {"type": "postback", "title": "✅ Approve & Log", "payload": "CONFIRM_CALENDAR"},
                        {"type": "postback", "title": "✏️ Edit Details", "payload": "EDIT_CALENDAR"}
                    ]
                }
            }
        }
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to deploy interactive push card UI: {e}")

# --- GOOGLE CALENDAR CONTROLLER CONNECTIONS ---
def get_calendar_service():
    """Builds and authenticates Google Calendar instance connections via standard local token caching."""
    creds = None
    if os.path.exists('Final Exam/token.json'):
        creds = Credentials.from_authorized_user_file('Final Exam/token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('Final Exam/credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('Final Exam/token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

def check_calendar_overlap(start_iso: str, end_iso: str):
    """Validates if a target timezone period hits against a scheduled block configuration."""
    try:
        service = get_calendar_service()
        events_result = service.events().list(
            calendarId='primary',
            timeMin=start_iso,
            timeMax=end_iso,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        print(f"Error executing calendar lookup validations: {e}")
        return []

# --- PRODUCTIVITY VERDICT GENERATOR ---
def compute_productivity_verdict(active_window: str, scheduled_task: str) -> str:
    """
    STRICT PRODUCTIVITY AGENT:
    1. Entertainment/Social Media = Immediate Mismatch.
    2. Coding/Documentation/Research Tools = Productive Match.
    """
    if not scheduled_task or "none" in scheduled_task.lower() or scheduled_task == "None (Free Time)":
        return "☕ You have nothing scheduled right now. Enjoy your coding or break!"
        
    lower_active = active_window.lower()
    
    # 1. THE ENTERTAINMENT BLACKLIST (The "Kill Switch")
    entertainment_sites = ["youtube", "netflix", "twitch", "facebook", "instagram", "tiktok", "twitter", "game", "gaming", "spotify", "hulu"]
    if any(kwd in lower_active for kwd in entertainment_sites):
        return f"🔴 [Mismatch]: {active_window} is categorized as entertainment and is not permitted during your '{scheduled_task}' block."

    # 2. THE PRODUCTIVITY WHITELIST (The "Necessary Tools")
    productive_tools = ["gemini", "chatgpt", "github", "stackoverflow", "docs", "w3schools", "vs code", "visual studio", "cursor"]
    if any(kwd in lower_active for kwd in productive_tools):
        return f"🟢 [Match]: You are using a productive tool ({active_window}) which supports your goal: '{scheduled_task}'."

    # 3. AI COGNITIVE EVALUATION (Only for non-listed apps)
    prompt = f"""
    You are a strict productivity coach.
    Active Window: "{active_window}"
    Scheduled Goal: "{scheduled_task}"
    
    If the active window is directly relevant to the project (e.g. IDE, documentation, AI coding assistant, research paper), output '🟢 [Match]'.
    If the active window is unrelated (e.g. personal email, random browser tabs, news sites, online shopping), output '🔴 [Mismatch]'.
    
    Keep the verdict to 1 sentence. Do not rationalize entertainment.
    """
    try:
        response = client.chat.completions.create(
            model="meta-llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ Evaluation failed: {str(e)}"

# --- EXTRACTOR LOGIC BASES (Enforced JSON Format) ---
def parse_activity_with_nexus_ai(user_message):
    """
    Fortified calendar extraction engine with Regex JSON isolation 
    to prevent conversational LLM fluff from triggering fallback clauses.
    Compatible with backends requiring "text" response formatting.
    """
    now_now = datetime.now()
    current_time_str = now_now.strftime("%Y-%m-%d %H:%M (%A)")
    current_date_iso = now_now.strftime("%Y-%m-%d")

    # 1. High-contrast prompt forcing strict extraction boundaries
    prompt = f"""
    You are a precise database tool. You must output raw JSON data only.
    Do NOT write conversational text, introductions, or code markdown block syntax.

    Current Context:
    - Base Time: {current_time_str}
    - Base Date: {current_date_iso}

    Target Input String: "{user_message}"
    
    Output exactly this structure filled with values from the message:
    {{
       "extracted_summary": "The string inside double quotes",
       "extracted_start": "The raw start time text",
       "extracted_end": "The raw end time text or duration"
    }}
    """
    
    # Pre-computation Baseline Fallback (Regex intercepts if the LLM entirely crashes)
    fallback_title = "Tracked Activity"
    quote_match = re.search(r'"([^"]*)"', user_message)
    if quote_match and quote_match.group(1).strip():
        fallback_title = quote_match.group(1).strip()
    elif "schedule" in user_message.lower():
        fallback_title = re.sub(r'(?i)set\s+schedule|add\s+schedule|schedule', '', user_message).strip()

    fallback_data = {
        'summary': fallback_title if fallback_title else 'Tracked Activity',
        'start_time': now_now.strftime("%Y-%m-%dT%H:%M:%S"),
        'end_time': (now_now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S"),
        'description': 'Logged via Nexus Hub Engine Fallback'
    }

    try:
        response = client.chat.completions.create(
            model="meta-llama-3.1-8b-instruct", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "text"}
        )
        
        raw_content = response.choices[0].message.content.strip()
        print(f"DEBUG 1 - Absolute Raw AI Output:\n{raw_content}")

        # 2. Advanced JSON Object Extraction Regex
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if json_match:
            clean_json_string = json_match.group(0)
            parsed_json = json.loads(clean_json_string)
        else:
            print("⚠️ Regex Error: No JSON boundaries found in model output. Forcing Fallback Engine.")
            raise ValueError("No JSON object located")

        # Normalize casing across all keys
        normalized_json = {str(k).lower(): v for k, v in parsed_json.items()}
        
        summary = (normalized_json.get("extracted_summary") or 
                   normalized_json.get("summary") or 
                   normalized_json.get("title"))
        
        raw_start = (normalized_json.get("extracted_start") or 
                     normalized_json.get("start"))
        
        raw_end = (normalized_json.get("extracted_end") or 
                   normalized_json.get("end"))

        # Verify string length validity to bypass placeholder string bugs
        if quote_match and (not summary or len(str(summary)) > len(fallback_title) or "enclosed" in str(summary)):
            summary = fallback_title
        elif summary:
            summary = str(summary).replace('"', '').replace("'", "").strip()

        # String Isolation Filter for Clean Dateutil Evaluation
        def clean_time_string(raw_val):
            if not raw_val or "the raw" in str(raw_val).lower() or "extracted" in str(raw_val).lower():
                return ""
            return str(raw_val).lower().replace("from", "").replace("to", "").replace("at", "").strip()

        clean_start = clean_time_string(raw_start)
        clean_end = clean_time_string(raw_end)
        
        print(f"DEBUG 2 - Filtered Strings -> Start: '{clean_start}', End: '{clean_end}'")

        # 3. Time Processing Engine
        if clean_start:
            try:
                start_dt = dparser.parse(clean_start, default=now_now, fuzzy=True)
            except Exception as start_err:
                print(f"⚠️ Start time parsing failed: {start_err}")
                start_dt = now_now
        else:
            start_dt = now_now

        end_dt = None
        if clean_end:
            # Check for duration patterns ("75 minutes", "2 hours")
            minute_match = re.search(r'(\d+)\s*(?:min|minute|mins)', clean_end)
            hour_match = re.search(r'(\d+)\s*(?:hr|hour|hrs)', clean_end)
            
            if minute_match:
                end_dt = start_dt + timedelta(minutes=int(minute_match.group(1)))
            elif hour_match:
                end_dt = start_dt + timedelta(hours=int(hour_match.group(1)))
            else:
                try:
                    end_dt = dparser.parse(clean_end, default=start_dt, fuzzy=True)
                    if end_dt <= start_dt:
                        end_dt = end_dt + timedelta(days=1)
                except Exception as end_err:
                    print(f"⚠️ End time parsing failed: {end_err}")
                    end_dt = None

        if not end_dt:
            end_dt = start_dt + timedelta(minutes=30)

        final_data = {
            'summary': str(summary).strip() if summary else "Tracked Activity",
            'start_time': start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            'end_time': end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            'description': 'Logged via Nexus JSON Processing Pipeline'
        }
        
        print(f"DEBUG 3 - Final Output Object: {final_data}")
        return final_data

    except Exception as err:
        print(f"🔴 System Processing Pipeline Failure: {err}")
        return fallback_data

# --- ACTIVITY LOG PARSING SUMMARY ENGINES ---
def generate_daily_summary_from_log():
    """
    Parses active window entries inside daily_activity_log.txt,
    filters strictly for TODAY, computes time deltas chronologically,
    and prepares an aggregated summary including a Grand Total.
    """
    today_date = date.today()
    paths_to_try = [LOG_FILE, "daily_activity_log.txt"]
    lines = []
    
    for p in paths_to_try:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                break
            except Exception as read_err:
                print(f"⚠️ Error reading from active path {p}: {read_err}")
    
    if not lines:
        return "No tracking log records located inside daily_activity_log.txt yet.", {}

    app_durations = {} # app_name -> total accumulated seconds
    parsed_entries = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Format parser matching: [2026-05-21 08:20:15] Active Window: VS Code OR [08:20] VS Code
        match = re.match(r'^\[(.*?)\]\s*(?:Active Window:\s*)?(.*)$', line)
        if match:
            time_str, window_title = match.groups()
            window_title = window_title.strip()
            try:
                entry_time = dparser.parse(time_str)
                # Strict check: only include logs where date matches today exactly
                if entry_time.date() == today_date:
                    parsed_entries.append((entry_time, window_title))
            except Exception:
                pass

    # Ensure chronological order
    parsed_entries.sort(key=lambda x: x[0])

    if not parsed_entries:
        return "No active focus tracked strictly today.", {}

    # Calculate exact delta durations between successive entry logs
    for i in range(len(parsed_entries) - 1):
        t1, app1 = parsed_entries[i]
        t2, _ = parsed_entries[i+1]
        diff = (t2 - t1).total_seconds()
        
        # Guard against system suspend/sleep downtime periods (cap tracking gap at 2 hours)
        if diff > 7200:
            diff = 60 # set to a default baseline log minute
            
        # Parse cleanly to isolate specific browser tabs
        clean_app = get_clean_app_name(app1)
        app_durations[clean_app] = app_durations.get(clean_app, 0) + diff

    # Account for current elapsed segment of the final tracked window
    if parsed_entries:
        last_t, last_app = parsed_entries[-1]
        now = datetime.now()
        diff = (now - last_t).total_seconds()
        if 0 < diff < 3600: # standard focus block cap
            clean_app = get_clean_app_name(last_app)
            app_durations[clean_app] = app_durations.get(clean_app, 0) + diff

    # Formatting structured output reports
    summary_lines = []
    for app_name, seconds in sorted(app_durations.items(), key=lambda x: x[1], reverse=True):
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        summary_lines.append(f"▪️ *{app_name}:* {mins}m {secs}s")

    # Math Grand Total Compilation
    grand_total_seconds = sum(app_durations.values())
    grand_total_str = f"{int(grand_total_seconds // 60)}m {int(grand_total_seconds % 60)}s"

    summary_text = "\n".join(summary_lines) if summary_lines else "No measurable active focus tracked today."
    summary_text += f"\n\n⏱️ *Grand Total Active Time Today:* {grand_total_str}"

    return summary_text, app_durations

def compute_summary_verdict(summary_text: str) -> str:
    """
    Invokes the LLM to inspect the aggregated daily activity log, 
    diagnosing focus performance and providing objective behavioral verdicts.
    """
    prompt = f"""
    You are an elite productivity analyst and cognitive performance coach.
    Below is a breakdown of the user's active application focus today derived from file logs:

    {summary_text}

    Analyze this breakdown to diagnose their efficiency:
    1. Identify if they are staying on task or spending excessive focus on distractions (e.g., Idle states, empty backgrounds, non-productive titles).
    2. Deliver an actionable verdict: tell them if they are doing great, or if they need further focus/coaching to realign.
    
    Provide your response in 3 clear sentences maximum. Be highly objective and motivating. Do not use markdown backticks.
    """
    try:
        response = client.chat.completions.create(
            model="meta-llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ Unable to synthesize productivity verdict: {str(e)}"

def get_suggested_schedule_from_log():
    """
    Extracts log history from daily_activity_log.txt and queries the LLM
    to suggest an optimal scheduling entry for today based on recent habits.
    """
    paths_to_try = [LOG_FILE, "daily_activity_log.txt"]
    lines = []
    for p in paths_to_try:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-30:]  # Grab the last 30 logs for recent window context
                break
            except Exception:
                pass
                
    log_context = "".join(lines) if lines else "No desktop activity log records found for today."
    
    prompt = f"""
    You are a proactive, highly advanced cognitive task planner and schedule architect.
    Analyze the user's active application history logs below to identify what project, task, or topic they should continue, focus on next, or review today.
    
    Recent PC Windows Activity History:
    {log_context}

    Formulate one highly practical suggested study or work schedule entry for the user.
    Output exactly in this JSON format and absolutely nothing else:
    {{
       "suggested_task": "A concise, specific task title (e.g., 'Program ESP32 Logic' or 'Review Engineering Math Concepts')",
       "duration_minutes": 45
    }}
    """
    try:
        response = client.chat.completions.create(
            model="meta-llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            response_format={"type": "text"}
        )
        raw_content = response.choices[0].message.content.strip()
        print(f"DEBUG SUGGESTION - AI Suggestions Output:\n{raw_content}")
        
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            task = parsed.get("suggested_task", "Focus Session").strip()
            duration = int(parsed.get("duration_minutes", 60))
            return task, duration
        raise ValueError("Valid suggestion JSON structure not found.")
    except Exception as e:
        print(f"⚠️ Proactive schedule suggesting pipeline failed: {e}")
        return "Engineering Focus Session", 60

def find_next_free_slot(duration_mins: int):
    """
    Queries Google Calendar for schedules over the next 24 hours, parses them, 
    and returns the first chronological block of 'duration_mins' with zero overlaps.
    """
    now = datetime.now()
    lookahead_limit = now + timedelta(hours=24)
    
    # Request calendar events in localized Manila Offset formats
    start_iso = now.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
    end_iso = lookahead_limit.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
    
    conflicts = check_calendar_overlap(start_iso, end_iso)
    
    if not conflicts:
        # No calendar activities, clean start!
        return now, now + timedelta(minutes=duration_mins)

    parsed_events = []
    for event in conflicts:
        st_raw = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
        et_raw = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
        if st_raw and et_raw:
            try:
                # Remove timezone offsets to perform clean naive datetime comparisons
                st = dparser.parse(st_raw).replace(tzinfo=None)
                et = dparser.parse(et_raw).replace(tzinfo=None)
                parsed_events.append((st, et))
            except Exception as parse_err:
                print(f"⚠️ Failed to parse calendar conflict event times: {parse_err}")
                
    parsed_events.sort(key=lambda x: x[0])
    
    # Check if there is space immediately right now
    current_candidate = now
    for st, et in parsed_events:
        # Check if gap between current candidate and next meeting start is large enough
        if (st - current_candidate).total_seconds() >= (duration_mins * 60):
            return current_candidate, current_candidate + timedelta(minutes=duration_mins)
        else:
            # Advance the candidate cursor past the end of the current meeting block
            if et > current_candidate:
                current_candidate = et
                
    # If no gaps are found between existing meetings, schedule right after the final conflict block ends
    return current_candidate, current_candidate + timedelta(minutes=duration_mins)

# --- PROACTIVE AUTOMATIC DISTRACTION WARNING ENGINE ---
def check_and_send_mismatch_warning(active_window: str, recipient_id: str):
    """
    Compares the currently active window title against active calendar schedules.
    Dispatches automated Messenger warning cards on off-task distraction shifts.
    """
    global LAST_WARN_TIME, LAST_WARNED_APP
    now = datetime.now()
    
    clean_app = get_clean_app_name(active_window)
    
    # Fetch current scheduled calendar task
    current_calendar_intent = "None (Free Time)"
    try:
        service = get_calendar_service()
        now_utc = datetime.utcnow()
        events_result = service.events().list(
            calendarId='primary', timeMin=now_utc.isoformat() + 'Z',
            timeMax=(now_utc + timedelta(minutes=1)).isoformat() + 'Z',
            maxResults=1, singleEvents=True
        ).execute()
        events = events_result.get('items', [])
        if events and events[0]['start'].get('dateTime'):
            current_calendar_intent = events[0].get('summary', 'Unknown Task')
    except Exception as e:
        print(f"Warning check failed to inspect Google Calendar: {e}")
        return

    # Do not trigger distraction warnings if no current tasks are booked
    if not current_calendar_intent or "none" in current_calendar_intent.lower() or current_calendar_intent == "None (Free Time)":
        return

    # Check productivity matching criteria
    ai_verdict = compute_productivity_verdict(active_window, current_calendar_intent)
    
    is_mismatch = "🔴" in ai_verdict or "mismatch" in ai_verdict.lower() or "distraction" in ai_verdict.lower()

    if is_mismatch:
        time_since_last = (now - LAST_WARN_TIME).total_seconds()
        
        # Throttled warnings: Trigger immediately on window transition OR after 10 mins (600s) on same app
        if clean_app != LAST_WARNED_APP or time_since_last >= 600:
            LAST_WARN_TIME = now
            LAST_WARNED_APP = clean_app
            
            warning_message = (
                f"🚨 *NEXUS PROACTIVE PRODUCTIVITY WARNING!*\n\n"
                f"You have drifted away from your scheduled calendar activity!\n\n"
                f"▪️ *Active Window:* {active_window}\n"
                f"▪️ *Scheduled Goal:* {current_calendar_intent}\n\n"
                f"💡 *AI Focus Verdict:* {ai_verdict}\n\n"
                f"👉 Please close the distraction, refocus, and proceed with your task!"
            )
            send_messenger_text(recipient_id, warning_message)
            print(f"🚨 WARNING DISPATCHED: Distraction detected -> User was playing/browsing on '{clean_app}' during '{current_calendar_intent}'")

# --- CORE INTEGRATED WEBHOOK ENDPOINT ---
@app.route('/webhook', methods=['GET'])
def verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Verification token mismatch", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    global PENDING_SUGGESTION, OVERRIDE_CACHE, CURRENT_WINDOW_TITLE, CURRENT_WINDOW_START_TIME, TOTAL_TIME_ACCUMULATOR, LAST_TICK_TIME, PROCESSED_MID_CACHE
    data = request.get_json()
    
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):
                sender_id = messaging_event["sender"]["id"]
                
                # Deduplication Protection Block against Facebook Retry attempts
                mid = messaging_event.get("message", {}).get("mid") or messaging_event.get("postback", {}).get("mid")
                if mid:
                    now_ts = time.time()
                    if mid in PROCESSED_MID_CACHE:
                        print(f"⚠️ Dropped duplicate Facebook event request retry: {mid}")
                        return "EVENT_RECEIVED", 200
                    PROCESSED_MID_CACHE[mid] = now_ts
                    
                    # Clean up old historical items inside cache tracking dictionary (older than 5 mins)
                    PROCESSED_MID_CACHE = {k: v for k, v in PROCESSED_MID_CACHE.items() if now_ts - v < 300}

                # --- PROCESS INCOMING RAW MESSAGES ---
                if "message" in messaging_event:
                    user_text = messaging_event["message"].get("text", "").strip()
                    if not user_text:
                        return "EVENT_RECEIVED", 200
                    
                    # 📊 LIVE RUNTIME CONFIGURATION STATUS COMMAND WITH AI VERDICT RESTORED
                    if user_text.lower() == "status":
                        now = datetime.now()
                        with state_lock:
                            current_title = CURRENT_WINDOW_TITLE
                            start_time = CURRENT_WINDOW_START_TIME
                            clean_app_key = get_clean_app_name(current_title)
                        
                        # Format session-specific runtime duration strings
                        current_str = f"{int((now - start_time).total_seconds() // 60)}m {int((now - start_time).total_seconds() % 60)}s" if start_time else "0m 0s"
                        
                        # Generate cumulative file-based active timeline statistics strictly for TODAY
                        _, today_app_durations = generate_daily_summary_from_log()
                        total_accumulated_seconds = today_app_durations.get(clean_app_key, 0)
                        total_str = f"{int(total_accumulated_seconds // 60)}m {int(total_accumulated_seconds % 60)}s"
                        
                        # Grand Total Active Time
                        grand_total_seconds = sum(today_app_durations.values())
                        grand_total_str = f"{int(grand_total_seconds // 60)}m {int(grand_total_seconds % 60)}s"
                        
                        current_calendar_intent = "None (Free Time)"
                        try:
                            service = get_calendar_service()
                            now_utc = datetime.utcnow()
                            events_result = service.events().list(
                                calendarId='primary', timeMin=now_utc.isoformat() + 'Z',
                                timeMax=(now_utc + timedelta(minutes=1)).isoformat() + 'Z',
                                maxResults=1, singleEvents=True
                            ).execute()
                            events = events_result.get('items', [])
                            if events and events[0]['start'].get('dateTime'):
                                current_calendar_intent = events[0].get('summary', 'Unknown Task')
                        except Exception as e:
                            print(f"Calendar check failed inside status: {e}")
                            pass

                        # Restored Evaluation Engine Call
                        ai_verdict = compute_productivity_verdict(current_title, current_calendar_intent)

                        # Set appropriate header and alerts based on verdict matching status
                        status_header = "🖥️ *NEXUS LIVE PRODUCTIVITY EVALUATION*"
                        if "🔴" in ai_verdict or "mismatch" in ai_verdict.lower() or "distraction" in ai_verdict.lower():
                            status_header = "🚨 *NEXUS PRODUCTIVITY WARNING - OFF TASK!*"

                        status_message = (
                            f"{status_header}\n\n"
                            f"▪️ *Active Window:* {current_title}\n"
                            f"▪️ *Current Session Time:* {current_str}\n"
                            f"▪️ *Time Spent on this App Today:* {total_str}\n"
                            f"▪️ *Grand Total Active Time Today:* {grand_total_str}\n"
                            f"▪️ *Scheduled Task:* {current_calendar_intent}\n\n"
                            f"💡 *AI Verdict:* {ai_verdict}"
                        )
                        send_messenger_text(sender_id, status_message)
                        return "EVENT_RECEIVED", 200

                    # 📈 DAILY SUMMARY COMMAND
                    elif user_text.lower() == "summary":
                        send_messenger_text(sender_id, "🔍 Analyzing system files to parse chronological app activity and consult AI behavior trends...")
                        summary_text, app_durations = generate_daily_summary_from_log()
                        
                        # Fetch the AI Verdict based on computed app times
                        ai_summary_verdict = compute_summary_verdict(summary_text)
                        
                        report_message = (
                            f"📊 *NEXUS DAILY LOG SUMMARY*\n"
                            f"Source File: `daily_activity_log.txt`\n\n"
                            f"{summary_text}\n\n"
                            f"🧠 *AI Focus Verdict:*\n{ai_summary_verdict}"
                        )
                        send_messenger_text(sender_id, report_message)
                        return "EVENT_RECEIVED", 200

                    # 🔮 HABIT-BASED SCHEDULE SUGGESTION COMMAND
                    elif user_text.lower() == "suggest schedule":
                        send_messenger_text(sender_id, "🔮 Analyzing daily trends and active calendar logs to locate an open schedule block...")
                        suggested_task, duration_mins = get_suggested_schedule_from_log()
                        
                        # Calculate first chronological block free of conflict overlap
                        free_start, free_end = find_next_free_slot(duration_mins)
                        
                        with state_lock:
                            PENDING_SUGGESTION = {
                                "summary": suggested_task,
                                "duration": duration_mins,
                                "status": "PENDING",
                                "target_day": "today",
                                "start_time": free_start.strftime("%Y-%m-%dT%H:%M:%S"),
                                "end_time": free_end.strftime("%Y-%m-%dT%H:%M:%S")
                            }
                        
                        # Inform user exactly what safe timezone slot has been selected 
                        start_time_clock = free_start.strftime("%I:%M %p")
                        end_time_clock = free_end.strftime("%I:%M %p")
                        
                        slot_details = (
                            f"📅 *NEXUS SCHEDULE ENGINE*\n"
                            f"I found a guaranteed open slot today!\n\n"
                            f"▪️ *Proposed Time:* {start_time_clock} to {end_time_clock} ({duration_mins}m)\n"
                            f"▪️ *Conflict Status:* ✅ No Overlaps Detected"
                        )
                        send_messenger_text(sender_id, slot_details)
                        
                        # Deploy interactive postback confirmation button UI directly inside Messenger
                        send_interactive_suggestion(sender_id, suggested_task, duration_mins, target_day="today")
                        return "EVENT_RECEIVED", 200

                    # --- ACTIVE RESOLUTION PATH CONTEXT STATES ---
                    with state_lock:
                        is_awaiting_edit = PENDING_SUGGESTION.get("status") == "AWAITING_EDIT"
                        override_active = bool(OVERRIDE_CACHE)

                    # ✏️ Path A: Handling manual text label editing adjustments
                    if is_awaiting_edit:
                        with state_lock:
                            PENDING_SUGGESTION["summary"] = user_text
                            PENDING_SUGGESTION["status"] = "PENDING"
                            duration = PENDING_SUGGESTION["duration"]
                            target_day = PENDING_SUGGESTION.get("target_day", "today")
                        send_interactive_suggestion(sender_id, user_text, duration, target_day=target_day)
                        return "EVENT_RECEIVED", 200

                    # ✅ Path B: Confirming calendar insertions when responding YES to conflict options
                    elif override_active and user_text.lower() in ["yes", "override", "y", "modify", "change"]:
                        with state_lock:
                            cached = OVERRIDE_CACHE
                            OVERRIDE_CACHE = {}
                        
                        try:
                            service = get_calendar_service()
                            event_data = {
                                'summary': cached["summary"],
                                'description': cached["description"],
                                'start': {'dateTime': cached["start_time"], 'timeZone': 'Asia/Manila'},
                                'end': {'dateTime': cached["end_time"], 'timeZone': 'Asia/Manila'},
                            }
                            service.events().insert(calendarId='primary', body=event_data).execute()
                            send_messenger_text(sender_id, f"🚀 *SCHEDULE SUCCESSFULLY MODIFIED*\nSuccessfully added \"{cached['summary']}\" despite schedule overlaps!")
                        except Exception as e:
                            send_messenger_text(sender_id, f"❌ Calendar commit failure: {str(e)}")
                        return "EVENT_RECEIVED", 200

                    # 🛑 Path C: Aborting changes completely on conflict warnings
                    elif override_active and user_text.lower() in ["no", "cancel", "n"]:
                        with state_lock:
                            OVERRIDE_CACHE = {}
                        send_messenger_text(sender_id, "🛑 *INPUT CANCELLED*\nExisting timeline details were left undisturbed.")
                        return "EVENT_RECEIVED", 200

                    # 📅 Path D: Triggering full text parsing block based on framework logic
                    elif any(kwd in user_text.lower() for kwd in ["schedule", "add", "calendar", "remind", "set"]):
                        send_messenger_text(sender_id, f"Processing tracking event: '{user_text}'...")
                        
                        parsed = parse_activity_with_nexus_ai(user_text)
                        iso_start = parsed.get('start_time')
                        iso_end = parsed.get('end_time')
                        
                        # Verify system integrity against overlap entries (Appending Manila standard offset context)
                        conflicts = check_calendar_overlap(iso_start + "+08:00", iso_end + "+08:00")
                        
                        if conflicts:
                            conflict_names = ", ".join([c.get('summary', 'Booked Task') for c in conflicts])
                            with state_lock:
                                OVERRIDE_CACHE = {
                                    "summary": parsed.get('summary'),
                                    "description": parsed.get('description'),
                                    "start_time": iso_start,
                                    "end_time": iso_end
                                }
                            
                            warning_text = (
                                f"⚠️ *CALENDAR OVERLAP DETECTED*\n\n"
                                f"You want to log: \"{parsed.get('summary')}\"\n"
                                f"But it overlaps with existing plans: *[{conflict_names}]*\n\n"
                                f"Do you want to change/modify your schedule? Reply *YES* to force change, or *NO* to cancel your input."
                            )
                            send_messenger_text(sender_id, warning_text)
                        else:
                            try:
                                service = get_calendar_service()
                                event = {
                                    'summary': parsed.get('summary', 'Tracked Habit'),
                                    'description': parsed.get('description', 'Logged via Nexus Agent'),
                                    'start': {'dateTime': iso_start, 'timeZone': 'Asia/Manila'},
                                    'end': {'dateTime': iso_end, 'timeZone': 'Asia/Manila'},
                                }
                                service.events().insert(calendarId='primary', body=event).execute()
                                
                                start_clock = iso_start.split('T')[1][:5]
                                end_clock = iso_end.split('T')[1][:5]
                                send_messenger_text(sender_id, f"✅ *SUCCESSFULLY ADDED*\nLogged '{parsed.get('summary')}' into your calendar configuration from {start_clock} to {end_clock}!")
                            except Exception as cal_err:
                                send_messenger_text(sender_id, f"❌ Calendar Error: {str(cal_err)}")
                        return "EVENT_RECEIVED", 200

                # --- HANDLING INTERACTIVE BUTTON POSTBACK ACTIONS ---
                elif "postback" in messaging_event:
                    payload = messaging_event["postback"]["payload"]
                    
                    with state_lock:
                        if payload == "CONFIRM_CALENDAR" and PENDING_SUGGESTION:
                            target_day = PENDING_SUGGESTION.get("target_day", "today")
                            summary = PENDING_SUGGESTION["summary"]
                            duration = PENDING_SUGGESTION["duration"]
                            pending_start = PENDING_SUGGESTION.get("start_time")
                            pending_end = PENDING_SUGGESTION.get("end_time")
                            PENDING_SUGGESTION = {}
                        else:
                            summary = None

                    if summary:
                        try:
                            service = get_calendar_service()
                            
                            # Standardize dynamic suggested event ranges securely
                            if pending_start and pending_end:
                                start_dt = dparser.parse(pending_start)
                                end_dt = dparser.parse(pending_end)
                            else:
                                if target_day == "tomorrow":
                                    tomorrow_date = date.today() + timedelta(days=1)
                                    start_dt = datetime.combine(tomorrow_date, dtime(9, 0, 0))
                                    end_dt = start_dt + timedelta(minutes=duration)
                                else:
                                    end_dt = datetime.now()
                                    start_dt = end_dt - timedelta(minutes=duration)
                                
                            event = {
                                'summary': summary,
                                'description': "Logged via Nexus Context Engine verification.",
                                'start': {'dateTime': start_dt.strftime("%Y-%m-%dT%H:%M:%S"), 'timeZone': 'Asia/Manila'},
                                'end': {'dateTime': end_dt.strftime("%Y-%m-%dT%H:%M:%S"), 'timeZone': 'Asia/Manila'},
                            }
                            service.events().insert(calendarId='primary', body=event).execute()
                            send_messenger_text(sender_id, f"✅ *SUCCESSFULLY ADDED*\nYour event \"{summary}\" has been recorded onto your calendar timeline!")
                            if os.path.exists(LOG_FILE + ".tmp"):
                                os.remove(LOG_FILE + ".tmp")
                        except Exception as e:
                            send_messenger_text(sender_id, f"❌ Postback insertion failed: {e}")
                    return "EVENT_RECEIVED", 200

    return "EVENT_RECEIVED", 200

# --- TRACKING ENGINE WORKING DAEMON LOOP ---
def run_agent_suggestion_pipeline():
    global CURRENT_WINDOW_TITLE, CURRENT_WINDOW_START_TIME, LAST_TICK_TIME
    
    import time
    try:
        import pygetwindow as gw
    except ImportError:
        gw = None

    print("🤖 NEXUS ENGINE: Active Background Window Tracker Daemon Started Successfully.")
    
    if CURRENT_WINDOW_START_TIME is None:
        CURRENT_WINDOW_START_TIME = datetime.now()
    if LAST_TICK_TIME is None:
        LAST_TICK_TIME = datetime.now()

    last_log_time = datetime.now()

    while True:
        try:
            active_title = "Desktop / Idle"
            if gw:
                try:
                    win = gw.getActiveWindow()
                    if win and win.title:
                        active_title = str(win.title).encode('ascii', 'ignore').decode('ascii').strip()
                except Exception:
                    pass
            
            if not active_title:
                active_title = "Unknown Application"

            now = datetime.now()
            window_changed = False
            
            with state_lock:
                if active_title != CURRENT_WINDOW_TITLE:
                    print(f"🔄 Window Focus Shifted: From '{CURRENT_WINDOW_TITLE}' To '{active_title}'")
                    CURRENT_WINDOW_TITLE = active_title
                    CURRENT_WINDOW_START_TIME = now
                    window_changed = True
                
                clean_app_key = get_clean_app_name(active_title)
                TOTAL_TIME_ACCUMULATOR[clean_app_key] = TOTAL_TIME_ACCUMULATOR.get(clean_app_key, 0) + (now - LAST_TICK_TIME).total_seconds()
                LAST_TICK_TIME = now

            # Immediate distraction assessment on focus transition
            if window_changed:
                threading.Thread(
                    target=check_and_send_mismatch_warning, 
                    args=(active_title, FB_RECIPIENT_USER_ID), 
                    daemon=True
                ).start()

            if (now - last_log_time).total_seconds() >= 60:
                try:
                    # Unified logging to write strictly to LOG_FILE in the subdirectory
                    log_entry = f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Active Window: {CURRENT_WINDOW_TITLE}\n"
                    
                    # Ensure path exists
                    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(log_entry)
                    
                    print(f"📝 File Pipeline Sync: Logged current state to {LOG_FILE}")
                    last_log_time = now

                    # Periodic reminder warning check on active tab
                    threading.Thread(
                        target=check_and_send_mismatch_warning, 
                        args=(CURRENT_WINDOW_TITLE, FB_RECIPIENT_USER_ID), 
                        daemon=True
                    ).start()
                except Exception as file_err:
                    print(f"⚠️ Failed writing to log file: {file_err}")

        except Exception as pipeline_fault:
            print(f"🔴 Internal Nexus Tracker Exception caught: {pipeline_fault}")
            pass
        
        time.sleep(1)

if __name__ == '__main__':
    tracker = threading.Thread(target=run_agent_suggestion_pipeline, daemon=True)
    tracker.start()
    
    app.run(port=5000, debug=False, use_reloader=False)