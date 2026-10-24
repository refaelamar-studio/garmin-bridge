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

def build_garmin_running_workout(workout_name, description="", distance_km=0, duration_minutes=0, steps=None):
    sport_type = {
        "sportTypeId": 1,
        "sportTypeKey": "running",
        "displayOrder": 1
    }
    
    workout_steps = []
    
    # If custom steps provided
    if steps and isinstance(steps, list) and len(steps) > 0:
        for idx, step in enumerate(steps, start=1):
            stype = str(step.get("type", "interval")).lower()
            if stype == "warmup":
                step_type_id, step_key = 1, "warmup"
            elif stype == "cooldown":
                step_type_id, step_key = 2, "cooldown"
            elif stype == "recovery":
                step_type_id, step_key = 4, "recovery"
            elif stype == "rest":
                step_type_id, step_key = 5, "rest"
            else:
                step_type_id, step_key = 3, "interval"
                
            dist_m = float(step.get("distanceMeters", 0) or 0)
            dur_s = float(step.get("durationSeconds", 0) or 0)
            
            if dist_m > 0:
                end_cond = {
                    "conditionTypeId": 3,
                    "conditionTypeKey": "distance",
                    "displayOrder": 3,
                    "displayable": True
                }
                end_val = dist_m
            elif dur_s > 0:
                end_cond = {
                    "conditionTypeId": 2,
                    "conditionTypeKey": "time",
                    "displayOrder": 2,
                    "displayable": True
                }
                end_val = dur_s
            else:
                end_cond = {
                    "conditionTypeId": 1,
                    "conditionTypeKey": "lap.button",
                    "displayOrder": 1,
                    "displayable": True
                }
                end_val = None
                
            step_dict = {
                "type": "ExecutableStepDTO",
                "stepOrder": idx,
                "stepType": {
                    "stepTypeId": step_type_id,
                    "stepTypeKey": step_key,
                    "displayOrder": step_type_id
                },
                "endCondition": end_cond,
                "targetType": {
                    "workoutTargetTypeId": 1,
                    "workoutTargetTypeKey": "no.target",
                    "displayOrder": 1
                }
            }
            if end_val is not None:
                step_dict["endConditionValue"] = end_val
            workout_steps.append(step_dict)
    else:
        # Default 3-step structured workout: Warmup -> Main Run -> Cooldown
        dist_m = float(distance_km or 0) * 1000.0
        dur_s = float(duration_minutes or 0) * 60.0
        
        # Step 1: Warmup (ended by lap button or 5 min)
        workout_steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": 1,
            "stepType": {
                "stepTypeId": 1,
                "stepTypeKey": "warmup",
                "displayOrder": 1
            },
            "endCondition": {
                "conditionTypeId": 1,
                "conditionTypeKey": "lap.button",
                "displayOrder": 1,
                "displayable": True
            },
            "targetType": {
                "workoutTargetTypeId": 1,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1
            }
        })
        
        # Step 2: Main Run (distance in meters, or duration, or lap button)
        if dist_m > 0:
            main_cond = {
                "conditionTypeId": 3,
                "conditionTypeKey": "distance",
                "displayOrder": 3,
                "displayable": True
            }
            main_val = dist_m
        elif dur_s > 0:
            main_cond = {
                "conditionTypeId": 2,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True
            }
            main_val = dur_s
        else:
            main_cond = {
                "conditionTypeId": 1,
                "conditionTypeKey": "lap.button",
                "displayOrder": 1,
                "displayable": True
            }
            main_val = None
            
        main_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": 2,
            "stepType": {
                "stepTypeId": 3,
                "stepTypeKey": "interval",
                "displayOrder": 3
            },
            "endCondition": main_cond,
            "targetType": {
                "workoutTargetTypeId": 1,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1
            }
        }
        if main_val is not None:
            main_step["endConditionValue"] = main_val
        workout_steps.append(main_step)
        
        # Step 3: Cooldown (ended by lap button)
        workout_steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": 3,
            "stepType": {
                "stepTypeId": 2,
                "stepTypeKey": "cooldown",
                "displayOrder": 2
            },
            "endCondition": {
                "conditionTypeId": 1,
                "conditionTypeKey": "lap.button",
                "displayOrder": 1,
                "displayable": True
            },
            "targetType": {
                "workoutTargetTypeId": 1,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1
            }
        })

    estimated_sec = int(float(distance_km) * 360) if distance_km else (int(duration_minutes * 60) if duration_minutes else 1800)
    
    return {
        "workoutName": workout_name or "אימון ריצה",
        "description": description or "",
        "sportType": sport_type,
        "estimatedDurationInSecs": estimated_sec,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": workout_steps
            }
        ]
    }

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
    def log_message(self, format, *args):
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
            elif self.path == "/schedule_workout":
                self.handle_schedule_workout(body)
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
            session_data = mfa_sessions.pop(session_id, None)

        if not session_data:
            self._send_json(400, {"error": "פג תוקפו של סשן האימות הדו-שלבי (עברו יותר מ-10 דקות). אנא נסה להתחבר שוב מחדש."})
            return

        garmin = session_data["garmin"]
        email = session_data.get("email", "")

        try:
            garmin.resume_login(mfa_code)
            dumped_tokens = garmin.client.dumps()
            profile_name = garmin.display_name or garmin.full_name or email
            logger.info(f"Garmin MFA login succeeded for {email}")
            self._send_json(200, {
                "status": "SUCCESS",
                "tokens": dumped_tokens,
                "profile": {
                    "displayName": profile_name,
                    "fullName": garmin.full_name or profile_name,
                }
            })
        except GarminConnectAuthenticationError as e:
            logger.error(f"Garmin MFA auth error for {email}: {e}")
            self._send_json(401, {"error": "קוד האימות הדו-שלבי שהוזן שגוי או שפג תוקפו."})
        except Exception as e:
            logger.error(f"Garmin MFA resume failed: {e}")
            self._send_json(500, {"error": f"שגיאה באימות קוד ה-MFA מול Garmin: {str(e)}"})

    def handle_activities(self, body):
        tokens = body.get("tokens", "")
        if not tokens:
            self._send_json(400, {"error": "tokens are required"})
            return

        tokens_str = parse_tokens_str(tokens)
        garmin = Garmin()
        try:
            garmin.client.loads(tokens_str)
            with garmin.client._token_lock:
                if garmin.client.di_refresh_token and garmin.client._token_expires_soon():
                    garmin.client._refresh_session()
        except Exception as e:
            self._send_json(401, {"error": f"טוקן Garmin אינו תקין: {str(e)}", "code": "INVALID_TOKEN"})
            return

        try:
            raw_activities = garmin.get_activities(0, 50) or []
            start_date = body.get("startDate", "").split("T")[0] if body.get("startDate") else None
            end_date = body.get("endDate", "").split("T")[0] if body.get("endDate") else None

            activities = []
            for act in raw_activities:
                if not act:
                    continue
                start_time_local = act.get("startTimeLocal") or ""
                act_date = start_time_local.split(" ")[0] if start_time_local else ""
                if not act_date and act.get("startTimeGMT"):
                    act_date = act.get("startTimeGMT", "").split("T")[0]

                if start_date and act_date and act_date < start_date:
                    continue
                if end_date and act_date and act_date > end_date:
                    continue

                dist_m = act.get("distance", 0) or 0
                dist_km = round(dist_m / 1000.0, 2)
                duration_s = act.get("duration", 0) or 0
                avg_speed = act.get("averageSpeed", 0) or 0
                max_speed = act.get("maxSpeed", 0) or 0

                activities.append({
                    "activityId": str(act.get("activityId", "")),
                    "activityName": act.get("activityName") or "אימון ריצה",
                    "date": act_date,
                    "startTime": start_time_local or act.get("startTimeGMT") or "",
                    "activityType": act.get("activityType", {}).get("typeKey", "running"),
                    "distanceKm": dist_km,
                    "durationFormatted": format_duration(duration_s),
                    "avgPace": format_pace(avg_speed),
                    "maxPace": format_pace(max_speed),
                    "avgHeartRate": act.get("averageHR"),
                    "maxHeartRate": act.get("maxHR"),
                    "calories": act.get("calories"),
                    "elevationGain": act.get("elevationGain"),
                    "elevationLoss": act.get("elevationLoss"),
                    "avgCadence": act.get("averageRunningCadenceInStepsPerMinute") or act.get("averageCadence"),
                    "aerobicTrainingEffect": act.get("aerobicTrainingEffect"),
                    "anaerobicTrainingEffect": act.get("anaerobicTrainingEffect"),
                    "activityUrl": f"https://connect.garmin.com/modern/activity/{act.get('activityId')}",
                })

            dumped_tokens = garmin.client.dumps()
            self._send_json(200, {
                "status": "SUCCESS",
                "count": len(activities),
                "activities": activities,
                "tokens": dumped_tokens,
            })
        except Exception as e:
            logger.error(f"Failed to fetch activities: {e}", exc_info=True)
            self._send_json(500, {"error": f"שגיאה בשליפת פעילויות מ-Garmin: {str(e)}"})

    def handle_activity_details(self, body):
        activity_id = str(body.get("activityId", "")).strip()
        tokens = body.get("tokens", "")
        if not activity_id:
            self._send_json(400, {"error": "activityId is required"})
            return

        raw_act = None
        splits_data = None
        weather_data = None
        hr_zones_data = None
        garmin = Garmin()
        dumped_tokens = None

        if tokens:
            try:
                tokens_str = parse_tokens_str(tokens)
                garmin.client.loads(tokens_str)
                with garmin.client._token_lock:
                    if garmin.client.di_refresh_token and garmin.client._token_expires_soon():
                        garmin.client._refresh_session()
                raw_act = garmin.get_activity(activity_id)
                dumped_tokens = garmin.client.dumps()
                try:
                    splits_data = garmin.get_activity_splits(activity_id)
                except Exception as se:
                    logger.debug(f"Splits fetch failed: {se}")
                try:
                    weather_data = garmin.get_activity_weather(activity_id)
                except Exception as we:
                    logger.debug(f"Weather fetch failed: {we}")
                try:
                    hr_zones_data = garmin.get_activity_hr_in_timezones(activity_id)
                except Exception as he:
                    logger.debug(f"HR Zones fetch failed: {he}")
            except Exception as e:
                logger.warning(f"Authenticated activity details failed: {e}")

        if not raw_act:
            raw_act = fetch_public_garmin_activity(activity_id)
            if raw_act:
                splits_data = fetch_public_garmin_splits(activity_id)

        if not raw_act:
            self._send_json(404, {
                "error": "לא ניתן היה לשלוף את פרטי הפעילות מ-Garmin. ודא שהאימון קיים ונגיש בחשבון.",
                "code": "ACTIVITY_NOT_FOUND"
            })
            return

        summary = raw_act.get("summaryDTO") or raw_act
        dist_m = float(summary.get("distance", 0) or 0)
        dist_km = round(dist_m / 1000.0, 2)
        duration_s = float(summary.get("duration", 0) or 0)
        moving_duration_s = float(summary.get("movingDuration", 0) or duration_s)
        elapsed_duration_s = float(summary.get("elapsedDuration", 0) or duration_s)
        avg_speed = float(summary.get("averageSpeed", 0) or 0)
        max_speed = float(summary.get("maxSpeed", 0) or 0)

        laps = []
        if splits_data:
            lap_dtos = splits_data.get("lapDTOs") or splits_data.get("splitSummaries") or []
            for idx, lap in enumerate(lap_dtos, start=1):
                l_dist = float(lap.get("distance", 0) or 0)
                l_dur = float(lap.get("duration", 0) or 0)
                l_spd = float(lap.get("averageSpeed", 0) or 0)
                laps.append({
                    "lapIndex": idx,
                    "distanceKm": round(l_dist / 1000.0, 2),
                    "durationFormatted": format_duration(l_dur),
                    "avgPace": format_pace(l_spd),
                    "avgHeartRate": lap.get("averageHR"),
                    "maxHeartRate": lap.get("maxHR"),
                    "elevationGain": lap.get("elevationGain"),
                    "elevationLoss": lap.get("elevationLoss"),
                    "calories": lap.get("calories"),
                })

        hr_zones = []
        if hr_zones_data and isinstance(hr_zones_data, list):
            for z in hr_zones_data:
                hr_zones.append({
                    "zoneNumber": z.get("zoneNumber", 0),
                    "zoneName": z.get("zoneName", ""),
                    "secsInZone": z.get("secsInZone", 0),
                    "percentInZone": round(float(z.get("zonePercentage", 0) or 0), 1),
                    "minHr": z.get("zoneLowBoundary"),
                    "maxHr": z.get("zoneHighBoundary"),
                })

        weather_info = None
        if weather_data and isinstance(weather_data, dict):
            weather_info = {
                "temp": weather_data.get("temperature"),
                "feelsLike": weather_data.get("apparentTemperature"),
                "humidity": weather_data.get("relativeHumidity"),
                "condition": weather_data.get("weatherTypeDTO", {}).get("weatherTypeDescription"),
                "windSpeedKmh": round(float(weather_data.get("windSpeed", 0) or 0) * 3.6, 1) if weather_data.get("windSpeed") else None,
            }

        start_time_local = summary.get("startTimeLocal") or summary.get("startTimeGMT") or ""
        date_part = start_time_local.split(" ")[0] if start_time_local else ""
        if not date_part and summary.get("startTimeGMT"):
            date_part = summary.get("startTimeGMT", "").split("T")[0]

        detail = {
            "id": activity_id,
            "activityName": raw_act.get("activityName") or summary.get("activityName") or "אימון ריצה",
            "date": date_part,
            "startTime": start_time_local,
            "distanceKm": dist_km,
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

    def handle_schedule_workout(self, body):
        tokens = body.get("tokens", "")
        if not tokens:
            self._send_json(400, {"error": "טוקן חיבור של Garmin נדרש"})
            return

        date_str = str(body.get("date", "")).strip()
        if not date_str:
            self._send_json(400, {"error": "תאריך האימון (date) נדרש בפורמט YYYY-MM-DD"})
            return

        workout_name = body.get("workoutName") or body.get("title") or "אימון ריצה"
        description = body.get("description") or body.get("coachDescription") or ""
        try:
            distance_km = float(body.get("distanceKm") or body.get("plannedDistance") or 0)
        except (ValueError, TypeError):
            distance_km = 0.0

        try:
            duration_minutes = float(body.get("durationMinutes") or 0)
        except (ValueError, TypeError):
            duration_minutes = 0.0

        steps = body.get("steps") or []
        push_to_device = body.get("pushToDevice", True)

        tokens_str = parse_tokens_str(tokens)
        garmin = Garmin()
        try:
            garmin.client.loads(tokens_str)
            with garmin.client._token_lock:
                if garmin.client.di_refresh_token and garmin.client._token_expires_soon():
                    garmin.client._refresh_session()
        except Exception as e:
            self._send_json(401, {"error": f"טוקן Garmin אינו תקין: {str(e)}", "code": "INVALID_TOKEN"})
            return

        try:
            workout_payload = build_garmin_running_workout(
                workout_name=workout_name,
                description=description,
                distance_km=distance_km,
                duration_minutes=duration_minutes,
                steps=steps
            )

            upload_res = garmin.upload_workout(workout_payload)
            workout_id = upload_res.get("workoutId")
            if not workout_id:
                raise ValueError(f"Garmin did not return a workoutId: {upload_res}")

            logger.info(f"Successfully uploaded workout {workout_id}: '{workout_name}'")

            scheduled_date = None
            try:
                garmin.schedule_workout(workout_id, date_str)
                scheduled_date = date_str
                logger.info(f"Successfully scheduled workout {workout_id} on {date_str}")
            except Exception as se:
                logger.warning(f"Could not schedule workout on calendar: {se}")

            pushed_to_device = False
            if push_to_device:
                try:
                    garmin.push_workout_to_device(workout_id)
                    pushed_to_device = True
                    logger.info(f"Successfully pushed workout {workout_id} to device queue")
                except Exception as pe:
                    logger.info(f"Push to device skipped or device not found: {pe}")

            dumped_tokens = garmin.client.dumps()
            self._send_json(200, {
                "status": "SUCCESS",
                "workoutId": workout_id,
                "workoutName": workout_name,
                "scheduledDate": scheduled_date or date_str,
                "pushedToDevice": pushed_to_device,
                "tokens": dumped_tokens,
                "message": f"האימון '{workout_name}' נשלח בהצלחה לחשבון ה-Garmin ולשעון של המתאמן לתאריך {date_str}!"
            })
        except Exception as e:
            logger.error(f"Failed to schedule workout: {e}", exc_info=True)
            self._send_json(500, {"error": f"שגיאה בהעלאת האימון ל-Garmin: {str(e)}"})

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
