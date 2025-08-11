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
SONARR_API_KEY = os.environ.get('SONARR_API_KEY')
PUSHOVER_USER_KEY = os.environ.get('PUSHOVER_USER_KEY')
PUSHOVER_API_TOKEN = os.environ.get('PUSHOVER_API_TOKEN')

# Paths & Settings
JDOWNLOADER_WATCH_FOLDER = os.environ.get('JDOWNLOADER_WATCH_FOLDER')
SOURCE_FOLDER = os.environ.get('SOURCE_FOLDER')
COMPLETED_FOLDER = os.environ.get('COMPLETED_FOLDER')
LOCAL_MOVE_PATH = os.environ.get('LOCAL_MOVE_PATH')
FINAL_TV_SHOW_FOLDER = os.environ.get('FINAL_TV_SHOW_FOLDER')
RADARR_ROOT_PATH = os.environ.get('RADARR_ROOT_PATH')
SONARR_ROOT_PATH = os.environ.get('SONARR_ROOT') # Note: form name is SONARR_ROOT
RADARR_URL = os.environ.get('RADARR_URL', '').rstrip('/')
SONARR_URL = os.environ.get('SONARR_URL', '').rstrip('/')
# If the value from .env is an empty string, fall back to '60'.
CHECK_INTERVAL_SECONDS = int(os.environ.get('CHECK_INTERVAL_SECONDS') or 60)

