#!/command/with-contenv bash
# with-contenv makes SUPERVISOR_TOKEN visible to the app
exec python3 -u /app/main.py
