"""Start the real HTTP handler against a disposable database, never config.json."""
import hashlib
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "TeamToDo V1.1"))
import server

with tempfile.TemporaryDirectory(prefix="teamtodo-refresh-") as folder:
    server.DB_PATH = str(Path(folder) / "tasks.db")
    server.NETWORK_DB = False
    server.DB_JOURNAL = "WAL"
    server.db_migrate()
    assert server.db_init()
    server.db_migrate()
    server.DB_READY = True
    conn = server.get_db()
    conn.execute("UPDATE users SET password=? WHERE username='admin'",
                 (hashlib.sha256(b"refresh-test").hexdigest(),))
    conn.close()
    httpd = server._BaseServer(("127.0.0.1", 0), server.Handler)
    print("TEST_URL=http://127.0.0.1:%d" % httpd.server_port, flush=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sys.stdin.read()
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()
