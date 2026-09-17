"""medcheck — 여러 병원에서 받은 약을 확인하고 진료 준비를 돕는다.

설계 원칙
  1. AI는 인식과 문장 생성에만. 판정은 결정론적 엔진이 한다.
  2. 사람이 확인하기 전에는 어떤 판정도 하지 않는다.
  3. 외부 AI가 죽어도 서비스는 계속 동작한다.
  4. 원본 이미지는 추출 직후 폐기하고, 로그에 PII를 남기지 않는다.
"""
import asyncio, os, re, sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core import match, engine, lines as lines_mod, ocr, polish
from core.jobs import Queue
from core.model import Review, Source
from core.matrix import build_matrix
from core.telemetry import Trace
from core.version import knowledge_version

ROOT = Path(__file__).parent
STATE = {"ready": False, "version": None, "ocr": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 요청마다 DB를 뒤지지 않도록 기동 시 사전을 메모리에 올린다
    t = Trace()
    with t.stage("catalog_preload"):
        n = len(match._catalog())
    STATE["version"] = knowledge_version()

    # OCR 모델도 미리 올린다. 안 하면 첫 사용자가 20초를 기다린다.
    # 엔진이 없어도 서비스는 뜬다. 사진 업로드 시점에 오류가 드러나는 편이 낫다.
    with t.stage("ocr_warmup"):
        try:
            eng = ocr.get_engine()
            ocr.warmup(eng)
            STATE["ocr"] = eng.name
        except Exception as e:
            STATE["ocr"] = f"unavailable ({type(e).__name__})"

    # 샘플 봉투도 미리 읽어둔다. 안 하면 첫 방문자가 13초를 낸다.
    # 투표 기간에 링크를 처음 누르는 사람이 그 13초를 맞는다.
    with t.stage("sample_preload"):
        try:
            n_sample = len(_sample_lines())
        except Exception:
            n_sample = -1

    STATE["ready"] = True
    t.emit("startup", sample_lines=n_sample, ocr=STATE["ocr"], catalog_items=n, catalog_version=STATE["version"]["catalog_version"])
    yield


app = FastAPI(title="medcheck", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
tpl = Jinja2Templates(directory=str(ROOT / "templates"))
tpl.env.globals["pick_value"] = lambda m: _pick_value(m)


@app.middleware("http")
async def _headers(request: Request, call_next):
    """접수번호가 URL 에 있다. 밖으로 새지 않게 막는다.

    확인 화면이 cdn.tailwindcss.com 을 부르므로 외부 요청이 나가고 그때
    Referer 에 현재 주소가 실린다. 요즘 브라우저 기본값
    (strict-origin-when-cross-origin)은 경로를 빼고 보내지만, 그건 기본값에
    기대는 것이다. 기본값이 다른 브라우저나 구버전에서는 접수번호가 통째로
    나간다. 로그인이 없어 번호가 곧 열쇠라 확정해 둔다.
    """
    r = await call_next(request)
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    if not STATE["ready"]:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "ready", "ocr": STATE["ocr"], **STATE["version"]}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return tpl.TemplateResponse(request, "index.html",
                                {"symptoms": engine.SYMPTOMS})


MAX_FILES = 5

# 사진 한 장의 상한. 25MB 다.
#
# 10MB 였는데 요즘 폰 사진이 그걸 쉽게 넘는다. 넘으면 조용히 버리고
# "사진을 한 장 이상 올려주세요" 가 떴다. 분명히 올린 사람이 안 올렸다는
# 말을 듣는다. 실제로 그렇게 걸렸다.
#
# 메모리는 문제가 아니다. 받자마자 _shrink 로 1280px 로 줄이므로 원본
# 크기는 잠깐만 들고 있는다.
MAX_BYTES = 25 * 1024 * 1024

# 무거운 요청(OCR+매칭)을 한 번에 몇 건까지 받을지. 1 이다.
#
# t3.small 은 CoreCount 1 / ThreadsPerCore 2 라 물리 코어가 하나다.
# "2 vCPU" 가 두 배 일하는 게 아니다. 실물 사진 한 장(33줄)으로 원격
# 측정한 처리량이 이렇다.
#
#   동시 1   1건 / 24.2s = 0.041 req/s
#   동시 2   2건 / 51.3s = 0.039 req/s
#   동시 3   3건 / 72.0s = 0.042 req/s
#
# 처리량이 평평하다. 동시 실행을 허용해도 초당 처리량은 그대로고 모든
# 사용자의 대기시간만 3배가 된다. 그래서 병렬로 돌리지 않고 줄을 세운다.
RETRY_AFTER = os.environ.get("RETRY_AFTER", "24")   # 실측 단건 소요시간

# 접수번호로 받고 뒤에서 한 건씩 처리한다. 동시 실행이 1인 이유는 위와 같다.
# 넘치면 대기시간으로 거절한다. core/jobs.py 참고.
QUEUE = Queue(lambda *a: _do_upload(*a),
              max_wait_sec=int(os.environ.get("MAX_WAIT_SEC", "120")))


def _pick_value(med) -> str:
    """확인 화면의 체크박스에 넣을 값.

    표시 이름이 카탈로그 원본과 다르면 "품목코드|표시이름" 으로 넘긴다.
    안 그러면 확인 화면에서 "레피졸정" 이라고 보여준 것을 사용자가 확인했는데
    브리핑에는 "레피졸정15밀리그램" 이 찍힌다. 확인한 것과 다른 이름을
    의사에게 보여주라고 말하게 된다.

    사진에서 확정된 약은 카탈로그 원본 그대로라 코드만 넘어간다.
    """
    from core.match import _catalog, _permit
    orig = dict((r[0], r[1]) for r in _catalog()).get(med.item_seq)         or dict(_permit()).get(med.item_seq)
    return f"{med.item_seq}|{med.product_name}"         if orig and med.product_name != orig else med.item_seq


def _envelope(index, label, lines, trace, drop_none=False):
    """텍스트 줄 -> 확인 화면 항목. 사진과 직접 입력이 같은 구조로 모인다.

    drop_none: 사진은 병원명·용법·약사명까지 다 읽힌다. 아무 품목과도
    맞지 않는 줄은 화면에서 빼고 몇 줄이 빠졌는지만 알린다.

    막는 층이 둘이다. 어느 하나만으로는 안 막힌다.
      lines.filter_lines   조제일자·용법용량 같은 안내 문구를 패턴으로 제거
      match 의 길이 가드    "식후" -> 후라시닐정 같은 짧은 조각의 오탐을 차단
    """
    read = len(lines)                      # 화면에 보여줄 값은 OCR이 실제로 읽은 줄 수다
    lines, pre_dropped = lines_mod.filter_lines(lines)
    trace.count("line_filtered", pre_dropped)

    items, dropped = [], pre_dropped
    for line in lines:
        med, cands, status = match.resolve(line)
        trace.count(status)
        if status == "none" and drop_none:
            dropped += 1
            continue
        items.append({"query": line, "status": status,
                      "auto": med, "candidates": cands})

    # 슬롯 번호를 여기서 박는다. 화면에서 loop.index0 을 쓰면 정렬하는 순간
    # 라디오 그룹 이름이 어긋나 다른 줄의 선택을 덮어쓴다.
    for n, it in enumerate(items):
        it["slot"] = n

    # 확실한 것을 위로 올린다. OCR 이 읽은 순서대로 두면 확정된 약과 잡음
    # 후보가 섞여서 나온다. 영수증형은 스무 줄이 넘어 사용자가 자기 약을
    # 찾으려면 잡음 사이를 훑어야 한다.
    #
    # 거르는 것이 아니라 순서만 바꾼다. 목록에서 사라지는 항목이 없으므로
    # 정확도에 영향이 없다. 잡음 후보는 사람이 보고 안 고르면 그만이지만,
    # 약이 안 보이면 사용자가 알 방법이 없다. 그래서 필터를 조이는 대신
    # 순서를 바꾼다.
    rank = {"auto": 0, "suggest": 1, "none": 2}
    items.sort(key=lambda it: (rank.get(it["status"], 3),
                               -(it["candidates"][0].confidence if it["candidates"] else 0)))
    return {"index": index, "label": label, "items": items,
            "dropped": dropped, "read": read,
            "n_auto": sum(1 for i in items if i["status"] == "auto"),
            "n_ask": sum(1 for i in items if i["status"] != "auto")}


MAX_SIDE = 1280          # OCR 전에 긴 변을 이만큼으로 줄인다


def _shrink(blob: bytes) -> bytes:
    """OCR 에 넣기 전에 사진을 줄인다.

    요즘 폰 사진은 3000px 이 넘는데 그 해상도가 정확도를 안 올린다. 실물
    5장에서 원본과 1280px 의 검출 결과가 같았고 시간만 1.3배 들었다.
    무료 티어는 0.1~0.5 vCPU 라 이 차이가 그대로 사용자 대기시간이 된다.

    실패하면 원본을 그대로 쓴다. 여기서 요청을 죽이지 않는다.
    """
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(blob))
        if max(im.size) <= MAX_SIDE:
            return blob
        im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return blob


