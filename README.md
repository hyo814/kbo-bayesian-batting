# KBO Bayesian Batting

```bash
cd kbo-bayesian-batting
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/본인계정/kbo-bayesian-batting.git
git push -u origin main
```

### 2. Render 연결
- render.com 가입 (GitHub로 로그인 가능)
- New + → Blueprint
- 방금 만든 리포 선택
- `render.yaml` 자동 인식 → 그대로 Apply
- 1~2분 후 URL 발급 (예: `https://kbo-bayesian-batting.onrender.com`)

## 매일 — 데이터 갱신

라인업이 발표되면 PC에서 5분 작업:

```bash
# 1. 라인업 파일 수정
# manual_lineups.json 열어서 오늘 경기로 변경
# (선수명, 타순, 상대 투수, 상대 투수 좌/우 정도만)

# 2. 시즌 타율 자동 수집
python crawler.py
# → data.json 갱신됨

# 3. 깃 푸시
git commit -am "data: 5/11"
git push

# → Render가 자동 재배포 (1~2분)
# → 폰 새로고침하면 새 데이터
```

## 폴더 구조

```
kbo-bayesian-batting/
├── app.py              # Flask 앱 (UI + 알고리즘)
├── crawler.py          # 매일 PC에서 실행
├── kbo_crawler.py      # KBO 윤리적 크롤러 코어
├── data.json           # 현재 추천 데이터 (배포에 포함)
├── manual_lineups.json # 매일 손으로 입력 (crawler가 자동 생성)
├── requirements.txt    # Python 의존성
├── Procfile            # gunicorn 실행 설정
├── render.yaml         # Render 자동 설정
└── .gitignore
```

## 가중치 튜닝

`app.py` 의 WEIGHTS 상수:

```python
WEIGHTS = {'season': 0.30, 'recent': 0.30, 'hand': 0.25, 'park': 0.15}
```

작년 데이터로 백테스트 후 본인이 조정.

## 트러블슈팅

### Render 배포 실패
- 빌드 로그에서 에러 확인 (Render 대시보드 → Logs)
- 보통 `requirements.txt` 의존성 충돌 → 버전 수정

### "데이터 없음" 표시
- `data.json`이 비어있거나 push 안 된 상태
- PC에서 `python crawler.py` 실행 → push

### 첫 접속 시 느림
- Render free tier는 15분 무활동 시 슬립
- 첫 요청에 30초~1분 걸림 (콜드스타트)
- 사용 빈도 높으면 paid plan ($7/월) 검토

### crawler가 빈 데이터 반환
- KBO 사이트가 ASP.NET이라 requests로 안 될 가능성
- README에 적힌 Playwright 전환 검토 필요
- 또는 manual_lineups.json에 시즌 타율도 직접 입력 가능

## 회색지대 회피 정책

- ✅ KBO 공식 사이트만 사용
- ✅ robots.txt 금지 경로 자동 차단
- ✅ Rate limiting (3초 간격)
- ✅ 24시간 캐싱
- ✅ User-Agent에 본인 정체 명시
