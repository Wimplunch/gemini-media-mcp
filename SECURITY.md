# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.
Use GitHub's private reporting instead: *Security* tab → *Report a vulnerability*
(GitHub Security Advisories). You should get an answer within 7 days.

Include what you found, how to reproduce it and what an attacker could do with it. Never send
real API keys; redact them.

## Supported versions

Only the latest release gets security fixes while the project is at 0.x.

## Threat model

The server runs locally as a child process of an MCP client (Claude Code, Claude Desktop …)
and acts on tool calls that a language model chooses. Tool arguments are therefore treated as
**untrusted input**: a prompt injection in a document the model reads could try to make it
read private files, spend money or overwrite data.

| Risk | Mitigation |
|---|---|
| API key leaks | Read only from `GEMINI_API_KEY` (or `OPENROUTER_API_KEY` when the OpenRouter provider is enabled); sent as `x-goog-api-key` header to Google, or as `Authorization: Bearer` header to `openrouter.ai`, only; not stored in settings objects; removed from all error messages; never written to logs, sidecars or the cost log. CI and pre-commit run gitleaks. |
| Reading arbitrary files (e.g. `~/.ssh`) and sending them to Google | Inputs must resolve (after following symlinks) to a path inside `GEMINI_MEDIA_ALLOWED_DIRS`. Default: only the output directory. Final open uses `O_NOFOLLOW`. |
| Non-image files disguised as images | Type detected from magic bytes (PNG, JPEG, WebP, HEIC/HEIF); size limit `GEMINI_MEDIA_MAX_INPUT_MB`. |
| Overwriting or deleting files | Outputs use exclusive create (`open(..., "x")`) with fresh timestamped names. The server has no delete operation. Output names are slugified (no path separators). |
| Runaway costs | Pre-call estimate; per-call, daily and monthly limits; serialized paid calls so parallel requests cannot both pass the check; videos need `confirm_cost=true`. Costs are *estimates* from public list prices. |
| Unexpected network access | Only the Google Gemini API, or only `openrouter.ai` with the opt-in OpenRouter provider (a request hook rejects every other host and non-HTTPS URL; redirects are not followed). Video downloads are restricted to `*.googleapis.com`. No telemetry (the MCP SDK ships only the no-op OpenTelemetry API). |
| Supply chain | Few direct dependencies, pinned exactly; `uv.lock` with hashes; GitHub Actions pinned to commit SHAs; Dependabot weekly. |
| Retrying a paid request twice | Retries only on connection errors and HTTP 500/502/503/504 (OpenRouter also 529) (max 2). OpenRouter 408/524 timeouts are not retried. Read timeouts are not retried because the request may already be running and billed. |

## Third-party data flow (OpenRouter, opt-in)

With the default `GEMINI_MEDIA_PROVIDER=google`, data goes to Google only. If you set
`GEMINI_MEDIA_PROVIDER=openrouter`:

- Prompts and reference images (base64 data URLs, after the same sandbox and type checks) are
  sent to **OpenRouter** (`https://openrouter.ai/api/v1/images`), which forwards them to
  Google. OpenRouter therefore processes your content and request metadata under
  [its own terms and privacy policy](https://openrouter.ai/docs/guides/privacy/data-collection).
  It says it stores metadata (tokens, latency) for every request, but not prompts or
  responses unless you turn on logging; content flagged as potential CSAM may be retained.
- Requests are restricted with `provider.only = ["google-ai-studio", "google-vertex"]`, so
  only Google-operated endpoints serve them. Check your OpenRouter account settings too,
  because account-wide provider settings are merged with this.
- Your `OPENROUTER_API_KEY` is sent only to `openrouter.ai`. Give it a credit limit in the
  OpenRouter dashboard as a second spending guard.
- Video is not available through OpenRouter; those tools return an error without any request.

## Known limitations

- Cost figures are estimates. Google's actual billing (token counts, price changes) can differ.
- Prices for some options (e.g. Omni Flash 1080p/4K) are not published; those options are
  disabled by default.
- `GEMINI_MEDIA_ALLOWED_DIRS` is only as safe as the folders you put in it. Do not allow your
  whole home directory.
- Prompts and input images are sent to Google and processed under Google's terms (and also
  to OpenRouter, under OpenRouter's terms, if you enable that provider).
