from flask import Flask, render_template, jsonify
import subprocess
import os
import re
import shutil
import requests
import json
import time
from threading import Thread
import logging
import configparser
from datetime import datetime
# NEW: Import secure_filename for robust filename sanitization
from werkzeug.utils import secure_filename
# NEW: To read command-line arguments for the subprocess fix
import sys

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
# Silence the default Werkzeug logger
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)


# --- Configuration Loading ---
# For production, it is strongly recommended to load secrets from environment
# variables rather than a plain text file.
config = configparser.ConfigParser()
config.read('config.ini')

# -- API and Path Config --
REAL_DEBRID_API_KEY = config['API_KEYS']['REAL_DEBRID']
JDOWNLOADER_WATCH_FOLDER = config['PATHS']['JDOWNLOADER_WATCH']
CHECK_INTERVAL_SECONDS = config.getint('SETTINGS', 'CHECK_INTERVAL_SECONDS')
SOURCE_FOLDER = config['PATHS']['SOURCE_FOLDER']
LOCAL_MOVE_PATH = config['PATHS']['LOCAL_MOVE_PATH']
RADARR_ROOT_PATH = config['PATHS']['RADARR_ROOT']
RADARR_URL = config['ENDPOINTS']['RADARR_URL'].rstrip('/')
RADARR_API_KEY = config['API_KEYS']['RADARR']

# NEW: Pushover Notification Config
PUSHOVER_USER_KEY = config['NOTIFICATIONS']['PUSHOVER_USER_KEY']
PUSHOVER_API_TOKEN = config['NOTIFICATIONS']['PUSHOVER_API_TOKEN']


# --- Flask App Initialization ---
app = Flask(__name__)
# It is critical to set a secret key for session management and other security features.
# For production, this should be a long, random string loaded from an environment variable.
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-insecure-fallback-key')


# --- Global State Variables ---
jdownloader_process = None
is_movie_processor_running = False
last_movie_run_stats = {}
JD_STATUS_FILE = "jd_status.json"


# --- NEW: Notification Helper ---
def send_notification(title, message):
    """Sends a push notification via Pushover if configured."""
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        return # Skip if keys are not configured

    payload = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "title": title,
        "message": message
    }
    try:
        requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
        logging.info(f"Sent notification: {title}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to send notification: {e}")


