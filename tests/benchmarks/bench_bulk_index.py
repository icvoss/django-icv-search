#!/usr/bin/env python3
"""Benchmark: old-path (sequential JSON) vs new-path (concurrent NDJSON).

Reads documents from an existing Meilisearch index, then re-indexes them
into temporary indexes using both approaches. Prints wall-clock time,
docs/sec, and speedup ratio.

Usage:
    python tests/benchmarks/bench_bulk_index.py [OPTIONS]

    --url           Meilisearch URL (default: http://127.0.0.1:7700)
    --source-index  Index to read documents from (default: vendably_products)
    --num-docs      Number of documents to benchmark (default: 50000)
    --old-batch     Batch size for old path (default: 1000)
    --new-batch     Batch size for new path (default: 5000)
    --concurrency   Sender threads for new path (default: 2)
    --skip-old      Skip the old-path benchmark (useful for quick iteration)

No Django required — talks to Meilisearch directly via httpx.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event

import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fetch_documents(client: httpx.Client, index: str, limit: int) -> list[dict]:
    """Paginate through GET /indexes/{index}/documents to collect docs."""
    docs: list[dict] = []
    offset = 0
    page_size = min(limit, 1000)  # Meilisearch caps at 1000 per page
    while len(docs) < limit:
        resp = client.get(
            f"/indexes/{index}/documents",
            params={"limit": page_size, "offset": offset},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            break
        docs.extend(results)
        offset += len(results)
        if len(results) < page_size:
            break
        sys.stdout.write(f"\r  Fetched {len(docs):,} / {limit:,} documents...")
        sys.stdout.flush()
    docs = docs[:limit]
    print(f"\r  Fetched {len(docs):,} documents.          ")
    return docs


def create_temp_index(client: httpx.Client, uid: str) -> None:
    resp = client.post("/indexes", json={"uid": uid, "primaryKey": "id"})
    if resp.status_code >= 400 and "already exists" not in resp.text.lower():
        resp.raise_for_status()


def delete_temp_index(client: httpx.Client, uid: str) -> None:
    client.delete(f"/indexes/{uid}")
    # Ignore 404 (already gone) and task-queued responses.


def wait_for_idle(client: httpx.Client, uid: str, timeout: float = 300) -> None:
    """Wait until the index has finished processing all enqueued tasks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/indexes/{uid}/stats")
        if resp.status_code == 200 and not resp.json().get("isIndexing", True):
            return
        time.sleep(0.5)
    print(f"  WARNING: timed out waiting for '{uid}' to finish indexing.")


# ---------------------------------------------------------------------------
# Old path: sequential JSON batches
# ---------------------------------------------------------------------------


