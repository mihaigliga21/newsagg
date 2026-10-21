# newsagg

Collects news from RSS feeds, websites and public Telegram channels, keeps what is about the configured regions (Moldova, Romania, international), merges duplicate stories, has an LLM write one headline line per story in Romanian, and posts a daily digest to a Telegram channel after you approve it in a private chat.

The digest looks like the Redactia.md one: a topic emoji, a bold lead, the source linked on a few words inside the sentence, and `::: ROMÂNIA` / `::: EXTERNE` sections.

## How it works

Four services, one Docker image. They only talk through Postgres (Supabase). Each one wakes as soon as the previous one writes something (Postgres `LISTEN/NOTIFY`), with a 60-second poll as a safety net.

| Service | Reads | Writes |
|---|---|---|
| `collector` | sources in `config/config.yaml`, each on its own interval | `raw_items` (status `new`) |
| `processor` | `raw_items` with status `new` | labels region, topic and importance (LLM, keyword fallback), rejects ads and off-region items, merges duplicates into `stories` |
| `writer` | `stories` with status `pending_write` | the line (emoji, bold lead, sentence, link anchor) in `stories.line` |
| `publisher` | `stories` with status `written`, on the cron in `channel.schedule` | keeps one line per event (LLM grouping, plain ranking as fallback), builds the digest, sends it to your review chat with ✅ / ❌ buttons, posts it to the channel on ✅ |

In the review chat, `/digest` builds a digest right away and `/status` shows counts for the last 24 hours and failing sources.

## Setup

1. **Supabase:** create a project. Copy the *Session pooler* connection string (port 5432) from Project Settings > Database. The transaction pooler (port 6543) does not support `LISTEN`.
2. **Telegram:** create a bot with @BotFather. Add it to your channel as an admin with "Post messages". Send `/start` to the bot from your own account, and get your user id from @userinfobot (that is the review chat id).
3. **LLM:** an Anthropic API key (default model `claude-haiku-5-5`).
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
```

Without Docker: `pip install -e ".[dev]"`, then `newsagg migrate`, `newsagg run all` (all four stages in one process) or `newsagg run collector` etc.

## Configuration

Everything is in `config/config.yaml`:

- **sources**: `rss`, `website` (listing page + CSS selector for article links) or `telegram` (public channel through `t.me/s/<channel>`). `priority` decides which outlet gets the link when several cover the same story (lower is better). Feeds marked `checked` returned valid RSS when this was written; the others are disabled until you check them with `test-source`.
- **regions** and **sections**: which regions exist and which section of the digest each goes into, and how many lines each section gets (`max_items`; the default 6/4/3 fits one Telegram message). Descriptions guide the LLM; keywords are the fallback.
- **channel.max_per_subject**: at most this many lines about the same person or organisation (default 2).
- **topics**: the emoji per topic.
- **filter.blocklist**: words that drop an item before anything else (horoscopes, ads).
- **dedupe**: time window and title similarity threshold for merging stories.
- **llm**: provider, model, batch size, `min_importance` (stories below it are kept for dedupe but not written or posted), and `curate` (at digest time, group candidates that report the same event and keep the best line).

### Changing the LLM

`llm.provider: anthropic` uses the Anthropic SDK. `llm.provider: openai_compatible` works with any OpenAI-style API (OpenAI, Gemini, Mistral, Groq, OpenRouter, a local Ollama): set `llm.model`, `LLM_API_KEY` and `LLM_BASE_URL` (for example `https://api.openai.com/v1` or `http://host.docker.internal:11434/v1`). To add another provider, implement `LLMProvider.generate` in `newsagg/llm/` and register it in `build_provider`.

With `llm.enabled: false` everything still runs: keyword labelling and a template line (title split into a bold lead and a linked tail).

## Tests

```bash
pip install -e ".[dev]"
pytest                                                      # unit tests
NEWSAGG_TEST_DATABASE_URL=postgresql://... pytest           # plus the end-to-end pipeline test (drops the newsagg schema!)
```