SAMPLE = Path("static/sample.png")
_SAMPLE_CACHE: list | None = None


def _sample_lines() -> list[str]:
    """샘플 봉투의 OCR 결과. 한 번만 읽고 프로세스가 살아 있는 동안 재사용한다.

    투표 기간에 링크를 누르는 사람 대부분은 약봉투 사진이 없다. 뭐 하는
    물건인지 보고 싶을 뿐이다. 그런데 무료 티어에서 OCR 한 장이 30~60초다.
    첫 화면에서 그만큼 기다리면 닫는다.

    샘플은 고정된 파일이라 결과도 고정이다. 한 번 읽어두고 그 뒤로는
    OCR 을 안 돈다. 자기 사진을 올리는 사람은 기다릴 이유가 있다.
    """
    global _SAMPLE_CACHE
    if _SAMPLE_CACHE is None:
        try:
            page = ocr.get_engine().read(SAMPLE.read_bytes())
            _SAMPLE_CACHE = [l.text for l in page]
        except Exception:
            _SAMPLE_CACHE = []
    return _SAMPLE_CACHE


@app.post("/sample", response_class=HTMLResponse)
async def sample(request: Request):
    """샘플 봉투로 바로 확인 화면까지 간다. OCR 을 돌지 않는다."""
    t = Trace()
    with t.stage("matching"):
        env = _envelope(1, "샘플 약봉투", _sample_lines(), t, drop_none=True)
    t.count("sample", 1)
    t.emit("sample", engine=ocr.get_engine().name)
    return tpl.TemplateResponse(request, "confirm.html", {
        "envelopes": [env], "symptoms": [], "symptom_defs": engine.SYMPTOMS,
        "trace": t.stages, "from_photo": True, "is_sample": True})


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request):
    """약봉투 사진 -> 확인 화면. 여러 장을 동시에 읽는다."""
    form = await request.form()
    t = Trace()

    # 슬롯 번호로 직접 읽는다. getlist 로 모으면 2번을 비웠을 때
    # 3번 사진이 label2 를 가져가 버린다.
    images, labels, too_big = [], [], False
    for i in range(1, MAX_FILES + 1):
        f = form.get(f"photo{i}")
        if not getattr(f, "filename", ""):
            continue
        blob = await f.read()
        if not blob:
            continue
        if len(blob) > MAX_BYTES:
            too_big = True          # 조용히 버리지 않는다. 아래에서 말한다
            continue
        images.append(blob)      # 축소는 작업자 안에서. 재시도에 원본이 필요하다
        labels.append((form.get(f"label{i}") or "").strip())

    if not images:
        # 올렸는데 안 올렸다고 하면 사용자는 자기가 뭘 잘못했는지 모른다.
        # 파일이 없는 것과 너무 큰 것을 구분해서 말한다.
        return tpl.TemplateResponse(request, "index.html", {
            "symptoms": engine.SYMPTOMS, "version": STATE["version"],
            "error": ("사진 용량이 너무 큽니다. 한 장에 25MB 까지 됩니다. "
                      "카메라 설정에서 화질을 낮추거나 다른 사진으로 "
                      "올려주세요.") if too_big else "사진을 한 장 이상 올려주세요."})

    if QUEUE.full(len(images)):
        return tpl.TemplateResponse(request, "index.html", {
            "symptoms": engine.SYMPTOMS, "version": STATE["version"],
            "error": f"지금 앞에 {QUEUE.eta_sec() // 60 + 1}분쯤 밀려 있습니다. "
                     f"잠시 뒤에 다시 눌러 주세요."},
            status_code=503, headers={"Retry-After": RETRY_AFTER})

    # 여기서 기다리지 않는다. 접수번호만 주고 끊는다.
    #
    # 전에는 이 자리에서 20~58초를 붙잡고 있었다. 폰 화면이 꺼지면 연결이
    # 끊기고 서버가 한 일이 전부 버려졌다. 약국 앞에서 폰으로 찍는 사람이
    # 대상이라 그 상황이 예외가 아니라 기본이다.
    job = QUEUE.submit(len(images), images, labels,
                       form.getlist("symptom"), t)
    return RedirectResponse(f"/wait/{job.id}", status_code=303)


