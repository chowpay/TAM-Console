#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import base64
import importlib.util
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "casefiles.db"
ATLASSIAN_CONFIG = Path(
    os.environ.get(
        "CASEFILES_ATLASSIAN_CONFIG",
        str(ROOT / "config" / "atlassian_config.py"),
    )
)
LEGACY_ATLASSIAN_CONFIG = Path(
    "/home/ubuntu/tag_inspect/tools/jira_api_tools/json_exporter/config.py"
)
ALLOWED_FILE_ROOTS = (
    Path("/home/ubuntu/tag_inspect").resolve(),
    Path("/home/ubuntu/issues").resolve(),
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def slugify(value: str) -> str:
    keep = []
    for char in value.lower().strip():
        if char.isalnum():
            keep.append(char)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-") or "customer"


def normalize_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


SOURCE_TYPE_SUGGESTIONS = (
    "2110",
    "ST 2110",
    "2022-6",
    "TR-07",
    "compressed",
    "DASH",
    "HLS",
    "SRT",
    "RTMP",
    "RTSP",
    "UDP",
    "RTP",
    "NDI",
    "SDI",
    "HDMI",
    "MPEG-TS",
    "OTT",
    "WebVTT",
    "STPP",
    "Kantar",
    "Nielsen",
)


def parse_tags(value: str) -> list[str]:
    seen = set()
    tags = []
    for raw in re.split(r"[,;\n]+", value or ""):
        tag = " ".join(raw.strip().split())
        if not tag:
            continue
        key = normalize_match(tag)
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def tags_csv(value: str) -> str:
    return ", ".join(parse_tags(value))


def render_tags(value: str) -> str:
    tags = parse_tags(value)
    return " ".join(f'<span class="tag">{esc(tag)}</span>' for tag in tags) or '<span class="muted">Not set</span>'


def health_badge(value: str) -> str:
    health = (value or "Unknown").strip() or "Unknown"
    key = normalize_match(health) or "unknown"
    if key not in {"green", "yellow", "red", "unknown"}:
        key = "unknown"
    return f'<span class="health-pill health-{key}"><span class="health-dot"></span>{esc(health)}</span>'


def editable_health_badge(slug: str, value: str) -> str:
    options = []
    for option in ("Unknown", "Green", "Yellow", "Red"):
        chosen = " selected" if option.lower() == (value or "").lower() else ""
        options.append(f'<option value="{esc(option)}"{chosen}>{esc(option)}</option>')
    return f"""<form class="health-picker" method="post" action="/customers/{esc(slug)}/health">
      {health_badge(value)}
      <select name="health" aria-label="Change customer health" onchange="this.form.submit()">
        {''.join(options)}
      </select>
    </form>"""


DASHBOARD_HELP = {
    "Customers": "Customer accounts currently tracked in TAM Console.",
    "Tickets": "Distinct Jira ticket keys imported across all customers.",
    "Active tickets": "Imported tickets that are not marked done, resolved, closed, or resolution provided.",
    "Ticket links": "Total customer-to-ticket links. One Jira ticket can be linked to more than one customer.",
    "Jira orgs": "Distinct Jira Organization IDs mapped to local customers.",
    "Red customers": "Customers manually marked Red in the health field.",
    "Yellow customers": "Customers manually marked Yellow in the health field.",
    "No environments": "Customers with no environments entered yet.",
    "Tickets missing env": "Imported tickets that are not mapped to a customer environment.",
    "Staff missing env": "Staff contacts that are not mapped to any customer environment.",
    "Tickets without environment": "Imported tickets that are not mapped to a customer environment.",
    "Staff without environment": "Staff contacts that are not mapped to any customer environment.",
    "Imported customers": "Customers created from Jira import that have not been reviewed and marked active.",
    "No next action": "Customers without a next action entered in their profile.",
}


def help_label(label: str, help_text: str | None = None) -> str:
    text = help_text or DASHBOARD_HELP.get(label, "")
    if not text:
        return esc(label)
    return (
        f'<span class="label-with-help">{esc(label)}'
        f'<span class="help-dot" tabindex="0" data-help="{esc(text)}" '
        f'aria-label="{esc(label)} help: {esc(text)}">?</span></span>'
    )


def metric_card(value: object, label: str, help_text: str | None = None) -> str:
    return f'<div class="metric"><strong>{esc(value)}</strong><span class="metric-label">{help_label(label, help_text)}</span></div>'


def search_terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[A-Za-z0-9_-]+", query.lower()) if term]


def extract_ticket_keys(value: str) -> list[str]:
    seen = set()
    keys = []
    for match in re.findall(r"\b(?:ESD|CS|FR|MB)-\d+\b", value or "", flags=re.I):
        key = match.upper()
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def customer_mapping_candidates() -> list[dict]:
    customers = rows(
        """
        select id, name, aliases
        from customers
        order by is_pinned desc,
                 case when sort_order = 0 then 1 else 0 end,
                 sort_order,
                 name
        """
    )
    orgs_by_customer: dict[int, list[str]] = {}
    for org in rows("select customer_id, organization_name from customer_jira_organizations"):
        orgs_by_customer.setdefault(org["customer_id"], []).append(org["organization_name"])
    candidates = []
    for customer in customers:
        terms = [customer["name"], *parse_tags(customer["aliases"])]
        terms.extend(orgs_by_customer.get(customer["id"], []))
        normalized_terms = []
        seen = set()
        for term in terms:
            normalized = normalize_match(term)
            if len(normalized) < 3 or normalized in seen:
                continue
            seen.add(normalized)
            normalized_terms.append((normalized, term))
        candidates.append(
            {
                "id": customer["id"],
                "name": customer["name"],
                "terms": normalized_terms,
            }
        )
    return candidates


def suggest_customer_for_ticket(ticket: sqlite3.Row, candidates: list[dict]) -> dict | None:
    text = normalize_match(
        " ".join(
            [
                ticket["key"],
                ticket["summary"],
                ticket["reporter"],
                ticket["notes"],
                ticket["source"],
            ]
        )
    )
    best = None
    for candidate in candidates:
        for normalized, label in candidate["terms"]:
            if normalized not in text:
                continue
            score = len(normalized)
            if best is None or score > best["score"]:
                best = {
                    "id": candidate["id"],
                    "name": candidate["name"],
                    "label": label,
                    "score": score,
                }
    return best


def advisory_match_terms(advisory: sqlite3.Row, context_text: str) -> list[str]:
    lower_context = context_text.lower()
    normalized_context = normalize_match(context_text)
    matched = []
    for term in parse_tags(advisory["relevance_terms"]):
        term_lower = term.lower()
        normalized = normalize_match(term)
        if not normalized:
            continue
        if re.search(r"[^A-Za-z0-9\s_-]", term) and term_lower in lower_context:
            matched.append(term)
        elif len(normalized) >= 3 and normalized in normalized_context:
            matched.append(term)
    product = normalize_match(advisory["product"])
    if not matched and not parse_tags(advisory["relevance_terms"]) and product and product in normalized_context:
        matched.append(advisory["product"])
    return matched


def customer_advisory_context(customer_id: int) -> str:
    customer = row("select * from customers where id = ?", (customer_id,))
    if customer is None:
        return ""
    pieces = [
        customer["name"],
        customer["aliases"],
        customer["products"],
        customer["overview"],
        customer["architecture"],
    ]
    for env in rows("select * from environments where customer_id = ?", (customer_id,)):
        pieces.extend(
            [
                env["name"],
                env["env_type"],
                env["location"],
                env["products"],
                env["source_types"],
                env["architecture"],
                env["notes"],
            ]
        )
    for software in rows("select * from software_deployments where customer_id = ?", (customer_id,)):
        pieces.extend(
            [
                software["product"],
                software["version"],
                software["version_notes"],
                software["deployment_mode"],
                software["redundancy"],
                software["node_count"],
                software["notes"],
            ]
        )
    return " ".join(str(piece or "") for piece in pieces)


def relevant_advisories_for_customer(customer_id: int) -> list[tuple[sqlite3.Row, list[str]]]:
    context = customer_advisory_context(customer_id)
    matches = []
    for advisory in rows(
        """
        select *
        from advisories
        where lower(status) = 'active'
        order by case lower(severity)
                   when 'critical' then 0
                   when 'high' then 1
                   when 'warning' then 2
                   when 'info' then 3
                   else 4
                 end,
                 updated_at desc,
                 title
        """
    ):
        matched_terms = advisory_match_terms(advisory, context)
        if matched_terms:
            matches.append((advisory, matched_terms))
    return matches


def affected_customers_for_advisory(advisory: sqlite3.Row) -> list[tuple[sqlite3.Row, list[str]]]:
    matches = []
    for customer in rows("select id, slug, name from customers order by is_pinned desc, name"):
        matched_terms = advisory_match_terms(advisory, customer_advisory_context(customer["id"]))
        if matched_terms:
            matches.append((customer, matched_terms))
    return matches


def render_advisory_items(advisory_matches: list[tuple[sqlite3.Row, list[str]]]) -> str:
    return "".join(
        f"""<article class="item">
          <span class="tag">{esc(advisory['severity'])}</span>
          <strong>{esc(advisory['title'])}</strong>
          <p>{esc(advisory['body'])}</p>
          <dl class="facts">
            <dt>Product</dt><dd>{esc(advisory['product']) or '<span class="muted">Any</span>'}</dd>
            <dt>Affected versions</dt><dd>{esc(advisory['affected_versions']) or '<span class="muted">Not specified</span>'}</dd>
            <dt>Matched</dt><dd>{render_tags(', '.join(matched_terms))}</dd>
            <dt>Ticket</dt><dd>{f'<a href="https://tag.atlassian.net/browse/{esc(advisory["ticket_key"])}" target="_blank">{esc(advisory["ticket_key"])}</a>' if advisory['ticket_key'] else '<span class="muted">None</span>'}</dd>
            <dt>Source</dt><dd>{f'<a href="{esc(advisory["source_url"])}" target="_blank">source</a>' if advisory['source_url'] else '<span class="muted">Manual</span>'}</dd>
          </dl>
        </article>"""
        for advisory, matched_terms in advisory_matches
    )


def advisory_rows() -> list[sqlite3.Row]:
    return rows(
        """
        select *
        from advisories
        order by case lower(severity)
                   when 'critical' then 0
                   when 'high' then 1
                   when 'warning' then 2
                   when 'info' then 3
                   else 4
                 end,
                 updated_at desc,
                 title
        """
    )


def render_advisory_table(advisories: list[sqlite3.Row] | None = None) -> str:
    advisories = advisory_rows() if advisories is None else advisories
    table_rows = "".join(
        (
            lambda affected: f"""<tr>
          <td>{esc(a['title'])}</td>
          <td>{esc(a['severity'])}</td>
          <td>{esc(a['product'])}</td>
          <td>{esc(a['affected_versions'])}</td>
          <td>{render_tags(a['relevance_terms'])}</td>
          <td>{', '.join(f'<a href="/customers/{esc(customer["slug"])}">{esc(customer["name"])}</a>' for customer, _ in affected) or '<span class="muted">No matches</span>'}</td>
          <td>{f'<a href="https://tag.atlassian.net/browse/{esc(a["ticket_key"])}" target="_blank">{esc(a["ticket_key"])}</a>' if a['ticket_key'] else '<span class="muted">None</span>'}</td>
          <td>{esc(a['status'])}</td>
        </tr>"""
        )(affected_customers_for_advisory(a))
        for a in advisories
    )
    return (
        f'<div class="table-scroll"><table><thead><tr><th>Title</th><th>Severity</th><th>Product</th><th>Affected versions</th><th>Relevance terms</th><th>Affected customers</th><th>Ticket</th><th>Status</th></tr></thead><tbody>{table_rows}</tbody></table></div>'
        if table_rows
        else '<div class="empty">No advisories yet.</div>'
    )

def render_advisory_form() -> str:
    return f"""<form method="post" action="/advisories">
      <div class="grid-2">
        <label>Title<input name="title" required placeholder="MCS 1.8.4 hotfix for 1+1 deployments"></label>
        <label>Severity<select name="severity"><option>Info</option><option>Warning</option><option>High</option><option>Critical</option></select></label>
        <label>Product<input name="product" placeholder="MCS"></label>
        <label>Affected versions<input name="affected_versions" placeholder="1.8.x"></label>
        <label>Ticket key<input name="ticket_key" placeholder="MB-10121"></label>
        <label>Status<select name="status"><option>Active</option><option>Watching</option><option>Resolved</option><option>Archived</option></select></label>
      </div>
      <label>Relevance terms{render_tag_editor('relevance_terms', '', '1+1, multi-host, SCTE')}</label>
      <label>Body<textarea name="body" placeholder="What should the TAM remember when planning upgrades?"></textarea></label>
      <label>Source URL<input name="source_url" placeholder="Slack permalink, Jira, Confluence, email note"></label>
      <button type="submit">Add advisory</button>
    </form>"""


def render_tag_editor(name: str, value: str = "", placeholder: str = "") -> str:
    return f"""<div class="tag-editor" data-tags="{esc(tags_csv(value))}">
      <div class="tag-editor-tags"></div>
      <input class="tag-editor-input" list="source-type-options" placeholder="{esc(placeholder)}">
      <input type="hidden" name="{esc(name)}" value="{esc(tags_csv(value))}">
    </div>"""


def local_file_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return "/files?path=" + quote(path_or_url)


def is_image_path(path_or_url: str) -> bool:
    lower = path_or_url.lower()
    return lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


def allowed_local_file(path_text: str) -> Path | None:
    path = Path(unquote(path_text)).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    for root in ALLOWED_FILE_ROOTS:
        if resolved == root or root in resolved.parents:
            return resolved
    return None


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            create table if not exists customers (
              id integer primary key,
              slug text not null unique,
              name text not null,
              aliases text default '',
              status text default 'Active',
              owner text default '',
              region text default '',
              products text default '',
              overview text default '',
              architecture text default '',
              health text default 'Unknown',
              risk_reason text default '',
              next_action text default '',
              next_action_due text default '',
              last_touch text default '',
              is_pinned integer default 0,
              is_hidden integer default 0,
              sort_order integer default 0,
              created_at text not null,
              updated_at text not null
            );

            create table if not exists tickets (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              environment_id integer references environments(id) on delete set null,
              key text not null,
              summary text default '',
              status text default '',
              priority text default '',
              assignee text default '',
              updated text default '',
              url text default '',
              notes text default '',
              synced_at text default '',
              created_at text not null,
              unique(customer_id, key)
            );

            create table if not exists meetings (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              environment_id integer references environments(id) on delete set null,
              meeting_date text not null,
              title text not null,
              attendees text default '',
              summary text default '',
              actions text default '',
              url text default '',
              created_at text not null
            );

            create table if not exists notes (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              environment_id integer references environments(id) on delete set null,
              note_type text not null default 'General',
              title text not null,
              body text default '',
              source_url text default '',
              created_at text not null
            );

            create table if not exists artifacts (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              environment_id integer references environments(id) on delete set null,
              label text not null,
              artifact_type text default '',
              path_or_url text not null,
              notes text default '',
              created_at text not null
            );

            create table if not exists hardware (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              environment_id integer references environments(id) on delete set null,
              label text not null,
              role text default '',
              vendor text default '',
              model text default '',
              cpu text default '',
              memory text default '',
              quantity text default '',
              serials text default '',
              status text default 'Active',
              notes text default '',
              source text default '',
              confidence text default 'Needs confirmation',
              created_at text not null,
              updated_at text not null
            );

            create table if not exists software_deployments (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              environment_id integer references environments(id) on delete set null,
              product text not null,
              version text default '',
              version_notes text default '',
              deployment_mode text default '',
              redundancy text default '',
              node_count text default '',
              status text default 'Active',
              notes text default '',
              source text default '',
              confidence text default 'Needs confirmation',
              created_at text not null,
              updated_at text not null
            );

            create table if not exists environments (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              slug text not null,
              name text not null,
              env_type text default '',
              location text default '',
              status text default 'Active',
              products text default '',
              source_types text default '',
              architecture text default '',
              notes text default '',
              created_at text not null,
              updated_at text not null,
              unique(customer_id, slug)
            );

            create table if not exists staff (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              name text not null,
              role text default '',
              team text default '',
              email text default '',
              slack_handle text default '',
              notes text default '',
              created_at text not null,
              updated_at text not null
            );

            create table if not exists environment_staff (
              id integer primary key,
              environment_id integer not null references environments(id) on delete cascade,
              staff_id integer not null references staff(id) on delete cascade,
              responsibility text default '',
              created_at text not null,
              unique(environment_id, staff_id)
            );

            create table if not exists customer_jira_organizations (
              id integer primary key,
              customer_id integer not null references customers(id) on delete cascade,
              organization_id text default '',
              organization_uuid text default '',
              organization_name text not null,
              normalized_name text not null,
              match_source text default 'manual',
              created_at text not null,
              unique(customer_id, normalized_name)
            );

            create table if not exists app_settings (
              key text primary key,
              value text not null,
              updated_at text not null
            );

            create table if not exists ui_preferences (
              key text primary key,
              value text not null,
              updated_at text not null
            );

            create table if not exists unmapped_jira_tickets (
              key text primary key,
              summary text default '',
              status text default '',
              priority text default '',
              assignee text default '',
              reporter text default '',
              updated text default '',
              url text default '',
              notes text default '',
              source text default '',
              synced_at text not null,
              created_at text not null
            );

            create table if not exists advisories (
              id integer primary key,
              title text not null,
              severity text default 'Info',
              product text default '',
              affected_versions text default '',
              relevance_terms text default '',
              body text default '',
              ticket_key text default '',
              source_url text default '',
              status text default 'Active',
              created_at text not null,
              updated_at text not null
            );

            create table if not exists slack_signals (
              id integer primary key,
              customer_id integer references customers(id) on delete set null,
              channel text default '',
              source_date text default '',
              author text default '',
              permalink text default '',
              signal_type text default '',
              confidence text default '',
              summary text default '',
              raw_json text default '',
              created_at text not null
            );
            """
        )
        ts = now_utc()
        for key, value in {
            "jira_sync_projects": "ESD, CS, FR, MB",
            "jira_sync_include_assignee": "1",
            "jira_sync_include_reporter": "1",
        }.items():
            conn.execute(
                "insert or ignore into app_settings (key, value, updated_at) values (?, ?, ?)",
                (key, value, ts),
            )
        org_indexes = [
            r["name"]
            for r in conn.execute("pragma index_list(customer_jira_organizations)").fetchall()
        ]
        if "sqlite_autoindex_customer_jira_organizations_1" in org_indexes:
            conn.execute("alter table customer_jira_organizations rename to customer_jira_organizations_old")
            conn.execute(
                """
                create table customer_jira_organizations (
                  id integer primary key,
                  customer_id integer not null references customers(id) on delete cascade,
                  organization_id text default '',
                  organization_uuid text default '',
                  organization_name text not null,
                  normalized_name text not null,
                  match_source text default 'manual',
                  created_at text not null
                )
                """
            )
            conn.execute(
                """
                insert into customer_jira_organizations
                  (customer_id, organization_id, organization_uuid, organization_name, normalized_name, match_source, created_at)
                select customer_id, organization_id, organization_uuid, organization_name, normalized_name, match_source, created_at
                from customer_jira_organizations_old
                """
            )
            conn.execute("drop table customer_jira_organizations_old")
        conn.execute(
            """
            create unique index if not exists idx_customer_jira_org_id
            on customer_jira_organizations(customer_id, organization_id)
            where organization_id != ''
            """
        )
        conn.execute(
            """
            create unique index if not exists idx_customer_jira_org_manual_name
            on customer_jira_organizations(customer_id, normalized_name)
            where organization_id = ''
            """
        )
        for table in ("tickets", "meetings", "notes", "artifacts"):
            cols = {
                r["name"]
                for r in conn.execute(f"pragma table_info({table})").fetchall()
            }
            if "environment_id" not in cols:
                conn.execute(
                    f"alter table {table} add column environment_id integer references environments(id) on delete set null"
                )
        ticket_cols = {
            r["name"]
            for r in conn.execute("pragma table_info(tickets)").fetchall()
        }
        if "synced_at" not in ticket_cols:
            conn.execute("alter table tickets add column synced_at text default ''")
        env_cols = {
            r["name"]
            for r in conn.execute("pragma table_info(environments)").fetchall()
        }
        if "source_types" not in env_cols:
            conn.execute("alter table environments add column source_types text default ''")
        customer_cols = {
            r["name"]
            for r in conn.execute("pragma table_info(customers)").fetchall()
        }
        if "is_pinned" not in customer_cols:
            conn.execute("alter table customers add column is_pinned integer default 0")
        if "is_hidden" not in customer_cols:
            conn.execute("alter table customers add column is_hidden integer default 0")
        if "sort_order" not in customer_cols:
            conn.execute("alter table customers add column sort_order integer default 0")
        for column, definition in (
            ("health", "text default 'Unknown'"),
            ("risk_reason", "text default ''"),
            ("next_action", "text default ''"),
            ("next_action_due", "text default ''"),
            ("last_touch", "text default ''"),
        ):
            if column not in customer_cols:
                conn.execute(f"alter table customers add column {column} {definition}")


def rows(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return list(conn.execute(query, params).fetchall())


def row(query: str, params: tuple = ()) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(query, params).fetchone()


def app_setting(key: str, default: str = "") -> str:
    setting = row("select value from app_settings where key = ?", (key,))
    return setting["value"] if setting is not None else default


def set_app_setting(conn: sqlite3.Connection, key: str, value: str, ts: str) -> None:
    conn.execute(
        """
        insert into app_settings (key, value, updated_at)
        values (?, ?, ?)
        on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, ts),
    )


