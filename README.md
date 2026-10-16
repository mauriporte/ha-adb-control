# ADB Control for Home Assistant

See and control the screens of several Android devices from a Home Assistant dashboard, side by side, over Wi-Fi.
It is a free alternative to tools like Vysor, built on [ws-scrcpy](https://github.com/NetrisTV/ws-scrcpy) (scrcpy in
the browser) plus a small control service that Home Assistant talks to.

Video and touch only (no audio). It is meant for your own devices on your home network. For audio, see the companion
project [Stream Control](https://github.com/mauriporte/ha-stream-control), which can share this viewer's layout.

## What you get

- **Scan** the network for devices with ADB over Wi-Fi (port 5555) and pick one in a selector.
- **ADB Control** switch: connect or disconnect the selected device. Several devices can be connected at once.
- **Viewer** (`/view`): every connected screen side by side, each sized from the device's own resolution:
  - all screens share a standard height, and tablets are shown in landscape;
  - rows pair a portrait screen with a landscape one, and the screens in each column line up;
  - a missing device lets the next one of its kind move up;
  - sizes are snapped to the 16-px grid ws-scrcpy uses, so the picture fills its frame with no black margins.
- Per device, optional switches:
  - **horizontal:** a landscape frame for apps that run in landscape (the device itself is not rotated);
  - **show/hide:** the device stays connected but leaves the viewer.
- Touch, keys (back, home, recents, power, volume) and keyboard through ws-scrcpy's own buttons.
- Dashboard: a panel view with one card per device (its own **ADB Control** switch on top, its other switches while
  it is connected), then the screens. The scan and the device selector remain available for finding new devices.

## How it works

```
Home Assistant ──REST──▶ control.py :8002 ──adb──▶ devices (Wi-Fi, port 5555)
dashboard iframe ──────▶ /view (screens) ──▶ ws-scrcpy :8001 ──▶ scrcpy server on each device
```

One container runs the adb server, ws-scrcpy and `control.py`. The control API needs an `X-Token` header; the
viewer page and ws-scrcpy itself have **no authentication**, so keep ports 8001/8002 on your LAN only.

## Install

1. Build and run the container (host network so adb can reach the devices):

   ```sh
   docker build -t adb-control .
   docker run -d --name adb-control --restart unless-stopped --network host \
     -e ADB_CONTROL_TOKEN=<a long random string> -e ADB_SCAN_SUBNET=192.168.1.0/24 \
     -v /path/to/adb-data:/root/.android \
     adb-control
   ```

   `/root/.android` keeps the adb key (so devices stay authorized) and the viewer settings.

2. Home Assistant:
   - add `adb_control_token` to `secrets.yaml`;
   - merge [`examples/home-assistant.yaml`](examples/home-assistant.yaml) into your configuration and restart;
   - add the view in [`examples/dashboard.yaml`](examples/dashboard.yaml) to a dashboard.

## Preparing each device

ADB over Wi-Fi on port 5555 has to be enabled once per boot.

- **Android 11+ (no cable):** Developer options → **Wireless debugging** → *Pair device with pairing code*, then from
  any computer with a recent adb:

  ```sh
  adb pair <ip>:<pairing port> <code>
  adb connect <ip>:<connection port>    # the port shown on the Wireless debugging screen
  adb -s <ip>:<connection port> tcpip 5555
  ```

- **Older Android:** enable USB debugging, connect it by USB once and run `adb tcpip 5555`.
- **Xiaomi / POCO / Redmi:** also enable *USB debugging (Security settings)*, or you will see the screen but touches
  are ignored.
- The container's adb key must be authorized on the device: accept the prompt on the first connection, or reuse a
  key the device already trusts by placing it as `adbkey`/`adbkey.pub` in the data folder.

## Remote access (https)

Home Assistant opened over https (its app away from home, or a reverse proxy) refuses http frames. Publish this
container's port 8002 at an https address instead, for example with a Cloudflare Tunnel route
`adbcontrol.example.com → http://<host>:8002`, and use that address in the dashboard:

- `control.py` then serves everything from that one address: the viewer, ws-scrcpy under `/scrcpy/` and Stream
  Control's players under `/stream/` (HTTP and WebSocket are forwarded as they are);
- put a long random key in the data folder as `remote_key` (or env `ADB_REMOTE_KEY`) and open the viewer as
  `https://adbcontrol.example.com/view?key=<key>`: the page puts the key in its frames' paths
  (`/scrcpy/<key>/…`, `/stream/<key>/…`), so their files and WebSockets carry it too (app WebViews send no cookies
  to frames);
- requests that come through Cloudflare (they carry `Cf-Connecting-Ip`) are refused without the key; the LAN keeps
  working without it, as before. Without a `remote_key`, nothing gets in through the tunnel.

The key sits in the dashboard's configuration, so anyone who can edit your dashboards can see it.

## Optional files (in the data folder)

| File | Purpose | Example |
|---|---|---|
| `layout.json` | preferred order of devices, row by row | `[["192.168.1.21", "192.168.1.22"], ["192.168.1.23"]]` |
| `names.json` | names shown by the scan instead of network hostnames | `{"192.168.1.22": "Kitchen tablet"}` |
| `orientation.json`, `hidden.json`, `selected` | written by the switches | — |
| `remote_key` | key required for requests through the tunnel (or env `ADB_REMOTE_KEY`) | a long random string |
| `known.json` | written: each device's last name/size, so Stream Control's viewer can show it while not connected here | — |
| `stream_url` | address of [Stream Control](https://github.com/mauriporte/ha-stream-control), for `/view?mode=stream` (or env `ADB_STREAM_URL`) | `http://192.168.1.20:8005` |

## API

| Request | Effect |
|---|---|
| `GET /status` | connected devices and the state of the selected one |
| `POST /scan` | devices with port 5555 open (plus the ones already connected) |
| `POST /select {"ip"}` | device the ADB Control switch refers to |
| `POST /switch {"ip", "on"}` | connect / disconnect that device |
| `GET/POST /orientation` | landscape frame per device |
| `GET/POST /display` | show / hide per device |
| `GET /view`, `GET /viewdata` | the viewer page and its data (no token) |
| `GET /view?mode=stream` | the same viewer and layout with Stream Control's audio+video players (needs `stream_url`) |

## Limits

- ADB over Wi-Fi turns off when a device reboots.
- Some old devices accept only one adb host at a time (for example, you cannot use Vysor and this together).
- ws-scrcpy is not actively maintained; this uses its last version as is.
