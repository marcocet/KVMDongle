"""
webui.py

Minimal Flask page for uploading/deleting ISOs on the Pi's SD card over
the Wi-Fi AP (toggled on with wifi-ap-toggle.sh). Runs as its own
systemd service, independent of daemon.py -- the two only interact
through the filesystem (the ISOs directory) and the gadget's read-only
lun.0/file attribute (just to show what's currently mounted, so an
upload/delete can't clobber the ISO the target machine is actively using).

Not hardened for exposure beyond the toggled, private AP this runs on --
don't leave the AP on permanently or port-forward this anywhere.
"""

import json
import os

from flask import Flask, redirect, render_template_string, request, url_for
from werkzeug.utils import secure_filename

GADGET_INFO_PATH = "/run/kvmdongle/gadget-info.json"

app = Flask(__name__)


def gadget_info():
    with open(GADGET_INFO_PATH) as f:
        return json.load(f)


def current_iso(info):
    try:
        with open(info["lun0_file_attr"]) as f:
            value = f.read().strip()
    except OSError:
        return None
    return os.path.basename(value) if value else None


PAGE = """
<!doctype html>
<title>KVM Dongle - ISOs</title>
<h1>ISOs on the Pi</h1>
<p>Currently mounted: <b>{{ current or "(none)" }}</b></p>
<ul>
{% for name in isos %}
  <li>
    {{ name }}{% if name == current %} (mounted, can't delete){% endif %}
    {% if name != current %}
      <form style="display:inline" method="post" action="{{ url_for('delete', name=name) }}">
        <button type="submit" onclick="return confirm('Delete {{ name }}?')">Delete</button>
      </form>
    {% endif %}
  </li>
{% endfor %}
</ul>
<form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
  <input type="file" name="file" accept=".iso" required>
  <button type="submit">Upload</button>
</form>
{% if message %}<p><b>{{ message }}</b></p>{% endif %}
"""


@app.route("/")
def index():
    info = gadget_info()
    isos = sorted(n for n in os.listdir(info["isos_dir"]) if n.lower().endswith(".iso"))
    return render_template_string(
        PAGE, isos=isos, current=current_iso(info), message=request.args.get("message")
    )


@app.route("/upload", methods=["POST"])
def upload():
    info = gadget_info()
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("index", message="No file selected"))

    name = secure_filename(f.filename)
    if not name.lower().endswith(".iso"):
        return redirect(url_for("index", message="Only .iso files are accepted"))

    isos_dir = info["isos_dir"]
    final_path = os.path.join(isos_dir, name)
    temp_path = final_path + ".uploading"

    f.save(temp_path)
    os.rename(temp_path, final_path)  # atomic: never visible half-written

    return redirect(url_for("index", message=f"Uploaded {name}"))


@app.route("/delete/<name>", methods=["POST"])
def delete(name):
    info = gadget_info()
    name = secure_filename(name)
    if name == current_iso(info):
        return redirect(url_for("index", message="Can't delete the currently mounted ISO"))

    path = os.path.join(info["isos_dir"], name)
    if os.path.isfile(path):
        os.remove(path)
        return redirect(url_for("index", message=f"Deleted {name}"))
    return redirect(url_for("index", message="File not found"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
