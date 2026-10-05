"""
Scale benchmark: write throughput, index build, and query latency for
one principal holding N memories (default 10,000 — years of accumulated
memory for a single developer).

    python benchmarks/scale.py [N]
"""
from __future__ import annotations

import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from mnemosyne.config import Settings
from mnemosyne.container import Mnemosyne
from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.enums import MemoryType
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.evaluation.persona import MEMORIES, QUERIES
from mnemosyne.services.retrieval_service import RecallRequest

SUBJECTS = ["invoice", "refund", "webhook", "session", "cache", "queue", "user", "report", "ledger", "export",
            "migration", "dashboard", "token", "payment", "schema", "worker", "retry", "timeout", "config", "build"]
ACTIONS = ["validate", "serialize", "log", "test", "deploy", "index", "paginate", "lock", "encrypt", "parse",
           "format", "batch", "stream", "throttle", "debounce", "memoize", "audit", "rollback", "retry", "notify"]
QUALIFIERS = ["always", "prefer", "avoid", "never", "usually", "carefully", "explicitly", "lazily"]
REPOS = [f"github.com/acme/service-{i}" for i in range(25)]
TYPES = [MemoryType.CONVENTION, MemoryType.PATTERN, MemoryType.FACT, MemoryType.DECISION, MemoryType.FAILURE,
         MemoryType.PROCEDURE, MemoryType.PREFERENCE, MemoryType.TOOLING]


def synthetic(rng: random.Random, i: int) -> NewMemoryData:
    subject, action, qualifier = rng.choice(SUBJECTS), rng.choice(ACTIONS), rng.choice(QUALIFIERS)
    scope = rng.choice(["global", "lang:python", "lang:typescript", "lang:go", f"repo:{rng.choice(REPOS)}"])
    return NewMemoryData(
        title=f"{qualifier.capitalize()} {action} the {subject} ({i})",
        content=f"When working on {subject} code, {qualifier} {action} {rng.choice(SUBJECTS)} data before "
                f"{rng.choice(ACTIONS)}ing it; this came up in incident {rng.randint(100, 999)}.",
        memory_type=rng.choice(TYPES),
        scope=scope,
        importance_score=round(rng.uniform(0.2, 0.9), 2),
        tags=[subject, action],
    )


def main(n: int) -> None:
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(database_url=f"sqlite:///{Path(tmp) / 'bench.db'}", log_level="WARNING", home=Path(tmp))
        with Mnemosyne(settings, schema="migrate") as app:
            started = time.perf_counter()
            batch: list[NewMemoryData] = []
            for i in range(n - len(MEMORIES)):
                batch.append(synthetic(rng, i))
                if len(batch) == 500:
                    app.memories.create_memories_batch(batch)
                    batch = []
            if batch:
                app.memories.create_memories_batch(batch)
            from mnemosyne.evaluation.harness import to_new_memory

            app.memories.create_memories_batch([to_new_memory(m) for m in MEMORIES])
            write_s = time.perf_counter() - started
            print(f"memories:            {n}")
            print(f"write throughput:    {n / write_s:,.0f} memories/s (batched, embeddings included)")

            started = time.perf_counter()
            index = app.retrieval.load_index()
            print(f"index build (cold):  {(time.perf_counter() - started) * 1000:,.0f} ms for {index.size} memories")

            started = time.perf_counter()
            app.retrieval.load_index()
            print(f"index reuse (warm):  {(time.perf_counter() - started) * 1000:,.1f} ms (signature check only)")

        # A new container = a new process's view: in-memory cache is empty,
        # so this measures the persistent snapshot path that hooks use.
        with Mnemosyne(settings, schema="migrate") as fresh:
            started = time.perf_counter()
            fresh.retrieval.load_index()
            print(f"index load (new process, disk snapshot): {(time.perf_counter() - started) * 1000:,.0f} ms")

        with Mnemosyne(settings, schema="migrate") as app:
            latencies = []
            for query in QUERIES * 3:
                context = WorkingContext.create(repo_id=query.repo, languages=list(query.languages))
                result = app.retrieval.recall(RecallRequest(query=query.text, context=context, limit=10))
                latencies.append(result.took_ms)
            latencies.sort()
            p95 = latencies[int(0.95 * len(latencies)) - 1]
            print(f"query latency:       p50 {statistics.median(latencies):.1f} ms, p95 {p95:.1f} ms "
                  f"({len(latencies)} queries, warm index)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10_000)
