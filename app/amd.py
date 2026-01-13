from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# -----------------------------
# V1 RULE-BASED AMD (EN only)
# -----------------------------

AMD_DECISION_VOICEMAIL = "VOICEMAIL"
AMD_DECISION_HUMAN = "HUMAN"
AMD_DECISION_UNKNOWN = "UNKNOWN"

_KEYWORDS_V1 = [
    "leave a message",
    "after the tone",
    "you have reached",
    "not available",
    "cannot take your call",
    "mailbox",
]

amd_logger = logging.getLogger("sales_dialer.amd")


def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    # Keep it simple/deterministic: replace non-alnum with spaces, collapse whitespace.
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _word_count(s: str) -> int:
    s = _normalize_text(s)
    if not s:
        return 0
    return len(s.split(" "))


@dataclass
class SpeechSegment:
    startMs: int
    endMs: int

    @property
    def durationMs(self) -> int:
        return max(0, int(self.endMs) - int(self.startMs))


@dataclass
class AmdState:
    # Required per spec
    startTimeMs: int
    accumulatedText: str = ""
    speechSegments: list[SpeechSegment] = field(default_factory=list)
    totalSpeakingTimeMs: int = 0
    longestMonologueMs: int = 0
    interruptionCount: int = 0
    firstSpeechStartMs: Optional[int] = None
    firstContinuousSpeechDurationMs: int = 0
    keywordHits: list[str] = field(default_factory=list)
    hasKeyword: bool = False
    beepDetected: bool = False
    humanBias: bool = False
    decision: Optional[str] = None
    confidence: float = 0.0
    decided: bool = False
    decisionRule: Optional[str] = None

    # Internal bookkeeping (deterministic, per-call)
    # Internal bookkeeping (not part of public state)
    startMonotonic: float = field(default_factory=time.monotonic, repr=False)
    decisionTimeMsInternal: Optional[int] = field(default=None, repr=False)
    interimUnknownTimeMsInternal: Optional[int] = field(default=None, repr=False)
    lastObservedText: str = field(default="", repr=False)
    lastObservedIsFinal: bool = field(default=False, repr=False)
    keywordsHitSet: set[str] = field(default_factory=set, repr=False)
    lastWordEndMsSeen: int = field(default=-1, repr=False)
    timerTask: Optional[asyncio.Task[None]] = field(default=None, repr=False)
    lastUpdateMonotonic: float = field(default_factory=time.monotonic, repr=False)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.startMonotonic) * 1000)

    def decision_time_ms(self) -> Optional[int]:
        return self.decisionTimeMsInternal


