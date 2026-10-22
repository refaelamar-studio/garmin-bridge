#!/usr/bin/env python3
import json
import logging
import os
import sys
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error

# Add local garmin directory and site-packages to sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_PACKAGES = os.path.join(CURRENT_DIR, "site-packages")
if SITE_PACKAGES not in sys.path:
    sys.path.insert(0, SITE_PACKAGES)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("garmin_service")

# Thread-safe in-memory storage for pending MFA sessions (expires after 10 minutes)
# Format: session_id -> {"garmin": GarminInstance, "created_at": timestamp, "email": email}
mfa_lock = threading.Lock()
mfa_sessions = {}

def clean_expired_mfa_sessions():
    now = time.time()
    with mfa_lock:
        expired = [sid for sid, data in mfa_sessions.items() if now - data.get("created_at", 0) > 600]
        for sid in expired:
            mfa_sessions.pop(sid, None)

def parse_tokens_str(tokens_input):
    if not tokens_input:
        return ""
    if isinstance(tokens_input, (dict, list)):
        return json.dumps(tokens_input)
    val = str(tokens_input).strip()
    return val

def format_pace(speed_mps):
    if not speed_mps or speed_mps <= 0:
        return ""
    seconds_per_km = 1000.0 / float(speed_mps)
    minutes = int(seconds_per_km // 60)
    seconds = int(round(seconds_per_km % 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"

def format_duration(duration_sec):
    if not duration_sec or duration_sec <= 0:
        return "00:00"
    total_sec = int(round(duration_sec))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

def fetch_public_garmin_activity(activity_id):
    """Fallback fetch for public Garmin activities without user credentials."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "NK": "NT",
        "origin": "https://connect.garmin.com",
        "referer": f"https://connect.garmin.com/modern/activity/{activity_id}",
        "Accept": "application/json, text/plain, */*"
    }
    url = f"https://connect.garmin.com/modern/proxy/activity-service/activity/{activity_id}"
    try:
        if HAS_CFFI:
            r = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=6)
            if r.status_code == 200:
                return r.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"Public activity fetch failed for {activity_id}: {e}")
    return None

def fetch_public_garmin_splits(activity_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "NK": "NT",
        "origin": "https://connect.garmin.com",
        "referer": f"https://connect.garmin.com/modern/activity/{activity_id}",
        "Accept": "application/json, text/plain, */*"
    }
    url = f"https://connect.garmin.com/modern/proxy/activity-service/activity/{activity_id}/splits"
    try:
        if HAS_CFFI:
            r = cffi_requests.get(url, headers=headers, impersonate="chrome120", timeout=5)
            if r.status_code == 200:
                return r.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
    except Exception:
        pass
    return None

class GarminHTTPHandler(BaseHTTPRequestHandler):
    # Disable default connection logging noise
    def log_message(self, format, *args):
        # Only log non-200 or interesting messages
        if len(args) > 1 and str(args[1]) not in ("200", "204"):
            logger.info(f"{self.address_string()} - {format % args}")

    def _send_json(self, status_code, data):
        try:
            response_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_bytes)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, IOError) as e:
            logger.debug(f"Client disconnected during response send ({status_code}): {e}")
        except Exception as e:
            logger.error(f"Unexpected error in _send_json: {e}")

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, IOError):
            pass

    def do_GET(self):
        if self.path == "/health":
            with mfa_lock:
                mfa_count = len(mfa_sessions)
            self._send_json(200, {"status": "ok", "service": "garmin-bridge", "mfa_pending_count": mfa_count})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        clean_expired_mfa_sessions()
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON body: {str(e)}"})
            return

        try:
            if self.path == "/login":
                self.handle_login(body)
            elif self.path == "/resume_mfa":
                self.handle_resume_mfa(body)
            elif self.path == "/activities":
                self.handle_activities(body)
            elif self.path == "/activity_details":
                self.handle_activity_details(body)
            elif self.path == "/refresh":
                self.handle_refresh(body)
            else:
                self._send_json(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError):
            logger.debug(f"Client disconnected during handler execution for {self.path}")
        except Exception as e:
            logger.error(f"Unhandled exception in handler for {self.path}: {e}", exc_info=True)
            self._send_json(500, {"error": f"שגיאה פנימית בשרת: {str(e)}"})

    def handle_login(self, body):
        email = body.get("email", "").strip()
        password = body.get("password", "").strip()
        tokens = body.get("tokens", "")

        garmin = Garmin(email=email or None, password=password or None, return_on_mfa=True)

        # 1. Try tokenstore login if tokens exist
        if tokens:
            try:
                tokens_str = parse_tokens_str(tokens)
                garmin.client.loads(tokens_str)
                with garmin.client._token_lock:
                    if garmin.client.di_refresh_token and garmin.client._token_expires_soon():
                        garmin.client._refresh_session()
                profile_name = garmin.display_name or garmin.full_name or "Garmin User"
                dumped_tokens = garmin.client.dumps()
                self._send_json(200, {
                    "status": "SUCCESS",
                    "tokens": dumped_tokens,
                    "profile": {
                        "displayName": profile_name,
                        "fullName": garmin.full_name or profile_name,
                    }
                })
                return
            except Exception as e:
                logger.warning(f"Token restoration failed: {e}")
                if not email or not password:
                    self._send_json(400, {
                        "error": "טוקן ה-Garmin שהוזן אינו תקין או פג תוקפו. יש לוודא שהטוקן מכיל את המפתחות di_token ו-di_refresh_token.",
                        "code": "INVALID_TOKEN"
                    })
                    return

        if not email or not password:
            self._send_json(400, {"error": "נא להזין כתובת אימייל וסיסמה של Garmin, או להזין טוקן התחברות ישיר."})
            return

        # 2. Login with credentials
        try:
            mfa_status, _ = garmin.login()
            is_mfa = (
                (mfa_status and str(mfa_status).lower() == "needs_mfa")
                or getattr(garmin.client, "_mfa_session", None) is not None
                or getattr(garmin.client, "_mfa_pending", False)
            )
            if is_mfa:
                session_id = str(uuid.uuid4())
                with mfa_lock:
                    mfa_sessions[session_id] = {
                        "garmin": garmin,
                        "created_at": time.time(),
                        "email": email,
                    }
                logger.info(f"MFA required for {email}, created session {session_id}")
                self._send_json(200, {
                    "status": "NEEDS_MFA",
                    "sessionId": session_id,
                    "message": "Garmin דורשת אימות דו-שלבי. קוד אימות חד-פעמי (OTP) נשלח לאימייל או לטלפון שלך מ-Garmin. הזן אותו כאן להשלמת החיבור."
                })
                return

            # Clean successful login
            dumped_tokens = garmin.client.dumps()
            profile_name = garmin.display_name or garmin.full_name or email
            logger.info(f"Garmin login succeeded for {email}")
            self._send_json(200, {
                "status": "SUCCESS",
                "tokens": dumped_tokens,
                "profile": {
                    "displayName": profile_name,
                    "fullName": garmin.full_name or profile_name,
                }
            })
        except GarminConnectAuthenticationError as e:
            logger.error(f"Garmin auth error for {email}: {e}")
            self._send_json(401, {"error": "כתובת האימייל או הסיסמה של Garmin שגויים."})
        except GarminConnectTooManyRequestsError as e:
            logger.error(f"Garmin rate limit for {email}: {e}")
            self._send_json(429, {
                "error": "שרתי Garmin חסמו זמנית ניסיונות התחברות מרובים (Rate Limit 429). ניתן להתחבר ישירות בעזרת טוקן התחברות (Direct Token) או להמתין כ-15 דקות.",
                "code": "RATE_LIMITED"
            })
        except Exception as e:
            logger.error(f"Garmin login unexpected error: {e}")
            self._send_json(500, {
                "error": f"שגיאה בהתחברות ל-Garmin: {str(e)}",
                "code": "SERVER_ERROR"
            })

    def handle_resume_mfa(self, body):
        session_id = body.get("sessionId", "").strip()
        mfa_code = body.get("mfaCode", "").strip()

        if not session_id or not mfa_code:
            self._send_json(400, {"error": "sessionId and mfaCode are required"})
            return

        with mfa_lock:
            session_data = mfa_sessions.get(session_id)
            if session_data:
                mfa_sessions.pop(session_id, None)

        if not session_data:
            self._send_json(400, {"error": "פג תוקף סשן האימות (10 דקות). אנא נסה להתחבר שוב."})
            return

        garmin = session_data["garmin"]
        try:
            garmin.client.resume_login(None, mfa_code)
            dumped_tokens = garmin.client.dumps()
            profile_name = garmin.display_name or garmin.full_name or session_data.get("email", "")
            logger.info(f"MFA successfully completed for session {session_id}")
            self._send_json(200, {
                "status": "SUCCESS",
                "tokens": dumped_tokens,
                "profile": {
                    "displayName": profile_name,
                    "fullName": garmin.full_name or profile_name,
                }
            })
        except GarminConnectAuthenticationError as e:
            logger.error(f"Garmin MFA verification error: {e}")
            self._send_json(401, {"error": "קוד האימות שהוזן שגוי או פג תוקפו."})
        except Exception as e:
            logger.error(f"Garmin MFA error: {e}")
            self._send_json(500, {"error": f"שגיאה באימות קוד ה-MFA: {str(e)}"})

    def handle_activities(self, body):
        tokens = body.get("tokens", "")
        start_date = body.get("startDate", "")
        end_date = body.get("endDate", "")
        activity_type = body.get("activityType", None)

        if not tokens:
            self._send_json(400, {"error": "tokens are required"})
            return

        tokens_str = parse_tokens_str(tokens)

        if not start_date:
            now = time.strftime("%Y-%m-%d")
            start_date = now

        garmin = Garmin()
        try:
            garmin.client.loads(tokens_str)
            with garmin.client._token_lock:
                if garmin.client.di_refresh_token and garmin.client._token_expires_soon():
                    garmin.client._refresh_session()

            # Fetch activities (fast single-call fetch of the latest 40 activities)
            raw_activities = []
            try:
                raw_activities = garmin.get_activities(0, limit=40) or []
            except Exception as e:
                logger.warning(f"garmin.get_activities(0, 40) failed ({e}), falling back to date query...")
                try:
                    raw_activities = garmin.get_activities_by_date(
                        startdate=start_date,
                        enddate=end_date or None,
                        activitytype=activity_type,
                        sortorder="desc"
                    ) or []
                except Exception as date_err:
                    logger.warning(f"get_activities_by_date also failed: {date_err}")
                    raw_activities = []

            activities = []
            for act in (raw_activities or []):
                act_id = str(act.get("activityId", ""))
                distance_m = float(act.get("distance", 0.0) or 0.0)
                duration_s = float(act.get("duration", 0.0) or 0.0)
                avg_speed = float(act.get("averageSpeed", 0.0) or 0.0)
                start_local = act.get("startTimeLocal", "")
                date_str = start_local[:10] if start_local else ""
                act_type_info = act.get("activityType", {}) or {}
                type_key = act_type_info.get("typeKey", "running")

                activities.append({
                    "id": act_id,
                    "activityId": act_id,
                    "activityName": act.get("activityName", "אימון"),
                    "activityType": type_key,
                    "date": date_str,
                    "startTime": start_local,
                    "distanceKm": round(distance_m / 1000.0, 2),
                    "durationSeconds": int(round(duration_s)),
                    "durationFormatted": format_duration(duration_s),
                    "avgPace": format_pace(avg_speed),
                    "maxPace": format_pace(float(act.get("maxSpeed", 0.0) or 0.0)),
                    "avgHeartRate": act.get("averageHR"),
                    "maxHeartRate": act.get("maxHR"),
                    "calories": act.get("calories"),
                    "elevationGain": act.get("elevationGain"),
                    "elevationLoss": act.get("elevationLoss"),
                    "avgCadence": act.get("averageRunningCadenceInStepsPerMinute") or act.get("averageCadence"),
                    "aerobicTrainingEffect": act.get("aerobicTrainingEffect"),
                    "anaerobicTrainingEffect": act.get("anaerobicTrainingEffect"),
                    "activityUrl": f"https://connect.garmin.com/modern/activity/{act_id}",
                })

            dumped_tokens = garmin.client.dumps()
            self._send_json(200, {
                "status": "SUCCESS",
                "count": len(activities),
                "activities": activities,
                "tokens": dumped_tokens,
            })
        except GarminConnectAuthenticationError:
            self._send_json(401, {"error": "חיבור ה-Garmin פג תוקף, יש להתחבר מחדש.", "code": "AUTH_EXPIRED"})
        except Exception as e:
            logger.warning(f"Error fetching Garmin activities: {e}")
            self._send_json(500, {"error": f"שגיאה בשליפת אימונים מ-Garmin: {str(e)}"})

    def handle_activity_details(self, body):
        tokens = body.get("tokens", "")
        activity_id = str(body.get("activityId", "")).strip()

        if not activity_id:
            self._send_json(400, {"error": "activityId is required"})
            return

        tokens_str = parse_tokens_str(tokens)
        garmin = Garmin(retry_attempts=0)
        raw_act = None
        splits_data = None
        raw_zones = None
        raw_weather = None
        dumped_tokens = tokens_str

        # 1. Try authenticated Garmin client if tokens are available
        if tokens_str:
            try:
                garmin.client.loads(tokens_str)
                with garmin.client._token_lock:
                    if garmin.client.di_refresh_token and garmin.client._token_expires_soon():
                        garmin.client._refresh_session()

                raw_act = garmin.get_activity(activity_id) or {}
                dumped_tokens = garmin.client.dumps()

                try:
                    splits_data = garmin.get_activity_splits(activity_id) or {}
                except Exception:
                    pass

                try:
                    raw_zones = garmin.get_activity_hr_in_timezones(activity_id) or []
                except Exception:
                    pass

                try:
                    raw_weather = garmin.get_activity_weather(activity_id) or {}
                except Exception:
                    pass

            except GarminConnectAuthenticationError:
                logger.info(f"Garmin token expired for activity {activity_id}, trying public fallback...")
            except Exception as e:
                logger.warning(f"Authenticated activity fetch failed: {e}")

        # 2. Public fallback if authenticated fetch didn't return activity data
        if not raw_act:
            logger.info(f"Attempting public activity fetch for activity {activity_id}...")
            raw_act = fetch_public_garmin_activity(activity_id)
            if raw_act:
                splits_data = fetch_public_garmin_splits(activity_id)

        if not raw_act:
            self._send_json(404, {
                "error": "לא ניתן היה לשלוף את פרטי הפעילות מ-Garmin. יש לוודא שהאימון קיים ונגיש בחשבון Garmin.",
                "code": "ACTIVITY_NOT_FOUND"
            })
            return

        summary = raw_act.get("summaryDTO", {}) or {}
        distance_m = float(summary.get("distance", 0.0) or 0.0)
        duration_s = float(summary.get("duration", 0.0) or 0.0)
        moving_duration_s = float(summary.get("movingDuration", duration_s) or duration_s)
        elapsed_duration_s = float(summary.get("elapsedDuration", duration_s) or duration_s)
        avg_speed = float(summary.get("averageSpeed", 0.0) or 0.0)
        max_speed = float(summary.get("maxSpeed", 0.0) or 0.0)

        # Laps / Splits
        laps = []
        if splits_data:
            lap_list = splits_data.get("lapDTOs") or splits_data.get("splitSummaries") or []
            for idx, lap in enumerate(lap_list):
                l_dist = float(lap.get("distance", 0.0) or 0.0)
                l_dur = float(lap.get("duration", 0.0) or 0.0)
                l_speed = float(lap.get("averageSpeed", 0.0) or 0.0)
                laps.append({
                    "lapIndex": idx + 1,
                    "distanceKm": round(l_dist / 1000.0, 2),
                    "durationFormatted": format_duration(l_dur),
                    "avgPace": format_pace(l_speed),
                    "avgHeartRate": lap.get("averageHR"),
                    "maxHeartRate": lap.get("maxHR"),
                    "elevationGain": lap.get("elevationGain"),
                    "elevationLoss": lap.get("elevationLoss"),
                    "calories": lap.get("calories"),
                })

        # Heart Rate Zones
        hr_zones = []
        if raw_zones:
            zone_names = {
                1: "חימום / התאוששות (Zone 1)",
                2: "אירובי קל (Zone 2)",
                3: "אירובי (Zone 3)",
                4: "סף אנאירובי (Zone 4)",
                5: "מקסימום (Zone 5)"
            }
            total_zone_secs = sum(float(z.get("secsInZone", 0.0) or 0.0) for z in raw_zones) or 1.0
            for z in raw_zones:
                z_num = int(z.get("zoneNumber", 0) or 0)
                if z_num <= 0:
                    continue
                z_secs = float(z.get("secsInZone", 0.0) or 0.0)
                pct = round((z_secs / total_zone_secs) * 100, 1)
                hr_zones.append({
                    "zoneNumber": z_num,
                    "zoneName": zone_names.get(z_num, f"Zone {z_num}"),
                    "secsInZone": int(round(z_secs)),
                    "percentInZone": pct,
                    "minHr": z.get("zoneLowBoundary"),
                    "maxHr": z.get("zoneHighBoundary") or None,
                })

        # Weather
        weather_info = None
        if raw_weather:
            weather_info = {
                "temp": raw_weather.get("temp"),
                "feelsLike": raw_weather.get("apparentTemp"),
                "humidity": raw_weather.get("relativeHumidity"),
                "condition": raw_weather.get("weatherTypeDTO", {}).get("desc", ""),
                "windSpeedKmh": raw_weather.get("windSpeed"),
            }

        detail = {
            "id": activity_id,
            "activityName": raw_act.get("activityName", "אימון Garmin"),
            "date": (raw_act.get("startTimeLocal") or "")[:10],
            "startTime": raw_act.get("startTimeLocal", ""),
            "distanceKm": round(distance_m / 1000.0, 2),
            "durationFormatted": format_duration(duration_s),
            "movingDurationFormatted": format_duration(moving_duration_s),
            "elapsedDurationFormatted": format_duration(elapsed_duration_s),
            "avgPace": format_pace(avg_speed),
            "maxPace": format_pace(max_speed),
            "avgSpeedKmh": round(avg_speed * 3.6, 2) if avg_speed else None,
            "maxSpeedKmh": round(max_speed * 3.6, 2) if max_speed else None,
            "avgHeartRate": summary.get("averageHR"),
            "maxHeartRate": summary.get("maxHR"),
            "elevationGain": summary.get("elevationGain"),
            "elevationLoss": summary.get("elevationLoss"),
            "minElevation": summary.get("minElevation"),
            "maxElevation": summary.get("maxElevation"),
            "calories": summary.get("calories"),
            "avgCadence": summary.get("averageRunningCadenceInStepsPerMinute"),
            "maxCadence": summary.get("maxRunningCadenceInStepsPerMinute"),
            "avgStrideLengthMeters": round(float(summary.get("strideLength", 0) or 0) / 100.0, 2) if summary.get("strideLength") else None,
            "aerobicTrainingEffect": summary.get("aerobicTrainingEffect"),
            "anaerobicTrainingEffect": summary.get("anaerobicTrainingEffect"),
            "trainingEffectMessage": summary.get("trainingEffectLabel", ""),
            "vo2MaxValue": summary.get("vo2MaxValue"),
            "avgPower": summary.get("avgPower"),
            "maxPower": summary.get("maxPower"),
            "groundContactTimeMs": summary.get("avgGroundContactTime"),
            "verticalOscillationCm": summary.get("avgVerticalOscillation"),
            "weather": weather_info,
            "laps": laps,
            "hrZones": hr_zones,
            "garminUrl": f"https://connect.garmin.com/modern/activity/{activity_id}",
        }

        self._send_json(200, {
            "status": "SUCCESS",
            "activity": detail,
            "tokens": dumped_tokens,
        })

    def handle_refresh(self, body):
        tokens = body.get("tokens", "")
        if not tokens:
            self._send_json(400, {"error": "tokens are required"})
            return
        tokens_str = parse_tokens_str(tokens)
        garmin = Garmin()
        try:
            garmin.client.loads(tokens_str)
            with garmin.client._token_lock:
                garmin.client._refresh_session()
            self._send_json(200, {
                "status": "SUCCESS",
                "tokens": garmin.client.dumps()
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

def run_server(port=5055):
    env_port = os.environ.get("PORT")
    final_port = int(env_port) if env_port else int(port)
    server_address = ("0.0.0.0", final_port)
    httpd = ThreadingHTTPServer(server_address, GarminHTTPHandler)
    httpd.daemon_threads = True
    logger.info(f"Garmin Python Bridge Service running on http://0.0.0.0:{final_port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Garmin service...")
        httpd.server_close()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5055))
    run_server(port)