# --- Startup Sanity Checks (Modified to not exit) ---
IS_FULLY_CONFIGURED = True
# Add new Sonarr vars to the required list
REQUIRED_VARS = [
    'FLASK_SECRET_KEY', 'INTERNAL_API_KEY', 'REAL_DEBRID_API_KEY', 
    'RADARR_API_KEY', 'SONARR_API_KEY',
    'JDOWNLOADER_WATCH_FOLDER', 'SOURCE_FOLDER', 'COMPLETED_FOLDER', 
    'LOCAL_MOVE_PATH', 'FINAL_TV_SHOW_FOLDER',
    'RADARR_ROOT_PATH', 'SONARR_ROOT_PATH', 
    'RADARR_URL', 'SONARR_URL'
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
app.config['SECRET_KEY'] = FLASK_SECRET_KEY or 'temporary-insecure-key-for-initial-setup'
if not FLASK_SECRET_KEY:
    logging.warning("WARNING: Using a temporary, insecure Flask secret key. Please set one on the Settings page.")

csrf = CSRFProtect(app)


# --- Security Decorator (Modified for Initial Setup) ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided_key = request.headers.get('X-Api-Key')

        if not INTERNAL_API_KEY:
            if request.endpoint in ['get_settings', 'save_settings']:
                if provided_key:
                    return f(*args, **kwargs)
                else:
                    abort(401)
            else:
                abort(403) 

        elif provided_key != INTERNAL_API_KEY:
            logging.warning(f"Unauthorized API access attempt from IP: {request.remote_addr}")
            abort(401) 

        return f(*args, **kwargs)
    return decorated_function


# --- Global State Variables ---
jdownloader_process = None
movie_organizer_process = None
tv_organizer_process = None # New process for TV shows
JD_STATUS_FILE = "jd_status.json"
MO_STATUS_FILE = "mo_status.json"
TV_STATUS_FILE = "tv_status.json" # New status file for TV shows


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
        if not os.path.exists(processed_torrents_file): return set()
        try:
            with open(processed_torrents_file, "r") as f: return set(json.load(f))
        except (json.JSONDecodeError, IOError): return set()

    def save_processed_torrents(processed_ids):
        with open(processed_torrents_file, "w") as f: json.dump(list(processed_ids), f)

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
        content = (f"text={links_text}\npackageName={safe_base_name}\nautoStart=TRUE\nforcedStart=TRUE\n")
        try:
            with open(file_path, "w", encoding='utf-8') as f: f.write(content)
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
            with open(JD_STATUS_FILE, "w") as f: json.dump({"last_check": datetime.utcnow().isoformat()}, f)
        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking Real-Debrid: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred in the JDownloader loop: {e}", exc_info=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


# --- Media Organizer Utilities ---
def is_directory_stable(dir_path):
    def get_dir_size(path):
        total = 0
        try:
            for entry in os.scandir(path):
                if entry.is_file(): total += entry.stat().st_size
                elif entry.is_dir(): total += get_dir_size(entry.path)
        except (FileNotFoundError, PermissionError): return 0
        return total
    try:
        logging.info(f"Watching directory '{os.path.basename(dir_path)}' for download completion...")
        initial_size = get_dir_size(dir_path)
        if initial_size == 0:
            logging.info(f"'{os.path.basename(dir_path)}' is empty. Skipping for now.")
            return False
        time.sleep(60)
        final_size = get_dir_size(dir_path)
        if initial_size == final_size:
            logging.info(f"Download of '{os.path.basename(dir_path)}' appears complete. Size: {final_size} bytes.")
            return True
        else:
            logging.info(f"Directory '{os.path.basename(dir_path)}' is still growing. Skipping for now.")
            return False
    except Exception as e:
        logging.error(f"Error during stability check for '{dir_path}': {e}")
        return False

def get_info_from_movie_name(name):
    clean_name, _ = os.path.splitext(name)
    year = None
    year_match = re.search(r'[.( \[](\d{4})[.) \]]', clean_name)
    if year_match:
        year = int(year_match.group(1))
        clean_name = clean_name[:year_match.start()]
    clean_name = clean_name.replace('.', ' ').replace('_', ' ').strip()
    tags_pattern = r'\b(1080p|720p|4k|2160p|bluray|dvdrip|webrip|web-dl|x264|x265|h264|h265|aac|dts|ac3|ddp|5 1|remux|repack|proper|internal|judas|edge2020)\b'
    title = re.sub(tags_pattern, '', clean_name, flags=re.IGNORECASE)
    title = re.sub(r'[\[\]()]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    if not year:
        final_match = re.match(r'^(.*?) (\d{4})$', title)
        if final_match:
            title = final_match.group(1).strip()
            year = int(final_match.group(2))
    return title, year

def get_info_from_tv_show_name(filename):
    episode_pattern = re.compile(r'S(\d{1,2})E(\d{1,2})|(\d{1,2})x(\d{1,2})', re.IGNORECASE)
    match = episode_pattern.search(filename)
    if not match:
        return None, None, None

    if match.group(1) is not None and match.group(2) is not None: # S01E01 format
        season = int(match.group(1))
        episode = int(match.group(2))
    elif match.group(3) is not None and match.group(4) is not None: # 1x01 format
        season = int(match.group(3))
        episode = int(match.group(4))
    else:
        return None, None, None

    show_title = filename[:match.start()].replace('.', ' ').replace('_', ' ').strip()
    show_title = re.sub(r'\b(\d{4})\b', '', show_title).strip()
    tags_pattern = r'\b(1080p|720p|4k|2160p|bluray|dvdrip|webrip|web-dl|x264|x265|h264|h265|aac|dts|ac3|ddp|5 1|remux|repack|proper|internal|fluxeztvx|to)\b'
    show_title = re.sub(tags_pattern, '', show_title, flags=re.IGNORECASE)
    show_title = re.sub(r'\s+', ' ', show_title).strip()
    
    return show_title, season, episode

def is_tv_show_pack(item_name, item_path):
    """Checks if a folder name or its contents match a TV show pattern."""
    tv_pattern = re.compile(r'\b(S\d{1,2}E\d{1,2}|S\d{1,2}|Season \d{1,2}|(\d{1,2})x(\d{1,2}))\b', re.IGNORECASE)
    
    if tv_pattern.search(item_name):
        return True
    
    if os.path.isdir(item_path):
        for _, _, files in os.walk(item_path):
            for file in files:
                if tv_pattern.search(file):
                    return True
    return False

def set_permissions_recursive(path):
    """Recursively sets permissions to 777 for a path and its contents."""
    try:
        logging.info(f"Setting permissions to 777 for: {path}")
        os.chmod(path, 0o777)
        for root, dirs, files in os.walk(path):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o777)
            for f in files:
                os.chmod(os.path.join(root, f), 0o777)
    except Exception as e:
        logging.error(f"Failed to set permissions for '{path}': {e}")


# --- Movie Organizer Logic ---
def movie_organizer_automation_loop():
    logging.info("Starting Movie Organizer automation loop...")
    while True:
        try:
            if not os.path.isdir(COMPLETED_FOLDER): os.makedirs(COMPLETED_FOLDER, exist_ok=True)
            
            completed_folder_name = os.path.basename(COMPLETED_FOLDER)
            for item_name in os.listdir(SOURCE_FOLDER):
                if item_name == completed_folder_name:
                    continue
                
                source_path = os.path.join(SOURCE_FOLDER, item_name)
                
                if is_tv_show_pack(item_name, source_path):
                    continue

                if os.path.isdir(source_path):
                    if is_directory_stable(source_path):
                        try:
                            destination = os.path.join(COMPLETED_FOLDER, item_name)
                            shutil.move(source_path, destination)
                            logging.info(f"Moved stable movie download '{item_name}' to completed folder.")
                        except Exception as e:
                            logging.error(f"Failed to move movie '{item_name}' to completed folder: {e}")
            
            process_completed_movies()
            with open(MO_STATUS_FILE, "w") as f: json.dump({"last_scan": datetime.utcnow().isoformat()}, f)
        except Exception as e:
            logging.error(f"An unexpected error occurred in the Movie Organizer loop: {e}", exc_info=True)
        
        logging.info(f"Movie Organizer loop finished. Waiting {CHECK_INTERVAL_SECONDS} seconds.")
        time.sleep(CHECK_INTERVAL_SECONDS)

def process_completed_movies():
    processed_count, skipped_count = 0, 0
    error_message = None
    
    def radarr_api_request(method, endpoint, json_data=None, params=None):
        url = f"{RADARR_URL}/api/v3/{endpoint}"
        headers = {"X-Api-Key": RADARR_API_KEY}
        try:
            if method.upper() == 'GET': response = requests.get(url, headers=headers, timeout=20, params=params)
            elif method.upper() == 'POST': response = requests.post(url, headers=headers, json=json_data, timeout=20)
            else: return None
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Radarr API {method} Error for endpoint '{endpoint}': {e}")
            if e.response is not None: logging.error(f"Radarr Response Body: {e.response.text}")
            return None

    radarr_root_folders = radarr_api_request('GET', 'rootfolder')
    if radarr_root_folders is None:
        error_message = "Could not communicate with Radarr to get root folders."
        logging.critical(error_message)
        return {"processed": 0, "skipped": 0, "error": error_message}

    valid_radarr_paths = [rf['path'] for rf in radarr_root_folders]
    if RADARR_ROOT_PATH not in valid_radarr_paths:
        error_message = f"Configuration Mismatch! Radarr Root Folder '{RADARR_ROOT_PATH}' is not valid. Valid paths: {valid_radarr_paths}."
        logging.critical(error_message)
        return {"processed": 0, "skipped": 0, "error": error_message}
    
    profiles = radarr_api_request('GET', "qualityprofile")
    if not profiles:
        logging.error("Could not fetch quality profiles from Radarr.")
        return {"processed": 0, "skipped": 0, "error": "Could not fetch Radarr quality profiles."}
    
    quality_profile_id = next((p['id'] for p in profiles if '1080p' in p['name']), profiles[0]['id'])

    logging.info(f"--- Processing movies from completed folder '{COMPLETED_FOLDER}' ---")
    for item_name in os.listdir(COMPLETED_FOLDER):
        source_path = os.path.join(COMPLETED_FOLDER, item_name)
        if not os.path.isdir(source_path) or is_tv_show_pack(item_name, source_path):
            continue
        
        title, year = get_info_from_movie_name(item_name)
        if not title:
            logging.warning(f"Could not extract a valid title from '{item_name}'. Skipping.")
            skipped_count += 1
            continue

        search_term = f"{title} ({year})" if year else title
        lookup_results = radarr_api_request('GET', f"movie/lookup", params={'term': search_term})
        if not lookup_results:
            logging.warning(f"Could not find '{search_term}' in Radarr's lookup. Skipping.")
            skipped_count += 1
            continue
        
        movie_info_from_lookup = lookup_results[0]
        tmdb_id = movie_info_from_lookup.get('tmdbId')
        existing_movies_in_library = radarr_api_request('GET', f"movie", params={'tmdbId': tmdb_id})
        
        radarr_movie_object = None
        if existing_movies_in_library:
            radarr_movie_object = existing_movies_in_library[0]
            if radarr_movie_object.get('hasFile', False):
                logging.warning(f"Movie already has a file in Radarr. Deleting redundant source: '{source_path}'")
                try:
                    shutil.rmtree(source_path)
                    skipped_count += 1
                    continue
                except Exception as e:
                    logging.error(f"Failed to delete redundant source folder '{source_path}': {e}")
                    skipped_count += 1
                    continue
        else:
            add_options = {"searchForMovie": False}
            movie_data_to_add = {
                "title": movie_info_from_lookup.get('title'), "year": movie_info_from_lookup.get('year'),
                "qualityProfileId": quality_profile_id, "titleSlug": movie_info_from_lookup.get('titleSlug'),
                "images": movie_info_from_lookup.get('images', []), "tmdbId": movie_info_from_lookup.get('tmdbId'),
                "rootFolderPath": RADARR_ROOT_PATH, "monitored": True, "addOptions": add_options
            }
            radarr_movie_object = radarr_api_request('POST', "movie", json_data=movie_data_to_add)
            if not (radarr_movie_object and 'id' in radarr_movie_object):
                logging.error(f"Failed to add movie '{movie_info_from_lookup.get('title')}' to Radarr.")
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
                shutil.rmtree(source_path)
                skipped_count += 1
            else:
                set_permissions_recursive(source_path)
                shutil.move(source_path, destination_path)
                processed_count += 1
        except Exception as e:
            logging.error(f"Failed to move '{item_name}': {e}")
            skipped_count += 1
    
    if processed_count > 0:
        radarr_api_request('POST', "command", json_data={"name": "RescanFolders", "folders": [LOCAL_MOVE_PATH]})
    
    return {"processed": processed_count, "skipped": skipped_count, "error": error_message}


# --- TV Show Organizer Logic ---
def tv_show_organizer_automation_loop():
    logging.info(f"TV Show Organizer is running as User ID: {os.getuid()} and Group ID: {os.getgid()}")
    logging.info("Starting TV Show Organizer automation loop...")

    while True:
        try:
            if not os.path.isdir(COMPLETED_FOLDER): os.makedirs(COMPLETED_FOLDER, exist_ok=True)
            
            completed_folder_name = os.path.basename(COMPLETED_FOLDER)
            for item_name in os.listdir(SOURCE_FOLDER):
                if item_name == completed_folder_name:
                    continue
                
                source_path = os.path.join(SOURCE_FOLDER, item_name)
                
                if not is_tv_show_pack(item_name, source_path):
                    continue

                if os.path.isdir(source_path):
                    if is_directory_stable(source_path):
                        try:
                            destination = os.path.join(COMPLETED_FOLDER, item_name)
                            shutil.move(source_path, destination)
                            logging.info(f"Moved stable TV download '{item_name}' to completed folder.")
                        except Exception as e:
                            logging.error(f"Failed to move TV show '{item_name}' to completed folder: {e}")
            
            process_completed_tv_shows()
            with open(TV_STATUS_FILE, "w") as f: json.dump({"last_scan": datetime.utcnow().isoformat()}, f)
        except Exception as e:
            logging.error(f"An unexpected error occurred in the TV Show Organizer loop: {e}", exc_info=True)
        
        logging.info(f"TV Show Organizer loop finished. Waiting {CHECK_INTERVAL_SECONDS} seconds.")
        time.sleep(CHECK_INTERVAL_SECONDS)

def process_completed_tv_shows():
    processed_count, skipped_count = 0, 0
    
    def sonarr_api_request(method, endpoint, json_data=None, params=None):
        url = f"{SONARR_URL}/api/v3/{endpoint}"
        headers = {"X-Api-Key": SONARR_API_KEY}
        try:
            if method.upper() == 'GET': response = requests.get(url, headers=headers, timeout=20, params=params)
            elif method.upper() == 'POST': response = requests.post(url, headers=headers, json=json_data, timeout=20)
            else: return None
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Sonarr API {method} Error for endpoint '{endpoint}': {e}")
            if e.response is not None: logging.error(f"Sonarr Response Body: {e.response.text}")
            return None

    logging.info(f"--- Processing TV shows from completed folder '{COMPLETED_FOLDER}' ---")
    for item_name in os.listdir(COMPLETED_FOLDER):
        source_pack_path = os.path.join(COMPLETED_FOLDER, item_name)
        if not os.path.isdir(source_pack_path) or not is_tv_show_pack(item_name, source_pack_path):
            continue

        logging.info(f"Processing TV pack: {item_name}")
        
        sample_file = next((f for f in os.listdir(source_pack_path) if os.path.isfile(os.path.join(source_pack_path, f))), None)
        if not sample_file:
            logging.warning(f"'{item_name}' contains no files. Skipping.")
            continue

        title, _, _ = get_info_from_tv_show_name(sample_file)
        if not title:
            logging.warning(f"Could not parse show title from sample file '{sample_file}'. Skipping pack.")
            continue

        lookup_results = sonarr_api_request('GET', 'series/lookup', params={'term': title})
        if not lookup_results:
            logging.warning(f"Could not find '{title}' in Sonarr's lookup. Skipping pack.")
            continue
        
        series_info = lookup_results[0]
        clean_series_folder_name = series_info['folder']
        
        clean_pack_path = os.path.join(COMPLETED_FOLDER, clean_series_folder_name)
        try:
            os.rename(source_pack_path, clean_pack_path)
            logging.info(f"Renamed '{item_name}' to '{clean_series_folder_name}' inside completed folder.")
        except OSError as e:
            logging.error(f"Failed to rename '{item_name}': {e}. Skipping pack.")
            continue

        episodes_by_season = {}
        for filename in os.listdir(clean_pack_path):
            file_path = os.path.join(clean_pack_path, filename)
            if os.path.isfile(file_path):
                _, season, _ = get_info_from_tv_show_name(filename)
                if season is not None:
                    if season not in episodes_by_season:
                        episodes_by_season[season] = []
                    episodes_by_season[season].append(filename)

        for season, files in episodes_by_season.items():
            season_folder_path = os.path.join(clean_pack_path, f"Season {season:02d}")
            os.makedirs(season_folder_path, exist_ok=True)
            for filename in files:
                shutil.move(os.path.join(clean_pack_path, filename), os.path.join(season_folder_path, filename))
            logging.info(f"Organized {len(files)} episodes into '{season_folder_path}'")
        
        final_destination = os.path.join(FINAL_TV_SHOW_FOLDER, clean_series_folder_name)
        try:
            set_permissions_recursive(clean_pack_path)
            shutil.move(clean_pack_path, final_destination)
            logging.info(f"Successfully moved '{clean_series_folder_name}' to final TV library.")
            processed_count += 1
        except Exception as e:
            logging.error(f"Failed to move final folder '{clean_series_folder_name}': {e}")
    
    if processed_count > 0:
        logging.info(f"Triggering Sonarr 'RescanSeries' command...")
        sonarr_api_request('POST', "command", json_data={"name": "RescanSeries"})


# --- Flask API Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/get-settings', methods=['GET'])
@require_api_key
def get_settings():
    settings = {}
    keys_to_fetch = [
        'FLASK_SECRET_KEY', 'INTERNAL_API_KEY', 'REAL_DEBRID_API_KEY', 
        'RADARR_API_KEY', 'SONARR_API_KEY', 'PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN',
        'JDOWNLOADER_WATCH_FOLDER', 'SOURCE_FOLDER', 'COMPLETED_FOLDER', 
        'LOCAL_MOVE_PATH', 'FINAL_TV_SHOW_FOLDER', 'RADARR_ROOT', 'SONARR_ROOT', 
        'RADARR_URL', 'SONARR_URL', 'CHECK_INTERVAL_SECONDS'
    ]
    for key in keys_to_fetch:
        if key == 'RADARR_ROOT': settings[key] = os.environ.get('RADARR_ROOT_PATH', '')
        elif key == 'SONARR_ROOT': settings[key] = os.environ.get('SONARR_ROOT_PATH', '')
        else: settings[key] = os.environ.get(key, '')
    return jsonify(settings)

@app.route('/api/save-settings', methods=['POST'])
@require_api_key
def save_settings():
    data = request.get_json()
    if not data: return jsonify(status="Error: No data received."), 400
    try:
        dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
        if not os.path.exists(dotenv_path):
            open(dotenv_path, 'a').close()
            logging.info("Created .env file as it did not exist.")
        for key, value in data.items():
            env_key = key
            if key == 'RADARR_ROOT': env_key = 'RADARR_ROOT_PATH'
            if key == 'SONARR_ROOT': env_key = 'SONARR_ROOT_PATH'
            set_key(dotenv_path, env_key, str(value))
        logging.info(f"Settings successfully saved to .env file by user from IP: {request.remote_addr}")
        return jsonify(status="Settings saved. Please restart the application for changes to take effect.")
    except Exception as e:
        logging.error(f"Failed to save settings to .env file: {e}", exc_info=True)
        return jsonify(status=f"Error saving settings: {e}"), 500

@app.route('/logs')
def get_logs():
    try:
        with open("app.log", "r") as f: return jsonify(logs="".join(f.readlines()[-100:]))
    except FileNotFoundError: return jsonify(logs="Log file not found."), 404
    except Exception as e: return jsonify(logs=f"Error reading log file: {e}"), 500

@app.route('/start_jdownloader', methods=['POST'])
@require_api_key
def start_jdownloader():
    global jdownloader_process
    if not IS_FULLY_CONFIGURED: return jsonify(status=f"Cannot start: App not configured."), 503
    if jdownloader_process and jdownloader_process.poll() is None: return jsonify(status="JDownloader watcher is already running.")
    jdownloader_process = subprocess.Popen([sys.executable, 'app.py', 'jdownloader'])
    msg = f"JDownloader watcher started with PID: {jdownloader_process.pid}"
    logging.info(msg)
    send_notification("Service Started", "JDownloader Watcher is now running.")
    return jsonify(status=msg)

@app.route('/stop_jdownloader', methods=['POST'])
@require_api_key
def stop_jdownloader():
    global jdownloader_process
    if not jdownloader_process or jdownloader_process.poll() is not None: return jsonify(status="JDownloader watcher is not running.")
    pid = jdownloader_process.pid
    jdownloader_process.terminate()
    if os.path.exists(JD_STATUS_FILE): os.remove(JD_STATUS_FILE)
    jdownloader_process = None
    msg = f"JDownloader watcher (PID: {pid}) stopped."
    logging.info(msg)
    send_notification("Service Stopped", "JDownloader Watcher has been stopped.")
    return jsonify(status=msg)

@app.route('/start_movie_organizer', methods=['POST'])
@require_api_key
def start_movie_organizer():
    global movie_organizer_process
    if not IS_FULLY_CONFIGURED: return jsonify(status=f"Cannot start: App not configured."), 503
    if movie_organizer_process and movie_organizer_process.poll() is None: return jsonify(status="Movie organizer is already running.")
    movie_organizer_process = subprocess.Popen([sys.executable, 'app.py', 'movie_organizer'])
    msg = f"Movie organizer started with PID: {movie_organizer_process.pid}"
    logging.info(msg)
    send_notification("Service Started", "Movie Organizer is now running.")
    return jsonify(status=msg)

@app.route('/stop_movie_organizer', methods=['POST'])
@require_api_key
def stop_movie_organizer():
    global movie_organizer_process
    if not movie_organizer_process or movie_organizer_process.poll() is not None: return jsonify(status="Movie organizer is not running.")
    pid = movie_organizer_process.pid
    movie_organizer_process.terminate()
    if os.path.exists(MO_STATUS_FILE): os.remove(MO_STATUS_FILE)
    movie_organizer_process = None
    msg = f"Movie organizer (PID: {pid}) stopped."
    logging.info(msg)
    send_notification("Service Stopped", "Movie Organizer has been stopped.")
    return jsonify(status=msg)

@app.route('/start_tv_organizer', methods=['POST'])
@require_api_key
def start_tv_organizer():
    global tv_organizer_process
    if not IS_FULLY_CONFIGURED: return jsonify(status=f"Cannot start: App not configured."), 503
    if tv_organizer_process and tv_organizer_process.poll() is None: return jsonify(status="TV organizer is already running.")
    tv_organizer_process = subprocess.Popen([sys.executable, 'app.py', 'tv_organizer'])
    msg = f"TV organizer started with PID: {tv_organizer_process.pid}"
    logging.info(msg)
    send_notification("Service Started", "TV Organizer is now running.")
    return jsonify(status=msg)

@app.route('/stop_tv_organizer', methods=['POST'])
@require_api_key
def stop_tv_organizer():
    global tv_organizer_process
    if not tv_organizer_process or tv_organizer_process.poll() is not None: return jsonify(status="TV organizer is not running.")
    pid = tv_organizer_process.pid
    tv_organizer_process.terminate()
    if os.path.exists(TV_STATUS_FILE): os.remove(TV_STATUS_FILE)
    tv_organizer_process = None
    msg = f"TV organizer (PID: {pid}) stopped."
    logging.info(msg)
    send_notification("Service Stopped", "TV Organizer has been stopped.")
    return jsonify(status=msg)

@app.route('/status', methods=['GET'])
def status():
    jd_status = {'state': "Stopped"}
    if jdownloader_process and jdownloader_process.poll() is None:
        jd_status['state'] = "Running"
        jd_status['pid'] = jdownloader_process.pid
        if os.path.exists(JD_STATUS_FILE):
            try:
                with open(JD_STATUS_FILE, "r") as f: jd_status['last_check'] = json.load(f).get('last_check')
            except (IOError, json.JSONDecodeError): jd_status['last_check'] = "N/A"
    
    movie_status = {'state': "Stopped"}
    if movie_organizer_process and movie_organizer_process.poll() is None:
        movie_status['state'] = "Running"
        movie_status['pid'] = movie_organizer_process.pid
        if os.path.exists(MO_STATUS_FILE):
            try:
                with open(MO_STATUS_FILE, "r") as f: movie_status['last_scan'] = json.load(f).get('last_scan')
            except (IOError, json.JSONDecodeError): movie_status['last_scan'] = "N/A"

    tv_status = {'state': "Stopped"}
    if tv_organizer_process and tv_organizer_process.poll() is None:
        tv_status['state'] = "Running"
        tv_status['pid'] = tv_organizer_process.pid
        if os.path.exists(TV_STATUS_FILE):
            try:
                with open(TV_STATUS_FILE, "r") as f: tv_status['last_scan'] = json.load(f).get('last_scan')
            except (IOError, json.JSONDecodeError): tv_status['last_scan'] = "N/A"
    
    return jsonify(jdownloader=jd_status, movie_organizer=movie_status, tv_organizer=tv_status)

# --- Main Entry Point ---
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'jdownloader':
        if not IS_FULLY_CONFIGURED: sys.exit(1)
        jdownloader_automation_logic()
    elif len(sys.argv) > 1 and sys.argv[1] == 'movie_organizer':
        if not IS_FULLY_CONFIGURED: sys.exit(1)
        movie_organizer_automation_loop()
    elif len(sys.argv) > 1 and sys.argv[1] == 'tv_organizer':
        if not IS_FULLY_CONFIGURED: sys.exit(1)
        tv_show_organizer_automation_loop()
    else:
        if not IS_FULLY_CONFIGURED and JDOWNLOADER_WATCH_FOLDER and not os.path.isdir(JDOWNLOADER_WATCH_FOLDER):
            logging.error(f"JDownloader watch folder not found: {JDOWNLOADER_WATCH_FOLDER}")
        
        logging.info("Flask application starting up.")
        if 'werkzeug' not in os.environ.get('SERVER_SOFTWARE', ''):
             logging.warning("Running in DEVELOPMENT mode. Do NOT use this mode in production. Use a WSGI server like Gunicorn.")
        app.run(host='0.0.0.0', port=5000, debug=False)
