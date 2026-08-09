from __future__ import annotations

import math
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

APP_NAME = "Football Edge v5 Online"
BASE_DIR = Path(__file__).resolve().parent
FOTMOB = "https://www.fotmob.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

app = FastAPI(title=APP_NAME, version="5.0")

CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 600


class AnalyzeRequest(BaseModel):
    home_team: str
    away_team: str
    recent_matches: int = 10


@dataclass
class TeamRef:
    id: int
    name: str


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


def get_json(path: str, params: dict | None = None) -> Any:
    params = params or {}
    key = path + repr(sorted(params.items()))
    cached = cache_get(key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            FOTMOB + path,
            params=params,
            timeout=18,
            headers={
                "User-Agent": UA,
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.fotmob.com/",
            },
        )
        if r.status_code == 429:
            raise HTTPException(429, detail="FotMob 暫時限制請求，請稍後再試。")
        if r.status_code in (401, 403):
            raise HTTPException(502, detail="FotMob 暫時拒絕伺服器存取，請稍後再試。")
        if r.status_code == 404:
            raise HTTPException(404, detail="FotMob 找不到該資料端點。")
        r.raise_for_status()

        ct = (r.headers.get("content-type") or "").lower()
        if "json" not in ct:
            raise HTTPException(
                502,
                detail="FotMob 回傳的不是 JSON，可能是暫時封鎖或端點改版。",
            )
        data = r.json()
        cache_set(key, data)
        return data
    except HTTPException:
        raise
    except requests.RequestException as e:
        raise HTTPException(502, detail=f"連線 FotMob 失敗：{type(e).__name__}")


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


KNOWN_NATIONAL_TEAMS = {
    "denmark": TeamRef(8238, "Denmark"),
    "norway": TeamRef(8492, "Norway"),
}


def candidates_from_search(data: Any, query: str) -> list[TeamRef]:
    out: list[tuple[float, TeamRef]] = []
    for d in recursive_dicts(data):
        name = d.get("name") or d.get("localizedName") or d.get("title") or d.get("teamName")
        raw_id = d.get("id") or d.get("teamId")
        if not name or raw_id is None:
            continue
        try:
            tid = int(raw_id)
        except (TypeError, ValueError):
            continue

        typ = str(d.get("type") or d.get("entityType") or "").lower()
        score = sim(query, str(name))
        if "team" in typ:
            score += .12
        if score >= .46:
            out.append((score, TeamRef(tid, str(name))))

    out.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    refs = []
    for _, ref in out:
        if ref.id not in seen:
            seen.add(ref.id)
            refs.append(ref)
    return refs


def search_team(query: str) -> TeamRef:
    q = query.strip()
    if len(q) < 2:
        raise HTTPException(400, detail="球隊名稱至少輸入 2 個字。")

    # Explicit fallback for common national-team cases we verified.
    known = KNOWN_NATIONAL_TEAMS.get(norm(q))
    if known:
        return known

    errors = []
    for path in ("/search/suggest", "/searchData"):
        try:
            data = get_json(path, {"term": q})
            cands = candidates_from_search(data, q)
            if cands:
                return cands[0]
        except HTTPException as e:
            errors.append(str(e.detail))

    raise HTTPException(
        404,
        detail=f"找不到「{q}」。建議使用正式英文隊名。"
        + (f" 搜尋診斷：{' / '.join(errors)}" if errors else ""),
    )


def side_name(side: Any) -> str | None:
    if isinstance(side, str):
        return side
    if isinstance(side, dict):
        return side.get("name") or side.get("teamName") or side.get("shortName")
    return None


def side_id(side: Any) -> int | None:
    if not isinstance(side, dict):
        return None
    for key in ("id", "teamId"):
        if key in side:
            try:
                return int(side[key])
            except (TypeError, ValueError):
                return None
    return None


