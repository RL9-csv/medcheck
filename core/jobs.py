"""사진 읽기를 요청과 분리한다.

왜 필요한가
-----------
사진 한 장이 20~58초다. 그동안 POST 하나가 계속 열려 있으면, 폰 화면이
꺼지는 순간 연결이 끊기고 서버가 한 일이 전부 버려진다. 사용자는 처음부터
다시 찍어야 한다. 이 도구의 사용자는 약국 앞에 서 있는 사람이라 그 상황이
예외가 아니라 기본이다.

접수번호를 먼저 돌려주고 브라우저가 물어보게 하면, 폰이 잠겨도 서버는
계속 일한다. 다시 켜면 이어서 물어보고 결과를 받는다.

접수번호는 곧 열쇠다
--------------------
/job/{id} 와 /confirm/{id} 에는 로그인이 없다. 번호를 아는 사람이 그
사람의 복약 목록을 본다. 공개 투표 기간에 모르는 사람들이 동시에 쓰므로
번호는 추측할 수 없어야 한다. secrets 로 128비트를 뽑는다. 순번이나
시각 기반은 쓰지 않는다. 이건 나중에 고칠 수 있는 종류가 아니다.

전제
----
프로세스가 하나라는 전제다. uvicorn --workers 를 늘리면 워커마다 다른
dict 를 보게 되어 접수번호가 "없는 번호" 가 된다. Dockerfile 이 워커를
지정하지 않는다(기본 1개). 늘릴 일이 생기면 여기를 먼저 바꿔야 한다.

원본 이미지는 여기 남기지 않는다. 읽기가 끝나면 즉시 버리고 결과만 남긴다.
"""
import asyncio
import secrets
import time

TTL = 15 * 60        # 결과를 들고 있는 시간(초)
ABANDON = 60         # 이 시간 동안 안 물어보면 버려진 것으로 본다
# 대기시간 추정용. 흔한 쪽(20초)이 아니라 관측 상한(58초) 쪽으로 잡는다.
#
# 25로 잡았더니 동시 4건이 전부 영수증형(129줄)일 때 네 번째에게 "75초"
# 라고 말하고 실제로는 175초가 걸렸다. 2분 거절선도 같이 뚫린다 — 추정
# 으로는 75초라 받아놓고 실제로는 3분을 기다리게 한다.
#
# 덜 기다리면 기분이 좋고 더 기다리면 고장으로 보인다. 과소 약속이 나쁜
# 쪽이다. 줄 수는 OCR 전에 알 수 없어 정확한 추정이 불가능하고, 그렇다면
# 비관 쪽으로 틀리는 편이 낫다.
SEC_PER_PHOTO = 55
MAX_WAIT_SEC = 120   # 예상 대기가 이보다 길면 접수하지 않는다


class Job:
    __slots__ = ("id", "state", "photos", "made", "seen",
                 "started", "done_at", "result", "error")

    def __init__(self, photos: int = 1):
        # 순번·시각 기반 금지. 이 값이 접근 권한이다.
        self.id = secrets.token_urlsafe(16)
        self.state = "queued"        # queued | running | done | error | abandoned
        self.photos = max(1, photos)
        self.made = time.time()
        self.seen = self.made        # 마지막으로 물어본 시각
        self.started = None
        self.done_at = None
        self.result = None
        self.error = None

    @property
    def short(self) -> str:
        """로그에 남길 때 쓴다. 전체를 남기면 로그를 본 사람이 결과를 연다."""
        return self.id[:6]

    def elapsed_ms(self) -> int:
        end = self.done_at or time.time()
        return int((end - (self.started or self.made)) * 1000)


