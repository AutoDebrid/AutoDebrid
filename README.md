# AutoDebrid
Automatically downloads movies added from your Real-Debrid Cache using JDownloader2 and adds them to Radarr

#Prerequisites
Python 3.10.12
Requests Python Package (pip install requests)
Flask Python Package (pip install Flask requests)
Jdownloader2
Radarr
Real-Debrid

#Installation
Currenty this is only known to work on Ubuntu (22.04 and 24.04)

Spin up an instance of Ubuntu
sudo apt update
sudo apt install python3-pip -y
pip3 install requests
pip install Flask requests

Move downloader app folder to /home/your_username

Open terminal in the application folder. Run python3 app.py

Navigate to http://YOUR_VM_IP:5000
