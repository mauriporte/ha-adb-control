# ws-scrcpy (Android screen in the browser) + adb + a small control API for Home Assistant.
FROM node:20-bookworm AS build
RUN git clone --depth 1 https://github.com/NetrisTV/ws-scrcpy.git /src
WORKDIR /src
RUN npm install && npm run dist && cd dist && npm install --omit=dev

FROM node:20-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends adb nmap python3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/dist /app
COPY control.py start.sh /opt/
RUN chmod +x /opt/start.sh
WORKDIR /app
CMD ["/opt/start.sh"]
