"""
Macroeconomic News & Financial Sentiment Analyzer.
Analyzes economic statements, central bank decisions (Fed/ECB/BOE),
inflation reports (CPI/PCE), and employment data (NFP) to produce
causal numeric sentiment scores [-1.0, +1.0] for Gold and FX assets.
"""
from __future__ import annotations

from typing import Any, Dict, List


class MacroNewsSentimentAnalyzer:
    """
    Rule-based and lexicon-driven financial sentiment analyzer.
    Scores headlines and macro events specifically for Gold (XAUUSD) and USD impact.
    """

    # Terms bullish for Gold / bearish for USD
    GOLD_BULLISH_TERMS = {
        "rate cut": 0.8,
        "dovish": 0.7,
        "easing": 0.6,
        "inflation rising": 0.7,
        "higher cpi": 0.6,
        "geopolitical risk": 0.8,
        "safe haven": 0.9,
        "dollar drops": 0.7,
        "recession fears": 0.8,
        "unemployment rises": 0.6,
        "war": 0.8,
        "tensions": 0.6,
        "lower yields": 0.7,
    }

    # Terms bearish for Gold / bullish for USD
    GOLD_BEARISH_TERMS = {
        "rate hike": 0.8,
        "hawkish": 0.7,
        "tightening": 0.6,
        "inflation cool": 0.6,
        "lower cpi": 0.6,
        "strong jobs": 0.7,
        "nfp beats": 0.8,
        "dollar surges": 0.8,
        "higher yields": 0.7,
        "peace talks": 0.6,
        "risk on": 0.7,
        "economic growth": 0.5,
    }

    def analyze_headline(self, text: str) -> Dict[str, Any]:
        """
        Analyzes a headline and returns sentiment score [-1.0, +1.0],
        gold impact bias ('bullish', 'bearish', 'neutral'), and confidence.
        """
        clean = text.lower()
        bull_score = 0.0
        bear_score = 0.0
        matched_terms = []

        for term, weight in self.GOLD_BULLISH_TERMS.items():
            if term in clean:
                bull_score += weight
                matched_terms.append(f"+{term}")

        for term, weight in self.GOLD_BEARISH_TERMS.items():
            if term in clean:
                bear_score += weight
                matched_terms.append(f"-{term}")

        total_weight = bull_score + bear_score
        if total_weight == 0:
            return {
                "score": 0.0,
                "bias": "neutral",
                "confidence": 0.0,
                "matched_terms": [],
            }

        net_score = (bull_score - bear_score) / max(total_weight, 1.0)
        confidence = min(total_weight / 2.0, 1.0)

        if net_score > 0.15:
            bias = "bullish"
        elif net_score < -0.15:
            bias = "bearish"
        else:
            bias = "neutral"

        return {
            "score": float(np_clip(net_score, -1.0, 1.0)),
            "bias": bias,
            "confidence": float(confidence),
            "matched_terms": matched_terms,
        }

    def score_batch(self, headlines: List[str]) -> float:
        """Computes aggregate sentiment score for a list of recent headlines."""
        if not headlines:
            return 0.0
        scores = [self.analyze_headline(h)["score"] for h in headlines]
        return float(sum(scores) / len(scores))

    def red_zone_event_sentiment(self, current_ts_utc: int = None,
                                 buffer_before_minutes: int = 30,
                                 buffer_after_minutes: int = 30,
                                 assets: tuple = ("USD", "ALL")) -> dict:
        """Score the sentiment of any High-Impact event currently inside the
        news red-zone buffer window (W17: wires this module into the live trading
        path as an optional sentiment veto).

        Returns {"score": float, "bias": str, "title": str, "in_red_zone": bool}.
        The event title is scored with the same lexicon as analyze_headline.
        Fails open (neutral) on any network/parse error so a broken feed never
        hard-blocks trading.
        """
        try:

            from data.news_filter import fetch_economic_calendar
        except Exception:
            return {"score": 0.0, "bias": "neutral", "title": "", "in_red_zone": False}
        if not current_ts_utc:
            return {"score": 0.0, "bias": "neutral", "title": "", "in_red_zone": False}
        try:
            events = fetch_economic_calendar() or []
        except Exception:
            return {"score": 0.0, "bias": "neutral", "title": "", "in_red_zone": False}

        buf_before = buffer_before_minutes * 60
        buf_after = buffer_after_minutes * 60
        for event in events:
            if event.get("country", "ALL") not in assets:
                continue
            news_ts = event.get("timestamp_utc")
            if news_ts is None:
                continue
            if (news_ts - buf_before) <= current_ts_utc <= (news_ts + buf_after):
                title = event.get("title", "")
                res = self.analyze_headline(title)
                return {"score": res["score"], "bias": res["bias"], "title": title,
                        "in_red_zone": True}
        return {"score": 0.0, "bias": "neutral", "title": "", "in_red_zone": False}


def np_clip(val: float, low: float, high: float) -> float:
    return max(min(val, high), low)
