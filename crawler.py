"""
crawler.py — KBO 시즌 타율 자동 수집 + 수동 라인업 결합
========================================================

전략 (실무적 절충안):
- 시즌 타율 = 자동 크롤링 (KBO 공식 사이트, 24시간 캐시)
- 라인업/상대 투수 = manual_lineups.json (매일 5분 손으로)

왜 라인업은 수동인가?
1. 라인업은 경기 1~2시간 전에 발표 → 자동화 타이밍 까다로움
2. KBO 사이트는 ASP.NET 동적 렌더링 → Playwright 셋업 부담
3. 개인 학습용 규모에선 5분 수동 입력이 더 안정적이고 빠름

자동화는 잘 동작 확인된 후 점진적으로.

실행:
    pip install requests beautifulsoup4
    python crawler.py
"""
import sys
import json
from pathlib import Path
from datetime import datetime

from kbo_crawler import KBOCrawler  # 이전에 만든 모듈

BASE_DIR = Path(__file__).parent
LINEUP_FILE = BASE_DIR / "manual_lineups.json"
OUTPUT_FILE = BASE_DIR / "data.json"


# 가상 선수 샘플 — 실제 선수 데이터는 커밋하지 않음
SAMPLE_LINEUPS = [
    {"player": "가상타자A", "team": "SSG", "batHand": "L", "lineupPos": 1, "isStarting": True,
     "game": "SSG vs 롯데 (인천)", "isHome": True, "oppPitcher": "가상투수1", "oppHand": "L",
     "vsLefty": 0.875, "vsRighty": 0.380,
     "homeAvg": 0.430, "awayAvg": 0.310,
     "last10Avg": 0.269, "note": "좌투 스플릿 표본이 작은 케이스 (축소 효과 예시)"},
    {"player": "가상타자B", "team": "삼성", "batHand": "L", "lineupPos": 2, "isStarting": True,
     "game": "삼성 vs NC (대구)", "isHome": True, "oppPitcher": "가상투수2", "oppHand": "R",
     "vsLefty": 0.310, "vsRighty": 0.395,
     "homeAvg": 0.350, "awayAvg": 0.330,
     "last10Avg": 0.350, "note": "스플릿 편차가 작은 기본 케이스"},
    {"player": "가상타자C", "team": "두산", "batHand": "R", "lineupPos": 3, "isStarting": True,
     "game": "두산 vs KIA (잠실)", "isHome": True, "oppPitcher": "가상투수3", "oppHand": "R",
     "vsLefty": 0.350, "vsRighty": 0.380,
     "homeAvg": 0.350, "awayAvg": 0.290,
     "last10Avg": 0.330, "note": "홈/원정 차이가 큰 케이스"},
    {"player": "가상타자D", "team": "한화", "batHand": "R", "lineupPos": 4, "isStarting": True,
     "game": "한화 vs LG (대전)", "isHome": True, "oppPitcher": "가상투수4", "oppHand": "L",
     "vsLefty": 0.350, "vsRighty": 0.390,
     "homeAvg": 0.300, "awayAvg": 0.280,
     "last10Avg": 0.310, "note": "좌투 상대 시 변동성 있음"},
    {"player": "가상타자E", "team": "KIA", "batHand": "R", "lineupPos": 2, "isStarting": True,
     "game": "두산 vs KIA (잠실)", "isHome": False, "oppPitcher": "가상투수5", "oppHand": "R",
     "vsLefty": 0.270, "vsRighty": 0.255,
     "homeAvg": 0.260, "awayAvg": 0.250,
     "stadiumAvg": {
         "잠실": {"avg": 0.420, "ab": 30},
         "수원": {"avg": 0.180, "ab": 18},
         "사직": {"avg": 0.250, "ab": 22}
     },
     "last10Avg": 0.310, "note": "구장별 기록이 있는 케이스 (잠실 강함, 수원 약함)"},
    {"player": "가상타자F", "team": "KT", "batHand": "R", "lineupPos": 4, "isStarting": False,
     "game": "KT vs 키움 (수원)", "isHome": True, "oppPitcher": "가상투수6", "oppHand": "R",
     "vsLefty": 0.350, "vsRighty": 0.380,
     "homeAvg": 0.350, "awayAvg": 0.300,
     "last10Avg": 0.0, "note": "부상 이탈"},
]


