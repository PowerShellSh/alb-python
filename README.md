# FastAPIバックエンドとAWSインフラストラクチャ (プランB)

## 1. プロジェクト概要

このプロジェクトは、AWS上にスケーラブルでセキュアなFastAPIバックエンドアプリケーションを構築するためのものです。認証にはAWS Cognitoを使用し、リクエストはApplication Load Balancer (ALB) とNginxリバースプロキシを経由してFastAPIアプリケーション (ECS Fargate上で実行) にルーティングされます。

このREADMEでは、**プランB**のアーキテクチャを採用しています。プランBでは、フロントのALB (ALB1) がCognitoと連携してユーザー認証を行い、発行されたJWTをHTTPヘッダーに付与します。EC2上で動作するNginxは、このリクエストを検証せずにバックエンドのALB (ALB2) にそのまま転送（パススルー）します。最終的なJWTの検証とユーザー情報の利用は、ALB2の背後にあるFastAPIアプリケーション自身が行います。

インフラストラクチャの構築と管理にはAWS CDK (Cloud Development Kit) を使用し、アプリケーションはDockerコンテナとしてデプロイされます。

## 2. アーキテクチャ (プランB)

1.  **ユーザー**: アプリケーションにアクセスします。
2.  **ALB1 (フロントALB + Cognito認証)**:
    * インターネットからのリクエストを受け付けます。
    * AWS Cognitoと連携し、ユーザー認証を行います。
    * 認証成功後、Cognitoが発行したJWT (アクセストークン) をHTTPヘッダー (`x-amzn-oidc-accesstoken` など) に付与します。
    * JWTが付与されたリクエストを、EC2上で稼働するNginxに転送します。
3.  **EC2 (Nginxリバースプロキシ)**:
    * ALB1からリクエストを受け取ります。
    * **JWTの検証は行わず**、リクエストと関連ヘッダーをそのままバックエンドのALB2に転送します (パススルー)。
    * 主にリバースプロキシとしての役割を担います。
4.  **ALB2 (バックエンドALB)**:
    * Nginxからの内部トラフィックを受け付けます。
    * FastAPIバックエンドアプリケーション群 (ECS Fargateタスク) へリクエストを負荷分散します。
5.  **FastAPIバックエンドアプリケーション (ECS Fargate)**:
    * ALB2経由でリクエストを受け取ります。
    * リクエストヘッダーからJWTを取得し、`cognito_utils.py` を使用してその有効性を検証します。
    * 検証済みのユーザークレームに基づいて、保護されたリソースへのアクセスを提供したり、ユーザー固有の処理を実行したりします。

[アーキテクチャ図のプレースホルダー - 必要に応じて図を挿入してください]

## 3. 主要コンポーネント

* **AWS CDK**: インフラストラクチャをコードとして定義し、プロビジョニングします (Pythonを使用)。
* **FastAPI**: 高パフォーマンスなPythonウェブフレームワークで、バックエンドAPIを構築します。
* **Nginx**: EC2上で動作する高性能リバースプロキシ。
* **AWS Cognito**: ユーザー認証と認可サービス。ユーザープールを使用してユーザー情報を管理します。
* **Amazon ECS (Fargate)**: FastAPIアプリケーションをコンテナとしてサーバーレスで実行します。
* **Amazon ECR**: FastAPIアプリケーションのDockerイメージを保存・管理します。
* **Amazon ALB**: L7ロードバランサー。フロント用とバックエンド用に2つ使用します。
* **Amazon VPC**: AWSリソースをホストするための隔離されたプライベートネットワーク。
* **Docker**: FastAPIアプリケーションをコンテナ化します。

## 4. ディレクトリ構造 (例)
.
├── app/                      # FastAPIアプリケーションのソースコード
│   ├── init.py
│   ├── main_app.py           # FastAPIメインアプリケーション (JWT検証ロジックを含む)
│   └── cognito_utils.py      # Cognito JWT検証ユーティリティ
├── cdk_project/              # AWS CDKプロジェクト
│   ├── app.py                # CDKアプリケーションのエントリポイント
│   ├── cdk_scenario_b_stack.py # メインのCDKスタック定義
│   ├── cdk.json
│   └── requirements.txt      # CDKのPython依存関係
├── scripts/                  # (オプション) 補助スクリプト (例: Nginx起動スクリプト)
├── Dockerfile                # FastAPIアプリケーションのDockerイメージ定義
├── requirements.txt          # FastAPIアプリケーションのPython依存関係 (pip用)
└── README.md                 # このファイル

## 5. セットアップとデプロイ手順

### 5.1. 前提条件

以下のツールがローカルマシンにインストールされ、設定済みであること:

* **Node.js と npm**: AWS CDKの実行に必要。
    * 確認: `node -v`, `npm -v`
* **AWS CLI**: AWSリソースの操作と認証情報の設定に必要。
    * 確認: `aws --version`
    * 設定: `aws configure` (アクセスキー、シークレットキー、リージョンを設定)