def bench_old_path(
    client: httpx.Client,
    uid: str,
    docs: list[dict],
    batch_size: int,
) -> float:
    """Index documents using sequential add_documents (JSON arrays)."""
    create_temp_index(client, uid)
    total = 0
    t0 = time.perf_counter()

    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        resp = client.post(
            f"/indexes/{uid}/documents",
            json=batch,
            params={"primaryKey": "id"},
        )
        resp.raise_for_status()
        total += len(batch)
        if (total // batch_size) % 10 == 0:
            sys.stdout.write(f"\r  [old] Sent {total:,} / {len(docs):,}...")
            sys.stdout.flush()

    elapsed = time.perf_counter() - t0
    print(f"\r  [old] Sent {total:,} documents in {elapsed:.1f}s          ")
    return elapsed


# ---------------------------------------------------------------------------
# New path: concurrent NDJSON batches
# ---------------------------------------------------------------------------


def bench_new_path(
    client: httpx.Client,
    uid: str,
    docs: list[dict],
    batch_size: int,
    concurrency: int,
) -> float:
    """Index documents using concurrent NDJSON sends."""
    create_temp_index(client, uid)

    send_queue: Queue[list[dict] | None] = Queue(maxsize=concurrency + 1)
    error_event = Event()
    first_error: list[Exception] = []

    def sender() -> None:
        while not error_event.is_set():
            try:
                batch = send_queue.get(timeout=1.0)
            except Empty:
                continue
            if batch is None:
                return
            try:
                body = b"".join(json.dumps(doc, separators=(",", ":")).encode() + b"\n" for doc in batch)
                resp = client.post(
                    f"/indexes/{uid}/documents",
                    content=body,
                    params={"primaryKey": "id"},
                    headers={"Content-Type": "application/x-ndjson"},
                )
                resp.raise_for_status()
            except Exception as exc:
                if not first_error:
                    first_error.append(exc)
                error_event.set()
                return

    total = 0
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(sender) for _ in range(concurrency)]

        for i in range(0, len(docs), batch_size):
            if error_event.is_set():
                break
            batch = docs[i : i + batch_size]
            send_queue.put(batch)
            total += len(batch)
            if (total // batch_size) % 5 == 0:
                sys.stdout.write(f"\r  [new] Sent {total:,} / {len(docs):,}...")
                sys.stdout.flush()

        for _ in range(concurrency):
            send_queue.put(None)

        for f in futures:
            f.result()

    if first_error:
        raise first_error[0]

    elapsed = time.perf_counter() - t0
    print(f"\r  [new] Sent {total:,} documents in {elapsed:.1f}s          ")
    return elapsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark bulk indexing paths.")
    parser.add_argument("--url", default="http://127.0.0.1:7700")
    parser.add_argument("--source-index", default="vendably_products")
    parser.add_argument("--num-docs", type=int, default=50_000)
    parser.add_argument("--old-batch", type=int, default=1000)
    parser.add_argument("--new-batch", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--skip-old", action="store_true")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.url, timeout=120)

    # Verify connectivity.
    resp = client.get("/health")
    if resp.status_code != 200 or resp.json().get("status") != "available":
        print(f"ERROR: Meilisearch at {args.url} is not healthy.")
        sys.exit(1)
    print(f"Connected to Meilisearch at {args.url}")

    # Fetch source documents.
    print(f"\nFetching {args.num_docs:,} documents from '{args.source_index}'...")
    docs = fetch_documents(client, args.source_index, args.num_docs)
    if not docs:
        print("ERROR: No documents found.")
        sys.exit(1)
    num = len(docs)

    old_uid = "_bench_old_path"
    new_uid = "_bench_new_path"

    # Clean up any leftover temp indexes.
    delete_temp_index(client, old_uid)
    delete_temp_index(client, new_uid)
    time.sleep(1)

    # --- Old path ---
    old_elapsed = 0.0
    if not args.skip_old:
        print(f"\n--- Old path: sequential JSON, batch_size={args.old_batch} ---")
        old_elapsed = bench_old_path(client, old_uid, docs, args.old_batch)
        print("  Waiting for Meilisearch to finish processing...")
        wait_for_idle(client, old_uid)
        old_docs_sec = num / old_elapsed if old_elapsed else 0
        print(f"  Result: {old_elapsed:.1f}s wall clock, {old_docs_sec:,.0f} docs/sec (send time)")

    # --- New path ---
    print(f"\n--- New path: concurrent NDJSON, batch_size={args.new_batch}, concurrency={args.concurrency} ---")
    new_elapsed = bench_new_path(client, new_uid, docs, args.new_batch, args.concurrency)
    print("  Waiting for Meilisearch to finish processing...")
    wait_for_idle(client, new_uid)
    new_docs_sec = num / new_elapsed if new_elapsed else 0
    print(f"  Result: {new_elapsed:.1f}s wall clock, {new_docs_sec:,.0f} docs/sec (send time)")

    # --- Summary ---
    print("\n" + "=" * 60)
    print(f"Documents:    {num:,}")
    if old_elapsed:
        print(f"Old path:     {old_elapsed:.1f}s  ({num / old_elapsed:,.0f} docs/sec)")
    print(f"New path:     {new_elapsed:.1f}s  ({new_docs_sec:,.0f} docs/sec)")
    if old_elapsed:
        speedup = old_elapsed / new_elapsed if new_elapsed else float("inf")
        print(f"Speedup:      {speedup:.1f}x")
    print("=" * 60)

    # Clean up.
    print("\nCleaning up temp indexes...")
    delete_temp_index(client, old_uid)
    delete_temp_index(client, new_uid)
    print("Done.")


if __name__ == "__main__":
    main()
