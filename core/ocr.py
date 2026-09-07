"""약봉투 사진 -> 텍스트 줄.

엔진은 갈아끼운다. 로컬(PaddleOCR)로 개발하고 클라우드로 올린다.
정확도가 다르지 어차피 뒤쪽 매칭·확인 화면이 오차를 흡수한다.

여러 장을 동시에 읽는다. 봉투 4장이면 4배 빨라지는 게 아니라
가장 느린 한 장의 시간만 든다.
"""
from __future__ import annotations

import asyncio
import functools
import os
import pathlib
import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Line:
    """OCR이 읽은 텍스트 한 줄."""
    text: str
    confidence: float = 0.0

    def __post_init__(self):
        self.text = " ".join(self.text.split())


class OcrEngine(Protocol):
    name: str
    parallel_safe: bool     # 한 인스턴스를 여러 스레드가 동시에 호출해도 되는가
    def read(self, image: bytes) -> list[Line]: ...


class MockEngine:
    """테스트용. 이미지를 안 본다.

    파이프라인 뒤쪽(매칭·확인·엔진)을 OCR 없이 검증하기 위해 존재한다.
    """
    name = "mock"
    parallel_safe = True

    def __init__(self, scripted: list[list[str]] | None = None):
        self._scripted = scripted or []
        self._i = 0

    def read(self, image: bytes) -> list[Line]:
        if self._i < len(self._scripted):
            texts = self._scripted[self._i]
        else:
            texts = []
        self._i += 1
        return [Line(t, 0.99) for t in texts]


class PaddleEngine:
    """로컬. 키가 필요 없고 비용이 0이다. 대신 한국어 정확도가 클라우드보다 낮다.

    각도 분류(use_angle_cls)는 기본으로 끈다. 켜면 멀쩡한 글자 조각을
    뒤집어 망가뜨렸다. 합성 약봉투 기준 약 이름 인식이 2/3에서 3/3으로
    올랐다. 뒤집힌 사진까지 받아야 하면 OCR_ANGLE_CLS=1 로 켠다.
    """
    name = "paddle"
    # 예측기 하나를 여러 스레드가 동시에 호출해도 되는지 보장되지 않는다.
    # 크래시보다 조용히 틀린 결과가 더 위험하므로 추론을 직렬화한다.
    # 클라우드 엔진으로 바꾸면 그대로 병렬로 돈다.
    parallel_safe = False

    _lock = threading.Lock()

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _build():
        # paddle 의 C++ 레이어가 비ASCII 경로를 못 연다. 윈도우 한글 계정에서
        # ~/.paddleocr 가 그대로 깨진다. 모델을 ASCII 경로에 두고 지정한다.
        # 리눅스 컨테이너에서는 이 문제가 없으므로 미설정이 기본이다.
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        import paddleocr
        from paddleocr import PaddleOCR

        kw = {}
        root = os.environ.get("PADDLE_MODEL_DIR")
        if root:
            # rec_model_dir 만 주면 문자사전이 기본으로 남는다. 같이 넘긴다.
            dic = (pathlib.Path(paddleocr.__file__).parent
                   / "ppocr" / "utils" / "dict" / "korean_dict.txt")
            kw = {"det_model_dir": f"{root}/det",
                  "rec_model_dir": f"{root}/rec",
                  "cls_model_dir": f"{root}/cls",
                  "rec_char_dict_path": str(dic),
                  "use_space_char": True}
        return PaddleOCR(lang="korean", show_log=False,
                         use_angle_cls=os.environ.get("OCR_ANGLE_CLS") == "1",
                         **kw)

    @classmethod
    def _reader(cls):
        """lru_cache 는 첫 호출이 동시에 들어오면 각각 모델을 만든다.
        봉투를 병렬로 읽을 때 무거운 초기화가 여러 번 일어난다."""
        with cls._lock:
            return cls._build()

    def read(self, image: bytes) -> list[Line]:
        import cv2
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return []
        result = self._reader().ocr(arr, cls=False)
        if not result or not result[0]:
            return []
        return [Line(t, float(c)) for _box, (t, c) in result[0] if t and t.strip()]


