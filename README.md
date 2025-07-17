# AutoDebrid 🎬

AutoDebrid is a slick automation tool that connects your Real-Debrid cache with JDownloader2 and Radarr. It automatically detects new movies added to your Real-Debrid account, sends them to JDownloader2 for download, and then adds them to your Radarr library for organization.

-----

## ✨ Features

  * **Automated Workflow**: Monitors your Real-Debrid account for new movie additions.
  * **JDownloader2 Integration**: Automatically sends download links to JDownloader2.
  * **Radarr Integration**: Adds downloaded movies to your Radarr library for renaming and organization.
  * **Web Interface**: A simple Flask-based web UI to trigger the process manually.

-----

## ⚙️ How It Works

The script works in a simple sequence:

1.  It fetches the list of recently added torrents from your Real-Debrid cache.
2.  It checks which of these are movies and not already in your Radarr library.
3.  It generates the direct download links for the new movies.
4.  Finally, it pushes these links to JDownloader2 to begin downloading.

-----

## 📋 Prerequisites

Before you begin, ensure you have the following software and services set up:

  * **OS**: Ubuntu 22.04 / 24.04
  * **Python**: Version 3.10+
  * **Python Packages**:
      * `requests`
      * `Flask`
  * **Applications**:
      * [JDownloader2](https://jdownloader.org/jdownloader2)
      * [Radarr](https://radarr.video/)
  * **Services**:
      * [Real-Debrid](http://real-debrid.com/) account

-----

## 🚀 Installation & Setup

Follow these steps to get AutoDebrid up and running on your Ubuntu machine.

1.  **Update System & Install Pip**
    Open a terminal and run the following commands to update your package list and install the Python package manager, pip.

    ```bash
    sudo apt update
    sudo apt install python3-pip -y
    ```

2.  **Install Python Dependencies**
    Install the required Python packages using pip.

    ```bash
    pip3 install requests Flask
    ```

3.  **Clone or Download the Application**
    Place the `AutoDebrid` application folder in your home directory.
    

5.  **Configure the Application**
    You'll need to edit the configuration file (`config.ini` or similar) to add your API keys and credentials for Real-Debrid, JDownloader2, and Radarr.
    *(You may want to add a section explaining which file to edit and what values are needed).*

-----

## ▶️ Usage

Once the setup is complete, you can run the application.

1.  Navigate to the application folder in your terminal:

    ```bash
    cd /home/your_username/AutoDebrid
    ```

2.  Run the application using Python:

    ```bash
    python3 app.py
    ```

3.  The application will start a web server. You can access it by opening your web browser and navigating to:

    `http://YOUR_SERVER_IP:5000`

    Replace `YOUR_SERVER_IP` with the local IP address of the machine running the script. From the web interface, you can trigger the script to check for new movies.
