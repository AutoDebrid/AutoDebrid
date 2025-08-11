# AutoDebrid Automation Dashboard

A web-based dashboard to automate your media workflow, bridging the gap between Real-Debrid, JDownloader, Radarr, and Sonarr. This application monitors your services, processes completed downloads, and organizes your media library automatically.

![Dashboard Screenshot](https://postimg.cc/yDqBBC2X) <!-- Replace with a URL to a screenshot of your dashboard -->

---

## Features

-   **Web-Based Dashboard:** A clean, dark-themed interface to monitor and control all services from any device on your network.
-   **JDownloader Automation:** Automatically checks your Real-Debrid account for completed torrents and sends the unrestricted links to JDownloader via `.crawljob` files.
-   **Automated Movie & TV Show Sorting:**
    -   Continuously scans your download folder for completed media.
    -   Intelligently distinguishes between movies and TV shows (including season packs).
    -   Moves completed downloads to a staging folder for processing.
-   **Seamless Radarr & Sonarr Integration:**
    -   Looks up movies and TV shows to get correct naming and metadata.
    -   Renames and organizes files into the correct season/movie folders.
    -   Moves the organized media into your final library folders.
    -   Triggers Radarr and Sonarr to scan and import the new media automatically.
-   **Background Services:** All organizers run as persistent background processes that can be started and stopped from the UI.
-   **Push Notifications:** Optional Pushover integration to notify you when services start/stop or when tasks are complete.

---

## Requirements

-   Python 3.x
-   Radarr, Sonarr, and JDownloader instances accessible on your network.
-   A Real-Debrid account with an active subscription.
-   (Optional) A Pushover account for notifications.

---

## Setup

1.  **Clone the Repository:**
    ```bash
    git clone [https://github.com/AutoDebrid/AutoDebrid.git](https://github.com/AutoDebrid/AutoDebrid.git)
    cd AutoDebrid
    ```

2.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
    *(Note: You will need to create a `requirements.txt` file containing `Flask`, `requests`, `python-dotenv`, `Flask-WTF`)*

3.  **Initial Run:**
    -   Run the application for the first time:
        ```bash
        python3 app.py
        ```
    -   The application will log a warning about missing environment variables. This is expected.

4.  **First-Time Configuration:**
    -   Open your web browser and navigate to `http://<your-server-ip>:5000`.
    -   You will be prompted for an "Internal API Key." **Enter any temporary text** (e.g., "setup") and press OK.
    -   Click on the **Settings** tab.
    -   Fill out **every field** with your API keys, URLs, and folder paths. Create a long, random string for the `INTERNAL_API_KEY` - this will be your permanent password for the dashboard.
    -   Click **Save Settings**.
    -   **Stop the Python script** in your terminal (`Ctrl+C`).

5.  **Start the Application:**
    -   Restart the application:
        ```bash
        python3 app.py
        ```
    -   The application will now load your saved settings from the `.env` file and be fully functional.

---

## Usage

1.  **Access the Dashboard:** Navigate to `http://<your-server-ip>:5000`.
2.  **Authenticate:** When prompted, enter the permanent **Internal API Key** you created in the settings.
3.  **Start Services:** On the Dashboard, click the "Start" button for each service (JDownloader Watcher, Movie Organizer, TV Show Organizer) to begin the automation.
4.  **Monitor:** Use the dashboard to view the status of each service and monitor the application logs in real-time.

---

## Troubleshooting

### Permission Denied / `[Errno 13]`

This is the most common issue, especially on systems like Unraid or when using Docker. It means the user running the `app.py` script does not have permission to write to your media folders.

1.  **Identify the User:** Start the TV Show or Movie Organizer and check the application logs. The script will log a line like: `INFO - Organizer is running as User ID: 1000 and Group ID: 1000`.
2.  **Set Permissions:** Connect to your server via SSH and use the `chown` command to give that user ownership of your media folders. For example, if the user ID was `99` (nobody) and group was `100` (users):
    ```bash
    sudo chown -R 99:100 /path/to/your/media/
    sudo chmod -R 775 /path/to/your/media/
    ```
    Sonarr Permissions
    (https://postimg.cc/Hjh3rYY5)
    Radarr Permissions
    (https://postimg.cc/RJHt6zdQ)

### Radarr/Sonarr Errors

-   **400 Bad Request:** This almost always means the "Root Folder Path" in the application's settings does not **exactly** match a root folder path configured inside Radarr or Sonarr's own settings.
-   **401 Unauthorized:** Your Radarr/Sonarr API key is incorrect.
-   **Connection Errors:** Your Radarr/Sonarr URL is incorrect or the service is not running.

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

---

## License

[MIT](https://choosealicense.com/licenses/mit/)
