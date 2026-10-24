# newsagg

Collects news from RSS feeds, websites and public Telegram channels, keeps what is about the configured regions (Moldova, Romania, international), merges duplicate stories, has an LLM write one headline line per story in Romanian, and posts a daily digest to a Telegram channel after you approve it in a private chat.

The digest looks like the Redactia.md one: a topic emoji, a bold lead, the source linked on a few words inside the sentence, and `::: ROMÂNIA` / `::: EXTERNE` sections.

## How it works

Four services, one Docker image. They only talk through Postgres (Supabase). Each one wakes as soon as the previous one writes something (Postgres `LISTEN/NOTIFY`), with a 60-second poll as a safety net.

| Service | Reads | Writes |
|---|---|---|
| `collector` | sources in `config/config.yaml`, each on its own interval | `raw_items` (status `new`) |
| `processor` | `raw_items` with status `new` | labels region, topic and importance (LLM, keyword fallback), rejects ads and off-region items, merges items about the same happening into one story (the LLM compares each item with recent stories; title similarity is the fallback) |
| `writer` | `stories` with status `pending_write` | the line (emoji, bold lead, sentence, link anchor) in `stories.line` |
| `publisher` | `stories` with status `written`, on the cron in `channel.schedule` | keeps one line per event (an incident and its same-day follow-ups count as one), links the other outlets that reported it after the line, drops events already sent (LLM grouping plus a title-similarity check; the review chat warns when the LLM step failed), builds the digest, sends it to your review chat with ✅ / ❌ buttons, posts it to the channel on ✅ |

In the review chat, `/digest` builds a digest right away and `/status` shows counts for the last 24 hours and failing sources.

If a scheduled digest fails (after a few retries) or its slot passes while the publisher is down, the next start catches it up: all of today's missed slots become one digest, and earlier days are dropped. If the next slot is less than 30 minutes away, that slot covers them instead.

## Setup

1. **Supabase:** create a project. Copy the *Session pooler* connection string (port 5432) from Project Settings > Database. The transaction pooler (port 6543) does not support `LISTEN`.
2. **Telegram:** create a bot with @BotFather. Add it to your channel as an admin with "Post messages". Send `/start` to the bot from your own account, and get your user id from @userinfobot (that is the review chat id).
3. **LLM:** an API key for Anthropic or an OpenAI-style provider such as OpenRouter, or a local model with Ollama (see below).
4. `cp .env.example .env` and fill it in.
5. `docker compose up -d --build`

The `migrate` service creates the schema (`newsagg` schema in the database) before the others start. Follow logs with `docker compose logs -f`.

To try it without Supabase: set `DATABASE_URL=postgresql://newsagg:newsagg@db:5432/newsagg` and run `docker compose --profile localdb up -d --build`.

## Useful commands

```bash
docker compose run --rm collector test-source zdg     # fetch one source and print what it returns
docker compose run --rm publisher digest --dry-run    # print the digest that would be built now
docker compose run --rm publisher digest              # build one now and send it for review
docker compose restart                                # after editing config/config.yaml
docker compose run --rm -T publisher llm-bench --model qwen3:8b > bench.md   # compare a local model (read-only)
```

Without Docker: `pip install -e ".[dev]"`, then `newsagg migrate`, `newsagg run all` (all four stages in one process) or `newsagg run collector` etc.

## Configuration

Everything is in `config/config.yaml`:

- **sources**: `rss`, `website` (listing page + CSS selector for article links) or `telegram` (public channel through `t.me/s/<channel>`). `priority` decides which outlet gets the link when several cover the same story (lower is better). Feeds marked `checked` returned valid RSS when this was written; the others are disabled until you check them with `test-source`.
- **regions** and **sections**: which regions exist and which section of the digest each goes into, and how many lines each section gets (`max_items`; the default 6/4/3 fits one Telegram message). Descriptions guide the LLM; keywords are the fallback.
- **channel.max_per_subject**: at most this many lines about the same person or organisation (default 2).
- **channel.repeat_window_hours**: events that were in a digest posted or waiting for review within this many hours are not repeated (default 48). A digest is never padded: on a quiet evening it is shorter, or not sent, and the review chat says how many repeats were left out.
- **topics**: the emoji per topic.
- **filter.blocklist**: words that drop an item before anything else (horoscopes, ads).
- **dedupe**: time window and title similarity threshold for merging stories, and `llm_recent_stories` (how many recent stories the LLM labeller compares new items with).
- **llm**: provider, model, batch size, `min_importance` (stories below it are kept for dedupe but not written or posted), and `curate` (at digest time, group candidates that report the same event and keep the best line).

### Changing the LLM

`llm.provider` and `llm.model` can be set in `config/config.yaml` or, to switch without editing it, with `LLM_PROVIDER` and `LLM_MODEL` in `.env`.

- `anthropic` uses the Anthropic SDK (`LLM_API_KEY`).
- `openai_compatible` works with any OpenAI-style API (OpenAI, Gemini, Mistral, Groq, OpenRouter): set `LLM_API_KEY` and `LLM_BASE_URL` (for example `https://api.openai.com/v1`).
- `ollama` runs a local model (below).

To add another provider, implement `LLMProvider.generate` in `newsagg/llm/` and register it in `build_provider`.

With `llm.enabled: false` everything still runs: keyword labelling and a template line (title split into a bold lead and a linked tail).

### Local models with Ollama

1. Install [Ollama](https://ollama.com) on the machine that runs Docker and pull a model: `ollama pull qwen3:8b`. Models that write Romanian reasonably well and fit an 8-12 GB GPU include `qwen3:8b` and `gemma3:12b`; bigger is better at grouping events.
2. In `.env`: `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3:8b`, and leave `LLM_API_KEY` and `LLM_BASE_URL` empty. The containers reach Ollama at `http://host.docker.internal:11434` (set `LLM_BASE_URL` if it runs elsewhere).
3. Before switching, compare it with the current model on your own recent data. This labels, writes and groups again and writes a report, without changing anything:
   `docker compose run --rm -T publisher llm-bench --model qwen3:8b > bench.md`
4. `docker compose up -d`

The `ollama` provider uses Ollama's own API rather than its OpenAI-style `/v1` one, because only the native API can set the context window. Ollama's default window (2-4k tokens) would silently cut the labelling and grouping prompts, which need about 10k. Settings under `llm` (ignored by the other providers):

- `num_ctx` (default 16384): the context window. A larger one uses more GPU memory. If a prompt fills it, the call fails with a message saying so, and the stage falls back as it does for any LLM failure.
- `think: false`: turns off the thinking mode of reasoning models such as `qwen3`, which is much faster. Ignored for models without one.
- `keep_alive` (default `30m` in the config): how long the model stays loaded between batches.
- `timeout_seconds` (default 600 for Ollama): a slow GPU or CPU may need more, or a smaller `batch_size`.

Replies are constrained to the JSON schema, so they are always valid JSON; what a smaller model gets wrong is the content (a region, an event grouping). Ollama runs one request at a time by default, so the stages wait for each other.

## Tests

```bash
pip install -e ".[dev]"
pytest                                                      # unit tests
NEWSAGG_TEST_DATABASE_URL=postgresql://... pytest           # plus the end-to-end pipeline test (drops the newsagg schema!)
```
