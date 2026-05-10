"""
KBO 공식 사이트 윤리적 크롤러 (개인 사용 전용)
==================================================

회색지대 회피 4원칙
1. robots.txt 준수: /Common, /Help, /Member, /ws 절대 금지
2. Rate limiting: 요청 간 최소 3초 (사람 클릭 속도)
3. 캐싱: 같은 데이터 반복 요청 X
4. User-Agent에 정체 명시 (관행, 사이트 운영자가 연락 가능)

⚠ 주의사항
- 이 코드는 개인 학습용. 수집한 데이터는 재배포하지 않음 (레포의 data.json은 가상 샘플).
- 회사·상업·공개 서비스로 쓰려면 KBO에 별도 데이터 사용 문의 필요.
- robots.txt와 약관은 변경될 수 있음. 운영 전 다시 확인할 것.
- ASP.NET 페이지는 ViewState 처리가 필요할 수 있음. requests로 안 되면
  단계 2의 Playwright 사용으로 넘어갈 것.

설치
    pip install requests beautifulsoup4

확장 시 (동적 페이지)
    pip install playwright
    playwright install chromium
"""

import time
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup


# ==========================================================
# 설정 — 반드시 본인 정보로 수정
# ==========================================================

USER_AGENT = (
    "KBOHitProbability/0.1 "
    "(personal-use; contact: your.email@example.com)"
)
RATE_LIMIT_SECONDS = 3.0   # 요청 간 최소 간격
DEFAULT_TTL = 86400        # 24시간 캐시
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger(__name__)


# ==========================================================
# Rate Limiter
# ==========================================================

