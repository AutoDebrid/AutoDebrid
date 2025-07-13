import os
import re
import shutil
import requests
import json

# --- Configuration ---
# The folder where your movie folders are currently located
SOURCE_FOLDER = 'SOURCE_FOLDER_PATH'

# --- Path Mapping Configuration ---
# 1. The REAL path where this script will physically move the files
LOCAL_MOVE_PATH = 'LOCAL_MOVE_PATH'

# 2. The corresponding path Radarr uses to access the above location
#    This is what will be sent to the Radarr API.
RADARR_ROOT_PATH = 'RADARR_ROOT_PATH'  #/movies

# --- Radarr Configuration ---
RADARR_URL = 'http://RADARR_URL:7878'  # ⚠️ Replace with your Radarr URL
RADARR_API_KEY = 'RADARR_API_KEY'      # ⚠️ Replace with your Radarr API Key

# --- End of Configuration ---

def get_info_from_folder_name(folder_name):
    """
    Extracts a clean movie title and year from a folder/file name.
    """
    base_name = os.path.splitext(folder_name)[0]
    year_match = re.search(r'(\d{4})', base_name)

    if year_match:
        year = year_match.group(1)
        title = base_name.split(year)[0]
    else:
        year = None
        title = base_name

    title = re.sub(r'[._]', ' ', title).strip()
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    title = title.strip('-_')

    return {'title': title, 'year': year}

def lookup_movie_in_radarr(title, year):
    """Searches for a movie using Radarr's API."""
    headers = {'X-Api-Key': RADARR_API_KEY}
    search_term = f"{title} ({year})" if year else title
    
    print(f"🎬 Searching Radarr with term: '{search_term}'...")
    try:
        response = requests.get(f"{RADARR_URL}/api/v3/movie/lookup", params={'term': search_term}, headers=headers)
        response.raise_for_status()
        return response.json()[0] if response.json() else None
    except requests.exceptions.RequestException as e:
        print(f"❌ Error looking up movie in Radarr: {e}")
        return None

def get_radarr_quality_profile_id():
    """Gets the ID of the first quality profile from Radarr."""
    headers = {'X-Api-Key': RADARR_API_KEY}
    try:
        response = requests.get(f"{RADARR_URL}/api/v3/qualityprofile", headers=headers)
        response.raise_for_status()
        return response.json()[0]['id']
    except (requests.exceptions.RequestException, IndexError, KeyError) as e:
        print(f"❌ Error getting Radarr quality profile: {e}")
        return None

def add_movie_to_radarr(movie_info, radarr_movie_path, quality_profile_id):
    """Adds a movie to Radarr using the RADARR_ROOT_PATH."""
    print(f"📡 Telling Radarr to add movie at path: '{radarr_movie_path}'")
    headers = {'X-Api-Key': RADARR_API_KEY}

    add_movie_payload = {
        'title': movie_info['title'],
        'year': movie_info['year'],
        'qualityProfileId': quality_profile_id,
        'tmdbId': movie_info['tmdbId'],
        'titleSlug': movie_info['titleSlug'],
        'images': movie_info['images'],
        'path': radarr_movie_path,  # Use the Radarr-specific path
        'monitored': True,
        'addOptions': {'searchForMovie': False}
    }

    try:
        response = requests.post(f"{RADARR_URL}/api/v3/movie", json=add_movie_payload, headers=headers)
        response.raise_for_status()
        print(f"✅ Successfully added '{movie_info['title']}' to Radarr.")
    except requests.exceptions.RequestException as e:
        error_text = str(e.response.json()) if e.response.content else str(e)
        if "already been added" in error_text.lower():
            print(f"ℹ️ Movie '{movie_info['title']}' is already in Radarr.")
        else:
            print(f"❌ Error adding movie to Radarr: {error_text}")

def rename_and_move_folder(original_path, movie_info):
    """
    Renames and moves a folder to the LOCAL_MOVE_PATH.
    Returns the clean 'Movie Title (Year)' folder name on success.
    """
    movie_title = movie_info['title']
    release_year = movie_info['year']
    
    new_folder_name = f"{movie_title} ({release_year})"
    sanitized_folder_name = re.sub(r'[<>:"/\\|?*]', '', new_folder_name)
    
    # This is the real, physical destination for the file move
    local_destination = os.path.join(LOCAL_MOVE_PATH, sanitized_folder_name)

    try:
        print(f"🚚 Moving file to local path: '{local_destination}'")
        os.makedirs(os.path.dirname(local_destination), exist_ok=True)
        shutil.move(original_path, local_destination)
        return sanitized_folder_name
    except Exception as e:
        print(f"❌ Error moving folder '{os.path.basename(original_path)}': {e}")
        return None

def main():
    """Main function to organize movie folders and add to Radarr."""
    if not os.path.isdir(LOCAL_MOVE_PATH):
        try:
            os.makedirs(LOCAL_MOVE_PATH)
        except OSError as e:
            print(f"❌ Could not create local move path '{LOCAL_MOVE_PATH}': {e}.")
            return

    quality_profile_id = get_radarr_quality_profile_id()
    if not quality_profile_id:
        print("❌ Could not retrieve Radarr Quality Profile. Exiting.")
        return

    for item_name in os.listdir(SOURCE_FOLDER):
        original_path = os.path.join(SOURCE_FOLDER, item_name)
        
        # Process both loose files and folders
        if os.path.isdir(original_path) or item_name.endswith(('.mkv', '.mp4', '.avi')):
            print(f"\nProcessing: '{item_name}'")
            
            parsed_info = get_info_from_folder_name(item_name)
            if not parsed_info['title']:
                print("❗️ Could not extract a valid title. Skipping.")
                continue
            
            print(f"🧹 Cleaned Info -> Title: '{parsed_info['title']}', Year: '{parsed_info['year']}'")

            movie_info = lookup_movie_in_radarr(parsed_info['title'], parsed_info['year'])
            if not movie_info:
                print(f"👎 Could not find a match in Radarr for '{parsed_info['title']}'.")
                continue

            print(f"👍 Found match: {movie_info['title']} ({movie_info['year']})")
            
            # 1. Move the folder locally and get its new name
            newly_created_folder = rename_and_move_folder(original_path, movie_info)
            
            if newly_created_folder:
                # 2. Create the path that Radarr will use
                radarr_path_for_movie = os.path.join(RADARR_ROOT_PATH, newly_created_folder)
                
                # 3. Add to Radarr using the Radarr-specific path
                add_movie_to_radarr(movie_info, radarr_path_for_movie, quality_profile_id)

if __name__ == "__main__":
    main()
