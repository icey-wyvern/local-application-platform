#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess  # noqa: S404 - fixed local Git fixture commands only.
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 1024 * 1024


def run_git(*arguments: str, cwd: Path) -> None:
    subprocess.run(  # noqa: S603 - arguments are controlled fixture values.
        ["git", *arguments],  # noqa: S607 - Git is the intentional PATH tool.
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def create_repository(fixtures: Path, root: Path, name_suffix: str = "") -> Path:
    work = root / "work"
    work.mkdir()
    run_git("init", "--initial-branch=success", cwd=work)
    run_git("config", "user.name", "LAP validation", cwd=work)
    run_git("config", "user.email", "validation@invalid.example", cwd=work)
    branches = (
        "success",
        "success-v2",
        "unhealthy",
        "invalid-config",
        "oversized-config",
        "failed-build",
        "vulnerable",
    )
    for index, branch in enumerate(branches):
        if index:
            run_git("checkout", "--orphan", branch, cwd=work)
            for tracked in work.iterdir():
                if tracked.name != ".git":
                    if tracked.is_dir():
                        shutil.rmtree(tracked)
                    else:
                        tracked.unlink()
        source_branch = "success" if branch == "oversized-config" else branch
        for source in (fixtures / source_branch).iterdir():
            if source.is_dir():
                shutil.copytree(source, work / source.name)
            else:
                shutil.copy2(source, work / source.name)
        if branch == "oversized-config":
            platform_config = work / "platform.yaml"
            platform_config.write_text(platform_config.read_text() + "#" * (17 * 1024) + "\n")
        if name_suffix and branch != "invalid-config":
            platform_config = work / "platform.yaml"
            lines = platform_config.read_text(encoding="utf-8").splitlines()
            name = lines[0].removeprefix("name: ")
            lines[0] = f"name: {name}-{name_suffix}"
            platform_config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        run_git("add", ".", cwd=work)
        run_git("commit", "--message", f"{branch} fixture", cwd=work)
    repository_name = f"fixture-{name_suffix}.git" if name_suffix else "fixture.git"
    bare = root / repository_name
    run_git("clone", "--bare", str(work), str(bare), cwd=root)
    run_git("config", "http.receivepack", "false", cwd=bare)
    return bare


class GitHandler(BaseHTTPRequestHandler):
    server_version = "LAPFixture/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        self._serve_git()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        self._serve_git()

    def _serve_git(self) -> None:
        parsed = urlsplit(self.path)
        server = self.server
        if not isinstance(server, FixtureServer):
            self.send_error(500)
            return
        if parsed.path == "/redirect-private.git/info/refs":
            self.send_response(302)
            self.send_header("Location", "https://169.254.169.254/metadata.git/info/refs")
            self.end_headers()
            return
        if not parsed.path.startswith(f"/{server.repository_name}/"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_REQUEST_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else b""
        environment = {
            **os.environ,
            "GIT_PROJECT_ROOT": str(server.repository_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "REMOTE_ADDR": self.client_address[0],
        }
        completed = subprocess.run(  # noqa: S603 - request data is only standard input.
            ["git", "http-backend"],  # noqa: S607 - Git is the intentional PATH tool.
            input=body,
            env=environment,
            capture_output=True,
            check=False,
        )
        header_block, separator, response_body = completed.stdout.partition(b"\r\n\r\n")
        if not separator:
            self.send_error(500)
            return
        status = 200
        headers: list[tuple[str, str]] = []
        for line in header_block.decode("ascii").split("\r\n"):
            name, value = line.split(":", 1)
            if name.lower() == "status":
                status = int(value.strip().split(" ", 1)[0])
            else:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args: object) -> None:
        return


class FixtureServer(ThreadingHTTPServer):
    repository_root: Path
    repository_name: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--name-suffix",
        default="",
        help="DNS-label suffix used to make disposable application identities unique",
    )
    arguments = parser.parse_args()
    if arguments.name_suffix and not re.fullmatch(r"[a-z0-9]{1,12}", arguments.name_suffix):
        parser.error("--name-suffix must contain 1-12 lowercase letters or digits")
    with tempfile.TemporaryDirectory(prefix="lap-build-agent-git-") as temporary:
        root = Path(temporary)
        repository = create_repository(arguments.fixtures.resolve(), root, arguments.name_suffix)
        server = FixtureServer(("127.0.0.1", arguments.port), GitHandler)
        server.repository_root = root
        server.repository_name = repository.name
        print(
            f"http://127.0.0.1:{arguments.port}/{server.repository_name}",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()


if __name__ == "__main__":
    main()