@app.get("/wait/{job_id}", response_class=HTMLResponse)
async def wait(request: Request, job_id: str):
    job = QUEUE.get(job_id, touch=True)
    if not job:
        return tpl.TemplateResponse(request, "index.html", {
            "symptoms": engine.SYMPTOMS, "version": STATE["version"],
            "error": "결과를 찾지 못했습니다. 사진을 다시 올려주세요."},
            status_code=404)
    if job.state == "done":
        return RedirectResponse(f"/confirm/{job.id}", status_code=303)
    return tpl.TemplateResponse(request, "wait.html",
                                {"job": job, "eta": QUEUE.eta_sec(job.id)})


@app.get("/job/{job_id}")
async def job_state(job_id: str):
    """대기 화면이 물어보는 곳. 가볍게 유지한다.

    이 요청이 살아있음 신호를 겸한다. 탭을 닫으면 신호가 끊기고, 아직
    차례가 안 온 작업은 줄에서 빠진다.
    """
    job = QUEUE.get(job_id, touch=True)
    if not job:
        return JSONResponse({"state": "expired"}, status_code=404)
    return {"state": job.state, "elapsed_ms": job.elapsed_ms(),
            "position": QUEUE.position(job.id), "eta_sec": QUEUE.eta_sec(job.id)}


