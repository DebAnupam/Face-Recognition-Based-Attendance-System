# Face Recognition Based Attendance System

This project is a face recognition-based attendance system that uses OpenCV and Python. The system uses a camera to capture images of individuals and then compares them with the images in the database to mark attendance.

## Installation

1. Clone the repository to your local machine. ``` git clone https://github.com/Arijit1080/Face-Recognition-Based-Attendance-System ```
2. Use **Python 3.10, 3.11 or 3.12** (3.14 is too new for dlib/OpenCV; replace 3.11 below with your version). Then, in PowerShell:
   ```
   py -3.11 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. (Optional - only for the old command-line scripts) download the dlib models from https://drive.google.com/drive/folders/12It2jeNQOxwStBxtagL1vvIJokoz-DL4?usp=sharing and place the data folder inside the repo

## Usage (everything from the website)

1. `python app.py` and open http://127.0.0.1:5000 (first login **admin / admin123** - change it under *Password*).
2. **Users -> Add user**, then click **Register face**. Start the camera, press *Auto-capture 10*
   (slowly turn your head), then *Save face data*.
3. Open the **Scan** page, press *Start camera*. Recognised people are marked automatically
   and appear on the live **Dashboard**.
4. No camera / want fake data? Run `python seed_demo.py` first.

The dlib model files are found automatically from the `face_recognition_models` package
(or from `data/data_dlib/` if you downloaded them from the Drive link).
The browser only allows the camera on `localhost`/`127.0.0.1` or HTTPS - open the site as http://127.0.0.1:5000.

The original command-line tools (`get_faces_from_camera_tkinter.py`, `features_extraction_to_csv.py`,
`attendance_taker.py`) still work and use the same files and database.

## What was added (v2)

### 1. Real-time Dashboard & Analytics (`/`)
- Live stat cards (total, present, late, absent, attendance rate) refreshed every 3 s from `/api/live`
- Charts (Chart.js): 14-day trend, check-ins by hour, today's present/late/absent split
- Live "recent check-ins" feed - a new face scan appears without reloading

### 2. Attendance Logging & Reporting (`/attendance`, `/reports`)
- `attendance_taker.py` now logs through `db.py`: one record per person per day, **Present/Late** status
  (cut-off configurable), recognition confidence, method (`face` / `manual`)
- Attendance page: pick any date, search by name, see who is **absent**, mark manually, delete wrong records (admin)
- Reports: date range, per-student present/late/absent and attendance %, 75 % warning colour,
  CSV export (summary or all records) and print view

### 3. Face Scan & Registration (`/scan`, `/users/<id>/face`)
- Browser webcam scan: live boxes + names on the video, attendance marked on recognition
  (green = marked, blue = already marked today, red = unknown)
- Register / re-register / delete a person's face data from the website (`face_engine.py`, dlib 128-d descriptors,
  same threshold 0.4 as the original project)

### 4. User Management (`/users`)
- Login system with **admin** and **teacher** roles (hashed passwords, CSRF protection)
- Add / edit / deactivate / delete students (name, roll no, department, email) and see whether face data exists
- "Rebuild face features" button runs `features_extraction_to_csv.py`
- Staff accounts and settings (late cut-off time, institution name)
- Deactivated users are ignored by the camera

## Project layout
| File | Purpose |
|------|---------|
| `app.py` | Flask web app (all routes) |
| `db.py` | SQLite layer shared by the web app and camera script; auto-upgrades an old `attendance.db` |
| `face_engine.py` | dlib face detection, recognition, photo saving, training |
| `attendance_taker.py` | (optional) stand-alone OpenCV camera window -> `db.log_attendance()` |
| `seed_demo.py` | Optional demo data |
| `templates/`, `static/` | Pages and CSS |

## Known limitations
- A printed photo or a phone screen held to the camera can fool the recognition (no liveness check).
- Accuracy depends on light and on the quality of the registration photos (take 8-10 at slightly different angles).
- Uses SQLite and Flask's development server - fine for a college project, not for production.

Set `SECRET_KEY` as an environment variable before deploying anywhere public.
