"""
KBO 안타 확률 추정 웹앱 (Render 배포 호환)
"""
import os
import math
import json
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"


# ==========================================================
# 알고리즘 — 베이즈 수축 적용 (Phase 1)
# ==========================================================

WEIGHTS = {'season': 0.30, 'recent': 0.30, 'hand': 0.25, 'venue': 0.15}

# KBO 역대 평균 타율 prior (대략 0.260~0.280 → 0.265)
LEAGUE_AVG = 0.265

# 수축 강도 (의사 타석 단위) — 클수록 prior 쪽으로 강하게 끌림
K_SEASON = 200   # 시즌타율 → 리그평균
K_RECENT = 100   # 최근10 → 시즌평균(수축본)
K_HAND = 100     # 좌/우투 split → 시즌평균(수축본)
K_VENUE = 100    # 홈/원정 → 시즌평균(수축본)
K_STADIUM = 80   # 구장별 → 홈/원정(수축본). 작은 표본이라 prior 강함

# 데이터에 AB가 없을 때의 휴리스틱 (시즌 초~중반 기준)
# venue = 홈/원정 split, stadium = 한 구장에서의 누적 (~25 AB)
DEFAULT_AB = {'season': 120, 'recent': 35, 'hand': 50, 'venue': 60, 'stadium': 25}

# 게임 문자열 → 구장명 파싱: "두산 vs KIA (잠실)" → "잠실"
def parse_stadium(game_str: str) -> str | None:
    if not game_str or '(' not in game_str:
        return None
    return game_str.split('(')[-1].rstrip(')').strip() or None


def shrink(observed: float, ab: float, prior: float, k: float) -> float:
    """베이즈 수축: (n·관측 + k·prior) / (n + k)."""
    if ab is None or ab <= 0:
        return prior
    return (ab * observed + k * prior) / (ab + k)


def calculate_hit_probability(p: dict) -> dict:
    status = p.get('rosterStatus', 'unknown')
    if status in ('released', 'demoted', 'injured'):
        reason_map = {'released': '방출/이적', 'demoted': '2군 말소', 'injured': '부상자명단'}
        return {
            'prob': 0.0, 'expectedPA': 0.0, 'perAB': 0.0,
            'breakdown': None, 'reason': reason_map[status],
        }
    if not p.get('isStarting'):
        return {
            'prob': 0.0, 'expectedPA': 0.0, 'perAB': 0.0,
            'breakdown': None, 'reason': '결장',
        }

    # 1. 시즌타율 → 리그평균으로 수축
    season_raw = p['seasonAvg']
    season_ab = p.get('seasonAB', DEFAULT_AB['season'])
    season_s = shrink(season_raw, season_ab, LEAGUE_AVG, K_SEASON)

    # 2. 최근10 → (수축된) 시즌평균으로 수축
    recent_raw = p['last10Avg']
    recent_ab = p.get('last10AB', DEFAULT_AB['recent'])
    recent_s = shrink(recent_raw, recent_ab, season_s, K_RECENT)

    # 3. 좌/우투 split → 시즌평균(수축)으로 수축
    if p['oppHand'] == 'L':
        hand_raw = p['vsLefty']
        hand_ab = p.get('vsLeftyAB', DEFAULT_AB['hand'])
    else:
        hand_raw = p['vsRighty']
        hand_ab = p.get('vsRightyAB', DEFAULT_AB['hand'])
    hand_s = shrink(hand_raw, hand_ab, season_s, K_HAND)

    # 4-a. 홈/원정 split → 시즌평균(수축)으로 수축 — 항상 계산 (stadium의 prior 역할)
    is_home = bool(p.get('isHome', False))
    if is_home:
        venue_raw = p.get('homeAvg', p.get('parkAvg', LEAGUE_AVG))
        venue_ab = p.get('homeAB', DEFAULT_AB['venue'])
    else:
        venue_raw = p.get('awayAvg', p.get('parkAvg', LEAGUE_AVG))
        venue_ab = p.get('awayAB', DEFAULT_AB['venue'])
    venue_s = shrink(venue_raw, venue_ab, season_s, K_VENUE)

    # 4-b. 구장별 split (있으면) → 홈/원정(수축)으로 수축
    stadium = p.get('stadium') or parse_stadium(p.get('game', ''))
    stadium_data = (p.get('stadiumAvg') or {}).get(stadium) if stadium else None
    if stadium_data and stadium_data.get('ab', 0) > 0:
        loc_raw = stadium_data['avg']
        loc_ab = stadium_data['ab']
        loc_s = shrink(loc_raw, loc_ab, venue_s, K_STADIUM)
        loc_label = stadium
        loc_source = 'stadium'
    else:
        loc_raw = venue_raw
        loc_ab = venue_ab
        loc_s = venue_s
        loc_label = '홈' if is_home else '원정'
        loc_source = 'home' if is_home else 'away'

    per_ab = (
        WEIGHTS['season'] * season_s +
        WEIGHTS['recent'] * recent_s +
        WEIGHTS['hand'] * hand_s +
        WEIGHTS['venue'] * loc_s
    )
    expected_pa = 5.0 - (p['lineupPos'] - 1) * 0.15
    prob = 1 - math.pow(max(0.0, 1 - per_ab), expected_pa)
    return {
        'prob': prob,
        'expectedPA': expected_pa,
        'perAB': per_ab,
        'isHome': is_home,
        'stadium': stadium,
        'breakdown': {
            'season': season_s, 'season_raw': season_raw, 'season_ab': season_ab,
            'recent': recent_s, 'recent_raw': recent_raw, 'recent_ab': recent_ab,
            'hand': hand_s, 'hand_raw': hand_raw, 'hand_ab': hand_ab,
            'venue': loc_s, 'venue_raw': loc_raw, 'venue_ab': loc_ab,
            'venue_label': loc_label, 'venue_source': loc_source,
            'home_away_value': venue_s, 'home_away_label': '홈' if is_home else '원정',
        },
    }


