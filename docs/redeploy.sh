#!/bin/bash
# 새 이미지를 먼저 다 굽고, 성공했을 때만 컨테이너를 바꾼다.
# 빌드 중에도 기존 컨테이너가 계속 돈다. 다운타임은 교체하는 수 초뿐이다.
# 심사 기간(9/21~10/17)에도 같은 순서로 배포한다.
set -e
cd /opt/medcheck

echo "== 1. 코드 =="
sudo git fetch origin
sudo git reset --hard origin/main
sudo git log --oneline -3

echo "== 2. 빌드 (기존 컨테이너는 계속 동작) =="
TAG="medcheck:$(date +%Y%m%d-%H%M)"
sudo docker build -t "$TAG" -t medcheck:next .

echo "== 3. 교체 =="
# 이름만 바꿔서는 포트가 안 비워진다. 첫 실행에서 여기서 깨졌다.
#   Bind for :::80 failed: port is already allocated
# 반드시 멈춘 다음에 새것을 띄운다. 이 몇 초가 유일한 다운타임이다.
sudo docker rm -f medcheck-stale 2>/dev/null || true
# docker inspect 는 컨테이너와 이미지를 둘 다 본다. 이미지 이름이
# medcheck 라서 컨테이너가 없어도 성공했고, 가드가 뚫린 채로
# rm -f medcheck-old 가 돌아 돌던 서비스를 지웠다. 실제로 내렸다.
# 컨테이너만 보려면 docker container inspect 여야 한다.
if sudo docker container inspect medcheck >/dev/null 2>&1; then
  sudo docker rm -f medcheck-old 2>/dev/null || true
  sudo docker rename medcheck medcheck-old
  sudo docker stop medcheck-old
fi
sudo docker run -d --name medcheck --restart always \
  -p 80:7860 -e OCR_THREADS=2 -e OMP_NUM_THREADS=2 \
  --log-opt max-size=10m --log-opt max-file=3 \
  medcheck:next

echo "== 4. 기동 확인 =="
OK=0
for i in $(seq 1 45); do
  if curl -sf -m 3 http://127.0.0.1/health > /dev/null; then echo "   up (${i}회)"; OK=1; break; fi
  sleep 2
done

if [ "$OK" != "1" ]; then
  echo "!! 새 컨테이너가 안 뜬다. 되돌린다."
  sudo docker rename medcheck medcheck-stale
  sudo docker rm -f medcheck-stale
  sudo docker rename medcheck-old medcheck
  sudo docker start medcheck
  for i in $(seq 1 30); do
    curl -sf -m 3 http://127.0.0.1/health > /dev/null && { echo "   옛것 복구됨"; exit 1; }
    sleep 2
  done
  echo "!! 복구도 실패했다. 사람이 봐야 한다."
  exit 2
fi

echo "== 5. 옛 컨테이너 정리 =="
sudo docker rm -f medcheck-old 2>/dev/null || true
sudo docker tag medcheck:next medcheck
echo
sudo docker ps --format "{{.Names}} | {{.Status}} | {{.Image}}"
