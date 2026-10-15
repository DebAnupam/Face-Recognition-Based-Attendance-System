"""Face detection / recognition used by the web app (Scan + Register pages).

Uses the SAME dlib models and the SAME data files as the original scripts:
  data/data_faces_from_camera/person_<n>_<name>/*.jpg   (photos)
  data/features_all.csv                                  (name + 128 numbers per row)
so attendance_taker.py and the web app can be used side by side.

Importing this module never fails: if dlib or the model files are missing,
`status()` explains what is wrong and the web pages show that message."""
import csv
import glob
import importlib.util
import os
import shutil
import threading

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
FACES_DIR = os.path.join(BASE, "data", "data_faces_from_camera")
FEATURES_CSV = os.path.join(BASE, "data", "features_all.csv")
MODEL_DIR = os.path.join(BASE, "data", "data_dlib")
LANDMARK_FILE = "shape_predictor_68_face_landmarks.dat"
RESNET_FILE = "dlib_face_recognition_resnet_model_v1.dat"

# Same value as the original attendance_taker.py (smaller = stricter).
THRESHOLD = float(os.environ.get("FACE_THRESHOLD", "0.4"))
SCAN_WIDTH = 480            # frames are shrunk to this width before detection (speed)

_lock = threading.Lock()    # dlib objects are not thread-safe
_state = {"loaded": False, "error": None}
_known = {"mtime": None, "names": [], "matrix": np.zeros((0, 128))}


# ------------------------------------------------------------------ loading
def _find_model(filename):
    p = os.path.join(MODEL_DIR, filename)
    if os.path.isfile(p):
        return p
    # fallback: `pip install face_recognition_models` ships the same two files
    spec = importlib.util.find_spec("face_recognition_models")
    if spec and spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            p = os.path.join(loc, "models", filename)
            if os.path.isfile(p):
                return p
    return None


def _load():
    if _state["loaded"] or _state["error"]:
        return
    try:
        import cv2  # noqa: F401
        import dlib
    except ImportError as e:
        _state["error"] = f"Missing package: {e.name}. Run: pip install -r requirements.txt"
        return
    lm, rn = _find_model(LANDMARK_FILE), _find_model(RESNET_FILE)
    if not lm or not rn:
        _state["error"] = ("dlib model files not found. Either put the two .dat files in "
                           "data/data_dlib/ or run: pip install face_recognition_models")
        return
    _state["dlib"] = dlib
    _state["detector"] = dlib.get_frontal_face_detector()
    _state["predictor"] = dlib.shape_predictor(lm)
    _state["model"] = dlib.face_recognition_model_v1(rn)
    _state["loaded"] = True


def status():
    """(ok, message)"""
    _load()
    return (True, "ready") if _state["loaded"] else (False, _state["error"])


# ------------------------------------------------------------------ helpers
def decode_image(data: bytes):
    import cv2
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Not a valid image")
    return img


def _rgb(img):
    import cv2
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _descriptor(rgb, rect):
    shape = _state["predictor"](rgb, rect)
    return np.array(_state["model"].compute_face_descriptor(rgb, shape))


def _largest(faces):
    return max(faces, key=lambda r: r.width() * r.height())


# ----------------------------------------------------------- known faces
def load_known(force=False):
    """(names, matrix) from features_all.csv; reloads when the file changes."""
    try:
        m = os.path.getmtime(FEATURES_CSV)
    except OSError:
        _known.update(mtime=None, names=[], matrix=np.zeros((0, 128)))
        return _known["names"], _known["matrix"]
    if force or _known["mtime"] != m:
        names, rows = [], []
        with open(FEATURES_CSV, newline="") as f:
            for row in csv.reader(f):
                if len(row) < 129:
                    continue
                try:
                    vec = [float(x) if x != "" else 0.0 for x in row[1:129]]
                except ValueError:
                    continue
                if not any(vec):          # person with no usable photos
                    continue
                names.append(row[0])
                rows.append(vec)
        _known.update(mtime=m, names=names, matrix=np.array(rows).reshape(-1, 128))
    return _known["names"], _known["matrix"]


def recognize(img):
    """Detect + identify every face in a BGR frame.
    Returns (faces, (w, h)); each face = {box:[x,y,w,h] as 0-1 fractions,
    name:str|None, distance:float|None}."""
    ok, msg = status()
    if not ok:
        raise RuntimeError(msg)
    import cv2
    h0, w0 = img.shape[:2]
    if w0 > SCAN_WIDTH:
        img = cv2.resize(img, (SCAN_WIDTH, int(h0 * SCAN_WIDTH / w0)))
    h, w = img.shape[:2]
    rgb = _rgb(img)
    names, matrix = load_known()
    out = []
    with _lock:
        for r in _state["detector"](rgb, 0):
            d = _descriptor(rgb, r)
            name = dist = None
            if len(names):
                dists = np.linalg.norm(matrix - d, axis=1)
                i = int(np.argmin(dists))
                dist = float(dists[i])
                if dist < THRESHOLD:
                    name = names[i]
            x, y = max(r.left(), 0), max(r.top(), 0)
            out.append({"box": [x / w, y / h, (r.right() - x) / w, (r.bottom() - y) / h],
                        "name": name, "distance": dist})
    return out, (w, h)