def ensure_lineup_file():
    """첫 실행 시 라인업 샘플 파일 생성."""
    if not LINEUP_FILE.exists():
        LINEUP_FILE.write_text(
            json.dumps(SAMPLE_LINEUPS, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        print(f"  ✓ {LINEUP_FILE.name} 샘플 생성")
        print(f"    → 매일 이 파일을 오늘 라인업으로 수정하세요")
        print(f"    → KBO 게임센터에서 라인업 발표 후 5분 입력")


def fetch_season_stats() -> dict:
    """KBO 시즌 타율 자동 수집. 실패 시 빈 dict 반환."""
    try:
        crawler = KBOCrawler()
        hitters = crawler.get_hitter_basic(season=2026)
        if not hitters:
            print("  ⚠ 데이터가 비었음. KBO 사이트 구조 변경 또는")
            print("    ASP.NET 동적 렌더링 이슈일 가능성")
            print("    → kbo_crawler.py의 _parse_hitter_table 셀렉터 확인 필요")
            print("    → 혹은 Playwright로 전환 (README 참고)")
            return {}
        return {h['name']: h for h in hitters}
    except Exception as e:
        print(f"  ⚠ 시즌 타율 수집 실패: {e}")
        print("    → 라인업 파일에 적힌 값을 그대로 사용합니다")
        return {}


def fetch_active_hitter_pool() -> set[str]:
    """
    시즌타율 페이지에 잡히는 타자 = 활성 선수.
    KBO Register.aspx 직접 검증이 ASP.NET 한계로 불가하여
    이 페이지를 fallback 풀로 사용. 시즌 30위 안의 선수는 모두 잡힘.
    """
    try:
        crawler = KBOCrawler()
        hitters = crawler.get_active_hitter_set(season=2026)
        if not hitters:
            print("  ⚠ 시즌타율 풀 비었음 → 검증 스킵")
        return set(hitters.keys())
    except Exception as e:
        print(f"  ⚠ 시즌타율 풀 수집 실패: {e}")
        return set()


def validate_roster(lineups: list, active_pool: set[str]) -> list:
    """
    manual_lineups 선수가 시즌타율 풀에 있는지 확인.
    rosterStatus 값:
      'active'      — 시즌타율에 잡힘 (방출 아닌 게 확실)
      'unverified'  — 시즌타율에 안 잡힘 (시즌 30위 밖이거나, 방출/신예/이적 가능)
                       계산 대상엔 포함하되 UI에 ⚠ 표시
      'released'    — manual_lineups에 직접 마킹된 경우만 (escape hatch)
      'demoted'     — 〃
      'injured'     — 〃
      'unknown'     — 풀 자체 수집 실패 (네트워크 등)

    manual로 미리 'released'/'demoted'/'injured' 마킹된 값은 보존.
    """
    if not active_pool:
        for p in lineups:
            p.setdefault('rosterStatus', 'unknown')
        return lineups

    unverified = []
    inactive = []
    for p in lineups:
        manual = p.get('rosterStatus')
        if manual in ('released', 'demoted', 'injured'):
            inactive.append((p['player'], manual, '수동'))
            continue  # 수동 마킹 보존
        name = p['player']
        if name in active_pool:
            p['rosterStatus'] = 'active'
        else:
            p['rosterStatus'] = 'unverified'
            unverified.append(name)

    if inactive:
        print(f"  ⚠ 수동 마킹된 비활성 선수 {len(inactive)}명:")
        for name, status, _ in inactive:
            print(f"    · {name} → {status}")
    if unverified:
        print(f"  ⚠ 미검증 선수 {len(unverified)}명 (시즌 30위 밖, 계산 대상엔 포함):")
        for name in unverified:
            print(f"    · {name}")
    if not inactive and not unverified:
        print(f"  ✓ 전원 시즌타율 풀에서 active 확인")
    return lineups


def merge_data(lineups: list, stats_by_name: dict) -> list:
    """라인업에 시즌 타율 머지."""
    merged = []
    for player in lineups:
        name = player['player']
        if name in stats_by_name:
            stat = stats_by_name[name]
            player['seasonAvg'] = stat.get('avg', player.get('seasonAvg', 0.0))
            print(f"  ✓ {name}: 시즌 타율 자동 갱신 ({player['seasonAvg']:.3f})")
        else:
            # 자동 수집 실패 또는 신인 → 라인업 파일의 값 유지
            if 'seasonAvg' not in player:
                player['seasonAvg'] = player.get('last10Avg', 0.250)
                print(f"  - {name}: 시즌 타율 없음, last10으로 대체")
        merged.append(player)
    return merged


def main():
    print("=" * 50)
    print(" KBO 데이터 수집기")
    print("=" * 50)

    # 1. 라인업 파일 확보
    print("\n[1/4] 수동 라인업 파일 확인...")
    ensure_lineup_file()
    lineups = json.loads(LINEUP_FILE.read_text(encoding='utf-8'))
    print(f"  ✓ {len(lineups)}명 로드")

    # 2. 활성 선수 검증 (시즌타율 풀 fallback)
    print("\n[2/4] 활성 선수 검증 (시즌타율 풀 fallback)...")
    active_pool = fetch_active_hitter_pool()
    lineups = validate_roster(lineups, active_pool)

    # 3. 시즌 타율 자동 수집
    print("\n[3/4] KBO 시즌 타율 수집...")
    stats_by_name = fetch_season_stats()
    if stats_by_name:
        print(f"  ✓ {len(stats_by_name)}명 통계 수집")

    # 4. 머지 + 저장
    print("\n[4/4] 데이터 결합 + 저장...")
    merged = merge_data(lineups, stats_by_name)

    blob = {
        'fetched_at': datetime.now().isoformat(),
        'players': merged,
    }
    OUTPUT_FILE.write_text(
        json.dumps(blob, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(f"  ✓ {OUTPUT_FILE.name} 저장 완료 ({len(merged)}명)")
    print()
    print("=" * 50)
    print(" 이제 python app.py 실행하면 실제 데이터로 계산됩니다")
    print("=" * 50)


if __name__ == '__main__':
    main()
