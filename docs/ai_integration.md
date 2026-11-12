# Local Ollama integration

Install [Ollama for Windows](https://docs.ollama.com/windows) using its native installer.
Ollama normally runs in the background at `http://localhost:11434`.

```powershell
ollama pull llama3.2:1b
ollama list
ollama serve   # only if no Ollama server is already running
```

Model: [Llama 3.2 1B](https://ollama.com/library/llama3.2:1b), the Ollama instruction-tuned
tag `llama3.2:1b`. The application uses the official `ollama` Python client; a small provider
Protocol separates transport from job-analysis services. No agent framework or extra
backend is introduced. The JSON schema/validation follows
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).

| Environment variable | Default | Purpose |
| --- | --- | --- |
| OLLAMA_BASE_URL | http://localhost:11434 | Loopback-only local HTTP endpoint |
| OLLAMA_MODEL | llama3.2:1b | Existing local model tag; no automatic downloads |
| OLLAMA_TEMPERATURE | 0 | Low-variance extraction |
| OLLAMA_NUM_CTX | 8192 | Model context window |
| OLLAMA_NUM_PREDICT | 4096 | Maximum generated tokens |
| OLLAMA_NUM_THREAD | 8 | CPU inference threads |
| OLLAMA_TIMEOUT | 600 | Per-attempt inference HTTP timeout |
| OLLAMA_RETRIES | 1 | Additional attempts for transient failures; maximum 2 |
| OLLAMA_MAX_DESCRIPTION_CHARS | 12000 | Absolute input character cap |

These variables are read from the project `.env` and environment.
The previous `OLLAMA_TIMEOUT_SECONDS` variable remains supported as an alias. When both
timeout names occur in the same configuration source, `OLLAMA_TIMEOUT` takes precedence.
Environment values override `.env`; explicit constructor values override both.

Inference also sets
`num_gpu=0`, seed 42 and five-minute model keep-alive. These defaults suit CPU inference
on the stated workstation; they do not establish a latency guarantee or exact reproducibility
across model digests/Ollama versions. Preserve the digest in audit runs for research.

HTTP requests disable environment proxies and redirects. URL validation rejects remote
hosts and cloud model tags. No job description is sent to an external API by this app;
operators must also use a trusted, genuinely local Ollama installation. Step 2 sends only
job descriptions; Step 3 uses the same provider for selected CV text chunks. The provider
checks the real model inventory rather than loading
or downloading a model on the operator's behalf.

Availability checks have a three-second request timeout. Inference connection timeout is
three seconds and the configured request/read timeout defaults to 600 seconds per attempt.
Timeouts, connection failures and HTTP 500/502/503/504 may be retried within the configured
attempt bound. Malformed output, missing model, nontransient HTTP errors and truncation
are not automatically retried. Two default attempts can therefore take roughly twenty
minutes plus connection/check overhead. Streamlit shows a loading indicator; other
operators/sessions should not rely on this single-operator prototype for concurrency.

Input is never silently truncated. A conservative character budget uses the smaller of
MAX_DESCRIPTION_CHARS and `2 * (NUM_CTX - NUM_PREDICT - 2000)` (default 4192 characters),
reserving room for instructions/schema/output. This is a guard, not an exact tokenizer;
unusual Unicode/tokenization may still exceed the context. Shorten lengthy descriptions
or increase context with awareness of CPU memory/latency. Increase output tokens for
truncated responses while keeping enough context room. Up to 50 criteria are accepted.

Failures expose short actionable messages without raw requests, model responses or
HTTP error bodies. A failed analysis run is recorded and any existing draft/approval
is preserved. No fake output or sample data is returned on failure.

Run the normal mocked suite with `uv run pytest`. For optional real inference:

```powershell
$env:ATS_TEST_LIVE_OLLAMA = "1"
uv run pytest -m live_ollama -rs
Remove-Item Env:ATS_TEST_LIVE_OLLAMA
```

Live inference quality, speed and memory depend on hardware and the actual installed
model. Prompt injection, semantic hallucination, irrelevant requirements and misclassified
priorities remain possible despite schema and exact-quote checks. Every configuration
requires HR review; this step implements neither automated screening nor legal validation.

For Step 3's CV schema, bounded chunk strategy and longer CPU request guidance, see
[CV analysis](cv_analysis.md). The shared `generate_structured` transport retains the
same loopback, timeout, retry, strict-validation and sanitized-error behavior for both
workflows. CV extraction never receives job criteria or candidate scores.