def table_width_preferences() -> dict[str, list[int]]:
    prefs: dict[str, list[int]] = {}
    for pref in rows("select key, value from ui_preferences where key like 'tam-console-column-widths:%'"):
        try:
            value = json.loads(pref["value"])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, list):
            continue
        widths = []
        for width in value:
            try:
                widths.append(int(width) if width else 0)
            except (TypeError, ValueError):
                widths.append(0)
        prefs[pref["key"]] = widths
    return prefs


def save_ui_preference(conn: sqlite3.Connection, key: str, value: str, ts: str) -> None:
    conn.execute(
        """
        insert into ui_preferences (key, value, updated_at)
        values (?, ?, ?)
        on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, ts),
    )


def jira_sync_projects_from_text(value: str) -> list[str]:
    projects = []
    seen = set()
    for raw in re.split(r"[,;\s]+", value or ""):
        project = re.sub(r"[^A-Za-z0-9_-]", "", raw).upper()
        if not project or project in seen:
            continue
        seen.add(project)
        projects.append(project)
    return projects or ["ESD", "CS", "FR", "MB"]


def jira_sync_projects() -> list[str]:
    return jira_sync_projects_from_text(app_setting("jira_sync_projects", "ESD, CS, FR, MB"))


def jira_sync_ownership_clause() -> str:
    clauses = []
    if app_setting("jira_sync_include_assignee", "1") == "1":
        clauses.append("assignee = currentUser()")
    if app_setting("jira_sync_include_reporter", "1") == "1":
        clauses.append("reporter = currentUser()")
    if not clauses:
        clauses.append("assignee = currentUser()")
    return "(" + " OR ".join(clauses) + ")"


def upsert_unmapped_jira_ticket(conn: sqlite3.Connection, issue: dict, item: dict, source: str, ts: str) -> None:
    fields = issue.get("fields", {})
    reporter = fields.get("reporter") or {}
    updated_date = str(item.get("updated_date", "")).split("T", 1)[0]
    conn.execute(
        """
        insert into unmapped_jira_tickets
          (key, summary, status, priority, assignee, reporter, updated, url, notes, source, synced_at, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(key) do update set
          summary=excluded.summary,
          status=excluded.status,
          priority=excluded.priority,
          assignee=excluded.assignee,
          reporter=excluded.reporter,
          updated=excluded.updated,
          url=excluded.url,
          notes=excluded.notes,
          source=excluded.source,
          synced_at=excluded.synced_at
        """,
        (
            item.get("key", ""),
            item.get("summary", ""),
            item.get("status", ""),
            item.get("priority", ""),
            item.get("assignee", ""),
            reporter.get("displayName", ""),
            updated_date,
            item.get("url", ""),
            item.get("brief_summary", ""),
            source,
            ts,
            ts,
        ),
    )


def extract_json_array(text: str) -> list[dict]:
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No JSON array found in proxy output")


def extract_json_records(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
        if fenced:
            data = json.loads(fenced.group(1))
        else:
            starts = [pos for pos in (text.find("["), text.find("{")) if pos != -1]
            if not starts:
                raise ValueError("No JSON found")
            start = min(starts)
            end = text.rfind("]" if text[start] == "[" else "}")
            if end <= start:
                raise ValueError("No complete JSON found")
            data = json.loads(text[start : end + 1])
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("records", "items", "signals", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []


def find_customer_for_signal(record: dict) -> sqlite3.Row | None:
    explicit = str(record.get("customer") or record.get("customer_name") or "").strip()
    haystack = " ".join(
        str(record.get(key, ""))
        for key in (
            "customer",
            "customer_name",
            "parent_summary",
            "short_summary",
            "summary",
            "customer_relevance_terms",
        )
    )
    normalized_haystack = normalize_match(haystack)
    for customer in rows("select id, slug, name, aliases from customers order by length(name) desc"):
        names = [customer["name"], *parse_tags(customer["aliases"])]
        for name in names:
            normalized_name = normalize_match(name)
            if explicit and normalize_match(explicit) == normalized_name:
                return customer
            if normalized_name and normalized_name in normalized_haystack:
                return customer
    return None


def slack_record_text(record: dict) -> str:
    replies = record.get("replies")
    reply_text = ""
    if isinstance(replies, list):
        reply_text = " ".join(
            str(reply.get("summary", ""))
            for reply in replies
            if isinstance(reply, dict)
        )
    return " ".join(
        str(record.get(key, ""))
        for key in ("parent_summary", "short_summary", "summary", "body")
    ) + " " + reply_text


def recommended_advisory_from_slack(record: dict) -> dict | None:
    recommended = record.get("recommended_tam_console_record")
    if isinstance(recommended, dict):
        return recommended
    if not record.get("advisory_candidate"):
        return None
    title = str(record.get("parent_summary") or record.get("short_summary") or record.get("summary") or "").strip()
    if not title:
        return None
    ticket_keys = extract_ticket_keys(slack_record_text(record))
    terms = record.get("customer_relevance_terms", "")
    if isinstance(terms, list):
        terms = ", ".join(str(term) for term in terms)
    return {
        "title": title[:160],
        "severity": record.get("severity", "Info"),
        "product": record.get("product", ""),
        "affected_versions": record.get("affected_versions", ""),
        "relevance_terms": terms,
        "body": slack_record_text(record).strip(),
        "ticket_key": ticket_keys[0] if ticket_keys else "",
        "source_url": record.get("permalink", ""),
        "status": "Active",
    }


def import_slack_records(text: str) -> str:
    records = extract_json_records(text)
    if not records:
        return "No Slack JSON records found."
    ts = now_utc()
    signal_count = 0
    advisory_count = 0
    with db() as conn:
        for record in records:
            customer = find_customer_for_signal(record)
            summary = str(record.get("short_summary") or record.get("summary") or record.get("parent_summary") or "").strip()
            permalink = str(record.get("permalink") or record.get("source_url") or "").strip()
            conn.execute(
                """
                insert into slack_signals
                  (customer_id, channel, source_date, author, permalink, signal_type, confidence, summary, raw_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer["id"] if customer else None,
                    str(record.get("channel") or "").strip(),
                    str(record.get("date") or record.get("parent_date") or "").strip(),
                    str(record.get("author") or record.get("parent_author") or "").strip(),
                    permalink,
                    str(record.get("signal_type") or ("advisory" if record.get("advisory_candidate") else "slack-context")),
                    str(record.get("confidence") or "").strip(),
                    summary,
                    json.dumps(record),
                    ts,
                ),
            )
            signal_count += 1
            advisory = recommended_advisory_from_slack(record)
            if advisory:
                existing = None
                if advisory.get("source_url"):
                    existing = conn.execute(
                        "select id from advisories where source_url = ?",
                        (str(advisory.get("source_url")),),
                    ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        insert into advisories
                          (title, severity, product, affected_versions, relevance_terms, body, ticket_key, source_url, status, created_at, updated_at)
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(advisory.get("title", "")),
                            str(advisory.get("severity", "Info")),
                            str(advisory.get("product", "")),
                            str(advisory.get("affected_versions", "")),
                            tags_csv(str(advisory.get("relevance_terms", ""))),
                            str(advisory.get("body", "")),
                            str(advisory.get("ticket_key", "")).upper(),
                            str(advisory.get("source_url", "")),
                            str(advisory.get("status", "Active")),
                            ts,
                            ts,
                        ),
                    )
                    advisory_count += 1
    return f"Imported {signal_count} Slack signal(s) and created {advisory_count} advisory record(s)."


