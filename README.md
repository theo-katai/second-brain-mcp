# second-brain-mcp

A minimal [MCP](https://modelcontextprotocol.io) server that lets any Claude client — iPhone, web, desktop — write to your GitHub-backed markdown vault directly, not just whatever agent happens to be running locally.

Four tools, nothing else:

- **`capture(text, actionable=False)`** — appends a bullet to your inbox file's capture section. No frontmatter, no filing decision, no question asked — zero-friction capture from wherever you're chatting.
- **`search(pattern, path_glob="", context=2, ignore_case=True)`** — greps the vault's markdown (`git grep -E`) and returns `path:line:text` matches with a few lines of context, capped at 200 lines. Searching `^summary:.*<topic>` gives one line per relevant note.
- **`list_files(path_glob="**")`** — lists file paths matching a glob (`Projects/**`, `**/*Solal*`), attachments included.
- **`read_file(path, start=1, end=0)`** — reads lines `start..end` of any file, numbered. Without `end` it returns the first 200 lines and says how long the file is, so Claude reads the part it found with `search` instead of whole files.

`capture` and `read_file` go through the [GitHub Contents API](https://docs.github.com/en/rest/repos/contents). `search` and `list_files` run on a shallow, markdown-only clone that the server makes on first use (images and other attachments are never downloaded) and refreshes before every call, so a capture is searchable a second later. The vault stays plain git — no database, no extra service to run besides this one.

## Why

If your vault lives in a local git repo and your agent only runs on your laptop, capture only works when you're at your laptop. This closes that gap: deploy it once, connect it to Claude as a custom connector, and `capture` works from any device signed into your account.

## Requirements

- A git repo (GitHub) holding your markdown vault, with an inbox file that has a heading (default `## To process`) followed by a bullet list ending in a trailing empty `-` bullet. This matches the convention from [killian31/second_brain_template](https://github.com/killian31/second_brain_template) — if your vault was bootstrapped from that template, this works out of the box.
- Somewhere to run a container with a public HTTPS URL. These instructions use [Google Cloud Run](https://cloud.google.com/run) (free tier comfortably covers personal use at `min-instances=0`), but any host that can run the `Dockerfile` works.
- Python 3.11+ (only to generate a token locally; the server itself just needs a container host).

## Setup

### 1. Fork or clone this repo

### 2. Create a GitHub fine-grained personal access token

Go to [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):

- **Resource owner**: your account
- **Repository access**: *Only select repositories* → your vault repo
- **Permissions → Repository permissions → Contents**: *Read and write*
- Generate, copy the value (`github_pat_...`)

> Fine-grained PATs require an expiration date and don't have a `gh`/API creation path — this step is manual by design on GitHub's end.

### 3. Generate a bearer token

This is the credential Claude will send on every request. Generate one yourself, don't reuse anything else:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')"
```

### 4. Deploy

Example with Cloud Run (adjust `PROJECT`/`REGION` to yours):

```bash
gcloud secrets create second-brain-mcp-github-token --data-file=- <<< "github_pat_xxx"
gcloud secrets create second-brain-mcp-bearer-token --data-file=- <<< "your-generated-token"

gcloud run deploy second-brain-mcp \
  --project PROJECT --region REGION --source . \
  --min-instances=0 --max-instances=1 --allow-unauthenticated \
  --set-secrets=GITHUB_TOKEN=second-brain-mcp-github-token:latest,BEARER_TOKEN=second-brain-mcp-bearer-token:latest \
  --set-env-vars=GITHUB_REPO=your-username/your-vault-repo
```

`--data-file=- <<<` writes the secret from stdin with no trailing newline — **don't** pipe in a value from `echo` or `print()` without stripping the newline, or the bearer check will never match (see Gotchas below).

The default compute service account needs `roles/secretmanager.secretAccessor` on both secrets:

```bash
gcloud secrets add-iam-policy-binding second-brain-mcp-github-token \
  --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
# repeat for second-brain-mcp-bearer-token
```

### 5. Connect it to Claude

In the Claude app: **Settings → Connectors → Add custom connector**

- **Server URL**: `https://your-service-url/mcp`
- **Authentication**: *No sign-in*
- Open **Request headers** → add header, name `Authorization`, value `Bearer your-generated-token`, mark **Required**

> The **Request headers** option is a beta Anthropic feature and may not be available on every account yet. If you don't see it, you're limited to OAuth for now — this server doesn't implement that (see Limitations).

Test it: ask Claude to capture something, then check your inbox file.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GITHUB_REPO` | yes | — | `owner/repo` |
| `GITHUB_TOKEN` | yes | — | fine-grained PAT, Contents read/write on that repo only |
| `BEARER_TOKEN` | yes | — | shared secret Claude sends back on every request |
| `GITHUB_BRANCH` | no | `main` | |
| `INBOX_PATH` | no | `Inbox.md` | repo-relative path |
| `CAPTURE_HEADING` | no | `## To process` | must match a line in `INBOX_PATH` exactly |
| `PORT` | no | `8080` | set by Cloud Run automatically |
| `VAULT_DIR` | no | `/tmp/vault` | where `search`/`list_files` keep their clone |

## Gotchas (hit these during the first deploy, saving you the time)

- **Trailing newlines break the bearer check.** `python3 -c "print(...)"` or `echo` add a `\n` that ends up baked into the secret, and `Authorization` header comparison is an exact string match. Use `print(..., end='')` or `printf '%s'`.
- **The MCP SDK's DNS-rebinding protection rejects your own Cloud Run hostname by default.** It's meant for browser-reachable local servers; this is a server-to-server API with no browser in the loop, so `app.py` explicitly disables it (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`). If you fork this and see `421 Invalid Host header`, that's why.
- **The GitHub Contents API caps out around 1MB per file.** Fine for notes, not for anything you'd embed as binary.

## Limitations

- Auth is a single static bearer token, not OAuth — anyone with the token can call `capture`/`read_file` on your repo. Fine for solo/small-group personal use; rotate the token (new secret version + redeploy) if it leaks.
- No write access outside `INBOX_PATH` — by design. This is meant to stay a dumb capture tool, not reinvent note-filing.
- `search` only covers `*.md` files on `GITHUB_BRANCH`. Files that are gitignored (a generated `INDEX.md`, say) aren't on GitHub, so no tool can see them.
- The first `search`/`list_files` after a cold start clones the vault (a few seconds); later calls only fetch what changed. On Cloud Run the clone lives in `/tmp`, which is memory: a 1,400-note vault takes about 4 MB.

## License

MIT — see [LICENSE](LICENSE).
