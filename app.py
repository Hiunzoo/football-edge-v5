from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

APP_NAME = "Football Edge v5.2"
BASE_DIR = Path(__file__).resolve().parent

THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"
FOTMOB_BASE = "https://www.fotmob.com/api"
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

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

app = FastAPI(title=APP_NAME, version="5.2")


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

ESPN_KNOWN = {
    "denmark": TeamRef("479", "Denmark", "espn", [], {"league": "uefa.nations"}),
    "norway": TeamRef("464", "Norway", "espn", [], {"league": "uefa.nations"}),
}


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


def espn_search_team(query: str) -> TeamRef | None:
    known = ESPN_KNOWN.get(norm(query))
    if known:
        return known

    best = None
    best_score = 0.0
    best_league = None

    for league in ESPN_LEAGUES:
        try:
            data = http_json(f"{ESPN_BASE}/{league}/teams")
        except RuntimeError:
            continue

        for t in _espn_team_rows(data):
            names = [
                t.get("displayName"),
                t.get("name"),
                t.get("shortDisplayName"),
                t.get("abbreviation"),
            ]
            score = max([sim(query, n) for n in names if n] or [0])
            if score > best_score:
                raw_id = t.get("id")
                name = t.get("displayName") or t.get("name")
                if raw_id is not None and name:
                    best = TeamRef(str(raw_id), str(name), "espn", [], {"league": league})
                    best_score = score
                    best_league = league

        if best_score >= .96:
            break

    return best if best_score >= .56 else None


