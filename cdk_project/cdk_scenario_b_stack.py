from aws_cdk import (
    App, # Appクラスは通常app.pyで使用されますが、型ヒントなどでインポートされることがあります
    Stack,
    RemovalPolicy,
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    # aws_ecs_patterns は高レベルなパターンを提供しますが、今回は個別にリソースを定義
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elbv2_actions, # ALBのアクション用
    aws_cognito as cognito,
    aws_iam as iam,
    aws_certificatemanager as acm,
    aws_autoscaling as autoscaling, # EC2 Auto Scaling Group用
    aws_logs as logs, # CloudWatch Logs用
    CfnOutput # CloudFormation出力用
)
from constructs import Construct
import os

# --- 定数・設定値 (環境変数やcdk.jsonのcontextから読み込むことを推奨) ---

# Cognitoユーザープールドメインのプレフィックス (世界でユニークである必要があります)
COGNITO_DOMAIN_PREFIX = os.getenv("COGNITO_DOMAIN_PREFIX", "my-unique-fastapi-app-auth-sample")

# ALB1 (フロントALB) に関連付けるカスタムドメイン名 (オプション)
# 例: "app.yourdomain.com"
MY_DOMAIN_NAME = os.getenv("DOMAIN_NAME")

# MY_DOMAIN_NAME を管理するRoute 53ホストゾーンID (オプション、CDKでDNS検証証明書を作成する場合に必要)
MY_HOSTED_ZONE_ID = os.getenv("HOSTED_ZONE_ID")

# MY_DOMAIN_NAME 用に事前にACMで発行した証明書のARN (オプション、CDKで証明書を作成しない場合)
ALB1_CERTIFICATE_ARN = os.getenv("ALB1_CERTIFICATE_ARN")

# Nginx EC2インスタンス用のAMI ID (オプション)
# 指定しない場合は、CDKコード内でAmazon Linux 2の最新版が使用されます。
# 例: Amazon Linux 2にNginxをインストール・設定済みのカスタムAMI ID
NGINX_AMI_ID = os.getenv("NGINX_IMAGE_ID") # 指定がなければNone

# FastAPIアプリケーションのDockerイメージURI (ECRなど、必須)
# 例: "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/my-fastapi-repo:latest"
FASTAPI_APP_IMAGE_URI = os.getenv("FASTAPI_APP_IMAGE_URI")
if not FASTAPI_APP_IMAGE_URI:
    raise ValueError("環境変数 FASTAPI_APP_IMAGE_URI が設定されていません。ECRのイメージURIを指定してください。")


class CdkScenarioBStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- 1. VPCの作成 ---
        # 2つのAZにまたがるパブリックサブネットとプライベートサブネットを作成
        # NAT Gatewayも作成し、プライベートサブネットからのアウトバウンド通信を可能にする
        vpc = ec2.Vpc(self, "MyVPC",
            max_azs=2, # 利用可能なAZを最大2つ使用
            cidr="10.0.0.0/16", # VPCのCIDRブロック
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="PublicSubnet",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24 # 各サブネットのサイズ
                ),
                ec2.SubnetConfiguration(
                    name="PrivateSubnetWithEgress", # NAT Gateway経由でアウトバウンドアクセス可能
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                ),
                # アプリケーションの性質によってはPRIVATE_ISOLATEDも検討
                # ec2.SubnetConfiguration(
                #     name="PrivateSubnetIsolated", # インターネットアクセスなし
                #     subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                #     cidr_mask=28
                # )
            ],
            nat_gateways=1 # コスト削減のため1つ。高可用性が必要な場合はAZごとに設定 (max_azs数)
                           # 0にするとPRIVATE_WITH_EGRESSサブネットは作成されません (またはエラー)
        )

        # --- 2. Cognitoユーザープールとクライアントの作成 ---
        user_pool = cognito.UserPool(self, "MyUserPool",
            user_pool_name=f"{construct_id}-user-pool", # スタック名を含むユニークな名前
            self_sign_up_enabled=True, # 自己サインアップを許可 (デモ用)
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY, # アカウント復旧方法
            user_verification=cognito.UserVerificationConfig( # ユーザー検証設定
                email_style=cognito.VerificationEmailStyle.LINK # 検証メールのスタイル
            ),
            auto_verify=cognito.AutoVerifiedAttrs(email=True), # Eメールを自動検証
            standard_attributes=cognito.StandardAttributes( # 標準属性の設定
                email=cognito.StandardAttribute(required=True, mutable=True),
                preferred_username=cognito.StandardAttribute(required=False, mutable=True) # ユーザー名のエイリアス
            ),
            password_policy=cognito.PasswordPolicy( # パスワードポリシー
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False # デモ用にシンボルは不要とする
            ),
            removal_policy=RemovalPolicy.DESTROY # スタック削除時にUserPoolも削除 (本番ではRETAINまたはSNAPSHOTを検討)
        )

        # Cognitoドメイン (Cognitoがホストするサインイン/サインアップページ用)
        user_pool_domain = user_pool.add_domain("CognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=COGNITO_DOMAIN_PREFIX # 世界でユニークなプレフィックス
            )
        )

        # ALB1のDNS名が確定した後にコールバックURLを設定する必要がある。
        # 固定ドメインを使用する場合、そのドメインがALB1を指すようにRoute 53で設定。
        app_client_callback_urls = []
        app_client_logout_urls = []

        if MY_DOMAIN_NAME:
            app_client_callback_urls.append(f"https://{MY_DOMAIN_NAME}/oauth2/idpresponse")
            app_client_logout_urls.append(f"https://{MY_DOMAIN_NAME}/") # ログアウト後のリダイレクト先
        else:
            # カスタムドメインがない場合、ALBのDNS名を使うが、これはデプロイ後まで不明。
            # デプロイ後に手動で設定するか、カスタムリソースで更新する。
            # ここでは仮のURLか、空のリストのままにしておく。
            # CfnOutputでALBのDNS名を出力し、それを使って手動更新を促す。
            app_client_callback_urls.append("https://placeholder.example.com/oauth2/idpresponse") # 仮のURL
            app_client_logout_urls.append("https://placeholder.example.com/") # 仮のURL

        user_pool_client = user_pool.add_client("AppClientAlb",
            user_pool_client_name=f"{construct_id}-alb-auth-client",
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True), # SRP認証とID/Pass認証を許可
            generate_secret=False, # ALB連携ではクライアントシークレットは通常不要 (パブリッククライアント)
            o_auth=cognito.OAuthSettings( # OAuth 2.0設定
                flows=cognito.OAuthFlows(authorization_code_grant=True), # 認可コードグラントフロー
                scopes=[ # 要求するスコープ
                    cognito.OAuthScope.OPENID,      # OIDC必須
                    cognito.OAuthScope.EMAIL,       # Eメールアドレス
                    cognito.OAuthScope.PROFILE      # プロファイル情報 (名前など)
                ],
                callback_urls=app_client_callback_urls, # 認証後のリダイレクト先
                logout_urls=app_client_logout_urls    # ログアウト後のリダイレクト先
            ),
            supported_identity_providers=[ # サポートするIDプロバイダー
                cognito.UserPoolClientIdentityProvider.COGNITO # Cognito自身のユーザープール
            ],
            prevent_user_existence_errors=True # ユーザー存在エラーを隠蔽
        )

        # FastAPIアプリに渡すCognito設定値 (CDKで作成したリソースのIDを使用)
        cognito_user_pool_id_for_app_actual = user_pool.user_pool_id
        cognito_region_for_app_actual = self.region # スタックがデプロイされるリージョン
        cognito_app_client_id_for_app_actual = user_pool_client.user_pool_client_id


        # --- 3. ALB1 (フロント、インターネット向け) の作成 ---
        certificate_alb1 = None
        if MY_DOMAIN_NAME and MY_HOSTED_ZONE_ID: # Route 53でホストゾーン管理している場合
            hosted_zone = elbv2.HostedZone.from_hosted_zone_attributes(self, "MyHostedZone",
                hosted_zone_id=MY_HOSTED_ZONE_ID,
                zone_name=MY_DOMAIN_NAME
            )
            certificate_alb1 = acm.Certificate(self, "Alb1Certificate",
                domain_name=MY_DOMAIN_NAME,
                validation=acm.CertificateValidation.from_dns(hosted_zone) # DNS検証
            )
        elif ALB1_CERTIFICATE_ARN: # 事前に作成した証明書のARNを指定する場合
             certificate_alb1 = acm.Certificate.from_certificate_arn(self, "Alb1CertImported", ALB1_CERTIFICATE_ARN)

        # セキュリティグループ for ALB1
        sg_alb1 = ec2.SecurityGroup(self, "SgAlb1",
            vpc=vpc,
            description="Security group for ALB1 (front-facing)",
            allow_all_outbound=True # アウトバウンドは通常許可
        )
        sg_alb1.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "Allow HTTPS from anywhere")
        sg_alb1.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "Allow HTTP from anywhere (for redirect to HTTPS)")

        alb1 = elbv2.ApplicationLoadBalancer(self, "Alb1",
            vpc=vpc,
            internet_facing=True, # インターネット向け
            security_group=sg_alb1,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC) # パブリックサブネットに配置
        )
        # HTTPリクエストをHTTPSにリダイレクト
        alb1.add_redirect(source_protocol=elbv2.ApplicationProtocol.HTTP, source_port=80,
                          target_protocol=elbv2.ApplicationProtocol.HTTPS, target_port=443)

        # --- 4. Nginx EC2インスタンス (Auto Scaling Group) の作成 ---
        # セキュリティグループ for Nginx EC2
        sg_nginx = ec2.SecurityGroup(self, "SgNginx",
            vpc=vpc,
            description="Security group for Nginx EC2 instances",
            allow_all_outbound=True # 実際は必要なアウトバウンドのみ許可することを検討
        )
        sg_nginx.add_ingress_rule(sg_alb1, ec2.Port.tcp(80), "Allow HTTP from ALB1") # ALB1からのHTTPトラフィックを許可
        # SSHアクセスが必要な場合は、踏み台サーバー経由や特定のIPからのみ許可するように設定
        # 例: sg_nginx.add_ingress_rule(ec2.Peer.ip("YOUR_BASTION_IP/32"), ec2.Port.tcp(22), "Allow SSH from Bastion")

        # IAMロール for Nginx EC2 (Systems Managerでの管理、SSM Parameter Storeへのアクセスなど)
        nginx_ec2_role = iam.Role(self, "NginxEc2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"), # SSM Agent用
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMReadOnlyAccess") # Parameter Store読み取り用
            ]
        )

        # Nginx設定のユーザーデータ
        # ALB2のDNS名をSSM Parameter Storeから取得する例
        # このパラメータはALB2作成後にCDKのカスタムリソースなどで設定する必要がある
        alb2_dns_ssm_param_name = f"/{construct_id}/alb2/dnsName" # SSMパラメータ名

        nginx_user_data_script = f"""
#!/bin/bash
yum update -y
amazon-linux-extras install nginx1 -y # Amazon Linux 2でのNginxインストール
systemctl enable nginx
systemctl start nginx

# SSM Parameter StoreからALB2のDNS名を取得
# AWS CLIがEC2インスタンスにインストールされている前提 (Amazon Linux 2にはデフォルトで含まれる)
ALB2_DNS_NAME=$(aws ssm get-parameter --name "{alb2_dns_ssm_param_name}" --region {self.region} --query Parameter.Value --output text)

if [ -z "$ALB2_DNS_NAME" ] || [ "$ALB2_DNS_NAME" == "None" ]; then # パラメータが存在しないか値がNoneの場合
  echo "ALB2 DNS Name not found or is None in SSM Parameter Store. Using placeholder."
  ALB2_DNS_NAME="placeholder-alb2-dns.internal" # フォールバックDNS名
fi

# Nginx設定ファイルを作成 (シナリオB: パススルー)
cat <<EOF > /etc/nginx/conf.d/app_proxy_passthrough.conf
upstream backend_alb_target {{
    server $ALB2_DNS_NAME; # ALB2のDNS名に置き換える
    keepalive 32; # keepalive接続を有効化
}}

server {{
    listen 80 default_server;
    server_name _; # すべてのホスト名にマッチ

    # アクセスログとエラーログの設定 (任意)
    access_log /var/log/nginx/access.log main;
    error_log /var/log/nginx/error.log warn;

    location / {{
        proxy_pass http://backend_alb_target; # upstreamで定義したバックエンドALB
        proxy_set_header Host $http_host; # 元のHostヘッダーを渡す
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme; # http or https
        proxy_http_version 1.1; # HTTP/1.1を使用
        proxy_set_header Upgrade $http_upgrade; # WebSocket対応
        proxy_set_header Connection "upgrade";   # WebSocket対応

        # Cognito関連のヘッダーもバックエンドALB2に渡す
        proxy_set_header X-Amzn-Oidc-Accesstoken $http_x_amzn_oidc_accesstoken;
        proxy_set_header X-Amzn-Oidc-Data $http_x_amzn_oidc_data;
        proxy_set_header X-Amzn-Oidc-Identity $http_x_amzn_oidc_identity;
    }}
}}
EOF
systemctl restart nginx # Nginxを再起動して設定を反映
"""
        nginx_user_data = ec2.UserData.for_linux()
        nginx_user_data.add_commands(nginx_user_data_script)

        # Nginxインスタンス用のマシンイメージ
        if NGINX_AMI_ID:
            nginx_machine_image = ec2.MachineImage.generic_linux({self.region: NGINX_AMI_ID})
        else:
            nginx_machine_image = ec2.MachineImage.latest_amazon_linux(
                generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2
            )

        nginx_asg = autoscaling.AutoScalingGroup(self, "NginxAsg",
            vpc=vpc,
            instance_type=ec2.InstanceType("t3.micro"), # または適切なインスタンスタイプ
            machine_image=nginx_machine_image,
            user_data=nginx_user_data,
            role=nginx_ec2_role,
            security_group=sg_nginx,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS), # プライベートサブネットに配置
            min_capacity=1,
            max_capacity=2, # 必要に応じて調整
            desired_capacity=1,
            health_check=autoscaling.HealthCheck.ec2( # EC2のヘルスチェックを使用
                grace=Duration.minutes(5) # 起動後の猶予時間
            )
        )

        # Nginx ASGをターゲットとするターゲットグループ (ALB1用)
        nginx_target_group = elbv2.ApplicationTargetGroup(self, "NginxTg",
            vpc=vpc, port=80, protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.INSTANCE, # EC2インスタンスがターゲット
            targets=[nginx_asg],
            health_check=elbv2.HealthCheck( # ALBからのヘルスチェック
                path="/", # Nginxが200を返す簡単なパス (または専用ヘルスチェックパス)
                interval=Duration.seconds(60), # ヘルスチェック間隔
                timeout=Duration.seconds(5)    # タイムアウト
            )
        )

        # ALB1のHTTPSリスナーにCognito認証とNginxターゲットグループへの転送アクションを追加
        if certificate_alb1:
            https_listener = alb1.add_listener("HttpsListener", port=443, certificates=[certificate_alb1],
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS # 推奨SSLポリシー
            )
            https_listener.add_action("DefaultActionCognito",
                action=elbv2_actions.AuthenticateCognitoAction(
                    user_pool=user_pool,
                    user_pool_client=user_pool_client,
                    user_pool_domain=user_pool_domain,
                    next=elbv2.ListenerAction.forward([nginx_target_group]), # 認証後、Nginx TGへ転送
                    on_unauthenticated_request=elbv2.UnauthenticatedAction.AUTHENTICATE # 未認証リクエストは認証へ
                )
            )
        else:
            # 証明書がない場合はHTTPリスナーに直接ターゲットグループを設定 (テスト用、非推奨)
            # この場合、Cognito認証は機能しない
            # ALB1のHTTPリスナーはリダイレクト専用なので、ここではエラーまたは警告を出すのが適切
            CfnOutput(self, "WarningNoCertificate",
                value="ALB1 HTTPS Listener not created due to missing certificate. Cognito authentication will not work.",
                description="WARNING: No SSL certificate provided for ALB1."
            )


        # --- 5. ALB2 (バックエンド、内部) の作成 ---
        # セキュリティグループ for ALB2
        sg_alb2 = ec2.SecurityGroup(self, "SgAlb2",
            vpc=vpc,
            description="Security group for ALB2 (internal backend)",
            allow_all_outbound=True
        )
        # Nginx ASGのセキュリティグループからのみ許可
        sg_alb2.add_ingress_rule(sg_nginx, ec2.Port.tcp(80), "Allow HTTP from Nginx ASG")

        alb2 = elbv2.ApplicationLoadBalancer(self, "Alb2",
            vpc=vpc,
            internet_facing=False, # 内部ALB
            security_group=sg_alb2,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS) # プライベートサブネット
        )
        # NginxのユーザーデータでこのDNS名を参照するため、SSM Parameter Storeなどに保存する
        # CDKのカスタムリソースを使ってSSMパラメータを作成・設定するのが理想的
        # ここではCfnOutputでパラメータ名と実際のDNS名を出力し、手動または別プロセスでの設定を促す
        CfnOutput(self, "Alb2DnsNameSsmParameterHint",
            value=alb2_dns_ssm_param_name,
            description=f"Hint: Create SSM Parameter Store '{alb2_dns_ssm_param_name}' with value: {alb2.load_balancer_dns_name}"
        )
        CfnOutput(self, "Alb2ActualDnsName", value=alb2.load_balancer_dns_name, description="ALB2 Actual DNS Name")


        # --- 6. FastAPIバックエンド (ECS Fargateサービス) の作成 ---
        # ECSクラスター
        cluster = ecs.Cluster(self, "MyEcsCluster", vpc=vpc)

        # FastAPIアプリ用タスクIAMロール (アプリケーションが必要とするAWSサービスへのアクセス権限)
        fastapi_task_role = iam.Role(self, "FastApiTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
            # 例: S3バケットへのアクセス権限など
            # fastapi_task_role.add_to_policy(iam.PolicyStatement(...))
        )
        # ECRイメージプルとCloudWatch Logsへの書き込み権限を持つタスク実行IAMロール
        fastapi_execution_role = iam.Role(self, "FastApiExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
            ]
        )

        # FastAPIアプリのロググループ
        log_group_fastapi = logs.LogGroup(self, "FastApiLogGroup",
            log_group_name=f"/ecs/{construct_id}/fastapi-app", # ロググループ名
            retention=logs.RetentionDays.ONE_WEEK, # ログ保持期間 (例: 1週間)
            removal_policy=RemovalPolicy.DESTROY # スタック削除時にロググループも削除 (本番ではRETAINを検討)
        )

        # FastAPIアプリ用タスク定義
        fastapi_task_definition = ecs.FargateTaskDefinition(self, "FastApiTaskDef",
            memory_limit_mib=512, # メモリ (MiB)
            cpu=256,              # CPUユニット (1024 = 1vCPU)
            task_role=fastapi_task_role,          # タスクIAMロール
            execution_role=fastapi_execution_role # タスク実行IAMロール
        )

        # タスク定義にコンテナを追加
        fastapi_container = fastapi_task_definition.add_container("FastApiContainer",
            image=ecs.ContainerImage.from_registry(FASTAPI_APP_IMAGE_URI), # ECRのイメージURI
            logging=ecs.LogDrivers.aws_logs(stream_prefix="fastapi", log_group=log_group_fastapi), # ログ設定
            port_mappings=[ecs.PortMapping(container_port=8000, host_port=8000, protocol=ecs.Protocol.TCP)], # FastAPIアプリがリッスンするポート
            environment={ # FastAPIアプリに必要な環境変数を設定
                "COGNITO_USER_POOL_ID": cognito_user_pool_id_for_app_actual,
                "COGNITO_REGION": cognito_region_for_app_actual,
                "COGNITO_APP_CLIENT_ID": cognito_app_client_id_for_app_actual,
                "FASTAPI_ENV": "production", # 例: 環境を示す変数
                # 他の必要な環境変数
            }
        )

        # セキュリティグループ for FastAPI Fargateサービス
        sg_fastapi = ec2.SecurityGroup(self, "SgFastApi",
            vpc=vpc,
            description="Security group for FastAPI Fargate service",
            allow_all_outbound=True # Cognito JWKS取得のためなどに必要
        )
        sg_fastapi.add_ingress_rule(sg_alb2, ec2.Port.tcp(8000), "Allow HTTP from ALB2 on port 8000") # ALB2からコンテナポートへのアクセス許可

        # Fargateサービス
        fastapi_service = ecs.FargateService(self, "FastApiService",
            cluster=cluster, # ECSクラスター
            task_definition=fastapi_task_definition, # タスク定義
            desired_count=1, # 実行するタスク数 (本番では2以上を推奨し、Auto Scalingを設定)
            assign_public_ip=False, # プライベートサブネットで実行するためパブリックIPは不要
            security_groups=[sg_fastapi], # セキュリティグループ
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS), # 配置するサブネット
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True), # デプロイ失敗時の自動ロールバック
            # enable_ecs_managed_tags=True, # ECS管理タグを有効化
        )

        # FastAPIサービスをターゲットとするターゲットグループ (ALB2用)
        # ALB2のリスナーにターゲットグループを追加
        alb2_listener = alb2.add_listener("FastApiListener", port=80) # ALB2はHTTPでリッスン
        alb2_listener.add_targets("FastApiTarget",
            port=80, # リスナーポート
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[fastapi_service.load_balancer_target( # Fargateサービスをターゲットとして指定
                container_name="FastApiContainer", # コンテナ名
                container_port=8000                 # コンテナがリッスンするポート
            )],
            health_check=elbv2.HealthCheck( # ヘルスチェック設定
                path="/", # FastAPIアプリのヘルスチェックエンドポイント (例: `/` や `/health`)
                interval=Duration.seconds(30), # ヘルスチェック間隔
                timeout=Duration.seconds(5),   # タイムアウト
                healthy_threshold_count=2,     # 正常と判断するまでの連続成功回数
                unhealthy_threshold_count=2,   # 不健康と判断するまでの連続失敗回数
                port="8000"                    # ターゲットのヘルスチェックポート
            )
        )

        # --- 7. CloudFormation Outputs (デプロイ後に確認できる値) ---
        CfnOutput(self, "Alb1DnsOutput", value=alb1.load_balancer_dns_name, description="ALB1 DNS Name")
        CfnOutput(self, "CognitoUserPoolIdOutput", value=user_pool.user_pool_id, description="Cognito User Pool ID")
        CfnOutput(self, "CognitoAppClientIdOutput", value=user_pool_client.user_pool_client_id, description="Cognito App Client ID")
        CfnOutput(self, "CognitoDomainUrlOutput",
            value=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
            description="Cognito Hosted UI URL (for sign-in/sign-up)"
        )
        if MY_DOMAIN_NAME:
            CfnOutput(self, "ApplicationCustomDomainOutput", value=f"https://{MY_DOMAIN_NAME}/",
                description="Application Endpoint URL (if custom domain is configured)"
            )
        CfnOutput(self, "FastApiEcsLogGroupNameOutput", value=log_group_fastapi.log_group_name, description="CloudWatch Log Group name for FastAPI ECS service")