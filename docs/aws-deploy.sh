#!/bin/bash
# AWS CloudShell 에 붙여넣는다. 서버를 만들고 medcheck 를 띄운다.
# 리전이 ap-northeast-2(서울)인지 먼저 확인할 것.
set -e

REGION=ap-northeast-2
NAME=medcheck

echo "== 1. 보안 그룹 =="
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text --region $REGION)
SG=$(aws ec2 create-security-group --group-name $NAME-sg \
     --description "medcheck web" --vpc-id $VPC \
     --query GroupId --output text --region $REGION 2>/dev/null \
     || aws ec2 describe-security-groups --group-names $NAME-sg \
        --query 'SecurityGroups[0].GroupId' --output text --region $REGION)
aws ec2 authorize-security-group-ingress --group-id $SG \
  --protocol tcp --port 80 --cidr 0.0.0.0/0 --region $REGION 2>/dev/null || true
echo "   $SG"

echo "== 2. 최신 Ubuntu 이미지 =="
AMI=$(aws ssm get-parameters --names \
  /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameters[0].Value' --output text --region $REGION)
echo "   $AMI"

echo "== 3. 부팅 스크립트 =="
# 빌드가 메모리를 많이 쓴다. 2GB 인스턴스라 스왑을 먼저 만든다.
cat > /tmp/userdata.sh <<'UD'
#!/bin/bash
exec > /var/log/medcheck-setup.log 2>&1
set -x
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
apt-get update
apt-get install -y docker.io git
systemctl enable --now docker
cd /opt
git clone https://github.com/RL9-csv/medcheck.git
cd medcheck
docker build -t medcheck .
docker run -d --name medcheck --restart always \
  -p 80:7860 -e OCR_THREADS=2 -e OMP_NUM_THREADS=2 \
  --log-opt max-size=10m --log-opt max-file=3 \
  medcheck
echo "SETUP DONE"
UD

echo "== 4. 인스턴스 =="
ID=$(aws ec2 run-instances --image-id $AMI --instance-type t3.small \
  --security-group-ids $SG --region $REGION \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3}' \
  --user-data file:///tmp/userdata.sh \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "   $ID"

echo "== 5. 고정 IP =="
ALLOC=$(aws ec2 allocate-address --domain vpc --region $REGION \
        --query AllocationId --output text)
aws ec2 wait instance-running --instance-ids $ID --region $REGION
aws ec2 associate-address --instance-id $ID --allocation-id $ALLOC --region $REGION >/dev/null
IP=$(aws ec2 describe-addresses --allocation-ids $ALLOC --region $REGION \
     --query 'Addresses[0].PublicIp' --output text)

echo
echo "===================================================="
echo "  인스턴스  $ID"
echo "  주소      http://$IP"
echo
echo "  빌드에 10~15분 걸린다. 그동안은 접속이 안 된다."
echo "  확인:  curl http://$IP/health"
echo "===================================================="
