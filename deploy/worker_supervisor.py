"""Small BaoTa-owned worker parent; no database dependency or trading state.

The launcher remains BaoTa's tracked process. Only unexpected worker exits are
restarted; a deliberate stop terminates the complete worker process group.
"""
import argparse
import ctypes
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class AlreadySupervised(RuntimeError):
    pass


def _linux_parent_death(parent_pid):
    # Loaded before fork, used only by this single-threaded launcher. The
    # worker retains SIGTERM across exec and performs its ordinary shutdown.
    if not sys.platform.startswith('linux'):
        return None
    libc = ctypes.CDLL(None, use_errno=True)

    def configure():
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot set parent-death signal')
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)
    return configure


def _stop_group(child, grace):
    """Also remove grandchildren left by an unexpectedly dead coordinator."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.05)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def supervise(command, lock_path, *, role, restart_min=5, restart_max=30, stop_grace=5):
    if restart_min <= 0 or restart_max < restart_min or stop_grace < 0:
        raise ValueError('Invalid supervisor timing')
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadySupervised(f'{role} already has a supervisor') from exc
        lock.seek(0); lock.truncate(); lock.write(str(os.getpid()) + '\n'); lock.flush()
        stopping = False
        child = None

        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        delay = restart_min
        try:
            while not stopping:
                started = time.monotonic()
                try:
                    child = subprocess.Popen(command, start_new_session=True,
                        preexec_fn=_linux_parent_death(os.getpid()), pass_fds=(lock.fileno(),))
                except OSError as exc:
                    # Resource pressure can temporarily prevent fork/exec too.
                    result = f'spawn-error:{exc.errno}'
                else:
                    print(f'{role}: worker started pid={child.pid}', flush=True)
                    while not stopping and child.poll() is None:
                        time.sleep(.1)
                    result = child.poll()
                    _stop_group(child, stop_grace)
                    child = None
                if stopping:
                    break
                # A healthy run resets crash backoff; repeated startup failures
                # cannot spin, including temporary database unavailability.
                if time.monotonic() - started >= 60:
                    delay = restart_min
                print(f'{role}: worker exited code={result}; restart in {delay:g}s', flush=True)
                deadline = time.monotonic() + delay
                while not stopping and time.monotonic() < deadline:
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
                delay = min(restart_max, delay * 2)
            return 0
        finally:
            if child is not None:
                _stop_group(child, stop_grace)
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)


def run_role(role, base):
    if role not in {'runtime', 'backtest'}:
        raise ValueError('Only independent runtime/backtest roles are supervised')
    return supervise([sys.executable, '-m', 'backend.worker', role],
                     Path(base) / 'services' / role / 'worker-supervisor.lock', role=role)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('runtime', 'backtest'))
    args = parser.parse_args()
    try:
        return run_role(args.role, Path(__file__).resolve().parents[1])
    except AlreadySupervised as exc:
        print(str(exc), file=sys.stderr)
        return 73


if __name__ == '__main__':
    raise SystemExit(main())
