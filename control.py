# ADB control API for Home Assistant, next to ws-scrcpy (same container, same adb server).
#   POST /pair  {"target": "ip:port", "code": "123456"}  pair once with a device («Wireless debugging → pair with code»)
#   GET  /status               -> {"is_on": <selected phone connected>, "selected": ip, "connected": [...]}
#   POST /scan                 -> {"devices": ["192.168.1.x (hostname)", ...]}  hosts with ADB over TCP (port 5555) open
#   POST /select {"ip": ...}   -> remember which phone the HA switch refers to
#   POST /switch {"ip", "on"}  -> connect (on) or disconnect (off) that phone only; several phones can be connected
#   POST /connect {"ip"} | /disconnect {"ip"?}  (no ip = disconnect all)
#   GET  /orientation?ip=...   -> {"is_on": <shown in landscape>}   POST /orientation {"ip", "landscape"} -> set it
#                                 (how HA shows the screen only; the device itself is not rotated)
#   GET  /display?ip=...       -> {"is_on": <screen shown in the viewer>}   POST /display {"ip", "show"} -> set it
#                                 (hidden screens stay connected; they are only left out of the viewer)
#   GET  /view?mode=stream     -> the same page with the audio+video streams instead (needs ADB_STREAM_URL)
#   GET  /view                 -> page with the screens of all connected devices side by side (no token: LAN only,
#                                 and ws-scrcpy itself on :8001 has no authentication either)
# Every other request needs the X-Token header (env ADB_CONTROL_TOKEN).
import json
import os
import re
import socket
import subprocess
import threading
import time

# adb 31+ auto-connects to paired devices over their wireless-debugging port too; the viewer would then start a second
# session per device. ADB Control brings devices back on 5555 itself (reconnect_loop), so that is switched off.
os.environ["ADB_MDNS_AUTO_CONNECT"] = "0"
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = os.environ["ADB_CONTROL_TOKEN"]
SUBNET = os.environ.get("ADB_SCAN_SUBNET", "192.168.1.0/24")
PORT = int(os.environ.get("ADB_CONTROL_PORT", "8002"))
SCRCPY_PORT = int(os.environ.get("WS_SCRCPY_PORT", "8001"))
# optional audio+video streams (Stream Control, https://github.com/mauriporte/ha-stream-control): /view?mode=stream shows them with this same layout
STREAM_FILE = "/root/.android/stream_url"  # or the env var; e.g. http://192.168.1.20:8005
STREAM_URL = os.environ.get("ADB_STREAM_URL") or (open(STREAM_FILE).read().strip() if os.path.exists(STREAM_FILE) else "")
# Remote access (an https tunnel such as Cloudflare's in front of this port): /scrcpy/ and /stream/ forward to ws-scrcpy
# and to Stream Control, so the viewer works from one https address. Requests that came through Cloudflare must carry
# this key once (?key=..., kept as a cookie); the LAN needs none, as before.
KEY_FILE = "/root/.android/remote_key"
REMOTE_KEY = os.environ.get("ADB_REMOTE_KEY") or (open(KEY_FILE).read().strip() if os.path.exists(KEY_FILE) else "")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
SIZE_RE = re.compile(r"(\d+)x(\d+)")
# Every screen is shown VIEW_HEIGHT px high: the minimum that fits ws-scrcpy's button column without scrollbars.
# The width follows the device's own aspect ratio (read from the device); tablets are shown in landscape.
VIEW_HEIGHT = 410
INFO: dict[str, dict] = {}  # per serial: model, aspect (long/short side), tablet; read once per connection
SELECTED_FILE = "/root/.android/selected"  # on the volume, so a restart keeps the phone the HA switch refers to
state = {"selected": open(SELECTED_FILE).read().strip() if os.path.exists(SELECTED_FILE) else ""}
LAYOUT_FILE = "/root/.android/layout.json"  # rows of IPs, top to bottom, e.g. [["192.168.1.21", "192.168.1.22"], ...]
NAMES_FILE = "/root/.android/names.json"  # ip -> name shown by the scan instead of the network hostname
KNOWN_FILE = "/root/.android/known.json"  # ip -> last read name/model/aspect/tablet: the stream viewer shows devices
# that are streaming while not connected here
HIDDEN_FILE = "/root/.android/hidden.json"  # IPs whose screen the viewer leaves out
hidden: set[str] = set(json.load(open(HIDDEN_FILE))) if os.path.exists(HIDDEN_FILE) else set()
ORIENT_FILE = "/root/.android/orientation.json"  # ip -> landscape (bool); devices not listed: tablets landscape
orient: dict[str, bool] = json.load(open(ORIENT_FILE)) if os.path.exists(ORIENT_FILE) else {}


