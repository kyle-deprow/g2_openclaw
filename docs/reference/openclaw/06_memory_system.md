# OpenClaw — Memory System

## Overview

OpenClaw uses a **hybrid memory system** combining human-readable Markdown files with vector search for retrieval. This gives the agent both persistent long-term memory and efficient semantic search across its knowledge base.

## Memory Storage

### Markdown Memory Files

All memories are stored as plain Markdown files in the agent's workspace:

```
~/.openclaw/
  memory/
    MEMORY.md           # Long-term, critical memory (always referenced)
    2024-01-15.md       # Daily memory file
    2024-01-16.md       # Daily memory file
    2024-01-17.md       # Daily memory file
    ...
```

### File Types

| File | Purpose | Behavior |
|---|---|---|
| **`MEMORY.md`** | Critical long-term memories | Always available in bootstrap context |
| **`memory/YYYY-MM-DD.md`** | Daily episodic memories | Written by the agent during conversations |
| **`USER.md`** | User-specific knowledge | Updated as agent learns about the user |

### How the Agent Writes Memories

The agent writes memories by using standard file tools (`Write`, `Edit`) to create/update Markdown files:

```markdown
# 2024-01-17

## Conversations
- User asked about deploying Python apps to Azure
- Discussed Bicep modules for App Service configuration
- User prefers uv over pip for dependency management

## Decisions
- Agreed to use managed identity for all Azure services
- Will implement RLS at database level

## TODO
- Follow up on the CI pipeline configuration tomorrow
- Research Azure Container Apps pricing
```

---

## Vector Search

### Architecture

OpenClaw indexes memory files into a vector database for semantic search:

```
Memory Files (Markdown)
        │
        ▼
   Chunking & Embedding
        │
        ▼
   Vector Store (SQLite)
        │
        ▼
   Hybrid Search (BM25 + Vector)
        │
        ▼
   MMR Re-ranking + Temporal Decay
        │
        ▼
   Search Results
```

### Embedding Providers

| Provider | Config Key | Notes |
|---|---|---|
| **Local** (default) | `local` | Uses built-in embedding model, no API calls |
| **OpenAI** | `openai` | `text-embedding-3-small` or `text-embedding-3-large` |
| **Gemini** | `gemini` | Google's embedding model |
| **Voyage** | `voyage` | Voyage AI embeddings |

### Configuration

```json
{
  "memory": {
    "enabled": true,
    "vectorSearch": {
      "enabled": true,
      "backend": "sqlite",
      "embeddingProvider": "local",
      "embeddingModel": "default",
      "chunkSize": 512,
      "chunkOverlap": 50
    }
  }
}
```

### QMD Backend

For larger memory stores, OpenClaw supports the **QMD backend** — an optimized alternative to SQLite:

```json
{
  "memory": {
    "vectorSearch": {
      "backend": "qmd"
    }
  }
}
```

### SQLite with sqlite-vec

The default SQLite backend can optionally use the `sqlite-vec` extension for faster vector operations:

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

---

## Search System

### Hybrid Search

OpenClaw uses a hybrid search approach combining:

1. **BM25 (keyword search)** — Traditional text matching with term frequency scoring
2. **Vector similarity** — Semantic similarity using embedding cosine distance

The results are merged with configurable weights:

```json
{
  "memory": {
    "vectorSearch": {
      "hybridWeight": 0.7,    // 0.0 = pure BM25, 1.0 = pure vector
      "topK": 20              // Number of candidates before re-ranking
    }
  }
}
```

### MMR Re-ranking (Maximal Marginal Relevance)

After hybrid retrieval, results are re-ranked using MMR to balance:
- **Relevance**: How well the result matches the query
- **Diversity**: Avoiding redundant/overlapping results

```json
{
  "memory": {
    "vectorSearch": {
      "mmr": {
        "enabled": true,
        "lambda": 0.7,        // Balance: 1.0 = pure relevance, 0.0 = pure diversity
        "fetchMultiplier": 3   // Fetch 3x topK candidates for re-ranking
      }
    }
  }
}
```

### Temporal Decay

More recent memories are boosted over older ones:

```json
{
  "memory": {
    "vectorSearch": {
      "temporalDecay": {
        "enabled": true,
        "halfLifeDays": 30,    // Score halves every 30 days
        "weight": 0.2          // How much temporal decay affects final score
      }
    }
  }
}
```

---

## Memory Tools

### `memory_search`

Semantic search across all indexed memory files:

```
Tool: memory_search
Input: { "query": "Azure deployment configuration", "limit": 10 }
Output: [
  { "file": "memory/2024-01-17.md", "chunk": "...", "score": 0.89 },
  { "file": "memory/2024-01-15.md", "chunk": "...", "score": 0.76 },
  ...
]
```

### `memory_get`

Retrieve a specific memory file:

```
Tool: memory_get
Input: { "file": "memory/2024-01-17.md" }
Output: "# 2024-01-17\n\n## Conversations\n- User asked about..."
```

---

## Automatic Memory Flush (Pre-Compaction)

When a session is approaching compaction (context getting too long), OpenClaw can automatically trigger a **memory flush** — a silent turn where the agent writes important context to memory files before the session is compacted.

### How It Works

1. Session token count approaches `compaction.reserveTokensFloor`
2. If `memoryFlush.enabled` is true, a silent agent turn is triggered
3. Agent receives a system prompt telling it to save important memories
4. Agent writes to `memory/YYYY-MM-DD.md` or `MEMORY.md`
5. Agent replies with `NO_REPLY` if nothing to store
6. Session compaction then proceeds normally

### Configuration

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "memoryFlush": {
          "enabled": true,
          "softThresholdTokens": 4000,
          "systemPrompt": "Session nearing compaction. Store durable memories now.",
          "prompt": "Write any lasting notes to memory/YYYY-MM-DD.md; reply with NO_REPLY if nothing to store."
        }
      }
    }
  }
}
```

---

## Session Memory Indexing (Experimental)

OpenClaw can also index session transcripts (`.jsonl` files) into the vector search index:

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

This allows `memory_search` to find relevant information from past conversations, even if the agent didn't explicitly write it to a memory file.

---

## Embedding Cache

Embeddings are cached to avoid recomputing:

```
~/.openclaw/
  cache/
    embeddings/
      <hash>.json    # Cached embedding vectors
```

- Cache is keyed by content hash
- Survives restarts
- Automatically invalidated when the embedding model changes

---

## Memory Workflow

### Typical Agent Memory Behavior

1. **Session Start**: Agent reads `MEMORY.md` and recent `memory/YYYY-MM-DD.md` files (via bootstrap)
2. **During Conversation**: Agent uses `memory_search` to find relevant past context
3. **During Conversation**: Agent writes important new information to daily memory file
4. **Pre-Compaction**: Automatic memory flush saves critical session context
5. **Session End**: `session-memory` hook (if enabled) indexes the session transcript
6. **Cross-Session**: Memories persist across session resets and are searchable

### Best Practices

- **MEMORY.md**: Reserve for truly critical, long-term information
- **Daily files**: Use for episodic, time-bound information
- **USER.md**: User preferences and facts
- **Let the agent manage it**: The AGENTS.md template instructs the agent on memory practices
- **Vector search**: Enable for large memory stores (100+ files)

## References

- [Memory](https://docs.openclaw.ai/concepts/memory)
- [Session Pruning](https://docs.openclaw.ai/concepts/session-pruning)
- [Compaction](https://docs.openclaw.ai/concepts/compaction)
- [Configuration](https://docs.openclaw.ai/reference/configuration)
