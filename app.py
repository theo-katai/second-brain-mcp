"""Minimal MCP connector for a GitHub-backed markdown vault: capture + read_file over the GitHub
Contents API, search + list_files over a shallow markdown-only clone."""

import asyncio
import base64
import os
import shutil
import urllib.parse

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # message reaches the model; other exceptions don't
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

GITHUB_REPO = os.environ["GITHUB_REPO"]  # "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
BEARER_TOKEN = os.environ["BEARER_TOKEN"]
INBOX_PATH = os.environ.get("INBOX_PATH", "Inbox.md")
CAPTURE_HEADING = os.environ.get("CAPTURE_HEADING", "## To process")
VAULT_DIR = os.environ.get("VAULT_DIR", "/tmp/vault")

MAX_LINE_CHARS = 300  # long lines get cut so one huge paragraph can't flood the context
SEARCH_MAX_LINES = 200
LIST_MAX_LINES = 300
READ_DEFAULT_LINES = 200
GIT_TIMEOUT = 30  # seconds; covers a cold-start clone, generous for grep/ls-files on a markdown-only vault

GITHUB_API = "https://api.github.com"
_headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Auth via env-injected git config: keeps the token out of argv, .git/config and error messages.
_basic = base64.b64encode(f"x-access-token:{GITHUB_TOKEN}".encode()).decode()
_GIT_AUTH_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "http.extraHeader",
    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {_basic}",
    "GIT_CONFIG_KEY_1": "core.quotePath",  # print "Théo.md", not "Th\303\251o.md"
    "GIT_CONFIG_VALUE_1": "false",
}
# Guards both the clone/fetch/reset AND the grep/ls-files that read its result, so a concurrent
# reset can't rewrite files out from under an in-flight read. Readers serialize behind each other
# too as a result — the simple, correct tradeoff for a personal/small-scale deployment.
_sync_lock = asyncio.Lock()


async def github_get_file(path: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}",
            headers=_headers,
            params={"ref": GITHUB_BRANCH},
        )
    resp.raise_for_status()
    return resp.json()


async def github_put_file(path: str, content: str, sha: str, message: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}",
            headers=_headers,
            json={
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "sha": sha,
                "branch": GITHUB_BRANCH,
            },
        )
    resp.raise_for_status()


async def git(*args: str, cwd: str = VAULT_DIR) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env={**os.environ, **_GIT_AUTH_ENV},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"git {args[0]} timed out after {GIT_TIMEOUT}s — try a narrower pattern or path_glob")
    except asyncio.CancelledError:
        proc.kill()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def git_ok(*args: str, cwd: str = VAULT_DIR) -> str:
    code, out, err = await git(*args, cwd=cwd)
    if code != 0:
        raise ToolError(f"git {args[0]} failed: {err.strip()}")
    return out


async def _sync_vault_locked() -> None:
    """Shallow, blobless, markdown-only checkout of the vault, refreshed so a capture made a
    second ago is already searchable. Images and other attachments are never downloaded, but
    `git ls-files` still lists them. Caller must already hold `_sync_lock`."""
    first_time = not os.path.isdir(os.path.join(VAULT_DIR, ".git"))
    if first_time:
        try:
            await git_ok(
                "clone", "--depth=1", "--filter=blob:none", "--no-checkout",
                f"--branch={GITHUB_BRANCH}", f"https://github.com/{GITHUB_REPO}.git", VAULT_DIR,
                cwd="/",
            )
            await git_ok("sparse-checkout", "set", "--no-cone", "*.md")
        except Exception:
            # a half-finished clone would fool the `.git`-exists check above forever; start clean next time
            shutil.rmtree(VAULT_DIR, ignore_errors=True)
            raise
    await git_ok("fetch", "--depth=1", "origin", GITHUB_BRANCH)
    # ponytail: skip the reset (otherwise unconditional on every call) when nothing actually moved —
    # but never on first_time: --no-checkout + sparse-checkout set alone leave the tree unmaterialized,
    # only reset --hard actually populates it
    if first_time or (await git_ok("rev-parse", "FETCH_HEAD")).strip() != (await git_ok("rev-parse", "HEAD")).strip():
        await git_ok("reset", "--hard", "--quiet", "FETCH_HEAD")