def score_num(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        return int(m.group()) if m else None
    if isinstance(v, dict):
        for k in ("current", "display", "normaltime", "score", "total"):
            if k in v:
                x = score_num(v[k])
                if x is not None:
                    return x
    return None


def parse_date(d: dict) -> str | None:
    for key in ("utcTime", "startDate", "date", "dateEvent"):
        if d.get(key):
            return str(d[key])
    status = d.get("status")
    if isinstance(status, dict):
        for key in ("utcTime", "startDate", "date"):
            if status.get(key):
                return str(status[key])
    return None


def event_from(d: dict) -> dict | None:
    home = d.get("home") or d.get("homeTeam")
    away = d.get("away") or d.get("awayTeam")
    hn = side_name(home) or d.get("homeName") or d.get("strHomeTeam")
    an = side_name(away) or d.get("awayName") or d.get("strAwayTeam")
    if not hn or not an:
        return None

    hs = score_num(d.get("homeScore") or d.get("scoreHome") or d.get("homeGoals") or d.get("intHomeScore"))
    aas = score_num(d.get("awayScore") or d.get("scoreAway") or d.get("awayGoals") or d.get("intAwayScore"))

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

    return {
        "id": d.get("id") or d.get("matchId") or d.get("eventId"),
        "date": parse_date(d),
        "home": str(hn),
        "away": str(an),
        "home_id": side_id(home),
        "away_id": side_id(away),
        "home_score": hs,
        "away_score": aas,
        "competition": str(comp) if comp else None,
    }


def collect_events(payload: Any) -> list[dict]:
    seen = set()
    out = []
    for d in recursive_dicts(payload):
        e = event_from(d)
        if not e:
            continue
        key = e.get("id") or (
            e["date"], e["home"], e["away"], e["home_score"], e["away_score"]
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda e: e.get("date") or "", reverse=True)
    return out


def belongs(e: dict, t: TeamRef) -> bool:
    return (
        e.get("home_id") == t.id
        or e.get("away_id") == t.id
        or sim(e.get("home"), t.name) > .87
        or sim(e.get("away"), t.name) > .87
    )


def versus(e: dict, a: TeamRef, b: TeamRef) -> bool:
    ids = {e.get("home_id"), e.get("away_id")}
    if a.id in ids and b.id in ids:
        return True
    return (
        max(sim(e.get("home"), a.name), sim(e.get("away"), a.name)) > .87
        and max(sim(e.get("home"), b.name), sim(e.get("away"), b.name)) > .87
    )


def recent(events: list[dict], t: TeamRef, n: int) -> list[dict]:
    return [e for e in events if belongs(e, t)][:n]


def stats(events: list[dict], t: TeamRef, venue: str | None = None) -> dict:
    gf = ga = w = d = l = 0
    form = []
    used = []
    for e in events:
        is_home = e.get("home_id") == t.id or sim(e["home"], t.name) > .87
        if venue == "home" and not is_home:
            continue
        if venue == "away" and is_home:
            continue
        a, b = (
            (e["home_score"], e["away_score"])
            if is_home
            else (e["away_score"], e["home_score"])
        )
        gf += a
        ga += b
        used.append(e)
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


def poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def dc_tau(h: int, a: int, hx: float, ax: float, rho: float = -0.08) -> float:
    if h == 0 and a == 0:
        return max(.05, 1 - hx * ax * rho)
    if h == 0 and a == 1:
        return max(.05, 1 + hx * rho)
    if h == 1 and a == 0:
        return max(.05, 1 + ax * rho)
    if h == 1 and a == 1:
        return max(.05, 1 - rho)
    return 1.0


def prediction(hs, aas, hv, av, h2h):
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

    edge = clamp(hs["ppg"] - aas["ppg"], -1.5, 1.5)
    hx *= 1 + edge * .04
    ax *= 1 - edge * .03

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
    conf = int(clamp(48 + sample * 1.3 + venue_sample * 1.2 + min(len(h2h), 5) * 2.0, 45, 90))

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
    }


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": APP_NAME,
        "version": "5.0",
        "provider": "FotMob web JSON adapter",
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    if norm(req.home_team) == norm(req.away_team):
        raise HTTPException(400, detail="請輸入兩支不同的球隊。")

    n = max(5, min(req.recent_matches, 20))

    home = search_team(req.home_team)
    away = search_team(req.away_team)

    if home.id == away.id:
        raise HTTPException(400, detail="兩個輸入被辨識成同一支球隊。")

    home_payload = get_json("/teams", {"id": home.id})
    away_payload = get_json("/teams", {"id": away.id})

    home_all = collect_events(home_payload)
    away_all = collect_events(away_payload)

    hr = recent(home_all, home, n)
    ar = recent(away_all, away, n)

    if len(hr) < 3 or len(ar) < 3:
        raise HTTPException(
            422,
            detail=f"可取得近期賽事不足：{home.name} {len(hr)} 場、{away.name} {len(ar)} 場。",
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

    hs = stats(hr, home)
    aas = stats(ar, away)
    hv = stats(hr, home, "home")
    av = stats(ar, away, "away")

    pred = prediction(hs, aas, hv, av, h2h)

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
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "FotMob public web JSON adapter",
            "note": "Non-official endpoint; server-side access can still be rate-limited or changed by FotMob.",
        },
    }
