# nolinterpark

NOL 인터파크([nol.interpark.com](https://nol.interpark.com/)) 취소표 예매 보조 매크로입니다.
[nolticket](https://github.com/zkdl0322/nolticket) 코드를 기반으로 합니다.

PySide6 GUI와 Selenium(Chrome)을 사용해 다음 흐름을 보조합니다.

1. [시작하기] → NOL 통합 로그인 페이지로 이동 (한 번 로그인하면 다음부터 자동 로그인)
2. 로그인 후 nol.interpark.com 으로 이동 → 원하는 공연에서 사용자가 [예매하기] 클릭
3. 보안문자가 있으면 입력 대기
4. "등급 별 가격"에서 좌석 등급을 읽어 목록 표시 → 등급 선택
5. 좌석 배치도에서 선택한 등급 색의 구역을 읽어 목록 표시 → 구역 선택
6. 필요 매수 입력
7. 선택한 구역을 순회: 회색(매진)이면 계속 순회, 색이 있는 좌석이 보이면 자동 클릭
   (2매 이상이면 가로·세로·대각선으로 붙어 있는 좌석만)
8. [선택 완료] → 가격 선택에서 "일반"을 필요 매수로 맞춤 → [N매 예매하기] 클릭 → 소리 알림

## 실행 환경

- Windows (소리 알림에 `winsound` 모듈 사용)
- Python 3.10 이상 권장
- Google Chrome 설치 필요 (ChromeDriver는 `webdriver-manager`가 자동 관리)

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python nolinterpark-fix.py
```

**시작하기**를 누르면 Chrome 창이 뜹니다. 로그인을 직접 완료한 뒤
매크로 창의 안내에 따라 번호를 입력하세요.

## 주의

- 본 도구는 개인 학습/편의 목적의 자동화 보조 도구입니다.
- 사이트 이용약관 및 관련 법규를 확인하고 본인 책임 하에 사용하세요.
