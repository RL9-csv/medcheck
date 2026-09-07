"""단계별 지연시간 계측과 PII 없는 구조화 로깅.

로그에 절대 넣지 않는 것: 이미지, 환자명, OCR 원문, 약 이름
로그에 넣는 것: 단계별 소요시간, 건수, 판정 분포, 오류 유형
"""
import time, uuid, json, logging, sys
from contextlib import contextmanager
from dataclasses import dataclass, field

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("medcheck")


@dataclass
class Trace:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stages: dict = field(default_factory=dict)        # 단계명 -> ms
    counters: dict = field(default_factory=dict)      # auto/suggest/reject 등

    @contextmanager
    def stage(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round((time.perf_counter() - t0) * 1000, 1)

    def count(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n

    def emit(self, event: str, **extra):
        payload = {
            "event": event,
            "request_id": self.request_id,
            "stages_ms": self.stages,
            "total_ms": round(sum(self.stages.values()), 1),
            **self.counters,
            **extra,
        }
        log.info(json.dumps(payload, ensure_ascii=False))
        return payload
