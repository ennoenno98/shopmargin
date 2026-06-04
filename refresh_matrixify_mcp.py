"""Refresh the dashboard's data files via the Matrixify **MCP server** (no Drive/FTP).

Matrixify exposes its export engine over a remote MCP server. This script runs an
MCP client against it, so it works unattended in CI (GitHub Actions) where the
network is open — unlike a Claude Code web session, whose egress allowlist blocks
``app.matrixify.app``.

What it does, end to end:

  1. Connect to ``https://mcp.matrixify.app/mcp`` with an MCP token.
  2. Export **products** (Basic + Variants + Variant Cost) -> data/matrixify_products.csv
  3. Export **orders** in **monthly date-range chunks** and stitch them into
     data/matrixify_orders.csv.gz

Why chunks: Matrixify caps a single export at the plan's per-job order limit
(Big plan = 10,000 orders/job). The store has ~17k+ orders, so one unfiltered
export silently drops the overflow. Monthly chunks (~a few thousand orders each)
stay well under the cap; if a month ever approaches the limit the chunk is split
in half and retried, so the output is always complete.

Refunds can post-date an order (a months-old order may be refunded today), so the
full history is re-pulled on every run rather than only the recent window — this
keeps net revenue / CM correct.

Environment:
    MATRIXIFY_MCP_TOKEN   (required)  token from Matrixify -> Settings ->
                                      "AI Agent MCP Tokens" -> Generate Token
    MATRIXIFY_MCP_URL     (optional)  default https://mcp.matrixify.app/mcp
    ORDERS_SINCE          (optional)  first order month, YYYY-MM-DD (default 2025-07-01)

Usage:
    python refresh_matrixify_mcp.py            # full refresh
    python refresh_matrixify_mcp.py --self-test  # offline: chunking + stitch only
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import io
import json
import os
import sys
import zipfile
from pathlib import Path

DATA = Path(__file__).with_name("data")
ORDERS_OUT = DATA / "matrixify_orders.csv.gz"
PRODUCTS_OUT = DATA / "matrixify_products.csv"

# Use `or` (not get's default) so an env var that is *set but empty* — e.g. the
# workflow passing an unset ${{ vars.ORDERS_SINCE }} as "" — falls back too.
MCP_URL = os.environ.get("MATRIXIFY_MCP_URL") or "https://mcp.matrixify.app/mcp"
ORDERS_SINCE = os.environ.get("ORDERS_SINCE") or "2025-07-01"

# Per-export Matrixify config (mirrors what the dashboard's matrixify_client reads).
ORDERS_GROUPS = {g: {"include": True} for g in
                 ("base", "customers", "line_type", "line_items", "refunds", "transactions")}
PRODUCTS_GROUPS = {g: {"include": True} for g in ("base", "variants", "variant_cost")}

# Months per order-export chunk. Each order export is slow (~30 min — the
# transactions group scans every order), and the Big plan runs only ONE export
# at a time, so keep the chunk COUNT low: quarterly (3) gives ~4 chunks/year,
# each ~a few thousand orders — well under the 10k/job cap and inside CI limits.
# Monthly would be ~12 chunks and overrun GitHub's 6h job ceiling.
CHUNK_MONTHS = int(os.environ.get("CHUNK_MONTHS") or 3)
POLL_SECONDS = 15
# Generous: a chunk may sit "queued" a long time behind the store's own
# recurring exports (only one export runs at a time). This bounds the wait,
# not the run.
JOB_TIMEOUT_SECONDS = 150 * 60


# ---------------------------------------------------------------------------
# Date chunking (pure, unit-testable)
# ---------------------------------------------------------------------------

def _add_months(d: dt.date, n: int) -> dt.date:
    """First of the month n months after d's month."""
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def period_chunks(since: dt.date, until: dt.date,
                  months: int = CHUNK_MONTHS) -> list[tuple[dt.date, dt.date]]:
    """Inclusive [start, end] ranges of ``months`` each, covering since..until.

    Ranges are non-overlapping and contiguous, so stitched chunks neither
    duplicate nor drop orders. The final chunk's end is clamped to ``until``.
    """
    out: list[tuple[dt.date, dt.date]] = []
    cur = since.replace(day=1)
    while cur <= until:
        nxt = _add_months(cur, months)
        end = min(nxt - dt.timedelta(days=1), until)
        out.append((max(cur, since), end))
        cur = nxt
    return out


