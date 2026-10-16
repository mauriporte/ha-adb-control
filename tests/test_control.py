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