# --- JDownloader Logic (MODIFIED for Security) ---
def jdownloader_automation_logic():
    """Contains the full logic for the JDownloader watcher daemon."""
    processed_torrents_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_torrents.json")

    def get_processed_torrents():
        if os.path.exists(processed_torrents_file):
            with open(processed_torrents_file, "r") as f:
                try:
                    return set(json.load(f))
                except json.JSONDecodeError:
                    return set()
        return set()

    def save_processed_torrents(processed_ids):
        with open(processed_torrents_file, "w") as f:
            json.dump(list(processed_ids), f)

    def get_unrestricted_link(link):
        headers = {"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"}
        data = {"link": link}
        response = requests.post("https://api.real-debrid.com/rest/1.0/unrestrict/link", headers=headers, data=data)
        if response.status_code == 200:
            return response.json()["download"]
        return None

    def create_crawljob_file(torrent_name, download_links):
        """
        Securely creates a .crawljob file, preventing path traversal.
        """
        safe_base_name = secure_filename(torrent_name)
        if not safe_base_name:
            safe_base_name = "unnamed_download"

        file_path = os.path.join(JDOWNLOADER_WATCH_FOLDER, f"{safe_base_name}.crawljob")
        abs_watch_folder = os.path.abspath(JDOWNLOADER_WATCH_FOLDER)
        abs_file_path = os.path.abspath(file_path)

        if os.path.commonprefix([abs_file_path, abs_watch_folder]) != abs_watch_folder:
            logging.error(f"SECURITY ALERT: Path traversal attack detected and blocked. "
                          f"Original Filename: '{torrent_name}', "
                          f"Sanitized Filename: '{safe_base_name}'")
            return

        links_as_string = "\\n".join(download_links)
        content = (
            f"text={links_as_string}\\n"
            f"packageName={safe_base_name}\\n"
            f"autoStart=TRUE\\n"
            f"forcedStart=TRUE\\n"
        )
        try:
            with open(file_path, "w", encoding='utf-8') as f:
                f.write(content)
            logging.info(f"Created .crawljob file for: {safe_base_name}")
            send_notification("New Download Sent to JDownloader", torrent_name)
        except IOError as e:
            logging.error(f"Failed to write .crawljob file at {file_path}: {e}")

    logging.info("Starting Real-Debrid to JDownloader automation loop...")
    while True:
        try:
            response = requests.get("https://api.real-debrid.com/rest/1.0/torrents", headers={"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"})
            if response.status_code == 200:
                torrents = response.json()
                processed_torrents = get_processed_torrents()
                for torrent in torrents:
                    if torrent["id"] not in processed_torrents and torrent["status"] == "downloaded":
                        logging.info(f"Found new completed torrent: {torrent['filename']}")
                        links = [link for link in (get_unrestricted_link(l) for l in torrent.get("links", [])) if link]
                        if links:
                            create_crawljob_file(torrent["filename"], links)
                            processed_torrents.add(torrent["id"])
                save_processed_torrents(processed_torrents)
            else:
                logging.error(f"Error checking Real-Debrid: {response.status_code} - {response.text}")

            with open(JD_STATUS_FILE, "w") as f:
                json.dump({"last_check": datetime.utcnow().isoformat()}, f)

        except requests.exceptions.RequestException as e:
            logging.error(f"An error occurred connecting to Real-Debrid: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


# --- Radarr/Mover Logic (MODIFIED: Full implementation) ---
def movie_organizer_logic():
    """
    Contains the full logic for the Radarr movie organizer.
    This function is now fully implemented.
    """
    processed_count = 0
    skipped_count = 0
    error_message = None
    # NEW: List to hold Radarr movie IDs for a final, targeted scan
    movie_ids_to_refresh = []

    # --- Helper functions for Radarr API interaction ---
    def radarr_api_get(endpoint):
        try:
            response = requests.get(f"{RADARR_URL}/api/v3/{endpoint}", headers={"X-Api-Key": RADARR_API_KEY}, timeout=15)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Radarr API GET Error for endpoint '{endpoint}': {e}")
            return None

    def radarr_api_post(endpoint, data):
        try:
            response = requests.post(f"{RADARR_URL}/api/v3/{endpoint}", headers={"X-Api-Key": RADARR_API_KEY}, json=data, timeout=15)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Radarr API POST Error for endpoint '{endpoint}': {e}")
            return None
            
    def get_info_from_folder_name(name):
        """Extracts and cleans movie title and year from a folder name."""
        title = name
        year = None
        
        match = re.search(r'^(.*?)\s*\((\d{4})\)', name)
        if match:
            title = match.group(1)
            year = int(match.group(2))
        else:
            match = re.search(r'^(.*?)[.\s](\d{4})([.\s]|$)', name)
            if match:
                title = match.group(1)
                year = int(match.group(2))

        cleaned_title = re.sub(r'[._]', ' ', title)
        cleaned_title = re.sub(r'^\d+\s*[-.]*\s*', '', cleaned_title)
        
        release_tags = [
            '1080p', '720p', '4k', '2160p', 'bluray', 'dvdrip', 'webrip', 'web-dl', 
            'x264', 'x265', 'h264', 'h265', 'aac', 'dts', 'ac3'
        ]
        for tag in release_tags:
            cleaned_title = re.sub(r'\b' + tag + r'\b', '', cleaned_title, flags=re.IGNORECASE)

        cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()
        
        return cleaned_title, year

    def get_radarr_quality_profile_id():
        """Fetches the ID of the 'HD - 720p/1080p' quality profile."""
        profiles = radarr_api_get("qualityprofile")
        if profiles:
            for profile in profiles:
                if 'HD - 720p/1080p' in profile['name']:
                    return profile['id']
        logging.warning("Could not find a matching quality profile in Radarr.")
        return 1

    def lookup_movie_in_radarr(title, year):
        """Looks up a movie by title and year to get its TMDB ID."""
        search_term = f"{title} {year}" if year else title
        logging.info(f"Looking up in Radarr with search term: '{search_term}'")
        lookup_result = radarr_api_get(f"movie/lookup?term={requests.utils.quote(search_term)}")
        if lookup_result:
            return lookup_result[0]
        return None

    def add_movie_to_radarr(movie_info, quality_profile_id):
        """Adds a new movie to Radarr."""
        add_options = {
            "searchForMovie": True,
            "monitor": "movieOnly",
            "ignoreEpisodesWithFiles": False,
            "ignoreEpisodesWithoutFiles": False,
        }
        movie_data = {
            "title": movie_info['title'],
            "year": movie_info['year'],
            "tmdbId": movie_info['tmdbId'],
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": RADARR_ROOT_PATH,
            "monitored": True,
            "addOptions": add_options,
        }
        return radarr_api_post("movie", data=movie_data)

    # --- Main execution for the movie organizer ---
    if not os.path.isdir(LOCAL_MOVE_PATH):
        os.makedirs(LOCAL_MOVE_PATH, exist_ok=True)
    
    quality_profile_id = get_radarr_quality_profile_id()
    if not quality_profile_id:
        error_message = "Could not get Radarr quality profile. Aborting."
        logging.error(error_message)
        return {"processed": 0, "skipped": len(os.listdir(SOURCE_FOLDER)), "error": error_message}

    logging.info("--- Starting Movie Processing Task ---")
    for item_name in os.listdir(SOURCE_FOLDER):
        source_path = os.path.join(SOURCE_FOLDER, item_name)
        if not os.path.isdir(source_path):
            logging.info(f"Skipping '{item_name}' as it is not a directory.")
            skipped_count += 1
            continue
        
        logging.info(f"Processing directory: {item_name}")
        title, year = get_info_from_folder_name(item_name)
        
        if not title:
            logging.warning(f"Could not extract a valid title from '{item_name}'. Skipping.")
            skipped_count += 1
            continue

        movie_info = lookup_movie_in_radarr(title, year)
        if not movie_info:
            logging.warning(f"Could not find '{title}' in Radarr's lookup. Skipping.")
            skipped_count += 1
            continue
            
        clean_folder_name = None
        radarr_internal_id = None
        radarr_movie = add_movie_to_radarr(movie_info, quality_profile_id)
        
        if radarr_movie and 'folderName' in radarr_movie:
            clean_folder_name = os.path.basename(radarr_movie['folderName'])
            radarr_internal_id = radarr_movie.get('id')
            logging.info(f"Movie '{title}' added to Radarr. Target folder: '{clean_folder_name}'")
        else:
            existing_movies = radarr_api_get(f"movie?tmdbId={movie_info['tmdbId']}")
            if existing_movies and len(existing_movies) > 0:
                logging.info(f"Movie '{title}' already exists in Radarr. Skipping add.")
                clean_folder_name = os.path.basename(existing_movies[0].get('folderName'))
                radarr_internal_id = existing_movies[0].get('id')
            else:
                logging.error(f"Failed to add '{title}' to Radarr and it doesn't appear to exist. Skipping.")
                skipped_count += 1
                continue

        if not clean_folder_name:
            logging.error(f"Could not determine a clean folder name for '{title}'. Skipping move.")
            skipped_count += 1
            continue

        # Add the movie's internal ID to our list for the final scan
        if radarr_internal_id:
            movie_ids_to_refresh.append(radarr_internal_id)

        destination_path = os.path.join(LOCAL_MOVE_PATH, clean_folder_name)
        try:
            shutil.move(source_path, destination_path)
            logging.info(f"Successfully moved and renamed '{item_name}' to '{destination_path}'.")
            processed_count += 1
        except Exception as e:
            logging.error(f"Failed to move '{item_name}' to '{destination_path}': {e}")
            skipped_count += 1
    
    # MODIFIED: Trigger a more specific and reliable scan command at the end
    if movie_ids_to_refresh:
        logging.info(f"Triggering Radarr refresh for {len(movie_ids_to_refresh)} movies...")
        radarr_api_post("command", data={"name": "RefreshMovie", "movieIds": movie_ids_to_refresh})
    else:
        logging.info("No movies were processed, skipping Radarr refresh.")
    
    logging.info(f"--- Movie processing complete. Processed: {processed_count}, Skipped: {skipped_count} ---")
    return {"processed": processed_count, "skipped": skipped_count, "error": error_message}


# --- Flask API Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

# NEW: Endpoint to get application logs
@app.route('/logs')
def get_logs():
    """Reads the last 100 lines of the log file."""
    try:
        with open("app.log", "r") as f:
            lines = f.readlines()
            # Get the last 100 lines
            last_100_lines = lines[-100:]
            return jsonify(logs="".join(last_100_lines))
    except FileNotFoundError:
        return jsonify(logs="Log file not found."), 404
    except Exception as e:
        logging.error(f"Error reading log file for UI: {e}")
        return jsonify(logs=f"Error reading log file: {e}"), 500

@app.route('/start_jdownloader', methods=['POST'])
def start_jdownloader():
    global jdownloader_process
    if jdownloader_process and jdownloader_process.poll() is None:
        return jsonify(status="JDownloader watcher is already running.")

    jdownloader_process = subprocess.Popen(['python3', 'app.py', 'jdownloader'])
    msg = f"JDownloader watcher started with PID: {jdownloader_process.pid}"
    logging.info(msg)
    send_notification("Service Started", "JDownloader Watcher is now running.")
    return jsonify(status=msg)

@app.route('/stop_jdownloader', methods=['POST'])
def stop_jdownloader():
    global jdownloader_process
    if jdownloader_process and jdownloader_process.poll() is None:
        pid = jdownloader_process.pid
        jdownloader_process.terminate()
        if os.path.exists(JD_STATUS_FILE):
            try:
                os.remove(JD_STATUS_FILE)
            except OSError as e:
                logging.error(f"Error removing status file: {e}")
        jdownloader_process = None
        msg = f"JDownloader watcher (PID: {pid}) stopped."
        logging.info(msg)
        send_notification("Service Stopped", "JDownloader Watcher has been stopped.")
        return jsonify(status=msg)
    return jsonify(status="JDownloader watcher is not running.")

@app.route('/process_movies', methods=['POST'])
def process_movies():
    global is_movie_processor_running, last_movie_run_stats
    if is_movie_processor_running:
        return jsonify(status="Movie processing is already in progress."), 409

    def task_wrapper():
        global is_movie_processor_running, last_movie_run_stats
        is_movie_processor_running = True
        run_summary = {}
        try:
            run_summary = movie_organizer_logic()
            send_notification(
                "Movie Processing Complete",
                f"Processed {run_summary.get('processed', 0)} and skipped {run_summary.get('skipped', 0)} items."
            )
        except Exception as e:
            logging.error(f"A critical error occurred in the movie organizer: {e}", exc_info=True)
            run_summary = {"error": str(e)}
            send_notification("Movie Processing FAILED", f"An error occurred: {e}")
        finally:
            is_movie_processor_running = False
            last_movie_run_stats = {
                "completed_at": datetime.utcnow().isoformat(),
                "summary": run_summary
            }

    thread = Thread(target=task_wrapper)
    thread.start()
    return jsonify(status="Movie processing started. Check logs for progress.")

@app.route('/status', methods=['GET'])
def status():
    """Check the detailed status of the background services."""
    global jdownloader_process, is_movie_processor_running, last_movie_run_stats

    jd_status = {}
    if jdownloader_process and jdownloader_process.poll() is None:
        jd_status['state'] = "Running"
        jd_status['pid'] = jdownloader_process.pid
        try:
            with open(JD_STATUS_FILE, "r") as f:
                status_data = json.load(f)
                jd_status['last_check'] = status_data.get('last_check')
        except (FileNotFoundError, json.JSONDecodeError):
            jd_status['last_check'] = "N/A"
    else:
        jd_status['state'] = "Stopped"

    movie_status = {
        "state": "Running" if is_movie_processor_running else "Idle",
        "last_run": last_movie_run_stats
    }

    return jsonify(jdownloader=jd_status, movie_organizer=movie_status)

# --- Main entry point ---
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'jdownloader':
        jdownloader_automation_logic()
    else:
        if not os.path.isdir(JDOWNLOADER_WATCH_FOLDER):
            logging.error(f"JDownloader watch folder not found at: {JDOWNLOADER_WATCH_FOLDER}. Please create it.")
        
        logging.info("Flask application starting up.")
        app.run(host='0.0.0.0', port=5000, debug=False)