def set_shown(ip: str, show: bool) -> None:
    (hidden.discard if show else hidden.add)(ip)
    with open(HIDDEN_FILE, "w") as f:
        json.dump(sorted(hidden), f)


def set_landscape(ip: str, landscape: bool) -> None:
    orient[ip] = landscape
    with open(ORIENT_FILE, "w") as f:
        json.dump(orient, f)


def select(ip: str) -> None:
    state["selected"] = ip
    with open(SELECTED_FILE, "w") as f:
        f.write(ip)


def run(*cmd: str, timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


def device_info(serial: str) -> dict:
    if serial not in INFO:
        def sh(*c: str) -> str:
            return run("adb", "-s", serial, "shell", *c, timeout=8).strip()

        sizes = SIZE_RE.findall(sh("wm", "size"))  # "Physical size" and, when set, "Override size" (the last one wins)
        w, h = map(int, sizes[-1]) if sizes else (9, 16)
        ip = serial.split(":")[0]
        names = json.load(open(NAMES_FILE)) if os.path.exists(NAMES_FILE) else {}
        try:
            host = socket.gethostbyaddr(ip)[0]  # the router's name for it (= the device's own network name)
        except OSError:
            host = ""
        INFO[serial] = {
            "name": names.get(ip) or host,
            "model": sh("getprop", "ro.product.marketname") or sh("getprop", "ro.product.model"),  # e.g. Xiaomi model codes
            "aspect": round(max(w, h) / min(w, h), 4),
            "tablet": "tablet" in sh("getprop", "ro.build.characteristics"),
        }
        known = json.load(open(KNOWN_FILE)) if os.path.exists(KNOWN_FILE) else {}
        if known.get(ip) != INFO[serial]:
            known[ip] = INFO[serial]
            with open(KNOWN_FILE, "w") as f:
                json.dump(known, f)
    return INFO[serial]


BATTERY: dict[str, tuple[float, int | None]] = {}  # serial -> (read at, level)


def battery(serial: str) -> int | None:
    """Battery level read over ADB (at most once a minute): the HA app on a tablet often sleeps and stops reporting it."""
    t, level = BATTERY.get(serial, (0, None))
    if time.time() - t > 60:
        m = re.search(r"^\s*level:\s*(\d+)", run("adb", "-s", serial, "shell", "dumpsys", "battery", timeout=8), re.M)
        level = int(m.group(1)) if m else level
        BATTERY[serial] = (time.time(), level)
    return level


def connected() -> list[dict]:
    devices, live = [], set()
    for line in run("adb", "devices").splitlines()[1:]:
        if ":5555" not in line:
            continue  # only network devices; a phone on USB is not managed here
        serial, _, st = line.partition("\t")
        st = st.strip()
        info = device_info(serial) if st == "device" else {"name": "", "model": "", "aspect": 16 / 9, "tablet": False}
        live.add(serial)
        ip = serial.split(":")[0]
        devices.append({"ip": ip, "state": st, **info, "landscape": orient.get(ip, info["tablet"]), "shown": ip not in hidden,
                        "battery": battery(serial) if st == "device" else None})
    for gone in set(INFO) - live:  # forget disconnected devices, so another device on the same IP is read again
        INFO.pop(gone)
    return devices


WANTED_FILE = "/root/.android/wanted.json"  # devices switched on in ADB Control: only these are brought back
PAIR_TARGET = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}")


