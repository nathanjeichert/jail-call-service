import sqlite3
import json

db = sqlite3.connect('jobs/jail_calls.db')
c = db.cursor()
c.execute("SELECT file_paths FROM jobs ORDER BY created_at DESC LIMIT 1")
res = c.fetchone()[0]
if res:
    print(json.loads(res)[:2])
else:
    print(res)
