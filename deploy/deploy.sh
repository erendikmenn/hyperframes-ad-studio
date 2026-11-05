#!/usr/bin/env bash
# Installs and updates Ad Studio in a customer's AWS account.
#
#   AWS_PROFILE=customer deploy/deploy.sh cert   studio.brand.com   # certificate, prints the record to add
#   AWS_PROFILE=customer deploy/deploy.sh up     studio.brand.com   # first install
#   AWS_PROFILE=customer deploy/deploy.sh update studio.brand.com   # ship the current code
#   AWS_PROFILE=customer deploy/deploy.sh info   studio.brand.com   # DNS record, login, status
#
# AWS_REGION picks where the server runs (default us-east-1).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CMD="${1:-}"
DOMAIN="${2:-}"
[ -n "$CMD" ] && [ -n "$DOMAIN" ] || { sed -n '2,9p' "$0"; exit 1; }
STACK="ad-studio-${DOMAIN//./-}"

cert_arn() {
  aws acm list-certificates --region us-east-1 --includes keyTypes=RSA_2048,EC_prime256v1 \
    --query "CertificateSummaryList[?DomainName=='$DOMAIN'] | [0].CertificateArn" --output text
}
output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

cert() {
  local arn; arn="$(cert_arn)"
  if [ "$arn" = "None" ] || [ -z "$arn" ]; then
    arn="$(aws acm request-certificate --region us-east-1 --domain-name "$DOMAIN" \
      --validation-method DNS --query CertificateArn --output text)"
    sleep 10
  fi
  local status name value
  read -r status name value < <(aws acm describe-certificate --region us-east-1 --certificate-arn "$arn" \
    --query "[Certificate.Status, Certificate.DomainValidationOptions[0].ResourceRecord.Name, Certificate.DomainValidationOptions[0].ResourceRecord.Value]" \
    --output text)
  echo "Sertifika durumu: $status"
  if [ "$status" != "ISSUED" ]; then
    echo
    echo "Müşterinin DNS'ine eklenecek doğrulama kaydı:"
    echo "  Tür:   CNAME"
    echo "  Ad:    $name"
    echo "  Değer: $value"
  fi
}

ship() {
  local bucket instance password origin key command status
  bucket="$(output ReleaseBucket)"; instance="$(output InstanceId)"
  password="$(output AppPasswordArn)"; origin="$(output OriginSecretArn)"
  key="release-$(date +%Y%m%d-%H%M%S).tgz"

  echo "Kod paketleniyor ve yükleniyor..."
  COPYFILE_DISABLE=1 tar -czf "/tmp/$key" -C "$ROOT" --exclude .git --exclude runs --exclude library/references \
    --exclude deploy --exclude __pycache__ --exclude '*.log' --exclude .env .
  aws s3 cp --region "$REGION" --only-show-errors "/tmp/$key" "s3://$bucket/$key"
  rm -f "/tmp/$key"

  echo "Sunucu uyandırılıyor..."
  aws ec2 start-instances --region "$REGION" --instance-ids "$instance" >/dev/null 2>&1 || true
  aws ec2 wait instance-running --region "$REGION" --instance-ids "$instance"
  until [ "$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$instance" --query 'InstanceInformationList[0].PingStatus' --output text)" = "Online" ]; do
    sleep 10
  done

  echo "İmaj sunucuda derleniyor (ilk kurulumda 5-10 dakika)..."
  local script params
  script=$(cat <<SCRIPT
set -eu
export PATH=\$PATH:/snap/bin
until [ -f /var/lib/server-ready ]; do sleep 5; done
touch /var/lib/no-sleep
aws s3 cp --region $REGION --only-show-errors s3://$bucket/$key /tmp/release.tgz
rm -rf /opt/app && mkdir -p /opt/app && tar -xzf /tmp/release.tgz -C /opt/app && rm /tmp/release.tgz
umask 077
{
  echo "APP_PASSWORD=\$(aws secretsmanager get-secret-value --region $REGION --secret-id $password --query SecretString --output text)"
  echo "ORIGIN_SECRET=\$(aws secretsmanager get-secret-value --region $REGION --secret-id $origin --query SecretString --output text)"
  echo "AWS_REGION=$REGION"
} > /opt/app.env
cd /opt/app
docker build --build-arg FRESH=\$(date +%s) -t ad-studio .
docker rm -f ad-studio 2>/dev/null || true
docker run -d --name ad-studio --restart unless-stopped -p 8080:8080 --env-file /opt/app.env -v /data:/data ad-studio
docker image prune -f
rm -f /var/lib/no-sleep
SCRIPT
)
  params="$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["3000"]}))' "$script")"
  command="$(aws ssm send-command --region "$REGION" --instance-ids "$instance" --timeout-seconds 3000 \
    --document-name AWS-RunShellScript --parameters "$params" --query Command.CommandId --output text)"
  while :; do
    status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$command" --instance-id "$instance" \
      --query Status --output text 2>/dev/null || echo Pending)"
    case "$status" in Success) break ;; Failed|Cancelled|TimedOut)
      aws ssm get-command-invocation --region "$REGION" --command-id "$command" --instance-id "$instance" \
        --query StandardErrorContent --output text
      echo "Kurulum başarısız: $status"; exit 1 ;; esac
    sleep 10
  done
  echo "Uygulama çalışıyor."
}

info() {
  echo "Müşterinin DNS'ine eklenecek kayıt:"
  echo "  Tür:   CNAME (kök alan adında ALIAS/ANAME)"
  echo "  Ad:    $DOMAIN"
  echo "  Değer: $(output CloudFrontDomain)"
  echo
  echo "Adres: https://$DOMAIN"
  echo "Şifre: aws secretsmanager get-secret-value --region $REGION --secret-id $(output AppPasswordArn) --query SecretString --output text"
}

case "$CMD" in
  cert) cert ;;
  up)
    if aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" >/dev/null 2>&1; then
      echo "Bu kurulum zaten var. Kod göndermek için: deploy/deploy.sh update $DOMAIN"; exit 1
    fi
    arn="$(cert_arn)"
    state="$(aws acm describe-certificate --region us-east-1 --certificate-arn "$arn" --query Certificate.Status --output text 2>/dev/null || echo NONE)"
    [ "$state" = "ISSUED" ] || { echo "Sertifika hazır değil ($state). Önce: deploy/deploy.sh cert $DOMAIN"; exit 1; }
    prefix="$(aws ec2 describe-managed-prefix-lists --region "$REGION" \
      --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
      --query 'PrefixLists[0].PrefixListId' --output text)"
    echo "AWS kaynakları oluşturuluyor (CloudFront 5-15 dakika sürer)..."
    aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file "$ROOT/deploy/template.yaml" \
      --capabilities CAPABILITY_IAM --parameter-overrides "DomainName=$DOMAIN" "CertificateArn=$arn" "CloudFrontPrefixList=$prefix"
    ship
    info
    ;;
  update) ship ;;
  info) info ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