def set_wanted(ip: str | None, on: bool) -> None:
    w = set(json.load(open(WANTED_FILE))) if os.path.exists(WANTED_FILE) else set()
    if ip is None:
        w = set() if not on else w  # «disconnect all»
    else:
        (w.add if on else w.discard)(ip)
    with open(WANTED_FILE, "w") as f:
        json.dump(sorted(w), f)


def reconnect_loop() -> None:
    """After a reboot a device's ADB on 5555 is gone and Wireless debugging listens on a random port, which the
    device announces over mDNS. For devices switched on here (and paired once with /pair): connect over that port,
    put ADB back on 5555 (adb tcpip) and connect there, as before the reboot."""
    while True:
        time.sleep(60)
        try:
            wanted = set(json.load(open(WANTED_FILE))) if os.path.exists(WANTED_FILE) else set()
            live = {d["ip"] for d in connected() if d["state"] == "device"}
            for line in run("adb", "devices").splitlines():  # leftovers of an automatic wireless session
                serial = line.split("	")[0]
                if "._adb-tls-connect._tcp" in serial:
                    run("adb", "disconnect", serial)
            for ip in wanted - live:  # still on 5555 (e.g. after this container restarted): just connect again
                if "connected" in run("adb", "connect", f"{ip}:5555", timeout=10):
                    live.add(ip)
            for line in run("adb", "mdns", "services", timeout=15).splitlines():
                m = re.search(r"_adb-tls-connect\._tcp\s+((?:\d{1,3}\.){3}\d{1,3}):(\d+)", line)
                if not m or m.group(1) not in wanted or m.group(1) in live:
                    continue
                ip, tls = m.group(1), f"{m.group(1)}:{m.group(2)}"
                if "connected" not in run("adb", "connect", tls, timeout=15):
                    continue  # not paired with this computer yet: pair once with /pair
                run("adb", "-s", tls, "tcpip", "5555", timeout=15)
                time.sleep(4)
                run("adb", "connect", f"{ip}:5555", timeout=15)
                run("adb", "disconnect", tls)
                print(f"reconnected {ip} after a restart (wireless debugging {tls} -> 5555)", flush=True)
        except Exception as e:  # never let the loop die
            print("reconnect loop:", type(e).__name__, e, flush=True)


def scan() -> list[str]:
    names = json.load(open(NAMES_FILE)) if os.path.exists(NAMES_FILE) else {}
    found = []
    for line in run("nmap", "-p5555", "--open", "-T4", "-oG", "-", SUBNET, timeout=120).splitlines():
        m = re.match(r"Host: (\S+) \(([^)]*)\).*5555/open", line)
        if m:
            name = names.get(m.group(1)) or m.group(2)
            found.append(f"{m.group(1)} ({name})" if name else m.group(1))
    # phones that are already connected are always listed: a sleeping phone can miss nmap's probe
    have = {x.split(" ")[0] for x in found}
    for d in connected():
        if d["ip"] not in have:
            try:
                host = names.get(d["ip"]) or socket.gethostbyaddr(d["ip"])[0]
            except OSError:
                host = ""
            found.append(f"{d['ip']} ({host})" if host else d["ip"])
    return sorted(found, key=lambda x: tuple(int(p) for p in x.split(" ")[0].split(".")))


