"""Deterministic and LLM-assisted screening for Telegram Business messages.

The customer message is untrusted input.  This module only produces a
structured assessment; the manager decides whether to hold, block, or draft.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional


_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class ScreeningResult:
    category: str = "benign"
    severity: str = "low"
    risk_score: float = 0.0
    confidence: float = 1.0
    reasons: tuple[str, ...] = field(default_factory=tuple)
    indicators: tuple[str, ...] = field(default_factory=tuple)
    rule_hits: tuple[str, ...] = field(default_factory=tuple)
    action: str = "allow"  # allow | review | hold | temp_block
    source: str = "rules"

    @property
    def suspicious(self) -> bool:
        return self.action != "allow"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "risk_score": self.risk_score,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "indicators": list(self.indicators),
            "rule_hits": list(self.rule_hits),
            "action": self.action,
            "source": self.source,
        }


def normalize_text(text: str) -> str:
    """Normalize common obfuscation without changing the stored message."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _ZERO_WIDTH.sub("", value).lower()
    return re.sub(r"\s+", " ", value).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def scan_keywords(text: str) -> ScreeningResult:
    """Run high-precision rules before any model call."""
    normalized = normalize_text(text)
    hits: List[str] = []
    reasons: List[str] = []
    indicators: List[str] = []
    category = "benign"
    score = 0.0
    hard = False

    rules = (
        (
            "phishing_login",
            "phishing",
            0.95,
            True,
            r"(?:verify|confirm|unlock|suspend|login|sign.?in|验证|登录|解冻).{0,80}(?:account|wallet|账户|钱包|账号)",
            "要求验证或恢复账户",
            "login_request",
        ),
        (
            "secret_request",
            "scam",
            0.98,
            True,
            r"(?:otp|one.?time|verification code|password|seed phrase|private key|验证码|密码|助记词|私钥)",
            "涉及验证码、密码或密钥",
            "credential_request",
        ),
        (
            "urgent_payment",
            "scam",
            0.90,
            True,
            r"(?:pay|payment|transfer|deposit|fee|付款|转账|汇款|保证金|预付款).{0,80}(?:urgent|immediately|today|马上|立即|紧急)",
            "以紧迫理由要求付款",
            "urgent_payment",
        ),
        (
            "guaranteed_return",
            "scam",
            0.86,
            True,
            r"(?:guaranteed|risk.?free|double your|稳赚|保本|高收益|翻倍).{0,60}(?:investment|crypto|投资|加密|理财)",
            "承诺无风险或保证收益",
            "investment_pitch",
        ),
        (
            "suspicious_url",
            "phishing",
            0.78,
            False,
            r"(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|is\.gd|cutt\.ly|登录|验证).{0,100}(?:https?://|www\.)",
            "包含短链接或可疑验证链接",
            "suspicious_url",
        ),
        (
            "unsolicited_ad",
            "advertising",
            0.62,
            False,
            r"(?:buy now|limited offer|discount|promo|affiliate|推广|优惠|折扣|代理|加盟|招聘)",
            "疑似未经请求的推广内容",
            "promotion",
        ),
    )

    for code, rule_category, rule_score, is_hard, pattern, reason, indicator in rules:
        if re.search(pattern, normalized, re.IGNORECASE):
            hits.append(code)
            reasons.append(reason)
            indicators.append(indicator)
            if rule_score >= score:
                category = rule_category
                score = rule_score
            hard = hard or is_hard

    urls = _URL.findall(normalized)
    if len(urls) >= 3:
        hits.append("many_urls")
        reasons.append("包含多个外部链接")
        indicators.append("url_density")
        if score < 0.65:
            category = "spam"
            score = 0.65

    if not hits:
        return ScreeningResult()

    severity = "high" if score >= 0.8 else "medium"
    action = "hold" if hard else "review"
    return ScreeningResult(
        category=category,
        severity=severity,
        risk_score=score,
        confidence=1.0 if hard else 0.55,
        reasons=tuple(dict.fromkeys(reasons))[:4],
        indicators=tuple(dict.fromkeys(indicators))[:6],
        rule_hits=tuple(dict.fromkeys(hits))[:8],
        action=action,
        source="rules",
    )


def parse_llm_result(raw: Any) -> Mapping[str, Any]:
    """Parse and validate the small JSON contract returned by the classifier."""
    if isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        value = str(getattr(raw, "text", raw) or "").strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
        payload = json.loads(value)
    if not isinstance(payload, Mapping):
        raise ValueError("screening result must be a JSON object")
    return payload


def combine_results(
    rules: ScreeningResult,
    raw_llm: Any,
    *,
    risk_threshold: float = 0.65,
    confidence_threshold: float = 0.60,
) -> ScreeningResult:
    """Merge model output with rules; local policy owns the final action."""
    payload = parse_llm_result(raw_llm)
    allowed_categories = {"benign", "scam", "phishing", "advertising", "spam", "other"}
    category = str(payload.get("category") or "other").lower()
    if category not in allowed_categories:
        category = "other"
    severity = str(payload.get("severity") or "low").lower()
    if severity not in {"low", "medium", "high"}:
        severity = "low"

    def _number(name: str, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(payload.get(name, default))))
        except (TypeError, ValueError):
            return default

    score = _number("risk_score", 0.0)
    confidence = _number("confidence", 0.0)
    reasons = _string_list(payload.get("reasons"))[:4]
    indicators = _string_list(payload.get("indicators"))[:6]
    all_reasons = tuple(dict.fromkeys((*rules.reasons, *reasons)))[:4]
    all_indicators = tuple(dict.fromkeys((*rules.indicators, *indicators)))[:6]
    all_hits = rules.rule_hits
    hard = any(
        hit in {"phishing_login", "secret_request", "urgent_payment", "guaranteed_return"}
        for hit in all_hits
    )

    if hard or rules.risk_score >= score:
        category = rules.category
    score = max(score, rules.risk_score)
    confidence = max(confidence, rules.confidence if rules.rule_hits else 0.0)
    if category == "benign" and score >= risk_threshold:
        category = "other"
    suspicious = category != "benign" and score >= risk_threshold and confidence >= confidence_threshold
    if hard or suspicious:
        action = "hold"
    elif category != "benign" or rules.action == "review":
        action = "review"
    else:
        action = "allow"

    return ScreeningResult(
        category=category,
        severity="high" if severity == "high" or score >= 0.8 else ("medium" if score >= 0.5 else "low"),
        risk_score=score,
        confidence=confidence,
        reasons=all_reasons,
        indicators=all_indicators,
        rule_hits=all_hits,
        action=action,
        source="rules+llm",
    )


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value[:160]] if value else []
    if not isinstance(value, Iterable):
        return []
    return [str(item)[:160] for item in value if str(item).strip()]
