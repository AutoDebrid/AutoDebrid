# --- Core Imports ---
import os
import sys
import json
import time
import logging
import re
import shutil
import subprocess
from threading import Thread
from datetime import datetime
from functools import wraps

# --- Dependency Imports ---
import requests
from dotenv import load_dotenv, set_key
from flask import Flask, render_template, jsonify, request, abort
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename

# --- Load Environment Variables ---
# This loads the variables from your .env file
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)


# --- Configuration Loading from Environment ---
# Secrets
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY')
REAL_DEBRID_API_KEY = os.environ.get('REAL_DEBRID_API_KEY')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY')
PUSHOVER_USER_KEY = os.environ.get('PUSHOVER_USER_KEY')
PUSHOVER_API_TOKEN = os.environ.get('PUSHOVER_API_TOKEN')

# Paths & Settings
JDOWNLOADER_WATCH_FOLDER = os.environ.get('JDOWNLOADER_WATCH_FOLDER')
SOURCE_FOLDER = os.environ.get('SOURCE_FOLDER')
LOCAL_MOVE_PATH = os.environ.get('LOCAL_MOVE_PATH')
RADARR_ROOT_PATH = os.environ.get('RADARR_ROOT_PATH')
RADARR_URL = os.environ.get('RADARR_URL', '').rstrip('/')
# If the value from .env is an empty string, fall back to '60'.
CHECK_INTERVAL_SECONDS = int(os.environ.get('CHECK_INTERVAL_SECONDS') or 60)

# --- Startup Sanity Checks (Modified to not exit) ---
IS_FULLY_CONFIGURED = True
REQUIRED_VARS = [
    'FLASK_SECRET_KEY', 'INTERNAL_API_KEY', 'REAL_DEBRID_API_KEY', 'RADARR_API_KEY',
    'JDOWNLOADER_WATCH_FOLDER', 'SOURCE_FOLDER', 'LOCAL_MOVE_PATH', 'RADARR_ROOT_PATH', 'RADARR_URL'
]
missing_vars = [var for var in REQUIRED_VARS if not os.environ.get(var)]
if missing_vars:
    IS_FULLY_CONFIGURED = False
    logging.warning(
        f"WARNING: The application is not fully configured. Missing variables: {', '.join(missing_vars)}. "
        "Core services will be disabled until all settings are provided via the web UI."
    )

# --- Flask App Initialization ---
app = Flask(__name__)
# Use a default, insecure key for the initial setup if one isn't provided.
# This is necessary for the settings page to function before it's been configured.
app.config['SECRET_KEY'] = FLASK_SECRET_KEY or 'temporary-insecure-key-for-initial-setup'
if not FLASK_SECRET_KEY:
    logging.warning("WARNING: Using a temporary, insecure Flask secret key. Please set one on the Settings page.")

csrf = CSRFProtect(app)


# --- Security Decorator (Modified for Initial Setup) ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided_key = request.headers.get('X-Api-Key')

        # If the internal API key is not yet configured in the .env file,
        # we enter a special "setup mode". In this mode, we only allow access
        # to the settings endpoints, and we require *any* key to be sent
        # to prevent trivial access, but we don't validate it.
        if not INTERNAL_API_KEY:
            if request.endpoint in ['get_settings', 'save_settings']:
                if provided_key:
                    return f(*args, **kwargs)
                else:
                    # No key was provided at all.
                    abort(401)
            else:
                # Trying to access a protected endpoint other than settings in setup mode.
                abort(403) # Forbidden

        # If the internal API key IS configured, enforce it strictly for all endpoints.
        elif provided_key != INTERNAL_API_KEY:
            logging.warning(f"Unauthorized API access attempt from IP: {request.remote_addr}")
            abort(401) # Incorrect key

        return f(*args, **kwargs)
    return decorated_function


# --- Global State Variables ---
jdownloader_process = None
is_movie_processor_running = False
last_movie_run_stats = {}
JD_STATUS_FILE = "jd_status.json"


# --- Notification Helper ---
def send_notification(title, message):
    """Sends a push notification via Pushover if configured."""
    if not IS_FULLY_CONFIGURED or not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        return
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_API_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "title": title,
            "message": message
        }, timeout=10)
        logging.info(f"Sent notification: {title}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to send notification: {e}")