class AmdEngine:
    """
    STRICT rule-based AMD engine.

    Safety priority:
    - UNKNOWN is acceptable.
    - Avoid HUMAN -> VOICEMAIL false positives.
    """

    def __init__(self, *, log_path: str = "amd_decisions.jsonl"):
        self._states: dict[str, AmdState] = {}
        self._lock = asyncio.Lock()
        self._log_path = log_path
        self._log_lock = asyncio.Lock()

    async def start_call(self, call_id: str) -> None:
        if not call_id:
            return
        async with self._lock:
            if call_id in self._states:
                return
            st = AmdState(startTimeMs=int(time.time() * 1000))
            # Per-call timers: ensure 3s bias evaluation + 10s hard stop even if no transcripts arrive.
            st.timerTask = asyncio.create_task(self._timer_loop(call_id))
            self._states[call_id] = st

    async def end_call(self, call_id: str) -> None:
        if not call_id:
            return
        st: Optional[AmdState] = None
        async with self._lock:
            st = self._states.get(call_id)
        if not st:
            return

        # If call ends early without a decision, finalize as UNKNOWN (conservative).
        should_log = False
        st_for_log: Optional[AmdState] = None
        task: Optional[asyncio.Task[None]] = None
        async with self._lock:
            st2 = self._states.get(call_id)
            if not st2:
                return
            if not st2.decided:
                self._decide(st2, AMD_DECISION_UNKNOWN, 0.30, rule="CALL_END_NO_DECISION")
                should_log = True
                st_for_log = st2

            task = st2.timerTask
            st2.timerTask = None
            self._states.pop(call_id, None)

        if should_log and st_for_log is not None:
            await self._log_decision(call_id, st_for_log)

        if task:
            task.cancel()

    async def mark_beep(self, call_id: str) -> None:
        """
        External hook: if any upstream component detects a voicemail beep, mark it.
        """
        if not call_id:
            return
        await self.start_call(call_id)
        should_log = False
        st_for_log: Optional[AmdState] = None
        async with self._lock:
            st = self._states.get(call_id)
            if not st or st.decided:
                return
            st.beepDetected = True
            self._evaluate_rules(call_id, st)
            if st.decided:
                should_log = True
                st_for_log = st

        if should_log and st_for_log is not None:
            await self._log_decision(call_id, st_for_log)

    async def process_transcript(
        self,
        *,
        call_id: str,
        text: str,
        timestamp_ms: int,
        duration_ms: int,
        is_final: bool,
        word_items: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Process one streaming transcription chunk.

        Inputs align to user spec (call_id, text, timestamp_ms, duration_ms).
        word_items (Deepgram) is used for accurate speech-segment timing without double counting.
        """
        if not call_id:
            return None

        await self.start_call(call_id)

        should_log = False
        st_for_log: Optional[AmdState] = None
        decided_payload: Optional[dict[str, Any]] = None
        async with self._lock:
            st = self._states.get(call_id)
            if not st:
                return None
            if st.decided:
                return {"decision": st.decision, "confidence": st.confidence}

            st.lastUpdateMonotonic = time.monotonic()
            if text:
                st.lastObservedText = text
                st.lastObservedIsFinal = bool(is_final)

            # Update accumulated text (conservative: FINAL only for keyword signal stability).
            if is_final and text:
                if st.accumulatedText:
                    st.accumulatedText = f"{st.accumulatedText} {text}"
                else:
                    st.accumulatedText = text
                self._update_keywords(st)

            # Update speech segments from word timing when available.
            spans = self._extract_word_spans_ms(word_items)
            if spans:
                self._apply_word_spans(st, spans)
            else:
                # Fallback (no word timings): only estimate when FINAL to avoid interim double counting.
                if is_final and duration_ms > 0:
                    end_ms = max(0, int(timestamp_ms))
                    start_ms = max(0, end_ms - int(duration_ms))
                    self._apply_word_spans(st, [(start_ms, end_ms)])

            # Evaluate strict rules in order.
            self._evaluate_rules(call_id, st)
            if st.decided:
                should_log = True
                st_for_log = st
                decided_payload = {"decision": st.decision, "confidence": st.confidence}
                # Do NOT return while holding the lock; log outside for safety.

            # If not decided, fall through and return None outside the lock.

        if should_log and st_for_log is not None:
            await self._log_decision(call_id, st_for_log)
        # If we don't have a final decision yet but we have an interim UNKNOWN
        # (e.g. after 3 seconds), expose it to callers.
        if decided_payload is None:
            async with self._lock:
                st2 = self._states.get(call_id)
                if st2 and (not st2.decided) and st2.decision == AMD_DECISION_UNKNOWN:
                    return {"decision": st2.decision, "confidence": st2.confidence, "decided": False}
        return decided_payload

    async def get_state(self, call_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            st = self._states.get(call_id)
            if not st:
                return None
            return self._public_state(st)

    # -----------------------------
    # Internals
    # -----------------------------

    async def _timer_loop(self, call_id: str) -> None:
        """
        Ensures rule 7 triggers at ~3s and hard-stop at 10s even without transcript messages.
        """
        try:
            # Wake at 3 seconds
            await asyncio.sleep(3.0)
            should_log = False
            st_for_log: Optional[AmdState] = None
            async with self._lock:
                st = self._states.get(call_id)
                if st and not st.decided:
                    self._evaluate_rules(call_id, st)
                    if st.decided:
                        should_log = True
                        st_for_log = st
            if should_log and st_for_log is not None:
                await self._log_decision(call_id, st_for_log)

            # Wake at 10 seconds
            await asyncio.sleep(7.0)
            should_log = False
            st_for_log = None
            async with self._lock:
                st = self._states.get(call_id)
                if st and not st.decided:
                    self._evaluate_rules(call_id, st)
                    if st.decided:
                        should_log = True
                        st_for_log = st
            if should_log and st_for_log is not None:
                await self._log_decision(call_id, st_for_log)
        except asyncio.CancelledError:
            return

    def _public_state(self, st: AmdState) -> dict[str, Any]:
        return {
            "startTimeMs": st.startTimeMs,
            "accumulatedText": st.accumulatedText,
            "speechSegments": [{"startMs": s.startMs, "endMs": s.endMs} for s in st.speechSegments],
            "totalSpeakingTimeMs": st.totalSpeakingTimeMs,
            "longestMonologueMs": st.longestMonologueMs,
            "interruptionCount": st.interruptionCount,
            "firstSpeechStartMs": st.firstSpeechStartMs,
            "firstContinuousSpeechDurationMs": st.firstContinuousSpeechDurationMs,
            "keywordHits": list(st.keywordHits),
            "hasKeyword": st.hasKeyword,
            "beepDetected": st.beepDetected,
            "humanBias": st.humanBias,
            "decision": st.decision,
            "confidence": st.confidence,
            "decided": st.decided,
            "decisionRule": st.decisionRule,
            "lastObservedText": st.lastObservedText,
            "lastObservedIsFinal": st.lastObservedIsFinal,
        }

    def _extract_word_spans_ms(
        self, word_items: Optional[list[dict[str, Any]]]
    ) -> list[tuple[int, int]]:
        if not word_items:
            return []
        spans: list[tuple[int, int]] = []
        for w in word_items:
            try:
                start_s = w.get("start")
                end_s = w.get("end")
                if start_s is None or end_s is None:
                    continue
                start_ms = int(float(start_s) * 1000)
                end_ms = int(float(end_s) * 1000)
                if end_ms <= start_ms:
                    continue
                spans.append((start_ms, end_ms))
            except (TypeError, ValueError):
                continue
        spans.sort(key=lambda x: (x[0], x[1]))
        return spans

    def _apply_word_spans(self, st: AmdState, spans_ms: list[tuple[int, int]]) -> None:
        # Use incremental spans to avoid double counting interim repeats.
        for start_ms, end_ms in spans_ms:
            if end_ms <= st.lastWordEndMsSeen:
                continue
            # Clamp start to last end if the span overlaps already-seen audio.
            if start_ms < st.lastWordEndMsSeen:
                start_ms = st.lastWordEndMsSeen
            st.lastWordEndMsSeen = max(st.lastWordEndMsSeen, end_ms)

            if not st.speechSegments:
                st.speechSegments.append(SpeechSegment(startMs=start_ms, endMs=end_ms))
            else:
                last = st.speechSegments[-1]
                if start_ms - last.endMs > 300:
                    # New segment after a silence gap > 300ms.
                    st.speechSegments.append(SpeechSegment(startMs=start_ms, endMs=end_ms))
                else:
                    last.endMs = max(last.endMs, end_ms)

        # Recompute derived fields (segments list is tiny; keep it deterministic and simple).
        st.totalSpeakingTimeMs = sum(seg.durationMs for seg in st.speechSegments)
        st.longestMonologueMs = max((seg.durationMs for seg in st.speechSegments), default=0)

        # interruptionCount is "number of interruptions" (segment breaks), not "number of segments".
        st.interruptionCount = max(0, len(st.speechSegments) - 1)

        if st.speechSegments:
            first = st.speechSegments[0]
            st.firstSpeechStartMs = first.startMs
            st.firstContinuousSpeechDurationMs = first.durationMs

    def _update_keywords(self, st: AmdState) -> None:
        hay = _normalize_text(st.accumulatedText)
        if not hay:
            return
        for kw in _KEYWORDS_V1:
            if kw in st.keywordsHitSet:
                continue
            if kw in hay:
                st.keywordsHitSet.add(kw)
                st.keywordHits.append(kw)
                st.hasKeyword = True

    def _decide(self, st: AmdState, decision: str, confidence: float, *, rule: str) -> None:
        if st.decided:
            return
        st.decided = True
        st.decision = decision
        st.confidence = float(confidence)
        st.decisionTimeMsInternal = st.elapsed_ms()
        st.decisionRule = rule

    def _set_interim_unknown(self, call_id: str, st: AmdState) -> None:
        """
        After ~3 seconds, expose UNKNOWN as the *current* classification if we still
        have no decisive evidence. This does NOT stop processing and is not logged
        as a final decision.
        """
        if st.decided:
            return
        if st.decision is None:
            st.decision = AMD_DECISION_UNKNOWN
            st.confidence = 0.30
            st.interimUnknownTimeMsInternal = st.elapsed_ms()
            # Visibility: log once when we enter interim UNKNOWN.
            amd_logger.info(
                "AMD interim call_id=%s decision=%s confidence=%.2f timeMs=%s",
                call_id,
                st.decision,
                st.confidence,
                st.interimUnknownTimeMsInternal,
            )

    def _evaluate_rules(self, call_id: str, st: AmdState) -> None:
        _ = call_id  # reserved (helps future per-call rule overrides); avoids unused-arg lint
        # Once decided → STOP processing.
        if st.decided:
            return

        elapsed = st.elapsed_ms()
        wordCount = _word_count(st.accumulatedText)

        # RULE 0 — HARD TIME LIMIT
        if elapsed >= 10_000:
            self._decide(st, AMD_DECISION_UNKNOWN, 0.30, rule="RULE_0_HARD_TIME_LIMIT")
            return

        # RULE 1 — BEEP (DEFINITIVE)
        if st.beepDetected is True:
            self._decide(st, AMD_DECISION_VOICEMAIL, 1.00, rule="RULE_1_BEEP")
            return

        # RULE 2 — STRONG VOICEMAIL KEYWORDS
        if st.hasKeyword is True:
            self._decide(st, AMD_DECISION_VOICEMAIL, 0.95, rule="RULE_2_STRONG_VOICEMAIL_KEYWORDS")
            return

        # RULE 3 — IMMEDIATE + CONTINUOUS SPEECH
        if (
            st.firstSpeechStartMs is not None
            and st.firstSpeechStartMs <= 400
            and st.firstContinuousSpeechDurationMs >= 1500
        ):
            self._decide(st, AMD_DECISION_VOICEMAIL, 0.92, rule="RULE_3_IMMEDIATE_CONTINUOUS_SPEECH")
            return

        # RULE 4 — LONG UNINTERRUPTED MONOLOGUE
        if st.longestMonologueMs >= 2200 and st.interruptionCount == 0:
            self._decide(st, AMD_DECISION_VOICEMAIL, 0.90, rule="RULE_4_LONG_UNINTERRUPTED_MONOLOGUE")
            return

        # RULE 5 — STRONG HUMAN GREETING
        if st.totalSpeakingTimeMs <= 1800 and wordCount <= 5 and st.interruptionCount >= 1:
            self._decide(st, AMD_DECISION_HUMAN, 0.90, rule="RULE_5_STRONG_HUMAN_GREETING")
            return

        # RULE 6 — DELAYED FIRST SPEECH (HUMAN BIAS)
        if elapsed >= 3000 and st.totalSpeakingTimeMs == 0:
            st.humanBias = True
            # DO NOT decide yet

        # RULE 7 — 3-SECOND BIAS DECISION
        if elapsed >= 3000:
            # 7A — Voicemail leaning
            immediate_partial = (
                st.firstSpeechStartMs is not None
                and st.firstSpeechStartMs <= 400
                and st.firstContinuousSpeechDurationMs >= 1000
            )
            if st.longestMonologueMs >= 2000 or immediate_partial:
                self._decide(st, AMD_DECISION_VOICEMAIL, 0.70, rule="RULE_7A_VOICEMAIL_LEANING")
                return

            # 7B — Human leaning
            # Safety: do NOT decide HUMAN from "no speech yet" (humanBias) alone.
            # This avoids incorrectly marking HUMAN when we have zero evidence.
            if st.interruptionCount >= 1:
                self._decide(st, AMD_DECISION_HUMAN, 0.70, rule="RULE_7B_HUMAN_LEANING")
                return

            # If we reached 3 seconds with no decision, report current state as UNKNOWN
            # (but keep processing until we see real evidence or hit 10 seconds).
            self._set_interim_unknown(call_id, st)

        # RULE 8 — FINAL HARD STOP (redundant with rule 0; kept for spec parity)
        if elapsed >= 10_000 and not st.decided:
            self._decide(st, AMD_DECISION_UNKNOWN, 0.30, rule="RULE_8_FINAL_HARD_STOP")

    async def _log_decision(self, call_id: str, st: AmdState) -> None:
        # Idempotent logging: only log on first transition to decided.
        if st.decision_time_ms() is None:
            return
        # Best-effort text snapshot for future ML:
        # - Prefer concatenated FINAL chunks (accumulatedText)
        # - If we decided before a FINAL arrives, include latest observed interim as context
        text_at_decision = (st.accumulatedText or "").strip()
        last_obs = (st.lastObservedText or "").strip()
        if last_obs and (last_obs not in text_at_decision):
            text_at_decision = (f"{text_at_decision} {last_obs}").strip() if text_at_decision else last_obs
        # Keep bounded/deterministic (10s audio shouldn't be huge, but cap anyway)
        if len(text_at_decision) > 2000:
            text_at_decision = text_at_decision[:2000]

        record = {
            "amdVersion": "v1",
            "call_id": call_id,
            "decision": st.decision,
            "confidence": st.confidence,
            "rule": st.decisionRule,
            "decisionTimeMs": st.decision_time_ms(),
            # Required / core metrics
            "firstSpeechStartMs": st.firstSpeechStartMs,
            "firstContinuousSpeechDurationMs": st.firstContinuousSpeechDurationMs,
            "totalSpeakingTimeMs": st.totalSpeakingTimeMs,
            "longestMonologueMs": st.longestMonologueMs,
            "interruptionCount": st.interruptionCount,
            "keywordHits": list(st.keywordHits),
            # Helpful context for ML + debugging
            "textAtDecision": text_at_decision,
            "wordCountAtDecision": _word_count(text_at_decision),
            "beepDetected": st.beepDetected,
            "hasKeyword": st.hasKeyword,
            "humanBias": st.humanBias,
            "accumulatedTextFinal": (st.accumulatedText or "").strip(),
            "lastObservedText": (st.lastObservedText or "").strip(),
            "lastObservedIsFinal": bool(st.lastObservedIsFinal),
            # Full state snapshot (includes speechSegments)
            "state": self._public_state(st),
            "loggedAtEpochMs": int(time.time() * 1000),
        }

        path = (self._log_path or "").strip() or "amd_decisions.jsonl"
        abs_path = os.path.abspath(path)
        # Ensure directory exists.
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        async with self._log_lock:
            # Small synchronous append is fine; still do it off the event loop.
            await asyncio.to_thread(self._append_line, path, line)

        # Also emit a single console log per call decision (easy to debug in prod).
        amd_logger.info(
            "AMD decision call_id=%s decision=%s confidence=%.2f rule=%s decisionTimeMs=%s "
            "firstSpeechStartMs=%s firstContinuousSpeechDurationMs=%s totalSpeakingTimeMs=%s "
            "longestMonologueMs=%s interruptionCount=%s keywordHits=%s log_path=%s",
            call_id,
            st.decision,
            st.confidence,
            st.decisionRule,
            st.decision_time_ms(),
            st.firstSpeechStartMs,
            st.firstContinuousSpeechDurationMs,
            st.totalSpeakingTimeMs,
            st.longestMonologueMs,
            st.interruptionCount,
            list(st.keywordHits),
            abs_path,
        )

    @staticmethod
    def _append_line(path: str, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