def parse_espn_event(raw: dict, league: str) -> dict | None:
    comps = raw.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    status = ((raw.get("status") or {}).get("type") or {})
    completed = status.get("completed")
    state = str(status.get("state") or "").lower()
    if completed is not True and state not in ("post", "final"):
        return None

    competitors = comp.get("competitors") or []
    home = away = None
    for c in competitors:
        ha = str(c.get("homeAway") or "").lower()
        if ha == "home":
            home = c
        elif ha == "away":
            away = c
    if not home or not away:
        return None

    ht = home.get("team") or {}
    at = away.get("team") or {}
    hs = home.get("score")
    aas = away.get("score")
    try:
        hs = int(float(hs))
        aas = int(float(aas))
    except (TypeError, ValueError):
        return None

    league_name = None
    l = raw.get("league") or {}
    if isinstance(l, dict):
        league_name = l.get("name") or l.get("abbreviation")
    league_name = league_name or league

    return {
        "id": raw.get("id"),
        "date": raw.get("date"),
        "home": ht.get("displayName") or ht.get("name") or "",
        "away": at.get("displayName") or at.get("name") or "",
        "home_id": str(ht.get("id") or ""),
        "away_id": str(at.get("id") or ""),
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


def espn_team_events(team: TeamRef, n=20) -> list[dict]:
    events = []
    seen = set()
    now = datetime.now()

    # National teams move between competition namespaces. Instead of assuming
    # one team schedule endpoint can see all matches, scan the international
    # competition scoreboards and filter by team name.
    is_known_national = norm(team.name) in ESPN_KNOWN
    leagues = ESPN_NATIONAL_LEAGUES if is_known_national else ESPN_LEAGUES

    # Two overlapping windows improve reliability when a competition endpoint
    # truncates a very large range. About 34 months is enough for 10 recent
    # national-team matches in normal circumstances.
    windows = [
        (datetime(now.year - 1, 1, 1), now),
        (datetime(now.year - 2, 1, 1), datetime(now.year - 1, 12, 31)),
    ]

    for league in leagues:
        for start_date, end_date in windows:
            for e in _espn_scoreboard_events(league, start_date, end_date):
                if not _team_name_matches_event(e, team):
                    continue
                key = e["id"] or (e["date"], e["home"], e["away"], e["home_score"], e["away_score"])
                if key not in seen:
                    seen.add(key)
                    events.append(e)

        # Stop scanning extra international competitions once we already have
        # a healthy sample; this also avoids unnecessary network traffic.
        if len(events) >= max(n, 12):
            break

    # Fallback to team schedule endpoints too. They are useful for clubs and
    # occasionally fill a missing international competition.
    if len(events) < max(n, 8):
        preferred = []
        if team.alt_ids and team.alt_ids.get("league"):
            preferred.append(team.alt_ids["league"])
        schedule_leagues = preferred + [x for x in leagues if x not in preferred]

        for league in schedule_leagues:
            for season in (now.year, now.year - 1, now.year - 2):
                try:
                    data = http_json(
                        f"{ESPN_BASE}/{league}/teams/{team.id}/schedule",
                        params={"season": season},
                    )
                except RuntimeError:
                    continue

                for raw in data.get("events") or []:
                    e = parse_espn_event(raw, league)
                    if not e or not _team_name_matches_event(e, team):
                        continue
                    key = e["id"] or (e["date"], e["home"], e["away"], e["home_score"], e["away_score"])
                    if key not in seen:
                        seen.add(key)
                        events.append(e)

            if len(events) >= max(n, 12):
                break

    events.sort(key=lambda x: x.get("date") or "", reverse=True)
    return events[:max(n, 30)]


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
    diagnostics = []
    best_ref = None
    best_events = []

    for ref in team_refs:
        if ref.provider == "football-data":
            events = fd_team_events(ref, n)
        elif ref.provider == "espn":
            events = espn_team_events(ref, n)
        elif ref.provider == "thesportsdb":
            events = tsdb_team_events(ref, n)
        else:
            events = fotmob_team_events(ref, n)

        diagnostics.append({
            "provider": ref.provider,
            "team": ref.name,
            "team_id": ref.id,
            "events": len(events),
        })

        if len(events) > len(best_events):
            best_ref = ref
            best_events = events

        # Prefer the first provider that can satisfy the requested sample.
        # Otherwise keep trying fallbacks and retain whichever provider yields
        # the largest usable history.
        if len(events) >= n:
            break

    return best_ref, best_events, diagnostics


# -----------------------------
# Analysis
# -----------------------------

def belongs(e, t: TeamRef):
    return (
        str(e.get("home_id") or "") == str(t.id)
        or str(e.get("away_id") or "") == str(t.id)
        or sim(e.get("home"), t.name) > .88
        or sim(e.get("away"), t.name) > .88
    )


def versus(e, a: TeamRef, b: TeamRef):
    return (
        max(sim(e.get("home"), a.name), sim(e.get("away"), a.name)) > .88
        and max(sim(e.get("home"), b.name), sim(e.get("away"), b.name)) > .88
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
            or sim(e.get("home"), team.name) > .88
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


def model_prediction(hs, aas, hv, av, h2h):
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

    ppg_edge = clamp(hs["ppg"] - aas["ppg"], -1.5, 1.5)
    hx *= 1 + ppg_edge * .04
    ax *= 1 - ppg_edge * .03

    if h2h:
        last = h2h[:5]
        total = sum(e["home_score"] + e["away_score"] for e in last) / len(last)
        cur = max(hx + ax, .1)
        blend = .88 * cur + .12 * total
        scale = clamp(blend / cur, .88, 1.12)
        hx *= scale
        ax *= scale

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
    }


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": APP_NAME,
        "version": "5.2",
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

    home_refs = resolve_team(req.home_team)
    away_refs = resolve_team(req.away_team)

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
            return ", ".join(
                f"{x['provider']}={x['events']}場" for x in rows
            ) or "無"
        raise HTTPException(
            422,
            detail=(
                f"資料不足，至少需要每隊 3 場才能產生預測。"
                f" 目前 {home.name} {len(hr)} 場、{away.name} {len(ar)} 場。"
                f" 主隊來源：{_diag_text(home_diag)}；"
                f"客隊來源：{_diag_text(away_diag)}。"
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

    pred = model_prediction(hs, aas, hv, av, h2h)

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
        },
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_home": home.provider,
            "source_away": away.provider,
            "football_data_enabled": bool(FOOTBALL_DATA_API_KEY),
            "note": (
                "v5.2 provider router: football-data.org when configured, then ESPN "
                "competition-history scan, TheSportsDB, then FotMob fallback."
            ),
        },
    }
