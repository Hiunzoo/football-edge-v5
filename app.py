from __future__ import annotations

import math
import csv
import io
import html
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

APP_NAME = "Football Edge v8.1 Diagnostic"
BASE_DIR = Path(__file__).resolve().parent

THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"
FOTMOB_BASE = "https://www.fotmob.com/api"
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/soccer"

# Curated ESPN soccer competitions. The same ESPN team ID is often reusable
# across competitions, so we can combine schedules from multiple competitions.
ESPN_LEAGUES = [
    # International / national teams
    "uefa.nations",
    "fifa.worldq.uefa",
    "uefa.euro",
    "uefa.euroq",
    "fifa.world",
    "fifa.friendly",
    # Major club competitions
    "uefa.champions",
    "uefa.europa",
    "eng.1",
    "esp.1",
    "ger.1",
    "ita.1",
    "fra.1",
    "ned.1",
    "por.1",
    "usa.1",
]

ESPN_NATIONAL_LEAGUES = [
    "uefa.nations",
    "fifa.worldq.uefa",
    "uefa.euro",
    "uefa.euroq",
    "fifa.world",
    "fifa.friendly",
]
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 600

app = FastAPI(title=APP_NAME, version="8.1")


class AnalyzeRequest(BaseModel):
    home_team: str
    away_team: str
    recent_matches: int = 10


@dataclass
class TeamRef:
    id: str
    name: str
    provider: str
    league_ids: list[str] | None = None
    alt_ids: dict[str, str] | None = None


def cache_get(key: str):
    item = CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any):
    CACHE[key] = (time.time(), value)


def http_json(url: str, *, params=None, headers=None, timeout=16):
    params = params or {}
    headers = headers or {}
    key = url + "?" + repr(sorted(params.items())) + repr(sorted(headers.items()))
    cached = cache_get(key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": UA, "Accept": "application/json", **headers},
            timeout=timeout,
        )
        if r.status_code == 429:
            raise RuntimeError("rate_limited")
        if r.status_code in (401, 403):
            raise RuntimeError("forbidden")
        if r.status_code == 404:
            raise RuntimeError("not_found")
        r.raise_for_status()
        data = r.json()
        cache_set(key, data)
        return data
    except requests.RequestException as e:
        raise RuntimeError(type(e).__name__) from e
    except ValueError as e:
        raise RuntimeError("invalid_json") from e


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.casefold()
    s = re.sub(r"[^a-z0-9\u00c0-\u024f\u4e00-\u9fff]+", " ", s)
    return " ".join(s.split())


def sim(a: str | None, b: str | None) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0
    if a == b:
        return 1
    if a in b or b in a:
        return .94
    return SequenceMatcher(None, a, b).ratio()


def recursive_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from recursive_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from recursive_dicts(item)


# -----------------------------
# ESPN public soccer API
# -----------------------------

ESPN_KNOWN: dict[str, TeamRef] = {}


def _espn_team_rows(payload: Any):
    rows = []
    sports = payload.get("sports") or []
    for sport in sports:
        for league in sport.get("leagues") or []:
            for item in league.get("teams") or []:
                team = item.get("team") or item
                if isinstance(team, dict):
                    rows.append(team)
    if payload.get("teams"):
        for item in payload["teams"]:
            team = item.get("team") if isinstance(item, dict) else None
            rows.append(team or item)
    return [r for r in rows if isinstance(r, dict)]


