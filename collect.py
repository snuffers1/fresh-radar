"""Сборщик радара «Свежего».
Раз в день: YouTube-аутлаеры по каналам, топ Reddit по сабреддитам, свежие эссе из RSS.
Результат: data/latest.json и data/YYYY-MM-DD.json.
"""
import datetime as dt
import json
import os
import re
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

KEY = os.environ.get("YOUTUBE_API_KEY", "")
NOW = dt.datetime.now(dt.timezone.utc)
UA = "fresh-radar/0.1 (personal trend research)"

OUTLIER_X = 5          # порог: во сколько раз выше медианы канала
WINDOW_DAYS = 14       # окно свежих роликов
BASELINE_N = 30        # сколько роликов берём для медианы
MIN_AGE_DAYS = 7       # в медиану только ролики старше недели
SHORT_SEC = 180        # короче — считаем шортсом
SUBS_RATIO_X = 1.5     # просмотры ≥ 1,5× подписчиков канала — приоритетный сигнал
MIN_VIEWS = 5000       # нижний порог, чтобы крошечные каналы не шумели


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def yt(endpoint, **params):
    params["key"] = KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    return json.loads(get(url))


def iso_dur(s):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def read_list(path):
    if not os.path.exists(path):
        return []
    txt = open(path, encoding="utf-8").read()
    return [x for x in re.split(r"\s+", txt) if x and not x.startswith("#")]


def youtube():
    if not KEY:
        return {"error": "нет YOUTUBE_API_KEY"}
    handles = read_list("channels.txt")
    out, errors = [], []
    for h in handles:
        try:
            ch = yt("channels", part="snippet,statistics,contentDetails", forHandle=h)
            if not ch.get("items"):
                errors.append(f"{h}: не найден")
                continue
            c = ch["items"][0]
            uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
            pl = yt("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50)
            ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
            if not ids:
                continue
            vs = yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids))
            vids = []
            for v in vs.get("items", []):
                pub = dt.datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
                dur = iso_dur(v["contentDetails"].get("duration"))
                vids.append({
                    "id": v["id"],
                    "title": v["snippet"]["title"],
                    "published": v["snippet"]["publishedAt"],
                    "age_days": round((NOW - pub).total_seconds() / 86400, 1),
                    "duration_min": round(dur / 60, 1),
                    "short": dur <= SHORT_SEC,
                    "views": int(v["statistics"].get("viewCount", 0)),
                    "comments": int(v["statistics"].get("commentCount", 0)),
                })
            long_ = [v for v in vids if not v["short"]]
            base = [v["views"] for v in long_ if v["age_days"] >= MIN_AGE_DAYS][:BASELINE_N]
            median = statistics.median(base) if len(base) >= 5 else None
            fresh = [v for v in long_ if v["age_days"] <= WINDOW_DAYS]
            for v in fresh:
                v["multiple"] = round(v["views"] / median, 2) if median else None
                v["views_per_day"] = round(v["views"] / max(v["age_days"], 0.5))
                v["url"] = f"https://www.youtube.com/watch?v={v['id']}"
            out.append({
                "handle": h,
                "title": c["snippet"]["title"],
                "subs": int(c["statistics"].get("subscriberCount", 0) or 0),
                "median_views": median,
                "fresh": fresh,
            })
        except Exception as e:  # noqa: BLE001
            errors.append(f"{h}: {e}")
        time.sleep(0.1)
    outliers = []
    for c in out:
        for v in c["fresh"]:
            subs = c["subs"] or 0
            ratio = round(v["views"] / subs, 2) if subs else None
            v["subs_ratio"] = ratio  # просмотры / подписчики канала
            big_multiple = v.get("multiple") and v["multiple"] >= OUTLIER_X
            beats_subs = ratio is not None and ratio >= SUBS_RATIO_X and v["views"] >= MIN_VIEWS
            if big_multiple or beats_subs:
                outliers.append({**v, "channel": c["title"], "handle": c["handle"],
                                 "subs": subs, "median_views": c["median_views"],
                                 "priority": bool(beats_subs)})
    # сначала ролики, обогнавшие подписчиков канала (чем сильнее, тем выше), потом по кратности медианы
    outliers.sort(key=lambda v: (v["priority"], v["subs_ratio"] or 0, v.get("multiple") or 0), reverse=True)
    return {"outliers": outliers, "channels": out, "errors": errors}


