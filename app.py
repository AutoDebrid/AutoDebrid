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

# --- Logging Setup ---
# This will log to a file (app.log) and the console simultaneously.
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
config = configparser.ConfigParser()
config.read('config.ini')

# -- JDownloader Script Config --
REAL_DEBRID_API_KEY = config['API_KEYS']['REAL_DEBRID']
JDOWNLOADER_WATCH_FOLDER = config['PATHS']['JDOWNLOADER_WATCH']
CHECK_INTERVAL_SECONDS = config.getint('SETTINGS', 'CHECK_INTERVAL_SECONDS')

# -- Radarr/Mover Script Config --
SOURCE_FOLDER = config['PATHS']['SOURCE_FOLDER']
LOCAL_MOVE_PATH = config['PATHS']['LOCAL_MOVE_PATH']
RADARR_ROOT_PATH = config['PATHS']['RADARR_ROOT']
RADARR_URL = config['ENDPOINTS']['RADARR_URL']
RADARR_API_KEY = config['API_KEYS']['RADARR']


# --- Flask App Initialization ---
app = Flask(__name__)

# --- Global State Variables ---
jdownloader_process = None
is_movie_processor_running = False # To prevent concurrent runs

# --- Logic from JDownloader Script (Modified for Logging) ---

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
        safe_torrent_name = "".join(c for c in torrent_name if c.isalnum() or c in (' ', '.', '_')).rstrip()
        file_path = os.path.join(JDOWNLOADER_WATCH_FOLDER, f"{safe_torrent_name}.crawljob")
        links_as_string = "\\n".join(download_links)
        content = (
            f"text={links_as_string}\\n"
            f"packageName={safe_torrent_name}\\n"
            f"autoStart=TRUE\\n"
            f"forcedStart=TRUE\\n"
        )
        with open(file_path, "w", encoding='utf-8') as f:
            f.write(content)
        logging.info(f"Created .crawljob file for: {safe_torrent_name}")

    logging.info("Starting Real-Debrid to JDownloader automation loop...")
    while True:
        processed_torrents = get_processed_torrents()
        headers = {"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"}
        try:
            response = requests.get("https://api.real-debrid.com/rest/1.0/torrents", headers=headers)
            if response.status_code == 200:
                torrents = response.json()
                found_new = False
                for torrent in torrents:
                    if torrent["id"] not in processed_torrents and torrent["status"] == "downloaded":
                        found_new = True
                        logging.info(f"Found new completed torrent: {torrent['filename']}")
                        download_links = [get_unrestricted_link(link) for link in torrent.get("links", [])]
                        download_links = [link for link in download_links if link]

                        if download_links:
                            create_crawljob_file(torrent["filename"], download_links)
                            processed_torrents.add(torrent["id"])
                save_processed_torrents(processed_torrents)
                if not found_new:
                    logging.info("No new completed torrents found on Real-Debrid.")
            else:
                logging.error(f"Error checking Real-Debrid: {response.status_code} - {response.text}")
        except requests.exceptions.RequestException as e:
            logging.error(f"An error occurred connecting to Real-Debrid: {e}")
        
        time.sleep(CHECK_INTERVAL_SECONDS)


# --- Logic from Radarr/Mover Script (Modified for Logging) ---