@app.get("/confirm/{job_id}", response_class=HTMLResponse)
async def confirm_job(request: Request, job_id: str):
    job = QUEUE.get(job_id, touch=True)
    if not job or job.state != "done":
        return RedirectResponse(f"/wait/{job_id}", status_code=303)
    if job.result is None:
        return tpl.TemplateResponse(request, "index.html", {
            "symptoms": engine.SYMPTOMS, "version": STATE["version"],
            "error": "사진에서 글자를 읽지 못했습니다. 더 밝은 곳에서 "
                     "글자가 잘 보이게 다시 찍어주시거나, 아래에서 약 "
                     "이름으로 찾아주세요."})
    return tpl.TemplateResponse(request, "confirm.html", job.result)


async def _do_upload(images, labels, symptoms, t):
    eng = ocr.get_engine()
    with t.stage("ocr"):
        small = [_shrink(b) for b in images]
        pages = await ocr.read_many(eng, small)

        # 한 줄도 못 읽은 사진만 원본 해상도로 한 번 더 본다.
        #
        # 기본 판독기는 긴 변을 1280 으로 줄인다. 약봉투를 가까이서 찍으면
        # 충분하지만, 화면이나 종이를 멀리서 찍으면 글자가 원래 작아서
        # 검출이 아예 안 된다. 실제로 화면을 찍은 사진이 0줄이 나왔다.
        #
        # 평소에는 이 경로를 안 탄다. 빈손일 때만 값을 치른다 — 정확히
        # 그때가 값을 치를 만한 때다.
        if hasattr(eng, "read_hi"):
            for i, page in enumerate(pages):
                if page:
                    continue
                try:
                    pages[i] = await asyncio.to_thread(eng.read_hi, images[i])
                    t.count("ocr_retry", 1)
                    if pages[i]:
                        t.count("ocr_retry_hit", 1)
                except Exception:
                    pass

    # 매칭은 rapidfuzz 안에서 도는 동기 연산이다. 그대로 await 없이
    # 부르면 이벤트 루프를 붙잡는다. 실물 사진 129줄이 45초였는데 그
    # 45초 동안 /health 를 포함해 다른 어떤 요청도 응답되지 않았다.
    # HEALTHCHECK 가 30초 간격 3회라 긴 요청 두 건이면 컨테이너가
    # unhealthy 로 넘어간다. 스레드로 뺀다.
    with t.stage("matching"):
        envelopes = await asyncio.to_thread(
            lambda: [_envelope(i, labels[i - 1], [l.text for l in page], t,
                               drop_none=True)
                     for i, page in enumerate(pages, 1)])

    t.count("photos", len(images))
    t.count("empty_pages", sum(1 for p in pages if not p))
    # 파일명과 읽은 텍스트는 로그에 남기지 않는다. 개수와 시간만 남긴다.
    t.emit("upload", engine=eng.name,
           bytes_total=sum(len(b) for b in small))

    # 원본 이미지는 여기서 끝난다. 남기는 것은 화면에 그릴 내용뿐이다.
    images.clear()

    # 모든 사진에서 한 줄도 못 읽었으면 확인 화면을 띄우지 않는다.
    #
    # 그냥 두면 "이렇게 읽었습니다. 맞는지 확인하고 고쳐주세요" 가 약 0개로
    # 뜬다. 읽은 게 없는데 읽었다고 말하는 것이라 사용자는 뭘 해야 할지
    # 모른다. 직접 입력 쪽에서 고친 것과 같은 모양이 사진 쪽에 남아 있었다.
    #
    # 여기로 오는 경로가 여럿이다. 아이폰 HEIC(변환 없이 올라오면 0줄),
    # 깨진 파일, 너무 어두운 사진, 약봉투가 아닌 사진. 원인별로 나누지
    # 않고 "못 읽었다 + 다음에 할 것" 하나로 덮는다. 사용자에게 필요한
    # 것은 원인 분류가 아니라 다음 행동이다.
    if not any(e["read"] for e in envelopes):
        return None

    return {"envelopes": envelopes, "symptoms": symptoms,
            "symptom_defs": engine.SYMPTOMS, "trace": t.stages,
            "from_photo": True}


