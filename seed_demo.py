"""Fill attendance.db with fake students + 30 days of attendance so you can
demo the dashboard/reports without a camera.  Run:  python seed_demo.py
(Delete attendance.db afterwards to start clean.)"""
import random
from datetime import datetime, timedelta

import db

db.init_db()
NAMES = ["Aarav", "Diya", "Rohan", "Sneha", "Kabir", "Meera", "Arjun", "Isha", "Vikram", "Anaya",
         "Rahul", "Priya", "Aditya", "Neha", "Karan"]
conn = db.get_conn()
for i, n in enumerate(NAMES, 1):
    conn.execute("INSERT OR IGNORE INTO users(name, roll_no, department, created_at) VALUES(?,?,?,?)",
                 (n, f"CSE{i:03d}", "CSE", datetime.now().isoformat(timespec="seconds")))
conn.commit()
random.seed(1)
for back in range(30, -1, -1):
    day = datetime.now() - timedelta(days=back)
    if day.weekday() == 6:      # skip Sundays
        continue
    for n in NAMES:
        if random.random() < 0.82:
            when = day.replace(hour=random.choice([8, 8, 9, 9, 9, 10]), minute=random.randint(0, 59),
                               second=random.randint(0, 59))
            if back == 0 and when > datetime.now():
                continue
            db.log_attendance(n, "face", round(random.uniform(.5, .95), 2), when=when, conn=conn)
conn.close()
print("Demo data added. Login: admin / admin123")
