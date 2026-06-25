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


def rows(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return list(conn.execute(query, params).fetchall())


def row(query: str, params: tuple = ()) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(query, params).fetchone()


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


def jira_get_issue(issue_key: str) -> dict:
    cfg = load_atlassian_config()
    site = jira_site_base(cfg.BASE_URL)
    fields = ",".join(["summary", "status", "priority", "assignee", "updated"])
    url = f"{site}/rest/api/3/issue/{quote(issue_key)}?fields={quote(fields)}"
    token = base64.b64encode(f"{cfg.EMAIL}:{cfg.API_TOKEN}".encode()).decode()
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Jira HTTP {exc.code} for {issue_key}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Jira request failed for {issue_key}: {exc}") from exc


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

    items = []
    errors = []
    for key in keys:
        try:
            issue = jira_get_issue(key)
        except Exception as exc:
            errors.append(f"{key}: {exc}")
            continue
        fields = issue.get("fields", {})
        assignee = fields.get("assignee") or {}
        priority = fields.get("priority") or {}
        status = fields.get("status") or {}
        items.append(
            {
                "key": issue.get("key", key),
                "summary": fields.get("summary", ""),
                "status": status.get("name", ""),
                "priority": priority.get("name", ""),
                "assignee": assignee.get("displayName", ""),
                "updated_date": fields.get("updated", ""),
                "url": f"{jira_site_base(load_atlassian_config().BASE_URL)}/browse/{issue.get('key', key)}",
            }
        )
    synced = 0
    ts = now_utc()
    with db() as conn:
        for item in items:
            key = str(item.get("key", "")).strip()
            if not key or item.get("error"):
                continue
            updated_date = str(item.get("updated_date", "")).split("T", 1)[0]
            conn.execute(
                """
                update tickets
                set summary = ?, status = ?, priority = ?, assignee = ?,
                    updated = ?, url = ?,
                    notes = case when ? != '' then ? else notes end,
                    synced_at = ?
                where customer_id = ? and key = ?
                """,
                (
                    item.get("summary", ""),
                    item.get("status", ""),
                    item.get("priority", ""),
                    item.get("assignee", ""),
                    updated_date,
                    item.get("url", ""),
                    item.get("brief_summary", ""),
                    item.get("brief_summary", ""),
                    ts,
                    customer_id,
                    key,
                ),
            )
            synced += 1
    if errors:
        return f"Synced {synced} Jira ticket(s) at {ts}. Errors: {'; '.join(errors[:3])}"
    return f"Synced {synced} Jira ticket(s) at {ts}."


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
  <title>{esc(title)} | TAG Case Files</title>
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
    .stack {{ display: grid; gap: 16px; }}
    .section {{ padding: 18px; }}
    .section h2, .section h3 {{ margin: 0 0 12px; font-size: 18px; }}
    .muted {{ color: var(--muted); }}
    .grid-2 {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .facts {{ display: grid; grid-template-columns: 150px 1fr; gap: 8px 14px; }}
    .facts dt {{ color: var(--muted); }}
    .facts dd {{ margin: 0; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; vertical-align: top; padding: 9px 8px; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); font-weight: 600; font-size: 13px; }}
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
      gap: 8px;
      flex-wrap: wrap;
      margin: 0 0 16px;
      position: sticky;
      top: 56px;
      z-index: 1;
      background: var(--bg);
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }}
    .tabs a {{ color: var(--ink); padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
    .tabs a.active {{ background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }}
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
      .layout, .grid-2 {{ grid-template-columns: 1fr; }}
      header {{ padding: 0 14px; }}
      main {{ padding: 14px; }}
      .table-scroll table {{ min-width: 920px; }}
    }}
  </style>