class Queue:
    """접수번호를 발급하고 순서대로 한 건씩 처리한다.

    동시 실행은 1건이다. t3.small 은 물리 코어가 하나라 동시에 돌려도
    처리량이 안 늘고 모두의 대기만 길어진다(동시 1/2/3건 처리량이
    0.041 / 0.039 / 0.042 req/s 로 같았다). 그래서 병렬로 만들지 않고
    줄을 세운다.

    전에는 넘치면 즉시 503 으로 돌려보냈다. 줄이 생겼으니 짧은 줄은
    기다리게 하고, 예상 대기가 길면 그때 거절한다. 개수가 아니라 시간으로
    거절한다 — "앞에 3명" 은 사용자에게 아무 정보가 아니다. 사진 장수에
    거의 선형이라 장수로 추정할 수 있다.
    """

    def __init__(self, worker, max_wait_sec: int = MAX_WAIT_SEC, ttl: int = TTL):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []      # 접수 순서
        self._lock = asyncio.Semaphore(1)
        self._tasks: set = set()
        self._worker = worker
        self.max_wait_sec = max_wait_sec
        self.ttl = ttl

    # -- 조회 ---------------------------------------------------------------

    def get(self, job_id: str, touch: bool = False) -> Job | None:
        # 청소보다 먼저 살아있음을 기록한다. 순서가 반대면, 폰이 오래
        # 잠겼다 깨어나 물어보는 바로 그 순간에 청소가 먼저 돌아 작업을
        # 버린다. 이 기능이 지원하려는 상황이 정확히 그 경우다.
        job = self._jobs.get(job_id)
        if job and touch:
            job.seen = time.time()
        self._sweep()
        return self._jobs.get(job_id)

    def position(self, job_id: str) -> int:
        """앞에 몇 명 있는가. 자기 차례면 0."""
        try:
            return self._order.index(job_id)
        except ValueError:
            return 0

    def eta_sec(self, job_id: str | None = None) -> int:
        """앞선 작업들의 사진 장수로 추정한 대기 시간."""
        ahead = self._order if job_id is None else self._order[:self.position(job_id)]
        return sum(self._jobs[k].photos for k in ahead
                   if k in self._jobs) * SEC_PER_PHOTO

    def full(self, photos: int = 1) -> bool:
        self._sweep()
        return self.eta_sec() + photos * SEC_PER_PHOTO > self.max_wait_sec

    # -- 접수 ---------------------------------------------------------------

    def submit(self, photos: int, *args, **kwargs) -> Job:
        """접수하고 바로 돌려준다. 실제 작업은 뒤에서 돈다."""
        self._sweep()
        job = Job(photos)
        self._jobs[job.id] = job
        self._order.append(job.id)
        task = asyncio.create_task(self._run(job, *args, **kwargs))
        # 태스크를 아무 데서도 안 붙들면 GC 가 중간에 수거할 수 있다.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, *args, **kwargs):
        try:
            async with self._lock:
                # 차례가 왔는데 그 사이에 떠난 사람이면 일하지 않는다.
                #
                # 탭을 닫아도 태스크는 계속 돈다. 작업자가 하나뿐이라
                # 58초짜리 유령 작업이 뒤의 진짜 사용자를 막는다. 폰 잠김을
                # 고치려는 기능이 폰을 놓고 간 사람 때문에 막히면 안 된다.
                # 폴링이 이미 살아있음 신호를 준다.
                if time.time() - job.seen > ABANDON:
                    job.state = "abandoned"
                    return
                job.state = "running"
                job.started = time.time()
                job.result = await self._worker(*args, **kwargs)
                job.state = "done"
        except Exception as e:
            # 실패해도 접수번호는 살려둔다. 대기 화면이 "실패했다" 를
            # 말할 수 있어야 한다. 조용히 사라지면 사용자는 영원히 기다린다.
            job.state = "error"
            job.error = type(e).__name__
        finally:
            job.done_at = time.time()
            if job.id in self._order:
                self._order.remove(job.id)

    # -- 청소 ---------------------------------------------------------------

    def _sweep(self):
        """오래된 결과를 버린다.

        TTL 을 짧게 잡는 근거는 메모리가 아니라 사생활이다. 결과에는 그
        사람의 복약 목록이 들어 있다. 오래 들고 있을 이유가 없다. 폰을
        잠갔다 다시 여는 데는 15분이면 충분하다.
        """
        now = time.time()
        cut = now - self.ttl
        for k in [k for k, j in self._jobs.items()
                  if (j.done_at or j.made) < cut]:
            self._jobs.pop(k, None)
            if k in self._order:
                self._order.remove(k)
        # 아직 차례가 안 왔는데 오래 안 물어본 것은 줄에서 뺀다. 그래야
        # 뒷사람의 예상 대기시간이 정직해진다.
        for k in list(self._order):
            j = self._jobs.get(k)
            if j and j.state == "queued" and now - j.seen > ABANDON:
                j.state = "abandoned"
                j.done_at = now
                self._order.remove(k)