# ==========================================================
# 데이터 로드 — data.json 우선, 없으면 빈 리스트
# ==========================================================

def load_player_data() -> tuple[list[dict], str, str | None]:
    if DATA_FILE.exists():
        try:
            blob = json.loads(DATA_FILE.read_text(encoding='utf-8'))
            return blob['players'], 'crawled', blob.get('fetched_at')
        except (json.JSONDecodeError, KeyError):
            app.logger.warning("data.json 파싱 실패")
    return [], 'empty', None


# ==========================================================
# 라우트
# ==========================================================

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/recommend')
def api_recommend():
    players, source, fetched_at = load_player_data()

    enriched = []
    for p in players:
        result = calculate_hit_probability(p)
        enriched.append({**p, **result})

    enriched.sort(key=lambda x: x['prob'], reverse=True)
    # 계산 대상: 선발 + (active|unverified|unknown). released/demoted/injured만 제외.
    ELIGIBLE = ('active', 'unverified', 'unknown')
    eligible = [
        x for x in enriched
        if x.get('isStarting') and x.get('rosterStatus', 'unknown') in ELIGIBLE
    ]
    inactive = [x for x in enriched if x.get('rosterStatus') in ('released', 'demoted', 'injured')]

    return jsonify({
        'top3': eligible[:3],
        'benched': [x for x in enriched if not x.get('isStarting') and x.get('rosterStatus', 'unknown') in ELIGIBLE],
        'inactive': inactive,
        'meta': {
            'source': source,
            'fetched_at': fetched_at,
            'computed_at': datetime.now().isoformat(),
            'algorithm_version': '0.4-stadium',
            'weights': WEIGHTS,
            'shrinkage': {
                'league_avg': LEAGUE_AVG,
                'k': {
                    'season': K_SEASON, 'recent': K_RECENT,
                    'hand': K_HAND, 'venue': K_VENUE, 'stadium': K_STADIUM,
                },
                'default_ab': DEFAULT_AB,
            },
        },
    })


@app.route('/health')
def health():
    """Render 헬스체크용"""
    return jsonify({'status': 'ok'})