# --- JDownloader Logic (Hardened) ---
def jdownloader_automation_logic():
    processed_torrents_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_torrents.json")

    def get_processed_torrents():
        if not os.path.exists(processed_torrents_file):
            return set()
        try:
            with open(processed_torrents_file, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            return set()

    def save_processed_torrents(processed_ids):
        with open(processed_torrents_file, "w") as f:
            json.dump(list(processed_ids), f)

    def get_unrestricted_link(link):
        headers = {"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"}
        data = {"link": link}
        try:
            response = requests.post("https://api.real-debrid.com/rest/1.0/unrestrict/link", headers=headers, data=data, timeout=10)
            response.raise_for_status()
            return response.json().get("download")
        except requests.exceptions.RequestException as e:
            logging.error(f"RD Unrestrict Link Error: {e}")
            return None

    def create_crawljob_file(torrent_name, download_links):
        safe_base_name = secure_filename(torrent_name) or "unnamed_download"
        file_path = os.path.join(JDOWNLOADER_WATCH_FOLDER, f"{safe_base_name}.crawljob")
        
        abs_watch_folder = os.path.abspath(JDOWNLOADER_WATCH_FOLDER)
        abs_file_path = os.path.abspath(file_path)
        if os.path.commonprefix([abs_file_path, abs_watch_folder]) != abs_watch_folder:
            logging.error(f"SECURITY ALERT: Path traversal detected and blocked for filename: '{torrent_name}'")
            return

        links_text = '\\n'.join(download_links)
        
        content = (
            f"text={links_text}\n"
            f"packageName={safe_base_name}\n"
            f"autoStart=TRUE\n"
            f"forcedStart=TRUE\n"
        )
        try:
            with open(file_path, "w", encoding='utf-8') as f:
                f.write(content)
            logging.info(f"Created .crawljob for: {safe_base_name}")
            send_notification("Download Sent to JDownloader", torrent_name)
        except IOError as e:
            logging.error(f"Failed to write .crawljob file: {e}")

    logging.info("Starting Real-Debrid to JDownloader automation loop...")
    while True:
        try:
            headers = {"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"}
            response = requests.get("https://api.real-debrid.com/rest/1.0/torrents", headers=headers, timeout=15)
            response.raise_for_status()
            
            torrents = response.json()
            processed_torrents = get_processed_torrents()
            for torrent in torrents:
                if torrent["id"] not in processed_torrents and torrent["status"] == "downloaded":
                    logging.info(f"Found new completed torrent: {torrent['filename']}")
                    unrestricted_links = [link for link in (get_unrestricted_link(l) for l in torrent.get("links", [])) if link]
                    if unrestricted_links:
                        create_crawljob_file(torrent["filename"], unrestricted_links)
                        processed_torrents.add(torrent["id"])
            save_processed_torrents(processed_torrents)
            
            with open(JD_STATUS_FILE, "w") as f:
                json.dump({"last_check": datetime.utcnow().isoformat()}, f)

        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking Real-Debrid: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred in the JDownloader loop: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL_SECONDS)


# --- Radarr/Mover Logic (Hardened & Updated) ---
def get_info_from_folder_name(name):
    """Extracts a clean title and year from a folder name."""
    title, year = name, None
    # Regex to find a year in parentheses or preceded by a dot/space
    match = re.search(r'^(.*?)[.(]\s*(\d{4})\s*[).]', name)
    if match:
        title = match.group(1).replace('.', ' ').strip()
        year = int(match.group(2))
    else:
        # If no year found, just clean up the title
        title = name.replace('.', ' ')

    # Aggressively clean up common release tags
    tags_pattern = r'\b(1080p|720p|4k|2160p|bluray|dvdrip|webrip|web-dl|x264|x265|h264|h265|aac|dts|ac3|ddp|5\.1|remux|repack|proper|internal)\b'
    cleaned_title = re.sub(tags_pattern, '', title, flags=re.IGNORECASE)
    # Remove any resulting double spaces
    cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()
    
    return cleaned_title or title, year # Fallback to original title if cleaning removes everything