def reddit():
    subs = read_list("subreddits.txt")
    posts, errors = [], []
    for s in subs:
        try:
            raw = get(f"https://www.reddit.com/r/{s}/top.json?t=day&limit=10")
            for ch in json.loads(raw)["data"]["children"]:
                d = ch["data"]
                posts.append({
                    "sub": s, "title": d["title"], "score": d["score"],
                    "comments": d["num_comments"],
                    "url": "https://www.reddit.com" + d["permalink"],
                    "text": (d.get("selftext") or "")[:500],
                })
        except Exception as e:  # noqa: BLE001
            # запасной путь — RSS (без цифр)
            try:
                root = ET.fromstring(get(f"https://www.reddit.com/r/{s}/top/.rss?t=day"))
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for en in root.findall("a:entry", ns)[:10]:
                    posts.append({
                        "sub": s, "title": en.findtext("a:title", "", ns),
                        "score": None, "comments": None,
                        "url": en.find("a:link", ns).get("href"),
                    })
                errors.append(f"{s}: json не дался ({e}), взял RSS")
            except Exception as e2:  # noqa: BLE001
                errors.append(f"{s}: {e} / rss: {e2}")
        time.sleep(1.5)
    posts.sort(key=lambda p: (p["comments"] or 0), reverse=True)
    return {"top": posts[:50], "all_count": len(posts), "errors": errors}


def essays():
    feeds = read_list("feeds.txt")
    items, errors = [], []
    cutoff = NOW - dt.timedelta(days=3)
    for f in feeds:
        try:
            root = ET.fromstring(get(f))
            entries = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
            for e in entries[:15]:
                t = e.findtext("title") or e.findtext("{http://www.w3.org/2005/Atom}title") or ""
                link = e.findtext("link") or ""
                if not link:
                    le = e.find("{http://www.w3.org/2005/Atom}link")
                    link = le.get("href") if le is not None else ""
                date = (e.findtext("pubDate") or e.findtext("{http://www.w3.org/2005/Atom}published")
                        or e.findtext("{http://www.w3.org/2005/Atom}updated") or "")
                try:
                    from email.utils import parsedate_to_datetime
                    d = parsedate_to_datetime(date) if "," in date else dt.datetime.fromisoformat(date.replace("Z", "+00:00"))
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=dt.timezone.utc)
                except Exception:  # noqa: BLE001
                    d = None
                if d and d < cutoff:
                    continue
                desc = re.sub(r"<[^>]+>", " ", e.findtext("description") or "")[:400]
                items.append({"feed": f, "title": t.strip(), "url": link.strip(),
                              "date": d.isoformat() if d else date, "summary": desc.strip()})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{f}: {e}")
    return {"items": items, "errors": errors}


def save_thumb(url, name, max_bytes=3_000_000):
    """Скачивает картинку в data/thumbs/<name>.<ext>, возвращает путь или None."""
    try:
        raw = get(url, timeout=20)
        if len(raw) > max_bytes or len(raw) < 2000:
            return None
        ext = "png" if raw[:4] == b"\x89PNG" else "webp" if raw[8:12] == b"WEBP" else "jpg"
        os.makedirs("data/thumbs", exist_ok=True)
        path = f"data/thumbs/{name}.{ext}"
        with open(path, "wb") as fh:
            fh.write(raw)
        return path
    except Exception:  # noqa: BLE001
        return None


def og_image(page_url):
    try:
        html = get(page_url, timeout=20)[:300_000].decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html)
         or re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', html))
    return m.group(1).replace("&amp;", "&") if m else None


def thumbs(data):
    for o in data["youtube"].get("outliers", []):
        vid = o["id"]
        o["thumb"] = (save_thumb(f"https://i.ytimg.com/vi/{vid}/hq720.jpg", f"yt-{vid}")
                      or save_thumb(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", f"yt-{vid}"))
    for i, e in enumerate(data["essays"]["items"]):
        img = og_image(e["url"]) if e.get("url") else None
        if img:
            slug = re.sub(r"[^a-z0-9]+", "-", e["url"].lower())[-60:].strip("-")
            e["thumb"] = save_thumb(img, f"essay-{slug}")


def main():
    data = {"generated_at": NOW.isoformat(), "settings": {
        "outlier_x": OUTLIER_X, "window_days": WINDOW_DAYS},
        "youtube": youtube(), "reddit": reddit(), "essays": essays()}
    thumbs(data)
    os.makedirs("data", exist_ok=True)
    for p in ("data/latest.json", f"data/{NOW.date()}.json"):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    y = data["youtube"]
    print("outliers:", len(y.get("outliers", [])), "| yt errors:", len(y.get("errors", [])),
          "| reddit:", data["reddit"]["all_count"], "| essays:", len(data["essays"]["items"]))


if __name__ == "__main__":
    main()