def halve(lo: dt.date, hi: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Split a date range into two halves by day count (used when a chunk is too big)."""
    if hi <= lo:
        return [(lo, hi)]
    mid = lo + (hi - lo) / 2
    mid = mid if isinstance(mid, dt.date) else lo + dt.timedelta(days=(hi - lo).days // 2)
    return [(lo, mid), (mid + dt.timedelta(days=1), hi)]


# ---------------------------------------------------------------------------
# CSV stitching (pure, unit-testable)
# ---------------------------------------------------------------------------

def stitch_orders(chunk_csvs: list[bytes]) -> bytes:
    """Concatenate Matrixify orders CSV chunks into one gzipped CSV.

    Each chunk shares the same header and each order's multi-row block lives
    entirely within one chunk (chunks are split on created_at date), so a plain
    concat with a single header preserves the structure the dashboard expects.
    A defensive de-dup on (Name, Line: ID) drops any accidental overlap.
    """
    import pandas as pd

    frames = [pd.read_csv(io.BytesIO(b), dtype=str, low_memory=False)
              for b in chunk_csvs if b.strip()]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise SystemExit("stitch_orders: no order rows in any chunk")
    df = pd.concat(frames, ignore_index=True)
    subset = [c for c in ("Name", "Line: ID", "Line: Type", "Transaction: ID", "Refund: ID")
              if c in df.columns]
    if "Name" in df.columns:
        df = df.drop_duplicates(subset=subset or None)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(df.to_csv(index=False).encode("utf-8"))
    return buf.getvalue()


def unzip_if_needed(content: bytes, hint: str = "") -> bytes:
    """Matrixify may return a zip even with zip=false in some paths; pick the CSV."""
    if content[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if members:
                chosen = next((n for n in members if hint and hint in n.lower()), members[0])
                return zf.read(chosen)
        except zipfile.BadZipFile:
            pass
    return content


# ---------------------------------------------------------------------------
# Matrixify MCP client
# ---------------------------------------------------------------------------

class MatrixifyMCP:
    """Thin async wrapper over the Matrixify remote MCP server."""

    def __init__(self, session):
        self.session = session

    @staticmethod
    def _parse(result):
        """CallToolResult -> dict. Prefer structuredContent, else parse text JSON."""
        sc = getattr(result, "structuredContent", None)
        if isinstance(sc, dict) and sc:
            return sc
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"_text": text}
        return {}

    async def call(self, name: str, args: dict, *, retries: int = 5) -> dict:
        last = {}
        for attempt in range(retries):
            result = await self.session.call_tool(name, args)
            data = self._parse(result)
            # MCP tools surface rate limits as text/errors rather than transport errors
            blob = json.dumps(data).lower()
            if "rate limit" in blob or "retry_after" in blob:
                await asyncio.sleep(2 ** attempt)
                last = data
                continue
            return data
        return last

    async def export(self, details: dict, file_name: str) -> int:
        data = await self.call("matrixify_export_create", {
            "format": "CSV",
            "details": details,
            "job_options": {"zip": False, "file_name": file_name},
        })
        job_id = data.get("id")
        if not job_id:
            raise SystemExit(f"export_create failed for {file_name}: {data}")
        return int(job_id)

    async def wait(self, job_id: int) -> dict:
        waited = 0
        last_state = None
        while True:
            data = await self.call("matrixify_job_get", {"job_id": job_id})
            status = str(data.get("status", "")).lower()
            state = str(data.get("state", "")).lower()
            if status in ("finished", "failed", "cancelled") or "finished" in state:
                if status == "failed":
                    raise SystemExit(f"job {job_id} failed: {data.get('details')}")
                return data
            if state != last_state:  # log transitions (Queued -> In Progress -> ...)
                print(f"    job {job_id}: {state or status} ({waited}s)", flush=True)
                last_state = state
            if waited >= JOB_TIMEOUT_SECONDS:
                raise SystemExit(f"job {job_id} timed out after {waited}s (state={state})")
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS

    async def download(self, job_id: int) -> bytes:
        import httpx
        data = await self.call("matrixify_job_results_download", {"job_id": job_id})
        url = data.get("download_url")
        if not url:
            raise SystemExit(f"no download_url for job {job_id}: {data}")
        async with httpx.AsyncClient(follow_redirects=True, timeout=600) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    @staticmethod
    def orders_limited(job: dict) -> int:
        """Orders dropped by the per-job cap (0 if the chunk fit).

        NB: the job's ``count`` field is the *store* order total, not the
        chunk's exported count, so it can't gauge chunk size — only ``limited``
        reliably signals the cap was hit.
        """
        o = (job.get("details") or {}).get("orders") or {}
        return int(o.get("limited") or 0)


async def export_orders_range(mcp: MatrixifyMCP, lo: dt.date, hi: dt.date) -> list[bytes]:
    """Export one date range, splitting recursively if it nears the cap. Returns CSV bytes."""
    details = {"orders": {"include": True, "groups": ORDERS_GROUPS,
                          "filter": {"created_at": {"condition": "date range",
                                                    "date_from": lo.isoformat(),
                                                    "date_to": hi.isoformat()}}}}
    name = f"dashboard_orders_{lo.isoformat()}_{hi.isoformat()}"
    job_id = await mcp.export(details, name)
    job = await mcp.wait(job_id)
    limited = mcp.orders_limited(job)
    if limited > 0:
        if hi <= lo:
            raise SystemExit(f"single day {lo} exceeds the export cap; cannot split further")
        print(f"  chunk {lo}..{hi}: cap hit (limited={limited}) -> splitting in half", flush=True)
        parts: list[bytes] = []
        for plo, phi in halve(lo, hi):
            parts += await export_orders_range(mcp, plo, phi)
        return parts
    print(f"  chunk {lo}..{hi}: OK", flush=True)
    await asyncio.sleep(5)  # respect ~1 download / 5s throttle
    return [unzip_if_needed(await mcp.download(job_id), "order")]


async def run() -> int:
    token = os.environ.get("MATRIXIFY_MCP_TOKEN")
    if not token:
        print("MATRIXIFY_MCP_TOKEN not set", file=sys.stderr)
        return 2

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    DATA.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    since = dt.date.fromisoformat(ORDERS_SINCE)
    chunks = period_chunks(since, today)
    print(f"Refreshing via {MCP_URL}: {len(chunks)} order chunks "
          f"({CHUNK_MONTHS}-month) {since}..{today}", flush=True)

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp = MatrixifyMCP(session)

            # Products
            print("Exporting products ...", flush=True)
            pid = await mcp.export({"products": {"include": True, "groups": PRODUCTS_GROUPS}},
                                   "dashboard_products")
            pjob = await mcp.wait(pid)
            await asyncio.sleep(5)
            products_csv = unzip_if_needed(await mcp.download(pid), "product")
            PRODUCTS_OUT.write_bytes(products_csv)
            n_products = (pjob.get("details") or {}).get("products", {}).get("count")
            print(f"  wrote {PRODUCTS_OUT.name} ({n_products} products)", flush=True)

            # Orders (chunked + stitched)
            print("Exporting orders ...", flush=True)
            order_chunks: list[bytes] = []
            for lo, hi in chunks:
                order_chunks += await export_orders_range(mcp, lo, hi)
                await asyncio.sleep(5)  # throttle between export_create calls

    ORDERS_OUT.write_bytes(stitch_orders(order_chunks))
    # Report the stitched total for the run log.
    import pandas as pd
    total = pd.read_csv(ORDERS_OUT, dtype=str, low_memory=False)
    n_orders = total["Name"].ffill().nunique() if "Name" in total.columns else len(total)
    print(f"  wrote {ORDERS_OUT.name} ({len(total):,} rows, {n_orders:,} orders)", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Offline self-test (no network): exercises chunking + stitching only.
# ---------------------------------------------------------------------------

def self_test() -> int:
    import pandas as pd

    cs = period_chunks(dt.date(2025, 7, 1), dt.date(2026, 6, 4), months=3)
    assert cs[0] == (dt.date(2025, 7, 1), dt.date(2025, 9, 30)), cs[0]
    assert cs[-1] == (dt.date(2026, 4, 1), dt.date(2026, 6, 4)), cs[-1]
    assert len(cs) == 4, cs
    # contiguous, non-overlapping
    for (_, a_hi), (b_lo, _) in zip(cs, cs[1:]):
        assert b_lo == a_hi + dt.timedelta(days=1), (a_hi, b_lo)
    # monthly still works (used if CHUNK_MONTHS=1)
    assert len(period_chunks(dt.date(2025, 7, 1), dt.date(2026, 6, 4), months=1)) == 12
    h = halve(dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    assert h[0][1] + dt.timedelta(days=1) == h[1][0] and h[1][1] == dt.date(2026, 1, 31), h

    # stitch: split the committed orders file by month, re-stitch, compare order count
    src = ORDERS_OUT
    if src.exists():
        df = pd.read_csv(src, dtype=str, low_memory=False)
        df["_m"] = pd.to_datetime(df["Created At"], errors="coerce", utc=True).dt.to_period("M")
        parts = []
        for _, g in df.groupby("_m"):
            parts.append(g.drop(columns="_m").to_csv(index=False).encode("utf-8"))
        out = stitch_orders(parts)
        back = pd.read_csv(io.BytesIO(gzip.decompress(out)), dtype=str, low_memory=False)
        a, b = df["Name"].ffill().nunique(), back["Name"].ffill().nunique()
        assert a == b, f"order count changed through stitch: {a} != {b}"
        print(f"self-test OK: {len(cs)} chunks; stitch preserved {b} orders across "
              f"{len(parts)} monthly parts")
    else:
        print(f"self-test OK: {len(cs)} chunks (stitch test skipped, {src.name} absent)")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(asyncio.run(run()))
