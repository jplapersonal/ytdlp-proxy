import time
import requests
import subprocess
import os

API_URL = "https://reloadtrack-app.pages.dev/api/queue"
SECRET = "rt2026proxy"
DOWNLOAD_FOLDER = "/Volumes/Musica/ReloadTrack/HouseMash"

def process_task(task):
    print(f"Processing task {task['id']}: {task['artist']} - {task['title']}")
    url = task.get('url')
    
    if not url:
        print("No URL provided, skipping")
        return False
        
    try:
        if 'deezer.com' in url:
            # use rip
            print(f"Downloading with rip: {url}")
            os.chdir(DOWNLOAD_FOLDER)
            res = subprocess.run(['rip', 'url', url], capture_output=True, text=True)
            if res.returncode != 0:
                print(f"Rip error: {res.stderr}")
                return False
        else:
            # use yt-dlp
            print(f"Downloading with yt-dlp: {url}")
            os.chdir(DOWNLOAD_FOLDER)
            res = subprocess.run(['yt-dlp', '-x', '--audio-format', 'mp3', url], capture_output=True, text=True)
            if res.returncode != 0:
                print(f"yt-dlp error: {res.stderr}")
                return False
        return True
    except Exception as e:
        print(f"Exception: {e}")
        return False

def main():
    print("Starting ReloadTrack Daemon...")
    while True:
        try:
            # Pop next task
            r = requests.get(f"{API_URL}?pop=1&secret={SECRET}")
            if r.status_code == 200:
                data = r.json()
                if data.get('pending') and data.get('task'):
                    task = data['task']
                    success = process_task(task)
                    
                    # Update status
                    status = 'completed' if success else 'error'
                    requests.patch(f"{API_URL}?secret={SECRET}", json={
                        "id": task['id'],
                        "status": status
                    })
                    print(f"Task {task['id']} marked as {status}")
                    continue # check for next task immediately
        except Exception as e:
            print(f"Polling error: {e}")
            
        time.sleep(10) # wait before polling again

if __name__ == '__main__':
    main()
