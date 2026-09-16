"""접수번호 방식.

사진 한 장이 20~58초다. POST 하나를 그 시간 내내 붙잡고 있으면 폰 화면이
꺼질 때 연결이 끊기고 서버가 한 일이 버려진다. 약국 앞에 서 있는 사람이
사용자라 그 상황이 예외가 아니라 기본이다.

여기서 지켜야 하는 것이 셋이다.
  1. 접수번호는 곧 열쇠다. 로그인이 없으므로 번호를 아는 사람이 그 사람의
     복약 목록을 본다. 추측할 수 없어야 한다.
  2. 떠난 사람의 작업이 남은 사람을 막으면 안 된다. 작업자가 하나뿐이다.
  3. 결과를 오래 들고 있지 않는다. 메모리가 아니라 사생활 때문이다.
"""
import asyncio
import time

from core.jobs import Queue, Job, ABANDON


def run(coro_fn):
    """pytest-asyncio 를 새로 들이지 않는다. 테스트 전용 의존성 하나가
    배포 이미지와 갈라지면 나중에 왜 여기만 다른지 아무도 모른다.

    Queue.submit 이 create_task 를 부르므로 루프 안에서 불려야 한다.
    """
    return asyncio.run(coro_fn())


async def quick(x=None):
    return {"ok": x}


async def slow(x=None):
    await asyncio.sleep(0.3)
    return {"ok": x}


async def boom(x=None):
    raise RuntimeError("터졌다")


async def drain(q, job, limit=5.0):
    t0 = time.time()
    while job.state in ("queued", "running") and time.time() - t0 < limit:
        await asyncio.sleep(0.02)
    return job.state


# -- 1. 접수번호는 추측할 수 없어야 한다 -------------------------------------

def test_접수번호가_추측_가능하면_안_된다():
    ids = [Job().id for _ in range(200)]
    assert len(set(ids)) == 200, "번호가 겹친다"
    assert all(len(i) >= 20 for i in ids), "번호가 너무 짧다"
    # 순번이나 시각 기반이면 정렬했을 때 이웃이 비슷하다. 앞 4자가
    # 몰리는지 본다.
    heads = {i[:4] for i in ids}
    assert len(heads) > 190, "번호에 규칙이 보인다"


def test_로그용_짧은_번호는_전체를_노출하지_않는다():
    j = Job()
    assert len(j.short) <= 8
    assert j.short != j.id


# -- 2. 떠난 작업이 남은 사람을 막으면 안 된다 --------------------------------

def test_기다리다_떠난_작업은_일하지_않는다():
    async def _t():
        q = Queue(slow)
        gone = q.submit(1)
        gone.seen = time.time() - ABANDON - 1      # 오래 안 물어본 상태
        alive = q.submit(1)
        await drain(q, alive)
        assert gone.state == "abandoned", "떠난 작업이 작업자를 잡아먹었다"
        assert alive.state == "done"

    run(_t)


def test_떠난_작업은_줄에서_빠져_뒷사람_예상시간이_정직해진다():
    async def _t():
        q = Queue(quick)
        gone = q.submit(2)
        gone.seen = time.time() - ABANDON - 1
        q._sweep()
        assert q.eta_sec() == 0, "떠난 작업이 대기시간에 계속 잡힌다"

    run(_t)


def test_물어보면_살아있는_것으로_본다():
    async def _t():
        q = Queue(quick)
        j = q.submit(1)
        j.seen = time.time() - ABANDON - 1
        q.get(j.id, touch=True)                     # 브라우저가 물어봤다
        q._sweep()
        assert j.state != "abandoned"

    run(_t)


# -- 3. 줄 세우기 -------------------------------------------------------------

def test_한_번에_한_건씩만_돈다():
    async def _t():
        seen = []

        async def watch(x=None):
            seen.append(len([s for s in seen if s == "in"]))
            seen.append("in")
            await asyncio.sleep(0.1)
            seen.remove("in")
            return {}

        q = Queue(watch)
        jobs = [q.submit(1) for _ in range(3)]
        for j in jobs:
            await drain(q, j)
        assert all(v == 0 for v in seen if isinstance(v, int)), "둘 이상이 동시에 돌았다"

    run(_t)


def test_예상_대기가_길면_접수하지_않는다():
    async def _t():
        q = Queue(slow, max_wait_sec=30)            # 사진 한 장 25초 가정
        assert not q.full(1)
        q.submit(1)
        assert q.full(1), "줄이 길어졌는데 계속 받는다"

    run(_t)


def test_앞에_몇_번째인지_알_수_있다():
    async def _t():
        q = Queue(slow)
        a, b = q.submit(1), q.submit(1)
        assert q.position(a.id) == 0
        assert q.position(b.id) == 1
        await drain(q, b)

    run(_t)


# -- 4. 실패해도 조용히 사라지지 않는다 ---------------------------------------

def test_작업이_터져도_접수번호는_살아있다():
    async def _t():
        q = Queue(boom)
        j = q.submit(1)
        await drain(q, j)
        assert j.state == "error", "실패가 전달되지 않으면 사용자는 영원히 기다린다"
        assert j.error == "RuntimeError"
        assert q.get(j.id) is not None

    run(_t)


# -- 5. 오래된 결과는 버린다 --------------------------------------------------

def test_시간이_지난_결과는_버린다():
    async def _t():
        q = Queue(quick, ttl=0)
        j = q.submit(1)
        await drain(q, j)
        assert q.get(j.id) is None, "복약 목록을 계속 들고 있다"

    run(_t)


def test_끝난_직후에는_결과가_남아있다():
    async def _t():
        q = Queue(quick, ttl=900)
        j = q.submit(1)
        await drain(q, j)
        assert q.get(j.id) is not None
        assert q.get(j.id).result == {"ok": None}

    run(_t)
