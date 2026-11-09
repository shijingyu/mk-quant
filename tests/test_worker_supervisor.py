"""Exercise real processes without connecting to a production service."""
from pathlib import Path
import signal
import subprocess
import sys
import time

from deploy.bt_register import LAUNCHER


ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError('subprocess condition timed out')


def launches(path):
    return path.read_text().splitlines() if path.exists() else []


def start_supervisor(tmp_path, child_source):
    script = tmp_path / 'child.py'
    script.write_text(child_source)
    runner = (
        'from deploy.worker_supervisor import supervise,AlreadySupervised\n'
        'import sys\n'
        'try:\n'
        f' sys.exit(supervise([sys.executable,{str(script)!r}],{str(tmp_path / "role.lock")!r},role="test",restart_min=.05,restart_max=.15,stop_grace=.2))\n'
        'except AlreadySupervised:\n sys.exit(73)\n'
    )
    return subprocess.Popen([sys.executable, '-c', runner], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def finish(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
    if process.stderr:
        process.stderr.close()


def test_crash_restarts_and_deliberate_stop_does_not_restart(tmp_path):
    counts = tmp_path / 'starts'
    source = f'''from pathlib import Path
import os,time
p=Path({str(counts)!r})
with p.open('a') as f:f.write(str(os.getpid())+'\\n')
if len(p.read_text().splitlines())<3:raise SystemExit(17)
while True:time.sleep(.1)
'''
    parent = start_supervisor(tmp_path, source)
    try:
        wait_for(lambda: len(launches(counts)) == 3)
        parent.terminate()
        assert parent.wait(timeout=4) == 0
        time.sleep(.25)
        assert len(launches(counts)) == 3
    finally:
        finish(parent)


def test_duplicate_supervisor_cannot_start_another_worker(tmp_path):
    counts = tmp_path / 'starts'
    source = f'''from pathlib import Path
import os,time
with Path({str(counts)!r}).open('a') as f:f.write(str(os.getpid())+'\\n')
while True:time.sleep(.1)
'''
    first = start_supervisor(tmp_path, source)
    second = None
    try:
        wait_for(lambda: len(launches(counts)) == 1)
        second = start_supervisor(tmp_path, source)
        assert second.wait(timeout=4) == 73
        assert len(launches(counts)) == 1
    finally:
        if second:finish(second)
        finish(first)


def test_deliberate_stop_terminates_worker_descendants(tmp_path):
    marker = tmp_path / 'grandchild-stopped'
    ready = tmp_path / 'grandchild-ready'
    grandchild = (
        'import signal,time\nfrom pathlib import Path\n'
        f'def stop(*args):\n Path({str(marker)!r}).write_text("stopped")\n raise SystemExit(0)\n'
        'signal.signal(signal.SIGTERM,stop)\n'
        f'Path({str(ready)!r}).touch()\n'
        'while True:time.sleep(.1)\n'
    )
    source = f'''import subprocess,sys,time,signal
p=subprocess.Popen([sys.executable,'-c',{grandchild!r}])
def stop(*args):
 p.wait(timeout=2)
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.1)
'''
    parent = start_supervisor(tmp_path, source)
    try:
        wait_for(ready.exists)
        parent.send_signal(signal.SIGINT)
        assert parent.wait(timeout=4) == 0
        assert marker.read_text() == 'stopped'
    finally:
        finish(parent)


def test_baota_launcher_keeps_non_api_supervisor_parent():
    compile(LAUNCHER, 'launcher.py', 'exec')
    assert 'raise SystemExit(run_role(role,base))' in LAUNCHER
    assert '"uvicorn","backend.app:app"' in LAUNCHER
    assert '"MK_ENABLE_LIVE"]="0"' in LAUNCHER