class RateLimiter:
    """요청 간 최소 간격을 강제. 사람 클릭 속도(3초+)를 흉내."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.last_request = 0.0

    def wait(self) -> None:
        elapsed = time.time() - self.last_request
        if elapsed < self.min_interval:
            sleep_for = self.min_interval - elapsed
            log.debug(f"Rate limit: sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)
        self.last_request = time.time()


# ==========================================================
# 파일 캐시 (TTL 기반)
# ==========================================================

class FileCache:
    """JSON 파일로 응답을 영속화. SQLite로 바꾸면 더 견고함."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir

    def _path(self, key: str) -> Path:
        h = hashlib.md5(key.encode()).hexdigest()
        return self.dir / f"{h}.json"

    def get(self, key: str, ttl_seconds: int) -> Optional[str]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            cached_at = datetime.fromisoformat(data['cached_at'])
            if datetime.now() - cached_at > timedelta(seconds=ttl_seconds):
                log.debug(f"Cache expired: {key}")
                return None
            log.info(f"Cache HIT: {key}")
            return data['value']
        except (json.JSONDecodeError, KeyError):
            return None

    def set(self, key: str, value: str) -> None:
        p = self._path(key)
        p.write_text(
            json.dumps(
                {'cached_at': datetime.now().isoformat(), 'value': value},
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )


# ==========================================================
# KBO 크롤러
# ==========================================================

class KBOCrawler:
    """
    KBO 공식 사이트(www.koreabaseball.com)만 대상.
    robots.txt(2026.05 기준) 금지 경로를 자동 거부함.
    """

    BASE = "https://www.koreabaseball.com"
    FORBIDDEN_PATHS = ['/Common/', '/Help/', '/Member/', '/ws/']

    # KBO 10개 구단 팀 코드 (Player/Register.aspx 쿼리스트링용)
    TEAM_CODES = {
        'SSG': 'SK', '삼성': 'SS', 'LG': 'LG', '두산': 'OB', 'KIA': 'HT',
        '롯데': 'LT', '한화': 'HH', 'NC': 'NC', 'KT': 'KT', '키움': 'WO',
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
            'Accept': 'text/html,application/xhtml+xml',
        })
        self.limiter = RateLimiter(RATE_LIMIT_SECONDS)
        self.cache = FileCache(CACHE_DIR)

    def _ensure_allowed(self, url: str) -> None:
        """robots.txt 금지 경로면 예외 발생 — 실수 방지용 가드."""
        for forbidden in self.FORBIDDEN_PATHS:
            if forbidden in url:
                raise PermissionError(
                    f"robots.txt가 금지한 경로 접근 시도: {forbidden}\n"
                    f"URL: {url}"
                )

    def fetch(self, url: str, ttl_seconds: int = DEFAULT_TTL) -> str:
        """
        URL을 가져옴. 캐시 우선, 없으면 rate limit 후 요청 → 캐시 저장.
        """
        self._ensure_allowed(url)

        cached = self.cache.get(url, ttl_seconds)
        if cached is not None:
            return cached

        self.limiter.wait()
        log.info(f"FETCH: {url}")
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding  # 한글 인코딩 보정
        html = resp.text
        self.cache.set(url, html)
        return html

    # ------------------------------------------------------
    # 도메인 메서드 — 페이지별 wrapper
    # ------------------------------------------------------

    def get_hitter_basic(self, season: int = 2026) -> list[dict]:
        """타자 기본 기록. 시즌 데이터라 24시간 캐싱."""
        url = f"{self.BASE}/Record/Player/HitterBasic/Basic1.aspx?seasonId={season}"
        html = self.fetch(url, ttl_seconds=86400)
        return self._parse_hitter_table(html)

    def get_today_schedule(self) -> list[dict]:
        """오늘 경기 일정. 라인업 발표 전이라 짧게 캐싱(1시간)."""
        url = f"{self.BASE}/Schedule/Schedule.aspx"
        html = self.fetch(url, ttl_seconds=3600)
        return self._parse_schedule(html)

    def get_active_hitter_set(self, season: int = 2026) -> dict[str, dict]:
        """
        시즌타율 페이지에 잡히는 타자 = 활성 선수.
        방출/은퇴/이적 선수는 시즌 누적이 안 쌓여 이 페이지에 안 나옴.
        반환: {선수명: {team, avg, ab, ...}}
        ※ KBO Register.aspx가 ASP.NET POST를 안 받아 팀 로스터 직접 검증이
          막혀서 이 풀을 fallback으로 사용. 시즌 30위까지 자동 커버.
        """
        hitters = self.get_hitter_basic(season=season)
        return {h['name']: h for h in hitters if h.get('name')}

    # ============================================================
    # ⚠ 아래 팀-로스터 흐름은 KBO ASP.NET이 GET TeamCode 무시 +
    #   POST __doPostBack 패턴 미사용으로 현재 미동작.
    #   향후 Playwright 도입 시 _parse_roster를 재사용 예정. 보존만.
    # ============================================================
    def get_team_roster(self, team_name: str, season: int = 2026) -> list[dict]:
        """팀 등록 선수 명단. ASP.NET POST 흐름 사용 (현재 미동작)."""
        code = self.TEAM_CODES.get(team_name)
        if not code:
            log.warning(f"알 수 없는 팀: {team_name}")
            return []
        # 단일 팀 요청 시: bulk 수집 후 해당 팀 HTML만 사용
        rosters = self._fetch_all_rosters_html(season)
        html = rosters.get(team_name, '')
        return self._parse_roster(html, team_name) if html else []

    def get_active_roster_set(self, season: int = 2026) -> set[str]:
        """전체 10개 구단 등록 선수 이름 집합 — 방출 검증용."""
        active: set[str] = set()
        rosters = self._fetch_all_rosters_html(season)
        for team_name, html in rosters.items():
            for p in self._parse_roster(html, team_name):
                if p.get('status') == 'active':
                    active.add(p['name'])
        return active

    def _fetch_all_rosters_html(self, season: int = 2026) -> dict[str, str]:
        """
        전체 10개 구단 등록 페이지 HTML을 ASP.NET POST 흐름으로 수집.
        - 1) 시드 GET → ViewState/EventValidation 획득
        - 2) 각 팀에 대해 hfSearchTeam을 그 팀 코드로 바꿔 POST
        - 3) 응답 HTML에서 갱신된 ViewState로 다음 POST에 사용
        결과는 (팀명 → HTML) 딕셔너리. 12시간 캐시.
        """
        # 캐시 체크 (팀별 HTML을 한 번에 묶어서 캐시)
        cache_key = f"rosters:bulk:{season}"
        cached = self.cache.get(cache_key, ttl_seconds=43200)
        if cached is not None:
            return json.loads(cached)

        seed_url = f"{self.BASE}/Player/Register.aspx?seasonId={season}"
        self._ensure_allowed(seed_url)
        self.limiter.wait()
        log.info(f"GET (seed): {seed_url}")
        resp = self.session.get(seed_url, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        current_html = resp.text

        rosters: dict[str, str] = {}
        for team_name, code in self.TEAM_CODES.items():
            form_data = self._extract_hidden_fields(current_html)
            team_field = next(
                (k for k in form_data if k.endswith('hfSearchTeam')), None
            )
            if not team_field:
                log.warning('hfSearchTeam 필드를 못 찾음 → 흐름 중단')
                break

            # 시드 응답이 이미 이 팀이면 그대로 사용 (POST 절약)
            if form_data.get(team_field) == code:
                rosters[team_name] = current_html
                continue

            form_data[team_field] = code
            form_data.setdefault('__EVENTTARGET', '')
            form_data.setdefault('__EVENTARGUMENT', '')

            post_url = f"{self.BASE}/Player/Register.aspx?TeamCode={code}&seasonId={season}"
            self.limiter.wait()
            log.info(f"POST roster: team={code}")
            try:
                resp = self.session.post(post_url, data=form_data, timeout=15)
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding
                current_html = resp.text
                rosters[team_name] = current_html
            except requests.RequestException as e:
                log.warning(f"  팀 {team_name} POST 실패: {e}")

        # 캐시 저장
        self.cache.set(cache_key, json.dumps(rosters, ensure_ascii=False))
        return rosters

    @staticmethod
    def _extract_hidden_fields(html: str) -> dict[str, str]:
        """form 안 모든 hidden input을 (name → value)로 수집."""
        soup = BeautifulSoup(html, 'html.parser')
        form = soup.find('form') or soup
        data = {}
        for inp in form.find_all('input', type='hidden'):
            name = inp.get('name', '')
            if name:
                data[name] = inp.get('value', '')
        return data

    # ------------------------------------------------------
    # 파서 — 실제 셀렉터는 사이트 구조 확인 후 조정 필요
    # ------------------------------------------------------

    def _parse_hitter_table(self, html: str) -> list[dict]:
        """
        시즌타율 페이지 파서.
        실제 KBO 마크업: <table class="tData01 tt">
        컬럼: 순위 | 선수명 | 팀 | AVG | G | PA | AB | R | H | 2B | 3B | HR | RBI | ...
        """
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.select('table.tData01 tbody tr')

        results = []
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.select('td')]
            if len(cells) < 9:
                continue
            try:
                results.append({
                    'rank': int(cells[0]) if cells[0].isdigit() else None,
                    'name': cells[1],
                    'team': cells[2],
                    'avg': float(cells[3]) if cells[3] else 0.0,
                    'g': int(cells[4]) if cells[4].isdigit() else 0,
                    'pa': int(cells[5]) if cells[5].isdigit() else 0,
                    'ab': int(cells[6]) if cells[6].isdigit() else 0,
                    'r': int(cells[7]) if cells[7].isdigit() else 0,
                    'h': int(cells[8]) if cells[8].isdigit() else 0,
                })
            except (ValueError, IndexError) as e:
                log.warning(f"Parse error on row: {cells} ({e})")
        return results

    def _parse_schedule(self, html: str) -> list[dict]:
        """오늘 일정 파서. 셀렉터는 직접 확인 필요."""
        soup = BeautifulSoup(html, 'html.parser')
        # 실제 사이트 구조 확인 후 작성
        return []

    def _parse_roster(self, html: str, team_name: str) -> list[dict]:
        """
        팀 등록 선수 페이지 파서.
        실제 KBO 마크업: <table class="tNData"> 가 카테고리별로 여러 개
        (감독/코치/투수/포수/내야/외야/신고선수/군보류).
        Register.aspx는 "1군 엔트리"가 아닌 "팀 등록 전체 명단"이므로
        말소/부상 여부는 알 수 없음 → 방출 여부만 판정.

        반환: [{name, team, status: 'active'}, ...]
        manual_lineups의 선수가 이 결과에 없으면 'released'로 간주됨.
        """
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        for table in soup.select('table.tNData'):
            for row in table.select('tbody tr'):
                cells = [c.get_text(strip=True) for c in row.select('td')]
                if len(cells) < 2:
                    continue
                # 첫 컬럼이 등번호(숫자)인 행만 선수로 인정 → 헤더/감독/코치 자연스레 걸러짐
                if not cells[0].isdigit():
                    continue
                name = cells[1]
                if not name or len(name) > 5:  # 한글 이름은 보통 2-4자
                    continue
                results.append({'name': name, 'team': team_name, 'status': 'active'})
        return results


# ==========================================================
# 사용 예시
# ==========================================================

def main():
    crawler = KBOCrawler()

    # 1) 시즌 타율 (24시간 캐싱 — 두 번째 실행은 즉시 반환)
    hitters = crawler.get_hitter_basic(season=2026)
    log.info(f"가져온 타자 수: {len(hitters)}")
    for h in hitters[:5]:
        print(f"  {h.get('rank'):>3} {h.get('name'):>5} ({h.get('team'):>3}) "
              f"AVG {h.get('avg'):.3f}")

    # 2) 금지 경로 시도 — 자동 차단
    try:
        crawler.fetch(f"{KBOCrawler.BASE}/ws/some-api")
    except PermissionError as e:
        print(f"\n[가드 동작 확인] {e}")


if __name__ == '__main__':
    main()
