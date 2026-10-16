#!/bin/sh
# adb keys live in /root/.android (a volume) so phones stay authorized across restarts
adb start-server
python3 /opt/control.py &
printf 'server:\n  - secure: false\n    port: %s\n' "${WS_SCRCPY_PORT:-8001}" > /tmp/ws-scrcpy.yaml
WS_SCRCPY_CONFIG=/tmp/ws-scrcpy.yaml exec node index.js
