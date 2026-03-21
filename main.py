#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOSPI 모닝브리핑 멀티에이전트 토론 시스템
- 5개 분석 에이전트 + 1개 하이브리드 모더레이터
- 3-Phase 토론: 독립분석(병렬) → 교차반론(8방향) → 모더레이터 종합
- 데이터: yfinance + 네이버금융 스크래핑
- 출력: HTML 다크모드 대시보드(Gist) + Telegram 알림
"""

import os
import json
import asyncio
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pytz
import anthropic
import yfinance as yf
import requests
from bs4 import BeautifulSoup

# ============================================================
# 로깅
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============================================================
# 설정 로드
# ============================================================
def load_config() -> dict:
    """환경변수 → config.json 폴백으로 설정 로드"""
    cfg = {
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "telegram_token":    os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id":  os.environ.get("TELEGRAM_CHAT_ID", ""),
        "github_pat":        os.environ.get("GH_PAT", "") or os.environ.get("GITHUB_PAT", ""),
    }
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


# ============================================================
# 시장 데이터 수집
# ============================================================
@dataclass
class MarketData:
    """수집된 시장 데이터"""
    date: str = ""
    # KOSPI
    kospi_current: float = 0.0
    kospi_change: float = 0.0
    kospi_change_pct: float = 0.0
    kospi_volume: str = ""
    sectors: Dict[str, Dict] = field(default_factory=dict)
    # 미국 증시
    sp500_close: float = 0.0
    sp500_change_pct: float = 0.0
    nasdaq_close: float = 0.0
    nasdaq_change_pct: float = 0.0
    dow_close: float = 0.0
    dow_change_pct: float = 0.0
    vix: float = 0.0
    # 환율
    usdkrw: float = 0.0
    usdkrw_change_pct: float = 0.0
    # 원자재
    gold_price: float = 0.0
    gold_change_pct: float = 0.0
    wti_price: float = 0.0
    wti_change_pct: float = 0.0
    brent_price: float = 0.0
    brent_change_pct: float = 0.0
    # 미국 국채
    us10y: float = 0.0
    us2y: float = 0.0
    # 수집 오류
    collection_errors: List[str] = field(default_factory=list)

    def to_summary_str(self) -> str:
        """에이전트에게 전달할 시장 데이터 요약 문자열"""
        kst = pytz.timezone("Asia/Seoul")
        now_kst = datetime.now(kst)
        spread = self.us10y - self.us2y

        lines = [
            f"=== KOSPI 모닝브리핑 시장 데이터 ({now_kst.strftime('%Y년 %m월 %d일 %H:%M KST')}) ===",
            "",
            "【전일 KOSPI 현황】",
            f"  KOSPI 지수: {self.kospi_current:,.2f}",
            f"  등락: {self.kospi_change:+.2f} ({self.kospi_change_pct:+.2f}%)",
            f"  거래량: {self.kospi_volume or 'N/A'}",
            "",
        ]
        if self.sectors:
            sorted_sec = sorted(
                self.sectors.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True
            )
            lines.append("【업종별 등락 (상위/하위)】")
            for name, d in sorted_sec[:5]:
                lines.append(f"  {name}: {d.get('change_pct', 0):+.2f}%")
            lines.append("  ...")
            for name, d in sorted_sec[-3:]:
                lines.append(f"  {name}: {d.get('change_pct', 0):+.2f}%")
            lines.append("")
        lines.extend([
            "【미국 증시 (전일 종가)】",
            f"  S&P 500: {self.sp500_close:,.2f} ({self.sp500_change_pct:+.2f}%)",
            f"  NASDAQ:  {self.nasdaq_close:,.2f} ({self.nasdaq_change_pct:+.2f}%)",
            f"  Dow:     {self.dow_close:,.2f} ({self.dow_change_pct:+.2f}%)",
            f"  VIX:     {self.vix:.2f}",
            "",
            "【환율 & 원자재】",
            f"  USD/KRW: {self.usdkrw:,.2f} ({self.usdkrw_change_pct:+.2f}%)",
            f"  금 (Gold):  ${self.gold_price:,.2f}/oz ({self.gold_change_pct:+.2f}%)",
            f"  WTI 유가:  ${self.wti_price:.2f}/bbl ({self.wti_change_pct:+.2f}%)",
            f"  Brent:     ${self.brent_price:.2f}/bbl ({self.brent_change_pct:+.2f}%)",
            "",
            "【미국 국채 금리】",
            f"  10년물: {self.us10y:.3f}%",
            f"  2년물:  {self.us2y:.3f}%",
            f"  장단기 스프레드(10Y-2Y): {spread:+.3f}%",
        ])
        if self.collection_errors:
            lines += ["", "【데이터 수집 오류 (참고)】"] + [f"  - {e}" for e in self.collection_errors]
        return "\n".join(lines)


class MarketDataCollector:
    """yfinance + 네이버금융 기반 시장 데이터 수집기"""
    NAVER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://finance.naver.com/",
    }

    # yfinance 심볼 → 데이터 필드 매핑
    YF_SYMBOLS = {
        "^GSPC":    "sp500",
        "^IXIC":    "nasdaq",
        "^DJI":     "dow",
        "^VIX":     "vix",
        "USDKRW=X": "usdkrw",
        "GC=F":     "gold",
        "CL=F":     "wti",
        "BZ=F":     "brent",
        "^TNX":     "us10y",
        "^IRX":     "us2y",
        "^KS11":    "kospi_yf",  # 네이버 실패 시 폴백
    }

    def _get_ticker_price(self, symbol: str) -> Tuple[float, float]:
        """단일 종목 가격 및 등락률 조회"""
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            hist = hist.dropna(subset=["Close"])
            if len(hist) >= 2:
                cur = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                return cur, (cur - prev) / prev * 100
            elif len(hist) == 1:
                return float(hist["Close"].iloc[-1]), 0.0
        except Exception as e:
            logger.debug(f"yfinance {symbol}: {e}")
        return 0.0, 0.0

    def collect_yfinance(self, data: MarketData) -> MarketData:
        """yfinance로 미국 증시/환율/원자재 수집 (병렬)"""
        try:
            with ThreadPoolExecutor(max_workers=6) as ex:
                results = dict(zip(
                    self.YF_SYMBOLS.keys(),
                    ex.map(self._get_ticker_price, self.YF_SYMBOLS.keys())
                ))
            r = results
            data.sp500_close, data.sp500_change_pct   = r["^GSPC"]
            data.nasdaq_close, data.nasdaq_change_pct = r["^IXIC"]
            data.dow_close, data.dow_change_pct       = r["^DJI"]
            data.vix                                  = r["^VIX"][0]
            data.usdkrw, data.usdkrw_change_pct       = r["USDKRW=X"]
            data.gold_price, data.gold_change_pct     = r["GC=F"]
            data.wti_price, data.wti_change_pct       = r["CL=F"]
            data.brent_price, data.brent_change_pct   = r["BZ=F"]
            data.us10y                                = r["^TNX"][0]
            data.us2y                                 = r["^IRX"][0]
            # KOSPI 폴백
            if data.kospi_current == 0.0:
                price, pct = r["^KS11"]
                if price > 0:
                    data.kospi_current = price
                    data.kospi_change_pct = pct
            logger.info("yfinance 수집 완료")
        except Exception as e:
            logger.warning(f"yfinance 전체 실패: {e}")
            data.collection_errors.append(f"yfinance: {str(e)[:120]}")
        return data

    def collect_naver_kospi(self, data: MarketData) -> MarketData:
        """네이버금융 KOSPI 지수 스크래핑"""
        try:
            url = "https://finance.naver.com/sise/sise_index.nhn?code=KOSPI"
            resp = requests.get(url, headers=self.NAVER_HEADERS, timeout=10)
            resp.raise_for_status()
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")

            # 현재가 — 여러 셀렉터 시도
            for sel in ["#now_value", ".now_value", "strong.num"]:
                elem = soup.select_one(sel)
                if elem:
                    try:
                        data.kospi_current = float(elem.text.strip().replace(",", ""))
                        break
                    except ValueError:
                        continue

            # 등락폭
            for sel in ["#change_value", ".change_value"]:
                elem = soup.select_one(sel)
                if elem:
                    try:
                        data.kospi_change = float(elem.text.strip().replace(",", ""))
                        break
                    except ValueError:
                        continue

            if data.kospi_current > 0 and data.kospi_change != 0:
                prev = data.kospi_current - data.kospi_change
                if prev > 0:
                    data.kospi_change_pct = data.kospi_change / prev * 100

            # 거래량
            elem = soup.select_one("#quant")
            if elem:
                data.kospi_volume = elem.text.strip()

            logger.info(f"네이버 KOSPI: {data.kospi_current:,.2f} ({data.kospi_change_pct:+.2f}%)")
        except Exception as e:
            logger.warning(f"네이버 KOSPI 실패: {e}")
            data.collection_errors.append(f"Naver KOSPI: {str(e)[:100]}")
        return data

    def collect_naver_sectors(self, data: MarketData) -> MarketData:
        """네이버금융 업종별 지수 스크래핑"""
        try:
            url = "https://finance.naver.com/sise/sise_industry.nhn"
            resp = requests.get(url, headers=self.NAVER_HEADERS, timeout=10)
            resp.raise_for_status()
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")

            table = soup.select_one("table.type_1") or soup.select_one("table")
            if table:
                for row in table.select("tr"):
                    cells = row.select("td")
                    if len(cells) < 4:
                        continue
                    name_elem = cells[0].select_one("a")
                    if not name_elem:
                        continue
                    name = name_elem.text.strip()
                    try:
                        current = float(cells[1].text.strip().replace(",", ""))
                        pct_text = cells[3].text.strip().replace("%", "").replace(",", "").replace("+", "")
                        pct = float(pct_text)
                        data.sectors[name] = {"current": current, "change_pct": pct}
                    except (ValueError, IndexError):
                        continue
            logger.info(f"네이버 업종 수집: {len(data.sectors)}개")
        except Exception as e:
            logger.warning(f"네이버 업종 실패: {e}")
            data.collection_errors.append(f"Naver sectors: {str(e)[:100]}")
        return data

    def collect_all(self) -> MarketData:
        """전체 데이터 수집"""
        kst = pytz.timezone("Asia/Seoul")
        data = MarketData(date=datetime.now(kst).strftime("%Y-%m-%d"))
        logger.info("시장 데이터 수집 시작...")
        data = self.collect_naver_kospi(data)
        data = self.collect_naver_sectors(data)
        data = self.collect_yfinance(data)
        logger.info("시장 데이터 수집 완료")
        return data


# ============================================================
# 에이전트 설정
# ============================================================
@dataclass
class AgentAnalysis:
    """에이전트 분석 결과"""
    agent_name: str
    agent_role: str
    analysis: str
    outlook: str       # 상승 / 중립 / 하락
    key_points: List[str] = field(default_factory=list)
    confidence: float = 0.5


AGENT_CONFIGS: Dict[str, dict] = {
    "매크로": {
        "role": "거시경제 분석가",
        "system": (
            "당신은 KOSPI 모닝브리핑의 거시경제 분석 에이전트입니다.\n"
            "글로벌 금리·달러·무역·경기사이클 등 매크로 환경을 중심으로 KOSPI 전망을 분석합니다.\n"
            "펀더멘털 지표와 경제 이론에 근거한 논리적 분석을 제공합니다. 한국어로 작성하세요."
        ),
    },
    "테크니컬": {
        "role": "기술적 분석가",
        "system": (
            "당신은 KOSPI 모닝브리핑의 기술적 분석 에이전트입니다.\n"
            "차트 패턴·이동평균·거래량·기술적 지표를 중심으로 단기 방향성을 분석합니다.\n"
            "지지/저항 레벨과 모멘텀 신호를 구체적으로 언급합니다. 한국어로 작성하세요."
        ),
    },
    "섹터": {
        "role": "섹터 로테이션 분석가",
        "system": (
            "당신은 KOSPI 모닝브리핑의 섹터 분석 에이전트입니다.\n"
            "업종별 상대 강도·섹터 로테이션·개별 섹터 펀더멘털을 분석합니다.\n"
            "선도 섹터와 부진 섹터를 파악해 시장 건전성을 평가합니다. 한국어로 작성하세요."
        ),
    },
    "센티먼트": {
        "role": "시장 심리 분석가",
        "system": (
            "당신은 KOSPI 모닝브리핑의 시장 심리 분석 에이전트입니다.\n"
            "VIX·외국인/기관 수급·투자자 심리·공포-탐욕 지표를 분석합니다.\n"
            "이론보다 시장 참여자들의 실제 행동과 심리에 집중합니다. 한국어로 작성하세요."
        ),
    },
    "리스크": {
        "role": "리스크 관리 분석가 (보수적 실적 추정)",
        "system": (
            "당신은 KOSPI 모닝브리핑의 리스크 분석 에이전트입니다.\n"
            "항상 보수적 시각으로 하방 위험과 잠재적 취약성을 분석합니다.\n"
            "과도한 낙관론을 견제하고 지정학·유동성·규제 리스크를 식별합니다. 한국어로 작성하세요."
        ),
    },
}

# 교차 반론 페어링 (비대칭 8방향)
REBUTTAL_PAIRS: List[Tuple[str, str, str]] = [
    ("매크로",    "센티먼트", "이론 vs 현실"),
    ("센티먼트",  "매크로",   "현실 vs 이론"),
    ("테크니컬",  "섹터",     "가격 vs 가치"),
    ("섹터",      "테크니컬", "가치 vs 가격"),
    ("리스크",    "매크로",   "실적 손상 견제"),
    ("리스크",    "섹터",     "취약성 견제"),
    ("센티먼트",  "리스크",   "과도한 비관 견제"),
    ("테크니컬",  "매크로",   "차트 vs 펀더멘털"),
]

MODERATOR_SYSTEM = (
    "당신은 KOSPI 모닝브리핑의 모더레이터입니다.\n"
    "5개 에이전트(매크로·테크니컬·섹터·센티먼트·리스크)의 분석과 교차 반론을 종합하여\n"
    "오늘의 KOSPI에 대한 최종 투자 판단을 내립니다.\n\n"
    "판단 기준:\n"
    "1. 강세/약세 신호 수 집계 (규칙 기반)\n"
    "2. 에이전트 간 핵심 이견 파악\n"
    "3. 리스크 요인 가중 (하방 리스크 = 상방 기회 × 1.3)\n"
    "4. 최종 전망: 상승/중립/하락 + 근거 2-3가지 + 핵심 관심 지표\n\n"
    "균형 잡힌 시각을 유지하되 소수 의견도 반영합니다. 한국어로 작성하세요."
)


class KOSPIAgent:
    """KOSPI 분석 에이전트"""

    def __init__(self, client: anthropic.AsyncAnthropic, name: str, config: dict):
        self.client = client
        self.name = name
        self.role = config["role"]
        self.system = config["system"]

    async def analyze(self, market_summary: str) -> AgentAnalysis:
        """Phase 1: 독립 분석"""
        logger.info(f"[{self.name}] 독립 분석 시작...")
        prompt = (
            f"{market_summary}\n\n"
            f"위 시장 데이터를 바탕으로 {self.role}로서 오늘의 KOSPI를 분석해주세요.\n\n"
            "다음 형식으로 작성하세요:\n"
            "## 전망: [상승/중립/하락]\n"
            "## 핵심 근거 (3가지)\n1. ...\n2. ...\n3. ...\n"
            "## 상세 분석\n(200-300자 이내로 핵심만)"
        )
        try:
            resp = await self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=600,
                system=self.system,
                messages=[{"role": "user", "content": prompt}],
            )
            content = resp.content[0].text
            outlook = self._extract_outlook(content)
            key_points = self._extract_key_points(content)
            logger.info(f"[{self.name}] 완료 → {outlook}")
            return AgentAnalysis(
                agent_name=self.name, agent_role=self.role,
                analysis=content, outlook=outlook,
                key_points=key_points, confidence=0.7,
            )
        except Exception as e:
            logger.error(f"[{self.name}] 분석 실패: {e}")
            return AgentAnalysis(
                agent_name=self.name, agent_role=self.role,
                analysis=f"분석 실패: {str(e)[:200]}", outlook="중립", confidence=0.0,
            )

    async def rebut(
        self, target: AgentAnalysis, mine: AgentAnalysis, pair_desc: str
    ) -> str:
        """Phase 2: 교차 반론"""
        logger.info(f"[{self.name}] → [{target.agent_name}] 반론({pair_desc})")
        prompt = (
            f"나({self.name})의 기존 분석:\n{mine.analysis[:400]}\n\n"
            f"{target.agent_name}의 분석 (반론 대상):\n{target.analysis[:400]}\n\n"
            f"{pair_desc} 관점에서 상대방의 핵심 논리에 근거 있는 반론을 "
            "100-150자 이내로 간결하게 제시하세요."
        )
        try:
            resp = await self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                system=self.system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            logger.error(f"반론 실패 [{self.name}→{target.agent_name}]: {e}")
            return f"반론 생성 실패: {str(e)[:100]}"

    # ── 내부 파서 ──────────────────────────────────────────
    def _extract_outlook(self, text: str) -> str:
        import re
        m = re.search(r"전망\s*[:\s]\s*[\[【]?\s*(상승|중립|하락|강세|약세|보합)", text)
        if m:
            w = m.group(1)
            return "상승" if w in ("강세", "상승") else ("하락" if w in ("약세", "하락") else "중립")
        bull = text.count("상승") + text.count("강세") + text.count("긍정")
        bear = text.count("하락") + text.count("약세") + text.count("부정") + text.count("위험")
        if bull > bear + 1:
            return "상승"
        if bear > bull + 1:
            return "하락"
        return "중립"

    def _extract_key_points(self, text: str) -> List[str]:
        import re
        pts = re.findall(r"^\d+\.\s*(.+)$", text, re.MULTILINE)
        return [p.strip()[:100] for p in pts[:3]]


class Moderator:
    """하이브리드(규칙+LLM) 모더레이터"""

    def __init__(self, client: anthropic.AsyncAnthropic):
        self.client = client

    async def synthesize(
        self, analyses: Dict[str, AgentAnalysis], rebuttals: List[dict]
    ) -> str:
        logger.info("[모더레이터] 종합 판단 시작...")
        counts = {"상승": 0, "중립": 0, "하락": 0}
        for a in analyses.values():
            if a.outlook in counts:
                counts[a.outlook] += 1
        rule_signal = (
            f"전망 집계 → 상승 {counts['상승']}개 / 중립 {counts['중립']}개 / 하락 {counts['하락']}개"
        )
        analyses_text = "\n\n".join(
            f"### {n} ({a.outlook})\n{a.analysis[:400]}" for n, a in analyses.items()
        )
        rebuttals_text = "\n".join(
            f"[{r['from']}→{r['to']}] ({r['pair_desc']}): {r['rebuttal'][:150]}"
            for r in rebuttals
        )
        prompt = (
            f"규칙 기반 집계:\n{rule_signal}\n\n"
            f"에이전트별 분석 요약:\n{analyses_text}\n\n"
            f"교차 반론 요약:\n{rebuttals_text}\n\n"
            "위 내용을 종합하여 최종 판단을 작성하세요.\n\n"
            "## 최종 전망: [상승/중립/하락]\n"
            "## 핵심 근거 (2-3가지)\n1. ...\n2. ...\n"
            "## 핵심 이견 사항\n"
            "## 오늘의 관심 지표\n"
            "## 주의사항"
        )
        try:
            resp = await self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=900,
                system=MODERATOR_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            result = resp.content[0].text
            logger.info("[모더레이터] 완료")
            return result
        except Exception as e:
            logger.error(f"모더레이터 실패: {e}")
            best = max(counts, key=counts.get)
            return (
                f"## 최종 전망: {best}\n"
                f"## 핵심 근거\n1. {rule_signal}\n2. LLM 실패 → 단순 집계 적용\n"
                f"## 주의사항\nAPI 오류({str(e)[:80]})로 LLM 종합 대신 다수결 적용됩니다."
            )


# ============================================================
# 3-Phase 토론 오케스트레이터
# ============================================================
@dataclass
class DiscussionResult:
    market_data: MarketData
    phase1_analyses: Dict[str, AgentAnalysis]
    phase2_rebuttals: List[dict]
    phase3_synthesis: str
    total_duration_sec: float = 0.0
    timestamp: str = ""


class DiscussionOrchestrator:
    def __init__(self, config: dict):
        self.client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        self.agents = {
            name: KOSPIAgent(self.client, name, cfg)
            for name, cfg in AGENT_CONFIGS.items()
        }
        self.moderator = Moderator(self.client)

    async def run(self, market_data: MarketData) -> DiscussionResult:
        import time
        start = time.time()
        kst = pytz.timezone("Asia/Seoul")
        timestamp = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
        summary = market_data.to_summary_str()

        # ── Phase 1: 독립 분석 (병렬) ────────────────────
        logger.info("=== Phase 1: 독립 분석 (병렬) ===")
        p1_tasks = [agent.analyze(summary) for agent in self.agents.values()]
        p1_results = await asyncio.gather(*p1_tasks, return_exceptions=True)

        phase1: Dict[str, AgentAnalysis] = {}
        for name, result in zip(self.agents.keys(), p1_results):
            if isinstance(result, Exception):
                logger.error(f"Phase1 [{name}] 예외: {result}")
                phase1[name] = AgentAnalysis(
                    agent_name=name, agent_role=AGENT_CONFIGS[name]["role"],
                    analysis="분석 실패", outlook="중립",
                )
            else:
                phase1[name] = result

        # ── Phase 2: 교차 반론 (병렬) ────────────────────
        logger.info("=== Phase 2: 교차 반론 (병렬) ===")
        rebuttal_meta = [
            (frm, to, desc)
            for frm, to, desc in REBUTTAL_PAIRS
            if frm in self.agents and to in phase1
        ]
        r_tasks = [
            self.agents[frm].rebut(phase1[to], phase1[frm], desc)
            for frm, to, desc in rebuttal_meta
        ]
        r_results = await asyncio.gather(*r_tasks, return_exceptions=True)

        phase2: List[dict] = []
        for (frm, to, desc), res in zip(rebuttal_meta, r_results):
            phase2.append({
                "from": frm, "to": to, "pair_desc": desc,
                "rebuttal": str(res) if isinstance(res, Exception) else res,
            })

        # ── Phase 3: 모더레이터 종합 ──────────────────────
        logger.info("=== Phase 3: 모더레이터 종합 ===")
        synthesis = await self.moderator.synthesize(phase1, phase2)

        duration = time.time() - start
        logger.info(f"전체 완료: {duration:.1f}초")
        return DiscussionResult(
            market_data=market_data,
            phase1_analyses=phase1,
            phase2_rebuttals=phase2,
            phase3_synthesis=synthesis,
            total_duration_sec=duration,
            timestamp=timestamp,
        )


# ============================================================
# HTML 리포트 생성
# ============================================================
def _outlook_emoji(o: str) -> str:
    return {"상승": "🟢", "중립": "🟡", "하락": "🔴"}.get(o, "⚪")

def _outlook_color(o: str) -> str:
    return {"상승": "#00c853", "중립": "#ffd600", "하락": "#ff1744"}.get(o, "#888")

def _sign_color(v: float, invert: bool = False) -> str:
    pos = "#00c853"
    neg = "#ff1744"
    if invert:
        pos, neg = neg, pos
    return pos if v >= 0 else neg


def generate_html_report(result: DiscussionResult) -> str:
    md = result.market_data

    # 업종 차트 데이터
    sec_sorted = sorted(md.sectors.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True)[:12]
    sec_names   = json.dumps([s[0] for s in sec_sorted], ensure_ascii=False)
    sec_values  = json.dumps([round(s[1].get("change_pct", 0), 2) for s in sec_sorted])
    sec_colors  = json.dumps(["#00c853" if s[1].get("change_pct", 0) >= 0 else "#ff1744" for s in sec_sorted])

    # 에이전트 카드
    outlook_counts = {"상승": 0, "중립": 0, "하락": 0}
    agent_cards_html = ""
    for name, a in result.phase1_analyses.items():
        if a.outlook in outlook_counts:
            outlook_counts[a.outlook] += 1
        color = _outlook_color(a.outlook)
        emoji = _outlook_emoji(a.outlook)
        body = (
            a.analysis
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        agent_cards_html += f"""
        <div class="agent-card">
          <div class="agent-header" style="border-left:4px solid {color}">
            <span class="agent-name">{emoji} {name}</span>
            <span class="agent-role">{a.agent_role}</span>
            <span class="agent-outlook" style="color:{color}">{a.outlook}</span>
          </div>
          <div class="agent-body">{body}</div>
        </div>"""

    # 반론 아이템
    rebuttal_html = ""
    for r in result.phase2_rebuttals:
        body = (
            r["rebuttal"]
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        rebuttal_html += f"""
        <div class="rebuttal-item">
          <div class="rebuttal-head">
            <span class="r-from">{r['from']}</span>
            <span class="r-arrow">→</span>
            <span class="r-to">{r['to']}</span>
            <span class="r-desc">({r['pair_desc']})</span>
          </div>
          <div class="rebuttal-body">{body}</div>
        </div>"""

    # 최종 종합
    final_outlook = "중립"
    if "## 최종 전망: 상승" in result.phase3_synthesis:
        final_outlook = "상승"
    elif "## 최종 전망: 하락" in result.phase3_synthesis:
        final_outlook = "하락"
    final_color = _outlook_color(final_outlook)
    final_emoji = _outlook_emoji(final_outlook)
    synthesis_html = (
        result.phase3_synthesis
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("## ", "<h3>").replace("\n", "<br>")
    )

    chart_js = ""
    if sec_sorted:
        chart_js = f"""
