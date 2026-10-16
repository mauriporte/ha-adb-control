import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

os.environ.setdefault("ADB_CONTROL_TOKEN", "test")
_spec = importlib.util.spec_from_file_location("control", pathlib.Path(__file__).parents[1] / "control.py")
control = importlib.util.module_from_spec(_spec)
sys.modules["control"] = control
_spec.loader.exec_module(control)


def test_ip_validation_blocks_anything_but_ipv4():
    assert control.ip_of("192.168.1.21 (Phone)") == "192.168.1.21"
    for bad in ("", None, "; rm -rf /", "192.168.1.21;id", "host.local", "1.2.3"):
        assert control.ip_of(bad) == ""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_viewer_page_javascript_parses(tmp_path):
    js = control.VIEW.split("<script>")[1].split("</script>")[0]
    js = js.replace("__VIEW_HEIGHT__", "410").replace("__SCRCPY_PORT__", "8001")
    f = tmp_path / "view.js"
    f.write_text(js, encoding="utf8")
    assert subprocess.run(["node", "--check", str(f)]).returncode == 0


def test_stream_mode_keeps_devices_with_video_on(monkeypatch):
    import io
    import json

    reply = {"on": True, "streams": {"192.168.1.21": {"src": "phone_a", "video": True},
                                     "192.168.1.22": {"src": "tablet", "video": False}}}
    monkeypatch.setattr(control, "STREAM_URL", "http://stream:8005")
    monkeypatch.setattr(control.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(json.dumps(reply).encode()))
    devices = [{"ip": "192.168.1.21"}, {"ip": "192.168.1.22"}, {"ip": "192.168.1.23"}]
    assert control.with_streams(devices) == [{"ip": "192.168.1.21", "player": "http://stream:8005/play?src=phone_a"}]
    reply["on"] = False  # Stream Control off: nothing to show
    assert control.with_streams(devices) == []
    monkeypatch.setattr(control, "STREAM_URL", "")  # stream mode not configured
    assert control.with_streams(devices) == []


class _Req:
    """Just enough of a request handler for the remote key check."""
    def __init__(self, path, headers):
        self.path, self.headers, self.replies = path, headers, []

    def _reply(self, code, body, *args):
        self.replies.append(code)


def test_remote_requests_need_the_key(monkeypatch):
    monkeypatch.setattr(control, "REMOTE_KEY", "s3cret")
    ok = control.Handler._remote_ok
    assert ok(_Req("/view", {}))  # LAN: no key needed
    cf = {"Cf-Connecting-Ip": "203.0.113.5"}
    denied = _Req("/view", cf)
    assert not ok(denied) and denied.replies == [403]
    assert ok(_Req("/view?key=s3cret", cf))
    assert ok(_Req("/scrcpy/bundle.js", {**cf, "Cookie": "a=1; adb_key=s3cret"}))
    assert not ok(_Req("/view?key=wrong", cf))
    monkeypatch.setattr(control, "REMOTE_KEY", "")  # no key configured: nothing gets in through the tunnel
    assert not ok(_Req("/view?key=", cf))
