#!/bin/bash
set -e # エラーが発生したらスクリプトを終了

# 変数 (CDKから渡される想定、またはスクリプト内で定義)
# ALB2_DNS_SSM_PARAM_NAME と REGION はCDKスタックからこのスクリプトに渡されるか、
# あるいはこのスクリプトを生成する際にCDKによって直接埋め込まれます。
# この例では、CDKがこのスクリプトを読み込み、これらの値を置換してからUserDataに設定する想定です。
ALB2_DNS_SSM_PARAM_NAME="__ALB2_DNS_SSM_PARAM_NAME__"
REGION="__REGION__"
NGINX_CONFIG_CONTENT_BASE64="__NGINX_CONFIG_CONTENT_BASE64__" # Base64エンコードされたnginx.confの内容

echo "Nginxセットアップスクリプトを開始します..."

# 1. OSパッケージのアップデートとNginxのインストール
echo "OSパッケージをアップデートし、Nginxをインストールします..."
yum update -y
amazon-linux-extras install nginx1 -y
echo "Nginxのインストールが完了しました。"

# 2. SSM Parameter StoreからALB2のDNS名を取得
echo "SSM Parameter StoreからALB2のDNS名を取得します (パラメータ名: ${ALB2_DNS_SSM_PARAM_NAME})..."
ALB2_DNS_NAME=$(aws ssm get-parameter --name "${ALB2_DNS_SSM_PARAM_NAME}" --region "${REGION}" --query Parameter.Value --output text)

if [ -z "$ALB2_DNS_NAME" ] || [ "$ALB2_DNS_NAME" == "None" ]; then
  echo "警告: ALB2 DNS名がSSMパラメータストアで見つからないか、値がNoneです。プレースホルダーを使用します。"
  ALB2_DNS_NAME="placeholder-alb2-dns.internal" # フォールバックDNS名
else
  echo "ALB2 DNS名として ${ALB2_DNS_NAME} を取得しました。"
fi

# 3. Nginx設定ファイルの配置とプレースホルダー置換
NGINX_CONFIG_PATH="/etc/nginx/conf.d/app_proxy_passthrough.conf"
echo "${NGINX_CONFIG_PATH} にNginx設定ファイルを配置します..."

# Base64デコードしてnginx.confの内容を復元し、ファイルに書き込む
echo "${NGINX_CONFIG_CONTENT_BASE64}" | base64 --decode > "${NGINX_CONFIG_PATH}"

echo "設定ファイル内のプレースホルダー __ALB2_DNS_NAME__ を ${ALB2_DNS_NAME} に置換します..."
sed -i "s|__ALB2_DNS_NAME__|${ALB2_DNS_NAME}|g" "${NGINX_CONFIG_PATH}"
echo "プレースホルダーの置換が完了しました。"

# (オプション) Nginxのメイン設定ファイル nginx.conf を確認・調整する場合
# 例: worker_processes auto; など
# sed -i 's/worker_processes  1;/worker_processes auto;/' /etc/nginx/nginx.conf

# 4. Nginxサービスの有効化と起動/再起動
echo "Nginxサービスを有効化し、再起動します..."
systemctl enable nginx
systemctl restart nginx # 設定変更を反映するためにrestart
echo "Nginxサービスが設定され、起動しました。"

echo "Nginxセットアップスクリプトが正常に完了しました。"