def movie_organizer_logic():
    processed_count, skipped_count = 0, 0
    error_message = None
    
    def radarr_api_request(method, endpoint, json_data=None):
        url = f"{RADARR_URL}/api/v3/{endpoint}"
        headers = {"X-Api-Key": RADARR_API_KEY}
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=20)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=json_data, timeout=20)
            else:
                return None
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Radarr API {method} Error for endpoint '{endpoint}': {e}")
            if e.response is not None:
                logging.error(f"Radarr Response Body: {e.response.text}")
            return None
            
    # --- Pre-flight Check for Radarr Configuration ---
    logging.info("Performing pre-flight check on Radarr configuration...")
    radarr_root_folders = radarr_api_request('GET', 'rootfolder')
    if radarr_root_folders is None:
        error_message = "Could not communicate with Radarr to get root folders. Check URL and API Key."
        logging.critical(error_message)
        return {"processed": 0, "skipped": len(os.listdir(SOURCE_FOLDER)), "error": error_message}

    valid_radarr_paths = [rf['path'] for rf in radarr_root_folders]
    if RADARR_ROOT_PATH not in valid_radarr_paths:
        error_message = (f"Configuration Mismatch! The 'Radarr Root Folder' path you provided ('{RADARR_ROOT_PATH}') "
                         f"is NOT valid. Radarr reported its valid paths are: {valid_radarr_paths}. "
                         "Please update your settings to match one of these exactly.")
        logging.critical(error_message)
        return {"processed": 0, "skipped": len(os.listdir(SOURCE_FOLDER)), "error": error_message}
    
    logging.info(f"Radarr root folder '{RADARR_ROOT_PATH}' validated successfully.")
    
    profiles = radarr_api_request('GET', "qualityprofile")
    if not profiles:
        logging.error("Could not fetch quality profiles from Radarr. Aborting movie processing.")
        return {"processed": 0, "skipped": len(os.listdir(SOURCE_FOLDER)), "error": "Could not fetch Radarr quality profiles."}
    
    quality_profile_id = next((p['id'] for p in profiles if '1080p' in p['name']), profiles[0]['id'])

    logging.info("--- Starting Movie Processing Task ---")
    for item_name in os.listdir(SOURCE_FOLDER):
        source_path = os.path.join(SOURCE_FOLDER, item_name)
        
        if not os.path.isdir(source_path):
            skipped_count += 1
            continue
        
        logging.info(f"Processing directory: {item_name}")
        title, year = get_info_from_folder_name(item_name)
        if not title:
            logging.warning(f"Could not extract a valid title from '{item_name}'. Skipping.")
            skipped_count += 1
            continue

        search_term = f"{title} {year}" if year else title
        lookup_results = radarr_api_request('GET', f"movie/lookup?term={requests.utils.quote(search_term)}")
        if not lookup_results:
            logging.warning(f"Could not find '{search_term}' in Radarr's lookup. Skipping.")
            skipped_count += 1
            continue
        
        movie_info_from_lookup = lookup_results[0]
        tmdb_id = movie_info_from_lookup.get('tmdbId')

        existing_movies_in_library = radarr_api_request('GET', f"movie?tmdbId={tmdb_id}")
        
        radarr_movie_object = None
        if existing_movies_in_library:
            radarr_movie_object = existing_movies_in_library[0]
            logging.info(f"Movie '{radarr_movie_object['title']}' ({radarr_movie_object['year']}) already exists in Radarr.")
        else:
            logging.info(f"Movie '{movie_info_from_lookup['title']}' ({movie_info_from_lookup['year']}) not in Radarr. Attempting to add.")
            add_options = {"searchForMovie": False}
            
            movie_data_to_add = {
                "title": movie_info_from_lookup.get('title'),
                "year": movie_info_from_lookup.get('year'),
                "qualityProfileId": quality_profile_id,
                "titleSlug": movie_info_from_lookup.get('titleSlug'),
                "images": movie_info_from_lookup.get('images', []),
                "tmdbId": movie_info_from_lookup.get('tmdbId'),
                "rootFolderPath": RADARR_ROOT_PATH,
                "monitored": True,
                "addOptions": add_options
            }

            radarr_movie_object = radarr_api_request('POST', "movie", json_data=movie_data_to_add)
            if not (radarr_movie_object and 'id' in radarr_movie_object):
                logging.error(f"Failed to add movie '{movie_info_from_lookup.get('title')}' to Radarr. The POST request failed.")
                skipped_count += 1
                continue

        if not radarr_movie_object or 'folderName' not in radarr_movie_object:
            logging.error(f"Failed to get final Radarr movie object for '{item_name}'. Skipping.")
            skipped_count += 1
            continue
            
        clean_folder_name = os.path.basename(radarr_movie_object['folderName'])
        destination_path = os.path.join(LOCAL_MOVE_PATH, clean_folder_name)
        
        try:
            if os.path.isdir(destination_path):
                logging.warning(f"Destination folder '{destination_path}' already exists. Skipping move.")
                shutil.rmtree(source_path) # Remove the source to avoid reprocessing
                logging.info(f"Removed source directory to prevent reprocessing: {source_path}")
                skipped_count += 1
            else:
                shutil.move(source_path, destination_path)
                logging.info(f"Successfully moved '{item_name}' to '{destination_path}'.")
                processed_count += 1
        except Exception as e:
            logging.error(f"Failed to move '{item_name}': {e}")
            skipped_count += 1
    
    # FIX: Use 'DownloadedMoviesScan' command instead of 'RefreshMovie'
    # This is more robust for importing newly added files and prevents duplicates.
    if processed_count > 0:
        logging.info(f"Triggering Radarr 'DownloadedMoviesScan' command to import {processed_count} new movie(s)...")
        radarr_api_request('POST', "command", json_data={"name": "DownloadedMoviesScan"})
    
    logging.info(f"--- Movie processing complete. Processed: {processed_count}, Skipped: {skipped_count} ---")
    return {"processed": processed_count, "skipped": skipped_count, "error": error_message}