# ---------------------------------------------------------- face folders
def _folders():
    if not os.path.isdir(FACES_DIR):
        return []
    return sorted(f for f in os.listdir(FACES_DIR) if os.path.isdir(os.path.join(FACES_DIR, f)))


def person_folder(name):
    for f in _folders():
        if f.split("_", 2)[-1] == name:
            return os.path.join(FACES_DIR, f)
    return None


def photo_count(name):
    p = person_folder(name)
    return len(glob.glob(os.path.join(p, "*.jpg"))) if p else 0


def save_photo(name, img):
    """Save one training photo for `name`. Requires exactly one face in the frame.
    Returns (ok, message, photo_count)."""
    ok, msg = status()
    if not ok:
        raise RuntimeError(msg)
    import cv2
    h0, w0 = img.shape[:2]
    if w0 > 640:
        img = cv2.resize(img, (640, int(h0 * 640 / w0)))
    with _lock:
        faces = _state["detector"](_rgb(img), 1)
    if len(faces) == 0:
        return False, "No face found - face the camera in good light.", photo_count(name)
    if len(faces) > 1:
        return False, "More than one face in the frame - only one person please.", photo_count(name)
    folder = person_folder(name)
    if not folder:
        nums = [int(f.split("_")[1]) for f in _folders() if f.split("_")[1].isdigit()]
        folder = os.path.join(FACES_DIR, f"person_{max(nums, default=0) + 1}_{name}")
        os.makedirs(folder)
    existing = [int(os.path.splitext(os.path.basename(p))[0].split("_")[-1])
                for p in glob.glob(os.path.join(folder, "img_face_*.jpg"))]
    path = os.path.join(folder, f"img_face_{max(existing, default=0) + 1}.jpg")
    cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return True, "Photo saved.", photo_count(name)


def _person_mean(folder):
    """Mean 128-d descriptor over the photos in a folder (None if no face found)."""
    import cv2
    vecs = []
    for p in sorted(glob.glob(os.path.join(folder, "*.jpg")) + glob.glob(os.path.join(folder, "*.png"))):
        img = cv2.imread(p)
        if img is None:
            continue
        rgb = _rgb(img)
        with _lock:
            faces = _state["detector"](rgb, 1)
            if len(faces) == 0:
                # photos from the Tkinter tool are already tight face crops
                faces = [_state["dlib"].rectangle(0, 0, img.shape[1], img.shape[0])]
            vecs.append(_descriptor(rgb, _largest(faces)))
    return np.mean(vecs, axis=0) if vecs else None


def _read_csv():
    rows = {}
    if os.path.isfile(FEATURES_CSV):
        with open(FEATURES_CSV, newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 129:
                    rows[row[0]] = row[1:129]
    return rows


def _write_csv(rows):
    os.makedirs(os.path.dirname(FEATURES_CSV), exist_ok=True)
    with open(FEATURES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        for name, vec in rows.items():
            w.writerow([name, *vec])
    load_known(force=True)


def train(name):
    """(Re)compute one person's features and update features_all.csv."""
    ok, msg = status()
    if not ok:
        raise RuntimeError(msg)
    folder = person_folder(name)
    mean = _person_mean(folder) if folder else None
    rows = _read_csv()
    if mean is None:
        rows.pop(name, None)
        _write_csv(rows)
        return False
    rows[name] = [repr(float(x)) for x in mean]
    _write_csv(rows)
    return True


def train_all():
    """Rebuild features_all.csv from every folder. Returns number of people trained."""
    ok, msg = status()
    if not ok:
        raise RuntimeError(msg)
    rows = {}
    for f in _folders():
        mean = _person_mean(os.path.join(FACES_DIR, f))
        if mean is not None:
            rows[f.split("_", 2)[-1]] = [repr(float(x)) for x in mean]
    _write_csv(rows)
    return len(rows)


def delete_person(name):
    """Remove a person's photos and their row in features_all.csv."""
    folder = person_folder(name)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    rows = _read_csv()
    if name in rows:
        rows.pop(name)
        _write_csv(rows)


def rename_person(old, new):
    folder = person_folder(old)
    if folder:
        head = os.path.basename(folder).split("_", 2)
        os.rename(folder, os.path.join(FACES_DIR, f"{head[0]}_{head[1]}_{new}"))
    rows = _read_csv()
    if old in rows:
        rows = {(new if k == old else k): v for k, v in rows.items()}
        _write_csv(rows)
