# AutoDebrid 🎬

AutoDebrid is a slick automation tool that connects your Real-Debrid cache with JDownloader2 and Radarr. It automatically detects new movies added to your Real-Debrid account, sends them to JDownloader2 for download, and then adds them to your Radarr library for organization.

***

## ✨ Features

* **Automated Workflow**: Monitors your Real-Debrid account for new movie additions.
* **JDownloader2 Integration**: Automatically sends download links to JDownloader2.
* **Radarr Integration**: Adds downloaded movies to your Radarr library for renaming and organization.
* **Web Interface**: A simple Flask-based web UI to trigger the process manually.
* **Easy Setup**: A command-line wizard helps you configure the app on the first run.

***

## ⚙️ How It Works

The script works in a simple sequence:
1.  It fetches the list of recently added torrents from your Real-Debrid cache.
2.  It checks which of these are movies and not already in your Radarr library.
3.  It generates the direct download links for the new movies.
4.  Finally, it pushes these links to JDownloader2 to begin downloading.

***

## 📋 Prerequisites

Before you begin, ensure you have the following software and services set up:

* **OS**: Ubuntu 22.04 / 24.04
* **Python**: Version 3.10+
* **Applications**:
    * JDownloader2 with MyJDownloader credentials configured.
    * Radarr
* **Services**:
    * A Real-Debrid account with an API token.
* **Python Packages**: All required packages are listed in the `requirements.txt` file.

***

## 🚀 Installation & Setup

Follow these steps to get AutoDebrid up and running on your machine.

### 1. Prepare System & Clone Repository
Open a terminal and run the following commands to update your system, install `pip` and `git`, and clone the application repository.
```bash
sudo apt update && sudo apt install python3-pip git -y
git clone [https://github.com/naughteric/AutoDebrid.git](https://github.com/your_username/AutoDebrid.git)
cd AutoDebrid
````

### 2\. Install Python Dependencies

Install the required Python packages using the `requirements.txt` file.

```bash
pip3 install -r requirements.txt
```

### 3\. Configure the Application

Configuration is handled automatically. Simply run the application for the first time, and a setup wizard will prompt you for all necessary information (API keys, credentials, etc.).

```bash
python3 app.py
```

After you enter your details, they will be saved to a `.env` file in the application's directory for future use.

-----

## ▶️ Usage

Once the setup is complete, you can run the application at any time.

Navigate to the application folder in your terminal:

```bash
cd /path/to/AutoDebrid
```

Run the application using Python:

```bash
python3 app.py
```

The application will start a web server. You can access it by opening your web browser and navigating to:
**`http://YOUR_SERVER_IP:5000`**

Replace `YOUR_SERVER_IP` with the local IP address of the machine running the script. From the web interface, you can trigger the script to check for new movies.

```
```
