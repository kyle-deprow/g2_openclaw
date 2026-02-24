---
name: openclaw-memory
description:
  OpenClaw memory system — Markdown-based storage, vector search, embedding providers, hybrid retrieval, and knowledge management. Use when configuring memory backends, tuning vector search parameters, choosing embedding providers, designing memory workflows, managing pre-compaction flush, or debugging memory search quality. Triggers on tasks involving memory files, MEMORY.md, vector indexing, BM25, MMR re-ranking, temporal decay, embedding cache, or session memory indexing.
---

# OpenClaw Memory System

Persistent knowledge through Markdown files, vector search with hybrid retrieval,
and deliberate memory management practices.

## When to Apply

Reference these guidelines when:

- Configuring the memory backend (SQLite vs QMD)
- Choosing and configuring embedding providers
- Tuning hybrid search weights (BM25 vs vector)
- Configuring MMR re-ranking for diverse results
- Setting temporal decay parameters
- Designing memory file structure and naming conventions
- Configuring pre-compaction memory flush
- Enabling or troubleshooting session memory indexing
- Debugging poor search results or missing memories

## Rule Categories by Priority

| Priority | Category              | Impact   | Prefix       |
| -------- | --------------------- | -------- | ------------ |
| 1        | Memory Architecture   | CRITICAL | `mem-`       |
| 2        | Vector Search Config  | HIGH     | `search-`    |
| 3        | Embedding Providers   | HIGH     | `embed-`     |
| 4        | Retrieval Tuning      | HIGH     | `retrieval-` |
| 5        | Memory Workflows      | MEDIUM   | `workflow-`  |

---

## 1. Memory Architecture (CRITICAL)

### `mem-three-file-types`
OpenClaw memory uses three file types, each with a distinct role:

| File                    | Purpose                     | Lifecycle                        |
| ----------------------- | --------------------------- | -------------------------------- |
| `MEMORY.md`             | Critical long-term facts    | Always in bootstrap, rarely updated |
| `memory/YYYY-MM-DD.md`  | Daily episodic memories     | Created daily, accumulates       |
| `USER.md`               | User-specific knowledge     | Updated as agent learns          |

```
~/.openclaw/
  MEMORY.md                  # "What I must never forget"
  USER.md                    # "What I know about my human"
  memory/
    2026-02-19.md            # "What happened two days ago"
    2026-02-20.md            # "What happened yesterday"
    2026-02-21.md            # "What happened today"
```

### `mem-memory-md-critical-only`
MEMORY.md is loaded in the bootstrap context every session. Keep it for truly
critical, rarely-changing facts:

```markdown
// ✅ MEMORY.md — durable, high-impact facts
# Memory
- Production database is PostgreSQL on Azure Flexible Server
- Deploy pipeline: GitHub Actions → Bicep → Azure
- HIPAA compliance required for all patient data
- Team standup at 10:00 AM UTC every weekday

// ❌ MEMORY.md — ephemeral details that belong in daily files
- Fixed a bug in the login page today
- User asked about React performance
```

### `mem-daily-files-structured`
Structure daily memory files with consistent sections:

```markdown
# 2026-02-21

## Conversations
- Discussed Azure Bicep module structure for Key Vault
- User wants managed identity for all service-to-service auth

## Decisions
- Will use per-channel-peer DM scoping for the team bot
- Chose SQLite backend for vector memory (team is small)

## Tasks
- [ ] Set up CI pipeline webhook integration
- [x] Configure daily summary cron job

## Learnings
- Azure Bicep @secure() decorator doesn't encrypt — it prevents logging
```

### `mem-human-readable-first`
All memory is plain Markdown — human-readable, version-controllable, portable.
The vector index is derived from these files, not the other way around. If the
vector store corrupts, re-index from the Markdown source of truth.

### `mem-survives-everything`
Memory files survive session resets, compaction, and Gateway restarts. They are
the agent's durable knowledge layer. Only explicit file deletion removes them.

```
Session reset   → memory files untouched ✅
Compaction      → memory files untouched ✅ (plus flush writes new memories)
Gateway restart → memory files untouched ✅
Agent switch    → per-agent memory dirs
```

---

## 2. Vector Search Configuration (HIGH)

### `search-backend-choice`
Two backends, choose based on scale:

| Backend   | Best For                  | Dependencies           |
| --------- | ------------------------- | ---------------------- |
| `sqlite`  | Most deployments (<1000 files) | None (built-in)    |
| `qmd`     | Large memory stores       | QMD library            |

```json
// ✅ Default — works for most users
{ "memory": { "vectorSearch": { "backend": "sqlite" } } }

// ✅ Large deployment with thousands of memory files
{ "memory": { "vectorSearch": { "backend": "qmd" } } }
```