_FORM_CUT = re.compile(
    r"(정|캡슐|캅셀|시럽|산|과립|주사|주|액|연고|크림|건조시럽|좌제|"
    r"현탁액|점안액|패치|환|겔|로션|분말|필름)")


# 먹는 약끼리는 제형이 달라도 같은 약으로 본다. 주사·연고·점안액은
# 사용 방법 자체가 달라서 따로 둔다. 봉투로 받아 입으로 먹는 약을 찾는
# 사람에게 주사제를 대표로 보여주면 안 된다.
_ORAL = ("정", "캡슐", "캅셀", "시럽", "산", "과립", "액", "환", "건조시럽",
         "현탁액", "분말", "필름", "트로키")


# 묶인 줄에 찍을 이름. 함량·제형만 다른 것들의 공통 앞부분을 쓴다.
#
# 레피졸정5 / 10 / 15 / 30밀리그램을 한 줄로 묶어놓고 대표를 "레피졸정15밀리그램"
# 으로 찍으면, 5밀리그램을 드시는 분의 브리핑에 15밀리그램이 인쇄된다. 판정은
# 성분만 보므로 결과는 같지만 출력물은 틀린다. 브리핑은 진료 때 보여주는 것이라
# 없는 숫자를 지어내면 안 된다. 공통 앞부분만 남기고 함량을 뗀다.
#
#   레피졸정5/10/15/30밀리그램   ->  레피졸정
#   아모잘탄엑스큐정 5/50/5/10…   ->  아모잘탄엑스큐정
#   뮤테란캡슐200 / 과립200      ->  뮤테란        (제형도 다르면 거기까지 잘린다)
_LABEL_TAIL = re.compile(r"[\s/.,\-\d]+$")
# 긴 것부터 본다. "건조시럽" 을 "시럽" 으로, "캡슐" 을 "산"(캡슐에는 없지만)
# 처럼 짧은 토큰이 먼저 걸리는 것을 막는다.
_LABEL_FORM = re.compile(
    r"(건조시럽|현탁액|점안액|캡슐|캅셀|과립|시럽|주사|연고|크림|좌제|패치|"
    r"로션|분말|필름|정|산|액|환|겔|주)")


def _group_label(names: list[str]) -> str:
    """묶인 품목명들에 찍을 이름.

    함량은 떼고 제형은 남긴다. 함량은 사용자가 모를 수 있고 판정에도 안
    쓰이지만, 제형은 사용자가 손에 들고 있어서 아는 정보다. 확인 화면은
    사람이 "내 약이 맞나" 를 보는 자리이므로 아는 정보를 지우면 안 된다.

      레피졸정5 / 10 / 30밀리그램     ->  레피졸정
      뮤테란캡슐200 / 과립200 / 캡슐100 ->  뮤테란 (캡슐·과립)
      도모호론연고 / 크림             ->  도모호론 (연고·크림)

    제형이 섞인 묶음은 카탈로그 기준 3,953개 중 214개(5%)다. 나머지
    95% 는 공통 앞부분에 제형이 그대로 남으므로 괄호가 붙지 않는다.
    """
    heads = [match._head(n) for n in names]
    p = _LABEL_TAIL.sub("", os.path.commonprefix(heads).strip())
    forms = []
    for h in heads:
        m = _LABEL_FORM.search(h[len(p):]) or _LABEL_FORM.search(h)
        f = m.group(0) if m else ""
        if f and f not in forms:
            forms.append(f)
    if len(forms) > 1:
        # 순서를 고정한다. 후보가 들어온 순서를 쓰면 같은 약이 질의에 따라
        # "뮤테란 (과립·캡슐)" 과 "뮤테란 (캡슐·과립)" 으로 갈린다. 이 이름은
        # 확인 화면과 브리핑에 그대로 찍히므로 같은 약은 항상 같아야 한다.
        return "%s (%s)" % (p, "·".join(sorted(forms)))
    return p if len(p) >= 2 else heads[0]


