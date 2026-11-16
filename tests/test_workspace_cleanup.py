"""Private workspace removal waits for transient Windows handle release."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from mutator.workers import delete_tree


@pytest.mark.skipif(os.name != 'nt', reason='Windows keeps a process working directory open')
def test_cleanup_waits_for_a_terminating_process_to_release_its_directory(tmp_path):
    directory=tmp_path/'worker'; directory.mkdir()
    child=subprocess.Popen([sys.executable,'-c','import time; print("ready", flush=True); time.sleep(30)'],
                           cwd=directory,stdout=subprocess.PIPE,text=True)
    assert child.stdout.readline().strip()=='ready'
    def release():
        time.sleep(0.05)
        child.terminate(); child.wait(timeout=5)
    thread=threading.Thread(target=release); thread.start()
    try:
        delete_tree(directory)
        assert not directory.exists()
    finally:
        thread.join(timeout=5)
        if child.poll() is None: child.kill()
        child.wait(timeout=5); child.stdout.close()


def test_cleanup_does_not_hide_permanent_errors(tmp_path,monkeypatch):
    directory=tmp_path/'worker'; directory.mkdir()
    error=PermissionError('access denied'); error.winerror=5
    def denied(self): raise error
    monkeypatch.setattr(Path,'rmdir',denied)
    with pytest.raises(PermissionError) as failure: delete_tree(directory)
    assert failure.value is error


def test_cleanup_preserves_sharing_error_when_release_deadline_expires(tmp_path,monkeypatch):
    from mutator import workers
    directory=tmp_path/'worker'; directory.mkdir()
    error=PermissionError('still in use'); error.winerror=32
    def locked(self): raise error
    clock=iter((0.0,2.0))
    monkeypatch.setattr(Path,'rmdir',locked)
    monkeypatch.setattr(workers.time,'monotonic',lambda:next(clock))
    with pytest.raises(PermissionError) as failure: delete_tree(directory)
    assert failure.value is error
