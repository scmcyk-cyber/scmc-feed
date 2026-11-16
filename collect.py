#!/usr/bin/env python3
"""성남시의료원 소식 수집기 -> data.json

소스: 구글 뉴스 RSS / 공지게시판(robots.txt 허용 시) / 인스타·페북(Meta 공식 API, 토큰 있을 때만)
실행: python collect.py   (필요 패키지: requests, beautifulsoup4)
"""
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT = Path(__file__).with_name("data.json")
UA = "scmc-feed-bot/1.0 (internal dashboard; contact: see repo)"
TODAY = dt.date.today().isoformat()
KEEP = 300  # 저장할 최대 항목 수
HEAD = {"User-Agent": UA}

NEWS_QUERY = "성남시의료원"
NOTICE_URL = "https://www.scmc.kr/information/VariousList/"


def trim(s, n=90):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def news():
    q = urllib.parse.quote(NEWS_QUERY)
    url = f"https://news.google.com/rss/search?q={q}+when:14d&hl=ko&gl=KR&ceid=KR:ko"
    r = requests.get(url, headers=HEAD, timeout=20)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        pub = it.findtext("pubDate")
        d = parsedate_to_datetime(pub).date().isoformat() if pub else TODAY
        src = it.findtext("source") or ""
        title = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", it.findtext("title") or "")
        out.append({"source": "news", "title": trim(title), "url": it.findtext("link"),
                    "date": d, "press": src})
    return out


def notice():
    rp = urllib.robotparser.RobotFileParser(urllib.parse.urljoin(NOTICE_URL, "/robots.txt"))
    try:
        rp.read()
        if not rp.can_fetch(UA, NOTICE_URL):
            print("[notice] robots.txt가 자동 수집을 허용하지 않아 건너뜀 -> 병원 홈페이지 관리자에게 문의 필요")
            return []
    except Exception as e:  # robots.txt를 못 읽으면 보수적으로 건너뜀
        print("[notice] robots.txt 확인 실패, 건너뜀:", e)
        return []
    r = requests.get(NOTICE_URL, headers=HEAD, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    # 게시판 마크업을 아직 못 봤으므로 범용 파서. 결과가 이상하면 아래 선택자를 실제 구조에 맞게 수정.
    for row in soup.select("table tbody tr, ul.board-list li, .board_list li"):
        a = row.find("a", href=True)
        if not a:
            continue
        m = re.search(r"(\d{4})[-.](\d{2})[-.](\d{2})", row.get_text(" "))
        d = f"{m[1]}-{m[2]}-{m[3]}" if m else TODAY
        out.append({"source": "notice", "title": trim(a.get_text()),
                    "url": urllib.parse.urljoin(NOTICE_URL, a["href"]), "date": d})
    return out


def meta(path, fields, token):
    r = requests.get(f"https://graph.facebook.com/v21.0/{path}",
                     params={"fields": fields, "limit": 10, "access_token": token}, timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])


def instagram():
    uid, tok = os.getenv("IG_USER_ID"), os.getenv("META_TOKEN")
    if not (uid and tok):
        print("[instagram] IG_USER_ID / META_TOKEN 없음, 건너뜀")
        return []
    return [{"source": "instagram", "title": trim((p.get("caption") or "(사진/영상)").split("\n")[0]),
             "url": p["permalink"], "date": p["timestamp"][:10]}
            for p in meta(f"{uid}/media", "caption,permalink,timestamp", tok)]


def facebook():
    pid, tok = os.getenv("FB_PAGE_ID"), os.getenv("META_TOKEN")
    if not (pid and tok):
        print("[facebook] FB_PAGE_ID / META_TOKEN 없음, 건너뜀")
        return []
    return [{"source": "facebook", "title": trim((p.get("message") or "(사진/영상)").split("\n")[0]),
             "url": p["permalink_url"], "date": p["created_time"][:10]}
            for p in meta(f"{pid}/posts", "message,permalink_url,created_time", tok)]


def main():
    old = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {"items": []}
    items = {i["url"]: i for i in old["items"]}
    added = 0
    for fn in (news, notice, instagram, facebook):
        try:
            for it in fn():
                if it["url"] and it["url"] not in items:
                    it["first_seen"] = TODAY
                    items[it["url"]] = it
                    added += 1
        except Exception as e:  # 한 소스가 실패해도 나머지는 계속
            print(f"[{fn.__name__}] 실패: {e}")
    allv = sorted(items.values(), key=lambda i: (i["date"], i["first_seen"]), reverse=True)[:KEEP]
    OUT.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "items": allv}, ensure_ascii=False, indent=1), "utf-8")
    print(f"새 항목 {added}건, 총 {len(allv)}건")


if __name__ == "__main__":
    main()
