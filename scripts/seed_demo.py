#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def main() -> None:
    app.init_db()
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with app.db() as conn:
        conn.execute(
            """
            insert into customers
              (slug, name, aliases, status, owner, region, products, overview, architecture, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(slug) do update set updated_at=excluded.updated_at
            """,
            (
                "demo-broadcast",
                "Demo Broadcast Co",
                "DBC",
                "Active",
                "Demo Owner",
                "Global",
                "MCM, MCS",
                "Demo customer used for public-safe development and screenshots.",
                "Demo architecture with on-prem ingest, cloud monitoring, and contribution-feed workflows.",
                ts,
                ts,
            ),
        )
        customer_id = conn.execute(
            "select id from customers where slug = ?", ("demo-broadcast",)
        ).fetchone()["id"]
        for name, env_type, location, source_types in (
            ("Main Facility", "On-prem", "New York", "2110, compressed, SDI"),
            ("Cloud Monitoring", "AWS", "us-east-1", "DASH, HLS, SRT"),
            ("Remote Production", "Hybrid", "London", "NDI, SRT"),
        ):
            conn.execute(
                """
                insert into environments
                  (customer_id, slug, name, env_type, location, status, products, source_types, architecture, notes, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(customer_id, slug) do update set updated_at=excluded.updated_at
                """,
                (
                    customer_id,
                    app.slugify(name),
                    name,
                    env_type,
                    location,
                    "Active",
                    "MCM, MCS",
                    source_types,
                    "Demo environment architecture notes.",
                    "Seeded by scripts/seed_demo.py.",
                    ts,
                    ts,
                ),
            )
        conn.execute(
            """
            insert into tickets
              (customer_id, key, summary, status, priority, assignee, updated, url, notes, synced_at, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(customer_id, key) do update set summary=excluded.summary
            """,
            (
                customer_id,
                "ESD-1000",
                "Demo contribution feed intermittently reports packet loss",
                "Investigating",
                "Medium",
                "Demo Assignee",
                "2026-01-15",
                "https://example.atlassian.net/browse/ESD-1000",
                "Public-safe demo ticket summary.",
                ts,
                ts,
            ),
        )
        conn.execute(
            """
            insert into staff
              (customer_id, name, role, team, email, slack_handle, notes, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                "Demo Engineer",
                "Technical contact",
                "Customer",
                "demo.engineer@example.com",
                "@demo-engineer",
                "Public-safe staff example.",
                ts,
                ts,
            ),
        )
    print("Seeded demo customer: http://127.0.0.1:8787/customers/demo-broadcast")


if __name__ == "__main__":
    main()