# --- Flask API Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/get-settings', methods=['GET'])
@require_api_key
def get_settings():
    """Fetches current settings from environment variables to populate the form."""
    settings = {}
    keys_to_fetch = [
        'FLASK_SECRET_KEY', 'INTERNAL_API_KEY', 'REAL_DEBRID_API_KEY', 
        'RADARR_API_KEY', 'PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN',
        'JDOWNLOADER_WATCH_FOLDER', 'SOURCE_FOLDER', 'LOCAL_MOVE_PATH',
        'RADARR_ROOT', 'RADARR_URL', 'CHECK_INTERVAL_SECONDS'
    ]
    for key in keys_to_fetch:
        if key == 'RADARR_ROOT':
             settings[key] = os.environ.get('RADARR_ROOT_PATH', '')
        else:
             settings[key] = os.environ.get(key, '')
    return jsonify(settings)

@app.route('/api/save-settings', methods=['POST'])
@require_api_key
def save_settings():
    """Receives settings from the frontend and saves them to the .env file."""
    data = request.get_json()
    if not data:
        return jsonify(status="Error: No data received."), 400

    try:
        dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
        if not os.path.exists(dotenv_path):
            open(dotenv_path, 'a').close()
            logging.info("Created .env file as it did not exist.")

        for key, value in data.items():
            env_key = 'RADARR_ROOT_PATH' if key == 'RADARR_ROOT' else key
            set_key(dotenv_path, env_key, str(value))
        
        logging.info(f"Settings successfully saved to .env file by user from IP: {request.remote_addr}")
        return jsonify(status="Settings saved. Please restart the application for changes to take effect.")

    except Exception as e:
        logging.error(f"Failed to save settings to .env file: {e}", exc_info=True)
        return jsonify(status=f"Error saving settings: {e}"), 500

@app.route('/logs')
def get_logs():
    try:
        with open("app.log", "r") as f:
            return jsonify(logs="".join(f.readlines()[-100:]))
    except FileNotFoundError:
        return jsonify(logs="Log file not found."), 404
    except Exception as e:
        return jsonify(logs=f"Error reading log file: {e}"), 500