### `search-sqlite-vec`
For SQLite backend, optionally enable sqlite-vec extension for faster vector
operations. Only worth it for 500+ memory files:

```json
{
  "memory": {
    "vectorSearch": {
      "backend": "sqlite",
      "sqliteVec": true
    }
  }
}
```

### `search-chunk-tuning`
The chunking configuration affects search quality:

```json
{
  "memory": {
    "vectorSearch": {
      "chunkSize": 512,
      "chunkOverlap": 50
    }
  }
}
```

| Parameter      | Default | Effect                                          |
| -------------- | ------- | ----------------------------------------------- |
| `chunkSize`    | 512     | Larger = broader context per chunk, fewer chunks |
| `chunkOverlap` | 50      | More overlap = better boundary matching          |

For short daily notes, 512 chunks work well. For longer documents or research
notes, consider 1024 with 100 overlap.

### `search-enable-for-scale`
Vector search adds overhead. Enable it when:
- Memory directory has 50+ files
- Agent frequently needs to recall old context
- Daily files contain varied topics

Disable it when:
- Memory is small (under 20 files)
- Agent only needs MEMORY.md and recent dailies
- Low-resource deployment

```json
// Enable vector search
{ "memory": { "vectorSearch": { "enabled": true } } }

// Disable — agent uses file tools directly
{ "memory": { "vectorSearch": { "enabled": false } } }
```

---

## 3. Embedding Providers (HIGH)

### `embed-provider-choice`
Four embedding providers, each with trade-offs:

| Provider   | Config Key | Latency | Quality | Cost    | Privacy |
| ---------- | ---------- | ------- | ------- | ------- | ------- |
| **Local**  | `local`    | Fast    | Good    | Free    | Full    |
| **OpenAI** | `openai`   | Medium  | Excellent | $/token | API    |
| **Gemini** | `gemini`   | Medium  | Very good | $/token | API   |
| **Voyage** | `voyage`   | Medium  | Excellent | $/token | API   |

```json
// ✅ Default — no API calls, full privacy
{ "memory": { "vectorSearch": { "embeddingProvider": "local" } } }

// ✅ Best quality for large knowledge bases
{ "memory": { "vectorSearch": { "embeddingProvider": "openai", "embeddingModel": "text-embedding-3-large" } } }
```

### `embed-local-default`
Start with local embeddings. Switch to a cloud provider only if search quality
is noticeably poor for your domain. Local embeddings work well for:
- General knowledge and conversation recall
- Technical documentation
- Daily notes and task tracking

Cloud embeddings excel for:
- Domain-specific jargon (medical, legal, scientific)
- Multi-language content
- Semantic nuance in complex queries

### `embed-cache-management`
Embeddings are cached at `~/.openclaw/cache/embeddings/`:
- Keyed by content hash — same content never re-embedded
- Survives restarts
- Automatically invalidated when embedding model changes

No manual cache management needed. If you switch providers, the cache rebuilds
transparently on next search.

---

## 4. Retrieval Tuning (HIGH)

### `retrieval-hybrid-weight`
Hybrid search combines BM25 (keyword) and vector (semantic). The weight
controls the balance:

```json
{
  "memory": {
    "vectorSearch": {
      "hybridWeight": 0.7
    }
  }
}
```

| Weight | Bias                    | Best For                          |
| ------ | ----------------------- | --------------------------------- |
| 0.0    | Pure BM25 (keyword)     | Exact term matching, code search  |
| 0.5    | Equal blend             | Balanced general use              |
| 0.7    | Vector-heavy (default)  | Conceptual/semantic queries       |
| 1.0    | Pure vector             | Abstract concept matching         |

Start at 0.7 (default). If the agent finds exact terms but misses concepts, increase.
If it finds related concepts but misses specific keywords, decrease.

### `retrieval-topk-candidates`
`topK` controls how many candidates are retrieved before re-ranking:

```json
{ "memory": { "vectorSearch": { "topK": 20 } } }
```

Higher topK = better recall but slower. 20 is a good default. Increase to 50
for very large memory stores or broad queries.

### `retrieval-mmr-diversity`
MMR (Maximal Marginal Relevance) re-ranks results to balance relevance and
diversity — avoiding returning five chunks that all say the same thing:

```json
{
  "memory": {
    "vectorSearch": {
      "mmr": {
        "enabled": true,
        "lambda": 0.7,
        "fetchMultiplier": 3
      }
    }
  }
}
```

| Parameter         | Effect                                         |
| ----------------- | ---------------------------------------------- |
| `lambda`          | 1.0 = pure relevance, 0.0 = pure diversity     |
| `fetchMultiplier` | Fetch N*topK candidates for re-ranking pool     |