def load_atlassian_config():
    config_path = ATLASSIAN_CONFIG if ATLASSIAN_CONFIG.exists() else LEGACY_ATLASSIAN_CONFIG
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing Atlassian config. Copy config/atlassian_config_sample.py "
            f"to {ATLASSIAN_CONFIG} or set CASEFILES_ATLASSIAN_CONFIG."
        )
    spec = importlib.util.spec_from_file_location("casefiles_atlassian_config", config_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load Atlassian config from {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def jira_site_base(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid Atlassian BASE_URL: {base_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def jira_request(path: str, payload: dict | None = None) -> dict:
    cfg = load_atlassian_config()
    site = jira_site_base(cfg.BASE_URL)
    url = f"{site}{path}"
    token = base64.b64encode(f"{cfg.EMAIL}:{cfg.API_TOKEN}".encode()).decode()
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Jira HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Jira request failed: {exc}") from exc


def jira_get_issue(issue_key: str) -> dict:
    fields = ",".join(["summary", "description", "status", "priority", "assignee", "reporter", "updated", "customfield_10002"])
    try:
        return jira_request(f"/rest/api/3/issue/{quote(issue_key)}?fields={quote(fields)}")
    except RuntimeError as exc:
        raise RuntimeError(f"{issue_key}: {exc}") from exc


def jql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def jira_search_issues(jql: str, max_results: int = 50) -> list[dict]:
    fields = ["summary", "description", "status", "priority", "assignee", "reporter", "updated"]
    data = jira_request(
        "/rest/api/3/search/jql",
        {
            "jql": jql,
            "fields": fields,
            "maxResults": max_results,
        },
    )
    return data.get("issues", [])


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def jira_get_issues_by_keys(issue_keys: list[str]) -> list[dict]:
    fields = ["summary", "description", "status", "priority", "assignee", "reporter", "updated", "customfield_10002"]
    issues = []
    seen = set()
    clean_keys = []
    for raw_key in issue_keys:
        key = str(raw_key or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        clean_keys.append(key)
    for key_batch in chunks(clean_keys, 80):
        jql = "issuekey in (" + ", ".join(jql_string(key) for key in key_batch) + ")"
        data = jira_request(
            "/rest/api/3/search/jql",
            {
                "jql": jql,
                "fields": fields,
                "maxResults": len(key_batch),
            },
        )
        issues.extend(data.get("issues", []))
    return issues


def adf_plain_text(value: object) -> str:
    parts = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                parts.append(text)
            for child in node.get("content", []) or []:
                walk(child)
            if node.get("type") in {"paragraph", "heading", "blockquote", "listItem"}:
                parts.append("\n")
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return " ".join(" ".join(parts).split())


def brief_text(value: object, fallback: str = "", limit: int = 180) -> str:
    text = adf_plain_text(value) if isinstance(value, (dict, list)) else str(value or "")
    text = " ".join(text.split()) or fallback
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def issue_to_ticket_item(issue: dict) -> dict:
    fields = issue.get("fields", {})
    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}
    status = fields.get("status") or {}
    key = issue.get("key", "")
    return {
        "key": key,
        "summary": fields.get("summary", ""),
        "status": status.get("name", ""),
        "priority": priority.get("name", ""),
        "assignee": assignee.get("displayName", ""),
        "updated_date": fields.get("updated", ""),
        "url": f"{jira_site_base(load_atlassian_config().BASE_URL)}/browse/{key}",
        "brief_summary": brief_text(fields.get("description"), fields.get("summary", "")),
    }


def upsert_ticket_items_with_conn(conn: sqlite3.Connection, customer_id: int, items: list[dict], ts: str) -> int:
    saved = 0
    for item in items:
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        updated_date = str(item.get("updated_date", "")).split("T", 1)[0]
        conn.execute(
            """
            insert into tickets
              (customer_id, key, summary, status, priority, assignee, updated, url, notes, synced_at, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(customer_id, key) do update set
              summary = excluded.summary,
              status = excluded.status,
              priority = excluded.priority,
              assignee = excluded.assignee,
              updated = excluded.updated,
              url = excluded.url,
              notes = case when tickets.notes = '' then excluded.notes else tickets.notes end,
              synced_at = excluded.synced_at
            """,
            (
                customer_id,
                key,
                item.get("summary", ""),
                item.get("status", ""),
                item.get("priority", ""),
                item.get("assignee", ""),
                updated_date,
                item.get("url", ""),
                item.get("brief_summary", ""),
                ts,
                ts,
            ),
        )
        saved += 1
    return saved


def upsert_ticket_items(customer_id: int, items: list[dict]) -> int:
    ts = now_utc()
    with db() as conn:
        return upsert_ticket_items_with_conn(conn, customer_id, items, ts)


def discover_jira_tickets(customer_id: int) -> str:
    customer = row("select name, aliases from customers where id = ?", (customer_id,))
    orgs = rows(
        """
        select organization_name, organization_id
        from customer_jira_organizations
        where customer_id = ?
        order by case when organization_id != '' then 0 else 1 end, organization_name
        """,
        (customer_id,),
    )
    search_specs = []
    for org in orgs:
        name = org["organization_name"].strip()
        if not name:
            continue
        if org["organization_id"]:
            search_specs.append(
                (
                    f'organization {name}',
                    f'project = ESD AND "Organizations" = {jql_string(name)} ORDER BY updated DESC',
                )
            )
        else:
            search_specs.append(
                (
                    f'text {name}',
                    f'project in (ESD, CS, FR, MB) AND text ~ {jql_string(name)} ORDER BY updated DESC',
                )
            )
    text_terms = []
    if customer:
        text_terms.append(customer["name"].strip())
        text_terms.extend(parse_tags(customer["aliases"]))
    seen_terms = {label for label, _ in search_specs}
    for term in text_terms:
        if not term:
            continue
        label = f"text {term}"
        if label in seen_terms:
            continue
        search_specs.append(
            (
                label,
                f'project in (ESD, CS, FR, MB) AND text ~ {jql_string(term)} ORDER BY updated DESC',
            )
        )
        seen_terms.add(label)

    if not search_specs:
        return "No Jira discovery terms configured for this customer."

    issues_by_key = {}
    errors = []
    for label, jql in search_specs:
        try:
            for issue in jira_search_issues(jql):
                if issue.get("key"):
                    issues_by_key[issue["key"]] = issue_to_ticket_item(issue)
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    saved = upsert_ticket_items(customer_id, list(issues_by_key.values()))
    if errors:
        return f"Discovered/imported {saved} Jira ticket(s). Errors: {'; '.join(errors[:3])}"
    return f"Discovered/imported {saved} Jira ticket(s)."


def sync_jira_ticket_keys(customer_id: int, keys: list[str], ts: str | None = None) -> dict[str, int | list[str]]:
    ts = ts or now_utc()
    stats: dict[str, int | list[str]] = {
        "total": 0,
        "refreshed": 0,
        "moved": 0,
        "kept_without_org": 0,
        "errors": [],
    }
    items_for_current = []
    clean_keys = [str(key or "").strip().upper() for key in keys if str(key or "").strip()]
    try:
        issues = jira_get_issues_by_keys(clean_keys)
    except Exception as exc:
        stats["errors"].append(str(exc))  # type: ignore[union-attr]
        return stats
    issues_by_key = {issue.get("key", "").upper(): issue for issue in issues if issue.get("key")}
    missing_keys = sorted(set(clean_keys) - set(issues_by_key))
    if missing_keys:
        stats["errors"].append(f"missing from Jira: {', '.join(missing_keys[:10])}")  # type: ignore[union-attr]
    for issue in issues:
        item = issue_to_ticket_item(issue)
        orgs = issue.get("fields", {}).get("customfield_10002") or []
        if not orgs:
            items_for_current.append(item)
            stats["kept_without_org"] += 1  # type: ignore[operator]
            continue
        with db() as conn:
            target_customer_ids = set()
            for org in orgs:
                target_customer_id = find_or_create_customer_for_org(conn, org, ts)
                target_customer_ids.add(target_customer_id)
                upsert_ticket_items_with_conn(conn, target_customer_id, [item], ts)
            if customer_id not in target_customer_ids:
                conn.execute(
                    "delete from tickets where customer_id = ? and key = ?",
                    (customer_id, key),
                )
                stats["moved"] += 1  # type: ignore[operator]
            stats["refreshed"] += 1  # type: ignore[operator]

    synced = upsert_ticket_items(customer_id, items_for_current) if items_for_current else 0
    stats["total"] = int(stats["refreshed"]) + synced
    return stats


def sync_jira_tickets(customer_id: int) -> str:
    keys = [
        r["key"]
        for r in rows(
            "select key from tickets where customer_id = ? order by key",
            (customer_id,),
        )
        if r["key"]
    ]
    if not keys:
        return "No linked Jira tickets to sync yet."

    stats = sync_jira_ticket_keys(customer_id, keys)
    details = []
    if stats["moved"]:
        details.append(f"moved {stats['moved']}")
    if stats["kept_without_org"]:
        details.append(f"kept {stats['kept_without_org']} without Jira Organization")
    errors = stats["errors"]
    if errors:
        details.append(f"errors: {'; '.join(errors[:3])}")  # type: ignore[index]
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"Synced {stats['total']} Jira ticket(s) at {now_utc()}{suffix}."


def unique_customer_slug(conn: sqlite3.Connection, name: str) -> str:
    slug = slugify(name)
    suffix = 2
    base = slug
    while conn.execute("select 1 from customers where slug = ?", (slug,)).fetchone():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def find_or_create_customer_for_org(conn: sqlite3.Connection, org: dict, ts: str) -> int:
    org_id = str(org.get("id", "") or "")
    org_name = str(org.get("name", "") or "").strip()
    normalized = normalize_match(org_name)
    existing = None
    if org_id:
        existing = conn.execute(
            "select customer_id from customer_jira_organizations where organization_id = ?",
            (org_id,),
        ).fetchone()
    if existing is None and normalized:
        existing = conn.execute(
            "select customer_id from customer_jira_organizations where normalized_name = ?",
            (normalized,),
        ).fetchone()
    if existing is not None:
        customer_id = existing["customer_id"]
    else:
        customer = conn.execute(
            "select id from customers where slug = ? or lower(name) = ?",
            (slugify(org_name), org_name.lower()),
        ).fetchone()
        if customer is not None:
            customer_id = customer["id"]
        else:
            slug = unique_customer_slug(conn, org_name)
            conn.execute(
                """
                insert into customers (slug, name, status, overview, created_at, updated_at)
                values (?, ?, 'Imported', 'Imported from assigned Jira issue Organizations.', ?, ?)
                """,
                (slug, org_name, ts, ts),
            )
            customer_id = conn.execute("select last_insert_rowid() as id").fetchone()["id"]

    if org_id:
        conn.execute(
            """
            insert into customer_jira_organizations
              (customer_id, organization_id, organization_uuid, organization_name, normalized_name, match_source, created_at)
            values (?, ?, ?, ?, ?, 'assigned-ticket-import', ?)
            on conflict(customer_id, organization_id) where organization_id != '' do update set
              organization_uuid = excluded.organization_uuid,
              organization_name = excluded.organization_name,
              normalized_name = excluded.normalized_name,
              match_source = excluded.match_source
            """,
            (
                customer_id,
                org_id,
                str(org.get("uuid", "") or org.get("_links", {}).get("self", "") or ""),
                org_name,
                normalized,
                ts,
            ),
        )
    else:
        conn.execute(
            """
            insert into customer_jira_organizations
              (customer_id, organization_id, organization_uuid, organization_name, normalized_name, match_source, created_at)
            values (?, '', ?, ?, ?, 'assigned-ticket-import', ?)
            on conflict(customer_id, normalized_name) where organization_id = '' do update set
              organization_uuid = excluded.organization_uuid,
              organization_name = excluded.organization_name,
              match_source = excluded.match_source
            """,
            (
                customer_id,
                str(org.get("uuid", "") or org.get("_links", {}).get("self", "") or ""),
                org_name,
                normalized,
                ts,
            ),
        )
    return customer_id


def import_assigned_jira_tickets() -> str:
    before_tickets = row("select count(*) as n from tickets")["n"]
    before_customers = row("select count(*) as n from customers")["n"]
    before_orgs = row("select count(*) as n from customer_jira_organizations")["n"]
    projects = jira_sync_projects()
    jql = f"{jira_sync_ownership_clause()} AND project in ({', '.join(projects)}) ORDER BY updated DESC"
    issues = []
    next_token = None
    while True:
        payload = {
            "jql": jql,
            "fields": ["summary", "description", "status", "priority", "assignee", "reporter", "updated", "customfield_10002"],
            "maxResults": 100,
        }
        if next_token:
            payload["nextPageToken"] = next_token
        data = jira_request("/rest/api/3/search/jql", payload)
        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if data.get("isLast") or not next_token:
            break

    ts = now_utc()
    imported_keys = set()
    org_ids = set()
    queued_no_org = 0
    with db() as conn:
        for issue in issues:
            item = issue_to_ticket_item(issue)
            orgs = issue.get("fields", {}).get("customfield_10002") or []
            if not orgs:
                existing_links = conn.execute(
                    "select distinct customer_id from tickets where key = ?",
                    (item.get("key", ""),),
                ).fetchall()
                if existing_links:
                    for link in existing_links:
                        upsert_ticket_items_with_conn(conn, link["customer_id"], [item], ts)
                        imported_keys.add(f"{link['customer_id']}:{item.get('key', '')}")
                else:
                    upsert_unmapped_jira_ticket(conn, issue, item, "jira-sync-no-organization", ts)
                    queued_no_org += 1
                continue
            for org in orgs:
                customer_id = find_or_create_customer_for_org(conn, org, ts)
                org_ids.add(str(org.get("id", "") or org.get("name", "")))
                upsert_ticket_items_with_conn(conn, customer_id, [item], ts)
                imported_keys.add(f"{customer_id}:{item.get('key', '')}")

    after_tickets = row("select count(*) as n from tickets")["n"]
    after_customers = row("select count(*) as n from customers")["n"]
    after_orgs = row("select count(*) as n from customer_jira_organizations")["n"]
    return (
        f"Processed {len(imported_keys)} Jira-owned ticket link(s) across {len(org_ids)} Jira organization(s). "
        f"Added {after_tickets - before_tickets} ticket link(s), "
        f"{after_customers - before_customers} customer(s), and {after_orgs - before_orgs} org mapping(s). "
        f"Queued {queued_no_org} ticket(s) with no Organization for manual mapping."
    )


def sync_jira() -> str:
    import_message = import_assigned_jira_tickets()
    tickets_by_customer: dict[int, list[str]] = {}
    for ticket in rows("select customer_id, key from tickets where key != '' order by customer_id, key"):
        tickets_by_customer.setdefault(ticket["customer_id"], []).append(ticket["key"])

    ts = now_utc()
    totals = {
        "total": 0,
        "moved": 0,
        "kept_without_org": 0,
        "errors": [],
    }
    for customer_id, keys in tickets_by_customer.items():
        stats = sync_jira_ticket_keys(customer_id, keys, ts)
        totals["total"] += int(stats["total"])  # type: ignore[arg-type]
        totals["moved"] += int(stats["moved"])  # type: ignore[arg-type]
        totals["kept_without_org"] += int(stats["kept_without_org"])  # type: ignore[arg-type]
        totals["errors"].extend(stats["errors"])  # type: ignore[union-attr,arg-type]

    details = [
        import_message,
        f"Refreshed {totals['total']} existing local ticket link(s).",
    ]
    if totals["moved"]:
        details.append(f"Moved {totals['moved']} ticket link(s) based on Jira Organizations.")
    if totals["kept_without_org"]:
        details.append(f"Kept {totals['kept_without_org']} ticket link(s) without Jira Organization.")
    if totals["errors"]:
        details.append(f"Errors: {'; '.join(totals['errors'][:3])}")  # type: ignore[index]
    return " ".join(details)


def render_artifact_item(artifact: sqlite3.Row) -> str:
    path_or_url = artifact["path_or_url"]
    url = local_file_url(path_or_url)
    preview = (
        f'<a href="{esc(url)}" target="_blank"><img class="artifact-preview" src="{esc(url)}" alt="{esc(artifact["label"])}"></a>'
        if is_image_path(path_or_url)
        else ""
    )
    return f"""<article class="item">
      <strong>{esc(artifact['label'])}</strong> <span class="tag">{esc(artifact['artifact_type'])}</span>
      {preview}
      <p><a href="{esc(url)}" target="_blank">{esc(path_or_url)}</a></p>
      <p class="muted">{esc(artifact['environment_name']) or 'Customer-wide'} · {esc(artifact['notes'])}</p>
    </article>"""


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} | TAM Console</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101316;
      --panel: #181d22;
      --panel-2: #20262d;
      --ink: #eef2f6;
      --muted: #9aa7b5;
      --line: #313944;
      --accent: #56a3ff;
      --accent-ink: #07111c;
      --warn: #ffc45c;
      --input: #11161b;
    }}
    :root[data-theme="light"] {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --panel-2: #eaf2fb;
      --ink: #17202a;
      --muted: #627084;
      --line: #d9dee7;
      --accent: #0b6bcb;
      --accent-ink: #ffffff;
      --warn: #a15c00;
      --input: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      font-size: 15px;
      line-height: 1.45;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    header {{
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    header strong {{ font-size: 17px; }}
    .header-actions {{ display: flex; align-items: center; gap: 10px; }}
    .global-search {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 0;
      border: 0;
      background: transparent;
    }}
    .global-search input {{
      width: min(34vw, 420px);
      min-width: 220px;
      padding: 7px 9px;
    }}
    .global-search button {{ padding: 7px 10px; }}
    main {{ width: min(1920px, 100%); margin: 0 auto; padding: 24px 32px; }}
    .layout {{ display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 24px; }}
    :root[data-sidebar="collapsed"] .layout {{ grid-template-columns: 44px minmax(0, 1fr); }}
    .sidebar, .section, .item, form {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .sidebar {{ padding: 14px; height: fit-content; }}
    :root[data-sidebar="collapsed"] .sidebar {{ padding: 8px; overflow: hidden; }}
    :root[data-sidebar="collapsed"] .sidebar-content {{ display: none; }}
    .customer-link {{
      display: block;
      padding: 9px 10px;
      border-radius: 6px;
      color: var(--ink);
    }}
    .customer-link.active, .customer-link:hover {{ background: var(--panel-2); text-decoration: none; }}
    .customer-row {{
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr) auto;
      gap: 6px;
      align-items: stretch;
      margin-bottom: 4px;
    }}
    .customer-row.hidden-customer-row {{ grid-template-columns: minmax(0, 1fr) auto; }}
    .map-customer-actions {{
      display: flex;
      gap: 6px;
      align-items: center;
      padding: 0;
      border: 0;
      background: transparent;
      margin: 0 0 10px;
    }}
    .map-customer-select {{
      min-width: 220px;
    }}
    .map-cell {{
      display: flex;
      gap: 8px;
      align-items: center;
      min-width: 360px;
    }}
    .map-suggestion {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .map-suggestion strong {{ color: var(--ink); font-weight: 650; }}
    .approve-map-button {{
      width: 72px;
      min-width: 72px;
      padding-left: 0;
      padding-right: 0;
      text-align: center;
      white-space: nowrap;
    }}
    .map-customer-button {{
      width: 104px;
      min-width: 104px;
      padding-left: 0;
      padding-right: 0;
      text-align: center;
      white-space: nowrap;
    }}
    .customer-bulk-check {{
      width: 16px;
      height: 16px;
      align-self: center;
      justify-self: center;
    }}
    .customer-bulk-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 10px;
      padding: 0;
      border: 0;
      background: transparent;
    }}
    .customer-bulk-actions button:disabled {{
      cursor: not-allowed;
      opacity: .45;
    }}
    .customer-bulk-count {{
      color: var(--muted);
      font-size: 12px;
    }}
    .customer-tools {{
      display: flex;
      gap: 3px;
      align-items: center;
    }}
    .customer-tools form {{
      padding: 0;
      border: 0;
      background: transparent;
    }}
    .icon-button {{
      min-width: 24px;
      height: 24px;
      padding: 0 5px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--ink);
      font-size: 12px;
      line-height: 1;
    }}
    .pin-mark {{ color: var(--warn); font-size: 12px; margin-left: 4px; }}
    .customer-search {{ margin: 8px 0 10px; }}
    .sidebar-group {{
      margin-top: 12px;
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }}
    .sidebar-group summary {{
      display: flex;
      justify-content: space-between;
      width: 100%;
      border: 0;
      border-radius: 6px;
      padding: 7px 8px;
      background: var(--panel-2);
      color: var(--ink);
      font-size: 13px;
    }}
    .sidebar-group summary span {{ color: var(--muted); }}
    .sidebar-group .customer-row:first-of-type {{ margin-top: 8px; }}
    .stack {{ display: grid; gap: 12px; align-content: start; }}
    .section {{ padding: 18px; }}
    .section h2, .section h3 {{ margin: 0 0 12px; font-size: 18px; }}
    .muted {{ color: var(--muted); }}
    .grid-2 {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .facts {{ display: grid; grid-template-columns: 150px 1fr; gap: 8px 14px; }}
    .facts dt {{ color: var(--muted); }}
    .facts dd {{ margin: 0; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ text-align: left; vertical-align: top; padding: 9px 8px; border-bottom: 1px solid var(--line); }}
    th {{
      position: relative;
      color: var(--muted);
      font-weight: 600;
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    td {{ overflow-wrap: anywhere; }}
    th.sortable {{ cursor: pointer; user-select: none; }}
    th.sortable::after {{ content: "^v"; margin-left: 6px; font-size: 11px; opacity: .55; }}
    th.sortable.sort-asc::after {{ content: "^"; opacity: .9; }}
    th.sortable.sort-desc::after {{ content: "v"; opacity: .9; }}
    .column-resizer {{
      position: absolute;
      top: 0;
      right: -3px;
      width: 8px;
      height: 100%;
      cursor: col-resize;
      touch-action: none;
      z-index: 1;
    }}
    .column-resizer::after {{
      content: "";
      position: absolute;
      top: 8px;
      bottom: 8px;
      left: 3px;
      width: 1px;
      background: transparent;
    }}
    th:hover .column-resizer::after,
    .column-resizer:hover::after {{
      background: var(--accent);
    }}
    body.resizing-column {{
      cursor: col-resize;
      user-select: none;
    }}
    th:first-child, td:first-child {{ min-width: 96px; white-space: nowrap; }}
    .item {{ padding: 14px; }}
    .item + .item {{ margin-top: 10px; }}
    .tag {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; font-size: 12px; color: var(--muted); }}
    form {{ padding: 16px; display: grid; gap: 10px; }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 13px; }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      color: var(--ink);
      background: var(--input);
    }}
    textarea {{ min-height: 96px; resize: vertical; }}
    button {{
      justify-self: start;
      border: 0;
      border-radius: 6px;
      padding: 9px 13px;
      background: var(--accent);
      color: var(--accent-ink);
      font-weight: 650;
      cursor: pointer;
    }}
    button.is-working {{
      opacity: .75;
      cursor: wait;
    }}
    .status-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 0;
    }}
    .status-panel summary {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      border: 0;
      border-radius: 8px;
      background: var(--panel-2);
      padding: 10px 12px;
    }}
    .status-panel p {{
      margin: 0;
      padding: 12px;
      color: var(--muted);
    }}
    .status-panel-actions {{
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .status-panel-actions button {{
      padding: 5px 8px;
      font-size: 12px;
      background: var(--panel);
      color: var(--ink);
      border: 1px solid var(--line);
    }}
    .progress-shell {{
      position: relative;
      height: 8px;
      margin: 0 12px 12px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--input);
    }}
    .progress-shell::before {{
      content: "";
      position: absolute;
      top: 0;
      bottom: 0;
      left: -35%;
      width: 35%;
      border-radius: 999px;
      background: var(--accent);
      animation: progress-slide 1.2s ease-in-out infinite;
    }}
    @keyframes progress-slide {{
      0% {{ left: -35%; }}
      55% {{ left: 55%; }}
      100% {{ left: 100%; }}
    }}
    .running-panel {{ display: none; }}
    .running-panel.visible {{ display: block; }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 8px 0 14px;
    }}
    .actions form {{ padding: 0; }}
    .filterbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin: 0 0 12px;
    }}
    .filterbar input {{ max-width: 360px; }}
    .segmented {{
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
    }}
    .segmented button {{
      border-radius: 0;
      border-right: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--ink);
      padding: 8px 10px;
    }}
    .segmented button:last-child {{ border-right: 0; }}
    .segmented button.active {{ background: var(--accent); color: var(--accent-ink); }}
    .filter-count {{ color: var(--muted); font-size: 13px; }}
    .env-map {{ display: grid; gap: 8px; }}
    .check-row {{
      display: grid;
      grid-template-columns: auto minmax(120px, .45fr) minmax(160px, 1fr);
      gap: 8px;
      align-items: center;
      color: var(--ink);
    }}
    .check-row input[type="checkbox"] {{ width: auto; }}
    .source-tickets {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: var(--input);
    }}
    .source-tickets ul {{ margin: 8px 0 0; padding-left: 18px; }}
    .source-tickets li {{ margin: 4px 0; }}
    .staff-table .staff-edit-row td {{
      padding: 0 8px 12px;
      background: color-mix(in srgb, var(--panel-2) 45%, transparent);
    }}
    .staff-edit-panel {{ display: none; margin: 6px 0 0; }}
    .staff-edit-panel.open {{ display: block; }}
    .staff-edit-panel form {{ width: 100%; }}
    .theme-button {{
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--ink);
      padding: 7px 10px;
    }}
    .sidebar-toggle {{
      width: 28px;
      height: 28px;
      padding: 0;
      display: inline-grid;
      place-items: center;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--ink);
    }}
    .tabs {{
      display: flex;
      gap: 6px;
      flex-wrap: nowrap;
      align-items: center;
      margin: 0 0 16px;
      position: sticky;
      top: 56px;
      z-index: 1;
      background: var(--bg);
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
      overflow-x: auto;
      overflow-y: hidden;
      scrollbar-width: thin;
      min-height: 42px;
      max-height: 48px;
    }}
    .tabs a {{
      display: inline-flex;
      align-items: center;
      color: var(--ink);
      padding: 5px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      white-space: nowrap;
      font-size: 13px;
      line-height: 1.2;
      min-height: 28px;
    }}
    .tabs a.active {{ background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }}
    .tabs a.danger {{ border-color: #d84a4a; color: #ff8b8b; }}
    .tabs a.danger.active {{ background: #d84a4a; border-color: #d84a4a; color: #fff; }}
    .dashboard-panel[hidden] {{ display: none; }}
    .dashboard-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
    .metric {{ padding: 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .metric strong {{ display: block; font-size: 26px; line-height: 1.1; }}
    .metric .metric-label {{ color: var(--muted); font-size: 13px; }}
    .label-with-help {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: inherit;
    }}
    .help-dot {{
      position: relative;
      display: inline-grid;
      place-items: center;
      width: 15px;
      height: 15px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--panel-2);
      font-size: 10px;
      font-weight: 800;
      line-height: 1;
      cursor: help;
      flex: 0 0 auto;
    }}
    .help-dot::after {{
      content: attr(data-help);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 8px);
      transform: translateX(-50%);
      display: none;
      width: max-content;
      max-width: 280px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--input);
      color: var(--ink);
      box-shadow: 0 8px 24px rgba(0, 0, 0, .28);
      font-size: 12px;
      font-weight: 500;
      line-height: 1.35;
      text-align: left;
      white-space: normal;
      z-index: 20;
      pointer-events: none;
    }}
    .help-dot::before {{
      content: "";
      position: absolute;
      left: 50%;
      bottom: calc(100% + 3px);
      transform: translateX(-50%) rotate(45deg);
      display: none;
      width: 8px;
      height: 8px;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      background: var(--input);
      z-index: 21;
      pointer-events: none;
    }}
    .help-dot:hover,
    .help-dot:focus {{
      color: var(--ink);
      border-color: var(--accent);
      outline: none;
    }}
    .help-dot:hover::after,
    .help-dot:hover::before,
    .help-dot:focus::after,
    .help-dot:focus::before {{
      display: block;
    }}
    .panel-grid {{ display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr); gap: 16px; }}
    .health-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: var(--panel-2);
    }}
    .health-dot {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--muted);
      box-shadow: 0 0 0 2px color-mix(in srgb, currentColor 18%, transparent);
    }}
    .health-green {{ color: #62d26f; }}
    .health-green .health-dot {{ background: #62d26f; }}
    .health-yellow {{ color: #ffd166; }}
    .health-yellow .health-dot {{ background: #ffd166; }}
    .health-red {{ color: #ff7a7a; }}
    .health-red .health-dot {{ background: #ff7a7a; }}
    .health-unknown {{ color: var(--muted); }}
    .health-select {{ border-left-width: 6px; }}
    .health-select.health-green {{ border-left-color: #62d26f; }}
    .health-select.health-yellow {{ border-left-color: #ffd166; }}
    .health-select.health-red {{ border-left-color: #ff7a7a; }}
    .health-select.health-unknown {{ border-left-color: var(--muted); }}
    .health-picker {{
      position: relative;
      display: inline-block;
      padding: 0;
      border: 0;
      background: transparent;
    }}
    .health-picker .health-pill {{
      cursor: pointer;
    }}
    .health-picker select {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      opacity: 0;
      cursor: pointer;
    }}
    .gap-list {{ display: grid; gap: 8px; }}
    .gap-item {{ display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }}
    .empty {{ color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; padding: 16px; }}
    .table-scroll {{ width: 100%; overflow-x: auto; }}
    .table-scroll table {{ min-width: 1180px; }}
    .artifact-preview {{
      display: block;
      width: min(100%, 1200px);
      max-height: 520px;
      object-fit: contain;
      margin: 12px 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--input);
    }}
    details {{ margin-top: 12px; }}
    summary {{
      display: inline-block;
      cursor: pointer;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 10px;
      background: var(--panel-2);
      color: var(--ink);
      font-weight: 650;
    }}
    details form {{ margin-top: 10px; }}
    .tag-editor {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
      background: var(--input);
    }}
    .tag-editor-tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      background: var(--panel-2);
      color: var(--ink);
      font-size: 13px;
    }}
    .tag-chip button {{
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 0;
      font-weight: 800;
      cursor: pointer;
    }}
    .tag-editor-input {{
      flex: 1 1 160px;
      min-width: 140px;
      border: 0;
      padding: 4px;
      background: transparent;
    }}
    #tickets.section {{ padding-left: 16px; padding-right: 16px; }}
    @media (max-width: 860px) {{
      .layout, .grid-2, .dashboard-grid, .panel-grid {{ grid-template-columns: 1fr; }}
      header {{ padding: 0 14px; }}
      main {{ padding: 14px; }}
      .table-scroll table {{ min-width: 920px; }}
    }}
  </style>
</head>
<body>
  <script>
    const savedTheme = localStorage.getItem("tam-console-theme") || "dark";
    document.documentElement.dataset.theme = savedTheme;
    const savedSidebar = localStorage.getItem("tam-console-sidebar") || "collapsed";
    document.documentElement.dataset.sidebar = savedSidebar;
    const serverTableWidths = {json.dumps(table_width_preferences())};
    function toggleTheme() {{
      const current = document.documentElement.dataset.theme || "dark";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("tam-console-theme", next);
      const button = document.getElementById("theme-toggle");
      if (button) button.textContent = next === "dark" ? "Light mode" : "Dark mode";
    }}
    window.addEventListener("DOMContentLoaded", () => {{
      const button = document.getElementById("theme-toggle");
      if (button) button.textContent = (document.documentElement.dataset.theme || "dark") === "dark" ? "Light mode" : "Dark mode";
      const sidebarButton = document.getElementById("sidebar-toggle");
      if (sidebarButton) sidebarButton.textContent = (document.documentElement.dataset.sidebar || "collapsed") === "collapsed" ? ">" : "<";
    }});
    function toggleSidebar() {{
      const current = document.documentElement.dataset.sidebar || "collapsed";
      const next = current === "collapsed" ? "open" : "collapsed";
      document.documentElement.dataset.sidebar = next;
      localStorage.setItem("tam-console-sidebar", next);
      const button = document.getElementById("sidebar-toggle");
      if (button) button.textContent = next === "collapsed" ? ">" : "<";
    }}
    function normalizeTag(tag) {{
      return tag.trim().replace(/\\s+/g, " ");
    }}
    function initTagEditor(editor) {{
      const input = editor.querySelector(".tag-editor-input");
      const hidden = editor.querySelector("input[type=hidden]");
      const container = editor.querySelector(".tag-editor-tags");
      let tags = (hidden.value || editor.dataset.tags || "")
        .split(",")
        .map(normalizeTag)
        .filter(Boolean);
      function sync() {{
        const seen = new Set();
        tags = tags.filter((tag) => {{
          const key = tag.toLowerCase().replace(/[^a-z0-9]+/g, "");
          if (!key || seen.has(key)) return false;
          seen.add(key);
          return true;
        }});
        hidden.value = tags.join(", ");
        container.innerHTML = "";
        tags.forEach((tag, index) => {{
          const chip = document.createElement("span");
          chip.className = "tag-chip";
          chip.textContent = tag;
          const remove = document.createElement("button");
          remove.type = "button";
          remove.textContent = "x";
          remove.onclick = () => {{
            tags.splice(index, 1);
            sync();
          }};
          chip.appendChild(remove);
          container.appendChild(chip);
        }});
      }}
      function addFromInput() {{
        const pieces = input.value.split(/[,;\\n]+/).map(normalizeTag).filter(Boolean);
        if (!pieces.length) return false;
        tags.push(...pieces);
        input.value = "";
        sync();
        return true;
      }}
      input.addEventListener("keydown", (event) => {{
        if (event.key === "Enter" || event.key === ",") {{
          event.preventDefault();
          addFromInput();
        }} else if (event.key === "Backspace" && !input.value && tags.length) {{
          tags.pop();
          sync();
        }}
      }});
      input.addEventListener("blur", addFromInput);
      sync();
    }}
    function sortValue(cell) {{
      const text = (cell.textContent || "").trim();
      const date = Date.parse(text);
      if (/^\\d{{4}}-\\d{{2}}-\\d{{2}}/.test(text) && !Number.isNaN(date)) {{
        return {{ type: "number", value: date }};
      }}
      const ticket = text.match(/^([A-Z]+)-(\\d+)$/);
      if (ticket) {{
        return {{ type: "string", value: `${{ticket[1]}}-${{String(Number(ticket[2])).padStart(10, "0")}}` }};
      }}
      const numeric = text.replace(/,/g, "").match(/^-?\\d+(?:\\.\\d+)?$/);
      if (numeric) {{
        return {{ type: "number", value: Number(numeric[0]) }};
      }}
      return {{ type: "string", value: text.toLowerCase() }};
    }}
    function compareCells(aCell, bCell, direction) {{
      const a = sortValue(aCell);
      const b = sortValue(bCell);
      let result = 0;
      if (a.type === "number" && b.type === "number") {{
        result = a.value - b.value;
      }} else {{
        result = String(a.value).localeCompare(String(b.value), undefined, {{ numeric: true, sensitivity: "base" }});
      }}
      return direction === "asc" ? result : -result;
    }}
    function initSortableTables() {{
      document.querySelectorAll("table").forEach((table) => {{
        const tbody = table.tBodies[0];
        if (!tbody || !table.tHead) return;
        Array.from(table.tHead.rows[0].cells).forEach((th, index) => {{
          th.classList.add("sortable");
          th.title = "Sort";
          th.addEventListener("click", () => {{
            if (th.dataset.resized === "1") {{
              th.dataset.resized = "0";
              return;
            }}
            const current = th.classList.contains("sort-asc") ? "asc" : th.classList.contains("sort-desc") ? "desc" : "";
            const direction = current === "asc" ? "desc" : "asc";
            Array.from(table.tHead.rows[0].cells).forEach((cell) => cell.classList.remove("sort-asc", "sort-desc"));
            th.classList.add(direction === "asc" ? "sort-asc" : "sort-desc");
            const rows = Array.from(tbody.rows);
            rows.sort((a, b) => compareCells(a.cells[index] || a, b.cells[index] || b, direction));
            rows.forEach((row) => tbody.appendChild(row));
          }});
        }});
      }});
    }}
    function initResizableTables() {{
      if (window.matchMedia("(max-width: 860px)").matches) return;
      function tableStorageKey(table, index) {{
        const headings = Array.from(table.tHead?.rows[0]?.cells || [])
          .map((cell) => (cell.textContent || "").replace(/[\\^v?]/g, "").trim().toLowerCase())
          .join("|");
        return `tam-console-column-widths:${{location.pathname}}:${{index}}:${{headings}}`;
      }}
      function applyColumnWidth(table, columnIndex, width) {{
        const nextWidth = Math.max(72, Number(width) || 72);
        const th = table.tHead?.rows[0]?.cells[columnIndex];
        if (th) {{
          th.style.width = `${{nextWidth}}px`;
          th.style.minWidth = `${{nextWidth}}px`;
        }}
        Array.from(table.tBodies).forEach((tbody) => {{
          Array.from(tbody.rows).forEach((row) => {{
            const cell = row.cells[columnIndex];
            if (cell) {{
              cell.style.width = `${{nextWidth}}px`;
              cell.style.minWidth = `${{nextWidth}}px`;
            }}
          }});
        }});
      }}
      function storedWidths(key) {{
        if (Array.isArray(serverTableWidths[key])) return serverTableWidths[key];
        try {{
          const value = JSON.parse(localStorage.getItem(key) || "[]");
          return Array.isArray(value) ? value : [];
        }} catch {{
          return [];
        }}
      }}
      function saveWidths(key, widths) {{
        serverTableWidths[key] = widths;
        localStorage.setItem(key, JSON.stringify(widths));
        fetch("/ui/table-widths", {{
          method: "POST",
          body: new URLSearchParams({{ key, widths: JSON.stringify(widths) }}),
          headers: {{ "Accept": "application/json", "X-Requested-With": "fetch" }},
        }}).catch(() => {{}});
      }}
      document.querySelectorAll("table").forEach((table, tableIndex) => {{
        if (!table.tHead) return;
        table.dataset.tableIndex = table.dataset.tableIndex || String(tableIndex);
        const storageKey = tableStorageKey(table, tableIndex);
        const widths = storedWidths(storageKey);
        widths.forEach((width, columnIndex) => {{
          if (width) applyColumnWidth(table, columnIndex, width);
        }});
        Array.from(table.tHead.rows[0].cells).forEach((th, columnIndex) => {{
          if (th.querySelector(".column-resizer")) return;
          const resizer = document.createElement("span");
          resizer.className = "column-resizer";
          resizer.title = "Drag to resize column";
          th.appendChild(resizer);
          resizer.addEventListener("click", (event) => event.stopPropagation());
          resizer.addEventListener("pointerdown", (event) => {{
            event.preventDefault();
            event.stopPropagation();
            const startX = event.clientX;
            const startWidth = th.getBoundingClientRect().width;
            document.body.classList.add("resizing-column");
            resizer.setPointerCapture(event.pointerId);
            function onMove(moveEvent) {{
              const nextWidth = Math.max(72, startWidth + moveEvent.clientX - startX);
              applyColumnWidth(table, columnIndex, nextWidth);
              th.dataset.resized = "1";
            }}
            function onUp(upEvent) {{
              const saved = storedWidths(storageKey);
              saved[columnIndex] = Math.round(th.getBoundingClientRect().width);
              saveWidths(storageKey, saved);
              document.body.classList.remove("resizing-column");
              resizer.releasePointerCapture(upEvent.pointerId);
              resizer.removeEventListener("pointermove", onMove);
              resizer.removeEventListener("pointerup", onUp);
              resizer.removeEventListener("pointercancel", onUp);
            }}
            resizer.addEventListener("pointermove", onMove);
            resizer.addEventListener("pointerup", onUp);
            resizer.addEventListener("pointercancel", onUp);
          }});
        }});
      }});
    }}
    function initCustomerSearch() {{
      const input = document.getElementById("customer-search");
      if (!input) return;
      input.addEventListener("input", () => {{
        const query = input.value.trim().toLowerCase();
        document.querySelectorAll(".sidebar-group").forEach((group) => {{
          if (query) group.open = true;
        }});
        document.querySelectorAll(".customer-row").forEach((row) => {{
          const text = row.textContent.toLowerCase();
          row.style.display = !query || text.includes(query) ? "" : "none";
        }});
      }});
    }}
    function initCustomerBulkActions() {{
      const form = document.getElementById("bulk-hide-form");
      const button = document.getElementById("bulk-hide-button");
      const count = document.getElementById("bulk-hide-count");
      const checks = Array.from(document.querySelectorAll("[data-customer-bulk-check]"));
      if (!form || !button || !count || !checks.length) return;
      function update() {{
        const selected = checks.filter((check) => check.checked).length;
        button.disabled = selected === 0;
        count.textContent = selected ? `${{selected}} selected` : "None selected";
      }}
      checks.forEach((check) => check.addEventListener("change", update));
      update();
    }}
    function initTicketFilters() {{
      const table = document.querySelector("[data-ticket-table]");
      const search = document.getElementById("ticket-search");
      const buttons = Array.from(document.querySelectorAll("[data-ticket-filter]"));
      const count = document.getElementById("ticket-filter-count");
      if (!table || !search || !buttons.length) return;
      let type = "all";
      function apply() {{
        const query = search.value.trim().toLowerCase();
        let visible = 0;
        const rows = Array.from(table.tBodies[0]?.rows || []);
        rows.forEach((row) => {{
          const rowType = row.dataset.ticketType || "";
          const text = row.textContent.toLowerCase();
          const typeMatch = type === "all" || rowType === type;
          const textMatch = !query || text.includes(query);
          const show = typeMatch && textMatch;
          row.style.display = show ? "" : "none";
          if (show) visible += 1;
        }});
        if (count) count.textContent = `${{visible}} of ${{rows.length}} shown`;
      }}
      buttons.forEach((button) => {{
        button.addEventListener("click", () => {{
          type = button.dataset.ticketFilter || "all";
          buttons.forEach((b) => b.classList.toggle("active", b === button));
          apply();
        }});
      }});
      search.addEventListener("input", apply);
      apply();
    }}
    function initDashboardTabs() {{
      const tabs = Array.from(document.querySelectorAll("[data-dashboard-tab]"));
      const panels = Array.from(document.querySelectorAll("[data-dashboard-panel]"));
      if (!tabs.length || !panels.length) return;
      function activate(name) {{
        tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.dashboardTab === name));
        panels.forEach((panel) => {{
          panel.hidden = panel.dataset.dashboardPanel !== name;
        }});
      }}
      tabs.forEach((tab) => {{
        tab.addEventListener("click", (event) => {{
          event.preventDefault();
          activate(tab.dataset.dashboardTab || "dashboard");
        }});
      }});
      activate(location.hash === "#dashboard-advisories" ? "advisories" : "dashboard");
    }}
    function toggleStaffEdit(id) {{
      const panel = document.getElementById(id);
      if (panel) panel.classList.toggle("open");
    }}
    function setHealthSelectClass(select) {{
      const key = (select.value || "Unknown").toLowerCase().replace(/[^a-z0-9]+/g, "") || "unknown";
      ["health-green", "health-yellow", "health-red", "health-unknown"].forEach((name) => select.classList.remove(name));
      select.classList.add(["green", "yellow", "red"].includes(key) ? `health-${{key}}` : "health-unknown");
    }}
    function initUnmappedJiraMapping() {{
      const form = document.getElementById("map-unmapped-form");
      if (!form) return;
      const section = form.closest("section");
      const status = document.createElement("span");
      status.className = "muted";
      status.style.marginLeft = "4px";
      form.appendChild(status);
      function removeMappedRows(keys) {{
        keys.forEach((key) => {{
          const row = section?.querySelector(`[data-unmapped-row="${{CSS.escape(key)}}"]`);
          if (row) row.remove();
        }});
        const tbody = section?.querySelector("tbody");
        if (tbody && !tbody.rows.length) {{
          const table = section.querySelector(".table-scroll");
          if (table) table.outerHTML = '<div class="empty">No unmapped Jira tickets waiting for customer mapping.</div>';
          form.remove();
        }}
      }}
      async function submitMapping(formData, button) {{
        if (button) {{
          button.disabled = true;
          button.dataset.originalText = button.textContent || "";
          button.textContent = "Mapping...";
        }}
        status.textContent = "Mapping...";
        try {{
          const response = await fetch(form.action, {{
            method: "POST",
            body: new URLSearchParams(formData),
            headers: {{
              "Accept": "application/json",
              "X-Requested-With": "fetch",
            }},
          }});
          const result = await response.json();
          if (!response.ok || result.ok === false) throw new Error(result.message || "Mapping failed.");
          removeMappedRows(result.mapped_keys || []);
          status.textContent = result.message || "Mapped.";
        }} catch (error) {{
          status.textContent = error.message || "Mapping failed.";
        }} finally {{
          if (button && document.body.contains(button)) {{
            button.disabled = false;
            button.textContent = button.dataset.originalText || "Approve";
          }}
        }}
      }}
      form.addEventListener("submit", (event) => {{
        event.preventDefault();
        event.stopImmediatePropagation();
        submitMapping(new FormData(form), form.querySelector("button[type='submit']"));
      }});
      section?.querySelectorAll("[data-map-approve]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const row = button.closest("[data-unmapped-row]");
          const select = row?.querySelector("[data-map-select]");
          if (!row || !select || !select.value) {{
            status.textContent = "Pick a customer before approving.";
            return;
          }}
          const formData = new FormData();
          formData.set("key", row.dataset.unmappedRow || button.dataset.mapApprove || "");
          formData.set("customer_id", select.value);
          submitMapping(formData, button);
        }});
      }});
    }}
    function initSubmitBusyState() {{
      document.querySelectorAll("form").forEach((form) => {{
        form.addEventListener("submit", () => {{
          const button = form.querySelector("button[type='submit']");
          if (!button || button.disabled) return;
          button.dataset.originalText = button.textContent || "";
          button.textContent = button.dataset.busyText || "Working...";
          button.classList.add("is-working");
          button.disabled = true;
          const panelId = form.dataset.runningPanel;
          if (panelId) {{
            const panel = document.getElementById(panelId);
            if (panel) {{
              panel.classList.add("visible");
              const started = panel.querySelector("[data-started-at]");
              if (started) started.textContent = new Date().toLocaleTimeString();
              panel.scrollIntoView({{ block: "nearest" }});
            }}
          }}
        }});
      }});
    }}
    window.addEventListener("DOMContentLoaded", () => {{
      document.querySelectorAll(".tag-editor").forEach(initTagEditor);
      document.querySelectorAll(".health-select").forEach(setHealthSelectClass);
      initSortableTables();
      initResizableTables();
      initCustomerSearch();
      initCustomerBulkActions();
      initUnmappedJiraMapping();
      initSubmitBusyState();
      initTicketFilters();
      initDashboardTabs();
    }});
  </script>
  <header>
    <strong><a href="/">TAM Console</a></strong>
    <div class="header-actions">
      <form class="global-search" method="get" action="/search">
        <input type="search" name="q" placeholder="Search customers or tickets">
        <button type="submit">Search</button>
      </form>
      <a href="/settings">Settings</a>
      <button id="theme-toggle" class="theme-button" type="button" onclick="toggleTheme()">Light mode</button>
    </div>
  </header>
  <main>{body}</main>
</body>
</html>""".encode()


def render_sidebar(active_slug: str = "") -> str:
    customers = rows(
        """
        select id, slug, name, status, is_pinned, is_hidden, sort_order
        from customers
        order by is_pinned desc,
                 case when sort_order = 0 then 1 else 0 end,
                 sort_order,
                 name
        """
    )
    visible_links = []
    hidden_links = []

    def customer_sidebar_row(customer: sqlite3.Row, hidden: bool = False) -> str:
        active = " active" if customer["slug"] == active_slug else ""
        pin_label = "Unpin" if customer["is_pinned"] else "Pin"
        pin_mark = '<span class="pin-mark">PIN</span>' if customer["is_pinned"] else ""
        action_buttons = (
            f"""<form method="post" action="/customers/{esc(customer['slug'])}/unhide"><button class="icon-button" type="submit" title="Show in main list">Show</button></form>"""
            if hidden
            else f"""<form method="post" action="/customers/{esc(customer['slug'])}/pin"><button class="icon-button" type="submit" title="{pin_label}">P</button></form>
                <form method="post" action="/customers/{esc(customer['slug'])}/move-up"><button class="icon-button" type="submit" title="Move up">^</button></form>
                <form method="post" action="/customers/{esc(customer['slug'])}/move-down"><button class="icon-button" type="submit" title="Move down">v</button></form>
                <form method="post" action="/customers/{esc(customer['slug'])}/hide"><button class="icon-button" type="submit" title="Move to hidden list">Hide</button></form>"""
        )
        checkbox = "" if hidden else (
            f"""<input class="customer-bulk-check" data-customer-bulk-check form="bulk-hide-form" """
            f"""type="checkbox" name="customer_id" value="{customer['id']}" aria-label="Select {esc(customer['name'])}">"""
        )
        hidden_class = " hidden-customer-row" if hidden else ""
        return f"""<div class="customer-row{hidden_class}">
              {checkbox}
              <a class="customer-link{active}" href="/customers/{esc(customer["slug"])}">
                {esc(customer["name"])}{pin_mark}<br><span class="muted">{esc(customer["status"])}</span>
              </a>
              <div class="customer-tools">
                {action_buttons}
              </div>
            </div>"""

    for customer in customers:
        if customer["is_hidden"]:
            hidden_links.append(customer_sidebar_row(customer, True))
        else:
            visible_links.append(customer_sidebar_row(customer, False))

    hidden_group = ""
    if hidden_links:
        hidden_group = f"""<details class="sidebar-group">
      <summary>Hidden <span>{len(hidden_links)}</span></summary>
      {''.join(hidden_links)}
    </details>"""
    return f"""<aside class="sidebar">
  <button id="sidebar-toggle" class="sidebar-toggle" type="button" onclick="toggleSidebar()" title="Toggle customers">></button>
  <div class="sidebar-content">
    <h3>Customers</h3>
    <input id="customer-search" class="customer-search" type="search" placeholder="Search customers">
    <form id="bulk-hide-form" class="customer-bulk-actions" method="post" action="/customers/bulk-hide">
      <input type="hidden" name="return_to" value="{esc('/customers/' + active_slug if active_slug else '/')}">
      <button id="bulk-hide-button" type="submit" disabled>Hide selected</button>
      <span id="bulk-hide-count" class="customer-bulk-count">None selected</span>
    </form>
    {''.join(visible_links) or '<p class="muted">No visible customers.</p>'}
    {hidden_group}
    <hr>
    <form method="post" action="/customers" style="border:0;padding:0;margin-top:14px">
      <label>New customer<input name="name" required placeholder="Customer name"></label>
      <button type="submit">Add</button>
    </form>
  </div>
</aside>"""


def render_home(message: str = "") -> bytes:
    count = row("select count(*) as n from customers")["n"]
    ticket_count = row("select count(distinct key) as n from tickets")["n"]
    active_ticket_count = row(
        """
        select count(distinct key) as n
        from tickets
        where lower(status) not in ('done', 'resolved', 'closed')
          and lower(status) not like '%resolution provided%'
        """
    )["n"]
    ticket_link_count = row("select count(*) as n from tickets")["n"]
    org_count = row("select count(distinct organization_id) as n from customer_jira_organizations where organization_id != ''")["n"]
    red_count = row("select count(*) as n from customers where lower(health) = 'red'")["n"]
    yellow_count = row("select count(*) as n from customers where lower(health) = 'yellow'")["n"]
    quality_gaps = [
        (
            "No environments",
            row(
                """
                select count(*) as n from customers c
                where not exists (select 1 from environments e where e.customer_id = c.id)
                """
            )["n"],
        ),
        (
            "Tickets without environment",
            row("select count(*) as n from tickets where environment_id is null")["n"],
        ),
        (
            "Staff without environment",
            row(
                """
                select count(*) as n from staff s
                where not exists (select 1 from environment_staff es where es.staff_id = s.id)
                """
            )["n"],
        ),
        (
            "Imported customers",
            row("select count(*) as n from customers where lower(status) = 'imported'")["n"],
        ),
        (
            "No next action",
            row("select count(*) as n from customers where coalesce(next_action, '') = ''")["n"],
        ),
    ]
    gap_items = "".join(
        f"""<div class="gap-item"><span>{help_label(label)}</span><strong>{count_value}</strong></div>"""
        for label, count_value in quality_gaps
    )
    risk_rows = "".join(
        f"""<tr>
          <td><a href="/customers/{esc(r['slug'])}">{esc(r['name'])}</a></td>
          <td>{health_badge(r['health'])}</td>
          <td>{esc(r['next_action']) or '<span class="muted">No next action</span>'}</td>
          <td>{esc(r['next_action_due'])}</td>
        </tr>"""
        for r in rows(
            """
            select slug, name, health, next_action, next_action_due
            from customers
            where lower(health) in ('red', 'yellow')
               or coalesce(next_action, '') != ''
            order by case lower(health) when 'red' then 0 when 'yellow' then 1 else 2 end,
                     next_action_due = '',
                     next_action_due,
                     name
            limit 12
            """
        )
    )
    recent_ticket_rows = "".join(
        f"""<tr>
          <td><a href="/customers/{esc(r['slug'])}/tickets">{esc(r['customer_name'])}</a></td>
          <td><a href="{esc(r['url'])}" target="_blank">{esc(r['key'])}</a></td>
          <td>{esc(r['status'])}</td>
          <td>{esc(r['updated'])}</td>
        </tr>"""
        for r in rows(
            """
            select c.slug, c.name as customer_name, t.key, t.status, t.updated, t.url
            from tickets t
            join customers c on c.id = t.customer_id
            order by t.updated desc, t.key
            limit 12
            """
        )
    )
    active_customer_rows = "".join(
        f"""<tr>
          <td><a href="/customers/{esc(r['slug'])}/tickets">{esc(r['name'])}</a></td>
          <td>{r['open_count']}</td>
          <td>{r['ticket_count']}</td>
        </tr>"""
        for r in rows(
            """
            select c.slug, c.name,
                   sum(case
                         when lower(t.status) in ('done', 'resolved', 'closed')
                           or lower(t.status) like '%resolution provided%'
                         then 0 else 1 end) as open_count,
                   count(t.id) as ticket_count
            from customers c
            join tickets t on t.customer_id = c.id
            group by c.id
            order by open_count desc, ticket_count desc, c.name
            limit 12
            """
        )
    )
    customers_for_mapping = rows(
        """
        select id, name
        from customers
        order by is_pinned desc,
                 case when sort_order = 0 then 1 else 0 end,
                 sort_order,
                 name
        """
    )
    candidates = customer_mapping_candidates()

    def mapping_options(selected_id: int | None = None) -> str:
        return "".join(
            f'<option value="{r["id"]}"{" selected" if selected_id == r["id"] else ""}>{esc(r["name"])}</option>'
            for r in customers_for_mapping
        )

    unmapped_rows = "".join(
        (
            lambda suggestion: f"""<tr data-unmapped-row="{esc(r['key'])}">
          <td><a href="{esc(r['url'])}" target="_blank">{esc(r['key'])}</a></td>
          <td>{esc(r['summary'])}</td>
          <td>{esc(r['status'])}</td>
          <td>{esc(r['reporter'])}</td>
          <td>{esc(r['updated'])}</td>
          <td>
            <div class="map-cell">
              <select class="map-customer-select" form="map-unmapped-form" name="map_{esc(r['key'])}" data-map-select>
                <option value="">Unmapped</option>{mapping_options(suggestion['id'] if suggestion else None)}
              </select>
              {f'<span class="map-suggestion">Suggested <strong>{esc(suggestion["name"])}</strong></span><button class="approve-map-button" type="button" data-map-approve="{esc(r["key"])}">Approve</button>' if suggestion else '<span class="muted">No suggestion</span>'}
            </div>
          </td>
        </tr>"""
        )(suggest_customer_for_ticket(r, candidates))
        for r in rows(
            """
            select *
            from unmapped_jira_tickets
            order by updated desc, key
            limit 20
            """
        )
    )
    body = f"""<div class="layout">
  {render_sidebar()}
  <div class="stack">
    <details id="jira-sync-running" class="status-panel running-panel" open>
      <summary><strong>Syncing Jira</strong><span class="muted">Working · started <span data-started-at>now</span></span></summary>
      <p>Importing assigned tickets and refreshing existing local ticket links. You can leave this open while it runs; the dashboard will return when the sync finishes.</p>
      <div class="progress-shell" aria-label="Jira sync in progress"></div>
    </details>
    {f'''<details id="dashboard-status" class="status-panel" open>
      <summary><strong>Last dashboard action</strong><span class="status-panel-actions"><button type="button" onclick="document.getElementById('dashboard-status').remove()">Dismiss</button></span></summary>
      <p>{esc(message)}</p>
    </details>''' if message else ''}
    <section class="section">
      <h2>Dashboard</h2>
      <nav class="tabs">
        <a class="active" href="/" data-dashboard-tab="dashboard">Dashboard</a>
        <a class="danger" href="#dashboard-advisories" data-dashboard-tab="advisories">Advisories</a>
      </nav>
      <div class="dashboard-panel" data-dashboard-panel="dashboard">
      <div class="dashboard-grid">
        {metric_card(count, "Customers")}
        {metric_card(ticket_count, "Tickets")}
        {metric_card(active_ticket_count, "Active tickets")}
        {metric_card(ticket_link_count, "Ticket links")}
        {metric_card(org_count, "Jira orgs")}
      </div>
      <div class="dashboard-grid" style="margin-top:12px">
        {metric_card(red_count, "Red customers")}
        {metric_card(yellow_count, "Yellow customers")}
        {metric_card(quality_gaps[0][1], "No environments")}
        {metric_card(quality_gaps[1][1], "Tickets missing env")}
        {metric_card(quality_gaps[2][1], "Staff missing env")}
      </div>
      <div class="actions">
        <form method="post" action="/jira/sync" data-running-panel="jira-sync-running">
          <button type="submit" data-busy-text="Syncing Jira...">Sync Jira</button>
        </form>
      </div>
      </div>
    </section>
    <div class="dashboard-panel" data-dashboard-panel="dashboard">
      <div class="panel-grid">
      <section class="section">
        <h3>Recent Tickets</h3>
        {f'<div class="table-scroll"><table><thead><tr><th>Customer</th><th>Key</th><th>Status</th><th>Updated</th></tr></thead><tbody>{recent_ticket_rows}</tbody></table></div>' if recent_ticket_rows else '<div class="empty">No tickets imported yet.</div>'}
      </section>
      <section class="section">
        <h3>Customers With Active Work</h3>
        {f'<table><thead><tr><th>Customer</th><th>Active</th><th>Total</th></tr></thead><tbody>{active_customer_rows}</tbody></table>' if active_customer_rows else '<div class="empty">No active ticket data yet.</div>'}
      </section>
      </div>
      <div class="panel-grid">
      <section class="section">
        <h3>Risk And Next Actions</h3>
        {f'<div class="table-scroll"><table><thead><tr><th>Customer</th><th>Health</th><th>Next action</th><th>Due</th></tr></thead><tbody>{risk_rows}</tbody></table></div>' if risk_rows else '<div class="empty">No risk or next-action data yet.</div>'}
      </section>
      <section class="section">
        <h3>Data Quality</h3>
        <div class="gap-list">{gap_items}</div>
      </section>
      </div>
      <section id="unmapped-jira" class="section">
      <h3>Unmapped Jira Tickets</h3>
      {f'<form id="map-unmapped-form" class="map-customer-actions" method="post" action="/jira/map-unmapped#unmapped-jira"><button class="map-customer-button" type="submit">Map selected</button><span class="muted">Review suggestions, change any dropdown, then map selected tickets.</span></form><div class="table-scroll"><table><thead><tr><th>Key</th><th>Summary</th><th>Status</th><th>Reporter</th><th>Updated</th><th>Map to customer</th></tr></thead><tbody>{unmapped_rows}</tbody></table></div>' if unmapped_rows else '<div class="empty">No unmapped Jira tickets waiting for customer mapping.</div>'}
      </section>
    </div>
    <section id="dashboard-advisories" class="section dashboard-panel" data-dashboard-panel="advisories" hidden>
      <h3>Advisories</h3>
      <p class="muted">Track release notes, hotfixes, regressions, and Slack/email/ticket signals that should appear on relevant customer profiles.</p>
      {render_advisory_table()}
      <details>
        <summary>Add advisory</summary>
        {render_advisory_form()}
      </details>
      <details>
        <summary>Import Claude Slack JSON</summary>
        <form method="post" action="/slack/import">
          <label>Claude Slack JSON<textarea name="payload" placeholder="Paste structured JSON returned by Claude's read-only Slack search"></textarea></label>
          <button type="submit">Import Slack context</button>
        </form>
      </details>
    </section>
  </div>
</div>"""
    return page("Dashboard", body)


def render_search(query: str = "") -> bytes:
    terms = search_terms(query)
    customer_results = []
    ticket_results = []
    if terms:
        customer_where = " and ".join(
            ["lower(c.name || ' ' || coalesce(c.aliases, '') || ' ' || coalesce(c.status, '') || ' ' || coalesce(c.products, '') || ' ' || coalesce(c.overview, '')) like ?"]
            * len(terms)
        )
        customer_params = tuple(f"%{term}%" for term in terms)
        customer_results = rows(
            f"""
            select c.slug, c.name, c.status, c.health, c.products, c.next_action
            from customers c
            where {customer_where}
            order by c.is_pinned desc, c.name
            limit 50
            """,
            customer_params,
        )

        ticket_where = " and ".join(
            ["lower(c.name || ' ' || coalesce(c.aliases, '') || ' ' || t.key || ' ' || coalesce(t.summary, '') || ' ' || coalesce(t.notes, '') || ' ' || coalesce(t.status, '') || ' ' || coalesce(t.assignee, '')) like ?"]
            * len(terms)
        )
        ticket_params = tuple(f"%{term}%" for term in terms)
        ticket_results = rows(
            f"""
            select c.slug, c.name as customer_name, t.key, t.summary, t.status, t.updated, t.url, t.notes
            from tickets t
            join customers c on c.id = t.customer_id
            where {ticket_where}
            order by t.updated desc, c.name, t.key
            limit 100
            """,
            ticket_params,
        )

    customer_rows = "".join(
        f"""<tr>
          <td><a href="/customers/{esc(r['slug'])}">{esc(r['name'])}</a></td>
          <td>{health_badge(r['health'])}</td>
          <td>{esc(r['status'])}</td>
          <td>{esc(r['products'])}</td>
          <td>{esc(r['next_action'])}</td>
        </tr>"""
        for r in customer_results
    )
    ticket_rows = "".join(
        f"""<tr>
          <td><a href="/customers/{esc(r['slug'])}/tickets">{esc(r['customer_name'])}</a></td>
          <td><a href="{esc(r['url'])}" target="_blank">{esc(r['key'])}</a></td>
          <td>{esc(r['summary'])}</td>
          <td>{esc(r['status'])}</td>
          <td>{esc(r['updated'])}</td>
          <td>{esc(r['notes'])}</td>
        </tr>"""
        for r in ticket_results
    )
    body = f"""<div class="layout">
  {render_sidebar()}
  <div class="stack">
    <section class="section">
      <h2>Search</h2>
      <form class="filterbar" method="get" action="/search">
        <input type="search" name="q" value="{esc(query)}" placeholder="Search customers, tickets, SCTE, FR, ESD-8143">
        <button type="submit">Search</button>
      </form>
      <p class="muted">{len(customer_results)} customer result(s), {len(ticket_results)} ticket result(s)</p>
    </section>
    <section class="section">
      <h3>Customers</h3>
      {f'<div class="table-scroll"><table><thead><tr><th>Customer</th><th>Health</th><th>Status</th><th>Products</th><th>Next action</th></tr></thead><tbody>{customer_rows}</tbody></table></div>' if customer_rows else '<div class="empty">No customer matches.</div>'}
    </section>
    <section class="section">
      <h3>Tickets</h3>
      {f'<div class="table-scroll"><table><thead><tr><th>Customer</th><th>Key</th><th>Summary</th><th>Status</th><th>Updated</th><th>Short summary</th></tr></thead><tbody>{ticket_rows}</tbody></table></div>' if ticket_rows else '<div class="empty">No ticket matches.</div>'}
    </section>
  </div>
</div>"""
    return page("Search", body)


def render_settings(message: str = "") -> bytes:
    include_assignee = app_setting("jira_sync_include_assignee", "1") == "1"
    include_reporter = app_setting("jira_sync_include_reporter", "1") == "1"
    projects = app_setting("jira_sync_projects", "ESD, CS, FR, MB")
    body = f"""<div class="layout">
  {render_sidebar()}
  <div class="stack">
    {f'<details class="status-panel" open><summary><strong>Settings saved</strong></summary><p>{esc(message)}</p></details>' if message else ''}
    <section class="section">
      <h2>Settings</h2>
      <form method="post" action="/settings">
        <h3>Jira Sync</h3>
        <label>Projects
          <input name="jira_sync_projects" value="{esc(projects)}" placeholder="ESD, CS, FR, MB">
        </label>
        <label class="check-row">
          <input type="checkbox" name="jira_sync_include_assignee" value="1"{' checked' if include_assignee else ''}>
          <span>Include tickets assigned to me</span>
        </label>
        <label class="check-row">
          <input type="checkbox" name="jira_sync_include_reporter" value="1"{' checked' if include_reporter else ''}>
          <span>Include tickets reported by me</span>
        </label>
        <p class="muted">Reporter-based sync is useful for FR/MB tickets that do not have a Jira Organization. Those tickets can still be manually mapped by adding them under a customer.</p>
        <button type="submit">Save settings</button>
      </form>
    </section>
  </div>
</div>"""
    return page("Settings", body)


def render_customer(slug: str, section: str = "overview", message: str = "") -> bytes:
    customer = row("select * from customers where slug = ?", (slug,))
    if customer is None:
        return page("Not found", "<section class='section'><h2>Customer not found</h2></section>")
    cid = customer["id"]
    environments = rows("select * from environments where customer_id = ? order by name", (cid,))
    tickets = rows(
        """
        select t.*, e.name as environment_name
        from tickets t
        left join environments e on e.id = t.environment_id
        where t.customer_id = ?
        order by t.updated desc, t.key
        """,
        (cid,),
    )
    tickets_by_key = {ticket["key"]: ticket for ticket in tickets}
    try:
        jira_browse_base = jira_site_base(load_atlassian_config().BASE_URL) + "/browse/"
    except Exception:
        jira_browse_base = "https://tag.atlassian.net/browse/"
    meetings = rows(
        """
        select m.*, e.name as environment_name
        from meetings m
        left join environments e on e.id = m.environment_id
        where m.customer_id = ?
        order by m.meeting_date desc, m.id desc
        """,
        (cid,),
    )
    notes = rows(
        """
        select n.*, e.name as environment_name
        from notes n
        left join environments e on e.id = n.environment_id
        where n.customer_id = ?
        order by n.created_at desc
        """,
        (cid,),
    )
    artifacts = rows(
        """
        select a.*, e.name as environment_name
        from artifacts a
        left join environments e on e.id = a.environment_id
        where a.customer_id = ?
        order by a.created_at desc
        """,
        (cid,),
    )
    hardware = rows(
        """
        select h.*, e.name as environment_name
        from hardware h
        left join environments e on e.id = h.environment_id
        where h.customer_id = ?
        order by h.label, h.id
        """,
        (cid,),
    )
    software = rows(
        """
        select s.*, e.name as environment_name
        from software_deployments s
        left join environments e on e.id = s.environment_id
        where s.customer_id = ?
        order by e.name, s.product, s.version
        """,
        (cid,),
    )
    staff = rows(
        """
        select s.*,
               group_concat(e.name || case when es.responsibility != '' then ' (' || es.responsibility || ')' else '' end, ', ') as environments
        from staff s
        left join environment_staff es on es.staff_id = s.id
        left join environments e on e.id = es.environment_id
        where s.customer_id = ?
        group by s.id
        order by s.name
        """,
        (cid,),
    )
    staff_environment_rows = rows(
        """
        select es.staff_id, es.environment_id, es.responsibility
        from environment_staff es
        join environments e on e.id = es.environment_id
        where e.customer_id = ?
        """,
        (cid,),
    )
    staff_env_map = {}
    for mapping in staff_environment_rows:
        staff_env_map.setdefault(mapping["staff_id"], {})[mapping["environment_id"]] = mapping["responsibility"]
    advisory_matches = relevant_advisories_for_customer(cid)
    advisory_items = render_advisory_items(advisory_matches)

    environment_options = '<option value="">Customer-wide</option>' + "".join(
        f'<option value="{env["id"]}">{esc(env["name"])}</option>'
        for env in environments
    )
    env_type_options = ("On-prem", "AWS", "GCP", "Azure", "Cloud", "Lab", "Hybrid", "Other")
    source_type_datalist = "".join(
        f'<option value="{esc(option)}"></option>' for option in SOURCE_TYPE_SUGGESTIONS
    )
    customer_status_options = ("Active", "Imported", "Watching", "Inactive", "Archived")
    def customer_status_select(selected: str = "") -> str:
        options = []
        for option in customer_status_options:
            chosen = " selected" if option.lower() == (selected or "").lower() else ""
            options.append(f'<option value="{esc(option)}"{chosen}>{esc(option)}</option>')
        if selected and selected not in customer_status_options:
            options.append(f'<option value="{esc(selected)}" selected>{esc(selected)}</option>')
        return f'<select name="status">{"".join(options)}</select>'
    def health_select(selected: str = "") -> str:
        options = []
        for option in ("Unknown", "Green", "Yellow", "Red"):
            chosen = " selected" if option.lower() == (selected or "").lower() else ""
            options.append(f'<option value="{esc(option)}"{chosen}>{esc(option)}</option>')
        key = normalize_match(selected or "Unknown")
        if key not in {"green", "yellow", "red", "unknown"}:
            key = "unknown"
        return (
            f'<select name="health" class="health-select health-{key}" '
            f'onchange="setHealthSelectClass(this)">{"".join(options)}</select>'
        )
    def env_type_select(name: str, selected: str = "") -> str:
        options = ['<option value="">Unspecified</option>']
        for option in env_type_options:
            chosen = " selected" if option.lower() == (selected or "").lower() else ""
            options.append(f'<option value="{esc(option)}"{chosen}>{esc(option)}</option>')
        if selected and selected not in env_type_options:
            options.append(f'<option value="{esc(selected)}" selected>{esc(selected)}</option>')
        return f'<select name="{name}">{"".join(options)}</select>'
    environment_cards = "".join(
        f"""<article class="item">
          <strong>{esc(env['name'])}</strong> <span class="tag">{esc(env['env_type']) or 'Environment'}</span>
          <dl class="facts">
            <dt>Location</dt><dd>{esc(env['location']) or '<span class="muted">Not set</span>'}</dd>
            <dt>Status</dt><dd>{esc(env['status'])}</dd>
            <dt>Source Type</dt><dd>{render_tags(env['source_types'])}</dd>
          </dl>
          <p>{esc(env['architecture'])}</p>
          <p class="muted">{esc(env['notes'])}</p>
          <details>
            <summary>Edit</summary>
            <form method="post" action="/customers/{esc(customer['slug'])}/environment-update">
              <input type="hidden" name="environment_id" value="{env['id']}">
              <div class="grid-2">
                <label>Name<input name="name" value="{esc(env['name'])}" required></label>
                <label>Type{env_type_select('env_type', env['env_type'])}</label>
                <label>Location<input name="location" value="{esc(env['location'])}"></label>
                <label>Status<input name="status" value="{esc(env['status'])}"></label>
              </div>
              <label>Source Type{render_tag_editor('source_types', env['source_types'], 'Type and press Enter')}</label>
              <label>Products<input name="products" value="{esc(env['products'])}" placeholder="MCM, MCS"></label>
              <label>Architecture<textarea name="architecture">{esc(env['architecture'])}</textarea></label>
              <label>Notes<textarea name="notes">{esc(env['notes'])}</textarea></label>
              <button type="submit">Save environment</button>
            </form>
          </details>
        </article>"""
        for env in environments
    )

    ticket_rows = "".join(
        f"""<tr data-ticket-type="{esc(t['key'].split('-', 1)[0].lower())}">
          <td><a href="{esc(t['url'])}" target="_blank">{esc(t['key'])}</a></td>
          <td>{esc(t['summary'])}</td>
          <td>{esc(t['environment_name']) or '<span class="muted">Customer-wide</span>'}</td>
          <td>{esc(t['status'])}</td>
          <td>{esc(t['priority'])}</td>
          <td>{esc(t['assignee'])}</td>
          <td>{esc(t['updated'])}</td>
          <td>{esc(t['notes'])}</td>
          <td>{esc(t['synced_at']) or '<span class="muted">Manual</span>'}</td>
        </tr>"""
        for t in tickets
    )
    meeting_items = "".join(
        f"""<article class="item">
          <strong>{esc(m['meeting_date'])} · {esc(m['title'])}</strong>
          <p class="muted">{esc(m['environment_name']) or 'Customer-wide'} · {esc(m['attendees'])}</p>
          <p>{esc(m['summary'])}</p>
          <p><strong>Actions:</strong> {esc(m['actions'])}</p>
          {f'<a href="{esc(m["url"])}" target="_blank">source</a>' if m['url'] else ''}
        </article>"""
        for m in meetings
    )
    note_items = "".join(
        f"""<article class="item">
          <span class="tag">{esc(n['note_type'])}</span>
          <strong> {esc(n['title'])}</strong>
          <p>{esc(n['body'])}</p>
          <p class="muted">{esc(n['environment_name']) or 'Customer-wide'} · {esc(n['created_at'])} {f'· <a href="{esc(n["source_url"])}" target="_blank">source</a>' if n['source_url'] else ''}</p>
        </article>"""
        for n in notes
    )
    artifact_items = "".join(render_artifact_item(a) for a in artifacts)
    architecture_artifacts = [
        a
        for a in artifacts
        if is_image_path(a["path_or_url"])
        and (
            "diagram" in (a["artifact_type"] or "").lower()
            or "architecture" in (a["artifact_type"] or "").lower()
            or "diagram" in (a["label"] or "").lower()
        )
    ]
    architecture_artifact_items = "".join(
        render_artifact_item(a) for a in architecture_artifacts
    )
    hardware_rows = "".join(
        f"""<tr>
          <td>{esc(h['label'])}</td>
          <td>{esc(h['environment_name']) or '<span class="muted">Customer-wide</span>'}</td>
          <td>{esc(h['role'])}</td>
          <td>{esc(h['vendor'])}</td>
          <td>{esc(h['model'])}</td>
          <td>{esc(h['cpu'])}</td>
          <td>{esc(h['memory'])}</td>
          <td>{esc(h['quantity'])}</td>
          <td>{esc(h['status'])}</td>
          <td>{esc(h['confidence'])}</td>
          <td>{esc(h['notes'])}</td>
        </tr>"""
        for h in hardware
    )
    software_rows = "".join(
        f"""<tr>
          <td>{esc(s['product'])}</td>
          <td>{esc(s['environment_name']) or '<span class="muted">Customer-wide</span>'}</td>
          <td>{esc(s['version'])}</td>
          <td>{esc(s['version_notes'])}</td>
          <td>{esc(s['deployment_mode'])}</td>
          <td>{esc(s['redundancy'])}</td>
          <td>{esc(s['node_count'])}</td>
          <td>{esc(s['status'])}</td>
          <td>{esc(s['confidence'])}</td>
          <td>{esc(s['notes'])}</td>
        </tr>"""
        for s in software
    )
    staff_rows_parts = []
    for s in staff:
        mapped = staff_env_map.get(s["id"], {})
        source_ticket_keys = extract_ticket_keys(s["notes"])
        def source_ticket_item(key: str) -> str:
            ticket = tickets_by_key.get(key)
            url = ticket["url"] if ticket and ticket["url"] else jira_browse_base + key
            summary = ticket["summary"] if ticket else ""
            return f"""<li>
              <a href="{esc(url)}" target="_blank">{esc(key)}</a>
              <span class="muted">{esc(summary)}</span>
            </li>"""

        source_ticket_links = "".join(
            source_ticket_item(key)
            for key in source_ticket_keys
        )
        source_tickets = (
            f"""<div class="source-tickets">
              <strong>Source tickets</strong>
              <ul>{source_ticket_links}</ul>
            </div>"""
            if source_ticket_links
            else '<p class="muted">No source tickets captured for this contact yet.</p>'
        )
        env_checks = "".join(
            f"""<label class="check-row">
              <input type="checkbox" name="environment_id" value="{env['id']}"{' checked' if env['id'] in mapped else ''}>
              <span>{esc(env['name'])}</span>
              <input name="responsibility_{env['id']}" value="{esc(mapped.get(env['id'], ''))}" placeholder="Responsibility">
            </label>"""
            for env in environments
        )
        edit_id = f"staff-edit-{s['id']}"
        edit_form = f"""<div id="{edit_id}" class="staff-edit-panel">
            <form method="post" action="/customers/{esc(customer['slug'])}/staff-update">
              <input type="hidden" name="staff_id" value="{s['id']}">
              <div class="grid-2">
                <label>Name<input name="name" value="{esc(s['name'])}" required></label>
                <label>Role<input name="role" value="{esc(s['role'])}"></label>
                <label>Team<input name="team" value="{esc(s['team'])}"></label>
                <label>Email<input name="email" value="{esc(s['email'])}"></label>
                <label>Slack handle<input name="slack_handle" value="{esc(s['slack_handle'])}"></label>
              </div>
              <label>Notes<textarea name="notes">{esc(s['notes'])}</textarea></label>
              {source_tickets}
              <div class="env-map">
                {env_checks or '<span class="muted">Add environments first.</span>'}
              </div>
              <button type="submit">Save staff</button>
            </form>
          </div>"""
        staff_rows_parts.append(
            f"""<tr>
              <td>{esc(s['name'])}</td>
              <td>{esc(s['role'])}</td>
              <td>{esc(s['team'])}</td>
              <td>{esc(s['email'])}</td>
              <td>{esc(s['slack_handle'])}</td>
              <td>{esc(s['environments']) or '<span class="muted">Customer-wide / unassigned</span>'}</td>
              <td><button class="icon-button" type="button" onclick="toggleStaffEdit('{edit_id}')">Edit</button></td>
            </tr>
            <tr class="staff-edit-row">
              <td colspan="7">{edit_form}</td>
            </tr>"""
        )
    staff_rows = "".join(staff_rows_parts)

    section_titles = {
        "overview": "Overview",
        "environments": "Environments",
        "architecture": "Architecture",
        "tickets": "Tickets",
        "staff": "Staff",
        "hardware": "Hardware",
        "software": "Software",
        "meetings": "Meetings",
        "notes": "Notes",
        "artifacts": "Artifacts",
    }
    if section not in section_titles:
        section = "overview"
    base = f"/customers/{customer['slug']}"
    tabs = "".join(
        f'<a class="{ "active" if key == section else "" }" href="{base if key == "overview" else base + "/" + key}">{label}</a>'
        for key, label in section_titles.items()
    )

    overview_section = f"""<section class="section">
      <h3>Overview</h3>
      <p>{esc(customer['overview'])}</p>
      {f'<div class="status-panel"><strong>{len(advisory_matches)} relevant advisory(s)</strong><div class="stack">{advisory_items}</div></div>' if advisory_matches else ''}
      <dl class="facts">
        <dt>Health</dt><dd>{editable_health_badge(customer['slug'], customer['health'])}</dd>
        <dt>Status</dt><dd>{esc(customer['status'])}</dd>
        <dt>Next action</dt><dd>{esc(customer['next_action']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Action due</dt><dd>{esc(customer['next_action_due']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Last touch</dt><dd>{esc(customer['last_touch']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Risk reason</dt><dd>{esc(customer['risk_reason']) or '<span class="muted">None</span>'}</dd>
        <dt>Aliases</dt><dd>{esc(customer['aliases'])}</dd>
        <dt>Owner</dt><dd>{esc(customer['owner']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Region</dt><dd>{esc(customer['region']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Products</dt><dd>{esc(customer['products'])}</dd>
        <dt>Updated</dt><dd>{esc(customer['updated_at'])}</dd>
      </dl>
      <details>
        <summary>Edit profile</summary>
        <form method="post" action="/customers/{esc(customer['slug'])}/profile">
          <div class="grid-2">
            <label>Owner<input name="owner" value="{esc(customer['owner'])}"></label>
            <label>Region<input name="region" value="{esc(customer['region'])}"></label>
            <label>Status{customer_status_select(customer['status'])}</label>
            <label>Health{health_select(customer['health'])}</label>
            <label>Products<input name="products" value="{esc(customer['products'])}"></label>
            <label>Next action due<input name="next_action_due" value="{esc(customer['next_action_due'])}" placeholder="YYYY-MM-DD"></label>
            <label>Last touch<input name="last_touch" value="{esc(customer['last_touch'])}" placeholder="YYYY-MM-DD"></label>
          </div>
          <label>Aliases<input name="aliases" value="{esc(customer['aliases'])}"></label>
          <label>Next action<textarea name="next_action">{esc(customer['next_action'])}</textarea></label>
          <label>Risk reason<textarea name="risk_reason">{esc(customer['risk_reason'])}</textarea></label>
          <label>Overview<textarea name="overview">{esc(customer['overview'])}</textarea></label>
          <label>Architecture<textarea name="architecture">{esc(customer['architecture'])}</textarea></label>
          <button type="submit">Save profile</button>
        </form>
      </details>
    </section>"""

    sections = {
        "overview": overview_section,
        "environments": f"""<section class="section">
          <h3>Environments</h3>
          {environment_cards or '<div class="empty">No environments yet.</div>'}
          <form method="post" action="/customers/{esc(customer['slug'])}/environments">
            <datalist id="source-type-options">{source_type_datalist}</datalist>
            <div class="grid-2">
              <label>Name<input name="name" required placeholder="Dallas"></label>
              <label>Type{env_type_select('env_type')}</label>
              <label>Location<input name="location" placeholder="City, region, cloud region"></label>
              <label>Status<input name="status" value="Active"></label>
            </div>
            <label>Source Type{render_tag_editor('source_types', '', 'Type and press Enter')}</label>
            <label>Products<input name="products" placeholder="MCM, MCS"></label>
            <label>Architecture<textarea name="architecture"></textarea></label>
            <label>Notes<textarea name="notes"></textarea></label>
            <button type="submit">Add environment</button>
          </form>
        </section>""",
        "architecture": f"""<section class="section">
          <h3>Architecture</h3>
          <p>{esc(customer['architecture'])}</p>
          {architecture_artifact_items}
          <form method="post" action="/customers/{esc(customer['slug'])}/profile">
            <div class="grid-2">
              <label>Owner<input name="owner" value="{esc(customer['owner'])}"></label>
              <label>Region<input name="region" value="{esc(customer['region'])}"></label>
              <label>Status{customer_status_select(customer['status'])}</label>
              <label>Health{health_select(customer['health'])}</label>
              <label>Products<input name="products" value="{esc(customer['products'])}"></label>
              <label>Next action due<input name="next_action_due" value="{esc(customer['next_action_due'])}" placeholder="YYYY-MM-DD"></label>
              <label>Last touch<input name="last_touch" value="{esc(customer['last_touch'])}" placeholder="YYYY-MM-DD"></label>
            </div>
            <label>Aliases<input name="aliases" value="{esc(customer['aliases'])}"></label>
            <label>Next action<textarea name="next_action">{esc(customer['next_action'])}</textarea></label>
            <label>Risk reason<textarea name="risk_reason">{esc(customer['risk_reason'])}</textarea></label>
            <label>Overview<textarea name="overview">{esc(customer['overview'])}</textarea></label>
            <label>Architecture<textarea name="architecture">{esc(customer['architecture'])}</textarea></label>
            <button type="submit">Save profile</button>
          </form>
        </section>""",
        "tickets": f"""<section class="section">
          <h3>Tickets</h3>
          <details>
            <summary>Add ticket</summary>
            <form method="post" action="/customers/{esc(customer['slug'])}/tickets">
              <div class="grid-2">
                <label>Ticket key<input name="key" required placeholder="ESD-9106, CS-1234, FR-898, or MB-9904" pattern="^(ESD|CS|FR|MB)-[0-9]+$"></label>
                <label>URL<input name="url" placeholder="https://tag.atlassian.net/browse/..."></label>
                <label>Environment<select name="environment_id">{environment_options}</select></label>
                <label>Status<input name="status"></label>
                <label>Priority<input name="priority"></label>
                <label>Assignee<input name="assignee"></label>
                <label>Updated<input name="updated" placeholder="YYYY-MM-DD"></label>
              </div>
              <label>Summary<input name="summary"></label>
              <label>Notes<textarea name="notes"></textarea></label>
              <button type="submit">Save ticket</button>
            </form>
          </details>
          <div class="actions">
            <form method="post" action="/customers/{esc(customer['slug'])}/discover-jira">
              <button type="submit">Discover Jira tickets</button>
            </form>
            <form method="post" action="/customers/{esc(customer['slug'])}/sync-jira">
              <button type="submit">Sync existing Jira tickets</button>
            </form>
          </div>
          {f'<div class="filterbar"><input id="ticket-search" type="search" placeholder="Search tickets"><div class="segmented"><button class="active" type="button" data-ticket-filter="all">All</button><button type="button" data-ticket-filter="esd">ESD</button><button type="button" data-ticket-filter="cs">CS</button><button type="button" data-ticket-filter="fr">FR</button><button type="button" data-ticket-filter="mb">MB</button></div><span id="ticket-filter-count" class="filter-count"></span></div><div class="table-scroll"><table data-ticket-table><thead><tr><th>Key</th><th>Summary</th><th>Environment</th><th>Status</th><th>Priority</th><th>Assignee</th><th>Updated</th><th>Short summary</th><th>Synced</th></tr></thead><tbody>{ticket_rows}</tbody></table></div>' if tickets else '<div class="empty">No tickets linked yet.</div>'}
        </section>""",
        "staff": f"""<section class="section">
          <h3>Staff</h3>
          {f'<div class="table-scroll"><table class="staff-table"><thead><tr><th>Name</th><th>Role</th><th>Team</th><th>Email</th><th>Slack</th><th>Environment mapping</th><th>Edit</th></tr></thead><tbody>{staff_rows}</tbody></table></div>' if staff else '<div class="empty">No customer staff mapped yet.</div>'}
          <form method="post" action="/customers/{esc(customer['slug'])}/staff">
            <div class="grid-2">
              <label>Name<input name="name" required placeholder="Jane Smith"></label>
              <label>Role<input name="role" placeholder="Account owner, TAM, support, engineering"></label>
              <label>Team<input name="team" placeholder="CS, R&D, customer, partner"></label>
              <label>Email<input name="email" placeholder="name@example.com"></label>
              <label>Slack handle<input name="slack_handle" placeholder="@name"></label>
              <label>Environment<select name="environment_id">{environment_options}</select></label>
            </div>
            <label>Environment responsibility<input name="responsibility" placeholder="Primary contact, Dallas lead, cloud escalation"></label>
            <label>Notes<textarea name="notes"></textarea></label>
            <button type="submit">Add staff</button>
          </form>
        </section>""",
        "hardware": f"""<section class="section">
          <h3>Hardware</h3>
          {f'<div class="table-scroll"><table><thead><tr><th>Label</th><th>Environment</th><th>Role</th><th>Vendor</th><th>Model</th><th>CPU</th><th>Memory</th><th>Qty</th><th>Status</th><th>Confidence</th><th>Notes</th></tr></thead><tbody>{hardware_rows}</tbody></table></div>' if hardware else '<div class="empty">No hardware inventory yet.</div>'}
          <form method="post" action="/customers/{esc(customer['slug'])}/hardware">
            <div class="grid-2">
              <label>Label<input name="label" required placeholder="MCM server"></label>
              <label>Environment<select name="environment_id">{environment_options}</select></label>
              <label>Role<input name="role" placeholder="MCM, MCS, storage, app node"></label>
              <label>Vendor<input name="vendor" placeholder="HP, Dell, AWS"></label>
              <label>Model<input name="model" placeholder="DL360 Gen11 High"></label>
              <label>Quantity<input name="quantity" placeholder="all, 2, 4"></label>
              <label>Status<input name="status" value="Active"></label>
              <label>Confidence<select name="confidence"><option>Needs confirmation</option><option>Likely</option><option>Confirmed</option><option>Conflicting</option><option>Stale</option></select></label>
            </div>
            <label>CPU<input name="cpu" placeholder="2x Intel Xeon Gold 6548N @ 2.8 GHz"></label>
            <label>Memory<input name="memory" placeholder="512 GB DDR5"></label>
            <label>Serials<textarea name="serials"></textarea></label>
            <label>Notes<textarea name="notes"></textarea></label>
            <label>Source<input name="source" placeholder="BOM, email, ticket, meeting"></label>
            <button type="submit">Add hardware</button>
          </form>
        </section>""",
        "software": f"""<section class="section">
          <h3>Software</h3>
          {f'<div class="table-scroll"><table><thead><tr><th>Product</th><th>Environment</th><th>Version</th><th>Version notes</th><th>Deployment mode</th><th>Redundancy</th><th>Nodes</th><th>Status</th><th>Confidence</th><th>Notes</th></tr></thead><tbody>{software_rows}</tbody></table></div>' if software else '<div class="empty">No software deployments yet.</div>'}
          <details>
            <summary>Add software</summary>
            <form method="post" action="/customers/{esc(customer['slug'])}/software">
              <div class="grid-2">
                <label>Product<input name="product" required placeholder="MCM, MCS, MCR"></label>
                <label>Environment<select name="environment_id">{environment_options}</select></label>
                <label>Version<input name="version" placeholder="6.9.1, 6.9.2, 1.8.2"></label>
                <label>Deployment mode<input name="deployment_mode" placeholder="Standalone, Multi-host, Clustered"></label>
                <label>Redundancy<input name="redundancy" placeholder="1+1, N+1, Active-active, None"></label>
                <label>Node count<input name="node_count" placeholder="2, 4, mixed"></label>
                <label>Status<input name="status" value="Active"></label>
                <label>Confidence<select name="confidence"><option>Needs confirmation</option><option>Likely</option><option>Confirmed</option><option>Conflicting</option><option>Stale</option></select></label>
              </div>
              <label>Version notes<textarea name="version_notes" placeholder="Mixed MCM versions: two nodes on 6.9.1, one on 6.9.4"></textarea></label>
              <label>Notes<textarea name="notes"></textarea></label>
              <label>Source<input name="source" placeholder="ticket, meeting, BOM, email, manual"></label>
              <button type="submit">Save software</button>
            </form>
          </details>
        </section>""",
        "meetings": f"""<section class="section">
          <h3>Meetings</h3>
          {meeting_items or '<div class="empty">No meeting notes yet.</div>'}
          <form method="post" action="/customers/{esc(customer['slug'])}/meetings">
            <div class="grid-2">
              <label>Date<input name="meeting_date" required placeholder="YYYY-MM-DD"></label>
              <label>Title<input name="title" required placeholder="Weekly sync"></label>
              <label>Environment<select name="environment_id">{environment_options}</select></label>
            </div>
            <label>Attendees<input name="attendees"></label>
            <label>Summary<textarea name="summary"></textarea></label>
            <label>Actions<textarea name="actions"></textarea></label>
            <label>Source URL<input name="url"></label>
            <button type="submit">Add meeting</button>
          </form>
        </section>""",
        "notes": f"""<section class="section">
          <h3>Notes</h3>
          {note_items or '<div class="empty">No notes yet.</div>'}
          <form method="post" action="/customers/{esc(customer['slug'])}/notes">
            <div class="grid-2">
              <label>Type<select name="note_type"><option>General</option><option>Architecture</option><option>Risk</option><option>Next Action</option><option>Finding</option></select></label>
              <label>Title<input name="title" required></label>
              <label>Environment<select name="environment_id">{environment_options}</select></label>
            </div>
            <label>Body<textarea name="body"></textarea></label>
            <label>Source URL<input name="source_url"></label>
            <button type="submit">Add note</button>
          </form>
        </section>""",
        "artifacts": f"""<section class="section">
          <h3>Artifacts</h3>
          {artifact_items or '<div class="empty">No artifacts linked yet.</div>'}
          <form method="post" action="/customers/{esc(customer['slug'])}/artifacts">
            <div class="grid-2">
              <label>Label<input name="label" required></label>
              <label>Type<input name="artifact_type" placeholder="pcap, diagram, finding, log"></label>
              <label>Environment<select name="environment_id">{environment_options}</select></label>
            </div>
            <label>Path or URL<input name="path_or_url" required></label>
            <label>Notes<textarea name="notes"></textarea></label>
            <button type="submit">Add artifact</button>
          </form>
        </section>""",
    }

    body = f"""<div class="layout">
  {render_sidebar(customer['slug'])}
  <div class="stack">
    {f'<section class="section"><strong>{esc(message)}</strong></section>' if message else ''}
    <section class="section">
      <h2>{esc(customer['name'])}</h2>
      <dl class="facts">
        <dt>Health</dt><dd>{editable_health_badge(customer['slug'], customer['health'])}</dd>
        <dt>Status</dt><dd>{esc(customer['status'])}</dd>
        <dt>Next action</dt><dd>{esc(customer['next_action']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Action due</dt><dd>{esc(customer['next_action_due']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Owner</dt><dd>{esc(customer['owner']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Products</dt><dd>{esc(customer['products']) or '<span class="muted">Not set</span>'}</dd>
      </dl>
    </section>
    <nav class="tabs">{tabs}</nav>
    {sections[section]}
  </div>
</div>"""
    return page(customer["name"], body)


def form_data(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length).decode()
    parsed = parse_qs(raw, keep_blank_values=True)
    handler.form_values = parsed
    return {key: values[0].strip() for key, values in parsed.items()}


class Handler(BaseHTTPRequestHandler):
    def send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/files":
            params = parse_qs(parsed.query)
            requested = params.get("path", [""])[0]
            local_path = allowed_local_file(requested)
            if local_path is None:
                self.send_html(page("Not found", "<section class='section'><h2>File not available</h2></section>"), 404)
                return
            content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            data = local_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/":
            self.send_html(render_home())
            return
        if path == "/search":
            params = parse_qs(parsed.query)
            self.send_html(render_search(params.get("q", [""])[0]))
            return
        if path == "/settings":
            self.send_html(render_settings())
            return
        if path == "/advisories":
            self.redirect("/#dashboard-advisories")
            return
        if path.startswith("/customers/"):
            parts = path.split("/")
            slug = parts[2]
            section = parts[3] if len(parts) > 3 else "overview"
            self.send_html(render_customer(slug, section))
            return
        self.send_html(page("Not found", "<section class='section'><h2>Not found</h2></section>"), 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        data = form_data(self)
        ts = now_utc()
        if path in ("/jira/import-assigned", "/jira/sync"):
            try:
                sys.stderr.write(f"{now_utc()} starting {path}\n")
                message = sync_jira() if path == "/jira/sync" else import_assigned_jira_tickets()
                sys.stderr.write(f"{now_utc()} finished {path}: {message}\n")
            except Exception as exc:
                message = f"Jira sync failed: {exc}"
                sys.stderr.write(f"{now_utc()} failed {path}: {exc}\n")
            self.send_html(render_home(message))
            return
        if path == "/settings":
            projects = ", ".join(jira_sync_projects_from_text(data.get("jira_sync_projects", "")))
            with db() as conn:
                set_app_setting(conn, "jira_sync_projects", projects or "ESD, CS, FR, MB", ts)
                set_app_setting(conn, "jira_sync_include_assignee", "1" if data.get("jira_sync_include_assignee") == "1" else "0", ts)
                set_app_setting(conn, "jira_sync_include_reporter", "1" if data.get("jira_sync_include_reporter") == "1" else "0", ts)
            self.send_html(render_settings("Jira sync settings updated."))
            return
        if path == "/advisories":
            with db() as conn:
                conn.execute(
                    """
                    insert into advisories
                      (title, severity, product, affected_versions, relevance_terms, body, ticket_key, source_url, status, created_at, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.get("title", ""),
                        data.get("severity", "Info"),
                        data.get("product", ""),
                        data.get("affected_versions", ""),
                        tags_csv(data.get("relevance_terms", "")),
                        data.get("body", ""),
                        data.get("ticket_key", "").upper(),
                        data.get("source_url", ""),
                        data.get("status", "Active"),
                        ts,
                        ts,
                    ),
                )
            self.redirect("/#dashboard-advisories")
            return
        if path == "/ui/table-widths":
            key = data.get("key", "")
            try:
                raw_widths = json.loads(data.get("widths", "[]"))
            except json.JSONDecodeError:
                raw_widths = []
            widths = []
            if key.startswith("tam-console-column-widths:") and isinstance(raw_widths, list):
                for width in raw_widths[:50]:
                    try:
                        widths.append(max(0, min(2000, int(width) if width else 0)))
                    except (TypeError, ValueError):
                        widths.append(0)
            if not key.startswith("tam-console-column-widths:") or not widths:
                self.send_json({"ok": False, "message": "Invalid table width preference."}, 400)
                return
            with db() as conn:
                save_ui_preference(conn, key, json.dumps(widths), ts)
            self.send_json({"ok": True, "key": key, "widths": widths})
            return
        if path == "/slack/import":
            try:
                message = import_slack_records(data.get("payload", ""))
            except Exception as exc:
                message = f"Slack import failed: {exc}"
            self.send_html(render_home(message))
            return
        if path == "/jira/map-unmapped":
            wants_json = (
                "application/json" in self.headers.get("Accept", "")
                or self.headers.get("X-Requested-With", "") == "fetch"
            )
            mappings = []
            for field, values in getattr(self, "form_values", {}).items():
                if not field.startswith("map_"):
                    continue
                raw_customer_id = values[0].strip() if values else ""
                if not raw_customer_id:
                    continue
                try:
                    mappings.append((field[4:].upper(), int(raw_customer_id)))
                except ValueError:
                    continue
            if not mappings and data.get("key") and data.get("customer_id"):
                try:
                    mappings.append((data["key"].upper(), int(data["customer_id"])))
                except ValueError:
                    mappings = []
            if not mappings:
                if wants_json:
                    self.send_json({"ok": False, "message": "No Jira tickets were selected for mapping."}, 400)
                    return
                self.send_html(render_home("No Jira tickets were selected for mapping."))
                return
            mapped = 0
            skipped = 0
            mapped_keys = []
            with db() as conn:
                for key, customer_id in mappings:
                    ticket = conn.execute("select * from unmapped_jira_tickets where key = ?", (key,)).fetchone()
                    customer = conn.execute("select id from customers where id = ?", (customer_id,)).fetchone()
                    if ticket is None or customer is None:
                        skipped += 1
                        continue
                    conn.execute(
                        """
                        insert into tickets
                          (customer_id, key, summary, status, priority, assignee, updated, url, notes, synced_at, created_at)
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        on conflict(customer_id, key) do update set
                          summary=excluded.summary, status=excluded.status, priority=excluded.priority,
                          assignee=excluded.assignee, updated=excluded.updated, url=excluded.url,
                          notes=excluded.notes, synced_at=excluded.synced_at
                        """,
                        (
                            customer_id,
                            ticket["key"],
                            ticket["summary"],
                            ticket["status"],
                            ticket["priority"],
                            ticket["assignee"],
                            ticket["updated"],
                            ticket["url"],
                            ticket["notes"],
                            ts,
                            ts,
                        ),
                    )
                    conn.execute("delete from unmapped_jira_tickets where key = ?", (key,))
                    mapped += 1
                    mapped_keys.append(key)
            message = f"Mapped {mapped} Jira ticket(s)."
            if skipped:
                message += f" Skipped {skipped} missing ticket/customer mapping(s)."
            if wants_json:
                self.send_json(
                    {
                        "ok": True,
                        "message": message,
                        "mapped": mapped,
                        "skipped": skipped,
                        "mapped_keys": mapped_keys,
                    }
                )
                return
            self.send_html(render_home(message))
            return
        with db() as conn:
            if path == "/customers":
                name = data.get("name", "")
                slug = slugify(name)
                suffix = 2
                base = slug
                while conn.execute("select 1 from customers where slug = ?", (slug,)).fetchone():
                    slug = f"{base}-{suffix}"
                    suffix += 1
                conn.execute(
                    "insert into customers (slug, name, created_at, updated_at) values (?, ?, ?, ?)",
                    (slug, name, ts, ts),
                )
                self.redirect(f"/customers/{slug}")
                return

            if path == "/customers/bulk-hide":
                selected_ids = []
                for raw_id in getattr(self, "form_values", {}).get("customer_id", []):
                    try:
                        selected_ids.append(int(raw_id))
                    except ValueError:
                        continue
                if selected_ids:
                    placeholders = ",".join("?" for _ in selected_ids)
                    conn.execute(
                        f"update customers set is_hidden = 1, updated_at = ? where id in ({placeholders})",
                        (ts, *selected_ids),
                    )
                return_to = data.get("return_to", "/")
                if return_to != "/" and not return_to.startswith("/customers/"):
                    return_to = "/"
                self.redirect(return_to)
                return

            parts = path.split("/")
            if len(parts) != 4 or parts[1] != "customers":
                self.send_html(page("Bad request", "<section class='section'><h2>Bad request</h2></section>"), 400)
                return
            slug, action = parts[2], parts[3]
            customer = conn.execute("select id from customers where slug = ?", (slug,)).fetchone()
            if customer is None:
                self.send_html(page("Not found", "<section class='section'><h2>Customer not found</h2></section>"), 404)
                return
            cid = customer["id"]

            if action == "profile":
                conn.execute(
                    """
                    update customers
                    set aliases = ?, status = ?, owner = ?, region = ?, products = ?,
                        overview = ?, architecture = ?, health = ?, risk_reason = ?,
                        next_action = ?, next_action_due = ?, last_touch = ?, updated_at = ?
                    where id = ?
                    """,
                    (
                        data.get("aliases", ""),
                        data.get("status", ""),
                        data.get("owner", ""),
                        data.get("region", ""),
                        data.get("products", ""),
                        data.get("overview", ""),
                        data.get("architecture", ""),
                        data.get("health", "Unknown"),
                        data.get("risk_reason", ""),
                        data.get("next_action", ""),
                        data.get("next_action_due", ""),
                        data.get("last_touch", ""),
                        ts,
                        cid,
                    ),
                )
            elif action == "health":
                health = data.get("health", "Unknown")
                if normalize_match(health) not in {"unknown", "green", "yellow", "red"}:
                    health = "Unknown"
                conn.execute(
                    "update customers set health = ?, updated_at = ? where id = ?",
                    (health, ts, cid),
                )
                redirect_section = "overview"
            elif action == "pin":
                current = conn.execute(
                    "select is_pinned, sort_order from customers where id = ?",
                    (cid,),
                ).fetchone()
                if current["is_pinned"]:
                    conn.execute(
                        "update customers set is_pinned = 0, updated_at = ? where id = ?",
                        (ts, cid),
                    )
                else:
                    next_order = conn.execute(
                        "select coalesce(max(sort_order), 0) + 10 as n from customers where is_pinned = 1"
                    ).fetchone()["n"]
                    conn.execute(
                        "update customers set is_pinned = 1, sort_order = ?, updated_at = ? where id = ?",
                        (next_order, ts, cid),
                    )
                redirect_section = "overview"
            elif action in ("hide", "unhide"):
                is_hidden = 1 if action == "hide" else 0
                conn.execute(
                    "update customers set is_hidden = ?, updated_at = ? where id = ?",
                    (is_hidden, ts, cid),
                )
                redirect_section = "overview"
            elif action in ("move-up", "move-down"):
                conn.execute(
                    """
                    update customers
                    set sort_order = case when sort_order = 0 then id * 10 else sort_order end
                    where sort_order = 0
                    """
                )
                customer_order = conn.execute(
                    "select sort_order from customers where id = ?",
                    (cid,),
                ).fetchone()["sort_order"]
                if action == "move-up":
                    neighbor = conn.execute(
                        """
                        select id, sort_order from customers
                        where is_pinned = (select is_pinned from customers where id = ?)
                          and is_hidden = (select is_hidden from customers where id = ?)
                          and sort_order < ?
                        order by sort_order desc
                        limit 1
                        """,
                        (cid, cid, customer_order),
                    ).fetchone()
                else:
                    neighbor = conn.execute(
                        """
                        select id, sort_order from customers
                        where is_pinned = (select is_pinned from customers where id = ?)
                          and is_hidden = (select is_hidden from customers where id = ?)
                          and sort_order > ?
                        order by sort_order asc
                        limit 1
                        """,
                        (cid, cid, customer_order),
                    ).fetchone()
                if neighbor is not None:
                    conn.execute(
                        "update customers set sort_order = ?, updated_at = ? where id = ?",
                        (neighbor["sort_order"], ts, cid),
                    )
                    conn.execute(
                        "update customers set sort_order = ?, updated_at = ? where id = ?",
                        (customer_order, ts, neighbor["id"]),
                    )
                redirect_section = "overview"
            elif action == "sync-jira":
                message = sync_jira_tickets(cid)
                self.send_html(render_customer(slug, "tickets", message))
                return
            elif action == "discover-jira":
                message = discover_jira_tickets(cid)
                self.send_html(render_customer(slug, "tickets", message))
                return
            elif action == "environments":
                name = data.get("name", "")
                env_slug = slugify(name)
                suffix = 2
                base = env_slug
                while conn.execute(
                    "select 1 from environments where customer_id = ? and slug = ?",
                    (cid, env_slug),
                ).fetchone():
                    env_slug = f"{base}-{suffix}"
                    suffix += 1
                conn.execute(
                    """
                    insert into environments
                      (customer_id, slug, name, env_type, location, status, products, source_types, architecture, notes, created_at, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        env_slug,
                        name,
                        data.get("env_type", ""),
                        data.get("location", ""),
                        data.get("status", "Active"),
                        data.get("products", ""),
                        tags_csv(data.get("source_types", "")),
                        data.get("architecture", ""),
                        data.get("notes", ""),
                        ts,
                        ts,
                    ),
                )
            elif action == "environment-update":
                environment_id = int(data["environment_id"])
                name = data.get("name", "")
                conn.execute(
                    """
                    update environments
                    set name = ?, env_type = ?, location = ?, status = ?, products = ?, source_types = ?,
                        architecture = ?, notes = ?, updated_at = ?
                    where id = ? and customer_id = ?
                    """,
                    (
                        name,
                        data.get("env_type", ""),
                        data.get("location", ""),
                        data.get("status", ""),
                        data.get("products", ""),
                        tags_csv(data.get("source_types", "")),
                        data.get("architecture", ""),
                        data.get("notes", ""),
                        ts,
                        environment_id,
                        cid,
                    ),
                )
            elif action == "tickets":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                ticket_data = {
                    "key": data.get("key", "").upper(),
                    "summary": data.get("summary", ""),
                    "status": data.get("status", ""),
                    "priority": data.get("priority", ""),
                    "assignee": data.get("assignee", ""),
                    "updated": data.get("updated", ""),
                    "url": data.get("url", ""),
                    "notes": data.get("notes", ""),
                }
                try:
                    issue = jira_get_issue(ticket_data["key"])
                    item = issue_to_ticket_item(issue)
                    ticket_data = {
                        "key": item.get("key", ticket_data["key"]),
                        "summary": ticket_data["summary"] or item.get("summary", ""),
                        "status": ticket_data["status"] or item.get("status", ""),
                        "priority": ticket_data["priority"] or item.get("priority", ""),
                        "assignee": ticket_data["assignee"] or item.get("assignee", ""),
                        "updated": ticket_data["updated"] or str(item.get("updated_date", "")).split("T", 1)[0],
                        "url": ticket_data["url"] or item.get("url", ""),
                        "notes": ticket_data["notes"] or item.get("brief_summary", ""),
                    }
                except Exception:
                    pass
                conn.execute(
                    """
                    insert into tickets
                      (customer_id, environment_id, key, summary, status, priority, assignee, updated, url, notes, synced_at, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                    on conflict(customer_id, key) do update set
                      environment_id=excluded.environment_id,
                      summary=excluded.summary, status=excluded.status, priority=excluded.priority,
                      assignee=excluded.assignee, updated=excluded.updated, url=excluded.url, notes=excluded.notes
                    """,
                    (
                        cid,
                        environment_id,
                        ticket_data["key"],
                        ticket_data["summary"],
                        ticket_data["status"],
                        ticket_data["priority"],
                        ticket_data["assignee"],
                        ticket_data["updated"],
                        ticket_data["url"],
                        ticket_data["notes"],
                        ts,
                    ),
                )
            elif action == "staff":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                cur = conn.execute(
                    """
                    insert into staff
                      (customer_id, name, role, team, email, slack_handle, notes, created_at, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        data.get("name", ""),
                        data.get("role", ""),
                        data.get("team", ""),
                        data.get("email", ""),
                        data.get("slack_handle", ""),
                        data.get("notes", ""),
                        ts,
                        ts,
                    ),
                )
                if environment_id is not None:
                    conn.execute(
                        """
                        insert into environment_staff
                          (environment_id, staff_id, responsibility, created_at)
                        values (?, ?, ?, ?)
                        """,
                        (
                            environment_id,
                            cur.lastrowid,
                            data.get("responsibility", ""),
                            ts,
                        ),
                    )
            elif action == "staff-update":
                staff_id = int(data["staff_id"])
                staff_row = conn.execute(
                    "select id from staff where id = ? and customer_id = ?",
                    (staff_id, cid),
                ).fetchone()
                if staff_row is None:
                    self.send_html(page("Not found", "<section class='section'><h2>Staff not found</h2></section>"), 404)
                    return
                conn.execute(
                    """
                    update staff
                    set name = ?, role = ?, team = ?, email = ?, slack_handle = ?, notes = ?, updated_at = ?
                    where id = ? and customer_id = ?
                    """,
                    (
                        data.get("name", ""),
                        data.get("role", ""),
                        data.get("team", ""),
                        data.get("email", ""),
                        data.get("slack_handle", ""),
                        data.get("notes", ""),
                        ts,
                        staff_id,
                        cid,
                    ),
                )
                selected_ids = []
                for raw_id in getattr(self, "form_values", {}).get("environment_id", []):
                    if raw_id.strip():
                        selected_ids.append(int(raw_id))
                conn.execute("delete from environment_staff where staff_id = ?", (staff_id,))
                for environment_id in selected_ids:
                    env = conn.execute(
                        "select id from environments where id = ? and customer_id = ?",
                        (environment_id, cid),
                    ).fetchone()
                    if env is None:
                        continue
                    conn.execute(
                        """
                        insert into environment_staff
                          (environment_id, staff_id, responsibility, created_at)
                        values (?, ?, ?, ?)
                        """,
                        (
                            environment_id,
                            staff_id,
                            data.get(f"responsibility_{environment_id}", ""),
                            ts,
                        ),
                    )
                redirect_section = "staff"
            elif action == "hardware":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                conn.execute(
                    """
                    insert into hardware
                      (customer_id, environment_id, label, role, vendor, model, cpu, memory,
                       quantity, serials, status, notes, source, confidence, created_at, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        environment_id,
                        data.get("label", ""),
                        data.get("role", ""),
                        data.get("vendor", ""),
                        data.get("model", ""),
                        data.get("cpu", ""),
                        data.get("memory", ""),
                        data.get("quantity", ""),
                        data.get("serials", ""),
                        data.get("status", "Active"),
                        data.get("notes", ""),
                        data.get("source", ""),
                        data.get("confidence", "Needs confirmation"),
                        ts,
                        ts,
                    ),
                )
            elif action == "software":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                conn.execute(
                    """
                    insert into software_deployments
                      (customer_id, environment_id, product, version, version_notes,
                       deployment_mode, redundancy, node_count, status, notes,
                       source, confidence, created_at, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        environment_id,
                        data.get("product", ""),
                        data.get("version", ""),
                        data.get("version_notes", ""),
                        data.get("deployment_mode", ""),
                        data.get("redundancy", ""),
                        data.get("node_count", ""),
                        data.get("status", "Active"),
                        data.get("notes", ""),
                        data.get("source", ""),
                        data.get("confidence", "Needs confirmation"),
                        ts,
                        ts,
                    ),
                )
            elif action == "meetings":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                conn.execute(
                    """
                    insert into meetings
                      (customer_id, environment_id, meeting_date, title, attendees, summary, actions, url, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        environment_id,
                        data.get("meeting_date", ""),
                        data.get("title", ""),
                        data.get("attendees", ""),
                        data.get("summary", ""),
                        data.get("actions", ""),
                        data.get("url", ""),
                        ts,
                    ),
                )
            elif action == "notes":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                conn.execute(
                    """
                    insert into notes (customer_id, environment_id, note_type, title, body, source_url, created_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        environment_id,
                        data.get("note_type", "General"),
                        data.get("title", ""),
                        data.get("body", ""),
                        data.get("source_url", ""),
                        ts,
                    ),
                )
            elif action == "artifacts":
                environment_id = int(data["environment_id"]) if data.get("environment_id") else None
                conn.execute(
                    """
                    insert into artifacts (customer_id, environment_id, label, artifact_type, path_or_url, notes, created_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        environment_id,
                        data.get("label", ""),
                        data.get("artifact_type", ""),
                        data.get("path_or_url", ""),
                        data.get("notes", ""),
                        ts,
                    ),
                )
            else:
                self.send_html(page("Bad request", "<section class='section'><h2>Bad action</h2></section>"), 400)
                return
        redirect_section = {
            "profile": "overview",
            "health": "overview",
            "environments": "environments",
            "environment-update": "environments",
            "tickets": "tickets",
            "staff": "staff",
            "staff-update": "staff",
            "hardware": "hardware",
            "software": "software",
            "meetings": "meetings",
            "notes": "notes",
            "artifacts": "artifacts",
        }.get(action, "overview")
        self.redirect(f"/customers/{slug}" if redirect_section == "overview" else f"/customers/{slug}/{redirect_section}")

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    init_db()
    host = os.environ.get("CASEFILES_HOST", "0.0.0.0")
    port = int(os.environ.get("CASEFILES_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"TAG customer case files listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