def _brand(name: str):
    """(상표, 먹는 약인가) — 같은 약인지 가르는 기준.

    뮤테란캡슐200밀리그람 -> (뮤테란, True)
    뮤테란과립200밀리그람 -> (뮤테란, True)     같이 묶인다
    뮤테란주사            -> (뮤테란, False)    따로 남는다
    타스펜8시간이알서방정  -> (타스펜, True)
    타이세펜8시간이알서방정 -> (타이세펜, True)  다른 상표라 안 묶인다
    """
    head = match._head(name)
    m = _FORM_CUT.search(head)
    brand = (head[:m.start()] if m and m.start() >= 2 else head).strip()
    form = m.group(0) if m else ""
    return (brand, form in _ORAL)


@app.get("/suggest")
async def suggest(q: str = "", limit: int = 6):
    """직접 입력 칸의 자동완성. 품목코드까지 같이 내려준다.

    사용자가 친 글자가 아니라 여기서 고른 품목코드가 넘어간다. 그래야
    오탈자가 원천적으로 안 생기고 "100mg" 과 "100밀리그램" 이 갈리지
    않는다. 봉투에 인쇄된 용량 표기와 허가 등재명의 표기가 다른 것이
    직접 입력에서 제일 흔한 실패였다.

    무거운 업로드와 달리 세마포어를 걸지 않는다. 타자 한 번에 한 번씩
    불리므로 막으면 입력이 끊긴다. 매칭은 이미 캐시가 받쳐준다.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse([])
    limit = max(1, min(limit, 10))
    hits = await asyncio.to_thread(match.search_all, q, limit * 4)

    # 성분을 같이 내려준다. 판정이 보는 것은 제품명이 아니라 성분이다.
    #
    # "타이레놀" 을 치면 여섯 개가 전부 같은 점수로 나오는데, 그중 넷은
    # 성분이 아세트아미노펜 하나로 같다. 어느 것을 골라도 판정이 같다.
    # 반대로 코푸 계열은 고르는 것마다 성분 구성이 다르다.
    #
    # 화면이 그 차이를 안 보여주니 사용자는 아무 근거 없이 하나를 골라야
    # 했다. 선택을 없애는 게 아니라 선택할 근거를 준다.
    def ing(med):
        seen, out = set(), []
        for i in med.ingredients:
            if i.name and i.name not in seen:
                seen.add(i.name)
                out.append(i.name)
        return out

    # 성분이 같으면 한 줄로 묶는다.
    #
    # 뮤테란을 치면 과립200 / 캡슐200 / 캡슐100 / 주사 네 줄이 나오는데
    # 성분이 전부 아세틸시스테인 하나다. 판정 결과가 같으므로 사용자에게
    # 물을 이유가 없는 선택이었다. 용량과 제형만 다른 같은 약을 네 줄로
    # 나눠 놓고 고르라고 한 셈이다.
    #
    # 묶는 기준은 (상표 + 성분) 이다. 성분만으로 묶으면 안 된다 —
    # 아세트아미노펜 하나짜리 해열제만 스무 개가 넘고 전부 다른 회사
    # 제품이다. 성분만 보면 타스펜과 타이세펜이 한 줄이 되어 사용자가
    # 찾던 약이 묻힌다.
    #
    # 상표는 이름에서 용량과 제형을 떼어낸 앞부분이다. 뮤테란캡슐200 과
    # 뮤테란과립200 은 상표가 같아 묶이고, 타스펜과 타이세펜은 갈린다.
    #
    # 대표 이름은 검색어에 가장 가까운 것을 쓴다. 가장 짧은 이름을 골랐더니
    # 뮤테란에서 "뮤테란주사" 가 대표가 됐다. 주사제는 사용자가 봉투로
    # 받아온 약이 아니다. 사용자가 친 글자와 가까운 쪽이 그 사람이 찾는
    # 약이다.
    out, seen = [], {}
    for med in hits:
        names = ing(med)
        key = (_brand(med.product_name), tuple(sorted(names))) if names             else ("", med.item_seq)
        if key in seen:
            g = seen[key]
            g["also"] += 1
            g["_members"].append(med.product_name)
            if med.confidence > g["_conf"] or (
                    med.confidence == g["_conf"]
                    and len(match._head(med.product_name)) < len(match._head(g["name"]))):
                g["name"], g["seq"] = med.product_name, med.item_seq
                g["_conf"] = med.confidence
            continue
        g = {"seq": med.item_seq, "name": med.product_name, "otc": med.otc,
             "dur": med.dur_covered, "ing": names, "also": 0,
             "_conf": med.confidence, "_members": [med.product_name]}
        seen[key] = g
        out.append(g)

    # 자르는 것은 전부 묶은 뒤에 한다. 묶는 도중에 끊으면 그 뒤 항목이
    # 기존 그룹에 합쳐질 수 있는데 세어지지 않아서 "외 N개" 가 실제보다
    # 작게 나온다.
    out = out[:limit]
    for g in out:
        g["name"] = _group_label(g["_members"]) if g["also"] else g["name"]
        g.pop("_conf", None)
        g.pop("_members", None)
    return JSONResponse(out)


@app.post("/confirm", response_class=HTMLResponse)
async def confirm(request: Request):
    """봉투별 입력 -> 후보 제시. 사람이 고르기 전에는 판정하지 않는다."""
    form = await request.form()
    t = Trace()

    def build():
        out = []
        for i in range(1, 6):
            # 자동완성에서 고른 것. 이미 품목이 정해졌으므로 다시 찾지 않는다.
            #
            # 값은 "품목코드" 또는 "품목코드|표시이름" 이다. 함량만 다른
            # 약을 한 줄로 묶어 보여줬을 때, 고른 뒤에 카탈로그 원래 이름을
            # 찍으면 안 된다. "레피졸정" 을 고른 사람 화면에
            # "레피졸정15밀리그램" 이 나오고, 우리 브리핑은 그 이름을
            # 의사에게 보여주라고 말한다. 사용자가 정하지 않은 용량이다.
            #
            # 사진에서 확정된 약은 다르다. OCR 이 봉투에 인쇄된 함량을
            # 실제로 읽었으므로 아는 정보다. 그건 그대로 보여준다.
            # 표시이름은 묶어서 고른 경우에만 붙는다.
            picked, disp = [], {}
            for v in form.getlist(f"seq{i}"):
                if not v:
                    continue
                seq, _, label = v.partition("|")
                seq = seq.strip()
                if seq not in disp:
                    picked.append(seq)
                if label.strip():
                    disp[seq] = label.strip()
            raw = (form.get(f"env{i}") or "").strip()
            lines = [x.strip() for x in raw.splitlines() if x.strip()]
            if not picked and not lines:
                continue
            env = _envelope(i, form.get(f"label{i}") or "", lines, t)
            if picked:
                chosen = _load_meds(picked, disp)
                t.count("picked", len(chosen))
                env["items"] = [{"query": m.product_name, "status": "auto",
                                 "auto": m, "candidates": [m]}
                                for m in chosen] + env["items"]
            out.append(env)
        return out

    with t.stage("matching"):
        envelopes = await asyncio.to_thread(build)

    # 아무것도 안 적고 제출한 경우. 그냥 두면 "이렇게 읽었습니다" 화면이
    # 약 0개로 뜬다. 읽은 게 없는데 읽었다고 말하는 막다른 길이다.
    # 자동완성 목록에서 클릭해야 품목이 정해지는데, 타이핑하는 사람은
    # 이름을 다 치고 엔터를 누르는 습관이 있어 이 경로로 쉽게 빠진다.
    if not envelopes:
        return tpl.TemplateResponse(request, "index.html", {
            "symptoms": engine.SYMPTOMS, "version": STATE["version"],
            "error": "약 이름을 적고 목록에서 골라주세요. 사진으로 "
                     "찾으시려면 위에서 '사진으로 찾기' 를 눌러주세요."})

    t.emit("confirm", envelopes=len(envelopes))
    return tpl.TemplateResponse(request, "confirm.html", {
        "envelopes": envelopes,
        "symptoms": form.getlist("symptom"),
        "symptom_defs": engine.SYMPTOMS, "trace": t.stages})


def _load_meds(seqs, display=None):
    """확정된 품목코드 -> Medication. 사람이 고른 것만 들어온다.

    1단(DUR)에 없으면 2단(허가목록)을 본다. 전에는 1단만 보고 없으면
    continue 했는데, 그러면 사람이 확인 화면에서 분명히 고른 약이
    결과에서 아무 말 없이 사라졌다. 실물 13장에서 auto 확정 43건 중
    20건(47%)이 2단이었다.

    2단은 이름만 있고 성분이 없다. 상호작용 판정을 못 한다. 그래서
    dur_covered=False 로 표시해 화면이 "판정 대상이 아니다" 를 말할 수
    있게 한다. 목록에서 지우는 것과 판정을 못 하는 것은 다르다.
    """
    from core.match import _catalog, _permit, _ingredients_cached
    from core.model import Medication
    by_seq = {r[0]: r for r in _catalog()}
    permit = dict(_permit())
    out = []
    for seq in seqs:
        r = by_seq.get(seq)
        if r:
            out.append(Medication(item_seq=r[0],
                                  product_name=(display or {}).get(seq) or r[1],
                                  otc=r[3],
                                  ingredients=list(_ingredients_cached(r[2])),
                                  confidence=1.0, confirmed=True))
            continue
        name = permit.get(seq)
        if name:
            out.append(Medication(item_seq=seq,
                                  product_name=(display or {}).get(seq) or name,
                                  otc="",
                                  ingredients=[], confidence=1.0,
                                  confirmed=True, dur_covered=False))
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
            # 같은 봉투 안 같은 품목은 한 번만 센다. OCR 이 같은 약을 두 줄로
            # 읽고(싸이메트정 / 싸아메트정) 매칭이 둘 다 같은 품목으로 정확히
            # 확정하면 사람이 둘 다 체크한다. 그러면 KABS 합산이 두 배가 되어
            # 없는 소견이 생긴다. 실물 약봉투에서 이렇게 났다.
            #   항콜린 부담 합계가 4점입니다 (검토 권장 3점 이상)
            #     2점 시메티딘 — 싸이메트정(시메티딘)
            #     2점 시메티딘 — 싸이메트정(시메티딘)
            # 한 종 2점이라 실제로는 권장 기준 아래다.
            #
            # 봉투가 다르면 접지 않는다. 다른 병원에서 같은 약을 받은 것을
            # 찾아내는 게 이 도구의 목적이다. 접는 범위는 봉투 안이다.
            seqs = list(dict.fromkeys(seqs))
            src = Source(index=i, label=(form.get(f"label{i}") or "").strip() or None)
            # 확인 화면이 "레피졸정" 이라고 보여준 것을 사용자가 확인했는데
            # 브리핑에 "레피졸정15밀리그램" 이 찍히면 안 된다. 확인한 것과
            # 다른 이름을 의사에게 보여주라고 말하게 된다. 표시이름을
            # 여기까지 들고 온다.
            #
            # 파이프를 가른 뒤에 중복을 제거한다. 먼저 제거하면 같은 약인데
            # 표시이름만 달라 둘로 세어진다.
            names, order = {}, []
            for v in seqs:
                seq, _, label = v.partition("|")
                seq = seq.strip()
                if seq not in names:
                    order.append(seq)
                    names[seq] = None
                if label.strip():
                    names[seq] = label.strip()
            src.medications = _load_meds(order, {k: v for k, v in names.items() if v})
            if src.medications:
                review.sources.append(src)

    with t.stage("engine"):
        findings = engine.analyze(review, symptoms)
        matrix = build_matrix(review)

    with t.stage("questions"):
        questions = engine.make_questions(findings, symptoms)

    # 판정은 위에서 끝났다. LLM 은 문장만 만진다.
    # 키가 없거나 호출이 죽거나 검증에 걸리면 위 템플릿이 그대로 나간다.
    with t.stage("polish"):
        questions, route, pstats = polish.polish(
            questions, engine.protected_terms(findings, symptoms))
    # 경로는 호출이 됐는지, 집계는 몇 문장이 통과했는지를 말한다.
    # 둘을 하나로 뭉치면 부분 거부가 안 보인다.
    t.count(f"polish_{route}")
    for k, n in pstats.items():
        t.count(f"polish_{k}", n)

    t.count("meds", len(review.all_meds))
    t.count("sources", len(review.sources))
    t.count("findings", len(findings))
    t.emit("result", symptoms=len(symptoms))

    return tpl.TemplateResponse(request, "result.html", {
        "review": review, "matrix": matrix, "findings": findings,
        "questions": questions, "trace": t.stages,
        "version": STATE["version"]["catalog_version"],
        "symptom_labels": [engine.SYMPTOM_LABEL.get(s, s) for s in symptoms]})
