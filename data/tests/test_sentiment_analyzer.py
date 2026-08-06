"""
Tests for Macro News Sentiment Analyzer.
"""
from data.sentiment_analyzer import MacroNewsSentimentAnalyzer


def test_sentiment_gold_bullish():
    analyzer = MacroNewsSentimentAnalyzer()
    res = analyzer.analyze_headline("Fed signals rate cut as recession fears mount and safe haven demand rises")
    assert res["bias"] == "bullish"
    assert res["score"] > 0.3
    assert res["confidence"] > 0.5


def test_sentiment_gold_bearish():
    analyzer = MacroNewsSentimentAnalyzer()
    res = analyzer.analyze_headline("Strong jobs report: NFP beats expectations, dollar surges on hawkish Fed outlook")
    assert res["bias"] == "bearish"
    assert res["score"] < -0.3
    assert res["confidence"] > 0.5


def test_sentiment_neutral():
    analyzer = MacroNewsSentimentAnalyzer()
    res = analyzer.analyze_headline("Markets open quietly ahead of tomorrow's session")
    assert res["bias"] == "neutral"
    assert res["score"] == 0.0


def test_score_batch():
    analyzer = MacroNewsSentimentAnalyzer()
    headlines = [
        "Fed signals rate cut as safe haven demand surges",
        "Gold rallies on geopolitical risk",
    ]
    avg_score = analyzer.score_batch(headlines)
    assert avg_score > 0.4