def cap_lines(text: str, limit: int, hint: str) -> str:
    """Cut each line to MAX_LINE_CHARS and the whole text to `limit` lines, saying what was dropped."""
    lines = [l if len(l) <= MAX_LINE_CHARS else l[:MAX_LINE_CHARS] + "…" for l in text.splitlines()]
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit]) + f"\n… {len(lines) - limit} more lines cut; {hint}."


def number_lines(text: str, start: int, end: int) -> str:
    """Lines start..end (1-based, inclusive) prefixed with their line number, plus a footer when
    the file goes on, so the agent knows where to continue. Lines are never cut here: this is
    where the agent reads a paragraph in full."""
    lines = text.splitlines()
    start = max(start, 1)
    if end < start:
        raise ToolError(f"end ({end}) must be ≥ start ({start})")
    if start > len(lines):
        return f"(file has only {len(lines)} lines)"
    body = "\n".join(f"{i}: {l}" for i, l in enumerate(lines[start - 1 : end], start))
    if end < len(lines):
        body += f"\n… file has {len(lines)} lines; continue with start={end + 1}."
    return body


def insert_into_inbox(markdown: str, bullet_line: str, heading: str = CAPTURE_HEADING) -> str:
    """Insert `bullet_line` into the `heading` section, just above its trailing empty bullet.

    Keeps the heading and the trailing empty bullet intact, so the file stays ready for the
    next capture (matches the convention from killian31/second_brain_template's Inbox.md).
    """
    lines = markdown.splitlines()
    try:
        heading_idx = lines.index(heading)
    except ValueError:
        raise ValueError(f'{INBOX_PATH} has no {heading!r} heading')

    next_heading_idx = next(
        (i for i in range(heading_idx + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    section = lines[heading_idx + 1 : next_heading_idx]

    empty_bullet_idx = next(
        (i for i in range(len(section) - 1, -1, -1) if section[i].strip() == "-"),
        None,
    )
    if empty_bullet_idx is None:
        section.append(bullet_line)
        section.append("-")
    else:
        section.insert(empty_bullet_idx, bullet_line)

    lines[heading_idx + 1 : next_heading_idx] = section
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


def demo() -> None:
    """ponytail: the one runnable check for insert_into_inbox's line-splicing logic."""
    sample = (
        "# Inbox\n\n"
        "## To process\n"
        "- existing item\n"
        "-\n"
        "<!-- comment -->\n\n"
        "## For the agent\n"
        "stuff\n"
    )
    out = insert_into_inbox(sample, "- new capture", "## To process")
    expected = (
        "# Inbox\n\n"
        "## To process\n"
        "- existing item\n"
        "- new capture\n"
        "-\n"
        "<!-- comment -->\n\n"
        "## For the agent\n"
        "stuff\n"
    )
    assert out == expected, f"mismatch:\n{out!r}\nvs\n{expected!r}"

    # no trailing empty bullet present: one is re-added after the new line
    sample2 = "## To process\n- a\n## For the agent\n"
    out2 = insert_into_inbox(sample2, "- b", "## To process")
    assert out2 == "## To process\n- a\n- b\n-\n## For the agent\n", out2

    text = "\n".join(f"line {i}" for i in range(1, 11))
    assert number_lines(text, 3, 4) == "3: line 3\n4: line 4\n… file has 10 lines; continue with start=5."
    assert number_lines(text, 9, 50) == "9: line 9\n10: line 10"
    assert number_lines(text, 11, 20) == "(file has only 10 lines)"

    for bad_start, bad_end in [(50, 10), (1, -1)]:
        try:
            number_lines(text, bad_start, bad_end)
            raise AssertionError(f"expected ToolError for start={bad_start}, end={bad_end}")
        except ToolError:
            pass

    assert cap_lines("a\nb\nc", 2, "narrow it") == "a\nb\n… 1 more lines cut; narrow it."
    assert cap_lines("x" * 400, 5, "") == "x" * MAX_LINE_CHARS + "…"

    print("demo: ok")


mcp = MCPServer("second-brain")


@mcp.tool()
async def capture(text: str, actionable: bool = False) -> str:
    """Append `text` as a bullet to the vault's inbox file, under its capture heading. No
    frontmatter, no filing decision, no question — even if the text is vague. Set
    actionable=True only when the text is unambiguously a to-do, so it's written as
    `- [ ] text` instead of a plain bullet. Never use this to file into a knowledge base,
    ideas folder, or project note — that defeats the point of a zero-friction inbox."""
    bullet = f"- [ ] {text}" if actionable else f"- {text}"
    file = await github_get_file(INBOX_PATH)
    try:
        content = base64.b64decode(file["content"]).decode()
    except UnicodeDecodeError:
        raise ToolError(f"{INBOX_PATH} isn't valid UTF-8 text — can't safely append to it")
    updated = insert_into_inbox(content, bullet)
    await github_put_file(INBOX_PATH, updated, file["sha"], f"capture: {text[:60]}")
    return f"Captured to {INBOX_PATH}: {bullet}"


@mcp.tool()
async def search(pattern: str, path_glob: str = "", context: int = 2, ignore_case: bool = True) -> str:
    """Grep the vault's markdown files with an extended regex (`git grep -E`). Matches come back
    as `path:line:text`, surrounding context lines as `path-line-text`. Use it to find where
    something is, then read_file(path, start, end) on just those lines instead of whole files.
    Narrow with path_glob, e.g. "Projects/**". Notes carry a one-line `summary:` in their
    frontmatter, so pattern "^summary:.*<topic>" with context=0 is the fastest way to see which
    notes exist on a topic."""
    args = ["grep", "-n", "-I", "-E", "--no-color", f"-C{min(max(context, 0), 10)}"]
    if ignore_case:
        args.append("-i")
    args += ["-e", pattern, "--"]
    if path_glob:
        args.append(f":(glob){path_glob}")
    async with _sync_lock:
        await _sync_vault_locked()
        code, out, err = await git(*args)
    if code == 1:
        return f"No matches for {pattern!r}."
    if code != 0:
        raise ToolError(err.strip())  # e.g. an invalid regex, so the model can fix it
    return cap_lines(out, SEARCH_MAX_LINES, "narrow the pattern or path_glob")


@mcp.tool()
async def list_files(path_glob: str = "**") -> str:
    """List the paths of files in the vault matching a glob, e.g. "Projects/**", "**/*Solal*",
    "*.md". Lists every tracked file, attachments included, not just markdown."""
    async with _sync_lock:
        await _sync_vault_locked()
        out = await git_ok("ls-files", "--", f":(glob){path_glob}")
    return cap_lines(out, LIST_MAX_LINES, "narrow path_glob") or f"No files match {path_glob!r}."


@mcp.tool()
async def read_file(path: str, start: int = 1, end: int = 0) -> str:
    """Read a file in the vault by its repo-relative path (e.g. "AGENTS.md", "Knowledge/Some
    Note.md"). Returns lines start..end (1-based, inclusive), each prefixed with its line
    number. Without `end`, returns at most 200 lines and says how long the file is: use
    search first, then read only the lines you need. A directory returns its listing."""
    try:
        data = await github_get_file(path)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ToolError(f"{path} not found; find the right path with list_files or search")
        raise ToolError(f"GitHub API error reading {path}: {e.response.status_code}")
    if isinstance(data, list):
        names = "\n".join(f"{e['type']}: {e['name']}" for e in data)
        return cap_lines(names, LIST_MAX_LINES, "narrow to a subpath") or f"{path} is an empty directory."
    # ponytail: contents API caps at ~1MB; switch to the git blobs API if a file ever exceeds that
    try:
        text = base64.b64decode(data["content"]).decode()
    except UnicodeDecodeError:
        raise ToolError(f"{path} isn't valid UTF-8 text (likely a binary attachment) — can't display it")
    return number_lines(text, start, end or max(start, 1) + READ_DEFAULT_LINES - 1)


mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    # ponytail: this is a server-to-server API behind our own bearer check, not a browser target,
    # so DNS-rebinding Host-header allowlisting (meant for browser-reachable local servers) doesn't apply
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"] == "/health":
        resp = await health(Request(scope, receive))
        return await resp(scope, receive, send)
    if scope["type"] == "http":
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {BEARER_TOKEN}":
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await resp(scope, receive, send)
    await mcp_app(scope, receive, send)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