</head>
<body>
  <script>
    const savedTheme = localStorage.getItem("casefiles-theme") || "dark";
    document.documentElement.dataset.theme = savedTheme;
    const savedSidebar = localStorage.getItem("casefiles-sidebar") || "collapsed";
    document.documentElement.dataset.sidebar = savedSidebar;
    function toggleTheme() {{
      const current = document.documentElement.dataset.theme || "dark";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("casefiles-theme", next);
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
      localStorage.setItem("casefiles-sidebar", next);
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
    window.addEventListener("DOMContentLoaded", () => {{
      document.querySelectorAll(".tag-editor").forEach(initTagEditor);
    }});
  </script>
  <header>
    <strong><a href="/">TAG Customer Case Files</a></strong>
    <button id="theme-toggle" class="theme-button" type="button" onclick="toggleTheme()">Light mode</button>
  </header>
  <main>{body}</main>
</body>
</html>""".encode()


def render_sidebar(active_slug: str = "") -> str:
    customers = rows("select slug, name, status from customers order by name")
    links = []
    for customer in customers:
        active = " active" if customer["slug"] == active_slug else ""
        links.append(
            f'<a class="customer-link{active}" href="/customers/{esc(customer["slug"])}">'
            f'{esc(customer["name"])}<br><span class="muted">{esc(customer["status"])}</span></a>'
        )
    return f"""<aside class="sidebar">
  <button id="sidebar-toggle" class="sidebar-toggle" type="button" onclick="toggleSidebar()" title="Toggle customers">></button>
  <div class="sidebar-content">
    <h3>Customers</h3>
    {''.join(links) or '<p class="muted">No customers yet.</p>'}
    <hr>
    <form method="post" action="/customers" style="border:0;padding:0;margin-top:14px">
      <label>New customer<input name="name" required placeholder="Customer name"></label>
      <button type="submit">Add</button>
    </form>
  </div>
</aside>"""


def render_home() -> bytes:
    count = row("select count(*) as n from customers")["n"]
    body = f"""<div class="layout">
  {render_sidebar()}
  <section class="section">
    <h2>Case File Dashboard</h2>
    <p class="muted">Track customer architecture, Jira issues, meetings, evidence, and next actions in one local place.</p>
    <dl class="facts">
      <dt>Customers</dt><dd>{count}</dd>
      <dt>Database</dt><dd>{esc(DB_PATH)}</dd>
      <dt>Next build step</dt><dd>Add Atlassian sync for selected customers once the reliable JQL fields are known.</dd>
    </dl>
  </section>
</div>"""
    return page("Dashboard", body)


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

    environment_options = '<option value="">Customer-wide</option>' + "".join(
        f'<option value="{env["id"]}">{esc(env["name"])}</option>'
        for env in environments
    )
    env_type_options = ("On-prem", "AWS", "GCP", "Azure", "Cloud", "Lab", "Hybrid", "Other")
    source_type_datalist = "".join(
        f'<option value="{esc(option)}"></option>' for option in SOURCE_TYPE_SUGGESTIONS
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
        f"""<tr>
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
    staff_rows = "".join(
        f"""<tr>
          <td>{esc(s['name'])}</td>
          <td>{esc(s['role'])}</td>
          <td>{esc(s['team'])}</td>
          <td>{esc(s['email'])}</td>
          <td>{esc(s['slack_handle'])}</td>
          <td>{esc(s['environments']) or '<span class="muted">Customer-wide / unassigned</span>'}</td>
        </tr>"""
        for s in staff
    )

    section_titles = {
        "overview": "Overview",
        "environments": "Environments",
        "architecture": "Architecture",
        "tickets": "Tickets",
        "staff": "Staff",
        "hardware": "Hardware",
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
      <dl class="facts">
        <dt>Status</dt><dd>{esc(customer['status'])}</dd>
        <dt>Aliases</dt><dd>{esc(customer['aliases'])}</dd>
        <dt>Owner</dt><dd>{esc(customer['owner']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Region</dt><dd>{esc(customer['region']) or '<span class="muted">Not set</span>'}</dd>
        <dt>Products</dt><dd>{esc(customer['products'])}</dd>
        <dt>Updated</dt><dd>{esc(customer['updated_at'])}</dd>
      </dl>
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
              <label>Status<input name="status" value="{esc(customer['status'])}"></label>
              <label>Products<input name="products" value="{esc(customer['products'])}"></label>
            </div>
            <label>Aliases<input name="aliases" value="{esc(customer['aliases'])}"></label>
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
                <label>Ticket key<input name="key" required placeholder="ESD-9106 or CS-1234" pattern="^(ESD|CS)-[0-9]+$"></label>
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
          <form method="post" action="/customers/{esc(customer['slug'])}/sync-jira" style="margin-bottom:12px">
            <button type="submit">Sync existing Jira tickets</button>
          </form>
          {f'<div class="table-scroll"><table><thead><tr><th>Key</th><th>Summary</th><th>Environment</th><th>Status</th><th>Priority</th><th>Assignee</th><th>Updated</th><th>Short summary</th><th>Synced</th></tr></thead><tbody>{ticket_rows}</tbody></table></div>' if tickets else '<div class="empty">No tickets linked yet.</div>'}
        </section>""",
        "staff": f"""<section class="section">
          <h3>Staff</h3>
          {f'<table><thead><tr><th>Name</th><th>Role</th><th>Team</th><th>Email</th><th>Slack</th><th>Environment mapping</th></tr></thead><tbody>{staff_rows}</tbody></table>' if staff else '<div class="empty">No customer staff mapped yet.</div>'}
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
        <dt>Status</dt><dd>{esc(customer['status'])}</dd>
        <dt>Products</dt><dd>{esc(customer['products'])}</dd>
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
    return {key: values[0].strip() for key, values in parsed.items()}


class Handler(BaseHTTPRequestHandler):
    def send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
                        overview = ?, architecture = ?, updated_at = ?
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
                        ts,
                        cid,
                    ),
                )
            elif action == "sync-jira":
                message = sync_jira_tickets(cid)
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
                        data.get("key", ""),
                        data.get("summary", ""),
                        data.get("status", ""),
                        data.get("priority", ""),
                        data.get("assignee", ""),
                        data.get("updated", ""),
                        data.get("url", ""),
                        data.get("notes", ""),
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
            "profile": "architecture",
            "environments": "environments",
            "environment-update": "environments",
            "tickets": "tickets",
            "staff": "staff",
            "hardware": "hardware",
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