# ==========================================================
# HTML 템플릿
# ==========================================================

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KBO 안타 확률 추정</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=Noto+Sans+KR:wght@400;500;700;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root {
    --bg: #0a0e14; --panel: #131822; --line: #2a3142;
    --text: #f3f4f6; --dim: #7a8294;
    --led-green: #00ff88; --led-amber: #ffb020; --led-red: #ff3860;
    --gold: #d4a64a;
  }
  * { -webkit-tap-highlight-color: transparent; }
  html, body { font-family: 'Noto Sans KR', system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; min-height: 100vh; }
  body {
    background-image:
      radial-gradient(ellipse 80% 50% at 50% -20%, rgba(0,255,136,0.08), transparent),
      radial-gradient(ellipse 60% 40% at 100% 100%, rgba(255,176,32,0.05), transparent),
      linear-gradient(var(--line) 1px, transparent 1px),
      linear-gradient(90deg, var(--line) 1px, transparent 1px);
    background-size: auto, auto, 60px 60px, 60px 60px;
  }
  .display { font-family: 'Black Han Sans', sans-serif; letter-spacing: -0.01em; }
  .mono { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }
  .led-green { color: var(--led-green); text-shadow: 0 0 12px rgba(0,255,136,0.4); }
  .led-amber { color: var(--led-amber); text-shadow: 0 0 12px rgba(255,176,32,0.4); }
  .led-red { color: var(--led-red); text-shadow: 0 0 12px rgba(255,56,96,0.4); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
  .scoreboard { background: linear-gradient(180deg, #1a2030 0%, #0d121b 100%); border: 1px solid #2a3142; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 8px 24px rgba(0,0,0,0.4); }
  .cta-button { background: linear-gradient(180deg, #ffc445 0%, #d4a64a 100%); color: #1a1100; border: 1px solid #8a6a25; box-shadow: inset 0 1px 0 rgba(255,255,255,0.4), inset 0 -2px 0 rgba(0,0,0,0.2), 0 4px 16px rgba(212,166,74,0.4); transition: transform 0.1s; }
  .cta-button:hover:not(:disabled) { transform: translateY(-1px); }
  .cta-button:active:not(:disabled) { transform: translateY(1px); }
  .cta-button:disabled { opacity: 0.6; cursor: not-allowed; }
  .pick-card { position: relative; overflow: hidden; }
  .pick-card.rank-1 { border: 1px solid var(--gold); background: linear-gradient(180deg, rgba(212,166,74,0.08) 0%, transparent 50%), var(--panel); }
  .pick-card.rank-1::before { content: ''; position: absolute; inset: 0; background: radial-gradient(circle at 30% 0%, rgba(212,166,74,0.15), transparent 50%); pointer-events: none; }
  .bar { height: 6px; background: #2a3142; border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; background: var(--led-green); border-radius: 3px; transition: width 0.6s ease-out; }
  @keyframes countup { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .reveal { animation: countup 0.5s ease-out both; }
  .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--led-red); box-shadow: 0 0 8px var(--led-red); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .team-tag { font-size: 11px; padding: 2px 6px; border-radius: 3px; font-weight: 700; }
  .tag-SSG { background: #cc0000; color: white; }
  .tag-삼성 { background: #074ca1; color: white; }
  .tag-한화 { background: #ff6600; color: white; }
  .tag-두산 { background: #131230; color: white; border: 1px solid #444; }
  .tag-KIA { background: #ea0029; color: white; }
  .tag-KT { background: #000000; color: white; border: 1px solid #444; }
  .tag-LG { background: #c30452; color: white; }
  .tag-NC { background: #315288; color: white; }
  .tag-롯데 { background: #041e42; color: white; }
  .tag-키움 { background: #570514; color: white; }
  details > summary { list-style: none; cursor: pointer; }
  details > summary::-webkit-details-marker { display: none; }
  .source-badge { font-size: 10px; padding: 3px 8px; border-radius: 3px; letter-spacing: 0.05em; font-weight: 700; }
  .source-crawled { background: rgba(0,255,136,0.15); color: var(--led-green); border: 1px solid rgba(0,255,136,0.3); }
  .source-empty { background: rgba(255,56,96,0.15); color: var(--led-red); border: 1px solid rgba(255,56,96,0.3); }
  .skeleton-line { height: 12px; background: linear-gradient(90deg, #1a2030 0%, #2a3142 50%, #1a2030 100%); background-size: 200% 100%; border-radius: 4px; animation: shimmer 1.2s infinite; }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
</style>
</head>
<body class="px-4 py-6 sm:px-6 sm:py-10">
<div class="max-w-3xl mx-auto">
  <header class="mb-8">
    <div class="flex items-center gap-3 mb-2">
      <span class="pulse-dot"></span>
      <span class="mono text-xs led-red tracking-widest">LIVE · KBO 2026</span>
    </div>
    <h1 class="display text-5xl sm:text-6xl leading-none mb-2">
      오늘의 <span class="led-amber">안타</span><br>확률
    </h1>
    <div class="flex items-center justify-between flex-wrap gap-2">
      <p class="text-sm text-[var(--dim)]">베이지안 축소 기반 1안타+ 확률 추정 · <span class="mono" id="todayDate"></span></p>
      <span id="sourceBadge" class="source-badge hidden"></span>
    </div>
  </header>

  <section class="mb-8">
    <button id="recommendBtn" class="cta-button w-full py-5 rounded-md display text-2xl tracking-wider">
      확률 계산 →
    </button>
    <p class="text-xs text-[var(--dim)] mt-3 text-center">서버에서 데이터 로드 + 알고리즘 계산</p>
  </section>

  <section id="results" class="hidden space-y-3 mb-8"></section>
  <section id="inactiveBox" class="hidden mb-8"></section>

  <section class="mb-6">
    <details class="scoreboard p-4 rounded-md">
      <summary class="flex items-center justify-between">
        <span class="text-sm font-bold">⚙ 어떻게 계산했나요?</span>
        <span class="text-xs text-[var(--dim)]">탭하여 보기</span>
      </summary>
      <div class="mt-4 text-sm space-y-3 text-[var(--dim)] leading-relaxed">
        <p class="text-[var(--text)]"><b>1단계.</b> 한 타석당 안타 확률을 가중평균으로 추정</p>
        <pre class="mono text-xs bg-[var(--bg)] p-3 rounded overflow-x-auto leading-relaxed">perAB = 0.30 × 시즌타율
      + 0.30 × 최근10경기타율
      + 0.25 × 좌/우투수 스플릿
      + 0.15 × 경기장 타율 (구장별 ★ → 홈/원정 fallback)</pre>
        <p class="text-[var(--text)]"><b>2단계.</b> 라인업 위치로 예상 타석수 계산</p>
        <pre class="mono text-xs bg-[var(--bg)] p-3 rounded">expectedPA = 5.0 - (lineupPos - 1) × 0.15</pre>
        <p class="text-[var(--text)]"><b>3단계.</b> 한 경기 1안타 이상 확률 (이항분포 보색)</p>
        <pre class="mono text-xs bg-[var(--bg)] p-3 rounded">P(1+ hit) = 1 - (1 - perAB)^expectedPA</pre>
        <p class="text-[var(--text)] pt-2 border-t border-[var(--line)]"><b>★ 베이즈 수축 (v0.2)</b> — 표본 적은 split은 prior 쪽으로 끌어당김</p>
        <pre class="mono text-xs bg-[var(--bg)] p-3 rounded overflow-x-auto leading-relaxed">shrunk = (n × 관측 + k × prior) / (n + k)
  · 시즌타율 → 리그평균 0.265 (k=200)
  · 최근/좌우/홈or원정 → (수축된) 시즌평균 (k=100)
  · 구장별 ★ → (수축된) 홈or원정 (k=80)
prior 체인: 구장 → 홈/원정 → 시즌 → 리그평균
표본 작을수록 prior에 가깝게,
표본 클수록 관측값을 그대로 신뢰</pre>
      </div>
    </details>
  </section>

  <footer class="text-xs text-[var(--dim)] text-center pb-6 leading-relaxed">
    <p>개인 학습 프로젝트 · 통계적 추정치이며 정확성을 보장하지 않습니다</p>
  </footer>
</div>

<script>
const btn = document.getElementById('recommendBtn');
const resultsEl = document.getElementById('results');
const inactiveBox = document.getElementById('inactiveBox');
const sourceBadge = document.getElementById('sourceBadge');

const STATUS_LABEL = {
  released: '방출/이적', demoted: '2군 말소', injured: '부상자명단',
};

function renderBar(label, value, raw, ab, max = 0.5) {
  const pct = Math.min(100, (value / max) * 100);
  const showRaw = raw !== undefined && Math.abs(raw - value) > 0.005;
  const rawTag = showRaw
    ? `<span class="mono text-[10px] text-[var(--dim)]">원본 ${raw.toFixed(3)}${ab ? ` · ${ab}AB` : ''}</span>`
    : '';
  return `
    <div class="flex items-center gap-3">
      <span class="text-xs text-[var(--dim)] w-20 shrink-0">${label}</span>
      <div class="flex-1 flex flex-col gap-1">
        <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
        ${rawTag}
      </div>
      <span class="mono text-xs w-12 text-right">${value.toFixed(3)}</span>
    </div>`;
}

function renderCard(p, rank) {
  const probPct = (p.prob * 100).toFixed(1);
  const rankClass = rank === 1 ? 'rank-1' : '';
  const rankLabel = rank === 1 ? '1위 · 최고 확률' : `${rank}위`;
  const rankColor = rank === 1 ? 'text-[var(--gold)]' : 'text-[var(--dim)]';
  const probColor = p.prob >= 0.85 ? 'led-green' : p.prob >= 0.7 ? 'led-amber' : 'led-red';

  return `
  <article class="pick-card panel p-5 ${rankClass} reveal" style="animation-delay:${(rank-1)*0.1}s">
    <div class="flex items-center justify-between mb-3 relative">
      <span class="mono text-xs ${rankColor} font-bold tracking-wider">${rankLabel}</span>
      <span class="flex items-center gap-2">
        ${p.rosterStatus === 'unverified' ? '<span class="mono text-[10px] led-amber" title="시즌타율 풀에 안 잡힘 — 신예/대체선수일 수 있음">⚠ 미검증</span>' : ''}
        <span class="team-tag tag-${p.team}">${p.team}</span>
      </span>
    </div>
    <div class="flex items-end justify-between mb-4 relative">
      <div>
        <h2 class="display text-3xl mb-1">${p.player}</h2>
        <p class="text-xs text-[var(--dim)]">
          ${p.lineupPos}번 타자 ·
          <span class="mono ${p.isHome ? 'led-green' : 'led-amber'}" style="font-weight:700;letter-spacing:0.05em">${p.isHome ? 'HOME' : 'AWAY'}</span>
          · ${p.game}
        </p>
      </div>
      <div class="text-right">
        <div class="mono text-4xl ${probColor} font-bold leading-none">${probPct}<span class="text-xl">%</span></div>
        <div class="text-[10px] text-[var(--dim)] mt-1 tracking-wider">P(1안타+)</div>
      </div>
    </div>
    <div class="space-y-2 pt-3 border-t border-[var(--line)] relative">
      ${renderBar('시즌타율', p.breakdown.season, p.breakdown.season_raw, p.breakdown.season_ab)}
      ${renderBar('최근10경기', p.breakdown.recent, p.breakdown.recent_raw, p.breakdown.recent_ab)}
      ${renderBar(`vs ${p.oppHand === 'L' ? '좌투' : '우투'}`, p.breakdown.hand, p.breakdown.hand_raw, p.breakdown.hand_ab)}
      ${renderBar(
        `${p.breakdown.venue_label} 타율${p.breakdown.venue_source === 'stadium' ? ' ★' : ''}`,
        p.breakdown.venue, p.breakdown.venue_raw, p.breakdown.venue_ab
      )}
    </div>
    <div class="mt-3 pt-3 border-t border-[var(--line)] flex justify-between text-[11px] text-[var(--dim)] mono relative">
      <span>예상 타석 ${p.expectedPA.toFixed(1)}</span>
      <span>per-AB ${p.perAB.toFixed(3)}</span>
      <span>vs ${p.oppPitcher}</span>
    </div>
    ${p.note ? `<div class="mt-3 text-xs text-[var(--led-amber)] bg-[rgba(255,176,32,0.08)] border border-[rgba(255,176,32,0.2)] rounded px-3 py-2">⚠ ${p.note}</div>` : ''}
  </article>`;
}

function renderSkeleton() {
  return Array(3).fill(0).map(() => `
    <div class="panel p-5 space-y-3">
      <div class="skeleton-line w-1/3"></div>
      <div class="skeleton-line w-2/3" style="height:24px"></div>
      <div class="skeleton-line w-full"></div>
      <div class="skeleton-line w-full"></div>
    </div>`).join('');
}

function updateSourceBadge(meta) {
  sourceBadge.classList.remove('hidden', 'source-empty', 'source-crawled');
  if (meta.source === 'crawled') {
    sourceBadge.classList.add('source-crawled');
    const fetchedDate = meta.fetched_at ? new Date(meta.fetched_at).toLocaleString('ko-KR') : '-';
    sourceBadge.textContent = `● ${fetchedDate}`;
  } else {
    sourceBadge.classList.add('source-empty');
    sourceBadge.textContent = '● 데이터 없음';
  }
}

btn.addEventListener('click', async () => {
  resultsEl.classList.remove('hidden');
  resultsEl.innerHTML = renderSkeleton();
  btn.disabled = true;
  btn.textContent = '계산 중...';

  try {
    const response = await fetch('/api/recommend');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();

    updateSourceBadge(data.meta);

    if (!data.top3 || data.top3.length === 0) {
      resultsEl.innerHTML = `
        <div class="panel p-5 border-yellow-500">
          <p class="led-amber font-bold mb-2">⚠ 데이터가 비어있어요</p>
          <p class="text-sm text-[var(--dim)]">PC에서 <code class="mono">python crawler.py</code> 실행 후 data.json을 GitHub에 push하면 자동 재배포됩니다.</p>
        </div>`;
    } else {
      resultsEl.innerHTML = data.top3.map((p, i) => renderCard(p, i + 1)).join('');
      resultsEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    // 비활성 선수 경고 (방출/말소/부상)
    if (data.inactive && data.inactive.length > 0) {
      inactiveBox.classList.remove('hidden');
      inactiveBox.innerHTML = `
        <div class="panel p-4 border" style="border-color: var(--led-red)">
          <p class="led-red font-bold text-sm mb-2">⚠ 계산에서 제외된 선수 ${data.inactive.length}명</p>
          <p class="text-xs text-[var(--dim)] mb-3">manual_lineups에 방출/말소/부상으로 마킹된 선수입니다. 라인업 갱신 권장.</p>
          <ul class="space-y-1 text-sm">
            ${data.inactive.map(p => `
              <li class="flex justify-between">
                <span><span class="team-tag tag-${p.team}">${p.team}</span> ${p.player}</span>
                <span class="led-red mono text-xs">${STATUS_LABEL[p.rosterStatus] || p.rosterStatus}</span>
              </li>`).join('')}
          </ul>
        </div>`;
    } else {
      inactiveBox.classList.add('hidden');
    }
  } catch (err) {
    resultsEl.innerHTML = `
      <div class="panel p-5">
        <p class="led-red font-bold mb-2">⚠ 에러 발생</p>
        <p class="text-sm text-[var(--dim)]">${err.message}</p>
      </div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '다시 계산 →';
  }
});

const d = new Date();
const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
document.getElementById('todayDate').textContent =
  `${d.getFullYear()}.${String(d.getMonth()+1).padStart(2,'0')}.${String(d.getDate()).padStart(2,'0')} ${days[d.getDay()]}`;
</script>
</body>
</html>
"""


# ==========================================================
# 실행 (로컬 테스트용 / Render는 gunicorn으로 띄움)
# ==========================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
