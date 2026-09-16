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
        q = Queue(slow, max_wait_sec=60)            # 사진 한 장 55초 추정
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


# -- 6. 대기시간을 과소 약속하지 않는다 ---------------------------------------

def test_대기시간_추정은_관측_상한_쪽이다():
    # 흔한 쪽(20초)으로 잡았더니, 동시 4건이 전부 영수증형(129줄)일 때
    # 네 번째에게 "75초" 라고 말하고 실제로는 175초가 걸렸다. 2분 거절선도
    # 같이 뚫렸다. 덜 기다리면 기분이 좋고 더 기다리면 고장으로 보인다.
    from core.jobs import SEC_PER_PHOTO
    assert SEC_PER_PHOTO >= 55, "추정이 관측 상한보다 낮으면 과소 약속이 된다"


def test_거절선이_실제_소요_기준으로_동작한다():
    async def _t():
        # 한 장 55초 추정, 상한 120초면 앞에 두 건까지만 받는다.
        q = Queue(slow, max_wait_sec=120)
        assert not q.full(1)
        q.submit(1)
        assert not q.full(1)
        q.submit(1)
        assert q.full(1), "실제로 3분을 기다릴 사람을 받고 있다"

    run(_t)


# -- 7. 멈춘 작업이 큐를 영원히 막지 않는다 -----------------------------------

def test_멈춘_작업은_상한에서_끊긴다():
    # 예외가 터지는 경우는 _run 이 받아낸다. 문제는 던지지 않고 그냥 멈추는
    # 경우다. 작업자가 하나뿐이라 그 한 건이 큐를 영원히 막는다. 컨테이너는
    # 살아 있으니 재시작 정책도 안 걸리고 아무도 모른다.
    async def _t():
        async def hang(x=None):
            await asyncio.sleep(30)

        import core.jobs as J
        old, J.TIMEOUT = J.TIMEOUT, 0.2
        try:
            q = Queue(hang)
            j = q.submit(1)
            await drain(q, j, limit=3.0)
            assert j.state == "error", "멈춘 작업이 안 끊겼다"
            assert j.error == "Timeout"
        finally:
            J.TIMEOUT = old

    run(_t)


def test_멈춘_작업_뒤에_기다리던_사람은_처리된다():
    async def _t():
        calls = []

        async def first_hangs(x=None):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(30)    # 첫 건만 멈춘다
            return {"ok": True}

        import core.jobs as J
        old, J.TIMEOUT = J.TIMEOUT, 0.2
        try:
            q = Queue(first_hangs)
            stuck = q.submit(1)
            after = q.submit(1)
            await drain(q, after, limit=5.0)
            assert stuck.state == "error"
            assert after.state == "done", "앞사람이 멈춰서 뒷사람이 막혔다"
        finally:
            J.TIMEOUT = old

    run(_t)