def with_streams(devices: list[dict]) -> list[dict]:
    """Keep the devices that have a stream to show, each with the URL of its player (served by adb-stream)."""
    if not STREAM_URL:
        return []
    try:
        req = urllib.request.Request(f"{STREAM_URL}/streams", headers={"X-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return []
    if not data.get("on"):
        return []
    streams = data.get("streams", {})
    known = json.load(open(KNOWN_FILE)) if os.path.exists(KNOWN_FILE) else {}
    here = {d["ip"] for d in devices}
    devices = devices + [{"ip": ip, "state": "device", **known[ip], "landscape": orient.get(ip, known[ip]["tablet"]),
                          "shown": True} for ip in streams if ip not in here and ip in known]
    return [{**d, "player": f"{STREAM_URL}/play?src={streams[d['ip']]['src']}"}
            for d in devices if streams.get(d["ip"], {}).get("video")]


def ip_of(value) -> str:
    ip = str(value or "").split(" ")[0]
    return ip if IP_RE.match(ip) else ""


def status() -> dict:
    devs = connected()
    sel = state["selected"]
    return {"is_on": any(d["ip"] == sel and d["state"] == "device" for d in devs), "selected": sel, "connected": devs}


VIEW = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ADB screens</title><style>
:root{color-scheme:dark}html,body{margin:0;height:100%;overflow:hidden;background:#111;color:#ddd;font:14px system-ui,sans-serif}
#wrap{display:flex;justify-content:center}
#rows{display:flex;flex-direction:column;align-items:center;gap:14px;padding:10px;transform-origin:top center}
.row{display:flex;gap:16px;align-items:flex-start;justify-content:space-between}
.phone{display:flex;flex-direction:column;align-items:center;gap:6px}.phone b{font-weight:600;white-space:nowrap}
iframe{border:0;background:#000;border-radius:10px}.empty{padding:40px;color:#888;text-align:center}
</style></head><body><div id="wrap"><div id="rows"><div class="empty">Cargando…</div></div></div><script>
const VH=__VIEW_HEIGHT__,  // kept for the page template; sizes below use MINH
  H=location.hostname, SP=__SCRCPY_PORT__, STREAM=new URLSearchParams(location.search).get("mode")==="stream",
  PROXY=location.protocol==="https:",  // opened through the https tunnel: everything goes through this page's own address
  // the key goes into the frames' paths, so their relative files and WebSockets carry it too (no cookies needed)
  KEY=(k=>k?encodeURIComponent(k)+"/":"")(new URLSearchParams(location.search).get("key"));
let shown="";
function link(udid){
 const base=PROXY ? `${location.origin}/scrcpy/${KEY}` : `http://${H}:${SP}/`;
 const ws=base.replace(/^http/,"ws")+`?action=proxy-adb&remote=tcp%3A8886&udid=${encodeURIComponent(udid)}`;
 return base+"#!"+new URLSearchParams({action:"stream",udid,player:"mse",ws,fitToScreen:"true"}).toString();}
const player=d=>PROXY ? `/stream/${KEY}`+d.player.split("/").slice(3).join("/") : d.player;
function fit(){  // scale everything down (never up) so every screen fits the card without scrollbars
 const rows=document.getElementById("rows"); rows.style.transform="none";
 const s=Math.min(1, innerWidth/rows.scrollWidth, innerHeight/rows.scrollHeight);
 rows.style.transform=`scale(${s})`;}
const GAP=16;  // minimum space between screens (.row gap)
const FRAME=STREAM?0:60;  // iframe width = video width + FRAME (ws-scrcpy's 59.44 px button column, rounded up)
const MINH=416;  // minimum video height: fits ws-scrcpy's button column without scrollbars
// ws-scrcpy sizes the video to its frame rounded DOWN to multiples of 16 px (minus the button column), so every
// video box here is a multiple of 16 in both directions: then the picture fills the frame with no black margins.
const S16=x=>Math.max(16,Math.round(x/16)*16), F16=x=>Math.max(16,Math.floor(x/16)*16);
const videoW=(d,h)=>d.landscape ? h*d.aspect : h/d.aspect;     // video width for a given height
const heightFor=(d,w)=>d.landscape ? w/d.aspect : w*d.aspect;  // video height for a given width
function boxFor(d,w,floor){  // video box for a wanted width: width and height snapped to 16, height >= MINH
 let vw=(floor?F16:S16)(w), vh=S16(heightFor(d,vw));
 if(vh<MINH){vh=MINH; vw=S16(videoW(d,MINH));}
 return {d,vw,vh};}
const rowWidth=bs=>bs.reduce((s,b)=>s+b.vw+FRAME,0)+GAP*(bs.length-1);
function screen(b){
 const box=document.createElement("div");box.className="phone";
 const t=document.createElement("b");t.textContent=(b.d.name||b.d.model||"Android")+" · "+b.d.ip;
 const f=document.createElement("iframe");f.src=STREAM ? player(b.d) : link(b.d.ip+":5555");
 const W=b.vw+FRAME, Hh=b.vh;
 // ws-scrcpy keeps the video size a shared stream started with and only refits on a resize; so start 2 px smaller
 // and grow to the final size once the stream is up, which makes it re-apply the bounds of this frame.
 f.width=W-2;f.height=Hh-2;f.allow="autoplay; fullscreen";
 f.addEventListener("load",()=>setTimeout(()=>{f.width=W;f.height=Hh;},STREAM?0:4000));
 box.append(t,f);return box;}
function render(rows,list){
 // Columns: the first row is the reference (height MINH). In the rows below, each screen takes the width of the screen
 // above it (height from its own aspect ratio) and the last one fills the rest. Every row is laid out at the same
 // width with the screens pushed to both edges, so the snapping leftovers go into the gaps and the edges line up.
 const first=list[0].map(d=>boxFor(d,videoW(d,MINH)));
 const natural=list.slice(1).map(r=>r.map(d=>boxFor(d,videoW(d,MINH))));
 const target=Math.max(rowWidth(first),...natural.map(rowWidth));
 list.forEach((r,ri)=>{
  let bs;
  if(ri===0) bs=first;
  else if(r.length===first.length){ bs=[]; let used=0;  // full row: same column widths, the last one fills
   r.forEach((d,i)=>{
    const b=(i<r.length-1 && first[i]) ? boxFor(d,first[i].vw) : boxFor(d,target-used-FRAME-GAP*i,true);
    used+=b.vw+FRAME; bs.push(b);});}
  else if(r.length===1 && r[0].landscape)  // a landscape screen alone in a row spans the full width of the rows
   bs=[boxFor(r[0],target-FRAME,true)];
  else  // a shorter row otherwise: each screen takes the width of the column of its own orientation
   bs=r.map(d=>{const above=first.find(b=>b.d.landscape===d.landscape); return above ? boxFor(d,above.vw) : boxFor(d,videoW(d,MINH));});
  const row=document.createElement("div");row.className="row";
  row.style.width=target+"px";
  if(bs.length===1 && bs[0].d.landscape && ri>0) row.style.justifyContent="center";  // full-width screen: centre the 16-px snap leftover
  else if(bs.length===1 && first.length>1) row.style.justifyContent = bs[0].d.landscape===first[first.length-1].d.landscape ? "flex-end" : "flex-start";
  else if(bs.length===1) row.style.justifyContent="center";
  bs.forEach(b=>row.append(screen(b)));
  rows.append(row);});}
async function refresh(){try{
 const data=await (await fetch("/viewdata"+location.search)).json();
 const devs=data.devices.filter(d=>d.state==="device" && d.shown);  // hidden screens stay connected, just not shown
 const key=JSON.stringify(data.layout)+devs.map(d=>d.ip+(d.landscape?"L":"P")).join(",");
 if(key===shown)return; shown=key;  // only rebuild when the devices or their orientation change, so streams are not restarted
 const rows=document.getElementById("rows"); rows.replaceChildren();
 if(!devs.length){const e=document.createElement("div");e.className="empty";e.textContent=STREAM ? "Stream apagado o ningún vídeo encendido." : "Ningún dispositivo conectado. Elige uno en «Dispositivo» y enciende ADB Control.";rows.append(e);fit();return;}
 // Preferred order = layout.json read row by row (devices not in it go last). Each row pairs the next portrait screen
 // (left) with the next landscape one, so a missing device lets the next of its kind move up and every row gets a
 // landscape screen while there are any. Leftovers of one kind fill further rows two at a time.
 const order=[...(data.layout||[]).flat(), ...devs.map(d=>d.ip)];
 const seen=new Set(), ordered=[];
 for(const ip of order){const d=devs.find(x=>x.ip===ip); if(d && !seen.has(ip)){seen.add(ip); ordered.push(d);}}
 const port=ordered.filter(d=>!d.landscape), land=ordered.filter(d=>d.landscape), list=[];
 const pairs=Math.min(port.length,land.length);
 for(let i=0;i<pairs;i++) list.push([port[i],land[i]]);
 const left=[...port.slice(pairs),...land.slice(pairs)];
 for(let i=0;i<left.length;i+=2) list.push(left.slice(i,i+2));
 render(rows,list);
 fit();
}catch(e){}}
addEventListener("resize",fit);
refresh();setInterval(refresh,8000);
</script></body></html>"""


PROXY_PREFIXES = ("/scrcpy/", "/stream/")


def splice(handler: BaseHTTPRequestHandler, host: str, port: int, path: str) -> None:
    """Forward this request to host:port with a new path, then copy bytes both ways until either side closes
    (a WebSocket stays open; a plain request ends with the response, since upstream is asked to close)."""
    body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
    upgrade = handler.headers.get("Upgrade", "").lower() == "websocket"
    head = [f"{handler.command} {path} HTTP/1.1", f"Host: {host}:{port}",
            "Connection: Upgrade" if upgrade else "Connection: close"]
    head += [f"{k}: {v}" for k, v in handler.headers.items() if k.lower() not in ("host", "connection", "origin")]
    try:
        up = socket.create_connection((host, port), timeout=10)
    except OSError:
        return handler._reply(502, {"error": "upstream unreachable"})
    up.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    up.settimeout(None)

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=pump, args=(handler.connection, up), daemon=True).start()
    pump(up, handler.connection)
    up.close()
    handler.close_connection = True


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body, ctype: str = "application/json", extra=None) -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        if extra:
            extra()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self) -> bool:
        if self.headers.get("X-Token") != TOKEN:
            self._reply(403, {"error": "forbidden"})
            return False
        return True

    def _remote_ok(self) -> bool:
        """Through Cloudflare (it adds Cf-Connecting-Ip), require the key: in ?key=, in the frames' own path
        (/scrcpy/<key>/..., /stream/<key>/...; app WebViews send no cookies to frames) or in the cookie /view sets."""
        if not self.headers.get("Cf-Connecting-Ip"):
            return True
        key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
        parts = urlparse(self.path).path.split("/")
        if len(parts) > 2 and "/" + parts[1] + "/" in PROXY_PREFIXES and parts[2] == REMOTE_KEY:
            key = parts[2]
        cookie = re.search(r"(?:^|;\s*)adb_key=([^;]+)", self.headers.get("Cookie", ""))
        if REMOTE_KEY and REMOTE_KEY in (key, cookie and cookie.group(1)):
            return True
        self._reply(403, {"error": "forbidden"})
        return False

    def _proxy(self) -> bool:
        """/scrcpy/... -> ws-scrcpy, /stream/... -> Stream Control (plain HTTP and WebSocket), byte for byte."""
        path = urlparse(self.path).path
        for prefix, target in (("/scrcpy/", f"http://127.0.0.1:{SCRCPY_PORT}"), ("/stream/", STREAM_URL)):
            if path.startswith(prefix) and target:
                rest = self.path[len(prefix):]
                if REMOTE_KEY and rest.startswith(REMOTE_KEY + "/"):
                    rest = rest[len(REMOTE_KEY) + 1:]  # the key in the path is for us, not for the service behind
                u = urlparse(target)
                splice(self, u.hostname, u.port or 80, "/" + rest)
                return True
        return False

    def _send_key_cookie(self) -> None:
        key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
        if key and key == REMOTE_KEY:
            self.send_header("Set-Cookie", f"adb_key={key}; Path=/; Secure; HttpOnly; SameSite=Lax")

    def do_GET(self):
        if not self._remote_ok() or self._proxy():
            return
        if urlparse(self.path).path == "/view":
            page = VIEW.replace("__VIEW_HEIGHT__", str(VIEW_HEIGHT)).replace("__SCRCPY_PORT__", str(SCRCPY_PORT))
            return self._reply(200, page, "text/html; charset=utf-8", self._send_key_cookie)
        if urlparse(self.path).path == "/viewdata":  # what the page needs: ip, state, model, aspect, landscape; the row layout
            layout = json.load(open(LAYOUT_FILE)) if os.path.exists(LAYOUT_FILE) else []
            devices = connected()
            if "mode=stream" in self.path:  # only devices with a stream whose Video switch is on, while Stream is on
                devices = with_streams(devices)
            return self._reply(200, {"devices": devices, "layout": layout})
        if not self._ok():
            return
        if self.path.startswith("/display"):
            ip = ip_of(parse_qs(urlparse(self.path).query).get("ip", [""])[0])
            return self._reply(200, {"is_on": ip not in hidden})
        if self.path.startswith("/orientation"):
            ip = ip_of(parse_qs(urlparse(self.path).query).get("ip", [""])[0])
            if ip not in orient:  # nothing saved: same default as the page (tablets landscape, phones portrait)
                tablet = any(i["tablet"] for s, i in INFO.items() if s.split(":")[0] == ip)
                return self._reply(200, {"is_on": tablet})
            return self._reply(200, {"is_on": orient[ip]})
        if self.path == "/status":
            return self._reply(200, status())
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._remote_ok() or self._proxy() or not self._ok():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/scan":
            return self._reply(200, {"devices": scan()})
        if self.path == "/display":
            ip = ip_of(body.get("ip"))
            if not ip:
                return self._reply(400, {"error": "ip must be an IPv4 address"})
            set_shown(ip, bool(body.get("show")))
            return self._reply(200, {"is_on": ip not in hidden})
        if self.path == "/orientation":
            ip = ip_of(body.get("ip"))
            if not ip:
                return self._reply(400, {"error": "ip must be an IPv4 address"})
            set_landscape(ip, bool(body.get("landscape")))
            return self._reply(200, {"is_on": orient[ip]})
        if self.path == "/select":
            select(ip_of(body.get("ip")))
            return self._reply(200, status())
        if self.path == "/switch":  # HA REST switch: {"ip": <selected>, "on": true|false}
            ip = ip_of(body.get("ip"))
            if not ip:
                return self._reply(400, {"error": "choose a device first"})
            select(ip)
            self.path = "/connect" if body.get("on") else "/disconnect"
        if self.path == "/disconnect":
            ip = ip_of(body.get("ip"))
            run("adb", "disconnect", f"{ip}:5555") if ip else run("adb", "disconnect")
            set_wanted(ip or None, False)
            return self._reply(200, status())
        if self.path == "/connect":
            ip = ip_of(body.get("ip"))
            if not ip:
                return self._reply(400, {"error": "ip must be an IPv4 address"})
            out = run("adb", "connect", f"{ip}:5555").strip()
            set_wanted(ip, True)
            return self._reply(200, {**status(), "adb": out})
        if self.path == "/pair":  # {"target": "ip:port", "code": "123456"} from the device's «pair with code» screen
            target, code = str(body.get("target", "")).strip(), str(body.get("code", "")).strip()
            if not PAIR_TARGET.fullmatch(target) or not re.fullmatch(r"\d{6}", code):
                return self._reply(400, {"error": "target must be ip:port and code 6 digits"})
            out = run("adb", "pair", target, code, timeout=30).strip()
            return self._reply(200, {"ok": "Successfully paired" in out, "adb": out[-300:]})
        self._reply(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=reconnect_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