def _espn_global_search_candidates(query: str) -> list[tuple[float, TeamRef]]:
    """Resolve arbitrary soccer teams through ESPN's global search API."""
    try:
        data = http_json(
            "https://site.web.api.espn.com/apis/common/v3/search",
            params={
                "query": query,
                "limit": 50,
                "mode": "prefix",
                "region": "us",
                "lang": "en",
            },
        )
    except RuntimeError:
        return []

    items = data.get("items") or []
    candidates: list[tuple[float, TeamRef]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_type = str(
            item.get("type")
            or item.get("contentType")
            or item.get("resultType")
            or ""
        ).casefold()
        sport = str(
            item.get("sport")
            or item.get("sportSlug")
            or item.get("sportName")
            or ""
        ).casefold()

        # ESPN search can return news, athletes, leagues and teams together.
        # Keep team-like soccer entries; unknown type is accepted only when the
        # payload clearly contains a team id/name and soccer context.
        team_like = (
            raw_type in ("team", "teams", "club", "nationalteam", "national team")
            or "team" in raw_type
        )
        soccer_like = sport in ("soccer", "football") or "soccer" in sport

        # Some search results carry sport/league information inside nested fields.
        league_obj = item.get("league")
        if isinstance(league_obj, dict):
            league_slug = (
                league_obj.get("slug")
                or league_obj.get("abbreviation")
                or league_obj.get("name")
            )
        else:
            league_slug = (
                item.get("leagueSlug")
                or item.get("defaultLeagueSlug")
                or league_obj
            )

        display = (
            item.get("displayName")
            or item.get("name")
            or item.get("shortDisplayName")
            or item.get("title")
        )
        raw_id = (
            item.get("id")
            or item.get("teamId")
            or item.get("entityId")
            or item.get("uid")
        )

        if raw_id is None or not display:
            continue

        # If the API identifies the object as a team but omits the sport field,
        # allow it and rely on the later schedule call for validation.
        if raw_type and not team_like:
            continue
        if sport and not soccer_like:
            continue

        aliases = [
            display,
            item.get("shortDisplayName"),
            item.get("abbreviation"),
            item.get("location"),
            item.get("slug"),
        ]
        score = max([sim(query, x) for x in aliases if x] or [0])

        # Exact/near exact names should dominate; low similarity is rejected.
        if score < .52:
            continue

        candidates.append((
            score,
            TeamRef(
                str(raw_id),
                str(display),
                "espn",
                [],
                {
                    "league": str(league_slug or "all"),
                    "resolver": "espn-global-search",
                },
            ),
        ))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates


def espn_search_team(query: str) -> TeamRef | None:
    # 1) Dynamic global ESPN search — no hard-coded team IDs.
    candidates = _espn_global_search_candidates(query)
    if candidates:
        # Validate the leading candidates by checking whether ESPN exposes a
        # team schedule. This prevents similarly named news/entities winning.
        for _, ref in candidates[:6]:
            try:
                data = http_json(f"{ESPN_BASE}/all/teams/{ref.id}/schedule")
                if isinstance(data.get("events"), list):
                    return ref
            except RuntimeError:
                try:
                    data = http_json(f"{ESPN_WEB_BASE}/all/teams/{ref.id}/schedule")
                    if isinstance(data.get("events"), list):
                        return ref
                except RuntimeError:
                    pass

        # If validation endpoints are temporarily blocked, keep the best
        # team-search result and let provider fallback handle data retrieval.
        return candidates[0][1]

    # 2) ESPN all-soccer team catalogue when available.
    def score_rows(rows, league="all"):
        scored = []
        for t in rows:
            names = [
                t.get("displayName"),
                t.get("name"),
                t.get("shortDisplayName"),
                t.get("abbreviation"),
                t.get("location"),
            ]
            score = max([sim(query, n) for n in names if n] or [0])
            raw_id = t.get("id")
            name = t.get("displayName") or t.get("name")
            if raw_id is not None and name and score >= .52:
                scored.append((
                    score,
                    TeamRef(
                        str(raw_id),
                        str(name),
                        "espn",
                        [],
                        {"league": league, "resolver": "espn-team-catalogue"},
                    ),
                ))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    try:
        data = http_json(f"{ESPN_BASE}/all/teams")
        scored = score_rows(_espn_team_rows(data), "all")
        if scored:
            return scored[0][1]
    except RuntimeError:
        pass

    # 3) Last ESPN fallback: known competition catalogues. This is not a
    # hard-coded team list; it dynamically scans team lists.
    best = None
    for league in ESPN_LEAGUES:
        try:
            data = http_json(f"{ESPN_BASE}/{league}/teams")
        except RuntimeError:
            continue
        scored = score_rows(_espn_team_rows(data), league)
        if scored and (best is None or scored[0][0] > best[0]):
            best = scored[0]
        if best and best[0] >= .96:
            break

    return best[1] if best else None


def _espn_score(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v)
        return int(float(m.group())) if m else None
    if isinstance(v, dict):
        for key in ("value", "displayValue", "display", "current", "score"):
            if key in v:
                x = _espn_score(v.get(key))
                if x is not None:
                    return x
    return None


def parse_espn_event(raw: dict, league: str) -> dict | None:
    comps = raw.get("competitions") or []
    if not comps:
        return None
    comp = comps[0] or {}

    # Soccer schedules commonly put status on the competition, not the event.
    status_obj = raw.get("status") or comp.get("status") or {}
    status_type = status_obj.get("type") if isinstance(status_obj, dict) else {}
    status_type = status_type if isinstance(status_type, dict) else {}
    completed = status_type.get("completed")
    state = str(status_type.get("state") or "").lower()
    description = str(
        status_type.get("description")
        or status_type.get("detail")
        or status_type.get("shortDetail")
        or ""
    ).lower()

    competitors = comp.get("competitors") or []
    if len(competitors) < 2:
        return None

    home = away = None
    for idx, c in enumerate(competitors):
        if not isinstance(c, dict):
            continue
        ha = str(c.get("homeAway") or "").lower()
        if ha == "home":
            home = c
        elif ha == "away":
            away = c

    # Defensive fallback for API variants that omit homeAway.
    if home is None or away is None:
        ordered = [c for c in competitors if isinstance(c, dict)]
        ordered.sort(key=lambda c: c.get("order", 99))
        if len(ordered) >= 2:
            home = home or ordered[0]
            away = away or ordered[1]

    if not home or not away:
        return None

    ht = home.get("team") or {}
    at = away.get("team") or {}

    hs = _espn_score(home.get("score"))
    aas = _espn_score(away.get("score"))

    # Results endpoint sometimes contains status variants we don't know yet.
    # If both final scores exist, accept a past event even if "completed" is absent.
    if hs is None or aas is None:
        return None

    event_date = raw.get("date") or comp.get("date")
    if completed is False or state in ("pre", "in"):
        return None
    if completed is not True and state not in ("post", "final"):
        if any(x in description for x in ("scheduled", "postponed", "canceled", "cancelled")):
            return None

    league_name = None
    l = raw.get("league") or {}
    if isinstance(l, dict):
        league_name = l.get("name") or l.get("abbreviation")

    season = raw.get("season") or {}
    if not league_name and isinstance(season, dict):
        slug = season.get("slug")
        if slug:
            league_name = str(slug).replace("-", " ").title()

    league_name = (
        league_name
        or ((comp.get("type") or {}).get("text") if isinstance(comp.get("type"), dict) else None)
        or league
    )

    return {
        "id": raw.get("id") or comp.get("id"),
        "date": event_date,
        "home": ht.get("displayName") or ht.get("name") or home.get("displayName") or "",
        "away": at.get("displayName") or at.get("name") or away.get("displayName") or "",
        "home_id": str(ht.get("id") or home.get("id") or ""),
        "away_id": str(at.get("id") or away.get("id") or ""),
        "home_score": hs,
        "away_score": aas,
        "competition": league_name,
        "source": "ESPN",
    }


def _espn_scoreboard_events(league: str, start_date: datetime, end_date: datetime):
    """Fetch a broad date range from ESPN scoreboard.

    ESPN's soccer site API is undocumented and range support can vary by
    competition, so callers treat failures as a soft miss and try another
    competition or fallback provider.
    """
    date_range = f"{start_date:%Y%m%d}-{end_date:%Y%m%d}"
    try:
        data = http_json(
            f"{ESPN_BASE}/{league}/scoreboard",
            params={"dates": date_range, "limit": 1000},
        )
    except RuntimeError:
        return []

    out = []
    for raw in data.get("events") or []:
        e = parse_espn_event(raw, league)
        if e:
            out.append(e)
    return out


def _team_name_matches_event(e: dict, team: TeamRef) -> bool:
    return (
        str(e.get("home_id") or "") == str(team.id)
        or str(e.get("away_id") or "") == str(team.id)
        or sim(e.get("home"), team.name) > .90
        or sim(e.get("away"), team.name) > .90
    )


def _dedupe_events(events: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for e in events:
        key = e.get("id") or (
            e.get("date"),
            e.get("home"),
            e.get("away"),
            e.get("home_score"),
            e.get("away_score"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


def espn_all_schedule_events(team: TeamRef, n=20) -> list[dict]:
    """Fetch completed results from ESPN across multiple seasons.

    Club and national-team schedule endpoints do not always return the same
    amount of history without a season parameter, so aggregate several seasons
    before falling back to other providers.
    """
    events = []
    now = datetime.now()

    urls = [
        f"{ESPN_BASE}/all/teams/{team.id}/schedule",
        f"{ESPN_WEB_BASE}/all/teams/{team.id}/schedule",
    ]

    param_sets = [{}]
    for season in range(now.year, now.year - 5, -1):
        param_sets.append({"season": season})

    for url in urls:
        for params in param_sets:
            try:
                data = http_json(url, params=params)
            except RuntimeError:
                continue

            for raw in data.get("events") or []:
                e = parse_espn_event(raw, "ESPN All Competitions")
                if e and _team_name_matches_event(e, team):
                    events.append(e)

            events = _dedupe_events(events)
            if len(events) >= max(n, 15):
                break
        if len(events) >= max(n, 15):
            break

    return events[:max(n, 40)]


def espn_team_events(team: TeamRef, n=20) -> list[dict]:
    # Primary path: one team, all competitions. This is the route that the live
    # debug endpoint confirmed returns a populated schedule.
    events = espn_all_schedule_events(team, n)
    if len(events) >= min(n, 5):
        return events

    # Secondary fallback: all-soccer scoreboards by year. Do not rely on the
    # competition-specific /teams/{id}/schedule routes that returned 403.
    now = datetime.now()
    fallback = list(events)

    for year in range(now.year, now.year - 5, -1):
        start_date = datetime(year, 1, 1)
        end_date = now if year == now.year else datetime(year, 12, 31)

        try:
            data = http_json(
                f"{ESPN_BASE}/all/scoreboard",
                params={
                    "dates": f"{start_date:%Y%m%d}-{end_date:%Y%m%d}",
                    "limit": 1000,
                },
            )
        except RuntimeError:
            continue

        for raw in data.get("events") or []:
            e = parse_espn_event(raw, "ESPN Soccer")
            if e and _team_name_matches_event(e, team):
                fallback.append(e)

        fallback = _dedupe_events(fallback)
        if len(fallback) >= n:
            break

    return _dedupe_events(fallback)[:max(n, 30)]


# -----------------------------
# TheSportsDB
# -----------------------------

def tsdb_search_team(query: str) -> TeamRef | None:
    try:
        data = http_json(
            THESPORTSDB_BASE + "/searchteams.php",
            params={"t": query},
        )
    except RuntimeError:
        return None

    teams = data.get("teams") or []
    scored = []
    for t in teams:
        name = t.get("strTeam")
        if not name:
            continue
        score = sim(query, name)
        if score < .45:
            continue
        league_ids = []
        for k, v in t.items():
            if k.startswith("idLeague") and v:
                league_ids.append(str(v))
        scored.append((
            score,
            TeamRef(
                id=str(t.get("idTeam")),
                name=name,
                provider="thesportsdb",
                league_ids=list(dict.fromkeys(league_ids)),
                alt_ids={},
            )
        ))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def parse_tsdb_event(e: dict) -> dict | None:
    hs, aas = e.get("intHomeScore"), e.get("intAwayScore")
    if hs in (None, "") or aas in (None, ""):
        return None
    try:
        hs, aas = int(hs), int(aas)
    except (TypeError, ValueError):
        return None

    return {
        "id": e.get("idEvent"),
        "date": e.get("strTimestamp") or e.get("dateEvent"),
        "home": e.get("strHomeTeam") or "",
        "away": e.get("strAwayTeam") or "",
        "home_id": str(e.get("idHomeTeam") or ""),
        "away_id": str(e.get("idAwayTeam") or ""),
        "home_score": hs,
        "away_score": aas,
        "competition": e.get("strLeague") or e.get("strEvent"),
        "source": "TheSportsDB",
    }


def tsdb_team_events(team: TeamRef, n=20) -> list[dict]:
    events = []
    seen = set()

    # 1) Previous events endpoint: free tier may return only a small number,
    # but it is cheap and useful when available.
    try:
        data = http_json(
            THESPORTSDB_BASE + "/eventslast.php",
            params={"id": team.id},
        )
        for raw in data.get("results") or []:
            e = parse_tsdb_event(raw)
            if e:
                key = e["id"] or (e["date"], e["home"], e["away"])
                if key not in seen:
                    seen.add(key)
                    events.append(e)
    except RuntimeError:
        pass

    # 2) Season endpoint. Free tier exposes a limited number of season events,
    # but this often yields enough extra history to build a useful recent sample.
    year = datetime.now().year
    season_candidates = [
        f"{year}-{year+1}",
        f"{year-1}-{year}",
        str(year),
        str(year-1),
    ]
    for league_id in (team.league_ids or [])[:3]:
        for season in season_candidates:
            try:
                data = http_json(
                    THESPORTSDB_BASE + "/eventsseason.php",
                    params={"id": league_id, "s": season},
                )
            except RuntimeError:
                continue

            for raw in data.get("events") or []:
                hid = str(raw.get("idHomeTeam") or "")
                aid = str(raw.get("idAwayTeam") or "")
                hn = raw.get("strHomeTeam") or ""
                an = raw.get("strAwayTeam") or ""
                if (
                    team.id not in (hid, aid)
                    and sim(hn, team.name) < .90
                    and sim(an, team.name) < .90
                ):
                    continue
                e = parse_tsdb_event(raw)
                if e:
                    key = e["id"] or (e["date"], e["home"], e["away"])
                    if key not in seen:
                        seen.add(key)
                        events.append(e)

            if len(events) >= n:
                break
        if len(events) >= n:
            break

    events.sort(key=lambda x: x.get("date") or "", reverse=True)
    return events[:max(n, 20)]


# -----------------------------
# football-data.org (optional)
# -----------------------------

def fd_search_team(query: str) -> TeamRef | None:
    if not FOOTBALL_DATA_API_KEY:
        return None
    try:
        data = http_json(
            FOOTBALL_DATA_BASE + "/teams",
            params={"limit": 500},
            headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
        )
    except RuntimeError:
        return None

    scored = []
    for t in data.get("teams") or []:
        names = [t.get("name"), t.get("shortName"), t.get("tla")]
        score = max([sim(query, n) for n in names if n] or [0])
        if score >= .50:
            scored.append((
                score,
                TeamRef(
                    id=str(t.get("id")),
                    name=t.get("name") or t.get("shortName"),
                    provider="football-data",
                    league_ids=[],
                    alt_ids={},
                )
            ))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def fd_team_events(team: TeamRef, n=20) -> list[dict]:
    if not FOOTBALL_DATA_API_KEY:
        return []
    try:
        data = http_json(
            FOOTBALL_DATA_BASE + f"/teams/{team.id}/matches",
            params={"status": "FINISHED", "limit": min(max(n, 20), 100)},
            headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
        )
    except RuntimeError:
        return []

    out = []
    for m in data.get("matches") or []:
        score = (m.get("score") or {}).get("fullTime") or {}
        hs, aas = score.get("home"), score.get("away")
        if hs is None or aas is None:
            continue
        ht, at = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        comp = m.get("competition") or {}
        out.append({
            "id": m.get("id"),
            "date": m.get("utcDate"),
            "home": ht.get("name") or "",
            "away": at.get("name") or "",
            "home_id": str(ht.get("id") or ""),
            "away_id": str(at.get("id") or ""),
            "home_score": int(hs),
            "away_score": int(aas),
            "competition": comp.get("name"),
            "source": "football-data.org",
        })
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


# -----------------------------
# FotMob fallback
# -----------------------------

KNOWN_FOTMOB = {
    "denmark": TeamRef("8238", "Denmark", "fotmob", [], {}),
    "norway": TeamRef("8492", "Norway", "fotmob", [], {}),
}


def fotmob_search_team(query: str) -> TeamRef | None:
    known = KNOWN_FOTMOB.get(norm(query))
    if known:
        return known

    for path in ("/search/suggest", "/searchData"):
        try:
            data = http_json(
                FOTMOB_BASE + path,
                params={"term": query},
                headers={"Referer": "https://www.fotmob.com/"},
            )
        except RuntimeError:
            continue

        candidates = []
        for d in recursive_dicts(data):
            name = d.get("name") or d.get("localizedName") or d.get("teamName") or d.get("title")
            raw_id = d.get("id") or d.get("teamId")
            if not name or raw_id is None:
                continue
            score = sim(query, str(name))
            if score >= .48:
                candidates.append((score, TeamRef(str(raw_id), str(name), "fotmob", [], {})))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    return None


def _side_name(side):
    if isinstance(side, dict):
        return side.get("name") or side.get("teamName") or side.get("shortName")
    return side if isinstance(side, str) else None


def _side_id(side):
    if not isinstance(side, dict):
        return ""
    return str(side.get("id") or side.get("teamId") or "")


def _score_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        return int(m.group()) if m else None
    if isinstance(v, dict):
        for k in ("current", "display", "normaltime", "score", "total"):
            if k in v:
                x = _score_num(v[k])
                if x is not None:
                    return x
    return None


def fotmob_event_from_dict(d):
    home = d.get("home") or d.get("homeTeam")
    away = d.get("away") or d.get("awayTeam")
    hn = _side_name(home) or d.get("homeName")
    an = _side_name(away) or d.get("awayName")
    if not hn or not an:
        return None

    hs = _score_num(d.get("homeScore") or d.get("scoreHome") or d.get("homeGoals"))
    aas = _score_num(d.get("awayScore") or d.get("scoreAway") or d.get("awayGoals"))
    if hs is None or aas is None:
        score_str = d.get("scoreStr") or d.get("score") or d.get("result")
        if isinstance(score_str, str):
            m = re.search(r"(\d+)\s*[-:]\s*(\d+)", score_str)
            if m:
                hs, aas = int(m.group(1)), int(m.group(2))
    if hs is None or aas is None:
        return None

    comp = d.get("leagueName") or d.get("tournamentName") or d.get("league")
    if isinstance(comp, dict):
        comp = comp.get("name")

    date = d.get("utcTime") or d.get("startDate") or d.get("date")
    if not date and isinstance(d.get("status"), dict):
        date = d["status"].get("utcTime") or d["status"].get("startDate")

    return {
        "id": d.get("id") or d.get("matchId") or d.get("eventId"),
        "date": str(date or ""),
        "home": str(hn),
        "away": str(an),
        "home_id": _side_id(home),
        "away_id": _side_id(away),
        "home_score": hs,
        "away_score": aas,
        "competition": str(comp) if comp else None,
        "source": "FotMob",
    }


def fotmob_team_events(team: TeamRef, n=20) -> list[dict]:
    try:
        payload = http_json(
            FOTMOB_BASE + "/teams",
            params={"id": team.id},
            headers={"Referer": "https://www.fotmob.com/"},
        )
    except RuntimeError:
        return []

    out = []
    seen = set()
    for d in recursive_dicts(payload):
        e = fotmob_event_from_dict(d)
        if not e:
            continue
        key = e["id"] or (e["date"], e["home"], e["away"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:max(n, 20)]


def _event_date_key(e: dict) -> str:
    raw = str(e.get("date") or "")
    m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    if m:
        return m.group(0)
    m = re.search(r"\d{8}", raw)
    if m:
        s = m.group(0)
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return raw[:10]


def _same_event(a: dict, b: dict) -> bool:
    if _event_date_key(a) != _event_date_key(b):
        return False
    if a.get("home_score") != b.get("home_score") or a.get("away_score") != b.get("away_score"):
        return False
    return (
        sim(a.get("home"), b.get("home")) >= .78
        and sim(a.get("away"), b.get("away")) >= .78
    )


def merge_provider_events(event_groups: list[list[dict]]) -> list[dict]:
    """Merge histories from different providers without double-counting games."""
    merged: list[dict] = []
    for group in event_groups:
        for e in group:
            if e.get("home_score") is None or e.get("away_score") is None:
                continue
            if any(_same_event(e, old) for old in merged):
                continue
            merged.append(e)
    merged.sort(key=lambda x: (x.get("date") or ""), reverse=True)
    return merged


def canonical_team_ref(team_refs: list[TeamRef]) -> TeamRef | None:
    if not team_refs:
        return None
    priority = {"espn": 0, "football-data": 1, "thesportsdb": 2, "fotmob": 3}
    return sorted(team_refs, key=lambda r: priority.get(r.provider, 9))[0]


# -----------------------------
# Provider router
# -----------------------------

def resolve_team(query: str) -> list[TeamRef]:
    refs = []

    # If user configured an official football-data token, try it first.
    fd = fd_search_team(query)
    if fd:
        refs.append(fd)

    ep = espn_search_team(query)
    if ep:
        refs.append(ep)

    ts = tsdb_search_team(query)
    if ts:
        refs.append(ts)

    fm = fotmob_search_team(query)
    if fm:
        refs.append(fm)

    # Deduplicate by normalized name/provider combo.
    uniq = []
    seen = set()
    for r in refs:
        k = (r.provider, r.id)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def collect_best_events(team_refs: list[TeamRef], n=20):
    """Collect from every resolved provider, then merge and deduplicate.

    This is intentionally different from the old 'pick the provider with the
    most rows' strategy. A requested recent-10 sample can now be assembled from
    ESPN + TheSportsDB + FotMob + football-data when necessary.
    """
    diagnostics = []
    groups: list[list[dict]] = []

    for ref in team_refs:
        events = []
        provider_error = None
        try:
            if ref.provider == "football-data":
                events = fd_team_events(ref, max(n, 20))
            elif ref.provider == "espn":
                events = espn_team_events(ref, max(n, 20))
            elif ref.provider == "thesportsdb":
                events = tsdb_team_events(ref, max(n, 20))
            else:
                events = fotmob_team_events(ref, max(n, 20))
        except Exception as e:
            provider_error = f"{type(e).__name__}: {str(e)[:180]}"
            events = []

        groups.append(events)
        diagnostics.append({
            "provider": ref.provider,
            "team": ref.name,
            "team_id": ref.id,
            "events": len(events),
            "error": provider_error,
        })

    merged = merge_provider_events(groups)
    canonical = canonical_team_ref(team_refs)

    diagnostics.append({
        "provider": "merged",
        "team": canonical.name if canonical else "",
        "team_id": "",
        "events": len(merged),
        "error": None,
    })

    return canonical, merged[:max(n, 40)], diagnostics


# -----------------------------
# Analysis
# -----------------------------

def belongs(e, t: TeamRef):
    return (
        str(e.get("home_id") or "") == str(t.id)
        or str(e.get("away_id") or "") == str(t.id)
        or sim(e.get("home"), t.name) > .76
        or sim(e.get("away"), t.name) > .76
    )


def versus(e, a: TeamRef, b: TeamRef):
    return (
        max(sim(e.get("home"), a.name), sim(e.get("away"), a.name)) > .76
        and max(sim(e.get("home"), b.name), sim(e.get("away"), b.name)) > .76
    )


def recent(events, team, n):
    return [e for e in events if belongs(e, team)][:n]


def team_stats(events, team, venue=None):
    gf = ga = w = d = l = 0
    form = []
    used = []
    for e in events:
        is_home = (
            str(e.get("home_id") or "") == str(team.id)
            or sim(e.get("home"), team.name) > .76
        )
        if venue == "home" and not is_home:
            continue
        if venue == "away" and is_home:
            continue

        a, b = (
            (e["home_score"], e["away_score"])
            if is_home
            else (e["away_score"], e["home_score"])
        )
        used.append(e)
        gf += a
        ga += b
        if a > b:
            w += 1
            form.append("W")
        elif a == b:
            d += 1
            form.append("D")
        else:
            l += 1
            form.append("L")

    n = max(len(used), 1)
    return {
        "played": len(used),
        "wins": w,
        "draws": d,
        "losses": l,
        "gf_avg": round(gf / n, 3),
        "ga_avg": round(ga / n, 3),
        "ppg": round((w * 3 + d) / n, 3),
        "form": form,
    }



GLOBAL_ELO_CACHE_TTL = 21600  # 6 hours


def _cached_external(key: str):
    item = CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > GLOBAL_ELO_CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return value


def _cache_external(key: str, value: Any):
    CACHE[key] = (time.time(), value)


def _clubelo_slug(name: str) -> str:
    s = norm(name)
    # ClubElo commonly uses compact slugs: RealMadrid, ManCity, Liverpool...
    for suffix in (" fc", " afc", " cf", " sc"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    return re.sub(r"[^a-z0-9]", "", s)


def clubelo_rating(team_name: str):
    """Try ClubElo public CSV history for club teams."""
    slug = _clubelo_slug(team_name)
    if not slug:
        return None

    key = f"clubelo:{slug}"
    cached = _cached_external(key)
    if cached is not None:
        return cached

    urls = [
        f"https://api.clubelo.com/{slug}",
        f"http://api.clubelo.com/{slug}",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": UA})
            if r.status_code != 200 or "Elo" not in r.text[:500]:
                continue
            rows = list(csv.DictReader(io.StringIO(r.text)))
            good = []
            for row in rows:
                try:
                    elo = float(row.get("Elo") or "")
                except (TypeError, ValueError):
                    continue
                good.append((row, elo))
            if not good:
                continue
            row, elo = good[-1]
            value = {
                "elo": round(elo, 1),
                "source": "ClubElo",
                "club": row.get("Club") or team_name,
                "rank": row.get("Rank"),
            }
            _cache_external(key, value)
            return value
        except Exception:
            continue

    _cache_external(key, None)
    return None


def national_elo_rating(team_name: str):
    """Read current national-team Elo from footballratings.org.

    The page mirrors World Football Elo Ratings and is updated regularly.
    We parse the visible ranking text conservatively and fall back if unavailable.
    """
    key = f"nationalelo:{norm(team_name)}"
    cached = _cached_external(key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            "https://www.footballratings.org/",
            timeout=12,
            headers={"User-Agent": UA, "Accept": "text/html,*/*"},
        )
        if r.status_code != 200:
            _cache_external(key, None)
            return None

        # Strip markup, then search around an exact-ish team name for "Rating N".
        raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.I | re.S)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html.unescape(raw)
        raw = re.sub(r"\s+", " ", raw)

        escaped = re.escape(team_name.strip())
        patterns = [
            rf"\b{escaped}\b\s+Rating\s*([12]\d{{3}})",
            rf"\b{escaped}\b.*?\bRating\s*([12]\d{{3}})",
        ]
        elo = None
        for pat in patterns:
            m = re.search(pat, raw, flags=re.I)
            if m:
                elo = float(m.group(1))
                break

        if elo is None:
            _cache_external(key, None)
            return None

        value = {
            "elo": round(elo, 1),
            "source": "World Football Elo",
            "club": team_name,
            "rank": None,
        }
        _cache_external(key, value)
        return value
    except Exception:
        _cache_external(key, None)
        return None


def external_global_rating(team_name: str):
    """Resolve a global long-term prior without hard-coded team strengths.

    National Elo is attempted first; if not found, ClubElo is attempted.
    """
    nat = national_elo_rating(team_name)
    if nat:
        return nat
    club = clubelo_rating(team_name)
    if club:
        return club
    return None


def synthetic_elo_from_power(power: dict | None):
    """Convert internal 0-100 power into an Elo-like fallback scale."""
    if not power:
        return 1500.0
    return 1500.0 + (float(power.get("rating", 50.0)) - 50.0) * 10.0


def hybrid_strength(global_rating, internal_power):
    """Blend global long-term Elo with opponent-adjusted current form.

    External Elo is the anchor. Internal power only nudges it.
    """
    fallback = synthetic_elo_from_power(internal_power)
    if global_rating and global_rating.get("elo") is not None:
        global_elo = float(global_rating["elo"])
        # 78% long-term/global, 22% current opponent-adjusted form.
        rating = 0.78 * global_elo + 0.22 * fallback
        return {
            "rating": round(rating, 1),
            "global_elo": round(global_elo, 1),
            "form_elo": round(fallback, 1),
            "source": global_rating.get("source") or "External Elo",
            "external": True,
        }

    return {
        "rating": round(fallback, 1),
        "global_elo": None,
        "form_elo": round(fallback, 1),
        "source": "Internal opponent-adjusted fallback",
        "external": False,
    }


def weighted_team_form(events, team):
    """Recency-weighted form used by the power layer.

    Recent matches matter more, but this score alone is NOT the final rating;
    opponent quality is added separately.
    """
    total_w = pts = gf = ga = 0.0
    for i, e in enumerate(events[:15]):
        w = 0.90 ** i
        is_home = (
            str(e.get("home_id") or "") == str(team.id)
            or sim(e.get("home"), team.name) > .76
        )
        scored, conceded = (
            (e["home_score"], e["away_score"])
            if is_home else
            (e["away_score"], e["home_score"])
        )
        p = 3 if scored > conceded else (1 if scored == conceded else 0)
        total_w += w
        pts += p * w
        gf += scored * w
        ga += conceded * w

    if total_w <= 0:
        return {"ppg": 1.5, "gd": 0.0, "gf": 1.2, "ga": 1.2}

    return {
        "ppg": pts / total_w,
        "gd": (gf - ga) / total_w,
        "gf": gf / total_w,
        "ga": ga / total_w,
    }


def basic_power_from_events(events, team):
    form = weighted_team_form(events, team)
    # Neutral team ~= 50. Strong recent performance can move the rating,
    # but is deliberately capped because schedule quality is handled separately.
    rating = (
        50
        + (form["ppg"] - 1.5) * 13.0
        + clamp(form["gd"], -2.0, 2.0) * 7.0
    )
    return clamp(rating, 25, 82)


def _event_opponent(e, team):
    is_home = (
        str(e.get("home_id") or "") == str(team.id)
        or sim(e.get("home"), team.name) > .76
    )
    if is_home:
        return {
            "name": e.get("away") or "",
            "id": str(e.get("away_id") or ""),
            "source": e.get("source") or "",
        }
    return {
        "name": e.get("home") or "",
        "id": str(e.get("home_id") or ""),
        "source": e.get("source") or "",
    }


def quick_opponent_power(opponent):
    """Estimate opponent strength without recursive opponent-quality calls.

    Cached aggressively because the same opponent can appear in many analyses.
    """
    name = (opponent.get("name") or "").strip()
    oid = str(opponent.get("id") or "")
    source = str(opponent.get("source") or "")
    if not name:
        return 50.0

    key = f"power:{norm(name)}:{oid}:{source}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    refs = []
    # If an ESPN event supplied the opponent ID, use it directly.
    if source.upper().startswith("ESPN") and oid:
        refs.append(TeamRef(oid, name, "espn", [], {"league": "all"}))

    # Add dynamically resolved refs as fallbacks.
    try:
        for r in resolve_team(name):
            if (r.provider, r.id) not in {(x.provider, x.id) for x in refs}:
                refs.append(r)
    except Exception:
        pass

    best = []
    best_ref = None
    for ref in refs[:4]:
        try:
            if ref.provider == "espn":
                rows = espn_team_events(ref, 6)
            elif ref.provider == "thesportsdb":
                rows = tsdb_team_events(ref, 6)
            elif ref.provider == "football-data":
                rows = fd_team_events(ref, 6)
            else:
                rows = fotmob_team_events(ref, 6)
        except Exception:
            rows = []

        if len(rows) > len(best):
            best = rows
            best_ref = ref
        if len(best) >= 5:
            break

    rating = basic_power_from_events(best, best_ref) if best_ref and best else 50.0
    cache_set(key, rating)
    return rating


def team_power_rating(events, team):
    """Opponent-adjusted 0-100-ish team strength layer."""
    base = basic_power_from_events(events, team)

    # Use only a handful of recent distinct opponents to control latency.
    opponents = []
    seen = set()
    for e in events[:10]:
        opp = _event_opponent(e, team)
        k = norm(opp["name"])
        if not k or k in seen:
            continue
        seen.add(k)
        opponents.append(opp)
        if len(opponents) >= 5:
            break

    opp_ratings = [quick_opponent_power(o) for o in opponents]
    schedule = sum(opp_ratings) / len(opp_ratings) if opp_ratings else 50.0

    # About 35% of the opponent-quality difference is transferred into rating.
    adjusted = base + (schedule - 50.0) * 0.35
    adjusted = clamp(adjusted, 20, 92)

    return {
        "rating": round(adjusted, 1),
        "base_form": round(base, 1),
        "opponent_strength": round(schedule, 1),
        "opponents_sampled": len(opp_ratings),
    }


def poisson(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def dc_tau(h, a, hx, ax, rho=-0.08):
    if h == 0 and a == 0:
        return max(.05, 1 - hx * ax * rho)
    if h == 0 and a == 1:
        return max(.05, 1 + hx * rho)
    if h == 1 and a == 0:
        return max(.05, 1 + ax * rho)
    if h == 1 and a == 1:
        return max(.05, 1 - rho)
    return 1.0


def model_prediction(hs, aas, hv, av, h2h, home_power=None, away_power=None, home_strength=None, away_strength=None):
    hgf = hv["gf_avg"] if hv["played"] >= 2 else hs["gf_avg"]
    hga = hv["ga_avg"] if hv["played"] >= 2 else hs["ga_avg"]
    agf = av["gf_avg"] if av["played"] >= 2 else aas["gf_avg"]
    aga = av["ga_avg"] if av["played"] >= 2 else aas["ga_avg"]

    hgf = .60 * hgf + .40 * hs["gf_avg"]
    hga = .60 * hga + .40 * hs["ga_avg"]
    agf = .60 * agf + .40 * aas["gf_avg"]
    aga = .60 * aga + .40 * aas["ga_avg"]

    hx = ((hgf + aga) / 2) * 1.08
    ax = ((agf + hga) / 2) * .96

    diagnostic_raw_hx = hx
    diagnostic_raw_ax = ax

    # Recent form still matters, but much less than in the old model.
    ppg_edge = clamp(hs["ppg"] - aas["ppg"], -1.5, 1.5)
    hx *= 1 + ppg_edge * .025
    ax *= 1 - ppg_edge * .020

    diagnostic_form_hx = hx
    diagnostic_form_ax = ax

    # v8 Global Elo Hybrid layer.
    # Long-term/global strength is the anchor; recent form is already blended into
    # home_strength / away_strength and only nudges the prior.
    elo_diff = 0.0
    home_advantage_elo = 55.0
    effective_diff = home_advantage_elo
    elo_factor = 1.0

    if home_strength and away_strength:
        elo_diff = clamp(
            float(home_strength["rating"]) - float(away_strength["rating"]),
            -500.0,
            500.0,
        )

        # A modest home advantage. It cannot erase a large global Elo gap.
        effective_diff = elo_diff + home_advantage_elo

        # Translate Elo gap into attack-rate adjustment.
        elo_factor = math.exp((effective_diff / 400.0) * 0.72)
        elo_factor = clamp(elo_factor, .58, 1.72)
        hx *= elo_factor
        ax /= elo_factor

    diagnostic_elo_hx = hx
    diagnostic_elo_ax = ax

    if h2h:
        last = h2h[:5]
        total = sum(e["home_score"] + e["away_score"] for e in last) / len(last)
        cur = max(hx + ax, .1)
        blend = .88 * cur + .12 * total
        scale = clamp(blend / cur, .88, 1.12)
        hx *= scale
        ax *= scale

    diagnostic_preclamp_hx = hx
    diagnostic_preclamp_ax = ax

    hx = clamp(hx, .25, 3.8)
    ax = clamp(ax, .20, 3.5)

    cells = []
    hp = dp = ap = over = btts = 0.0

    for h in range(9):
        for a in range(9):
            p = poisson(h, hx) * poisson(a, ax) * dc_tau(h, a, hx, ax)
            cells.append((p, h, a))
            if h > a:
                hp += p
            elif h == a:
                dp += p
            else:
                ap += p
            if h + a >= 3:
                over += p
            if h > 0 and a > 0:
                btts += p

    mass = sum(p for p, _, _ in cells)
    cells = [(p / mass, h, a) for p, h, a in cells]
    hp, dp, ap, over, btts = [x / mass for x in (hp, dp, ap, over, btts)]
    cells.sort(reverse=True)

    top10 = [
        {"home": h, "away": a, "probability": round(p * 100, 1)}
        for p, h, a in cells[:10]
    ]

    sample = min(hs["played"] + aas["played"], 20)
    venue_sample = min(hv["played"] + av["played"], 10)
    min_side_sample = min(hs["played"], aas["played"])

    if min_side_sample >= 8:
        sample_quality = "normal"
        sample_penalty = 0
    elif min_side_sample >= 5:
        sample_quality = "medium"
        sample_penalty = 8
    else:
        sample_quality = "low"
        sample_penalty = 18

    conf = int(clamp(
        45 + sample * 1.35 + venue_sample * 1.1 + min(len(h2h), 5) * 2.0 - sample_penalty,
        28,
        90
    ))

    return {
        "expected_goals": {
            "home": round(hx, 2),
            "away": round(ax, 2),
            "total": round(hx + ax, 2),
        },
        "outcomes": {
            "home": round(hp * 100, 1),
            "draw": round(dp * 100, 1),
            "away": round(ap * 100, 1),
            "home_or_draw": round((hp + dp) * 100, 1),
            "away_or_draw": round((ap + dp) * 100, 1),
        },
        "markets": {
            "over_2_5": round(over * 100, 1),
            "under_2_5": round((1 - over) * 100, 1),
            "btts_yes": round(btts * 100, 1),
            "btts_no": round((1 - btts) * 100, 1),
        },
        "top_scores": top10[:3],
        "top_scores_10": top10,
        "confidence": conf,
        "sample_quality": sample_quality,
        "sample_counts": {
            "home": hs["played"],
            "away": aas["played"],
        },
        "power": {
            "home": home_power,
            "away": away_power,
            "difference": round(
                (home_power["rating"] - away_power["rating"])
                if home_power and away_power else 0.0,
                1
            ),
        },
        "strength": {
            "home": home_strength,
            "away": away_strength,
            "difference": round(
                (home_strength["rating"] - away_strength["rating"])
                if home_strength and away_strength else 0.0,
                1
            ),
        },
        "diagnostic": {
            "raw_xg": {
                "home": round(diagnostic_raw_hx, 3),
                "away": round(diagnostic_raw_ax, 3),
            },
            "after_recent_form_xg": {
                "home": round(diagnostic_form_hx, 3),
                "away": round(diagnostic_form_ax, 3),
            },
            "elo": {
                "home_global_elo": home_strength.get("global_elo") if home_strength else None,
                "away_global_elo": away_strength.get("global_elo") if away_strength else None,
                "home_form_elo": home_strength.get("form_elo") if home_strength else None,
                "away_form_elo": away_strength.get("form_elo") if away_strength else None,
                "home_hybrid": home_strength.get("rating") if home_strength else None,
                "away_hybrid": away_strength.get("rating") if away_strength else None,
                "home_source": home_strength.get("source") if home_strength else None,
                "away_source": away_strength.get("source") if away_strength else None,
                "home_external": home_strength.get("external") if home_strength else False,
                "away_external": away_strength.get("external") if away_strength else False,
                "rating_difference": round(elo_diff, 1),
                "home_advantage": round(home_advantage_elo, 1),
                "effective_difference": round(effective_diff, 1),
                "xg_factor": round(elo_factor, 4),
            },
            "after_elo_xg": {
                "home": round(diagnostic_elo_hx, 3),
                "away": round(diagnostic_elo_ax, 3),
            },
            "before_final_clamp_xg": {
                "home": round(diagnostic_preclamp_hx, 3),
                "away": round(diagnostic_preclamp_ax, 3),
            },
            "final_xg": {
                "home": round(hx, 3),
                "away": round(ax, 3),
            },
            "recent_form": {
                "home_ppg": hs.get("ppg"),
                "away_ppg": aas.get("ppg"),
                "ppg_edge": round(ppg_edge, 3),
            },
            "h2h_matches_used": min(len(h2h), 5),
        },
    }


# -----------------------------
# ESPN diagnostics
# -----------------------------

def debug_get(url: str, params: dict | None = None) -> dict:
    """Return raw HTTP diagnostics without raising FastAPI errors."""
    params = params or {}
    result = {
        "url": url,
        "params": params,
        "status": None,
        "content_type": None,
        "json": False,
        "keys": [],
        "events_count": None,
        "error": None,
    }

    try:
        r = requests.get(
            url,
            params=params,
            timeout=15,
            headers={
                "User-Agent": UA,
                "Accept": "application/json,text/plain,*/*",
            },
        )
        result["status"] = r.status_code
        result["content_type"] = r.headers.get("content-type")

        try:
            data = r.json()
            result["json"] = True
            if isinstance(data, dict):
                result["keys"] = list(data.keys())[:30]
                events = data.get("events")
                if isinstance(events, list):
                    result["events_count"] = len(events)

                # Small safe preview so we can understand alternate response shapes.
                preview = {}
                for key in (
                    "name", "timestamp", "status", "season",
                    "team", "requestedSeason", "league",
                ):
                    if key in data:
                        preview[key] = data[key]
                if preview:
                    result["preview"] = preview
            elif isinstance(data, list):
                result["events_count"] = len(data)
        except Exception as e:
            result["error"] = f"non-json response: {type(e).__name__}; first100={r.text[:100]!r}"

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


@app.get("/api/debug-team/{team_name}")
def debug_team(team_name: str):
    """Diagnose ESPN team resolution and league schedule endpoints.

    Example:
      /api/debug-team/Denmark
      /api/debug-team/Norway
    """
    q = team_name.strip()
    resolved = espn_search_team(q)

    if not resolved:
        return {
            "ok": False,
            "query": q,
            "message": "ESPN team resolution failed.",
            "known_team": None,
        }

    current_year = datetime.now().year
    resolved_league = (resolved.alt_ids or {}).get("league", "all")
    leagues = ESPN_NATIONAL_LEAGUES if resolved_league in ESPN_NATIONAL_LEAGUES else ESPN_LEAGUES

    tests = []

    # Test the v5.3 guessed all-competition route as a diagnostic only.
    tests.append({
        "name": "all-web-no-season",
        **debug_get(f"{ESPN_WEB_BASE}/all/teams/{resolved.id}/schedule"),
    })

    # Test the documented competition-scoped schedule path.
    for league in leagues:
        for season in (current_year, current_year - 1, current_year - 2):
            row = debug_get(
                f"{ESPN_BASE}/{league}/teams/{resolved.id}/schedule",
                {"season": season},
            )
            row["name"] = f"{league} / {season}"
            tests.append(row)

    # Also test teams listing, to see whether the ID exists in each namespace.
    namespace_tests = []
    for league in leagues:
        data = debug_get(f"{ESPN_BASE}/{league}/teams")
        data["name"] = f"{league} teams"
        namespace_tests.append(data)

    successful = [
        x for x in tests
        if x.get("status") == 200 and (x.get("events_count") or 0) > 0
    ]

    return {
        "ok": True,
        "query": q,
        "resolved": asdict(resolved),
        "resolver_mode": "dynamic",
        "successful_schedule_tests": [
            {
                "name": x["name"],
                "events_count": x["events_count"],
                "status": x["status"],
            }
            for x in successful
        ],
        "schedule_tests": tests,
        "namespace_tests": namespace_tests,
        "instructions": (
            "The production collector now prioritizes the all-competitions "
            "team schedule and parses competition-level status/score objects."
        ),
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    # Always return JSON so the frontend never receives an HTML/plain-text 500.
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                "伺服器分析時發生未預期錯誤："
                f"{type(exc).__name__}: {str(exc)[:220]}"
            )
        },
    )


def _teamref_public(ref: TeamRef) -> dict:
    return {
        "id": ref.id,
        "name": ref.name,
        "provider": ref.provider,
        "league_ids": ref.league_ids or [],
        "alt_ids": ref.alt_ids or {},
    }


def _candidate_key(ref: TeamRef):
    return (ref.provider, str(ref.id))


def _candidate_score(query: str, ref: TeamRef) -> float:
    score = sim(query, ref.name)
    qn, rn = norm(query), norm(ref.name)
    if qn and rn:
        # tolerate common suffix/prefix forms like Liverpool -> Liverpool FC
        suffixes = (" fc", " cf", " afc", " sc", " calcio", " club")
        rn2 = rn
        for suf in suffixes:
            if rn2.endswith(suf):
                rn2 = rn2[:-len(suf)].strip()
        if qn == rn2:
            score = max(score, .995)
        elif qn in rn2 or rn2 in qn:
            score = max(score, .94)
    return score


@app.get("/api/team-search")
def team_search(q: str = Query(..., min_length=2), limit: int = Query(6, ge=1, le=10)):
    query = q.strip()
    refs = resolve_team(query)

    # If resolver returns too few candidates, probe individual providers directly.
    extras = []
    for fn in (espn_search_team, tsdb_search_team, fotmob_search_team, fd_search_team):
        try:
            ref = fn(query)
            if ref:
                extras.append(ref)
        except Exception:
            pass

    merged = []
    seen = set()
    for ref in list(refs) + extras:
        k = _candidate_key(ref)
        if k in seen:
            continue
        seen.add(k)
        merged.append(ref)

    scored = sorted(
        [(_candidate_score(query, ref), ref) for ref in merged],
        key=lambda x: x[0],
        reverse=True,
    )

    results = []
    for score, ref in scored[:limit]:
        item = _teamref_public(ref)
        item["score"] = round(score, 3)
        results.append(item)

    auto = results[0] if results and results[0]["score"] >= .92 else None
    return {
        "query": query,
        "auto_select": auto,
        "results": results,
    }


@app.post("/api/diagnose")
def diagnose(req: AnalyzeRequest):
    """Return the normal analysis plus a compact model diagnostic view."""
    result = analyze(req)
    p = result.get("prediction") or {}
    return {
        "teams": result.get("teams"),
        "recent": {
            "home_count": len((result.get("recent") or {}).get("home") or []),
            "away_count": len((result.get("recent") or {}).get("away") or []),
        },
        "outcomes": p.get("outcomes"),
        "expected_goals": p.get("expected_goals"),
        "power": p.get("power"),
        "strength": p.get("strength"),
        "diagnostic": p.get("diagnostic"),
        "providers": result.get("diagnostics"),
        "meta": result.get("meta"),
    }


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": APP_NAME,
        "version": "8.1",
        "providers": {
            "football-data": bool(FOOTBALL_DATA_API_KEY),
            "espn": True,
            "thesportsdb": True,
            "fotmob_fallback": True,
        },
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    if norm(req.home_team) == norm(req.away_team):
        raise HTTPException(400, detail="請輸入兩支不同的球隊。")

    n = max(5, min(req.recent_matches, 20))

    try:
        home_refs = resolve_team(req.home_team)
    except Exception as e:
        raise HTTPException(
            502,
            detail=f"主隊搜尋失敗：{type(e).__name__}: {str(e)[:160]}"
        )

    try:
        away_refs = resolve_team(req.away_team)
    except Exception as e:
        raise HTTPException(
            502,
            detail=f"客隊搜尋失敗：{type(e).__name__}: {str(e)[:160]}"
        )

    if not home_refs:
        raise HTTPException(404, detail=f"找不到主隊「{req.home_team}」。")
    if not away_refs:
        raise HTTPException(404, detail=f"找不到客隊「{req.away_team}」。")

    home, home_all, home_diag = collect_best_events(home_refs, n)
    away, away_all, away_diag = collect_best_events(away_refs, n)

    if not home:
        home = home_refs[0]
    if not away:
        away = away_refs[0]

    hr = recent(home_all, home, n)
    ar = recent(away_all, away, n)

    if len(hr) < 3 or len(ar) < 3:
        def _diag_text(rows):
            parts = []
            for x in rows:
                s = f"{x['provider']}={x['events']}場"
                if x.get("error"):
                    s += f"(錯誤:{x['error']})"
                parts.append(s)
            return ", ".join(parts) or "無"
        raise HTTPException(
            422,
            detail=(
                f"合併所有可用資料源後仍不足，至少需要每隊 3 場才能產生預測。"
                f" 目前 {home.name} {len(hr)} 場、{away.name} {len(ar)} 場。"
                f" 主隊來源：{_diag_text(home_diag)}；"
                f"客隊來源：{_diag_text(away_diag)}。"
                f" 自動解析：{home.name}({home.provider}:{home.id}) / "
                f"{away.name}({away.provider}:{away.id})。"
            ),
        )

    union = []
    seen = set()
    for e in home_all + away_all:
        key = e.get("id") or (
            e.get("date"), e.get("home"), e.get("away"),
            e.get("home_score"), e.get("away_score")
        )
        if key not in seen:
            seen.add(key)
            union.append(e)

    h2h = [e for e in union if versus(e, home, away)]
    h2h.sort(key=lambda e: e.get("date") or "", reverse=True)
    h2h = h2h[:10]

    hs = team_stats(hr, home)
    aas = team_stats(ar, away)
    hv = team_stats(hr, home, "home")
    av = team_stats(ar, away, "away")

    home_power = team_power_rating(hr, home)
    away_power = team_power_rating(ar, away)

    home_global = external_global_rating(home.name)
    away_global = external_global_rating(away.name)
    home_strength = hybrid_strength(home_global, home_power)
    away_strength = hybrid_strength(away_global, away_power)

    pred = model_prediction(
        hs, aas, hv, av, h2h,
        home_power=home_power,
        away_power=away_power,
        home_strength=home_strength,
        away_strength=away_strength,
    )

    return {
        "teams": {
            "home": asdict(home),
            "away": asdict(away),
        },
        "recent": {
            "home": hr,
            "away": ar,
        },
        "stats": {
            "home": hs,
            "away": aas,
            "home_venue": hv,
            "away_venue": av,
        },
        "h2h": h2h,
        "prediction": pred,
        "diagnostics": {
            "home": home_diag,
            "away": away_diag,
            "resolved_home": asdict(home),
            "resolved_away": asdict(away),
        },
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_home": home.provider,
            "source_away": away.provider,
            "football_data_enabled": bool(FOOTBALL_DATA_API_KEY),
            "note": ("v8.1 Diagnostic exposes Global Elo source, hybrid strength, home advantage, xG transformation stages, and final Dixon-Coles probabilities."),
        },
    }
