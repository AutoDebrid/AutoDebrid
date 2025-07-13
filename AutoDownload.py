import requests
import os
import json
import time

# --- Configuration ---
REAL_DEBRID_API_KEY = "REAL_DEBRID_API_KEY"  # Replace with your API key
# Important: Use a Linux-style path. Replace 'your_username' with your actual username.
JDOWNLOADER_WATCH_FOLDER = "JDOWNLOADER_WATCH_FOLDER_PATH"
CHECK_INTERVAL_SECONDS = 300  # Check for new files every 5 minutes

# --- Do not edit below this line ---
PROCESSED_TORRENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_torrents.json")

def get_processed_torrents():
    if os.path.exists(PROCESSED_TORRENTS_FILE):
        with open(PROCESSED_TORRENTS_FILE, "r") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()

def save_processed_torrents(processed_ids):
    with open(PROCESSED_TORRENTS_FILE, "w") as f:
        json.dump(list(processed_ids), f)

def get_unrestricted_link(link):
    headers = {"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"}
    data = {"link": link}
    response = requests.post("https://api.real-debrid.com/rest/1.0/unrestrict/link", headers=headers, data=data)
    if response.status_code == 200:
        return response.json()["download"]
    return None

def create_crawljob_file(torrent_name, download_links):
    # Sanitize the filename to prevent issues
    safe_torrent_name = "".join(c for c in torrent_name if c.isalnum() or c in (' ', '.', '_')).rstrip()
    file_path = os.path.join(JDOWNLOADER_WATCH_FOLDER, f"{safe_torrent_name}.crawljob")
    
    # --- THIS FUNCTION IS MODIFIED ---
    # Manually build the key=value string instead of using JSON
    
    # JDownloader expects boolean values as uppercase "TRUE" or "FALSE" in this format
    autostart_str = "TRUE"
    forcedstart_str = "TRUE"
    
    # Join all download links with a newline character
    links_str = "\n".join(download_links)
    
    # Build the final content string
    content = (
        f"text={links_str}\n"
        f"packageName={safe_torrent_name}\n"
        f"autoStart={autostart_str}\n"
        f"forcedStart={forcedstart_str}\n"
    )
    
    with open(file_path, "w", encoding='utf-8') as f:
        f.write(content)
        
    print(f"Created .crawljob file for: {safe_torrent_name}")

def check_real_debrid():
    processed_torrents = get_processed_torrents()
    headers = {"Authorization": f"Bearer {REAL_DEBRID_API_KEY}"}
    try:
        response = requests.get("https://api.real-debrid.com/rest/1.0/torrents", headers=headers)
        if response.status_code == 200:
            torrents = response.json()
            for torrent in torrents:
                if torrent["id"] not in processed_torrents and torrent["status"] == "downloaded":
                    print(f"Found new completed torrent: {torrent['filename']}")
                    download_links = []
                    # The 'links' array is populated when the torrent is ready
                    if torrent.get("links"):
                        unrestricted_links = [get_unrestricted_link(link) for link in torrent["links"]]
                        download_links.extend(filter(None, unrestricted_links))

                    if download_links:
                        create_crawljob_file(torrent["filename"], download_links)
                        processed_torrents.add(torrent["id"])
            save_processed_torrents(processed_torrents)
        else:
            print(f"Error checking Real-Debrid: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    print("Starting Real-Debrid to JDownloader automation script...")
    while True:
        check_real_debrid()
        print(f"Waiting for {CHECK_INTERVAL_SECONDS} seconds before the next check...")
        time.sleep(CHECK_INTERVAL_SECONDS)