@app.route('/start_jdownloader', methods=['POST'])
@require_api_key
def start_jdownloader():
    global jdownloader_process
    if not IS_FULLY_CONFIGURED:
        return jsonify(status=f"Cannot start: App not configured. Missing: {', '.join(missing_vars)}"), 503
    if jdownloader_process and jdownloader_process.poll() is None:
        return jsonify(status="JDownloader watcher is already running.")
    jdownloader_process = subprocess.Popen([sys.executable, 'app.py', 'jdownloader'])
    msg = f"JDownloader watcher started with PID: {jdownloader_process.pid}"
    logging.info(msg)
    send_notification("Service Started", "JDownloader Watcher is now running.")
    return jsonify(status=msg)

@app.route('/stop_jdownloader', methods=['POST'])
@require_api_key
def stop_jdownloader():
    global jdownloader_process
    if not jdownloader_process or jdownloader_process.poll() is not None:
        return jsonify(status="JDownloader watcher is not running.")
    pid = jdownloader_process.pid
    jdownloader_process.terminate()
    if os.path.exists(JD_STATUS_FILE):
        os.remove(JD_STATUS_FILE)
    jdownloader_process = None
    msg = f"JDownloader watcher (PID: {pid}) stopped."
    logging.info(msg)
    send_notification("Service Stopped", "JDownloader Watcher has been stopped.")
    return jsonify(status=msg)

@app.route('/process_movies', methods=['POST'])
@require_api_key
def process_movies():
    global is_movie_processor_running
    if not IS_FULLY_CONFIGURED:
        return jsonify(status=f"Cannot start: App not configured. Missing: {', '.join(missing_vars)}"), 503
    if is_movie_processor_running:
        return jsonify(status="Movie processing is already in progress."), 409

    def task_wrapper():
        global is_movie_processor_running, last_movie_run_stats
        is_movie_processor_running = True
        run_summary = {}
        try:
            run_summary = movie_organizer_logic()
            notification_message = f"Processed {run_summary.get('processed', 0)}, skipped {run_summary.get('skipped', 0)}."
            if run_summary.get('error'):
                notification_message += f" Error: {run_summary.get('error')}"

            send_notification(
                "Movie Processing Complete",
                notification_message
            )
        except Exception as e:
            logging.error(f"Critical error in movie organizer: {e}", exc_info=True)
            run_summary = {"error": str(e)}
            send_notification("Movie Processing FAILED", f"Error: {e}")
        finally:
            is_movie_processor_running = False
            last_movie_run_stats = {
                "completed_at": datetime.utcnow().isoformat(),
                "summary": run_summary
            }

    Thread(target=task_wrapper, daemon=True).start()
    return jsonify(status="Movie processing started. Check logs for progress.")

@app.route('/status', methods=['GET'])
def status():
    jd_status = {'state': "Stopped"}
    if jdownloader_process and jdownloader_process.poll() is None:
        jd_status['state'] = "Running"
        jd_status['pid'] = jdownloader_process.pid
        if os.path.exists(JD_STATUS_FILE):
            try:
                with open(JD_STATUS_FILE, "r") as f:
                    jd_status['last_check'] = json.load(f).get('last_check')
            except (IOError, json.JSONDecodeError):
                jd_status['last_check'] = "N/A"
    
    movie_status = {
        "state": "Running" if is_movie_processor_running else "Idle",
        "last_run": last_movie_run_stats
    }
    return jsonify(jdownloader=jd_status, movie_organizer=movie_status)

# --- Main Entry Point ---
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'jdownloader':
        if not IS_FULLY_CONFIGURED:
            logging.error("Cannot start JDownloader process: Application is not configured.")
            sys.exit(1)
        jdownloader_automation_logic()
    else:
        if not IS_FULLY_CONFIGURED and JDOWNLOADER_WATCH_FOLDER and not os.path.isdir(JDOWNLOADER_WATCH_FOLDER):
            logging.error(f"JDownloader watch folder not found: {JDOWNLOADER_WATCH_FOLDER}")
        
        logging.info("Flask application starting up.")
        if 'werkzeug' not in os.environ.get('SERVER_SOFTWARE', ''):
             logging.warning("Running in DEVELOPMENT mode. Do NOT use this mode in production. Use a WSGI server like Gunicorn.")
        app.run(host='0.0.0.0', port=5000, debug=False)