class GoogleVisionEngine:
    """GOOGLE_APPLICATION_CREDENTIALS 또는 GOOGLE_VISION_KEY 필요."""
    name = "google"
    parallel_safe = True      # 요청마다 HTTP. 공유 상태가 없다

    def read(self, image: bytes) -> list[Line]:
        from google.cloud import vision

        client = vision.ImageAnnotatorClient()
        resp = client.document_text_detection(image=vision.Image(content=image))
        if resp.error.message:
            raise RuntimeError(resp.error.message)
        out = []
        for page in resp.full_text_annotation.pages:
            for block in page.blocks:
                for para in block.paragraphs:
                    text = "".join(
                        "".join(s.text for s in w.symbols) for w in para.words)
                    if text.strip():
                        out.append(Line(text, float(block.confidence or 0.0)))
        return out


class ClovaEngine:
    """CLOVA_OCR_URL, CLOVA_OCR_SECRET 필요. 한국어 인쇄체에 강하다."""
    name = "clova"
    parallel_safe = True      # 요청마다 HTTP. 공유 상태가 없다

    def read(self, image: bytes) -> list[Line]:
        import base64
        import json
        import uuid

        import requests

        url = os.environ["CLOVA_OCR_URL"]
        secret = os.environ["CLOVA_OCR_SECRET"]
        body = {
            "version": "V2",
            "requestId": str(uuid.uuid4()),
            "timestamp": 0,
            "images": [{"format": "jpg", "name": "envelope",
                        "data": base64.b64encode(image).decode()}],
        }
        r = requests.post(url, headers={"X-OCR-SECRET": secret},
                          data=json.dumps(body), timeout=30)
        r.raise_for_status()
        out = []
        for img in r.json().get("images", []):
            for f in img.get("fields", []):
                t = f.get("inferText", "")
                if t.strip():
                    out.append(Line(t, float(f.get("inferConfidence", 0.0))))
        return out


_ENGINES = {e.name: e for e in (PaddleEngine, GoogleVisionEngine, ClovaEngine)}


@functools.lru_cache(maxsize=1)
def get_engine(name: str | None = None) -> OcrEngine:
    """OCR_ENGINE 환경변수로 고른다. 기본은 로컬."""
    name = name or os.environ.get("OCR_ENGINE", "paddle")
    if name not in _ENGINES:
        raise ValueError(f"모르는 엔진: {name}. 가능: {sorted(_ENGINES)}")
    return _ENGINES[name]()


async def read_many(engine: OcrEngine, images: list[bytes]) -> list[list[Line]]:
    """봉투 여러 장을 동시에 읽는다.

    engine.read 는 동기 함수(모델 추론 또는 HTTP)라 스레드로 내보낸다.
    한 장이 실패해도 나머지는 살린다. 실패한 장은 빈 목록이 된다.
    """
    limit = asyncio.Semaphore(len(images) if getattr(engine, "parallel_safe", False) else 1)

    async def one(blob):
        async with limit:
            return await asyncio.to_thread(engine.read, blob)

    results = await asyncio.gather(*(one(b) for b in images), return_exceptions=True)
    return [[] if isinstance(r, BaseException) else r for r in results]


def warmup(engine: OcrEngine) -> None:
    """무거운 초기화를 기동 시점으로 옮긴다.

    안 하면 첫 사용자의 요청 안에서 모델이 올라간다. 실패해도 죽이지 않는다.
    엔진이 없는 환경에서도 서비스는 떠 있어야 하고, 오류는 실제 사용
    시점에 드러나는 편이 낫다.
    """
    warm = getattr(engine, "_reader", None)
    if callable(warm):
        warm()