* **AWS CDK Toolkit**: CDKコマンドの実行に必要。
    * 確認: `cdk --version`
    * インストール: `npm install -g aws-cdk`
* **Docker**: FastAPIアプリケーションのコンテナイメージビルドに必要。
    * 確認: `docker --version`
* **Python**: FastAPIアプリケーション開発とCDKコード記述に必要 (例: Python 3.9+)。
    * 確認: `python --version` または `python3 --version`
* **(オプション) Poetry**: Pythonの依存関係管理ツール (pipと`requirements.txt`の代替)。

### 5.2. AWS CDKのブートストラップ

CDKを初めて使用するAWSアカウントとリージョンの組み合わせに対して、`cdk bootstrap` を実行します。これにより、CDKがアセットを保存するためのS3バケットなどが作成されます。

```bash
cdk bootstrap aws://YOUR_AWS_ACCOUNT_ID/YOUR_AWS_REGION
# 例: cdk bootstrap aws://123456789012/ap-northeast-1
### 5.3. FastAPIアプリケーションのDockerイメージ準備

1.  **Dockerfileの確認/作成**: プロジェクトルートにある `Dockerfile` を確認または作成します。
    (例: `python:3.12-slim` ベースで、`requirements.txt` を使用するDockerfile)
2.  **`requirements.txt` (または `pyproject.toml`) の準備**: FastAPIアプリケーションの依存関係を定義します。
3.  **Dockerイメージのビルド**:
    ```bash
    docker build -t my-fastapi-app .
    ```
4.  **Amazon ECRリポジトリの作成**: AWSコンソールまたはAWS CLIで、Dockerイメージを保存するためのECRリポジトリを作成します (例: `my-fastapi-app-repo`)。
5.  **ECRへのログイン**:
    ```bash
    aws ecr get-login-password --region YOUR_AWS_REGION | docker login --username AWS --password-stdin YOUR_AWS_ACCOUNT_ID.dkr.ecr.YOUR_AWS_REGION.amazonaws.com
    ```
6.  **イメージのタグ付けとプッシュ**:
    ```bash
    docker tag my-fastapi-app:latest YOUR_AWS_ACCOUNT_ID.dkr.ecr.YOUR_AWS_[REGION.amazonaws.com/my-fastapi-app-repo:latest](https://REGION.amazonaws.com/my-fastapi-app-repo:latest)
    docker push YOUR_AWS_ACCOUNT_ID.dkr.ecr.YOUR_AWS_[REGION.amazonaws.com/my-fastapi-app-repo:latest](https://REGION.amazonaws.com/my-fastapi-app-repo:latest)
    ```
    ECRリポジトリURIは、CDKスタックの環境変数 (`FASTAPI_APP_IMAGE_URI`) として渡します。

### 5.4. CognitoドメインとACM証明書の準備

1.  **ドメイン名の準備**: ALB1 (フロントALB) で使用するカスタムドメイン名 (例: `app.yourdomain.com`) を用意します。
2.  **ACM証明書のリクエスト**: 用意したドメイン名に対して、AWS Certificate Manager (ACM) でパブリックSSL/TLS証明書をリクエストし、検証を完了させます。
    * **重要**: 証明書はALB1と同じリージョンで作成する必要があります。
    * 証明書のARNは、CDKスタックの環境変数 (`ALB1_CERTIFICATE_ARN`) として渡すか、CDKコード内でDNS検証証明書として作成します。

### 5.5. 環境変数の設定

CDKスタックとFastAPIアプリケーションの動作に必要な環境変数を設定します。これらはCDKコード内で参照されたり、ECSタスク定義に渡されたりします。

**CDKスタックデプロイ時に必要な主な環境変数 (例):**

* `CDK_DEFAULT_ACCOUNT`: AWSアカウントID
* `CDK_DEFAULT_REGION`: AWSリージョン
* `COGNITO_DOMAIN_PREFIX`: Cognitoユーザープールドメインのプレフィックス (世界でユニークに)
* `DOMAIN_NAME`: ALB1で使用するカスタムドメイン名
* `HOSTED_ZONE_ID`: (オプション) `DOMAIN_NAME` を管理するRoute 53ホストゾーンID
* `ALB1_CERTIFICATE_ARN`: (オプション) ACM証明書のARN (CDKで証明書を作成しない場合)
* `NGINX_IMAGE_ID`: Nginx EC2インスタンス用のAMI ID
* `FASTAPI_APP_IMAGE_URI`: ECRにプッシュしたFastAPIアプリのDockerイメージURI
* `COGNITO_USER_POOL_ID_ENV`: (CDKでCognitoを作成する場合、デプロイ後にFastAPI用に設定)
* `COGNITO_REGION_ENV`: (CDKでCognitoを作成する場合、デプロイ後にFastAPI用に設定)
* `COGNITO_APP_CLIENT_ID_ENV`: (CDKでCognitoを作成する場合、デプロイ後にFastAPI用に設定)

**FastAPIアプリケーション (ECSタスク) に渡す環境変数 (CDKで設定):**

* `COGNITO_USER_POOL_ID`: FastAPIアプリが検証に使用するCognitoユーザープールID
* `COGNITO_REGION`: Cognitoユーザープールのリージョン
* `COGNITO_APP_CLIENT_ID`: FastAPIアプリが検証に使用するCognitoアプリクライアントID

### 5.6. CDKによるインフラストラクチャのデプロイ

CDKプロジェクトのルートディレクトリ (`cdk_project/` など) に移動し、デプロイコマンドを実行します。

1.  **(初回のみ) Python依存関係のインストール (CDK用):**
    ```bash
    cd cdk_project
    python -m venv .venv # 仮想環境の作成 (推奨)
    source .venv/bin/activate # 仮想環境のアクティベート (Linux/macOS)
    # .venv\Scripts\activate (Windows)
    pip install -r requirements.txt
    ```
2.  **CDKスタックの合成 (CloudFormationテンプレート生成の確認):**
    ```bash
    cdk synth
    ```
3.  **CDKスタックのデプロイ:**
    ```bash
    cdk deploy CdkScenarioBStack # スタック名を指定 (app.pyで定義した名前)
    ```
    デプロイ中、IAMポリシーの変更などについて確認を求められることがあります。

### 5.7. Nginx設定の動的更新に関する注意

Nginxの設定ファイル (`/etc/nginx/conf.d/app_proxy_passthrough.conf` など) 内で、バックエンドALB (ALB2) のDNS名を指定する必要があります。ALB2のDNS名はCDKデプロイが完了するまで確定しません。

以下のいずれかの方法で、Nginxインスタンス起動後またはCDKデプロイプロセス中にこのDNS名をNginx設定に反映させる必要があります。

* **ユーザーデータスクリプトの強化**: EC2インスタンスのユーザーデータ内で、AWS CLIやIMDS (Instance Metadata Service) を使用してALB2のDNS名を取得し、`sed`コマンドなどで設定ファイルを書き換える。
* **Systems Manager (SSM) Parameter Store連携**: CDKデプロイでALB2のDNS名をSSM Parameter Storeに保存し、Nginxのユーザーデータでそのパラメータを読み込んで設定ファイルに反映させる。
* **CDKカスタムリソース**: Lambda関数を利用したCDKカスタムリソースを作成し、デプロイの最終段階でNginxインスタンスの設定ファイルを更新する。

CDKコード例では、この部分が手動または別の自動化ステップで対応が必要であることを示唆しています。

## 6. ローカルでのFastAPIアプリケーション実行 (テスト・開発用)

1.  **必要な環境変数の設定**:
    ```bash
    export COGNITO_USER_POOL_ID="your-local-test-pool-id"
    export COGNITO_REGION="your-local-test-region"
    export COGNITO_APP_CLIENT_ID="your-local-test-app-client-id"
    # その他、アプリケーションが必要とする環境変数
    ```
2.  **Uvicornで起動**: FastAPIアプリケーションのルートディレクトリ (`app/` の親) で以下を実行します。
    ```bash
    # requirements.txt を使用している場合
    # (仮想環境をアクティベートしている前提)
    uvicorn app.main_app:app --reload --port 8000

    # Poetry を使用している場合
    # poetry run uvicorn app.main_app:app --reload --port 8000
    ```--reload` オプションは開発中にコード変更を自動でリロードします。

## 7. 使用技術・ライブラリ

* **AWS**:
    * CDK, VPC, EC2, ECS (Fargate), ECR, ALB, Cognito, Route 53 (オプション), ACM, S3, IAM, CloudWatch Logs
* **バックエンド**:
    * Python 3.9+
    * FastAPI
    * Uvicorn (ASGIサーバー)
    * python-jose[cryptography] (JWT検証)
    * requests (HTTPクライアント)
    * cachetools (キャッシュ)
* **フロントエンド/プロキシ**:
    * Nginx
* **その他**:
    * Docker
    * Node.js, npm (CDK用)
    * Git

## 8. 今後の改善点・考慮事項 (オプション)

* **CI/CDパイプラインの構築**: AWS CodePipeline, GitHub Actionsなどで、コード変更時の自動テスト、ビルド、デプロイを実現する。
* **モニタリングとアラート**: Amazon CloudWatch, AWS X-Rayなどで詳細なメトリクスを収集し、アラートを設定する。
* **セキュリティ強化**: WAF (Web Application Firewall) の導入、セキュリティグループルールの厳格化、Secrets Managerによる機密情報管理。
* **コスト最適化**: インスタンスタイプやFargateリソースの適切なサイジング、スポットインスタンスの活用検討。
* **テスト**: 単体テスト、結合テスト、E2Eテストの拡充。
* **データベース**: 必要に応じてRDSやDynamoDBなどのデータベースサービスをインテグレーションする。
* **Nginx設定の自動化**: ALB2 DNS名のNginx設定への反映を完全に自動化する (例: SSM Parameter Storeとユーザーデータの連携強化、またはCDKカスタムリソース)。

---

このREADMEはプロジェクトの基本的な情報を提供します。実際のプロジェクトに合わせて詳細を追加・修正してください。