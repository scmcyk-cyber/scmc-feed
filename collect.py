#!/usr/bin/env python3
"""성남시의료원 소식 수집기 -> data.json

소스: 구글 뉴스 RSS / 병원 홈페이지 공지 2곳(robots.txt 허용 시) / 인스타·페북(Meta 공식 API, 토큰 있을 때만)
실행: python collect.py   (필요 패키지: requests, beautifulsoup4)
"""
import datetime as dt
import json
import os
import re
import ssl
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

OUT = Path(__file__).with_name("data.json")
UA = "scmc-feed-bot/1.0 (internal dashboard; contact: see repo)"
TODAY = dt.date.today().isoformat()
KEEP = 300  # 저장할 최대 항목 수
HEAD = {"User-Agent": UA}

NEWS_QUERY = "성남시의료원"
BASE = "https://www.scmc.kr"
# (주소, 표시 이름, 목록 형태)  table = 번호/제목/게시일자 표, cards = 사진+제목+요약 카드
NOTICE_PAGES = [
    (BASE + "/information/scmcInfoList/", "의료원 공지", "table"),
    (BASE + "/information/VariousList/", "Now,성남시의료원", "cards"),
]


def trim(s, n=90):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- 접속 (병원 서버의 오래된 TLS 설정 대응) ----------
class RelaxedTLS(HTTPAdapter):
    """OpenSSL 3 기본 보안 수준이 거부하는 구형 서버 서명 방식을 허용. 인증서 검증은 그대로 유지."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except Exception:
            pass
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


def get(url, **kw):
    """기본 설정으로 먼저 접속하고, scmc.kr 에서 TLS 오류가 날 때만 완화 설정으로 한 번 더 시도."""
    kw.setdefault("headers", HEAD)
    kw.setdefault("timeout", 20)
    try:
        return requests.get(url, **kw)
    except requests.exceptions.SSLError as e:
        host = urllib.parse.urlparse(url).hostname or ""
        if not host.endswith("scmc.kr"):
            raise
        print(f"[tls] 기본 보안 설정으로 연결 실패 -> {host} 에만 완화 설정으로 재시도: {str(e)[:120]}")
        s = requests.Session()
        s.mount("https://", RelaxedTLS())
        return s.get(url, **kw)


_robots = {}


def robots_allows(url):
    """robots.txt 를 읽어 수집 허용 여부 판단. 못 읽거나 서버 오류면 보수적으로 건너뜀."""
    p = urllib.parse.urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        ok = True
        try:
            r = get(root + "/robots.txt")
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif r.status_code in (401, 403) or r.status_code >= 500:
                print(f"[notice] robots.txt 응답 {r.status_code} -> 건너뜀")
                ok = False
            else:  # 404 등: robots.txt 없음 = 제한 없음
                rp.parse([])
        except Exception as e:
            print("[notice] robots.txt 확인 실패, 건너뜀:", e)
            ok = False
        _robots[root] = (ok, rp)
    ok, rp = _robots[root]
    if not ok:
        return False
    if not rp.can_fetch(UA, url):
        print(f"[notice] robots.txt가 {url} 자동 수집을 허용하지 않아 건너뜀 -> 병원 홈페이지 관리자에게 문의 필요")
        return False
    return True


# ---------- 소스 ----------
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


def parse_table(html, base, label):
    """의료원 공지: 표(table tbody tr) 한 줄 = td.title 안의 링크 + td.date"""
    out = []
    for tr in BeautifulSoup(html, "html.parser").select("table tbody tr"):
        a, d = tr.select_one("td.title a[href]"), tr.select_one("td.date")
        if a and d:
            out.append({"source": "notice", "title": trim(a.get_text()),
                        "url": urllib.parse.urljoin(base, a["href"]),
                        "date": d.get_text(strip=True), "press": label})
    return out


def parse_cards(html, base, label):
    """Now,성남시의료원: 카드(.column-list li) 하나 = 링크 + p.title + p.date"""
    out = []
    for li in BeautifulSoup(html, "html.parser").select(".column-list li"):
        a, t, d = li.select_one("a[href]"), li.select_one("p.title"), li.select_one("p.date")
        if a and t and d:
            out.append({"source": "notice", "title": trim(t.get_text()),
                        "url": urllib.parse.urljoin(base, a["href"]),
                        "date": d.get_text(strip=True), "press": label})
    return out


def notice():
    out = []
    for url, label, kind in NOTICE_PAGES:
        try:
            if not robots_allows(url):
                continue
            r = get(url)
            r.raise_for_status()
            items = (parse_table if kind == "table" else parse_cards)(r.text, url, label)
            if not items:
                print(f"[notice] {label}: 글을 하나도 못 읽음 -> 홈페이지 구조가 바뀌었을 수 있음")
            for it in items:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", it["date"]):
                    it["date"] = TODAY
                it["first_seen"] = it["date"]  # 처음 모으는 예전 글이 전부 NEW 로 뜨지 않게 게시일 기준
            print(f"[notice] {label}: {len(items)}건 읽음")
            out += items
        except Exception as e:  # 한 페이지가 실패해도 다른 페이지는 계속
            print(f"[notice] {label} 실패: {e}")
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
                    it.setdefault("first_seen", TODAY)
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
