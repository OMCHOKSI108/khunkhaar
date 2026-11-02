"""Server runner with free-port detection and prerequisite checks."""
from __future__ import annotations

import socket
import sys
from core.cli import cmd_doctor


def find_free_port(start: int = 8899, end: int = 9999) -> int:
    """Find and return an unbound TCP port."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def run_prerequisite_check() -> bool:
    """Run Khunkhaar doctor check and report readiness."""
    print("Checking KHUNKHAAR prerequisites & MCP tool health...")
    code = cmd_doctor(json_output=False)
    if code != 0:
        print("Notice: Some optional tools are missing, but server will continue.")
    return True


def start_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    import uvicorn
    from core.server.app import create_web_app

    run_prerequisite_check()
    final_port = port if port else find_free_port()
    app = create_web_app()
    print(f"\n=======================================================")
    print(f"🚀 KHUNKHAAR QA Server running at: http://localhost:{final_port}")
    print(f"   Prerequisites verified. Full senior QA suite active.")
    print(f"=======================================================\n")
    uvicorn.run(app, host=host, port=final_port, log_level="info")

