# For MCP directory checks (Glama etc.): lets a reviewer run the server's
# initialize/tools handshake anywhere. Tool *calls* need the daemons
# running on a real machine with /dev/uinput — inside a container they
# fail cleanly with "daemon not running", by design.
FROM python:3.12-slim
WORKDIR /app
COPY elfkey.py elfpad.py elfmouse.py elfdeckai_mcp.py ./
CMD ["python3", "elfdeckai_mcp.py"]