Default lambda 0.7 is good for most cases. Lower to 0.5 if results are redundant.

### `retrieval-temporal-decay`
Boost recent memories over old ones:

```json
{
  "memory": {
    "vectorSearch": {
      "temporalDecay": {
        "enabled": true,
        "halfLifeDays": 30,
        "weight": 0.2
      }
    }
  }
}
```

| Parameter      | Effect                                        |
| -------------- | --------------------------------------------- |
| `halfLifeDays` | Score halves every N days (30 = gentle decay)  |
| `weight`       | How much decay affects final score (0.2 = 20%) |

Enable for personal assistants where recency matters. Disable for reference
knowledge bases where old information is equally valuable.

---

## 5. Memory Workflows (MEDIUM)

### `workflow-memory-tools`
Two tools for memory access during conversations:

| Tool             | Purpose                                    |
| ---------------- | ------------------------------------------ |
| `memory_search`  | Semantic search across all indexed memories|
| `memory_get`     | Retrieve a specific memory file by path    |

The agent uses `memory_search` when it needs to recall something from the past,
and `memory_get` to read a specific day's notes.

### `workflow-precompaction-flush`
The most important memory workflow: automatic flush before compaction.

When the context window approaches its limit, OpenClaw silently prompts the
agent to write durable memories before compaction summarizes and compresses:

```json
{
  "compaction": {
    "memoryFlush": {
      "enabled": true,
      "softThresholdTokens": 4000,
      "systemPrompt": "Session nearing compaction. Store durable memories now.",
      "prompt": "Write lasting notes to memory/YYYY-MM-DD.md. Reply NO_REPLY if nothing."
    }
  }
}
```

**Always enable this.** Without it, nuanced session context is lost to
compaction's summarization.

### `workflow-session-memory-indexing`
Experimental: index session transcripts (.jsonl) into vector search:

```json
{
  "memory": {
    "sessionMemory": {
      "enabled": true,
      "indexTranscripts": true
    }
  }
}
```

This lets `memory_search` find information from conversations even if the agent
didn't explicitly write it to memory. Useful as a safety net, but increases
index size and search latency.

### `workflow-memory-hygiene`
Instruct the agent on memory hygiene through AGENTS.md:

```markdown
## Memory Practices
- Write to daily file when a decision is made
- Update USER.md when learning lasting preferences
- Review MEMORY.md monthly — archive stale entries
- Don't duplicate: check memory_search before writing similar notes
- Prefer structured notes over raw conversation dumps
```

### `workflow-search-before-write`
Tell the agent to search memory before writing to avoid duplicates:

```markdown
Before writing to memory, search for existing entries on the topic.
If similar information exists, update rather than duplicate.
```

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
| --- | --- | --- |
| Everything in MEMORY.md | Bloats bootstrap, wastes tokens | MEMORY.md for critical only; daily files for details |
| No pre-compaction flush | Context lost to summarization | Enable memoryFlush.enabled always |
| Cloud embeddings for small stores | Unnecessary cost and latency | Use local embeddings for <500 files |
| Pure vector search (weight 1.0) | Misses exact keyword matches | Use hybrid (0.7 default) |
| Temporal decay on reference docs | Old docs scored lower unfairly | Disable decay for knowledge bases |
| No MMR re-ranking | Redundant search results | Enable MMR with lambda 0.7 |
| Unstructured daily files | Hard to search and maintain | Use consistent section headers |
| Over-writing to memory | Index bloat, noise in search | Search before write, be selective |
| Session memory without flush | Double indexing with less control | Enable flush; use session indexing as safety net |

---

## Quick Config Template

```json
{
  "memory": {
    "enabled": true,
    "vectorSearch": {
      "enabled": true,
      "backend": "sqlite",
      "sqliteVec": false,
      "embeddingProvider": "local",
      "chunkSize": 512,
      "chunkOverlap": 50,
      "hybridWeight": 0.7,
      "topK": 20,
      "mmr": {
        "enabled": true,
        "lambda": 0.7,
        "fetchMultiplier": 3
      },
      "temporalDecay": {
        "enabled": true,
        "halfLifeDays": 30,
        "weight": 0.2
      }
    },
    "sessionMemory": {
      "enabled": false
    }
  },
  "agents": {
    "defaults": {
      "compaction": {
        "memoryFlush": {
          "enabled": true,
          "softThresholdTokens": 4000
        }
      }
    }
  }
}
```

## References

- https://docs.openclaw.ai/concepts/memory
- https://docs.openclaw.ai/concepts/session-pruning
- https://docs.openclaw.ai/concepts/compaction
- https://docs.openclaw.ai/reference/configuration
