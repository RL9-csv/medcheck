"""medcheck — 부모님 진료 준비 브리핑.

설계 원칙
  1. AI는 인식과 문장 생성에만. 판정은 결정론적 엔진이 한다.
  2. 사람이 확인하기 전에는 어떤 판정도 하지 않는다.
  3. 외부 AI가 죽어도 서비스는 계속 동작한다.
  4. 원본 이미지는 추출 직후 폐기하고, 로그에 PII를 남기지 않는다.
"""
import asyncio, sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core import match, engine
from core.model import Review, Source
from core.matrix import build_matrix
from core.telemetry import Trace
from core.version import knowledge_version

ROOT = Path(__file__).parent
STATE = {"ready": False, "version": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 요청마다 DB를 뒤지지 않도록 기동 시 사전을 메모리에 올린다
    t = Trace()
    with t.stage("catalog_preload"):
        n = len(match._catalog())
    STATE["version"] = knowledge_version()
    STATE["ready"] = True
    t.emit("startup", catalog_items=n, catalog_version=STATE["version"]["catalog_version"])
    yield


app = FastAPI(title="medcheck", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
tpl = Jinja2Templates(directory=str(ROOT / "templates"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    if not STATE["ready"]:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "ready", **STATE["version"]}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return tpl.TemplateResponse(request, "index.html",
                                {"symptoms": engine.SYMPTOMS})


@app.post("/confirm", response_class=HTMLResponse)
async def confirm(request: Request):
    """봉투별 입력 -> 후보 제시. 사람이 고르기 전에는 판정하지 않는다."""
    form = await request.form()
    t = Trace()
    envelopes = []

    with t.stage("matching"):
        for i in range(1, 6):
            raw = (form.get(f"env{i}") or "").strip()
            if not raw:
                continue
            items = []
            for line in [x.strip() for x in raw.splitlines() if x.strip()]:
                med, cands, status = match.resolve(line)
                t.count(status)
                items.append({"query": line, "status": status,
                              "auto": med, "candidates": cands})
            envelopes.append({"index": i, "label": form.get(f"label{i}") or "", "items": items})

    t.emit("confirm", envelopes=len(envelopes))
    return tpl.TemplateResponse(request, "confirm.html", {
        "envelopes": envelopes,
        "symptoms": form.getlist("symptom"),
        "symptom_defs": engine.SYMPTOMS, "trace": t.stages})


def _load_meds(seqs):
    """확정된 품목코드 -> Medication. 사람이 고른 것만 들어온다."""
    from core.match import _catalog, _ingredients_cached
    from core.model import Medication
    by_seq = {r[0]: r for r in _catalog()}
    out = []
    for seq in seqs:
        r = by_seq.get(seq)
        if not r:
            continue
        out.append(Medication(item_seq=r[0], product_name=r[1], otc=r[3],
                              ingredients=list(_ingredients_cached(r[2])),
                              confidence=1.0, confirmed=True))
    return out


@app.post("/result", response_class=HTMLResponse)
async def result(request: Request):
    form = await request.form()
    t = Trace()
    symptoms = form.getlist("symptom")

    with t.stage("collect"):
        review = Review()
        for i in range(1, 6):
            seqs = []
            for k in form.keys():
                if k == f"pick{i}" or k.startswith(f"pick{i}_"):
                    seqs.extend([v for v in form.getlist(k) if v])
            if not seqs:
                continue
            src = Source(index=i, label=(form.get(f"label{i}") or "").strip() or None)
            src.medications = _load_meds(seqs)
            if src.medications:
                review.sources.append(src)

    with t.stage("engine"):
        findings = engine.analyze(review, symptoms)
        matrix = build_matrix(review)

    with t.stage("questions"):
        # LLM은 아직 붙이지 않았다. 붙일 때도 실패하면 이 템플릿으로 되돌아온다.
        questions = engine.make_questions(findings, symptoms)

    t.count("meds", len(review.all_meds))
    t.count("sources", len(review.sources))
    t.count("findings", len(findings))
    t.emit("result", symptoms=len(symptoms))

    return tpl.TemplateResponse(request, "result.html", {
        "review": review, "matrix": matrix, "findings": findings,
        "questions": questions, "trace": t.stages,
        "version": STATE["version"]["catalog_version"],
        "symptom_labels": [engine.SYMPTOM_LABEL.get(s, s) for s in symptoms]})