def movie_organizer_logic():
    """Contains the full logic for the Radarr movie organizer."""
    
    def get_info_from_folder_name(folder_name):
        # This function remains the same
        base_name = os.path.splitext(folder_name)[0]
        year_match = re.search(r'(\d{4})', base_name)
        if year_match:
            year = year_match.group(1)
            title = base_name.split(year)[0]
        else:
            year, title = None, base_name
        title = re.sub(r'[._]', ' ', title).strip()
        title = re.sub(r'\[.*?\]', '', title).strip()
        title = re.sub(r'\s+', ' ', title).strip('-_')
        return {'title': title, 'year': year}

    def lookup_movie_in_radarr(title, year):
        headers = {'X-Api-Key': RADARR_API_KEY}
        search_term = f"{title} ({year})" if year else title
        logging.info(f"🎬 Searching Radarr with term: '{search_term}'...")
        try:
            response = requests.get(f"{RADARR_URL}/api/v3/movie/lookup", params={'term': search_term}, headers=headers)
            response.raise_for_status()
            return response.json()[0] if response.json() else None
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ Error looking up movie in Radarr: {e}")
            return None

    def get_radarr_quality_profile_id():
        headers = {'X-Api-Key': RADARR_API_KEY}
        try:
            response = requests.get(f"{RADARR_URL}/api/v3/qualityprofile", headers=headers)
            response.raise_for_status()
            # It's better to find a specific profile if you can, otherwise the first is okay.
            return response.json()[0]['id']
        except Exception as e:
            logging.error(f"❌ Error getting Radarr quality profile: {e}")
            return None

    def add_movie_to_radarr(movie_info, radarr_movie_path, quality_profile_id):
        logging.info(f"📡 Telling Radarr to add movie at path: '{radarr_movie_path}'")
        headers = {'X-Api-Key': RADARR_API_KEY}
        payload = {
            'title': movie_info['title'], 'year': movie_info['year'],
            'qualityProfileId': quality_profile_id, 'tmdbId': movie_info['tmdbId'],
            'titleSlug': movie_info['titleSlug'], 'images': movie_info['images'],
            'path': radarr_movie_path, 'monitored': True,
            'addOptions': {'searchForMovie': False}
        }
        try:
            response = requests.post(f"{RADARR_URL}/api/v3/movie", json=payload, headers=headers)
            response.raise_for_status()
            logging.info(f"✅ Successfully added '{movie_info['title']}' to Radarr.")
        except requests.exceptions.RequestException as e:
            try:
                error = str(e.response.json())
            except:
                error = str(e)
            if "already been added" in error.lower():
                logging.warning(f"ℹ️ Movie '{movie_info['title']}' is already in Radarr.")
            else:
                logging.error(f"❌ Error adding movie to Radarr: {error}")

    def rename_and_move_folder(original_path, movie_info):
        new_folder_name = f"{movie_info['title']} ({movie_info['year']})"
        sanitized_name = re.sub(r'[<>:"/\\|?*]', '', new_folder_name)
        local_destination = os.path.join(LOCAL_MOVE_PATH, sanitized_name)
        try:
            logging.info(f"🚚 Moving file to local path: '{local_destination}'")
            os.makedirs(os.path.dirname(local_destination), exist_ok=True)
            shutil.move(original_path, local_destination)
            return sanitized_name
        except Exception as e:
            logging.error(f"❌ Error moving folder '{os.path.basename(original_path)}': {e}")
            return None

    if not os.path.isdir(LOCAL_MOVE_PATH):
        os.makedirs(LOCAL_MOVE_PATH, exist_ok=True)
    
    quality_profile_id = get_radarr_quality_profile_id()
    if not quality_profile_id:
        logging.error("❌ Could not retrieve Radarr Quality Profile. Aborting movie processing.")
        return

    logging.info("--- Starting Movie Processing Task ---")
    for item_name in os.listdir(SOURCE_FOLDER):
        original_path = os.path.join(SOURCE_FOLDER, item_name)
        if os.path.isdir(original_path) or item_name.endswith(('.mkv', '.mp4', '.avi')):
            logging.info(f"\nProcessing: '{item_name}'")
            parsed_info = get_info_from_folder_name(item_name)
            if not parsed_info['title']:
                logging.warning("❗️ Could not extract a valid title. Skipping.")
                continue
            
            logging.info(f"🧹 Cleaned Info -> Title: '{parsed_info['title']}', Year: '{parsed_info['year']}'")
            movie_info = lookup_movie_in_radarr(parsed_info['title'], parsed_info['year'])
            
            if movie_info:
                logging.info(f"👍 Found match: {movie_info['title']} ({movie_info['year']})")
                newly_created_folder = rename_and_move_folder(original_path, movie_info)
                if newly_created_folder:
                    radarr_path = os.path.join(RADARR_ROOT_PATH, newly_created_folder)
                    add_movie_to_radarr(movie_info, radarr_path, quality_profile_id)
            else:
                logging.warning(f"👎 Could not find a match in Radarr for '{parsed_info['title']}'.")
    logging.info("--- Movie processing complete. ---")


# --- Flask API Endpoints ---

@app.route('/')
def index():
    """Render the main control panel page."""
    return render_template('index.html')

@app.route('/start_jdownloader', methods=['POST'])
def start_jdownloader():
    """Start the JDownloader watcher script as a background process."""
    global jdownloader_process
    if jdownloader_process and jdownloader_process.poll() is None:
        return jsonify(status="JDownloader watcher is already running.")
    
    # This command re-runs the script, which will initialize its own logging and config.
    jdownloader_process = subprocess.Popen(['python3', '-c', 'from app import jdownloader_automation_logic; jdownloader_automation_logic()'])
    logging.info(f"JDownloader watcher started with PID: {jdownloader_process.pid}")
    return jsonify(status=f"JDownloader watcher started with PID: {jdownloader_process.pid}")

@app.route('/stop_jdownloader', methods=['POST'])
def stop_jdownloader():
    """Stop the JDownloader watcher script."""
    global jdownloader_process
    if jdownloader_process and jdownloader_process.poll() is None:
        pid = jdownloader_process.pid
        jdownloader_process.terminate()
        jdownloader_process = None
        logging.info(f"JDownloader watcher (PID: {pid}) stopped.")
        return jsonify(status="JDownloader watcher stopped.")
    return jsonify(status="JDownloader watcher is not running.")

@app.route('/process_movies', methods=['POST'])
def process_movies():
    """Run the movie organizer script in a background thread to avoid blocking."""
    global is_movie_processor_running
    if is_movie_processor_running:
        return jsonify(status="Movie processing is already in progress."), 409

    def task_wrapper():
        global is_movie_processor_running
        is_movie_processor_running = True
        try:
            movie_organizer_logic()
        except Exception as e:
            logging.error(f"A critical error occurred in the movie organizer: {e}")
        finally:
            is_movie_processor_running = False

    thread = Thread(target=task_wrapper)
    thread.start()
    return jsonify(status="Movie processing started. Check the logs for progress.")

@app.route('/status', methods=['GET'])
def status():
    """Check the status of the background services."""
    global jdownloader_process, is_movie_processor_running
    jd_status = "Running" if jdownloader_process and jdownloader_process.poll() is None else "Stopped"
    movie_status = "Running" if is_movie_processor_running else "Idle"
    return jsonify(jdownloader_status=jd_status, movie_organizer_status=movie_status)

@app.route('/stream_logs')
def stream_logs():
    """Streams the last 100 lines of the log file."""
    try:
        with open("app.log", "r") as f:
            lines = f.readlines()
            log_content = "".join(lines[-100:])
        return jsonify(log=log_content)
    except FileNotFoundError:
        return jsonify(log="Log file not created yet.")


# --- Main entry point ---
if __name__ == '__main__':
    logging.info("Flask application starting up.")
    app.run(host='0.0.0.0', port=5000, debug=False) # Debug mode should be off for this setup