<script>
(function(){{
  var ctx = document.getElementById('sectorChart').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {sec_names},
      datasets: [{{ data: {sec_values}, backgroundColor: {sec_colors}, borderRadius: 4 }}]
    }},
    options: {{
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ color: '#8899aa', font: {{ size: 10 }} }}, grid: {{ color: '#1e3a5f' }} }},
        y: {{ ticks: {{ color: '#8899aa', callback: v => v + '%' }}, grid: {{ color: '#1e3a5f' }} }}
      }}
    }}
  }});
}})();
</script>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KOSPI 모닝브리핑 {md.date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0e1a;color:#e0e6f0;font-family:'Segoe UI','Malgun Gothic',sans-serif;line-height:1.6;min-height:100vh}}
.hdr{{background:linear-gradient(135deg,#0f1729,#1a2744);padding:22px 32px;border-bottom:1px solid #1e3a5f}}
.hdr h1{{font-size:1.75rem;font-weight:700;color:#7db8f7;margin-bottom:4px}}
.hdr .sub{{color:#8899aa;font-size:.85rem}}
.wrap{{max-width:1400px;margin:0 auto;padding:24px 32px}}
.mkt-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:28px}}
.mkt-card{{background:#111827;border:1px solid #1e3a5f;border-radius:10px;padding:14px 18px}}
.mkt-card .lbl{{font-size:.72rem;color:#6b7a8d;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}}
.mkt-card .val{{font-size:1.35rem;font-weight:700}}
.mkt-card .chg{{font-size:.82rem;margin-top:3px}}
.sec{{margin-bottom:28px}}
.sec-title{{font-size:1.1rem;font-weight:600;color:#7db8f7;padding:7px 0;border-bottom:1px solid #1e3a5f;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.badge{{background:#1e3a5f;color:#7db8f7;font-size:.68rem;padding:2px 7px;border-radius:4px}}
.agents-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}}
.agent-card{{background:#111827;border:1px solid #1e3a5f;border-radius:10px;overflow:hidden}}
.agent-header{{background:#0f1729;padding:10px 14px;display:flex;align-items:center;gap:9px}}
.agent-name{{font-weight:700;font-size:.95rem;flex:1}}
.agent-role{{font-size:.72rem;color:#6b7a8d}}
.agent-outlook{{font-weight:700;font-size:.82rem}}
.agent-body{{padding:12px 14px;font-size:.8rem;color:#c0ccd8;line-height:1.7}}
.outlook-pills{{display:flex;gap:10px;margin-bottom:14px}}
.pill{{padding:5px 14px;border-radius:18px;font-size:.82rem;font-weight:600}}
.rebuttal-item{{background:#111827;border:1px solid #1e3a5f;border-radius:8px;margin-bottom:10px;overflow:hidden}}
.rebuttal-head{{background:#0f1729;padding:7px 12px;display:flex;align-items:center;gap:7px;font-size:.82rem}}
.r-from{{color:#7db8f7;font-weight:600}}
.r-arrow{{color:#ff6b6b}}
.r-to{{color:#ffd166;font-weight:600}}
.r-desc{{color:#6b7a8d;font-size:.74rem}}
.rebuttal-body{{padding:9px 12px;font-size:.8rem;color:#c0ccd8;line-height:1.65}}
.synth-box{{background:#111827;border:1px solid {final_color}44;border-radius:12px;padding:22px}}
.synth-verdict{{display:flex;align-items:center;gap:12px;margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #1e3a5f}}
.verdict-lbl{{font-size:.82rem;color:#6b7a8d}}
.verdict-val{{font-size:2rem;font-weight:800;color:{final_color}}}
.synth-body{{font-size:.83rem;color:#c0ccd8;line-height:1.8}}
.synth-body h3{{color:#7db8f7;font-size:.92rem;margin:10px 0 5px}}
.chart-box{{background:#111827;border:1px solid #1e3a5f;border-radius:10px;padding:14px;margin-bottom:14px}}
.footer{{text-align:center;padding:20px;color:#3a4a5a;font-size:.75rem;border-top:1px solid #1e3a5f;margin-top:28px}}
</style>
</head>
<body>
<div class="hdr">
  <h1>📊 KOSPI 모닝브리핑</h1>
  <div class="sub">멀티에이전트 토론 시스템 | {result.timestamp} | 소요: {result.total_duration_sec:.1f}초</div>
</div>
<div class="wrap">

  <!-- 시장 현황 -->
  <div class="sec">
    <div class="sec-title">📈 시장 현황</div>
    <div class="mkt-grid">
      <div class="mkt-card">
        <div class="lbl">KOSPI</div>
        <div class="val">{md.kospi_current:,.2f}</div>
        <div class="chg" style="color:{_sign_color(md.kospi_change_pct)}">{md.kospi_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">S&amp;P 500</div>
        <div class="val">{md.sp500_close:,.2f}</div>
        <div class="chg" style="color:{_sign_color(md.sp500_change_pct)}">{md.sp500_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">NASDAQ</div>
        <div class="val">{md.nasdaq_close:,.2f}</div>
        <div class="chg" style="color:{_sign_color(md.nasdaq_change_pct)}">{md.nasdaq_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">Dow Jones</div>
        <div class="val">{md.dow_close:,.2f}</div>
        <div class="chg" style="color:{_sign_color(md.dow_change_pct)}">{md.dow_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">VIX</div>
        <div class="val">{md.vix:.2f}</div>
        <div class="chg" style="color:#888">공포지수</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">USD/KRW</div>
        <div class="val">{md.usdkrw:,.1f}</div>
        <div class="chg" style="color:{_sign_color(md.usdkrw_change_pct, invert=True)}">{md.usdkrw_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">금 (Gold)</div>
        <div class="val">${md.gold_price:,.0f}</div>
        <div class="chg" style="color:{_sign_color(md.gold_change_pct)}">{md.gold_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">WTI 유가</div>
        <div class="val">${md.wti_price:.1f}</div>
        <div class="chg" style="color:{_sign_color(md.wti_change_pct)}">{md.wti_change_pct:+.2f}%</div>
      </div>
      <div class="mkt-card">
        <div class="lbl">미국채 10Y</div>
        <div class="val">{md.us10y:.3f}%</div>
        <div class="chg" style="color:#888">2Y: {md.us2y:.3f}%</div>
      </div>
    </div>
  </div>

  {'<div class="sec"><div class="sec-title">🏭 업종별 등락률</div><div class="chart-box"><canvas id="sectorChart" height="100"></canvas></div></div>' if sec_sorted else ''}

  <!-- Phase 1 -->
  <div class="sec">
    <div class="sec-title">🤖 에이전트 독립 분석 <span class="badge">Phase 1</span></div>
    <div class="outlook-pills">
      <div class="pill" style="background:#00c85322;color:#00c853">🟢 상승 {outlook_counts['상승']}개</div>
      <div class="pill" style="background:#ffd60022;color:#ffd600">🟡 중립 {outlook_counts['중립']}개</div>
      <div class="pill" style="background:#ff174422;color:#ff1744">🔴 하락 {outlook_counts['하락']}개</div>
    </div>
    <div class="agents-grid">{agent_cards_html}</div>
  </div>

  <!-- Phase 2 -->
  <div class="sec">
    <div class="sec-title">⚔️ 교차 반론 <span class="badge">Phase 2</span></div>
    {rebuttal_html}
  </div>

  <!-- Phase 3 -->
  <div class="sec">
    <div class="sec-title">🎯 모더레이터 최종 종합 <span class="badge">Phase 3</span></div>
    <div class="synth-box">
      <div class="synth-verdict">
        <div>
          <div class="verdict-lbl">최종 전망</div>
          <div class="verdict-val">{final_emoji} {final_outlook}</div>
        </div>
      </div>
      <div class="synth-body">{synthesis_html}</div>
    </div>
  </div>

</div>
{chart_js}
<div class="footer">
  KOSPI 모닝브리핑 멀티에이전트 시스템 | {result.timestamp}<br>
  ⚠️ 본 리포트는 AI 분석으로 투자 조언이 아닙니다
</div>
</body>
</html>"""
    return html


# ============================================================
# Telegram 메시지 생성
# ============================================================
def generate_telegram_messages(result: DiscussionResult, report_url: Optional[str] = None) -> List[str]:
    md = result.market_data
    counts = {"상승": 0, "중립": 0, "하락": 0}
    for a in result.phase1_analyses.values():
        if a.outlook in counts:
            counts[a.outlook] += 1

    final_outlook = "중립"
    if "## 최종 전망: 상승" in result.phase3_synthesis:
        final_outlook = "상승"
    elif "## 최종 전망: 하락" in result.phase3_synthesis:
        final_outlook = "하락"

    arrow_kospi = "▲" if md.kospi_change_pct >= 0 else "▼"
    msg1 = (
        f"📊 <b>KOSPI 모닝브리핑</b>  {md.date}\n\n"
        f"<b>전일 KOSPI</b>: {md.kospi_current:,.2f} {arrow_kospi} {md.kospi_change_pct:+.2f}%\n"
        f"<b>S&amp;P500</b>: {md.sp500_close:,.2f} ({md.sp500_change_pct:+.2f}%)\n"
        f"<b>NASDAQ</b>: {md.nasdaq_close:,.2f} ({md.nasdaq_change_pct:+.2f}%)\n"
        f"<b>Dow</b>: {md.dow_close:,.2f} ({md.dow_change_pct:+.2f}%)\n"
        f"<b>VIX</b>: {md.vix:.2f} | <b>USD/KRW</b>: {md.usdkrw:,.1f}\n"
        f"<b>금</b>: ${md.gold_price:,.0f} ({md.gold_change_pct:+.2f}%) | "
        f"<b>WTI</b>: ${md.wti_price:.1f} ({md.wti_change_pct:+.2f}%)\n"
        f"<b>미국채 10Y</b>: {md.us10y:.3f}%\n\n"
        f"🤖 <b>에이전트 전망 집계</b>\n"
        f"🟢 상승 {counts['상승']}개 | 🟡 중립 {counts['중립']}개 | 🔴 하락 {counts['하락']}개"
    )

    agent_lines = "\n\n📌 <b>에이전트 핵심 포인트</b>"
    for name, a in result.phase1_analyses.items():
        pts = "\n".join(f"  • {p[:80]}" for p in a.key_points[:2])
        agent_lines += f"\n{_outlook_emoji(a.outlook)} <b>{name}</b> ({a.outlook})"
        if pts:
            agent_lines += f"\n{pts}"
    msg2 = agent_lines

    fe = _outlook_emoji(final_outlook)
    synth_short = result.phase3_synthesis[:700]
    msg3 = (
        f"{fe} <b>최종 판단: {final_outlook}</b>\n\n"
        f"{synth_short}\n\n"
        f"⏱ 소요: {result.total_duration_sec:.0f}초 | ⚠️ 투자 조언 아님"
    )

    msgs = [msg1, msg2, msg3]
    if report_url:
        msgs.append(f"📄 <b>전체 HTML 리포트</b>\n{report_url}")
    return msgs


# ============================================================
# GitHub Gist 업로드
# ============================================================
def upload_to_gist(html: str, github_pat: str, date_str: str) -> Optional[str]:
    if not github_pat:
        logger.warning("GITHUB_PAT 없음 — Gist 업로드 스킵")
        return None
    headers = {
        "Authorization": f"token {github_pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "kospi-morning-briefing",
    }
    filename = f"kospi_briefing_{date_str}.html"
    payload = {
        "description": f"KOSPI 모닝브리핑 - {date_str}",
        "public": True,
        "files": {filename: {"content": html}},
    }
    try:
        # 기존 Gist 검색
        existing_id = None
        list_resp = requests.get("https://api.github.com/gists", headers=headers, timeout=15)
        if list_resp.ok:
            for g in list_resp.json():
                if "KOSPI 모닝브리핑" in g.get("description", ""):
                    existing_id = g["id"]
                    break
        if existing_id:
            resp = requests.patch(f"https://api.github.com/gists/{existing_id}", headers=headers, json=payload, timeout=30)
        else:
            resp = requests.post("https://api.github.com/gists", headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        gist_data = resp.json()
        raw_url = gist_data["files"][filename]["raw_url"]
        preview = f"https://htmlpreview.github.io/?{raw_url}"
        logger.info(f"Gist 업로드 완료: {preview}")
        return preview
    except Exception as e:
        logger.error(f"Gist 업로드 실패: {e}")
        return None


# ============================================================
# Telegram 발송
# ============================================================
def send_telegram(messages: List[str], token: str, chat_id: str) -> None:
    if not token or not chat_id:
        logger.warning("Telegram 설정 없음 — 발송 스킵")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, msg in enumerate(messages, 1):
        try:
            r = requests.post(url, json={
                "chat_id": chat_id, "text": msg[:4096], "parse_mode": "HTML"
            }, timeout=20)
            r.raise_for_status()
            logger.info(f"Telegram {i}/{len(messages)} 발송 완료")
        except Exception as e:
            logger.error(f"Telegram 발송 실패 ({i}/{len(messages)}): {e}")


# ============================================================
# 메인
# ============================================================
async def main_async() -> None:
    config = load_config()

    if not config.get("anthropic_api_key"):
        msg = "❌ KOSPI 모닝브리핑 실패: ANTHROPIC_API_KEY가 설정되지 않았습니다."
        logger.error(msg)
        send_telegram([msg], config.get("telegram_token", ""), config.get("telegram_chat_id", ""))
        return

    kst = pytz.timezone("Asia/Seoul")
    today = datetime.now(kst).strftime("%Y-%m-%d")
    logger.info(f"KOSPI 모닝브리핑 시작: {today}")

    try:
        # 1. 데이터 수집
        market_data = MarketDataCollector().collect_all()

        # 2. 토론 실행
        result = await DiscussionOrchestrator(config).run(market_data)

        # 3. HTML 리포트 생성 & 저장
        html = generate_html_report(result)
        report_path = f"report_{today}.html"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"HTML 저장: {report_path}")

        # 4. Gist 업로드
        gist_url = upload_to_gist(html, config.get("github_pat", ""), today)

        # 5. Telegram 발송
        msgs = generate_telegram_messages(result, report_url=gist_url)
        send_telegram(msgs, config["telegram_token"], config["telegram_chat_id"])

        logger.info("KOSPI 모닝브리핑 완료!")

    except Exception as e:
        tb = traceback.format_exc()
        err_msg = f"❌ KOSPI 모닝브리핑 실패\n{str(e)[:300]}\n\n{tb[:400]}"
        logger.error(err_msg)
        send_telegram([err_msg], config.get("telegram_token", ""), config.get("telegram_chat_id", ""))
        raise


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
