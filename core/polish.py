"""템플릿 문장을 LLM으로 다듬는다.

이 모듈은 판정을 하지 않는다. 어느 약이 겹치는지, 항콜린 부담이 몇 점인지는
core/engine.py 에서 결정론적으로 이미 끝나 있다. 여기서 하는 일은 그 결론을
보호자가 진료실에서 읽을 만한 문장으로 바꾸는 것뿐이다.

그래서 실패해도 잃는 게 없다. 키가 없거나, 호출이 죽거나, 검증에 걸리면
원래 템플릿 문장이 그대로 나간다. LLM 은 있으면 좋고 없어도 되는 층이다.

약 이름은 밖으로 내보내지 않는다. telemetry.py 가 로그에도 안 남기는 것을
외부 API 에 통째로 보내면 원칙이 한 저장소 안에서 뒤집힌다. 어차피 이 층은
어미와 흐름만 다듬으므로 약 이름을 알 필요가 없다. 토큰으로 바꿔 보내고
돌아오면 되돌린다. 검증도 쉬워진다 - 토큰이 그대로 있는지만 보면 된다.
"""
from __future__ import annotations

import os
import re

MODEL = "claude-opus-5"
TIMEOUT = 3.0            # 사용자가 기다리는 요청 안에서 동기로 돈다.
                         # 실패해도 이만큼은 버린다. 8초는 전체 2.3초를 4배로 만든다.
MAX_LEN_RATIO = 2.0      # 원문 대비 이 배수를 넘으면 거부한다

# 이 표현이 들어가면 버린다. 우리는 복약 중단이나 위험을 말하지 않는다.
BANNED = re.compile(
    r"중단|끊으|끊어|복용하지 마|위험|해롭|부작용이 있습니다|"
    r"원인입니다|때문입니다|드시지 마|줄이세요|바꾸세요"
)

SYSTEM = """\
당신은 보호자가 의사에게 건넬 질문 문장을 다듬습니다.

지켜야 할 것
- 내용을 바꾸지 마십시오. 숫자는 그대로 둡니다.
- 〈1〉 〈2〉 같은 표시는 이름을 가린 자리입니다. 글자 그대로 두십시오.
- 그 표시 바로 뒤의 조사(은/는/이/가/을/를)는 원문에 있던 것을 그대로 씁니다.
  가려진 이름의 받침을 알 수 없으므로 조사를 바꾸면 틀립니다.
- 새로운 사실을 넣지 마십시오. 주어진 문장에 없는 내용은 쓰지 않습니다.
- 판단하지 마십시오. 위험하다, 원인이다, 중단하라는 표현을 쓰지 않습니다.
- 존댓말 질문 형태를 유지하십시오. 의사에게 확인을 부탁하는 어조입니다.
- 한 문장을 두 문장 이내로 유지하십시오.

출력 형식
- 입력으로 받은 문장 개수와 같은 개수를 출력합니다.
- 한 줄에 한 문장씩, 번호나 기호 없이 문장만 씁니다.
"""


def _mask(questions: list[str], terms: set[str]) -> tuple[list[str], dict[str, str]]:
    """보호 단어를 토큰으로 바꾼다. 밖으로 나가는 것은 토큰뿐이다.

    긴 이름부터 바꾼다. "에어탈"과 "에어탈정"이 같이 있으면 짧은 쪽이 먼저
    걸려서 "〈1〉정"처럼 쪼개진다.
    """
    mapping: dict[str, str] = {}
    for i, term in enumerate(sorted(terms, key=len, reverse=True), 1):
        if not any(term in q for q in questions):
            continue
        tok = f"〈1{i}〉".replace("1", "", 1)   # 〈1〉 〈2〉 ...
        mapping[tok] = term
        questions = [q.replace(term, tok) for q in questions]
    return questions, mapping


def _unmask(text: str, mapping: dict[str, str]) -> str:
    for tok, term in mapping.items():
        text = text.replace(tok, term)
    return text


def _reject_reason(src: str, out: str, protected: set[str]) -> str | None:
    """받아들일 수 없는 이유. None 이면 받아들인다.

    참·거짓이 아니라 이유를 돌려주는 이유는, 거부율만으로는 프롬프트를 못 고치기
    때문이다. "숫자를 바꿔서 3건, 금지 표현으로 1건"이라야 어디를 고칠지 안다.

    protected 는 이제 토큰이다. 어미 변화가 없으므로 정확히 일치 검사를 한다.
    """
    if not out.strip():
        return "empty"
    if len(out) > len(src) * MAX_LEN_RATIO:
        return "too_long"
    if BANNED.search(out):
        return "banned"
    for term in protected:                      # 보호 단어가 사라지면 다른 질문이다
        if term in src and term not in out:
            return "term_lost"
    # 숫자는 정확히 같아야 한다. 부분집합으로 두면 "3점"이 "3점(5점 만점)"이
    # 되어도 통과한다. 없던 사실을 넣지 말라고 해놓고 검증이 안 막으면 안 된다.
    if set(re.findall(r"\d+", src)) != set(re.findall(r"\d+", out)):
        return "number_changed"
    return None


def polish(questions: list[str],
           protected: list[str] | None = None) -> tuple[list[str], str, dict[str, int]]:
    """다듬은 문장, 호출 경로, 문장 단위 집계를 돌려준다.

    경로와 집계를 나눈 이유가 있다. 예전에는 "출력이 입력과 다른가"로 경로를
    정했는데, 그러면 두 방향으로 틀린다.
      LLM 이 전부 원문 그대로 반환   검증 전부 통과인데 거부로 집계됨
      3개 중 1개만 거부             전부 성공으로 집계되어 부분 거부가 안 보임
    경로는 호출이 됐는가만 말하고, 몇 문장이 통과했는지는 집계가 말한다.

    경로: llm | rejected | failed | disabled
      llm       호출 성공, 줄 수 일치. 문장별 채택 여부는 집계를 본다
      rejected  줄 수가 안 맞아 응답 전체를 버림
      failed    호출 실패 또는 거절
      disabled  키 없음 또는 질문 없음
    """
    stats: dict[str, int] = {}

    def bump(k, n=1):
        stats[k] = stats.get(k, 0) + n

    keep_terms = {t for t in (protected or []) if t}
    if not questions:
        return questions, "disabled", stats
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return questions, "disabled", stats

    # 약 이름은 여기서 토큰이 된다. 이 아래로는 실제 이름이 나가지 않는다.
    masked, mapping = _mask(questions, keep_terms)
    tokens = set(mapping)

    try:
        import anthropic

        client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=0)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},   # 문장 다듬기다. 깊게 생각할 일이 없다.
            system=SYSTEM,
            messages=[{"role": "user", "content": chr(10).join(masked)}],
        )
        if resp.stop_reason == "refusal":
            bump("failed", len(questions))
            return questions, "failed", stats
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception:
        # 어떤 이유로 죽든 원문으로 간다. 여기서 예외를 밖으로 보내지 않는다.
        bump("failed", len(questions))
        return questions, "failed", stats

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) != len(questions):
        bump("rejected", len(questions))
        bump("reason_line_count", len(questions))
        return questions, "rejected", stats

    out = []
    for src, cand in zip(masked, lines):
        why = _reject_reason(src, cand, tokens)
        if why is None:
            bump("accepted")
            out.append(_unmask(cand, mapping))
        else:
            bump("rejected")
            bump(f"reason_{why}")
            out.append(_unmask(src, mapping))

    return out, "llm", stats
