"""Start the real HTTP handler against a disposable database, never config.json."""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
    if "--exe" in sys.argv:
        app = Path(__file__).resolve().parents[1] / "TeamToDo V1.1"
        shutil.copy2(app / "TeamToDo.exe", Path(folder) / "TeamToDo.exe")
        shutil.copytree(app / "static", Path(folder) / "static")
        shutil.copytree(app / "db", Path(folder) / "db")
        shutil.copy2(app / "excelutil.py", Path(folder) / "excelutil.py")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, TEAMTODO_DB=server.DB_PATH,
                   TEAMTODO_HOST="127.0.0.1", TEAMTODO_UPDATE_ENABLED="false",
                   TEAMTODO_UPDATE_AUTO_INSTALL="false")
        child = subprocess.Popen(
            [str(Path(folder) / "TeamToDo.exe"), "--db", server.DB_PATH,
             "--port", str(port), "--no-browser", "--local"],
            cwd=folder, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = "http://127.0.0.1:%d" % port
            for attempt in range(100):
                if child.poll() is not None:
                    raise RuntimeError("Isolated EXE exited: %s" % child.returncode)
                try:
                    with urllib.request.urlopen(url + "/api/ping", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Isolated EXE did not start")
            print("TEST_URL=" + url, flush=True)
            sys.stdin.read()
        finally:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child.wait(timeout=10)
    else:
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
